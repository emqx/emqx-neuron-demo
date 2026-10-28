SELECT add_retention_policy('plc_telemetry', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('hvac_telemetry', INTERVAL '7 days', if_not_exists => TRUE);

CREATE OR REPLACE PROCEDURE cleanup_demo_batches(job_id INTEGER, config JSONB)
LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM batches WHERE ts < now() - INTERVAL '30 days';
END;
$$;
REVOKE ALL ON PROCEDURE cleanup_demo_batches(INTEGER, JSONB) FROM PUBLIC;
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM timescaledb_information.jobs WHERE proc_name = 'cleanup_demo_batches') THEN
    PERFORM add_job('cleanup_demo_batches', INTERVAL '1 hour');
  END IF;
END;
$$;
