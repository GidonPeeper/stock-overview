"""Choices made in Settings (backend/prefs.py): saved in settings.json, each
falling back to its old environment variable."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import clock, fx, harvest, prefs


class PrefsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for p in (mock.patch.object(prefs, "DATA_DIR", self.dir),
                  mock.patch.object(prefs, "_cache", [None, None]),
                  mock.patch.dict(os.environ, {"TAX_RESIDENCE": "NL", "DISPLAY_CURRENCY": "GBP",
                                               "DASHBOARD_TZ": "Europe/Amsterdam"})):
            p.start()
            self.addCleanup(p.stop)

    def test_falls_back_to_the_environment(self):
        got = prefs.get()
        self.assertEqual((got["country"], got["currency"], got["timezone"]), ("NL", "GBP", "Europe/Amsterdam"))
        self.assertEqual(got["chosen"], [])
        self.assertEqual(harvest.residence(), "NL")
        self.assertEqual(fx.display_currency(), "GBP")
        self.assertEqual(str(clock.tz()), "Europe/Amsterdam")

    def test_dismissed_warnings_are_saved_with_the_settings(self):
        prefs.update({"currency": "usd"})
        self.assertEqual(prefs.set_dismissed("backup_old", True, "2026-11-27"), {"backup_old": "2026-11-27"})
        prefs.set_dismissed("notice:1a2b3c", True, "2026-11-28")
        self.assertEqual(prefs.value("currency"), "USD")                   # the choices are kept
        self.assertEqual(prefs.get()["chosen"], ["currency"])
        self.assertEqual(prefs.set_dismissed("backup_old", False, "2026-11-29"), {"notice:1a2b3c": "2026-11-28"})
        for bad in ("", "x" * 201, "<script>", None):
            with self.assertRaises(ValueError):
                prefs.set_dismissed(bad, True, "2026-11-27")
        with mock.patch.object(prefs, "_MAX_DISMISSED", 2):                # the oldest are forgotten
            prefs.set_dismissed("a", True, "2026-11-01")
            prefs.set_dismissed("b", True, "2026-11-30")
        self.assertEqual(set(prefs.dismissed()), {"notice:1a2b3c", "b"})

    def test_a_choice_wins_and_is_kept(self):
        prefs.update({"country": "uk", "currency": "usd", "timezone": "America/New_York",
                      "color_base": "#0A7A5C"})
        self.assertEqual(harvest.residence(), "UK")              # stored as GB
        self.assertEqual(prefs.value("country"), "GB")
        self.assertEqual(fx.display_currency(), "USD")
        self.assertEqual(str(clock.tz()), "America/New_York")
        self.assertEqual(prefs.value("color_base"), "#0a7a5c")
        self.assertEqual(prefs.get()["chosen"], ["color_base", "country", "currency", "timezone"])
        prefs.update({"country": "DE"})                           # no tax cards for Germany yet
        self.assertEqual(harvest.residence(), "OTHER")
        prefs.update({"country": None})                           # forget it: the environment again
        self.assertEqual(prefs.value("country"), "NL")

    def test_no_country(self):
        prefs.update({"country": ""})
        self.assertEqual(prefs.value("country"), "")
        self.assertEqual(harvest.residence(), "OTHER")

    def test_bad_values_are_refused_and_nothing_is_saved(self):
        for bad in ({"currency": "JPY"}, {"country": "Netherlands"}, {"timezone": "Mars/Base"},
                    {"color_base": "blue"}, {"theme": "dark"}):
            with self.assertRaises(ValueError):
                prefs.update(bad)
        self.assertFalse((self.dir / prefs.FILE).exists())

    def test_a_damaged_file_falls_back_and_the_next_save_repairs_it(self):
        (self.dir / prefs.FILE).write_text("{not json")
        self.assertEqual(prefs.value("currency"), "GBP")
        prefs.update({"currency": "EUR"})
        self.assertEqual(prefs.value("currency"), "EUR")


if __name__ == "__main__":
    unittest.main()
