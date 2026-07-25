#!/usr/bin/env bash
# Encrypted off-container backup of the stateful data — runs ON the VM
# (daily systemd timer). Covers what cannot be rebuilt from git:
#   - orbit-poc Postgres: open-data cache AND private tenant telemetry
#   - Keycloak Postgres: organizations, users, credentials
# Grafana volumes are provisioned from git (dashboards) + hold only session
# state, so they are not backed up here.
#
# Backups are gzipped SQL dumps, age-encrypted to a public key so the VM
# never holds the decryption secret, kept 14 days. Restore: deploy/restore.sh.
set -euo pipefail
DEST="${BACKUP_DIR:-$HOME/backups}"
KEYFILE="${BACKUP_AGE_RECIPIENT:-$HOME/.config/overwatch/backup.pub}"
mkdir -p "$DEST"
STAMP=$(date -u +"%Y%m%dT%H%M%SZ")

dump() {  # $1 container, $2 pguser, $3 db, $4 label
  local out="$DEST/${4}-${STAMP}.sql.gz"
  podman exec "$1" pg_dump -U "$2" "$3" | gzip > "$out"
  if [ -f "$KEYFILE" ] && command -v age >/dev/null 2>&1; then
    age -R "$KEYFILE" -o "$out.age" "$out" && rm "$out"
    out="$out.age"
  fi
  echo "  $(basename "$out") ($(du -h "$out" | cut -f1))"
}

echo "== backup $STAMP"
dump orbit-poc_db_1 orbit orbit          orbit
dump ovw2_kc-db_1   keycloak keycloak     keycloak

# Retention: 14 days.
find "$DEST" -name '*.sql.gz*' -mtime +14 -delete
echo "== kept: $(ls "$DEST"/*.sql.gz* 2>/dev/null | wc -l) files, $(du -sh "$DEST" | cut -f1)"

# Off-site copy (DR-3): a backup that lives only on this VM dies with the VM.
# The operator picks the destination and sets BACKUP_OFFSITE_DEST to any rsync
# target — a remote host (user@host:overwatch-backups/), a mounted EU bucket
# (rclone/s3fs path), or an external disk. We mirror the (encrypted) dumps
# there after each run. No --delete: the off-site keeps its own copies even
# after local 14-day pruning, and manages its own retention. No-op with a loud
# warning when unset, so the gap is never silent.
OFFSITE="${BACKUP_OFFSITE_DEST:-}"
if [ -n "$OFFSITE" ]; then
  if command -v rsync >/dev/null 2>&1; then
    echo "== off-site push -> $OFFSITE"
    # shellcheck disable=SC2086
    if rsync -a ${BACKUP_OFFSITE_RSYNC_OPTS:-} "$DEST"/ "$OFFSITE"; then
      echo "  off-site copy ok"
    else
      echo "!! off-site push FAILED — dumps remain on the VM only (DR-3 gap)"
    fi
  else
    echo "!! BACKUP_OFFSITE_DEST set but rsync missing — off-site copy skipped"
  fi
else
  echo "!! WARNING: no BACKUP_OFFSITE_DEST — backups live only on this VM (DR-3 gap)"
fi

# Warn loudly if encryption is not configured (plaintext dumps on disk).
[ -f "$KEYFILE" ] || echo "!! WARNING: no age recipient at $KEYFILE — dumps are PLAINTEXT"
