"""Polar billing client unit tests (POLAR.md sandbox spike) — stub, no creds.

(Named test_billing to avoid the existing test_polar.py, which covers polar
*orbits*, not Polar.sh.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import polar

# Opt into the stub for these unit tests. Set the module flag directly (not via
# env) so it holds regardless of when polar was first imported by another test.
polar.ALLOW_STUB = True


def test_off_by_default_not_configured():
    assert polar.POLAR_ENV == "off"
    assert not polar.configured()                # no real Polar creds
    assert polar.stub_allowed()                  # but stub is opted-in here


def test_production_is_inert_without_real_secret(monkeypatch):
    # a public prod (POLAR_ENV set, no real webhook secret) must reject every
    # webhook — the public stub secret is never honored there
    monkeypatch.setattr(polar, "POLAR_ENV", "production")
    monkeypatch.setattr(polar, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(polar, "ALLOW_STUB", True)
    assert polar._secret() == ""
    assert not polar.verify_webhook(b'{"x":1}', {"webhook-signature": "sha256=deadbeef"})


def test_checkout_returns_in_app_stub_url():
    ck = polar.create_checkout("org-123", "a@b.c", "https://x/success")
    assert ck["stub"] is True
    assert "org-123" in ck["url"] and ck["url"].startswith("/v1/billing/")


def test_webhook_sign_verify_roundtrip():
    raw = b'{"type":"subscription.active"}'
    sig = polar.sign(raw)
    assert polar.verify_webhook(raw, {"webhook-signature": sig})
    assert not polar.verify_webhook(raw, {"webhook-signature": "sha256=deadbeef"})
    assert not polar.verify_webhook(b'{"tampered":1}', {"webhook-signature": sig})


def test_parse_event_resolves_org_and_sub():
    ev = polar.parse_event({
        "type": "subscription.active",
        "data": {"id": "sub_1", "status": "active",
                 "customer": {"external_id": "org-123"}}})
    assert ev["type"] == "subscription.active"
    assert ev["org_id"] == "org-123"
    assert ev["subscription_id"] == "sub_1"
    assert ev["status"] == "active"


def test_customer_session_returns_stub_url():
    # portal link falls back to an in-app URL when Polar isn't configured, so the
    # "invoices / manage subscription" flow stays testable with no creds
    ps = polar.create_customer_session("org-123", "https://x/back")
    assert ps["stub"] is True
    assert "org-123" in ps["url"] and ps["url"].startswith("/v1/billing/")


def test_api_image_ships_polar():
    df = os.path.join(os.path.dirname(__file__), "Dockerfile")
    assert "COPY polar.py" in open(df, encoding="utf-8").read()
