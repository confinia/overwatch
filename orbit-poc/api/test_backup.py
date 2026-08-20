"""Backup & DR structural tests (#31 encryption, #32 off-site copy).

A cheap guard so the disaster-recovery properties of deploy/backup.sh and
deploy/restore.sh cannot silently regress: dumps are age-encrypted when a
recipient is configured, pushed off-site when a destination is configured,
pruned on a retention window, and each gap warns loudly instead of failing
silent. The real backup/restore round-trip runs on the VM; this asserts the
mechanism is present. run-tests.sh copies deploy/ into the runner."""
import os

# Runner copies the scripts to /tmp/deploy; locally they sit at repo-root deploy/.
_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "..", "deploy"),  # repo root (local)
    "/tmp/deploy",                                                  # gate runner
    os.path.join(os.path.dirname(__file__), "..", "deploy"),        # fallback
]


def _script(name):
    for d in _CANDIDATES:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
    raise AssertionError(f"{name} not found in any known location")


def test_backup_encrypts_when_recipient_present():          # #31
    t = _script("backup.sh")
    assert "age -R" in t, "no age encryption step"
    assert "KEYFILE" in t and "BACKUP_AGE_RECIPIENT" in t


def test_backup_warns_when_plaintext():                     # #31
    t = _script("backup.sh")
    assert "PLAINTEXT" in t, "no loud warning when encryption unconfigured"


def test_backup_pushes_offsite_when_configured():           # #32
    t = _script("backup.sh")
    assert "BACKUP_OFFSITE_DEST" in t, "no off-site destination hook"
    assert "rsync" in t, "no off-site copy mechanism"


def test_backup_warns_when_no_offsite():                    # #32
    t = _script("backup.sh")
    # the DR-3 gap must be surfaced, not silent
    assert "DR-3" in t and "no BACKUP_OFFSITE_DEST" in t


def test_backup_has_retention():                            # #25 hygiene
    t = _script("backup.sh")
    assert "-mtime +14 -delete" in t, "no retention window"


def test_restore_decrypts_age():                            # #31 round-trip
    t = _script("restore.sh")
    assert "*.age" in t and "age -d" in t
    assert "BACKUP_AGE_IDENTITY" in t


def test_a_dump_is_verified_before_it_counts():   # incident 2026-08-19
    """When the stack moved to its own Unix user, `podman exec` failed for the
    unit still running as `debian` — but the gzip in the pipeline kept writing
    a valid EMPTY archive (20 bytes). Seventeen nightly 'backups' were empty
    and nothing said so. The dump must therefore verify what it produced:
    write aside, test the archive, check it is non-trivial, publish only then,
    and fail the run otherwise so systemd marks the unit failed."""
    s = _script("backup.sh")
    assert 'tmp="$out.part"' in s, "dumps must not be published before verification"
    assert "gzip -t" in s, "a corrupt archive must be detected"
    assert "MIN_BYTES" in s and "an empty archive is not a backup" in s
    assert "mv \"$tmp\" \"$out\"" in s, "publish only after the checks"
    # and the failure must propagate: the script runs under set -e
    assert "set -euo pipefail" in s


def test_backup_captures_cluster_roles():   # incident 2026-08-19
    """A pg_dump of a database contains no roles. Our per-org RLS roles
    (org_<hex>) and service roles (grafana_ro, ops_ro, orbit_app) are cluster
    objects, and the data dumps are full of OWNER/GRANT statements naming
    them — so a restore onto a fresh Postgres dies on the first missing role.
    A rehearsal of the 2026-08-02 dump proved it: it would not load."""
    s = _script("backup.sh")
    assert "pg_dumpall" in s and "--globals-only" in s
    assert "globals orbit-poc_db_1" in s and "globals ovw2_kc-db_1" in s


def test_a_restore_rehearsal_exists_and_never_touches_live_data():
    """A dump that has never been restored is a belief, not a backup — and
    restore.sh loads into the LIVE database behind a confirmation prompt, so
    it is never exercised."""
    s = _script("restore-rehearsal.sh")
    # A rehearsal that runs in the environment it is meant to replace tests
    # the environment, not the backup: restoring into a scratch database
    # inside the LIVE cluster passes even when roles are missing, because
    # they still exist there. It must use a throwaway cluster.
    assert "podman run -d --name" in s and "podman rm -f" in s
    assert "FRESH" in s
    # the globals must be LOADED, not grepped — in an empty cluster a missing
    # role is a hard error on the first OWNER/GRANT, as in a real recovery
    assert "globals loaded" in s
    assert "roles are NOT backed up" in s
    # never mistake the roles file for the data dump
    assert "-globals-" in s and "grep -v" in s
    # and an empty dump must fail BEFORE any check that would pass vacuously
    assert s.index("nothing to restore") < s.index("globals loaded")
    # a slow disk must not read as a broken backup: the wait is configurable
    # and its failure says so (a false negative is what gets a rehearsal
    # abandoned)
    assert "RH_READY_TRIES" in s
    assert "NOT a verdict on the backup" in s
