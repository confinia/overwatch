"""Grafana dashboard tests (#28 accounts & organizations).

Provisioned dashboards ship as JSON in git and are loaded by Grafana's file
provider. These guards keep them valid and keep the internal accounts/orgs
dashboard pointed at the right datasource and tables, so it can't silently
break on a schema or datasource rename. run-tests.sh copies grafana/ into the
runner."""
import glob
import json
import os
import re

HERE = os.path.dirname(__file__)
DASH = os.path.join(HERE, "..", "grafana", "dashboards")
# the admin-only boards moved to their own Grafana org's dir (#168); they are
# still validated here
OPS = os.path.join(HERE, "..", "grafana", "ops-dashboards")


def _dashboards():
    return (glob.glob(os.path.join(DASH, "**", "*.json"), recursive=True)
            + glob.glob(os.path.join(OPS, "*.json")))


def test_all_dashboards_are_valid():
    files = _dashboards()
    assert files, "no dashboard JSON found"
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        assert d.get("uid"), f"{f}: missing uid"
        assert d.get("title"), f"{f}: missing title"
        assert isinstance(d.get("panels"), list) and d["panels"], f"{f}: no panels"


def test_accounts_orgs_dashboard_shape():          # #28
    d = json.load(open(os.path.join(OPS, "accounts-orgs.json"), encoding="utf-8"))
    assert d["uid"] == "accounts-orgs"
    assert "ops" in d.get("tags", []), "ops board (admin-only Grafana org, #168)"
    # every panel targets the provisioned Postgres datasource
    for p in d["panels"]:
        assert p.get("datasource", {}).get("uid") == "orbitcache-ops", p.get("title")


def test_accounts_orgs_queries_the_org_model():    # #28
    d = json.load(open(os.path.join(OPS, "accounts-orgs.json"), encoding="utf-8"))
    sql = " ".join(t.get("rawSql", "")
                   for p in d["panels"] for t in p.get("targets", []))
    for table in ("organization", "org_user", "org_token", "api_key"):
        assert table in sql, f"dashboard never queries {table}"


def test_next_passes_is_timeline_only_with_named_station_rows():   # #232
    """Next passes is ONE graphical view per direction — no tables — and every
    ground station is its own NAMED row.

    The row names come from prepareTimeSeries pivoting the long frame, which
    only works when `value` is NUMERIC and `metric` carries the label. Making
    both columns text broke the pivot and collapsed the panel to two rows
    literally called "value" and "metric" — this test pins that shape.
    Colour is by series NAME (one per station), not by value (which produced a
    colour, and a legend row, per distinct elevation)."""
    d = json.load(open(os.path.join(DASH, "public", "next-passes.json"),
                       encoding="utf-8"))
    assert d["uid"] == "next-passes"
    assert [p["type"] for p in d["panels"]] == ["state-timeline"] * 2, \
        "next passes must be timeline-only (tables removed)"
    sqls = [p["targets"][0]["rawSql"] for p in d["panels"]]
    assert any("observer = '$station'" in q for q in sqls)   # station -> satellites
    assert any("norad = $norad" in q for q in sqls)          # satellite -> stations
    for p, q in zip(d["panels"], sqls):
        assert "FROM pass" in q and "aos > now()" in q
        assert "p.max_el_deg AS value" in q, "value must stay numeric or the pivot dies"
        assert re.search(r"(p\.observer|s\.name) AS metric", q), "no series label"
        assert "NULL AS value" in q                          # band ends at LOS
        assert p["fieldConfig"]["defaults"]["color"]["mode"] == "palette-classic-by-name"
        assert p["options"]["legend"]["showLegend"] is False, "legend floods the panel"


def test_timelines_pivot_long_frames_into_bands():   # #232
    """Grafana 11's SQL datasource returns ONE long frame (time, metric, value).
    Without prepareTimeSeries the timeline draws a single lumped band instead of
    one per ground station — verified against the live query API."""
    d = json.load(open(os.path.join(DASH, "public", "next-passes.json"),
                       encoding="utf-8"))
    for p in d["panels"]:
        if p["type"] != "state-timeline":
            continue
        tr = p.get("transformations") or []
        assert any(t["id"] == "prepareTimeSeries" and
                   t.get("options", {}).get("format") == "wide" for t in tr), \
            f"{p['title']}: long frame is never pivoted into per-series bands"


def test_next_passes_focuses_on_the_next_24h():   # #232
    """The board answers "what's coming up", not "the next week": the time range
    and the queries are both bounded to 24 h — the query bound also keeps the
    embed light, since a 7-day horizon was fetched only to be clipped."""
    d = json.load(open(os.path.join(DASH, "public", "next-passes.json"),
                       encoding="utf-8"))
    assert d["time"] == {"from": "now", "to": "now+24h"}
    for p in d["panels"]:
        assert "interval '24 hours'" in p["targets"][0]["rawSql"], p["title"]
