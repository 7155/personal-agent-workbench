from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import apply_database_migrations
from .deepseek_memory_organizer import MEMORY_BOOK_COMPILE_SCHEMA_VERSION
from .input_event_assembly import (
    cumulative_context_snapshots_are_revisions,
    reconstruct_input_fragment_run,
)
from .input_quality import (
    FINALIZED_INPUT_SOURCE,
    RIME_FRAGMENT_SOURCE,
    assess_input_text,
    source_context_enabled,
)
from .memory_ingest import normalize_text, upsert_memory_item
from .memory_card_mutations import assert_background_card_unchanged, card_revision
from .memory_curation import (
    MEMORY_CURATION_ARCHITECTURE,
    memory_atom_authority_hash,
    memory_atom_identity_hash,
    memory_catalog_digest,
)
from .memory_evidence_admission import (
    admitted_personal_evidence_sql,
    rollback_evidence_admissions_for_run,
)
from .memory_projection import RETRIEVAL_DOCS_PROJECTION, enqueue_memory_projection
from .memory_projection_consistency import (
    bump_source_revision,
    delete_retrieval_docs,
    invalidate_superseded_atom_dependencies,
    restore_dependency_invalidation,
    sync_projection_dependencies,
)
from .memory_purpose import purpose_audit_fields
from .personal_memory_books import project_personal_memory_books
from .text_utils import compact_whitespace, now_ms, stable_text_hash, token_terms, truncate_text


MEMORY_BOOK_RUN_SCHEMA_VERSION = "rag-ime.memory-book-run.v1"
MEMORY_BOOK_PREVIEW_SCHEMA_VERSION = "rag-ime.memory-book-preview.v1"
MEMORY_BOOK_VALIDATE_SCHEMA_VERSION = "rag-ime.memory-book-validate.v1"
MEMORY_CATALOG_RUN_KIND = "catalog_consolidation"
MEMORY_BOOK_APPLY_SCHEMA_VERSION = "rag-ime.memory-book-apply.v1"
MEMORY_BOOK_ROLLBACK_SCHEMA_VERSION = "rag-ime.memory-book-rollback.v1"

# Incremental curation intentionally keeps the historical DSV4/user graph
# boundary. A complete catalog snapshot must also include curated imports, and
# these are the only sources admitted as governed retrieval tags.
_DEFAULT_TAG_SOURCES = ("dsv4", "user")
_GOVERNED_TAG_SOURCES = ("curated_import", "dsv4", "user")
_MEMORY_BOOK_SCOPE_FIELDS = (
    "owner_kind",
    "owner_id",
    "project",
    "app",
    "knowledge_domain",
    "scope_kind",
    "scope_id",
    "visibility",
    "authorization_revision",
    "binding_id",
    "scope_mode",
)


def _tag_source_predicate(alias: str, *, catalog_only: bool) -> str:
    """Return the fixed SQL source boundary for a tag catalog query."""
    sources = _GOVERNED_TAG_SOURCES if catalog_only else _DEFAULT_TAG_SOURCES
    return f"{alias}.source IN ({', '.join(repr(source) for source in sources)})"


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_URL_SECRET_QUERY_RE = re.compile(
    r"(?P<separator>[?&])"
    r"(?:access[_-]?token|refresh[_-]?token|id[_-]?token|token|api[_-]?key|apikey|secret|password|passwd|credential|auth)"
    r"=[^&#\s]*",
    re.IGNORECASE,
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:password|passwd|token|api[_ -]?key|secret|credential|authorization)\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;，；]+)",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(?<!REDACTED_)(?:"
    r"bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"sk-[A-Za-z0-9_-]{8,}"
    r")",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDENTITY_RE = re.compile(r"(?<!\d)[1-9]\d{16}[0-9Xx](?![0-9Xx])")
