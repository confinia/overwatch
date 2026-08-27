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
    assert uids == {"telemetry-stalled", "positions-stalled", "elements-stale"}, \
        ("freshness means 'nothing has arrived'. provider-refused is the "
         "opposite shape — an empty table there means nobody refused us — so "
         "it lives in its own group (#357)")


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


# ---------------------------------------------------------------------------
# The api must talk to ITS OWN Grafana, and say so when it cannot (#328)
# ---------------------------------------------------------------------------
def test_colour_stacks_pin_the_production_grafana():
    """`grafana` is not a unique name. The colour stacks join ovw2_default for
    the Keycloak path, and the sandbox and staging Grafanas are attached there
    too — each registering that same alias. Production resolved to one of THEM,
    so every provisioning write authenticated against another environment,
    got 401, and was thrown away. Pin the project-qualified name."""
    for colour in ("blue", "green"):
        f = os.path.join(HERE, "..", f"docker-compose.{colour}.yml")
        body = open(f, encoding="utf-8").read()
        line = next((l for l in body.splitlines() if "GF_URL:" in l), None)
        assert line, f"{colour} does not pin GF_URL"
        assert "orbit-poc_grafana_1" in line, \
            f"{colour} GF_URL must name the production Grafana: {line.strip()}"
        assert "//grafana:" not in line, \
            f"{colour} still uses the ambiguous alias: {line.strip()}"


