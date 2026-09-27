from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .activity_timeline_curation import (
    ActivityOrganizationPacket,
    ActivityOrganizationResult,
    build_activity_organization_packet,
    validate_activity_organization_output,
)
from .contracts.json_schema import validate_contract
from .db import apply_database_migrations
from .memory_ingest import normalize_text
from .memory_projection import RETRIEVAL_DOCS_PROJECTION, enqueue_memory_projection
from .sensitive_content import is_redacted_or_sensitive
from .text_utils import compact_whitespace, now_ms, truncate_text

if TYPE_CHECKING:
    from .personal_context_observability import PersonalContextObservability


DAILY_ACTIVITY_TIMELINE_SCHEMA_VERSION = "rag-ime.daily-activity-timeline.v1"
DEFAULT_SEGMENT_GAP_MS = 45 * 60 * 1_000
MIN_CONSOLIDATED_ACTIVITY_SPAN_MS = 30 * 60 * 1_000
ACTIVITY_SPAN_SEMANTICS = "first_to_last_source_event"
_SEMANTIC_TASK_MAX_GAP_MS = 6 * 60 * 60 * 1_000
_MAX_SEMANTIC_TASKS_PER_DAY = 3
_FRAGMENT_BURST_GAP_MS = 20 * 1_000
TIMELINE_SEGMENTATION_MODE = "semantic_task_v5"
_TIMELINE_SEGMENTATION_MODE = TIMELINE_SEGMENTATION_MODE

_INTERNAL_EVENT_SOURCES = frozenset(
    {
        "api_core_optimizer",
        "api_lexicon_optimizer",
        "squirrel_rime_sidecar",
        "vcp_memory_generator",
        "ime_demo_fixture",
    }
)


class StaleActivityTimelineError(ValueError):
    """The reviewed draft no longer represents the current input-event set."""


@dataclass(frozen=True)
class ActivityTimelineSegment:
    segment_id: str
    position: int
    app: str
    source_kinds: tuple[str, ...]
    context_group_ids: tuple[str, ...]
    start_ms: int
    end_ms: int
    period: str
    event_count: int
    source_event_ids: tuple[int, ...]
    source_event_hash: str
    summary: str
    redacted_event_count: int
    title: str
    apps: tuple[str, ...]
    activity_kind: str
    evidence_refs: tuple[dict[str, object], ...]

    def payload(self) -> dict[str, object]:
        return {
            "segmentId": self.segment_id,
            "position": self.position,
            "app": self.app,
            "sourceKinds": list(self.source_kinds),
            "contextGroupIds": list(self.context_group_ids),
            "startMs": self.start_ms,
            "endMs": self.end_ms,
            "period": self.period,
            "eventCount": self.event_count,
            "sourceEventIds": list(self.source_event_ids),
            "sourceEventHash": self.source_event_hash,
            "summary": self.summary,
            "redactedEventCount": self.redacted_event_count,
            "title": self.title,
            "apps": list(self.apps),
            "activityKind": self.activity_kind,
            "spanSemantics": ACTIVITY_SPAN_SEMANTICS,
            "evidenceRefs": [dict(value) for value in self.evidence_refs],
            "source": {
                "type": "input_event_bundle",
                "id": f"event-set:{self.source_event_hash}",
            },
        }


@dataclass(frozen=True)
class _ActivityEvent:
    event_id: int
    created_at_ms: int
    source: str
    text: str
    recent_context: str
    preedit: str
    app: str
    project: str
    context_group_id: str
    context_group_level: str


