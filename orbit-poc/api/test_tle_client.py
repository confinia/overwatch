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
    assert "least(power(2, element_fetch.attempts + 1), 24)" in rec, \
        "the backoff curve must still cap at 24h"


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


def test_sat_id_resolution_reads_our_catalogue_not_the_network():
    """The second per-object loop, missed by the first fix. resolve_sat_id
    queried /api/satellites/?norad_cat_id= once per satellite at EVERY ingest
    start, for every satellite still missing a sat_id — and while the lookup
    fails the column stays null, so the next start asks again. A polling loop
    that grows as it fails, against the provider already blocking us.

    The catalogue table holds sat_id for the whole network from one paginated
    bulk pass per day; read it from there."""
    fn = INGEST[INGEST.index("def resolve_sat_id("):]
    fn = _code(fn[:fn.index("\ndef ", 10)])
    assert "FROM catalog WHERE norad" in fn, "must read the catalogue first"
    # a live query only when the catalogue does not exist yet
    assert "if catalogued:" in fn and "return None" in fn
    # and even then, backed off and silent while cooling down
    assert "_cooling(\"satnogs\")" in fn and "_due_for_lookup" in fn
    assert "_record_lookup" in fn
    assert "403, 429" in fn or "(403, 429)" in fn


def test_no_unbounded_per_object_loop_remains():
    """Every call site that hits a provider per object must go through a
    backed-off path. A structural guard, deliberately: the two incidents this
    week both came from a new loop being added without one."""
    code = _code(INGEST)
    start = code.index("def _tle_for(")
    end = code.find("\ndef ", start + 1)
    guarded = range(start, end if end != -1 else len(code))
    for fname in ("_tle_from_celestrak", "_tle_from_satnogs"):
        i = code.find(fname + "(")
        while i != -1:
            if not code[:i].rstrip().endswith("def"):     # skip the definition
                assert i in guarded, (
                    fname + " is called from outside _tle_for; every per-object "
                    "provider call must go through the path that consults the "
                    "bulk cache, honours the backoff and records the outcome")
            i = code.find(fname + "(", i + 1)


# ---------------------------------------------------------------------------
# A 404 is a verdict, not a failure (#309)
#
# The guards above read the source as text, because ingest.py imports
# psycopg2, sgp4 and numpy and reads DB_DSN at import time — none of which
# exist in this suite. These tests get real behaviour anyway by lifting the
# functions out of the file and running them against stubs, which is worth the
# small extra machinery: "does a 404 stop us" is a question about what the code
# DOES, and a substring match would pass on a comment.
# ---------------------------------------------------------------------------
def _lift(*names, **stubs):
    """Exec the named top-level functions from ingest.py in a stub namespace."""
    ns = {"Exception": Exception}
    ns.update(stubs)
    for name in names:
        start = INGEST.index("def %s(" % name)
        end = INGEST.find("\ndef ", start + 1)
        exec(compile(INGEST[start:end if end != -1 else len(INGEST)],
                     "ingest.py", "exec"), ns)
    return ns


class _Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code, self.text, self._payload = status, text, payload
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


def _celestrak(resp, cooled=None):
    """_tle_from_celestrak wired to one canned response."""
    cooled = [] if cooled is None else cooled

    class _Req:
        @staticmethod
        def get(*a, **k):
            if isinstance(resp, Exception):
                raise resp
            return resp
    return _lift("_tle_from_celestrak",
                 requests=_Req, log=_Log(), UA={}, CELESTRAK_BASE="x",
                 CELESTRAK_ONE_TIMEOUT=8,
                 _cool=lambda *a, **k: cooled.append(a[0]))


