"""The semantic layer (#526): what a decoded field IS, decided by data.

The fleet's 1315 distinct field names are flattened kaitai paths, and the
per-satellite boards plotted the packet header because nothing said which
names were measurements. These lock the two decisions — scaffolding or not,
and which measure — on names taken from the real population, and prove that
the JSON is the thing deciding, not the Python.
"""
import json
import os
import tempfile

import field_semantics as fs

HERE = os.path.dirname(__file__)


def setup_function(_):
    fs.reload()                       # every test starts from the committed data


# --- 1. is it a measurement at all? -----------------------------------------

def test_frame_plumbing_is_scaffolding():
    for f in ("beacon_types_type_check_csp_header_source_port",
              "beacon_types_type_check_csp_header_priority",
              "packet_header_csp_header_priority", "ax25_header_dest_callsign",
              "ax25_frame_length", "frame_length", "crc", "crc16", "checksum", "fcs",
              "callsign", "sequence_count", "primary_header_version",
              "header_receiver_address", "header_transaction_number",
              "payload_frame_generation_time", "payload_frame_number",
              "beacon_types_type_check_time_hour", "beacon_types_check",
              "beacon_types_type_check_not_used_1", "csp_options_hmac"):
        assert fs.classify(f)["kind"] == "scaffolding", f


def test_ax25_as_a_path_prefix_is_not_scaffolding():
    """767 of 1315 fields sit under `ax25_frame_...`: the decoder puts the
    payload there. The substring test the api used to run called all of them
    framing, including the ISS's only real field."""
    for f, m in (("ax25_frame_payload_info_temp", "temperature"),
                 ("id1_id2_id3_id4_ax25_frame_psu_battery", "voltage"),
                 ("ax25_frame_payload_ax25_info_beacon_payload_beacon_payload_vals_out_of_range", None)):
        c = fs.classify(f, "catsat" if "psu" in f else None)
        assert c["kind"] != "scaffolding", f
        if m:
            assert c["measure"] == m, (f, c)


# --- 2. what does it measure? -------------------------------------------------

def test_measures_from_the_real_population():
    cases = {
        ("norbi", "payload_ses_median_panel_x_temp_negative"): "temperature",
        ("norbi", "payload_brk_temp_active"): "temperature",
        ("norbi", "payload_brk_last_received_packet_snr_active"): "signal",
        ("norbi", "payload_brk_last_received_packet_rssi_active"): "signal",
        ("norbi", "payload_ses_charge_level_m_ah"): "charge",
        ("norbi", "payload_brk_restarts_count_active"): "counter",
        ("norbi", "payload_brk_transmitter_power_active"): "power",
        ("norbi", "payload_sop_latitude_glonass"): "position",
        ("cubebel2", "beacon_vbus"): "voltage",
        ("fox", "raw_frame_rt_tlm_sat_x_ang_vcty"): "attitude",
        ("co65", "v3_3"): "voltage",
        (None, "solar_panel_x_current"): "current",
        (None, "uptime_s"): "counter",
        (None, "freemem"): "memory",
        (None, "tx_frequency"): "frequency",
    }
    for (dec, f), want in cases.items():
        c = fs.classify(f, dec)
        assert c["kind"] == "measure" and c["measure"] == want, (f, c)
        assert c["unit"], f


def test_the_tail_decides_state_before_anything_earlier_in_the_path():
    """`power_line_state` is a state, not power; `current_mode_id` a state,
    not a current; NORBI's `_active` suffix is not a state at all."""
    for dec, f in (("norbi", "payload_ses_power_line_state"),
                   ("norbi", "payload_brk_current_mode_id"),
                   ("norbi", "payload_ms_pn_supply_state"),
                   (None, "obc_mode"), ("fox", "raw_frame_rt_tlm_ant_deploy_sensors")):
        assert fs.classify(f, dec)["measure"] == "state", f
    assert fs.classify("payload_brk_restarts_count_active", "norbi")["measure"] == "counter"


def test_canonical_names_are_measures_and_flagged():
    for f, m in (("battery_v", "voltage"), ("battery_i", "current"), ("battery_pct", "charge")):
        c = fs.classify(f)
        assert c["canonical"] is True and c["measure"] == m


