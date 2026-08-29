-- Local cache schema. MapLibre and Grafana read ONLY from here.
-- Upstream APIs (CelesTrak, SatNOGS) are touched only by the ingest service.

CREATE TABLE IF NOT EXISTS satellite (
    norad        INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    sat_id       TEXT,              -- SatNOGS sat_id, may be NULL (position-only)
    has_telemetry BOOLEAN DEFAULT FALSE,
    decoder      TEXT,              -- satnogs-decoders module for LOCAL decoding
    note         TEXT
);
-- migration for pre-existing databases (init.sql only runs on first boot)
ALTER TABLE satellite ADD COLUMN IF NOT EXISTS decoder TEXT;
ALTER TABLE satellite ADD COLUMN IF NOT EXISTS last_telemetry_fetch TIMESTAMPTZ;

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
    sunlit       BOOLEAN,           -- false = in Earth's shadow (eclipse)
    PRIMARY KEY (norad, ts)
);
ALTER TABLE position ADD COLUMN IF NOT EXISTS sunlit BOOLEAN;
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

-- Who heard whom: one row per (frame, receiving station). The observer string
-- from SatNOGS is "CALLSIGN-GRIDLOCATOR"; lat/lon decoded from the Maidenhead
-- grid locator (NULL when the station publishes no locator).
CREATE TABLE IF NOT EXISTS reception (
    norad        INTEGER REFERENCES satellite(norad),
    ts           TIMESTAMPTZ NOT NULL,
    observer     TEXT NOT NULL,
    -- SatNOGS app_source: 'network' (network observation) or 'sids' (direct
    -- upload). One operator runs both kinds under one callsign and cannot
    -- tell them apart in a station list (#97). NULL = ingested before this.
    source       TEXT,
    lat          DOUBLE PRECISION,
    lon          DOUBLE PRECISION,
    PRIMARY KEY (norad, ts, observer)
);
CREATE INDEX IF NOT EXISTS reception_obs_idx ON reception (observer, ts DESC);

-- Upcoming passes per ground station (#217): recomputed periodically by the
-- ingest (forward SGP4 + topocentric elevation >= threshold). Drives the
-- "next passes" Grafana table (sorted by AOS, colour-coded by imminence).
CREATE TABLE IF NOT EXISTS pass (
    observer     TEXT NOT NULL,
    norad        INTEGER REFERENCES satellite(norad),
    aos          TIMESTAMPTZ NOT NULL,   -- rise: elevation crosses up
    los          TIMESTAMPTZ NOT NULL,   -- set:  elevation crosses down
    max_el_deg   DOUBLE PRECISION,       -- peak elevation of the pass
    PRIMARY KEY (observer, norad, aos)
);
CREATE INDEX IF NOT EXISTS pass_aos_idx ON pass (aos);
