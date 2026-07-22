"""Row-level-security isolation test for #13 (rule 13: every issue ships a test).

Proves the DB-level guarantee behind per-org Grafana datasources: an org's
read role can SELECT only that org's telemetry, even with a raw query — the
isolation does not rely on the application filtering.
"""
import os
import uuid
import psycopg2
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DB_DSN", "dbname=orbit user=orbit password=orbit host=localhost port=5432")
os.environ.setdefault("ORG_DB_SECRET", "test-org-db-secret")
import main  # noqa: E402


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(main, "RATE_PER_SEC", 10**9)
    monkeypatch.setattr(main, "RATE_PER_MIN", 10**9)
    main._rate.clear()


def _as_user(monkeypatch, org_id, name):
    claims = {"sub": str(uuid.uuid4()), "email": "u@x.io", "name": "U",
              "organization": {name: {"id": org_id}}}
    monkeypatch.setattr(main, "_claims", lambda request: claims)


def _role_conn(org_id):
    role, pw = main._org_role(org_id)
    dsn = os.environ["DB_DSN"]
    host = dict(kv.split("=", 1) for kv in dsn.split()).get("host", "localhost")
    return psycopg2.connect(host=host, dbname="orbit", user=role, password=pw)


def test_rls_blocks_cross_org_reads(monkeypatch):
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    with TestClient(main.app) as c:
        # Touching an org endpoint provisions its RLS role + policy and pushes data.
        _as_user(monkeypatch, org_a, "A")
        tok_a = c.post("/v1/org/tokens", json={"label": "a"}).json()["token"]
        c.post(f"/v1/tenants/{tok_a}/telemetry", json={"satellite": "A1",
               "points": [{"ts": "2026-07-22T10:00:00Z", "field": "battery_v", "value": 7.4}]})
        _as_user(monkeypatch, org_b, "B")
        tok_b = c.post("/v1/org/tokens", json={"label": "b"}).json()["token"]
        c.post(f"/v1/tenants/{tok_b}/telemetry", json={"satellite": "B1",
               "points": [{"ts": "2026-07-22T10:00:00Z", "field": "battery_v", "value": 8.1}]})

    # Org A's DB role sees ONLY org A's rows — a raw, unfiltered SELECT.
    conn = _role_conn(org_a)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT tenant::text FROM tenant_telemetry")
        seen = {r[0] for r in cur.fetchall()}
    conn.close()
    assert seen == {org_a}, f"RLS leak: role for {org_a} saw {seen}"

    # And org B's role sees only B — symmetric.
    conn = _role_conn(org_b)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tenant_telemetry WHERE tenant = %s::uuid", (org_a,))
        assert cur.fetchone()[0] == 0        # cannot see A even by asking for it
    conn.close()
