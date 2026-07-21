"""
Overwatch public API (/v1) — read-only satellite data from the local cache.

Separate from the map's internal /api/* endpoints so the product API and the
UI can evolve independently. Same boundary rule as everything else: this
service reads ONLY from Postgres; upstream (CelesTrak, SatNOGS) is touched
by ingest alone.

Pattern lifted from the confinia API (api.confinia.io): landing page with
copy-pasteable examples, OpenAPI /docs, self-serve keys (email = lead),
per-IP rate limits, per-key daily metering, OTel request counter with
route/status/country dims -> collector -> Prometheus -> ops dashboard.

Free during development; REQUIRE_API_KEY=true flips the beta gate.
"""
from __future__ import annotations

import hashlib
import os
import time
from contextlib import asynccontextmanager, contextmanager

import psycopg2
import psycopg2.pool
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

DSN = os.environ["DB_DSN"]
RATE_PER_SEC = int(os.environ.get("RATE_PER_SEC", "5"))
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", "120"))
REQUIRE_KEY = os.environ.get("REQUIRE_API_KEY", "false").lower() == "true"
# Paths that never require a key (docs, health, key issuance itself).
OPEN_PATHS = ("/", "/v1", "/v1/docs", "/v1/openapi.json", "/v1/healthz",
              "/healthz", "/v1/keys")

pool: psycopg2.pool.SimpleConnectionPool | None = None

