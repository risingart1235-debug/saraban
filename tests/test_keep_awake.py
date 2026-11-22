"""On Render the server pings its own /healthz during school hours so it never sleeps mid-work."""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

try:
    from web import main as webmain
    from core import THAI_TZ
except (ImportError, RuntimeError):     # server packages not installed
    webmain = None


@unittest.skipIf(webmain is None, "server packages not installed")
class WorkHoursTests(unittest.TestCase):
    def at(self, y, m, d, hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=THAI_TZ)

    def test_weekdays_seven_to_five(self):
        self.assertTrue(webmain.in_work_hours(self.at(2026, 11, 23, 7, 0)))     # Monday
        self.assertTrue(webmain.in_work_hours(self.at(2026, 11, 27, 16, 59)))   # Friday
        self.assertFalse(webmain.in_work_hours(self.at(2026, 11, 23, 6, 59)))
        self.assertFalse(webmain.in_work_hours(self.at(2026, 11, 23, 17, 0)))
        self.assertFalse(webmain.in_work_hours(self.at(2026, 11, 22, 10, 0)))   # Sunday

    def test_hours_can_be_set(self):
        with patch.dict("os.environ", {"SARABAN_AWAKE_HOURS": "06:30-18:00"}):
            self.assertTrue(webmain.in_work_hours(self.at(2026, 11, 23, 6, 30)))
            self.assertTrue(webmain.in_work_hours(self.at(2026, 11, 23, 17, 30)))
        with patch.dict("os.environ", {"SARABAN_AWAKE_HOURS": "nonsense"}):
            self.assertTrue(webmain.in_work_hours(self.at(2026, 11, 23, 8, 0)))


if __name__ == "__main__":
    unittest.main()
