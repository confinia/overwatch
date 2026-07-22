#!/usr/bin/env bash
# Restore a backup produced by deploy/backup.sh — runs ON the VM.
#   deploy/restore.sh orbit    <file.sql.gz[.age]>
#   deploy/restore.sh keycloak <file.sql.gz[.age]>
# Decrypts if needed (age identity in $BACKUP_AGE_IDENTITY), then loads into
# the live database. DESTRUCTIVE: it replaces current data — confirm first.
set -euo pipefail
what=${1:?usage: restore.sh orbit|keycloak <file>}
file=${2:?path to backup file}
case $what in
  orbit)    container=orbit-poc_db_1; user=orbit;    db=orbit ;;
  keycloak) container=ovw2_kc-db_1;   user=keycloak; db=keycloak ;;
  *) echo "unknown target: $what"; exit 2 ;;
esac

read -rp "This REPLACES the live $what database. Type 'restore' to proceed: " ok
[ "$ok" = restore ] || { echo "aborted"; exit 1; }

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
if [[ "$file" == *.age ]]; then
  age -d -i "${BACKUP_AGE_IDENTITY:?set BACKUP_AGE_IDENTITY}" "$file" | gunzip > "$tmp"
else
  gunzip -c "$file" > "$tmp"
fi

podman exec -i "$container" psql -U "$user" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" "$db"
podman exec -i "$container" psql -U "$user" "$db" < "$tmp"
echo "== restored $what from $(basename "$file")"
