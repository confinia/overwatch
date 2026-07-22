# Security policy

## Reporting a vulnerability

Email **contact@confinia.io** with details and reproduction steps. Please
report privately first and allow reasonable time to fix before public
disclosure. We do not run a paid bounty, but we credit reporters who ask.

## Security model

- **Open core.** The code is AGPL and self-hostable. Running your own
  instance keeps your data entirely under your control; the hosted service
  at overwatch.confinia.io is operated by us under this policy.
- **Data classes.**
  - *Open data* (SatNOGS/CelesTrak fleet): public, free, keyless.
  - *Private tenant data* (an organization's own telemetry): isolated per
    organization (org id + row-level checks), visible only to that
    organization's members and machine tokens.
  - *No end-user PII beyond account basics.* Identity (name, email) lives
    in Keycloak; the app stores a reflection for attribution only.
- **Identity.** Keycloak is the single identity provider; one OpenID
  client, one token (httpOnly cookie) across the app, API and Grafana.
- **Sovereignty.** Self-hosted in Europe, no US hyperscaler in the data
  path. Billing (Polar) is the one external processor and is optional for
  self-hosters.
- **Privacy by construction.** Request metrics reduce IPs to a country
  (GeoIP) or a salted daily hash — no raw IP is stored; the usage beacon
  carries no identifiers.

## Operational controls (hosted instance)

- Backends bind to `127.0.0.1`; a single caddy edge terminates TLS
  (Let's Encrypt) and is the only public entry point.
- Secrets live in `.env` files (0600, never committed, never rsynced) and
  GitHub Actions secrets — never in the repo or images.
- Zero-downtime blue/green deploys with instant rollback; a test gate runs
  before any deploy.
- Daily encrypted database backups (tenant data + Keycloak), 14-day
  retention, with a documented restore procedure.

## For self-hosters

Set your own secrets in `orbit-poc/.env` and `orbit-poc/v2/.env`; the
stack runs fully without any billing integration. Put the services behind
your own TLS edge and firewall to `127.0.0.1` as we do. Rotate the
Keycloak bootstrap admin after first login.
