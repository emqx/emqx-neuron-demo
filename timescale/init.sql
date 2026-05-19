CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS plc_telemetry (
  ts             TIMESTAMPTZ NOT NULL,
  plc_id         TEXT,
  axis_id        TEXT,
  spindle_rpm    DOUBLE PRECISION,
  feed_mm_min    DOUBLE PRECISION,
  coolant_temp_c DOUBLE PRECISION,
  torque_nm      DOUBLE PRECISION,
  power_kw       DOUBLE PRECISION,
  state          TEXT,
  raw            JSONB
);

SELECT create_hypertable('plc_telemetry', 'ts', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS plc_telemetry_plc_ts ON plc_telemetry (plc_id, axis_id, ts DESC);


CREATE TABLE IF NOT EXISTS batches (
  ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
  batch_id     TEXT PRIMARY KEY,
  order_id     TEXT,
  recipe_id    TEXT,
  plc_id       TEXT,
  part_serial  TEXT,
  state        TEXT,
  started_at   TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  fail_reason  TEXT,
  raw          JSONB                                  -- full record; events at raw->'events'
);

CREATE INDEX IF NOT EXISTS batches_started_idx
  ON batches (COALESCE(completed_at, started_at) DESC);
