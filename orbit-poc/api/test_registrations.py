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
    # The signup pair must be there; the set is no longer closed, because
    # ops-alerts.json is the source of truth and #303 added freshness rules.
    assert {"new-registration", "new-api-key"} <= set(rules)
    assert "registered_user" in rules["new-registration"]["data"][0]["model"]["rawSql"]
    assert "api_key" in rules["new-api-key"]["data"][0]["model"]["rawSql"]
    for r in rules.values():
        assert r["data"][0]["datasourceUid"] == main.OPS_DS_UID
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


def test_new_registration_alert_ignores_the_e2e_bot():   # ops-alert noise
    """The signup e2e creates a disposable e2e-bot+<run>@confinia.io on every
    run; the new-registration alert must exclude it so a push doesn't e-mail a
    firing alert for a test user."""
    rules = {r["uid"]: r for r in main._ops_alert_rules()}
    sql = rules["new-registration"]["data"][0]["model"]["rawSql"]
    assert "e2e-bot%" in sql and "NOT LIKE" in sql


def test_record_login_captures_country():            # #185
    import inspect
    assert "country" in inspect.signature(main._record_login).parameters


def test_alerts_are_config_as_code():                # #201
    """The alert definitions live in the committed ops-alerts.json, not in
    Python: the app must apply exactly the file's rules/contact-point/policy —
    nothing hardcoded in main.py anymore."""
    spec = main._ops_alert_spec()
    applied = {r["uid"]: r for r in main._ops_alert_rules()}
    declared = {r["uid"]: r for r in spec["rules"]}
    # What matters is that Python applies exactly what the file declares — not
    # that the file holds a particular pair of rules.
    assert set(applied) == set(declared)
    assert {"new-registration", "new-api-key"} <= set(declared)
    for uid, r in declared.items():                  # SQL/summary come verbatim
        assert applied[uid]["data"][0]["model"]["rawSql"] == r["sql"]
        if "summary" in r:
            assert r["summary"] in applied[uid]["annotations"]["summary"]
    assert spec["contactPoint"]["name"] == "ops-email"
    assert spec["policy"]["receiver"] == "ops-email"


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


# ---------------------------------------------------------------------------
# Data-freshness alerts (#303) — uptime without data is not up
# ---------------------------------------------------------------------------
def _freshness_rules():
    return [r for r in main._ops_alert_spec()["rules"]
            if r.get("group") == "freshness"]


def _table_and_column(sql):
    """Derive what a freshness rule watches from the rule itself, so a new
    rule is covered without editing this test."""
    # rsplit, and only over the part before HAVING: the freshness SQL contains
    # `extract(epoch FROM now() - max(ts))`, so the FIRST " FROM " is inside an
    # expression, not the table clause.
    head = sql.split(" HAVING")[0]
    table = head.rsplit(" FROM ", 1)[1].strip()
    return table, ("epoch" if "max(epoch)" in sql else "ts")


def test_the_three_freshness_rules_exist():
    uids = {r["uid"] for r in _freshness_rules()}
    assert uids == {"telemetry-stalled", "positions-stalled", "elements-stale"}


def test_a_healthy_system_returns_no_rows():
    """The pipeline is reduce -> threshold(>0) with noDataState OK, so silence
    is how "fresh" is expressed. A rule written as `count(*)` returning 0 would
    also be silent, but could never carry the labels the e-mail needs; HAVING
    gives both."""
    for r in _freshness_rules():
        assert "HAVING" in r["sql"], f"{r['uid']} cannot be silent when fresh"


def test_every_watched_table_is_readable_by_the_alerting_role():
    """The trap this closes: the alerts run as ops_ro, which was NOT granted
    telemetry/position/elements. A denied query yields no data, and with
    noDataState OK that is indistinguishable from healthy — an alert that
    cannot read its own table looks exactly like one that has nothing to
    report."""
    for r in _freshness_rules():
        table, _ = _table_and_column(r["sql"])
        assert table in main.OPS_TABLES, \
            f"{r['uid']} watches {table}, which ops_ro cannot read"


def test_ops_still_cannot_read_tenant_payloads():
    """Widening ops_ro for #303 must not have widened it to private data."""
    assert "tenant_telemetry" not in main.OPS_TABLES


def test_elements_freshness_uses_the_epoch_not_the_fetch_time():
    """Re-downloading the same old element set is not freshness. During the
    2026-08-20 block we kept fetching successfully while the epochs aged."""
    sql = next(r["sql"] for r in _freshness_rules() if r["uid"] == "elements-stale")
    assert "max(epoch)" in sql and "fetched_at" not in sql


def test_freshness_rules_fire_only_when_stale():
    """Rule 13, against a real Postgres: prove the SQL, do not pattern-match
    it. Temp tables shadow the real ones for this session only, so production
    rows are neither read nor written."""
    c = psycopg2.connect(DSN)
    try:
        cur = c.cursor()
        for r in _freshness_rules():
            table, col = _table_and_column(r["sql"])
            cur.execute(f"CREATE TEMP TABLE {table} ({col} timestamptz)")
            c.commit()

            def rows():
                cur.execute(f"SELECT count(*) FROM ({r['sql']}) q")
                n = cur.fetchone()[0]
                c.commit()
                return n

            assert rows() == 1, f"{r['uid']} stayed silent on an EMPTY table"

            cur.execute(f"INSERT INTO {table} VALUES (now())")
            c.commit()
            assert rows() == 0, f"{r['uid']} fired on fresh data"

            cur.execute(f"TRUNCATE {table}")
            cur.execute(f"INSERT INTO {table} VALUES (now() - interval '100 days')")
            c.commit()
            assert rows() == 1, f"{r['uid']} stayed silent on 100-day-old data"

            cur.execute(f"DROP TABLE {table}")      # unshadow for the next rule
            c.commit()
    finally:
        c.close()
