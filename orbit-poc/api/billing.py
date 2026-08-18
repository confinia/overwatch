"""The billing provider seam (#269).

main.py talks to THIS module only. The active merchant of record is chosen by
BILLING_PROVIDER (creem | polar); every provider exposes the same surface:

    configured() stub_allowed() create_checkout() create_customer_session()
    verify_webhook() parse_event() sign() ENV

Default is creem — the EU merchant of record (Armitage Labs OÜ, Estonia). The
polar module stays selectable so an already-deployed stack keeps working and
the switch is a one-variable rollback. Both run identical STUB behaviour when
unconfigured, so environments without billing (prod/staging today) behave the
same whichever provider is named.
"""
import os

PROVIDER = os.environ.get("BILLING_PROVIDER", "creem").strip().lower()

if PROVIDER == "polar":
    import polar as _p
    ENV = _p.POLAR_ENV
else:
    PROVIDER = "creem"
    import creem as _p
    ENV = _p.ENV

configured = _p.configured
stub_allowed = _p.stub_allowed
verify_webhook = _p.verify_webhook
parse_event = _p.parse_event
sign = _p.sign

# plans the active provider can actually sell (#275)
PLANS = tuple(getattr(_p, "PRODUCTS", {"pro": True}).keys()) or ("pro",)


def create_checkout(org_id: str, email: str, success_url: str,
                    plan: str = "pro") -> dict:
    """Polar predates the plan ladder and only ever sold Pro."""
    if PROVIDER == "polar":
        if plan != "pro":
            raise LookupError(f"plan {plan!r} is not available via polar")
        return _p.create_checkout(org_id, email, success_url)
    return _p.create_checkout(org_id, email, success_url, plan)


def create_customer_session(org_id: str, return_url: str,
                            customer_id: str = "") -> dict:
    """Polar addresses the portal by our org id (set as external id at
    checkout); Creem needs its own customer id, recorded by the webhook."""
    if PROVIDER == "creem":
        return _p.create_customer_session(org_id, return_url, customer_id)
    return _p.create_customer_session(org_id, return_url)
