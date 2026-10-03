from __future__ import annotations

import codecs
import hashlib
import json
import os
import selectors
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .agent_execution_policy import unrestricted_workspace_policy_active
from .agent_background_ownership import BackgroundJobLeaseLost, BackgroundJobOwnership
from .agent_sessions import AgentSessionStore
from .agent_workspace import (
    PreparedWorkspaceCommand,
    SpawnedWorkspaceCommand,
    WorkspaceHarness,
)
from .contracts.json_schema import validate_contract
from .db import apply_database_migrations, sqlite_connection
from .room_runtime_host_kill_gate import _process_identity, _signal_process_group


class AgentBackgroundJobError(RuntimeError):
    pass


class AgentBackgroundJobConflict(AgentBackgroundJobError):
    """An idempotency key was reused for a different execution request."""


EventPublisher = Callable[..., object]
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "orphaned"})
_ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})
_MAX_ACTIVE_JOBS_PER_SESSION = 8
_MAX_LOG_BYTES = 4 * 1024 * 1024
_LOG_TRIM_TRIGGER_BYTES = _MAX_LOG_BYTES + 512 * 1024
_MAX_LOG_READ_BYTES = 128 * 1024
_PROGRESS_INTERVAL_SECONDS = 0.75
_DETACHED_PROCESS_LOCK = threading.Lock()
_DETACHED_PROCESSES: dict[int, SpawnedWorkspaceCommand] = {}


@dataclass
class _LiveJob:
    launched: SpawnedWorkspaceCommand | object | None
    log_path: Path
    max_run_seconds: int
    job_id: str = ""
    session_id: str = ""
    raw_output_path: Path | None = None
    exit_status_path: Path | None = None
    temporary_path: Path | None = None
    launch_receipt_path: Path | None = None
    launch_release_path: Path | None = None
    pid: int = 0
    process_group_id: int = 0
    process_birth_token: str = ""
    started_at_ms: int = 0
    raw_output_cursor: int = 0
    detach_requested: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    output_bytes: int = 0
    log_start_cursor: int = 0
    log_truncated: bool = False
    last_progress_at: float = 0.0
    last_persisted_at: float = 0.0


