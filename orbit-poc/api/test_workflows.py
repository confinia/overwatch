"""Guards issue #143: the CI/CD workflows must address the product user and
must not silently drift from the deploy scripts they wrap.
"""
import os
import re

ROOT = next(p for p in (os.path.join(os.path.dirname(__file__), "..", ".."),
                        os.path.join(os.path.dirname(__file__), ".."))
            if os.path.isdir(os.path.join(p, ".github", "workflows")))
WF = os.path.join(ROOT, ".github", "workflows")


def _wf(name):
    return open(os.path.join(WF, name), encoding="utf-8").read()


def test_all_three_workflows_exist():
    for f in ("ci.yml", "e2e.yml", "deploy.yml"):
        assert os.path.exists(os.path.join(WF, f)), f


def test_no_retired_alias_anywhere():
    for f in os.listdir(WF):
        assert "confinia-ovh-debian" not in _wf(f), f


def test_deploy_targets_the_product_user_not_root_or_debian():
    d = _wf("deploy.yml")
    assert "secrets.VM_USER" in d          # never a hardcoded login
    assert "@debian" not in d and "root@" not in d


def test_unit_tests_need_no_secrets():
    """ci.yml must stay runnable on fork PRs: no secrets, hence no deploy power."""
    assert "secrets." not in _wf("ci.yml")


def test_deploy_runs_the_gate_before_staging():
    d = _wf("deploy.yml")
    assert d.index("run-tests.sh") < d.index("slots.sh stage")


def test_promotion_is_gated_by_an_environment():
    """rule 17: going live stays a human decision."""
    d = _wf("deploy.yml")
    assert re.search(r"environment:\s*production", d)
    assert d.index("needs: stage") < d.index("slots.sh promote")


def test_deploy_verifies_the_security_invariant():
    """#129 must be re-checked after every promotion."""
    d = _wf("deploy.yml")
    assert "tenant_telemetry" in d and "permission denied" in d


def test_e2e_does_not_touch_the_deployment_checkout():
    """#147: deploy.yml owns ~/projects/overwatch on the VM and both workflows
    fire on the same push — sharing it raced on .git/index.lock. The e2e ships
    its script to a scratch path instead."""
    e = _wf("e2e.yml")
    assert "git fetch" not in e and "git checkout" not in e
    assert "e2e-run" in e and "scp" in e


def test_e2e_reaches_keycloak_over_loopback_on_the_vm():
    """A container cannot reach a loopback-bound port, so the e2e runs on the
    host itself with KC_ADMIN_BASE pointing at 127.0.0.1 (#137)."""
    e = _wf("e2e.yml")
    assert "127.0.0.1:8096" in e
    assert "e2e_sandbox.py" in e


def test_sandbox_workflow_builds_from_its_own_checkout():
    """#159: the sandbox must never build from ~/projects/overwatch — that is
    production's tree, owned by deploy.yml (#147 was the same class of bug)."""
    s = _wf("sandbox.yml")
    assert "sandbox-src" in s
    # never build from, or cd into, the production tree
    assert "cd ~/projects/overwatch" not in s
    # the only *executed* reference to it copies the stack's secrets in
    uses = [l for l in s.splitlines()
            if "~/projects/overwatch" in l and not l.lstrip().startswith("#")]
    assert len(uses) == 1 and "sandbox/.env" in uses[0], uses


def test_sandbox_workflow_runs_on_pull_requests_but_not_for_forks():
    s = _wf("sandbox.yml")
    assert "pull_request" in s
    assert "head.repo.full_name == github.repository" in s
    assert "group: sandbox-deploy" in s
