"""Guards issue #139: on staging/sandbox the caddy basic-auth gate makes the
browser replay `Authorization: Basic …` on every request. That header must NOT
shadow the session cookie — it used to, so every authenticated call 401'd.

Tests the real `_claims` (not the monkeypatched boundary the other suites use),
with JWT verification stubbed so no Keycloak is needed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402


class Req:
    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}


def _accept_any_token(monkeypatch):
    """Decode step stubbed: we only assert WHICH token _claims picks."""
    monkeypatch.setattr(main, "_jwks_client", lambda: type(
        "K", (), {"get_signing_key_from_jwt": staticmethod(
            lambda t: type("S", (), {"key": "k"})())})())
    monkeypatch.setattr(main._jwt, "decode",
                        lambda tok, key, **kw: {"sub": "s", "picked": tok})


def test_basic_header_falls_through_to_the_cookie(monkeypatch):
    _accept_any_token(monkeypatch)
    c = main._claims(Req(headers={"authorization": "Basic Y2xlbWVudDpwdw=="},
                         cookies={main.COOKIE: "the-session-token"}))
    assert c and c["picked"] == "the-session-token"


def test_bearer_header_is_used_when_present(monkeypatch):
    _accept_any_token(monkeypatch)
    c = main._claims(Req(headers={"authorization": "Bearer api-token"},
                         cookies={main.COOKIE: "the-session-token"}))
    assert c and c["picked"] == "api-token"


def test_bearer_is_case_insensitive(monkeypatch):
    _accept_any_token(monkeypatch)
    c = main._claims(Req(headers={"authorization": "bearer api-token"}))
    assert c and c["picked"] == "api-token"


def test_no_credentials_is_anonymous(monkeypatch):
    _accept_any_token(monkeypatch)
    assert main._claims(Req()) is None
    # a Basic header alone must not be treated as a token either
    assert main._claims(Req(headers={"authorization": "Basic xyz"})) is None


# --- #140: the organization claim may carry only the alias ------------------
def test_org_of_uses_the_uuid_from_the_dict_claim():
    assert main._org_of({"organization": {"acme": {"id": "11111111-2222-3333-4444-555555555555"}}}) \
        == ("11111111-2222-3333-4444-555555555555", "acme")


def test_org_of_resolves_an_alias_only_claim(monkeypatch):
    """Keycloak's multivalued shape is ["alias"] — the UUID must be resolved."""
    monkeypatch.setattr(main, "_resolve_org_id", lambda a: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert main._org_of({"organization": ["acme-org"]}) \
        == ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "acme-org")


def test_org_of_refuses_an_unresolvable_alias(monkeypatch):
    import fastapi
    monkeypatch.setattr(main, "_resolve_org_id", lambda a: None)
    try:
        main._org_of({"organization": ["ghost-org"]})
    except fastapi.HTTPException as e:
        assert e.status_code == 502
    else:
        raise AssertionError("a non-UUID org id must not reach the database")


def test_org_of_without_claim_is_none():
    assert main._org_of({}) is None
