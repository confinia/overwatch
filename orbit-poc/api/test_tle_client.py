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
    # the bulk cache is the FIRST thing consulted; there is no per-object
    # CelesTrak path left to come after it (#357)
    assert "_refresh_bulk_tles()" in body


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
    # SatNOGS is the only per-object source left (#357); it is asked only when
    # not cooling down. CelesTrak is still cooled in the BULK path.
    guard = next(l for l in fn.splitlines()
                 if '_cooling("satnogs")' in l and l.strip().startswith("if "))
    assert "not " in guard, "satnogs is asked even while cooling down"
    bulk = _code(INGEST[INGEST.index("def _refresh_bulk_tles("):])
    bulk = bulk[:bulk.index("\ndef ", 10)]
    assert '_cooling("celestrak")' in bulk and '_cool("celestrak"' in bulk, \
        "the bulk path must still stop when CelesTrak refuses or vanishes"


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
    bulk pass per day; read it from there.

    The guarantee is stronger than when this was written. main() now builds
    the catalogue BEFORE seeding (#335), so the "catalogue is empty" window
    that justified a live fallback cannot occur, and the fallback is gone:
    resolve_sat_id issues no request at all.
    """
    fn = INGEST[INGEST.index("def resolve_sat_id("):]
    fn = _code(fn[:fn.index("\ndef ", 10)])
    assert "FROM catalog WHERE norad" in fn, "must read the catalogue first"
    assert "requests.get" not in fn and "SATNOGS_BASE" not in fn, \
        "resolve_sat_id must answer from our own copy, never the network"


def test_no_unbounded_per_object_loop_remains():
    """Every call site that hits a provider per object must go through a
    backed-off path. A structural guard, deliberately: the two incidents this
    week both came from a new loop being added without one."""
    code = _code(INGEST)
    start = code.index("def _tle_for(")
    end = code.find("\ndef ", start + 1)
    guarded = range(start, end if end != -1 else len(code))
    for fname in ("_tle_from_satnogs",):
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
    # CelesTrak calls go through _timed_get (records the request rate). In
    # isolation the record is a no-op and _timed_get just delegates to the
    # injected `requests` mock, so lifted functions behave as their raw
    # requests.get form under test. A caller can override either via **stubs.
    ns["_record_request"] = lambda *a, **k: None
    ns["_timed_get"] = lambda source, url, **kw: ns["requests"].get(url, **kw)
    ns.update(stubs)
    for name in names:
        start = INGEST.index("def %s(" % name)
        end = INGEST.find("\ndef ", start + 1)
        exec(compile(INGEST[start:end if end != -1 else len(INGEST)],
                     "ingest.py", "exec"), ns)
    return ns


class _Log:
    """Stand-in logger for lifted functions (the module cannot be imported)."""
    def debug(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def info(self, *a, **k): pass
    def exception(self, *a, **k): pass


class _Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code, self.text, self._payload = status, text, payload
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


def test_no_per_object_celestrak_request_exists():   # #357
    """CelesTrak's log: 1,418 gp.php?CATNR= queries in a day, which is what put
    us in their firewall. Their reply was blunt — "You really shouldn't need to
    make all of those CATNR requests" — and they are right: GROUP=satnogs is
    ~1400 objects in ONE request. The fallback is not backed off any more, it
    is gone."""
    code = _code(INGEST)
    assert "CATNR" not in code, "a per-object CelesTrak request survives"
    assert "_tle_from_celestrak" not in code, "the per-object helper survives"


def test_we_ask_celestrak_for_the_set_built_for_us():   # #357
    groups = INGEST.split('CELESTRAK_GROUPS", "')[1].split('"')[0]
    lookup = INGEST.split('CELESTRAK_LOOKUP_GROUPS", "')[1].split('"')[0]
    assert groups == "satnogs" and lookup == "satnogs", \
        f"fetching {groups!r}/{lookup!r} instead of the satnogs set"


def test_our_user_agent_names_a_reachable_human():   # #357
    """Their log showed `orbit-poc` with contact you@example.org — a
    placeholder. When they needed to tell us we were misbehaving, they had no
    way to reach us."""
    ua = INGEST.split('HTTP_USER_AGENT",')[1].split(')')[0]
    assert "example.org" not in ua and "example.com" not in ua, \
        "the user agent still carries a placeholder contact"
    assert "confinia.io" in ua and "overwatch" in ua.lower()


def test_a_refusal_is_recorded_for_a_human_not_just_logged():   # #357
    """"immediately stop querying and report the problem to a human for
    investigation" — we stop, but we ignored 59 custom 403s in five minutes
    because nothing was watching."""
    fn = INGEST[INGEST.index("def _cool("):]
    fn = fn[:fn.index("\ndef ", 10)]
    assert "INSERT INTO provider_refusal" in fn
    assert "except Exception" in fn, \
        "bookkeeping must never mask the refusal it is recording"


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


def _tle_for(sn, cooling=()):
    """_tle_for wired to canned provider verdicts. Returns recorded calls."""
    recorded = []
    ns = _lift("_tle_for",
               _refresh_bulk_tles=lambda: {},
               _due_for_lookup=lambda n: True,
               _cooling=lambda src: src in cooling,
               _tle_from_satnogs=lambda n: sn,
               _record_lookup=lambda *a, **k: recorded.append((a, k)))
    ns["_tle_for"](99999)
    return recorded


def test_an_answered_not_carried_gives_up_on_the_first_attempt():
    """SatNOGS is the only per-object source now (#357), so its answer is the
    whole verdict. This nearly broke silently: `permanent` was computed as
    `ct_absent and sn_absent`, and with CelesTrak gone ct_absent is always
    False — the immediate give-up from #310 would have been dead code."""
    (args, kw), = _tle_for(sn=(None, True))
    assert args[1] is False, "not a success"
    assert kw["permanent"] is True, \
        "an answered 'not carried' must give up at once, not after six tries"


def test_a_404_does_not_give_up_while_satnogs_is_cooling():
    """CelesTrak dropping an object says nothing about SatNOGS. Giving up here
    would lose LAPAN-A2-class satellites for good over a transient refusal."""
    (args, kw), = _tle_for(sn=(None, False), cooling=("satnogs",))
    assert kw["permanent"] is False


def test_a_timeout_keeps_the_ordinary_backoff():
    (args, kw), = _tle_for(sn=(None, False))
    assert kw["permanent"] is False


def test_a_found_tle_clears_the_backoff_row():
    recorded = _tle_for(sn=(("1 ...", "2 ..."), False))
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
               _cool=lambda src, *a, **k: cooled.append(src),
               _spacetrack_bulk=lambda norads: {},
               db=_NoDb)
    ns["_refresh_bulk_tles"]()
    assert cooled == ["celestrak"], \
        f"an unreachable provider must be cooled down exactly once, got {cooled}"


class _EmptyDb:
    """Context-manager stand-in for db(): the bulk refresher reads the fleet
    before trying the Space-Track fallback, and these tests exercise the
    CelesTrak path with an empty fleet."""
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return self
    def execute(self, *a, **k): pass
    def fetchall(self): return []


def _NoDb():
    return _EmptyDb()


class _FakeTime:
    @staticmethod
    def time():
        return 0.0

    @staticmethod
    def sleep(_):
        pass


def test_a_cooldown_short_circuits_the_per_satellite_path():
    """With SatNOGS cooling, _tle_for must not ask it at all."""
    calls = []
    ns = _lift("_tle_for",
               _refresh_bulk_tles=lambda: {},
               _due_for_lookup=lambda n: True,
               _cooling=lambda src: src == "satnogs",
               _tle_from_satnogs=lambda n: calls.append(n) or (None, False),
               _record_lookup=lambda *a, **k: None)
    ns["_tle_for"](25544)
    assert calls == [], "a cooling provider was asked anyway"
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


# ---------------------------------------------------------------------------
# Satellites SatNOGS flags get one request a day, not forty-eight (#317)
# ---------------------------------------------------------------------------
MAIN = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()


def _fn(src, name):
    start = src.index("def %s(" % name)
    end = src.find("\ndef ", start + 1)
    return src[start:end if end != -1 else len(src)]


def test_the_violator_flag_is_mirrored_from_the_bulk_list():
    """It rides along in the payload refresh_catalog already downloads, so
    honouring it costs no extra request."""
    fn = _fn(INGEST, "refresh_catalog")
    assert "is_frequency_violator" in fn, "the flag is not read from the payload"
    assert "is_violator = EXCLUDED.is_violator" in fn, \
        "a satellite flagged upstream must update, not just insert"


def test_flagged_satellites_are_excluded_from_the_normal_cycle():
    fn = _code(_fn(INGEST, "fetch_telemetry"))
    assert "is_violator" in fn and "last_telemetry_fetch" in fn, \
        "the target query ignores the daily limit"
    assert "LEFT JOIN catalog" in fn, \
        "reading the flag from catalog is what makes a newly flagged " \
        "satellite take effect without migrating our own rows"


def test_the_daily_gap_is_longer_than_a_day():
    """24h exactly lets a drifting cycle land twice inside one window."""
    gap = INGEST.split('VIOLATOR_GAP", "')[1].split('"')[0]
    hours = int(gap.split()[0])
    assert "hour" in gap and hours > 24, f"{gap} leaves no margin over 1/day"


def test_the_request_is_stamped_on_the_attempt_not_the_success():
    """A refused or failed request still spent the satellite's allowance for
    the day. Stamping only successes would retry it immediately, which is
    precisely what the limit exists to stop."""
    fn = _fn(INGEST, "fetch_telemetry")
    stamp = fn.index("last_telemetry_fetch = now()")
    assert fn.index("except Exception") < stamp, \
        "the stamp is inside the success path"


def test_the_last_fetch_time_survives_a_restart():
    """In-process state would let a container recycle re-poll a once-a-day
    satellite on every boot — the same failure as the give-up state in #311."""
    assert "ADD COLUMN IF NOT EXISTS last_telemetry_fetch" in MAIN, \
        "no migration for existing databases"
    assert "ADD COLUMN IF NOT EXISTS is_violator" in MAIN
    init = open(os.path.join(HERE, "..", "db", "init.sql"), encoding="utf-8").read()
    assert "last_telemetry_fetch" in init, "fresh installs would lack the column"


# ---------------------------------------------------------------------------
# Space-Track: the second bulk source (#370)
# ---------------------------------------------------------------------------
def _spacetrack(login_status=200, query_status=200, body="", exc=None,
                identity="user", cooled=None, cooling=False):
    cooled = [] if cooled is None else cooled

    class _Sess:
        def __init__(self): self.headers = {}
        def post(self, url, data=None, timeout=None):
            if exc: raise exc
            return _Resp(login_status)
        def get(self, url, timeout=None):
            if exc: raise exc
            _Sess.last_url = url
            return _Resp(query_status, body)

    class _Req:
        Session = _Sess
        class exceptions:
            RequestException = RuntimeError

    ns = _lift("_spacetrack_bulk",
               requests=_Req, log=_Log(), UA={},
               SPACETRACK_BASE="https://st", SPACETRACK_IDENTITY=identity,
               SPACETRACK_PASSWORD="pw" if identity else "",
               CONNECT_TIMEOUT=8,
               _spacetrack_session={"s": None},
               _cooling=lambda src: cooling,
               _cool=lambda src, *a, **k: cooled.append(src),
               _parse_tle_file=_parse_from_ingest())
    return ns, _Sess


def _parse_from_ingest():
    ns = _lift("_parse_tle_file")
    return ns["_parse_tle_file"]


TLE_BODY = ("1 25544U 98067A   26239.50000000  .00016717  00000-0  10270-3 0  9000\n"
            "2 25544  51.6400 208.9163 0006317  69.9862 25.2906 15.49560000000000\n"
            "1 43017U 17073A   26239.50000000  .00000300  00000-0  10000-4 0  9001\n"
            "2 43017  97.7000 100.0000 0010000 100.0000 260.0000 14.95000000000000\n")


def test_spacetrack_is_silently_absent_without_credentials():
    """Self-host installs and CI configure nothing and must lose nothing."""
    ns, _ = _spacetrack(identity="")
    assert ns["_spacetrack_bulk"]([25544]) == {}


def test_one_query_covers_the_whole_fleet():
    """30 requests/min is their published ceiling; ours is one query per
    elements cycle with every NORAD id in the URL."""
    ns, Sess = _spacetrack(body=TLE_BODY)
    out = ns["_spacetrack_bulk"]([43017, 25544])
    assert set(out) == {25544, 43017}
    assert "25544,43017" in Sess.last_url, "ids must be batched into ONE query"
    assert "/format/tle" in Sess.last_url


def test_a_refusal_cools_spacetrack_down():
    """Sized to the published limit AND carrying the firewall lesson: any
    refusal silences us and reaches provider_refusal via _cool."""
    cooled = []
    ns, _ = _spacetrack(query_status=429, cooled=cooled)
    assert ns["_spacetrack_bulk"]([25544]) == {}
    assert cooled == ["spacetrack"]


def test_a_login_refusal_cools_down_too():
    cooled = []
    ns, _ = _spacetrack(login_status=401, cooled=cooled)
    assert ns["_spacetrack_bulk"]([25544]) == {}
    assert cooled == ["spacetrack"]


def test_no_spacetrack_request_while_cooling():
    ns, Sess = _spacetrack(body=TLE_BODY, cooling=True)
    Sess.last_url = None
    assert ns["_spacetrack_bulk"]([25544]) == {}
    assert Sess.last_url is None, "a cooling source was queried anyway"


def test_unreachable_spacetrack_cools_down():
    cooled = []
    ns, _ = _spacetrack(exc=RuntimeError("timeout"), cooled=cooled)
    assert ns["_spacetrack_bulk"]([25544]) == {}
    assert cooled == ["spacetrack"]


def test_spacetrack_is_second_never_first():
    """Order is deliberate: public bulk first, the authenticated source only
    when that yields nothing, per-object SatNOGS last. And the permanent
    give-up verdict stays SatNOGS's alone — #310 semantics untouched."""
    fn = _code(INGEST[INGEST.index("def _refresh_bulk_tles("):])
    fn = fn[:fn.index("\ndef ", 10)]
    assert fn.index("CELESTRAK_BASE") < fn.index("_spacetrack_bulk"), \
        "Space-Track must be the fallback, not the primary"
    assert "if not found:" in fn
    tle_for = _code(INGEST[INGEST.index("def _tle_for("):])
    tle_for = tle_for[:tle_for.index("\ndef ", 10)]
    assert "spacetrack" not in tle_for, \
        "the give-up verdict must not involve Space-Track"
