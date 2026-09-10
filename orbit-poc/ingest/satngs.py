"""
SATNGS LoRa stations as a telemetry source (#454).

SATNGS (Satellite Access and Tracking Network of Ground Stations) complements
SatNOGS with inexpensive ESP8266 LoRa receivers. A station publishes its own
reception log as CSV, one row per frame heard:

    unix,local_time,norad,sat,rssi_dbm,snr_db,ferr_hz,len,hex

Pulling from the station directly gives three things the SatNOGS API does not:
frames keep arriving while that API is unreachable (it was, for days, in
August and again from 2026-09-07), the link quality the station measured
(RSSI / SNR), and the raw frame itself, which the station's rolling logger
forgets within a day. The raw frame is stored so that a decoder added later
can replay it: none of the LoRa satellites heard in September ships in
satnogs-decoders 1.124.0, so today they are position-and-reception only.

One small GET per station per cycle. The other end is a microcontroller: the
default cadence is ten minutes, never seconds.
"""
import csv
import io
import logging
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import requests
from psycopg2.extras import execute_values

log = logging.getLogger("ingest.satngs")

# "url=OBSERVER[-GRID],url=OBSERVER[-GRID]". The observer follows the SatNOGS
# convention (callsign, optionally "-" + Maidenhead locator), so the same
# station shows up as ONE entry whether a frame came through SatNOGS or
# straight from the station, and the grid gives it a place on the map.
SATNGS_STATIONS = os.environ.get("SATNGS_STATIONS", "")
SATNGS_INTERVAL = int(os.environ.get("SATNGS_INTERVAL", 600))
SATNGS_TIMEOUT = float(os.environ.get("SATNGS_TIMEOUT", 20))
SOURCE = "satngs"
MAX_ROWS = 2000                        # a rolling logger holds far fewer


def stations(spec=None):
    """Parse SATNGS_STATIONS into [(base_url, observer)]. Malformed entries
    are logged and skipped, never fatal: one bad line must not stop the
    other stations."""
    out = []
    for item in (spec if spec is not None else SATNGS_STATIONS).split(","):
        item = item.strip()
        if not item:
            continue
        url, sep, observer = item.partition("=")
        url, observer = url.strip().rstrip("/"), observer.strip()
        if not sep or not url.startswith("http") or not observer:
            log.warning("SATNGS_STATIONS entry ignored: %r (want url=OBSERVER)", item)
            continue
        out.append((url, observer))
    return out


def parse_log(text, observer, now=None):
    """CSV text -> frames in the shape _store_frames expects (timestamp,
    frame, observer, app_source) plus the station's link measurements.
    Rows without a usable norad, time or hex are skipped; future-dated rows
    (a station with a wrong clock) are skipped like SatNOGS ones are."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=1)
    frames = []
    for row in list(csv.DictReader(io.StringIO(text)))[:MAX_ROWS]:
        try:
            norad = int(row["norad"])
            ts = datetime.fromtimestamp(int(row["unix"]), tz=timezone.utc)
            hexs = (row.get("hex") or "").strip().upper()
            bytes.fromhex(hexs)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if norad <= 0 or not hexs or ts > horizon:
            continue
        frames.append({
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "frame": hexs,
            "observer": observer,
            "app_source": SOURCE,
            "norad": norad,
            "name": (row.get("sat") or "").strip() or f"NORAD {norad}",
            "rssi_dbm": _num(row.get("rssi_dbm")),
            "snr_db": _num(row.get("snr_db")),
        })
    return frames


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def fetch_station(url, observer, headers=None):
    r = requests.get(f"{url}/log.csv", headers=headers or {}, timeout=SATNGS_TIMEOUT)
    r.raise_for_status()
    return parse_log(r.text, observer)


def store(conn, frames, store_frames, decoder_for):
    """Persist one station's frames. `store_frames(norad, frames, decoder)`
    is the ingest's existing path (telemetry + reception rows); this adds
    the satellite when it is new to us, the raw frame, and the station's
    RSSI / SNR on the reception row. Returns (satellites, frames stored)."""
    by_sat = {}
    for f in frames:
        by_sat.setdefault(f["norad"], []).append(f)
    stored = 0
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('frame')")
        if cur.fetchone()[0] is None:
            log.info("frame table not created yet (the api creates it); skipping")
            return 0, 0
        for norad, fs in by_sat.items():
            # heard but not tracked: add it, position-only until a decoder
            # exists. Never overwrite what the catalogue or an operator set.
            cur.execute(
                """INSERT INTO satellite (norad, name, has_telemetry, note)
                   VALUES (%s, %s, false, %s) ON CONFLICT (norad) DO NOTHING""",
                (norad, fs[0]["name"], "Heard by a SATNGS LoRa station (#454)"))
        conn.commit()
    for norad, fs in by_sat.items():
        store_frames(norad, fs, decoder_for(norad))
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO frame (norad, ts, observer, source, hex)
                VALUES %s ON CONFLICT DO NOTHING""",
                [(norad, f["timestamp"], f["observer"], SOURCE, f["frame"]) for f in fs])
            execute_values(cur, """
                UPDATE reception r
                   SET rssi_dbm = v.rssi::double precision,
                       snr_db = v.snr::double precision
                  FROM (VALUES %s) AS v(norad, ts, observer, rssi, snr)
                 WHERE r.norad = v.norad::int AND r.ts = v.ts::timestamptz
                   AND r.observer = v.observer""",
                [(norad, f["timestamp"], f["observer"], f["rssi_dbm"], f["snr_db"])
                 for f in fs])
            conn.commit()
        stored += len(fs)
    return len(by_sat), stored


def fetch_satngs(db, store_frames, decoder_for, headers=None):
    """One cycle over every configured station."""
    for url, observer in stations():
        try:
            frames = fetch_station(url, observer, headers)
        except Exception as e:                        # one station down != all
            log.warning("satngs %s (%s): %s", observer, url, e)
            continue
        with db() as conn:
            sats, n = store(conn, frames, store_frames, decoder_for)
        log.info("satngs %s: %d frames from %d satellites", observer, n, sats)
