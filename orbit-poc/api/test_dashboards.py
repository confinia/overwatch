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
        assert p.get("datasource", {}).get("uid") == "orbitcache", p.get("title")


def test_accounts_orgs_queries_the_org_model():    # #28
    d = json.load(open(os.path.join(OPS, "accounts-orgs.json"), encoding="utf-8"))
    sql = " ".join(t.get("rawSql", "")
                   for p in d["panels"] for t in p.get("targets", []))
    for table in ("organization", "org_user", "org_token", "api_key"):
        assert table in sql, f"dashboard never queries {table}"


def test_next_passes_is_timeline_only_coloured_per_station():   # #232
    """Next passes is ONE graphical view per direction — no tables — and the
    colour identifies the ground station: state-timeline colours by VALUE, so
    the value must be the station label, not the elevation (an elevation value
    produced a colour and a legend entry per distinct degree, swamping the
    panel). The y-axis already names each row, so the legend stays off."""
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
        assert "AS metric" in q and "NULL::text AS value" in q   # band ends at LOS
        # the value IS the series label -> one colour per station, not per degree
        assert re.search(r"SELECT p\.aos AS time, (.+?) AS value, \1 AS metric", q), q
        assert p["fieldConfig"]["defaults"]["color"]["mode"] == "palette-classic"
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
