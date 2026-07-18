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
    {"norad": 43017, "name": "FOX-1B (AO-91)", "telemetry": True, "decoder": "fox",
     "note": "Same Fox DUV beacon family as AO-85; active amateur repeater."},
    {"norad": 43137, "name": "FOX-1D (AO-92)", "telemetry": True, "decoder": "fox",
     "note": "Same Fox DUV beacon family as AO-85."},
    {"norad": 60237, "name": "GRBBeta", "telemetry": True, "decoder": "grbbeta",
     "note": "European (Slovak/Hungarian) gamma-ray CubeSat; very active."},
    {"norad": 57175, "name": "CUBEBEL-2", "telemetry": True, "decoder": "cubebel2",
     "note": "Belarusian 3U; richest live beacon found (70 decoded fields)."},
    {"norad": 55104, "name": "Sharjahsat-1", "telemetry": True, "decoder": "sharjahsat1",
     "note": "UAE 3U; heard hourly by volunteer stations."},
    {"norad": 60246, "name": "CatSat", "telemetry": True, "decoder": "catsat",
     "note": "University of Arizona 6U; active beacon."},
    {"norad": 40931, "name": "LAPAN-A2 (IO-86)", "telemetry": True, "decoder": "io86",
     "note": "Indonesian microsat, amateur payload; frequent passes."},
]
# Product decision 2026-07-18: telemetry-only constellation. Position-only
# anchors and CelesTrak bulk groups are out; every satellite shown must have
# open, locally-decodable telemetry. Bulk groups can come back by setting
# CELESTRAK_GROUPS in docker-compose.yml.

# NOTE on ids: NORAD ids for specific CubeSats churn as objects re-catalog.
# UPMSAT-2 (46276) was removed 2026-07-18: CelesTrak per-CATNR fetch 404s, so
# it never even got a position. Candidates found decodable by the batch probe
# get added here with their decoder module.
