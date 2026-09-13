"""Guards #463: a stack recreate must not fake a SatNOGS block, kill the
healthcheck, or silence the ingest for an hour.

Three defects shipped together on the 0fa3c8d stage: the gateway read its own
boot-race ENETUNREACH as a firewall block (grace-tested in
gateway/test_gateway.py), the compose healthcheck never executed because
podman-compose flattens the CMD array through `sh` and the quoted one-liner
died on its parentheses, and refresh_catalog slept through a 2,946 s
Retry-After before main() started a single loop.
"""
import os
import re
import sys
import time
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))


def _read(*parts):
    return open(os.path.join(HERE, *parts), encoding="utf-8").read()


def test_the_gateway_healthcheck_is_plain_argv():
    """podman-compose runs a CMD healthcheck through `sh -c` on the JOINED
    argv, so anything the shell can misparse (quotes, parentheses) makes the
    check fail forever without ever probing the gateway — it did, silently,
    since #449. Only bare words survive every flattening."""
    compose = _read("..", "docker-compose.yml")
    m = re.search(r'healthcheck:\s*\n\s*test:\s*(\[.*?\])\s*\n(?:.*\n)*?'
                  r'\s*interval', compose[compose.index("satnogs-gateway:"):])
    assert m, "the gateway must keep a healthcheck (its restart policy needs it)"
    argv = m.group(1)
    assert re.fullmatch(r'\[\s*"CMD"(\s*,\s*"[A-Za-z0-9_./-]+")+\s*\]', argv), \
        f"healthcheck argv must be shell-proof bare words, got {argv}"


def test_the_healthz_probe_ships_in_the_image():
    assert os.path.exists(os.path.join(HERE, "..", "gateway", "healthz.py"))
    dockerfile = _read("..", "gateway", "Dockerfile")
    assert "healthz.py" in dockerfile, \
        "the Dockerfile must COPY healthz.py or the plain-argv check 404s"


def test_a_long_retry_after_does_not_stall_the_ingest(monkeypatch):
    """refresh_catalog runs BEFORE main() starts any loop; honouring a
    provider cooldown in full (2,946 s observed) kept staging and sandbox
    ingests silent for an hour after every recreate. Beyond the cap it gives
    up for this cycle — the catalog loop returns at CATALOG_INTERVAL."""
    import ingest

    class Resp:
        status_code = 503
        headers = {"Retry-After": "2946"}

    class Cur:
        def execute(self, *a):
            pass

        def fetchone(self):
            return (None,)          # catalog never refreshed: proceed to fetch

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    slept = []
    monkeypatch.setattr(ingest, "db", lambda: Conn())
    monkeypatch.setattr(ingest.requests, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(ingest.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(ingest, "_pace_satnogs", lambda: None)
    t0 = time.monotonic()
    ingest.refresh_catalog()        # must return, not raise, not sleep it out
    assert time.monotonic() - t0 < 5
    assert not any(s > ingest.CATALOG_MAX_RETRY_AFTER for s in slept), \
        "a Retry-After above the cap must abort the cycle, not be slept through"
