# Test — signup / subscription flow

What we validate about a **new user signing up** and getting an isolated
organization, and how it is checked automatically.

Suite: [`orbit-poc/api/test_subscription.py`](orbit-poc/api/test_subscription.py).
CI: `.github/workflows/tests.yml` job **subscription**.

## What is validated

| # | Expectation | Test |
|---|---|---|
| 1 | Anonymous users keep full open-data access (no key) | `test_anonymous_open_data_ok` |
| 2 | `/v1/me` is 401 when not signed in | `test_anonymous_me_is_401` |
| 3 | Org endpoints refuse anonymous callers | `test_anonymous_cannot_read_org` |
| 4 | A signed-in user's identity + organization materialize from the token | `test_me_returns_identity_and_org` |
| 5 | **One organization never sees another's data** | `test_org_data_isolation` |
| 6 | An org service token pushes under its org and reads back | `test_service_token_push_and_read` |
| 7 | Unknown tenant/token → 404 | `test_unknown_token_rejected` |
| 8 | Over-size batch → 413 | `test_batch_over_limit_rejected` |

## How it runs

Keycloak identity is monkeypatched at the single boundary (`main._claims`)
so the suite exercises the **organization logic** against a real Postgres
without a live Keycloak. The end-to-end path *through* Keycloak (real
login, real org claim) is covered separately by the manual E2E below and,
later, a scheduled smoke test.

CI (GitHub Actions): a `postgres:16` service, `db/init.sql` loaded, then
`pytest test_subscription.py`. Runs on every push and pull request.

Local / on-VM reproduction:

```sh
podman run -d --rm --name t-pg -e POSTGRES_USER=orbit -e POSTGRES_PASSWORD=orbit \
  -e POSTGRES_DB=orbit -p 5432:5432 docker.io/library/postgres:16
psql "host=localhost user=orbit password=orbit dbname=orbit" -f orbit-poc/db/init.sql
cd orbit-poc/api && pip install -r requirements.txt pytest httpx
DB_DSN="dbname=orbit user=orbit password=orbit host=localhost port=5432" pytest test_subscription.py -v
```

## Manual end-to-end (through real Keycloak)

1. Incognito → https://overwatch.confinia.io → **Sign in / Register** →
   register (name, email, password).
2. Back on the map, signed in → **create your organization**.
3. After the automatic re-login, **Your fleet — <org> (private)** appears
   and open data is hidden by default.
4. Create a service token (browser console) and push a point; it appears
   in *Your fleet*, and only for that organization.

## Latest results

- **Suite: 8 passed** (2026-07-22), run in a `python:3.12` container on the
  VM against a fresh `postgres:16`.
- This run found and fixed a real defect: `_require_org` inserted the
  `org_user` row before the `organization` it references, causing a
  foreign-key violation for any user arriving from an **upstream Keycloak
  organization** (invitation path). Order corrected; suite green.
- **CI activation pending**: `.github/workflows/tests.yml` is authored in
  the repo; adding it to `.github/workflows/` on GitHub needs a push with
  the `workflow` token scope (maintainer action: `gh auth refresh -s
  workflow` then push, or add the file via the GitHub web UI). Until then
  results are from the local/VM run above; this section updates with the
  Actions run link once CI is live.
