from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from .db import apply_database_migrations
from .memory_projection import memory_projection_freshness


_DRAFT_KINDS = frozenset({"user_memory", "role_book", "activity_timeline"})
_DRAFT_DECISIONS = frozenset({"accepted", "rejected", "deferred"})


class PersonalContextObservability:
    """Persist draft decisions and expose text-free acceptance telemetry."""

    def __init__(self, db_path: str | Path, *, project: str = "") -> None:
        self.db_path = Path(db_path)
        self.project = _text(project, field="project", maximum=200, allow_empty=True)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)

    def record_draft_decision(
        self,
        *,
        draft_kind: object,
        draft_id: object,
        decision: object,
        role_id: object = "",
        role_version: object = "",
        run_id: object = "",
        decided_by: object = "control-center-user",
        reason: object = "",
        created_at_ms: int | None = None,
    ) -> dict[str, object]:
        with self._connect(immediate=True) as conn:
            return self.record_draft_decision_in_connection(
                conn,
                draft_kind=draft_kind,
                draft_id=draft_id,
                decision=decision,
                role_id=role_id,
                role_version=role_version,
                run_id=run_id,
                decided_by=decided_by,
                reason=reason,
                created_at_ms=created_at_ms,
            )

    def record_draft_decision_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        draft_kind: object,
        draft_id: object,
        decision: object,
        role_id: object = "",
        role_version: object = "",
        run_id: object = "",
        decided_by: object = "control-center-user",
        reason: object = "",
        created_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Record a decision inside the caller's atomic governance write."""

        kind = _choice(draft_kind, field="draftKind", choices=_DRAFT_KINDS)
        draft = _text(draft_id, field="draftId", maximum=240)
        resolved_decision = _choice(
            decision,
            field="decision",
            choices=_DRAFT_DECISIONS,
        )
        role = _text(role_id, field="roleId", maximum=120, allow_empty=True)
        version = _text(
            role_version,
            field="roleVersion",
            maximum=80,
            allow_empty=True,
        )
        run = _text(run_id, field="runId", maximum=240, allow_empty=True)
        actor = _text(decided_by, field="decidedBy", maximum=120)
        reason_sha256 = hashlib.sha256(
            str(reason or "").encode("utf-8", errors="replace")
        ).hexdigest()
        timestamp = _timestamp(created_at_ms)
        stable_key = "\x1f".join(
            (
                self.project,
                kind,
                draft,
                resolved_decision,
                actor,
            )
        )
        decision_id = (
            "draft-decision:"
            + hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:32]
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO personal_context_draft_decisions(
                decision_id, draft_kind, draft_id, run_id, project,
                role_id, role_version, decision, decided_by,
                reason_sha256, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                kind,
                draft,
                run,
                self.project,
                role,
                version,
                resolved_decision,
                actor,
                reason_sha256,
                timestamp,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM personal_context_draft_decisions
            WHERE decision_id = ?
            """,
            (decision_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("draft decision was not persisted")
        return _decision_payload(row)

    def snapshot(
        self,
        *,
        session_id: object = "",
        role_id: object = "",
        limit: int = 20,
        current_ms: int | None = None,
    ) -> dict[str, object]:
        session = _text(
            session_id,
            field="sessionId",
            maximum=240,
            allow_empty=True,
        )
        role = _text(role_id, field="roleId", maximum=120, allow_empty=True)
        bounded_limit = max(1, min(int(limit), 100))
        timestamp = _timestamp(current_ms)
        with self._connect(readonly=True) as conn:
            if session:
                session_row = conn.execute(
                    """
                    SELECT id, role_id, role_version, role_book_revision_id
                    FROM agent_sessions WHERE id = ?
                    """,
                    (session,),
                ).fetchone()
                if session_row is None:
                    raise ValueError("Agent session does not exist")
                session_role = str(session_row["role_id"] or "")
                if role and session_role != role:
                    raise ValueError("session role does not match observability scope")
                role = session_role
            else:
                session_row = None

            evidence = self._evidence_summary(conn, session=session, role=role)
            bootstrap = self._bootstrap_summary(
                conn,
                session=session,
                role=role,
            )
            runtime = self._runtime_summary(
                conn,
                session=session,
                role=role,
            )
            revisions = self._revision_summary(
                conn,
                session_row=session_row,
                role=role,
            )
            drafts = self._draft_summary(
                conn,
                role=role,
                limit=bounded_limit,
            )
            projection = memory_projection_freshness(
                conn,
                current_ms=timestamp,
            )
        return {
            "schemaVersion": "rag-ime.personal-context-observability.v1",
            "project": self.project,
            "scope": {
                "sessionId": session,
                "roleId": role,
            },
            "revisions": revisions,
            "evidence": evidence,
            "bootstrap": bootstrap,
            "runtime": runtime,
            "drafts": drafts,
            "projection": projection,
            "privacy": {
                "rawTextIncluded": False,
                "decisionReasons": "sha256_only",
            },
            "generatedAtMs": timestamp,
        }

    def _evidence_summary(
        self,
        conn: sqlite3.Connection,
        *,
        session: str,
        role: str,
    ) -> dict[str, object]:
        clauses = ["project = ?"]
        values: list[object] = [self.project]
        if session:
            clauses.append("session_id = ?")
            values.append(session)
        elif role:
            clauses.append("role_id = ?")
            values.append(role)
        rows = conn.execute(
            f"""
            SELECT source_kind, status, COUNT(*) AS count
            FROM agent_memory_evidence
            WHERE {' AND '.join(clauses)}
            GROUP BY source_kind, status
            """,  # noqa: S608 - clauses are fixed strings
            tuple(values),
        ).fetchall()
        by_source: Counter[str] = Counter()
        by_status: Counter[str] = Counter()
        for row in rows:
            count = int(row["count"] or 0)
            by_source[str(row["source_kind"])] += count
            by_status[str(row["status"])] += count
        return {
            "total": sum(by_status.values()),
            "byStatus": dict(sorted(by_status.items())),
            "bySourceKind": dict(sorted(by_source.items())),
        }

    def _bootstrap_summary(
        self,
        conn: sqlite3.Connection,
        *,
        session: str,
        role: str,
    ) -> dict[str, object]:
        clauses = ["i.source_kind = 'memory_bootstrap'"]
        values: list[object] = []
        if session:
            clauses.append("i.session_id = ?")
            values.append(session)
        elif role:
            clauses.append("s.role_id = ?")
            values.append(role)
        rows = conn.execute(
            f"""
            SELECT i.item_id, i.session_id, i.status, i.delivered_turn_id,
                   i.delivered_at_ms
            FROM agent_context_items AS i
            JOIN agent_sessions AS s ON s.id = i.session_id
            WHERE {' AND '.join(clauses)}
            ORDER BY i.created_at_ms DESC
            """,  # noqa: S608 - clauses are fixed strings
            tuple(values),
        ).fetchall()
        status_counts = Counter(str(row["status"]) for row in rows)
        delivered_rows = [
            row for row in rows if int(row["delivered_at_ms"] or 0) > 0
        ]
        delivery_keys = {
            (str(row["session_id"]), str(row["delivered_turn_id"] or ""))
            for row in delivered_rows
        }
        sessions = Counter(str(row["session_id"]) for row in delivered_rows)
        return {
            "items": len(rows),
            "byStatus": dict(sorted(status_counts.items())),
            "deliveryCount": len(delivered_rows),
            "uniqueDeliveryCount": len(delivery_keys),
            "sessionsWithMultipleDeliveries": sum(
                1 for count in sessions.values() if count > 1
            ),
        }

    def _runtime_summary(
        self,
        conn: sqlite3.Connection,
        *,
        session: str,
        role: str,
    ) -> dict[str, object]:
        clauses: list[str] = []
        values: list[object] = []
        if session:
            clauses.append("e.session_id = ?")
            values.append(session)
        elif role:
            clauses.append("s.role_id = ?")
            values.append(role)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT e.session_id, e.turn_id, e.event_type, e.created_at_ms,
                   e.metrics_json
            FROM agent_runtime_events AS e
            JOIN agent_sessions AS s ON s.id = e.session_id
            {where}
            ORDER BY e.created_at_ms, e.sequence
            """,  # noqa: S608 - clauses are fixed strings
            tuple(values),
        ).fetchall()
        tokens = Counter(
            {
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "totalTokens": 0,
            }
        )
        tool_calls = 0
        turns: dict[tuple[str, str], list[int]] = {}
        completed_turns: set[tuple[str, str]] = set()
        for row in rows:
            key = (str(row["session_id"]), str(row["turn_id"] or ""))
            if key[1]:
                turns.setdefault(key, []).append(int(row["created_at_ms"]))
            if str(row["event_type"]) in {"turn_completed", "turn_failed"}:
                completed_turns.add(key)
            metrics = _json_object(row["metrics_json"])
            usage = metrics.get("usage")
            if isinstance(usage, Mapping):
                for token_key in tokens:
                    tokens[token_key] += _nonnegative_int(usage.get(token_key))
            tool_calls += _nonnegative_int(metrics.get("toolCalls"))
        latencies = sorted(
            max(times) - min(times)
            for key, times in turns.items()
            if key in completed_turns and len(times) >= 2
        )

        trace_clauses: list[str] = []
        trace_values: list[object] = []
        if session:
            trace_clauses.append("t.session_id = ?")
            trace_values.append(session)
        elif role:
            trace_clauses.append(
                "t.session_id IN (SELECT id FROM agent_sessions WHERE role_id = ?)"
            )
            trace_values.append(role)
        trace_where = (
            f"WHERE {' AND '.join(trace_clauses)}"
            if trace_clauses
            else ""
        )
        context_tokens = int(
            conn.execute(
                f"""
                SELECT COALESCE(SUM(n.token_estimate), 0)
                FROM agent_context_trace_nodes AS n
                JOIN agent_context_traces AS t ON t.trace_id = n.trace_id
                {trace_where}
                """,  # noqa: S608 - clauses are fixed strings
                tuple(trace_values),
            ).fetchone()[0]
            or 0
        )
        return {
            "eventCount": len(rows),
            "turnCount": len(turns),
            "completedTurnCount": len(completed_turns),
            "toolCallCount": tool_calls,
            "tokens": dict(tokens),
            "contextTokenEstimate": context_tokens,
            "latencyMs": {
                "count": len(latencies),
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
                "max": max(latencies, default=0),
            },
        }

    def _revision_summary(
        self,
        conn: sqlite3.Connection,
        *,
        session_row: sqlite3.Row | None,
        role: str,
    ) -> dict[str, object]:
        clauses: list[str] = []
        values: list[object] = []
        if role:
            clauses.append("role_id = ?")
            values.append(role)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        status_rows = conn.execute(
            f"""
            SELECT status, COUNT(*) AS count
            FROM agent_role_book_revisions
            {where}
            GROUP BY status
            """,  # noqa: S608 - clauses are fixed strings
            tuple(values),
        ).fetchall()
        event_rows = conn.execute(
            f"""
            SELECT event_type, COUNT(*) AS count
            FROM agent_role_book_activation_events
            {where}
            GROUP BY event_type
            """,  # noqa: S608 - clauses are fixed strings
            tuple(values),
        ).fetchall()
        return {
            "pinnedRevisionId": (
                str(session_row["role_book_revision_id"] or "")
                if session_row is not None
                else ""
            ),
            "byStatus": {
                str(row["status"]): int(row["count"])
                for row in status_rows
            },
            "activationEvents": {
                str(row["event_type"]): int(row["count"])
                for row in event_rows
            },
        }

    def _draft_summary(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        limit: int,
    ) -> dict[str, object]:
        clauses = ["project = ?"]
        values: list[object] = [self.project]
        if role:
            clauses.append("role_id = ?")
            values.append(role)
        rows = conn.execute(
            f"""
            SELECT * FROM personal_context_draft_decisions
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at_ms DESC, decision_id DESC
            """,  # noqa: S608 - clauses are fixed strings
            tuple(values),
        ).fetchall()
        latest_by_draft: dict[tuple[str, str], sqlite3.Row] = {}
        for row in rows:
            latest_by_draft.setdefault(
                (str(row["draft_kind"]), str(row["draft_id"])),
                row,
            )
        latest_rows = list(latest_by_draft.values())
        decisions = Counter(str(row["decision"]) for row in latest_rows)
        resolved = decisions["accepted"] + decisions["rejected"]
        generated = self._generated_draft_counts(conn, role=role)
        timeline_statuses = self._activity_timeline_status_counts(
            conn,
            role=role,
        )
        return {
            "generatedByKind": generated,
            "activityTimelineByStatus": timeline_statuses,
            "latestDecisionByOutcome": dict(sorted(decisions.items())),
            "acceptanceRate": (
                round(decisions["accepted"] / resolved, 6)
                if resolved
                else None
            ),
            "decidedDrafts": len(latest_rows),
            "latest": [
                _decision_payload(row)
                for row in latest_rows[:limit]
            ],
        }

    def _generated_draft_counts(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
    ) -> dict[str, int]:
        clauses = ["project = ?", "status = 'succeeded'"]
        values: list[object] = [self.project]
        if role:
            clauses.append("role_id = ?")
            values.append(role)
        rows = conn.execute(
            f"""
            SELECT output_json
            FROM personal_context_consolidation_runs
            WHERE {' AND '.join(clauses)}
            """,  # noqa: S608 - clauses are fixed strings
            tuple(values),
        ).fetchall()
        counts = Counter(
            {
                "user_memory": 0,
                "role_book": 0,
                "activity_timeline": 0,
            }
        )
        for row in rows:
            output = _json_object(row["output_json"])
            if isinstance(output.get("userMemoryDraft"), Mapping):
                counts["user_memory"] += 1
            if isinstance(output.get("roleBookDraft"), Mapping):
                counts["role_book"] += 1
        if not role:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM daily_activity_timelines
                WHERE project = ?
                """,
                (self.project,),
            ).fetchone()
            counts["activity_timeline"] = int(row["count"] or 0) if row else 0
        return dict(counts)

    def _activity_timeline_status_counts(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
    ) -> dict[str, int]:
        if role:
            return {}
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM daily_activity_timelines
            WHERE project = ?
            GROUP BY status
            ORDER BY status
            """,
            (self.project,),
        ).fetchall()
        return {
            str(row["status"]): int(row["count"] or 0)
            for row in rows
        }

    @contextmanager
    def _connect(
        self,
        *,
        immediate: bool = False,
        readonly: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        if readonly:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        else:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        if readonly:
            conn.execute("PRAGMA query_only=ON")
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


def _decision_payload(row: sqlite3.Row) -> dict[str, object]:
    return {
        "decisionId": str(row["decision_id"]),
        "draftKind": str(row["draft_kind"]),
        "draftId": str(row["draft_id"]),
        "runId": str(row["run_id"]),
        "project": str(row["project"]),
        "roleId": str(row["role_id"]),
        "roleVersion": str(row["role_version"]),
        "decision": str(row["decision"]),
        "decidedBy": str(row["decided_by"]),
        "reasonSha256": str(row["reason_sha256"]),
        "createdAtMs": int(row["created_at_ms"]),
    }


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _percentile(values: Sequence[int], fraction: float) -> int:
    if not values:
        return 0
    index = max(
        0,
        min(
            len(values) - 1,
            int(math.ceil(len(values) * fraction) - 1),
        ),
    )
    return int(values[index])


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _choice(value: object, *, field: str, choices: frozenset[str]) -> str:
    normalized = _text(value, field=field, maximum=80)
    if normalized not in choices:
        raise ValueError(f"{field} is not supported")
    return normalized


def _text(
    value: object,
    *,
    field: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} is too long")
    return normalized


def _timestamp(value: int | None) -> int:
    return max(0, int(value if value is not None else time.time() * 1000))


__all__ = ["PersonalContextObservability"]
