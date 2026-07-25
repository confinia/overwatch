"""Frontend affordance tests (#45 sign-in visible, #47 contact link).

Reads the served index.html and asserts the lead-capture affordances are
present, so they can't silently regress.
"""
import os
import pytest

HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "..", "web", "static")
INDEX = os.path.join(STATIC, "index.html")
PAGES = ("index.html", "pro.html", "article.html", "talk.html")


@pytest.fixture(scope="module")
def html():
    if not os.path.exists(INDEX):
        pytest.skip("web/static not available in this run")
    return open(INDEX, encoding="utf-8").read()


def test_contact_link_present(html):            # #47
    assert "mailto:contact@confinia.io" in html


def test_signin_is_visible_action(html):        # #45
    # the anonymous entry uses the visible accent class, not the dim badge
    assert 'class="action"' in html
    assert "Sign in / Register" in html


def test_footer_links_present(html):            # #47
    for label in ("Contact", "Source", "API", "Write-up"):
        assert f">{label}<" in html


def test_favicon_files_exist():                 # #59
    if not os.path.exists(STATIC):
        pytest.skip("web/static not available in this run")
    for name in ("favicon.svg", "favicon.ico"):
        assert os.path.exists(os.path.join(STATIC, name)), f"missing {name}"


def test_every_page_links_favicon():            # #59
    if not os.path.exists(STATIC):
        pytest.skip("web/static not available in this run")
    for page in PAGES:
        t = open(os.path.join(STATIC, page), encoding="utf-8").read()
        assert 'rel="icon"' in t and "favicon.svg" in t, f"{page} missing favicon link"
