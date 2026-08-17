"""Ask Polar sandbox whether the payment the browser walk claims actually happened (#267).

A green browser walk proves the *screens* work. It does not prove money moved:
the redirect to success_url fires on Polar's side, and the entitlement flip
depends on a webhook that can be misconfigured, unsigned, or silently dropped.
So the walk is only believed once sandbox.polar.sh agrees there is an order for
this run's customer.

No order for a walk that reached the success page is a FAILURE, not a warning —
that gap is exactly the checkout/webhook bug this step exists to catch.

Usage:  polar_report.py <customer-email> [checkout-id]
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
    checkout_id = sys.argv[2] if len(sys.argv) > 2 else None
    if not TOKEN:
        sys.exit("POLAR_ORG_TOKEN is not set — cannot verify the payment in Polar")

    print(f"\n=== Polar sandbox report — {BASE} ===")

    if checkout_id:
        st, ck = get(f"/v1/checkouts/{urllib.parse.quote(checkout_id)}")
        if st == 200:
            print(f"  checkout   {ck.get('id')}")
            print(f"             status={ck.get('status')} "
                  f"amount={money(ck.get('total_amount'), ck.get('currency'))} "
                  f"product={(ck.get('product') or {}).get('name')}")
        else:
            print(f"  checkout   lookup failed ({st})")

    st, orders = get(f"/v1/orders/?limit=10")
    if st != 200:
        sys.exit(f"  orders     Polar refused the query ({st}: {orders})")

    mine = [o for o in orders.get("items", [])
            if (o.get("customer") or {}).get("email", "").lower() == email.lower()]
    if not mine:
        print(f"  orders     NONE for {email}")
        sys.exit("\nFAIL: the walk reached the success page but Polar sandbox has no "
                 "order for this customer — the checkout did not complete, or the "
                 "webhook never told us about it.")

    for o in mine:
        print(f"  order      {o.get('id')}")
        print(f"             status={o.get('status')} paid={o.get('paid')} "
              f"total={money(o.get('total_amount'), o.get('currency'))}")
        print(f"             product={(o.get('product') or {}).get('name')} "
              f"subscription={o.get('subscription_id') or '—'}")

    st, subs = get("/v1/subscriptions/?limit=10")
    if st == 200:
        for s in subs.get("items", []):
            if (s.get("customer") or {}).get("email", "").lower() == email.lower():
                print(f"  subscript. {s.get('id')} status={s.get('status')} "
                      f"period_end={s.get('current_period_end')}")

    print(f"\nOK: Polar sandbox confirms {len(mine)} paid order(s) for {email}.")


if __name__ == "__main__":
    main()
