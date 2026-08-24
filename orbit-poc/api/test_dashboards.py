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


def test_series_labels_survive_boilerplate_prefixes():   # #248
    """UWE-4's thermistors share a 58-char decoder prefix; a legend truncates
    from the right, so labeling by raw field rendered six identical series.
    Every timeseries that labels by field must go through the tail-extracting
    CASE, and that expression must (a) leave short names alone and (b) make
    the six thermistors distinct within the first 20 characters."""
    import json as _json
    import re
    files = (os.path.join(HERE, "..", "grafana", "dashboards", "public",
                          "orbit-telemetry.json"),
             os.path.join(HERE, "..", "grafana", "dashboards", "ops",
                          "tenant-demo.json"),
             os.path.join(HERE, "tenant_dashboard.json"))
    seen_case = 0
    for f in files:
        d = _json.load(open(f, encoding="utf-8"))
        for p in d.get("panels", []):
            for t in p.get("targets", []):
                sql = t.get("rawSql", "")
                assert "field AS metric" not in sql, \
                    f"{os.path.basename(f)}/{p.get('title')}: raw field label"
                if "ELSE field END AS metric" in sql:
                    seen_case += 1
    assert seen_case >= 9, "the labeled panels lost the CASE expression"

    # mirror of the SQL expression: same regex, same length gate
    def label(field):
        if len(field) > 40:
            return re.sub(r"^.*_([^_]+_[^_]+_[^_]+_[^_]+)$", r"\1", field)
        return field

    tails = {label("ax25_frame_payload_ax25_info_beacon_payload_beacon_payload_"
                   f"panel_{s}_temp")[:20]
             for s in ("pos_z", "neg_z", "pos_y", "pos_x", "neg_y", "neg_x")}
    assert len(tails) == 6
    assert label("battery_v") == "battery_v"


def _ds_uid(obj):
    ds = obj.get("datasource")
    return ds.get("uid") if isinstance(ds, dict) else ds


def test_no_panel_relies_on_the_default_datasource():   # #319
    """A dashboard must name its datasource. Panels that leave it unset
    resolve to whichever source happens to hold the default flag, and in
    production that was `orbitcache-ops` — a datasource created through the UI
    and never added to provisioning, which runs as `ops_ro`. That role is
    deliberately not granted the public tables, so all 20 unpinned panels
    rendered "permission denied for table reception" as an empty chart on the
    live site while the data itself was fine."""
    for f in _dashboards():
        d = json.load(open(f, encoding="utf-8"))
        name = os.path.basename(f)
        for p in d["panels"]:
            assert _ds_uid(p), f"{name}: panel {p.get('title')!r} has no datasource"
            for t in p.get("targets", []):
                assert _ds_uid(t), \
                    f"{name}: a target of {p.get('title')!r} has no datasource"
        for v in d.get("templating", {}).get("list", []):
            if v.get("type") == "query":
                assert _ds_uid(v), \
                    f"{name}: template variable {v.get('name')!r} has no datasource"


def test_public_boards_use_the_least_privilege_role():   # #319
    """The public embeds are served to anonymous viewers, so they must go
    through `grafana_ro` (uid orbitcache) — never the ops role, and never the
    app role that can reach private tenant data."""
    for f in glob.glob(os.path.join(DASH, "public", "*.json")):
        d = json.load(open(f, encoding="utf-8"))
        for p in d["panels"]:
            assert _ds_uid(p) == "orbitcache", \
                f"{os.path.basename(f)}: {p.get('title')!r} -> {_ds_uid(p)}"
