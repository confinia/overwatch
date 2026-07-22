# Test — Polar pro-account registration

What we validate about a user **registering for a pro account** through
Polar (the merchant of record), and how it is checked automatically.

Suite: [`orbit-poc/api/test_polar.py`](orbit-poc/api/test_polar.py).
CI: `.github/workflows/tests.yml` job **polar**.

## What is validated (against the LIVE Polar org)

| # | Expectation | Test |
|---|---|---|
| 1 | The Fleet Tenant product exists, is active and recurring | `test_product_is_active_recurring` |
| 2 | Price is €490.00 / month | `test_price_is_490_eur_monthly` |
| 3 | A 14-day free trial is configured | `test_trial_is_14_days` |
| 4 | Routing metadata `app=overwatch`, `plan=fleet` is present | `test_routing_metadata_present` |
| 5 | Three design-partner codes exist (100% off, 3 months, single use) | `test_three_design_partner_codes` |
| 6 | The public checkout link resolves | `test_checkout_page_reachable` |

## The registration path being protected

1. A user opens `/pro.html` → clicks **Start the 14-day trial**.
2. Polar checkout: €490/mo product, 14-day trial (card required, not
   charged during the trial), EUR, discount code accepted.
3. On success → `pro.html?welcome=1` (onboarding note).
4. Beta: the sale notification triggers manual tenant provisioning;
   target: the `subscription.active` webhook activates the organization
   (issue #16). The `app`/`plan` metadata routes that webhook — hence
   test #4 guards it.

Design-partner slots are the three `DESIGNPARTNER{1,2,3}` codes: a free
3-month path on the same product, converting to €490/mo afterwards.

## How it runs

Read-only checks against `api.polar.sh` using `POLAR_ACCESS_TOKEN`
(a GitHub repo secret in CI; an env var locally). No test creates or
charges anything. Skips cleanly when the token is absent.

```sh
cd orbit-poc/api && pip install pytest requests
POLAR_ACCESS_TOKEN=polar_oat_… pytest test_polar.py -v
```

## Latest results

- **Suite: 6 passed** (2026-07-22), run in a `python:3.12` container on the
  VM against the live Polar API. Confirms the €490/mo Fleet Tenant, its
  14-day trial, the routing metadata, and all three design-partner codes
  (0 redemptions).
- **CI activation pending**: the `POLAR_ACCESS_TOKEN` secret is set on the
  repo; the workflow file still needs the `workflow`-scope push to appear
  under `.github/workflows/` (see TEST_SUBSCRIPTION.md). This section
  updates with the Actions run link once CI is live.

## Security note

The Polar token grants product/checkout/discount management. It lives as a
GitHub **secret** (never in the repo) and in `orbit-poc/.env` on the VM.
Rotate it when this setup phase is complete.
