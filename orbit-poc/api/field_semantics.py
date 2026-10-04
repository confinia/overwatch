"""What a decoded telemetry field IS — the semantic layer of #526.

The fleet has 1315 distinct field names across 25 satellites, flattened kaitai
decoder paths: `beacon_types_type_check_csp_header_source_port` next to
`payload_ses_median_panel_x_temp_negative`. Two questions, asked in order:

  1. Is this a measurement at all?  CSP/AX.25 headers, checksums, sequence
     numbers and timestamp components are frame plumbing — they belong in a
     raw-frame inspector, never on a chart. The per-satellite boards were a
     wall of lines because they plotted the packet header.
  2. What does it measure?  A small canonical set (temperature, voltage,
     current, ...) with a unit and a plausible range, so a Power panel can
     plot whatever THIS spacecraft's voltage and current map to.

Everything that decides is data in field_semantics.json — patterns seeded
across the fleet, corrected per decoder by leaf — so a wrong guess is a
one-line edit there. This module only applies it. Pure: no deps, no I/O past
loading the JSON once, so the gate can test it and the api can import it.

A lesson baked into the data format: `ax25` appears in 767 of the 1315 names
as a PATH PREFIX (most decoders put the payload under `ax25_frame_...`), so
"contains ax25" is not scaffolding — the api's old transport classifier said
it was, and marked the ISS's `ax25_frame_payload_info_temp` as framing.
Segments are matched whole, leaves exactly.
"""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
SEMANTICS_FILE = os.environ.get("FIELD_SEMANTICS_FILE",
                                os.path.join(_HERE, "field_semantics.json"))

CANONICAL = {"battery_v": "voltage", "battery_i": "current", "battery_pct": "charge"}


def _load(path=None):
    with open(path or SEMANTICS_FILE, encoding="utf-8") as fh:
        d = json.load(fh)
    return {
        "segments": set(d["scaffolding_segments"]),
        "leaves": set(d["scaffolding_leaves"]),
        "state_leaves": set(d.get("state_leaves", [])),
        "measures": [(m["measure"], m.get("unit"), m.get("range"), re.compile(m["match"]))
                     for m in d["measures"]],
        "units": {m["measure"]: (m.get("unit"), m.get("range")) for m in d["measures"]},
        "overrides": {k: v for k, v in d.get("overrides", {}).items() if not k.startswith("_")},
    }


_SEM = None


def _sem():
    global _SEM
    if _SEM is None:
        _SEM = _load()
    return _SEM


def reload(path=None):
    """Re-read the data file (tests point FIELD_SEMANTICS_FILE at a variant)."""
    global _SEM
    _SEM = _load(path)
    return _SEM


