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
