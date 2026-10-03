"""Guards #542: the test runner cannot leave containers behind, and could never
sweep anything that is not its own.

The trap in run-tests.sh only fires on a normal exit; a cancelled workflow or a
killed client leaves the throwaway database container running, and `--rm`
does nothing once the client is gone — a neighbour's CI container ran 26
hours that way. The fix has three legs, and each is pinned here: the
containers carry a label and a lifetime of their own, the sweep selects by
that label and nothing else, and what it swept is a row on the Deploy
pipeline board (rule 34).
"""
import json
import os
import re

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..", "..")
LABEL = "io.confinia.overwatch.test"


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _runs(sh):
    """Every `podman run` the runner issues, each as one logical line."""
    joined = sh.replace("\\\n", " ")
    return [l for l in joined.splitlines() if re.search(r"\bpodman run\b", l)]


def test_everything_the_runner_starts_is_labelled_and_bounded():
    sh = _read("deploy", "run-tests.sh")
    runs = _runs(sh)
    assert len(runs) == 2, f"expected the database and the pytest run, found {len(runs)}: {runs}"
    for r in runs:
        assert f'--label "$LABEL=1"' in r, f"unlabelled container: {r[:80]}"
        assert re.search(r"--timeout \d+", r), f"no lifetime of its own: {r[:80]}"
    assert f"LABEL={LABEL}" in sh
    assert 'podman network create --label "$LABEL=1"' in sh, "the network must be sweepable too"


def test_the_sweep_selects_by_our_label_and_never_by_name():
    sh = _read("deploy", "run-tests.sh")
    fn = sh.split("sweep_strays(){", 1)
    assert len(fn) == 2, "no sweep — a killed run leaves its database behind"
    # Directives only: the function's own comments explain what NOT to do
    # (`date -d`, selecting by name), and the first version of this test
    # failed on its own explanation.
    body = "\n".join(l.split("#", 1)[0] for l in fn[1].split("\n}", 1)[0].splitlines())
    assert '--filter "label=$LABEL=1"' in body, "the sweep must select by OUR label"
    assert "--filter name=" not in body and "--filter \"name=" not in body, \
        "selecting by name is how a neighbour's container got killed (#542)"
    assert "-gt 7200" in body, "a concurrent run must be younger than the cut-off"
    assert "podman rm -f" in body
    # The age must come from podman as an epoch. `date -d` on `{{.Created}}`
    # fails on the "+0000 UTC" suffix, and the first version skipped every
    # container silently on that: a planted stray survived with swept=0.
    assert "{{.Created.Unix}}" in body, "read the age as an epoch from podman"
    assert "date -d" not in body, "GNU date cannot parse podman's {{.Created}}"
    assert "cannot read age" in body, "an unreadable age must be said, not skipped"
    assert "sweep_strays\n" in sh.split("podman run", 1)[0], \
        "the sweep must run BEFORE anything is started"


def test_what_was_swept_is_recorded_and_visible():
    sh = _read("deploy", "run-tests.sh")
    assert "CREATE TABLE IF NOT EXISTS test_sweep" in sh, "bootstrap window, as deploy_event"
    assert "INSERT INTO test_sweep" in sh
    assert "ON_ERROR_STOP" in sh
    # schema truth + the ops grant, or the board renders empty (#320)
    src = _read("orbit-poc", "api", "main.py")
    assert "CREATE TABLE IF NOT EXISTS test_sweep" in src
    tables = src[src.index("OPS_TABLES = ("):][:900]
    assert '"test_sweep"' in tables, "ops_ro cannot read it -> empty panel, not an error"
    board = json.loads(_read("orbit-poc", "grafana", "ops-dashboards", "deploys.json"))
    hits = [p for p in board["panels"]
            if any("test_sweep" in t.get("rawSql", "") for t in p.get("targets", []))]
    assert hits, "no panel reads test_sweep — the sweep is invisible (rule 34)"
    for p in hits:
        for t in p["targets"]:
            assert t["datasource"]["uid"] == "orbitcache-ops"
