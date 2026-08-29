"""Guards issue #131: the public API is served on its own hostnames
(api.overwatch.confinia.io, sandbox.api.overwatch.confinia.io) at the root
(/v1/...), while /api/v1/... keeps working there (FastAPI runs with
root_path=/api, so its docs resolve under that prefix).
"""
import os
import re

HERE = os.path.dirname(__file__)
TMPL = os.path.join(HERE, "..", "deploy", "caddy", "Caddyfile.tmpl")
SANDBOX_CADDY = os.path.join(HERE, "..", "sandbox", "Caddyfile")


def _block(text, host):
    """The vhost block for `host`, up to the next top-level closing brace."""
    m = re.search(rf'^http://{re.escape(host)}[^\n]*\{{(.*?)^\}}', text,
                  re.S | re.M)
    return m.group(1) if m else ""


def test_prod_api_host_declared():
    b = _block(open(TMPL).read(), "api.overwatch.confinia.io")
    assert b, "api.overwatch.confinia.io vhost missing from the app caddy"
    assert "handle /v1*" in b and "_api_1:8000" in b     # served at the root
    assert "uri strip_prefix /api" in b                  # docs/openapi keep working
    assert 'X-Overwatch-Env "production"' in b


def test_prod_api_host_follows_the_live_color():
    """#135: it was copied from the staging block and served the CANDIDATE
    color, so right after a promote the API host ran the OLD image."""
    b = _block(open(TMPL).read(), "api.overwatch.confinia.io")
    assert "%LIVE%_api_1:8000" in b
    for line in b.splitlines():
        if "reverse_proxy" in line:
            assert "%LIVE%_api_1" in line, f"candidate-only upstream: {line.strip()}"


def test_sandbox_api_host_declared():
    b = _block(open(SANDBOX_CADDY).read(), "sandbox.api.overwatch.confinia.io")
    assert b, "sandbox.api.overwatch.confinia.io vhost missing"
    assert "handle /v1*" in b and "reverse_proxy api:8000" in b
    assert "uri strip_prefix /api" in b
    assert "import sandbox_common" in b                  # gate + env header


def test_sandbox_api_host_is_gated_but_webhook_stays_open():
    caddy = open(SANDBOX_CADDY).read()
    assert "basic_auth @protected" in caddy
    # the exemption must cover both host spellings of the webhook path
    assert "/api/v1/billing/webhook*" in caddy
    assert "/v1/billing/webhook*" in caddy


def test_site_hosts_keep_the_api_prefix():
    # no breaking change for existing consumers of the site host
    tmpl = open(TMPL).read()
    assert "handle /api/v1*" in _block(tmpl, "overwatch.confinia.io")
    assert "handle /api/v1*" in _block(open(SANDBOX_CADDY).read(),
                                       "sandbox.overwatch.confinia.io")


# ---------------------------------------------------------------------------
# grafana.overwatch.confinia.io — the memorable hostname (#386)
# ---------------------------------------------------------------------------
EDGE_STUB = os.path.join(HERE, "..", "..", "deploy", "caddy", "overwatch.caddy")


def test_grafana_hostname_redirects_to_the_canonical_path():
    """A 308 to overwatch.confinia.io/grafana — never a second serving
    origin: root_url, the same-origin iframes, frame-ancestors 'self' and
    the Keycloak redirect URIs all assume the canonical one."""
    tmpl = open(TMPL, encoding="utf-8").read()
    block = tmpl[tmpl.index("http://grafana.overwatch.confinia.io"):]
    block = block[:block.index("\n}")]
    assert "redir * https://overwatch.confinia.io/grafana{uri} 308" in block
    assert "reverse_proxy" not in block, \
        "the hostname must redirect, not serve — one Grafana origin only"


def test_grafana_hostname_is_in_the_edge_stub():
    # the stub is what the founder applies on the platform edge (rule 19) —
    # a hostname missing here never terminates TLS, and the redirect above
    # is unreachable dead config
    stub = open(EDGE_STUB, encoding="utf-8").read()
    assert "grafana.overwatch.confinia.io" in stub
