"""Observed MQTT state and a single, cancellable booth walkthrough.

The PLC/MES remain authoritative. Captions advance after observed transitions;
reset aborts only this walkthrough's order and never deletes stored history.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import threading
import time
import uuid


class Cancelled(Exception):
    pass


class Demo:
    FRESH_SECONDS = 6

    def __init__(self, publish, enterprise, site, area, line):
        self.publish = publish
        self.prefix = f"{enterprise}/{site}"
        self.machine_prefix = f"{self.prefix}/{area}/{line}"
        self.area = area
        self.line = line
        self.lock = threading.RLock()
        self.connected = False
        self.assets = {}
        self.plcs = {}
        self.orders = {}
        self.events = deque(maxlen=12)
        self.activity = {}
        self.sample = None
        self.messages = 0
        self.event_seq = 0
        self.reset_version = 0
        self.owned_order = None
        self.stop = threading.Event()
        self.worker = None
        self.scene = self.idle_scene()
        self.recipe = "program-a"
        self.guided = True
        self.next_order = None

    @staticmethod
    def idle_scene():
        return {"active": False, "loop": False, "cycle": None, "step": "ready",
                "title": "Connect once. Use the data everywhere.",
                "caption": "Follow a production order, then connect another dashboard to the same factory data.",
                "order_id": None, "plc_id": None, "iteration": 0,
                "attach_dashboard": False, "error": None}

    def connection(self, connected):
        with self.lock:
            self.connected = connected

    def event(self, label, detail, kind):
        self.event_seq += 1
        self.events.appendleft({"id": self.event_seq, "time": time.time(),
                                "label": label, "detail": detail, "kind": kind})

    @staticmethod
    def source_time(body, received):
        stamp = body.get("timestamp")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp):
            return stamp / 1000
        return received

    def observe(self, topic, body, retained=False):
        if not isinstance(body, dict) or not topic.startswith(self.prefix + "/"):
            return
        parts = topic.split("/")
        now = time.time()
        with self.lock:
            self.messages += 1
            if len(parts) == 6 and parts[-1] == "telemetry":
                asset = parts[4]
                if asset not in ("plc-1", "plc-2", "hvac-1"):
                    return
                values = body.get("values", {})
                required = ("supply_air_temp_c", "fan_kw") if asset == "hvac-1" else ("spindle_rpm", "power_kw")
                valid = isinstance(values, dict) and all(
                    isinstance(values.get(k), (int, float))
                    and not isinstance(values[k], bool) and math.isfinite(values[k])
                    for k in required)
                prev = self.assets.get(asset, {})
                self.assets[asset] = {
                    "values": values if valid else prev.get("values", {}),
                    "received_at": now if valid and not retained else prev.get("received_at", 0),
                    "source_at": self.source_time(body, now) if valid else prev.get("source_at", 0),
                    "error": not valid, "topic": topic,
                    "protocol": "BACnet/IP" if asset == "hvac-1" else "OPC UA"}
                if valid and not retained:
                    self.activity["acquisition"] = now
                    self.activity[f"acquisition:{asset}"] = now
                    # Restore lifecycle state after a broker restart without
                    # fabricating an on-change machine event.
                    if asset != "hvac-1" and all(isinstance(values.get(k), str) for k in ("phase_state", "current_batch_id", "current_phase")):
                        self.plcs[asset] = {"plc_id": asset, "state": values["phase_state"],
                                            "batch_id": values["current_batch_id"], "phase": values["current_phase"],
                                            "reason": values.get("state_reason", ""),
                                            "ts": datetime.now(timezone.utc).isoformat()}
                return
            if len(parts) == 6 and parts[-1] == "state-events":
                values = body.get("values", {})
                if not retained and isinstance(values, dict) and values.get("phase_event_json"):
                    self.activity["machine-events"] = now
                    self.activity[f"events:{parts[4]}"] = now
                return
            if len(parts) == 7 and parts[-2] == "telemetry":
                if not retained:
                    self.activity["distribution"] = now
                if parts[4] == "plc-1" and parts[-1] == "spindle_rpm":
                    self.sample = {"topic": topic, "payload": body, "retained": retained,
                                   "received_at": now, "source": "OPC UA · ns=2;i=2", "unit": "rpm"}
                return
            if len(parts) == 5 and parts[2] == "orders" and parts[-1] == "status":
                if not body:
                    self.orders.pop(parts[3], None)
                    return
                old = self.orders.get(parts[3], {})
                self.orders[parts[3]] = body
                if not retained:
                    self.activity["order-status"] = now
                # Keep only recent terminal orders, including the current walkthrough.
                if len(self.orders) > 100:
                    for key, value in list(self.orders.items()):
                        if key != self.owned_order and value.get("state") in ("completed", "failed"):
                            del self.orders[key]
                            break
                if not retained and old.get("state") != body.get("state"):
                    self.activity["events"] = now
                    self.event("Order " + str(body.get("state", "updated")), parts[3], "order")
                return
            if len(parts) == 4 and parts[2] == "orders":
                if not retained and body:
                    self.activity["orders"] = now
                return
            if len(parts) == 6 and parts[-1] == "state":
                if body:
                    old = self.plcs.get(parts[4], {})
                    self.plcs[parts[4]] = body
                    if not retained and (old.get("state"), old.get("phase")) != (body.get("state"), body.get("phase")):
                        self.activity["events"] = now
                        self.event(parts[4].replace("plc-", "CNC ") + " · " + str(body.get("state", "")),
                                   body.get("phase") or "Ready for the next order", "machine")
                else:
                    self.plcs.pop(parts[4], None)
                return
            if len(parts) == 10 and parts[-1] == "command" and not retained:
                self.activity["commands"] = now
                self.activity[f"commands:{parts[4]}"] = now
                self.event("MES → machine", f"{body.get('action', '')} · {body.get('phase', '')}", "command")
            if len(parts) == 7 and parts[-2:] == ["cmd", "req"] and not retained:
                self.activity["commands"] = now
                self.activity[f"commands:{parts[4]}"] = now

    def snapshot(self):
        now = time.time()
        with self.lock:
            assets = deepcopy(self.assets)
            for asset in assets.values():
                age = max(now - asset["received_at"], now - asset["source_at"], 0)
                asset["age"] = round(age, 1)
                asset["fresh"] = self.connected and not asset["error"] and age < self.FRESH_SECONDS
            ready = self.connected and all(assets.get(k, {}).get("fresh", False) for k in ("plc-1", "plc-2", "hvac-1"))
            scene = deepcopy(self.scene)
            order = deepcopy(self.orders.get(scene["order_id"], {}))
            return {"connected": self.connected, "ready": ready, "assets": assets,
                    "plcs": deepcopy(self.plcs), "scene": scene, "order": order,
                    "events": list(self.events), "sample": deepcopy(self.sample),
                    "activity": {key: now - value < 1.6 for key, value in self.activity.items()},
                    "messages": self.messages, "reset_version": self.reset_version,
                    "server_time": now}

    def set_scene(self, step, title, caption, **values):
        with self.lock:
            self.scene.update(step=step, title=title, caption=caption, **values)

    def send(self, topic, payload, retain=False):
        self.publish(topic, json.dumps(payload), qos=1, retain=retain)

    def fault(self, action):
        with self.lock:
            plc_id = self.scene["plc_id"]
            state = self.plcs.get(plc_id, {}).get("state")
        if action not in ("inject", "clear") or not plc_id:
            raise ValueError("Start a walkthrough before using its fault controls.")
        if state != ("running" if action == "inject" else "faulted"):
            raise ValueError("The machine is not ready for that action.")
        self.send(f"{self.machine_prefix}/{plc_id}/_demo/fault",
                  {"action": action, "type": "over_temp"})

    def available(self):
        with self.lock:
            return (self.snapshot()["ready"] and not (self.worker and self.worker.is_alive())
                    and all(self.plcs.get(key, {}).get("state") in ("idle", "completed", "aborted") for key in ("plc-1", "plc-2"))
                    and (not self.owned_order or self.orders.get(self.owned_order, {}).get("state") in ("completed", "failed")))

    def stop_loop(self):
        with self.lock:
            self.scene["loop"] = False

    def command(self, action, batch_id, phase, plc_id=None):
        with self.lock:
            order = self.orders.get(self.owned_order, {})
            machine = order.get("plc_id")
            plc = self.plcs.get(machine, {})
            if (not self.scene["active"] or not self.snapshot()["ready"] or order.get("state") != "running"
                    or not batch_id or batch_id != order.get("batch_id") or batch_id != plc.get("batch_id")
                    or phase != plc.get("phase") or (plc_id and machine != plc_id)):
                raise ValueError("The order or phase changed. Refresh before using this control.")
            expected = {"hold": ("running",), "resume": ("holding",), "abort": ("running", "holding", "faulted"),
                        "inject": ("running",), "clear": ("faulted",)}
            if action not in expected or plc.get("state") not in expected[action]:
                raise ValueError("The machine is not ready for that action.")
            if action in ("inject", "clear"):
                self.fault(action)
            else:
                self.send(f"{self.machine_prefix}/{machine}/batch/{batch_id}/phase/{phase}/command",
                          {"action": action, "batch_id": batch_id, "phase": phase})

    def start(self, loop=False, recipe="program-a", guided=True, order_id=None):
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("A walkthrough is already running. Reset it before starting again.")
            if not self.snapshot()["ready"]:
                raise ValueError("Waiting for fresh OPC UA and BACnet data. Check the product connections.")
            if any(self.plcs.get(key, {}).get("state") not in ("idle", "completed", "aborted") for key in ("plc-1", "plc-2")):
                raise ValueError("The machines are busy. Finish the current order first.")
            if self.owned_order and self.orders.get(self.owned_order, {}).get("state") not in ("completed", "failed"):
                raise ValueError("The previous order still needs recovery. Use Reset walkthrough.")
            self.stop.clear()
            self.recipe, self.guided, self.next_order = recipe, guided, order_id
            self.owned_order = order_id
            self.scene = self.idle_scene()
            self.scene.update(active=True, loop=bool(loop))
            self.worker = threading.Thread(target=self.run, name="booth-walkthrough", daemon=True)
            self.worker.start()

    def reset(self):
        with self.lock:
            self.stop.set()
            self.scene.update(step="stopping", title="Resetting the walkthrough",
                              caption="Waiting for the current order to stop. Production history is preserved.", loop=False)
            if not self.worker or not self.worker.is_alive():
                self.worker = threading.Thread(target=self.finish_reset, daemon=True)
                self.worker.start()

    def wait_for(self, predicate, timeout, description):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stop.is_set():
                raise Cancelled()
            with self.lock:
                result = predicate()
                status = self.orders.get(self.owned_order, {}).get("state")
            if result:
                return result
            if status == "failed":
                raise RuntimeError("The MES reported a failed order. Reset the walkthrough and check the MES.")
            self.stop.wait(0.1)
        raise RuntimeError(f"Timed out waiting for {description}. Check EMQX Neuron and MES, then reset.")

    def pause(self, seconds):
        if self.stop.wait(seconds):
            raise Cancelled()

    def abort_owned_order(self):
        """Use the normal MQTT command path; retry across phase-boundary races."""
        if not self.owned_order:
            return
        # Also cancel a pending/not-yet-observed order. MES persists the
        # cancellation, so a delayed delivery cannot start it later.
        self.send(f"{self.prefix}/orders/{self.owned_order}",
                  {"order_id": self.owned_order, "cancel": True})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with self.lock:
                order = dict(self.orders.get(self.owned_order, {}))
                plc = dict(self.plcs.get(order.get("plc_id"), {}))
            if order.get("state") in ("completed", "failed"):
                return
            if plc.get("batch_id") == order.get("batch_id") and plc.get("phase"):
                topic = (f"{self.machine_prefix}/{order['plc_id']}/batch/{order['batch_id']}"
                         f"/phase/{plc['phase']}/command")
                self.send(topic, {"action": "abort", "batch_id": order["batch_id"], "phase": plc["phase"]})
            # The stop event is already set during cancellation; do not spin on it.
            time.sleep(0.5)
        raise RuntimeError("Could not confirm that the order stopped. Restore connectivity, then reset again.")

    def finish_reset(self):
        try:
            self.abort_owned_order()
        except Exception as exc:
            self.set_scene("error", "Recovery needs attention", str(exc), error=str(exc), active=False)
            return
        with self.lock:
            self.owned_order = None
            self.events.clear()
            self.reset_version += 1
            self.scene = self.idle_scene()

    def cycle(self):
        with self.lock:
            self.events.clear()
            self.scene.update(cycle=uuid.uuid4().hex, iteration=self.scene["iteration"] + 1,
                              order_id=None, plc_id=None, attach_dashboard=False)
        self.set_scene("connect", "Two protocols. One shared data foundation.",
                       "EMQX Neuron collects OPC UA machine data and BACnet building data. Both are live in the same namespace.")
        self.pause(5)
        if not self.snapshot()["ready"]:
            raise RuntimeError("Source data is stale. Restore the connections before starting an order.")
        order_id = self.next_order or f"BOOTH-{uuid.uuid4().hex[:12]}"
        with self.lock:
            self.owned_order = order_id
            self.scene["order_id"] = order_id
        self.set_scene("order", "An order becomes machine activity.",
                       "The MES sends phase commands through EMQX Enterprise and EMQX Neuron. The simulated CNC executes them over OPC UA.")
        self.send(f"{self.prefix}/orders/{order_id}",
                  {"order_id": order_id, "recipe_id": self.recipe, "qty": 1,
                   "expires_at": time.time() + 20,
                   "placed_at": datetime.now(timezone.utc).isoformat()})
        order = self.wait_for(lambda: self.orders.get(order_id, {}).get("state") == "running" and self.orders[order_id], 15, "MES dispatch")
        with self.lock:
            self.scene["plc_id"] = order["plc_id"]
        plc_id = order["plc_id"]
        if not self.guided:
            self.set_scene("order", "Your order is running", "Use the controls during your turn, or watch the order finish.")
            self.wait_for(lambda: self.orders.get(order_id, {}).get("state") == "completed", 85, "order completion")
            self.set_scene("complete", "Your order is complete", "The factory is ready for the next visitor.")
            return
        self.wait_for(lambda: self.plcs.get(plc_id, {}).get("phase") == "rough" and self.plcs[plc_id].get("state") == "running", 20, "the machining phase")
        self.set_scene("share", "Add an application. Reuse the same data.",
                       "The new dashboard opens its own MQTT connection. Existing retained values arrive first, followed by live updates.",
                       attach_dashboard=True)
        self.pause(4)
        with self.lock:
            already_faulted = self.plcs.get(plc_id, {}).get("state") == "faulted"
        if not already_faulted:
            self.fault("inject")
        self.wait_for(lambda: self.plcs.get(plc_id, {}).get("state") == "faulted", 8, "the fault event")
        self.set_scene("fault", "A machine event reaches the applications.",
                       "A simulated over-temperature fault pauses machining. The MES sees the same state change; recovery follows in a few seconds.")
        self.pause(7)
        with self.lock:
            faulted = self.plcs.get(plc_id, {}).get("state") == "faulted"
        if faulted:
            self.fault("clear")
        self.wait_for(lambda: self.plcs.get(plc_id, {}).get("state") == "running", 8, "machine recovery")
        self.set_scene("recover", "Clear the fault. Continue the order.",
                       "The command travels back through EMQX Neuron. Machine events drive the MES through the remaining phases.")
        self.wait_for(lambda: self.orders.get(order_id, {}).get("state") == "completed", 65, "order completion")
        self.set_scene("complete", "One connection. More ways to use factory data.",
                       "The order is complete. Machine and building data remain available for the next application.")
        self.pause(9)

    def run(self):
        try:
            while True:
                self.cycle()
                with self.lock:
                    looping = self.scene["loop"]
                if not looping:
                    break
        except Cancelled:
            self.finish_reset()
        except Exception as exc:
            error = str(exc)
            try:
                self.abort_owned_order()
            except Exception as recovery:
                error += " " + str(recovery)
            self.set_scene("error", "The walkthrough needs attention", error,
                           error=error, loop=False)
        finally:
            with self.lock:
                self.scene["active"] = False
