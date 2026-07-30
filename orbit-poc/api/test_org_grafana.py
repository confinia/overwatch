"""Guards issue #13 (PR B): each organization gets its OWN Grafana — an org, a
datasource bound to its **RLS-scoped Postgres role**, and a private dashboard.

The isolation guarantee is the database role, so the seeded dashboard must carry
no tenant filter and must never reference a privileged datasource role.
"""
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


def test_api_image_ships_the_template():
    df = open(os.path.join(HERE, "Dockerfile"), encoding="utf-8").read()
    assert "COPY tenant_dashboard.json" in df
