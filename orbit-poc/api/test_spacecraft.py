"""Spacecraft view tests (#55).

Structural guards for the dedicated 3D spacecraft window: the page exists, is
wired to Three.js and the telemetry->component mapping, honours the honest-state
rule (a component with no backing field is shown as such, never faked), and the
/w/<view> control-room route (#49) serves it. The 3D render and the mapping's
runtime behaviour are validated in the browser; here we lock the contract so it
can't silently disappear. run-tests.sh copies web/ into the runner."""
import os

HERE = os.path.dirname(__file__)
WEB = os.path.join(HERE, "..", "web")
STATIC = os.path.join(WEB, "static")
PAGE = os.path.join(STATIC, "spacecraft.html")


def _page():
    return open(PAGE, encoding="utf-8").read()


def test_spacecraft_page_exists():
    assert os.path.isfile(PAGE)


def test_spacecraft_uses_three_and_telemetry_mapping():
    t = _page()
    assert "three@0.128" in t, "Three.js not loaded"
    assert "componentMap" in t, "no telemetry->component mapper"
    assert "/api/v1/telemetry/" in t, "not fed by the fields endpoint"


def test_spacecraft_maps_expected_components():          # #55
    t = _page()
    for token in ("battery_v", "battery_i", "battery_pct", "temp", "attitude", "isSunlit"):
        assert token in t, f"component mapping missing {token}"


def test_spacecraft_is_honest_about_missing_data():      # honest-state
    t = _page()
    assert "no data" in t or "no battery telemetry" in t


def test_w_route_serves_control_room_views():            # #49 foundation
    app = open(os.path.join(WEB, "app.py"), encoding="utf-8").read()
    assert "/w/<view>" in app and "unknown view" in app


def test_index_links_the_spacecraft_view():              # #55 discoverable
    idx = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
    assert "/w/spacecraft?sat=" in idx
