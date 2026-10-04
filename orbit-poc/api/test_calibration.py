"""Calibration unit tests — CubeBel-2 raw register -> physical units.

Derived from Vlad Chorney's (EU1SAT) report that CUBEBEL-2 temperatures decode
wrong, and from the satellite's own SatNOGS dashboard. Pure-function tests; the
runner copies /src/ingest so `calibration` is importable.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingest"))
try:
    from calibration import calibrate, canonical_from, CALIBRATION
except Exception:                                    # pragma: no cover
    calibrate = None

P = "ax25_frame_payload_ax25_info_cdm_payload_"
BEACON = "ax25_frame_payload_ax25_info_trx_beacon_"


@pytest.mark.skipif(calibrate is None, reason="ingest/calibration not available")
def test_cubebel2_temperatures_become_physical():
    f = {
        P + "adc_temp_1": 64928,          # unsigned; -608 signed -> -4.74 C
        P + "adc_temp_2": 65408,          # -128 signed -> -1.00 C
        P + "tmp75_temp": 254.3125,       # signed wrap at 256 -> -1.69 C
        BEACON + "beacon_pamp_temp": 3255,  # * 0.001 -> 3.26 C
        P + "common_trx_mcu_temp": -2.0,  # already C — must stay untouched
    }
    calibrate("cubebel2", f)
    assert abs(f[P + "adc_temp_1"] - (-4.7424)) < 0.01
    assert abs(f[P + "adc_temp_2"] - (-0.9984)) < 0.01
    assert abs(f[P + "tmp75_temp"] - (-1.6875)) < 0.001
    assert abs(f[BEACON + "beacon_pamp_temp"] - 3.255) < 0.001
    assert f[P + "common_trx_mcu_temp"] == -2.0       # no rule -> unchanged
    # every calibrated temperature is now physically plausible
    for k in (P + "adc_temp_1", P + "adc_temp_2", P + "tmp75_temp"):
        assert -60.0 <= f[k] <= 60.0, f"{k}={f[k]} implausible"


@pytest.mark.skipif(calibrate is None, reason="ingest/calibration not available")
def test_cubebel2_battery_v_from_vbus_not_trx_rail():   # Vlad's battery-voltage bug
    # beacon_vbus is the bus/battery voltage (SatNOGS *0.001); the 5 V TRX rail
    # ina226_pamp_voltage must NOT become battery_v
    f = {
        BEACON + "beacon_vbus": 4140,                 # raw -> 4.14 V
        P + "ina226_pamp_voltage": 5.02,              # a TRX rail, must be ignored
    }
    calibrate("cubebel2", f)
    assert abs(f[BEACON + "beacon_vbus"] - 4.14) < 0.001
    canon = dict(canonical_from("cubebel2", f))
    assert abs(canon["battery_v"] - 4.14) < 0.001     # battery_v comes from vbus
    assert not (3.0 <= canon["battery_v"] <= 5.05 and abs(canon["battery_v"] - 5.02) < 0.01)


@pytest.mark.skipif(calibrate is None, reason="ingest/calibration not available")
def test_unknown_decoder_is_noop():
    f = {P + "adc_temp_1": 64928}
    calibrate("netsat", f)               # no rules for netsat
    assert f[P + "adc_temp_1"] == 64928


@pytest.mark.skipif(calibrate is None, reason="ingest/calibration not available")
def test_cubebel2_registered():
    assert "cubebel2" in CALIBRATION


def test_ingest_image_ships_calibration():
    # the ingest container imports calibration at startup — the Dockerfile must
    # COPY it or the service crashes (like the satnogs_dashboards.json gap)
    df = os.path.join(os.path.dirname(__file__), "..", "ingest", "Dockerfile")
    if not os.path.exists(df):
        pytest.skip("ingest not available")
    assert "calibration.py" in open(df, encoding="utf-8").read()


S = "ax25_frame_payload_ax25_info_data_"


@pytest.mark.skipif(calibrate is None, reason="ingest/calibration not available")
def test_sharjahsat1_raw_registers_become_physical():
    """From the formulas in satnogs-decoders' sharjahsat1.ksy. The raw values
    are what our rows hold (2026-10-04): battery vbat ~830, tbrd ~740,
    ibcra ~18000 — read on the Current panel as 18 A before this."""
    f = {
        S + "battery_vbat": 830,                 # x*0.008993 -> 7.46 V
        S + "battery_ibat": -120,                # x*14.662757 mA -> -1.76 A (signed)
        S + "battery_ipcm3v3": 70,               # battery board: x*1.327547 mA -> 0.093 A
        S + "eps_ipcm3v3": 85,                   # eps board, SAME leaf: x*0.00682 A -> 0.58 A
        S + "eps_tbrd": 740,                     # (x*0.372434)-273.15 -> 2.45 degC
        S + "eps_tbrd_db": 744,                  # distinct from eps_tbrd
        S + "battery_tbat1": 700,                # (x*0.3976)-238.57 -> 39.75 degC
        S + "solar_panels_ibcra3": 18000,        # x*0.0009775 -> 17.6 A (a bright panel)
        S + "solar_panels_tbcrb6": 600,          # (x*0.4963)-273.15 -> 24.6 degC
        S + "solar_panels_vbcr3": 300,           # the one divider that differs
        S + "solar_panels_vbcr4": 300,
        S + "obc_tlm_vbat_i": 549,               # x/1000 -> 0.549 A
        S + "obc_tlm_board_temp1": 1220,         # x/100 -> 12.2 degC
        S + "s_band_modem_pa_temperature": 1000,  # ((x*3/4096)-0.5)*100 -> 23.2 degC
        S + "uhf_vhf_modem_pa_temp": 193,        # deliberately NOT calibrated (ambiguous sign law)
    }
    calibrate("sharjahsat1", f)
    assert abs(f[S + "battery_vbat"] - 7.464) < 0.01
    assert abs(f[S + "battery_ibat"] - (-1.7595)) < 0.01
    assert abs(f[S + "battery_ipcm3v3"] - 0.0929) < 0.001
    assert abs(f[S + "eps_ipcm3v3"] - 0.5797) < 0.001, "same leaf, other board, other formula"
    assert abs(f[S + "eps_tbrd"] - 2.45) < 0.05
    assert abs(f[S + "eps_tbrd_db"] - 3.94) < 0.05
    assert abs(f[S + "battery_tbat1"] - 39.75) < 0.05
    assert abs(f[S + "solar_panels_ibcra3"] - 17.595) < 0.01
    assert abs(f[S + "solar_panels_tbcrb6"] - 24.63) < 0.05
    assert abs(f[S + "solar_panels_vbcr3"] - 2.991) < 0.01
    assert abs(f[S + "solar_panels_vbcr4"] - 9.677) < 0.01
    assert abs(f[S + "obc_tlm_vbat_i"] - 0.549) < 0.001
    assert abs(f[S + "obc_tlm_board_temp1"] - 12.2) < 0.001
    assert abs(f[S + "s_band_modem_pa_temperature"] - 23.24) < 0.05
    assert f[S + "uhf_vhf_modem_pa_temp"] == 193


@pytest.mark.skipif(calibrate is None, reason="ingest/calibration not available")
def test_sharjahsat1_battery_canonicals_come_from_the_battery_board():
    """The heuristic had picked a 1.3 V source for battery_v on the live
    site. The battery is `battery_vbat` (x*0.008993), its current `battery_ibat`."""
    f = {S + "battery_vbat": 830, S + "battery_ibat": -120, S + "obc_tlm_vbat_v": 1980}
    calibrate("sharjahsat1", f)
    canon = dict(canonical_from("sharjahsat1", f))
    assert abs(canon["battery_v"] - 7.464) < 0.01
    assert abs(canon["battery_i"] - (-1.7595)) < 0.01
    assert "sharjahsat1" in CALIBRATION
