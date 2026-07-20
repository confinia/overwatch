#!/usr/bin/env bash
# Staged blue-green for the stateless HTTP services (web, api) — runs ON the VM.
#
#   stage    build the working tree into the STAGING slot (9081/9082).
#            Production is untouched. Validate the candidate at
#            https://staging.overwatch.confinia.io (basic-auth protected).
#   promote  restart the PROD slot (8081/8082) from the staged images.
#            The prod caddy vhost lists prod first, staging second, with
#            active health checks: during the restart, traffic serves from
#            staging — the same version just validated — so the flip drops
#            no requests and users only ever see old-version -> new-version.
#
# Singletons (db, ingest, grafana, otel-collector, prometheus) stay under
# podman-compose (stateful or no public HTTP surface).
set -euo pipefail
cmd=${1:?usage: slots.sh stage|promote}
cd "$(dirname "$0")/../orbit-poc"

NET=orbit-poc_default
DB_DSN="dbname=orbit user=orbit password=orbit host=db port=5432"
VERSION=$(tr -d '[:space:]' < ../VERSION 2>/dev/null || echo dev)
declare -A WEB_PORT=( [prod]=8081 [staging]=9081 )
declare -A API_PORT=( [prod]=8082 [staging]=9082 )

run_slot() {  # $1 = prod|staging — (re)create the slot from the staged images
  local slot=$1
  podman rm -f "overwatch_web_$slot" "overwatch_api_$slot" >/dev/null 2>&1 || true
  podman run -d --name "overwatch_web_$slot" --network "$NET" \
    -p "127.0.0.1:${WEB_PORT[$slot]}:8080" \
    -e DB_DSN="$DB_DSN" \
    -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
    -e OTEL_SERVICE_NAME=overwatch-web \
    -e OVERWATCH_VERSION="$VERSION" \
    --restart=always overwatch-web:staged >/dev/null
  podman run -d --name "overwatch_api_$slot" --network "$NET" \
    -p "127.0.0.1:${API_PORT[$slot]}:8000" \
    -e DB_DSN="$DB_DSN" \
    -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
    -e OTEL_SERVICE_NAME=overwatch-api \
    -e REQUIRE_API_KEY=false \
    -e OVERWATCH_VERSION="$VERSION" \
    --env-file .env \
    -v "$PWD/deploy/geoip:/geoip:ro" \
    --restart=always overwatch-api:staged >/dev/null
}

healthy() {  # $1 = prod|staging — gate on both services answering
  local slot=$1
  for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:${WEB_PORT[$slot]}/healthz" >/dev/null \
    && curl -sf "http://127.0.0.1:${API_PORT[$slot]}/healthz" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

case $cmd in
stage)
  echo "== building $VERSION into the staging slot"
  podman build -q -t overwatch-web:staged ./web
  podman build -q -t overwatch-api:staged ./api
  podman tag overwatch-web:staged "overwatch-web:$VERSION"
  podman tag overwatch-api:staged "overwatch-api:$VERSION"
  run_slot staging
  if ! healthy staging; then
    echo "!! staging failed its health gate — production untouched"
    podman logs --tail 30 overwatch_web_staging overwatch_api_staging || true
    exit 1
  fi
  echo "== staged $VERSION — validate at https://staging.overwatch.confinia.io"
  echo "   then: make promote"
  ;;
promote)
  if ! healthy staging; then
    echo "!! staging slot is not healthy — run 'make stage' and validate first"
    exit 1
  fi
  echo "== promoting staged images to prod (traffic covers via staging)"
  run_slot prod
  if ! healthy prod; then
    echo "!! prod failed its health gate — traffic is serving from staging"
    podman logs --tail 30 overwatch_web_prod overwatch_api_prod || true
    exit 1
  fi
  # legacy cleanup from the alternating blue/green era
  podman rm -f overwatch_web_blue overwatch_web_green \
    overwatch_api_blue overwatch_api_green >/dev/null 2>&1 || true
  echo "== promoted: prod on :${WEB_PORT[prod]}/:${API_PORT[prod]} ($VERSION)"
  ;;
*)
  echo "usage: slots.sh stage|promote"; exit 2 ;;
esac
