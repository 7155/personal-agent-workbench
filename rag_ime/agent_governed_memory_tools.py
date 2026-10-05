from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .contracts.json_schema import validate_contract
from .db import apply_database_migrations, sqlite_connection
from .memory_projection import RETRIEVAL_DOCS_PROJECTION, enqueue_memory_projection
from .memory_card_mutations import card_revision, correct_memory_card
from .memory_projection_consistency import (
    invalidate_superseded_atom_dependencies,
    restore_dependency_invalidation,
)
from .sensitive_content import contains_sensitive_content
from .text_utils import compact_whitespace


_ROLE_BOOK_SECTIONS = (
    "personality",
    "capabilities",
    "recentWork",
    "lessonsAndLimits",
    "activeCommitments",
)
_MEMORY_PREVIEW_OPERATIONS = frozenset(
    {"remember_preview", "correct_preview", "forget_preview"}
)
_MEMORY_APPLY_OPERATIONS = {
    "remember_apply": "remember_preview",
    "correct_apply": "correct_preview",
    "forget_apply": "forget_preview",
}
_MEMORY_KINDS = frozenset(
    {"fact", "preference", "decision", "commitment", "project_state"}
)
_MEMORY_READ_MODES = frozenset({"current", "historical", "change"})
_PROPOSAL_TTL_MS = 15 * 60 * 1000
_IMPLICIT_USER_EVIDENCE_MAX_AGE_MS = 5 * 60 * 1000
_IMPLICIT_USER_EVIDENCE_CLOCK_SKEW_MS = 30 * 1000
_PROMPT_INJECTION_PATTERNS = (
    re.compile(
        r"\b(?:ignore|disregard|override|bypass)\b.{0,48}"
        r"\b(?:previous|prior|system|developer|safety)\b.{0,32}"
        r"\b(?:instruction|message|prompt|policy|rule)s?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:reveal|print|show|leak|repeat)\b.{0,32}"
        r"\b(?:system|developer)\s+(?:prompt|message|instruction)s?\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:<\|/?(?:system|assistant|developer|user)\|>|\[/?INST\])", re.IGNORECASE),
    re.compile(
        r"(?:忽略|无视|覆盖|绕过).{0,24}"
        r"(?:系统|开发者|安全|之前).{0,20}(?:指令|提示词|规则|限制)"
    ),
)
_ROLE_GOVERNANCE_PATTERNS = (
    re.compile(
        r"\b(?:grant|enable|disable|expand|change|override|bypass)\b.{0,40}"
        r"\b(?:permission|tool allowlist|tool rule|safety policy|approval rule|identity)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:授予|开启|关闭|扩大|修改|覆盖|绕过).{0,24}"
        r"(?:权限|工具白名单|工具规则|安全策略|审批规则|角色身份)"
    ),
    re.compile(r"\b(?:tool allowlist|system prompt|developer instruction)s?\b", re.IGNORECASE),
    re.compile(r"(?:工具白名单|系统提示词|开发者指令)"),
)


