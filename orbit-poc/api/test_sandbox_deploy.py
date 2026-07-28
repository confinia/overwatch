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
