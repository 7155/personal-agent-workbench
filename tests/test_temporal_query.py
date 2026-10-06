from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from rag_ime.temporal_query import parse_temporal_query


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 13, 16, 30, tzinfo=TZ)  # Monday.


class TemporalQueryTests(unittest.TestCase):
    def test_calendar_relative_ranges(self) -> None:
        cases = {
            "今天做了什么": ("2026-07-13", "2026-07-14"),
            "昨天做了什么": ("2026-07-12", "2026-07-13"),
            "前天做了什么": ("2026-07-11", "2026-07-12"),
            "上周做了什么": ("2026-07-06", "2026-07-13"),
            "本周做了什么": ("2026-07-13", "2026-07-20"),
            "上个月做了什么": ("2026-06-01", "2026-07-01"),
        }
        for query, (start, end) in cases.items():
            with self.subTest(query=query):
                parsed = parse_temporal_query(query, now=NOW)
                self.assertEqual(len(parsed.ranges), 1)
                self.assertEqual(datetime.fromtimestamp(parsed.ranges[0].start_ms / 1000, TZ).date().isoformat(), start)
                self.assertEqual(datetime.fromtimestamp(parsed.ranges[0].end_ms / 1000, TZ).date().isoformat(), end)

    def test_recent_and_dynamic_ranges(self) -> None:
        recent = parse_temporal_query("最近干了什么", now=NOW)
        three_days = parse_temporal_query("最近三天改了什么", now=NOW)
        two_weeks_ago = parse_temporal_query("两周前做过什么", now=NOW)
        last_friday = parse_temporal_query("上周五处理了什么", now=NOW)

        self.assertEqual(recent.ranges[0].label, "2026-07-09 至 2026-07-13")
        self.assertEqual(three_days.ranges[0].label, "2026-07-11 至 2026-07-13")
        self.assertEqual(two_weeks_ago.ranges[0].label, "2026-06-29 至 2026-07-05")
        self.assertEqual(last_friday.ranges[0].label, "2026-07-10")
        self.assertEqual(three_days.cleaned_query, "改了什么")

    def test_multiple_time_expressions_are_deduplicated(self) -> None:
        parsed = parse_temporal_query("比较昨天和上周五的输入法工作", now=NOW)

        self.assertEqual([item.label for item in parsed.ranges], ["2026-07-10", "2026-07-12"])
        self.assertEqual(parsed.cleaned_query, "比较 和 的输入法工作")

    def test_rejected_explicit_dates_remain_query_text(self) -> None:
        queries = (
            "查找 2026-02-30 的记录",
            "查找 2025/02/29 的记录",
            "查找 0000-01-01 的记录",
            "查找 2026-13-01 的记录",
            "查找 2026-07-130 的记录",
            "查找 2026-007-13 的记录",
            "查找 026-07-13 的记录",
            "查找 12026-07-13 的记录",
            "查找 9999-12-31 的记录",
        )
        for query in queries:
            with self.subTest(query=query):
                parsed = parse_temporal_query(query, now=NOW)
                self.assertEqual(parsed.cleaned_query, query)
                self.assertEqual(parsed.ranges, ())

    def test_rejected_chinese_dates_do_not_become_relative_day_phrases(self) -> None:
        queries = (
            "查找 2026年2月30日前的记录",
            "查找 2026-07-130日前的记录",
            "查找 2026年7月9999999999日前 的记录",
        )
        for query in queries:
            with self.subTest(query=query):
                parsed = parse_temporal_query(query, now=NOW)
                self.assertEqual(parsed.cleaned_query, query)
                self.assertEqual(parsed.ranges, ())

        query = "查找 2026年2月30日前和昨天的记录"
        parsed = parse_temporal_query(query, now=NOW)
        self.assertEqual(parsed.cleaned_query, "查找 2026年2月30日前和 的记录")
        self.assertEqual([item.label for item in parsed.ranges], ["2026-07-12"])
        self.assertEqual(parse_temporal_query("30日前的记录", now=NOW).ranges[0].label, "2026-06-13")

    def test_rejected_date_ranges_are_atomic(self) -> None:
        expressions = (
            "2026-02-30 到 2026-03-02",
            "2026-07-130 至 2026-03-02",
            "2026-07-13~2026-003-02",
            "2026-07-13 — 2026-02-30",
            "12026-07-13 到 2026-07-14",
            "2026-07-13 到 026-07-14",
            "9999-12-30 到 9999-12-31",
            "9999-12-31 到 9999-12-30",
        )
        for expression in expressions:
            query = f"比较 {expression} 的记录"
            with self.subTest(expression=expression):
                parsed = parse_temporal_query(query, now=NOW)
                self.assertEqual(parsed.cleaned_query, query)
                self.assertEqual(parsed.ranges, ())

    def test_rejected_ranges_are_atomic_for_all_existing_separators(self) -> None:
        for separator in ("到", "至", "~", "—", "-"):
            for gap in ("", " "):
                query = f"比较 2026-02-30{gap}{separator}{gap}2026-03-02 的记录"
                with self.subTest(query=query):
                    parsed = parse_temporal_query(query, now=NOW)
                    self.assertEqual(parsed.cleaned_query, query)
                    self.assertEqual(parsed.ranges, ())

    def test_rejected_range_does_not_hide_independent_expressions(self) -> None:
        rejected = "2026-02-30 到 2026-03-02"
        query = f"比较 {rejected} 和昨天的记录"
        parsed = parse_temporal_query(query, now=NOW)
        self.assertEqual(parsed.cleaned_query, f"比较 {rejected} 和 的记录")
        self.assertEqual([item.label for item in parsed.ranges], ["2026-07-12"])
        self.assertEqual([item.matched_text for item in parsed.ranges], ["昨天"])

        # The valid endpoint remains eligible when it appears independently.
        query = f"比较 {rejected} 和 2026-03-02 的记录"
        parsed = parse_temporal_query(query, now=NOW)
        self.assertEqual(parsed.cleaned_query, f"比较 {rejected} 和 的记录")
        self.assertEqual([item.label for item in parsed.ranges], ["2026-03-02"])

    def test_valid_explicit_date_ranges_are_inclusive_and_reversible(self) -> None:
        cases = (
            ("查找 2026-03-02到2026-03-04", "2026-03-02 至 2026-03-04", "2026-03-02到2026-03-04"),
            ("查找 2026/03/04 — 2026/03/02", "2026-03-02 至 2026-03-04", "2026/03/04 — 2026/03/02"),
        )
        for query, label, matched_text in cases:
            with self.subTest(query=query):
                item = parse_temporal_query(query, now=NOW).ranges[0]
                self.assertEqual(item.label, label)
                self.assertEqual(item.matched_text, matched_text)
                start = datetime.fromtimestamp(item.start_ms / 1000, TZ)
                end = datetime.fromtimestamp(item.end_ms / 1000, TZ)
                self.assertEqual(start.date().isoformat(), "2026-03-02")
                self.assertEqual(end.date().isoformat(), "2026-03-05")
                self.assertEqual((item.end_ms - item.start_ms) // 86_400_000, 3)

    def test_explicit_date_separator_forms_remain_supported(self) -> None:
        for token in ("2026/7/13", "2026.7.13", "2026年7月13日"):
            with self.subTest(token=token):
                parsed = parse_temporal_query(f"查找 {token}", now=NOW)
                self.assertEqual([item.label for item in parsed.ranges], ["2026-07-13"])
                self.assertEqual(parsed.cleaned_query, "查找")

    def test_relative_expression_keeps_deduplication_priority(self) -> None:
        for query in ("今天 与 2026-07-13", "2026-07-13 与 今天"):
            with self.subTest(query=query):
                parsed = parse_temporal_query(query, now=NOW)
                self.assertEqual(len(parsed.ranges), 1)
                self.assertEqual(parsed.ranges[0].matched_text, "今天")
                self.assertEqual(parsed.cleaned_query, "与")
                self.assertEqual(parsed.ranges[0].payload()["matchedText"], "今天")

    def test_multiple_rejected_spans_preserve_normalization_and_valid_dates(self) -> None:
        query = "  ，检查 2026-02-30 和 2026-04-31 至 2026-05-02 与 2026-06-01 的数据？！  "
        parsed = parse_temporal_query(query, now=NOW)
        self.assertEqual(parsed.cleaned_query, "检查 2026-02-30 和 2026-04-31 至 2026-05-02 与 的数据")
        self.assertEqual([item.label for item in parsed.ranges], ["2026-06-01"])

    def test_adjacent_hyphen_and_one_day_ranges_remain_inclusive(self) -> None:
        for expression, days in (("2026-07-12-2026-07-13", 2), ("2026/7/13~2026/7/13", 1)):
            with self.subTest(expression=expression):
                parsed = parse_temporal_query(f"核对 {expression} 日志", now=NOW)
                self.assertEqual(len(parsed.ranges), 1)
                self.assertEqual(parsed.ranges[0].end_ms - parsed.ranges[0].start_ms, days * 86400000)
                self.assertEqual(parsed.cleaned_query, "核对 日志")

    def test_explicit_dates_use_local_midnights_across_dst(self) -> None:
        tz = ZoneInfo("America/New_York")
        cases = (("2026-03-08", 23), ("2026-11-01", 25))
        for token, hours in cases:
            with self.subTest(token=token):
                item = parse_temporal_query(
                    token,
                    now=NOW,
                    timezone="America/New_York",
                ).ranges[0]
                start = datetime.fromtimestamp(item.start_ms / 1000, tz)
                end = datetime.fromtimestamp(item.end_ms / 1000, tz)
                self.assertEqual((start.hour, start.minute, start.second), (0, 0, 0))
                self.assertEqual((end.hour, end.minute, end.second), (0, 0, 0))
                self.assertEqual((item.end_ms - item.start_ms) // 3_600_000, hours)


if __name__ == "__main__":
    unittest.main()
