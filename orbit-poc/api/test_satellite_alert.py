"""Guards issue #412: a satellite alert must rule out the two innocent causes.

A station is quiet for one interesting reason, its own equipment. A satellite
has two boring ones that must be excluded first: our ingest being blocked (the
fleet clause, which kept the 2026-08-20 outage from paging anyone), and the
few stations that usually hear it being down. Alerting on the second would
tell an operator their spacecraft went quiet when the spacecraft is fine and
the ground is broken.
"""
import datetime as dt
import os
import sys

import psycopg2
import pytest

from conftest import require_test_db

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402

DSN = os.environ.get("DB_DSN")
SRC = os.path.join(os.path.dirname(__file__), "main.py")

QUIET, LOUD = 99981, 99982          # the watched satellite, and the fleet's
S1, S2 = "TEST-LIS-1-JN00aa", "TEST-LIS-2-JN00bb"


# --- source-level: the alert must not invent its own threshold -------------

def test_the_sender_runs_the_detector_not_its_own_rule():
    src = open(SRC, encoding="utf-8").read()
    fn = src[src.index("def _check_watched_satellites("):]
    fn = fn[:fn.index("\ndef ")]
    assert "SATELLITE_HEALTH_SQL" in fn, \
        "the sender must reuse the detector, or it stops inheriting its clauses"


def test_both_suppression_clauses_are_in_the_detector():
    src = open(SRC, encoding="utf-8").read()
    sql = src[src.index("SATELLITE_HEALTH_SQL = "):]
    sql = sql[:sql.index('"""', sql.index('"""') + 3)]
    assert "fleet_recent >= s.fleet_base" in sql, "missing the fleet clause"
    assert "lis_recent >= l.lis_base" in sql, "missing the listener clause"


def test_the_sender_is_wired_into_the_push_loop():
    src = open(SRC, encoding="utf-8").read()
    loop = src[src.index("def _push_watch_loop("):]
    assert "_check_watched_satellites()" in loop[:1200]


# --- behavioural: the listener clause actually suppresses ------------------

pytestmark_db = pytest.mark.skipif(not DSN, reason="no database")


@pytest.fixture()
def cur():
    require_test_db()
    conn = psycopg2.connect(DSN)

    def scrub(c):
        c.execute("DELETE FROM reception WHERE norad IN %s", ((QUIET, LOUD),))
        c.execute("DELETE FROM station_opportunity WHERE observer IN %s", ((S1, S2),))
        c.execute("DELETE FROM station_daily WHERE observer IN %s", ((S1, S2),))
        c.execute("DELETE FROM satellite WHERE norad IN %s", ((QUIET, LOUD),))

    with conn, conn.cursor() as c:
        scrub(c)
    with conn:
        with conn.cursor() as c:
            yield c
    with conn, conn.cursor() as c:
        scrub(c)
    conn.close()


def _seed(cur, listeners_stay_healthy: bool):
    """21 days: both satellites heard well, then QUIET collapses on the last 3.

    `listeners_stay_healthy` decides the only thing under test: whether the
    stations that hear QUIET keep hearing LOUD normally.
    """
    today = dt.date.today()
    for norad, name in ((QUIET, "TEST-QUIET"), (LOUD, "TEST-LOUD")):
        cur.execute("INSERT INTO satellite (norad, name) VALUES (%s, %s)",
                    (norad, name))
    for back in range(21, 0, -1):
        day = today - dt.timedelta(days=back)
        recent = back <= 3
        for st in (S1, S2):
            for norad in (QUIET, LOUD):
                cur.execute("INSERT INTO station_opportunity "
                            "(observer, day, norad, passes, best_max_el) "
                            "VALUES (%s, %s, %s, 4, 30)", (st, day, norad))
            # frames actually decoded, per satellite
            heard = {QUIET: 0 if recent else 8, LOUD: 8}
            if recent and not listeners_stay_healthy:
                heard[LOUD] = 0          # the ground went down, not the sky
            for norad, n in heard.items():
                for i in range(n):
                    cur.execute("INSERT INTO reception (norad, ts, observer) "
                                "VALUES (%s, %s::date + %s * interval '1 hour', %s)",
                                (norad, day, i, st))
            cur.execute("INSERT INTO station_daily "
                        "(observer, day, frames, satellites_heard) "
                        "VALUES (%s, %s, %s, %s)",
                        (st, day, sum(heard.values()),
                         sum(1 for v in heard.values() if v)))


def _quiet_norads(cur):
    cur.execute(main.SATELLITE_HEALTH_SQL, {
        "as_of": dt.date.today().isoformat(),
        "window": main.HEALTH_RECENT_DAYS + main.HEALTH_BASELINE_DAYS,
        "recent": main.HEALTH_RECENT_DAYS,
        "min_days": main.HEALTH_MIN_DAYS,
        "min_base": main.HEALTH_MIN_BASELINE,
        "collapse": main.HEALTH_COLLAPSE,
        "fleet_ok": main.HEALTH_FLEET_OK,
    })
    return {r[0] for r in cur.fetchall()}


@pytestmark_db
def test_a_satellite_that_really_went_quiet_is_reported(cur):
    _seed(cur, listeners_stay_healthy=True)
    assert QUIET in _quiet_norads(cur), \
        "its own reception collapsed while fleet and listeners kept working"


@pytestmark_db
def test_a_satellite_whose_listeners_went_down_is_NOT_reported(cur):
    _seed(cur, listeners_stay_healthy=False)
    assert QUIET not in _quiet_norads(cur), \
        "the ground is broken, not the spacecraft: this must page nobody"
