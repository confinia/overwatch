"""Move the API off the Postgres bootstrap superuser (#133).

The app connects as `orbit`, which is the container's bootstrap user and
therefore SUPERUSER — Postgres refuses to downgrade it. So instead of trying,
create a dedicated **owner** role and hand the schema over:

    orbit      bootstrap superuser, maintenance only (dumps, restores)
    orbit_app  owns the schema and every table; NOSUPERUSER, NOBYPASSRLS,
               CREATEROLE (the API provisions per-org roles and policies)

The app keeps working — it still owns what it creates, so its startup DDL,
`CREATE POLICY` for per-org isolation and its GRANTs all succeed — but an SQL
injection can no longer reach superuser territory: no COPY ... PROGRAM, no
reading other databases, no touching roles it does not own.

Idempotent, stdlib only. Run per stack:

    DB_CONTAINER=orbit-poc_db_1 APP_PASSWORD=... python3 orbit-poc/db/pg_app_role.py

The SQL below is also executed (over psycopg2) by api/test_app_role.py, which
then re-runs the API's startup provisioning as {role} — the exact path that
crash-looped when ALTER ROLE restated NOSUPERUSER (#133).
"""
import os
import subprocess
import sys

CONTAINER = os.environ.get("DB_CONTAINER", "orbit-poc_db_1")
APP_ROLE = os.environ.get("APP_ROLE", "orbit_app")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

SQL = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
    CREATE ROLE {role} LOGIN PASSWORD '{pw}';
  ELSE
    ALTER ROLE {role} LOGIN PASSWORD '{pw}';
  END IF;
END $$;
-- least privilege: owns its schema, may provision per-org roles, nothing more
ALTER ROLE {role} NOSUPERUSER NOBYPASSRLS NOCREATEDB CREATEROLE;
ALTER SCHEMA public OWNER TO {role};
DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO {role}', t);
  END LOOP;
  FOR t IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO {role}', t);
  END LOOP;
END $$;
GRANT ALL ON SCHEMA public TO {role};
-- The API also administers the roles it provisions (grafana_ro, per-org
-- org_<hex>). CREATEROLE only governs roles you created yourself, so hand over
-- admin on the ones the superuser created before this migration — otherwise the
-- app fails at startup with "permission denied to alter role".
DO $$
DECLARE r text;
BEGIN
  FOR r IN SELECT rolname FROM pg_roles
           WHERE rolname = 'grafana_ro' OR rolname LIKE 'org\\_%' LOOP
    EXECUTE format('GRANT %I TO {role} WITH ADMIN OPTION', r);
  END LOOP;
END $$;
"""

CHECK = """
SELECT rolsuper, rolbypassrls, rolcreaterole FROM pg_roles WHERE rolname = '{role}';
SELECT count(*) FILTER (WHERE tableowner = '{role}') AS owned,
       count(*) AS total FROM pg_tables WHERE schemaname = 'public';
"""


def psql(sql, user="orbit"):
    p = subprocess.run(["podman", "exec", "-i", CONTAINER,
                        "psql", "-U", user, "-d", "orbit", "-v", "ON_ERROR_STOP=1"],
                       input=sql, capture_output=True, text=True)
    if p.returncode:
        print(p.stderr.strip()[:400], file=sys.stderr)
        sys.exit(1)
    return p.stdout


def main():
    if not APP_PASSWORD:
        raise SystemExit("APP_PASSWORD not set")
    psql(SQL.format(role=APP_ROLE, pw=APP_PASSWORD.replace("'", "''")))
    print(psql(CHECK.format(role=APP_ROLE)))


if __name__ == "__main__":
    main()