class DailyActivityTimelineStore:
    """Derive reviewable cross-app daily activity without creating user facts."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        project: str = "",
        timezone_name: str = "",
        segment_gap_ms: int = DEFAULT_SEGMENT_GAP_MS,
        observability: PersonalContextObservability | None = None,
        preverified_schema: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.project = compact_whitespace(project)
        self.timezone = _resolve_timezone(timezone_name)
        self.timezone_name = _timezone_name(self.timezone)
        self.segment_gap_ms = max(60_000, int(segment_gap_ms))
        self.observability = observability
        self.preverified_schema = bool(preverified_schema)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            if self.preverified_schema:
                exists = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'daily_activity_timelines'
                    """
                ).fetchone()
                if exists is None:
                    raise RuntimeError(
                        "preverified activity timeline schema is incomplete"
                    )
            else:
                apply_database_migrations(conn)

    def projects_for_date(self, timeline_date: str) -> tuple[str, ...]:
        day = _validated_date(timeline_date)
        start_ms, end_ms = _day_bounds_ms(day, self.timezone)
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT e.project
                FROM input_events e
                LEFT JOIN memory_state s ON s.event_id = e.id
                WHERE e.created_at_ms >= ? AND e.created_at_ms < ?
                  AND COALESCE(s.deleted, 0) = 0
                  AND e.source NOT IN ({_placeholders(_INTERNAL_EVENT_SOURCES)})
                ORDER BY e.project
                """,
                (start_ms, end_ms, *sorted(_INTERNAL_EVENT_SOURCES)),
            ).fetchall()
        projects = tuple(str(row["project"] or "") for row in rows)
        if self.project:
            return tuple(value for value in projects if value == self.project)
        return projects

    def calendar(self, timeline_month: str) -> dict[str, object]:
        """Project one month of source coverage and current timeline state."""

        first_day = _validated_month(timeline_month)
        next_month = (
            date(first_day.year + 1, 1, 1)
            if first_day.month == 12
            else date(first_day.year, first_day.month + 1, 1)
        )
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM daily_activity_timelines
                WHERE project = ? AND timeline_date >= ? AND timeline_date < ?
                  AND status <> 'superseded'
                ORDER BY timeline_date ASC,
                  CASE status
                    WHEN 'draft' THEN 0
                    WHEN 'approved' THEN 1
                    WHEN 'rejected' THEN 2
                    ELSE 3
                  END,
                  updated_at_ms DESC, timeline_id DESC
                """,
                (self.project, first_day.isoformat(), next_month.isoformat()),
            ).fetchall()
            latest_by_day: dict[str, sqlite3.Row] = {}
            for row in rows:
                latest_by_day.setdefault(str(row["timeline_date"]), row)

            days: list[dict[str, object]] = []
            source_event_count = 0
            activity_day_count = 0
            organized_day_count = 0
            approved_day_count = 0
            draft_day_count = 0
            waiting_day_count = 0
            outdated_day_count = 0
            cursor = first_day
            while cursor < next_month:
                events = self._events(conn, cursor)
                row = latest_by_day.get(cursor.isoformat())
                if not events and row is None:
                    cursor += timedelta(days=1)
                    continue

                event_count = len(events)
                current_hash = _timeline_event_hash(events) if events else ""
                status = str(row["status"] or "") if row is not None else "none"
                timeline_hash = (
                    str(row["source_event_hash"] or "") if row is not None else ""
                )
                metadata = (
                    _json_mapping(row["metadata_json"])
                    if row is not None
                    else {}
                )
                is_current = bool(events and row is not None and timeline_hash == current_hash)
                model_organized = bool(
                    metadata.get("modelOrganized") is True
                    and str(metadata.get("organizationMode") or "")
                    == "luna_activity_v1"
                )
                organized = (
                    status in {"draft", "approved"}
                    and is_current
                    and model_organized
                )
                needs_refresh = bool(
                    events
                    and row is not None
                    and status in {"draft", "approved"}
                    and not is_current
                )
                waiting = bool(events and not organized)

                source_event_count += event_count
                activity_day_count += int(bool(events))
                organized_day_count += int(organized)
                approved_day_count += int(status == "approved" and is_current)
                draft_day_count += int(status == "draft" and is_current)
                waiting_day_count += int(waiting)
                outdated_day_count += int(needs_refresh)
                days.append(
                    {
                        "date": cursor.isoformat(),
                        "status": status,
                        "organized": organized,
                        "modelOrganized": model_organized,
                        "needsRefresh": needs_refresh,
                        "sourceEventCount": event_count,
                        "timelineId": str(row["timeline_id"] or "") if row is not None else "",
                        "timelineEventCount": int(row["event_count"] or 0) if row is not None else 0,
                        "segmentCount": int(row["segment_count"] or 0) if row is not None else 0,
                        "updatedAtMs": int(row["updated_at_ms"] or 0) if row is not None else 0,
                    }
                )
                cursor += timedelta(days=1)

        return {
            "schemaVersion": "rag-ime.activity-timeline-calendar.v1",
            "ok": True,
            "project": self.project,
            "timezone": self.timezone_name,
            "month": first_day.strftime("%Y-%m"),
            "summary": {
                "sourceEventCount": source_event_count,
                "activityDayCount": activity_day_count,
                "organizedDayCount": organized_day_count,
                "approvedDayCount": approved_day_count,
                "draftDayCount": draft_day_count,
                "waitingDayCount": waiting_day_count,
                "outdatedDayCount": outdated_day_count,
            },
            "days": days,
        }

    def requires_model_organization(self, timeline_id: str) -> bool:
        """Return whether the current projection still contains heuristic text."""

        identifier = _required_text(timeline_id, "timeline_id")
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status, metadata_json
                FROM daily_activity_timelines
                WHERE timeline_id = ? AND project = ?
                """,
                (identifier, self.project),
            ).fetchone()
        if row is None:
            raise ValueError("daily activity timeline does not exist in this project")
        if str(row["status"] or "") not in {"draft", "approved"}:
            return False
        metadata = _json_mapping(row["metadata_json"])
        return not (
            metadata.get("modelOrganized") is True
            and str(metadata.get("organizationMode") or "")
            == "luna_activity_v1"
        )

    def dates_requiring_model_organization(
        self,
        through_date: str,
        *,
        start_date: str = "",
    ) -> tuple[str, ...]:
        """List source days whose current projection lacks verified semantics."""

        start, through = activity_timeline_date_range(through_date, start_date=start_date)
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.created_at_ms
                FROM input_events e
                LEFT JOIN memory_state s ON s.event_id = e.id
                WHERE e.project = ?
                  AND COALESCE(s.deleted, 0) = 0
                  AND NOT EXISTS (
                      SELECT 1
                      FROM memory_tombstones AS tombstone
                      WHERE tombstone.active = 1
                        AND (
                             (tombstone.target_type = 'source_event_id'
                              AND tombstone.target_value = CAST(e.id AS TEXT))
                          OR (tombstone.target_type = 'memory_id'
                              AND tombstone.target_value = ('event:' || e.id))
                        )
                  )
                  AND e.source NOT IN ({_placeholders(_INTERNAL_EVENT_SOURCES)})
                ORDER BY e.created_at_ms ASC
                """,
                (self.project, *sorted(_INTERNAL_EVENT_SOURCES)),
            ).fetchall()
        source_dates = sorted(
            {
                datetime.fromtimestamp(
                    int(row["created_at_ms"] or 0) / 1_000,
                    tz=self.timezone,
                ).date()
                for row in rows
                if int(row["created_at_ms"] or 0) > 0
            }
        )
        pending: list[str] = []
        for day in source_dates:
            if start is not None and day < start:
                continue
            if day > through:
                break
            latest = self.latest(day.isoformat())
            if latest is None:
                pending.append(day.isoformat())
                continue
            metadata = self._timeline_metadata(str(latest["timelineId"]))
            calendar = self.calendar(day.strftime("%Y-%m"))
            calendar_day = next(
                (
                    value
                    for value in calendar["days"]
                    if value["date"] == day.isoformat()
                ),
                None,
            )
            if (
                str(latest.get("status") or "") == "rejected"
                and isinstance(calendar_day, Mapping)
                and int(calendar_day.get("sourceEventCount") or 0)
                == int(latest.get("eventCount") or 0)
            ):
                # A current rejected projection is an explicit user decision,
                # not unfinished model work. New evidence makes it pending
                # again because build_draft will create a fresh membership.
                continue
            if not (
                metadata.get("modelOrganized") is True
                and str(metadata.get("organizationMode") or "")
                == "luna_activity_v1"
            ):
                pending.append(day.isoformat())
                continue
            # A previously organized day becomes pending again when new source
            # events change its immutable evidence membership.
            if not isinstance(calendar_day, Mapping) or not bool(
                calendar_day.get("organized")
            ):
                pending.append(day.isoformat())
        return tuple(pending)

    def _timeline_metadata(self, timeline_id: str) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT metadata_json FROM daily_activity_timelines
                WHERE timeline_id = ? AND project = ?
                """,
                (timeline_id, self.project),
            ).fetchone()
        return _json_mapping(row["metadata_json"]) if row is not None else {}

    def build_draft(
        self,
        timeline_date: str,
        *,
        generated_at_ms: int | None = None,
    ) -> dict[str, object]:
        day = _validated_date(timeline_date)
        timestamp = now_ms() if generated_at_ms is None else max(0, int(generated_at_ms))
        self.initialize()
        with self._connect(immediate=True) as conn:
            events = self._events(conn, day)
            if not events:
                return {
                    "schemaVersion": "rag-ime.daily-activity-timeline-build.v1",
                    "ok": True,
                    "created": False,
                    "status": "no_events",
                    "project": self.project,
                    "date": day.isoformat(),
                    "timeline": {},
                }
            event_hash = _timeline_event_hash(events)
            existing = conn.execute(
                """
                SELECT * FROM daily_activity_timelines
                WHERE project = ? AND timeline_date = ? AND source_event_hash = ?
                """,
                (self.project, day.isoformat(), event_hash),
            ).fetchone()
            existing_status = str(existing["status"]) if existing is not None else ""
            existing_metadata = (
                _json_mapping(existing["metadata_json"])
                if existing is not None
                else {}
            )
            existing_segmentation_mode = str(
                existing_metadata.get("segmentationMode") or ""
            )
            if (
                existing is not None
                and (
                    existing_status in {"approved", "rejected", "superseded"}
                    or existing_segmentation_mode == _TIMELINE_SEGMENTATION_MODE
                )
            ):
                timeline = _timeline_payload(existing)
                validate_contract(timeline, "daily-activity-timeline.v1.json")
                return {
                    "schemaVersion": "rag-ime.daily-activity-timeline-build.v1",
                    "ok": True,
                    "created": False,
                    "status": str(existing["status"]),
                    "project": self.project,
                    "date": day.isoformat(),
                    "timeline": timeline,
                }

            segments = _segments(
                events,
                timezone=self.timezone,
                max_gap_ms=self.segment_gap_ms,
            )
            summary = _timeline_summary(segments, timezone=self.timezone)
            timeline_id = (
                "activity-timeline:"
                f"{_stable_digest(self.project, day.isoformat(), event_hash)[:28]}"
            )
            source_ids = [event.event_id for event in events]
            segments_json = json.dumps(
                [segment.payload() for segment in segments],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            metadata = {
                "derivedFrom": "input_events",
                "derivedArtifactType": "daily_activity_timeline",
                "segmentGapMs": self.segment_gap_ms,
                "segmentationMode": _TIMELINE_SEGMENTATION_MODE,
                "longTermFact": False,
                "automaticPromotion": True,
                "explicitApprovalRequired": False,
                "corroborationOnly": True,
            }
            conn.execute(
                """
                UPDATE daily_activity_timelines
                SET status = 'superseded', updated_at_ms = ?
                WHERE project = ? AND timeline_date = ? AND status = 'draft'
                  AND source_event_hash <> ?
                """,
                (timestamp, self.project, day.isoformat(), event_hash),
            )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO daily_activity_timelines(
                        timeline_id, project, timeline_date, timezone, status,
                        source_event_ids_json, source_event_hash, segments_json,
                        summary_text, event_count, segment_count, metadata_json,
                        created_at_ms, updated_at_ms
                    ) VALUES (
                        ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        timeline_id,
                        self.project,
                        day.isoformat(),
                        self.timezone_name,
                        json.dumps(source_ids, separators=(",", ":")),
                        event_hash,
                        segments_json,
                        summary,
                        len(events),
                        len(segments),
                        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                timeline_id = str(existing["timeline_id"])
                conn.execute(
                    """
                    UPDATE daily_activity_timelines
                    SET status = 'draft', timezone = ?,
                        source_event_ids_json = ?, segments_json = ?,
                        summary_text = ?, event_count = ?, segment_count = ?,
                        approved_book_id = '', approved_by = '',
                        approved_at_ms = NULL, rejection_reason = '',
                        metadata_json = ?, updated_at_ms = ?
                    WHERE timeline_id = ?
                    """,
                    (
                        self.timezone_name,
                        json.dumps(source_ids, separators=(",", ":")),
                        segments_json,
                        summary,
                        len(events),
                        len(segments),
                        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                        timestamp,
                        timeline_id,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM daily_activity_timelines WHERE timeline_id = ?",
                (timeline_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("daily activity timeline draft was not persisted")
        timeline = _timeline_payload(row)
        validate_contract(timeline, "daily-activity-timeline.v1.json")
        return {
            "schemaVersion": "rag-ime.daily-activity-timeline-build.v1",
            "ok": True,
            "created": True,
            "status": "draft",
            "project": self.project,
            "date": day.isoformat(),
            "timeline": timeline,
        }

    def review(self, timeline_id: str) -> dict[str, object]:
        identifier = _required_text(timeline_id, "timeline_id")
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM daily_activity_timelines
                WHERE timeline_id = ? AND project = ?
                """,
                (identifier, self.project),
            ).fetchone()
        if row is None:
            raise ValueError("daily activity timeline does not exist in this project")
        payload = _timeline_payload(row)
        validate_contract(payload, "daily-activity-timeline.v1.json")
        return payload

    def organization_packet(self, timeline_id: str) -> ActivityOrganizationPacket:
        """Freeze the exact current source membership for semantic organization."""

        identifier = _required_text(timeline_id, "timeline_id")
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM daily_activity_timelines
                WHERE timeline_id = ? AND project = ?
                """,
                (identifier, self.project),
            ).fetchone()
            if row is None:
                raise ValueError("daily activity timeline does not exist in this project")
            day = _validated_date(str(row["timeline_date"]))
            events = self._events(conn, day)
            if not events or _timeline_event_hash(events) != str(row["source_event_hash"]):
                raise StaleActivityTimelineError(
                    "input events changed before semantic Activity organization"
                )
        return build_activity_organization_packet(
            [_activity_event_row(event) for event in events],
            timeline_id=identifier,
            project=self.project,
            timeline_date=day.isoformat(),
            timezone_name=self.timezone_name,
        )

    def apply_model_organization(
        self,
        timeline_id: str,
        *,
        organization: Mapping[str, object],
        receipt: Mapping[str, object],
        organized_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Apply a verified model result to the derived Timeline projection.

        The source event set remains authoritative. Model text can only replace
        Activity boundaries, titles, and summaries after exact reference-ledger
        validation against the current immutable membership.
        """

        identifier = _required_text(timeline_id, "timeline_id")
        timestamp = now_ms() if organized_at_ms is None else max(0, int(organized_at_ms))
        packet = self.organization_packet(identifier)
        result = validate_activity_organization_output(
            organization,
            packet=packet,
        )
        self.initialize()
        with self._connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM daily_activity_timelines
                WHERE timeline_id = ? AND project = ?
                """,
                (identifier, self.project),
            ).fetchone()
            if row is None:
                raise ValueError("daily activity timeline does not exist in this project")
            if str(row["status"]) not in {"draft", "approved"}:
                raise ValueError("only a current draft or approved timeline can be organized")
            day = _validated_date(str(row["timeline_date"]))
            events = self._events(conn, day)
            if (
                not events
                or _timeline_event_hash(events) != str(row["source_event_hash"])
                or packet.membership_sha256
                != build_activity_organization_packet(
                    [_activity_event_row(event) for event in events],
                    timeline_id=identifier,
                    project=self.project,
                    timeline_date=day.isoformat(),
                    timezone_name=self.timezone_name,
                ).membership_sha256
            ):
                raise StaleActivityTimelineError(
                    "input events changed before semantic Activity organization was applied"
                )
            segments = _organized_segments(
                events,
                packet=packet,
                result=result,
                timezone=self.timezone,
            )
            metadata = _json_mapping(row["metadata_json"])
            metadata.update(
                {
                    "segmentationMode": _TIMELINE_SEGMENTATION_MODE,
                    "organizationMode": "luna_activity_v1",
                    "modelOrganized": True,
                    "modelOrganization": _public_activity_organization_receipt(
                        receipt,
                        membership_sha256=packet.membership_sha256,
                        organized_at_ms=timestamp,
                    ),
                }
            )
            conn.execute(
                """
                UPDATE daily_activity_timelines
                SET segments_json = ?, summary_text = ?, segment_count = ?,
                    metadata_json = ?, updated_at_ms = ?
                WHERE timeline_id = ?
                """,
                (
                    json.dumps(
                        [segment.payload() for segment in segments],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    _timeline_summary(segments, timezone=self.timezone),
                    len(segments),
                    json.dumps(
                        metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    identifier,
                ),
            )
            if str(row["status"]) == "approved":
                enqueue_memory_projection(
                    conn,
                    projection_kind=RETRIEVAL_DOCS_PROJECTION,
                    aggregate_type="daily_activity_timeline",
                    aggregate_id=identifier,
                    operation="approve",
                    project=self.project,
                    revision=max(1, timestamp),
                    payload={
                        "timelineId": identifier,
                        "sourceEventHash": str(row["source_event_hash"]),
                    },
                    available_at_ms=timestamp,
                )
            updated = conn.execute(
                "SELECT * FROM daily_activity_timelines WHERE timeline_id = ?",
                (identifier,),
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("organized timeline disappeared")
        payload = _timeline_payload(updated)
        validate_contract(payload, "daily-activity-timeline.v1.json")
        return payload

    def latest(
        self,
        timeline_date: str,
        *,
        status: str = "",
    ) -> dict[str, object] | None:
        day = _validated_date(timeline_date)
        state = compact_whitespace(status)
        if state and state not in {"draft", "approved", "rejected", "superseded"}:
            raise ValueError("unsupported timeline status")
        self.initialize()
        query = """
            SELECT * FROM daily_activity_timelines
            WHERE project = ? AND timeline_date = ?
        """
        params: list[object] = [self.project, day.isoformat()]
        if state:
            query += " AND status = ?"
            params.append(state)
        query += """
            ORDER BY
              CASE status
                WHEN 'draft' THEN 0
                WHEN 'approved' THEN 1
                WHEN 'rejected' THEN 2
                ELSE 3
              END,
              updated_at_ms DESC, timeline_id DESC
            LIMIT 1
        """
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        payload = _timeline_payload(row)
        validate_contract(payload, "daily-activity-timeline.v1.json")
        return payload

    def approve(
        self,
        timeline_id: str,
        *,
        expected_source_event_hash: str,
        approved_by: str,
        confirm_text: str,
        approved_at_ms: int | None = None,
    ) -> dict[str, object]:
        identifier = _required_text(timeline_id, "timeline_id")
        expected_hash = _required_text(
            expected_source_event_hash,
            "expected_source_event_hash",
        )
        actor = _required_text(approved_by, "approved_by")
        if compact_whitespace(confirm_text).lower() != "approve":
            raise ValueError("confirm_text must be approve")
        timestamp = now_ms() if approved_at_ms is None else max(0, int(approved_at_ms))
        self.initialize()
        with self._connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM daily_activity_timelines
                WHERE timeline_id = ? AND project = ?
                """,
                (identifier, self.project),
            ).fetchone()
            if row is None:
                raise ValueError("daily activity timeline does not exist in this project")
            if str(row["source_event_hash"]) != expected_hash:
                raise StaleActivityTimelineError(
                    "timeline approval hash does not match the reviewed draft"
                )
            if str(row["status"]) == "approved":
                payload = _timeline_payload(row)
                validate_contract(payload, "daily-activity-timeline.v1.json")
                return payload
            if str(row["status"]) != "draft":
                raise ValueError("only a current draft timeline can be approved")

            day = _validated_date(str(row["timeline_date"]))
            current_events = self._events(conn, day)
            if not current_events or _timeline_event_hash(current_events) != expected_hash:
                raise StaleActivityTimelineError(
                    "input events changed after review; rebuild the timeline draft"
                )

            old_rows = conn.execute(
                """
                SELECT timeline_id, approved_book_id
                FROM daily_activity_timelines
                WHERE project = ? AND timeline_date = ? AND status = 'approved'
                  AND timeline_id <> ?
                """,
                (self.project, day.isoformat(), identifier),
            ).fetchall()
            for old in old_rows:
                old_book_id = compact_whitespace(str(old["approved_book_id"] or ""))
                if old_book_id:
                    conn.execute(
                        """
                        UPDATE memory_books
                        SET status = 'archived', archived_at_ms = ?,
                            archive_reason = 'superseded_activity_timeline',
                            updated_at_ms = ?
                        WHERE book_id = ?
                        """,
                        (timestamp, timestamp, old_book_id),
                    )
            legacy_book_id = compact_whitespace(str(row["approved_book_id"] or ""))
            if legacy_book_id:
                conn.execute(
                    """
                    UPDATE memory_books
                    SET status = 'archived', archived_at_ms = ?,
                        archive_reason = 'migrated_to_timeline_index',
                        updated_at_ms = ?
                    WHERE book_id = ? AND book_type = 'daily'
                    """,
                    (timestamp, timestamp, legacy_book_id),
                )
            conn.execute(
                """
                UPDATE daily_activity_timelines
                SET status = 'superseded', updated_at_ms = ?
                WHERE project = ? AND timeline_date = ? AND status = 'approved'
                  AND timeline_id <> ?
                """,
                (timestamp, self.project, day.isoformat(), identifier),
            )

            metadata = _json_mapping(row["metadata_json"])
            metadata.update(
                {
                    "schemaVersion": "rag-ime.activity-timeline-index.v1",
                    "derivedArtifactType": "daily_activity_timeline",
                    "timelineId": identifier,
                    "sourceEventHash": expected_hash,
                    "publicationStatus": "approved",
                    "publishedBy": actor,
                    "publishedAtMs": timestamp,
                    "maySupportFacts": False,
                    "longTermFact": False,
                    "automaticPromotion": True,
                    "explicitApprovalRequired": False,
                    "corroborationOnly": True,
                    "timelineDate": day.isoformat(),
                },
            )
            conn.execute(
                """
                UPDATE daily_activity_timelines
                SET status = 'approved', approved_book_id = '',
                    approved_by = ?, approved_at_ms = ?, metadata_json = ?,
                    updated_at_ms = ?
                WHERE timeline_id = ? AND status = 'draft'
                """,
                (
                    actor,
                    timestamp,
                    json.dumps(
                        metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    identifier,
                ),
            )
            if self.observability is not None:
                self.observability.record_draft_decision_in_connection(
                    conn,
                    draft_kind="activity_timeline",
                    draft_id=identifier,
                    decision="accepted",
                    decided_by=actor,
                    created_at_ms=timestamp,
                )
            enqueue_memory_projection(
                conn,
                projection_kind=RETRIEVAL_DOCS_PROJECTION,
                aggregate_type="daily_activity_timeline",
                aggregate_id=identifier,
                operation="approve",
                project=self.project,
                revision=max(1, timestamp),
                payload={
                    "timelineId": identifier,
                    "sourceEventHash": expected_hash,
                },
                available_at_ms=timestamp,
            )
            approved = conn.execute(
                "SELECT * FROM daily_activity_timelines WHERE timeline_id = ?",
                (identifier,),
            ).fetchone()
        if approved is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("approved timeline disappeared")
        payload = _timeline_payload(approved)
        validate_contract(payload, "daily-activity-timeline.v1.json")
        return payload

    def reject(
        self,
        timeline_id: str,
        *,
        reason: str,
        rejected_by: str = "control-center-user",
        rejected_at_ms: int | None = None,
    ) -> dict[str, object]:
        identifier = _required_text(timeline_id, "timeline_id")
        rejection = _required_text(reason, "reason")
        actor = _required_text(rejected_by, "rejected_by")
        timestamp = now_ms() if rejected_at_ms is None else max(0, int(rejected_at_ms))
        self.initialize()
        with self._connect(immediate=True) as conn:
            changed = conn.execute(
                """
                UPDATE daily_activity_timelines
                SET status = 'rejected', rejection_reason = ?, updated_at_ms = ?
                WHERE timeline_id = ? AND project = ? AND status = 'draft'
                """,
                (truncate_text(rejection, 500), timestamp, identifier, self.project),
            )
            if changed.rowcount != 1:
                raise ValueError("only a current draft timeline can be rejected")
            if self.observability is not None:
                self.observability.record_draft_decision_in_connection(
                    conn,
                    draft_kind="activity_timeline",
                    draft_id=identifier,
                    decision="rejected",
                    decided_by=actor,
                    reason=rejection,
                    created_at_ms=timestamp,
                )
            row = conn.execute(
                "SELECT * FROM daily_activity_timelines WHERE timeline_id = ?",
                (identifier,),
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("rejected timeline disappeared")
        payload = _timeline_payload(row)
        validate_contract(payload, "daily-activity-timeline.v1.json")
        return payload

    def _events(
        self,
        conn: sqlite3.Connection,
        day: date,
    ) -> list[_ActivityEvent]:
        start_ms, end_ms = _day_bounds_ms(day, self.timezone)
        rows = conn.execute(
            f"""
            SELECT e.id, e.created_at_ms, e.source, e.committed_text,
                   e.recent_context, e.preedit, e.app, e.project,
                   e.context_group_id, e.context_group_level
            FROM input_events e
            LEFT JOIN memory_state s ON s.event_id = e.id
            WHERE e.created_at_ms >= ? AND e.created_at_ms < ?
              AND e.project = ?
              AND COALESCE(s.deleted, 0) = 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM memory_tombstones AS tombstone
                  WHERE tombstone.active = 1
                    AND (
                         (tombstone.target_type = 'source_event_id'
                          AND tombstone.target_value = CAST(e.id AS TEXT))
                      OR (tombstone.target_type = 'memory_id'
                          AND tombstone.target_value = ('event:' || e.id))
                    )
              )
              AND e.source NOT IN ({_placeholders(_INTERNAL_EVENT_SOURCES)})
            ORDER BY e.created_at_ms ASC, e.id ASC
            """,
            (
                start_ms,
                end_ms,
                self.project,
                *sorted(_INTERNAL_EVENT_SOURCES),
            ),
        ).fetchall()
        return [
            _ActivityEvent(
                event_id=int(row["id"]),
                created_at_ms=int(row["created_at_ms"]),
                source=str(row["source"] or ""),
                text=str(row["committed_text"] or ""),
                recent_context=str(row["recent_context"] or ""),
                preedit=str(row["preedit"] or ""),
                app=str(row["app"] or ""),
                project=str(row["project"] or ""),
                context_group_id=str(row["context_group_id"] or ""),
                context_group_level=str(row["context_group_level"] or ""),
            )
            for row in rows
        ]

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def _segments(
    events: Sequence[_ActivityEvent],
    *,
    timezone: tzinfo,
    max_gap_ms: int,
) -> list[ActivityTimelineSegment]:
    groups = _semantic_task_groups(
        events,
        max_gap_ms=max_gap_ms,
    )
    _assert_event_conservation(events, groups)

    segments: list[ActivityTimelineSegment] = []
    for position, group in enumerate(groups):
        apps = tuple(
            dict.fromkeys(
                _safe_label(
                    event.app,
                    fallback=_safe_label(event.source, fallback="unknown-app"),
                )
                for event in group
            )
        )
        app = apps[0] if len(apps) == 1 else "multiple"
        source_kinds = tuple(
            dict.fromkeys(
                _safe_label(event.source, fallback="unknown-source")
                for event in group
            )
        )
        context_groups = tuple(
            dict.fromkeys(
                value
                for event in group
                if (
                    value := _safe_optional_label(event.context_group_id)
                )
            )
        )
        source_ids = tuple(event.event_id for event in group)
        source_hash = _event_hash(group)
        snippets: list[str] = []
        redacted_count = 0
        evidence_refs: list[dict[str, object]] = []
        for event in group:
            event_app = _safe_label(
                event.app,
                fallback=_safe_label(event.source, fallback="unknown-app"),
            )
            event_source = _safe_label(event.source, fallback="unknown-source")
            raw_text = compact_whitespace(event.text)
            redacted = is_redacted_or_sensitive(raw_text)
            text = "[敏感内容已脱敏]" if redacted else truncate_text(raw_text, 180)
            if redacted:
                redacted_count += 1
            if text and text not in snippets:
                snippets.append(text)
            evidence_refs.append(
                {
                    "sourceType": "input_event",
                    "sourceId": f"event:{event.event_id}",
                    "eventId": event.event_id,
                    "app": event_app,
                    "sourceKind": event_source,
                    "occurredAtMs": event.created_at_ms,
                    "redacted": redacted,
                }
            )
        if not snippets:
            snippets = ["[无可显示文本]"]
        title = _task_title(group, snippets=snippets)
        summary = title
        if len(group) >= 8:
            app_label = f"跨 {len(apps)} 个应用" if len(apps) > 1 else f"在 {apps[0]}"
            summary += f"：{app_label}持续推进，归并 {len(group)} 条可追溯输入"
        elif len(group) == 1:
            summary = title
        else:
            detail_snippets = [
                value
                for value in snippets
                if value != title and not _is_low_information_text(value)
            ]
            if detail_snippets:
                summary += f"：{'；'.join(detail_snippets[:3])}"
            if len(group) > 3:
                summary += f"；另有 {len(group) - 3} 条输入"
        segment_id = (
            "activity-segment:"
            f"{_stable_digest(str(position), app, source_hash)[:24]}"
        )
        segments.append(
            ActivityTimelineSegment(
                segment_id=segment_id,
                position=position,
                app=app,
                source_kinds=source_kinds,
                context_group_ids=context_groups,
                start_ms=group[0].created_at_ms,
                end_ms=group[-1].created_at_ms,
                period=_activity_period(
                    group[0].created_at_ms,
                    group[-1].created_at_ms,
                    timezone=timezone,
                ),
                event_count=len(group),
                source_event_ids=source_ids,
                source_event_hash=source_hash,
                summary=truncate_text(summary, 760),
                redacted_event_count=redacted_count,
                title=title,
                apps=apps,
                activity_kind=activity_timeline_kind(
                    group[0].created_at_ms,
                    group[-1].created_at_ms,
                ),
                evidence_refs=tuple(evidence_refs),
            )
        )
    return segments


def _timeline_summary(
    segments: Sequence[ActivityTimelineSegment],
    *,
    timezone: tzinfo,
) -> str:
    parts: list[str] = []
    for segment in segments:
        start = datetime.fromtimestamp(segment.start_ms / 1_000, tz=timezone)
        end = datetime.fromtimestamp(segment.end_ms / 1_000, tz=timezone)
        time_label = start.strftime("%H:%M")
        if end.strftime("%H:%M") != time_label:
            time_label = f"{time_label}-{end.strftime('%H:%M')}"
        parts.append(f"{time_label} {segment.summary}")
    return truncate_text("；".join(parts), 1_800)


def _activity_event_row(event: _ActivityEvent) -> dict[str, object]:
    return {
        "id": event.event_id,
        "created_at_ms": event.created_at_ms,
        "source": event.source,
        "committed_text": event.text,
        "recent_context": event.recent_context,
        "app": event.app,
        "project": event.project,
        "context_group_id": event.context_group_id,
    }


def _organized_segments(
    events: Sequence[_ActivityEvent],
    *,
    packet: ActivityOrganizationPacket,
    result: ActivityOrganizationResult,
    timezone: tzinfo,
) -> list[ActivityTimelineSegment]:
    events_by_ref = {
        record.ref: event
        for record, event in zip(packet.records, events, strict=True)
    }
    groups: list[tuple[str, str, Sequence[str], str]] = [
        (
            activity.title,
            activity.summary,
            activity.event_refs,
            activity.activity_id,
        )
        for activity in result.activities
    ]
    if result.unclassified:
        groups.append(
            (
                "暂未归类的活动",
                f"{len(result.unclassified)} 条输入缺少足够语义线索，已保留来源等待后续补充。",
                tuple(item.event_ref for item in result.unclassified),
                "unclassified",
            )
        )

    segments: list[ActivityTimelineSegment] = []
    for position, (title, summary, refs, activity_id) in enumerate(groups):
        group = sorted(
            (events_by_ref[ref] for ref in refs),
            key=lambda event: (event.created_at_ms, event.event_id),
        )
        if not group:
            continue
        apps = tuple(
            dict.fromkeys(
                _safe_label(
                    event.app,
                    fallback=_safe_label(event.source, fallback="unknown-app"),
                )
                for event in group
            )
        )
        source_kinds = tuple(
            dict.fromkeys(
                _safe_label(event.source, fallback="unknown-source")
                for event in group
            )
        )
        context_groups = tuple(
            dict.fromkeys(
                value
                for event in group
                if (value := _safe_optional_label(event.context_group_id))
            )
        )
        source_event_ids = tuple(event.event_id for event in group)
        source_hash = _event_hash(group)
        evidence_refs = tuple(
            {
                "sourceType": "input_event",
                "sourceId": f"event:{event.event_id}",
                "eventId": event.event_id,
                "app": _safe_label(
                    event.app,
                    fallback=_safe_label(event.source, fallback="unknown-app"),
                ),
                "sourceKind": _safe_label(event.source, fallback="unknown-source"),
                "occurredAtMs": event.created_at_ms,
                "redacted": is_redacted_or_sensitive(event.text),
            }
            for event in group
        )
        segments.append(
            ActivityTimelineSegment(
                segment_id=(
                    f"activity-segment:{_stable_digest(activity_id, source_hash)[:24]}"
                ),
                position=position,
                app=apps[0] if len(apps) == 1 else "multiple",
                source_kinds=source_kinds,
                context_group_ids=context_groups,
                start_ms=group[0].created_at_ms,
                end_ms=group[-1].created_at_ms,
                period=_activity_period(
                    group[0].created_at_ms,
                    group[-1].created_at_ms,
                    timezone=timezone,
                ),
                event_count=len(group),
                source_event_ids=source_event_ids,
                source_event_hash=source_hash,
                summary=truncate_text(summary, 760),
                redacted_event_count=sum(
                    is_redacted_or_sensitive(event.text) for event in group
                ),
                title=truncate_text(title, 160),
                apps=apps,
                activity_kind=activity_timeline_kind(
                    group[0].created_at_ms,
                    group[-1].created_at_ms,
                ),
                evidence_refs=evidence_refs,
            )
        )
    return segments


def _public_activity_organization_receipt(
    value: Mapping[str, object],
    *,
    membership_sha256: str,
    organized_at_ms: int,
) -> dict[str, object]:
    scores = value.get("scores")
    return {
        "membershipSha256": membership_sha256,
        "organizerPromptVersion": compact_whitespace(
            str(value.get("organizerPromptVersion") or "")
        )[:120],
        "verifierPromptVersion": compact_whitespace(
            str(value.get("verifierPromptVersion") or "")
        )[:120],
        "contractRepairPromptVersion": compact_whitespace(
            str(value.get("contractRepairPromptVersion") or "")
        )[:120],
        "semanticRepairPromptVersion": compact_whitespace(
            str(value.get("semanticRepairPromptVersion") or "")
        )[:120],
        "organizerOutputSha256": compact_whitespace(
            str(value.get("organizerOutputSha256") or "")
        )[:64],
        "verifierOutputSha256": compact_whitespace(
            str(value.get("verifierOutputSha256") or "")
        )[:64],
        "verdict": compact_whitespace(str(value.get("verdict") or ""))[:16],
        "scores": (
            {
                compact_whitespace(str(key))[:80]: int(score)
                for key, score in scores.items()
                if isinstance(score, int) and not isinstance(score, bool)
            }
            if isinstance(scores, Mapping)
            else {}
        ),
        "contractRepaired": value.get("contractRepaired") is True,
        "semanticRepaired": value.get("semanticRepaired") is True,
        "semanticRepairRounds": max(0, int(value.get("semanticRepairRounds") or 0)),
        "semanticContractRepaired": value.get("semanticContractRepaired") is True,
        "organizedAtMs": max(0, int(organized_at_ms)),
    }


def _event_hash(events: Sequence[_ActivityEvent]) -> str:
    canonical = [
        {
            "id": event.event_id,
            "createdAtMs": event.created_at_ms,
            "source": event.source,
            "text": event.text,
            "recentContext": event.recent_context,
            "preedit": event.preedit,
            "app": event.app,
            "project": event.project,
            "contextGroupId": event.context_group_id,
            "contextGroupLevel": event.context_group_level,
        }
        for event in events
    ]
    raw = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _timeline_event_hash(events: Sequence[_ActivityEvent]) -> str:
    return _stable_digest(_event_hash(events), _TIMELINE_SEGMENTATION_MODE)


def _timeline_payload(row: sqlite3.Row) -> dict[str, object]:
    timeline_id = str(row["timeline_id"])
    event_hash = str(row["source_event_hash"])
    metadata = _json_mapping(row["metadata_json"])
    segmentation_mode = compact_whitespace(
        str(metadata.get("segmentationMode") or "")
    )
    if segmentation_mode not in {
        _TIMELINE_SEGMENTATION_MODE,
        "semantic_task_v4",
        "semantic_task_v3",
        "semantic_task_v2",
    }:
        segmentation_mode = "legacy_app_interval_v1"
    segments = _timeline_segments_payload(
        row["segments_json"],
        timezone_name=str(row["timezone"] or ""),
    )
    observed_starts = [
        _safe_timestamp(segment.get("startMs"))
        for segment in segments
        if _safe_timestamp(segment.get("startMs")) > 0
    ]
    observed_ends = [
        _safe_timestamp(segment.get("endMs"))
        for segment in segments
        if _safe_timestamp(segment.get("endMs")) > 0
    ]
    observed_start_ms = min(observed_starts, default=0)
    observed_end_ms = max(observed_ends, default=observed_start_ms)
    ordinary_activity_count = sum(
        segment.get("activityKind") == "ordinary_activity"
        for segment in segments
    )
    consolidated_activity_count = sum(
        segment.get("activityKind") == "consolidated_activity"
        for segment in segments
    )
    return {
        "schemaVersion": DAILY_ACTIVITY_TIMELINE_SCHEMA_VERSION,
        "timelineId": timeline_id,
        "project": str(row["project"] or ""),
        "date": str(row["timeline_date"]),
        "timezone": str(row["timezone"] or "local"),
        "status": str(row["status"]),
        "sourceEventIds": _json_ints(row["source_event_ids_json"]),
        "sourceEventHash": event_hash,
        "segments": segments,
        "summary": str(row["summary_text"]),
        "eventCount": int(row["event_count"]),
        "segmentCount": int(row["segment_count"]),
        "observedStartMs": observed_start_ms,
        "observedEndMs": observed_end_ms,
        "spanSemantics": ACTIVITY_SPAN_SEMANTICS,
        "ordinaryActivityCount": ordinary_activity_count,
        "consolidatedActivityCount": consolidated_activity_count,
        "approvedBookId": str(row["approved_book_id"] or ""),
        "approvedBy": str(row["approved_by"] or ""),
        "approvedAtMs": int(row["approved_at_ms"] or 0),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "segmentationMode": segmentation_mode,
        "source": {
            "type": "input_event_set",
            "id": f"event-set:{event_hash}",
        },
        "ref": {
            "type": "timeline",
            "id": timeline_id,
        },
        "policy": {
            "derivedFromInputEvents": True,
            "longTermFact": False,
            "automaticPromotion": True,
            "explicitApprovalRequired": False,
            "minimumConsolidatedSpanMs": MIN_CONSOLIDATED_ACTIVITY_SPAN_MS,
        },
    }


def _timeline_segments_payload(
    raw: object,
    *,
    timezone_name: str,
) -> list[dict[str, object]]:
    """Project source-owned span semantics and reference-only evidence."""

    timezone = _payload_timezone(timezone_name)
    segments: list[dict[str, object]] = []
    for value in _json_objects(raw):
        segment = dict(value)
        period = compact_whitespace(
            str(segment.get("period") or segment.get("dayPart") or "")
        ).lower()
        if period not in {"day", "morning", "afternoon", "evening"}:
            period = _activity_period(
                _safe_timestamp(segment.get("startMs")),
                _safe_timestamp(segment.get("endMs")),
                timezone=timezone,
            )
        segment.pop("dayPart", None)
        segment["period"] = period
        start_ms = _safe_timestamp(segment.get("startMs"))
        end_ms = max(start_ms, _safe_timestamp(segment.get("endMs")))
        segment["activityKind"] = activity_timeline_kind(start_ms, end_ms)
        segment["spanSemantics"] = ACTIVITY_SPAN_SEMANTICS
        segment["evidenceRefs"] = _timeline_evidence_refs(segment.get("evidenceRefs"))
        segments.append(segment)
    return segments


def activity_timeline_kind(start_ms: int, end_ms: int) -> str:
    span_ms = max(0, end_ms - start_ms)
    return (
        "consolidated_activity"
        if span_ms >= MIN_CONSOLIDATED_ACTIVITY_SPAN_MS
        else "ordinary_activity"
    )


def _timeline_evidence_refs(raw: object) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    for value in raw if isinstance(raw, list) else []:
        if not isinstance(value, Mapping):
            continue
        try:
            event_id = int(value.get("eventId") or 0)
        except (TypeError, ValueError):
            continue
        if event_id <= 0:
            continue
        preview = compact_whitespace(str(value.get("preview") or ""))
        refs.append(
            {
                "sourceType": "input_event",
                "sourceId": f"event:{event_id}",
                "eventId": event_id,
                "app": _safe_label(str(value.get("app") or ""), fallback="unknown-app"),
                "sourceKind": _safe_label(
                    str(value.get("sourceKind") or ""),
                    fallback="unknown-source",
                ),
                "occurredAtMs": _safe_timestamp(value.get("occurredAtMs")),
                "redacted": bool(value.get("redacted"))
                or is_redacted_or_sensitive(preview),
            }
        )
    return refs


def _payload_timezone(value: str) -> tzinfo:
    name = compact_whitespace(value)
    try:
        return _resolve_timezone("" if name == "local" else name)
    except ValueError:
        return _resolve_timezone("")


def _safe_timestamp(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _activity_period(start_ms: int, end_ms: int, *, timezone: tzinfo) -> str:
    """Classify one semantic task; crossing a day partition is an all-day task."""

    if start_ms <= 0:
        return "day"
    bounded_end = max(start_ms, end_ms)
    start_part = _activity_day_part(start_ms, timezone=timezone)
    end_part = _activity_day_part(bounded_end, timezone=timezone)
    return start_part if start_part == end_part else "day"


def activity_timeline_period(
    start_ms: object,
    end_ms: object,
    *,
    timezone_name: str = "",
) -> str:
    """Return the canonical period for API projections outside this store."""

    return _activity_period(
        _safe_timestamp(start_ms),
        _safe_timestamp(end_ms),
        timezone=_payload_timezone(timezone_name),
    )


def _activity_day_part(timestamp_ms: int, *, timezone: tzinfo) -> str:
    hour = datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone).hour
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def _semantic_task_groups(
    events: Sequence[_ActivityEvent],
    *,
    max_gap_ms: int,
) -> list[list[_ActivityEvent]]:
    """Build task episodes without treating foreground App switches as boundaries."""

    episodes: list[list[_ActivityEvent]] = []
    for event in events:
        if (
            not episodes
            or event.created_at_ms - episodes[-1][-1].created_at_ms > max_gap_ms
        ):
            episodes.append([event])
        else:
            episodes[-1].append(event)

    groups: list[list[_ActivityEvent]] = []
    episode_indexes: list[int] = []
    for episode_index, episode in enumerate(episodes):
        episode_groups: list[list[_ActivityEvent]] = []
        for event in episode:
            if not episode_groups:
                episode_groups.append([event])
                continue
            if _same_short_fragment_burst(event, episode_groups[-1][-1]):
                episode_groups[-1].append(event)
                continue
            best_index = max(
                range(len(episode_groups)),
                key=lambda index: _event_group_affinity(
                    event,
                    episode_groups[index],
                    max_gap_ms=max_gap_ms,
                ),
            )
            best_score = _event_group_affinity(
                event,
                episode_groups[best_index],
                max_gap_ms=max_gap_ms,
            )
            if best_score >= 0.34:
                episode_groups[best_index].append(event)
            elif _obvious_topic_break(event, episode_groups[-1]):
                episode_groups.append([event])
            else:
                episode_groups[-1].append(event)
        for group in episode_groups:
            group.sort(key=lambda value: (value.created_at_ms, value.event_id))
            groups.append(group)
            episode_indexes.append(episode_index)

    # A real pause starts a new episode. Merge across it only when the events
    # carry strong shared entities or commands; runtime scope is only a weak
    # corroborating signal.
    changed = True
    while changed:
        changed = False
        for left in range(len(groups)):
            for right in range(left + 1, len(groups)):
                if episode_indexes[left] == episode_indexes[right]:
                    continue
                if _strong_cross_episode_match(groups[left], groups[right]):
                    groups[left] = sorted(
                        [*groups[left], *groups[right]],
                        key=lambda value: (value.created_at_ms, value.event_id),
                    )
                    del groups[right]
                    del episode_indexes[right]
                    changed = True
                    break
            if changed:
                break
    groups = sorted(groups, key=lambda group: (group[0].created_at_ms, group[0].event_id))
    groups = _absorb_low_information_groups(groups)
    return _coarsen_task_groups(groups, maximum=_MAX_SEMANTIC_TASKS_PER_DAY)


def _absorb_low_information_groups(
    groups: Sequence[Sequence[_ActivityEvent]],
) -> list[list[_ActivityEvent]]:
    """Keep one-off fragments as evidence without presenting them as tasks."""

    result = [list(group) for group in groups if group]
    index = 0
    while len(result) > 1 and index < len(result):
        group = result[index]
        if not _is_low_information_group(group):
            index += 1
            continue
        if index == 0:
            target = 1
        elif index == len(result) - 1:
            target = index - 1
        else:
            previous_gap = max(
                0,
                group[0].created_at_ms - result[index - 1][-1].created_at_ms,
            )
            next_gap = max(
                0,
                result[index + 1][0].created_at_ms - group[-1].created_at_ms,
            )
            target = index - 1 if previous_gap <= next_gap else index + 1
        result[target] = sorted(
            [*result[target], *group],
            key=lambda value: (value.created_at_ms, value.event_id),
        )
        del result[index]
        if target < index:
            index = max(0, index - 1)
    return result


def _is_low_information_group(group: Sequence[_ActivityEvent]) -> bool:
    if len(group) != 1:
        return False
    return _is_low_information_text(group[0].text)


def _is_low_information_text(value: str) -> bool:
    text = compact_whitespace(value)
    if not text or is_redacted_or_sensitive(text):
        return True
    latin = re.sub(r"[^a-z0-9]+", "", text.casefold())
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    return cjk_count < 2 and len(latin) < 4


def _event_group_affinity(
    event: _ActivityEvent,
    group: Sequence[_ActivityEvent],
    *,
    max_gap_ms: int,
) -> float:
    latest = max(group, key=lambda value: (value.created_at_ms, value.event_id))
    gap = max(0, event.created_at_ms - latest.created_at_ms)
    event_groups = _event_context_groups(event)
    group_contexts = {
        value for item in group for value in _event_context_groups(item)
    }
    same_runtime_scope = bool(event_groups & group_contexts)
    event_terms = _event_semantic_terms(event)
    group_terms = {term for item in group for term in _event_semantic_terms(item)}
    shared = event_terms & group_terms
    salient_shared = _specific_semantic_terms(shared)
    overlap = len(shared) / max(1, min(len(event_terms), len(group_terms)))
    union = len(event_terms | group_terms)
    jaccard = len(shared) / max(1, union)
    score = 0.62 * overlap + 0.22 * jaccard
    score += min(0.48, 0.30 * len(salient_shared))
    event_app = _normalized_app(event)
    if event_app and any(_normalized_app(item) == event_app for item in group):
        score += 0.06
    # context_group_id is an App/document/project runtime scope, not a topic.
    # It can break a tie but must never force unrelated work into one task.
    if same_runtime_scope:
        score += 0.08
    if gap <= max_gap_ms:
        score += 0.08
    elif gap > _SEMANTIC_TASK_MAX_GAP_MS and not shared:
        return 0.0
    same_app = bool(
        event_app and any(_normalized_app(item) == event_app for item in group)
    )
    if not event_terms and same_app and gap <= max_gap_ms:
        score += 0.34
    return min(1.0, score)


def _obvious_topic_break(
    event: _ActivityEvent,
    current_group: Sequence[_ActivityEvent],
) -> bool:
    event_terms = _event_semantic_terms(event)
    group_terms = {
        term for item in current_group for term in _event_semantic_terms(item)
    }
    if not event_terms or not group_terms or event_terms & group_terms:
        return False
    event_contexts = _event_context_groups(event)
    group_contexts = {
        value for item in current_group for value in _event_context_groups(item)
    }
    event_concepts = {
        term for term in event_terms if term.startswith(("concept:", "action:"))
    }
    group_concepts = {
        term for term in group_terms if term.startswith(("concept:", "action:"))
    }
    if event_concepts and group_concepts and not (event_concepts & group_concepts):
        return True
    if event_contexts and group_contexts and not (event_contexts & group_contexts):
        return True
    return False


def _strong_cross_episode_match(
    left: Sequence[_ActivityEvent],
    right: Sequence[_ActivityEvent],
) -> bool:
    left_contexts = {
        value for item in left for value in _event_context_groups(item)
    }
    right_contexts = {
        value for item in right for value in _event_context_groups(item)
    }
    same_runtime_scope = bool(left_contexts & right_contexts)
    left_terms = {term for item in left for term in _event_semantic_terms(item)}
    right_terms = {term for item in right for term in _event_semantic_terms(item)}
    shared = left_terms & right_terms
    salient = _specific_semantic_terms(shared)
    overlap = len(shared) / max(1, min(len(left_terms), len(right_terms)))
    if len(salient) >= 2 and overlap >= 0.45:
        return True
    return same_runtime_scope and len(salient) >= 2 and overlap >= 0.55


def _coarsen_task_groups(
    groups: Sequence[Sequence[_ActivityEvent]],
    *,
    maximum: int,
) -> list[list[_ActivityEvent]]:
    """Keep the user-facing Timeline at work-block granularity.

    Fine-grained evidence remains inside each block. Only adjacent task groups
    are merged, so this cap cannot reorder the day or lose provenance.
    """

    result = [list(group) for group in groups if group]
    limit = max(1, int(maximum))
    while len(result) > limit:
        pair_index = max(
            range(len(result) - 1),
            key=lambda index: _adjacent_group_merge_score(
                result[index],
                result[index + 1],
            ),
        )
        result[pair_index] = sorted(
            [*result[pair_index], *result[pair_index + 1]],
            key=lambda value: (value.created_at_ms, value.event_id),
        )
        del result[pair_index + 1]
    return result


def _adjacent_group_merge_score(
    left: Sequence[_ActivityEvent],
    right: Sequence[_ActivityEvent],
) -> float:
    left_terms = {term for item in left for term in _event_semantic_terms(item)}
    right_terms = {term for item in right for term in _event_semantic_terms(item)}
    shared = left_terms & right_terms
    specific = _specific_semantic_terms(shared)
    left_end = max(item.created_at_ms for item in left)
    right_start = min(item.created_at_ms for item in right)
    gap = max(0, right_start - left_end)
    score = 1.4 * len(specific)
    score += 0.8 * (len(shared) / max(1, min(len(left_terms), len(right_terms))))
    if gap <= DEFAULT_SEGMENT_GAP_MS:
        score += 0.8
    elif gap <= _SEMANTIC_TASK_MAX_GAP_MS:
        score += 0.25
    left_apps = {_normalized_app(item) for item in left}
    right_apps = {_normalized_app(item) for item in right}
    if left_apps & right_apps:
        score += 0.25
    # Prefer absorbing a tiny follow-up into its surrounding work block instead
    # of leaving "继续" or one correction as a standalone Timeline task.
    score += 0.45 / max(1, min(len(left), len(right)))
    if _is_cas_account_group(left) != _is_cas_account_group(right) and not specific:
        score -= 1.5
    return score


def _specific_semantic_terms(terms: set[str]) -> set[str]:
    return {
        term
        for term in terms
        if term.startswith("action:")
        or term in {"concept:account", "concept:thesis"}
        or (
            not term.startswith("concept:")
            and len(term) >= 4
            and term not in _SEMANTIC_STOP_WORDS
        )
    }


def _is_cas_account_group(group: Sequence[_ActivityEvent]) -> bool:
    if len(group) > 12:
        return False
    terms = {term for item in group for term in _event_semantic_terms(item)}
    combined = compact_whitespace(" ".join(item.text for item in group)).casefold()
    return "cas" in combined and {
        "concept:codex",
        "concept:account",
        "action:switch",
    }.issubset(terms)


def _assert_event_conservation(
    events: Sequence[_ActivityEvent],
    groups: Sequence[Sequence[_ActivityEvent]],
) -> None:
    expected = [event.event_id for event in events]
    actual = [event.event_id for group in groups for event in group]
    if len(actual) != len(set(actual)):
        raise RuntimeError("semantic activity grouping duplicated an input event")
    if sorted(actual) != sorted(expected):
        raise RuntimeError("semantic activity grouping lost an input event")


def _event_context_groups(event: _ActivityEvent) -> set[str]:
    value = compact_whitespace(event.context_group_id).casefold()
    return {value} if value else set()


def _same_short_fragment_burst(
    event: _ActivityEvent,
    previous: _ActivityEvent,
) -> bool:
    gap = event.created_at_ms - previous.created_at_ms
    if gap < 0 or gap > _FRAGMENT_BURST_GAP_MS:
        return False
    if _normalized_app(event) != _normalized_app(previous):
        return False
    event_contexts = _event_context_groups(event)
    previous_contexts = _event_context_groups(previous)
    if event_contexts and previous_contexts and not (event_contexts & previous_contexts):
        return False
    current = compact_whitespace(event.text)
    before = compact_whitespace(previous.text)
    return (
        0 < len(current) <= 12
        and 0 < len(before) <= 12
        and min(len(current), len(before)) <= 4
    )


def _event_semantic_terms(event: _ActivityEvent) -> set[str]:
    text = compact_whitespace(
        " ".join((event.text, event.recent_context, event.preedit))
    ).casefold()
    if not text or is_redacted_or_sensitive(text):
        return set()
    terms = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9._-]{1,48}", text)
        if token not in _SEMANTIC_STOP_WORDS
    }
    for chunk in re.findall(r"[\u3400-\u9fff]{2,24}", text):
        terms.add(chunk)
        terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    aliases: set[str] = set()
    if terms & {"codex", "chatgpt", "openai", "gpt"} or "大模型" in text:
        aliases.add("concept:codex")
    if terms & {"cas", "account", "accounts", "login", "profile"} or any(
        value in text for value in ("账号", "账户", "登录")
    ):
        aliases.add("concept:account")
    if "cas" in terms:
        aliases.add("action:switch")
    if terms & {"switch", "switched", "change"} or "切换" in text:
        aliases.add("action:switch")
    if terms & {"memory", "rag", "longmemeval"} or "记忆" in text:
        aliases.add("concept:memory")
    if terms & {"ime", "squirrel", "rime"} or "输入法" in text:
        aliases.add("concept:ime")
    if terms & {"thesis", "paper", "bibliography", "citation"} or any(
        value in text for value in ("论文", "参考文献", "毕业")
    ):
        aliases.add("concept:thesis")
    return terms | aliases


