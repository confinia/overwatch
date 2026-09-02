"""The scope-routed telemetry write (#438): one primitive, two stores.

No DB — a fake cursor records the SQL and params, which is enough to prove
the routing and that the tenant path's behaviour is preserved byte-for-byte.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from telemetry_sink import (PublicSeries, TenantSeries,  # noqa: E402
                            flatten, write_points)


class FakeCur:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))


def test_flatten_matches_the_tenant_push_it_grew_from():
    assert flatten(12.5) == (12.5, None)
    assert flatten(7) == (7.0, None)
    assert flatten("SAFE") == (None, "SAFE")
    # bool is int in Python, so it lands numeric — exactly as the old
    # tenant-push loop did (num = value if isinstance(value,(int,float)))
    assert flatten(True) == (1.0, None)
    assert flatten(False) == (0.0, None)


def test_public_scope_writes_the_public_table():
    c = FakeCur()
    n = write_points(c, PublicSeries(25544),
                     [("2026-09-02T10:00:00Z", "battery_v", 8.1)])
    assert n == 1
    sql, params = c.calls[0]
    assert "INSERT INTO telemetry" in sql and "tenant_telemetry" not in sql
    assert params == (25544, "2026-09-02T10:00:00Z", "battery_v", 8.1, None)


def test_tenant_scope_writes_the_tenant_table_unchanged():
    c = FakeCur()
    tenant = "11111111-1111-1111-1111-111111111111"
    write_points(c, TenantSeries(tenant, "MYSAT"),
                 [("2026-09-02T10:00:00Z", "mode", "SAFE"),
                  ("2026-09-02T10:00:05Z", "temp", -3.5)])
    # same table, same column order, same casts the endpoint used before
    sql0, p0 = c.calls[0]
    assert "INSERT INTO tenant_telemetry" in sql0
    assert "%s::uuid" in sql0 and "%s::timestamptz" in sql0
    assert p0 == (tenant, "MYSAT", "2026-09-02T10:00:00Z", "mode", None, "SAFE")
    _, p1 = c.calls[1]
    assert p1 == (tenant, "MYSAT", "2026-09-02T10:00:05Z", "temp", -3.5, None)


def test_unknown_scope_is_a_typeerror_not_a_silent_drop():
    with pytest.raises(TypeError):
        write_points(FakeCur(), object(), [("t", "f", 1)])
