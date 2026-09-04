# Batch jobs (run on the Debian VM, in podman — see RULES.md)

Discovery sweeps and backfills for SatNOGS-decodable satellites. Validated
satellites get promoted into `orbit-poc/ingest/satellites.py`.

## Always run through the gateway

SatNOGS throttles **per user** (by token), not per process. These jobs share the
ingest's token and IP, so a sweep that hammers SatNOGS spends the *ingest's*
budget too — that is what got us IP-blocked, twice. So nothing here talks to
db.satnogs.org directly. Every job goes through the SatNOGS egress gateway (one
shared rate limiter + cache — `orbit-poc/gateway/`, #449) via `run.sh`:

```bash
ssh overwatch 'cd ~/projects/overwatch/batch && ./run.sh probe3.py'
```

`run.sh` joins the compose network, sets `SATNOGS_BASE` to the gateway, and
**blackholes db.satnogs.org**, so a script that still hardcodes the host fails
fast rather than overspending the budget. Every script reads `SATNOGS_BASE`
(default `https://db.satnogs.org/api` for standalone dev; the launcher overrides
it to the gateway). Never run a SatNOGS job with a bare `podman run` — that is
the direct path the gateway exists to remove.

The gateway caches (telemetry 30m, TLE 6h, catalogue 24h), so re-running a sweep
or backfill mostly hits the cache, not SatNOGS.

## Knobs

- `OW_BATCH_PKGS` — pip packages to install (default `requests psycopg2-binary
  satnogs-decoders`).
- `OW_ENV` — path to the stack `.env` for the token and DSN (default
  `../orbit-poc/.env`).
- `OW_NET` — compose network (default `orbit-poc_default`).

## Not yet gatewayed

`resolve_satnogs_dashboards.py` scrapes `db.satnogs.org/satellite/<norad>` HTML
(not the `/api`), which the gateway does not proxy — under `run.sh` it is
blackholed and will fail. Run it deliberately and sparingly, or extend the
gateway to cover that path, before relying on it.
