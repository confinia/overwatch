"""What comes back after a reboot, and what a shared network still leaks.

Both found by the platform session's VM-wide audits. `restart: always` and
`unless-stopped` do NOT survive a reboot on this host: podman-restart.service
is disabled for our user and only acts on `always` anyway. A systemd unit is
the only thing that brings a stack back.
"""
import os
import re

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..", "..")
UNITS = os.path.join(ROOT, "orbit-poc", "deploy", "systemd")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _unit(name):
    return open(os.path.join(UNITS, name), encoding="utf-8").read()


def test_every_always_on_stack_has_a_unit():
    """The demo had none: a reboot would take the public demo off the site
    while production came back, and nothing would look wrong."""
    have = {f[len("overwatch-"):-len(".service")]
            for f in os.listdir(UNITS) if f.endswith(".service")}
    assert {"core", "blue", "green", "ovw2", "demo"} <= have, \
        f"missing a unit for: {{'core','blue','green','ovw2','demo'}} - {have}"


def test_units_keep_the_shape_that_works():
    """RemainAfterExit is load-bearing: without it the unit goes inactive
    after `up -d` and systemd kills podman's helpers in its cgroup."""
    for f in os.listdir(UNITS):
        if not f.endswith(".service"):
            continue
        u = _unit(f)
        assert "Type=oneshot" in u, f
        assert "RemainAfterExit=yes" in u, f
        assert "WantedBy=default.target" in u, f
        assert re.search(r"^ExecStart=.*podman-compose.*up -d", u, re.M), f


def test_the_demo_unit_starts_after_the_core_it_pushes_into():
    u = _unit("overwatch-demo.service")
    assert "After=" in u and "overwatch-core.service" in u
    assert "Wants=" in u, "Wants, not Requires: a slow core must not kill it"
    assert "Requires=" not in u
    assert "-p ow-demo" in u, "must match the project the deploy uses"
    assert "docker-compose.internal.yml" in u, \
        "the overlay is what joins it to the stack network"


# --- the ambiguity #523 introduced in the other direction -----------------

def test_nothing_addresses_the_ingest_by_its_network_name():
    """#523 put the non-production ingests on a network with the gateway.
    From the INGESTS' side it is single-member, which is what mattered — they
    no longer see production's database. From the GATEWAY's side it is not:
    podman-compose registers each joining container's service name as an
    alias, so `ingest` now resolves to all three environments from production.

    An explicit `aliases:` entry does NOT suppress the default — verified on
    the VM, podman registers both `ingest` and the alias — so this cannot be
    removed in compose. It stays harmless only while nothing calls the ingest
    by name, which is what this pins. The ingest has no HTTP port at all: it
    is a writer, not a service, and should never be addressed."""
    hits = []
    skip = os.path.basename(__file__)
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "orbit-poc")):
        if "vendor" in dirpath or "__pycache__" in dirpath:
            continue
        for fn in files:
            if fn == skip or not fn.endswith((".py", ".yml", ".yaml")):
                continue
            full = os.path.join(dirpath, fn)
            body = open(full, encoding="utf-8", errors="replace").read()
            code = "\n".join(l.split("#", 1)[0] for l in body.splitlines())
            if re.search(r"://ingest[:/]|\bhost=ingest\b|\bingest:\d{2,}", code):
                hits.append(os.path.relpath(full, ROOT))
    assert not hits, f"these address the ingest by network name: {hits}"


def test_the_restart_policy_survives_a_late_systemd_scope():
    """The policy above is applied by `podman update --restart=always`, which
    talks to the container's transient systemd scope over sd-bus. That scope is
    registered asynchronously, so under host load the call lands before it
    exists and fails the whole stage with exit 125 (#535) — it blocked two
    promotions. Setting the policy must tolerate a late scope, and must still
    fail if it never arrives: a container without it does not come back."""
    slots = _read("deploy", "slots.sh")
    body = slots.split("set_restart_policy() {", 1)
    assert len(body) == 2, "set_restart_policy() is gone — who sets the policy now?"
    fn = body[1].split("\n}", 1)[0]

    assert "scope not found" in fn, \
        "nothing waits on the late scope — the stage dies on a loaded host"
    assert re.search(r"sleep\s+1", fn), "no retry delay: the wait would spin"
    # Giving up must be loud. A `return 0` on exhaustion would leave a
    # container that silently stays down after the next reboot. Look at the
    # exhaustion path only — the in-loop "anything else is real" branch also
    # returns 1, and splitting on the wait made this assertion vacuous once.
    done = fn.split("never appeared", 1)
    assert len(done) == 2, "nothing reports giving up on the scope"
    assert "return 0" not in done[1] and "return 1" in done[1], \
        "exhausting the wait must fail the stage, not pass it quietly"

    # And nobody may go back to the bare call that caused this.
    for line in slots.splitlines():
        if "podman update --restart" in line and "set_restart_policy" not in line:
            assert "err=$(" in line, \
                f"bare `podman update --restart` is the #535 crash: {line.strip()}"
