"""Signup / subscription flow tests.

Covers the multi-tenant path a NEW USER walks: anonymous access stays open;
signing in with an organization materializes the tenant; organizations are
isolated from each other; org service tokens push under the org; quotas hold.

Keycloak identity is monkeypatched at the single boundary (`_claims`) so the
suite runs in CI against a real Postgres without a live Keycloak — the org
LOGIC is what we test, not Keycloak itself (that is the E2E smoke test).
"""
import os
import uuid
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DB_DSN", "dbname=orbit user=orbit password=orbit host=localhost port=5432")
import main  # noqa: E402


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """CI runs many calls from one host; lift the per-IP rate limit and reset
    its window so tests observe real status codes, not 429s."""
    monkeypatch.setattr(main, "RATE_PER_SEC", 10**9)
    monkeypatch.setattr(main, "RATE_PER_MIN", 10**9)
    main._rate.clear()


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


def _as_user(monkeypatch, sub, org_id, org_name, email="u@example.com"):
    """Simulate a signed-in Keycloak user carrying an organization claim."""
    claims = {"sub": sub, "email": email, "name": "Test User",
              "organization": {org_name: {"id": org_id}}}
    monkeypatch.setattr(main, "_claims", lambda request: claims)


def _anon(monkeypatch):
    monkeypatch.setattr(main, "_claims", lambda request: None)


# --- anonymous: open data stays free and keyless -------------------------

def test_anonymous_open_data_ok(client):
    assert client.get("/v1/satellites").status_code == 200
    assert client.get("/v1/stations").status_code == 200


def test_anonymous_me_is_401(client, monkeypatch):
    _anon(monkeypatch)
    assert client.get("/v1/me").status_code == 401


def test_anonymous_cannot_read_org(client, monkeypatch):
    _anon(monkeypatch)
    assert client.get("/v1/org/satellites").status_code == 401


# --- signed in: identity + organization materialize ----------------------

def test_me_returns_identity_and_org(client, monkeypatch):
    org = str(uuid.uuid4())
    _as_user(monkeypatch, str(uuid.uuid4()), org, "Acme Space")
    r = client.get("/v1/me")
    assert r.status_code == 200
    body = r.json()
    assert body["organization"]["id"] == org
    assert body["organization"]["name"] == "Acme Space"


# --- isolation: one org never sees another's data ------------------------

def test_org_data_isolation(client, monkeypatch):
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())

    _as_user(monkeypatch, str(uuid.uuid4()), org_a, "Org A")
    tok_a = client.post("/v1/org/tokens", json={"label": "a"}).json()["token"]
    client.post(f"/v1/tenants/{tok_a}/telemetry",
                json={"satellite": "A-SAT", "points": [
                    {"ts": "2026-07-22T10:00:00Z", "field": "battery_v", "value": 7.4}]})

    _as_user(monkeypatch, str(uuid.uuid4()), org_b, "Org B")
    sats_b = client.get("/v1/org/satellites").json()
    assert all(s["satellite"] != "A-SAT" for s in sats_b)

    _as_user(monkeypatch, str(uuid.uuid4()), org_a, "Org A")
    sats_a = client.get("/v1/org/satellites").json()
    assert any(s["satellite"] == "A-SAT" for s in sats_a)


# --- org service token: machine push under the org -----------------------

def test_service_token_push_and_read(client, monkeypatch):
    org = str(uuid.uuid4())
    _as_user(monkeypatch, str(uuid.uuid4()), org, "Push Co")
    tok = client.post("/v1/org/tokens", json={"label": "ground"}).json()["token"]

    r = client.post(f"/v1/tenants/{tok}/telemetry",
                    json={"satellite": "PUSH-1", "points": [
                        {"ts": "2026-07-22T11:00:00Z", "field": "temp", "value": 20.5}]})
    assert r.status_code == 202 and r.json()["accepted"] == 1

    series = client.get("/v1/org/telemetry", params={
        "satellite": "PUSH-1", "field": "temp", "hours": 24}).json()
    assert series and series[0]["value"] == 20.5


def test_unknown_token_rejected(client):
    r = client.post(f"/v1/tenants/{uuid.uuid4()}/telemetry",
                    json={"satellite": "X", "points": []})
    assert r.status_code == 404


def test_batch_over_limit_rejected(client):
    # tenant existence is checked after the size guard; a big batch is 413
    pts = [{"ts": "2026-07-22T10:00:00Z", "field": "f", "value": 1}] * 1001
    r = client.post(f"/v1/tenants/{uuid.uuid4()}/telemetry",
                    json={"satellite": "X", "points": pts})
    assert r.status_code == 413
