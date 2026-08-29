"""Guards issue #265: password recovery must be proven, not assumed.

The capability went live with the realm SMTP work and nothing in CI would
have noticed it breaking: rotated SMTP credentials, a Keycloak recreate
dropping the realm's smtpServer, the sender failing SPF. The e2e walker now
exercises the flow against the sandbox realm; these file-level guards pin
the configuration that walk depends on.
"""
import json
import os

HERE = os.path.dirname(__file__)
KC_CONFIG = os.path.join(HERE, "..", "v2", "keycloak-config")
REALMS = ("overwatch", "overwatch-sandbox", "overwatch-staging")


def _realm(name):
    with open(os.path.join(KC_CONFIG, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def test_every_realm_owns_password_recovery():
    """resetPasswordAllowed lived only in the live realm before — so a
    recreate from config would have silently disabled recovery. The config
    files own it now."""
    for name in REALMS:
        r = _realm(name)
        assert r.get("resetPasswordAllowed") is True, name
        assert (r.get("smtpServer") or {}).get("host"), \
            f"{name}: recovery mail cannot send without an smtpServer"


def test_every_realm_records_events():
    """The e2e's witness: SEND_RESET_PASSWORD must be queryable, so events
    have to be on (with an expiry — unbounded event tables grow forever)."""
    for name in REALMS:
        r = _realm(name)
        assert r.get("eventsEnabled") is True, name
        assert isinstance(r.get("eventsExpiration"), int) and \
            r["eventsExpiration"] > 0, name


def test_the_walker_exercises_the_reset_flow():
    src = open(os.path.join(HERE, "..", "..", "deploy", "e2e_sandbox.py"),
               encoding="utf-8").read()
    assert "execute-actions-email" in src, "no reset trigger in the walk"
    assert "SEND_RESET_PASSWORD" in src, "the send is not asserted"
    assert "resetPasswordAllowed" in src, "the realm guard is missing"


def test_the_stage_applies_realm_config():
    """A merged realm-file change must not sit unapplied waiting for a
    by-hand one-shot — that is exactly how eventsEnabled would have."""
    wf = open(os.path.join(HERE, "..", "..", ".github", "workflows",
                           "deploy.yml"), encoding="utf-8").read()
    assert "keycloak-config-cli" in wf
    assert "-p ovw2" in wf, "a bare project name forks a parallel stack"
