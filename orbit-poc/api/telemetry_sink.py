"""One telemetry write, routed by scope (#438).

The store is one Postgres, scoped by tenant: public open data and private
per-tenant telemetry share the same (ts, field, value_num, value_txt)
shape and differ only in the key. This module is that shared write, so a
point lands the same way whichever scope it belongs to:

    write_points(cur, PublicSeries(norad),        points)   # open data
    write_points(cur, TenantSeries(tenant, sat),  points)   # private

Today the tenant push (main.py) uses it. The open-data ingest adopts it
next; that step also reconciles the one behaviour difference — the
open-data decoder currently files booleans as text, while this shared
rule (grown from the tenant push) files them as 1/0 in value_num.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PublicSeries:
    """The public scope: a satellite's open-data series, keyed by NORAD."""
    norad: int


@dataclass(frozen=True)
class TenantSeries:
    """A private scope: one tenant's satellite series, keyed by tenant + name."""
    tenant: str
    satellite: str


def flatten(value):
    """Split a telemetry value into (value_num, value_txt). Numbers stay
    graphable in value_num; everything else is text. bool is an int in
    Python, so a boolean lands as 1.0/0.0 — matching the tenant push this
    grew from."""
    if isinstance(value, (int, float)):
        return (float(value), None)
    return (None, str(value))


_SQL = {
    PublicSeries: (
        """INSERT INTO telemetry (norad, ts, field, value_num, value_txt)
           VALUES (%s, %s::timestamptz, %s, %s, %s)
           ON CONFLICT (norad, ts, field) DO UPDATE
           SET value_num = EXCLUDED.value_num, value_txt = EXCLUDED.value_txt""",
        lambda d: (d.norad,)),
    TenantSeries: (
        """INSERT INTO tenant_telemetry
           (tenant, satellite, ts, field, value_num, value_txt)
           VALUES (%s::uuid, %s, %s::timestamptz, %s, %s, %s)
           ON CONFLICT (tenant, satellite, ts, field) DO UPDATE
           SET value_num = EXCLUDED.value_num, value_txt = EXCLUDED.value_txt""",
        lambda d: (d.tenant, d.satellite)),
}


def write_points(cur, dest, points) -> int:
    """Upsert (ts, field, value) points into the destination scope's store.
    Returns the number written. The store is chosen by the scope type, which
    is the whole point: the caller does not know or care which table backs
    which scope."""
    try:
        sql, key = _SQL[type(dest)]
    except KeyError:
        raise TypeError(f"unknown telemetry scope: {dest!r}")
    prefix = key(dest)
    n = 0
    for ts, field, value in points:
        num, txt = flatten(value)
        cur.execute(sql, (*prefix, ts, field, num, txt))
        n += 1
    return n