class MemoryGovernanceProposalStore:
    """Durable, hash-bound memory proposals and approval-only mutations."""

    def __init__(self, db_path: str | Path, *, project: str) -> None:
        self.db_path = Path(db_path)
        self.project = _safe_text(
            project,
            field="project",
            maximum=200,
            allow_empty=True,
            prompt_guard=False,
        )

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)

    def preview(
        self,
        operation: str,
        args: Mapping[str, object],
        *,
        session_id: str,
        created_at_ms: int | None = None,
    ) -> dict[str, object]:
        normalized_operation = str(operation or "").strip()
        if normalized_operation not in _MEMORY_PREVIEW_OPERATIONS:
            raise ValueError("unsupported memory governance preview operation")
        session = _identifier(session_id, field="sessionId", maximum=240)
        allowed = {
            "remember_preview": {
                "_sessionId",
                "text",
                "memoryKind",
                "claimKey",
                "reason",
                "evidenceIds",
                "idempotencyKey",
            },
            "correct_preview": {
                "_sessionId",
                "targetId",
                "text",
                "memoryKind",
                "reason",
                "evidenceIds",
                "idempotencyKey",
            },
            "forget_preview": {
                "_sessionId",
                "targetId",
                "reason",
                "evidenceIds",
                "idempotencyKey",
            },
        }[normalized_operation]
        _reject_unexpected_args(args, allowed=allowed, operation=normalized_operation)
        timestamp = (
            int(time.time() * 1000)
            if created_at_ms is None
            else max(0, int(created_at_ms))
        )
        target_id = ""
        if normalized_operation != "remember_preview":
            target_id = _identifier(args.get("targetId"), field="targetId", maximum=240)
        proposed_text = ""
        if normalized_operation != "forget_preview":
            proposed_text = _safe_text(
                args.get("text"),
                field="text",
                maximum=1_200,
                prompt_guard=True,
            )
        reason = _safe_text(
            args.get("reason"),
            field="reason",
            maximum=400,
            allow_empty=normalized_operation == "remember_preview",
            prompt_guard=True,
        )
        requested_kind = str(args.get("memoryKind") or "").strip().lower()
        if requested_kind and requested_kind not in _MEMORY_KINDS:
            raise ValueError("unsupported memoryKind")
        explicit_evidence = "evidenceIds" in args
        evidence_ids = (
            _evidence_ids(args.get("evidenceIds"))
            if explicit_evidence
            else []
        )
        explicit_claim_key = _optional_identifier(
            args.get("claimKey"),
            field="claimKey",
            maximum=240,
        )
        explicit_idempotency = _optional_identifier(
            args.get("idempotencyKey"),
            field="idempotencyKey",
            maximum=240,
        )

        with self._connect(immediate=True) as conn:
            session_role_id = self._session_role_id(conn, session)
            evidence_binding = "explicit"
            if not explicit_evidence:
                evidence_ids = self._latest_user_message_evidence(
                    conn,
                    session_id=session,
                    session_role_id=session_role_id,
                    current_ms=timestamp,
                )
                evidence_binding = "server_latest_user_message"
            evidence_snapshot = self._evidence_snapshot(
                conn,
                evidence_ids,
                session_role_id=session_role_id,
            )
            target_snapshot: dict[str, object] = {}
            target_state_sha256 = ""
            if target_id:
                target_snapshot = self._current_target(conn, target_id)
                target_state_sha256 = _atom_state_sha256(target_snapshot)
            memory_kind = requested_kind
            if normalized_operation == "correct_preview" and not memory_kind:
                memory_kind = str(target_snapshot.get("kind") or "")
            if normalized_operation == "remember_preview" and not memory_kind:
                memory_kind = "fact"
            if (
                normalized_operation == "correct_preview"
                and _canonical_memory_text(proposed_text)
                == _canonical_memory_text(
                    str(target_snapshot.get("canonical_text") or target_snapshot.get("text") or "")
                )
            ):
                raise ValueError("correct_preview must change the target memory text")
            claim_key = (
                explicit_claim_key
                if normalized_operation == "remember_preview"
                else str(target_snapshot.get("claim_key") or "")
            )
            if normalized_operation == "remember_preview" and not claim_key:
                claim_key = _derived_claim_key(memory_kind, proposed_text)
            action = {
                "operation": normalized_operation,
                "sessionId": session,
                "project": self.project,
                "targetId": target_id,
                "memoryKind": memory_kind,
                "claimKey": claim_key,
                "proposedText": proposed_text,
                "reason": reason,
                "evidenceIds": evidence_ids,
                "evidenceBinding": evidence_binding,
                "evidenceSnapshotSha256": _sha256_json(evidence_snapshot),
                "targetStateSha256": target_state_sha256,
            }
            payload_sha256 = _sha256_json(action)
            idempotency_key = explicit_idempotency or (
                f"memory-governance:{payload_sha256[:40]}"
            )
            conn.execute(
                """
                UPDATE memory_governance_proposals
                SET status = 'expired', updated_at_ms = ?
                WHERE session_id = ? AND status = 'preview' AND expires_at_ms <= ?
                """,
                (timestamp, session, timestamp),
            )
            existing = conn.execute(
                """
                SELECT *
                FROM memory_governance_proposals
                WHERE session_id = ? AND idempotency_key = ?
                  AND status IN ('preview', 'applied', 'rolled_back')
                ORDER BY created_at_ms DESC, proposal_id DESC
                LIMIT 1
                """,
                (session, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_sha256"]) != payload_sha256:
                    raise ValueError(
                        "memory proposal idempotencyKey already belongs to different content"
                    )
                row = existing
            else:
                proposal_id = f"memory-proposal:{uuid.uuid4()}"
                expires_at_ms = timestamp + _PROPOSAL_TTL_MS
                conn.execute(
                    """
                    INSERT INTO memory_governance_proposals(
                        proposal_id, session_id, project, operation,
                        target_memory_id, memory_kind, proposed_text, reason,
                        evidence_ids_json, evidence_snapshot_json,
                        target_snapshot_json, target_state_sha256,
                        action_json, payload_sha256, idempotency_key, status,
                        created_at_ms, expires_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preview', ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        session,
                        self.project,
                        normalized_operation,
                        target_id,
                        memory_kind,
                        proposed_text,
                        reason,
                        _json(evidence_ids),
                        _json(evidence_snapshot),
                        _json(target_snapshot),
                        target_state_sha256,
                        _json(action),
                        payload_sha256,
                        idempotency_key,
                        timestamp,
                        expires_at_ms,
                        timestamp,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM memory_governance_proposals WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("memory governance proposal was not persisted")
        return self._preview_payload(row, current_ms=timestamp)

    def prepare_apply(
        self,
        apply_operation: str,
        *,
        proposal_id: str,
        session_id: str,
        current_ms: int | None = None,
    ) -> dict[str, object]:
        expected_preview = _MEMORY_APPLY_OPERATIONS.get(apply_operation)
        if expected_preview is None:
            raise ValueError("unsupported memory governance apply operation")
        proposal = _identifier(proposal_id, field="proposalId", maximum=240)
        session = _identifier(session_id, field="sessionId", maximum=240)
        timestamp = int(time.time() * 1000) if current_ms is None else max(0, int(current_ms))
        with self._connect() as conn:
            row = self._proposal_for_session(conn, proposal, session)
            self._assert_proposal_ready(
                conn,
                row,
                expected_preview=expected_preview,
                current_ms=timestamp,
            )
            evidence_snapshot = _json_array(row["evidence_snapshot_json"])
            current_evidence = self._evidence_snapshot(
                conn,
                _json_strings(row["evidence_ids_json"]),
                session_role_id=self._session_role_id(conn, session),
            )
            if _sha256_json(current_evidence) != _sha256_json(evidence_snapshot):
                raise ValueError("memory proposal evidence changed after preview")
            target_state_sha256 = ""
            if str(row["target_memory_id"]):
                target = self._current_target(conn, str(row["target_memory_id"]))
                target_state_sha256 = _atom_state_sha256(target)
                if target_state_sha256 != str(row["target_state_sha256"]):
                    raise ValueError("memory target changed after preview")
        action_payload = {
            "proposalId": proposal,
            "payloadSha256": str(row["payload_sha256"]),
        }
        base_state = {
            "proposalStatus": "preview",
            "expiresAtMs": int(row["expires_at_ms"]),
            "targetStateSha256": target_state_sha256,
            "evidenceStateSha256": _sha256_json(evidence_snapshot),
        }
        return {
            "proposal": self._preview_payload(row, current_ms=timestamp),
            "actionPayload": action_payload,
            "baseState": base_state,
            "title": "确认修改长期记忆",
            "operationLabel": {
                "remember_apply": "写入长期记忆",
                "correct_apply": "更正长期记忆",
                "forget_apply": "撤回长期记忆",
            }[apply_operation],
            "summary": _memory_apply_summary(apply_operation, row),
            "changes": _memory_apply_changes(apply_operation, row),
        }

    def apply(
        self,
        apply_operation: str,
        *,
        proposal_id: str,
        session_id: str,
        approval_id: str,
        current_ms: int | None = None,
    ) -> dict[str, object]:
        expected_preview = _MEMORY_APPLY_OPERATIONS.get(apply_operation)
        if expected_preview is None:
            raise ValueError("unsupported memory governance apply operation")
        proposal = _identifier(proposal_id, field="proposalId", maximum=240)
        session = _identifier(session_id, field="sessionId", maximum=240)
        approval = _identifier(approval_id, field="approvalId", maximum=240)
        timestamp = int(time.time() * 1000) if current_ms is None else max(0, int(current_ms))
        with self._connect(immediate=True) as conn:
            row = self._proposal_for_session(conn, proposal, session)
            if str(row["status"]) == "applied":
                receipt = _json_object(row["receipt_json"])
                if receipt:
                    return {**receipt, "idempotentReplay": True}
            self._assert_proposal_ready(
                conn,
                row,
                expected_preview=expected_preview,
                current_ms=timestamp,
            )
            evidence_snapshot = self._validated_evidence_for_proposal(
                conn,
                row,
                session_id=session,
            )
            if expected_preview == "remember_preview":
                mutation = self._apply_remember(
                    conn,
                    row,
                    evidence_snapshot=evidence_snapshot,
                    timestamp=timestamp,
                    approval_id=approval,
                )
            elif expected_preview == "correct_preview":
                mutation = self._apply_correct(
                    conn,
                    row,
                    evidence_snapshot=evidence_snapshot,
                    timestamp=timestamp,
                    approval_id=approval,
                )
            else:
                mutation = self._apply_forget(
                    conn,
                    row,
                    evidence_snapshot=evidence_snapshot,
                    timestamp=timestamp,
                    approval_id=approval,
                )
            outbox_id = enqueue_memory_projection(
                conn,
                projection_kind=RETRIEVAL_DOCS_PROJECTION,
                aggregate_type="memory_atom",
                aggregate_id=str(mutation["memoryId"]),
                operation=apply_operation,
                project=self.project,
                payload={
                    "proposalId": proposal,
                    "approvalId": approval,
                    "memoryId": str(mutation["memoryId"]),
                },
            )
            receipt = {
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "mutationApplied": True,
                "approvalId": approval,
                "auditId": proposal,
                "toolId": "memory",
                "operation": apply_operation,
                "proposalId": proposal,
                "status": "applied",
                "memoryId": str(mutation["memoryId"]),
                "previousMemoryId": str(mutation.get("previousMemoryId") or ""),
                "evidenceIds": _json_strings(row["evidence_ids_json"]),
                "projectionOutboxId": outbox_id,
                "summary": str(mutation["summary"]),
                "undoAvailable": True,
                "rollback": {
                    "tool": "memory",
                    "operation": "governance_rollback",
                    "args": {"proposalId": proposal},
                },
            }
            conn.execute(
                """
                UPDATE memory_governance_proposals
                SET status = 'applied', applied_memory_id = ?, rollback_json = ?,
                    receipt_json = ?, applied_at_ms = ?, updated_at_ms = ?
                WHERE proposal_id = ? AND status = 'preview'
                """,
                (
                    str(mutation["memoryId"]),
                    _json(mutation["rollback"]),
                    _json(receipt),
                    timestamp,
                    timestamp,
                    proposal,
                ),
            )
        return receipt

    def prepare_rollback(
        self,
        *,
        proposal_id: str,
        session_id: str,
    ) -> dict[str, object]:
        proposal = _identifier(proposal_id, field="proposalId", maximum=240)
        session = _identifier(session_id, field="sessionId", maximum=240)
        with self._connect() as conn:
            row = self._proposal_for_session(conn, proposal, session)
            if str(row["status"]) != "applied":
                raise ValueError("memory proposal is not currently rollbackable")
            rollback = _json_object(row["rollback_json"])
            if not rollback:
                raise ValueError("memory proposal has no rollback state")
            rollback_state_sha256 = self._rollback_state_sha256(conn, rollback)
        action_payload = {
            "proposalId": proposal,
            "payloadSha256": str(row["payload_sha256"]),
        }
        base_state = {
            "proposalStatus": "applied",
            "appliedAtMs": int(row["applied_at_ms"] or 0),
            "rollbackStateSha256": rollback_state_sha256,
        }
        return {
            "actionPayload": action_payload,
            "baseState": base_state,
            "title": "确认回滚长期记忆",
            "operationLabel": "回滚长期记忆",
            "summary": f"将回滚记忆提议 {proposal}",
            "changes": [
                {
                    "label": "记忆状态",
                    "before": "已应用",
                    "after": "恢复到应用前",
                }
            ],
        }

    def rollback(
        self,
        *,
        proposal_id: str,
        session_id: str,
        approval_id: str,
        expected_state_sha256: str,
        current_ms: int | None = None,
    ) -> dict[str, object]:
        proposal = _identifier(proposal_id, field="proposalId", maximum=240)
        session = _identifier(session_id, field="sessionId", maximum=240)
        approval = _identifier(approval_id, field="approvalId", maximum=240)
        timestamp = int(time.time() * 1000) if current_ms is None else max(0, int(current_ms))
        with self._connect(immediate=True) as conn:
            row = self._proposal_for_session(conn, proposal, session)
            if str(row["status"]) == "rolled_back":
                receipt = _json_object(row["rollback_receipt_json"])
                if receipt:
                    return {**receipt, "idempotentReplay": True}
            if str(row["status"]) != "applied":
                raise ValueError("memory proposal is not currently rollbackable")
            rollback = _json_object(row["rollback_json"])
            current_state_sha256 = self._rollback_state_sha256(conn, rollback)
            if current_state_sha256 != expected_state_sha256:
                raise ValueError("memory state changed after rollback approval preview")
            kind = str(rollback.get("kind") or "")
            if kind == "remember":
                memory_id = self._rollback_remember(conn, rollback, timestamp=timestamp)
            elif kind == "correct":
                memory_id = self._rollback_correct(conn, rollback, timestamp=timestamp)
            elif kind == "forget":
                memory_id = self._rollback_forget(conn, rollback, timestamp=timestamp)
            else:
                raise ValueError("unsupported memory rollback state")
            outbox_id = enqueue_memory_projection(
                conn,
                projection_kind=RETRIEVAL_DOCS_PROJECTION,
                aggregate_type="memory_atom",
                aggregate_id=memory_id,
                operation="governance_rollback",
                project=self.project,
                payload={
                    "proposalId": proposal,
                    "approvalId": approval,
                    "memoryId": memory_id,
                },
            )
            receipt = {
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "mutationApplied": True,
                "approvalId": approval,
                "auditId": proposal,
                "toolId": "memory",
                "operation": "governance_rollback",
                "proposalId": proposal,
                "revertedProposalId": proposal,
                "memoryId": memory_id,
                "projectionOutboxId": outbox_id,
                "status": "rolled_back",
                "summary": "已在状态复验后回滚长期记忆修改",
                "undoAvailable": False,
            }
            conn.execute(
                """
                UPDATE memory_governance_proposals
                SET status = 'rolled_back', rollback_receipt_json = ?,
                    rolled_back_at_ms = ?, updated_at_ms = ?
                WHERE proposal_id = ? AND status = 'applied'
                """,
                (_json(receipt), timestamp, timestamp, proposal),
            )
        return receipt

    def read(
        self,
        operation: str,
        args: Mapping[str, object],
    ) -> dict[str, object]:
        mode = str(args.get("mode") or "current").strip().lower()
        if mode not in _MEMORY_READ_MODES:
            raise ValueError("mode must be current, historical, or change")
        limit = _bounded_int(args.get("limit"), default=8, minimum=1, maximum=20)
        query = _safe_text(
            args.get("query"),
            field="query",
            maximum=240,
            allow_empty=True,
            prompt_guard=False,
        )
        target_id = _optional_identifier(
            args.get("targetId"),
            field="targetId",
            maximum=240,
        )
        kind = str(args.get("kind") or "atoms").strip().lower()
        if kind not in {"atoms", "timelines"}:
            raise ValueError("kind must be atoms or timelines")
        if kind == "timelines" and operation != "search":
            raise ValueError("Timeline memory currently supports search only")
        if kind == "timelines" and mode != "current":
            raise ValueError("Timeline memory only has an approved current view")
        with self._connect() as conn:
            if operation == "search":
                if kind == "timelines":
                    return self._search_timelines(conn, query=query, limit=limit)
                return self._search(conn, mode=mode, query=query, limit=limit)
            if operation == "get":
                if not target_id:
                    raise ValueError("targetId is required for memory.get")
                return self._get(conn, mode=mode, target_id=target_id)
            if operation == "explain":
                if not target_id:
                    raise ValueError("targetId is required for memory.explain")
                return self._explain(conn, mode=mode, target_id=target_id)
        raise ValueError("unsupported governed memory read operation")

    def _search_timelines(
        self,
        conn: sqlite3.Connection,
        *,
        query: str,
        limit: int,
    ) -> dict[str, object]:
        sql = """
            SELECT timeline.*
            FROM daily_activity_timelines AS timeline
            WHERE timeline.project = ? AND timeline.status = 'approved'
        """
        params: list[object] = [self.project]
        if query:
            needle = f"%{query}%"
            sql += """
              AND (
                timeline.timeline_id LIKE ? OR timeline.timeline_date LIKE ?
                OR timeline.summary_text LIKE ? OR timeline.segments_json LIKE ?
              )
            """
            params.extend([needle] * 4)
        sql += " ORDER BY timeline.timeline_date DESC, timeline.updated_at_ms DESC LIMIT ?"
        params.append(limit)
        items: list[dict[str, object]] = []
        for row in conn.execute(sql, params).fetchall():
            summary = compact_whitespace(str(row["summary_text"] or ""))
            if not summary or contains_sensitive_content(summary):
                continue
            timeline_id = str(row["timeline_id"])
            title = f"{str(row['timeline_date'])} 活动时间线"
            segments = [
                _safe_timeline_segment(value, timeline_id=timeline_id)
                for value in _json_array(row["segments_json"])
                if isinstance(value, Mapping)
            ][:6]
            source = {
                "type": "activity_timeline",
                "kind": "activity_timeline",
                "id": timeline_id,
            }
            items.append(
                {
                    "timelineId": timeline_id,
                    "date": str(row["timeline_date"]),
                    "title": title,
                    "summary": summary,
                    "status": "approved",
                    "taskCount": int(row["segment_count"] or 0),
                    "eventCount": int(row["event_count"] or 0),
                    "segments": segments,
                    "source": source,
                    "ref": _compatible_reference(
                        "timeline",
                        timeline_id,
                        legacy_type="timeline",
                    ),
                    "maySupportFacts": False,
                    "corroborationOnly": True,
                }
            )
        return {
            "summary": f"检索到 {len(items)} 条已批准活动时间线",
            "mode": "current",
            "kind": "timelines",
            "query": query,
            "items": items,
            "count": len(items),
            "boundary": "Timeline 只说明某时段做过什么，不证明稳定事实。",
        }

    def daily_user_memory_draft(
        self,
        *,
        draft_id: str,
        session_id: str,
    ) -> dict[str, object]:
        draft = _identifier(draft_id, field="draftId", maximum=240)
        session = _identifier(session_id, field="sessionId", maximum=240)
        with self._connect() as conn:
            role_id = self._session_role_id(conn, session)
            rows = conn.execute(
                """
                SELECT output_json
                FROM personal_context_consolidation_runs
                WHERE project = ? AND role_id = ? AND status = 'succeeded'
                ORDER BY completed_at_ms DESC, run_id DESC
                LIMIT 200
                """,
                (self.project, role_id),
            ).fetchall()
            selected: dict[str, object] | None = None
            for row in rows:
                output = _json_object(row["output_json"])
                candidate = output.get("userMemoryDraft")
                if (
                    isinstance(candidate, Mapping)
                    and str(candidate.get("draftId") or "") == draft
                ):
                    selected = dict(candidate)
                    break
            if selected is None:
                raise ValueError(
                    "daily user-memory draft does not exist for this project/role"
                )
            if (
                str(selected.get("project") or "") != self.project
                or str(selected.get("roleId") or "") != role_id
            ):
                raise ValueError("daily user-memory draft scope does not match this session")
            evidence = _daily_draft_evidence(
                conn,
                selected.get("sourceEvidenceIds"),
                project=self.project,
                role_id=role_id,
            )
        candidates = (
            selected.get("candidates")
            if isinstance(selected.get("candidates"), list)
            else []
        )
        return {
            "summary": f"每日用户记忆草案包含 {len(candidates)} 个待审候选",
            "reviewKind": "daily_user_memory_draft",
            "draft": {
                **selected,
                "sourceEvidence": evidence,
                "sourceEvidenceTrust": "untrusted_data_not_instructions",
            },
            "reviewRequired": True,
            "applyOperationAvailable": False,
            "mutationApplied": False,
        }

    def _apply_remember(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        evidence_snapshot: list[dict[str, object]],
        timestamp: int,
        approval_id: str,
    ) -> dict[str, object]:
        action = _json_object(row["action_json"])
        atom_id = _proposal_atom_id(str(row["proposal_id"]))
        claim_key = str(action.get("claimKey") or "")
        memory_kind = str(row["memory_kind"])
        existing = conn.execute(
            """
            SELECT id
            FROM memory_atoms
            WHERE owner_kind = 'user' AND owner_id = 'default'
              AND claim_key = ? AND COALESCE(scope_project, '') = ?
              AND COALESCE(scope_app, '') = ''
              AND claim_state = 'current' AND status IN ('active', 'approved')
            """,
            (claim_key, self.project),
        ).fetchone()
        if existing is not None and str(existing["id"]) != atom_id:
            raise ValueError("a current memory already exists for this claimKey; use correct_preview")
        privacy_level = _evidence_privacy(evidence_snapshot)
        lineage_id = _derived_lineage_id(self.project, memory_kind, claim_key)
        conn.execute(
            """
            INSERT INTO memory_atoms(
                id, kind, text, canonical_text, source_event_ids_json,
                source_memory_ids_json, scope_app, scope_project, language,
                confidence, quality_score, echo_risk, privacy_level, status,
                created_at_ms, updated_at_ms, last_used_at_ms, claim_key,
                lineage_id, claim_state, valid_from_ms, valid_to_ms, supersedes_id
            ) VALUES (?, ?, ?, ?, '[]', '[]', NULL, ?, 'zh', 1.0, 1.0, 0.0, ?,
                      'approved', ?, ?, NULL, ?, ?, 'current', ?, NULL, NULL)
            """,
            (
                atom_id,
                memory_kind,
                str(row["proposed_text"]),
                _canonical_memory_text(str(row["proposed_text"])),
                self.project,
                privacy_level,
                timestamp,
                timestamp,
                claim_key,
                lineage_id,
                timestamp,
            ),
        )
        self._link_evidence(
            conn,
            atom_id=atom_id,
            proposal_id=str(row["proposal_id"]),
            relation="supports",
            evidence_snapshot=evidence_snapshot,
            timestamp=timestamp,
        )
        return {
            "memoryId": atom_id,
            "summary": "已写入一条有证据来源的长期记忆",
            "rollback": {
                "kind": "remember",
                "createdAtomId": atom_id,
                "postAtomStateSha256": _atom_state_sha256(
                    self._atom_by_id(conn, atom_id)
                ),
                "approvalId": approval_id,
            },
        }

    def _apply_correct(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        evidence_snapshot: list[dict[str, object]],
        timestamp: int,
        approval_id: str,
    ) -> dict[str, object]:
        old_id = str(row["target_memory_id"])
        old = self._current_target(conn, old_id)
        if _atom_state_sha256(old) != str(row["target_state_sha256"]):
            raise ValueError("memory target changed after preview")
        new_id = _proposal_atom_id(str(row["proposal_id"]))
        supersession_id = _proposal_supersession_id(str(row["proposal_id"]))
        mutation = correct_memory_card(
            conn, old_id, text=str(row["proposed_text"]),
            expected_revision=card_revision(old), timestamp=timestamp,
            mutation_id=str(row["proposal_id"]), new_id=new_id,
            supersession_id=supersession_id,
            memory_kind=str(row["memory_kind"] or old.get("kind") or "fact"),
            reason=str(row["reason"]), user_edit=True, metadata={
                "source": "agent_governed_memory", "proposalId": str(row["proposal_id"]),
                "approvalId": approval_id, "evidenceIds": _json_strings(row["evidence_ids_json"]),
            },
        )
        self._link_evidence(
            conn,
            atom_id=new_id,
            proposal_id=str(row["proposal_id"]),
            relation="corrects",
            evidence_snapshot=evidence_snapshot,
            timestamp=timestamp,
        )
        dependency_invalidation = mutation["dependencyInvalidation"]
        return {
            "memoryId": new_id,
            "previousMemoryId": old_id,
            "summary": "已将旧事实原子化 supersede 为新事实",
            "rollback": {
                "kind": "correct",
                "oldAtom": old,
                "newAtomId": new_id,
                "supersessionId": supersession_id,
                "postOldStateSha256": _atom_state_sha256(
                    self._atom_by_id(conn, old_id)
                ),
                "postNewStateSha256": _atom_state_sha256(
                    self._atom_by_id(conn, new_id)
                ),
                "approvalId": approval_id,
                "dependencyInvalidation": dependency_invalidation,
            },
        }

    def _apply_forget(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        evidence_snapshot: list[dict[str, object]],
        timestamp: int,
        approval_id: str,
    ) -> dict[str, object]:
        target_id = str(row["target_memory_id"])
        old = self._current_target(conn, target_id)
        if _atom_state_sha256(old) != str(row["target_state_sha256"]):
            raise ValueError("memory target changed after preview")
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
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValueError("memory target is no longer current")
        cursor = conn.execute(
            """
            INSERT INTO memory_tombstones(
                created_at_ms, target_type, target_value, reason, active, metadata_json
            ) VALUES (?, 'memory_id', ?, ?, 1, ?)
            """,
            (
                timestamp,
                target_id,
                str(row["reason"]),
                _json(
                    {
                        "source": "agent_governed_memory",
                        "proposalId": str(row["proposal_id"]),
                        "approvalId": approval_id,
                        "evidenceIds": _json_strings(row["evidence_ids_json"]),
                    }
                ),
            ),
        )
        tombstone_id = int(cursor.lastrowid)
        self._link_evidence(
            conn,
            atom_id=target_id,
            proposal_id=str(row["proposal_id"]),
            relation="retracts",
            evidence_snapshot=evidence_snapshot,
            timestamp=timestamp,
        )
        dependency_invalidation = invalidate_superseded_atom_dependencies(
            conn,
            [target_id],
            timestamp=timestamp,
        )
        return {
            "memoryId": target_id,
            "previousMemoryId": target_id,
            "summary": "已撤回长期记忆并写入可审计 tombstone",
            "rollback": {
                "kind": "forget",
                "oldAtom": old,
                "tombstoneId": tombstone_id,
                "postAtomStateSha256": _atom_state_sha256(
                    self._atom_by_id(conn, target_id)
                ),
                "approvalId": approval_id,
                "dependencyInvalidation": dependency_invalidation,
            },
        }

    def _rollback_remember(
        self,
        conn: sqlite3.Connection,
        rollback: Mapping[str, object],
        *,
        timestamp: int,
    ) -> str:
        atom_id = str(rollback.get("createdAtomId") or "")
        atom = self._atom_by_id(conn, atom_id)
        if _atom_state_sha256(atom) != str(rollback.get("postAtomStateSha256") or ""):
            raise ValueError("remembered memory changed and cannot be rolled back")
        conn.execute(
            """
            UPDATE memory_atoms
            SET status = 'tombstoned', claim_state = 'retracted',
                valid_to_ms = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (timestamp, timestamp, atom_id),
        )
        return atom_id

    def _rollback_correct(
        self,
        conn: sqlite3.Connection,
        rollback: Mapping[str, object],
        *,
        timestamp: int,
    ) -> str:
        old = rollback.get("oldAtom")
        if not isinstance(old, Mapping):
            raise ValueError("correction rollback is missing the old atom")
        old_id = str(old.get("id") or "")
        new_id = str(rollback.get("newAtomId") or "")
        if (
            _atom_state_sha256(self._atom_by_id(conn, old_id))
            != str(rollback.get("postOldStateSha256") or "")
            or _atom_state_sha256(self._atom_by_id(conn, new_id))
            != str(rollback.get("postNewStateSha256") or "")
        ):
            raise ValueError("corrected memory lineage changed and cannot be rolled back")
        conn.execute(
            """
            UPDATE memory_atoms
            SET status = 'tombstoned', claim_state = 'retracted',
                valid_to_ms = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (timestamp, timestamp, new_id),
        )
        self._restore_atom_state(conn, old)
        conn.execute(
            """
            UPDATE memory_supersessions
            SET status = 'rolled_back', rolled_back_at_ms = ?
            WHERE supersession_id = ? AND status = 'active'
            """,
            (timestamp, str(rollback.get("supersessionId") or "")),
        )
        dependency_rollback = rollback.get("dependencyInvalidation")
        if isinstance(dependency_rollback, Mapping):
            restore_dependency_invalidation(
                conn,
                dependency_rollback,
                timestamp=timestamp,
            )
        return old_id

    def _rollback_forget(
        self,
        conn: sqlite3.Connection,
        rollback: Mapping[str, object],
        *,
        timestamp: int,
    ) -> str:
        old = rollback.get("oldAtom")
        if not isinstance(old, Mapping):
            raise ValueError("forget rollback is missing the old atom")
        atom_id = str(old.get("id") or "")
        if _atom_state_sha256(self._atom_by_id(conn, atom_id)) != str(
            rollback.get("postAtomStateSha256") or ""
        ):
            raise ValueError("forgotten memory changed and cannot be rolled back")
        self._restore_atom_state(conn, old)
        conn.execute(
            "UPDATE memory_tombstones SET active = 0 WHERE id = ? AND active = 1",
            (int(rollback.get("tombstoneId") or 0),),
        )
        dependency_rollback = rollback.get("dependencyInvalidation")
        if isinstance(dependency_rollback, Mapping):
            restore_dependency_invalidation(
                conn,
                dependency_rollback,
                timestamp=timestamp,
            )
        return atom_id

    def _validated_evidence_for_proposal(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        session_id: str,
    ) -> list[dict[str, object]]:
        stored = _json_array(row["evidence_snapshot_json"])
        current = self._evidence_snapshot(
            conn,
            _json_strings(row["evidence_ids_json"]),
            session_role_id=self._session_role_id(conn, session_id),
        )
        if _sha256_json(current) != _sha256_json(stored):
            raise ValueError("memory proposal evidence changed after preview")
        return current

    def _assert_proposal_ready(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        expected_preview: str,
        current_ms: int,
    ) -> None:
        if str(row["operation"]) != expected_preview:
            raise ValueError("memory proposal operation does not match apply operation")
        if str(row["project"]) != self.project:
            raise ValueError("memory proposal belongs to another project")
        if str(row["status"]) != "preview":
            raise ValueError("memory proposal is no longer applicable")
        if int(row["expires_at_ms"]) <= current_ms:
            raise ValueError("memory proposal has expired")
        action = _json_object(row["action_json"])
        if _sha256_json(action) != str(row["payload_sha256"]):
            raise ValueError("memory proposal payload hash is stale")

    def _proposal_for_session(
        self,
        conn: sqlite3.Connection,
        proposal_id: str,
        session_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT * FROM memory_governance_proposals
            WHERE proposal_id = ? AND session_id = ?
            """,
            (proposal_id, session_id),
        ).fetchone()
        if row is None:
            raise ValueError("memory proposal does not exist in this session")
        return row

    def _current_target(
        self,
        conn: sqlite3.Connection,
        target_id: str,
    ) -> dict[str, object]:
        row = conn.execute(
            """
            SELECT * FROM memory_atoms
            WHERE id = ? AND COALESCE(scope_project, '') = ?
              AND claim_state = 'current' AND status IN ('active', 'approved')
            """,
            (target_id, self.project),
        ).fetchone()
        if row is None:
            raise ValueError("target memory is missing, historical, or outside this project")
        return dict(row)

    def _atom_by_id(
        self,
        conn: sqlite3.Connection,
        atom_id: str,
    ) -> dict[str, object]:
        row = conn.execute(
            """
            SELECT * FROM memory_atoms
            WHERE id = ? AND COALESCE(scope_project, '') = ?
            """,
            (atom_id, self.project),
        ).fetchone()
        if row is None:
            raise ValueError("memory atom does not exist in this project")
        return dict(row)

    def _session_role_id(self, conn: sqlite3.Connection, session_id: str) -> str:
        row = conn.execute(
            "SELECT role_id FROM agent_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError("memory proposal session does not exist")
        return str(row["role_id"] or "")

    def _evidence_snapshot(
        self,
        conn: sqlite3.Connection,
        evidence_ids: Sequence[str],
        *,
        session_role_id: str,
    ) -> list[dict[str, object]]:
        placeholders = ", ".join("?" for _ in evidence_ids)
        rows = conn.execute(
            f"""
            SELECT *
            FROM agent_memory_evidence
            WHERE evidence_id IN ({placeholders})
            """,
            list(evidence_ids),
        ).fetchall()
        by_id = {str(row["evidence_id"]): row for row in rows}
        result: list[dict[str, object]] = []
        for evidence_id in evidence_ids:
            row = by_id.get(evidence_id)
            if row is None:
                raise ValueError(f"evidence does not exist: {evidence_id}")
            if str(row["project"]) != self.project or str(row["status"]) != "active":
                raise ValueError("evidence is tombstoned or belongs to another project")
            if str(row["scope_mode"] or "legacy") == "authoritative":
                raise ValueError(
                    "Room-scoped evidence requires an explicit governed promotion receipt"
                )
            evidence_role = str(row["role_id"] or "")
            if evidence_role and session_role_id and evidence_role != session_role_id:
                raise ValueError("evidence belongs to another Agent role")
            stored_content = str(row["content_text"] or "")
            _safe_text(
                stored_content,
                field=f"evidence[{evidence_id}]",
                maximum=32_000,
                prompt_guard=True,
            )
            # The evidence digest is an identity for the exact canonical text
            # persisted by AgentMemoryEvidenceStore.  Validation may normalize
            # Unicode for safety checks, but must not silently change the bytes
            # whose provenance hash we verify (for example Chinese full-width
            # punctuation under NFKC).
            content_sha256 = hashlib.sha256(stored_content.encode("utf-8")).hexdigest()
            if content_sha256 != str(row["content_sha256"]):
                raise ValueError("evidence content hash does not match its stored provenance")
            provenance = _json_object(row["provenance_json"])
            if not provenance or not str(provenance.get("sourceType") or "") or not str(
                provenance.get("sourceId") or ""
            ):
                raise ValueError("evidence provenance is required")
            event_ids = _input_event_ids_from_provenance(provenance)
            if event_ids and not _input_events_are_memory_eligible(conn, event_ids):
                raise ValueError(
                    "evidence source event is deleted, tombstoned, expired, or not for memory"
                )
            result.append(
                {
                    "evidenceId": evidence_id,
                    "sourceKind": str(row["source_kind"]),
                    "sourceId": str(row["source_id"]),
                    "contentSha256": content_sha256,
                    "provenance": provenance,
                    "privacyClass": str(row["privacy_class"]),
                    "scopeMode": str(row["scope_mode"] or "legacy"),
                    "occurredAtMs": int(row["occurred_at_ms"]),
                }
            )
        return result

    def _latest_user_message_evidence(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        session_role_id: str,
        current_ms: int,
    ) -> list[str]:
        lower_bound = max(0, current_ms - _IMPLICIT_USER_EVIDENCE_MAX_AGE_MS)
        upper_bound = current_ms + _IMPLICIT_USER_EVIDENCE_CLOCK_SKEW_MS
        row = conn.execute(
            """
            SELECT evidence_id
            FROM agent_memory_evidence
            WHERE project = ? AND role_id = ? AND session_id = ?
              AND source_kind = 'user_message' AND status = 'active'
              AND occurred_at_ms BETWEEN ? AND ?
              AND recorded_at_ms BETWEEN ? AND ?
            ORDER BY occurred_at_ms DESC, recorded_at_ms DESC, evidence_id DESC
            LIMIT 1
            """,
            (
                self.project,
                session_role_id,
                session_id,
                lower_bound,
                upper_bound,
                lower_bound,
                upper_bound,
            ),
        ).fetchone()
        if row is None:
            raise ValueError(
                "evidenceIds is required because no recent active user_message "
                "evidence exists for this session/project/role"
            )
        return [str(row["evidence_id"])]

    def _link_evidence(
        self,
        conn: sqlite3.Connection,
        *,
        atom_id: str,
        proposal_id: str,
        relation: str,
        evidence_snapshot: Sequence[Mapping[str, object]],
        timestamp: int,
    ) -> None:
        for evidence in evidence_snapshot:
            conn.execute(
                """
                INSERT INTO memory_atom_evidence_links(
                    memory_atom_id, evidence_id, proposal_id, relation,
                    content_sha256, provenance_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    atom_id,
                    str(evidence["evidenceId"]),
                    proposal_id,
                    relation,
                    str(evidence["contentSha256"]),
                    _json(evidence["provenance"]),
                    timestamp,
                ),
            )

    def _restore_atom_state(
        self,
        conn: sqlite3.Connection,
        atom: Mapping[str, object],
    ) -> None:
        conn.execute(
            """
            UPDATE memory_atoms
            SET status = ?, claim_state = ?, valid_to_ms = ?,
                updated_at_ms = ?, supersedes_id = ?
            WHERE id = ?
            """,
            (
                atom.get("status"),
                atom.get("claim_state"),
                atom.get("valid_to_ms"),
                atom.get("updated_at_ms"),
                atom.get("supersedes_id"),
                atom.get("id"),
            ),
        )

    def _rollback_state_sha256(
        self,
        conn: sqlite3.Connection,
        rollback: Mapping[str, object],
    ) -> str:
        kind = str(rollback.get("kind") or "")
        if kind == "remember":
            state = {
                "atom": _atom_state(
                    self._atom_by_id(conn, str(rollback.get("createdAtomId") or ""))
                )
            }
        elif kind == "correct":
            supersession = conn.execute(
                "SELECT status, rolled_back_at_ms FROM memory_supersessions WHERE supersession_id = ?",
                (str(rollback.get("supersessionId") or ""),),
            ).fetchone()
            state = {
                "oldAtom": _atom_state(
                    self._atom_by_id(
                        conn,
                        str(dict(rollback.get("oldAtom") or {}).get("id") or ""),
                    )
                ),
                "newAtom": _atom_state(
                    self._atom_by_id(conn, str(rollback.get("newAtomId") or ""))
                ),
                "supersession": dict(supersession) if supersession is not None else {},
            }
        elif kind == "forget":
            tombstone = conn.execute(
                "SELECT active FROM memory_tombstones WHERE id = ?",
                (int(rollback.get("tombstoneId") or 0),),
            ).fetchone()
            old = rollback.get("oldAtom")
            state = {
                "atom": _atom_state(
                    self._atom_by_id(
                        conn,
                        str(dict(old or {}).get("id") or ""),
                    )
                ),
                "tombstoneActive": (
                    int(tombstone["active"]) if tombstone is not None else -1
                ),
            }
        else:
            raise ValueError("unsupported memory rollback state")
        return _sha256_json(state)

    def _preview_payload(
        self,
        row: sqlite3.Row,
        *,
        current_ms: int,
    ) -> dict[str, object]:
        status = str(row["status"])
        ready = status == "preview" and int(row["expires_at_ms"]) > current_ms
        payload: dict[str, object] = {
            "schemaVersion": "rag-ime.memory-governance-preview.v1",
            "previewId": str(row["proposal_id"]),
            "proposalId": str(row["proposal_id"]),
            "operation": str(row["operation"]),
            "applyOperation": _preview_apply_operation(str(row["operation"])),
            "sessionId": str(row["session_id"]),
            "project": str(row["project"]),
            "targetId": str(row["target_memory_id"]),
            "memoryKind": str(row["memory_kind"]),
            "proposedText": str(row["proposed_text"]),
            "reason": str(row["reason"]),
            "evidenceIds": _json_strings(row["evidence_ids_json"]),
            "summary": _memory_preview_summary(
                str(row["operation"]),
                target_id=str(row["target_memory_id"]),
                proposed_text=str(row["proposed_text"]),
            ),
            "status": "ready" if ready else status,
            "reviewRequired": True,
            "applyOperationAvailable": ready,
            "mutationApplied": False,
            "writes": {
                "proposalStored": True,
                "memoryAtoms": False,
                "memoryBooks": False,
                "retrievalVectors": False,
            },
            "audit": {
                "payloadSha256": str(row["payload_sha256"]),
                "recordKind": "memory_governance_proposal",
                "sessionId": str(row["session_id"]),
                "idempotencyKey": str(row["idempotency_key"]),
            },
            "createdAtMs": int(row["created_at_ms"]),
            "expiresAtMs": int(row["expires_at_ms"]),
        }
        validate_contract(payload, "memory-governance-preview.v1.json")
        return payload

    def _search(
        self,
        conn: sqlite3.Connection,
        *,
        mode: str,
        query: str,
        limit: int,
    ) -> dict[str, object]:
        if mode == "change":
            sql = """
                SELECT s.*, old.text AS old_text, old.status AS old_status,
                       old.canonical_text AS old_canonical_text,
                       old.privacy_level AS old_privacy_level,
                       old.scope_project AS old_scope_project,
                       new.text AS new_text, new.status AS new_status,
                       new.canonical_text AS new_canonical_text,
                       new.privacy_level AS new_privacy_level,
                       new.scope_project AS new_scope_project
                FROM memory_supersessions s
                JOIN memory_atoms old ON old.id = s.old_memory_id
                JOIN memory_atoms new ON new.id = s.new_memory_id
                WHERE COALESCE(old.scope_project, '') = ?
                  AND COALESCE(new.scope_project, '') = ?
                  AND COALESCE(old.privacy_level, '') <> 'sensitive'
                  AND COALESCE(new.privacy_level, '') <> 'sensitive'
            """
            params: list[object] = [self.project, self.project]
            if query:
                sql += (
                    " AND (s.supersession_id LIKE ? OR s.old_memory_id LIKE ? "
                    "OR s.new_memory_id LIKE ? OR old.text LIKE ? OR new.text LIKE ?)"
                )
                needle = f"%{query}%"
                params.extend([needle] * 5)
            sql += " ORDER BY s.created_at_ms DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            items = [
                _change_payload(row)
                for row in rows
                if _agent_visible_change(row)
            ]
        else:
            where = (
                "claim_state = 'current' AND status IN ('active', 'approved')"
                if mode == "current"
                else "NOT (claim_state = 'current' AND status IN ('active', 'approved'))"
            )
            sql = (
                "SELECT * FROM memory_atoms "
                "WHERE COALESCE(scope_project, '') = ? AND "
                "COALESCE(privacy_level, '') <> 'sensitive' AND "
                f"{where}"
            )
            params = [self.project]
            if query:
                sql += (
                    " AND (id LIKE ? OR text LIKE ? OR canonical_text LIKE ? "
                    "OR claim_key LIKE ? OR lineage_id LIKE ?)"
                )
                needle = f"%{query}%"
                params.extend([needle] * 5)
            sql += " ORDER BY updated_at_ms DESC, id DESC LIMIT ?"
            params.append(limit)
            items = [
                _atom_payload(row)
                for row in conn.execute(sql, params).fetchall()
                if _agent_visible_atom(row)
            ]
        return {
            "summary": f"按 {mode} 视图检索到 {len(items)} 条记忆记录",
            "mode": mode,
            "query": query,
            "items": items,
            "count": len(items),
        }

    def _get(
        self,
        conn: sqlite3.Connection,
        *,
        mode: str,
        target_id: str,
    ) -> dict[str, object]:
        if mode == "change":
            rows = conn.execute(
                """
                SELECT s.*, old.text AS old_text, old.status AS old_status,
                       old.canonical_text AS old_canonical_text,
                       old.privacy_level AS old_privacy_level,
                       old.scope_project AS old_scope_project,
                       new.text AS new_text, new.status AS new_status,
                       new.canonical_text AS new_canonical_text,
                       new.privacy_level AS new_privacy_level,
                       new.scope_project AS new_scope_project
                FROM memory_supersessions s
                JOIN memory_atoms old ON old.id = s.old_memory_id
                JOIN memory_atoms new ON new.id = s.new_memory_id
                WHERE (s.supersession_id = ? OR s.old_memory_id = ? OR s.new_memory_id = ?)
                  AND COALESCE(old.scope_project, '') = ?
                  AND COALESCE(new.scope_project, '') = ?
                  AND COALESCE(old.privacy_level, '') <> 'sensitive'
                  AND COALESCE(new.privacy_level, '') <> 'sensitive'
                ORDER BY s.created_at_ms DESC
                LIMIT 20
                """,
                (target_id, target_id, target_id, self.project, self.project),
            ).fetchall()
            visible_rows = [
                row for row in rows if _agent_visible_change(row)
            ]
            return {
                "summary": f"读取到 {len(visible_rows)} 条记忆变更",
                "mode": mode,
                "targetId": target_id,
                "found": bool(visible_rows),
                "items": [_change_payload(row) for row in visible_rows],
            }
        row = conn.execute(
            """
            SELECT * FROM memory_atoms
            WHERE id = ? AND COALESCE(scope_project, '') = ?
            """,
            (target_id, self.project),
        ).fetchone()
        visible = row is not None and (
            mode == "historical"
            or (
                str(row["claim_state"]) == "current"
                and str(row["status"]) in {"active", "approved"}
            )
        ) and _agent_visible_atom(row)
        return {
            "summary": "已读取记忆原子" if visible else "当前视图中没有该记忆",
            "mode": mode,
            "targetId": target_id,
            "found": visible,
            "item": _atom_payload(row) if visible and row is not None else {},
        }

    def _explain(
        self,
        conn: sqlite3.Connection,
        *,
        mode: str,
        target_id: str,
    ) -> dict[str, object]:
        base = self._get(conn, mode=mode, target_id=target_id)
        if mode == "change":
            return {
                **base,
                "selectionPolicy": "change 只返回显式 supersession 关系",
            }
        raw = conn.execute(
            """
            SELECT * FROM memory_atoms
            WHERE id = ? AND COALESCE(scope_project, '') = ?
            """,
            (target_id, self.project),
        ).fetchone()
        if raw is None or not _agent_visible_atom(raw):
            return {**base, "selectionPolicy": "目标不存在或不属于当前项目"}
        is_current = (
            str(raw["claim_state"]) == "current"
            and str(raw["status"]) in {"active", "approved"}
        )
        if mode == "current" and not is_current:
            return {
                **base,
                "excluded": True,
                "exclusionReason": "not_current",
                "selectionPolicy": (
                    "current 默认排除 superseded、retracted 与 tombstoned 记忆"
                ),
            }
        evidence_rows = conn.execute(
            """
            SELECT evidence_id, proposal_id, relation, content_sha256,
                   provenance_json, created_at_ms
            FROM memory_atom_evidence_links
            WHERE memory_atom_id = ?
            ORDER BY created_at_ms DESC, evidence_id
            LIMIT 32
            """,
            (target_id,),
        ).fetchall()
        changes = self._get(conn, mode="change", target_id=target_id)
        return {
            **base,
            "selectionPolicy": (
                "current 只接受 active/approved 且 claim_state=current"
                if mode == "current"
                else "historical 显式允许读取非 current 状态"
            ),
            "claim": {
                "claimKey": str(raw["claim_key"] or ""),
                "lineageId": str(raw["lineage_id"] or ""),
                "claimState": str(raw["claim_state"] or ""),
                "supersedesId": str(raw["supersedes_id"] or ""),
            },
            "evidence": [
                {
                    "evidenceId": str(item["evidence_id"]),
                    "proposalId": str(item["proposal_id"]),
                    "relation": str(item["relation"]),
                    "contentSha256": str(item["content_sha256"]),
                    "provenance": _json_object(item["provenance_json"]),
                    "createdAtMs": int(item["created_at_ms"]),
                }
                for item in evidence_rows
            ],
            "changes": list(changes.get("items") or []),
        }

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if immediate:
                conn.commit()
        except Exception:
            if immediate:
                conn.rollback()
            raise
        finally:
            conn.close()


def build_memory_governance_preview(
    operation: str,
    args: Mapping[str, object],
    *,
    session_id: str,
    project: str,
    db_path: str | Path,
    created_at_ms: int | None = None,
) -> dict[str, object]:
    """Persist one proposal while leaving long-term memory untouched."""

    store = MemoryGovernanceProposalStore(db_path, project=project)
    store.initialize()
    return store.preview(
        operation,
        args,
        session_id=session_id,
        created_at_ms=created_at_ms,
    )


class AgentRoleBookToolAdapter:
    """Governed tool adapter; only proposal creation is writable."""

    def __init__(
        self,
        store: object,
        *,
        db_path: str | Path,
        project: str = "",
    ) -> None:
        self.store = store
        self.db_path = Path(db_path)
        self.project = _safe_text(
            project,
            field="project",
            maximum=200,
            allow_empty=True,
            prompt_guard=False,
        )

    def execute(
        self,
        operation: str,
        args: Mapping[str, object],
        *,
        session: Mapping[str, object],
    ) -> dict[str, object]:
        role_id, role_version = _session_role(session)
        if operation == "get":
            _reject_unexpected_args(
                args,
                allowed={"_sessionId", "revisionId"},
                operation="agent_role_book.get",
            )
            revision_id = _optional_identifier(
                args.get("revisionId"),
                field="revisionId",
                maximum=240,
            )
            if not revision_id:
                revision_id = _optional_identifier(
                    session.get("roleBookRevisionId"),
                    field="roleBookRevisionId",
                    maximum=240,
                )
            if not revision_id:
                raise ValueError("session has no pinned Role Book revision")
            revision = self._revision_for_role(revision_id, role_id, role_version)
            return self._result(
                "get",
                role_id,
                role_version,
                summary=f"已读取角色书 revision {revision['revisionNumber']}",
                result={
                    "revision": revision,
                    "pinned": revision_id == session.get("roleBookRevisionId"),
                },
            )
        if operation == "history":
            _reject_unexpected_args(
                args,
                allowed={"_sessionId", "limit"},
                operation="agent_role_book.history",
            )
            limit = _bounded_int(args.get("limit"), default=20, minimum=1, maximum=100)
            items = self._history(role_id, role_version, limit=limit)
            return self._result(
                "history",
                role_id,
                role_version,
                summary=f"已读取 {len(items)} 个角色书版本",
                result={"items": items, "count": len(items)},
            )
        if operation == "propose_revision":
            _reject_unexpected_args(
                args,
                allowed={"_sessionId", "updates", "changeSummary"},
                operation="agent_role_book.propose_revision",
            )
            updates = args.get("updates")
            if not isinstance(updates, Mapping):
                raise ValueError("updates must be an object")
            _validate_role_book_updates(updates)
            pinned_id = _optional_identifier(
                session.get("roleBookRevisionId"),
                field="roleBookRevisionId",
                maximum=240,
            )
            active = self.store.active(role_id, role_version)  # type: ignore[attr-defined]
            if not isinstance(active, Mapping):
                raise ValueError("role book has no active revision")
            if not pinned_id or pinned_id != str(active.get("revisionId") or ""):
                raise ValueError(
                    "stale or unpinned sessions cannot propose a Role Book revision"
                )
            draft = self.store.propose_revision(  # type: ignore[attr-defined]
                role_id,
                role_version,
                updates,
                proposed_by=f"agent-session:{session.get('id') or ''}",
                change_summary=_safe_text(
                    args.get("changeSummary"),
                    field="changeSummary",
                    maximum=400,
                    allow_empty=True,
                    prompt_guard=False,
                ),
            )
            if str(draft.get("status") or "") != "draft":
                raise RuntimeError("Role Book proposal did not produce a draft")
            return self._result(
                "propose_revision",
                role_id,
                role_version,
                summary=(
                    f"已保存角色书草案 revision {draft['revisionNumber']}，"
                    "等待人工审阅"
                ),
                result={
                    "draft": draft,
                    "draftStored": True,
                    "reviewRequired": True,
                    "activationAvailableInTool": False,
                },
            )
        if operation == "review":
            _reject_unexpected_args(
                args,
                allowed={"_sessionId", "revisionId", "draftId"},
                operation="agent_role_book.review",
            )
            revision_id = _optional_identifier(
                args.get("revisionId"),
                field="revisionId",
                maximum=240,
            )
            draft_id = _optional_identifier(
                args.get("draftId"),
                field="draftId",
                maximum=240,
            )
            if bool(revision_id) == bool(draft_id):
                raise ValueError("review requires exactly one of revisionId or draftId")
            if draft_id:
                daily = self._daily_role_book_draft(
                    draft_id,
                    role_id=role_id,
                    role_version=role_version,
                )
                patch = (
                    daily.get("patch")
                    if isinstance(daily.get("patch"), Mapping)
                    else {}
                )
                counts = {
                    key: len(value) if isinstance(value, list) else 0
                    for key, value in patch.items()
                }
                return self._result(
                    "review",
                    role_id,
                    role_version,
                    summary=(
                        f"每日角色书草案包含 {sum(counts.values())} 项待审内容"
                    ),
                    result={
                        "reviewKind": "daily_role_book_draft",
                        "draft": daily,
                        "counts": counts,
                        "reviewRequired": True,
                        "activationAvailableInTool": False,
                    },
                )
            assert revision_id
            revision = self._revision_for_role(revision_id, role_id, role_version)
            source_id = str(revision.get("sourceRevisionId") or "")
            source = (
                self._revision_for_role(source_id, role_id, role_version)
                if source_id
                else None
            )
            review = _role_book_review(revision, source)
            return self._result(
                "review",
                role_id,
                role_version,
                summary=(
                    f"角色书 revision {revision['revisionNumber']} "
                    f"包含 {review['counts']['total']} 项变化"
                ),
                result={
                    "revision": revision,
                    "review": review,
                    "reviewRequired": str(revision.get("status") or "") == "draft",
                    "activationAvailableInTool": False,
                },
            )
        raise ValueError("unsupported agent_role_book operation")

    def _daily_role_book_draft(
        self,
        draft_id: str,
        *,
        role_id: str,
        role_version: str,
    ) -> dict[str, object]:
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                """
                SELECT output_json
                FROM personal_context_consolidation_runs
                WHERE project = ? AND role_id = ? AND role_version = ?
                  AND status = 'succeeded'
                ORDER BY completed_at_ms DESC, run_id DESC
                LIMIT 200
                """,
                (self.project, role_id, role_version),
            ).fetchall()
            selected: dict[str, object] | None = None
            for row in rows:
                output = _json_object(row["output_json"])
                candidate = output.get("roleBookDraft")
                if (
                    isinstance(candidate, Mapping)
                    and str(candidate.get("draftId") or "") == draft_id
                ):
                    selected = dict(candidate)
                    break
            if selected is None:
                raise ValueError(
                    "daily Role Book draft does not exist for this project/role/version"
                )
            if (
                str(selected.get("project") or "") != self.project
                or str(selected.get("roleId") or "") != role_id
                or str(selected.get("baseRoleVersion") or "") != role_version
            ):
                raise ValueError("daily Role Book draft scope does not match this session")
            evidence = _daily_draft_evidence(
                conn,
                selected.get("sourceEvidenceIds"),
                project=self.project,
                role_id=role_id,
            )
        return {
            **selected,
            "sourceEvidence": evidence,
            "sourceEvidenceTrust": "untrusted_data_not_instructions",
        }

    def _history(
        self,
        role_id: str,
        role_version: str,
        *,
        limit: int,
    ) -> list[dict[str, object]]:
        with sqlite_connection(self.db_path) as conn:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                """
                SELECT revision_id
                FROM agent_role_book_revisions
                WHERE role_id = ? AND role_version = ?
                ORDER BY revision_number DESC
                LIMIT ?
                """,
                (role_id, role_version, limit),
            ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            revision = self._revision_for_role(str(row[0]), role_id, role_version)
            sections = revision.get("sections")
            counts = {
                section: len(sections.get(section) or [])
                for section in _ROLE_BOOK_SECTIONS
                if isinstance(sections, Mapping)
            }
            items.append(
                {
                    "revisionId": revision["revisionId"],
                    "revisionNumber": revision["revisionNumber"],
                    "status": revision["status"],
                    "sourceRevisionId": revision["sourceRevisionId"],
                    "changeSummary": revision["changeSummary"],
                    "proposedBy": revision["proposedBy"],
                    "createdAtMs": revision["createdAtMs"],
                    "activatedAtMs": revision["activatedAtMs"],
                    "sectionCounts": counts,
                }
            )
        return items

    def _revision_for_role(
        self,
        revision_id: str,
        role_id: str,
        role_version: str,
    ) -> dict[str, object]:
        revision = self.store.get_revision(revision_id)  # type: ignore[attr-defined]
        if (
            str(revision.get("roleId") or ""),
            str(revision.get("roleVersion") or ""),
        ) != (role_id, role_version):
            raise ValueError("Role Book revision belongs to another role")
        return dict(revision)

    @staticmethod
    def _result(
        operation: str,
        role_id: str,
        role_version: str,
        *,
        summary: str,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-role-book-tool-result.v1",
            "operation": operation,
            "roleId": role_id,
            "roleVersion": role_version,
            "summary": summary,
            "activeRevisionChanged": False,
            "result": dict(result),
        }
        validate_contract(payload, "agent-role-book-tool-result.v1.json")
        return payload


def _validate_role_book_updates(updates: Mapping[str, object]) -> None:
    unexpected = sorted(str(key) for key in updates if key not in _ROLE_BOOK_SECTIONS)
    if unexpected:
        raise ValueError(
            "Role Book proposals cannot change identity, permissions, safety, or tools; "
            f"unsupported fields: {', '.join(unexpected)}"
        )
    if not updates:
        raise ValueError("Role Book proposal must include at least one section")
    for section, raw_items in updates.items():
        if not isinstance(raw_items, Sequence) or isinstance(
            raw_items, (str, bytes, bytearray)
        ):
            raise ValueError(f"{section} must be an array")
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise ValueError(f"{section} items must be objects")
            text = str(item.get("text") or "")
            if any(pattern.search(text) for pattern in _ROLE_GOVERNANCE_PATTERNS):
                raise ValueError(
                    "Role Book proposals cannot change identity, permissions, "
                    "safety policy, or tool allowlists"
                )


def _role_book_review(
    revision: Mapping[str, object],
    source: Mapping[str, object] | None,
) -> dict[str, object]:
    target_sections = revision.get("sections")
    source_sections = source.get("sections") if isinstance(source, Mapping) else {}
    target_sections = target_sections if isinstance(target_sections, Mapping) else {}
    source_sections = source_sections if isinstance(source_sections, Mapping) else {}
    section_reviews: dict[str, object] = {}
    totals = {"added": 0, "removed": 0, "changed": 0, "total": 0}
    for section in _ROLE_BOOK_SECTIONS:
        target_items = _items_by_id(target_sections.get(section))
        source_items = _items_by_id(source_sections.get(section))
        added = [
            target_items[item_id]
            for item_id in sorted(target_items.keys() - source_items.keys())
        ]
        removed = [
            source_items[item_id]
            for item_id in sorted(source_items.keys() - target_items.keys())
        ]
        changed = [
            {"before": source_items[item_id], "after": target_items[item_id]}
            for item_id in sorted(target_items.keys() & source_items.keys())
            if target_items[item_id] != source_items[item_id]
        ]
        section_reviews[section] = {
            "added": added,
            "removed": removed,
            "changed": changed,
        }
        totals["added"] += len(added)
        totals["removed"] += len(removed)
        totals["changed"] += len(changed)
    totals["total"] = totals["added"] + totals["removed"] + totals["changed"]
    return {
        "sourceRevisionId": str(revision.get("sourceRevisionId") or ""),
        "targetRevisionId": str(revision.get("revisionId") or ""),
        "counts": totals,
        "sections": section_reviews,
    }


def _items_by_id(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return {}
    return {
        str(item.get("itemId")): dict(item)
        for item in value
        if isinstance(item, Mapping) and str(item.get("itemId") or "")
    }


def _session_role(session: Mapping[str, object]) -> tuple[str, str]:
    return (
        _identifier(session.get("roleId"), field="session.roleId", maximum=120),
        _identifier(
            session.get("roleVersion"),
            field="session.roleVersion",
            maximum=80,
        ),
    )


def _memory_preview_summary(
    operation: str,
    *,
    target_id: str,
    proposed_text: str,
) -> str:
    if operation == "remember_preview":
        return f"记住新事实的预览：{proposed_text[:80]}"
    if operation == "correct_preview":
        return f"修正 {target_id} 的预览：{proposed_text[:80]}"
    return f"遗忘 {target_id} 的预览"


def _memory_apply_summary(operation: str, row: Mapping[str, object]) -> str:
    if operation == "remember_apply":
        return f"将写入长期记忆：{str(row['proposed_text'])[:80]}"
    if operation == "correct_apply":
        return (
            f"将把 {row['target_memory_id']} 更正为："
            f"{str(row['proposed_text'])[:80]}"
        )
    return f"将撤回长期记忆 {row['target_memory_id']}"


def _memory_apply_changes(
    operation: str,
    row: Mapping[str, object],
) -> list[dict[str, str]]:
    if operation == "remember_apply":
        return [
            {
                "label": "长期记忆",
                "before": "不存在",
                "after": str(row["proposed_text"])[:160],
            }
        ]
    target = _json_object(row["target_snapshot_json"])
    before = str(target.get("text") or target.get("canonical_text") or "")[:160]
    return [
        {
            "label": "长期记忆",
            "before": before,
            "after": (
                str(row["proposed_text"])[:160]
                if operation == "correct_apply"
                else "撤回并保留 tombstone"
            ),
        }
    ]


def _preview_apply_operation(operation: str) -> str:
    for apply_operation, preview_operation in _MEMORY_APPLY_OPERATIONS.items():
        if preview_operation == operation:
            return apply_operation
    raise ValueError("unsupported memory governance preview operation")


def _canonical_memory_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _derived_claim_key(memory_kind: str, text: str) -> str:
    digest = hashlib.sha256(
        f"{memory_kind}\n{_canonical_memory_text(text).casefold()}".encode("utf-8")
    ).hexdigest()
    return f"agent-claim:{digest[:32]}"


def _derived_lineage_id(project: str, memory_kind: str, claim_key: str) -> str:
    digest = hashlib.sha256(
        f"{project}\n{memory_kind}\n{claim_key}".encode("utf-8")
    ).hexdigest()
    return f"memory-lineage:{digest[:32]}"


def _proposal_atom_id(proposal_id: str) -> str:
    return f"atom:agent-governed:{hashlib.sha256(proposal_id.encode('utf-8')).hexdigest()[:28]}"


def _proposal_supersession_id(proposal_id: str) -> str:
    return (
        "supersession:agent-governed:"
        f"{hashlib.sha256(proposal_id.encode('utf-8')).hexdigest()[:24]}"
    )


def _evidence_privacy(evidence: Sequence[Mapping[str, object]]) -> str:
    return (
        "private"
        if any(str(item.get("privacyClass") or "") == "private" for item in evidence)
        else "local"
    )


_ATOM_STATE_FIELDS = (
    "id",
    "kind",
    "text",
    "canonical_text",
    "scope_app",
    "scope_project",
    "privacy_level",
    "status",
    "updated_at_ms",
    "claim_key",
    "lineage_id",
    "claim_state",
    "valid_from_ms",
    "valid_to_ms",
    "supersedes_id",
    "owner_kind", "owner_id", "knowledge_domain", "scope_kind", "scope_id",
    "visibility", "authorization_revision", "binding_id", "scope_mode",
    "source_event_ids_json", "source_memory_ids_json", "language", "user_edit_revision",
)


def _atom_state(value: Mapping[str, object]) -> dict[str, object]:
    return {field: value.get(field) for field in _ATOM_STATE_FIELDS}


def _atom_state_sha256(value: Mapping[str, object]) -> str:
    return _sha256_json(_atom_state(value))


def _agent_visible_atom(row: Mapping[str, object]) -> bool:
    if str(_row_value(row, "privacy_level") or "").lower() == "sensitive":
        return False
    return not contains_sensitive_content(
        _row_value(row, "text")
    ) and not contains_sensitive_content(
        _row_value(row, "canonical_text")
    )


def _agent_visible_change(row: Mapping[str, object]) -> bool:
    if str(_row_value(row, "old_scope_project") or "") != str(
        _row_value(row, "new_scope_project") or ""
    ):
        return False
    if str(_row_value(row, "old_privacy_level") or "").lower() == "sensitive":
        return False
    if str(_row_value(row, "new_privacy_level") or "").lower() == "sensitive":
        return False
    return not any(
        contains_sensitive_content(_row_value(row, field))
        for field in (
            "old_text",
            "old_canonical_text",
            "new_text",
            "new_canonical_text",
        )
    )


def _row_value(row: Mapping[str, object], key: str) -> object:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _compatible_reference(
    kind: str,
    reference_id: str,
    *,
    legacy_type: str = "",
    **metadata: object,
) -> dict[str, object]:
    """Expose the new stable reference without breaking pre-migration callers."""

    value: dict[str, object] = {
        "kind": kind,
        "id": reference_id,
        "referenceKind": kind,
        "referenceId": reference_id,
    }
    if legacy_type:
        value["type"] = legacy_type
    value.update(metadata)
    return value


def _atom_payload(row: Mapping[str, object]) -> dict[str, object]:
    if not _agent_visible_atom(row):
        raise ValueError("sensitive memory atoms are not Agent-visible")
    memory_id = str(row["id"])
    source_event_ids = [
        int(value)
        for value in _json_strings(_row_value(row, "source_event_ids_json"))
        if str(value).isdigit() and int(value) > 0
    ]
    return {
        "memoryId": memory_id,
        "kind": str(row["kind"]),
        "text": str(row["text"]),
        "canonicalText": str(row["canonical_text"] or ""),
        "project": str(row["scope_project"] or ""),
        "app": str(row["scope_app"] or ""),
        "privacyLevel": str(row["privacy_level"] or ""),
        "status": str(row["status"]),
        "claimKey": str(row["claim_key"] or ""),
        "lineageId": str(row["lineage_id"] or ""),
        "claimState": str(row["claim_state"] or ""),
        "validFromMs": int(row["valid_from_ms"] or 0),
        "validToMs": (
            int(row["valid_to_ms"]) if row["valid_to_ms"] is not None else None
        ),
        "supersedesId": str(row["supersedes_id"] or ""),
        "createdAtMs": int(row["created_at_ms"] or 0),
        "updatedAtMs": int(row["updated_at_ms"] or 0),
        "sourceEventIds": source_event_ids,
        "source": {
            "type": "memory_atom",
            "kind": "memory_atom",
            "id": memory_id,
        },
        "ref": _compatible_reference(
            "atom",
            memory_id,
            legacy_type="memory_atom",
        ),
        "evidenceRefs": [
            _compatible_reference(
                "event",
                str(event_id),
                legacy_type="event",
            )
            for event_id in source_event_ids
        ],
    }


def _change_payload(row: Mapping[str, object]) -> dict[str, object]:
    if not _agent_visible_change(row):
        raise ValueError("cross-project or sensitive memory changes are not Agent-visible")
    supersession_id = str(row["supersession_id"])
    return {
        "supersessionId": supersession_id,
        "oldMemoryId": str(row["old_memory_id"]),
        "newMemoryId": str(row["new_memory_id"]),
        "oldText": str(row["old_text"] or ""),
        "newText": str(row["new_text"] or ""),
        "oldStatus": str(row["old_status"] or ""),
        "newStatus": str(row["new_status"] or ""),
        "reason": str(row["reason"] or ""),
        "status": str(row["status"]),
        "createdAtMs": int(row["created_at_ms"] or 0),
        "rolledBackAtMs": (
            int(row["rolled_back_at_ms"])
            if row["rolled_back_at_ms"] is not None
            else None
        ),
        "source": {"type": "memory_supersession", "id": supersession_id},
        "ref": {"type": "memory_supersession", "id": supersession_id},
    }


def _safe_timeline_segment(
    value: Mapping[str, object],
    *,
    timeline_id: str,
) -> dict[str, object]:
    segment_id = _optional_identifier(
        value.get("segmentId"),
        field="segmentId",
        maximum=240,
    ) or ""
    previews: list[str] = []
    evidence_refs: list[dict[str, object]] = []
    for evidence in _json_array(value.get("evidenceRefs")):
        preview = compact_whitespace(str(evidence.get("preview") or ""))
        if preview and not contains_sensitive_content(preview):
            previews.append(preview[:180])
        event_id = str(evidence.get("eventId") or "")
        source_id = compact_whitespace(str(evidence.get("sourceId") or ""))
        if not event_id and source_id.startswith("event:"):
            event_id = source_id.split(":", 1)[1]
        if event_id.isdigit() and int(event_id) > 0:
            evidence_refs.append(
                _compatible_reference(
                    "event",
                    event_id,
                    legacy_type="event",
                    **({"label": preview[:180]} if preview else {}),
                )
            )
    return {
        "segmentId": segment_id,
        "title": compact_whitespace(str(value.get("title") or ""))[:160],
        "apps": [
            compact_whitespace(str(item))[:240]
            for item in _json_strings(value.get("apps"))
            if compact_whitespace(str(item))
        ][:12],
        "startMs": max(0, int(value.get("startMs") or 0)),
        "endMs": max(0, int(value.get("endMs") or 0)),
        "eventCount": max(0, int(value.get("eventCount") or 0)),
        "previews": previews[:6],
        "evidenceRefs": evidence_refs[:40],
        "source": {
            "type": "activity_timeline",
            "kind": "activity_timeline",
            "id": timeline_id,
        },
        "ref": _compatible_reference(
            "timeline",
            timeline_id,
            legacy_type="timeline",
            segmentId=segment_id,
        ),
    }


def _reject_unexpected_args(
    args: Mapping[str, object],
    *,
    allowed: set[str],
    operation: str,
) -> None:
    accepted = {*allowed, "op"}
    unexpected = sorted(str(key) for key in args if key not in accepted)
    if unexpected:
        raise ValueError(
            f"{operation} received unsupported fields: {', '.join(unexpected)}"
        )


def _evidence_ids(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("evidenceIds must be an array")
    if not 1 <= len(value) <= 16:
        raise ValueError("evidenceIds must contain between 1 and 16 items")
    items = [
        _identifier(item, field=f"evidenceIds[{index}]", maximum=240)
        for index, item in enumerate(value)
    ]
    if len(set(items)) != len(items):
        raise ValueError("evidenceIds must not contain duplicates")
    return items


def _safe_text(
    value: object,
    *,
    field: str,
    maximum: int,
    allow_empty: bool = False,
    prompt_guard: bool,
) -> str:
    if not isinstance(value, str):
        if allow_empty and value is None:
            return ""
        raise ValueError(f"{field} must be a string")
    canonical = unicodedata.normalize("NFKC", value)
    if any(
        (ord(character) < 32 and not character.isspace())
        or unicodedata.category(character) == "Cf"
        for character in canonical
    ):
        raise ValueError(f"{field} contains unsupported control characters")
    normalized = " ".join(canonical.split())
    if not normalized and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} must not exceed {maximum} characters")
    if normalized and contains_sensitive_content(normalized):
        raise ValueError(f"{field} contains sensitive text")
    if (
        prompt_guard
        and normalized
        and any(pattern.search(normalized) for pattern in _PROMPT_INJECTION_PATTERNS)
    ):
        raise ValueError(f"{field} contains prompt injection instructions")
    return normalized


def _identifier(value: object, *, field: str, maximum: int) -> str:
    text = _optional_identifier(value, field=field, maximum=maximum)
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text


def _optional_identifier(value: object, *, field: str, maximum: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if len(text) > maximum:
        raise ValueError(f"{field} must not exceed {maximum} characters")
    if text and (
        any(character.isspace() or unicodedata.category(character) == "Cf" for character in text)
        or not re.fullmatch(r"[\w.@:+/-]+", text, flags=re.UNICODE)
    ):
        raise ValueError(f"{field} contains unsupported characters")
    if text and contains_sensitive_content(text):
        raise ValueError(f"{field} contains sensitive text")
    return text


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        integer = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        integer = default
    return max(minimum, min(integer, maximum))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_array(value: object) -> list[dict[str, object]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parsed = value
    else:
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, Mapping)]


def _json_strings(value: object) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parsed = value
    else:
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item)]


def _input_event_ids_from_provenance(value: Mapping[str, object]) -> list[int]:
    candidates: list[object] = []
    for key in ("eventId", "inputEventId", "sourceEventId"):
        if key in value:
            candidates.append(value[key])
    for key in ("eventIds", "inputEventIds", "sourceEventIds"):
        raw = value.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            candidates.extend(raw)
    source_type = compact_whitespace(str(value.get("sourceType") or "")).lower()
    if source_type in {"event", "input_event"} and value.get("sourceId") is not None:
        candidates.append(value["sourceId"])

    event_ids: list[int] = []
    for candidate in candidates:
        text = compact_whitespace(str(candidate or ""))
        for prefix in ("event:", "input-memory:", "input_event:"):
            if text.lower().startswith(prefix):
                text = text[len(prefix) :]
                break
        if not text.isdigit():
            continue
        event_id = int(text)
        if 0 < event_id <= 9_223_372_036_854_775_807 and event_id not in event_ids:
            event_ids.append(event_id)
    return event_ids


def _input_events_are_memory_eligible(
    conn: sqlite3.Connection,
    event_ids: Sequence[int],
) -> bool:
    unique_ids = list(dict.fromkeys(int(value) for value in event_ids if int(value) > 0))
    if not unique_ids:
        return True
    placeholders = ", ".join("?" for _ in unique_ids)
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM input_events event
        LEFT JOIN memory_state state ON state.event_id = event.id
        WHERE event.id IN ({placeholders})
          AND COALESCE(state.deleted, 0) = 0
          AND NOT EXISTS (
              SELECT 1
              FROM memory_tombstones tombstone
              WHERE tombstone.active = 1
                AND (
                    (tombstone.target_type = 'source_event_id'
                     AND tombstone.target_value = CAST(event.id AS TEXT))
                    OR
                    (tombstone.target_type = 'memory_id'
                     AND tombstone.target_value = ('event:' || event.id))
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM agent_memory_sources source
              WHERE source.input_event_id = event.id
                AND (
                    source.status = 'tombstoned'
                    OR source.disposition IN ('not_for_memory', 'expired')
                    OR source.disposition_reason = 'sensitive_input'
                )
          )
        """,  # noqa: S608 - placeholders are generated, never values
        unique_ids,
    ).fetchone()
    return row is not None and int(row[0] or 0) == len(unique_ids)


def _daily_draft_evidence(
    conn: sqlite3.Connection,
    value: object,
    *,
    project: str,
    role_id: str,
) -> list[dict[str, object]]:
    evidence_ids = _json_strings(value)
    if not evidence_ids:
        raise ValueError("daily draft is missing source evidence")
    if len(evidence_ids) > 1_000:
        raise ValueError("daily draft has too many source evidence rows")
    placeholders = ", ".join("?" for _ in evidence_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM agent_memory_evidence
        WHERE evidence_id IN ({placeholders})
        """,
        evidence_ids,
    ).fetchall()
    by_id = {str(row["evidence_id"]): row for row in rows}
    result: list[dict[str, object]] = []
    for evidence_id in evidence_ids:
        row = by_id.get(evidence_id)
        if row is None:
            raise ValueError(f"daily draft source evidence is missing: {evidence_id}")
        if (
            str(row["project"]) != project
            or str(row["role_id"]) != role_id
            or str(row["status"]) != "active"
        ):
            raise ValueError(
                "daily draft evidence is inactive or belongs to another project/role"
            )
        provenance = _json_object(row["provenance_json"])
        result.append(
            {
                "evidenceId": evidence_id,
                "sourceKind": str(row["source_kind"]),
                "sourceId": str(row["source_id"]),
                "contentSha256": str(row["content_sha256"]),
                "privacyClass": str(row["privacy_class"]),
                "occurredAtMs": int(row["occurred_at_ms"]),
                "provenance": provenance,
            }
        )
    return result


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "AgentRoleBookToolAdapter",
    "MemoryGovernanceProposalStore",
    "build_memory_governance_preview",
]
