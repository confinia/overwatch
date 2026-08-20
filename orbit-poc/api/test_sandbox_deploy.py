"""Guards issue #119: `make sandbox-up` must do a CLEAN recreate. podman-compose
`up` collides on existing container names instead of recreating them, so a
redeploy silently keeps the OLD containers unless an explicit `down` runs first.
This asserts the sandbox-up recipe runs `down` before `up`.
"""
import os
import re

HERE = os.path.dirname(__file__)
# repo root locally (../../Makefile); copied next to the suite in CI (../Makefile)
MAKEFILE = next(
    p for p in (os.path.join(HERE, "..", "Makefile"),
                os.path.join(HERE, "..", "..", "Makefile"))
    if os.path.exists(p))


def _sandbox_up_recipe(text):
    # capture from `sandbox-up:` to the next target (line starting non-tab, non-comment)
    m = re.search(r'^sandbox-up:.*?(?=^\S)', text, re.S | re.M)
    return m.group(0) if m else ""


def test_sandbox_up_does_clean_recreate():
    recipe = _sandbox_up_recipe(open(MAKEFILE).read())
    assert recipe, "sandbox-up target not found in Makefile"
    down = recipe.find("compose -p ovw-sandbox -f docker-compose.yml down")
    up = recipe.find("compose -p ovw-sandbox -f docker-compose.yml up")
    assert down != -1, "sandbox-up must run an explicit `down` (clean recreate)"
    assert up != -1, "sandbox-up must run `up`"
    assert down < up, "the `down` must precede the `up` in sandbox-up"


def test_sandbox_workflow_deploys_without_a_downtime_window():   # #237
    """The automatic deploy must NOT tear the stack down: `down` removes the
    caddy for the 2-4 minutes of a rebuild and everything aimed at the sandbox
    — a prospect's browser, our own e2e walks — gets a bare 502. `up -d
    --build` recreates only what changed and the listener stays up.

    The Makefile's `sandbox-up` keeps its explicit down->up: that one is an
    operator deliberately asking for a clean recreate, not a push."""
    wf = open(os.path.join(os.path.dirname(MAKEFILE), ".github", "workflows",
                           "sandbox.yml"), encoding="utf-8").read()
    deploy = wf.split("Check it answers")[0]
    for line in deploy.splitlines():
        code = line.split("#", 1)[0]
        assert "compose -p ovw-sandbox -f docker-compose.yml down" not in code, \
            f"the workflow still tears the sandbox down: {line.strip()}"
    # the build must happen while the old stack still serves...
    b = deploy.index("$C build")
    # ...and the swap must be EXPLICIT: `up` alone keeps the old containers
    # (#119), so a plain `up -d --build` would deploy nothing at all
    r = deploy.index("--force-recreate")
    assert b < r, "the build must precede the recreate, or the swap is the slow part"
    assert "caddy" not in deploy[r:deploy.index("\n", r)], \
        "caddy must NOT be force-recreated — it is what keeps the listener up"
    # a routing change must still be picked up, gracefully
    assert "caddy reload" in deploy


def test_sandbox_deploy_cannot_serve_a_stale_image():   # #237 follow-up
    """Dropping the `down` (#237) removed the downtime window but introduced a
    worse failure: `--force-recreate` recreates the CONTAINER while reusing the
    image id pinned in it, so a freshly built :latest is ignored and the stack
    silently serves yesterday's code — with every check green. Observed live.

    Two properties fix it: the app containers are REMOVED before `up` (so the
    tag is resolved again), and the deploy asserts the running container uses
    the image just built. Container age proves neither."""
    wf = open(os.path.join(os.path.dirname(MAKEFILE), ".github", "workflows",
                           "sandbox.yml"), encoding="utf-8").read()
    deploy = wf.split("Check it answers")[0]
    # podman-compose 1.3.0 has no `rm` subcommand, so this uses podman directly.
    # Join shell line-continuations first: the command spans two lines.
    logical, buf = [], ""
    for line in deploy.splitlines():
        buf += line.rstrip()
        if buf.endswith("\\"):
            buf = buf[:-1] + " "
            continue
        logical.append(buf); buf = ""
    rm_lines = [l for l in logical if "podman rm -f" in l]
    assert rm_lines, "containers must be removed so `up` re-resolves the image tag"
    removed = " ".join(rm_lines)
    for svc in ("web", "api", "grafana", "ingest"):
        assert f"ovw-sandbox_{svc}_1" in removed, svc
    # caddy and db are never torn down — that is what keeps the listener up
    assert "caddy" not in removed and "_db_1" not in removed
    # and the deploy must prove the code is new, not merely the container
    # `up` must touch ONLY the services just removed: podman-compose errors on
    # an existing container name instead of skipping it, so a bare `up` dies on
    # the still-running caddy and nothing is recreated at all.
    up_line = next(l for l in logical if " up -d" in l and "--no-deps" in l)
    for svc in ("web", "api", "grafana", "ingest"):
        assert svc in up_line, svc
    assert "caddy" not in up_line
    assert "STALE DEPLOY" in deploy
    assert "podman image inspect" in deploy and "{{.Image}}" in deploy
