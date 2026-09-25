"""Guards #489: the sandbox e2e sweeps the e2e-bot+<run> Keycloak users left
by earlier runs that died before their cleanup. Those bots kept their orgs
alive through the memberless sweep (#485): the org had a member, the member
was a corpse. 12 of them were deleted by hand on 2026-09-23.
"""
import importlib.util
import os

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "..", "..", "deploy", "e2e_sandbox.py")


def _load():
    spec = importlib.util.spec_from_file_location("e2e_sandbox", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DAY = 86400
NOW = 1_800_000_000.0


def _fake_kc(users, deleted, expect_realm=None):
    def kc(op, method, path, body=None, token="", realm=None):
        assert realm == expect_realm, f"swept the wrong realm: {realm}"
        if method == "GET":
            assert path.startswith("/users?search=e2e-")
            return 200, __import__("json").dumps(users)
        assert method == "DELETE"
        deleted.append(path.rsplit("/", 1)[1])
        return 204, ""
    return kc


def test_only_bots_older_than_a_day_are_deleted(monkeypatch):
    e2e = _load()
    ms = lambda age_s: int((NOW - age_s) * 1000)
    users = [
        {"id": "old", "email": "e2e-bot+abc123@confinia.io", "createdTimestamp": ms(3 * DAY)},
        {"id": "fresh", "email": "e2e-bot+def456@confinia.io", "createdTimestamp": ms(600)},
        {"id": "human", "email": "e2e-bot-fan@example.org", "createdTimestamp": ms(9 * DAY)},
        {"id": "other-domain", "email": "e2e-bot+x@evil.example", "createdTimestamp": ms(9 * DAY)},
        {"id": "no-email", "username": "e2e-bot+ghi789@confinia.io", "createdTimestamp": ms(2 * DAY)},
    ]
    deleted = []
    monkeypatch.setattr(e2e, "kc", _fake_kc(users, deleted))
    assert e2e.sweep_stale_bots(None, "tok", now=NOW) == 2
    assert sorted(deleted) == ["no-email", "old"]


def test_a_failed_listing_deletes_nothing(monkeypatch):
    e2e = _load()
    deleted = []
    monkeypatch.setattr(e2e, "kc", lambda *a, **k: (401, "nope"))
    assert e2e.sweep_stale_bots(None, "tok", now=NOW) == 0
    assert deleted == []


def test_the_gate_realm_is_swept_the_same_way(monkeypatch):
    """#290 added a second disposable account per run, in another realm. The
    same leak applies to it, so the same sweep has to reach it."""
    e2e = _load()
    ms = lambda age_s: int((NOW - age_s) * 1000)
    users = [
        {"id": "old", "email": "e2e-gate+abc@confinia.io", "createdTimestamp": ms(3 * DAY)},
        {"id": "fresh", "email": "e2e-gate+def@confinia.io", "createdTimestamp": ms(600)},
        {"id": "founder", "email": "someone@confinia.io", "createdTimestamp": ms(9 * DAY)},
    ]
    deleted = []
    monkeypatch.setattr(e2e, "kc",
                        _fake_kc(users, deleted, expect_realm="overwatch-gate"))
    assert e2e.sweep_stale_bots(None, "tok", now=NOW, prefix="e2e-gate+",
                                realm="overwatch-gate") == 1
    assert deleted == ["old"], "a standing account must never be swept"


def test_the_walk_sweeps_before_creating_its_own_user():
    src = open(SCRIPT, encoding="utf-8").read()
    sweep = src.index("sweep_stale_bots(adm, token)")
    create = src.index("setup_user(adm, token)")
    assert sweep < create, "the sweep must run at the start of every walk"
    gate = src.index("prefix='e2e-gate+'")
    assert gate < src.index("setup_gate_user(adm, token)"), \
        "the gate realm must be swept before this run adds to it"
