"""Guards #511: Overwatch was 57% of the shared VM's log lines, 7.0/s
sustained, and 71% of that was three success messages repeated forever.

Measured per journal entry over 5 minutes:

    GET /healthz 200        3.00/s   43%   (both colours, 3 checkers, every 2s)
    POST .../telemetry 202  1.00/s   14%   (the demo pushes at 1 Hz)
    "pushed 8 points"       1.00/s   14%   (the same event, logged again)

The rule these tests defend is one line: **silence repeated success, never
failure.** A health check earns a log line exactly once - on the day it stops
passing.
"""
import importlib.util
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402

HERE = os.path.dirname(__file__)


def _record(path, status, method="GET"):
    """A uvicorn access record: the parts arrive as args, not a message."""
    r = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                          '%s - "%s %s HTTP/%s" %d', None, None)
    r.args = ("10.89.1.2:55746", method, path, "1.1", status)
    return r


# ---- the filter ---------------------------------------------------------

def test_successful_health_checks_are_dropped():
    f = main.QuietSuccessfulProbes()
    for path in ("/healthz", "/v1/healthz", "/api/v1/healthz"):
        assert f.filter(_record(path, 200)) is False, path


def test_a_failing_health_check_is_always_logged():
    """The only day the line was ever worth writing."""
    f = main.QuietSuccessfulProbes()
    for status in (500, 503, 404, 401):
        assert f.filter(_record("/healthz", status)) is True, status


def test_tenant_telemetry_pushes_are_dropped_by_shape_not_by_name():
    f = main.QuietSuccessfulProbes()
    uuid = "139343d1-3059-491f-b829-7d1e4869c60d"
    for path in (f"/v1/tenants/{uuid}/telemetry",
                 f"/api/v1/tenants/{uuid}/telemetry"):
        assert f.filter(_record(path, 202, "POST")) is False, path
    # a failed push is a real event: a tenant losing data must not be silent
    assert f.filter(_record(f"/v1/tenants/{uuid}/telemetry", 429, "POST")) is True


def test_everything_else_still_gets_logged():
    f = main.QuietSuccessfulProbes()
    for path in ("/v1/satellites", "/v1/demo/satellite", "/v1", "/",
                 "/v1/tenants/abc/satellites", "/v1/healthzzz"):
        assert f.filter(_record(path, 200)) is True, path


def test_a_query_string_or_trailing_slash_does_not_defeat_it():
    f = main.QuietSuccessfulProbes()
    for path in ("/healthz?probe=1", "/healthz/", "/v1/healthz?x=1"):
        assert f.filter(_record(path, 200)) is False, path


def test_a_record_of_an_unexpected_shape_is_left_alone():
    """A filter that swallows what it does not understand is worse than the
    noise it removes."""
    f = main.QuietSuccessfulProbes()
    plain = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                              "something else entirely", None, None)
    assert f.filter(plain) is True
    odd = _record("/healthz", 200)
    odd.args = ("only", "three", "args")
    assert f.filter(odd) is True
    nonint = _record("/healthz", 200)
    nonint.args = ("c", "GET", "/healthz", "1.1", "200")   # status as a string
    assert f.filter(nonint) is True


def test_the_suppression_is_announced_and_can_be_turned_off():
    """Silently dropping lines is how somebody loses an afternoon to a
    request that left no trace."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert "install_access_log_filter()" in src
    start = src.index("async def lifespan")
    assert "access log:" in src[start:start + 900], \
        "start-up must say that lines are being dropped"
    assert 'os.environ.get(\n        "ACCESS_LOG_QUIET"' in src \
        or '"ACCESS_LOG_QUIET"' in src


# ---- the bridge ---------------------------------------------------------

def _bridge():
    path = os.path.join(HERE, "..", "bridge", "yamcs", "bridge.py")
    spec = importlib.util.spec_from_file_location("bridge_logvol", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_bridge_reports_the_first_push_immediately():
    """A fresh bridge must not stay silent for a minute: whether it works at
    all is worth more than the lines saved."""
    b = _bridge()
    out = []
    p = b.Progress(60, now=lambda: 1000.0, out=out.append)
    p.add(8)
    assert out == ["pushed 8 points"]


def test_the_bridge_then_reports_on_a_timer_not_per_push():
    b = _bridge()
    out, clock = [], [1000.0]
    p = b.Progress(60, now=lambda: clock[0], out=out.append)
    p.add(8)                       # first, immediate
    for _ in range(59):            # a minute of 1 Hz pushes
        clock[0] += 1
        p.add(8)
    assert len(out) == 1, "no second line before the interval elapses"
    clock[0] += 1
    p.add(8)
    assert len(out) == 2
    assert "pushed 480 points in 60 batches over 60s" == out[1], out[1]


def test_a_minute_of_pushes_costs_one_line_instead_of_sixty():
    """The measured change: 1.00/s -> ~0.017/s from this container."""
    b = _bridge()
    out, clock = [], [0.0]
    p = b.Progress(60, now=lambda: clock[0], out=out.append)
    for _ in range(600):           # ten minutes at 1 Hz
        p.add(8)
        clock[0] += 1
    assert len(out) <= 11, f"{len(out)} lines for 600 pushes"


def test_the_bridge_still_logs_failures_immediately():
    src = open(os.path.join(HERE, "..", "bridge", "yamcs", "bridge.py"),
               encoding="utf-8").read()
    for msg in ("cycle failed, retrying next poll",
                "ws subscription failed",
                "ws session dropped, reconnecting"):
        assert msg in src, msg
        line = src[src.index(msg):]
        assert "stderr" in line[:200], f"{msg} must still go to stderr at once"
