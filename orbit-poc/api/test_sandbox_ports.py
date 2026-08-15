"""Guards issue #111: the sandbox stack must live in its OWN dedicated TCP
port range (8190-8199) and must NOT reuse prod's ports (808x/908x/8090) or any
arbitrary port that could collide with another product on the VM (the original
8087 clashed with mapmax_web_1).

Parses orbit-poc/sandbox/docker-compose.yml + Caddyfile textually (no yaml dep).
"""
import os
import re

HERE = os.path.dirname(__file__)
SANDBOX = os.path.join(HERE, "..", "sandbox")
COMPOSE = os.path.join(SANDBOX, "docker-compose.yml")
CADDYFILE = os.path.join(SANDBOX, "Caddyfile")

RESERVED = range(8190, 8200)              # legacy sandbox band, 8190-8199
# 1PESI (#213): the sandbox keeps its own band under the new scheme too —
# overwatch(2)·sandbox(4)·*  ->  124xx. Dual-published alongside legacy.
SANDBOX_1PESI = range(12400, 12500)
PROD_PORTS = {8081, 8082, 8090, 9081, 9082,   # legacy prod blue/green + caddy
              12000, 12040, 12070, 12110, 12120, 12210, 12220}  # + 1PESI prod


def _published_host_ports(text):
    # matches `- "127.0.0.1:8190:80"` and `- "127.0.0.1:8191:8000"`
    return [int(p) for p in re.findall(r'127\.0\.0\.1:(\d+):\d+', text)]


def test_all_sandbox_ports_in_reserved_range():
    ports = _published_host_ports(open(COMPOSE).read())
    assert ports, "no 127.0.0.1-published ports found in sandbox compose"
    for p in ports:
        assert p in RESERVED or p in SANDBOX_1PESI, \
            f"sandbox port {p} outside sandbox bands (8190-8199 / 124xx)"


def test_sandbox_does_not_reuse_prod_ports():
    ports = set(_published_host_ports(open(COMPOSE).read()))
    assert ports.isdisjoint(PROD_PORTS), (
        f"sandbox reuses prod port(s): {ports & PROD_PORTS}")


def test_no_8087_collision():
    # 8087 is taken by mapmax_web_1 on the VM — must never be used by the sandbox
    assert 8087 not in _published_host_ports(open(COMPOSE).read())


def test_sandbox_has_own_caddy_on_8190():
    compose = open(COMPOSE).read()
    assert re.search(r'127\.0\.0\.1:8190:80', compose), \
        "sandbox caddy must publish 8190"
    # the caddy service must exist and mount the sandbox Caddyfile
    assert "./Caddyfile:/etc/caddy/Caddyfile" in compose
    assert os.path.exists(CADDYFILE), "sandbox/Caddyfile missing"


def test_api_debug_port_is_8191():
    assert re.search(r'127\.0\.0\.1:8191:8000', open(COMPOSE).read()), \
        "sandbox api debug port must be 8191 (not 8087)"


def _service_block(compose, name):
    """The text of one compose service, from `  <name>:` to the next service."""
    m = re.search(rf'^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)',
                  compose, re.M | re.S)
    return m.group(0) if m else ""


def test_grafana_reaches_keycloak_for_oauth_code_exchange():   # #151
    """Grafana runs the OAuth code exchange server-side against the internal
    Keycloak (GF_AUTH_GENERIC_OAUTH_TOKEN_URL -> ovw2_keycloak_1). If the
    grafana service is not on the shared-Keycloak network (v2net), that DNS
    name doesn't resolve and every private-Grafana login 401s. Guards sandbox
    AND staging, which both configure OAuth."""
    for path in (COMPOSE, os.path.join(HERE, "..", "staging", "docker-compose.yml")):
        gf = _service_block(open(path).read(), "grafana")
        if "GF_AUTH_GENERIC_OAUTH_TOKEN_URL" not in gf:
            continue                                  # no OAuth here, nothing to wire
        assert "ovw2_keycloak_1" in gf                # exchange targets internal KC
        assert re.search(r'networks:\s*(?:#.*)?\n(?:\s*-\s*\w+\s*(?:#.*)?\n)*'
                         r'\s*-\s*v2net', gf), \
            f"{os.path.basename(os.path.dirname(path))} grafana must be on v2net"


def test_grafana_has_db_password_matching_db():
    # #117: the datasource provisioning resolves password: $__env{DB_PASSWORD};
    # the sandbox grafana must set it to the sandbox db password, or every panel
    # 400s ("No data" + norad templating error).
    compose = open(COMPOSE).read()
    db_pw = re.search(r'POSTGRES_PASSWORD:\s*"?(\w+)"?', compose)
    gf_pw = re.search(r'DB_PASSWORD:\s*"?(\w+)"?', compose)
    assert db_pw and gf_pw, "sandbox compose must set POSTGRES_PASSWORD and DB_PASSWORD"
    assert gf_pw.group(1) == db_pw.group(1), (
        f"grafana DB_PASSWORD ({gf_pw.group(1)}) must equal db "
        f"POSTGRES_PASSWORD ({db_pw.group(1)})")
