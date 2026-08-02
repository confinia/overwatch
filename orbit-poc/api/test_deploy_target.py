"""Guards issue #141: deploy targets must address the product's own Unix user.

The stack moved to /home/overwatch/projects/overwatch and the old SSH alias was
retired, so a Makefile still pointing at it breaks every deploy silently.
"""
import os
import re

MAKEFILE = next(
    p for p in (os.path.join(os.path.dirname(__file__), "..", "Makefile"),
                os.path.join(os.path.dirname(__file__), "..", "..", "Makefile"))
    if os.path.exists(p))


def test_targets_the_product_user():
    mk = open(MAKEFILE, encoding="utf-8").read()
    assert re.search(r"^VM\s*:=\s*overwatch\s*$", mk, re.M)
    assert re.search(r"^REMOTE\s*:=\s*~/projects/overwatch\s*$", mk, re.M)


def test_retired_alias_is_gone():
    mk = open(MAKEFILE, encoding="utf-8").read()
    assert "confinia-ovh-debian" not in mk


def test_edge_target_refuses_to_act():
    """RULES.md rule 19: the shared platform edge is founder-only."""
    mk = open(MAKEFILE, encoding="utf-8").read()
    edge = mk[mk.index("\nedge:"):]
    edge = edge[:edge.index("\n\n")]
    assert "rsync" not in edge and "deploy-edge.sh" not in edge
    assert "exit 1" in edge
