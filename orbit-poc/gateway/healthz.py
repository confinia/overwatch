"""Container healthcheck: exit 0 iff the gateway answers /healthz.

A separate file, not an inline `python -c`: podman-compose flattens the
healthcheck CMD array through `sh`, and a quoted one-liner with parentheses
dies there with a syntax error — which made the check fail on every probe
since #449, permanently `(unhealthy)`, without ever testing the gateway (#463).
A plain ["CMD", "python", "healthz.py"] has nothing for the shell to mangle.
"""
import sys
import urllib.request

try:
    r = urllib.request.urlopen("http://127.0.0.1:8088/healthz", timeout=3)
    sys.exit(0 if r.status == 200 else 1)
except Exception:  # noqa: BLE001 — any failure is unhealthy
    sys.exit(1)
