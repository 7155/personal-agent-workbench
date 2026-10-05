from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from rag_ime.local_calendar import resolve_calendar_timezone


class LocalCalendarTests(unittest.TestCase):
    def test_explicit_zone_and_user_configuration_have_priority(self):
        with patch.dict(os.environ, {"RAG_IME_TIMEZONE": "Asia/Shanghai", "TZ": "UTC"}):
            self.assertEqual(resolve_calendar_timezone().key, "Asia/Shanghai")
            self.assertEqual(resolve_calendar_timezone("America/Los_Angeles").key, "America/Los_Angeles")

    def test_invalid_user_configuration_never_silently_changes_the_calendar(self):
        with patch.dict(os.environ, {"RAG_IME_TIMEZONE": "Mars/Olympus_Mons", "TZ": "UTC"}):
            with self.assertRaisesRegex(ValueError, "unknown IANA timezone"):
                resolve_calendar_timezone()

    def test_no_key_local_offset_uses_the_os_iana_identity(self):
        local = timezone(timedelta(hours=-7), "PDT")
        with patch.dict(os.environ, {"RAG_IME_TIMEZONE": "", "TZ": ""}), \
             patch("rag_ime.local_calendar.datetime") as clock, \
             patch("rag_ime.local_calendar._system_timezone_names", return_value=["America/Los_Angeles"]):
            clock.now.return_value.astimezone.return_value.tzinfo = local
            zone = resolve_calendar_timezone()
        self.assertEqual(zone.key, "America/Los_Angeles")
        self.assertEqual(datetime(2026, 1, 1, tzinfo=zone).utcoffset(), timedelta(hours=-8))
        self.assertEqual(datetime(2026, 7, 1, tzinfo=zone).utcoffset(), timedelta(hours=-7))

    def test_unknown_abbreviation_requires_configuration_instead_of_guessing(self):
        local = timezone(timedelta(hours=8), "CST")
        with patch.dict(os.environ, {"RAG_IME_TIMEZONE": "", "TZ": ""}), \
             patch("rag_ime.local_calendar.datetime") as clock, \
             patch("rag_ime.local_calendar._system_timezone_names", return_value=[]):
            clock.now.return_value.astimezone.return_value.tzinfo = local
            with self.assertRaisesRegex(ValueError, "set RAG_IME_TIMEZONE"):
                resolve_calendar_timezone()

    def test_utc_and_colon_prefixed_system_names_are_supported(self):
        for name, expected in (("UTC0", "UTC"), (":America/Los_Angeles", "America/Los_Angeles")):
            with self.subTest(name=name), patch.dict(os.environ, {"RAG_IME_TIMEZONE": "", "TZ": name}):
                self.assertEqual(resolve_calendar_timezone().key, expected)

    def test_os_zoneinfo_paths_preserve_the_region(self):
        with patch("rag_ime.local_calendar.Path") as path, patch("rag_ime.local_calendar.datetime") as clock:
            clock.now.return_value.astimezone.return_value.tzinfo = timezone(timedelta(hours=8), "CST")
            path.return_value.resolve.return_value = Path("/private/var/db/timezone/zoneinfo/Asia/Shanghai")
            path.return_value.read_text.side_effect = FileNotFoundError
            with patch.dict(os.environ, {"RAG_IME_TIMEZONE": "", "TZ": ""}):
                self.assertEqual(resolve_calendar_timezone().key, "Asia/Shanghai")

    def test_calendar_consumers_agree_in_isolated_timezone_processes(self):
        root = Path(__file__).resolve().parents[1]
        program = """
import json
from rag_ime.activity_timeline import DailyActivityTimelineStore
from rag_ime.personal_context import local_date_for_timestamp, local_day_bounds_ms
from datetime import datetime
store = DailyActivityTimelineStore('unused-calendar-test.sqlite')
timestamp = 1784318400000
day = local_date_for_timestamp(timestamp)
assert day == datetime.fromtimestamp(timestamp / 1000, store.timezone).date().isoformat()
start, end = local_day_bounds_ms(day)
assert start <= timestamp < end
print(json.dumps({'zone': store.timezone_name, 'day': day}))
"""
        for host, configured, expected_zone, expected_day in (
            ("UTC", "", "UTC", "2026-07-17"),
            ("UTC", "Asia/Shanghai", "Asia/Shanghai", "2026-07-18"),
            ("America/Los_Angeles", "", "America/Los_Angeles", "2026-07-17"),
            ("Asia/Shanghai", "UTC", "UTC", "2026-07-17"),
        ):
            with self.subTest(host=host, configured=configured):
                environment = {**os.environ, "TZ": host, "RAG_IME_TIMEZONE": configured}
                result = subprocess.run([sys.executable, "-c", program], cwd=root, env=environment,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), {"zone": expected_zone, "day": expected_day})

    def test_daily_bounds_follow_spring_and_fall_dst_transitions(self):
        from rag_ime.personal_context import local_date_for_timestamp, local_day_bounds_ms
        with patch.dict(os.environ, {"RAG_IME_TIMEZONE": "America/Los_Angeles", "TZ": "UTC"}):
            for day, hours in (("2026-03-08", 23), ("2026-11-01", 25), ("2026-07-17", 24)):
                with self.subTest(day=day):
                    start, end = local_day_bounds_ms(day)
                    self.assertEqual(end - start, hours * 60 * 60 * 1000)
                    self.assertEqual(local_date_for_timestamp(start), day)
                    self.assertEqual(local_date_for_timestamp(end - 1), day)
                    self.assertNotEqual(local_date_for_timestamp(start - 1), day)
                    self.assertNotEqual(local_date_for_timestamp(end), day)


if __name__ == "__main__":
    unittest.main()
