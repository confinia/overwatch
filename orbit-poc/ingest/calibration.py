"""Per-satellite telemetry calibration (raw register -> physical units).

Some satnogs-decoders kaitai structs expose raw register values, not physical
units — so a temperature can decode as ~65000 (an unsigned 16-bit count) instead
of a few degrees C. SatNOGS's own per-satellite dashboards apply the scale/sign
each field needs (right in the panel query), so they are ground truth for the
calibration.

CUBEBEL-2 (57175): derived from dashboard.satnogs.org/d/dnpcsk94k — reported by
Vlad Chorney (EU1SAT), who runs the CubeBel ground station. Two systematic
errors were fixed: (1) missing scale factors, (2) missing signed interpretation
(cold temps wrapped to ~65000 / ~255).

A rule is matched by the field-name *leaf* (our fields carry the full kaitai
path, e.g. ...cdm_payload_adc_temp_1). Transform: optional two's-complement
`wrap` (subtract `wrap` when value >= wrap/2), then `value * scale + offset`.
Pure module (no deps) so the gate can unit-test it.
"""

CALIBRATION = {
    "cubebel2": [
        # temperatures — confirmed against live frames + the SatNOGS dashboard
        ("adc_temp_1", {"wrap": 65536, "scale": 0.0078}),   # 64928 -> -608 -> -4.7 C
        ("adc_temp_2", {"wrap": 65536, "scale": 0.0078}),
        ("tmp75_temp", {"wrap": 256}),                        # 254.31 -> -1.69 C (signed)
        ("beacon_pamp_temp", {"scale": 0.001}),               # 3255 -> 3.26 C
        ("beacon_vbus", {"scale": 0.001}),                    # 4140..5053 -> 4.14..5.05 V (bus/battery)
    ],
}

# Per-decoder explicit canonical sources: canonical field <- a (calibrated) leaf.
# When a decoder is listed here the generic heuristic (_canonical) is skipped for
# it, so a wrong field can't masquerade as the canonical (e.g. CUBEBEL-2's 5 V
# TRX rail `ina226_pamp_voltage` matched "volt" and became battery_v — Vlad's
# battery-voltage bug). SatNOGS labels beacon_vbus (*0.001) the battery voltage.
CANONICAL_SOURCES = {
    "cubebel2": {"battery_v": "beacon_vbus"},
}


def calibrate(decoder, fields):
    """Apply the decoder's calibration in place, returning the same dict.

    Only numeric leaves matching a rule are transformed; everything else is
    untouched, and an unknown decoder is a no-op.
    """
    rules = CALIBRATION.get(decoder)
    if not rules:
        return fields
    for key, v in list(fields.items()):
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        for suffix, t in rules:
            if key.endswith(suffix):
                wrap = t.get("wrap")
                if wrap and v >= wrap / 2:
                    v -= wrap
                fields[key] = v * t.get("scale", 1.0) + t.get("offset", 0.0)
                break
    return fields


def canonical_from(decoder, fields):
    """Explicit canonical (name, value) pairs for a decoder, read from the
    already-calibrated fields. Empty list if the decoder has no explicit map
    (callers then fall back to the generic heuristic)."""
    src = CANONICAL_SOURCES.get(decoder)
    if not src:
        return []
    out = []
    for canon, leaf in src.items():
        for k, v in fields.items():
            if k.endswith(leaf) and isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append((canon, float(v)))
                break
    return out
