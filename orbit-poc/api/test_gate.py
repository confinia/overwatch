"""The internal access gate in front of sandbox and staging (#290).

What replaced what: the two environments used to be behind one `basic_auth`
line in their Caddyfile, carrying a bcrypt hash of a password shared by every
human and every CI job. Nobody could rotate it without a commit, it was pasted
into workflow secrets and local settings files, and the API had to be taught to
ignore the replayed Authorization header (#139).

Now caddy asks an oauth2-proxy (`gate`) whether the visitor has a session in the
shared Keycloak's `overwatch-gate` realm and holds the realm role `internal`.
Passkeys are the point: the realm's browser flow offers a registered passkey
first and a password only as the fallback, so the founder signs in with Touch ID
and there is no shared secret left to leak.

These tests guard the shape of that, end to end and in both environments: the
gate in front, the carve-outs that must stay open, the realm that authenticates
it, and the absence of every trace of the old scheme.
"""
import glob
import json
import os

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..", "..")
ENVS = ("sandbox", "staging")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _caddy(env):
    return _read("orbit-poc", env, "Caddyfile")


def _compose(env):
    return _read("orbit-poc", env, "docker-compose.yml")


GATE_REALM = json.load(open(
    os.path.join(ROOT, "orbit-poc", "v2", "keycloak-config",
                 "overwatch-gate.json"), encoding="utf-8"))


# ---------------------------------------------------------------------------
# The old gate is gone, everywhere
# ---------------------------------------------------------------------------
def test_no_caddyfile_in_the_repo_carries_a_basic_auth_gate():
    """A bcrypt hash in a Caddyfile is a password that cannot be rotated
    without a commit, a review and a deploy. There is no longer one."""
    for path in glob.glob(os.path.join(ROOT, "**", "Caddyfile*"),
                          recursive=True):
        body = open(path, encoding="utf-8").read()
        assert "basic_auth" not in body, f"{path} still gates with basic auth"
        assert "$2a$" not in body and "$2y$" not in body, \
            f"{path} still carries a bcrypt hash"


def test_no_workflow_or_walk_needs_a_shared_gate_password():
    """The password used to travel from GitHub's secret store into every e2e
    run. Both walks now make their own disposable account instead."""
    paths = (glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))
             + [os.path.join(ROOT, "deploy", "e2e_sandbox.py"),
                os.path.join(ROOT, "e2e", "side", "run.sh"),
                os.path.join(ROOT, "e2e", "side", "ci_env.sh"),
                os.path.join(ROOT, "orbit-poc", "simsat", "simsat.py")])
    for path in paths:
        body = open(path, encoding="utf-8").read()
        for needle in ("SANDBOX_BASIC_PASS", "BASIC_PASS", "BASIC_USER",
                       "SIM_BASIC_USER", "GATE_USER="):
            assert needle not in body, f"{os.path.basename(path)}: {needle}"


# ---------------------------------------------------------------------------
# caddy: the gate in front, and what stays open
# ---------------------------------------------------------------------------
def test_both_environments_forward_auth_to_the_gate():
    for env in ENVS:
        c = _caddy(env)
        assert "forward_auth @gated gate:4180" in c, env
        assert "uri /oauth2/auth" in c, env
        # a session-less visitor must be SENT somewhere, not just refused
        assert "/oauth2/start?rd=" in c, env
        # and the gate's own endpoints must be reachable to get there
        assert "handle /oauth2/*" in c, env
        assert "reverse_proxy gate:4180" in c, env


def test_the_snippet_that_gates_is_imported_by_every_host():
    """Both hostnames of both environments, or one of the four is public."""
    for env in ENVS:
        c = _caddy(env)
        assert c.count(f"import {env}_common") == 2, \
            f"{env}: the gate snippet must be imported by both hosts"


def test_machine_and_probe_paths_stay_open_in_both_spellings():
    """A browser session is the wrong credential for these four, so asking for
    one would only break them: the billing provider POSTs the webhook with a
    signature, the deploy probes health before anybody could be signed in, and
    a satellite pushes telemetry with its tenant key."""
    for env in ENVS:
        c = _caddy(env)
        for path in ("/api/v1/billing/webhook*", "/v1/billing/webhook*",
                     "/api/v1/healthz", "/v1/healthz",
                     "/api/v1/tenants/*", "/v1/tenants/*",
                     "/oauth2/*"):
            assert path in c, f"{env}: {path} must be exempt from the gate"


def test_grafana_still_gets_the_authorization_header_stripped():
    """#149: Grafana reads an Authorization header as a login attempt and
    answers 401, ignoring the OIDC cookie that is its real session."""
    for env in ENVS:
        c = _caddy(env)
        assert "header_up -Authorization" in c, env


