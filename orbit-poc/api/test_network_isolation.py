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
