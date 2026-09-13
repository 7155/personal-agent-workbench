from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agent_protocol import AgentEventEnvelope
from .agent_role_identity import canonical_agent_role_id
from .db import apply_database_migrations


_RECURRENCE_DAYS = {
    "daily": 1,
    "weekly": 7,
}
_MAX_HORIZON_MS = 366 * 24 * 60 * 60 * 1000
_DEFAULT_LEASE_MS = 6 * 60 * 60 * 1000


class AgentWakeScheduleStore:
    """Durable schedule and run ledger shared by the control and Agent processes."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)

    def validate_create(
        self,
        payload: Mapping[str, object],
        *,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _now_ms(now_ms)
        target_type = _text(payload.get("targetType"), maximum=20).lower()
        if target_type not in {"session", "role"}:
            raise ValueError("wake targetType must be session or role")
        target_session_id = _text(payload.get("targetSessionId"), maximum=240)
        target_role_id = canonical_agent_role_id(
            _text(payload.get("targetRoleId"), maximum=120)
        )
        target_role_version = _text(payload.get("targetRoleVersion"), maximum=40) or "1"
        if target_type == "session" and not target_session_id:
            raise ValueError("targetSessionId is required for a session wake")
        if target_type == "role" and not target_role_id:
            raise ValueError("targetRoleId is required for a role wake")
        instruction = _text(payload.get("instruction"), maximum=8_000)
        if not instruction:
            raise ValueError("wake instruction must not be empty")
        title = _text(payload.get("title"), maximum=120) or instruction[:80]
        wake_at_ms = _integer(payload.get("wakeAtMs"), minimum=1)
        if wake_at_ms < timestamp + 1_000:
            raise ValueError("wakeAtMs must be at least one second in the future")
        if wake_at_ms > timestamp + _MAX_HORIZON_MS:
            raise ValueError("wakeAtMs cannot be more than 366 days in the future")
        recurrence_kind = _text(payload.get("recurrenceKind"), maximum=20).lower() or "once"
        if recurrence_kind not in {"once", "daily", "weekly"}:
            raise ValueError("recurrenceKind must be once, daily, or weekly")
        recurrence_interval = _integer(
            payload.get("recurrenceInterval"), default=1, minimum=1, maximum=30
        )
        max_runs = _integer(
            payload.get("maxRuns"),
            default=1 if recurrence_kind == "once" else 30,
            minimum=1,
            maximum=100,
        )
        if recurrence_kind == "once":
            recurrence_interval = 1
            max_runs = 1
        timezone = _text(payload.get("timezone"), maximum=80) or "Asia/Shanghai"
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return {
            "title": title,
            "instruction": instruction,
            "targetType": target_type,
            "targetSessionId": target_session_id if target_type == "session" else "",
            "targetRoleId": target_role_id if target_type == "role" else "",
            "targetRoleVersion": target_role_version if target_type == "role" else "",
            "planningTaskId": _text(payload.get("planningTaskId"), maximum=240),
            "timezone": timezone,
            "recurrenceKind": recurrence_kind,
            "recurrenceInterval": recurrence_interval,
            "maxRuns": max_runs,
            "wakeAtMs": wake_at_ms,
        }

    def create(
        self,
        payload: Mapping[str, object],
        *,
        created_by_session_id: str = "",
        now_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _now_ms(now_ms)
        normalized = self.validate_create(payload, now_ms=timestamp)
        schedule_id = f"wake:{uuid.uuid4()}"
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_wake_schedules(
                    schedule_id, title, instruction, target_type,
                    target_session_id, target_role_id, target_role_version,
                    created_by_session_id, planning_task_id, timezone,
                    recurrence_kind, recurrence_interval, max_runs, run_count,
                    status, next_wake_at_ms, created_at_ms, updated_at_ms, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'scheduled', ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    normalized["title"],
                    normalized["instruction"],
                    normalized["targetType"],
                    normalized["targetSessionId"],
                    normalized["targetRoleId"],
                    normalized["targetRoleVersion"],
                    _text(created_by_session_id, maximum=240),
                    normalized["planningTaskId"],
                    normalized["timezone"],
                    normalized["recurrenceKind"],
                    normalized["recurrenceInterval"],
                    normalized["maxRuns"],
                    normalized["wakeAtMs"],
                    timestamp,
                    timestamp,
                    json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True),
                ),
            )
        return self.get(schedule_id)

    def update(
        self,
        schedule_id: str,
        payload: Mapping[str, object],
        *,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Edit a future plan atomically without replacing its run ledger."""
        identifier = _required_id(schedule_id)
        timestamp = _now_ms(now_ms)
        value = self.validate_create(payload, now_ms=timestamp)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM agent_wake_schedules WHERE schedule_id = ?", (identifier,)
            ).fetchone()
            if current is None:
                raise KeyError(identifier)
            if current["status"] not in {"scheduled", "paused"}:
                raise ValueError("only a scheduled or paused wake can be edited")
            if int(value["maxRuns"]) <= int(current["run_count"]):
                raise ValueError("maxRuns must exceed the number of completed attempts")
            conn.execute(
                """UPDATE agent_wake_schedules SET title = ?, instruction = ?,
                   target_type = ?, target_session_id = ?, target_role_id = ?, target_role_version = ?,
                   planning_task_id = ?, timezone = ?, recurrence_kind = ?, recurrence_interval = ?,
                   max_runs = ?, next_wake_at_ms = ?, updated_at_ms = ? WHERE schedule_id = ?""",
                (value["title"], value["instruction"], value["targetType"], value["targetSessionId"],
                 value["targetRoleId"], value["targetRoleVersion"], value["planningTaskId"], value["timezone"],
                 value["recurrenceKind"], value["recurrenceInterval"], value["maxRuns"], value["wakeAtMs"],
                 timestamp, identifier),
            )
        return self.get(identifier)

    def create_room_wake(
        self,
        *,
        schedule_id: str,
        target_session_id: str,
        created_by_session_id: str,
        title: str,
        instruction: str,
        metadata: Mapping[str, object],
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Create one deterministic internal Room wake on the shared ledger.

        Public scheduling continues through ``create`` and its future-time
        validation. Room completion wakes are Runtime events, so they are due
        immediately and use a caller-owned idempotency key.
        """

        timestamp = _now_ms(now_ms)
        identifier = _required_id(schedule_id)
        session_id = _required_id(target_session_id)
        creator = _required_id(created_by_session_id)
        normalized_title = _text(title, maximum=120)
        normalized_instruction = _text(instruction, maximum=8_000)
        if not normalized_title or not normalized_instruction:
            raise ValueError("Room wake title and instruction must not be empty")
        metadata_json = json.dumps(
            dict(metadata),
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM agent_wake_schedules WHERE schedule_id = ?",
                (identifier,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO agent_wake_schedules(
                        schedule_id, title, instruction, target_type,
                        target_session_id, target_role_id, target_role_version,
                        created_by_session_id, planning_task_id, timezone,
                        recurrence_kind, recurrence_interval, max_runs, run_count,
                        status, next_wake_at_ms, created_at_ms, updated_at_ms,
                        metadata_json
                    ) VALUES (?, ?, ?, 'session', ?, '', '', ?, '',
                              'Asia/Shanghai', 'once', 1, 1, 0,
                              'scheduled', ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        normalized_title,
                        normalized_instruction,
                        session_id,
                        creator,
                        timestamp,
                        timestamp,
                        timestamp,
                        metadata_json,
                    ),
                )
            else:
                if (
                    str(existing["target_session_id"]) != session_id
                    or str(existing["created_by_session_id"]) != creator
                    or str(existing["metadata_json"]) != metadata_json
                ):
                    raise ValueError(
                        "Room wake idempotency key was reused for a different dispatch"
                    )
        return self.get(identifier)

    def get(self, schedule_id: str) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_wake_schedules WHERE schedule_id = ?",
                (_required_id(schedule_id),),
            ).fetchone()
            if row is None:
                raise KeyError(schedule_id)
            latest = conn.execute(
                "SELECT * FROM agent_wake_runs WHERE schedule_id = ? ORDER BY started_at_ms DESC LIMIT 1",
                (schedule_id,),
            ).fetchone()
        return _schedule_payload(row, latest)

    def list(
        self,
        *,
        status: str = "",
        target_type: str = "",
        target_id: str = "",
        created_by_session_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        values: list[object] = []
        normalized_status = _text(status, maximum=20).lower()
        if normalized_status:
            if normalized_status not in {"scheduled", "paused", "running", "completed", "failed", "cancelled"}:
                raise ValueError("unsupported wake schedule status")
            clauses.append("s.status = ?")
            values.append(normalized_status)
        normalized_target_type = _text(target_type, maximum=20).lower()
        if normalized_target_type:
            if normalized_target_type not in {"session", "role"}:
                raise ValueError("unsupported wake target type")
            clauses.append("s.target_type = ?")
            values.append(normalized_target_type)
        normalized_target_id = _text(target_id, maximum=240)
        if normalized_target_id:
            clauses.append("(s.target_session_id = ? OR s.target_role_id = ?)")
            values.extend((normalized_target_id, normalized_target_id))
        owner = _text(created_by_session_id, maximum=240)
        if owner:
            clauses.append("s.created_by_session_id = ?")
            values.append(owner)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = _integer(limit, default=100, minimum=1, maximum=500)
        values.append(bounded_limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT s.*, r.run_id AS latest_run_id, r.state AS latest_run_state,
                       r.session_id AS latest_session_id, r.turn_id AS latest_turn_id,
                       r.error AS latest_run_error, r.started_at_ms AS latest_started_at_ms,
                       r.finished_at_ms AS latest_finished_at_ms
                FROM agent_wake_schedules AS s
                LEFT JOIN agent_wake_runs AS r ON r.run_id = s.last_run_id
                {where}
                ORDER BY
                    CASE s.status WHEN 'running' THEN 0 WHEN 'scheduled' THEN 1
                                      WHEN 'paused' THEN 2 ELSE 3 END,
                    COALESCE(s.next_wake_at_ms, 9223372036854775807),
                    s.updated_at_ms DESC
                LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        return [_schedule_payload(row, row) for row in rows]

    def runs(self, schedule_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        self.get(schedule_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_wake_runs WHERE schedule_id = ? ORDER BY started_at_ms DESC LIMIT ?",
                (schedule_id, _integer(limit, default=100, minimum=1, maximum=500)),
            ).fetchall()
        return [_run_payload(row) for row in rows]

    def schedule_for_terminal_event(
        self,
        event: AgentEventEnvelope,
    ) -> dict[str, object] | None:
        """Return the durable wake whose accepted turn emitted ``event``.

        The lookup deliberately includes already-finished runs.  Terminal
        observers run after the schedule ledger is settled, and recovery must
        be able to inspect that immutable failed run without reopening it.
        """

        if event.event_type not in {"turn_completed", "turn_failed"} or not event.turn_id:
            return None
        with self._connect() as conn:
            run = conn.execute(
                """
                SELECT * FROM agent_wake_runs
                WHERE session_id = ? AND turn_id = ?
                ORDER BY started_at_ms DESC LIMIT 1
                """,
                (event.session_id, event.turn_id),
            ).fetchone()
            if run is None:
                return None
            schedule = conn.execute(
                "SELECT * FROM agent_wake_schedules WHERE schedule_id = ?",
                (run["schedule_id"],),
            ).fetchone()
            if schedule is None:
                return None
        return _schedule_payload(schedule, run)

    def action(
        self,
        schedule_id: str,
        action: str,
        *,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        identifier = _required_id(schedule_id)
        requested = _text(action, maximum=20).lower()
        if requested not in {"pause", "resume", "cancel", "retry"}:
            raise ValueError("wake schedule action must be pause, resume, cancel, or retry")
        timestamp = _now_ms(now_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agent_wake_schedules WHERE schedule_id = ?", (identifier,)
            ).fetchone()
            if row is None:
                raise KeyError(identifier)
            current = str(row["status"])
            if requested == "pause":
                if current != "scheduled":
                    raise ValueError("only a scheduled wake can be paused")
                next_status = "paused"
                next_at = row["next_wake_at_ms"]
            elif requested == "resume":
                if current != "paused":
                    raise ValueError("only a paused wake can be resumed")
                next_status = "scheduled"
                next_at = max(timestamp + 1_000, int(row["next_wake_at_ms"] or 0))
            elif requested == "retry":
                if current not in {"failed", "completed"}:
                    raise ValueError("only a terminal wake can be retried")
                next_status = "scheduled"
                next_at = timestamp + 1_000
            else:
                if current == "running":
                    raise ValueError("a running wake cannot be cancelled after its Agent turn started")
                if current in {"cancelled", "completed"}:
                    raise ValueError("wake schedule is already terminal")
                next_status = "cancelled"
                next_at = None
            conn.execute(
                """
                UPDATE agent_wake_schedules
                SET status = ?, next_wake_at_ms = ?, last_error = '',
                    lease_token = '', lease_expires_at_ms = NULL, updated_at_ms = ?
                WHERE schedule_id = ?
                """,
                (next_status, next_at, timestamp, identifier),
            )
        return self.get(identifier)

    def cancel_for_root(
        self,
        schedule_id: str,
        *,
        reason: str,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Durably fence a wake even after its Agent turn has started."""

        identifier = _required_id(schedule_id)
        timestamp = _now_ms(now_ms)
        message = _text(reason, maximum=500)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agent_wake_schedules WHERE schedule_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise KeyError(identifier)
            current = str(row["status"])
            if current in {"completed", "failed", "cancelled"}:
                return _schedule_payload(row, self._latest_run_locked(conn, identifier))
            run_id = str(row["last_run_id"] or "")
            if current == "running" and run_id:
                conn.execute(
                    """
                    UPDATE agent_wake_runs
                    SET state = 'failed', finished_at_ms = ?, error = ?,
                        result_json = ?
                    WHERE run_id = ? AND state IN ('claimed', 'accepted')
                    """,
                    (
                        timestamp,
                        message,
                        json.dumps(
                            {
                                "status": "aborted",
                                "reason": message,
                                "source": "room_root_cancel",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        run_id,
                    ),
                )
            conn.execute(
                """
                UPDATE agent_wake_schedules
                SET status = 'cancelled', next_wake_at_ms = NULL,
                    last_error = ?, lease_token = '',
                    lease_expires_at_ms = NULL, updated_at_ms = ?
                WHERE schedule_id = ?
                """,
                (message, timestamp, identifier),
            )
        return self.get(identifier)

    def claim_due(
        self,
        *,
        now_ms: int | None = None,
        limit: int = 2,
        max_active: int = 2,
        lease_ms: int = _DEFAULT_LEASE_MS,
    ) -> list[dict[str, object]]:
        timestamp = _now_ms(now_ms)
        claims: list[dict[str, object]] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._recover_expired_locked(conn, timestamp)
            active_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_wake_schedules WHERE status = 'running'"
                ).fetchone()[0]
            )
            capacity = max(0, min(int(limit), max(1, int(max_active)) - active_count))
            if capacity == 0:
                return []
            rows = conn.execute(
                """
                SELECT * FROM agent_wake_schedules
                WHERE status = 'scheduled' AND next_wake_at_ms IS NOT NULL
                  AND next_wake_at_ms <= ?
                ORDER BY next_wake_at_ms ASC, created_at_ms ASC
                LIMIT ?
                """,
                (timestamp, _integer(capacity, default=2, minimum=1, maximum=8)),
            ).fetchall()
            for row in rows:
                schedule_id = str(row["schedule_id"])
                lease_token = uuid.uuid4().hex
                run_id = f"wake-run:{uuid.uuid4()}"
                attempt = int(row["run_count"] or 0) + 1
                due_at_ms = int(row["next_wake_at_ms"] or timestamp)
                cursor = conn.execute(
                    """
                    UPDATE agent_wake_schedules
                    SET status = 'running', run_count = ?, last_wake_at_ms = ?,
                        last_run_id = ?, lease_token = ?, lease_expires_at_ms = ?,
                        updated_at_ms = ?, last_error = ''
                    WHERE schedule_id = ? AND status = 'scheduled' AND next_wake_at_ms = ?
                    """,
                    (
                        attempt,
                        due_at_ms,
                        run_id,
                        lease_token,
                        timestamp + max(60_000, int(lease_ms)),
                        timestamp,
                        schedule_id,
                        due_at_ms,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                conn.execute(
                    """
                    INSERT INTO agent_wake_runs(
                        run_id, schedule_id, attempt, state, due_at_ms, started_at_ms
                    ) VALUES (?, ?, ?, 'claimed', ?, ?)
                    """,
                    (run_id, schedule_id, attempt, due_at_ms, timestamp),
                )
                claim = _schedule_payload(row, None)
                claim.update(
                    {
                        "runId": run_id,
                        "attempt": attempt,
                        "dueAtMs": due_at_ms,
                        "leaseToken": lease_token,
                    }
                )
                claims.append(claim)
        return claims

    def defer(
        self,
        run_id: str,
        *,
        reason: str,
        cause_code: str = "",
        delay_ms: int = 60_000,
        now_ms: int | None = None,
    ) -> None:
        timestamp = _now_ms(now_ms)
        normalized_cause_code = _text(cause_code, maximum=80)
        result_json = json.dumps(
            (
                {"causeCode": normalized_cause_code}
                if normalized_cause_code
                else {}
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self._active_run_locked(conn, run_id)
            conn.execute(
                """
                UPDATE agent_wake_runs
                SET state = 'deferred', finished_at_ms = ?, error = ?,
                    result_json = ?
                WHERE run_id = ?
                """,
                (
                    timestamp,
                    _text(reason, maximum=500),
                    result_json,
                    run_id,
                ),
            )
            conn.execute(
                """
                UPDATE agent_wake_schedules
                SET status = 'scheduled', next_wake_at_ms = ?,
                    run_count = MAX(0, run_count - 1), last_error = ?,
                    lease_token = '', lease_expires_at_ms = NULL, updated_at_ms = ?
                WHERE schedule_id = ? AND last_run_id = ?
                """,
                (
                    timestamp + max(5_000, int(delay_ms)),
                    _text(reason, maximum=500),
                    timestamp,
                    run["schedule_id"],
                    run_id,
                ),
            )

    def accept(
        self,
        run_id: str,
        *,
        session_id: str,
        turn_id: str,
        now_ms: int | None = None,
    ) -> None:
        timestamp = _now_ms(now_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._active_run_locked(conn, run_id)
            conn.execute(
                """
                UPDATE agent_wake_runs
                SET state = 'accepted', accepted_at_ms = ?, session_id = ?, turn_id = ?
                WHERE run_id = ?
                """,
                (timestamp, _required_id(session_id), _required_id(turn_id), run_id),
            )

    def fail_dispatch(
        self,
        run_id: str,
        *,
        error: str,
        now_ms: int | None = None,
    ) -> None:
        self._finish(run_id, succeeded=False, error=error, result={}, now_ms=now_ms)

    def finish_event(self, event: AgentEventEnvelope) -> bool:
        if event.event_type not in {"turn_completed", "turn_failed"} or not event.turn_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run_id FROM agent_wake_runs
                WHERE session_id = ? AND turn_id = ? AND state = 'accepted'
                ORDER BY started_at_ms DESC LIMIT 1
                """,
                (event.session_id, event.turn_id),
            ).fetchone()
        if row is None:
            return False
        self._finish(
            str(row["run_id"]),
            succeeded=event.event_type == "turn_completed",
            error=str(event.payload.get("error") or ""),
            result={
                "terminalEvent": event.event_type,
                "eventId": event.event_id,
                **(
                    {"failureKind": str(event.payload.get("failureKind"))}
                    if event.payload.get("failureKind")
                    else {}
                ),
                **(
                    {"exitCode": event.payload.get("exitCode")}
                    if "exitCode" in event.payload
                    else {}
                ),
            },
            now_ms=event.created_at_ms,
        )
        return True

    def _finish(
        self,
        run_id: str,
        *,
        succeeded: bool,
        error: str,
        result: Mapping[str, object],
        now_ms: int | None,
    ) -> None:
        timestamp = _now_ms(now_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self._active_run_locked(conn, run_id)
            schedule = conn.execute(
                "SELECT * FROM agent_wake_schedules WHERE schedule_id = ?",
                (run["schedule_id"],),
            ).fetchone()
            if schedule is None:
                raise KeyError(str(run["schedule_id"]))
            next_wake_at_ms = _next_wake(schedule, timestamp)
            schedule_status = "scheduled" if next_wake_at_ms is not None else ("completed" if succeeded else "failed")
            message = _text(error, maximum=500)
            conn.execute(
                """
                UPDATE agent_wake_runs
                SET state = ?, finished_at_ms = ?, error = ?, result_json = ?
                WHERE run_id = ?
                """,
                (
                    "completed" if succeeded else "failed",
                    timestamp,
                    message,
                    json.dumps(dict(result), ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )
            conn.execute(
                """
                UPDATE agent_wake_schedules
                SET status = ?, next_wake_at_ms = ?, last_error = ?,
                    lease_token = '', lease_expires_at_ms = NULL, updated_at_ms = ?
                WHERE schedule_id = ? AND last_run_id = ?
                """,
                (
                    schedule_status,
                    next_wake_at_ms,
                    "" if succeeded else message,
                    timestamp,
                    run["schedule_id"],
                    run_id,
                ),
            )

    def _recover_expired_locked(self, conn: sqlite3.Connection, timestamp: int) -> None:
        rows = conn.execute(
            """
            SELECT s.*, r.run_id, r.state AS run_state
            FROM agent_wake_schedules AS s
            JOIN agent_wake_runs AS r ON r.run_id = s.last_run_id
            WHERE s.status = 'running' AND s.lease_expires_at_ms IS NOT NULL
              AND s.lease_expires_at_ms <= ? AND r.state IN ('claimed', 'accepted')
            """,
            (timestamp,),
        ).fetchall()
        for row in rows:
            next_at = _next_wake(row, timestamp)
            conn.execute(
                """
                UPDATE agent_wake_runs
                SET state = 'failed', finished_at_ms = ?, error = ?
                WHERE run_id = ?
                """,
                (timestamp, "Agent Gateway restarted before the scheduled turn settled", row["run_id"]),
            )
            conn.execute(
                """
                UPDATE agent_wake_schedules
                SET status = ?, next_wake_at_ms = ?, last_error = ?,
                    lease_token = '', lease_expires_at_ms = NULL, updated_at_ms = ?
                WHERE schedule_id = ?
                """,
                (
                    "scheduled" if next_at is not None else "failed",
                    next_at,
                    "Agent Gateway restarted before the scheduled turn settled",
                    timestamp,
                    row["schedule_id"],
                ),
            )

    @staticmethod
    def _active_run_locked(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM agent_wake_runs WHERE run_id = ? AND state IN ('claimed', 'accepted')",
            (_required_id(run_id),),
        ).fetchone()
        if row is None:
            raise ValueError("wake run is no longer active")
        return row

    @staticmethod
    def _latest_run_locked(
        conn: sqlite3.Connection,
        schedule_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM agent_wake_runs
            WHERE schedule_id = ?
            ORDER BY started_at_ms DESC LIMIT 1
            """,
            (schedule_id,),
        ).fetchone()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


class AgentWakeScheduler:
    """Bounded Gateway-owned dispatcher for durable schedules.

    ``on_tick`` runs from this scheduler's existing poll loop.  It is a small
    maintenance seam for other Runtime-owned ledgers (such as Eval) and does
    not create another daemon or poller.
    """

    def __init__(
        self,
        *,
        store: AgentWakeScheduleStore,
        dispatch: Callable[[Mapping[str, object]], None],
        enabled: bool = True,
        poll_seconds: float = 1.0,
        max_parallel: int = 2,
        on_tick: Callable[[int | None], object] | None = None,
    ) -> None:
        self.store = store
        self.dispatch = dispatch
        self.on_tick = on_tick
        self.enabled = bool(enabled)
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.max_parallel = max(1, min(int(max_parallel), 4))
        self._stop = Event()
        self._notify = Event()
        self._terminal_observer: Callable[
            [AgentEventEnvelope, Mapping[str, object]], None
        ] | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_parallel,
            thread_name_prefix="agent-wake",
        )
        self._maintenance_lock = Lock()
        self._maintenance_future: Future[object] | None = None
        self._thread: Thread | None = None
        if self.enabled:
            self._thread = Thread(target=self._run, name="agent-wake-scheduler", daemon=True)
            self._thread.start()

    def wake(self) -> None:
        if self.enabled:
            self._notify.set()

    def bind_terminal_observer(
        self,
        observer: Callable[
            [AgentEventEnvelope, Mapping[str, object]], None
        ]
        | None,
    ) -> None:
        """Bind one post-ledger terminal observer.

        Ordering matters: the generic wake run is first made terminal, then a
        product adapter may create a new generation.  This avoids relying on
        the unordered observer set in ``AgentEventHub``.
        """

        self._terminal_observer = observer

    def observe_event(self, event: AgentEventEnvelope) -> None:
        if not self.store.finish_event(event):
            return
        schedule = self.store.schedule_for_terminal_event(event)
        try:
            if schedule is not None and self._terminal_observer is not None:
                self._terminal_observer(event, schedule)
        finally:
            self.wake()

    def run_due_once(self, *, now_ms: int | None = None) -> int:
        claims = self.store.claim_due(
            now_ms=now_ms,
            limit=self.max_parallel,
            max_active=self.max_parallel,
        )
        self._submit_maintenance(now_ms)
        for claim in claims:
            self._executor.submit(self._dispatch_safely, claim)
        return len(claims)

    def _submit_maintenance(self, now_ms: int | None) -> None:
        callback = self.on_tick
        if callback is None:
            return
        with self._maintenance_lock:
            if (
                self._maintenance_future is not None
                and not self._maintenance_future.done()
            ):
                return
            self._maintenance_future = self._executor.submit(callback, now_ms)

    def close(self) -> None:
        self._stop.set()
        self._notify.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._notify.wait(self.poll_seconds)
            self._notify.clear()
            if self._stop.is_set():
                break
            try:
                self.run_due_once()
            except Exception:
                # The durable ledger keeps the task visible for the next poll.
                pass

    def _dispatch_safely(self, claim: Mapping[str, object]) -> None:
        try:
            self.dispatch(claim)
        except Exception as exc:
            try:
                self.store.fail_dispatch(
                    str(claim.get("runId") or ""),
                    error=_public_error(exc),
                )
            except Exception:
                pass
        finally:
            self.wake()


def _next_wake(schedule: Mapping[str, object] | sqlite3.Row, timestamp: int) -> int | None:
    recurrence = str(schedule["recurrence_kind"] or "once")
    run_count = int(schedule["run_count"] or 0)
    max_runs = int(schedule["max_runs"] or 1)
    if recurrence == "once" or run_count >= max_runs:
        return None
    interval_days = _RECURRENCE_DAYS[recurrence] * max(
        1, int(schedule["recurrence_interval"] or 1)
    )
    timezone = ZoneInfo(str(schedule["timezone"] or "Asia/Shanghai"))
    base_ms = int(schedule["last_wake_at_ms"] or schedule["next_wake_at_ms"] or timestamp)
    candidate_at = datetime.fromtimestamp(base_ms / 1000, tz=timezone) + timedelta(
        days=interval_days
    )
    candidate = int(candidate_at.timestamp() * 1000)
    while candidate <= timestamp:
        candidate_at += timedelta(days=interval_days)
        candidate = int(candidate_at.timestamp() * 1000)
    return candidate


def _schedule_payload(
    row: sqlite3.Row,
    latest: sqlite3.Row | None,
) -> dict[str, object]:
    latest_payload: dict[str, object] = {}
    if latest is not None:
        keys = set(latest.keys())
        run_id_key = "run_id" if "run_id" in keys else "latest_run_id"
        state_key = "state" if "state" in keys else "latest_run_state"
        if latest[run_id_key]:
            result: dict[str, object] = {}
            if "result_json" in keys:
                try:
                    parsed_result = json.loads(str(latest["result_json"] or "{}"))
                except json.JSONDecodeError:
                    parsed_result = {}
                if isinstance(parsed_result, dict):
                    result = parsed_result
            latest_payload = {
                "runId": str(latest[run_id_key]),
                "state": str(latest[state_key] or ""),
                "sessionId": str(
                    (latest["session_id"] if "session_id" in keys else latest["latest_session_id"])
                    or ""
                ),
                "turnId": str(
                    (latest["turn_id"] if "turn_id" in keys else latest["latest_turn_id"])
                    or ""
                ),
                "error": str(
                    (latest["error"] if "error" in keys else latest["latest_run_error"])
                    or ""
                ),
                "startedAtMs": int(
                    (latest["started_at_ms"] if "started_at_ms" in keys else latest["latest_started_at_ms"])
                    or 0
                ),
                "finishedAtMs": int(
                    (latest["finished_at_ms"] if "finished_at_ms" in keys else latest["latest_finished_at_ms"])
                    or 0
                ),
                "result": result,
            }
    try:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
    except json.JSONDecodeError:
        metadata = {}
    return {
        "id": str(row["schedule_id"]),
        "title": str(row["title"]),
        "instruction": str(row["instruction"]),
        "targetType": str(row["target_type"]),
        "targetSessionId": str(row["target_session_id"]),
        "targetRoleId": canonical_agent_role_id(row["target_role_id"]),
        "targetRoleVersion": str(row["target_role_version"]),
        "createdBySessionId": str(row["created_by_session_id"]),
        "planningTaskId": str(row["planning_task_id"]),
        "timezone": str(row["timezone"]),
        "recurrenceKind": str(row["recurrence_kind"]),
        "recurrenceInterval": int(row["recurrence_interval"]),
        "maxRuns": int(row["max_runs"]),
        "runCount": int(row["run_count"]),
        "status": str(row["status"]),
        "nextWakeAtMs": int(row["next_wake_at_ms"] or 0),
        "lastWakeAtMs": int(row["last_wake_at_ms"] or 0),
        "lastError": str(row["last_error"] or ""),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "metadata": metadata if isinstance(metadata, dict) else {},
        "latestRun": latest_payload,
    }


def _run_payload(row: sqlite3.Row) -> dict[str, object]:
    try:
        result = json.loads(str(row["result_json"] or "{}"))
    except json.JSONDecodeError:
        result = {}
    return {
        "id": str(row["run_id"]),
        "scheduleId": str(row["schedule_id"]),
        "attempt": int(row["attempt"]),
        "state": str(row["state"]),
        "dueAtMs": int(row["due_at_ms"]),
        "startedAtMs": int(row["started_at_ms"]),
        "acceptedAtMs": int(row["accepted_at_ms"] or 0),
        "finishedAtMs": int(row["finished_at_ms"] or 0),
        "sessionId": str(row["session_id"] or ""),
        "turnId": str(row["turn_id"] or ""),
        "error": str(row["error"] or ""),
        "result": result if isinstance(result, dict) else {},
    }


def _text(value: object, *, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _required_id(value: object) -> str:
    text = _text(value, maximum=240)
    if not text or any(ord(char) < 32 for char in text):
        raise ValueError("wake schedule identifier is invalid")
    return text


def _integer(
    value: object,
    *,
    default: int = 0,
    minimum: int = -(2**63),
    maximum: int = 2**63 - 1,
) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _now_ms(value: int | None) -> int:
    return int(time.time() * 1000) if value is None else int(value)


def _public_error(error: BaseException) -> str:
    return _text(error, maximum=500) or type(error).__name__
