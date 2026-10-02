"""Guards #531: the demo's telemetry is swept; nobody else's ever is.

tenant_telemetry reached 1507 MB / 4.4M rows, growing ~250k a day from one
source — the public demo pushing 8 fields at 1 Hz forever — on a host whose
disks are the bottleneck (#492).

The dangerous part is not the growth, it is the delete. A real tenant's
telemetry is their data. Verified against the live database before shipping:
the selection took 2,865,026 rows, every one of them `demo = true`, and left
all 152,262 non-demo rows alone despite every one being older than the window.
"""
import os
import re

HERE = os.path.dirname(__file__)


def _sweep_source() -> str:
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    start = src.index("def _sweep_demo_telemetry(")
    return src[start:src.index("\ndef ", start + 1)]


def test_the_delete_can_only_reach_a_tenant_flagged_demo():
    """The whole safety property, in one assertion. If this join is ever
    loosened, a paying tenant's history goes with it."""
    fn = _sweep_source()
    delete = fn[fn.index("DELETE FROM tenant_telemetry"):]
    assert "JOIN tenant" in delete and "tenant.demo" in delete, \
        "the delete must join on the demo flag, not filter in Python"
    assert "tenant.key = tt.tenant" in delete, "joined on the right column"
    # and there must be no path that deletes without that join
    assert fn.count("DELETE FROM tenant_telemetry") == 1


def test_the_window_cannot_delete_anything_readable():
    """The endpoint caps `hours` at 48, so nothing can ask for older data.
    A retention window shorter than that would delete what a caller may
    legitimately request."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    default = int(re.search(r'DEMO_RETENTION_HOURS", (\d+)\)', src).group(1))
    # the DEMO endpoint's cap specifically — other endpoints take `hours` too
    # (receptions caps at 168) but read `telemetry`/`frame`, not this table.
    sig = src[src.index("def demo_satellite("):]
    sig = sig[:sig.index(")")]
    cap = int(re.search(r"le=(\d+)", sig).group(1))
    assert default >= cap, \
        f"retention {default}h would delete data the demo endpoint serves ({cap}h)"
    # and no OTHER reader of this table may outlive the window unnoticed
    readers = [m.group(1) for m in
               re.finditer(r'@app\.get\("(/v1/[^"]*)"\)', src)]
    assert "/v1/demo/satellite" in readers


def test_the_delete_is_bounded_per_cycle():
    """One unbounded DELETE of millions of rows holds locks and hammers a
    disk every other product on the VM shares."""
    fn = _sweep_source()
    assert "LIMIT %s" in fn, "each statement must be bounded"
    assert "DEMO_RETENTION_BATCH" in fn
    assert re.search(r"for _ in range\(\d+\)", fn), "and the loop itself bounded"
    assert "commit()" in fn, "commit per batch, not one giant transaction"


def test_a_failure_never_kills_the_loop_and_silence_is_quiet():
    fn = _sweep_source()
    assert "except Exception" in fn
    assert "if removed:" in fn, "say nothing when there was nothing to remove"


def test_the_sweep_is_actually_scheduled():
    """A sweep that is never called is the same as no sweep."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert "_demo_retention_loop()" in src
    loop = src[src.index("def _demo_retention_loop("):]
    loop = loop[:loop.index("\ndef ")]
    assert "_sweep_demo_telemetry()" in loop
    assert "threading.Thread" in loop and "daemon=True" in loop
    assert "while True" in loop, "recurring: the table grows every day"
