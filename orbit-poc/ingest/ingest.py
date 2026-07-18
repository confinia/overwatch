"""
Ingest / cache service -- the heart of the POC's architecture.

RESPONSIBILITY: it is the ONLY component allowed to talk to upstream APIs.
It fetches on sane schedules, caches locally, and propagates positions in-process.
MapLibre and Grafana never touch CelesTrak or SatNOGS directly.

Why this matters (learned from the research, not invented):
  * CelesTrak firewalls IPs pulling >100 MB/day and asks you to download data
    once per update, not per view. Elements update a few times daily.
  * SatNOGS telemetry updates only when a volunteer ground station hears a pass,
    so polling it fast is pointless and rude.

Cadences (env-overridable):
  ELEMENTS_INTERVAL  = 6h   (orbital elements)
  POSITION_INTERVAL  = 15s  (local SGP4 propagation -> position table)
  TELEMETRY_INTERVAL = 30m  (decoded frames)

Graceful degradation: no SatNOGS token => telemetry step is skipped with a
clear log line; the map + orbit half runs fully without any account.
"""

import os
import time
import logging
import importlib
import threading
from datetime import datetime, timezone, timedelta

import requests
import psycopg2
from psycopg2.extras import execute_values
from sgp4.api import Satrec, jday
import numpy as np

from satellites import SHOWCASE

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest")

DB_DSN            = os.environ["DB_DSN"]
SATNOGS_TOKEN     = os.environ.get("SATNOGS_TOKEN", "").strip()
CELESTRAK_BASE    = "https://celestrak.org/NORAD/elements/gp.php"
SATNOGS_BASE      = "https://db.satnogs.org/api"

ELEMENTS_INTERVAL  = int(os.environ.get("ELEMENTS_INTERVAL",  6 * 3600))
POSITION_INTERVAL  = int(os.environ.get("POSITION_INTERVAL",  15))
TELEMETRY_INTERVAL = int(os.environ.get("TELEMETRY_INTERVAL", 30 * 60))

# Be a good citizen: identify ourselves.
UA = {"User-Agent": "orbit-poc/0.1 (educational; contact: you@example.org)"}


def db():
    return psycopg2.connect(DB_DSN)


# --------------------------------------------------------------------------
# Startup: register showcase satellites, resolve SatNOGS sat_ids by norad id.
# --------------------------------------------------------------------------
def seed_satellites():
    with db() as conn, conn.cursor() as cur:
        for s in SHOWCASE:
            sat_id = None
            if s["telemetry"]:
                sat_id = resolve_sat_id(s["norad"])
                if sat_id is None:
                    log.warning("No SatNOGS sat_id for %s (%s); "
                                "keeping as position-only.", s["name"], s["norad"])
            cur.execute(
                """INSERT INTO satellite (norad, name, sat_id, has_telemetry, decoder, note)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (norad) DO UPDATE SET
                     name=EXCLUDED.name, sat_id=EXCLUDED.sat_id,
                     has_telemetry=EXCLUDED.has_telemetry,
                     decoder=EXCLUDED.decoder, note=EXCLUDED.note""",
                (s["norad"], s["name"], sat_id,
                 bool(sat_id) and s["telemetry"], s.get("decoder"), s["note"]))
        conn.commit()
    log.info("Seeded %d showcase satellites.", len(SHOWCASE))


