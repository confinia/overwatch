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
        assert s["host"] == "ssl0.ovh.net" and s["port"] == "587"
        assert s["from"] == "alert@confinia.io" and s["user"] == "alert@confinia.io"
        assert s["starttls"] == "true" and s["auth"] == "true"
        assert s["replyTo"] == "contact@confinia.io"
        assert r["verifyEmail"] is True


def test_password_is_env_substituted_never_committed():
    for f in REALM_FILES:
        s = json.load(open(f, encoding="utf-8"))["smtpServer"]
        assert s["password"] == "$(env:KC_SMTP_PASSWORD)", \
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
