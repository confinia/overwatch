"""CCSDS OEM (Orbit Ephemeris Message) parsing — #208 Phase 1.

Turns a user-supplied precise ephemeris — the standard file operators exchange
orbits in — into the same (ts, lat, lon, alt_km) rows the TLE propagator writes
(orbit-poc/ingest/ingest.py -> position), so a precise track plots on the globe
exactly like a public one — only more accurate, and correct across a maneuver
where a TLE goes stale.

Phase 1 scope, deliberately narrow so it ships and is verifiable:
  - KVN text (the common OEM form), not XML;
  - Earth-fixed reference frames only (ITRF/ECEF) — the state is already in a
    rotating Earth frame, so it maps to geodetic coordinates with no
    time-dependent rotation. Inertial frames (EME2000/GCRF/...) need an
    Earth-orientation transform and are deferred to Phase 1b: rejected here with
    a clear message rather than silently mis-plotted;
  - UTC time system;
  - position only (velocity columns are parsed-past but unused).

Coordinates use the WGS84 ellipsoid — the correct geodetic model for lon/lat on
the map. (The TLE path in ingest.py uses a spherical approximation; the precise
OEM path deserves the ellipsoid.)
"""
import math
import re
from datetime import datetime, timedelta, timezone

# --- WGS84 ---
_A_KM = 6378.137                      # semi-major axis (km)
_F = 1.0 / 298.257223563              # flattening
_E2 = _F * (2.0 - _F)                 # first eccentricity squared

# Rotating, Earth-fixed frames: the state is already ECEF, no rotation needed.
# Names are normalised (upper-cased, separators stripped) before lookup.
_EARTH_FIXED = {"ITRF", "ITRF93", "ITRF97", "ITRF2000", "ITRF2005", "ITRF2008",
                "ITRF2014", "ITRF2020", "GRGS", "ECEF", "ECF", "GTOD", "TDR",
                "ITRF199", "ITRFECEF"}
# Inertial frames need an Earth-orientation transform — deferred to Phase 1b.
_INERTIAL = {"EME2000", "J2000", "GCRF", "ICRF", "MOD", "TOD", "TEME", "TNW",
             "RTN", "QSW"}


class OemError(ValueError):
    """A malformed OEM, or one outside Phase 1 scope (raised with a clear why)."""


def _norm(name: str) -> str:
    return re.sub(r"[-_ ]", "", name.strip().upper())


def ecef_to_geodetic(x: float, y: float, z: float):
    """WGS84 ECEF (km) -> (lat_deg, lon_deg, alt_km), Bowring iteration."""
    lon = math.degrees(math.atan2(y, x))
    lon = ((lon + 180.0) % 360.0) - 180.0
    p = math.hypot(x, y)
    if p < 1e-9:                          # on the spin axis (poles)
        n = _A_KM / math.sqrt(1.0 - _E2)  # sin^2(lat) = 1
        return math.copysign(90.0, z), lon, abs(z) - n * (1.0 - _E2)
    lat = math.atan2(z, p * (1.0 - _E2))
    for _ in range(6):                    # converges in ~2-3 for near-Earth orbits
        s = math.sin(lat)
        n = _A_KM / math.sqrt(1.0 - _E2 * s * s)
        alt = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1.0 - _E2 * n / (n + alt)))
    s = math.sin(lat)
    n = _A_KM / math.sqrt(1.0 - _E2 * s * s)
    alt = p / math.cos(lat) - n
    return math.degrees(lat), lon, alt


