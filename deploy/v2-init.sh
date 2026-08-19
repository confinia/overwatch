#!/usr/bin/env bash
# Keycloak post-boot init — runs ON the VM after `podman-compose -p ovw2 up`.
# Idempotent: waits for KC, then enforces the pieces realm-import cannot
# carry safely: the real client secret (from v2/.env) and the organization
# scope as default on the single client.
set -euo pipefail
cd "$(dirname "$0")/../orbit-poc/v2"
source .env

echo "== waiting for keycloak"
for _ in $(seq 1 60); do
  curl -sf http://127.0.0.1:12070/auth/realms/overwatch/.well-known/openid-configuration >/dev/null && break
  sleep 2
done

KC="podman exec ovw2_keycloak_1 /opt/keycloak/bin/kcadm.sh"
# Authenticate as the NAMED admin, not the bootstrap account (#36). The
# bootstrap credentials remain in v2/.env solely so Keycloak can create a first
# admin when it initialises an EMPTY database; the account itself is disabled.
$KC config credentials --server http://127.0.0.1:8080/auth \
  --realm master --user "$KC_ADMIN_USERNAME" --password "$KC_ADMIN_PASSWORD" >/dev/null

# sed -n 1p, not head -1: head exits early, kcadm takes SIGPIPE, and under
# pipefail the assignment fails even though the id was read.
CID=$($KC get clients -r overwatch -q clientId=overwatch --fields id --format csv --noquotes | sed -n 1p)
$KC update "clients/$CID" -r overwatch -s "secret=$OVERWATCH_CLIENT_SECRET"
echo "== client secret set (client $CID)"
$KC update "clients/$CID" -r overwatch -s 'defaultClientScopes+=organization' 2>/dev/null || true
echo "== v2 init done"
