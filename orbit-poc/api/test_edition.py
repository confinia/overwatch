"""Guards issue #276: cloud vs self-host editions.

Payments exist ONLY in the cloud edition. The default — an operator who copies
the compose profile and sets nothing — is the paymentless one: every
/v1/billing/* answers 404 (webhook included), so a leaked cloud .env cannot
arm a checkout on-prem and a self-host Caddyfile has nothing to exempt.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..", "..")


def test_the_default_edition_is_selfhost():
    """Unset env (this test run) must resolve to the paymentless edition."""
    assert os.environ.get("OVERWATCH_EDITION") is None
    assert main.EDITION == "selfhost"


def test_every_billing_route_is_guarded():
    """Each /v1/billing/* endpoint must call _cloud_only() before anything
    else — a new billing route without the guard would quietly exist on-prem."""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    routes = re.findall(
        r'@app\.\w+\("(/v1/billing[^"]*)"[^)]*\)\s*\n(?:async )?def (\w+)'
        r'\([^)]*\):\n(?:\s+"""(?:.|\n)*?"""\n)?(\s+\S+)', src)
    assert len(routes) >= 6, [r[0] for r in routes]
    for path, fn, first in routes:
        assert "_cloud_only()" in first, f"{path} ({fn}) is not edition-guarded"


def test_selfhost_answers_404_on_billing(monkeypatch):
    monkeypatch.setattr(main, "EDITION", "selfhost")
    try:
        main.billing_mode()
        assert False, "billing_mode answered in the self-host edition"
    except main.HTTPException as e:
        assert e.status_code == 404


def test_cloud_keeps_the_billing_surface(monkeypatch):
    monkeypatch.setattr(main, "EDITION", "cloud")
    mode = main.billing_mode()
    assert "provider" in mode and "env" in mode


def test_our_stacks_declare_cloud_explicitly():
    """selfhost-by-default only works if OUR composes opt in to cloud."""
    for f in ("orbit-poc/docker-compose.blue.yml",
              "orbit-poc/docker-compose.green.yml",
              "orbit-poc/staging/docker-compose.yml",
              "orbit-poc/sandbox/docker-compose.yml"):
        s = open(os.path.join(ROOT, f), encoding="utf-8").read()
        assert 'OVERWATCH_EDITION: "cloud"' in s, f


def test_account_page_hides_the_plan_ui_without_billing():
    """A 404 from billing/status means self-host: no plan row, no upgrade, no
    portal, no 'billing unavailable' apology — and never an error."""
    html = open(os.path.join(HERE, "..", "web", "static", "account.html"),
                encoding="utf-8").read()
    assert "billingOn = false" in html and "r.status === 404" in html
    assert "!billingOn ? ``" in html
    assert "} else if (billingOn) {" in html
    assert "if (billingOn) rows.push(" in html


def test_selfhost_profile_never_names_a_billing_credential():
    """The compose profile an operator copies must not even mention billing
    env vars — a leaked cloud .env pasted next to it stays inert because the
    profile passes nothing through and the edition defaults to selfhost."""
    compose = open(os.path.join(ROOT, "selfhost", "docker-compose.yml"),
                   encoding="utf-8").read()
    for needle in ("CREEM_", "POLAR_", 'OVERWATCH_EDITION: "cloud"'):
        assert needle not in compose, needle
    env = open(os.path.join(ROOT, "selfhost", ".env.example"),
               encoding="utf-8").read()
    assert "CREEM" not in env and "POLAR" not in env


def test_selfhost_caddy_has_no_gate_and_no_webhook_carveout():
    """Directives only — comments may explain the absence they guarantee."""
    lines = [l.split("#", 1)[0] for l in
             open(os.path.join(ROOT, "selfhost", "Caddyfile"),
                  encoding="utf-8")]
    body = "\n".join(lines)
    assert "basic_auth" not in body
    assert "webhook" not in body.lower()


def test_selfhost_boot_proof_asserts_the_404s():
    """The CI boot job must fail if a billing route ever answers on-prem."""
    wf = open(os.path.join(ROOT, ".github", "workflows", "selfhost.yml"),
              encoding="utf-8").read()
    assert "/api/v1/billing/mode" in wf and "/api/v1/billing/webhook" in wf
    assert '"404"' in wf
    assert "./up.sh" in wf, "CI must boot exactly the way the README says"
