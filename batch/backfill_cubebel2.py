"""One-time backfill: re-decode recent CUBEBEL-2 frames WITH calibration and
overwrite the stale telemetry rows, so the Temperatures panel shows physical
values immediately instead of the ~65000 raw counts (Vlad/EU1SAT report).

Idempotent (re-decode is deterministic; UPSERT DO UPDATE). Run inside the ingest
container, which has satnogs-decoders, DB_DSN and SATNOGS_TOKEN; needs
calibration.py alongside. Self-contained so it doesn't depend on the image's
ingest.py version.
"""
import datetime
import importlib
import os
import re
import time

import psycopg2
import requests
from psycopg2.extras import execute_values

from calibration import calibrate

SATNOGS_BASE = os.environ.get("SATNOGS_BASE", "https://db.satnogs.org/api").rstrip("/")

NORAD = 57175
SAT_ID = "PIHV-8715-3112-5892-6258"
DECODER = "cubebel2"
DB_DSN = os.environ["DB_DSN"]
TOKEN = os.environ["SATNOGS_TOKEN"]
BASE = f"{SATNOGS_BASE}"
H = {"Authorization": f"Token {TOKEN}", "User-Agent": "orbit-poc/backfill"}
JUNK = re.compile(r"(ax25_header|ssid|hbit|_ctl$|_pid$|mask|_raw$|callsign|crc"
                  r"|_magic|(message|msg|packet|frame)_type)", re.I)


def get_frames(pages=30):
    frames, url, params = [], f"{BASE}/telemetry/", {"sat_id": SAT_ID}
    for _ in range(pages):
        for _ in range(5):
            r = requests.get(url, params=params, headers=H, timeout=40)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 15)) + 1)
                continue
            r.raise_for_status()
            break
        d = r.json()
        page = d.get("results", []) if isinstance(d, dict) else d
        frames += page
        url, params = (d.get("next"), None) if isinstance(d, dict) else (None, None)
        if not url:
            break
        time.sleep(1)
    return frames


def decode(hexf):
    m = importlib.import_module(f"satnogsdecoders.decoder.{DECODER}")
    cls = getattr(m, DECODER.capitalize())
    obj = cls.from_bytes(bytes.fromhex(hexf))
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


def main():
    frames = get_frames()
    print(f"fetched {len(frames)} frames", flush=True)
    horizon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    rows = []
    for f in frames:
        ts, hexf = f.get("timestamp"), f.get("frame")
        if not ts or not hexf:
            continue
        try:
            if datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")) > horizon:
                continue
        except ValueError:
            continue
        try:
            fields = decode(hexf)
        except Exception:
            continue
        calibrate(DECODER, fields)
        for k, v in fields.items():
            if JUNK.search(k):
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                rows.append((NORAD, ts, k, float(v)))
    rows = list({(r[0], r[1], r[2]): r for r in rows}.values())
    print(f"upserting {len(rows)} rows (DO UPDATE)", flush=True)
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()
    execute_values(cur,
                   "INSERT INTO telemetry (norad, ts, field, value_num) VALUES %s "
                   "ON CONFLICT (norad, ts, field) DO UPDATE SET value_num = EXCLUDED.value_num",
                   rows)
    conn.commit()
    cur.execute("SELECT field, count(*), round(min(value_num)::numeric, 2), "
                "round(max(value_num)::numeric, 2) FROM telemetry "
                "WHERE norad = %s AND field ~* 'temp' GROUP BY field ORDER BY field", (NORAD,))
    print("temperature fields after backfill (field, n, min, max):", flush=True)
    for row in cur.fetchall():
        print("  ", row, flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
