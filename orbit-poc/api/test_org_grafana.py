"""Guards issue #13 (PR B): each organization gets its OWN Grafana — an org, a
datasource bound to its **RLS-scoped Postgres role**, and a private dashboard.

The isolation guarantee is the database role, so the seeded dashboard must carry
no tenant filter and must never reference a privileged datasource role.
"""
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402

HERE = os.path.dirname(__file__)
TPL = os.path.join(HERE, "tenant_dashboard.json")


def test_dashboard_template_relies_on_rls_not_on_a_filter():
    tpl = open(TPL, encoding="utf-8").read()
    assert "tenant_telemetry" in tpl
    # RLS scopes the role to its own rows; a tenant filter would be redundant
    # and would let an editor widen it. It must NOT be there.
    assert "tenant =" not in tpl and "$tenant" not in tpl
    assert "__DS_UID__" in tpl                  # datasource is injected per org


def test_dashboard_template_is_valid_json_with_panels():
    d = json.loads(open(TPL, encoding="utf-8").read())
    assert d["uid"] == "org-private"
    assert len(d["panels"]) >= 2
    for p in d["panels"]:
        assert p["datasource"]["uid"] == "__DS_UID__"


def test_provisioning_is_noop_without_config(monkeypatch):
    # self-host / tests: no ORG_DB_SECRET or no Grafana admin password -> no-op,
    # never a crash on the authenticated path
    monkeypatch.setattr(main, "GF_ADMIN_PASS", "")
    assert main._provision_grafana_org(None, "org-1", "Acme", "a@b.c") is None


def test_provisioning_uses_the_org_rls_role(monkeypatch):
    """The datasource must authenticate as org_<hex>, never as the app role
    `orbit` or the public `grafana_ro`."""
    calls = []
    monkeypatch.setattr(main, "ORG_DB_SECRET", "test-secret")
    monkeypatch.setattr(main, "GF_ADMIN_PASS", "pw")

    class R:
        status_code = 404
        def json(self):
            return {"orgId": 7, "id": 7}

    def fake_gf(method, path, body=None, gorg=None):
        calls.append((method, path, body, gorg))
        r = R()
        if method == "POST":
            r.status_code = 200
        return r

    class Cur:
        def __init__(self): self.connection = self
        def execute(self, *a): self._last = a
        def fetchone(self): return (None,)
        def commit(self): pass

    monkeypatch.setattr(main, "_gf", fake_gf)
    gorg = main._provision_grafana_org(Cur(), "11111111-2222-3333-4444-555555555555",
                                       "Acme", "a@b.c")
    assert gorg == 7
    ds = next(b for m, p, b, g in calls if p == "/datasources")
    expected_role, expected_pw = main._org_role("11111111-2222-3333-4444-555555555555")
    assert ds["user"] == expected_role
    assert ds["user"].startswith("org_")
    assert ds["user"] not in ("orbit", main.GRAFANA_ROLE)
    assert ds["secureJsonData"]["password"] == expected_pw
    # the dashboard is seeded into that Grafana org with the org's datasource
    dash = next(b for m, p, b, g in calls if p == "/dashboards/db")
    assert dash["dashboard"]["uid"] == "org-private"
    assert "__DS_UID__" not in json.dumps(dash)
    # and the user is Editor in THEIR org only
    member = next(b for m, p, b, g in calls if p.endswith("/users"))
    assert member["role"] == "Editor"


def test_membership_resyncs_when_org_already_provisioned(monkeypatch):   # #13
    """A user exists in Grafana only after their first OIDC login, so the org is
    usually already provisioned (grafana_org_id set) by the time membership can
    be added. Every call must re-attempt the Editor membership, not short-circuit
    past it — else the user is stuck in Main Org and never sees their dashboard."""
    calls = []
    monkeypatch.setattr(main, "GF_ADMIN_PASS", "pw")
    monkeypatch.setattr(main, "ORG_DB_SECRET", "test-secret")

    def fake_gf(method, path, body=None, gorg=None):
        calls.append((method, path, body))
        class R:
            status_code = 200
        return R()

    monkeypatch.setattr(main, "_gf", fake_gf)

    class Cur:
        def __init__(self): self.connection = self
        def execute(self, *a): pass
        def fetchone(self): return (42,)              # already provisioned
        def commit(self): pass

    gorg = main._provision_grafana_org(Cur(), "11111111-2222-3333-4444-555555555555",
                                       "Acme", "a@b.c")
    assert gorg == 42
    member = [b for m, p, b in calls if p == "/orgs/42/users"]
    assert member, "membership was not re-synced on the already-provisioned path"
    assert member[0]["loginOrEmail"] == "a@b.c" and member[0]["role"] == "Editor"


def test_api_image_ships_the_template():
    df = open(os.path.join(HERE, "Dockerfile"), encoding="utf-8").read()
    assert "COPY tenant_dashboard.json" in df


def test_ops_datasource_uid_cannot_clobber_the_public_one():
    """Grafana datasource uids are GLOBALLY unique. The ops datasource used to
    reuse the public uid "orbitcache", so its POST 409'd and the refresh path
    PUT the ops config (user ops_ro) onto the PUBLIC datasource — every public
    dashboard then queried as a role with no SELECT on satellite/telemetry and
    rendered "No data". The two must never share a uid."""
    assert main.OPS_DS_UID != "orbitcache"
    src = inspect.getsource(main._provision_ops_org)
    assert "OPS_DS_UID" in src
    # and the refresh path must not overwrite someone else's datasource
    assert 'cur.get("name") == ds["name"]' in src
    # the ops boards/alerts must query through the ops datasource
    assert "OPS_DS_UID" in inspect.getsource(main._ops_alert_rules)
    ops_dir = os.path.join(os.path.dirname(__file__), "..", "grafana", "ops-dashboards")
    for f in os.listdir(ops_dir):
        if f.endswith(".json"):
            body = open(os.path.join(ops_dir, f), encoding="utf-8").read()
            assert '"orbitcache"' not in body, f"{f} still points at the public datasource"


def test_ops_datasource_uid_is_migrated_not_left_colliding(monkeypatch):   # #261
    """An install created before OPS_DS_UID still holds the PUBLIC uid on its ops
    datasource. Two rows sharing a uid make Grafana fail provisioning at boot and
    crash-loop, so provisioning must MOVE ours — and must never touch the public
    one, which answers to the same uid lookup."""
    calls = []

    def fake_gf(method, path, body=None, gorg=None):
        calls.append((method, path, body))
        class R:
            status_code = 200
            def json(self):
                return {"id": 9, "uid": "orbitcache", "name": "OrbitCache (ops)"}
        return R()

    monkeypatch.setattr(main, "_gf", fake_gf)
    main._migrate_ops_datasource_uid(3)
    put = [b for m, p, b in calls if m == "PUT"]
    assert put, "ops datasource left on the colliding uid"
    assert put[0]["uid"] == main.OPS_DS_UID


def test_migration_never_renames_the_public_datasource(monkeypatch):   # #261
    """The public datasource answers to the same uid lookup. Renaming it would
    break every public dashboard — the exact failure #253 was about."""
    calls = []

    def fake_gf(method, path, body=None, gorg=None):
        calls.append((method, path, body))
        class R:
            status_code = 200
            def json(self):
                return {"id": 1, "uid": "orbitcache", "name": "OrbitCache"}
        return R()

    monkeypatch.setattr(main, "_gf", fake_gf)
    main._migrate_ops_datasource_uid(3)
    assert not [m for m, p, b in calls if m == "PUT"], "renamed the PUBLIC datasource"
