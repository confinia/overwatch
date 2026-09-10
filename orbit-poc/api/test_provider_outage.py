"""Guards #452: a cut in an upstream (the SatNOGS API was unreachable for two
days in September, four in August) must be VISIBLE on the dashboards, because
once access returns the ingest backfills the frames and the hole in the data
closes over. The gateway derives `provider_outage` rows from its request log;
every public dashboard draws them as annotations; the datasource role can read
the intervals and nothing more of the log. Against a real Postgres.
"""
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from conftest import require_test_db

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import main  # noqa: E402
import gateway  # noqa: E402

DSN = os.environ["DB_DSN"]
PUBLIC_DIR = os.path.join(HERE, "..", "grafana", "dashboards", "public")
OPS_DIR = os.path.join(HERE, "..", "grafana", "ops-dashboards")
SRC = "test-upstream"                # never 'satnogs': the live rows stay alone


@pytest.fixture
def cur():
    require_test_db()
    conn = psycopg2.connect(DSN)
    with conn, conn.cursor() as c:
        c.execute(main.KEYS_SQL)
        c.execute("DELETE FROM provider_outage WHERE source = %s", (SRC,))
        c.execute("DELETE FROM upstream_request WHERE source = %s", (SRC,))
    with conn:
        with conn.cursor() as c:
            yield c
    with conn, conn.cursor() as c:
        c.execute("DELETE FROM provider_outage WHERE source = %s", (SRC,))
        c.execute("DELETE FROM upstream_request WHERE source = %s", (SRC,))
    conn.close()


def _log(cur, minutes_ago, status):
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    cur.execute("INSERT INTO upstream_request (source, endpoint, status, ms, ts) "
                "VALUES (%s, '/api/telemetry/', %s, 100, %s)", (SRC, status, ts))


def _outages(cur):
    cur.execute("SELECT started, ended FROM provider_outage WHERE source = %s "
                "ORDER BY started", (SRC,))
    return cur.fetchall()


def test_a_blip_is_not_an_outage(cur):
    """One 429, one timeout, answers around them: nothing to draw."""
    _log(cur, 40, 200)
    _log(cur, 30, 429)
    _log(cur, 29, 200)
    _log(cur, 20, None)                       # timeout
    _log(cur, 19, 200)
    gateway.roll_up_outages(cur, SRC, after=900)
    assert _outages(cur) == []


def test_a_cut_opens_after_the_threshold_and_closes_on_the_next_answer(cur):
    _log(cur, 60, 200)
    _log(cur, 50, None)                       # first failure: the cut starts here
    _log(cur, 40, None)
    _log(cur, 30, 429)
    gateway.roll_up_outages(cur, SRC, after=900)
    rows = _outages(cur)
    assert len(rows) == 1 and rows[0][1] is None, "open while it lasts"
    started = rows[0][0]
    assert abs((datetime.now(timezone.utc) - started).total_seconds() - 50 * 60) < 5
    # still failing: the same row, no second one
    _log(cur, 10, None)
    gateway.roll_up_outages(cur, SRC, after=900)
    assert len(_outages(cur)) == 1
    # the first answer closes it, at the answer's time
    _log(cur, 5, 200)
    gateway.roll_up_outages(cur, SRC, after=900)
    rows = _outages(cur)
    assert len(rows) == 1 and rows[0][0] == started
    assert abs((datetime.now(timezone.utc) - rows[0][1]).total_seconds() - 5 * 60) < 5
    # a new cut after the recovery is a NEW row
    _log(cur, 4, None)
    gateway.roll_up_outages(cur, SRC, after=60)
    assert len(_outages(cur)) == 2


def test_a_failure_younger_than_the_threshold_is_not_yet_a_cut(cur):
    _log(cur, 20, 200)
    _log(cur, 5, None)
    gateway.roll_up_outages(cur, SRC, after=900)
    assert _outages(cur) == []
    gateway.roll_up_outages(cur, SRC, after=60)      # 5 min > 1 min
    assert len(_outages(cur)) == 1


def test_an_answer_that_is_not_a_200_still_counts_as_reachable(cur):
    """A 404 for an unknown object is SatNOGS answering; only silence, a
    refusal or a server error is a failure."""
    _log(cur, 60, 200)
    _log(cur, 50, 404)
    _log(cur, 40, 404)
    gateway.roll_up_outages(cur, SRC, after=900)
    assert _outages(cur) == []
    _log(cur, 30, 502)
    _log(cur, 20, 503)
    gateway.roll_up_outages(cur, SRC, after=900)
    assert len(_outages(cur)) == 1


def test_the_gateway_records_through_the_roll_up(monkeypatch):
    """make_recorder's callback must reach roll_up_outages; a recorder that
    only logs would leave the table empty forever."""
    require_test_db()
    calls = []
    monkeypatch.setattr(gateway, "roll_up_outages", lambda cur: calls.append(1))
    gateway.make_recorder(DSN)("/api/telemetry/", 200, 12, "test")
    assert calls == [1]


def _annotation_sql(d):
    return [a["target"].get("rawSql", "") for a in d.get("annotations", {}).get("list", [])
            if "provider_outage" in json.dumps(a)]


def test_every_public_dashboard_draws_the_outages():
    files = glob.glob(os.path.join(PUBLIC_DIR, "*.json"))
    assert len(files) >= 5
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        sqls = _annotation_sql(d)
        assert sqls, f"{os.path.basename(f)}: no provider_outage annotation"
        for q in sqls:
            assert "coalesce(ended, now())" in q.lower(), "an ongoing cut draws up to now"
            assert "$__timefrom" in q.lower() or "__timefilter" in q.lower(), "must be range-bound"


def test_the_satnogs_ops_boards_draw_the_outages_too():
    for name in ("satnogs-gateway.json", "upstream-requests.json"):
        d = json.load(open(os.path.join(OPS_DIR, name), encoding="utf-8"))
        assert _annotation_sql(d), f"{name}: no provider_outage annotation"


def test_the_datasource_role_sees_intervals_not_the_log():
    assert "provider_outage" in main.GRAFANA_PUBLIC_TABLES
    assert "upstream_request" not in main.GRAFANA_PUBLIC_TABLES
