import os
import unittest
from datetime import datetime, timezone
from unittest import mock

import requests

from bdh_sdk import ha

ENV = {"HOMEASSISTANT_URL": "http://ha.test:8123", "HOMEASSISTANT_ACCESS_TOKEN": "tok"}


def _resp(status=200, body=None, text=""):
    r = mock.Mock(status_code=status, text=text, content=b"js")
    r.json.return_value = body
    return r


@mock.patch.dict(os.environ, ENV)
class HAClientTests(unittest.TestCase):
    @mock.patch("bdh_sdk.ha.requests.get")
    def test_get_entity_state_uses_env_connection(self, get):
        get.return_value = _resp(body={"entity_id": "light.a", "state": "on"})
        self.assertEqual(ha.get_entity_state("light.a")["state"], "on")
        url = get.call_args.args[0]
        self.assertEqual(url, "http://ha.test:8123/api/states/light.a")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer tok")

    @mock.patch("bdh_sdk.ha.requests.get", side_effect=requests.ConnectionError("down"))
    def test_unreachable_ha_degrades(self, get):
        self.assertIsNone(ha.get_entity_state("light.a"))
        self.assertEqual(ha.get_entity_states(["light.a"]), {})
        self.assertEqual(ha.get_history(["light.a"], datetime.now(timezone.utc)), {})
        with self.assertRaises(ha.HAError):
            ha.get_all_states()

    @mock.patch("bdh_sdk.ha.requests.get")
    def test_get_entity_states_filters(self, get):
        get.return_value = _resp(body=[{"entity_id": "a"}, {"entity_id": "b"}])
        self.assertEqual(list(ha.get_entity_states(["b"])), ["b"])

    @mock.patch("bdh_sdk.ha.requests.get")
    def test_history_minimal_response(self, get):
        get.return_value = _resp(body=[[
            {"entity_id": "sensor.t", "state": "1", "last_changed": "t1"},
            {"state": "2", "last_updated": "t2"},
        ], []])
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(ha.get_history(["sensor.t"], start), {"sensor.t": [("t1", "1"), ("t2", "2")]})

    @mock.patch("bdh_sdk.ha.requests.post")
    def test_call_service(self, post):
        post.return_value = _resp()
        self.assertEqual(ha.call_service("light", "turn_on", "light.a", {"brightness": 5}), (True, None))
        self.assertEqual(post.call_args.kwargs["json"], {"entity_id": "light.a", "brightness": 5})
        post.return_value = _resp(status=401, text="nope")
        ok, err = ha.call_service("light", "turn_on", "light.a")
        self.assertFalse(ok)
        self.assertIn("401", err)

    @mock.patch("bdh_sdk.ha.requests.post")
    def test_call_service_without_entity(self, post):
        post.return_value = _resp()
        ha.call_service("notify", "alexa_media", None, {"message": "hi"})
        self.assertEqual(post.call_args.kwargs["json"], {"message": "hi"})

    @mock.patch("bdh_sdk.ha.requests.post")
    def test_upload_media(self, post):
        post.return_value = _resp(body={"media_content_id": "media-source://media_source/local/x/a.wav"})
        mid = ha.upload_media("/x/", "a.wav", b"RIFF", "audio/wav")
        self.assertEqual(mid, "media-source://media_source/local/x/a.wav")
        self.assertEqual(post.call_args.kwargs["data"], {"media_content_id": "media-source://media_source/local/x"})
        self.assertEqual(post.call_args.kwargs["files"]["file"], ("a.wav", b"RIFF", "audio/wav"))
        post.return_value = _resp(status=401, text="admin only")
        with self.assertRaises(ha.HAError):
            ha.upload_media("x", "a.wav", b"", "audio/wav")

class _Provider:
    def instances(self):
        return [{"id": "mi", "name": "Home", "site": "MI", "default": True},
                {"id": "bdl", "name": "Lodge", "site": "BDL", "default": False}]

    def connection(self, instance):
        conns = {None: ("http://mi:8123", "t-mi"), "mi": ("http://mi:8123", "t-mi"),
                 "bdl": ("http://bdl:8123", "t-bdl")}
        if instance not in conns:
            raise ha.HAError(f"unknown instance {instance}")
        return conns[instance]


class HAInstanceTests(unittest.TestCase):
    def setUp(self):
        ha.set_provider(_Provider())
        self.addCleanup(ha.set_provider, None)

    @mock.patch("bdh_sdk.ha.requests.get")
    def test_instance_selects_connection(self, get):
        get.return_value = _resp(body={"entity_id": "light.a", "state": "on"})
        ha.get_entity_state("light.a", instance="bdl")
        self.assertEqual(get.call_args.args[0], "http://bdl:8123/api/states/light.a")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer t-bdl")
        ha.get_entity_state("light.a")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer t-mi")

    @mock.patch("bdh_sdk.ha.requests.get")
    def test_unknown_instance(self, get):
        self.assertIsNone(ha.get_entity_state("light.a", instance="nope"))
        self.assertEqual(ha.call_service("light", "turn_on", "light.a", instance="nope")[0], False)
        with self.assertRaises(ha.HAError):
            ha.get_all_states(instance="nope")
        get.assert_not_called()

    def test_instances(self):
        self.assertEqual([i["id"] for i in ha.instances()], ["mi", "bdl"])
        ha.set_provider(None)
        self.assertEqual(ha.instances(), [{"id": "ha", "name": "Home Assistant", "site": "", "default": True}])


class HARegistryTests(unittest.TestCase):
    ENTITIES = [
        {"entity_id": "climate.fp", "device_id": "d1", "platform": "napoleon", "translation_key": "t",
         "disabled_by": None, "name": None, "original_name": "Fireplace", "unique_id": "u1"},
        {"entity_id": "light.k", "device_id": "d2", "platform": "hue", "disabled_by": None},
    ]
    DEVICES = [{"id": "d1", "name": "FP", "name_by_user": None, "manufacturer": "Napoleon",
                "model": "X", "serial_number": "s"},
               {"id": "d2", "name": "Bulb"}, {"id": "d3", "name": "Unused"}]
    CONFIG = {"unit_system": {"temperature": "°F", "length": "mi"}, "location_name": "Home"}

    @mock.patch("bdh_sdk.ha.ws_commands")
    def test_trims_and_filters_by_platform(self, ws):
        ws.return_value = [self.ENTITIES, self.DEVICES, self.CONFIG]
        reg = ha.get_registry(platform="napoleon", instance="bdl")
        self.assertEqual(ws.call_args.kwargs, {"instance": "bdl"})
        self.assertEqual(reg["entities"], [
            {"entity_id": "climate.fp", "device_id": "d1", "platform": "napoleon",
             "translation_key": "t", "disabled_by": None, "name": None, "original_name": "Fireplace"}])
        self.assertEqual(reg["devices"], [{"id": "d1", "name": "FP", "name_by_user": None,
                                           "manufacturer": "Napoleon", "model": "X"}])
        self.assertEqual(reg["unit_system"], {"temperature": "°F", "length": "mi"})

    @mock.patch("bdh_sdk.ha.ws_commands")
    def test_without_platform_keeps_all_referenced_devices(self, ws):
        ws.return_value = [self.ENTITIES, self.DEVICES, None]
        reg = ha.get_registry()
        self.assertEqual([e["entity_id"] for e in reg["entities"]], ["climate.fp", "light.k"])
        self.assertEqual([d["id"] for d in reg["devices"]], ["d1", "d2"])
        self.assertEqual(reg["unit_system"], {})



if __name__ == "__main__":
    unittest.main()
