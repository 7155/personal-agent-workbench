from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from .input_capture_contract import (
    CAPTURE_SCHEMA_VERSION,
    InputCaptureContractError,
    InputCaptureContractV2,
    capture_contract_from_metadata,
)
from .input_quality import assess_input_text
from .memory_ingest import looks_sensitive
from .text_utils import compact_whitespace


_EXPLICIT_MEMORY_SOURCES = frozenset({"squirrel_assistant_remember", "personal_profile_editor"})


@dataclass(frozen=True)
class InputEvidencePolicy:
    source_kind: str
    trust_class: str
    origin_kind: str
    boundary_kind: str


def input_event_evidence_policy(
    source: str,
    *,
    tags: Iterable[str] = (),
    capture_contract: InputCaptureContractV2 | None = None,
) -> InputEvidencePolicy | None:
    """Classify only typed capture-v2 finals and explicit remember actions.

    v1 tags and transport-source names are not proof of a final boundary. Raw
    Rime bursts, ASR partials, manual/debug commits, generated text, and imports
    therefore remain input audit rather than personal Memory candidates.
    """

    del tags  # Reserved for additive, explicitly reviewed source policies.
    normalized = compact_whitespace(source).lower()
    if capture_contract is not None:
        if not capture_contract.is_strong_final:
            return None
        return InputEvidencePolicy(
            source_kind="user_final",
            trust_class="user_claim",
            origin_kind=(
                "capture_v2_voice"
                if capture_contract.channel == "voice"
                else "capture_v2_input"
            ),
            boundary_kind=capture_contract.boundary_kind,
        )
    if normalized in _EXPLICIT_MEMORY_SOURCES:
        return InputEvidencePolicy(
            source_kind="explicit_memory",
            trust_class="explicit_command",
            origin_kind="explicit_user_memory",
            boundary_kind="explicit_user_action",
        )
    return None


