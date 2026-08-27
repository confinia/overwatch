"""Refuse to let a database-writing test touch a real database.

Several suites here write through the real code path — `test_registrations.py`
inserts into `registered_user` and does NOT clean up. Pointed at production,
which is one `--env-file orbit-poc/.env` away, that puts fake signups in the
live registry and fires the new-user alert at the founder.

It happened. Eight `a@example.org` accounts reached production between
2026-08-25 and 2026-08-27, each one e-mailed as a new registration, and the
question they prompted was "our CI/CD, or a real attack?" — neither, just a
test suite aimed at the wrong database.

deploy/run-tests.sh and ci.yml stand up a throwaway Postgres and set
OVERWATCH_TEST_DB=1. Anything else asking for a database is refused, loudly,
rather than quietly writing somewhere real.
"""
import os

import pytest

MARKER = "OVERWATCH_TEST_DB"


def require_test_db():
    """Called by every fixture that hands out a writable connection."""
    if os.environ.get(MARKER) == "1":
        return
    dsn = os.environ.get("DB_DSN", "")
    pytest.fail(
        f"refusing to run a database test without {MARKER}=1.\n"
        f"DB_DSN points at: {dsn.split('host=')[-1].split()[0] if 'host=' in dsn else dsn or '(unset)'}\n"
        "These suites write through the real code path and do not always clean "
        "up. Use deploy/run-tests.sh, which creates a throwaway Postgres and "
        "sets the marker.",
        pytrace=False)
