"""Polar (Merchant of Record) pro-registration config tests.

Verifies the LIVE Polar setup a user meets when registering for a pro
account: the Fleet Tenant product (price, monthly interval, 14-day trial,
routing metadata) and the design-partner discount codes.

Needs POLAR_ACCESS_TOKEN in the environment (a repo secret in CI). Skipped
when absent so local runs without the secret stay green.
"""
import os
import pytest
import requests

TOKEN = os.environ.get("POLAR_ACCESS_TOKEN", "")
API = "https://api.polar.sh/v1"
pytestmark = pytest.mark.skipif(not TOKEN, reason="POLAR_ACCESS_TOKEN not set")


def _get(path):
    r = requests.get(f"{API}{path}", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=20)
    r.raise_for_status()
    return r.json()


@pytest.fixture(scope="module")
def fleet_product():
    items = _get("/products/").get("items", [])
    p = next((x for x in items if x["name"] == "Overwatch Fleet Tenant (beta)"), None)
    assert p is not None, "Fleet Tenant product missing on Polar"
    return p


def test_product_is_active_recurring(fleet_product):
    assert fleet_product["is_recurring"] is True
    assert fleet_product["is_archived"] is False


def test_price_is_490_eur_monthly(fleet_product):
    price = fleet_product["prices"][0]
    assert price["price_amount"] == 49000
    assert price["price_currency"] == "eur"
    assert price["recurring_interval"] == "month"


def test_trial_is_14_days(fleet_product):
    assert fleet_product.get("trial_interval") == "day"
    assert fleet_product.get("trial_interval_count") == 14


def test_routing_metadata_present(fleet_product):
    md = fleet_product.get("metadata", {})
    assert md.get("app") == "overwatch"
    assert md.get("plan") == "fleet"


def test_three_design_partner_codes(fleet_product):
    codes = {d["code"]: d for d in _get("/discounts/").get("items", [])}
    for n in (1, 2, 3):
        d = codes.get(f"DESIGNPARTNER{n}")
        assert d, f"DESIGNPARTNER{n} missing"
        assert d["basis_points"] == 10000        # 100% off
        assert d["duration_in_months"] == 3
        assert d["max_redemptions"] == 1


def test_checkout_page_reachable():
    """The public checkout link a pro user lands on must resolve."""
    url = "https://buy.polar.sh/polar_cl_VYpdGBRjeH4Pn7vvD1dNbN450Yncw9sOHwqLf154vRQ"
    assert requests.get(url, timeout=20).status_code == 200
