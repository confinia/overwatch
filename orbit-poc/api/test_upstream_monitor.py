"""We must be able to SEE our real SatNOGS request rate, and no caller may
bypass the shared limiter — the lesson of two blocks. The egress gateway (#449)
is the single door: it records the true footprint (cache hits excluded) and is
the only thing that can reach db.satnogs.org. Source-invariant guards, like
test_passes.py.
"""
import json
import os

HERE = os.path.dirname(__file__)


def _read(*parts):
    return open(os.path.join(HERE, *parts), encoding="utf-8").read()


def test_upstream_request_table_and_ops_grant():
    m = _read("main.py")
    assert "CREATE TABLE IF NOT EXISTS upstream_request" in m, \
        "the upstream_request table must exist so the rate is queryable"
    ops = m[m.index("OPS_TABLES = ("):]
    ops = ops[:ops.index(")")]
    assert '"upstream_request"' in ops, \
        "upstream_request must be in OPS_TABLES so the ops org can chart it"


def test_gateway_records_the_true_upstream_rate():
    g = _read("..", "gateway", "gateway.py")
    # the real footprint is written to upstream_request as source 'satnogs'
    assert "INSERT INTO upstream_request" in g and "'satnogs'" in g, \
        "the gateway must record every real SatNOGS request"
    # recorded in a finally, so a failed or timed-out attempt still counts
    assert "finally:" in g and "self._record(" in g, \
        "even a refused or timed-out attempt must be recorded"
    # a cache hit returns before the upstream call, so it is never recorded as
    # load — otherwise the chart would overstate how hard we hit SatNOGS
    assert 'return hit[1], hit[2], hit[3], "HIT"' in g, \
        "a cache hit must short-circuit before the upstream call and its record"


def test_nothing_can_bypass_the_shared_limiter():
    # the ingest reaches SatNOGS only through SATNOGS_BASE (env), never a
    # hardcoded host, so cloud can route it through the gateway
    ing = _read("..", "ingest", "ingest.py")
    assert 'os.environ.get("SATNOGS_BASE"' in ing, \
        "SATNOGS_BASE must be overridable so cloud routes it through the gateway"
    compose = _read("..", "docker-compose.yml")
    assert "satnogs-gateway" in compose and "satnogs-gateway:8088" in compose, \
        "the ingest must be pointed at the gateway"
    # sole-egress: the provider is blackholed for the ingest, so a stray direct
    # call fails fast instead of overspending the shared per-user budget
    assert "db.satnogs.org:127.0.0.1" in compose, \
        "db.satnogs.org must be blackholed so the gateway is the only door"


def test_ops_dashboard_charts_the_rate():
    d = json.load(open(os.path.join(
        HERE, "..", "grafana", "ops-dashboards", "upstream-requests.json"),
        encoding="utf-8"))
    assert d["uid"] == "upstream-requests"
    sql = json.dumps(d)
    assert "upstream_request" in sql and "orbitcache-ops" in sql, \
        "the dashboard must query upstream_request via the ops datasource"
    assert "$__timeGroup" in sql, "the rate panel needs a time-grouped query"
