"""Unit tests for the SatNOGS egress gateway logic. No network, no DB: `get`,
`now`, `sleep` and `record` are injected, so these prove the gate, the cache
and the cooldown behave — the properties that keep us a considerate consumer."""
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
        record=lambda ep, st, ms: calls.append((ep, st)),
    )
    return g, clock, calls


def test_ttl_for_matches_the_published_cadences():
    assert gateway.ttl_for("/telemetry/") == 1800
    assert gateway.ttl_for("/tle/") == 21600
    assert gateway.ttl_for("/satellites/") == 86400
    assert gateway.ttl_for("/unknown/") == gateway.DEFAULT_TTL


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


def test_every_real_request_is_recorded(tmp_path):
    def get(url, headers, timeout):
        return FakeResp(200, b'[]')

    g, _, calls = _gw(tmp_path, get)
    g.fetch("/telemetry/", "sat_id=A", 1800)
    g.fetch("/telemetry/", "sat_id=A", 1800)   # cache hit -> NOT a real request
    g.fetch("/telemetry/", "sat_id=B", 1800)
    assert calls == [("/telemetry/", 200), ("/telemetry/", 200)], \
        "cache hits must not be recorded as upstream load"
