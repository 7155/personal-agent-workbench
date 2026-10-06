from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Protocol

from .agent_role_book import (
    AgentRoleBookStore,
    normalize_role_book_review_item,
)
from .contracts.json_schema import validate_contract
from .daily_planner import planning_context
from .db import apply_database_migrations
from .local_calendar import resolve_calendar_timezone
from .memory_ingest import normalize_text
from .knowledge_scope import (
    quarantine_scope_issue,
    room_public_scope,
    session_knowledge_scope,
)
from .sensitive_content import (
    contains_sensitive_content,
    is_redacted_or_sensitive,
    is_sensitive_mapping_key,
    redact_sensitive_text,
)
from .text_utils import compact_whitespace


EVIDENCE_SOURCE_KINDS = frozenset(
    {
        "user_message",
        "assistant_message",
        "tool_receipt",
        "session_digest",
        "room_event",
        "work_receipt",
    }
)
DEFAULT_BOOTSTRAP_MAX_CHARS = 6_000
DEFAULT_CONSOLIDATION_INTERVAL_MS = 24 * 60 * 60 * 1_000
_MAX_EVIDENCE_CHARS = 32_000
_MAX_JSON_BYTES = 64 * 1024
_RUN_STALE_AFTER_MS = 5 * 60 * 1_000
_ACTIVITY_CONTEXT_INTERNAL_SOURCES = frozenset(
    {
        "pi_agent_compaction",
        "pi_agent_tool_receipt",
    }
)
_ACTIVITY_CONTEXT_MAX_EVENT_IDS = 2_000
_ACTIVITY_CONTEXT_DEDUPE_WINDOW_MS = 5 * 60 * 1_000
_STABLE_ATOM_KINDS = frozenset(
    {
        "personal_fact",
        "personal_habit",
        "personal_principle",
        "durable_preference",
        "preference",
        "stable_memory",
        "user_preference",
    }
)
_ROLE_BOOK_MODEL_MAX_CHARS = 12_000
_ROLE_BOOK_MODEL_MAX_EVIDENCE_ITEMS = 24
_ROLE_BOOK_PROPOSAL_MAX_CHARS = 3_200
_ROLE_PROPOSAL_FIELDS = (
    "traitProposals",
    "capabilityProposals",
    "lessonProposals",
    "commitmentProposals",
)
_ROLE_PROPOSAL_SECTIONS = {
    "traitProposals": "personality",
    "capabilityProposals": "capabilities",
    "lessonProposals": "lessonsAndLimits",
    "commitmentProposals": "activeCommitments",
}
_ROLE_PROPOSAL_LIMITS = {
    "traitProposals": 6,
    "capabilityProposals": 12,
    "lessonProposals": 8,
    "commitmentProposals": 8,
}


class EvidenceConflictError(ValueError):
    """The same idempotency key was reused for different evidence."""


RoleBookApplier = Callable[[Mapping[str, object]], object]


class RoleBookOrganizer(Protocol):
    @property
    def provider_name(self) -> str: ...

    def curate_role_book(
        self,
        *,
        bundle: dict[str, object],
        project: str,
        role_id: str,
        role_version: str,
    ) -> dict[str, object]: ...


def local_date_for_timestamp(timestamp_ms: int) -> str:
    """Return the configured calendar date shared with the timeline builder."""

    return datetime.fromtimestamp(max(0, int(timestamp_ms)) / 1_000, resolve_calendar_timezone()).date().isoformat()


def local_day_bounds_ms(timeline_date: str) -> tuple[int, int]:
    day = date.fromisoformat(compact_whitespace(timeline_date))
    local_zone = resolve_calendar_timezone()
    start = datetime.combine(day, datetime_time.min, tzinfo=local_zone)
    end = datetime.combine(day + timedelta(days=1), datetime_time.min, tzinfo=local_zone)
    return int(start.timestamp() * 1_000), int(end.timestamp() * 1_000)


