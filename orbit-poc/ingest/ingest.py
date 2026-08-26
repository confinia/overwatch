"""
Ingest / cache service -- the heart of the POC's architecture.

RESPONSIBILITY: it is the ONLY component allowed to talk to upstream APIs.
It fetches on sane schedules, caches locally, and propagates positions in-process.
MapLibre and Grafana never touch CelesTrak or SatNOGS directly.

Why this matters (learned from the research, not invented):
  * CelesTrak firewalls IPs pulling >100 MB/day and asks you to download data
    once per update, not per view. Elements update a few times daily.
  * SatNOGS telemetry updates only when a volunteer ground station hears a pass,
    so polling it fast is pointless and rude.

Cadences (env-overridable):
  ELEMENTS_INTERVAL  = 6h   (orbital elements)
  POSITION_INTERVAL  = 15s  (local SGP4 propagation -> position table)
  TELEMETRY_INTERVAL = 30m  (decoded frames)

Graceful degradation: no SatNOGS token => telemetry step is skipped with a
clear log line; the map + orbit half runs fully without any account.
"""

import os
import re
import time
import logging
import importlib
import threading
from datetime import datetime, timezone, timedelta

import requests
import psycopg2
from psycopg2.extras import execute_values
from sgp4.api import Satrec, jday
import numpy as np

from satellites import SHOWCASE
from calibration import calibrate, canonical_from, CANONICAL_SOURCES

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest")

DB_DSN            = os.environ["DB_DSN"]
SATNOGS_TOKEN     = os.environ.get("SATNOGS_TOKEN", "").strip()
CELESTRAK_BASE    = "https://celestrak.org/NORAD/elements/gp.php"
SATNOGS_BASE      = "https://db.satnogs.org/api"

ELEMENTS_INTERVAL  = int(os.environ.get("ELEMENTS_INTERVAL",  6 * 3600))
POSITION_INTERVAL  = int(os.environ.get("POSITION_INTERVAL",  15))
TELEMETRY_INTERVAL = int(os.environ.get("TELEMETRY_INTERVAL", 30 * 60))
CATALOG_INTERVAL   = int(os.environ.get("CATALOG_INTERVAL",   86400))
# Next-pass prediction (#217): heavy-ish forward scan, so run it a few times a
# day. Bounded to the most-active stations x the fleet, 7-day horizon at 60 s.
PASSES_INTERVAL     = int(os.environ.get("PASSES_INTERVAL",     6 * 3600))
PASSES_HORIZON_H    = int(os.environ.get("PASSES_HORIZON_H",    168))
PASSES_MIN_EL       = float(os.environ.get("PASSES_MIN_EL",     10.0))
PASSES_MAX_STATIONS = int(os.environ.get("PASSES_MAX_STATIONS", 20))

# Be a good citizen: identify ourselves.
from flatten import flatten_decoded

UA = {"User-Agent": "orbit-poc/0.1 (educational; contact: you@example.org)"}


def db():
    return psycopg2.connect(DB_DSN)


# --------------------------------------------------------------------------
# Startup: register showcase satellites, resolve SatNOGS sat_ids by norad id.
# --------------------------------------------------------------------------
def seed_satellites():
    with db() as conn, conn.cursor() as cur:
        for s in SHOWCASE:
            sat_id = None
            if s["telemetry"]:
                sat_id = resolve_sat_id(s["norad"])
                if sat_id is None:
                    log.warning("No SatNOGS sat_id for %s (%s); "
                                "keeping as position-only.", s["name"], s["norad"])
            cur.execute(
                """INSERT INTO satellite (norad, name, sat_id, has_telemetry, decoder, note)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (norad) DO UPDATE SET
                     name=EXCLUDED.name,
                     -- Not knowing a sat_id today is not evidence there is
                     -- none. This used to overwrite unconditionally, so a
                     -- deploy during a provider outage replaced every known
                     -- row with sat_id=NULL / has_telemetry=false — and since
                     -- nothing re-resolves outside startup, the fleet could
                     -- not recover. The 2026-08-20 block lasted hours; the
                     -- outage lasted four days, and this is why (#335).
                     sat_id=COALESCE(EXCLUDED.sat_id, satellite.sat_id),
                     has_telemetry=
                       COALESCE(EXCLUDED.sat_id, satellite.sat_id) IS NOT NULL
                       AND %s,
                     decoder=EXCLUDED.decoder, note=EXCLUDED.note""",
                (s["norad"], s["name"], sat_id,
                 bool(sat_id) and s["telemetry"], s.get("decoder"), s["note"],
                 bool(s["telemetry"])))
        conn.commit()
    log.info("Seeded %d showcase satellites.", len(SHOWCASE))


# SatNOGS throttles per user, not per satellite and not per endpoint:
# get_telemetry_user = 6/minute in satnogs-db's db/settings.py (and 1/day for
# satellites flagged is_frequency_violator). Six a minute is one every ten
# seconds, so the default leaves a second of margin.
#
# This has to be global rather than a sleep in the caller: the limit counts
# every request we make, so pagination inside one satellite and the move to
# the next satellite both have to pass through it. The previous sleep(5)
# between satellites, with three pages fetched back to back inside each, ran
# at roughly 26 requests a minute while its comment claimed it stayed "well
# under" the limit.
#
# EVERY SatNOGS call goes through here, not just telemetry (#315). Pacing one
# loop while three other call sites burst past it just moves the overspend:
# once #313 put an unreachable CelesTrak on cooldown, fill_missing_elements
# started sending one /tle/ request per satellite, 23 back to back, and spent
# the budget the telemetry loop was rationing. Holding our whole footprint to
# the strictest published scope is the only version of this that stays true
# when a new call site is added.
SATNOGS_MIN_GAP = float(os.environ.get("SATNOGS_MIN_GAP", 11))
_satnogs_gate = threading.Lock()
_satnogs_last = [0.0]


# SatNOGS allows one telemetry request A DAY for satellites it flags as
# violating frequency regulations (get_telemetry_violator = 1/day, and the
# spectrum policy states it as one request per day per satellite). Our normal
# cycle is 30 minutes, which for a flagged satellite would be 48 requests
# against a limit of one — 47 guaranteed refusals a day, each one evidence
# against us. The extra hour is margin: a cycle that drifts must not be able
# to land twice inside one 24h window.
VIOLATOR_GAP = os.environ.get("VIOLATOR_GAP", "25 hours")


