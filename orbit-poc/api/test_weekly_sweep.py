"""Guards #494: the weekly SatNOGS sweep goes through the egress gateway.

The VM unit used to run sweep_full.py with a bare `podman run`, token in the
environment, no SATNOGS_BASE: only the "/upstream is down, skip" pre-check
stood between it and db.satnogs.org. The first Monday after the block lifts,
that would have spent the ingest's budget outside the gateway (#449).
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_the_sweep_is_versioned_and_runs_through_the_launcher():
    sh = _read("deploy", "weekly-sweep.sh")
    assert "batch/run.sh sweep_full.py" in sh
    assert "podman run" not in sh, "bare podman run bypasses the gateway"
    assert "SATNOGS_TOKEN" not in sh and "TOKEN=" not in sh, \
        "the launcher holds the token, the wrapper never sees it"
    assert os.access(os.path.join(ROOT, "deploy", "weekly-sweep.sh"), os.X_OK)


def test_a_blocked_upstream_skips_instead_of_failing():
    sh = _read("deploy", "weekly-sweep.sh")
    assert "/upstream" in sh
    assert re.search(r'!= "200".*?\n.*?skipped: upstream down.*?\n\s*exit 0', sh, re.S)


def test_the_launcher_still_blackholes_satnogs():
    sh = _read("batch", "run.sh")
    assert "--add-host db.satnogs.org:127.0.0.1" in sh
    assert 'SATNOGS_BASE="http://satnogs-gateway:8088/api"' in sh
    assert "OVERWATCH_CALLER" in sh, "the SPOT board needs the caller label"


def test_the_sweep_script_reads_the_gateway_base():
    py = _read("batch", "sweep_full.py")
    assert 'os.environ.get("SATNOGS_BASE"' in py
    assert "X-Overwatch-Caller" in py
