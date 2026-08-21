"""Guards issue #307: the telemetry-import guide, and that it stays true.

Documentation rots differently from code — silently, and only for the reader.
The limits here are the part most likely to drift, so they are read out of the
enforcing code and compared with the page rather than trusted.
"""
import os
import re
import subprocess

HERE = os.path.dirname(__file__)
ROOT = next(p for p in (os.path.join(HERE, "..", ".."), os.path.join(HERE, ".."))
            if os.path.isdir(os.path.join(p, "orbit-poc", "web"))
            or os.path.isdir(os.path.join(p, "web")))
WEB = (os.path.join(ROOT, "orbit-poc", "web", "static")
       if os.path.isdir(os.path.join(ROOT, "orbit-poc", "web")) else
       os.path.join(ROOT, "web", "static"))
GUIDE = os.path.join(WEB, "telemetry.html")
MAIN = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()


def _guide():
    return open(GUIDE, encoding="utf-8").read()


def test_the_guide_exists_and_is_served():
    """Static pages are served by filename, so existing IS being served."""
    assert os.path.isfile(GUIDE)
    html = _guide()
    assert "<title>" in html and "telemetry" in html.lower()


def test_the_documented_limits_match_what_the_api_enforces():
    """The numbers a reader will size their ground segment against. If the code
    changes and the page does not, the page becomes a lie — so read both."""
    batch = int(re.search(r"len\(body\.points\) > (\d+)", MAIN).group(1))
    daily = int(re.search(r"max_points_day bigint NOT NULL DEFAULT (\d+)", MAIN).group(1))
    html = _guide()
    assert str(batch) in html, f"guide does not state the real batch cap ({batch})"
    assert str(daily) in html, f"guide does not state the real daily quota ({daily})"


def test_every_error_the_push_path_can_return_is_explained():
    """401/403/413/429 each have a different fix; a reader hitting one should
    not have to guess."""
    html = _guide()
    for code in ("401", "403", "413", "429"):
        assert f"<code>{code}</code>" in html, f"{code} is not documented"


def test_the_canonical_field_names_are_documented():
    """Using them is what makes the fleet-wide panels fill in by themselves —
    knowledge we have and would otherwise not share."""
    html = _guide()
    for field in ("battery_v", "battery_i", "battery_pct"):
        assert field in html, field


def test_the_guide_is_reachable_from_where_people_look():
    assert '/telemetry.html' in MAIN, "the API landing does not link the guide"
    pro = open(os.path.join(WEB, "pro.html"), encoding="utf-8").read()
    assert "/telemetry.html" in pro, "the pricing page does not link the guide"


def test_public_docs_never_link_a_file_that_is_not_published():
    """FAQ.md is public and linked PRO.md, TENANT.md and SPECIFICATIONS.md —
    all local-only under rule 16, so all three 404'd on GitHub for every
    reader. A public document must not point at something nobody can open."""
    try:
        tracked = set(subprocess.run(["git", "ls-files"], cwd=ROOT,
                                     capture_output=True, text=True).stdout.split())
    except FileNotFoundError:             # no git in this image
        return
    if not tracked:                       # not a git checkout (copied tree)
        return
    broken = []
    for doc in ("FAQ.md", "README.md"):
        path = os.path.join(ROOT, doc)
        if not os.path.isfile(path):
            continue
        for m in re.finditer(r"\]\(([A-Za-z0-9_./-]+\.md)\)",
                             open(path, encoding="utf-8").read()):
            if m.group(1) not in tracked:
                broken.append(f"{doc} -> {m.group(1)}")
    assert not broken, f"public docs link unpublished files: {broken}"
