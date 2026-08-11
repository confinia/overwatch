"""Grafana SMTP as code (#183): each stack's grafana service declares the OVH
(EU) SMTP config in its compose, and no stack commits the password.
"""
import os

HERE = os.path.dirname(__file__)
STACKS = {
    "prod":    os.path.join(HERE, "..", "docker-compose.yml"),
    "staging": os.path.join(HERE, "..", "staging", "docker-compose.yml"),
    "sandbox": os.path.join(HERE, "..", "sandbox", "docker-compose.yml"),
}
FROM_NAME = {
    "prod":    'GF_SMTP_FROM_NAME: "Overwatch"',
    "staging": 'GF_SMTP_FROM_NAME: "Overwatch (staging)"',
    "sandbox": 'GF_SMTP_FROM_NAME: "Overwatch (sandbox)"',
}


def test_every_stack_declares_ovh_smtp():
    for name, path in STACKS.items():
        c = open(path, encoding="utf-8").read()
        assert 'GF_SMTP_ENABLED: "true"' in c, f"{name}: SMTP not enabled"
        assert 'GF_SMTP_HOST: "ssl0.ovh.net:587"' in c, f"{name}: not OVH SMTP"
        assert 'GF_SMTP_USER: "alert@confinia.io"' in c, f"{name}: wrong user"
        assert 'GF_SMTP_FROM_ADDRESS: "alert@confinia.io"' in c, f"{name}: wrong from"
        assert 'GF_SMTP_STARTTLS_POLICY: "MandatoryStartTLS"' in c, f"{name}: no starttls"
        assert FROM_NAME[name] in c, f"{name}: wrong/absent From display name"


def test_no_stack_commits_the_smtp_password():
    # a mention in a comment is fine; a `GF_SMTP_PASSWORD:` assignment is not
    for name, path in STACKS.items():
        c = open(path, encoding="utf-8").read()
        assert "GF_SMTP_PASSWORD:" not in c, \
            f"{name}: SMTP password must come from .env, never the compose (Rule 4)"
