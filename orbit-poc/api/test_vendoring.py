"""RULES.md rule 25, enforced on EVERY page this time (#520).

Nothing in the render path may come from an origin we do not control:
sovereignty (an EU-hosted product must not need a US CDN to draw), availability
(a CDN outage blanks the view with no deploy of ours), privacy (visitor IPs
handed to a third party) and integrity (a compromised CDN runs JS in our page).

`test_maplibre.py` enforced this on index.html alone, and the 3D spacecraft
view kept loading its entire renderer from unpkg for months — while
test_spacecraft.py asserted that it did. A guard that names one file gives the
appearance of compliance. This one walks the whole static tree.
"""
import os
import re

STATIC = os.path.join(os.path.dirname(__file__), "..", "web", "static")
VENDOR = os.path.join(STATIC, "vendor")

# What counts as the render path: script sources, stylesheets, CSS imports and
# url() references. A plain <a href> to GitHub is a link, not a dependency.
RENDER_REFS = re.compile(
    r"""(?:<script[^>]*\ssrc\s*=\s*["']|"""
    r"""<link[^>]*\shref\s*=\s*["']|"""
    r"""@import\s+(?:url\()?\s*["']?|"""
    r"""url\(\s*["']?)"""
    r"""\s*(https?:)?//([^/"'\s)]+)""", re.IGNORECASE)

# Origins we serve ourselves. Nothing else may appear in a render-path reference.
OUR_ORIGINS = {"overwatch.confinia.io", "staging.overwatch.confinia.io",
               "sandbox.overwatch.confinia.io"}


def _static_files():
    for root, _dirs, files in os.walk(STATIC):
        if os.sep + "vendor" in root:
            continue          # third-party bundles reference nothing we control
        for f in files:
            if f.endswith((".html", ".css", ".js")):
                yield os.path.join(root, f)


def _render_origins(path):
    body = open(path, encoding="utf-8", errors="replace").read()
    return {m.group(2).lower() for m in RENDER_REFS.finditer(body)}


def test_no_page_loads_render_path_assets_from_a_foreign_origin():
    files = list(_static_files())
    assert len(files) >= 5, f"the walk found almost nothing: {files}"
    assert any(f.endswith("spacecraft.html") for f in files), \
        "spacecraft.html is not in the walk — the page this guard exists for"
    foreign = {}
    for p in files:
        bad = _render_origins(p) - OUR_ORIGINS
        if bad:
            foreign[os.path.relpath(p, STATIC)] = sorted(bad)
    assert not foreign, (
        "render-path assets from origins we do not control (rule 25): "
        f"{foreign} — vendor them under static/vendor/ with a VERSION file")


def test_the_pattern_sees_what_it_must_and_ignores_links():
    """Pin the regex: a later 'tidy-up' must not quietly stop matching."""
    for bad, origin in (
        ('<script src="https://unpkg.com/three@0.128.0/build/three.min.js">', "unpkg.com"),
        ("<script defer src='//cdn.jsdelivr.net/npm/x.js'>", "cdn.jsdelivr.net"),
        ('<link rel="stylesheet" href="https://cdnjs.cloudflare.com/a.css">', "cdnjs.cloudflare.com"),
        ("@import url(https://fonts.googleapis.com/css2?family=Inter);", "fonts.googleapis.com"),
        ('@import "https://fonts.googleapis.com/css2";', "fonts.googleapis.com"),
        ("background: url(https://tiles.example.org/t.png)", "tiles.example.org"),
    ):
        m = RENDER_REFS.search(bad)
        assert m and m.group(2).lower() == origin, bad
    for ok in (
        '<a href="https://github.com/confinia/overwatch">source</a>',
        '<script src="/vendor/three/three.min.js"></script>',
        '<link rel="stylesheet" href="/vendor/maplibre/maplibre-gl.css">',
        'fetch("https://api.satnogs.example/x")',       # data, not render path
        "// see https://unpkg.com/three for upstream",   # prose
    ):
        assert not RENDER_REFS.search(ok), ok


def test_three_is_vendored_with_the_version_beside_it():
    """Mirror of the MapLibre check. OrbitControls is a classic script that
    expects the THREE global, so it must load after three.min.js and both must
    come from our origin. A truncated download or an error page would still
    'exist', hence the size floors."""
    page = open(os.path.join(STATIC, "spacecraft.html"), encoding="utf-8").read()
    assert "/vendor/three/three.min.js" in page
    assert "/vendor/three/OrbitControls.js" in page
    assert page.index("/vendor/three/three.min.js") < page.index("/vendor/three/OrbitControls.js"), \
        "OrbitControls needs the THREE global: three.min.js must come first"
    assert "unpkg.com" not in page and "three@0.128" not in page
    vd = os.path.join(VENDOR, "three")
    for f, min_kb in (("three.min.js", 500), ("OrbitControls.js", 20), ("LICENSE", 1)):
        p = os.path.join(vd, f)
        assert os.path.exists(p), f"missing vendored {f}"
        assert os.path.getsize(p) > min_kb * 1024, f"{f} looks truncated"
    version = open(os.path.join(vd, "VERSION"), encoding="utf-8").read()
    assert "0.128.0" in version
    assert "sha256" in version, "VERSION must carry the checksums it was vendored with"
    assert "MIT" in open(os.path.join(vd, "LICENSE"), encoding="utf-8").read()
