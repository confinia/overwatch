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


def test_next_passes_dashboard_colour_bands():   # #217
    """The next-passes table: sorted-by-AOS query over `pass`, a $station
    selector, and the exact imminence colour bands."""
    d = json.load(open(os.path.join(DASH, "public", "next-passes.json"),
                       encoding="utf-8"))
    assert d["uid"] == "next-passes"
    panel = d["panels"][0]
    sql = panel["targets"][0]["rawSql"]
    assert "FROM pass" in sql and "aos > now()" in sql and "ORDER BY p.aos" in sql
    assert "$station" in sql
    ov = next(o for o in panel["fieldConfig"]["overrides"]
              if o["matcher"]["options"] == "AOS in (h)")
    steps = next(p["value"]["steps"] for p in ov["properties"]
                 if p["id"] == "thresholds")
    got = [(s.get("value"), s["color"]) for s in steps]
    assert got == [(None, "red"), (1, "orange"), (24, "yellow"), (168, "transparent")], got
    assert any(v["name"] == "station" for v in d["templating"]["list"])
