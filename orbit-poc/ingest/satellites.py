"""
Curated showcase satellite set for the POC.

Design choice (see the README): we do NOT try to render the whole 30k-object
catalogue. We hand-pick a small set where BOTH position AND (for most of them)
open telemetry are reliably available, so the demo always looks alive.

Each entry:
  norad     - NORAD catalog id (positions come from CelesTrak by this id)
  name      - display name
  sat_id    - SatNOGS DB sat_id (used for telemetry). None => position-only.
  telemetry - whether we expect decoded health frames in SatNOGS DB
  note      - why it's in the showcase

The `sat_id` values are looked up automatically at runtime from the SatNOGS
/api/satellites/ endpoint by norad id, so you do NOT need to hardcode them.
This list is intentionally small and editable.
"""

SHOWCASE = [
    # Position + rich telemetry: the reliable demo stars
    {"norad": 25544, "name": "ISS (ZARYA)",      "telemetry": True,
     "note": "Densest coverage of anything in orbit; always has fresh passes."},
    {"norad": 40967, "name": "LightSail / well-tracked CubeSat class",
     "telemetry": True,
     "note": "Representative amateur CubeSat with open beacon."},

    # Position + telemetry, European / NewSpace flavour (fits the pitch)
    {"norad": 46277, "name": "NEMO-HD",           "telemetry": True,
     "note": "European smallsat; appears in SatNOGS TLE feed."},
    {"norad": 46276, "name": "UPMSAT-2",          "telemetry": True,
     "note": "Universidad Politecnica de Madrid sat - lab audience relevance."},

    # Position-only anchors (no open telemetry) - shown honestly as such
    {"norad": 43013, "name": "SENTINEL-2 class (EU EO)", "telemetry": False,
     "note": "Copernicus/EU sovereignty story; position-only (encrypted downlink)."},
    {"norad": 48274, "name": "GNSS reference (Galileo class)", "telemetry": False,
     "note": "Navigation context; position-only."},
]

# NOTE on ids: NORAD ids for specific CubeSats churn as objects re-catalog.
# The ingest service resolves each showcase entry against SatNOGS + CelesTrak
# at startup and logs any it cannot find, rather than failing hard. Treat this
# list as a starting point and prune/extend based on the startup log.
