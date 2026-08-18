#!/usr/bin/env bash
# First run and every run: boot self-hosted Overwatch (#276).
#
#   cp .env.example .env && $EDITOR .env
#   ./up.sh
#
# Idempotent: the Keycloak bootstrap creates the realm and client only where
# they are missing and updates them where they exist, so re-running after an
# upgrade or a .env change is the normal workflow.
set -euo pipefail
cd "$(dirname "$0")"

die() { echo "FAIL: $*" >&2; exit 1; }

[ -f .env ] || die ".env is missing — copy .env.example and fill it in"
set -a; . ./.env; set +a
for v in PUBLIC_BASE PG_PASSWORD KC_DB_PASSWORD KC_ADMIN_PASSWORD \
         OVERWATCH_CLIENT_SECRET ORG_DB_SECRET GF_ADMIN_PASSWORD; do
  [ -n "${!v:-}" ] || die "$v is empty in .env (openssl rand -hex 24)"
done

# docker compose v2 or podman-compose — whichever this host has
if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
elif command -v podman-compose >/dev/null 2>&1; then COMPOSE="podman-compose"
else die "need docker compose or podman-compose"; fi

echo "== 1/3 building and starting the stack"
$COMPOSE up -d --build

echo "== 2/3 bootstrapping Keycloak (realm, client, organization claim)"
$COMPOSE run --rm --no-deps --entrypoint python3 \
  -v "$PWD/bootstrap_keycloak.py:/bootstrap.py:ro" \
  -e KC_BOOT_BASE=http://keycloak:8080/auth \
  api /bootstrap.py

echo "== 3/3 waiting for the front door"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
         "http://127.0.0.1:${HTTP_BIND##*:}/" || true)
  [ "$code" = "200" ] && break
  [ "$i" = "30" ] && die "the front answers $code — check '$COMPOSE logs caddy web'"
  sleep 5
done

echo
echo "Overwatch is up: $PUBLIC_BASE"
echo "  control room ......... $PUBLIC_BASE/"
echo "  sign in / register ... $PUBLIC_BASE/api/v1/auth/login"
echo "  Grafana .............. $PUBLIC_BASE/grafana"
echo "  API .................. $PUBLIC_BASE/api/v1/satellites"
