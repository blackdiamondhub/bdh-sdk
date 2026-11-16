import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from bdh_sdk import alerts, events, ha, rpc, settings, speakers, storage
from bdh_sdk.testing import FakeBroker


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker().start()
        self.addCleanup(self.broker.stop)
        self.client = rpc.Client(self.broker.url, "test-token", backoff=0)

    def test_bad_token_is_a_broker_error(self):
        with self.assertRaises(rpc.BrokerError) as cm:
            rpc.Client(self.broker.url, "nope").call("settings.get", {})
        self.assertEqual((cm.exception.code, cm.exception.status), ("unauthorized", 401))

    def test_ungranted_op_is_permission_denied(self):
        self.broker.grants = ["settings.*"]
        with self.assertRaises(rpc.PermissionDenied):
            self.client.call("ha.get_entity_state", {"entity_id": "lock.front"})

    def test_error_code_is_kept(self):
        def fail(payload):
            raise FakeBroker.Error("rate_limited", "slow down", 429)
        self.broker.on("state.set", fail)
        with self.assertRaises(rpc.BrokerError) as cm:
            self.client.call("state.set", {"app": "a", "key": "k", "value": 1})
        self.assertEqual((cm.exception.code, cm.exception.status, str(cm.exception)),
                         ("rate_limited", 429, "slow down"))

    def test_reads_retry_on_503_but_writes_do_not(self):
        attempts = {"settings.get": 0, "state.set": 0}

        def flaky(op):
            def handler(payload):
                attempts[op] += 1
                if attempts[op] == 1:
                    raise FakeBroker.Error("busy", status=503)
                return {"found": False}
            return handler

        for op in attempts:
            self.broker.on(op, flaky(op))
        self.assertEqual(self.client.call("settings.get", {}), {"found": False})
        with self.assertRaises(rpc.BrokerError) as cm:
            self.client.call("state.set", {})
        self.assertEqual(cm.exception.code, "unavailable")
        self.assertEqual(attempts, {"settings.get": 2, "state.set": 1})

    def test_unreachable_broker(self):
        url = self.broker.url
        self.broker.stop()
        with self.assertRaises(rpc.BrokerError) as cm:
            rpc.Client(url, "t", retries=1, backoff=0, timeout=2).call("settings.get", {})
        self.assertEqual(cm.exception.code, "unavailable")


class InstalledTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker(install=True).start()
        self.addCleanup(self.broker.stop)
        self.addCleanup(events.reset)

    def test_settings_and_state_go_to_the_broker(self):
        self.broker.settings[("weather", "units")] = "metric"
        self.assertEqual(settings.app_setting("weather", "units", "imperial"), "metric")
        self.assertEqual(settings.app_setting("weather", "other", "x"), "x")

        state = storage.app_state("weather")
        state.set("last", {"t": 1})
        self.assertEqual(state.get("last"), {"t": 1})
        self.assertIn("last", state)
        self.assertEqual(state.keys(), ["last"])
        state.delete("last")
        self.assertEqual(state.get("last", "gone"), "gone")

    def test_settings_cache_falls_back_when_broker_goes_away(self):
        lookup = rpc.SettingsLookup(rpc.client(), ttl=0)
        self.broker.settings[("a", "k")] = 5
        self.assertEqual(lookup("a", "k"), 5)

        def down(payload):
            raise FakeBroker.Error("busy", status=503)
        self.broker.on("settings.get", down)
        rpc.client().backoff = 0
        with self.assertLogs("bdh_sdk.rpc", "WARNING"):
            self.assertEqual(lookup("a", "k"), 5)
        with self.assertRaises(rpc.BrokerError):
            lookup("a", "never-seen")

    def test_alerts_and_events_are_sent_in_the_background(self):
        alerts.push(alerts.Alert(app="cameras", key="door", title="Door", embed_url="/cameras/"))
        alerts.dismiss("cameras", "door")
        events.subscribe("cameras/#", lambda e: None)
        events.publish("cameras/doorbell/front", {"who": "x"})
        self.assertTrue(rpc.flush())
        self.assertEqual(self.broker.ops("alerts.push")[0]["key"], "door")
        self.assertEqual(self.broker.ops("alerts.dismiss"), [{"app": "cameras", "key": "door"}])
        self.assertEqual(self.broker.ops("events.subscribe")[-1], {"patterns": ["cameras/#"]})
        published = self.broker.ops("events.publish")
        self.assertEqual([(p["topic"], p["data"]) for p in published],
                         [("cameras/doorbell/front", {"who": "x"})])

    def test_delivered_events_are_not_forwarded_back(self):
        got = []
        events.subscribe("ha/#", lambda e: got.append(e.topic))
        events.deliver("ha/state/light.x", {"s": "on"}, time.time())
        events.flush()
        rpc.flush()
        self.assertEqual(got, ["ha/state/light.x"])
        self.assertEqual(self.broker.ops("events.publish"), [])

    def test_ha_calls(self):
        self.broker.on("ha.get_entity_state", lambda p: {"entity_id": p["entity_id"], "state": "on"})
        self.broker.on("ha.call_service", lambda p: [True, None])
        self.broker.on("ha.get_history", lambda p: {"sensor.t": [["2026-01-01T00:00:00+00:00", 1.5]]})
        self.broker.on("ha.get_tts", lambda p: {"audio": "aGk=", "content_type": "audio/mpeg"})

        self.assertEqual(ha.get_entity_state("light.a")["state"], "on")
        self.assertEqual(ha.call_service("light", "turn_on", "light.a", {"brightness": 9}),
                         (True, None))
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(rpc.HABackend(rpc.client()).get_history(["sensor.t"], start),
                         {"sensor.t": [("2026-01-01T00:00:00+00:00", 1.5)]})
        self.assertEqual(self.broker.ops("ha.get_history")[0]["start"], start.isoformat())
        self.assertEqual(rpc.HABackend(rpc.client()).get_tts("tts.x", "hi"), (b"hi", "audio/mpeg"))
        self.assertEqual(self.broker.ops("ha.call_service")[0],
                         {"domain": "light", "service": "turn_on", "entity_id": "light.a",
                          "extra_data": {"brightness": 9}})

    def test_ha_instance_is_forwarded(self):
        self.broker.on("ha.get_all_states", lambda p: [{"entity_id": "x", "i": p.get("instance")}])
        self.assertEqual(ha.get_all_states(instance="bdl"), [{"entity_id": "x", "i": "bdl"}])
        self.assertEqual(ha.get_all_states(), [{"entity_id": "x", "i": None}])
        self.assertEqual(self.broker.ops("ha.get_all_states")[-1], {})
        self.broker.on("ha.call_service", lambda p: [True, None])
        ha.call_service("light", "turn_on", "light.a", instance="bdl")
        self.assertEqual(self.broker.ops("ha.call_service")[-1]["instance"], "bdl")
        self.assertEqual(ha.instances()[0]["id"], "ha")

    def test_ha_instances_on_old_hub(self):
        def missing(payload):
            raise FakeBroker.Error("unknown_op", "no op", 404)
        self.broker.on("ha.instances", missing)
        self.assertEqual([i["id"] for i in ha.instances()], ["ha"])

    def test_ha_errors_map_to_ha_error(self):
        def fail(payload):
            raise FakeBroker.Error("ha_error", "HA said no", 500)
        self.broker.on("ha.get_all_states", fail)
        with self.assertRaises(ha.HAError):
            rpc.HABackend(rpc.client()).get_all_states()

    def test_speakers(self):
        self.broker.on("speakers.list", lambda p: [["media_player.k", "Kitchen"]])
        self.broker.on("speakers.announce", lambda p: ["media_player.k"])
        self.assertEqual(speakers.list_speakers(), [("media_player.k", "Kitchen")])
        self.assertEqual(speakers.announce("hello", ["media_player.k"]), ["media_player.k"])
        sent = self.broker.ops("speakers.announce")[0]
        self.assertEqual((sent["message"], sent["speakers"]), ("hello", ["media_player.k"]))

        def bad(payload):
            raise FakeBroker.Error("announce_error", "no speakers")
        self.broker.on("speakers.announce", bad)
        with self.assertRaises(speakers.AnnounceError):
            speakers.announce("hello", ["media_player.k"])

    def test_uninstall_restores_in_process_defaults(self):
        rpc.uninstall()
        self.assertFalse(rpc.installed())
        state = storage.app_state("weather")
        state.set("k", 1)
        self.assertEqual(state.get("k"), 1)
        self.assertEqual(self.broker.ops("state.set"), [])


class InstallFromEnvTests(unittest.TestCase):
    def test_install_reads_the_environment(self):
        with FakeBroker(token="env-token") as broker, mock.patch.dict(
                "os.environ", {rpc.BROKER_ENV: broker.url, rpc.TOKEN_ENV: "env-token"}):
            self.assertTrue(rpc.configured())
            try:
                client = rpc.install()
                self.assertIs(rpc.client(), client)
                broker.settings[("a", "k")] = 1
                self.assertEqual(settings.app_setting("a", "k"), 1)
            finally:
                rpc.uninstall()
        with self.assertRaises(rpc.BrokerError):
            rpc.client()


if __name__ == "__main__":
    unittest.main()
