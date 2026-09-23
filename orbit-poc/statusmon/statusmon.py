"""Status monitor — every service and the pipeline itself, visible (#470).

Rule 34: if a component's failure would show up nowhere on Grafana, the
component is not done. The week that taught it: a healthcheck that had never
executed (#463), a test suite CI never collected (#469), a weekly sweep
failing five Mondays in a row — all invisible unless someone read container
logs or the contact@ mailbox.

Two loops, one tiny process:

  * every PROBE_INTERVAL: one cheap HTTP GET per service (or SELECT 1 for the
    database) -> a `service_health` row. Services with no port of their own
    are probed through the door that fronts them (caddy vhosts), which also
    proves the door. The ingest has no port at all: its liveness is already
    on the boards through upstream_request/telemetry freshness, so it is
    deliberately not here.
  * every PIPELINE_INTERVAL: the repo's recent workflow runs from the public
    GitHub API (unauthenticated, ~12 requests/hour against a 60/h limit) ->
    `pipeline_run` upserts, so test/build/deploy outcomes chart next to
    stage/promote (#382) instead of living only in checks and mail.

Same shape as the gateway: stdlib + requests + psycopg2, everything
injectable, so the logic is unit-testable with no network and no database.
"""
import json
import logging
import re
import os
import threading
import time

import psycopg2
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("statusmon")

DB_DSN = os.environ.get("DB_DSN", "")
PROBE_INTERVAL = float(os.environ.get("PROBE_INTERVAL", 60))
PIPELINE_INTERVAL = float(os.environ.get("PIPELINE_INTERVAL", 300))
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", 5))
# A write+fsync slower than this is "down" for the disk probe (#492): the VM's
# spinning pair sat at 36 % iowait for hours with no process moving data, and
# nothing on the boards said so while container healthchecks timed out.
FSYNC_MAX_MS = int(os.environ.get("FSYNC_MAX_MS", 1000))
FSYNC_FILE = os.environ.get("FSYNC_FILE", "/tmp/fsync-probe")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "confinia/overwatch")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", 14))
PIPELINE_KEEP = int(os.environ.get("PIPELINE_KEEP", 500))
# The healthcheck (healthz.py) verifies this file stays fresh, so a wedged
# probe loop turns the CONTAINER unhealthy — the monitor is monitored.
BEAT_FILE = os.environ.get("BEAT_FILE", "/tmp/beat")
UA = {"User-Agent":
      "overwatch-statusmon/1.0 (+https://overwatch.confinia.io; contact@confinia.io)"}

# service -> (url, Host header or None). Services without a port of their own
# are reached through caddy, which makes every probe of them a probe of the
# door too. `sql` is special-cased: the database speaks no HTTP.
TARGETS = {
    "db": ("sql", None),
    "disk (fsync)": ("fsync", None),
    "satnogs-gateway": ("http://satnogs-gateway:8088/healthz", None),
    "web (via caddy)": ("http://caddy:80/healthz", "overwatch.confinia.io"),
    "api (via caddy)": ("http://caddy:80/api/v1/healthz", "overwatch.confinia.io"),
    "grafana": ("http://grafana:3000/api/health", None),
    "keycloak (via caddy)": ("http://caddy:80/auth/realms/master",
                             "overwatch.confinia.io"),
    "prometheus": ("http://prometheus:9090/-/healthy", None),
    # any HTTP answer (404 included) proves the collector is up; a dead one
    # refuses the connection.
    "otel-collector": ("http://otel-collector:4318/", None),
}

DDL = """
CREATE TABLE IF NOT EXISTS service_health (
    ts      timestamptz NOT NULL DEFAULT now(),
    service text        NOT NULL,
    ok      boolean     NOT NULL,
    ms      integer,
    detail  text
);
CREATE INDEX IF NOT EXISTS service_health_idx ON service_health (service, ts DESC);
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id     bigint PRIMARY KEY,
    workflow   text NOT NULL,
    sha        text NOT NULL,
    title      text,
    pr         integer,
    refs       text,
    event      text,
    status     text,
    conclusion text,
    created    timestamptz,
    updated    timestamptz,
    url        text
);
"""


def db():
    return psycopg2.connect(DB_DSN)


def probe(url, host=None, get=None, now=time.monotonic):
    """One reachability check -> (ok, ms, detail). Any HTTP status below 500
    is 'up': a 404 from a live process is reachability, not health of the
    route — the per-service healthz paths make 200 the normal case anyway."""
    get = get or requests.get
    headers = dict(UA)
    if host:
        headers["Host"] = host
    t0 = now()
    try:
        r = get(url, headers=headers, timeout=PROBE_TIMEOUT)
        ms = int((now() - t0) * 1000)
        status = getattr(r, "status_code", 0)
        return status < 500, ms, f"http {status}"
    except Exception as e:  # noqa: BLE001 — the failure IS the measurement
        return False, int((now() - t0) * 1000), type(e).__name__


def probe_db(connect=None, now=time.monotonic):
    t0 = now()
    try:
        with (connect or db)() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True, int((now() - t0) * 1000), "select 1"
    except Exception as e:  # noqa: BLE001
        return False, int((now() - t0) * 1000), type(e).__name__


