#!/usr/bin/env bash
# Compose e2e/side/.env on the VM for a CI run of the payment walk (#267).
#
# Only the basic-auth gate password has to travel from GitHub. Everything else —
# the Keycloak service admin, the Polar sandbox token — already lives on the VM
# in the sandbox stack's own .env, so no second copy of those secrets needs to
# exist in GitHub's secret store.
#
# Expects in the environment: TARGET_ENV, KEEP_USER, GATE_USER, BASIC_PASS.
set -euo pipefail

SRC=~/projects/overwatch/orbit-poc/sandbox/.env
DEST=~/e2e-side/.env

[ -f "$SRC" ] || { echo "sandbox stack .env not found at $SRC" >&2; exit 1; }
: "${BASIC_PASS:?the gate password was not passed through}"

val() { grep -E "^$1=" "$SRC" | head -1 | cut -d= -f2- | tr -d '"'; }

umask 077
cat > "$DEST" <<EOF
TARGET_ENV=${TARGET_ENV:-sandbox}
GATE_USER=${GATE_USER:-clement}
GATE_PASS=$BASIC_PASS
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
for v in GATE_PASS SIGNUP_PASS KC_ADMIN_PASSWORD; do
  grep -qE "^$v=.+" "$DEST" || { echo "$v came out empty" >&2; exit 1; }
done
echo "configuration written: $(wc -l < "$DEST") lines, no value echoed"
