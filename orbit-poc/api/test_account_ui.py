"""Guards issue #124: the account surface exists and is wired — identity,
plan badge, subscription state, and the checkout/portal actions; the header
links to it; checkout success lands on it.
"""
import os

HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "..", "web", "static")
ACCOUNT = os.path.join(STATIC, "account.html")
INDEX = os.path.join(STATIC, "index.html")
MAIN = os.path.join(HERE, "main.py")


def test_account_page_exists_and_fetches_status():
    html = open(ACCOUNT, encoding="utf-8").read()
    assert "/api/v1/me" in html                       # identity
    assert "/api/v1/billing/status" in html           # plan + subscription
    assert 'class="badge' in html                     # plan badge (PRO/FREE)


def test_account_page_wires_checkout_and_portal():
    html = open(ACCOUNT, encoding="utf-8").read()
    assert "/api/v1/billing/checkout" in html         # Upgrade action
    assert "/api/v1/billing/portal" in html           # Invoices / manage
    assert "/api/v1/auth/login" in html               # anonymous fallback
    assert "/api/v1/auth/logout" in html


def test_account_page_acknowledges_purchase():
    html = open(ACCOUNT, encoding="utf-8").read()
    assert "upgraded" in html and 'id="banner"' in html


def test_header_links_to_account():
    # the header is rendered by app.js since the MapLibre 6 split (#145)
    html = open(INDEX, encoding="utf-8").read()
    app = os.path.join(STATIC, "app.js")
    if os.path.exists(app):
        html += open(app, encoding="utf-8").read()
    assert html.count('href="/w/account"') >= 2       # org + no-org signed-in states


def test_checkout_success_lands_on_account():
    src = open(MAIN, encoding="utf-8").read()
    assert "/w/account?upgraded=1" in src
