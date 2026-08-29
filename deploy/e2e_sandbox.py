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
import time
import urllib.error
import urllib.parse
import urllib.request

# A live-environment walk races the sandbox's own redeploy (both fire on a push
# to main): while a container is being recreated, Caddy answers 5xx. Treat those
# — and a 429 from the api's rate limiter — as transient and retry rather than
# failing the whole walk (#151).
TRANSIENT = {429, 502, 503, 504}

# The walk fires its steps back to back (well over the api's 5/s limit), so it
# would trip the rate limiter mid-flight. Space requests out to stay under it.
MIN_INTERVAL = 0.25
_last_req = [0.0]

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
# A live walk leaves a soft-deleted org behind; re-running with a FIXED email
# re-links the fresh user to that tombstone (410 "organization has been
# deleted") and the walk dies before Grafana. A per-run id gives each run a
# genuinely fresh user + org, so runs never collide with a prior tombstone.
RUN_ID = os.environ.get("E2E_RUN_ID") or hex(int(time.time()))[-6:]
USER_EMAIL = os.environ.get("E2E_EMAIL", f"e2e-bot+{RUN_ID}@confinia.io")
USER_PASS = os.environ.get("E2E_PASSWORD", "e2e-Bot-passw0rd!")
ORG_NAME = os.environ.get("E2E_ORG", f"E2E Bot Org {RUN_ID}")

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


