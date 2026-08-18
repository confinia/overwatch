"""First-run Keycloak bootstrap for self-hosted Overwatch (#276).

Creates, idempotently, everything the app expects from its identity provider:

- realm ``overwatch`` — self-registration on, e-mail is the username,
  organizations enabled; e-mail verification stays OFF unless SMTP is
  configured (a self-host without a mail relay must not lock users out);
- confidential client ``overwatch`` with the secret from the environment and
  redirect/logout URIs under PUBLIC_BASE;
- the ``organization`` client scope's claim carrying the organization **id**
  (the API keys tenants off that UUID, not the alias).

Runs from up.sh inside the compose network (stdlib only):

    docker compose run --rm --entrypoint python3 \
        -v ./bootstrap_keycloak.py:/b.py api /b.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

KC = os.environ.get("KC_BOOT_BASE", "http://keycloak:8080/auth")
REALM = "overwatch"
PUBLIC_BASE = os.environ["PUBLIC_BASE"].rstrip("/")
ADMIN = os.environ.get("KC_ADMIN_USERNAME", "admin")
ADMIN_PW = os.environ["KC_ADMIN_PASSWORD"]
CLIENT_SECRET = os.environ["OVERWATCH_CLIENT_SECRET"]
SMTP_HOST = os.environ.get("SMTP_HOST", "")


def api(method, path, body=None, token=None, form=None):
    data, headers = None, {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(KC + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except urllib.error.URLError as e:
        # connection refused while Keycloak is still booting — the wait loop's
        # normal diet, never a crash
        return 0, str(e.reason)


def wait_and_login():
    for i in range(60):
        st, body = api("POST", "/realms/master/protocol/openid-connect/token",
                       form={"grant_type": "password", "client_id": "admin-cli",
                             "username": ADMIN, "password": ADMIN_PW})
        if st == 200:
            return body["access_token"]
        time.sleep(5)
    sys.exit(f"keycloak did not come up ({st}): {body}")


def main():
    tok = wait_and_login()
    print("keycloak is up, admin authenticated")

    # --- realm ------------------------------------------------------------
    st, _ = api("GET", f"/admin/realms/{REALM}", token=tok)
    realm = {
        "realm": REALM, "enabled": True,
        "registrationAllowed": True,
        "registrationEmailAsUsername": True,
        "loginWithEmailAllowed": True,
        "resetPasswordAllowed": bool(SMTP_HOST),
        # without a mail relay, verification would lock every user out
        "verifyEmail": bool(SMTP_HOST),
        "organizationsEnabled": True,
        "ssoSessionIdleTimeout": 36000,
    }
    if SMTP_HOST:
        realm["smtpServer"] = {
            "host": SMTP_HOST,
            "port": os.environ.get("SMTP_PORT", "587"),
            "from": os.environ.get("SMTP_FROM", "overwatch@localhost"),
            "fromDisplayName": "Overwatch",
            "starttls": "true", "auth": "true",
            "user": os.environ.get("SMTP_USER", ""),
            "password": os.environ.get("SMTP_PASSWORD", ""),
        }
    if st == 200:
        st2, d = api("PUT", f"/admin/realms/{REALM}", realm, token=tok)
        assert st2 in (200, 204), d
        print(f"realm {REALM}: updated")
    else:
        st2, d = api("POST", "/admin/realms", realm, token=tok)
        assert st2 in (201, 409), d
        print(f"realm {REALM}: created")

    # --- client -----------------------------------------------------------
    client = {
        "clientId": "overwatch", "protocol": "openid-connect",
        "publicClient": False, "secret": CLIENT_SECRET,
        "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
        "redirectUris": [f"{PUBLIC_BASE}/*"],
        "webOrigins": [PUBLIC_BASE],
        "attributes": {"post.logout.redirect.uris": f"{PUBLIC_BASE}/*"},
        "defaultClientScopes": ["profile", "email", "organization"],
    }
    st, existing = api(f"GET", f"/admin/realms/{REALM}/clients?clientId=overwatch",
                       token=tok)
    if st == 200 and existing:
        cid = existing[0]["id"]
        st2, d = api("PUT", f"/admin/realms/{REALM}/clients/{cid}",
                     {**existing[0], **client}, token=tok)
        assert st2 in (200, 204), d
        print("client overwatch: updated")
    else:
        st2, d = api("POST", f"/admin/realms/{REALM}/clients", client, token=tok)
        assert st2 in (201, 409), d
        print("client overwatch: created")

    # --- organization id claim (#140) --------------------------------------
    st, scopes = api("GET", f"/admin/realms/{REALM}/client-scopes", token=tok)
    scope = next((s for s in scopes if s.get("name") == "organization"), None)
    if scope:
        for m in scope.get("protocolMappers", []):
            cfg = m.get("config", {})
            if cfg.get("add.organization.id") != "true":
                cfg["add.organization.id"] = "true"
                st2, d = api("PUT",
                             f"/admin/realms/{REALM}/client-scopes/{scope['id']}"
                             f"/protocol-mappers/models/{m['id']}", m, token=tok)
                assert st2 in (200, 204), d
        print("organization claim carries the id")
    else:
        print("note: no `organization` client scope yet — Keycloak creates it "
              "with the first organization; the API sets the id flag as needed")

    print("\nbootstrap complete — sign-up is open at "
          f"{PUBLIC_BASE}/api/v1/auth/login")


if __name__ == "__main__":
    main()
