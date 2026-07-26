"""Frontend affordance tests (#45 sign-in visible, #47 contact link).

Reads the served index.html and asserts the lead-capture affordances are
present, so they can't silently regress.
"""
import os
import re
import pytest

HERE = os.path.dirname(__file__)
WEB = os.path.join(HERE, "..", "web")
STATIC = os.path.join(WEB, "static")
INDEX = os.path.join(STATIC, "index.html")
PAGES = ("index.html", "pro.html", "article.html", "talk.html")


@pytest.fixture(scope="module")
def html():
    if not os.path.exists(INDEX):
        pytest.skip("web/static not available in this run")
    return open(INDEX, encoding="utf-8").read()


def test_contact_link_present(html):            # #47
    assert "mailto:contact@confinia.io" in html


def test_signin_is_visible_action(html):        # #45
    # the anonymous entry uses the visible accent class, not the dim badge
    assert 'class="action"' in html
    assert "Sign in / Register" in html


def test_footer_links_present(html):            # #47
    for label in ("Contact", "Source", "API", "Write-up"):
        assert f">{label}<" in html


def test_favicon_files_exist():                 # #59
    if not os.path.exists(STATIC):
        pytest.skip("web/static not available in this run")
    for name in ("favicon.svg", "favicon.ico"):
        assert os.path.exists(os.path.join(STATIC, name)), f"missing {name}"


def test_every_page_links_github_source():      # #75
    if not os.path.exists(STATIC):
        pytest.skip("web/static not available in this run")
    for page in PAGES + ("spacecraft.html",):
        t = open(os.path.join(STATIC, page), encoding="utf-8").read()
        assert "github.com/confinia/overwatch" in t, f"{page} has no Source link"


def test_every_page_links_favicon():            # #59
    if not os.path.exists(STATIC):
        pytest.skip("web/static not available in this run")
    for page in PAGES:
        t = open(os.path.join(STATIC, page), encoding="utf-8").read()
        assert 'rel="icon"' in t and "favicon.svg" in t, f"{page} missing favicon link"


def test_native_fields_panel_replaces_grafana_table(html):   # #42/#46
    # the decoded-fields panel is now native (clickable), not a Grafana iframe
    assert "fieldsPanelHTML" in html and "fields-cell" in html
    assert 'table class="fields"' in html
    # Grafana panel id 4 (the old fields table) is no longer embedded
    assert "{ id: 4," not in html


def test_fields_coloured_by_source(html):        # #46
    # each source category has a distinct style hook
    for cat in ("canonical", "telemetry", "transport"):
        assert f"tr.fld.{cat}" in html, f"missing colour rule for {cat}"


def test_field_click_flies_to_reception(html):   # #42
    # clicking a field jumps to the nearest reception line and pulses it
    assert "jumpToReception" in html and "wireFieldRows" in html
    assert "pulseLink" in html and "rxLinkFeatures" in html


def test_reception_legend_anchored_top_right(html):   # #62
    # the reception legend sits in the top-right of the map, not over the
    # dashboards at the bottom of the left column
    m = re.search(r"#rxlegend\s*\{[^}]*\}", html)
    assert m, "no #rxlegend rule"
    rule = m.group(0)
    # anchored to the top-right, not the bottom-left (border-left is unrelated)
    assert re.search(r"\btop:\s*\d", rule) and re.search(r"[; {]right:\s*\d", rule)
    assert "bottom:" not in rule and not re.search(r"[; {]left:\s*\d", rule)


def test_track_splits_at_antimeridian(html):     # #66
    # the ground track is split at the ±180 seam so it can't paint a chord/
    # circle across the globe near the poles
    assert "splitAntimeridian" in html
    assert "MultiLineString" in html


def test_satellite_title_has_operator_link(html):   # operator POC in title bar
    assert "db.satnogs.org/satellite/" in html and "Operator ↗" in html


def test_station_deeplink_guards_track_source(html):  # direct #station: link fix
    # landing straight on a #station: deep link selects no satellite first, so
    # the lazily-created track sources may not exist; clearing them must be guarded
    assert 'for (const src of ["track", "track-arcs"])' in html


def test_station_contact_operator_link(html):    # #80
    assert "qrz.com/db/" in html and "Contact operator" in html


def test_station_satellites_are_clickable(html): # #81
    assert 'class="stsat"' in html and 'href="#${x.norad}"' in html


def test_popup_links_use_accent_not_purple(html):  # #82
    assert ".maplibregl-popup-content a" in html and ":visited" in html


def test_track_line_is_clickable(html):          # #65
    # a wide hit-line + click handler open a popup with the track's satellite
    # and time window
    assert "track-hit" in html and "onTrackClick" in html
    assert "Ground track" in html


def test_blue_track_spans_selected_range(html):  # #79
    # the blue track follows the selected window and fades/thins for long ranges
    assert "/api/track/${norad}?hours=" in html
    assert 'setPaintProperty("track", "line-opacity"' in html
    app_py = open(os.path.join(WEB, "app.py"), encoding="utf-8").read()
    # default (non-heard) track is windowed + downsampled to ~2000 points
    assert "row_number()" in app_py and "GREATEST(total" in app_py


