#!/usr/bin/env bash
# The ONLY sanctioned way to run a batch job that touches SatNOGS.
#
# It routes the job through the SatNOGS egress gateway (#449) — one shared rate
# limiter + cache for the whole deployment — and blackholes db.satnogs.org, so a
# job that still hardcodes the host fails fast instead of overspending the
# per-user budget that got us blocked (twice). The scripts read SATNOGS_BASE;
# this points it at the gateway on the compose network.
#
#   ./run.sh probe3.py [args...]
#
# Run on the Debian VM (podman), with the overwatch stack up (the gateway lives
# on its compose network). Token and DSN are read from the stack's .env.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${OW_ENV:-$HERE/../orbit-poc/.env}"
NET="${OW_NET:-orbit-poc_default}"
IMAGE="${OW_BATCH_IMAGE:-docker.io/library/python:3.12-slim}"
PKGS="${OW_BATCH_PKGS:-requests psycopg2-binary satnogs-decoders}"
# Resource ceiling so a batch job can never starve the shared VM.
CPUS="${OW_BATCH_CPUS:-1.0}"
MEM="${OW_BATCH_MEM:-1g}"
# Hard timeout: a one-shot job that cannot finish must die, not sit for days
# holding the machine (platform rule 4). Override for a legitimately long run.
TIMEOUT="${OW_BATCH_TIMEOUT:-1800}"

[ $# -ge 1 ] || { echo "usage: $(basename "$0") <script.py> [args...]" >&2; exit 2; }
[ -f "$ENV_FILE" ] || { echo "no env file at $ENV_FILE (set OW_ENV)" >&2; exit 2; }

# sed -n 1p (not head -1): under `set -o pipefail`, head closing the pipe early
# SIGPIPEs grep and fails the whole pipeline even on success (#288).
val() { grep -oE "^$1=.*" "$ENV_FILE" | sed -n '1p' | cut -d= -f2- ; }

exec timeout --signal=TERM --kill-after=30 "$TIMEOUT" podman run --rm \
  --cpus "$CPUS" --memory "$MEM" \
  --network "$NET" \
  --add-host db.satnogs.org:127.0.0.1 \
  --add-host network.satnogs.org:127.0.0.1 \
  -e SATNOGS_BASE="http://satnogs-gateway:8088/api" \
  -e SATNOGS_TOKEN="$(val SATNOGS_TOKEN)" \
  -e TOKEN="$(val SATNOGS_TOKEN)" \
  -e DB_DSN="$(val DB_DSN)" \
  -v "$HERE":/work:rw -w /work \
  "$IMAGE" sh -c 'pip install -q '"$PKGS"' && python -u "$@"' _ "$@"
