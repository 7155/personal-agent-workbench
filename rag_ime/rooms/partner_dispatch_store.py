from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Condition

from rag_ime.db import apply_database_migrations


_TERMINAL_STATES = frozenset(
    {
        "review",
        "blocked",
        "failed",
        "aborted",
        "accepted",
        "returned",
        "cancelled",
    }
)
_WAKEABLE_TERMINAL_STATES = frozenset(
    {"review", "blocked", "failed", "aborted", "accepted"}
)


class AgentRoomPartnerDispatchStore:
    """Durable receipt and wake ledger for asynchronous Room delegation."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._changed = Condition()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)

    def register(
        self,
        *,
        child_dispatch_id: str,
        room_id: str,
        root_id: str,
        parent_dispatch_id: str,
        tool_call_id: str,
        source_participant_id: str,
        source_session_id: str,
        target_participant_id: str,
        target_session_id: str,
        work_item_id: str,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _now_ms(now_ms)
        values = {
            "child_dispatch_id": _required(child_dispatch_id),
            "room_id": _required(room_id),
            "root_id": _required(root_id),
            "parent_dispatch_id": _required(parent_dispatch_id),
            "tool_call_id": _required(tool_call_id),
            "source_participant_id": _required(source_participant_id),
            "source_session_id": _required(source_session_id),
            "target_participant_id": _required(target_participant_id),
            "target_session_id": _required(target_session_id),
            "work_item_id": _required(work_item_id),
        }
        with self._connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_room_partner_dispatches
                WHERE room_id = ? AND root_id = ? AND tool_call_id = ?
                """,
                (values["room_id"], values["root_id"], values["tool_call_id"]),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO agent_room_partner_dispatches(
                        child_dispatch_id, room_id, root_id, parent_dispatch_id,
                        tool_call_id, source_participant_id, source_session_id,
                        target_participant_id, target_session_id, work_item_id,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        values["child_dispatch_id"],
                        values["room_id"],
                        values["root_id"],
                        values["parent_dispatch_id"],
                        values["tool_call_id"],
                        values["source_participant_id"],
                        values["source_session_id"],
                        values["target_participant_id"],
                        values["target_session_id"],
                        values["work_item_id"],
                        timestamp,
                        timestamp,
                    ),
                )
                row = self._row(conn, values["child_dispatch_id"])
            else:
                for key, column in (
                    ("child_dispatch_id", "child_dispatch_id"),
                    ("parent_dispatch_id", "parent_dispatch_id"),
                    ("source_participant_id", "source_participant_id"),
                    ("source_session_id", "source_session_id"),
                    ("target_participant_id", "target_participant_id"),
                    ("target_session_id", "target_session_id"),
                    ("work_item_id", "work_item_id"),
                ):
                    if str(row[column]) != values[key]:
                        raise ValueError(
                            "Room delegate idempotency key was reused for a different dispatch"
                        )
        return _payload(row)

    def get(self, child_dispatch_id: str) -> dict[str, object]:
        with self._connect() as conn:
            return _payload(self._row(conn, child_dispatch_id))

    def get_by_tool(
        self,
        *,
        room_id: str,
        root_id: str,
        tool_call_id: str,
    ) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_room_partner_dispatches
                WHERE room_id = ? AND root_id = ? AND tool_call_id = ?
                """,
                (_required(room_id), _required(root_id), _required(tool_call_id)),
            ).fetchone()
        return _payload(row) if row is not None else None

    def get_by_work(self, work_item_id: str) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_room_partner_dispatches
                WHERE work_item_id = ? ORDER BY updated_at_ms DESC LIMIT 1
                """,
                (_required(work_item_id),),
            ).fetchone()
            if row is None:
                raise KeyError(work_item_id)
        return _payload(row)

    def mark_dispatched(
        self,
        child_dispatch_id: str,
        *,
        target_session_turn_id: str = "",
        now_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _now_ms(now_ms)
        turn_id = str(target_session_turn_id or "").strip()[:320]
        with self._connect(immediate=True) as conn:
            row = self._row(conn, child_dispatch_id)
            if str(row["status"]) not in {"prepared", "dispatched"}:
                return _payload(row)
            existing_turn_id = str(row["target_session_turn_id"] or "")
            if existing_turn_id and turn_id and existing_turn_id != turn_id:
                raise ValueError(
                    "Room delegate idempotency key resolved to a different target turn"
                )
            conn.execute(
                """
                UPDATE agent_room_partner_dispatches
                SET status = 'dispatched',
                    target_session_turn_id = CASE
                        WHEN ? <> '' THEN ? ELSE target_session_turn_id END,
                    updated_at_ms = ?
                WHERE child_dispatch_id = ?
                  AND status IN ('prepared', 'dispatched')
                """,
                (turn_id, turn_id, timestamp, child_dispatch_id),
            )
            row = self._row(conn, child_dispatch_id)
        self._notify()
        return _payload(row)

    def record_result(
        self,
        child_dispatch_id: str,
        result: str,
        *,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _now_ms(now_ms)
        with self._connect(immediate=True) as conn:
            row = self._row(conn, child_dispatch_id)
            if str(row["status"]) not in {"prepared", "dispatched"}:
                return _payload(row)
            conn.execute(
                """
                UPDATE agent_room_partner_dispatches
                SET result_text = ?, updated_at_ms = ?
                WHERE child_dispatch_id = ?
                """,
                (str(result or "")[:16_000], timestamp, child_dispatch_id),
            )
            row = self._row(conn, child_dispatch_id)
        self._notify()
        return _payload(row)

    def settle(
        self,
        child_dispatch_id: str,
        *,
        status: str,
        result: str,
        completion_source: str,
        post_id: str = "",
        error: str = "",
        now_ms: int | None = None,
    ) -> dict[str, object]:
        normalized_status = str(status or "").strip()
        if normalized_status not in _TERMINAL_STATES:
            raise ValueError("unsupported Room Partner terminal state")
        timestamp = _now_ms(now_ms)
        bounded_result = str(result or "")[:16_000]
        bounded_source = str(completion_source or "")[:80]
        bounded_post_id = str(post_id or "")[:320]
        bounded_error = str(error or "")[:2_000]
        with self._connect(immediate=True) as conn:
            row = self._row(conn, child_dispatch_id)
            if str(row["status"]) not in {"prepared", "dispatched"}:
                return _payload(row)
            changed = any(
                (
                    str(row["status"]) != normalized_status,
                    str(row["result_text"]) != bounded_result,
                    str(row["completion_source"]) != bounded_source,
                    str(row["post_id"]) != bounded_post_id,
                    str(row["error"]) != bounded_error,
                )
            )
            if changed:
                conn.execute(
                    """
                    UPDATE agent_room_partner_dispatches
                    SET status = ?, result_text = ?, completion_source = ?,
                        post_id = ?, error = ?, wake_generation = wake_generation + 1,
                        wake_schedule_id = '', wake_state = CASE
                            WHEN ? = 'cancelled' THEN 'cancelled'
                            ELSE 'pending' END,
                        updated_at_ms = ?, completed_at_ms = ?
                    WHERE child_dispatch_id = ?
                    """,
                    (
                        normalized_status,
                        bounded_result,
                        bounded_source,
                        bounded_post_id,
                        bounded_error,
                        normalized_status,
                        timestamp,
                        timestamp,
                        child_dispatch_id,
                    ),
                )
            row = self._row(conn, child_dispatch_id)
        self._notify()
        return _payload(row)

    def mark_wake(
        self,
        child_dispatch_id: str,
        *,
        generation: int,
        state: str,
        schedule_id: str = "",
        error: str = "",
        now_ms: int | None = None,
    ) -> dict[str, object]:
        if state not in {"scheduled", "delivered", "failed", "cancelled"}:
            raise ValueError("unsupported Room Partner wake state")
        timestamp = _now_ms(now_ms)
        with self._connect(immediate=True) as conn:
            row = self._row(conn, child_dispatch_id)
            if int(row["wake_generation"]) != int(generation):
                return _payload(row)
            if str(row["wake_state"]) in {"cancelled", "failed"} and state != str(
                row["wake_state"]
            ):
                return _payload(row)
            conn.execute(
                """
                UPDATE agent_room_partner_dispatches
                SET wake_state = ?, wake_schedule_id = CASE
                        WHEN ? <> '' THEN ? ELSE wake_schedule_id END,
                    error = CASE WHEN ? <> '' THEN ? ELSE error END,
                    updated_at_ms = ?
                WHERE child_dispatch_id = ? AND wake_generation = ?
                """,
                (
                    state,
                    schedule_id,
                    schedule_id,
                    error,
                    str(error or "")[:2_000],
                    timestamp,
                    child_dispatch_id,
                    int(generation),
                ),
            )
            row = self._row(conn, child_dispatch_id)
        self._notify()
        return _payload(row)

    def record_review(
        self,
        child_dispatch_id: str,
        *,
        accepted: bool,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _now_ms(now_ms)
        with self._connect(immediate=True) as conn:
            row = self._row(conn, child_dispatch_id)
            if str(row["status"]) == "cancelled":
                return _payload(row)
            if str(row["status"]) != "review":
                raise ValueError("Room Partner dispatch must be in review")
            conn.execute(
                """
                UPDATE agent_room_partner_dispatches
                SET status = ?,
                    wake_state = CASE
                        WHEN wake_state IN ('pending', 'scheduled')
                        THEN 'cancelled' ELSE wake_state END,
                    updated_at_ms = ?, completed_at_ms = ?
                WHERE child_dispatch_id = ? AND status = 'review'
                """,
                (
                    "accepted" if accepted else "returned",
                    timestamp,
                    timestamp,
                    child_dispatch_id,
                ),
            )
            row = self._row(conn, child_dispatch_id)
        self._notify()
        return _payload(row)

    def pending_wakes(self, *, limit: int = 200) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_room_partner_dispatches
                WHERE wake_state = 'pending'
                ORDER BY updated_at_ms ASC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [_payload(row) for row in rows]

    def scheduled_wakes(self, *, limit: int = 200) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_room_partner_dispatches
                WHERE wake_state = 'scheduled'
                ORDER BY updated_at_ms ASC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [_payload(row) for row in rows]

    def delivered_wakes(self, *, limit: int = 200) -> list[dict[str, object]]:
        """Return terminal deliveries whose Facilitator wake already started."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_room_partner_dispatches
                WHERE status IN ('review', 'blocked', 'failed', 'aborted', 'accepted')
                  AND wake_state IN ('scheduled', 'delivered')
                ORDER BY updated_at_ms ASC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [_payload(row) for row in rows]

    def failed_wakes(self, *, limit: int = 200) -> list[dict[str, object]]:
        """Return terminal dispatches whose projected wake is failed."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_room_partner_dispatches
                WHERE status IN ('review', 'blocked', 'failed', 'aborted', 'accepted')
                  AND wake_state = 'failed'
                ORDER BY updated_at_ms ASC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [_payload(row) for row in rows]

    def terminal_result_candidates(
        self,
        *,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        """Return accepted dispatches whose terminal wake has run or settled.

        The caller must still prove that the wake run completed and that every
        WorkItem under the Root has an explicit two-axis acceptance.  This
        query only makes a missed terminal receipt discoverable after restart.
        """

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT d.* FROM agent_room_partner_dispatches AS d
                WHERE d.status = 'accepted'
                  AND d.wake_state IN ('scheduled', 'delivered', 'failed')
                  AND (
                    d.wake_state = 'failed'
                    OR NOT EXISTS (
                      SELECT 1
                      FROM agent_room_public_projection_receipts AS p
                      WHERE p.projection_key =
                        'room-terminal-result:' || d.room_id || ':' || d.root_id
                    )
                  )
                ORDER BY d.updated_at_ms ASC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [_payload(row) for row in rows]

    def has_unsettled_root_dispatches(
        self,
        *,
        room_id: str,
        root_id: str,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM agent_room_partner_dispatches
                WHERE room_id = ? AND root_id = ?
                  AND status IN ('prepared', 'dispatched', 'review', 'blocked')
                LIMIT 1
                """,
                (_required(room_id), _required(root_id)),
            ).fetchone()
        return row is not None

    def settle_terminal_projection(
        self,
        child_dispatch_id: str,
        *,
        generation: int,
        expected_schedule_id: str,
        expected_error: str,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Settle an exact, accepted terminal-projection attempt.

        This never changes dispatch review status.  The compare-and-set fence
        only clears the known "missing typed result" failure after the public
        projection receipt has been durably appended.
        """

        timestamp = _now_ms(now_ms)
        schedule_id = _required(expected_schedule_id)
        bounded_error = str(expected_error or "")[:2_000]
        with self._connect(immediate=True) as conn:
            row = self._row(conn, child_dispatch_id)
            if (
                str(row["status"]) != "accepted"
                or int(row["wake_generation"]) != int(generation)
                or str(row["wake_schedule_id"]) != schedule_id
                or str(row["wake_state"])
                not in {"scheduled", "delivered", "failed"}
                or (
                    str(row["wake_state"]) == "failed"
                    and str(row["error"]) != bounded_error
                )
            ):
                return _payload(row)
            room_id = str(row["room_id"])
            root_id = str(row["root_id"])
            outstanding = conn.execute(
                """
                SELECT 1 FROM agent_room_partner_dispatches
                WHERE room_id = ? AND root_id = ? AND status = 'accepted'
                  AND wake_state IN ('scheduled', 'delivered', 'failed')
                  AND (wake_state <> 'delivered' OR error <> '')
                LIMIT 1
                """,
                (room_id, root_id),
            ).fetchone()
            if outstanding is None:
                return _payload(row)
            conn.execute(
                """
                UPDATE agent_room_partner_dispatches
                SET wake_state = 'delivered', error = '', updated_at_ms = ?
                WHERE room_id = ? AND root_id = ? AND status = 'accepted'
                  AND wake_state IN ('scheduled', 'delivered', 'failed')
                  AND (wake_state <> 'failed' OR error = ?)
                """,
                (
                    timestamp,
                    room_id,
                    root_id,
                    bounded_error,
                ),
            )
            row = self._row(conn, child_dispatch_id)
        self._notify()
        return _payload(row)

    def requeue_delivered_wake(
        self,
        child_dispatch_id: str,
        *,
        generation: int,
        expected_schedule_id: str,
        max_generation: int,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Atomically allocate one bounded retry generation after delivery.

        The prior wake schedule and run remain immutable in their own ledger.
        A generation fence makes concurrent/replayed reconciliation a no-op.
        """

        timestamp = _now_ms(now_ms)
        expected_generation = int(generation)
        expected_schedule = _required(expected_schedule_id)
        bounded_max_generation = max(0, int(max_generation))
        with self._connect(immediate=True) as conn:
            row = self._row(conn, child_dispatch_id)
            if (
                str(row["status"]) not in _WAKEABLE_TERMINAL_STATES
                or str(row["wake_state"]) not in {"scheduled", "delivered"}
                or int(row["wake_generation"]) != expected_generation
                or str(row["wake_schedule_id"]) != expected_schedule
                or expected_generation >= bounded_max_generation
            ):
                return _payload(row)
            conn.execute(
                """
                UPDATE agent_room_partner_dispatches
                SET wake_generation = wake_generation + 1,
                    wake_schedule_id = '', wake_state = 'pending', error = '',
                    updated_at_ms = ?
                WHERE child_dispatch_id = ?
                  AND status IN ('review', 'blocked', 'failed', 'aborted', 'accepted')
                  AND wake_state IN ('scheduled', 'delivered')
                  AND wake_generation = ?
                  AND wake_schedule_id = ?
                  AND wake_generation < ?
                """,
                (
                    timestamp,
                    child_dispatch_id,
                    expected_generation,
                    expected_schedule,
                    bounded_max_generation,
                ),
            )
            row = self._row(conn, child_dispatch_id)
        self._notify()
        return _payload(row)

    def requeue_failed_wake(
        self,
        child_dispatch_id: str,
        *,
        generation: int,
        expected_schedule_id: str,
        expected_error: str,
        max_generation: int,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Atomically allocate one bounded retry for one exact failed wake.

        The caller must independently prove that the failed command is safe to
        retry.  This compare-and-set only fences the dispatch projection; the
        prior schedule and run remain immutable in the wake ledger.
        """

        timestamp = _now_ms(now_ms)
        expected_generation = int(generation)
        expected_schedule = _required(expected_schedule_id)
        bounded_error = str(expected_error or "")[:2_000]
        bounded_max_generation = max(0, int(max_generation))
        with self._connect(immediate=True) as conn:
            row = self._row(conn, child_dispatch_id)
            if (
                str(row["status"]) not in _WAKEABLE_TERMINAL_STATES
                or str(row["wake_state"]) != "failed"
                or int(row["wake_generation"]) != expected_generation
                or str(row["wake_schedule_id"]) != expected_schedule
                or str(row["error"]) != bounded_error
                or expected_generation >= bounded_max_generation
            ):
                return _payload(row)
            conn.execute(
                """
                UPDATE agent_room_partner_dispatches
                SET wake_generation = wake_generation + 1,
                    wake_schedule_id = '', wake_state = 'pending', error = '',
                    updated_at_ms = ?
                WHERE child_dispatch_id = ?
                  AND status IN ('review', 'blocked', 'failed', 'aborted', 'accepted')
                  AND wake_state = 'failed'
                  AND wake_generation = ?
                  AND wake_schedule_id = ?
                  AND error = ?
                  AND wake_generation < ?
                """,
                (
                    timestamp,
                    child_dispatch_id,
                    expected_generation,
                    expected_schedule,
                    bounded_error,
                    bounded_max_generation,
                ),
            )
            row = self._row(conn, child_dispatch_id)
        self._notify()
        return _payload(row)

    def inflight(self, *, limit: int = 500) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_room_partner_dispatches
                WHERE status IN ('prepared', 'dispatched')
                ORDER BY updated_at_ms ASC LIMIT ?
                """,
                (max(1, min(int(limit), 1_000)),),
            ).fetchall()
        return [_payload(row) for row in rows]

    def runtime_recovery_candidates(
        self,
        *,
        after_child_dispatch_id: str = "",
        limit: int = 500,
    ) -> list[dict[str, object]]:
        """Page all execution receipts on an immutable dispatch identity.

        Submitted results remain eligible after review. A newest-first limit
        would let that history permanently hide older in-flight dispatches;
        updated_at_ms is also unsafe as a cursor because recovery changes it.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_room_partner_dispatches
                WHERE child_dispatch_id > ?
                  AND (status IN ('prepared', 'dispatched')
                       OR (completion_source = 'room_post' AND target_session_turn_id <> ''))
                ORDER BY child_dispatch_id ASC LIMIT ?
                """,
                (str(after_child_dispatch_id), max(1, min(int(limit), 1_000))),
            ).fetchall()
        return [_payload(row) for row in rows]

    def cancel_root(
        self,
        *,
        room_id: str,
        root_id: str,
        reason: str,
        now_ms: int | None = None,
    ) -> list[dict[str, object]]:
        timestamp = _now_ms(now_ms)
        with self._connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE agent_room_partner_dispatches
                SET status = 'cancelled', error = ?, wake_state = 'cancelled',
                    updated_at_ms = ?, completed_at_ms = COALESCE(completed_at_ms, ?)
                WHERE room_id = ? AND root_id = ?
                  AND status IN ('prepared', 'dispatched', 'review', 'blocked')
                """,
                (
                    str(reason or "")[:2_000],
                    timestamp,
                    timestamp,
                    _required(room_id),
                    _required(root_id),
                ),
            )
            rows = conn.execute(
                """
                SELECT * FROM agent_room_partner_dispatches
                WHERE room_id = ? AND root_id = ?
                ORDER BY created_at_ms ASC
                """,
                (room_id, root_id),
            ).fetchall()
        self._notify()
        return [_payload(row) for row in rows]

    def wait(
        self,
        child_dispatch_id: str,
        *,
        timeout_seconds: float,
    ) -> dict[str, object]:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            current = self.get(child_dispatch_id)
            if str(current["status"]) in _TERMINAL_STATES:
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return current
            with self._changed:
                self._changed.wait(timeout=min(remaining, 0.25))


    @staticmethod
    def _row(conn: sqlite3.Connection, child_dispatch_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM agent_room_partner_dispatches WHERE child_dispatch_id = ?",
            (_required(child_dispatch_id),),
        ).fetchone()
        if row is None:
            raise KeyError(child_dispatch_id)
        return row

    def _notify(self) -> None:
        with self._changed:
            self._changed.notify_all()

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def _payload(row: sqlite3.Row) -> dict[str, object]:
    return {
        "childDispatchId": str(row["child_dispatch_id"]),
        "roomId": str(row["room_id"]),
        "rootId": str(row["root_id"]),
        "parentDispatchId": str(row["parent_dispatch_id"]),
        "toolCallId": str(row["tool_call_id"]),
        "sourceParticipantId": str(row["source_participant_id"]),
        "sourceSessionId": str(row["source_session_id"]),
        "targetParticipantId": str(row["target_participant_id"]),
        "targetSessionId": str(row["target_session_id"]),
        "targetSessionTurnId": str(row["target_session_turn_id"]),
        "workItemId": str(row["work_item_id"]),
        "status": str(row["status"]),
        "result": str(row["result_text"]),
        "completionSource": str(row["completion_source"]),
        "postId": str(row["post_id"]),
        "error": str(row["error"]),
        "wake": {
            "generation": int(row["wake_generation"]),
            "scheduleId": str(row["wake_schedule_id"]),
            "state": str(row["wake_state"]),
        },
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "completedAtMs": int(row["completed_at_ms"] or 0),
    }


def _required(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Room Partner dispatch identifiers must not be empty")
    return text[:320]


def _now_ms(value: int | None) -> int:
    return int(value if value is not None else time.time() * 1_000)