def test_track_shows_heard_pass_arcs(html):      # #70 (no orphan endpoints)
    # the track is split into per-pass arcs, and the API returns only positions
    # near a reception, so orange endpoints land on a visible arc (no flood)
    assert "splitPasses" in html
    app_py = open(os.path.join(WEB, "app.py"), encoding="utf-8").read()
    assert "EXISTS" in app_py and "reception r" in app_py


def test_resizable_collapsible_panes(html):      # #69
    assert 'id="gutter-side"' in html and 'id="gutter-map"' in html
    assert "setupGutters" in html and "dragGutter" in html
    assert "--side-w" in html and "--map-h" in html      # grid tracks are vars
    assert "ovw_sideW" in html and "ovw_mapH" in html     # persisted per browser
    assert "map.resize()" in html                          # WebGL re-render on change


def test_time_range_selector_drives_all_views(html):   # #71/#72
    assert "rangebar" in html and "rangeHours" in html and "RANGES" in html
    # one selected range threaded into receptions, decoded fields and the
    # heard-pass track arcs
    for frag in ("/api/track/${norad}?heard=1&hours=",
                 "/api/receptions/${norad}?hours=",
                 "fields?hours="):
        assert frag in html, frag


def test_satnogs_dashboard_linkout(html):              # #88
    # each satellite with a curated SatNOGS Grafana dashboard gets a discovered
    # "Telemetry Dashboard" link next to Operator; the map is resolved offline
    # (batch/resolve_satnogs_dashboards.py) and served by /api/satellites.
    import json
    mp = os.path.join(WEB, "satnogs_dashboards.json")
    assert os.path.isfile(mp), "missing discovered dashboard map"
    m = json.load(open(mp, encoding="utf-8"))
    assert isinstance(m, dict) and m, "dashboard map is empty"
    for norad, url in m.items():
        assert norad.isdigit(), f"non-norad key {norad}"
        assert url.startswith("https://dashboard.satnogs.org/d/"), url
    # API merges the map into each satellite row
    app_py = open(os.path.join(WEB, "app.py"), encoding="utf-8").read()
    assert "SATNOGS_DASHBOARDS" in app_py and '"satnogs_dashboard"' in app_py
    # the runtime image must actually ship the map next to app.py (the gate
    # copies all of web/, but the Dockerfile is selective) — else it loads empty
    dockerfile = open(os.path.join(WEB, "Dockerfile"), encoding="utf-8").read()
    assert "COPY satnogs_dashboards.json" in dockerfile, \
        "Dockerfile must ship satnogs_dashboards.json into the image"
    # title bar renders the link only when the satellite has one (honest-state)
    assert "s.satnogs_dashboard" in html and "Telemetry Dashboard ↗" in html


def test_ground_station_panels_from_reception(html):   # #86
    # a generic ground-station leaderboard + reception summary, learned from
    # SatNOGS dashboards (every one leads with a station leaderboard) and built
    # from our own reception table — shown for every satellite, no curation.
    import json
    dash = os.path.join(HERE, "..", "grafana", "dashboards", "public",
                        "orbit-telemetry.json")
    d = json.load(open(dash, encoding="utf-8"))
    by_id = {p["id"]: p for p in d["panels"]}
    for pid in (13, 14):
        assert pid in by_id, f"dashboard missing ground-station panel {pid}"
        sql = by_id[pid]["targets"][0]["rawSql"]
        assert "FROM reception" in sql and "norad = $norad" in sql
        assert "$__timeFilter(ts)" in sql, "panel must honour the range selector"
    assert by_id[13]["type"] == "bargauge"   # leaderboard
    # both surfaced in the embed
    assert "{ id: 13," in html and "{ id: 14," in html


def test_auto_grouped_telemetry_panels(html):          # #88
    # every decoded numeric field lands in a chart: the shared dashboard gains
    # category panels (counters, power, modes) plus a catch-all, and the
    # frontend gates each one on the fields actually present (no per-sat
    # curation, no empty panels).
    import json
    dash = os.path.join(HERE, "..", "grafana", "dashboards", "public",
                        "orbit-telemetry.json")
    d = json.load(open(dash, encoding="utf-8"))
    ids = {p["id"] for p in d["panels"]}
    for pid in (9, 10, 11, 12):
        assert pid in ids, f"dashboard missing auto-group panel {pid}"
    # panel 12 is the honest catch-all: it must negate the specific filters so
    # it never double-charts a field another panel already owns
    other = next(p for p in d["panels"] if p["id"] == 12)
    sql = other["targets"][0]["rawSql"]
    assert "!~*" in sql and "value_num IS NOT NULL" in sql
    # frontend shows each new panel conditionally, mirroring the SQL filters
    for tok in ("COUNT_RE", "POWER_RE", "MODE_RE", "hasOther",
                "{ id: 9,", "{ id: 12,"):
        assert tok in html, f"embed missing {tok}"


def test_api_hours_param_is_bounded():                 # #71
    # both the web map API and the public /v1 fields API bound the window 1..168h
    app_py = open(os.path.join(WEB, "app.py"), encoding="utf-8").read()
    assert "_hours" in app_py and "min(h, 168)" in app_py
    main_py = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert "hours: int = Query(168, ge=1, le=168" in main_py
