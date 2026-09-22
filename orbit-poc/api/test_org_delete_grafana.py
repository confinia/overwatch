"""Guards #478: deleting an organization takes its private Grafana org with it,
and a boot sweep clears the tombstones that were left holding one (158 dead
"E2E Bot Org" orgs on the sandbox, one more per e2e run before this).

Grafana is faked at the single boundary (`_gf`); the organization rows are
real, in the CI Postgres, because the contract is "the column is NULL once the
org is gone, and kept while it is not".
"""
import inspect
import os
import uuid

import psycopg2
import pytest
import requests
from fastapi.testclient import TestClient

os.environ.setdefault("DB_DSN", "dbname=orbit user=orbit password=orbit host=localhost port=5432")
import main  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(main, "RATE_PER_SEC", 10**9)
    monkeypatch.setattr(main, "RATE_PER_MIN", 10**9)
    main._rate.clear()


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def _fake_grafana(monkeypatch, calls, status=200):
    """Grafana admin API stand-in: records (method, path), answers `status`,
    or raises when `status` is an exception class (Grafana down)."""
    monkeypatch.setattr(main, "GF_ADMIN_PASS", "secret")

    def fake_gf(method, path, body=None, gorg=None):
        calls.append((method, path))
        if isinstance(status, type):
            raise status("grafana down")
        return _Resp(status)

    monkeypatch.setattr(main, "_gf", fake_gf)
    monkeypatch.setattr(main, "_kc_admin_token", lambda: (_ for _ in ()).throw(RuntimeError("no kc")))


def _grafana_org_id(org_id):
    conn = psycopg2.connect(os.environ["DB_DSN"])
    with conn, conn.cursor() as cur:
        cur.execute("SELECT grafana_org_id, archived_at FROM organization WHERE id=%s", (org_id,))
        row = cur.fetchone()
    conn.close()
    return row


def _org_with_grafana(client, monkeypatch, gorg):
    """A signed-in user materializes their org (Grafana unconfigured in CI, so
    no provisioning happens); give it a Grafana org id the way
    _provision_grafana_org would have. Call before faking Grafana."""
    org = str(uuid.uuid4())
    claims = {"sub": str(uuid.uuid4()), "email": "u@example.com", "name": "U",
              "organization": {"Temp Org": {"id": org}}}
    monkeypatch.setattr(main, "_claims", lambda request: claims)
    assert client.get("/v1/org/satellites").status_code == 200
    conn = psycopg2.connect(os.environ["DB_DSN"])
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE organization SET grafana_org_id=%s WHERE id=%s", (gorg, org))
    conn.close()
    return org


def test_delete_org_removes_the_grafana_org(client, monkeypatch):
    org = _org_with_grafana(client, monkeypatch, 77)
    calls = []
    _fake_grafana(monkeypatch, calls)

    r = client.delete(f"/v1/orgs/{org}")
    assert r.status_code == 200 and r.json()["deleted"] == org
    assert ("DELETE", "/orgs/77") in calls
    gorg, archived = _grafana_org_id(org)
    assert archived is not None                      # tombstone kept
    assert gorg is None                              # Grafana org gone: id released


def test_a_failed_grafana_delete_keeps_the_id_for_the_sweep(client, monkeypatch):
    """Best effort like the Keycloak step: the request still succeeds, but the
    id stays on the tombstone so the next boot sweep finishes the job."""
    org = _org_with_grafana(client, monkeypatch, 78)
    calls = []
    _fake_grafana(monkeypatch, calls, status=requests.ConnectionError)

    assert client.delete(f"/v1/orgs/{org}").status_code == 200
    assert ("DELETE", "/orgs/78") in calls
    assert _grafana_org_id(org)[0] == 78


def _tombstones(rows):
    """Insert organization rows: (name, active, grafana_org_id)."""
    ids = []
    conn = psycopg2.connect(os.environ["DB_DSN"])
    with conn, conn.cursor() as cur:
        for name, active, gorg in rows:
            oid = str(uuid.uuid4())
            cur.execute("""INSERT INTO organization (id, name, active, archived_at, grafana_org_id)
                           VALUES (%s, %s, %s, CASE WHEN %s THEN NULL ELSE now() END, %s)""",
                        (oid, name, active, active, gorg))
            ids.append(oid)
    conn.close()
    return ids


def test_boot_sweep_clears_tombstoned_orgs_and_only_those(client, monkeypatch):
    calls = []
    _fake_grafana(monkeypatch, calls)
    dead1, dead2, live, clean = _tombstones([
        ("E2E Bot Org 1", False, 901), ("E2E Bot Org 2", False, 902),
        ("Real Customer", True, 903), ("Old tombstone", False, None)])

    assert main._sweep_orphan_grafana_orgs() is True
    deleted = {p for m, p in calls if m == "DELETE"}
    assert {"/orgs/901", "/orgs/902"} <= deleted     # (other tests' tombstones too)
    assert "/orgs/903" not in deleted                # never a live org's
    assert _grafana_org_id(dead1)[0] is None and _grafana_org_id(dead2)[0] is None
    assert _grafana_org_id(live)[0] == 903

    calls.clear()
    assert main._sweep_orphan_grafana_orgs() is True  # idempotent: nothing left
    assert not calls


def test_an_already_missing_grafana_org_counts_as_gone(client, monkeypatch):
    calls = []
    _fake_grafana(monkeypatch, calls, status=404)
    (dead,) = _tombstones([("E2E Bot Org 3", False, 904)])
    assert main._sweep_orphan_grafana_orgs() is True
    assert _grafana_org_id(dead)[0] is None


def test_sweep_keeps_the_ids_while_grafana_is_down(client, monkeypatch):
    """Grafana usually boots after the api: the sweep reports not-done so the
    startup loop retries, and no tombstone loses its id on a failed delete."""
    calls = []
    _fake_grafana(monkeypatch, calls, status=requests.ConnectionError)
    (dead,) = _tombstones([("E2E Bot Org 4", False, 905)])
    assert main._sweep_orphan_grafana_orgs() is False
    assert _grafana_org_id(dead)[0] == 905


def test_sweep_is_wired_into_the_startup_loop():
    src = inspect.getsource(main._provision_ops_org_async)
    assert "_sweep_orphan_grafana_orgs()" in src


def test_sandbox_e2e_asserts_the_grafana_org_is_gone_after_delete():
    src = open(os.path.join(ROOT, "deploy", "e2e_sandbox.py"), encoding="utf-8").read()
    delete = src.index('method="DELETE"')
    assert "/grafana/api/user/orgs" in src[delete:]
    assert "survived the organization delete" in src[delete:]