_SEMANTIC_STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "into",
        "then",
        "继续",
        "今天",
        "完成",
        "进行",
    }
)


def _normalized_app(event: _ActivityEvent) -> str:
    return _safe_label(
        event.app,
        fallback=_safe_label(event.source, fallback="unknown-app"),
    ).casefold()


def _task_title(
    group: Sequence[_ActivityEvent],
    *,
    snippets: Sequence[str],
) -> str:
    combined = compact_whitespace(" ".join(snippets)).casefold()
    terms = {term for event in group for term in _event_semantic_terms(event)}
    coarse_title = _coarse_task_title(group)
    if coarse_title:
        return coarse_title
    if {
        "concept:codex",
        "concept:account",
        "action:switch",
    }.issubset(terms) and len(group) <= 12 and (
        "cas" in combined or any("cas" in value for value in terms)
    ):
        return "CAS 切换 Codex 账号"
    if sum("Git 合并" in _event_task_facets(event) for event in group) >= 2:
        return "合并分支并记录改动"
    visible = [value for value in snippets if value != "[敏感内容已脱敏]"]
    if not visible:
        return "敏感活动（内容已脱敏）"
    candidate = max(
        visible,
        key=lambda value: (len(_semantic_title_terms(value)), len(value)),
    )
    return truncate_text(candidate, 72)