def resolve_sat_id(norad):
    """Look up a SatNOGS sat_id by norad id. /api/satellites/ needs no key."""
    try:
        r = requests.get(f"{SATNOGS_BASE}/satellites/",
                         params={"norad_cat_id": norad}, headers=UA, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data:
            return data[0].get("sat_id")
    except Exception as e:
        log.warning("sat_id lookup failed for %s: %s", norad, e)
    return None


# --------------------------------------------------------------------------
# Elements: bulk GROUP fetches from CelesTrak (one request per group per 6h
# cadence — the polite pattern; per-view fetching gets IPs firewalled).
# Groups seed the satellite table too: every member becomes a position-only
# entry unless the curated showcase already claims it (decoder, note...).
# Showcase norads not present in any group fall back to one per-CATNR fetch.
# --------------------------------------------------------------------------
CELESTRAK_GROUPS = [g.strip() for g in
                    os.environ.get("CELESTRAK_GROUPS", "amateur,stations").split(",")
                    if g.strip()]


def fetch_elements():
    seen = set()
    for group in CELESTRAK_GROUPS:
        try:
            r = requests.get(CELESTRAK_BASE,
                             params={"GROUP": group, "FORMAT": "TLE"},
                             headers=UA, timeout=60)
            r.raise_for_status()
            triples = _parse_tle_file(r.text)
            with db() as conn, conn.cursor() as cur:
                for name, tle1, tle2 in triples:
                    norad = int(tle1[2:7])
                    seen.add(norad)
                    cur.execute(
                        """INSERT INTO satellite (norad, name, has_telemetry, note)
                           VALUES (%s,%s,false,%s)
                           ON CONFLICT (norad) DO NOTHING""",
                        (norad, name, f"CelesTrak group '{group}'"))
                    cur.execute(
                        """INSERT INTO elements (norad, epoch, tle1, tle2)
                           VALUES (%s,%s,%s,%s)
                           ON CONFLICT (norad, epoch) DO NOTHING""",
                        (norad, _epoch_from_tle(tle1), tle1, tle2))
                conn.commit()
            log.info("Elements: group '%s' -> %d satellites", group, len(triples))
        except Exception as e:
            log.warning("Element group fetch failed for '%s': %s", group, e)
        time.sleep(2)

    # showcase satellites not covered by any group (e.g. EO / GNSS anchors)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT norad FROM satellite WHERE note NOT LIKE 'CelesTrak group%%'")
        rest = [r[0] for r in cur.fetchall() if r[0] not in seen]
    for norad in rest:
        try:
            r = requests.get(CELESTRAK_BASE,
                             params={"CATNR": norad, "FORMAT": "TLE"},
                             headers=UA, timeout=30)
            r.raise_for_status()
            lines = [ln for ln in r.text.strip().splitlines() if ln.strip()]
            if len(lines) < 2:
                log.warning("No elements returned for %s", norad)
                continue
            tle1, tle2 = lines[-2], lines[-1]
            with db() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO elements (norad, epoch, tle1, tle2)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (norad, epoch) DO NOTHING""",
                    (norad, _epoch_from_tle(tle1), tle1, tle2))
                conn.commit()
            log.info("Elements updated: %s", norad)
        except Exception as e:
            log.warning("Element fetch failed for %s: %s", norad, e)
        time.sleep(1)


def _parse_tle_file(text):
    """Parse a 3-line-element file into (name, tle1, tle2) triples."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out, i = [], 0
    while i + 2 < len(lines) + 1:
        if lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            out.append((f"NORAD {lines[i][2:7].strip()}", lines[i], lines[i + 1]))
            i += 2
        elif i + 2 < len(lines) and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            out.append((lines[i].strip(), lines[i + 1], lines[i + 2]))
            i += 3
        else:
            i += 1
    return out


def _epoch_from_tle(tle1):
    """Parse epoch (YYDDD.frac) from TLE line 1 into a UTC timestamp."""
    yy = int(tle1[18:20]); day = float(tle1[20:32])
    year = 2000 + yy if yy < 57 else 1900 + yy
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1)


