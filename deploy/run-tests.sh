#!/usr/bin/env bash
# Automated test run — executes ON the VM. No GitHub Actions needed: this is
# the CI gate (called by `make test`, and by `make stage` before a candidate
# is built) plus a daily systemd timer. Runs both suites in throwaway
# containers and writes TEST_RESULTS.md at the repo root.
#
#   subscription: real Postgres, Keycloak identity monkeypatched at _claims
#   polar:        live Polar API (needs POLAR_ACCESS_TOKEN in orbit-poc/.env)
set -uo pipefail
cd "$(dirname "$0")/.."                       # ~/projects/overwatch on the VM
NET=citest-$$
PG=citest-pg-$$
POLAR_TOKEN=$(grep -h '^POLAR_ACCESS_TOKEN=' orbit-poc/.env 2>/dev/null | cut -d= -f2-)
# Every container and network this script starts carries this label, and the
# sweep below selects by it and nothing else — never by name across the host.
# Rootless podman only shows this user's containers anyway, but the label is
# what makes "ours" explicit (#542: a neighbour's CI container was mistaken
# for one of ours from a host-wide ps).
LABEL=io.confinia.overwatch.test

cleanup(){ podman rm -f "$PG" >/dev/null 2>&1; podman network rm "$NET" >/dev/null 2>&1; }
trap cleanup EXIT

# The trap only fires on a normal exit. A cancelled workflow or a SIGKILLed
# client leaves the database container and its network behind, and `--rm`
# does nothing for a container whose client is gone — a neighbour's test
# container ran 26 hours that way. So: sweep strays by label before starting,
# older than two hours so a concurrent run (daily timer vs a stage) is never
# touched, and record what was swept so it shows on the Deploy pipeline board
# (rule 34) instead of in a ps nobody reads.
sweep_strays(){
  local now swept=0 oldest=0 c created age n
  now=$(date +%s)
  for c in $(podman ps -a --filter "label=$LABEL=1" --format '{{.Names}}'); do
    # .Created.Unix, not `date -d` on .Created: GNU date rejects podman's
    # "+0000 UTC" suffix, and the first version of this loop skipped every
    # container silently on exactly that — a planted stray survived the sweep
    # while the row said 0. An age it cannot read is now said out loud.
    created=$(podman inspect "$c" --format '{{.Created.Unix}}' 2>/dev/null)
    case "$created" in ''|*[!0-9]*) echo "!! cannot read age of $c — not swept"; continue;; esac
    age=$(( now - created ))
    [ "$age" -gt 7200 ] || continue
    podman rm -f "$c" >/dev/null 2>&1 && swept=$((swept+1)) && echo "swept stray $c (${age}s old)"
    [ "$age" -gt "$oldest" ] && oldest=$age
  done
  for n in $(podman network ls --filter "label=$LABEL=1" --format '{{.Name}}'); do
    podman network rm "$n" >/dev/null 2>&1 || true     # refuses while in use — fine
  done
  record_sweep "$swept" "$oldest"
}

# Schema truth is main.py's startup DDL (where ops_ro gets its grant); the
# CREATE here covers the bootstrap window only, as record-deploy-event.sh does.
record_sweep(){  # $1 swept, $2 oldest age in seconds
  podman container exists orbit-poc_db_1 2>/dev/null || return 0   # local dev: no ops database
  podman exec -i -e PGOPTIONS="-c client_min_messages=warning" orbit-poc_db_1 \
    psql -U orbit -q -v ON_ERROR_STOP=1 \
    -v swept="$1" -v oldest="$2" -v run="$NET" <<'SQL' || echo "!! test_sweep row not recorded"
CREATE TABLE IF NOT EXISTS test_sweep (
    ts       timestamptz NOT NULL DEFAULT now(),
    swept    int         NOT NULL,
    oldest_s int         NOT NULL,
    run      text        NOT NULL
);
INSERT INTO test_sweep (swept, oldest_s, run) VALUES (:'swept', :'oldest', :'run');
SQL
}

sweep_strays
podman network create --label "$LABEL=1" "$NET" >/dev/null 2>&1 || true
# The data directory lives in RAM. This database is created for one run and
# destroyed by the trap above, so there is nothing to persist and nothing to
# lose — and the VM's two 7200rpm disks are IOPS-saturated (25% io pressure)
# while moving only 1.5 MB/s, so every seek this does not take is one another
# tenant can. Removes the disk from the equation rather than tuning it away;
# measured: ready in 2s instead of ~6, and 46 MB of the 512 used by a full run.
# --timeout bounds the CONTAINER's life, whoever happens to its client: podman
# kills it after that many seconds. Far above any real run (~3 min), far below
# "forever".
podman run -d --rm --name "$PG" --network "$NET" --label "$LABEL=1" --timeout 3600 \
  --tmpfs /var/lib/postgresql/data:size=512m \
  -e POSTGRES_USER=orbit -e POSTGRES_PASSWORD=orbit -e POSTGRES_DB=orbit \
  docker.io/library/postgres:16 >/dev/null
