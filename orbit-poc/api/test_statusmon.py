"""Guards #470 (rule 34): every service and the pipeline itself must be visible
on Grafana. The week that wrote the rule: a healthcheck that had never run
(#463), a test suite CI never collected (#469), a weekly sweep failing five
Mondays unnoticed. Two halves here: the statusmon logic with injected get/db
(no network), and the compose/ops wiring that makes its rows reach a board.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "statusmon"))
import statusmon  # noqa: E402

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..", "..")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


class FakeResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


# ---- probes -------------------------------------------------------------

def test_a_404_from_a_live_process_is_up_but_a_5xx_or_refusal_is_down():
    up = statusmon.probe("http://x/", get=lambda *a, **k: FakeResp(404))
    assert up[0] is True and up[2] == "http 404"
    down = statusmon.probe("http://x/", get=lambda *a, **k: FakeResp(503))
    assert down[0] is False

    def refuse(*a, **k):
        raise ConnectionError("refused")
    ok, ms, detail = statusmon.probe("http://x/", get=refuse)
    assert ok is False and detail == "ConnectionError", \
        "an exception is the measurement, never a crash of the loop"


def test_a_host_header_reaches_the_door_for_services_behind_caddy():
    seen = {}

    def get(url, headers=None, timeout=None):
        seen.update(headers)
        return FakeResp(200)
    statusmon.probe("http://caddy:80/healthz", host="overwatch.confinia.io", get=get)
    assert seen["Host"] == "overwatch.confinia.io"
    assert "overwatch-statusmon" in seen["User-Agent"]


def test_one_slow_or_dead_service_never_hides_the_others():
    rows = []

    def get(url, headers=None, timeout=None):
        if "dead" in url:
            raise TimeoutError()
        return FakeResp(200)
    targets = {"a": ("http://a/", None), "dead": ("http://dead/", None),
               "b": ("http://b/", None)}
    statusmon.run_probes(lambda *r: rows.append(r), targets=targets, get=get)
    assert [r[0] for r in rows] == ["a", "dead", "b"]
    assert [r[1] for r in rows] == [True, False, True]


def test_every_target_is_a_service_of_the_prod_compose():
    compose = _read("orbit-poc", "docker-compose.yml")
    for service, (url, _host) in statusmon.TARGETS.items():
        if url in ("sql", "fsync"):
            continue
        name = url.split("//")[1].split(":")[0]
        assert f"\n  {name}:\n" in compose, f"{service}: no compose service {name}"


def test_disk_latency_is_a_service_and_slow_is_down(tmp_path):
    """#492: the VM's disks sat at 36 % iowait for hours with nothing on the
    boards saying so. A write+fsync per pass makes it a row like the rest."""
    clock = iter([0.0, 0.2, 0.0, 3.0])
    path = str(tmp_path / "probe")
    ok, ms, detail = statusmon.probe_fsync(path, max_ms=1000, now=lambda: next(clock))
    assert (ok, ms) == (True, 200) and "1000 ms" in detail
    assert os.path.getsize(path) == 4096
    ok, ms, _ = statusmon.probe_fsync(path, max_ms=1000, now=lambda: next(clock))
    assert (ok, ms) == (False, 3000)                 # slow IS down

    def broken(fd):
        raise OSError("EIO")
    ok, _, detail = statusmon.probe_fsync(path, max_ms=1000, fsync=broken)
    assert ok is False and detail == "OSError"
    assert statusmon.TARGETS["disk (fsync)"] == ("fsync", None)


def test_the_healthcheck_outlives_disk_latency():
    """#492: 5 s was Python start-up under iowait; the check timed out for an
    hour while every probe landed. The heartbeat window is the real signal."""
    compose = _read("orbit-poc", "docker-compose.yml")
    block = compose[compose.index("\n  statusmon:\n"):]
    block = block[:block.index("\n  caddy:")]
    assert "timeout: 20s" in block


# ---- pipeline poller ----------------------------------------------------

def test_the_pr_is_the_last_ref_and_the_issues_are_the_rest():
    assert statusmon.parse_refs("web: usable on a phone (#471) (#472)") == (472, "#471")
    assert statusmon.parse_refs("gateway: restart race (#463, #456) (#469)") == \
        (469, "#463 #456")
    assert statusmon.parse_refs("Merge branch 'x'") == (None, "")
    assert statusmon.parse_refs(None) == (None, "")


def test_runs_become_rows_with_title_pr_and_refs():
    body = {"workflow_runs": [{
        "id": 35468309199, "name": "deploy", "head_sha": "884d081abc",
        "display_title": "web: usable on a phone (#471) (#472)", "event": "push",
        "status": "completed", "conclusion": "success",
        "created_at": "2026-09-19T20:00:00Z", "updated_at": "2026-09-19T20:20:00Z",
        "html_url": "https://github.com/confinia/overwatch/actions/runs/35468309199"}]}
    rows = statusmon.fetch_runs(get=lambda *a, **k: FakeResp(200, body))
    assert len(rows) == 1
    run_id, wf, sha, title, pr, refs, *_rest, url = rows[0]
    assert (run_id, wf, sha, pr, refs) == (35468309199, "deploy", "884d081abc", 472, "#471")
    assert url.endswith("/35468309199")


def test_a_github_hiccup_yields_no_rows_not_a_crash():
    assert statusmon.fetch_runs(get=lambda *a, **k: FakeResp(403)) == []

    def boom(*a, **k):
        raise OSError("dns")
    assert statusmon.fetch_runs(get=boom) == []


def test_store_upserts_on_run_id_so_a_run_in_progress_is_updated_not_duplicated():
    class Cur:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            self.sql.append(sql)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class Conn:
        def __init__(self):
            self.cur = Cur()
            self.commits = 0

        def cursor(self):
            return self.cur

        def commit(self):
            self.commits += 1
    conn = Conn()
    statusmon.store_runs(conn, [(1, "unit tests", "abc", "t", None, "", "push",
                                 "in_progress", None, None, None, "u")])
    assert "ON CONFLICT (run_id) DO UPDATE" in conn.cur.sql[0]
    assert conn.commits == 1


# ---- wiring: compose, schema, boards -------------------------------------

def test_the_monitor_runs_under_compose_with_a_bare_argv_healthcheck():
    compose = _read("orbit-poc", "docker-compose.yml")
    assert "\n  statusmon:\n" in compose, "rule 33: managed by compose or it does not exist"
    block = compose[compose.index("\n  statusmon:\n"):]
    block = block[:block.index("\n  caddy:")]
    assert 'test: ["CMD", "python", "healthz.py"]' in block, \
        "podman-compose flattens CMD through sh (#463): plain argv only"
    assert "restart: unless-stopped" in block
    docker = _read("orbit-poc", "statusmon", "Dockerfile")
    assert "healthz.py" in docker, "the healthcheck script must ship in the image"


def test_the_api_owns_the_schema_and_grants_it_to_the_ops_reader():
    src = _read("orbit-poc", "api", "main.py")
    for table in ("service_health", "pipeline_run"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in src, \
            f"{table} must exist after a plain api boot, or the ops grant never lands"
    tables = src[src.index("OPS_TABLES = ("):][:1200]
    assert '"service_health"' in tables and '"pipeline_run"' in tables, \
        "a table ops_ro cannot read renders as an empty board, not an error"
    # Both sides repeat the CREATE (the monitor may boot before a new api);
    # the column lists must not drift apart.
    for col in ("title", "pr", "refs", "conclusion", "url"):
        assert f"    {col} " in statusmon.DDL and f"    {col} " in src


def _board(name):
    return json.loads(_read("orbit-poc", "grafana", "ops-dashboards", name))


def test_the_deployments_board_shows_all_four_environments_with_pr_and_issues():
    board = _board("deployments.json")
    assert board["uid"] == "deployments"
    sql = json.dumps(board)
    for env in ("'test'", "'sandbox'", "'staging'", "'prod'"):
        assert env in sql, f"environment {env} missing from the board"
    assert "pipeline_run" in sql and "deploy_event" in sql, \
        "test/sandbox come from the poller, staging/prod from deploy.yml's rows"
    assert "github.com/confinia/overwatch/pull/${__value.text}" in sql, \
        "the PR column must link to the pull request"
    assert 'AS \\"Issues\\"' in sql and 'AS \\"Change\\"' in sql, \
        "the table must carry the issues and the change title, not just a sha"
    for panel in board["panels"]:
        for t in panel.get("targets", []):
            assert t["datasource"]["uid"] == "orbitcache-ops", \
                "a null datasource resolves to the denied default (#320)"


def test_the_service_health_board_reads_the_probes_and_watches_the_monitor():
    board = _board("service-health.json")
    assert board["uid"] == "service-health"
    sql = json.dumps(board)
    assert "service_health" in sql
    assert "Monitor freshness" in sql, \
        "a dead statusmon must be visible too, not a board frozen on green"
    types = {p["type"] for p in board["panels"]}
    assert "state-timeline" in types, "up/down over time is the point"
    for panel in board["panels"]:
        for t in panel.get("targets", []):
            assert t["datasource"]["uid"] == "orbitcache-ops"


def test_every_built_core_service_is_recreated_by_the_deploy():
    """#480: statusmon merged, staged, promoted, and never existed on prod,
    because the deploy step names the services it builds and recreates. A
    core service with a `build:` (its own code, so `--no-recreate` would pin
    a stale image forever) must be in that list, or it is not deployed."""
    compose = open(os.path.join(HERE, "..", "docker-compose.yml"), encoding="utf-8").read()
    built = []
    for i, line in enumerate(compose.splitlines()):
        if line.startswith("    build:"):
            above = [l for l in compose.splitlines()[:i] if l.startswith("  ") and not l.startswith("   ")]
            built.append(above[-1].strip().rstrip(":"))
    assert "statusmon" in built and "ingest" in built
    wf = next(p for p in (os.path.join(HERE, "..", "..", ".github", "workflows", "deploy.yml"),
                          os.path.join(HERE, "..", ".github", "workflows", "deploy.yml"))
              if os.path.exists(p))
    deploy = open(wf, encoding="utf-8").read()
    for svc in built:
        assert f"podman rm -f orbit-poc_{svc}_1" in deploy, f"{svc} is never recreated on prod"
        assert f"up -d --no-deps {svc} " in deploy, f"{svc} is never started on prod"
    m = re.search(r"for svc in ([a-z\- ]+); do", deploy)
    assert m and set(m.group(1).split()) >= set(built), "image freshness is asserted for every built service"