def test_a_failed_grafana_provision_is_reported():
    """The 401s produced no log line at all, so alerting looked configured for
    as long as nobody checked Grafana itself. 409 stays quiet: it means the
    object is already there."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert "def _gf_ok(" in src
    fn = src[src.index("def _gf_ok("):src.index("def _provision_ops_alerts(")]
    assert "409" in fn, "an existing object is not a failure"
    for code in ("200", "201"):
        assert code in fn
    body = src[src.index("def _provision_ops_alerts("):]
    body = body[:body.index("\ndef ", 10)]
    assert body.count("_gf_ok(") >= 4, \
        "contact point, policy, folder and each rule must all be checked"


# ---------------------------------------------------------------------------
# Ops alerting is authoritative, not additive (#330)
# ---------------------------------------------------------------------------
def test_our_rules_are_pruned_from_other_orgs():
    """The signup rules had accumulated in org 1 as well — Grafana's Main Org,
    which serves the public dashboards anonymously — and both orgs routed to
    contact@confinia.io, so every signup sent two identical e-mails."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert "def _prune_alerts_outside_ops(" in src
    fn = src[src.index("def _prune_alerts_outside_ops("):
             src.index("def _drop_placeholder_contact_point(")]
    assert "_ops_alert_spec()" in fn, \
        "the uids to clean up must come from the file, not a second list"
    assert "if oid == gorg:" in fn and "continue" in fn, \
        "the ops org itself must never be pruned"
    assert "DELETE" in fn


def test_pruning_never_touches_another_owners_rules():
    """We delete only uids we declare. Anything else in another org belongs to
    someone else."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    fn = src[src.index("def _prune_alerts_outside_ops("):
             src.index("def _drop_placeholder_contact_point(")]
    assert 'rule.get("uid") in ours' in fn, \
        "deletion must be gated on the uid being one of ours"


def test_the_placeholder_contact_point_is_dropped_only_while_placeholder():
    """A live e-mail contact point aimed at <example@email.com> is one routing
    mistake from mailing a stranger. But once somebody puts a real address
    there it is theirs, not ours to delete."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    fn = src[src.index("def _drop_placeholder_contact_point("):
             src.index("def _provision_ops_alerts(")]
    assert "PLACEHOLDER_ADDRESSES" in fn
    assert 'c.get("name") == "email receiver"' in fn, \
        "only the built-in default is a candidate"
    assert 'if not c.get("uid"):' in fn, \
        "the built-in default has no uid and cannot be deleted here (#332)"
    assert "example@email.com" in src


def test_cleanup_runs_as_part_of_provisioning():
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    body = src[src.index("def _provision_ops_alerts("):]
    body = body[:body.index("\ndef ", 10)]
    assert "_prune_alerts_outside_ops(gorg)" in body
    assert "_drop_placeholder_contact_point(gorg)" in body


def test_cleanup_failures_are_reported():
    """Same rule as #328: a cleanup that silently fails leaves duplicate mail
    flowing while the code claims to have stopped it."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    for name in ("_prune_alerts_outside_ops", "_drop_placeholder_contact_point"):
        fn = src[src.index(f"def {name}("):]
        fn = fn[:fn.index("\ndef ", 10)]
        assert "_gf_ok(" in fn, f"{name} discards failures"


def test_an_existing_folder_is_not_reported_as_a_failure():   # #332
    """Grafana 11 answers 412 version-mismatch to a repeat folder create. That
    is a no-op, and it was logging GRAFANA PROVISIONING FAILED on every single
    api start — the fastest way to make a real failure unreadable."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    body = src[src.index("def _provision_ops_alerts("):]
    body = body[:body.index("\ndef ", 10)]
    folder = body[body.index("alerts-folder") - 200:body.index("alerts-folder") + 40]
    assert "also_ok=(412,)" in folder, "the folder call must tolerate 412"


def test_the_412_tolerance_is_not_global():   # #332
    """412 means something real on other endpoints; only the folder call gets
    to ignore it."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    fn = src[src.index("def _gf_ok("):]
    fn = fn[:fn.index("\ndef ", 10)]
    assert "412" not in fn.split('"""')[-1], \
        "412 must come from the caller, not be baked into _gf_ok"
    assert src.count("also_ok=(412,)") == 1, \
        "exactly one call may tolerate 412"


# ---------------------------------------------------------------------------
# An alert that mails every hour is an alert nobody reads (#341)
# ---------------------------------------------------------------------------
def test_a_freshness_rule_returns_value_and_nothing_else():   # #352
    """A rule's returned columns ARE its alert identity. #341 dropped the age
    columns but kept `newest` — which still moves when the system is SLOW
    rather than STOPPED: positions kept trickling, `newest` advanced, and every
    evaluation became a brand-new alert. Detail belongs on the dashboard."""
    for r in _freshness_rules():
        select = r["sql"][:r["sql"].index(" FROM ")]
        assert select.strip() == "SELECT 1 AS value", (
            f"{r['uid']} returns more than a constant: {select.strip()!r}")


def test_no_freshness_rule_returns_a_moving_column():
    """Labels are an alert instance's identity. `hours_stale` incremented on
    every evaluation, so each hour was a NEW alert and notified immediately —
    ~50 e-mails a day, and no repeat interval could have suppressed it,
    because nothing was repeating. Subjects read "...stale staging alerts 144",
    then 143, then 142: that number was the label."""
    for r in _freshness_rules():
        for moving in ("minutes_stale", "hours_stale", "AS age", "now() - max"):
            if moving in ("now() - max",):
                continue                       # fine inside the HAVING clause
            assert moving not in r["sql"].split("HAVING")[0], (
                f"{r['uid']} returns {moving}, which changes while the "
                f"condition holds and so churns the alert identity")


def test_the_policy_states_a_repeat_interval():
    assert main._ops_alert_spec()["policy"].get("repeat_interval"), \
        "a persistent condition must mail on a stated cadence, not a default"


def test_legacy_give_ups_are_repaired():
    """A code fix that stops bad state being WRITTEN does not repair state
    already written. #312 removed the attempts-based give-up, but staging and
    sandbox kept 23/23 and 24/24 flagged rows and stopped fetching elements
    entirely — for six days, while production looked fine because it had been
    repaired by hand during the incident."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert "DELETE FROM element_fetch WHERE gave_up AND attempts >= 6" in src


def test_the_repair_cannot_match_a_current_row():
    """gave_up is now set only through an answered 'not carried', which fires
    on the first attempt — so attempts >= 6 identifies legacy rows exactly,
    and the repair is idempotent."""
    src = open(os.path.join(HERE, "..", "ingest", "ingest.py"), encoding="utf-8").read()
    rec = src[src.index("def _record_lookup("):src.index("def _tle_for(")]
    sets = [l for l in rec.splitlines() if "gave_up" in l and "=" in l]
    for line in sets:
        assert "attempts" not in line, \
            "if attempts can set gave_up again, the repair would delete live rows"


def test_a_provider_refusal_is_alerted_on(): # #357
    """CelesTrak: "immediately stop querying and report the problem to a human
    for investigation." We stop; this reports. It is deliberately NOT a
    freshness rule — an empty provider_refusal table means nobody refused us,
    which is the healthy state, the inverse of an empty telemetry table."""
    rules = {r["uid"]: r for r in main._ops_alert_spec()["rules"]}
    assert "provider-refused" in rules
    r = rules["provider-refused"]
    assert r.get("group") == "providers"
    assert r["sql"].split(" FROM ")[0].strip() == "SELECT 1 AS value", \
        "the alert identity must not churn (#352)"
    assert "provider_refusal" in r["sql"]
