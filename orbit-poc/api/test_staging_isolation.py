"""Guards issue #154: staging must run its own database, and must not be served
by the production app caddy any more.

Sharing one database meant a test signup wrote into real accounts (it happened),
and a schema mistake could have taken production down.
"""
import os
import re

HERE = os.path.dirname(__file__)
STAGING = os.path.join(HERE, "..", "staging")
COMPOSE = open(os.path.join(STAGING, "docker-compose.yml"), encoding="utf-8").read()
TMPL = open(os.path.join(HERE, "..", "deploy", "caddy", "Caddyfile.tmpl"),
            encoding="utf-8").read()
STAGING_CADDY = open(os.path.join(STAGING, "Caddyfile"), encoding="utf-8").read()


def test_staging_has_its_own_database():
    assert "st_pgdata" in COMPOSE                    # its own volume
    assert "postgres:16" in COMPOSE


def test_staging_owns_its_port_band():
    """1PESI (#213): 12 · environment · service. Staging is 123xx.

    This asserted the pre-#213 band 8200-8209 until 2026-08-25. The scheme
    changed deliberately and the assertion was never updated, because this
    suite raised a collection error in the gate and therefore never ran
    (#286) — the drift the auto-discovery switch exists to prevent."""
    ports = [int(p) for p in re.findall(r'127\.0\.0\.1:(\d+):\d+', COMPOSE)]
    assert ports, "no published port"
    for p in ports:
        assert 12300 <= p <= 12399, f"{p} outside the staging band 123xx"
    # and never another environment's band: blue 121xx, green 122xx,
    # sandbox 124xx
    for base in (12100, 12200, 12400):
        assert not [p for p in ports if base <= p <= base + 99], \
            f"staging publishes into the {base} band"


def test_staging_uses_its_own_keycloak_realm():
    """A test signup must never land in the production realm — that is the
    incident this issue came from."""
    assert "realms/overwatch-staging" in COMPOSE
    assert not re.search(r'realms/overwatch"', COMPOSE)


def test_billing_is_off_on_staging():
    """Payments belong to the sandbox, the environment for actions with no
    accounting impact."""
    assert 'POLAR_ENV: "off"' in COMPOSE


def test_staging_caddy_trusts_edge_proxy():
    """The staging app caddy listens on http (TLS ends at the platform edge),
    so it must trust that edge to pass X-Forwarded-Proto (https) + the real
    client IP through. Without it the app builds an http:// OIDC redirect_uri
    that the overwatch-staging realm rejects (#179). Prod already does this."""
    assert "trusted_proxies" in STAGING_CADDY
    assert "trusted_proxies" in TMPL          # the prod reference it mirrors


def test_production_caddy_no_longer_serves_staging():
    """#154: staging runs its own stack, so no staging hostname is served by
    the production caddy.

    It used to also assert %CANDIDATE%_web_1 was absent — true when staging WAS
    the candidate colour, but blue/green failover (lb_policy first + health
    checks) has since made the candidate a legitimate second upstream for
    PRODUCTION traffic. The hostname is the thing that matters."""
    # code only: the template CARRIES a comment explaining that staging is not
    # served here, and a guard that trips on its own documentation is a guard
    # nobody keeps.
    code = "\n".join(l.split("#", 1)[0] for l in TMPL.splitlines())
    assert "staging.overwatch.confinia.io" not in code


def test_pipeline_deploys_the_staging_stack():
    """The exact-binary property holds only if the pipeline builds the staging
    stack from the same commit as the candidate colour."""
    wf = open(os.path.join(HERE, "..", "..", ".github", "workflows", "deploy.yml"),
              encoding="utf-8").read()
    assert "ovw-staging" in wf
    assert wf.index("slots.sh stage") < wf.index("ovw-staging")