for _ in $(seq 1 30); do
  podman exec "$PG" pg_isready -U orbit >/dev/null 2>&1 && break; sleep 1; done

# The WHOLE repo goes in, not a hand-picked list of directories. The old cp
# list omitted staging/, .github/ and README.md while the test list named
# suites that read them, so those suites raised collection errors — and a
# collection error aborts the entire run. The gate was executing one skipped
# test and reporting green (#286).
OUT=$(podman run --rm --network "$NET" --label "$LABEL=1" --timeout 3000 \
  -e "DB_DSN=dbname=orbit user=orbit password=orbit host=$PG port=5432" \
  -e "POLAR_ACCESS_TOKEN=$POLAR_TOKEN" \
  -e "ORG_DB_SECRET=ci-org-db-secret" \
  -e "OVERWATCH_TEST_DB=1" \
  -v "$PWD:/repo:ro" \
  docker.io/library/python:3.12-slim bash -c '
set -e
mkdir -p /tmp/work
tar -C /repo --exclude=.git --exclude=node_modules -cf - . | tar -C /tmp/work -xf -
cd /tmp/work/orbit-poc
pip install -q -r api/requirements.txt pytest httpx requests sgp4 numpy kaitaistruct >/dev/null 2>&1
apt-get -qq update >/dev/null 2>&1 && apt-get -qq install -y postgresql-client >/dev/null 2>&1
PGPASSWORD=orbit psql -h "$(echo $DB_DSN | sed -E "s/.*host=([^ ]+).*/\1/")" -U orbit -d orbit -f db/init.sql >/dev/null 2>&1
cd api
# Auto-discovery, so a new suite is covered the moment it is written. Every
# exclusion is explicit and carries its reason — the only way to leave a test
# out is to say so here, in the open.
#   test_polar.py  live Polar API, needs POLAR_ACCESS_TOKEN. Polar is the
#                  fallback provider since Creem became the MoR (#269/#270),
#                  so it is run separately and never gates a deploy.
python -m pytest -q -p no:cacheprovider --ignore=test_polar.py . ../gateway 2>&1 | tail -30
echo "MAIN_EXIT=${PIPESTATUS[0]}"
python -m pytest test_polar.py -q -p no:cacheprovider 2>&1 | tail -3
' 2>&1)

# `head -2` closes the pipe early, upstream takes SIGPIPE and — under
# `set -o pipefail` — the whole pipeline reports failure even though it
# matched. `sed -n` reads to EOF, so nothing upstream is ever killed.
SUB=$(echo "$OUT" | grep -oE '[0-9]+ passed|[0-9]+ failed' | sed -n '1,2p' | paste -sd' ' -)
POL=$(echo "$OUT" | grep -oE '[0-9]+ passed|[0-9]+ failed|[0-9]+ skipped' | tail -2 | paste -sd' ' -)
STAMP=$(date -u +"%Y-%m-%d %H:%M UTC")
# pytest's EXIT CODE, not the word "failed" in its summary. A run that dies
# during collection ends "N errors in ...", which contains no "failed" — so
# the old grep scored a suite that never ran as a pass (#286).
MAIN_EXIT=$(echo "$OUT" | sed -n 's/^MAIN_EXIT=//p' | tail -1)
FAILED=${MAIN_EXIT:-1}

cat > TEST_RESULTS.md <<EOF
# Latest test results

Auto-generated by \`deploy/run-tests.sh\` (pre-deploy gate + daily VM timer).
Do not edit by hand.

| Suite | Result | Covers |
|---|---|---|
| subscription | $(echo "$OUT" | grep -oE '[0-9]+ passed.*' | sed -n 1p) | signup, org isolation, service tokens, quotas |
| polar | $(echo "$OUT" | grep -oE '[0-9]+ passed.*' | tail -1) | pro-account product, trial, discount codes |

**Run:** $STAMP · host: VM (podman) · $([ "$FAILED" -eq 0 ] && echo "ALL GREEN ✅" || echo "FAILURES ❌ (pytest exit $FAILED)")

<details><summary>raw</summary>

\`\`\`
$OUT
\`\`\`
</details>
EOF

echo "$OUT"
echo "== wrote TEST_RESULTS.md ($STAMP)"
[ "$FAILED" -eq 0 ]
