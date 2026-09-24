#!/usr/bin/env bash
# Live proof of the OW-over-YAMCS bridge (#428): a stock quickstart with its
# simulator, the bridge in subscription (ws) mode, points landing in a real
# tenant. Then the two things a mocked test cannot show: YAMCS restarts and
# the bridge meets it with a reconnect (not a downgrade to polling), and
# YAMCS_MODE=poll works against the same instance.
#
#   TENANT_KEY=<tenant key> ./prove.sh
#   TENANT_KEY=... OVERWATCH_URL=http://api:8000 COMPOSE_FILES="-f docker-compose.yml -f docker-compose.internal.yml" \
#     PROVE_READ_URL=http://127.0.0.1:12000/api PROVE_READ_HOST=overwatch.confinia.io ./prove.sh
#
# Exit 0 = every assertion held; the first failed one exits non-zero with
# the bridge's log tail. Needs curl and python3 on the host, docker compose
# or podman-compose. The first run builds YAMCS (minutes): PROVE_UP_TIMEOUT.
set -uo pipefail
cd "$(dirname "$0")"

: "${TENANT_KEY:?set TENANT_KEY to the tenant the bridge pushes into}"
OVERWATCH_URL="${OVERWATCH_URL:-https://overwatch.confinia.io/api}"
# Where THIS script reads back (the bridge's URL may be unreachable from the
# host, e.g. an internal api:8000 on the compose network).
READ_URL="${PROVE_READ_URL:-$OVERWATCH_URL}"
# Host header for the read-back (a local edge routes by name: PROVE_READ_HOST).
READ_HOST="${PROVE_READ_HOST:-$(printf '%s' "$READ_URL" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')}"
UP_TIMEOUT="${PROVE_UP_TIMEOUT:-900}"
FRESH_S="${PROVE_FRESH_S:-30}"          # a point this recent = flowing
COMPOSE_FILES="${COMPOSE_FILES:--f docker-compose.yml}"
export TENANT_KEY OVERWATCH_URL

if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
elif command -v podman-compose >/dev/null 2>&1; then COMPOSE="podman-compose"
else echo "need docker compose or podman-compose" >&2; exit 2; fi
# shellcheck disable=SC2086
compose() { $COMPOSE $COMPOSE_FILES "$@"; }

STEP=0
step() { STEP=$((STEP + 1)); printf '\n%2d. %s\n' "$STEP" "$*"; }
fail() {
  echo "FAILED at step $STEP: $*" >&2
  echo "--- bridge log tail ---" >&2
  compose logs --tail 40 bridge 2>&1 | tail -40 >&2
  exit 1
}
blog() { compose logs bridge 2>&1; }

# Seconds since the newest QuickSat point in the tenant (or a huge number).
age() {
  curl -sf -m 20 -H "Host: $READ_HOST" "$READ_URL/v1/tenants/$TENANT_KEY/satellites" | python3 -c '
import sys, json, datetime as dt
rows = [r for r in json.load(sys.stdin) if r["satellite"] == "QuickSat"]
now = dt.datetime.now(dt.timezone.utc)
ages = [(now - dt.datetime.fromisoformat(r["last"])).total_seconds() for r in rows]
print(int(min(ages)) if ages else 10**9)' 2>/dev/null || echo 1000000000
}

# Wait up to $1 s for a point fresher than FRESH_S; $2 names the wait.
wait_fresh() {
  local deadline=$(( $(date +%s) + $1 )) a
  while :; do
    a=$(age)
    if [ "$a" -le "$FRESH_S" ]; then echo "   points flowing (newest ${a}s old)"; return 0; fi
    [ "$(date +%s)" -lt "$deadline" ] || fail "$2: newest point is ${a}s old after $1s"
    sleep 5
  done
}

# Wait up to $1 s for the bridge log to contain $2.
wait_log() {
  local deadline=$(( $(date +%s) + $1 ))
  until blog | grep -q -- "$2"; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "no '$2' in the bridge log after $1s"
    sleep 3
  done
  echo "   log: $(blog | grep -- "$2" | tail -1 | cut -c1-120)"
}

step "bring the demo up (YAMCS + simulator + bridge, ws mode)"
YAMCS_MODE=auto compose up -d --build >/dev/null 2>&1 || fail "compose up"
# a fresh bridge container, so every log assertion below reads THIS run
YAMCS_MODE=auto compose up -d --force-recreate bridge >/dev/null 2>&1 || fail "recreate bridge"
wait_fresh "$UP_TIMEOUT" "first points"

step "the bridge is on the subscription, not the poll fallback"
blog | grep -q "yamcs-bridge \[auto\]\|yamcs-bridge \[ws\]" || fail "bridge did not announce ws/auto mode"
blog | grep -q "falling back to polling" && fail "bridge fell back to polling"
echo "   ws confirmed"

step "restart YAMCS: the bridge reconnects instead of downgrading"
compose restart yamcs >/dev/null 2>&1 || fail "compose restart yamcs"
wait_log 90 "ws session dropped, reconnecting"
wait_fresh "$UP_TIMEOUT" "points after the YAMCS restart"
blog | grep -q "falling back to polling" && fail "bridge downgraded to polling on the restart"
echo "   still ws after the restart"

step "no duplicate stamps reached the tenant across the reconnect"
curl -sf -m 20 -H "Host: $READ_HOST" "$READ_URL/v1/tenants/$TENANT_KEY/telemetry?satellite=QuickSat&field=Battery1_Voltage&hours=1" \
  | python3 -c '
import sys, json
ts = [r["ts"] for r in json.load(sys.stdin)]
assert ts, "no Battery1_Voltage in the last hour"
assert len(ts) == len(set(ts)), f"{len(ts) - len(set(ts))} duplicate stamps"
print(f"   {len(ts)} points, all distinct")' || fail "duplicates in the tenant"

step "the fallback works: YAMCS_MODE=poll against the same instance"
YAMCS_MODE=poll compose up -d --force-recreate bridge >/dev/null 2>&1 || fail "compose up bridge (poll)"
wait_log 60 "yamcs-bridge \[poll\]"
wait_fresh 120 "points in poll mode"

step "back to the subscription"
YAMCS_MODE=auto compose up -d --force-recreate bridge >/dev/null 2>&1 || fail "compose up bridge (auto)"
wait_log 60 "yamcs-bridge \[auto\]"
wait_fresh 120 "points back in ws mode"

echo
echo "PROVEN: ws subscription, reconnect after a YAMCS restart, no duplicates, poll fallback."
