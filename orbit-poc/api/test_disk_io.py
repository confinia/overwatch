"""Guards the VM-wide IOPS work: the box is saturated on two 7200rpm disks
(325 writes/s, 25% io pressure) while moving 1.5 MB/s. Every write is a seek,
and seeks are shared by eleven products.

The rule these defend: **non-production may trade durability for seeks;
production may not.** Losing acknowledged customer commits is a different
category of problem from losing 600ms of sandbox fixtures.
"""
import os
import re

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..", "..")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _db_block(env):
    """The `db:` service block, up to the next service key."""
    body = _read("orbit-poc", env, "docker-compose.yml")
    start = body.index("\n  db:\n") + 1
    rest = body[start:]
    nxt = re.search(r"\n  [a-z][a-z0-9_-]*:\n", rest[5:])
    return rest if not nxt else rest[:5 + nxt.start()]


def test_non_production_does_not_wait_for_the_platter():
    for env in ("sandbox", "staging"):
        block = _db_block(env)
        assert "synchronous_commit=off" in block, \
            f"{env} db should not fsync every commit on a saturated disk"


def test_production_still_waits_for_the_platter():
    """The whole point of the split. A promote must never carry this to prod
    by copy-paste, so it is asserted rather than remembered."""
    prod = _read("orbit-poc", "docker-compose.yml")
    code = "\n".join(l.split("#", 1)[0] for l in prod.splitlines())
    assert "synchronous_commit" not in code, \
        "production keeps the durable default"


def test_fsync_is_never_turned_off_anywhere():
    """A different setting entirely, and the dangerous one: fsync=off can
    leave an unrecoverable cluster. Not in production, not in staging, not in
    the sandbox, not in CI."""
    for path in ("orbit-poc/docker-compose.yml",
                 "orbit-poc/sandbox/docker-compose.yml",
                 "orbit-poc/staging/docker-compose.yml",
                 "orbit-poc/v2/docker-compose.yml",
                 "selfhost/docker-compose.yml",
                 "deploy/run-tests.sh"):
        full = os.path.join(ROOT, *path.split("/"))
        if not os.path.exists(full):
            continue
        body = open(full, encoding="utf-8").read()
        code = "\n".join(l.split("#", 1)[0] for l in body.splitlines())
        assert "fsync=off" not in code, f"{path}: fsync=off can lose the cluster"


def test_the_ci_database_never_touches_the_disk():
    """It is created for one run and removed by the exit trap, so there is
    nothing to persist — and the stage runs the full suite on every deploy."""
    sh = _read("deploy", "run-tests.sh")
    assert "--tmpfs /var/lib/postgresql/data" in sh
    # and it must still be torn down, or a RAM-backed database leaks memory
    assert "podman rm -f \"$PG\"" in sh