class AgentBackgroundJobService:
    """Authoritative, session-scoped owner for governed background commands."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        events: EventPublisher,
        workspace_harness: WorkspaceHarness | None = None,
        log_root: str | Path | None = None,
        execution_owner: bool = True,
        sessions: AgentSessionStore | None = None,
        lease_seconds: float = 10.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.events = events
        self.workspace_harness = workspace_harness or WorkspaceHarness()
        self.sessions = sessions or AgentSessionStore(self.db_path)
        self.log_root = Path(log_root) if log_root is not None else self.db_path.parent / "BackgroundJobs"
        self.execution_owner = bool(execution_owner)
        self._lock = threading.RLock()
        self._live: dict[str, _LiveJob] = {}
        self._closed = False
        self._ownership = BackgroundJobOwnership(self.db_path, lease_seconds=lease_seconds)
        self._recovery_stop = threading.Event()
        self._recovery_thread: threading.Thread | None = None
        self._recovery_lock = threading.Lock()

    def initialize(self) -> None:
        self._ensure_log_root()
        with sqlite_connection(
            self.db_path,
            row_factory=sqlite3.Row,
            foreign_keys=True,
        ) as conn:
            apply_database_migrations(conn)
            active_rows = (
                conn.execute(
                    """
                    SELECT *
                    FROM agent_background_jobs
                    WHERE status IN ('queued', 'running', 'cancelling')
                    ORDER BY created_at_ms, job_id
                    """
                ).fetchall()
                if self.execution_owner
                else []
            )
        with self._recovery_lock:
            for row in active_rows:
                self._claim_and_reattach(row)
            if self.execution_owner and self._recovery_thread is None and not self._closed:
                self._recovery_thread = threading.Thread(
                    target=self._recover_loop, name="rag-ime-background-recovery", daemon=True,
                )
                self._recovery_thread.start()

    def _claim_and_reattach(self, row: Mapping[str, object]) -> None:
        if self._closed or not self._ownership.claim(str(row["job_id"])):
            return
        try:
            self._reattach_persisted_job(self._row_for_job(str(row["job_id"])))
        except BackgroundJobLeaseLost:
            return

    def _recover_loop(self) -> None:
        while not self._recovery_stop.wait(0.5):
            try:
                with self._recovery_lock:
                    with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
                        rows = conn.execute(
                            "SELECT * FROM agent_background_jobs WHERE status IN ('queued','running','cancelling')"
                        ).fetchall()
                    for row in rows:
                        self._claim_and_reattach(row)
            except (sqlite3.Error, OSError):
                # A transient storage failure does not permit an unfenced
                # write or a replacement execution. Retry discovery later.
                continue

    def _reattach_persisted_job(self, row: Mapping[str, object]) -> None:
        job_id = str(row["job_id"])
        row = self._restore_launch_identity(row)
        if str(row["status"]) in _TERMINAL_STATUSES:
            return
        pid = int(row["pid"]) if row["pid"] is not None else 0
        process_group_id = (
            int(row["process_group_id"])
            if row["process_group_id"] is not None
            else 0
        )
        birth_token = str(row["process_birth_token"] or "")
        expected = (process_group_id, birth_token)
        observed = _process_identity(pid) if pid > 0 else None
        raw_output_path = self._stored_runtime_path(
            str(row["raw_output_path"] or ""),
            job_id,
            suffix="raw",
        )
        exit_status_path = self._stored_runtime_path(
            str(row["exit_status_path"] or ""),
            job_id,
            suffix="exit",
        )
        temporary_path = self._stored_runtime_path(
            str(row["temporary_path"] or ""),
            job_id,
            suffix="tmp",
        )
        resumable = raw_output_path is not None and exit_status_path is not None
        exit_ready = bool(
            exit_status_path is not None
            and exit_status_path.exists()
            and exit_status_path.stat().st_size > 0
        )
        identity_matches = (
            pid > 0
            and process_group_id > 0
            and bool(birth_token)
            and process_group_id != os.getpgrp()
            and observed == expected
        )
        if not identity_matches and not exit_ready:
            self._finalize_without_process(
                job_id,
                status="orphaned",
                error=(
                    "后台任务进程身份已失效，未向可能复用的进程发送信号"
                ),
            )
            return
        log_path = self._safe_stored_log_path(
            str(row["log_path"] or ""),
            job_id,
        )
        with _DETACHED_PROCESS_LOCK:
            detached_launch = _DETACHED_PROCESSES.pop(pid, None)
        live = _LiveJob(
            launched=detached_launch,
            job_id=job_id,
            log_path=log_path,
            max_run_seconds=int(row["max_run_seconds"]),
            session_id=str(row["session_id"] or ""),
            raw_output_path=raw_output_path if resumable else None,
            exit_status_path=exit_status_path if resumable else None,
            temporary_path=temporary_path,
            launch_receipt_path=self.log_root / f"{job_id}.launch",
            launch_release_path=self.log_root / f"{job_id}.release",
            pid=pid,
            process_group_id=process_group_id,
            process_birth_token=birth_token,
            started_at_ms=int(row["started_at_ms"] or row["created_at_ms"]),
            raw_output_cursor=int(row["raw_output_cursor"] or 0),
            output_bytes=int(row["output_bytes"] or 0),
            log_start_cursor=int(row["log_start_cursor"] or 0),
            log_truncated=bool(row["log_truncated"]),
        )
        with self._lock:
            self._live[job_id] = live
        if identity_matches and str(row["status"]) == "cancelling":
            self._terminate_live(live)
        elif identity_matches and live.launch_receipt_path.exists():
            try:
                self._release_launch(job_id)
            except BackgroundJobLeaseLost:
                with self._lock:
                    self._live.pop(job_id, None)
                return
            except Exception as exc:
                self._terminate_live(live)
                self._finalize_without_process(
                    job_id,
                    status="cancelled" if isinstance(exc, AgentBackgroundJobError) else "failed",
                    error=_public_error(exc),
                )
                self._cleanup_runtime_files(live)
                with self._lock:
                    self._live.pop(job_id, None)
                return
        monitor = threading.Thread(
            target=self._monitor,
            args=(job_id,),
            name=f"rag-ime-background-job-{job_id[-8:]}",
            daemon=True,
        )
        live.thread = monitor
        monitor.start()

    def start(
        self,
        session_id: str,
        prepared: PreparedWorkspaceCommand,
        *,
        label: object = "",
        approval_id: str = "",
        causal_metadata: Mapping[str, object] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, object]:
        self._require_execution_owner()
        with self._lock:
            if self._closed:
                raise AgentBackgroundJobError("background job service is closed")
        session = _required_text(session_id, field="sessionId", maximum=240)
        normalized_label = _job_label(label, prepared.command)
        normalized_idempotency_key = _idempotency_key(idempotency_key)
        idempotency_digest = (
            _background_start_digest(prepared)
            if normalized_idempotency_key
            else ""
        )
        job_id = f"bg_{uuid.uuid4().hex}"
        log_path = self._log_path(job_id)
        raw_output_path = self._raw_output_path(job_id)
        exit_status_path = self._exit_status_path(job_id)
        temporary_path = self._temporary_path(job_id)
        now_ms = _now_ms()
        command_sha256 = hashlib.sha256(prepared.command.encode("utf-8")).hexdigest()
        replay_receipt: dict[str, object] | None = None
        try:
            with sqlite_connection(
                self.db_path,
                row_factory=sqlite3.Row,
                foreign_keys=True,
            ) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if normalized_idempotency_key:
                    existing = conn.execute(
                        """
                        SELECT *
                        FROM agent_background_jobs
                        WHERE session_id = ? AND idempotency_key = ?
                        """,
                        (session, normalized_idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        if str(existing["idempotency_digest"] or "") != idempotency_digest:
                            raise AgentBackgroundJobConflict(
                                "background job idempotency key is already bound "
                                "to different execution content"
                            )
                        replay_job = self._job_payload(existing)
                        replay_receipt = {
                            "schemaVersion": (
                                "rag-ime.agent-background-job-start-receipt.v1"
                            ),
                            "ok": True,
                            "summary": (
                                f"后台任务《{replay_job['label']}》已存在，返回原任务"
                            ),
                            "replayed": True,
                            "job": replay_job,
                            "launchReceipt": {
                                "jobId": str(replay_job["jobId"]),
                                "approvalId": str(replay_job["approvalId"]),
                                "commandSha256": str(replay_job["commandSha256"]),
                                "startedAtMs": int(replay_job["startedAtMs"]),
                            },
                        }
                    else:
                        replay_receipt = None
                if replay_receipt is None:
                    causal = _job_causal_metadata(
                        conn,
                        session_id=session,
                        approval_id=str(approval_id or ""),
                        supplied=causal_metadata,
                    )
                    self._require_capacity(conn, session)
                    self._require_live_causal_epoch(conn, session, causal)
                    log_path.touch(mode=0o600, exist_ok=False)
                    os.chmod(log_path, 0o600)
                    conn.execute(
                        """
                        INSERT INTO agent_background_jobs(
                            job_id,
                            session_id,
                            label,
                            status,
                            command,
                            command_sha256,
                            cwd,
                            network_allowed,
                            max_run_seconds,
                            log_path,
                            raw_output_path,
                            exit_status_path,
                            temporary_path,
                            approval_id,
                            idempotency_key,
                            idempotency_digest,
                            causal_todo_id,
                            causal_todo_revision,
                            causal_goal_id,
                            causal_goal_revision,
                            causal_turn_id,
                            room_bound,
                            causal_room_id,
                            causal_root_id,
                            causal_generation,
                            causal_task_id,
                            causal_dispatch_id,
                            created_at_ms,
                            updated_at_ms
                        ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            session,
                            normalized_label,
                            prepared.command,
                            command_sha256,
                            str(prepared.cwd),
                            int(prepared.allow_network),
                            prepared.timeout_seconds,
                            str(log_path),
                            str(raw_output_path),
                            str(exit_status_path),
                            str(temporary_path),
                            str(approval_id or "")[:240],
                            normalized_idempotency_key,
                            idempotency_digest,
                            causal["todoId"],
                            causal["todoRevision"],
                            causal["goalId"],
                            causal["goalRevision"],
                            causal["turnId"],
                            int(bool(causal["roomBound"])),
                            causal["roomId"],
                            causal["rootId"],
                            causal["generation"],
                            causal["taskId"],
                            causal["dispatchId"],
                            now_ms,
                            now_ms,
                        ),
                    )
                    self._ownership.admit(conn, job_id)
        except Exception:
            log_path.unlink(missing_ok=True)
            raise
        if replay_receipt is not None:
            return replay_receipt
        try:
            with self._ownership.guard(job_id):
                pass
            # Process creation must not hold the shared database write lock.
            # The launcher cannot execute user code until the fenced release.
            launched = self.workspace_harness.spawn_background(
                prepared,
                output_path=raw_output_path,
                exit_status_path=exit_status_path,
                temporary_path=temporary_path,
                launch_receipt_path=self.log_root / f"{job_id}.launch",
                launch_release_path=self.log_root / f"{job_id}.release",
            )
        except Exception as exc:
            error = _public_error(exc)
            self._mark_launch_failed(job_id, error)
            job = self.status(session, job_id)["job"]
            try:
                self._publish("background_job_failed", job)
            except Exception:
                pass
            raise AgentBackgroundJobError(error) from exc

        live = _LiveJob(
            launched=launched,
            job_id=job_id,
            log_path=log_path,
            session_id=session,
            max_run_seconds=prepared.timeout_seconds,
            raw_output_path=raw_output_path,
            exit_status_path=exit_status_path,
            temporary_path=temporary_path,
            launch_receipt_path=self.log_root / f"{job_id}.launch",
            launch_release_path=self.log_root / f"{job_id}.release",
            pid=launched.process.pid,
        )
        identity = _process_identity(launched.process.pid)
        if identity is None:
            self._abort_launched_job(
                job_id,
                live,
                error="background job process identity is unavailable",
            )
            raise AgentBackgroundJobError(
                "background job process identity is unavailable"
            )
        process_group_id, process_birth_token = identity
        live.process_group_id = process_group_id
        live.process_birth_token = process_birth_token
        try:
            with self._lock:
                if self._closed:
                    raise AgentBackgroundJobError("background job service is closing")
                self._live[job_id] = live
            started_at_ms = _now_ms()
            live.started_at_ms = started_at_ms
            with self._ownership.guard(job_id, renew_expired=True) as conn:
                self._require_live_causal_epoch(conn, session, causal)
                cursor = conn.execute(
                    """
                    UPDATE agent_background_jobs
                    SET status = 'running',
                        pid = ?,
                        process_group_id = ?,
                        process_birth_token = ?,
                        started_at_ms = ?,
                        updated_at_ms = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (
                        launched.process.pid,
                        process_group_id,
                        process_birth_token,
                        started_at_ms,
                        started_at_ms,
                        job_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AgentBackgroundJobError(
                        "background job admission was cancelled before launch"
                    )
            self._release_launch(job_id)
            job = self.status(session, job_id)["job"]
            self._publish("background_job_started", job)
            monitor = threading.Thread(
                target=self._monitor,
                args=(job_id,),
                name=f"rag-ime-background-job-{job_id[-8:]}",
                daemon=True,
            )
            live.thread = monitor
            monitor.start()
        except Exception as exc:
            error = _public_error(exc)
            self._abort_launched_job(job_id, live, error=error)
            try:
                job = self.status(session, job_id)["job"]
                self._publish("background_job_failed", job)
            except Exception:
                pass
            if isinstance(exc, AgentBackgroundJobError):
                raise
            raise AgentBackgroundJobError(error) from exc
        return {
            "schemaVersion": "rag-ime.agent-background-job-start-receipt.v1",
            "ok": True,
            "summary": f"后台任务《{normalized_label}》已启动",
            "job": job,
            "launchReceipt": {
                "jobId": job_id,
                "approvalId": str(approval_id or "")[:240],
                "commandSha256": command_sha256,
                "startedAtMs": started_at_ms,
            },
        }

    def list(
        self,
        session_id: str,
        *,
        limit: object = 50,
        status: object = "",
    ) -> dict[str, object]:
        session = _required_text(session_id, field="sessionId", maximum=240)
        normalized_limit = _bounded_int(limit, default=50, minimum=1, maximum=100)
        normalized_status = str(status or "").strip().lower()
        if normalized_status and normalized_status not in _ACTIVE_STATUSES | _TERMINAL_STATUSES:
            raise AgentBackgroundJobError("unsupported background job status")
        where_status = " AND status = ?" if normalized_status else ""
        values: tuple[object, ...] = (
            (session, normalized_status, normalized_limit)
            if normalized_status
            else (session, normalized_limit)
        )
        with sqlite_connection(
            self.db_path,
            row_factory=sqlite3.Row,
            foreign_keys=True,
        ) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM agent_background_jobs
                WHERE session_id = ?{where_status}
                ORDER BY
                    CASE status
                        WHEN 'running' THEN 0
                        WHEN 'cancelling' THEN 1
                        WHEN 'queued' THEN 2
                        ELSE 3
                    END,
                    updated_at_ms DESC,
                    created_at_ms DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        items = [self._job_payload(row) for row in rows]
        return {
            "schemaVersion": "rag-ime.agent-background-job-list.v1",
            "ok": True,
            "sessionId": session,
            "items": items,
            "activeCount": sum(item["status"] in _ACTIVE_STATUSES for item in items),
        }

    def status(self, session_id: str, job_id: str) -> dict[str, object]:
        session = _required_text(session_id, field="sessionId", maximum=240)
        normalized_job_id = _job_id(job_id)
        row = self._row(session, normalized_job_id)
        return {
            "schemaVersion": "rag-ime.agent-background-job-status.v1",
            "ok": True,
            "job": self._job_payload(row),
        }

    def logs(
        self,
        session_id: str,
        job_id: str,
        *,
        cursor: object = 0,
        limit_bytes: object = 65_536,
    ) -> dict[str, object]:
        session = _required_text(session_id, field="sessionId", maximum=240)
        normalized_job_id = _job_id(job_id)
        row = self._row(session, normalized_job_id)
        requested_cursor = _bounded_int(cursor, default=0, minimum=0, maximum=2**63 - 1)
        normalized_limit = _bounded_int(
            limit_bytes,
            default=65_536,
            minimum=1,
            maximum=_MAX_LOG_READ_BYTES,
        )
        with self._lock:
            live = self._live.get(normalized_job_id)
            if live is None:
                row = self._row(session, normalized_job_id)
                start_cursor = int(row["log_start_cursor"] or 0)
            else:
                start_cursor = live.log_start_cursor
            path = self._safe_stored_log_path(
                str(row["log_path"] or ""),
                normalized_job_id,
            )
            actual_cursor = max(requested_cursor, start_cursor)
            byte_offset = max(0, actual_cursor - start_cursor)
            try:
                with path.open("rb") as handle:
                    handle.seek(byte_offset)
                    skipped_bytes = 0
                    first = handle.read(1)
                    while first and first[0] & 0xC0 == 0x80:
                        skipped_bytes += 1
                        first = handle.read(1)
                    actual_cursor += skipped_bytes
                    if first:
                        data = first + handle.read(normalized_limit - 1)
                        while True:
                            try:
                                decoded_text = data.decode("utf-8")
                                break
                            except UnicodeDecodeError as exc:
                                if exc.reason != "unexpected end of data":
                                    raise
                                continuation = handle.read(1)
                                if not continuation:
                                    raise
                                data += continuation
                    else:
                        data = b""
                        decoded_text = ""
                    has_more = bool(handle.read(1))
            except FileNotFoundError:
                data = b""
                decoded_text = ""
                has_more = False
        if not self._session_allows_raw_output(session):
            decoded_text = self.workspace_harness.redact_output(decoded_text)
        next_cursor = actual_cursor + len(data)
        return {
            "schemaVersion": "rag-ime.agent-background-job-log.v1",
            "ok": True,
            "jobId": normalized_job_id,
            "sessionId": session,
            "cursor": actual_cursor,
            "nextCursor": next_cursor,
            "logStartCursor": start_cursor,
            "truncatedBeforeCursor": requested_cursor < start_cursor,
            "hasMore": has_more,
            "text": decoded_text,
        }

    def cancel(
        self,
        session_id: str,
        job_id: str,
        *,
        reason: object = "user_requested",
    ) -> dict[str, object]:
        return self._cancel_owned(
            session_id,
            job_id,
            reason=reason,
            room_owner=False,
        )

    def cancel_room_dispatch_owned(
        self, session_id: str, job_id: str, *, context: Mapping[str, object],
        reason: object = "dispatch_requested",
    ) -> dict[str, object]:
        """Stop only the immutable job lineage authorized by the JEV owner."""
        session = _required_text(session_id, field="sessionId", maximum=240)
        normalized_job_id = _job_id(job_id)
        row = self._row(session, normalized_job_id)
        fields = {"roomId": "causal_room_id", "rootId": "causal_root_id",
                  "dispatchId": "causal_dispatch_id", "turnId": "causal_turn_id"}
        if (not bool(row["room_bound"])
                or any(not context.get(key) or row[column] != context[key]
                       for key, column in fields.items())
                or not context.get("generation")
                or row["causal_generation"] != context["generation"]):
            raise AgentBackgroundJobError("background job belongs to another Room dispatch or turn")
        return self._cancel_owned(session, normalized_job_id, reason=reason, room_owner=True)

    def cancel_room_owned(
        self,
        session_id: str,
        job_id: str,
        *,
        room_turn_id: str,
        reason: object = "room_root_cancelled",
    ) -> dict[str, object]:
        session = _required_text(session_id, field="sessionId", maximum=240)
        normalized_job_id = _job_id(job_id)
        root_id = _required_text(
            room_turn_id,
            field="roomTurnId",
            maximum=240,
        )
        row = self._row(session, normalized_job_id)
        if not bool(row["room_bound"]):
            raise AgentBackgroundJobError(
                "background job is not owned by a Room root"
            )
        # Legacy Room jobs stored the Root in turn_id before explicit lineage.
        if str(row["causal_root_id"] or row["causal_turn_id"] or "") != root_id:
            raise AgentBackgroundJobError(
                "background job belongs to another Room root"
            )
        return self._cancel_owned(
            session,
            normalized_job_id,
            reason=reason,
            room_owner=True,
        )

    def cancel_room_root(
        self,
        session_id: str,
        *,
        room_turn_id: str,
        reason: object = "room_root_cancelled",
    ) -> list[dict[str, object]]:
        session = _required_text(session_id, field="sessionId", maximum=240)
        root_id = _required_text(
            room_turn_id,
            field="roomTurnId",
            maximum=240,
        )
        with sqlite_connection(self.db_path, foreign_keys=True) as conn:
            rows = conn.execute(
                """
                SELECT job_id
                FROM agent_background_jobs
                WHERE session_id = ?
                  AND room_bound = 1
                  AND COALESCE(NULLIF(causal_root_id, ''), causal_turn_id) = ?
                  AND status IN ('queued', 'running', 'cancelling')
                ORDER BY created_at_ms, job_id
                """,
                (session, root_id),
            ).fetchall()
        if rows:
            self._require_execution_owner()
        return [
            self.cancel_room_owned(
                session,
                str(row[0]),
                room_turn_id=root_id,
                reason=reason,
            )
            for row in rows
        ]

    def cancel_room_root_all_sessions(
        self,
        *,
        room_turn_id: str,
        reason: object = "room_root_cancelled",
    ) -> list[dict[str, object]]:
        root_id = _required_text(
            room_turn_id,
            field="roomTurnId",
            maximum=240,
        )
        with sqlite_connection(self.db_path, foreign_keys=True) as conn:
            rows = conn.execute(
                """
                SELECT session_id, job_id
                FROM agent_background_jobs
                WHERE room_bound = 1
                  AND COALESCE(NULLIF(causal_root_id, ''), causal_turn_id) = ?
                  AND status IN ('queued', 'running', 'cancelling')
                ORDER BY created_at_ms, job_id
                """,
                (root_id,),
            ).fetchall()
        if rows:
            self._require_execution_owner()
        return [
            self.cancel_room_owned(
                str(row[0]),
                str(row[1]),
                room_turn_id=root_id,
                reason=reason,
            )
            for row in rows
        ]

    def request_turn_cancellation(
        self, session_id: str, identity: Mapping[str, object],
    ) -> Callable[[], dict[str, object]]:
        """Fence and capture one standalone turn; wait only outside Runtime locks."""
        self._require_execution_owner()
        turn_id = str(identity.get("turnId") or "")
        turn_ids = {turn_id} if turn_id else set()
        unresolved_job_ids: list[str] = []
        now_ms = _now_ms()
        with sqlite_connection(self.db_path, foreign_keys=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not turn_ids and identity.get("pendingAdmission") is True:
                turn_ids.update(str(row[0]) for row in conn.execute(
                    """SELECT DISTINCT request.turn_id FROM agent_gateway_requests AS request
                       JOIN agent_gateway_cancelled_turns AS stopped
                         ON stopped.session_id=request.session_id AND stopped.turn_id=request.turn_id
                       WHERE request.session_id=? AND request.client_message_id=?
                         AND (request.state<>'completed' OR EXISTS (
                           SELECT 1 FROM agent_approvals AS approval
                           WHERE approval.session_id=request.session_id
                             AND approval.tool_call_id=request.tool_call_id
                             AND approval.state IN ('pending','approved')))""",
                    (session_id, str(identity.get("clientMessageId") or "")),
                ))
                # Completing the HTTP receipt does not complete a background
                # process. Its original admitted binding remains authoritative.
                bound_jobs = conn.execute(
                    """SELECT DISTINCT job.job_id, job.causal_turn_id
                       FROM agent_background_jobs AS job
                       JOIN agent_gateway_requests AS request
                         ON request.session_id=job.session_id AND request.turn_id=job.causal_turn_id
                       WHERE job.session_id=? AND request.client_message_id=?
                         AND job.causal_turn_id<>'' AND job.room_bound=0
                         AND job.status IN ('queued','running','cancelling')""",
                    (session_id, str(identity.get("clientMessageId") or "")),
                ).fetchall()
                turn_ids.update(str(row[1]) for row in bound_jobs)
                if len(turn_ids) > 1:
                    # A reused client ID cannot select an original generation.
                    unresolved_job_ids = sorted(str(row[0]) for row in bound_jobs)
                    turn_ids.clear()
            job_ids: list[str] = []
            for target in sorted(turn_ids):
                conn.execute("INSERT OR IGNORE INTO agent_gateway_cancelled_turns VALUES (?, ?, ?)",
                             (session_id, target, now_ms))
                job_ids.extend(str(row[0]) for row in conn.execute(
                    """SELECT job_id FROM agent_background_jobs
                       WHERE session_id=? AND causal_turn_id=? AND room_bound=0
                         AND status IN ('queued','running','cancelling') ORDER BY job_id""",
                    (session_id, target),
                ))
            for job_id in job_ids:
                conn.execute("""UPDATE agent_background_jobs SET status='cancelling',
                    cancel_requested_at_ms=?, updated_at_ms=?, error='user_abort'
                    WHERE job_id=? AND status IN ('queued','running')""",
                    (now_ms, now_ms, job_id))

        def wait_for_jobs() -> dict[str, object]:
            for job_id in job_ids:
                self.cancel(session_id, job_id, reason="user_abort")
            deadline = time.monotonic() + 2.0
            while True:
                pending = list(unresolved_job_ids)
                for job_id in job_ids:
                    row = self._row(session_id, job_id)
                    # A missing identity or orphaned receipt cannot prove that
                    # the original process group has physically drained.
                    group_id = int(row["process_group_id"] or 0)
                    if (str(row["status"]) not in {"completed", "failed", "cancelled"}
                            or group_id <= 0 or _process_group_exists(group_id)):
                        pending.append(job_id)
                if unresolved_job_ids or not pending or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            return {"schemaVersion": "rag-ime.agent-background-job-turn-cancellation.v1",
                    "sessionId": session_id, "turnIds": sorted(turn_ids),
                    "jobIds": job_ids, "pendingJobIds": pending, "drained": not pending}
        return wait_for_jobs

    def _cancel_owned(
        self,
        session_id: str,
        job_id: str,
        *,
        reason: object,
        room_owner: bool,
    ) -> dict[str, object]:
        session = _required_text(session_id, field="sessionId", maximum=240)
        normalized_job_id = _job_id(job_id)
        normalized_reason = _bounded_text(reason, maximum=240) or "user_requested"
        before_row = self._row(session, normalized_job_id)
        before = self._job_payload(before_row)
        if bool(before_row["room_bound"]) and not room_owner:
            raise AgentBackgroundJobError(
                "Room-bound background jobs must be cancelled by the Room/Root owner"
            )
        if before["status"] in _TERMINAL_STATUSES:
            return {
                "schemaVersion": "rag-ime.agent-background-job-cancel-receipt.v1",
                "ok": True,
                "summary": "后台任务已经结束",
                "alreadyTerminal": True,
                "job": before,
            }
        self._require_execution_owner()
        requested_at_ms = _now_ms()
        with sqlite_connection(self.db_path, foreign_keys=True) as conn:
            conn.execute(
                """
                UPDATE agent_background_jobs
                SET status = 'cancelling',
                    cancel_requested_at_ms = ?,
                    updated_at_ms = ?,
                    error = ?
                WHERE job_id = ? AND session_id = ?
                  AND status IN ('queued', 'running')
                """,
                (
                    requested_at_ms,
                    requested_at_ms,
                    normalized_reason,
                    normalized_job_id,
                    session,
                ),
            )
        requested = self._job_payload(self._row(session, normalized_job_id))
        if requested["status"] in _TERMINAL_STATUSES:
            return {
                "schemaVersion": "rag-ime.agent-background-job-cancel-receipt.v1",
                "ok": True,
                "summary": "后台任务已经结束",
                "alreadyTerminal": True,
                "job": requested,
            }
        requested_at_ms = int(requested["cancelRequestedAtMs"])
        with self._lock:
            live = self._live.get(normalized_job_id)
        if live is not None:
            try:
                self._terminate_live(live)
            except BackgroundJobLeaseLost:
                live.detach_requested.set()
            thread = live.thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        job = self.status(session, normalized_job_id)["job"]
        return {
            "schemaVersion": "rag-ime.agent-background-job-cancel-receipt.v1",
            "ok": True,
            "summary": (
                "后台任务已停止"
                if job["status"] == "cancelled"
                else "已请求停止后台任务"
            ),
            "alreadyTerminal": False,
            "job": job,
            "cancelReceipt": {
                "jobId": normalized_job_id,
                "requestedAtMs": requested_at_ms,
                "status": job["status"],
            },
        }

    def cancel_causal(
        self,
        session_id: str,
        *,
        request_id: str,
        scope_kind: str,
        scope_id: str,
        source_revision: int,
        reason: str,
    ) -> dict[str, object]:
        session = _required_text(session_id, field="sessionId", maximum=240)
        normalized_request_id = _required_text(
            request_id,
            field="requestId",
            maximum=240,
        )
        if scope_kind != "goal":
            raise AgentBackgroundJobError(
                "unsupported lifecycle cancellation scope"
            )
        causal_clause = "causal_goal_id = ?"
        causal_values: tuple[object, ...] = (scope_id,)
        with sqlite_connection(
            self.db_path,
            row_factory=sqlite3.Row,
            foreign_keys=True,
        ) as conn:
            room_rows = conn.execute(
                f"""
                SELECT job_id
                FROM agent_background_jobs
                WHERE session_id = ? AND {causal_clause}
                  AND room_bound = 1
                  AND status IN ('queued', 'running', 'cancelling')
                ORDER BY created_at_ms, job_id
                """,
                (session, *causal_values),
            ).fetchall()
            rows = conn.execute(
                f"""
                SELECT job_id, status
                FROM agent_background_jobs
                WHERE session_id = ? AND room_bound = 0
                  AND (
                    ({causal_clause}
                     AND status IN ('queued', 'running', 'cancelling'))
                    OR lifecycle_cancel_request_id = ?
                  )
                ORDER BY created_at_ms, job_id
                """,
                (session, *causal_values, normalized_request_id),
            ).fetchall()
            active_ids = [
                str(row["job_id"])
                for row in rows
                if str(row["status"]) in _ACTIVE_STATUSES
            ]
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                conn.execute(
                    f"""
                    UPDATE agent_background_jobs
                    SET lifecycle_cancel_request_id = ?
                    WHERE session_id = ? AND job_id IN ({placeholders})
                      AND lifecycle_cancel_request_id IN ('', ?)
                    """,
                    (
                        normalized_request_id,
                        session,
                        *active_ids,
                        normalized_request_id,
                    ),
                )
        if active_ids:
            self._require_execution_owner()
        normalized_reason = _bounded_text(reason, maximum=240) or "lifecycle_cancelled"
        for job_id in active_ids:
            self.cancel(session, job_id, reason=normalized_reason)
        with sqlite_connection(
            self.db_path,
            row_factory=sqlite3.Row,
            foreign_keys=True,
        ) as conn:
            cancelled_rows = conn.execute(
                """
                SELECT job_id, status
                FROM agent_background_jobs
                WHERE session_id = ? AND lifecycle_cancel_request_id = ?
                ORDER BY created_at_ms, job_id
                """,
                (session, normalized_request_id),
            ).fetchall()
        return {
            "schemaVersion": "rag-ime.agent-background-job-lifecycle-cancellation-summary.v1",
            "requestId": normalized_request_id,
            "sessionId": session,
            "scopeKind": scope_kind,
            "scopeId": scope_id,
            "sourceRevision": int(source_revision),
            "jobs": [
                {
                    "jobId": str(row["job_id"]),
                    "status": str(row["status"]),
                }
                for row in cancelled_rows
            ],
            "excludedRoomBoundJobIds": [
                str(row["job_id"])
                for row in room_rows
            ],
        }

    def cancel_session(self, session_id: str, *, reason: str) -> list[dict[str, object]]:
        active = self.list(session_id, limit=100)["items"]
        room_bound = [
            item
            for item in active
            if item["status"] in _ACTIVE_STATUSES
            and bool(item["causalMetadata"]["roomBound"])
        ]
        if room_bound:
            raise AgentBackgroundJobError(
                "Room-bound background jobs must be cancelled by the Room/Root owner"
            )
        if not self.execution_owner:
            if any(item["status"] in _ACTIVE_STATUSES for item in active):
                raise AgentBackgroundJobError(
                    "background jobs must be cancelled by the execution owner"
                )
            return []
        receipts: list[dict[str, object]] = []
        for item in active:
            if item["status"] not in _ACTIVE_STATUSES:
                continue
            receipts.append(self.cancel(session_id, str(item["jobId"]), reason=reason))
        return receipts

    def close(self) -> None:
        if not self.execution_owner:
            with self._lock:
                self._closed = True
            return
        with self._lock:
            if self._closed:
                return
            self._closed = True
            live_jobs = list(self._live.values())
        self._recovery_stop.set()
        if self._recovery_thread is not None:
            self._recovery_thread.join(timeout=2.0)
        for live in live_jobs:
            live.detach_requested.set()
        for live in live_jobs:
            thread = live.thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
            if (
                isinstance(live.launched, SpawnedWorkspaceCommand)
                and live.launched.process.poll() is None
            ):
                with _DETACHED_PROCESS_LOCK:
                    _DETACHED_PROCESSES[live.pid] = live.launched
        self._ownership.release_all()

    def _monitor(self, job_id: str) -> None:
        with self._lock:
            live = self._live.get(job_id)
        if live is None:
            return
        try:
            if live.raw_output_path is not None and live.exit_status_path is not None:
                self._monitor_resumable(job_id, live)
            elif live.launched is None:
                self._monitor_legacy_recovery(job_id, live)
            else:
                self._monitor_pipe(job_id)
        except BackgroundJobLeaseLost:
            live.detach_requested.set()

    def _poll_control(self, job_id: str, live: _LiveJob) -> None:
        # Heartbeats do not depend on the command producing output. Cancellation
        # can be admitted by another control host; only the current owner signals.
        with self._ownership.guard(job_id, renew_expired=True) as conn:
            row = conn.execute("SELECT status FROM agent_background_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is not None and row["status"] == "cancelling":
                self._terminate_live(live)

    def _monitor_resumable(self, job_id: str, live: _LiveJob) -> None:
        timed_out = False
        terminal = False
        try:
            while not live.detach_requested.wait(0.1):
                self._poll_control(job_id, live)
                exit_code = self._read_exit_status(live.exit_status_path)
                terminal = exit_code is not None
                self._drain_raw_output(job_id, live, final=terminal)
                if terminal:
                    break
                elapsed_ms = max(0, _now_ms() - live.started_at_ms)
                if elapsed_ms >= live.max_run_seconds * 1_000:
                    timed_out = True
                    self._request_timeout(job_id)
                    self._terminate_live(live)
                    break
                if not self._live_identity_matches(live):
                    break
            if live.detach_requested.is_set():
                self._persist_live_progress(job_id, live, force=True)
                return
            if not terminal:
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    exit_code = self._read_exit_status(live.exit_status_path)
                    if exit_code is not None:
                        terminal = True
                        break
                    if not self._live_identity_matches(live):
                        time.sleep(0.02)
                        continue
                    time.sleep(0.02)
            self._drain_raw_output(job_id, live, final=True)
            self._persist_live_progress(job_id, live, force=True)
            event_type, job = self._commit_result(job_id, live, exit_code=exit_code, timed_out=timed_out)
            if self._live_identity_matches(live):
                self._terminate_live(live)
            try:
                self._publish(event_type, job)
            except Exception:
                pass
        except BackgroundJobLeaseLost:
            live.detach_requested.set()
        except Exception as exc:
            if live.detach_requested.is_set():
                return
            self._terminate_live(live)
            self._finalize_without_process(
                job_id,
                status="failed",
                error=_public_error(exc),
            )
        finally:
            if not live.detach_requested.is_set():
                try:
                    self._cleanup_runtime_files(live)
                except BackgroundJobLeaseLost:
                    live.detach_requested.set()
            with self._lock:
                self._live.pop(job_id, None)

    def _monitor_legacy_recovery(self, job_id: str, live: _LiveJob) -> None:
        try:
            while not live.detach_requested.wait(0.1):
                self._poll_control(job_id, live)
                if not self._live_identity_matches(live):
                    row = self._row_for_job(job_id)
                    cancelling = str(row["status"] or "") == "cancelling"
                    self._finalize_without_process(
                        job_id,
                        status="cancelled" if cancelling else "orphaned",
                        error=(
                            str(row["error"] or "")
                            if cancelling
                            else "旧版后台任务在宿主恢复后结束；缺少可恢复退出回执"
                        ),
                    )
                    return
        finally:
            with self._lock:
                self._live.pop(job_id, None)

    def _monitor_pipe(self, job_id: str) -> None:
        with self._lock:
            live = self._live.get(job_id)
        if live is None:
            return
        process = live.launched.process
        output = process.stdout
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending_text = ""
        selector = selectors.DefaultSelector()
        deadline = time.monotonic() + live.max_run_seconds
        timed_out = False
        try:
            if output is not None:
                selector.register(output, selectors.EVENT_READ)
            while selector.get_map():
                if live.detach_requested.is_set():
                    return
                self._poll_control(job_id, live)
                if process.poll() is None and time.monotonic() >= deadline:
                    timed_out = True
                    self._request_timeout(job_id)
                    self._terminate_and_wait(live)
                events = selector.select(timeout=0.2)
                if not events and process.poll() is not None:
                    events = selector.select(timeout=0)
                    if not events:
                        break
                for key, _ in events:
                    chunk = os.read(key.fd, 65_536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    pending_text += decoder.decode(chunk)
                    with self._ownership.guard(job_id):
                        pending_text = self._flush_complete_lines(live, pending_text)
                        self._persist_live_progress(job_id, live)
                if process.poll() is not None:
                    # A shell that exits must not leave an approved descendant
                    # running in the same process group with the log pipe open.
                    self._terminate_and_wait(live)
                    if not events:
                        break
            pending_text += decoder.decode(b"", final=True)
            if pending_text:
                with self._ownership.guard(job_id):
                    self._append_redacted_text(live, pending_text)
            try:
                exit_code = process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._terminate_and_wait(live)
                exit_code = process.poll()
                if exit_code is None:
                    raise AgentBackgroundJobError(
                        "background job process group did not terminate"
                    )
            else:
                self._terminate_and_wait(live)
            self._persist_live_progress(job_id, live, force=True)
            event_type, job = self._commit_result(job_id, live, exit_code=exit_code, timed_out=timed_out)
            try:
                self._publish(event_type, job)
            except Exception:
                pass
        except BackgroundJobLeaseLost:
            live.detach_requested.set()
        except Exception as exc:
            error = _public_error(exc)
            self._terminate_and_wait(live)
            self._finalize_without_process(job_id, status="failed", error=error)
            try:
                job = self._job_payload(self._row_for_job(job_id))
                self._publish("background_job_failed", job)
            except Exception:
                pass
        finally:
            try:
                selector.close()
            except Exception:
                pass
            if output is not None:
                try:
                    output.close()
                except Exception:
                    pass
            if not live.detach_requested.is_set():
                try:
                    self._cleanup_runtime_files(live)
                except BackgroundJobLeaseLost:
                    live.detach_requested.set()
            with self._lock:
                self._live.pop(job_id, None)

    def _drain_raw_output(
        self,
        job_id: str,
        live: _LiveJob,
        *,
        final: bool,
    ) -> None:
        path = live.raw_output_path
        if path is None or not path.exists():
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending_text = ""
        while True:
            with path.open("rb") as handle:
                handle.seek(live.raw_output_cursor)
                data = handle.read(1_048_576)
            if not data:
                if final:
                    pending_text += decoder.decode(b"", final=True)
                    if pending_text:
                        with self._ownership.guard(job_id):
                            self._append_redacted_text(live, pending_text)
                return
            consumed = len(data)
            if not final:
                boundary = max(data.rfind(b"\n"), data.rfind(b"\r"))
                if boundary < 0:
                    return
                consumed = boundary + 1
                data = data[:consumed]
            pending_text += decoder.decode(data)
            with self._ownership.guard(job_id):
                pending_text = self._flush_complete_lines(live, pending_text)
                live.raw_output_cursor += consumed
                self._persist_live_progress(job_id, live)
            if not final and consumed < 1_048_576:
                return

    def _commit_result(self, job_id: str, live: _LiveJob, *, exit_code: int | None, timed_out: bool):
        with self._ownership.guard(job_id) as conn:
            # Read cancellation and commit the result under the same fence.
            # A control request cannot slip between the snapshot and writeback.
            row = self._row_for_job(job_id)
            if row["status"] in _TERMINAL_STATUSES:
                raise BackgroundJobLeaseLost("background job already has a terminal result")
            timed_out = timed_out or row["error"] == "background_job_timeout"
            if row["status"] == "cancelling" and not timed_out:
                status, event_type, error = "cancelled", "background_job_cancelled", str(row["error"] or "")
            elif timed_out:
                status, event_type, error = "failed", "background_job_failed", f"后台任务超过 {live.max_run_seconds} 秒运行上限"
            elif exit_code == 0:
                status, event_type, error = "completed", "background_job_completed", ""
            elif exit_code is not None:
                status, event_type, error = "failed", "background_job_failed", f"后台任务退出码 {exit_code}"
            else:
                status, event_type, error = "orphaned", "background_job_failed", "后台任务进程已结束，但退出回执缺失"
            now_ms = _now_ms()
            conn.execute(
                """UPDATE agent_background_jobs
                   SET status=?, exit_code=?, output_bytes=?, raw_output_cursor=?,
                       log_start_cursor=?, log_truncated=?, updated_at_ms=?, ended_at_ms=?, error=?
                   WHERE job_id=? AND status IN ('queued','running','cancelling')""",
                (status, exit_code, live.output_bytes, live.raw_output_cursor,
                 live.log_start_cursor, int(live.log_truncated), now_ms, now_ms, error, job_id),
            )
            return event_type, self._job_payload(self._row_for_job(job_id))

    @staticmethod
    def _read_exit_status(path: Path | None) -> int | None:
        if path is None or not path.exists() or path.stat().st_size <= 0:
            return None
        value = path.read_text(encoding="ascii", errors="replace").splitlines()
        if not value:
            return None
        try:
            return int(value[0].strip())
        except ValueError as exc:
            raise AgentBackgroundJobError(
                "background job exit receipt is invalid"
            ) from exc

    @staticmethod
    def _live_identity_matches(live: _LiveJob) -> bool:
        if (
            live.pid <= 0
            or live.process_group_id <= 0
            or not live.process_birth_token
            or live.process_group_id == os.getpgrp()
        ):
            return False
        return _process_identity(live.pid) == (
            live.process_group_id,
            live.process_birth_token,
        )

    def _terminate_live(self, live: _LiveJob) -> None:
        if not isinstance(live.launched, SpawnedWorkspaceCommand) and not self._live_identity_matches(live):
            return
        with self._ownership.guard(live.job_id):
            self._terminate_live_owned(live)

    def _terminate_live_owned(self, live: _LiveJob) -> None:
        if isinstance(live.launched, SpawnedWorkspaceCommand):
            try:
                self.workspace_harness.terminate_background(live.launched)
            except Exception:
                pass
            process = live.launched.process
            try:
                process.wait(timeout=2.0)
            except Exception:
                pass
            return
        if not self._live_identity_matches(live):
            return
        try:
            _signal_process_group(live.process_group_id)
        except (OSError, ProcessLookupError):
            pass

    def _cleanup_runtime_files(self, live: _LiveJob) -> None:
        with self._ownership.guard(live.job_id):
            self._cleanup_runtime_files_owned(live)

    def _cleanup_runtime_files_owned(self, live: _LiveJob) -> None:
        if isinstance(live.launched, SpawnedWorkspaceCommand):
            try:
                live.launched.process.wait(timeout=1.0)
            except Exception:
                pass
            try:
                live.launched.cleanup()
            except Exception:
                pass
        elif live.temporary_path is not None:
            shutil.rmtree(live.temporary_path, ignore_errors=True)
        for path in (live.launch_receipt_path, live.launch_release_path):
            if path is not None:
                path.unlink(missing_ok=True)
        if live.raw_output_path is not None:
            live.raw_output_path.unlink(missing_ok=True)
        if live.exit_status_path is not None:
            live.exit_status_path.unlink(missing_ok=True)

    def _restore_launch_identity(self, row: Mapping[str, object]) -> Mapping[str, object]:
        """Recover the child's pre-execution receipt, never spawn a replacement."""
        if str(row["status"]) not in {"queued", "cancelling"} or row["pid"] is not None:
            return row
        job_id = str(row["job_id"])
        path = self.log_root / f"{job_id}.launch"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                content = stream.read(4097)
            if len(content) > 4096:
                return row
            receipt = json.loads(content)
            if (
                not isinstance(receipt, dict)
                or receipt.get("schemaVersion") != "rag-ime.background-launch.v1"
                or receipt.get("commandSha256") != str(row["command_sha256"])
            ):
                return row
            pid = int(receipt["pid"])
            group = int(receipt["processGroupId"])
            birth = str(receipt["processBirthToken"])
        except (OSError, ValueError, TypeError, KeyError):
            return row
        if pid <= 0 or group != pid or group == os.getpgrp() or not birth:
            return row
        if _process_identity(pid) != (group, birth):
            return row
        with self._ownership.guard(job_id) as conn:
            now_ms = _now_ms()
            conn.execute(
                """
                UPDATE agent_background_jobs
                SET status=CASE WHEN status='queued' THEN 'running' ELSE status END,
                    pid=?, process_group_id=?, process_birth_token=?,
                    started_at_ms=?, updated_at_ms=?
                WHERE job_id=? AND status IN ('queued', 'cancelling') AND pid IS NULL
                """,
                (pid, group, birth, now_ms, now_ms, job_id),
            )
        return self._row_for_job(job_id)

    def _release_launch(self, job_id: str) -> None:
        """Release only the same durably admitted job under its current epoch."""
        with self._ownership.guard(job_id) as conn:
            row = conn.execute("SELECT * FROM agent_background_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or str(row["status"]) != "running":
                raise AgentBackgroundJobError("background job was cancelled before launch release")
            self._require_live_causal_epoch(conn, str(row["session_id"]), {
                "roomBound": bool(row["room_bound"]),
                "turnId": str(row["causal_turn_id"] or ""),
                "goalId": str(row["causal_goal_id"] or ""),
                "goalRevision": int(row["causal_goal_revision"] or 0),
            })
            path = self.log_root / f"{job_id}.release"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return
            with os.fdopen(fd, "wb") as stream:
                stream.write(b"admitted\n")
                stream.flush()
                os.fsync(stream.fileno())

    def _session_allows_raw_output(self, session_id: str) -> bool:
        if not session_id:
            return False
        try:
            session = self.sessions.get(session_id)
        except Exception:
            return False
        return unrestricted_workspace_policy_active(session)

    def _flush_complete_lines(self, live: _LiveJob, text: str) -> str:
        last_newline = max(text.rfind("\n"), text.rfind("\r"))
        if last_newline >= 0:
            self._append_redacted_text(live, text[: last_newline + 1])
            text = text[last_newline + 1 :]
        if len(text) > 65_536:
            retain_from = len(text) - 1_024
            if self._session_allows_raw_output(live.session_id):
                self._append_redacted_text(live, text[:retain_from])
                return text[retain_from:]
            redacted = self.workspace_harness.redact_output(text)
            context_size = 1_024
            while retain_from > 0:
                redacted_suffix = self.workspace_harness.redact_output(text[retain_from:])
                if redacted.endswith(redacted_suffix):
                    prefix_length = len(redacted) - len(redacted_suffix)
                    self._append_redacted_text(live, redacted[:prefix_length])
                    return text[retain_from:]
                retain_from = max(0, retain_from - context_size)
                context_size *= 2
            return text
        return text

    def _append_redacted_text(self, live: _LiveJob, text: str) -> None:
        output_text = (
            text
            if self._session_allows_raw_output(live.session_id)
            else self.workspace_harness.redact_output(text)
        )
        data = output_text.encode("utf-8")
        if not data:
            return
        with self._lock:
            current_size = live.log_path.stat().st_size if live.log_path.exists() else 0
            live.output_bytes += len(data)
            if current_size + len(data) > _LOG_TRIM_TRIGGER_BYTES:
                combined = live.log_path.read_bytes() + data
                tail = _valid_utf8_tail(combined[-_MAX_LOG_BYTES:])
                live.log_start_cursor = live.output_bytes - len(tail)
                live.log_truncated = True
                live.log_path.write_bytes(tail)
            else:
                with live.log_path.open("ab") as handle:
                    handle.write(data)

    def _persist_live_progress(self, job_id: str, live: _LiveJob, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - live.last_persisted_at < _PROGRESS_INTERVAL_SECONDS:
            return
        live.last_persisted_at = now
        now_ms = _now_ms()
        with self._ownership.guard(job_id) as conn:
            conn.execute(
                """
                UPDATE agent_background_jobs
                SET output_bytes = ?,
                    raw_output_cursor = ?,
                    log_start_cursor = ?,
                    log_truncated = ?,
                    updated_at_ms = ?
                WHERE job_id = ? AND status IN ('queued', 'running', 'cancelling')
                """,
                (
                    live.output_bytes,
                    live.raw_output_cursor,
                    live.log_start_cursor,
                    int(live.log_truncated),
                    now_ms,
                    job_id,
                ),
            )
        if live.output_bytes > 0 and (force or now - live.last_progress_at >= _PROGRESS_INTERVAL_SECONDS):
            live.last_progress_at = now
            job = self._job_payload(self._row_for_job(job_id))
            self._publish("background_job_progress", job)

    def _request_timeout(self, job_id: str) -> None:
        now_ms = _now_ms()
        with self._ownership.guard(job_id) as conn:
            conn.execute(
                """
                UPDATE agent_background_jobs
                SET status = 'cancelling',
                    cancel_requested_at_ms = ?,
                    updated_at_ms = ?,
                    error = 'background_job_timeout'
                WHERE job_id = ? AND status IN ('queued', 'running')
                """,
                (now_ms, now_ms, job_id),
            )

    def _mark_launch_failed(self, job_id: str, error: str) -> None:
        now_ms = _now_ms()
        with self._ownership.guard(job_id) as conn:
            conn.execute(
                """
                UPDATE agent_background_jobs
                SET status = 'failed', updated_at_ms = ?, ended_at_ms = ?, error = ?
                WHERE job_id = ? AND status IN ('queued', 'running', 'cancelling')
                """,
                (now_ms, now_ms, error, job_id),
            )

    def _finalize_without_process(self, job_id: str, *, status: str, error: str) -> None:
        now_ms = _now_ms()
        with self._ownership.guard(job_id) as conn:
            conn.execute(
                """
                UPDATE agent_background_jobs
                SET status = ?, updated_at_ms = ?, ended_at_ms = ?, error = ?
                WHERE job_id = ? AND status NOT IN ('completed', 'failed', 'cancelled', 'orphaned')
                """,
                (status, now_ms, now_ms, error[:500], job_id),
            )

    def _terminate_and_wait(self, live: _LiveJob) -> None:
        with self._ownership.guard(live.job_id):
            self._terminate_and_wait_owned(live)

    def _terminate_and_wait_owned(self, live: _LiveJob) -> None:
        process = live.launched.process
        try:
            self.workspace_harness.terminate_background(live.launched)
        except Exception:
            pass
        try:
            # The harness may already have reaped the leader. Reuse the
            # generation-safe group terminator instead of signalling its old PID.
            WorkspaceHarness._terminate_group(process)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.wait(timeout=2.0)
            except Exception:
                pass
        except Exception:
            pass

    def _abort_launched_job(
        self,
        job_id: str,
        live: _LiveJob,
        *,
        error: str,
    ) -> None:
        try:
            self._terminate_and_wait(live)
        except BackgroundJobLeaseLost:
            live.detach_requested.set()
            with self._lock:
                self._live.pop(job_id, None)
            return
        output = live.launched.process.stdout
        if output is not None:
            try:
                output.close()
            except Exception:
                pass
        self._cleanup_runtime_files(live)
        try:
            self._mark_launch_failed(job_id, error)
        except Exception:
            pass
        with self._lock:
            self._live.pop(job_id, None)

    @staticmethod
    def _require_live_causal_epoch(
        conn: sqlite3.Connection,
        session_id: str,
        causal: Mapping[str, object],
    ) -> None:
        if bool(causal["roomBound"]):
            return
        turn_id = str(causal.get("turnId") or "")
        if turn_id and conn.execute(
            "SELECT 1 FROM agent_gateway_cancelled_turns WHERE session_id=? AND turn_id=?",
            (session_id, turn_id),
        ).fetchone() is not None:
            raise AgentBackgroundJobError("background job turn was cancelled before launch")
        goal_id = str(causal["goalId"] or "")
        goal_revision = int(causal["goalRevision"] or 0)
        clauses: list[str] = []
        values: list[object] = [session_id]
        if goal_id and goal_revision > 0:
            clauses.append(
                "(scope_kind = 'goal' AND scope_id = ? AND source_revision = ?)"
            )
            values.extend((goal_id, goal_revision))
        if not clauses:
            return
        cancelled = conn.execute(
            f"""
            SELECT 1
            FROM agent_lifecycle_cancellation_audits
            WHERE session_id = ? AND ({' OR '.join(clauses)})
            LIMIT 1
            """,
            tuple(values),
        ).fetchone()
        if cancelled is not None:
            raise AgentBackgroundJobError(
                "background job causal lifecycle was cancelled before launch"
            )

    def _require_capacity(
        self,
        conn: sqlite3.Connection,
        session_id: str,
    ) -> None:
        count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM agent_background_jobs
                WHERE session_id = ? AND status IN ('queued', 'running', 'cancelling')
                """,
                (session_id,),
            ).fetchone()[0]
        )
        if count >= _MAX_ACTIVE_JOBS_PER_SESSION:
            raise AgentBackgroundJobError("a session may own at most 8 active background jobs")

    def _row(self, session_id: str, job_id: str) -> sqlite3.Row:
        row = self._row_for_job(job_id)
        if str(row["session_id"] or "") != session_id:
            raise AgentBackgroundJobError("background job is outside the current session")
        return row

    def _row_for_job(self, job_id: str) -> sqlite3.Row:
        owned_conn = self._ownership.current_connection(job_id)
        if owned_conn is not None:
            row = owned_conn.execute("SELECT * FROM agent_background_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise AgentBackgroundJobError("background job was not found")
            return row
        with sqlite_connection(
            self.db_path,
            row_factory=sqlite3.Row,
            foreign_keys=True,
        ) as conn:
            row = conn.execute(
                "SELECT * FROM agent_background_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise AgentBackgroundJobError("background job was not found")
        return row

    def _job_payload(self, row: Mapping[str, object]) -> dict[str, object]:
        payload: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-background-job.v1",
            "jobId": str(row["job_id"]),
            "sessionId": str(row["session_id"]),
            "label": str(row["label"]),
            "status": str(row["status"]),
            "command": str(row["command"]),
            "commandSha256": str(row["command_sha256"]),
            "cwd": str(row["cwd"]),
            "networkAllowed": bool(row["network_allowed"]),
            "maxRunSeconds": int(row["max_run_seconds"]),
            "pid": int(row["pid"]) if row["pid"] is not None else None,
            "createdAtMs": int(row["created_at_ms"]),
            "startedAtMs": int(row["started_at_ms"]),
            "updatedAtMs": int(row["updated_at_ms"]),
            "endedAtMs": int(row["ended_at_ms"]),
            "exitCode": int(row["exit_code"]) if row["exit_code"] is not None else None,
            "outputBytes": int(row["output_bytes"]),
            "logStartCursor": int(row["log_start_cursor"]),
            "logTruncated": bool(row["log_truncated"]),
            "cancelRequestedAtMs": int(row["cancel_requested_at_ms"]),
            "error": str(row["error"] or "")[:500],
            "approvalId": str(row["approval_id"] or "")[:240],
            "causalMetadata": {
                "todoId": str(row["causal_todo_id"] or ""),
                "todoRevision": int(row["causal_todo_revision"] or 0),
                "goalId": str(row["causal_goal_id"] or ""),
                "goalRevision": int(row["causal_goal_revision"] or 0),
                "turnId": str(row["causal_turn_id"] or ""),
                "roomBound": bool(row["room_bound"]),
            },
            "roomLineage": {
                "roomId": str(row["causal_room_id"] or ""),
                "rootId": str(row["causal_root_id"] or ""),
                "generation": int(row["causal_generation"] or 0),
                "taskId": str(row["causal_task_id"] or ""),
                "dispatchId": str(row["causal_dispatch_id"] or ""),
            },
        }
        validate_contract(payload, "agent-background-job.v1.json")
        return payload

    def _require_execution_owner(self) -> None:
        if not self.execution_owner:
            raise AgentBackgroundJobError(
                "background job execution is owned by the Agent Gateway"
            )

    def _publish(self, event_type: str, job: Mapping[str, object]) -> None:
        if self._ownership.defer_event(lambda: self._publish(event_type, job)):
            return
        self.events(
            str(job["sessionId"]),
            event_type,
            {
                "jobId": str(job["jobId"]),
                "status": str(job["status"]),
                "summary": _event_summary(event_type, job),
                "job": dict(job),
            },
            turn_id=f"background:{job['jobId']}",
        )

    def _ensure_log_root(self) -> None:
        if self.log_root.exists() and self.log_root.is_symlink():
            raise AgentBackgroundJobError("background job log directory cannot be a symlink")
        self.log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.log_root, 0o700)

    def _log_path(self, job_id: str) -> Path:
        self._ensure_log_root()
        return self.log_root / f"{job_id}.log"

    def _raw_output_path(self, job_id: str) -> Path:
        self._ensure_log_root()
        return self.log_root / f"{job_id}.raw"

    def _exit_status_path(self, job_id: str) -> Path:
        self._ensure_log_root()
        return self.log_root / f"{job_id}.exit"

    def _temporary_path(self, job_id: str) -> Path:
        self._ensure_log_root()
        return self.log_root / f"{job_id}.tmp"

    def _stored_runtime_path(
        self,
        value: str,
        job_id: str,
        *,
        suffix: str,
    ) -> Path | None:
        if not value:
            return None
        expected = (
            self.log_root / f"{job_id}.exit"
            if suffix == "exit"
            else self.log_root / f"{job_id}.{suffix}"
        ).resolve(strict=False)
        candidate = Path(value).resolve(strict=False)
        if candidate != expected or candidate.is_symlink():
            return None
        return candidate

    def _safe_stored_log_path(self, value: str, job_id: str) -> Path:
        expected = self._log_path(job_id).resolve(strict=False)
        candidate = Path(value).resolve(strict=False)
        if candidate != expected or candidate.is_symlink():
            raise AgentBackgroundJobError("background job log path is invalid")
        return candidate


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(int(process_group_id), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _job_causal_metadata(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    approval_id: str,
    supplied: Mapping[str, object] | None,
) -> dict[str, object]:
    causal: dict[str, object] = {
        "todoId": "",
        "todoRevision": 0,
        "goalId": "",
        "goalRevision": 0,
        "turnId": "",
        "roomBound": False,
        "roomId": "",
        "rootId": "",
        "generation": 0,
        "taskId": "",
        "dispatchId": "",
    }
    normalized_approval_id = str(approval_id or "").strip()
    inherited_room_bound = False
    if normalized_approval_id:
        row = conn.execute(
            """
            SELECT session_id, tool_name, operation, state,
                   causal_todo_id, causal_todo_revision, causal_goal_id,
                   causal_goal_revision, causal_turn_id, room_bound
            FROM agent_approvals
            WHERE approval_id = ?
            """,
            (normalized_approval_id,),
        ).fetchone()
        if row is None:
            raise AgentBackgroundJobError("background job approval was not found")
        if (
            str(row[0]) != session_id
            or str(row[1]) != "workspace_job"
            or str(row[2]) != "start"
        ):
            raise AgentBackgroundJobError(
                "background job approval does not authorize this launch"
            )
        if str(row[3]) != "approved":
            raise AgentBackgroundJobError(
                "background job approval is no longer approved"
            )
        causal = {
            "todoId": str(row[4] or ""),
            "todoRevision": int(row[5] or 0),
            "goalId": str(row[6] or ""),
            "goalRevision": int(row[7] or 0),
            "turnId": str(row[8] or ""),
            "roomBound": bool(row[9]),
            "roomId": "",
            "rootId": "",
            "generation": 0,
            "taskId": "",
            "dispatchId": "",
        }
        inherited_room_bound = bool(causal["roomBound"])
        if supplied is not None:
            for key in ("roomId", "rootId", "generation", "taskId", "dispatchId"):
                if key in supplied:
                    causal[key] = supplied[key]
    elif supplied is not None:
        for key in causal:
            if key in supplied:
                causal[key] = supplied[key]
    causal["todoId"] = str(causal["todoId"] or "").strip()[:240]
    causal["todoRevision"] = max(0, int(causal["todoRevision"] or 0))
    causal["goalId"] = str(causal["goalId"] or "").strip()[:240]
    causal["goalRevision"] = max(0, int(causal["goalRevision"] or 0))
    causal["turnId"] = str(causal["turnId"] or "").strip()[:240]
    causal["roomId"] = str(causal["roomId"] or "").strip()[:240]
    causal["rootId"] = str(causal["rootId"] or "").strip()[:240]
    causal["generation"] = max(0, int(causal["generation"] or 0))
    causal["taskId"] = str(causal["taskId"] or "").strip()[:240]
    causal["dispatchId"] = str(causal["dispatchId"] or "").strip()[:240]
    causal["roomBound"] = inherited_room_bound or (
        bool(supplied.get("roomBound"))
        if supplied is not None and not normalized_approval_id
        else False
    )
    return causal


def _event_summary(event_type: str, job: Mapping[str, object]) -> str:
    label = str(job.get("label") or "后台任务")
    return {
        "background_job_started": f"后台任务《{label}》已启动",
        "background_job_progress": f"后台任务《{label}》产生了新日志",
        "background_job_completed": f"后台任务《{label}》已完成",
        "background_job_failed": f"后台任务《{label}》失败",
        "background_job_cancelled": f"后台任务《{label}》已停止",
    }.get(event_type, f"后台任务《{label}》已更新")


def _job_label(value: object, command: str) -> str:
    label = _bounded_text(value, maximum=120)
    if label:
        return label
    compact = " ".join(command.split())
    return (compact[:117] + "...") if len(compact) > 120 else compact


def _job_id(value: object) -> str:
    normalized = str(value or "").strip()
    if len(normalized) != 35 or not normalized.startswith("bg_"):
        raise AgentBackgroundJobError("background job id is invalid")
    suffix = normalized[3:]
    if any(character not in "0123456789abcdef" for character in suffix):
        raise AgentBackgroundJobError("background job id is invalid")
    return normalized


def _idempotency_key(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if len(normalized) > 240 or any(ord(char) < 32 for char in normalized):
        raise AgentBackgroundJobError("background job idempotency key is invalid")
    return normalized


def _background_start_digest(prepared: PreparedWorkspaceCommand) -> str:
    execution = {
        "schemaVersion": "rag-ime.agent-background-job-start-identity.v1",
        "command": prepared.command,
        "cwd": str(prepared.cwd),
        "rootsDigest": prepared.roots_digest,
        "timeoutSeconds": int(prepared.timeout_seconds),
        "allowNetwork": bool(prepared.allow_network),
        "sourceReadOnly": bool(prepared.source_read_only),
        "unrestricted": bool(prepared.unrestricted),
    }
    return hashlib.sha256(
        json.dumps(
            execution,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _required_text(value: object, *, field: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        raise AgentBackgroundJobError(f"{field} is invalid")
    return normalized


def _bounded_text(value: object, *, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        parsed = default
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
    return max(minimum, min(maximum, parsed))


def _valid_utf8_tail(value: bytes) -> bytes:
    tail = value
    while tail and 0x80 <= tail[0] <= 0xBF:
        tail = tail[1:]
    return tail


def _public_error(exc: BaseException) -> str:
    value = " ".join(str(exc).split())[:500]
    return value or type(exc).__name__


def _now_ms() -> int:
    return int(time.time() * 1_000)
