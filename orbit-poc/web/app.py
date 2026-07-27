"""
Read-only position API for the map. Serves ONLY from the local cache.
This is the boundary in action: the browser talks to us, we talk to Postgres,
and nothing here ever calls CelesTrak or SatNOGS.
"""
import json
import os
from flask import Flask, jsonify, request, send_from_directory
import psycopg2
from psycopg2.extras import RealDictCursor

DB_DSN = os.environ["DB_DSN"]
app = Flask(__name__, static_folder="static", static_url_path="")

# Per-satellite SatNOGS Telemetry Dashboard links (#88), discovered by
# batch/resolve_satnogs_dashboards.py. Surfaced next to our own auto-grouped
# panels so the mission team's curated dashboard is one click away.
_DASH_PATH = os.path.join(os.path.dirname(__file__), "satnogs_dashboards.json")
try:
    with open(_DASH_PATH, encoding="utf-8") as _f:
        SATNOGS_DASHBOARDS = json.load(_f)
except (OSError, ValueError):
    SATNOGS_DASHBOARDS = {}

# Per-satellite country of origin (#99), ISO-2, for a flag in the list.
_COUNTRY_PATH = os.path.join(os.path.dirname(__file__), "satellite_countries.json")
try:
    with open(_COUNTRY_PATH, encoding="utf-8") as _f:
        SATELLITE_COUNTRIES = json.load(_f)
except (OSError, ValueError):
    SATELLITE_COUNTRIES = {}


def _hours(default=168):
    """Selected time window in hours, bounded 1h–7d (168h) to protect the
    cache/DB. Drives receptions, track and fields so they share one window."""
    try:
        h = int(request.args.get("hours", default))
    except (TypeError, ValueError):
        h = default
    return max(1, min(h, 168))

# OpenTelemetry: every request becomes a trace; the collector's spanmetrics
# connector turns them into per-route rate/latency/error metrics for the
# admin-only "Platform access" dashboard. No-op when the endpoint is unset.
if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    _provider = TracerProvider(resource=Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME", "overwatch-web")}))
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_provider)
    FlaskInstrumentor().instrument_app(app)
    _ui_tracer = trace.get_tracer("overwatch-ui")
else:
    _ui_tracer = None


def db():
    return psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor)


# A station can only hear a LEO satellite above its horizon (~2600 km ground
# distance for typical altitudes). The reception timestamp does not always
# align with the true frame time, so matching it to the continuous position
# cache can land on a random orbit point; this great-circle guard drops those
# physically impossible links (the station dot still shows; only the line to
# the sub-satellite point is suppressed). 3000 km covers LEO up to ~800 km.
HORIZON_KM = 3000
_within_horizon = """
    2*6371*asin(sqrt(power(sin(radians(p.lat - r.lat)/2), 2)
      + cos(radians(r.lat))*cos(radians(p.lat))
        *power(sin(radians(p.lon - r.lon)/2), 2))) <= %s
"""


@app.get("/api/satellites")
def satellites():
    """Latest known position for each showcase satellite + metadata."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT s.norad, s.name, s.has_telemetry, s.note,
                   p.lat, p.lon, p.alt_km, p.ts, tf.last_frame
            FROM satellite s
            LEFT JOIN LATERAL (
                SELECT lat, lon, alt_km, ts FROM position
                WHERE norad = s.norad ORDER BY ts DESC LIMIT 1
            ) p ON true
            LEFT JOIN LATERAL (
                SELECT max(ts) AS last_frame FROM telemetry
                WHERE norad = s.norad
            ) tf ON true
            ORDER BY s.name""")
        rows = cur.fetchall()
    for r in rows:
        url = SATNOGS_DASHBOARDS.get(str(r["norad"]))
        if url:
            r["satnogs_dashboard"] = url
        cc = SATELLITE_COUNTRIES.get(str(r["norad"]))
        if cc:
            r["country"] = cc
    return jsonify(rows)


