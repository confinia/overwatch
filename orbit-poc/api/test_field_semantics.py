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
    """A guess is worse than a gap: `analog3` and `misc` get no measure."""
    for f in ("io86_type_check_ax25_frame_payload_ax25_info_analog3",
              "ax25_frame_payload_ax25_info_compass_misc", "raw_frame_rt_tlm_ihu_diag_data"):
        assert fs.classify(f)["kind"] == "unknown", f


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
