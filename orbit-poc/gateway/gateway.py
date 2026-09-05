"""SatNOGS egress gateway — the one door to db.satnogs.org.

Every Overwatch process that reads SatNOGS (the always-on `ingest`, and any
operational batch tooling) points at THIS service instead of db.satnogs.org
directly. It is the only thing on the deployment allowed to reach SatNOGS at
all — everything else has the host blackholed — so the per-user rate limit is
enforced in ONE place that no new caller can bypass:

  * one global minimum gap between real upstream requests. SatNOGS throttles
    per USER (get_telemetry_user = 6/min in satnogs-db settings), not per
    process, so the whole deployment has to share one gate. This gateway IS
    that user; the gap is the gate.
  * a response cache per endpoint, so re-runs and backfills never touch SatNOGS.
  * Retry-After honoured, with a cooldown PERSISTED to disk, so a restart does
    not resume hammering a provider that just refused us (the exact gap that
    let ad-hoc tooling overspend the budget twice).
  * the token injected here — callers never hold it.
  * every real upstream request recorded (upstream_request) for the ops monitor,
    so our true SatNOGS footprint is visible on Grafana, cache hits excluded.

Selfhost does NOT run this: a single-tenant install talks to SatNOGS directly
from its own paced ingest. The gateway exists for the multi-caller cloud, where
more than one process shares one token and one IP.
"""
import logging
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("satnogs-gateway")


def _http_get(url, headers, timeout):
    """The real upstream call. `requests` is imported lazily so the module (and
    its unit tests, which inject a fake) import with no third-party deps."""
    import requests
    return requests.get(url, headers=headers, timeout=timeout)

UPSTREAM = os.environ.get("SATNOGS_UPSTREAM", "https://db.satnogs.org").rstrip("/")
TOKEN = os.environ.get("SATNOGS_TOKEN", "").strip()
MIN_GAP = float(os.environ.get("SATNOGS_MIN_GAP", 11))   # 6/min = one per 10s, + margin
PORT = int(os.environ.get("GATEWAY_PORT", 8088))
DB_DSN = os.environ.get("DB_DSN", "")
UA = os.environ.get("HTTP_USER_AGENT",
                    "overwatch/1.0 (+https://overwatch.confinia.io; contact@confinia.io)")
COOLDOWN_FILE = os.environ.get("COOLDOWN_FILE", "/tmp/satnogs_cooldown")

# Per-endpoint freshness, matched on the first path segment after /api/. A
# backfill re-reading the same satellite's telemetry inside the window is served
# from cache instead of becoming a real upstream request.
TTL = {
    "telemetry":  int(os.environ.get("TTL_TELEMETRY", 1800)),    # 30m
    "tle":        int(os.environ.get("TTL_TLE", 21600)),         # 6h
    "satellites": int(os.environ.get("TTL_SATELLITES", 86400)),  # 24h
    "satellite":  int(os.environ.get("TTL_SATELLITE", 86400)),   # 24h (the /satellite/<norad> page)
}
DEFAULT_TTL = int(os.environ.get("TTL_DEFAULT", 300))


def ttl_for(path):
    """Freshness for an upstream path. Handles both the JSON API
    (/api/<endpoint>/) and the plain pages (/satellite/<norad>), so the SPOT can
    proxy ANY db.satnogs.org path, not only /api."""
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] == "api":
        parts = parts[1:]
    seg = parts[0] if parts else ""
    return TTL.get(seg, DEFAULT_TTL)


# --- OpenTelemetry metrics (optional): the SPOT emits its request rate to the
# otel-collector -> prometheus -> grafana, the same path the api uses for
# ovw.api.requests. The disposition attribute (HIT/MISS/COOL/ERR) makes the
# cache-vs-resend split a query, not a schema change. Guarded: no OTEL endpoint
# means no-op, and observability never breaks a fetch. ---
REQ_COUNTER = None
DUR_HIST = None
_OTLP = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
if _OTLP:
    try:
        from opentelemetry import metrics as _otel_metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.resources import Resource
        _reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{_OTLP}/v1/metrics"),
            export_interval_millis=15000)
        _otel_metrics.set_meter_provider(MeterProvider(
            resource=Resource.create({"service.name": os.environ.get(
                "OTEL_SERVICE_NAME", "overwatch-satnogs-gateway")}),
            metric_readers=[_reader]))
        _meter = _otel_metrics.get_meter("overwatch")
        REQ_COUNTER = _meter.create_counter(
            "ovw.satnogs.requests",
            description="SatNOGS requests through the SPOT, by disposition/status")
        DUR_HIST = _meter.create_histogram(
            "ovw.satnogs.request.duration", unit="ms",
            description="Duration of real SatNOGS upstream attempts")
    except Exception as e:  # noqa: BLE001
        log.warning("OpenTelemetry not initialized: %s", e)


def _otel(disposition, status, ms):
    """Record one request to OTel. HIT/COOL are cache/cooldown short-circuits;
    MISS/ERR are real upstream attempts (only those carry a meaningful duration)."""
    if REQ_COUNTER is None:
        return
    try:
        REQ_COUNTER.add(1, {"disposition": disposition, "status": str(status)})
        if ms is not None and disposition in ("MISS", "ERR"):
            DUR_HIST.record(ms, {"disposition": disposition})
    except Exception:  # noqa: BLE001 — metrics must never break a fetch
        pass


