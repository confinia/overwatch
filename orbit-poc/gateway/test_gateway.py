"""Unit tests for the SatNOGS egress gateway logic. No network, no DB: `get`,
`now`, `sleep` and `record` are injected, so these prove the gate, the cache
and the cooldown behave — the properties that keep us a considerate consumer."""
import errno
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import gateway  # noqa: E402


class FakeResp:
    def __init__(self, status, body=b"{}", headers=None):
        self.status_code = status
        self.content = body
        self.headers = headers or {"Content-Type": "application/json"}


class Clock:
    """Controllable time: sleep advances the clock, so pacing is testable."""
    def __init__(self, t=1000.0):
        self.t = t
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _gw(tmp_path, get, clock=None, token="tok", calls=None):
    clock = clock or Clock()
    calls = calls if calls is not None else []
    g = gateway.Gateway(
        get=get, sleep=clock.sleep, now=clock.now, token=token,
        upstream="https://db.satnogs.org/api", min_gap=11,
        cooldown_file=str(tmp_path / "cooldown"),
        record=lambda ep, st, ms, caller="unknown": calls.append((ep, st)),
    )
    return g, clock, calls


def test_ttl_for_matches_the_published_cadences():
    assert gateway.ttl_for("/telemetry/") == 1800
    assert gateway.ttl_for("/tle/") == 21600
    assert gateway.ttl_for("/satellites/") == 86400
    assert gateway.ttl_for("/unknown/") == gateway.DEFAULT_TTL
    # full-proxy: the /api/ prefix is stripped, and non-api pages are proxied too
    assert gateway.ttl_for("/api/telemetry/") == 1800
    assert gateway.ttl_for("/satellite/57175") == 86400


def test_cache_hit_never_touches_upstream(tmp_path):
    hits = {"n": 0}

    def get(url, headers, timeout):
        hits["n"] += 1
        return FakeResp(200, b'[{"a":1}]')

    g, _, _ = _gw(tmp_path, get)
    s1, b1, _, d1 = g.fetch("/telemetry/", "sat_id=X", 1800)
    s2, b2, _, d2 = g.fetch("/telemetry/", "sat_id=X", 1800)
    assert hits["n"] == 1, "the second identical request must be served from cache"
    assert (d1, d2) == ("MISS", "HIT")
    assert b1 == b2 == b'[{"a":1}]'


def test_one_global_gate_paces_real_requests(tmp_path):
    def get(url, headers, timeout):
        return FakeResp(200)

    g, clock, _ = _gw(tmp_path, get)
    g.fetch("/telemetry/", "sat_id=A", 1800)   # first: no wait
    g.fetch("/telemetry/", "sat_id=B", 1800)   # different key -> real -> must wait
    assert clock.slept and abs(clock.slept[-1] - 11) < 0.001, \
        "a second real request inside the gap must be held to min_gap"


def test_slots_are_reserved_in_arrival_order_one_per_gap(tmp_path):
    """Three arrivals at the same instant get slots at 0, gap, 2*gap: the
    queue is deterministic, nobody starves behind a lucky latecomer."""
    def get(url, headers, timeout):
        return FakeResp(200)

    g, clock, _ = _gw(tmp_path, get)
    g.max_wait = 1000
    assert g.pace() == 0
    assert g.pace() == 11            # clock.sleep advanced the clock by 11
    assert g.pace() == 11            # then the next one, another 11 later
    assert g.retry_after() == 11


def test_busy_refuses_at_once_without_spending_a_slot(tmp_path):
    """#450: a caller that would wait longer than max_wait gets 503 BUSY now,
    no upstream request is made for it, and the queue is unchanged, so the
    slot goes to a caller that is still there to read the reply."""
    hits = {"n": 0}

    def get(url, headers, timeout):
        hits["n"] += 1
        return FakeResp(200, b"[]")

    clock = Clock()
    g, _, calls = _gw(tmp_path, get, clock=clock)
    g.max_wait = 15
    g.fetch("/telemetry/", "sat_id=A", 1800)          # slot at t0
    # a second caller holds the next slot (t0+11) and is still sleeping for it;
    # simulate that reservation without advancing the clock
    g._next_slot = clock.now() + 22
    # a third arrival would wait 22s > 15: refused at once
    st, body, _, disp = g.fetch("/telemetry/", "sat_id=C", 1800)
    assert (st, disp) == (503, "BUSY")
    assert b"busy" in body
    assert hits["n"] == 1, "BUSY must not touch upstream"
    assert calls == [("/telemetry/", 200)], "BUSY is not a real request, not recorded"
    assert g.retry_after() == 22, "Retry-After says when the queue is drained"
    assert clock.slept == [], "the refused caller was not held"
    # queue untouched: once max_wait allows it, the next arrival gets that slot
    g.max_wait = 60
    assert g.pace() == 22
    assert hits["n"] == 1


