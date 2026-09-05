# SatNOGS egress gateway

The single door to `db.satnogs.org`. Every Overwatch process that reads SatNOGS
points at this service instead of the provider directly, so the per-user rate
limit is enforced in one place no caller can bypass.

SatNOGS throttles **per user** (by token), not per process — `get_telemetry_user
= 6/min` in satnogs-db's settings. With more than one caller sharing one token
(the always-on ingest, plus operational batch tooling), their footprints add up,
and any new call path can silently reintroduce the overspend. The gateway is the
one counter that sees the whole footprint.

## What it does

- **One global gate.** A minimum gap (`SATNOGS_MIN_GAP`, default 11s ≈ 6/min with
  margin) between real upstream requests, shared across all callers.
- **Cache** per endpoint: telemetry 30m, TLE 6h, catalogue 24h. Re-runs and
  backfills hit the cache, not SatNOGS.
- **Honours refusal.** Respects `Retry-After`, persists a cooldown to disk (so a
  restart cannot resume hammering), backs off on timeouts too.
- **Injects the token** — callers never hold it.
- **Records every request** two ways: a per-request row in `upstream_request`
  (the ops Grafana dashboard, cache hits excluded so the chart is the true rate)
  AND OTel metrics through the collector (`ovw.satnogs.requests` counter with a
  `disposition` label HIT/MISS/COOL/ERR, + a duration histogram) → prometheus →
  Grafana OpsMetrics, the same pipeline the api uses.

## Using it

The SPOT proxies **any db.satnogs.org path** verbatim — the JSON API and the
plain pages alike — so a caller just sends the full path to the gateway host:

    SATNOGS_BASE=http://satnogs-gateway:8088/api   # the JSON API  (/api/telemetry/, …)
    SATNOGS_HOST=http://satnogs-gateway:8088        # the plain pages (/satellite/<norad>)

A GET to `…:8088/api/telemetry/?sat_id=…` reaches
`https://db.satnogs.org/api/telemetry/?sat_id=…`, and `…:8088/satellite/57175`
reaches `https://db.satnogs.org/satellite/57175` — both paced and cached. Only
`GET` is proxied — nothing writes upstream. `/healthz` returns `{"ok":true}`.

**All** SatNOGS access must go through the gateway, including one-off batch and
sweep tooling. In the deployment, non-gateway containers have `db.satnogs.org`
blackholed, so a script that forgets fails fast rather than overspending the
shared budget.

## Not for selfhost

A single-tenant selfhost install talks to SatNOGS directly from its own paced
ingest (no batch tooling, one caller) and does not run this service. The gateway
exists for the multi-caller cloud.

## Config

| env | default | meaning |
|-----|---------|---------|
| `SATNOGS_UPSTREAM` | `https://db.satnogs.org/api` | real provider base |
| `SATNOGS_TOKEN` | — | injected as `Authorization: Token …` |
| `SATNOGS_MIN_GAP` | `11` | seconds between real upstream requests |
| `GATEWAY_PORT` | `8088` | listen port |
| `DB_DSN` | — | where `upstream_request` rows are written |
| `TTL_TELEMETRY` / `TTL_TLE` / `TTL_SATELLITES` | `1800` / `21600` / `86400` | cache windows (s) |
