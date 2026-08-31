"""The public repository must not describe users as sales targets.

The code is open so anyone can audit it, which is a selling point right up
until a prospective customer reads that their API key marks them as "a person
we can talk to" and that their usage "is what pricing reads at the
monetization review". Both were real, in committed ops dashboards. The panels
do exactly the same work described in operational terms.

This guards vocabulary, not intent: strategy belongs in local notes, product
facts belong in the repo.
"""
import glob
import json
import os

HERE = os.path.dirname(__file__)
DASH = os.path.join(HERE, "..", "grafana", "ops-dashboards")

# Split so this file does not match its own guard when scanned by a human
# grep, and kept narrow on purpose: "leads to" is ordinary English, a panel
# titled "(leads)" is not.
BANNED = ["monetization" + " review", "person we can " + "talk to",
          "API-key " + "leads", "(" + "leads)", "prospect"]


def _texts(d):
    """Every human-readable string in a dashboard: title and description of
    the board itself and of each panel."""
    yield d.get("title") or ""
    yield d.get("description") or ""
    for p in d.get("panels", []):
        yield p.get("title") or ""
        yield p.get("description") or ""


def test_ops_dashboards_describe_measurements_not_sales():
    for path in sorted(glob.glob(os.path.join(DASH, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        for text in _texts(d):
            low = text.lower()
            for term in BANNED:
                assert term not in low, \
                    f"{os.path.basename(path)}: {term!r} in {text!r}"


def test_the_word_prospect_is_not_used_for_visitors():
    """A person looking at the sandbox is a visitor. Calling them a prospect
    in a public workflow tells them what we think they are for."""
    root = os.path.join(HERE, "..", "..")
    for rel in (".github/workflows/sandbox.yml",
                "orbit-poc/api/test_sandbox_deploy.py"):
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            assert "prospect" not in fh.read().lower(), rel
