"""Guards issue #126: sandbox sign-in uses the ONE shared Keycloak via the
dedicated realm `overwatch-sandbox` — issuer-URL distinction, no second
instance, and no admin call ever hardcodes the prod realm.
"""
import os
import re

HERE = os.path.dirname(__file__)
SANDBOX = os.path.join(HERE, "..", "sandbox")
COMPOSE = os.path.join(SANDBOX, "docker-compose.yml")
CADDYFILE = os.path.join(SANDBOX, "Caddyfile")
MAIN = os.path.join(HERE, "main.py")
TMPL_PATH = os.path.join(HERE, "..", "deploy", "caddy", "Caddyfile.tmpl")


def test_sandbox_issuer_is_dedicated_realm():
    compose = open(COMPOSE).read()
    assert ("KC_ISSUER: \"https://sandbox.overwatch.confinia.io/auth/realms/"
            "overwatch-sandbox\"") in compose
    assert "realms/overwatch-sandbox" in compose      # internal URL too
    # never the prod realm as issuer
    assert not re.search(r'KC_ISSUER:.*?/realms/overwatch"', compose)


def test_sandbox_joins_shared_keycloak_network():
    compose = open(COMPOSE).read()
    assert "name: ovw2_default" in compose            # ONE Keycloak, shared net
    assert compose.count("v2net") >= 3                # api + caddy + definition


def test_sandbox_caddy_proxies_auth():
    caddy = open(CADDYFILE).read()
    assert "handle /auth*" in caddy
    assert "ovw2_keycloak_1:8080" in caddy


def test_no_hardcoded_prod_realm_in_admin_calls():
    src = open(MAIN).read()
    assert "admin/realms/overwatch\"" not in src      # must derive from KC_REALM
    assert "admin/realms/{KC_REALM}" in src


def test_no_hardcoded_callback_host():
    # the OAuth redirect_uri must derive from PUBLIC_BASE so each env logs in
    # on its own host (prod hardcoding broke sandbox login with a 400)
    src = open(MAIN).read()
    assert "overwatch.confinia.io/api/v1/auth/callback" not in src
    assert src.count("{PUBLIC_BASE}/api/v1/auth/callback") == 2


def test_env_example_documents_kc_secrets():
    env = open(os.path.join(SANDBOX, ".env.example")).read()
    for key in ("OVERWATCH_CLIENT_SECRET", "KC_ADMIN_USERNAME", "KC_ADMIN_PASSWORD"):
        assert key in env


def _grafana_block(text, after=""):
    """The `handle /grafana*` block of a given vhost (the file holds several)."""
    start = text.index(after) if after else 0
    i = text.index("handle /grafana*", start)
    return text[i:text.index("\n\t}", i)]


def test_gated_vhosts_strip_the_credential_before_grafana():
    """#149: Grafana supports basic auth, so the gate's replayed Authorization
    header is read as a failed login and the OIDC session cookie is ignored.
    Only the GATED vhosts strip it — production has no gate and must not."""
    sandbox = open(CADDYFILE, encoding="utf-8").read()
    assert "header_up -Authorization" in _grafana_block(sandbox)

    tmpl = open(TMPL_PATH, encoding="utf-8").read()
    staging = _grafana_block(tmpl, after="http://staging.overwatch.confinia.io")
    assert "header_up -Authorization" in staging, "staging does not strip it"

    prod = _grafana_block(tmpl, after="http://overwatch.confinia.io")
    assert "header_up -Authorization" not in prod, "production has no gate to strip"
