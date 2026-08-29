"""Guards issue #108: the first visit must not be a learning curve.

Two operators said so on the SatNOGS forum in almost the same week — René
F1SXJ ("belle application mais qui mérite un apprentissage") and Daniel
DL7NDR ("what awaits me if I register?"). The guide is a card, not a tour:
readable in twenty seconds, shown once per browser, reachable forever after.
"""
import os

HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "..", "web", "static")


def _read(name):
    return open(os.path.join(STATIC, name), encoding="utf-8").read()


def test_a_first_run_guide_exists_and_is_dismissible():
    html = _read("index.html")
    assert 'id="guide"' in html, "no first-run guide in the page"
    assert 'id="guide-close"' in html, "the guide must be dismissible"
    js = _read("app.js")
    assert "ovw_guide_seen" in js, \
        "dismissal must persist per browser, or every visit nags"


def test_how_to_read_this_stays_reachable_after_dismissal():
    # The learning curve does not end after the first visit (F1SXJ is an
    # EXPERIENCED operator) — the guide must reopen on demand, forever.
    html = _read("index.html")
    assert 'id="guide-open"' in html
    assert "How to read this" in html
    js = _read("app.js")
    assert 'getElementById("guide-open")' in js


def test_the_guide_answers_the_actual_questions():
    html = _read("index.html")
    guide = html[html.index('id="guide"'):html.index('id="app"')]
    assert "orange dot" in guide, "must explain the reception network"
    assert "callsign" in guide, "must tell a station owner how to find themselves"
    # DL7NDR's question verbatim: what awaits me if I register? (#95 tie-in)
    assert "free" in guide and "private telemetry" in guide, \
        "must say what signing in gives, and that orgs are for private data only"
