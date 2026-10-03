"""Guards #516: the Keycloak pages a user signs in on look like the product,
delivered as config-as-code and rolled out staging-first.

Signing in used to hand the user to stock Keycloak mid-flow, on the one
screen where they decide whether to trust us with a password. The theme is a
child of keycloak.v2 (templates and element ids untouched, so the e2e walks
keep finding id=username / id=password / id=kc-login), mounted into the
shared Keycloak, and assigned per realm in the same files that carry SMTP.
Staging and sandbox get a variant with a "not production" band: one shared
Keycloak serves four realms, and a uniformly branded page would leave the
display name as the only thing between a user and typing production
credentials into a test form.
"""
import json
import os

HERE = os.path.dirname(__file__)
V2 = os.path.join(HERE, "..", "v2")
THEMES = os.path.join(V2, "keycloak-themes")
CONFIG = os.path.join(V2, "keycloak-config")


def _read(*parts):
    return open(os.path.join(*parts), encoding="utf-8").read()


def _props(theme):
    body = _read(THEMES, theme, "login", "theme.properties")
    return dict(l.split("=", 1) for l in body.splitlines()
                if l.strip() and not l.startswith("#") and "=" in l)


def test_the_theme_is_a_child_of_keycloak_v2_and_only_adds_a_stylesheet():
    p = _props("overwatch")
    assert p["parent"] == "keycloak.v2", "templates and ids must come from the parent"
    assert "css/overwatch.css" in p["styles"]
    assert "css/styles.css" in p["styles"], \
        "`styles` replaces the parent's list — its sheet must be named again"
    login = os.path.join(THEMES, "overwatch", "login")
    assert not any(f.endswith(".ftl") for f in os.listdir(login)), \
        "no template overrides: the e2e walks depend on the stock element ids"


def test_the_stylesheet_carries_the_apps_tokens_and_stays_dark():
    css = _read(THEMES, "overwatch", "login", "resources", "css", "overwatch.css")
    index = _read(HERE, "..", "web", "static", "index.html")
    for token in ("#0a0e17", "#121826", "#1f2a3d", "#dfe7f5", "#5aa9ff"):
        assert token in css, f"app token {token} missing from the login theme"
        assert token in index, f"{token} is not (or no longer) an app token"
    assert ".pf-v5-theme-dark" in css and ":root" in css, \
        "the page must be dark whatever the OS prefers"
    assert "--keycloak-bg-logo-url: none" in css


def test_non_production_realms_are_unmistakable():
    p = _props("overwatch-nonprod")
    assert p["parent"] == "overwatch"
    assert "css/nonprod.css" in p["styles"]
    css = _read(THEMES, "overwatch-nonprod", "login", "resources", "css", "nonprod.css")
    assert "NOT PRODUCTION" in css and "position: fixed" in css, \
        "the marker must be text, on screen, not a colour someone may not notice"


def test_no_realm_is_assigned_before_the_mount_exists():
    """Ordering, made explicit. The deploy applies realm settings at every
    stage but never recreates Keycloak (#519), so a `loginTheme` merged in the
    same change as the mount would point a realm at a theme the container
    cannot see yet — a broken login page on three realms until someone does
    the recreate. This change ships the theme and the mount; the recreate is
    a deliberate step; the realm assignments come in their own change after
    it, staging and sandbox first, the gate next, prod last and alone."""
    for realm in ("overwatch-staging", "overwatch-sandbox", "overwatch-gate", "overwatch"):
        r = json.loads(_read(CONFIG, realm + ".json"))
        assert "loginTheme" not in r, \
            f"{realm}: assigned before the theme is mounted — see the docstring"


def test_the_themes_are_mounted_read_only_into_the_shared_keycloak():
    compose = _read(V2, "docker-compose.yml")
    kc = compose[compose.index("\n  keycloak:"):]
    kc = kc[:kc.index("\n  kc-db:") if "\n  kc-db:" in kc[1:] else len(kc)]
    kc = kc[:kc.index("\n  keycloak-config-cli:")] if "\n  keycloak-config-cli:" in kc else kc
    assert "./keycloak-themes:/opt/keycloak/themes:ro" in kc, \
        "the theme directory must reach the container, read-only"
    assert "./keycloak:/opt/keycloak/data/import:ro" in kc, "the import mount must stay"
