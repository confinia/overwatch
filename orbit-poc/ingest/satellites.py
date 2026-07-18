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

    # Validated by batch/sweep_full.py on 2026-07-18 (frames < 7 days, decode OK)
    {"norad": 32785, "name": "CUTE-1.7+APD II (CO-65)", "telemetry": True, "decoder": "co65",
     "note": "Tokyo Tech veteran; battery current in beacon."},
    {"norad": 41789, "name": "ALSAT-1N", "telemetry": True, "decoder": "alsat1n",
     "note": "Algerian/UK 3U; EPS temperatures."},
    {"norad": 43784, "name": "SNUGLITE", "telemetry": True, "decoder": "snuglite",
     "note": "SNU 2U; 77-field beacon incl. battery V/I."},
    {"norad": 43880, "name": "UWE-4", "telemetry": True, "decoder": "uwe4",
     "note": "Uni Würzburg; per-battery voltage, current, state of charge."},
    {"norad": 46504, "name": "NETSAT 4", "telemetry": True, "decoder": "netsat",
     "note": "Würzburg formation-flying quartet (1/4)."},
    {"norad": 46505, "name": "NETSAT 3", "telemetry": True, "decoder": "netsat",
     "note": "Würzburg formation-flying quartet (2/4)."},
    {"norad": 46506, "name": "NETSAT 1", "telemetry": True, "decoder": "netsat",
     "note": "Würzburg formation-flying quartet (3/4)."},
    {"norad": 46507, "name": "NETSAT 2", "telemetry": True, "decoder": "netsat",
     "note": "Würzburg formation-flying quartet (4/4)."},
    {"norad": 62391, "name": "LASARsat", "telemetry": True, "decoder": "lasarsat",
     "note": "Czech pocketqube; active beacon."},
    {"norad": 62394, "name": "CroCube", "telemetry": True, "decoder": "crocube",
     "note": "First Croatian CubeSat; active beacon."},
    {"norad": 66778, "name": "Foresail-1p", "telemetry": True, "decoder": "foresail1",
     "note": "Aalto/FMI (Finland); Foresail-1 frame format."},
    {"norad": 67683, "name": "KNACKSAT-2", "telemetry": True, "decoder": "knacksat2",
     "note": "Thai 3U; compact beacon."},
    {"norad": 68458, "name": "SAL-E", "telemetry": True, "decoder": "cp16",
     "note": "Cal Poly CP16 class; panel temperatures."},
    {"norad": 68506, "name": "AEPEX", "telemetry": True, "decoder": "aepex",
     "note": "CU Boulder space-weather 6U."},
    {"norad": 69015, "name": "FrontierSat", "telemetry": True, "decoder": "frontiersat",
     "note": "36-field beacon incl. battery percent + temps."},
]
# Product decision 2026-07-18: telemetry-only constellation. Position-only
# anchors and CelesTrak bulk groups are out; every satellite shown must have
# open, locally-decodable telemetry. Bulk groups can come back by setting
# CELESTRAK_GROUPS in docker-compose.yml.

# NOTE on ids: NORAD ids for specific CubeSats churn as objects re-catalog.
# UPMSAT-2 (46276) was removed 2026-07-18: CelesTrak per-CATNR fetch 404s, so
# it never even got a position. Candidates found decodable by the batch probe
# get added here with their decoder module.
