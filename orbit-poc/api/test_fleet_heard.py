"""Guards #464: the fleet list's "heard" (last_frame) counts ANY reception,
not only decoded telemetry. With the SatNOGS API cut, the SATNGS LoRa
stations are the live signal — a satellite they hear every pass must not
wear the red "silent for a week" dot just because it has no decoder yet.

web/app.py needs flask, which the test env does not install, so the query
is read out of the source with `ast` and run against the real test DB.
"""
import ast
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from conftest import require_test_db

HERE = os.path.dirname(__file__)
DSN = os.environ.get("DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="no database")
SATS = (99921, 99922, 99923)
NOW = datetime.now(timezone.utc)


def fleet_sql():
    src = open(os.path.join(HERE, "..", "web", "app.py"), encoding="utf-8").read()
    for node in ast.parse(src).body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "FLEET_SQL" for t in node.targets)):
            return node.value.value
    raise AssertionError("FLEET_SQL not found in web/app.py")


@pytest.fixture
def conn():
    require_test_db()
    c = psycopg2.connect(DSN)
    scrub(c)
    with c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO satellite (norad, name, has_telemetry) VALUES "
            "(99921, 'RXONLY', false), (99922, 'DECODED', true), "
            "(99923, 'NEVERHEARD', false)")
        cur.execute(
            "INSERT INTO reception (norad, ts, observer, source) VALUES "
            "(99921, %s, 'TEST0X', 'satngs'), (99922, %s, 'TEST0X', 'satngs')",
            (NOW - timedelta(hours=2), NOW - timedelta(hours=1)))
        cur.execute(
            "INSERT INTO telemetry (norad, ts, field, value_num) VALUES "
            "(99922, %s, 'battery_v', 8.1)", (NOW - timedelta(days=6),))
    yield c
    scrub(c)
    c.close()


def scrub(c):
    with c, c.cursor() as cur:
        for t in ("reception", "telemetry"):
            cur.execute(f"DELETE FROM {t} WHERE norad = ANY(%s)", (list(SATS),))
        cur.execute("DELETE FROM satellite WHERE norad = ANY(%s)", (list(SATS),))


def rows(conn):
    with conn.cursor() as cur:
        cur.execute(fleet_sql())
        cols = [d.name for d in cur.description]
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()
                if r[0] in SATS}


def test_a_reception_alone_counts_as_heard(conn):
    got = rows(conn)
    assert got[99921]["last_frame"] is not None, \
        "a satellite a LoRa station heard must not read as silent"
    assert abs((got[99921]["last_frame"] - (NOW - timedelta(hours=2)))
               .total_seconds()) < 2


def test_heard_is_the_newest_of_telemetry_and_reception(conn):
    got = rows(conn)
    # reception (1h ago) is newer than the last decoded frame (6d ago)
    assert abs((got[99922]["last_frame"] - (NOW - timedelta(hours=1)))
               .total_seconds()) < 2


def test_never_heard_stays_empty(conn):
    assert rows(conn)[99923]["last_frame"] is None