def _pace_satnogs():
    """Block until issuing a SatNOGS request stays inside the published rate."""
    with _satnogs_gate:
        wait = SATNOGS_MIN_GAP - (time.time() - _satnogs_last[0])
        if wait > 0:
            time.sleep(wait)
        _satnogs_last[0] = time.time()


def resolve_sat_id(norad):
    """Find a SatNOGS sat_id for a norad id — from OUR OWN catalogue first.

    This used to be a per-object query to /api/satellites/?norad_cat_id=, run
    at every startup for every satellite still missing a sat_id. While the
    lookup fails (as it does when they are blocking us) the column stays null,
    so the next start asks again: a polling loop that grows as it fails. That
    is the same shape as the CelesTrak per-object loop that got this VM
    blocked (#304).

    The `catalog` table already holds sat_id for the whole network, filled by
    ONE paginated bulk pass per day. Read it from there; only fall back to a
    single live query when the catalogue has not been built yet, and back off
    like any other per-object lookup."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT sat_id FROM catalog WHERE norad = %s AND sat_id IS NOT NULL",
                    (norad,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("SELECT count(*) FROM catalog")
        catalogued = cur.fetchone()[0]
    # No per-object query, ever. main() builds the catalogue before seeding,
    # so an empty catalogue means SatNOGS was unreachable at startup — asking
    # again per satellite would be the pattern LSF blocked us for, and the
    # bulk list carries the same data anyway (their words). Callers preserve
    # whatever sat_id they already had, so an outage costs us nothing.
    if not catalogued:
        log.warning("Catalogue is empty — no sat_id for %s until the bulk "
                    "pass succeeds", norad)
    return None


# --------------------------------------------------------------------------
# Elements: bulk GROUP fetches from CelesTrak (one request per group per 6h
# cadence — the polite pattern; per-view fetching gets IPs firewalled).
# Groups seed the satellite table too: every member becomes a position-only
# entry unless the curated showcase already claims it (decoder, note...).
# Showcase norads not present in any group fall back to one per-CATNR fetch.
# --------------------------------------------------------------------------
CELESTRAK_GROUPS = [g.strip() for g in
                    os.environ.get("CELESTRAK_GROUPS", "amateur,stations").split(",")
                    if g.strip()]

# Groups fetched for the TLE CACHE only — their members are NOT seeded into
# `satellite` (that would add thousands of objects nobody asked for). This is
# how an arbitrary satellite added from the catalogue gets its elements
# without ever touching the per-object endpoint: one bulk file, cached,
# refreshed on the elements cycle. CelesTrak asks for exactly this, and an
# unbounded per-object loop is what got our address blocked.
CELESTRAK_LOOKUP_GROUPS = [g.strip() for g in
                           os.environ.get("CELESTRAK_LOOKUP_GROUPS", "active").split(",")
                           if g.strip()]
_bulk_tles = {"ts": 0.0, "by_norad": {}}

# How long to wait for a TCP connection to a provider. Downloads may be slow;
# a handshake with a reachable host is not. Keeping these separate is what
# stops an unreachable provider from being indistinguishable from a big file.
CONNECT_TIMEOUT = int(os.environ.get("CONNECT_TIMEOUT", 8))

# When CelesTrak or SatNOGS answers 403/429, stop asking entirely until this
# passes. Hammering something that just refused us is how a rate limit turns
# into a ban.
_cooldown_until = {"celestrak": 0.0, "satnogs": 0.0}


def _cooling(source):
    left = _cooldown_until[source] - time.time()
    if left > 0:
        log.debug("%s: cooling down for another %.0f min", source, left / 60)
        return True
    return False


def _cool(source, response=None, hours=6, reason="refused us"):
    wait = hours * 3600
    if response is not None:
        try:
            wait = max(wait, int(response.headers.get("Retry-After", 0)))
        except ValueError:
            pass
    _cooldown_until[source] = time.time() + wait
    log.warning("%s %s — not asking again for %.1f h", source, reason, wait / 3600)


def _refresh_bulk_tles():
    """One bulk file per lookup group, cached in memory for the elements cycle."""
    if time.time() - _bulk_tles["ts"] < ELEMENTS_INTERVAL and _bulk_tles["by_norad"]:
        return _bulk_tles["by_norad"]
    if _cooling("celestrak"):
        return _bulk_tles["by_norad"]
    found = {}
    for group in CELESTRAK_LOOKUP_GROUPS:
        try:
            # (connect, read): the `active` file is large and deserves a
            # generous read budget, but a connect to a host that is dropping
            # our packets must fail in seconds. A single 120s value here cost
            # 120s PER SATELLITE inside fetch_elements — see below.
            r = requests.get(CELESTRAK_BASE, params={"GROUP": group, "FORMAT": "TLE"},
                             headers=UA, timeout=(CONNECT_TIMEOUT, 120))
            if r.status_code in (403, 429):
                _cool("celestrak", r)
                break
            r.raise_for_status()
            for _name, tle1, tle2 in _parse_tle_file(r.text):
                found[int(tle1[2:7])] = (tle1, tle2)
        except requests.exceptions.RequestException as e:
            # Unreachable is not the same as refused, but it calls for the same
            # silence. Without this the cache is never stamped, so EVERY
            # caller retries: fetch_elements primes synchronously before the
            # position loop starts, so 23 satellites x 120s left the globe
            # frozen for ~50 minutes after each restart (#313), while knocking
            # ~700 times a day on a host that is firewalling us.
            log.warning("Bulk group '%s' fetch failed: %s", group, e)
            _cool("celestrak", hours=1, reason="is unreachable")
            break
        time.sleep(2)
    if found:
        _bulk_tles["by_norad"] = found
        _bulk_tles["ts"] = time.time()
        log.info("TLE cache: %d objects from %s", len(found),
                 ",".join(CELESTRAK_LOOKUP_GROUPS))
    return _bulk_tles["by_norad"]


def _due_for_lookup(norad):
    """Has this object served its backoff? Persisted, so a restart does not
    resume polling something we already gave up on."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT gave_up, next_attempt <= now() FROM element_fetch
                       WHERE norad = %s""", (norad,))
        row = cur.fetchone()
    if not row:
        return True
    gave_up, due = row
    return bool(due) and not gave_up


def _record_lookup(norad, ok, error="", permanent=False):
    """Exponential backoff, then a permanent give-up. CelesTrak simply does not
    carry some objects; asking forever is the abusive pattern.

    `permanent` skips the backoff entirely and gives up on the first attempt.
    It is for the case where a provider has answered that the object is not
    there — a verdict, unlike a timeout, that repetition cannot overturn.

    It is also the ONLY thing that sets gave_up. Counting attempts used to do
    it too, which meant an outage long enough to burn six of them was filed as
    "not carried by CelesTrak or SatNOGS" — permanently, with nothing to clear
    it. Production came out of the 2026-08-20 block with all 23 satellites in
    that state, the ISS among them, and could not recover on its own. Backoff
    already caps at 24h, so an object we merely cannot reach costs one request
    a day and heals by itself the moment the network does."""
    with db() as conn, conn.cursor() as cur:
        if ok:
            cur.execute("DELETE FROM element_fetch WHERE norad = %s", (norad,))
        else:
            cur.execute("""INSERT INTO element_fetch (norad, attempts, last_attempt,
                                                      next_attempt, gave_up, last_error)
                           VALUES (%s, 1, now(), now() + interval '1 hour', %s, %s)
                           ON CONFLICT (norad) DO UPDATE SET
                             attempts = element_fetch.attempts + 1,
                             last_attempt = now(),
                             next_attempt = now() +
                               (least(power(2, element_fetch.attempts + 1), 24)
                                * interval '1 hour'),
                             gave_up = %s,
                             last_error = EXCLUDED.last_error""",
                        (norad, permanent, error[:200], permanent))
        conn.commit()


# How far back to re-aggregate on each pass. Frames arrive late — a station
# uploads a pass hours after it happened — so yesterday's total is not final
# when yesterday ends. Recomputing a trailing window is cheaper than being
# wrong, and the upsert makes it idempotent.
STATION_ROLLUP_DAYS = int(os.environ.get("STATION_ROLLUP_DAYS", 3))
_rollup_backfilled = [False]


def rollup_tick():
    """One full pass over all history, then trailing windows.

    The full pass used to run from main(), where it lost a race: station_daily
    is created by the API's startup DDL, and the ingest often starts first, so
    it raised "relation does not exist", was caught so it could not block
    startup, and was never retried. Every month of history we already held
    stayed out of the baseline (#339).

    Doing it on the first SUCCESSFUL tick removes the ordering dependency
    between the two services: if the table is not there yet the tick fails, the
    flag stays unset, and the next hour tries again.
    """
    if not _rollup_backfilled[0]:
        n = roll_up_stations()              # everything we hold
        _rollup_backfilled[0] = True        # only on success — an exception
        return n                            # propagates and we retry next tick
    return roll_up_stations(STATION_ROLLUP_DAYS)


def roll_up_stations(days=None):
    """Per-station daily activity, derived from `reception` (#337).

    The baseline a degradation detector needs: to say a station has gone quiet
    you must know what it was like when it was working. Purely a local
    aggregation — it issues NO provider request, which is what makes it safe
    to run often and safe to backfill in full.

    `days=None` rebuilds everything, for the one-time backfill; otherwise it
    recomputes a trailing window.
    """
    where = ""
    params = ()
    if days is not None:
        where = "WHERE ts >= current_date - %s::integer"
        params = (days,)
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO station_daily
                   (observer, day, frames, satellites_heard, first_ts, last_ts)
            SELECT observer, ts::date, count(*), count(DISTINCT norad),
                   min(ts), max(ts)
            FROM reception
            {where}
            GROUP BY observer, ts::date
            ON CONFLICT (observer, day) DO UPDATE SET
              frames = EXCLUDED.frames,
              satellites_heard = EXCLUDED.satellites_heard,
              first_ts = EXCLUDED.first_ts,
              last_ts = EXCLUDED.last_ts
        """, params)
        n = cur.rowcount
        conn.commit()
    log.info("Station rollup: %d station-days%s", n,
             "" if days is None else f" (last {days}d)")
    return n


# Opportunity scan (#346). 60s catches every LEO pass above 10 deg — they
# last minutes — while keeping the grid cheap. OPPORTUNITY_DAYS bounds how far
# back we will reconstruct: SGP4 degrades away from a TLE's epoch, so there is
# no honest answer beyond the element history we actually hold.
OPPORTUNITY_STEP_S = int(os.environ.get("OPPORTUNITY_STEP_S", 60))
OPPORTUNITY_MIN_EL = float(os.environ.get("OPPORTUNITY_MIN_EL", 10.0))
OPPORTUNITY_DAYS = int(os.environ.get("OPPORTUNITY_DAYS", 40))


def _tle_nearest(cur, norad, when):
    """The element set closest in epoch to `when` — propagating a TLE far from
    its epoch is how you get confident nonsense."""
    cur.execute("""SELECT tle1, tle2 FROM elements WHERE norad = %s
                   ORDER BY abs(extract(epoch FROM epoch - %s)) LIMIT 1""",
                (norad, when))
    return cur.fetchone()


def compute_opportunities(day):
    """How many passes each station COULD have heard on `day`.

    The loops are inverted deliberately. The obvious shape — for each station,
    for each satellite, scan the day — costs 360 x 23 x 1440 SGP4 evaluations,
    which is hours of Python. Propagating each satellite ONCE per timestep and
    then testing every station against that single position costs 23 SGP4 calls
    per step instead of 8280, leaving only arithmetic in the inner loop. Same
    answer, ~3 orders of magnitude less work.

    Local geometry only: coordinates from `reception`, elements from
    `elements`. No provider request, which is what makes backfilling safe.
    """
    import passes as _p
    from sgp4.api import Satrec, jday

    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    mid = start + timedelta(hours=12)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT observer, max(lat), max(lon) FROM reception
                       WHERE lat IS NOT NULL GROUP BY observer""")
        stations = [(o, _p.station_ecef(la, lo)) for o, la, lo in cur.fetchall()]
        cur.execute("SELECT DISTINCT norad FROM satellite")
        sats = []
        for (norad,) in cur.fetchall():
            row = _tle_nearest(cur, norad, mid)
            if row:
                sats.append((norad, Satrec.twoline2rv(row[0], row[1])))
    if not stations or not sats:
        log.info("Opportunities %s: no stations or no elements — skipped", day)
        return 0

    # (observer, norad) -> [passes, best_el]; peak carries the in-flight pass
    tally, peak = {}, {}
    steps = 86400 // OPPORTUNITY_STEP_S
    for i in range(steps + 1):
        t = start + timedelta(seconds=i * OPPORTUNITY_STEP_S)
        jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute,
                      t.second + t.microsecond * 1e-6)
        for norad, sat in sats:
            e, r, _v = sat.sgp4(jd, fr)
            if e != 0:
                continue
            ecef = _p.teme_to_ecef(r[0], r[1], r[2], jd, fr)
            for observer, secef in stations:
                el = _p.elevation_deg(ecef, secef)
                key = (observer, norad)
                if el >= OPPORTUNITY_MIN_EL:
                    peak[key] = max(peak.get(key, -90.0), el)
                elif key in peak:                       # pass just ended
                    slot = tally.setdefault(key, [0, -90.0])
                    slot[0] += 1
                    slot[1] = max(slot[1], peak.pop(key))
    for key, pk in peak.items():                        # still up at midnight
        slot = tally.setdefault(key, [0, -90.0])
        slot[0] += 1
        slot[1] = max(slot[1], pk)

    rows = [(o, day, n, v[0], round(v[1], 1)) for (o, n), v in tally.items()]
    if rows:
        with db() as conn, conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO station_opportunity
                       (observer, day, norad, passes, best_max_el)
                VALUES %s
                ON CONFLICT (observer, day, norad) DO UPDATE SET
                  passes = EXCLUDED.passes,
                  best_max_el = EXCLUDED.best_max_el""", rows)
            conn.commit()
    log.info("Opportunities %s: %d station-satellite rows over %d stations",
             day, len(rows), len(stations))
    return len(rows)


