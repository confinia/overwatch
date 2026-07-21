# v2 stack — self-serve multi-tenant platform (epic #11)

Isolated from the free/open version. One access point: the app caddy
routes paths to this stack (`/auth/*` → Keycloak :8095; later
`/grafana2/*` and org-scoped app surfaces).

## Journey this stack exists to serve (acceptance criteria, #11)
1. Register an organization + user — self-serve (Keycloak).
2. Pay for API access linked to the organization (Polar, #16).
3. Org admin pushes private telemetry via the API (org tokens, #14).
4. Org users see the injected data in the frontend (#13 + frontend).

## Deploy notes (first deployment — next session)
- `.env` (VM-side only): `POSTGRES_PASSWORD`, `KC_DB_PASSWORD`,
  `KC_BOOTSTRAP_ADMIN_USERNAME/PASSWORD`, real client secrets.
- App caddy: `handle /auth/*` → `host.containers.internal:8095`
  (cross-compose-project routing goes via the host port).
- Email verification stays OFF until an SMTP relay exists; enable and
  set `verifyEmail: true` before real customers register.
- Keycloak Organizations: create-on-signup flow + Polar checkout carry
  the org id in metadata (#16).