KEYS_SQL = """
CREATE TABLE IF NOT EXISTS api_key (
    key        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email      text NOT NULL,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    active     boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS api_usage (
    key      uuid NOT NULL REFERENCES api_key(key),
    day      date NOT NULL,
    requests bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (key, day)
);
-- Unique visitors per day/country. Never the IP: client_hash is a salted
-- digest (env secret + UTC day), irreversible and uncorrelatable across days.
-- UNLOGGED: observability data, losable without regret. Purged at 45 days.
CREATE UNLOGGED TABLE IF NOT EXISTS visitor_daily (
    day         date  NOT NULL,
    country     text  NOT NULL,
    client_hash bytea NOT NULL,
    PRIMARY KEY (day, client_hash)
);
DELETE FROM visitor_daily WHERE day < CURRENT_DATE - 45;
-- v2 organizations (tenant = organization; id mirrors the Keycloak org id).
CREATE TABLE IF NOT EXISTS organization (
    id         uuid PRIMARY KEY,
    name       text NOT NULL,
    active     boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS org_user (
    sub        uuid NOT NULL,
    org        uuid NOT NULL REFERENCES organization(id),
    email      text,
    name       text,
    last_seen  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sub, org)
);
CREATE TABLE IF NOT EXISTS org_token (
    token      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org        uuid NOT NULL REFERENCES organization(id),
    label      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked    boolean NOT NULL DEFAULT false
);
-- Private tenants: a party plugs ITS OWN satellite telemetry in and
-- observes it in isolated dashboards. Never mixed with the public fleet.
CREATE TABLE IF NOT EXISTS tenant (
    key        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text NOT NULL,
    email      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    active     boolean NOT NULL DEFAULT true,
    max_points_day bigint NOT NULL DEFAULT 200000
);
CREATE TABLE IF NOT EXISTS tenant_telemetry (
    tenant    uuid NOT NULL REFERENCES tenant(key),
    satellite text NOT NULL,
    ts        timestamptz NOT NULL,
    field     text NOT NULL,
    value_num double precision,
    value_txt text,
    PRIMARY KEY (tenant, satellite, ts, field)
);
CREATE INDEX IF NOT EXISTS tenant_tlm_idx
    ON tenant_telemetry (tenant, satellite, field, ts DESC);
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool
    last_err = None
    for _attempt in range(30):                     # db may start after us
        try:
            pool = psycopg2.pool.SimpleConnectionPool(1, 8, DSN)
            break
        except psycopg2.OperationalError as e:
            last_err = e
            time.sleep(2)
    if pool is None:
        raise RuntimeError(f"Postgres unreachable: {last_err}")
    conn = pool.getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(KEYS_SQL)
    finally:
        pool.putconn(conn)
    yield
    pool.closeall()


PRODUCT_VERSION = os.environ.get("OVERWATCH_VERSION", "dev")

app = FastAPI(
    title="Overwatch API",
    version=PRODUCT_VERSION,
    description="Live positions, decoded telemetry and reception network for "
                "the open-telemetry cubesat fleet. Telemetry & receptions: "
                "SatNOGS DB (CC-BY-SA), decoded locally with satnogs-decoders. "
                "Orbital elements: CelesTrak.",
    lifespan=lifespan,
    # Public URL is /api/v1/* (caddy strips /api); root_path makes the docs
    # UI and OpenAPI "servers" resolve under the public prefix.
    root_path="/api",
    docs_url="/v1/docs", openapi_url="/v1/openapi.json", redoc_url=None,
)

# Public read-only API: open CORS (the map mirror lives on GitHub Pages).
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["GET", "POST"], allow_headers=["*"])

# ---------------------------------------------------------------------------
#  Observability: OTel request counter -> collector -> Prometheus -> Grafana
#  ops dashboard. Calling country via GeoIP (DB-IP Country Lite, CC BY 4.0)
#  on the anonymized IP — the IP itself is never stored, only the country.
# ---------------------------------------------------------------------------
REQ_COUNTER = None
OTLP = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
if OTLP:
    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.resources import Resource
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{OTLP}/v1/metrics"),
            export_interval_millis=15000)
        otel_metrics.set_meter_provider(MeterProvider(
            resource=Resource.create(
                {"service.name": os.environ.get("OTEL_SERVICE_NAME", "overwatch-api")}),
            metric_readers=[reader]))
        REQ_COUNTER = otel_metrics.get_meter("overwatch").create_counter(
            "ovw.api.requests", description="API requests by route/status/country")
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)   # traces -> spanmetrics (latency)
    except Exception as e:                        # observability never breaks the API
        print(f"[obs] OpenTelemetry not initialized: {e}")

GEOIP = None
try:
    import maxminddb
    GEOIP = maxminddb.open_database("/geoip/dbip-country-lite.mmdb")
except Exception:
    pass


def client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() \
        or (request.client.host if request.client else "")


def client_country(request: Request) -> str:
    ip = client_ip(request)
    if not GEOIP or not ip:
        return "??"
    try:
        rec = GEOIP.get(ip)
        return (rec or {}).get("country", {}).get("iso_code", "??")
    except Exception:
        return "??"


def client_kind(request: Request) -> str:
    """Where the call comes from: our map UI, the GitHub Pages mirror, or a
    direct API consumer. Origin/Referer only — bounded cardinality, no PII."""
    ref = request.headers.get("origin") or request.headers.get("referer") or ""
    if "confinia.github.io" in ref:
        return "mirror"
    if "overwatch.confinia.io" in ref:
        return "site"
    return "direct"


# ---------------------------------------------------------------------------
#  Unique visitors per day and country — GDPR posture: the IP is reduced to a
#  salted digest (env secret + UTC day), irreversible without the secret and
#  uncorrelatable across days. The per-worker memory cache avoids an INSERT
#  per request; the table provides cross-worker exactness.
# ---------------------------------------------------------------------------
VISITOR_SECRET = os.environ.get("VISITOR_SALT_SECRET", "")
_seen_today: set[bytes] = set()
_seen_day = ""


def note_visitor(ip: str, country: str) -> None:
    global _seen_day
    if not ip or not VISITOR_SECRET or pool is None:
        return
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if day != _seen_day:
        _seen_day = day
        _seen_today.clear()
    h = hashlib.sha256(f"{VISITOR_SECRET}|{day}|{ip}".encode()).digest()[:16]
    if h in _seen_today:
        return
    if len(_seen_today) < 200_000:              # per-worker memory bound
        _seen_today.add(h)
    try:
        conn = pool.getconn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO visitor_daily (day, country, client_hash) "
                    "VALUES (CURRENT_DATE, %s, %s) ON CONFLICT DO NOTHING",
                    (country, h))
        finally:
            pool.putconn(conn)
    except Exception:
        pass                                    # fail-open: never blocking


# --- Rate limiting: per IP, in memory, two fixed windows -------------------
_rate: dict[str, list] = {}          # ip -> [sec_window, sec_n, min_window, min_n]


def rate_limited(ip: str) -> bool:
    now = int(time.time())
    if len(_rate) > 50_000:                     # memory bound
        _rate.clear()
    w = _rate.setdefault(ip, [now, 0, now - now % 60, 0])
    if w[0] != now:
        w[0], w[1] = now, 0
    m = now - now % 60
    if w[2] != m:
        w[2], w[3] = m, 0
    w[1] += 1
    w[3] += 1
    return w[1] > RATE_PER_SEC or w[3] > RATE_PER_MIN


def meter_key(request: Request) -> str | None:
    """Validate the optional API key and count today's usage. Fail-open."""
    key = request.headers.get("x-api-key") or request.query_params.get("api_key")
    if not key or pool is None:
        return None
    try:
        conn = pool.getconn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT active FROM api_key WHERE key = %s::uuid", (key,))
                row = cur.fetchone()
                if not row or not row[0]:
                    return None
                cur.execute(
                    "INSERT INTO api_usage (key, day, requests) VALUES (%s::uuid, CURRENT_DATE, 1) "
                    "ON CONFLICT (key, day) DO UPDATE SET requests = api_usage.requests + 1", (key,))
                return key
        finally:
            pool.putconn(conn)
    except Exception:
        return None


