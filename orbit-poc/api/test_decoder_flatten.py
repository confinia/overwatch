"""Guards issue #171: the decoder flattener must not truncate real payloads.

The old cutoff (depth > 4) stopped exactly one level above where AX.25-wrapped
decoders keep their telemetry, in per-subsystem structs. SAL-E's cp16 frames
yielded 7 fields instead of 121 and ingest logged "19/25 frames
decoded+stored" while storing almost nothing — silent data loss across every
satellite whose decoder nests that way (ISS, LAPAN-A2, Sharjahsat-1, …).

These tests use stand-in objects shaped like kaitai structs, so they run in the
CI gate with no satnogs-decoders installed. The real-frame fixture test runs
wherever the decoders are available (the ingest container).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingest"))
from flatten import MAX_DEPTH, flatten_decoded  # noqa: E402

HERE = os.path.dirname(__file__)
FIXTURE = os.path.join(HERE, "..", "ingest", "fixtures", "sal-e-cp16.hex")


class Node:
    """Stand-in for a kaitai struct: descended into like the real thing."""
    __module__ = "satnogsdecoders.decoder.fake"

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _nested(depth, leaf_name="temp_c", leaf=42):
    """A chain `l0 -> l1 -> ... -> leaf`, mimicking ax25 -> payload -> info ->
    contents -> subsystem -> value."""
    node = Node(**{leaf_name: leaf})
    for i in reversed(range(depth)):
        node = Node(**{f"l{i}": node})
    return node


def test_the_real_payload_depth_is_reached():
    """cp16 keeps telemetry at depth 5-6; anything that stops at 4 loses it."""
    assert MAX_DEPTH >= 6, "below 6 the subsystem structs are cut off again"
    out = flatten_decoded(_nested(5))
    assert out == {"l0_l1_l2_l3_l4_temp_c": 42}, out


def test_the_old_cutoff_would_have_failed_this():
    """Pins the regression itself: with the historic bound the same payload
    comes back empty."""
    assert flatten_decoded(_nested(5), max_depth=4) == {}


def test_recursion_stays_bounded():
    """The bound exists to stop runaway walks — deeper than MAX_DEPTH is
    dropped rather than followed forever."""
    assert flatten_decoded(_nested(MAX_DEPTH + 3)) == {}


def test_cycles_cannot_loop():
    """A decoder exposing a public back-reference must not hang the ingest."""
    a = Node(value=1)
    b = Node(value=2, back=a)
    a.forward = b
    out = flatten_decoded(a)
    assert out["value"] == 1
    assert out["forward_value"] == 2


def test_booleans_are_not_telemetry():
    out = flatten_decoded(Node(flag=True, count=3))
    assert out == {"count": 3}


def test_unreadable_properties_do_not_abort_the_frame():
    """Kaitai lazy properties can raise; one bad field must not cost the rest."""
    class Cranky(Node):
        @property
        def broken(self):
            raise ValueError("lazy parse failed")

    out = flatten_decoded(Cranky(good=7))
    assert out == {"good": 7}


def test_the_fixture_frame_is_committed():
    """A real SAL-E cp16 frame, so the decode can be re-verified for good."""
    hexf = open(FIXTURE, encoding="utf-8").read().strip()
    assert len(hexf) > 200 and all(c in "0123456789ABCDEFabcdef" for c in hexf)


def test_real_frame_decodes_past_the_old_cutoff():
    """Runs where satnogs-decoders exists (the ingest container): the captured
    frame must yield far more than the 7 fields the old bound allowed."""
    try:
        import importlib
        mod = importlib.import_module("satnogsdecoders.decoder.cp16")
    except ImportError:
        import pytest
        pytest.skip("satnogs-decoders not installed here")
    hexf = open(FIXTURE, encoding="utf-8").read().strip()
    obj = mod.Cp16.from_bytes(bytes.fromhex(hexf))
    assert len(flatten_decoded(obj, max_depth=4)) < 10      # the bug
    assert len(flatten_decoded(obj)) > 100                  # the fix


def test_the_candidate_sweep_uses_the_same_depth_bound():   # #171 / fleet growth
    """batch/sweep_full.py scores which satellites are worth adding by how many
    fields decode. It carries its own copy of the flattener (its container
    mounts only batch/), so if that copy keeps the old cutoff the sweep
    under-counts exactly the AX.25-nested satellites — and we would select
    against the good candidates while believing we were selecting for them."""
    sweep = os.path.join(HERE, "..", "..", "batch", "sweep_full.py")
    if not os.path.exists(sweep):
        sweep = os.path.join(HERE, "..", "batch", "sweep_full.py")
    src = open(sweep, encoding="utf-8").read()
    assert "MAX_DEPTH = 8" in src, "the sweep's bound drifted from flatten.py"
    assert "depth > MAX_DEPTH" in src and "depth > 4" not in src
    assert "seen" in src, "no cycle guard in the sweep's copy"