# ---------------------------------------------------------------------------
# compose: the gate service itself
# ---------------------------------------------------------------------------
def test_each_environment_runs_its_own_gate_against_the_shared_realm():
    for env in ENVS:
        c = _compose(env)
        assert "oauth2-proxy/oauth2-proxy:v" in c, f"{env}: no gate image"
        assert f'OAUTH2_PROXY_CLIENT_ID: "gate-{env}"' in c, env
        assert ("OAUTH2_PROXY_OIDC_ISSUER_URL: "
                '"https://overwatch.confinia.io/auth/realms/overwatch-gate"') in c, env
        # the role is the authorization decision; membership of the realm is not
        assert 'OAUTH2_PROXY_ALLOWED_ROLES: "internal"' in c, env
        # server-to-server legs must not go out through the public name: the VM
        # cannot reach its own edge (hairpin)
        assert "ovw2_keycloak_1:8080" in c, env
        assert 'OAUTH2_PROXY_SKIP_OIDC_DISCOVERY: "true"' in c, env
        assert 'OAUTH2_PROXY_COOKIE_SECURE: "true"' in c, env
        assert "v2net" in c, f"{env}: the gate needs the shared Keycloak network"


def test_the_gate_secrets_come_from_the_stack_env_not_the_compose_file():
    for env in ENVS:
        ex = _read("orbit-poc", env, ".env.example")
        for k in ("OAUTH2_PROXY_CLIENT_SECRET", "OAUTH2_PROXY_COOKIE_SECRET"):
            assert k + "=" in ex, f"{env}/.env.example: {k} missing"
            # documented, never filled in
            assert k + "=change-me" not in ex


def test_no_gate_secret_is_committed_anywhere():
    """Named in a comment is fine, given a value is not: these two reach the
    container through the stack's own .env, which is never in git."""
    for env in ENVS:
        for line in _compose(env).splitlines():
            code = line.split("#", 1)[0]
            for k in ("OAUTH2_PROXY_CLIENT_SECRET", "OAUTH2_PROXY_COOKIE_SECRET"):
                assert f"{k}:" not in code, \
                    f"{env}: {k} must not be given a value in compose: {line}"


# ---------------------------------------------------------------------------
# the realm the gate authenticates against
# ---------------------------------------------------------------------------
def test_the_realm_declares_one_client_per_gated_environment():
    clients = {c["clientId"]: c for c in GATE_REALM["clients"]}
    assert set(clients) == {"gate-sandbox", "gate-staging"}
    for env, c in (("sandbox", clients["gate-sandbox"]),
                   ("staging", clients["gate-staging"])):
        assert c["publicClient"] is False, env      # it holds a secret
        assert c["secret"] == f"$(env:GATE_CLIENT_SECRET_{env.upper()})", \
            f"{env}: the client secret must be env-substituted, never committed"
        # both hostnames of the environment come back through the gate
        assert f"https://{env}.overwatch.confinia.io/oauth2/callback" \
            in c["redirectUris"], env
        assert f"https://{env}.api.overwatch.confinia.io/oauth2/callback" \
            in c["redirectUris"], env
        assert c["attributes"]["pkce.code.challenge.method"] == "S256", env
        assert c["directAccessGrantsEnabled"] is False, \
            f"{env}: a gate client must not hand out tokens for a password"


def test_the_realm_declares_the_role_the_gate_demands():
    roles = [r["name"] for r in GATE_REALM["roles"]["realm"]]
    assert "internal" in roles


def test_a_passkey_is_offered_before_a_password():
    """The whole point of the change: Touch ID, with the password only as the
    fallback for an account that has not registered a key yet."""
    flows = {f["alias"]: f for f in GATE_REALM["authenticationFlows"]}
    assert GATE_REALM["browserFlow"] in flows, "the custom flow is not in use"
    steps = [e for f in flows.values() for e in f["authenticationExecutions"]]
    kinds = [e.get("authenticator") for e in steps]
    assert "webauthn-authenticator-passwordless" in kinds
    assert "auth-password-form" in kinds
    # inside the same subflow, both ALTERNATIVE, passkey first
    sub = next(f for f in flows.values()
               if any(e.get("authenticator") == "webauthn-authenticator-passwordless"
                      for e in f["authenticationExecutions"]))
    execs = sorted(sub["authenticationExecutions"], key=lambda e: e["priority"])
    assert execs[0]["authenticator"] == "webauthn-authenticator-passwordless"
    assert all(e["requirement"] == "ALTERNATIVE" for e in execs), \
        "a REQUIRED step here would demand BOTH a passkey and a password"


def test_registering_a_passkey_is_an_action_the_realm_can_ask_for():
    """How a new person is onboarded: the founder sends the action by e-mail,
    the person registers Touch ID, and no password is ever exchanged."""
    actions = {a["alias"]: a for a in GATE_REALM["requiredActions"]}
    a = actions["webauthn-register-passwordless"]
    assert a["enabled"] is True
    assert a["defaultAction"] is False      # asked for, not forced on everyone


