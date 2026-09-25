#!/usr/bin/env bash
# Compose e2e/side/.env on the VM for a CI run of the payment walk (#267).
#
# NOTHING has to travel from GitHub any more (#290): the Keycloak service admin
# and the Creem test key already live on the VM in the sandbox stack's own .env,
# and the gate account is created per run with a generated password, so no copy
# of any of it needs to exist in GitHub's secret store.
#
# Expects in the environment: TARGET_ENV, KEEP_USER (both optional).
set -euo pipefail

SRC=~/projects/overwatch/orbit-poc/sandbox/.env
DEST=~/e2e-side/.env

[ -f "$SRC" ] || { echo "sandbox stack .env not found at $SRC" >&2; exit 1; }

# sed -n 1p rather than head -1: head would SIGPIPE grep, and pipefail
# turns that into a failure of a lookup that actually succeeded.
val() { grep -E "^$1=" "$SRC" | sed -n 1p | cut -d= -f2- | tr -d '"'; }

umask 077
cat > "$DEST" <<EOF
TARGET_ENV=${TARGET_ENV:-sandbox}
SIGNUP_PASS=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
KC_ADMIN_BASE=http://127.0.0.1:12070
KC_REALM=overwatch-sandbox
KC_ADMIN_USERNAME=$(val KC_ADMIN_USERNAME)
KC_ADMIN_PASSWORD=$(val KC_ADMIN_PASSWORD)
CREEM_API_BASE=$(val CREEM_API_BASE)
CREEM_API_KEY=$(val CREEM_API_KEY)
KEEP_USER=${KEEP_USER:-0}
EOF

# The password is hex on purpose. A generated password containing '+' decodes as
# a space when a form posts it, and one containing '#' comments out the rest of
# the line when the file is sourced — both produce a login failure that looks
# like a broken test rather than a broken password.
# CREEM_API_KEY becomes mandatory once the founder registers the Creem test
# account (rule 27); until then the walk runs and fails only at the checkout.
grep -qE "^CREEM_API_KEY=.+" "$DEST" || echo "warning: CREEM_API_KEY is empty — the payment leg will fail" >&2
for v in SIGNUP_PASS KC_ADMIN_PASSWORD; do
  grep -qE "^$v=.+" "$DEST" || { echo "$v came out empty" >&2; exit 1; }
done
echo "configuration written: $(wc -l < "$DEST") lines, no value echoed"