def opportunities_tick():
    """Compute ONE missing day per tick — never the whole backlog at once.

    Measured at ~15s for a day across 360 stations x 25 satellites, so a tick
    is short and a 40-day reconstruction is ~10 minutes of compute spread over
    a few hours. Doing it in one call would stall the loop it runs in and, on
    a restart, begin again from nothing; one day per tick makes progress
    durable and every tick bounded.
    """
    today = datetime.now(timezone.utc).date()
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT min(epoch)::date FROM elements")
        row = cur.fetchone()
        oldest_tle = row[0] if row and row[0] else today
        floor = max(oldest_tle, today - timedelta(days=OPPORTUNITY_DAYS))
        cur.execute("""SELECT d::date FROM generate_series(%s, %s, '1 day') d
                       WHERE NOT EXISTS (SELECT 1 FROM station_opportunity o
                                         WHERE o.day = d::date)
                       ORDER BY d DESC LIMIT 1""",
                    (floor, today - timedelta(days=1)))
        due = cur.fetchone()
    if not due:
        return 0
    return compute_opportunities(due[0])


def _tle_for(norad):
    """Bulk cache first, per-object only as a rare, backed-off fallback."""
    bulk = _refresh_bulk_tles()
    if norad in bulk:
        return bulk[norad]
    if not _due_for_lookup(norad):
        return None
    tle = None
    ct_absent = sn_absent = False
    if not _cooling("celestrak"):
        tle, ct_absent = _tle_from_celestrak(norad)
    if not tle and not _cooling("satnogs"):
        tle, sn_absent = _tle_from_satnogs(norad)
    if tle:
        _record_lookup(norad, True)
        return tle
    # Both providers ANSWERED that they do not carry it. Five more requests
    # over the next several days cannot learn anything the first one did not
    # already tell us, and that is the pattern that got this address
    # firewalled. Absence is only absence when someone said so: a cooldown, a
    # timeout or a missing SatNOGS token leaves the ordinary backoff in charge,
    # because SatNOGS does carry objects CelesTrak has dropped.
    _record_lookup(norad, False, "not carried by CelesTrak or SatNOGS",
                   permanent=ct_absent and sn_absent)
    return None