def test_genuinely_unknown_stays_unknown():
    """A guess is worse than a gap: `analog3`, `misc` and a dosimeter channel
    get no measure. (`diag_data` moved to scaffolding in step 3: it is a
    diagnostics blob, not a value.)"""
    for dec, f in (("io86", "io86_type_check_ax25_frame_payload_ax25_info_analog3"),
                   ("netsat", "ax25_frame_payload_ax25_info_compass_misc"),
                   ("lasarsat", "ax25_frame_payload_dos_lppa")):
        assert fs.classify(f, dec)["kind"] == "unknown", f
    assert fs.classify("raw_frame_rt_tlm_ihu_diag_data", "fox")["kind"] == "scaffolding"


def test_step_three_names_from_the_work_queue():
    """Decoder by decoder, the names a .ksy makes unambiguous (#526 step 3)."""
    cases = {
        ("sharjahsat1", "payload_panels_ibcra3"): "current",
        ("sharjahsat1", "payload_panels_tbcrb6"): "temperature",
        ("sharjahsat1", "payload_battery_tbat1"): "temperature",
        ("sharjahsat1", "payload_eps_i3v3drw"): "current",
        ("grbbeta", "payload_in_volt1"): "voltage",
        ("grbbeta", "payload_in_amp2"): "current",
        ("grbbeta", "payload_in_power3"): "power",
        ("lasarsat", "payload_diode_xp"): "attitude",
        ("lasarsat", "payload_vel_x"): "attitude",
        ("lasarsat", "payload_nav_sats"): "position",
        ("lasarsat", "payload_bus_vol"): "voltage",
        ("marina", "payload_psu_battery"): "voltage",
        ("cubebel2", "payload_rx_datarate"): "frequency",
        ("cubebel2", "payload_bus_c"): "current",
        ("cubebel2", "payload_ch1_oc"): "state",
        ("cubebel2", "payload_background_noise"): "signal",
        ("catsat", "payload_batt_heater"): "state",
        ("catsat", "payload_in_eclipse"): "state",
        ("catsat", "payload_1_brn"): "state",
        ("catsat", "payload_rx_baud"): "frequency",
        ("knacksat2", "payload_battery_iout"): "current",
        ("knacksat2", "payload_isens_4"): "current",
        ("knacksat2", "payload_lora_ant"): "state",
        ("frontiersat", "payload_battery_percent"): "charge",
        ("frontiersat", "payload_fs_mounted"): "state",
        ("foresail1", "payload_is_authenticated"): "state",
        ("cp16", "payload_load_5min"): "counter",
        ("cp16", "payload_rx_bytes"): "memory",
        ("norbi", "payload_sop_angle_priority1"): "state",
        ("uwe4", "beacon_payload_vals_out_of_range"): "counter",
    }
    for (dec, f), want in cases.items():
        c = fs.classify(f, dec)
        assert c["kind"] == "measure" and c["measure"] == want, (f, c)
    for dec, f in (("cubebel2", "payload_rtc_unixtime"), ("catsat", "payload_obc_clock"),
                   ("knacksat2", "payload_res_2"), ("fox", "raw_frame_hdr_id"),
                   ("cp16", "payload_var_byte3"), ("cubebel2", "payload_beacon_id")):
        assert fs.classify(f, dec)["kind"] == "scaffolding", f


# --- 3. the JSON decides, not the code ----------------------------------------

def test_the_data_file_is_what_decides(monkeypatch):
    """Point the layer at a variant of the JSON with one override added and
    one measure removed; the answers must follow the data."""
    with open(os.path.join(HERE, "field_semantics.json"), encoding="utf-8") as fh:
        d = json.load(fh)
    d["overrides"]["norbi"] = {"ses_charge_level_m_ah": "scaffolding"}
    d["measures"] = [m for m in d["measures"] if m["measure"] != "memory"]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(d, tmp)
    try:
        fs.reload(tmp.name)
        assert fs.classify("payload_ses_charge_level_m_ah", "norbi")["kind"] == "scaffolding"
        assert fs.classify("freemem")["kind"] == "unknown", "removed measure must stop matching"
    finally:
        os.unlink(tmp.name)
        fs.reload()
    assert fs.classify("freemem")["measure"] == "memory"


def test_overrides_match_the_trailing_segments():
    """NETSAT's `powerpath_a_iup_u` / `_p` leaves mean voltage / power in that
    decoder and nothing anywhere else; the override is scoped to it."""
    f = "ax25_frame_payload_ax25_info_payload_beacon_payload_powerpath_a_iup_u"
    assert fs.classify(f, "netsat")["measure"] == "voltage"
    assert fs.classify(f, "other")["kind"] == "unknown"


# --- 4. the coverage report ----------------------------------------------------