def _coarse_task_title(group: Sequence[_ActivityEvent]) -> str:
    """Name a large work block by repeated facets, not one longest utterance."""

    if len(group) < 4:
        return ""
    facet_counts: dict[str, int] = {}
    facet_order = {
        "记忆系统": 0,
        "上下文捕获": 1,
        "前端界面": 2,
        "时间线": 3,
        "Agent 工具": 4,
        "输入法": 5,
        "Git 合并": 6,
    }
    for event in group:
        for facet in _event_task_facets(event):
            facet_counts[facet] = facet_counts.get(facet, 0) + 1
    if not facet_counts:
        return ""
    strongest = max(facet_counts.values())
    minimum_count = max(2, strongest // 3)
    facets = sorted(
        (
            (facet, count)
            for facet, count in facet_counts.items()
            if count >= minimum_count
        ),
        key=lambda value: (-value[1], facet_order[value[0]]),
    )[:3]
    if not facets:
        return ""
    labels = [facet for facet, _count in facets]
    if len(labels) == 1:
        return f"{labels[0]}优化"
    last_label = labels[-1]
    if last_label[0].isascii():
        last_label = f" {last_label}"
    return f"{'、'.join(labels[:-1])}与{last_label}协同优化"


def _event_task_facets(event: _ActivityEvent) -> set[str]:
    text = compact_whitespace(event.text).casefold()
    if not text or is_redacted_or_sensitive(text):
        return set()
    latin_terms = set(re.findall(r"[a-z0-9][a-z0-9._-]{1,48}", text))
    facets: set[str] = set()
    if any(
        value in text for value in ("记忆", "召回", "检索", "主题书", "原子")
    ) or latin_terms & {
        "rag",
        "book",
        "bm25",
        "embedding",
    }:
        facets.add("记忆系统")
    if any(
        value in text for value in ("上下文", "输入框", "无障碍")
    ) or latin_terms & {
        "ax",
        "context",
        "accessibility",
    }:
        facets.add("上下文捕获")
    if any(
        value in text for value in ("前端", "界面", "抽屉")
    ) or latin_terms & {
        "ui",
        "ux",
        "react",
        "vite",
        "css",
    }:
        facets.add("前端界面")
    if "时间线" in text or "timeline" in latin_terms:
        facets.add("时间线")
    if any(value in text for value in ("工具", "智能体")) or latin_terms & {
        "agent",
        "agents",
        "tool",
        "tools",
        "mcp",
        "pi",
    }:
        facets.add("Agent 工具")
    if any(
        value in text for value in ("输入法", "候选", "拼音")
    ) or latin_terms & {
        "ime",
        "squirrel",
        "rime",
    }:
        facets.add("输入法")
    if any(value in text for value in ("合并", "分支")) or "git" in latin_terms:
        facets.add("Git 合并")
    return facets


def _semantic_title_terms(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9][a-z0-9._-]{1,48}|[\u3400-\u9fff]{2,24}", value.casefold()))


def _day_bounds_ms(day: date, timezone: tzinfo) -> tuple[int, int]:
    start = datetime.combine(day, time.min, tzinfo=timezone)
    next_day = datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone)
    return int(start.timestamp() * 1_000), int(next_day.timestamp() * 1_000)


