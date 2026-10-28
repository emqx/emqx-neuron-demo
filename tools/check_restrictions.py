# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "paho-mqtt==2.1.0", "psycopg[binary]>=3.2,<4"]
# ///
"""Live restriction checks using independent cookie jars; exercises device claims and a complete batch."""
import argparse
import json
import threading
import time
import uuid
import httpx
import paho.mqtt.client as mqtt
import psycopg
from init_security import load_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8080")
    args = parser.parse_args()
    clients = [httpx.Client(base_url=args.url, timeout=10) for _ in range(6)]
    env = load_env()
    def no_credentials(response):
        assert "BEGIN PRIVATE KEY" not in response.text and "BEGIN RSA PRIVATE KEY" not in response.text
        for key, value in env.items():
            if key.endswith(("PASSWORD", "SECRET", "KEY")) and value:
                assert value not in response.text, f"Viewer response exposed {key}: {response.request.url}"
    prefix = clients[0].get("/api/state").json()
    prefix = prefix["enterprise"] + "/" + prefix["site"]

    def state(client):
        response = client.get("/api/demo/state")
        response.raise_for_status()
        return response.json()

    def post(client, path, body, expected):
        visitor = state(client)["visitor"]
        response = client.post(path, json=body, headers={"X-Demo-CSRF": visitor["csrf"]})
        assert response.status_code == expected, (path, response.status_code, response.text)
        return response.json()

    assert state(clients[0])["ready"], "Provision the stack and wait for live telemetry."
    assert not any(d["busy"] or d["waiting"] for d in state(clients[0])["visitor"]["devices"].values()), "Device claims are already active."
    assert clients[0].post("/api/demo/start", json={}).status_code == 403
    print("PASS: controls require the visitor's CSRF token", flush=True)

    # Broker permissions are checked on an actual MQTT 5 WebSocket connection.
    credentials = clients[0].get("/api/mqtt-credentials?slot=test").json()
    connection = threading.Event()
    sample = threading.Event()
    rejected = threading.Event()
    codes = []
    subscriber = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=credentials["clientId"],
                             transport="websockets", protocol=mqtt.MQTTv5)
    subscriber.username_pw_set(credentials["username"], credentials["password"])
    def connected(client, _u, _f, reason, _p):
        codes.append(reason.value)
        connection.set()
        if not reason.is_failure:
            client.subscribe(prefix + "/+/+/+/telemetry/+", qos=0)
    subscriber.on_connect = connected
    subscriber.on_message = lambda *_: sample.set()
    subscriber.on_publish = lambda _c, _u, _m, reason, _p: rejected.set() if reason.is_failure else None
    subscriber.on_disconnect = lambda *_: rejected.set()
    subscriber.connect("localhost", 8083)
    subscriber.loop_start()
    try:
        assert connection.wait(10) and codes == [0], f"MQTT viewer login failed: {codes}"
        assert sample.wait(10), "Viewer did not receive telemetry."
        subscriber.publish(prefix + "/_restriction_probe", "{}", qos=1)
        assert rejected.wait(10), "Broker did not reject viewer publishing."
    finally:
        subscriber.disconnect()
        subscriber.loop_stop()
    print("PASS: MQTT viewer receives telemetry and publishing is denied", flush=True)

    with httpx.Client(base_url="http://localhost:18083", timeout=10) as product:
        login = product.post("/api/v5/login", json={"username": "viewer", "password": "demo-viewer"})
        login.raise_for_status()
        product.headers["Authorization"] = "Bearer " + login.json()["token"]
        assert product.get("/api/v5/stats").status_code == 200
        assert product.post("/api/v5/users", json={}).status_code == 403
        for path in ("/api/v5/configs", "/api/v5/authentication", "/api/v5/listeners/ws%3Adefault/authentication"):
            no_credentials(product.get(path))
    for port in (8085, 8086, 8087):
        with httpx.Client(base_url=f"http://localhost:{port}", timeout=10) as product:
            login = product.post("/api/login", json={"name": "viewer", "pass": "demo-viewer"})
            login.raise_for_status()
            product.headers["Authorization"] = "Bearer " + login.json()["token"]
            assert product.get("/api/neuron/node?type=1").status_code == 200
            assert product.post("/api/neuron/node", json={}).status_code == 403
            no_credentials(product.get("/api/neuron/node/setting?node=uns-mqtt"))
    print("PASS: EMQX and all three Neuron viewers can read but cannot write", flush=True)

    assert httpx.post("http://localhost:8090/api/batch/nonexistent/abort").status_code == 403
    with psycopg.connect(host="localhost", dbname="demo", user="grafana_reader", password=env["GRAFANA_DB_PASSWORD"], autocommit=True) as db:
        assert db.execute("SELECT count(*) FROM batches").fetchone()[0] >= 0
        # Test actual privileges with read-only transaction defaults disabled.
        db.execute("SET default_transaction_read_only=off")
        try:
            db.execute("DELETE FROM batches WHERE false")
        except psycopg.errors.InsufficientPrivilege:
            pass
        else:
            raise AssertionError("Grafana's database account can write.")
    print("PASS: direct MES writes and Grafana database writes are denied", flush=True)

    def wait_for(predicate, description, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshots = [state(visitor) for visitor in clients]
            if predicate(snapshots):
                return snapshots
            time.sleep(0.4)
        raise AssertionError("Timed out: " + description)

    def device(client, name):
        return state(client)["visitor"]["devices"][name]

    def claim(client, name, expected=202):
        return post(client, f"/api/devices/{name}/claim", {"request_id": str(uuid.uuid4())}, expected)

    def command(client, name, action, expected=202, **values):
        return post(client, f"/api/devices/{name}/command", {"request_id": str(uuid.uuid4()),
            "lease_id": device(client, name)["lease_id"], "action": action, **values}, expected)

    def release(client, name):
        return post(client, f"/api/devices/{name}/release", {"lease_id": device(client, name)["lease_id"]}, 202)

    owner, next_owner, _, _, hvac_owner, batch_owner = clients
    try:
        claim(owner, "plc-1")
        snapshots = wait_for(lambda states: states[0]["visitor"]["devices"]["plc-1"]["owned"], "first CNC claim")
        lease = snapshots[0]["visitor"]["devices"]["plc-1"]["lease_id"]
        for other in clients[1:4]:
            assert not device(other, "plc-1")["lease_id"]
            claim(other, "plc-1")
        claim(hvac_owner, "plc-1", 429)
        claim(owner, "plc-2", 409)
        for suffix, body in [("release", {}), ("command", {"action": "rpm", "value": 6000, "request_id": str(uuid.uuid4())})]:
            # A queued visitor may cancel their own queue entry. Use a visitor
            # with no entry when probing a copied lease on the release route.
            probe = hvac_owner if suffix == "release" else next_owner
            post(probe, "/api/devices/plc-1/" + suffix, {**body, "lease_id": lease}, 403)
        command(owner, "plc-1", "rpm", value=2000)
        wait_for(lambda states: states[0]["assets"]["plc-1"]["values"].get("spindle_rpm") == 2000
                 and states[0]["visitor"]["devices"]["plc-1"]["command"]["status"] == "observed", "manual RPM write, acknowledgement and telemetry")
        print("PASS: per-device queue is bounded; copied leases are denied; real OPC UA RPM write is observed", flush=True)

        claim(hvac_owner, "hvac-1")
        wait_for(lambda states: states[4]["visitor"]["devices"]["hvac-1"]["owned"], "independent HVAC claim")
        command(hvac_owner, "hvac-1", "hvac", temperature=24, fan=70, mode=5, enabled=1)
        wait_for(lambda states: states[4]["visitor"]["devices"]["hvac-1"]["command"]["status"] == "observed", "BACnet setpoint write and telemetry")
        release(hvac_owner, "hvac-1")
        wait_for(lambda states: not states[4]["visitor"]["devices"]["hvac-1"]["busy"], "HVAC defaults restored")
        print("PASS: another browser controls HVAC independently; BACnet settings reset on release", flush=True)

        post(batch_owner, "/api/place-order", {"request_id": str(uuid.uuid4()), "plc_id": "plc-2", "recipe_id": "program-quick", "qty": 1}, 202)
        snapshots = wait_for(lambda states: states[5]["visitor"]["devices"]["plc-2"]["owned"], "ERP machine reservation")
        order_id = snapshots[5]["visitor"]["devices"]["plc-2"]["order_id"]
        wait_for(lambda states: any(o.get("order_id") == order_id and o.get("state") == "running" and o.get("plc_id") == "plc-2"
                                   for o in states[5]["orders"]), "MES batch allocated to reserved CNC 02")
        for other in clients[2:4]:
            release(other, "plc-1")
        release(owner, "plc-1")
        snapshots = wait_for(lambda states: states[1]["visitor"]["devices"]["plc-1"]["owned"], "FIFO handoff after spindle reset")
        assert snapshots[1]["assets"]["plc-1"]["values"]["spindle_rpm"] == 0
        post(owner, "/api/devices/plc-1/command", {"request_id": str(uuid.uuid4()), "action": "rpm", "value": 6000, "lease_id": lease}, 403)
        command(next_owner, "plc-1", "rpm", value=1500)
        wait_for(lambda states: states[1]["assets"]["plc-1"]["values"]["spindle_rpm"] == 1500, "next visitor's spindle command")
        print("PASS: MES uses the separately reserved CNC; release restores defaults before FIFO handoff", flush=True)

        for _ in range(6):
            command(hvac_owner, "hvac-1", "status")
        command(hvac_owner, "hvac-1", "status", expected=429)
        wait_for(lambda states: not states[1]["visitor"]["devices"]["plc-1"]["busy"]
                 and states[1]["assets"]["plc-1"]["values"]["spindle_rpm"] == 0, "30-second expiry stops spindle and releases device", timeout=40)
        wait_for(lambda states: any(o.get("order_id") == order_id and o.get("state") == "completed" for o in states[5]["orders"])
                 and not states[5]["visitor"]["devices"]["plc-2"]["busy"], "ERP batch completion and release", timeout=50)
        print("PASS: manual expiry stops the spindle; status requests are rate limited; ERP batch completes and releases its CNC", flush=True)
    finally:
        for visitor in clients:
            current = state(visitor)["visitor"]["devices"]
            for name, claim_state in current.items():
                if claim_state["owned"] or claim_state["position"]:
                    release(visitor, name)
        for visitor in clients:
            visitor.close()


if __name__ == "__main__":
    main()
