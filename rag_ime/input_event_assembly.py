from __future__ import annotations

import json
import re
import sqlite3
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Iterable, Mapping, Sequence

from .daily_planner import estimate_tokens
from .input_quality import (
    FINALIZED_INPUT_SOURCE,
    INTERNAL_EVENT_SOURCES,
    RIME_FRAGMENT_SOURCE,
    assess_input_text,
    source_context_enabled,
)
from .text_utils import compact_whitespace, stable_text_hash


INPUT_CONTEXT_SCHEMA_VERSION = "rag-ime.recent-complete-input-context.v1"

_RIME_FRAGMENT_SOURCE = RIME_FRAGMENT_SOURCE
_SENTENCE_END_RE = re.compile(r"[。！？!?；;.]$")
_EXPLICIT_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})日?(?!\d)")


@dataclass(frozen=True)
class TemporalWindow:
    label: str
    start_ms: int
    end_ms: int
    explicit: bool

    def payload(self) -> dict[str, object]:
        return {
            "label": self.label,
            "startMs": self.start_ms,
            "endMs": self.end_ms,
            "explicit": self.explicit,
        }


def resolve_temporal_window(text: str, *, now: datetime | None = None) -> TemporalWindow | None:
    value = compact_whitespace(text)
    if not value:
        return None
    current = now.astimezone() if now is not None and now.tzinfo else (now or datetime.now().astimezone())
    today = current.date()
    explicit_match = _EXPLICIT_DATE_RE.search(value)
    if explicit_match:
        try:
            target = date(*(int(part) for part in explicit_match.groups()))
        except ValueError:
            target = today
        return _day_window(target, label=target.isoformat(), template=current)
    if "前天" in value:
        return _day_window(today - timedelta(days=2), label="前天", template=current)
    if "昨天" in value or "昨日" in value:
        return _day_window(today - timedelta(days=1), label="昨天", template=current)
    if "今天" in value or "今日" in value:
        return _day_window(today, label="今天", template=current)
    if any(marker in value for marker in ("上周", "上星期", "上礼拜")):
        this_week = today - timedelta(days=today.weekday())
        return _date_range_window(
            this_week - timedelta(days=7),
            this_week,
            label="上周",
            template=current,
        )
    if any(marker in value for marker in ("本周", "这周", "这个星期", "这星期")):
        start = today - timedelta(days=today.weekday())
        return _date_range_window(start, start + timedelta(days=7), label="本周", template=current)
    return None