def classify(field: str, decoder: str | None = None) -> dict:
    """-> {"kind": "scaffolding" | "measure" | "unknown", "measure": str | None,
           "unit": str | None, "range": [lo, hi] | None, "canonical": bool}

    Order: per-decoder override by leaf, then the canonical names our ingest
    derives, then scaffolding (whole segments / exact leaf), then the first
    measure whose pattern matches, else unknown."""
    s = _sem()
    f = field.lower()
    parts = f.split("_")
    leaf = parts[-1] if parts else f

    def measure(name, canonical=False):
        unit, rng = s["units"].get(name, (None, None))
        return {"kind": "measure", "measure": name, "unit": unit, "range": rng,
                "canonical": canonical}

    ov = s["overrides"].get(decoder or "", {})
    for n in range(1, len(parts) + 1):                  # leaf, then every longer trailing path
        hit = ov.get("_".join(parts[-n:]))
        if hit:
            if hit == "scaffolding":
                return {"kind": "scaffolding", "measure": None, "unit": None, "range": None,
                        "canonical": False}
            if hit == "unknown":
                return {"kind": "unknown", "measure": None, "unit": None, "range": None,
                        "canonical": False}
            return measure(hit)

    if f in CANONICAL:
        return measure(CANONICAL[f], canonical=True)

    # whole-segment scaffolding: `csp_header` as a two-token segment anywhere
    joined = "_" + f + "_"
    if any("_" + seg + "_" in joined for seg in s["segments"]):
        return {"kind": "scaffolding", "measure": None, "unit": None, "range": None,
                "canonical": False}
    if leaf in s["leaves"] or "_".join(parts[-2:]) in s["leaves"]:
        return {"kind": "scaffolding", "measure": None, "unit": None, "range": None,
                "canonical": False}

    # A field whose last segments say mode/state/status IS a state, whatever
    # precedes them: `power_line_state`, `current_mode_id`, `restarts_count`
    # all decide on their tail, not on `power`/`current` earlier in the path.
    if any(t in s["state_leaves"] for t in parts[-2:]):
        return measure("state")

    # A one-letter unit leaf decides too: `tlm_vbat_i` is the current OF the
    # battery rail, not a voltage — `vbat` earlier in the name must not win.
    unit_leaf = {"i": "current", "v": "voltage", "t": "temperature"}.get(leaf)
    if unit_leaf:
        return measure(unit_leaf)

    for name, unit, rng, rx in s["measures"]:
        if rx.search(f):
            return {"kind": "measure", "measure": name, "unit": unit, "range": rng,
                    "canonical": False}
    return {"kind": "unknown", "measure": None, "unit": None, "range": None, "canonical": False}


def is_transport(field: str, decoder: str | None = None) -> bool:
    """The api's `source` category for a field (#46), from the same data."""
    return classify(field, decoder)["kind"] == "scaffolding"


def coverage(fields_by_sat: dict, decoders: dict | None = None, unknown_sample: int = 12) -> dict:
    """fields_by_sat: {norad: [field, ...]}; decoders: {norad: decoder}.
    -> {"fleet": {...}, "satellites": {norad: {...}}} with counts per kind and
    per measure, and a bounded sample of the unknown names — the work queue."""
    decoders = decoders or {}
    fleet = {"fields": 0, "scaffolding": 0, "measure": 0, "unknown": 0, "by_measure": {}}
    sats = {}
    seen_fleet = set()
    for norad, fields in fields_by_sat.items():
        dec = decoders.get(norad)
        row = {"decoder": dec, "fields": 0, "scaffolding": 0, "measure": 0, "unknown": 0,
               "by_measure": {}, "unknown_sample": []}
        for field in sorted(set(fields)):
            c = classify(field, dec)
            row["fields"] += 1
            row[c["kind"]] += 1
            if c["kind"] == "measure":
                row["by_measure"][c["measure"]] = row["by_measure"].get(c["measure"], 0) + 1
            elif c["kind"] == "unknown" and len(row["unknown_sample"]) < unknown_sample:
                row["unknown_sample"].append(field)
            if field not in seen_fleet:
                seen_fleet.add(field)
                fleet["fields"] += 1
                fleet[c["kind"]] += 1
                if c["kind"] == "measure":
                    fleet["by_measure"][c["measure"]] = fleet["by_measure"].get(c["measure"], 0) + 1
        for k in ("scaffolding", "measure", "unknown"):
            row[k + "_pct"] = round(100.0 * row[k] / row["fields"], 1) if row["fields"] else 0.0
        sats[norad] = row
    for k in ("scaffolding", "measure", "unknown"):
        fleet[k + "_pct"] = round(100.0 * fleet[k] / fleet["fields"], 1) if fleet["fields"] else 0.0
    # how many satellites each measure reaches — the number the fleet panels care about
    fleet["satellites_by_measure"] = {}
    for row in sats.values():
        for m in row["by_measure"]:
            fleet["satellites_by_measure"][m] = fleet["satellites_by_measure"].get(m, 0) + 1
    return {"fleet": fleet, "satellites": sats}
