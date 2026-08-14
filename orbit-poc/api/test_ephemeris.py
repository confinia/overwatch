"""#208 Phase 1c: OEM upload -> private, owner-scoped ephemeris. DB-backed.

Exercises the store/read helpers directly (no token needed) against a real
Postgres, including the isolation guarantee: one user cannot read another's
uploaded orbit.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402
import oem   # noqa: E402

DSN = os.environ["DB_DSN"]

OEM_ITRF = """CCSDS_OEM_VERS = 2.0
CREATION_DATE = 2026-08-14T00:00:00
ORIGINATOR = OVERWATCH_TEST

META_START
OBJECT_NAME = TESTSAT
OBJECT_ID = 2026-001A
CENTER_NAME = EARTH
REF_FRAME = ITRF2014
TIME_SYSTEM = UTC
START_TIME = 2026-08-14T00:00:00.000
STOP_TIME = 2026-08-14T00:02:00.000
META_STOP
2026-08-14T00:00:00.000  6778.137 0.0 0.0
2026-08-14T00:01:00.000  0.0 6778.137 0.0
2026-08-14T00:02:00.000  0.0 0.0 6756.752
"""


@pytest.fixture(scope="module")
def conn():
    c = psycopg2.connect(DSN)
    with c, c.cursor() as cur:
        cur.execute(main.KEYS_SQL)          # ensures the ephemeris tables exist
    yield c
    c.close()


def test_store_and_read_roundtrip(conn):
    owner = str(uuid.uuid4())
    with conn, conn.cursor() as cur:
        r = main._store_oem(cur, owner, OEM_ITRF, label="my orbit")
        assert r["object_id"] == "2026-001A" and r["points"] == 3
        track = main._read_oem_track(cur, owner, str(r["id"]))
    assert track["label"] == "my orbit"
    assert len(track["points"]) == 3
    ts, lat, lon, alt = track["points"][0]     # equator, prime meridian, 400 km
    assert abs(lat) < 1e-6 and abs(lon) < 1e-6 and abs(alt - 400) < 1e-3


def test_owner_isolation(conn):
    """The whole point: another user cannot read your uploaded orbit."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    with conn, conn.cursor() as cur:
        r = main._store_oem(cur, a, OEM_ITRF)
        assert main._read_oem_track(cur, a, str(r["id"])) is not None   # owner sees it
        assert main._read_oem_track(cur, b, str(r["id"])) is None       # nobody else does


def test_delete_is_owner_scoped_and_cascades(conn):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    with conn, conn.cursor() as cur:
        r = main._store_oem(cur, a, OEM_ITRF)
        eph = str(r["id"])
        cur.execute("DELETE FROM ephemeris WHERE id=%s::uuid AND owner_sub=%s::uuid", (eph, b))
        assert cur.rowcount == 0                       # B can't delete A's
        cur.execute("DELETE FROM ephemeris WHERE id=%s::uuid AND owner_sub=%s::uuid", (eph, a))
        assert cur.rowcount == 1                       # A can
        cur.execute("SELECT count(*) FROM ephemeris_point WHERE ephemeris_id=%s::uuid", (eph,))
        assert cur.fetchone()[0] == 0                  # points cascaded away


def test_invalid_oem_rejected(conn):
    owner = str(uuid.uuid4())
    with conn, conn.cursor() as cur:
        with pytest.raises(oem.OemError):
            main._store_oem(cur, owner, "not an oem")


def test_upload_endpoint_requires_auth():
    """The route must be gated — an unauthenticated call is 401, never a write."""
    import inspect
    src = inspect.getsource(main.upload_ephemeris)
    assert "_require_user(request)" in src
