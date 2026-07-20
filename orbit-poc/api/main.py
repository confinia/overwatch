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
        return JSONResponse({"detail": "API key required: POST /v1/keys {\"email\"} "
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
            "usage": f"/v1/keys/{key}/usage"}


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

GET <a href="/v1/satellites">/v1/satellites</a>

One satellite (CUBEBEL-2, the richest live beacon — NORAD 57175):

GET <a href="/v1/track/57175?minutes=100">/v1/track/57175?minutes=100</a>            → recent ground track (+ eclipse flag)
GET <a href="/v1/receptions/57175?hours=24">/v1/receptions/57175?hours=24</a>          → volunteer stations that heard it
GET <a href="/v1/telemetry/57175/fields">/v1/telemetry/57175/fields</a>             → decoded fields available
GET <a href="/v1/telemetry/57175?field=battery_v&amp;hours=24">/v1/telemetry/57175?field=battery_v&amp;hours=24</a>  → battery voltage series

Canonical fields work fleet-wide: battery_v, battery_i, battery_pct —
raw beacon fields (per-satellite naming) stay queryable next to them.</pre>
<ul>
<li><a href="https://overwatch.confinia.io/#57175">Live demo — the control room (MapLibre globe + Grafana)</a></li>
<li><a href="https://overwatch.confinia.io/article.html">The write-up — architecture &amp; decisions</a></li>
<li><a href="/v1/docs">Interactive documentation (OpenAPI)</a></li>
<li><a href="/v1/healthz">Service health</a></li>
</ul>
<footer>Version __VERSION__ · Free during development — no key required yet
(<code>POST /v1/keys {"email": …}</code> to get one for the beta;
<code>/v1/keys/{key}/usage</code> shows your own consumption).
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
