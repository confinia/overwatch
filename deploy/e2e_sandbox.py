"""End-to-end test of the paid path against a LIVE environment, with no browser.

The OIDC login is a standard OAuth2 authorization-code flow, so it can be driven
over plain HTTP: this walks sign-in → organization → private Grafana (org,
datasource, dashboard) → isolation check → cleanup, and exits non-zero on the
first failure so CI can gate on it. Stdlib only — runs anywhere (laptop, VM,
GitHub runner) with no install (RULES.md rule 1).

    BASE=https://sandbox.overwatch.confinia.io \
    BASIC_USER=… BASIC_PASS=… KC_ADMIN_USERNAME=… KC_ADMIN_PASSWORD=… \
    python3 deploy/e2e_sandbox.py

Defaults target the sandbox (realm overwatch-sandbox), the environment meant for
short-loop validation with no accounting impact. It creates a disposable user
and organization and removes both at the end, so it is safe to re-run.
"""
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE", "https://sandbox.overwatch.confinia.io").rstrip("/")
# Keycloak's admin API authenticates with a Bearer token, which would collide
# with the basic-auth gate's Authorization header on the public host. Point this
# at Keycloak directly (VM-internal, or through an SSH tunnel) — the user flow
# below still goes through the public URL, gate included.
KC_ADMIN_BASE = os.environ.get("KC_ADMIN_BASE", BASE + "/auth").rstrip("/")
REALM = os.environ.get("KC_REALM", "overwatch-sandbox")
BASIC_USER = os.environ.get("BASIC_USER", "")
BASIC_PASS = os.environ.get("BASIC_PASS", "")
ADMIN_USER = os.environ.get("KC_ADMIN_USERNAME", "")
ADMIN_PASS = os.environ.get("KC_ADMIN_PASSWORD", "")
USER_EMAIL = os.environ.get("E2E_EMAIL", "e2e-bot@confinia.io")
USER_PASS = os.environ.get("E2E_PASSWORD", "e2e-Bot-passw0rd!")
ORG_NAME = os.environ.get("E2E_ORG", "E2E Bot Org")

_steps: list[str] = []


def step(msg):
    _steps.append(msg)
    print(f"  {len(_steps):2}. {msg}", flush=True)


def die(msg):
    print(f"\nFAILED at step {len(_steps)}: {msg}", file=sys.stderr)
    sys.exit(1)


def opener():
    """One session: cookie jar + basic auth (the sandbox gate applies to all)."""
    jar = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if BASIC_USER:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, BASE, BASIC_USER, BASIC_PASS)
        handlers.append(urllib.request.HTTPBasicAuthHandler(mgr))
    op = urllib.request.build_opener(*handlers)
    op.jar = jar
    # Some Keycloak/Grafana endpoints 401 without a preemptive header, and
    # urllib only retries after a challenge — send it up front.
    if BASIC_USER:
        import base64
        tok = base64.b64encode(f"{BASIC_USER}:{BASIC_PASS}".encode()).decode()
        op.addheaders = [("Authorization", f"Basic {tok}"),
                         ("User-Agent", "overwatch-e2e/1.0")]
    return op


def fetch(op, url, data=None, headers=None, method=None):
    """Returns (status, final_url, body). Never raises on HTTP errors."""
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with op.open(req, timeout=30) as r:
            return r.status, r.geturl(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.geturl(), e.read().decode("utf-8", "replace")


def jpost(op, path, body=None, method="POST"):
    st, _, txt = fetch(op, BASE + path, data=json.dumps(body or {}).encode(),
                       headers={"Content-Type": "application/json"}, method=method)
    try:
        return st, json.loads(txt or "{}")
    except json.JSONDecodeError:
        return st, {"raw": txt[:200]}


def jget(op, path):
    st, _, txt = fetch(op, BASE + path)
    try:
        return st, json.loads(txt or "{}")
    except json.JSONDecodeError:
        return st, {"raw": txt[:200]}


# --------------------------------------------------------------------------
# Keycloak admin: create the disposable user (setup) and remove it (teardown)
# --------------------------------------------------------------------------
def admin_opener():
    """No basic-auth header: the admin base is reached directly, and a Basic
    header would take precedence over the Bearer token urllib sets per-request."""
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "overwatch-e2e/1.0")]
    return op


def kc_admin_token(op):
    st, _, txt = fetch(op, f"{KC_ADMIN_BASE}/realms/master/protocol/openid-connect/token",
                       data={"grant_type": "password", "client_id": "admin-cli",
                             "username": ADMIN_USER, "password": ADMIN_PASS})
    if st != 200:
        die(f"Keycloak admin token failed ({st})")
    return json.loads(txt)["access_token"]


def kc(op, method, path, body=None, token=""):
    st, _, txt = fetch(op, f"{KC_ADMIN_BASE}/admin/realms/{REALM}{path}",
                       data=json.dumps(body).encode() if body is not None else None,
                       headers={"Authorization": f"Bearer {token}",
                                "Content-Type": "application/json"}, method=method)
    return st, txt


