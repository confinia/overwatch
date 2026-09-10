"""Guards #454: a SATNGS LoRa station's CSV log becomes reception rows (with
the station's RSSI / SNR), raw frames kept for a later decoder, and a
satellite we did not track yet. Re-polling the same rolling log changes
nothing. Against a real Postgres; no network (the CSV is inline)."""
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from conftest import require_test_db

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))
os.environ.setdefault("DB_DSN", "")
import main  # noqa: E402
import satngs  # noqa: E402

DSN = os.environ.get("DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="no database")
OBS = "TEST0X-GF05tj"                     # callsign + Maidenhead, SatNOGS style
SATS = (99901, 99902)

NOW = int(datetime.now(timezone.utc).timestamp())
CSV = "unix,local_time,norad,sat,rssi_dbm,snr_db,ferr_hz,len,hex\n" + "\n".join([
    f"{NOW-300},10/09 10:57:11,99901,KOSAR-T,-105.0,-6.0,818,4,54494E59",
    f"{NOW-299},10/09 10:57:12,99901,KOSAR-T,-97.8,-0.8,969,4,54494E5A",
    f"{NOW-200},10/09 10:58:51,99902,HUC-T,-112.3,-11.3,-2714,2,FFFF",
    f"{NOW+7200},10/09 12:58:51,99902,HUC-T,-112.3,-11.3,-2714,2,FFFF",   # clock ahead
    f"{NOW-100},10/09 11:00:31,notanumber,X,-1,-1,0,2,FFFF",             # junk
    f"{NOW-90},10/09 11:00:41,99902,HUC-T,-112.3,-11.3,-2714,2,ZZZZ",     # not hex
]) + "\n"


@pytest.fixture
def conn():
    require_test_db()
    c = psycopg2.connect(DSN)
    with c, c.cursor() as cur:
        cur.execute(main.KEYS_SQL)
    scrub(c)
    yield c
    scrub(c)
    c.close()


def scrub(c):
    with c, c.cursor() as cur:
        for t in ("frame", "reception", "telemetry"):
            cur.execute(f"DELETE FROM {t} WHERE norad = ANY(%s)", (list(SATS),))
        cur.execute("DELETE FROM satellite WHERE norad = ANY(%s)", (list(SATS),))


def _ingest(conn):
    import ingest                                             # noqa: E402
    frames = satngs.parse_log(CSV, OBS)
    return satngs.store(conn, frames, ingest._store_frames, ingest._decoder_for)


def test_parse_keeps_only_usable_rows():
    frames = satngs.parse_log(CSV, OBS)
    assert [f["norad"] for f in frames] == [99901, 99901, 99902]
    assert frames[0]["frame"] == "54494E59" and frames[0]["app_source"] == "satngs"
    assert frames[0]["rssi_dbm"] == -105.0 and frames[0]["snr_db"] == -6.0
    assert frames[0]["timestamp"].endswith("Z")


def test_stations_spec_is_forgiving():
    assert satngs.stations("https://station.satngs.net=LW2DTZ") == \
        [("https://station.satngs.net", "LW2DTZ")]
    assert satngs.stations(" https://a.example/=A-JN03 , junk, =B, https://b.example=B ") == \
        [("https://a.example", "A-JN03"), ("https://b.example", "B")]
    assert satngs.stations("") == []


def test_a_log_becomes_reception_frames_and_a_new_satellite(conn):
    sats, n = _ingest(conn)
    assert (sats, n) == (2, 3)
    with conn.cursor() as cur:
        cur.execute("SELECT norad, name, has_telemetry FROM satellite "
                    "WHERE norad = ANY(%s) ORDER BY 1", (list(SATS),))
        assert cur.fetchall() == [(99901, "KOSAR-T", False), (99902, "HUC-T", False)]
        cur.execute("SELECT norad, observer, source, lat, lon, rssi_dbm, snr_db "
                    "FROM reception WHERE norad = ANY(%s) ORDER BY ts", (list(SATS),))
        rows = cur.fetchall()
        assert len(rows) == 3
        norad, obs, src, lat, lon, rssi, snr = rows[0]
        assert (norad, obs, src, rssi, snr) == (99901, OBS, "satngs", -105.0, -6.0)
        assert lat is not None and lon is not None, "grid locator placed the station"
        cur.execute("SELECT hex, source FROM frame WHERE norad = 99902")
        assert cur.fetchall() == [("FFFF", "satngs")]
        cur.execute("SELECT count(*) FROM telemetry WHERE norad = ANY(%s)", (list(SATS),))
        assert cur.fetchone()[0] == 0, "no decoder: no telemetry rows, no crash"


def test_repolling_the_rolling_log_is_idempotent(conn):
    _ingest(conn)
    _ingest(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM reception WHERE norad = ANY(%s)", (list(SATS),))
        assert cur.fetchone()[0] == 3
        cur.execute("SELECT count(*) FROM frame WHERE norad = ANY(%s)", (list(SATS),))
        assert cur.fetchone()[0] == 3


def test_a_tracked_satellite_is_never_renamed(conn):
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO satellite (norad, name, has_telemetry, note) "
                    "VALUES (99901, 'KOSAR-1.5 (curated)', false, 'ours')")
    _ingest(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT name, note FROM satellite WHERE norad = 99901")
        assert cur.fetchone() == ("KOSAR-1.5 (curated)", "ours")


def test_the_ingest_starts_the_loop_only_when_configured(monkeypatch):
    monkeypatch.setattr(satngs, "SATNGS_STATIONS", "")
    assert satngs.stations() == []
    monkeypatch.setattr(satngs, "SATNGS_STATIONS", "https://s.example=S")
    assert satngs.stations() == [("https://s.example", "S")]
