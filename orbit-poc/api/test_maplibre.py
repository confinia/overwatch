"""Guards issue #145: MapLibre 6 is ESM-only, so the page must import the module
build and must not reference the retired UMD bundle — and the app code must stay
a classic script, or every inline onclick handler loses its function.
"""
import os
import re

STATIC = os.path.join(os.path.dirname(__file__), "..", "web", "static")
INDEX = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
APP = os.path.join(STATIC, "app.js")


def test_uses_the_esm_build():
    assert "maplibre-gl.mjs" in INDEX
    assert 'type="module"' in INDEX


def test_retired_umd_bundle_is_gone():
    """v6 does not publish dist/maplibre-gl.js — LOADING it would 404. Match a
    real script tag, not the string (prose may legitimately name the file)."""
    assert not re.search(r'<script[^>]+src=[^>]*maplibre-gl\.js', INDEX)


def test_single_pinned_version_everywhere():
    versions = set(re.findall(r"maplibre-gl@([0-9]+\.[0-9]+\.[0-9]+)", INDEX))
    assert versions == {"6.1.0"}, versions


def test_library_is_exposed_globally_before_the_app_loads():
    """The app is a classic script: it needs `maplibregl` as a global, and it
    must only be injected once the import resolved."""
    assert "window.maplibregl" in INDEX
    assert INDEX.index("window.maplibregl") < INDEX.index('s.src = "/app.js"')


def test_app_code_is_a_separate_classic_script():
    assert os.path.exists(APP), "static/app.js missing"
    body = open(APP, encoding="utf-8").read()
    assert "new maplibregl.Map(" in body          # the map still lives there
    assert "import " not in body.split("\n\n")[0]  # not turned into a module


def test_oem_import_and_overlay_wired():   # #208 Phase 1c-2
    """A user-uploaded CCSDS OEM plots on the globe: an #oem:<id> deep link
    draws /v1/ephemeris/<id> as a distinct cyan overlay, and the Import control
    POSTs a file to the owner-scoped /v1/ephemeris endpoint."""
    app = open(APP, encoding="utf-8").read()
    assert "function hashOem" in app and "#oem:" in app          # deep-link route
    assert "/api/v1/ephemeris/" in app                            # drawOem fetches track
    assert "oem-track" in app and "00e5cc" in app                 # distinct cyan overlay
    assert "/api/v1/ephemeris" in app and "JSON.stringify({ oem:" in app  # importOem upload
    for el in ('id="oem-import"', 'id="oem-file"', 'id="oem-banner"'):
        assert el in INDEX, el                                    # UI present


def test_next_passes_embedding():   # #217/#232 (in-app integration)
    """Where the next-passes board is (and is not) embedded in the app."""
    app = open(APP, encoding="utf-8").read()
    assert "d-solo/next-passes/next-passes" in app
    # #232: the coverage timeline is NOT embedded in the satellite view — it
    # lives as its own Grafana board until the visualisation earns a place there.
    assert "panelId=3&var-norad=" not in app        # satellite view: no passes embed
    assert "panelId=4&var-station=" in app          # station view keeps its timeline
    assert "panelId=2&var-norad=" not in app        # tables removed
    assert "panelId=1&var-station=" not in app


def test_favorite_satellites_wired():   # #221
    """Signed-in users star satellites: a ★ toggle POSTs/DELETEs the owner-scoped
    /v1/me/satellites, favourites float to the top, and open by default."""
    app = open(APP, encoding="utf-8").read()
    assert "function toggleFavorite" in app
    assert "/api/v1/me/satellites" in app
    assert "myFavorites" in app and "signedIn" in app
    assert 'class="fav' in app                       # the star control on each row


def test_sign_out_asks_confirmation():   # #223
    """A stray click must not drop the session: every user-facing sign-out
    confirms first. The post-delete-org redirect is exempt (already confirmed)."""
    app = open(APP, encoding="utf-8").read()
    assert "function signOut" in app and 'confirm("Sign out of Overwatch?")' in app
    # no bare sign-out link left that navigates straight to logout
    assert 'href="${API_BASE}/api/v1/auth/logout" style' not in app
    acct = open(os.path.join(STATIC, "account.html"), encoding="utf-8").read()
    for line in acct.splitlines():
        if "auth/logout" in line and "Sign out" in line:
            assert "confirm(" in line, line


def test_selected_satellite_is_highlighted_on_the_globe():
    """Selecting a satellite must be obvious on the globe: the dot is drawn
    larger with a bright ring, plus a pulsing halo beneath it — MapLibre paint
    properties can't be CSS-animated, so it's driven from the frame loop."""
    app = open(APP, encoding="utf-8").read()
    assert '"sel": ' in app or "sel: s.norad === activeNorad" in app   # per-feature flag
    assert 'id:"sat-pulse"' in app                    # the halo layer
    assert "pulseSelected" in app and "requestAnimationFrame" in app
    assert "refreshSatHighlight" in app               # follows the selection at once
    assert '["get","sel"]' in app                     # styling keys off it


def test_orbit_altitude_panel_is_grafana_only():
    """The orbit-altitude chart is not embedded in the app any more — it stays
    available in Grafana."""
    app = open(APP, encoding="utf-8").read()
    assert "{ id: 5, wide: true, show: true }" not in app


def test_panel_never_waits_forever_on_the_globe():
    """The satellite panel used to be gated purely on the map's "idle" event.
    On a slow connection the globe streams tiles for tens of seconds (or stalls
    on one) and never goes idle, so the panel sat on "Loading dashboards…"
    indefinitely and no data call was ever made. There must be a timer racing
    the idle event, and the two data sources must load concurrently."""
    app = open(APP, encoding="utf-8").read()
    assert 'map.once("idle", embed)' in app and "setTimeout(embed," in app
    assert "let embedded = false" in app          # whichever fires first wins, once
    assert "Promise.allSettled" in app            # fields + passes in parallel
