from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .agent_artifacts import AgentArtifactStore
from .agent_context_runtime import AgentContextRuntime
from .agent_events import AgentEventHub
from .agent_execution_policy import (
    FULL_TRUST_EXECUTION_MODE,
    PER_ACTION_EXECUTION_MODE,
    READ_ONLY_EXECUTION_MODE,
    WORKSPACE_MANAGED_EXECUTION_MODE,
    unrestricted_workspace_policy_active,
)
from .agent_tool_ids import (
    DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
    FULL_ACCESS_TOOL_PROFILE,
    READONLY_TOOL_PROFILE,
)
from .agent_protocol import AgentEventEnvelope
from .agent_runtime_driver import (
    AgentRuntimeDriver,
    AgentRuntimeError,
    CompactionObserver,
    RuntimeDriverContext,
    RuntimeDriverFactory,
    ToolManifestProvider,
)
from .agent_sessions import AgentSessionStore
from .agent_templates import (
    AgentTemplate,
    AgentTemplateBudget,
    agent_template,
    agent_template_catalog,
)
from .agent_workspace_roots import existing_workspace_roots, system_wide_workspace_roots
from .contracts.json_schema import validate_contract, validate_json_schema
from .db import apply_database_migrations
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.factory import PiRuntimeDriverFactory


_TERMINAL_STATES = frozenset({"completed", "failed", "aborted", "timed_out"})
_ACTIVE_STATES = frozenset({"queued", "running"})
_MAX_PARALLEL_RUNS = 2
_MAX_FORK_BYTES = 32 * 1024 * 1024
_SOFT_BUDGET_RATIO = 0.8
_DEFAULT_CANCELLATION_GRACE_MS = 2_000
_DEFAULT_SUBAGENT_SESSION_RETENTION_MS = 72 * 60 * 60 * 1_000
_DEFAULT_SUBAGENT_SESSION_GC_INTERVAL_MS = 15 * 60 * 1_000
_ROOM_PERMISSION_MODES = frozenset(
    {
        READ_ONLY_EXECUTION_MODE,
        PER_ACTION_EXECUTION_MODE,
        WORKSPACE_MANAGED_EXECUTION_MODE,
        FULL_TRUST_EXECUTION_MODE,
    }
)
_ROOM_PERMISSION_MODE_RANK = {
    READ_ONLY_EXECUTION_MODE: 0,
    PER_ACTION_EXECUTION_MODE: 1,
    WORKSPACE_MANAGED_EXECUTION_MODE: 1,
    FULL_TRUST_EXECUTION_MODE: 2,
}
_DELEGATION_TASK_CONTRACT: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "agent",
        "version",
        "task",
        "expectedOutput",
        "acceptanceCriteria",
    ],
    "properties": {
        "agent": {"type": "string", "minLength": 1, "maxLength": 40},
        "version": {"type": "string", "const": "1"},
        "task": {"type": "string", "minLength": 1, "maxLength": 8_000},
        "expectedOutput": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2_000,
        },
        "acceptanceCriteria": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 1_000},
        },
        "outputSchema": {"type": "object"},
        "modelProfile": {
            "type": "string",
            "minLength": 3,
            "maxLength": 240,
            "pattern": r"^[^/\s]+/[^/\s]+$",
            "description": (
                "Omit to inherit the parent Session; an override must be an exact "
                "Pi-confirmed provider/model selected by the user, never a guessed alias."
            ),
        },
        "thinkingLevel": {
            "type": "string",
            "enum": ["off", "minimal", "low", "medium", "high", "xhigh", "max"],
        },
        "budget": {
            "type": "object",
            "additionalProperties": False,
            "minProperties": 1,
            "properties": {
                "maxTotalTokens": {
                    "type": "integer",
                    "minimum": 256,
                    "maximum": 262_144,
                },
                "maxDurationMs": {
                    "type": "integer",
                    "minimum": 1_000,
                    "maximum": 900_000,
                },
                "maxOutputChars": {
                    "type": "integer",
                    "minimum": 256,
                    "maximum": 100_000,
                },
            },
        },
        "access": {
            "type": "string",
            "enum": ["inherit", "read_only", "write"],
        },
        "allowedTools": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 120},
        },
        "piSkillsEnabled": {"type": "boolean"},
        "codexSkillsEnabled": {"type": "boolean"},
        "workspaceRoots": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 1024},
        },
    },
}


