"""Guards #523: a non-production container must not be able to resolve a
production service name.

Staging and sandbox joined `orbit-poc_default` wholesale so their ingest could
reach `satnogs-gateway`. podman answers a name from ALL of a container's
networks, so `db` returned BOTH their own database and production's — about
half of the staging ingest's connections authenticated against the production
database and failed, 1618 times on 2026-09-27. Only a password mismatch kept
them apart, and #154 gave staging its own database precisely because sharing
one meant "a test signup wrote into real accounts".

The rule: a network shared across environments carries exactly ONE service.
A network with a single member cannot be ambiguous.
"""
import os
import re

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..", "..")
NONPROD = ("sandbox", "staging")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _networks_block(body):
    """The top-level `networks:` mapping of a compose file."""
    m = re.search(r"\nnetworks:\n((?:  \S.*\n|    .*\n|\n)*)", body)
    assert m, "no top-level networks block"
    return m.group(1)


def test_no_non_production_stack_joins_the_production_network():
    """The whole point. orbit-poc_default carries db, api, grafana, caddy,
    ingest and the gateway; joining it imports all of them."""
    for env in NONPROD:
        body = _read("orbit-poc", env, "docker-compose.yml")
        code = "\n".join(l.split("#", 1)[0] for l in body.splitlines())
        assert "orbit-poc_default" not in code, (
            f"{env} joins the production network: every production service "
            f"name resolves from it")


def test_the_shared_gateway_network_has_exactly_one_member():
    """satnogsnet exists so staging and sandbox can reach satnogs-gateway and
    nothing else. If a second production service ever joins it, the ambiguity
    is back."""
    prod = _read("orbit-poc", "docker-compose.yml")
    members = []
    for svc in re.finditer(r"\n  ([a-z][a-z0-9_-]*):\n((?:    .*\n|\n)*)", prod):
        name, block = svc.group(1), svc.group(2)
        code = "\n".join(l.split("#", 1)[0] for l in block.splitlines())
        if "satnogsnet" in code:
            members.append(name)
    assert members == ["satnogs-gateway"], \
        f"satnogsnet must carry only the gateway, found: {members}"


def test_non_production_reaches_the_gateway_through_that_network():
    for env in NONPROD:
        body = _read("orbit-poc", env, "docker-compose.yml")
        assert "satnogsnet" in body, f"{env} cannot reach the SatNOGS gateway"
        assert "orbit-poc_satnogsnet" in body, \
            f"{env} must reference the gateway network by its external name"
        # and it must still route SatNOGS through the gateway, not direct.
        # Addressed by CONTAINER name rather than service alias, which is the
        # unambiguous form and resolves on the shared network either way.
        assert "orbit-poc_satnogs-gateway_1:8088" in body, \
            f"{env} must still use the rate-limited door (#449)"
        # directives only: comments discuss db.satnogs.org to explain the
        # blackhole, which is the opposite of reaching it
        code = "\n".join(l.split("#", 1)[0] for l in body.splitlines())
        assert '"db.satnogs.org:127.0.0.1"' in code, \
            f"{env} must keep the sole-egress blackhole (#449)"


def test_the_reason_travels_with_the_change():
    """A future reader will wonder why these stacks do not simply join
    prodnet like they used to. The answer has to be next to the network."""
    for env in NONPROD:
        body = _read("orbit-poc", env, "docker-compose.yml")
        block = _networks_block(body)
        assert "#523" in block, f"{env}: the networks block must cite why"


# --- #540: the name `api` answers for BOTH colours ------------------------

# Every core-stack file that names a peer. A file missing from disk must FAIL
# the test, not silently shrink the scan — that is how three guards in this
# repo were vacuous on their first attempt.
CORE_CALLERS = (
    ("orbit-poc", "docker-compose.yml"),
    ("orbit-poc", "statusmon", "statusmon.py"),
    ("orbit-poc", "deploy", "otel-collector.yaml"),
    ("orbit-poc", "deploy", "prometheus.yml"),
    ("orbit-poc", "deploy", "caddy", "Caddyfile.tmpl"),
    ("orbit-poc", "grafana", "provisioning", "datasources", "postgres.yml"),
    ("orbit-poc", "grafana", "provisioning", "datasources", "prometheus.yml"),
    ("orbit-poc", "bridge", "yamcs", "demo", "docker-compose.yml"),
    ("orbit-poc", "bridge", "yamcs", "demo", "docker-compose.internal.yml"),
)
# `://api`, `api:8000` and the like, as a whole word: `/api/v1/...` paths and
# `satnogs-api` style names are not the bare service name.
BARE_API = re.compile(r"(://api(?=[:/\s\"']|$))|((?<![\w.\-/])api:\d{2,5}\b)")


def _directives(body):
    """Comment-stripped lines: a comment may *discuss* `api:8000`."""
    return "\n".join(l.split("#", 1)[0] for l in body.splitlines())


def test_no_core_caller_addresses_the_api_by_its_bare_name():
    """blue_api_1 and green_api_1 both sit on orbit-poc_default with the
    compose alias `api`, and podman answers a name from every holder:

        $ getent hosts api
        10.89.1.26  api.dns.podman     <- blue, live
        10.89.1.61  api.dns.podman     <- green, candidate

    So a core-stack caller written as http://api:8000 is round-robined
    between production and whatever is being staged — the right database
    behind the wrong colour's code, which is the quietest kind of wrong. The
    alias cannot be suppressed from compose (#523: `aliases:` ADD to it), so
    the rule is on the callers: address the colour-independent door (caddy,
    as statusmon and the demo bridge do since #524) or a colour by its
    container name (as the caddy template does). Blast radius was zero when
    this was written; this keeps it there."""
    for parts in CORE_CALLERS:
        path = os.path.join(ROOT, *parts)
        assert os.path.exists(path), f"scan set names a file that is gone: {path}"
        hits = [l.strip() for l in _directives(_read(*parts)).splitlines()
                if BARE_API.search(l)]
        assert not hits, (
            f"{'/'.join(parts)} addresses the api by its bare name, which "
            f"resolves to BOTH colours (#540): {hits}")


def test_the_pattern_catches_the_bare_name_and_spares_the_paths():
    """Pin the regex itself, so a later 'simplification' cannot quietly stop
    matching the thing it exists for."""
    for bad in ("http://api:8000/healthz", "reverse_proxy api:8000",
                'url: "http://api:8000"', "- http://api/v1/x",
                "OVERWATCH_URL=http://api:8000"):
        assert BARE_API.search(bad), bad
    for ok in ("http://caddy:80/api/v1/healthz", "https://overwatch.confinia.io/api",
               "reverse_proxy %LIVE%_api_1:8000", "blue_api_1:8000",
               "satnogs-api:8000", "/api/v1/satellites", "  api:",
               "ovw.api.requests"):
        assert not BARE_API.search(ok), ok
