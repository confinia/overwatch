"""Guards issue #172: registrations become visible and actionable — a
registry table fed by logins and the Keycloak backfill, an ops board, and
signup alert rules mailing OPS_ALERT_EMAIL.
"""
import json
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402

DSN = os.environ["DB_DSN"]
HERE = os.path.dirname(__file__)
BOARD = os.path.join(HERE, "..", "grafana", "ops-dashboards", "registrations.json")


@pytest.fixture(scope="module")
def conn():
    c = psycopg2.connect(DSN)
    with c, c.cursor() as cur:
        cur.execute(main.KEYS_SQL)
    yield c
    c.close()


def test_login_upsert_is_idempotent(conn):
    """Second login: last_login advances, first_seen must not move."""
    sub = str(uuid.uuid4())
    with conn, conn.cursor() as cur:
        main._record_login(cur, sub, "a@example.org", "A")
        cur.execute("SELECT first_seen, last_login FROM registered_user "
                    "WHERE sub = %s::uuid", (sub,))
        first_seen1, last_login1 = cur.fetchone()
        assert last_login1 is not None
        cur.execute("UPDATE registered_user SET last_login = last_login - "
                    "interval '1 hour' WHERE sub = %s::uuid", (sub,))
        main._record_login(cur, sub, "a@example.org", "A")
        cur.execute("SELECT first_seen, last_login FROM registered_user "
                    "WHERE sub = %s::uuid", (sub,))
        first_seen2, last_login2 = cur.fetchone()
    assert first_seen2 == first_seen1
    assert last_login2 > last_login1 - __import__("datetime").timedelta(hours=1)


def test_backfill_mode_keeps_earliest_date_and_no_login(conn):
    """The Keycloak sweep records the true signup date and must NOT count as
    a login; a later real login must not overwrite the early first_seen."""
    sub = str(uuid.uuid4())
    with conn, conn.cursor() as cur:
        cur.execute("SELECT now() - interval '30 days'")
        created = cur.fetchone()[0]
        main._record_login(cur, sub, "b@example.org", "B",
                          first_seen=created, login=False)
        cur.execute("SELECT first_seen, last_login FROM registered_user "
                    "WHERE sub = %s::uuid", (sub,))
        first_seen, last_login = cur.fetchone()
        assert last_login is None
        assert abs((first_seen - created).total_seconds()) < 1
        main._record_login(cur, sub, "b@example.org", "B")   # real login later
        cur.execute("SELECT first_seen, last_login FROM registered_user "
                    "WHERE sub = %s::uuid", (sub,))
        first_seen2, last_login2 = cur.fetchone()
    assert first_seen2 == first_seen                  # backfilled date wins
    assert last_login2 is not None


def test_alert_rules_watch_the_right_tables():
    rules = {r["uid"]: r for r in main._ops_alert_rules()}
    assert set(rules) == {"new-registration", "new-api-key"}
    assert "registered_user" in rules["new-registration"]["data"][0]["model"]["rawSql"]
    assert "api_key" in rules["new-api-key"]["data"][0]["model"]["rawSql"]
    for r in rules.values():
        assert r["data"][0]["datasourceUid"] == "orbitcache"
        assert r["folderUID"] == "ops-alerts"
        assert r["condition"] == "C"


def test_alert_email_defaults_to_contact():
    assert main.OPS_ALERT_EMAIL == "contact@confinia.io"


def test_new_registration_alert_is_personalized():   # #185
    """The e-mail names who signed up, their org and country — not just a count."""
    rules = {r["uid"]: r for r in main._ops_alert_rules()}
    sql = rules["new-registration"]["data"][0]["model"]["rawSql"]
    for frag in ("ru.email", "ru.name", "org_user", "organization", "ru.country"):
        assert frag in sql, frag
    summary = rules["new-registration"]["annotations"]["summary"]
    for frag in ("$labels.name", "$labels.email", "$labels.org", "$labels.country"):
        assert frag in summary, frag


def test_record_login_captures_country():            # #185
    import inspect
    assert "country" in inspect.signature(main._record_login).parameters


def test_alerts_are_env_labelled():                  # #187
    """Every ops alert names its environment (prod/staging/sandbox) so a staging
    alert is never mistaken for prod. The test env has no PUBLIC_BASE -> prod."""
    for r in main._ops_alert_rules():
        assert r["labels"]["env"] == "production"
        assert r["annotations"]["summary"].startswith("[production]")


def test_registrations_board_exists_in_the_ops_org_dir():
    d = json.load(open(BOARD, encoding="utf-8"))
    assert d["uid"] == "registrations"
    sql = json.dumps(d)
    assert "registered_user" in sql
    assert "ops" in d["tags"]


def test_registry_is_granted_to_ops_ro():
    """The board reads it through the ops datasource, so the grant must be in
    the OPS_TABLES allow-list (which test_ops_role verifies against Postgres)."""
    assert "registered_user" in main.OPS_TABLES
