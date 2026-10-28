import importlib
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "operator"))
from showcase import Showcase


class ShowcaseTests(unittest.TestCase):
    def setUp(self):
        self.publish = Mock()
        self.demo = Showcase(self.publish, "acme-mfg", "plant-1", "machining", "line-1")
        self.demo.connection(True)

    def feed(self, stamp=None, retained=False):
        for asset in ("plc-1", "plc-2", "hvac-1"):
            area = "utilities/compressor-room" if asset == "hvac-1" else "machining/line-1"
            values = {"supply_air_temp_c": 18.5, "fan_kw": 1} if asset == "hvac-1" else {"spindle_rpm": 3000, "power_kw": 6}
            self.demo.observe(f"acme-mfg/plant-1/{area}/{asset}/telemetry",
                              {"timestamp": stamp if stamp is not None else time.time() * 1000, "values": values}, retained)
            if asset != "hvac-1":
                self.demo.observe(f"acme-mfg/plant-1/{area}/{asset}/state", {"state": "idle"}, True)

    def test_fresh_sources_and_broker_are_required(self):
        self.feed()
        self.assertTrue(self.demo.snapshot()["ready"])
        self.demo.connection(False)
        self.assertFalse(self.demo.snapshot()["ready"])
        self.assertFalse(self.demo.snapshot()["assets"]["plc-1"]["fresh"])

    def test_old_source_timestamps_and_retained_data_do_not_claim_live(self):
        self.feed(stamp=(time.time() - 60) * 1000)
        self.assertFalse(self.demo.snapshot()["ready"])
        self.demo.assets.clear()
        self.feed(retained=True)
        self.assertFalse(self.demo.snapshot()["ready"])

    def test_polled_lifecycle_restores_state_after_broker_restart(self):
        self.demo.observe("acme-mfg/plant-1/machining/line-1/plc-1/telemetry",
                          {"values": {"spindle_rpm": 0, "power_kw": 0, "phase_state": "idle",
                                      "current_batch_id": "", "current_phase": ""}})
        self.assertEqual(self.demo.snapshot()["plcs"]["plc-1"]["state"], "idle")
        self.assertFalse(self.demo.snapshot()["events"])

    def test_error_reads_keep_last_value_but_invalidate_freshness(self):
        self.feed()
        self.demo.observe("acme-mfg/plant-1/machining/line-1/plc-1/telemetry",
                          {"timestamp": time.time() * 1000, "errors": {"spindle_rpm": 3002}})
        asset = self.demo.snapshot()["assets"]["plc-1"]
        self.assertTrue(asset["error"])
        self.assertFalse(asset["fresh"])
        self.assertEqual(asset["values"]["spindle_rpm"], 3000)

    def test_foreign_namespace_and_invalid_payloads_are_ignored(self):
        self.demo.observe("other/plant-1/orders/o/status", {"state": "running"})
        self.demo.observe("acme-mfg/plant-1/orders/o/status", ["unexpected"])
        self.assertEqual(self.demo.snapshot()["messages"], 0)

    def test_retained_state_does_not_animate_a_new_event(self):
        self.demo.observe("acme-mfg/plant-1/machining/line-1/plc-1/state",
                          {"state": "running", "phase": "rough"}, True)
        self.assertEqual(self.demo.snapshot()["events"], [])
        self.demo.observe("acme-mfg/plant-1/machining/line-1/plc-1/state",
                          {"state": "faulted", "phase": "rough"})
        self.assertEqual(len(self.demo.snapshot()["events"]), 1)

    def test_command_activity_targets_only_the_addressed_machine(self):
        topic = "acme-mfg/plant-1/machining/line-1/plc-2/batch/B-1/phase/load/command"
        command = {"action": "start", "phase": "load"}
        self.demo.observe(topic, command, retained=True)
        self.assertNotIn("commands:plc-2", self.demo.snapshot()["activity"])
        self.demo.observe(topic, command)
        activity = self.demo.snapshot()["activity"]
        self.assertTrue(activity["commands"])
        self.assertTrue(activity["commands:plc-2"])
        self.assertNotIn("commands:plc-1", activity)
        self.assertNotIn("commands:hvac-1", activity)

    def test_packet_activity_is_specific_to_each_source_and_expires(self):
        topic = "acme-mfg/plant-1/machining/line-1/plc-1/telemetry"
        body = {"values": {"spindle_rpm": 3000, "power_kw": 6}}
        self.demo.observe(topic, body, retained=True)
        self.assertNotIn("acquisition:plc-1", self.demo.snapshot()["activity"])
        with patch("showcase.time.time", return_value=100):
            self.demo.observe(topic, body)
            activity = self.demo.snapshot()["activity"]
            self.assertTrue(activity["acquisition:plc-1"])
            self.assertNotIn("acquisition:plc-2", activity)
            self.assertNotIn("acquisition:hvac-1", activity)
        with patch("showcase.time.time", return_value=102):
            self.assertFalse(self.demo.snapshot()["activity"]["acquisition:plc-1"])

    def test_application_packets_follow_their_own_message_types(self):
        messages = [
            ("orders/O-1", {"recipe_id": "program-a"}, "orders"),
            ("orders/O-1/status", {"state": "running"}, "order-status"),
            ("machining/line-1/plc-1/state-events",
             {"values": {"phase_event_json": '{"state":"running"}'}}, "machine-events"),
        ]
        for suffix, body, key in messages:
            topic = "acme-mfg/plant-1/" + suffix
            self.demo.observe(topic, body, retained=True)
            self.assertNotIn(key, self.demo.snapshot()["activity"])
            self.demo.observe(topic, body)
            self.assertTrue(self.demo.snapshot()["activity"][key])

    def test_only_one_controller_can_own_the_walkthrough(self):
        self.feed()
        entered = threading.Event()

        def cycle():
            entered.set()
            self.demo.pause(10)

        self.demo.cycle = cycle
        self.demo.start(loop=True)
        self.assertTrue(entered.wait(2))
        with self.assertRaisesRegex(ValueError, "already running"):
            self.demo.start()
        self.demo.reset()
        self.demo.worker.join(2)
        self.assertFalse(self.demo.worker.is_alive())
        self.assertEqual(self.demo.snapshot()["scene"]["step"], "ready")

    def test_reset_aborts_owned_order_and_preserves_history(self):
        self.demo.owned_order = "BOOTH-one"
        self.demo.orders = {"BOOTH-one": {"state": "running", "plc_id": "plc-1", "batch_id": "B-1"},
                            "manual": {"state": "completed"}}
        self.demo.plcs["plc-1"] = {"state": "faulted", "batch_id": "B-1", "phase": "rough"}

        def acknowledge(topic, payload, **kwargs):
            if topic.endswith("/orders/BOOTH-one"):
                self.assertTrue(json.loads(payload)["cancel"])
                return
            self.assertEqual(topic, "acme-mfg/plant-1/machining/line-1/plc-1/batch/B-1/phase/rough/command")
            self.assertEqual(json.loads(payload)["action"], "abort")
            self.demo.orders["BOOTH-one"]["state"] = "failed"

        self.publish.side_effect = acknowledge
        with patch("showcase.time.sleep"):
            self.demo.finish_reset()
        self.assertIn("manual", self.demo.orders)
        self.assertIn("BOOTH-one", self.demo.orders)
        self.assertEqual(self.demo.snapshot()["scene"]["step"], "ready")
        self.assertEqual(self.demo.reset_version, 1)
        self.assertEqual(self.publish.call_count, 2)

    def test_reset_does_not_falsely_report_success_without_ack(self):
        self.demo.owned_order = "BOOTH-unresolved"
        with patch("showcase.time.monotonic", side_effect=[0, 16]):
            self.demo.finish_reset()
        self.assertEqual(self.demo.snapshot()["scene"]["step"], "error")
        self.assertEqual(self.demo.owned_order, "BOOTH-unresolved")
        self.assertEqual(self.demo.reset_version, 0)

    def test_fault_controls_require_a_valid_machine_state(self):
        with self.assertRaises(ValueError):
            self.demo.fault("inject")
        self.demo.scene["plc_id"] = "plc-1"
        self.demo.plcs["plc-1"] = {"state": "idle"}
        with self.assertRaises(ValueError):
            self.demo.fault("inject")
        self.publish.assert_not_called()


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch("paho.mqtt.client.Client"):
            cls.module = importlib.import_module("app")
        from werkzeug.security import generate_password_hash
        cls.module.PASSWORD_HASH = generate_password_hash("rehearsal-only-password")
        cls.module.PUBLIC_BASE = "https://factory.example.test"
        cls.module.app.config.update(TESTING=True, SECRET_KEY="test-only-session-key", SESSION_COOKIE_SECURE=True)

    def setUp(self):
        self.client = self.module.app.test_client()
        self.module.login_attempts.clear()
        self.base = "https://factory.example.test"

    def login(self, **extra):
        response = self.client.post("/login", base_url=self.base,
                                    data={"password": "rehearsal-only-password", **extra})
        if response.status_code == 302:
            with self.client.session_transaction(base_url=self.base) as session:
                self.client.environ_base["HTTP_X_DEMO_CSRF"] = session["csrf"]
        return response

    def test_unauthenticated_controls_are_blocked(self):
        response = self.client.post("/api/demo/start", base_url=self.base, json={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/showcase", base_url=self.base).status_code, 302)

    def test_auth_gateway_accepts_session_without_consuming_product_token(self):
        response = self.login()
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("Secure", response.headers["Set-Cookie"])
        response = self.client.get("/auth/check", base_url=self.base,
                                   headers={"Authorization": "Bearer product-token"})
        self.assertEqual(response.status_code, 204)

    def test_redirect_cannot_escape_the_demo(self):
        for value in ("https://evil.example/", "//evil.example/", "/\\evil.example/"):
            response = self.login(next=value)
            self.assertEqual(response.headers["Location"], self.base + "/showcase")

    def test_bad_password_and_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.client.post("/login", base_url=self.base, data={"password": "bad"}).status_code, 401)
        self.assertEqual(self.login().status_code, 429)

    def test_cross_origin_and_non_json_actions_are_rejected(self):
        self.login()
        self.assertEqual(self.client.post("/api/demo/reset", base_url=self.base, json={},
                                          headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/demo/reset", base_url=self.base, data={}).status_code, 415)

    def test_input_validation_and_local_template_assets(self):
        self.login()
        for body in ([], {"loop": "true"}):
            self.assertEqual(self.client.post("/api/demo/start", base_url=self.base, json=body).status_code, 400)
        self.assertEqual(self.client.post("/api/place-order", base_url=self.base,
                                          json={"qty": "not a number", "recipe_id": "program-a"}).status_code, 400)
        page = self.client.get("/showcase", base_url=self.base).get_data(as_text=True)
        self.assertIn("/static/vendor/mqtt.min.js", page)
        self.assertNotIn("unpkg.com", page)
        self.assertNotIn("http://localhost", page)

    def test_live_home_and_legacy_tools_remain_accessible(self):
        self.login()
        with patch.dict("os.environ", {"SHOWCASE_HOME": "1"}):
            page = self.client.get("/", base_url=self.base)
            self.assertEqual(page.status_code, 200)
            self.assertIn('id="asset-hvac-1"', page.get_data(as_text=True))
        with patch.dict("os.environ", {"SHOWCASE_HOME": "0"}):
            page = self.client.get("/", base_url=self.base).get_data(as_text=True)
            self.assertIn("/static/architecture.svg", page)
        page = self.client.get("/tools", base_url=self.base).get_data(as_text=True)
        self.assertIn("/static/architecture.svg", page)


if __name__ == "__main__":
    unittest.main()
