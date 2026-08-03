"""Guards issue #168: the admin-only ops dashboards live in their OWN Grafana
org, backed by `ops_ro` — a role that can read the account/metering tables and
nothing else. Three invariants, against a real Postgres:

1. `ops_ro` reads every table the ops dashboards query, and is refused tenant
   payloads (tenant_telemetry) and writes.
2. The ops dashboard JSON only references tables `ops_ro` is granted (a new
   panel on an ungranted table must fail CI, prompting a deliberate grant) and
   only the per-org datasource uids the provisioning creates.
3. The ops dashboards are OUT of the org-1 file-provisioning path — in org 1
   the datasource role is grafana_ro (#129), which is refused these tables, so
   re-adding them there would only recreate the blank boards this issue fixes.
"""
import json
import glob
import os
import re
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402

DSN = os.environ["DB_DSN"]
PW = "test-ops-ro-pw"
HERE = os.path.dirname(__file__)
OPS_DIR = os.path.join(HERE, "..", "grafana", "ops-dashboards")
ORG1_DIR = os.path.join(HERE, "..", "grafana", "dashboards")


@pytest.fixture(scope="module")
def ops_conn():
    os.environ["OPS_DB_PASSWORD"] = PW
    admin = psycopg2.connect(DSN)
    with admin, admin.cursor() as cur:
        cur.execute(main.KEYS_SQL)                # the tables the grants target
        main._provision_ops_role(cur)
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    conn = psycopg2.connect(dbname=parts["dbname"], host=parts["host"],
                            port=parts.get("port", "5432"),
                            user=main.OPS_ROLE, password=PW)
    yield conn
    conn.close()
    admin.close()


def test_ops_role_is_not_privileged(ops_conn):
    with ops_conn.cursor() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                    "FROM pg_roles WHERE rolname = %s", (main.OPS_ROLE,))
        assert cur.fetchone() == (False, False, False, False)


def test_ops_role_reads_every_ops_table(ops_conn):
    for t in main.OPS_TABLES:
        with ops_conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {t}")     # must not raise
            assert cur.fetchone()[0] >= 0
        ops_conn.commit()


def test_ops_role_cannot_read_tenant_payloads(ops_conn):
    """Tenant telemetry stays out of ops — and out of anything a leaked ops
    credential could reach."""
    for t in ("tenant_telemetry", "billing_event"):
        with ops_conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(f"SELECT * FROM {t} LIMIT 1")
        ops_conn.rollback()


def test_concurrent_worker_startup_does_not_deadlock(ops_conn):
    """Several uvicorn workers run the startup DDL at once; unserialized, the
    grafana_ro + ops_ro REVOKE/GRANT sequences deadlock (9 restarts per
    sandbox deploy). _startup_provision must hold the advisory lock so
    parallel boots come out clean."""
    os.environ["GRAFANA_DB_PASSWORD"] = "test-grafana-ro-pw"
    errors = []

    def worker():
        try:
            conn = psycopg2.connect(DSN)
            for _ in range(3):
                main._startup_provision(conn)
            conn.close()
        except Exception as e:                     # noqa: BLE001 — collected
            errors.append(e)

    import threading
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_ops_role_cannot_write(ops_conn):
    with ops_conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM organization")
    ops_conn.rollback()


def _panels(d):
    for p in d.get("panels", []):
        yield p
        yield from p.get("panels", [])


def test_ops_dashboards_query_only_granted_tables():
    files = glob.glob(os.path.join(OPS_DIR, "*.json"))
    assert len(files) >= 3                        # accounts-orgs, api-access, platform-access
    allowed = set(main.OPS_TABLES)
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        for p in _panels(d):
            for t in p.get("targets", []):
                for table in re.findall(r"(?:FROM|JOIN)\s+([a-z_]+)",
                                        t.get("rawSql", ""), re.I):
                    assert table in allowed, f"{f}: table {table} not granted to ops_ro"


def test_ops_dashboards_use_per_org_datasource_uids():
    """The provisioning recreates org 1's uids inside the ops org, so the JSON
    must reference exactly those."""
    for f in glob.glob(os.path.join(OPS_DIR, "*.json")):
        d = json.load(open(f, encoding="utf-8"))
        for p in _panels(d):
            ds = p.get("datasource") or {}
            if isinstance(ds, dict) and ds.get("uid"):
                assert ds["uid"] in ("orbitcache", "promops"), f"{f}: {ds}"


def test_ops_dashboards_are_out_of_the_org1_provider_path():
    org1 = {os.path.basename(f)
            for f in glob.glob(os.path.join(ORG1_DIR, "**", "*.json"), recursive=True)}
    for name in ("accounts-orgs.json", "api-access.json", "platform-access.json"):
        assert name not in org1, f"{name} back in org 1, where grafana_ro cannot read it"