def _parse_epoch(tok: str) -> datetime:
    """CCSDS calendar or day-of-year epoch -> tz-aware UTC datetime."""
    tok = tok.strip().rstrip("Zz")
    m = re.match(r"^(\d{4})-(\d{3})T(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)$", tok)
    if m:                                 # YYYY-DDDThh:mm:ss[.f] (day of year)
        year, doy, hh, mm, ss = m.groups()
        base = datetime(int(year), 1, 1, tzinfo=timezone.utc) + timedelta(days=int(doy) - 1)
        return base + timedelta(hours=int(hh), minutes=int(mm), seconds=float(ss))
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(tok, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise OemError(f"unrecognised epoch: {tok!r}")


def _check_meta(meta: dict) -> None:
    frame_raw = meta.get("REF_FRAME")
    if not frame_raw:
        raise OemError("segment missing REF_FRAME")
    frame = _norm(frame_raw)
    if frame in _INERTIAL:
        raise OemError(f"inertial frame {frame_raw!r} not supported yet "
                       f"(#208 Phase 1b); Phase 1 handles Earth-fixed frames")
    if frame not in _EARTH_FIXED:
        raise OemError(f"unknown/unsupported REF_FRAME {frame_raw!r}")
    ts = _norm(meta.get("TIME_SYSTEM", ""))
    if ts != "UTC":
        raise OemError(f"TIME_SYSTEM {meta.get('TIME_SYSTEM') or '(missing)'!r} "
                       f"not supported yet; Phase 1 requires UTC")
    center = _norm(meta.get("CENTER_NAME", "EARTH"))
    if center != "EARTH":
        raise OemError(f"CENTER_NAME {meta.get('CENTER_NAME')!r} not supported "
                       f"(Earth-centred only)")


def parse_oem(text: str) -> dict:
    """Parse a CCSDS OEM (KVN). Returns
       {version, originator, segments: [{metadata: {...},
                                         records: [(dt_utc, lat, lon, alt_km), ...]}]}
    Raises OemError on malformed input or anything outside Phase 1 scope."""
    if "CCSDS_OEM_VERS" not in text:
        raise OemError("not a CCSDS OEM (missing CCSDS_OEM_VERS)")
    lines = [ln.strip() for ln in text.splitlines()]
    n = len(lines)
    header: dict = {}
    i = 0
    while i < n and lines[i] != "META_START":       # file header
        ln = lines[i]; i += 1
        if ln and not ln.startswith("COMMENT") and "=" in ln:
            k, v = ln.split("=", 1)
            header[k.strip()] = v.strip()
    segments = []
    while i < n:
        if lines[i] != "META_START":
            i += 1
            continue
        i += 1
        meta: dict = {}
        while i < n and lines[i] != "META_STOP":
            ln = lines[i]; i += 1
            if ln and not ln.startswith("COMMENT") and "=" in ln:
                k, v = ln.split("=", 1)
                meta[k.strip()] = v.strip()
        if i >= n:
            raise OemError("META_START without META_STOP")
        i += 1                                       # consume META_STOP
        _check_meta(meta)
        records = []
        while i < n and lines[i] != "META_START":
            ln = lines[i]; i += 1
            if not ln or ln.startswith("COMMENT"):
                continue
            parts = ln.split()
            if len(parts) < 4:
                raise OemError(f"bad ephemeris line: {ln!r}")
            dt = _parse_epoch(parts[0])
            try:
                x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
            except ValueError:
                raise OemError(f"bad state vector: {ln!r}")
            lat, lon, alt = ecef_to_geodetic(x, y, z)
            records.append((dt, lat, lon, alt))
        if not records:
            raise OemError("ephemeris segment has no state vectors")
        segments.append({"metadata": meta, "records": records})
    if not segments:
        raise OemError("no ephemeris segment (missing META_START)")
    return {"version": header.get("CCSDS_OEM_VERS"),
            "originator": header.get("ORIGINATOR"),
            "segments": segments}


def positions_from_oem(text: str):
    """Convenience for the (Phase 1c) ingest path: parse and return
    (object_id, [(dt_utc, lat, lon, alt_km), ...]) sorted across all segments,
    ready to upsert into `position`."""
    oem = parse_oem(text)
    meta0 = oem["segments"][0]["metadata"]
    obj = meta0.get("OBJECT_ID") or meta0.get("OBJECT_NAME")
    recs = sorted((r for seg in oem["segments"] for r in seg["records"]),
                  key=lambda r: r[0])
    return obj, recs
