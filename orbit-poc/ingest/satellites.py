"""
Curated showcase satellite set for the POC.

Design choice (see the README): we do NOT try to render the whole 30k-object
catalogue. We hand-pick a small set where BOTH position AND (for most of them)
open telemetry are reliably available, so the demo always looks alive.

Each entry:
  norad     - NORAD catalog id (positions come from CelesTrak by this id)
  name      - display name
  telemetry - whether we expect open telemetry frames in SatNOGS DB
  decoder   - satnogs-decoders module used to decode raw frames LOCALLY.
              SatNOGS stopped inlining decoded values in its API (they live in
              their InfluxDB), so sovereign local decoding is the only path.
  note      - why it's in the showcase

sat_id values are resolved automatically at runtime from the SatNOGS
/api/satellites/ endpoint by norad id. Entries whose TLE or sat_id cannot be
resolved are logged at startup, not fatal. Prune/extend from that log and
from batch/probe3.py sweeps (see batch/).
"""

SHOWCASE = [
    # Position + telemetry, decoded locally from raw frames
    {"norad": 25544, "name": "ISS (ZARYA)", "telemetry": True, "decoder": "iss",
     "note": "Densest coverage of anything in orbit; always has fresh passes."},
    {"norad": 40967, "name": "FOX-1A (AO-85)", "telemetry": True, "decoder": "fox",
     "note": "AMSAT CubeSat with open DUV beacon; heard regularly."},
    {"norad": 60237, "name": "GRBBeta", "telemetry": True, "decoder": "grbbeta",
     "note": "European (Slovak/Hungarian) gamma-ray CubeSat; very active."},
    {"norad": 53385, "name": "Geoscan-Edelveis", "telemetry": True, "decoder": "geoscan",
     "note": "Active 3U with open beacon; dense volunteer coverage."},

    # Position-only anchors - shown honestly as such
    {"norad": 46277, "name": "NEMO-HD", "telemetry": False, "decoder": None,
     "note": "European smallsat; no public frame decoder - position-only."},
    {"norad": 43013, "name": "SENTINEL-2 class (EU EO)", "telemetry": False, "decoder": None,
     "note": "Copernicus/EU sovereignty story; position-only (encrypted downlink)."},
    {"norad": 48274, "name": "GNSS reference (Galileo class)", "telemetry": False, "decoder": None,
     "note": "Navigation context; position-only."},
]

# NOTE on ids: NORAD ids for specific CubeSats churn as objects re-catalog.
# UPMSAT-2 (46276) was removed 2026-07-18: CelesTrak per-CATNR fetch 404s, so
# it never even got a position. Candidates found decodable by the batch probe
# get added here with their decoder module.
