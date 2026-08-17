"""Guards issue #269: Creem.io as the EU merchant of record (rule 27).

Stub-mode unit tests — no Creem account, no network. The seam contract is that
creem.py and polar.py are drop-in replacements behind billing.py, and that a
production stack without real credentials stays inert instead of accepting
forged webhooks.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import billing  # noqa: E402
import creem  # noqa: E402

creem.ALLOW_STUB = True


def test_the_default_provider_is_creem():
    """Rule 27: Creem is the payment method; polar is the explicit fallback."""
    assert billing.PROVIDER in ("creem", "polar")
    assert os.environ.get("BILLING_PROVIDER", "creem") != "" and \
        billing.PROVIDER == os.environ.get("BILLING_PROVIDER", "creem").lower()


def test_env_is_derived_from_the_key_prefix(monkeypatch):
    """creem_test_ must NEVER read as production: the test/production split is
    the whole safety story of Test Mode. Re-import the module under each key so
    the module-level derivation itself is what is tested."""
    import importlib
    assert creem.ENV == "off"                        # no key in unit tests
    for key, want in (("creem_test_abc", "sandbox"),
                      ("creem_live_abc", "production"), ("", "off")):
        monkeypatch.setenv("CREEM_API_KEY", key)
        mod = importlib.reload(creem)
        assert mod.ENV == want, (key, mod.ENV)
        if want == "sandbox":
            assert mod.API_BASE.startswith("https://test-api.creem.io")
    monkeypatch.delenv("CREEM_API_KEY", raising=False)
    importlib.reload(creem)
    creem.ALLOW_STUB = True                          # restore the module state


def test_test_mode_talks_to_the_test_host():
    """A creem_test_ key must hit test-api.creem.io — pointing test keys at the
    production API is how a 'sandbox' charges a real card."""
    src = open(os.path.join(os.path.dirname(__file__), "creem.py"),
               encoding="utf-8").read()
    assert "https://test-api.creem.io/v1" in src
    assert 'ENV == "sandbox"' in src


def test_off_by_default_runs_the_stub():
    assert not creem.configured()
    assert creem.stub_allowed()
    ck = creem.create_checkout("org-123", "a@b.c", "https://x/success")
    assert ck["stub"] is True
    assert "org-123" in ck["url"] and ck["url"].startswith("/v1/billing/")


def test_production_is_inert_without_real_secret(monkeypatch):
    """A public prod with a live key but no webhook secret must reject every
    webhook rather than fall back to the public stub secret."""
    monkeypatch.setattr(creem, "ENV", "production")
    monkeypatch.setattr(creem, "WEBHOOK_SECRET", "")
    assert creem._secret() == ""
    assert not creem.verify_webhook(b'{"x":1}', {"creem-signature": "deadbeef"})


def test_webhook_sign_verify_roundtrip():
    raw = b'{"eventType":"subscription.active"}'
    sig = creem.sign(raw)
    assert creem.verify_webhook(raw, {"creem-signature": sig})
    assert creem.verify_webhook(raw, {"creem-signature": "sha256=" + sig})
    assert not creem.verify_webhook(raw, {"creem-signature": "deadbeef"})
    assert not creem.verify_webhook(b'{"tampered":1}', {"creem-signature": sig})
    assert not creem.verify_webhook(raw, {})


def test_parse_event_normalizes_the_subscription_envelope():
    ev = creem.parse_event({
        "id": "evt_1", "eventType": "subscription.active",
        "object": {"id": "sub_9", "status": "active",
                   "customer": {"id": "cust_5", "email": "a@b.c"},
                   "metadata": {"org": "org-123"},
                   "current_period_end_date": "2026-09-17T00:00:00Z"}})
    assert ev["type"] == "subscription.active"
    assert ev["org_id"] == "org-123"
    assert ev["subscription_id"] == "sub_9"
    assert ev["customer_id"] == "cust_5"
    assert ev["status"] == "active"
    assert ev["until"] == "2026-09-17T00:00:00Z"


def test_parse_event_checkout_completed_activates():
    """checkout.completed IS the proof of payment; its own status ('completed')
    must not fail the active check in _apply_billing_event."""
    ev = creem.parse_event({
        "eventType": "checkout.completed",
        "object": {"id": "ch_1", "status": "completed",
                   "customer": "cust_5", "subscription": "sub_9",
                   "metadata": {"org": "org-123"}}})
    assert ev["type"] == "order.created"
    assert ev["org_id"] == "org-123"
    assert ev["subscription_id"] == "sub_9"
    assert ev["customer_id"] == "cust_5"
    assert ev["status"] is None                       # None counts as active


def test_parse_event_lifecycle_mapping():
    for creem_type, ours in (("subscription.canceled", "subscription.canceled"),
                             ("subscription.scheduled_cancel", "subscription.canceled"),
                             ("subscription.expired", "subscription.revoked"),
                             ("subscription.paid", "subscription.active"),
                             ("subscription.trialing", "subscription.active")):
        assert creem.parse_event({"eventType": creem_type, "object": {}})["type"] == ours
    # a failing card mid-period must NOT revoke access: past_due maps to itself,
    # which _apply_billing_event ignores; Creem escalates to expired later
    assert creem.parse_event({"eventType": "subscription.past_due",
                              "object": {}})["type"] == "subscription.past_due"


def test_the_two_providers_expose_the_same_seam():
    """billing.py swaps them with one variable — same six functions or the swap
    is a lie."""
    import polar
    for fn in ("configured", "stub_allowed", "create_checkout",
               "create_customer_session", "verify_webhook", "parse_event", "sign"):
        assert callable(getattr(creem, fn)), f"creem.{fn}"
        assert callable(getattr(polar, fn)), f"polar.{fn}"


def test_portal_falls_back_without_a_customer_id():
    """Before the first webhook there is no Creem customer id; the portal
    button must land on our status page, never dead-end or 500."""
    ps = creem.create_customer_session("org-123", "https://x/account", "")
    assert ps["stub"] is True and "org-123" in ps["url"]


def test_main_talks_to_the_facade_not_to_polar():
    """Every billing call site in main.py goes through billing.*; the only
    allowed direct polar reference is the import (kept for the fallback)."""
    src = open(os.path.join(os.path.dirname(__file__), "main.py"),
               encoding="utf-8").read()
    assert "import billing" in src
    bad = [l for l in src.splitlines()
           if "polar." in l and "polar_customer_id" not in l
           and not l.strip().startswith(("#", "import", "from"))]
    assert not bad, f"main.py still calls polar directly: {bad[:3]}"


def test_sandbox_stack_runs_creem_test_mode():
    """Rule 27: the sandbox is the Creem Test-Mode environment."""
    compose = open(os.path.join(os.path.dirname(__file__), "..", "sandbox",
                                "docker-compose.yml"), encoding="utf-8").read()
    assert 'BILLING_PROVIDER: "creem"' in compose
    assert 'CREEM_ALLOW_STUB: "1"' in compose


def test_badge_reads_the_provider_neutral_field():
    """#256's badge must survive the provider switch: it reads `env` (with the
    legacy polar_env as fallback), so a Creem sandbox still shows the warning."""
    app = open(os.path.join(os.path.dirname(__file__), "..", "web", "static",
                            "app.js"), encoding="utf-8").read()
    assert "m.env || m.polar_env" in app


def test_the_image_ships_every_local_module_main_imports():
    """The api Dockerfile copies modules BY NAME; a new local import that is
    not added there passes every unit test and then crash-loops the staged
    container at boot (`import billing` — how #269 broke the deploy)."""
    here = os.path.dirname(__file__)
    df = open(os.path.join(here, "Dockerfile"), encoding="utf-8").read()
    src = open(os.path.join(here, "main.py"), encoding="utf-8").read()
    local = {f[:-3] for f in os.listdir(here)
             if f.endswith(".py") and not f.startswith("test_")}
    imported = set()
    for line in src.splitlines():
        line = line.strip()
        if line.startswith("import ") or line.startswith("from "):
            mod = line.split()[1].split(".")[0]
            if mod in local:
                imported.add(mod)
    # follow one level: modules imported by those modules (billing -> creem)
    for mod in list(imported):
        for line in open(os.path.join(here, mod + ".py"), encoding="utf-8"):
            line = line.strip()
            if line.startswith("import ") or line.startswith("from "):
                sub = line.split()[1].split(".")[0]
                if sub in local:
                    imported.add(sub)
    missing = [m for m in sorted(imported) if f"COPY {m}.py" not in df]
    assert not missing, f"Dockerfile does not ship: {missing}"


def test_org_create_survives_a_replay(monkeypatch):
    """Keycloak rejects a duplicate alias with 400, not 409. A double click or
    browser retry replays the POST; if the organization exists, the replay is a
    success — the walk saw 'Organization creation failed (400)' with the org
    sitting right there (#267)."""
    import main
    src = __import__("inspect").getsource(main.create_org)
    assert "not found" in src, "the duplicate-alias fallthrough is gone"
    assert 'r.status_code not in (201, 409) and not found' in src
