"""Guards issue #258: the time range is part of the address.

A shared link or bookmark must show the sender's window, not the reader's
leftover localStorage; and a cold first visit should answer "what is
happening now" (24h), not pull a week of receptions before first paint.
File-level guards in the style of the other static-UI tests.
"""
import os
import re

APP = os.path.join(os.path.dirname(__file__), "..", "web", "static", "app.js")


def _src():
    return open(APP, encoding="utf-8").read()


def test_the_default_range_is_24h():
    src = _src()
    m = re.search(r"if \(!RANGES\.some\(r => r\[0\] === rangeHours\)\) "
                  r"rangeHours = (\d+)", src)
    assert m and m.group(1) == "24", "the cold-visit default must be 24h"


def test_the_url_wins_over_localstorage():
    src = _src()
    read = src.index('get("range")')
    stored = src.index('localStorage.getItem("ovw_rangeHours")')
    assert read < stored, "?range= must be read before the leftover setting"


def test_clicking_a_range_writes_the_url_in_place():
    src = _src()
    assert "rangeToUrl()" in src[src.index("function setRange"):], \
        "setRange must write the range back to the address bar"
    assert 'q.set("range"' in src and "history.replaceState" in src


def test_clearing_the_selection_keeps_the_range():
    # replaceState(pathname) alone would silently drop ?range=
    assert "location.pathname + location.search" in _src()
