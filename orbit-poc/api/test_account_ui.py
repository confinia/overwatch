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


# ---------------------------------------------------------------------------
# An organization is not the price of a first look (#345)
# ---------------------------------------------------------------------------
APP_JS = os.path.join(STATIC, "app.js")


def test_org_creation_is_a_form_not_a_browser_prompt():
    """window.prompt() is suppressed by several mobile browsers and embedded
    webviews — the kind a forum link opens. It returned null, the handler
    returned silently, and the button did nothing, with no request to log. The
    access log for our first real signup showed zero POST /v1/orgs: nobody
    ever reached org creation."""
    html = open(ACCOUNT, encoding="utf-8").read()
    fn = html[html.index("async function createOrg("):]
    fn = fn[:fn.index("\nasync function", 10)]
    # code only — the comment explaining what this replaced names prompt(),
    # and a guard that trips on its own documentation is a guard nobody keeps
    code = "\n".join(l.split("//", 1)[0] for l in fn.splitlines())
    assert "prompt(" not in code, "org creation still asks via window.prompt()"
    assert 'id="org-name"' in html and "<form" in html, \
        "there must be a real form field, which is visible when it fails"
    assert 'onsubmit="return createOrg(event)"' in html


def test_the_control_room_does_not_push_an_organization():
    """The fleet, ground stations and passes are open data. Presenting an
    organization as the next step after signing in put our data model in front
    of the product."""
    js = open(APP_JS, encoding="utf-8").read()
    assert "createOrg" not in js, \
        "the globe header still sells an organization to a signed-in visitor"


def test_the_account_page_says_an_organization_is_optional():
    html = open(ACCOUNT, encoding="utf-8").read()
    assert "do not need an" in html and "open data" in html, \
        "a signed-in visitor must be told an organization is not required"


def test_creating_one_explains_the_re_login():
    """Being bounced to a login screen straight after signing up reads as the
    signup having failed."""
    html = open(ACCOUNT, encoding="utf-8").read()
    assert "Signing you in again so it takes effect" in html
