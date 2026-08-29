#!/usr/bin/env bash
# One e-mail to the ops address when a deploy run waiting on the production
# approval was cancelled by a newer push (gate-watch.yml calls this over ssh).
#
# The reviewer saw "approval requested" for a run that no longer exists; if
# nothing says so, the request just vanishes — and when the newer run's
# sandbox leg then fails, merged work sits unpromoted with every check green
# (#377, previously the #320/#324 class).
set -euo pipefail
OLD_SHA=$1; OLD_URL=$2; NEW_SHA=$3; NEW_URL=$4

# Pick single keys out of .env rather than `source` it: the file holds
# unrelated values whose characters a shell would interpret.
ENV_FILE="$HOME/projects/overwatch/orbit-poc/.env"
val() { grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2-; }
HOST=$(val GF_SMTP_HOST); USER=$(val GF_SMTP_USER); PASS=$(val GF_SMTP_PASSWORD)
FROM=$(val GF_SMTP_FROM_ADDRESS); TO=$(val OPS_ALERT_EMAIL)
for v in HOST USER PASS FROM TO; do
  [ -n "${!v}" ] || { echo "missing SMTP setting for $v in .env" >&2; exit 1; }
done

curl -s --ssl-reqd "smtp://$HOST" -u "$USER:$PASS" \
  --mail-from "$FROM" --mail-rcpt "$TO" -T - <<MAIL
From: Overwatch deploy <$FROM>
To: $TO
Subject: promote gate for ${OLD_SHA:0:7} cancelled - superseded by ${NEW_SHA:0:7}

The deploy run waiting on the production approval was cancelled by a newer
push to main. That approval request is void; the newer run carries the
current candidate and raises its own gate when it reaches promote.

cancelled: $OLD_URL  (${OLD_SHA:0:7})
newer:     $NEW_URL  (${NEW_SHA:0:7})
MAIL
echo "gate-superseded mail sent to $TO"
