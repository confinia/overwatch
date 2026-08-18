"""Guards issue #275: the DevSat plan — the 9 €/month entry tier.

One private satellite fed by the simulator (#260), plan-aware checkout and
webhook, and the account-page ladder Free → DevSat → Pro. The entitlement is
the PLAN resolved from the product the webhook carries — never a raw id.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(__file__))
import creem  # noqa: E402

HERE = os.path.dirname(__file__)
creem.ALLOW_STUB = True


def test_the_plan_ladder_is_derived_from_products(monkeypatch):
    monkeypatch.setattr(creem, "PRODUCTS", {"pro": "prod_P", "devsat": "prod_D"})
    assert creem.plan_of("prod_D") == "devsat"
    assert creem.plan_of("prod_P") == "pro"
    # an unknown product on a PAID event fails toward the old behaviour —
    # entitling as pro beats refusing money already taken
    assert creem.plan_of("prod_unknown") == "pro"


def test_checkout_picks_the_product_for_the_plan(monkeypatch):
    calls = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"id": "ch_1", "checkout_url": "https://x"}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.update(json); return R()

    monkeypatch.setattr(creem, "ENV", "sandbox")
    monkeypatch.setattr(creem, "API_KEY", "creem_test_x")
    monkeypatch.setattr(creem, "PRODUCT_PRO", "prod_P")
    monkeypatch.setattr(creem, "PRODUCTS", {"pro": "prod_P", "devsat": "prod_D"})
    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    creem.create_checkout("org-1", "a@b.c", "https://s", plan="devsat")
    assert calls["product_id"] == "prod_D"
    creem.create_checkout("org-1", "a@b.c", "https://s", plan="pro")
    assert calls["product_id"] == "prod_P"
    try:
        creem.create_checkout("org-1", "a@b.c", "https://s", plan="galactic")
        assert False, "unknown plan must not silently sell something else"
    except LookupError:
        pass


def test_webhook_events_carry_the_plan(monkeypatch):
    monkeypatch.setattr(creem, "PRODUCTS", {"pro": "prod_P", "devsat": "prod_D"})
    ev = creem.parse_event({
        "eventType": "subscription.active",
        "object": {"id": "sub_1", "status": "active",
                   "product": {"id": "prod_D", "name": "devsat plan"},
                   "metadata": {"org": "org-1"}}})
    assert ev["plan"] == "devsat"
    # product nested in subscription items, as Creem also ships it
    ev = creem.parse_event({
        "eventType": "subscription.paid",
        "object": {"id": "sub_1", "status": "paid",
                   "items": [{"productId": "prod_D"}],
                   "metadata": {"org": "org-1"}}})
    assert ev["plan"] == "devsat"


def test_apply_billing_event_writes_the_plan():
    """The UPDATE must set the plan the event carries, not a literal 'pro'."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert "SET plan='pro', sub_status='active'" not in src
    assert 'ev.get("plan") or "pro"' in src


def test_polar_cannot_sell_devsat():
    import importlib
    import billing
    if billing.PROVIDER == "polar":
        try:
            billing.create_checkout("o", "e", "s", plan="devsat")
            assert False
        except LookupError:
            pass
    else:
        assert "devsat" in billing.PLANS or not creem.PRODUCT_DEVSAT


def test_billing_status_exposes_paid_and_plan():
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert '"paid": paid' in src
    assert 'plan in ("pro", "devsat")' in src


def test_account_page_offers_the_ladder():
    html = open(os.path.join(HERE, "..", "web", "static", "account.html"),
                encoding="utf-8").read()
    assert 'id="act-devsat"' in html and "upgrade('devsat')" in html
    assert "upgrade('pro')" in html
    # a paid DevSat org is offered the portal AND the Pro upgrade
    assert 'planName' in html and "s.paid" in html


def test_pricing_page_shows_three_tiers_and_the_eu_mor():
    html = open(os.path.join(HERE, "..", "web", "static", "pro.html"),
                encoding="utf-8").read()
    assert "DevSat — 9" in html
    assert "Pro (beta) — 49" in html
    assert "Creem" in html and "Polar (merchant of record)" not in html


def test_devsat_quota_one_satellite(monkeypatch):
    """Rule-13 core: a devsat org can feed ONE satellite; a push naming a
    second is refused with the upgrade path; a pro org is not capped."""
    import psycopg2
    import psycopg2.pool
    import main
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        import pytest
        pytest.skip("no database in this environment")
    main.pool = psycopg2.pool.SimpleConnectionPool(1, 2, dsn)
    conn = psycopg2.connect(dsn)
    init = open(os.path.join(HERE, "..", "db", "init.sql"), encoding="utf-8").read()
    org = str(uuid.uuid4())
    tok = None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(init)
            cur.execute(main.KEYS_SQL)
            cur.execute("INSERT INTO organization (id, name, plan) "
                        "VALUES (%s::uuid, 'devsat test', 'devsat')", (org,))
            # a service token resolves through the org's OWN tenant record
            cur.execute("INSERT INTO tenant (key, name, email) VALUES "
                        "(%s::uuid, 'devsat test', 'devsat@test.invalid')", (org,))
            cur.execute("INSERT INTO org_token (org, label) VALUES (%s::uuid, 'sim') "
                        "RETURNING token", (org,))
            tok = str(cur.fetchone()[0])
        pts = [{"ts": "2026-08-18T00:00:00Z", "field": "battery_v", "value": 7.9}]
        r = main.tenant_push(tok, main.TenantPush(satellite="SIM One", points=pts))
        assert r["accepted"] == 1
        r = main.tenant_push(tok, main.TenantPush(satellite="SIM One", points=[
            {"ts": "2026-08-18T00:01:00Z", "field": "battery_v", "value": 7.8}]))
        assert r["accepted"] == 1                    # same satellite: fine
        try:
            main.tenant_push(tok, main.TenantPush(satellite="SIM Two", points=pts))
            assert False, "second satellite must be refused on DevSat"
        except main.HTTPException as e:
            assert e.status_code == 403
            assert "Upgrade to Pro" in e.detail
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE organization SET plan='pro' WHERE id=%s::uuid", (org,))
        r = main.tenant_push(tok, main.TenantPush(satellite="SIM Two", points=pts))
        assert r["accepted"] == 1                    # pro: the fleet is open
    finally:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM tenant_telemetry WHERE tenant = %s::uuid", (org,))
            cur.execute("DELETE FROM tenant WHERE key = %s::uuid", (org,))
            cur.execute("DELETE FROM org_usage WHERE customer = %s", (org,))
            if tok:
                cur.execute("DELETE FROM org_token WHERE token = %s::uuid", (tok,))
            cur.execute("DELETE FROM organization WHERE id = %s::uuid", (org,))
        conn.close()
        if main.pool:
            main.pool.closeall()
            main.pool = None
