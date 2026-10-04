from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Shanghai"
_CN_NUMBER = r"[零〇一二两三四五六七八九十百\d]+"


@dataclass(frozen=True)
class TemporalRange:
    label: str
    matched_text: str
    start_ms: int
    end_ms: int  # Exclusive.

    def payload(self) -> dict[str, object]:
        return {
            "label": self.label,
            "matchedText": self.matched_text,
            "startMs": self.start_ms,
            "endMs": self.end_ms,
        }


@dataclass(frozen=True)
class TemporalQuery:
    original_query: str
    cleaned_query: str
    timezone: str
    ranges: tuple[TemporalRange, ...]

    @property
    def matched(self) -> bool:
        return bool(self.ranges)


def parse_temporal_query(
    query: str,
    *,
    now: datetime | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> TemporalQuery:
    tz = ZoneInfo(timezone)
    anchor = (now or datetime.now(tz)).astimezone(tz)
    working = str(query)
    ranges: list[TemporalRange] = []
    protected_ranges: dict[str, str] = {}

    def consume(
        pattern: str,
        handler,
        *,
        protect_rejected: bool = False,
        preserve_accepted: bool = False,
    ) -> None:
        nonlocal working
        regex = re.compile(pattern, re.IGNORECASE)
        pieces: list[str] = []
        cursor = 0
        for match in regex.finditer(working):
            result = handler(match, anchor, tz)
            if (result is None and not protect_rejected) or (result is not None and preserve_accepted):
                continue
            pieces.append(working[cursor:match.start()])
            if result is None:
                token = f"\ue000rejected-date-range-{len(protected_ranges)}\ue001"
                while token in working:
                    token += "\ue000"
                protected_ranges[token] = match.group(0)
                pieces.append(token)
            else:
                ranges.append(result)
                pieces.append(" ")
            cursor = match.end()
        if cursor:
            pieces.append(working[cursor:])
            working = "".join(pieces)

    range_endpoint = r"(\d+)[-/.年](\d+)[-/.月](\d+)日?"
    consume(
        rf"(?<!\d){range_endpoint}\s*(?:到|至|~|—|-)\s*{range_endpoint}(?!\d)",
        _explicit_date_range,
        protect_rejected=True,
    )
    consume(
        r"(?<!\d)(\d+)[-/.年](\d+)[-/.月](\d+)日?(?!\d)",
        _explicit_date,
        protect_rejected=True,
        preserve_accepted=True,
    )
    consume(r"上周([一二三四五六日天])", _last_weekday)
    consume(rf"({_CN_NUMBER})\s*(?:天|日)前", _days_ago)
    consume(rf"({_CN_NUMBER})\s*周前", _weeks_ago)
    consume(rf"({_CN_NUMBER})\s*个?月前", _months_ago)
    consume(rf"(?:最近|近|过去)\s*({_CN_NUMBER})\s*(?:天|日)", _recent_days)
    consume(rf"(?:最近|近|过去)\s*({_CN_NUMBER})\s*周", _recent_weeks)
    consume(rf"(?:最近|近|过去)\s*({_CN_NUMBER})\s*个?月", _recent_months)
    consume(r"\b(today)\b|今天|今日|当天", lambda match, *_: _day_range(anchor.date(), match.group(0), tz))
    consume(r"\b(yesterday)\b|昨天|昨日", lambda match, *_: _day_range(anchor.date() - timedelta(days=1), match.group(0), tz))
    consume(r"大前天", lambda match, *_: _day_range(anchor.date() - timedelta(days=3), match.group(0), tz))
    consume(r"前天", lambda match, *_: _day_range(anchor.date() - timedelta(days=2), match.group(0), tz))
    consume(r"上周|last\s+week", lambda match, *_: _week_range(anchor.date() - timedelta(weeks=1), match.group(0), tz))
    consume(r"本周|这周|this\s+week", lambda match, *_: _week_range(anchor.date(), match.group(0), tz))
    consume(r"上个?月|last\s+month", lambda match, *_: _month_range(_shift_month(anchor.date(), -1), match.group(0), tz))
    consume(r"本月|这个月|this\s+month", lambda match, *_: _month_range(anchor.date(), match.group(0), tz))
    consume(r"最近|近期|recently|lately", _default_recent)
    consume(
        r"(?<!\d)(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?!\d)",
        _explicit_date,
    )

    unique: dict[tuple[int, int], TemporalRange] = {}
    for item in ranges:
        unique.setdefault((item.start_ms, item.end_ms), item)
    for token, expression in protected_ranges.items():
        working = working.replace(token, expression)
    cleaned = re.sub(r"\s+", " ", working).strip(" ，,。？?！!")
    return TemporalQuery(
        original_query=str(query),
        cleaned_query=cleaned,
        timezone=timezone,
        ranges=tuple(sorted(unique.values(), key=lambda item: item.start_ms)),
    )


def _day_range(target: date, matched: str, tz: ZoneInfo) -> TemporalRange:
    start = datetime.combine(target, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return _range(matched, target.isoformat(), start, end)


def _week_range(target: date, matched: str, tz: ZoneInfo) -> TemporalRange:
    monday = target - timedelta(days=target.weekday())
    start = datetime.combine(monday, time.min, tzinfo=tz)
    end = start + timedelta(days=7)
    label = f"{monday.isoformat()} 至 {(monday + timedelta(days=6)).isoformat()}"
    return _range(matched, label, start, end)


def _month_range(target: date, matched: str, tz: ZoneInfo) -> TemporalRange:
    first = target.replace(day=1)
    next_month = _shift_month(first, 1)
    start = datetime.combine(first, time.min, tzinfo=tz)
    end = datetime.combine(next_month, time.min, tzinfo=tz)
    return _range(matched, f"{first.year:04d}-{first.month:02d}", start, end)


def _rolling_days(days: int, matched: str, anchor: datetime, tz: ZoneInfo) -> TemporalRange:
    days = max(1, min(days, 3660))
    start_date = anchor.date() - timedelta(days=days - 1)
    start = datetime.combine(start_date, time.min, tzinfo=tz)
    end = datetime.combine(anchor.date() + timedelta(days=1), time.min, tzinfo=tz)
    label = f"{start_date.isoformat()} 至 {anchor.date().isoformat()}"
    return _range(matched, label, start, end)


def _range(matched: str, label: str, start: datetime, end: datetime) -> TemporalRange:
    return TemporalRange(
        label=label,
        matched_text=matched,
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(end.timestamp() * 1000),
    )


def _days_ago(match: re.Match[str], anchor: datetime, tz: ZoneInfo) -> TemporalRange:
    return _day_range(anchor.date() - timedelta(days=_number(match.group(1))), match.group(0), tz)


def _weeks_ago(match: re.Match[str], anchor: datetime, tz: ZoneInfo) -> TemporalRange:
    return _week_range(anchor.date() - timedelta(weeks=_number(match.group(1))), match.group(0), tz)


def _months_ago(match: re.Match[str], anchor: datetime, tz: ZoneInfo) -> TemporalRange:
    return _month_range(_shift_month(anchor.date(), -_number(match.group(1))), match.group(0), tz)


def _recent_days(match: re.Match[str], anchor: datetime, tz: ZoneInfo) -> TemporalRange:
    return _rolling_days(_number(match.group(1)), match.group(0), anchor, tz)


def _recent_weeks(match: re.Match[str], anchor: datetime, tz: ZoneInfo) -> TemporalRange:
    return _rolling_days(_number(match.group(1)) * 7, match.group(0), anchor, tz)


def _recent_months(match: re.Match[str], anchor: datetime, tz: ZoneInfo) -> TemporalRange:
    months = max(1, min(_number(match.group(1)), 120))
    start_date = _shift_month(anchor.date(), -months)
    start = datetime.combine(start_date, time.min, tzinfo=tz)
    end = datetime.combine(anchor.date() + timedelta(days=1), time.min, tzinfo=tz)
    return _range(match.group(0), f"{start_date.isoformat()} 至 {anchor.date().isoformat()}", start, end)


def _default_recent(match: re.Match[str], anchor: datetime, tz: ZoneInfo) -> TemporalRange:
    days = 7 if match.group(0) in {"近期", "lately"} else 5
    return _rolling_days(days, match.group(0), anchor, tz)


def _last_weekday(match: re.Match[str], anchor: datetime, tz: ZoneInfo) -> TemporalRange:
    weekday = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[match.group(1)]
    this_monday = anchor.date() - timedelta(days=anchor.date().weekday())
    return _day_range(this_monday - timedelta(days=7) + timedelta(days=weekday), match.group(0), tz)


def _explicit_date(match: re.Match[str], _anchor: datetime, tz: ZoneInfo) -> TemporalRange | None:
    fields = match.groups()
    if len(fields[0]) != 4 or any(not 1 <= len(field) <= 2 for field in fields[1:]):
        return None
    try:
        target = date(int(fields[0]), int(fields[1]), int(fields[2]))
        return _day_range(target, match.group(0), tz)
    except (ValueError, OverflowError, OSError):
        return None


def _explicit_date_range(match: re.Match[str], _anchor: datetime, tz: ZoneInfo) -> TemporalRange | None:
    fields = match.groups()
    if any(len(fields[index]) != 4 for index in (0, 3)) or any(
        not 1 <= len(fields[index]) <= 2 for index in (1, 2, 4, 5)
    ):
        return None
    try:
        start_date = date(int(fields[0]), int(fields[1]), int(fields[2]))
        end_date = date(int(fields[3]), int(fields[4]), int(fields[5]))
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        start = datetime.combine(start_date, time.min, tzinfo=tz)
        end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=tz)
        return _range(match.group(0), f"{start_date.isoformat()} 至 {end_date.isoformat()}", start, end)
    except (ValueError, OverflowError, OSError):
        return None


def _shift_month(target: date, months: int) -> date:
    month_index = target.year * 12 + target.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(target.day, calendar.monthrange(year, month)[1]))


def _number(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        tens, ones = value.split("十", 1)
        return (digits.get(tens, 1) * 10) + digits.get(ones, 0)
    return digits.get(value, 0)
