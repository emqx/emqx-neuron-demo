# /// script
# requires-python = ">=3.11"
# dependencies = ["paho-mqtt==2.1.0", "flask==3.0.3"]
# ///
"""MES simulator.

For each order on <ent>/<site>/orders/<order_id>:
  1. Look up recipe (static dict below).
  2. Allocate an idle PLC.
  3. For each phase in recipe: publish phase command, wait for state=completed.
  4. Build batch record, publish to <ent>/<site>/batches/<batch_id>/record.
  5. Update order status.

v1: HOLD / RESUME / ABORT and fault paths are stubs — happy path only.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from flask import Flask, jsonify
import paho.mqtt.client as mqtt


ENTERPRISE  = os.getenv("ENTERPRISE", "acme-mfg")
SITE        = os.getenv("SITE", "plant-1")
AREA        = os.getenv("AREA", "machining")
LINE        = os.getenv("LINE", "line-1")
MQTT_HOST   = os.getenv("MQTT_HOST", "emqx")
MQTT_PORT   = int(os.getenv("MQTT_PORT", "1883"))
PORT        = int(os.getenv("PORT", "8090"))
PHASE_TIMEOUT_S = float(os.getenv("PHASE_TIMEOUT_S", "120"))


ORDERS_PATTERN  = f"{ENTERPRISE}/{SITE}/orders/+"
ORDER_TOPIC_RE  = re.compile(
    rf"^{re.escape(ENTERPRISE)}/{re.escape(SITE)}/orders/([^/]+)$"
)
EVENTS_RE       = re.compile(
    rf"^{re.escape(ENTERPRISE)}/{re.escape(SITE)}/{re.escape(AREA)}/"
    rf"([^/]+)/([^/]+)/state-events$"
)


# Static recipe library. Phase names must match plc.PHASES.
RECIPES: dict[str, list[str]] = {
    "program-a":     ["load", "rough", "finish", "unload"],
    "program-quick": ["load", "rough", "unload"],
}

PLCS = [{"line": LINE, "id": f"plc-{i}"} for i in range(1, 3)]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PhaseEvent:
    phase: str
    state: str
    ts: str


@dataclass
class Batch:
    batch_id:   str
    order_id:   str
    recipe_id:  str
    plc_id:     str
    line:       str
    part_serial: str
    state:      str = "pending"      # pending | running | completed | failed
    current_phase: str | None = None
    current_phase_state: str | None = None  # running | holding | faulted (live)
    fault_reason: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    fail_reason: str | None = None
    events:     list[PhaseEvent] = field(default_factory=list)


# In-memory state. All access guarded by `lock`.
lock = threading.Lock()
batches: dict[str, Batch] = {}                        # batch_id -> Batch
orders_in_flight: set[str] = set()                    # order_ids actively running or queued
order_queue: "queue.Queue[dict]" = queue.Queue()      # raw order payloads
phase_events: dict[tuple[str, str], threading.Event] = {}  # (batch_id, phase) -> Event
phase_results: dict[tuple[str, str], str] = {}        # (batch_id, phase) -> last state seen
batch_counter = 0
part_counter = 0


def next_batch_id() -> str:
    global batch_counter
    batch_counter += 1
    return f"B-{batch_counter:04d}"


def next_part_serial() -> str:
    global part_counter
    part_counter += 1
    return f"PART-{int(time.time()):010d}-{part_counter:04d}"


def allocate_plc() -> dict | None:
    """Return the first PLC not currently running a batch."""
    busy = {b.plc_id for b in batches.values() if b.state == "running"}
    for plc in PLCS:
        if plc["id"] not in busy:
            return plc
    return None


def publish_order_status(client: mqtt.Client, order_id: str, **payload) -> None:
    topic = f"{ENTERPRISE}/{SITE}/orders/{order_id}/status"
    body = {"order_id": order_id, "ts": utc_now(), **payload}
    client.publish(topic, json.dumps(body), qos=1, retain=True)


def clear_retained_order(client: mqtt.Client, order_id: str) -> None:
    """Publish empty payload to clear the retained order. Call when a batch
    reaches a terminal state — otherwise restarting MES would re-process the
    retained order. Status remains on orders/<id>/status (retained, separate)."""
    client.publish(f"{ENTERPRISE}/{SITE}/orders/{order_id}", payload=b"",
                   qos=1, retain=True)


def publish_phase_command(client: mqtt.Client, batch: Batch, phase: str, action: str) -> None:
    topic = (f"{ENTERPRISE}/{SITE}/{AREA}/{batch.line}/{batch.plc_id}"
             f"/batch/{batch.batch_id}/phase/{phase}/command")
    body = {"action": action, "batch_id": batch.batch_id, "phase": phase,
            "plc_id": batch.plc_id, "ts": utc_now()}
    client.publish(topic, json.dumps(body), qos=2)


def publish_batch_record(client: mqtt.Client, batch: Batch) -> None:
    topic = f"{ENTERPRISE}/{SITE}/batches/{batch.batch_id}/record"
    payload = {
        **asdict(batch),
        "events": [asdict(e) for e in batch.events],
    }
    client.publish(topic, json.dumps(payload), qos=1, retain=True)


def on_connect(client, _u, _f, reason_code, _p):
    if reason_code == 0:
        client.subscribe(ORDERS_PATTERN, qos=1)
        client.subscribe(
            f"{ENTERPRISE}/{SITE}/{AREA}/+/+/state-events", qos=1)
        print(f"[{utc_now()}] MQTT connected; subs: orders + PLC state-events",
              flush=True)
    else:
        print(f"[{utc_now()}] MQTT connect failed: {reason_code}", flush=True)


def on_message(_c, _u, msg):
    # Order intake.
    m = ORDER_TOPIC_RE.match(msg.topic)
    if m and "/status" not in msg.topic:
        order_id = m.group(1)
        try:
            order = json.loads(msg.payload.decode("utf-8")) if msg.payload else {}
        except json.JSONDecodeError:
            return
        if not order:
            return  # tombstone / retained-clear
        order["order_id"] = order.get("order_id", order_id)
        with lock:
            if order_id in orders_in_flight:
                return
            orders_in_flight.add(order_id)
        order_queue.put(order)
        print(f"[{utc_now()}] order received: {order_id} {order.get('recipe_id')}",
              flush=True)
        return

    # PLC state-events. Each message corresponds to a single PLC
    # transition: Neuron's events driver subscribes to the PLC's
    # phase_event_json OPC UA tag, and the PLC only writes that tag
    # when state actually changes. The EMQX Neuron-shaped outer payload
    # carries the PLC's snapshot as a JSON string, which we parse.
    m = EVENTS_RE.match(msg.topic)
    if m:
        try:
            outer = json.loads(msg.payload.decode("utf-8")) if msg.payload else {}
        except json.JSONDecodeError:
            return
        snapshot = outer.get("values", {}).get("phase_event_json") or ""
        if not snapshot:
            return
        try:
            evt_payload = json.loads(snapshot)
        except json.JSONDecodeError:
            return
        batch_id = evt_payload.get("batch_id") or ""
        phase    = evt_payload.get("phase") or ""
        state    = evt_payload.get("state")
        reason   = evt_payload.get("reason") or None
        if not (batch_id and phase and state):
            return
        with lock:
            phase_results[(batch_id, phase)] = state
            evt = phase_events.get((batch_id, phase))
            b = batches.get(batch_id)
            if b:
                b.events.append(PhaseEvent(phase=phase, state=state, ts=utc_now()))
                if state in ("running", "holding", "faulted"):
                    b.current_phase_state = state
                    b.fault_reason = reason if state == "faulted" else None
        # Terminal states wake the run_batch waiter; holding/faulted/running
        # are non-terminal — operator must explicitly abort to fail a batch.
        if evt and state in ("completed", "aborted"):
            evt.set()


def run_batch(client: mqtt.Client, order: dict) -> None:
    global batch_counter, part_counter
    order_id = order["order_id"]
    recipe_id = order.get("recipe_id")

    if recipe_id not in RECIPES:
        publish_order_status(client, order_id, state="failed",
                             reason=f"unknown recipe {recipe_id!r}")
        clear_retained_order(client, order_id)
        with lock:
            orders_in_flight.discard(order_id)
        return

    # Allocate PLC + create batch atomically (avoids TOCTOU between
    # parallel run_batch threads — without this, two orders could both
    # see the same PLC as idle).
    while True:
        with lock:
            plc = allocate_plc()
            if plc is not None:
                batch_counter += 1
                batch_id = f"B-{batch_counter:04d}"
                part_counter += 1
                part_serial = f"PART-{int(time.time()):010d}-{part_counter:04d}"
                batch = Batch(
                    batch_id=batch_id,
                    order_id=order_id,
                    recipe_id=recipe_id,
                    plc_id=plc["id"],
                    line=plc["line"],
                    part_serial=part_serial,
                    state="running",
                    started_at=utc_now(),
                )
                batches[batch_id] = batch
                break
        time.sleep(0.5)

    publish_order_status(client, order_id, state="running", batch_id=batch_id,
                         part_serial=part_serial, plc_id=plc["id"])
    publish_batch_record(client, batch)
    print(f"[{utc_now()}] start batch {batch_id} order={order_id} "
          f"recipe={recipe_id} plc={plc['id']} part={part_serial}", flush=True)

    for phase in RECIPES[recipe_id]:
        evt = threading.Event()
        with lock:
            phase_events[(batch_id, phase)] = evt
            batch.current_phase = phase
        publish_batch_record(client, batch)
        publish_phase_command(client, batch, phase, "start")
        if not evt.wait(timeout=PHASE_TIMEOUT_S):
            with lock:
                batch.state = "failed"
                batch.fail_reason = f"phase {phase} timed out"
                batch.current_phase_state = None
                batch.completed_at = utc_now()
            publish_batch_record(client, batch)
            publish_order_status(client, order_id, state="failed", batch_id=batch_id,
                                 reason=batch.fail_reason)
            clear_retained_order(client, order_id)
            with lock:
                orders_in_flight.discard(order_id)
            return
        with lock:
            result = phase_results.get((batch_id, phase))
        if result != "completed":
            with lock:
                batch.state = "failed"
                batch.fail_reason = f"phase {phase} ended in state {result!r}"
                batch.current_phase_state = None
                batch.completed_at = utc_now()
            publish_batch_record(client, batch)
            publish_order_status(client, order_id, state="failed", batch_id=batch_id,
                                 reason=batch.fail_reason)
            clear_retained_order(client, order_id)
            with lock:
                orders_in_flight.discard(order_id)
            return

    with lock:
        batch.state = "completed"
        batch.current_phase = None
        batch.current_phase_state = None
        batch.completed_at = utc_now()
    publish_batch_record(client, batch)
    publish_order_status(client, order_id, state="completed", batch_id=batch_id,
                         part_serial=part_serial)
    clear_retained_order(client, order_id)
    with lock:
        orders_in_flight.discard(order_id)
    print(f"[{utc_now()}] done batch {batch_id} part={part_serial}", flush=True)


def dispatcher(client: mqtt.Client) -> None:
    """One thread per order — multiple batches run concurrently across PLCs."""
    while True:
        order = order_queue.get()
        threading.Thread(
            target=_safe_run, args=(client, order), daemon=True,
            name=f"batch-{order.get('order_id', '?')}",
        ).start()


def _safe_run(client: mqtt.Client, order: dict) -> None:
    try:
        run_batch(client, order)
    except Exception as e:
        print(f"[{utc_now()}] batch failed: {e}", flush=True)


# ── HTTP API ────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/api/batches")
def api_batches():
    with lock:
        items = sorted(
            (asdict(b) for b in batches.values()),
            key=lambda b: b["batch_id"], reverse=True,
        )
    return jsonify({"batches": items})


@app.get("/api/recipes")
def api_recipes():
    return jsonify({"recipes": RECIPES})


def _action_on_running_batch(batch_id: str, action: str):
    """Send a phase command (hold/resume/abort) on the batch's current phase."""
    with lock:
        b = batches.get(batch_id)
    if b is None:
        return jsonify({"error": "no such batch"}), 404
    if b.state != "running":
        return jsonify({"error": f"batch not running (state={b.state})"}), 400
    if not b.current_phase:
        return jsonify({"error": "batch has no current phase"}), 400
    publish_phase_command(_mqtt, b, b.current_phase, action)
    return jsonify({"status": "ok", "batch_id": batch_id,
                    "phase": b.current_phase, "action": action})


