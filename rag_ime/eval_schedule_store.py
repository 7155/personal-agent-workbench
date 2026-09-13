"""Durable, daemon-free scheduling ledger for deterministic Eval runs.

This module deliberately does not dispatch Agent turns or write Memory/Knowledge.
An owning runtime may call :class:`EvalScheduleRunner` from an existing lifecycle
and inject the actual evaluator.  The ledger only stores bounded suite identity,
lease state, and an EvalRun reference; it never manufactures or persists an
EvalRun on behalf of an evaluator.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from .db import apply_database_migrations
from .vertical_agent_harness import (
    VerticalSuiteResolutionError,
    resolve_builtin_vertical_suite,
)


_RECURRENCE_DAYS = {"daily": 1, "weekly": 7}
_DEFAULT_LEASE_MS = 6 * 60 * 60 * 1000


class EvalScheduleExecutionError(RuntimeError):
    """A stable, safe error code for a scheduled evaluator boundary."""

    def __init__(self, error_code: str) -> None:
        self.error_code = _text(error_code, maximum=120)
        super().__init__(self.error_code)


class EvalScheduleValidationError(ValueError):
    """A schedule request failed a stable pre-persistence validation."""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = str(code)
        self.error_code = self.code
        super().__init__(message)


class EvalScheduleStore:
    """SQLite schedule/run ledger; no background thread is created."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> int:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            return apply_database_migrations(conn).current_version

    def create(
        self,
        payload: Mapping[str, object],
        *,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _now_ms(now_ms)
        schedule_id = _id(payload.get("scheduleId"), prefix="eval-schedule:")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM eval_schedules WHERE schedule_id = ?", (schedule_id,)
            ).fetchone()
            normalized = self.validate_create(
                payload,
                # A delayed retry of an already-created request is compared
                # against its immutable initial due time. Past-due rejection
                # applies only when first creating the schedule.
                now_ms=0 if existing is not None else timestamp,
            )
            if existing is not None:
                existing_definition = {
                    "suiteId": str(existing["suite_id"]),
                    "suiteRevision": str(existing["suite_revision"]),
                    "recurrenceKind": str(existing["recurrence_kind"]),
                    "recurrenceInterval": int(existing["recurrence_interval"]),
                    "maxRuns": int(existing["max_runs"]),
                    "nextDueAtMs": int(existing["initial_due_at_ms"]),
                }
                if existing_definition != normalized:
                    raise ValueError("eval schedule identity was rebound to different content")
                return _schedule_payload(existing, _latest_run_payload(conn, schedule_id))
            conn.execute(
                """
                INSERT INTO eval_schedules(
                    schedule_id, suite_id, suite_revision, recurrence_kind,
                    recurrence_interval, max_runs, initial_due_at_ms, next_due_at_ms,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    normalized["suiteId"],
                    normalized["suiteRevision"],
                    normalized["recurrenceKind"],
                    normalized["recurrenceInterval"],
                    normalized["maxRuns"],
                    normalized["nextDueAtMs"],
                    normalized["nextDueAtMs"],
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(schedule_id)

    def validate_create(
        self,
        payload: Mapping[str, object],
        *,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _now_ms(now_ms)
        suite_id = _text(payload.get("suiteId"), maximum=200)
        if not suite_id:
            raise ValueError("eval suiteId is required")
        suite_revision = _text(payload.get("suiteRevision"), maximum=120)
        if not suite_revision:
            raise ValueError("eval suiteRevision is required")
        try:
            resolve_builtin_vertical_suite(suite_id, suite_revision)
        except VerticalSuiteResolutionError as exc:
            raise EvalScheduleValidationError(
                f"eval schedule {exc}",
                code=exc.code,
            ) from exc
        recurrence = _text(payload.get("recurrenceKind"), maximum=20).lower()
        if recurrence not in _RECURRENCE_DAYS:
            raise ValueError("eval recurrenceKind must be daily or weekly")
        interval = _strict_int(payload.get("recurrenceInterval"), default=1)
        if not 1 <= interval <= 30:
            raise ValueError("eval recurrenceInterval must be between 1 and 30")
        max_runs = _strict_int(payload.get("maxRuns"), default=30)
        if not 1 <= max_runs <= 100:
            raise ValueError("eval maxRuns must be between 1 and 100")
        next_due = _strict_int(payload.get("nextDueAtMs"), default=0)
        if next_due < timestamp:
            raise ValueError("eval nextDueAtMs must not be in the past")
        return {
            "suiteId": suite_id,
            "suiteRevision": suite_revision,
            "recurrenceKind": recurrence,
            "recurrenceInterval": interval,
            "maxRuns": max_runs,
            "nextDueAtMs": next_due,
        }

    def get(self, schedule_id: str) -> dict[str, object]:
        identifier = _required_id(schedule_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM eval_schedules WHERE schedule_id = ?", (identifier,)
            ).fetchone()
            if row is None:
                raise KeyError(identifier)
            return _schedule_payload(row, _latest_run_payload(conn, identifier))

    def list(self, *, limit: int = 100) -> list[dict[str, object]]:
        """Return a bounded, stable-order projection of every Eval schedule.

        The schedule ledger is local-only, so this projection deliberately
        contains only public schedule/run fields.  Lease tokens remain inside
        the claim/settlement seam and are never copied into a list response.
        ``created_at_ms`` plus the immutable schedule ID gives callers a
        deterministic order even when several schedules share a timestamp.
        """

        bounded = _strict_int(limit, default=100)
        if not 1 <= bounded <= 500:
            raise ValueError("eval schedule list limit must be between 1 and 500")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM eval_schedules "
                "ORDER BY created_at_ms ASC, schedule_id ASC LIMIT ?",
                (bounded,),
            ).fetchall()
            return [
                _schedule_payload(row, _latest_run_payload(conn, str(row["schedule_id"])))
                for row in rows
            ]

    def runs(self, schedule_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        identifier = _required_id(schedule_id)
        self.get(identifier)
        bounded = _strict_int(limit, default=100)
        if not 1 <= bounded <= 500:
            raise ValueError("eval schedule run limit must be between 1 and 500")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM eval_schedule_runs WHERE schedule_id = "
                "? ORDER BY claimed_at_ms DESC, run_id DESC LIMIT ?",
                (identifier, bounded),
            ).fetchall()
        return [_run_payload(row) for row in rows]

    def action(self, schedule_id: str, action: str, *, now_ms: int | None = None) -> dict[str, object]:
        """Change future execution without altering pinned suite or past runs."""
        timestamp = _now_ms(now_ms)
        identifier = _required_id(schedule_id)
        if action not in {"pause", "resume", "cancel", "retry"}:
            raise ValueError("unknown Eval schedule action")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM eval_schedules WHERE schedule_id = ?", (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            status = _schedule_payload(row)["status"]
            control = "active"
            next_due = int(row["next_due_at_ms"])
            run_status = str(row["status"])
            if action == "pause" and status == "scheduled":
                control = "paused"
            elif action == "resume" and status == "paused":
                next_due = max(next_due, timestamp + 1_000)
            elif action == "cancel" and status in {"scheduled", "paused", "failed"}:
                control = "cancelled"
            elif action == "retry" and status in {"failed", "completed"} and int(row["run_count"]) < 100:
                run_status = "scheduled"
                next_due = timestamp + 1_000
            else:
                raise ValueError("Eval schedule cannot perform this action in its current state")
            conn.execute(
                "UPDATE eval_schedules SET control_state = ?, status = ?, next_due_at_ms = ?, updated_at_ms = ? WHERE schedule_id = ?",
                (control, run_status, next_due, timestamp, identifier),
            )
        return self.get(identifier)

    def claim_due(
        self,
        *,
        now_ms: int | None = None,
        limit: int = 1,
        max_active: int = 1,
        lease_ms: int = _DEFAULT_LEASE_MS,
    ) -> list[dict[str, object]]:
        timestamp = _now_ms(now_ms)
        bounded_limit = _strict_int(limit, default=1)
        bounded_active = _strict_int(max_active, default=1)
        bounded_lease = _strict_int(lease_ms, default=_DEFAULT_LEASE_MS)
        if not 1 <= bounded_limit <= 100 or not 1 <= bounded_active <= 100:
            raise ValueError("eval claim bounds must be between 1 and 100")
        if bounded_lease < 60_000:
            raise ValueError("eval lease must be at least one minute")
        claims: list[dict[str, object]] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._recover_expired_locked(conn, timestamp)
            active = int(
                conn.execute(
                    "SELECT COUNT(*) FROM eval_schedules WHERE status = 'running'"
                ).fetchone()[0]
            )
            capacity = min(bounded_limit, max(0, bounded_active - active))
            if capacity == 0:
                return []
            rows = conn.execute(
                "SELECT * FROM eval_schedules WHERE status = 'scheduled' AND control_state = 'active' "
                "AND next_due_at_ms <= ? ORDER BY next_due_at_ms, created_at_ms LIMIT ?",
                (timestamp, capacity),
            ).fetchall()
            for row in rows:
                schedule_id = str(row["schedule_id"])
                run_id = f"eval-run:{uuid.uuid4()}"
                token = uuid.uuid4().hex
                attempt = int(row["run_count"]) + 1
                due_at = int(row["next_due_at_ms"])
                changed = conn.execute(
                    "UPDATE eval_schedules SET status='running', run_count=?, "
                    "last_due_at_ms=?, last_run_id=?, lease_token=?, "
                    "lease_expires_at_ms=?, updated_at_ms=? "
                    "WHERE schedule_id=? AND status='scheduled' AND next_due_at_ms=?",
                    (
                        attempt,
                        due_at,
                        run_id,
                        token,
                        timestamp + bounded_lease,
                        timestamp,
                        schedule_id,
                        due_at,
                    ),
                )
                if changed.rowcount != 1:
                    continue
                conn.execute(
                    "INSERT INTO eval_schedule_runs(run_id, schedule_id, attempt, state, "
                    "due_at_ms, claimed_at_ms, lease_token) VALUES (?, ?, ?, 'claimed', ?, ?, ?)",
                    (run_id, schedule_id, attempt, due_at, timestamp, token),
                )
                claims.append(_claim_payload(row, run_id, token, attempt, due_at))
        return claims

    def succeed(
        self,
        run_id: str,
        *,
        lease_token: str,
        eval_run_id: str = "",
        now_ms: int | None = None,
    ) -> dict[str, object]:
        return self._settle(
            run_id,
            lease_token=lease_token,
            state="succeeded",
            eval_run_id=eval_run_id,
            error_code="",
            now_ms=now_ms,
        )

    def fail(
        self,
        run_id: str,
        *,
        lease_token: str,
        error_code: str,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        safe_error = _text(error_code, maximum=120)
        if not safe_error:
            raise ValueError("eval failure requires an error code")
        return self._settle(
            run_id,
            lease_token=lease_token,
            state="failed",
            eval_run_id="",
            error_code=safe_error,
            now_ms=now_ms,
        )

    def _settle(
        self,
        run_id: str,
        *,
        lease_token: str,
        state: str,
        eval_run_id: str,
        error_code: str,
        now_ms: int | None,
    ) -> dict[str, object]:
        identifier = _required_id(run_id)
        token = _required_token(lease_token)
        timestamp = _now_ms(now_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT * FROM eval_schedule_runs WHERE run_id = ?", (identifier,)
            ).fetchone()
            if run is None:
                raise KeyError(identifier)
            current = str(run["state"])
            if current != "claimed":
                if (
                    current == state
                    and str(run["eval_run_id"] or "") == _text(eval_run_id, maximum=240)
                    and str(run["error_code"] or "") == error_code
                ):
                    return _run_payload(run)
                raise ValueError("eval run is no longer active")
            schedule = conn.execute(
                "SELECT * FROM eval_schedules WHERE schedule_id = ?", (run["schedule_id"],)
            ).fetchone()
            if schedule is None:
                raise KeyError(str(run["schedule_id"]))
            if str(run["lease_token"]) != token or str(schedule["last_run_id"]) != identifier or str(schedule["lease_token"]) != token:
                raise ValueError("eval lease token is stale")
            safe_eval_id = _text(eval_run_id, maximum=240)
            next_due = _next_due(schedule, timestamp)
            terminal_status = "completed" if state == "succeeded" else "failed"
            next_status = "scheduled" if next_due is not None else terminal_status
            conn.execute(
                "UPDATE eval_schedule_runs SET state=?, finished_at_ms=?, eval_run_id=?, error_code=? WHERE run_id=?",
                (state, timestamp, safe_eval_id, error_code, identifier),
            )
            conn.execute(
                "UPDATE eval_schedules SET status=?, next_due_at_ms=?, last_error_code=?, "
                "lease_token='', lease_expires_at_ms=NULL, updated_at_ms=? WHERE schedule_id=? AND last_run_id=?",
                (next_status, next_due or 0, error_code, timestamp, schedule["schedule_id"], identifier),
            )
            return _run_payload(
                conn.execute("SELECT * FROM eval_schedule_runs WHERE run_id=?", (identifier,)).fetchone()
            )

    def _recover_expired_locked(self, conn: sqlite3.Connection, timestamp: int) -> None:
        rows = conn.execute(
            "SELECT s.*, r.run_id FROM eval_schedules s JOIN eval_schedule_runs r "
            "ON r.run_id=s.last_run_id WHERE s.status='running' "
            "AND s.lease_expires_at_ms IS NOT NULL AND s.lease_expires_at_ms <= ? "
            "AND r.state='claimed'",
            (timestamp,),
        ).fetchall()
        for schedule in rows:
            run_id = str(schedule["run_id"])
            next_due = _next_due(schedule, timestamp)
            conn.execute(
                "UPDATE eval_schedule_runs SET state='failed', finished_at_ms=?, error_code='lease_expired' WHERE run_id=?",
                (timestamp, run_id),
            )
            conn.execute(
                "UPDATE eval_schedules SET status=?, next_due_at_ms=?, last_error_code='lease_expired', "
                "lease_token='', lease_expires_at_ms=NULL, updated_at_ms=? WHERE schedule_id=?",
                ("scheduled" if next_due is not None else "failed", next_due or 0, timestamp, schedule["schedule_id"]),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            with conn:
                yield conn
        finally:
            conn.close()


class EvalScheduleRunner:
    """Synchronous adapter for an injected evaluator; intentionally no daemon.

    The evaluator owns creation and persistence of the immutable EvalRun.  The
    runner only accepts its stable identity after an authority lookup and an
    exact suite identity check, then settles the schedule lease.  This keeps
    scheduled execution from claiming success for a fabricated, unpersisted,
    or differently-versioned result.
    """

    def __init__(
        self,
        *,
        store: EvalScheduleStore,
        execute: Callable[[dict[str, object]], Mapping[str, object]],
        eval_run_exists: Callable[[str], bool] | None = None,
        eval_run_loader: Callable[[str], Mapping[str, object] | None] | None = None,
        max_parallel: int = 1,
    ) -> None:
        self.store = store
        self.execute = execute
        self.eval_run_exists = eval_run_exists
        self.eval_run_loader = eval_run_loader
        self.max_parallel = max(1, min(int(max_parallel), 100))

    def run_due_once(self, *, now_ms: int | None = None) -> int:
        claims = self.store.claim_due(
            now_ms=now_ms,
            limit=self.max_parallel,
            max_active=self.max_parallel,
        )
        for claim in claims:
            try:
                result = self.execute(dict(claim))
                if not isinstance(result, Mapping):
                    raise EvalScheduleExecutionError("executor_invalid_result")
                eval_run_id = _text(result.get("evalRunId"), maximum=240)
                if not eval_run_id:
                    raise EvalScheduleExecutionError("eval_run_missing")
                if self.eval_run_exists is not None and not self.eval_run_exists(eval_run_id):
                    raise EvalScheduleExecutionError("eval_run_not_found")
                self._require_matching_suite_binding(claim, eval_run_id)
                self.store.succeed(
                    str(claim["runId"]),
                    lease_token=str(claim["leaseToken"]),
                    eval_run_id=eval_run_id,
                    now_ms=now_ms,
                )
            except Exception as exc:
                error_code = (
                    exc.error_code
                    if isinstance(exc, EvalScheduleExecutionError)
                    else "executor_failed"
                )
                self.store.fail(
                    str(claim["runId"]),
                    lease_token=str(claim["leaseToken"]),
                    error_code=error_code,
                    now_ms=now_ms,
                )
        return len(claims)

    def _require_matching_suite_binding(
        self,
        claim: Mapping[str, object],
        eval_run_id: str,
    ) -> None:
        """Require the durable EvalRun to name this claim's exact suite.

        ``eval_run_exists`` remains a compatibility probe for callers that
        only need the legacy not-found error.  Scheduled success always needs
        the loader as well: existence alone cannot prove that a result belongs
        to the pinned built-in suite revision.
        """

        if self.eval_run_loader is None:
            raise EvalScheduleExecutionError("eval_run_binding_unavailable")
        try:
            payload = self.eval_run_loader(eval_run_id)
        except Exception as exc:
            raise EvalScheduleExecutionError("eval_run_lookup_failed") from exc
        if not isinstance(payload, Mapping):
            raise EvalScheduleExecutionError("eval_run_not_found")
        binding = payload.get("suiteBinding")
        if not isinstance(binding, Mapping):
            raise EvalScheduleExecutionError("eval_run_suite_binding_missing")
        suite_id = binding.get("suiteId")
        suite_revision = binding.get("suiteRevision")
        if (
            not isinstance(suite_id, str)
            or not isinstance(suite_revision, str)
            or not suite_id
            or not suite_revision
        ):
            raise EvalScheduleExecutionError("eval_run_suite_binding_missing")
        if (
            suite_id != claim.get("suiteId")
            or suite_revision != claim.get("suiteRevision")
        ):
            raise EvalScheduleExecutionError("eval_run_suite_mismatch")


def _next_due(schedule: Mapping[str, object], timestamp: int) -> int | None:
    run_count = int(schedule["run_count"])
    if run_count >= int(schedule["max_runs"]):
        return None
    interval_days = _RECURRENCE_DAYS[str(schedule["recurrence_kind"])] * int(schedule["recurrence_interval"])
    base = int(schedule["next_due_at_ms"])
    timezone = ZoneInfo("UTC")
    candidate = int((datetime.fromtimestamp(base / 1000, tz=timezone) + timedelta(days=interval_days)).timestamp() * 1000)
    while candidate <= timestamp:
        candidate += interval_days * 86_400_000
    return candidate


def _schedule_payload(row: sqlite3.Row, latest: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "id": str(row["schedule_id"]),
        "suiteId": str(row["suite_id"]),
        "suiteRevision": str(row["suite_revision"]),
        "recurrenceKind": str(row["recurrence_kind"]),
        "recurrenceInterval": int(row["recurrence_interval"]),
        "maxRuns": int(row["max_runs"]),
        "runCount": int(row["run_count"]),
        "status": str(row["control_state"]) if row["control_state"] != "active" else str(row["status"]),
        "initialDueAtMs": int(row["initial_due_at_ms"]),
        "nextDueAtMs": int(row["next_due_at_ms"]),
        "lastErrorCode": str(row["last_error_code"] or ""),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "latestRun": latest or {},
    }


def _latest_run_payload(conn: sqlite3.Connection, schedule_id: str) -> dict[str, object] | None:
    row = conn.execute(
        "SELECT * FROM eval_schedule_runs WHERE schedule_id=? "
        "ORDER BY claimed_at_ms DESC, run_id DESC LIMIT 1",
        (schedule_id,),
    ).fetchone()
    return _run_payload(row) if row is not None else None


def _claim_payload(row: sqlite3.Row, run_id: str, token: str, attempt: int, due_at: int) -> dict[str, object]:
    return {
        "scheduleId": str(row["schedule_id"]),
        "suiteId": str(row["suite_id"]),
        "suiteRevision": str(row["suite_revision"]),
        "recurrenceKind": str(row["recurrence_kind"]),
        "attempt": attempt,
        "runId": run_id,
        "leaseToken": token,
        "dueAtMs": due_at,
    }


def _run_payload(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["run_id"]),
        "scheduleId": str(row["schedule_id"]),
        "attempt": int(row["attempt"]),
        "state": str(row["state"]),
        "dueAtMs": int(row["due_at_ms"]),
        "claimedAtMs": int(row["claimed_at_ms"]),
        "finishedAtMs": int(row["finished_at_ms"] or 0),
        "evalRunId": str(row["eval_run_id"] or ""),
        "errorCode": str(row["error_code"] or ""),
    }


def _text(value: object, *, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _id(value: object, *, prefix: str) -> str:
    candidate = _text(value, maximum=240) if value is not None else f"{prefix}{uuid.uuid4()}"
    if not candidate.startswith(prefix) or any(ord(char) < 32 for char in candidate):
        raise ValueError(f"eval schedule id must start with {prefix}")
    return candidate


def _required_id(value: object) -> str:
    candidate = _text(value, maximum=240)
    if not candidate or any(ord(char) < 32 for char in candidate):
        raise ValueError("eval identifier is invalid")
    return candidate


def _required_token(value: object) -> str:
    token = _text(value, maximum=128)
    if not token or any(ord(char) < 32 for char in token):
        raise ValueError("eval lease token is invalid")
    return token


def _strict_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("eval integer cannot be boolean")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("eval integer is invalid") from exc
    if str(parsed) != str(value).strip() and not isinstance(value, int):
        raise ValueError("eval integer must be integral")
    return parsed


def _now_ms(value: int | None) -> int:
    return int(time.time() * 1000) if value is None else int(value)