def test_coverage_counts_add_up_and_name_the_work_queue():
    rep = fs.coverage({46494: ["battery_v", "header_length", "payload_brk_temp_active",
                                "payload_sop_magnetic_induction_module", "mystery_x"],
                       60246: ["beacon_types_check", "beacon_types_check"]},
                      {46494: "norbi", 60246: "catsat"}, unknown_sample=1)
    s = rep["satellites"][46494]
    assert s["fields"] == 5 and s["scaffolding"] + s["measure"] + s["unknown"] == 5
    assert s["by_measure"] == {"voltage": 1, "temperature": 1, "attitude": 1}
    assert s["unknown_sample"] == ["mystery_x"], "bounded sample, the work queue"
    assert rep["satellites"][60246]["fields"] == 1, "distinct names, not rows"
    f = rep["fleet"]
    assert f["fields"] == 6 and f["satellites_by_measure"]["temperature"] == 1
    assert abs(f["scaffolding_pct"] + f["measure_pct"] + f["unknown_pct"] - 100) < 0.2


def test_the_api_transport_category_comes_from_the_layer():
    import main
    assert main.field_source("ax25_frame_payload_info_temp") == "telemetry"
    assert main.field_source("beacon_types_type_check_csp_header_source_port") == "transport"
    assert main.field_source("battery_v") == "canonical"


def test_the_coverage_endpoint_answers_on_an_empty_fleet():
    """Shape and liveness: on the test database (no telemetry) the fleet
    report is all zeros, carries its cache age, and a satellite with no
    fields is a 404 rather than an empty report."""
    import main
    from fastapi.testclient import TestClient
    # the context manager runs the lifespan, which is where the pool is made
    with TestClient(main.app) as c:
        r = c.get("/v1/telemetry/coverage?refresh=true")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["fleet"]["fields"] == 0 and body["fleet"]["unknown_pct"] == 0.0
        assert "cached_seconds" in body and body["ttl_seconds"] >= 1
        assert c.get("/v1/telemetry/coverage?norad=46494").status_code == 404


def test_the_refresh_materialises_the_classification_for_grafana():
    """Step 2 (#526): on the test database, two decoded fields for one
    satellite become two rows in field_semantic with the layer's verdict,
    and a re-run after the verdict changes overwrites rather than duplicates."""
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c, main.cursor() as cur:
        cur.execute("INSERT INTO satellite (norad, name, decoder) VALUES (99901, 'SEMTEST', 'norbi') "
                    "ON CONFLICT (norad) DO UPDATE SET decoder = EXCLUDED.decoder")
        cur.execute("INSERT INTO telemetry (norad, ts, field, value_num) VALUES "
                    "(99901, now(), 'payload_ses_median_pdm_temp', 21.5), "
                    "(99901, now(), 'header_length', 42) ON CONFLICT DO NOTHING")
        cur.connection.commit()
        assert c.get("/v1/telemetry/coverage?refresh=true").status_code == 200
        cur.execute("SELECT field, kind, measure, unit, lo, hi FROM field_semantic WHERE norad = 99901 ORDER BY field")
        rows = {f: (k, m, u, lo, hi) for f, k, m, u, lo, hi in cur.fetchall()}
        assert rows["payload_ses_median_pdm_temp"][:3] == ("measure", "temperature", "degC")
        assert rows["payload_ses_median_pdm_temp"][3:] == (-60.0, 120.0), "the plausible range travels with it"
        assert rows["header_length"][0] == "scaffolding"
        assert c.get("/v1/telemetry/coverage?refresh=true").status_code == 200
        cur.execute("SELECT count(*) FROM field_semantic WHERE norad = 99901")
        assert cur.fetchone()[0] == 2, "a second refresh must not duplicate"
        cur.execute("DELETE FROM telemetry WHERE norad = 99901"); cur.execute("DELETE FROM field_semantic WHERE norad = 99901")
        cur.execute("DELETE FROM satellite WHERE norad = 99901"); cur.connection.commit()


def test_a_unit_letter_leaf_decides_the_measure():
    """`tlm_vbat_i` is the current of the battery rail, not a voltage: the
    `vbat` token earlier in the name used to win. A trailing `_i`, `_v` or
    `_t` names the unit of what is measured."""
    assert fs.classify("ax25_frame_payload_data_obc_tlm_vbat_i", "sharjahsat1")["measure"] == "current"
    assert fs.classify("ax25_frame_payload_data_obc_tlm_vbat_v", "sharjahsat1")["measure"] == "voltage"
    assert fs.classify("ax25_frame_payload_data_obc_tlm_3v3_periph_i", "sharjahsat1")["measure"] == "current"
    assert fs.classify("payload_bat_t")["measure"] == "temperature"
