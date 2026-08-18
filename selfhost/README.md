# Self-hosted Overwatch

Run the complete Overwatch control room — live globe, open-data telemetry,
your own private satellites, per-organization Grafana — **on your own
infrastructure**. Same images as the hosted service, nothing held back, no
account with anyone required. The self-hosted edition has **no billing
surface at all**: every feature is simply available, and billing endpoints do
not exist (they answer 404 by design).

The only external dependency is the public open-data feed the ingest reads
(SatNOGS network + public TLEs). Everything else — database, identity,
dashboards — runs in the containers below and stays on your machines.

## Prerequisites

- Linux host (or macOS for evaluation) with **docker compose v2** or
  **podman-compose**
- 4 GB RAM free (Keycloak + Grafana + Postgres ×2 + the app)
- outbound HTTPS (open-data feed, container images)
- for a real deployment: a DNS name and a TLS terminator in front (or let
  the bundled Caddy issue certificates — see `Caddyfile`)

## Install

```bash
git clone https://github.com/confinia/overwatch
cd overwatch/selfhost
cp .env.example .env
# fill in .env — every secret is yours; generate them with: openssl rand -hex 24
./up.sh
```

`up.sh` builds the images, starts the stack, bootstraps Keycloak (realm
`overwatch`, self-registration on, the OIDC client wired to your
`PUBLIC_BASE`) and waits for the front door. It is idempotent — re-run it
after any `.env` change or upgrade.

First local run: keep `PUBLIC_BASE=http://localhost:8080`, then open
<http://localhost:8080>. The globe is live as soon as the ingest has pulled
its first TLE/positions cycle (a few minutes).

## First login

1. Open `PUBLIC_BASE` → **Account** → **Sign in / Register**.
2. Register — with no SMTP configured, accounts work immediately (e-mail
   verification and password reset switch on automatically when you set the
   `SMTP_*` values and re-run `./up.sh`).
3. Create your organization, then mint a **service token** on the account
   page and push telemetry:

```bash
curl -X POST "$PUBLIC_BASE/api/v1/tenants/<token>/telemetry" \
  -H 'content-type: application/json' \
  -d '{"satellite":"MySat-1","points":[{"ts":"2026-08-18T12:00:00Z","field":"battery_v","value":7.9}]}'
```

No satellite yet? Run the simulator against your token and watch a plausible
one appear:

```bash
podman build -t simsat ../orbit-poc/simsat
podman run --rm --network=host -e SIM_KEY=<token> \
  -e SIM_BASE="$PUBLIC_BASE" simsat
```

## Operate

| task | command |
|---|---|
| status | `docker compose ps` |
| logs | `docker compose logs -f api web ingest` |
| upgrade | `git pull && ./up.sh` |
| stop | `docker compose down` |
| backup | `docker compose exec db pg_dump -U orbit orbit > overwatch.sql`, plus the `kc_pgdata` and `grafana` volumes |
| restore | recreate volumes, `psql -U orbit orbit < overwatch.sql` |

## TLS

The bundled Caddy listens on plain HTTP (`HTTP_BIND`, default `:8080`) —
put your existing reverse proxy / TLS terminator in front and set
`PUBLIC_BASE` to the public `https://` URL. Alternatively edit
`selfhost/Caddyfile`, replace `:80` with your domain, publish ports 80/443,
and Caddy provisions certificates itself.

## Troubleshooting

- **Front answers 502** — the stack is still building/booting; `docker
  compose logs caddy web api`.
- **Sign-in loops or 401s** — `PUBLIC_BASE` must match the URL in the
  browser exactly (scheme included); re-run `./up.sh` after changing it.
- **Globe empty after 10 minutes** — check `docker compose logs ingest`;
  ensure `COMPOSE_PROFILES=data` is set in `.env`.
- **Password reset greyed out** — configure `SMTP_*` and re-run `./up.sh`.
