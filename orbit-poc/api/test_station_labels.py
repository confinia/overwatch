"""Guards issue #402: a station's name ends at the LAST dash.

DL7NDR's station is "DL7NDR UHF-Turnstile-JN48ap". The labels split on the
first dash and showed the name as "DL7NDR UHF" with locator "Turnstile".
The ingest already did rsplit; only the labels were wrong.
"""
import os
import re

APP = os.path.join(os.path.dirname(__file__), "..", "web", "static", "app.js")


def test_no_label_splits_on_the_first_dash():
    js = open(APP, encoding="utf-8").read()
    assert 'split("-")[0]' not in js and 'split("-")[1]' not in js
    assert "function stationName(" in js and "function stationGrid(" in js


def test_the_helpers_agree_with_the_ingest_rsplit():
    js = open(APP, encoding="utf-8").read()
    body = js[js.index("function stationName("):js.index("function baseCall(")]
    assert 'lastIndexOf("-")' in body, "must split on the LAST dash, like rsplit"
    # a dashed name resolves the way the ingest resolves its locator
    obs = "DL7NDR UHF-Turnstile-JN48ap"
    assert obs.rsplit("-", 1) == ["DL7NDR UHF-Turnstile", "JN48ap"]
