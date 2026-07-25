"""Decoded-field categorizer tests (#46).

The native "latest decoded fields" panel colours each field by where it comes
from, so real satellite health stands out from link-layer framing. This locks
the categorizer's mapping so the colouring can't silently drift.
"""
import os

os.environ.setdefault("DB_DSN", "dbname=orbit user=orbit password=orbit host=localhost port=5432")
import main  # noqa: E402


def test_canonical_health_fields():
    for f in ("battery_v", "battery_i", "battery_pct"):
        assert main.field_source(f) == "canonical", f


def test_transport_framing_fields():
    for f in ("packet_header_csp_header_priority", "csp_header_source",
              "ax25_frame_length", "frame_length", "crc", "callsign",
              "sequence_count", "primary_header_version"):
        assert main.field_source(f) == "transport", f


def test_payload_telemetry_is_default():
    for f in ("temp_1", "temperature_mcu", "solar_panel_x_current",
              "obc_mode", "rssi", "uptime_s"):
        assert main.field_source(f) == "telemetry", f


def test_categorizer_is_case_insensitive():
    assert main.field_source("Battery_V") == "canonical"
    assert main.field_source("CSP_HEADER_DEST") == "transport"