@app.get("/api/receptions/<int:norad>")
def receptions(norad):
    """Who heard this satellite in the selected window (default 7 days):
    receiving station (Maidenhead-decoded) + the satellite's cached position at
    reception time when our position history covers it."""
    hours = _hours()
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT r.ts, r.observer, r.lat, r.lon,
                   p.lat AS sat_lat, p.lon AS sat_lon
            FROM reception r
            LEFT JOIN LATERAL (
                SELECT lat, lon FROM position
                WHERE norad = r.norad
                  AND ts BETWEEN r.ts - interval '2 minutes'
                             AND r.ts + interval '2 minutes'
                ORDER BY abs(extract(epoch FROM ts - r.ts)) LIMIT 1
            ) p ON """ + _within_horizon + """
            WHERE r.norad = %s AND r.ts > now() - %s * interval '1 hour'
            ORDER BY r.ts DESC LIMIT 300""", (HORIZON_KM, norad, hours))
        return jsonify(cur.fetchall())


@app.get("/api/track/<int:norad>")
def track(norad):
    """Default: the full ground track over the SELECTED window (`hours`, #79),
    ending at the satellite's current position — downsampled server-side to
    ~2000 points so a multi-day track stays light; the frontend fades/thins it
    as the window grows so 7 days reads as a swath, not a flood. With ?heard=1,
    returns instead the heard-pass arcs over the window: only position points
    near a reception (±4 min), so each orange reception line lands on a short
    orbit arc (#70). The frontend splits both into per-segment paths (time gap)
    and at the antimeridian (#66)."""
    hours = _hours()
    with db() as conn, conn.cursor() as cur:
        if request.args.get("heard"):
            cur.execute("""
                SELECT p.lat, p.lon, p.ts
                FROM position p
                WHERE p.norad = %s AND p.ts > now() - %s * interval '1 hour'
                  AND EXISTS (
                    SELECT 1 FROM reception r
                    WHERE r.norad = p.norad
                      AND r.ts BETWEEN p.ts - interval '4 minutes'
                                    AND p.ts + interval '4 minutes')
                ORDER BY p.ts""", (norad, hours))
        else:
            cur.execute("""
                WITH t AS (
                    SELECT lat, lon, ts, row_number() OVER (ORDER BY ts) AS rn,
                           count(*) OVER () AS total
                    FROM position
                    WHERE norad = %s AND ts > now() - %s * interval '1 hour'
                )
                SELECT lat, lon, ts FROM t
                WHERE rn %% (GREATEST(total / 2000, 1))::int = 0 OR rn = total
                ORDER BY ts""", (norad, hours))
        return jsonify(cur.fetchall())


@app.get("/api/stations")
def stations():
    """All ground stations heard in the last 7 days, aggregated — feeds the
    station search (station-first view: 'does MY station appear?')."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT observer, max(lat) AS lat, max(lon) AS lon,
                   count(*) AS frames, count(DISTINCT norad) AS sats,
                   max(ts) AS last_rx
            FROM reception
            WHERE ts > now() - interval '7 days' AND lat IS NOT NULL
            GROUP BY observer ORDER BY frames DESC""")
        return jsonify(cur.fetchall())


@app.get("/api/station/<path:observer>")
def station(observer):
    """One station's receptions across the whole fleet (7 days), with the
    satellite's cached position at each reception when history covers it."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT r.ts, r.norad, s.name, r.lat, r.lon,
                   p.lat AS sat_lat, p.lon AS sat_lon
            FROM reception r JOIN satellite s USING (norad)
            LEFT JOIN LATERAL (
                SELECT lat, lon FROM position
                WHERE norad = r.norad
                  AND ts BETWEEN r.ts - interval '2 minutes'
                             AND r.ts + interval '2 minutes'
                ORDER BY abs(extract(epoch FROM ts - r.ts)) LIMIT 1
            ) p ON """ + _within_horizon + """
            WHERE r.observer = %s AND r.ts > now() - interval '7 days'
            ORDER BY r.ts DESC LIMIT 500""", (HORIZON_KM, observer,))
        return jsonify(cur.fetchall())


@app.get("/api/event")
def ui_event():
    """First-party usage beacon: page loads, satellite selections, searches.
    No cookies, no ids — just anonymous counters as OTel spans, turned into
    metrics by the collector's spanmetrics connector (admin-only dashboard)."""
    from flask import request
    etype = request.args.get("type", "")
    if etype not in ("load", "select", "search"):
        return {"ok": False}, 400
    if _ui_tracer is not None:
        with _ui_tracer.start_as_current_span(f"ui.{etype}") as span:
            origin = request.args.get("origin", "")
            if origin in ("direct", "mirror", "local"):
                span.set_attribute("origin", origin)
            norad = request.args.get("norad", "")
            if norad.isdigit():
                span.set_attribute("sat_norad", norad)
    return {"ok": True}


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/w/<view>")
def window(view):
    """Chromeless, URL-driven control-room windows (#49): /w/<view> serves
    <view>.html. e.g. /w/spacecraft?sat=25544&chrome=0 is the dedicated
    spacecraft view (#55), embeddable in a control-room grid. Query params are
    read client-side by the page."""
    safe = view.replace("/", "").replace("..", "").replace("\\", "")
    if os.path.isfile(os.path.join(app.static_folder, f"{safe}.html")):
        return send_from_directory("static", f"{safe}.html")
    return ("unknown view", 404)


@app.get("/api/version")
def version():
    """SaaS + API version for the frontend badge. Single source of truth is
    the VERSION file at the repo root, injected as env by the deploy."""
    return {"version": os.environ.get("OVERWATCH_VERSION", "dev"), "api": "v1"}


@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
