# /// script
# requires-python = ">=3.11"
# dependencies = ["flask==3.0.3", "paho-mqtt==2.1.0"]
# ///
"""Operator console.

Routes:
    /          — landing page: what the demo is about + architecture
    /console   — ERP panel + PLCs table + recent orders + fault injection
    /uns       — live MQTT-topic tree (connects to EMQX WS directly)
    /api/*     — REST endpoints used by the console JS

The MES web UI (separate service on :8090) shows batch-execution detail.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request
import paho.mqtt.client as mqtt


ENTERPRISE = os.getenv("ENTERPRISE", "acme-mfg")
SITE       = os.getenv("SITE", "plant-1")
AREA       = os.getenv("AREA", "machining")
LINE       = os.getenv("LINE", "line-1")
MQTT_HOST  = os.getenv("MQTT_HOST", "emqx")
MQTT_PORT  = int(os.getenv("MQTT_PORT", "1883"))
PORT       = int(os.getenv("PORT", "8080"))


# Keep RECIPES in sync with services/mes-sim/mes.py:RECIPES.
RECIPES = ["program-a", "program-quick"]


ORDER_STATUS_RE = re.compile(
    rf"^{re.escape(ENTERPRISE)}/{re.escape(SITE)}/orders/([^/]+)/status$"
)
PLC_STATE_RE    = re.compile(
    rf"^{re.escape(ENTERPRISE)}/{re.escape(SITE)}/{re.escape(AREA)}/"
    rf"([^/]+)/([^/]+)/state$"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# In-memory state, populated via retained-topic subscriptions.
state_lock = threading.Lock()
orders: dict[str, dict] = {}     # order_id -> latest status payload
plcs: dict[str, dict] = {}       # plc_id -> latest state payload


def on_connect(client, _u, _f, reason_code, _p):
    if reason_code == 0:
        client.subscribe(f"{ENTERPRISE}/{SITE}/orders/+/status", qos=1)
        client.subscribe(
            f"{ENTERPRISE}/{SITE}/{AREA}/+/+/state", qos=1
        )
        print(f"[{utc_now()}] MQTT connected; subs: orders status + PLC state",
              flush=True)
    else:
        print(f"[{utc_now()}] MQTT connect failed: {reason_code}", flush=True)


def on_message(_c, _u, msg):
    try:
        body = json.loads(msg.payload.decode("utf-8")) if msg.payload else {}
    except json.JSONDecodeError:
        return

    m = ORDER_STATUS_RE.match(msg.topic)
    if m:
        order_id = m.group(1)
        with state_lock:
            orders[order_id] = body
        return

    m = PLC_STATE_RE.match(msg.topic)
    if m:
        plc_id = m.group(2)
        with state_lock:
            if not body:
                # Tombstone (retained-clear). Drop the entry so the UI hides
                # the PLC rather than rendering an empty row.
                plcs.pop(plc_id, None)
            else:
                # Make sure plc_id is always present even if the publisher
                # omitted it from the payload.
                body["plc_id"] = body.get("plc_id") or plc_id
                plcs[plc_id] = body
        return


# Single shared MQTT client.
client = mqtt.Client(
    client_id=f"operator-{SITE}",
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
client.loop_start()


app = Flask(__name__)


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/api/state")
def api_state():
    with state_lock:
        ords = sorted(
            orders.values(),
            key=lambda o: o.get("ts", ""),
            reverse=True,
        )
        plc_list = sorted(plcs.values(), key=lambda c: c.get("plc_id", ""))
    return jsonify({
        "enterprise": ENTERPRISE,
        "site": SITE,
        "area": AREA,
        "recipes": RECIPES,
        "orders": ords,
        "plcs": plc_list,
    })


@app.post("/api/fault")
def api_fault():
    """Inject or clear a fault on a PLC. Used for the fault-path demo step."""
    body = request.get_json(silent=True) or {}
    plc_id = body.get("plc_id")
    action = body.get("action")
    ftype = body.get("type", "over_temp")
    if action not in ("inject", "clear"):
        return jsonify({"error": "action must be 'inject' or 'clear'"}), 400
    if not plc_id:
        return jsonify({"error": "plc_id required"}), 400
    topic = f"{ENTERPRISE}/{SITE}/{AREA}/{LINE}/{plc_id}/_demo/fault"
    payload = {"action": action, "type": ftype, "ts": utc_now()}
    client.publish(topic, json.dumps(payload), qos=1)
    return jsonify({"status": "ok", "topic": topic, "action": action})


@app.post("/api/place-order")
def api_place_order():
    body = request.get_json(silent=True) or {}
    recipe = body.get("recipe_id")
    qty = int(body.get("qty", 1))
    if recipe not in RECIPES:
        return jsonify({"error": "unknown recipe"}), 400
    if qty < 1 or qty > 100:
        return jsonify({"error": "qty out of range"}), 400

    order_id = f"ORD-{int(time.time())}-{uuid.uuid4().hex[:4]}"
    payload = {
        "order_id":  order_id,
        "recipe_id": recipe,
        "qty":       qty,
        "placed_at": utc_now(),
    }
    topic = f"{ENTERPRISE}/{SITE}/orders/{order_id}"
    client.publish(topic, json.dumps(payload), qos=1, retain=True)
    return jsonify({"status": "ok", "order_id": order_id, "topic": topic})


@app.get("/console")
def console():
    return render_template("console.html")


@app.get("/uns")
def uns():
    return render_template("uns.html")


@app.get("/")
def index():
    return render_template("index.html")


def main() -> None:
    print(f"[{utc_now()}] Operator console listening on :{PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
