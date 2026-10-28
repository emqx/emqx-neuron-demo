# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "psycopg[binary]>=3.2,<4"]
# ///
"""Provision product viewers and database grants for fresh and existing volumes."""
import os
from pathlib import Path
import httpx
import psycopg
from psycopg import sql
from init_security import load_env


def main():
    env = {**load_env(), **os.environ}
    base = env.get("EMQX_API_URL", "http://localhost:18083")
    with httpx.Client(base_url=base, timeout=15) as client:
        login = client.post("/api/v5/login", json={"username": "admin", "password": env["EMQX_ADMIN_PASSWORD"]})
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["token"]
        response = client.get("/api/v5/users")
        response.raise_for_status()
        users = response.json()
        if isinstance(users, dict):
            users = users["data"]
        if not any(user["username"] == "viewer" for user in users):
            response = client.post("/api/v5/users", json={"username": "viewer", "password": "demo-viewer", "role": "viewer", "description": "Public demo read-only account"})
            response.raise_for_status()
        response = client.put("/api/v5/users/viewer", json={"role": "viewer", "description": "Public demo read-only account",
                               "scopes": ["connections", "monitoring", "data_integration", "license"]})
        if response.status_code >= 400:
            raise RuntimeError(f"Could not restrict the EMQX viewer: {response.text}")
        response.raise_for_status()
    # An existing Grafana volume does not apply a new ADMIN_PASSWORD env var.
    # Rotate only the documented default; do not overwrite an unknown password.
    grafana = env.get("GRAFANA_API_URL", "http://localhost:3000")
    desired = env["GRAFANA_ADMIN_PASSWORD"]
    with httpx.Client(base_url=grafana, timeout=15, auth=("admin", desired)) as client:
        response = client.get("/api/user")
        if response.status_code == 401:
            response = client.get("/api/user", auth=("admin", "admin"))
            if response.status_code == 401:
                raise RuntimeError("Set GRAFANA_ADMIN_PASSWORD in .env to the existing Grafana admin password.")
            response.raise_for_status()
            user_id = response.json()["id"]
            changed = client.put(f"/api/admin/users/{user_id}/password", auth=("admin", "admin"), json={"password": desired})
            changed.raise_for_status()
        else:
            response.raise_for_status()
    with psycopg.connect(host=env.get("DB_HOST", "localhost"), dbname="demo", user="demo", password="demo", autocommit=True) as db:
        db.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='grafana_reader') THEN CREATE ROLE grafana_reader LOGIN; END IF; END $$")
        db.execute(sql.SQL("ALTER ROLE grafana_reader WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 15 PASSWORD {}").format(sql.Literal(env["GRAFANA_DB_PASSWORD"])))
        db.execute("ALTER ROLE grafana_reader SET statement_timeout = '5s'")
        db.execute("ALTER ROLE grafana_reader SET default_transaction_read_only = on")
        db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        db.execute("REVOKE TEMPORARY ON DATABASE demo FROM PUBLIC")
        db.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_reader")
        db.execute("GRANT CONNECT ON DATABASE demo TO grafana_reader")
        db.execute("GRANT USAGE ON SCHEMA public TO grafana_reader")
        db.execute("GRANT SELECT ON plc_telemetry, hvac_telemetry, batches TO grafana_reader")
        db.execute((Path(__file__).resolve().parents[1] / "timescale" / "retention.sql").read_text())
    print("EMQX viewer ready: viewer / demo-viewer. Neuron viewer uses the same credentials.")
    print("Grafana datasource is restricted to SELECT on demo history; retention policies installed.")


if __name__ == "__main__":
    main()