def test_pagination_links_are_rewritten_to_the_gateway(tmp_path):
    """#450: SatNOGS answers with absolute next/previous links. Followed
    verbatim they leave the door (and hit the blackhole); rewritten they keep
    page 2 paced, cached and recorded like page 1."""
    upstream = b'{"next":"https://db.satnogs.org/api/telemetry/?cursor=abc&sat_id=A",' \
               b'"previous":null,"results":[{"a":1}]}'

    def get(url, headers, timeout):
        return FakeResp(200, upstream)

    g, _, _ = _gw(tmp_path, get)
    g.public_base = "http://satnogs-gateway:8088"
    _, body, _, _ = g.fetch("/api/telemetry/", "sat_id=A", 1800)
    assert b'"next":"http://satnogs-gateway:8088/api/telemetry/?cursor=abc&sat_id=A"' in body
    assert b"db.satnogs.org" not in body
    # the rewritten body is what the cache serves too
    _, body2, _, disp = g.fetch("/api/telemetry/", "sat_id=A", 1800)
    assert disp == "HIT" and body2 == body
    # non-JSON bodies (the /satellite/<norad> pages) are passed through untouched
    html = b'<a href="https://db.satnogs.org/satellite/1">x</a>'
    g2, _, _ = _gw(tmp_path, lambda u, headers, timeout: FakeResp(200, html, {"Content-Type": "text/html"}))
    _, body3, _, _ = g2.fetch("/satellite/1", "", 86400)
    assert body3 == html


def test_429_sets_cooldown_then_short_circuits(tmp_path):
    def get(url, headers, timeout):
        return FakeResp(429, b'{"detail":"throttled"}', {"Retry-After": "40",
                                                         "Content-Type": "application/json"})

    g, clock, calls = _gw(tmp_path, get)
    st, _, _, disp = g.fetch("/telemetry/", "sat_id=A", 1800)
    assert st == 429 and disp == "MISS"
    assert g.cooling() >= 40, "Retry-After must arm a cooldown"
    # while cooling, a DIFFERENT request must not reach upstream at all
    st2, _, _, disp2 = g.fetch("/telemetry/", "sat_id=B", 1800)
    assert disp2 == "COOL" and st2 == 503
    assert calls == [("/telemetry/", 429)], "only the real request is recorded"


def test_timeout_backs_off_too(tmp_path):
    def get(url, headers, timeout):
        raise RuntimeError("connect timed out")

    g, _, calls = _gw(tmp_path, get)
    st, _, _, disp = g.fetch("/tle/", "norad_cat_id=25544", 21600)
    assert st == 502 and disp == "ERR"
    assert g.cooling() > 0, "a timeout is a refusal to honour, not a reason to retry now"
    assert calls == [("/tle/", None)], "the failed attempt is still recorded (status None)"


def test_block_signature_backs_off_hard_not_every_minute(tmp_path):
    """A firewall block (network unreachable / admin-prohibited) must earn the
    LONG backoff, so a blocked gateway probes ~hourly instead of knocking every
    minute on a provider that has deliberately shut us out."""
    import errno as _errno

    def get(url, headers, timeout):
        raise OSError(_errno.ENETUNREACH, "Network is unreachable")

    g, _, calls = _gw(tmp_path, get)
    g.timeout_cooldown, g.block_cooldown = 60, 3600
    st, _, _, disp = g.fetch("/telemetry/", "sat_id=A", 1800)
    assert (st, disp) == (502, "ERR")
    assert g.cooling() >= 3599, "an admin-prohibited block must arm the 1h backoff"
    assert calls == [("/telemetry/", None)], "the blocked attempt is still recorded"


def test_transient_timeout_keeps_the_short_backoff(tmp_path):
    def get(url, headers, timeout):
        raise RuntimeError("HTTPSConnectionPool: Read timed out")

    g, _, _ = _gw(tmp_path, get)
    g.timeout_cooldown, g.block_cooldown = 60, 3600
    g.fetch("/tle/", "norad_cat_id=25544", 21600)
    assert 59 <= g.cooling() <= 61, \
        "a plain timeout is a blip: the short backoff, not the 1h block one"


def test_is_block_classifies_by_errno_and_by_message():
    import errno as _errno
    assert gateway.is_block(OSError(_errno.ENETUNREACH, "Network is unreachable"))
    assert gateway.is_block(OSError(_errno.ECONNREFUSED, "Connection refused"))
    # requests wraps the socket error; the __cause__ chain must be walked
    wrapped = RuntimeError("connect failed")
    wrapped.__cause__ = OSError(_errno.EHOSTUNREACH, "No route to host")
    assert gateway.is_block(wrapped)
    # a plain read timeout is NOT a block — it stays transient
    assert not gateway.is_block(RuntimeError("Read timed out"))
    assert not gateway.is_block(TimeoutError("timed out"))


