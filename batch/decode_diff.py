"""Decode-quality harness — inspect a satellite's decoded EPS/thermal values.

Motivated by Vlad Chorney's (EU1SAT) report that CUBEBEL-2 battery voltage and
temperatures decode wrong in Overwatch. This fetches a satellite's recent raw
frames from SatNOGS (the same public source + decoder our sweep uses) and prints
every EPS/thermal field with its value range, so a mis-scaled or mis-parsed
value (e.g. millivolts shown as volts, raw ADC counts, a bad byte offset) is
obvious against the mission's SatNOGS dashboard gauges. Ground truth = the raw
hex frame in hand.

Run through the gateway launcher on the VM (never a bare podman run — that is
the direct path the gateway exists to remove):
  ssh overwatch 'cd ~/projects/overwatch/batch && ./run.sh decode_diff.py 57175 cubebel2'
"""
import importlib
import os
import statistics
import sys
import time

import requests

SATNOGS_BASE = os.environ.get("SATNOGS_BASE", "https://db.satnogs.org/api").rstrip("/")

H = {"Authorization": f"Token {os.environ['TOKEN']}", "User-Agent": "orbit-poc/0.1"}
HEALTH = ("volt", "vbat", "batt", "temp", "curr", "_i_", "power", "pwr", "amp")


def flatten(obj, prefix="", depth=0, out=None):
    if out is None:
        out = {}
    if depth > 5:
        return out
    for a in dir(obj):
        if a.startswith("_"):
            continue
        try:
            v = getattr(obj, a)
        except Exception:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[prefix + a] = v
        elif hasattr(v, "__class__") and v.__class__.__module__.startswith("satnogsdecoders"):
            flatten(v, prefix + a + "_", depth + 1, out)
    return out


def fetch(**flt):
    # SatNOGS throttles hard: honor Retry-After and back off, like sweep_full.py.
    # The telemetry endpoint wants sat_id (norad_cat_id 400s), so callers pass
    # sat_id=... and fall back to norad_cat_id only if needed.
    for _ in range(6):
        r = requests.get(f"{SATNOGS_BASE}/telemetry/",
                         params={**flt, "format": "json"}, headers=H, timeout=60)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 20)) + 2
            print(f"  429 — backing off {wait}s", flush=True)
            time.sleep(wait)
            continue
        r.raise_for_status()
        d = r.json()
        return d["results"] if isinstance(d, dict) else d
    raise SystemExit("gave up after repeated 429s")


def main():
    norad = sys.argv[1] if len(sys.argv) > 1 else "57175"
    mod = sys.argv[2] if len(sys.argv) > 2 else "cubebel2"
    sat_id = sys.argv[3] if len(sys.argv) > 3 else None
    results = fetch(sat_id=sat_id) if sat_id else fetch(norad_cat_id=norad)
    print(f"{norad}: {len(results)} frames fetched; decoder={mod}\n", flush=True)

    m = importlib.import_module(f"satnogsdecoders.decoder.{mod}")
    cls = getattr(m, mod.capitalize())

    series, decoded = {}, 0
    for fr in results:
        try:
            f = flatten(cls.from_bytes(bytes.fromhex(fr["frame"])))
        except Exception:
            continue
        if len(f) < 3:
            continue
        decoded += 1
        for k, v in f.items():
            if any(w in k.lower() for w in HEALTH):
                series.setdefault(k, []).append(v)

    print(f"decoded {decoded}/{len(results)} frames; {len(series)} EPS/thermal fields "
          f"(watch for implausible ranges — battery ~3.0-8.4 V, temps ~ -40..+60 C):\n")
    for k in sorted(series):
        vals = series[k]
        print(f"  {k:56s} n={len(vals):3d}  min={min(vals):12.3f}  "
              f"max={max(vals):12.3f}  mean={statistics.fmean(vals):12.3f}  last={vals[-1]}")


if __name__ == "__main__":
    main()
