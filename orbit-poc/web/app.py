"""
Read-only position API for the map. Serves ONLY from the local cache.
This is the boundary in action: the browser talks to us, we talk to Postgres,
and nothing here ever calls CelesTrak or SatNOGS.
"""
import os
from flask import Flask, jsonify, send_from_directory
import psycopg2
from psycopg2.extras import RealDictCursor

DB_DSN = os.environ["DB_DSN"]
app = Flask(__name__, static_folder="static", static_url_path="")

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
        return jsonify(cur.fetchall())


@app.get("/api/receptions/<int:norad>")
def receptions(norad):
    """Who heard this satellite in the last 7 days: receiving station
    (Maidenhead-decoded) + the satellite's cached position at reception time
    when our position history covers it."""
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
            ) p ON true
            WHERE r.norad = %s AND r.ts > now() - interval '7 days'
            ORDER BY r.ts DESC LIMIT 300""", (norad,))
        return jsonify(cur.fetchall())


@app.get("/api/track/<int:norad>")
def track(norad):
    """Recent ground track for one satellite (for drawing the orbit line)."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT lat, lon, ts FROM position
            WHERE norad = %s AND ts > now() - interval '100 minutes'
            ORDER BY ts""", (norad,))
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
            ) p ON true
            WHERE r.observer = %s AND r.ts > now() - interval '7 days'
            ORDER BY r.ts DESC LIMIT 500""", (observer,))
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
