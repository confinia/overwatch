"""Guards issue #230: pick any satellite from the open network.

Telemetry decoding needs a per-satellite decoder — that is why the fleet is
curated. Position tracking does not: CelesTrak has a TLE for anything
catalogued and SGP4 propagates it. So adding an arbitrary satellite means
position tracking, and telemetry only where a decoder already exists. These
tests pin that distinction, and the guardrails around a SHARED tracked set
that anonymous callers can write to.
"""
import os
import sys

import psycopg2
import psycopg2.pool
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402

DSN = os.environ.get("DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="no database")


@pytest.fixture(scope="module")
def db():
    main.pool = psycopg2.pool.SimpleConnectionPool(1, 3, DSN)
    conn = psycopg2.connect(DSN)
    init = open(os.path.join(os.path.dirname(__file__), "..", "db", "init.sql"),
                encoding="utf-8").read()
    with conn, conn.cursor() as cur:
        cur.execute(init)
        cur.execute(main.KEYS_SQL)
        cur.execute("DELETE FROM catalog WHERE norad IN (999001, 999002, 999003)")
        cur.execute("DELETE FROM satellite WHERE norad IN (999001, 999002, 999003)")
        cur.execute("""INSERT INTO catalog (norad, name, sat_id, status, decoder)
                       VALUES (999001, 'TESTSAT ALPHA', 'AAAA-0001', 'alive', NULL),
                              (999002, 'TESTSAT BETA',  'BBBB-0002', 'alive', 'fox'),
                              (999003, 'DEADSAT GAMMA', 'CCCC-0003', 'dead',  NULL)""")
    conn.commit()
    yield conn
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM satellite WHERE norad IN (999001, 999002, 999003)")
        cur.execute("DELETE FROM catalog WHERE norad IN (999001, 999002, 999003)")
    conn.close()
    main.pool.closeall(); main.pool = None


def test_search_finds_catalogue_entries_beyond_the_fleet(db):
    out = main.catalog_search(q="TESTSAT", limit=20)
    names = {r["name"] for r in out}
    assert {"TESTSAT ALPHA", "TESTSAT BETA"} <= names
    assert all(r["tracked"] is False for r in out if r["norad"] in (999001, 999002))


def test_search_by_norad_number(db):
    out = main.catalog_search(q="999002", limit=20)
    assert len(out) == 1 and out[0]["norad"] == 999002
    assert out[0]["telemetry"] is True          # it has a decoder


def test_search_marks_which_have_telemetry(db):
    by_norad = {r["norad"]: r for r in main.catalog_search(q="TESTSAT", limit=20)}
    assert by_norad[999001]["telemetry"] is False   # position-only
    assert by_norad[999002]["telemetry"] is True


def test_tracking_adds_it_and_is_idempotent(db):
    first = main.catalog_track(None, main.TrackRequest(norad=999001))
    assert first["tracked"] is True and first["already"] is False
    assert first["telemetry"] is False
    assert "position tracking only" in first["note"]
    again = main.catalog_track(None, main.TrackRequest(norad=999001))
    assert again["already"] is True
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM satellite WHERE norad = 999001")
        assert cur.fetchone()[0] == 1


def test_tracked_satellite_is_position_only_without_a_decoder(db):
    main.catalog_track(None, main.TrackRequest(norad=999001))
    with db.cursor() as cur:
        cur.execute("SELECT has_telemetry, decoder FROM satellite WHERE norad = 999001")
        has_tlm, decoder = cur.fetchone()
    assert has_tlm is False and decoder is None, \
        "never fabricate telemetry for a satellite with no decoder"


def test_tracking_carries_the_decoder_when_there_is_one(db):
    r = main.catalog_track(None, main.TrackRequest(norad=999002))
    assert r["telemetry"] is True
    with db.cursor() as cur:
        cur.execute("SELECT has_telemetry, decoder FROM satellite WHERE norad = 999002")
        assert cur.fetchone() == (True, "fox")


def test_unknown_norad_is_refused(db):
    """The tracked set is shared and anonymous-writable; only catalogued
    objects may enter it."""
    with pytest.raises(main.HTTPException) as e:
        main.catalog_track(None, main.TrackRequest(norad=42424242))
    assert e.value.status_code == 404


def test_the_tracked_set_is_capped(db, monkeypatch):
    """A shared set that anyone can add to needs a ceiling."""
    monkeypatch.setattr(main, "TRACK_CAP", 1)
    with pytest.raises(main.HTTPException) as e:
        main.catalog_track(None, main.TrackRequest(norad=999003))
    assert e.value.status_code == 429
    assert "full" in e.value.detail


def test_the_picker_is_reachable_from_the_search_box():   # #230
    """The list the user types into must offer the open network beneath the
    tracked fleet — that IS the feature; an endpoint nobody can reach is not."""
    app = open(os.path.join(os.path.dirname(__file__), "..", "web", "static",
                            "app.js"), encoding="utf-8").read()
    assert "renderCatalog" in app and "/api/v1/catalog/search" in app
    assert "/api/v1/catalog/track" in app
    # only search once the query is worth a round trip, and never race
    assert "q.length >= 2" in app
    assert "catalogSeq" in app, "stale responses could overwrite newer ones"
    # already-tracked satellites belong in the list above, not the picker
    assert "filter(h => !h.tracked)" in app


def test_tracking_does_not_block_on_a_third_party():   # #230
    """A request handler must not make a slow external call: CelesTrak was
    unreachable from the VM the first time this ran, costing 15s per add.
    Elements are the ingest's job — it has the token and the SatNOGS
    fallback — and it fills them within a couple of minutes."""
    src = open(os.path.join(os.path.dirname(__file__), "main.py"),
               encoding="utf-8").read()
    track = src[src.index("def catalog_track("):src.index("@app.get(\"/v1/me/satellites\")")]
    assert "celestrak" not in track.lower(), "the request path fetches from a third party"
    assert "requests.get" not in track and "_rq.get" not in track
    ing = open(os.path.join(os.path.dirname(__file__), "..", "ingest", "ingest.py"),
               encoding="utf-8").read()
    assert "def fill_missing_elements" in ing
    assert "_tle_from_celestrak(norad) or _tle_from_satnogs(norad)" in ing, \
        "the fill must keep the SatNOGS fallback"
    assert "elements-fill" in ing, "the fill loop is not scheduled"
