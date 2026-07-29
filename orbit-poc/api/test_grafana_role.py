"""Guards issue #129 (critical): Grafana's datasource proxy runs arbitrary SQL
for any caller — including the anonymous Viewer the public embeds need — so the
DATABASE ROLE is the security boundary. This asserts the role the datasource
uses can read the public tables and NOTHING else, against a real Postgres.
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402

DSN = os.environ["DB_DSN"]
PW = "test-grafana-ro-pw"
SENSITIVE = ("tenant_telemetry", "api_key", "org_token", "organization",
             "billing_event", "org_usage", "visitor_daily", "tenant")


@pytest.fixture(scope="module")
def ro_conn():
    os.environ["GRAFANA_DB_PASSWORD"] = PW
    admin = psycopg2.connect(DSN)
    with admin, admin.cursor() as cur:
        main._provision_grafana_role(cur)
    # connect AS the datasource role, exactly like Grafana does
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    conn = psycopg2.connect(dbname=parts["dbname"], host=parts["host"],
                            port=parts.get("port", "5432"),
                            user=main.GRAFANA_ROLE, password=PW)
    yield conn
    conn.close()
    admin.close()


def test_role_is_not_privileged(ro_conn):
    with ro_conn.cursor() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                    "FROM pg_roles WHERE rolname = %s", (main.GRAFANA_ROLE,))
        assert cur.fetchone() == (False, False, False, False)


def test_role_can_read_public_tables(ro_conn):
    for t in main.GRAFANA_PUBLIC_TABLES:
        with ro_conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {t}")     # must not raise
            assert cur.fetchone()[0] >= 0
        ro_conn.commit()


def test_role_cannot_read_private_tables(ro_conn):
    """The actual vulnerability: private tenant telemetry, keys, billing."""
    for t in SENSITIVE:
        with ro_conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(f"SELECT * FROM {t} LIMIT 1")
        ro_conn.rollback()


def test_role_cannot_write(ro_conn):
    with ro_conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM satellite")
    ro_conn.rollback()


def test_datasource_provisioning_uses_the_role():
    p = os.path.join(os.path.dirname(__file__), "..", "grafana",
                     "provisioning", "datasources", "postgres.yml")
    y = open(p, encoding="utf-8").read()
    assert f"user: {main.GRAFANA_ROLE}" in y
    assert "$__env{GRAFANA_DB_PASSWORD}" in y
    assert "user: orbit" not in y                 # never the privileged role
