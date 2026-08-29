"""Guards issue #382: the deploy pipeline must be visible in the ops org.

The only alternative is reading GitHub Actions runs by hand — which is how a
green-everything production ran a 41-hour-old ingest (#305) and how #320/#324
sat merged and undeployed for a day. The gap between staged and promoted is
rule 26 made visible.
"""
import json
import os

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..", "..")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_the_workflow_records_both_phases():
    wf = _read(".github", "workflows", "deploy.yml")
    assert wf.count("record-deploy-event.sh") == 2, \
        "stage and promote must each write their row"
    assert "stage \"$SHA\"" in wf
    assert "promote '$SHA'" in wf


def test_the_recorder_bootstraps_the_schema_and_rejects_junk():
    # Schema truth is main.py's startup DDL; the recorder repeats the CREATE
    # because the very first stage row lands before any new api has booted.
    sh = _read("deploy", "record-deploy-event.sh")
    assert "CREATE TABLE IF NOT EXISTS deploy_event" in sh, \
        "first run must need no migration step"
    ddl = _read("orbit-poc", "api", "main.py")
    assert "CREATE TABLE IF NOT EXISTS deploy_event" in ddl, \
        "the table must exist after a plain boot, or the ops grant never lands"
    assert "stage|promote" in sh, "phase must be validated"
    assert "ON_ERROR_STOP" in sh, "a failed insert must fail the step, not vanish"


def test_ops_ro_gets_the_table():
    src = _read("orbit-poc", "api", "main.py")
    tables = src[src.index("OPS_TABLES = ("):][:600]
    assert '"deploy_event"' in tables, \
        "a table ops_ro cannot read renders as an empty board, not an error"


def test_the_board_is_provisioned_and_reads_the_right_things():
    board = json.loads(_read("orbit-poc", "grafana", "ops-dashboards", "deploys.json"))
    assert board["uid"] == "deploys"
    sql = json.dumps(board)
    assert "deploy_event" in sql
    assert "Unpromoted candidate" in sql, "the gap is the point of the board"
    for panel in board["panels"]:
        for t in panel.get("targets", []):
            assert t["datasource"]["uid"] == "orbitcache-ops", \
                "a null datasource resolves to the denied default (#320)"
