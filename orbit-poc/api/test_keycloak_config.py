"""Declarative Keycloak realm config via keycloak-config-cli (#181).

The realm files must (1) carry the OVH SMTP block + verifyEmail, (2) keep the
password as an env-substituted placeholder (never a committed secret), and
(3) stay PARTIAL — no clients/roles/scopes arrays — so the CLI reconciles only
these settings and can never delete anything else on the live realms.
"""
import glob
import json
import os

HERE = os.path.dirname(__file__)
CONFIG_DIR = os.path.join(HERE, "..", "v2", "keycloak-config")
COMPOSE = open(os.path.join(HERE, "..", "v2", "docker-compose.yml"),
               encoding="utf-8").read()
REALM_FILES = sorted(glob.glob(os.path.join(CONFIG_DIR, "*.json")))


def test_all_three_realms_present():
    names = {json.load(open(f, encoding="utf-8"))["realm"] for f in REALM_FILES}
    assert names == {"overwatch", "overwatch-staging", "overwatch-sandbox"}


def test_realms_declare_ovh_smtp_and_verify():
    for f in REALM_FILES:
        r = json.load(open(f, encoding="utf-8"))
        s = r["smtpServer"]
        # host/port/from/user come from the generic SMTP_* env, not hardcoded (#195)
        assert s["host"] == "$(env:SMTP_HOST)" and s["port"] == "$(env:SMTP_PORT)"
        assert s["from"] == "$(env:SMTP_FROM)" and s["user"] == "$(env:SMTP_USER)"
        assert s["starttls"] == "true" and s["auth"] == "true"
        assert s["replyTo"] == "$(env:ALERT_RCPT)"
        assert r["verifyEmail"] is True


def test_password_is_env_substituted_never_committed():
    for f in REALM_FILES:
        s = json.load(open(f, encoding="utf-8"))["smtpServer"]
        assert s["password"] == "$(env:SMTP_PASSWORD)", \
            f"{f}: SMTP password must be an env placeholder, not a secret"


def test_files_are_partial_so_reconcile_is_safe():
    # No collection arrays => keycloak-config-cli manages only the realm settings
    # above and never deletes clients/roles/scopes/flows on the live realm.
    for f in REALM_FILES:
        r = json.load(open(f, encoding="utf-8"))
        for k in ("clients", "roles", "clientScopes", "groups",
                  "authenticationFlows", "identityProviders", "users"):
            assert k not in r, f"{f}: must not declare '{k}' (keep it partial)"


def test_compose_runs_config_cli_in_no_delete_mode():
    assert "keycloak-config-cli" in COMPOSE
    assert 'IMPORT_VARSUBSTITUTION_ENABLED: "true"' in COMPOSE
    # the destructive default (full) must never be used for the collections
    assert 'IMPORT_MANAGED_CLIENT: "no-delete"' in COMPOSE
    assert 'IMPORT_MANAGED_AUTHENTICATION_FLOW: "no-delete"' in COMPOSE


def test_v2_env_example_lists_required_keys():   # #191
    """A dropped v2 secret breaks all login on the next recreate. The committed
    example pins the required set so an edit can't silently lose one."""
    ex = open(os.path.join(HERE, "..", "v2", ".env.example"), encoding="utf-8").read()
    for k in ("POSTGRES_PASSWORD", "KC_DB_PASSWORD", "KC_BOOTSTRAP_ADMIN_USERNAME",
              "KC_BOOTSTRAP_ADMIN_PASSWORD", "OVERWATCH_CLIENT_SECRET",
              "KEYCLOAK_USER", "KEYCLOAK_PASSWORD",
              "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        assert k + "=" in ex, f"{k} missing from v2/.env.example"
