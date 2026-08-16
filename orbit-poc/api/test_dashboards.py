"""Grafana dashboard tests (#28 accounts & organizations).

Provisioned dashboards ship as JSON in git and are loaded by Grafana's file
provider. These guards keep them valid and keep the internal accounts/orgs
dashboard pointed at the right datasource and tables, so it can't silently
break on a schema or datasource rename. run-tests.sh copies grafana/ into the
runner."""
import glob
import json
import os

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


def test_next_passes_dashboard_both_views_and_colour_bands():   # #217
    """Two views over `pass`: satellites-over-a-station AND the inverted
    stations-covering-a-satellite; both sorted by AOS with the exact imminence
    colour bands, selectable by $station and $norad."""
    d = json.load(open(os.path.join(DASH, "public", "next-passes.json"),
                       encoding="utf-8"))
    assert d["uid"] == "next-passes"
    sqls = [p["targets"][0]["rawSql"] for p in d["panels"]]
    joined = "\n".join(sqls)
    assert "FROM pass" in joined and "aos > now()" in joined
    assert any("observer = '$station'" in s for s in sqls)   # station -> satellites
    assert any("norad = $norad" in s for s in sqls)          # satellite -> stations (inverted)
    for p in d["panels"]:                        # tables colour-code AOS-in-h
        if p["type"] != "table":
            continue
        ov = next(o for o in p["fieldConfig"]["overrides"]
                  if o["matcher"]["options"] == "AOS in (h)")
        steps = next(x["value"]["steps"] for x in ov["properties"]
                     if x["id"] == "thresholds")
        assert [(s.get("value"), s["color"]) for s in steps] == \
            [(None, "red"), (1, "orange"), (24, "yellow"), (168, "transparent")]
    names = {v["name"] for v in d["templating"]["list"]}
    assert {"station", "norad"} <= names


def test_next_passes_has_coverage_timelines():   # #232
    """Passes are shown graphically, not only as a table: a state-timeline per
    view, one coloured band per ground station (resp. per satellite), so
    overlapping coverage is visible at a glance."""
    d = json.load(open(os.path.join(DASH, "public", "next-passes.json"),
                       encoding="utf-8"))
    tls = [p for p in d["panels"] if p["type"] == "state-timeline"]
    assert len(tls) == 2, "expected a timeline for both views"
    for p in tls:
        sql = p["targets"][0]["rawSql"]
        assert p["targets"][0]["format"] == "time_series"
        assert "AS metric" in sql                  # one band per station/satellite
        assert "UNION ALL" in sql and "NULL AS value" in sql   # band ends at LOS
        assert p["fieldConfig"]["defaults"]["color"]["mode"] == "palette-classic"
    grouped = {("p.observer AS metric" in p["targets"][0]["rawSql"]) for p in tls}
    assert True in grouped, "no timeline grouped by ground station"


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
