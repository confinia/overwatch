#!/usr/bin/env bash
# Blue/green as two complete, independent compose stacks — runs ON the VM.
#
#   blue  = podman-compose project "blue"  (docker-compose.blue.yml,  :8081/:8082)
#   green = podman-compose project "green" (docker-compose.green.yml, :9081/:9082)
#
# One color is LIVE (production), the other is the CANDIDATE (served at
# https://staging.overwatch.confinia.io behind basic-auth). The app caddy's
# config is generated from deploy/caddy/Caddyfile.tmpl with the two colors
# substituted; switching = regenerate + graceful reload. No container is
# touched at promote time, so:
#   - deploys drop zero requests (health-checked failover during reload),
#   - the previous color keeps running -> `rollback` is instant.
#
#   stage     build the working tree into the CANDIDATE color, point staging at it
#   promote   swap colors in the caddy config (candidate becomes LIVE)
#   rollback  swap back (previous color still runs the previous version)
#   status    show colors, versions, container state
set -euo pipefail
cmd=${1:?usage: slots.sh stage|promote|rollback|reload|status}
cd "$(dirname "$0")/../orbit-poc"

CADDY_DIR=deploy/caddy
STATE=$CADDY_DIR/LIVE_COLOR
live=$(cat "$STATE" 2>/dev/null || echo blue)
cand=$([ "$live" = blue ] && echo green || echo blue)
declare -A WEB_PORT=( [blue]=8081 [green]=9081 )
declare -A API_PORT=( [blue]=8082 [green]=9082 )

gen_caddy() {  # $1 = live color, $2 = candidate color
  sed -e "s/%LIVE%/$1/g" -e "s/%CANDIDATE%/$2/g" \
    "$CADDY_DIR/Caddyfile.tmpl" > "$CADDY_DIR/Caddyfile.new"
  # Validate in an ephemeral container (never the running one: after a
  # rsync it may hold stale inodes) before touching the mounted file.
  podman run --rm -v "$PWD/$CADDY_DIR:/check:ro" \
    docker.io/library/caddy:2.11.4-alpine \
    caddy validate --config /check/Caddyfile.new --adapter caddyfile >/dev/null
  mv "$CADDY_DIR/Caddyfile.new" "$CADDY_DIR/Caddyfile"
  podman exec orbit-poc_caddy_1 caddy reload --config /etc/caddy/Caddyfile
}

healthy() {  # $1 = color
  local c=$1
  for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:${WEB_PORT[$c]}/healthz" >/dev/null \
    && curl -sf "http://127.0.0.1:${API_PORT[$c]}/healthz" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

case $cmd in
stage)
  echo "== live: $live — building candidate: $cand"
  # --no-cache for the api: podman's layer cache misses modified COPYs
  # (same gotcha as the confinia api) — a stale build passing the health
  # gate is worse than a slow one.
  podman-compose -p "$cand" -f "docker-compose.$cand.yml" build --no-cache api 2>&1 | tail -1
  podman-compose -p "$cand" -f "docker-compose.$cand.yml" build web 2>&1 | tail -1
  # podman-compose's `up -d` does NOT reliably recreate running containers
  # on a new image — remove the candidate's containers first (safe: the
  # candidate is never the live color; caddy health-checks cover the gap).
  podman rm -f "${cand}_web_1" "${cand}_api_1" >/dev/null 2>&1 || true
  podman-compose -p "$cand" -f "docker-compose.$cand.yml" up -d 2>&1 | tail -2
  for c in $(podman ps --format '{{.Names}}' | grep -E "^${cand}_"); do
    podman update --restart=always "$c" >/dev/null
  done
  if ! healthy "$cand"; then
    echo "!! candidate $cand failed its health gate — live ($live) untouched"
    podman logs --tail 30 "${cand}_web_1" "${cand}_api_1" || true
    exit 1
  fi
  gen_caddy "$live" "$cand"
  echo "== staged on $cand — validate at https://staging.overwatch.confinia.io"
  echo "   then: make promote   (or make rollback later if regret)"
  ;;
promote)
  if ! healthy "$cand"; then
    echo "!! candidate $cand is not healthy — run 'make stage' first"
    exit 1
  fi
  gen_caddy "$cand" "$live"          # candidate becomes LIVE, old live becomes fallback+staging
  echo "$cand" > "$STATE"
  echo "== promoted: $cand is LIVE; $live keeps running ($(podman ps --filter name=${live}_web_1 --format '{{.Status}}')) — instant rollback available"
  ;;
reload)
  # Config-only change (Caddyfile.tmpl edited): regenerate with the current
  # colors and graceful-reload — no builds, no container churn.
  gen_caddy "$live" "$cand"
  echo "== app caddy reloaded (live: $live, candidate: $cand)"
  ;;
rollback)
  if ! healthy "$cand"; then
    echo "!! previous color $cand is not healthy — cannot roll back onto it"
    exit 1
  fi
  gen_caddy "$cand" "$live"
  echo "$cand" > "$STATE"
  echo "== rolled back: $cand is LIVE again"
  ;;
status)
  echo "LIVE: $live — candidate: $cand"
  for c in blue green; do
    printf "%s: web " "$c"
    curl -sf "http://127.0.0.1:${WEB_PORT[$c]}/healthz" >/dev/null && printf "up" || printf "DOWN"
    printf " api "
    curl -sf "http://127.0.0.1:${API_PORT[$c]}/api/version" >/dev/null 2>&1 || true
    curl -s "http://127.0.0.1:${API_PORT[$c]}/healthz" | head -c 120; echo
  done
  podman ps --format '{{.Names}} {{.Status}}' | grep -E '^(blue|green)_' || true
  ;;
*)
  echo "usage: slots.sh stage|promote|rollback|reload|status"; exit 2 ;;
esac
