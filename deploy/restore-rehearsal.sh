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
# unusable all along. This restores into a scratch database in the same
# Postgres, checks the data is really there, and drops it again. Safe to run
# unattended, and non-zero on failure so a systemd unit surfaces it.
#
# Portable across products: everything specific is an env var. Another stack
# reuses this by setting RH_CONTAINER / RH_USER / RH_DB / RH_EXPECT.
#
#   RH_BACKUP_DIR   where dumps live                  (default ~/backups)
#   RH_CONTAINER    postgres container                (default per target)
#   RH_USER         postgres role                     (default per target)
#   RH_DB           the LIVE db name — never written  (default per target)
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

SCRATCH="rehearsal_${TARGET}_$$"
[ "$SCRATCH" != "$LIVE_DB" ] || fail "refusing to touch the live database"

echo "== rehearsing $(basename "$FILE") -> scratch db $SCRATCH"

psql_() { podman exec -i "$CONTAINER" psql -qtA -U "$PGUSER" "$@"; }

cleanup() {
  psql_ -d postgres -c "DROP DATABASE IF EXISTS \"$SCRATCH\"" >/dev/null 2>&1 || true
  rm -f "$PLAIN"
}
PLAIN=$(mktemp)
trap cleanup EXIT

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
GLOBALS=$(ls -1t "$DIR/${TARGET}-globals-"*.sql.gz* 2>/dev/null | sed -n 1p) || true
if [ -n "$GLOBALS" ]; then
  HAVE=$(gunzip -c "$GLOBALS" 2>/dev/null | grep -oE '^CREATE ROLE [A-Za-z0-9_]+' \
         | awk '{print $3}' | sort -u)
  NEED=$(grep -ohE '(OWNER TO|GRANT [^;]* TO) [A-Za-z0-9_]+' "$PLAIN" \
         | awk '{print $NF}' | sort -u | grep -vE '^(PUBLIC|postgres)$' || true)
  MISSING=$(comm -23 <(echo "$NEED") <(echo "$HAVE") | grep -v '^$' || true)
  [ -z "$MISSING" ] || fail "the backup names roles it does not contain: $(echo $MISSING | tr '\n' ' ')"
  echo "   roles: $(echo "$HAVE" | grep -c .) captured, all referenced roles present"
else
  echo "   !! no globals dump beside this one — roles are NOT backed up" >&2
  fail "cluster roles missing from the backup set (restore onto a fresh Postgres would fail)"
fi

# --- restore into the scratch database --------------------------------------
psql_ -d postgres -c "DROP DATABASE IF EXISTS \"$SCRATCH\"" >/dev/null
psql_ -d postgres -c "CREATE DATABASE \"$SCRATCH\"" >/dev/null || fail "cannot create scratch db"
if ! podman exec -i "$CONTAINER" psql -q -U "$PGUSER" -d "$SCRATCH" \
       -v ON_ERROR_STOP=1 < "$PLAIN" >/dev/null 2>"$PLAIN.err"; then
  echo "   psql errors:"; sed -n '1,5p' "$PLAIN.err" >&2; rm -f "$PLAIN.err"
  fail "the dump did not load"
fi
rm -f "$PLAIN.err"

# --- prove the data is actually there ---------------------------------------
IFS=',' read -ra CHECKS <<< "$EXPECT"
for check in "${CHECKS[@]}"; do
  table="${check%%:*}"; minrows="${check##*:}"
  rows=$(psql_ -d "$SCRATCH" -c "SELECT count(*) FROM \"$table\"" 2>/dev/null) \
    || fail "table $table is missing from the restored dump"
  [ "$rows" -ge "$minrows" ] \
    || fail "$table restored with $rows rows (expected >= $minrows)"
  echo "   $table: $rows rows"
done

echo "== OK — $(basename "$FILE") restores and contains data"
