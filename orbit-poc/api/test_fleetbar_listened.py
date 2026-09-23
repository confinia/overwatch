"""Guards #490: the fleet bar's link tallies cover only satellites somebody
listens to (a decoder, or heard at least once, #464). Position-only
satellites, 122 of 153 on prod after the Planet feed (#456), are not silent:
they have no open downlink, and counting them as silent read as a broken
network. They get their own neutral count instead.

No JS engine in the test env: the assertions read app.js and index.html.
"""
import os
import re

HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "..", "web", "static")


def _src(name):
    return open(os.path.join(STATIC, name), encoding="utf-8").read()


def _fleetbar():
    js = _src("app.js")
    m = re.search(r"function renderFleetbar\(sats\)\{(.*?)\n\}", js, re.S)
    assert m, "renderFleetbar not found"
    return m.group(1)


def test_listened_means_a_decoder_or_heard_once():
    js = _src("app.js")
    m = re.search(r"function isListened\(s\)\{(.*?)\}", js, re.S)
    assert m, "isListened not found"
    assert "has_telemetry" in m.group(1) and "last_frame" in m.group(1)


def test_link_tallies_cover_only_listened_satellites():
    body = _fleetbar()
    assert "sats.filter(isListened)" in body
    assert "listened.forEach(s => by[linkStatus(s)]" in body
    assert "listened.filter(isLive)" in body
    assert "sats.forEach(s => by[linkStatus(s)]" not in body


def test_position_only_satellites_get_their_own_neutral_count():
    body = _fleetbar()
    assert "sats.length - listened.length" in body
    assert "position-only" in body
    assert "not counted as silent" in body
    assert "#fleetbar .fpos" in _src("index.html")
