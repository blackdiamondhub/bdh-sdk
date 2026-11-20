import unittest
from datetime import date, datetime, timezone

from bdh_sdk import (
    LIVE, PHOTOS, SAVER, STATS, Background, PluginSpec, SaverImage, SaverSource, SeasonHint, Stat,
)
from bdh_sdk.runtime.views import describe


class ProvidesTests(unittest.TestCase):
    def test_stat_needs_exactly_one_of_value_or_time(self):
        Stat("temp", "Temperature", value="3°C")
        Stat("sunrise", "Sunrise", time=datetime(2026, 1, 1, 8, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            Stat("temp", "Temperature")
        with self.assertRaises(ValueError):
            Stat("temp", "Temperature", value="3°C",
                 time=datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_stat_time_must_be_aware(self):
        with self.assertRaises(ValueError):
            Stat("sunrise", "Sunrise", time=datetime(2026, 1, 1, 8))

    def test_background_refresh_and_season(self):
        with self.assertRaises(ValueError):
            Background("cam", "Cam", "/cam.jpg", refresh_seconds=0)
        SeasonHint("winter", "Lifts are open")
        with self.assertRaises(ValueError):
            SeasonHint("summer", "Warm")

    def test_season_opens_on_must_be_a_date(self):
        self.assertEqual(SeasonHint("winter", "Opening day", date(2026, 11, 26)).opens_on,
                         date(2026, 11, 26))
        for bad in ("2026-11-26", datetime(2026, 11, 26, tzinfo=timezone.utc)):
            with self.assertRaises(ValueError):
                SeasonHint("winter", "Opening day", bad)

    def test_spec_provides_and_stat_keys(self):
        spec = PluginSpec(id="weather", app="weather",
                          provides={STATS: "weather.provide.stats"},
                          stat_keys=(("temp", "Temperature"),))
        self.assertEqual(spec.provides, {"stats": "weather.provide.stats"})
        hash(spec)  # still usable as a dict key / in sets
        self.assertEqual(PluginSpec(id="x", app="x").provides, {})
        with self.assertRaises(ValueError):
            PluginSpec(id="x", app="x", stat_keys=(("a", "A"), ("a", "B")))
        with self.assertRaises(ValueError):
            PluginSpec(id="x", app="x", stat_keys=(("a", ""),))

    def test_saver_source_validation(self):
        SaverSource("cams", "Mountain cams", "Live webcams from around Sun Peaks", LIVE)
        SaverSource("family-album_2", "Family album", kind=PHOTOS)
        for bad in (dict(key="Cams", title="T"), dict(key="-x", title="T"),
                    dict(key="a b", title="T"), dict(key="", title="T"),
                    dict(key="x", title=" "), dict(key="x", title="T" * 33),
                    dict(key="x", title="T", description="d" * 81),
                    dict(key="x", title="T", kind="video")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SaverSource(**bad)

    def test_saver_image_validation(self):
        SaverImage("cams", "village", "/webcams/cam/village.jpg", refresh_seconds=60)
        for bad in (dict(id="", image_url="/a.jpg"),
                    dict(id="a", image_url="https://evil.example/a.jpg"),
                    dict(id="a", image_url="//evil.example/a.jpg"),
                    dict(id="a", image_url="a.jpg"),
                    dict(id="a", image_url="/a.jpg", refresh_seconds=0),
                    dict(id="a", image_url="/a.jpg", refresh_seconds=True),
                    dict(id="a", image_url="/a.jpg", refresh_seconds=1.5)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SaverImage("cams", **bad)

    def test_spec_saver_sources(self):
        cams = SaverSource("cams", "Mountain cams", "Live webcams", LIVE)
        spec = PluginSpec(id="webcams", app="webcams", provides={SAVER: "w.saver"},
                          saver_sources=[cams])
        self.assertEqual(spec.saver_sources, (cams,))
        hash(spec)
        self.assertEqual(describe(spec)["saver_sources"], [
            {"key": "cams", "title": "Mountain cams", "description": "Live webcams", "kind": "live"}])
        self.assertEqual(describe(PluginSpec(id="x", app="x"))["saver_sources"], [])
        for bad in (dict(saver_sources=[cams, SaverSource("cams", "Other")]),
                    dict(saver_sources=[SaverSource(f"s{i}", "S") for i in range(9)]),
                    dict(saver_sources=[{"key": "cams", "title": "T"}]),
                    dict(saver_sources="cams"),
                    dict(provides={SAVER: "w.saver"})):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PluginSpec(id="x", app="x", **bad)


if __name__ == "__main__":
    unittest.main()
