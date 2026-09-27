"""Guards the two things that surfaced once the #511 noise stopped.

With health-check spam gone, a settled 5-minute sample put Overwatch at 2.29/s
— and 1.00/s of that was ONE line, `METER dry-run`, from a single 1 Hz tenant.
44% of what was left. The other find was `orbit-poc_db_1` failing its
healthcheck ~22 times per 5 minutes on a 3s timeout, which is three journal
lines each time AND a flapping health state that `depends_on` reads.
"""
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, HERE)


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _metering():
    """Fresh module each time: the aggregator keeps state between calls."""
    spec = importlib.util.spec_from_file_location(
        "metering_logtest", os.path.join(HERE, "metering.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _event(qty=8, customer="demo-tenant", name="frame_ingested"):
    return {"name": name, "external_customer_id": customer,
            "metadata": {"quantity": qty}}


def test_the_first_dry_run_still_logs_immediately():
    """The audit property is 'you can see it working without credentials'."""
    m = _metering()
    out = []
    m.log.info = lambda fmt, *a: out.append(fmt % a)
    m._log_dry_run(_event(), now=0.0)
    assert len(out) == 1 and "METER dry-run" in out[0]
    assert "frame_ingested" in out[0]


def test_a_second_of_pushes_does_not_become_a_second_of_lines():
    m = _metering()
    out = []
    m.log.info = lambda fmt, *a: out.append(fmt % a)
    for i in range(60):                      # a minute at 1 Hz
        m._log_dry_run(_event(), now=float(i))
    assert len(out) == 1, f"{len(out)} lines for 60 events inside the window"
    m._log_dry_run(_event(), now=61.0)       # past the window
    assert len(out) == 2
    assert "x60" in out[1] and "qty=" in out[1], out[1]


def test_the_summary_carries_the_count_and_the_quantity():
    """A rolling count has to say what it stands for, or it is worse than
    the lines it replaced."""
    m = _metering()
    out = []
    m.log.info = lambda fmt, *a: out.append(fmt % a)
    m._log_dry_run(_event(qty=8), now=0.0)
    for i in range(1, 11):
        m._log_dry_run(_event(qty=8), now=float(i))
    m._log_dry_run(_event(qty=8), now=100.0)
    assert re.search(r"x11 frame_ingested qty=88 over 100s for demo-tenant", out[1]), out[1]


def test_tenants_and_event_types_are_counted_separately():
    """One tenant going quiet must not be hidden by another staying busy."""
    m = _metering()
    out = []
    m.log.info = lambda fmt, *a: out.append(fmt % a)
    m._log_dry_run(_event(customer="a"), now=0.0)
    m._log_dry_run(_event(customer="b"), now=0.0)
    m._log_dry_run(_event(customer="a", name="tm_request"), now=0.0)
    assert len(out) == 3, "each customer+event pair reports its own first line"


def test_a_real_emission_is_never_aggregated():
    """Only the DRY-RUN path is summarised: a real Polar call and every
    failure still log individually."""
    src = _read("orbit-poc", "api", "metering.py")
    emit = src[src.index("def _emit("):]
    dry = emit[:emit.index("try:")]
    assert "_log_dry_run(event" in dry
    assert "log.warning" in emit, "failures still log"
    assert emit.count("_log_dry_run") == 1


# ---- the healthcheck -----------------------------------------------------

def test_the_database_healthcheck_survives_disk_latency():
    """#492/#493 for the third time. pg_isready is cheap; its start-up under
    sustained iowait is not, and a 3s timeout made orbit-poc_db_1 fail ~22
    times per 5 minutes — three journal lines each, plus a health state that
    `depends_on: service_healthy` believes."""
    for path in ("orbit-poc/docker-compose.yml",
                 "orbit-poc/sandbox/docker-compose.yml",
                 "orbit-poc/staging/docker-compose.yml",
                 "orbit-poc/v2/docker-compose.yml"):
        body = _read(*path.split("/"))
        for block in re.finditer(r"healthcheck:\n(?:\s+#.*\n|\s+\w[^\n]*\n)+", body):
            b = block.group(0)
            if "pg_isready" not in b:
                continue
            m = re.search(r"timeout:\s*(\d+)s", b)
            assert m and int(m.group(1)) >= 20, \
                f"{path}: pg_isready healthcheck timeout is {m and m.group(1)}s"
