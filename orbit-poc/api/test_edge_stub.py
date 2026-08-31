"""Guards deploy/caddy/overwatch.caddy against rotting into a landmine.

It is a MIRROR of the shared edge, not a source. It once claimed port 8090,
decommissioned by the 1PESI migration on 2026-08-15 (#213), and nothing has
listened there since. Applying it would have 502'd production, staging and
sandbox together, dropped the three api.* hostnames with their certificates,
and collapsed three environments into one block. It was caught on the way in
rather than after.
"""
import os
import re

STUB = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "caddy",
                    "overwatch.caddy")
LIVE_PORTS = {"12000": "production", "12300": "staging", "12400": "sandbox"}
# Decommissioned by #213. Split so this file does not match its own guard.
DEAD_PORT = "80" + "90"


def _src():
    return open(STUB, encoding="utf-8").read()


def _config(src=None):
    """Only what Caddy would act on. The comments deliberately RECOUNT the
    8090 mistake, and a guard that trips on its own documentation is a guard
    somebody deletes."""
    return "\n".join(l for l in (src or _src()).splitlines()
                     if not l.lstrip().startswith("#"))


def test_no_decommissioned_port():
    assert DEAD_PORT not in _config(), \
        "the stub points at a port nothing listens on: applying it 502s prod"


def test_each_environment_keeps_its_own_block():
    """Sandbox riding on the production port is how isolation dies quietly."""
    src = _src()
    blocks = re.findall(r"reverse_proxy 127\.0\.0\.1:(\d+)", _config(src))
    assert len(blocks) == 3, f"expected one block per environment, found {blocks}"
    assert set(blocks) == set(LIVE_PORTS), f"ports drifted from the edge: {blocks}"


def test_every_hostname_the_edge_serves_is_present():
    src = _config()
    for host in ("overwatch.confinia.io", "api.overwatch.confinia.io",
                 "grafana.overwatch.confinia.io",
                 "staging.overwatch.confinia.io", "staging.api.overwatch.confinia.io",
                 "sandbox.overwatch.confinia.io", "sandbox.api.overwatch.confinia.io"):
        assert re.search(rf"(^|[\s,]){re.escape(host)}[\s,{{]", src), \
            f"{host} would lose its routing and its certificate"


def test_it_says_which_direction_to_copy():
    src = _src().lower()
    assert "mirror" in src and "not a source" in src, \
        "without this, the next reader applies it to the edge again"