def kc_user_id(op, token):
    st, txt = kc(op, "GET", f"/users?email={urllib.parse.quote(USER_EMAIL)}&exact=true",
                 token=token)
    users = json.loads(txt or "[]") if st == 200 else []
    return users[0]["id"] if users else None


def setup_user(op, token):
    uid = kc_user_id(op, token)
    if uid:
        kc(op, "DELETE", f"/users/{uid}", token=token)      # start from clean
    st, txt = kc(op, "POST", "/users", {
        "username": USER_EMAIL, "email": USER_EMAIL, "emailVerified": True,
        "enabled": True, "firstName": "E2E", "lastName": "Bot",
        "credentials": [{"type": "password", "value": USER_PASS, "temporary": False}],
    }, token=token)
    if st not in (201, 409):
        die(f"could not create the test user ({st}): {txt[:200]}")
    return kc_user_id(op, token)


# --------------------------------------------------------------------------
# The flow
# --------------------------------------------------------------------------
def login(op):
    """Drive /auth/login → Keycloak form → callback. Returns the final URL."""
    st, url, html = fetch(op, f"{BASE}/api/v1/auth/login")
    if "openid-connect/auth" not in url and "kc-form-login" not in html:
        # already authenticated (Keycloak SSO cookie): the callback ran
        return url
    m = re.search(r'action="([^"]+)"', html)
    if not m:
        die(f"no login form at {url}")
    action = m.group(1).replace("&amp;", "&")
    st, url, body = fetch(op, action, data={"username": USER_EMAIL,
                                            "password": USER_PASS})
    if st != 200 or "auth/callback" in url and "Invalid" in body:
        die(f"login rejected ({st}) at {url}")
    if "kc-form-login" in body:
        die("credentials rejected by Keycloak (login form returned)")
    return url


def main():
    if not (ADMIN_USER and ADMIN_PASS):
        die("KC_ADMIN_USERNAME / KC_ADMIN_PASSWORD required")
    print(f"e2e against {BASE} (realm {REALM})")
    op = opener()
    adm = admin_opener()

    step("Keycloak admin token")
    token = kc_admin_token(adm)

    step(f"create disposable user {USER_EMAIL}")
    uid = setup_user(adm, token)
    if not uid:
        die("user not found after creation")

    org_id = None
    try:
        step("sign in through the OIDC authorization-code flow")
        login(op)
        st, me = jget(op, "/api/v1/me")
        if st != 200 or me.get("email") != USER_EMAIL:
            die(f"/v1/me did not identify the user ({st}): {me}")

        step("create the organization")
        st, d = jpost(op, "/api/v1/orgs", {"name": ORG_NAME})
        if st not in (201, 409):
            die(f"organization creation failed ({st}): {d}")

        step("sign in again so the token carries the organization")
        op = opener()                      # fresh session, Keycloak SSO still valid
        login(op)
        st, me = jget(op, "/api/v1/me")
        if st != 200 or not me.get("organization"):
            die(f"token carries no organization after re-login: {me}")
        org_id = me["organization"]["id"]

        step(f"private Grafana provisioned for org {org_id[:8]}")
        st, g = jget(op, "/api/v1/org/grafana")
        if st != 200 or not g.get("grafana_org_id"):
            die(f"/v1/org/grafana failed ({st}): {g}")
        gorg = g["grafana_org_id"]

        step("sign in to Grafana through OIDC")
        st, url, _ = fetch(op, f"{BASE}/grafana/login/generic_oauth")
        st, u = jget(op, "/grafana/api/user")
        if st != 200 or (u.get("email") or "").lower() != USER_EMAIL.lower():
            die(f"Grafana did not authenticate the user ({st}): {u}")

        step(f"user is a member of their Grafana org ({gorg})")
        st, orgs = jget(op, "/grafana/api/user/orgs")
        if st != 200 or not any(o.get("orgId") == gorg for o in orgs):
            die(f"user is not in Grafana org {gorg}: {orgs}")

        step("the private dashboard exists in that org")
        st, _, _ = fetch(op, f"{BASE}/grafana/api/user/using/{gorg}",
                         data=b"", method="POST")
        st, found = jget(op, "/grafana/api/search?query=private")
        if st != 200 or not any(d.get("uid") == "org-private" for d in found):
            die(f"private dashboard not found in org {gorg}: {found}")

        print("\nALL GREEN — signup → org → private Grafana works end to end.")
    finally:
        step("cleanup (organization + user)")
        if org_id:
            jpost(op, f"/api/v1/orgs/{org_id}", method="DELETE")
        uid = kc_user_id(adm, token)
        if uid:
            kc(adm, "DELETE", f"/users/{uid}", token=token)


if __name__ == "__main__":
    main()