# --------------------------------------------------------------------------
# Positions: propagate latest elements with SGP4, in-process, every 15s.
# This is the ONLY high-frequency loop and it touches NO external service.
# --------------------------------------------------------------------------
def propagate_positions():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (norad) norad, tle1, tle2
            FROM elements ORDER BY norad, epoch DESC""")
        rows = cur.fetchall()

    now = datetime.now(timezone.utc)
    jd, fr = jday(now.year, now.month, now.day,
                  now.hour, now.minute, now.second + now.microsecond * 1e-6)
    out = []
    for norad, tle1, tle2 in rows:
        try:
            sat = Satrec.twoline2rv(tle1, tle2)
            e, r_teme, _ = sat.sgp4(jd, fr)
            if e != 0:
                continue
            lat, lon, alt = _teme_to_geodetic(r_teme, jd, fr)
            out.append((norad, now, lat, lon, alt))
        except Exception as ex:
            log.debug("propagation failed %s: %s", norad, ex)

    if out:
        with db() as conn, conn.cursor() as cur:
            execute_values(cur,
                """INSERT INTO position (norad, ts, lat, lon, alt_km)
                   VALUES %s ON CONFLICT DO NOTHING""", out)
            # prune: keep 6h rolling window
            cur.execute("DELETE FROM position WHERE ts < now() - interval '6 hours'")
            conn.commit()


def _teme_to_geodetic(r_teme, jd, fr):
    """
    Convert TEME position (km) to lat/lon/alt.
    Simplified: rotate TEME->ECEF by GMST, then geodetic on a sphere-ish Earth.
    Good enough for a map POC; swap in astropy for precision later.
    """
    x, y, z = r_teme
    # Greenwich Mean Sidereal Time (radians), low-precision formula.
    t = (jd + fr - 2451545.0) / 36525.0
    gmst = (280.46061837 + 360.98564736629 * (jd + fr - 2451545.0)
            + 0.000387933 * t * t) % 360.0
    g = np.radians(gmst)
    xe =  np.cos(g) * x + np.sin(g) * y
    ye = -np.sin(g) * x + np.cos(g) * y
    ze = z
    lon = np.degrees(np.arctan2(ye, xe))
    hyp = np.sqrt(xe * xe + ye * ye)
    lat = np.degrees(np.arctan2(ze, hyp))
    R_EARTH = 6371.0
    alt = np.sqrt(xe*xe + ye*ye + ze*ze) - R_EARTH
    # normalise lon to [-180,180]
    lon = ((lon + 180) % 360) - 180
    # plain floats: psycopg2 cannot adapt numpy scalars (np.float64 renders
    # as "np.float64(...)" in SQL -> InvalidSchemaName "np")
    return float(lat), float(lon), float(alt)


# --------------------------------------------------------------------------
# Telemetry: decoded frames from SatNOGS (needs API token). Skipped cleanly
# if no token is provided -> the position-only demo still runs.
# --------------------------------------------------------------------------
def fetch_telemetry():
    if not SATNOGS_TOKEN:
        log.info("No SATNOGS_TOKEN set -> skipping telemetry ingest "
                 "(map + orbits still work). Add a free token to light up "
                 "the health dashboards.")
        return

    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT norad, sat_id, decoder FROM satellite "
                    "WHERE has_telemetry AND sat_id IS NOT NULL")
        targets = cur.fetchall()

    for norad, sat_id, decoder in targets:
        try:
            frames = _get_frames(sat_id)
            if frames is None:
                return  # token invalid — logged in _get_frames
            n = _store_frames(norad, frames, decoder)
            log.info("Telemetry: %d/%d frames decoded+stored for %s",
                     n, len(frames), norad)
        except Exception as e:
            log.warning("Telemetry fetch failed for %s: %s", norad, e)
        time.sleep(5)  # stay well under SatNOGS rate limits


def _get_frames(sat_id, pages=2):
    """Fetch recent frames. The endpoint is cursor-paginated since 2026
    ({next, previous, results}); older deployments returned a bare list.
    Honors 429 Retry-After — SatNOGS throttles aggressively."""
    headers = dict(UA); headers["Authorization"] = f"Token {SATNOGS_TOKEN}"
    frames, url, params = [], f"{SATNOGS_BASE}/telemetry/", {"sat_id": sat_id}
    for _ in range(pages):
        for attempt in range(4):
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 401:
                log.warning("SatNOGS 401 -> token invalid/expired; skipping telemetry.")
                return None
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 15)) + 1
                log.info("SatNOGS 429 — backing off %ss", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        data = r.json()
        if isinstance(data, dict):
            frames += data.get("results", [])
            url, params = data.get("next"), None
            if not url:
                break
        else:
            frames += data
            break
    return frames


def _decode_frame(decoder, frame_hex):
    """Decode a raw frame LOCALLY with satnogs-decoders (kaitai structs) and
    flatten numeric leaves. SatNOGS stopped inlining decoded values in the API
    (they live in their InfluxDB), so sovereign local decoding is the way."""
    mod = importlib.import_module(f"satnogsdecoders.decoder.{decoder}")
    cls = getattr(mod, decoder.capitalize())
    obj = cls.from_bytes(bytes.fromhex(frame_hex))
    out = {}

    def flat(o, prefix="", depth=0):
        if depth > 4:
            return
        for a in dir(o):
            if a.startswith("_"):
                continue
            try:
                v = getattr(o, a)
            except Exception:
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[prefix + a] = v
            elif hasattr(v, "__class__") and \
                    v.__class__.__module__.startswith("satnogsdecoders"):
                flat(v, prefix + a + "_", depth + 1)

    flat(obj)
    return out


def _store_frames(norad, frames, decoder):
    """Turn frames into (field, value_num) rows. Preferred path: local kaitai
    decode of the raw hex. Fallback: inline decoded dicts (legacy API shape)."""
    rows, decoded_n = [], 0
    horizon = datetime.now(timezone.utc) + timedelta(hours=1)
    for f in frames[:200]:  # cap per cycle
        ts = f.get("timestamp") or f.get("time")
        if not ts:
            continue
        try:
            # guard: volunteer stations sometimes upload future-dated frames
            if datetime.fromisoformat(ts.replace("Z", "+00:00")) > horizon:
                continue
        except ValueError:
            continue
        fields = {}
        if decoder and f.get("frame"):
            try:
                fields = _decode_frame(decoder, f["frame"])
            except Exception:
                pass  # frame type not covered by the decoder — normal
        if not fields:
            legacy = f.get("decoded") or f.get("fields") or {}
            fields = dict(_flatten(legacy)) if isinstance(legacy, dict) else {}
        if not fields:
            continue
        decoded_n += 1
        for k, v in fields.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                rows.append((norad, ts, k, float(v), None))
            elif isinstance(v, str):
                rows.append((norad, ts, k, None, v))
    if rows:
        with db() as conn, conn.cursor() as cur:
            execute_values(cur,
                """INSERT INTO telemetry (norad, ts, field, value_num, value_txt)
                   VALUES %s ON CONFLICT DO NOTHING""", rows)
            conn.commit()
    return decoded_n


def _flatten(d, prefix=""):
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from _flatten(v, prefix=key + "_")
        else:
            yield key, v


# --------------------------------------------------------------------------
# Scheduler: independent loops on their own cadences.
# --------------------------------------------------------------------------
def loop(fn, interval, name):
    while True:
        try:
            fn()
        except Exception as e:
            log.exception("%s loop error: %s", name, e)
        time.sleep(interval)


def main():
    _wait_for_db()
    seed_satellites()
    fetch_elements()  # prime once before propagating

    threading.Thread(target=loop, args=(fetch_elements, ELEMENTS_INTERVAL, "elements"),
                     daemon=True).start()
    threading.Thread(target=loop, args=(fetch_telemetry, TELEMETRY_INTERVAL, "telemetry"),
                     daemon=True).start()
    # positions in the main thread
    loop(propagate_positions, POSITION_INTERVAL, "positions")


def _wait_for_db(retries=30):
    for i in range(retries):
        try:
            with db():
                return
        except Exception:
            log.info("Waiting for database... (%d)", i)
            time.sleep(2)
    raise SystemExit("Database never became available")


if __name__ == "__main__":
    main()
