import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/operator"))
from visitors import Refused, Limiter
from control import DeviceControl
from demo import Demo


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        with patch("paho.mqtt.client.Client"):
            self.module = importlib.import_module("app")
        self.password = self.module.PASSWORD_HASH
        self.module.PASSWORD_HASH = ""
        self.module.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.module.http_limits = Limiter()
        self.demo = Mock()
        self.demo.prefix = "acme-mfg/plant-1"
        self.demo.machine_prefix = self.demo.prefix + "/machining/line-1"
        self.demo.snapshot.return_value = {"connected": True, "scene": {"active": False},
            "assets": {device: {"fresh": True} for device in DeviceControl.DEVICES},
            "plcs": {"plc-1": {"state": "idle"}, "plc-2": {"state": "idle"}}}
        self.original_controls = self.module.controls
        self.module.controls = DeviceControl(self.demo)
        self.a, self.b = self.module.app.test_client(), self.module.app.test_client()
        self.tokens = {}
        for client in (self.a, self.b):
            self.tokens[client] = client.get("/api/state").json["visitor"]["csrf"]

    def tearDown(self):
        self.module.PASSWORD_HASH = self.password
        self.module.controls = self.original_controls

    def post(self, client, path, body):
        return client.post(path, json=body, headers={"X-Demo-CSRF": self.tokens[client]})

    def test_different_browsers_cannot_control_each_others_device(self):
        path = "/api/devices/plc-1"
        self.assertEqual(self.post(self.a, path + "/claim", {"request_id": "request-one"}).status_code, 202)
        self.module.controls.tick()
        ticket = self.module.controls.active["plc-1"]
        self.module.controls.observe(self.module.controls.base("plc-1") + "/control/reservation/ack", {"id": ticket["id"], "accepted": True})
        self.module.controls.tick()
        lease = self.a.get("/api/state").json["visitor"]["devices"]["plc-1"]["lease_id"]
        self.assertTrue(lease)
        self.assertIsNone(self.b.get("/api/state").json["visitor"]["devices"]["plc-1"]["lease_id"])
        for suffix, body in [("release", {}), ("command", {"action": "rpm", "value": 1000, "request_id": "command-one"})]:
            self.assertEqual(self.post(self.b, path + "/" + suffix, {**body, "lease_id": lease}).status_code, 403)
        self.assertEqual(self.post(self.a, path + "/release", {"lease_id": "stale"}).status_code, 403)
        self.assertEqual(self.post(self.a, path + "/release", {"lease_id": lease}).status_code, 202)

    def test_csrf_and_unrestricted_loop_are_denied(self):
        self.assertEqual(self.a.post("/api/demo/start", json={"request_id": "request-one"}).status_code, 403)
        self.assertEqual(self.post(self.a, "/api/demo/start", {"loop": True}).status_code, 403)
        self.assertEqual(self.post(self.a, "/api/place-order", {"recipe_id": "program-a", "qty": 100}).status_code, 400)

    def test_status_reply_is_available_without_claim_or_browser_mqtt(self):
        asset = self.demo.snapshot.return_value["assets"]["plc-1"]
        asset.update(values={"spindle_rpm": 0, "state": "idle"}, source_at=1234)
        response = self.post(self.a, "/api/devices/plc-1/command",
                             {"action": "status", "request_id": "snapshot-one"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["snapshot"], {"id": "snapshot-one", "source": "observed telemetry",
            "fresh": True, "observed_at": 1234, "values": {"spindle_rpm": 0, "state": "idle"}})
        self.assertFalse(self.module.controls.active)
        self.assertEqual(self.demo.send.call_count, 2)  # MQTT request and cached response remain visible.

    def test_mqtt_token_cannot_grant_publish_or_impersonate_services(self):
        import base64
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with patch.object(self.module, "JWT_PRIVATE_KEY", key):
            response = self.a.get("/api/mqtt-credentials?slot=test")
        import jwt
        verified = jwt.decode(response.json["password"], key.public_key(), algorithms=["RS256"])
        self.assertEqual(verified["username"], "visitor")
        payload = response.json["password"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        self.assertEqual(claims["username"], "visitor")
        self.assertEqual(claims["clientid"], response.json["clientId"])
        self.assertEqual(claims["exp"] - claims["iat"], 300)
        self.assertEqual(claims["acl"][0]["action"], "subscribe")
        self.assertEqual(claims["acl"][-1]["permission"], "deny")

    def test_stale_phase_and_foreign_batch_never_publish(self):
        demo = Demo(Mock(), "acme", "plant", "machining", "line")
        demo.snapshot = lambda: {"ready": True}
        demo.scene.update(active=True, plc_id="plc-1")
        demo.owned_order = "O-one"
        demo.orders["O-one"] = {"state": "running", "batch_id": "B-one", "plc_id": "plc-1"}
        demo.plcs["plc-1"] = {"batch_id": "B-one", "phase": "rough", "state": "running"}
        for batch, phase in (("B-other", "rough"), ("B-one", "load")):
            with self.assertRaises(ValueError):
                demo.command("hold", batch, phase)
        demo.publish.assert_not_called()
        demo.command("hold", "B-one", "rough")
        demo.publish.assert_called_once()


class JournalTests(unittest.TestCase):
    def test_restart_marks_interrupted_orders_and_preserves_deduplication(self):
        sys.path.insert(0, str(ROOT / "services/mes-sim"))
        from journal import Journal
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "mes.sqlite")
            journal = Journal(path)
            self.assertTrue(journal.claim("O-one"))
            self.assertFalse(journal.claim("O-one"))
            journal.set("O-one", "running")
            journal.set("O-two", "completed")
            restarted = Journal(path)
            self.assertEqual(restarted.interrupted(), ["O-one"])
            self.assertEqual(restarted.state("O-one"), "failed")
            self.assertFalse(restarted.claim("O-one"))
            self.assertFalse(restarted.claim("O-two"))


class MesAdmissionTests(unittest.TestCase):
    def setUp(self):
        import queue
        sys.path.insert(0, str(ROOT / "services/mes-sim"))
        self.mes = importlib.import_module("mes")
        self.mes.journal = self.mes.Journal(":memory:")
        self.mes.order_queue = queue.Queue(maxsize=3)
        self.mes.orders_in_flight.clear()
        self.mes.batches.clear()
        self.mes.plc_states.clear()
        self.mes.cancelled_orders.clear()
        self.mes.reservations.clear()
        self.client = Mock()

    def send(self, order_id, *, retained=False, **values):
        import time
        body = {"order_id": order_id, "recipe_id": "program-a", "qty": 1, "expires_at": time.time() + 20, **values}
        topic = f"{self.mes.ENTERPRISE}/{self.mes.SITE}/orders/{order_id}"
        self.mes.on_message(self.client, None, Mock(topic=topic, payload=json.dumps(body).encode(), retain=retained))

    def test_backlog_is_bounded_and_duplicates_do_not_consume_slots(self):
        for name in ("one", "one", "two", "three", "four"):
            self.send(name)
        self.assertEqual(self.mes.order_queue.qsize(), 3)
        self.assertEqual(self.mes.journal.state("four"), "failed")
        self.assertNotIn("four", self.mes.orders_in_flight)

    def test_expired_retained_and_invalid_orders_never_dispatch(self):
        self.send("expired", expires_at=0)
        self.send("retained", retained=True)
        self.send("invalid", qty=True)
        self.assertTrue(self.mes.order_queue.empty())
        for name in ("expired", "retained", "invalid"):
            self.assertEqual(self.mes.journal.state(name), "failed")

    def test_cancellation_before_order_delivery_prevents_late_dispatch(self):
        self.send("cancelled", cancel=True)
        self.send("cancelled")
        self.assertTrue(self.mes.order_queue.empty())
        self.assertEqual(self.mes.journal.state("cancelled"), "failed")

    def test_cancelled_queued_order_is_skipped_by_worker(self):
        self.send("pending")
        self.send("pending", cancel=True)
        with patch.object(self.mes, "publish_phase_command") as command:
            self.mes._safe_run(self.client, self.mes.order_queue.get_nowait())
        command.assert_not_called()
        self.assertFalse(self.mes.orders_in_flight)

    def test_reservation_excludes_manual_device_and_only_admits_matching_batch(self):
        import time
        for device in ("plc-1", "plc-2"):
            self.mes.plc_states[device] = {"state": "idle", "received": time.monotonic()}
        topic = f"{self.mes.ENTERPRISE}/{self.mes.SITE}/{self.mes.AREA}/{self.mes.LINE}/plc-1/control/reservation"
        def reserve(body):
            self.mes.on_message(self.client, None, Mock(topic=topic, payload=json.dumps(body).encode(), retain=False))
        reserve({"id": "manual-one", "kind": "manual", "order_id": None})
        self.assertEqual(self.mes.allocate_plc()["id"], "plc-2")
        self.assertIsNone(self.mes.allocate_plc({"plc_id": "plc-1", "claim_id": "manual-one"}))
        reserve({"id": "wrong", "release": True})
        self.assertIn("plc-1", self.mes.reservations)
        reserve({"id": "manual-one", "release": True})
        reserve({"id": "batch-one", "kind": "batch", "order_id": "order-one"})
        self.assertIsNone(self.mes.allocate_plc({"plc_id": "plc-1", "claim_id": "batch-one", "order_id": "foreign"}))
        self.assertEqual(self.mes.allocate_plc({"plc_id": "plc-1", "claim_id": "batch-one", "order_id": "order-one"})["id"], "plc-1")

    def test_reservation_cannot_take_machine_from_running_mes_batch(self):
        batch = Mock(plc_id="plc-1", state="running", order_id="order-one")
        self.mes.batches["B-one"] = batch
        topic = f"{self.mes.ENTERPRISE}/{self.mes.SITE}/{self.mes.AREA}/{self.mes.LINE}/plc-1/control/reservation"
        self.mes.on_message(self.client, None, Mock(topic=topic, payload=json.dumps({"id": "manual-one", "kind": "manual"}).encode(), retain=False))
        self.assertNotIn("plc-1", self.mes.reservations)
        self.assertFalse(json.loads(self.client.publish.call_args.args[1])["accepted"])

    def test_direct_http_actions_need_private_operator_key(self):
        client = self.mes.app.test_client()
        with patch.dict(os.environ, {"DEMO_ADMIN_KEY": "private-test-key"}):
            self.assertEqual(client.post("/api/batch/unknown/abort").status_code, 403)
            self.assertEqual(client.post("/api/batch/unknown/abort", headers={"X-Demo-Admin": "wrong"}).status_code, 403)
            self.assertEqual(client.post("/api/batch/unknown/abort", headers={"X-Demo-Admin": "private-test-key"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
