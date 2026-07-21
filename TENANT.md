# Multi-tenancy in Overwatch

How organizations, users, dashboards and API access fit together. The
**organization is the tenant**: one isolation unit, mapped 1:1:1 across
the three systems that share it.

```
        Keycloak org  ←──── identity: who you are, which org, which role
              │
     ┌────────┼───────────────┐
     ▼        ▼               ▼
 Overwatch   Grafana        API access
 org         org            (user tokens + org service tokens)
 (data,      (dashboards,
  quotas)     views)
```

Free/paid boundary (fixed): the **frontend and all open-data API access
are free**. An organization and its private data is the paid unit —
the day private telemetry flows, it is a subscription.

## 1 · Identity — Keycloak account and organization

Keycloak (realm `overwatch`, own isolated stack) is the only place
accounts exist:

- **Self-registration**: a person signs up with name and email (email is
  the username), verifies it, and creates or joins an **organization**
  (Keycloak's native Organizations feature).
- **Roles per organization**: `org_admin` (manage members, credentials,
  billing, write data) and `org_member` (read and observe).
- Every issued token (OIDC/JWT) carries the user's identity (`sub`,
  `email`, `name`), the organization id, and the role — downstream
  systems trust the token, never a password.
- **A single Keycloak client (`overwatch`) serves everything.** Grafana
  supports one OpenID client, so the app shares it: Grafana and the
  Overwatch frontend perform the code flow against the same client
  (different redirect URIs), and the API validates the same tokens as a
  resource server (JWKS). **The token travels in a secure, httpOnly
  cookie on the single hostname**: one login sets it, and every surface
  reads the same session — the frontend's API calls send it
  automatically, the API accepts it (cookie or `Authorization: Bearer`),
  and the app caddy forwards it to Grafana's JWT auth. One login, one
  cookie, one token — valid in the frontend, in Grafana, and on the API.
  This cookie/JWT SSO into embedded, tenant-isolated dashboards is the
  productized mechanism of the platform itself.

### How a user gets an organization

Keycloak is the **source of truth**; the SaaS follows the token and
auto-materializes any organization it sees (first authenticated call
mirrors the org and creates its tenant record — no operator action).
Three paths in:

1. **Invitation** (stock Keycloak): an org admin invites a colleague;
   on acceptance the next token carries the membership — the SaaS
   recognizes them as a member of the existing tenant immediately.
2. **Email-domain matching** (stock Keycloak, one realm-flow toggle,
   not yet enabled): an org declares its domain; new users registering
   with a matching email are attached automatically — whole teams
   self-join without invitations.
3. **`POST /api/v1/orgs`** — the "create an organization" button stock
   Keycloak lacks for end users: it creates the org IN Keycloak via the
   admin API and joins the caller. The SaaS never invents org state.

The only organization data the SaaS owns is what Keycloak cannot know:
quotas, subscription state (billing), service tokens, and the telemetry
itself.

## 2 · Overwatch SaaS — the organization registry

The Overwatch application holds no passwords. It keeps:

- an **organization record** (id ↔ Keycloak org, subscription state
  driven by billing webhooks, quotas, created/suspended timestamps);
- a **user reflection** synced from token claims on first sight (name,
  email, org, role) — enough to attribute actions and show members,
  never an authentication source;
- all **private data keyed by organization**: satellites, pushed
  telemetry, decoder definitions. Every row carries the org id, and
  row-level security makes cross-organization reads impossible at the
  database layer, not just the application layer.

## 3 · Grafana — tenant-isolated dashboards

One **Grafana organization per tenant organization**, provisioned
automatically when the org is activated:

- Users enter through OIDC (same Keycloak session); an org-mapping
  places them in *their* Grafana org only.
- Members arrive as **Editors**: each organization builds and customizes
  its own dashboards over its own data — dashboards are theirs, not a
  shared template they may not touch.
- Isolation is enforced below Grafana: the org's datasource connects
  with credentials that row-level security restricts to that org's rows.
  A misconfigured dashboard can never read another tenant's data.

## 4 · API access — who is reading or writing

Two credential types, both organization-scoped:

| Credential | Who uses it | Can | Issued by |
|---|---|---|---|
| **User token** (OIDC JWT) | Humans and their tools | Read org data; write if `org_admin` | Keycloak login |
| **Org service token** | Machines (ground segment, pipelines, AIT benches) | Push and read telemetry for the org | Org admin, from the account; revocable individually. Issued by Overwatch (opaque), not Keycloak — preserves the single-client rule |

Every API call is attributed to (organization, identity): user calls by
their token claims, machine calls by the named service token. Rate
limits, daily ingest quotas and usage metering apply per organization —
metering feeds billing, enforcement stays in the API.

Open-data endpoints (`/api/v1/satellites`, `/telemetry/{norad}`,
`/stations`, …) remain keyless and free — they serve the public fleet
and never touch organization data.

## 5 · Billing — Polar and API access

The subscription is **organization-level**, handled by Polar as merchant
of record; API access follows the organization's state:

| Org state | How it happens | API effect |
|---|---|---|
| Registered | Self-serve signup + org creation | Beta: push allowed within default quota; target: trial or subscribe before private push |
| Subscribed | Checkout linked to the org (the app creates the Polar checkout carrying the org id; the `subscription.active` webhook flips the org) | Full quota; usage metered per org and reported to Polar's customer meter (billing informs, the API enforces) |
| Past due / canceled | Polar webhook | Push refused (402 pointing at the customer portal); reads keep a grace window, then the org is archived |

During the beta the checkout is a public link and activation is manual
(operator on the sale notification); the target removes both manual
steps. Design-partner slots are 100%/3-month discount codes on the same
product. Self-hosted deployments run without any of this: billing is
optional and environment-gated — an instance without Polar credentials
treats every organization as active.

## The journey these pieces serve

1. Register a user and an organization (self-serve, Keycloak).
2. Subscribe — the checkout is linked to the organization; the webhook
   activates it and provisions Grafana + quotas, with no operator action.
3. As `org_admin`, push private telemetry through the API with an org
   service token.
4. As any member of the organization, open the frontend and see the
   injected data — visible to your organization only, on dashboards your
   organization can reshape.

## Current state vs target

| Piece | Today (beta) | Target |
|---|---|---|
| Identity & orgs | Manual tenant key by email | Keycloak self-serve (issues #12, #15) |
| Org registry | `tenant` table, single key | Org model + user reflection (#14) |
| Dashboards | Admin-shown demo dashboard | Grafana org per tenant, Editor members (#13) |
| API credentials | Tenant key (uuid) | User tokens + org service tokens (#14) |
| Activation on payment | Manual on sale notification | Billing webhook → provisioning (#16) |

Self-hosters run all of this without the billing integration — billing
is optional and environment-gated by design.
