"""Create the dedicated Keycloak realm `overwatch-sandbox` on the ONE shared
Keycloak instance, as a clone of the prod realm `overwatch` (#126, TENANT.md §1
"Sandbox identity"). Idempotent; stdlib only; runs on the VM:

    KC_ADMIN_USERNAME=admin KC_ADMIN_PASSWORD=... python3 batch/kc_sandbox_realm.py

Mechanism: admin partial-export of the prod realm (clients + groups/roles, no
users) -> rename to overwatch-sandbox -> point the `overwatch` client's URIs at
the sandbox host -> import. Then a fresh client secret is generated and printed
(paste into orbit-poc/sandbox/.env as OVERWATCH_CLIENT_SECRET). The sandbox
realm keeps self-registration and the organizations feature.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

KC = os.environ.get("KC_BASE", "http://127.0.0.1:8096/auth")
SRC_REALM = "overwatch"
DST_REALM = "overwatch-sandbox"
HOST = "https://sandbox.overwatch.confinia.io"
ADMIN_USER = os.environ.get("KC_ADMIN_USERNAME", "")
ADMIN_PASS = os.environ.get("KC_ADMIN_PASSWORD", "")


def api(method, path, body=None, token=None, form=None):
    url = KC + path
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, (json.loads(e.read().decode() or "{}") if e.fp else {})


def main():
    if not (ADMIN_USER and ADMIN_PASS):
        raise SystemExit("KC_ADMIN_USERNAME / KC_ADMIN_PASSWORD not set")
    st, d = api("POST", "/realms/master/protocol/openid-connect/token", form={
        "grant_type": "password", "client_id": "admin-cli",
        "username": ADMIN_USER, "password": ADMIN_PASS})
    assert st == 200, f"admin token failed: {st} {d}"
    tok = d["access_token"]

    st, _ = api("GET", f"/admin/realms/{DST_REALM}", token=tok)
    if st == 200:
        print(f"realm {DST_REALM} already exists")
    else:
        st, realm = api("POST",
                        f"/admin/realms/{SRC_REALM}/partial-export"
                        "?exportClients=true&exportGroupsAndRoles=true", token=tok)
        assert st == 200, f"partial-export failed: {st}"
        realm["realm"] = DST_REALM
        realm["id"] = DST_REALM
        realm["displayName"] = "Overwatch SANDBOX"
        realm.pop("keycloakVersion", None)
        for c in realm.get("clients", []):
            if c.get("clientId") == "overwatch":
                c["redirectUris"] = [f"{HOST}/*"]
                c["baseUrl"] = HOST
                c["rootUrl"] = HOST
                c.setdefault("attributes", {})["post.logout.redirect.uris"] = f"{HOST}/*"
                c["webOrigins"] = [HOST]
                c.pop("secret", None)          # never copy the prod secret
        st, d = api("POST", "/admin/realms", body=realm, token=tok)
        assert st in (201, 409), f"realm import failed: {st} {json.dumps(d)[:300]}"
        print(f"realm {DST_REALM} created (clone of {SRC_REALM}, users empty)")

    # fresh client secret for the sandbox realm's `overwatch` client
    st, clients = api("GET", f"/admin/realms/{DST_REALM}/clients?clientId=overwatch",
                      token=tok)
    assert st == 200 and clients, f"client lookup failed: {st}"
    cid = clients[0]["id"]
    st, d = api("POST", f"/admin/realms/{DST_REALM}/clients/{cid}/client-secret",
                token=tok)
    assert st == 200, f"secret rotation failed: {st}"
    print("\n=== paste into orbit-poc/sandbox/.env ===")
    print(f"OVERWATCH_CLIENT_SECRET={d.get('value')}")


if __name__ == "__main__":
    main()
