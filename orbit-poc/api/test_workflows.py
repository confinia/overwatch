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


def test_e2e_walker_follows_grafana_form_and_survives_redeploys():   # #151
    """The Grafana OIDC leg must follow the Keycloak login form (not a bare
    fetch), and the walk must tolerate the sandbox being mid-redeploy — else CI
    goes red on a race rather than a real regression."""
    w = open(os.path.join(ROOT, "deploy", "e2e_sandbox.py"), encoding="utf-8").read()
    assert w.count("_walk_forms(") >= 2                   # app leg + Grafana leg
    grafana = w[w.index("generic_oauth"):]
    assert "_walk_forms(" in grafana[:400]               # right after the Grafana fetch
    assert "TRANSIENT" in w and "502" in w and "429" in w   # transient 5xx + rate limit
    assert "wait_ready(" in w                            # and wait for readiness first
    assert "MIN_INTERVAL" in w                           # throttled under the api's 5/s


def test_a_skipped_chained_deploy_cannot_cancel_a_real_one():   # #263
    """Concurrency is evaluated BEFORE the job `if`, so a chained run whose
    sandbox rebuild failed would join deploy-production, cancel the deploy in
    flight, and only then skip — promotion silently no-ops. A doomed run must
    get its own throwaway group instead."""
    d = _wf("deploy.yml")
    # the expression spans several lines for readability — read the whole
    # concurrency block, not just the first line after `group:`
    blk = re.search(r"^concurrency:\n(?:[ \t].*\n)+", d, re.M).group(0)
    grp = blk[blk.index("group:"):]
    assert "${{" in grp, "concurrency group is a constant — a skipped run will cancel a real one"
    assert "deploy-skip-" in grp and "github.run_id" in grp   # unique, throwaway
    assert "deploy-production" in grp                          # real deploys still serialise
    assert "workflow_run.conclusion != 'success'" in grp


def test_deploy_does_not_deadlock_on_a_pending_approval():   # #203
    """The promote job waits on the production reviewer. With
    cancel-in-progress:false a single un-actioned approval sits in `waiting`
    holding the concurrency group forever, wedging every later deploy (0 jobs,
    pending). Cancelling the stale run instead keeps the pipeline alive."""
    d = _wf("deploy.yml")
    assert re.search(r"group:\s*deploy-production", d)        # serialized...
    assert re.search(r"cancel-in-progress:\s*true", d)        # ...but never wedged


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


def test_e2e_runs_after_the_sandbox_redeploy_not_racing_it():   # #151
    """On a push to main, 'sandbox (per PR)' recreates the sandbox while the e2e
    walks it — a mid-flight 502. The e2e must chain off that deploy completing
    (workflow_run), not the push, and skip when the deploy failed."""
    e = _wf("e2e.yml")
    assert "workflow_run:" in e
    assert re.search(r'workflows:\s*\[\s*"sandbox \(per PR\)"\s*\]', e)
    assert "on:\n  push:" not in e                    # no longer races the push
    assert "workflow_run.conclusion == 'success'" in e


def test_e2e_reaches_keycloak_over_loopback_on_the_vm():
    """A container cannot reach a loopback-bound port, so the e2e runs on the
    host itself with KC_ADMIN_BASE pointing at 127.0.0.1 (#137)."""
    e = _wf("e2e.yml")
    assert "127.0.0.1:12070" in e
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


def test_registration_alert_e2e_wired():         # #193
    wf = _wf("e2e-registration-alert.yml")
    assert "workflow_dispatch" in wf
    assert "secrets.VM_SSH_KEY" in wf and "secrets.VM_USER" in wf   # reuse deploy secrets
    assert "e2e_registration_alert.sh" in wf                        # ships the script
    for opt in ("staging", "sandbox", "production"):
        assert opt in wf
    s = open(os.path.join(ROOT, "deploy", "e2e_registration_alert.sh"),
             encoding="utf-8").read()
    assert "rule_uid=new-registration" in s and \
           "Sending alerts to local notifier" in s                 # asserts the send
    assert "DELETE FROM registered_user" in s                      # cleans up


def test_production_deploy_runs_after_the_sandbox_is_updated():
    """Sandbox first, production second. Firing both on the same push meant a
    commit could reach the production candidate before it existed anywhere
    validatable; chaining deploy on the sandbox rebuild guarantees the sandbox
    already carries the commit, and a failed sandbox blocks the prod candidate."""
    d = _wf("deploy.yml")
    assert "workflow_run:" in d
    assert re.search(r'workflows:\s*\[\s*"sandbox \(per PR\)"\s*\]', d)
    assert "on:\n  push:" not in d               # no longer races the sandbox
    assert "workflow_run.conclusion == 'success'" in d
    assert re.search(r"environment:\s*production", d)   # promote still gated


def test_deploy_applies_grafana_dashboards_and_ingest():
    """The core singletons come up with --no-recreate, so an edited dashboard or
    a changed ingest would sit in the repo and never reach production unless
    someone touched the VM by hand. CI must apply both."""
    d = _wf("deploy.yml")
    assert "provisioning/dashboards/reload" in d     # boards re-read, no restart
    assert "--build ingest" in d                     # ingest follows main
