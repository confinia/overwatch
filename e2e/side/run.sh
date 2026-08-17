#!/usr/bin/env bash
# Run the Selenium IDE walk of registration + Polar sandbox payment (#267).
#
#   cp .env.example .env && $EDITOR .env
#   ./run.sh
#
# The same overwatch-signup-payment.side is what you open in the Selenium IDE
# browser extension to record or replay a step by hand; this script only fills
# its `00 config` values from .env and drives it headless.
#
# Everything runs inside a container (rule 1): chromium, chromedriver and
# selenium-side-runner are never installed on the host.
#
# The walk is stopped in the middle on purpose. Between "10 register" and
# "20 sign in" the freshly created user must be marked e-mail-verified through
# the Keycloak admin API, because the realms have verifyEmail=true. That is the
# one step no browser can do without a mailbox.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="overwatch-side-runner"
cd "$HERE"

die() { printf '\n\033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ -f .env ] || die ".env is missing — copy .env.example to .env and fill it in"
set -a; . ./.env; set +a

# Never production. Real cards, real money; and production runs POLAR_ENV=off
# so a checkout there cannot work anyway.
case "${TARGET_ENV:-}" in
  sandbox|staging) ;;
  *) die "TARGET_ENV must be 'sandbox' or 'staging' (got '${TARGET_ENV:-}')" ;;
esac
for v in GATE_PASS SIGNUP_PASS KC_ADMIN_PASSWORD POLAR_ORG_TOKEN; do
  [ -n "${!v:-}" ] || die "$v is empty in .env"
done

BASE="https://${TARGET_ENV}.overwatch.confinia.io"
RUN_ID="$(od -An -N4 -tu4 </dev/urandom | tr -d ' ')"
EMAIL="e2e-bot+side${RUN_ID}@confinia.io"
ORG="E2E Side ${RUN_ID}"

# A per-run address, never a fixed one: a live walk leaves a soft-deleted org
# behind, and a repeated e-mail re-links the new user to that tombstone (410
# "organization has been deleted") — the walk then dies before payment.
say "target ${BASE} — disposable user ${EMAIL}"

command -v podman >/dev/null || die "podman is required"
podman image exists "$IMAGE" || { say "building $IMAGE"; podman build -q -t "$IMAGE" . ; }

# --- render: .env values into the `00 config` store commands -----------------
mkdir -p rendered results
python3 - "$EMAIL" "$ORG" "$BASE" <<'PY'
import json, os, sys, urllib.parse
email, org, base = sys.argv[1], sys.argv[2], sys.argv[3]
side = json.load(open("overwatch-signup-payment.side"))
gate = urllib.parse.quote(os.environ["GATE_USER"], safe="")
gpw = urllib.parse.quote(os.environ["GATE_PASS"], safe="")
values = {"BASE": base, "EMAIL": email,
          "PASS": os.environ["SIGNUP_PASS"], "ORG": org,
          # A gate password containing ':' '@' or '/' would otherwise cut the
          # URL in half, and the walk would 401 with no clue why.
          "GATED_BASE": base.replace("https://", f"https://{gate}:{gpw}@")}
for t in side["tests"]:
    for cmd in t["commands"]:
        if cmd["command"] == "store" and cmd["value"] in values:
            cmd["target"] = values[cmd["value"]]
json.dump(side, open("rendered/run.side", "w"), indent=2, ensure_ascii=False)
PY

# --- run ---------------------------------------------------------------------
# --network=host: Keycloak's admin API is bound to VM loopback, and the app is
# reached over the public URL either way.
side_runner() {
  podman run --rm --network=host --shm-size=1g \
    -v "$HERE:/work:z" -w /work "$IMAGE" \
    selenium-side-runner \
      -c "browserName=chrome goog:chromeOptions.args=[headless=new,no-sandbox,disable-dev-shm-usage] goog:chromeOptions.binary=/usr/bin/chromium" \
      --timeout 60000 \
      --jest-timeout 900000 \
      --filter "$1" \
      rendered/run.side
}

kc() {
  podman run --rm --network=host \
    -e KC_ADMIN_BASE -e KC_REALM -e KC_ADMIN_USERNAME -e KC_ADMIN_PASSWORD \
    -v "$HERE:/work:z" -w /work "$IMAGE" python3 kc_admin.py "$1" "$EMAIL"
}

teardown() {
  if [ "${KEEP_USER:-0}" = "1" ]; then
    say "KEEP_USER=1 — leaving ${EMAIL} in place"
  else
    say "teardown"; kc delete || true
  fi
}
trap teardown EXIT

say "1/4  register through the signup form"
side_runner '^register$' || die "registration walk failed"

say "2/4  mark the e-mail verified (realm has verifyEmail=true)"
kc verify

say "3/4  sign in, create the organization, pay on Polar sandbox"
side_runner '^pay$' || die "payment walk failed"

say "4/4  what does Polar sandbox actually say?"
podman run --rm --network=host \
  -e POLAR_API_BASE -e POLAR_ORG_TOKEN \
  -v "$HERE:/work:z" -w /work "$IMAGE" python3 polar_report.py "$EMAIL"

printf '\n\033[32mPASS\033[0m  %s: registration and payment walked end to end.\n' "$TARGET_ENV"
