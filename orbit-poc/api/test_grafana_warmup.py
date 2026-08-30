"""Guards issue #409: the warm-session flag must record a result, not an attempt.

On a cold visit Grafana paints its home page inside a d-solo iframe. The
mitigation reloads the frame, which is what a manual refresh does and what no
visitor will ever think to do. The first version set `gfReady = true` in the
same statement that read it, before anything had painted, so a failed first
attempt convinced every later embed that the session was warm and no frame was
ever retried again (#105 reopened as #409).
"""
import os
import re

APP = os.path.join(os.path.dirname(__file__), "..", "web", "static", "app.js")


def _src():
    return open(APP, encoding="utf-8").read()


def test_the_flag_is_never_set_where_it_is_read():
    src = _src()
    assert "gfReady = true;" not in src.replace("if (home !== true) gfReady = true;", ""), \
        "gfReady must only be set after a panel is seen to paint"
    assert not re.search(r"!gfReady;\s*gfReady\s*=\s*true", src), \
        "reading and asserting readiness in one statement is the #409 bug"


def test_a_home_page_frame_is_detected_and_retried():
    src = _src()
    assert "function gfShowsHome(" in src
    body = src[src.index("function gfShowsHome("):]
    body = body[:body.index("\n}")]
    assert '"/d-solo/"' in body, "the structural signal is the route Grafana left"
    assert "Welcome to Grafana" in body, "text check backs up the route check"
    assert "return null" in body, "cross-origin mirror must yield no opinion"


def test_retries_are_bounded_and_cross_origin_keeps_the_old_behaviour():
    src = _src()
    m = re.search(r"const retry = (.+?);", src)
    assert m, "no retry decision"
    expr = m.group(1)
    assert "tries < 3" in expr, "verified-wrong frames retry a few times"
    assert "home === null && cold && tries < 1" in expr, \
        "unreadable (mirror) frames keep exactly one blind cold reload"


def test_a_known_broken_panel_is_not_reported_as_loaded():
    src = _src()
    handler = src[src.index("const home = gfShowsHome(f);"):]
    handler = handler[:handler.index("done++")]
    assert "if (home === true) return;" in handler, \
        "counting a frame we can see is wrong would claim all panels loaded"
