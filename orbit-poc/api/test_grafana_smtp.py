"""Grafana SMTP as code (#183): each long-lived stack's grafana service
declares the OVH (EU) SMTP config in its compose, and no stack commits the
password. The sandbox deliberately has NO mail channel (#467): it is
recreated on every PR and its alertmanager forgets what it already sent, so
its alerts render on dashboards but never reach contact@.
"""
import os

HERE = os.path.dirname(__file__)
STACKS = {
    "prod":    os.path.join(HERE, "..", "docker-compose.yml"),
    "staging": os.path.join(HERE, "..", "staging", "docker-compose.yml"),
    "sandbox": os.path.join(HERE, "..", "sandbox", "docker-compose.yml"),
}
MAILING = ("prod", "staging")
FROM_NAME = {
    "prod":    'GF_SMTP_FROM_NAME: "Overwatch"',
    "staging": 'GF_SMTP_FROM_NAME: "Overwatch (staging)"',
}


def test_every_mailing_stack_declares_ovh_smtp():
    for name in MAILING:
        c = open(STACKS[name], encoding="utf-8").read()
        assert 'GF_SMTP_ENABLED: "true"' in c, f"{name}: SMTP not enabled"
        assert 'GF_SMTP_HOST: "ssl0.ovh.net:587"' in c, f"{name}: not OVH SMTP"
        assert 'GF_SMTP_USER: "alert@confinia.io"' in c, f"{name}: wrong user"
        assert 'GF_SMTP_FROM_ADDRESS: "alert@confinia.io"' in c, f"{name}: wrong from"
        assert 'GF_SMTP_STARTTLS_POLICY: "MandatoryStartTLS"' in c, f"{name}: no starttls"
        assert FROM_NAME[name] in c, f"{name}: wrong/absent From display name"


def test_the_sandbox_stays_mail_silent():                        # #467
    c = open(STACKS["sandbox"], encoding="utf-8").read()
    assert 'GF_SMTP_ENABLED: "false"' in c, \
        "sandbox alerts must never mail: every PR recreate re-sends the world"
    assert 'GF_SMTP_ENABLED: "true"' not in c


def test_no_stack_commits_the_smtp_password():
    # a mention in a comment is fine; a `GF_SMTP_PASSWORD:` assignment is not
    for name, path in STACKS.items():
        c = open(path, encoding="utf-8").read()
        assert "GF_SMTP_PASSWORD:" not in c, \
            f"{name}: SMTP password must come from .env, never the compose (Rule 4)"
