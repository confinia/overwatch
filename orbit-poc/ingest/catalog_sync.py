"""Reconcile our catalogue mirror after a COMPLETE SatNOGS bulk pass (#384).

The SatNOGS DB merges temporary NORAD IDs (98xxx) into the confirmed one BY
HAND, entry by entry — fredy, on the HUCSat thread: "unfortunately this needs
to be done manually … all the data will be associated with the merged entry."
Our refresh was upsert-only, so every one of those merges would have left a
ghost here: the dead norad still in the picker, the same sat_id under two
numbers, and anything a user keyed to the old norad stranded. 629 of 2,768
rows carry temporary IDs today; each is a future manual merge.

Standalone module (no ingest imports) so the api test container can exercise
it against a real database without pulling requests/sgp4.
"""


def norad_keyed_tables(cur):
    """Every table with a `norad` column, discovered live — a future table
    added with that column joins the migration without anyone remembering
    this module exists. `catalog` is handled explicitly (the old row must
    go, not move)."""
    cur.execute("""SELECT table_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND column_name = 'norad'
                     AND table_name <> 'catalog'""")
    return [r[0] for r in cur.fetchall()]


def _copy_satellite_forward(cur, old, new):
    """The child tables (telemetry, position, user_satellite, …) foreign-key
    satellite(norad), so the parent PK cannot simply change while children
    point at it. Instead: the row is copied under the new norad first (all
    columns, discovered live, so a future column rides along), the children
    move, and _then_ the old parent row goes. If the new norad is already
    tracked, that row wins and the copy is a no-op."""
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'satellite'
                   ORDER BY ordinal_position""")
    cols = [r[0] for r in cur.fetchall()]
    src = ", ".join("%s" if c == "norad" else f'"{c}"' for c in cols)
    dst = ", ".join(f'"{c}"' for c in cols)
    cur.execute(f'INSERT INTO satellite ({dst}) '
                f'SELECT {src} FROM satellite WHERE norad = %s '
                f'ON CONFLICT (norad) DO NOTHING', (new, old))
    return cur.rowcount


def _move_norad(cur, table, old, new, log):
    """Re-key one table. If both sides already have rows that collide on a
    primary key (a user tracked BOTH numbers before the merge), the new
    norad's rows win and the old ones go — same outcome the SatNOGS merge
    itself produces upstream."""
    cur.execute("SAVEPOINT move_norad")
    try:
        cur.execute(f'UPDATE "{table}" SET norad = %s WHERE norad = %s',
                    (new, old))
        moved = cur.rowcount
        cur.execute("RELEASE SAVEPOINT move_norad")
        return moved
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT move_norad")
        cur.execute(f'DELETE FROM "{table}" WHERE norad = %s', (old,))
        dropped = cur.rowcount
        log.warning("catalog merge %s->%s: %s rows collided in %s, "
                    "old rows dropped (new norad's rows win)",
                    old, new, dropped, table)
        return 0


def reconcile_catalog(cur, seen, log):
    """Apply the DB's manual merges and removals to our mirror.

    `seen` is the full set of norads from one COMPLETE bulk pass — the caller
    must not invoke this after a partial one (a failed page would read as a
    mass extinction and prune half the catalogue).
    """
    seen = list(seen)
    # A merge is a sat_id that survived the pass under a different number:
    # the old norad vanished from the bulk list while its sat_id reappeared
    # (the new row was upserted page-by-page before we got here).
    cur.execute("""SELECT old.norad, new.norad, old.sat_id
                   FROM catalog old JOIN catalog new ON new.sat_id = old.sat_id
                    AND new.norad <> old.norad
                   WHERE NOT (old.norad = ANY(%s)) AND new.norad = ANY(%s)
                     AND old.sat_id IS NOT NULL""", (seen, seen))
    merges = cur.fetchall()
    tables = [t for t in norad_keyed_tables(cur) if t != "satellite"]
    for old, new, sat_id in merges:
        _copy_satellite_forward(cur, old, new)
        counts = {t: n for t in tables
                  if (n := _move_norad(cur, t, old, new, log))}
        cur.execute("DELETE FROM satellite WHERE norad = %s", (old,))
        cur.execute("DELETE FROM catalog WHERE norad = %s", (old,))
        # Loud on purpose: a merge rewires user-visible identity.
        log.warning("catalog merge applied: %s -> %s (sat_id %s), rows moved %s",
                    old, new, sat_id, counts or "none")
    # The ordinary vanish (decayed, withdrawn, merged-with-us-untracked):
    # prune only what nothing references — a tracked satellite that fell out
    # of the bulk list keeps its row, and we say so instead of guessing.
    cur.execute("""DELETE FROM catalog
                   WHERE NOT (norad = ANY(%s))
                     AND norad NOT IN (SELECT norad FROM satellite)""", (seen,))
    pruned = cur.rowcount
    cur.execute("SELECT norad, name FROM catalog WHERE NOT (norad = ANY(%s))",
                (seen,))
    for norad, name in cur.fetchall():
        log.warning("catalog: tracked %s (%s) no longer in the SatNOGS bulk "
                    "list — kept, needs a human decision", norad, name)
    return {"merged": len(merges), "pruned": pruned}
