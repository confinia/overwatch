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


def db():
    return psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor)


@app.get("/api/satellites")
def satellites():
    """Latest known position for each showcase satellite + metadata."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT s.norad, s.name, s.has_telemetry, s.note,
                   p.lat, p.lon, p.alt_km, p.ts
            FROM satellite s
            LEFT JOIN LATERAL (
                SELECT lat, lon, alt_km, ts FROM position
                WHERE norad = s.norad ORDER BY ts DESC LIMIT 1
            ) p ON true
            ORDER BY s.name""")
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


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
