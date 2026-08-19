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

# A dump is only a backup once it is verified. When the container is missing
# (as after the stack moved to its own Unix user), `podman exec` fails but the
# gzip in the pipeline still writes a valid EMPTY archive: 20 bytes of header.
# Seventeen consecutive nightly "backups" were exactly that, and nothing said
# so. Now: write to a temp name, check the archive is intact and non-trivial,
# and only then publish it — a bad dump leaves no file and fails the run, so
# systemd marks the unit failed and the alerting sees it.
MIN_BYTES="${BACKUP_MIN_BYTES:-1024}"

dump() {  # $1 container, $2 pguser, $3 db, $4 label
  local out="$DEST/${4}-${STAMP}.sql.gz"
  local tmp="$out.part"
  if ! podman exec "$1" pg_dump -U "$2" "$3" | gzip > "$tmp"; then
    rm -f "$tmp"
    echo "!! FAILED: pg_dump of $3 from $1 (is the container running under THIS user?)" >&2
    return 1
  fi
  if ! gzip -t "$tmp" 2>/dev/null; then
    rm -f "$tmp"; echo "!! FAILED: $4 archive is corrupt" >&2; return 1
  fi
  local size; size=$(wc -c < "$tmp")
  if [ "$size" -lt "$MIN_BYTES" ]; then
    rm -f "$tmp"
    echo "!! FAILED: $4 dump is $size bytes (< $MIN_BYTES) — an empty archive is not a backup" >&2
    return 1
  fi
  mv "$tmp" "$out"
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
