"""Guards #456: an operator-published 3LE feed (Planet's planet_mc.tle) seeds
its fleet position-only and feeds `elements`, in ONE request per cycle: none
of its members ever takes the per-object lookup path, and a satellite we
curated keeps its name and note. The feed is inline (no network); the
storage tests need a Postgres."""
import os
import sys
from unittest import mock

import psycopg2
import pytest

from conftest import require_test_db

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))
os.environ.setdefault("DB_DSN", "")
import main  # noqa: E402
import ingest  # noqa: E402

DSN = os.environ.get("DB_DSN")
SATS = (99911, 99912)
# Planet's format: "0 NAME" title lines; some line-1s carry PLANET in place
# of the international designator (their own orbit determination).
FEED = """0 FLOCK 4BE 28 24AE
1 99911U PLANET   26254.03123843  .00000000  00000+0  36937-3 0    07
2 99911 097.3710 341.4092 0003048 007.8389 103.3735 15.49768156    07
0 SKYSAT-C1
1 99912U 16038B   26254.03123843  .00000000  00000+0  70913-3 0    07
2 99912 097.3716 338.0216 0001494 327.9611 174.7151 15.43801020    04
"""


def test_feed_spec_is_forgiving():
    assert ingest.operator_feeds("planet=https://ephemerides.planet-labs.com/planet_mc.tle") == \
        [("planet", "https://ephemerides.planet-labs.com/planet_mc.tle")]
    assert ingest.operator_feeds(" A=https://a.example/x.tle , junk, =https://b, c=ftp://d ") == \
        [("a", "https://a.example/x.tle")]
    assert ingest.operator_feeds("") == []


def test_3le_title_lines_lose_their_leading_zero():
    triples = ingest._parse_tle_file(FEED)
    assert [t[0] for t in triples] == ["FLOCK 4BE 28 24AE", "SKYSAT-C1"]
    assert [int(t[1][2:7]) for t in triples] == list(SATS)
    # a plain 2LE and a name without the "0 " prefix still parse as before
    two = "\n".join(FEED.splitlines()[1:3]) + "\n"
    assert ingest._parse_tle_file(two)[0][0] == "NORAD 99911"


@pytest.fixture
def conn():
    require_test_db()
    c = psycopg2.connect(DSN)
    with c, c.cursor() as cur:
        cur.execute(main.KEYS_SQL)
    scrub(c)
    yield c
    scrub(c)
    c.close()


def scrub(c):
    with c, c.cursor() as cur:
        for t in ("position", "elements"):
            cur.execute(f"DELETE FROM {t} WHERE norad = ANY(%s)", (list(SATS),))
        cur.execute("DELETE FROM satellite WHERE norad = ANY(%s)", (list(SATS),))


class _Resp:
    status_code, text = 200, FEED

    def raise_for_status(self):
        pass


def _run_feed(monkeypatch):
    monkeypatch.setattr(ingest, "OPERATOR_TLE_FEEDS", "planet=https://feed.example/p.tle")
    calls = []
    monkeypatch.setattr(ingest.requests, "get", lambda url, **kw: calls.append(url) or _Resp())
    return calls


@pytest.mark.skipif(not DSN, reason="no database")
def test_a_feed_seeds_its_fleet_position_only_with_elements(conn, monkeypatch):
    calls = _run_feed(monkeypatch)
    assert ingest.fetch_operator_feeds() == set(SATS)
    assert calls == ["https://feed.example/p.tle"], "one GET per feed, no per-object call"
    with conn.cursor() as cur:
        cur.execute("SELECT norad, name, has_telemetry, note FROM satellite "
                    "WHERE norad = ANY(%s) ORDER BY 1", (list(SATS),))
        assert cur.fetchall() == [
            (99911, "FLOCK 4BE 28 24AE", False, "Operator feed 'planet'"),
            (99912, "SKYSAT-C1", False, "Operator feed 'planet'")]
        cur.execute("SELECT norad, tle1 FROM elements WHERE norad = ANY(%s) ORDER BY 1",
                    (list(SATS),))
        rows = cur.fetchall()
        assert [r[0] for r in rows] == list(SATS) and "PLANET" in rows[0][1]
    # the same feed again: nothing duplicated, nothing renamed
    ingest.fetch_operator_feeds()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM elements WHERE norad = ANY(%s)", (list(SATS),))
        assert cur.fetchone()[0] == 2


@pytest.mark.skipif(not DSN, reason="no database")
def test_feed_members_never_take_the_per_object_path(conn, monkeypatch):
    # fetch_elements: groups off, one feed on, and the per-object fallback
    # (_tle_for) must not be asked for anything the feed covered
    _run_feed(monkeypatch)
    monkeypatch.setattr(ingest, "CELESTRAK_GROUPS", [])
    asked = []
    monkeypatch.setattr(ingest, "_tle_for", lambda norad: asked.append(norad) or None)
    monkeypatch.setattr(ingest.time, "sleep", lambda s: None)
    ingest.fetch_elements()
    assert not (set(asked) & set(SATS)), asked


@pytest.mark.skipif(not DSN, reason="no database")
def test_a_curated_satellite_keeps_its_identity(conn, monkeypatch):
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO satellite (norad, name, has_telemetry, note) "
                    "VALUES (99912, 'SkySat-C1 (curated)', false, 'EO anchor')")
    _run_feed(monkeypatch)
    ingest.fetch_operator_feeds()
    with conn.cursor() as cur:
        cur.execute("SELECT name, note FROM satellite WHERE norad = 99912")
        assert cur.fetchone() == ("SkySat-C1 (curated)", "EO anchor")
        cur.execute("SELECT count(*) FROM elements WHERE norad = 99912")
        assert cur.fetchone()[0] == 1, "still gets the operator's elements"
