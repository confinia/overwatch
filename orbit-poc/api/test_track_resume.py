"""Guards #465: panning away from the tracked satellite must offer the way
back — a "Back to <name>" pill on the first user camera gesture, and an
automatic return after 10 s without another one. Source-invariant, like
test_frontend.py: the affordance and its wiring must not silently regress.
"""
import os
import re

import pytest

HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "..", "web", "static")


@pytest.fixture(scope="module")
def app_js():
    p = os.path.join(STATIC, "app.js")
    if not os.path.exists(p):
        pytest.skip("web/static not available in this run")
    return open(p, encoding="utf-8").read()


@pytest.fixture(scope="module")
def index_html():
    p = os.path.join(STATIC, "index.html")
    if not os.path.exists(p):
        pytest.skip("web/static not available in this run")
    return open(p, encoding="utf-8").read()


def test_the_pill_exists_and_starts_hidden(index_html):
    m = re.search(r'<button id="resumetrack"[^>]*>', index_html)
    assert m, "the back-to-track control must exist"
    assert "hidden" in m.group(0), "it appears only once the camera is taken"


def test_only_a_user_gesture_shows_the_pill(app_js):
    # movestart with an originalEvent = a drag/wheel; our own flyTo has none.
    block = app_js[app_js.index('map.on("movestart"'):]
    block = block[:block.index("})")]
    assert "originalEvent" in block, \
        "programmatic camera moves must never trigger the back-to-track flow"


def test_inactivity_resumes_tracking_after_ten_seconds(app_js):
    assert "RESUME_TRACK_MS = 10000" in app_js
    assert re.search(r'map\.on\("moveend".*\n.*camAway', app_js), \
        "the timer arms when the user gesture ends, not mid-drag"
    assert "setTimeout(resumeTrack, RESUME_TRACK_MS)" in app_js


def test_resume_flies_back_and_selection_resets_the_state(app_js):
    resume = app_js[app_js.index("function resumeTrack()"):]
    resume = resume[:resume.index("\n}")]
    assert "flyTo" in resume and "satsByNorad[activeNorad]" in resume
    # clicking the pill and selecting a satellite both end the away state
    assert 'getElementById("resumetrack").addEventListener("click", resumeTrack)' \
        in app_js
    assert "trackEngaged();" in app_js
