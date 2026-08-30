"""Guards issue #408: the bars over the globe must not overlap.

Five elements each claimed `position:absolute; top:10px`: the first-run guide
button, the fleet bar, the centred range bar, the reception legend and the
OEM banner. They covered each other. The globe pane is resized by dragging a
gutter, so its width is not the viewport width and no media query can fix
this. One wrapping flex row cannot overlap at any drag position.
"""
import os
import re

HTML = os.path.join(os.path.dirname(__file__), "..", "web", "static", "index.html")
CHILDREN = ("guide-open", "fleetbar", "rangebar", "rxlegend", "oem-banner")


def _css():
    src = open(HTML, encoding="utf-8").read()
    return src[src.index("<style>"):src.index("</style>")], src


def test_the_bars_live_in_one_row():
    _, src = _css()
    row = src[src.index('<div id="topbar">'):]
    row = row[:row.index("</div>\n    </div>") + 20] if "</div>\n    </div>" in row else row[:800]
    for name in CHILDREN:
        assert f'id="{name}"' in row, f"{name} must sit inside the topbar row"


def test_no_child_positions_itself_absolutely():
    css, _ = _css()
    for name in CHILDREN:
        rule = css[css.index(f"#{name} {{"):]
        rule = rule[:rule.index("}")]
        assert "position:absolute" not in rule, \
            f"#{name} still positions itself: it can overlap its neighbours again"
        assert not re.search(r"\btop:\s*10px", rule), f"#{name} still pins top:10px"


def test_the_row_lets_map_drags_through_its_gaps():
    css, _ = _css()
    rule = css[css.index("#topbar {"):]
    rule = rule[:rule.index("}")]
    assert "pointer-events:none" in rule, "empty row space would steal map drags"
    assert "flex-wrap:wrap" in rule, "without wrapping a narrow pane overlaps again"
    assert "#topbar > * { pointer-events:auto; }" in css, \
        "the children must take their own clicks back"
