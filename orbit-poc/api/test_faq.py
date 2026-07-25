"""End-user FAQ test (#56): the public FAQ exists and answers the two
questions the issue requires — why the fleet is small (the telemetry-only /
decoder / freshness reasons) and what tracking more would take (more decoders,
sourced from satnogs-decoders). A structural guard so the answers can't quietly
disappear or be gutted. FAQ.md is copied next to the runner by run-tests.sh."""
import os

# Runner copies FAQ.md to /tmp (one level above the api/ working dir); locally
# it sits two levels up at the repo root. Accept whichever is present.
_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "FAQ.md"),        # gate runner
    os.path.join(os.path.dirname(__file__), "..", "..", "FAQ.md"),  # repo root
]


def _faq_text():
    for p in _CANDIDATES:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
    raise AssertionError("FAQ.md not found in any known location")


def test_faq_explains_the_satellite_limit():
    t = _faq_text().lower()
    # the "why ~23" answer must name its three real reasons
    assert "telemetry-only" in t or "telemetry only" in t
    assert "decod" in t          # decoder / decodable
    assert "7 days" in t or "7-day" in t or "freshness" in t


def test_faq_explains_how_to_track_more():
    t = _faq_text().lower()
    # the "what would be missing" answer must point at more decoders + source
    assert ".ksy" in t or "kaitai" in t
    assert "satnogs-decoders" in t


def test_faq_covers_expected_questions():
    t = _faq_text()
    # a real FAQ, not a stub: several distinct question headings
    assert t.count("\n## ") >= 4
