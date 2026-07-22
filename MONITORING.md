# Monitoring

What is observed in Overwatch, and how. The rule mirrors the product:
**operational and usage signals are monitored; private tenant telemetry
is the customers' data, never mixed into ops monitoring.**

## Pipeline

```
web / api  ──OTLP──►  otel-collector  ──►  Prometheus  ──►  Grafana
(traces + metrics)    (spanmetrics,        (also scrapes    (datasource
                       namespace ovw)        Grafana /metrics) promops)
caddy edge  ──►  JSON access log (persistent volume)
```

- **Traces → metrics**: FastAPI/Flask auto-instrumentation exports spans;
  the collector's spanmetrics connector derives per-route rate, latency
  and error metrics (namespace `ovw`).
- **Explicit counters**: the API emits `ovw_api_requests_total` with
  dimensions `route`, `status`, `country` (GeoIP, never the IP), `client`
  (direct/site/mirror), `keyed`.
- **Prometheus** scrapes the collector and Grafana's own `/metrics`.
- **Grafana** reads Prometheus via the `promops` datasource; ops
  dashboards live in an admin-only folder.

## What is monitored

### 1. Platform access (open service health)
Dashboard `platform-access` (ops folder).
- Requests/min per route, p95 latency, error rate.
- HTTP status mix; Grafana embed traffic.
- First-party UI beacon: page loads, satellite selections, searches
  (origin + satellite dims) — anonymous counters, no cookies/ids.
- Estimated concurrent viewers.

### 2. API access (product API)
Dashboard `api-access` (ops folder).
- `/v1` requests/min per route, p50/p95 latency, status mix.
- Keyed vs anonymous share; consumer kind.
- **Requests by country** (GeoIP) and **unique visitors by country**
  (salted daily hash in `visitor_daily` — never the IP).
- Top API keys with emails (the design-partner lead list).

### 3. Per-organization usage (tenant billing/quotas)
- Ingest volume per organization (points/day) vs quota — feeds quota
  enforcement (in the API) and, at monetization, the Polar customer meter
  (billing informs, the API enforces).
- Per-org request attribution: user-token calls by identity claims,
  machine calls by named service token.
- Target: an org-scoped Grafana view so a tenant sees its own usage
  (issue #13).

### 4. Edge / raw traffic
- Caddy JSON access log on a persistent volume: referrers, raw pageviews,
  real client IPs (rolls at 20 MiB, keeps 5). Complements the JS beacon,
  which undercounts (curl, no-JS, blocked scripts).
- Read: `podman exec orbit-poc_caddy_1 tail -f /data/logs/access.log`.

### 5. Ingest / data freshness
- Ingest logs: frames decoded/stored per satellite, SatNOGS backoff (429)
  behaviour, TLE fetch cadence.
- Freshness is visible in-product (fleet status chip: live<1h / nominal /
  quiet / silent) and via `/api/v1/satellites` `last_frame`.

### 6. Deploy & availability
- Blue/green deploys are probe-verified (zero-dropped-requests checks).
- `make status` reports live color, per-color health, versions.
- Health endpoints: `/healthz`, `/api/v1/healthz` (version + last
  position), Keycloak `/auth/realms/overwatch/...` discovery.

## Access & privacy

- Ops dashboards are **admin-only** (folder + dashboard ACLs stripped of
  Viewer); public dashboards expose only open-data views.
- Privacy by construction: IPs are reduced to country (GeoIP) or a salted
  daily hash; no raw IP is stored in metrics; the UI beacon carries no
  identifiers.
- Private tenant telemetry is **customer data**, surfaced only to that
  organization — it is never an input to platform ops monitoring.

## Gaps / next

- Alerting to a phone for the failure cases (edge down, ingest stalled,
  provisioning webhook failing) — the piece that makes "away for months"
  safe.
- Per-organization self-service usage dashboard (#13).
- Blackbox uptime probes on the public endpoints (platform layer).
