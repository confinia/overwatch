#!/usr/bin/env python3
"""Resolve each tracked satellite's SatNOGS Telemetry Dashboard URL (#88).

SatNOGS DB publishes a hand-curated Grafana dashboard for most satellites and
links to it from the satellite page (db.satnogs.org/satellite/<norad>) as an
href into dashboard.satnogs.org. This script discovers that link for every
satellite Overwatch tracks and writes orbit-poc/web/satnogs_dashboards.json
({norad: url}). Satellites with no dashboard are simply omitted.

Overwatch surfaces the link per satellite (title bar + spacecraft view) so a
user one click away from our own auto-grouped panels can also open the mission
team's curated dashboard. Re-run periodically; stdlib only, no dependencies.
"""
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "orbit-poc", "web", "satnogs_dashboards.json")
SATS_URL = os.environ.get(
    "OVERWATCH_SATS", "https://overwatch.confinia.io/api/satellites")
# db.satnogs.org goes through the SPOT (rate-limited, cached) like every other
# SatNOGS request. Default direct for standalone dev; run.sh sets the gateway.
SATNOGS_HOST = os.environ.get("SATNOGS_HOST", "https://db.satnogs.org").rstrip("/")
UA = {"User-Agent": "overwatch-dashboard-resolver/1.0 "
      "(+https://overwatch.confinia.io)"}
HREF = re.compile(r'href="(https://dashboard\.satnogs\.org/d/[^"]+)"', re.I)


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def clean(url):
    # keep the stable /d/<uid>[/<slug>] identity, drop volatile query params
    return url.split("?")[0].replace("&amp;", "&").rstrip("/")


def main():
    sats = json.loads(fetch(SATS_URL))
    out = {}
    for s in sats:
        norad = s["norad"]
        try:
            html = fetch(f"{SATNOGS_HOST}/satellite/{norad}")
        except Exception as e:                                    # noqa: BLE001
            print(f"  {norad} fetch failed: {e}", file=sys.stderr)
            continue
        m = HREF.search(html)
        if m:
            out[str(norad)] = clean(m.group(1))
            print(f"  YES {norad} {s['name']} -> {out[str(norad)]}")
        else:
            print(f"  no  {norad} {s['name']}")
    ordered = dict(sorted(out.items(), key=lambda kv: int(kv[0])))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=2)
        f.write("\n")
    print(f"wrote {OUT}: {len(out)}/{len(sats)} have a SatNOGS dashboard")


if __name__ == "__main__":
    main()
