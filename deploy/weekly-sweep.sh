#!/usr/bin/env bash
# Weekly SatNOGS decodability sweep (report only), fired by the overwatch
# user's overwatch-sweep.timer (Mon 04:00 UTC). Executes ON the VM.
#
# Three states, three looks (#463): broken sweep = red unit, SatNOGS blocked =
# a "skipped" log line (exit 0, no platform failed-unit alert every Monday),
# ran = the report. The gateway's /upstream is PASSIVE: reading it sends
# nothing to db.satnogs.org.
#
# The sweep itself goes through batch/run.sh, never a bare container (#494):
# that launcher joins the compose network, points SATNOGS_BASE at the egress
# gateway (one shared rate limiter + cache, #449) and blackholes db.satnogs.org,
# so the first Monday after an unblock cannot spend the ingest's budget again.
set -uo pipefail
cd "$(dirname "$0")/.."                       # ~/projects/overwatch on the VM

GATEWAY="${OW_GATEWAY:-http://127.0.0.1:12080}"
REPORT="batch/reports/sweep-$(date +%F).log"
mkdir -p batch/reports

up=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$GATEWAY/upstream")
if [ "$up" != "200" ]; then
  echo "skipped: upstream down (gateway /upstream -> $up)" | tee "$REPORT"
  exit 0
fi

exec batch/run.sh sweep_full.py > "$REPORT" 2>&1
