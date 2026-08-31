"""Guards issue #421: the landing page tells a mission team what it can do
for them. Every channel points here, and the page showed a globe with no
hint that private telemetry exists, although /pro.html says it all one URL
away. Gated on the billing probe so the selfhost edition (#280) never grows
a plans link that would be false there."""
import os

APP = os.path.join(os.path.dirname(__file__), "..", "web", "static", "app.js")


def test_the_affordance_exists_and_points_at_the_pro_page():
    js = open(APP, encoding="utf-8").read()
    assert 'a.href = "/pro.html"' in js
    assert "Fly something?" in js, "the line must speak to the audience it is for"


def test_it_only_appears_where_the_billing_surface_answers():
    js = open(APP, encoding="utf-8").read()
    probe = js[js.index("billing/mode"):]
    probe = probe[:probe.index(".catch")]
    assert 'id = "fly"' in probe, \
        "insert inside the successful billing probe, or selfhost grows a dead link"
