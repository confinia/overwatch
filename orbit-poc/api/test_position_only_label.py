"""Guards issue #406: never promise frames that cannot arrive.

The open-network picker (#230) tracks position for anything catalogued;
telemetry needs an open decoder. The list said "no frames yet" for both
cases, so a reader could not tell "waiting" from "impossible here", and
went looking for a bug in someone else's database. Same honest-state rule
as #96 (absence is not a paywall) and #97 (claim only the known kind).
"""
import os

APP = os.path.join(os.path.dirname(__file__), "..", "web", "static", "app.js")


def test_a_satellite_without_a_decoder_is_labelled_position_only():
    js = open(APP, encoding="utf-8").read()
    block = js[js.index("const heard = "):]
    block = block[:block.index(";")]
    assert "s.has_telemetry" in block, \
        "the label must branch on whether telemetry is even possible"
    assert "position only (no open decoder)" in block
    assert "no frames yet" in block, "the waiting case keeps its own wording"
