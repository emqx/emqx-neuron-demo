# /// script
# requires-python = ">=3.11"
# dependencies = ["flask==3.1.3", "paho-mqtt==2.1.0", "waitress==3.0.2", "PyJWT[crypto]==2.10.1"]
# ///
"""Operator console.

Routes:
    /          — live factory map, device claims, ERP orders and MQTT traffic
    /showcase  — alias for the interactive demo
    /tools     — product links and architecture
    /console   — compatibility redirect to the interactive demo
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
import hmac
import secrets
from pathlib import Path
import jwt
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
from urllib.parse import urlsplit, urlencode

from flask import Flask, jsonify, render_template, request, redirect, session, g
from werkzeug.security import check_password_hash
from waitress import serve
from showcase import Showcase
from visitors import Limiter, Refused
from control import DeviceControl
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
    controls.observe(msg.topic, body, retained=msg.retain)

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
if os.getenv("MQTT_USERNAME"):
    client.username_pw_set(os.environ["MQTT_USERNAME"], os.environ["MQTT_PASSWORD"])


def publish(topic, payload, qos=1, retain=False):
    if not client.is_connected():
        raise RuntimeError("The MQTT connection is unavailable.")
    info = client.publish(topic, payload, qos=qos, retain=retain)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError("MQTT did not accept the message.")
    return info


showcase = Showcase(publish, ENTERPRISE, SITE, AREA, LINE)
controls = DeviceControl(showcase, os.getenv("VISITOR_DB", ":memory:"),
                         seconds=int(os.getenv("CLAIM_SECONDS", "30")),
                         batch_seconds=int(os.getenv("BATCH_SECONDS", "90")),
                         capacity=int(os.getenv("DEVICE_QUEUE_SIZE", "3")))
http_limits = Limiter()
JWT_PRIVATE_KEY = Path(os.environ["MQTT_JWT_PRIVATE_KEY_FILE"]).read_text() if os.getenv("MQTT_JWT_PRIVATE_KEY_FILE") else None
client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
client.loop_start()


app = Flask(__name__)
PASSWORD_HASH = os.getenv("DEMO_PASSWORD_HASH", "")
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
if PASSWORD_HASH and (not os.getenv("DEMO_SESSION_SECRET") or not PUBLIC_BASE):
    raise RuntimeError("Hosted authentication needs DEMO_SESSION_SECRET and PUBLIC_BASE_URL.")
app.config.update(
    SECRET_KEY=os.getenv("DEMO_SESSION_SECRET") or secrets.token_hex(32),
    SESSION_COOKIE_NAME="factory_demo_session",
    SESSION_COOKIE_DOMAIN=os.getenv("DEMO_COOKIE_DOMAIN") or None,
    SESSION_COOKIE_SECURE=os.getenv("DEMO_COOKIE_SECURE", "1" if PUBLIC_BASE.startswith("https:") else "0") == "1",
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
    supplied = request.headers.get("X-Demo-Admin", "")
    admin_key = os.getenv("DEMO_ADMIN_KEY", "")
    g.admin = bool(admin_key and supplied and hmac.compare_digest(supplied, admin_key))
    if not session.get("visitor"):
        session["visitor"] = secrets.token_urlsafe(24)
        session["csrf"] = secrets.token_urlsafe(24)
        session.permanent = True
    if PASSWORD_HASH and not g.admin and request.endpoint not in ("login", "auth_check", "healthz") and not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify(error="Sign in to use the demo.", login_url=PUBLIC_BASE + "/login"), 401
        return login_redirect(request.url)
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify(error="Cross-origin actions are not allowed."), 403
        if request.path.startswith("/api/") and not request.is_json:
            return jsonify(error="Send a JSON request."), 415
        if request.path.startswith("/api/") and not g.admin:
            token = request.headers.get("X-Demo-CSRF", "")
            if not token or not hmac.compare_digest(token, session["csrf"]):
                return jsonify(error="Reload the page before using the controls."), 403
            http_limits.take(("write", request.remote_addr), 120, 60)
    if request.path.startswith("/api/"):
        http_limits.take(("read", request.remote_addr), 1200, 60)


@app.errorhandler(Refused)
def refused(error):
    response = jsonify(error=str(error))
    response.status_code = error.status
    if error.status == 429:
        response.headers["Retry-After"] = str(error.retry_after)
    return response


def visitor_state():
    return {**controls.snapshot(session["visitor"]), "csrf": session["csrf"]}


@app.post("/api/devices/<device>/claim")
def claim_device(device):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send a JSON object."), 400
    ticket = controls.join(session["visitor"], request.remote_addr, body.get("request_id"), device)
    return jsonify(status="queued", ticket_id=ticket), 202


@app.post("/api/devices/<device>/release")
def release_device(device):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send a JSON object."), 400
    controls.release(session["visitor"], device, body.get("lease_id"))
    return jsonify(status="releasing"), 202


@app.post("/api/devices/<device>/command")
def device_command(device):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send a JSON object."), 400
    try:
        rid = controls.command(session["visitor"], device, body)
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503
    result = {"status": "sent", "request_id": rid}
    if body.get("action") == "status":
        result["snapshot"] = controls.status_snapshot(device, rid)
    return jsonify(result), 202


@app.get("/api/mqtt-credentials")
def mqtt_credentials():
    http_limits.take(("mqtt", request.remote_addr), 60, 60)
    if not JWT_PRIVATE_KEY:
        return jsonify(error="MQTT viewer authentication is not configured."), 503
    slot = request.args.get("slot", "showcase")
    if slot not in ("showcase", "uns", "test"):
        return jsonify(error="Unknown subscriber."), 400
    client_id = "visitor-" + session["visitor"] + "-" + slot
    # A fixed client ID per visitor/view bounds connections and prevents the
    # token being used to impersonate an internal service or another visitor.
    suffixes = ("orders/#", "batches/#", "+/+/+/telemetry", "+/+/+/telemetry/+", "+/+/+/state",
                "+/+/+/state-events", "+/+/+/cmd/+", "+/+/+/control/#", "+/+/+/_demo/fault", "+/+/+/batch/+/phase/+/command")
    payload = {"username": "visitor", "clientid": client_id, "iat": int(time.time()), "exp": int(time.time()) + 300,
               "acl": [{"permission": "allow", "action": "subscribe", "topic": f"{ENTERPRISE}/{SITE}/{suffix}"}
                       for suffix in suffixes] + [{"permission": "deny", "action": "all", "topic": "#"}]}
    token = jwt.encode(payload, JWT_PRIVATE_KEY, algorithm="RS256")
    return jsonify(clientId=client_id, username="visitor", password=token, expires=payload["exp"])


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
            for old in [key for key, attempts in login_attempts.items() if not attempts or now - attempts[-1] > 60]:
                login_attempts.pop(old, None)
            if key not in login_attempts and len(login_attempts) >= 5000:
                return render_template("login.html", target=target, error="Please try again in a minute."), 429
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
            session["visitor"] = secrets.token_urlsafe(24)
            session["csrf"] = secrets.token_urlsafe(24)
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
        "title": os.getenv("SHOWCASE_TITLE", "From shop floor to cloud."),
        "event": os.getenv("SHOWCASE_EVENT", "Live manufacturing demonstration"),
    }}


@app.get("/showcase")
def showcase_page():
    return render_template("showcase.html")


@app.get("/api/showcase")
def showcase_state():
    with showcase.lock:
        recent_orders = sorted(showcase.orders.values(), key=lambda order: order.get("ts", ""))[-12:]
    return jsonify(**showcase.snapshot(), orders=recent_orders, visitor=visitor_state())


@app.get("/readyz")
def readyz():
    state = showcase.snapshot()
    return jsonify(ready=state["ready"], connected=state["connected"]), 200 if state["ready"] else 503


@app.post("/api/demo/start")
def demo_start():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("loop", False), bool):
        return jsonify(error="loop must be true or false."), 400
    if not g.admin:
        return jsonify(error="Use device claims or the ERP order button."), 403
    if controls.active or any(controls.waiting.values()):
        return jsonify(error="Wait for device claims to finish before starting an operator loop."), 409
    try:
        showcase.start(loop=body.get("loop", False))
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(status="started"), 202


@app.post("/api/demo/reset")
def demo_reset():
    if g.admin:
        with controls.lock:
            for ticket in controls.active.values():
                ticket.update(stage="recovering", pending=None)
            controls.save()
        showcase.reset()
    else:
        return jsonify(error="Release your device using its claim controls."), 403
    return jsonify(status="resetting"), 202


@app.post("/api/demo/fault")
def demo_fault():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send an action."), 400
    try:
        if g.admin:
            showcase.fault(body.get("action"))
        else:
            return jsonify(error="Use the controls on your reserved device."), 403
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
    plc_list = sorted(showcase.snapshot()["plcs"].values(), key=lambda c: c.get("plc_id", ""))
    return jsonify({
        "enterprise": ENTERPRISE,
        "site": SITE,
        "area": AREA,
        "recipes": RECIPES,
        "orders": ords,
        "plcs": plc_list,
        "visitor": visitor_state(),
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
    if not g.admin:
        return jsonify(error="Use the controls on your reserved device."), 403
    topic = f"{ENTERPRISE}/{SITE}/{AREA}/{LINE}/{plc_id}/_demo/fault"
    payload = {"action": action, "type": ftype, "ts": utc_now()}
    try:
        publish(topic, json.dumps(payload))
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503
    return jsonify({"status": "ok", "topic": topic, "action": action})


@app.post("/api/place-order")
def api_place_order():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send a JSON object."), 400
    recipe = body.get("recipe_id")
    qty = body.get("qty", 1)
    if not isinstance(qty, int) or isinstance(qty, bool):
        return jsonify(error="qty must be an integer."), 400
    if recipe not in RECIPES:
        return jsonify({"error": "unknown recipe"}), 400
    if qty != 1:
        return jsonify({"error": "Each demo order makes one part."}), 400

    ticket = controls.join(session["visitor"], request.remote_addr, body.get("request_id"), body.get("plc_id"), recipe)
    return jsonify(status="queued", ticket_id=ticket), 202


@app.post("/api/command")
def api_command():
    return jsonify(error="Use the controls on your reserved device."), 403


@app.get("/console")
def console():
    return redirect("/showcase")


@app.get("/uns")
def uns():
    return render_template("uns.html")


@app.get("/")
def index():
    return render_template("index.html" if os.getenv("SHOWCASE_HOME") == "0" else "showcase.html")


@app.get("/tools")
def tools_page():
    return render_template("index.html")


def main() -> None:
    controls.start()
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
