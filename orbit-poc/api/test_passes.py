"""#217: next-pass geometry + SGP4 pass detection (ingest/passes.py).

Pure math + sgp4, no DB. Skipped where sgp4 isn't installed (the VM api gate);
CI installs sgp4 so it runs there.
"""
import math
import os
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("sgp4")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingest"))
import passes  # noqa: E402


def test_station_ecef_cardinal_points():
    x, y, z = passes.station_ecef(0, 0)
    assert abs(x - passes.R_EARTH) < 1e-6 and abs(y) < 1e-6 and abs(z) < 1e-6
    x, y, z = passes.station_ecef(90, 0)
    assert abs(z - passes.R_EARTH) < 1e-6 and abs(x) < 1e-6 and abs(y) < 1e-6


def test_elevation_overhead_is_90_and_slant_is_between():
    st = passes.station_ecef(0, 0)
    overhead = (passes.R_EARTH + 400, 0, 0)                 # straight up
    assert abs(passes.elevation_deg(overhead, st) - 90) < 1e-6
    # 10 deg of geocentric arc is inside the ~19.8 deg horizon for a 400 km
    # satellite, so the point is above the horizon (elevation ~14 deg).
    slant = (math.cos(math.radians(10)) * (passes.R_EARTH + 400),
             math.sin(math.radians(10)) * (passes.R_EARTH + 400), 0)
    assert 0 < passes.elevation_deg(slant, st) < 90         # off-zenith, still up


def test_finds_iss_passes_over_a_mid_latitude_station():
    # Canonical ISS TLE (python-sgp4 reference, guaranteed to parse). Over 48 h
    # the ground track sweeps all longitudes at +-51.6 deg, so a mid-latitude
    # station gets several passes.
    tle1 = "1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927"
    tle2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537"
    start = datetime(2008, 9, 20, 12, 0, 0, tzinfo=timezone.utc)   # near epoch
    ps = passes.find_passes(tle1, tle2, 45.0, 0.0, start, hours=48, min_el=10.0)
    assert ps, "expected at least one ISS pass over (45N, 0E) in 48 h"
    for aos, los, max_el in ps:
        assert aos < los                    # rise before set
        assert aos >= start                 # in the future
        assert max_el >= 10.0               # actually cleared the horizon threshold
        assert (los - aos).total_seconds() < 30 * 60   # a LEO pass is minutes, not hours


# --- persistence: the recompute must be idempotent (#232) ---
import psycopg2  # noqa: E402
from datetime import timedelta  # noqa: E402


@pytest.fixture(scope="module")
def conn():
    c = psycopg2.connect(os.environ["DB_DSN"])
    with c, c.cursor() as cur:
        cur.execute("INSERT INTO satellite (norad, name) VALUES (99992, 'PASS-TEST') "
                    "ON CONFLICT (norad) DO NOTHING")
        cur.execute("""CREATE TABLE IF NOT EXISTS pass (
            observer TEXT NOT NULL, norad INTEGER REFERENCES satellite(norad),
            aos TIMESTAMPTZ NOT NULL, los TIMESTAMPTZ NOT NULL,
            max_el_deg DOUBLE PRECISION, PRIMARY KEY (observer, norad, aos))""")
    yield c
    with c, c.cursor() as cur:
        cur.execute("DELETE FROM pass WHERE observer = 'TEST-STATION'")
    c.close()


def _near_duplicates(cur):
    """Rows for the same station+satellite whose AOS are within a minute — i.e.
    the same physical pass stored more than once."""
    cur.execute("""
        SELECT count(*) FROM (
            SELECT observer, norad, aos,
                   lag(aos) OVER (PARTITION BY observer, norad ORDER BY aos) AS prev
            FROM pass WHERE observer = 'TEST-STATION') t
        WHERE prev IS NOT NULL AND aos - prev < interval '1 minute'""")
    return cur.fetchone()[0]


def test_recompute_is_idempotent_and_does_not_duplicate(conn):
    """Re-running the compute must not accumulate copies of the same pass. Each
    run's AOS is jittered by a fraction of a second (as the real forward scan
    does), which used to defeat the exact-timestamp upsert and duplicate rows."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingest"))
    import passes as _p
    base = datetime.now(timezone.utc) + timedelta(hours=2)
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pass WHERE observer = 'TEST-STATION'")
        for jitter in (0.0, 0.4, 0.9):          # three recomputes, drifting AOS
            rows = [("TEST-STATION", 99992,
                     base + timedelta(seconds=jitter + 600 * k),
                     base + timedelta(seconds=jitter + 600 * k + 300), 20.0 + k)
                    for k in range(3)]          # three distinct passes each run
            _p.store_passes(cur, ["TEST-STATION"], rows)
        cur.execute("SELECT count(*) FROM pass WHERE observer = 'TEST-STATION'")
        assert cur.fetchone()[0] == 3, "recompute duplicated passes"
        assert _near_duplicates(cur) == 0, "same pass stored more than once"


def test_store_passes_prunes_past_passes(conn):
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingest"))
    import passes as _p
    past = datetime.now(timezone.utc) - timedelta(hours=3)
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pass WHERE observer = 'TEST-STATION'")
        cur.execute("INSERT INTO pass (observer, norad, aos, los, max_el_deg) "
                    "VALUES ('TEST-STATION', 99992, %s, %s, 10)",
                    (past, past + timedelta(minutes=5)))
        _p.store_passes(cur, ["TEST-STATION"], [])
        cur.execute("SELECT count(*) FROM pass WHERE observer = 'TEST-STATION'")
        assert cur.fetchone()[0] == 0            # past pass pruned
