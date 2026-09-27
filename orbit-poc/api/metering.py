"""Usage metering for private (Pro) tenants -> Polar (see POLAR.md).

Counts frames (private-telemetry ingest) and TM/TC requests per customer, mirrors
them into `org_usage` (our reconciliation source of truth), and emits usage
events to Polar. Sandbox-first and env-gated: with no Polar token it runs in
DRY-RUN — it logs the exact event it would send — so the whole metering loop
(CLEMSAT-1 -> private tenant -> meter) is provable without real credentials or
money. Only the private /v1/tenants/* paths meter; public open-data ingest never
does.
"""
import datetime
import json
import logging
import os
import time

log = logging.getLogger("metering")
# Ensure usage events are always visible in container logs (billing audit trail),
# independent of uvicorn's log config which otherwise swallows this logger's INFO.
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s metering: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

# Dry-run proves the loop works; it does not need to prove it once a second.
# A single 1 Hz tenant (the YAMCS demo) made this line 44% of all remaining
# Overwatch log volume once the health-check noise was gone (#511), on a host
# whose disks are the bottleneck (#492). The audit property is "you can see it
# working without credentials", which a first line plus a rolling count keeps;
# writing 86,400 identical lines a day does not add to it. REAL emissions and
# every failure are unaffected and still log individually.
DRY_RUN_LOG_EVERY_S = float(os.environ.get("METER_DRY_RUN_LOG_EVERY_S", "60"))
_dry = {}          # (customer, event) -> [reported_at, calls, quantity]


def _log_dry_run(event, now):
    """First one at once, then one summary per window per customer+event."""
    key = (event["external_customer_id"], event["name"])
    slot = _dry.get(key)
    if slot is None:
        _dry[key] = [now, 0, 0]
        log.info("METER dry-run %s", json.dumps(event))
        return
    slot[1] += 1
    slot[2] += event["metadata"].get("quantity", 0) or 0
    if now - slot[0] >= DRY_RUN_LOG_EVERY_S:
        log.info("METER dry-run x%d %s qty=%d over %.0fs for %s",
                 slot[1], event["name"], slot[2], now - slot[0],
                 event["external_customer_id"])
        _dry[key] = [now, 0, 0]


# off | sandbox | production. Default off => dry-run (no creds needed).
POLAR_ENV = os.environ.get("POLAR_ENV", "off").lower()
POLAR_API_BASE = os.environ.get("POLAR_API_BASE", "https://sandbox-api.polar.sh")
POLAR_ORG_TOKEN = os.environ.get("POLAR_ORG_TOKEN", "").strip()

# event name -> org_usage column it accumulates
DIM_COL = {
    "frame_ingested": "frames",
    "tm_request": "tm_count",
    "tc_request": "tc_count",
}


def _period(now=None):
    """Billing period key = current UTC month (matches a monthly Polar cycle)."""
    n = now or datetime.datetime.now(datetime.timezone.utc)
    return f"{n.year:04d}-{n.month:02d}"


def _emit(customer_id, event_name, quantity, metadata):
    """Send (or dry-run log) a Polar usage event. Never raises into the request."""
    event = {
        "name": event_name,
        "external_customer_id": str(customer_id),
        "metadata": {**(metadata or {}), "quantity": quantity},
    }
    if POLAR_ENV == "off" or not POLAR_ORG_TOKEN:
        _log_dry_run(event, time.monotonic())               # provable without creds
        return
    try:
        import requests
        requests.post(
            f"{POLAR_API_BASE}/v1/events/ingest",
            headers={"Authorization": f"Bearer {POLAR_ORG_TOKEN}"},
            json={"events": [event]}, timeout=5)
    except Exception as e:                                    # reconcile from org_usage
        log.warning("METER emit failed (%s): %s", event_name, e)


def record(cur, customer_id, event_name, quantity=1, metadata=None):
    """Accumulate usage for a private customer, in the caller's transaction.

    Increments the matching org_usage counter (so the caller's commit persists it
    atomically with the telemetry write) and emits the Polar event. `customer_id`
    is the tenant key / org id — the Polar external customer.
    """
    col = DIM_COL.get(event_name)
    if col is None:
        return
    cur.execute(
        f"INSERT INTO org_usage (customer, period, {col}) VALUES (%s, %s, %s) "
        f"ON CONFLICT (customer, period) DO UPDATE "
        f"SET {col} = org_usage.{col} + EXCLUDED.{col}, updated_at = now()",
        (str(customer_id), _period(), int(quantity)))
    _emit(customer_id, event_name, quantity, metadata)
