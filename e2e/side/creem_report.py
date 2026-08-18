"""Ask Creem Test Mode whether the payment the browser walk claims actually happened (#267, #269).

A green browser walk proves the *screens* work. It does not prove money moved:
the redirect to success_url fires on Creem's side, and the entitlement flip
depends on a webhook that can be misconfigured, unsigned, or silently dropped.
So the walk is only believed once Creem's API agrees there is a live
subscription for this run's customer. No subscription for a walk that reached
the success page is a FAILURE, not a warning — that gap is exactly the
checkout/webhook bug this step exists to catch.

Usage:  creem_report.py <customer-email> [checkout-id]
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ.get("CREEM_API_KEY", "").strip()
BASE = os.environ.get("CREEM_API_BASE", "").rstrip("/") or (
    "https://test-api.creem.io/v1" if API_KEY.startswith("creem_test_")
    else "https://api.creem.io/v1")
UA = "overwatch-e2e/1.0 (+https://overwatch.confinia.io)"


def get(path):
    req = urllib.request.Request(BASE + path, headers={
        "x-api-key": API_KEY, "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def main():
    email = sys.argv[1]
    checkout_id = sys.argv[2] if len(sys.argv) > 2 else None
    if not API_KEY:
        sys.exit("CREEM_API_KEY is not set — cannot verify the payment in Creem")
    if not API_KEY.startswith("creem_test_"):
        sys.exit("refusing to run the e2e report against a PRODUCTION Creem key "
                 "(rule 27: Test Mode only for now)")

    print(f"\n=== Creem test-mode report — {BASE} ===")
    print(f"  customer   {email}")
    problems = []

    if checkout_id:
        st, ck = get(f"/checkouts/{urllib.parse.quote(checkout_id)}")
        if st == 200:
            print(f"  checkout   {ck.get('id')}  status={ck.get('status')}  "
                  f"product={(ck.get('product') or {}).get('name', ck.get('product'))}")
            if ck.get("status") not in ("completed", "succeeded", "paid"):
                problems.append(f"checkout status is {ck.get('status')}, not completed")
        else:
            print(f"  checkout   {checkout_id} could not be read ({st}: {ck})")
            problems.append(f"checkout {checkout_id} could not be read ({st})")

    st, cust = get("/customers?" + urllib.parse.urlencode({"email": email}))
    if st != 200:
        sys.exit(f"  customer   Creem refused the query ({st}: {cust})")
    items = cust.get("items", [cust] if cust.get("id") else [])
    mine = [c for c in items if (c.get("email") or "").lower() == email.lower()]
    if not mine:
        print("  customer   NONE")
        sys.exit("\nFAIL: Creem test mode has no customer for this run — the "
                 "checkout never completed.")
    cid = mine[0]["id"]
    print(f"  customer   {cid}")

    # /v1/subscriptions wants a subscription_id; by-customer listing lives
    # under the customer resource
    st, subs = get(f"/customers/{urllib.parse.quote(cid)}/subscriptions")
    active = []
    if st == 200:
        for s in subs.get("items", [subs] if subs.get("id") else []):
            print(f"  subscript. {s.get('id')}  status={s.get('status')}  "
                  f"product={(s.get('product') or {}).get('name', s.get('product'))}  "
                  f"period_end={s.get('current_period_end_date')}")
            if s.get("status") in ("active", "trialing"):
                active.append(s)
    else:
        problems.append(f"subscriptions could not be read ({st}: {subs})")

    if not active:
        problems.append("no active subscription for this customer")

    if problems:
        print()
        for p in problems:
            print(f"  ! {p}")
        sys.exit("\nFAIL: the walk reached the success page, but Creem does not "
                 "confirm a live subscription — the checkout did not complete, or "
                 "the webhook never told us about it.")

    print(f"\nOK: Creem test mode confirms an active subscription for {email}.")


if __name__ == "__main__":
    main()
