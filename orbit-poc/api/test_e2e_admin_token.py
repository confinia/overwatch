"""Guards #500: the e2e walk's Keycloak admin token outliving the walk.

The master realm's accessTokenLifespan is 60 seconds; a walk takes longer. With
one token minted at the start, the teardown ran past the expiry and every admin
call answered 401 — which `kc_user_id` reads as "no such user", so the deletes
were skipped and the run still exited 0. Silent, and the disposable accounts
stayed in the realm forever.

What must hold: an expired token can never look like an absent user.
"""
import importlib.util
import json
import os

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "..", "..", "deploy", "e2e_sandbox.py")


def _load():
    spec = importlib.util.spec_from_file_location("e2e_admin_token", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fetch_recorder(script):
    """Replaces fetch(): plays `script` (one entry per call) and records them."""
    calls = []

    def fetch(op, url, data=None, headers=None, method=None, retries=6):
        calls.append({"url": url, "method": method,
                      "auth": (headers or {}).get("Authorization", "")})
        return script[len(calls) - 1]

    return fetch, calls


def test_a_stale_token_is_re_minted_and_the_call_retried(monkeypatch):
    e2e = _load()
    fetch, calls = _fetch_recorder([
        (401, "", "expired"),                                   # the admin call
        (200, "", json.dumps({"access_token": "fresh"})),        # re-mint
        (200, "", '[{"id": "u1"}]'),                             # the retry
    ])
    monkeypatch.setattr(e2e, "fetch", fetch)
    e2e._ADMIN.update(op=object(), token="stale", minted=0.0)
    st, txt = e2e.kc(None, "GET", "/users?email=x", token="stale")
    assert st == 200 and json.loads(txt)[0]["id"] == "u1"
    assert calls[1]["url"].endswith("/realms/master/protocol/openid-connect/token")
    assert calls[2]["auth"] == "Bearer fresh", "the retry must use the new token"


def test_an_expired_token_never_reads_as_a_missing_user(monkeypatch):
    """The exact shape of the bug: the lookup that decides whether to delete."""
    e2e = _load()
    fetch, _ = _fetch_recorder([
        (401, "", "expired"),
        (200, "", json.dumps({"access_token": "fresh"})),
        (200, "", '[{"id": "the-user"}]'),
    ])
    monkeypatch.setattr(e2e, "fetch", fetch)
    e2e._ADMIN.update(op=object(), token="stale", minted=0.0)
    assert e2e.kc_user_id(None, "stale", "e2e-bot+x@confinia.io") == "the-user"


def test_the_token_is_re_minted_before_keycloak_would_expire_it(monkeypatch):
    e2e = _load()
    assert e2e.ADMIN_TOKEN_TTL < 60, \
        "Keycloak's master realm expires admin tokens after 60 s"
    clock = [1000.0]
    monkeypatch.setattr(e2e.time, "monotonic", lambda: clock[0])
    minted = []

    def fetch(op, url, data=None, headers=None, method=None, retries=6):
        minted.append(url)
        return 200, "", json.dumps({"access_token": f"t{len(minted)}"})

    monkeypatch.setattr(e2e, "fetch", fetch)
    e2e._ADMIN.update(op=None, token="", minted=0.0)
    assert e2e.kc_admin_token(None) == "t1"
    clock[0] += e2e.ADMIN_TOKEN_TTL - 1
    assert e2e.kc_admin_token(None) == "t1", "still fresh, must not re-mint"
    clock[0] += 2
    assert e2e.kc_admin_token(None) == "t2", "past the TTL, must re-mint"
    assert len(minted) == 2


def test_the_walk_still_mints_once_up_front():
    """The cache must not turn into a lazy first call: a bad admin password has
    to fail at step 2, not halfway through a live walk."""
    src = open(SCRIPT, encoding="utf-8").read()
    assert 'step("Keycloak admin token")' in src
    assert "token = kc_admin_token(adm)" in src
