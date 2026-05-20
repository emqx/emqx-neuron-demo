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


-- Facility-side telemetry from the BACnet HVAC unit. Schema follows the
-- BACnet object map in services/hvac-sim/hvac.py.
CREATE TABLE IF NOT EXISTS hvac_telemetry (
  ts                   TIMESTAMPTZ NOT NULL,
  hvac_id              TEXT,
  supply_air_temp_c    DOUBLE PRECISION,
  return_air_temp_c    DOUBLE PRECISION,
  outside_air_temp_c   DOUBLE PRECISION,
  chilled_water_temp_c DOUBLE PRECISION,
  fan_kw               DOUBLE PRECISION,
  filter_dp_pa         DOUBLE PRECISION,
  temp_setpoint_c      DOUBLE PRECISION,
  fan_speed_pct        DOUBLE PRECISION,
  mode_actual          INTEGER,
  -- Neuron's BACnet driver emits BV/BIT as 0/1 ints; storing as SMALLINT
  -- avoids an EMQX rule-SQL boolean cast that the parser can't express.
  unit_running         SMALLINT,
  fault_active         SMALLINT,
  raw                  JSONB
);

SELECT create_hypertable('hvac_telemetry', 'ts', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS hvac_telemetry_hvac_ts
  ON hvac_telemetry (hvac_id, ts DESC);
