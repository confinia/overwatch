"""Guards issue #97: a station's kind is labelled, and only when known.

DL7NDR, verbatim: "How can I distinguish between my station DL7NDR-JN48ap
(upload via SiDS) and my real network station DL7NDR UHF-Turnstile?" One
operator runs both kinds under one callsign; the UI used to call everything
a "volunteer ground station (SatNOGS)" — wrong for a SiDS uploader.
"""
import os

HERE = os.path.dirname(__file__)


def _read(*parts):
    return open(os.path.join(HERE, *parts), encoding="utf-8").read()


def test_ingest_records_how_the_frame_arrived():
    src = _read("..", "ingest", "ingest.py")
    assert 'f.get("app_source")' in src, \
        "SatNOGS app_source is the only honest basis for a station kind"
    assert "observer, lat, lon, source" in src, "reception must store it"


def test_the_schema_carries_the_column_for_old_and_new_databases():
    assert "source       TEXT" in _read("..", "db", "init.sql")
    assert ("ALTER TABLE IF EXISTS reception ADD COLUMN IF NOT EXISTS source"
            in _read("main.py")), "existing databases migrate at api startup"


def test_both_station_endpoints_expose_the_kind():
    src = _read("main.py")
    assert src.count("mode() WITHIN GROUP (ORDER BY source)") == 2, \
        "the list and the health endpoint must both say how frames arrive"


def test_the_ui_labels_only_what_it_knows():
    js = _read("..", "web", "static", "app.js")
    assert "SatNOGS network station" in js
    assert "direct-upload station (SiDS)" in js
    assert "volunteer ground station" in js, \
        "an unknown kind must fall back to a claim that is always true"
