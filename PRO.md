# Overwatch PRO — what a subscription gives you

Overwatch is **open core**: the frontend and all open-data access are free
forever, and the whole stack is self-hostable (AGPL). **PRO is what you pay
for when it is *your own* data and *your* operation** — a private, isolated
tenant, managed and supported, on sovereign European infrastructure.

Honest status below: **✅ Available** vs **⏳ Roadmap**. We don't sell what
isn't built.

## The line: free vs PRO

| | Free | PRO (per organization) |
|---|---|---|
| Open-data globe, telemetry, receptions, API | ✅ always free | ✅ included |
| **Your private telemetry**, isolated | — | ✅ the paid unit |
| Managed hosting, SLA, support | — | ✅ |
| Self-host everything (no PRO) | ✅ AGPL | — |

## What PRO adds

### ✅ Import your own telemetry — isolated per organization (available)
The core of PRO, and live today. Push your satellite's telemetry into your
own **tenant** via the API (`POST /api/v1/tenants/{token}/telemetry`) — from
your ground segment, your provider's API, or an AIT/EGSE bench. Your data is
isolated per organization at the **database layer** (row-level security, not
just app filtering): another tenant cannot read your telemetry even with a
hand-edited query. This is the multi-tenant capacity of Overwatch — see
[TENANT.md](TENANT.md). No decoder needed; push whatever fields you have.

### ⏳ Private, customizable dashboards (roadmap — #13)
Per-organization Grafana with Editor rights: your team builds and reshapes
its own dashboards over its own data, isolated by the same RLS. Today the
private view is the frontend fleet summary + the read API; per-org Grafana
is the next build.

### ⏳ Pass management (roadmap)
Predicted TM/TC visibility windows per satellite and per ground station
(SGP4 over cached elements) — when you can listen, and when you can act.
An operator-facing view of your fleet's contact opportunities.

### ⏳ Command console — TC (roadmap)
An operator interface for telecommand, **layered on your ground-segment
provider's API** — Overwatch provides preparation, history and procedures;
your provider's network remains the uplink. We are the console, not the
radio. (Scoped and sequenced with the customer; not a replacement for your
mission-ops chain.)

### ⏳ Control-room views (roadmap — #49)
Multi-window, URL-driven views laid out across monitors or a video wall —
a sovereign control room your team configures by writing URLs. See
[SATELLITE_VIEW.md](SATELLITE_VIEW.md).

### ✅ Sovereign, managed operation (available)
Self-hosted in Europe, no US hyperscaler in the data path. You pay to *not*
run it yourself: managed hosting, updates, backups, and — as PRO matures —
an SLA and support. The code stays open (AGPL); the service is the product.

## Why pay when it's open source?

Same reason teams pay for hosted Grafana or GitLab they could self-host:
you pay for **operation, not code** — hosting, uptime, isolation at scale,
support, and the features that only matter with private data. The open core
means no lock-in: audit it, or run your own instance, any time.

## How isolation works (in one line)

Organization = tenant, mapped across Keycloak (identity), the API (data +
quotas), and Grafana (dashboards); private rows carry an org id and are
walled by Postgres row-level security. Full model: [TENANT.md](TENANT.md).

## Pricing & starting

- **Fleet Tenant (beta)** — €490/month, 14-day free trial. Isolated tenant,
  push API, private views, up to 5 satellites.
- **Design-partner slots** — free while we shape the offer together; your
  feedback steers the roadmap above.
- Start: [overwatch.confinia.io/pro.html](https://overwatch.confinia.io/pro.html)
  · talk to us: contact@confinia.io

Billing is handled by Polar (merchant of record). Self-hosted deployments
run the whole stack without any of this — billing is optional and
environment-gated.
