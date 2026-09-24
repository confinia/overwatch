#!/bin/sh
# Run YAMCS and its simulator together, so parameters have live values.
set -e
cd /qs

echo "demo: starting YAMCS..."
mvn -q -B yamcs:run &
YAMCS_PID=$!

echo "demo: waiting for the YAMCS API..."
until curl -sf http://localhost:8090/api/instances >/dev/null 2>&1; do sleep 3; done
echo "demo: YAMCS is up, starting the simulator (TM playback)..."

# simulator.py plays testdata.ccsds as UDP TM into the udp-in link (:10015).
# It plays the file ONCE (86,400 packets = 24 h at 1 Hz) and then idles with
# the process alive, so a demo left running goes silent after a day while
# everything still reports healthy (the live run, #428). Restart it just
# before the file ends, for ever.
(
  while true; do
    timeout 86000 python3 simulator.py --tm_host localhost --tm_port 10015 \
                                       --tc_host localhost --tc_port 10025 || true
    echo "demo: simulator playback ended, restarting it"
  done
) &

# keep the container alive on YAMCS; if it exits, we exit
wait ${YAMCS_PID}
