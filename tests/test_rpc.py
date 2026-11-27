import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from bdh_sdk import alerts, displays, events, ha, rpc, settings, speakers, storage
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

    def test_ha_registry_goes_to_the_broker(self):
        self.broker.registry = {
            "entities": [{"entity_id": "climate.fp", "device_id": "d1", "platform": "napoleon"},
                         {"entity_id": "light.k", "device_id": "d2", "platform": "hue"}],
            "devices": [{"id": "d1", "name": "FP"}, {"id": "d2", "name": "Bulb"}],
            "unit_system": {"temperature": "°C"},
        }
        reg = ha.get_registry(platform="napoleon", instance="bdl")
        self.assertEqual(self.broker.ops("ha.registry")[-1], {"platform": "napoleon", "instance": "bdl"})
        self.assertEqual([e["entity_id"] for e in reg["entities"]], ["climate.fp"])
        self.assertEqual(reg["devices"], [{"id": "d1", "name": "FP"}])
        self.assertEqual(reg["unit_system"], {"temperature": "°C"})
        self.assertEqual(len(ha.get_registry()["entities"]), 2)
        self.assertEqual(self.broker.ops("ha.registry")[-1], {})

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

    SPEAKERS = [
        {"id": "sonos:RINCON_A", "legacy_id": "media_player.kitchen", "name": "Kitchen", "kind": "sonos",
         "caps": {"audio": True, "duck": True, "text": False, "volume": True}, "online": True},
        {"id": "alexa:ha:media_player.echo", "legacy_id": "media_player.echo", "name": "Echo", "kind": "alexa",
         "caps": {"audio": False, "duck": False, "text": True, "volume": False}, "online": True},
        {"id": "sonos:RINCON_B", "legacy_id": None, "name": "Deck", "kind": "sonos",
         "caps": {"audio": True, "duck": True, "text": False, "volume": True}, "online": False},
    ]

    def test_fake_broker_speaker_ops(self):
        self.broker.speakers = self.SPEAKERS
        self.assertEqual(speakers.list_speakers(), [
            ("sonos:RINCON_B", "Deck (Sonos) - offline"), ("media_player.echo", "Echo (Alexa)"),
            ("media_player.kitchen", "Kitchen (Sonos)")])
        self.assertEqual([d["id"] for d in speakers.devices()], [d["id"] for d in self.SPEAKERS])
        self.assertEqual(speakers.canonical(["media_player.kitchen", "sonos:RINCON_A", "media_player.gone"]),
                         ["sonos:RINCON_A", "media_player.gone"])
        self.assertEqual(
            speakers.announce("hi", ["media_player.kitchen", "media_player.echo", "sonos:RINCON_B", "x"],
                              detail=True),
            {"media_player.kitchen": "played", "media_player.echo": "played", "sonos:RINCON_B": "offline",
             "x": "failed"})
        self.assertEqual(speakers.announce("hi", ["media_player.kitchen", "x"]), ["x"])
        self.assertEqual(speakers.play(b"ID3abc", ["sonos:RINCON_A", "media_player.echo"], volume=30, detail=True),
                         {"sonos:RINCON_A": "played", "media_player.echo": "unsupported"})
        sent = self.broker.ops("speakers.play")[0]
        self.assertEqual((sent["audio"], sent["volume"], sent["night"]), ("SUQzYWJj", 30, False))
        with self.assertRaises(speakers.AnnounceError):
            speakers.play(b"ID3abc", ["media_player.echo"])

    def test_speaker_ops_need_the_speakers_permission(self):
        self.broker.permissions = ["alerts"]
        for call in (speakers.devices, lambda: speakers.play(b"ID3", ["sonos:RINCON_A"])):
            with self.assertRaises(rpc.PermissionDenied):
                call()
        self.broker.permissions = ["speakers"]
        self.broker.speakers = self.SPEAKERS
        self.assertEqual(speakers.play(b"ID3", ["sonos:RINCON_A"]), [])

    def test_play_checks_size_before_sending(self):
        with self.assertRaises(ValueError):
            speakers.play(b"x" * (speakers.MAX_PLAY_BYTES + 1), ["sonos:RINCON_A"])
        with self.assertRaises(ValueError):
            speakers.play(b"ID3", ["sonos:RINCON_A"], volume=101)
        self.assertEqual(self.broker.ops("speakers.play"), [])

    def test_older_hub_without_devices_or_detail(self):
        def gone(payload):
            raise FakeBroker.Error("unknown_op", "no op", 404)
        self.broker.on("speakers.devices", gone)
        self.broker.on("speakers.play", gone)
        self.broker.on("speakers.list", lambda p: [["media_player.k", "Kitchen (Sonos)"],
                                                   ["media_player.e", "Echo (Alexa) - offline"]])
        self.broker.on("speakers.announce", lambda p: ["media_player.e"])  # ignores detail
        self.assertEqual(speakers.devices(), [
            {"id": "media_player.k", "legacy_id": None, "name": "Kitchen", "kind": "sonos",
             "caps": {"audio": True, "duck": True, "text": False, "volume": True}, "online": True},
            {"id": "media_player.e", "legacy_id": None, "name": "Echo", "kind": "alexa",
             "caps": {"audio": False, "duck": False, "text": True, "volume": False}, "online": False}])
        self.assertEqual(speakers.announce("hi", ["media_player.k", "media_player.e"], detail=True),
                         {"media_player.k": "played", "media_player.e": "failed"})
        with self.assertRaises(speakers.AnnounceError):
            speakers.play(b"ID3", ["media_player.k"])

    def test_displays_status(self):
        now = datetime.now(timezone.utc)
        self.broker.heartbeats = {"192.168.1.99": now - timedelta(seconds=90)}
        result = displays.status()
        self.assertEqual([c["ip"] for c in result["clients"]], ["192.168.1.99"])
        self.assertGreaterEqual(result["clients"][0]["age_seconds"], 90)
        self.assertIsNotNone(result["clients"][0]["last_seen"].tzinfo)
        self.assertLessEqual(result["ready_since"], result["now"])

    def test_displays_status_needs_permission(self):
        self.broker.permissions = ["alerts"]
        with self.assertRaises(rpc.PermissionDenied):
            displays.status()
        self.broker.permissions = ["displays.read"]
        self.assertEqual(displays.status()["clients"], [])

    def test_stays_current(self):
        import hashlib

        from bdh_sdk import Stay, stays

        self.assertIsNone(stays.current())
        mt = timezone(timedelta(hours=-7))
        arrival = datetime(2026, 12, 20, 16, 0, tzinfo=mt)
        self.broker.stay = Stay("The Smiths", arrival, datetime.now(mt) + timedelta(days=2))
        cur = stays.current()
        self.assertEqual(cur.stay, self.broker.stay)
        self.assertEqual(cur.stay.arrival.utcoffset(), timedelta(hours=-7))
        self.assertFalse(cur.stay.owner)
        # The hub's identity "stay" key (hub/context_processors.stay_key).
        expected = hashlib.sha256(f"The Smiths|{arrival.isoformat()}".encode()).hexdigest()[:12]
        self.assertEqual(cur.key, expected)
        self.assertEqual(stays.key("x"), hashlib.sha256(b"x|").hexdigest()[:12])

        self.broker.stay = Stay("Gone", arrival, datetime.now(timezone.utc) - timedelta(minutes=1))
        self.assertIsNone(stays.current())

    def test_stays_current_needs_permission_and_raises(self):
        from bdh_sdk import Stay, stays

        self.broker.stay = Stay("Owners", owner=True)
        self.broker.permissions = ["displays.read"]
        with self.assertRaises(rpc.PermissionDenied):
            stays.current()
        self.broker.permissions = ["stays.read"]
        cur = stays.current()
        self.assertTrue(cur.stay.owner)
        self.assertIsNone(cur.stay.arrival)

        def boom(payload):
            raise FakeBroker.Error("internal", status=500)
        self.broker.handlers["stays.current"] = boom
        with self.assertRaises(rpc.BrokerError):
            stays.current()

    def test_uninstall_restores_in_process_defaults(self):
        rpc.uninstall()
        self.assertFalse(rpc.installed())
        state = storage.app_state("weather")
        state.set("k", 1)
        self.assertEqual(state.get("k"), 1)
        self.assertEqual(self.broker.ops("state.set"), [])