class AgentDelegationStore:
    """Durable lifecycle index for bounded task subagents."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        artifacts: AgentArtifactStore | None = None,
    ):
        self.db_path = Path(db_path)
        self.artifacts = artifacts

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)
        if self.artifacts is not None:
            self.artifacts.initialize()
            self._sync_all_artifacts()

    def create_batch(
        self,
        *,
        parent_session_id: str,
        parent_run_id: str,
        context_mode: str,
        depth: int,
        max_depth: int,
        runs: Sequence[Mapping[str, object]],
        result_delivery_mode: str = "inline",
        causal_metadata: Mapping[str, object] | None = None,
        created_at_ms: int | None = None,
    ) -> dict[str, object]:
        values = [dict(item) for item in runs]
        if not 1 <= len(values) <= _MAX_PARALLEL_RUNS:
            raise ValueError("delegation requires one or two tasks")
        if context_mode not in {"fresh", "fork"}:
            raise ValueError("delegation contextMode must be fresh or fork")
        if result_delivery_mode not in {"inline", "next_turn"}:
            raise ValueError("delegation result delivery mode is invalid")
        if not 1 <= depth <= max_depth <= 2:
            raise ValueError("delegation depth is outside the managed limit")
        now = _timestamp(created_at_ms)
        batch_id = f"subagent-batch:{uuid.uuid4()}"
        run_ids: list[str] = []
        causal = _delegation_causal_metadata(causal_metadata)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_subagent_batches(
                    id, parent_session_id, parent_run_id, context_mode, state,
                    result_delivery_mode, depth, max_depth, causal_todo_id,
                    causal_todo_revision, causal_goal_id, causal_goal_revision,
                    room_bound, causal_room_id, causal_root_id, causal_task_id,
                    causal_dispatch_id, causal_generation, created_at_ms, updated_at_ms
                ) VALUES (
                    ?, ?, ?, ?,
                    'queued',
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?
                )
                """,
                (
                    batch_id,
                    parent_session_id,
                    parent_run_id,
                    context_mode,
                    result_delivery_mode,
                    depth,
                    max_depth,
                    causal["todoId"],
                    causal["todoRevision"],
                    causal["goalId"],
                    causal["goalRevision"],
                    int(bool(causal["roomBound"])),
                    causal["roomId"],
                    causal["rootId"],
                    causal["taskId"],
                    causal["dispatchId"],
                    causal["generation"],
                    now,
                    now,
                ),
            )
            for ordinal, value in enumerate(values):
                run_id = f"subagent-run:{uuid.uuid4()}"
                node_id = _bounded_text(
                    value.get("nodeId") or f"subagent-node:{uuid.uuid4()}",
                    maximum=240,
                    required=True,
                )
                attempt_id = _bounded_text(
                    value.get("attemptId") or f"subagent-attempt:{uuid.uuid4()}",
                    maximum=240,
                    required=True,
                )
                attempt_number = _bounded_int(
                    value.get("attemptNumber") or 1,
                    minimum=1,
                    maximum=1_000,
                )
                predecessor_attempt_id = _bounded_text(
                    value.get("predecessorAttemptId"),
                    maximum=240,
                )
                owner_run_id = _bounded_text(
                    value.get("ownerRunId")
                    or parent_run_id
                    or f"session:{parent_session_id}",
                    maximum=240,
                    required=True,
                )
                launch_digest = _delegation_launch_digest(
                    value.get("launchDigest"),
                    context_mode=context_mode,
                    template_id=_required_text(value, "templateId"),
                    template_version=_required_text(value, "templateVersion"),
                    output_schema=value.get("outputSchema"),
                )
                run_ids.append(run_id)
                child_session_id = _required_text(value, "childSessionId")
                marked = conn.execute(
                    """
                    UPDATE agent_sessions
                    SET session_kind = 'subagent_runtime'
                    WHERE id = ?
                    """,
                    (child_session_id,),
                )
                if marked.rowcount != 1:
                    raise ValueError("delegated child session does not exist")
                conn.execute(
                    """
                    INSERT INTO agent_subagent_runs(
                        id, batch_id, child_session_id, template_id, template_version,
                        logical_node_id, attempt_id, attempt_number,
                        predecessor_attempt_id, owner_run_id, parent_run_id, depth,
                        ordinal, task_text, expected_output, acceptance_criteria_json,
                        output_schema_json, launch_digest_json,
                        todo_task, todo_phase, state,
                        max_turns, max_tool_calls, max_total_tokens, max_duration_ms,
                        max_output_chars, created_at_ms, updated_at_ms
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        run_id,
                        batch_id,
                        child_session_id,
                        _required_text(value, "templateId"),
                        _required_text(value, "templateVersion"),
                        node_id,
                        attempt_id,
                        attempt_number,
                        predecessor_attempt_id,
                        owner_run_id,
                        parent_run_id,
                        depth,
                        ordinal,
                        _bounded_task(value.get("task")),
                        _delegation_expected_output(value.get("expectedOutput")),
                        json.dumps(
                            _delegation_acceptance_criteria(value.get("acceptanceCriteria")),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            _delegation_output_schema(value.get("outputSchema")),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            launch_digest,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        _bounded_text(value.get("todoTask"), maximum=240),
                        _bounded_text(value.get("todoPhase"), maximum=80),
                        _bounded_int(value.get("maxTurns"), minimum=0, maximum=32),
                        _bounded_int(value.get("maxToolCalls"), minimum=0, maximum=64),
                        _bounded_int(value.get("maxTotalTokens"), minimum=256, maximum=262_144),
                        _bounded_int(value.get("maxDurationMs"), minimum=1_000, maximum=900_000),
                        _bounded_int(value.get("maxOutputChars"), minimum=256, maximum=100_000),
                        now,
                        now,
                    ),
                )
                self._append_event_conn(
                    conn,
                    run_id=run_id,
                    event_type="queued",
                    payload={
                        "batchId": batch_id,
                        "nodeId": node_id,
                        "attemptId": attempt_id,
                        "attemptNumber": attempt_number,
                        "ordinal": ordinal,
                        "todoTask": _bounded_text(value.get("todoTask"), maximum=240),
                        "todoPhase": _bounded_text(
                            value.get("todoPhase"),
                            maximum=80,
                        ),
                    },
                    created_at_ms=now,
                )
        # The SQLite rows above are the acceptance receipt.  Lifecycle
        # artifacts are advisory and are materialized by the worker so a
        # contended artifact lock cannot delay wait=false acknowledgement.
        return self.get_batch(batch_id, hydrate_artifacts=False)

    def get_batch(
        self,
        batch_id: str,
        *,
        hydrate_artifacts: bool = True,
    ) -> dict[str, object]:

        with self._connect() as conn:
            # A batch projection is made from two related rows.  In SQLite,
            # SELECTs do not start a transaction by themselves, so without an
            # explicit read transaction a concurrent terminal callback can
            # commit between these queries and produce the impossible
            # combination ``batch.state == running`` with an aborted run (or
            # the reverse).  Lifecycle cancellation records this projection
            # as its replay receipt; keep the batch and its runs on one
            # snapshot so the first receipt is byte-for-byte replayable.
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT * FROM agent_subagent_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise KeyError(batch_id)
            runs = conn.execute(
                "SELECT * FROM agent_subagent_runs WHERE batch_id = ? ORDER BY ordinal ASC",
                (batch_id,),
            ).fetchall()
        payload = _batch_payload(row, runs)
        if self.artifacts is not None and hydrate_artifacts:
            payload["runs"] = [self._decorate_run(run) for run in payload["runs"]]
            for run in payload["runs"]:
                validate_contract(run, "agent-subagent-run.v1.json")
            validate_contract(payload, "agent-subagent-batch.v1.json")
        return payload

    def get_run(
        self,
        run_id: str,
        *,
        hydrate_artifacts: bool = True,
    ) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_subagent_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        payload = _run_payload(row)
        return self._decorate_run(payload) if self.artifacts is not None and hydrate_artifacts else payload


    def submit_structured_output(
        self,
        *,
        child_session_id: str,
        value: object,
        tool_call_id: str,
        validated_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Validate and persist the one terminal contract value for a child run."""

        call_id = _bounded_text(tool_call_id, maximum=240, required=True)
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("structured output must contain only JSON values") from exc
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("structured output exceeds 256 KiB")
        now = _timestamp(validated_at_ms)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, state, output_schema_json, structured_output_json,
                       structured_output_tool_call_id
                FROM agent_subagent_runs
                WHERE child_session_id = ?
                """,
                (child_session_id,),
            ).fetchone()
            if row is None:
                raise ValueError("structured_output is only available to a delegated child")
            run_id = str(row["id"])
            schema, schema_error = _stored_output_schema(row["output_schema_json"])
            if schema_error:
                validation_message = _bounded_text(schema_error, maximum=500)
                conn.execute(
                    """
                    UPDATE agent_subagent_runs
                    SET structured_output_error = ?, updated_at_ms = ?
                    WHERE id = ? AND structured_output_tool_call_id = ''
                    """,
                    (validation_message, now, run_id),
                )
                self._append_event_conn(
                    conn,
                    run_id=run_id,
                    event_type="progress",
                    payload={"contractStatus": "invalid", "error": validation_message},
                    created_at_ms=now,
                )
            elif schema is None:
                raise ValueError("this delegated task did not request structured output")
            existing_call_id = str(row["structured_output_tool_call_id"] or "")
            if existing_call_id:
                existing = str(row["structured_output_json"] or "{}")
                if existing_call_id == call_id and existing == encoded:
                    return self.get_run(run_id)
                raise ValueError("structured output was already submitted for this attempt")
            if str(row["state"]) not in _ACTIVE_STATES:
                raise ValueError("the delegated attempt is no longer accepting output")
            validation_message = schema_error
            if not validation_message:
                try:
                    validate_contract(value, schema)
                except ValueError as exc:
                    validation_message = _bounded_text(
                        f"delivery contract validation failed: {exc}",
                        maximum=500,
                    )
            if validation_message and not schema_error:
                conn.execute(
                    """
                    UPDATE agent_subagent_runs
                    SET structured_output_error = ?, updated_at_ms = ?
                    WHERE id = ? AND structured_output_tool_call_id = ''
                    """,
                    (validation_message, now, run_id),
                )
                self._append_event_conn(
                    conn,
                    run_id=run_id,
                    event_type="progress",
                    payload={
                        "contractStatus": "invalid",
                        "error": validation_message,
                    },
                    created_at_ms=now,
                )
            if not validation_message:
                cursor = conn.execute(
                    """
                    UPDATE agent_subagent_runs
                    SET structured_output_json = ?, structured_output_tool_call_id = ?,
                        structured_output_error = '', structured_output_validated_at_ms = ?,
                        updated_at_ms = ?
                    WHERE id = ? AND structured_output_tool_call_id = ''
                    """,
                    (encoded, call_id, now, now, run_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("structured output was already submitted for this attempt")
                self._append_event_conn(
                    conn,
                    run_id=run_id,
                    event_type="progress",
                    payload={"contractStatus": "valid", "toolCallId": call_id},
                    created_at_ms=now,
                )
        self._sync_run_artifact(run_id)
        if validation_message:
            raise ValueError(validation_message)
        return self.get_run(run_id)

    def finalize_structured_output_contract(
        self,
        run_id: str,
        *,
        completed_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Turn a missing terminal tool call into a local delivery-contract failure."""

        now = _timestamp(completed_at_ms)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT output_schema_json, structured_output_tool_call_id,
                       structured_output_error
                FROM agent_subagent_runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            schema, schema_error = _stored_output_schema(row["output_schema_json"])
            if schema_error:
                conn.execute(
                    """
                    UPDATE agent_subagent_runs
                    SET structured_output_error = ?, updated_at_ms = ?
                    WHERE id = ? AND structured_output_tool_call_id = ''
                    """,
                    (_bounded_text(schema_error, maximum=500), now, run_id),
                )
            elif schema is not None and not str(row["structured_output_tool_call_id"] or ""):
                message = str(row["structured_output_error"] or "").strip()
                if not message:
                    message = (
                        "delivery contract validation failed: structured_output "
                        "was not submitted before the turn completed"
                    )
                conn.execute(
                    """
                    UPDATE agent_subagent_runs
                    SET structured_output_error = ?, updated_at_ms = ?
                    WHERE id = ? AND structured_output_tool_call_id = ''
                    """,
                    (_bounded_text(message, maximum=500), now, run_id),
                )
        self._sync_run_artifact(run_id)
        return self.get_run(run_id)

    def run_for_child_session(self, session_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.*, b.parent_session_id, b.parent_run_id, b.context_mode,
                       b.depth, b.max_depth, b.abort_requested
                FROM agent_subagent_runs r
                JOIN agent_subagent_batches b ON b.id = r.batch_id
                WHERE r.child_session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        payload = _run_payload(row)
        payload.update(
            {
                "parentSessionId": str(row["parent_session_id"]),
                "parentRunId": str(row["parent_run_id"] or ""),
                "contextMode": str(row["context_mode"]),
                "depth": int(row["depth"]),
                "maxDepth": int(row["max_depth"]),
                "abortRequested": bool(row["abort_requested"]),
            }
        )
        return payload

    def list_batches(
        self,
        *,
        parent_session_id: str = "",
        limit: int = 50,
        hydrate_artifacts: bool = True,
    ) -> list[dict[str, object]]:
        bounded = max(1, min(int(limit), 200))
        with self._connect() as conn:
            if hydrate_artifacts:
                if parent_session_id:
                    rows = conn.execute(
                        """
                        SELECT id FROM agent_subagent_batches
                        WHERE parent_session_id = ? ORDER BY created_at_ms DESC LIMIT ?
                        """,
                        (parent_session_id, bounded),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id FROM agent_subagent_batches ORDER BY created_at_ms DESC LIMIT ?",
                        (bounded,),
                    ).fetchall()
                batch_ids = [str(row["id"]) for row in rows]
            else:
                conn.execute("BEGIN")
                if parent_session_id:
                    batch_rows = conn.execute(
                        """
                        SELECT * FROM agent_subagent_batches
                        WHERE parent_session_id = ?
                        ORDER BY created_at_ms DESC LIMIT ?
                        """,
                        (parent_session_id, bounded),
                    ).fetchall()
                else:
                    batch_rows = conn.execute(
                        """
                        SELECT * FROM agent_subagent_batches
                        ORDER BY created_at_ms DESC LIMIT ?
                        """,
                        (bounded,),
                    ).fetchall()
                if not batch_rows:
                    return []
                batch_ids = [str(row["id"]) for row in batch_rows]
                placeholders = ",".join("?" for _ in batch_ids)
                run_rows = conn.execute(
                    f"""
                    SELECT * FROM agent_subagent_runs
                    WHERE batch_id IN ({placeholders})
                    ORDER BY batch_id, ordinal ASC
                    """,  # noqa: S608 - placeholders are generated internally.
                    tuple(batch_ids),
                ).fetchall()
        if hydrate_artifacts:
            return [self.get_batch(batch_id) for batch_id in batch_ids]
        runs_by_batch: dict[str, list[sqlite3.Row]] = {batch_id: [] for batch_id in batch_ids}
        for run_row in run_rows:
            runs_by_batch[str(run_row["batch_id"])].append(run_row)
        return [
            _batch_payload(
                batch_row,
                runs_by_batch[str(batch_row["id"])],
            )
            for batch_row in batch_rows
        ]

    def active_run_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM agent_subagent_runs WHERE state IN ('queued', 'running')"
            ).fetchone()
        return int(row[0] if row else 0)
    def room_delivery_allowed(self, run_id: str) -> bool:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT b.abort_requested
                    FROM agent_subagent_runs AS r
                    JOIN agent_subagent_batches AS b ON b.id = r.batch_id
                    WHERE r.id = ?
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            return False
        if row is None:
            return False
        # The parent Session owns child delivery. Abort is the only durable
        # fence; there is no parallel Room Kernel generation to consult.
        return not bool(row["abort_requested"])

    def terminal_child_sessions(
        self,
        *,
        completed_before_ms: int | None = None,
    ) -> list[tuple[str, str]]:
        clauses = ["state IN ('completed', 'failed', 'aborted', 'timed_out')"]
        values: list[object] = []
        if completed_before_ms is not None:
            clauses.append("COALESCE(completed_at_ms, updated_at_ms) <= ?")
            values.append(int(completed_before_ms))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, child_session_id
                FROM agent_subagent_runs
                WHERE {' AND '.join(clauses)}
                ORDER BY completed_at_ms, id
                """,  # noqa: S608 - clauses are fixed above.
                tuple(values),
            ).fetchall()
        return [(str(row["id"]), str(row["child_session_id"])) for row in rows]

    def start_run(self, run_id: str, *, started_at_ms: int | None = None) -> dict[str, object]:
        now = _timestamp(started_at_ms)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_subagent_runs
                SET state = 'running', started_at_ms = ?, updated_at_ms = ?
                WHERE id = ? AND state = 'queued'
                """,
                (now, now, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("delegated run is no longer queued")
            self._append_event_conn(
                conn,
                run_id=run_id,
                event_type="started",
                payload={},
                created_at_ms=now,
            )
            batch_id = str(
                conn.execute(
                    "SELECT batch_id FROM agent_subagent_runs WHERE id = ?", (run_id,)
                ).fetchone()[0]
            )
            self._refresh_batch_conn(conn, batch_id, updated_at_ms=now)
        self._sync_run_artifact(run_id)
        return self.get_run(run_id)

    def update_usage(
        self,
        run_id: str,
        *,
        turn_count: int,
        tool_count: int,
        total_tokens: int,
        summary: str = "",
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        now = _timestamp(updated_at_ms)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_subagent_runs
                SET turn_count = ?, tool_count = ?, total_tokens = ?, updated_at_ms = ?
                WHERE id = ? AND state = 'running'
                """,
                (
                    max(0, int(turn_count)),
                    max(0, int(tool_count)),
                    max(0, int(total_tokens)),
                    now,
                    run_id,
                ),
            )
            if cursor.rowcount == 1 and summary:
                self._append_event_conn(
                    conn,
                    run_id=run_id,
                    event_type="progress",
                    payload={"summary": _bounded_text(summary, maximum=240)},
                    created_at_ms=now,
                )
        self._sync_run_artifact(run_id)
        return self.get_run(run_id)

    def finish_run(
        self,
        run_id: str,
        *,
        state: str,
        result: Mapping[str, object] | None = None,
        error: str = "",
        turn_count: int = 0,
        tool_count: int = 0,
        total_tokens: int = 0,
        completed_at_ms: int | None = None,
    ) -> dict[str, object]:
        if state not in _TERMINAL_STATES:
            raise ValueError("delegated run terminal state is invalid")
        now = _timestamp(completed_at_ms)
        already_terminal = False
        with self._connect() as conn:
            # Serialize terminal selection with the write. A late runtime
            # callback may race cancellation or a sibling terminal callback,
            # but exactly one of them owns the durable terminal transition.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT batch_id, state FROM agent_subagent_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            result_json = json.dumps(
                dict(result or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            already_terminal = str(row["state"]) in _TERMINAL_STATES
            if not already_terminal:
                conn.execute(
                    """
                    UPDATE agent_subagent_runs
                    SET state = ?, result_json = ?, error = ?, turn_count = ?,
                        tool_count = ?, total_tokens = ?, updated_at_ms = ?, completed_at_ms = ?
                    WHERE id = ?
                    """,
                    (
                        state,
                        result_json,
                        _bounded_text(error, maximum=500),
                        max(0, int(turn_count)),
                        max(0, int(tool_count)),
                        max(0, int(total_tokens)),
                        now,
                        now,
                        run_id,
                    ),
                )
                self._append_event_conn(
                    conn,
                    run_id=run_id,
                    event_type=state,
                    payload={"error": _bounded_text(error, maximum=240)} if error else {},
                    created_at_ms=now,
                )
                self._refresh_batch_conn(conn, str(row["batch_id"]), updated_at_ms=now)
        self._sync_run_artifact(run_id)
        return self.get_run(run_id)

    def pending_result_context_runs(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.id AS run_id, r.batch_id, b.parent_session_id
                FROM agent_subagent_runs r
                JOIN agent_subagent_batches b ON b.id = r.batch_id
                WHERE b.result_delivery_mode = 'next_turn'
                  AND r.state IN ('completed', 'failed', 'aborted', 'timed_out')
                  AND r.result_context_scheduled_at_ms IS NULL
                ORDER BY r.completed_at_ms, r.id
                """
            ).fetchall()
        return [
            {
                "runId": str(row["run_id"]),
                "batchId": str(row["batch_id"]),
                "parentSessionId": str(row["parent_session_id"]),
            }
            for row in rows
        ]

    def mark_result_context_scheduled(
        self,
        run_id: str,
        *,
        scheduled_at_ms: int | None = None,
        sync_artifacts: bool = True,
    ) -> dict[str, object]:
        now = _timestamp(scheduled_at_ms)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_subagent_runs
                SET result_context_scheduled_at_ms = ?, updated_at_ms = ?
                WHERE id = ? AND state IN ('completed', 'failed', 'aborted', 'timed_out')
                  AND result_context_scheduled_at_ms IS NULL
                """,
                (now, now, run_id),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT state, result_context_scheduled_at_ms FROM agent_subagent_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(run_id)
                if row["result_context_scheduled_at_ms"] is None:
                    raise ValueError("delegated run is not ready for result context")
        if sync_artifacts:
            self._sync_run_artifact(run_id)
        return self.get_run(run_id, hydrate_artifacts=sync_artifacts)

    def request_abort(
        self, identifier: str, *, requested_at_ms: int | None = None,
        sync_artifacts: bool = True,
    ) -> list[str]:
        now = _timestamp(requested_at_ms)
        affected: list[str] = []
        with self._connect() as conn:
            batch = conn.execute(
                "SELECT id FROM agent_subagent_batches WHERE id = ?", (identifier,)
            ).fetchone()
            if batch is not None:
                batch_id = identifier
                rows = conn.execute(
                    "SELECT id, state FROM agent_subagent_runs WHERE batch_id = ?",
                    (batch_id,),
                ).fetchall()
            else:
                run = conn.execute(
                    "SELECT id, batch_id, state FROM agent_subagent_runs WHERE id = ?",
                    (identifier,),
                ).fetchone()
                if run is None:
                    raise KeyError(identifier)
                batch_id = str(run["batch_id"])
                rows = conn.execute(
                    "SELECT id, state FROM agent_subagent_runs WHERE batch_id = ?",
                    (batch_id,),
                ).fetchall()
            conn.execute(
                "UPDATE agent_subagent_batches SET abort_requested = 1, updated_at_ms = ? WHERE id = ?",
                (now, batch_id),
            )
            active: list[str] = []
            for row in rows:
                run_id = str(row["id"])
                affected.append(run_id)
                state = str(row["state"])
                if state == "queued":
                    conn.execute(
                        """
                        UPDATE agent_subagent_runs
                        SET state = 'aborted', error = 'Stopped before launch',
                            updated_at_ms = ?, completed_at_ms = ?
                        WHERE id = ?
                        """,
                        (now, now, run_id),
                    )
                    self._append_event_conn(
                        conn,
                        run_id=run_id,
                        event_type="aborted",
                        payload={"reason": "user_stop"},
                        created_at_ms=now,
                    )
                elif state == "running":
                    active.append(run_id)
            self._refresh_batch_conn(conn, batch_id, updated_at_ms=now)
        if sync_artifacts:
            for run_id in affected:
                self._sync_run_artifact(run_id)
        return active

    def request_causal_abort(
        self,
        *,
        request_id: str,
        scope_kind: str,
        scope_id: str,
        source_revision: int,
        reason: str,
        requested_at_ms: int | None = None,
        sync_artifacts: bool = True,
    ) -> dict[str, object]:
        if scope_kind != "goal":
            raise ValueError("unsupported delegation cancellation scope")
        normalized_request_id = _bounded_text(
            request_id,
            maximum=240,
            required=True,
        )
        normalized_reason = (
            _bounded_text(reason, maximum=240)
            or "Parent lifecycle cancelled"
        )
        causal_clause = "causal_goal_id = ?"
        causal_values: tuple[object, ...] = (scope_id,)
        now = _timestamp(requested_at_ms)
        active_run_ids: list[str] = []
        affected_run_ids: list[str] = []
        batch_ids: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            excluded_rows = conn.execute(
                f"""
                SELECT id
                FROM agent_subagent_batches
                WHERE {causal_clause} AND room_bound = 1
                  AND state IN ('queued', 'running')
                ORDER BY created_at_ms, id
                """,
                causal_values,
            ).fetchall()
            rows = conn.execute(
                f"""
                SELECT id, state, lifecycle_cancel_request_id
                FROM agent_subagent_batches
                WHERE room_bound = 0
                  AND (
                    ({causal_clause} AND state IN ('queued', 'running'))
                    OR lifecycle_cancel_request_id = ?
                  )
                ORDER BY created_at_ms, id
                """,
                (*causal_values, normalized_request_id),
            ).fetchall()
            for batch in rows:
                batch_id = str(batch["id"])
                current_owner = str(
                    batch["lifecycle_cancel_request_id"] or ""
                )
                if current_owner not in {"", normalized_request_id}:
                    continue
                batch_ids.append(batch_id)
                conn.execute(
                    """
                    UPDATE agent_subagent_batches
                    SET abort_requested = 1,
                        lifecycle_cancel_request_id = ?,
                        updated_at_ms = ?
                    WHERE id = ?
                      AND lifecycle_cancel_request_id IN ('', ?)
                    """,
                    (
                        normalized_request_id,
                        now,
                        batch_id,
                        normalized_request_id,
                    ),
                )
                runs = conn.execute(
                    """
                    SELECT id, state
                    FROM agent_subagent_runs
                    WHERE batch_id = ?
                    ORDER BY ordinal, id
                    """,
                    (batch_id,),
                ).fetchall()
                for run in runs:
                    run_id = str(run["id"])
                    state = str(run["state"])
                    if state == "queued":
                        cursor = conn.execute(
                            """
                            UPDATE agent_subagent_runs
                            SET state = 'aborted', error = ?,
                                updated_at_ms = ?, completed_at_ms = ?
                            WHERE id = ? AND state = 'queued'
                            """,
                            (
                                normalized_reason,
                                now,
                                now,
                                run_id,
                            ),
                        )
                        if cursor.rowcount == 1:
                            affected_run_ids.append(run_id)
                            self._append_event_conn(
                                conn,
                                run_id=run_id,
                                event_type="aborted",
                                payload={
                                    "reason": "parent_lifecycle_cancelled",
                                    "requestId": normalized_request_id,
                                    "scopeKind": scope_kind,
                                    "scopeId": scope_id,
                                },
                                created_at_ms=now,
                            )
                    elif state == "running":
                        affected_run_ids.append(run_id)
                        active_run_ids.append(run_id)
                self._refresh_batch_conn(
                    conn,
                    batch_id,
                    updated_at_ms=now,
                )
        if sync_artifacts:
            for run_id in affected_run_ids:
                self._sync_run_artifact(run_id)
        return {
            "requestId": normalized_request_id,
            "batchIds": batch_ids,
            "activeRunIds": active_run_ids,
            "excludedRoomBoundBatchIds": [
                str(row["id"])
                for row in excluded_rows
            ],
        }

    def claim_control(
        self,
        *,
        run_id: str,
        parent_session_id: str,
        client_action_id: str,
        action: str,
        payload: Mapping[str, object],
        created_at_ms: int | None = None,
    ) -> tuple[dict[str, object], bool]:
        """Claim one parent control command exactly once.

        A repeated clientActionId is a replay only when the action and canonical
        payload are byte-for-byte equivalent. This prevents a retried HTTP
        request from steering or restarting a child twice.
        """

        if action not in {"steer", "retry", "resume", "abort", "reply"}:
            raise ValueError("unsupported delegated control action")
        action_id = _bounded_text(client_action_id, maximum=128, required=True)
        normalized = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = _timestamp(created_at_ms)
        control_id = f"subagent-control:{uuid.uuid4()}"
        with self._connect() as conn:
            owner = conn.execute(
                """
                SELECT b.parent_session_id
                FROM agent_subagent_runs r
                JOIN agent_subagent_batches b ON b.id = r.batch_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if owner is None:
                raise KeyError(run_id)
            if str(owner["parent_session_id"]) != parent_session_id:
                raise ValueError("delegated run does not belong to this session")
            try:
                conn.execute(
                    """
                    INSERT INTO agent_subagent_controls(
                        id, run_id, parent_session_id, client_action_id, action,
                        payload_sha256, payload_json, state, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                    """,
                    (
                        control_id,
                        run_id,
                        parent_session_id,
                        action_id,
                        action,
                        payload_sha256,
                        normalized,
                        now,
                        now,
                    ),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = conn.execute(
                """
                SELECT * FROM agent_subagent_controls
                WHERE run_id = ? AND client_action_id = ?
                """,
                (run_id, action_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("delegated control claim was not persisted")
            if (
                str(row["action"]) != action
                or str(row["payload_sha256"]) != payload_sha256
            ):
                raise ValueError("clientActionId was already used for a different action")
        return _control_payload(row), created

    def finish_control(
        self,
        control_id: str,
        *,
        result: Mapping[str, object] | None = None,
        error: str = "",
        completed_at_ms: int | None = None,
    ) -> dict[str, object]:
        now = _timestamp(completed_at_ms)
        state = "failed" if error else "completed"
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_subagent_controls
                SET state = ?, result_json = ?, error = ?, updated_at_ms = ?,
                    completed_at_ms = ?
                WHERE id = ? AND state = 'accepted'
                """,
                (
                    state,
                    json.dumps(
                        dict(result or {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    _bounded_text(error, maximum=500),
                    now,
                    now,
                    control_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_subagent_controls WHERE id = ?",
                (control_id,),
            ).fetchone()
            if row is None:
                raise KeyError(control_id)
            if cursor.rowcount == 0 and str(row["state"]) == "accepted":
                raise RuntimeError("delegated control could not be completed")
        return _control_payload(row)

    def list_controls(self, run_id: str, *, limit: int = 50) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_subagent_controls
                WHERE run_id = ? ORDER BY created_at_ms DESC LIMIT ?
                """,
                (run_id, max(1, min(int(limit), 100))),
            ).fetchall()
        return [_control_payload(row) for row in rows]

    def upsert_inbox(
        self,
        *,
        run_id: str,
        parent_session_id: str,
        child_session_id: str,
        request_id: str,
        kind: str,
        title: str,
        message: str,
        request_payload: Mapping[str, object] | None = None,
        turn_id: str = "",
        created_at_ms: int | None = None,
    ) -> dict[str, object]:
        if kind not in {"need_decision", "interview", "progress"}:
            raise ValueError("unsupported delegated inbox kind")
        now = _timestamp(created_at_ms)
        bounded_request_id = _bounded_text(request_id, maximum=180, required=True)
        inbox_id = f"subagent-inbox:{uuid.uuid4()}"
        initial_status = "observed" if kind == "progress" else "pending"
        envelope_json = json.dumps(
            {"request": dict(request_payload or {})},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_subagent_inbox(
                    id, run_id, parent_session_id, child_session_id, turn_id,
                    request_id, kind, title, message, status,
                    response_json, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, request_id) DO UPDATE SET
                    title = excluded.title,
                    message = excluded.message,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    inbox_id,
                    run_id,
                    parent_session_id,
                    child_session_id,
                    _bounded_text(turn_id, maximum=180),
                    bounded_request_id,
                    kind,
                    _bounded_text(title, maximum=160),
                    _bounded_text(message, maximum=500),
                    initial_status,
                    envelope_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM agent_subagent_inbox
                WHERE run_id = ? AND request_id = ?
                """,
                (run_id, bounded_request_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("delegated inbox item was not persisted")
        return _inbox_payload(row)

    def list_inbox(self, run_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_subagent_inbox
                WHERE run_id = ? ORDER BY created_at_ms DESC LIMIT ?
                """,
                (run_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [_inbox_payload(row) for row in rows]

    def reply_inbox(
        self,
        *,
        run_id: str,
        inbox_id: str,
        response: Mapping[str, object],
        resolved_at_ms: int | None = None,
    ) -> dict[str, object]:
        now = _timestamp(resolved_at_ms)
        with self._connect() as conn:
            previous = conn.execute(
                "SELECT * FROM agent_subagent_inbox WHERE id = ? AND run_id = ?",
                (inbox_id, run_id),
            ).fetchone()
            previous_envelope = (
                _json_mapping(previous["response_json"]) if previous is not None else {}
            )
            response_json = json.dumps(
                {
                    "request": dict(previous_envelope.get("request") or {})
                    if isinstance(previous_envelope.get("request"), Mapping)
                    else {},
                    "response": dict(response),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            cursor = conn.execute(
                """
                UPDATE agent_subagent_inbox
                SET status = 'replied', response_json = ?, updated_at_ms = ?,
                    resolved_at_ms = ?
                WHERE id = ? AND run_id = ? AND status = 'pending'
                """,
                (
                    response_json,
                    now,
                    now,
                    inbox_id,
                    run_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_subagent_inbox WHERE id = ? AND run_id = ?",
                (inbox_id, run_id),
            ).fetchone()
        if row is None:
            raise KeyError(inbox_id)
        if cursor.rowcount != 1:
            raise ValueError("delegated inbox item is no longer pending")
        return _inbox_payload(row)

    def reopen_run(self, run_id: str, *, updated_at_ms: int | None = None) -> dict[str, object]:
        now = _timestamp(updated_at_ms)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT batch_id, state, output_schema_json,
                       structured_output_tool_call_id, structured_output_error
                FROM agent_subagent_runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            output_schema, schema_error = _stored_output_schema(row["output_schema_json"])
            contract_invalid = (
                str(row["state"]) == "completed"
                and (output_schema is not None or bool(schema_error))
                and not str(row["structured_output_tool_call_id"] or "")
                and bool(str(row["structured_output_error"] or "").strip())
            )
            if (
                str(row["state"]) not in {"failed", "aborted", "timed_out"}
                and not contract_invalid
            ):
                raise ValueError("only a stopped delegated run can resume")
            conn.execute(
                """
                UPDATE agent_subagent_runs
                SET state = 'queued', result_json = '{}', error = '',
                    structured_output_error = '',
                    started_at_ms = NULL, completed_at_ms = NULL,
                    result_context_scheduled_at_ms = NULL, updated_at_ms = ?
                WHERE id = ?
                """,
                (now, run_id),
            )
            conn.execute(
                """
                UPDATE agent_subagent_batches
                SET abort_requested = 0, state = 'queued', completed_at_ms = NULL,
                    updated_at_ms = ?
                WHERE id = ?
                """,
                (now, str(row["batch_id"])),
            )
            self._append_event_conn(
                conn,
                run_id=run_id,
                event_type="progress",
                payload={"summary": "Parent resumed retained child session"},
                created_at_ms=now,
            )
        self._sync_run_artifact(run_id)
        return self.get_run(run_id)

    def latest_resume_message(self, run_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM agent_subagent_controls
                WHERE run_id = ? AND action = 'resume' AND state = 'completed'
                ORDER BY completed_at_ms DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return ""
        payload = _json_mapping(row["payload_json"])
        return _bounded_text(
            payload.get("message")
            or (
                "继续之前中断的任务。已完成节点不可重跑；先核对已有进度与原 Tool "
                "回执。只读操作可以继续；写文件、命令或外部操作若回执不能证明结果，"
                "停止并报告 blocker，不得盲目重放。"
            ),
            maximum=4_000,
        )

    def list_events(self, run_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, sequence, event_type, created_at_ms, payload_json
                FROM agent_subagent_events
                WHERE run_id = ? ORDER BY sequence DESC LIMIT ?
                """,
                (run_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [
            {
                "eventId": str(row["event_id"]),
                "sequence": int(row["sequence"]),
                "eventType": str(row["event_type"]),
                "createdAtMs": int(row["created_at_ms"]),
                "payload": _json_mapping(row["payload_json"]),
            }
            for row in reversed(rows)
        ]

    def append_budget_event(self, run_id: str, reason: str, *, phase: str) -> None:
        if phase not in {"soft", "hard"}:
            raise ValueError("delegated budget phase must be soft or hard")
        now = _timestamp(None)
        with self._connect() as conn:
            self._append_event_conn(
                conn,
                run_id=run_id,
                event_type="budget_exceeded",
                payload={
                    "phase": phase,
                    "reason": _bounded_text(reason, maximum=240),
                },
                created_at_ms=now,
            )
        self._sync_run_artifact(run_id)
        self.checkpoint_supervision(
            run_id,
            phase=phase,
            reason=reason,
            requested_at_ms=now if phase == "hard" else None,
        )

    def checkpoint_runtime_event(
        self,
        run_id: str,
        event: AgentEventEnvelope,
        checkpoint: Mapping[str, object],
    ) -> None:
        if self.artifacts is None:
            return
        self.artifacts.append_records(
            owner_kind="subagent_run",
            owner_id=run_id,
            records=[
                {
                    "recordId": f"runtime:{event.event_id}",
                    "eventType": event.event_type,
                    "createdAtMs": event.created_at_ms,
                    "payload": _safe_runtime_artifact_payload(event),
                }
            ],
        )
        self.artifacts.checkpoint(
            owner_kind="subagent_run",
            owner_id=run_id,
            runtime_checkpoint=checkpoint,
            updated_at_ms=event.created_at_ms,
        )

    def checkpoint_supervision(
        self,
        run_id: str,
        *,
        phase: str,
        reason: str,
        requested_at_ms: int | None = None,
        grace_ms: int | None = None,
    ) -> None:
        if self.artifacts is None:
            return
        now = _timestamp(None)
        self.artifacts.append_records(
            owner_kind="subagent_run",
            owner_id=run_id,
            records=[
                {
                    "recordId": f"supervision:{phase}:{now}",
                    "eventType": f"supervision_{phase}",
                    "createdAtMs": now,
                    "payload": {
                        "reason": _bounded_text(reason, maximum=240),
                        "graceMs": max(0, int(grace_ms or 0)),
                    },
                }
            ],
        )
        self.artifacts.checkpoint(
            owner_kind="subagent_run",
            owner_id=run_id,
            supervision={
                "phase": phase,
                "reason": _bounded_text(reason, maximum=240),
                "requestedAtMs": requested_at_ms,
                "graceMs": max(0, int(grace_ms or 0)),
            },
            updated_at_ms=now,
        )

    def reconcile_interrupted_runs(self) -> list[str]:
        """Recover safe queued work and project terminal artifact checkpoints."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, state FROM agent_subagent_runs WHERE state IN ('queued', 'running')"
            ).fetchall()
        relaunch: list[str] = []
        for row in rows:
            run_id = str(row["id"])
            state = str(row["state"])
            self._sync_run_artifact(run_id)
            if state == "queued":
                relaunch.append(run_id)
                continue

            snapshot = (
                self.artifacts.snapshot(owner_kind="subagent_run", owner_id=run_id)
                if self.artifacts is not None
                else None
            )
            checkpoint = (
                snapshot.get("runtimeCheckpoint")
                if isinstance(snapshot, Mapping)
                and isinstance(snapshot.get("runtimeCheckpoint"), Mapping)
                else {}
            )
            terminal_state = str(checkpoint.get("terminalState") or "")
            usage = checkpoint.get("usage") if isinstance(checkpoint.get("usage"), Mapping) else {}
            last_message = (
                dict(checkpoint["lastMessage"])
                if isinstance(checkpoint.get("lastMessage"), Mapping)
                else {}
            )
            if terminal_state == "completed" and last_message:
                run = self.get_run(run_id)
                result = {
                    "summary": _message_summary(
                        last_message,
                        int(run["budget"]["maxOutputChars"]),
                    ),
                    "message": last_message,
                    "childSessionId": run["childSessionId"],
                    "templateId": run["templateId"],
                    "deliveryStatus": "returned",
                    "verificationStatus": "unverified",
                    "authority": "evidence_only",
                    "recovered": True,
                }
                final_state = "completed"
                error = ""
            elif terminal_state in {"failed", "aborted", "timed_out"}:
                result = {}
                final_state = terminal_state
                error = _bounded_text(
                    checkpoint.get("error") or "Delegated runtime ended before projection",
                    maximum=500,
                )
            else:
                result = {}
                final_state = "failed"
                error = "Sidecar restarted before the delegated runtime reached a durable terminal checkpoint"

            self.finish_run(
                run_id,
                state=final_state,
                result=result,
                error=error,
                turn_count=max(0, int(usage.get("turnCount") or 0)),
                tool_count=max(0, int(usage.get("toolCount") or 0)),
                total_tokens=max(0, int(usage.get("totalTokens") or 0)),
            )
            if self.artifacts is not None:
                now = _timestamp(None)
                self.artifacts.append_records(
                    owner_kind="subagent_run",
                    owner_id=run_id,
                    records=[
                        {
                            "recordId": f"reconcile:{now}",
                            "eventType": "reconciled",
                            "createdAtMs": now,
                            "payload": {
                                "fromState": "running",
                                "toState": final_state,
                                "terminalCheckpoint": bool(terminal_state),
                            },
                        }
                    ],
                )
        return relaunch

    def _decorate_run(self, run: Mapping[str, object]) -> dict[str, object]:
        payload = dict(run)
        if self.artifacts is None:
            return payload
        try:
            payload["artifact"] = self.artifacts.reference(
                owner_kind="subagent_run",
                owner_id=str(payload["id"]),
            )
        except KeyError:
            # Explicit artifact hydration is allowed to materialize the
            # lifecycle receipt if the background worker has not reached its
            # first checkpoint yet. Status uses hydrate_artifacts=False and
            # never enters this path.
            self._sync_run_artifact(str(payload["id"]))
            payload["artifact"] = self.artifacts.reference(
                owner_kind="subagent_run",
                owner_id=str(payload["id"]),
            )
        snapshot = self.artifacts.snapshot(
            owner_kind="subagent_run",
            owner_id=str(payload["id"]),
        )
        supervision = (
            snapshot.get("supervision")
            if isinstance(snapshot, Mapping)
            and isinstance(snapshot.get("supervision"), Mapping)
            else {}
        )
        payload["supervision"] = {
            "phase": str(supervision.get("phase") or "none"),
            "reason": str(supervision.get("reason") or ""),
            "requestedAtMs": supervision.get("requestedAtMs"),
            "graceMs": max(0, int(supervision.get("graceMs") or 0)),
        }
        return payload

    def _sync_all_artifacts(self) -> None:
        if self.artifacts is None:
            return
        with self._connect() as conn:
            run_ids = [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT r.id
                    FROM agent_subagent_runs r
                    LEFT JOIN agent_artifacts a
                      ON a.owner_kind = 'subagent_run'
                     AND a.owner_id = r.id
                     AND a.artifact_kind = 'lifecycle'
                    WHERE a.id IS NULL
                       OR a.updated_at_ms < r.updated_at_ms
                       OR r.state IN ('queued', 'running')
                    ORDER BY r.created_at_ms
                    """
                ).fetchall()
            ]
        for run_id in run_ids:
            self._sync_run_artifact(run_id)

    def _sync_run_artifact(self, run_id: str) -> None:
        if self.artifacts is None:
            return
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_subagent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            event_rows = conn.execute(
                "SELECT * FROM agent_subagent_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        projection = _run_payload(row)
        records = [
            {
                "recordId": str(event["event_id"]),
                "sequence": int(event["sequence"]),
                "eventType": str(event["event_type"]),
                "createdAtMs": int(event["created_at_ms"]),
                "payload": _json_mapping(event["payload_json"]),
            }
            for event in event_rows
        ]
        self.artifacts.append_records(
            owner_kind="subagent_run",
            owner_id=run_id,
            records=records,
        )
        snapshot = self.artifacts.snapshot(
            owner_kind="subagent_run",
            owner_id=run_id,
        )
        previous_projection = (
            snapshot.get("projection")
            if isinstance(snapshot, Mapping)
            and isinstance(snapshot.get("projection"), Mapping)
            else None
        )
        if previous_projection != projection:
            self.artifacts.checkpoint(
                owner_kind="subagent_run",
                owner_id=run_id,
                projection=projection,
                updated_at_ms=int(projection["updatedAtMs"]),
            )

    def owns_session(self, session_id: str) -> bool:
        return self.run_for_child_session(session_id) is not None

    def _refresh_batch_conn(
        self,
        conn: sqlite3.Connection,
        batch_id: str,
        *,
        updated_at_ms: int,
    ) -> None:
        states = [
            str(row[0])
            for row in conn.execute(
                "SELECT state FROM agent_subagent_runs WHERE batch_id = ? ORDER BY ordinal",
                (batch_id,),
            ).fetchall()
        ]
        if any(state == "running" for state in states):
            state = "running"
        elif any(state == "queued" for state in states):
            state = "queued"
        elif states and all(item == "completed" for item in states):
            state = "completed"
        elif any(item == "timed_out" for item in states):
            state = "timed_out"
        elif any(item == "failed" for item in states):
            state = "failed"
        else:
            state = "aborted"
        completed_at = updated_at_ms if state in _TERMINAL_STATES else None
        conn.execute(
            """
            UPDATE agent_subagent_batches
            SET state = ?, updated_at_ms = ?, completed_at_ms = ? WHERE id = ?
            """,
            (state, updated_at_ms, completed_at, batch_id),
        )

    def _append_event_conn(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, object],
        created_at_ms: int,
    ) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_subagent_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        sequence = int(row[0] if row else 1)
        conn.execute(
            """
            INSERT INTO agent_subagent_events(
                event_id, run_id, sequence, event_type, created_at_ms, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"{run_id}:{sequence}",
                run_id,
                sequence,
                event_type,
                created_at_ms,
                json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
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


@dataclass
class _ActiveDelegatedRun:
    runtime: AgentRuntimeDriver
    owns_runtime: bool
    child_session_id: str
    terminal: threading.Event
    forced: threading.Event
    lock: threading.RLock
    cancellation_state: str = ""
    cancellation_reason: str = ""


class AgentDelegationCoordinator:
    """Product-owned adapter for fixed-catalog Pi child sessions."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        runtime_config: PiRuntimeConfig,
        sessions: AgentSessionStore,
        events: AgentEventHub,
        context_runtime: AgentContextRuntime,
        media_resolver: Callable[[str, str, str], str] | None = None,
        runtime_factory: Callable[..., AgentRuntimeDriver] | None = None,
        runtime_driver_factory: RuntimeDriverFactory | None = None,
        runtime_provider: Callable[[], AgentRuntimeDriver] | None = None,
        tool_gateway_token: str = "",
        tool_gateway_url: str = "",
        tool_manifest_provider: ToolManifestProvider | None = None,
        compaction_observer: CompactionObserver | None = None,
        prompt_settings_provider: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        scenario_policy_provider: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        artifact_root: str | Path | None = None,
        cancellation_grace_ms: int = _DEFAULT_CANCELLATION_GRACE_MS,
        subagent_session_retention_ms: int | None = None,
        subagent_session_gc_interval_ms: int | None = None,
        room_context_provider: Callable[
            [str], Mapping[str, object] | None
        ] | None = None,
        model_route_provider: Callable[
            [str], Mapping[str, object] | None
        ] | None = None,
        startup_recovery: bool = True,
    ) -> None:
        self.artifacts = AgentArtifactStore(db_path, root=artifact_root)
        self.store = AgentDelegationStore(db_path, artifacts=self.artifacts)
        self.store.initialize()
        self.runtime_config = runtime_config
        self.context_runtime = context_runtime
        self.sessions = sessions
        self.events = events
        self.media_resolver = media_resolver
        self._legacy_runtime_factory = runtime_factory
        self._runtime_driver_factory = runtime_driver_factory or PiRuntimeDriverFactory(
            runtime_config
        )
        self._runtime_provider = runtime_provider
        self._tool_gateway_token = str(
            tool_gateway_token or runtime_config.tool_gateway_token
        )
        self._tool_gateway_url = str(
            tool_gateway_url or runtime_config.tool_gateway_url
        ).strip()
        if not self._tool_gateway_url:
            raise ValueError("delegated Tool gateway URL must not be empty")
        self._tool_manifest_provider = tool_manifest_provider
        self._compaction_observer = compaction_observer
        self._prompt_settings_provider = prompt_settings_provider
        self._scenario_policy_provider = scenario_policy_provider
        self._room_context_provider = room_context_provider
        self._model_route_provider = model_route_provider
        self._cancellation_grace_ms = max(10, min(int(cancellation_grace_ms), 30_000))
        self._subagent_session_retention_ms = max(
            0,
            min(
                int(
                    subagent_session_retention_ms
                    if subagent_session_retention_ms is not None
                    else _subagent_session_retention_from_environment()
                ),
                30 * 24 * 60 * 60 * 1_000,
            ),
        )
        self._subagent_session_gc_interval_ms = max(
            0,
            min(
                int(
                    subagent_session_gc_interval_ms
                    if subagent_session_gc_interval_ms is not None
                    else _subagent_session_gc_interval_from_environment()
                ),
                24 * 60 * 60 * 1_000,
            ),
        )
        self._lock = threading.RLock()
        self._retirement_lock = threading.RLock()
        self._last_session_gc_monotonic = 0.0
        self._active_runs: dict[str, _ActiveDelegatedRun] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._reserved_run_count = 0
        self._closed = False
        self._startup_recovery_lock = threading.RLock()
        self._startup_recovery_complete = False
        if startup_recovery:
            self.reconcile_startup()

    def reconcile_startup(self) -> None:
        """Recover durable delegated runs and retention once per process."""

        with self._startup_recovery_lock:
            if self._startup_recovery_complete:
                return
            recoverable = self.store.reconcile_interrupted_runs()
            self._schedule_pending_result_contexts()
            self.collect_expired_sessions(force=True)
            if self.runtime_config.enabled:
                for run_id in recoverable:
                    self._start_run_thread(run_id)
            self._startup_recovery_complete = True

    def catalog(self) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.agent-template-list.v1",
            "ok": True,
            "maxParallel": _MAX_PARALLEL_RUNS,
            "maxDepth": 2,
            "items": agent_template_catalog(),
        }

    @contextmanager
    def _capacity_reservation(self, count: int):
        count = max(0, int(count))
        with self._lock:
            if self._closed:
                raise ValueError("delegation runtime is closed")
            if (
                self.store.active_run_count()
                + self._reserved_run_count
                + count
                > _MAX_PARALLEL_RUNS
            ):
                raise ValueError("at most two delegated tasks may run at once")
            self._reserved_run_count += count
        try:
            yield
        finally:
            with self._lock:
                self._reserved_run_count = max(
                    0,
                    self._reserved_run_count - count,
                )

    def delegate(self, parent_session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        parent = self.sessions.get(parent_session_id)
        self.sessions.require_goal_execution(parent_session_id)
        parent_run = self.store.run_for_child_session(parent_session_id)
        if parent.get("status") == "archived" and parent_run is None:
            raise ValueError("archived sessions cannot delegate tasks")
        wait = payload.get("wait") is True
        tasks = _delegation_tasks(payload)
        retry_lineage = (
            dict(payload.get("_retryLineage"))
            if isinstance(payload.get("_retryLineage"), Mapping)
            else {}
        )
        if retry_lineage and len(tasks) != 1:
            raise ValueError("a retry lineage may contain exactly one delegated task")
        todo_task, todo_phase = self._todo_task_link(
            parent_session_id,
            payload,
            parent_run=parent_run,
        )
        context_mode = str(payload.get("contextMode") or "fresh").strip()
        if context_mode not in {"fresh", "fork"}:
            raise ValueError("contextMode must be fresh or fork")
        parent_runtime_binding = self.sessions.runtime_binding(parent_session_id)
        runtime_context = payload.get("_runtimeContext")
        control_fork_entry_id = _bounded_text(
            payload.get("forkEntryId"),
            maximum=240,
        )
        if context_mode == "fork" and isinstance(runtime_context, Mapping):
            native_forks = _validated_native_fork_sessions(
                runtime_context,
                parent_runtime_binding=parent_runtime_binding,
                session_dir=self._runtime_driver_factory.session_root,
                agent_dir=self._runtime_driver_factory.working_root,
                expected_count=len(tasks),
            )
        elif context_mode == "fork" and control_fork_entry_id:
            if self._runtime_provider is None:
                raise ValueError("fork context requires the active Pi runtime")
            native_forks = []
        elif context_mode == "fork":
            # Keep Tool-originated forks fail-closed: only Pi may provide the
            # native transcript receipt. The Control Center instead supplies
            # an entry anchor and asks the product-owned Runtime to create it.
            raise ValueError("fork context must be created by the active Pi runtime")
        else:
            native_forks = []
        depth = int(parent_run.get("depth") or 0) + 1 if parent_run else 1
        parent_run_id = str(parent_run.get("id") or "") if parent_run else ""
        if parent_run is not None:
            parent_batch = self.store.get_batch(
                str(parent_run["batchId"]),
                hydrate_artifacts=False,
            )
            causal_metadata = dict(parent_batch["causalMetadata"])
        else:
            todo = self.sessions.agent_todo(parent_session_id)
            goal = self.sessions.agent_goal(parent_session_id)
            causal_metadata = {
                "todoId": str(todo["id"]),
                "todoRevision": int(todo["revision"]),
                "goalId": (
                    str(goal["goalId"])
                    if bool(goal["configured"])
                    and str(goal["status"]) in {"active", "paused"}
                    else ""
                ),
                "goalRevision": (
                    int(goal["revision"])
                    if bool(goal["configured"])
                    and str(goal["status"]) in {"active", "paused"}
                    else 0
                ),
                "roomBound": False,
                "roomId": "",
                "rootId": "",
                "taskId": "",
                "dispatchId": "",
            }
        room_context: Mapping[str, object] | None = None
        room_permission_policy = None
        if self._room_context_provider is not None:
            # Private children are not Room participants. Resolve the policy
            # through their owning participant instead of treating a missing
            # child membership as a return to standalone template defaults.
            policy_session_id = (
                self._delegation_root_session_id(parent_session_id)
                if parent_run is not None
                else parent_session_id
            )
            candidate_room_context = self._room_context_provider(policy_session_id)
            if isinstance(candidate_room_context, Mapping):
                room_context = candidate_room_context
                room_permission_policy = _room_permission_policy(
                    room_context.get("permissionPolicy")
                )
                if parent_run is None:
                    causal_metadata = _delegation_causal_metadata(
                        {**causal_metadata, **room_context}
                    )
                # A nested child keeps its original dispatch receipt. Current
                # Room membership/policy must not erase or rotate that identity.
        effective_room_execution_mode = _effective_room_execution_mode(
            room_permission_policy
        )
        if parent_run is not None and effective_room_execution_mode is not None:
            parent_mode = str(parent.get("executionMode") or READ_ONLY_EXECUTION_MODE)
            if (
                _ROOM_PERMISSION_MODE_RANK[parent_mode]
                < _ROOM_PERMISSION_MODE_RANK[effective_room_execution_mode]
            ):
                effective_room_execution_mode = parent_mode
        if depth > 2:
            raise ValueError("subagent maximum depth is 2")

        templates: list[AgentTemplate] = []
        for task in tasks:
            template = agent_template(str(task["agent"]), str(task.get("version") or "1"))
            if context_mode not in template.context_modes:
                raise ValueError(
                    f"agent template {template.template_id} does not support {context_mode} context"
                )
            requested_access = str(task.get("access") or "inherit")
            if (
                requested_access != "inherit"
                and requested_access not in template.allowed_access
            ):
                raise ValueError(
                    f"agent template {template.template_id} does not allow {requested_access} access"
                )
            templates.append(template)

        created_sessions: list[dict[str, object]] = []
        prepared_model_routes: list[dict[str, str]] = []
        prepared_authority: list[str] = []
        prepared_files: list[Path] = []
        control_fork_runtime: AgentRuntimeDriver | None = None
        resolved_control_fork_entry_id = control_fork_entry_id
        with self._capacity_reservation(len(tasks)):
            try:
                for ordinal, (task, template) in enumerate(zip(tasks, templates, strict=True)):
                    parent_profile = str(parent.get("toolProfileVersion") or "control-center-v1")
                    parent_execution_mode = str(parent.get("executionMode") or "").strip()
                    requested_access = str(task.get("access") or "inherit")
                    access, policy_execution_mode = _room_policy_access(
                        requested_access,
                        effective_execution_mode=effective_room_execution_mode,
                    )
                    writable = access == "write"
                    if writable and str(parent.get("mode") or "") != "coordinator":
                        raise ValueError("writable delegated tasks require a coordinator parent")
                    if writable and (
                        parent_profile == READONLY_TOOL_PROFILE
                        or parent_execution_mode == READ_ONLY_EXECUTION_MODE
                    ):
                        raise ValueError("the parent Session cannot grant write access")
                    room_unrestricted = _room_policy_is_unrestricted(
                        effective_room_execution_mode
                    )
                    unrestricted_parent = (
                        unrestricted_workspace_policy_active(parent)
                        or room_unrestricted
                    )
                    parent_roots = [str(value) for value in parent.get("workspaceRoots") or []]
                    requested_roots = [str(value) for value in task.get("workspaceRoots") or []]
                    # Workspace roots describe where a delegated Session may
                    # resolve paths, not whether it may mutate them. Read-only
                    # templates still need the parent's roots for browse,
                    # search and read tools; their assistant mode, read-only
                    # execution policy and tool profile remain the write gate.
                    if requested_roots:
                        requested_roots = list(existing_workspace_roots(requested_roots))
                        normalized_requested_roots = tuple(
                            Path(value).expanduser().resolve(strict=False)
                            for value in requested_roots
                        )
                        normalized_parent_roots = tuple(
                            Path(value).expanduser().resolve(strict=False)
                            for value in parent_roots
                        )
                        if unrestricted_parent:
                            authorized = normalized_parent_roots + (Path("/"),)
                            inherited = all(
                                any(
                                    root == candidate or root in candidate.parents
                                    for root in authorized
                                )
                                for candidate in normalized_requested_roots
                            )
                        else:
                            inherited = all(
                                candidate in normalized_parent_roots
                                for candidate in normalized_requested_roots
                            )
                        if not inherited:
                            raise ValueError(
                                "delegated workspaceRoots must be inherited from the parent Session"
                            )
                    child_roots = requested_roots or parent_roots
                    if writable and room_unrestricted:
                        child_roots = list(system_wide_workspace_roots(child_roots))
                    if writable and not child_roots:
                        raise ValueError("writable delegated tasks require an authorized workspace")
                    child_profile = (
                        READONLY_TOOL_PROFILE
                        if access == "read_only"
                        or parent_profile == READONLY_TOOL_PROFILE
                        or parent_execution_mode == READ_ONLY_EXECUTION_MODE
                        else DANGEROUS_AUTO_APPROVE_TOOL_PROFILE
                        if writable and effective_room_execution_mode == FULL_TRUST_EXECUTION_MODE
                        else FULL_ACCESS_TOOL_PROFILE
                        if writable and effective_room_execution_mode == PER_ACTION_EXECUTION_MODE
                        else parent_profile
                        if writable and unrestricted_parent
                        else "subagent-worker-v1"
                        if writable
                        else template.tool_profile_version
                    )
                    child_mode = "coordinator" if writable else "assistant"
                    child_execution_mode = (
                        policy_execution_mode
                        if policy_execution_mode is not None and access == "write"
                        else READ_ONLY_EXECUTION_MODE
                        if policy_execution_mode is not None and access == "read_only"
                        else parent_execution_mode
                        if writable
                        else None
                    )
                    model_route_id = "toolAgent" if writable else "subagent"
                    configured_model_route = (
                        self._model_route_provider(model_route_id)
                        if self._model_route_provider is not None
                        else None
                    )
                    model_route = (
                        dict(configured_model_route)
                        if isinstance(configured_model_route, Mapping)
                        else {}
                    )
                    routed_model_profile = str(
                        model_route.get("modelProfile") or "inherit"
                    ).strip()
                    routed_thinking_level = str(
                        model_route.get("thinkingLevel") or "inherit"
                    ).strip()
                    explicit_model_profile = str(
                        task.get("modelProfile") or ""
                    ).strip()
                    explicit_thinking_level = (
                        str(task.get("thinkingLevel") or "").strip()
                        if task.get("thinkingLevel") is not None
                        else ""
                    )
                    selected_model_profile = str(
                        (
                            routed_model_profile
                            if routed_model_profile != "inherit"
                            else explicit_model_profile
                        )
                        or parent.get("modelProfile")
                        or "pi/default"
                    )
                    selected_thinking_level = str(
                        (
                            routed_thinking_level
                            if routed_thinking_level != "inherit"
                            else explicit_thinking_level
                        )
                        or parent.get("thinkingLevel")
                        or ""
                    )
                    child = self.sessions.create(
                        title=f"{template.display_name} · {_bounded_text(task['task'], maximum=72)}",
                        mode=child_mode,
                        role_id=str(parent.get("roleId") or "companion-present-v1"),
                        role_version=str(parent.get("roleVersion") or "1"),
                        role_book_revision_id=str(
                            parent.get("roleBookRevisionId") or ""
                        ),
                        model_profile=selected_model_profile,
                        thinking_level=selected_thinking_level,
                        tool_profile_version=child_profile,
                        execution_mode=child_execution_mode,
                        project_context_enabled=(
                            True
                            if writable and unrestricted_parent
                            else bool(
                                task.get("projectContextEnabled")
                                if task.get("projectContextEnabled") is not None
                                else parent.get("projectContextEnabled", False)
                            )
                        ),
                        pi_skills_enabled=(
                            True
                            if writable and unrestricted_parent
                            else bool(
                                task.get("piSkillsEnabled")
                                if task.get("piSkillsEnabled") is not None
                                else parent.get("piSkillsEnabled", False)
                            )
                        ),
                        codex_skills_enabled=(
                            True
                            if writable and unrestricted_parent
                            else bool(
                                task.get("codexSkillsEnabled")
                                if task.get("codexSkillsEnabled") is not None
                                else parent.get("codexSkillsEnabled", False)
                            )
                        ),
                        workspace_roots=child_roots,
                        session_kind="subagent_runtime",
                    )
                    # Register immediately so a later policy/fork failure can
                    # always remove the half-prepared internal Session.
                    created_sessions.append(child)
                    prepared_authority.append(access)
                    prepared_model_routes.append(
                        {
                            "modelRoute": model_route_id,
                            "modelRouteSource": (
                                "configured"
                                if routed_model_profile != "inherit"
                                else "explicit"
                                if explicit_model_profile
                                else "inherited"
                            ),
                        }
                    )
                    requested_tools = (
                        list(
                            dict.fromkeys(
                                str(value)
                                for value in task.get("allowedTools") or []
                            )
                        )
                        if "allowedTools" in task
                        else None
                    )
                    if str(parent.get("toolAllowlistMode") or "profile") == "explicit":
                        parent_tool_order = [
                            str(value) for value in parent.get("allowedTools") or []
                        ]
                        parent_tools = set(parent_tool_order)
                        requested_tools = (
                            parent_tool_order
                            if requested_tools is None
                            else [
                                tool for tool in requested_tools if tool in parent_tools
                            ]
                        )
                    if (
                        requested_tools is not None
                        and not (writable and unrestricted_parent)
                    ):
                        child = self.sessions.set_runtime_policy(
                            str(child["id"]),
                            mode=child_mode,
                            tool_profile_version=child_profile,
                            execution_mode=child_execution_mode,
                            grant_workspace_scope=(
                                writable
                                and bool(parent.get("workspaceScopeGranted"))
                            ),
                            allowed_tools=requested_tools,
                            pi_skills_enabled=bool(child.get("piSkillsEnabled")),
                            codex_skills_enabled=bool(child.get("codexSkillsEnabled")),
                            workspace_roots=child_roots,
                        )
                    if context_mode == "fork":
                        if native_forks:
                            branch = native_forks[ordinal]
                            prepared_files.append(branch["path"])
                            child = self.sessions.bind_runtime_session(
                                str(child["id"]),
                                driver_id="managed-pi",
                                runtime_kind="pi_rpc",
                                external_session_id=str(branch["sessionId"]),
                                transcript_ref=str(branch["path"]),
                                branch_anchor=str(branch["parentLeafId"]),
                                binding_state="prepared",
                            )
                        else:
                            control_fork_runtime = control_fork_runtime or self._runtime_provider()  # type: ignore[misc]
                            if resolved_control_fork_entry_id == "latest":
                                candidates = control_fork_runtime.fork_candidates(
                                    parent_session_id
                                )
                                if not candidates:
                                    raise ValueError(
                                        "the parent Session has no Pi context that can be forked"
                                    )
                                resolved_control_fork_entry_id = str(
                                    candidates[-1].get("entryId") or ""
                                ).strip()
                            if not resolved_control_fork_entry_id:
                                raise ValueError("forkEntryId does not resolve to a Pi anchor")
                            control_fork_runtime.fork_session(
                                parent_session_id,
                                str(child["id"]),
                                entry_id=resolved_control_fork_entry_id,
                            )
                            child = self.sessions.get(str(child["id"]))
                            branch_file = str(child.get("sessionFile") or "").strip()
                            if branch_file:
                                prepared_files.append(
                                    Path(branch_file).expanduser().resolve(strict=False)
                                )
                    created_sessions[-1] = child

                run_specs = []
                for child, task, template, prepared_model_route, authority in zip(
                    created_sessions,
                    tasks,
                    templates,
                    prepared_model_routes,
                    prepared_authority,
                    strict=True,
                ):
                    budget = _delegation_budget(template.budget, task.get("budget"))
                    try:
                        runtime_manifests = (
                            list(self._tool_manifest_provider(child))
                            if self._tool_manifest_provider is not None
                            else []
                        )
                    except Exception:
                        runtime_manifests = []
                    tool_names = [
                        str(item.get("name") or "").strip()
                        for item in runtime_manifests
                        if isinstance(item, Mapping)
                        and str(item.get("name") or "").strip()
                    ]
                    if "outputSchema" in task and "structured_output" not in tool_names:
                        tool_names.append("structured_output")
                    workspace_access = authority
                    launch_digest = {
                        "modelProfile": str(child.get("modelProfile") or "pi/default"),
                        "thinkingLevel": str(child.get("thinkingLevel") or ""),
                        **prepared_model_route,
                        "toolProfileVersion": str(
                            child.get("toolProfileVersion") or "subagent-readonly-v1"
                        ),
                        "toolAllowlistMode": str(
                            child.get("toolAllowlistMode") or "profile"
                        ),
                        "tools": tool_names,
                        "piSkillsEnabled": bool(child.get("piSkillsEnabled", False)),
                        "codexSkillsEnabled": bool(
                            child.get("codexSkillsEnabled", False)
                        ),
                        "workspaceAccess": workspace_access,
                        "workspaceRootCount": len(child.get("workspaceRoots") or []),
                    }
                    run_specs.append(
                        {
                            "childSessionId": child["id"],
                            "templateId": template.template_id,
                            "expectedOutput": task["expectedOutput"],
                            "acceptanceCriteria": task["acceptanceCriteria"],
                            "outputSchema": task.get("outputSchema", {}),
                            "templateVersion": template.version,
                            "task": task["task"],
                            "todoTask": todo_task,
                            "todoPhase": todo_phase,
                            "nodeId": retry_lineage.get("nodeId"),
                            "attemptId": f"subagent-attempt:{uuid.uuid4()}",
                            "attemptNumber": retry_lineage.get("attemptNumber", 1),
                            "predecessorAttemptId": retry_lineage.get(
                                "predecessorAttemptId",
                                "",
                            ),
                            "ownerRunId": retry_lineage.get("ownerRunId")
                            or parent_run_id
                            or f"session:{parent_session_id}",
                            "launchDigest": launch_digest,
                            "maxTurns": budget.max_turns,
                            "maxToolCalls": budget.max_tool_calls,
                            "maxTotalTokens": budget.max_total_tokens,
                            "maxDurationMs": budget.max_duration_ms,
                            "maxOutputChars": budget.max_output_chars,
                        }
                    )
                batch = self.store.create_batch(
                    parent_session_id=parent_session_id,
                    parent_run_id=parent_run_id,
                    context_mode=context_mode,
                    depth=depth,
                    max_depth=2,
                    result_delivery_mode="inline" if wait else "next_turn",
                    runs=run_specs,
                    causal_metadata=causal_metadata,
                )
            except Exception:
                for child in reversed(created_sessions):
                    if control_fork_runtime is not None:
                        close_session = getattr(control_fork_runtime, "close_session", None)
                        if callable(close_session):
                            try:
                                close_session(str(child["id"]))
                            except Exception:
                                pass
                    try:
                        self.sessions.delete(str(child["id"]))
                    except Exception:
                        pass
                for path in prepared_files:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                raise

            for run in batch["runs"]:
                self._start_run_thread(str(run["id"]))
        if wait:
            batch = self.wait(str(batch["id"]))
        return {
            "schemaVersion": "rag-ime.agent-delegation.v1",
            "ok": True,
            "accepted": True,
            # This acknowledges only that the bounded delegation request was
            # accepted for execution. It is not evidence that any child
            # result has satisfied the parent task's acceptance criteria.
            "acceptanceScope": "delegation_request",
            "waited": wait,
            "batch": batch,
        }

    def status(self, parent_session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        identifier = str(payload.get("runId") or payload.get("batchId") or "").strip()
        if not identifier:
            return {
                "schemaVersion": "rag-ime.agent-delegation-status.v1",
                "ok": True,
                "items": self.store.list_batches(
                    parent_session_id=parent_session_id,
                    limit=_bounded_int(payload.get("limit") or 20, minimum=1, maximum=100),
                    hydrate_artifacts=False,
                ),
                "peers": self._peer_runs(parent_session_id),
                "tree": self._run_tree(parent_session_id),
            }
        try:
            batch = self.store.get_batch(identifier, hydrate_artifacts=False)
        except KeyError:
            run = self.store.get_run(identifier, hydrate_artifacts=False)
            batch = self.store.get_batch(
                str(run["batchId"]),
                hydrate_artifacts=False,
            )
        self._assert_delegation_tree_access(parent_session_id, batch)
        return {
            "schemaVersion": "rag-ime.agent-delegation-status.v1",
            "ok": True,
            "batch": batch,
            "peers": self._peer_runs(parent_session_id),
            "tree": self._run_tree(parent_session_id),
        }

    def structured_output_manifest(
        self,
        child_session_id: str,
    ) -> dict[str, object] | None:
        """Expose a terminal, schema-bound tool only to its delegated child."""

        run = self.store.run_for_child_session(child_session_id)
        if run is None or str(run.get("state") or "") not in _ACTIVE_STATES:
            return None
        output_schema = run.get("outputSchema")
        if not _output_schema_requested(output_schema):
            return None
        return {
            "name": "structured_output",
            "description": (
                "Submit the delegated task's final value. The value must match the "
                "assigned JSON Schema; a valid submission ends this child turn."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["value"],
                "properties": {
                    "value": (
                        dict(output_schema)
                        if isinstance(output_schema, Mapping)
                        else output_schema
                    )
                },
            },
            "when": ["The delegated task is complete and its final value is ready."],
            "notFor": ["Progress updates", "prose-only final answers"],
            "input": "One value matching the delegated output contract.",
            "output": "A durable contract receipt that terminates this child turn.",
            "does": "Validates and stores exactly one final value for this attempt.",
            "risk": "R0",
            "alwaysAvailable": True,
        }

    def submit_structured_output(
        self,
        child_session_id: str,
        args: Mapping[str, object],
        *,
        tool_call_id: str,
    ) -> dict[str, object]:
        if set(args) != {"value"}:
            raise ValueError("structured_output requires exactly one value field")
        run = self.store.submit_structured_output(
            child_session_id=child_session_id,
            value=args["value"],
            tool_call_id=tool_call_id,
        )
        return {
            "schemaVersion": "rag-ime.agent-subagent-structured-output.v1",
            "summary": "结构化交付合同有效；本次子 Agent 已结束。",
            "runId": run["id"],
            "nodeId": run["nodeId"],
            "attemptId": run["attemptId"],
            "contractStatus": "valid",
            "structuredOutput": run.get("structuredOutput"),
            "terminate": True,
        }

    def call(self, caller_session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        """Deliver one direct message to another live subagent Session.

        Pi remains the only message runtime: this adapter only proves that the
        caller and target belong to the same bounded delegation tree, then
        injects an ordinary steer into the target Session. The target may call
        the sender back through the same operation.
        """

        self.sessions.require_goal_execution(caller_session_id)
        target_run_id = _bounded_text(
            payload.get("targetRunId"), maximum=240, required=True
        )
        message = _bounded_text(payload.get("message"), maximum=4_000, required=True)
        target_run = self.store.get_run(target_run_id)
        target_batch = self.store.get_batch(str(target_run["batchId"]))
        self._assert_delegation_tree_access(caller_session_id, target_batch)
        caller_run = self.store.run_for_child_session(caller_session_id)
        if caller_run is not None and str(caller_run.get("id") or "") == target_run_id:
            raise ValueError("a subagent cannot call itself")
        if str(target_run.get("state") or "") != "running":
            raise ValueError("target subagent is not running")

        active = self._active_run(target_run_id)
        caller_run_id = str(caller_run.get("id") or "") if caller_run else ""
        sender = (
            f"子 Agent {caller_run_id}"
            if caller_run_id
            else f"主持 Session {caller_session_id}"
        )
        delivery = dict(
            active.runtime.prompt(
                active.child_session_id,
                f"[{sender} 直接调用]\n{message}",
                client_message_id=_bounded_text(
                    payload.get("_toolCallId") or f"peer-call:{uuid.uuid4()}",
                    maximum=240,
                ),
                delivery="steer",
            )
        )
        return {
            "schemaVersion": "rag-ime.agent-peer-call.v1",
            "ok": True,
            "accepted": True,
            "callerSessionId": caller_session_id,
            "callerRunId": caller_run_id,
            "targetRunId": target_run_id,
            "targetSessionId": str(target_run["childSessionId"]),
            "delivery": str(delivery.get("delivery") or "steer"),
            "turnId": str(delivery.get("turnId") or ""),
        }

    def _peer_runs(self, session_id: str) -> list[dict[str, object]]:
        root_session_id = self._delegation_root_session_id(session_id)
        caller_run = self.store.run_for_child_session(session_id)
        caller_run_id = str(caller_run.get("id") or "") if caller_run else ""
        peers: list[dict[str, object]] = []
        for batch in self.store.list_batches(limit=200, hydrate_artifacts=False):
            if (
                self._delegation_root_session_id(str(batch["parentSessionId"]))
                != root_session_id
            ):
                continue
            for run in batch["runs"]:
                run_id = str(run["id"])
                if run_id == caller_run_id:
                    continue
                peers.append(
                    {
                        "runId": run_id,
                        "childSessionId": str(run["childSessionId"]),
                        "templateId": str(run["templateId"]),
                        "state": str(run["state"]),
                        "task": _bounded_text(run.get("task"), maximum=240),
                        "callable": str(run["state"]) == "running",
                    }
                )
        return peers

    def _run_tree(self, session_id: str) -> dict[str, object]:
        root_session_id = self._delegation_root_session_id(session_id)
        runs: list[dict[str, object]] = []
        for batch in self.store.list_batches(limit=200, hydrate_artifacts=False):
            if (
                self._delegation_root_session_id(str(batch["parentSessionId"]))
                != root_session_id
            ):
                continue
            runs.extend(dict(run) for run in batch["runs"])
        runs.sort(
            key=lambda run: (
                int(run.get("createdAtMs") or 0),
                int(run.get("ordinal") or 0),
                str(run.get("id") or ""),
            )
        )
        node_by_run_id = {
            str(run["id"]): {"run": run, "children": []}
            for run in runs
        }
        roots: list[dict[str, object]] = []
        for run in runs:
            node = node_by_run_id[str(run["id"])]
            parent_run_id = str(run.get("parentRunId") or "")
            parent = node_by_run_id.get(parent_run_id)
            if parent is None:
                roots.append(node)
            else:
                children = parent["children"]
                if isinstance(children, list):
                    children.append(node)
        return {
            "schemaVersion": "rag-ime.agent-subagent-tree.v1",
            "rootSessionId": root_session_id,
            "nodeCount": len(runs),
            "maxDepth": max(
                (int(run.get("depth") or 1) for run in runs),
                default=0,
            ),
            "roots": roots,
        }

    def _assert_delegation_tree_access(
        self,
        session_id: str,
        batch: Mapping[str, object],
    ) -> None:
        caller_root = self._delegation_root_session_id(session_id)
        target_root = self._delegation_root_session_id(
            str(batch.get("parentSessionId") or "")
        )
        if not caller_root or caller_root != target_root:
            raise ValueError("delegated run does not belong to this Session tree")

    def _delegation_root_session_id(self, session_id: str) -> str:
        current = str(session_id or "").strip()
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            run = self.store.run_for_child_session(current)
            if run is None:
                return current
            batch = self.store.get_batch(
                str(run["batchId"]),
                hydrate_artifacts=False,
            )
            current = str(batch.get("parentSessionId") or "")
        return current

    def console(self, parent_session_id: str, run_id: str) -> dict[str, object]:
        run = self.store.get_run(run_id)
        batch = self.store.get_batch(str(run["batchId"]))
        self._assert_delegation_tree_access(parent_session_id, batch)
        with self._lock:
            active = self._active_runs.get(run_id)

        conversation: dict[str, object]
        if active is not None:
            try:
                messages = active.runtime.messages(active.child_session_id)
                conversation = {
                    "availability": "available",
                    "source": "active_runtime",
                    "items": messages,
                }
            except Exception:
                conversation = {
                    "availability": "temporarily_unavailable",
                    "source": "active_runtime",
                    "items": [],
                }
        else:
            result = run.get("result")
            result_mapping = dict(result) if isinstance(result, Mapping) else {}
            persisted = result_mapping.get("messages")
            if isinstance(persisted, list):
                conversation = {
                    "availability": "available",
                    "source": "completion_snapshot",
                    "items": [dict(item) for item in persisted if isinstance(item, Mapping)],
                }
            else:
                final_message = result_mapping.get("message")
                items = (
                    [dict(final_message)]
                    if isinstance(final_message, Mapping)
                    else []
                )
                conversation = {
                    "availability": "partial" if items else "unavailable",
                    "source": "legacy_result",
                    "items": items,
                }

        inbox = self.store.list_inbox(run_id)
        child_available = True
        try:
            self.sessions.get(str(run["childSessionId"]))
        except KeyError:
            child_available = False
        state = str(run["state"])
        retry_available, retry_reason = self._retry_capability(run)
        contract_invalid = _run_contract_invalid(run)
        capabilities = {
            "steer": {
                "available": active is not None and state == "running",
                "reason": "" if active is not None and state == "running" else "仅运行中的子 Agent 可接收干预",
            },
            "abort": {
                "available": state in _ACTIVE_STATES,
                "reason": "" if state in _ACTIVE_STATES else "任务已结束",
            },
            "retry": {
                "available": retry_available,
                "reason": retry_reason,
            },
            "resume": {
                "available": (
                    state in {"failed", "aborted", "timed_out"} or contract_invalid
                ) and child_available,
                "reason": (
                    ""
                    if (
                        state in {"failed", "aborted", "timed_out"} or contract_invalid
                    ) and child_available
                    else "仅保留会话的中断任务或合同无效交付可继续"
                ),
            },
            "reply": {
                "available": active is not None
                and any(item["status"] == "pending" for item in inbox),
                "reason": (
                    ""
                    if active is not None
                    and any(item["status"] == "pending" for item in inbox)
                    else "当前没有等待主持人回复的问题"
                ),
            },
        }
        records = self.artifacts.lifecycle_records(
            owner_kind="subagent_run",
            owner_id=run_id,
        )
        activity = [
            {
                "id": str(item.get("recordId") or ""),
                "eventType": str(item.get("eventType") or ""),
                "createdAtMs": int(item.get("createdAtMs") or 0),
                "payload": dict(item.get("payload") or {})
                if isinstance(item.get("payload"), Mapping)
                else {},
            }
            for item in records[-120:]
        ]
        return {
            "schemaVersion": "rag-ime.agent-subagent-console.v1",
            "ok": True,
            "parentSessionId": parent_session_id,
            "run": run,
            "capabilities": capabilities,
            "conversation": conversation,
            "activity": activity,
            "inbox": inbox,
            "controls": self.store.list_controls(run_id),
            "updatedAtMs": _timestamp(None),
        }

    def control(
        self,
        parent_session_id: str,
        run_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        action = _bounded_text(payload.get("action"), maximum=24, required=True)
        client_action_id = _bounded_text(
            payload.get("clientActionId"),
            maximum=128,
            required=True,
        )
        message = _bounded_text(payload.get("message"), maximum=4_000)
        inbox_id = _bounded_text(payload.get("inboxId"), maximum=180)
        if action == "resume" and not message:
            message = (
                "修复上一次结构化交付，使 value 严格满足原 JSON Schema；"
                "不要重做已经完成的工作。完成后重新调用 structured_output。"
                if _run_contract_invalid(self.store.get_run(run_id))
                else (
                    "继续之前中断的任务。已完成节点不可重跑；先核对已有进度与"
                    "原 Tool 回执。只读操作可以继续；写文件、命令或外部操作若"
                    "回执不能证明结果，停止并报告 blocker，不得盲目重放。"
                )
            )
        command_payload = {
            "action": action,
            "message": message,
            "inboxId": inbox_id,
        }
        control, created = self.store.claim_control(
            run_id=run_id,
            parent_session_id=parent_session_id,
            client_action_id=client_action_id,
            action=action,
            payload=command_payload,
        )
        if not created:
            return {
                "schemaVersion": "rag-ime.agent-subagent-control.v1",
                "ok": str(control["state"]) != "failed",
                "replayed": True,
                "control": control,
            }

        try:
            result: dict[str, object]
            if action != "abort":
                self.sessions.require_goal_execution(parent_session_id)
            if action == "steer":
                if not message:
                    raise ValueError("steer message is required")
                active = self._active_run(run_id)
                result = dict(
                    active.runtime.prompt(
                        active.child_session_id,
                        message,
                        client_message_id=client_action_id,
                        delivery="steer",
                    )
                )
            elif action == "reply":
                if not inbox_id or not message:
                    raise ValueError("reply requires inboxId and message")
                pending = next(
                    (
                        item
                        for item in self.store.list_inbox(run_id)
                        if item["id"] == inbox_id and item["status"] == "pending"
                    ),
                    None,
                )
                if pending is None:
                    raise ValueError("delegated inbox item is no longer pending")
                active = self._active_run(run_id)
                resolution = dict(
                    active.runtime.resolve_ui_request(
                        active.child_session_id,
                        str(pending.get("requestId") or ""),
                        response={"value": message},
                    )
                )
                replied = self.store.reply_inbox(
                    run_id=run_id,
                    inbox_id=inbox_id,
                    response={
                        "message": message,
                        "requestId": str(pending.get("requestId") or ""),
                        "resolution": resolution,
                    },
                )
                result = {"resolution": resolution, "inbox": replied}
            elif action == "abort":
                result = self.abort(parent_session_id, {"runId": run_id})
            elif action == "retry":
                current = self.store.get_run(run_id)
                retry_available, retry_reason = self._retry_capability(current)
                if not retry_available:
                    raise ValueError(retry_reason)
                result = self.delegate(
                    parent_session_id,
                    self._retry_payload(current),
                )
            elif action == "resume":
                with self._lock:
                    previous_thread = self._threads.get(run_id)
                if (
                    previous_thread is not None
                    and previous_thread is not threading.current_thread()
                ):
                    previous_thread.join(timeout=2.0)
                    if previous_thread.is_alive():
                        raise ValueError("delegated run is still finalizing")
                reopened = self.store.reopen_run(run_id)
                result = {"run": reopened, "delivery": "retained_session"}
            else:
                raise ValueError("unsupported delegated control action")
            completed = self.store.finish_control(str(control["id"]), result=result)
            if action == "resume":
                self._start_run_thread(run_id)
            return {
                "schemaVersion": "rag-ime.agent-subagent-control.v1",
                "ok": True,
                "replayed": False,
                "control": completed,
            }
        except Exception as exc:
            self.store.finish_control(
                str(control["id"]),
                error=_bounded_text(exc, maximum=500),
            )
            raise

    def _retry_payload(self, run: Mapping[str, object]) -> dict[str, object]:
        launch = (
            dict(run.get("launchDigest"))
            if isinstance(run.get("launchDigest"), Mapping)
            else {}
        )
        tools = [
            str(item)
            for item in launch.get("tools", [])
            if isinstance(item, str) and item != "structured_output"
        ]
        workspace_access = str(launch.get("workspaceAccess") or "none")
        payload: dict[str, object] = {
            "agent": run["templateId"],
            "version": run["templateVersion"],
            "expectedOutput": run["expectedOutput"],
            "acceptanceCriteria": run["acceptanceCriteria"],
            "outputSchema": run.get("outputSchema", {}),
            "task": run["task"],
            "todoTask": run["todoTask"],
            "contextMode": "fresh",
            "wait": False,
            "piSkillsEnabled": bool(launch.get("piSkillsEnabled", False)),
            "codexSkillsEnabled": bool(launch.get("codexSkillsEnabled", False)),
            "access": (
                workspace_access
                if workspace_access in {"read_only", "write"}
                else "inherit"
            ),
            "_retryLineage": {
                "nodeId": run["nodeId"],
                "attemptNumber": int(run["attemptNumber"]) + 1,
                "predecessorAttemptId": run["attemptId"],
                "ownerRunId": run["ownerRunId"],
            },
        }
        model_profile = str(launch.get("modelProfile") or "").strip()
        if model_profile:
            payload["modelProfile"] = model_profile
        thinking_level = str(launch.get("thinkingLevel") or "").strip()
        if thinking_level:
            payload["thinkingLevel"] = thinking_level
        if tools:
            payload["allowedTools"] = tools
        return payload

    def _retry_capability(
        self,
        run: Mapping[str, object],
    ) -> tuple[bool, str]:
        state = str(run.get("state") or "")
        if state not in _TERMINAL_STATES:
            return False, "等待当前任务结束"
        result = run.get("result") if isinstance(run.get("result"), Mapping) else {}
        failure_class = str(result.get("failureClass") or "")
        usage = run.get("usage") if isinstance(run.get("usage"), Mapping) else {}
        if failure_class != "transient_runtime":
            return False, "工具、逻辑、合同或验收错误必须显式修复或改派"
        if int(usage.get("toolCount") or 0) > 0:
            return False, "已有 Tool 调用；必须先核对原回执，不能盲目重试"
        if int(run.get("attemptNumber") or 1) >= 2:
            return False, "此节点已用完唯一一次有界重试"
        predecessor_attempt_id = str(run.get("attemptId") or "")
        for batch in self.store.list_batches(limit=200):
            for candidate in batch.get("runs", []):
                if (
                    isinstance(candidate, Mapping)
                    and str(candidate.get("predecessorAttemptId") or "")
                    == predecessor_attempt_id
                ):
                    return False, "唯一一次有界重试已经排队"
        return True, ""

    def _active_run(self, run_id: str) -> _ActiveDelegatedRun:
        with self._lock:
            active = self._active_runs.get(run_id)
        if active is None:
            raise ValueError("delegated run is not currently active")
        return active

    def inspect_artifact(
        self,
        parent_session_id: str,
        artifact_id: str,
        *,
        limit: int = 100,
    ) -> dict[str, object]:
        self.sessions.get(parent_session_id)
        reference = self.artifacts.reference_by_id(artifact_id)
        if str(reference.get("ownerKind") or "") != "subagent_run":
            raise ValueError("artifact is not a delegated run lifecycle")
        run = self.store.get_run(str(reference.get("ownerId") or ""))
        batch = self.store.get_batch(str(run["batchId"]))
        _assert_batch_owner(batch, parent_session_id)
        return self.artifacts.inspect(artifact_id, limit=limit)

    def abort(self, parent_session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        identifier = str(payload.get("runId") or payload.get("batchId") or "").strip()
        if not identifier:
            raise ValueError("runId or batchId is required")
        try:
            batch = self.store.get_batch(identifier, hydrate_artifacts=False)
        except KeyError:
            run = self.store.get_run(identifier, hydrate_artifacts=False)
            batch = self.store.get_batch(str(run["batchId"]), hydrate_artifacts=False)
        _assert_batch_owner(batch, parent_session_id)
        # Cancellation is accepted by the primary rows. Advisory lifecycle
        # materialization must not delay delivery to the running Pi Session.
        active = self.store.request_abort(identifier, sync_artifacts=False)
        for run_id in active:
            self._request_cancel(run_id, state="aborted", reason="Stopped by user")
        deadline = (
            time.monotonic()
            + self._cancellation_grace_ms / 1000.0
            + 1.0
        )
        current = self.store.get_batch(str(batch["id"]), hydrate_artifacts=False)
        while (
            any(
                str(run.get("state") or "") in _ACTIVE_STATES
                for run in current["runs"]
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            current = self.store.get_batch(str(batch["id"]), hydrate_artifacts=False)
        self._schedule_pending_result_contexts(hydrate_artifacts=False)
        current = self.store.get_batch(str(batch["id"]), hydrate_artifacts=False)
        pending_run_ids = [
            str(run.get("id") or "")
            for run in current["runs"]
            if str(run.get("state") or "") in _ACTIVE_STATES
        ]
        return {
            "schemaVersion": "rag-ime.agent-delegation-abort.v1",
            "ok": True,
            "batch": current,
            "cancellation": {
                "schemaVersion": "rag-ime.agent-delegation-cancellation.v1",
                "state": "requested" if pending_run_ids else "terminated",
                "pendingRunIds": pending_run_ids,
                "graceMs": self._cancellation_grace_ms,
            },
        }

    def cancel_causal(
        self,
        parent_session_id: str,
        *,
        request_id: str,
        scope_kind: str,
        scope_id: str,
        source_revision: int,
        reason: str,
    ) -> dict[str, object]:
        selection = self.store.request_causal_abort(
            request_id=request_id,
            scope_kind=scope_kind,
            scope_id=scope_id,
            source_revision=source_revision,
            reason=reason,
            sync_artifacts=False,
        )
        for run_id in selection["activeRunIds"]:
            self._request_cancel(
                str(run_id),
                state="aborted",
                reason="Parent lifecycle cancelled",
            )
        deadline = (
            time.monotonic()
            + self._cancellation_grace_ms / 1000.0
            + 1.0
        )
        batches = [
            self.store.get_batch(str(batch_id), hydrate_artifacts=False)
            for batch_id in selection["batchIds"]
        ]
        while (
            any(
                str(run.get("state") or "") in _ACTIVE_STATES
                for batch in batches
                for run in batch["runs"]
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            batches = [
                self.store.get_batch(str(batch_id), hydrate_artifacts=False)
                for batch_id in selection["batchIds"]
            ]
        self._schedule_pending_result_contexts(hydrate_artifacts=False)
        pending_run_ids = [
            str(run["id"])
            for batch in batches
            for run in batch["runs"]
            if str(run.get("state") or "") in _ACTIVE_STATES
        ]
        return {
            "schemaVersion": "rag-ime.agent-delegation-lifecycle-cancellation-summary.v1",
            "requestId": request_id,
            "parentSessionId": parent_session_id,
            "scopeKind": scope_kind,
            "scopeId": scope_id,
            "sourceRevision": int(source_revision),
            "state": (
                "requested"
                if pending_run_ids
                else "terminated"
            ),
            "pendingRunIds": pending_run_ids,
            "batches": [
                {
                    "batchId": str(batch["id"]),
                    "state": str(batch["state"]),
                    "runs": [
                        {
                            "runId": str(run["id"]),
                            "state": str(run["state"]),
                        }
                        for run in batch["runs"]
                    ],
                }
                for batch in batches
            ],
            "excludedRoomBoundBatchIds": list(
                selection["excludedRoomBoundBatchIds"]
            ),
        }

    def wait(self, batch_id: str) -> dict[str, object]:
        batch = self.store.get_batch(batch_id, hydrate_artifacts=False)
        maximum = max(
            int(run["budget"]["maxDurationMs"])
            for run in batch["runs"]
        )
        deadline = time.monotonic() + maximum / 1000.0 + 10.0
        while time.monotonic() < deadline:
            batch = self.store.get_batch(batch_id, hydrate_artifacts=False)
            if str(batch["state"]) in _TERMINAL_STATES:
                self.collect_expired_sessions(force=False)
                for run in batch["runs"]:
                    self.store._sync_run_artifact(str(run["id"]))
                return self.store.get_batch(batch_id)
            time.sleep(0.025)
        self.store.request_abort(batch_id)
        batch = self.store.get_batch(batch_id, hydrate_artifacts=False)
        for run in batch["runs"]:
            if str(run["state"]) in _ACTIVE_STATES:
                self._request_cancel(
                    str(run["id"]),
                    state="timed_out",
                    reason="Delegation wait deadline exceeded",
                )
        for run in batch["runs"]:
            self.store._sync_run_artifact(str(run["id"]))
        return self.store.get_batch(batch_id)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            active_runs = list(self._active_runs.values())
            threads = list(self._threads.values())
        for active in active_runs:
            try:
                if active.owns_runtime:
                    active.runtime.stop()
                else:
                    active.runtime.abort(active.child_session_id)
            except Exception:
                pass
            active.forced.set()
        deadline = time.monotonic() + self._cancellation_grace_ms / 1000.0 + 1.0
        for thread in threads:
            if thread is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def reconfigure(self, config: PiRuntimeConfig) -> None:
        self.close()
        with self._lock:
            self.runtime_config = config
            self._runtime_driver_factory.reconfigure(config)
            self._tool_gateway_token = config.tool_gateway_token
            self._tool_gateway_url = config.tool_gateway_url
            self._closed = False
        recoverable = self.store.reconcile_interrupted_runs()
        self.collect_expired_sessions(force=True)
        if config.enabled:
            for run_id in recoverable:
                self._start_run_thread(run_id)

    def refresh_runtime_factory(self) -> None:
        """Restart delegated-driver ownership after Kernel policy changes."""

        self.close()
        with self._lock:
            factory_config = getattr(self._runtime_driver_factory, "config", None)
            if isinstance(factory_config, PiRuntimeConfig):
                self.runtime_config = factory_config
                self._tool_gateway_token = factory_config.tool_gateway_token
                self._tool_gateway_url = factory_config.tool_gateway_url
            self._closed = False
        recoverable = self.store.reconcile_interrupted_runs()
        self.collect_expired_sessions(force=True)
        if self.runtime_config.enabled:
            for run_id in recoverable:
                self._start_run_thread(run_id)

    def owns_session(self, session_id: str) -> bool:
        return self.store.owns_session(session_id)

    def runtime_session_context(
        self,
        session: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Return the delegated policy layer for one product-owned child Session."""

        session_id = str(session.get("id") or "").strip()
        if not session_id:
            return {}
        run = self.store.run_for_child_session(session_id)
        if run is None:
            return {}
        batch = self.store.get_batch(str(run["batchId"]))
        return {
            "agentTemplateId": run["templateId"],
            "agentTemplateVersion": run["templateVersion"],
            "delegationDepth": batch["depth"],
            "todoTask": run["todoTask"],
            "todoPhase": run["todoPhase"],
            "toolProfileVersion": session["toolProfileVersion"],
        }

    def _start_run_thread(self, run_id: str) -> None:
        with self._lock:
            if self._closed or run_id in self._threads:
                return
            thread = threading.Thread(
                target=self._run_task,
                args=(run_id,),
                name=f"rag-ime-{run_id[-8:]}",
                daemon=True,
            )
            self._threads[run_id] = thread
            thread.start()

    def _run_task(self, run_id: str) -> None:
        try:
            run = self.store.start_run(run_id)
        except ValueError:
            with self._lock:
                self._threads.pop(run_id, None)
            return
        batch = self.store.get_batch(str(run["batchId"]))
        child_session_id = str(run["childSessionId"])
        parent_session_id = str(batch["parentSessionId"])
        terminal = threading.Event()
        forced = threading.Event()
        terminal_error = ""
        terminal_failure_context: dict[str, object] = {}
        completed = False
        budget_reason = ""
        last_message: dict[str, object] = {}
        turn_count = int(run["usage"]["turnCount"])
        total_tokens = int(run["usage"]["totalTokens"])
        output_chars = 0
        base_tool_count = int(run["usage"]["toolCount"])
        tool_ids: set[str] = set()
        update_lock = threading.RLock()
        soft_reasons: set[str] = set()
        hard_scheduled = False
        active_run: _ActiveDelegatedRun | None = None

        def usage_checkpoint() -> dict[str, object]:
            return {
                "usage": {
                    "turnCount": turn_count,
                    "toolCount": base_tool_count + len(tool_ids),
                    "totalTokens": total_tokens,
                    "outputChars": output_chars,
                },
                "lastMessage": last_message,
            }

        def persist_runtime_event(
            event: AgentEventEnvelope,
            *,
            terminal_state: str = "",
            error: str = "",
        ) -> None:
            checkpoint = usage_checkpoint()
            if terminal_state:
                checkpoint.update(
                    {
                        "terminalState": terminal_state,
                        "error": _bounded_text(error, maximum=500),
                        "terminalAtMs": event.created_at_ms,
                    }
                )
            try:
                self.store.checkpoint_runtime_event(run_id, event, checkpoint)
            except Exception:
                # The primary DB projection still finishes the run. Startup
                # reconciliation will fail this run closed if the artifact is unusable.
                pass

        def schedule_hard_budget(reason: str, event: AgentEventEnvelope) -> None:
            nonlocal budget_reason, hard_scheduled
            with update_lock:
                if hard_scheduled:
                    return
                budget_reason = reason
                hard_scheduled = True
            self.store.append_budget_event(run_id, reason, phase="hard")
            persist_runtime_event(event)
            self._request_cancel(run_id, state="failed", reason=reason)

        def check_usage_budget(event: AgentEventEnvelope) -> None:
            budget = run["budget"]
            checks = (
                (turn_count, int(budget["maxTurns"]), "turn budget exceeded"),
                (
                    base_tool_count + len(tool_ids),
                    int(budget["maxToolCalls"]),
                    "tool-call budget exceeded",
                ),
                (total_tokens, int(budget["maxTotalTokens"]), "token budget exceeded"),
                (output_chars, int(budget["maxOutputChars"]), "output budget exceeded"),
            )
            for current, maximum, reason in checks:
                # maxTurns/maxToolCalls use zero as an explicit unlimited
                # sentinel. Other safety budgets always remain positive.
                if maximum > 0 and current > maximum:
                    schedule_hard_budget(reason, event)
                    return
                if maximum <= 0:
                    continue
                threshold = max(1, int(maximum * _SOFT_BUDGET_RATIO + 0.999))
                if current >= threshold and reason not in soft_reasons:
                    soft_reasons.add(reason)
                    self.store.append_budget_event(run_id, reason, phase="soft")
                    persist_runtime_event(event)

        def accept_runtime_terminal(
            event: AgentEventEnvelope,
            *,
            state: str,
            error: str = "",
        ) -> tuple[str, str]:
            def persist() -> tuple[str, str]:
                cancellation_state = ""
                cancellation_reason = ""
                if active_run is not None:
                    cancellation_state = active_run.cancellation_state
                    cancellation_reason = active_run.cancellation_reason
                final_state = cancellation_state or state
                final_error = cancellation_reason or error
                persist_runtime_event(
                    event,
                    terminal_state=final_state,
                    error=final_error,
                )
                # Wake the worker only after the recovery checkpoint is
                # durable. _request_cancel uses this same lock, so cancellation
                # and runtime terminal delivery have one linearized winner.
                terminal.set()
                return final_state, final_error

            if active_run is None:
                return persist()
            with active_run.lock:
                return persist()

        def observe(event: AgentEventEnvelope) -> None:
            nonlocal terminal_error, completed, last_message
            nonlocal turn_count, total_tokens, output_chars
            nonlocal terminal_failure_context
            if event.session_id != child_session_id:
                return
            with update_lock:
                if terminal.is_set():
                    return
                cancellation_state = ""
                cancellation_reason = ""
                if active_run is not None:
                    with active_run.lock:
                        cancellation_state = active_run.cancellation_state
                        cancellation_reason = active_run.cancellation_reason
                if cancellation_state and event.event_type not in {
                    "turn_completed",
                    "turn_failed",
                }:
                    # Cancellation owns the terminal projection. Runtime output
                    # arriving after that decision is late evidence and cannot
                    # mutate usage, inbox state, or the recovery checkpoint.
                    return
                if event.event_type == "text_delta":
                    output_chars += len(str(event.payload.get("delta") or ""))
                elif event.event_type == "tool_started":
                    identity = str(
                        event.payload.get("toolCallId")
                        or event.payload.get("callId")
                        or event.event_id
                    )
                    tool_ids.add(identity)
                    persist_runtime_event(event)
                    self.store.update_usage(
                        run_id,
                        turn_count=turn_count,
                        tool_count=base_tool_count + len(tool_ids),
                        total_tokens=total_tokens,
                    )
                elif event.event_type == "tool_progress":
                    summary = _bounded_text(
                        event.payload.get("summary")
                        or event.payload.get("message")
                        or event.payload.get("toolName")
                        or "工具仍在执行",
                        maximum=240,
                    )
                    self.store.upsert_inbox(
                        run_id=run_id,
                        parent_session_id=parent_session_id,
                        child_session_id=child_session_id,
                        request_id=f"progress:{event.event_id}",
                        kind="progress",
                        title=_bounded_text(
                            event.payload.get("toolName") or "执行进度",
                            maximum=160,
                        ),
                        message=summary,
                        turn_id=event.turn_id,
                        created_at_ms=event.created_at_ms,
                    )
                    persist_runtime_event(event)
                elif event.event_type == "user_input_required":
                    method = str(event.payload.get("method") or "")
                    kind = "interview" if method in {"input", "editor"} else "need_decision"
                    self.store.upsert_inbox(
                        run_id=run_id,
                        parent_session_id=parent_session_id,
                        child_session_id=child_session_id,
                        request_id=str(
                            event.payload.get("requestId") or event.event_id
                        ),
                        kind=kind,
                        title=_bounded_text(
                            event.payload.get("title")
                            or ("需要补充信息" if kind == "interview" else "需要主持人决定"),
                            maximum=160,
                        ),
                        message=_bounded_text(
                            event.payload.get("message") or "",
                            maximum=500,
                        ),
                        request_payload={
                            key: event.payload[key]
                            for key in (
                                "requestId",
                                "requestKind",
                                "method",
                                "options",
                                "placeholder",
                                "prefill",
                                "defaultValue",
                                "timeout",
                                "runId",
                            )
                            if key in event.payload
                        },
                        turn_id=event.turn_id,
                        created_at_ms=event.created_at_ms,
                    )
                    persist_runtime_event(event)
                elif event.event_type == "message_completed":
                    message = event.payload.get("message")
                    if isinstance(message, Mapping) and str(message.get("role") or "") == "assistant":
                        turn_count += 1
                        last_message = dict(message)
                        usage = event.payload.get("usage")
                        if isinstance(usage, Mapping):
                            total_tokens += max(0, int(usage.get("totalTokens") or 0))
                        persist_runtime_event(event)
                        self.store.update_usage(
                            run_id,
                            turn_count=turn_count,
                            tool_count=base_tool_count + len(tool_ids),
                            total_tokens=total_tokens,
                            summary="子 Agent 已返回一个阶段输出",
                            updated_at_ms=event.created_at_ms,
                        )
                elif event.event_type == "turn_completed":
                    final_state, final_error = accept_runtime_terminal(
                        event,
                        state="completed",
                    )
                    completed = final_state == "completed"
                    terminal_error = final_error
                elif event.event_type == "turn_failed":
                    runtime_error = _bounded_text(
                        event.payload.get("error") or "delegated Pi turn failed",
                        maximum=500,
                    )
                    terminal_failure_context = {
                        key: event.payload[key]
                        for key in (
                            "failureKind",
                            "retryable",
                            "hadToolActivity",
                            "retryExhausted",
                            "providerRetryAttempts",
                            "providerRetryMaxAttempts",
                            "nextStep",
                        )
                        if key in event.payload
                    }
                    _, terminal_error = accept_runtime_terminal(
                        event,
                        state="failed",
                        error=runtime_error,
                    )
                if not terminal.is_set():
                    check_usage_budget(event)

        remove_observer = self.events.add_observer(observe)
        context = dict(
            self.runtime_session_context(self.sessions.get(child_session_id))
        )
        owns_runtime = self._runtime_provider is None
        if self._runtime_provider is not None:
            runtime = self._runtime_provider()
        elif self._legacy_runtime_factory is not None:
            runtime = self._legacy_runtime_factory(
                config=replace(self.runtime_config, idle_timeout_seconds=0),
                sessions=self.sessions,
                events=self.events,
                media_resolver=self.media_resolver,
                session_context_provider=lambda _session: context,
            )
        else:
            runtime = self._runtime_driver_factory.create(
                RuntimeDriverContext(
                    sessions=self.sessions,
                    events=self.events,
                    media_resolver=self.media_resolver,
                    tool_gateway_token=self._tool_gateway_token,
                    tool_gateway_url=self._tool_gateway_url,
                    tool_manifest_provider=self._tool_manifest_provider,
                    compaction_observer=self._compaction_observer,
                    prompt_settings_provider=self._prompt_settings_provider,
                    scenario_policy_provider=self._scenario_policy_provider,
                ),
                purpose="delegated",
                session_context_provider=lambda _session: context,
            )
        active_run = _ActiveDelegatedRun(
            runtime=runtime,
            owns_runtime=owns_runtime,
            child_session_id=child_session_id,
            terminal=terminal,
            forced=forced,
            lock=threading.RLock(),
        )
        with self._lock:
            self._active_runs[run_id] = active_run
        self._publish_parent_progress(parent_session_id, batch, run, "子 Agent 已开始")
        state = "failed"
        error = ""
        result: dict[str, object] = {}
        try:
            if self.store.get_batch(str(batch["id"]))["abortRequested"]:
                state = "aborted"
                error = "Stopped by user"
            else:
                prompt = self.store.latest_resume_message(run_id) or _subagent_prompt(run, batch)
                runtime.prompt(child_session_id, prompt)
                duration_seconds = int(run["budget"]["maxDurationMs"]) / 1000.0
                started = time.monotonic()
                soft_deadline = started + duration_seconds * _SOFT_BUDGET_RATIO
                hard_deadline = started + duration_seconds
                duration_soft_emitted = False
                duration_hard_emitted = False
                while not terminal.wait(0.025) and not forced.is_set():
                    now = time.monotonic()
                    if not duration_soft_emitted and now >= soft_deadline:
                        duration_soft_emitted = True
                        self.store.append_budget_event(
                            run_id,
                            "duration budget approaching",
                            phase="soft",
                        )
                    if not duration_hard_emitted and now >= hard_deadline:
                        duration_hard_emitted = True
                        budget_reason = "duration budget exceeded"
                        self.store.append_budget_event(run_id, budget_reason, phase="hard")
                        self._request_cancel(
                            run_id,
                            state="timed_out",
                            reason=budget_reason,
                        )

                with active_run.lock:
                    cancellation_state = active_run.cancellation_state
                    cancellation_reason = active_run.cancellation_reason
                if cancellation_state:
                    state = cancellation_state
                    error = cancellation_reason
                elif self.store.get_batch(str(batch["id"]))["abortRequested"]:
                    state = "aborted"
                    error = "Stopped by user"
                elif budget_reason:
                    state = "failed"
                    error = budget_reason
                elif terminal_error:
                    state = "failed"
                    error = terminal_error
                elif completed:
                    run = self.store.finalize_structured_output_contract(run_id)
                    contract = (
                        dict(run.get("contract"))
                        if isinstance(run.get("contract"), Mapping)
                        else {}
                    )
                    contract_status = str(
                        contract.get("status") or "not_requested"
                    )
                    state = "completed"
                    summary = _message_summary(
                        last_message,
                        int(run["budget"]["maxOutputChars"]),
                    )
                    if not summary and contract_status == "valid":
                        summary = _bounded_text(
                            json.dumps(
                                run.get("structuredOutput"),
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            maximum=int(run["budget"]["maxOutputChars"]),
                        )
                    try:
                        conversation = runtime.messages(child_session_id)
                    except Exception:
                        conversation = []
                    result = {
                        "summary": summary,
                        "message": last_message,
                        "messages": conversation,
                        "childSessionId": child_session_id,
                        "templateId": run["templateId"],
                        # `turn_completed` means the delegated runtime stopped
                        # normally.  Only the parent/Room quality gate can
                        # decide whether its claims satisfy the parent task.
                        "deliveryStatus": "returned",
                        "contractStatus": contract_status,
                        "contractError": str(contract.get("error") or ""),
                        "verificationStatus": (
                            "contract_valid"
                            if contract_status == "valid"
                            else "contract_invalid"
                            if contract_status == "invalid"
                            else "unverified"
                        ),
                        "authority": "evidence_only",
                        **(
                            {"structuredOutput": run.get("structuredOutput")}
                            if contract_status == "valid"
                            else {}
                        ),
                    }
                else:
                    error = "delegated Pi turn ended without a terminal event"
        except Exception as exc:
            error = _bounded_text(exc, maximum=500)
            if isinstance(exc, AgentRuntimeError) and "stopped" in error.lower():
                state = "aborted"
        finally:
            remove_observer()
            terminal.set()
            try:
                if owns_runtime:
                    runtime.stop()
                else:
                    close_session = getattr(runtime, "close_session", None)
                    if callable(close_session):
                        close_session(child_session_id)
            except Exception:
                pass
            with self._lock:
                self._active_runs.pop(run_id, None)
        failure_class = _subagent_failure_class(error) if state == "failed" else ""
        observed_tool_count = base_tool_count + len(tool_ids)
        retry_safe = failure_class == "transient_runtime" and observed_tool_count == 0
        retry_exhausted = terminal_failure_context.get("retryExhausted") is True
        automatic_retry_eligible = retry_safe and not retry_exhausted
        auto_retry = (
            state == "failed"
            and automatic_retry_eligible
            and int(run.get("attemptNumber") or 1) < 2
        )
        if state == "failed":
            launch = (
                dict(run.get("launchDigest"))
                if isinstance(run.get("launchDigest"), Mapping)
                else {}
            )
            failure_report = {
                "schemaVersion": "rag-ime.agent-subagent-failure-report.v1",
                "failureClass": failure_class,
                "error": error,
                "retryExhausted": retry_exhausted,
                "providerRetryAttempts": _nonnegative_event_int(
                    terminal_failure_context.get("providerRetryAttempts")
                ),
                "providerRetryMaxAttempts": _nonnegative_event_int(
                    terminal_failure_context.get("providerRetryMaxAttempts")
                ),
                "toolCallCount": observed_tool_count,
                "completedToolCallsMayHaveSideEffects": observed_tool_count > 0,
                "modelRoute": str(launch.get("modelRoute") or "subagent"),
                "modelProfile": str(launch.get("modelProfile") or ""),
                "nextStep": _bounded_text(
                    terminal_failure_context.get("nextStep")
                    or (
                        "由上级继续当前任务、切换该运行角色的模型、重试、恢复或改派；"
                        "不要重放已有 Tool 回执不能证明安全的操作。"
                    ),
                    maximum=500,
                ),
            }
            result = {
                **result,
                "deliveryStatus": "not_returned",
                "verificationStatus": "not_applicable",
                "failureClass": failure_class,
                "failureReport": failure_report,
                "parentDecision": {
                    "required": True,
                    "parentSessionRemainsRunnable": True,
                    "allowedActions": [
                        "continue_parent",
                        "retry",
                        "resume",
                        "switch_model",
                        "redelegate",
                    ],
                    "recommendedAction": (
                        "switch_model"
                        if retry_exhausted
                        else "resume"
                        if observed_tool_count > 0
                        else "retry"
                    ),
                },
                "recovery": {
                    "runId": run_id,
                    "childSessionId": child_session_id,
                    "retryControlAction": "retry",
                    "resumeControlAction": "resume",
                    "modelRoute": str(launch.get("modelRoute") or "subagent"),
                    "completedToolsMustNotReplayBlindly": True,
                },
                "retryPolicy": {
                    "automaticEligible": automatic_retry_eligible,
                    "automaticLimit": 1,
                    "automaticScheduled": auto_retry,
                    "requiresReceiptReview": observed_tool_count > 0,
                },
            }
        try:
            final = self.store.finish_run(
                run_id,
                state=state,
                result=result,
                error=error,
                turn_count=turn_count,
                tool_count=base_tool_count + len(tool_ids),
                total_tokens=total_tokens,
            )
            terminal_batch = self.store.get_batch(str(batch["id"]))
            final = self._schedule_result_context(
                parent_session_id,
                terminal_batch,
                final,
            )
            self._publish_parent_progress(
                parent_session_id,
                self.store.get_batch(str(batch["id"])),
                final,
                (
                    "子 Agent 已返回，但交付合同无效；等待修复或改派"
                    if state == "completed"
                    and str(
                        dict(final.get("result") or {}).get("contractStatus")
                    )
                    == "invalid"
                    else "子 Agent 已返回结果，待主持会话核验"
                    if state == "completed"
                    else (
                        "子 Agent 模型连接重试耗尽；失败报告已返回，等待上级继续决策或恢复"
                        if retry_exhausted
                        else "子 Agent 未完成；失败报告已返回，等待上级继续决策或恢复"
                    )
                ),
            )
            self._record_retained_child_session(run_id, child_session_id)
            if auto_retry:
                try:
                    self.delegate(
                        parent_session_id,
                        self._retry_payload(final),
                    )
                except Exception:
                    self._publish_parent_progress(
                        parent_session_id,
                        terminal_batch,
                        final,
                        "瞬时错误的一次自动重试未能排队；请检查运行环境后手动重试或改派",
                    )
            self.collect_expired_sessions(force=False)
        finally:
            with self._lock:
                self._threads.pop(run_id, None)

    def collect_expired_sessions(
        self,
        *,
        force: bool = True,
        now_ms: int | None = None,
    ) -> int:
        """Destroy expired child-runtime state while preserving run results and Artifacts."""

        monotonic_now = time.monotonic()
        with self._retirement_lock:
            elapsed_ms = (monotonic_now - self._last_session_gc_monotonic) * 1_000
            if (
                not force
                and self._last_session_gc_monotonic > 0
                and elapsed_ms < self._subagent_session_gc_interval_ms
            ):
                return 0
            self._last_session_gc_monotonic = monotonic_now
        current_ms = _timestamp(now_ms)
        cutoff_ms = current_ms - self._subagent_session_retention_ms
        retired_count = 0
        for run_id, child_session_id in self.store.terminal_child_sessions(
            completed_before_ms=cutoff_ms
        ):
            try:
                session = self.sessions.get(child_session_id)
            except KeyError:
                continue
            if self._retire_child_session(run_id, child_session_id):
                retired_count += 1
        return retired_count

    def _record_retained_child_session(self, run_id: str, child_session_id: str) -> None:
        try:
            run = self.store.get_run(run_id)
            completed_at_ms = int(run.get("completedAtMs") or run.get("updatedAtMs") or 0)
            retained_until_ms = completed_at_ms + self._subagent_session_retention_ms
            now = _timestamp(None)
            self.artifacts.append_records(
                owner_kind="subagent_run",
                owner_id=run_id,
                records=[
                    {
                        "recordId": f"runtime-retained:{now}",
                        "eventType": "runtime_retained",
                        "createdAtMs": now,
                        "payload": {
                            "childSessionId": child_session_id,
                            "retainedUntilMs": retained_until_ms,
                        },
                    }
                ],
            )
        except Exception:
            # The run result is already durable. Retention metadata is advisory.
            return

    def _retire_child_session(self, run_id: str, child_session_id: str) -> bool:
        with self._retirement_lock:
            try:
                session = self.sessions.get(child_session_id)
                binding = self.sessions.runtime_binding(child_session_id)
                managed_root = self._runtime_driver_factory.session_root.expanduser().resolve(
                    strict=False
                )
                paths = {
                    str(session.get("sessionFile") or "").strip(),
                    str(binding.get("transcriptRef") or "").strip()
                    if isinstance(binding, Mapping)
                    else "",
                }
                for raw_path in paths:
                    if not raw_path:
                        continue
                    candidate = Path(raw_path).expanduser()
                    path = candidate.resolve(strict=False)
                    if (
                        not candidate.is_symlink()
                        and _is_within(path, managed_root)
                        and path.is_file()
                    ):
                        path.unlink()
                destroyed = self.sessions.destroy_internal(child_session_id)
                now = _timestamp(None)
                self.artifacts.append_records(
                    owner_kind="subagent_run",
                    owner_id=run_id,
                    records=[
                        {
                            "recordId": f"runtime-retired:{now}",
                            "eventType": "runtime_retired",
                            "createdAtMs": now,
                            "payload": {
                                "childSessionId": child_session_id,
                                "status": "destroyed",
                                "previousStatus": destroyed["status"],
                            },
                        }
                    ],
                )
                return True
            except Exception:
                # The run projection and Artifact are already durable. A stale
                # internal runtime remains hidden and can be retired on restart.
                return False

    def _request_cancel(self, run_id: str, *, state: str, reason: str) -> None:
        if state not in {"failed", "aborted", "timed_out"}:
            raise ValueError("delegated cancellation terminal state is invalid")
        with self._lock:
            active = self._active_runs.get(run_id)
        if active is None:
            return
        with active.lock:
            if active.terminal.is_set() or active.cancellation_state:
                return
            active.cancellation_state = state
            active.cancellation_reason = _bounded_text(reason, maximum=240)
        threading.Thread(
            target=self._cancel_active_run,
            args=(run_id, active),
            name=f"rag-ime-cancel-{run_id[-8:]}",
            daemon=True,
        ).start()

    def _cancel_active_run(self, run_id: str, active: _ActiveDelegatedRun) -> None:
        try:
            active.runtime.abort(active.child_session_id)
        except Exception:
            pass
        acknowledged = active.terminal.wait(self._cancellation_grace_ms / 1000.0)
        if not acknowledged and active.owns_runtime:
            try:
                active.runtime.stop()
            except Exception:
                pass
        # The decision is already in the active run and durable abort rows.
        # Deliver cancellation and owned-runtime escalation before advisory I/O.
        try:
            self.store.checkpoint_supervision(
                run_id,
                phase="hard",
                reason=active.cancellation_reason,
                requested_at_ms=_timestamp(None),
                grace_ms=self._cancellation_grace_ms,
            )
        except Exception:
            # An unavailable projection must not strand forced cancellation.
            pass
        if acknowledged:
            return
        try:
            # Publish the wake-up only after the forced supervision checkpoint
            # is durable. Otherwise the worker can finish and its caller can
            # tear down the runtime while this daemon is still writing the
            # artifact, leaving a visible terminal run stuck at phase=hard.
            self.store.checkpoint_supervision(
                run_id,
                phase="forced",
                reason=active.cancellation_reason or "Delegated runtime ignored cancellation",
                requested_at_ms=_timestamp(None),
                grace_ms=self._cancellation_grace_ms,
            )
        finally:
            active.forced.set()

    def _schedule_pending_result_contexts(self, *, hydrate_artifacts: bool = True) -> None:
        for pending in self.store.pending_result_context_runs():
            batch = self.store.get_batch(str(pending["batchId"]), hydrate_artifacts=hydrate_artifacts)
            run = self.store.get_run(str(pending["runId"]), hydrate_artifacts=hydrate_artifacts)
            self._schedule_result_context(
                str(pending["parentSessionId"]),
                batch,
                run,
                hydrate_artifacts=hydrate_artifacts,
            )

    def _schedule_result_context(
        self,
        parent_session_id: str,
        batch: Mapping[str, object],
        run: Mapping[str, object],
        *,
        hydrate_artifacts: bool = True,
    ) -> dict[str, object]:
        current = dict(run)
        if (
            str(batch.get("resultDeliveryMode") or "") != "next_turn"
            or current.get("resultContextScheduledAtMs") is not None
            or str(current.get("state") or "") not in _TERMINAL_STATES
        ):
            return current
        causal = batch.get("causalMetadata")
        if isinstance(causal, Mapping) and bool(causal.get("roomBound")):
            if not self.store.room_delivery_allowed(str(current["id"])):
                # The child may finish after its Root generation was revoked.
                # Persist a consumed delivery marker, but never enqueue a late
                # result into the fenced parent Session.
                return self.store.mark_result_context_scheduled(
                    str(current["id"]), sync_artifacts=hydrate_artifacts,
                )
        self.context_runtime.enqueue_delegated_result(
            parent_session_id=parent_session_id,
            batch=batch,
            run=current,
        )
        return self.store.mark_result_context_scheduled(
            str(current["id"]), sync_artifacts=hydrate_artifacts,
        )

    def _publish_parent_progress(
        self,
        parent_session_id: str,
        batch: Mapping[str, object],
        run: Mapping[str, object],
        summary: str,
    ) -> None:
        causal = batch.get("causalMetadata")
        if isinstance(causal, Mapping) and bool(causal.get("roomBound")):
            if not self.store.room_delivery_allowed(str(run["id"])):
                return
        result = dict(run["result"]) if isinstance(run.get("result"), Mapping) else {}
        parent_decision = (
            dict(result["parentDecision"])
            if isinstance(result.get("parentDecision"), Mapping)
            else {}
        )
        failure_report = (
            dict(result["failureReport"])
            if isinstance(result.get("failureReport"), Mapping)
            else {}
        )
        recovery = (
            dict(result["recovery"])
            if isinstance(result.get("recovery"), Mapping)
            else {}
        )
        self.events.publish(
            parent_session_id,
            "tool_progress",
            {
                "toolName": "agents",
                "toolCallId": f"subagent:{batch['id']}",
                "summary": summary,
                "batchId": batch["id"],
                "runId": run["id"],
                "agent": run["templateId"],
                "state": run["state"],
                "todoTask": run["todoTask"],
                "todoPhase": run["todoPhase"],
                "requiresParentTodoUpdate": run["state"] not in {"queued", "running"},
                **(
                    {
                        "parentDecisionRequired": bool(parent_decision.get("required")),
                        "failureReport": failure_report,
                        "recovery": recovery,
                    }
                    if isinstance(run.get("result"), Mapping)
                    and str(run.get("state") or "") == "failed"
                    else {}
                ),
            },
        )

    def _todo_task_link(
        self,
        parent_session_id: str,
        payload: Mapping[str, object],
        *,
        parent_run: Mapping[str, object] | None,
    ) -> tuple[str, str]:
        requested = _bounded_text(payload.get("todoTask"), maximum=240)
        if parent_run is not None:
            inherited = _bounded_text(parent_run.get("todoTask"), maximum=240)
            if requested and requested != inherited:
                raise ValueError("nested delegation must keep the parent run todoTask")
            return (
                inherited,
                _bounded_text(parent_run.get("todoPhase"), maximum=80),
            )

        todo = self.sessions.agent_todo(parent_session_id)
        linked_tasks = [
            (str(phase.get("name") or ""), item)
            for phase in todo.get("phases", [])
            if isinstance(phase, Mapping)
            for item in phase.get("tasks", [])
            if isinstance(item, Mapping)
        ]
        if not linked_tasks:
            if requested:
                raise ValueError("todoTask does not belong to the parent Session Todo")
            return "", ""
        if not requested:
            # Todo is optional navigation metadata, never delegation authority.
            # Only an explicit todoTask may link a child run into that document.
            return "", ""
        linked = next(
            (
                (phase_name, item)
                for phase_name, item in linked_tasks
                if str(item.get("content") or "") == requested
            ),
            None,
        )
        if linked is None:
            raise ValueError("todoTask does not belong to the parent Session Todo")
        phase_name, item = linked
        if str(item.get("status") or "") != "in_progress":
            raise ValueError("todoTask must be the current in_progress Todo task")
        return requested, _bounded_text(phase_name, maximum=80, required=True)


def _delegation_tasks(payload: Mapping[str, object]) -> list[dict[str, object]]:
    has_tasks = "tasks" in payload
    raw_tasks = payload.get("tasks")
    if has_tasks:
        mixed_fields = [
            field
            for field in (
                "agent",
                "version",
                "task",
                "expectedOutput",
                "acceptanceCriteria",
                "outputSchema",
                "modelProfile",
                "thinkingLevel",
                "budget",
                "access",
                "allowedTools",
                "piSkillsEnabled",
                "codexSkillsEnabled",
                "workspaceRoots",
            )
            if field in payload
        ]
        if mixed_fields:
            raise ValueError("tasks cannot be combined with single-task delegation fields")
    if not has_tasks:
        raw_tasks = [
            {
                "agent": payload.get("agent"),
                "version": payload.get("version") or "1",
                "task": payload.get("task"),
                "expectedOutput": payload.get("expectedOutput"),
                "acceptanceCriteria": payload.get("acceptanceCriteria"),
                **(
                    {"outputSchema": payload.get("outputSchema")}
                    if "outputSchema" in payload
                    else {}
                ),
                **{
                    field: payload.get(field)
                    for field in (
                        "modelProfile",
                        "thinkingLevel",
                        "budget",
                        "access",
                        "allowedTools",
                        "piSkillsEnabled",
                        "codexSkillsEnabled",
                        "workspaceRoots",
                    )
                    if field in payload
                },
            }
        ]
    if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= _MAX_PARALLEL_RUNS:
        raise ValueError("tasks must contain one or two delegated tasks")
    tasks: list[dict[str, object]] = []
    allowed_fields = {
        "agent",
        "version",
        "task",
        "expectedOutput",
        "acceptanceCriteria",
        "outputSchema",
        "modelProfile",
        "thinkingLevel",
        "budget",
        "access",
        "allowedTools",
        "piSkillsEnabled",
        "codexSkillsEnabled",
        "workspaceRoots",
    }
    for value in raw_tasks:
        if not isinstance(value, Mapping):
            raise ValueError("each delegated task must be an object")
        unsupported = set(value) - allowed_fields
        if unsupported:
            raise ValueError(
                f"unsupported delegated task field: {sorted(unsupported)[0]}"
            )
        task: dict[str, object] = {
            "agent": _bounded_text(value.get("agent"), maximum=40, required=True),
            "version": _bounded_text(
                value.get("version") or "1",
                maximum=12,
                required=True,
            ),
            "task": _bounded_task(value.get("task")),
            "expectedOutput": _delegation_expected_output(
                value.get("expectedOutput")
            ),
            "acceptanceCriteria": _delegation_acceptance_criteria(
                value.get("acceptanceCriteria")
            ),
        }
        output_schema = _delegation_output_schema(value.get("outputSchema"))
        if _output_schema_requested(output_schema):
            task["outputSchema"] = output_schema
        for field in (
            "modelProfile",
            "thinkingLevel",
            "access",
            "piSkillsEnabled",
            "codexSkillsEnabled",
        ):
            if field in value:
                task[field] = value.get(field)
        if "budget" in value:
            raw_budget = value.get("budget")
            if not isinstance(raw_budget, Mapping):
                raise ValueError("budget must be an object")
            task["budget"] = dict(raw_budget)
        if "allowedTools" in value:
            raw_tools = value.get("allowedTools")
            if not isinstance(raw_tools, list):
                raise ValueError("allowedTools must be an array")
            task["allowedTools"] = [str(tool).strip() for tool in raw_tools]
        if "workspaceRoots" in value:
            raw_roots = value.get("workspaceRoots")
            if not isinstance(raw_roots, list):
                raise ValueError("workspaceRoots must be an array")
            task["workspaceRoots"] = [str(root).strip() for root in raw_roots]
        validate_contract(task, _DELEGATION_TASK_CONTRACT)
        tasks.append(task)
    return tasks


def _delegation_budget(
    template_budget: AgentTemplateBudget,
    requested: object,
) -> AgentTemplateBudget:
    """Apply request-local caps without allowing a task to widen its template."""

    if requested is None:
        return template_budget
    if not isinstance(requested, Mapping):
        raise ValueError("budget must be an object")
    allowed = {"maxTotalTokens", "maxDurationMs", "maxOutputChars"}
    unsupported = set(requested) - allowed
    if unsupported:
        raise ValueError(f"unsupported delegation budget field: {sorted(unsupported)[0]}")
    if not requested:
        raise ValueError("budget must contain at least one limit")
    limits = {
        "maxTotalTokens": (template_budget.max_total_tokens, 256, 262_144),
        "maxDurationMs": (template_budget.max_duration_ms, 1_000, 900_000),
        "maxOutputChars": (template_budget.max_output_chars, 256, 100_000),
    }
    normalized: dict[str, int] = {}
    for field, value in requested.items():
        template_limit, minimum, maximum = limits[field]
        parsed = _bounded_int(value, minimum=minimum, maximum=maximum)
        if parsed > template_limit:
            raise ValueError(f"delegation budget cannot exceed template {field}")
        normalized[field] = parsed
    return replace(
        template_budget,
        max_total_tokens=normalized.get(
            "maxTotalTokens", template_budget.max_total_tokens
        ),
        max_duration_ms=normalized.get(
            "maxDurationMs", template_budget.max_duration_ms
        ),
        max_output_chars=normalized.get(
            "maxOutputChars", template_budget.max_output_chars
        ),
    )


def _validated_native_fork_sessions(
    runtime_context: object,
    *,
    parent_runtime_binding: Mapping[str, object] | None,
    session_dir: Path,
    agent_dir: Path,
    expected_count: int,
) -> list[dict[str, object]]:
    if not isinstance(runtime_context, Mapping):
        raise ValueError("fork context must be created by the active Pi runtime")
    if str(runtime_context.get("schemaVersion") or "") != "rag-ime.agent-runtime-context.v1":
        raise ValueError("fork runtime context version is unsupported")
    values = runtime_context.get("forkSessions")
    if not isinstance(values, list) or len(values) != expected_count:
        raise ValueError("fork runtime context does not match the delegated task count")

    root = session_dir.expanduser().resolve(strict=False)
    if not isinstance(parent_runtime_binding, Mapping):
        raise ValueError("fork context requires a persisted parent runtime binding")
    if (
        str(parent_runtime_binding.get("driverId") or "") != "managed-pi"
        or str(parent_runtime_binding.get("runtimeKind") or "") != "pi_rpc"
    ):
        raise ValueError("native Pi fork requires a managed Pi parent binding")

    expected_parent_value = str(parent_runtime_binding.get("transcriptRef") or "").strip()
    if not expected_parent_value:
        raise ValueError("fork context requires a persisted parent Pi session")
    expected_parent = Path(expected_parent_value).expanduser().resolve(strict=False)
    if not _managed_regular_file(expected_parent, root):
        raise ValueError("fork parent session is outside the managed session directory")
    parent_records = _read_session_records(expected_parent, label="fork parent session")
    parent_entry_ids = {
        str(record["id"])
        for record in parent_records
        if isinstance(record.get("id"), str) and record.get("id")
    }

    expected_cwd = agent_dir.expanduser().resolve(strict=False)
    seen_paths: set[Path] = set()
    seen_session_ids: set[str] = set()
    resolved: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise ValueError("fork runtime context contains an invalid session")
        parent_value = str(value.get("parentSessionFile") or "").strip()
        parent = Path(parent_value).expanduser().resolve(strict=False)
        if parent != expected_parent:
            raise ValueError("fork runtime context does not belong to the active parent session")
        parent_leaf_id = _bounded_text(value.get("parentLeafId"), maximum=128, required=True)
        if parent_leaf_id not in parent_entry_ids:
            raise ValueError("fork runtime context references an unknown parent branch leaf")

        session_file_value = str(value.get("sessionFile") or "").strip()
        session_file = Path(session_file_value).expanduser().resolve(strict=False)
        if session_file in seen_paths or not _managed_regular_file(session_file, root):
            raise ValueError("native fork session is duplicated or outside the managed directory")
        mode = session_file.stat().st_mode & 0o777
        if mode & 0o077:
            raise ValueError("native fork session permissions are broader than 0600")
        records = _read_session_records(session_file, label="native fork session")
        header = records[0]
        session_id = _bounded_text(value.get("sessionId"), maximum=128, required=True)
        if str(header.get("id") or "") != session_id:
            raise ValueError("native fork session id does not match its header")
        if session_id in seen_session_ids:
            raise ValueError("native fork session id is duplicated")
        header_parent = Path(str(header.get("parentSession") or "")).expanduser().resolve(
            strict=False
        )
        if header_parent != expected_parent:
            raise ValueError("native fork header does not reference the active parent session")
        header_cwd = Path(str(header.get("cwd") or "")).expanduser().resolve(strict=False)
        if header_cwd != expected_cwd:
            raise ValueError("native fork session has an unexpected runtime working directory")
        branch_entry_ids = {
            str(record["id"])
            for record in records
            if isinstance(record.get("id"), str) and record.get("id")
        }
        if parent_leaf_id not in branch_entry_ids:
            raise ValueError("native fork does not contain the requested parent branch")
        if any(_unsafe_fork_thinking(record) for record in records):
            raise ValueError("native fork still contains provider-bound thinking data")

        seen_paths.add(session_file)
        seen_session_ids.add(session_id)
        resolved.append(
            {
                "sessionId": session_id,
                "path": session_file,
                "parentLeafId": parent_leaf_id,
                "thinkingOverride": str(value.get("thinkingOverride") or ""),
            }
        )
    return resolved
def _room_permission_policy(value: object) -> dict[str, dict[str, str]] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("room permissionPolicy must be an object")
    expected_keys = {"schemaVersion", "room", "partner", "toolAgent"}
    unknown_keys = set(value) - expected_keys
    if unknown_keys:
        raise ValueError("room permissionPolicy contains an unknown field")
    if str(value.get("schemaVersion") or "") != "rag-ime.room-permission-policy.v1":
        raise ValueError("room permissionPolicy schemaVersion is invalid")
    expected_layers = ("room", "partner", "toolAgent")
    policy: dict[str, dict[str, str]] = {}
    for layer in expected_layers:
        layer_value = value.get(layer)
        if not isinstance(layer_value, Mapping):
            raise ValueError(f"room permissionPolicy.{layer} must be an object")
        unknown_fields = set(layer_value) - {"executionMode"}
        if unknown_fields:
            raise ValueError(f"room permissionPolicy.{layer} contains an unknown field")
        execution_mode = str(layer_value.get("executionMode") or "").strip()
        if execution_mode not in _ROOM_PERMISSION_MODES and not (
            layer in {"partner", "toolAgent"} and execution_mode == "inherit"
        ):
            raise ValueError(
                f"room permissionPolicy.{layer}.executionMode is invalid"
            )
        policy[layer] = {"executionMode": execution_mode}
    room_mode = policy["room"]["executionMode"]
    partner_mode = policy["partner"]["executionMode"]
    tool_agent_mode = policy["toolAgent"]["executionMode"]
    room_rank = _ROOM_PERMISSION_MODE_RANK[room_mode]
    partner_rank = (
        room_rank
        if partner_mode == "inherit"
        else _ROOM_PERMISSION_MODE_RANK[partner_mode]
    )
    tool_agent_rank = (
        partner_rank
        if tool_agent_mode == "inherit"
        else _ROOM_PERMISSION_MODE_RANK[tool_agent_mode]
    )
    if partner_rank > room_rank or tool_agent_rank > partner_rank:
        raise ValueError("room permissionPolicy cannot widen parent authority")
    return policy


def _effective_room_execution_mode(
    policy: Mapping[str, Mapping[str, str]] | None,
) -> str | None:
    if not policy:
        return None
    room_mode = str(policy["room"]["executionMode"])
    partner_mode = str(policy["partner"]["executionMode"])
    tool_agent_mode = str(policy["toolAgent"]["executionMode"])
    if partner_mode == "inherit":
        partner_mode = room_mode
    if tool_agent_mode == "inherit":
        tool_agent_mode = partner_mode
    return tool_agent_mode


def _room_policy_is_unrestricted(
    execution_mode: str | None,
) -> bool:
    return execution_mode in {
        PER_ACTION_EXECUTION_MODE,
        FULL_TRUST_EXECUTION_MODE,
    }


def _room_policy_access(
    requested_access: str,
    *,
    effective_execution_mode: str | None,
) -> tuple[str, str | None]:
    access = requested_access
    if access == "inherit":
        access = (
            "read_only"
            if effective_execution_mode == READ_ONLY_EXECUTION_MODE
            else "write"
            if effective_execution_mode is not None
            else "inherit"
        )
    if access == "write" and effective_execution_mode == READ_ONLY_EXECUTION_MODE:
        raise ValueError("the Room permission policy cannot grant write access")
    return access, effective_execution_mode




def _delegation_causal_metadata(
    value: Mapping[str, object] | None,
) -> dict[str, object]:
    source = value if isinstance(value, Mapping) else {}
    return {
        "todoId": _bounded_text(source.get("todoId"), maximum=240),
        "todoRevision": max(0, int(source.get("todoRevision") or 0)),
        "goalId": _bounded_text(source.get("goalId"), maximum=240),
        "goalRevision": max(0, int(source.get("goalRevision") or 0)),
        "roomBound": bool(source.get("roomBound")),
        "roomId": _bounded_text(source.get("roomId"), maximum=240),
        "rootId": _bounded_text(source.get("rootId"), maximum=240),
        "taskId": _bounded_text(source.get("taskId"), maximum=240),
        "dispatchId": _bounded_text(source.get("dispatchId"), maximum=240),
        "generation": max(0, int(source.get("generation") or 0)),
    }


def _managed_regular_file(path: Path, root: Path) -> bool:
    if not _is_within(path, root) or path.is_symlink() or not path.is_file():
        return False
    size = path.stat().st_size
    return 0 < size <= _MAX_FORK_BYTES


def _read_session_records(path: Path, *, label: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} has invalid JSONL at line {line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label} contains a non-object record")
        records.append(value)
    if not records or records[0].get("type") != "session":
        raise ValueError(f"{label} is missing its header")
    return records


def _unsafe_fork_thinking(record: Mapping[str, object]) -> bool:
    if str(record.get("type") or "") != "message":
        return False
    message = record.get("message")
    if not isinstance(message, Mapping) or str(message.get("role") or "") != "assistant":
        return False
    provider = str(message.get("provider") or "").lower()
    api = str(message.get("api") or "").lower()
    model = str(message.get("model") or "").lower()
    anthropic = provider == "anthropic" or api == "anthropic-messages" or model.startswith(
        "anthropic/"
    )
    content = message.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, Mapping):
            continue
        block_type = str(block.get("type") or "")
        if block_type == "redacted_thinking":
            return True
        if block_type != "thinking" or not anthropic:
            continue
        signature = block.get("thinkingSignature") or block.get("signature")
        if block.get("redacted") is True or isinstance(signature, str) and signature:
            return True
    return False


def _subagent_prompt(run: Mapping[str, object], batch: Mapping[str, object]) -> str:
    criteria = "\n".join(
        f"- {item}"
        for item in run.get("acceptanceCriteria", [])
        if isinstance(item, str)
    )
    output_schema = run.get("outputSchema")
    schema_section = (
        "\n\n输出 JSON Schema：\n"
        + json.dumps(
            output_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + (
            "\n完成后必须调用 structured_output 工具，并把最终值放在 value 字段；"
            "不要用普通文本代替。Schema 校验失败时修正 value 后再次调用。"
        )
        if _output_schema_requested(output_schema)
        else ""
    )
    room_handoff = ""
    causal_metadata = batch.get("causalMetadata")
    if isinstance(causal_metadata, Mapping) and bool(
        causal_metadata.get("roomBound")
    ):
        context_mode = str(batch.get("contextMode") or "fresh")
        context_guidance = (
            "fork 已验证 exact managed Pi transcript prefix；只在其后追加本有界 brief，"
            "不得重写或重排 system/model/tool 顺序，也不承诺 provider cache hit。"
            if context_mode == "fork"
            else (
                "fresh 表示独立上下文；独立复核保持 fresh、只读，不引入父会话私有 transcript。"
            )
        )
        room_handoff = (
            "\n\nRoom nested handoff：你是 parent Session 的私有、有界助手，"
            "不得发布 Room 进展、委派正式 Room Partner、直接打开原生 Ask，"
            "也不得代替 parent 给用户最终答复。缺少用户决定时把一个最小结构化 blocker "
            "和恢复条件交回 parent。\n"
            f"{context_guidance}\n"
            "workspace_lsp 仅按当前 tool profile：readonly 只用 status/symbols/hover/definition/"
            "references/diagnostics；worker 的 rename/code_action_apply 仍需现有 hash-bound "
            "approval，导出符号变更先用 references。"
        )
    room_suffix = f"{room_handoff}\n\n"
    if not room_handoff:
        room_suffix = "\n\n"

    return (
        "请完成下面这一项有界委派任务。只返回可交给主持会话使用的结果；"
        "不要把自己描述成长期群聊成员，也不要扩大工具或权限。"
        "最终输出必须区分已经观察到的事实与由此推断的结论，列出可核验的 claims、"
        "实际取得的工具回执或产物引用，以及仍未消除的不确定性；"
        "没有真实回执时不要虚构引用，也不要宣称父任务已经验收通过。\n\n"
        f"任务：\n{run['task']}\n\n"
        f"预期交付：\n{run['expectedOutput']}\n\n"
        f"验收条件：\n{criteria}"
        f"{schema_section}{room_suffix}"
        f"上下文模式：{batch['contextMode']}\n"
        f"委派深度：{batch['depth']}/{batch['maxDepth']}"
    )


def _message_summary(message: Mapping[str, object], maximum: int) -> str:
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping) or str(block.get("type") or "") not in {"text", "code"}:
            continue
        data = block.get("data")
        if not isinstance(data, Mapping):
            continue
        text = str(data.get("text") or data.get("code") or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)[:maximum]


def _subagent_failure_class(error: object) -> str:
    message = str(error or "").strip().lower()
    if not message:
        return "logic_error"
    if any(marker in message for marker in ("budget exceeded", "acceptance", "schema", "contract")):
        return "logic_error"
    if any(
        marker in message
        for marker in (
            "tool call",
            "tool failed",
            "tool error",
            "outside the authorized workspace",
            "does not exist in the authorized workspace",
            "path is outside",
            "permission denied",
        )
    ):
        return "tool_error"
    if any(
        marker in message
        for marker in (
            "connection refused",
            "connection reset",
            "network is unreachable",
            "econnrefused",
            "econnreset",
            "socket hang up",
            "broken pipe",
            "failed to start",
            "failed to spawn",
            "process exited",
            "runtime process",
            "runtime unavailable",
            "transport closed",
        )
    ):
        return "transient_runtime"
    return "logic_error"


def _run_contract_invalid(run: Mapping[str, object]) -> bool:
    contract = run.get("contract")
    result = run.get("result")
    return (
        isinstance(contract, Mapping)
        and str(contract.get("status") or "") == "invalid"
    ) or (
        isinstance(result, Mapping)
        and str(result.get("contractStatus") or "") == "invalid"
    )


def _safe_runtime_artifact_payload(event: AgentEventEnvelope) -> dict[str, object]:
    if event.event_type == "text_delta":
        return {"characterCount": len(str(event.payload.get("delta") or ""))}
    if event.event_type == "tool_started":
        return {
            "toolName": str(event.payload.get("toolName") or "")[:120],
            "toolCallId": str(
                event.payload.get("toolCallId") or event.payload.get("callId") or ""
            )[:160],
        }
    if event.event_type == "message_completed":
        message = event.payload.get("message")
        usage = event.payload.get("usage")
        return {
            "messageId": str(message.get("id") or "")[:160]
            if isinstance(message, Mapping)
            else "",
            "role": str(message.get("role") or "")[:40]
            if isinstance(message, Mapping)
            else "",
            "usage": dict(usage) if isinstance(usage, Mapping) else {},
        }
    if event.event_type == "user_input_required":
        return {
            key: event.payload[key]
            for key in (
                "requestId",
                "requestKind",
                "method",
                "title",
                "message",
                "options",
                "placeholder",
                "prefill",
                "defaultValue",
                "timeout",
                "runId",
            )
            if key in event.payload
        }
    if event.event_type in {"turn_completed", "turn_failed"}:
        default_state = "completed" if event.event_type == "turn_completed" else "failed"
        state = _bounded_text(
            event.payload.get("state") or event.payload.get("status") or default_state,
            maximum=80,
        )
        payload: dict[str, object] = {
            "status": _bounded_text(
                event.payload.get("status") or state,
                maximum=80,
            ),
            "state": state,
            "error": _bounded_text(event.payload.get("error"), maximum=240),
        }
        for key in ("toolName", "toolCallId", "callId", "toolId"):
            if key in event.payload:
                payload[key] = _bounded_text(event.payload.get(key), maximum=160)
        tool = event.payload.get("tool")
        if isinstance(tool, Mapping):
            payload["tool"] = {
                key: _bounded_text(tool.get(key), maximum=160)
                for key in ("name", "id", "toolName", "toolCallId", "callId")
                if key in tool
            }
        return payload
    return {}


def _json_mapping(value: object) -> dict[str, object]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _output_schema_requested(value: object) -> bool:
    return isinstance(value, Mapping) and bool(value)


def _stored_output_schema(value: object) -> tuple[dict[str, object] | bool | None, str]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        return None, (
            "delivery contract validation failed: persisted output schema is malformed "
            f"({exc.msg})"
        )
    if payload == {}:
        return None, ""
    try:
        schema = _delegation_output_schema(payload)
    except ValueError as exc:
        return None, (
            "delivery contract validation failed: persisted output schema is malformed "
            f"({exc})"
        )
    return schema, ""


def _json_value(value: object) -> object:
    try:
        return json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}


def _control_payload(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "runId": str(row["run_id"]),
        "clientActionId": str(row["client_action_id"]),
        "action": str(row["action"]),
        "state": str(row["state"]),
        "payload": _json_mapping(row["payload_json"]),
        "result": _json_mapping(row["result_json"]),
        "error": str(row["error"] or ""),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "completedAtMs": (
            int(row["completed_at_ms"]) if row["completed_at_ms"] is not None else None
        ),
    }


def _inbox_payload(row: sqlite3.Row) -> dict[str, object]:
    envelope = _json_mapping(row["response_json"])
    request = envelope.get("request")
    response = envelope.get("response")
    if not isinstance(request, Mapping):
        request = {}
    if not isinstance(response, Mapping):
        response = envelope if "request" not in envelope else {}
    return {
        "id": str(row["id"]),
        "runId": str(row["run_id"]),
        "requestId": str(row["request_id"]),
        "kind": str(row["kind"]),
        "title": str(row["title"] or ""),
        "message": str(row["message"] or ""),
        "status": str(row["status"]),
        "turnId": str(row["turn_id"] or ""),
        "request": dict(request),
        "response": dict(response),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "resolvedAtMs": (
            int(row["resolved_at_ms"]) if row["resolved_at_ms"] is not None else None
        ),
    }


def _assert_batch_owner(batch: Mapping[str, object], parent_session_id: str) -> None:
    if str(batch.get("parentSessionId") or "") != parent_session_id:
        raise ValueError("delegated run does not belong to this session")


def _batch_payload(row: sqlite3.Row, runs: Sequence[sqlite3.Row]) -> dict[str, object]:
    run_payloads = [_run_payload(item) for item in runs]
    payload: dict[str, object] = {
        "schemaVersion": "rag-ime.agent-subagent-batch.v1",
        "id": str(row["id"]),
        "parentSessionId": str(row["parent_session_id"]),
        "parentRunId": str(row["parent_run_id"] or ""),
        "contextMode": str(row["context_mode"]),
        "resultDeliveryMode": str(row["result_delivery_mode"]),
        "state": str(row["state"]),
        "depth": int(row["depth"]),
        "maxDepth": int(row["max_depth"]),
        "abortRequested": bool(row["abort_requested"]),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "completedAtMs": (
            int(row["completed_at_ms"])
            if row["completed_at_ms"] is not None
            else None
        ),
        "runs": run_payloads,
        "causalMetadata": {
            "todoId": str(row["causal_todo_id"] or ""),
            "todoRevision": int(row["causal_todo_revision"] or 0),
            "goalId": str(row["causal_goal_id"] or ""),
            "goalRevision": int(row["causal_goal_revision"] or 0),
            "roomBound": bool(row["room_bound"]),
            "roomId": str(row["causal_room_id"] or ""),
            "rootId": str(row["causal_root_id"] or ""),
            "taskId": str(row["causal_task_id"] or ""),
            "dispatchId": str(row["causal_dispatch_id"] or ""),
            "generation": int(row["causal_generation"] or 0),
        },
    }
    for run in run_payloads:
        validate_contract(run, "agent-subagent-run.v1.json")
    validate_contract(payload, "agent-subagent-batch.v1.json")
    return payload


def _run_payload(row: sqlite3.Row) -> dict[str, object]:
    result = json.loads(str(row["result_json"] or "{}"))
    output_schema, schema_error = _stored_output_schema(row["output_schema_json"])
    launch_digest = _json_mapping(row["launch_digest_json"])
    if not launch_digest:
        launch_digest = _delegation_launch_digest(
            {},
            context_mode="fresh",
            template_id=str(row["template_id"]),
            template_version=str(row["template_version"]),
            output_schema=output_schema if output_schema is not None else {},
        )
    structured_output = _json_value(row["structured_output_json"])
    contract_call_id = str(row["structured_output_tool_call_id"] or "")
    contract_error = schema_error or str(row["structured_output_error"] or "")
    if schema_error:
        contract_status = "invalid"
    elif output_schema is None:
        contract_status = "not_requested"
    elif contract_call_id:
        contract_status = "valid"
    elif contract_error:
        contract_status = "invalid"
    else:
        contract_status = "pending"
    run_id = str(row["id"])
    parent_run_id = str(row["parent_run_id"] or "")
    payload: dict[str, object] = {
        "schemaVersion": "rag-ime.agent-subagent-run.v1",
        "id": run_id,
        "nodeId": str(row["logical_node_id"] or run_id),
        "attemptId": str(row["attempt_id"] or f"{run_id}:attempt:1"),
        "attemptNumber": max(1, int(row["attempt_number"] or 1)),
        "predecessorAttemptId": str(row["predecessor_attempt_id"] or ""),
        "ownerRunId": str(
            row["owner_run_id"]
            or parent_run_id
            or f"batch:{row['batch_id']}"
        ),
        "parentRunId": parent_run_id,
        "depth": max(1, min(2, int(row["depth"] or 1))),
        "batchId": str(row["batch_id"]),
        "childSessionId": str(row["child_session_id"]),
        "todoTask": str(row["todo_task"] or ""),
        "todoPhase": str(row["todo_phase"] or ""),
        "templateId": str(row["template_id"]),
        "templateVersion": str(row["template_version"]),
        "ordinal": int(row["ordinal"]),
        "task": str(row["task_text"]),
        "expectedOutput": str(row["expected_output"]),
        "acceptanceCriteria": [
            str(item)
            for item in json.loads(str(row["acceptance_criteria_json"] or "[]"))
            if isinstance(item, str)
        ],
        "launchDigest": launch_digest,
        "contract": {
            "status": contract_status,
            "error": contract_error,
            "toolCallId": contract_call_id,
            "validatedAtMs": (
                int(row["structured_output_validated_at_ms"])
                if row["structured_output_validated_at_ms"] is not None
                else None
            ),
        },
        "state": str(row["state"]),
        "budget": {
            "maxTurns": int(row["max_turns"]),
            "maxToolCalls": int(row["max_tool_calls"]),
            "maxTotalTokens": int(row["max_total_tokens"]),
            "maxDurationMs": int(row["max_duration_ms"]),
            "maxOutputChars": int(row["max_output_chars"]),
        },
        "usage": {
            "turnCount": int(row["turn_count"]),
            "toolCount": int(row["tool_count"]),
            "totalTokens": int(row["total_tokens"]),
        },
        "result": result if isinstance(result, dict) else {},
        "error": str(row["error"] or ""),
        "resultContextScheduledAtMs": (
            int(row["result_context_scheduled_at_ms"])
            if row["result_context_scheduled_at_ms"] is not None
            else None
        ),
        "createdAtMs": int(row["created_at_ms"]),
        "startedAtMs": (
            int(row["started_at_ms"])
            if row["started_at_ms"] is not None
            else None
        ),
        "updatedAtMs": int(row["updated_at_ms"]),
        "completedAtMs": (
            int(row["completed_at_ms"])
            if row["completed_at_ms"] is not None
            else None
        ),
    }
    if output_schema is not None:
        payload["outputSchema"] = output_schema
    if contract_call_id:
        payload["structuredOutput"] = structured_output
    validate_contract(payload, "agent-subagent-run.v1.json")
    return payload

def _required_text(value: Mapping[str, object], key: str) -> str:
    return _bounded_text(value.get(key), maximum=240, required=True)


def _bounded_text(value: object, *, maximum: int, required: bool = False) -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise ValueError("required delegation text is missing")
    return text[:maximum]

def _bounded_task(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("delegated task must not be empty")
    return text[:8_000]

def _delegation_expected_output(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("delegated expectedOutput must be a string")
    text = " ".join(value.split())
    if not text:
        raise ValueError("delegated expectedOutput must not be empty")
    if len(text) > 2_000:
        raise ValueError("delegated expectedOutput exceeds 2000 characters")
    return text


def _delegation_acceptance_criteria(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("delegated acceptanceCriteria must contain one to eight items")
    criteria: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("delegated acceptanceCriteria items must be strings")
        text = " ".join(item.split())
        if not text:
            raise ValueError("delegated acceptanceCriteria items must not be empty")
        if len(text) > 1_000:
            raise ValueError(
                "delegated acceptanceCriteria item exceeds 1000 characters"
            )
        criteria.append(text)
    if len(criteria) != len(set(criteria)):
        raise ValueError("delegated acceptanceCriteria items must be unique")
    return criteria


def _delegation_output_schema(value: object) -> dict[str, object] | bool:
    if value is None:
        return {}
    if not isinstance(value, (Mapping, bool)):
        raise ValueError("delegated outputSchema must be a JSON Schema object or boolean")
    schema = value if isinstance(value, bool) else dict(value)
    if isinstance(schema, Mapping) and len(schema) > 128:
        raise ValueError("delegated outputSchema has too many top-level properties")
    try:
        encoded = json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("delegated outputSchema must contain only JSON values") from exc
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise ValueError("delegated outputSchema exceeds 16 KiB")
    validate_json_schema(
        schema,
        path="delegated outputSchema",
        maximum_depth=32,
        maximum_nodes=512,
        allow_boolean_root=False,
    )
    return schema


def _delegation_launch_digest(
    value: object,
    *,
    context_mode: str,
    template_id: str,
    template_version: str,
    output_schema: object,
) -> dict[str, object]:
    source = dict(value) if isinstance(value, Mapping) else {}
    schema = _delegation_output_schema(output_schema)
    encoded_schema = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    tools = list(
        dict.fromkeys(
            _bounded_text(item, maximum=128, required=True)
            for item in source.get("tools", [])
            if isinstance(item, str) and item.strip()
        )
    )[:256]
    access = str(source.get("workspaceAccess") or "none")
    if access not in {"none", "read_only", "write"}:
        access = "none"
    allowlist_mode = str(source.get("toolAllowlistMode") or "profile")
    if allowlist_mode not in {"profile", "explicit"}:
        allowlist_mode = "profile"
    digest = {
        "schemaVersion": "rag-ime.agent-subagent-launch-digest.v1",
        "contextMode": context_mode,
        "templateId": template_id,
        "templateVersion": template_version,
        "modelProfile": _bounded_text(
            source.get("modelProfile") or "pi/default",
            maximum=240,
            required=True,
        ),
        "thinkingLevel": _bounded_text(
            source.get("thinkingLevel"),
            maximum=24,
        ),
        "toolProfileVersion": _bounded_text(
            source.get("toolProfileVersion") or "subagent-readonly-v1",
            maximum=120,
            required=True,
        ),
        "toolAllowlistMode": allowlist_mode,
        "tools": tools,
        "piSkillsEnabled": bool(source.get("piSkillsEnabled", False)),
        "codexSkillsEnabled": bool(source.get("codexSkillsEnabled", False)),
        "workspaceAccess": access,
        "workspaceRootCount": _bounded_int(
            source.get("workspaceRootCount") or 0,
            minimum=0,
            maximum=4,
        ),
        "outputContract": {
            "required": _output_schema_requested(schema),
            "schemaSha256": (
                hashlib.sha256(encoded_schema).hexdigest()
                if _output_schema_requested(schema)
                else ""
            ),
        },
        "extensionRuntime": "pi_host_managed",
    }
    return digest


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("delegation budget value is invalid") from exc
    if not minimum <= number <= maximum:
        raise ValueError("delegation budget value is outside the managed range")
    return number


def _nonnegative_event_int(value: object) -> int:
    try:
        return max(0, min(int(value or 0), 10_000))
    except (TypeError, ValueError):
        return 0


def _subagent_session_retention_from_environment() -> int:
    return (
        _bounded_environment_int(
            "RAG_IME_SUBAGENT_SESSION_RETENTION_HOURS",
            default=72,
            minimum=1,
            maximum=30 * 24,
        )
        * 60
        * 60
        * 1_000
    )


def _subagent_session_gc_interval_from_environment() -> int:
    return (
        _bounded_environment_int(
            "RAG_IME_SUBAGENT_SESSION_GC_INTERVAL_SECONDS",
            default=15 * 60,
            minimum=60,
            maximum=24 * 60 * 60,
        )
        * 1_000
    )


def _bounded_environment_int(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _timestamp(value: int | None) -> int:
    return int(value if value is not None else time.time() * 1000)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
