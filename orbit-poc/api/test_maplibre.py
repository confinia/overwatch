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


def test_next_passes_embedded_in_both_views():   # #217 (in-app integration)
    """The next-passes board is embedded IN the app, both ways: the satellite
    view shows covering stations (panel 2, var-norad); the station view shows
    passing satellites (panel 1, var-station)."""
    app = open(APP, encoding="utf-8").read()
    assert "d-solo/next-passes/next-passes" in app
    assert "panelId=2&var-norad=" in app            # satellite view -> stations (inverted)
    assert "panelId=1&var-station=" in app          # station view -> satellites
