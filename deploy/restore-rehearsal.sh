#!/usr/bin/env bash
# Prove a backup can actually be restored — WITHOUT touching anything live.
#
#   deploy/restore-rehearsal.sh                 # newest orbit dump
#   deploy/restore-rehearsal.sh keycloak        # newest keycloak dump
#   deploy/restore-rehearsal.sh orbit <file>    # a specific dump
#
# A dump that has never been restored is a belief, not a backup. deploy/
# restore.sh loads into the LIVE database and asks for confirmation, so it is
# never rehearsed — which is how you discover at 3am that the dumps were
# unusable all along. This restores into a THROWAWAY Postgres container —
# empty cluster, no roles, nothing of ours in it — loads the globals and then
# the data, checks the rows are really there, and destroys it. Safe to run
# unattended, and non-zero on failure so a systemd unit surfaces it.
#
# A fresh cluster is the point. Restoring into a scratch database inside the
# LIVE Postgres passes even when the backup is missing roles, because they
# still exist there: a rehearsal that runs in the environment it is meant to
# replace tests the environment, not the backup. That is exactly how the
# missing-roles gap stayed hidden.
#
# Portable across products: everything specific is an env var. Another stack
# reuses this by setting RH_CONTAINER / RH_USER / RH_DB / RH_EXPECT.
#
#   RH_BACKUP_DIR   where dumps live                  (default ~/backups)
#   RH_CONTAINER    postgres container                (default per target)
#   RH_USER         postgres role                     (default per target)
#   RH_DB           database name inside the throwaway cluster (per target)
#   RH_IMAGE        postgres image for the throwaway    (default postgres:16)
#   RH_READY_TRIES  2s waits for the cluster to start    (default 30 = 60s)
#   RH_EXPECT       "table:minrows,table:minrows"     (default per target)
set -euo pipefail

TARGET="${1:-orbit}"
FILE="${2:-}"
DIR="${RH_BACKUP_DIR:-$HOME/backups}"

case "$TARGET" in
  orbit)
    CONTAINER="${RH_CONTAINER:-orbit-poc_db_1}"; PGUSER="${RH_USER:-orbit}"
    LIVE_DB="${RH_DB:-orbit}"
    EXPECT="${RH_EXPECT:-satellite:1,telemetry:1}" ;;
  keycloak)
    CONTAINER="${RH_CONTAINER:-ovw2_kc-db_1}"; PGUSER="${RH_USER:-keycloak}"
    LIVE_DB="${RH_DB:-keycloak}"
    EXPECT="${RH_EXPECT:-realm:1,user_entity:1}" ;;
  *) echo "usage: restore-rehearsal.sh [orbit|keycloak] [file]" >&2; exit 2 ;;
esac

fail() { echo "!! REHEARSAL FAILED: $*" >&2; exit 1; }

# Newest dump for this target unless one was named.
if [ -z "$FILE" ]; then
  # `${TARGET}-*` also matches `${TARGET}-globals-*`; restoring the roles file
  # as if it were data fails with "role already exists".
  FILE=$(ls -1t "$DIR/${TARGET}-"*.sql.gz "$DIR/${TARGET}-"*.sql.gz.age 2>/dev/null \
         | grep -v -- '-globals-' | sed -n 1p) || true
  [ -n "$FILE" ] || fail "no ${TARGET} dump found in $DIR"
fi
[ -f "$FILE" ] || fail "no such file: $FILE"

RH_IMAGE="${RH_IMAGE:-docker.io/library/postgres:16}"
SANDBOX="rehearsal-$TARGET-$$"

echo "== rehearsing $(basename "$FILE") in a FRESH $RH_IMAGE cluster"

PLAIN=$(mktemp)
cleanup() {
  podman rm -f "$SANDBOX" >/dev/null 2>&1 || true
  rm -f "$PLAIN" "$PLAIN.err"
}
trap cleanup EXIT

# The throwaway cluster never touches the live one: separate container,
# no volume, destroyed on exit.
podman run -d --name "$SANDBOX" -e POSTGRES_PASSWORD=rehearsal \
  -e POSTGRES_USER="$PGUSER" -e POSTGRES_DB="$LIVE_DB" "$RH_IMAGE" >/dev/null \
  || fail "could not start the throwaway cluster"