def recent_complete_input_context(
    conn: sqlite3.Connection,
    *,
    project: str = "",
    app: str = "",
    query_text: str = "",
    baseline_records: int = 20,
    max_records: int = 80,
    char_budget: int = 12000,
    token_budget: int = 4096,
    reserved_tokens: int = 1024,
    raw_row_limit: int = 500,
) -> dict[str, object]:
    available_tokens = max(256, int(token_budget) - max(0, int(reserved_tokens)))
    requested_count = max(1, min(200, int(max_records)))
    requested_chars = max(1, min(100_000, int(char_budget)))
    temporal = resolve_temporal_window(query_text)
    where = ["COALESCE(s.deleted, 0) = 0"]
    params: list[object] = []
    if project:
        where.append("(e.project = ? OR e.project = '')")
        params.append(project)
    if app:
        where.append("(e.app = ? OR e.app = '')")
        params.append(app)
    if temporal is not None:
        where.append("e.created_at_ms >= ? AND e.created_at_ms < ?")
        params.extend((temporal.start_ms, temporal.end_ms))
    params.append(max(40, min(2000, int(raw_row_limit))))
    rows = conn.execute(
        f"""
        SELECT e.id, e.created_at_ms, e.source, e.committed_text, e.recent_context,
               e.preedit, e.app, e.project, e.context_group_id, e.context_group_level,
               e.tags_json, e.capture_metadata_json
        FROM input_events e
        LEFT JOIN memory_state s ON s.event_id = e.id
        WHERE {' AND '.join(where)}
        ORDER BY e.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    assembled = assemble_input_rows(list(reversed(rows)))
    eligible = [item for item in assembled if bool(item.get("injectable"))]
    selected: list[dict[str, object]] = []
    used_tokens = 0
    limit = max(max(1, int(baseline_records)), requested_count)
    actual_count = len(eligible)
    actual_chars = sum(
        len(compact_whitespace(str(record.get("text") or "")))
        for record in eligible
    )
    for record in reversed(eligible):
        text = compact_whitespace(str(record.get("text") or ""))
        if not text:
            continue
        remaining_chars = requested_chars - sum(
            len(compact_whitespace(str(item.get("text") or "")))
            for item in selected
        )
        if remaining_chars <= 0:
            break
        char_truncated = len(text) > remaining_chars
        if char_truncated and selected:
            break
        if char_truncated:
            text = text[:remaining_chars].rstrip()
            if not text:
                break
        item_tokens = estimate_tokens(text) + 8
        if selected and used_tokens + item_tokens > available_tokens:
            break
        if not selected and item_tokens > available_tokens:
            text = tail_for_token_budget(text, available_tokens - 8)
            text = text[:remaining_chars].rstrip()
            item_tokens = estimate_tokens(text) + 8
            char_truncated = True
        if char_truncated:
            record = {**record, "text": text, "truncated": True}
        selected.append(record)
        used_tokens += item_tokens
        if len(selected) >= limit:
            break
    selected.reverse()
    effective_chars = sum(
        len(compact_whitespace(str(item.get("text") or "")))
        for item in selected
    )
    effective_count = len(selected)
    truncated = bool(
        actual_count > effective_count
        or actual_chars > effective_chars
        or any(bool(item.get("truncated")) for item in selected)
    )
    rendered = "\n".join(
        f"[{_date_label(int(item.get('createdAtMs') or 0))}]"
        f"[App: {_app_label(str(item.get('app') or ''))}] {item.get('text', '')}"
        for item in selected
    )
    return {
        "schemaVersion": INPUT_CONTEXT_SCHEMA_VERSION,
        "records": selected,
        "rendered": rendered,
        "observability": {
            "rawEventCount": len(rows),
            "assembledRecordCount": len(assembled),
            "eligibleRecordCount": len(eligible),
            "blockedRecordCount": len(assembled) - len(eligible),
            "blockedReasons": _blocked_reason_counts(assembled),
            "selectedRecordCount": len(selected),
            "baselineRecordCount": max(1, int(baseline_records)),
            "maxRecordCount": limit,
            "recentInputPolicy": {
                "requestedCount": requested_count,
                "requestedChars": requested_chars,
                "effectiveCount": effective_count,
                "effectiveChars": effective_chars,
                "actualCount": actual_count,
                "actualChars": actual_chars,
                "truncated": truncated,
                "unavailableReason": (
                    "no_eligible_inputs"
                    if actual_count == 0
                    else "budget_exhausted"
                    if actual_count > 0 and not selected
                    else ""
                ),
            },
            "tokenBudget": int(token_budget),
            "reservedTokens": int(reserved_tokens),
            "availableTokens": available_tokens,
            "estimatedTokens": used_tokens,
            "temporalWindow": temporal.payload() if temporal is not None else None,
            "explicitTemporalQuery": temporal is not None,
        },
    }


def assemble_input_rows(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    fragment_run: list[Mapping[str, object]] = []

    def flush() -> None:
        if not fragment_run:
            return
        result.append(_collapse_fragment_run(fragment_run))
        fragment_run.clear()

    for row in rows:
        source = compact_whitespace(str(row["source"] if "source" in row.keys() else ""))
        text = compact_whitespace(str(row["committed_text"] if "committed_text" in row.keys() else row.get("text", "")))
        if source in INTERNAL_EVENT_SOURCES or not text:
            continue
        tags = _tags(row)
        if source == "codex_history" and "role:user" not in tags:
            continue
        if not source_context_enabled(source, tags=tags):
            continue
        if source == _RIME_FRAGMENT_SOURCE:
            if fragment_run and not _fragments_belong_together(fragment_run[-1], row):
                flush()
            fragment_run.append(row)
            continue
        flush()
        result.append(_standalone_record(row, text=text))
    flush()
    return result


def _fragments_belong_together(previous: Mapping[str, object], current: Mapping[str, object]) -> bool:
    if _value(previous, "app") != _value(current, "app"):
        return False
    if _value(previous, "context_group_id", "contextGroupId") != _value(current, "context_group_id", "contextGroupId"):
        return False
    previous_ms = _int_value(previous, "created_at_ms", "createdAtMs")
    current_ms = _int_value(current, "created_at_ms", "createdAtMs")
    gap_ms = max(0, current_ms - previous_ms)
    previous_context = _value(previous, "recent_context", "recentContext")
    current_context = _value(current, "recent_context", "recentContext")
    if previous_context and current_context:
        if previous_context in current_context or current_context in previous_context:
            return gap_ms <= 5 * 60 * 1000
        if cumulative_context_snapshots_are_revisions(
            previous_context,
            current_context,
            gap_ms=gap_ms,
        ):
            return True
        overlap_limit = min(len(previous_context), len(current_context), 120)
        for size in range(overlap_limit, 7, -1):
            if previous_context[-size:] == current_context[:size]:
                return gap_ms <= 5 * 60 * 1000
        return False
    previous_text = _value(previous, "committed_text", "text")
    if _SENTENCE_END_RE.search(previous_text) and gap_ms > 1200:
        return False
    return gap_ms <= 8_000


def _collapse_fragment_run(events: Sequence[Mapping[str, object]]) -> dict[str, object]:
    source_ids = [_int_value(item, "id", "eventId") for item in events]
    source_ids = [value for value in source_ids if value > 0]
    reconstructed = reconstruct_input_fragment_run(
        [_value(item, "committed_text", "text") for item in events],
        [_value(item, "recent_context", "recentContext") for item in events],
    )
    last = events[-1]
    quality = assess_input_text(
        reconstructed,
        source=_RIME_FRAGMENT_SOURCE,
        source_count=len(source_ids),
        finalized=False,
        reconstructed=True,
        tags=(tag for item in events for tag in _tags(item)),
    )
    return {
        "id": f"assembled:{source_ids[0] if source_ids else 0}-{source_ids[-1] if source_ids else 0}",
        "text": reconstructed,
        "textHash": stable_text_hash(reconstructed),
        "sourceEventIds": source_ids,
        "sourceEventCount": len(source_ids),
        "source": "assembled_user_input",
        "createdAtMs": _int_value(last, "created_at_ms", "createdAtMs"),
        "app": _value(last, "app"),
        "project": _value(last, "project"),
        "contextGroupId": _value(last, "context_group_id", "contextGroupId"),
        "contextGroupLevel": _value(last, "context_group_level", "contextGroupLevel") or "app",
        "finalized": False,
        **quality.payload(),
        "reconstruction": {"method": "rime-fragment-run", "rawEventCount": len(events)},
    }


def _standalone_record(row: Mapping[str, object], *, text: str) -> dict[str, object]:
    event_id = _int_value(row, "id", "eventId")
    recent_context = compact_whitespace(_value(row, "recent_context", "recentContext"))
    # A short commit is not useful by itself, but older Squirrel events may
    # carry the bounded foreground field snapshot that gives it meaning. Keep
    # that trusted context without allowing an arbitrary editor document to
    # replace the actual input event.
    use_context = bool(
        recent_context
        and len(recent_context) > len(text)
        and len(recent_context) <= 180
        and len(recent_context) <= len(text) + 120
        and (len(text) < 8 or text in recent_context)
    )
    reconstructed = recent_context if use_context else text
    source = _value(row, "source") or "input"
    tags = _tags(row)
    row_keys = row.keys()
    capture_metadata_value: object = (
        row["capture_metadata_json"]
        if "capture_metadata_json" in row_keys
        else row["captureMetadata"]
        if "captureMetadata" in row_keys
        else {}
    )
    capture_metadata = _json_object(capture_metadata_value)
    finalized = source != FINALIZED_INPUT_SOURCE or (
        "finalized" in tags or "complete-input" in tags
    )
    quality = assess_input_text(
        reconstructed,
        source=source,
        source_count=1,
        finalized=finalized,
        reconstructed=use_context,
        tags=tags,
        capture_metadata=capture_metadata,
        app=_value(row, "app"),
    )
    return {
        "id": f"event:{event_id}" if event_id else f"event:{stable_text_hash(reconstructed).split(':')[-1][:12]}",
        "text": reconstructed,
        "textHash": stable_text_hash(reconstructed),
        "sourceEventIds": [event_id] if event_id else [],
        "sourceEventCount": 1 if event_id else 0,
        "source": source,
        "createdAtMs": _int_value(row, "created_at_ms", "createdAtMs"),
        "app": _value(row, "app"),
        "project": _value(row, "project"),
        "contextGroupId": _value(row, "context_group_id", "contextGroupId"),
        "contextGroupLevel": _value(row, "context_group_level", "contextGroupLevel") or "app",
        "finalized": finalized,
        **quality.payload(),
        "reconstruction": {
            "method": (
                "standalone-context"
                if use_context
                else "finalized-input-segment"
                if source == FINALIZED_INPUT_SOURCE
                else "standalone"
            ),
            "rawEventCount": 1,
        },
    }


def join_input_fragments(fragments: Iterable[str]) -> str:
    """Join committed IME pieces without turning Chinese commits into spaced words."""

    return compact_whitespace("".join(str(fragment or "") for fragment in fragments))


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def reconstruct_input_fragment_run(fragments: Sequence[str], recent_contexts: Sequence[str]) -> str:
    """Use a compact cumulative snapshot only when it clearly covers this run.

    Old Squirrel builds stored one final foreground snapshot beside every burst
    fragment. A bounded snapshot can recover Backspace corrections, while a
    large editor paragraph must never replace the actual committed run.
    """

    joined = join_input_fragments(fragments)
    contexts = [compact_whitespace(item) for item in recent_contexts if compact_whitespace(item)]
    if not joined or not contexts:
        return joined
    latest = contexts[-1]
    if len(latest) > min(180, len(joined) + 48):
        return joined
    if len(contexts) > 1 and all(
        cumulative_context_snapshots_are_revisions(
            previous,
            current,
            gap_ms=0,
        )
        for previous, current in zip(contexts, contexts[1:])
    ):
        # Older Squirrel history can contain several bounded snapshots of the
        # same field while Backspace replaces one character. Concatenating
        # those revisions repeats the whole sentence and leaks editor noise to
        # memory curation; the final bounded snapshot is the corrected value.
        return latest
    cursor = 0
    for fragment in (compact_whitespace(item) for item in fragments):
        if not fragment:
            continue
        index = latest.find(fragment, cursor)
        if index < 0:
            return joined
        cursor = index + len(fragment)
    return latest if len(latest) >= len(joined) else joined


def cumulative_context_snapshots_are_revisions(
    previous: str,
    current: str,
    *,
    gap_ms: int,
) -> bool:
    """Recognize a short-lived correction of one bounded foreground snapshot.

    Exact containment is handled by callers as the ordinary append case. This
    predicate is deliberately narrower: both snapshots must be bounded, occur
    within one Rime burst, stay close in length, and preserve most characters
    at their edges. It therefore catches Backspace corrections without merging
    merely related commands or separate utterances in the same application.
    """

    left = compact_whitespace(previous)
    right = compact_whitespace(current)
    if (
        not left
        or not right
        or left == right
        or gap_ms < 0
        or gap_ms > 30_000
        or min(len(left), len(right)) < 8
        or max(len(left), len(right)) > 180
    ):
        return False
    longest = max(len(left), len(right))
    shortest = min(len(left), len(right))
    if longest - shortest > max(4, longest // 5):
        return False

    prefix = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        prefix += 1
    suffix = 0
    remaining = shortest - prefix
    for left_char, right_char in zip(reversed(left), reversed(right)):
        if left_char != right_char or suffix >= remaining:
            break
        suffix += 1
    stable_edge_ratio = (prefix + suffix) / shortest
    similarity = SequenceMatcher(None, left, right, autojunk=False).ratio()
    return stable_edge_ratio >= 0.8 and similarity >= 0.86


def _day_window(target: date, *, label: str, template: datetime) -> TemporalWindow:
    return _date_range_window(target, target + timedelta(days=1), label=label, template=template)


def _date_range_window(start: date, end: date, *, label: str, template: datetime) -> TemporalWindow:
    timezone = template.tzinfo
    start_dt = datetime.combine(start, datetime_time.min, tzinfo=timezone)
    end_dt = datetime.combine(end, datetime_time.min, tzinfo=timezone)
    return TemporalWindow(
        label=label,
        start_ms=int(start_dt.timestamp() * 1000),
        end_ms=int(end_dt.timestamp() * 1000),
        explicit=True,
    )


def tail_for_token_budget(text: str, budget: int) -> str:
    """Return the longest trailing text that fits the approximate token budget."""

    value = compact_whitespace(text)
    if estimate_tokens(value) <= max(1, int(budget)):
        return value
    low = 1
    high = len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(value[-middle:]) <= max(1, int(budget)):
            low = middle
        else:
            high = middle - 1
    return value[-low:]


def _date_label(timestamp_ms: int) -> str:
    if timestamp_ms <= 0:
        return "unknown"
    return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().strftime("%Y-%m-%d %H:%M")


def _app_label(app: str) -> str:
    value = compact_whitespace(app)
    return value or "unknown"


def _blocked_reason_counts(records: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        if bool(record.get("injectable")):
            continue
        reasons = record.get("qualityReasons")
        if not isinstance(reasons, list):
            reasons = ["unknown"]
        for reason in reasons:
            key = compact_whitespace(str(reason)) or "unknown"
            counts[key] = counts.get(key, 0) + 1
    return counts


def _tags(row: Mapping[str, object]) -> list[str]:
    raw = _value(row, "tags_json", "tagsJson")
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [compact_whitespace(str(item)) for item in payload if compact_whitespace(str(item))]


def _value(row: Mapping[str, object], *keys: str) -> str:
    available = set(row.keys())
    for key in keys:
        if key in available:
            return compact_whitespace(str(row[key] or ""))
    return ""


def _int_value(row: Mapping[str, object], *keys: str) -> int:
    available = set(row.keys())
    for key in keys:
        if key not in available:
            continue
        try:
            return int(row[key] or 0)
        except (TypeError, ValueError):
            continue
    return 0