def test_token_is_injected_callers_never_hold_it(tmp_path):
    seen = {}

    def get(url, headers, timeout):
        seen.update(headers)
        return FakeResp(200)

    g, _, _ = _gw(tmp_path, get, token="secret-token")
    g.fetch("/satellites/", "", 86400)
    assert seen.get("Authorization") == "Token secret-token"
    assert "User-Agent" in seen


def test_cooldown_persists_across_restart(tmp_path):
    def get(url, headers, timeout):
        return FakeResp(200)

    g, clock, _ = _gw(tmp_path, get)
    g.set_cooldown(120)
    # a fresh Gateway (a restart) reads the persisted cooldown from disk
    g2 = gateway.Gateway(get=get, sleep=clock.sleep, now=clock.now,
                         cooldown_file=str(tmp_path / "cooldown"))
    assert g2.cooling() >= 119, "a restart must not resume hammering a cooled provider"


def test_caller_of_prefers_header_then_ua_token_then_unknown():
    assert gateway.caller_of({"X-Overwatch-Caller": "ingest"}) == "ingest"
    assert gateway.caller_of({"X-Overwatch-Caller": "batch-sweep_full"}) == "batch-sweep_full"
    # no header: an unlabelled script still attributes by its UA product token
    assert gateway.caller_of({"User-Agent": "python-requests/2.32.3"}) == "python-requests/2.32.3"
    assert gateway.caller_of({"User-Agent": "overwatch/1.0 (+https://x; c@x)"}) == "overwatch/1.0"
    # sanitised + capped: safe as a metric label, bounded cardinality
    assert gateway.caller_of({"X-Overwatch-Caller": "Bad Caller!!<script>"}) == "bad-caller-script"
    assert len(gateway.caller_of({"X-Overwatch-Caller": "x" * 200})) == 40
    assert gateway.caller_of({}) == "unknown"


def test_caller_is_recorded_but_never_forwarded_upstream(tmp_path):
    """The whole point: SatNOGS sees ONE identity (the gateway's UA + token),
    while OUR attribution knows who asked. A per-caller header on the wire would
    read as UA rotation to a provider that blocked us twice."""
    upstream_headers = {}
    recorded = []

    def get(url, headers, timeout):
        upstream_headers.update(headers)
        return FakeResp(200, b"[]")

    g = gateway.Gateway(get=get, sleep=lambda s: None, now=lambda: 1000.0,
                        token="tok", upstream="https://db.satnogs.org/api", min_gap=0,
                        cooldown_file=str(tmp_path / "cooldown"),
                        record=lambda ep, st, ms, caller: recorded.append(caller))
    g.fetch("/telemetry/", "sat_id=A", 1800, caller="batch-sweep_full")
    assert recorded == ["batch-sweep_full"], "the caller must reach our own recorder"
    assert "X-Overwatch-Caller" not in upstream_headers, "attribution must NOT go upstream"
    assert upstream_headers["User-Agent"] == gateway.UA, \
        "upstream always sees the gateway's own stable UA, never the caller's"
    assert upstream_headers["Authorization"] == "Token tok"


def test_every_real_request_is_recorded(tmp_path):
    def get(url, headers, timeout):
        return FakeResp(200, b'[]')

    g, _, calls = _gw(tmp_path, get)
    g.fetch("/telemetry/", "sat_id=A", 1800)
    g.fetch("/telemetry/", "sat_id=A", 1800)   # cache hit -> NOT a real request
    g.fetch("/telemetry/", "sat_id=B", 1800)
    assert calls == [("/telemetry/", 200), ("/telemetry/", 200)], \
        "cache hits must not be recorded as upstream load"


def test_upstream_reachability_is_passive_and_follows_real_outcomes(tmp_path):
    outcome = {"ok": True}

    def get(url, headers, timeout):
        if not outcome["ok"]:
            raise OSError(errno.ENETUNREACH, "Network is unreachable")
        return FakeResp(200, b'[]')

    g, clock, calls = _gw(tmp_path, get)
    g.stale_after = 3600
    ok, info = g.reachability()
    assert (ok, info["state"]) == (True, "idle"), "no attempt yet must not page"
    assert calls == [], "reachability must never send anything upstream"

    g.fetch("/telemetry/", "sat_id=A", 1800)
    assert g.reachability()[1]["state"] == "reachable"

    outcome["ok"] = False
    clock.t += 100
    g.fetch("/telemetry/", "sat_id=B", 1800)          # fails, success is 100s old
    ok, info = g.reachability()
    assert (ok, info["state"], info["last_fail"]) == (True, "degraded", "blocked")

    clock.t += 3600                                   # success is now stale
    g._cooldown_until = 0
    g.fetch("/telemetry/", "sat_id=C", 1800)
    ok, info = g.reachability()
    assert (ok, info["state"]) == (False, "down")
    assert info["last_ok_age_s"] >= 3600
    assert len(calls) == 3, "the probe itself added no upstream request"
