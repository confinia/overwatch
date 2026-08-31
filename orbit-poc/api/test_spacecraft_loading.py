"""Guards the spacecraft view's loading state.

A single static line ("Loading spacecraft…") sat on screen for the whole
start-up: a ~3s telemetry call plus WebGL scene creation. That reads as a
frozen page, and if buildScene threw, the overlay stayed for ever with
nothing said — the same invisible infinite load that #239 fixed for the
dashboard embeds.
"""
import os

PAGE = os.path.join(os.path.dirname(__file__), "..", "web", "static",
                    "spacecraft.html")


def _src():
    return open(PAGE, encoding="utf-8").read()


def test_the_overlay_moves_while_it_loads():
    src = _src()
    assert 'class="spin"' in src, "no moving indicator: a static line reads as frozen"
    assert "@keyframes spin" in src
    assert "prefers-reduced-motion" in src, \
        "a reader who asked for less motion must still get the state"


def test_it_says_which_step_it_is_on():
    src = _src()
    assert "function loadingStep(" in src
    for step in ("Building the 3D view", "Loading the fleet",
                 "Loading decoded telemetry"):
        assert step in src, f"the {step!r} step is never announced"
    assert 'aria-live="polite"' in src, "the step must reach a screen reader"


def test_a_failure_is_shown_instead_of_spinning_for_ever():
    src = _src()
    assert "function loadingFailed(" in src
    load = src[src.index("async function load(){"):]
    load = load[:load.index("\nasync function refresh")]
    assert "catch" in load and "loadingFailed(" in load, \
        "buildScene throwing must name the failure, not leave the overlay up"
    assert "location.reload()" in src, "a stuck reader needs the one action that works"


def test_the_periodic_refresh_stays_silent():
    """The 15s refresh must not narrate itself over a working page: only the
    first load passes `say`."""
    src = _src()
    assert "async function refresh(say)" in src
    assert "setInterval(refresh, 15000)" in src, \
        "the interval must call refresh with no reporter"