_PAYMENT_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){11,18}\d(?!\d)")
_IPV4_CANDIDATE_RE = re.compile(r"(?<![0-9A-Za-z_.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9A-Za-z_.])")
_IPV6_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Fa-f:])(?=[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:)(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])"
)
_RIME_PINYIN_RE = re.compile(r"^[a-zv]+(?: [a-zv]+)*$")
_PATH_RE = re.compile(
    r"(?:(?:/(?:Users|Volumes|home)/|~/)[^\s\"']+|(?:[A-Za-z]:\\(?:Users\\)?|\\\\[^\\\s]+\\[^\\\s]+\\)[^\s\"']+)",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_RIME_FRAGMENT_SOURCE = RIME_FRAGMENT_SOURCE
_MODEL_GENERATED_EVENT_SOURCES = {
    "api_core_optimizer",
    "api_lexicon_optimizer",
    "squirrel_rime_sidecar",
    "vcp_memory_generator",
}
_NON_MEMORY_EVENT_SOURCES = {
    "ime_demo_fixture",
}
_RUNTIME_PROBE_MARKERS = (
    "ceshiwendang",
    "press a visible candidate",
    "wait for llm/model",
    "wait for the next prediction",
)
_RUNTIME_PROBE_SIGNATURES = (
    ("渐进工具披露验收", "tool_search", "tool_load"),
    ("上一轮得到的插件数量是多少", "只回复数字", "不要调用工具"),
)


def build_memory_book_source_bundle(
    conn: sqlite3.Connection,
    *,
    project: str,
    since_days: int = 7,
    limit: int = 80,
    after_event_id: int | None = None,
    newest_first: bool = False,
    curation_scope: str = "incremental",
    catalog_only: bool = False,
) -> dict[str, object]:
    global_catalog = (
        compact_whitespace(curation_scope).lower() == "global"
        and bool(catalog_only)
    )
    state = memory_compile_state(conn, project=project)
    applied_cursor = int(state["lastCompiledEventId"])
    cursor = (
        applied_cursor
        if catalog_only or after_event_id is None
        else max(0, int(after_event_id))
    )
    cutoff_ms = now_ms() - max(1, int(since_days)) * 24 * 60 * 60 * 1000
    event_order = "DESC" if newest_first else "ASC"
    rows = (
        []
        if catalog_only
        else conn.execute(
            f"""
            SELECT e.id, e.created_at_ms, e.source, e.committed_text, e.recent_context,
                   e.app, e.project, e.tags_json, e.context_group_id, e.context_group_level,
                   e.capture_metadata_json
            FROM input_events e
            LEFT JOIN memory_state s ON s.event_id = e.id
            WHERE e.id > ?
              AND e.created_at_ms >= ?
              AND (? = '' OR e.project = ? OR e.project = '')
              AND COALESCE(s.deleted, 0) = 0
            ORDER BY e.id {event_order}
            LIMIT ?
            """,
            (cursor, cutoff_ms, project, project, max(1, int(limit))),
        ).fetchall()
    )
    if newest_first:
        rows = list(reversed(rows))
    raw_events: list[dict[str, object]] = []
    redaction_stats = _empty_redaction_counts()
    for row in rows:
        text, text_counts = _sanitize_text(str(row["committed_text"] or ""), max_chars=220)
        recent, recent_counts = _sanitize_text(str(row["recent_context"] or ""), max_chars=220)
        app, app_counts = _sanitize_text(str(row["app"] or ""), max_chars=120)
        event_project, project_counts = _sanitize_text(str(row["project"] or ""), max_chars=120)
        tags: list[str] = []
        tag_counts = _empty_redaction_counts()
        for tag in _json_list(row["tags_json"]):
            sanitized_tag, counts = _sanitize_text(tag, max_chars=48)
            _merge_counts(tag_counts, counts)
            if sanitized_tag:
                tags.append(sanitized_tag)
        _merge_counts(redaction_stats, text_counts)
        _merge_counts(redaction_stats, recent_counts)
        _merge_counts(redaction_stats, app_counts)
        _merge_counts(redaction_stats, project_counts)
        _merge_counts(redaction_stats, tag_counts)
        raw_events.append(
            {
                "eventId": int(row["id"]),
                "sourceEventIds": [int(row["id"])],
                "createdAtMs": int(row["created_at_ms"] or 0),
                "source": str(row["source"] or ""),
                "text": text,
                "recentContext": recent,
                "app": app,
                "project": event_project,
                # Source tags are transport metadata, not semantic labels. The
                # organizer may use them as weak hints but must never copy them
                # into the long-term tag graph without semantic evidence.
                "sourceMetadataTags": tags,
                "contextGroupId": str(row["context_group_id"] or ""),
                "contextGroupLevel": str(row["context_group_level"] or "app"),
                # Internal-only: quality gating must validate the same strong
                # native boundary that created canonical Evidence. This field
                # is removed before the bundle is exposed to an organizer.
                "_captureMetadata": _json_object(row["capture_metadata_json"]),
            }
        )
    events, reconstruction = _reconstruct_memory_source_events(raw_events)
    raw_event_ids = [int(item["eventId"]) for item in raw_events]
    feedback, feedback_redactions = _source_feedback(
        conn,
        event_ids=raw_event_ids,
        project=project,
    )
    rime_rank_feedback, rime_feedback_redactions = _source_rime_rank_feedback(
        conn,
        project=project,
        cutoff_ms=cutoff_ms,
    )
    _merge_counts(redaction_stats, feedback_redactions)
    _merge_counts(redaction_stats, rime_feedback_redactions)
    existing_books = _existing_book_summaries(
        conn,
        project=project,
        catalog_only=global_catalog,
    )
    existing_atoms = _existing_memory_atoms(
        conn,
        project=project,
        catalog_only=global_catalog,
    )
    existing_groups = _existing_semantic_groups(
        conn,
        project=project,
        catalog_only=global_catalog,
    )
    existing_tags = _existing_semantic_tags(conn, catalog_only=global_catalog)
    existing_tag_edges = _existing_semantic_tag_edges(
        conn,
        catalog_only=global_catalog,
    )
    catalog_truncated = _catalog_truncation_flags(
        existing_books=existing_books,
        existing_atoms=existing_atoms,
        existing_groups=existing_groups,
        existing_tags=existing_tags,
        existing_tag_edges=existing_tag_edges,
        catalog_only=global_catalog,
    )
    catalog_complete = not any(catalog_truncated.values())
    catalog_digest = (
        memory_catalog_digest(
            {
                "existingMemoryBooks": existing_books,
                "existingMemoryAtoms": existing_atoms,
                "existingSemanticGroups": existing_groups,
                "existingSemanticTags": existing_tags,
                "existingTagEdges": existing_tag_edges,
            }
        )
        if global_catalog and catalog_complete
        else ""
    )
    legal_groups = sorted({str(item.get("contextGroupId") or "") for item in events if item.get("contextGroupId")})
    max_event_id = max(raw_event_ids, default=cursor)
    pending_count = int(
        conn.execute(
            """SELECT COUNT(*) FROM input_events e
               LEFT JOIN memory_state s ON s.event_id = e.id
               WHERE e.id > ? AND (? = '' OR e.project = ? OR e.project = '')
                 AND COALESCE(s.deleted, 0) = 0""",
            (cursor, project, project),
        ).fetchone()[0]
    )
    payload = {
        "schemaVersion": "rag-ime.memory-book-source-bundle.v1",
        "project": project,
        "curationScope": (
            "global" if compact_whitespace(curation_scope).lower() == "global" else "incremental"
        ),
        "catalogAudit": global_catalog,
        "catalogDigest": catalog_digest,
        "catalogComplete": catalog_complete,
        "catalogTruncated": catalog_truncated,
        "evidenceOrder": (
            "catalog_only"
            if catalog_only
            else "latest_retained"
            if newest_first
            else "cursor_forward"
        ),
        "sinceDays": max(1, int(since_days)),
        "exportedAtMs": now_ms(),
        "redactionStats": redaction_stats,
        "recentEvents": events,
        "rawEventCount": len(raw_events),
        "reconstruction": reconstruction,
        "feedback": feedback,
        "rimeRankFeedback": rime_rank_feedback,
        "existingMemoryBooks": existing_books,
        "existingMemoryAtoms": existing_atoms,
        "existingSemanticGroups": existing_groups,
        "existingSemanticTags": existing_tags,
        "existingTagEdges": existing_tag_edges,
        "legalContextGroupIds": legal_groups,
        "cursor": {
            "fromEventId": cursor,
            "toEventId": max_event_id,
            "pendingEventCount": pending_count,
        },
    }
    hash_payload = dict(payload)
    hash_payload.pop("exportedAtMs", None)
    payload["bundleHash"] = stable_text_hash(
        json.dumps(hash_payload, ensure_ascii=False, sort_keys=True)
    )
    return payload


def _reconstruct_memory_source_events(
    raw_events: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Turn Rime's per-commit ledger into coherent model-facing utterances.

    The source rows remain immutable. This projection only removes generated or
    demo rows and collapses cumulative Rime context snapshots before DSV4 sees
    them, so the model receives sentences instead of two-character commits.
    """

    result: list[dict[str, object]] = []
    fragment_run: list[dict[str, object]] = []
    excluded_counts: dict[str, int] = {}

    def flush_fragments() -> None:
        if not fragment_run:
            return
        result.append(collapse_rime_fragment_run(fragment_run))
        fragment_run.clear()

    for event in raw_events:
        source = compact_whitespace(str(event.get("source") or ""))
        source_tags = _strings(event.get("sourceMetadataTags"))
        if source == "codex_history" and "role:user" not in source_tags:
            excluded_counts["codex_history:non_user"] = (
                excluded_counts.get("codex_history:non_user", 0) + 1
            )
            continue
        if not source_context_enabled(source, tags=source_tags):
            excluded_counts[f"{source}:source_not_enabled"] = (
                excluded_counts.get(f"{source}:source_not_enabled", 0) + 1
            )
            continue
        if source in _MODEL_GENERATED_EVENT_SOURCES or source in _NON_MEMORY_EVENT_SOURCES:
            excluded_counts[source] = excluded_counts.get(source, 0) + 1
            continue
        if source == _RIME_FRAGMENT_SOURCE:
            if fragment_run and not rime_fragments_belong_together(fragment_run[-1], event):
                flush_fragments()
            fragment_run.append(event)
            continue
        flush_fragments()
        result.append(dict(event))
    flush_fragments()

    filtered, filter_stats = _filter_reconstructed_memory_events(result)
    return filtered, {
        "schemaVersion": "rag-ime.memory-source-reconstruction.v1",
        "rawEventCount": len(raw_events),
        "modelEventCount": len(filtered),
        "collapsedEventCount": max(0, len(raw_events) - sum(excluded_counts.values()) - len(result)),
        "excludedGeneratedEventCount": sum(
            count for source, count in excluded_counts.items() if source in _MODEL_GENERATED_EVENT_SOURCES
        ),
        "excludedNonMemoryEventCount": sum(
            count for source, count in excluded_counts.items() if source in _NON_MEMORY_EVENT_SOURCES
        ),
        "excludedSources": excluded_counts,
        **filter_stats,
    }


def rime_fragments_belong_together(
    previous: dict[str, object],
    current: dict[str, object],
) -> bool:
    if compact_whitespace(str(previous.get("app") or "")) != compact_whitespace(str(current.get("app") or "")):
        return False
    if compact_whitespace(str(previous.get("contextGroupId") or "")) != compact_whitespace(
        str(current.get("contextGroupId") or "")
    ):
        return False
    previous_ms = _optional_int(previous.get("createdAtMs"))
    current_ms = _optional_int(current.get("createdAtMs"))
    gap_ms = max(0, current_ms - previous_ms)
    if gap_ms > 5 * 60 * 1000:
        return False
    previous_context = compact_whitespace(str(previous.get("recentContext") or ""))
    current_context = compact_whitespace(str(current.get("recentContext") or ""))
    if not previous_context or not current_context:
        return gap_ms <= 8_000
    if previous_context in current_context or current_context in previous_context:
        return True
    if cumulative_context_snapshots_are_revisions(
        previous_context,
        current_context,
        gap_ms=gap_ms,
    ):
        return True
    overlap_limit = min(len(previous_context), len(current_context), 80)
    for size in range(overlap_limit, 7, -1):
        if previous_context[-size:] == current_context[:size]:
            return True
    return False


def collapse_rime_fragment_run(events: list[dict[str, object]]) -> dict[str, object]:
    source_event_ids = [
        event_id
        for event in events
        for event_id in _positive_ints(event.get("sourceEventIds") or [event.get("eventId")])
    ]
    source_event_ids = list(dict.fromkeys(source_event_ids))
    source_ids = _unique_strings(
        [
            source_id
            for event in events
            for source_id in _strings(event.get("sourceIds"))
        ],
        limit=50_000,
    )
    evidence_ids = _unique_strings(
        [
            evidence_id
            for event in events
            for evidence_id in _strings(
                event.get("evidenceIds") or [event.get("evidenceId")]
            )
        ],
        limit=50_000,
    )
    reconstructed = reconstruct_input_fragment_run(
        [str(item.get("text") or "") for item in events],
        [str(item.get("recentContext") or "") for item in events],
    )
    last = dict(events[-1])
    surrounding_context = _surrounding_input_context(
        str(last.get("recentContext") or ""),
        reconstructed,
    )
    tags = _unique_strings(
        [tag for item in events for tag in _strings(item.get("sourceMetadataTags"))],
        limit=24,
    )
    last.update(
        {
            "eventId": source_event_ids[-1] if source_event_ids else _optional_int(last.get("eventId")),
            "sourceEventIds": source_event_ids,
            "sourceIds": source_ids,
            "evidenceId": evidence_ids[0] if len(evidence_ids) == 1 else "",
            "evidenceIds": evidence_ids,
            "source": "reconstructed_user_input",
            "text": reconstructed,
            # Preserve only the bounded AX/application text around the final
            # expression.  The expression itself is already canonical
            # Evidence and must not be duplicated as context.
            "recentContext": surrounding_context,
            "sourceMetadataTags": tags,
            "reconstruction": {
                "method": "rime-cumulative-context",
                "rawEventCount": len(events),
            },
        }
    )
    return last


def _surrounding_input_context(
    raw_context: str,
    reconstructed: str,
    *,
    limit: int = 800,
) -> str:
    context = compact_whitespace(raw_context)
    expression = compact_whitespace(reconstructed)
    if not context or not expression or context == expression:
        return ""
    index = context.rfind(expression)
    if index < 0:
        return context[-max(1, int(limit)) :]
    before = context[:index].strip()
    after = context[index + len(expression) :].strip()
    surrounding = compact_whitespace(
        " ".join(
            value
            for value in (before[-600:], after[:200])
            if value
        )
    )
    return surrounding[-max(1, int(limit)) :]


def _filter_reconstructed_memory_events(
    events: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    filtered: list[dict[str, object]] = []
    dropped_doctor = 0
    dropped_probe = 0
    dropped_fragment = 0
    quality_reason_counts: dict[str, int] = {}
    for event in events:
        text = compact_whitespace(str(event.get("text") or ""))
        group_id = compact_whitespace(str(event.get("contextGroupId") or "")).lower()
        if "doctor-" in group_id or group_id.startswith("doctor:"):
            dropped_doctor += 1
            continue
        lowered = text.lower()
        if sum(marker in lowered for marker in _RUNTIME_PROBE_MARKERS) >= 2 or any(
            all(marker in lowered for marker in signature)
            for signature in _RUNTIME_PROBE_SIGNATURES
        ):
            dropped_probe += 1
            continue
        source = compact_whitespace(str(event.get("source") or ""))
        source_ids = _positive_ints(event.get("sourceEventIds") or [event.get("eventId")])
        reconstruction = event.get("reconstruction") if isinstance(event.get("reconstruction"), dict) else {}
        reconstructed = bool(reconstruction)
        quality = assess_input_text(
            text,
            source=_RIME_FRAGMENT_SOURCE if reconstructed else source,
            source_count=len(source_ids),
            finalized=(
                False
                if reconstructed
                else source != FINALIZED_INPUT_SOURCE
                or "finalized" in _strings(event.get("sourceMetadataTags"))
                or "complete-input" in _strings(event.get("sourceMetadataTags"))
            ),
            reconstructed=reconstructed,
            tags=_strings(event.get("sourceMetadataTags")),
            capture_metadata=(
                event.get("_captureMetadata")
                if isinstance(event.get("_captureMetadata"), Mapping)
                else None
            ),
            app=compact_whitespace(str(event.get("app") or "")),
        )
        if not quality.memory_eligible or not _evidence_tokens(text):
            dropped_fragment += 1
            for reason in quality.reasons or ("no_evidence_tokens",):
                quality_reason_counts[reason] = quality_reason_counts.get(reason, 0) + 1
            continue
        public_event = dict(event)
        public_event.pop("_captureMetadata", None)
        filtered.append({**public_event, **quality.payload()})

    merged: list[dict[str, object]] = []
    duplicate_count = 0
    for event in filtered:
        normalized = normalize_text(str(event.get("text") or ""))
        app = compact_whitespace(str(event.get("app") or ""))
        group_id = compact_whitespace(str(event.get("contextGroupId") or ""))
        match_index = next(
            (
                index
                for index, previous in enumerate(merged)
                if app == compact_whitespace(str(previous.get("app") or ""))
                and group_id == compact_whitespace(str(previous.get("contextGroupId") or ""))
                and normalize_text(str(previous.get("text") or "")) == normalized
            ),
            None,
        )
        if match_index is None:
            merged.append(event)
            continue
        previous = dict(merged[match_index])
        ids = _positive_ints(previous.get("sourceEventIds"))
        ids.extend(item for item in _positive_ints(event.get("sourceEventIds")) if item not in ids)
        previous["sourceEventIds"] = ids
        previous["eventId"] = ids[-1] if ids else previous.get("eventId")
        reconstruction = dict(previous.get("reconstruction") or {})
        reconstruction["repeatCount"] = int(reconstruction.get("repeatCount") or 1) + 1
        previous["reconstruction"] = reconstruction
        merged[match_index] = previous
        duplicate_count += 1

    return merged, {
        "droppedDoctorEventCount": dropped_doctor,
        "droppedRuntimeProbeCount": dropped_probe,
        "droppedLowSignalEventCount": dropped_fragment,
        "droppedQualityReasons": quality_reason_counts,
        "mergedDuplicateEventCount": duplicate_count,
    }


def memory_compile_state(conn: sqlite3.Connection, *, project: str) -> dict[str, object]:
    _ensure_compile_state_table(conn)
    row = conn.execute(
        "SELECT last_compiled_event_id, last_run_ms, pending_event_count, last_bundle_hash, "
        "last_drafted_event_id, last_draft_ms, last_draft_bundle_hash, last_draft_run_id "
        "FROM memory_compile_state WHERE project = ?",
        (project,),
    ).fetchone()
    last_event_id = int(row["last_compiled_event_id"] or 0) if row is not None else 0
    last_drafted_event_id = (
        int(row["last_drafted_event_id"] or 0) if row is not None else 0
    )
    last_draft_run_id = str(row["last_draft_run_id"] or "") if row is not None else ""
    last_draft_status = ""
    if last_draft_run_id:
        draft_row = conn.execute(
            "SELECT status FROM memory_cleanup_runs WHERE run_id = ?",
            (last_draft_run_id,),
        ).fetchone()
        if draft_row is not None:
            last_draft_status = str(draft_row["status"] or "")
    pending = int(
        conn.execute(
            """SELECT COUNT(*) FROM input_events e
               LEFT JOIN memory_state s ON s.event_id = e.id
               WHERE e.id > ? AND (? = '' OR e.project = ? OR e.project = '')
                 AND COALESCE(s.deleted, 0) = 0""",
            (last_event_id, project, project),
        ).fetchone()[0]
    )
    undrafted_cursor = max(last_event_id, last_drafted_event_id)
    undrafted = int(
        conn.execute(
            """SELECT COUNT(*) FROM input_events e
               LEFT JOIN memory_state s ON s.event_id = e.id
               WHERE e.id > ? AND (? = '' OR e.project = ? OR e.project = '')
                 AND COALESCE(s.deleted, 0) = 0""",
            (undrafted_cursor, project, project),
        ).fetchone()[0]
    )
    return {
        "project": project,
        "lastCompiledEventId": last_event_id,
        "lastRunMs": int(row["last_run_ms"] or 0) if row is not None else 0,
        "pendingEventCount": pending,
        "lastBundleHash": str(row["last_bundle_hash"] or "") if row is not None else "",
        "lastDraftedEventId": last_drafted_event_id,
        "lastDraftMs": int(row["last_draft_ms"] or 0) if row is not None else 0,
        "lastDraftBundleHash": (
            str(row["last_draft_bundle_hash"] or "") if row is not None else ""
        ),
        "lastDraftRunId": last_draft_run_id,
        "lastDraftStatus": last_draft_status,
        "activeDraftPendingReview": last_draft_status == "draft",
        "undraftedEventCount": undrafted,
        "draftCoversPending": undrafted == 0,
    }


def memory_compile_due(
    conn: sqlite3.Connection,
    *,
    project: str,
    manual: bool = False,
    idle_ms: int = 0,
    current_ms: int | None = None,
    min_events: int = 50,
    idle_threshold_ms: int = 20 * 60 * 1000,
    daily_interval_ms: int = 24 * 60 * 60 * 1000,
) -> tuple[bool, str, dict[str, object]]:
    state = memory_compile_state(conn, project=project)
    if manual:
        return True, "manual", state
    if bool(state.get("activeDraftPendingReview")):
        # Keep one authoritative review batch. New input waits behind it instead
        # of causing an hourly model call that supersedes unreviewed decisions.
        return False, "draft_pending_review", state
    undrafted = int(state["undraftedEventCount"])
    if undrafted >= max(1, int(min_events)):
        return True, "pending_events", state
    if undrafted > 0 and int(idle_ms) >= max(1, int(idle_threshold_ms)):
        return True, "idle", state
    current = now_ms() if current_ms is None else max(0, int(current_ms))
    last_maintenance_ms = max(
        int(state["lastRunMs"]),
        int(state["lastDraftMs"]),
    )
    if undrafted > 0 and current - last_maintenance_ms >= max(1, int(daily_interval_ms)):
        return True, "daily", state
    return False, "not_due", state


def _global_catalog_atoms_equivalent(
    left: Mapping[str, object] | None,
    right: Mapping[str, object] | None,
) -> bool:
    """Compare the complete DB-owned Atom identity, never a redacted projection."""
    if left is None or right is None:
        return False
    left_identity = compact_whitespace(str(left.get("identityHash") or ""))
    right_identity = compact_whitespace(str(right.get("identityHash") or ""))
    if left_identity and right_identity:
        if left_identity != right_identity:
            return False
    elif left_identity:
        if left_identity != memory_atom_identity_hash(right):
            return False
    elif right_identity:
        if right_identity != memory_atom_identity_hash(left):
            return False
    elif memory_atom_identity_hash(left) != memory_atom_identity_hash(right):
        return False
    left_authority = compact_whitespace(str(left.get("authorityHash") or ""))
    right_authority = compact_whitespace(str(right.get("authorityHash") or ""))
    if left_authority and right_authority:
        return left_authority == right_authority
    if left_authority:
        return left_authority == memory_atom_authority_hash(right)
    if right_authority:
        return right_authority == memory_atom_authority_hash(left)
    return memory_atom_authority_hash(left) == memory_atom_authority_hash(right)


def _global_catalog_tag_synonym(
    source: str,
    target: str,
    *,
    source_tag_id: object = 0,
    target_tag_id: object = 0,
    tags: list[dict[str, object]],
) -> bool:
    by_id = {
        _optional_int(item.get("tagId")): item
        for item in tags
        if _optional_int(item.get("tagId")) > 0
    }
    by_name: dict[str, dict[str, object]] = {}
    for item in tags:
        name = compact_whitespace(str(item.get("name") or ""))
        normalized_name = normalize_text(name)
        if not normalized_name:
            continue
        by_name.setdefault(normalized_name, item)
        for alias in _strings(item.get("aliases")):
            normalized_alias = normalize_text(alias)
            if normalized_alias:
                by_name.setdefault(normalized_alias, item)
    source_id = _optional_int(source_tag_id)
    target_id = _optional_int(target_tag_id)
    source_item = by_id.get(source_id) if source_id > 0 else by_name.get(
        normalize_text(source)
    )
    target_item = by_id.get(target_id) if target_id > 0 else by_name.get(
        normalize_text(target)
    )
    if source_item is None or target_item is None:
        return False
    resolved_source_id = _optional_int(source_item.get("tagId"))
    resolved_target_id = _optional_int(target_item.get("tagId"))
    if source_item is target_item or (
        resolved_source_id > 0
        and resolved_target_id > 0
        and resolved_source_id == resolved_target_id
    ):
        return False
    source_name = normalize_text(str(source_item.get("name") or source))
    target_name = normalize_text(str(target_item.get("name") or target))
    if not source_name or not target_name:
        return False
    if source_name == target_name:
        return (
            resolved_source_id > 0
            and resolved_target_id > 0
            and resolved_source_id != resolved_target_id
        )
    source_aliases = {
        normalize_text(alias)
        for alias in _strings(source_item.get("aliases"))
        if normalize_text(alias)
    }
    target_aliases = {
        normalize_text(alias)
        for alias in _strings(target_item.get("aliases"))
        if normalize_text(alias)
    }
    if source_name in target_aliases or target_name in source_aliases:
        return True
    # A chain such as C -> B -> A is still an exact synonym relation once the
    # intermediate aliases are resolved to A. Follow only declared alias
    # links; fuzzy and semantic relationships remain disallowed.
    alias_graph: dict[str, set[str]] = {}
    for item in tags:
        item_name = normalize_text(str(item.get("name") or ""))
        if not item_name:
            continue
        alias_graph.setdefault(item_name, set())
        for alias in _strings(item.get("aliases")):
            alias_name = normalize_text(alias)
            if not alias_name:
                continue
            alias_graph.setdefault(item_name, set()).add(alias_name)
            alias_graph.setdefault(alias_name, set()).add(item_name)
    reachable = {source_name}
    frontier = [source_name]
    while frontier:
        current = frontier.pop()
        for neighbour in alias_graph.get(current, ()):
            if neighbour not in reachable:
                reachable.add(neighbour)
                frontier.append(neighbour)
    return target_name in reachable


def _catalog_atom_union_values(
    atoms: list[Mapping[str, object]],
    key: str,
) -> list[str]:
    return _unique_strings(
        [
            value
            for atom in atoms
            for value in _strings(atom.get(key))
        ],
        limit=None,
    )


def _catalog_atom_union_ints(
    atoms: list[Mapping[str, object]],
    key: str,
) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for atom in atoms:
        for value in _positive_ints(atom.get(key)):
            if value not in seen:
                seen.add(value)
                result.append(value)
    return result


def _catalog_atom_merge_payload(
    target: Mapping[str, object],
    sources: list[Mapping[str, object]],
    *,
    target_id: str,
) -> dict[str, object]:
    """Rebuild a merge payload from complete DB-owned catalog rows."""
    members = [target, *sources]
    payload = dict(target)
    payload["atomId"] = target_id
    payload["operation"] = "merge"
    payload["globalCatalogMerge"] = True
    payload["expectedCardRevisions"] = {
        str(atom.get("atomId") or atom.get("id") or ""): str(atom["revision"])
        for atom in members if atom.get("revision")
    }
    for key in (
        "aliases",
        "surfaceHints",
        "queryExpansions",
        "sourceMemoryIds",
        "tags",
        "semanticGroupIds",
    ):
        payload[key] = _catalog_atom_union_values(members, key)
    payload["sourceEventIds"] = _catalog_atom_union_ints(members, "sourceEventIds")
    payload["mergeSourceIds"] = [
        compact_whitespace(str(atom.get("atomId") or atom.get("id") or ""))
        for atom in sources
        if compact_whitespace(str(atom.get("atomId") or atom.get("id") or ""))
    ]
    if payload["mergeSourceIds"]:
        payload["mergeSourceId"] = payload["mergeSourceIds"][0]
    # The target's scalar identity, authority, privacy, and binding fields are
    # intentionally retained verbatim; source rows only contribute evidence.
    payload["identityHash"] = compact_whitespace(
        str(target.get("identityHash") or "")
    ) or memory_atom_identity_hash(target)
    payload["authorityHash"] = compact_whitespace(
        str(target.get("authorityHash") or "")
    ) or memory_atom_authority_hash(target)

    return payload

def _catalog_supersede_graph_errors(
    supersedes: list[tuple[int, dict[str, object], str, str]],
    *,
    atoms_by_id: Mapping[str, Mapping[str, object]],
    accepted_upserts: Mapping[str, tuple[int, dict[str, object]]],
) -> list[str]:
    outgoing: dict[str, str] = {}
    incoming: dict[str, list[str]] = {}
    errors: list[str] = []
    for _index, _diff, old_id, new_id in supersedes:
        if old_id in outgoing and outgoing[old_id] != new_id:
            errors.append(f"duplicate_source:{old_id}")
            continue
        if old_id in outgoing:
            errors.append(f"duplicate_edge:{old_id}->{new_id}")
            continue
        outgoing[old_id] = new_id
        incoming.setdefault(new_id, []).append(old_id)
        if new_id in outgoing:
            errors.append(f"non_root_target:{new_id}")
        if new_id not in accepted_upserts:
            errors.append(f"missing_target_upsert:{new_id}")
        if old_id not in atoms_by_id or new_id not in atoms_by_id:
            errors.append(f"missing_atom:{old_id}->{new_id}")
    for target_id in sorted(set(outgoing.values()) & set(outgoing)):
        errors.append(f"non_root_target:{target_id}")
    state: dict[str, int] = {}
    for start in outgoing:
        node = start
        path: list[str] = []
        while node in outgoing:
            if state.get(node) == 1 or node in path:
                errors.append("cycle:" + "->".join([*path, node]))
                break
            if state.get(node) == 2:
                break
            state[node] = 1
            path.append(node)
            node = outgoing[node]
        for item in path:
            state[item] = 2
    nodes = set(outgoing) | set(incoming)
    unseen: set[str] = set(nodes)
    while unseen:
        root = unseen.pop()
        component = {root}
        frontier = [root]
        while frontier:
            item = frontier.pop()
            neighbours = set()
            if item in outgoing:
                neighbours.add(outgoing[item])
            neighbours.update(incoming.get(item, []))
            for neighbour in neighbours:
                if neighbour in unseen:
                    unseen.remove(neighbour)
                    component.add(neighbour)
                    frontier.append(neighbour)
        sinks = sorted(item for item in component if item not in outgoing)
        if len(sinks) != 1:
            errors.append(
                "root_count:" + ",".join(sinks or sorted(component))
            )
    return sorted(set(errors))


def _filter_global_catalog_diffs(
    diffs: list[dict[str, object]],
    *,
    source_bundle: Mapping[str, object] | None,
) -> tuple[list[dict[str, object]], list[str]]:
    """Keep only locally verified, complete catalog consolidation diffs."""
    source = source_bundle or {}
    atoms = _list_of_dicts(source.get("existingMemoryAtoms"))
    atoms_by_id = {
        compact_whitespace(str(item.get("atomId") or item.get("id") or "")): item
        for item in atoms
        if compact_whitespace(str(item.get("atomId") or item.get("id") or ""))
    }
    tags = _list_of_dicts(source.get("existingSemanticTags"))
    books = _list_of_dicts(source.get("existingMemoryBooks"))
    books_by_id = {
        compact_whitespace(str(item.get("bookId") or "")): item
        for item in books
        if compact_whitespace(str(item.get("bookId") or ""))
    }
    accepted_upserts: dict[str, tuple[int, dict[str, object]]] = {}
    supersedes: list[tuple[int, dict[str, object], str, str]] = []
    accepted_tag_merges: set[int] = set()
    accepted_book_merges: set[int] = set()
    warnings: list[str] = []
    for index, diff in enumerate(diffs):
        op = compact_whitespace(str(diff.get("op") or ""))
        payload = diff.get("payload")
        if op == "upsert_memory_atom":
            if not isinstance(payload, dict):
                warnings.append(f"global_catalog_disallowed_diff:{index}:upsert_memory_atom")
                continue
            if compact_whitespace(str(payload.get("operation") or "")).lower() != "merge":
                warnings.append(f"global_catalog_disallowed_diff:{index}:upsert_memory_atom")
                continue
            atom_id = compact_whitespace(
                str(diff.get("targetId") or payload.get("atomId") or "")
            )
            payload_atom_id = compact_whitespace(str(payload.get("atomId") or ""))
            existing = atoms_by_id.get(atom_id)
            if (
                not atom_id
                or (payload_atom_id and payload_atom_id != atom_id)
                or existing is None
                or not _global_catalog_atoms_equivalent(existing, payload)
            ):
                warnings.append(f"global_catalog_unsafe_atom_merge:{index}")
                continue
            accepted_upserts[atom_id] = (index, diff)
        elif op == "supersede_memory":
            if not isinstance(payload, dict):
                warnings.append(f"global_catalog_disallowed_diff:{index}:supersede_memory")
                continue
            old_id = compact_whitespace(
                str(payload.get("oldId") or diff.get("targetId") or "")
            )
            new_id = compact_whitespace(str(payload.get("newId") or ""))
            if (
                not old_id
                or not new_id
                or old_id == new_id
                or old_id not in atoms_by_id
                or new_id not in atoms_by_id
                or not _global_catalog_atoms_equivalent(
                    atoms_by_id.get(old_id),
                    atoms_by_id.get(new_id),
                )
            ):
                warnings.append(f"global_catalog_unsafe_atom_merge:{index}")
                continue
            supersedes.append((index, diff, old_id, new_id))
        elif op == "merge_semantic_tag":
            if not isinstance(payload, dict):
                warnings.append(f"global_catalog_disallowed_diff:{index}:merge_semantic_tag")
                continue
            source_name = compact_whitespace(str(payload.get("source") or ""))
            target_name = compact_whitespace(str(payload.get("target") or ""))
            if (
                not source_name
                or not target_name
                or not _global_catalog_tag_synonym(
                    source_name,
                    target_name,
                    source_tag_id=payload.get("sourceTagId"),
                    target_tag_id=payload.get("targetTagId"),
                    tags=tags,
                )
            ):
                warnings.append(f"global_catalog_unsafe_tag_merge:{index}")
                continue
            accepted_tag_merges.add(index)
        elif op == "merge_memory_books":
            if not isinstance(payload, dict):
                warnings.append(f"global_catalog_disallowed_diff:{index}:merge_memory_books")
                continue
            target_id = compact_whitespace(str(payload.get("targetBookId") or ""))
            source_ids = _memory_book_merge_sources(payload)
            target = books_by_id.get(target_id)
            if (
                target is None
                or compact_whitespace(str(target.get("bookType") or "topic")) != "topic"
                or compact_whitespace(str(target.get("status") or "")) not in {"active", "approved"}
                or not source_ids
                or target_id in source_ids
                or len(source_ids) != len(set(source_ids))
            ):
                warnings.append(f"global_catalog_unsafe_book_merge:{index}")
                continue
            sources = [books_by_id.get(source_id) for source_id in source_ids]
            if (
                any(source is None for source in sources)
                or any(
                    compact_whitespace(str(source.get("bookType") or "topic")) != "topic"
                    or compact_whitespace(str(source.get("status") or "")) not in {"active", "approved"}
                    or not _memory_books_same_scope(source, target)
                    for source in sources
                    if source is not None
                )
                or not compact_whitespace(str(payload.get("reason") or ""))
                or _bounded_float(payload.get("confidence"), default=0.0) < 0.8
            ):
                warnings.append(f"global_catalog_unsafe_book_merge:{index}")
                continue
            # Historical Books can already contain members from another
            # authority scope. Exclude that complete merge before inspection
            # so independent, valid groups can still be applied. Keep the
            # original Books and Atoms intact and expose the rejected refs.
            incompatible_members = sorted({
                member_id
                for book in [target, *sources]
                if book is not None
                for member_id in _strings(book.get("memoryAtomIds"))
                if member_id in atoms_by_id
                and not _memory_book_atom_scope_matches(target, atoms_by_id[member_id])
            })
            if incompatible_members:
                warnings.append(
                    f"global_catalog_book_merge_member_scope_mismatch:{index}:"
                    + ",".join(incompatible_members)
                )
                continue
            accepted_book_merges.add(index)
        else:
            warnings.append(f"global_catalog_disallowed_diff:{index}:{op or 'empty'}")

    graph_errors = _catalog_supersede_graph_errors(
        supersedes,
        atoms_by_id=atoms_by_id,
        accepted_upserts=accepted_upserts,
    )
    if graph_errors:
        warnings.append(
            "global_catalog_supersede_graph_invalid:" + "|".join(graph_errors)
        )
        return [], warnings

    accepted_indexes = set(accepted_tag_merges) | set(accepted_book_merges)
    paired_upserts: set[str] = set()
    source_ids_by_target: dict[str, list[str]] = {}
    for index, _diff, old_id, new_id in supersedes:
        upsert = accepted_upserts.get(new_id)
        if upsert is None:
            warnings.append(f"global_catalog_unpaired_atom_merge:{index}")
            continue
        if not _global_catalog_atoms_equivalent(
            atoms_by_id.get(new_id),
            upsert[1].get("payload")
            if isinstance(upsert[1].get("payload"), dict)
            else None,
        ):
            warnings.append(f"global_catalog_unsafe_atom_merge:{index}")
            continue
        accepted_indexes.update({index, upsert[0]})
        paired_upserts.add(new_id)
        source_ids_by_target.setdefault(new_id, []).append(old_id)
    for atom_id, (index, _diff) in accepted_upserts.items():
        if atom_id not in paired_upserts:
            warnings.append(f"global_catalog_unpaired_atom_merge:{index}")

    rewritten: list[dict[str, object]] = []
    for index, diff in enumerate(diffs):
        if index not in accepted_indexes:
            continue
        if diff.get("op") == "merge_memory_books":
            payload = diff.get("payload")
            if not isinstance(payload, dict):
                continue
            target_id = compact_whitespace(str(payload.get("targetBookId") or ""))
            source_ids = _memory_book_merge_sources(payload)
            target = books_by_id.get(target_id)
            sources = [books_by_id.get(source_id) for source_id in source_ids]
            if (
                target is None
                or any(source is None for source in sources)
                or any(
                    not _memory_books_same_scope(source, target)
                    for source in sources
                    if source is not None
                )
            ):
                warnings.append(f"global_catalog_unsafe_book_merge:{index}")
                continue
            rewritten_diff = dict(diff)
            rewritten_diff["targetId"] = target_id
            rewritten_diff["payload"] = {
                "targetBookId": target_id,
                "sourceBookIds": source_ids,
                "ownerKind": _memory_book_field(target, "owner_kind"),
                "ownerId": _memory_book_field(target, "owner_id"),
                "project": _memory_book_field(target, "project"),
                "reason": compact_whitespace(str(payload.get("reason") or ""))[:360],
                "confidence": _bounded_float(payload.get("confidence"), default=0.0),
            }
            rewritten.append(rewritten_diff)
            continue
        if diff.get("op") != "upsert_memory_atom":
            rewritten.append(diff)
            continue
        target_id = compact_whitespace(str(diff.get("targetId") or ""))
        target = atoms_by_id.get(target_id)
        source_atoms = [
            atoms_by_id[source_id]
            for source_id in source_ids_by_target.get(target_id, [])
            if source_id in atoms_by_id
        ]
        if target is None or len(source_atoms) != len(
            source_ids_by_target.get(target_id, [])
        ):
            warnings.append(f"global_catalog_unsafe_atom_merge:{index}")
            continue
        rewritten_diff = dict(diff)
        rewritten_diff["payload"] = _catalog_atom_merge_payload(
            target,
            source_atoms,
            target_id=target_id,
        )
        rewritten.append(rewritten_diff)
    return rewritten, warnings


def memory_book_plan_from_compile_output(
    compile_output: dict[str, object],
    *,
    project: str,
    provider: str,
    model: str,
    run_id: str = "",
    source_bundle: dict[str, object] | None = None,
    owner_kind: str = "user",
    owner_id: str = "default",
    run_kind: str = "legacy",
) -> dict[str, object]:
    normalized_owner_kind, normalized_owner_id = _memory_owner(owner_kind, owner_id)
    diffs: list[dict[str, object]] = []
    warnings = list(compile_output.get("warnings") or [])
    source_data = source_bundle or {}
    frozen_cards = {str(item.get("atomId") or ""): item
                    for item in _list_of_dicts(source_data.get("existingMemoryAtoms"))}
    source_scope = compact_whitespace(
        str(source_data.get("curationScope") or compile_output.get("curationScope") or "incremental")
    ).lower()
    curation_scope = "global" if source_scope == "global" else "incremental"
    global_scope = curation_scope == "global"
    catalog_audit = bool(
        source_data.get("catalogAudit")
        if "catalogAudit" in source_data
        else compile_output.get("catalogAudit")
    )
    catalog_truncated = dict(
        source_data.get("catalogTruncated")
        or compile_output.get("catalogTruncated")
        or {}
    )
    catalog_complete = bool(
        source_data.get("catalogComplete")
        if "catalogComplete" in source_data
        else compile_output.get(
            "catalogComplete",
            not global_scope and not any(catalog_truncated.values()),
        )
    )
    tag_merges = _planned_tag_merges(compile_output, source_bundle=source_bundle)
    book_merges = _planned_memory_book_merges(
        compile_output,
        source_bundle=source_bundle,
    )
    tag_name_map = {
        normalize_text(str(item["source"])): str(item["target"])
        for item in tag_merges
        if normalize_text(str(item.get("source") or ""))
    }
    existing_group_ids = {
        compact_whitespace(str(item.get("groupId") or "")).lower()
        for item in _list_of_dicts((source_bundle or {}).get("existingSemanticGroups"))
        if compact_whitespace(str(item.get("groupId") or ""))
    }
    accepted_group_ids: set[str] = set()
    group_source_ids: dict[str, list[int]] = {}
    new_group_count = 0
    for item in _list_of_dicts(compile_output.get("semanticGroups")):
        title = compact_whitespace(str(item.get("title") or item.get("name") or ""))
        description = compact_whitespace(str(item.get("description") or item.get("summary") or ""))
        if not title or not description:
            continue
        group_id = _semantic_group_id(item.get("groupId"), title=title)
        if group_id in accepted_group_ids:
            warnings.append(f"duplicate_semantic_group_ignored:{group_id}")
            continue
        if len(accepted_group_ids) >= 8:
            warnings.append("semantic_group_total_limit_applied")
            continue
        if group_id not in existing_group_ids:
            if new_group_count >= 3:
                warnings.append("semantic_group_new_limit_applied")
                continue
            new_group_count += 1
        accepted_group_ids.add(group_id)
        source_ids = _positive_ints(item.get("sourceEventIds")) or _infer_source_event_ids(
            [title, description, *_strings(item.get("aliases")), *_strings(item.get("tags"))],
            source_bundle=source_bundle,
        )
        group_source_ids[group_id] = source_ids
        diffs.append(
            {
                "op": "upsert_semantic_group",
                "targetId": group_id,
                "payload": {
                    "groupId": group_id,
                    "title": title,
                    "description": description,
                    "project": compact_whitespace(str(item.get("project") or project)),
                    "aliases": _strings(item.get("aliases")),
                    "tags": _canonical_tag_names(_strings(item.get("tags")), tag_name_map),
                    "sourceEventIds": source_ids,
                    "confidence": _bounded_float(item.get("confidence"), default=0.7),
                    "qualityScore": _bounded_float(item.get("qualityScore"), default=0.7),
                    "status": compact_whitespace(str(item.get("status") or "active")) or "active",
                },
                "status": "pending",
            }
        )
    for item in _list_of_dicts(compile_output.get("semanticTags")):
        name = compact_whitespace(str(item.get("name") or item.get("tag") or ""))
        description = compact_whitespace(str(item.get("description") or ""))
        if not name or not description:
            continue
        canonical_name = _canonical_tag_name(name, tag_name_map)
        if normalize_text(canonical_name) != normalize_text(name):
            warnings.append(f"semantic_tag_merged_into_existing:{name}->{canonical_name}")
            continue
        name = canonical_name
        source_ids = _positive_ints(item.get("sourceEventIds")) or _infer_source_event_ids(
            [name, description, *_strings(item.get("aliases"))],
            source_bundle=source_bundle,
        )
        target = f"semantic-tag:{stable_text_hash(normalize_text(name)).removeprefix('sha256:')[:20]}"
        diffs.append(
            {
                "op": "upsert_semantic_tag",
                "targetId": target,
                "payload": {
                    "name": name,
                    "description": description,
                    "type": compact_whitespace(str(item.get("type") or "concept")) or "concept",
                    "aliases": _canonical_tag_aliases(name, _strings(item.get("aliases")), tag_name_map),
                    "semanticGroupIds": _resolved_semantic_group_ids(
                        item,
                        source_ids=source_ids,
                        group_source_ids=group_source_ids,
                    ),
                    "sourceEventIds": source_ids,
                    "confidence": _bounded_float(item.get("confidence"), default=0.7),
                    "qualityScore": _bounded_float(item.get("qualityScore"), default=0.7),
                    "status": compact_whitespace(str(item.get("status") or "active")) or "active",
                },
                "status": "pending",
            }
        )
    for merge in book_merges:
        target_book_id = compact_whitespace(str(merge.get("targetBookId") or ""))
        diffs.append(
            {
                "op": "merge_memory_books",
                "targetId": target_book_id,
                "payload": dict(merge),
                "status": "pending",
            }
        )
    book_items = [
        *(("daily", item) for item in _list_of_dicts(compile_output.get("dailyBooks"))),
        *(("topic", item) for item in _list_of_dicts(compile_output.get("topicBooks"))),
    ]
    for default_book_type, item in book_items:
        title = compact_whitespace(str(item.get("title") or ""))
        summary = compact_whitespace(str(item.get("summary") or ""))
        if not title and not summary:
            continue
        existing_book = (
            _match_existing_topic_book(
                item,
                source_bundle=source_bundle,
                project=project,
                owner_kind=normalized_owner_kind,
                owner_id=normalized_owner_id,
            )
            if default_book_type == "topic"
            else None
        )
        if default_book_type == "topic":
            default_book_key = f"topic-{stable_text_hash(title or summary).removeprefix('sha256:')[:16]}"
        else:
            default_book_key = _default_book_key(source_bundle)
        # A topic organizer is allowed to invent a fresh id/key, but a strong
        # match against an existing topic must win. Otherwise every maintenance
        # run would create another near-duplicate book even though reuse was
        # detected above.
        book_key = (
            compact_whitespace(str((existing_book or {}).get("bookKey") or ""))
            or compact_whitespace(str(item.get("bookKey") or ""))
            or default_book_key
        )
        book_type = compact_whitespace(str(item.get("bookType") or default_book_type)) or default_book_type
        book_id = (
            compact_whitespace(str((existing_book or {}).get("bookId") or ""))
            or compact_whitespace(str(item.get("bookId") or ""))
            or f"book:{book_type}:{book_key}"
        )
        inferred_source_ids = _positive_ints(item.get("sourceEventIds")) or _infer_source_event_ids(
            [book_key, title, summary, *_strings(item.get("tags")), *_strings(item.get("surfaceHints"))],
            source_bundle=source_bundle,
        )
        source_ids = _positive_ints(
            [*_positive_ints((existing_book or {}).get("sourceEventIds")), *inferred_source_ids]
        )
        prior_tags = _strings((existing_book or {}).get("tags"))
        prior_atom_ids = _strings((existing_book or {}).get("memoryAtomIds"))
        scope_payload: dict[str, object] = {}
        for scope_key in (
            "knowledgeDomain",
            "scopeKind",
            "scopeId",
            "visibility",
            "authorizationRevision",
            "bindingId",
            "scopeMode",
        ):
            if scope_key in item:
                scope_payload[scope_key] = item.get(scope_key)
            elif existing_book is not None and scope_key in existing_book:
                scope_payload[scope_key] = existing_book.get(scope_key)
        payload = {
            "bookId": book_id,
            "bookType": book_type,
            "bookKey": book_key,
            "title": title,
            "summary": summary,
            "topicAliases": _unique_strings(
                [
                    *_strings((existing_book or {}).get("aliases")),
                    *_strings(item.get("aliases")),
                    title,
                ],
                limit=64,
            ),
            "tags": _canonical_tag_names([*prior_tags, *_strings(item.get("tags"))], tag_name_map),
            "surfaceHints": _strings(item.get("surfaceHints")),
            "queryExpansions": _strings(item.get("queryExpansions")),
            "sourceEventIds": source_ids,
            "memoryAtomIds": _unique_strings([*prior_atom_ids, *_strings(item.get("memoryAtomIds"))], limit=256),
            "semanticGroupIds": _resolved_semantic_group_ids(
                item,
                source_ids=source_ids,
                group_source_ids=group_source_ids,
            ),
            "project": compact_whitespace(
                str(
                    item.get("project")
                    if "project" in item
                    else (existing_book or {}).get("project") or project
                )
            ),
            "app": compact_whitespace(
                str(
                    item.get("app")
                    if "app" in item
                    else (existing_book or {}).get("app") or ""
                )
            ),
            **scope_payload,
            "confidence": _bounded_float(item.get("confidence"), default=0.5),
            "qualityScore": _bounded_float(item.get("qualityScore"), default=_bounded_float(item.get("confidence"), default=0.5)),
            "status": compact_whitespace(str(item.get("status") or "active")) or "active",
            "contextGroupId": _group_for_source_ids(source_ids, source_bundle=source_bundle),
            "reusedExistingBookId": compact_whitespace(str((existing_book or {}).get("bookId") or "")),
            "previousStatus": compact_whitespace(str((existing_book or {}).get("status") or "")),
            "ownerKind": normalized_owner_kind,
            "ownerId": normalized_owner_id,
        }
        diffs.append({"op": "upsert_memory_book", "targetId": book_id, "payload": payload, "status": "pending"})
    for item in _list_of_dicts(compile_output.get("memoryAtoms")):
        atom_id = compact_whitespace(str(item.get("atomId") or item.get("id") or ""))
        canonical = compact_whitespace(str(item.get("canonicalText") or item.get("text") or ""))
        if not canonical:
            continue
        if not atom_id and canonical:
            atom_id = f"atom:{stable_text_hash(canonical)[:16]}"
        source_ids = _positive_ints(item.get("sourceEventIds")) or _infer_source_event_ids(
            [
                canonical,
                compact_whitespace(str(item.get("summary") or "")),
                *_strings(item.get("tags")),
                *_strings(item.get("aliases")),
                *_strings(item.get("surfaceHints")),
                *_strings(item.get("queryExpansions")),
            ],
            source_bundle=source_bundle,
        )
        atom_kind = compact_whitespace(str(item.get("kind") or "project_fact"))
        personal_v2 = (
            compact_whitespace(str(item.get("knowledgeDomain") or ""))
            == "personal_memory"
            and compact_whitespace(str(item.get("scopeMode") or ""))
            == "authoritative"
        )
        atom_project = (
            ""
            if personal_v2
            else compact_whitespace(str(item.get("project") or project))
        )
        atom_app = "" if personal_v2 else compact_whitespace(str(item.get("app") or ""))
        claim_key = _normalized_claim_key(
            item.get("claimKey") or item.get("claim") or atom_id
        )
        if not claim_key:
            # Older organizer/eval payloads predate claim lineage. Keep them
            # isolated under a deterministic atom-scoped claim rather than
            # rejecting the whole governed plan or guessing that two
            # differently worded facts are the same claim.
            claim_key = (
                "atom:"
                + stable_text_hash(canonical).removeprefix("sha256:")[:24]
            )
        lineage_id = compact_whitespace(str(item.get("lineageId") or ""))[:200]
        if claim_key and not lineage_id:
            lineage_id = (
                "lineage:"
                + stable_text_hash(
                    f"{atom_kind}\n{atom_project}\n{atom_app}\n{claim_key}"
                ).removeprefix("sha256:")[:24]
            )
        payload = {
            "atomId": atom_id,
            "identityHash": compact_whitespace(str(item.get("identityHash") or "")),
            "authorityHash": compact_whitespace(str(item.get("authorityHash") or "")),
            "tagRelationsHash": compact_whitespace(
                str(item.get("tagRelationsHash") or "")
            ),
            "groupMembershipsHash": compact_whitespace(
                str(item.get("groupMembershipsHash") or "")
            ),
            "aliasRecordsHash": compact_whitespace(
                str(item.get("aliasRecordsHash") or "")
            ),
            "mergeSourceId": compact_whitespace(
                str(item.get("mergeSourceId") or "")
            ),
            "mergeSourceIds": _strings(item.get("mergeSourceIds")),
            "globalCatalogMerge": bool(item.get("globalCatalogMerge")),
            "kind": atom_kind,
            "language": compact_whitespace(str(item.get("language") or "zh")) or "zh",
            "canonicalText": canonical,
            "text": compact_whitespace(str(item.get("text") or canonical)),
            "summary": compact_whitespace(str(item.get("summary") or "")),
            "tags": _canonical_tag_names(_strings(item.get("tags")), tag_name_map),
            "aliases": _strings(item.get("aliases")),
            "surfaceHints": _strings(item.get("surfaceHints")),
            "queryExpansions": _strings(item.get("queryExpansions")),
            "sourceEventIds": source_ids,
            "sourceMemoryIds": _strings(item.get("sourceMemoryIds")),
            "semanticGroupIds": _resolved_semantic_group_ids(
                item,
                source_ids=source_ids,
                group_source_ids=group_source_ids,
            ),
            "directCandidateAllowed": bool(item.get("directCandidateAllowed", False)),
            "project": atom_project,
            "app": atom_app,
            "claimKey": claim_key,
            "lineageId": lineage_id,
            "claimState": compact_whitespace(
                str(item.get("claimState") or "current")
            ).lower(),
            "validFromMs": (
                _optional_int(item.get("validFromMs"))
                or _latest_source_event_ms(source_ids, source_bundle=source_bundle)
            ),
            "validToMs": _optional_int(item.get("validToMs")) or None,
            "supersedesId": compact_whitespace(
                str(item.get("supersedesId") or "")
            )[:240],
            "confidence": _bounded_float(item.get("confidence"), default=0.5),
            "qualityScore": _bounded_float(item.get("qualityScore"), default=0.5),
            "status": compact_whitespace(str(item.get("status") or "active")) or "active",
            "contextGroupId": _group_for_source_ids(source_ids, source_bundle=source_bundle),
            "ownerKind": compact_whitespace(
                str(item.get("ownerKind") or normalized_owner_kind)
            ),
            "ownerId": compact_whitespace(
                str(item.get("ownerId") or normalized_owner_id)
            ),
            "evidenceIds": _strings(item.get("evidenceIds")),
            "curationRunId": compact_whitespace(
                str(item.get("curationRunId") or "")
            ),
            "operation": compact_whitespace(str(item.get("operation") or "")),
            "knowledgeDomain": compact_whitespace(
                str(item.get("knowledgeDomain") or "legacy")
            ),
            "scopeKind": compact_whitespace(
                str(item.get("scopeKind") or "legacy")
            ),
            "scopeId": compact_whitespace(str(item.get("scopeId") or "")),
            "visibility": compact_whitespace(
                str(item.get("visibility") or "legacy")
            ),
            "authorizationRevision": compact_whitespace(
                str(item.get("authorizationRevision") or "")
            ),
            "bindingId": compact_whitespace(str(item.get("bindingId") or "")),
            "scopeMode": compact_whitespace(
                str(item.get("scopeMode") or "legacy")
            ),
            "privacyLevel": compact_whitespace(
                str(item.get("privacyLevel") or "")
            ),
            "curationArchitecture": compact_whitespace(
                str(item.get("curationArchitecture") or "")
            ),
        }
        # Only the server's frozen source catalog may provide edit fences.
        payload["expectedCardRevisions"] = {
            key: str(value["revision"]) for key, value in frozen_cards.items()
            if value.get("revision") and (key == atom_id or (
                value.get("claimKey") == claim_key
                and str(value.get("ownerKind") or "user") == payload["ownerKind"]
                and str(value.get("ownerId") or "default") == payload["ownerId"]
                and str(value.get("project") or "") == atom_project
                and str(value.get("app") or "") == atom_app))
        }
        diffs.append({"op": "upsert_memory_atom", "targetId": atom_id, "payload": payload, "status": "pending"})
    for item in _list_of_dicts(compile_output.get("tagEdges")):
        src = _canonical_tag_name(
            compact_whitespace(str(item.get("src") or item.get("sourceTagName") or item.get("source") or "")),
            tag_name_map,
        )
        dst = _canonical_tag_name(
            compact_whitespace(str(item.get("dst") or item.get("targetTagName") or item.get("target") or "")),
            tag_name_map,
        )
        if not src or not dst:
            continue
        if normalize_text(src) == normalize_text(dst):
            warnings.append(f"self_tag_edge_ignored:{src}")
            continue
        edge_type = compact_whitespace(str(item.get("edgeType") or item.get("relationType") or "related")) or "related"
        evidence_ids = _positive_ints(item.get("evidenceEventIds")) or _infer_source_event_ids(
            [src, dst, edge_type],
            source_bundle=source_bundle,
        )
        target = f"tag-edge:{src}->{dst}:{edge_type}"
        diffs.append(
            {
                "op": "upsert_tag_edge",
                "targetId": target,
                "payload": {
                    "src": src,
                    "dst": dst,
                    "edgeType": edge_type,
                    "weight": _bounded_float(
                        item.get("weight"),
                        default=_bounded_float(item.get("confidence"), default=0.5),
                    ),
                    "evidenceEventIds": evidence_ids,
                },
                "status": "pending",
            }
        )
    for item in _list_of_dicts(compile_output.get("phraseCandidates")):
        text = compact_whitespace(str(item.get("text") or ""))
        if len(text) < 2 or len(text) > 18:
            continue
        pinyin = _normalized_rime_pinyin(item.get("pinyin"))
        review_source = "dsv4" if compact_whitespace(provider).lower() in {"deepseek", "deepseek-v4", "dsv4"} else "memory"
        target = f"phrase:{normalize_text(text)}"
        source_ids = _positive_ints(item.get("sourceEventIds")) or _infer_source_event_ids(
            [text, *_canonical_tag_names(_strings(item.get("tags")), tag_name_map)],
            source_bundle=source_bundle,
        )
        diffs.append(
            {
                "op": "add_phrase_candidate",
                "targetId": target,
                "payload": {
                    "memoryId": target,
                    "text": text,
                    "pinyin": pinyin,
                    "reviewSource": review_source if pinyin else "memory",
                    "reviewReason": compact_whitespace(
                        str(item.get("reason") or "")
                    ),
                    "tags": _strings(item.get("tags")),
                    "semanticGroupIds": _resolved_semantic_group_ids(
                        item,
                        source_ids=source_ids,
                        group_source_ids=group_source_ids,
                    ),
                    "sourceEventIds": source_ids,
                    "weight": _bounded_float(item.get("weight"), default=0.6),
                    "project": compact_whitespace(
                        str(item.get("project") or project)
                    ),
                    "contextGroupId": _group_for_source_ids(
                        source_ids,
                        source_bundle=source_bundle,
                    ),
                },
                "status": "pending",
            }
        )
    for item in _list_of_dicts(compile_output.get("negativePhrases")):
        text = compact_whitespace(str(item.get("text") or ""))
        if len(text) < 2 or len(text) > 48:
            continue
        source_ids = _positive_ints(item.get("sourceEventIds")) or _infer_source_event_ids(
            [text, compact_whitespace(str(item.get("reason") or ""))],
            source_bundle=source_bundle,
        )
        suppression_id = compact_whitespace(
            str(item.get("suppressionId") or f"negative:{normalize_text(text)}")
        )
        diffs.append(
            {
                "op": "add_negative_phrase",
                "targetId": suppression_id,
                "payload": {
                    "suppressionId": suppression_id,
                    "text": text,
                    "reason": compact_whitespace(
                        str(item.get("reason") or "memory_compiler_negative_phrase")
                    ),
                    "sourceEventIds": source_ids,
                },
                "status": "pending",
            }
        )
    for item in _list_of_dicts(compile_output.get("supersedes")):
        old_id = compact_whitespace(str(item.get("oldId") or item.get("supersededId") or ""))
        new_id = compact_whitespace(str(item.get("newId") or item.get("supersedingId") or ""))
        if not old_id or not new_id or old_id == new_id:
            continue
        source_ids = _positive_ints(item.get("sourceEventIds")) or _infer_source_event_ids(
            [old_id, new_id],
            source_bundle=source_bundle,
        )
        diffs.append(
            {
                "op": "supersede_memory",
                "targetId": old_id,
                "payload": {
                    "oldId": old_id,
                    "expectedRevision": dict(frozen_cards.get(old_id) or {}).get("revision"),
                    "globalCatalogMerge": global_scope,
                    "newId": new_id,
                    "sourceEventIds": source_ids,
                    "reason": compact_whitespace(str(item.get("reason") or "newer_explicit_information")),
                },
                "status": "pending",
            }
        )
    for item in _list_of_dicts(compile_output.get("memoryRetractions")):
        target_id = compact_whitespace(
            str(item.get("targetAtomId") or item.get("targetId") or "")
        )
        if not target_id:
            continue
        source_ids = _positive_ints(item.get("sourceEventIds"))
        diffs.append(
            {
                "op": "retract_memory_atom",
                "targetId": target_id,
                "payload": {
                    "targetAtomId": target_id,
                    "expectedRevision": dict(frozen_cards.get(target_id) or {}).get("revision"),
                    "reason": compact_whitespace(
                        str(item.get("reason") or "explicit_user_forget")
                    ),
                    "sourceEventIds": source_ids,
                    "confidence": _bounded_float(
                        item.get("confidence"),
                        default=0.0,
                    ),
                    "project": (
                        ""
                        if compact_whitespace(
                            str(item.get("knowledgeDomain") or "")
                        )
                        == "personal_memory"
                        else compact_whitespace(
                            str(item.get("project") or project)
                        )
                    ),
                    "ownerKind": normalized_owner_kind,
                    "ownerId": normalized_owner_id,
                    "evidenceIds": _strings(item.get("evidenceIds")),
                    "curationRunId": compact_whitespace(
                        str(item.get("curationRunId") or "")
                    ),
                    "knowledgeDomain": compact_whitespace(
                        str(item.get("knowledgeDomain") or "legacy")
                    ),
                    "scopeKind": compact_whitespace(
                        str(item.get("scopeKind") or "legacy")
                    ),
                    "scopeId": compact_whitespace(
                        str(item.get("scopeId") or "")
                    ),
                    "visibility": compact_whitespace(
                        str(item.get("visibility") or "legacy")
                    ),
                    "scopeMode": compact_whitespace(
                        str(item.get("scopeMode") or "legacy")
                    ),
                },
                "status": "pending",
            }
        )
    for item in tag_merges:
        source = compact_whitespace(str(item.get("source") or ""))
        target = compact_whitespace(str(item.get("target") or ""))
        source_tag_id = _optional_int(item.get("sourceTagId"))
        target_tag_id = _optional_int(item.get("targetTagId"))
        same_tag = (
            source_tag_id == target_tag_id
            if source_tag_id > 0 and target_tag_id > 0
            else normalize_text(source) == normalize_text(target)
        )
        if not source or not target or same_tag:
            continue
        if not bool(item.get("requiresApply", True)):
            warnings.append(f"virtual_tag_merge_canonicalized:{source}->{target}")
            continue
        evidence_ids = _positive_ints(
            item.get("evidenceEventIds")
        ) or _infer_source_event_ids(
            [source, target, compact_whitespace(str(item.get("reason") or ""))],
            source_bundle=source_bundle,
        )
        diffs.append(
            {
                "op": "merge_semantic_tag",
                "targetId": (
                    f"tag-merge:{source_tag_id}->{target_tag_id}"
                    if source_tag_id > 0 and target_tag_id > 0
                    else f"tag-merge:{source}->{target}"
                ),
                "payload": {
                    "source": source,
                    "target": target,
                    **(
                        {"sourceTagId": source_tag_id}
                        if source_tag_id > 0
                        else {}
                    ),
                    **(
                        {"targetTagId": target_tag_id}
                        if target_tag_id > 0
                        else {}
                    ),
                    "reason": compact_whitespace(
                        str(item.get("reason") or "同义标签规范化")
                    ),
                    "evidenceEventIds": evidence_ids,
                    "confidence": _bounded_float(
                        item.get("confidence"),
                        default=0.8,
                    ),
                },
                "status": "pending",
            }
        )
    proposed_tag_names = {
        normalize_text(str(diff.get("payload", {}).get("name") or ""))
        for diff in diffs
        if diff.get("op") == "upsert_semantic_tag" and isinstance(diff.get("payload"), dict)
    }
    connected_tag_names = {
        normalize_text(str(diff.get("payload", {}).get(field) or ""))
        for diff in diffs
        if diff.get("op") == "upsert_tag_edge" and isinstance(diff.get("payload"), dict)
        for field in ("src", "dst")
    }
    isolated_proposed_tags = sorted(name for name in proposed_tag_names if name and name not in connected_tag_names)
    if len(proposed_tag_names) > 1 and isolated_proposed_tags:
        warnings.append(f"isolated_semantic_tags_in_draft:{len(isolated_proposed_tags)}")
    run_id = compact_whitespace(str(run_id or "")) or f"memory_book_{now_ms()}"
    resolved_run_kind = (
        MEMORY_CATALOG_RUN_KIND
        if global_scope
        else compact_whitespace(run_kind) or "legacy"
    )
    catalog_graph_valid = True
    if (
        resolved_run_kind == "legacy"
        and source_bundle
        and not any(diff.get("op") == "upsert_memory_book" for diff in diffs)
    ):
        daily_book = _synthesize_daily_book_diff_from_diffs(
            diffs,
            project=project,
            source_bundle=source_bundle,
        )
        if daily_book:
            diffs.insert(0, daily_book)
            warnings.append("daily_book_synthesized_from_atoms")
    if global_scope:
        if not catalog_audit or not catalog_complete:
            diffs = []
            catalog_graph_valid = False
            warnings.append("global_catalog_incomplete")
        else:
            diffs, global_warnings = _filter_global_catalog_diffs(
                diffs,
                source_bundle=source_bundle,
            )
            warnings.extend(global_warnings)
            catalog_graph_valid = not any(
                warning.startswith("global_catalog_supersede_graph_invalid:")
                for warning in global_warnings
            )
    for diff in diffs:
        if diff.get("op") not in {"upsert_memory_book", "upsert_memory_atom"}:
            continue
        if global_scope and diff.get("op") == "upsert_memory_atom":
            continue
        payload = diff.get("payload")
        if isinstance(payload, dict):
            payload["ownerKind"] = normalized_owner_kind
            payload["ownerId"] = normalized_owner_id
    curation_architecture = compact_whitespace(
        str(compile_output.get("curationArchitecture") or "")
    )
    curation_outcome = compact_whitespace(
        str(compile_output.get("curationOutcome") or "")
    )
    if curation_architecture.startswith("atom-first"):
        # The plan is the authoritative write surface. A native lexicon
        # projection, so the raw compiler arrays must not claim a review is
        # still required.
        curation_outcome = "changes" if diffs else "no_changes"
    if not diffs and source_bundle and curation_outcome == "no_changes":
        warnings.append("curation_review_found_no_changes")
    elif not diffs and source_bundle:
        # Never turn raw history into semantic memory when the organizer did
        # not produce a governed result. The cursor stays pending so a later
        # DSV4 run can retry instead of indexing uncleaned ASR/user text.
        warnings.append("organizer_returned_no_governed_memory")
    return {
        "schemaVersion": MEMORY_BOOK_RUN_SCHEMA_VERSION,
        "runId": run_id,
        "provider": provider,
        "model": model,
        "summary": _memory_book_summary(diffs),
        "metadata": {
            "compileSchemaVersion": str(compile_output.get("schemaVersion") or MEMORY_BOOK_COMPILE_SCHEMA_VERSION),
            "warnings": warnings,
            "elapsedMs": int(compile_output.get("elapsedMs") or 0),
            "modelDiagnostics": dict(compile_output.get("modelDiagnostics") or {}),
            "modelBundleStats": dict(compile_output.get("modelBundleStats") or {}),
            "personalCurationV2": dict(
                compile_output.get("personalCurationV2") or {}
            ),
            "instruction": _sanitize_text(str(compile_output.get("instruction") or ""), max_chars=600)[0],
            "curationArchitecture": curation_architecture,
            "curationOutcome": curation_outcome,
            "curationScope": curation_scope,
            "catalogDigest": compact_whitespace(
                str(
                    source_data.get("catalogDigest")
                    or compile_output.get("catalogDigest")
                    or ""
                )
            ),
            "catalogAudit": catalog_audit,
            "catalogTruncated": catalog_truncated,
            "curationDiagnostics": dict(compile_output.get("curationDiagnostics") or {}),
            "lexiconDiagnostics": dict(compile_output.get("lexiconDiagnostics") or {}),
            "tagGraphDiagnostics": {
                "existingTags": len(_list_of_dicts((source_bundle or {}).get("existingSemanticTags"))),
                "existingEdges": len(_list_of_dicts((source_bundle or {}).get("existingTagEdges"))),
                "proposedTags": len(proposed_tag_names),
                "proposedEdges": sum(1 for diff in diffs if diff.get("op") == "upsert_tag_edge"),
                "proposedMerges": sum(1 for diff in diffs if diff.get("op") == "merge_semantic_tag"),
                "isolatedProposedTags": isolated_proposed_tags,
            },
            "project": project,
            "ownerKind": normalized_owner_kind,
            "ownerId": normalized_owner_id,
            "runKind": resolved_run_kind,
            "catalogComplete": catalog_complete,
            "catalogGraphValid": catalog_graph_valid,
            "bundleHash": str((source_bundle or {}).get("bundleHash") or ""),
            "sourceCursor": dict((source_bundle or {}).get("cursor") or {}),
            "legalContextGroupIds": list((source_bundle or {}).get("legalContextGroupIds") or []),
            "legalSourceEventIds": _legal_source_event_ids(source_bundle),
            "sourceInputRefs": _source_input_refs(source_bundle),
            "existingSemanticGroupIds": [
                str(item.get("groupId") or "")
                for item in _list_of_dicts((source_bundle or {}).get("existingSemanticGroups"))
                if compact_whitespace(str(item.get("groupId") or ""))
            ],
            "globalCatalogAtoms": (
                _list_of_dicts(source_data.get("existingMemoryAtoms"))
                if global_scope
                else []
            ),
            "globalCatalogTags": (
                _list_of_dicts(source_data.get("existingSemanticTags"))
                if global_scope
                else []
            ),
            "globalCatalogBooks": (
                _list_of_dicts(source_data.get("existingMemoryBooks"))
                if global_scope
                else []
            ),
            **(
                purpose_audit_fields(
                    dict((source_bundle or {}).get("purposeProfile") or {})
                )
                if isinstance((source_bundle or {}).get("purposeProfile"), dict)
                else {}
            ),
        },
        "diffs": diffs,
    }


def load_memory_book_plan(path: str | Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("memory book run file must contain a JSON object")
    return payload


def inspect_memory_book_plan(plan: dict[str, object]) -> dict[str, object]:
    errors: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    counts: dict[str, int] = {
        "semanticGroups": 0,
        "semanticTags": 0,
        "tagMerges": 0,
        "bookMerges": 0,
        "memoryBooks": 0,
        "memoryAtoms": 0,
        "tagEdges": 0,
        "phraseCandidates": 0,
        "negativePhrases": 0,
        "supersedes": 0,
        "retractions": 0,
    }
    diffs = _list_of_dicts(plan.get("diffs"))
    metadata = dict(plan.get("metadata") or {})
    legal_source_event_ids = set(_positive_ints(metadata.get("legalSourceEventIds")))
    legal_groups = {
        compact_whitespace(str(item))
        for item in metadata.get("legalContextGroupIds", [])
        if compact_whitespace(str(item))
    }
    planned_semantic_groups = {
        compact_whitespace(str(item.get("payload", {}).get("groupId") or ""))
        for item in diffs
        if item.get("op") == "upsert_semantic_group" and isinstance(item.get("payload"), dict)
    }
    existing_semantic_groups = {
        compact_whitespace(str(item))
        for item in metadata.get("existingSemanticGroupIds", [])
        if compact_whitespace(str(item))
    }
    global_scope = (
        compact_whitespace(str(metadata.get("curationScope") or "")).lower()
        == "global"
    )
    global_catalog_audit = global_scope and bool(metadata.get("catalogAudit"))
    global_catalog_atoms = _list_of_dicts(metadata.get("globalCatalogAtoms"))
    global_catalog_atoms_by_id = {
        compact_whitespace(str(item.get("atomId") or item.get("id") or "")): item
        for item in global_catalog_atoms
        if compact_whitespace(str(item.get("atomId") or item.get("id") or ""))
    }
    global_catalog_tags = _list_of_dicts(metadata.get("globalCatalogTags"))
    global_catalog_books = _list_of_dicts(metadata.get("globalCatalogBooks"))
    global_catalog_books_by_id = {
        compact_whitespace(str(item.get("bookId") or "")): item
        for item in global_catalog_books
        if compact_whitespace(str(item.get("bookId") or ""))
    }
    global_book_merge_requested = any(
        str(diff.get("op") or "") == "merge_memory_books"
        for diff in diffs
    )
    global_book_merge_sources: set[str] = set()
    global_book_merge_targets: set[str] = set()
    global_atom_upsert_targets: set[str] = set()
    global_atom_pairs: list[tuple[str, str]] = []
    if global_scope and (
        compact_whitespace(str(metadata.get("runKind") or ""))
        != MEMORY_CATALOG_RUN_KIND
        or not global_catalog_audit
        or not bool(metadata.get("catalogComplete"))
        or not bool(metadata.get("catalogGraphValid", True))
        or not compact_whitespace(str(metadata.get("catalogDigest") or ""))
        or "globalCatalogAtoms" not in metadata
        or "globalCatalogTags" not in metadata
        or (global_book_merge_requested and "globalCatalogBooks" not in metadata)
    ):
        errors.append(_issue(0, "", "catalog", "global_catalog_snapshot_missing"))
    if global_scope and global_catalog_audit and not bool(metadata.get("catalogComplete", True)):
        errors.append(_issue(0, "", "catalog", "global_catalog_incomplete"))
    if compact_whitespace(str(plan.get("schemaVersion") or "")) != MEMORY_BOOK_RUN_SCHEMA_VERSION:
        errors.append(_issue(0, "", "schemaVersion", "unsupported_schema_version"))
    source_cursor = dict(metadata.get("sourceCursor") or {})
    no_change_review = (
        compact_whitespace(str(metadata.get("curationOutcome") or "")) == "no_changes"
        and compact_whitespace(str(metadata.get("curationArchitecture") or "")).startswith("atom-first")
    )
    if not diffs and int(source_cursor.get("pendingEventCount") or 0) > 0 and not no_change_review:
        errors.append(_issue(0, "", "diffs", "organizer_returned_no_governed_memory"))
    elif not diffs and no_change_review:
        warnings.append(_issue(0, "", "diffs", "curation_review_found_no_changes"))
    for index, diff in enumerate(diffs, start=1):
        op = compact_whitespace(str(diff.get("op") or ""))
        payload = diff.get("payload") if isinstance(diff.get("payload"), dict) else {}
        if global_scope:
            if op not in {
                "upsert_memory_atom",
                "supersede_memory",
                "merge_semantic_tag",
                "merge_memory_books",
            }:
                errors.append(_issue(index, op, "op", "global_catalog_disallowed_op"))
                continue
            if op == "upsert_memory_atom":
                counts["memoryAtoms"] += 1
                _validate_required_text(errors, index, op, payload, "atomId")
                _validate_required_text(errors, index, op, payload, "canonicalText")
                _validate_required_text(errors, index, op, payload, "claimKey")
                if compact_whitespace(str(payload.get("operation") or "")).lower() != "merge":
                    errors.append(_issue(index, op, "operation", "global_catalog_requires_merge"))
                atom_id = compact_whitespace(str(payload.get("atomId") or ""))
                existing = global_catalog_atoms_by_id.get(atom_id)
                if existing is None:
                    errors.append(_issue(index, op, "atomId", "global_catalog_atom_not_found"))
                elif not _global_catalog_atoms_equivalent(existing, payload):
                    errors.append(_issue(index, op, "canonicalText", "global_catalog_atom_not_equivalent"))
                if bool(payload.get("directCandidateAllowed")):
                    errors.append(
                        _issue(
                            index,
                            op,
                            "directCandidateAllowed",
                            "canonical_text_must_not_be_direct_candidate",
                        )
                    )
                global_atom_upsert_targets.add(atom_id)
            elif op == "supersede_memory":
                counts["supersedes"] += 1
                _validate_required_text(errors, index, op, payload, "oldId")
                _validate_required_text(errors, index, op, payload, "newId")
                old_id = compact_whitespace(str(payload.get("oldId") or ""))
                new_id = compact_whitespace(str(payload.get("newId") or ""))
                if (
                    old_id == new_id
                    or old_id not in global_catalog_atoms_by_id
                    or new_id not in global_catalog_atoms_by_id
                    or not _global_catalog_atoms_equivalent(
                        global_catalog_atoms_by_id.get(old_id),
                        global_catalog_atoms_by_id.get(new_id),
                    )
                ):
                    errors.append(_issue(index, op, "newId", "global_catalog_atom_not_equivalent"))
                global_atom_pairs.append((old_id, new_id))
            elif op == "merge_memory_books":
                counts["bookMerges"] += 1
                _validate_required_text(errors, index, op, payload, "targetBookId")
                _validate_required_text(errors, index, op, payload, "reason")
                source_ids = _memory_book_merge_sources(payload)
                if not source_ids:
                    errors.append(_issue(index, op, "sourceBookIds", "required"))
                target_id = compact_whitespace(str(payload.get("targetBookId") or ""))
                target = global_catalog_books_by_id.get(target_id)
                if (
                    target is None
                    or compact_whitespace(str(target.get("bookType") or "topic")) != "topic"
                    or compact_whitespace(str(target.get("status") or "")) not in {"active", "approved"}
                ):
                    errors.append(_issue(index, op, "targetBookId", "global_catalog_book_not_found"))
                if target_id in source_ids:
                    errors.append(_issue(index, op, "sourceBookIds", "book_merge_source_equals_target"))
                if len(source_ids) != len(set(source_ids)):
                    errors.append(_issue(index, op, "sourceBookIds", "duplicate_book_merge_source"))
                for source_id in source_ids:
                    source = global_catalog_books_by_id.get(source_id)
                    if source is None:
                        errors.append(_issue(index, op, "sourceBookIds", "global_catalog_book_not_found", preview=source_id))
                    elif (
                        compact_whitespace(str(source.get("bookType") or "topic")) != "topic"
                        or compact_whitespace(str(source.get("status") or "")) not in {"active", "approved"}
                    ):
                        errors.append(_issue(index, op, "sourceBookIds", "global_catalog_book_not_mergeable", preview=source_id))
                    elif target is not None and not _memory_books_same_scope(source, target):
                        errors.append(_issue(index, op, "sourceBookIds", "book_merge_scope_mismatch", preview=source_id))
                    if target is not None:
                        for member_id in _strings(
                            [
                                *(_strings(target.get("memoryAtomIds")) if target is not None else []),
                                *(_strings(source.get("memoryAtomIds")) if source is not None else []),
                            ]
                        ):
                            atom = global_catalog_atoms_by_id.get(member_id)
                            if atom is not None and not _memory_book_atom_scope_matches(target, atom):
                                errors.append(_issue(index, op, "sourceBookIds", "book_merge_member_scope_mismatch", preview=member_id))
                    if source_id in global_book_merge_sources:
                        errors.append(_issue(index, op, "sourceBookIds", "book_merge_source_reused", preview=source_id))
                    global_book_merge_sources.add(source_id)
                if target_id:
                    if target_id in global_book_merge_sources:
                        errors.append(_issue(index, op, "targetBookId", "book_merge_target_is_source", preview=target_id))
                    global_book_merge_targets.add(target_id)
                if _bounded_float(payload.get("confidence"), default=0.0) < 0.8:
                    errors.append(_issue(index, op, "confidence", "book_merge_confidence_too_low"))
                _validate_secret_free(errors, index, op, payload, ("reason",))
            else:
                counts["tagMerges"] += 1
                _validate_required_text(errors, index, op, payload, "source")
                _validate_required_text(errors, index, op, payload, "target")
                _validate_required_text(errors, index, op, payload, "reason")
                source_name = compact_whitespace(str(payload.get("source") or ""))
                target_name = compact_whitespace(str(payload.get("target") or ""))
                if not _global_catalog_tag_synonym(
                    source_name,
                    target_name,
                    source_tag_id=payload.get("sourceTagId"),
                    target_tag_id=payload.get("targetTagId"),
                    tags=global_catalog_tags,
                ):
                    errors.append(_issue(index, op, "target", "global_catalog_tag_not_exact_synonym"))
            continue
        if op == "upsert_semantic_group":
            counts["semanticGroups"] += 1
            _validate_required_text(errors, index, op, payload, "groupId")
            _validate_required_text(errors, index, op, payload, "title")
            _validate_required_text(errors, index, op, payload, "description")
            _validate_source_ids(errors, index, op, payload.get("sourceEventIds"), legal_source_event_ids=legal_source_event_ids)
            _validate_secret_free(errors, index, op, payload, ("title", "description", "aliases", "tags"))
        elif op == "upsert_semantic_tag":
            counts["semanticTags"] += 1
            _validate_required_text(errors, index, op, payload, "name")
            _validate_required_text(errors, index, op, payload, "description")
            _validate_source_ids(errors, index, op, payload.get("sourceEventIds"), legal_source_event_ids=legal_source_event_ids)
            _validate_secret_free(errors, index, op, payload, ("name", "description", "aliases"))
        elif op == "upsert_memory_book":
            counts["memoryBooks"] += 1
            _validate_required_text(errors, index, op, payload, "bookId")
            _validate_required_text(errors, index, op, payload, "bookKey")
            _validate_source_ids(errors, index, op, payload.get("sourceEventIds"), legal_source_event_ids=legal_source_event_ids)
            _validate_short_terms(errors, index, op, "surfaceHints", payload.get("surfaceHints"), max_len=16)
            _validate_secret_free(errors, index, op, payload, ("title", "summary", "tags", "topicAliases", "surfaceHints", "queryExpansions"))
        elif op == "upsert_memory_atom":
            counts["memoryAtoms"] += 1
            _validate_required_text(errors, index, op, payload, "atomId")
            _validate_required_text(errors, index, op, payload, "canonicalText")
            _validate_required_text(errors, index, op, payload, "claimKey")
            _validate_source_ids(errors, index, op, payload.get("sourceEventIds"), legal_source_event_ids=legal_source_event_ids)
            _validate_short_terms(errors, index, op, "surfaceHints", payload.get("surfaceHints"), max_len=16)
            _validate_secret_free(
                errors,
                index,
                op,
                payload,
                (
                    "canonicalText",
                    "summary",
                    "tags",
                    "aliases",
                    "surfaceHints",
                    "queryExpansions",
                    "claimKey",
                ),
            )
            if payload.get("claimState") not in {"current", "superseded", "retracted"}:
                errors.append(
                    _issue(index, op, "claimState", "unsupported_claim_state")
                )
            if bool(payload.get("directCandidateAllowed")):
                errors.append(_issue(index, op, "directCandidateAllowed", "canonical_text_must_not_be_direct_candidate"))
        elif op == "upsert_tag_edge":
            counts["tagEdges"] += 1
            _validate_required_text(errors, index, op, payload, "src")
            _validate_required_text(errors, index, op, payload, "dst")
            _validate_source_ids(
                errors,
                index,
                op,
                payload.get("evidenceEventIds"),
                field="evidenceEventIds",
                legal_source_event_ids=legal_source_event_ids,
            )
            _validate_secret_free(errors, index, op, payload, ("src", "dst"))
        elif op == "merge_semantic_tag":
            counts["tagMerges"] += 1
            _validate_required_text(errors, index, op, payload, "source")
            _validate_required_text(errors, index, op, payload, "target")
            _validate_required_text(errors, index, op, payload, "reason")
            _validate_source_ids(
                errors,
                index,
                op,
                payload.get("evidenceEventIds"),
                field="evidenceEventIds",
                legal_source_event_ids=legal_source_event_ids,
            )
            source_tag_id = _optional_int(payload.get("sourceTagId"))
            target_tag_id = _optional_int(payload.get("targetTagId"))
            if (
                normalize_text(str(payload.get("source") or ""))
                == normalize_text(str(payload.get("target") or ""))
                and (
                    source_tag_id <= 0
                    or target_tag_id <= 0
                    or source_tag_id == target_tag_id
                )
            ):
                errors.append(
                    _issue(index, op, "target", "tag_merge_source_equals_target")
                )
            _validate_secret_free(errors, index, op, payload, ("source", "target", "reason"))
        elif op == "add_phrase_candidate":
            counts["phraseCandidates"] += 1
            _validate_required_text(errors, index, op, payload, "text")
            _validate_source_ids(errors, index, op, payload.get("sourceEventIds"), legal_source_event_ids=legal_source_event_ids)
            text = compact_whitespace(str(payload.get("text") or ""))
            if len(text) < 2 or len(text) > 18:
                errors.append(_issue(index, op, "text", "phrase_text_length_out_of_range", value=len(text), preview=truncate_text(text, 80)))
            pinyin = compact_whitespace(str(payload.get("pinyin") or ""))
            if pinyin and not _RIME_PINYIN_RE.fullmatch(pinyin):
                errors.append(_issue(index, op, "pinyin", "invalid_rime_pinyin", preview=truncate_text(pinyin, 80)))
            _validate_secret_free(errors, index, op, payload, ("text", "tags"))
        elif op == "add_negative_phrase":
            counts["negativePhrases"] += 1
            _validate_required_text(errors, index, op, payload, "suppressionId")
            _validate_required_text(errors, index, op, payload, "text")
            _validate_source_ids(errors, index, op, payload.get("sourceEventIds"), legal_source_event_ids=legal_source_event_ids)
            _validate_secret_free(errors, index, op, payload, ("text", "reason"))
        elif op == "supersede_memory":
            counts["supersedes"] += 1
            _validate_required_text(errors, index, op, payload, "oldId")
            _validate_required_text(errors, index, op, payload, "newId")
            _validate_source_ids(errors, index, op, payload.get("sourceEventIds"), legal_source_event_ids=legal_source_event_ids)
        elif op == "retract_memory_atom":
            counts["retractions"] += 1
            _validate_required_text(errors, index, op, payload, "targetAtomId")
            _validate_required_text(errors, index, op, payload, "reason")
            _validate_source_ids(
                errors,
                index,
                op,
                payload.get("sourceEventIds"),
                legal_source_event_ids=legal_source_event_ids,
            )
            confidence = _bounded_float(payload.get("confidence"), default=0.0)
            if confidence < 0.9:
                errors.append(
                    _issue(index, op, "confidence", "retraction_confidence_too_low")
                )
            _validate_secret_free(errors, index, op, payload, ("reason",))
        else:
            errors.append(_issue(index, op, "op", "unsupported_op"))
        if op and _looks_like_long_history_sentence(payload):
            warnings.append(_issue(index, op, "payload", "looks_like_long_history_sentence"))
        group_id = compact_whitespace(str(payload.get("contextGroupId") or ""))
        if group_id and legal_groups and group_id not in legal_groups and group_id != "global":
            errors.append(_issue(index, op, "contextGroupId", "context_group_not_in_source_bundle"))
        for semantic_group_id in _strings(payload.get("semanticGroupIds")):
            if semantic_group_id not in planned_semantic_groups | existing_semantic_groups:
                errors.append(
                    _issue(index, op, "semanticGroupIds", "semantic_group_not_in_plan", preview=semantic_group_id)
                )
    if global_scope:
        paired_targets = {new_id for _old_id, new_id in global_atom_pairs}
        plan_supersedes: list[tuple[int, dict[str, object], str, str]] = []
        planned_upserts: dict[str, tuple[int, dict[str, object]]] = {}
        for index, diff in enumerate(diffs, start=1):
            payload = diff.get("payload")
            if diff.get("op") == "supersede_memory" and isinstance(payload, dict):
                plan_supersedes.append(
                    (
                        index,
                        diff,
                        compact_whitespace(str(payload.get("oldId") or "")),
                        compact_whitespace(str(payload.get("newId") or "")),
                    )
                )
            elif diff.get("op") == "upsert_memory_atom" and isinstance(payload, dict):
                atom_id = compact_whitespace(str(payload.get("atomId") or ""))
                planned_upserts[atom_id] = (index, diff)
                if not bool(payload.get("globalCatalogMerge")):
                    errors.append(
                        _issue(index, "upsert_memory_atom", "globalCatalogMerge", "global_catalog_merge_marker_missing")
                    )
                target = global_catalog_atoms_by_id.get(atom_id)
                source_ids = [
                    old_id
                    for old_id, new_id in global_atom_pairs
                    if new_id == atom_id
                ]
                source_atoms = [
                    global_catalog_atoms_by_id[source_id]
                    for source_id in source_ids
                    if source_id in global_catalog_atoms_by_id
                ]
                if target is not None and len(source_atoms) == len(source_ids):
                    expected = _catalog_atom_merge_payload(
                        target,
                        source_atoms,
                        target_id=atom_id,
                    )
                    for key in (
                        "aliases",
                        "surfaceHints",
                        "queryExpansions",
                        "sourceMemoryIds",
                        "tags",
                        "semanticGroupIds",
                    ):
                        if _strings(payload.get(key)) != _strings(expected.get(key)):
                            errors.append(
                                _issue(index, "upsert_memory_atom", key, "global_catalog_payload_not_lossless")
                            )
                    if _positive_ints(payload.get("sourceEventIds")) != _positive_ints(
                        expected.get("sourceEventIds")
                    ):
                        errors.append(
                            _issue(index, "upsert_memory_atom", "sourceEventIds", "global_catalog_payload_not_lossless")
                        )
                    if _strings(payload.get("mergeSourceIds")) != source_ids:
                        errors.append(
                            _issue(index, "upsert_memory_atom", "mergeSourceIds", "global_catalog_merge_sources_mismatch")
                        )
        graph_errors = _catalog_supersede_graph_errors(
            plan_supersedes,
            atoms_by_id=global_catalog_atoms_by_id,
            accepted_upserts=planned_upserts,
        )
        for graph_error in graph_errors:
            errors.append(
                _issue(
                    0,
                    "supersede_memory",
                    "catalogGraph",
                    "global_catalog_supersede_graph_invalid",
                    preview=graph_error,
                )
            )
        for old_id, new_id in sorted(global_atom_pairs):
            if new_id not in global_atom_upsert_targets:
                errors.append(
                    _issue(
                        0,
                        "supersede_memory",
                        "newId",
                        "global_catalog_atom_merge_missing_target_upsert",
                        preview=f"{old_id}->{new_id}",
                    )
                )
        for atom_id in sorted(global_atom_upsert_targets - paired_targets):
            errors.append(
                _issue(
                    0,
                    "upsert_memory_atom",
                    "atomId",
                    "global_catalog_atom_merge_missing_supersede",
                    preview=atom_id,
                )
            )
    return {
        "schemaVersion": MEMORY_BOOK_VALIDATE_SCHEMA_VERSION,
        "ok": not errors,
        "runId": str(plan.get("runId") or ""),
        "provider": str(plan.get("provider") or ""),
        "model": str(plan.get("model") or ""),
        "summary": str(plan.get("summary") or ""),
        "diffCount": len(diffs),
        "counts": counts,
        "errors": errors,
        "warnings": warnings,
    }


def _is_global_catalog_plan(plan: Mapping[str, object]) -> bool:
    metadata = dict(plan.get("metadata") or {})
    return compact_whitespace(str(metadata.get("curationScope") or "")).lower() == "global"


def _reserve_global_catalog_write(
    conn: sqlite3.Connection,
    plan: Mapping[str, object],
) -> None:
    """Acquire a RESERVED lock before taking a global catalog snapshot."""
    if not _is_global_catalog_plan(plan):
        return
    if conn.in_transaction:
        # A caller-owned deferred transaction cannot be upgraded with
        # ``BEGIN IMMEDIATE``. Any real-table UPDATE upgrades it to the same
        # RESERVED state without changing a value.
        project = compact_whitespace(
            str(dict(plan.get("metadata") or {}).get("project") or "")
        )
        conn.execute(
            """
            UPDATE memory_compile_state
            SET last_compiled_event_id = last_compiled_event_id
            WHERE project = ?
            """,
            (project,),
        )
    else:
        conn.execute("BEGIN IMMEDIATE")


def _current_global_catalog_digest(
    conn: sqlite3.Connection,
    *,
    project: str,
) -> str:
    current = build_memory_book_source_bundle(
        conn,
        project=project,
        curation_scope="global",
        catalog_only=True,
    )
    current_digest = compact_whitespace(str(current.get("catalogDigest") or ""))
    if not bool(current.get("catalogComplete")) or not current_digest:
        raise ValueError("global catalog digest changed")
    return current_digest


def _assert_global_catalog_plan_fresh(
    conn: sqlite3.Connection,
    plan: Mapping[str, object],
) -> str:
    metadata = dict(plan.get("metadata") or {})
    if not _is_global_catalog_plan(plan):
        return ""
    if compact_whitespace(str(metadata.get("runKind") or "")) != MEMORY_CATALOG_RUN_KIND:
        raise ValueError("global catalog run kind is invalid")
    frozen_digest = compact_whitespace(str(metadata.get("catalogDigest") or ""))
    project = compact_whitespace(str(metadata.get("project") or ""))
    current_digest = _current_global_catalog_digest(conn, project=project)
    if not frozen_digest or current_digest != frozen_digest:
        raise ValueError("global catalog digest changed")
    return current_digest


def seal_global_memory_book_plan(
    conn: sqlite3.Connection,
    plan: Mapping[str, object],
) -> str:
    """Reserve and verify a global catalog plan, leaving the lock held.

    Callers that do not already own a transaction must commit or roll back the
    transaction opened here. Store/apply paths invoke this inside their
    transaction so the reservation covers the complete write.
    """
    if not _is_global_catalog_plan(plan):
        return ""
    _reserve_global_catalog_write(conn, plan)
    return _assert_global_catalog_plan_fresh(conn, plan)


def apply_memory_book_plan(conn: sqlite3.Connection, plan: dict[str, object]) -> dict[str, object]:
    validation = inspect_memory_book_plan(plan)
    if not validation.get("ok"):
        raise ValueError("memory book plan failed validation")
    run_id = compact_whitespace(str(plan.get("runId") or ""))
    if not run_id:
        raise ValueError("memory book runId is required")
    sealed_catalog_digest = ""
    with conn:
        sealed_catalog_digest = seal_global_memory_book_plan(conn, plan)
        _persist_memory_book_run(conn, plan)
        rows = conn.execute(
            """
            SELECT id, op, target_memory_id, payload_json
            FROM memory_cleanup_diffs
            WHERE run_id = ? AND status IN ('pending', 'approved')
            ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()
        affected_book_ids: set[str] = set()
        for row in rows:
            rollback = _apply_memory_book_diff(conn, row=row)
            if str(row["op"]) in {"upsert_memory_book", "merge_memory_books"}:
                resolved_book_id = compact_whitespace(
                    str(
                        rollback.get("resolvedBookId")
                        or rollback.get("targetId")
                        or rollback.get("pkValue")
                        or row["target_memory_id"]
                        or ""
                    )
                )
                if resolved_book_id:
                    affected_book_ids.add(resolved_book_id)
            conn.execute(
                """
                UPDATE memory_cleanup_diffs
                SET status = 'applied', applied_at_ms = ?, rollback_json = ?
                WHERE id = ?
                """,
                (now_ms(), json.dumps(rollback, ensure_ascii=False, sort_keys=True), int(row["id"])),
            )
        for book_id in sorted(affected_book_ids):
            _reconcile_memory_book_current_state(conn, book_id=book_id)
        _sync_run_status(conn, run_id)
        if rows:
            _advance_compile_state(conn, plan=plan)
            enqueue_memory_projection(
                conn,
                projection_kind=RETRIEVAL_DOCS_PROJECTION,
                aggregate_type="memory_book_run",
                aggregate_id=run_id,
                operation="apply",
                project=compact_whitespace(
                    str(dict(plan.get("metadata") or {}).get("project") or "")
                ),
                payload={"runId": run_id, "diffCount": len(rows)},
            )
        if sealed_catalog_digest:
            sealed_catalog_digest = _current_global_catalog_digest(
                conn,
                project=compact_whitespace(
                    str(dict(plan.get("metadata") or {}).get("project") or "")
                ),
            )
    result = memory_book_run_payload(conn, run_id=run_id)
    if sealed_catalog_digest:
        result["sealedCatalogDigest"] = sealed_catalog_digest
    return result


def find_memory_book_draft_for_bundle(
    conn: sqlite3.Connection,
    *,
    project: str,
    bundle_hash: str,
) -> dict[str, object] | None:
    """Return the newest review draft for one exact source bundle.

    Scheduled maintenance must not spend another model call every hour while
    the same evidence is still waiting for human review.
    """

    normalized_project = compact_whitespace(project)
    normalized_hash = compact_whitespace(bundle_hash)
    if not normalized_hash:
        return None
    rows = conn.execute(
        """
        SELECT run_id, metadata_json
        FROM memory_cleanup_runs
        WHERE status = 'draft' AND run_id LIKE 'memory_book_%'
        ORDER BY created_at_ms DESC, id DESC
        """
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        if compact_whitespace(str(metadata.get("project") or "")) != normalized_project:
            continue
        if compact_whitespace(str(metadata.get("bundleHash") or "")) != normalized_hash:
            continue
        run = memory_book_run_payload(conn, run_id=str(row["run_id"]))
        if memory_book_run_is_stale(conn, run=run):
            continue
        return run
    return None


def memory_book_run_is_stale(conn: sqlite3.Connection, *, run: Mapping[str, object]) -> bool:
    """Return whether a draft's evidence cursor was already consumed by a newer apply."""

    if compact_whitespace(str(run.get("status") or "")) != "draft":
        return False
    metadata = dict(run.get("metadata") or {})
    if compact_whitespace(str(metadata.get("runKind") or "legacy")) != "legacy":
        return False
    project = compact_whitespace(str(metadata.get("project") or ""))
    source_cursor = dict(metadata.get("sourceCursor") or {})
    try:
        to_event_id = int(source_cursor.get("toEventId") or 0)
    except (TypeError, ValueError):
        return False
    if not project or to_event_id <= 0:
        return False
    state = memory_compile_state(conn, project=project)
    return int(state.get("lastCompiledEventId") or 0) >= to_event_id


def memory_book_plan_from_stored_run(run: Mapping[str, object]) -> dict[str, object]:
    """Rebuild a validated plan payload from a review draft."""

    run_id = compact_whitespace(str(run.get("runId") or ""))
    if not run_id:
        raise ValueError("stored memory book runId is required")
    return {
        "schemaVersion": MEMORY_BOOK_RUN_SCHEMA_VERSION,
        "runId": run_id,
        "provider": str(run.get("provider") or ""),
        "model": str(run.get("model") or ""),
        "summary": str(run.get("summary") or ""),
        "metadata": dict(run.get("metadata") or {}),
        "diffs": [
            {
                "op": str(item.get("op") or ""),
                "targetId": str(item.get("targetId") or ""),
                "payload": dict(item.get("payload") or {}),
                "status": str(item.get("status") or "pending"),
            }
            for item in _list_of_dicts(run.get("diffs"))
        ],
    }


def store_memory_book_plan(
    conn: sqlite3.Connection,
    plan: dict[str, object],
    *,
    supersede_project_drafts: bool = False,
) -> dict[str, object]:
    """Persist a validated compiler plan without applying proposed memory changes."""
    validation = inspect_memory_book_plan(plan)
    if not validation.get("ok"):
        raise ValueError("memory book plan failed validation")
    run_id = compact_whitespace(str(plan.get("runId") or ""))
    if not run_id:
        raise ValueError("memory book runId is required")
    sealed_catalog_digest = ""
    with conn:
        # This runs for no-diff plans too: a clean empty review must not be
        # able to acknowledge a catalog snapshot that changed after drafting.
        sealed_catalog_digest = seal_global_memory_book_plan(conn, plan)
        if supersede_project_drafts:
            _supersede_project_memory_book_drafts(conn, plan=plan)
        _persist_memory_book_run(conn, plan)
        if _list_of_dicts(plan.get("diffs")):
            _record_draft_compile_state(conn, plan=plan)
        else:
            # A no-change Atom review is already complete: it mutates no user
            # memory and therefore needs no approval, but its evidence cursor
            # must advance or the scheduler would call the model forever.
            _sync_run_status(conn, run_id)
            _advance_compile_state(conn, plan=plan)
        if sealed_catalog_digest:
            sealed_catalog_digest = _current_global_catalog_digest(
                conn,
                project=compact_whitespace(
                    str(dict(plan.get("metadata") or {}).get("project") or "")
                ),
            )
    result = memory_book_run_payload(conn, run_id=run_id)
    if sealed_catalog_digest:
        result["sealedCatalogDigest"] = sealed_catalog_digest
    return result


def _supersede_project_memory_book_drafts(
    conn: sqlite3.Connection,
    *,
    plan: Mapping[str, object],
) -> None:
    run_id = compact_whitespace(str(plan.get("runId") or ""))
    metadata = dict(plan.get("metadata") or {})
    project = compact_whitespace(str(metadata.get("project") or ""))
    current_run_kind = compact_whitespace(str(metadata.get("runKind") or "")) or "legacy"
    owner_kind, owner_id = _memory_owner(
        metadata.get("ownerKind") or "user",
        metadata.get("ownerId") or "default",
    )
    if not project:
        return
    rows = conn.execute(
        """
        SELECT run_id, metadata_json
        FROM memory_cleanup_runs
        WHERE status = 'draft' AND run_id LIKE 'memory_book_%' AND run_id != ?
          AND owner_kind = ? AND owner_id = ?
        """,
        (run_id, owner_kind, owner_id),
    ).fetchall()
    superseded: list[str] = []
    for row in rows:
        try:
            previous_metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(previous_metadata, dict):
            continue
        previous_run_kind = compact_whitespace(
            str(previous_metadata.get("runKind") or "")
        ) or "legacy"
        if (
            compact_whitespace(str(previous_metadata.get("project") or "")) == project
            and previous_run_kind == current_run_kind
        ):
            superseded.append(str(row["run_id"]))
    for previous_run_id in superseded:
        conn.execute(
            "UPDATE memory_cleanup_runs SET status = 'superseded' WHERE run_id = ?",
            (previous_run_id,),
        )
        conn.execute(
            """
            UPDATE memory_cleanup_diffs
            SET status = 'rejected'
            WHERE run_id = ? AND status IN ('pending', 'approved')
            """,
            (previous_run_id,),
        )


def update_stored_memory_book_diff(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    diff_id: int,
    payload: dict[str, object] | None = None,
    selected: bool = True,
) -> dict[str, object]:
    """Edit or include/exclude one draft operation before the atomic apply step."""
    current = memory_book_run_payload(conn, run_id=run_id)
    if not current.get("provider"):
        raise ValueError(f"memory book run not found: {run_id}")
    if str(current.get("status") or "") != "draft":
        raise ValueError("only draft memory book runs can be edited")
    row = conn.execute(
        """
        SELECT id, op, target_memory_id, payload_json, status
        FROM memory_cleanup_diffs
        WHERE run_id = ? AND id = ?
        LIMIT 1
        """,
        (run_id, int(diff_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"memory book diff not found: {diff_id}")
    if str(row["status"] or "") not in {"pending", "approved", "rejected"}:
        raise ValueError("applied memory book diffs cannot be edited")

    next_payload = dict(payload) if payload is not None else json.loads(row["payload_json"] or "{}")
    next_status = "approved" if selected else "rejected"
    if selected:
        metadata = dict(current.get("metadata") or {})
        selected_diffs: list[dict[str, object]] = []
        for item in current.get("diffs") or []:
            if not isinstance(item, dict):
                continue
            item_id = int(item.get("diffId") or 0)
            item_status = next_status if item_id == int(diff_id) else str(item.get("status") or "pending")
            if item_status == "rejected":
                continue
            selected_diffs.append(
                {
                    "op": str(item.get("op") or ""),
                    "targetId": str(item.get("targetId") or ""),
                    "payload": next_payload if item_id == int(diff_id) else dict(item.get("payload") or {}),
                    "status": item_status,
                }
            )
        validation = inspect_memory_book_plan(
            {
                "schemaVersion": MEMORY_BOOK_RUN_SCHEMA_VERSION,
                "runId": run_id,
                "provider": str(current.get("provider") or ""),
                "model": str(current.get("model") or ""),
                "summary": str(current.get("summary") or ""),
                "metadata": metadata,
                "diffs": selected_diffs,
            }
        )
        if not validation.get("ok"):
            first_error = next(iter(validation.get("errors") or []), {})
            raise ValueError(f"draft edit failed validation: {first_error.get('reason') or 'invalid payload'}")

    with conn:
        conn.execute(
            """
            UPDATE memory_cleanup_diffs
            SET payload_json = ?, status = ?
            WHERE run_id = ? AND id = ?
            """,
            (json.dumps(next_payload, ensure_ascii=False, sort_keys=True), next_status, run_id, int(diff_id)),
        )
    return memory_book_run_payload(conn, run_id=run_id)


def apply_stored_memory_book_run(conn: sqlite3.Connection, *, run_id: str) -> dict[str, object]:
    """Apply a previously reviewed draft run without regenerating it."""
    current = memory_book_run_payload(conn, run_id=run_id)
    if not current.get("provider"):
        raise ValueError(f"memory book run not found: {run_id}")
    status = compact_whitespace(str(current.get("status") or ""))
    if status not in {"draft", "partial"}:
        raise ValueError(f"memory book run is not reviewable: {run_id} ({status or 'unknown'})")
    stored_plan = memory_book_plan_from_stored_run(current)
    sealed_catalog_digest = ""
    with conn:
        # Global freshness must be checked after reserving the writer slot,
        # otherwise another catalog mutation can land between the check and
        # the first applied diff.
        sealed_catalog_digest = seal_global_memory_book_plan(conn, stored_plan)
        if memory_book_run_is_stale(conn, run=current):
            raise ValueError(f"memory book draft is stale: {run_id}")
        rows = conn.execute(
            """
            SELECT id, op, target_memory_id, payload_json
            FROM memory_cleanup_diffs
            WHERE run_id = ? AND status IN ('pending', 'approved')
            ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()
        affected_book_ids: set[str] = set()
        for row in rows:
            rollback = _apply_memory_book_diff(conn, row=row)
            if str(row["op"]) in {"upsert_memory_book", "merge_memory_books"}:
                resolved_book_id = compact_whitespace(
                    str(
                        rollback.get("resolvedBookId")
                        or rollback.get("targetId")
                        or rollback.get("pkValue")
                        or row["target_memory_id"]
                        or ""
                    )
                )
                if resolved_book_id:
                    affected_book_ids.add(resolved_book_id)
            conn.execute(
                """
                UPDATE memory_cleanup_diffs
                SET status = 'applied', applied_at_ms = ?, rollback_json = ?
                WHERE id = ?
                """,
                (now_ms(), json.dumps(rollback, ensure_ascii=False, sort_keys=True), int(row["id"])),
            )
        for book_id in sorted(affected_book_ids):
            _reconcile_memory_book_current_state(conn, book_id=book_id)
        resolved_status = _sync_run_status(conn, run_id)
        _advance_compile_state(
            conn,
            plan={
                "runId": run_id,
                "metadata": dict(current.get("metadata") or {}),
            },
        )
        if rows:
            _transition_owner_curation_sources(
                conn,
                run=current,
                applied=True,
            )
            enqueue_memory_projection(
                conn,
                projection_kind=RETRIEVAL_DOCS_PROJECTION,
                aggregate_type="memory_book_run",
                aggregate_id=run_id,
                operation="apply",
                project=compact_whitespace(
                    str(dict(current.get("metadata") or {}).get("project") or "")
                ),
                payload={"runId": run_id, "diffCount": len(rows)},
            )
        elif resolved_status == "empty":
            _resolve_owner_curation_review_without_writes(
                conn,
                run=current,
            )
        project_personal_memory_books(conn)
        if sealed_catalog_digest:
            sealed_catalog_digest = _current_global_catalog_digest(
                conn,
                project=compact_whitespace(
                    str(dict(current.get("metadata") or {}).get("project") or "")
                ),
            )
    result = memory_book_run_payload(conn, run_id=run_id)
    if sealed_catalog_digest:
        result["sealedCatalogDigest"] = sealed_catalog_digest
    return result


def rollback_memory_book_run(conn: sqlite3.Connection, *, run_id: str) -> dict[str, object]:
    current = memory_book_run_payload(conn, run_id=run_id)
    if not current.get("provider"):
        raise ValueError(f"memory book run not found: {run_id}")
    status = compact_whitespace(str(current.get("status") or ""))
    if status not in {"applied", "partial"}:
        raise ValueError(f"memory book run is not rollbackable: {run_id} ({status or 'unknown'})")
    newer_run = find_newer_applied_memory_book_run(conn, run_id=run_id)
    if newer_run is not None:
        raise ValueError(
            "memory book run is not rollbackable after a newer applied run: "
            f"{run_id} -> {newer_run['runId']}"
        )
    with conn:
        rows = conn.execute(
            """
            SELECT id, op, rollback_json, status
            FROM memory_cleanup_diffs
            WHERE run_id = ? AND status = 'applied'
            ORDER BY id DESC
            """,
            (run_id,),
        ).fetchall()
        for row in rows:
            _rollback_memory_book_diff(conn, row=row)
            conn.execute(
                "UPDATE memory_cleanup_diffs SET status = 'rolled_back' WHERE id = ?",
                (int(row["id"]),),
            )
        _sync_run_status(conn, run_id)
        if rows:
            _transition_owner_curation_sources(
                conn,
                run=current,
                applied=False,
            )
            rollback_evidence_admissions_for_run(
                conn,
                run_id=run_id,
                created_at_ms=now_ms(),
            )
            # Evidence rollback may rewind the owner cursor as part of its
            # compatibility projection. Restore the frozen pre-run cursor
            # last so replay sees the exact same source bundle and can reuse
            # the content-addressed model result.
            _restore_owner_curation_cursor(conn, run=current)
            enqueue_memory_projection(
                conn,
                projection_kind=RETRIEVAL_DOCS_PROJECTION,
                aggregate_type="memory_book_run",
                aggregate_id=run_id,
                operation="rollback",
                project=compact_whitespace(
                    str(dict(current.get("metadata") or {}).get("project") or "")
                ),
                payload={"runId": run_id, "diffCount": len(rows)},
            )
        project_personal_memory_books(conn)
    return memory_book_run_payload(conn, run_id=run_id)


def _transition_owner_curation_sources(
    conn: sqlite3.Connection,
    *,
    run: dict[str, object],
    applied: bool,
) -> None:
    run_kind = compact_whitespace(str(run.get("runKind") or ""))
    if run_kind not in {"daily_curation", "manual_curation"}:
        return
    run_id = compact_whitespace(str(run.get("runId") or ""))
    owner_kind = compact_whitespace(str(run.get("ownerKind") or ""))
    owner_id = compact_whitespace(str(run.get("ownerId") or ""))
    metadata = dict(run.get("metadata") or {})
    # One reconstructed Rime utterance can cover thousands of low-level
    # commits. Keep every physical evidence id so apply/rollback cannot leave
    # the tail silently stranded behind the curation cursor.
    source_ids = _unique_strings(metadata.get("sourceIds") or [], limit=50_000)
    if not run_id or not source_ids:
        return
    previous_disposition = "remember" if applied else "consolidated"
    new_disposition = "consolidated" if applied else "remember"
    reason_code = "curation_applied" if applied else "curation_rolled_back"
    actor_kind = "system" if applied else "rollback"
    rows = conn.execute(
        """
        WITH selected_source_ids(source_id) AS (
            SELECT DISTINCT CAST(value AS TEXT)
            FROM json_each(?)
        )
        SELECT source.source_id AS source_id,
               source.created_at_ms AS created_at_ms
        FROM agent_memory_sources AS source
        JOIN selected_source_ids AS selected
          ON selected.source_id = source.source_id
        WHERE source.owner_kind = ? AND source.owner_id = ?
          AND source.curation_run_id = ?
          AND source.disposition = ?
        ORDER BY source.created_at_ms, source.source_id
        """,
        (
            json.dumps(source_ids, ensure_ascii=False, separators=(",", ":")),
            owner_kind,
            owner_id,
            run_id,
            previous_disposition,
        ),
    ).fetchall()
    timestamp = now_ms()
    for row in rows:
        source_id = str(row["source_id"])
        conn.execute(
            """
            UPDATE agent_memory_sources
            SET disposition = ?, disposition_reason = ?,
                disposition_updated_at_ms = ?, processed_at_ms = ?
            WHERE source_id = ?
            """,
            (
                new_disposition,
                reason_code,
                timestamp,
                timestamp,
                source_id,
            ),
        )
        event_id = (
            "memory-disposition:"
            + stable_text_hash(
                f"{run_id}\0{source_id}\0{new_disposition}"
            ).removeprefix("sha256:")[:40]
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO memory_source_disposition_events(
                event_id, source_id, previous_disposition, new_disposition,
                reason_code, actor_kind, run_id, created_at_ms, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                source_id,
                previous_disposition,
                new_disposition,
                reason_code,
                actor_kind,
                run_id,
                timestamp,
                json.dumps(
                    {"source": "memory_book_apply", "runKind": run_kind},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
    project = compact_whitespace(str(metadata.get("project") or ""))
    if applied:
        conn.execute(
            """
            UPDATE memory_curation_cursors
            SET status = 'idle', last_error = '', updated_at_ms = ?
            WHERE owner_kind = ? AND owner_id = ? AND project = ?
              AND lane = 'daily' AND last_run_id = ?
            """,
            (timestamp, owner_kind, owner_id, project, run_id),
        )


def _restore_owner_curation_cursor(
    conn: sqlite3.Connection,
    *,
    run: dict[str, object],
) -> None:
    """Restore the exact pre-run cursor after all rollback projections."""

    run_kind = compact_whitespace(str(run.get("runKind") or ""))
    if run_kind not in {"daily_curation", "manual_curation"}:
        return
    run_id = compact_whitespace(str(run.get("runId") or ""))
    owner_kind = compact_whitespace(str(run.get("ownerKind") or ""))
    owner_id = compact_whitespace(str(run.get("ownerId") or ""))
    metadata = dict(run.get("metadata") or {})
    project = compact_whitespace(str(metadata.get("project") or ""))
    source_cursor = dict(metadata.get("sourceCursor") or {})
    has_exact_cursor = (
        "fromSourceCreatedAtMs" in source_cursor
        and "fromSourceId" in source_cursor
    )
    try:
        restored_cursor_ms = max(
            0,
            int(source_cursor.get("fromSourceCreatedAtMs") or 0),
        )
    except (TypeError, ValueError):
        has_exact_cursor = False
        restored_cursor_ms = 0
    restored_cursor_id = compact_whitespace(
        str(source_cursor.get("fromSourceId") or "")
    )
    if not has_exact_cursor:
        # Compatibility fallback for runs stored before sourceCursor was
        # recorded. Resolve all physical source ids without relying on their
        # post-rollback disposition.
        source_ids = _unique_strings(
            metadata.get("sourceIds") or [],
            limit=50_000,
        )
        if not source_ids:
            return
        row = conn.execute(
            """
            WITH selected_source_ids(source_id) AS (
                SELECT DISTINCT CAST(value AS TEXT)
                FROM json_each(?)
            )
            SELECT MIN(source.created_at_ms) AS earliest_ms
            FROM agent_memory_sources AS source
            JOIN selected_source_ids AS selected
              ON selected.source_id = source.source_id
            WHERE source.owner_kind = ? AND source.owner_id = ?
            """,
            (
                json.dumps(
                    source_ids,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                owner_kind,
                owner_id,
            ),
        ).fetchone()
        if row is None or row["earliest_ms"] is None:
            return
        restored_cursor_ms = max(0, int(row["earliest_ms"] or 0) - 1)
        restored_cursor_id = ""
    if not run_id or not owner_kind or not owner_id:
        return
    timestamp = now_ms()
    conn.execute(
        """
        UPDATE memory_curation_cursors
        SET last_source_created_at_ms = ?,
            last_source_id = ?,
            next_due_at_ms = 0,
            status = 'idle',
            last_error = '',
            updated_at_ms = ?
        WHERE owner_kind = ? AND owner_id = ? AND project = ?
          AND lane = 'daily' AND last_run_id = ?
        """,
        (
            restored_cursor_ms,
            restored_cursor_id,
            timestamp,
            owner_kind,
            owner_id,
            project,
            run_id,
        ),
    )


def _resolve_owner_curation_review_without_writes(
    conn: sqlite3.Connection,
    *,
    run: dict[str, object],
) -> None:
    """Close an owner review when every proposed semantic write was rejected.

    The raw evidence remains in its prior disposition and audit history. Only
    the review draft and cursor are resolved, so a rejected synthesis is not
    silently reinterpreted as a request to forget its source evidence.
    """
    run_kind = compact_whitespace(str(run.get("runKind") or ""))
    if run_kind not in {"daily_curation", "manual_curation"}:
        return
    run_id = compact_whitespace(str(run.get("runId") or ""))
    owner_kind = compact_whitespace(str(run.get("ownerKind") or ""))
    owner_id = compact_whitespace(str(run.get("ownerId") or ""))
    metadata = dict(run.get("metadata") or {})
    project = compact_whitespace(str(metadata.get("project") or ""))
    if not run_id or not owner_kind or not owner_id:
        return
    metadata.update(
        {
            "curationOutcome": "review_rejected_all_writes",
            "reviewResolvedAtMs": now_ms(),
        }
    )
    timestamp = int(metadata["reviewResolvedAtMs"])
    conn.execute(
        """
        UPDATE memory_cleanup_runs
        SET summary = ?, metadata_json = ?
        WHERE run_id = ? AND status = 'empty'
        """,
        (
            "审阅完成：未应用任何长期记忆变更",
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            run_id,
        ),
    )
    conn.execute(
        """
        UPDATE memory_curation_cursors
        SET status = 'idle', last_error = '', updated_at_ms = ?
        WHERE owner_kind = ? AND owner_id = ? AND project = ?
          AND lane = 'daily' AND last_run_id = ?
        """,
        (timestamp, owner_kind, owner_id, project, run_id),
    )


def memory_book_run_payload(conn: sqlite3.Connection, *, run_id: str) -> dict[str, object]:
    run = conn.execute(
        """
        SELECT run_id, created_at_ms, provider, model, status, summary, metadata_json,
               owner_kind, owner_id, run_kind
        FROM memory_cleanup_runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    diffs = conn.execute(
        """
        SELECT id, op, target_memory_id, payload_json, status, created_at_ms, applied_at_ms
        FROM memory_cleanup_diffs
        WHERE run_id = ?
        ORDER BY id ASC
        """,
        (run_id,),
    ).fetchall()
    return {
        "runId": run_id,
        "createdAtMs": 0 if run is None else int(run["created_at_ms"] or 0),
        "status": "" if run is None else str(run["status"]),
        "provider": "" if run is None else str(run["provider"]),
        "model": "" if run is None else str(run["model"]),
        "summary": "" if run is None else str(run["summary"]),
        "ownerKind": "" if run is None else str(run["owner_kind"]),
        "ownerId": "" if run is None else str(run["owner_id"]),
        "runKind": "" if run is None else str(run["run_kind"]),
        "metadata": {} if run is None else json.loads(run["metadata_json"] or "{}"),
        "diffs": [
            {
                "diffId": int(row["id"]),
                "op": str(row["op"]),
                "targetId": str(row["target_memory_id"]),
                "payload": json.loads(row["payload_json"] or "{}"),
                "status": str(row["status"]),
                "createdAtMs": int(row["created_at_ms"] or 0),
                "appliedAtMs": int(row["applied_at_ms"] or 0),
            }
            for row in diffs
        ],
    }


def find_newer_applied_memory_book_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> dict[str, object] | None:
    """Return a later active apply that makes this run unsafe to roll back.

    Each diff stores the database state that existed immediately before its
    own apply. Replaying an older rollback after a newer apply would therefore
    overwrite the newer memory state. Runs must unwind in reverse apply order.
    """

    current = conn.execute(
        "SELECT id, metadata_json, owner_kind, owner_id FROM memory_cleanup_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if current is None:
        return None
    try:
        current_metadata = json.loads(current["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        current_metadata = {}
    project = compact_whitespace(
        str(current_metadata.get("project") or "") if isinstance(current_metadata, dict) else ""
    )
    rows = conn.execute(
        """
        SELECT id, run_id, created_at_ms, status, metadata_json
        FROM memory_cleanup_runs
        WHERE id > ? AND status IN ('applied', 'partial') AND run_id LIKE 'memory_book_%'
          AND owner_kind = ? AND owner_id = ?
        ORDER BY id DESC
        """,
        (int(current["id"]), str(current["owner_kind"]), str(current["owner_id"])),
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        if compact_whitespace(str(metadata.get("project") or "")) != project:
            continue
        return {
            "runId": str(row["run_id"]),
            "createdAtMs": int(row["created_at_ms"] or 0),
            "status": str(row["status"]),
        }
    return None


def _persist_memory_book_run(conn: sqlite3.Connection, plan: dict[str, object]) -> None:
    run_id = compact_whitespace(str(plan.get("runId") or ""))
    created_at = now_ms()
    metadata = dict(plan.get("metadata") or {})
    owner_kind, owner_id = _memory_owner(
        metadata.get("ownerKind") or "user",
        metadata.get("ownerId") or "default",
    )
    run_kind = compact_whitespace(str(metadata.get("runKind") or "legacy"))
    if run_kind not in {
        "legacy",
        "daily_curation",
        "manual_curation",
        "dream_insight",
        MEMORY_CATALOG_RUN_KIND,
    }:
        raise ValueError("unsupported memory curation run kind")
    conn.execute(
        """
        INSERT OR REPLACE INTO memory_cleanup_runs(
            run_id, created_at_ms, provider, model, status, summary, metadata_json,
            owner_kind, owner_id, run_kind
        )
        VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            created_at,
            str(plan.get("provider") or ""),
            str(plan.get("model") or ""),
            str(plan.get("summary") or ""),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            owner_kind,
            owner_id,
            run_kind,
        ),
    )
    conn.execute("DELETE FROM memory_cleanup_diffs WHERE run_id = ?", (run_id,))
    for diff in _list_of_dicts(plan.get("diffs")):
        conn.execute(
            """
            INSERT INTO memory_cleanup_diffs(run_id, op, target_memory_id, payload_json, status, created_at_ms, rollback_json)
            VALUES (?, ?, ?, ?, ?, ?, '{}')
            """,
            (
                run_id,
                str(diff.get("op") or ""),
                str(diff.get("targetId") or diff.get("targetMemoryId") or ""),
                json.dumps(dict(diff.get("payload") or {}), ensure_ascii=False, sort_keys=True),
                str(diff.get("status") or "pending"),
                created_at,
            ),
        )


def _apply_memory_book_diff(conn: sqlite3.Connection, *, row: sqlite3.Row) -> dict[str, object]:
    op = str(row["op"])
    payload = json.loads(row["payload_json"] or "{}")
    if op == "upsert_semantic_group":
        return _apply_semantic_group(conn, payload)
    if op == "upsert_semantic_tag":
        return _apply_semantic_tag(conn, payload)
    if op == "upsert_memory_book":
        return _apply_memory_book(conn, payload)
    if op == "merge_memory_books":
        return _apply_memory_book_merge(conn, payload)
    if op == "upsert_memory_atom":
        return _apply_memory_atom(conn, payload)
    if op == "upsert_tag_edge":
        return _apply_tag_edge(conn, payload)
    if op == "merge_semantic_tag":
        return _apply_semantic_tag_merge(conn, payload)
    if op == "add_phrase_candidate":
        return _apply_phrase_candidate(conn, payload)
    if op == "add_negative_phrase":
        return _apply_negative_phrase(conn, payload)
    if op == "supersede_memory":
        return _apply_supersede_memory(conn, payload)
    if op == "retract_memory_atom":
        return _apply_retract_memory_atom(conn, payload)
    raise ValueError(f"unsupported memory book op: {op}")


def _rollback_memory_book_diff(conn: sqlite3.Connection, *, row: sqlite3.Row) -> None:
    op = str(row["op"])
    rollback = json.loads(row["rollback_json"] or "{}")
    if op == "upsert_semantic_group":
        _restore_or_delete_row(conn, table="memory_semantic_groups", pk="group_id", rollback=rollback)
    elif op == "upsert_semantic_tag":
        _restore_or_delete_row(conn, table="memory_tags", pk="id", rollback=rollback)
        _restore_or_delete_row(conn, table="memory_tag_profiles", pk="tag_id", rollback=dict(rollback.get("profile") or {}))
        _restore_semantic_group_members(conn, rollback)
    elif op == "upsert_memory_book":
        _restore_or_delete_row(conn, table="memory_books", pk="book_id", rollback=rollback)
        _restore_semantic_group_members(conn, rollback)
    elif op == "merge_memory_books":
        _rollback_memory_book_merge(conn, rollback)
    elif op == "upsert_memory_atom":
        _restore_or_delete_row(conn, table="memory_atoms", pk="id", rollback=rollback)
        for old_row in rollback.get("autoSuperseded", []) or []:
            if isinstance(old_row, dict):
                _insert_or_replace_dict(conn, "memory_atoms", old_row)
        for old_book in rollback.get("autoSupersededBooks", []) or []:
            if isinstance(old_book, dict):
                _insert_or_replace_dict(conn, "memory_books", old_book)
        dependency_rollback = rollback.get("dependencyInvalidation")
        if isinstance(dependency_rollback, dict):
            restore_dependency_invalidation(conn, dependency_rollback)
        for relation in rollback.get("supersessionRollbacks", []) or []:
            if isinstance(relation, dict):
                _restore_or_delete_row(
                    conn,
                    table="memory_supersessions",
                    pk="supersession_id",
                    rollback=relation,
                )
        _restore_semantic_group_members(conn, rollback)
        if rollback.get("previous"):
            atom_id = compact_whitespace(str(rollback.get("pkValue") or ""))
            conn.execute("DELETE FROM memory_aliases WHERE memory_atom_id = ?", (atom_id,))
            conn.execute("DELETE FROM memory_atom_tags WHERE memory_atom_id = ?", (atom_id,))
            for alias in rollback.get("previousAliases", []) or []:
                if isinstance(alias, dict):
                    _insert_or_replace_dict(conn, "memory_aliases", alias)
            for tag in rollback.get("previousAtomTags", []) or []:
                if isinstance(tag, dict):
                    _insert_or_replace_dict(conn, "memory_atom_tags", tag)
        _rollback_personal_atom_evidence_governance(
            conn,
            rollback.get("personalEvidenceGovernance"),
        )
    elif op == "upsert_tag_edge":
        edge = rollback.get("edge")
        if isinstance(edge, dict):
            previous = rollback.get("previous")
            if isinstance(previous, dict) and previous:
                columns = list(previous.keys())
                conn.execute(
                    f"INSERT OR REPLACE INTO memory_tag_edges({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    [previous[column] for column in columns],
                )
            else:
                conn.execute(
                    "DELETE FROM memory_tag_edges WHERE src_tag_id = ? AND dst_tag_id = ? AND edge_type = ?",
                    (
                        int(edge.get("srcTagId") or 0),
                        int(edge.get("dstTagId") or 0),
                        str(edge.get("edgeType") or ""),
                    ),
                )
    elif op == "merge_semantic_tag":
        _rollback_semantic_tag_merge(conn, rollback)
    elif op == "add_phrase_candidate":
        _restore_or_delete_row(conn, table="memory_items", pk="memory_id", rollback=rollback)
        _restore_semantic_group_members(conn, rollback)
        conn.execute("DELETE FROM memory_items_fts WHERE rowid NOT IN (SELECT id FROM memory_items)")
        conn.execute("DELETE FROM memory_item_tags WHERE memory_item_id NOT IN (SELECT id FROM memory_items)")
    elif op == "add_negative_phrase":
        _restore_or_delete_row(conn, table="memory_candidate_suppressions", pk="id", rollback=rollback)
    elif op == "supersede_memory":
        table = compact_whitespace(str(rollback.get("table") or ""))
        pk = "id" if table == "memory_atoms" else "memory_id"
        if table in {"memory_atoms", "memory_items"}:
            _restore_or_delete_row(conn, table=table, pk=pk, rollback=rollback)
        relation = rollback.get("supersession")
        if isinstance(relation, dict):
            _restore_or_delete_row(
                conn,
                table="memory_supersessions",
                pk="supersession_id",
                rollback=relation,
            )
        dependency_rollback = rollback.get("dependencyInvalidation")
        if isinstance(dependency_rollback, dict):
            restore_dependency_invalidation(conn, dependency_rollback)
    elif op == "retract_memory_atom":
        atom = rollback.get("atom")
        if isinstance(atom, dict) and atom:
            _insert_or_replace_dict(conn, "memory_atoms", atom)
        for book in rollback.get("books", []) or []:
            if isinstance(book, dict) and book:
                _insert_or_replace_dict(conn, "memory_books", book)
        dependency_rollback = rollback.get("dependencyInvalidation")
        if isinstance(dependency_rollback, dict):
            restore_dependency_invalidation(conn, dependency_rollback)
        tombstone_id = int(rollback.get("tombstoneId") or 0)
        if tombstone_id > 0:
            conn.execute(
                "UPDATE memory_tombstones SET active = 0 WHERE id = ?",
                (tombstone_id,),
            )
        _rollback_personal_atom_evidence_governance(
            conn,
            rollback.get("personalEvidenceGovernance"),
        )


def _apply_semantic_group(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    group_id = str(payload["groupId"])
    previous = _row_dict(
        conn.execute("SELECT * FROM memory_semantic_groups WHERE group_id = ?", (group_id,)).fetchone()
    )
    timestamp = now_ms()
    conn.execute(
        """
        INSERT INTO memory_semantic_groups(
            group_id, title, description, project, aliases_json, tags_json,
            source_event_ids_json, status, confidence, quality_score,
            created_at_ms, updated_at_ms, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            project = excluded.project,
            aliases_json = excluded.aliases_json,
            tags_json = excluded.tags_json,
            source_event_ids_json = excluded.source_event_ids_json,
            status = excluded.status,
            confidence = excluded.confidence,
            quality_score = excluded.quality_score,
            updated_at_ms = excluded.updated_at_ms,
            metadata_json = excluded.metadata_json
        """,
        (
            group_id,
            str(payload.get("title") or ""),
            str(payload.get("description") or ""),
            str(payload.get("project") or ""),
            json.dumps(_strings(payload.get("aliases")), ensure_ascii=False),
            json.dumps(_strings(payload.get("tags")), ensure_ascii=False),
            json.dumps(_positive_ints(payload.get("sourceEventIds")), ensure_ascii=False),
            str(payload.get("status") or "active"),
            _bounded_float(payload.get("confidence"), default=0.7),
            _bounded_float(payload.get("qualityScore"), default=0.7),
            int(previous.get("created_at_ms") or timestamp),
            timestamp,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    return {"table": "memory_semantic_groups", "pk": "group_id", "pkValue": group_id, "previous": previous}


def _apply_semantic_tag(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    name = str(payload["name"])
    row = conn.execute(
        "SELECT id FROM memory_tags WHERE tag = ? OR normalized_tag = ? "
        "ORDER BY CASE WHEN tag = ? THEN 0 ELSE 1 END, quality_score DESC LIMIT 1",
        (name, normalize_text(name), name),
    ).fetchone()
    timestamp = now_ms()
    if row is None:
        cursor = conn.execute(
            """
            INSERT INTO memory_tags(
                tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms,
                description, source, status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'dsv4', ?, ?)
            """,
            (
                name,
                normalize_text(name),
                str(payload.get("type") or "concept"),
                _bounded_float(payload.get("qualityScore"), default=0.7),
                timestamp,
                timestamp,
                str(payload.get("description") or ""),
                str(payload.get("status") or "active"),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        tag_id = int(cursor.lastrowid)
        previous: dict[str, object] = {}
    else:
        tag_id = int(row["id"])
        previous = _row_dict(conn.execute("SELECT * FROM memory_tags WHERE id = ?", (tag_id,)).fetchone())
        conn.execute(
            """
            UPDATE memory_tags
            SET normalized_tag = ?, tag_type = ?, quality_score = ?, updated_at_ms = ?,
                description = ?,
                source = CASE WHEN source = 'user' THEN 'user' ELSE 'dsv4' END,
                status = ?, metadata_json = ?
            WHERE id = ?
            """,
            (
                normalize_text(name),
                str(payload.get("type") or "concept"),
                _bounded_float(payload.get("qualityScore"), default=0.7),
                timestamp,
                str(payload.get("description") or ""),
                str(payload.get("status") or "active"),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                tag_id,
            ),
        )
    profile_previous = _row_dict(
        conn.execute("SELECT * FROM memory_tag_profiles WHERE tag_id = ?", (tag_id,)).fetchone()
    )
    conn.execute(
        """
        INSERT INTO memory_tag_profiles(tag_id, color_token, aliases_json, updated_at_ms)
        VALUES (?, 'blue', ?, ?)
        ON CONFLICT(tag_id) DO UPDATE SET aliases_json = excluded.aliases_json, updated_at_ms = excluded.updated_at_ms
        """,
        (tag_id, json.dumps(_strings(payload.get("aliases")), ensure_ascii=False), timestamp),
    )
    memberships = _sync_semantic_group_members(
        conn,
        member_type="tag",
        member_id=str(tag_id),
        group_ids=_strings(payload.get("semanticGroupIds")),
    )
    return {
        "table": "memory_tags",
        "pk": "id",
        "pkValue": str(tag_id),
        "previous": previous,
        "profile": {
            "table": "memory_tag_profiles",
            "pk": "tag_id",
            "pkValue": str(tag_id),
            "previous": profile_previous,
        },
        **memberships,
    }


def _resolve_memory_book_write_target(
    conn: sqlite3.Connection,
    requested_book_id: str,
) -> tuple[str, dict[str, object], list[str]]:
    """Resolve a Book write through every persisted superseded redirect."""

    requested_id = compact_whitespace(requested_book_id)
    if not requested_id:
        return "", {}, []
    current = _row_dict(
        conn.execute(
            "SELECT * FROM memory_books WHERE book_id = ?",
            (requested_id,),
        ).fetchone()
    )
    if not current:
        return requested_id, {}, []
    visited: set[str] = set()
    redirect_chain: list[str] = []
    while True:
        current_id = compact_whitespace(str(current.get("book_id") or ""))
        if not current_id or current_id in visited:
            raise ValueError("memory Book redirect is cyclic or incomplete")
        visited.add(current_id)
        redirect_chain.append(current_id)
        if compact_whitespace(str(current.get("status") or "")) != "superseded":
            return current_id, current, redirect_chain
        metadata = _json_object(current.get("metadata_json"))
        redirect_id = compact_whitespace(
            str(metadata.get("supersededByBookId") or metadata.get("resolvedBookId") or "")
        )
        if not redirect_id:
            raise ValueError("superseded memory Book has no redirect")
        next_row = _row_dict(
            conn.execute(
                "SELECT * FROM memory_books WHERE book_id = ?",
                (redirect_id,),
            ).fetchone()
        )
        if not next_row:
            raise ValueError("superseded memory Book redirect target is missing")
        if not _memory_books_same_scope(current, next_row):
            raise ValueError("memory Book redirect crosses owner or scope")
        current = next_row


def _book_summary_from_atom_rows(atom_rows: list[Mapping[str, object]]) -> str:
    return compact_whitespace(
        "；".join(
            compact_whitespace(str(row.get("canonical_text") or row.get("text") or ""))
            for row in atom_rows
            if compact_whitespace(str(row.get("canonical_text") or row.get("text") or ""))
        )
    )[:2400]


def _apply_memory_book(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    requested_book_id = compact_whitespace(str(payload.get("bookId") or ""))
    if not requested_book_id:
        raise ValueError("memory Book id is required")
    book_id, previous, redirect_chain = _resolve_memory_book_write_target(
        conn,
        requested_book_id,
    )
    payload = dict(payload)
    payload["bookId"] = book_id
    _assert_memory_owner_unchanged(previous, payload, target_kind="book")
    _assert_memory_book_scope_unchanged(previous, payload)

    previous_member_ids = _json_list(previous.get("memory_atom_ids_json"))
    requested_member_ids = _strings(payload.get("memoryAtomIds"))
    candidate_member_ids = list(
        dict.fromkeys([*previous_member_ids, *requested_member_ids])
    )
    scope_book: Mapping[str, object] = previous or payload
    valid_atoms = _memory_book_valid_atom_members(
        conn,
        book=scope_book,
        atom_ids=candidate_member_ids,
    )
    existing_atom_ids = {
        compact_whitespace(str(row.get("id") or ""))
        for row in (
            _row_dict(conn.execute(
                "SELECT * FROM memory_atoms WHERE id = ?",
                (atom_id,),
            ).fetchone())
            for atom_id in candidate_member_ids
        )
        if row
    }
    # Preserve IDs for atoms created later in the same governed plan.  The
    # post-plan reconciliation removes unknown or out-of-scope IDs once all
    # Atom diffs have been applied.
    pending_requested_ids = [
        atom_id for atom_id in requested_member_ids if atom_id not in existing_atom_ids
    ]
    effective_member_ids = list(
        dict.fromkeys(
            [str(row["id"]) for row in valid_atoms] + pending_requested_ids
        )
    )
    if candidate_member_ids:
        summary = _book_summary_from_atom_rows(valid_atoms)
        if not summary and pending_requested_ids:
            summary = compact_whitespace(str(payload.get("summary") or ""))[:2400]
    else:
        summary = compact_whitespace(str(payload.get("summary") or ""))[:2400]

    previous_tags = _json_list(previous.get("tags_json"))
    tags = _unique_strings(
        [*previous_tags, *_strings(payload.get("tags"))],
        limit=None,
    )
    previous_surface_hints = _json_list(previous.get("surface_hints_json"))
    surface_hints = _unique_strings(
        [*previous_surface_hints, *_strings(payload.get("surfaceHints"))],
        limit=None,
    )
    previous_query_expansions = _json_list(previous.get("query_expansions_json"))
    query_expansions = _unique_strings(
        [*previous_query_expansions, *_strings(payload.get("queryExpansions"))],
        limit=None,
    )
    source_event_ids = _positive_ints(
        [
            *_json_list(previous.get("source_event_ids_json")),
            *_positive_ints(payload.get("sourceEventIds")),
            *[
                event_id
                for row in valid_atoms
                for event_id in _positive_ints(
                    _json_list(row.get("source_event_ids_json"))
                )
            ],
        ]
    )
    previous_group_ids = [
        str(row[0])
        for row in conn.execute(
            "SELECT group_id FROM memory_semantic_group_members "
            "WHERE member_type = 'book' AND member_id = ? ORDER BY group_id",
            (book_id,),
        ).fetchall()
    ]
    group_ids = _unique_strings(
        [*previous_group_ids, *_strings(payload.get("semanticGroupIds"))],
        limit=None,
    )

    previous_metadata = _json_object(previous.get("metadata_json"))
    metadata = {**previous_metadata, **payload}
    aliases = _unique_strings(
        [
            *_strings(previous_metadata.get("topicAliases")),
            *_strings(previous_metadata.get("aliases")),
            *_strings(payload.get("topicAliases")),
            *_strings(payload.get("aliases")),
            compact_whitespace(str(previous.get("title") or "")),
            compact_whitespace(str(previous.get("book_key") or "")),
            compact_whitespace(str(payload.get("title") or "")),
            compact_whitespace(str(payload.get("bookKey") or "")),
        ],
        limit=64,
    )
    if aliases:
        metadata["topicAliases"] = aliases
    previous_merged = _strings(previous_metadata.get("mergedSourceBookIds"))
    requested_merged = _strings(payload.get("mergedSourceBookIds"))
    if previous_merged or requested_merged:
        metadata["mergedSourceBookIds"] = _unique_strings(
            [*previous_merged, *requested_merged],
            limit=None,
        )
    metadata["memberCount"] = len(effective_member_ids)
    for stale_key in (
        "retrievalStale",
        "retrievalStaleReason",
        "retrievalStaleAtMs",
        "supersededByBookId",
    ):
        metadata.pop(stale_key, None)

    timestamp = now_ms()
    conn.execute(
        """
        INSERT INTO memory_books(
            book_id, book_type, book_key, title, summary, normalized_text, project, app,
            tags_json, surface_hints_json, query_expansions_json, source_event_ids_json,
            memory_atom_ids_json, owner_kind, owner_id, status, confidence, quality_score,
            created_at_ms, updated_at_ms, metadata_json, archived_at_ms,
            last_active_at_ms, archive_reason
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                COALESCE((SELECT created_at_ms FROM memory_books WHERE book_id = ?), ?),
                ?, ?, NULL, ?, '')
        ON CONFLICT(book_id) DO UPDATE SET
            book_type = excluded.book_type,
            book_key = excluded.book_key,
            title = excluded.title,
            summary = excluded.summary,
            normalized_text = excluded.normalized_text,
            project = excluded.project,
            app = excluded.app,
            tags_json = excluded.tags_json,
            surface_hints_json = excluded.surface_hints_json,
            query_expansions_json = excluded.query_expansions_json,
            source_event_ids_json = excluded.source_event_ids_json,
            memory_atom_ids_json = excluded.memory_atom_ids_json,
            owner_kind = excluded.owner_kind,
            owner_id = excluded.owner_id,
            status = excluded.status,
            confidence = excluded.confidence,
            quality_score = excluded.quality_score,
            updated_at_ms = excluded.updated_at_ms,
            metadata_json = excluded.metadata_json,
            archived_at_ms = NULL,
            last_active_at_ms = excluded.last_active_at_ms,
            archive_reason = ''
        """,
        (
            book_id,
            str(payload.get("bookType") or previous.get("book_type") or "daily"),
            str(payload.get("bookKey") or previous.get("book_key") or ""),
            str(payload.get("title") or previous.get("title") or ""),
            summary,
            normalize_text(
                f"{payload.get('title') or previous.get('title') or ''} {summary}"
            ),
            str(payload.get("project") or previous.get("project") or ""),
            str(payload.get("app") or previous.get("app") or ""),
            json.dumps(tags, ensure_ascii=False),
            json.dumps(surface_hints, ensure_ascii=False),
            json.dumps(query_expansions, ensure_ascii=False),
            json.dumps(source_event_ids, ensure_ascii=False),
            json.dumps(effective_member_ids, ensure_ascii=False),
            str(payload.get("ownerKind") or previous.get("owner_kind") or "user"),
            str(payload.get("ownerId") or previous.get("owner_id") or "default"),
            str(payload.get("status") or "active"),
            _bounded_float(payload.get("confidence"), default=float(previous.get("confidence") or 0.5)),
            _bounded_float(payload.get("qualityScore"), default=float(previous.get("quality_score") or 0.5)),
            book_id,
            timestamp,
            timestamp,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            timestamp,
        ),
    )
    scope_values = {
        "knowledge_domain": compact_whitespace(
            str(payload.get("knowledgeDomain") or previous.get("knowledge_domain") or "legacy")
        ),
        "scope_kind": compact_whitespace(
            str(payload.get("scopeKind") or previous.get("scope_kind") or "legacy")
        ),
        "scope_id": compact_whitespace(
            str(payload.get("scopeId") or previous.get("scope_id") or "")
        ),
        "visibility": compact_whitespace(
            str(payload.get("visibility") or previous.get("visibility") or "legacy")
        ),
        "authorization_revision": compact_whitespace(
            str(payload.get("authorizationRevision") or previous.get("authorization_revision") or "")
        ),
        "binding_id": compact_whitespace(
            str(payload.get("bindingId") or previous.get("binding_id") or "")
        ),
        "scope_mode": compact_whitespace(
            str(payload.get("scopeMode") or previous.get("scope_mode") or "legacy")
        ),
    }
    conn.execute(
        """
        UPDATE memory_books
        SET knowledge_domain = ?, scope_kind = ?, scope_id = ?, visibility = ?,
            authorization_revision = ?, binding_id = ?, scope_mode = ?
        WHERE book_id = ?
        """,
        (*scope_values.values(), book_id),
    )
    memberships = _sync_semantic_group_members(
        conn,
        member_type="book",
        member_id=book_id,
        group_ids=group_ids,
    )
    return {
        "table": "memory_books",
        "pk": "book_id",
        "pkValue": book_id,
        "requestedBookId": requested_book_id,
        "redirectChain": redirect_chain,
        "previous": previous,
        **memberships,
    }


def _memory_book_valid_atom_members(
    conn: sqlite3.Connection,
    *,
    book: Mapping[str, object],
    atom_ids: list[str],
) -> list[dict[str, object]]:
    """Return current, non-sensitive Atoms that belong to one Book scope."""

    book_owner = (
        _memory_book_field(book, "owner_kind"),
        _memory_book_field(book, "owner_id"),
    )
    book_project = _memory_book_field(book, "project")
    result: list[dict[str, object]] = []
    for atom_id in dict.fromkeys(atom_ids):
        row = _row_dict(
            conn.execute("SELECT * FROM memory_atoms WHERE id = ?", (atom_id,)).fetchone()
        )
        if not row:
            continue
        if compact_whitespace(str(row.get("status") or "")) not in {"active", "approved"}:
            continue
        if compact_whitespace(str(row.get("claim_state") or "current")) != "current":
            continue
        if compact_whitespace(str(row.get("privacy_level") or "")) == "sensitive":
            continue
        if (
            compact_whitespace(str(row.get("owner_kind") or "user")),
            compact_whitespace(str(row.get("owner_id") or "default")),
        ) != book_owner:
            continue
        atom_project = compact_whitespace(str(row.get("scope_project") or ""))
        if atom_project != book_project:
            continue
        if not _memory_book_atom_scope_matches(book, row):
            continue
        result.append(row)
    return result


def _memory_book_union_values(
    rows: list[Mapping[str, object]],
    column: str,
) -> list[str]:
    values: list[str] = []
    for row in rows:
        values.extend(_json_list(row.get(column)))
    return _unique_strings(values, limit=None)


def _memory_book_union_ints(
    rows: list[Mapping[str, object]],
    column: str,
) -> list[int]:
    values: list[int] = []
    for row in rows:
        values.extend(_positive_ints(_json_list(row.get(column))))
    return _positive_ints(values)


def _memory_book_aliases(rows: list[Mapping[str, object]]) -> list[str]:
    aliases: list[str] = []
    for row in rows:
        metadata = _json_object(row.get("metadata_json"))
        aliases.extend(_strings(metadata.get("topicAliases")))
        aliases.extend(_strings(metadata.get("aliases")))
        for key in ("title", "book_key"):
            value = compact_whitespace(str(row.get(key) or ""))
            if value:
                aliases.append(value)
    return _unique_strings(
        [alias for alias in aliases if not _contains_sensitive_text(alias)],
        limit=64,
    )


def _refresh_memory_book_projection_state(
    conn: sqlite3.Connection,
    *,
    book_ids: list[str],
    timestamp: int,
) -> None:
    normalized_ids = list(dict.fromkeys(compact_whitespace(value) for value in book_ids if compact_whitespace(value)))
    if not normalized_ids:
        return
    delete_retrieval_docs(conn, [f"book:{book_id}" for book_id in normalized_ids])
    for book_id in normalized_ids:
        conn.execute(
            "DELETE FROM memory_projection_dependencies "
            "WHERE dependent_type = 'book' AND dependent_id = ?",
            (book_id,),
        )
    for book_id in normalized_ids:
        bump_source_revision(
            conn,
            source_type="book",
            source_id=book_id,
            timestamp=timestamp,
        )
        row = _row_dict(
            conn.execute("SELECT * FROM memory_books WHERE book_id = ?", (book_id,)).fetchone()
        )
        if not row or compact_whitespace(str(row.get("status") or "")) not in {"active", "approved", "archived"}:
            continue
        revision = max(
            1,
            int(
                conn.execute(
                    "SELECT generation FROM memory_source_generations "
                    "WHERE source_type = 'book' AND source_id = ?",
                    (book_id,),
                ).fetchone()[0]
                or 1
            ),
        )
        sync_projection_dependencies(
            conn,
            [
                {
                    "doc_id": f"book:{book_id}",
                    "doc_type": "book",
                    "source_id": book_id,
                    "source_revision": revision,
                    "metadata": {
                        "memoryAtomIds": _json_list(row.get("memory_atom_ids_json")),
                    },
                }
            ],
            timestamp=timestamp,
        )


def _reconcile_memory_book_current_state(
    conn: sqlite3.Connection,
    *,
    book_id: str,
    timestamp: int | None = None,
) -> dict[str, object] | None:
    """Recompute one live Book from the current DB Atom state.

    Book diffs can run before Atom diffs in a stored plan, and another
    transaction may have added a member after the model snapshot.  This
    final pass closes that race without trusting the frozen member subset.
    """

    normalized_id = compact_whitespace(book_id)
    if not normalized_id:
        return None
    row = _row_dict(
        conn.execute(
            "SELECT * FROM memory_books WHERE book_id = ?",
            (normalized_id,),
        ).fetchone()
    )
    if not row or compact_whitespace(str(row.get("status") or "")) == "superseded":
        return None
    member_ids = _json_list(row.get("memory_atom_ids_json"))
    if not member_ids:
        return None
    valid_atoms = _memory_book_valid_atom_members(
        conn,
        book=row,
        atom_ids=member_ids,
    )
    valid_ids = [str(atom.get("id") or "") for atom in valid_atoms if str(atom.get("id") or "")]
    source_event_ids = _positive_ints(_json_list(row.get("source_event_ids_json")))
    source_event_ids.extend(
        event_id
        for atom in valid_atoms
        for event_id in _positive_ints(_json_list(atom.get("source_event_ids_json")))
    )
    resolved_at = now_ms() if timestamp is None else max(0, int(timestamp))
    metadata = _json_object(row.get("metadata_json"))
    metadata["memberCount"] = len(valid_ids)
    summary = _book_summary_from_atom_rows(valid_atoms)
    conn.execute(
        """
        UPDATE memory_books
        SET summary = ?, normalized_text = ?, memory_atom_ids_json = ?,
            source_event_ids_json = ?, metadata_json = ?, updated_at_ms = ?,
            last_active_at_ms = CASE
                WHEN status IN ('active', 'approved') THEN ?
                ELSE last_active_at_ms
            END
        WHERE book_id = ? AND status != 'superseded'
        """,
        (
            summary,
            normalize_text(f"{row.get('title') or ''} {summary}"),
            json.dumps(valid_ids, ensure_ascii=False),
            json.dumps(_positive_ints(source_event_ids), ensure_ascii=False),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            resolved_at,
            resolved_at,
            normalized_id,
        ),
    )
    _refresh_memory_book_projection_state(
        conn,
        book_ids=[normalized_id],
        timestamp=resolved_at,
    )
    return {
        "bookId": normalized_id,
        "memoryAtomIds": valid_ids,
        "memberCount": len(valid_ids),
    }


def _apply_memory_book_merge(
    conn: sqlite3.Connection,
    payload: dict[str, object],
) -> dict[str, object]:
    target_id = compact_whitespace(str(payload.get("targetBookId") or ""))
    source_ids = _memory_book_merge_sources(payload)
    if not target_id or not source_ids or target_id in source_ids:
        raise ValueError("memory Book merge references are incomplete")
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("memory Book merge source is repeated")
    target = _row_dict(
        conn.execute("SELECT * FROM memory_books WHERE book_id = ?", (target_id,)).fetchone()
    )
    if not target:
        raise ValueError(f"memory Book merge target is missing: {target_id}")
    if compact_whitespace(str(target.get("book_type") or "")) != "topic":
        raise ValueError("only topic Books can be merged")
    if compact_whitespace(str(target.get("status") or "")) not in {"active", "approved"}:
        raise ValueError("memory Book merge target is no longer active")
    sources: list[dict[str, object]] = []
    for source_id in source_ids:
        source = _row_dict(
            conn.execute("SELECT * FROM memory_books WHERE book_id = ?", (source_id,)).fetchone()
        )
        if not source:
            raise ValueError(f"memory Book merge source is missing: {source_id}")
        if compact_whitespace(str(source.get("book_type") or "")) != "topic":
            raise ValueError("only topic Books can be merged")
        if compact_whitespace(str(source.get("status") or "")) not in {"active", "approved"}:
            raise ValueError(f"memory Book merge source is no longer active: {source_id}")
        if not _memory_books_same_scope(source, target):
            raise ValueError("memory Book merge crosses owner or scope")
        sources.append(source)

    all_rows = [target, *sources]
    previous_group_members = [
        _row_dict(row)
        for row in conn.execute(
            "SELECT * FROM memory_semantic_group_members "
            "WHERE member_type = 'book' AND member_id IN ({}) ORDER BY group_id, member_id".format(
                ",".join("?" for _ in all_rows)
            ),
            tuple(str(row["book_id"]) for row in all_rows),
        ).fetchall()
    ]
    member_ids = _unique_strings(
        [
            atom_id
            for row in all_rows
            for atom_id in _json_list(row.get("memory_atom_ids_json"))
        ],
        limit=None,
    )
    valid_atoms = _memory_book_valid_atom_members(
        conn,
        book=target,
        atom_ids=member_ids,
    )
    valid_member_ids = [str(row["id"]) for row in valid_atoms]
    summary = compact_whitespace(
        "；".join(
            compact_whitespace(str(row.get("canonical_text") or row.get("text") or ""))
            for row in valid_atoms
            if compact_whitespace(str(row.get("canonical_text") or row.get("text") or ""))
        )
    )[:2400]
    tags = _memory_book_union_values(all_rows, "tags_json")
    surface_hints = _memory_book_union_values(all_rows, "surface_hints_json")
    query_expansions = _memory_book_union_values(all_rows, "query_expansions_json")
    source_event_ids = _memory_book_union_ints(all_rows, "source_event_ids_json")
    aliases = _memory_book_aliases(all_rows)
    target_metadata = _json_object(target.get("metadata_json"))
    previous_merged = _strings(target_metadata.get("mergedSourceBookIds"))
    target_metadata.update(
        {
            "topicAliases": aliases,
            "mergedSourceBookIds": _unique_strings(
                [*previous_merged, *source_ids],
                limit=None,
            ),
            "memberCount": len(valid_member_ids),
            "lastMergeReason": compact_whitespace(str(payload.get("reason") or ""))[:360],
            "lastMergedAtMs": now_ms(),
        }
    )
    for key in ("retrievalStale", "retrievalStaleReason", "retrievalStaleAtMs", "supersededByBookId"):
        target_metadata.pop(key, None)
    timestamp = now_ms()
    conn.execute(
        """
        UPDATE memory_books
        SET summary = ?, normalized_text = ?, tags_json = ?,
            surface_hints_json = ?, query_expansions_json = ?,
            source_event_ids_json = ?, memory_atom_ids_json = ?,
            status = 'active', archived_at_ms = NULL, archive_reason = '',
            updated_at_ms = ?, last_active_at_ms = ?, metadata_json = ?
        WHERE book_id = ?
        """,
        (
            summary,
            normalize_text(f"{target.get('title') or ''} {summary}"),
            json.dumps(tags, ensure_ascii=False, sort_keys=False),
            json.dumps(surface_hints, ensure_ascii=False, sort_keys=False),
            json.dumps(query_expansions, ensure_ascii=False, sort_keys=False),
            json.dumps(source_event_ids, ensure_ascii=False, sort_keys=False),
            json.dumps(valid_member_ids, ensure_ascii=False, sort_keys=False),
            timestamp,
            timestamp,
            json.dumps(target_metadata, ensure_ascii=False, sort_keys=True),
            target_id,
        ),
    )
    for source in sources:
        source_id = str(source["book_id"])
        source_metadata = _json_object(source.get("metadata_json"))
        source_metadata["supersededByBookId"] = target_id
        source_metadata["supersededAtMs"] = timestamp
        source_metadata["supersededReason"] = compact_whitespace(
            str(payload.get("reason") or "topic_book_merge")
        )[:360]
        source_metadata["retrievalStale"] = True
        source_metadata["retrievalStaleReason"] = "superseded_book"
        source_metadata["topicAliases"] = aliases
        conn.execute(
            """
            UPDATE memory_books
            SET status = 'superseded', updated_at_ms = ?,
                metadata_json = ?
            WHERE book_id = ?
            """,
            (
                timestamp,
                json.dumps(source_metadata, ensure_ascii=False, sort_keys=True),
                source_id,
            ),
        )

    # Preserve historical source membership while making the live target
    # discoverable through every semantic group that contained a fragment.
    target_groups = {
        str(row["group_id"])
        for row in previous_group_members
        if str(row.get("member_id") or "") == target_id
    }
    for row in previous_group_members:
        group_id = compact_whitespace(str(row.get("group_id") or ""))
        if not group_id or group_id in target_groups or str(row.get("member_id") or "") == target_id:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO memory_semantic_group_members(
                group_id, member_type, member_id, weight, source, updated_at_ms
            ) VALUES (?, 'book', ?, ?, ?, ?)
            """,
            (
                group_id,
                target_id,
                float(row.get("weight") or 0.0),
                str(row.get("source") or "dsv4"),
                int(row.get("updated_at_ms") or timestamp),
            ),
        )
        target_groups.add(group_id)

    _refresh_memory_book_projection_state(
        conn,
        book_ids=[target_id, *source_ids],
        timestamp=timestamp,
    )
    return {
        "op": "merge_memory_books",
        "target": {"bookId": target_id, "previous": target},
        "sources": [
            {"bookId": str(source["book_id"]), "previous": source}
            for source in sources
        ],
        "groupMembers": previous_group_members,
        "sourceBookIds": source_ids,
    }


def _rollback_memory_book_merge(
    conn: sqlite3.Connection,
    rollback: Mapping[str, object],
) -> None:
    target = rollback.get("target")
    if isinstance(target, Mapping):
        previous_target = target.get("previous")
        if isinstance(previous_target, dict) and previous_target:
            _insert_or_replace_dict(conn, "memory_books", dict(previous_target))
    sources = rollback.get("sources")
    source_ids: list[str] = []
    if isinstance(sources, list):
        for item in sources:
            if not isinstance(item, Mapping):
                continue
            source_id = compact_whitespace(str(item.get("bookId") or ""))
            previous = item.get("previous")
            if source_id:
                source_ids.append(source_id)
            if isinstance(previous, dict) and previous:
                _insert_or_replace_dict(conn, "memory_books", dict(previous))
    target_id = (
        compact_whitespace(str(target.get("bookId") or ""))
        if isinstance(target, Mapping)
        else ""
    )
    all_ids = [target_id, *source_ids]
    if all_ids:
        conn.execute(
            "DELETE FROM memory_semantic_group_members "
            "WHERE member_type = 'book' AND member_id IN ({})".format(",".join("?" for _ in all_ids)),
            tuple(all_ids),
        )
    for row in _list_of_dicts(rollback.get("groupMembers")):
        _insert_or_replace_dict(conn, "memory_semantic_group_members", row)
    _refresh_memory_book_projection_state(
        conn,
        book_ids=all_ids,
        timestamp=now_ms(),
    )


def _memory_atom_row_identity_projection(
    row: Mapping[str, object],
) -> dict[str, object]:
    return {
        "text": row.get("text"),
        "canonicalText": row.get("canonical_text") or row.get("text"),
        "kind": row.get("kind"),
        "language": row.get("language"),
        "project": row.get("scope_project"),
        "app": row.get("scope_app"),
        "claimKey": row.get("claim_key"),
        "lineageId": row.get("lineage_id"),
        "claimState": row.get("claim_state"),
        "validFromMs": row.get("valid_from_ms"),
        "validToMs": row.get("valid_to_ms"),
        "supersedesId": row.get("supersedes_id"),
        "status": row.get("status"),
        "ownerKind": row.get("owner_kind"),
        "ownerId": row.get("owner_id"),
        "privacyLevel": row.get("privacy_level"),
        "knowledgeDomain": row.get("knowledge_domain"),
        "scopeKind": row.get("scope_kind"),
        "scopeId": row.get("scope_id"),
        "visibility": row.get("visibility"),
        "authorizationRevision": row.get("authorization_revision"),
        "bindingId": row.get("binding_id"),
        "scopeMode": row.get("scope_mode"),
    }


def _global_atom_relation_snapshot(
    conn: sqlite3.Connection,
    atom_id: str,
) -> dict[str, list[dict[str, object]]]:
    return {
        "aliases": [
            _row_dict(row)
            for row in conn.execute(
                "SELECT * FROM memory_aliases WHERE memory_atom_id = ? ORDER BY id",
                (atom_id,),
            ).fetchall()
        ],
        "tags": [
            _row_dict(row)
            for row in conn.execute(
                "SELECT * FROM memory_atom_tags WHERE memory_atom_id = ? ORDER BY tag_id",
                (atom_id,),
            ).fetchall()
        ],
        "groups": [
            _row_dict(row)
            for row in conn.execute(
                """
                SELECT * FROM memory_semantic_group_members
                WHERE member_type = 'atom' AND member_id = ?
                ORDER BY group_id
                """,
                (atom_id,),
            ).fetchall()
        ],
    }


def _lossless_relation_scalar(values: list[object]) -> str:
    normalized = [
        str(value)
        for value in values
        if value is not None and str(value)
    ]
    unique = list(dict.fromkeys(normalized))
    if not unique:
        return ""
    if len(unique) == 1:
        return unique[0]
    return json.dumps(unique, ensure_ascii=False, sort_keys=False)


def _prepare_global_atom_merge(
    conn: sqlite3.Connection,
    *,
    atom_id: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    source_ids = _strings(payload.get("mergeSourceIds"))
    if not source_ids:
        source_id = compact_whitespace(str(payload.get("mergeSourceId") or ""))
        source_ids = [source_id] if source_id else []
    source_ids = list(dict.fromkeys(source_ids))
    if not source_ids or atom_id in source_ids:
        raise ValueError("global Atom merge sources are incomplete")
    target_row = conn.execute(
        "SELECT * FROM memory_atoms WHERE id = ?",
        (atom_id,),
    ).fetchone()
    if target_row is None:
        raise ValueError("global Atom merge target is missing")
    target_projection = _memory_atom_row_identity_projection(_row_dict(target_row))
    target_identity = memory_atom_identity_hash(target_projection)
    target_authority = memory_atom_authority_hash(target_projection)
    payload_identity = compact_whitespace(str(payload.get("identityHash") or ""))
    payload_authority = compact_whitespace(str(payload.get("authorityHash") or ""))
    if payload_identity and payload_identity != target_identity:
        raise ValueError("global Atom merge target identity changed")
    if payload_authority and payload_authority != target_authority:
        raise ValueError("global Atom merge target authority changed")
    source_rows: list[dict[str, object]] = []
    for source_id in source_ids:
        row = conn.execute(
            "SELECT * FROM memory_atoms WHERE id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise ValueError("global Atom merge source is missing")
        source = _row_dict(row)
        source_projection = _memory_atom_row_identity_projection(source)
        if memory_atom_identity_hash(source_projection) != target_identity:
            raise ValueError("global Atom merge source identity conflicts")
        if memory_atom_authority_hash(source_projection) != target_authority:
            raise ValueError("global Atom merge source authority conflicts")
        source_rows.append(source)
    snapshots = {
        atom_id: _global_atom_relation_snapshot(conn, atom_id),
        **{
            source_id: _global_atom_relation_snapshot(conn, source_id)
            for source_id in source_ids
        },
    }
    aliases = [
        alias
        for snapshot in snapshots.values()
        for alias in snapshot["aliases"]
    ]
    alias_metadata: dict[tuple[str, str], set[str]] = {}
    for alias in aliases:
        key = (
            str(alias.get("alias_type") or "alias"),
            str(alias.get("alias") or ""),
        )
        pinyin = compact_whitespace(str(alias.get("pinyin") or ""))
        if pinyin:
            alias_metadata.setdefault(key, set()).add(pinyin)
    if any(len(values) > 1 for values in alias_metadata.values()):
        raise ValueError("global Atom alias metadata conflict")
    return {
        "sourceIds": source_ids,
        "sourceRows": source_rows,
        "snapshots": snapshots,
        "targetIdentity": target_identity,
        "targetAuthority": target_authority,
    }


def _apply_global_atom_merge_relations(
    conn: sqlite3.Connection,
    *,
    atom_id: str,
    merge: Mapping[str, object],
) -> None:
    snapshots = merge.get("snapshots")
    if not isinstance(snapshots, dict):
        raise ValueError("global Atom merge relation snapshot is missing")
    all_snapshots = [
        value for value in snapshots.values() if isinstance(value, dict)
    ]
    aliases_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for snapshot in all_snapshots:
        for alias in snapshot.get("aliases", []):
            if not isinstance(alias, dict):
                continue
            key = (
                str(alias.get("alias_type") or "alias"),
                str(alias.get("alias") or ""),
            )
            if not key[1]:
                continue
            existing = aliases_by_key.get(key)
            if existing is None or float(alias.get("weight") or 0.0) > float(
                existing.get("weight") or 0.0
            ):
                aliases_by_key[key] = dict(alias)
            elif existing.get("pinyin") in (None, "") and alias.get("pinyin"):
                existing["pinyin"] = alias.get("pinyin")
            existing = aliases_by_key[key]
            existing["created_at_ms"] = min(
                int(existing.get("created_at_ms") or now_ms()),
                int(alias.get("created_at_ms") or now_ms()),
            )
    for (alias_type, alias_value), alias in aliases_by_key.items():
        alias_id = f"alias:{stable_text_hash(f'{atom_id}:{alias_type}:{alias_value}')[:20]}"
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_aliases(
                id, memory_atom_id, alias, alias_type, pinyin, weight, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alias_id,
                atom_id,
                alias_value,
                alias_type,
                str(alias.get("pinyin") or ""),
                float(alias.get("weight") or 0.0),
                int(alias.get("created_at_ms") or now_ms()),
            ),
        )

    tags_by_id: dict[str, list[dict[str, object]]] = {}
    for snapshot in all_snapshots:
        for tag in snapshot.get("tags", []):
            if isinstance(tag, dict) and str(tag.get("tag_id") or ""):
                tags_by_id.setdefault(str(tag["tag_id"]), []).append(tag)
    for tag_id, rows in tags_by_id.items():
        weights = [float(row.get("weight") or 0.0) for row in rows]
        sources = [row.get("source") for row in rows]
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_atom_tags(
                memory_atom_id, tag_id, weight, source
            ) VALUES (?, ?, ?, ?)
            """,
            (
                atom_id,
                tag_id,
                max(weights, default=0.0),
                _lossless_relation_scalar(sources),
            ),
        )

    groups_by_id: dict[str, list[dict[str, object]]] = {}
    for snapshot in all_snapshots:
        for group in snapshot.get("groups", []):
            if isinstance(group, dict) and str(group.get("group_id") or ""):
                groups_by_id.setdefault(str(group["group_id"]), []).append(group)
    for group_id, rows in groups_by_id.items():
        weights = [float(row.get("weight") or 0.0) for row in rows]
        sources = [row.get("source") for row in rows]
        updated = [
            int(row.get("updated_at_ms") or 0)
            for row in rows
        ]
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_semantic_group_members(
                group_id, member_type, member_id, weight, source, updated_at_ms
            ) VALUES (?, 'atom', ?, ?, ?, ?)
            """,
            (
                group_id,
                atom_id,
                max(weights, default=0.0),
                _lossless_relation_scalar(sources),
                max(updated, default=now_ms()),
            ),
        )


def _apply_memory_atom(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    atom_id = str(payload["atomId"])
    previous = _row_dict(conn.execute("SELECT * FROM memory_atoms WHERE id = ?", (atom_id,)).fetchone())
    expected_cards = dict(payload.get("expectedCardRevisions") or {})
    assert_background_card_unchanged(previous, expected_cards.get(atom_id),
                                     allow_historical_merge=bool(payload.get("globalCatalogMerge")))
    if previous and int(previous.get("user_edit_revision") or 0) and compact_whitespace(
        str(payload.get("canonicalText") or payload.get("text") or "")
    ) != compact_whitespace(str(previous.get("canonical_text") or previous.get("text") or "")):
        raise ValueError("curation must create a successor to change a user-edited card")
    operation = compact_whitespace(str(payload.get("operation") or "")).lower()
    previous_aliases = [
        _row_dict(row)
        for row in conn.execute(
            "SELECT * FROM memory_aliases WHERE memory_atom_id = ? ORDER BY created_at_ms, id",
            (atom_id,),
        ).fetchall()
    ]
    previous_atom_tags = [
        _row_dict(row)
        for row in conn.execute(
            "SELECT * FROM memory_atom_tags WHERE memory_atom_id = ? ORDER BY tag_id",
            (atom_id,),
        ).fetchall()
    ]
    global_merge = (
        _prepare_global_atom_merge(
            conn,
            atom_id=atom_id,
            payload=payload,
        )
        if bool(payload.get("globalCatalogMerge"))
        else None
    )
    _assert_memory_owner_unchanged(previous, payload, target_kind="atom")
    if global_merge is not None:
        target_row = dict(previous)
        source_rows = [
            dict(item)
            for item in global_merge.get("sourceRows", [])
            if isinstance(item, dict)
        ]
        members = [target_row, *source_rows]
        for field in (
            "text",
            "canonical_text",
            "kind",
            "language",
            "scope_app",
            "scope_project",
            "confidence",
            "quality_score",
            "privacy_level",
            "owner_kind",
            "owner_id",
            "status",
            "knowledge_domain",
            "scope_kind",
            "scope_id",
            "visibility",
            "authorization_revision",
            "binding_id",
            "scope_mode",
            "claim_key",
            "lineage_id",
            "claim_state",
            "valid_from_ms",
            "valid_to_ms",
            "supersedes_id",
        ):
            payload[field] = target_row.get(field)
        payload["canonicalText"] = target_row.get("canonical_text") or target_row.get("text")
        payload["project"] = target_row.get("scope_project") or ""
        payload["app"] = target_row.get("scope_app") or ""
        payload["kind"] = target_row.get("kind") or "project_fact"
        payload["language"] = target_row.get("language") or "zh"
        payload["ownerKind"] = target_row.get("owner_kind") or "user"
        payload["ownerId"] = target_row.get("owner_id") or "default"
        payload["status"] = target_row.get("status") or "active"
        payload["knowledgeDomain"] = target_row.get("knowledge_domain") or "legacy"
        payload["scopeKind"] = target_row.get("scope_kind") or "legacy"
        payload["scopeId"] = target_row.get("scope_id") or ""
        payload["visibility"] = target_row.get("visibility") or "legacy"
        payload["authorizationRevision"] = target_row.get("authorization_revision") or ""
        payload["bindingId"] = target_row.get("binding_id") or ""
        payload["scopeMode"] = target_row.get("scope_mode") or "legacy"
        payload["claimKey"] = target_row.get("claim_key") or ""
        payload["lineageId"] = target_row.get("lineage_id") or ""
        payload["claimState"] = target_row.get("claim_state") or "current"
        payload["validFromMs"] = target_row.get("valid_from_ms")
        payload["validToMs"] = target_row.get("valid_to_ms")
        payload["supersedesId"] = target_row.get("supersedes_id") or ""
        payload["sourceEventIds"] = _catalog_atom_union_ints(
            [
                {
                    "sourceEventIds": _json_list(row.get("source_event_ids_json"))
                }
                for row in members
            ],
            "sourceEventIds",
        )
        payload["sourceMemoryIds"] = _catalog_atom_union_values(
            [
                {
                    "sourceMemoryIds": _json_list(row.get("source_memory_ids_json"))
                }
                for row in members
            ],
            "sourceMemoryIds",
        )
        snapshots = global_merge.get("snapshots")
        all_snapshots = (
            [value for value in snapshots.values() if isinstance(value, dict)]
            if isinstance(snapshots, dict)
            else []
        )
        alias_values: dict[str, list[str]] = {
            "alias": [],
            "surface_hint": [],
            "query_expansion": [],
        }
        for snapshot in all_snapshots:
            for alias in snapshot.get("aliases", []):
                if isinstance(alias, dict):
                    alias_values.setdefault(
                        str(alias.get("alias_type") or "alias"),
                        [],
                    ).append(str(alias.get("alias") or ""))
        payload["aliases"] = _unique_strings(alias_values["alias"], limit=None)
        payload["surfaceHints"] = _unique_strings(
            alias_values["surface_hint"],
            limit=None,
        )
        payload["queryExpansions"] = _unique_strings(
            alias_values["query_expansion"],
            limit=None,
        )
        tag_ids: list[str] = []
        group_ids: list[str] = []
        for snapshot in all_snapshots:
            for tag in snapshot.get("tags", []):
                if isinstance(tag, dict):
                    tag_id = compact_whitespace(str(tag.get("tag_id") or ""))
                    if tag_id and tag_id not in tag_ids:
                        tag_ids.append(tag_id)
            for group in snapshot.get("groups", []):
                if isinstance(group, dict):
                    group_id = compact_whitespace(str(group.get("group_id") or ""))
                    if group_id and group_id not in group_ids:
                        group_ids.append(group_id)
        payload["tags"] = [
            str(row["tag"])
            for tag_id in tag_ids
            for row in [
                conn.execute("SELECT tag FROM memory_tags WHERE id = ?", (tag_id,)).fetchone()
            ]
            if row is not None and str(row["tag"] or "")
        ]
        payload["semanticGroupIds"] = group_ids
        payload["text"] = target_row.get("text") or payload["canonicalText"]
        payload["confidence"] = target_row.get("confidence")
        payload["qualityScore"] = target_row.get("quality_score")
        payload["privacyLevel"] = target_row.get("privacy_level") or "local"
    timestamp = now_ms()
    atom_text = str(payload.get("text") or payload.get("canonicalText") or "")
    language = compact_whitespace(str(payload.get("language") or "zh")) or "zh"
    canonical = str(payload.get("canonicalText") or atom_text)
    kind = str(payload.get("kind") or "project_fact")
    app = str(payload.get("app") or "")
    project = str(payload.get("project") or "")
    owner_kind, owner_id = _memory_owner(
        payload.get("ownerKind") or "user",
        payload.get("ownerId") or "default",
    )
    knowledge_domain = compact_whitespace(
        str(payload.get("knowledgeDomain") or "legacy")
    )
    scope_kind = compact_whitespace(str(payload.get("scopeKind") or "legacy"))
    scope_id = compact_whitespace(str(payload.get("scopeId") or ""))
    visibility = compact_whitespace(str(payload.get("visibility") or "legacy"))
    authorization_revision = compact_whitespace(
        str(payload.get("authorizationRevision") or "")
    )
    binding_id = compact_whitespace(str(payload.get("bindingId") or ""))
    scope_mode = compact_whitespace(str(payload.get("scopeMode") or "legacy"))
    if knowledge_domain == "personal_memory":
        if (
            owner_kind != "user"
            or owner_id != "default"
            or scope_kind != "user"
            or scope_id != "default"
            or visibility != "private"
            or scope_mode != "authoritative"
            or project
            or app
            or kind
            not in {
                "personal_fact",
                "personal_habit",
                "durable_preference",
                "personal_principle",
            }
        ):
            raise ValueError("personal Atom scope is not authoritative global user Memory")
        if not authorization_revision or not binding_id:
            raise ValueError("personal Atom authority binding is incomplete")
    claim_key = _normalized_claim_key(payload.get("claimKey"))
    if not claim_key:
        raise ValueError("memory atom claimKey is required")
    privacy_level = compact_whitespace(
        str(payload.get("privacyLevel") or "local")
    ) or "local"
    lineage_id = compact_whitespace(str(payload.get("lineageId") or ""))
    if not lineage_id:
        lineage_id = (
            "lineage:"
            + stable_text_hash(
                f"{kind}\n{project}\n{app}\n{claim_key}"
            ).removeprefix("sha256:")[:24]
        )
    claim_state = compact_whitespace(str(payload.get("claimState") or "current")).lower()
    if claim_state not in {"current", "superseded", "retracted"}:
        raise ValueError(f"unsupported memory atom claimState: {claim_state}")
    valid_from_ms = _optional_int(payload.get("validFromMs")) or timestamp
    valid_to_ms = _optional_int(payload.get("validToMs")) or None
    requested_status = str(payload.get("status") or "active")
    stored_status = requested_status
    auto_superseded: list[dict[str, object]] = []
    supersession_rollbacks: list[dict[str, object]] = []
    supersedes_id = compact_whitespace(str(payload.get("supersedesId") or ""))

    current_rows = conn.execute(
        """
        SELECT *
        FROM memory_atoms
        WHERE owner_kind = ?
          AND owner_id = ?
          AND claim_key = ?
          AND COALESCE(scope_project, '') = ?
          AND COALESCE(scope_app, '') = ?
          AND id <> ?
          AND claim_state = 'current'
          AND status IN ('active', 'approved')
        ORDER BY valid_from_ms DESC, updated_at_ms DESC
        """,
        (owner_kind, owner_id, claim_key, project, app, atom_id),
    ).fetchall()
    if not current_rows and not previous:
        forgotten = conn.execute("""
            SELECT atom.* FROM memory_atoms atom
            JOIN memory_tombstones tombstone ON tombstone.target_type='memory_id'
              AND tombstone.target_value=atom.id AND tombstone.active=1
            WHERE atom.owner_kind=? AND atom.owner_id=? AND atom.claim_key=?
              AND COALESCE(atom.scope_project,'')=? AND COALESCE(atom.scope_app,'')=?
              AND atom.knowledge_domain=? AND atom.scope_kind=? AND atom.scope_id=?
              AND atom.status IN ('tombstoned','hidden') LIMIT 1
            """, (owner_kind, owner_id, claim_key, project, app,
                  knowledge_domain, scope_kind, scope_id)).fetchone()
        if forgotten is not None:
            assert_background_card_unchanged(dict(forgotten))
    for current in current_rows:
        assert_background_card_unchanged(dict(current), expected_cards.get(str(current["id"])))
        if any(str(current[field] or "") != str(value or "") for field, value in (
            ("knowledge_domain", knowledge_domain), ("scope_kind", scope_kind), ("scope_id", scope_id),
            ("visibility", visibility), ("authorization_revision", authorization_revision),
            ("binding_id", binding_id), ("scope_mode", scope_mode), ("privacy_level", privacy_level),
        )):
            raise ValueError("curation cannot change a current card authority tuple")
    if current_rows:
        existing_lineage_id = next(
            (
                compact_whitespace(str(row["lineage_id"] or ""))
                for row in current_rows
                if compact_whitespace(str(row["lineage_id"] or ""))
            ),
            "",
        )
        if existing_lineage_id:
            lineage_id = existing_lineage_id
    if claim_state == "current" and requested_status in {"active", "approved"}:
        newer_current = next(
            (
                row
                for row in current_rows
                if int(row["valid_from_ms"] or 0) > valid_from_ms
            ),
            None,
        )
        if newer_current is not None:
            # Replayed or delayed evidence is history, not the current truth.
            claim_state = "superseded"
            stored_status = "superseded"
            valid_to_ms = int(newer_current["valid_from_ms"] or timestamp)
            supersedes_id = ""
        else:
            for row in current_rows:
                old = _row_dict(row)
                auto_superseded.append(old)
                old_id = str(row["id"])
                conn.execute(
                    """
                    UPDATE memory_atoms
                    SET status = 'superseded',
                        claim_state = 'superseded',
                        valid_to_ms = ?,
                        updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (max(valid_from_ms, 1), timestamp, old_id),
                )
                supersedes_id = supersedes_id or old_id
                supersession_rollbacks.append(
                    _record_memory_supersession(
                        conn,
                        old_id=old_id,
                        new_id=atom_id,
                        source_event_ids=_positive_ints(payload.get("sourceEventIds")),
                        reason="same_claim_key_newer_value",
                    )
                )
    elif claim_state != "current" and stored_status in {"active", "approved"}:
        stored_status = "superseded"
        valid_to_ms = valid_to_ms or timestamp

    conn.execute(
        """
        INSERT INTO memory_atoms(
            id, kind, text, canonical_text, source_event_ids_json, source_memory_ids_json,
            scope_app, scope_project, language, confidence, quality_score, echo_risk,
            privacy_level, owner_kind, owner_id, status, created_at_ms, updated_at_ms,
            last_used_at_ms, knowledge_domain, scope_kind, scope_id, visibility,
            authorization_revision, binding_id, scope_mode,
            claim_key, lineage_id, claim_state, valid_from_ms, valid_to_ms, supersedes_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?, ?, NULL,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            kind = excluded.kind,
            text = excluded.text,
            canonical_text = excluded.canonical_text,
            source_event_ids_json = excluded.source_event_ids_json,
            source_memory_ids_json = excluded.source_memory_ids_json,
            scope_app = excluded.scope_app,
            scope_project = excluded.scope_project,
            language = excluded.language,
            confidence = excluded.confidence,
            quality_score = excluded.quality_score,
            echo_risk = excluded.echo_risk,
            privacy_level = excluded.privacy_level,
            owner_kind = excluded.owner_kind,
            owner_id = excluded.owner_id,
            status = excluded.status,
            updated_at_ms = excluded.updated_at_ms,
            knowledge_domain = excluded.knowledge_domain,
            scope_kind = excluded.scope_kind,
            scope_id = excluded.scope_id,
            visibility = excluded.visibility,
            authorization_revision = excluded.authorization_revision,
            binding_id = excluded.binding_id,
            scope_mode = excluded.scope_mode,
            claim_key = excluded.claim_key,
            lineage_id = excluded.lineage_id,
            claim_state = excluded.claim_state,
            valid_from_ms = excluded.valid_from_ms,
            valid_to_ms = excluded.valid_to_ms,
            supersedes_id = excluded.supersedes_id
        """,
        (
            atom_id,
            kind,
            atom_text,
            canonical,
            json.dumps(_positive_ints(payload.get("sourceEventIds")), ensure_ascii=False),
            json.dumps(_strings(payload.get("sourceMemoryIds")), ensure_ascii=False),
            app,
            project,
            language,
            _bounded_float(payload.get("confidence"), default=0.5),
            _bounded_float(payload.get("qualityScore"), default=0.5),
            privacy_level,
            owner_kind,
            owner_id,
            stored_status,
            timestamp,
            timestamp,
            knowledge_domain,
            scope_kind,
            scope_id,
            visibility,
            authorization_revision,
            binding_id,
            scope_mode,
            claim_key,
            lineage_id,
            claim_state,
            valid_from_ms,
            valid_to_ms,
            supersedes_id or None,
        ),
    )
    protected_revision = max([int(previous.get("user_edit_revision") or 0),
                              *(int(row["user_edit_revision"] or 0) for row in current_rows)])
    if protected_revision:
        conn.execute("UPDATE memory_atoms SET user_edit_revision=? WHERE id=?", (protected_revision, atom_id))
    created_alias_ids: list[str] = []
    for alias_type, values in (
        ("alias", _strings(payload.get("aliases"))),
        ("surface_hint", _strings(payload.get("surfaceHints"))),
        ("query_expansion", _strings(payload.get("queryExpansions"))),
    ):
        for value in values:
            alias_id = f"alias:{stable_text_hash(f'{atom_id}:{alias_type}:{value}')[:20]}"
            existed = conn.execute("SELECT 1 FROM memory_aliases WHERE id = ?", (alias_id,)).fetchone() is not None
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_aliases(id, memory_atom_id, alias, alias_type, pinyin, weight, created_at_ms)
                VALUES (?, ?, ?, ?, '', ?, ?)
                """,
                (alias_id, atom_id, value, alias_type, 0.8 if alias_type != "alias" else 0.6, timestamp),
            )
            if not existed:
                created_alias_ids.append(alias_id)
    conn.execute("DELETE FROM memory_atom_tags WHERE memory_atom_id = ?", (atom_id,))
    for tag in _strings(payload.get("tags")):
        tag_id = _ensure_tag(conn, tag, source="dsv4")
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_atom_tags(memory_atom_id, tag_id, weight, source)
            VALUES (?, ?, 0.8, 'memory_book_compile')
            """,
            (atom_id, str(tag_id)),
        )
    memberships = _sync_semantic_group_members(
        conn,
        member_type="atom",
        member_id=atom_id,
        group_ids=_strings(payload.get("semanticGroupIds")),
    )
    if global_merge is not None:
        _apply_global_atom_merge_relations(
            conn,
            atom_id=atom_id,
            merge=global_merge,
        )
    revised_atom_ids = [
        compact_whitespace(str(item.get("id") or ""))
        for item in auto_superseded
    ]
    if previous and normalize_text(
        str(previous.get("canonical_text") or previous.get("text") or "")
    ) != normalize_text(canonical):
        # An in-place semantic update also invalidates summaries and vectors
        # derived from the previous text, even though the Atom ID is stable.
        revised_atom_ids.append(atom_id)
    dependency_invalidation = (
        invalidate_superseded_atom_dependencies(
            conn,
            old_atom_ids=revised_atom_ids,
            new_atom_id=atom_id,
            timestamp=timestamp,
        )
        if revised_atom_ids
        and claim_state == "current"
        and stored_status in {"active", "approved"}
        else {}
    )
    evidence_governance = (
        _apply_personal_atom_evidence_governance(
            conn,
            payload=payload,
            atom_id=atom_id,
            previous_atom=previous,
            relation=(
                "corrects"
                if compact_whitespace(str(payload.get("operation") or ""))
                == "supersede"
                or bool(supersedes_id)
                else "supports"
            ),
        )
        if knowledge_domain == "personal_memory"
        or (
            compact_whitespace(
                str(payload.get("curationArchitecture") or "")
            )
            == MEMORY_CURATION_ARCHITECTURE
            and bool(_strings(payload.get("evidenceIds")))
            and bool(compact_whitespace(str(payload.get("curationRunId") or "")))
        )
        else {}
    )
    return {
        "table": "memory_atoms",
        "pk": "id",
        "pkValue": atom_id,
        "previous": previous,
        "createdAliasIds": created_alias_ids,
        "previousAliases": previous_aliases,
        "previousAtomTags": previous_atom_tags,
        "autoSuperseded": auto_superseded,
        "autoSupersededBooks": [],
        "dependencyInvalidation": dependency_invalidation,
        "supersessionRollbacks": supersession_rollbacks,
        "personalEvidenceGovernance": evidence_governance,
        **memberships,
    }


def _apply_personal_atom_evidence_governance(
    conn: sqlite3.Connection,
    *,
    payload: Mapping[str, object],
    atom_id: str,
    previous_atom: Mapping[str, object],
    relation: str,
) -> dict[str, object]:
    evidence_ids = _unique_strings(_strings(payload.get("evidenceIds")), limit=1_500)
    run_id = compact_whitespace(str(payload.get("curationRunId") or ""))
    if not evidence_ids or not run_id:
        raise ValueError("curated Atom requires canonical Evidence and a curation run")
    run = conn.execute(
        """
        SELECT session_id, provider, model_id, thinking_level
        FROM memory_curation_model_runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if run is None or not compact_whitespace(str(run["session_id"] or "")):
        raise ValueError("curated Atom run has no auditable Session")
    placeholders = ",".join("?" for _ in evidence_ids)
    evidence_rows = conn.execute(
        f"""
        SELECT evidence_id, content_sha256, origin_kind, boundary_kind,
               admission_revision
        FROM agent_memory_evidence AS evidence
        WHERE evidence.evidence_id IN ({placeholders})
          AND {admitted_personal_evidence_sql('evidence')}
        ORDER BY evidence.evidence_id
        """,
        tuple(evidence_ids),
    ).fetchall()
    if {str(row["evidence_id"]) for row in evidence_rows} != set(evidence_ids):
        raise ValueError("curated Atom references missing or non-admitted Evidence")
    if relation not in {"supports", "corrects", "retracts"}:
        raise ValueError("unsupported curated Atom Evidence relation")

    operation = {
        "supports": "remember_preview",
        "corrects": "correct_preview",
        "retracts": "forget_preview",
    }[relation]
    canonical = compact_whitespace(
        str(payload.get("canonicalText") or previous_atom.get("canonical_text") or "")
    )
    proposal_seed = json.dumps(
        {
            "runId": run_id,
            "atomId": atom_id,
            "relation": relation,
            "evidenceIds": evidence_ids,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    proposal_id = (
        "proposal:curation:"
        + hashlib.sha256(proposal_seed.encode("utf-8")).hexdigest()[:32]
    )
    previous_proposal = _row_dict(
        conn.execute(
            "SELECT * FROM memory_governance_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
    )
    previous_links = [
        _row_dict(row)
        for row in conn.execute(
            """
            SELECT * FROM memory_atom_evidence_links
            WHERE proposal_id = ?
            ORDER BY evidence_id, relation
            """,
            (proposal_id,),
        ).fetchall()
    ]
    timestamp = now_ms()
    evidence_snapshot = [
        {
            "evidenceId": str(row["evidence_id"]),
            "contentSha256": str(row["content_sha256"]),
            "originKind": str(row["origin_kind"]),
            "boundaryKind": str(row["boundary_kind"]),
            "admissionRevision": int(row["admission_revision"] or 0),
        }
        for row in evidence_rows
    ]
    atom_first = (
        compact_whitespace(str(payload.get("curationArchitecture") or ""))
        == MEMORY_CURATION_ARCHITECTURE
    )
    action_source = (
        "memory_curation_atom_first"
        if atom_first
        else "personal_memory_curation_v2"
    )
    action = {
        "source": action_source,
        "runId": run_id,
        "atomId": atom_id,
        "relation": relation,
        "evidenceIds": evidence_ids,
        "provider": str(run["provider"] or ""),
        "modelId": str(run["model_id"] or ""),
        "thinkingLevel": str(run["thinking_level"] or ""),
    }
    payload_sha256 = hashlib.sha256(
        json.dumps(
            action,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    conn.execute(
        """
        INSERT INTO memory_governance_proposals(
            proposal_id, session_id, project, operation, target_memory_id,
            memory_kind, proposed_text, reason, evidence_ids_json,
            evidence_snapshot_json, target_snapshot_json, action_json,
            payload_sha256, idempotency_key, status, applied_memory_id,
            receipt_json, created_at_ms, expires_at_ms, updated_at_ms,
            applied_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?,
                  ?, ?, ?, ?)
        ON CONFLICT(proposal_id) DO UPDATE SET
            session_id = excluded.session_id,
            operation = excluded.operation,
            target_memory_id = excluded.target_memory_id,
            memory_kind = excluded.memory_kind,
            proposed_text = excluded.proposed_text,
            reason = excluded.reason,
            evidence_ids_json = excluded.evidence_ids_json,
            evidence_snapshot_json = excluded.evidence_snapshot_json,
            target_snapshot_json = excluded.target_snapshot_json,
            action_json = excluded.action_json,
            payload_sha256 = excluded.payload_sha256,
            idempotency_key = excluded.idempotency_key,
            status = 'applied',
            applied_memory_id = excluded.applied_memory_id,
            receipt_json = excluded.receipt_json,
            updated_at_ms = excluded.updated_at_ms,
            applied_at_ms = excluded.applied_at_ms
        """,
        (
            proposal_id,
            str(run["session_id"]),
            compact_whitespace(str(payload.get("project") or "")),
            operation,
            (
                compact_whitespace(str(payload.get("supersedesId") or atom_id))
                if relation == "corrects"
                else atom_id
                if relation == "retracts"
                else ""
            ),
            compact_whitespace(str(payload.get("kind") or previous_atom.get("kind") or "")),
            canonical,
            f"{action_source}:{relation}",
            json.dumps(evidence_ids, ensure_ascii=False, separators=(",", ":")),
            json.dumps(evidence_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(dict(previous_atom), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            payload_sha256,
            f"{action_source}:{payload_sha256}",
            atom_id,
            json.dumps(
                {"applied": True, "runId": run_id, "relation": relation},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            timestamp,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    conn.execute(
        "DELETE FROM memory_atom_evidence_links WHERE proposal_id = ?",
        (proposal_id,),
    )
    for row in evidence_rows:
        conn.execute(
            """
            INSERT INTO memory_atom_evidence_links(
                memory_atom_id, evidence_id, proposal_id, relation,
                content_sha256, provenance_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                atom_id,
                str(row["evidence_id"]),
                proposal_id,
                relation,
                str(row["content_sha256"]),
                json.dumps(
                    {
                        "runId": run_id,
                        "protocol": (
                            MEMORY_CURATION_ARCHITECTURE
                            if atom_first
                            else "personal-v2"
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                timestamp,
            ),
        )
    return {
        "proposalId": proposal_id,
        "previousProposal": previous_proposal,
        "previousLinks": previous_links,
    }


def _rollback_personal_atom_evidence_governance(
    conn: sqlite3.Connection,
    rollback: object,
) -> None:
    if not isinstance(rollback, Mapping):
        return
    proposal_id = compact_whitespace(str(rollback.get("proposalId") or ""))
    if not proposal_id:
        return
    conn.execute(
        "DELETE FROM memory_atom_evidence_links WHERE proposal_id = ?",
        (proposal_id,),
    )
    previous_proposal = rollback.get("previousProposal")
    if isinstance(previous_proposal, dict) and previous_proposal:
        _insert_or_replace_dict(conn, "memory_governance_proposals", previous_proposal)
        for row in rollback.get("previousLinks") or []:
            if isinstance(row, dict):
                _insert_or_replace_dict(conn, "memory_atom_evidence_links", row)
    else:
        conn.execute(
            "DELETE FROM memory_governance_proposals WHERE proposal_id = ?",
            (proposal_id,),
        )




def _apply_tag_edge(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    src_id = _ensure_tag(conn, str(payload.get("src") or ""), source="dsv4")
    dst_id = _ensure_tag(conn, str(payload.get("dst") or ""), source="dsv4")
    edge_type = str(payload.get("edgeType") or "related")
    previous = _row_dict(
        conn.execute(
            "SELECT * FROM memory_tag_edges WHERE src_tag_id = ? AND dst_tag_id = ? AND edge_type = ?",
            (src_id, dst_id, edge_type),
        ).fetchone()
    )
    conn.execute(
        """
        INSERT INTO memory_tag_edges(src_tag_id, dst_tag_id, edge_type, weight, direction_bias, evidence_count, updated_at_ms, metadata_json)
        VALUES (?, ?, ?, ?, 0.0, ?, ?, ?)
        ON CONFLICT(src_tag_id, dst_tag_id, edge_type) DO UPDATE SET
            weight = excluded.weight,
            evidence_count = excluded.evidence_count,
            updated_at_ms = excluded.updated_at_ms,
            metadata_json = excluded.metadata_json
        """,
        (
            src_id,
            dst_id,
            edge_type,
            _bounded_float(payload.get("weight"), default=0.5),
            max(1, len(_positive_ints(payload.get("evidenceEventIds")))),
            now_ms(),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    return {
        "edge": {"srcTagId": src_id, "dstTagId": dst_id, "edgeType": edge_type},
        "previous": previous,
    }


def _lossless_json_blob(values: list[object]) -> str:
    raw_values = [
        str(value)
        for value in values
        if value is not None and str(value)
    ]
    if not raw_values:
        return "{}"
    parsed: list[object] = []
    for raw in raw_values:
        try:
            parsed.append(json.loads(raw))
        except json.JSONDecodeError:
            parsed.append(raw)
    if all(item == parsed[0] for item in parsed[1:]):
        return raw_values[0]
    return json.dumps({"merged": parsed}, ensure_ascii=False, sort_keys=True)
def _lossless_edge_metadata(
    existing: Mapping[str, object],
    source: Mapping[str, object],
) -> str:
    existing_fields = dict(existing)
    source_fields = dict(source)
    metadata = _lossless_json_blob(
        [existing_fields.get("metadata_json"), source_fields.get("metadata_json")]
    )
    try:
        existing_bias = float(existing_fields.get("direction_bias") or 0.0)
        source_bias = float(source_fields.get("direction_bias") or 0.0)
    except (TypeError, ValueError):
        existing_bias = source_bias = 0.0
    if existing_bias == source_bias:
        return metadata
    return json.dumps(
        {
            "mergedMetadata": metadata,
            "directionBias": [existing_bias, source_bias],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _apply_semantic_tag_merge(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    source_name = compact_whitespace(str(payload.get("source") or ""))
    target_name = compact_whitespace(str(payload.get("target") or ""))
    source_tag_id = _optional_int(payload.get("sourceTagId"))
    target_tag_id = _optional_int(payload.get("targetTagId"))
    source = (
        conn.execute(
            "SELECT * FROM memory_tags WHERE id = ?",
            (source_tag_id,),
        ).fetchone()
        if source_tag_id > 0
        else conn.execute(
            "SELECT * FROM memory_tags WHERE tag = ? OR normalized_tag = ? "
            "ORDER BY CASE WHEN tag = ? THEN 0 ELSE 1 END, quality_score DESC LIMIT 1",
            (source_name, normalize_text(source_name), source_name),
        ).fetchone()
    )
    target = (
        conn.execute(
            "SELECT * FROM memory_tags WHERE id = ?",
            (target_tag_id,),
        ).fetchone()
        if target_tag_id > 0
        else conn.execute(
            "SELECT * FROM memory_tags WHERE tag = ? OR normalized_tag = ? "
            "ORDER BY CASE WHEN tag = ? THEN 0 ELSE 1 END, quality_score DESC LIMIT 1",
            (target_name, normalize_text(target_name), target_name),
        ).fetchone()
    )
    if source is None or target is None:
        # Older stored drafts could contain a model-proposed merge whose
        # source only existed in the same compile output. The compiler already
        # canonicalized that virtual tag into the target, so there is no row
        # left to move. Treat this as an observable no-op instead of aborting
        # every other reviewed diff in the transaction.
        missing = "source" if source is None else "target"
        return {
            "noOp": True,
            "reason": f"missing_{missing}",
            "source": source_name,
            "target": target_name,
        }
    source_id = int(source["id"])
    target_id = int(target["id"])
    if source_id == target_id:
        raise ValueError("semantic Tag merge resolves to the same physical tag")

    snapshot = {
        "sourceTagId": source_id,
        "targetTagId": target_id,
        "tags": [dict(source), dict(target)],
        "profiles": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM memory_tag_profiles WHERE tag_id IN (?, ?)",
                (source_id, target_id),
            ).fetchall()
        ],
        "itemTags": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM memory_item_tags WHERE tag_id IN (?, ?)",
                (source_id, target_id),
            ).fetchall()
        ],
        "atomTags": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM memory_atom_tags WHERE CAST(tag_id AS INTEGER) IN (?, ?)",
                (source_id, target_id),
            ).fetchall()
        ],
        "edges": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM memory_tag_edges WHERE src_tag_id IN (?, ?) OR dst_tag_id IN (?, ?)",
                (source_id, target_id, source_id, target_id),
            ).fetchall()
        ],
        "groupMembers": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM memory_semantic_group_members "
                "WHERE member_type = 'tag' AND member_id IN (?, ?)",
                (str(source_id), str(target_id)),
            ).fetchall()
        ],
        "bookTags": [
            {"book_id": str(row["book_id"]), "tags_json": str(row["tags_json"] or "[]")}
            for row in conn.execute("SELECT book_id, tags_json FROM memory_books").fetchall()
            if source_name in _json_list(row["tags_json"])
        ],
    }
    for edge in snapshot["edges"]:
        mapped_src = target_id if int(edge["src_tag_id"]) == source_id else int(edge["src_tag_id"])
        mapped_dst = target_id if int(edge["dst_tag_id"]) == source_id else int(edge["dst_tag_id"])
        if mapped_src == mapped_dst:
            raise ValueError("semantic Tag merge would create a self edge")
    timestamp = now_ms()
    source_tag = snapshot["tags"][0]
    target_tag = snapshot["tags"][1]
    conn.execute(
        "UPDATE memory_tags SET metadata_json = ? WHERE id = ?",
        (
            _lossless_json_blob(
                [target_tag.get("metadata_json"), source_tag.get("metadata_json")]
            ),
            target_id,
        ),
    )

    for row in conn.execute(
        "SELECT memory_item_id, weight, position, evidence FROM memory_item_tags WHERE tag_id = ?",
        (source_id,),
    ).fetchall():
        existing = conn.execute(
            "SELECT weight, position, evidence FROM memory_item_tags WHERE memory_item_id = ? AND tag_id = ?",
            (row["memory_item_id"], target_id),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO memory_item_tags(memory_item_id, tag_id, weight, position, evidence) VALUES (?, ?, ?, ?, ?)",
                (row["memory_item_id"], target_id, row["weight"], row["position"], row["evidence"]),
            )
        else:
            conn.execute(
                "UPDATE memory_item_tags SET weight = MAX(weight, ?), position = MIN(position, ?), evidence = ? "
                "WHERE memory_item_id = ? AND tag_id = ?",
                (
                    row["weight"],
                    row["position"],
                    _lossless_relation_scalar(
                        [existing["evidence"], row["evidence"]]
                    ),
                    row["memory_item_id"],
                    target_id,
                ),
            )
    conn.execute("DELETE FROM memory_item_tags WHERE tag_id = ?", (source_id,))

    for row in conn.execute(
        "SELECT memory_atom_id, weight, source FROM memory_atom_tags WHERE CAST(tag_id AS INTEGER) = ?",
        (source_id,),
    ).fetchall():
        existing = conn.execute(
            "SELECT weight, source FROM memory_atom_tags WHERE memory_atom_id = ? AND CAST(tag_id AS INTEGER) = ?",
            (row["memory_atom_id"], target_id),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO memory_atom_tags(memory_atom_id, tag_id, weight, source) VALUES (?, ?, ?, ?)",
                (row["memory_atom_id"], str(target_id), row["weight"], row["source"]),
            )
        else:
            conn.execute(
                "UPDATE memory_atom_tags SET weight = MAX(weight, ?), source = ? "
                "WHERE memory_atom_id = ? AND CAST(tag_id AS INTEGER) = ?",
                (
                    row["weight"],
                    _lossless_relation_scalar([existing["source"], row["source"]]),
                    row["memory_atom_id"],
                    target_id,
                ),
            )
    conn.execute("DELETE FROM memory_atom_tags WHERE CAST(tag_id AS INTEGER) = ?", (source_id,))

    source_edges = conn.execute(
        "SELECT * FROM memory_tag_edges WHERE src_tag_id = ? OR dst_tag_id = ?",
        (source_id, source_id),
    ).fetchall()
    conn.execute("DELETE FROM memory_tag_edges WHERE src_tag_id = ? OR dst_tag_id = ?", (source_id, source_id))
    for row in source_edges:
        src_id = target_id if int(row["src_tag_id"]) == source_id else int(row["src_tag_id"])
        dst_id = target_id if int(row["dst_tag_id"]) == source_id else int(row["dst_tag_id"])
        if src_id == dst_id:
            raise ValueError("semantic Tag merge would create a self edge")
        existing = conn.execute(
            "SELECT weight, direction_bias, evidence_count, metadata_json FROM memory_tag_edges "
            "WHERE src_tag_id = ? AND dst_tag_id = ? AND edge_type = ?",
            (src_id, dst_id, row["edge_type"]),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO memory_tag_edges(src_tag_id, dst_tag_id, edge_type, weight, direction_bias, "
                "evidence_count, updated_at_ms, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    src_id,
                    dst_id,
                    row["edge_type"],
                    row["weight"],
                    row["direction_bias"],
                    row["evidence_count"],
                    timestamp,
                    row["metadata_json"],
                ),
            )
        else:
            conn.execute(
                "UPDATE memory_tag_edges SET weight = MAX(weight, ?), "
                "direction_bias = ?, evidence_count = evidence_count + ?, "
                "updated_at_ms = ?, metadata_json = ? "
                "WHERE src_tag_id = ? AND dst_tag_id = ? AND edge_type = ?",
                (
                    row["weight"],
                    existing["direction_bias"],
                    row["evidence_count"],
                    timestamp,
                    _lossless_edge_metadata(existing, row),
                    src_id,
                    dst_id,
                    row["edge_type"],
                ),
            )

    for row in conn.execute(
        "SELECT group_id, weight, source FROM memory_semantic_group_members "
        "WHERE member_type = 'tag' AND member_id = ?",
        (str(source_id),),
    ).fetchall():
        existing = conn.execute(
            "SELECT weight, source FROM memory_semantic_group_members "
            "WHERE group_id = ? AND member_type = 'tag' AND member_id = ?",
            (row["group_id"], str(target_id)),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO memory_semantic_group_members(group_id, member_type, member_id, weight, source, updated_at_ms) "
                "VALUES (?, 'tag', ?, ?, ?, ?)",
                (row["group_id"], str(target_id), row["weight"], row["source"], timestamp),
            )
        else:
            conn.execute(
                "UPDATE memory_semantic_group_members SET weight = MAX(weight, ?), "
                "source = ?, updated_at_ms = ? "
                "WHERE group_id = ? AND member_type = 'tag' AND member_id = ?",
                (
                    row["weight"],
                    _lossless_relation_scalar([existing["source"], row["source"]]),
                    timestamp,
                    row["group_id"],
                    str(target_id),
                ),
            )
    conn.execute(
        "DELETE FROM memory_semantic_group_members WHERE member_type = 'tag' AND member_id = ?",
        (str(source_id),),
    )

    source_profile = next((row for row in snapshot["profiles"] if int(row["tag_id"]) == source_id), {})
    target_profile = next((row for row in snapshot["profiles"] if int(row["tag_id"]) == target_id), {})
    aliases = _unique_strings(
        [
            *_json_list(target_profile.get("aliases_json")),
            source_name,
            *_json_list(source_profile.get("aliases_json")),
        ],
        limit=None,
    )
    conn.execute(
        "INSERT INTO memory_tag_profiles(tag_id, color_token, aliases_json, updated_at_ms) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(tag_id) DO UPDATE SET aliases_json = excluded.aliases_json, updated_at_ms = excluded.updated_at_ms",
        (
            target_id,
            str(target_profile.get("color_token") or source_profile.get("color_token") or "blue"),
            json.dumps(aliases, ensure_ascii=False),
            timestamp,
        ),
    )
    conn.execute("DELETE FROM memory_tag_profiles WHERE tag_id = ?", (source_id,))
    for row in snapshot["bookTags"]:
        tags = [target_name if tag == source_name else tag for tag in _json_list(row["tags_json"])]
        conn.execute(
            "UPDATE memory_books SET tags_json = ?, updated_at_ms = ? WHERE book_id = ?",
            (json.dumps(_unique_strings(tags, limit=None), ensure_ascii=False), timestamp, row["book_id"]),
        )
    conn.execute("DELETE FROM memory_tags WHERE id = ?", (source_id,))
    return snapshot


def _rollback_semantic_tag_merge(conn: sqlite3.Connection, rollback: dict[str, object]) -> None:
    if rollback.get("noOp"):
        return
    source_id = int(rollback.get("sourceTagId") or 0)
    target_id = int(rollback.get("targetTagId") or 0)
    if not source_id or not target_id:
        return
    for row in _list_of_dicts(rollback.get("tags")):
        _insert_or_replace_dict(conn, "memory_tags", row)
    conn.execute("DELETE FROM memory_tag_profiles WHERE tag_id IN (?, ?)", (source_id, target_id))
    for row in _list_of_dicts(rollback.get("profiles")):
        _insert_or_replace_dict(conn, "memory_tag_profiles", row)
    conn.execute("DELETE FROM memory_item_tags WHERE tag_id IN (?, ?)", (source_id, target_id))
    for row in _list_of_dicts(rollback.get("itemTags")):
        _insert_or_replace_dict(conn, "memory_item_tags", row)
    conn.execute("DELETE FROM memory_atom_tags WHERE CAST(tag_id AS INTEGER) IN (?, ?)", (source_id, target_id))
    for row in _list_of_dicts(rollback.get("atomTags")):
        _insert_or_replace_dict(conn, "memory_atom_tags", row)
    conn.execute(
        "DELETE FROM memory_tag_edges WHERE src_tag_id IN (?, ?) OR dst_tag_id IN (?, ?)",
        (source_id, target_id, source_id, target_id),
    )
    for row in _list_of_dicts(rollback.get("edges")):
        _insert_or_replace_dict(conn, "memory_tag_edges", row)
    conn.execute(
        "DELETE FROM memory_semantic_group_members WHERE member_type = 'tag' AND member_id IN (?, ?)",
        (str(source_id), str(target_id)),
    )
    for row in _list_of_dicts(rollback.get("groupMembers")):
        _insert_or_replace_dict(conn, "memory_semantic_group_members", row)
    for row in _list_of_dicts(rollback.get("bookTags")):
        conn.execute(
            "UPDATE memory_books SET tags_json = ? WHERE book_id = ?",
            (str(row.get("tags_json") or "[]"), str(row.get("book_id") or "")),
        )


def _insert_or_replace_dict(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    if not row:
        return
    columns = list(row.keys())
    conn.execute(
        f"INSERT OR REPLACE INTO {table}({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [row[column] for column in columns],
    )


def _apply_phrase_candidate(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    memory_id = str(payload["memoryId"])
    previous = _row_dict(conn.execute("SELECT * FROM memory_items WHERE memory_id = ?", (memory_id,)).fetchone())
    _assert_memory_owner_unchanged(previous, payload, target_kind="phrase")
    text = str(payload.get("text") or "")
    evidence_ids = _positive_ints(payload.get("sourceEventIds"))
    timestamp = now_ms()
    upsert_memory_item(
        conn,
        memory_id=memory_id,
        kind="phrase",
        text=text,
        normalized_text=normalize_text(text),
        summary="memory-book phrase candidate",
        source_event_id=evidence_ids[0] if evidence_ids else None,
        project=str(payload.get("project") or ""),
        app="",
        confidence=max(0.55, _bounded_float(payload.get("weight"), default=0.6)),
        quality_score=max(0.55, _bounded_float(payload.get("weight"), default=0.6)),
        status="approved",
        privacy_class="local",
        created_at_ms=timestamp,
        updated_at_ms=timestamp,
        metadata={**payload, "direct_candidate_allowed": True, "source": "memory_book_compile"},
        tags=tuple(_strings(payload.get("tags"))),
        embedding_provider=None,
        tag_source="dsv4",
        owner_kind=str(payload.get("ownerKind") or "user"),
        owner_id=str(payload.get("ownerId") or "default"),
    )
    memberships = _sync_semantic_group_members(
        conn,
        member_type="phrase",
        member_id=memory_id,
        group_ids=_strings(payload.get("semanticGroupIds")),
    )
    return {"table": "memory_items", "pk": "memory_id", "pkValue": memory_id, "previous": previous, **memberships}


def _apply_negative_phrase(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    suppression_id = str(payload["suppressionId"])
    previous = _row_dict(
        conn.execute("SELECT * FROM memory_candidate_suppressions WHERE id = ?", (suppression_id,)).fetchone()
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO memory_candidate_suppressions(
            id, match_type, match_value, action, reason, strength, expires_at_ms, created_at_ms
        ) VALUES (?, 'text', ?, 'block', ?, 1.0, NULL, ?)
        """,
        (suppression_id, str(payload.get("text") or ""), str(payload.get("reason") or ""), now_ms()),
    )
    return {
        "table": "memory_candidate_suppressions",
        "pk": "id",
        "pkValue": suppression_id,
        "previous": previous,
    }


def _record_memory_supersession(
    conn: sqlite3.Connection,
    *,
    old_id: str,
    new_id: str,
    source_event_ids: list[int],
    reason: str,
) -> dict[str, object]:
    supersession_id = f"supersession:{stable_text_hash(f'{old_id}->{new_id}').removeprefix('sha256:')[:24]}"
    previous_relation = _row_dict(
        conn.execute(
            "SELECT * FROM memory_supersessions WHERE supersession_id = ?",
            (supersession_id,),
        ).fetchone()
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO memory_supersessions(
            supersession_id, old_memory_id, new_memory_id, reason,
            source_event_ids_json, status, created_at_ms, rolled_back_at_ms,
            metadata_json
        ) VALUES (?, ?, ?, ?, ?, 'active', ?, NULL, ?)
        """,
        (
            supersession_id,
            old_id,
            new_id,
            compact_whitespace(reason) or "newer_explicit_information",
            json.dumps(source_event_ids, ensure_ascii=False),
            now_ms(),
            json.dumps({"source": "memory_book_compile"}, ensure_ascii=False, sort_keys=True),
        ),
    )
    return {
        "table": "memory_supersessions",
        "pk": "supersession_id",
        "pkValue": supersession_id,
        "previous": previous_relation,
    }


def _apply_supersede_memory(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    old_id = str(payload["oldId"])
    new_id = str(payload["newId"])
    prior = conn.execute("SELECT * FROM memory_atoms WHERE id=?", (old_id,)).fetchone()
    already_replaced = conn.execute(
        "SELECT 1 FROM memory_supersessions WHERE old_memory_id=? AND new_memory_id=? AND status='active'",
        (old_id, new_id),
    ).fetchone()
    if prior is not None and already_replaced is None:
        assert_background_card_unchanged(dict(prior), payload.get("expectedRevision"),
                                         allow_historical_merge=bool(payload.get("globalCatalogMerge")))
    relation_rollback = _record_memory_supersession(
        conn,
        old_id=old_id,
        new_id=new_id,
        source_event_ids=_positive_ints(payload.get("sourceEventIds")),
        reason=compact_whitespace(
            str(payload.get("reason") or "newer_explicit_information")
        ),
    )
    atom = conn.execute("SELECT * FROM memory_atoms WHERE id = ?", (old_id,)).fetchone()
    if atom is not None:
        previous = _row_dict(atom)
        timestamp = now_ms()
        conn.execute(
            """
            UPDATE memory_atoms
            SET status = 'superseded',
                claim_state = 'superseded',
                valid_to_ms = COALESCE(valid_to_ms, ?),
                updated_at_ms = ?
            WHERE id = ?
            """,
            (timestamp, timestamp, old_id),
        )
        dependency_invalidation = invalidate_superseded_atom_dependencies(
            conn,
            [old_id],
            new_atom_id=new_id,
            timestamp=timestamp,
        )
        return {
            "table": "memory_atoms",
            "pk": "id",
            "pkValue": old_id,
            "previous": previous,
            "supersession": relation_rollback,
            "dependencyInvalidation": dependency_invalidation,
        }
    item = conn.execute("SELECT * FROM memory_items WHERE memory_id = ?", (old_id,)).fetchone()
    if item is not None:
        previous = _row_dict(item)
        conn.execute("UPDATE memory_items SET status = 'hidden', updated_at_ms = ? WHERE memory_id = ?", (now_ms(), old_id))
        return {
            "table": "memory_items",
            "pk": "memory_id",
            "pkValue": old_id,
            "previous": previous,
            "supersession": relation_rollback,
        }
    conn.execute(
        "DELETE FROM memory_supersessions WHERE supersession_id = ?",
        (str(relation_rollback["pkValue"]),),
    )
    raise ValueError(f"superseded memory does not exist: {old_id}")


def _apply_retract_memory_atom(
    conn: sqlite3.Connection,
    payload: dict[str, object],
) -> dict[str, object]:
    target_id = compact_whitespace(str(payload.get("targetAtomId") or ""))
    previous = _row_dict(
        conn.execute(
            "SELECT * FROM memory_atoms WHERE id = ?",
            (target_id,),
        ).fetchone()
    )
    if not previous:
        raise ValueError(f"memory atom does not exist: {target_id}")
    assert_background_card_unchanged(previous, payload.get("expectedRevision"))
    _assert_memory_owner_unchanged(previous, payload, target_kind="atom")
    project = compact_whitespace(str(payload.get("project") or ""))
    atom_project = compact_whitespace(str(previous.get("scope_project") or ""))
    if project and atom_project and project != atom_project:
        raise ValueError("memory atom belongs to another project")
    if (
        compact_whitespace(str(previous.get("claim_state") or "current"))
        != "current"
        or compact_whitespace(str(previous.get("status") or ""))
        not in {"active", "approved"}
    ):
        raise ValueError("memory atom is no longer current")

    timestamp = now_ms()
    conn.execute(
        """
        UPDATE memory_atoms
        SET status = 'tombstoned', claim_state = 'retracted',
            valid_to_ms = ?, updated_at_ms = ?
        WHERE id = ? AND claim_state = 'current'
          AND status IN ('active', 'approved')
        """,
        (timestamp, timestamp, target_id),
    )
    if int(conn.execute("SELECT changes()").fetchone()[0]) != 1:
        raise ValueError("memory atom changed before retraction")

    dependency_invalidation = invalidate_superseded_atom_dependencies(
        conn,
        [target_id],
        timestamp=timestamp,
    )

    tombstone = conn.execute(
        """
        INSERT INTO memory_tombstones(
            created_at_ms, target_type, target_value, reason, active, metadata_json
        ) VALUES (?, 'memory_id', ?, ?, 1, ?)
        """,
        (
            timestamp,
            target_id,
            compact_whitespace(
                str(payload.get("reason") or "explicit_user_forget")
            ),
            json.dumps(
                {
                    "source": "memory_book_compile",
                    "sourceEventIds": _positive_ints(
                        payload.get("sourceEventIds")
                    ),
                    "ownerKind": str(payload.get("ownerKind") or ""),
                    "ownerId": str(payload.get("ownerId") or ""),
                    "project": project,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )

    affected_books: list[dict[str, object]] = []
    for row in conn.execute(
        """
        SELECT *
        FROM memory_books
        WHERE owner_kind = ? AND owner_id = ?
          AND status IN ('active', 'approved')
        """,
        (
            str(payload.get("ownerKind") or "user"),
            str(payload.get("ownerId") or "default"),
        ),
    ).fetchall():
        atom_ids = _json_list(row["memory_atom_ids_json"])
        if target_id not in atom_ids:
            continue
        # Retraction preserves the historical owner-scoped cleanup behavior:
        # an existing Book that explicitly contains the Atom must lose that
        # member even when legacy rows predate the newer full scope metadata.
        affected_books.append(dict(row))
        remaining_ids = [atom_id for atom_id in atom_ids if atom_id != target_id]
        active_rows = (
            conn.execute(
                f"""
                SELECT id, canonical_text, text
                FROM memory_atoms
                WHERE id IN ({','.join('?' for _ in remaining_ids)})
                  AND status IN ('active', 'approved')
                  AND claim_state = 'current'
                ORDER BY updated_at_ms DESC, id ASC
                """,
                tuple(remaining_ids),
            ).fetchall()
            if remaining_ids
            else []
        )
        active_ids = [str(atom_row["id"]) for atom_row in active_rows]
        summary = "；".join(
            compact_whitespace(
                str(atom_row["canonical_text"] or atom_row["text"] or "")
            )
            for atom_row in active_rows
            if compact_whitespace(
                str(atom_row["canonical_text"] or atom_row["text"] or "")
            )
        )[:2400]
        if active_ids:
            metadata = _json_object(row["metadata_json"])
            for stale_key in (
                "retrievalStale",
                "retrievalStaleReason",
                "retrievalStaleAtMs",
            ):
                metadata.pop(stale_key, None)
            conn.execute(
                """
                UPDATE memory_books
                SET summary = ?, normalized_text = ?,
                    memory_atom_ids_json = ?, metadata_json = ?, updated_at_ms = ?,
                    last_active_at_ms = ?
                WHERE book_id = ?
                """,
                (
                    summary,
                    normalize_text(f"{row['title']} {summary}"),
                    json.dumps(active_ids, ensure_ascii=False),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    timestamp,
                    str(row["book_id"]),
                ),
            )
        else:
            conn.execute(
                """
                UPDATE memory_books
                SET summary = '', normalized_text = '',
                    memory_atom_ids_json = '[]', status = 'archived',
                    archived_at_ms = ?, updated_at_ms = ?,
                    archive_reason = 'empty_after_memory_retraction'
                WHERE book_id = ?
                """,
                (timestamp, timestamp, str(row["book_id"])),
            )

    evidence_governance = (
        _apply_personal_atom_evidence_governance(
            conn,
            payload=payload,
            atom_id=target_id,
            previous_atom=previous,
            relation="retracts",
        )
        if compact_whitespace(str(payload.get("knowledgeDomain") or ""))
        == "personal_memory"
        else {}
    )

    return {
        "atom": previous,
        "books": affected_books,
        "tombstoneId": int(tombstone.lastrowid),
        "dependencyInvalidation": dependency_invalidation,
        "personalEvidenceGovernance": evidence_governance,
    }


def _restore_or_delete_row(conn: sqlite3.Connection, *, table: str, pk: str, rollback: dict[str, object]) -> None:
    pk_value = str(rollback.get("pkValue") or "")
    previous = rollback.get("previous")
    if isinstance(previous, dict) and previous:
        columns = list(previous.keys())
        assignments = ", ".join(f"{col} = ?" for col in columns)
        values = [previous[col] for col in columns]
        conn.execute(f"INSERT OR REPLACE INTO {table}({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", values)
        return
    if pk_value:
        conn.execute(f"DELETE FROM {table} WHERE {pk} = ?", (pk_value,))


def _sync_semantic_group_members(
    conn: sqlite3.Connection,
    *,
    member_type: str,
    member_id: str,
    group_ids: list[str],
) -> dict[str, object]:
    previous = [
        dict(row)
        for row in conn.execute(
            """
            SELECT group_id, member_type, member_id, weight, source, updated_at_ms
            FROM memory_semantic_group_members
            WHERE member_type = ? AND member_id = ?
            """,
            (member_type, member_id),
        ).fetchall()
    ]
    conn.execute(
        "DELETE FROM memory_semantic_group_members WHERE member_type = ? AND member_id = ?",
        (member_type, member_id),
    )
    timestamp = now_ms()
    for group_id in dict.fromkeys(_strings(group_ids)):
        if conn.execute(
            "SELECT 1 FROM memory_semantic_groups WHERE group_id = ? AND status = 'active'",
            (group_id,),
        ).fetchone() is None:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_semantic_group_members(
                group_id, member_type, member_id, weight, source, updated_at_ms
            ) VALUES (?, ?, ?, 0.8, 'dsv4', ?)
            """,
            (group_id, member_type, member_id, timestamp),
        )
    return {
        "semanticMemberType": member_type,
        "semanticMemberId": member_id,
        "previousSemanticGroupMembers": previous,
    }


def _restore_semantic_group_members(conn: sqlite3.Connection, rollback: dict[str, object]) -> None:
    member_type = compact_whitespace(str(rollback.get("semanticMemberType") or ""))
    member_id = compact_whitespace(str(rollback.get("semanticMemberId") or ""))
    if not member_type or not member_id:
        return
    conn.execute(
        "DELETE FROM memory_semantic_group_members WHERE member_type = ? AND member_id = ?",
        (member_type, member_id),
    )
    for row in _list_of_dicts(rollback.get("previousSemanticGroupMembers")):
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_semantic_group_members(
                group_id, member_type, member_id, weight, source, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(row.get("group_id") or ""),
                str(row.get("member_type") or member_type),
                str(row.get("member_id") or member_id),
                _bounded_float(row.get("weight"), default=0.8),
                str(row.get("source") or "dsv4"),
                int(row.get("updated_at_ms") or now_ms()),
            ),
        )


def _ensure_tag(conn: sqlite3.Connection, tag: str, *, source: str = "dsv4") -> int:
    value = compact_whitespace(tag)
    row = conn.execute(
        "SELECT id FROM memory_tags WHERE tag = ? OR normalized_tag = ? "
        "ORDER BY CASE WHEN tag = ? THEN 0 ELSE 1 END, quality_score DESC LIMIT 1",
        (value, normalize_text(value), value),
    ).fetchone()
    if row is not None:
        tag_id = int(row["id"])
        if source == "dsv4":
            conn.execute(
                """
                UPDATE memory_tags
                SET source = CASE WHEN source = 'user' THEN 'user' ELSE 'dsv4' END,
                    status = 'active', updated_at_ms = ?
                WHERE id = ?
                """,
                (now_ms(), tag_id),
            )
        return tag_id
    cur = conn.execute(
        """
        INSERT INTO memory_tags(
            tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms,
            description, source, status, metadata_json
        )
        VALUES (?, ?, 'concept', 0.6, ?, ?, '', ?, 'active', '{}')
        """,
        (value, normalize_text(value), now_ms(), now_ms(), source),
    )
    return int(cur.lastrowid)


def _sync_run_status(conn: sqlite3.Connection, run_id: str) -> str:
    rows = conn.execute("SELECT status FROM memory_cleanup_diffs WHERE run_id = ?", (run_id,)).fetchall()
    statuses = {str(row["status"]) for row in rows}
    effective_statuses = statuses - {"rejected"}
    if not statuses:
        status = "empty"
    elif not effective_statuses:
        run = conn.execute(
            "SELECT run_kind FROM memory_cleanup_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        run_kind = "" if run is None else compact_whitespace(str(run["run_kind"] or ""))
        status = "empty" if run_kind in {"daily_curation", "manual_curation"} else "dismissed"
    elif effective_statuses == {"applied"}:
        status = "applied"
    elif effective_statuses == {"rolled_back"}:
        status = "rolled_back"
    elif "applied" in effective_statuses:
        status = "partial"
    else:
        status = "draft"
    conn.execute("UPDATE memory_cleanup_runs SET status = ? WHERE run_id = ?", (status, run_id))
    return status


def _synthesize_daily_book_diff_from_diffs(
    diffs: list[dict[str, object]],
    *,
    project: str,
    source_bundle: dict[str, object],
) -> dict[str, object] | None:
    atom_payloads = [
        diff.get("payload")
        for diff in diffs
        if diff.get("op") == "upsert_memory_atom" and isinstance(diff.get("payload"), dict)
    ]
    if not atom_payloads:
        return None
    source_ids: list[int] = []
    tags: list[str] = []
    hints: list[str] = []
    atom_ids: list[str] = []
    semantic_group_ids: list[str] = []
    for payload in atom_payloads:
        if not isinstance(payload, dict):
            continue
        for event_id in _positive_ints(payload.get("sourceEventIds")):
            if event_id not in source_ids:
                source_ids.append(event_id)
        tags.extend(_strings(payload.get("tags")))
        hints.extend(_strings(payload.get("surfaceHints")))
        semantic_group_ids.extend(_strings(payload.get("semanticGroupIds")))
        atom_id = compact_whitespace(str(payload.get("atomId") or ""))
        if atom_id and atom_id not in atom_ids:
            atom_ids.append(atom_id)
    for diff in diffs:
        if diff.get("op") != "add_phrase_candidate" or not isinstance(diff.get("payload"), dict):
            continue
        text = compact_whitespace(str(diff["payload"].get("text") or ""))
        if 2 <= len(text) <= 16:
            hints.append(text)
        for event_id in _positive_ints(diff["payload"].get("sourceEventIds")):
            if event_id not in source_ids:
                source_ids.append(event_id)
    if not source_ids:
        source_ids = _infer_source_event_ids(hints, source_bundle=source_bundle, limit=6)
    if not source_ids:
        return None
    book_key = _default_book_key(source_bundle)
    book_id = f"book:daily:{book_key}"
    unique_hints = [item for item in _unique_strings(hints, limit=6) if 2 <= len(item) <= 16]
    payload = {
        "bookId": book_id,
        "bookType": "daily",
        "bookKey": book_key,
        "title": f"本地记忆归档 {book_key}",
        "summary": (
            f"按来源事件归档 {len(source_ids)} 条记录，保留 {len(atom_payloads)} 个去重记忆项。"
        ),
        "tags": _unique_strings(tags, limit=8),
        "surfaceHints": unique_hints[:3],
        "queryExpansions": [],
        "sourceEventIds": source_ids[:10],
        "memoryAtomIds": atom_ids,
        "semanticGroupIds": _unique_strings(semantic_group_ids, limit=4),
        "project": project,
        "app": "",
        "confidence": 0.6,
        "qualityScore": 0.6,
        "status": "active",
    }
    return {"op": "upsert_memory_book", "targetId": book_id, "payload": payload, "status": "pending"}


def _unique_strings(values: list[str], *, limit: int | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = compact_whitespace(value)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
        if limit is not None and len(result) >= limit:
            break
    return result


def _default_book_key(source_bundle: dict[str, object] | None) -> str:
    events = _source_event_records(source_bundle)
    for event in events:
        created_at_ms = event.get("createdAtMs")
        if isinstance(created_at_ms, int) and created_at_ms > 0:
            return datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(now_ms() / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _infer_source_event_ids(
    texts: list[str],
    *,
    source_bundle: dict[str, object] | None,
    limit: int = 3,
) -> list[int]:
    events = _source_event_records(source_bundle)
    if not events:
        return []
    tokens: set[str] = set()
    for text in texts:
        tokens.update(_evidence_tokens(text))
    if not tokens:
        return [int(event["eventId"]) for event in events[:limit]]
    scored: list[tuple[int, int, int]] = []
    for index, event in enumerate(events):
        event_tokens = _evidence_tokens(str(event.get("text") or ""))
        overlap = len(tokens.intersection(event_tokens))
        if overlap > 0:
            scored.append((-overlap, index, int(event["eventId"])))
    if scored:
        return [event_id for _, _, event_id in sorted(scored)[:limit]]
    return [int(event["eventId"]) for event in events[:limit]]


def _source_event_records(source_bundle: dict[str, object] | None) -> list[dict[str, object]]:
    if not isinstance(source_bundle, dict):
        return []
    raw_events = source_bundle.get("recentEvents")
    if not isinstance(raw_events, list):
        return []
    events: list[dict[str, object]] = []
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        event_ids = _positive_ints(item.get("sourceEventIds") or [item.get("eventId")])
        if not event_ids:
            continue
        committed_text = compact_whitespace(str(item.get("text") or ""))
        recent_context = compact_whitespace(str(item.get("recentContext") or ""))
        tags = _strings(item.get("tags"))
        text = compact_whitespace(
            " ".join(
                [
                    committed_text,
                    recent_context,
                    " ".join(tags),
                    str(item.get("app") or ""),
                    str(item.get("project") or ""),
                    str(item.get("source") or ""),
                ]
            )
        )
        events.append(
            {
                "eventId": event_ids[0],
                "sourceEventIds": event_ids,
                "createdAtMs": _optional_int(item.get("createdAtMs")),
                "text": text,
                "committedText": committed_text,
                "recentContext": recent_context,
                "tags": tags,
                "contextGroupId": compact_whitespace(str(item.get("contextGroupId") or "")),
            }
        )
    return events


def _evidence_tokens(text: str) -> set[str]:
    value = compact_whitespace(text).lower()
    if not value:
        return set()
    tokens = {
        token
        for token in re.findall(r"[a-z][a-z0-9_+.-]{1,}|[0-9]+(?:\.[0-9]+)?", normalize_text(value))
        if len(token) >= 2
    }
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        for size in (2, 3):
            if len(segment) < size:
                continue
            tokens.update(segment[index : index + size] for index in range(0, len(segment) - size + 1))
    for stop in ("用户", "要求", "需要", "这个", "那个", "当前", "目前", "项目", "可以", "应该", "使用", "进行"):
        tokens.discard(stop)
    return tokens


def _optional_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _memory_book_summary(diffs: list[dict[str, object]]) -> str:
    counts = {
        "分组": sum(1 for item in diffs if item.get("op") == "upsert_semantic_group"),
        "标签": sum(1 for item in diffs if item.get("op") == "upsert_semantic_tag"),
        "主题": sum(1 for item in diffs if item.get("op") == "upsert_memory_book"),
        "主题合并": sum(1 for item in diffs if item.get("op") == "merge_memory_books"),
        "记忆": sum(1 for item in diffs if item.get("op") == "upsert_memory_atom"),
        "标签关系": sum(1 for item in diffs if item.get("op") == "upsert_tag_edge"),
        "标签合并": sum(1 for item in diffs if item.get("op") == "merge_semantic_tag"),
        "词表提案": sum(1 for item in diffs if item.get("op") == "add_phrase_candidate"),
    }
    visible = [f"{label} {count}" for label, count in counts.items() if count]
    return "；".join(visible) if visible else "没有生成可入库的高置信变更"



def _sanitize_catalog_text(
    value: object,
    *,
    max_chars: int,
    uncapped: bool,
) -> str:
    return _sanitize_text(
        str(value or ""),
        max_chars=1_000_000 if uncapped else max_chars,
    )[0]

def _sanitize_text(text: str, *, max_chars: int) -> tuple[str, dict[str, int]]:
    counts = _empty_redaction_counts()
    value = text or ""
    value, private_key_count = _PRIVATE_KEY_RE.subn("[REDACTED_SECRET]", value)
    counts["secret"] += private_key_count
    value, url_secret_count = _URL_SECRET_QUERY_RE.subn(
        lambda match: f"{match.group('separator')}[REDACTED_SECRET]",
        value,
    )
    counts["secret"] += url_secret_count
    value, assignment_count = _CREDENTIAL_ASSIGNMENT_RE.subn("[REDACTED_SECRET]", value)
    counts["secret"] += assignment_count
    value = compact_whitespace(value)
    value, counts["identity"] = _IDENTITY_RE.subn("[REDACTED_IDENTITY]", value)
    value, counts["phone"] = _PHONE_RE.subn("[REDACTED_PHONE]", value)
    value, counts["paymentCard"] = _PAYMENT_CARD_RE.subn("[REDACTED_PAYMENT_CARD]", value)
    value, counts["path"] = _PATH_RE.subn("[REDACTED_PATH]", value)
    value, counts["email"] = _EMAIL_RE.subn("[REDACTED_EMAIL]", value)
    value, ip_count = _redact_ip_addresses(value)
    counts["ipAddress"] += ip_count
    value, bare_secret_count = _SECRET_RE.subn("[REDACTED_SECRET]", value)
    counts["secret"] += bare_secret_count
    return truncate_text(value, max_chars), counts


def _empty_redaction_counts() -> dict[str, int]:
    return {
        "secret": 0,
        "path": 0,
        "email": 0,
        "phone": 0,
        "identity": 0,
        "paymentCard": 0,
        "ipAddress": 0,
    }


def _redact_ip_addresses(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        candidate = match.group(0)
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return candidate
        count += 1
        return "[REDACTED_IP]"

    value = _IPV4_CANDIDATE_RE.sub(replace, text)
    value = _IPV6_CANDIDATE_RE.sub(replace, value)
    return value, count


def _contains_sensitive_text(text: str) -> bool:
    value = text or ""
    if any(
        pattern.search(value)
        for pattern in (
            _PRIVATE_KEY_RE,
            _URL_SECRET_QUERY_RE,
            _CREDENTIAL_ASSIGNMENT_RE,
            _SECRET_RE,
            _PHONE_RE,
            _IDENTITY_RE,
            _PAYMENT_CARD_RE,
            _PATH_RE,
            _EMAIL_RE,
        )
    ):
        return True
    _, count = _redact_ip_addresses(value)
    return count > 0


def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = int(target.get(key, 0)) + int(value)


def _json_list(raw: object) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return _strings(parsed)


def _json_object(raw: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _list_of_dicts(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value or [] if isinstance(item, dict)]


def _memory_book_field(item: Mapping[str, object], field: str) -> str:
    aliases = {
        "owner_kind": ("ownerKind", "owner_kind"),
        "owner_id": ("ownerId", "owner_id"),
        "project": ("project", "scope_project"),
        "app": ("app", "scope_app"),
        "knowledge_domain": ("knowledgeDomain", "knowledge_domain"),
        "scope_kind": ("scopeKind", "scope_kind"),
        "scope_id": ("scopeId", "scope_id"),
        "visibility": ("visibility",),
        "authorization_revision": (
            "authorizationRevision",
            "authorization_revision",
        ),
        "binding_id": ("bindingId", "binding_id"),
        "scope_mode": ("scopeMode", "scope_mode"),
    }
    candidates = aliases.get(field, (field,))
    for candidate in candidates:
        if candidate in item:
            return compact_whitespace(str(item.get(candidate) or ""))
    return ""


def _memory_books_same_scope(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    """Compare authorization scope, not a derived resource's own identity.

    Personal projected Books deliberately have one binding per resource while
    their authorization is the same global ``user/default`` domain as the
    personal Atoms.  Other Books retain the historical exact binding check.
    """

    identity_fields = (
        "owner_kind",
        "owner_id",
        "project",
        "app",
        "knowledge_domain",
        "scope_kind",
        "scope_id",
        "visibility",
        "scope_mode",
    )
    if any(
        _memory_book_field(left, field) != _memory_book_field(right, field)
        for field in identity_fields
    ):
        return False
    personal = (
        _memory_book_field(left, "knowledge_domain") == "personal_memory"
        and _memory_book_field(right, "knowledge_domain") == "personal_memory"
    )
    if not personal:
        return (
            _memory_book_field(left, "authorization_revision")
            == _memory_book_field(right, "authorization_revision")
            and _memory_book_field(left, "binding_id")
            == _memory_book_field(right, "binding_id")
        )

    def valid_personal_book_binding(item: Mapping[str, object]) -> bool:
        book_id = compact_whitespace(
            str(item.get("book_id") or item.get("bookId") or "")
        )
        return (
            _memory_book_field(item, "authorization_revision") == "memory-book-v1"
            and bool(book_id)
            and _memory_book_field(item, "binding_id")
            == f"personal-memory-book:{book_id}"
        )

    return valid_personal_book_binding(left) and valid_personal_book_binding(right)


def _memory_book_atom_scope_matches(
    book: Mapping[str, object],
    atom: Mapping[str, object],
) -> bool:
    """Check an Atom against the Book's effective authorization scope.

    A Book with an empty ``app`` is intentionally app-unbounded; Atom app
    provenance remains in the Atom row and is not collapsed during a merge.
    Personal projected Books and personal Atoms have different resource
    bindings (one is per Book, one is per user), so those binding IDs are
    validated as a compatible authority family rather than compared as if
    they were the same resource.
    """

    for book_field, atom_field in (
        ("owner_kind", "owner_kind"),
        ("owner_id", "owner_id"),
        ("project", "project"),
        ("knowledge_domain", "knowledge_domain"),
        ("scope_kind", "scope_kind"),
        ("scope_id", "scope_id"),
        ("visibility", "visibility"),
        ("scope_mode", "scope_mode"),
    ):
        if _memory_book_field(book, book_field) != _memory_book_field(
            atom, atom_field
        ):
            return False
    book_app = _memory_book_field(book, "app")
    atom_app = _memory_book_field(atom, "app")
    if book_app and book_app != atom_app:
        return False

    book_domain = _memory_book_field(book, "knowledge_domain")
    atom_domain = _memory_book_field(atom, "knowledge_domain")
    if book_domain != "personal_memory" or atom_domain != "personal_memory":
        return (
            _memory_book_field(book, "authorization_revision")
            == _memory_book_field(atom, "authorization_revision")
            and _memory_book_field(book, "binding_id")
            == _memory_book_field(atom, "binding_id")
        )

    book_id = compact_whitespace(
        str(book.get("book_id") or book.get("bookId") or "")
    )
    atom_owner_kind = _memory_book_field(atom, "owner_kind")
    atom_owner_id = _memory_book_field(atom, "owner_id")
    return (
        _memory_book_field(book, "authorization_revision") == "memory-book-v1"
        and bool(book_id)
        and _memory_book_field(book, "binding_id")
        == f"personal-memory-book:{book_id}"
        and _memory_book_field(atom, "authorization_revision") == "memory-atom-v2"
        and _memory_book_field(atom, "binding_id")
        == f"personal-memory:{atom_owner_kind}:{atom_owner_id}"
    )


def _memory_book_merge_sources(payload: Mapping[str, object]) -> list[str]:
    raw = payload.get("sourceBookIds") or payload.get("sourceIds")
    if raw is None:
        raw = [
            payload.get("sourceBookId")
            or payload.get("sourceRef")
            or payload.get("source")
        ]
    return list(
        dict.fromkeys(
            compact_whitespace(str(value or ""))
            for value in (raw if isinstance(raw, (list, tuple, set)) else [raw])
            if compact_whitespace(str(value or ""))
        )
    )


def _planned_memory_book_merges(
    compile_output: Mapping[str, object],
    *,
    source_bundle: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    """Resolve explicit Book references into deterministic merge diffs."""

    source = source_bundle or {}
    global_catalog = (
        compact_whitespace(str(source.get("curationScope") or "")).lower()
        == "global"
        and bool(source.get("catalogAudit"))
        and bool(source.get("catalogComplete", True))
    )
    if not global_catalog:
        return []
    books = _list_of_dicts(source.get("existingMemoryBooks"))
    if not books:
        return []
    by_id = {
        compact_whitespace(str(item.get("bookId") or "")): item
        for item in books
        if compact_whitespace(str(item.get("bookId") or ""))
    }
    by_key = {
        compact_whitespace(str(item.get("bookKey") or "")): item
        for item in books
        if compact_whitespace(str(item.get("bookKey") or ""))
    }
    by_ref = {
        f"B{index}": item
        for index, item in enumerate(books, start=1)
        if compact_whitespace(str(item.get("bookId") or ""))
    }

    def resolve(value: object) -> dict[str, object] | None:
        reference = compact_whitespace(str(value or ""))
        if not reference:
            return None
        return by_ref.get(reference) or by_id.get(reference) or by_key.get(reference)

    raw_items: list[dict[str, object]] = []
    for key in ("bookMerges", "topicBookMerges", "mergeBooks"):
        raw_items.extend(_list_of_dicts(compile_output.get(key)))
    result: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    by_target: dict[str, dict[str, object]] = {}
    for item in raw_items:
        target = resolve(
            item.get("targetBookId")
            or item.get("targetRef")
            or item.get("target")
        )
        if target is None:
            continue
        target_id = compact_whitespace(str(target.get("bookId") or ""))
        if (
            not target_id
            or compact_whitespace(str(target.get("bookType") or "topic")) != "topic"
            or compact_whitespace(str(target.get("status") or "")) not in {"active", "approved"}
        ):
            continue
        reason = compact_whitespace(
            str(item.get("reason") or item.get("semanticReason") or "")
        )[:360]
        confidence = _bounded_float(item.get("confidence"), default=0.0)
        if not reason or confidence < 0.8:
            continue
        accepted: list[str] = []
        for reference in _memory_book_merge_sources(item):
            source_item = resolve(reference)
            if source_item is None:
                continue
            source_id = compact_whitespace(str(source_item.get("bookId") or ""))
            if (
                not source_id
                or source_id == target_id
                or source_id in seen_sources
                or compact_whitespace(str(source_item.get("bookType") or "topic")) != "topic"
                or compact_whitespace(str(source_item.get("status") or "")) not in {"active", "approved"}
                or not _memory_books_same_scope(source_item, target)
            ):
                continue
            accepted.append(source_id)
        if not accepted:
            continue
        current = by_target.setdefault(
            target_id,
            {
                "targetBookId": target_id,
                "sourceBookIds": [],
                "ownerKind": _memory_book_field(target, "owner_kind"),
                "ownerId": _memory_book_field(target, "owner_id"),
                "project": _memory_book_field(target, "project"),
                "reason": reason,
                "confidence": confidence,
            },
        )
        for source_id in accepted:
            if source_id not in current["sourceBookIds"]:
                current["sourceBookIds"].append(source_id)
                seen_sources.add(source_id)
    result.extend(by_target.values())
    return result


def _planned_tag_merges(
    compile_output: dict[str, object],
    *,
    source_bundle: dict[str, object] | None,
) -> list[dict[str, object]]:
    existing = _list_of_dicts((source_bundle or {}).get("existingSemanticTags"))
    global_catalog = (
        compact_whitespace(
            str((source_bundle or {}).get("curationScope") or "")
        ).lower()
        == "global"
        and bool((source_bundle or {}).get("catalogAudit"))
        and bool((source_bundle or {}).get("catalogComplete"))
    )
    tags_by_id = {
        _optional_int(item.get("tagId")): item
        for item in existing
        if _optional_int(item.get("tagId")) > 0
    }
    tags_by_normalized_name: dict[str, list[dict[str, object]]] = {}
    canonical_by_name: dict[str, str] = {}
    for item in existing:
        name = compact_whitespace(str(item.get("name") or ""))
        normalized = normalize_text(name)
        if not normalized:
            continue
        tags_by_normalized_name.setdefault(normalized, []).append(item)
        canonical_by_name.setdefault(normalized, name)
    proposed_names = {
        normalize_text(str(item.get("name") or item.get("tag") or ""))
        for item in _list_of_dicts(compile_output.get("semanticTags"))
        if normalize_text(str(item.get("name") or item.get("tag") or ""))
    }

    merges: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    merge_target_by_source: dict[str, str] = {}

    def add_merge(
        source: object,
        target: object,
        *,
        reason: object,
        source_tag_id: object = 0,
        target_tag_id: object = 0,
        evidence_event_ids: object = None,
        confidence: object = 0.8,
    ) -> None:
        source_id = _optional_int(source_tag_id)
        target_id = _optional_int(target_tag_id)
        source_item = tags_by_id.get(source_id)
        target_item = tags_by_id.get(target_id)
        source_name = compact_whitespace(
            str((source_item or {}).get("name") or source or "")
        )
        target_name = compact_whitespace(
            str((target_item or {}).get("name") or target or "")
        )
        source_norm = normalize_text(source_name)
        target_norm = normalize_text(target_name)
        if not source_norm or not target_norm:
            return
        if source_id <= 0:
            source_candidates = tags_by_normalized_name.get(source_norm, [])
            if len(source_candidates) == 1:
                source_item = source_candidates[0]
                source_id = _optional_int(source_item.get("tagId"))
                source_name = compact_whitespace(
                    str(source_item.get("name") or source_name)
                )
                source_norm = normalize_text(source_name)
            else:
                source_id = 0
        if target_id <= 0:
            target_candidates = tags_by_normalized_name.get(target_norm, [])
            if len(target_candidates) == 1:
                target_item = target_candidates[0]
                target_id = _optional_int(target_item.get("tagId"))
                target_name = compact_whitespace(
                    str(target_item.get("name") or target_name)
                )
                target_norm = normalize_text(target_name)
            else:
                target_id = 0
                target_name = canonical_by_name.get(target_norm, target_name)
                target_norm = normalize_text(target_name)
        source_key = f"id:{source_id}" if source_id else f"name:{source_norm}"
        target_key = f"id:{target_id}" if target_id else f"name:{target_norm}"
        if source_key == target_key or source_key in seen_sources:
            return
        if (
            target_id <= 0
            and target_norm not in canonical_by_name
            and target_norm not in proposed_names
        ):
            return
        cursor = target_key
        visited: set[str] = set()
        while cursor and cursor not in visited:
            if cursor == source_key:
                return
            visited.add(cursor)
            cursor = merge_target_by_source.get(cursor, "")
        seen_sources.add(source_key)
        merge_target_by_source[source_key] = target_key
        merges.append(
            {
                "source": source_name,
                "target": target_name,
                **({"sourceTagId": source_id} if source_id else {}),
                **({"targetTagId": target_id} if target_id else {}),
                "reason": compact_whitespace(str(reason or "同义标签规范化")),
                "evidenceEventIds": _positive_ints(evidence_event_ids),
                "confidence": _bounded_float(confidence, default=0.8),
                "requiresApply": (
                    source_id in tags_by_id
                    if source_id
                    else source_norm in canonical_by_name
                ),
            }
        )

    for item in _list_of_dicts(compile_output.get("tagMerges")):
        add_merge(
            item.get("source") or item.get("from"),
            item.get("target") or item.get("into"),
            source_tag_id=item.get("sourceTagId"),
            target_tag_id=item.get("targetTagId"),
            reason=item.get("reason"),
            evidence_event_ids=item.get("evidenceEventIds"),
            confidence=item.get("confidence"),
        )

    # Exact normalized-name duplicates are safe to coalesce by physical id.
    # The source bundle is already ordered by quality and recency, so the first
    # row is the canonical target and later rows become aliases of it.
    for duplicate_items in (
        tags_by_normalized_name.values() if global_catalog else ()
    ):
        if len(duplicate_items) < 2:
            continue
        target_item = duplicate_items[0]
        for source_item in duplicate_items[1:]:
            add_merge(
                source_item.get("name"),
                target_item.get("name"),
                source_tag_id=source_item.get("tagId"),
                target_tag_id=target_item.get("tagId"),
                reason="规范化名称重复",
                confidence=0.99,
            )

    # Declared aliases are also deterministic catalog evidence. They do not
    # authorize fuzzy, related, parent/child, or co-occurrence merges.
    for target_item in existing:
        target_name = compact_whitespace(str(target_item.get("name") or ""))
        for alias in _strings(target_item.get("aliases")):
            alias_items = tags_by_normalized_name.get(normalize_text(alias), [])
            for source_item in alias_items:
                add_merge(
                    source_item.get("name"),
                    target_name,
                    source_tag_id=source_item.get("tagId"),
                    target_tag_id=target_item.get("tagId"),
                    reason="已有标签别名重合",
                    confidence=0.95,
                )

    for item in _list_of_dicts(compile_output.get("semanticTags")):
        name = compact_whitespace(str(item.get("name") or item.get("tag") or ""))
        for candidate in [name, *_strings(item.get("aliases"))]:
            existing_items = tags_by_normalized_name.get(
                normalize_text(candidate),
                [],
            )
            if existing_items and normalize_text(name) != normalize_text(
                str(existing_items[0].get("name") or "")
            ):
                add_merge(
                    name,
                    existing_items[0].get("name"),
                    target_tag_id=existing_items[0].get("tagId"),
                    reason="本批标签与已有规范标签或别名重合",
                    evidence_event_ids=item.get("sourceEventIds"),
                    confidence=item.get("confidence"),
                )
                break
    return _resolve_tag_merge_terminals(merges)

def _tag_merge_key(name: object, tag_id: object) -> str:
    physical_id = _optional_int(tag_id)
    normalized = normalize_text(str(name or ""))
    return f"id:{physical_id}" if physical_id > 0 else f"name:{normalized}"


def _resolve_tag_merge_terminals(
    merges: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Rewrite alias chains so every physical merge deletes only a leaf."""
    target_by_source: dict[str, str] = {}
    endpoint_by_key: dict[str, tuple[str, int]] = {}
    for merge in merges:
        source_name = compact_whitespace(str(merge.get("source") or ""))
        target_name = compact_whitespace(str(merge.get("target") or ""))
        source_key = _tag_merge_key(source_name, merge.get("sourceTagId"))
        target_key = _tag_merge_key(target_name, merge.get("targetTagId"))
        if not source_name or not target_name or source_key == target_key:
            continue
        target_by_source.setdefault(source_key, target_key)
        endpoint_by_key.setdefault(
            source_key,
            (source_name, _optional_int(merge.get("sourceTagId"))),
        )
        endpoint_by_key.setdefault(
            target_key,
            (target_name, _optional_int(merge.get("targetTagId"))),
        )

    def terminal_for(key: str) -> str:
        visited: set[str] = set()
        current = key
        while current in target_by_source and current not in visited:
            visited.add(current)
            current = target_by_source[current]
        return current

    resolved: list[dict[str, object]] = []
    for merge in merges:
        source_name = compact_whitespace(str(merge.get("source") or ""))
        target_name = compact_whitespace(str(merge.get("target") or ""))
        source_key = _tag_merge_key(source_name, merge.get("sourceTagId"))
        target_key = _tag_merge_key(target_name, merge.get("targetTagId"))
        terminal_key = terminal_for(target_key)
        if terminal_key == source_key:
            # The planner already rejects cycles; keep this guard here so a
            # malformed legacy graph cannot reintroduce a self-merge.
            continue
        terminal = endpoint_by_key.get(terminal_key)
        if terminal is None:
            resolved.append(dict(merge))
            continue
        terminal_name, terminal_id = terminal
        rewritten = dict(merge)
        rewritten["target"] = terminal_name
        if terminal_id > 0:
            rewritten["targetTagId"] = terminal_id
        else:
            rewritten.pop("targetTagId", None)
        resolved.append(rewritten)
    return resolved



def _canonical_tag_name(value: str, mapping: dict[str, str]) -> str:
    current = compact_whitespace(value)
    visited: set[str] = set()
    while current:
        normalized = normalize_text(current)
        if not normalized or normalized in visited:
            break
        visited.add(normalized)
        target = compact_whitespace(mapping.get(normalized, ""))
        if not target or normalize_text(target) == normalized:
            break
        current = target
    return current


def _canonical_tag_names(values: list[str], mapping: dict[str, str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        canonical = _canonical_tag_name(value, mapping)
        normalized = normalize_text(canonical)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(canonical)
    return result


def _canonical_tag_aliases(name: str, aliases: list[str], mapping: dict[str, str]) -> list[str]:
    name_normalized = normalize_text(name)
    return _unique_strings(
        [alias for alias in aliases if normalize_text(alias) and normalize_text(alias) != name_normalized],
        limit=24,
    )


def _strings(value: object) -> list[str]:
    return [compact_whitespace(str(item)) for item in value or [] if compact_whitespace(str(item))]


def _positive_ints(value: object) -> list[int]:
    result: list[int] = []
    for item in value or []:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in result:
            result.append(number)
    return result


def _optional_positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _bounded_float(value: object, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _row_dict(row: sqlite3.Row | None) -> dict[str, object]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _ensure_compile_state_table(conn: sqlite3.Connection) -> None:
    apply_database_migrations(conn)


def _record_draft_compile_state(conn: sqlite3.Connection, *, plan: dict[str, object]) -> None:
    metadata = dict(plan.get("metadata") or {})
    # ``memory_compile_state`` is the cursor owned by the legacy whole-book
    # compiler. Owner/daily Atom-first runs have their own audited curation
    # cursor and must not replay database migrations merely to store a draft.
    if compact_whitespace(str(metadata.get("runKind") or "legacy")) != "legacy":
        return
    project = compact_whitespace(str(metadata.get("project") or ""))
    cursor = dict(metadata.get("sourceCursor") or {})
    to_event_id = max(0, int(cursor.get("toEventId") or 0))
    run_id = compact_whitespace(str(plan.get("runId") or ""))
    if not project or to_event_id <= 0 or not run_id:
        return
    _ensure_compile_state_table(conn)
    pending = int(
        conn.execute(
            """SELECT COUNT(*) FROM input_events e
               LEFT JOIN memory_state s ON s.event_id = e.id
               WHERE e.id > COALESCE(
                   (SELECT last_compiled_event_id FROM memory_compile_state WHERE project = ?),
                   0
               )
                 AND (? = '' OR e.project = ? OR e.project = '')
                 AND COALESCE(s.deleted, 0) = 0""",
            (project, project, project),
        ).fetchone()[0]
    )
    timestamp = now_ms()
    bundle_hash = compact_whitespace(str(metadata.get("bundleHash") or ""))
    conn.execute(
        """
        INSERT INTO memory_compile_state(
            project, last_compiled_event_id, last_run_ms, pending_event_count,
            last_bundle_hash, last_drafted_event_id, last_draft_ms,
            last_draft_bundle_hash, last_draft_run_id
        )
        VALUES (?, 0, 0, ?, '', ?, ?, ?, ?)
        ON CONFLICT(project) DO UPDATE SET
            pending_event_count = excluded.pending_event_count,
            last_drafted_event_id = MAX(
                memory_compile_state.last_drafted_event_id,
                excluded.last_drafted_event_id
            ),
            last_draft_ms = excluded.last_draft_ms,
            last_draft_bundle_hash = excluded.last_draft_bundle_hash,
            last_draft_run_id = excluded.last_draft_run_id
        """,
        (project, pending, to_event_id, timestamp, bundle_hash, run_id),
    )


def _advance_compile_state(conn: sqlite3.Connection, *, plan: dict[str, object]) -> None:
    metadata = dict(plan.get("metadata") or {})
    if compact_whitespace(str(metadata.get("runKind") or "legacy")) != "legacy":
        return
    project = compact_whitespace(str(metadata.get("project") or ""))
    cursor = dict(metadata.get("sourceCursor") or {})
    to_event_id = max(0, int(cursor.get("toEventId") or 0))
    if not project or to_event_id <= 0:
        return
    _ensure_compile_state_table(conn)
    pending = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM input_events AS e
            WHERE e.id > ? AND (? = '' OR e.project = ? OR e.project = '')
            """,
            (to_event_id, project, project),
        ).fetchone()[0]
    )
    conn.execute(
        """
        INSERT INTO memory_compile_state(
            project, last_compiled_event_id, last_run_ms, pending_event_count,
            last_bundle_hash, last_drafted_event_id, last_draft_ms,
            last_draft_bundle_hash, last_draft_run_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project) DO UPDATE SET
            last_compiled_event_id = MAX(memory_compile_state.last_compiled_event_id, excluded.last_compiled_event_id),
            last_run_ms = excluded.last_run_ms,
            pending_event_count = excluded.pending_event_count,
            last_bundle_hash = excluded.last_bundle_hash,
            last_drafted_event_id = MAX(memory_compile_state.last_drafted_event_id, excluded.last_drafted_event_id),
            last_draft_ms = MAX(memory_compile_state.last_draft_ms, excluded.last_draft_ms),
            last_draft_bundle_hash = excluded.last_draft_bundle_hash,
            last_draft_run_id = excluded.last_draft_run_id
        """,
        (
            project,
            to_event_id,
            now_ms(),
            pending,
            compact_whitespace(str(metadata.get("bundleHash") or "")),
            to_event_id,
            now_ms(),
            compact_whitespace(str(metadata.get("bundleHash") or "")),
            compact_whitespace(str(plan.get("runId") or "")),
        ),
    )


def _source_feedback(
    conn: sqlite3.Connection,
    *,
    event_ids: list[int],
    project: str,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    if not event_ids:
        return [], _empty_redaction_counts()
    rows = conn.execute(
        """
        SELECT action, candidate_id, candidate_text, candidate_source, raw_input,
               preedit, committed_tail, created_at_ms, metadata_json
        FROM memory_feedback_events
        ORDER BY created_at_ms DESC
        LIMIT 80
        """
    ).fetchall()
    wanted_event_ids = set(event_ids)
    result: list[dict[str, object]] = []
    redaction_stats = _empty_redaction_counts()
    for row in rows:
        metadata = _json_object(row["metadata_json"])
        metadata_project = compact_whitespace(str(metadata.get("project") or ""))
        if project and metadata_project and metadata_project != project:
            continue
        source_event_id = _optional_positive_int(metadata.get("sourceEventId"))
        if source_event_id and source_event_id not in wanted_event_ids:
            continue
        text, text_counts = _sanitize_text(str(row["candidate_text"] or ""), max_chars=100)
        raw_input, raw_counts = _sanitize_text(str(row["raw_input"] or ""), max_chars=80)
        preedit, preedit_counts = _sanitize_text(str(row["preedit"] or ""), max_chars=80)
        committed_tail, tail_counts = _sanitize_text(str(row["committed_tail"] or ""), max_chars=180)
        selected_text, selected_counts = _sanitize_text(str(metadata.get("selectedText") or ""), max_chars=100)
        for counts in (text_counts, raw_counts, preedit_counts, tail_counts, selected_counts):
            _merge_counts(redaction_stats, counts)
        result.append(
            {
                "action": str(row["action"] or ""),
                "candidateId": _sanitize_text(str(row["candidate_id"] or ""), max_chars=120)[0],
                "text": text,
                "source": str(row["candidate_source"] or ""),
                "rawInput": raw_input,
                "preedit": preedit,
                "committedTail": committed_tail,
                "selectedText": selected_text,
                "selectedRank": _optional_positive_int(metadata.get("selectedRank")) or 0,
                "deleteCount": _optional_positive_int(metadata.get("deleteCount")) or 0,
                "sourceEventId": source_event_id,
                "createdAtMs": int(row["created_at_ms"] or 0),
            }
        )
    return result, redaction_stats


def _source_rime_rank_feedback(
    conn: sqlite3.Connection,
    *,
    project: str,
    cutoff_ms: int,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    rows = conn.execute(
        """
        SELECT preedit, rejected_text, accepted_text, action, app, project,
               candidate_rank, created_at_ms, metadata_json
        FROM rime_rank_feedback
        WHERE created_at_ms >= ?
          AND (? = '' OR project = ? OR project = '')
        ORDER BY created_at_ms DESC, id DESC
        LIMIT 80
        """,
        (cutoff_ms, project, project),
    ).fetchall()
    result: list[dict[str, object]] = []
    redaction_stats = _empty_redaction_counts()
    for row in rows:
        preedit, preedit_counts = _sanitize_text(str(row["preedit"] or ""), max_chars=80)
        rejected, rejected_counts = _sanitize_text(str(row["rejected_text"] or ""), max_chars=100)
        accepted, accepted_counts = _sanitize_text(str(row["accepted_text"] or ""), max_chars=100)
        app, app_counts = _sanitize_text(str(row["app"] or ""), max_chars=120)
        for counts in (preedit_counts, rejected_counts, accepted_counts, app_counts):
            _merge_counts(redaction_stats, counts)
        result.append(
            {
                "action": str(row["action"] or ""),
                "preedit": preedit,
                "rejectedText": rejected,
                "acceptedText": accepted,
                "candidateRank": int(row["candidate_rank"] or 0),
                "app": app,
                "createdAtMs": int(row["created_at_ms"] or 0),
            }
        )
    return result, redaction_stats


def _existing_book_summaries(
    conn: sqlite3.Connection,
    *,
    project: str,
    catalog_only: bool = False,
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT book_id, book_type, book_key, title, summary, project, app,
               tags_json, surface_hints_json, query_expansions_json,
               source_event_ids_json, memory_atom_ids_json, status,
               created_at_ms, updated_at_ms, archived_at_ms, archive_reason, metadata_json,
               owner_kind, owner_id, knowledge_domain, scope_kind, scope_id,
               visibility, authorization_revision, binding_id, scope_mode
        FROM memory_books
        WHERE (
                status IN ('active', 'approved', 'archived')
                OR (? = 1 AND status = 'superseded')
              )
          AND (
                ? = 1
                OR ? = ''
                OR project = ?
                OR project = ''
              )
        ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                 updated_at_ms DESC, book_id ASC
        LIMIT ?
        """,
        (
            1 if catalog_only else 0,
            1 if catalog_only else 0,
            project,
            project,
            -1 if catalog_only else 48,
        ),
    ).fetchall()
    return [
        {
            "bookId": str(row["book_id"] or ""),
            "bookType": str(row["book_type"] or ""),
            "bookKey": str(row["book_key"] or ""),
            "aliases": _unique_strings(
                _strings(_json_object(row["metadata_json"]).get("topicAliases")),
                limit=None if catalog_only else 32,
            ),
            "supersededByBookId": compact_whitespace(
                str(_json_object(row["metadata_json"]).get("supersededByBookId") or "")
            ),
            "title": _sanitize_catalog_text(
                row["title"],
                max_chars=80,
                uncapped=catalog_only,
            ),
            "summary": _sanitize_catalog_text(
                row["summary"],
                max_chars=180,
                uncapped=catalog_only,
            ),
            "contentHash": stable_text_hash(
                json.dumps(
                    {
                        "title": row["title"],
                        "summary": row["summary"],
                        "tags": row["tags_json"],
                        "surfaceHints": row["surface_hints_json"],
                        "queryExpansions": row["query_expansions_json"],
                        "sourceEventIds": row["source_event_ids_json"],
                        "memoryAtomIds": row["memory_atom_ids_json"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
            "tags": [
                _sanitize_catalog_text(tag, max_chars=48, uncapped=catalog_only)
                for tag in _json_list(row["tags_json"])
            ],
            "surfaceHints": [
                _sanitize_catalog_text(
                    item,
                    max_chars=48,
                    uncapped=catalog_only,
                )
                for item in _json_list(row["surface_hints_json"])
            ],
            "queryExpansions": [
                _sanitize_catalog_text(
                    item,
                    max_chars=80,
                    uncapped=catalog_only,
                )
                for item in _json_list(row["query_expansions_json"])
            ],
            "sourceEventIds": _json_list(row["source_event_ids_json"]),
            "memoryAtomIds": _json_list(row["memory_atom_ids_json"]),
            "semanticGroupIds": [
                str(member[0])
                for member in conn.execute(
                    "SELECT group_id FROM memory_semantic_group_members "
                    "WHERE member_type = 'book' AND member_id = ? "
                    "ORDER BY weight DESC, group_id ASC LIMIT ?",
                    (str(row["book_id"] or ""), -1 if catalog_only else 8),
                ).fetchall()
            ],
            "project": _sanitize_catalog_text(
                row["project"],
                max_chars=120,
                uncapped=catalog_only,
            ),
            "app": _sanitize_catalog_text(
                row["app"],
                max_chars=120,
                uncapped=catalog_only,
            ),
            "ownerKind": str(row["owner_kind"] or ""),
            "ownerId": str(row["owner_id"] or ""),
            "knowledgeDomain": str(row["knowledge_domain"] or ""),
            "scopeKind": str(row["scope_kind"] or ""),
            "scopeId": str(row["scope_id"] or ""),
            "visibility": str(row["visibility"] or ""),
            "authorizationRevision": str(row["authorization_revision"] or ""),
            "bindingId": str(row["binding_id"] or ""),
            "scopeMode": str(row["scope_mode"] or ""),
            "metadataHash": stable_text_hash(str(row["metadata_json"] or "{}")),
            "status": str(row["status"] or "active"),
            "createdAtMs": int(row["created_at_ms"] or 0),
            "archiveReason": compact_whitespace(str(row["archive_reason"] or "")),
            "updatedAtMs": int(row["updated_at_ms"] or 0),
            "archivedAtMs": int(row["archived_at_ms"] or 0),
        }
        for row in rows
    ]


def _existing_memory_atoms(
    conn: sqlite3.Connection,
    *,
    project: str,
    catalog_only: bool = False,
) -> list[dict[str, object]]:
    """Return the compact global Atom catalog used for semantic deduplication."""
    rows = conn.execute(
        """
        SELECT *
        FROM memory_atoms
        WHERE status IN ('active', 'approved', 'superseded')
          AND (
                ? = 1
                OR ? = ''
                OR scope_project = ?
                OR scope_project = ''
                OR scope_project IS NULL
              )
        ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                 quality_score DESC, updated_at_ms DESC, id ASC
        LIMIT ?
        """,
        (1 if catalog_only else 0, project, project, -1 if catalog_only else 500),
    ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        atom_id = str(row["id"] or "")
        raw_alias_rows = [
            _row_dict(alias_row)
            for alias_row in conn.execute(
                """
                SELECT alias, alias_type, pinyin, weight, created_at_ms
                FROM memory_aliases
                WHERE memory_atom_id = ?
                ORDER BY weight DESC, created_at_ms DESC, alias ASC
                """,
                (atom_id,),
            ).fetchall()
        ]
        aliases: list[str] = []
        surface_hints: list[str] = []
        query_expansions: list[str] = []
        for alias_row in raw_alias_rows if catalog_only else raw_alias_rows[:64]:
            value = _sanitize_catalog_text(
                alias_row.get("alias"),
                max_chars=80,
                uncapped=catalog_only,
            )
            if not value:
                continue
            alias_type = str(alias_row.get("alias_type") or "alias")
            if alias_type == "surface_hint":
                surface_hints.append(value)
            elif alias_type == "query_expansion":
                query_expansions.append(value)
            else:
                aliases.append(value)
        raw_tag_rows = [
            _row_dict(tag_row)
            for tag_row in conn.execute(
                """
                SELECT mat.tag_id, mat.weight, mat.source, mt.tag
                FROM memory_atom_tags mat
                JOIN memory_tags mt ON CAST(mt.id AS TEXT) = mat.tag_id
                WHERE mat.memory_atom_id = ? AND mt.status = 'active'
                ORDER BY mat.weight DESC, mt.quality_score DESC, mt.id ASC
                """,
                (atom_id,),
            ).fetchall()
        ]
        raw_group_rows = [
            _row_dict(member)
            for member in conn.execute(
                "SELECT group_id, weight, source, updated_at_ms "
                "FROM memory_semantic_group_members "
                "WHERE member_type = 'atom' AND member_id = ?",
                (atom_id,),
            ).fetchall()
        ]
        raw_identity = {
            "text": row["text"],
            "canonicalText": row["canonical_text"] or row["text"],
            "kind": row["kind"],
            "language": row["language"],
            "project": row["scope_project"],
            "app": row["scope_app"],
            "claimKey": row["claim_key"],
            "lineageId": row["lineage_id"],
            "claimState": row["claim_state"],
            "validFromMs": row["valid_from_ms"],
            "validToMs": row["valid_to_ms"],
            "supersedesId": row["supersedes_id"],
            "status": row["status"],
            "ownerKind": row["owner_kind"],
            "ownerId": row["owner_id"],
            "privacyLevel": row["privacy_level"],
            "knowledgeDomain": row["knowledge_domain"],
            "scopeKind": row["scope_kind"],
            "scopeId": row["scope_id"],
            "visibility": row["visibility"],
            "authorizationRevision": row["authorization_revision"],
            "bindingId": row["binding_id"],
            "scopeMode": row["scope_mode"],
        }
        identity_hash = memory_atom_identity_hash(raw_identity)
        authority_hash = memory_atom_authority_hash(raw_identity)
        result.append(
            {
                "atomId": atom_id,
                "revision": card_revision(dict(row)),
                "identityHash": identity_hash,
                "authorityHash": authority_hash,
                "kind": str(row["kind"] or "project_fact"),
                "language": str(row["language"] or ""),
                "canonicalText": _sanitize_catalog_text(
                    row["canonical_text"] or row["text"],
                    max_chars=500,
                    uncapped=catalog_only,
                ),
                "text": _sanitize_catalog_text(
                    row["text"],
                    max_chars=500,
                    uncapped=catalog_only,
                ),
                "sourceEventIds": _json_list(row["source_event_ids_json"]),
                "sourceMemoryIds": _json_list(row["source_memory_ids_json"]),
                "app": _sanitize_catalog_text(
                    row["scope_app"],
                    max_chars=120,
                    uncapped=catalog_only,
                ),
                "project": _sanitize_catalog_text(
                    row["scope_project"],
                    max_chars=120,
                    uncapped=catalog_only,
                ),
                "confidence": float(row["confidence"] or 0.0),
                "qualityScore": float(row["quality_score"] or 0.0),
                "status": str(row["status"] or "active"),
                "claimKey": str(row["claim_key"] or ""),
                "lineageId": str(row["lineage_id"] or ""),
                "claimState": str(row["claim_state"] or "current"),
                "validFromMs": int(row["valid_from_ms"] or 0),
                "validToMs": (
                    int(row["valid_to_ms"])
                    if row["valid_to_ms"] is not None
                    else None
                ),
                "supersedesId": str(row["supersedes_id"] or ""),
                **{
                    field: raw_identity[field]
                    for field in (
                        "ownerKind",
                        "ownerId",
                        "privacyLevel",
                        "knowledgeDomain",
                        "scopeKind",
                        "scopeId",
                        "visibility",
                        "authorizationRevision",
                        "bindingId",
                        "scopeMode",
                    )
                },
                "tags": [
                    _sanitize_catalog_text(
                        tag_row.get("tag"),
                        max_chars=48,
                        uncapped=catalog_only,
                    )
                    for tag_row in (
                        raw_tag_rows if catalog_only else raw_tag_rows[:24]
                    )
                    if str(tag_row.get("tag") or "")
                ],
                "semanticGroupIds": [
                    str(member.get("group_id") or "")
                    for member in (
                        raw_group_rows if catalog_only else raw_group_rows[:8]
                    )
                    if str(member.get("group_id") or "")
                ],
                "aliases": aliases,
                "surfaceHints": surface_hints,
                "queryExpansions": query_expansions,
                "tagRelationsHash": stable_text_hash(
                    json.dumps(
                        [
                            {
                                key: value
                                for key, value in item.items()
                                if key not in {"tag"}
                            }
                            for item in raw_tag_rows
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
                "groupMembershipsHash": stable_text_hash(
                    json.dumps(
                        [
                            {
                                key: value
                                for key, value in item.items()
                                if key != "updated_at_ms"
                            }
                            for item in raw_group_rows
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
                "aliasRecordsHash": stable_text_hash(
                    json.dumps(
                        [
                            {
                                key: value
                                for key, value in item.items()
                                if key != "created_at_ms"
                            }
                            for item in raw_alias_rows
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
            }
        )
    return result


def _match_existing_topic_book(
    item: dict[str, object],
    *,
    source_bundle: dict[str, object] | None,
    project: str = "",
    owner_kind: str = "user",
    owner_id: str = "default",
) -> dict[str, object] | None:
    """Resolve an existing Topic Book without letting identity cross scope."""

    existing_by_id: dict[str, dict[str, object]] = {}
    for candidate in [
        *(_list_of_dicts((source_bundle or {}).get("existingMemoryBookIndex"))),
        *(_list_of_dicts((source_bundle or {}).get("existingMemoryBooks"))),
    ]:
        candidate_id = compact_whitespace(str(candidate.get("bookId") or ""))
        if candidate_id:
            existing_by_id[candidate_id] = {
                **existing_by_id.get(candidate_id, {}),
                **candidate,
            }
    existing = list(existing_by_id.values())
    if not existing:
        return None
    requested_id = compact_whitespace(str(item.get("bookId") or ""))
    requested_key = compact_whitespace(str(item.get("bookKey") or ""))
    requested_aliases = {
        normalize_text(str(value or ""))
        for value in (
            item.get("title"),
            item.get("topicAlias"),
            item.get("bookAlias"),
            *(item.get("aliases") if isinstance(item.get("aliases"), list) else []),
        )
        if normalize_text(str(value or ""))
    }
    scope_fields = (
        ("ownerKind", owner_kind),
        ("ownerId", owner_id),
        ("project", compact_whitespace(project)),
        ("app", ""),
        ("knowledgeDomain", ""),
        ("scopeKind", ""),
        ("scopeId", ""),
        ("visibility", ""),
        ("authorizationRevision", ""),
        ("bindingId", ""),
        ("scopeMode", ""),
    )

    def compatible(candidate: Mapping[str, object]) -> bool:
        for field, default in scope_fields:
            # Owner/project are supplied by the caller's governed context;
            # other fields are compared when the model explicitly asserted
            # them. Incomplete legacy fixtures omit fields and remain readable.
            if field in item and item.get(field) is not None:
                requested = compact_whitespace(str(item.get(field) or ""))
            elif field in {"ownerKind", "ownerId", "project"}:
                requested = default
            else:
                continue
            if field not in candidate:
                if field in {"ownerKind", "ownerId", "project"}:
                    continue
                continue
            if compact_whitespace(str(candidate.get(field) or "")) != requested:
                return False
        return True

    def resolve(candidate: Mapping[str, object]) -> dict[str, object] | None:
        current = dict(candidate)
        seen: set[str] = set()
        while True:
            if not compatible(current):
                return None
            current_id = compact_whitespace(str(current.get("bookId") or ""))
            if not current_id or current_id in seen:
                return None
            seen.add(current_id)
            if compact_whitespace(str(current.get("status") or "")) != "superseded":
                return current
            redirect_id = compact_whitespace(
                str(current.get("supersededByBookId") or "")
            )
            if not redirect_id or redirect_id in seen:
                return None
            current = dict(existing_by_id.get(redirect_id) or {})
            if not current:
                return None

    # An existing explicit ID wins over a conflicting title or alias. An
    # unknown model-invented ID may still be corrected by strong similarity.
    if requested_id and requested_id in existing_by_id:
        return resolve(existing_by_id[requested_id])
    if requested_key:
        key_matches = [
            candidate
            for candidate in existing
            if compact_whitespace(str(candidate.get("bookKey") or "")) == requested_key
        ]
        if key_matches:
            resolved = {str(item.get("bookId") or ""): item for item in (resolve(candidate) for candidate in key_matches) if item is not None}
            return next(iter(resolved.values())) if len(resolved) == 1 else None

    exact_candidates = [
        candidate
        for candidate in existing
        if requested_aliases.intersection(
            {
                normalize_text(str(candidate.get("title") or "")),
                normalize_text(str(candidate.get("bookKey") or "")),
                *{
                    normalize_text(str(value or ""))
                    for value in candidate.get("aliases") or []
                    if normalize_text(str(value or ""))
                },
            }
        )
    ]
    if exact_candidates:
        resolved = {str(item.get("bookId") or ""): item for item in (resolve(candidate) for candidate in exact_candidates) if item is not None}
        return next(iter(resolved.values())) if len(resolved) == 1 else None
    if (
        compact_whitespace(str((source_bundle or {}).get("schemaVersion") or ""))
        == "rag-ime.owner-memory-source-bundle.v1"
        and requested_id.startswith("book:owner:")
    ):
        return None

    title = compact_whitespace(str(item.get("title") or ""))
    summary = compact_whitespace(str(item.get("summary") or ""))
    tags = _strings(item.get("tags"))
    candidates = [candidate for candidate in existing if resolve(candidate) is not None]
    ranked = sorted(
        (
            (_topic_book_similarity(title, summary, tags, candidate), candidate)
            for candidate in candidates
        ),
        key=lambda pair: (-pair[0], str(pair[1].get("bookId") or "")),
    )
    if not ranked or ranked[0][0] < 0.52:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08 and ranked[0][0] < 0.82:
        return None
    return ranked[0][1]


def _topic_book_similarity(
    title: str,
    summary: str,
    tags: list[str],
    candidate: dict[str, object],
) -> float:
    current_title = normalize_text(title)
    previous_title = normalize_text(str(candidate.get("title") or ""))
    if current_title and current_title == previous_title:
        return 1.0
    if current_title and previous_title and (
        current_title in previous_title or previous_title in current_title
    ):
        title_score = 0.82
    else:
        current_terms = _topic_terms(" ".join([title, summary, *tags]))
        previous_terms = _topic_terms(
            " ".join(
                [
                    str(candidate.get("title") or ""),
                    str(candidate.get("summary") or ""),
                    *_strings(candidate.get("tags")),
                ]
            )
        )
        title_terms = _topic_terms(title)
        previous_title_terms = _topic_terms(str(candidate.get("title") or ""))
        title_score = _jaccard(title_terms, previous_title_terms)
        content_score = _jaccard(current_terms, previous_terms)
        current_tags = {normalize_text(value) for value in tags if normalize_text(value)}
        previous_tags = {
            normalize_text(value)
            for value in _strings(candidate.get("tags"))
            if normalize_text(value)
        }
        tag_score = _jaccard(current_tags, previous_tags)
        return 0.50 * title_score + 0.35 * content_score + 0.15 * tag_score
    current_tags = {normalize_text(value) for value in tags if normalize_text(value)}
    previous_tags = {
        normalize_text(value)
        for value in _strings(candidate.get("tags"))
        if normalize_text(value)
    }
    return min(1.0, title_score + 0.10 * _jaccard(current_tags, previous_tags))


def _topic_terms(text: str) -> set[str]:
    value = normalize_text(text)
    terms = {normalize_text(term) for term in token_terms(text, max_terms=80) if normalize_text(term)}
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", value))
    if len(cjk) == 1:
        terms.add(cjk)
    elif len(cjk) >= 2:
        terms.update(cjk[index : index + 2] for index in range(len(cjk) - 1))
    return terms


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _existing_semantic_groups(
    conn: sqlite3.Connection,
    *,
    project: str,
    catalog_only: bool = False,
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT group_id, title, description, project, aliases_json, tags_json,
               source_event_ids_json, confidence, quality_score, metadata_json
        FROM memory_semantic_groups
        WHERE status = 'active' AND (
            ? = 1 OR ? = '' OR project = ? OR project = ''
        )
        ORDER BY quality_score DESC, updated_at_ms DESC, group_id ASC
        LIMIT ?
        """,
        (1 if catalog_only else 0, project, project, -1 if catalog_only else 24),
    ).fetchall()
    return [
        {
            "groupId": str(row["group_id"] or ""),
            "title": _sanitize_catalog_text(
                row["title"],
                max_chars=48,
                uncapped=catalog_only,
            ),
            "description": _sanitize_catalog_text(
                row["description"],
                max_chars=160,
                uncapped=catalog_only,
            ),
            "project": _sanitize_catalog_text(
                row["project"],
                max_chars=120,
                uncapped=catalog_only,
            ),
            "aliases": [
                _sanitize_catalog_text(item, max_chars=32, uncapped=catalog_only)
                for item in _json_list(row["aliases_json"])
            ],
            "tags": [
                _sanitize_catalog_text(item, max_chars=32, uncapped=catalog_only)
                for item in _json_list(row["tags_json"])
            ],
            "sourceEventIds": _json_list(row["source_event_ids_json"]),
            "confidence": float(row["confidence"] or 0.0),
            "qualityScore": float(row["quality_score"] or 0.0),
            "metadataHash": stable_text_hash(str(row["metadata_json"] or "{}")),
            "memberMetadataHash": stable_text_hash(
                json.dumps(
                    [
                        _row_dict(member)
                        for member in conn.execute(
                            "SELECT member_type, member_id, weight, source "
                            "FROM memory_semantic_group_members "
                            "WHERE group_id = ?",
                            (str(row["group_id"] or ""),),
                        ).fetchall()
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        }
        for row in rows
    ]


def _existing_semantic_tags(
    conn: sqlite3.Connection,
    *,
    catalog_only: bool = False,
) -> list[dict[str, object]]:
    rows = conn.execute(
        f"""
        SELECT mt.id, mt.tag, mt.normalized_tag, mt.description, mt.tag_type,
               mt.quality_score, mt.source, mt.status, mt.metadata_json,
               mt.created_at_ms, mt.updated_at_ms,
               COALESCE(mtp.aliases_json, '[]') AS aliases_json,
               COALESCE(mtp.color_token, '') AS color_token,
               (SELECT COUNT(*) FROM memory_tag_edges edge
                WHERE edge.src_tag_id = mt.id OR edge.dst_tag_id = mt.id) AS degree
        FROM memory_tags mt
        LEFT JOIN memory_tag_profiles mtp ON mtp.tag_id = mt.id
        WHERE mt.status = 'active' AND {_tag_source_predicate('mt', catalog_only=catalog_only)}
        ORDER BY mt.quality_score DESC, mt.updated_at_ms DESC, mt.id ASC
        LIMIT ?
        """,
        (-1 if catalog_only else 160,),
    ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        metadata = _json_object(row["metadata_json"])
        group_rows = [
            _row_dict(member)
            for member in conn.execute(
                "SELECT group_id, weight, source, updated_at_ms "
                "FROM memory_semantic_group_members "
                "WHERE member_type = 'tag' AND member_id = ?",
                (str(row["id"]),),
            ).fetchall()
        ]
        aliases = _json_list(row["aliases_json"])
        result.append(
            {
                "tagId": int(row["id"]),
                "name": _sanitize_catalog_text(
                    row["tag"],
                    max_chars=32,
                    uncapped=catalog_only,
                ),
                "normalizedName": str(row["normalized_tag"] or ""),
                "description": _sanitize_catalog_text(
                    row["description"],
                    max_chars=120,
                    uncapped=catalog_only,
                ),
                "type": str(row["tag_type"] or "concept"),
                "aliases": [
                    _sanitize_catalog_text(
                        item,
                        max_chars=32,
                        uncapped=catalog_only,
                    )
                    for item in aliases
                ],
                "semanticGroupIds": [
                    str(member.get("group_id") or "")
                    for member in (
                        group_rows if catalog_only else group_rows[:4]
                    )
                    if str(member.get("group_id") or "")
                ],
                "degree": int(row["degree"] or 0),
                "qualityScore": float(row["quality_score"] or 0.0),
                "sourceEventIds": _positive_ints(metadata.get("sourceEventIds")),
                "source": str(row["source"] or ""),
                "status": str(row["status"] or "active"),
                "metadataHash": stable_text_hash(str(row["metadata_json"] or "{}")),
                "profileHash": stable_text_hash(
                    json.dumps(
                        {
                            "colorToken": row["color_token"],
                            "aliases": aliases,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
                "groupMembershipsHash": stable_text_hash(
                    json.dumps(
                        [
                            {
                                key: value
                                for key, value in item.items()
                                if key != "updated_at_ms"
                            }
                            for item in group_rows
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
            }
        )
    return result


def _existing_semantic_tag_edges(
    conn: sqlite3.Connection,
    *,
    catalog_only: bool = False,
) -> list[dict[str, object]]:
    rows = conn.execute(
        f"""
        SELECT edge.src_tag_id, edge.dst_tag_id, src.tag AS src, dst.tag AS dst,
               edge.edge_type, edge.weight, edge.direction_bias,
               edge.evidence_count, edge.metadata_json, edge.updated_at_ms
        FROM memory_tag_edges edge
        JOIN memory_tags src ON src.id = edge.src_tag_id
        JOIN memory_tags dst ON dst.id = edge.dst_tag_id
        WHERE src.status = 'active' AND dst.status = 'active'
          AND {_tag_source_predicate('src', catalog_only=catalog_only)}
          AND {_tag_source_predicate('dst', catalog_only=catalog_only)}
        ORDER BY edge.weight DESC, edge.evidence_count DESC, edge.updated_at_ms DESC,
                 edge.src_tag_id ASC, edge.dst_tag_id ASC, edge.edge_type ASC
        LIMIT ?
        """,
        (-1 if catalog_only else 240,),
    ).fetchall()
    return [
        {
            "srcTagId": int(row["src_tag_id"]),
            "dstTagId": int(row["dst_tag_id"]),
            "src": _sanitize_catalog_text(
                row["src"],
                max_chars=32,
                uncapped=catalog_only,
            ),
            "dst": _sanitize_catalog_text(
                row["dst"],
                max_chars=32,
                uncapped=catalog_only,
            ),
            "edgeType": str(row["edge_type"] or "related_to"),
            "weight": float(row["weight"] or 0.0),
            "directionBias": float(row["direction_bias"] or 0.0),
            "evidenceCount": int(row["evidence_count"] or 0),
            "metadataHash": stable_text_hash(
                str(row["metadata_json"] or "{}")
            ),
        }
        for row in rows
    ]


def _catalog_truncation_flags(
    *,
    existing_books: list[dict[str, object]],
    existing_atoms: list[dict[str, object]],
    existing_groups: list[dict[str, object]],
    existing_tags: list[dict[str, object]],
    existing_tag_edges: list[dict[str, object]],
    catalog_only: bool,
) -> dict[str, bool]:
    if catalog_only:
        return {
            "atoms": False,
            "groups": False,
            "tags": False,
            "books": False,
            "edges": False,
            "atomAliases": False,
            "atomTags": False,
            "atomGroups": False,
            "atomSurfaceHints": False,
            "atomQueryExpansions": False,
            "atomSourceMemoryIds": False,
            "groupAliases": False,
            "groupTags": False,
            "tagAliases": False,
            "tagGroups": False,
            "bookTags": False,
            "bookGroups": False,
            "bookAtoms": False,
        }
    return {
        "atoms": len(existing_atoms) >= 500,
        "groups": len(existing_groups) >= 24,
        "tags": len(existing_tags) >= 160,
        "books": len(existing_books) >= 48,
        "edges": len(existing_tag_edges) >= 240,
        "atomAliases": any(
            len(_strings(item.get("aliases"))) >= 64 for item in existing_atoms
        ),
        "atomTags": any(
            len(_strings(item.get("tags"))) >= 24 for item in existing_atoms
        ),
        "atomGroups": any(
            len(_strings(item.get("semanticGroupIds"))) >= 8
            for item in existing_atoms
        ),
        "atomSurfaceHints": any(
            len(_strings(item.get("surfaceHints"))) >= 64
            for item in existing_atoms
        ),
        "atomQueryExpansions": any(
            len(_strings(item.get("queryExpansions"))) >= 64
            for item in existing_atoms
        ),
        "atomSourceMemoryIds": any(
            len(_strings(item.get("sourceMemoryIds"))) >= 64
            for item in existing_atoms
        ),
        "groupAliases": any(
            len(_strings(item.get("aliases"))) >= 24 for item in existing_groups
        ),
        "groupTags": any(
            len(_strings(item.get("tags"))) >= 24 for item in existing_groups
        ),
        "tagAliases": any(
            len(_strings(item.get("aliases"))) >= 32 for item in existing_tags
        ),
        "tagGroups": any(
            len(_strings(item.get("semanticGroupIds"))) >= 4
            for item in existing_tags
        ),
        "bookTags": any(
            len(_strings(item.get("tags"))) >= 48 for item in existing_books
        ),
        "bookGroups": any(
            len(_strings(item.get("semanticGroupIds"))) >= 8
            for item in existing_books
        ),
        "bookAtoms": any(
            len(_strings(item.get("memoryAtomIds"))) >= 256
            for item in existing_books
        ),
    }

def _group_for_source_ids(source_ids: list[int], *, source_bundle: dict[str, object] | None) -> str:
    if not source_bundle:
        return ""
    wanted = set(source_ids)
    for event in _list_of_dicts(source_bundle.get("recentEvents")):
        event_ids = set(_positive_ints(event.get("sourceEventIds") or [event.get("eventId")]))
        group_id = compact_whitespace(str(event.get("contextGroupId") or ""))
        if wanted.intersection(event_ids) and group_id:
            return group_id
    return ""


def _legal_source_event_ids(source_bundle: dict[str, object] | None) -> list[int]:
    if not source_bundle:
        return []
    result: list[int] = []
    for event in _list_of_dicts(source_bundle.get("recentEvents")):
        for event_id in _positive_ints(event.get("sourceEventIds") or [event.get("eventId")]):
            if event_id not in result:
                result.append(event_id)
    for collection_name in (
        "existingMemoryBooks",
        # Owner curation receives a complete identity index separately from
        # the bounded related-book body.  Topic projection intentionally keeps
        # historical source lineage from that index, so those immutable
        # evidence references must be legal during plan inspection as well.
        "existingMemoryBookIndex",
        "existingMemoryAtoms",
        "existingSemanticGroups",
        "existingSemanticTags",
    ):
        for item in _list_of_dicts(source_bundle.get(collection_name)):
            for event_id in _positive_ints(item.get("sourceEventIds")):
                if event_id not in result:
                    result.append(event_id)
    return result

def _source_input_refs(
    source_bundle: dict[str, object] | None,
) -> list[dict[str, object]]:
    """Persist bounded pointers to source inputs without copying their text."""
    if not source_bundle:
        return []
    collection = _list_of_dicts(source_bundle.get("inputs"))
    if not collection:
        collection = _list_of_dicts(source_bundle.get("recentEvents"))
    result: list[dict[str, object]] = []
    seen: set[tuple[int, ...]] = set()
    for item in collection:
        event_ids = _positive_ints(
            item.get("sourceEventIds") or [item.get("eventId")]
        )
        if not event_ids:
            continue
        event_key = tuple(event_ids)
        if event_key in seen:
            continue
        seen.add(event_key)
        result.append(
            {
                "sourceRef": compact_whitespace(
                    str(item.get("sourceRef") or "")
                )[:80],
                "sourceId": compact_whitespace(
                    str(item.get("sourceId") or "")
                )[:240],
                "sourceIds": _unique_strings(
                    item.get("sourceIds") or [],
                    limit=50_000,
                ),
                "sourceEventIds": event_ids,
                "sourceKind": compact_whitespace(
                    str(item.get("sourceKind") or item.get("source") or "")
                )[:80],
                "source": compact_whitespace(
                    str(item.get("source") or "")
                )[:80],
                "createdAtMs": _optional_int(item.get("createdAtMs")),
                "sourceOccurredAtMs": (
                    _optional_int(item.get("sourceOccurredAtMs"))
                    or _optional_int(item.get("createdAtMs"))
                ),
                "app": compact_whitespace(str(item.get("app") or ""))[:160],
                "contextGroupId": compact_whitespace(
                    str(item.get("contextGroupId") or "")
                )[:240],
            }
        )
        if len(result) >= 64:
            break
    return result



def _semantic_group_id(value: object, *, title: str) -> str:
    raw = compact_whitespace(str(value or "")).lower()
    if raw.startswith("group:") and re.fullmatch(r"group:[a-z0-9][a-z0-9._-]{1,63}", raw):
        return raw
    digest = stable_text_hash(normalize_text(title)).removeprefix("sha256:")[:16]
    return f"group:topic-{digest}"


def _normalized_claim_key(value: object) -> str:
    raw = compact_whitespace(str(value or "")).lower()
    if not raw:
        return ""
    raw = re.sub(r"\s+", "-", raw)
    if len(raw) > 160:
        return ""
    if not re.fullmatch(r"[a-z0-9\u4e00-\u9fff][a-z0-9\u4e00-\u9fff._:/-]*", raw):
        return ""
    return raw


def _latest_source_event_ms(
    source_ids: list[int],
    *,
    source_bundle: dict[str, object] | None,
) -> int:
    if not source_bundle:
        return 0
    wanted = set(source_ids)
    values: list[int] = []
    for collection_name in ("recentEvents", "inputs"):
        for event in _list_of_dicts(source_bundle.get(collection_name)):
            event_ids = _positive_ints(
                event.get("sourceEventIds") or [event.get("eventId")]
            )
            if not wanted.intersection(event_ids):
                continue
            # External summaries are ingested now so the curation cursor can
            # see them, but their factual recency is the source occurrence
            # time. Using ingestion time here could let an old imported fact
            # supersede a newer local fact.
            occurred_at_ms = _optional_int(event.get("sourceOccurredAtMs"))
            created_at_ms = _optional_int(event.get("createdAtMs"))
            if occurred_at_ms > 0:
                values.append(occurred_at_ms)
            elif created_at_ms > 0:
                values.append(created_at_ms)
    return max(values, default=0)


def _semantic_group_ids(item: dict[str, object]) -> list[str]:
    values = item.get("semanticGroupIds")
    if values is None:
        values = item.get("groupIds")
    return [
        value.lower()
        for value in _strings(values)
        if re.fullmatch(r"group:[a-z0-9][a-z0-9._-]{1,63}", value.lower())
    ][:4]


def _resolved_semantic_group_ids(
    item: dict[str, object],
    *,
    source_ids: list[int],
    group_source_ids: dict[str, list[int]],
) -> list[str]:
    explicit = _semantic_group_ids(item)
    if explicit or not source_ids or not group_source_ids:
        return explicit

    wanted = set(source_ids)
    scored = [
        (len(wanted.intersection(event_ids)), group_id)
        for group_id, event_ids in group_source_ids.items()
    ]
    overlap, group_id = max(scored, default=(0, ""))
    return [group_id] if overlap > 0 and group_id else []


def _normalized_rime_pinyin(value: object) -> str:
    pinyin = " ".join(compact_whitespace(str(value or "")).lower().split())
    return pinyin if _RIME_PINYIN_RE.fullmatch(pinyin) else ""


def _memory_owner(owner_kind: object, owner_id: object) -> tuple[str, str]:
    kind = compact_whitespace(str(owner_kind or ""))
    identity = compact_whitespace(str(owner_id or ""))
    if kind not in {"user", "shared", "agent", "session", "room"}:
        raise ValueError("unsupported memory owner kind")
    if not identity:
        raise ValueError("memory owner id must not be empty")
    return kind, identity


def _assert_memory_owner_unchanged(
    previous: dict[str, object] | None,
    payload: dict[str, object],
    *,
    target_kind: str,
) -> None:
    if not previous:
        return
    previous_owner = (
        compact_whitespace(str(previous.get("owner_kind") or "user")),
        compact_whitespace(str(previous.get("owner_id") or "default")),
    )
    requested_owner = _memory_owner(
        payload.get("ownerKind") or "user",
        payload.get("ownerId") or "default",
    )
    if previous_owner != requested_owner:
        raise ValueError(f"memory {target_kind} id is already owned by another scope")


def _assert_memory_book_scope_unchanged(
    previous: dict[str, object] | None,
    payload: Mapping[str, object],
) -> None:
    """Prevent a stable Book ID from being moved to another scope."""

    if not previous:
        return
    payload_fields = {
        "owner_kind": "ownerKind",
        "owner_id": "ownerId",
        "project": "project",
        "app": "app",
        "knowledge_domain": "knowledgeDomain",
        "scope_kind": "scopeKind",
        "scope_id": "scopeId",
        "visibility": "visibility",
        "authorization_revision": "authorizationRevision",
        "binding_id": "bindingId",
        "scope_mode": "scopeMode",
    }
    for field, payload_key in payload_fields.items():
        if payload_key not in payload or payload.get(payload_key) is None:
            continue
        requested = compact_whitespace(str(payload.get(payload_key) or ""))
        stored = _memory_book_field(previous, field)
        if requested != stored:
            raise ValueError("memory book id is already bound to another scope")


def _validate_required_text(errors: list[dict[str, object]], index: int, op: str, payload: dict[str, object], field: str) -> None:
    if not compact_whitespace(str(payload.get(field) or "")):
        errors.append(_issue(index, op, field, "required"))


def _validate_source_ids(
    errors: list[dict[str, object]],
    index: int,
    op: str,
    value: object,
    *,
    field: str = "sourceEventIds",
    legal_source_event_ids: set[int] | None = None,
) -> None:
    source_ids = _positive_ints(value)
    if not source_ids:
        errors.append(_issue(index, op, field, "missing_source_event_ids"))
        return
    legal_ids = legal_source_event_ids or set()
    unknown_ids = [source_id for source_id in source_ids if legal_ids and source_id not in legal_ids]
    if unknown_ids:
        errors.append(
            _issue(
                index,
                op,
                field,
                "source_event_not_in_bundle",
                preview=",".join(str(source_id) for source_id in unknown_ids[:8]),
            )
        )


def _validate_short_terms(
    errors: list[dict[str, object]],
    index: int,
    op: str,
    field: str,
    value: object,
    *,
    max_len: int,
) -> None:
    for item in _strings(value):
        if len(item) < 2 or len(item) > max_len:
            errors.append(_issue(index, op, field, "term_length_out_of_range", value=len(item), preview=truncate_text(item, 80)))


def _validate_secret_free(
    errors: list[dict[str, object]],
    index: int,
    op: str,
    payload: dict[str, object],
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        values = _strings(payload.get(field)) if isinstance(payload.get(field), list) else [compact_whitespace(str(payload.get(field) or ""))]
        for value in values:
            if _contains_sensitive_text(value):
                errors.append(_issue(index, op, field, "sensitive_text_detected", preview=truncate_text(value, 80)))


def _looks_like_long_history_sentence(payload: dict[str, object]) -> bool:
    candidates = _strings(payload.get("surfaceHints")) + [compact_whitespace(str(payload.get("text") or ""))]
    return any(len(item) >= 24 and any(mark in item for mark in ("，", "。", ",", ".", " ")) for item in candidates)


def _issue(
    diff_index: int,
    op: str,
    field: str,
    code: str,
    *,
    value: object = None,
    preview: str = "",
) -> dict[str, object]:
    payload: dict[str, object] = {"diffIndex": diff_index, "op": op, "field": field, "code": code}
    if value is not None:
        payload["value"] = value
    if preview:
        payload["preview"] = preview
    return payload
