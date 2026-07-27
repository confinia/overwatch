"""Usage-metering unit tests (POLAR.md) — dry-run, no DB / no Polar creds."""
import datetime
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import metering


def test_period_is_utc_month():
    d = datetime.datetime(2026, 7, 27, 23, 0, tzinfo=datetime.timezone.utc)
    assert metering._period(d) == "2026-07"


def test_dry_run_by_default():
    # with no Polar token / env off, metering must dry-run (provable, no money)
    assert metering.POLAR_ENV == "off" or not metering.POLAR_ORG_TOKEN


def test_dim_col_covers_the_three_dimensions():
    assert set(metering.DIM_COL) == {"frame_ingested", "tm_request", "tc_request"}
    assert set(metering.DIM_COL.values()) == {"frames", "tm_count", "tc_count"}


def test_emit_dry_run_shape(caplog):
    caplog.set_level(logging.INFO, logger="metering")
    metering._emit("cust-1", "frame_ingested", 5, {"satellite": "CLEMSAT-1"})
    lines = [r.getMessage() for r in caplog.records if "METER dry-run" in r.getMessage()]
    assert lines, "dry-run must log the event it would send"
    payload = json.loads(lines[0].split("METER dry-run ", 1)[1])
    assert payload["name"] == "frame_ingested"
    assert payload["external_customer_id"] == "cust-1"
    assert payload["metadata"]["quantity"] == 5
    assert payload["metadata"]["satellite"] == "CLEMSAT-1"


def test_emit_never_raises_into_request():
    # even a bad customer / metadata must not raise (billing must never 500 a push)
    metering._emit(None, "tm_request", 1, None)


def test_api_image_ships_metering():
    # main.py imports metering at startup — the Dockerfile must COPY it or the
    # api container crashes on boot (same class of bug as calibration.py)
    df = os.path.join(os.path.dirname(__file__), "Dockerfile")
    assert "COPY metering.py" in open(df, encoding="utf-8").read()
