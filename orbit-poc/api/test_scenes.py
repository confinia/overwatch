"""Guards #457: imaging scenes from an open STAC catalog (Planet's disaster
releases) become footprints on the open globe, linked to the satellite that
took them, with the capture-to-publication lag as a KPI. The walker is
exercised over an inline catalog tree (no network); storage needs a Postgres."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from conftest import require_test_db

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))
os.environ.setdefault("DB_DSN", "")
import main  # noqa: E402
import scenes  # noqa: E402

DSN = os.environ.get("DB_DSN")
ROOT = "https://stac.example/planet/catalog.json"
NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _item(id_, when, platform="SSC1", constellation="skysat", published_h=5):
    t = datetime.fromisoformat(when).replace(tzinfo=timezone.utc)
    return {"type": "Feature", "id": id_,
            "geometry": {"type": "Polygon",
                         "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
            "properties": {"datetime": t.isoformat().replace("+00:00", "Z"),
                           "published": (t + timedelta(hours=published_h)).isoformat(),
                           "platform": platform, "constellation": constellation,
                           "gsd": 0.5, "eo:cloud_cover": 3.0},
            "assets": {"thumbnail": {"href": f"./{id_}_thumb.png"},
                       "visual": {"href": f"./{id_}_visual.tif"}}}


def _coll(id_, items, end):
    return {"type": "Collection", "id": id_,
            "extent": {"temporal": {"interval": [["2026-01-01T00:00:00Z", end]]}},
            "links": [{"rel": "item", "href": f"./{i}.json"} for i in items]}


def _tree():
    """Two events as Planet lays them out: one with pre/post-event
    sub-catalogs, one flat with a collection directly under the event."""
    base = "https://stac.example/planet/"
    docs = {
        ROOT: {"type": "Catalog", "id": "planet-disasters", "title": "Planet Crisis Response",
               "links": [{"rel": "child", "href": "./fire/catalog.json"},
                         {"rel": "child", "href": "./flood/catalog.json"}]},
        base + "fire/catalog.json": {
            "type": "Catalog", "id": "fire", "title": "Planet Crisis Response — Big Fire (2026)",
            "description": "A wildfire.",
            "links": [{"rel": "child", "href": "./pre-event/catalog.json"},
                      {"rel": "child", "href": "./post-event/catalog.json"}]},
        base + "fire/pre-event/catalog.json": {
            "type": "Catalog", "id": "fire-pre",
            "links": [{"rel": "child", "href": "./skysat/collection.json"}]},
        base + "fire/pre-event/skysat/collection.json":
            _coll("fire-pre-skysat", ["s1"], "2026-09-18T00:00:00Z"),
        base + "fire/pre-event/skysat/s1.json": _item("s1", "2026-09-17T10:00:00"),
        base + "fire/post-event/catalog.json": {
            "type": "Catalog", "id": "fire-post",
            "links": [{"rel": "child", "href": "./pelican/collection.json"}]},
        base + "fire/post-event/pelican/collection.json":
            _coll("fire-post-pelican", ["p1", "p2"], "2026-09-19T00:00:00Z"),
        base + "fire/post-event/pelican/p1.json": _item("p1", "2026-09-19T08:00:00", "3009", "pelican"),
        base + "fire/post-event/pelican/p2.json": _item("p2", "2026-09-19T09:00:00", "3009", "pelican", 30),
        base + "flood/catalog.json": {
            "type": "Catalog", "id": "flood", "title": "Planet Crisis Response — Old Flood (2026)",
            "links": [{"rel": "child", "href": "./collection.json"}]},
        base + "flood/collection.json": _coll("flood-all", ["f1"], "2026-03-01T00:00:00Z"),
        base + "flood/f1.json": _item("f1", "2026-02-28T12:00:00", "24e5", "planetscope"),
    }
    return docs


class _Resp:
    def __init__(self, doc):
        self.doc, self.status_code = doc, 200 if doc is not None else 404

    def raise_for_status(self):
        if self.doc is None:
            raise RuntimeError("404")

    def json(self):
        return self.doc


def _get(docs, calls):
    def get(url, **kw):
        calls.append(url)
        return _Resp(docs.get(url))
    return get


def test_catalog_spec_is_forgiving():
    assert scenes.catalogs("planet=https://x.example/catalog.json") == \
        [("planet", "https://x.example/catalog.json")]
    assert scenes.catalogs(" A=https://a/c.json , junk, =https://b, c=ftp://d ") == \
        [("a", "https://a/c.json")]
    assert scenes.catalogs("") == []


def test_walk_finds_every_event_and_scene_whatever_the_tree_depth():
    calls = []
    events, todo = scenes.walk(_get(_tree(), calls), ROOT, set(), lambda c: False, NOW)
    assert [(e[0], e[1], e[2]) for e in events] == [
        ("fire", "Planet Crisis Response", "Planet Crisis Response — Big Fire (2026)"),
        ("flood", "Planet Crisis Response", "Planet Crisis Response — Old Flood (2026)")]
    assert events[0][3] == "A wildfire."
    got = {(u.rsplit("/", 1)[1], ev, coll, phase) for u, ev, coll, phase in todo}
    assert got == {("s1.json", "fire", "fire-pre-skysat", "pre-event"),
                   ("p1.json", "fire", "fire-post-pelican", "post-event"),
                   ("p2.json", "fire", "fire-post-pelican", "post-event"),
                   ("f1.json", "flood", "flood-all", None)}
    # the walk itself never fetches items: catalogs and collections only
    assert not any(u.endswith(("s1.json", "p1.json", "p2.json", "f1.json")) for u in calls)


def test_stored_scenes_and_cold_collections_are_not_fetched_again():
    calls = []
    # s1 already stored; the flood collection ended in March and has scenes
    events, todo = scenes.walk(_get(_tree(), calls), ROOT, {"s1"},
                               lambda c: c == "flood-all", NOW)
    assert {u.rsplit("/", 1)[1] for u, *_ in todo} == {"p1.json", "p2.json"}
    # a cold collection WITHOUT scenes yet is still walked (first run after a
    # deploy must backfill it)
    _, todo = scenes.walk(_get(_tree(), []), ROOT, set(), lambda c: False, NOW)
    assert any(u.endswith("f1.json") for u, *_ in todo)


def test_a_broken_branch_does_not_sink_the_other_events():
    docs = _tree()
    docs["https://stac.example/planet/fire/post-event/catalog.json"] = None
    events, todo = scenes.walk(_get(docs, []), ROOT, set(), lambda c: False, NOW)
    assert [e[0] for e in events] == ["fire", "flood"]
    assert {u.rsplit("/", 1)[1] for u, *_ in todo} == {"s1.json", "f1.json"}


def test_the_walk_has_a_request_ceiling(monkeypatch):
    # a runaway catalog costs a bounded number of GETs; what was found before
    # the ceiling is still returned, so the cycle stores it
    monkeypatch.setattr(scenes, "MAX_DOCS", 3)
    calls = []
    events, todo = scenes.walk(_get(_tree(), calls), ROOT, set(), lambda c: False, NOW)
    assert len(calls) == 3
    assert [e[0] for e in events] == ["fire"] and todo == []


def test_platform_tokens_match_the_operator_feed_names():
    # "SKYSAT C1 S3", "PELICAN 2 3009 3009", "FLOCK 4Q 16 24E5"
    assert scenes.platform_token("skysat", "SSC1") == ("SKYSAT", "C1")
    assert scenes.platform_token("skysat", "SSC16") == ("SKYSAT", "C16")
    assert scenes.platform_token("pelican", "3009") == ("PELICAN", "3009")
    assert scenes.platform_token("planetscope", "24e5") == ("FLOCK", "24E5")
    assert scenes.platform_token(None, None) == (None, None)


def test_scene_row_keeps_metadata_and_resolves_asset_urls():
    url = "https://stac.example/planet/fire/post-event/pelican/p1.json"
    row = scenes.scene_row(_item("p1", "2026-09-19T08:00:00", "3009", "pelican"),
                           "fire", "fire-post-pelican", "post-event", url)
    assert row[:6] == ("p1", "fire", "fire-post-pelican", "post-event", "pelican", "3009")
    assert row[6] == datetime(2026, 9, 19, 8, tzinfo=timezone.utc)
    assert row[7] - row[6] == timedelta(hours=5)
    assert (row[8], row[9]) == (0.5, 3.0)
    assert json.loads(row[10])["type"] == "Polygon"
    assert row[11] == "https://stac.example/planet/fire/post-event/pelican/p1_thumb.png"
    assert row[12].endswith("/p1_visual.tif") and row[13] == url
    # no footprint or no capture time: not a scene
    bare = _item("x", "2026-09-19T08:00:00")
    bare["geometry"] = None
    assert scenes.scene_row(bare, "fire", "c", None, url) is None


@pytest.fixture
def conn():
    require_test_db()
    c = psycopg2.connect(DSN)
    init = open(os.path.join(HERE, "..", "db", "init.sql"), encoding="utf-8").read()
    with c, c.cursor() as cur:
        cur.execute(init)
        cur.execute(main.KEYS_SQL)
    _scrub(c)
    yield c
    _scrub(c)
    c.close()


def _scrub(c):
    with c, c.cursor() as cur:
        cur.execute("DELETE FROM scene WHERE event IN ('fire', 'flood')")
        cur.execute("DELETE FROM event WHERE slug IN ('fire', 'flood')")
        cur.execute("DELETE FROM satellite WHERE norad IN (99921, 99922)")


@pytest.mark.skipif(not DSN, reason="no database")
def test_fetch_stores_scenes_linked_to_the_operator_feed_satellite(conn, monkeypatch):
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO satellite (norad, name, note) VALUES "
                    "(99921, 'SKYSAT C1 S3', 'Operator feed ''planet'''), "
                    "(99922, 'PELICAN 2 3009 3009', 'Operator feed ''planet''')")
    monkeypatch.setattr(scenes, "STAC_CATALOGS", "planet=" + ROOT)
    calls = []
    get = _get(_tree(), calls)
    added = scenes.fetch_scenes(lambda: psycopg2.connect(DSN),
                                lambda label, url, **kw: get(url))
    assert added == 4
    with conn.cursor() as cur:
        cur.execute("SELECT slug, title FROM event WHERE slug IN ('fire','flood') ORDER BY 1")
        assert cur.fetchall() == [("fire", "Planet Crisis Response — Big Fire (2026)"),
                                  ("flood", "Planet Crisis Response — Old Flood (2026)")]
        cur.execute("SELECT id, norad, phase FROM scene WHERE event IN ('fire','flood') ORDER BY 1")
        assert cur.fetchall() == [("f1", None, None), ("p1", 99922, "post-event"),
                                  ("p2", 99922, "post-event"), ("s1", 99921, "pre-event")]
    # second cycle: nothing re-fetched but the tree, nothing duplicated
    n = len(calls)
    assert scenes.fetch_scenes(lambda: psycopg2.connect(DSN),
                               lambda label, url, **kw: get(url)) == 0
    assert not any(u.endswith(("s1.json", "p1.json", "p2.json", "f1.json")) for u in calls[n:])


def test_scene_tables_are_open_to_the_public_boards():
    assert "CREATE TABLE IF NOT EXISTS scene" in main.KEYS_SQL
    assert "CREATE TABLE IF NOT EXISTS event" in main.KEYS_SQL
    assert {"scene", "event"} <= set(main.GRAFANA_PUBLIC_TABLES)
    # the public fleet board carries the lag KPI and the events table
    board = json.load(open(os.path.join(HERE, "..", "grafana", "dashboards", "public",
                                        "fleet-overview.json"), encoding="utf-8"))
    sqls = [t["rawSql"] for p in board["panels"] for t in p.get("targets", [])]
    assert any("FROM scene" in s and "published - captured" in s for s in sqls)
    assert any("FROM event" in s for s in sqls)


def test_every_cloud_stack_walks_the_same_catalog():
    line = 'STAC_CATALOGS: "planet-disasters=https://data.source.coop/planet/disasterdata/catalog.json"'
    for stack in ("docker-compose.yml",
                  os.path.join("staging", "docker-compose.yml"),
                  os.path.join("sandbox", "docker-compose.yml")):
        c = open(os.path.join(HERE, "..", stack), encoding="utf-8").read()
        assert line in c, f"{stack} must walk the same catalogs as prod"
    ing = open(os.path.join(HERE, "..", "ingest", "ingest.py"), encoding="utf-8").read()
    assert 'args=(fetch_scenes, scenes.SCENE_INTERVAL, "scenes")' in ing
    assert "scenes.py" in open(os.path.join(HERE, "..", "ingest", "Dockerfile")).read()


def test_the_web_draws_scenes_on_the_open_view_only():
    web = os.path.join(HERE, "..", "web")
    app = open(os.path.join(web, "app.py"), encoding="utf-8").read()
    assert '"/api/events"' in app and '"/api/scenes/<slug>"' in app
    assert "CC-BY-NC-4.0" in app, "the licence travels with the data"
    js = open(os.path.join(web, "static", "app.js"), encoding="utf-8").read()
    assert 'map.addSource("scenes"' in js and "attribution:SCENE_ATTR" in js
    assert 'map.on("click", "scenes"' in js
    # the open-view switch hides the layers and the picker with everything else
    vis = js[js.index("function applyOpenVisibility"):js.index("function", js.index("function applyOpenVisibility") + 10)]
    assert '"scenes","scenes-line"' in vis and 'getElementById("eventbar")' in vis
    html = open(os.path.join(web, "static", "index.html"), encoding="utf-8").read()
    assert 'id="eventbar"' in html
    assert "#eventbar .lbl { display:none; }" in html, "phone: the label goes, the picker stays"
