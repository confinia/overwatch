"""Guards a shell trap that makes correct code report failure.

Under `set -o pipefail`, a reader that exits early — `head -N`, `grep -q`,
`grep -m1` — closes the pipe, the upstream command takes SIGPIPE, and the
PIPELINE reports failure even though the data was read and the pattern
matched. The cruel part: an interactive shell has no `pipefail`, so testing
the same command by hand passes, and the failure only appears inside the
systemd unit or CI job that sets it.

Reported by the platform session after it rejected a perfectly good 6.6 MB
database dump on another tenant. Our own audit found six instances, including
two in the nightly test runner.

Use readers that consume their input: `sed -n 1p` instead of `head -1`,
`grep -e PAT >/dev/null` instead of `grep -q PAT`.
"""
import os
import re

HERE = os.path.dirname(__file__)
ROOT = next(p for p in (os.path.join(HERE, "..", ".."), os.path.join(HERE, ".."))
            if os.path.isdir(os.path.join(p, "deploy")))
SEARCH_DIRS = ("deploy", "e2e/side", "selfhost", "batch")

# a pipe into a reader that stops before EOF
EARLY_EXIT = re.compile(r"\|\s*(head\b|grep\b[^|]*\s-\w*q|grep\b[^|]*\s-m\s*\d)")


def _pipefail_scripts():
    for d in SEARCH_DIRS:
        full = os.path.join(ROOT, *d.split("/"))
        if not os.path.isdir(full):
            continue
        for name in sorted(os.listdir(full)):
            if not name.endswith(".sh"):
                continue
            path = os.path.join(full, name)
            text = open(path, encoding="utf-8").read()
            if "pipefail" in text:
                yield os.path.join(d, name), text


def test_there_are_pipefail_scripts_to_check():
    """A guard that silently checks nothing is worse than no guard."""
    assert len(list(_pipefail_scripts())) >= 5


def test_no_pipefail_script_pipes_into_an_early_exit_reader():
    offenders = []
    for rel, text in _pipefail_scripts():
        for n, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if EARLY_EXIT.search(code):
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, (
        "under pipefail these report failure even when they succeed; use "
        "`sed -n 1p` / `grep -e PAT >/dev/null`:\n  " + "\n  ".join(offenders))
