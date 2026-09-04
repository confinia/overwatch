#!/usr/bin/env python3
"""Resolve each tracked satellite's country of origin (#99) from SatNOGS DB.

SatNOGS DB carries a `countries` field per satellite (ISO 3166 alpha-2, e.g.
"BY", "US", "DE"). This writes orbit-poc/web/satellite_countries.json
({norad: "BY"}) so the API can serve it and the frontend can render a flag
emoji in front of the satellite name. Only single-country entries are kept
(multi/empty -> omitted, so we never show a wrong flag). Stdlib only.
"""
import json
import os
import re
import urllib.request

SATNOGS_BASE = os.environ.get("SATNOGS_BASE", "https://db.satnogs.org/api").rstrip("/")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "orbit-poc", "web", "satellite_countries.json")
SATS_URL = os.environ.get("OVERWATCH_SATS", "https://overwatch.confinia.io/api/satellites")
UA = {"User-Agent": "overwatch-country-resolver/1.0 (+https://overwatch.confinia.io)"}


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    ours = {s["norad"] for s in fetch(SATS_URL)}
    catalog = fetch(f"{SATNOGS_BASE}/satellites/?format=json")
    out = {}
    for s in catalog:
        norad = s.get("norad_cat_id")
        if norad not in ours:
            continue
        c = (s.get("countries") or "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}", c):        # single ISO-2 only
            out[str(norad)] = c
    ordered = dict(sorted(out.items(), key=lambda kv: int(kv[0])))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=2)
        f.write("\n")
    print(f"wrote {OUT}: {len(out)}/{len(ours)} have a single-country flag")
    for k, v in ordered.items():
        print(f"  {k} {v}")


if __name__ == "__main__":
    main()