class _ScriptedClient:
    """Answers each call with the next scripted outcome (an exception, a callable or a value)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.lock = threading.Lock()

    def call(self, op, payload):
        with self.lock:
            self.calls.append((op, payload))
            outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(op, payload)
        return outcome


class DesiredTests(unittest.TestCase):
    def desired(self, client, **kw):
        kw.setdefault("delay", 0)
        kw.setdefault("backoff", 0.01)
        kw.setdefault("max_backoff", 0.02)
        d = rpc._Desired(client, **kw)
        self.addCleanup(d.stop)
        return d

    def test_retries_until_the_hub_takes_it(self):
        down = rpc.BrokerError("broker unreachable", "unavailable")
        client = _ScriptedClient(down, rpc.BrokerError("busy", "unavailable", 503), down)
        d = self.desired(client)
        with self.assertLogs("bdh_sdk.rpc", "WARNING"):
            d.set("events", "events.subscribe", {"patterns": ["ha/#"]})
            self.assertTrue(d.join())
        self.assertEqual(client.calls, [("events.subscribe", {"patterns": ["ha/#"]})] * 4)

    def test_newest_value_wins_while_retrying(self):
        d = None

        def first_fails(op, payload):
            # A newer value arrives while the hub is down.
            d.set("events", op, {"patterns": ["b/#"]})
            raise rpc.BrokerError("down", "unavailable")

        client = _ScriptedClient(first_fails)
        d = self.desired(client)
        with self.assertLogs("bdh_sdk.rpc", "WARNING"):
            d.set("events", "events.subscribe", {"patterns": ["a/#"]})
            self.assertTrue(d.join())
        self.assertEqual([p for _, p in client.calls], [{"patterns": ["a/#"]}, {"patterns": ["b/#"]}])

    def test_value_changed_mid_send_is_sent_again(self):
        d = None
        client = _ScriptedClient(lambda op, p: d.set("events", op, {"patterns": ["b/#"]}))
        d = self.desired(client)
        d.set("events", "events.subscribe", {"patterns": ["a/#"]})
        self.assertTrue(d.join())
        self.assertEqual([p for _, p in client.calls], [{"patterns": ["a/#"]}, {"patterns": ["b/#"]}])

    def test_burst_is_coalesced(self):
        client = _ScriptedClient()
        d = self.desired(client, delay=0.2)
        for n in range(5):
            d.set("events", "events.subscribe", {"patterns": [f"t/{n}"]})
        self.assertTrue(d.join())
        self.assertEqual(client.calls, [("events.subscribe", {"patterns": ["t/4"]})])

    def test_refusals_are_not_retried(self):
        for exc in (rpc.PermissionDenied("no", "forbidden", 403),
                    rpc.BrokerError("bad pattern", "invalid", 400)):
            with self.subTest(code=exc.code):
                client = _ScriptedClient(exc)
                d = self.desired(client)
                with self.assertLogs("bdh_sdk.rpc", "ERROR"):
                    d.set("events", "events.subscribe", {"patterns": ["x"]})
                    self.assertTrue(d.join())
                self.assertEqual(len(client.calls), 1)

    def test_empty_set_is_sent(self):
        client = _ScriptedClient()
        d = self.desired(client)
        d.set("events", "events.subscribe", {"patterns": []})
        self.assertTrue(d.join())
        self.assertEqual(client.calls, [("events.subscribe", {"patterns": []})])

    def test_keys_are_independent(self):
        client = _ScriptedClient()
        d = self.desired(client, delay=0.1)
        d.set("a", "op.a", {"v": 1})
        d.set("b", "op.b", {"v": 2})
        self.assertTrue(d.join())
        self.assertCountEqual(client.calls, [("op.a", {"v": 1}), ("op.b", {"v": 2})])

    def test_stop_drops_pending(self):
        client = _ScriptedClient()
        d = self.desired(client, delay=0.2)
        d.set("events", "events.subscribe", {"patterns": ["a"]})
        d.stop()
        d.set("events", "events.subscribe", {"patterns": ["b"]})
        self.assertTrue(d.join())
        time.sleep(0.3)
        self.assertEqual(client.calls, [])


class InstalledSubscribeTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker(install=True).start()
        self.addCleanup(self.broker.stop)
        self.addCleanup(events.reset)

    def test_subscriptions_survive_a_hub_outage(self):
        rpc._desired.backoff = rpc._desired.max_backoff = 0.01
        down = {"on": True}

        def subscribe(payload):
            if down["on"]:
                raise FakeBroker.Error("busy", status=503)
        self.broker.on("events.subscribe", subscribe)
        with self.assertLogs("bdh_sdk.rpc", "WARNING"):
            events.subscribe("cameras/#", lambda e: None)
            self.assertFalse(rpc.flush(timeout=1.0))
        down["on"] = False
        self.assertTrue(rpc.flush())
        self.assertEqual(self.broker.ops("events.subscribe")[-1], {"patterns": ["cameras/#"]})

    def test_alerts_are_not_held_up_by_a_failing_subscribe(self):
        rpc._desired.backoff = rpc._desired.max_backoff = 0.05
        down = {"on": True}

        def subscribe(payload):
            if down["on"]:
                raise FakeBroker.Error("busy", status=503)
        self.broker.on("events.subscribe", subscribe)
        with self.assertLogs("bdh_sdk.rpc", "WARNING"):
            events.subscribe("cameras/#", lambda e: None)
            deadline = time.monotonic() + 3
            while len(self.broker.ops("events.subscribe")) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            alerts.push(alerts.Alert(app="cameras", key="door", title="Door", embed_url="/c/"))
            self.assertTrue(rpc._background.join(2))
            self.assertEqual(len(self.broker.ops("alerts.push")), 1)
            self.assertFalse(rpc._desired.join(0))
            down["on"] = False
            self.assertTrue(rpc.flush())


class ManifestPermissionTests(unittest.TestCase):
    """FakeBroker.for_plugin refuses what the hub would for the app's manifest."""

    REG = {
        "entities": [
            {"entity_id": "camera.door", "device_id": "d1", "area_id": None, "platform": "unifiprotect"},
            {"entity_id": "binary_sensor.door_motion", "device_id": "d1", "area_id": "porch",
             "platform": "unifiprotect"},
            {"entity_id": "lock.front", "device_id": "d2", "area_id": "hall", "platform": "august"},
        ],
        "devices": [{"id": "d1", "name": "Door cam", "area_id": "entry"},
                    {"id": "d2", "name": "Lock", "area_id": "hall"}],
        "areas": [{"area_id": "porch", "name": "Porch"}, {"area_id": "entry", "name": "Entry"},
                  {"area_id": "hall", "name": "Hall"}],
        "unit_system": {},
    }

    def setUp(self):
        spec = mock.Mock(id="cams", permissions=("ha.read:camera.*", "ha.read:binary_sensor.*",
                                                 "events.subscribe:+/state/#"))
        self.broker = FakeBroker.for_plugin(spec, install=True).start()
        self.addCleanup(self.broker.stop)
        self.addCleanup(events.reset)
        self.broker.instances = [{"id": "ha", "name": "MI", "site": "mi", "default": True},
                                 {"id": "ha_bdl", "name": "SP", "site": "bdl", "default": False}]

    def test_ws_commands_need_the_admin_grant(self):
        with self.assertRaises(rpc.PermissionDenied):
            ha.ws_commands({"type": "config/entity_registry/list"})

    def test_registry_is_cut_to_readable_entities_with_their_devices_and_areas(self):
        self.broker.registries["ha_bdl"] = self.REG
        reg = ha.get_registry(instance="ha_bdl")
        self.assertEqual([e["entity_id"] for e in reg["entities"]],
                         ["camera.door", "binary_sensor.door_motion"])
        self.assertEqual([d["id"] for d in reg["devices"]], ["d1"])
        self.assertEqual(sorted(a["area_id"] for a in reg["areas"]), ["entry", "porch"])
        self.assertEqual(ha.get_registry()["entities"], [])  # default instance is empty

    def test_unknown_instance_is_invalid(self):
        with self.assertRaises(rpc.BrokerError) as cm:
            ha.get_registry(instance="nope")
        self.assertEqual(cm.exception.code, "invalid")

    def test_states_and_calls_follow_read_and_call_grants(self):
        self.broker.ha_states["ha"] = [{"entity_id": "camera.door", "state": "idle"},
                                       {"entity_id": "lock.front", "state": "locked"}]
        self.assertEqual([s["entity_id"] for s in ha.get_all_states()], ["camera.door"])
        with self.assertRaises(rpc.PermissionDenied):
            ha.get_entity_state("lock.front")
        with self.assertRaises(rpc.PermissionDenied):
            ha.call_service("lock", "unlock", "lock.front")

    def test_other_apps_state_and_alerts_are_refused(self):
        client = rpc.Client(self.broker.url, self.broker.token, retries=0)
        client.call("state.set", {"app": "cams", "key": "k", "value": 1})
        with self.assertRaises(rpc.PermissionDenied):
            client.call("state.get", {"app": "timers", "key": "k"})
        with self.assertRaises(rpc.PermissionDenied):
            client.call("alerts.push", {"app": "cams", "key": "k"})
        self.assertEqual(client.call("settings.get", {"app": "hub", "key": "units"})["found"], False)
        with self.assertRaises(rpc.BrokerError) as cm:
            client.call("no.such_op", {})
        self.assertEqual(cm.exception.status, 404)


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
