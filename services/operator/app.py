# /// script
# requires-python = ">=3.11"
# dependencies = ["flask==3.1.3", "paho-mqtt==2.1.0", "waitress==3.0.2"]
# ///
"""Operator console.

Routes:
    /          — landing page: what the demo is about + architecture
    /showcase  — guided booth walkthrough + separate MQTT dashboard
    /tools     — product links and architecture
    /console   — ERP panel + PLCs table + recent orders + fault injection
    /uns       — live MQTT-topic tree (connects to EMQX WS directly)
    /api/*     — operator and showcase controls
    /login     — optional hosted session login
    /auth/check — gateway authentication for HTTP and WebSocket handshakes

The MES web UI (separate service on :8090) shows batch-execution detail.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
from urllib.parse import urlsplit, urlencode

from flask import Flask, jsonify, render_template, request, redirect, session
from werkzeug.security import check_password_hash
from waitress import serve
from showcase import Showcase
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
    showcase.connection(reason_code == 0)
    if reason_code == 0:
        client.subscribe(f"{ENTERPRISE}/{SITE}/#", qos=1)
        print(f"[{utc_now()}] MQTT connected; subs: orders status + PLC state",
              flush=True)
    else:
        print(f"[{utc_now()}] MQTT connect failed: {reason_code}", flush=True)


def on_message(_c, _u, msg):
    try:
        body = json.loads(msg.payload.decode("utf-8")) if msg.payload else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return

    if not isinstance(body, dict):
        return
    showcase.observe(msg.topic, body, retained=msg.retain)

    m = ORDER_STATUS_RE.match(msg.topic)
    if m:
        order_id = m.group(1)
        with state_lock:
            if body:
                orders[order_id] = body
                if len(orders) > 100:
                    oldest = next(iter(orders))
                    orders.pop(oldest, None)
            else:
                orders.pop(order_id, None)
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
client.on_disconnect = lambda *_: showcase.connection(False)
client.reconnect_delay_set(min_delay=1, max_delay=5)


def publish(topic, payload, qos=1, retain=False):
    if not client.is_connected():
        raise RuntimeError("The MQTT connection is unavailable.")
    info = client.publish(topic, payload, qos=qos, retain=retain)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError("MQTT did not accept the message.")
    return info


showcase = Showcase(publish, ENTERPRISE, SITE, AREA, LINE)
client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
client.loop_start()


app = Flask(__name__)
PASSWORD_HASH = os.getenv("DEMO_PASSWORD_HASH", "")
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
if PASSWORD_HASH and (not os.getenv("DEMO_SESSION_SECRET") or not PUBLIC_BASE):
    raise RuntimeError("Hosted authentication needs DEMO_SESSION_SECRET and PUBLIC_BASE_URL.")
app.config.update(
    SECRET_KEY=os.getenv("DEMO_SESSION_SECRET"),
    SESSION_COOKIE_NAME="factory_demo_session",
    SESSION_COOKIE_DOMAIN=os.getenv("DEMO_COOKIE_DOMAIN") or None,
    SESSION_COOKIE_SECURE=os.getenv("DEMO_COOKIE_SECURE", "1") == "1",
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=16), MAX_CONTENT_LENGTH=16384,
)
login_attempts = defaultdict(lambda: deque(maxlen=5))
login_lock = threading.Lock()


def links():
    host = urlsplit(request.host_url).hostname or "localhost"
    host = f"[{host}]" if ":" in host else host
    scheme = request.scheme
    defaults = {"mes": f"{scheme}://{host}:8090/", "grafana": f"{scheme}://{host}:3000/",
                "emqx": f"{scheme}://{host}:18083/",
                **{f"neuron_{i}": f"{scheme}://{host}:{8084+i}/" for i in range(1, 4)}}
    return {key: os.getenv(key.upper() + "_PUBLIC_URL", value).rstrip("/") + "/"
            for key, value in defaults.items()}


def safe_next(value):
    if not value or "\\" in value or any(ord(c) < 32 for c in value):
        return PUBLIC_BASE + "/showcase"
    if value.startswith("/") and not value.startswith("//"):
        return (PUBLIC_BASE or "") + value
    allowed = {urlsplit(PUBLIC_BASE).netloc, *(urlsplit(v).netloc for v in links().values())}
    try:
        parsed = urlsplit(value)
    except ValueError:
        return PUBLIC_BASE + "/showcase"
    if parsed.scheme == urlsplit(PUBLIC_BASE).scheme and parsed.netloc in allowed and not parsed.username:
        return value
    return PUBLIC_BASE + "/showcase"


def login_redirect(target):
    return redirect(PUBLIC_BASE + "/login?" + urlencode({"next": safe_next(target)}))


@app.before_request
def protect():
    if PASSWORD_HASH and request.endpoint not in ("login", "auth_check", "healthz") and not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify(error="Sign in to use the demo.", login_url=PUBLIC_BASE + "/login"), 401
        return login_redirect(request.url)
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify(error="Cross-origin actions are not allowed."), 403
        if request.path.startswith("/api/") and not request.is_json:
            return jsonify(error="Send a JSON request."), 415


@app.after_request
def response_headers(response):
    if request.path.startswith("/api/") or request.path in ("/login", "/auth/check"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if not PASSWORD_HASH:
        return redirect("/showcase")
    target = safe_next(request.values.get("next"))
    error = None
    status = 200
    if request.method == "POST":
        key = request.remote_addr
        now = time.monotonic()
        with login_lock:
            recent = login_attempts[key]
            while recent and now - recent[0] > 60:
                recent.popleft()
            limited = len(recent) >= 5
            if not limited:
                recent.append(now)
        if limited:
            error, status = "Please wait a minute before trying again.", 429
        elif check_password_hash(PASSWORD_HASH, request.form.get("password", "")):
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            with login_lock:
                login_attempts.pop(key, None)
            return redirect(target)
        else:
            error, status = "That password did not match.", 401
    return render_template("login.html", target=target, error=error), status


@app.get("/auth/check")
def auth_check():
    # Used by Caddy for HTTP and WebSocket routes, including the product UIs.
    # Cookie auth leaves each product's Authorization header intact.
    if not PASSWORD_HASH or session.get("authenticated"):
        return "", 204
    host = request.headers.get("X-Forwarded-Host", request.host)
    uri = request.headers.get("X-Forwarded-Uri", "/showcase")
    scheme = urlsplit(PUBLIC_BASE).scheme or request.scheme
    return login_redirect(f"{scheme}://{host}{uri}")


@app.context_processor
def template_config():
    return {"links": links(), "demo_config": {
        "enterprise": ENTERPRISE, "site": SITE,
        "prefix": f"{ENTERPRISE}/{SITE}",
        "machinePrefix": f"{ENTERPRISE}/{SITE}/{AREA}/{LINE}",
        "mqttUrl": os.getenv("MQTT_PUBLIC_URL", ""),
        "mqttProxy": os.getenv("MQTT_PROXY", "0") == "1",
        "title": os.getenv("SHOWCASE_TITLE", "The Factory Data Connection Foundation"),
        "event": os.getenv("SHOWCASE_EVENT", "Live manufacturing demonstration"),
    }}


@app.get("/showcase")
def showcase_page():
    return render_template("showcase.html")


@app.get("/api/showcase")
def showcase_state():
    return jsonify(showcase.snapshot())


@app.get("/readyz")
def readyz():
    state = showcase.snapshot()
    return jsonify(ready=state["ready"], connected=state["connected"]), 200 if state["ready"] else 503


@app.post("/api/demo/start")
def demo_start():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("loop", False), bool):
        return jsonify(error="loop must be true or false."), 400
    try:
        showcase.start(loop=body.get("loop", False))
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(status="started"), 202


@app.post("/api/demo/reset")
def demo_reset():
    showcase.reset()
    return jsonify(status="resetting"), 202


@app.post("/api/demo/fault")
def demo_fault():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send an action."), 400
    try:
        showcase.fault(body.get("action"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503
    return jsonify(status="sent"), 202


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
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send a JSON object."), 400
    plc_id = body.get("plc_id")
    action = body.get("action")
    ftype = body.get("type", "over_temp")
    if action not in ("inject", "clear"):
        return jsonify({"error": "action must be 'inject' or 'clear'"}), 400
    if plc_id not in ("plc-1", "plc-2"):
        return jsonify({"error": "unknown plc_id"}), 400
    topic = f"{ENTERPRISE}/{SITE}/{AREA}/{LINE}/{plc_id}/_demo/fault"
    payload = {"action": action, "type": ftype, "ts": utc_now()}
    try:
        publish(topic, json.dumps(payload))
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503
    return jsonify({"status": "ok", "topic": topic, "action": action})


@app.post("/api/place-order")
def api_place_order():
    if showcase.snapshot()["scene"]["active"]:
        return jsonify(error="Reset the guided walkthrough before placing a manual order."), 409
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send a JSON object."), 400
    recipe = body.get("recipe_id")
    qty = body.get("qty", 1)
    if not isinstance(qty, int) or isinstance(qty, bool):
        return jsonify(error="qty must be an integer."), 400
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
    try:
        publish(topic, json.dumps(payload), retain=True)
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503
    return jsonify({"status": "ok", "order_id": order_id, "topic": topic})


@app.get("/console")
def console():
    return render_template("console.html")


@app.get("/uns")
def uns():
    return render_template("uns.html")


@app.get("/")
def index():
    if os.getenv("SHOWCASE_HOME") == "1":
        return redirect("/showcase")
    return render_template("index.html")


@app.get("/tools")
def tools_page():
    return render_template("index.html")


def main() -> None:
    print(f"[{utc_now()}] Operator console listening on :{PORT}", flush=True)
    # Only the hosted profile trusts proxy headers. Its app port is private
    # (Docker network + loopback); Caddy is the only public entry point.
    proxy_options = {}
    if os.getenv("TRUST_PROXY") == "1":
        proxy_options = {
            "trusted_proxy": "*", "trusted_proxy_count": 1,
            "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
        }
    serve(app, host="0.0.0.0", port=PORT, threads=8, **proxy_options)


if __name__ == "__main__":
    main()