@app.post("/api/batch/<batch_id>/hold")
def api_batch_hold(batch_id):
    return _action_on_running_batch(batch_id, "hold")


@app.post("/api/batch/<batch_id>/resume")
def api_batch_resume(batch_id):
    return _action_on_running_batch(batch_id, "resume")


@app.post("/api/batch/<batch_id>/abort")
def api_batch_abort(batch_id):
    return _action_on_running_batch(batch_id, "abort")


INDEX_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>MES · Batches</title>
  <style>
    :root { --bg:#f3f5f7; --card:#ffffff; --line:#d8dde3; --muted:#5a6473;
            --fg:#1a2330; --accent:#2563eb; --good:#16a34a;
            --warn:#d97706; --bad:#dc2626; }
    * { box-sizing: border-box; }
    body { background:var(--bg); color:var(--fg); margin:0;
           font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
    header { padding:14px 24px; border-bottom:1px solid var(--line);
             background:var(--card);
             display:flex; justify-content:space-between; align-items:center; }
    h1 { font-size:14px; letter-spacing:.18em; margin:0; font-weight:600; }
    .right { font-size:11px; color:var(--muted); letter-spacing:.1em; }
    main { padding:18px 24px; max-width:1280px; margin:0 auto; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:6px;
            padding:14px 18px; margin-bottom:14px; }
    .card h2 { font-size:11px; letter-spacing:.18em; text-transform:uppercase;
               color:var(--muted); margin:0 0 12px; font-weight:600; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th { text-align:left; padding:8px 10px; color:var(--muted);
         font-weight:600; border-bottom:1px solid var(--line);
         font-size:11px; letter-spacing:.06em; text-transform:uppercase; }
    td { padding:8px 10px; border-bottom:1px solid var(--line); font-family:
         ui-monospace,Menlo,monospace; font-size:12px; }
    tr:last-child td { border-bottom:none; }
    .pill { display:inline-block; padding:2px 8px; border-radius:10px;
            font-size:10px; letter-spacing:.08em; text-transform:uppercase;
            font-weight:600; }
    .running   { background:rgba(37,99,235,.12);  color:var(--accent); }
    .pending   { background:rgba(90,100,115,.12); color:var(--muted); }
    .completed { background:rgba(22,163,74,.12);  color:var(--good); }
    .failed    { background:rgba(220,38,38,.12);  color:var(--bad); }
    .aborted   { background:rgba(217,119,6,.14);  color:var(--warn); }
    .holding   { background:rgba(217,119,6,.14);  color:var(--warn); }
    .faulted   { background:rgba(220,38,38,.12);  color:var(--bad); }
    .empty { color:var(--muted); padding:18px 0; text-align:center; }
    button.action {
      font-family:inherit; font-size:11px; padding:3px 8px;
      border-radius:3px; border:1px solid var(--line);
      background:var(--card); color:var(--fg); cursor:pointer; margin-right:4px;
    }
    button.action:hover { background:#eef1f5; }
    button.action.danger { color:var(--bad); border-color:rgba(220,38,38,.4); }
    button.action:disabled { opacity:.4; cursor:not-allowed; }
  </style>
</head>
<body>
  <header>
    <h1>MES · BATCH EXECUTION</h1>
    <span class="right">__SITE_LABEL__ · __LINE_LABEL__</span>
  </header>
  <main>
    <div class="card">
      <h2>Batches in flight &amp; history</h2>
      <table id="batches"><thead><tr>
        <th>Batch</th><th>Order</th><th>Recipe</th><th>PLC</th><th>Part</th>
        <th>Phase</th><th>State</th><th>Started</th><th>Actions</th>
      </tr></thead><tbody></tbody></table>
      <div id="empty" class="empty" style="display:none;">No batches yet</div>
    </div>
    <div class="card">
      <h2>Last events</h2>
      <table id="events"><thead><tr>
        <th>Batch</th><th>Phase</th><th>State</th><th>When</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </main>
<script>
function fmt(s) {
  if (s == null || s === '') return '';
  const n = (typeof s === 'number') ? s
          : (typeof s === 'string' && /^\\d+$/.test(s)) ? parseInt(s, 10)
          : null;
  const d = (n != null) ? new Date(n) : new Date(s);
  return isNaN(d.getTime()) ? '' : d.toLocaleTimeString();
}

function el(tag, props, children) {
  const node = document.createElement(tag);
  if (props) for (const [k, v] of Object.entries(props)) {
    if (k === 'className') node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of (children || [])) {
    if (c == null || c === '') continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

function pill(state) {
  return el('span', {className: 'pill ' + (state || 'pending')}, [state || '—']);
}

async function batchAction(batchId, action) {
  await fetch(`api/batch/${batchId}/${action}`, {method: 'POST'});
  load();
}

function actionBtn(label, batchId, action, disabled, danger) {
  const btn = el('button', {className: 'action' + (danger ? ' danger' : '')},
                 [label]);
  if (disabled) btn.setAttribute('disabled', '');
  else btn.addEventListener('click', () => batchAction(batchId, action));
  return btn;
}

function batchRow(b) {
  // Live phase state takes precedence over batch.state for the visible pill
  // when the batch is running and its phase has a non-terminal state.
  const phaseState = (b.state === 'running' && b.current_phase_state)
                       ? b.current_phase_state : b.state;
  const phaseLabel = b.current_phase
    ? (b.current_phase + (b.fault_reason ? ` (${b.fault_reason})` : ''))
    : '—';
  const isRunning = b.state === 'running';
  const isHolding = phaseState === 'holding';
  const actions = el('td', null, [
    actionBtn('Hold',   b.batch_id, 'hold',   !isRunning || isHolding, false),
    actionBtn('Resume', b.batch_id, 'resume', !isHolding,              false),
    actionBtn('Abort',  b.batch_id, 'abort',  !isRunning,              true),
  ]);
  return el('tr', null, [
    el('td', null, [b.batch_id]),
    el('td', null, [b.order_id]),
    el('td', null, [b.recipe_id]),
    el('td', null, [b.plc_id]),
    el('td', null, [b.part_serial || '—']),
    el('td', null, [phaseLabel]),
    el('td', null, [pill(phaseState)]),
    el('td', null, [fmt(b.started_at)]),
    actions,
  ]);
}

function eventRow(e) {
  return el('tr', null, [
    el('td', null, [e.batch]),
    el('td', null, [e.phase]),
    el('td', null, [pill(e.state)]),
    el('td', null, [fmt(e.ts)]),
  ]);
}

async function load() {
  const r = await fetch('api/batches');
  const j = await r.json();
  const rows = j.batches || [];

  const tbody = document.querySelector('#batches tbody');
  const empty = document.getElementById('empty');
  empty.style.display = rows.length ? 'none' : 'block';
  tbody.replaceChildren(...rows.map(batchRow));

  const events = [];
  for (const b of rows) {
    for (const e of (b.events || []).slice(-5).reverse()) {
      events.push({batch: b.batch_id, phase: e.phase, state: e.state, ts: e.ts});
    }
  }
  events.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));
  document.querySelector('#events tbody')
    .replaceChildren(...events.slice(0, 20).map(eventRow));
}

load();
setInterval(load, 2000);
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return (INDEX_HTML
            .replace("__SITE_LABEL__", SITE.upper())
            .replace("__LINE_LABEL__", LINE.upper()))


_mqtt: mqtt.Client | None = None  # populated by main(), used by Flask handlers


def main() -> None:
    global _mqtt
    client = mqtt.Client(
        client_id="mes-sim",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    _mqtt = client

    threading.Thread(target=dispatcher, args=(client,), daemon=True).start()
    print(f"[{utc_now()}] MES sim listening on :{PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