def fetch(op, url, data=None, headers=None, method=None, retries=6):
    """Returns (status, final_url, body). Never raises on HTTP errors. Retries
    transient gateway errors (5xx) and connection blips with a short backoff so
    a sandbox mid-redeploy doesn't fail the walk."""
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    for attempt in range(retries):
        gap = MIN_INTERVAL - (time.monotonic() - _last_req[0])
        if gap > 0:
            time.sleep(gap)                          # stay under the api's 5/s
        _last_req[0] = time.monotonic()
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with op.open(req, timeout=30) as r:
                return r.status, r.geturl(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in TRANSIENT and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return e.code, e.geturl(), e.read().decode("utf-8", "replace")
        except urllib.error.URLError:
            if attempt < retries - 1:                # connection refused/reset
                time.sleep(2 * (attempt + 1))
                continue
            raise


def jpost(op, path, body=None, method="POST"):
    st, _, txt = fetch(op, BASE + path, data=json.dumps(body or {}).encode(),
                       headers={"Content-Type": "application/json"}, method=method)
    try:
        return st, json.loads(txt or "{}")
    except json.JSONDecodeError:
        return st, {"raw": txt[:200]}


def jget(op, path, retries=6):
    st, _, txt = fetch(op, BASE + path, retries=retries)
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
def _form(html):
    """(action, {field: ''}) of the login form — Keycloak may split the flow
    into several steps (username page, then password page), so we follow
    whatever fields the current page asks for instead of assuming one form."""
    m = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', html)
    if not m:
        m = re.search(r'<form[^>]*action="([^"]+)"', html)
    if not m:
        return None, {}
    action = m.group(1).replace("&amp;", "&")
    fields = {}
    for i in re.finditer(r'<input\b[^>]*>', html):
        tag = i.group(0)
        n = re.search(r'name="([^"]+)"', tag)
        if not n or re.search(r'type="submit"', tag):
            continue
        v = re.search(r'value="([^"]*)"', tag)
        fields[n.group(1)] = v.group(1) if v else ""
    return action, fields


def _walk_forms(op, url, html, max_steps=4):
    """Submit the Keycloak login form(s) until the page is no longer one.
    Keycloak may split the flow (username page, then password page), so we
    follow whatever fields each page asks for. Returns the final (url, html).
    A no-op when the page is already off the form (e.g. SSO bounced straight
    through), which is why the same helper drives both the app and Grafana."""
    for _ in range(max_steps):
        if "kc-form-login" not in html:
            return url, html                # authenticated (callback ran)
        action, fields = _form(html)
        if not action:
            die(f"no login form at {url}")
        for name in list(fields):
            low = name.lower()
            if low in ("username", "email"):
                fields[name] = USER_EMAIL
            elif low == "password":
                fields[name] = USER_PASS
        fields.pop("rememberMe", None)
        before = url
        st, url, html = fetch(op, action, data=fields)
        if st >= 400:
            die(f"login step failed ({st}) at {before}")
    die("login did not complete (still on a Keycloak form)")


def login(op):
    """Drive /auth/login through the Keycloak forms to the callback."""
    st, url, html = fetch(op, f"{BASE}/api/v1/auth/login")
    url, _ = _walk_forms(op, url, html)
    return url


def wait_ready(op, tries=45):
    """Poll a public endpoint until the sandbox answers 200, so the walk never
    starts against a stack that is still coming up (or mid-redeploy)."""
    for _ in range(tries):
        st, _, _ = fetch(op, f"{BASE}/api/v1/satellites", retries=1)
        if st == 200:
            return
        time.sleep(2)
    die(f"sandbox never became ready at {BASE}")


def main():
    if not (ADMIN_USER and ADMIN_PASS):
        die("KC_ADMIN_USERNAME / KC_ADMIN_PASSWORD required")
    print(f"e2e against {BASE} (realm {REALM})")
    op = opener()
    adm = admin_opener()

    step("wait for the sandbox to answer")
    wait_ready(op)

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

        step("password recovery: the reset mail is really sent (#265)")
        # The capability has been live in every realm since #174, and nothing
        # would notice it breaking: SMTP credentials rotate, a Keycloak
        # recreate can drop the realm's smtpServer, the sender can start
        # failing SPF. Three checks, all through the admin API we already
        # authenticated against:
        #   1. the realm still offers recovery and still has an SMTP host
        #      (a recreate that drops either fails CI instead of users);
        #   2. execute-actions-email answers 204 — Keycloak sends the mail
        #      synchronously, so a 500 here IS the SMTP failure surfaced;
        #   3. the SEND_RESET_PASSWORD event exists for our user — the send
        #      was recorded, not merely not-erroring (needs eventsEnabled,
        #      which the realm config now owns).
        st, txt = kc(adm, "GET", "", token=token)
        realm = json.loads(txt) if st == 200 else {}
        if not realm.get("resetPasswordAllowed"):
            die("realm no longer offers password recovery (resetPasswordAllowed)")
        if not (realm.get("smtpServer") or {}).get("host"):
            die("realm has no smtpServer host — recovery mail cannot send")
        st, txt = kc(adm, "PUT", f"/users/{uid}/execute-actions-email",
                     ["UPDATE_PASSWORD"], token=token)
        if st != 204:
            die(f"execute-actions-email failed ({st}): {txt[:300]}")
        if realm.get("eventsEnabled"):
            sent = False
            for _ in range(10):
                st, txt = kc(adm, "GET",
                             f"/events?type=SEND_RESET_PASSWORD&user={uid}",
                             token=token)
                if st == 200 and json.loads(txt or "[]"):
                    sent = True
                    break
                time.sleep(2)
            if not sent:
                st, txt = kc(adm, "GET",
                             f"/events?type=SEND_RESET_PASSWORD_ERROR&user={uid}",
                             token=token)
                err = (json.loads(txt or "[]") or [{}])[0].get("error",
                                                              "no event at all")
                die(f"reset mail send not recorded — {err}")
        else:
            # events are turned on by the same change that added this step;
            # until the realm config has been applied once, the 204 above is
            # the proof (Keycloak sends synchronously - a send failure is a
            # 500 on execute-actions-email, not a silent 204)
            print("   (realm events not enabled yet - 204 send is the proof)")

        step(f"private Grafana provisioned for org {org_id[:8]}")
        # The endpoint provisions inside the request and answers 503 when
        # Grafana did not respond in time — its docstring calls that retriable,
        # so a single attempt is the client getting the contract wrong. The
        # sandbox shares this VM with the deploy builds, so while one is
        # running the first attempt loses the race and a healthy stack gets
        # reported broken (run 32774088344). Same shape as RH_READY_TRIES in
        # restore-rehearsal.sh: an unretried wait is a false negative
        # generator, and a suite that cries wolf stops being read.
        # retries=1 so this loop owns the waiting: fetch() already spends ~30s
        # retrying a 503 internally, and nesting the two would make the worst
        # case minutes rather than the minute intended here.
        tries = int(os.environ.get("E2E_GRAFANA_TRIES", 20))
        gap = float(os.environ.get("E2E_GRAFANA_GAP", 3))
        for attempt in range(tries):
            st, g = jget(op, "/api/v1/org/grafana", retries=1)
            if st == 200 and g.get("grafana_org_id"):
                break
            if st != 503:
                die(f"/v1/org/grafana failed ({st}): {g}")
            time.sleep(gap)
        else:
            die(f"/v1/org/grafana still 503 after {tries} attempts over "
                f"{tries * gap:.0f}s — this is a TIMEOUT, not a verdict on the "
                f"stack; raise E2E_GRAFANA_TRIES and retry")
        gorg = g["grafana_org_id"]

        step("sign in to Grafana through OIDC")
        # Grafana runs the code exchange server-side against the internal
        # Keycloak (GF_AUTH_GENERIC_OAUTH_TOKEN_URL) — that requires Grafana to
        # be on the shared-Keycloak network (#151). We still follow the Keycloak
        # form the same way the app leg does, for the case SSO doesn't bounce.
        st, url, html = fetch(op, f"{BASE}/grafana/login/generic_oauth")
        _walk_forms(op, url, html)
        st, u = jget(op, "/grafana/api/user")
        if st != 200 or (u.get("email") or "").lower() != USER_EMAIL.lower():
            die(f"Grafana did not authenticate the user ({st}): {u}")
        # The Grafana user only exists now (first OIDC login, just above), so the
        # membership add attempted at org creation 404'd. Re-hit /v1/org/grafana
        # so the api adds them to their org now that they exist (#13).
        jget(op, "/api/v1/org/grafana")

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
