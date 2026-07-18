-- Local cache schema. MapLibre and Grafana read ONLY from here.
-- Upstream APIs (CelesTrak, SatNOGS) are touched only by the ingest service.

CREATE TABLE IF NOT EXISTS satellite (
    norad        INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    sat_id       TEXT,              -- SatNOGS sat_id, may be NULL (position-only)
    has_telemetry BOOLEAN DEFAULT FALSE,
    note         TEXT
);

-- Latest orbital elements (OMM/JSON from CelesTrak). We keep the raw TLE lines
-- because SGP4 libraries consume them directly.
CREATE TABLE IF NOT EXISTS elements (
    norad        INTEGER REFERENCES satellite(norad),
    epoch        TIMESTAMPTZ NOT NULL,
    tle1         TEXT NOT NULL,
    tle2         TEXT NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (norad, epoch)
);

-- Propagated positions, written by the ingest service on a short cadence.
-- This is what the map reads. Keep a rolling window; prune old rows.
CREATE TABLE IF NOT EXISTS position (
    norad        INTEGER REFERENCES satellite(norad),
    ts           TIMESTAMPTZ NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    alt_km       DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (norad, ts)
);
CREATE INDEX IF NOT EXISTS position_ts_idx ON position (ts DESC);

-- Decoded telemetry frames from SatNOGS. Schema-per-satellite varies wildly,
-- so we store the decoded field/value pairs generically -> easy to graph in
-- Grafana with a WHERE field = '...' filter.
CREATE TABLE IF NOT EXISTS telemetry (
    norad        INTEGER REFERENCES satellite(norad),
    ts           TIMESTAMPTZ NOT NULL,
    field        TEXT NOT NULL,      -- e.g. 'battery_voltage', 'temp_eps', 'mode'
    value_num    DOUBLE PRECISION,   -- numeric fields (graphable)
    value_txt    TEXT,               -- categorical fields (e.g. mode name)
    PRIMARY KEY (norad, ts, field)
);
CREATE INDEX IF NOT EXISTS telemetry_lookup_idx ON telemetry (norad, field, ts DESC);