@app.middleware("http")
async def access_control(request: Request, call_next):
    t0 = time.perf_counter()
    ip = client_ip(request)
    path = request.url.path
    # Internal traffic (VM, compose network) is unlimited — the public comes
    # through caddy and arrives with its real IP in X-Forwarded-For.
    internal = ip.startswith(("10.", "127.", "192.168.")) or not ip
    if not internal and path.startswith("/v1") and rate_limited(ip):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"detail": f"Too many requests (limits: {RATE_PER_SEC}/s, {RATE_PER_MIN}/min). "
                       "Need more? contact@confinia.io"},
            status_code=429,
            headers={"Retry-After": "10",
                     "X-RateLimit-Limit": f"{RATE_PER_SEC};w=1, {RATE_PER_MIN};w=60"})
    valid_key = meter_key(request) if path.startswith("/v1") else None
    if (REQUIRE_KEY and valid_key is None and path.startswith("/v1")
            and path not in OPEN_PATHS and not path.startswith("/v1/keys")):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "API key required: POST /api/v1/keys {\"email\"} "
                                       "then header X-API-Key."}, status_code=401)
    response = await call_next(request)
    response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
    response.headers["X-RateLimit-Limit"] = f"{RATE_PER_SEC};w=1, {RATE_PER_MIN};w=60"
    country = client_country(request)
    # The app caddy health-checks /healthz on every upstream every 2 s;
    # counting those floods the metrics with country="??" noise. Real
    # (external) healthz calls still count.
    probe = internal and path.rstrip("/") in ("/healthz", "/v1/healthz")
    if REQ_COUNTER is not None and not probe:
        route = request.scope.get("route")
        REQ_COUNTER.add(1, {
            "route": getattr(route, "path", path),
            "method": request.method,
            "status": str(response.status_code),
            "country": country,
            "client": client_kind(request),
            "keyed": valid_key is not None,
        })
    if not internal:
        note_visitor(ip, country)
    return response


@contextmanager
def cursor():
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        pool.putconn(conn)


def known_norad(cur, norad: int) -> bool:
    cur.execute("SELECT 1 FROM satellite WHERE norad = %s", (norad,))
    return cur.fetchone() is not None


# --- Endpoints -------------------------------------------------------------

@app.get("/v1/satellites")
def satellites():
    """The whole fleet: latest known position, last decoded frame, metadata."""
    with cursor() as cur:
        cur.execute("""
            SELECT s.norad, s.name, s.has_telemetry, s.note,
                   p.lat, p.lon, p.alt_km, p.sunlit, p.ts AS position_ts,
                   tf.last_frame
            FROM satellite s
            LEFT JOIN LATERAL (
                SELECT lat, lon, alt_km, sunlit, ts FROM position
                WHERE norad = s.norad ORDER BY ts DESC LIMIT 1
            ) p ON true
            LEFT JOIN LATERAL (
                SELECT max(ts) AS last_frame FROM telemetry
                WHERE norad = s.norad
            ) tf ON true
            ORDER BY s.name""")
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.get("/v1/track/{norad}")
def track(norad: int, minutes: int = Query(100, ge=1, le=10080)):
    """Recent ground track (positions are SGP4-propagated locally, 15 s step;
    retention is 7 days — minutes is capped accordingly)."""
    with cursor() as cur:
        if not known_norad(cur, norad):
            raise HTTPException(404, f"Unknown NORAD id {norad} (see /v1/satellites).")
        cur.execute("""
            SELECT ts, lat, lon, alt_km, sunlit FROM position
            WHERE norad = %s AND ts > now() - %s * interval '1 minute'
            ORDER BY ts""", (norad, minutes))
        return [{"ts": ts.isoformat(), "lat": lat, "lon": lon,
                 "alt_km": alt, "sunlit": sunlit}
                for ts, lat, lon, alt, sunlit in cur.fetchall()]


