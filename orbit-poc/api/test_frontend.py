"""Frontend affordance tests (#45 sign-in visible, #47 contact link).

Reads the served index.html and asserts the lead-capture affordances are
present, so they can't silently regress.
"""
import os
import pytest

HERE = os.path.dirname(__file__)
INDEX = os.path.join(HERE, "..", "web", "static", "index.html")


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
