# Overwatch — Specifications

What the Overwatch SaaS is expected to do, as it stands today. Companion
docs: architecture in [DEV.md](DEV.md), multi-tenancy in
[TENANT.md](TENANT.md), monitoring in [MONITORING.md](MONITORING.md),
business in [BUSINESS.md](BUSINESS.md).

## Purpose

A sovereign, open-source **satellite telemetry control room**: fuse
satellite positions with decoded telemetry and the volunteer reception
network into one observable surface — free on open data, paid on your own
private data. Self-hostable by anyone; operated as a hosted service by us.

## Product principles (non-negotiable)

1. **Open core.** The whole stack is AGPL and runs without any billing
   integration; the hosted instance is what people pay for. Billing code
   is optional and environment-gated.
2. **Pricing follows the data, not the surface.** Frontend: always free.
   Open-data API: always free (rate limits protect, never monetize).
   Private end-user data: paid.
3. **Sovereignty.** Self-hosted in Europe, no US hyperscaler in the data
   path. Open data in, open code, auditable.
4. **Single access point.** One hostname (`overwatch.confinia.io`); paid
   v2 keeps infrastructure isolation but shares the hostname.
5. **Honest state.** No SLA claimed before it exists; a satellite with no
   public decoder is absent rather than faked.

## Functional expectations

### A. Open data (anonymous, free)
- Real-time globe of the satellites currently broadcasting decodable open
  telemetry; auto-select the freshest-heard satellite on load.
- Per-satellite view: ground track, decoded telemetry dashboards
  (positions live for all; health panels only where real data exists),
  battery-vs-eclipse fusion.
- Reception network: which volunteer ground stations heard a satellite,
  and where it was at each reception — clickable per frame.
- Station-first view: search a callsign, see its receptions across the
  fleet; shareable deep links (`#station:CALLSIGN`).
- Public read API `/api/v1/*`: satellites, track, receptions, telemetry,
  stations. Keyless, rate-limited, CC-BY-SA attributed.

### B. Identity & organizations
- Self-registration (name, email) via Keycloak; organizations are the
  tenant unit (see TENANT.md). Single Keycloak client shared by the app,
  Grafana and the API; one OpenID token in an httpOnly cookie.
- A user's organization can come from an invitation, email-domain
  matching, or the self-serve create-org endpoint.

### C. Private data (paid)
- Signed-in org members get a **private fleet** view (open data hidden by
  default, toggle to show).
- Org admins push private telemetry via the API using org service tokens;
  data is isolated per organization (org_id + row-level security).
- Read back via API; per-organization Grafana dashboards with Editor
  rights (target — issue #13).
- Per-organization quotas and usage metering.

### D. Subscription & billing
- Pro registration via Polar (merchant of record): €490/mo Fleet Tenant,
  14-day trial, design-partner discount codes.
- Subscription is organization-level; activation provisions the org
  (target: webhook-driven, issue #16; beta: manual on sale notification).

## Non-functional expectations

- **Zero-downtime deploys**: two blue/green compose stacks, caddy
  health-checked color swap, instant rollback (see DEV.md).
- **Isolation**: v2 identity/data stack separate from the free version;
  cross-org reads impossible at the database layer.
- **Observability**: request/latency/usage metrics per route and per
  organization; see MONITORING.md.
- **Politeness**: exactly one service touches upstream APIs
  (CelesTrak/SatNOGS), behind a cache boundary.
- **Reproducibility**: infra as code (compose, realm-as-code, caddy
  template); tests in CI (see TEST_SUBSCRIPTION.md, TEST_POLAR.md).

## Current state (2026-07-22)

| Capability | State |
|---|---|
| Open-data map + API, stations, mobile | Live |
| Blue/green zero-downtime deploys | Live |
| Keycloak identity, self-serve orgs | Live (org-claim E2E being validated) |
| Cookie SSO (app + API) | Live |
| Private push/read + service tokens + isolation | Live |
| Polar product, checkout, trial, codes | Live (activation manual) |
| Per-org Grafana dashboards | Pending (#13) |
| Webhook-driven provisioning | Pending (#16) |
| CI test suites | Authored + passing; workflow activation pending |

Open work is tracked as GitHub issues (epic #11 for the v2 platform).
