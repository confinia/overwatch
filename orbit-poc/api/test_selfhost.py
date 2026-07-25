"""Self-host install test (#53): the one-command compose is well-formed and
bundles a complete open-data instance. A cheap structural guard so the
`docker compose up` promise can't silently regress; the real up-smoke runs
on the VM (see deploy notes)."""
import os
import re

HERE = os.path.dirname(__file__)
COMPOSE = os.path.join(HERE, "..", "docker-compose.selfhost.yml")
CADDY = os.path.join(HERE, "..", "deploy", "caddy", "Caddyfile.selfhost")


def _text(p):
    return open(p, encoding="utf-8").read()


def test_selfhost_compose_has_full_stack():
    t = _text(COMPOSE)
    # every service needed for a working open-data instance
    for svc in ("db:", "ingest:", "web:", "api:", "grafana:", "caddy:"):
        assert re.search(rf"^  {svc}", t, re.M), f"missing service {svc}"


def test_selfhost_is_zero_config():
    t = _text(COMPOSE)
    assert 'SATNOGS_TOKEN: ""' in t          # optional, empty by default
    assert 'REQUIRE_API_KEY: "false"' in t   # keyless open data
    assert "ORG_DB_SECRET" not in t or "unset" in t  # tenants OFF (no secret)


def test_selfhost_exposes_one_port():
    assert '"8080:80"' in _text(COMPOSE)


def test_selfhost_caddy_routes_all_surfaces():
    c = _text(CADDY)
    for route in ("/api/v1*", "/api/*", "/grafana*"):
        assert route in c, f"caddy missing route {route}"
