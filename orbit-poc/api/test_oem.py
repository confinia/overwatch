"""#208 Phase 1: CCSDS OEM parsing + Earth-fixed -> geodetic. Pure, no DB."""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import oem  # noqa: E402

# Three ECEF points with hand-checkable geodetic answers (WGS84):
#  (A+400, 0, 0)      -> equator, prime meridian, 400 km
#  (0, A+400, 0)      -> equator, 90 deg E, 400 km
#  (0, 0, b+400)      -> north pole, 400 km   (b = A*sqrt(1-e2) = 6356.752 km)
OEM_ITRF = """CCSDS_OEM_VERS = 2.0
CREATION_DATE = 2026-08-14T00:00:00
ORIGINATOR = OVERWATCH_TEST

META_START
OBJECT_NAME = TESTSAT
OBJECT_ID = 2026-001A
CENTER_NAME = EARTH
REF_FRAME = ITRF2014
TIME_SYSTEM = UTC
START_TIME = 2026-08-14T00:00:00.000
STOP_TIME = 2026-08-14T00:02:00.000
META_STOP
2026-08-14T00:00:00.000  6778.137 0.0 0.0  1.0 2.0 3.0
2026-08-14T00:01:00.000  0.0 6778.137 0.0  0.0 0.0 0.0
2026-08-14T00:02:00.000  0.0 0.0 6756.752  0.0 0.0 0.0
"""


def test_parses_earth_fixed_and_maps_to_geodetic():
    obj, recs = oem.positions_from_oem(OEM_ITRF)
    assert obj == "2026-001A"
    assert len(recs) == 3
    (t0, la0, lo0, al0), (t1, la1, lo1, al1), (t2, la2, lo2, al2) = recs
    assert abs(la0) < 1e-6 and abs(lo0) < 1e-6 and abs(al0 - 400) < 1e-3
    assert abs(la1) < 1e-6 and abs(lo1 - 90) < 1e-6 and abs(al1 - 400) < 1e-3
    assert abs(la2 - 90) < 1e-6 and abs(al2 - 400) < 1e-2      # pole
    assert t1 > t0 and t0.tzinfo is not None                  # sorted, tz-aware UTC


def test_ecef_to_geodetic_equator_and_pole():
    lat, lon, alt = oem.ecef_to_geodetic(6778.137, 0.0, 0.0)
    assert abs(lat) < 1e-9 and abs(lon) < 1e-9 and abs(alt - 400) < 1e-6
    lat, _, alt = oem.ecef_to_geodetic(0.0, 0.0, -6756.752)   # south pole
    assert abs(lat + 90) < 1e-9 and abs(alt - 400) < 1e-2


def test_inertial_frame_rejected_with_clear_message():
    bad = OEM_ITRF.replace("ITRF2014", "EME2000")
    with pytest.raises(oem.OemError) as e:
        oem.parse_oem(bad)
    msg = str(e.value).lower()
    assert "inertial" in msg and "1b" in msg


def test_non_utc_time_system_rejected():
    bad = OEM_ITRF.replace("TIME_SYSTEM = UTC", "TIME_SYSTEM = TAI")
    with pytest.raises(oem.OemError):
        oem.parse_oem(bad)


def test_not_an_oem_rejected():
    with pytest.raises(oem.OemError):
        oem.parse_oem("hello world\nnot an ephemeris")


def test_missing_meta_stop_rejected():
    bad = OEM_ITRF.replace("META_STOP\n", "")
    with pytest.raises(oem.OemError):
        oem.parse_oem(bad)


def test_bad_state_vector_rejected():
    bad = OEM_ITRF.replace("6778.137 0.0 0.0", "6778.137 oops 0.0")
    with pytest.raises(oem.OemError):
        oem.parse_oem(bad)


def test_day_of_year_epoch_form():
    # 2026-08-14 is day-of-year 226; the DOY form must parse to the same date.
    doy = OEM_ITRF.replace("2026-08-14T00:00:00.000  6778.137",
                           "2026-226T00:00:00.000  6778.137")
    _, recs = oem.positions_from_oem(doy)
    assert any(r[0].month == 8 and r[0].day == 14 for r in recs)
