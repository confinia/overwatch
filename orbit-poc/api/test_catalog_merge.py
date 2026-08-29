"""Guards issue #384: the SatNOGS DB merges temporary NORAD IDs by hand;
our mirror must follow instead of keeping ghosts.

Learned on the HUCSat thread (98291 -> 69794, fredy: "this needs to be done
manually … all the data will be associated with the merged entry"). 629 of
our 2,768 catalogue rows carry temporary 98xxx IDs — each one is a future
manual merge upstream, and before this fix each would have left the dead
norad in the picker forever, with anything a user keyed to it stranded.
"""
import logging
import os
import sys

import psycopg2
import pytest

from conftest import require_test_db

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingest"))
import catalog_sync  # noqa: E402

DSN = os.environ.get("DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="no database")
log = logging.getLogger("test")

SID = "RIXQ-TEST-0000-0000-0000"


@pytest.fixture()
def cur():
    require_test_db()
    conn = psycopg2.connect(DSN)
    def scrub(c):
        # children first: telemetry FKs satellite, deleting the parent while
        # rows point at it is exactly the violation the module works around
        c.execute("DELETE FROM telemetry WHERE norad IN (98291, 69794)")
        c.execute("DELETE FROM satellite WHERE norad IN (98291, 69794)")
        c.execute("DELETE FROM catalog WHERE sat_id = %s", (SID,))
    with conn, conn.cursor() as c:
        scrub(c)
    with conn:
        with conn.cursor() as c:
            yield c
    with conn, conn.cursor() as c:
        scrub(c)
    conn.close()


def _others(cur):
    # reconcile prunes anything not in `seen`, so every pre-existing row must
    # ride along in the fixture's seen-set or the test would nuke real data
    cur.execute("SELECT norad FROM catalog")
    return {r[0] for r in cur.fetchall()}


def test_a_manual_merge_follows_the_sat_id(cur):
    cur.execute("INSERT INTO catalog (norad, name, sat_id) "
                "VALUES (98291, 'HUCSAT-TEST', %s)", (SID,))
    cur.execute("INSERT INTO satellite (norad, name) VALUES (98291, 'HUCSAT-TEST')")
    cur.execute("INSERT INTO telemetry (norad, ts, field, value_num) "
                "VALUES (98291, now(), 'test_field', 1)")
    # pass B: the DB merged the entry — same sat_id, confirmed norad
    cur.execute("INSERT INTO catalog (norad, name, sat_id) "
                "VALUES (69794, 'HUCSAT-TEST', %s)", (SID,))
    seen = _others(cur) - {98291}
    out = catalog_sync.reconcile_catalog(cur, seen, log)
    assert out["merged"] == 1
    cur.execute("SELECT norad FROM catalog WHERE sat_id = %s", (SID,))
    assert cur.fetchall() == [(69794,)], "the ghost row must be gone"
    cur.execute("SELECT norad FROM satellite WHERE name = 'HUCSAT-TEST'")
    assert cur.fetchall() == [(69794,)], "tracking must follow the merge"
    cur.execute("SELECT count(*) FROM telemetry WHERE norad = 69794")
    assert cur.fetchone()[0] == 1, "history must follow the merge"


def test_a_vanished_untracked_row_is_pruned_a_tracked_one_kept(cur):
    cur.execute("INSERT INTO catalog (norad, name, sat_id) "
                "VALUES (98291, 'GONE-TEST', %s)", (SID,))
    out = catalog_sync.reconcile_catalog(cur, _others(cur) - {98291}, log)
    assert out["pruned"] == 1
    cur.execute("INSERT INTO catalog (norad, name, sat_id) "
                "VALUES (98291, 'KEPT-TEST', %s)", (SID,))
    cur.execute("INSERT INTO satellite (norad, name) VALUES (98291, 'KEPT-TEST')")
    out = catalog_sync.reconcile_catalog(cur, _others(cur) - {98291}, log)
    assert out["pruned"] == 0, "a tracked satellite is never silently pruned"
    cur.execute("SELECT count(*) FROM catalog WHERE norad = 98291")
    assert cur.fetchone()[0] == 1


def test_reconcile_only_runs_on_a_complete_pass():
    """The wiring guard: a partial list must read as 'unknown', not as a mass
    extinction. Source-level, same style as the ops-board guards."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "ingest",
                            "ingest.py"), encoding="utf-8").read()
    call = src.index("reconcile_catalog(cur, seen")
    guard = src.rindex("if url is None and seen", 0, call)
    assert call - guard < 400, "the completeness guard must gate the call"


def test_migration_discovers_norad_tables_at_runtime(cur):
    tables = catalog_sync.norad_keyed_tables(cur)
    assert "satellite" in tables and "telemetry" in tables
    assert "catalog" not in tables, "catalog is deleted, never re-keyed"