# initdb on a fsync-bound array needs 25-40s idle and well over a minute
# under load, so a fixed 60s declares a HEALTHY backup unrestorable — the
# false negative most likely to get a rehearsal quietly abandoned. Reported
# by the gitlab tenant, who hit it on their first three runs.
READY_TRIES="${RH_READY_TRIES:-30}"
for _ in $(seq 1 "$READY_TRIES"); do
  podman exec "$SANDBOX" pg_isready -U "$PGUSER" >/dev/null 2>&1 && break
  sleep 2
done
podman exec "$SANDBOX" pg_isready -U "$PGUSER" >/dev/null 2>&1 \
  || fail "the throwaway cluster was not ready after $((READY_TRIES * 2))s — \
this is a timeout, NOT a verdict on the backup; raise RH_READY_TRIES and retry"

psql_() { podman exec -i "$SANDBOX" psql -qtA -U "$PGUSER" "$@"; }

# --- decompress (and decrypt where the dump is encrypted) --------------------
if [[ "$FILE" == *.age ]]; then
  [ -n "${BACKUP_AGE_IDENTITY:-}" ] || fail "encrypted dump but BACKUP_AGE_IDENTITY unset"
  age -d -i "$BACKUP_AGE_IDENTITY" "$FILE" | gunzip > "$PLAIN" \
    || fail "decrypt/decompress failed"
else
  gunzip -c "$FILE" > "$PLAIN" || fail "decompress failed"
fi
BYTES=$(wc -c < "$PLAIN")
[ "$BYTES" -gt 1024 ] || fail "dump expands to $BYTES bytes — nothing to restore"
echo "   expands to $(numfmt --to=iec "$BYTES" 2>/dev/null || echo "$BYTES")B"

# --- every role the dump names must be in the globals dump -------------------
# A scratch restore inside the LIVE cluster passes even when roles are missing
# from the backup, because they still exist here — which is exactly how the
# gap hid. Compare instead against what the backup actually captured.
# Load the globals into the empty cluster — not grep them. In a fresh cluster
# a missing role is a hard error on the first OWNER/GRANT, which is precisely
# what a real disaster recovery would hit.
GLOBALS=$(ls -1t "$DIR/${TARGET}-globals-"*.sql.gz* 2>/dev/null | sed -n 1p) || true
[ -n "$GLOBALS" ] \
  || fail "no globals dump beside this one — roles are NOT backed up, and a restore onto a fresh Postgres would fail"
if [[ "$GLOBALS" == *.age ]]; then
  age -d -i "${BACKUP_AGE_IDENTITY:?}" "$GLOBALS" | gunzip | psql_ -d "$LIVE_DB" >/dev/null 2>&1 || true
else
  gunzip -c "$GLOBALS" | psql_ -d "$LIVE_DB" >/dev/null 2>&1 || true
fi
ROLES=$(psql_ -d "$LIVE_DB" -c "SELECT count(*) FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%'")
echo "   globals loaded: $ROLES roles now exist in the fresh cluster"

# --- restore the data into the fresh cluster --------------------------------
if ! psql_ -q -d "$LIVE_DB" -v ON_ERROR_STOP=1 < "$PLAIN" >/dev/null 2>"$PLAIN.err"; then
  echo "   psql errors:"; sed -n '1,5p' "$PLAIN.err" >&2
  fail "the dump did not load into a fresh cluster"
fi

# --- prove the data is actually there ---------------------------------------
IFS=',' read -ra CHECKS <<< "$EXPECT"
for check in "${CHECKS[@]}"; do
  table="${check%%:*}"; minrows="${check##*:}"
  rows=$(psql_ -d "$LIVE_DB" -c "SELECT count(*) FROM \"$table\"" 2>/dev/null) \
    || fail "table $table is missing from the restored dump"
  [ "$rows" -ge "$minrows" ] \
    || fail "$table restored with $rows rows (expected >= $minrows)"
  echo "   $table: $rows rows"
done

echo "== OK — $(basename "$FILE") restores into an EMPTY cluster and contains data"
