# Orbit POC — live positions + deep telemetry, in one `docker compose up`

A self-contained proof of concept that **links orbital position with deep
telemetry monitoring**, entirely from open data. A MapLibre globe shows live
satellite positions; clicking a satellite opens embedded Grafana panels with its
health data (battery, temperature, current, decoded fields).

The point it proves: *your stack can fuse a position feed and a telemetry feed
into one embeddable, sovereign-hostable map + analytics surface.* Swapping the
public SatNOGS feed for a private operator feed later is a datasource change, not
a rewrite.

## Run it

```bash
docker compose up --build
```

Then open **http://localhost:8080**. Grafana is on **http://localhost:3000**.

The map and orbit tracks work with **zero accounts**. To light up the telemetry
dashboards, add a free SatNOGS token (see below) and restart:

```bash
SATNOGS_TOKEN=your_token_here docker compose up --build
```

## Architecture — the caching boundary is the whole idea

```
CelesTrak ─┐                 ┌─ MapLibre globe (web) ── reads cache only
SatNOGS  ──┤─►  ingest  ─►  db (Postgres)                │
           │  (ONLY thing            └─ Grafana ─────────┘ reads cache only
           │   touching upstream)       (embedded per-satellite panels)
```

* **ingest** is the only component that ever calls an external API. It fetches
  orbital elements from CelesTrak on a 6-hour cadence, propagates positions
  locally with SGP4 every 15 s (no external calls), and pulls decoded telemetry
  from SatNOGS every 30 min.
* **db** (Postgres) is the local cache. Everything else reads only from here.
* **web** serves the map and a tiny read-only position API.
* **grafana** renders telemetry dashboards, embedded per-satellite via a `norad`
  template variable.

This boundary is not decoration. CelesTrak firewalls IPs that pull more than
~100 MB/day and asks you to download each dataset once per update. A naive
"fetch on every map refresh" design gets you banned. Caching once and serving
locally is both the polite and the production-correct pattern.

## Getting a SatNOGS token (free, optional, ~1 minute)

Telemetry (not positions) now requires an API key:

1. Register at https://db.satnogs.org
2. Click your avatar → **Settings** → copy your **API Key**
3. Pass it as `SATNOGS_TOKEN` (see run command above)

Positions and orbit tracks need **no account at all**.

## Honest caveats (read these — they shape the demo)

* **Telemetry coverage is sparse and uneven.** Open health data exists only for
  satellites that broadcast it *and* were recently heard by a volunteer ground
  station. Most objects show position but no telemetry. The showcase list in
  `ingest/satellites.py` is deliberately small and curated so the demo always
  looks alive. Position-only satellites are labelled as such in the UI rather
  than faked.

* **NORAD ids churn.** The catalogue overflowed the 5-digit TLE format in
  July 2026; new objects get 6-digit ids that have no legacy-TLE representation.
  For the small showcase set this POC uses per-id TLE fetches for SGP4
  convenience, but for a real fleet switch to the CelesTrak **OMM/JSON group
  endpoint**. The ingest service resolves each showcase entry at startup and
  logs anything it can't find rather than failing hard — prune/extend the list
  from that log.

* **Coordinate conversion is simplified.** TEME→ECEF uses a low-precision GMST
  and a spherical Earth. It's accurate enough to place dots on a world map
  (verified: ISS ~402 km, in-range lat/lon). For precise ground tracks, swap in
  `astropy` later.

* **Auth is anonymous for the local POC.** Grafana runs with anonymous viewer
  access and embedding enabled — fine locally. In production this is exactly the
  seam where a **cookie/JWT auth-proxy** replaces anonymous access to give
  secure, multi-tenant embedding. That proxy is the productizable part.

## What to change first

* `ingest/satellites.py` — the showcase set. Start here.
* `grafana/dashboards/orbit-telemetry.json` — the health panels. The field
  matching (`ILIKE '%volt%'` etc.) is generic on purpose because each
  satellite's decoded schema differs; tighten it per satellite as you learn
  their frames.

## Next step toward a product

The paid conversation with a NewSpace operator (U-Space-type) is:
"now pipe in *our* private telemetry instead of SatNOGS, in an isolated
sovereign tenant." That is a datasource swap plus the auth-proxy — both of which
this layout is built to accept without restructuring.
