"""Keycloak admin helper for the Selenium walk (#267).

The walk registers its user through the real signup *form*, but the realms have
verifyEmail=true — so Keycloak parks the brand-new account on a "verify your
e-mail" notice and no login is possible. This marks that one disposable user
verified so the walk can carry on, and deletes it at teardown.

It touches nothing else: the account it acts on is the one this run just
created, addressed by its unique per-run e-mail.

Usage:  kc_admin.py verify <email>
        kc_admin.py delete <email>
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("KC_ADMIN_BASE", "http://127.0.0.1:12070").rstrip("/")
REALM = os.environ["KC_REALM"]
USER = os.environ["KC_ADMIN_USERNAME"]
PASS = os.environ["KC_ADMIN_PASSWORD"]


def _call(method, path, token=None, body=None, form=None):
    data, headers = None, {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def token():
    st, body = _call("POST", "/auth/realms/master/protocol/openid-connect/token",
                     form={"grant_type": "password", "client_id": "admin-cli",
                           "username": USER, "password": PASS})
    if st != 200:
        sys.exit(f"keycloak admin login failed ({st}) — check KC_ADMIN_* in .env "
                 f"and that {BASE} is reachable (tunnel from the Mac)")
    return body["access_token"]


def find(tok, email):
    st, body = _call(
        "GET",
        f"/auth/admin/realms/{REALM}/users?email={urllib.parse.quote(email)}&exact=true",
        tok)
    if st != 200 or not body:
        return None
    return body[0]


def main():
    action, email = sys.argv[1], sys.argv[2]
    tok = token()
    u = find(tok, email)
    if not u:
        if action == "delete":
            print(f"  teardown: {email} already gone")
            return
        sys.exit(f"registration did not create {email} in realm {REALM} — "
                 f"the signup form step failed")
    if action == "verify":
        u["emailVerified"] = True
        u["requiredActions"] = [a for a in u.get("requiredActions", [])
                                if a != "VERIFY_EMAIL"]
        st, _ = _call("PUT", f"/auth/admin/realms/{REALM}/users/{u['id']}", tok, u)
        if st not in (200, 204):
            sys.exit(f"could not verify {email} ({st})")
        print(f"  e-mail marked verified for {email}")
    elif action == "delete":
        st, _ = _call("DELETE", f"/auth/admin/realms/{REALM}/users/{u['id']}", tok)
        print(f"  teardown: deleted {email} ({st})")
    else:
        sys.exit(f"unknown action {action}")


if __name__ == "__main__":
    main()