def fetch_elements():
    seen = set()
    for group in CELESTRAK_GROUPS:
        try:
            r = requests.get(CELESTRAK_BASE,
                             params={"GROUP": group, "FORMAT": "TLE"},
                             headers=UA, timeout=60)
            r.raise_for_status()
            triples = _parse_tle_file(r.text)
            with db() as conn, conn.cursor() as cur:
                for name, tle1, tle2 in triples:
                    norad = int(tle1[2:7])
                    seen.add(norad)
                    cur.execute(
                        """INSERT INTO satellite (norad, name, has_telemetry, note)
                           VALUES (%s,%s,false,%s)
                           ON CONFLICT (norad) DO NOTHING""",
                        (norad, name, f"CelesTrak group '{group}'"))
                    cur.execute(
                        """INSERT INTO elements (norad, epoch, tle1, tle2)
                           VALUES (%s,%s,%s,%s)
                           ON CONFLICT (norad, epoch) DO NOTHING""",
                        (norad, _epoch_from_tle(tle1), tle1, tle2))
                conn.commit()
            log.info("Elements: group '%s' -> %d satellites", group, len(triples))
        except Exception as e:
            log.warning("Element group fetch failed for '%s': %s", group, e)
        time.sleep(2)

    # showcase satellites not covered by any group (e.g. EO / GNSS anchors)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT norad FROM satellite WHERE note NOT LIKE 'CelesTrak group%%'")
        rest = [r[0] for r in cur.fetchall() if r[0] not in seen]
    for norad in rest:
        try:
            tle = _tle_for(norad)
            if not tle:
                log.warning("No elements found for %s (CelesTrak + SatNOGS)", norad)
                continue
            tle1, tle2 = tle
            with db() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO elements (norad, epoch, tle1, tle2)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (norad, epoch) DO NOTHING""",
                    (norad, _epoch_from_tle(tle1), tle1, tle2))
                conn.commit()
            log.info("Elements updated: %s", norad)
        except Exception as e:
            log.warning("Element fetch failed for %s: %s", norad, e)
        time.sleep(1)


def fill_missing_elements(limit=3):
    """Give freshly tracked satellites their TLE quickly (#230).

    The six-hourly sweep would eventually cover them, but a satellite someone
    just added should appear on the globe in a couple of minutes, not hours.
    Runs here rather than in the API because fetching belongs to the ingest —
    it has the token and the CelesTrak -> SatNOGS fallback, and a request
    handler must never block on a third party."""
    with db() as conn, conn.cursor() as cur:
        # Only objects whose backoff has expired and that we have not given
        # up on — otherwise this loop polls the same unfindable satellites
        # forever, which is precisely what got our address blocked.
        cur.execute("""SELECT s.norad FROM satellite s
                       LEFT JOIN elements e ON e.norad = s.norad
                       LEFT JOIN element_fetch f ON f.norad = s.norad
                       WHERE e.norad IS NULL
                         AND COALESCE(f.gave_up, false) = false
                         AND COALESCE(f.next_attempt, now()) <= now()
                       LIMIT %s""", (limit,))
        pending = [r[0] for r in cur.fetchall()]
    for norad in pending:
        tle = _tle_for(norad)
        if not tle:
            log.warning("No elements for %s yet — backing off (persisted)", norad)
            continue
        tle1, tle2 = tle
        with db() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO elements (norad, epoch, tle1, tle2)
                           VALUES (%s,%s,%s,%s)
                           ON CONFLICT (norad, epoch) DO NOTHING""",
                        (norad, _epoch_from_tle(tle1), tle1, tle2))
            conn.commit()
        log.info("Elements filled for newly tracked %s", norad)
        time.sleep(2)


