"""Next-pass prediction (#217).

Forward-propagate a satellite with SGP4 and, for a ground station, find the
upcoming rise->set windows where its topocentric elevation clears a threshold.
Pure geometry on a spherical Earth (R = 6371 km), consistent with the position
loop in ingest.py — plenty for scheduling (AOS/LOS at ~10 deg). Stdlib `math` +
`sgp4` only; no external service, no numpy, so the geometry is unit-testable on
its own.
"""
import math
from datetime import timedelta

from sgp4.api import Satrec, jday

R_EARTH = 6371.0  # km, spherical (matches ingest._teme_to_geodetic)


def _gmst_rad(jd, fr):
    """Greenwich Mean Sidereal Time (rad) — the same low-precision formula
    ingest uses to rotate TEME -> ECEF."""
    d = jd + fr - 2451545.0
    t = d / 36525.0
    return math.radians((280.46061837 + 360.98564736629 * d
                         + 0.000387933 * t * t) % 360.0)


def teme_to_ecef(x, y, z, jd, fr):
    """Rotate a TEME position (km) into Earth-fixed ECEF by GMST."""
    g = _gmst_rad(jd, fr)
    c, s = math.cos(g), math.sin(g)
    return (c * x + s * y, -s * x + c * y, z)


def station_ecef(lat_deg, lon_deg):
    """Geocentric ECEF (km) of a ground station on the spherical Earth."""
    la, lo = math.radians(lat_deg), math.radians(lon_deg)
    return (R_EARTH * math.cos(la) * math.cos(lo),
            R_EARTH * math.cos(la) * math.sin(lo),
            R_EARTH * math.sin(la))


def elevation_deg(sat_ecef, stat_ecef):
    """Topocentric elevation (deg) of a satellite (ECEF km) from a station
    (ECEF km): the line-of-sight angle above the station's local horizon."""
    sx, sy, sz = stat_ecef
    sn = math.sqrt(sx * sx + sy * sy + sz * sz)
    ux, uy, uz = sx / sn, sy / sn, sz / sn                 # local zenith (radial)
    rx, ry, rz = sat_ecef[0] - sx, sat_ecef[1] - sy, sat_ecef[2] - sz
    rn = math.sqrt(rx * rx + ry * ry + rz * rz)
    if rn == 0:
        return 90.0
    sin_el = (rx * ux + ry * uy + rz * uz) / rn
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))


def _elev_at(sat, stat_ecef, dt):
    jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute,
                  dt.second + dt.microsecond * 1e-6)
    err, r, _ = sat.sgp4(jd, fr)
    if err != 0:
        return None
    return elevation_deg(teme_to_ecef(r[0], r[1], r[2], jd, fr), stat_ecef)


def _interp(t0, e0, t1, e1, target):
    """Linear time of the elevation == target crossing between two samples."""
    if e1 == e0:
        return t0
    return t0 + (t1 - t0) * ((target - e0) / (e1 - e0))


def find_passes(tle1, tle2, lat_deg, lon_deg, start, hours=168,
                step_s=60, min_el=10.0):
    """Upcoming (aos, los, max_el_deg) windows for one satellite over one
    station, from `start` for `hours`, where elevation >= `min_el`. AOS/LOS are
    linearly interpolated across the crossing; max_el is the sampled peak.
    step_s of 60 s catches every LEO pass (they last minutes) while keeping the
    forward scan cheap."""
    sat = Satrec.twoline2rv(tle1, tle2)
    stat = station_ecef(lat_deg, lon_deg)
    out = []
    n = int(hours * 3600 / step_s)
    prev_el = prev_t = None
    aos = None
    peak = -90.0
    for i in range(n + 1):
        t = start + timedelta(seconds=i * step_s)
        el = _elev_at(sat, stat, t)
        if el is None:                       # SGP4 error (decayed / bad TLE)
            prev_el = prev_t = None
            continue
        if prev_el is not None:
            if prev_el < min_el <= el:                        # rising -> AOS
                aos = _interp(prev_t, prev_el, t, el, min_el)
                peak = el
            elif prev_el >= min_el > el and aos is not None:  # setting -> LOS
                los = _interp(prev_t, prev_el, t, el, min_el)
                out.append((aos, los, round(peak, 1)))
                aos, peak = None, -90.0
            elif aos is not None:
                peak = max(peak, el)
        prev_el, prev_t = el, t
    return out


def store_passes(cur, rows):
    """Persist a recompute idempotently (#232). Caller commits.

    The forward scan starts at "now", so each run samples on a different grid
    and the interpolated AOS of the SAME physical pass shifts by a fraction of a
    second. Upserting on the exact `(observer, norad, aos)` key therefore
    INSERTS a near-duplicate every run instead of updating — three runs, three
    rows for one pass. A pass's identity is "this station, this satellite, this
    rise", not a millisecond, so we take a clean slate instead: drop the future
    rows of the stations we just recomputed, then insert this run's answer.
    Delete+insert share one transaction, so readers never see an empty table.
    """
    from psycopg2.extras import execute_values
    # Full clean slate: a run recomputes the complete horizon for every tracked
    # station, so the fresh rows ARE the whole truth. Scoping the delete to the
    # recomputed stations instead left passes behind for stations that dropped
    # out of the tracked set — stale windows that still showed in the dashboards
    # and its station picker (#232).
    # FUTURE rows only, as the paragraph above says — the code used to clear
    # the whole table, so completed passes survived exactly until the next
    # recompute and the per-pass history (#366) was wiped every cycle. The
    # forward scan can only produce future AOS times (a pass already in
    # progress has no rising crossing to find), so this is the precise clean
    # slate. Completed passes are kept 30 days, matching the opportunity
    # window; unbounded would grow forever.
    cur.execute("DELETE FROM pass WHERE aos > now()")
    cur.execute("DELETE FROM pass WHERE los < now() - interval '30 days'")
    if rows:
        execute_values(cur, "INSERT INTO pass "
                            "(observer, norad, aos, los, max_el_deg) VALUES %s "
                            "ON CONFLICT (observer, norad, aos) DO NOTHING", rows)
