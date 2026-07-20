#!/usr/bin/env bash
# Blue-green deploy for the stateless HTTP services (web, api) — runs ON the VM.
#
# The caddy edge lists both color slots per service (blue 808x, green 908x)
# with lb_policy first + health checks: traffic goes to the first HEALTHY
# slot. We build the new version into the idle slot, gate on its health,
# then retire the old slot — caddy fails over between the two without a
# reload and without dropping requests.
#
# Singletons (db, ingest, grafana, otel-collector, prometheus) stay under
# podman-compose: they are stateful or have no public HTTP surface, and
# their restarts don't take the SaaS down.
set -euo pipefail
cd "$(dirname "$0")/../orbit-poc"

NET=orbit-poc_default
DB_DSN="dbname=orbit user=orbit password=orbit host=db port=5432"
declare -A WEB_PORT=( [blue]=8081 [green]=9081 )
declare -A API_PORT=( [blue]=8082 [green]=9082 )

running() { podman ps --format '{{.Names}}' | grep -qx "$1"; }

active=none
running overwatch_web_blue  && active=blue
running overwatch_web_green && active=green
target=green; [ "$active" = green ] && target=blue
echo "== active slot: $active -> deploying to: $target"

echo "== building images"
podman build -q -t "overwatch-web:$target" ./web
podman build -q -t "overwatch-api:$target" ./api

echo "== starting $target slot"
podman rm -f "overwatch_web_$target" "overwatch_api_$target" >/dev/null 2>&1 || true
podman run -d --name "overwatch_web_$target" --network "$NET" \
  -p "127.0.0.1:${WEB_PORT[$target]}:8080" \
  -e DB_DSN="$DB_DSN" \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
  -e OTEL_SERVICE_NAME=overwatch-web \
  --restart=always "overwatch-web:$target" >/dev/null
podman run -d --name "overwatch_api_$target" --network "$NET" \
  -p "127.0.0.1:${API_PORT[$target]}:8000" \
  -e DB_DSN="$DB_DSN" \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
  -e OTEL_SERVICE_NAME=overwatch-api \
  -e REQUIRE_API_KEY=false \
  --env-file .env \
  -v "$PWD/deploy/geoip:/geoip:ro" \
  --restart=always "overwatch-api:$target" >/dev/null

echo "== health gate ($target)"
ok=0
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${WEB_PORT[$target]}/healthz" >/dev/null \
  && curl -sf "http://127.0.0.1:${API_PORT[$target]}/healthz" >/dev/null; then
    ok=1; break
  fi
  sleep 1
done
if [ "$ok" != 1 ]; then
  echo "!! $target failed its health gate — old slot stays live, new slot kept for debugging"
  podman logs --tail 30 "overwatch_web_$target" "overwatch_api_$target" || true
  exit 1
fi

echo "== retiring old slot"
if [ "$active" != none ]; then
  podman rm -f "overwatch_web_$active" "overwatch_api_$active" >/dev/null 2>&1 || true
fi
# First-migration cleanup: the compose-managed originals, if still present.
podman rm -f orbit-poc_web_1 orbit-poc_api_1 >/dev/null 2>&1 || true

echo "== live slot: $target (web :${WEB_PORT[$target]}, api :${API_PORT[$target]})"
