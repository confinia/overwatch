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
    # NORBI (46494), from the field docs of LW2DTZ's norbi.ksy (#458)
    "norbi": [
        ("ses_voltage", {"scale": 0.001}),                    # mV -> V (power-system bus)
        ("sop_latitude_glonass", {"scale": 1e-7}),            # degrees x 1e7
        ("sop_longitude_glonass", {"scale": 1e-7}),
        ("brk_last_received_packet_snr_active", {"scale": 0.25}),   # dB x4
        ("brk_last_received_packet_snr_inactive", {"scale": 0.25}),
    ],
    # Sharjahsat-1 (55104), from the per-field formulas documented in
    # satnogs-decoders' sharjahsat1.ksy ("x*0.0009775 [A]" and the like), read
    # 2026-10-04. Every value is expressed in V, A or degC. Leaves that repeat
    # under different parents with DIFFERENT formulas (battery_ipcm3v3 is mA,
    # eps_ipcm3v3 is A) carry the parent in the suffix; a bare suffix would
    # match both. Checked against our raw rows: battery vbat ~830 -> 7.5 V,
    # tbrd ~740 -> 2.4 degC, ibcra* up to ~18000 -> 17.6 A.
    "sharjahsat1": [
        # OBC telemetry block
        ("tlm_board_temp1", {"scale": 0.01}), ("tlm_board_temp2", {"scale": 0.01}),
        ("tlm_board_temp3", {"scale": 0.01}),
        ("tlm_vbat_v", {"scale": 0.001}), ("tlm_vbat_plat_v", {"scale": 0.001}),
        ("tlm_3v3_plat_v", {"scale": 0.001}), ("tlm_vbat_periph_v", {"scale": 0.001}),
        ("tlm_3v3_periph_v", {"scale": 0.001}),
        ("tlm_vbat_i", {"scale": 0.001}),
        ("tlm_vbat_periph_i", {"scale": 0.01}), ("tlm_3v3_periph_i", {"scale": 0.01}),
        # battery board
        ("battery_vbat", {"scale": 0.008993}),              # the battery itself
        ("battery_ibat", {"scale": 0.014662757}),           # mA -> A
        ("battery_vpcm3v3", {"scale": 0.004311}), ("battery_vpcm5v", {"scale": 0.005865}),
        ("battery_ipcm3v3", {"scale": 0.001327547}), ("battery_ipcm5v", {"scale": 0.001327547}),
        ("battery_tbrd", {"scale": 0.372434, "offset": -273.15}),
        ("battery_tbat1", {"scale": 0.3976, "offset": -238.57}),
        ("battery_tbat2", {"scale": 0.3976, "offset": -238.57}),
        ("battery_tbat3", {"scale": 0.3976, "offset": -238.57}),
        # EPS board
        ("eps_vpcmbatv", {"scale": 0.008978}), ("eps_ipcmbatv", {"scale": 0.00681988679}),
        ("eps_vpcm3v3", {"scale": 0.004311}), ("eps_ipcm3v3", {"scale": 0.00681988679}),
        ("eps_vpcm5v", {"scale": 0.005865}), ("eps_ipcm5v", {"scale": 0.00681988679}),
        ("eps_vpcm12v", {"scale": 0.01349}), ("eps_ipcm12v", {"scale": 0.002066632361}),
        ("eps_i3v3drw", {"scale": 0.001327547}), ("eps_i5vdrw", {"scale": 0.001327547}),
        ("eps_tbrd_db", {"scale": 0.372434, "offset": -273.15}),
        ("eps_tbrd", {"scale": 0.372434, "offset": -273.15}),
        # ADCS: hundredths of a degree / km
        ("sat_pos_llh_lat", {"scale": 0.01}), ("sat_pos_llh_lon", {"scale": 0.01}),
        ("sat_pos_llh_alt", {"scale": 0.01}),
        ("estm_att_angle_yaw", {"scale": 0.01}), ("estm_att_angle_pitch", {"scale": 0.01}),
        ("estm_att_angle_roll", {"scale": 0.01}),
        ("estm_ang_rate_yaw", {"scale": 0.01}), ("estm_ang_rate_pitch", {"scale": 0.01}),
        ("estm_ang_rate_roll", {"scale": 0.01}),
        # UHF/VHF modem (uA / mV)
        ("uhf_vhf_modem_current_3v3", {"scale": 3e-6}), ("uhf_vhf_modem_voltage_3v3", {"scale": 0.004}),
        ("uhf_vhf_modem_current_5v", {"scale": 62e-6}), ("uhf_vhf_modem_voltage_5v", {"scale": 0.004}),
        # S-band modem (uA / mV / its own thermistor law)
        ("s_band_modem_battery_current", {"scale": 40e-6}), ("s_band_modem_pa_current", {"scale": 40e-6}),
        ("s_band_modem_battery_voltage", {"scale": 0.004}), ("s_band_modem_pa_voltage", {"scale": 0.004}),
        ("s_band_modem_pa_temperature", {"scale": 300.0 / 4096.0, "offset": -50.0}),  # ((x*3/4096)-0.5)*100
        ("s_band_modem_board_temp_top", {"scale": 0.00390625}),
        ("s_band_modem_board_temp_bottom", {"scale": 0.00390625}),
        # solar panels: bus voltages (vbcr3 is a different divider), currents, thermistors
        ("vbcr3", {"scale": 0.0099706}),
        ("vbcr1", {"scale": 0.0322581}), ("vbcr2", {"scale": 0.0322581}), ("vbcr4", {"scale": 0.0322581}),
        ("vbcr5", {"scale": 0.0322581}), ("vbcr6", {"scale": 0.0322581}), ("vbcr7", {"scale": 0.0322581}),
        ("vbcr8", {"scale": 0.0322581}), ("vbcr9", {"scale": 0.0322581}),
        ("ibcra1", {"scale": 0.0009775}), ("ibcra2", {"scale": 0.0009775}), ("ibcra3", {"scale": 0.0009775}),
        ("ibcra4", {"scale": 0.0009775}), ("ibcra5", {"scale": 0.0009775}), ("ibcra6", {"scale": 0.0009775}),
        ("ibcra7", {"scale": 0.0009775}), ("ibcra8", {"scale": 0.0009775}), ("ibcra9", {"scale": 0.0009775}),
        ("ibcrb1", {"scale": 0.0009775}), ("ibcrb2", {"scale": 0.0009775}), ("ibcrb3", {"scale": 0.0009775}),
        ("ibcrb4", {"scale": 0.0009775}), ("ibcrb5", {"scale": 0.0009775}), ("ibcrb6", {"scale": 0.0009775}),
        ("ibcrb7", {"scale": 0.0009775}), ("ibcrb8", {"scale": 0.0009775}), ("ibcrb9", {"scale": 0.0009775}),
        ("tbcra1", {"scale": 0.4963, "offset": -273.15}), ("tbcra2", {"scale": 0.4963, "offset": -273.15}),
        ("tbcra3", {"scale": 0.4963, "offset": -273.15}), ("tbcra4", {"scale": 0.4963, "offset": -273.15}),
        ("tbcra5", {"scale": 0.4963, "offset": -273.15}), ("tbcra6", {"scale": 0.4963, "offset": -273.15}),
        ("tbcra7", {"scale": 0.4963, "offset": -273.15}), ("tbcra8", {"scale": 0.4963, "offset": -273.15}),
        ("tbcra9", {"scale": 0.4963, "offset": -273.15}),
        ("tbcrb1", {"scale": 0.4963, "offset": -273.15}), ("tbcrb2", {"scale": 0.4963, "offset": -273.15}),
        ("tbcrb3", {"scale": 0.4963, "offset": -273.15}), ("tbcrb4", {"scale": 0.4963, "offset": -273.15}),
        ("tbcrb5", {"scale": 0.4963, "offset": -273.15}), ("tbcrb6", {"scale": 0.4963, "offset": -273.15}),
        ("tbcrb7", {"scale": 0.4963, "offset": -273.15}), ("tbcrb8", {"scale": 0.4963, "offset": -273.15}),
        ("tbcrb9", {"scale": 0.4963, "offset": -273.15}),
        ("vidiodeout", {"scale": 0.008993157}), ("iidiodeout", {"scale": 0.014662757}),
        # NOT calibrated, on purpose: uhf_vhf_modem pa_temp / smps_temp (the ksy
        # says "MSB is a sign bit, [T7:T0]" and our raw rows sit at 0..255 —
        # ambiguous), interfacebrd_rtc_temperature (no formula in the ksy),
        # rf_output_power (a detector voltage, not a power).
    ],
}

# Per-decoder explicit canonical sources: canonical field <- a (calibrated) leaf.
# When a decoder is listed here the generic heuristic (_canonical) is skipped for
# it, so a wrong field can't masquerade as the canonical (e.g. CUBEBEL-2's 5 V
# TRX rail `ina226_pamp_voltage` matched "volt" and became battery_v — Vlad's
# battery-voltage bug). SatNOGS labels beacon_vbus (*0.001) the battery voltage.
CANONICAL_SOURCES = {
    "cubebel2": {"battery_v": "beacon_vbus"},
    # the SES (power system) bus voltage is the only voltage NORBI's TMI-0
    # frame carries; the spec calls it the system voltage, hence battery_v
    "norbi": {"battery_v": "ses_voltage"},
    # Sharjahsat-1: the battery board's own vbat / ibat (calibrated above).
    # The generic heuristic had picked a 1.3 V source on the live site.
    "sharjahsat1": {"battery_v": "battery_vbat", "battery_i": "battery_ibat"},
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
