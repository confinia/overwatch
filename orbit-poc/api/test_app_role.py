"""Guards issue #133: the API connects as the least-privilege owner role
(orbit_app), not the bootstrap superuser. Two things must hold, against a real
Postgres:

1. The startup provisioning (_provision_grafana_role, _provision_org_db) runs
   as that non-superuser role. The regression this pins down: the ALTER path
   restated NOSUPERUSER, which only a superuser may do — even to keep the
   attribute off — so the API crash-looped after the DSN switch.
2. The role is actually de-privileged: no superuser, no BYPASSRLS, refused
   pg_authid and COPY ... TO PROGRAM.
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "db"))
import main  # noqa: E402
import pg_app_role  # noqa: E402  (the migration SQL, executed over psycopg2)

DSN = os.environ["DB_DSN"]
APP_ROLE = "orbit_app"
APP_PW = "test-app-role-pw"


@pytest.fixture(scope="module")
def app_conn():
    os.environ["GRAFANA_DB_PASSWORD"] = "test-grafana-ro-pw"
    su = psycopg2.connect(DSN)
    su.autocommit = True
    with su.cursor() as cur:
        # The application schema — tenant_telemetry and friends live in
        # KEYS_SQL, not db/init.sql. This suite used to inherit them from
        # whichever suite happened to run first, an ordering dependency that
        # only surfaced when the gate switched to auto-discovery (#286). A
        # suite that needs a table should create it.
        cur.execute(main.KEYS_SQL)
        # grafana_ro must pre-exist so provisioning below takes the ALTER path
        main._provision_grafana_role(cur)
        migration = pg_app_role.SQL.format(role=APP_ROLE, pw=APP_PW)
        cur.execute(migration)
        cur.execute(migration)                      # must be idempotent
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    conn = psycopg2.connect(dbname=parts["dbname"], host=parts["host"],
                            port=parts.get("port", "5432"),
                            user=APP_ROLE, password=APP_PW)
    yield conn
    conn.close()
    su.close()


def test_app_role_is_deprivileged_owner(app_conn):
    with app_conn.cursor() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls, rolcreaterole "
                    "FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
        assert cur.fetchone() == (False, False, True)
        cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public' "
                    "AND tableowner <> %s", (APP_ROLE,))
        assert cur.fetchone()[0] == 0               # owns every app table
    app_conn.rollback()


def test_grafana_provisioning_runs_as_app_role(app_conn):
    """The crash-loop: ALTER ROLE grafana_ro as a non-superuser (586 restarts)."""
    with app_conn.cursor() as cur:
        main._provision_grafana_role(cur)           # must not raise


def test_ops_provisioning_runs_as_app_role(app_conn):
    """Same trap for the ops-org role (#168): its ALTER path must also work
    without superuser."""
    os.environ["OPS_DB_PASSWORD"] = "test-ops-ro-pw"
    with app_conn.cursor() as cur:
        main._provision_ops_role(cur)               # create path
        main._provision_ops_role(cur)               # ALTER path, as orbit_app


def test_org_provisioning_runs_as_app_role(app_conn):
    """The other startup DDL: per-org RLS role + policy, as the app role."""
    org_id = "00000000-0000-4000-8000-00000000a133"
    with app_conn.cursor() as cur:
        main._provision_org_db(cur, org_id)         # must not raise
        role, _ = main._org_role(org_id)
        cur.execute("SELECT 1 FROM pg_policies WHERE tablename='tenant_telemetry' "
                    "AND policyname = %s", (role + "_pol",))
        assert cur.fetchone()
    app_conn.commit()


def test_superuser_capabilities_refused(app_conn):
    """What #133 removes from an SQL injection's reach."""
    for sql in ("SELECT rolpassword FROM pg_authid LIMIT 1",
                "COPY (SELECT 1) TO PROGRAM 'cat'"):
        with app_conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(sql)
        app_conn.rollback()
