"""Guards #458: NORBI (46494) frames decode with the decoder vendored under
ingest/decoders (LW2DTZ's norbi.ksy, pending upstream), raw frames kept by
#455 are replayed into telemetry once a decoder exists, each tried once, and
a satellite that gains a decoder later gets its frames re-armed."""
import os
import sys

import psycopg2
import pytest

from conftest import require_test_db

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))
os.environ.setdefault("DB_DSN", "")
import main  # noqa: E402
import ingest  # noqa: E402
from calibration import calibrate, canonical_from  # noqa: E402

DSN = os.environ.get("DB_DSN")
NORAD = 46494
# A real TMI-0 frame heard by DK3WN (satblog.info/norbi-lora-telemetry): 143
# bytes = 15-byte header + 128-byte payload. The blog's dump stops after 113
# bytes, so the tail is zero-filled here except ses_voltage (payload bytes
# 124-125, LE mV), set to 8000 so the battery calibration has something to
# scale; every other field asserted below sits in the real part.
HEX = ("8effffffff0a0601c9066900000000f10f000069060641ed2742524b204d57205645523a30325f3132"
       "0000000000000e0000dc05000000021c0002c80a8700ac0000000228201a68"
       "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
       "000025bffc00600217350e0c0c0c00001")
HEX = (HEX + "0" * (286 - len(HEX))).upper()
HEX = HEX[:278] + "401F" + HEX[282:]           # ses_voltage = 0x1F40 = 8000 mV
OBS = "TEST0N-JN49"


def test_the_vendored_decoder_is_used_when_the_release_lacks_it():
    with pytest.raises(ModuleNotFoundError):
        __import__("satnogsdecoders.decoder.norbi")
    fields = ingest._decode_frame("norbi", HEX)
    assert fields["payload_frame_number"] == 1641
    assert fields["payload_frame_generation_time"] == 669860102   # 2021, 2000 epoch
    assert fields["payload_brk_temp_active"] == 28 and fields["payload_ms_temp"] == 26
    assert fields["payload_brk_transmitter_power_active"] == 2
    assert fields["payload_brk_last_received_packet_rssi_active"] == -121
    assert fields["payload_ses_voltage"] == 8000
    assert fields["payload_sop_altitude_glonass"] == 0        # no GLONASS fix in this frame


def test_norbi_calibration_and_canonical_battery():
    fields = {"payload_ses_voltage": 8240, "payload_sop_latitude_glonass": 551234567,
              "payload_brk_last_received_packet_snr_active": -32}
    calibrate("norbi", fields)
    assert fields["payload_ses_voltage"] == pytest.approx(8.24)
    assert fields["payload_sop_latitude_glonass"] == pytest.approx(55.1234567)
    assert fields["payload_brk_last_received_packet_snr_active"] == -8
    assert canonical_from("norbi", fields) == [("battery_v", pytest.approx(8.24))]


def test_norbi_is_seeded_with_its_decoder_and_shipped_in_the_image():
    from satellites import SHOWCASE
    sat = next(s for s in SHOWCASE if s["norad"] == NORAD)
    assert sat["decoder"] == "norbi" and sat["telemetry"]
    docker = open(os.path.join(HERE, "..", "ingest", "Dockerfile"), encoding="utf-8").read()
    assert "COPY decoders ./decoders" in docker
    assert os.path.exists(os.path.join(HERE, "..", "ingest", "decoders", "norbi.py"))


@pytest.fixture
def conn():
    require_test_db()
    c = psycopg2.connect(DSN)
    with c, c.cursor() as cur:
        cur.execute(main.KEYS_SQL)
    scrub(c)
    with c, c.cursor() as cur:
        cur.execute("INSERT INTO satellite (norad, name, has_telemetry, decoder, note) "
                    "VALUES (%s, 'NORBI', true, 'norbi', 'test')", (NORAD,))
        cur.execute("INSERT INTO frame (norad, ts, observer, source, hex) VALUES "
                    "(%s, '2026-09-10T01:15:03Z', %s, 'satngs', %s), "
                    "(%s, '2026-09-10T01:17:03Z', %s, 'satngs', 'FFFF')",
                    (NORAD, OBS, HEX, NORAD, OBS))
    yield c
    scrub(c)
    c.close()


def scrub(c):
    with c, c.cursor() as cur:
        for t in ("frame", "reception", "telemetry"):
            cur.execute(f"DELETE FROM {t} WHERE norad = %s", (NORAD,))
        cur.execute("DELETE FROM satellite WHERE norad = %s", (NORAD,))


@pytest.mark.skipif(not DSN, reason="no database")
def test_stored_frames_are_replayed_into_telemetry_once(conn):
    assert ingest.replay_frames() == 1            # the real frame; FFFF is not TMI-0
    with conn.cursor() as cur:
        cur.execute("SELECT field, value_num FROM telemetry WHERE norad = %s "
                    "AND field IN ('payload_frame_number', 'battery_v')", (NORAD,))
        got = dict(cur.fetchall())
        assert got["payload_frame_number"] == 1641 and got["battery_v"] == pytest.approx(8.0)
        cur.execute("SELECT count(*) FROM frame WHERE norad = %s AND replayed IS NULL", (NORAD,))
        assert cur.fetchone()[0] == 0, "both frames tried, the undecodable one too"
    assert ingest.replay_frames() == 0, "nothing is tried twice"


@pytest.mark.skipif(not DSN, reason="no database")
def test_a_decoder_that_arrives_later_rearms_the_frames(conn):
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE frame SET replayed = now() WHERE norad = %s", (NORAD,))
    # tried before the decoder existed: no telemetry, decoder now set
    assert ingest._rearm_replay() == 2
    assert ingest.replay_frames() == 1
    assert ingest._rearm_replay() == 0, "once telemetry exists, nothing to re-arm"
