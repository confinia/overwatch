"""Container healthcheck: exit 0 iff the probe loop's heartbeat is fresh.

A plain file, plain argv — the lesson of #463: podman-compose flattens the
healthcheck CMD through `sh`, so anything quotable dies silently. The probe
loop touches BEAT_FILE each pass; a wedged loop turns the container
unhealthy, so the monitor is itself monitored (rule 34 applies to it too).
"""
import os
import sys
import time

beat = os.environ.get("BEAT_FILE", "/tmp/beat")
try:
    fresh = (time.time() - os.stat(beat).st_mtime) < 3 * float(
        os.environ.get("PROBE_INTERVAL", 60))
    sys.exit(0 if fresh else 1)
except OSError:
    sys.exit(1)
