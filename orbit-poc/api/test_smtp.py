"""SMTP wiring for Keycloak + Grafana (#174).

Verification/reset e-mails and ops alerts were silently broken because the
Keycloak realm carried no SMTP server and the Grafana env had none. These
assertions pin the EU (OVH) SMTP configuration so it can't silently regress,
without ever committing the password (that stays in the VM-side .env / the
Keycloak admin console).
"""
import json
import os

HERE = os.path.dirname(__file__)
REALM = os.path.join(HERE, "..", "v2", "keycloak", "realm-overwatch.json")
ENV_EXAMPLE = os.path.join(HERE, "..", ".env.example")


def test_keycloak_realm_has_ovh_smtp():
    realm = json.load(open(REALM, encoding="utf-8"))
    smtp = realm.get("smtpServer")
    assert smtp, "realm has no smtpServer — verification/reset mail is broken (#174)"
    # EU-hosted OVH SMTP (RULES.md: keep services in EU)
    assert smtp["host"] == "ssl0.ovh.net"
    assert smtp["port"] == "587"
    assert smtp["starttls"] == "true" and smtp["auth"] == "true"
    assert smtp["from"] == "alert@confinia.io" and smtp["user"] == "alert@confinia.io"
    assert smtp.get("replyTo") == "contact@confinia.io"
    # the password must NOT be committed — it is set in the admin console / .env
    assert "password" not in smtp, "SMTP password must not be committed (Rule 4)"


def test_email_verification_enabled():
    # the point of wiring SMTP is that signups actually verify their address
    realm = json.load(open(REALM, encoding="utf-8"))
    assert realm.get("verifyEmail") is True
    assert realm.get("resetPasswordAllowed") is True


def test_env_example_documents_ovh_smtp():
    env = open(ENV_EXAMPLE, encoding="utf-8").read()
    assert "GF_SMTP_HOST=ssl0.ovh.net:587" in env
    assert "GF_SMTP_USER=alert@confinia.io" in env
    assert "GF_SMTP_FROM_ADDRESS=alert@confinia.io" in env
    assert "GF_SMTP_ENABLED=true" in env