def make_recorder(dsn):
    """A callback that logs one real upstream request to `upstream_request`,
    so the ops dashboard charts our true SatNOGS rate (cache hits never call
    this). Best-effort: monitoring must never break a fetch."""
    import psycopg2

    def record(endpoint, status, ms):
        try:
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO upstream_request (source, endpoint, status, ms) "
                            "VALUES ('satnogs', %s, %s, %s)",
                            ((endpoint or "")[:200], status, ms))
                cur.execute("DELETE FROM upstream_request WHERE ts < now() - interval '14 days'")
                conn.commit()
        except Exception as e:  # noqa: BLE001 — never let the monitor break a fetch
            log.debug("could not record request: %s", e)

    return record


class Gateway:
    """The chokepoint logic, kept free of the HTTP layer so it is unit-testable.
    `get`, `sleep`, `now` and `record` are injectable for tests."""

    def __init__(self, get=None, sleep=time.sleep, now=time.time,
                 record=None, upstream=UPSTREAM, token=TOKEN, min_gap=MIN_GAP,
                 cooldown_file=COOLDOWN_FILE):
        self._get = get or _http_get
        self._sleep = sleep
        self._now = now
        self._record = record or (lambda *a: None)
        self.upstream = upstream.rstrip("/")
        self.token = token
        self.min_gap = min_gap
        self.cooldown_file = cooldown_file
        self._lock = threading.Lock()
        self._last = 0.0
        self._cache = {}            # key -> (expires_at, status, body, ctype)
        self._cooldown_until = self._read_cooldown()

    # --- cooldown, persisted so a restart cannot resume hammering ---
    def _read_cooldown(self):
        try:
            with open(self.cooldown_file) as f:
                return float(f.read().strip() or 0)
        except Exception:
            return 0.0

    def cooling(self):
        return max(0.0, self._cooldown_until - self._now())

    def set_cooldown(self, seconds):
        self._cooldown_until = self._now() + seconds
        try:
            with open(self.cooldown_file, "w") as f:
                f.write(str(self._cooldown_until))
        except Exception as e:  # noqa: BLE001
            log.debug("cooldown persist failed: %s", e)

    # --- one global gate: the whole deployment shares this gap ---
    def pace(self):
        with self._lock:
            wait = self.min_gap - (self._now() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = self._now()

    # --- cache ---
    def cache_get(self, key):
        hit = self._cache.get(key)
        if hit and hit[0] > self._now():
            return hit
        return None

    def cache_put(self, key, ttl, status, body, ctype):
        self._cache[key] = (self._now() + ttl, status, body, ctype)

    def fetch(self, path, query, ttl):
        """Serve a SatNOGS GET: cache -> cooldown -> gate -> upstream.
        Returns (status, body_bytes, content_type, disposition)."""
        key = path + "?" + query
        hit = self.cache_get(key)
        if hit:
            _otel("HIT", hit[1], 0)
            return hit[1], hit[2], hit[3], "HIT"
        if self.cooling() > 0:
            _otel("COOL", 503, 0)
            return (503, b'{"detail":"upstream cooling down"}',
                    "application/json", "COOL")
        self.pace()
        url = self.upstream + path + (("?" + query) if query else "")
        headers = {"User-Agent": UA}
        if self.token:
            headers["Authorization"] = "Token " + self.token
        t0 = self._now()
        status = None
        disp = "ERR"
        try:
            r = self._get(url, headers=headers, timeout=(5, 30))
            status = getattr(r, "status_code", None)
            body = r.content
            ctype = r.headers.get("Content-Type", "application/json")
            if status == 429:
                self.set_cooldown(int(r.headers.get("Retry-After", 30)) + 1)
            elif status == 200:
                self.cache_put(key, ttl, status, body, ctype)
            disp = "MISS"
            return status, body, ctype, "MISS"
        except Exception as e:  # noqa: BLE001 — a timeout is also a refusal to honour
            log.warning("upstream error for %s: %s", path, e)
            self.set_cooldown(60)   # back off on timeout too — the gap that hurt us
            return (502, b'{"detail":"upstream unreachable"}',
                    "application/json", "ERR")
        finally:
            ms = int((self._now() - t0) * 1000)
            self._record(path, status, ms)   # detailed per-request DB log
            _otel(disp, status, ms)          # aggregated metric -> prometheus


class Handler(BaseHTTPRequestHandler):
    gateway = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            return self._reply(200, b'{"ok":true}', "application/json", "-")
        # Proxy ANY db.satnogs.org path verbatim: the JSON API (/api/...) and the
        # plain pages (/satellite/<norad>) alike, so the SPOT is the one door for
        # every Overwatch SatNOGS request. Callers hit gateway/<the full path>.
        path = parsed.path
        status, body, ctype, disp = self.gateway.fetch(
            path, parsed.query, ttl_for(path))
        self._reply(status or 502, body, ctype, disp)

    def do_POST(self):   # SatNOGS reads are GET; nothing writes upstream
        self._reply(405, b'{"detail":"read-only gateway"}', "application/json", "-")

    def _reply(self, status, body, ctype, disp):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Cache", disp)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):   # our own logging, not stderr access lines
        pass


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    Handler.gateway = Gateway(record=make_recorder(DB_DSN) if DB_DSN else None)
    log.info("SatNOGS gateway on :%d -> %s (min gap %ss, token %s)",
             PORT, UPSTREAM, MIN_GAP, "set" if TOKEN else "absent")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
