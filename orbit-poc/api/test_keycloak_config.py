"""Declarative Keycloak realm config via keycloak-config-cli (#181).

The APPLICATION realm files must (1) carry the OVH SMTP block + verifyEmail,
(2) keep the password as an env-substituted placeholder (never a committed
secret), and (3) stay PARTIAL — no clients/roles/scopes arrays — so the CLI
reconciles only these settings and can never delete anything else on the live
realms.

overwatch-gate is the exception, and deliberately the opposite (#290): nothing
else configures that realm and no self-serve user lives in it, so its file owns
its clients, its role and its passkey flow outright. What it must NOT carry is
users or a literal secret. See also test_gate.py for the gate as a whole.
"""
import glob
import json
import os

HERE = os.path.dirname(__file__)
CONFIG_DIR = os.path.join(HERE, "..", "v2", "keycloak-config")
COMPOSE = open(os.path.join(HERE, "..", "v2", "docker-compose.yml"),
               encoding="utf-8").read()
REALM_FILES = sorted(glob.glob(os.path.join(CONFIG_DIR, "*.json")))
GATE_FILE = os.path.join(CONFIG_DIR, "overwatch-gate.json")
# the realms the product's own users live in
APP_FILES = [f for f in REALM_FILES if os.path.abspath(f) != os.path.abspath(GATE_FILE)]


def test_every_realm_has_a_file():
    names = {json.load(open(f, encoding="utf-8"))["realm"] for f in REALM_FILES}
    assert names == {"overwatch", "overwatch-staging", "overwatch-sandbox",
                     "overwatch-gate"}


def test_realms_declare_ovh_smtp_and_verify():
    for f in APP_FILES:
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


def test_app_realm_files_are_partial_so_reconcile_is_safe():
    # No collection arrays => keycloak-config-cli manages only the realm settings
    # above and never deletes clients/roles/scopes/flows on the live realm.
    for f in APP_FILES:
        r = json.load(open(f, encoding="utf-8"))
        for k in ("clients", "roles", "clientScopes", "groups",
                  "authenticationFlows", "identityProviders", "users"):
            assert k not in r, f"{f}: must not declare '{k}' (keep it partial)"


def test_no_realm_file_declares_users():
    """People and bots are created by hand or per run, never by a commit —
    a user in a realm file is a standing account with a committed password."""
    for f in REALM_FILES:
        r = json.load(open(f, encoding="utf-8"))
        assert "users" not in r, f"{f}: must not declare users"


def test_gate_realm_smtp_matches_the_others_but_needs_no_verification():
    """It sends password-reset mail like any realm; it does not verify
    addresses, because every account in it is created by an administrator."""
    r = json.load(open(GATE_FILE, encoding="utf-8"))
    s = r["smtpServer"]
    assert s["host"] == "$(env:SMTP_HOST)" and s["port"] == "$(env:SMTP_PORT)"
    assert s["password"] == "$(env:SMTP_PASSWORD)"
    assert r["verifyEmail"] is False
    assert r["registrationAllowed"] is False, "the gate is not self-serve"


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
              "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM",
              "GATE_CLIENT_SECRET_SANDBOX", "GATE_CLIENT_SECRET_STAGING"):
        assert k + "=" in ex, f"{k} missing from v2/.env.example"
