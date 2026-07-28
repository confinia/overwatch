"""Polar.sh client — sandbox-first, stub-capable (see POLAR.md).

Env-gated by POLAR_ENV = off | sandbox | production. When off / unconfigured it
runs in STUB mode: create_checkout returns an in-app stub URL and webhooks verify
against a shared test secret — so the whole billing flow (checkout -> webhook ->
entitlement) is testable with no Polar account and no money. Set the sandbox env
vars (POLAR.md §3) and the same code talks to real Polar sandbox, no code change.

NOTE: verify_webhook here is a simple HMAC-over-body for the scaffold. Before
enabling real sandbox, replace it with Polar's actual standard-webhooks (svix)
scheme (webhook-id/webhook-timestamp/webhook-signature over `id.ts.body`).
"""
import hashlib
import hmac
import os

POLAR_ENV = os.environ.get("POLAR_ENV", "off").lower()
API_BASE = os.environ.get("POLAR_API_BASE", "https://sandbox-api.polar.sh")
ORG_TOKEN = os.environ.get("POLAR_ORG_TOKEN", "").strip()
PRODUCT_PRO = os.environ.get("POLAR_PRODUCT_ID_PRO", "").strip()
WEBHOOK_SECRET = os.environ.get("POLAR_WEBHOOK_SECRET", "").strip()
_STUB_SECRET = "stub-webhook-secret"
# The stub webhook secret is public (it's in this source). It is opt-in via
# POLAR_ALLOW_STUB and NEVER honored in production, so a public prod running with
# no real secret stays inert (every webhook 401s) instead of letting anyone flip
# an org to Pro. Only the isolated sandbox stack sets POLAR_ALLOW_STUB=1.
ALLOW_STUB = os.environ.get("POLAR_ALLOW_STUB", "").lower() in ("1", "true", "yes")


def configured() -> bool:
    """True when we can talk to real Polar (sandbox or prod)."""
    return POLAR_ENV in ("sandbox", "production") and bool(ORG_TOKEN and PRODUCT_PRO)


def stub_allowed() -> bool:
    """Stub billing (public secret, simulate endpoint) is allowed here?"""
    return ALLOW_STUB and POLAR_ENV != "production"


def _secret() -> str:
    if WEBHOOK_SECRET:
        return WEBHOOK_SECRET
    return _STUB_SECRET if stub_allowed() else ""      # inert when not allowed


def create_checkout(org_id: str, email: str, success_url: str) -> dict:
    """Return {url, id, stub}. Real Polar checkout when configured; otherwise a
    stub URL pointing back at our own simulate flow so it's testable/embeddable."""
    if not configured():
        return {"id": f"stub_{org_id}",
                "url": f"/v1/billing/checkout/stub?org={org_id}", "stub": True}
    import requests
    r = requests.post(
        f"{API_BASE}/v1/checkouts/",
        headers={"Authorization": f"Bearer {ORG_TOKEN}"},
        json={"product_id": PRODUCT_PRO, "customer_external_id": str(org_id),
              "customer_email": email, "success_url": success_url}, timeout=10)
    r.raise_for_status()
    d = r.json()
    return {"id": d.get("id"), "url": d.get("url"), "stub": False}


def sign(raw: bytes) -> str:
    """Produce a valid signature header value for the current secret (tests/sim)."""
    return "sha256=" + hmac.new(_secret().encode(), raw, hashlib.sha256).hexdigest()


def verify_webhook(raw: bytes, headers: dict) -> bool:
    secret = _secret()
    if not secret:
        return False
    sig = headers.get("webhook-signature") or headers.get("x-polar-signature") or ""
    want = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig.split("=", 1)[-1].strip())


def parse_event(payload: dict) -> dict:
    """Normalize a Polar webhook to the fields we act on. Tolerant of shape;
    org resolved via the external customer id we set at checkout."""
    typ = payload.get("type", "")
    data = payload.get("data", payload) or {}
    cust = data.get("customer") or {}
    org_id = (cust.get("external_id") or data.get("external_customer_id")
              or (data.get("metadata") or {}).get("org"))
    sub_id = data.get("id") if "subscription" in typ else data.get("subscription_id")
    return {"type": typ, "org_id": org_id, "subscription_id": sub_id,
            "status": data.get("status"),
            "until": data.get("current_period_end") or data.get("ends_at")}
