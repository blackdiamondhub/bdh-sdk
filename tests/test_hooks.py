import unittest
from datetime import datetime, timezone
from unittest import mock

from bdh_sdk import hooks
from bdh_sdk.plugins import CHOICE, Action, PluginSpec, Setting, Shortcut, Status, StatusRow, Stay
from bdh_sdk.provides import BACKGROUNDS, SEASON, STATS, Background, SeasonHint, Stat

HERE = __name__
seen = []


def status():
    return Status("2 open", level="warn", refresh=1, rows=(
        StatusRow("front", "Front door", image="javascript:alert(1)",
                  actions=(Action("close", "fa-solid fa-x"), Action("bad id!", "x"))),
        StatusRow("no good", "skipped"),
        StatusRow("garage", "Garage", image="/cameras/snap.jpg", dimmed=True),
    ))


def status_for(mode):
    return f"mode {mode}"


def shortcuts():
    return [Shortcut("movie", "Movie", "fa-film", modes=frozenset({"owner", "guest"})),
            Shortcut("bad id", "x", "y")]


def stay():
    return Stay("Smith", owner=True)


def run_shortcut(shortcut_id):
    seen.append(shortcut_id)
    return "done"


def run_action(row_id, action_id, mode=None):
    seen.append((row_id, action_id, mode))


def stats():
    when = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    return [Stat("snow", "Snow", value=12), Stat("snow", "dup", value=1),
            Stat("lift", "First lift", time=when), Stat("other", "nope", value=1)]


def backgrounds():
    return [Background("a", "A", "/bg/a.jpg"), Background("a", "dup", "/x"),
            Background("b", "B", "")]


def season():
    return SeasonHint("winter", "snow")


def broken():
    return 42


def spec(**kw):
    return PluginSpec(id="demo", app="demo", **kw)


class RunTests(unittest.TestCase):
    def setUp(self):
        seen.clear()

    def test_status(self):
        out = hooks.run(spec(status=f"{HERE}.status"), "status")
        self.assertEqual(out["level"], "warn")
        self.assertEqual(out["refresh"], hooks.MIN_REFRESH)
        self.assertEqual([r["id"] for r in out["rows"]], ["front", "garage"])
        front, garage = out["rows"]
        self.assertEqual(front["image"], "")
        self.assertEqual([a["id"] for a in front["actions"]], ["close"])
        self.assertEqual((garage["image"], garage["dimmed"]), ("/cameras/snap.jpg", True))

    def test_status_countdown(self):
        with mock.patch("time.time", return_value=0):
            out = hooks.status_value(Status("Egg {left}", ends_at=1000.0, rows=(
                StatusRow("egg", "Egg", detail="{left} · ends 6:30 PM", ends_at=3700.5),
                StatusRow("tea", "Tea", detail="Done", ends_at=50.0))))
        self.assertEqual((out["text"], out["text_tpl"], out["ends_at"]), ("Egg 16:40", "Egg {left}", 1000.0))
        egg, tea = out["rows"]
        self.assertEqual((egg["detail"], egg["detail_tpl"], egg["ends_at"]),
                         ("1:01:41 · ends 6:30 PM", "{left} · ends 6:30 PM", 3700.5))
        self.assertEqual(tea["detail"], "Done")
        self.assertNotIn("ends_at", tea)
        self.assertEqual(hooks.fmt_left(-5), "0:00")

    def test_status_gets_mode_when_it_asks(self):
        out = hooks.run(spec(status=f"{HERE}.status_for"), "status", mode="guest")
        self.assertEqual(out, {"text": "mode guest", "level": "info"})

    def test_bad_return_type(self):
        with self.assertRaises(TypeError):
            hooks.run(spec(status=f"{HERE}.broken"), "status")

    def test_shortcuts_and_stay(self):
        out = hooks.run(spec(shortcuts=f"{HERE}.shortcuts"), "shortcuts")
        self.assertEqual(out, [{"id": "movie", "title": "Movie", "icon": "fa-film",
                                "order": 100, "modes": ["guest", "owner"]}])
        self.assertEqual(hooks.run(spec(stay=f"{HERE}.stay"), "stay"),
                         {"guest": "Smith", "arrival": None, "checkout": None, "owner": True})

    def test_run_shortcut_and_action(self):
        s = spec(run_shortcut=f"{HERE}.run_shortcut", run_action=f"{HERE}.run_action")
        self.assertEqual(hooks.run(s, "run_shortcut", shortcut_id="movie"), "done")
        self.assertIsNone(hooks.run(s, "run_action", row_id="r", action_id="a", mode="owner"))
        self.assertEqual(seen, ["movie", ("r", "a", "owner")])

    def test_provide(self):
        s = spec(provides={STATS: f"{HERE}.stats", BACKGROUNDS: f"{HERE}.backgrounds",
                           SEASON: f"{HERE}.season"},
                 stat_keys=(("snow", "Snow"), ("lift", "First lift")))
        self.assertEqual(hooks.run(s, "provide", capability=STATS), {
            "snow": {"label": "Snow", "order": 100, "value": "12"},
            "lift": {"label": "First lift", "order": 100, "time": "2026-01-01T12:00:00Z"},
        })
        self.assertEqual([b["id"] for b in hooks.run(s, "provide", capability=BACKGROUNDS)], ["a"])
        self.assertEqual(hooks.run(s, "provide", capability=SEASON),
                         {"season": "winter", "reason": "snow"})

    def test_setting_options(self):
        s = spec(settings=(Setting("units", "Units", CHOICE, choices=(("m", "Metric"), "imperial")),))
        self.assertEqual(hooks.run(s, "setting_options", key="units"),
                         [["m", "Metric"], ["imperial", "imperial"]])

    def test_missing_hooks(self):
        s = spec()
        for hook, kw in [("status", {}), ("card", {}), ("run_action", {}),
                         ("provide", {"capability": STATS}), ("setting_options", {"key": "x"}),
                         ("nope", {})]:
            with self.subTest(hook), self.assertRaises(hooks.HookNotFound):
                hooks.run(s, hook, **kw)


if __name__ == "__main__":
    unittest.main()