@app.get("/v1/receptions/{norad}")
def receptions(norad: int, hours: int = Query(24, ge=1, le=168)):
    """Which volunteer ground stations heard this satellite (SatNOGS network,
    station positions decoded from their Maidenhead locators)."""
    with cursor() as cur:
        if not known_norad(cur, norad):
            raise HTTPException(404, f"Unknown NORAD id {norad} (see /v1/satellites).")
        cur.execute("""
            SELECT ts, observer, lat, lon FROM reception
            WHERE norad = %s AND ts > now() - %s * interval '1 hour'
            ORDER BY ts DESC LIMIT 500""", (norad, hours))
        return [{"ts": ts.isoformat(), "observer": obs, "lat": lat, "lon": lon}
                for ts, obs, lat, lon in cur.fetchall()]


@app.get("/v1/telemetry/{norad}/fields")
def telemetry_fields(norad: int):
    """Decoded fields available for this satellite (7-day window): raw beacon
    fields plus the canonical battery_v / battery_i / battery_pct."""
    with cursor() as cur:
        if not known_norad(cur, norad):
            raise HTTPException(404, f"Unknown NORAD id {norad} (see /v1/satellites).")
        cur.execute("""
            SELECT field, count(*) AS points, max(ts) AS last_seen
            FROM telemetry
            WHERE norad = %s AND ts > now() - interval '7 days'
            GROUP BY field ORDER BY field""", (norad,))
        return [{"field": f, "points": n, "last_seen": ts.isoformat()}
                for f, n, ts in cur.fetchall()]


@app.get("/v1/telemetry/{norad}")
def telemetry(norad: int,
              field: str = Query(..., min_length=1, max_length=128,
                                 description="Field name — see /v1/telemetry/{norad}/fields"),
              hours: int = Query(24, ge=1, le=168)):
    """Time series of one decoded telemetry field, straight from the radio
    frames (decoded locally with satnogs-decoders, no upstream call)."""
    with cursor() as cur:
        if not known_norad(cur, norad):
            raise HTTPException(404, f"Unknown NORAD id {norad} (see /v1/satellites).")
        cur.execute("""
            SELECT ts, value_num, value_txt FROM telemetry
            WHERE norad = %s AND field = %s
              AND ts > now() - %s * interval '1 hour'
            ORDER BY ts""", (norad, field, hours))
        return [{"ts": ts.isoformat(), "value": num if num is not None else txt}
                for ts, num, txt in cur.fetchall()]


@app.get("/v1/stations")
def stations_list():
    """Volunteer ground stations that received the fleet in the last 7 days
    (positions decoded from their Maidenhead locators)."""
    with cursor() as cur:
        cur.execute("""
            SELECT observer, max(lat) AS lat, max(lon) AS lon,
                   count(*) AS frames, count(DISTINCT norad) AS satellites,
                   max(ts) AS last_rx
            FROM reception
            WHERE ts > now() - interval '7 days' AND lat IS NOT NULL
            GROUP BY observer ORDER BY frames DESC""")
        return [{"observer": o, "lat": la, "lon": lo, "frames": f,
                 "satellites": s, "last_rx": t.isoformat()}
                for o, la, lo, f, s, t in cur.fetchall()]


@app.get("/v1/stations/{callsign}")
def station_receptions(callsign: str):
    """One station's receptions across the fleet (7 days). The callsign
    matches the part before the grid locator in the observer string."""
    with cursor() as cur:
        cur.execute("""
            SELECT r.ts, r.norad, s.name, r.observer
            FROM reception r JOIN satellite s USING (norad)
            WHERE split_part(r.observer, '-', 1) ILIKE %s
              AND r.ts > now() - interval '7 days'
            ORDER BY r.ts DESC LIMIT 500""", (callsign,))
        rows = cur.fetchall()
    if not rows:
        raise HTTPException(404, f"No receptions by '{callsign}' in the last "
                                 "7 days (tracked fleet only).")
    return [{"ts": ts.isoformat(), "norad": n, "satellite": name,
             "observer": obs} for ts, n, name, obs in rows]


# --- v2 identity: Keycloak (single client), cookie-borne OpenID token ------
import secrets as _secrets
import requests as _rq
import jwt as _jwt
from jwt import PyJWKClient

KC_ISSUER = os.environ.get("KC_ISSUER",
    "https://overwatch.confinia.io/auth/realms/overwatch")
