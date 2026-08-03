"""Guards issue #155: an organization must reach its first ingested point with
no operator action. Five orgs signed up and none pushed a point, because the
page promised a key sent by hand while POST /v1/org/tokens already existed.
"""
import os

HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "..", "web", "static")
ACCOUNT = open(os.path.join(STATIC, "account.html"), encoding="utf-8").read()
PRO = open(os.path.join(STATIC, "pro.html"), encoding="utf-8").read()
MAIN = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()


def test_account_page_issues_keys():
    assert "/api/v1/org/tokens" in ACCOUNT
    assert "mintKey" in ACCOUNT and "renderKeys" in ACCOUNT


def test_account_page_can_revoke():
    assert "revokeKey" in ACCOUNT
    assert '"DELETE"' in ACCOUNT


def test_revoke_endpoint_exists_and_is_scoped_to_the_org():
    assert '@app.delete("/v1/org/tokens/{token}"' in MAIN
    body = MAIN[MAIN.index('def org_token_revoke'):][:600]
    assert "_require_org" in body           # never another org's token
    assert "org = %s::uuid" in body


def test_account_page_shows_how_to_push():
    """The point of the key is the first frame — show the exact command."""
    assert "pushSnippet" in ACCOUNT
    assert "/api/v1/tenants/" in ACCOUNT and "telemetry" in ACCOUNT


def test_pro_page_no_longer_promises_a_human():
    assert "human still presses the button" not in PRO
    assert "arrive by email within the day" not in PRO
    assert "account page" in PRO            # says where the key comes from


def test_account_page_creates_the_organization_itself():
    """#157: the button used to link to '/', which is where the user came from.
    Two of the first seven signups stopped at exactly this step."""
    assert "createOrg" in ACCOUNT
    assert "/api/v1/orgs" in ACCOUNT
    assert '<a class="cta" href="/">Create your organization</a>' not in ACCOUNT


def test_push_snippet_surfaces_the_outcome():
    """#161: a rejected push looked exactly like a successful one — the gate's
    401 has an empty body, so the user saw a blank line and assumed it worked."""
    assert "curl -i" in ACCOUNT                     # status line is printed
    assert "gate-user" in ACCOUNT                   # credentials on gated hosts
    assert "sandbox|staging" in ACCOUNT             # ...only on those hosts
    assert "accepted" in ACCOUNT                    # what success looks like


def test_pro_page_snippet_also_shows_the_outcome():
    assert "curl -i" in PRO
    assert "202" in PRO and "accepted" in PRO
