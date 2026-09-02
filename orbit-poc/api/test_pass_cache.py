"""#433: the pass-detail cache must not freeze a pass that is still receiving
late SatNOGS uploads.

A pass detail was sent with Cache-Control max-age=3600 under the belief that
"a completed pass never changes". SatNOGS uploads lag by hours, so a phone
that opened a just-ended pass cached 3 frames and kept showing 3 while the
station row, fetched fresh, already read 16. The fix caches hard only once
the late-upload window (a day) has passed; a young pass is kept fresh.

Source-invariant guard, in the style of test_passes.py — no live HTTP, no DB.
"""
import os
import re


def _pass_detail_source() -> str:
    src = open(os.path.join(os.path.dirname(__file__), "main.py"),
               encoding="utf-8").read()
    start = src.index("def station_pass_detail(")
    end = src.index("\ndef ", start + 1)
    return src[start:end]


def test_a_recent_pass_is_not_hard_cached():
    fn = _pass_detail_source()
    # the old unconditional hour-long cache must be gone
    assert 'max-age=3600"' not in fn, \
        "a flat max-age=3600 freezes passes still gaining late uploads (#433)"
    # cache is now decided from the pass age against the ~1-day upload window
    assert "86400" in fn, "settled passes should cache hard (a day)"
    assert re.search(r"p_los\).total_seconds\(\)\s*>\s*86400", fn), \
        "the freeze decision must compare the pass age to the upload window"


def test_young_pass_gets_a_short_cache_not_none():
    fn = _pass_detail_source()
    # a short positive max-age (not no-store): the detail is still cacheable
    # for seconds, it just must not outlive the late uploads
    m = re.search(r'max-age=(\d+)"\s*if settled\s*\n?\s*else "public, max-age=(\d+)"', fn) \
        or re.search(r'"public, max-age=(\d+)".*?else.*?"public, max-age=(\d+)"',
                     fn, re.S)
    assert m, "expected a settled/young branch on Cache-Control"
    settled, young = int(m.group(1)), int(m.group(2))
    assert settled >= 86400 > young >= 1, \
        f"settled must cache long and young short (got {settled} / {young})"
