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


def test_maplibre_is_vendored_not_fetched_from_a_cdn():
    """RULES.md rule 25 — nothing in the render path may come from a CDN we do
    not control. MapLibre is served from our own origin: sovereignty (an
    EU-hosted product must not need a US CDN to draw its globe), availability
    (a CDN outage blanks the map with no deploy of ours), privacy (visitor IPs
    handed to a third party) and integrity (a compromised CDN runs JS in our
    page). The version lives beside the bundle now that it is not in a URL."""
    assert "unpkg.com" not in INDEX and "cdn.jsdelivr" not in INDEX
    assert "/vendor/maplibre/maplibre-gl.mjs" in INDEX
    assert "/vendor/maplibre/maplibre-gl.css" in INDEX
    vd = os.path.join(STATIC, "vendor", "maplibre")
    # the ESM build splits into chunks that resolve each other relatively —
    # shipping only the entrypoint gives a blank globe at runtime
    for f, min_kb in (("maplibre-gl.mjs", 400), ("maplibre-gl-shared.mjs", 300),
                      ("maplibre-gl-worker.mjs", 10), ("maplibre-gl.css", 40)):
        p = os.path.join(vd, f)
        assert os.path.exists(p), f"missing vendored {f}"
        # a truncated download or an error page would still "exist"
        assert os.path.getsize(p) > min_kb * 1024, f"{f} looks truncated"
    assert os.path.exists(os.path.join(vd, "VERSION"))
    assert "6.4.0" in open(os.path.join(vd, "VERSION"), encoding="utf-8").read()


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


def test_default_landing_satellite():
    """With no deep link and no favourite, the app opens on the default
    satellite — but a signed-in user's own favourite still wins (#221), and if
    the default ever leaves the fleet it falls back to the freshest telemetry."""
    app = open(APP, encoding="utf-8").read()
    assert "const DEFAULT_NORAD = 69015" in app
    i_fav = app.index("if (fav) { select(fav, true); }")
    i_def = app.index("else if (dflt) { select(dflt, true); }")
    assert i_fav < i_def, "the user's favourite must take precedence"
    assert "satsByNorad[DEFAULT_NORAD]" in app     # fallback when absent


def test_panel_loading_is_visible_and_counted():   # #239
    """A slow or stalled panel must never look like a frozen app: the wait names
    what it is waiting for (globe tiles in flight), embedded panels are counted
    in as they paint, and a panel that never answers says so instead of pulsing
    forever — which is exactly how an infinite load hid in plain sight."""
    app = open(APP, encoding="utf-8").read()
    assert "trackPanelLoading" in app and "gfload" in app
    assert "gfpending" in app                      # per-cell shimmer
    assert "gfstuck" in app and "not answering" in app   # explicit stalled state
    # the wait explains itself with live globe numbers
    assert "globeStatusText" in app and "map tile" in app
    assert 'map.on("dataloading"' in app and 'map.on("idle"' in app
    # the cold-reload double load must not count twice
    assert "dataset.counted" in app and "dataset.r" in app
    idx = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
    assert "@keyframes shimmer" in idx and ".gfload" in idx


def test_private_satellite_clears_open_data_map_state():
    """Selecting a PRIVATE org satellite must not inherit the previously
    selected open-data satellite's map state. Its telemetry is pushed by the
    org — SatNOGS never heard it — so a leftover "N ground stations heard this
    satellite" legend, orange reception lines or a blue ground track would be
    read as belonging to the private satellite. All of it is cleared."""
    app = open(APP, encoding="utf-8").read()
    fn = app[app.index("function selectOrgSat"):]
    fn = fn[:fn.index("\n}")]
    assert 'setRxLegend("")' in fn, "stale reception legend kept"
    for src in ("rx-links", "rx-stations", "rx-endpoints", "track", "track-arcs"):
        assert src in fn, f"{src} not cleared for a private satellite"
    assert "clearPulse()" in fn