def test_passkeys_must_verify_the_person_and_stay_on_the_device():
    assert GATE_REALM["webAuthnPolicyPasswordlessUserVerificationRequirement"] \
        == "required"
    assert GATE_REALM["webAuthnPolicyPasswordlessRequireResidentKey"] == "Yes"


# ---------------------------------------------------------------------------
# the walks: they sign in like a person, with an account that dies with the run
# ---------------------------------------------------------------------------
def test_the_api_walk_makes_and_removes_its_own_gate_account():
    w = _read("deploy", "e2e_sandbox.py")
    assert "overwatch-gate" in w
    assert "def gate_login(" in w and "def setup_gate_user(" in w
    assert "role-mappings/realm" in w, "the account must be granted the role"
    assert "e2e-gate+" in w              # per-run address, swept if stranded
    assert "prefix='e2e-gate+'" in w
    # no Authorization header anywhere: the api reads one as the caller's
    # identity (#139), which would shadow the session under test
    assert "Basic " not in w


def test_a_stuck_gate_login_says_what_the_page_was():
    """It failed once in five runs (run 36234727653, step 9) with only a URL
    to go on — and that URL is identical whether Keycloak served the login
    form, an expired action, an invalid parameter or a WebAuthn prompt. A
    failure message that cannot be acted on costs another whole run."""
    w = _read("deploy", "e2e_sandbox.py")
    stuck = w[w.index("gate login did not complete"):]
    stuck = stuck[:stuck.index("if st >= 400")]
    for part in ("page title:", "forms:", "text:"):
        assert part in stuck, f"the stuck-gate message must include {part}"
    for helper in ("def _title(", "def _form_ids(", "def _page_text("):
        assert helper in w, helper


def test_the_page_text_helper_strips_markup_and_scripts():
    """A Keycloak page is mostly script and style; a raw slice of the HTML
    would be 300 characters of nothing."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "e2e_gate", os.path.join(ROOT, "deploy", "e2e_sandbox.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    html = ("<html><head><title>Sign in</title><style>.a{color:red}</style>"
            "</head><body><script>var x=1;</script>"
            "<h1>Action expired</h1><p>Please restart.</p></body></html>")
    assert m._title(html) == "Sign in"
    assert m._page_text(html) == "Action expired Please restart."
    assert "var x" not in m._page_text(html)
    assert m._form_ids('<form id="kc-form-login">') == "kc-form-login"


def test_the_browser_walk_signs_in_at_the_gate_before_anything_else():
    side = json.load(open(os.path.join(ROOT, "e2e", "side",
                                       "overwatch-signup-payment.side"),
                          encoding="utf-8"))
    for test in side["tests"]:
        cmds = test["commands"]
        stores = {c["value"] for c in cmds if c["command"] == "store"}
        assert {"GATE_EMAIL", "GATE_PASS"} <= stores, test["name"]
        typed = [c for c in cmds if c["command"] == "type"]
        assert typed[0]["value"] == "${GATE_EMAIL}", \
            f"{test['name']}: the gate login must come first"
        assert any(c["value"] == "${GATE_PASS}" for c in typed), test["name"]
    run = _read("e2e", "side", "run.sh")
    assert "kc gate-create" in run and "kc gate-delete" in run
    assert "load-extension" not in run, \
        "the MV3 header-injecting extension is no longer needed"
    kc = _read("e2e", "side", "kc_admin.py")
    assert "def gate_create(" in kc and "def gate_delete(" in kc


# ---------------------------------------------------------------------------
# CI applies the realm, and a pull request cannot touch the application realms
# ---------------------------------------------------------------------------
def test_the_gate_realm_is_applied_by_the_deploy_and_never_by_a_pull_request():
    """Two properties in one place. An unmerged branch must not be able to
    reconcile Keycloak, because the same cli run also owns live prod auth. And
    the config-cli must only ever be started from the production tree: run from
    anywhere else, podman-compose recreates the shared keycloak container and
    every environment's login goes down while it boots (paid for on 2026-09-25,
    ~40s of 000 on the prod issuer)."""
    deploy = _read(".github", "workflows", "deploy.yml")
    assert "keycloak-config-cli" in deploy
    assert "cd ~/projects/overwatch/orbit-poc/v2" in deploy
    # and detached: an attached `up` hangs on the dependencies podman-compose
    # starts for itself, long after the apply is done
    assert "up -d --no-deps keycloak-config-cli" in deploy
    assert "podman wait ovw2_keycloak-config-cli_1" in deploy
    sandbox = _read(".github", "workflows", "sandbox.yml")
    code = "\n".join(l.split("#", 1)[0]
                     for l in sandbox.splitlines())     # comments may explain it
    assert "keycloak-config-cli" not in code


def test_both_health_checks_prove_the_gate_is_in_front():
    for wf in ("sandbox.yml", "deploy.yml"):
        body = _read(".github", "workflows", wf)
        assert "/oauth2/sign_in*|30" in body or "/oauth2/start*" in body, wf
        assert "v1/healthz" in body, wf
