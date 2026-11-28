import asyncio
import unittest

from bdh_sdk.alerts import CHIMES, MAX_SPEAK, SILENT, Alert, apush, dismiss, push, set_sender


def make(**kw):
    return Alert(**{"app": "cameras", "key": "doorbell:front", "title": "Someone's at the door",
                    "embed_url": "/cameras/popup/front/", **kw})


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        set_sender(self.sent.append)
        self.addCleanup(set_sender, None)

    def test_push_and_apush_reach_the_sender(self):
        alert = make()
        push(alert)
        asyncio.run(apush(alert))
        self.assertEqual(self.sent, [alert, alert])
        self.assertEqual(alert.as_dict()["ttl"], 60)

    def test_dismiss_reaches_the_dismisser(self):
        closed = []
        set_sender(self.sent.append, lambda app, key: closed.append((app, key)))
        dismiss("timers", "timer:3")
        self.assertEqual(closed, [("timers", "timer:3")])
        set_sender(self.sent.append)
        dismiss("timers", "timer:3")  # no dismisser: a no-op
        with self.assertRaises(ValueError):
            dismiss("Timers", "timer:3")

    def test_speak_is_short_text(self):
        self.assertEqual(make(speak="Egg timer has expired", resound=True).as_dict()["speak"],
                         "Egg timer has expired")
        for bad in ("x" * (MAX_SPEAK + 1), None):
            with self.subTest(speak=bad), self.assertRaises(ValueError):
                make(speak=bad)

    def test_admins_only_is_a_flag(self):
        self.assertFalse(make().as_dict()["admins_only"])
        self.assertTrue(make(admins_only=True).as_dict()["admins_only"])
        for bad in (1, "yes", None):
            with self.subTest(admins_only=bad), self.assertRaises(ValueError):
                make(admins_only=bad)

    def test_without_a_sender_push_is_a_no_op(self):
        set_sender(None)
        push(make())
        self.assertEqual(self.sent, [])

    def test_rejects_bad_alerts(self):
        for kw in ({"app": "Cameras"}, {"key": ""}, {"title": ""}, {"ttl": 0}, {"ttl": 601},
                   {"ttl": True}, {"embed_url": "https://evil.example/"}, {"embed_url": "//evil.example/"},
                   {"embed_url": "/\\evil.example"}, {"embed_url": "cameras/popup/"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                make(**kw)
        with self.assertRaises(TypeError):
            push({"app": "cameras"})

    def test_sound_is_a_chime_silent_or_a_hub_path(self):
        for snd in (None, SILENT, *CHIMES, "/static/cameras/ring.mp3"):
            with self.subTest(sound=snd):
                self.assertEqual(make(sound=snd).as_dict()["sound"], snd)
        for snd in ("", "foghorn", "https://evil.example/a.mp3", "//evil.example/a.mp3", "/\\evil", 3):
            with self.subTest(sound=snd), self.assertRaises(ValueError):
                make(sound=snd)


if __name__ == "__main__":
    unittest.main()