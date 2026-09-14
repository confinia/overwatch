"""Guards #467: the alert channel must stay readable. ~79 mails hit contact@
in one day — not repeats (repeat_interval was already 24h) but re-fires: the
telemetry freshness window was narrower than the natural pass cadence of a
LoRa-heard satellite (~96 min/orbit), so the rule flapped every pass, and
Grafana 11.2.0 silently drops keepFiringFor, the one hold-down that would
have absorbed it. Sandbox additionally re-mailed on every PR recreate.
"""
import json
import os
import re

HERE = os.path.dirname(__file__)


def _read(*parts):
    return open(os.path.join(HERE, *parts), encoding="utf-8").read()


def _spec():
    return json.loads(_read("..", "grafana", "ops-alerts.json"))


def test_the_telemetry_window_is_wider_than_a_pass_gap():
    rule = next(r for r in _spec()["rules"] if r["uid"] == "telemetry-stalled")
    m = re.search(r"interval '(\d+) (minute|hour)s?'", rule["sql"])
    assert m, "the freshness window must be an explicit interval"
    minutes = int(m.group(1)) * (60 if m.group(2) == "hour" else 1)
    assert minutes >= 240, \
        ("the only live telemetry can arrive once per ~96-minute orbit; a "
         "window under a few passes flaps every orbit and each re-fire mails "
         "fresh — repeat_interval only limits CONTINUOUS firing")


def test_repeat_interval_stays_a_daily_reminder():
    assert _spec()["policy"]["repeat_interval"] == "24h"


def test_sandbox_never_mails():
    sandbox = _read("..", "sandbox", "docker-compose.yml")
    assert 'GF_SMTP_ENABLED: "false"' in sandbox, \
        ("sandbox is recreated on every PR and its alertmanager forgets what "
         "it already sent — its alerts render on dashboards, never in mail")
    # the long-lived stacks keep their mail channel
    for stack in ("docker-compose.yml", os.path.join("staging",
                                                     "docker-compose.yml")):
        assert 'GF_SMTP_ENABLED: "true"' in _read("..", stack), \
            f"{stack} must keep SMTP: repeat_interval holds on a stack that lives"
