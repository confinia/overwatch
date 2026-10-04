"""Step 2 of #526: the per-satellite board picks fields through the semantic
layer instead of guessing from names.

Every measure panel used to carry its own regex over field names — so the
Temperatures panel plotted `..._temp_check_csp...` and the "everything else"
panel was a wall of 253 lines, mostly packet header. Now the api writes what
each field IS into field_semantic, Grafana joins it, scaffolding is never
plotted, and the further measures a satellite reports appear as repeated
panels — none for a measure it does not have.
"""
import json
import os
import re

HERE = os.path.dirname(__file__)
BOARD = os.path.join(HERE, "..", "grafana", "dashboards", "public", "orbit-telemetry.json")
MAIN = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()


def _board():
    return json.load(open(BOARD, encoding="utf-8"))


def _telemetry_panels(d):
    """Panels whose query reads the telemetry table at all."""
    out = []
    for p in d["panels"]:
        sql = " ".join(t.get("rawSql", "") for t in p.get("targets", []))
        if re.search(r"\bFROM telemetry\b", sql):
            out.append((p, sql))
    return out


def test_the_layer_is_materialised_and_readable_by_the_public_role():
    assert "CREATE TABLE IF NOT EXISTS field_semantic" in MAIN
    tables = MAIN[MAIN.index("GRAFANA_PUBLIC_TABLES = ("):][:700]
    assert '"field_semantic"' in tables, "grafana_ro cannot read it -> every panel empty, no error"
    assert "ON CONFLICT (norad, field) DO UPDATE" in MAIN, "re-classification must overwrite, never duplicate"
    lifespan = MAIN[MAIN.index("async def lifespan("):][:3000]
    assert "_field_semantics_loop()" in lifespan, "the table must fill at boot, not on first report"


def test_no_telemetry_panel_guesses_fields_from_their_names():
    """The regex per panel is what plotted the packet header."""
    for p, sql in _telemetry_panels(_board()):
        assert "~*" not in sql and "!~*" not in sql, f"panel {p['id']} {p['title']!r} still regexes field names"


def test_measure_panels_join_the_layer_with_its_range():
    want = {1: "voltage", 2: "temperature", 3: "current", 9: "counter", 10: "power", 11: "state"}
    panels = {p["id"]: sql for p, sql in _telemetry_panels(_board())}
    for pid, m in want.items():
        sql = panels[pid]
        assert "JOIN field_semantic s" in sql and f"s.measure = '{m}'" in sql, (pid, m)
        assert "BETWEEN s.lo AND s.hi" in sql, f"panel {pid}: the plausible range lives in the layer now"


def test_scaffolding_is_never_plotted_and_unknown_is_the_work_queue():
    panels = {p["id"]: (p, sql) for p, sql in _telemetry_panels(_board())}
    p12, sql12 = panels[12]
    assert "s.kind = 'unknown'" in sql12 and "work queue" in p12["title"]
    p4, sql4 = panels[4]
    assert "s.kind <> 'scaffolding'" in sql4, "the latest-fields table must hide frame plumbing"
    for pid, (p, sql) in panels.items():
        if "field_semantic" in sql:
            assert "scaffolding" not in sql or "<> 'scaffolding'" in sql, pid


def test_further_measures_repeat_only_over_what_the_satellite_reports():
    d = _board()
    var = next(v for v in d["templating"]["list"] if v["name"] == "measure")
    assert "FROM field_semantic WHERE norad = $norad" in var["query"]
    assert var["multi"] and var["includeAll"]
    rep = next(p for p in d["panels"] if p.get("repeat") == "measure")
    assert rep["title"] == "$measure"
    sql = rep["targets"][0]["rawSql"]
    assert "s.measure = '$measure'" in sql and "JOIN field_semantic s" in sql
    assert rep["datasource"]["uid"] == "orbitcache" and rep["targets"][0]["datasource"]["uid"] == "orbitcache"