def checkpoint_input_event_evidence(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    created_at_ms: int,
    source: str,
    committed_text: str,
    project: str,
    app: str = "",
    provider_name: str = "",
    tags: Iterable[str] = (),
    capture_contract: InputCaptureContractV2 | None = None,
    capture_error_reason: str = "",
) -> dict[str, object]:
    """Create one compatibility checkpoint and one canonical Evidence candidate."""

    normalized_tags = tuple(
        dict.fromkeys(
            compact_whitespace(str(tag))
            for tag in tags
            if compact_whitespace(str(tag))
        )
    )
    policy = input_event_evidence_policy(
        source,
        tags=normalized_tags,
        capture_contract=capture_contract,
    )
    normalized_capture_error = compact_whitespace(capture_error_reason)[:160]
    if policy is None and normalized_capture_error:
        policy = InputEvidencePolicy(
            source_kind="user_final",
            trust_class="user_claim",
            origin_kind="legacy_untyped_input",
            boundary_kind="invalid_capture_v2",
        )
    if policy is None:
        return {
            "stored": False,
            "status": "source_not_eligible",
            "sourceId": "",
        }

    existing = conn.execute(
        """
        SELECT source_id, disposition
        FROM agent_memory_sources
        WHERE input_event_id = ?
        ORDER BY created_at_ms ASC, source_id ASC
        LIMIT 1
        """,
        (max(1, int(event_id)),),
    ).fetchone()
    if existing is not None:
        evidence = conn.execute(
            """
            SELECT evidence_id, admission_state, admission_reason
            FROM agent_memory_evidence
            WHERE idempotency_key = ?
            LIMIT 1
            """,
            (f"canonical-input-event:{max(1, int(event_id))}",),
        ).fetchone()
        return {
            "stored": False,
            "status": "already_checkpointed",
            "sourceId": str(existing["source_id"]),
            "disposition": str(existing["disposition"]),
            "evidenceId": str(evidence["evidence_id"]) if evidence is not None else "",
            "admissionState": (
                str(evidence["admission_state"]) if evidence is not None else "not_evaluated"
            ),
        }

    canonical = compact_whitespace(committed_text)
    if not canonical:
        return {
            "stored": False,
            "status": "empty_input",
            "sourceId": "",
        }

    timestamp = max(0, int(created_at_ms))
    sensitive = looks_sensitive(canonical)
    assessment = assess_input_text(
        canonical,
        source=source,
        source_count=1,
        finalized=capture_contract is not None and capture_contract.is_strong_final,
        capture_metadata=(capture_contract.metadata if capture_contract is not None else {}),
        tags=normalized_tags,
    )
    quality_reasons = tuple(str(reason) for reason in assessment.reasons)
    capture_receipt_verified = True
    if capture_contract is not None:
        capture_receipt_verified = (
            conn.execute(
                """
                SELECT 1
                FROM input_capture_receipts
                WHERE capture_id = ? AND input_event_id = ?
                  AND outcome = 'stored' AND boundary_confidence = 'strong'
                LIMIT 1
                """,
                (capture_contract.capture_id, max(1, int(event_id))),
            ).fetchone()
            is not None
        )
    if sensitive:
        admission_state = "rejected"
        reason = "sensitive_input"
    elif normalized_capture_error:
        admission_state = "rejected"
        reason = normalized_capture_error
    elif not assessment.memory_eligible:
        admission_state = "rejected"
        reason = quality_reasons[0] if quality_reasons else "not_durable_personal_input"
    elif not capture_receipt_verified:
        admission_state = "needs_review"
        reason = "missing_capture_receipt"
    else:
        admission_state = "candidate"
        reason = "awaiting_luna_adjudication"
    disposition = (
        "pending"
        if admission_state == "candidate"
        else "needs_review"
        if admission_state == "needs_review"
        else "not_for_memory"
    )
    source_id = f"input-memory:{max(1, int(event_id))}"
    metadata = {
        "ledgerVersion": 2,
        "transportSource": compact_whitespace(source),
        "project": compact_whitespace(project),
        "app": compact_whitespace(app),
        "providerName": compact_whitespace(provider_name),
        "sourceMetadataTags": list(normalized_tags[:24]),
        "captureId": capture_contract.capture_id if capture_contract is not None else "",
        "qualityReasons": list(quality_reasons[:12]),
    }
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT INTO agent_memory_sources(
            source_id, session_id, pi_entry_id, input_event_id, source_role,
            source_revision, canonical_text_sha256, status, turn_id,
            approval_id, created_at_ms, owner_kind, owner_id, role_id,
            role_version, source_kind, trust_class, disposition,
            disposition_reason, disposition_updated_at_ms, processed_at_ms,
            metadata_json
        ) VALUES (
            ?, '', ?, ?, 'user', 1, ?, 'active', '', '', ?,
            'user', 'default', '', '', ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            source_id,
            f"input-event:{max(1, int(event_id))}",
            max(1, int(event_id)),
            digest,
            timestamp,
            policy.source_kind,
            policy.trust_class,
            disposition,
            reason,
            timestamp,
            timestamp if sensitive else None,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.execute(
        """
        UPDATE memory_items
        SET owner_kind = 'user', owner_id = 'default'
        WHERE source_event_id = ?
        """,
        (max(1, int(event_id)),),
    )
    conn.execute(
        """
        INSERT INTO memory_source_disposition_events(
            event_id, source_id, previous_disposition, new_disposition,
            reason_code, actor_kind, run_id, created_at_ms, metadata_json
        ) VALUES (?, ?, '', ?, ?, ?, '', ?, '{}')
        """,
        (
            f"memory-disposition:input:{max(1, int(event_id))}:initial",
            source_id,
            disposition,
            reason or "checkpoint_created",
            "rule" if sensitive else "system",
            timestamp,
        ),
    )
    evidence_id = f"evidence:input:{max(1, int(event_id))}"
    evidence_source_id = (
        capture_contract.capture_id
        if capture_contract is not None
        else source_id
    )
    evidence_domain = (
        "personal_memory"
        if policy.origin_kind
        in {"capture_v2_input", "capture_v2_voice", "explicit_user_memory"}
        else "audit_context"
    )
    evidence_metadata = {
        "sourceChannel": (
            capture_contract.channel
            if capture_contract is not None
            else "explicit_memory"
        ),
        "qualityReasons": list(quality_reasons[:12]),
    }
    conn.execute(
        """
        INSERT INTO agent_memory_evidence(
            evidence_id, project, role_id, session_id, source_kind,
            source_id, idempotency_key, content_text, content_sha256,
            provenance_json, metadata_json, privacy_class, status,
            occurred_at_ms, recorded_at_ms, owner_kind, owner_id,
            knowledge_domain, scope_kind, scope_id, visibility,
            authorization_revision, binding_id, scope_mode,
            evidence_domain, origin_kind, admission_state, admission_reason,
            trust_class, boundary_kind, admission_revision,
            admission_updated_at_ms
        ) VALUES (
            ?, ?, '', '', 'user_message', ?, ?, ?, ?, ?, ?, 'private',
            'active', ?, ?, 'user', 'default', 'personal_memory', 'user',
            'default', 'private', 'memory-evidence-v2', ?, 'authoritative',
            ?, ?, ?, ?, ?, ?, 1, ?
        )
        """,
        (
            evidence_id,
            compact_whitespace(project),
            evidence_source_id,
            f"canonical-input-event:{max(1, int(event_id))}",
            canonical,
            digest,
            json.dumps(
                {
                    "sourceType": "input_event",
                    "sourceId": str(max(1, int(event_id))),
                    "inputEventId": max(1, int(event_id)),
                    "transportProject": compact_whitespace(project),
                    "transportApp": compact_whitespace(app),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            json.dumps(
                evidence_metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            timestamp,
            timestamp,
            (
                f"capture:{capture_contract.capture_id}"
                if capture_contract is not None
                else f"explicit-input:{max(1, int(event_id))}"
            ),
            evidence_domain,
            policy.origin_kind,
            admission_state,
            reason,
            policy.trust_class,
            policy.boundary_kind,
            timestamp,
        ),
    )
    conn.execute(
        """
        INSERT INTO memory_evidence_input_event_links(
            evidence_id, input_event_id, ordinal, relation,
            content_sha256, created_at_ms
        ) VALUES (?, ?, 0, 'source', ?, ?)
        """,
        (evidence_id, max(1, int(event_id)), digest, timestamp),
    )
    conn.execute(
        """
        INSERT INTO memory_evidence_admission_events(
            event_id, evidence_id, previous_state, new_state, reason_code,
            actor_kind, created_at_ms, metadata_json
        ) VALUES (?, ?, '', ?, ?, 'rule', ?, ?)
        """,
        (
            f"evidence-admission:input:{max(1, int(event_id))}:initial",
            evidence_id,
            admission_state,
            reason,
            timestamp,
            json.dumps(
                {"qualityReasons": list(quality_reasons[:12])},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    if capture_contract is not None:
        conn.execute(
            """
            UPDATE input_capture_receipts
            SET evidence_id = ?, evidence_state = ?, evidence_reason = ?
            WHERE capture_id = ? AND input_event_id = ?
            """,
            (
                evidence_id,
                admission_state,
                reason,
                capture_contract.capture_id,
                max(1, int(event_id)),
            ),
        )
    return {
        "stored": True,
        "status": "checkpointed",
        "sourceId": source_id,
        "sourceKind": policy.source_kind,
        "trustClass": policy.trust_class,
        "disposition": disposition,
        "evidenceId": evidence_id,
        "admissionState": admission_state,
        "admissionReason": reason,
    }


def backfill_input_event_evidence(
    conn: sqlite3.Connection,
    *,
    project: str = "",
    limit: int = 5000,
) -> dict[str, object]:
    """Idempotently checkpoint only capture-v2 or explicit historical inputs."""

    normalized_project = compact_whitespace(project)
    bounded_limit = max(1, min(int(limit), 50_000))
    rows = conn.execute(
        """
        SELECT e.id, e.created_at_ms, e.source, e.committed_text, e.project,
               e.app, e.provider_name, e.tags_json, e.capture_metadata_json
        FROM input_events AS e
        WHERE (
              lower(e.source) = 'squirrel_assistant_remember'
              OR json_extract(e.capture_metadata_json, '$.schemaVersion') = ?
          )
          AND (? = '' OR e.project = ? OR e.project = '')
          AND length(trim(e.committed_text, ' ' || char(9) || char(10) || char(13))) > 0
          AND NOT EXISTS (
              SELECT 1
              FROM agent_memory_sources AS memory_source
              WHERE memory_source.input_event_id = e.id
          )
        ORDER BY e.id ASC
        LIMIT ?
        """,
        (CAPTURE_SCHEMA_VERSION, normalized_project, normalized_project, bounded_limit),
    ).fetchall()
    stored = 0
    sensitive = 0
    invalid_capture = 0
    for row in rows:
        tags = _json_strings(row["tags_json"])
        capture_contract = None
        capture_error = ""
        try:
            capture_metadata = json.loads(str(row["capture_metadata_json"] or "{}"))
            capture_contract = capture_contract_from_metadata(
                capture_metadata,
                text=str(row["committed_text"] or ""),
                source=str(row["source"] or ""),
                app=str(row["app"] or ""),
            )
        except (TypeError, ValueError, json.JSONDecodeError, InputCaptureContractError):
            capture_error = "invalid_capture_v2"
            invalid_capture += 1
        result = checkpoint_input_event_evidence(
            conn,
            event_id=int(row["id"]),
            created_at_ms=int(row["created_at_ms"] or 0),
            source=str(row["source"] or ""),
            committed_text=str(row["committed_text"] or ""),
            project=str(row["project"] or ""),
            app=str(row["app"] or ""),
            provider_name=str(row["provider_name"] or ""),
            tags=tags,
            capture_contract=capture_contract,
            capture_error_reason=capture_error,
        )
        if bool(result["stored"]):
            stored += 1
        if result.get("disposition") == "not_for_memory":
            sensitive += 1
    remaining = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM input_events AS e
            WHERE (
                  lower(e.source) = 'squirrel_assistant_remember'
                  OR json_extract(e.capture_metadata_json, '$.schemaVersion') = ?
              )
              AND (? = '' OR e.project = ? OR e.project = '')
              AND length(trim(e.committed_text, ' ' || char(9) || char(10) || char(13))) > 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM agent_memory_sources AS memory_source
                  WHERE memory_source.input_event_id = e.id
              )
            """,
            (CAPTURE_SCHEMA_VERSION, normalized_project, normalized_project),
        ).fetchone()[0]
    )
    return {
        "scannedCount": len(rows),
        "storedCount": stored,
        "sensitiveCount": sensitive,
        "invalidCaptureCount": invalid_capture,
        "remainingCount": remaining,
    }


def _json_strings(value: object) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(
        compact_whitespace(str(item))
        for item in decoded
        if compact_whitespace(str(item))
    )
