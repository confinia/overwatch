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

import metering
import billing
import oem as _oem
import polar

import re as _re

import psycopg2
import psycopg2.pool
from psycopg2.extras import execute_values
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
-- Every Keycloak login lands here (#172): the registration registry the ops
-- registrations board and signup alerts read. Backfilled once from the
-- Keycloak admin API, whose createdTimestamp carries the true signup date.
CREATE TABLE IF NOT EXISTS registered_user (
    sub        uuid PRIMARY KEY,
    email      text,
    name       text,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_login timestamptz,
    country    text                    -- ISO-2 from GeoIP at first login (#185)
);
ALTER TABLE registered_user ADD COLUMN IF NOT EXISTS country text;
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
    created_at timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz            -- soft-delete tombstone (removals metric)
);
ALTER TABLE organization ADD COLUMN IF NOT EXISTS archived_at timestamptz;
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
-- The open-network catalogue users pick from (#230). Refreshed from the
-- SatNOGS satellite DB by the ingest; it is a LOOKUP list, not the tracked
-- set — tracking a satellite copies it into `satellite`.
-- Per-object TLE lookup state (#303-adjacent). CelesTrak asks callers to
-- fetch bulk GROUP= files and cache them, not to poll gp.php?CATNR= per
-- object; an unbounded retry loop against that endpoint got our shared
-- egress address blocked by two data providers. Backoff must survive a
-- container restart, or a recycle silently resumes the polling.
CREATE TABLE IF NOT EXISTS element_fetch (
    norad        INTEGER PRIMARY KEY,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_attempt timestamptz,
    next_attempt timestamptz NOT NULL DEFAULT now(),
    gave_up      BOOLEAN NOT NULL DEFAULT false,
    last_error   TEXT
);
CREATE TABLE IF NOT EXISTS catalog (
    norad      INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    sat_id     TEXT,
    status     TEXT,                       -- alive | dead | re-entered | future
    is_violator BOOLEAN NOT NULL DEFAULT false,   -- SatNOGS: telemetry 1/day
    decoder    TEXT,                       -- satnogs-decoders module, when known
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS catalog_name_idx ON catalog (lower(name));
-- Per-station daily activity (#337): the baseline a degradation detector needs.
-- Derived entirely from `reception`, which we already ingest, so filling it
-- costs no provider request. Its value accrues with time, which is why it
-- exists before the detector that will read it.
CREATE TABLE IF NOT EXISTS station_daily (
    observer         TEXT NOT NULL,
    day              DATE NOT NULL,
    frames           INTEGER NOT NULL,
    satellites_heard INTEGER NOT NULL,
    first_ts         timestamptz,
    last_ts          timestamptz,
    PRIMARY KEY (observer, day)
);
CREATE INDEX IF NOT EXISTS station_daily_day_idx ON station_daily (day);
-- The denominator (#346): how many passes were geometrically AVAILABLE to a
-- station on a day. station_daily counts what was heard; without this, a
-- station under a quiet sky is indistinguishable from a broken one.
-- Derived from coordinates in `reception` and TLEs in `elements` — no
-- provider request.
CREATE TABLE IF NOT EXISTS station_opportunity (
    observer    TEXT NOT NULL,
    day         DATE NOT NULL,
    norad       INTEGER NOT NULL,
    passes      INTEGER NOT NULL,
    best_max_el DOUBLE PRECISION,
    PRIMARY KEY (observer, day, norad)
);
CREATE INDEX IF NOT EXISTS station_opportunity_day_idx ON station_opportunity (day);
-- SatNOGS throttles telemetry for satellites it flags as violating frequency
-- regulations to one request per day, against six a minute for everything
-- else. Mirrored from the bulk list so a satellite flagged upstream is
-- honoured from the next daily refresh, with no migration of our own rows.
ALTER TABLE catalog ADD COLUMN IF NOT EXISTS is_violator BOOLEAN NOT NULL DEFAULT false;
-- When we last ASKED for a satellite's telemetry (not when its newest frame
-- is from). In the database rather than in memory because a restart loop must
-- not be able to re-poll a once-a-day satellite on every boot.
ALTER TABLE satellite ADD COLUMN IF NOT EXISTS last_telemetry_fetch timestamptz;
-- One-time repair (#341). Before #312, six failed attempts set gave_up
-- regardless of WHY they failed, so a provider outage was recorded as a
-- permanent "not carried by CelesTrak or SatNOGS" — for every satellite,
-- the ISS included. Nothing clears that flag, so those environments never
-- fetch elements again: staging and sandbox sat frozen at the hour of the
-- 2026-08-20 block while production, repaired by hand during the incident,
-- looked fine.
--
-- attempts >= 6 identifies the legacy rows exactly: the current code sets
-- gave_up only through an answered "not carried", which fires on the FIRST
-- attempt. A row that reached six attempts and is flagged cannot have been
-- written by it, so this is idempotent — it can never match a current row.
--
-- The lesson worth keeping: preventing bad state from being written does not
-- repair state already written.
DELETE FROM element_fetch WHERE gave_up AND attempts >= 6;
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
-- Row-level security: per-org DB roles (provisioned on org creation) may
-- read ONLY their own org's rows — the isolation guarantee behind each
-- tenant's Grafana datasource, enforced by Postgres, not the app.
ALTER TABLE tenant_telemetry ENABLE ROW LEVEL SECURITY;
-- Usage metering mirror (POLAR.md): per-customer, per-billing-period counters
-- for private telemetry. Frames (ingest), TM/TC requests. Our source of truth
-- for reconciliation; also emitted to Polar as usage events.
CREATE TABLE IF NOT EXISTS org_usage (
    customer   text NOT NULL,
    period     text NOT NULL,                 -- YYYY-MM billing period (UTC)
    frames     bigint NOT NULL DEFAULT 0,
    tm_count   bigint NOT NULL DEFAULT 0,
    tc_count   bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (customer, period)
);
-- Billing entitlements on the organization (POLAR.md §7). Free by default;
-- flipped to Pro by the Polar webhook. Entitlement checks read entitled_until.
ALTER TABLE organization ADD COLUMN IF NOT EXISTS plan text NOT NULL DEFAULT 'free';
ALTER TABLE organization ADD COLUMN IF NOT EXISTS polar_customer_id text;
ALTER TABLE organization ADD COLUMN IF NOT EXISTS subscription_id text;
ALTER TABLE organization ADD COLUMN IF NOT EXISTS sub_status text;
ALTER TABLE organization ADD COLUMN IF NOT EXISTS freq_tier text NOT NULL DEFAULT 'standard';
ALTER TABLE organization ADD COLUMN IF NOT EXISTS entitled_until timestamptz;
-- Private per-org Grafana (#13): the Grafana organization we provisioned for
-- this org. Set once; also the orgId used when embedding its private boards.
ALTER TABLE organization ADD COLUMN IF NOT EXISTS grafana_org_id int;
-- Webhook idempotency + audit: process each delivery once.
CREATE TABLE IF NOT EXISTS billing_event (
    delivery_id text PRIMARY KEY,
    type        text,
    payload     jsonb,
    received_at timestamptz NOT NULL DEFAULT now(),
    processed   boolean NOT NULL DEFAULT false
);
-- Bring-your-own precise orbit (#208): a CCSDS OEM a user uploaded, stored as a
-- private ephemeris track. Owner-scoped by the signed-in subject; deliberately
-- SEPARATE from the public `position` fleet so an upload can never touch the
-- shared showcase.
CREATE TABLE IF NOT EXISTS ephemeris (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_sub   uuid NOT NULL,
    object_id   text,
    label       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ephemeris_owner_idx ON ephemeris (owner_sub, created_at DESC);
CREATE TABLE IF NOT EXISTS ephemeris_point (
    ephemeris_id uuid NOT NULL REFERENCES ephemeris(id) ON DELETE CASCADE,
    ts           timestamptz NOT NULL,
    lat          double precision NOT NULL,
    lon          double precision NOT NULL,
    alt_km       double precision NOT NULL,
    PRIMARY KEY (ephemeris_id, ts)
);
-- A registered user's favourite/focus satellites (#221): open-data satellites
-- they added from the fleet to personalise their view. Owner-scoped by subject.
CREATE TABLE IF NOT EXISTS user_satellite (
    sub        uuid NOT NULL,
    norad      integer NOT NULL REFERENCES satellite(norad),
    added_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sub, norad)
);
CREATE INDEX IF NOT EXISTS user_satellite_sub_idx ON user_satellite (sub, added_at DESC);
"""


STARTUP_LOCK = 168133   # arbitrary constant, shared by every api worker


def _startup_provision(conn) -> None:
    """Run the startup DDL (tables, grafana_ro, ops_ro) under an advisory
    lock: several uvicorn workers boot concurrently, and two sessions doing
    the same REVOKE/GRANT sequences deadlock each other (#168 — 9 restarts
    per deploy before this)."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (STARTUP_LOCK,))
    conn.commit()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(KEYS_SQL)
            _provision_grafana_role(cur)
            _provision_ops_role(cur)
    finally:
        conn.rollback()                       # clear any aborted transaction
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (STARTUP_LOCK,))
        conn.commit()


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
        _startup_provision(conn)
    finally:
        pool.putconn(conn)
    _provision_ops_org_async()                     # Grafana may still be booting
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


def field_source(name: str) -> str:
    """Classify a decoded telemetry field by origin, so the UI can separate real
    satellite health from link-layer framing (#46). Pattern-based, derived at
    read time — no schema change.

      canonical : the normalized health fields our ingest derives
      transport : AX.25 / CSP / framing metadata — not satellite health
      telemetry : everything else — the raw decoded satellite values
    """
    n = name.lower()
    if n in ("battery_v", "battery_i", "battery_pct"):
        return "canonical"
    if any(b in n for b in ("csp_header", "ax25", "packet_header",
                            "primary_header", "secondary_header",
                            "frame_header", "transfer_frame")):
        return "transport"
    if n in ("frame_length", "length", "crc", "crc16", "checksum", "fcs",
             "syncword", "sync_word", "callsign", "dest_callsign",
             "src_callsign", "source_callsign", "destination_callsign",
             "frame_id", "packet_id", "sequence_count", "seq_count",
             "spacecraft_id", "sat_id", "norad"):
        return "transport"
    return "telemetry"


@app.get("/v1/telemetry/{norad}/fields")
def telemetry_fields(norad: int,
                     hours: int = Query(168, ge=1, le=168,
                                        description="Window in hours (1–168); default 7 days")):
    """Decoded fields available for this satellite over the selected window
    (default 7 days): raw beacon fields plus the canonical battery_v /
    battery_i / battery_pct. Each field carries its latest value and a `source`
    category (canonical / telemetry / transport) so the UI can rank real health
    above link-layer framing (#46). The `hours` window matches the map's
    receptions so both describe the same frames (#70/#71)."""
    with cursor() as cur:
        if not known_norad(cur, norad):
            raise HTTPException(404, f"Unknown NORAD id {norad} (see /v1/satellites).")
        cur.execute("""
            SELECT field, count(*) AS points, max(ts) AS last_seen,
                   (array_agg(value_num ORDER BY ts DESC))[1] AS last_num,
                   (array_agg(value_txt ORDER BY ts DESC))[1] AS last_txt
            FROM telemetry
            WHERE norad = %s AND ts > now() - %s * interval '1 hour'
            GROUP BY field ORDER BY field""", (norad, hours))
        return [{"field": f, "points": n, "last_seen": ts.isoformat(),
                 "source": field_source(f),
                 "last_value": (num if num is not None else txt)}
                for f, n, ts, num, txt in cur.fetchall()]


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
# Server-to-server calls bypass the public edge (rootless containers cannot
# hairpin to the host's public IP): direct container-network URL. Browsers
# and the token `iss` claim keep the public URL.
KC_INTERNAL = os.environ.get("KC_INTERNAL",
    "http://ovw2_keycloak_1:8080/auth/realms/overwatch")
# Realm derived from the internal URL so each env talks to ITS realm
# (prod: overwatch, sandbox: overwatch-sandbox) — never hardcoded (#126).
KC_REALM = KC_INTERNAL.rsplit("/realms/", 1)[-1]
KC_CLIENT_ID = os.environ.get("OVERWATCH_CLIENT_ID", "overwatch")
KC_CLIENT_SECRET = os.environ.get("OVERWATCH_CLIENT_SECRET", "")
KC_ADMIN_USER = os.environ.get("KC_ADMIN_USERNAME", "")
KC_ADMIN_PASS = os.environ.get("KC_ADMIN_PASSWORD", "")
from urllib.parse import urlencode as _urlencode

COOKIE = "ovw_token"
# The id token, kept only to pass as `id_token_hint` on logout (#223) so
# Keycloak ends the SSO session without showing its own confirmation page —
# the app already asks for confirmation. Never used for authorization.
ID_COOKIE = "ovw_idt"
_jwks = None


def _jwks_client():
    global _jwks
    if _jwks is None:
        _jwks = PyJWKClient(f"{KC_INTERNAL}/protocol/openid-connect/certs")
    return _jwks


def _claims(request: Request):
    """The same OpenID token everywhere: Authorization bearer or cookie."""
    # Only a *Bearer* header carries our token. Anything else (notably the
    # `Basic` credentials the browser replays on every request once a caddy
    # basic-auth gate has challenged it, as on staging/sandbox) must fall
    # through to the cookie — otherwise it shadows the session and every
    # authenticated call 401s (#139).
    auth = request.headers.get("authorization") or ""
    tok = (auth[7:].strip() if auth[:7].lower() == "bearer "
           else request.cookies.get(COOKIE, ""))
    if not tok:
        return None
    try:
        key = _jwks_client().get_signing_key_from_jwt(tok)
        return _jwt.decode(tok, key.key, algorithms=["RS256"],
                           issuer=KC_ISSUER, options={"verify_aud": False})
    except Exception:
        return None


_UUID_RE = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                       r"[0-9a-f]{4}-[0-9a-f]{12}$", _re.I)
_org_id_cache: dict[str, str] = {}


def _resolve_org_id(alias: str) -> str | None:
    """Keycloak's `organization` claim ships only the alias in its multivalued
    shape (#140), but the tenant is keyed on the organization UUID. Resolve it
    once through the admin API and cache it for the process."""
    if alias in _org_id_cache:
        return _org_id_cache[alias]
    try:
        base = f"{KC_INTERNAL.rsplit('/realms/', 1)[0]}/admin/realms/{KC_REALM}"
        r = _rq.get(f"{base}/organizations?search={_rq.utils.quote(alias)}",
                    headers={"Authorization": f"Bearer {_kc_admin_token()}"},
                    timeout=10)
        for o in (r.json() if r.status_code == 200 else []):
            if alias in (o.get("alias"), o.get("name")) and o.get("id"):
                _org_id_cache[alias] = o["id"]
                return o["id"]
    except Exception:
        return None
    return None


def _org_of(claims) -> tuple[str, str] | None:
    """Extract (org_id, org_name) from the Keycloak organization claim.

    The claim has several shapes depending on the mapper: `{"alias": {"id": …}}`,
    `[{"id": …, "name": …}]`, or plainly `["alias"]`. Only the last lacks the
    UUID the tenant is keyed on — resolve it through the admin API instead of
    letting a non-UUID reach the database (#140).
    """
    o = claims.get("organization")
    if isinstance(o, dict) and o:
        name, meta = next(iter(o.items()))
        oid = (meta or {}).get("id") or name
    elif isinstance(o, list) and o:
        oid, name = ((o[0], o[0]) if isinstance(o[0], str)
                     else (o[0].get("id"), o[0].get("name", "org")))
    else:
        return None
    if not _UUID_RE.match(str(oid)):
        resolved = _resolve_org_id(str(oid))
        if not resolved:
            raise HTTPException(502, "Cannot resolve the Keycloak organization "
                                     f"'{oid}' to its id.")
        oid = resolved
    return (oid, name)


def _record_login(cur, sub, email=None, name=None, first_seen=None,
                  login=True, country=None) -> None:
    """Upsert the registration registry (#172). first_seen keeps the earliest
    known date (the Keycloak backfill knows better than now()); last_login
    only moves on a real login, not on a backfill sweep. country is captured
    from the login request's GeoIP (#185); the backfill leaves it null."""
    cur.execute("""INSERT INTO registered_user (sub, email, name, first_seen, last_login, country)
                   VALUES (%s::uuid, %s, %s, COALESCE(%s::timestamptz, now()),
                           CASE WHEN %s THEN now() END, %s)
                   ON CONFLICT (sub) DO UPDATE SET
                     last_login = CASE WHEN %s THEN now()
                                       ELSE registered_user.last_login END,
                     email = COALESCE(EXCLUDED.email, registered_user.email),
                     name  = COALESCE(EXCLUDED.name,  registered_user.name),
                     country = COALESCE(EXCLUDED.country, registered_user.country),
                     first_seen = LEAST(registered_user.first_seen,
                                        EXCLUDED.first_seen)""",
                (sub, email, name, first_seen, login, country, login))
    # cursor() never commits and putconn rolls back — every write here
    # commits explicitly (same convention as the _provision_* helpers)
    cur.connection.commit()


def _backfill_registered_users() -> bool:
    """One idempotent sweep of the Keycloak realm (#172): existing users
    predate the login registry. Returns False when Keycloak was unreachable
    (caller retries), True otherwise (done, or not configured)."""
    if not (KC_ADMIN_USER and KC_ADMIN_PASS):
        return True
    import datetime as _bdt
    try:
        tok = _kc_admin_token()
        base = f"{KC_INTERNAL.rsplit('/realms/', 1)[0]}/admin/realms/{KC_REALM}"
        r = _rq.get(f"{base}/users?max=500",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=15)
        r.raise_for_status()
        users = r.json()
    except _rq.RequestException:
        return False
    with cursor() as cur:
        for u in users:
            ts = u.get("createdTimestamp")
            first = (_bdt.datetime.fromtimestamp(ts / 1000, _bdt.timezone.utc)
                     if ts else None)
            name = " ".join(x for x in (u.get("firstName"), u.get("lastName"))
                            if x) or u.get("username")
            _record_login(cur, u["id"], u.get("email"), name,
                          first_seen=first, login=False)
    return True


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
        cur.execute("SELECT archived_at FROM organization WHERE id = %s::uuid", (org[0],))
        row = cur.fetchone()
        if row and row[0]:
            raise HTTPException(410, "This organization has been deleted.")
        # Order matters: organization and its tenant must exist before the
        # org_user row that references them (a user arriving from an upstream
        # Keycloak invitation has neither locally yet).
        cur.execute("""INSERT INTO organization (id, name) VALUES (%s::uuid, %s)
                       ON CONFLICT (id) DO NOTHING""", (org[0], org[1]))
        cur.execute("""INSERT INTO tenant (key, name, email)
                       VALUES (%s::uuid, %s, %s) ON CONFLICT (key) DO NOTHING""",
                    (org[0], org[1], c.get("email", "")))
        cur.execute("""INSERT INTO org_user (sub, org, email, name, last_seen)
                       VALUES (%s::uuid, %s::uuid, %s, %s, now())
                       ON CONFLICT (sub, org) DO UPDATE SET last_seen = now(),
                         email = EXCLUDED.email, name = EXCLUDED.name""",
                    (c["sub"], org[0], c.get("email"), c.get("name")))
        _provision_org_db(cur, org[0])
        # Private Grafana (#13): first authenticated call provisions it once
        # (guarded by organization.grafana_org_id, so this is a no-op after).
        _provision_grafana_org(cur, org[0], org[1], c.get("email", ""))
        cur.connection.commit()
    return c, org


import hmac as _hmac
ORG_DB_SECRET = os.environ.get("ORG_DB_SECRET", "")


def _org_role(org_id: str) -> tuple[str, str]:
    """Deterministic per-org DB role name + password (derived, never stored):
    the read-only Postgres identity a tenant's Grafana datasource uses."""
    role = "org_" + org_id.replace("-", "")[:24]
    pw = _hmac.new(ORG_DB_SECRET.encode(), org_id.encode(), "sha256").hexdigest()[:32]
    return role, pw


# The ONLY tables the public dashboards read. The Grafana datasource role is
# granted these and nothing else: Grafana's datasource proxy lets any caller
# (anonymous Viewer — required for the public embeds) run arbitrary SQL, so the
# database role IS the security boundary, not the dashboard JSON (#129).
GRAFANA_PUBLIC_TABLES = ("satellite", "position", "telemetry", "reception", "pass")
GRAFANA_ROLE = "grafana_ro"


def _provision_grafana_role(cur) -> None:
    """Idempotently create the least-privilege role the Grafana datasource uses.

    NOSUPERUSER + NOBYPASSRLS + SELECT on the public tables only, so arbitrary
    SQL sent through Grafana's datasource proxy can reach nothing but data that
    is already public. Never grant it tenant_telemetry, api_key, org_*,
    organization, billing_event or visitor_daily.
    """
    pw = os.environ.get("GRAFANA_DB_PASSWORD", "")
    if not pw:
        return                                    # not configured: leave as is
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (GRAFANA_ROLE,))
    if cur.fetchone():
        # Only LOGIN PASSWORD here: restating NOSUPERUSER (even to keep it off)
        # requires being superuser, and the app runs as orbit_app (#133). The
        # privilege attributes are fixed at CREATE and never change.
        cur.execute(f'ALTER ROLE "{GRAFANA_ROLE}" LOGIN PASSWORD %s', (pw,))
    else:
        cur.execute(f'CREATE ROLE "{GRAFANA_ROLE}" LOGIN NOSUPERUSER NOBYPASSRLS '
                    f'NOCREATEDB NOCREATEROLE PASSWORD %s', (pw,))
    # Reset every privilege, then grant back only the public tables — so a table
    # added to the allow-list is picked up and a removed one is actually revoked.
    cur.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{GRAFANA_ROLE}"')
    cur.execute(f'GRANT USAGE ON SCHEMA public TO "{GRAFANA_ROLE}"')
    for t in GRAFANA_PUBLIC_TABLES:
        cur.execute(f'GRANT SELECT ON {t} TO "{GRAFANA_ROLE}"')
    cur.connection.commit()


# The admin-only ops boards (#168). They query account/metering tables that
# grafana_ro must never read (#129), so they live in their OWN Grafana org with
# their own datasource role: datasources are scoped per Grafana org, and the
# anonymous public embeds are bound to org 1 — no proxy path reaches this one.
OPS_ROLE = "ops_ro"
# The ops datasource MUST NOT share a uid with the public one. Grafana uids are
# globally unique, so posting the ops datasource with uid "orbitcache" returned
# 409 and the refresh path below then PUT the OPS config (user ops_ro) onto the
# PUBLIC datasource — every public dashboard then queried as a role with no
# SELECT on satellite/telemetry/reception and showed "No data" (seen on sandbox;
# prod was one api restart away from the same outage).
OPS_DS_UID = "orbitcache-ops"
# Account/metering tables, plus the three the freshness alerts read (#303).
# Those three are PUBLIC open data — grafana_ro already serves them to
# anonymous viewers — so ops seeing them widens nothing. tenant_telemetry
# stays out, which is the boundary that actually matters here.
OPS_TABLES = ("organization", "org_user", "org_token", "api_key",
              "api_usage", "visitor_daily", "registered_user",
              "telemetry", "position", "elements")
OPS_ALERT_EMAIL = os.environ.get("OPS_ALERT_EMAIL", "contact@confinia.io")
# How long the OIDC CSRF nonce stays valid. Must outlive a registration with
# e-mail verification, not merely a login (#343).
AUTH_STATE_TTL = int(os.environ.get("AUTH_STATE_TTL", 3600))
OPS_GF_ORG = "Overwatch Ops"
OPS_DASHBOARDS_DIR = os.environ.get("OPS_DASHBOARDS_DIR", "/ops-dashboards")
# Alerting as code (#201): the contact point, root policy and each rule's
# SQL/summary are declared in this committed file, not built in Python. The
# container mounts it at /ops-alerts.json; the module-relative repo copy is the
# fallback so tests and a local run read the same source of truth.
OPS_ALERTS_FILE = os.environ.get("OPS_ALERTS_FILE", "/ops-alerts.json")
if not os.path.isfile(OPS_ALERTS_FILE):
    OPS_ALERTS_FILE = os.path.join(
        os.path.dirname(__file__), "..", "grafana", "ops-alerts.json")


def _ops_alert_spec() -> dict:
    """The declarative ops-alert definitions (contact point, policy, rules)."""
    import json as _j
    with open(OPS_ALERTS_FILE, encoding="utf-8") as fh:
        return _j.load(fh)


def _provision_ops_role(cur) -> None:
    """Idempotently create the read-only role the ops-org datasource uses:
    SELECT on OPS_TABLES and nothing else — in particular never
    tenant_telemetry (tenant payloads stay out of ops).

    The alert rules run through this role, so a table the alerts read has to
    be listed: a rule whose SQL is denied evaluates to no data, and with
    noDataState OK that is indistinguishable from healthy. An alert that
    cannot read its own table is worse than no alert, because it looks like
    one."""
    pw = os.environ.get("OPS_DB_PASSWORD", "")
    if not pw:
        return                                    # not configured: leave as is
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (OPS_ROLE,))
    if cur.fetchone():
        # LOGIN PASSWORD only: restating NOSUPERUSER requires superuser, and
        # the app runs as orbit_app (#133)
        cur.execute(f'ALTER ROLE "{OPS_ROLE}" LOGIN PASSWORD %s', (pw,))
    else:
        cur.execute(f'CREATE ROLE "{OPS_ROLE}" LOGIN NOSUPERUSER NOBYPASSRLS '
                    f'NOCREATEDB NOCREATEROLE PASSWORD %s', (pw,))
    cur.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{OPS_ROLE}"')
    cur.execute(f'GRANT USAGE ON SCHEMA public TO "{OPS_ROLE}"')
    for t in OPS_TABLES:
        cur.execute("SELECT to_regclass(%s)", (t,))
        if cur.fetchone()[0]:                     # grant only what exists yet
            cur.execute(f'GRANT SELECT ON {t} TO "{OPS_ROLE}"')
    cur.connection.commit()


GF_URL = os.environ.get("GF_URL", "http://grafana:3000")
GF_ADMIN_USER = os.environ.get("GF_SECURITY_ADMIN_USER", "admin")
GF_ADMIN_PASS = os.environ.get("GF_SECURITY_ADMIN_PASSWORD", "")


def _gf(method: str, path: str, body=None, gorg: int | None = None):
    """Grafana admin API call. `gorg` scopes the call to a Grafana org."""
    h = {"Content-Type": "application/json"}
    if gorg is not None:
        h["X-Grafana-Org-Id"] = str(gorg)
    return _rq.request(method, f"{GF_URL}/api{path}", json=body, headers=h,
                       auth=(GF_ADMIN_USER, GF_ADMIN_PASS), timeout=10)


def _migrate_ops_datasource_uid(gorg: int) -> None:
    """#261: self-heal installs that still hold the PUBLIC uid on the ops
    datasource.

    #253 stopped NEW ops datasources from taking uid "orbitcache", but rows
    created before that keep it — and Grafana uids are globally unique, so two
    rows sharing one makes file-provisioning fail at boot with "data source with
    the same uid already exists" and Grafana exits(1) in a crash loop. That hit
    the sandbox and was latent in staging and production. A hand-fix does not
    survive a restore from an older backup, so move it here, idempotently,
    before the ops datasource is (re)provisioned.
    """
    r = _gf("GET", "/datasources/uid/orbitcache", gorg=gorg)
    if r.status_code != 200:
        return                                    # nothing holds the old uid
    try:
        ds = r.json()
    except ValueError:
        return
    # Only ever move OUR ops datasource. The same uid also resolves to the
    # PUBLIC datasource in org 1 — renaming that one would break every public
    # dashboard, which is the very failure #253 was about.
    if "(ops)" not in (ds.get("name") or ""):
        return
    ds["uid"] = OPS_DS_UID
    resp = _gf("PUT", f"/datasources/{ds['id']}", ds, gorg=gorg)
    print(f"migrated ops datasource uid orbitcache -> {OPS_DS_UID} "
          f"(HTTP {resp.status_code})", flush=True)


def _provision_ops_org() -> bool:
    """Idempotently create/refresh the admin-only ops Grafana org (#168).

    Own org, own datasources, dashboards imported from OPS_DASHBOARDS_DIR.
    The datasources reuse org 1's uids (`orbitcache`, `promops`) — uids are
    unique per org, so the dashboard JSON works unchanged in both orgs — but
    here the Postgres one authenticates as OPS_ROLE, which can read the
    account/metering tables. Only server admins reach this org; anonymous
    viewers are bound to org 1, whose datasource stays on grafana_ro (#129).

    Returns False when Grafana was unreachable (caller retries), True
    otherwise (done, or not configured for this stack).
    """
    import json as _j
    pw = os.environ.get("OPS_DB_PASSWORD", "")
    if not (pw and GF_ADMIN_PASS and os.path.isdir(OPS_DASHBOARDS_DIR)):
        return True                               # not configured: no-op
    try:
        r = _gf("GET", f"/orgs/name/{_rq.utils.quote(OPS_GF_ORG)}")
        if r.status_code == 200:
            gorg = r.json()["id"]
        else:
            r = _gf("POST", "/orgs", {"name": OPS_GF_ORG})
            if r.status_code not in (200, 409):
                return False
            gorg = r.json().get("orgId")
        _migrate_ops_datasource_uid(gorg)      # #261: before we (re)create it
        for ds in (
            {"name": "OrbitCache (ops)", "uid": OPS_DS_UID, "type": "postgres",
             "access": "proxy", "url": os.environ.get("GF_DS_HOST", "db:5432"),
             "user": OPS_ROLE, "database": "orbit", "isDefault": True,
             "jsonData": {"sslmode": "disable", "postgresVersion": 1600},
             "secureJsonData": {"password": pw}},
            {"name": "OpsMetrics (ops)", "uid": "promops", "type": "prometheus",
             "access": "proxy", "url": "http://prometheus:9090"},
        ):
            r = _gf("POST", "/datasources", ds, gorg=gorg)
            if r.status_code == 409:              # exists: refresh (password…)
                cur_ds = _gf("GET", f"/datasources/uid/{ds['uid']}", gorg=gorg)
                if cur_ds.status_code == 200:
                    cur = cur_ds.json()
                    # Only ever refresh OUR datasource. A uid that resolves to a
                    # datasource owned by another org means a uid clash, and
                    # overwriting it would break that org's dashboards.
                    if cur.get("name") == ds["name"]:
                        _gf("PUT", f"/datasources/{cur['id']}", ds, gorg=gorg)
                    else:                     # uid clash: leave it alone
                        print(f"datasource uid {ds['uid']} already used by "
                              f"{cur.get('name')!r} — not overwriting", flush=True)
        for f in sorted(os.listdir(OPS_DASHBOARDS_DIR)):
            if not f.endswith(".json"):
                continue
            with open(os.path.join(OPS_DASHBOARDS_DIR, f), encoding="utf-8") as fh:
                d = _j.load(fh)
            d.pop("id", None)                     # ids are per-org; import by uid
            _gf("POST", "/dashboards/db", {"dashboard": d, "overwrite": True},
                gorg=gorg)
        _provision_ops_alerts(gorg)               # signup e-mails (#172)
        return True
    except _rq.RequestException:
        return False                              # Grafana down: retry


def _ops_alert_rules() -> list:
    """The signup alert rules (#172), shaped for Grafana's provisioning API.
    Each rule's SQL/summary comes from the committed ops-alerts.json (#201);
    here we wrap them in the mechanical Postgres-count -> reduce -> threshold(>0)
    pipeline. Every alert carries an `env` label (production/staging/sandbox,
    derived from PUBLIC_BASE) so the e-mail says which environment fired it
    (#187)."""
    host = PUBLIC_BASE.split("://")[-1].split("/")[0].split(".")[0]
    env = host if host in ("staging", "sandbox") else "production"

    def rule(uid, title, sql, summary=None, group="signups"):
        return {
            "uid": uid, "title": title, "condition": "C",
            "folderUID": "ops-alerts", "ruleGroup": group,
            "for": "0s", "noDataState": "OK", "execErrState": "OK",
            "labels": {"env": env},
            "annotations": {"summary": f"[{env}] " + (summary or title)},
            "data": [
                {"refId": "A", "relativeTimeRange": {"from": 900, "to": 0},
                 "datasourceUid": OPS_DS_UID,
                 "model": {"refId": "A", "format": "table", "rawSql": sql,
                           "intervalMs": 60000, "maxDataPoints": 100}},
                {"refId": "B", "relativeTimeRange": {"from": 0, "to": 0},
                 "datasourceUid": "__expr__",
                 "model": {"refId": "B", "type": "reduce", "reducer": "last",
                           "expression": "A"}},
                {"refId": "C", "relativeTimeRange": {"from": 0, "to": 0},
                 "datasourceUid": "__expr__",
                 "model": {"refId": "C", "type": "threshold", "expression": "B",
                           "conditions": [{"evaluator": {"params": [0],
                                                         "type": "gt"}}]}},
            ],
        }
    return [rule(r["uid"], r["title"], r["sql"], r.get("summary"),
                 r.get("group", "signups"))
            for r in _ops_alert_spec()["rules"]]


def _gf_ok(resp, what: str, also_ok: tuple = ()) -> bool:
    """Report a Grafana provisioning call that did not take.

    Every call site here was fire-and-forget, which is exactly how #328 stayed
    invisible: the api was resolving `grafana` to another environment's
    instance, so every write 401'd and the ops alert rules simply never
    existed. Provisioning that silently no-ops is worse than provisioning that
    crashes — the dashboard looks configured and is not. 409 is success: it
    means the object is already there.

    `also_ok` is for calls with their own idea of "already fine": Grafana 11
    answers 412 version-mismatch when a folder already exists, which is a
    no-op, not a failure. Per call rather than globally — 412 means something
    real elsewhere, and a checker that cries wolf on every boot is precisely
    what trains people to stop reading it (#332).
    """
    if resp.status_code in (200, 201, 202, 409) or resp.status_code in also_ok:
        return True
    print(f"GRAFANA PROVISIONING FAILED [{what}]: "
          f"{resp.status_code} {resp.text[:200]}", flush=True)
    return False


PLACEHOLDER_ADDRESSES = ("<example@email.com>", "example@email.com")


def _prune_alerts_outside_ops(gorg: int) -> None:
    """Our alert rules belong in the ops org and nowhere else.

    The signup rules had also accumulated in org 1 — Grafana's Main Org, the
    one that serves the public dashboards with anonymous viewer access — from
    calls made before the ops org existed, or without an org header, which
    defaults to the admin's current org. Both orgs routed to the same mailbox,
    so every signup sent two identical e-mails.

    Deleting them by hand would fix today and drift again, so provisioning is
    authoritative instead of additive: the uids come from ops-alerts.json, so
    what we clean up cannot diverge from what we declare. Rules belonging to
    anyone else are left strictly alone.
    """
    ours = {r["uid"] for r in _ops_alert_spec()["rules"]}
    orgs = _gf("GET", "/orgs")
    if orgs.status_code != 200:
        _gf_ok(orgs, "list-orgs")
        return
    for org in orgs.json():
        oid = org.get("id")
        if oid == gorg:
            continue
        existing = _gf("GET", "/v1/provisioning/alert-rules", gorg=oid)
        if existing.status_code != 200:
            continue                      # not ours to read: leave it be
        for rule in existing.json():
            if rule.get("uid") in ours:
                print(f"removing stray alert rule {rule['uid']} from Grafana "
                      f"org {oid} ({org.get('name')!r})", flush=True)
                _gf_ok(_gf("DELETE",
                           f"/v1/provisioning/alert-rules/{rule['uid']}",
                           gorg=oid), f"delete-stray {rule['uid']}")


def _drop_placeholder_contact_point(gorg: int) -> None:
    """Remove Grafana's built-in contact point while it still points at the
    placeholder address.

    Nothing routes to it — the root policy names ops-email — but a live e-mail
    contact point aimed at <example@email.com> is one routing mistake away from
    sending our ops mail to a stranger. Only ever removed while it carries the
    placeholder: once someone has put a real address there it is theirs.
    """
    cps = _gf("GET", "/v1/provisioning/contact-points", gorg=gorg)
    if cps.status_code != 200:
        return
    for c in cps.json():
        addr = (c.get("settings") or {}).get("addresses", "")
        # Grafana's built-in default has NO uid: it lives in the default
        # alertmanager configuration rather than as a provisioned object, so
        # this endpoint has nothing to address and 404s every time. Removing
        # it would mean rewriting the whole alertmanager config — out of
        # proportion for a contact point nothing routes to. Skipped, with this
        # comment so it is not attempted again (#332).
        if not c.get("uid"):
            continue
        if c.get("name") == "email receiver" and addr in PLACEHOLDER_ADDRESSES:
            _gf_ok(_gf("DELETE",
                       f"/v1/provisioning/contact-points/{c.get('uid')}",
                       gorg=gorg), "drop-placeholder-contact-point")


def _provision_ops_alerts(gorg: int) -> None:
    """Contact point, root policy and the signup alert rules in the ops org
    (#172) — API-provisioned like the org itself. E-mails to OPS_ALERT_EMAIL
    start flowing the moment GF_SMTP_* is filled in .env; the rules provision
    and evaluate regardless."""
    spec = _ops_alert_spec()
    cps = _gf("GET", "/v1/provisioning/contact-points", gorg=gorg)
    existing = ({c["name"]: c["uid"] for c in cps.json()}
                if cps.status_code == 200 else {})
    # The addresses are the one runtime secret (OPS_ALERT_EMAIL, .env); the
    # rest of the contact point is declared in ops-alerts.json.
    cp = {**spec["contactPoint"],
          "settings": {"addresses": OPS_ALERT_EMAIL}}
    if cp["name"] in existing:
        _gf_ok(_gf("PUT",
                   f"/v1/provisioning/contact-points/{existing[cp['name']]}",
                   cp, gorg=gorg), "contact-point")
    else:
        _gf_ok(_gf("POST", "/v1/provisioning/contact-points", cp, gorg=gorg),
               "contact-point")
    _gf_ok(_gf("PUT", "/v1/provisioning/policies", spec["policy"], gorg=gorg),
           "notification-policy")
    # 409 and 412 both mean the folder is already there: Grafana 11 answers
    # 412 "the folder has been changed by someone else" to a repeat create.
    _gf_ok(_gf("POST", "/folders", {"title": "alerts", "uid": "ops-alerts"},
               gorg=gorg), "alerts-folder", also_ok=(412,))
    for r in _ops_alert_rules():
        resp = _gf("POST", "/v1/provisioning/alert-rules", r, gorg=gorg)
        if resp.status_code not in (200, 201):    # exists (or shape drift)
            resp = _gf("PUT", f"/v1/provisioning/alert-rules/{r['uid']}", r,
                       gorg=gorg)
        _gf_ok(resp, f"alert-rule {r['uid']}")
    # Authoritative, not additive: what we declare is what exists, and only
    # where it belongs (#330).
    _prune_alerts_outside_ops(gorg)
    _drop_placeholder_contact_point(gorg)


def _provision_ops_org_async() -> None:
    """Startup helper: Grafana usually comes up after the API, so retry in a
    daemon thread instead of blocking or losing the provisioning until the
    next deploy. Also runs the one-shot Keycloak registration backfill."""
    import threading

    def _loop():
        ops_done = reg_done = False
        for _ in range(60):
            ops_done = ops_done or _provision_ops_org()
            reg_done = reg_done or _backfill_registered_users()
            if ops_done and reg_done:
                return
            time.sleep(5)

    threading.Thread(target=_loop, daemon=True).start()


def _ensure_grafana_member(gorg: int, email: str) -> None:
    """Add the user to THEIR Grafana org as Editor — idempotently. A user only
    exists in Grafana after their first OIDC login, so the add attempted at org
    creation 404s; running it on every /v1/org/grafana call makes the membership
    land once they've signed in (409 = already a member) (#13)."""
    if not (email and GF_ADMIN_PASS):
        return
    try:
        _gf("POST", f"/orgs/{gorg}/users", {"loginOrEmail": email, "role": "Editor"})
    except _rq.RequestException:
        pass                                          # Grafana down: next call retries


def _provision_grafana_org(cur, org_id: str, org_name: str, email: str = "") -> int | None:
    """Idempotently give an organization its OWN Grafana: a Grafana org, a
    datasource authenticating as the org's **RLS-scoped Postgres role**, and the
    private dashboard seeded from tenant_dashboard.json (#13).

    The isolation guarantee is the database role, not the dashboard: the seeded
    queries carry NO tenant filter — Postgres row-level security restricts the
    role to this org's rows, so a client editing their own panels can never
    reach another tenant's data.

    Returns the Grafana org id, or None when not configured (self-host/tests).
    """
    if not (ORG_DB_SECRET and GF_ADMIN_PASS):
        return None
    cur.execute("SELECT grafana_org_id FROM organization WHERE id=%s::uuid", (org_id,))
    row = cur.fetchone()
    if row and row[0]:
        _ensure_grafana_member(row[0], email)          # sync membership post-login
        return row[0]                                  # already provisioned
    role, pw = _org_role(org_id)
    gf_name = f"{org_name} ({org_id[:8]})"             # unique, human-readable
    try:
        r = _gf("GET", f"/orgs/name/{_rq.utils.quote(gf_name)}")
        if r.status_code == 200:
            gorg = r.json()["id"]
        else:
            r = _gf("POST", "/orgs", {"name": gf_name})
            if r.status_code not in (200, 409):
                return None
            gorg = r.json().get("orgId")
        ds_uid = "org-" + org_id.replace("-", "")[:12]
        _gf("POST", "/datasources", {
            "name": "Private telemetry", "uid": ds_uid, "type": "postgres",
            "access": "proxy", "url": os.environ.get("GF_DS_HOST", "db:5432"),
            "user": role, "database": "orbit", "isDefault": True,
            "jsonData": {"sslmode": "disable", "postgresVersion": 1600},
            "secureJsonData": {"password": pw}}, gorg=gorg)
        tpl = open(os.path.join(os.path.dirname(__file__),
                                "tenant_dashboard.json"), encoding="utf-8").read()
        _gf("POST", "/dashboards/db",
            {"dashboard": _json.loads(tpl.replace("__DS_UID__", ds_uid)),
             "overwrite": True}, gorg=gorg)
        _ensure_grafana_member(gorg, email)            # Editor in THEIR org only
    except _rq.RequestException:
        return None                                    # Grafana down: retry later
    cur.execute("UPDATE organization SET grafana_org_id=%s WHERE id=%s::uuid",
                (gorg, org_id))
    return gorg


def _provision_org_db(cur, org_id: str) -> None:
    """Idempotently create the org's RLS-scoped read role + policy. The role
    (non-superuser) is subject to RLS and can only SELECT this org's rows.
    Requires ORG_DB_SECRET; no-op without it (self-host / tests may skip)."""
    if not ORG_DB_SECRET:
        return
    role, pw = _org_role(org_id)
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    if not cur.fetchone():
        cur.execute(f'CREATE ROLE "{role}" LOGIN PASSWORD %s', (pw,))
    cur.execute(f'GRANT SELECT ON tenant_telemetry TO "{role}"')
    cur.execute("SELECT 1 FROM pg_policies WHERE tablename = 'tenant_telemetry' AND policyname = %s",
                (role + "_pol",))
    if not cur.fetchone():
        cur.execute(f'CREATE POLICY "{role}_pol" ON tenant_telemetry '
                    f'FOR SELECT TO "{role}" USING (tenant = %s::uuid)', (org_id,))


def _kc_admin_token() -> str:
    r = _rq.post(f"{KC_INTERNAL.rsplit('/realms/',1)[0]}/realms/master/protocol/openid-connect/token",
                 data={"grant_type": "password", "client_id": "admin-cli",
                       "username": KC_ADMIN_USER, "password": KC_ADMIN_PASS},
                 timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


@app.get("/v1/auth/login", include_in_schema=False)
def auth_login(request: Request):
    from fastapi.responses import RedirectResponse
    state = _secrets.token_urlsafe(16)
    url = (f"{KC_ISSUER}/protocol/openid-connect/auth?client_id={KC_CLIENT_ID}"
           f"&response_type=code&scope=openid+profile+email+organization"
           f"&redirect_uri={_base_of(request)}/api/v1/auth/callback"
           f"&state={state}")
    resp = RedirectResponse(url)
    # Ten minutes covered a LOGIN but not a REGISTRATION: the realm verifies
    # e-mail, so a new user leaves for their inbox and comes back — routinely
    # well past ten minutes, and our first real signup took over two hours.
    # The cookie is a CSRF nonce, not a session; outliving the round trip
    # costs nothing and is the difference between signing up and giving up.
    resp.set_cookie("ovw_state", state, max_age=AUTH_STATE_TTL, httponly=True,
                    secure=True, samesite="lax")
    return resp


@app.get("/v1/auth/callback", include_in_schema=False)
def auth_callback(request: Request, code: str = "", state: str = ""):
    from fastapi.responses import RedirectResponse
    if not code or state != request.cookies.get("ovw_state"):
        # Don't hand a first-time visitor a raw JSON error naming an API path.
        # The state is a CSRF nonce: a mismatch means we cannot trust THIS
        # callback, so throw it away and start a fresh login. That accepts
        # nothing unverified and is invisible to the user, who simply arrives
        # signed in (#343).
        #
        # One shot only: if the retry ALSO arrives without the cookie, the
        # browser is refusing cookies and redirecting again would loop, so we
        # say so in words instead. The marker is a cookie rather than a query
        # parameter because redirect_uri must match the value registered in
        # Keycloak exactly, and decorating it risks invalid_redirect_uri.
        if not request.cookies.get("ovw_authretry"):
            r = RedirectResponse(f"{_base_of(request)}/api/v1/auth/login")
            r.set_cookie("ovw_authretry", "1", max_age=120, httponly=True,
                         secure=True, samesite="lax")
            return r
        raise HTTPException(400, "Your browser did not keep the sign-in "
                                 "cookie, so the login could not be "
                                 "completed. Enable cookies for this site and "
                                 "try again.")
    r = _rq.post(f"{KC_INTERNAL}/protocol/openid-connect/token",
                 data={"grant_type": "authorization_code", "code": code,
                       "client_id": KC_CLIENT_ID, "client_secret": KC_CLIENT_SECRET,
                       "redirect_uri": f"{_base_of(request)}/api/v1/auth/callback"},
                 timeout=15)
    if r.status_code != 200:
        raise HTTPException(502, "Token exchange failed")
    _tokens = r.json()
    tok = _tokens["access_token"]
    id_tok = _tokens.get("id_token", "")     # for RP-initiated logout (#223)
    try:                                    # registration registry (#172) —
        key = _jwks_client().get_signing_key_from_jwt(tok)   # best-effort,
        c = _jwt.decode(tok, key.key, algorithms=["RS256"],  # never blocks
                        issuer=KC_ISSUER, options={"verify_aud": False})
        with cursor() as cur:
            _record_login(cur, c["sub"], c.get("email"), c.get("name"),
                          country=client_country(request))
    except Exception:
        pass
    resp = RedirectResponse("/")
    resp.set_cookie(COOKIE, tok, max_age=1740,
                    httponly=True, secure=True, samesite="lax", path="/")
    if id_tok:
        resp.set_cookie(ID_COOKIE, id_tok, max_age=1740,
                        httponly=True, secure=True, samesite="lax", path="/")
    resp.delete_cookie("ovw_state")
    resp.delete_cookie("ovw_authretry")   # a later failure gets its own retry
    return resp


@app.get("/v1/auth/logout", include_in_schema=False)
def auth_logout(request: Request):
    """RP-initiated OIDC logout (#223). Dropping our own cookie is not enough:
    the Keycloak SSO session would survive and the next sign-in would silently
    re-authenticate — so it never felt like logging out. Bounce through the
    realm's end-session endpoint to actually end the IdP session, then land back
    on the app. `id_token_hint` (kept from the callback) lets Keycloak skip its
    own confirmation page — the app already confirms before calling this."""
    from fastapi.responses import RedirectResponse
    base = _base_of(request)
    id_tok = request.cookies.get(ID_COOKIE, "")
    args = {"post_logout_redirect_uri": f"{base}/", "client_id": KC_CLIENT_ID}
    if id_tok:
        args["id_token_hint"] = id_tok
    url = (f"{KC_ISSUER}/protocol/openid-connect/logout"
           f"?{_urlencode(args)}")
    resp = RedirectResponse(url)
    resp.delete_cookie(COOKIE, path="/")
    resp.delete_cookie(ID_COOKIE, path="/")
    return resp


@app.get("/v1/me")
def me(request: Request):
    """Who am I — identity, organization, and favourite satellites (#221)."""
    c = _require_user(request)
    org = _org_of(c)
    with cursor() as cur:
        favs = _list_favorites(cur, c["sub"])
    return {"sub": c["sub"], "email": c.get("email"), "name": c.get("name"),
            "organization": {"id": org[0], "name": org[1]} if org else None,
            "satellites": [{"norad": r[0], "name": r[1]} for r in favs]}


# --------------------------------------------------------------------------
# Favourite / focus satellites (#221): a registered user adds open-data
# satellites from the fleet to personalise their view. Owner-scoped by subject;
# no private data — just which public satellites they foreground. Store/read are
# factored out so isolation is testable without a token.
# --------------------------------------------------------------------------
def _add_favorite(cur, sub: str, norad: int) -> None:
    cur.execute("INSERT INTO user_satellite (sub, norad) VALUES (%s::uuid, %s) "
                "ON CONFLICT (sub, norad) DO NOTHING", (sub, norad))


def _list_favorites(cur, sub: str):
    cur.execute("SELECT us.norad, s.name FROM user_satellite us "
                "JOIN satellite s USING (norad) WHERE us.sub = %s::uuid "
                "ORDER BY us.added_at DESC", (sub,))
    return cur.fetchall()


def _remove_favorite(cur, sub: str, norad: int) -> int:
    cur.execute("DELETE FROM user_satellite WHERE sub = %s::uuid AND norad = %s",
                (sub, norad))
    return cur.rowcount


class SatAdd(BaseModel):
    norad: int


# --- open-network catalogue: pick any satellite, not just the fleet (#230) ---
# Telemetry decoding needs a per-satellite decoder, which is why the fleet is
# curated. Position tracking does not: CelesTrak has a TLE for anything
# catalogued and SGP4 propagates it. So "add any satellite" means position
# tracking, with telemetry only where a decoder already exists.
TRACK_CAP = int(os.environ.get("TRACK_CAP", "250"))


# --- Station degradation (#348) --------------------------------------------
# A station is degraded when its OWN hit rate collapses while the fleet's does
# not. Both halves are load-bearing.
#
# Against itself, because an absolute rate says nothing: on 2026-08-25 Neumayer
# ran 110/178 = 62% and SPUTNIX-Murmansk 0/163 = 0%, and the second is not a
# fault — a station that never tracked our 23 satellites looks exactly like a
# dead one. Only a station that used to hear us and stopped has degraded.
#
# Against the fleet, because when everything drops at once the fault is ours.
# Our own 2026-08-20 outage took telemetry down for four days, so every
# station's rate went to zero; without this clause the detector would have
# accused all 360 of breaking simultaneously.
HEALTH_RECENT_DAYS   = int(os.environ.get("HEALTH_RECENT_DAYS", 3))
HEALTH_BASELINE_DAYS = int(os.environ.get("HEALTH_BASELINE_DAYS", 21))
HEALTH_MIN_DAYS      = int(os.environ.get("HEALTH_MIN_DAYS", 7))
HEALTH_MIN_BASELINE  = float(os.environ.get("HEALTH_MIN_BASELINE", 0.05))
HEALTH_COLLAPSE      = float(os.environ.get("HEALTH_COLLAPSE", 0.25))
HEALTH_FLEET_OK      = float(os.environ.get("HEALTH_FLEET_OK", 0.5))

STATION_HEALTH_SQL = """
WITH rate AS (
    SELECT o.observer, o.day,
           sum(o.passes)                       AS passes,
           coalesce(max(d.frames), 0)          AS frames,
           coalesce(max(d.frames), 0)::float
             / nullif(sum(o.passes), 0)        AS hit_rate
    FROM station_opportunity o
    LEFT JOIN station_daily d
           ON d.observer = o.observer AND d.day = o.day
    WHERE o.day > current_date - %(window)s::integer
    GROUP BY o.observer, o.day
), fleet AS (
    -- the whole network's rate that day: the denominator for "was it us?"
    SELECT day, avg(hit_rate) AS fleet_rate
    FROM rate WHERE hit_rate IS NOT NULL GROUP BY day
), split AS (
    SELECT r.observer,
           avg(r.hit_rate) FILTER (
             WHERE r.day > current_date - %(recent)s::integer)      AS recent_rate,
           avg(r.hit_rate) FILTER (
             WHERE r.day <= current_date - %(recent)s::integer)     AS base_rate,
           count(*)        FILTER (
             WHERE r.day <= current_date - %(recent)s::integer)     AS base_days,
           avg(f.fleet_rate) FILTER (
             WHERE r.day > current_date - %(recent)s::integer)      AS fleet_recent,
           avg(f.fleet_rate) FILTER (
             WHERE r.day <= current_date - %(recent)s::integer)     AS fleet_base,
           sum(r.passes)   FILTER (
             WHERE r.day > current_date - %(recent)s::integer)      AS recent_passes
    FROM rate r JOIN fleet f ON f.day = r.day
    GROUP BY r.observer
)
SELECT observer,
       round(base_rate::numeric, 4)    AS baseline_rate,
       round(recent_rate::numeric, 4)  AS recent_rate,
       base_days, recent_passes,
       round(fleet_base::numeric, 4)   AS fleet_baseline,
       round(fleet_recent::numeric, 4) AS fleet_recent
FROM split
WHERE base_days >= %(min_days)s
  AND base_rate >= %(min_base)s
  AND recent_passes > 0
  AND recent_rate <= base_rate * %(collapse)s
  -- and only when the FLEET did not fall with it: otherwise this is ours
  AND fleet_recent >= fleet_base * %(fleet_ok)s
ORDER BY base_rate - recent_rate DESC
"""


@app.get("/v1/stations/health")
def station_health():
    """Ground stations whose reception has collapsed against their own history.

    Open data: this is what a station operator wants to know and cannot easily
    find out — whether a silent antenna is theirs or the sky's.
    """
    with cursor() as cur:
        cur.execute(STATION_HEALTH_SQL, {
            "window": HEALTH_RECENT_DAYS + HEALTH_BASELINE_DAYS,
            "recent": HEALTH_RECENT_DAYS,
            "min_days": HEALTH_MIN_DAYS,
            "min_base": HEALTH_MIN_BASELINE,
            "collapse": HEALTH_COLLAPSE,
            "fleet_ok": HEALTH_FLEET_OK,
        })
        cols = [c.name for c in cur.description]
        degraded = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"degraded": degraded,
            "window": {"recent_days": HEALTH_RECENT_DAYS,
                       "baseline_days": HEALTH_BASELINE_DAYS},
            "note": ("A station is listed only when its own hit rate collapsed "
                     "while the fleet's held. If everything drops together the "
                     "fault is ours, not the station's.")}


@app.get("/v1/catalog/search")
def catalog_search(q: str = Query("", max_length=60),
                   limit: int = Query(20, ge=1, le=50)):
    """Text search over the open network. Anonymous-friendly: this is a
    read-only lookup, and the result says what is already tracked."""
    term = q.strip()
    with cursor() as cur:
        if term.isdigit():
            cur.execute(
                """SELECT c.norad, c.name, c.status, c.decoder,
                          (s.norad IS NOT NULL) AS tracked
                   FROM catalog c LEFT JOIN satellite s ON s.norad = c.norad
                   WHERE c.norad = %s""", (int(term),))
        else:
            cur.execute(
                """SELECT c.norad, c.name, c.status, c.decoder,
                          (s.norad IS NOT NULL) AS tracked
                   FROM catalog c LEFT JOIN satellite s ON s.norad = c.norad
                   WHERE (%s = '' OR c.name ILIKE %s)
                   ORDER BY (s.norad IS NOT NULL) DESC,
                            (c.status = 'alive') DESC, c.name
                   LIMIT %s""", (term, f"%{term}%", limit))
        return [{"norad": n, "name": nm, "status": st,
                 "telemetry": bool(dec), "tracked": tr}
                for n, nm, st, dec, tr in cur.fetchall()]


class TrackRequest(BaseModel):
    norad: int


@app.post("/v1/catalog/track", status_code=201)
def catalog_track(request: Request, body: TrackRequest):
    """Add a catalogued satellite to the tracked set, so its position is
    computed and it appears on the globe. Idempotent. The tracked set is
    SHARED — one person adding a satellite makes it available to everyone,
    which is why it is capped and validated against the catalogue."""
    with cursor() as cur:
        cur.execute("SELECT name, sat_id, decoder, status FROM catalog WHERE norad = %s",
                    (body.norad,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"NORAD {body.norad} is not in the open-network "
                                     f"catalogue — nothing to track.")
        name, sat_id, decoder, status = row
        cur.execute("SELECT 1 FROM satellite WHERE norad = %s", (body.norad,))
        if cur.fetchone():
            return {"norad": body.norad, "name": name, "tracked": True,
                    "already": True, "telemetry": bool(decoder)}
        cur.execute("SELECT count(*) FROM satellite")
        if cur.fetchone()[0] >= TRACK_CAP:
            raise HTTPException(429, f"The tracked set is full ({TRACK_CAP} satellites). "
                                     "Ask us to raise it: contact@confinia.io")
        cur.execute(
            """INSERT INTO satellite (norad, name, sat_id, has_telemetry, decoder, note)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (norad) DO NOTHING""",
            (body.norad, name, sat_id, bool(decoder), decoder, "added from the open network"))
        cur.connection.commit()
    # Elements are fetched by the ingest, not here: a request handler must not
    # block on a third party (CelesTrak was unreachable from this VM the first
    # time this ran, for 15s per call), and the ingest already has the
    # CelesTrak -> SatNOGS fallback and the token. Its fill loop runs every
    # couple of minutes, so the satellite appears shortly after being added.
    return {"norad": body.norad, "name": name, "tracked": True, "already": False,
            "telemetry": bool(decoder), "status": status,
            "elements": False,
            "note": ("position tracking only — no decoder for this satellite"
                     if not decoder else "telemetry decoding available")}


@app.get("/v1/me/satellites")
def list_my_satellites(request: Request):
    """The signed-in user's favourite satellites (newest first)."""
    c = _require_user(request)
    with cursor() as cur:
        rows = _list_favorites(cur, c["sub"])
    return [{"norad": r[0], "name": r[1]} for r in rows]


@app.post("/v1/me/satellites", status_code=201)
def add_my_satellite(request: Request, body: SatAdd):
    """Add an open-data fleet satellite to the user's favourites."""
    c = _require_user(request)
    with cursor() as cur:
        if not known_norad(cur, body.norad):
            raise HTTPException(404, f"Unknown satellite {body.norad}.")
        _add_favorite(cur, c["sub"], body.norad)
        cur.connection.commit()
    return {"norad": body.norad, "added": True}


@app.delete("/v1/me/satellites/{norad}", status_code=204)
def remove_my_satellite(request: Request, norad: int):
    """Remove a satellite from the user's favourites (owner-scoped)."""
    c = _require_user(request)
    with cursor() as cur:
        _remove_favorite(cur, c["sub"], norad)
        cur.connection.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Bring-your-own precise orbit (#208): upload a CCSDS OEM -> a private ephemeris
# the owner can view on the globe. Owner-scoped by the signed-in subject; never
# writes the public `position` fleet. Store/read are factored out of the routes
# so they can be tested (incl. isolation) without minting a token.
# --------------------------------------------------------------------------
_OEM_MAX_BYTES = 4_000_000
_OEM_MAX_POINTS = 100_000


def _store_oem(cur, owner_sub: str, oem_text: str, label: str | None = None) -> dict:
    """Parse an OEM (raises _oem.OemError) and persist it as this owner's
    ephemeris. Caller commits."""
    object_id, recs = _oem.positions_from_oem(oem_text)
    if not recs:
        raise _oem.OemError("OEM has no state vectors")
    if len(recs) > _OEM_MAX_POINTS:
        raise _oem.OemError(f"OEM has too many points (>{_OEM_MAX_POINTS})")
    lbl = (label or object_id or "ephemeris").strip()[:80]
    cur.execute("INSERT INTO ephemeris (owner_sub, object_id, label) "
                "VALUES (%s::uuid, %s, %s) RETURNING id", (owner_sub, object_id, lbl))
    eph_id = cur.fetchone()[0]
    execute_values(cur, "INSERT INTO ephemeris_point "
                        "(ephemeris_id, ts, lat, lon, alt_km) VALUES %s",
                   [(eph_id, dt, la, lo, al) for (dt, la, lo, al) in recs])
    return {"id": eph_id, "object_id": object_id, "label": lbl,
            "points": len(recs), "start": recs[0][0], "stop": recs[-1][0]}


def _read_oem_track(cur, owner_sub: str, eph_id: str):
    """The owner's ephemeris track, or None. The WHERE on owner_sub IS the
    isolation — a user can only read their own."""
    cur.execute("SELECT object_id, label FROM ephemeris "
                "WHERE id = %s::uuid AND owner_sub = %s::uuid", (eph_id, owner_sub))
    head = cur.fetchone()
    if not head:
        return None
    cur.execute("SELECT ts, lat, lon, alt_km FROM ephemeris_point "
                "WHERE ephemeris_id = %s::uuid ORDER BY ts", (eph_id,))
    return {"object_id": head[0], "label": head[1], "points": cur.fetchall()}


class OemUpload(BaseModel):
    oem: str
    label: str | None = None


@app.post("/v1/ephemeris", status_code=201)
def upload_ephemeris(request: Request, body: OemUpload):
    """Import a CCSDS OEM (KVN, Earth-fixed frames — #208 Phase 1) as a private
    ephemeris owned by the signed-in user. Returns its id + a summary."""
    c = _require_user(request)
    if len(body.oem.encode("utf-8", "ignore")) > _OEM_MAX_BYTES:
        raise HTTPException(413, "OEM too large.")
    try:
        with cursor() as cur:
            r = _store_oem(cur, c["sub"], body.oem, body.label)
            cur.connection.commit()
    except _oem.OemError as e:
        raise HTTPException(422, f"Invalid OEM: {e}")
    return {"id": str(r["id"]), "object_id": r["object_id"], "label": r["label"],
            "points": r["points"], "start": r["start"].isoformat(),
            "stop": r["stop"].isoformat()}


@app.get("/v1/ephemeris")
def list_ephemeris(request: Request):
    """The signed-in user's uploaded ephemerides (newest first)."""
    c = _require_user(request)
    with cursor() as cur:
        cur.execute(
            "SELECT e.id, e.object_id, e.label, e.created_at, count(p.*) AS points, "
            "min(p.ts) AS t0, max(p.ts) AS t1 "
            "FROM ephemeris e LEFT JOIN ephemeris_point p ON p.ephemeris_id = e.id "
            "WHERE e.owner_sub = %s::uuid GROUP BY e.id ORDER BY e.created_at DESC",
            (c["sub"],))
        rows = cur.fetchall()
    return [{"id": str(r[0]), "object_id": r[1], "label": r[2],
             "created_at": r[3].isoformat(), "points": r[4],
             "start": r[5].isoformat() if r[5] else None,
             "stop": r[6].isoformat() if r[6] else None} for r in rows]


@app.get("/v1/ephemeris/{eph_id}")
def get_ephemeris(request: Request, eph_id: str):
    """The track points, for the globe to draw. Owner-scoped (404 for anyone
    else's id, so ownership isn't even disclosed)."""
    c = _require_user(request)
    if not _UUID_RE.match(eph_id):
        raise HTTPException(404, "No such ephemeris.")
    with cursor() as cur:
        track = _read_oem_track(cur, c["sub"], eph_id)
    if track is None:
        raise HTTPException(404, "No such ephemeris.")
    return {"id": eph_id, "object_id": track["object_id"], "label": track["label"],
            "points": [{"ts": t.isoformat(), "lat": la, "lon": lo, "alt_km": al}
                       for (t, la, lo, al) in track["points"]]}


@app.delete("/v1/ephemeris/{eph_id}", status_code=204)
def delete_ephemeris(request: Request, eph_id: str):
    """Remove the owner's ephemeris (points cascade). 404 for anyone else's."""
    c = _require_user(request)
    if not _UUID_RE.match(eph_id):
        raise HTTPException(404, "No such ephemeris.")
    with cursor() as cur:
        cur.execute("DELETE FROM ephemeris WHERE id = %s::uuid AND owner_sub = %s::uuid",
                    (eph_id, c["sub"]))
        deleted = cur.rowcount
        cur.connection.commit()
    if not deleted:
        raise HTTPException(404, "No such ephemeris.")
    return Response(status_code=204)


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
    base = f"{KC_INTERNAL.rsplit('/realms/',1)[0]}/admin/realms/{KC_REALM}"
    h = {"Authorization": f"Bearer {at}"}
    r = _rq.post(f"{base}/organizations", json={
        "name": name, "alias": alias,
        "domains": [{"name": f"{alias}.invalid", "verified": False}]}, headers=h, timeout=15)
    # Keycloak answers 400 (not 409) when the alias already exists, so a
    # replayed create — a double click, a browser retry — must fall through to
    # the lookup: if the organization is there, creating it was a success.
    found = []
    for attempt in (0, 1):
        r2 = _rq.get(f"{base}/organizations?search={alias}", headers=h, timeout=15)
        found = r2.json() if r2.status_code == 200 else []
        if found or r.status_code in (201, 409):
            break
        # a concurrent create of the same alias can 400 on the uniqueness
        # check before the winning transaction commits — look again once
        time.sleep(0.5)
    if r.status_code not in (201, 409) and not found:
        # surface what Keycloak actually said — "(400)" alone cost a day (#267)
        print(f"org create failed: kc={r.status_code} body={r.text[:300]!r} "
              f"realm={KC_REALM} alias={alias}", flush=True)
        raise HTTPException(502, f"Organization creation failed "
                                 f"({r.status_code}: {r.text[:120]})")
    org_id = found[0]["id"]
    _rq.post(f"{base}/organizations/{org_id}/members",
             json=c["sub"], headers={**h, "Content-Type": "application/json"}, timeout=15)
    with cursor() as cur:
        cur.execute("""INSERT INTO organization (id, name) VALUES (%s::uuid, %s)
                       ON CONFLICT (id) DO NOTHING""", (org_id, name))
        cur.execute("""INSERT INTO tenant (key, name, email)
                       VALUES (%s::uuid, %s, %s) ON CONFLICT (key) DO NOTHING""",
                    (org_id, name, c.get("email", "")))
        _provision_org_db(cur, org_id)
        cur.connection.commit()
    return {"id": org_id, "name": name,
            "note": "Sign in again so your session carries the organization."}


# --- Org-scoped data: same storage as tenants, keyed by the org id --------

@app.get("/v1/org/grafana")
def org_grafana(request: Request):
    """The organization's private Grafana: its org id and dashboard URLs (#13).

    Provisioning happens on the first authenticated call; if Grafana was
    unreachable then, this retries it rather than returning a dead link.
    """
    c, org = _require_org(request)
    with cursor() as cur:
        gorg = _provision_grafana_org(cur, org[0], org[1], c.get("email", ""))
        cur.connection.commit()
    if not gorg:
        raise HTTPException(503, "Private dashboards are not provisioned yet.")
    return {"grafana_org_id": gorg,
            "dashboard_url": f"{_base_of(request)}/grafana/d/org-private?orgId={gorg}",
            "embed_url": f"{_base_of(request)}/grafana/d-solo/org-private?orgId={gorg}&panelId=1"}


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


@app.delete("/v1/org/tokens/{token}", status_code=204)
def org_token_revoke(request: Request, token: str):
    """Revoke a service token. Kept as a row so the audit trail survives."""
    _, org = _require_org(request)
    with cursor() as cur:
        cur.execute("UPDATE org_token SET revoked = true "
                    "WHERE org = %s::uuid AND token = %s::uuid", (org[0], token))
        if cur.rowcount == 0:
            raise HTTPException(404, "No such token in this organization.")
        cur.connection.commit()
    return Response(status_code=204)


@app.delete("/v1/orgs/{org_id}")
def delete_org(request: Request, org_id: str):
    """Delete the caller's own organization: purge its private data and its
    Keycloak organization, keep a tombstone row (name, created_at,
    archived_at) so removals stay measurable. Irreversible."""
    c, org = _require_org(request)
    if org[0] != org_id:
        raise HTTPException(403, "You can only delete your own organization.")
    # Purge private data (customer data goes), keep the org row as tombstone.
    with cursor() as cur:
        cur.execute("DELETE FROM tenant_telemetry WHERE tenant = %s::uuid", (org_id,))
        cur.execute("DELETE FROM org_token WHERE org = %s::uuid", (org_id,))
        cur.execute("DELETE FROM org_user WHERE org = %s::uuid", (org_id,))
        cur.execute("DELETE FROM tenant WHERE key = %s::uuid", (org_id,))
        cur.execute("""UPDATE organization SET active = false, archived_at = now()
                       WHERE id = %s::uuid""", (org_id,))
        cur.connection.commit()
    # Delete the Keycloak organization (source of truth). Best-effort: the
    # local purge already happened; log but do not fail the request.
    try:
        at = _kc_admin_token()
        base = f"{KC_INTERNAL.rsplit('/realms/',1)[0]}/admin/realms/{KC_REALM}"
        _rq.delete(f"{base}/organizations/{org_id}",
                   headers={"Authorization": f"Bearer {at}"}, timeout=15)
    except Exception as e:
        print(f"[org-delete] Keycloak org {org_id} not removed: {e}")
    return {"deleted": org_id, "name": org[1]}


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
            # DevSat includes exactly ONE satellite slot (#275): a push naming a
            # second one is refused with the upgrade path, not silently dropped.
            cur.execute("SELECT plan FROM organization WHERE id = %s::uuid", (key,))
            plan_row = cur.fetchone()
            if plan_row and plan_row[0] == "devsat":
                cur.execute("""SELECT DISTINCT satellite FROM tenant_telemetry
                               WHERE tenant = %s::uuid""", (key,))
                existing = {r[0] for r in cur.fetchall()}
                if existing and body.satellite not in existing:
                    raise HTTPException(
                        403, "The DevSat plan includes one satellite "
                             f"({sorted(existing)[0]!r}). Upgrade to Pro for a "
                             "fleet — /w/account.")
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
        # meter private-telemetry ingest (POLAR.md) before commit, so usage and
        # telemetry persist atomically; dry-run unless Polar is configured
        metering.record(cur, key, "frame_ingested", len(body.points),
                        {"satellite": body.satellite})
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
        row = _tenant(cur, key)
        customer = str(row[2]) if len(row) > 2 else key   # same id push meters under
        cur.execute("""SELECT ts, value_num, value_txt FROM tenant_telemetry
                       WHERE tenant = %s::uuid AND satellite = %s AND field = %s
                         AND ts > now() - %s * interval '1 hour'
                       ORDER BY ts""", (key, satellite, field, hours))
        out = [{"ts": ts.isoformat(), "value": n if n is not None else t}
               for ts, n, t in cur.fetchall()]
        # meter the private telemetry read (TM request)
        metering.record(cur, customer, "tm_request", 1, {"satellite": satellite, "field": field})
        cur.connection.commit()
        return out


# --- Billing (Polar) — POLAR.md sandbox spike ------------------------------
# Free -> Pro. Entitlement flips on the Polar webhook (source of truth), never on
# the browser redirect. Stub-capable: the whole flow works without Polar creds.
import json as _json
import datetime as _dt

PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://overwatch.confinia.io")


def _base_of(request: Request) -> str:
    """The origin THIS request arrived on.

    staging and production are the same container (two colours of one blue/green
    stack), so a compiled-in PUBLIC_BASE cannot represent both hostnames: a
    visitor signing in on staging was sent back to the production host, where
    the state cookie set on the staging origin is never presented (#152). Caddy
    preserves the original Host, so trust it and keep PUBLIC_BASE as fallback.
    """
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        return PUBLIC_BASE
    proto = request.headers.get("x-forwarded-proto", "https").split(",")[0].strip()
    return f"{proto}://{host}"


# Cloud vs self-host editions (#276). Payments exist ONLY in the cloud
# edition; the default — an operator who copies the compose profile and sets
# nothing — is the paymentless one, so a leaked cloud .env cannot arm a
# checkout on-prem. Our own deploy composes set OVERWATCH_EDITION=cloud
# explicitly.
EDITION = os.environ.get("OVERWATCH_EDITION", "selfhost").strip().lower() or "selfhost"


def _cloud_only():
    """The self-host edition has NO billing surface: every /v1/billing/*
    answers 404 — the webhook included, so there is nothing to secure or
    exempt in a self-host Caddyfile."""
    if EDITION != "cloud":
        raise HTTPException(404, "Not found")


def _apply_billing_event(cur, ev):
    """Flip an org's entitlement from a normalized Polar event (idempotent SQL)."""
    org_id = ev.get("org_id")
    if not org_id:
        return
    typ, status = ev.get("type", ""), ev.get("status")
    active = (typ in ("subscription.active", "subscription.created",
                      "subscription.updated", "order.created")
              and status in ("active", "trialing", None))
    if active:
        cur.execute(
            "UPDATE organization SET plan=%s, sub_status='active', subscription_id=%s, "
            "entitled_until = COALESCE(%s::timestamptz, now() + interval '32 days'), "
            "polar_customer_id = COALESCE(%s, polar_customer_id) "
            "WHERE id=%s::uuid",
            (ev.get("plan") or "pro", ev.get("subscription_id"), ev.get("until"),
             ev.get("customer_id"), org_id))
        _provision_org_db(cur, org_id)                 # ensure RLS role (idempotent)
    elif typ == "subscription.canceled":
        cur.execute("UPDATE organization SET sub_status='canceled' WHERE id=%s::uuid",
                    (org_id,))                          # keep access until period end
    elif typ == "subscription.revoked":
        cur.execute("UPDATE organization SET plan='free', sub_status='revoked', "
                    "entitled_until=now() WHERE id=%s::uuid", (org_id,))


@app.get("/v1/billing/mode")
def billing_mode():
    _cloud_only()
    """Which payment mode this API is actually in — public and unauthenticated.

    The UI badge MUST come from here, not from a POLAR_ENV copied into the web
    container: two sources drift, and a payment-safety badge that can say
    "sandbox" while the API charges real cards is worse than no badge (#256).
    Exposes only the mode, never a token or product id. `polar_env` is the
    legacy field name the badge used before the provider seam (#269) — kept so
    a cached web bundle keeps rendering the right badge.
    """
    return {"provider": billing.PROVIDER, "env": billing.ENV,
            "polar_env": billing.ENV}


class CheckoutRequest(BaseModel):
    plan: str = "pro"


@app.post("/v1/billing/checkout")
def billing_checkout(request: Request, body: CheckoutRequest | None = None):
    """Mint an embedded checkout for the caller's org — they never leave Overwatch."""
    _cloud_only()
    c, org = _require_org(request)
    if not (billing.configured() or billing.stub_allowed()):
        raise HTTPException(503, "Billing is not open yet — contact contact@confinia.io.")
    plan = (body.plan if body else "pro").lower()
    if plan not in ("pro", "devsat"):
        raise HTTPException(422, f"Unknown plan {plan!r}.")
    try:
        ck = billing.create_checkout(org[0], c.get("email", ""),
                                     f"{_base_of(request)}/w/account?upgraded=1",
                                     plan=plan)
    except LookupError as e:
        raise HTTPException(503, str(e))
    return {"checkout_url": ck["url"], "checkout_id": ck["id"], "stub": ck["stub"]}


@app.post("/v1/billing/portal")
def billing_portal(request: Request):
    """Link the caller's org to the merchant of record's customer portal —
    where the end-user downloads invoices/receipts and manages the
    subscription. The MoR owns invoicing; we only mint the session and return
    its URL."""
    _cloud_only()
    c, org = _require_org(request)
    if not (billing.configured() or billing.stub_allowed()):
        raise HTTPException(503, "Billing is not open yet — contact contact@confinia.io.")
    with cursor() as cur:
        cur.execute("SELECT polar_customer_id FROM organization WHERE id=%s::uuid",
                    (org[0],))
        row = cur.fetchone()
    ps = billing.create_customer_session(org[0], f"{_base_of(request)}/w/account",
                                         customer_id=(row and row[0]) or "")
    return {"portal_url": ps["url"], "stub": ps["stub"]}


@app.post("/v1/billing/webhook")
async def billing_webhook(request: Request):
    """MoR -> us: signature-verified, idempotent; the entitlement source of truth."""
    _cloud_only()
    raw = await request.body()
    hdrs = {k.lower(): v for k, v in request.headers.items()}
    if not billing.verify_webhook(raw, hdrs):
        raise HTTPException(401, "bad webhook signature")
    payload = _json.loads(raw or b"{}")
    ev = billing.parse_event(payload)
    delivery = hdrs.get("webhook-id") or hashlib.sha256(raw).hexdigest()
    with cursor() as cur:
        cur.execute("INSERT INTO billing_event (delivery_id, type, payload) "
                    "VALUES (%s,%s,%s) ON CONFLICT (delivery_id) DO NOTHING",
                    (delivery, ev.get("type"), _json.dumps(payload)))
        if cur.rowcount == 0:                           # already processed -> idempotent
            cur.connection.commit()
            return {"ok": True, "duplicate": True}
        _apply_billing_event(cur, ev)
        cur.execute("UPDATE billing_event SET processed=true WHERE delivery_id=%s", (delivery,))
        cur.connection.commit()
    return {"ok": True, "type": ev.get("type")}


@app.get("/v1/billing/status")
def billing_status(request: Request):
    """Org plan, entitlement and current-period usage — for the account UI."""
    _cloud_only()
    c, org = _require_org(request)
    org_id = org[0]
    with cursor() as cur:
        cur.execute("SELECT plan, sub_status, entitled_until, freq_tier "
                    "FROM organization WHERE id=%s::uuid", (org_id,))
        plan, sub, until, tier = cur.fetchone() or ("free", None, None, "standard")
        cur.execute("SELECT frames, tm_count, tc_count FROM org_usage "
                    "WHERE customer=%s AND period=%s", (str(org_id), metering._period()))
        u = cur.fetchone() or (0, 0, 0)
    entitled = bool(until and until > _dt.datetime.now(_dt.timezone.utc))
    pro = bool(plan == "pro" and entitled)
    paid = bool(plan in ("pro", "devsat") and entitled)
    return {"plan": plan, "pro": pro, "paid": paid, "sub_status": sub,
            "entitled_until": until.isoformat() if until else None, "freq_tier": tier,
            "usage": {"frames": u[0], "tm": u[1], "tc": u[2], "period": metering._period()}}


@app.post("/v1/billing/dev/simulate-paid")
def billing_simulate_paid(request: Request):
    """DEV/sandbox only: simulate a completed payment for the caller's org so the
    checkout -> webhook -> entitlement flow is provable in-app without Polar.
    Only where the stub is explicitly allowed (POLAR_ALLOW_STUB, never prod)."""
    _cloud_only()
    if not billing.stub_allowed():
        raise HTTPException(403, "stub billing not enabled here")
    c, org = _require_org(request)
    with cursor() as cur:
        _apply_billing_event(cur, {"type": "subscription.active", "org_id": org[0],
                                   "subscription_id": f"sim_{org[0]}", "status": "active"})
        cur.connection.commit()
    return {"ok": True, "simulated": True, "org": org[0]}


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
<li><a href="/telemetry.html">Push your OWN satellite's telemetry — contract, limits, errors</a></li>
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
