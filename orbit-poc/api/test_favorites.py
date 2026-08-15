"""#221: favourite / focus satellites — owner-scoped add / list / remove.

DB-backed; exercises the store/read helpers directly (no token) including the
isolation guarantee: one user cannot see or remove another's favourites.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402

DSN = os.environ["DB_DSN"]


@pytest.fixture(scope="module")
def conn():
    c = psycopg2.connect(DSN)
    with c, c.cursor() as cur:
        cur.execute(main.KEYS_SQL)                    # ensures user_satellite exists
        cur.execute("INSERT INTO satellite (norad, name) VALUES (99991, 'FAV-TEST') "
                    "ON CONFLICT (norad) DO NOTHING")  # a satellite to favourite (FK)
    yield c
    c.close()


def test_add_list_and_isolation(conn):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    with conn, conn.cursor() as cur:
        main._add_favorite(cur, a, 99991)
        assert [r[0] for r in main._list_favorites(cur, a)] == [99991]
        assert list(main._list_favorites(cur, b)) == []       # B can't see A's
        main._add_favorite(cur, a, 99991)                     # idempotent
        assert len(main._list_favorites(cur, a)) == 1


def test_remove_is_owner_scoped(conn):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    with conn, conn.cursor() as cur:
        main._add_favorite(cur, a, 99991)
        assert main._remove_favorite(cur, b, 99991) == 0      # B can't remove A's
        assert main._remove_favorite(cur, a, 99991) == 1      # A can
        assert list(main._list_favorites(cur, a)) == []


def test_add_endpoint_requires_auth_and_validates():
    import inspect
    src = inspect.getsource(main.add_my_satellite)
    assert "_require_user(request)" in src and "known_norad" in src


def test_me_surfaces_favorites():
    import inspect
    assert "satellites" in inspect.getsource(main.me)
