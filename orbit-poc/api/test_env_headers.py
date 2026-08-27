"""Guards issue #113: each environment self-identifies via a distinct
X-Overwatch-Env response header, so sandbox / staging / production are never
confused. production + staging live in the app caddy template; sandbox in its
own Caddyfile.
"""
import os

HERE = os.path.dirname(__file__)
TMPL = os.path.join(HERE, "..", "deploy", "caddy", "Caddyfile.tmpl")
SANDBOX_CADDY = os.path.join(HERE, "..", "sandbox", "Caddyfile")


STAGING_CADDY = os.path.join(HERE, "..", "staging", "Caddyfile")


def test_production_and_staging_self_identify():
    # staging moved to its own stack and its own Caddyfile (#154)
    assert 'X-Overwatch-Env "production"' in open(TMPL).read()
    assert 'X-Overwatch-Env "staging"' in open(STAGING_CADDY).read()


def test_sandbox_self_identifies():
    assert 'X-Overwatch-Env "sandbox"' in open(SANDBOX_CADDY).read()


def test_three_distinct_envs():
    values = set()
    for f in (TMPL, SANDBOX_CADDY, STAGING_CADDY):
        for line in open(f):
            line = line.strip()
            if line.startswith('X-Overwatch-Env "'):
                values.add(line.split('"')[1])
    assert {"production", "staging", "sandbox"} <= values, values


def test_the_live_colour_is_advertised_and_never_hardcoded():   # #323
    """The platform dashboard reads X-Active-Colour to show which colour
    serves production. A header naming a colour that is not serving is worse
    than no header — it makes the panel a confident liar — so it must be the
    %LIVE% placeholder that slots.sh rewrites on every promote, never a
    literal. There is then no path that repoints the proxy without also
    updating the header."""
    tmpl = open(TMPL).read()
    assert tmpl.count('X-Active-Colour "%LIVE%"') == 2, \
        "both production hostnames must advertise the promoted colour"
    for literal in ("blue", "green"):
        assert f'X-Active-Colour "{literal}"' not in tmpl, \
            f"a hardcoded {literal} drifts the moment the other is promoted"


def test_single_instance_environments_claim_no_colour():   # #323
    """Staging and sandbox are single-instance. Emitting no header is the
    correct answer and renders as neutral; inventing one would be a false
    signal about a deployment model we do not have."""
    for f in (STAGING_CADDY, SANDBOX_CADDY):
        assert "X-Active-Colour" not in open(f).read(), \
            f"{f} is not blue/green and must not claim a colour"


def test_the_proxy_adds_no_cors_header_of_its_own():   # #365
    """The api's CORSMiddleware already answers Access-Control-Allow-Origin.
    A second copy added at the proxy is not merged by browsers — it makes the
    whole response FAIL the CORS check, which broke the mobile app in exactly
    the cross-origin case it exists for. One owner for that header: the api."""
    body = open(TMPL, encoding="utf-8").read()
    code = "\n".join(l.split("#", 1)[0] for l in body.splitlines())
    # Only the handlers that proxy to the API: the /api/* -> web handler sets
    # a CORS header legitimately, because the static web upstream sends none.
    import re as _re
    api_blocks = _re.findall(r"handle /api/v1\*.*?\n\t\}", code, _re.S)
    assert len(api_blocks) >= 2, "expected the api handler on both hostnames"
    for block in api_blocks:
        assert "Access-Control-Allow-Origin" not in block, \
            "the api handler sets CORS at the proxy — the api already does, " \
            "and duplicate ACAO makes browsers reject the response"
