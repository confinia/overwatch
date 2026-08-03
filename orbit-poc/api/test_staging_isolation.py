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


def test_staging_has_its_own_database():
    assert "st_pgdata" in COMPOSE                    # its own volume
    assert "postgres:16" in COMPOSE


def test_staging_owns_its_port_band():
    ports = [int(p) for p in re.findall(r'127\.0\.0\.1:(\d+):\d+', COMPOSE)]
    assert ports, "no published port"
    for p in ports:
        assert 8200 <= p <= 8209, f"{p} outside the staging band 8200-8209"
    assert 8190 not in ports and 8090 not in ports   # not the sandbox nor prod


def test_staging_uses_its_own_keycloak_realm():
    """A test signup must never land in the production realm — that is the
    incident this issue came from."""
    assert "realms/overwatch-staging" in COMPOSE
    assert not re.search(r'realms/overwatch"', COMPOSE)


def test_billing_is_off_on_staging():
    """Payments belong to the sandbox, the environment for actions with no
    accounting impact."""
    assert 'POLAR_ENV: "off"' in COMPOSE


def test_production_caddy_no_longer_serves_staging():
    assert "http://staging.overwatch.confinia.io" not in TMPL
    assert "%CANDIDATE%_web_1" not in TMPL           # nothing routes to it now


def test_pipeline_deploys_the_staging_stack():
    """The exact-binary property holds only if the pipeline builds the staging
    stack from the same commit as the candidate colour."""
    wf = open(os.path.join(HERE, "..", "..", ".github", "workflows", "deploy.yml"),
              encoding="utf-8").read()
    assert "ovw-staging" in wf
    assert wf.index("slots.sh stage") < wf.index("ovw-staging")