def activity_timeline_date_range(
    through_date: str,
    *,
    start_date: str = "",
) -> tuple[date | None, date]:
    """Validate inclusive source-day bounds without reading or changing state."""

    through = _validated_date(through_date)
    start = _validated_date(start_date) if start_date else None
    if start is not None and start > through:
        raise ValueError("timeline start date must not exceed through date")
    return start, through


def _validated_date(value: str) -> date:
    text = compact_whitespace(value)
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("timeline_date must use YYYY-MM-DD") from exc
    if day.isoformat() != text:
        raise ValueError("timeline_date must use YYYY-MM-DD")
    return day


def _validated_month(value: str) -> date:
    text = compact_whitespace(value)
    if not re.fullmatch(r"\d{4}-\d{2}", text):
        raise ValueError("timeline_month must use YYYY-MM")
    try:
        return date.fromisoformat(f"{text}-01")
    except ValueError as exc:
        raise ValueError("timeline_month must use YYYY-MM") from exc


def _resolve_timezone(value: str) -> tzinfo:
    name = compact_whitespace(value)
    if name:
        try:
            return ZoneInfo(name)
        except Exception as exc:
            raise ValueError(f"unknown timezone: {name}") from exc
    local = datetime.now().astimezone().tzinfo
    local_key = compact_whitespace(str(getattr(local, "key", "") or ""))
    if local_key:
        return local or ZoneInfo("UTC")
    # macOS commonly exposes only the ambiguous abbreviation ``CST`` here.
    # Persist an IANA name so semantic packets remain replayable and valid.
    return ZoneInfo(os.environ.get("RAG_IME_TIMEZONE", "Asia/Shanghai"))


def _timezone_name(value: tzinfo) -> str:
    key = compact_whitespace(str(getattr(value, "key", "") or ""))
    return key or compact_whitespace(str(value)) or "local"


def _safe_label(value: object, *, fallback: str) -> str:
    text = compact_whitespace(str(value or ""))
    if not text or is_redacted_or_sensitive(text):
        return fallback
    return truncate_text(text, 160)


def _safe_optional_label(value: object) -> str:
    text = compact_whitespace(str(value or ""))
    if not text or is_redacted_or_sensitive(text):
        return ""
    return truncate_text(text, 240)


def _required_text(value: object, name: str) -> str:
    text = compact_whitespace(str(value or ""))
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


def _stable_digest(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _json_ints(raw: object) -> list[int]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [
        int(item)
        for item in value
        if isinstance(item, int) and not isinstance(item, bool) and item > 0
    ]


def _json_objects(raw: object) -> list[dict[str, object]]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _json_mapping(raw: object) -> dict[str, object]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _placeholders(values: Sequence[object] | frozenset[str]) -> str:
    return ",".join("?" for _ in values)