def probe_fsync(path=None, max_ms=None, now=time.monotonic, fsync=os.fsync):
    """Disk latency as a service (#492): write 4 KB and fsync it, the way
    every Postgres commit and journal write does. Slow is down: the number
    is the point, and a disk that takes a second per sync is failing the
    whole host whatever the processes on it report."""
    path = path or FSYNC_FILE
    max_ms = FSYNC_MAX_MS if max_ms is None else max_ms
    t0 = now()
    try:
        with open(path, "wb") as f:
            f.write(b"\0" * 4096)
            f.flush()
            fsync(f.fileno())
        ms = int((now() - t0) * 1000)
        return ms <= max_ms, ms, f"fsync 4k (max {max_ms} ms)"
    except Exception as e:  # noqa: BLE001
        return False, int((now() - t0) * 1000), type(e).__name__


def run_probes(record, targets=None, get=None):
    """One pass over every target. One slow service must not hide the rest,
    so each probe records independently."""
    for service, (url, host) in (targets or TARGETS).items():
        if url == "sql":
            ok, ms, detail = probe_db()
        elif url == "fsync":
            ok, ms, detail = probe_fsync()
        else:
            ok, ms, detail = probe(url, host, get)
        record(service, ok, ms, detail)


def parse_refs(title):
    """(pr, refs) out of a run title. A squash-merge title ends with the PR
    number — "web: usable on a phone … (#471) (#472)" — so the LAST #N is the
    PR and the ones before it are the issues the change was for."""
    ns = re.findall(r"#(\d+)", title or "")
    if not ns:
        return None, ""
    return int(ns[-1]), " ".join(f"#{n}" for n in ns[:-1])


def fetch_runs(get=None, repo=GITHUB_REPO):
    """Recent workflow runs from the public API. Returns [] on any failure —
    a GitHub hiccup must not mark our own services down."""
    get = get or requests.get
    try:
        r = get(f"https://api.github.com/repos/{repo}/actions/runs?per_page=30",
                headers=UA, timeout=30)
        if r.status_code != 200:
            log.warning("pipeline poll: http %s", r.status_code)
            return []
        rows = []
        for run in r.json().get("workflow_runs", []):
            title = run.get("display_title") or ""
            pr, refs = parse_refs(title)
            rows.append(
                (run["id"], run.get("name") or run.get("path", "?"),
                 run.get("head_sha", ""), title, pr, refs, run.get("event"),
                 run.get("status"), run.get("conclusion"),
                 run.get("created_at"), run.get("updated_at"),
                 run.get("html_url")))
        return rows
    except Exception as e:  # noqa: BLE001
        log.warning("pipeline poll failed: %s", e)
        return []


def store_runs(conn, rows):
    with conn.cursor() as cur:
        for row in rows:
            cur.execute("""
                INSERT INTO pipeline_run (run_id, workflow, sha, title, pr,
                                          refs, event, status, conclusion,
                                          created, updated, url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE
                   SET status = EXCLUDED.status,
                       conclusion = EXCLUDED.conclusion,
                       updated = EXCLUDED.updated""", row)
    conn.commit()


def prune(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM service_health WHERE ts < now() - %s * interval '1 day'",
                    (RETENTION_DAYS,))
        cur.execute("""DELETE FROM pipeline_run WHERE run_id NOT IN (
                       SELECT run_id FROM pipeline_run
                       ORDER BY updated DESC NULLS LAST LIMIT %s)""",
                    (PIPELINE_KEEP,))
    conn.commit()


def _beat():
    with open(BEAT_FILE, "w") as f:
        f.write(str(int(time.time())))


def probe_loop():
    while True:
        try:
            with db() as conn:
                def record(service, ok, ms, detail):
                    with conn.cursor() as cur:
                        cur.execute("INSERT INTO service_health (service, ok, ms, detail) "
                                    "VALUES (%s, %s, %s, %s)", (service, ok, ms, detail))
                    conn.commit()
                run_probes(record)
        except Exception as e:  # noqa: BLE001 — the db being down is a probe result we cannot store
            log.warning("probe pass failed: %s", e)
        _beat()
        time.sleep(PROBE_INTERVAL)


def pipeline_loop():
    last_prune = 0.0
    while True:
        try:
            rows = fetch_runs()
            if rows:
                with db() as conn:
                    store_runs(conn, rows)
                    if time.monotonic() - last_prune > 3600:
                        prune(conn)
                        last_prune = time.monotonic()
        except Exception as e:  # noqa: BLE001
            log.warning("pipeline pass failed: %s", e)
        time.sleep(PIPELINE_INTERVAL)


def main():
    for attempt in range(60):           # the db may still be coming up
        try:
            with db() as conn, conn.cursor() as cur:
                cur.execute(DDL)
            break
        except Exception as e:  # noqa: BLE001
            log.info("waiting for the database (%s)", type(e).__name__)
            time.sleep(5)
    _beat()
    threading.Thread(target=pipeline_loop, daemon=True).start()
    log.info("statusmon: %d services every %.0fs, pipeline every %.0fs",
             len(TARGETS), PROBE_INTERVAL, PIPELINE_INTERVAL)
    probe_loop()


if __name__ == "__main__":
    main()
