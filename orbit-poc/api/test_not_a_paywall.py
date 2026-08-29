"""Guards issue #96: a missing satellite must not read as a paywall.

DL7NDR, verbatim: "I'm missing satellites like MARINA. Is the telemetry not
free?" Absence means no open decoder or nothing heard recently — never a
paid tier. And MARINA itself qualified on inspection (fresh decoded frames,
open 'marina' decoder), so it joins the fleet.
"""
import os

HERE = os.path.dirname(__file__)


def _read(*parts):
    return open(os.path.join(HERE, *parts), encoding="utf-8").read()


def test_marina_is_tracked_with_its_open_decoder():
    src = _read("..", "ingest", "satellites.py")
    assert '"norad": 98293' in src and '"decoder": "marina"' in src


def test_the_picker_says_absence_is_not_a_paywall():
    js = _read("..", "web", "static", "app.js")
    assert "nothing here is behind a paywall" in js
    assert "open decoder" in js, \
        "the honest reason (decoder + heard recently) must accompany the denial"
