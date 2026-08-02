"""Make Keycloak put the organization **id** in the `organization` claim (#140).

By default the claim carries only the org alias, so the API had no UUID to key
its tenant on (`invalid input syntax for type uuid: "acme-org"`, a 500 on every
authenticated call of a member). Enabling `add.organization.id` turns the claim
into `{"alias": {"id": "<uuid>"}}` — the shape `_org_of()` already expects.

Idempotent, stdlib only, runs on the VM against the shared Keycloak:

    KC_ADMIN_USERNAME=… KC_ADMIN_PASSWORD=… python3 batch/kc_org_id_claim.py
    KC_REALMS=overwatch,overwatch-sandbox   # default
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

KC = os.environ.get("KC_BASE", "http://127.0.0.1:8096/auth")
REALMS = os.environ.get("KC_REALMS", "overwatch,overwatch-sandbox").split(",")
ADMIN_USER = os.environ.get("KC_ADMIN_USERNAME", "")
ADMIN_PASS = os.environ.get("KC_ADMIN_PASSWORD", "")


def api(method, path, body=None, token=None, form=None):
    if form is not None:
        data, ct = urllib.parse.urlencode(form).encode(), \
            "application/x-www-form-urlencoded"
    else:
        data, ct = (json.dumps(body).encode() if body is not None else None), \
            "application/json"
    headers = {"Content-Type": ct}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(KC + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or "{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200]}


def main():
    if not (ADMIN_USER and ADMIN_PASS):
        raise SystemExit("KC_ADMIN_USERNAME / KC_ADMIN_PASSWORD not set")
    st, d = api("POST", "/realms/master/protocol/openid-connect/token", form={
        "grant_type": "password", "client_id": "admin-cli",
        "username": ADMIN_USER, "password": ADMIN_PASS})
    assert st == 200, f"admin token failed: {st}"
    tok = d["access_token"]

    for realm in [r.strip() for r in REALMS if r.strip()]:
        st, scopes = api("GET", f"/admin/realms/{realm}/client-scopes", token=tok)
        if st != 200:
            print(f"{realm}: cannot list client scopes ({st})")
            continue
        scope = next((s for s in scopes if s.get("name") == "organization"), None)
        if not scope:
            print(f"{realm}: no `organization` client scope — skipped")
            continue
        st, mappers = api(
            "GET", f"/admin/realms/{realm}/client-scopes/{scope['id']}"
                   "/protocol-mappers/models", token=tok)
        for m in mappers if st == 200 else []:
            cfg = m.get("config") or {}
            if cfg.get("add.organization.id") == "true":
                print(f"{realm}: `{m['name']}` already carries the id")
                continue
            cfg["add.organization.id"] = "true"
            m["config"] = cfg
            st2, d2 = api("PUT",
                          f"/admin/realms/{realm}/client-scopes/{scope['id']}"
                          f"/protocol-mappers/models/{m['id']}", body=m, token=tok)
            ok = st2 in (200, 204)
            print(f"{realm}: `{m['name']}` -> add.organization.id=true "
                  f"({'ok' if ok else f'FAILED {st2} {d2}'})")


if __name__ == "__main__":
    main()
