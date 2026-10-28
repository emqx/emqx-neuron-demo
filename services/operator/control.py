"""Per-device FIFO claims and batch reservations, enforced before every write.

Ownership is durable; a restart recovers the device before handing it on.
MQTT acknowledgements prove gateway writes, telemetry proves observed values.
"""
from collections import deque
import json
import math
import secrets
import sqlite3
import threading
import time
import uuid

from visitors import Limiter, Refused


class DeviceControl:
    DEVICES = ("plc-1", "plc-2", "hvac-1")
    HVAC_DEFAULTS = {"temp_setpoint_c": 22, "fan_speed_pct": 50, "mode_cmd": 5, "enable_cmd": 1}
    CNC_DEFAULTS = {"manual_rpm": 0, "manual_until": 0}

    def __init__(self, demo, path=":memory:", *, seconds=30, batch_seconds=90,
                 capacity=3, heartbeat=20, clock=time.time):
        self.demo, self.clock = demo, clock
        self.seconds, self.batch_seconds = seconds, batch_seconds
        self.capacity, self.heartbeat = capacity, heartbeat
        self.lock = threading.RLock()
        self.waiting = {device: deque() for device in self.DEVICES}
        self.active = {}
        self.limiter = Limiter(clock=clock)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS device_claims (device TEXT PRIMARY KEY, ticket TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS control_requests (owner TEXT, request_id TEXT, body TEXT, ticket TEXT, created REAL, PRIMARY KEY(owner,request_id))")
        for device, value in self.db.execute("SELECT device,ticket FROM device_claims"):
            ticket = json.loads(value)
            ticket.update(stage="recovering", pending=None, last_reserve=0, last_cancel=0)
            self.active[device] = ticket
        self.reservations = {}
        self.stop = threading.Event()

    def base(self, device):
        return (f"{self.demo.prefix}/utilities/compressor-room/{device}" if device == "hvac-1"
                else f"{self.demo.machine_prefix}/{device}")

    def send(self, device, suffix, body, retain=False):
        self.demo.send(self.base(device) + "/" + suffix, body, retain=retain)

    def save(self):
        with self.db:
            self.db.execute("DELETE FROM device_claims")
            self.db.executemany("INSERT INTO device_claims VALUES (?,?)",
                                [(key, json.dumps(value)) for key, value in self.active.items()])

    @staticmethod
    def request_id(value):
        if not isinstance(value, str) or not 8 <= len(value) <= 80:
            raise Refused("A request_id of 8–80 characters is required.", 400)
        return value

    def join(self, owner, ip, request_id, device, recipe=None):
        self.request_id(request_id)
        if device not in self.DEVICES or (recipe is not None and (device == "hvac-1" or recipe not in ("program-a", "program-quick"))):
            raise Refused("Choose a valid device and recipe.", 400)
        now = self.clock()
        body = json.dumps([device, recipe])
        with self.lock:
            old = self.db.execute("SELECT body,ticket FROM control_requests WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if old:
                if old[0] != body:
                    raise Refused("This request_id was already used for a different request.")
                return old[1]
            tickets = list(self.active.values()) + [t for queue in self.waiting.values() for t in queue]
            if any(t["owner"] == owner for t in tickets):
                raise Refused("Finish or release your current claim before requesting another device.")
            if len(self.waiting[device]) >= self.capacity:
                raise Refused("This device's queue is full. Try another device or wait.", 429, 10)
            self.limiter.take(("join", owner), 6, 60)
            self.limiter.take(("join-ip", ip), 15, 60)
            self.limiter.take("join-global", 40, 60)
            ticket = {"id": uuid.uuid4().hex, "owner": owner, "lease": secrets.token_urlsafe(24),
                      "device": device, "kind": "batch" if recipe else "manual", "recipe": recipe,
                      "order_id": "VIS-" + uuid.uuid4().hex if recipe else None,
                      "created": now, "seen": now, "stage": "waiting", "pending": None,
                      "dispatched": False, "last_reserve": 0, "last_cancel": 0}
            with self.db:
                self.db.execute("DELETE FROM control_requests WHERE created < ?", (now - 86400,))
                self.db.execute("INSERT INTO control_requests VALUES (?,?,?,?,?)", (owner, request_id, body, ticket["id"], now))
            self.waiting[device].append(ticket)
            return ticket["id"]

    def snapshot(self, owner):
        now = self.clock()
        with self.lock:
            result = {}
            for device in self.DEVICES:
                ticket = self.active.get(device)
                owned = bool(ticket and ticket["owner"] == owner)
                position = None
                for i, queued in enumerate(self.waiting[device]):
                    if queued["owner"] == owner:
                        queued["seen"] = now
                        position = i + 1
                if owned:
                    ticket["seen"] = now
                enabled = owned and ticket["stage"] == "active" and now < ticket["deadline"]
                pending = ticket.get("pending") if ticket else None
                result[device] = {
                    "owned": enabled, "mine": owned, "busy": bool(ticket),
                    "kind": ticket["kind"] if ticket else None,
                    "stage": ticket["stage"] if ticket else "available",
                    "remaining": max(0, math.ceil(ticket["deadline"] - now)) if ticket else 0,
                    "position": position, "waiting": len(self.waiting[device]), "capacity": self.capacity,
                    "lease_id": ticket["lease"] if enabled else None,
                    "order_id": ticket["order_id"] if ticket else None,
                    "command": {k: pending.get(k) for k in ("id", "status", "error")} if pending else None,
                }
            return {"devices": result, "claim_seconds": self.seconds,
                    "has_request": any(d["mine"] or d["position"] for d in result.values())}

    def owned(self, owner, device, lease):
        ticket = self.active.get(device)
        if (not ticket or ticket["owner"] != owner or ticket["stage"] != "active"
                or self.clock() >= ticket["deadline"] or not isinstance(lease, str)
                or not secrets.compare_digest(lease, ticket["lease"])):
            raise Refused("You need the current claim for this device.", 403)
        return ticket

    def release(self, owner, device, lease):
        if device not in self.DEVICES:
            raise Refused("Unknown device.", 400)
        with self.lock:
            queue = self.waiting[device]
            if any(t["owner"] == owner for t in queue):
                self.waiting[device] = deque(t for t in queue if t["owner"] != owner)
                return
            ticket = self.owned(owner, device, lease)
            ticket.update(stage="recovering", pending=None)
            self.save()

    def observe(self, topic, body, retained=False):
        if retained:
            return
        with self.lock:
            for device, ticket in self.active.items():
                if topic == self.base(device) + "/control/reservation/ack" and body.get("id") == ticket["id"]:
                    self.reservations[device] = body
                pending = ticket.get("pending")
                if pending and topic == self.base(device) + "/cmd/ack" and str(body.get("uuid")) == pending["id"]:
                    pending["acked"] = body.get("error") == 0
                    if not pending["acked"]:
                        pending.update(status="failed", error="The gateway rejected this write.")

    @staticmethod
    def number(value, low, high):
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise Refused(f"Choose a number from {low} to {high}.", 400)
        return value

    def write(self, ticket, values, request_id):
        device = ticket["device"]
        ticket["pending"] = {"id": request_id, "sent": self.clock(), "values": values,
                             "acked": False, "status": "sent", "error": None}
        self.send(device, "cmd/req", {"uuid": request_id,
                  "node": "src-bacnet" if device == "hvac-1" else "src-opcua",
                  "group": "hvac" if device == "hvac-1" else "plc",
                  "tags": [{"tag": tag, "value": value} for tag, value in values.items()]})

    def command(self, owner, device, body):
        if device not in self.DEVICES:
            raise Refused("Unknown device.", 400)
        rid = self.request_id(body.get("request_id"))
        action = body.get("action")
        with self.lock:
            snapshot = self.demo.snapshot()
            asset = snapshot["assets"].get(device, {})
            if action == "status":
                self.limiter.take(("status", owner), 6, 10)
                self.limiter.take("status-global", 90, 60)
                self.send(device, "control/request", {"id": rid, "action": "status"})
                # Explicitly a snapshot of the last observed values, not a new
                # protocol read or a synthetic device acknowledgement.
                self.send(device, "control/response", self.status_snapshot(device, rid))
                return rid
            ticket = self.owned(owner, device, body.get("lease_id"))
            if not asset.get("fresh"):
                raise Refused("Waiting for fresh device data.", 503)
            previous = ticket.get("pending")
            if previous and previous["id"] == rid:
                if previous.get("request") != body:
                    raise Refused("This request_id was already used for a different command.")
                return rid
            if previous and previous["status"] == "sent":
                raise Refused("Wait for the previous command to be observed.")
            self.limiter.take(("command", owner), 8, 5)
            values = None
            if ticket["kind"] == "manual" and action == "rpm" and device != "hvac-1":
                values = {"manual_rpm": self.number(body.get("value"), 0, 6000), "manual_until": ticket["deadline"]}
            elif ticket["kind"] == "manual" and action == "hvac" and device == "hvac-1":
                values = {"temp_setpoint_c": self.number(body.get("temperature"), 18, 28),
                          "fan_speed_pct": self.number(body.get("fan"), 0, 100),
                          "mode_cmd": self.number(body.get("mode"), 1, 5),
                          "enable_cmd": self.number(body.get("enabled"), 0, 1)}
                if type(values["mode_cmd"]) is not int or type(values["enable_cmd"]) is not int:
                    raise Refused("Mode and enabled must be integers.", 400)
            elif ticket["kind"] == "batch" and action in ("hold", "resume", "abort", "inject", "clear"):
                with self.demo.lock:
                    order = dict(self.demo.orders.get(ticket["order_id"], {}))
                plc = snapshot["plcs"].get(device, {})
                allowed = {"hold": ("running",), "resume": ("holding",), "abort": ("running", "holding", "faulted"),
                           "inject": ("running",), "clear": ("faulted",)}
                if (order.get("state") != "running" or not body.get("batch_id")
                        or body["batch_id"] != order.get("batch_id") or body["batch_id"] != plc.get("batch_id")
                        or body.get("phase") != plc.get("phase") or plc.get("state") not in allowed[action]):
                    raise Refused("The batch or phase changed. Wait for the current state.")
                if action == "abort":
                    ticket.update(stage="recovering", pending=None)
                    self.save()
                elif action in ("inject", "clear"):
                    self.send(device, "_demo/fault", {"action": action, "type": "over_temp"})
                else:
                    self.send(device, f"batch/{body['batch_id']}/phase/{body['phase']}/command",
                              {"action": action, "batch_id": body["batch_id"], "phase": body["phase"]})
                return rid
            else:
                raise Refused("This action is not available for this claim.", 400)
            self.send(device, "control/request", {"id": rid, "action": action, "values": values})
            self.write(ticket, values, rid)
            ticket["pending"]["request"] = body
            return rid

    def status_snapshot(self, device, request_id):
        """The cached observation is also available to an HTTP-only viewer."""
        asset = self.demo.snapshot()["assets"].get(device, {})
        return {"id": request_id, "source": "observed telemetry",
                "fresh": bool(asset.get("fresh")), "observed_at": asset.get("source_at"),
                "values": asset.get("values", {})}

    def reserve(self, ticket):
        if ticket["device"] == "hvac-1":
            return True
        now = self.clock()
        interval = 10 if self.reservations.get(ticket["device"], {}).get("accepted") else 2
        if now - ticket["last_reserve"] >= interval:
            self.send(ticket["device"], "control/reservation",
                      {"id": ticket["id"], "kind": ticket["kind"], "order_id": ticket["order_id"]}, retain=True)
            ticket["last_reserve"] = now
        return self.reservations.get(ticket["device"], {}).get("accepted") is True

    def pending_done(self, ticket, asset):
        pending = ticket.get("pending")
        if not pending:
            return False
        values = asset.get("values", {})
        matched = (pending["acked"] and asset.get("fresh") and asset.get("received_at", 0) > pending["sent"]
                   and all(type(values.get(key)) in (int, float, bool) and abs(values[key] - value) < 0.01
                           for key, value in pending["values"].items()))
        if ticket["device"] != "hvac-1":
            # A tag write can be polled before the PLC's next simulation tick.
            # Wait for the spindle itself, including zero RPM before handoff.
            matched = matched and values.get("spindle_rpm") == pending["values"].get("manual_rpm")
        if matched:
            pending["status"] = "observed"
        elif pending["status"] == "sent" and self.clock() - pending["sent"] > 10:
            pending.update(status="failed", error="No matching telemetry arrived. Check the gateway.")
        return matched

    def recover(self, ticket, state):
        device, now = ticket["device"], self.clock()
        if not self.reserve(ticket):
            return
        if ticket["dispatched"]:
            with self.demo.lock:
                order = dict(self.demo.orders.get(ticket["order_id"], {}))
            if order.get("state") not in ("completed", "failed"):
                if now - ticket["last_cancel"] >= 2:
                    self.demo.send(f"{self.demo.prefix}/orders/{ticket['order_id']}",
                                   {"order_id": ticket["order_id"], "cancel": True})
                    ticket["last_cancel"] = now
                return
        asset = state["assets"].get(device, {})
        if not asset.get("fresh"):
            return
        if device != "hvac-1" and state["plcs"].get(device, {}).get("state") not in ("idle", "completed", "aborted"):
            return
        if self.pending_done(ticket, asset):
            self.send(device, "control/released", {"id": ticket["id"], "state": "available"})
            if device != "hvac-1":
                self.send(device, "control/reservation", {"id": ticket["id"], "release": True}, retain=True)
            del self.active[device]
            self.reservations.pop(device, None)
            self.save()
        elif not ticket["pending"] or now - ticket["pending"]["sent"] > 12:
            self.write(ticket, self.HVAC_DEFAULTS if device == "hvac-1" else self.CNC_DEFAULTS, uuid.uuid4().hex)

    def tick(self):
        now = self.clock()
        with self.lock:
            state = self.demo.snapshot()
            for device in self.DEVICES:
                self.waiting[device] = deque(t for t in self.waiting[device]
                                            if now - t["seen"] < self.heartbeat and now - t["created"] < 600)
                ticket = self.active.get(device)
                if not ticket:
                    asset = state["assets"].get(device, {})
                    idle = device == "hvac-1" or state["plcs"].get(device, {}).get("state") in ("idle", "completed", "aborted")
                    if not self.waiting[device] or not asset.get("fresh") or not idle:
                        continue
                    # The legacy private booth script must finish before public
                    # reservations start; it has no per-device reservation ID.
                    if state["scene"]["active"]:
                        self.demo.stop_loop()
                        continue
                    ticket = self.waiting[device].popleft()
                    ticket.update(stage="reserving", deadline=now + (self.batch_seconds if ticket["kind"] == "batch" else self.seconds))
                    self.active[device] = ticket
                    self.save()
                if now >= ticket["deadline"] or (ticket["kind"] == "manual" and now - ticket["seen"] > self.heartbeat):
                    if ticket["stage"] != "recovering":
                        ticket.update(stage="recovering", pending=None)
                        self.save()
                if ticket["stage"] == "recovering":
                    self.recover(ticket, state)
                    continue
                if not self.reserve(ticket):
                    continue
                if ticket["stage"] == "reserving":
                    ticket["stage"] = "active"
                    if ticket["kind"] == "batch":
                        ticket["dispatched"] = True
                        self.save()  # Persist before publishing; recover uncertain delivery.
                        self.demo.send(f"{self.demo.prefix}/orders/{ticket['order_id']}",
                                       {"order_id": ticket["order_id"], "recipe_id": ticket["recipe"], "qty": 1,
                                        "plc_id": device, "claim_id": ticket["id"], "expires_at": now + 20})
                    self.save()
                self.pending_done(ticket, state["assets"].get(device, {}))
                if ticket["kind"] == "batch":
                    with self.demo.lock:
                        done = self.demo.orders.get(ticket["order_id"], {}).get("state") in ("completed", "failed")
                    if done:
                        ticket.update(stage="recovering", pending=None)
                        self.save()

    def run(self):
        while not self.stop.wait(0.25):
            try:
                self.tick()
            except Exception as exc:
                print(f"Device claim recovery waiting: {type(exc).__name__}: {exc}", flush=True)

    def start(self):
        threading.Thread(target=self.run, name="device-claims", daemon=True).start()