def refresh_catalog():
    """Mirror the SatNOGS satellite DB into `catalog` — the list users pick
    from (#230). A lookup table only: choosing an entry copies it into
    `satellite`, which is what actually gets tracked. Paginated, ~1700 rows,
    refreshed daily; SatNOGS needs no key for this endpoint."""
    # We deploy several times a day and this runs at every startup, so without
    # a freshness guard "one bulk pass per day" — what we told LSF — would be
    # one per deploy.
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(updated_at) > now() - %s::interval FROM catalog",
                    (f"{CATALOG_INTERVAL} seconds",))
        row = cur.fetchone()
    if row and row[0]:
        log.info("Catalog: refreshed within the last %.1fh, skipping",
                 CATALOG_INTERVAL / 3600)
        return
    url, page, rows = f"{SATNOGS_BASE}/satellites/", 0, 0
    headers = dict(UA)
    if SATNOGS_TOKEN:
        headers["Authorization"] = f"Token {SATNOGS_TOKEN}"
    while url and page < 40:
        try:
            _pace_satnogs()
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 30)))
                continue
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("Catalog refresh failed at page %d: %s", page, e)
            return
        items = data.get("results", data) if isinstance(data, dict) else data
        batch = []
        for sat in items:
            norad = sat.get("norad_cat_id")
            name = (sat.get("name") or "").strip()
            if not norad or not name:
                continue            # objects without a catalogue number cannot be propagated
            batch.append((norad, name, sat.get("sat_id"), sat.get("status"),
                          bool(sat.get("is_frequency_violator"))))
        if batch:
            with db() as conn, conn.cursor() as cur:
                execute_values(cur,
                    """INSERT INTO catalog (norad, name, sat_id, status, is_violator)
                       VALUES %s
                       ON CONFLICT (norad) DO UPDATE SET
                         name = EXCLUDED.name, sat_id = EXCLUDED.sat_id,
                         status = EXCLUDED.status,
                         is_violator = EXCLUDED.is_violator,
                         updated_at = now()""", batch)
                conn.commit()
            rows += len(batch)
        url = data.get("next") if isinstance(data, dict) else None
        page += 1
        time.sleep(1)
    # decoders are what turn a tracked satellite into a telemetry one
    with db() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE catalog c SET decoder = s.decoder
                       FROM satellite s
                       WHERE s.norad = c.norad AND s.decoder IS NOT NULL
                         AND c.decoder IS DISTINCT FROM s.decoder""")
        conn.commit()
    log.info("Catalog: %d satellites available to pick from", rows)


# A per-satellite CelesTrak lookup is a fallback path, so it must fail FAST.
# At 30s it does not: CelesTrak's per-object endpoint has been unreachable
# from this VM while the bulk group endpoint kept working, and priming
# elements at startup then costs ~30s PER satellite before positions can
# start — twelve minutes of dark globe after every deploy. SatNOGS answers
# right after, so a short timeout loses nothing.
CELESTRAK_ONE_TIMEOUT = int(os.environ.get("CELESTRAK_ONE_TIMEOUT", 8))


def _tle_from_celestrak(norad):
    """Returns (tle, absent).

    `absent` means CelesTrak ANSWERED that it does not carry this object.
    Its usage policy is explicit that a 403 or 404 will not change on retry,
    so the caller must stop asking rather than back off. A timeout or a 5xx
    is ignorance, not absence, and leaves `absent` false."""
    try:
        r = requests.get(CELESTRAK_BASE,
                         params={"CATNR": norad, "FORMAT": "TLE"},
                         headers=UA, timeout=CELESTRAK_ONE_TIMEOUT)
        if r.status_code in (403, 429):
            _cool("celestrak", r)
            return None, False
        if r.status_code == 404:
            return None, True
        r.raise_for_status()
        # An unknown CATNR answers 200 with this body, not a 404 — so status
        # alone would send us back for six more tries.
        if "No GP data found" in r.text:
            return None, True
        lines = [ln for ln in r.text.strip().splitlines() if ln.strip()]
        if len(lines) >= 2 and lines[-2].startswith("1 "):
            return (lines[-2], lines[-1]), False
    except Exception as e:
        log.debug("CelesTrak per-object lookup failed for %s: %s", norad, e)
    return None, False


def _tle_from_satnogs(norad):
    """Fallback: SatNOGS keeps TLEs for satellites CelesTrak drops from GP
    (e.g. LAPAN-A2). Needs the same free token as telemetry.

    Returns (tle, absent) like its CelesTrak counterpart. Only an answered,
    empty result counts as absent — no token, a cooldown or a failed request
    all mean we do not know, which must never be read as "does not exist"."""
    if not SATNOGS_TOKEN:
        return None, False
    try:
        headers = dict(UA); headers["Authorization"] = f"Token {SATNOGS_TOKEN}"
        _pace_satnogs()
        r = requests.get(f"{SATNOGS_BASE}/tle/",
                         params={"norad_cat_id": norad},
                         headers=headers, timeout=30)
        if r.status_code in (403, 429):
            _cool("satnogs", r)
            return None, False
        r.raise_for_status()
        data = r.json()
        if data:
            return (data[0]["tle1"], data[0]["tle2"]), False
        return None, True
    except Exception as e:
        log.debug("SatNOGS TLE fallback failed for %s: %s", norad, e)
    return None, False


def _parse_tle_file(text):
    """Parse a 3-line-element file into (name, tle1, tle2) triples."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out, i = [], 0
    while i + 2 < len(lines) + 1:
        if lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            out.append((f"NORAD {lines[i][2:7].strip()}", lines[i], lines[i + 1]))
            i += 2
        elif i + 2 < len(lines) and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            out.append((lines[i].strip(), lines[i + 1], lines[i + 2]))
            i += 3
        else:
            i += 1
    return out


