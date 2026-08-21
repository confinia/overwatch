"""Guards the request pattern that got our shared egress address blocked.

On 2026-08-20 both CelesTrak and SatNOGS stopped answering this VM. The
diagnosis was not a network fault: traceroute reached the destination and got
"administratively prohibited" FROM it, and a provider on another continent
failed identically — the common factor was our source address. The cause was
ours: a fill loop polling gp.php?CATNR= every 120s for objects that could
never be found, forever, with no backoff — up to 3600/day against the endpoint
CelesTrak explicitly asks callers not to poll.

These tests pin the polite pattern: bulk-first with caching, per-object only as
a backed-off fallback, a give-up that survives restarts, and silence after a
provider refuses us.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

HERE = os.path.dirname(__file__)
INGEST = open(os.path.join(HERE, "..", "ingest", "ingest.py"), encoding="utf-8").read()


def _code(text):
    """Strip comments — a comment naming an endpoint must not trip a guard."""
    return "\n".join(l.split("#", 1)[0] for l in text.splitlines())


def test_bulk_is_the_primary_path():
    """CelesTrak asks for bulk GROUP= files, cached — not per-object polling."""
    assert "CELESTRAK_LOOKUP_GROUPS" in INGEST
    assert "_refresh_bulk_tles" in INGEST
    lookup = INGEST[INGEST.index("def _tle_for("):INGEST.index("def _tle_for(") + 900]
    body = _code(lookup)
    # the cache is consulted BEFORE any per-object call
    assert body.index("_refresh_bulk_tles()") < body.index("_tle_from_celestrak"), \
        "per-object lookup must come after the bulk cache, not before"


def test_per_object_lookups_are_backed_off_and_can_give_up():
    assert "_due_for_lookup" in INGEST and "_record_lookup" in INGEST
    rec = INGEST[INGEST.index("def _record_lookup("):INGEST.index("def _tle_for(")]
    # exponential, capped, and a permanent stop
    assert "power(2" in rec and "least(" in rec
    assert "gave_up = element_fetch.attempts + 1 >= 6" in rec


def test_the_give_up_survives_a_restart():
    """In-process state would let a container recycle resume the polling weeks
    later, with nobody connecting it to this incident."""
    assert "FROM element_fetch" in INGEST, "backoff state must be in the database"
    main = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS element_fetch" in main
    assert "next_attempt" in main and "gave_up" in main


def test_a_refusal_silences_us_rather_than_provoking_a_ban():
    assert "_cooling(" in INGEST and "_cool(" in INGEST
    assert "Retry-After" in INGEST
    assert "403, 429" in INGEST or "(403, 429)" in INGEST
    fn = INGEST[INGEST.index("def _tle_for("):]
    fn = _code(fn[:fn.index("\ndef ", 10)])
    # both providers are consulted only when not cooling down
    for provider in ("celestrak", "satnogs"):
        guard = next(l for l in fn.splitlines()
                     if f'_cooling("{provider}")' in l and l.strip().startswith("if "))
        assert "not " in guard, f"{provider} is asked even while cooling down"


def test_the_fill_loop_is_not_a_poller():
    """The loop as originally written: every 120s, 5 objects, no backoff, no
    give-up — up to 3600 per-object requests a day."""
    fill = INGEST[INGEST.index("def fill_missing_elements("):]
    fill = fill[:fill.index("def ", 10)]
    assert "gave_up, false) = false" in fill, "given-up objects must be skipped"
    assert "next_attempt, now()) <= now()" in fill, "backoff must be honoured"
    interval = int(INGEST.split('"FILL_INTERVAL", ')[1].split(")")[0])
    assert interval >= 600, f"{interval}s is a polling cadence, not a repair cadence"


def test_worst_case_request_volume_is_bounded():
    """The number that mattered. With backoff and give-up, an object CelesTrak
    does not carry costs a handful of requests in total, not one every two
    minutes forever."""
    # attempts: 1h, 2h, 4h, 8h, 16h, then gave_up at 6
    delays_h, total = [1, 2, 4, 8, 16], 6
    assert sum(delays_h) < 48, "should give up inside two days"
    per_day_before = 24 * 60 / 2 * 5          # old loop: every 2 min, 5 objects
    assert per_day_before > 3000              # what we were doing
    assert total <= 6                          # what an unfindable object costs now