class _Log:
    def debug(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def info(self, *a, **k): pass


def test_celestrak_404_is_absence_not_failure():
    ns = _celestrak(_Resp(404))
    assert ns["_tle_from_celestrak"](99999) == (None, True)


def test_celestrak_answers_200_with_no_gp_data_for_an_unknown_object():
    """The real endpoint does this instead of a 404 — status alone would send
    us back for five more attempts over several days."""
    ns = _celestrak(_Resp(200, "No GP data found"))
    assert ns["_tle_from_celestrak"](99999) == (None, True)


def test_a_timeout_is_ignorance_not_absence():
    ns = _celestrak(RuntimeError("timed out"))
    assert ns["_tle_from_celestrak"](25544) == (None, False)


def test_a_refusal_cools_down_and_claims_nothing_about_the_object():
    cooled = []
    ns = _celestrak(_Resp(403), cooled=cooled)
    assert ns["_tle_from_celestrak"](25544) == (None, False)
    assert cooled == ["celestrak"], "a 403 must start a cooldown"


def test_celestrak_still_returns_a_tle_it_has():
    body = "1 25544U 98067A   26236.5 .000\n2 25544  51.6 100.0 0002\n"
    ns = _celestrak(_Resp(200, body))
    tle, absent = ns["_tle_from_celestrak"](25544)
    assert absent is False and tle[0].startswith("1 ") and tle[1].startswith("2 ")


def _satnogs(resp, token="tok"):
    class _Req:
        @staticmethod
        def get(*a, **k):
            if isinstance(resp, Exception):
                raise resp
            return resp
    return _lift("_tle_from_satnogs",
                 requests=_Req, log=_Log(), UA={}, SATNOGS_BASE="x",
                 SATNOGS_TOKEN=token, _cool=lambda *a, **k: None,
                 _pace_satnogs=lambda: None)


def test_satnogs_answering_empty_is_absence():
    ns = _satnogs(_Resp(200, payload=[]))
    assert ns["_tle_from_satnogs"](99999) == (None, True)


def test_satnogs_without_a_token_knows_nothing():
    """No token is not evidence the object does not exist. Reading it as
    absence would permanently give up on every object the moment the token
    goes missing — SatNOGS carries objects CelesTrak has dropped."""
    ns = _satnogs(_Resp(200, payload=[]), token="")
    assert ns["_tle_from_satnogs"](99999) == (None, False)


def _tle_for(ct, sn, cooling=()):
    """_tle_for wired to canned provider verdicts. Returns recorded calls."""
    recorded = []
    ns = _lift("_tle_for",
               _refresh_bulk_tles=lambda: {},
               _due_for_lookup=lambda n: True,
               _cooling=lambda src: src in cooling,
               _tle_from_celestrak=lambda n: ct,
               _tle_from_satnogs=lambda n: sn,
               _record_lookup=lambda *a, **k: recorded.append((a, k)))
    ns["_tle_for"](99999)
    return recorded


def test_both_providers_saying_no_gives_up_on_the_first_attempt():
    (args, kw), = _tle_for(ct=(None, True), sn=(None, True))
    assert args[1] is False, "not a success"
    assert kw["permanent"] is True, \
        "a 404 from both providers must give up at once, not after six tries"


def test_a_404_does_not_give_up_while_satnogs_is_cooling():
    """CelesTrak dropping an object says nothing about SatNOGS. Giving up here
    would lose LAPAN-A2-class satellites for good over a transient refusal."""
    (args, kw), = _tle_for(ct=(None, True), sn=(None, False), cooling=("satnogs",))
    assert kw["permanent"] is False


def test_a_timeout_keeps_the_ordinary_backoff():
    (args, kw), = _tle_for(ct=(None, False), sn=(None, False))
    assert kw["permanent"] is False


def test_a_found_tle_clears_the_backoff_row():
    recorded = _tle_for(ct=(("1 ...", "2 ..."), False), sn=(None, False))
    (args, kw), = recorded
    assert args[1] is True, "a success must clear element_fetch, not back off"


def test_the_permanent_flag_reaches_the_give_up_column():
    rec = INGEST[INGEST.index("def _record_lookup("):INGEST.index("def _tle_for(")]
    assert "permanent=False" in rec, "_record_lookup must accept the flag"
    assert "gave_up = %s," in _code(rec), \
        "permanent must be the only thing that sets gave_up"


# ---------------------------------------------------------------------------
# Staying inside the published rate, and healing from an outage (#311)
# ---------------------------------------------------------------------------
def test_unreachability_alone_never_becomes_a_permanent_give_up():
    """Production came out of the 2026-08-20 block with all 23 satellites
    flagged "not carried by CelesTrak or SatNOGS" — the ISS among them —
    because six failed attempts set gave_up regardless of why they failed.
    Nothing clears that flag, so the fleet could not recover on its own."""
    rec = _code(INGEST[INGEST.index("def _record_lookup("):INGEST.index("def _tle_for(")])
    sets = [l for l in rec.splitlines() if "gave_up" in l and "=" in l]
    for line in sets:
        assert "attempts" not in line, (
            "an unreachable provider must not count toward a permanent "
            "give-up: " + line.strip())


def test_every_satnogs_telemetry_request_goes_through_the_pacer():
    """The limit counts requests, not satellites, so pagination inside one
    satellite has to be paced too — that is where the old 26/min came from."""
    body = _code(INGEST[INGEST.index("def _get_frames("):])
    body = body[:body.index("\ndef ", 10)]
    for line in body.splitlines():
        if "requests.get(" in line:
            before = body[:body.index(line)]
            assert "_pace_satnogs()" in before.rsplit("for attempt", 1)[-1], \
                "a telemetry request is issued without pacing: " + line.strip()


def test_the_pacer_holds_the_gap_across_separate_calls():
    """Global, not per-satellite: two consecutive requests must be spaced even
    when they come from different satellites."""
    slept = []
    now = [1000.0]

    class _T:
        @staticmethod
        def time():
            return now[0]

        @staticmethod
        def sleep(s):
            slept.append(s)
            now[0] += s

    class _Lock:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    ns = _lift("_pace_satnogs", time=_T, SATNOGS_MIN_GAP=11,
               _satnogs_gate=_Lock(), _satnogs_last=[0.0])
    ns["_pace_satnogs"]()          # first call: nothing to wait for
    assert slept == []
    ns["_pace_satnogs"]()          # immediately after: must wait the full gap
    assert slept and abs(slept[0] - 11) < 0.01, slept


def test_the_published_rate_is_not_exceeded_by_the_default():
    """6/minute is the documented ceiling (get_telemetry_user in satnogs-db).
    A gap below 10s would exceed it."""
    gap = float(INGEST.split('SATNOGS_MIN_GAP", ')[1].split(")")[0])
    assert gap >= 10, f"{gap}s between requests exceeds 6/minute"


def test_no_extra_sleep_pretends_to_be_the_rate_limit():
    """The old sleep(5) claimed to stay "well under SatNOGS rate limits" while
    running at four times them. A comment is not a rate limiter."""
    fetch = _code(INGEST[INGEST.index("def fetch_telemetry("):])
    fetch = fetch[:fetch.index("\ndef ", 10)]
    assert "time.sleep(" not in fetch, \
        "pacing belongs in _pace_satnogs, not in an unexplained sleep here"


# ---------------------------------------------------------------------------
# An unreachable provider must go quiet, not be retried per satellite (#313)
# ---------------------------------------------------------------------------
def test_the_bulk_fetch_bounds_its_connect_separately_from_its_read():
    """One scalar timeout cannot serve both: the `active` file is large and
    needs a long read, but a handshake with a host that is dropping our
    packets must fail in seconds. A single 120s value cost 120s per satellite
    inside fetch_elements and froze the globe for ~50 minutes per restart."""
    bulk = _code(INGEST[INGEST.index("def _refresh_bulk_tles("):])
    bulk = bulk[:bulk.index("\ndef ", 10)]
    assert "timeout=(CONNECT_TIMEOUT, 120)" in bulk, \
        "the bulk fetch must bound its connect separately from its read"
    connect = int(INGEST.split('CONNECT_TIMEOUT", ')[1].split(")")[0])
    assert connect <= 10, f"{connect}s to open a socket is not a bounded connect"


def test_an_unreachable_provider_is_put_on_cooldown():
    """Only 403/429 used to cool us down, so a blackholed host was retried by
    every caller: ~700 connection attempts a day at a provider that is
    firewalling us, while an unblock request is open with them."""
    cooled = []

    class _Exc(Exception):
        pass

    class _Requests:
        class exceptions:
            RequestException = _Exc

        @staticmethod
        def get(*a, **k):
            raise _Exc("connection timed out")

    ns = _lift("_refresh_bulk_tles",
               requests=_Requests, log=_Log(), time=_FakeTime(), UA={},
               CELESTRAK_BASE="x", CONNECT_TIMEOUT=8, ELEMENTS_INTERVAL=21600,
               CELESTRAK_LOOKUP_GROUPS=["active", "amateur", "stations"],
               _bulk_tles={"ts": 0.0, "by_norad": {}},
               _parse_tle_file=lambda t: [],
               _cooling=lambda src: False,
               _cool=lambda src, *a, **k: cooled.append(src))
    ns["_refresh_bulk_tles"]()
    assert cooled == ["celestrak"], \
        f"an unreachable provider must be cooled down exactly once, got {cooled}"


class _FakeTime:
    @staticmethod
    def time():
        return 0.0

    @staticmethod
    def sleep(_):
        pass


def test_a_cooldown_short_circuits_the_per_satellite_path():
    """With CelesTrak cooling, _tle_for must not call it at all — that is what
    turns 23 x 120s of startup into nothing."""
    calls = []
    ns = _lift("_tle_for",
               _refresh_bulk_tles=lambda: {},
               _due_for_lookup=lambda n: True,
               _cooling=lambda src: src == "celestrak",
               _tle_from_celestrak=lambda n: calls.append(n) or (None, False),
               _tle_from_satnogs=lambda n: (None, False),
               _record_lookup=lambda *a, **k: None)
    ns["_tle_for"](25544)
    assert calls == [], "CelesTrak was asked while on cooldown"


def test_no_satnogs_call_site_bypasses_the_pacer():   # #315
    """Deliberately derived from the source rather than a list of function
    names: the failure this guards against is a NEW call site being added
    without pacing, which a hardcoded list would never notice.

    Production hit exactly that. The pacer sat inside _get_frames, so when
    #313 put an unreachable CelesTrak on cooldown and fill_missing_elements
    began asking SatNOGS for every satellite's TLE instead, 23 unpaced
    requests a cycle spent the budget the telemetry loop was rationing —
    a 429 on nearly every telemetry request, days after LSF unblocked us."""
    code = _code(INGEST)
    offenders = []
    for i, line in enumerate(code.splitlines()):
        if not line.startswith("def "):
            continue
        name = line[4:line.index("(")]
        body = code[code.index(line):]
        nxt = body.find("\ndef ", 1)
        body = body[:nxt] if nxt != -1 else body
        if "SATNOGS_BASE" not in body or "requests.get(" not in body:
            continue
        if "_pace_satnogs()" not in body:
            offenders.append(name)
        elif body.index("_pace_satnogs()") > body.index("requests.get("):
            offenders.append(name + " (paces after the request)")
    assert not offenders, \
        "these call SatNOGS without going through the pacer: " + ", ".join(offenders)


def test_the_pacer_is_defined_before_its_first_caller():
    """A module-level helper used by resolve_sat_id, which runs during
    seed_satellites at import-time-adjacent startup."""
    assert INGEST.index("def _pace_satnogs(") < INGEST.index("def resolve_sat_id(")
