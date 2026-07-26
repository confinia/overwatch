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
    from calibration import calibrate, CALIBRATION
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