def _epoch_from_tle(tle1):
    """Parse epoch (YYDDD.frac) from TLE line 1 into a UTC timestamp."""
    yy = int(tle1[18:20]); day = float(tle1[20:32])
    year = 2000 + yy if yy < 57 else 1900 + yy
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1)


# --------------------------------------------------------------------------
# Positions: propagate latest elements with SGP4, in-process, every 15s.
# This is the ONLY high-frequency loop and it touches NO external service.
# --------------------------------------------------------------------------
def propagate_positions():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (norad) norad, tle1, tle2
            FROM elements ORDER BY norad, epoch DESC""")
        rows = cur.fetchall()

    now = datetime.now(timezone.utc)
    jd, fr = jday(now.year, now.month, now.day,
                  now.hour, now.minute, now.second + now.microsecond * 1e-6)
    sun = _sun_unit_vector(jd + fr)
    out = []
    for norad, tle1, tle2 in rows:
        try:
            sat = Satrec.twoline2rv(tle1, tle2)
            e, r_teme, _ = sat.sgp4(jd, fr)
            if e != 0:
                continue
            lat, lon, alt = _teme_to_geodetic(r_teme, jd, fr)
            out.append((norad, now, lat, lon, alt, _is_sunlit(r_teme, sun)))
        except Exception as ex:
            log.debug("propagation failed %s: %s", norad, ex)

    if out:
        with db() as conn, conn.cursor() as cur:
            execute_values(cur,
                """INSERT INTO position (norad, ts, lat, lon, alt_km, sunlit)
                   VALUES %s ON CONFLICT DO NOTHING""", out)
            # prune: keep a week so position joins telemetry history
            cur.execute("DELETE FROM position WHERE ts < now() - interval '7 days'")
            conn.commit()


def compute_passes():
    """Recompute upcoming passes (#217): for the most-active ground stations x
    the fleet, forward-propagate and record each rise->set window into `pass`.
    Bounded and idempotent; Grafana reads the table directly."""
    import passes as _passes
    with db() as conn, conn.cursor() as cur:
        # created in init.sql on a fresh DB; ensure it on existing ones too.
        cur.execute("""CREATE TABLE IF NOT EXISTS pass (
            observer TEXT NOT NULL, norad INTEGER REFERENCES satellite(norad),
            aos TIMESTAMPTZ NOT NULL, los TIMESTAMPTZ NOT NULL,
            max_el_deg DOUBLE PRECISION, PRIMARY KEY (observer, norad, aos))""")
        cur.execute("CREATE INDEX IF NOT EXISTS pass_aos_idx ON pass (aos)")
        conn.commit()
        cur.execute("""SELECT observer, max(lat), max(lon) FROM reception
            WHERE lat IS NOT NULL AND ts > now() - interval '7 days'
            GROUP BY observer ORDER BY count(*) DESC LIMIT %s""",
            (PASSES_MAX_STATIONS,))
        stations = cur.fetchall()
        cur.execute("""SELECT DISTINCT ON (norad) norad, tle1, tle2
            FROM elements ORDER BY norad, epoch DESC""")
        sats = cur.fetchall()
    if not stations or not sats:
        return
    start = datetime.now(timezone.utc)
    rows = []
    for observer, lat, lon in stations:
        for norad, tle1, tle2 in sats:
            try:
                for aos, los, mx in _passes.find_passes(
                        tle1, tle2, float(lat), float(lon), start,
                        hours=PASSES_HORIZON_H, min_el=PASSES_MIN_EL):
                    rows.append((observer, norad, aos, los, mx))
            except Exception as ex:
                log.debug("passes failed %s/%s: %s", observer, norad, ex)
    with db() as conn, conn.cursor() as cur:
        # Idempotent persist (#232): a plain upsert on the exact AOS duplicated
        # every pass on each run, because the interpolated rise time drifts by
        # a fraction of a second between grids. store_passes takes a clean slate.
        _passes.store_passes(cur, rows)
        conn.commit()
    log.info("passes: %d upcoming across %d stations x %d sats",
             len(rows), len(stations), len(sats))


def _sun_unit_vector(jd):
    """Low-precision solar position (ECI, equinox of date ~ TEME): standard
    almanac formulas, plenty accurate for an eclipse flag."""
    n = jd - 2451545.0
    L = np.radians((280.460 + 0.9856474 * n) % 360.0)   # mean longitude
    g = np.radians((357.528 + 0.9856003 * n) % 360.0)   # mean anomaly
    lam = L + np.radians(1.915) * np.sin(g) + np.radians(0.020) * np.sin(2 * g)
    eps = np.radians(23.439 - 0.0000004 * n)             # obliquity
    return np.array([np.cos(lam), np.cos(eps) * np.sin(lam),
                     np.sin(eps) * np.sin(lam)])


def _is_sunlit(r_teme, sun_hat):
    """Cylindrical Earth-shadow model: in shadow iff on the night side AND
    within one Earth radius of the anti-sun axis."""
    r = np.array(r_teme)
    s = float(np.dot(r, sun_hat))
    if s >= 0:
        return True
    perp = float(np.sqrt(max(np.dot(r, r) - s * s, 0.0)))
    return perp > 6371.0


def _teme_to_geodetic(r_teme, jd, fr):
    """
    Convert TEME position (km) to lat/lon/alt.
    Simplified: rotate TEME->ECEF by GMST, then geodetic on a sphere-ish Earth.
    Good enough for a map POC; swap in astropy for precision later.
    """
    x, y, z = r_teme
    # Greenwich Mean Sidereal Time (radians), low-precision formula.
    t = (jd + fr - 2451545.0) / 36525.0
    gmst = (280.46061837 + 360.98564736629 * (jd + fr - 2451545.0)
            + 0.000387933 * t * t) % 360.0
    g = np.radians(gmst)
    xe =  np.cos(g) * x + np.sin(g) * y
    ye = -np.sin(g) * x + np.cos(g) * y
    ze = z
    lon = np.degrees(np.arctan2(ye, xe))
    hyp = np.sqrt(xe * xe + ye * ye)
    lat = np.degrees(np.arctan2(ze, hyp))
    R_EARTH = 6371.0
    alt = np.sqrt(xe*xe + ye*ye + ze*ze) - R_EARTH
    # normalise lon to [-180,180]
    lon = ((lon + 180) % 360) - 180
    # plain floats: psycopg2 cannot adapt numpy scalars (np.float64 renders
    # as "np.float64(...)" in SQL -> InvalidSchemaName "np")
    return float(lat), float(lon), float(alt)


# --------------------------------------------------------------------------
# Telemetry: decoded frames from SatNOGS (needs API token). Skipped cleanly
# if no token is provided -> the position-only demo still runs.
# --------------------------------------------------------------------------
def fetch_telemetry():
    if not SATNOGS_TOKEN:
        log.info("No SATNOGS_TOKEN set -> skipping telemetry ingest "
                 "(map + orbits still work). Add a free token to light up "
                 "the health dashboards.")
        return

    with db() as conn, conn.cursor() as cur:
        # The flag is read from `catalog` rather than copied onto `satellite`,
        # so a satellite flagged upstream starts being honoured at the next
        # daily catalogue refresh without migrating anything of ours.
        cur.execute("""SELECT s.norad, s.sat_id, s.decoder
                       FROM satellite s
                       LEFT JOIN catalog c USING (norad)
                       WHERE s.has_telemetry AND s.sat_id IS NOT NULL
                         AND (NOT coalesce(c.is_violator, false)
                              OR s.last_telemetry_fetch IS NULL
                              OR s.last_telemetry_fetch < now() - %s::interval)
                    """, (VIOLATOR_GAP,))
        targets = cur.fetchall()
        cur.execute("""SELECT count(*) FROM satellite s
                       JOIN catalog c USING (norad)
                       WHERE s.has_telemetry AND s.sat_id IS NOT NULL
                         AND c.is_violator
                         AND s.last_telemetry_fetch >= now() - %s::interval
                    """, (VIOLATOR_GAP,))
        held = cur.fetchone()[0]
    if held:
        log.info("Telemetry: %d flagged satellite(s) held to one request a day",
                 held)

    for norad, sat_id, decoder in targets:
        try:
            with db() as conn, conn.cursor() as cur:
                cur.execute("SELECT max(ts) FROM telemetry WHERE norad=%s", (norad,))
                last_ts = cur.fetchone()[0]
            # First sight of a satellite: backfill ~a week of frames so the
            # 7-day dashboards are dense. Afterwards: fetch only what's new.
            frames = _get_frames(sat_id, pages=12 if last_ts is None else 3,
                                 until=last_ts)
            if frames is None:
                return  # token invalid — logged in _get_frames
            n = _store_frames(norad, frames, decoder)
            log.info("Telemetry: %d/%d frames decoded+stored for %s",
                     n, len(frames), norad)
        except Exception as e:
            log.warning("Telemetry fetch failed for %s: %s", norad, e)
        # Stamped on the ATTEMPT, not on success: a request that failed or was
        # refused still spent the satellite's daily allowance, and retrying it
        # sooner is exactly what the limit exists to prevent.
        with db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE satellite SET last_telemetry_fetch = now() "
                        "WHERE norad = %s", (norad,))
            conn.commit()
        # No sleep here: _pace_satnogs() already holds every request to the
        # published rate, and a second delay on top only slows the cycle.


def _get_frames(sat_id, pages=2, until=None):
    """Fetch recent frames. The endpoint is cursor-paginated since 2026
    ({next, previous, results}); older deployments returned a bare list.
    Honors 429 Retry-After — SatNOGS throttles aggressively. Stops paginating
    once frames get older than `until` (our newest stored frame)."""
    headers = dict(UA); headers["Authorization"] = f"Token {SATNOGS_TOKEN}"
    frames, url, params = [], f"{SATNOGS_BASE}/telemetry/", {"sat_id": sat_id}
    for _ in range(pages):
        for attempt in range(4):
            _pace_satnogs()
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 401:
                log.warning("SatNOGS 401 -> token invalid/expired; skipping telemetry.")
                return None
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 15)) + 1
                log.info("SatNOGS 429 — backing off %ss", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        data = r.json()
        page = data.get("results", []) if isinstance(data, dict) else data
        frames += page
        if until is not None and page:
            try:
                oldest = datetime.fromisoformat(
                    page[-1]["timestamp"].replace("Z", "+00:00"))
                if oldest <= until:
                    break  # reached already-stored history
            except (KeyError, ValueError):
                pass
        url, params = (data.get("next"), None) if isinstance(data, dict) else (None, None)
        if not url:
            break
    return frames


# Protocol/framing noise — true for every AX.25-based decoder. Filtering at
# ingest keeps the "Latest decoded fields" panel meaningful.
JUNK_FIELD_RE = re.compile(
    r"(ax25_header|ssid|hbit|_ctl$|_pid$|mask|_raw$|callsign|crc|_magic"
    r"|(message|msg|packet|frame)_type)", re.I)


def _canonical(field, v):
    """Derive normalized health fields (battery_v/battery_i/battery_pct) from
    decoder-specific names+units, so the dashboards work for every satellite
    without per-decoder panel queries. Heuristic mV/mA scaling on purpose."""
    f = field.lower()
    out = []
    if re.search(r"(vbat|v_bat|volt|bat[a-z0-9_]*_v$)", f):
        val = v / 1000.0 if 100 <= v <= 60000 else v
        if 0.5 <= val <= 60:
            out.append(("battery_v", val))
    elif re.search(r"(bat[a-z0-9_]*_i$|i_batt|charging_current|battery_current)", f):
        val = v / 1000.0 if 100 <= abs(v) <= 20000 else v
        if 0 < abs(val) <= 20:
            out.append(("battery_i", val))
    elif re.search(r"(state_of_charge|battery_percent|_soc$)", f):
        if 0 <= v <= 100:
            out.append(("battery_pct", float(v)))
    return out


def _decode_frame(decoder, frame_hex):
    """Decode a raw frame LOCALLY with satnogs-decoders (kaitai structs) and
    flatten numeric leaves. SatNOGS stopped inlining decoded values in the API
    (they live in their InfluxDB), so sovereign local decoding is the way."""
    mod = importlib.import_module(f"satnogsdecoders.decoder.{decoder}")
    cls = getattr(mod, decoder.capitalize())
    obj = cls.from_bytes(bytes.fromhex(frame_hex))
    return flatten_decoded(obj)


def _maidenhead(loc):
    """Maidenhead grid locator -> (lat, lon) at cell center, or None."""
    m = re.match(r"^([A-Ra-r]{2})(\d{2})([A-Xa-x]{2})?$", loc.strip())
    if not m:
        return None
    lon = (ord(m.group(1)[0].upper()) - 65) * 20 - 180 + int(m.group(2)[0]) * 2
    lat = (ord(m.group(1)[1].upper()) - 65) * 10 - 90 + int(m.group(2)[1])
    if m.group(3):
        lon += (ord(m.group(3)[0].lower()) - 97) * (2 / 24) + 1 / 24
        lat += (ord(m.group(3)[1].lower()) - 97) * (1 / 24) + 0.5 / 24
    else:
        lon += 1.0
        lat += 0.5
    return lat, lon


def _reception_row(norad, ts, f):
    """SatNOGS observer strings look like 'KM7DOS-CN87xi'."""
    obs = (f.get("observer") or "").strip()
    if not obs:
        return None
    lat = lon = None
    if "-" in obs:
        pos = _maidenhead(obs.rsplit("-", 1)[1])
        if pos:
            lat, lon = pos
    return (norad, ts, obs, lat, lon)


def _store_frames(norad, frames, decoder):
    """Turn frames into (field, value_num) rows. Preferred path: local kaitai
    decode of the raw hex. Fallback: inline decoded dicts (legacy API shape).
    Also records who-heard-whom reception rows (observer + grid locator)."""
    rows, decoded_n = [], 0
    receptions = []
    horizon = datetime.now(timezone.utc) + timedelta(hours=1)
    for f in frames[:200]:  # cap per cycle
        ts = f.get("timestamp") or f.get("time")
        if not ts:
            continue
        try:
            # guard: volunteer stations sometimes upload future-dated frames
            if datetime.fromisoformat(ts.replace("Z", "+00:00")) > horizon:
                continue
        except ValueError:
            continue
        rec = _reception_row(norad, ts, f)
        if rec:
            receptions.append(rec)
        fields = {}
        if decoder and f.get("frame"):
            try:
                fields = _decode_frame(decoder, f["frame"])
                calibrate(decoder, fields)  # raw register -> physical units
            except Exception:
                pass  # frame type not covered by the decoder — normal
        if not fields:
            legacy = f.get("decoded") or f.get("fields") or {}
            fields = dict(_flatten(legacy)) if isinstance(legacy, dict) else {}
        if not fields:
            continue
        decoded_n += 1
        # decoders with an explicit canonical map (calibration.py) skip the
        # generic heuristic, so a mislabelled rail can't become battery_v
        explicit = decoder in CANONICAL_SOURCES
        for k, v in fields.items():
            if JUNK_FIELD_RE.search(k):
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                rows.append((norad, ts, k, float(v), None))
                if not explicit:
                    for ck, cv in _canonical(k, v):
                        rows.append((norad, ts, ck, cv, None))
            elif isinstance(v, str):
                rows.append((norad, ts, k, None, v))
        if explicit:
            for ck, cv in canonical_from(decoder, fields):
                rows.append((norad, ts, ck, cv, None))
    if rows:
        # dedupe on the PK — same canonical field can derive from several
        # source fields in one frame, and ON CONFLICT rejects in-batch dups
        rows = list({(r[0], r[1], r[2]): r for r in rows}.values())
        with db() as conn, conn.cursor() as cur:
            execute_values(cur,
                """INSERT INTO telemetry (norad, ts, field, value_num, value_txt)
                   VALUES %s ON CONFLICT DO NOTHING""", rows)
            conn.commit()
    if receptions:
        receptions = list({(r[0], r[1], r[2]): r for r in receptions}.values())
        with db() as conn, conn.cursor() as cur:
            execute_values(cur,
                """INSERT INTO reception (norad, ts, observer, lat, lon)
                   VALUES %s ON CONFLICT DO NOTHING""", receptions)
            conn.commit()
    return decoded_n


def _flatten(d, prefix=""):
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from _flatten(v, prefix=key + "_")
        else:
            yield key, v


# --------------------------------------------------------------------------
# Scheduler: independent loops on their own cadences.
# --------------------------------------------------------------------------
def loop(fn, interval, name):
    while True:
        try:
            fn()
        except Exception as e:
            log.exception("%s loop error: %s", name, e)
        time.sleep(interval)


def main():
    _wait_for_db()
    # Before seeding, not after: seed_satellites resolves sat_ids from this
    # table, and an empty catalogue was the only remaining reason to query
    # SatNOGS per object (#335).
    refresh_catalog()
    seed_satellites()
    # Prime ONLY a genuinely empty database. The prime exists so positions have
    # elements to propagate, which is true on a fresh install and false on
    # every other one: the cache is already minutes-to-hours old and the
    # elements loop refreshes it seconds after the threads start. Priming
    # synchronously costs ~4.5 min — 8s of CelesTrak connect timeout plus 11s
    # per satellite through our own SatNOGS pacer — and NOTHING propagates
    # meanwhile. Measured 2026-08-26: started 10:47:41, first thread 10:52:04.
    # A dark globe after every deploy, and it tripped positions-stalled on
    # every environment (#352).
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM elements")
        have_elements = cur.fetchone()[0]
    if not have_elements:
        log.info("No cached elements — priming before positions start")
        fetch_elements()

    threading.Thread(target=loop, args=(fetch_elements, ELEMENTS_INTERVAL, "elements"),
                     daemon=True).start()
    threading.Thread(target=loop, args=(fetch_telemetry, TELEMETRY_INTERVAL, "telemetry"),
                     daemon=True).start()
    threading.Thread(target=loop, args=(compute_passes, PASSES_INTERVAL, "passes"),
                     daemon=True).start()
    threading.Thread(target=loop,
                     args=(fill_missing_elements,
                           int(os.environ.get("FILL_INTERVAL", 600)), "elements-fill"),
                     daemon=True).start()
    threading.Thread(target=loop,
                     args=(refresh_catalog, CATALOG_INTERVAL, "catalog"),
                     daemon=True).start()
    threading.Thread(
        target=loop,
        args=(opportunities_tick,
              int(os.environ.get("OPPORTUNITY_INTERVAL", 300)),
              "opportunities"),
        daemon=True).start()
    threading.Thread(
        target=loop,
        args=(rollup_tick,
              int(os.environ.get("STATION_ROLLUP_INTERVAL", 3600)),
              "station-rollup"),
        daemon=True).start()
    # positions LAST and in the main thread: loop() never returns, so anything
    # called after it is dead code. Adding a loop above this line instead of a
    # thread silently stops the globe (#230 did exactly that).
    loop(propagate_positions, POSITION_INTERVAL, "positions")


def _wait_for_db(retries=30):
    for i in range(retries):
        try:
            with db():
                return
        except Exception:
            log.info("Waiting for database... (%d)", i)
            time.sleep(2)
    raise SystemExit("Database never became available")


if __name__ == "__main__":
    main()
