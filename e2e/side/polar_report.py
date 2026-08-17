"""Ask Polar sandbox whether the payment the browser walk claims actually happened (#267).

A green browser walk proves the *screens* work. It does not prove money moved:
the redirect to success_url fires on Polar's side, and the entitlement flip
depends on a webhook that can be misconfigured, unsigned, or silently dropped.
So the walk is only believed once sandbox.polar.sh agrees.

The verdict hangs on the **subscription**, because that is the object the
entitlement is derived from: a confirmed checkout with no subscription is a
half-finished purchase, and the customer would see PRO in the app and nothing
in Polar. Orders are reported too when the token is allowed to read them —
`orders:read` is a separate scope and the stack's own token does not carry it,
so its absence is noted rather than treated as a failure.

Usage:  polar_report.py <customer-email> [checkout-client-secret]
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("POLAR_API_BASE", "https://sandbox-api.polar.sh").rstrip("/")
TOKEN = os.environ.get("POLAR_ORG_TOKEN", "")

# Polar sits behind Cloudflare, which answers 403 (error 1010) to the default
# Python-urllib signature. A named agent of our own gets through — and tells
# Polar's logs who is calling.
UA = "overwatch-e2e/1.0 (+https://overwatch.confinia.io)"


def get(path):
    req = urllib.request.Request(BASE + path, headers={
        "Authorization": "Bearer " + TOKEN, "Accept": "application/json",
        "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def money(cents, currency):
    return f"{(cents or 0) / 100:.2f} {(currency or '').upper()}"


def main():
    email = sys.argv[1]
    client_secret = sys.argv[2] if len(sys.argv) > 2 else None
    if not TOKEN:
        sys.exit("POLAR_ORG_TOKEN is not set — cannot verify the payment in Polar")

    print(f"\n=== Polar sandbox report — {BASE} ===")
    print(f"  customer   {email}")
    problems = []

    # --- the checkout the browser was sent to --------------------------------
    if client_secret:
        # The last path segment of the checkout URL is the CLIENT SECRET
        # (polar_c_…), not the checkout's UUID — /v1/checkouts/{id} answers 422
        # for it. The client endpoint is the one that takes this form.
        st, ck = get(f"/v1/checkouts/client/{urllib.parse.quote(client_secret)}")
        if st == 200:
            print(f"  checkout   {ck.get('id')}  status={ck.get('status')}  "
                  f"{money(ck.get('total_amount'), ck.get('currency'))}  "
                  f"product={(ck.get('product') or {}).get('name')}")
            if ck.get("status") not in ("confirmed", "succeeded"):
                problems.append(f"checkout status is {ck.get('status')}, not confirmed")
        else:
            # Printed, not just collected: an early exit further down must never
            # swallow the first diagnostic of the run.
            print(f"  checkout   {client_secret} could not be read ({st}: {ck})")
            problems.append(f"checkout {client_secret} could not be read ({st})")

    # --- the customer Polar created for this run -----------------------------
    st, cust = get("/v1/customers/?" + urllib.parse.urlencode({"email": email, "limit": 5}))
    if st != 200:
        sys.exit(f"  customers  Polar refused the query ({st}: {cust})")
    mine = [c for c in cust.get("items", [])
            if (c.get("email") or "").lower() == email.lower()]
    if not mine:
        print("  customer   NONE")
        sys.exit("\nFAIL: Polar sandbox has no customer for this run — the checkout "
                 "never completed.")
    cid = mine[0]["id"]
    print(f"  customer   {cid}")

    # --- the subscription: what the entitlement is actually made of ----------
    st, subs = get("/v1/subscriptions/?" + urllib.parse.urlencode(
        {"customer_id": cid, "limit": 10}))
    active = []
    if st == 200:
        for s in subs.get("items", []):
            print(f"  subscript. {s.get('id')}  status={s.get('status')}  "
                  f"product={(s.get('product') or {}).get('name')}  "
                  f"period_end={s.get('current_period_end')}")
            if s.get("status") in ("active", "trialing"):
                active.append(s)
    else:
        problems.append(f"subscriptions could not be read ({st})")

    # --- orders: nice to have, needs its own scope ---------------------------
    st, orders = get("/v1/orders/?" + urllib.parse.urlencode({"customer_id": cid, "limit": 10}))
    if st == 200:
        for o in orders.get("items", []):
            print(f"  order      {o.get('id')}  status={o.get('status')}  "
                  f"{money(o.get('total_amount'), o.get('currency'))}")
    elif st == 403:
        print("  orders     not readable — this token has no orders:read scope "
              "(add it to see amounts; not required for the verdict)")
    else:
        problems.append(f"orders could not be read ({st})")

    if not active:
        problems.append("no active subscription for this customer")

    if problems:
        print()
        for p in problems:
            print(f"  ! {p}")
        sys.exit("\nFAIL: the walk reached the success page, but Polar sandbox does not "
                 "confirm a live subscription — the checkout did not complete, or the "
                 "webhook never told us about it.")

    print(f"\nOK: Polar sandbox confirms an active subscription for {email}.")


if __name__ == "__main__":
    main()
