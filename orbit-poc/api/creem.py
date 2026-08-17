"""Creem.io client — the EU merchant of record (#269), sandbox-first, stub-capable.

Creem (Armitage Labs OÜ, Estonia) invoices the customer in its own name, which
keeps the whole payment path — invoice issuer, billing data, jurisdiction —
inside the EU. Same seam as polar.py: six functions main.py consumes through
the billing facade, so the providers are drop-in replacements for each other.

Environment is derived from the API key itself, the way Creem routes it:
`creem_test_…` keys hit https://test-api.creem.io (test cards, no money),
other `creem_…` keys hit production. No key -> "off", which runs the same
STUB mode as polar.py: checkout returns an in-app URL and webhooks verify
against a public test secret — so checkout -> webhook -> entitlement is
provable with no Creem account at all. The stub is opt-in via CREEM_ALLOW_STUB
and never honored when a production key is present.

Webhooks: Creem signs the raw body with HMAC-SHA256 (hex) of the dashboard's
webhook secret, header `creem-signature`. Events arrive in an envelope
{id, eventType, created_at, object}; parse_event normalizes them to the exact
shapes _apply_billing_event already acts on.
"""
import hashlib
import hmac
import os

API_KEY = os.environ.get("CREEM_API_KEY", "").strip()
PRODUCT_PRO = os.environ.get("CREEM_PRODUCT_ID_PRO", "").strip()
WEBHOOK_SECRET = os.environ.get("CREEM_WEBHOOK_SECRET", "").strip()

ENV = ("off" if not API_KEY
       else "sandbox" if API_KEY.startswith("creem_test_") else "production")
API_BASE = os.environ.get("CREEM_API_BASE", "").rstrip("/") or (
    "https://test-api.creem.io/v1" if ENV == "sandbox" else "https://api.creem.io/v1")

_STUB_SECRET = "stub-webhook-secret"
# Public by definition (it is in this source). Opt-in and NEVER honored in
# production, so a prod running without a real secret stays inert (every
# webhook 401s) instead of letting anyone flip an org to Pro.
ALLOW_STUB = os.environ.get("CREEM_ALLOW_STUB", "").lower() in ("1", "true", "yes")


def configured() -> bool:
    """True when we can talk to real Creem (test mode or production)."""
    return ENV in ("sandbox", "production") and bool(API_KEY and PRODUCT_PRO)


def stub_allowed() -> bool:
    return ALLOW_STUB and ENV != "production"


def _secret() -> str:
    if WEBHOOK_SECRET:
        return WEBHOOK_SECRET
    return _STUB_SECRET if stub_allowed() else ""      # inert when not allowed


def create_checkout(org_id: str, email: str, success_url: str) -> dict:
    """Return {url, id, stub}. The org rides in metadata — the webhook hands it
    back, which is how the entitlement finds its organization."""
    if not configured():
        return {"id": f"stub_{org_id}",
                "url": f"/v1/billing/checkout/stub?org={org_id}", "stub": True}
    import requests
    r = requests.post(
        f"{API_BASE}/checkouts",
        headers={"x-api-key": API_KEY},
        json={"product_id": PRODUCT_PRO,
              "customer": {"email": email} if email else None,
              "success_url": success_url,
              "metadata": {"org": str(org_id)}}, timeout=10)
    r.raise_for_status()
    d = r.json()
    return {"id": d.get("id"), "url": d.get("checkout_url"), "stub": False}


def create_customer_session(org_id: str, return_url: str,
                            customer_id: str = "") -> dict:
    """Return {url, stub}. Creem hosts the customer portal (invoices, card,
    cancellation) — we mint a link for the org's stored Creem customer id,
    which the webhook recorded at first payment. Without one yet, fall back to
    our own status page so the button never dead-ends."""
    if not (configured() and customer_id):
        return {"url": f"/v1/billing/status?org={org_id}", "stub": True}
    import requests
    r = requests.post(
        f"{API_BASE}/customers/billing",
        headers={"x-api-key": API_KEY},
        json={"customer_id": customer_id}, timeout=10)
    r.raise_for_status()
    d = r.json()
    return {"url": d.get("customer_portal_link") or d.get("url"), "stub": False}


def sign(raw: bytes) -> str:
    """A valid creem-signature value for the current secret (tests/simulate)."""
    return hmac.new(_secret().encode(), raw, hashlib.sha256).hexdigest()


def verify_webhook(raw: bytes, headers: dict) -> bool:
    """HMAC-SHA256 hex of the raw body, header `creem-signature`."""
    if not _secret():
        return False
    sig = (headers.get("creem-signature") or "").strip()
    if not sig:
        return False
    want = hmac.new(_secret().encode(), raw, hashlib.sha256).hexdigest()
    # tolerate an optional scheme prefix ("sha256=…") — costs nothing, saves a
    # class of integration bug
    return hmac.compare_digest(want, sig.split("=", 1)[-1].strip())


# Creem's event names, normalized to the shapes _apply_billing_event acts on.
# past_due is deliberately mapped to itself: a failing card must not revoke
# access mid-period — Creem escalates it to `expired` when it gives up.
_TYPE_MAP = {
    "checkout.completed": "order.created",
    "subscription.active": "subscription.active",
    "subscription.paid": "subscription.active",
    "subscription.trialing": "subscription.active",
    "subscription.update": "subscription.updated",
    "subscription.updated": "subscription.updated",
    "subscription.canceled": "subscription.canceled",
    "subscription.scheduled_cancel": "subscription.canceled",
    "subscription.expired": "subscription.revoked",
}


def _id_of(v):
    """Creem nests related objects either as a bare id string or expanded."""
    return v.get("id") if isinstance(v, dict) else v


def parse_event(payload: dict) -> dict:
    """Normalize a Creem webhook envelope {id, eventType, object} to the fields
    we act on. Tolerant of shape; the org comes back out of the metadata we set
    at checkout."""
    raw_type = payload.get("eventType") or payload.get("type", "")
    obj = payload.get("object", payload) or {}
    meta = obj.get("metadata") or {}
    if not meta.get("org") and isinstance(obj.get("checkout"), dict):
        meta = obj["checkout"].get("metadata") or {}
    if raw_type.startswith("subscription"):
        sub_id = obj.get("id")
    else:
        sub_id = _id_of(obj.get("subscription"))
    status = obj.get("status")
    # a completed checkout IS the proof of payment; its own status ("completed")
    # would fail _apply_billing_event's active-status check
    if raw_type == "checkout.completed":
        status = None
    return {"type": _TYPE_MAP.get(raw_type, raw_type),
            "org_id": meta.get("org"),
            "subscription_id": sub_id,
            "customer_id": _id_of(obj.get("customer")),
            "status": "active" if status in ("active", "paid") else status,
            "until": (obj.get("current_period_end_date")
                      or obj.get("current_period_end"))}
