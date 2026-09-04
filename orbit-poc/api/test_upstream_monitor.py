"""Upstream request-rate monitoring: we must be able to SEE how hard we hit
SatNOGS/CelesTrak, so we can prove we stay a considerate consumer (the lesson
of the SatNOGS IPv4 block). Source-invariant guards, like test_passes.py.
"""
import json
import os

HERE = os.path.dirname(__file__)


def _main() -> str:
    return open(os.path.join(HERE, "main.py"), encoding="utf-8").read()


def _ingest() -> str:
    return open(os.path.join(HERE, "..", "ingest", "ingest.py"),
                encoding="utf-8").read()


def test_upstream_request_table_and_ops_grant():
    m = _main()
    assert "CREATE TABLE IF NOT EXISTS upstream_request" in m, \
        "the upstream_request table must exist so the rate is queryable"
    # ops role must be able to read it (the ops dashboard uses ops_ro)
    ops = m[m.index("OPS_TABLES = ("):]
    ops = ops[:ops.index(")")]
    assert '"upstream_request"' in ops, \
        "upstream_request must be in OPS_TABLES so the ops org can chart it"


def test_every_upstream_call_is_recorded():
    ing = _ingest()
    assert "def _timed_get(" in ing and "def _record_request(" in ing, \
        "there must be a recording wrapper for upstream calls"
    # no raw requests.get to SatNOGS/CelesTrak may bypass the wrapper
    import re
    raw = re.findall(r"requests\.get\((?:CELESTRAK_BASE|SATNOGS_BASE|f\"\{SATNOGS)", ing)
    assert not raw, \
        f"{len(raw)} upstream requests.get bypass _timed_get and go unrecorded"


def test_ops_dashboard_charts_the_rate():
    d = json.load(open(os.path.join(
        HERE, "..", "grafana", "ops-dashboards", "upstream-requests.json"),
        encoding="utf-8"))
    assert d["uid"] == "upstream-requests"
    sql = json.dumps(d)
    assert "upstream_request" in sql and "orbitcache-ops" in sql, \
        "the dashboard must query upstream_request via the ops datasource"
    assert "$__timeGroup" in sql, "the rate panel needs a time-grouped query"
