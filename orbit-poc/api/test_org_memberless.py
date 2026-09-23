"""Guards #485: an organization nobody can sign into any more (its Keycloak
organization gone, or every member deleted) is purged after ORG_ORPHAN_DAYS,
the way a self-delete purges it. Thirteen e2e leftovers sat active on the
sandbox for six weeks before this, seven of them still holding a Grafana org.

Keycloak is faked at `_kc_org_has_members` / `_kc_admin_token` / `_rq`; the
organization rows are real, in the CI Postgres.
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


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _kc_configured(monkeypatch):
    monkeypatch.setattr(main, "KC_ADMIN_USER", "admin")
    monkeypatch.setattr(main, "KC_ADMIN_PASS", "secret")
    monkeypatch.setattr(main, "_kc_admin_token", lambda: "tok")
    monkeypatch.setattr(main._rq, "delete", lambda *a, **k: _Resp(204))


def _org(name, days_old, gorg=None):
    oid = str(uuid.uuid4())
    conn = psycopg2.connect(os.environ["DB_DSN"])
    with conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO organization (id, name, created_at, grafana_org_id)
                       VALUES (%s, %s, now() - make_interval(days => %s), %s)""",
                    (oid, name, days_old, gorg))
        cur.execute("INSERT INTO org_user (sub, org, email, name, last_seen) "
                    "VALUES (%s, %s, 'x@example.com', 'X', now())", (str(uuid.uuid4()), oid))
    conn.close()
    return oid


def _state(oid):
    conn = psycopg2.connect(os.environ["DB_DSN"])
    with conn, conn.cursor() as cur:
        cur.execute("SELECT archived_at IS NOT NULL, grafana_org_id FROM organization WHERE id=%s",
                    (oid,))
        row = cur.fetchone()
    conn.close()
    return row


def test_old_memberless_org_is_purged_like_a_self_delete(client, monkeypatch):
    _kc_configured(monkeypatch)
    dead = _org("E2E Side 1", 30, gorg=64)
    monkeypatch.setattr(main, "_kc_org_has_members", lambda oid, tok: oid != dead)
    gf = []
    monkeypatch.setattr(main, "GF_ADMIN_PASS", "x")
    monkeypatch.setattr(main, "_gf", lambda m, p, body=None, gorg=None: gf.append((m, p)) or _Resp(200))

    assert main._sweep_memberless_orgs() is True
    assert _state(dead) == (True, None)               # tombstoned, Grafana id released
    assert ("DELETE", "/orgs/64") in gf


def test_a_fresh_memberless_org_is_left_alone(client, monkeypatch):
    """A signup in progress has no members yet for a moment, and a broken
    one deserves a look before it is wiped: the age gate is the safety."""
    _kc_configured(monkeypatch)
    fresh = _org("Just signed up", 0)
    monkeypatch.setattr(main, "_kc_org_has_members", lambda oid, tok: False)
    assert main._sweep_memberless_orgs() is True
    assert _state(fresh)[0] is False


def test_an_old_org_with_members_is_kept(client, monkeypatch):
    _kc_configured(monkeypatch)
    live = _org("Real Customer", 400, gorg=5)
    monkeypatch.setattr(main, "_kc_org_has_members", lambda oid, tok: True)
    assert main._sweep_memberless_orgs() is True
    assert _state(live) == (False, 5)


def test_keycloak_down_touches_nothing_and_reports_not_done(client, monkeypatch):
    _kc_configured(monkeypatch)
    old = _org("Unknown state", 30)
    monkeypatch.setattr(main, "_kc_org_has_members", lambda oid, tok: None)
    assert main._sweep_memberless_orgs() is False
    assert _state(old)[0] is False

    def no_token():
        raise requests.ConnectionError("kc down")
    monkeypatch.setattr(main, "_kc_admin_token", no_token)
    assert main._sweep_memberless_orgs() is False
    assert _state(old)[0] is False


def test_membership_answer_maps_keycloak_status_codes(monkeypatch):
    def get(url, headers=None, timeout=None):
        return get.resp
    monkeypatch.setattr(main._rq, "get", get)
    get.resp = _Resp(200, [{"id": "u1"}])
    assert main._kc_org_has_members("o", "t") is True
    get.resp = _Resp(200, [])
    assert main._kc_org_has_members("o", "t") is False
    get.resp = _Resp(404)
    assert main._kc_org_has_members("o", "t") is False     # organization gone
    get.resp = _Resp(503)
    assert main._kc_org_has_members("o", "t") is None      # cannot tell: keep

    def boom(url, headers=None, timeout=None):
        raise requests.ConnectionError("kc down")
    monkeypatch.setattr(main._rq, "get", boom)
    assert main._kc_org_has_members("o", "t") is None


def test_not_configured_is_done_not_stuck(monkeypatch):
    monkeypatch.setattr(main, "KC_ADMIN_USER", "")
    assert main._sweep_memberless_orgs() is True


def test_sweep_is_wired_into_the_startup_loop_and_delete_shares_the_purge():
    assert "_sweep_memberless_orgs()" in inspect.getsource(main._provision_ops_org_async)
    assert "_purge_org(org_id)" in inspect.getsource(main.delete_org)
