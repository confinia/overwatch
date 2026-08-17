"""Guards issue #267: the Selenium IDE walk of registration + Polar sandbox payment.

The walk itself needs a browser and a live environment, so it cannot run in the
unit gate. What CAN be guarded here is everything that would make it dangerous
or quietly useless: that it can never point at production, that it carries no
secret, that it asserts against Polar SANDBOX, and that the two encoding traps
already paid for once (below) stay fixed.
"""
import json
import os

ROOT = next(p for p in (os.path.join(os.path.dirname(__file__), "..", ".."),
                        os.path.join(os.path.dirname(__file__), ".."))
            if os.path.isdir(os.path.join(p, "e2e", "side")))
SIDE_DIR = os.path.join(ROOT, "e2e", "side")
PROJECT = os.path.join(SIDE_DIR, "overwatch-signup-payment.side")


def _side():
    return json.load(open(PROJECT, encoding="utf-8"))


def _run_sh():
    return open(os.path.join(SIDE_DIR, "run.sh"), encoding="utf-8").read()


def _commands(test_name):
    return next(t for t in _side()["tests"] if t["name"] == test_name)["commands"]


def test_the_walk_can_never_target_production():
    """It types a card number. Production runs POLAR_ENV=off and real money is
    one misconfigured variable away, so the runner must refuse anything but the
    two gated environments."""
    s = _run_sh()
    assert "sandbox|staging)" in s
    assert "TARGET_ENV must be" in s


def test_no_secret_is_committed_in_the_project():
    """The .side is public. Credentials arrive from the gitignored .env at render
    time; the committed file must hold placeholders only."""
    raw = open(PROJECT, encoding="utf-8").read()
    cfg = {c["value"]: c["target"] for c in _commands("00 config")
           if c["command"] == "store"}
    for var in ("GATE_USER", "GATE_PASS", "EMAIL", "PASS", "ORG"):
        assert cfg[var].startswith("__") and cfg[var].endswith("__"), \
            f"{var} is not a placeholder — a credential may have been committed"
    assert "polar_oat_" not in raw and "polar_" not in raw.lower().replace("polar.sh", "")
    ignored = open(os.path.join(SIDE_DIR, ".gitignore"), encoding="utf-8").read()
    assert ".env" in ignored.split()


def test_every_rendered_variable_is_actually_rendered():
    """A store the runner does not fill would ship its `__PLACEHOLDER__` into a
    live form — the walk would fail late and confusingly."""
    rendered = {"BASE", "GATE_USER", "GATE_PASS", "EMAIL", "PASS", "ORG"}
    placeholders = {c["value"] for c in _commands("00 config")
                    if c["command"] == "store" and c["target"].startswith("__")}
    assert placeholders <= rendered, f"never rendered: {placeholders - rendered}"
    s = _run_sh()
    for var in rendered:
        assert f'"{var}"' in s, f"run.sh does not render {var}"


def test_payment_is_asserted_against_polar_sandbox():
    """The checkout must land on sandbox.polar.sh. assertLocation compares
    literally in selenium-side-runner — a `regexp:` target is taken as a literal
    string and passes nothing — so the host is read and asserted explicitly."""
    cmds = _commands("40 checkout")
    assert not any(c["command"] == "assertLocation" for c in cmds)
    host = next(c for c in cmds
                if c["command"] == "executeScript" and "location.host" in c["target"])
    check = next(c for c in cmds
                 if c["command"] == "assert" and c["target"] == host["value"])
    assert check["value"] == "sandbox.polar.sh"
    assert "4242424242424242" in json.dumps(_side()), "not using the sandbox test card"


def test_capability_args_carry_no_comma():
    """selenium-side-runner splits the -c argument list on commas, so an arg
    like `window-size=1280,900` becomes two broken ones and the driver never
    comes up ('Driver took too long to build')."""
    caps = next(l for l in _run_sh().splitlines() if "chromeOptions.args" in l)
    args = caps.split("chromeOptions.args=[", 1)[1].split("]", 1)[0]
    for a in args.split(","):
        assert "=" not in a or a.count("=") == 1, a
        assert not a.strip().endswith("="), a
    assert "headless" in args, "the CI run must be headless"


def test_sign_in_follows_the_identity_first_flow():
    """The realms ask for the username on one screen and the password on the
    next. Typing both on the first screen silently fails: only #username exists
    there."""
    cmds = _commands("20 sign in")
    order = [(c["command"], c["target"]) for c in cmds]
    u = order.index(("type", "id=username"))
    submit1 = next(i for i, (cmd, t) in enumerate(order)
                   if cmd == "click" and t == "id=kc-login" and i > u)
    wait_pw = next(i for i, (cmd, t) in enumerate(order)
                   if cmd == "waitForElementVisible" and t == "id=password")
    assert u < submit1 < wait_pw, "password typed before the second screen is shown"


def test_the_walk_uses_the_stable_account_page_hooks():
    """The account page carries ids for the walk to aim at; button prose changes,
    ids should not."""
    account = open(os.path.join(ROOT, "orbit-poc", "web", "static", "account.html"),
                   encoding="utf-8").read()
    raw = json.dumps(_side())
    for hook in ("act-signin", "act-create-org", "act-upgrade", "act-portal", "plan"):
        assert f'id="{hook}"' in account, f"account.html lost id={hook}"
        assert hook in raw, f"the walk no longer uses id={hook}"


def test_entitlement_is_asserted_not_just_the_redirect():
    """Polar redirects to success_url on its own. The proof that the money
    landed is the PRO badge, which the app renders from the webhook-driven
    billing status."""
    cmds = _commands("60 entitlement")
    pro = next(c for c in cmds if c["command"] == "assertText" and c["target"] == "id=plan")
    assert pro["value"] == "PRO"


def test_the_run_is_verified_against_polar_itself():
    """A green browser walk with no order in Polar is the bug this exists to
    catch, so the report must fail the run rather than warn."""
    s = _run_sh()
    assert "polar_report.py" in s
    report = open(os.path.join(SIDE_DIR, "polar_report.py"), encoding="utf-8").read()
    assert "sys.exit" in report and "FAIL" in report
