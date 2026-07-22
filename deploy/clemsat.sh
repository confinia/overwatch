#!/usr/bin/env bash
# CLEMSAT-1 demo control (#27) — runs ON the VM.
#   clemsat.sh up    provision a 'clemsat-demo' tenant + start the generator
#   clemsat.sh down  stop it and purge the demo tenant's data
set -euo pipefail
cmd=${1:?usage: clemsat.sh up|down}
cd "$(dirname "$0")/../orbit-poc"
STATE="$HOME/clemsat-demo-token"
PSQL="podman exec -i orbit-poc_db_1 psql -U orbit -t -A"

case $cmd in
up)
  TK=$($PSQL -c "WITH ins AS (INSERT INTO tenant (name,email) VALUES ('clemsat-demo','demo@confinia.io') RETURNING key) SELECT key FROM ins;" | head -1 | tr -d '[:space:]')
  echo "$TK" > "$STATE"
  podman build -q -t localhost/clemsat:latest ./clemsat >/dev/null
  podman rm -f clemsat >/dev/null 2>&1 || true
  podman run -d --name clemsat --network orbit-poc_default --restart=always \
    -e API_BASE=http://orbit-poc_caddy_1 -e HOST=overwatch.confinia.io -e TENANT_TOKEN="$TK" \
    localhost/clemsat:latest >/dev/null
  echo "CLEMSAT-1 demo tenant: $TK"
  ;;
down)
  podman rm -f clemsat >/dev/null 2>&1 || true
  TK=$(cat "$STATE" 2>/dev/null || true)
  if [ -n "$TK" ]; then
    $PSQL -c "DELETE FROM tenant_telemetry WHERE tenant='$TK'::uuid;" >/dev/null
    $PSQL -c "DELETE FROM tenant WHERE key='$TK'::uuid;" >/dev/null
    rm -f "$STATE"
  fi
  echo "clemsat demo removed"
  ;;
*) echo "usage: clemsat.sh up|down"; exit 2 ;;
esac
