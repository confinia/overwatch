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