def load_activity_timeline_context(
    conn: sqlite3.Connection,
    *,
    project: str,
    timeline_date: str,
    timeline_id: str = "",
    max_segments: int = 8,
    max_chars: int = 2_400,
) -> dict[str, object]:
    """Load a bounded timeline view that can corroborate, but never prove, facts.

    Source event ids are deliberately absent from this contract. The owner-memory
    governor therefore cannot accidentally treat timeline activity as legal fact
    evidence. Internal Agent summaries/receipts and near-duplicate final-input
    checkpoints are also removed before any text reaches a model-facing bundle.
    """

    day = date.fromisoformat(compact_whitespace(timeline_date)).isoformat()
    bounded_segments = max(1, min(int(max_segments), 12))
    char_budget = max(400, min(int(max_chars), 2_400))
    identifier = compact_whitespace(timeline_id)
    row = None
    if identifier:
        row = conn.execute(
            """
            SELECT * FROM daily_activity_timelines
            WHERE timeline_id = ? AND project = ? AND timeline_date = ?
              AND status IN ('draft', 'approved')
            """,
            (identifier, project, day),
        ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT * FROM daily_activity_timelines
            WHERE project = ? AND timeline_date = ?
              AND status IN ('draft', 'approved')
            ORDER BY CASE status WHEN 'draft' THEN 0 ELSE 1 END,
                     updated_at_ms DESC, timeline_id DESC
            LIMIT 1
            """,
            (project, day),
        ).fetchone()

    if row is None:
        payload = _empty_activity_context(day)
        validate_contract(payload, "activity-timeline-context.v1.json")
        return payload

    raw_segments = [
        dict(value)
        for value in _json_list(row["segments_json"])
        if isinstance(value, Mapping)
    ]
    ordered_ids = list(
        dict.fromkeys(
            int(value)
            for segment in raw_segments
            for value in list(segment.get("sourceEventIds") or [])
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        )
    )
    sampled_ids = _sample_int_ids(
        ordered_ids,
        limit=_ACTIVITY_CONTEXT_MAX_EVENT_IDS,
    )
    event_rows: list[sqlite3.Row] = []
    if sampled_ids:
        placeholders = ",".join("?" for _ in sampled_ids)
        event_rows = conn.execute(
            f"""
            SELECT e.id, e.created_at_ms, e.source, e.committed_text,
                   e.app, e.context_group_id
            FROM input_events e
            LEFT JOIN memory_state state ON state.event_id = e.id
            WHERE e.id IN ({placeholders}) AND e.project = ?
              AND COALESCE(state.deleted, 0) = 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM memory_tombstones tombstone
                  WHERE tombstone.active = 1
                    AND (
                        (tombstone.target_type = 'source_event_id'
                         AND tombstone.target_value = CAST(e.id AS TEXT))
                        OR
                        (tombstone.target_type = 'memory_id'
                         AND tombstone.target_value = ('event:' || e.id))
                    )
              )
            ORDER BY e.created_at_ms ASC, e.id ASC
            """,  # noqa: S608 - placeholders are generated, never values
            (*sampled_ids, project),
        ).fetchall()

    retained: dict[int, dict[str, object]] = {}
    filtered_internal = 0
    deduplicated = 0
    redacted = 0
    last_seen: dict[tuple[str, str], int] = {}
    for event in event_rows:
        source = compact_whitespace(str(event["source"] or ""))
        if source in _ACTIVITY_CONTEXT_INTERNAL_SOURCES:
            filtered_internal += 1
            continue
        text = compact_whitespace(str(event["committed_text"] or ""))
        if not text:
            continue
        if is_redacted_or_sensitive(text):
            redacted += 1
            continue
        app = _bounded_text(event["app"], 240) or "unknown-app"
        occurred_at_ms = int(event["created_at_ms"] or 0)
        dedupe_key = (normalize_text(text), app.casefold())
        previous = last_seen.get(dedupe_key)
        if previous is not None and occurred_at_ms - previous <= _ACTIVITY_CONTEXT_DEDUPE_WINDOW_MS:
            deduplicated += 1
            continue
        last_seen[dedupe_key] = occurred_at_ms
        retained[int(event["id"])] = {
            "source": source or "unknown-source",
            "text": _truncate(text, 180),
            "app": app,
            "createdAtMs": occurred_at_ms,
            "contextGroupId": _bounded_text(event["context_group_id"], 240),
        }

    segments: list[dict[str, object]] = []
    remaining_chars = char_budget
    for raw in raw_segments:
        if len(segments) >= bounded_segments or remaining_chars <= 0:
            break
        segment_events = [
            retained[event_id]
            for event_id in list(raw.get("sourceEventIds") or [])
            if isinstance(event_id, int) and event_id in retained
        ]
        if not segment_events:
            continue
        snippets = list(
            dict.fromkeys(str(item["text"]) for item in segment_events if item.get("text"))
        )[:4]
        # Never reuse display fields derived before internal/sensitive evidence
        # was filtered. Rebuild the model-facing task solely from retained rows.
        apps = list(
            dict.fromkeys(str(item["app"]) for item in segment_events)
        )[:12]
        app = "multiple" if len(apps) > 1 else apps[0]
        title_source = max(snippets, key=lambda value: (len(value), value))
        # Input snippets may retain up to 180 characters for the richer
        # segment summary, while the public timeline contract limits titles
        # to 160. Bound the derived display field at its owning projection
        # boundary instead of letting one long input poison every curation
        # prepare/preview attempt for the same frozen evidence batch.
        title = _truncate(title_source, 160)
        detail = [value for value in snippets if value != title_source]
        summary = _truncate(
            title_source + (f"：{'；'.join(detail)}" if detail else ""),
            min(760, remaining_chars),
        )
        if not summary:
            continue
        source_kinds = list(
            dict.fromkeys(str(item["source"]) for item in segment_events)
        )[:12]
        context_groups = list(
            dict.fromkeys(
                str(item["contextGroupId"])
                for item in segment_events
                if item.get("contextGroupId")
            )
        )[:12]
        segment = {
            "segmentId": _bounded_text(raw.get("segmentId"), 160)
            or f"activity-context-segment:{len(segments)}",
            "position": len(segments),
            "app": app,
            "title": title,
            "apps": apps,
            "sourceKinds": source_kinds,
            "contextGroupIds": context_groups,
            "startMs": min(int(item["createdAtMs"]) for item in segment_events),
            "endMs": max(int(item["createdAtMs"]) for item in segment_events),
            "eventCount": len(segment_events),
            "summary": summary,
            "redactedEventCount": 0,
            "source": {
                "type": "activity_timeline",
                "id": str(row["timeline_id"]),
            },
            "ref": {
                "type": "timeline",
                "id": str(row["timeline_id"]),
                "segmentId": _bounded_text(raw.get("segmentId"), 160)
                or f"activity-context-segment:{len(segments)}",
            },
        }
        segments.append(segment)
        remaining_chars -= len(summary)

    summary = _truncate("；".join(str(item["summary"]) for item in segments), char_budget)
    if not summary:
        summary = "该时间线仅包含已过滤、重复或敏感事件。"
    payload = {
        "schemaVersion": "rag-ime.activity-timeline-context.v1",
        "available": True,
        "date": day,
        "timelineId": str(row["timeline_id"]),
        "status": str(row["status"]),
        "sourceEventHash": str(row["source_event_hash"]),
        "summary": summary,
        "segments": segments,
        "eventCount": int(row["event_count"] or 0),
        "retainedEventCount": len(retained),
        "filteredInternalEventCount": filtered_internal,
        "deduplicatedEventCount": deduplicated,
        "redactedEventCount": redacted,
        "corroborationOnly": True,
        "maySupportFacts": False,
        "source": {
            "type": "activity_timeline",
            "id": str(row["timeline_id"]),
        },
        "ref": {
            "type": "timeline",
            "id": str(row["timeline_id"]),
        },
    }
    validate_contract(payload, "activity-timeline-context.v1.json")
    return payload


def _empty_activity_context(timeline_date: str) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.activity-timeline-context.v1",
        "available": False,
        "date": timeline_date,
        "timelineId": "",
        "status": "unavailable",
        "sourceEventHash": "",
        "summary": "",
        "segments": [],
        "eventCount": 0,
        "retainedEventCount": 0,
        "filteredInternalEventCount": 0,
        "deduplicatedEventCount": 0,
        "redactedEventCount": 0,
        "corroborationOnly": True,
        "maySupportFacts": False,
    }


def _sample_int_ids(values: Sequence[int], *, limit: int) -> list[int]:
    ordered = list(dict.fromkeys(int(value) for value in values if int(value) > 0))
    bounded = max(1, int(limit))
    if len(ordered) <= bounded:
        return ordered
    if bounded == 1:
        return [ordered[-1]]
    final_index = len(ordered) - 1
    return list(
        dict.fromkeys(
            ordered[round(position * final_index / (bounded - 1))]
            for position in range(bounded)
        )
    )


class AgentMemoryEvidenceStore:
    """Durable evidence journal.

    Rows in this store are provenance-bearing observations. Recording a chat
    message here never promotes it to ``memory_atoms`` or another long-term
    factual store.
    """

    def __init__(self, db_path: str | Path, *, project: str = "") -> None:
        self.db_path = Path(db_path)
        self.project = compact_whitespace(project)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)

    def record(
        self,
        *,
        source_kind: str,
        source_id: str,
        text: str,
        role_id: str = "",
        session_id: str = "",
        idempotency_key: str = "",
        occurred_at_ms: int | None = None,
        provenance: Mapping[str, object] | None = None,
        metadata: Mapping[str, object] | None = None,
        privacy_class: str = "local",
    ) -> dict[str, object]:
        kind = _required_text(source_kind, "source_kind", 64).lower()
        if kind not in EVIDENCE_SOURCE_KINDS:
            raise ValueError(f"unsupported evidence source kind: {kind}")
        source = _required_text(source_id, "source_id", 320)
        from .memory_lifecycle.privacy import assess_capture

        decision = assess_capture(
            source=kind,
            text=text,
            metadata={"metadata": dict(metadata or {}), "provenance": dict(provenance or {})},
        )
        if not decision.allowed:
            return {
                "schemaVersion": "rag-ime.agent-memory-evidence-write.v1",
                "ok": True, "stored": False, "status": "skipped_privacy",
                "reason": decision.reason,
            }
        text = decision.text
        metadata = decision.metadata["metadata"]
        provenance = decision.metadata["provenance"]
        canonical = compact_whitespace(text)
        if not canonical:
            raise ValueError("evidence text must not be empty")
        if len(canonical) > _MAX_EVIDENCE_CHARS:
            raise ValueError(f"evidence text exceeds {_MAX_EVIDENCE_CHARS} characters")
        # Ingress may already have removed sensitive spans. A redacted body
        # cannot support an Evidence claim; metadata-only redaction is safe.
        if is_redacted_or_sensitive(canonical):
            return {
                "schemaVersion": "rag-ime.agent-memory-evidence-write.v1",
                "ok": True,
                "stored": False,
                "status": "skipped_sensitive",
                "sourceKind": kind,
                "sourceId": source,
            }

        role = _bounded_text(role_id, 240)
        session = _bounded_text(session_id, 240)
        idempotency = _bounded_text(idempotency_key, 400) or source
        privacy = _required_text(privacy_class, "privacy_class", 24).lower()
        if privacy not in {"local", "private"}:
            raise ValueError("privacy_class must be local or private")
        occurred = _now_ms() if occurred_at_ms is None else max(0, int(occurred_at_ms))
        recorded = _now_ms()
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        evidence_id = f"evidence:{_stable_digest(self.project, kind, idempotency)[:32]}"

        safe_provenance = _sanitize_mapping(provenance or {})
        safe_provenance.update(
            {
                "sourceType": kind,
                "sourceId": source,
                "project": self.project,
                "roleId": role,
                "sessionId": session,
            }
        )
        safe_metadata = _sanitize_mapping(metadata or {})
        provenance_json = _json_object_text(safe_provenance, "provenance")
        metadata_json = _json_object_text(safe_metadata, "metadata")

        with self._connect(immediate=True) as conn:
            from .memory_lifecycle.common import excluded

            if excluded(conn, project=self.project, session_id=session, source_id=source):
                return {
                    "schemaVersion": "rag-ime.agent-memory-evidence-write.v1",
                    "ok": True, "stored": False, "status": "skipped_privacy",
                    "reason": "capture_scope_excluded",
                }
            scope_columns = {
                "owner_kind": "user", "owner_id": "default",
                "knowledge_domain": "legacy", "scope_kind": "legacy", "scope_id": "",
                "visibility": "legacy", "authorization_revision": "", "binding_id": "",
                "scope_mode": "legacy",
            }
            room_id = compact_whitespace(str(safe_provenance.get("roomId") or ""))
            try:
                if room_id and kind in {"room_event", "work_receipt"}:
                    scope_status, _, scope_reason = session_knowledge_scope(conn, session)
                    if scope_status == "quarantined":
                        raise ValueError(scope_reason)
                    if scope_status == "authoritative":
                        scope_columns = room_public_scope(
                            conn,
                            room_id=room_id,
                            session_id=session,
                        ).columns()
                elif session:
                    scope_status, scope, scope_reason = session_knowledge_scope(conn, session)
                    if scope_status == "quarantined":
                        raise ValueError(scope_reason)
                    if scope is not None:
                        scope_columns = scope.columns()
            except ValueError as exc:
                quarantine_id = quarantine_scope_issue(
                    conn,
                    source_table="agent_memory_evidence",
                    source_id=evidence_id,
                    reason_code="invalid_server_binding",
                    observed_scope={"roomId": room_id, "sessionId": session, "error": str(exc)},
                    observed_at_ms=recorded,
                )
                return {
                    "schemaVersion": "rag-ime.agent-memory-evidence-write.v1",
                    "ok": True,
                    "stored": False,
                    "status": "quarantined",
                    "quarantineId": quarantine_id,
                    "sourceKind": kind,
                    "sourceId": source,
                }
            existing = conn.execute(
                """
                SELECT * FROM agent_memory_evidence
                WHERE project = ? AND source_kind = ? AND idempotency_key = ?
                """,
                (self.project, kind, idempotency),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["source_id"]) != source
                    or str(existing["content_sha256"]) != digest
                ):
                    raise EvidenceConflictError(
                        "evidence idempotency key already belongs to different content"
                    )
                return {
                    "schemaVersion": "rag-ime.agent-memory-evidence-write.v1",
                    "ok": True,
                    "stored": False,
                    "status": "already_recorded",
                    "evidence": _evidence_payload(existing),
                }

            conn.execute(
                """
                INSERT INTO agent_memory_evidence(
                    evidence_id, project, role_id, session_id, source_kind,
                    source_id, idempotency_key, content_text, content_sha256,
                    provenance_json, metadata_json, privacy_class, status,
                    occurred_at_ms, recorded_at_ms, owner_kind, owner_id,
                    knowledge_domain, scope_kind, scope_id, visibility,
                    authorization_revision, binding_id, scope_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    self.project,
                    role,
                    session,
                    kind,
                    source,
                    idempotency,
                    canonical,
                    digest,
                    provenance_json,
                    metadata_json,
                    privacy,
                    occurred,
                    recorded,
                    scope_columns["owner_kind"], scope_columns["owner_id"],
                    scope_columns["knowledge_domain"], scope_columns["scope_kind"],
                    scope_columns["scope_id"], scope_columns["visibility"],
                    scope_columns["authorization_revision"], scope_columns["binding_id"],
                    scope_columns["scope_mode"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_memory_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("evidence row was not persisted")
        return {
            "schemaVersion": "rag-ime.agent-memory-evidence-write.v1",
            "ok": True,
            "stored": True,
            "status": "recorded",
            "evidence": _evidence_payload(row),
        }

    def record_user_message(
        self,
        *,
        session_id: str,
        pi_entry_id: str,
        text: str,
        role_id: str = "",
        turn_id: str = "",
        occurred_at_ms: int | None = None,
    ) -> dict[str, object]:
        session = _required_text(session_id, "session_id", 240)
        entry = _required_text(pi_entry_id, "pi_entry_id", 320)
        return self.record(
            source_kind="user_message",
            source_id=entry,
            idempotency_key=f"{session}:{entry}",
            session_id=session,
            role_id=role_id,
            text=text,
            occurred_at_ms=occurred_at_ms,
            provenance={"piEntryId": entry, "turnId": _bounded_text(turn_id, 240)},
            metadata={"messageRole": "user", "factCandidate": False},
        )

    def record_assistant_message(
        self,
        *,
        session_id: str,
        pi_entry_id: str,
        text: str,
        role_id: str,
        turn_id: str = "",
        occurred_at_ms: int | None = None,
    ) -> dict[str, object]:
        session = _required_text(session_id, "session_id", 240)
        entry = _required_text(pi_entry_id, "pi_entry_id", 320)
        return self.record(
            source_kind="assistant_message",
            source_id=entry,
            idempotency_key=f"{session}:{entry}",
            session_id=session,
            role_id=role_id,
            text=text,
            occurred_at_ms=occurred_at_ms,
            provenance={"piEntryId": entry, "turnId": _bounded_text(turn_id, 240)},
            metadata={"messageRole": "assistant", "factCandidate": False},
        )

    def record_session_digest(
        self,
        *,
        session_id: str,
        digest_id: str,
        text: str,
        role_id: str,
        occurred_at_ms: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        session = _required_text(session_id, "session_id", 240)
        identifier = _required_text(digest_id, "digest_id", 320)
        return self.record(
            source_kind="session_digest",
            source_id=identifier,
            idempotency_key=f"{session}:{identifier}",
            session_id=session,
            role_id=role_id,
            text=text,
            occurred_at_ms=occurred_at_ms,
            provenance={"digestId": identifier},
            metadata=metadata,
        )

    def record_tool_receipt(
        self,
        receipt: Mapping[str, object] | None = None,
        *,
        receipt_id: str = "",
        text: str = "",
        session_id: str = "",
        role_id: str = "",
        applied: bool | None = None,
        occurred_at_ms: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        source = dict(receipt or {})
        nested = source.get("receipt") if isinstance(source.get("receipt"), Mapping) else {}
        nested = dict(nested)
        resolved_applied = (
            bool(applied)
            if applied is not None
            else bool(
                source.get("state") == "applied"
                and nested.get("mutationApplied") is True
            )
        )
        if not resolved_applied:
            return {
                "schemaVersion": "rag-ime.agent-memory-evidence-write.v1",
                "ok": True,
                "stored": False,
                "status": "skipped_unapplied",
                "sourceKind": "tool_receipt",
            }
        identifier = compact_whitespace(
            receipt_id
            or str(source.get("receiptId") or "")
            or str(source.get("approvalId") or "")
            or str(nested.get("receiptId") or "")
        )
        summary = compact_whitespace(
            text
            or str(nested.get("summary") or "")
            or str(source.get("summary") or "")
        )
        resolved_session = compact_whitespace(session_id or str(source.get("sessionId") or ""))
        merged_metadata = dict(metadata or {})
        merged_metadata.update(
            {
                "applied": True,
                "toolName": _bounded_text(source.get("toolName"), 160),
                "operation": _bounded_text(source.get("operation"), 160),
            }
        )
        return self.record(
            source_kind="tool_receipt",
            source_id=_required_text(identifier, "receipt_id", 320),
            idempotency_key=identifier,
            session_id=resolved_session,
            role_id=role_id,
            text=summary,
            occurred_at_ms=occurred_at_ms,
            provenance={
                "approvalId": _bounded_text(source.get("approvalId"), 320),
                "receiptId": identifier,
            },
            metadata=merged_metadata,
        )

    def record_room_event(
        self,
        *,
        room_id: str,
        event_id: str,
        text: str,
        role_id: str,
        session_id: str = "",
        event_type: str = "",
        accepted: bool = False,
        occurred_at_ms: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        room = _required_text(room_id, "room_id", 240)
        event = _required_text(event_id, "event_id", 320)
        merged_metadata = dict(metadata or {})
        merged_metadata.update(
            {
                "eventType": _bounded_text(event_type, 120),
                "accepted": bool(accepted),
            }
        )
        return self.record(
            source_kind="room_event",
            source_id=event,
            idempotency_key=f"{room}:{event}",
            session_id=session_id,
            role_id=role_id,
            text=text,
            occurred_at_ms=occurred_at_ms,
            provenance={"roomId": room, "eventId": event},
            metadata=merged_metadata,
        )

    def record_work_receipt(
        self,
        *,
        work_item_id: str,
        receipt_id: str,
        text: str,
        role_id: str,
        room_id: str = "",
        session_id: str = "",
        accepted: bool = True,
        occurred_at_ms: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        work_item = _required_text(work_item_id, "work_item_id", 320)
        receipt = _required_text(receipt_id, "receipt_id", 320)
        merged_metadata = dict(metadata or {})
        merged_metadata["accepted"] = bool(accepted)
        return self.record(
            source_kind="work_receipt",
            source_id=receipt,
            idempotency_key=f"{work_item}:{receipt}",
            session_id=session_id,
            role_id=role_id,
            text=text,
            occurred_at_ms=occurred_at_ms,
            provenance={
                "workItemId": work_item,
                "receiptId": receipt,
                "roomId": _bounded_text(room_id, 240),
            },
            metadata=merged_metadata,
        )

    def get(self, evidence_id: str) -> dict[str, object]:
        identifier = _required_text(evidence_id, "evidence_id", 320)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_memory_evidence WHERE evidence_id = ?",
                (identifier,),
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return _evidence_payload(row)

    def list(
        self,
        *,
        role_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        clauses = ["project = ?"]
        params: list[object] = [self.project]
        if role_id is not None:
            clauses.append("role_id = ?")
            params.append(compact_whitespace(role_id))
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(compact_whitespace(session_id))
        params.append(max(1, min(int(limit), 1_000)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM agent_memory_evidence
                WHERE {' AND '.join(clauses)}
                ORDER BY occurred_at_ms DESC, evidence_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_evidence_payload(row) for row in rows]

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


class MemoryBootstrapBuilder:
    """Build the one-shot, query-free memory payload for a new Agent session."""

    def __init__(self, db_path: str | Path, *, project: str = "") -> None:
        self.db_path = Path(db_path)
        self.project = compact_whitespace(project)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)

    def build(
        self,
        session_id: str,
        *,
        role_id: str = "",
        max_chars: int = DEFAULT_BOOTSTRAP_MAX_CHARS,
        generated_at_ms: int | None = None,
    ) -> dict[str, object]:
        session = _required_text(session_id, "session_id", 240)
        role = _bounded_text(role_id, 240)
        budget = max(512, min(int(max_chars), 12_000))
        generated = _now_ms() if generated_at_ms is None else max(0, int(generated_at_ms))
        self.initialize()
        with self._connect() as conn:
            candidates = {
                "stablePreferences": self._stable_preferences(conn),
                "projectState": self._project_state(conn),
                "topicBooks": self._topic_books(conn),
                "recentTimeline": self._recent_timeline(conn),
                "activeAtoms": self._active_atoms(conn),
                "oneRing": self._one_ring(conn, role_id=role),
            }
        payload = self._pack(
            session_id=session,
            role_id=role,
            max_chars=budget,
            generated_at_ms=generated,
            candidates=candidates,
        )
        validate_contract(payload, "memory-bootstrap.v1.json")
        bootstrap_id = str(payload["bootstrapId"])
        # This is intentionally the exact keyword shape accepted by
        # AgentContextRuntime.enqueue(**builder.build(...)).
        return {
            "session_id": session,
            "source_kind": "memory_bootstrap",
            "source_id": bootstrap_id,
            "title": "个人上下文启动快照",
            "summary": "仅在新 Session 首轮注入；后续由 Agent 按需调用记忆工具。",
            "payload": payload,
            "lane": "fact",
            "lifecycle": "once",
            "dedupe_key": f"memory-bootstrap:{session}:v1",
        }

    def _stable_preferences(self, conn: sqlite3.Connection) -> list[dict[str, object]]:
        placeholders = ",".join("?" for _ in _STABLE_ATOM_KINDS)
        rows = conn.execute(
            f"""
            SELECT * FROM memory_atoms
            WHERE status IN ('active', 'approved')
              AND claim_state = 'current'
              AND privacy_level != 'sensitive'
              AND kind IN ({placeholders})
              AND (scope_project = ? OR scope_project IS NULL OR scope_project = '')
            ORDER BY
              CASE WHEN scope_project = ? THEN 0 ELSE 1 END,
              quality_score DESC,
              confidence DESC,
              updated_at_ms DESC,
              id ASC
            LIMIT 24
            """,
            (*sorted(_STABLE_ATOM_KINDS), self.project, self.project),
        ).fetchall()
        return [
            item
            for row in rows
            if (item := _atom_bootstrap_item(row)) is not None
        ]

    def _active_atoms(self, conn: sqlite3.Connection) -> list[dict[str, object]]:
        placeholders = ",".join("?" for _ in _STABLE_ATOM_KINDS)
        rows = conn.execute(
            f"""
            SELECT * FROM memory_atoms
            WHERE status IN ('active', 'approved')
              AND claim_state = 'current'
              AND privacy_level != 'sensitive'
              AND kind NOT IN ({placeholders})
              AND (scope_project = ? OR scope_project IS NULL OR scope_project = '')
            ORDER BY
              CASE WHEN scope_project = ? THEN 0 ELSE 1 END,
              quality_score DESC,
              confidence DESC,
              updated_at_ms DESC,
              id ASC
            LIMIT 32
            """,
            (*sorted(_STABLE_ATOM_KINDS), self.project, self.project),
        ).fetchall()
        return [
            item
            for row in rows
            if (item := _atom_bootstrap_item(row)) is not None
        ]

    def _project_state(self, conn: sqlite3.Connection) -> list[dict[str, object]]:
        context = planning_context(
            conn,
            project=self.project,
            task_limit=8,
            goal_limit=4,
        )
        items: list[dict[str, object]] = []
        intention = compact_whitespace(str(context.get("intention") or ""))
        if intention and not _contains_sensitive_content(intention):
            items.append(
                {
                    "sourceType": "planning_item",
                    "sourceId": f"planning-intention:{context['date']}:{self.project or 'global'}",
                    "text": _truncate(f"今日意图：{intention}", 420),
                    "kind": "daily_intention",
                    "occurredAtMs": 0,
                    "maySupportFacts": True,
                    "provenance": {
                        "project": self.project,
                        "planDate": str(context["date"]),
                    },
                }
            )
        for task in context.get("openTasks", ()):
            if not isinstance(task, Mapping):
                continue
            title = compact_whitespace(str(task.get("title") or ""))
            detail = compact_whitespace(str(task.get("detail") or ""))
            text = compact_whitespace(
                "：".join(part for part in (title, detail) if part)
            )
            if not text or _contains_sensitive_content(text):
                continue
            items.append(
                {
                    "sourceType": "planning_item",
                    "sourceId": str(task.get("id") or ""),
                    "text": _truncate(
                        f"{'进行中' if task.get('status') == 'in_progress' else '待办'}：{text}",
                        520,
                    ),
                    "kind": "project_task",
                    "occurredAtMs": max(0, int(task.get("updatedAtMs") or 0)),
                    "maySupportFacts": True,
                    "provenance": {
                        "project": str(task.get("project") or ""),
                        "status": str(task.get("status") or ""),
                        "priority": int(task.get("priority") or 0),
                        "goalId": str(task.get("goalId") or ""),
                    },
                }
            )
        for goal in context.get("longTermGoals", ()):
            if not isinstance(goal, Mapping):
                continue
            title = compact_whitespace(str(goal.get("title") or ""))
            detail = compact_whitespace(str(goal.get("detail") or ""))
            text = compact_whitespace(
                "：".join(part for part in (title, detail) if part)
            )
            if not text or _contains_sensitive_content(text):
                continue
            items.append(
                {
                    "sourceType": "planning_item",
                    "sourceId": str(goal.get("id") or ""),
                    "text": _truncate(f"长期目标：{text}", 520),
                    "kind": "project_goal",
                    "occurredAtMs": max(0, int(goal.get("updatedAtMs") or 0)),
                    "maySupportFacts": True,
                    "provenance": {
                        "project": str(goal.get("project") or ""),
                        "status": str(goal.get("status") or ""),
                        "priority": int(goal.get("priority") or 0),
                        "targetDate": str(goal.get("targetDate") or ""),
                    },
                }
            )
        return items

    def _topic_books(self, conn: sqlite3.Connection) -> list[dict[str, object]]:
        rows = conn.execute(
            """
            SELECT * FROM memory_books
            WHERE status = 'active'
              AND book_type IN ('topic', 'project')
              AND (project = ? OR project = '')
              AND archived_at_ms IS NULL
            ORDER BY
              CASE WHEN project = ? THEN 0 ELSE 1 END,
              CASE book_type WHEN 'project' THEN 0 ELSE 1 END,
              quality_score DESC,
              confidence DESC,
              COALESCE(last_active_at_ms, updated_at_ms) DESC,
              book_id ASC
            LIMIT 16
            """,
            (self.project, self.project),
        ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            metadata = _json_object(row["metadata_json"])
            if bool(metadata.get("stale") or metadata.get("deprecated")):
                continue
            source_id = compact_whitespace(str(row["book_id"] or ""))
            if not source_id or _contains_sensitive_content(source_id):
                continue
            text = compact_whitespace(
                "：".join(
                    part
                    for part in (
                        str(row["title"] or ""),
                        str(row["summary"] or row["normalized_text"] or ""),
                    )
                    if compact_whitespace(part)
                )
            )
            if not text or _contains_sensitive_content(text):
                continue
            items.append(
                {
                    "sourceType": "memory_book",
                    "sourceId": source_id,
                    "text": _truncate(text, 640),
                    "kind": str(row["book_type"]),
                    "occurredAtMs": int(
                        row["last_active_at_ms"]
                        if row["last_active_at_ms"] is not None
                        else row["updated_at_ms"]
                    ),
                    "maySupportFacts": True,
                    "provenance": _sanitize_mapping(
                        {
                            "project": str(row["project"] or ""),
                            "sourceEventIds": _json_list(
                                row["source_event_ids_json"]
                            )[:12],
                            "memoryAtomIds": _json_list(
                                row["memory_atom_ids_json"]
                            )[:12],
                        }
                    ),
                }
            )
        return items

    def _recent_timeline(self, conn: sqlite3.Connection) -> list[dict[str, object]]:
        rows = conn.execute(
            """
            SELECT * FROM memory_books
            WHERE status = 'active'
              AND book_type = 'daily'
              AND (project = ? OR project = '')
              AND archived_at_ms IS NULL
            ORDER BY
              CASE WHEN project = ? THEN 0 ELSE 1 END,
              COALESCE(last_active_at_ms, updated_at_ms) DESC,
              quality_score DESC,
              book_id ASC
            LIMIT 4
            """,
            (self.project, self.project),
        ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            metadata = _json_object(row["metadata_json"])
            if bool(metadata.get("stale") or metadata.get("deprecated")):
                continue
            is_activity_timeline = (
                str(metadata.get("derivedArtifactType") or "")
                == "daily_activity_timeline"
            )
            if is_activity_timeline and str(
                metadata.get("approvalStatus") or ""
            ) != "approved":
                continue
            source_id = compact_whitespace(str(row["book_id"] or ""))
            if not source_id or _contains_sensitive_content(source_id):
                continue
            text = compact_whitespace(
                "：".join(
                    part
                    for part in (
                        str(row["title"] or ""),
                        str(row["summary"] or row["normalized_text"] or ""),
                    )
                    if compact_whitespace(part)
                )
            )
            if not text or _contains_sensitive_content(text):
                continue
            items.append(
                {
                    "sourceType": "memory_book",
                    "sourceId": source_id,
                    "text": _truncate(text, 640),
                    "kind": "daily_timeline",
                    "occurredAtMs": int(
                        row["last_active_at_ms"]
                        if row["last_active_at_ms"] is not None
                        else row["updated_at_ms"]
                    ),
                    # An approved activity timeline is useful session context,
                    # but remains an activity derivative rather than a user fact.
                    "maySupportFacts": not is_activity_timeline,
                    "provenance": _sanitize_mapping(
                        {
                            "project": str(row["project"] or ""),
                            "app": str(row["app"] or ""),
                            "bookKey": str(row["book_key"] or ""),
                            "timelineId": str(metadata.get("timelineId") or ""),
                            "sourceEventHash": str(
                                metadata.get("sourceEventHash") or ""
                            ),
                            "approvalStatus": str(
                                metadata.get("approvalStatus") or ""
                            ),
                            "sourceEventIds": _json_list(
                                row["source_event_ids_json"]
                            )[:20],
                            "memoryAtomIds": _json_list(
                                row["memory_atom_ids_json"]
                            )[:12],
                        }
                    ),
                }
            )
        return items

    def _one_ring(
        self,
        conn: sqlite3.Connection,
        *,
        role_id: str,
    ) -> list[dict[str, object]]:
        if role_id:
            role_clause = """
              AND (
                source_kind IN ('user_message', 'room_event')
                OR role_id IN ('', ?)
              )
            """
            params: tuple[object, ...] = (self.project, role_id)
        else:
            role_clause = ""
            params = (self.project,)
        rows = conn.execute(
            f"""
            SELECT * FROM agent_memory_evidence
            WHERE project = ?
              AND status = 'active'
              {role_clause}
            ORDER BY occurred_at_ms DESC, evidence_id DESC
            LIMIT 24
            """,
            params,
        ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            text = compact_whitespace(str(row["content_text"]))
            if not text or _contains_sensitive_content(text):
                continue
            source_kind = str(row["source_kind"])
            metadata = _json_object(row["metadata_json"])
            if source_kind in {"user_message", "assistant_message"}:
                # Pi already carries the current conversation history. Raw chat
                # Evidence remains auditable, but must not become a second,
                # query-free memory channel in a future Session.
                continue
            if source_kind == "tool_receipt" and metadata.get("applied") is not True:
                continue
            if source_kind in {"room_event", "work_receipt"} and metadata.get("accepted") is not True:
                continue
            items.append(
                {
                    "sourceType": "memory_evidence",
                    "sourceId": str(row["evidence_id"]),
                    "text": _truncate(text, 420),
                    "kind": source_kind,
                    "occurredAtMs": int(row["occurred_at_ms"]),
                    "maySupportFacts": False,
                    "provenance": {
                        "sourceId": str(row["source_id"]),
                        "sessionId": str(row["session_id"]),
                        **_select_provenance(_json_object(row["provenance_json"])),
                    },
                }
            )
        return items

    def _pack(
        self,
        *,
        session_id: str,
        role_id: str,
        max_chars: int,
        generated_at_ms: int,
        candidates: Mapping[str, Sequence[Mapping[str, object]]],
    ) -> dict[str, object]:
        section_names = (
            "stablePreferences",
            "projectState",
            "topicBooks",
            "recentTimeline",
            "activeAtoms",
            "oneRing",
        )
        caps = {
            "stablePreferences": 6,
            "projectState": 6,
            "topicBooks": 3,
            "recentTimeline": 2,
            "activeAtoms": 8,
            "oneRing": 6,
        }
        selected: dict[str, list[dict[str, object]]] = {
            name: [] for name in section_names
        }
        omitted = {name: len(candidates.get(name, ())) for name in section_names}
        source_ids: list[str] = []
        payload: dict[str, object] = {
            "schemaVersion": "rag-ime.memory-bootstrap.v1",
            "bootstrapId": "memory-bootstrap:pending",
            "sessionId": session_id,
            "project": self.project,
            "roleId": role_id,
            "generatedAtMs": generated_at_ms,
            "queryFree": True,
            "sections": selected,
            "sourceIds": source_ids,
            "budget": {
                "maxChars": max_chars,
                "usedChars": 0,
                "omittedCounts": omitted,
            },
            "policy": {
                "automaticRecall": "session_start_only",
                "lifecycle": "once",
                "oneRingMaySupportFacts": False,
                "rawDialogueIsLongTermFact": False,
                "layerBoundaries": {
                    "evidence": "原始来源，不等于当前事实。",
                    "atom": "仅使用经治理且仍为 current 的事实。",
                    "topicBook": "主题聚合用于找背景，不替代证据。",
                    "roleBook": "Agent 自身画像，不是用户事实。",
                    "timeline": "已批准活动只说明做过什么。",
                },
            },
        }
        base_fingerprint = _stable_digest(
            session_id,
            self.project,
            role_id,
            *(str(item.get("sourceId") or "") for values in candidates.values() for item in values),
        )
        payload["bootstrapId"] = f"memory-bootstrap:{base_fingerprint[:24]}"
        _update_payload_size(payload)
        if _serialized_chars(payload) > max_chars:
            raise ValueError(
                f"max_chars={max_chars} cannot hold the memory bootstrap contract"
            )

        # Round-robin gives every context class a chance before any one class
        # consumes the full bootstrap budget.
        for index in range(max(caps.values())):
            for section in section_names:
                values = candidates.get(section, ())
                if index >= min(caps[section], len(values)):
                    continue
                item = _with_stable_source_ref(values[index])
                selected[section].append(item)
                source_ids.append(str(item["sourceId"]))
                omitted[section] -= 1
                _update_payload_size(payload)
                if _serialized_chars(payload) <= max_chars:
                    continue
                selected[section].pop()
                source_ids.pop()
                omitted[section] += 1
                _update_payload_size(payload)

        _update_payload_size(payload)
        if _serialized_chars(payload) > max_chars:  # pragma: no cover - defensive
            raise RuntimeError("memory bootstrap exceeded its strict character budget")
        return payload

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


class PersonalContextConsolidator:
    """Incrementally derive daily drafts from evidence, never from raw facts."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        project: str = "",
        role_book_applier: RoleBookApplier | object | None = None,
        role_book_organizer: RoleBookOrganizer | object | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.project = compact_whitespace(project)
        self.role_book_applier = role_book_applier
        self.role_book_organizer = role_book_organizer

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)

    def due(
        self,
        role_id: str,
        *,
        now_ms: int | None = None,
        min_interval_ms: int = DEFAULT_CONSOLIDATION_INTERVAL_MS,
    ) -> dict[str, object]:
        role = _required_text(role_id, "role_id", 240)
        now = _now_ms() if now_ms is None else max(0, int(now_ms))
        interval = max(0, int(min_interval_ms))
        self.initialize()
        with self._connect() as conn:
            cursor = _cursor_row(conn, self.project, role)
            recoverable = _recoverable_run(conn, self.project, role, cursor, now)
            if recoverable is not None:
                return {
                    "due": True,
                    "reason": "retry_failed"
                    if str(recoverable["status"]) == "failed"
                    else "retry_stale_running",
                    "cursor": _cursor_payload(cursor),
                    "runId": str(recoverable["run_id"]),
                }
            next_row = _next_evidence_row(conn, self.project, role, cursor)
        if next_row is None:
            return {
                "due": False,
                "reason": "no_evidence",
                "cursor": _cursor_payload(cursor),
                "runId": "",
            }
        last_succeeded = int(cursor["last_succeeded_at_ms"])
        if last_succeeded and now < last_succeeded + interval:
            return {
                "due": False,
                "reason": "interval_not_elapsed",
                "nextDueAtMs": last_succeeded + interval,
                "cursor": _cursor_payload(cursor),
                "runId": "",
            }
        return {
            "due": True,
            "reason": "new_evidence",
            "cursor": _cursor_payload(cursor),
            "runId": "",
        }

    def run(
        self,
        role_id: str,
        role_version: str,
        *,
        now_ms: int | None = None,
        min_interval_ms: int = DEFAULT_CONSOLIDATION_INTERVAL_MS,
        force: bool = False,
        apply_safe_recent_work: bool = False,
        batch_limit: int = 500,
        activity_timeline_id: str = "",
    ) -> dict[str, object]:
        role = _required_text(role_id, "role_id", 240)
        version = _required_text(role_version, "role_version", 240)
        now = _now_ms() if now_ms is None else max(0, int(now_ms))
        if apply_safe_recent_work and self.role_book_applier is None:
            raise ValueError(
                "apply_safe_recent_work requires an idempotent role_book_applier"
            )
        self.initialize()
        claim = self._claim(
            role_id=role,
            role_version=version,
            now_ms=now,
            min_interval_ms=max(0, int(min_interval_ms)),
            force=bool(force),
            batch_limit=max(1, min(int(batch_limit), 1_000)),
        )
        status = str(claim["status"])
        if status == "succeeded_existing":
            output = dict(claim["output"])
            output.pop("_alreadySucceeded", None)
            return output
        if status in {"not_due", "no_evidence", "in_progress"}:
            return {
                "schemaVersion": "rag-ime.personal-context-consolidation-result.v1",
                "runId": str(claim.get("runId") or ""),
                "status": status,
                "reason": str(claim.get("reason") or ""),
                "digest": {},
                "userMemoryDraft": {},
                "roleBookDraft": {},
                "appliedRoleBookRevisionId": "",
                "proposedRoleBookRevisionId": "",
                "cursor": dict(claim["cursor"]),
            }

        run_id = str(claim["runId"])
        evidence = list(claim["evidence"])
        window_start_ms = int(claim["windowStartMs"])
        window_end_ms = int(claim["windowEndMs"])
        try:
            timeline_date = local_date_for_timestamp(window_start_ms)
            with self._connect() as conn:
                activity_context = load_activity_timeline_context(
                    conn,
                    project=self.project,
                    timeline_date=timeline_date,
                    timeline_id=_bounded_text(activity_timeline_id, 320),
                )
            digest = _build_daily_digest(
                run_id=run_id,
                project=self.project,
                role_id=role,
                evidence=evidence,
                activity_context=activity_context,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                generated_at_ms=now,
            )
            active_role_book = AgentRoleBookStore(self.db_path).active(role, version)
            model_proposals, proposal_diagnostics = self._curate_role_book_proposals(
                run_id=run_id,
                role_id=role,
                role_version=version,
                evidence=evidence,
                activity_context=activity_context,
                active_role_book=active_role_book,
            )
            user_memory_draft = _build_user_memory_draft(
                run_id=run_id,
                project=self.project,
                role_id=role,
                digest=digest,
                evidence=evidence,
                created_at_ms=now,
            )
            role_book_draft = _build_role_book_draft(
                run_id=run_id,
                project=self.project,
                role_id=role,
                role_version=version,
                digest=digest,
                evidence=evidence,
                model_proposals=model_proposals,
                proposal_diagnostics=proposal_diagnostics,
                created_at_ms=now,
            )
            applied_revision_id = ""
            if apply_safe_recent_work and role_book_draft["patch"]["recentWork"]:
                applied_revision_id = self._apply_safe_recent_work(
                    run_id=run_id,
                    role_book_draft=role_book_draft,
                )
            proposed_revision_id = self._persist_role_book_review_draft(
                role_book_draft=role_book_draft,
                created_at_ms=now,
            )
            output = {
                "schemaVersion": "rag-ime.personal-context-consolidation-result.v1",
                "runId": run_id,
                "status": "succeeded",
                "reason": "drafts_created",
                "digest": digest,
                "userMemoryDraft": user_memory_draft,
                "roleBookDraft": role_book_draft,
                "appliedRoleBookRevisionId": applied_revision_id,
                "proposedRoleBookRevisionId": proposed_revision_id,
            }
            cursor = self._complete_success(
                claim=claim,
                output=output,
                completed_at_ms=now,
            )
            output["cursor"] = cursor
            return output
        except Exception as exc:
            error = _truncate(compact_whitespace(str(exc)) or exc.__class__.__name__, 800)
            self._complete_failure(run_id, error=error, failed_at_ms=now)
            return {
                "schemaVersion": "rag-ime.personal-context-consolidation-result.v1",
                "runId": run_id,
                "status": "failed",
                "reason": "consolidation_failed",
                "error": error,
                "digest": {},
                "userMemoryDraft": {},
                "roleBookDraft": {},
                "appliedRoleBookRevisionId": "",
                "proposedRoleBookRevisionId": "",
                "cursor": dict(claim["cursor"]),
            }

    def cursor(self, role_id: str) -> dict[str, object]:
        role = _required_text(role_id, "role_id", 240)
        self.initialize()
        with self._connect() as conn:
            row = _cursor_row(conn, self.project, role)
        return _cursor_payload(row)

    def _claim(
        self,
        *,
        role_id: str,
        role_version: str,
        now_ms: int,
        min_interval_ms: int,
        force: bool,
        batch_limit: int,
    ) -> dict[str, object]:
        with self._connect(immediate=True) as conn:
            cursor = _cursor_row(conn, self.project, role_id)
            recoverable = _recoverable_run(
                conn, self.project, role_id, cursor, now_ms
            )
            if recoverable is not None:
                evidence_ids = [
                    str(value)
                    for value in _json_list(recoverable["source_evidence_ids_json"])
                    if str(value)
                ]
                evidence = _evidence_rows_by_ids(conn, evidence_ids)
                if len(evidence) == len(evidence_ids):
                    conn.execute(
                        """
                        UPDATE personal_context_consolidation_runs
                        SET status = 'running',
                            attempt_count = attempt_count + 1,
                            role_version = ?,
                            error_text = '',
                            started_at_ms = ?,
                            updated_at_ms = ?
                        WHERE run_id = ?
                        """,
                        (role_version, now_ms, now_ms, recoverable["run_id"]),
                    )
                    return _claim_payload(recoverable, cursor, evidence)

            running = conn.execute(
                """
                SELECT * FROM personal_context_consolidation_runs
                WHERE project = ? AND role_id = ? AND status = 'running'
                  AND start_cursor_at_ms = ?
                  AND start_cursor_evidence_id = ?
                ORDER BY created_at_ms DESC, run_id DESC
                LIMIT 1
                """,
                (
                    self.project,
                    role_id,
                    cursor["last_evidence_at_ms"],
                    cursor["last_evidence_id"],
                ),
            ).fetchone()
            if running is not None:
                return {
                    "status": "in_progress",
                    "reason": "run_already_claimed",
                    "runId": str(running["run_id"]),
                    "cursor": _cursor_payload(cursor),
                }

            next_row = _next_evidence_row(conn, self.project, role_id, cursor)
            if next_row is None:
                return {
                    "status": "no_evidence",
                    "reason": "no_evidence",
                    "runId": "",
                    "cursor": _cursor_payload(cursor),
                }
            _, evidence_day_end_ms = local_day_bounds_ms(
                local_date_for_timestamp(int(next_row["occurred_at_ms"]))
            )
            last_succeeded = int(cursor["last_succeeded_at_ms"])
            if (
                not force
                and last_succeeded
                and now_ms < last_succeeded + min_interval_ms
            ):
                return {
                    "status": "not_due",
                    "reason": "interval_not_elapsed",
                    "runId": "",
                    "cursor": _cursor_payload(cursor),
                }
            rows = conn.execute(
                """
                SELECT * FROM agent_memory_evidence
                WHERE project = ? AND role_id = ? AND status = 'active'
                  AND (
                    occurred_at_ms > ?
                    OR (occurred_at_ms = ? AND evidence_id > ?)
                  )
                  AND occurred_at_ms <= ?
                  AND occurred_at_ms < ?
                ORDER BY occurred_at_ms ASC, evidence_id ASC
                LIMIT ?
                """,
                (
                    self.project,
                    role_id,
                    cursor["last_evidence_at_ms"],
                    cursor["last_evidence_at_ms"],
                    cursor["last_evidence_id"],
                    now_ms,
                    evidence_day_end_ms,
                    batch_limit,
                ),
            ).fetchall()
            if not rows:
                return {
                    "status": "no_evidence",
                    "reason": "no_evidence_in_window",
                    "runId": "",
                    "cursor": _cursor_payload(cursor),
                }
            evidence = [_evidence_payload(row) for row in rows]
            ids = [str(item["evidenceId"]) for item in evidence]
            last = rows[-1]
            idempotency = _stable_digest(
                self.project,
                role_id,
                str(cursor["last_evidence_at_ms"]),
                str(cursor["last_evidence_id"]),
                *ids,
            )
            run_id = f"personal-context:{idempotency[:24]}"
            existing = conn.execute(
                """
                SELECT * FROM personal_context_consolidation_runs
                WHERE project = ? AND role_id = ? AND idempotency_key = ?
                """,
                (self.project, role_id, idempotency),
            ).fetchone()
            if existing is not None:
                if str(existing["status"]) == "succeeded":
                    output = _json_object(existing["output_json"])
                    output["cursor"] = _cursor_payload(cursor)
                    output["_alreadySucceeded"] = True
                    return {
                        "status": "succeeded_existing",
                        "runId": str(existing["run_id"]),
                        "output": output,
                        "cursor": _cursor_payload(cursor),
                    }
                conn.execute(
                    """
                    UPDATE personal_context_consolidation_runs
                    SET status = 'running', role_version = ?,
                        attempt_count = attempt_count + 1, error_text = '',
                        started_at_ms = ?, updated_at_ms = ?
                    WHERE run_id = ?
                    """,
                    (role_version, now_ms, now_ms, existing["run_id"]),
                )
                return _claim_payload(existing, cursor, evidence)

            window_start_ms = int(rows[0]["occurred_at_ms"])
            window_end_ms = int(last["occurred_at_ms"])
            conn.execute(
                """
                INSERT INTO personal_context_consolidation_runs(
                    run_id, project, role_id, role_version, idempotency_key,
                    status, start_cursor_at_ms, start_cursor_evidence_id,
                    end_cursor_at_ms, end_cursor_evidence_id,
                    window_start_ms, window_end_ms, source_evidence_ids_json,
                    output_json, attempt_count, error_text, created_at_ms,
                    started_at_ms, updated_at_ms
                ) VALUES (
                    ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, '{}',
                    1, '', ?, ?, ?
                )
                """,
                (
                    run_id,
                    self.project,
                    role_id,
                    role_version,
                    idempotency,
                    cursor["last_evidence_at_ms"],
                    cursor["last_evidence_id"],
                    int(last["occurred_at_ms"]),
                    str(last["evidence_id"]),
                    window_start_ms,
                    window_end_ms,
                    json.dumps(ids, ensure_ascii=False, separators=(",", ":")),
                    now_ms,
                    now_ms,
                    now_ms,
                ),
            )
            return {
                "status": "claimed",
                "runId": run_id,
                "cursor": _cursor_payload(cursor),
                "evidence": evidence,
                "windowStartMs": window_start_ms,
                "windowEndMs": window_end_ms,
                "endCursorAtMs": int(last["occurred_at_ms"]),
                "endCursorEvidenceId": str(last["evidence_id"]),
            }

    def _curate_role_book_proposals(
        self,
        *,
        run_id: str,
        role_id: str,
        role_version: str,
        evidence: Sequence[Mapping[str, object]],
        activity_context: Mapping[str, object],
        active_role_book: Mapping[str, object] | None,
    ) -> tuple[dict[str, list[dict[str, object]]], dict[str, object]]:
        empty = {field: [] for field in _ROLE_PROPOSAL_FIELDS}
        organizer = self.role_book_organizer
        if organizer is None:
            return empty, {
                "status": "not_configured",
                "provider": "",
                "inputChars": 0,
                "acceptedProposalCount": 0,
                "rejectedProposalCount": 0,
            }
        bundle = _build_role_book_model_bundle(
            role_id=role_id,
            role_version=role_version,
            evidence=evidence,
            activity_context=activity_context,
            active_role_book=active_role_book,
        )
        curation_evidence = list(bundle.get("curationEvidence") or [])
        provider = _bounded_text(getattr(organizer, "provider_name", ""), 80)
        if not curation_evidence:
            return empty, {
                "status": "no_eligible_evidence",
                "provider": provider,
                "inputChars": _serialized_chars(bundle),
                "acceptedProposalCount": 0,
                "rejectedProposalCount": 0,
            }
        method = getattr(organizer, "curate_role_book", None)
        if not callable(method):
            return empty, {
                "status": "unsupported",
                "provider": provider,
                "inputChars": _serialized_chars(bundle),
                "acceptedProposalCount": 0,
                "rejectedProposalCount": 0,
            }
        model_run_started = False
        try:
            begin_model_run = getattr(organizer, "begin_run", None)
            if callable(begin_model_run):
                frozen_input_sha256 = hashlib.sha256(
                    json.dumps(
                        bundle,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                begin_model_run(
                    f"role-book-curation:{run_id}",
                    frozen_input_sha256=frozen_input_sha256,
                )
                model_run_started = True
            raw = method(
                bundle=bundle,
                project=self.project,
                role_id=role_id,
                role_version=role_version,
            )
            normalized, rejected = _normalize_model_role_proposals(
                raw,
                allowed_evidence_ids={
                    str(item["evidenceId"])
                    for item in curation_evidence
                    if isinstance(item, Mapping)
                },
                active_role_book=active_role_book,
            )
            if model_run_started:
                finish_model_run = getattr(organizer, "finish_run", None)
                if callable(finish_model_run):
                    finish_model_run()
                model_run_started = False
        except Exception as exc:
            if model_run_started:
                fail_model_run = getattr(organizer, "fail_run", None)
                if callable(fail_model_run):
                    try:
                        fail_model_run(exc)
                    except Exception:
                        pass
            return empty, {
                "status": "failed",
                "provider": provider,
                "inputChars": _serialized_chars(bundle),
                "acceptedProposalCount": 0,
                "rejectedProposalCount": 0,
                "error": _truncate(
                    compact_whitespace(str(exc)) or exc.__class__.__name__,
                    400,
                ),
            }
        return normalized, {
            "status": "completed",
            "provider": provider,
            "inputChars": _serialized_chars(bundle),
            "acceptedProposalCount": sum(len(value) for value in normalized.values()),
            "rejectedProposalCount": rejected,
        }

    def _persist_role_book_review_draft(
        self,
        *,
        role_book_draft: Mapping[str, object],
        created_at_ms: int,
    ) -> str:
        patch = role_book_draft.get("patch")
        patch = patch if isinstance(patch, Mapping) else {}
        source_ids = {
            str(value)
            for value in list(role_book_draft.get("sourceEvidenceIds") or [])
            if str(value)
        }
        store = AgentRoleBookStore(self.db_path)
        active = store.active(
            role_book_draft.get("roleId"),
            role_book_draft.get("baseRoleVersion"),
        )
        if active is None:
            return ""
        sections = active.get("sections")
        sections = sections if isinstance(sections, Mapping) else {}
        updates: dict[str, object] = {}
        for field in _ROLE_PROPOSAL_FIELDS:
            section = _ROLE_PROPOSAL_SECTIONS[field]
            incoming: list[dict[str, object]] = []
            values = patch.get(field)
            if not isinstance(values, Sequence) or isinstance(
                values, (str, bytes, bytearray)
            ):
                continue
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                evidence_ids = [
                    str(item)
                    for item in list(value.get("sourceEvidenceIds") or [])
                    if str(item)
                ]
                if not evidence_ids or not set(evidence_ids).issubset(source_ids):
                    continue
                try:
                    incoming.append(
                        _role_book_review_item(
                            value,
                            section=section,
                            draft_id=str(role_book_draft["draftId"]),
                            observed_at_ms=created_at_ms,
                        )
                    )
                except (TypeError, ValueError):
                    continue
            merged = _merge_role_book_review_items(
                sections.get(section),
                incoming,
                limit=_ROLE_PROPOSAL_LIMITS[field],
            )
            existing = [
                dict(item)
                for item in list(sections.get(section) or [])
                if isinstance(item, Mapping)
            ]
            if incoming and merged != existing:
                updates[section] = merged
        if not updates:
            return ""
        revision = store.propose_revision_idempotent(
            role_book_draft.get("roleId"),
            role_book_draft.get("baseRoleVersion"),
            updates,
            idempotency_key=f"review:{role_book_draft.get('draftId') or ''}",
            change_summary=(
                "Daily review-only Role Book proposals backed by active Agent evidence; "
                f"digest={role_book_draft.get('sourceDigestId') or ''}"
            ),
            created_at_ms=created_at_ms,
        )
        if str(revision.get("status") or "") != "draft":
            raise ValueError("daily Role Book review revision must remain a draft")
        return str(revision.get("revisionId") or "")

    def _apply_safe_recent_work(
        self,
        *,
        run_id: str,
        role_book_draft: Mapping[str, object],
    ) -> str:
        patch = role_book_draft.get("patch")
        patch = dict(patch) if isinstance(patch, Mapping) else {}
        request = {
            "schemaVersion": "rag-ime.role-book-safe-recent-work-apply.v1",
            "idempotencyKey": str(role_book_draft["draftId"]),
            "runId": run_id,
            "project": self.project,
            "roleId": str(role_book_draft["roleId"]),
            "baseRoleVersion": str(role_book_draft["baseRoleVersion"]),
            "sourceDigestId": str(role_book_draft["sourceDigestId"]),
            # Only this field crosses the automatic-apply boundary.
            "recentWork": list(patch.get("recentWork") or []),
        }
        applier = self.role_book_applier
        if applier is None:  # pragma: no cover - checked by run()
            raise RuntimeError("role book applier is unavailable")
        method = getattr(applier, "apply_safe_recent_work", None)
        result = method(request) if callable(method) else applier(request)  # type: ignore[operator]
        if isinstance(result, Mapping):
            revision_id = compact_whitespace(
                str(
                    result.get("revisionId")
                    or result.get("roleBookRevisionId")
                    or result.get("id")
                    or ""
                )
            )
        else:
            revision_id = compact_whitespace(str(result or ""))
        if not revision_id:
            raise ValueError("role_book_applier did not return a revision id")
        return revision_id

    def _complete_success(
        self,
        *,
        claim: Mapping[str, object],
        output: Mapping[str, object],
        completed_at_ms: int,
    ) -> dict[str, object]:
        run_id = str(claim["runId"])
        serialized = json.dumps(
            dict(output),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE personal_context_consolidation_runs
                SET status = 'succeeded', output_json = ?, error_text = '',
                    completed_at_ms = ?, updated_at_ms = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (serialized, completed_at_ms, completed_at_ms, run_id),
            )
            cursor = _cursor_row(conn, self.project, str(claim["roleId"])) if "roleId" in claim else None
            if cursor is None:
                cursor = _cursor_row(
                    conn,
                    self.project,
                    str(output["digest"]["roleId"]),  # type: ignore[index]
                )
            expected_at = int(claim["cursor"]["lastEvidenceAtMs"])  # type: ignore[index]
            expected_id = str(claim["cursor"]["lastEvidenceId"])  # type: ignore[index]
            role_id = str(output["digest"]["roleId"])  # type: ignore[index]
            existing = conn.execute(
                """
                SELECT * FROM personal_context_consolidation_cursors
                WHERE project = ? AND role_id = ?
                """,
                (self.project, role_id),
            ).fetchone()
            if existing is None:
                if expected_at != 0 or expected_id:
                    raise RuntimeError("consolidation cursor changed while run was active")
                conn.execute(
                    """
                    INSERT INTO personal_context_consolidation_cursors(
                        project, role_id, last_evidence_at_ms, last_evidence_id,
                        last_successful_run_id, last_succeeded_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.project,
                        role_id,
                        int(claim["endCursorAtMs"]),
                        str(claim["endCursorEvidenceId"]),
                        run_id,
                        completed_at_ms,
                        completed_at_ms,
                    ),
                )
            elif (
                int(existing["last_evidence_at_ms"]) != expected_at
                or str(existing["last_evidence_id"]) != expected_id
            ):
                raise RuntimeError("consolidation cursor changed while run was active")
            else:
                conn.execute(
                    """
                    UPDATE personal_context_consolidation_cursors
                    SET last_evidence_at_ms = ?, last_evidence_id = ?,
                        last_successful_run_id = ?, last_succeeded_at_ms = ?,
                        updated_at_ms = ?
                    WHERE project = ? AND role_id = ?
                    """,
                    (
                        int(claim["endCursorAtMs"]),
                        str(claim["endCursorEvidenceId"]),
                        run_id,
                        completed_at_ms,
                        completed_at_ms,
                        self.project,
                        role_id,
                    ),
                )
            row = _cursor_row(conn, self.project, role_id)
        return _cursor_payload(row)

    def _complete_failure(
        self,
        run_id: str,
        *,
        error: str,
        failed_at_ms: int,
    ) -> None:
        with self._connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE personal_context_consolidation_runs
                SET status = 'failed', error_text = ?, completed_at_ms = ?,
                    updated_at_ms = ?
                WHERE run_id = ?
                """,
                (error, failed_at_ms, failed_at_ms, run_id),
            )

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


def _build_daily_digest(
    *,
    run_id: str,
    project: str,
    role_id: str,
    evidence: Sequence[Mapping[str, object]],
    activity_context: Mapping[str, object],
    window_start_ms: int,
    window_end_ms: int,
    generated_at_ms: int,
) -> dict[str, object]:
    counts = Counter(str(item["sourceKind"]) for item in evidence)
    recent = list(evidence)[-12:]
    highlights = [_digest_item(item) for item in recent]
    recent_work = [
        _digest_item(item)
        for item in evidence
        if _is_verified_recent_work(item)
    ][-8:]
    count_text = "、".join(
        f"{kind} {count} 条" for kind, count in sorted(counts.items())
    )
    timeline_segment_count = len(list(activity_context.get("segments") or []))
    summary = (
        f"本次按增量游标整理 {len(evidence)} 条证据（{count_text}）。"
        f"其中 {len(recent_work)} 条是带回执或验收来源的近期工作；"
        f"同日活动时间线提供 {timeline_segment_count} 个辅助片段；"
        "原始对话和时间线仅作为待判断证据，没有升级为长期事实。"
    )
    activity_timeline_id = compact_whitespace(
        str(activity_context.get("timelineId") or "")
    )
    payload = {
        "schemaVersion": "rag-ime.daily-conversation-digest.v1",
        "digestId": f"daily-digest:{_stable_digest(run_id, 'digest')[:24]}",
        "project": project,
        "roleId": role_id,
        "window": {"startMs": window_start_ms, "endMs": window_end_ms},
        "sourceEvidenceIds": [str(item["evidenceId"]) for item in evidence],
        # The digest keeps only the governed artifact identity. Raw input-event
        # ids never cross this corroboration-only timeline context boundary.
        "activityTimelineId": activity_timeline_id,
        "activityContext": dict(activity_context),
        "sourceCounts": dict(sorted(counts.items())),
        "summary": summary,
        "highlights": highlights,
        "recentWork": recent_work,
        "caveats": [
            "原始 user/assistant 对话不是长期事实。",
            "活动时间线只能帮助理解上下文，不能单独支撑长期事实。",
            "用户记忆与角色性格、能力变化必须经过草案和审核。",
        ],
        "generatedAtMs": generated_at_ms,
    }
    validate_contract(payload, "daily-conversation-digest.v1.json")
    return payload


def _build_user_memory_draft(
    *,
    run_id: str,
    project: str,
    role_id: str,
    digest: Mapping[str, object],
    evidence: Sequence[Mapping[str, object]],
    created_at_ms: int,
) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    for item in evidence:
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        proposal = metadata.get("userMemoryProposal")
        proposals = proposal if isinstance(proposal, list) else [proposal]
        for value in proposals:
            if not isinstance(value, Mapping):
                continue
            text = compact_whitespace(str(value.get("text") or ""))
            if not text or _contains_sensitive_content(text):
                continue
            candidates.append(
                {
                    "candidateId": f"user-memory-candidate:{_stable_digest(str(item['evidenceId']), text)[:20]}",
                    "kind": _bounded_text(value.get("kind"), 80) or "proposed_memory",
                    "text": _truncate(text, 600),
                    "confidence": _bounded_float(value.get("confidence"), default=0.5),
                    "sourceEvidenceIds": [str(item["evidenceId"])],
                    "reviewRequired": True,
                }
            )
    payload = {
        "schemaVersion": "rag-ime.user-memory-draft.v1",
        "draftId": f"user-memory-draft:{_stable_digest(run_id, 'user-memory')[:24]}",
        "status": "review_required",
        "project": project,
        "roleId": role_id,
        "sourceDigestId": str(digest["digestId"]),
        "sourceEvidenceIds": [str(item["evidenceId"]) for item in evidence],
        "candidates": candidates,
        "policy": {
            "rawDialoguePromotion": "forbidden",
            "defaultApply": False,
        },
        "createdAtMs": created_at_ms,
    }
    validate_contract(payload, "user-memory-draft.v1.json")
    return payload


def _build_role_book_draft(
    *,
    run_id: str,
    project: str,
    role_id: str,
    role_version: str,
    digest: Mapping[str, object],
    evidence: Sequence[Mapping[str, object]],
    model_proposals: Mapping[str, object],
    proposal_diagnostics: Mapping[str, object],
    created_at_ms: int,
) -> dict[str, object]:
    recent_work = [
        _role_book_recent_work_item(item)
        for item in list(digest.get("recentWork") or [])
        if isinstance(item, Mapping)
    ]
    explicit = {
        "traitProposals": _explicit_role_proposals(evidence, "roleTraitProposal"),
        "capabilityProposals": _explicit_role_proposals(
            evidence, "roleCapabilityProposal"
        ),
        "lessonProposals": _explicit_role_proposals(
            evidence, "roleLessonProposal"
        ),
        "commitmentProposals": _explicit_role_proposals(
            evidence, "roleCommitmentProposal"
        ),
    }
    proposals = {
        field: _merge_role_proposals(
            explicit[field],
            model_proposals.get(field),
            limit=_ROLE_PROPOSAL_LIMITS[field],
        )
        for field in _ROLE_PROPOSAL_FIELDS
    }
    payload = {
        "schemaVersion": "rag-ime.role-book-revision-draft.v1",
        "draftId": f"role-book-draft:{_stable_digest(run_id, role_version)[:24]}",
        "status": "draft",
        "project": project,
        "roleId": role_id,
        "baseRoleVersion": role_version,
        "sourceDigestId": str(digest["digestId"]),
        "sourceEvidenceIds": [str(item["evidenceId"]) for item in evidence],
        "patch": {
            "recentWork": recent_work,
            **proposals,
        },
        "policy": {
            "defaultApply": False,
            "safeAutoApplyFields": ["recentWork"],
            "reviewRequiredFields": [
                "traits",
                "capabilities",
                "lessonsAndLimits",
                "activeCommitments",
            ],
        },
        "proposalDiagnostics": dict(proposal_diagnostics),
        "createdAtMs": created_at_ms,
    }
    validate_contract(payload, "role-book-revision-draft.v1.json")
    return payload


def _role_book_recent_work_item(item: Mapping[str, object]) -> dict[str, object]:
    evidence_id = str(item["evidenceId"])
    source_kind = str(item["sourceKind"])
    occurred_at_ms = int(item["occurredAtMs"])
    return {
        "itemId": f"recent-work:{_stable_digest(evidence_id)[:20]}",
        "text": _truncate(compact_whitespace(str(item["text"])), 280),
        "provenance": {
            "sourceType": source_kind,
            "sourceId": evidence_id,
            "observedAtMs": occurred_at_ms,
        },
        "evidenceIds": [evidence_id],
    }


def _explicit_role_proposals(
    evidence: Sequence[Mapping[str, object]],
    metadata_key: str,
) -> list[dict[str, object]]:
    proposals: list[dict[str, object]] = []
    for item in evidence:
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        raw = metadata.get(metadata_key)
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            if isinstance(value, Mapping):
                text = compact_whitespace(str(value.get("text") or ""))
                confidence = _bounded_float(value.get("confidence"), default=0.5)
            else:
                text = compact_whitespace(str(value or ""))
                confidence = 0.5
            if not text or _contains_sensitive_content(text):
                continue
            proposals.append(
                {
                    "text": _truncate(text, 280),
                    "confidence": confidence,
                    "sourceEvidenceIds": [str(item["evidenceId"])],
                    "reviewRequired": True,
                }
            )
    return proposals


def _build_role_book_model_bundle(
    *,
    role_id: str,
    role_version: str,
    evidence: Sequence[Mapping[str, object]],
    activity_context: Mapping[str, object],
    active_role_book: Mapping[str, object] | None,
) -> dict[str, object]:
    curation_evidence: list[dict[str, object]] = []
    for item in evidence:
        source_kind = str(item.get("sourceKind") or "")
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        eligible = (
            source_kind == "session_digest"
            and compact_whitespace(str(metadata.get("trigger") or ""))
            in {"automatic", "manual", "compaction", "idle_batch"}
        ) or (
            source_kind == "tool_receipt" and metadata.get("applied") is True
        ) or (
            source_kind == "work_receipt" and metadata.get("accepted") is True
        )
        if not eligible:
            continue
        text = compact_whitespace(str(item.get("text") or ""))
        evidence_id = compact_whitespace(str(item.get("evidenceId") or ""))
        if not text or not evidence_id or _contains_sensitive_content(text):
            continue
        curation_evidence.append(
            {
                "evidenceId": evidence_id,
                "sourceKind": source_kind,
                "text": _truncate(text, 600),
                "occurredAtMs": max(0, int(item.get("occurredAtMs") or 0)),
                "reviewEvidenceOnly": True,
            }
        )
    curation_evidence = curation_evidence[-_ROLE_BOOK_MODEL_MAX_EVIDENCE_ITEMS:]

    segments: list[dict[str, object]] = []
    for item in list(activity_context.get("segments") or []):
        if not isinstance(item, Mapping):
            continue
        summary = compact_whitespace(str(item.get("summary") or ""))
        if not summary:
            continue
        segments.append(
            {
                "segmentId": _bounded_text(item.get("segmentId"), 160),
                "app": _bounded_text(item.get("app"), 120),
                "startMs": max(0, int(item.get("startMs") or 0)),
                "endMs": max(0, int(item.get("endMs") or 0)),
                "summary": _truncate(summary, 420),
            }
        )
        if len(segments) >= 6:
            break
    activity = {
        "available": activity_context.get("available") is True and bool(segments),
        "date": _bounded_text(activity_context.get("date"), 10),
        "summary": _truncate(
            compact_whitespace(str(activity_context.get("summary") or "")),
            1_200,
        ),
        "segments": segments,
        # No timeline source/event ids cross this model boundary.
        "corroborationOnly": True,
        "maySupportFacts": False,
        "maySupportRoleProposals": False,
    }

    active_sections: dict[str, list[str]] = {}
    sections = (
        active_role_book.get("sections")
        if isinstance(active_role_book, Mapping)
        else None
    )
    sections = sections if isinstance(sections, Mapping) else {}
    for section in _ROLE_PROPOSAL_SECTIONS.values():
        texts: list[str] = []
        for item in list(sections.get(section) or [])[-4:]:
            if not isinstance(item, Mapping):
                continue
            text = compact_whitespace(str(item.get("text") or ""))
            if text:
                texts.append(_truncate(text, 280))
        active_sections[section] = texts

    payload: dict[str, object] = {
        "schemaVersion": "rag-ime.role-book-curation-input.v2",
        "roleId": role_id,
        "roleVersion": role_version,
        "curationEvidence": curation_evidence,
        "activityContext": activity,
        "activeRoleBook": {
            "sections": active_sections,
            "corroborationOnly": True,
            "maySupportNewProposals": False,
        },
        "policy": {
            "reviewOnly": True,
            "allowedEvidenceIds": [
                str(item["evidenceId"]) for item in curation_evidence
            ],
            "rawConversationMaySupplyEvidence": False,
            "timelineMaySupplyEvidence": False,
            "autoActivation": False,
        },
    }
    while (
        _serialized_chars(payload) > _ROLE_BOOK_MODEL_MAX_CHARS
        and len(curation_evidence) > 1
    ):
        curation_evidence.pop(0)
        payload["policy"]["allowedEvidenceIds"] = [  # type: ignore[index]
            str(item["evidenceId"]) for item in curation_evidence
        ]
    if _serialized_chars(payload) > _ROLE_BOOK_MODEL_MAX_CHARS:
        raise ValueError("Role Book organizer input exceeded its strict character budget")
    return payload


def _normalize_model_role_proposals(
    value: object,
    *,
    allowed_evidence_ids: set[str],
    active_role_book: Mapping[str, object] | None,
) -> tuple[dict[str, list[dict[str, object]]], int]:
    source = value if isinstance(value, Mapping) else {}
    active_sections = (
        active_role_book.get("sections")
        if isinstance(active_role_book, Mapping)
        else None
    )
    active_sections = active_sections if isinstance(active_sections, Mapping) else {}
    normalized: dict[str, list[dict[str, object]]] = {}
    rejected = 0
    accepted_chars = 0
    for field in _ROLE_PROPOSAL_FIELDS:
        section = _ROLE_PROPOSAL_SECTIONS[field]
        seen = {
            _role_proposal_text_key(item.get("text"))
            for item in list(active_sections.get(section) or [])
            if isinstance(item, Mapping)
        }
        output: list[dict[str, object]] = []
        raw_values = source.get(field)
        values = (
            raw_values
            if isinstance(raw_values, Sequence)
            and not isinstance(raw_values, (str, bytes, bytearray))
            else []
        )
        for raw in values:
            if len(output) >= _ROLE_PROPOSAL_LIMITS[field]:
                rejected += 1
                continue
            if not isinstance(raw, Mapping):
                rejected += 1
                continue
            text_value = raw.get("text")
            if not isinstance(text_value, str):
                rejected += 1
                continue
            text = compact_whitespace(text_value)
            evidence_values = raw.get("sourceEvidenceIds")
            if (
                not text
                or len(text) > 280
                or not isinstance(evidence_values, Sequence)
                or isinstance(evidence_values, (str, bytes, bytearray))
                or not 1 <= len(evidence_values) <= 8
                or any(not isinstance(item, str) for item in evidence_values)
            ):
                rejected += 1
                continue
            evidence_ids = [
                compact_whitespace(str(item or "")) for item in evidence_values
            ]
            if (
                any(not item for item in evidence_ids)
                or len(set(evidence_ids)) != len(evidence_ids)
                or not set(evidence_ids).issubset(allowed_evidence_ids)
            ):
                rejected += 1
                continue
            confidence_raw = raw.get("confidence")
            if isinstance(confidence_raw, bool):
                rejected += 1
                continue
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                rejected += 1
                continue
            if not 0.0 <= confidence <= 1.0:
                rejected += 1
                continue
            try:
                normalize_role_book_review_item(
                    {
                        "text": text,
                        "provenance": {
                            "sourceType": "daily_role_model",
                            "sourceId": "role-curation:model",
                            "observedAtMs": 0,
                        },
                        "evidenceIds": evidence_ids,
                    },
                    section=section,
                )
            except (TypeError, ValueError):
                rejected += 1
                continue
            text_key = _role_proposal_text_key(text)
            if (
                not text_key
                or text_key in seen
                or accepted_chars + len(text) > _ROLE_BOOK_PROPOSAL_MAX_CHARS
            ):
                rejected += 1
                continue
            seen.add(text_key)
            accepted_chars += len(text)
            output.append(
                {
                    "text": text,
                    "confidence": confidence,
                    "sourceEvidenceIds": evidence_ids,
                    "reviewRequired": True,
                }
            )
        normalized[field] = output
    contract_payload = {
        "schemaVersion": "rag-ime.role-book-curation.v1",
        **normalized,
        "warnings": [],
    }
    validate_contract(contract_payload, "role-book-curation.v1.json")
    return normalized, rejected


def _merge_role_proposals(
    first: object,
    second: object,
    *,
    limit: int,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for values in (first, second):
        if not isinstance(values, Sequence) or isinstance(
            values, (str, bytes, bytearray)
        ):
            continue
        for item in values:
            if not isinstance(item, Mapping):
                continue
            key = _role_proposal_text_key(item.get("text"))
            if not key or key in seen:
                continue
            result.append(dict(item))
            seen.add(key)
            if len(result) >= limit:
                return result
    return result


def _role_book_review_item(
    proposal: Mapping[str, object],
    *,
    section: str,
    draft_id: str,
    observed_at_ms: int,
) -> dict[str, object]:
    text = compact_whitespace(str(proposal.get("text") or ""))
    evidence_ids = [
        compact_whitespace(str(value or ""))
        for value in list(proposal.get("sourceEvidenceIds") or [])
    ]
    item = {
        "itemId": (
            f"role-review:{_stable_digest(draft_id, section, text, *evidence_ids)[:24]}"
        ),
        "text": text,
        "provenance": {
            "sourceType": "daily_role_review",
            "sourceId": draft_id,
            "observedAtMs": max(0, int(observed_at_ms)),
        },
        "evidenceIds": evidence_ids,
    }
    return normalize_role_book_review_item(item, section=section)


def _merge_role_book_review_items(
    existing: object,
    incoming: Sequence[Mapping[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    values = [
        dict(item)
        for item in list(existing or [])
        if isinstance(item, Mapping)
    ]
    seen = {_role_proposal_text_key(item.get("text")) for item in values}
    for item in incoming:
        key = _role_proposal_text_key(item.get("text"))
        if key and key not in seen:
            values.append(dict(item))
            seen.add(key)
    return values[-max(1, int(limit)) :]


def _role_proposal_text_key(value: object) -> str:
    return compact_whitespace(str(value or "")).casefold()


def _is_verified_recent_work(item: Mapping[str, object]) -> bool:
    kind = str(item.get("sourceKind") or "")
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    if kind == "tool_receipt":
        return metadata.get("applied") is True
    if kind == "work_receipt":
        return metadata.get("accepted") is True
    if kind == "room_event":
        return metadata.get("accepted") is True
    return False


def _digest_item(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "evidenceId": str(item["evidenceId"]),
        "sourceKind": str(item["sourceKind"]),
        "text": _truncate(compact_whitespace(str(item["text"])), 420),
        "occurredAtMs": int(item["occurredAtMs"]),
    }


def _evidence_payload(row: sqlite3.Row) -> dict[str, object]:
    payload = {
        "schemaVersion": "rag-ime.agent-memory-evidence.v1",
        "evidenceId": str(row["evidence_id"]),
        "project": str(row["project"]),
        "roleId": str(row["role_id"]),
        "sessionId": str(row["session_id"]),
        "ownerKind": str(row["owner_kind"]),
        "ownerId": str(row["owner_id"]),
        "knowledgeDomain": str(row["knowledge_domain"]),
        "scopeKind": str(row["scope_kind"]),
        "scopeId": str(row["scope_id"]),
        "visibility": str(row["visibility"]),
        "authorizationRevision": str(row["authorization_revision"]),
        "bindingId": str(row["binding_id"]),
        "scopeMode": str(row["scope_mode"]),
        "sourceKind": str(row["source_kind"]),
        "sourceId": str(row["source_id"]),
        "idempotencyKey": str(row["idempotency_key"]),
        "text": str(row["content_text"]),
        "textSha256": str(row["content_sha256"]),
        "classification": "raw_evidence",
        "maySupportLongTermFact": False,
        "provenance": _json_object(row["provenance_json"]),
        "metadata": _json_object(row["metadata_json"]),
        "privacyClass": str(row["privacy_class"]),
        "status": str(row["status"]),
        "occurredAtMs": int(row["occurred_at_ms"]),
        "recordedAtMs": int(row["recorded_at_ms"]),
    }
    validate_contract(payload, "agent-memory-evidence.v1.json")
    return payload


def _atom_bootstrap_item(row: sqlite3.Row) -> dict[str, object] | None:
    if str(row["privacy_level"] or "").strip().lower() == "sensitive":
        return None
    source_id = compact_whitespace(str(row["id"] or ""))
    text = compact_whitespace(str(row["canonical_text"] or row["text"] or ""))
    if (
        not source_id
        or _contains_sensitive_content(source_id)
        or not text
        or _contains_sensitive_content(text)
    ):
        return None
    return {
        "sourceType": "memory_atom",
        "sourceId": source_id,
        "text": _truncate(text, 560),
        "kind": str(row["kind"]),
        "occurredAtMs": int(row["updated_at_ms"]),
        "maySupportFacts": True,
        "provenance": _sanitize_mapping(
            {
                "scopeProject": str(row["scope_project"] or ""),
                "sourceEventIds": _json_list(row["source_event_ids_json"])[:12],
                "sourceMemoryIds": _json_list(row["source_memory_ids_json"])[:12],
                "confidence": float(row["confidence"]),
                "qualityScore": float(row["quality_score"]),
            }
        ),
    }


def _with_stable_source_ref(value: Mapping[str, object]) -> dict[str, object]:
    item = dict(value)
    source_type = compact_whitespace(str(item.get("sourceType") or ""))
    source_id = compact_whitespace(str(item.get("sourceId") or ""))
    provenance = item.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    ref_type = {
        "memory_atom": "atom",
        "memory_book": "book",
    }.get(source_type, "")
    ref_id = source_id
    ref: dict[str, object] | None = (
        {
            "type": ref_type,
            "kind": ref_type,
            "id": ref_id,
        }
        if ref_type and ref_id
        else None
    )
    if str(item.get("kind") or "") == "daily_timeline":
        timeline_id = compact_whitespace(str(provenance.get("timelineId") or ""))
        if timeline_id:
            ref = {
                "type": "timeline",
                "kind": "timeline",
                "id": timeline_id,
                "bookId": source_id,
            }
    # sourceType/sourceId already identify the bootstrap source. Keep only the
    # actionable canonical reference instead of duplicating both objects inside
    # a tightly budgeted Session-start payload.
    if ref is not None:
        item["ref"] = ref
    return item


def _cursor_row(
    conn: sqlite3.Connection,
    project: str,
    role_id: str,
) -> sqlite3.Row | Mapping[str, object]:
    row = conn.execute(
        """
        SELECT * FROM personal_context_consolidation_cursors
        WHERE project = ? AND role_id = ?
        """,
        (project, role_id),
    ).fetchone()
    if row is not None:
        return row
    return {
        "project": project,
        "role_id": role_id,
        "last_evidence_at_ms": 0,
        "last_evidence_id": "",
        "last_successful_run_id": "",
        "last_succeeded_at_ms": 0,
        "updated_at_ms": 0,
    }


def _cursor_payload(row: Mapping[str, object] | sqlite3.Row) -> dict[str, object]:
    return {
        "project": str(row["project"]),
        "roleId": str(row["role_id"]),
        "lastEvidenceAtMs": int(row["last_evidence_at_ms"]),
        "lastEvidenceId": str(row["last_evidence_id"]),
        "lastSuccessfulRunId": str(row["last_successful_run_id"]),
        "lastSucceededAtMs": int(row["last_succeeded_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
    }


def _next_evidence_row(
    conn: sqlite3.Connection,
    project: str,
    role_id: str,
    cursor: Mapping[str, object] | sqlite3.Row,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM agent_memory_evidence
        WHERE project = ? AND role_id = ? AND status = 'active'
          AND (
            occurred_at_ms > ?
            OR (occurred_at_ms = ? AND evidence_id > ?)
          )
        ORDER BY occurred_at_ms ASC, evidence_id ASC
        LIMIT 1
        """,
        (
            project,
            role_id,
            cursor["last_evidence_at_ms"],
            cursor["last_evidence_at_ms"],
            cursor["last_evidence_id"],
        ),
    ).fetchone()


def _recoverable_run(
    conn: sqlite3.Connection,
    project: str,
    role_id: str,
    cursor: Mapping[str, object] | sqlite3.Row,
    now_ms: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM personal_context_consolidation_runs
        WHERE project = ? AND role_id = ?
          AND start_cursor_at_ms = ?
          AND start_cursor_evidence_id = ?
          AND (
            status = 'failed'
            OR (status = 'running' AND updated_at_ms <= ?)
          )
        ORDER BY
          CASE status WHEN 'failed' THEN 0 ELSE 1 END,
          created_at_ms DESC,
          run_id DESC
        LIMIT 1
        """,
        (
            project,
            role_id,
            cursor["last_evidence_at_ms"],
            cursor["last_evidence_id"],
            max(0, now_ms - _RUN_STALE_AFTER_MS),
        ),
    ).fetchone()


def _evidence_rows_by_ids(
    conn: sqlite3.Connection,
    evidence_ids: Sequence[str],
) -> list[dict[str, object]]:
    if not evidence_ids:
        return []
    placeholders = ",".join("?" for _ in evidence_ids)
    rows = conn.execute(
        f"""
        SELECT * FROM agent_memory_evidence
        WHERE evidence_id IN ({placeholders}) AND status = 'active'
        """,
        tuple(evidence_ids),
    ).fetchall()
    by_id = {str(row["evidence_id"]): _evidence_payload(row) for row in rows}
    return [by_id[evidence_id] for evidence_id in evidence_ids if evidence_id in by_id]


def _claim_payload(
    run: sqlite3.Row,
    cursor: Mapping[str, object] | sqlite3.Row,
    evidence: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "status": "claimed",
        "runId": str(run["run_id"]),
        "cursor": _cursor_payload(cursor),
        "evidence": [dict(item) for item in evidence],
        "windowStartMs": int(run["window_start_ms"]),
        "windowEndMs": int(run["window_end_ms"]),
        "endCursorAtMs": int(run["end_cursor_at_ms"]),
        "endCursorEvidenceId": str(run["end_cursor_evidence_id"]),
    }


def _sanitize_mapping(value: Mapping[str, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        key = _bounded_text(raw_key, 160)
        if not key or is_sensitive_mapping_key(key):
            continue
        child = _sanitize_value(raw_value, depth=0)
        if child is not None:
            sanitized[key] = child
    return sanitized


def _sanitize_value(value: object, *, depth: int) -> object:
    if depth >= 4:
        return redact_sensitive_text(_bounded_text(value, 500))
    if isinstance(value, Mapping):
        return {
            key: child
            for raw_key, raw_value in value.items()
            if (key := _bounded_text(raw_key, 160))
            and not is_sensitive_mapping_key(key)
            and (child := _sanitize_value(raw_value, depth=depth + 1)) is not None
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            child
            for item in list(value)[:100]
            if (child := _sanitize_value(item, depth=depth + 1)) is not None
        ]
    if isinstance(value, str):
        text = _truncate(value, 2_000)
        return redact_sensitive_text(text)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_sensitive_text(_truncate(str(value), 2_000))


def _json_object_text(value: Mapping[str, object], label: str) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{label} exceeds {_MAX_JSON_BYTES} bytes")
    return encoded


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_list(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _select_provenance(value: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "piEntryId",
        "turnId",
        "roomId",
        "workItemId",
        "receiptId",
        "eventId",
        "digestId",
    )
    return {key: value[key] for key in keys if key in value and value[key]}


def _contains_sensitive_content(text: str) -> bool:
    return contains_sensitive_content(compact_whitespace(text))


def _serialized_chars(payload: Mapping[str, object]) -> int:
    return len(
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _update_payload_size(payload: dict[str, object]) -> None:
    budget = payload.get("budget")
    if not isinstance(budget, dict):
        return
    budget["usedChars"] = 0
    for _ in range(3):
        budget["usedChars"] = _serialized_chars(payload)


def _stable_digest(*parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _required_text(value: object, field: str, limit: int) -> str:
    normalized = _bounded_text(value, limit)
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _bounded_text(value: object, limit: int) -> str:
    return compact_whitespace(str(value or ""))[: max(0, int(limit))]


def _truncate(value: str, limit: int) -> str:
    text = compact_whitespace(value)
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def _bounded_float(value: object, *, default: float) -> float:
    try:
        resolved = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        resolved = default
    return max(0.0, min(resolved, 1.0))


def _now_ms() -> int:
    return int(time.time() * 1_000)
