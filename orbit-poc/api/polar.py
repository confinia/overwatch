"""Polar.sh client — sandbox-first, stub-capable (see POLAR.md).

Env-gated by POLAR_ENV = off | sandbox | production. When off / unconfigured it
runs in STUB mode: create_checkout returns an in-app stub URL and webhooks verify
against a shared test secret — so the whole billing flow (checkout -> webhook ->
entitlement) is testable with no Polar account and no money. Set the sandbox env
vars (POLAR.md §3) and the same code talks to real Polar sandbox, no code change.

verify_webhook speaks BOTH schemes (#121): real Polar deliveries are signed per
standard-webhooks (HMAC-SHA256 over `webhook-id.webhook-timestamp.body` with the
base64-decoded whsec_ key, header `webhook-signature: v1,<base64>`), while the
stub/tests keep the simple legacy HMAC-hex over the body. The scheme is picked
from the headers actually present.
"""
import base64
import hashlib
import hmac
import os
import time

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


def create_customer_session(org_id: str, return_url: str) -> dict:
    """Return {url, stub}. As Merchant of Record, Polar hosts the customer
    portal where the end-user downloads invoices/receipts and manages the
    subscription — we just mint a session and hand back its portal URL. The
    customer is addressed by external_customer_id = our org id (set at checkout).
    Stub URL (own status page) when Polar isn't configured, so it stays testable."""
    if not configured():
        return {"url": f"/v1/billing/status?org={org_id}", "stub": True}
    import requests
    r = requests.post(
        f"{API_BASE}/v1/customer-sessions/",
        headers={"Authorization": f"Bearer {ORG_TOKEN}"},
        json={"external_customer_id": str(org_id), "return_url": return_url},
        timeout=10)
    r.raise_for_status()
    d = r.json()
    return {"url": d.get("customer_portal_url"), "stub": False}


def sign(raw: bytes) -> str:
    """Produce a valid signature header value for the current secret (tests/sim)."""
    return "sha256=" + hmac.new(_secret().encode(), raw, hashlib.sha256).hexdigest()


def _sw_key() -> bytes:
    """standard-webhooks signing key: base64-decode the part after whsec_."""
    s = _secret()
    if s.startswith("whsec_"):
        b64 = s[len("whsec_"):]
        return base64.b64decode(b64 + "=" * (-len(b64) % 4))
    return s.encode()


def sign_standard(msg_id: str, timestamp: str, raw: bytes) -> str:
    """standard-webhooks signature for `id.timestamp.body` (tests/sim)."""
    signed = f"{msg_id}.{timestamp}.".encode() + raw
    digest = hmac.new(_sw_key(), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


_SW_TOLERANCE = 300     # seconds of allowed webhook-timestamp clock skew


def verify_webhook(raw: bytes, headers: dict) -> bool:
    if not _secret():
        return False
    sig_header = headers.get("webhook-signature") or ""
    msg_id = headers.get("webhook-id")
    ts = headers.get("webhook-timestamp")
    if msg_id and ts and "v1," in sig_header:
        # real Polar: standard-webhooks. Header may carry several space-separated
        # signatures (secret rotation); accept if any v1 entry matches.
        try:
            if abs(time.time() - int(ts)) > _SW_TOLERANCE:
                return False
        except ValueError:
            return False
        want = sign_standard(msg_id, ts, raw).split(",", 1)[1]
        return any(
            v == "v1" and hmac.compare_digest(want, s)
            for v, _, s in (p.partition(",") for p in sig_header.split()))
    # legacy scaffold/stub scheme: HMAC-hex over the raw body
    sig = sig_header or headers.get("x-polar-signature") or ""
    want = hmac.new(_secret().encode(), raw, hashlib.sha256).hexdigest()
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