KC_CLIENT_ID = os.environ.get("OVERWATCH_CLIENT_ID", "overwatch")
KC_CLIENT_SECRET = os.environ.get("OVERWATCH_CLIENT_SECRET", "")
KC_ADMIN_USER = os.environ.get("KC_ADMIN_USERNAME", "")
KC_ADMIN_PASS = os.environ.get("KC_ADMIN_PASSWORD", "")
COOKIE = "ovw_token"
_jwks = None


def _jwks_client():
    global _jwks
    if _jwks is None:
        _jwks = PyJWKClient(f"{KC_ISSUER}/protocol/openid-connect/certs")
    return _jwks


def _claims(request: Request):
    """The same OpenID token everywhere: Authorization bearer or cookie."""
    tok = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()         or request.cookies.get(COOKIE, "")
    if not tok:
        return None
    try:
        key = _jwks_client().get_signing_key_from_jwt(tok)
        return _jwt.decode(tok, key.key, algorithms=["RS256"],
                           issuer=KC_ISSUER, options={"verify_aud": False})
    except Exception:
        return None


def _org_of(claims) -> tuple[str, str] | None:
    """Extract (org_id, org_name) from the Keycloak organization claim,
    tolerating its dict/list shapes."""
    o = claims.get("organization")
    if isinstance(o, dict) and o:
        name, meta = next(iter(o.items()))
        return ((meta or {}).get("id") or name, name)
    if isinstance(o, list) and o:
        return (o[0], o[0]) if isinstance(o[0], str) else                (o[0].get("id"), o[0].get("name", "org"))
    return None


def _require_user(request: Request):
    c = _claims(request)
    if not c:
        raise HTTPException(401, "Sign in first: /api/v1/auth/login")
    return c


def _require_org(request: Request):
    c = _require_user(request)
    org = _org_of(c)
    if not org:
        raise HTTPException(403, "No organization yet: POST /api/v1/orgs {\"name\"}")
    with cursor() as cur:
        cur.execute("""INSERT INTO org_user (sub, org, email, name, last_seen)
                       VALUES (%s::uuid, %s::uuid, %s, %s, now())
                       ON CONFLICT (sub, org) DO UPDATE SET last_seen = now(),
                         email = EXCLUDED.email, name = EXCLUDED.name""",
                    (c["sub"], org[0], c.get("email"), c.get("name")))
        cur.execute("""INSERT INTO organization (id, name) VALUES (%s::uuid, %s)
                       ON CONFLICT (id) DO NOTHING""", (org[0], org[1]))
        cur.execute("""INSERT INTO tenant (key, name, email)
                       VALUES (%s::uuid, %s, %s) ON CONFLICT (key) DO NOTHING""",
                    (org[0], org[1], c.get("email", "")))
        cur.connection.commit()
    return c, org


