"""Guards #471: the app must be USABLE on a phone (iPhone SE, 375x667).

The failure mode this pins down: below 900px the old stylesheet simply did
`#side { display:none }` — no satellite list and NO SEARCH on a phone — while
the topbar chips stacked three rows deep over the globe and half the screen
went to dashboard iframes that loaded even when empty. Source-invariant, in
the test_frontend.py style.
"""
import os
import re

import pytest

HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "..", "web", "static")


def _read(name):
    p = os.path.join(STATIC, name)
    if not os.path.exists(p):
        pytest.skip("web/static not available in this run")
    return open(p, encoding="utf-8").read()


def _phone_css(html):
    """Every @media (max-width: 900px) block's body, concatenated."""
    blocks = []
    for m in re.finditer(r"@media \(max-width:\s*900px\)\s*\{", html):
        depth, i = 1, m.end()
        while depth and i < len(html):
            depth += {"{": 1, "}": -1}.get(html[i], 0)
            i += 1
        blocks.append(html[m.end():i])
    assert blocks, "the phone breakpoint must exist"
    return "\n".join(blocks)


def test_the_tab_bar_exists_and_only_below_the_breakpoint():
    html = _read("index.html")
    for view in ("globe", "sats", "data"):
        assert f'data-view="{view}"' in html, f"tab for {view} missing"
    css = _phone_css(html)
    assert "#tabbar { display:flex; }" in css, \
        "the bar appears on phones/tablets only — desktop keeps the grid"


def test_the_list_and_search_are_restored_on_phones():
    css = _phone_css(_read("index.html"))
    assert 'body[data-view="sats"] #side { display:flex; }' in css, \
        ("the Satellites view must show the full side pane; the old layout "
         "amputated the list AND the search below 900px")
    assert not re.search(r"^\s*#side \{ display:none; \}", css, re.M), \
        "no unconditional amputation: hiding #side must depend on the view"


def test_one_thin_chip_row_instead_of_a_stack():
    css = _phone_css(_read("index.html"))
    assert "flex-wrap:nowrap" in css and "overflow-x:auto" in css, \
        "the topbar chips must scroll in one row, not bury the globe"


def test_switching_views_resizes_the_map_and_selection_returns_to_it():
    js = _read("app.js")
    assert "function setView(" in js
    assert "map.resize()" in js, \
        "MapLibre must be told its pane changed size or the globe distorts"
    assert re.search(r"if \(!auto && phoneMode\(\)\) setView\(\"globe\"\)", js), \
        ("picking from the Satellites view lands on the globe, but deep-link "
         "auto-selection must not yank someone who already switched tabs")


def test_every_dashboard_iframe_is_lazy():
    js = _read("app.js")
    for m in re.finditer(r"<iframe[^>]*", js):
        assert 'loading="lazy"' in m.group(0), \
            (m.group(0)[:60] + "… — an eager iframe loads Grafana even while "
             "its view is hidden, which is half a phone screen of dead weight")
