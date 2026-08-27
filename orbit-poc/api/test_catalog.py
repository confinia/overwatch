"""Guards issue #230: pick any satellite from the open network.

Telemetry decoding needs a per-satellite decoder — that is why the fleet is
curated. Position tracking does not: CelesTrak has a TLE for anything
catalogued and SGP4 propagates it. So adding an arbitrary satellite means
position tracking, and telemetry only where a decoder already exists. These
tests pin that distinction, and the guardrails around a SHARED tracked set
that anonymous callers can write to.
"""
import os
import sys

import psycopg2
import psycopg2.pool
import pytest

from conftest import require_test_db

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402

DSN = os.environ.get("DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="no database")


@pytest.fixture(scope="module")
def db():
    require_test_db()
    main.pool = psycopg2.pool.SimpleConnectionPool(1, 3, DSN)
    conn = psycopg2.connect(DSN)
    init = open(os.path.join(os.path.dirname(__file__), "..", "db", "init.sql"),
                encoding="utf-8").read()
    with conn, conn.cursor() as cur:
        cur.execute(init)
        cur.execute(main.KEYS_SQL)
        cur.execute("DELETE FROM catalog WHERE norad IN (999001, 999002, 999003)")
        cur.execute("DELETE FROM satellite WHERE norad IN (999001, 999002, 999003)")
        cur.execute("""INSERT INTO catalog (norad, name, sat_id, status, decoder)
                       VALUES (999001, 'TESTSAT ALPHA', 'AAAA-0001', 'alive', NULL),
                              (999002, 'TESTSAT BETA',  'BBBB-0002', 'alive', 'fox'),
                              (999003, 'DEADSAT GAMMA', 'CCCC-0003', 'dead',  NULL)""")
    conn.commit()
    yield conn
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM satellite WHERE norad IN (999001, 999002, 999003)")
        cur.execute("DELETE FROM catalog WHERE norad IN (999001, 999002, 999003)")
    conn.close()
    main.pool.closeall(); main.pool = None


def test_search_finds_catalogue_entries_beyond_the_fleet(db):
    out = main.catalog_search(q="TESTSAT", limit=20)
    names = {r["name"] for r in out}
    assert {"TESTSAT ALPHA", "TESTSAT BETA"} <= names
    assert all(r["tracked"] is False for r in out if r["norad"] in (999001, 999002))


def test_search_by_norad_number(db):
    out = main.catalog_search(q="999002", limit=20)
    assert len(out) == 1 and out[0]["norad"] == 999002
    assert out[0]["telemetry"] is True          # it has a decoder


def test_search_marks_which_have_telemetry(db):
    by_norad = {r["norad"]: r for r in main.catalog_search(q="TESTSAT", limit=20)}
    assert by_norad[999001]["telemetry"] is False   # position-only
    assert by_norad[999002]["telemetry"] is True


def test_tracking_adds_it_and_is_idempotent(db):
    first = main.catalog_track(None, main.TrackRequest(norad=999001))
    assert first["tracked"] is True and first["already"] is False
    assert first["telemetry"] is False
    assert "position tracking only" in first["note"]
    again = main.catalog_track(None, main.TrackRequest(norad=999001))
    assert again["already"] is True
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM satellite WHERE norad = 999001")
        assert cur.fetchone()[0] == 1


def test_tracked_satellite_is_position_only_without_a_decoder(db):
    main.catalog_track(None, main.TrackRequest(norad=999001))
    with db.cursor() as cur:
        cur.execute("SELECT has_telemetry, decoder FROM satellite WHERE norad = 999001")
        has_tlm, decoder = cur.fetchone()
    assert has_tlm is False and decoder is None, \
        "never fabricate telemetry for a satellite with no decoder"


def test_tracking_carries_the_decoder_when_there_is_one(db):
    r = main.catalog_track(None, main.TrackRequest(norad=999002))
    assert r["telemetry"] is True
    with db.cursor() as cur:
        cur.execute("SELECT has_telemetry, decoder FROM satellite WHERE norad = 999002")
        assert cur.fetchone() == (True, "fox")


def test_unknown_norad_is_refused(db):
    """The tracked set is shared and anonymous-writable; only catalogued
    objects may enter it."""
    with pytest.raises(main.HTTPException) as e:
        main.catalog_track(None, main.TrackRequest(norad=42424242))
    assert e.value.status_code == 404


def test_the_tracked_set_is_capped(db, monkeypatch):
    """A shared set that anyone can add to needs a ceiling."""
    monkeypatch.setattr(main, "TRACK_CAP", 1)
    with pytest.raises(main.HTTPException) as e:
        main.catalog_track(None, main.TrackRequest(norad=999003))
    assert e.value.status_code == 429
    assert "full" in e.value.detail


def test_the_picker_is_reachable_from_the_search_box():   # #230
    """The list the user types into must offer the open network beneath the
    tracked fleet — that IS the feature; an endpoint nobody can reach is not."""
    app = open(os.path.join(os.path.dirname(__file__), "..", "web", "static",
                            "app.js"), encoding="utf-8").read()
    assert "renderCatalog" in app and "/api/v1/catalog/search" in app
    assert "/api/v1/catalog/track" in app
    # only search once the query is worth a round trip, and never race
    assert "q.length >= 2" in app
    assert "catalogSeq" in app, "stale responses could overwrite newer ones"
    # already-tracked satellites belong in the list above, not the picker
    assert "filter(h => !h.tracked)" in app


def test_tracking_does_not_block_on_a_third_party():   # #230
    """A request handler must not make a slow external call: CelesTrak was
    unreachable from the VM the first time this ran, costing 15s per add.
    Elements are the ingest's job — it has the token and the SatNOGS
    fallback — and it fills them within a couple of minutes."""
    src = open(os.path.join(os.path.dirname(__file__), "main.py"),
               encoding="utf-8").read()
    track = src[src.index("def catalog_track("):src.index("@app.get(\"/v1/me/satellites\")")]
    # code only — the comment explains WHY CelesTrak is absent, and naming it
    # there must not trip the guard
    code = "\n".join(l.split("#", 1)[0] for l in track.splitlines())
    assert "celestrak" not in code.lower(), "the request path fetches from a third party"
    assert "requests.get" not in code and "_rq.get" not in code
    ing = open(os.path.join(os.path.dirname(__file__), "..", "ingest", "ingest.py"),
               encoding="utf-8").read()
    assert "def fill_missing_elements" in ing
    # The fallback now lives in _tle_for(), which tries the bulk cache first
    # and only then CelesTrak, then SatNOGS — see test_tle_client.py. What
    # matters here is that the fill uses that path rather than calling a
    # provider directly.
    fill = ing[ing.index("def fill_missing_elements("):]
    fill = fill[:fill.index("\ndef ", 10)]
    assert "_tle_for(norad)" in fill, "the fill must go through the polite lookup"
    assert "CATNR" not in fill, "no per-object CelesTrak call here"
    assert "_tle_from_satnogs" in ing, "the SatNOGS fallback must still exist"
    assert "elements-fill" in ing, "the fill loop is not scheduled"


def test_position_propagation_is_never_starved_by_another_loop():   # #230 regression
    """`loop()` never returns, so it can only be called ONCE outside a thread —
    whatever comes after it is dead code. Adding the catalogue loops above the
    positions call made propagate_positions unreachable and silently stopped
    the globe: no satellite got a position for as long as it was deployed.

    Every loop except positions must therefore run in its own thread, and
    positions must be the last statement."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "ingest", "ingest.py"),
               encoding="utf-8").read()
    body = src[src.index("def main():"):src.index("def _wait_for_db(")]
    calls = [l.strip() for l in body.splitlines()
             if l.strip().startswith("loop(")]
    assert len(calls) == 1, f"only positions may call loop() directly: {calls}"
    assert "propagate_positions" in calls[0]
    # and nothing may follow it
    after = body[body.index(calls[0]) + len(calls[0]):]
    assert not [l for l in after.splitlines() if l.strip() and not l.strip().startswith("#")], \
        "code after loop(propagate_positions) never runs"


def test_a_dead_tle_source_cannot_stall_startup():   # #230 follow-up, #357
    """The per-object CelesTrak lookup this once guarded is gone entirely:
    GROUP=satnogs is one request for the whole set, and 1,418 CATNR queries in
    a day is what put us in their firewall. The connect budget still has to be
    bounded, because an unreachable provider must fail fast (#313)."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "ingest", "ingest.py"),
               encoding="utf-8").read()
    assert "CATNR" not in "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    connect = int(src.split('CONNECT_TIMEOUT", ')[1].split(")")[0])
    assert connect <= 10, f"{connect}s to open a socket is not a bounded connect"
INGEST_SRC = open(os.path.join(os.path.dirname(__file__), "..", "ingest",
                               "ingest.py"), encoding="utf-8").read()


def _seed_upsert(cur, norad, name, sat_id, telemetry):
    """The exact upsert seed_satellites runs, lifted from the source so the
    test cannot drift from it."""
    body = INGEST_SRC[INGEST_SRC.index("INSERT INTO satellite (norad, name, sat_id"):]
    sql = body[:body.index('"""')]
    cur.execute(sql, (norad, name, sat_id, bool(sat_id) and telemetry,
                      None, "test", bool(telemetry)))


def test_a_failed_lookup_keeps_the_sat_id_we_already_had(db):
    """A deploy during a provider outage used to overwrite every known row
    with sat_id=NULL / has_telemetry=false. Nothing re-resolves outside
    startup, so the fleet could not recover: the 2026-08-20 block lasted
    hours, the outage lasted four days."""
    with db, db.cursor() as cur:
        cur.execute("DELETE FROM satellite WHERE norad = 999001")
        _seed_upsert(cur, 999001, "TESTSAT", "ABCD-1234", True)
        cur.execute("SELECT sat_id, has_telemetry FROM satellite WHERE norad=999001")
        assert cur.fetchone() == ("ABCD-1234", True)

        _seed_upsert(cur, 999001, "TESTSAT", None, True)      # lookup failed
        cur.execute("SELECT sat_id, has_telemetry FROM satellite WHERE norad=999001")
        assert cur.fetchone() == ("ABCD-1234", True), \
            "a failed lookup erased a known sat_id"
        cur.execute("DELETE FROM satellite WHERE norad = 999001")


def test_a_successful_lookup_still_updates(db):
    with db, db.cursor() as cur:
        cur.execute("DELETE FROM satellite WHERE norad = 999002")
        _seed_upsert(cur, 999002, "TESTSAT2", "OLD-0001", True)
        _seed_upsert(cur, 999002, "TESTSAT2", "NEW-0002", True)
        cur.execute("SELECT sat_id FROM satellite WHERE norad=999002")
        assert cur.fetchone()[0] == "NEW-0002"
        cur.execute("DELETE FROM satellite WHERE norad = 999002")


def test_a_position_only_satellite_never_gets_telemetry(db):
    """has_telemetry is derived from the coalesced sat_id AND the showcase
    flag — a satellite we do not decode must not acquire telemetry just
    because a sat_id exists."""
    with db, db.cursor() as cur:
        cur.execute("DELETE FROM satellite WHERE norad = 999003")
        _seed_upsert(cur, 999003, "TESTSAT3", "SOME-0003", False)
        cur.execute("SELECT has_telemetry FROM satellite WHERE norad=999003")
        assert cur.fetchone()[0] is False
        cur.execute("DELETE FROM satellite WHERE norad = 999003")


def test_no_per_object_satellites_query_remains():
    """LSF: the bulk list carries the same data. We have said in public that
    every lookup is answered from our own copy."""
    code = "\n".join(l.split("#", 1)[0] for l in INGEST_SRC.splitlines())
    assert "SATNOGS_BASE}/satellites/\",\n" not in code
    resolve = code[code.index("def resolve_sat_id("):]
    resolve = resolve[:resolve.index("\ndef ", 10)]
    assert "requests.get" not in resolve, \
        "resolve_sat_id must answer from the catalogue, never the network"


def test_the_catalogue_is_built_before_the_first_seed():
    """seed_satellites resolves from `catalog`; an empty one was the only
    remaining reason to query per object."""
    body = INGEST_SRC[INGEST_SRC.index("def main():"):INGEST_SRC.index("def _wait_for_db(")]
    assert body.index("refresh_catalog()") < body.index("seed_satellites()")


def test_the_bulk_pass_is_not_repeated_on_every_deploy():
    """It runs at startup now, and we deploy several times a day — without a
    freshness guard 'one bulk pass per day' would be one per deploy."""
    fn = INGEST_SRC[INGEST_SRC.index("def refresh_catalog("):]
    fn = fn[:fn.index("\ndef ", 10)]
    assert "CATALOG_INTERVAL" in fn and "max(updated_at)" in fn


# ---------------------------------------------------------------------------
# Per-station daily baseline (#337)
# ---------------------------------------------------------------------------
def _rollup_sql():
    """The aggregation, lifted from the source so the test cannot drift."""
    fn = INGEST_SRC[INGEST_SRC.index("def roll_up_stations("):]
    body = fn[:fn.index("\ndef ", 10)]
    sql = body[body.index("INSERT INTO station_daily"):body.index('""", params)')]
    return sql.replace("{where}", "").replace("f\"\"\"", "")


def _seed_reception(cur, observer="TEST-OBS"):
    """Three frames from two satellites, today. Self-contained: creates the
    satellite rows reception references, so the test does not depend on
    another suite having run first."""
    cur.execute("DELETE FROM station_daily WHERE observer = %s", (observer,))
    cur.execute("DELETE FROM reception WHERE observer = %s", (observer,))
    for norad in (999001, 999002):
        cur.execute("INSERT INTO satellite (norad, name, has_telemetry) "
                    "VALUES (%s, 'ROLLUP TEST', false) "
                    "ON CONFLICT (norad) DO NOTHING", (norad,))
    for norad, hour in ((999001, 1), (999001, 2), (999002, 3)):
        cur.execute("INSERT INTO reception (norad, ts, observer, lat, lon) "
                    "VALUES (%s, date_trunc('day', now()) + %s * interval '1 hour',"
                    " %s, 0, 0)", (norad, hour, observer))


def _clean_reception(cur, observer="TEST-OBS"):
    cur.execute("DELETE FROM station_daily WHERE observer = %s", (observer,))
    cur.execute("DELETE FROM reception WHERE observer = %s", (observer,))
    cur.execute("DELETE FROM satellite WHERE norad IN (999001, 999002)")


def test_the_rollup_matches_a_direct_count(db):
    with db, db.cursor() as cur:
        _seed_reception(cur, "TEST-OBS-A")
        cur.execute(_rollup_sql())
        cur.execute("SELECT frames, satellites_heard FROM station_daily "
                    "WHERE observer='TEST-OBS-A' AND day = current_date")
        assert cur.fetchone() == (3, 2)
        _clean_reception(cur, "TEST-OBS-A")


def test_the_rollup_is_idempotent(db):
    """Frames arrive late, so a day is recomputed repeatedly. Running it twice
    must not double a station's count — that would read as a surge, and a
    detector calibrated on phantom surges is worse than none."""
    with db, db.cursor() as cur:
        _seed_reception(cur, "TEST-OBS-B")
        cur.execute(_rollup_sql())
        cur.execute(_rollup_sql())
        cur.execute("SELECT frames FROM station_daily "
                    "WHERE observer='TEST-OBS-B' AND day = current_date")
        assert cur.fetchone()[0] == 3
        _clean_reception(cur, "TEST-OBS-B")


def test_the_rollup_makes_no_outbound_request():
    """The whole point: a baseline we can build and rebuild freely, because it
    costs a provider nothing. If this ever calls out, backfilling becomes the
    scraping pattern LSF blocked us for."""
    fn = INGEST_SRC[INGEST_SRC.index("def roll_up_stations("):]
    fn = fn[:fn.index("\ndef ", 10)]
    code = "\n".join(l.split("#", 1)[0] for l in fn.splitlines())
    for forbidden in ("requests.", "SATNOGS_BASE", "CELESTRAK_BASE", "_pace_satnogs"):
        assert forbidden not in code, f"the rollup must stay local: {forbidden}"


def test_a_trailing_window_is_recomputed_not_just_today():
    """A station uploads a pass hours after it happened, so yesterday's total
    is not final when yesterday ends."""
    days = int(INGEST_SRC.split('STATION_ROLLUP_DAYS", ')[1].split(")")[0])
    assert days >= 2, f"{days}d does not cover late-arriving frames"


def _ingest_fn(name, **stubs):
    """Lift one top-level function out of ingest.py and run it against stubs —
    ingest.py imports psycopg2/sgp4/numpy and reads DB_DSN at import, so it
    cannot be imported here."""
    start = INGEST_SRC.index(f"def {name}(")
    end = INGEST_SRC.find("\ndef ", start + 1)
    ns = dict(stubs)
    exec(compile(INGEST_SRC[start:end if end != -1 else len(INGEST_SRC)],
                 "ingest.py", "exec"), ns)
    return ns


def test_the_backfill_is_not_called_from_startup():   # #339
    """It used to run in main(), where it lost a race with the API's startup
    DDL: station_daily did not exist yet, the call raised, was caught so it
    could not block startup, and was never retried."""
    body = INGEST_SRC[INGEST_SRC.index("def main():"):INGEST_SRC.index("def _wait_for_db(")]
    assert "roll_up_stations()" not in body, \
        "the full pass must not depend on another service's schema being ready"


def test_the_backfill_runs_once_then_trailing_windows(db):
    calls = []
    ns = _ingest_fn("rollup_tick",
                    roll_up_stations=lambda days=None: calls.append(days),
                    STATION_ROLLUP_DAYS=3,
                    _rollup_backfilled=[False])
    for _ in range(3):
        ns["rollup_tick"]()
    assert calls == [None, 3, 3], f"expected one full pass then windows: {calls}"


def test_a_failed_backfill_is_retried_not_marked_done(db):
    """If the table is not there yet the tick fails and the next one must try
    the FULL pass again — otherwise the history is lost for good."""
    calls = []

    def flaky(days=None):
        calls.append(days)
        if len(calls) == 1:
            raise RuntimeError("relation \"station_daily\" does not exist")

    ns = _ingest_fn("rollup_tick", roll_up_stations=flaky,
                    STATION_ROLLUP_DAYS=3, _rollup_backfilled=[False])
    try:
        ns["rollup_tick"]()          # raises; loop() logs and retries
    except RuntimeError:
        pass
    ns["rollup_tick"]()
    assert calls == [None, None], \
        f"a failed backfill must be retried in full, got {calls}"


# ---------------------------------------------------------------------------
# The denominator: passes available per station per day (#346)
# ---------------------------------------------------------------------------
def _opportunity_upsert(cur, observer, day_expr, norad, passes, el):
    sql = INGEST_SRC[INGEST_SRC.index("INSERT INTO station_opportunity"):]
    sql = sql[:sql.index('""", rows)')].replace("VALUES %s", "VALUES (%s,"
                                                + day_expr + ",%s,%s,%s)")
    cur.execute(sql, (observer, norad, passes, el))


def test_the_opportunity_upsert_is_idempotent(db):
    """A day gets recomputed whenever elements improve. Double-counting the
    denominator would deflate every hit rate derived from it."""
    with db, db.cursor() as cur:
        cur.execute("DELETE FROM station_opportunity WHERE observer='OPP-TEST'")
        for _ in range(2):
            _opportunity_upsert(cur, "OPP-TEST", "current_date", 25544, 5, 61.2)
        cur.execute("SELECT count(*), max(passes) FROM station_opportunity "
                    "WHERE observer='OPP-TEST'")
        assert cur.fetchone() == (1, 5)
        cur.execute("DELETE FROM station_opportunity WHERE observer='OPP-TEST'")


def test_the_scan_makes_no_outbound_request():
    """Local geometry only — coordinates from reception, elements from
    elements. That is what makes a 40-day backfill safe to run at all."""
    for name in ("compute_opportunities", "opportunities_tick"):
        fn = INGEST_SRC[INGEST_SRC.index(f"def {name}("):]
        fn = fn[:fn.index("\ndef ", 10)]
        code = "\n".join(l.split("#", 1)[0] for l in fn.splitlines())
        for forbidden in ("requests.", "SATNOGS_BASE", "CELESTRAK_BASE"):
            assert forbidden not in code, f"{name} must stay local: {forbidden}"


def test_elements_are_chosen_by_nearest_epoch():
    """Propagating a TLE far from its epoch produces confident nonsense, so a
    past day must use the element set closest to it — not the newest."""
    fn = INGEST_SRC[INGEST_SRC.index("def _tle_nearest("):]
    fn = fn[:fn.index("\ndef ", 10)]
    assert "abs(extract(epoch FROM epoch - %s))" in fn and "LIMIT 1" in fn


def test_the_backfill_never_reaches_past_the_elements():
    """SGP4 has no honest answer before our earliest TLE, and OPPORTUNITY_DAYS
    caps it further. A day with nothing to propagate must be skipped, not
    written as zero passes — zero is a claim, and a wrong one."""
    fn = INGEST_SRC[INGEST_SRC.index("def opportunities_tick("):]
    fn = fn[:fn.index("\ndef ", 10)]
    assert "min(epoch)" in fn and "OPPORTUNITY_DAYS" in fn and "max(" in fn
    scan = INGEST_SRC[INGEST_SRC.index("def compute_opportunities("):]
    scan = scan[:scan.index("\ndef ", 10)]
    assert "no stations or no elements" in scan, \
        "an empty scan must be skipped explicitly"


def test_one_day_per_tick():
    """A full reconstruction is minutes of arithmetic; doing it in one call
    would stall the loop and restart from nothing on a redeploy."""
    fn = INGEST_SRC[INGEST_SRC.index("def opportunities_tick("):]
    fn = fn[:fn.index("\ndef ", 10)]
    assert "LIMIT 1" in fn and "ORDER BY d DESC" in fn, \
        "newest missing day first, one at a time"


# ---------------------------------------------------------------------------
# Station degradation detector (#348)
# ---------------------------------------------------------------------------
RECENT_OFFSETS = (1, 2)                 # inside `day > current_date - 3`
BASE_OFFSETS = tuple(range(3, 21))      # 18 days of baseline, >= HEALTH_MIN_DAYS


def _seed_station(cur, observer, base_rate, recent_rate, base=BASE_OFFSETS,
                  recent=RECENT_OFFSETS, passes=100):
    for offsets, rate in ((base, base_rate), (recent, recent_rate)):
        for off in offsets:
            cur.execute(
                "INSERT INTO station_opportunity (observer, day, norad, passes,"
                " best_max_el) VALUES (%s, current_date - %s::integer, 1, %s, 45)"
                " ON CONFLICT (observer, day, norad) DO UPDATE SET passes=EXCLUDED.passes",
                (observer, off, passes))
            cur.execute(
                "INSERT INTO station_daily (observer, day, frames,"
                " satellites_heard) VALUES (%s, current_date - %s::integer, %s, 1)"
                " ON CONFLICT (observer, day) DO UPDATE SET frames=EXCLUDED.frames",
                (observer, off, int(round(passes * rate))))


def _wipe_health(cur):
    cur.execute("DELETE FROM station_opportunity WHERE observer LIKE 'HT-%'")
    cur.execute("DELETE FROM station_daily WHERE observer LIKE 'HT-%'")


def _degraded(cur, as_of="today"):
    cur.execute(main.STATION_HEALTH_SQL, {
        "as_of": as_of,
        "window": main.HEALTH_RECENT_DAYS + main.HEALTH_BASELINE_DAYS,
        "recent": main.HEALTH_RECENT_DAYS,
        "min_days": main.HEALTH_MIN_DAYS,
        "min_base": main.HEALTH_MIN_BASELINE,
        "collapse": main.HEALTH_COLLAPSE,
        "fleet_ok": main.HEALTH_FLEET_OK,
    })
    return {r[0] for r in cur.fetchall()}


def test_a_station_that_stops_hearing_while_the_fleet_does_not_is_flagged(db):
    with db, db.cursor() as cur:
        _wipe_health(cur)
        for good in ("HT-GOOD-1", "HT-GOOD-2", "HT-GOOD-3"):
            _seed_station(cur, good, 0.60, 0.60)
        _seed_station(cur, "HT-BROKE", 0.60, 0.00)
        found = _degraded(cur)
        assert "HT-BROKE" in found, "a collapsed station was not flagged"
        assert not any(o.startswith("HT-GOOD") for o in found), \
            f"healthy stations flagged: {found}"
        _wipe_health(cur)


def test_our_own_outage_does_not_accuse_every_station(db):
    """The 2026-08-20 block stopped telemetry for four days, so EVERY station's
    hit rate went to zero. A detector without the fleet clause would have
    reported 360 simultaneous antenna failures. This is the most valuable test
    we have, and it needs no third-party data."""
    with db, db.cursor() as cur:
        _wipe_health(cur)
        for obs in ("HT-A", "HT-B", "HT-C", "HT-D"):
            _seed_station(cur, obs, 0.60, 0.00)     # all collapse together
        found = _degraded(cur)
        assert found == set(), \
            f"blamed stations for our own outage: {found}"
        _wipe_health(cur)


def test_a_station_that_never_heard_us_is_not_a_failure(db):
    """SPUTNIX-Murmansk ran 0/163 on 2026-08-25. That is not a fault: a station
    which never tracked our 23 satellites is indistinguishable from a dead one
    by absolute rate. Only a fall from its OWN history counts."""
    with db, db.cursor() as cur:
        _wipe_health(cur)
        for good in ("HT-GOOD-1", "HT-GOOD-2"):
            _seed_station(cur, good, 0.60, 0.60)
        _seed_station(cur, "HT-ZERO", 0.00, 0.00)
        found = _degraded(cur)
        assert "HT-ZERO" not in found, "a never-listening station was flagged"
        _wipe_health(cur)


def test_a_station_without_enough_history_cannot_degrade(db):
    """No baseline, no verdict — otherwise every new station is born broken."""
    with db, db.cursor() as cur:
        _wipe_health(cur)
        for good in ("HT-GOOD-1", "HT-GOOD-2"):
            _seed_station(cur, good, 0.60, 0.60)
        _seed_station(cur, "HT-NEW", 0.60, 0.00, base=(3, 4))   # 2 days only
        found = _degraded(cur)
        assert "HT-NEW" not in found, "flagged a station with no baseline"
        _wipe_health(cur)


def test_a_mild_dip_is_not_a_collapse(db):
    """One quiet spell is weather or maintenance. Only a collapse past the
    threshold counts, or the alert becomes noise nobody reads."""
    with db, db.cursor() as cur:
        _wipe_health(cur)
        for good in ("HT-GOOD-1", "HT-GOOD-2"):
            _seed_station(cur, good, 0.60, 0.60)
        _seed_station(cur, "HT-DIP", 0.60, 0.45)
        found = _degraded(cur)
        assert "HT-DIP" not in found, "a mild dip was reported as a collapse"
        _wipe_health(cur)


def test_the_detector_can_be_replayed_against_a_past_date(db):
    """It hardcoded current_date, so it could never be run against a real
    failure — only argued about. Replay is the whole methodology: find a
    collapse, wind back, check the rule would have called it (#350)."""
    with db, db.cursor() as cur:
        _wipe_health(cur)
        # A station that broke 10 days ago and has been dead since. Anchored
        # to a fixed offset so "now" sees a long-dead station, while a replay
        # positioned at the break sees the collapse itself.
        for good in ("HT-GOOD-1", "HT-GOOD-2"):
            _seed_station(cur, good, 0.60, 0.60,
                          base=tuple(range(13, 31)), recent=(11, 12))
        _seed_station(cur, "HT-OLDBREAK", 0.60, 0.00,
                      base=tuple(range(13, 31)), recent=(11, 12))
        cur.execute("SELECT (current_date - 10)::text")
        as_of = cur.fetchone()[0]
        assert "HT-OLDBREAK" in _degraded(cur, as_of), \
            "replaying at the break did not surface the collapse"
        _wipe_health(cur)


def test_replay_defaults_to_today():
    """An empty as_of must mean now, not an error or an epoch."""
    src = open(os.path.join(os.path.dirname(__file__), "main.py"),
               encoding="utf-8").read()
    fn = src[src.index("def station_health("):]
    fn = fn[:fn.index("cols = ")]
    assert 'as_of or _dt.datetime.now' in fn
    assert "current_date" not in main.STATION_HEALTH_SQL, \
        "the SQL must take the date as a parameter, never hardcode it"


def test_a_replay_cannot_see_past_its_own_as_of_date(db):
    """The rate CTE had no upper bound, so replaying a past date pulled in
    everything after it. Replaying 2026-08-23 across our own outage included
    the 24th and 25th — the recovery — lifting the fleet average from 0.0020 to
    0.0354 and diluting the clause being tested (#351).

    Here: a station that collapsed and then RECOVERED. Standing at the
    collapse, the recovery must be invisible, so it is still flagged."""
    with db, db.cursor() as cur:
        _wipe_health(cur)
        # days 21..38 baseline, 19..20 collapse, 1..2 recovered (the "future")
        for good in ("HT-GOOD-1", "HT-GOOD-2"):
            _seed_station(cur, good, 0.60, 0.60,
                          base=tuple(range(21, 39)), recent=(19, 20))
            _seed_station(cur, good, 0.60, 0.60, base=(3, 4), recent=(1, 2))
        _seed_station(cur, "HT-RECOVERED", 0.60, 0.00,
                      base=tuple(range(21, 39)), recent=(19, 20))
        _seed_station(cur, "HT-RECOVERED", 0.60, 0.60, base=(3, 4), recent=(1, 2))
        cur.execute("SELECT (current_date - 18)::text")
        at_collapse = cur.fetchone()[0]
        assert "HT-RECOVERED" in _degraded(cur, at_collapse), \
            "the replay saw the recovery it should not have been able to see"
        _wipe_health(cur)


def test_the_rate_window_is_bounded_at_both_ends():
    assert "o.day <= %(as_of)s::date" in main.STATION_HEALTH_SQL, \
        "a replay must not read days after the date it stands at"


def test_startup_does_not_prime_elements_it_already_has():   # #352
    """The synchronous prime cost ~4.5 minutes with nothing propagating —
    8s of CelesTrak timeout plus 11s per satellite through our own pacer.
    Measured: started 10:47:41, first thread 10:52:04. A dark globe after
    every deploy, and it tripped positions-stalled everywhere."""
    body = INGEST_SRC[INGEST_SRC.index("def main():"):INGEST_SRC.index("def _wait_for_db(")]
    assert "if not have_elements:" in body, \
        "elements must be primed only when there are none"
    prime = body[body.index("have_elements"):]
    assert "fetch_elements()" in prime.split("threading.Thread")[0], \
        "the prime must sit inside the empty-database guard"


# ---------------------------------------------------------------------------
# Dead satellites must read as dead in the picker (#302)
# ---------------------------------------------------------------------------
WEB = os.path.join(os.path.dirname(__file__), "..", "web", "static")


def test_a_non_alive_catalogue_entry_is_dimmed():
    """The picker is the first thing an anonymous visitor touches. Adding a
    dead object gives a dot that never moves and no telemetry — which reads as
    a broken app, not a dead satellite."""
    js = open(os.path.join(WEB, "app.js"), encoding="utf-8").read()
    fn = js[js.index("async function renderCatalog("):]
    fn = fn[:fn.index("\n}", 10)]
    assert '"alive"' in fn, "the catalogue status is still ignored"
    assert '"sat inactive"' in fn and '"sat"' in fn, \
        "alive and non-alive rows must not render identically"


def test_dead_entries_stay_selectable():
    """Someone may legitimately want a re-entered object's last known orbit.
    Dim it, do not disable it."""
    js = open(os.path.join(WEB, "app.js"), encoding="utf-8").read()
    fn = js[js.index("async function renderCatalog("):]
    fn = fn[:fn.index("\n}", 10)]
    assert "disabled" not in fn, "a dimmed row must still be addable"
    assert "trackSat(" in fn


def test_the_dimming_uses_an_existing_colour_token():
    """A new colour for one state is how a palette rots."""
    html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    rule = html[html.index(".sat.inactive"):]
    rule = rule[:rule.index("}") + 1]
    assert "var(--dim)" in rule, "use the existing --dim token, not a new colour"


def test_catalogue_status_is_not_confused_with_link_status():
    """The fleet list colours dots by LINK recency; this is CATALOGUE status.
    Two different notions of alive sharing one word must not read as one
    scale, so the picker says which one it means."""
    js = open(os.path.join(WEB, "app.js"), encoding="utf-8").read()
    fn = js[js.index("async function renderCatalog("):]
    fn = fn[:fn.index("\n}", 10)]
    assert "catalogue:" in fn, "the picker must name which kind of status it shows"


# ---------------------------------------------------------------------------
# The station API the mobile app stands on (#363)
# ---------------------------------------------------------------------------
def test_the_health_route_is_registered_before_the_callsign_route():
    """FastAPI matches in registration order. /{callsign} was registered first,
    so GET /v1/stations/health returned "No receptions by 'health'" — the
    detector endpoint was NEVER reachable over HTTP, and nothing noticed
    because its validation ran the SQL directly."""
    src = open(os.path.join(os.path.dirname(__file__), "main.py"),
               encoding="utf-8").read()
    assert src.index('@app.get("/v1/stations/health")') \
         < src.index('@app.get("/v1/stations/{callsign}")'), \
        "the literal route must be registered before the parameter route"


def test_per_station_health_returns_days_and_a_caveat(db):
    # seed and COMMIT first: the endpoint reads through its own pool
    # connection and cannot see this transaction's uncommitted rows
    with db, db.cursor() as cur:
        _wipe_health(cur)
        _seed_station(cur, "HT-MOB-JN97", 0.60, 0.60)
    out = main.station_health_one("HT-MOB-JN97")
    with db, db.cursor() as cur:
        assert out["observer"] == "HT-MOB-JN97"
        assert out["days"] and all("hit_rate" in d for d in out["days"])
        # the satellite-centric blindfold must be stated to the caller, not
        # discovered by them: a low absolute rate is not a verdict
        assert "own baseline" in out["note"]
        _wipe_health(cur)
        return


def test_per_station_health_404s_an_unknown_callsign(db):
    import pytest as _pt
    from fastapi import HTTPException as _He
    with db:
        with _pt.raises(_He):
            main.station_health_one("HT-NOSUCH-XX00")


def test_past_passes_carry_what_was_heard(db):   # #366
    """The per-pass version of the product question: three frames during
    yesterday's pass means the station worked; zero means it did not hear
    THAT pass — far more actionable than a daily aggregate."""
    with db, db.cursor() as cur:
        _wipe_health(cur)
        _seed_station(cur, "HT-PASS-JN97", 0.6, 0.6)
        cur.execute("INSERT INTO satellite (norad, name, has_telemetry) VALUES "
                    "(999001, 'PASS TEST', false) ON CONFLICT (norad) DO NOTHING")
        cur.execute("DELETE FROM pass WHERE observer = 'HT-PASS-JN97'")
        cur.execute("INSERT INTO pass (observer, norad, aos, los, max_el_deg) VALUES "
                    "('HT-PASS-JN97', 999001, now() - interval '3 hours', "
                    " now() - interval '170 minutes', 45)")
        for m in (175, 173, 171):    # inside the padded window
            cur.execute("INSERT INTO reception (norad, ts, observer, lat, lon) "
                        "VALUES (999001, now() - %s * interval '1 minute', "
                        "'HT-PASS-JN97', 0, 0)", (m,))
    out = main.station_health_one("HT-PASS-JN97")
    assert out["past_passes"], "a completed pass must appear"
    assert out["past_passes"][0]["frames"] == 3, \
        "the frames decoded during the pass must ride along"
    with db, db.cursor() as cur:
        cur.execute("DELETE FROM pass WHERE observer = 'HT-PASS-JN97'")
        cur.execute("DELETE FROM reception WHERE observer = 'HT-PASS-JN97'")
        cur.execute("DELETE FROM satellite WHERE norad = 999001")
        _wipe_health(cur)


def test_a_pass_can_be_inspected_frame_by_frame(db):   # #368
    """WHERE the frames sit in the window is the diagnostic: only around max
    elevation = horizon problem, across the whole window = healthy chain."""
    with db, db.cursor() as cur:
        _wipe_health(cur)
        _seed_station(cur, "HT-DET-JN97", 0.6, 0.6)
        cur.execute("INSERT INTO satellite (norad, name, has_telemetry) VALUES "
                    "(999001, 'DET TEST', false) ON CONFLICT (norad) DO NOTHING")
        cur.execute("DELETE FROM pass WHERE observer = 'HT-DET-JN97'")
        cur.execute("INSERT INTO pass (observer, norad, aos, los, max_el_deg) "
                    "VALUES ('HT-DET-JN97', 999001, now() - interval '3 hours', "
                    "now() - interval '170 minutes', 45)")
        cur.execute("INSERT INTO reception (norad, ts, observer, lat, lon) "
                    "VALUES (999001, now() - interval '175 minutes', 'HT-DET-JN97', 0, 0)")
        # one decoded field riding on that exact frame timestamp
        cur.execute("INSERT INTO telemetry (norad, ts, field, value_num) "
                    "SELECT 999001, ts, 'battery_v', 7.4 FROM reception "
                    "WHERE observer = 'HT-DET-JN97' LIMIT 1")
    out = main.station_pass_detail("HT-DET-JN97", 999001,
                                   out_aos := main.station_health_one("HT-DET-JN97")
                                   ["past_passes"][0]["aos"])
    assert out["satellite"] == "DET TEST" and out["duration_s"] == 600
    assert len(out["frames"]) == 1 and out["frames"][0]["fields"] == 1, \
        "the frame must carry how many fields it decoded into"
    with db, db.cursor() as cur:
        for t in ("pass", "reception", "telemetry"):
            cur.execute(f"DELETE FROM {t} WHERE norad = 999001")
        cur.execute("DELETE FROM satellite WHERE norad = 999001")
        _wipe_health(cur)


def test_pass_detail_survives_an_unencoded_plus(db):   # #369
    """+00:00 arrives as " 00:00" from any client that does not percent-encode
    the plus: query decoding maps + to space, Swift URLComponents leaves +
    literal, and a bare curl does too. The first live request 500d on the
    timestamp cast. A truly malformed aos is a 400, never a 500."""
    import pytest as _pt
    from fastapi import HTTPException as _He
    with db, db.cursor() as cur:
        _wipe_health(cur)
        _seed_station(cur, "HT-ENC-JN97", 0.6, 0.6)
        cur.execute("INSERT INTO satellite (norad, name, has_telemetry) VALUES "
                    "(999001, 'ENC TEST', false) ON CONFLICT (norad) DO NOTHING")
        cur.execute("DELETE FROM pass WHERE observer = 'HT-ENC-JN97'")
        cur.execute("INSERT INTO pass (observer, norad, aos, los, max_el_deg) "
                    "VALUES ('HT-ENC-JN97', 999001, now() - interval '3 hours', "
                    "now() - interval '170 minutes', 45)")
    aos = main.station_health_one("HT-ENC-JN97")["past_passes"][0]["aos"]
    assert "+" in aos, "the fixture must exercise the offset form"
    out = main.station_pass_detail("HT-ENC-JN97", 999001, aos.replace("+", " "))
    assert out["satellite"] == "ENC TEST"
    with _pt.raises(_He) as e:
        main.station_pass_detail("HT-ENC-JN97", 999001, "not-a-time")
    assert e.value.status_code == 400, "malformed aos must be a 400, not a 500"
    with db, db.cursor() as cur:
        cur.execute("DELETE FROM pass WHERE observer = 'HT-ENC-JN97'")
        cur.execute("DELETE FROM satellite WHERE norad = 999001")
        _wipe_health(cur)
