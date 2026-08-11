#!/usr/bin/env bash
# On-demand e2e (#193): prove that a new user registration fires the ops
# "new-registration" alert AND Grafana sends the notification e-mail, then clean
# up. Runs ON the VM (needs podman access to the env's db + grafana). It asserts
# via Grafana's own logs (the "Sending alerts to local notifier" line), so no
# per-env Grafana port/HTTP client is required — works the same for all three.
#
# Usage: bash e2e_registration_alert.sh <production|staging|sandbox>
#
# It seeds a registration ROW directly (the signal the alert watches), rather
# than walking the full Keycloak signup — that path is covered by e2e_sandbox.py.
set -euo pipefail

ENV="${1:-production}"
case "$ENV" in
  production) DB=orbit-poc_db_1;   GRAF=orbit-poc_grafana_1 ;;
  staging)    DB=ovw-staging_db_1; GRAF=ovw-staging_grafana_1 ;;
  sandbox)    DB=ovw-sandbox_db_1; GRAF=ovw-sandbox_grafana_1 ;;
  *) echo "unknown env: $ENV (want production|staging|sandbox)"; exit 2 ;;
esac

SUB="e2e0e2e0-0000-0000-0000-0000000000e2"
cleanup() {
  podman exec -i "$DB" psql -U orbit -d orbit -q \
    -c "DELETE FROM registered_user WHERE sub = '$SUB';" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[$ENV] seeding a registration row (sub=$SUB)..."
podman exec -i "$DB" psql -U orbit -d orbit -q <<SQL
INSERT INTO registered_user (sub, email, name, first_seen, last_login, country)
VALUES ('$SUB', 'e2e-ci@example.com', 'CI E2E', now(), now(), 'FR')
ON CONFLICT (sub) DO UPDATE SET first_seen = now(), country = 'FR';
SQL

echo "[$ENV] waiting up to 120s for the alert to fire + Grafana to notify..."
for i in $(seq 1 12); do
  if podman logs --since 130s "$GRAF" 2>&1 \
       | grep -q 'rule_uid=new-registration.*Sending alerts to local notifier'; then
    echo "[$ENV] PASS — new-registration alert fired and Grafana sent the ops mail."
    exit 0
  fi
  sleep 10
done

echo "[$ENV] FAIL — no new-registration notification in the window."
echo "[$ENV] recent grafana alert log:"
podman logs --since 130s "$GRAF" 2>&1 | grep -iE 'ngalert|new-registration' | tail -5 || true
exit 1
