"""Guards issue #400: the reception popup names the satellite.

Forum, with a screenshot: "maybe small request, if we can know from what
satellite that decoded frame." On the station view every line is a
different satellite and the global selection is null by design, so a popup
that resolved the name from the selection said "the satellite" exactly
where the satellite was the unknown.
"""
import os

APP = os.path.join(os.path.dirname(__file__), "..", "web", "static", "app.js")


def test_reception_lines_carry_their_satellite():
    js = open(APP, encoding="utf-8").read()
    assert js.count("norad:r.norad, sat:r.name") == 1, "station-view lines"
    assert "norad, sat:(satsByNorad[norad]" in js, "satellite-view lines"


def test_the_popup_resolves_from_the_line_not_the_selection():
    js = open(APP, encoding="utf-8").read()
    popup = js[js.index('map.on("click", "rx-links-hit"'):]
    popup = popup[:popup.index(".addTo(map)")]
    assert "p.sat" in popup and "satsByNorad[p.norad]" in popup
    assert "satsByNorad[activeNorad]" not in popup, \
        "activeNorad is null on the station view — the placeholder returns"
    assert 'href="#${p.norad}"' in popup, "the name should open that satellite"