def _kc_admin_token() -> str:
    r = _rq.post(f"{KC_ISSUER.rsplit('/realms/',1)[0]}/realms/master/protocol/openid-connect/token",
                 data={"grant_type": "password", "client_id": "admin-cli",
                       "username": KC_ADMIN_USER, "password": KC_ADMIN_PASS},
                 timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


@app.get("/v1/auth/login", include_in_schema=False)
def auth_login():
    from fastapi.responses import RedirectResponse
    state = _secrets.token_urlsafe(16)
    url = (f"{KC_ISSUER}/protocol/openid-connect/auth?client_id={KC_CLIENT_ID}"
           f"&response_type=code&scope=openid+profile+email+organization"
           f"&redirect_uri=https://overwatch.confinia.io/api/v1/auth/callback"
           f"&state={state}")
    resp = RedirectResponse(url)
    resp.set_cookie("ovw_state", state, max_age=600, httponly=True,
                    secure=True, samesite="lax")
    return resp


@app.get("/v1/auth/callback", include_in_schema=False)
def auth_callback(request: Request, code: str = "", state: str = ""):
    from fastapi.responses import RedirectResponse
    if not code or state != request.cookies.get("ovw_state"):
        raise HTTPException(400, "Invalid login state — retry /api/v1/auth/login")
    r = _rq.post(f"{KC_ISSUER}/protocol/openid-connect/token",
                 data={"grant_type": "authorization_code", "code": code,
                       "client_id": KC_CLIENT_ID, "client_secret": KC_CLIENT_SECRET,
                       "redirect_uri": "https://overwatch.confinia.io/api/v1/auth/callback"},
                 timeout=15)
    if r.status_code != 200:
        raise HTTPException(502, "Token exchange failed")
    resp = RedirectResponse("/")
    resp.set_cookie(COOKIE, r.json()["access_token"], max_age=1740,
                    httponly=True, secure=True, samesite="lax", path="/")
    resp.delete_cookie("ovw_state")
    return resp


@app.get("/v1/auth/logout", include_in_schema=False)
def auth_logout():
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse("/")
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/v1/me")
def me(request: Request):
    """Who am I — identity and organization from the shared OpenID token."""
    c = _require_user(request)
    org = _org_of(c)
    return {"sub": c["sub"], "email": c.get("email"), "name": c.get("name"),
            "organization": {"id": org[0], "name": org[1]} if org else None}


class OrgCreate(BaseModel):
    name: str


@app.post("/v1/orgs", status_code=201)
def create_org(request: Request, body: OrgCreate):
    """Self-serve organization creation: creates the Keycloak organization,
    joins the current user, mirrors it locally. Sign in again afterwards so
    the token carries the new membership."""
    c = _require_user(request)
    if _org_of(c):
        raise HTTPException(409, "You already belong to an organization.")
    name = body.name.strip()[:60]
    if len(name) < 2:
        raise HTTPException(422, "Organization name too short.")
    alias = "".join(ch if ch.isalnum() else "-" for ch in name.lower())[:40]
    at = _kc_admin_token()
    base = f"{KC_ISSUER.rsplit('/realms/',1)[0]}/admin/realms/overwatch"
    h = {"Authorization": f"Bearer {at}"}
    r = _rq.post(f"{base}/organizations", json={
        "name": name, "alias": alias,
        "domains": [{"name": f"{alias}.invalid", "verified": False}]}, headers=h, timeout=15)
    if r.status_code not in (201, 409):
        raise HTTPException(502, f"Organization creation failed ({r.status_code})")
    r2 = _rq.get(f"{base}/organizations?search={alias}", headers=h, timeout=15)
    org_id = r2.json()[0]["id"]
    _rq.post(f"{base}/organizations/{org_id}/members",
             json=c["sub"], headers={**h, "Content-Type": "application/json"}, timeout=15)
    with cursor() as cur:
        cur.execute("""INSERT INTO organization (id, name) VALUES (%s::uuid, %s)
                       ON CONFLICT (id) DO NOTHING""", (org_id, name))
        cur.execute("""INSERT INTO tenant (key, name, email)
                       VALUES (%s::uuid, %s, %s) ON CONFLICT (key) DO NOTHING""",
                    (org_id, name, c.get("email", "")))
        cur.connection.commit()
    return {"id": org_id, "name": name,
            "note": "Sign in again so your session carries the organization."}


# --- Org-scoped data: same storage as tenants, keyed by the org id --------

@app.get("/v1/org/satellites")
def org_satellites(request: Request):
    _, org = _require_org(request)
    with cursor() as cur:
        cur.execute("""SELECT satellite, field, count(*), max(ts)
                       FROM tenant_telemetry WHERE tenant = %s::uuid
                       GROUP BY 1, 2 ORDER BY 1, 2""", (org[0],))
        rows = cur.fetchall()
    return [{"satellite": s, "field": f, "points": n, "last": t.isoformat()}
            for s, f, n, t in rows]


@app.post("/v1/org/telemetry", status_code=202)
def org_push(request: Request, body: TenantPush):
    _, org = _require_org(request)
    return tenant_push(org[0], body)


@app.get("/v1/org/telemetry")
def org_read(request: Request, satellite: str, field: str,
             hours: int = Query(24, ge=1, le=8760)):
    _, org = _require_org(request)
    return tenant_read(org[0], satellite, field, hours)


class TokenCreate(BaseModel):
    label: str


@app.post("/v1/org/tokens", status_code=201)
def org_token_create(request: Request, body: TokenCreate):
    """Org service token for machine push (ground segment, pipelines).
    Use it as the key in /v1/tenants/{token}/telemetry. Revocable."""
    _, org = _require_org(request)
    with cursor() as cur:
        cur.execute("""INSERT INTO org_token (org, label) VALUES (%s::uuid, %s)
                       RETURNING token""", (org[0], body.label[:60]))
        tok = cur.fetchone()[0]
        cur.connection.commit()
    return {"token": str(tok), "label": body.label[:60],
            "push": f"/api/v1/tenants/{tok}/telemetry"}


@app.get("/v1/org/tokens")
def org_token_list(request: Request):
    _, org = _require_org(request)
    with cursor() as cur:
        cur.execute("""SELECT token, label, created_at, revoked FROM org_token
                       WHERE org = %s::uuid ORDER BY created_at""", (org[0],))
        return [{"token": str(t)[:8] + "…", "label": l,
                 "created": c.isoformat(), "revoked": r}
                for t, l, c, r in cur.fetchall()]


# --- Private tenants: push YOUR telemetry, observe it immediately ----------

class TenantPoint(BaseModel):
    ts: str                      # ISO 8601
    field: str
    value: float | str


class TenantPush(BaseModel):
    satellite: str
    points: list[TenantPoint]


def _tenant(cur, key: str):
    cur.execute("SELECT active, max_points_day FROM tenant WHERE key = %s::uuid", (key,))
    row = cur.fetchone()
    if row:
        if not row[0]:
            raise HTTPException(404, "Unknown or inactive tenant key.")
        return row
    # org service token? resolve to the org's tenant record
    cur.execute("""SELECT t.active, t.max_points_day, ot.org
                   FROM org_token ot JOIN tenant t ON t.key = ot.org
                   WHERE ot.token = %s::uuid AND NOT ot.revoked""", (key,))
    row = cur.fetchone()
    if not row or not row[0]:
        raise HTTPException(404, "Unknown or inactive tenant key.")
    return row


@app.post("/v1/tenants/{key}/telemetry", status_code=202)
def tenant_push(key: str, body: TenantPush):
    """Plug your satellite data in: batch-push time-series points into your
    isolated tenant. The key is a tenant key or an org service token."""
    if len(body.points) > 1000:
        raise HTTPException(413, "Max 1000 points per request — batch your pushes.")
    with cursor() as cur:
        row = _tenant(cur, key)
        quota = row[1]
        if len(row) > 2:
            key = str(row[2])          # service token -> write under the org id
        cur.execute("""SELECT count(*) FROM tenant_telemetry
                       WHERE tenant = %s::uuid AND ts > now() - interval '1 day'""", (key,))
        if cur.fetchone()[0] + len(body.points) > quota:
            raise HTTPException(429, f"Daily ingest quota reached ({quota} points/day). "
                                     "Need more? contact@confinia.io")
        for p in body.points:
            num = p.value if isinstance(p.value, (int, float)) else None
            txt = None if num is not None else str(p.value)
            cur.execute("""INSERT INTO tenant_telemetry
                           (tenant, satellite, ts, field, value_num, value_txt)
                           VALUES (%s::uuid, %s, %s::timestamptz, %s, %s, %s)
                           ON CONFLICT (tenant, satellite, ts, field) DO UPDATE
                           SET value_num = EXCLUDED.value_num,
                               value_txt = EXCLUDED.value_txt""",
                        (key, body.satellite, p.ts, p.field, num, txt))
        cur.connection.commit()
    return {"accepted": len(body.points), "satellite": body.satellite}


@app.get("/v1/tenants/{key}/satellites")
def tenant_satellites(key: str):
    """What this tenant has: satellites, fields, freshness."""
    with cursor() as cur:
        _tenant(cur, key)
        cur.execute("""SELECT satellite, field, count(*), max(ts)
                       FROM tenant_telemetry WHERE tenant = %s::uuid
                       GROUP BY 1, 2 ORDER BY 1, 2""", (key,))
        rows = cur.fetchall()
    return [{"satellite": s, "field": f, "points": n, "last": t.isoformat()}
            for s, f, n, t in rows]


@app.get("/v1/tenants/{key}/telemetry")
def tenant_read(key: str, satellite: str, field: str,
                hours: int = Query(24, ge=1, le=8760)):
    """Read back one of your series (also what your dashboards query)."""
    with cursor() as cur:
        _tenant(cur, key)
        cur.execute("""SELECT ts, value_num, value_txt FROM tenant_telemetry
                       WHERE tenant = %s::uuid AND satellite = %s AND field = %s
                         AND ts > now() - %s * interval '1 hour'
                       ORDER BY ts""", (key, satellite, field, hours))
        return [{"ts": ts.isoformat(), "value": n if n is not None else t}
                for ts, n, t in cur.fetchall()]


# --- Keys (free during the beta; email = the design-partner conversation) ---

class KeyRequest(BaseModel):
    email: EmailStr
    note: str | None = None


@app.post("/v1/keys", status_code=201)
def create_key(req: KeyRequest):
    """Create an API key (free — beta). Pass it as the X-API-Key header."""
    with cursor() as cur:
        cur.execute("INSERT INTO api_key (email, note) VALUES (%s, %s) RETURNING key, created_at",
                    (req.email, req.note))
        key, created = cur.fetchone()
        cur.connection.commit()
    return {"key": str(key), "created_at": created.isoformat(),
            "usage": f"/api/v1/keys/{key}/usage"}


@app.get("/v1/keys/{key}/usage")
def key_usage(key: str):
    """Self-service: this key's consumption over the last 30 days."""
    with cursor() as cur:
        cur.execute(
            "SELECT day, requests FROM api_usage "
            "WHERE key = %s::uuid AND day > CURRENT_DATE - 30 ORDER BY day", (key,))
        rows = cur.fetchall()
    return {"key": key, "days": [{"day": d.isoformat(), "requests": n} for d, n in rows],
            "total_30d": sum(n for _, n in rows)}


@app.get("/v1/healthz")
@app.get("/healthz", include_in_schema=False)
def healthz():
    with cursor() as cur:
        cur.execute("SELECT count(*) FROM satellite")
        sats = cur.fetchone()[0]
        cur.execute("SELECT max(ts) FROM position")
        last = cur.fetchone()[0]
    return {"status": "ok", "version": PRODUCT_VERSION, "satellites": sats,
            "last_position": last.isoformat() if last else None}


# --- Landing page (same spirit as api.confinia.io) --------------------------

LANDING = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Overwatch API</title><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#10151d; color:#e8eaed; font:16px/1.6 system-ui,-apple-system,sans-serif; }
  main { max-width:46rem; padding:2rem; }
  h1 { font-size:1.6rem; margin:0 0 .3rem; } h1 span { color:#7ab8ff; }
  p.tag { margin:0 0 1.4rem; opacity:.85; }
  pre { background:#0b0f16; border:1px solid #26314a; border-radius:8px;
        padding:.9rem 1rem; overflow-x:auto; font-size:.85rem; }
  a { color:#7ab8ff; text-decoration:none; } a:hover { text-decoration:underline; }
  ul { padding-left:1.2rem; } footer { margin-top:1.6rem; font-size:.8rem; opacity:.7; }
</style></head><body><main>
<h1><span>Overwatch</span> API</h1>
<p class="tag">Live positions, decoded telemetry and reception network for the
~23 cubesats currently broadcasting open telemetry — batteries, temperatures,
currents decoded locally from their actual radio frames. 100% open data,
self-hosted in Europe.</p>
<pre>The whole fleet — latest positions + when each satellite was last heard:

GET <a href="/api/v1/satellites">/api/v1/satellites</a>

One satellite (CUBEBEL-2, the richest live beacon — NORAD 57175):

GET <a href="/api/v1/track/57175?minutes=100">/api/v1/track/57175?minutes=100</a>            → recent ground track (+ eclipse flag)
GET <a href="/api/v1/receptions/57175?hours=24">/api/v1/receptions/57175?hours=24</a>          → volunteer stations that heard it
GET <a href="/api/v1/telemetry/57175/fields">/api/v1/telemetry/57175/fields</a>             → decoded fields available
GET <a href="/api/v1/telemetry/57175?field=battery_v&amp;hours=24">/api/v1/telemetry/57175?field=battery_v&amp;hours=24</a>  → battery voltage series

Canonical fields work fleet-wide: battery_v, battery_i, battery_pct —
raw beacon fields (per-satellite naming) stay queryable next to them.</pre>
<ul>
<li><a href="https://overwatch.confinia.io/#57175">Live demo — the control room (MapLibre globe + Grafana)</a></li>
<li><a href="https://overwatch.confinia.io/article.html">The write-up — architecture &amp; decisions</a></li>
<li><a href="/pro.html">Operators: run this on YOUR fleet's telemetry (private tenants)</a></li>
<li><a href="/api/v1/docs">Interactive documentation (OpenAPI)</a></li>
<li><a href="/api/v1/healthz">Service health</a></li>
</ul>
<footer>Version __VERSION__ · Free during development — no key required yet
(<code>POST /api/v1/keys {"email": …}</code> to get one for the beta;
<code>/api/v1/keys/{key}/usage</code> shows your own consumption).
Rate limits apply; positions are SGP4-propagated from cached elements,
telemetry is decoded locally — no request here ever hits an upstream API.
Attribution: telemetry &amp; receptions © <a href="https://db.satnogs.org">SatNOGS DB</a>
contributors (CC-BY-SA) · decoders: satnogs-decoders (LGPL) ·
elements: <a href="https://celestrak.org">CelesTrak</a>.</footer>
</main></body></html>"""


LANDING = LANDING.replace("__VERSION__", PRODUCT_VERSION)


@app.get("/v1", include_in_schema=False)
@app.get("/", include_in_schema=False)
def landing():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(LANDING)
