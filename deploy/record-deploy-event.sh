#!/usr/bin/env bash
# Record one deploy-pipeline event (stage | promote) in the orbit database, so
# the ops Grafana can show what production runs versus what is merged (#382).
# Called from deploy.yml over the ssh session it already holds. The schema
# truth lives in main.py's startup DDL (where ops_ro gets its grant); the
# CREATE here repeats it for one bootstrap window only: the very first stage
# row lands on a database no new api has booted against yet.
set -euo pipefail
PHASE=$1; SHA=$2; RUN_ID=$3; RUN_URL=$4
case "$PHASE" in stage|promote) ;; *) echo "phase must be stage|promote" >&2; exit 1;; esac
podman exec -i orbit-poc_db_1 psql -U orbit -v ON_ERROR_STOP=1 \
  -v phase="$PHASE" -v sha="$SHA" -v run_id="$RUN_ID" -v run_url="$RUN_URL" <<'SQL'
CREATE TABLE IF NOT EXISTS deploy_event (
    ts      timestamptz NOT NULL DEFAULT now(),
    phase   text        NOT NULL CHECK (phase IN ('stage', 'promote')),
    sha     text        NOT NULL,
    run_id  bigint      NOT NULL,
    run_url text        NOT NULL
);
INSERT INTO deploy_event (phase, sha, run_id, run_url)
VALUES (:'phase', :'sha', :'run_id', :'run_url');
SQL
echo "deploy_event: $PHASE ${SHA:0:7} recorded"
