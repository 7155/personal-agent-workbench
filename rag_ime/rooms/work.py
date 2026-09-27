from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from rag_ime.contracts.json_schema import validate_contract
from rag_ime.db import apply_database_migrations
from rag_ime.rooms.work_transaction import participate_in_work_transaction


WORK_STATES = frozenset(
    {"queued", "active", "review", "blocked", "done", "failed", "cancelled"}
)
OPEN_WORK_STATES = frozenset({"queued", "active", "review", "blocked"})
AUTHORITATIVE_WORK_STATES = frozenset({"active", "review"})
MAX_ASSIGNMENTS_PER_ROOT = 6
MAX_ASSIGNMENT_DEPTH = 3
MAX_REVISIONS = 2
OPERABILITY_VERDICTS = frozenset({"passed", "failed", "unverified"})
REQUIREMENT_VERDICTS = frozenset({"satisfied", "not_satisfied", "unverified"})


class AgentRoomWorkAssignmentChanged(RuntimeError):
    """Raised when a formal Room assignment changes during dispatch."""


class AgentRoomWorkStore:
    """Durable Room responsibility ledger with append-only transition evidence."""

    # Optional trusted Jev commands enlist in the same canonical transaction.
    task_graph_guard_version = 1

    def __init__(
        self,
        db_path: str | Path,
        *,
        terminal_observer: Callable[[str, str], object] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._terminal_observer = terminal_observer

    def set_terminal_observer(
        self,
        observer: Callable[[str, str], object] | None,
    ) -> None:
        """Observe committed terminal evidence without sharing Room ownership."""

        self._terminal_observer = observer

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)

    def create(
        self,
        *,
        room_id: str,
        objective: str,
        expected_output: str,
        current_owner_participant_id: str,
        created_by_participant_id: str,
        client_message_id: str,
        accountable_participant_id: str = "",
        topic_id: str = "",
        root_turn_id: str = "",
        parent_work_id: str = "",
        acceptance_criteria: Sequence[object] = (),
        state: str = "active",
        depth: int = 1,
        created_at_ms: int | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        """Create a directly owned WorkItem from the Room control surface.

        Agent-to-agent delegation continues to use ``assign`` and its queued
        acceptance flow. This entry point is for an explicit human/Room owner
        assignment and therefore starts active by default.
        """

        timestamp = _timestamp(created_at_ms)
        normalized_room_id = _required_text(room_id, "room_id", maximum=320)
        owner_id = _required_text(
            current_owner_participant_id,
            "current_owner_participant_id",
            maximum=320,
        )
        creator_id = _required_text(
            created_by_participant_id,
            "created_by_participant_id",
            maximum=320,
        )
        accountable_id = _optional_text(accountable_participant_id, maximum=320) or owner_id
        normalized_client_id = _required_text(
            client_message_id,
            "client_message_id",
            maximum=320,
        )
        normalized_state = str(state or "").strip()
        if normalized_state not in WORK_STATES:
            raise ValueError("unsupported agent room work item state")
        normalized_depth = int(depth)
        if not 1 <= normalized_depth <= MAX_ASSIGNMENT_DEPTH:
            raise ValueError("agent room work item depth must be between 1 and 3")
        criteria = _text_list(
            acceptance_criteria,
            "acceptance_criteria",
            maximum_items=8,
            maximum_length=500,
            required=True,
        )
        normalized_objective = _required_text(objective, "objective", maximum=8_000)
        normalized_expected = _required_text(
            expected_output,
            "expected_output",
            maximum=8_000,
        )
        normalized_parent_id = _optional_text(parent_work_id, maximum=320)
        work_id = f"room-work:{uuid.uuid4()}"

        with self._creation_connection(_connection) as conn:
            existing = conn.execute(
                """
                SELECT * FROM agent_room_work_items
                WHERE room_id = ? AND created_by_participant_id = ?
                  AND client_message_id = ?
                """,
                (normalized_room_id, creator_id, normalized_client_id),
            ).fetchone()
            if existing is not None:
                payload = work_item_payload(existing)
                if (
                    payload["objective"] != normalized_objective
                    or payload["expectedOutput"] != normalized_expected
                    or payload["currentOwnerParticipantId"] != owner_id
                    or payload["accountableParticipantId"] != accountable_id
                    or payload["acceptanceCriteria"] != criteria
                    or payload["state"] != normalized_state
                    or payload["depth"] != normalized_depth
                ):
                    raise ValueError(
                        "client_message_id was already used for a different room work item"
                    )
                return payload

            rows = conn.execute(
                """
                SELECT id, participant_status FROM agent_room_participants
                WHERE room_id = ? AND id IN (?, ?, ?)
                """,
                (normalized_room_id, owner_id, creator_id, accountable_id),
            ).fetchall()
            statuses = {str(row["id"]): str(row["participant_status"]) for row in rows}
            if any(statuses.get(value) != "active" for value in {owner_id, creator_id, accountable_id}):
                raise ValueError(
                    "work item owner, creator, and accountable participant must be active members of the room"
                )

            if normalized_parent_id:
                parent = self._row(conn, normalized_parent_id)
                if str(parent["room_id"]) != normalized_room_id:
                    raise ValueError("parent work item does not belong to this room")
                root_work_id = str(parent["root_work_id"])
            else:
                root_work_id = work_id
            assignment_key = f"{normalized_room_id}:{work_id}:assignment:{uuid.uuid4()}"
            conn.execute(
                """
                INSERT INTO agent_room_work_items(
                    id, room_id, topic_id, root_turn_id, root_work_id, parent_work_id,
                    objective, expected_output, acceptance_criteria_json,
                    accountable_participant_id, current_owner_participant_id,
                    offered_to_participant_id, created_by_participant_id,
                    client_message_id, assignment_key, state, depth, revision,
                    result_summary, artifact_refs_json, evidence_refs_json,
                    blocker_json, accepted_turn_id, created_at_ms, updated_at_ms,
                    completed_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 0,
                          '', '[]', '[]', '{}', '', ?, ?, NULL)
                """,
                (
                    work_id,
                    normalized_room_id,
                    str(topic_id or ""),
                    str(root_turn_id or ""),
                    root_work_id,
                    normalized_parent_id or None,
                    normalized_objective,
                    normalized_expected,
                    json.dumps(criteria, ensure_ascii=False, separators=(",", ":")),
                    accountable_id,
                    owner_id,
                    creator_id,
                    normalized_client_id,
                    assignment_key,
                    normalized_state,
                    normalized_depth,
                    timestamp,
                    timestamp,
                ),
            )
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                event_type="assigned",
                actor_participant_id=creator_id,
                created_at_ms=timestamp,
            )
        return work_item_payload(row)
    def create_root_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        work_id: str,
        room_id: str,
        objective: str,
        expected_output: str,
        current_owner_participant_id: str,
        created_by_participant_id: str,
        client_message_id: str,
        acceptance_criteria: Sequence[object],
        created_at_ms: int,
        topic_id: str = "",
        root_turn_id: str = "",
        prepared_only: bool = False,
    ) -> tuple[dict[str, object], bool]:
        """Create the one accountable Root WorkItem in a caller transaction."""

        normalized_room_id = _required_text(room_id, "room_id", maximum=320)
        owner_id = _required_text(
            current_owner_participant_id,
            "current_owner_participant_id",
            maximum=320,
        )
        creator_id = _required_text(
            created_by_participant_id,
            "created_by_participant_id",
            maximum=320,
        )
        normalized_client_id = _required_text(
            client_message_id,
            "client_message_id",
            maximum=320,
        )
        normalized_objective = _required_text(
            objective,
            "objective",
            maximum=8_000,
        )
        normalized_expected = _required_text(
            expected_output,
            "expected_output",
            maximum=8_000,
        )
        criteria = _text_list(
            acceptance_criteria,
            "acceptance_criteria",
            maximum_items=16,
            maximum_length=4_000,
            required=True,
        )
        normalized_work_id = _required_text(work_id, "work_id", maximum=320)
        root_turn = _required_text(root_turn_id, "root_turn_id", maximum=320)
        timestamp = _timestamp(created_at_ms)
        existing = conn.execute(
            """
            SELECT * FROM agent_room_work_items
            WHERE room_id = ? AND created_by_participant_id = ?
              AND client_message_id = ?
            """,
            (normalized_room_id, creator_id, normalized_client_id),
        ).fetchone()
        if existing is not None:
            payload = work_item_payload(existing)
            if (
                payload["id"] != normalized_work_id
                or payload["objective"] != normalized_objective
                or payload["expectedOutput"] != normalized_expected
                or payload["currentOwnerParticipantId"] != owner_id
                or payload["accountableParticipantId"] != owner_id
                or payload["acceptanceCriteria"] != criteria
                or payload["rootTurnId"] != root_turn
            ):
                raise ValueError(
                    "room definition WorkItem identity changed"
                )
            return payload, False
        participant_rows = conn.execute(
            """
            SELECT id, participant_status FROM agent_room_participants
            WHERE room_id = ? AND id IN (?, ?)
            """,
            (normalized_room_id, owner_id, creator_id),
        ).fetchall()
        statuses = {
            str(row["id"]): str(row["participant_status"])
            for row in participant_rows
        }
        if any(
            statuses.get(value) != "active"
            for value in {owner_id, creator_id}
        ):
            raise ValueError(
                "root WorkItem owner and creator must be active Room participants"
            )
        duplicate_id = conn.execute(
            "SELECT id FROM agent_room_work_items WHERE id = ?",
            (normalized_work_id,),
        ).fetchone()
        if duplicate_id is not None:
            raise ValueError("room definition WorkItem identity already exists")
        assignment_key = (
            f"room-definition:{root_turn}:{normalized_work_id}:assignment"
        )
        conn.execute(
            """
            INSERT INTO agent_room_work_items(
                id, room_id, topic_id, root_turn_id, root_work_id, parent_work_id,
                objective, expected_output, acceptance_criteria_json,
                accountable_participant_id, current_owner_participant_id,
                offered_to_participant_id, created_by_participant_id,
                client_message_id, assignment_key, state, depth, revision,
                result_summary, artifact_refs_json, evidence_refs_json,
                blocker_json, accepted_turn_id, created_at_ms, updated_at_ms,
                completed_at_ms
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?,
                      'active', 1, 0, '', '[]', '[]', '{}', ?, ?, ?, NULL)
            """,
            (
                normalized_work_id,
                normalized_room_id,
                str(topic_id or ""),
                root_turn,
                normalized_work_id,
                normalized_objective,
                normalized_expected,
                json.dumps(criteria, ensure_ascii=False, separators=(",", ":")),
                owner_id,
                owner_id,
                creator_id,
                normalized_client_id,
                assignment_key,
                "" if prepared_only else root_turn,
                timestamp,
                timestamp,
            ),
        )
        row = self._row(conn, normalized_work_id)
        self._append_event(
            conn,
            row,
            event_type="assigned",
            actor_participant_id=creator_id,
            created_at_ms=timestamp,
            payload={"source": "facilitator_assignment", "rootId": root_turn},
        )
        return work_item_payload(row), True

    def assign(
        self,
        source_session_id: str,
        payload: Mapping[str, object],
        *,
        root_turn_id: str = "",
        topic_id: str = "",
        created_at_ms: int | None = None,
    ) -> tuple[dict[str, object], bool]:
        timestamp = _timestamp(created_at_ms)
        target_id = _required_text(
            payload.get("targetParticipantId"),
            "targetParticipantId",
            maximum=240,
        )
        client_message_id = _required_text(
            payload.get("clientMessageId"),
            "clientMessageId",
            maximum=200,
        )
        objective = _required_text(payload.get("objective"), "objective", maximum=4_000)
        expected_output = _required_text(
            payload.get("expectedOutput"),
            "expectedOutput",
            maximum=2_000,
        )
        criteria = _text_list(
            payload.get("acceptanceCriteria"),
            "acceptanceCriteria",
            maximum_items=8,
            maximum_length=500,
            required=True,
        )
        parent_work_id = _optional_text(
            payload.get("parentWorkId"),
            maximum=240,
        )
        assignment_key = hashlib.sha256(
            f"{parent_work_id}\0{target_id}\0{' '.join(objective.lower().split())}".encode()
        ).hexdigest()

        with self._connect(immediate=True) as conn:
            source = _participant_for_session(conn, source_session_id)
            room_id = str(source["room_id"])
            existing = conn.execute(
                """
                SELECT * FROM agent_room_work_items
                WHERE room_id = ? AND created_by_participant_id = ?
                  AND client_message_id = ?
                """,
                (room_id, str(source["id"]), client_message_id),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["offered_to_participant_id"] or "") != target_id
                    or str(existing["objective"]) != objective
                    or str(existing["expected_output"]) != expected_output
                    or json.loads(str(existing["acceptance_criteria_json"])) != criteria
                    or str(existing["parent_work_id"] or "") != parent_work_id
                ):
                    raise ValueError(
                        "clientMessageId was already used for a different Room assignment"
                    )
                return work_item_payload(existing), False

            target = _participant(conn, room_id, target_id)
            if str(target["id"]) == str(source["id"]):
                raise ValueError("Room work cannot be assigned to the current owner")

            accountable_id = str(source["id"])
            root_work_id = ""
            depth = 1
            parent: sqlite3.Row | None = None
            if parent_work_id:
                parent = conn.execute(
                    """
                    SELECT * FROM agent_room_work_items
                    WHERE id = ? AND room_id = ?
                    """,
                    (parent_work_id, room_id),
                ).fetchone()
                if parent is None:
                    raise ValueError("parentWorkId does not identify work in this Room")
                if str(parent["current_owner_participant_id"]) != str(source["id"]):
                    raise ValueError("only the current owner may split a WorkItem")
                if str(parent["state"]) not in {"active", "blocked"}:
                    raise ValueError("only active or blocked work may be split")
                root_work_id = str(parent["root_work_id"])
                accountable_id = str(parent["accountable_participant_id"])
                depth = int(parent["depth"]) + 1
                if depth > MAX_ASSIGNMENT_DEPTH:
                    raise ValueError("Room assignment depth limit is 3")
                self._reject_ancestor_bounce(
                    conn,
                    parent,
                    target_participant_id=target_id,
                )

            open_children = conn.execute(
                """
                SELECT COUNT(*) FROM agent_room_work_items
                WHERE created_by_participant_id = ?
                  AND state IN ('queued', 'active', 'review', 'blocked')
                """,
                (str(source["id"]),),
            ).fetchone()
            parallel_limit = (
                2 if str(source["collaboration_role"]) == "coordinator" else 1
            )
            if int(open_children[0] if open_children else 0) >= parallel_limit:
                raise ValueError(
                    f"Room assignment parallel limit is {parallel_limit} for this role"
                )

            duplicate = conn.execute(
                """
                SELECT * FROM agent_room_work_items
                WHERE room_id = ? AND created_by_participant_id = ?
                  AND assignment_key = ? AND state = 'queued'
                ORDER BY created_at_ms ASC LIMIT 1
                """,
                (room_id, str(source["id"]), assignment_key),
            ).fetchone()
            if duplicate is not None:
                return work_item_payload(duplicate), False

            work_id = f"room-work:{uuid.uuid4()}"
            root_work_id = root_work_id or work_id
            assignment_count = conn.execute(
                "SELECT COUNT(*) FROM agent_room_work_items WHERE root_work_id = ?",
                (root_work_id,),
            ).fetchone()
            if int(assignment_count[0] if assignment_count else 0) >= MAX_ASSIGNMENTS_PER_ROOT:
                raise ValueError("Room root assignment limit is 6")

            conn.execute(
                """
                INSERT INTO agent_room_work_items(
                    id, room_id, topic_id, root_turn_id, root_work_id, parent_work_id,
                    objective, expected_output, acceptance_criteria_json,
                    accountable_participant_id, current_owner_participant_id,
                    offered_to_participant_id, created_by_participant_id,
                    client_message_id, assignment_key, state, depth, revision,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'queued', ?, 0, ?, ?)
                """,
                (
                    work_id,
                    room_id,
                    str(topic_id or ""),
                    str(root_turn_id or ""),
                    root_work_id,
                    parent_work_id or None,
                    objective,
                    expected_output,
                    json.dumps(criteria, ensure_ascii=False, separators=(",", ":")),
                    accountable_id,
                    str(source["id"]),
                    target_id,
                    str(source["id"]),
                    client_message_id,
                    assignment_key,
                    depth,
                    timestamp,
                    timestamp,
                ),
            )
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                event_type="assigned",
                actor_participant_id=str(source["id"]),
                created_at_ms=timestamp,
            )
        return work_item_payload(row), True

    def accept_assignment(
        self,
        work_id: str,
        *,
        target_participant_id: str,
        accepted_turn_id: str,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _timestamp(updated_at_ms)
        with self._connect(immediate=True) as conn:
            row = self._row(conn, work_id)
            if str(row["state"]) == "active":
                return work_item_payload(row)
            if str(row["state"]) != "queued":
                raise ValueError("Room assignment is no longer queued")
            if str(row["offered_to_participant_id"] or "") != target_participant_id:
                raise ValueError("only the offered participant may accept this assignment")
            conn.execute(
                """
                UPDATE agent_room_work_items
                SET state = 'active', current_owner_participant_id = ?,
                    offered_to_participant_id = NULL, accepted_turn_id = ?,
                    updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    target_participant_id,
                    str(accepted_turn_id or "")[:240],
                    timestamp,
                    work_id,
                ),
            )
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                event_type="accepted",
                actor_participant_id=target_participant_id,
                created_at_ms=timestamp,
            )
        return work_item_payload(row)

    def fail_assignment(
        self,
        work_id: str,
        *,
        actor_participant_id: str,
        reason: str,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _timestamp(updated_at_ms)
        with self._connect(immediate=True) as conn:
            row = self._row(conn, work_id)
            if str(row["state"]) != "queued":
                return work_item_payload(row)
            blocker = {"reason": _bounded(reason, 500), "phase": "assignment"}
            conn.execute(
                """
                UPDATE agent_room_work_items
                SET state = 'failed', blocker_json = ?, updated_at_ms = ?,
                    completed_at_ms = ?
                WHERE id = ?
                """,
                (
                    json.dumps(blocker, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                    timestamp,
                    work_id,
                ),
            )
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                event_type="assignment_failed",
                actor_participant_id=actor_participant_id,
                created_at_ms=timestamp,
            )
        payload = work_item_payload(row)
        self._notify_terminal(payload)
        return payload

    def submit(
        self,
        session_id: str,
        payload: Mapping[str, object],
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _timestamp(updated_at_ms)
        work_id = _required_text(payload.get("workId"), "workId", maximum=240)
        summary = _required_text(
            payload.get("resultSummary"),
            "resultSummary",
            maximum=4_000,
        )
        artifact_refs = _text_list(
            payload.get("artifactRefs"),
            "artifactRefs",
            maximum_items=16,
            maximum_length=1_000,
        )
        evidence_refs = _text_list(
            payload.get("evidenceRefs"),
            "evidenceRefs",
            maximum_items=24,
            maximum_length=1_000,
        )
        if not artifact_refs and not evidence_refs:
            raise ValueError(
                "work submission requires at least one artifactRefs or evidenceRefs entry"
            )
        proposed_operability = _optional_verdict(
            payload.get("proposedOperabilityVerdict"),
            "proposedOperabilityVerdict",
            allowed=OPERABILITY_VERDICTS,
        )
        proposed_requirement = _optional_verdict(
            payload.get("proposedRequirementVerdict"),
            "proposedRequirementVerdict",
            allowed=REQUIREMENT_VERDICTS,
        )
        if not proposed_operability and not proposed_requirement:
            inferred_op, inferred_req = infer_proposed_verdicts_from_summary(summary)
            proposed_operability = inferred_op
            proposed_requirement = inferred_req
        with self._connect(immediate=True) as conn:
            actor = _participant_for_session(conn, session_id)
            row = self._owned_row(conn, work_id, actor)
            if str(row["state"]) not in {"active", "blocked"}:
                raise ValueError("only active or blocked work may be submitted")
            open_children = conn.execute(
                """
                SELECT COUNT(*) FROM agent_room_work_items
                WHERE parent_work_id = ?
                  AND state IN ('queued', 'active', 'review', 'blocked')
                """,
                (work_id,),
            ).fetchone()
            if int(open_children[0] if open_children else 0) > 0:
                raise ValueError(
                    "Room work cannot be submitted while child WorkItems are open"
                )
            conn.execute(
                """
                UPDATE agent_room_work_items
                SET state = 'review', result_summary = ?, artifact_refs_json = ?,
                    evidence_refs_json = ?, blocker_json = '{}',
                    proposed_operability_verdict = ?,
                    proposed_requirement_verdict = ?,
                    updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    summary,
                    json.dumps(artifact_refs, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(evidence_refs, ensure_ascii=False, separators=(",", ":")),
                    proposed_operability,
                    proposed_requirement,
                    timestamp,
                    work_id,
                ),
            )
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                event_type="submitted",
                actor_participant_id=str(actor["id"]),
                created_at_ms=timestamp,
            )
        return work_item_payload(row)

    def accept(
        self,
        session_id: str,
        payload: Mapping[str, object],
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        return self._review_transition(
            session_id,
            payload,
            accept=True,
            updated_at_ms=updated_at_ms,
        )

    def return_for_revision(
        self,
        session_id: str,
        payload: Mapping[str, object],
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        return self._review_transition(
            session_id,
            payload,
            accept=False,
            updated_at_ms=updated_at_ms,
        )

    def block(
        self,
        session_id: str,
        payload: Mapping[str, object],
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _timestamp(updated_at_ms)
        work_id = _required_text(payload.get("workId"), "workId", maximum=240)
        blocker = {
            "reason": _required_text(payload.get("reason"), "reason", maximum=2_000),
            "nextStep": _required_text(
                payload.get("nextStep"),
                "nextStep",
                maximum=2_000,
            ),
            "wakeCondition": _wake_condition(payload.get("wakeCondition")),
        }
        deadline = payload.get("deadlineAtMs")
        if deadline is not None:
            if not isinstance(deadline, int) or isinstance(deadline, bool) or deadline < 1:
                raise ValueError("deadlineAtMs must be a positive integer")
            blocker["deadlineAtMs"] = deadline
        with self._connect(immediate=True) as conn:
            actor = _participant_for_session(conn, session_id)
            row = self._owned_row(conn, work_id, actor)
            if str(row["state"]) != "active":
                raise ValueError("only active work may be blocked")
            conn.execute(
                """
                UPDATE agent_room_work_items
                SET state = 'blocked', blocker_json = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    json.dumps(blocker, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                    work_id,
                ),
            )
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                event_type="blocked",
                actor_participant_id=str(actor["id"]),
                created_at_ms=timestamp,
            )
        return work_item_payload(row)

    def escalate(
        self,
        session_id: str,
        payload: Mapping[str, object],
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _timestamp(updated_at_ms)
        work_id = _required_text(payload.get("workId"), "workId", maximum=240)
        reason = _required_text(payload.get("reason"), "reason", maximum=2_000)
        next_step = _required_text(payload.get("nextStep"), "nextStep", maximum=2_000)
        with self._connect(immediate=True) as conn:
            actor = _participant_for_session(conn, session_id)
            row = self._owned_row(conn, work_id, actor)
            if str(row["state"]) not in {"active", "blocked"}:
                raise ValueError("only active or blocked work may be escalated")
            blocker = {
                "reason": reason,
                "nextStep": next_step,
                "escalated": True,
            }
            conn.execute(
                """
                UPDATE agent_room_work_items
                SET state = 'failed', blocker_json = ?, updated_at_ms = ?,
                    completed_at_ms = ?
                WHERE id = ?
                """,
                (
                    json.dumps(blocker, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                    timestamp,
                    work_id,
                ),
            )
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                event_type="escalated",
                actor_participant_id=str(actor["id"]),
                created_at_ms=timestamp,
            )
        payload = work_item_payload(row)
        self._notify_terminal(payload)
        return payload

    def list(
        self,
        *,
        room_id: str,
        states: Sequence[str] = (),
        owner_participant_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, object]]:
        normalized_states = tuple(
            dict.fromkeys(str(value or "").strip() for value in states if str(value or "").strip())
        )
        if any(value not in WORK_STATES for value in normalized_states):
            raise ValueError("unsupported agent room work item state")
        clauses = ["room_id = ?"]
        parameters: list[object] = [_required_text(room_id, "room_id", maximum=320)]
        if normalized_states:
            clauses.append(f"state IN ({', '.join('?' for _ in normalized_states)})")
            parameters.extend(normalized_states)
        owner_id = _optional_text(owner_participant_id, maximum=320)
        if owner_id:
            clauses.append("current_owner_participant_id = ?")
            parameters.append(owner_id)
        parameters.append(max(1, min(int(limit), 200)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM agent_room_work_items
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at_ms DESC, id DESC LIMIT ?
                """,  # noqa: S608 - clauses only contain fixed internal columns
                parameters,
            ).fetchall()
        return [work_item_payload(row) for row in rows]

    def list_for_root(
        self,
        *,
        room_id: str,
        root_turn_id: str,
    ) -> list[dict[str, object]]:
        """Return the complete WorkItem set for one Room Root."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_room_work_items
                WHERE room_id = ? AND root_turn_id = ?
                ORDER BY created_at_ms ASC, id ASC
                """,
                (
                    _required_text(room_id, "room_id", maximum=320),
                    _required_text(root_turn_id, "root_turn_id", maximum=320),
                ),
            ).fetchall()
        return [work_item_payload(row) for row in rows]

    def authoritative_owner(
        self,
        work_id: str,
        *,
        room_id: str,
    ) -> tuple[dict[str, object], str]:
        item = self.get(work_id, room_id=room_id)
        state = str(item["state"])
        if state not in AUTHORITATIVE_WORK_STATES:
            raise ValueError("only active or review work items can be dispatched")
        owner_id = str(item["currentOwnerParticipantId"])
        if owner_id:
            with self._connect() as conn:
                assignment = conn.execute(
                    """
                    SELECT payload_json FROM agent_room_work_events
                    WHERE work_id = ? AND event_type = 'assigned'
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (work_id,),
                ).fetchone()
            if assignment is None:
                raise RuntimeError("active WorkItem owner has no formal assignment event")
            payload = json.loads(str(assignment["payload_json"] or "{}"))
            snapshot = payload.get("work") if isinstance(payload, Mapping) else None
            recorded_owner = (
                str(snapshot.get("currentOwnerParticipantId") or "")
                if isinstance(snapshot, Mapping)
                else str(payload.get("currentOwnerParticipantId") or "")
            )
            if recorded_owner != owner_id:
                raise RuntimeError("WorkItem owner does not match its latest formal assignment event")
        return item, owner_id

    def reassign(
        self,
        work_id: str,
        *,
        actor_participant_id: str,
        current_owner_participant_id: str,
        reason: str = "",
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        actor_id = _required_text(actor_participant_id, "actor_participant_id", maximum=320)
        owner_id = _required_text(
            current_owner_participant_id,
            "current_owner_participant_id",
            maximum=320,
        )
        timestamp = _timestamp(updated_at_ms)
        with self._connect(immediate=True) as conn:
            row = self._row(conn, work_id)
            if str(row["state"]) not in AUTHORITATIVE_WORK_STATES:
                raise ValueError("only active or review work items can be reassigned")
            room_id = str(row["room_id"])
            participant_rows = conn.execute(
                """
                SELECT id, participant_status FROM agent_room_participants
                WHERE room_id = ? AND id IN (?, ?)
                """,
                (room_id, actor_id, owner_id),
            ).fetchall()
            statuses = {str(value["id"]): str(value["participant_status"]) for value in participant_rows}
            if statuses.get(actor_id) != "active" or statuses.get(owner_id) != "active":
                raise ValueError("reassignment actor and owner must be active room participants")
            previous_owner_id = str(row["current_owner_participant_id"])
            if actor_id not in {previous_owner_id, str(row["accountable_participant_id"])}:
                raise ValueError("only the current owner or accountable participant may reassign work")
            if previous_owner_id == owner_id:
                return work_item_payload(row)
            conn.execute(
                """
                UPDATE agent_room_work_items
                SET current_owner_participant_id = ?, offered_to_participant_id = NULL,
                    assignment_key = ?, accepted_turn_id = '', updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    owner_id,
                    f"{room_id}:{work_id}:assignment:{uuid.uuid4()}",
                    timestamp,
                    work_id,
                ),
            )
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                event_type="assigned",
                actor_participant_id=actor_id,
                created_at_ms=timestamp,
                payload={
                    "reason": _bounded(reason, 500) or "reassignment",
                    "previousOwnerParticipantId": previous_owner_id,
                    "currentOwnerParticipantId": owner_id,
                },
            )
        return work_item_payload(row)

    def transfer_for_jev_removal(
        self, work_id: str, *, removed_participant_id: str,
        target_participant_id: str, expected_revision: int,
        expected_owner_id: str, expected_assignment_key: str,
        expected_accepted_turn_id: str, reason: str,
    ) -> dict[str, object]:
        """Transfer one drained graph responsibility without changing its result.

        GuardedWorkOwner supplies the exact execution proof and enlists this
        mutation in the graph transaction. A queued offer becomes direct active
        work only when its delegating owner is the departing participant.
        """
        removed = _required_text(removed_participant_id, "removed_participant_id", maximum=320)
        target = _required_text(target_participant_id, "target_participant_id", maximum=320)
        if removed == target:
            raise ValueError("removal target must differ from departing participant")
        with self._connect(immediate=True) as conn:
            row = self._row(conn, work_id)
            if (int(row["revision"]) != expected_revision
                or str(row["current_owner_participant_id"]) != expected_owner_id
                or str(row["assignment_key"]) != expected_assignment_key
                or str(row["accepted_turn_id"] or "") != expected_accepted_turn_id):
                raise ValueError("Room responsibility changed before removal transfer")
            if str(row["state"]) not in {"queued", "active", "blocked"}:
                raise ValueError("review or terminal work cannot be transferred during removal")
            if removed not in {str(row["accountable_participant_id"]),
                               str(row["current_owner_participant_id"]),
                               str(row["offered_to_participant_id"] or "")}:
                raise ValueError("departing participant has no open responsibility here")
            _participant(conn, str(row["room_id"]), target)
            move_owner = str(row["current_owner_participant_id"]) == removed
            next_state = "active" if move_owner and row["state"] == "queued" else row["state"]
            next_owner = target if move_owner else str(row["current_owner_participant_id"])
            next_accountable = (target if str(row["accountable_participant_id"]) == removed
                                else str(row["accountable_participant_id"]))
            next_offered = (target if str(row["offered_to_participant_id"] or "") == removed and not move_owner
                            else None if move_owner else row["offered_to_participant_id"])
            now = _timestamp(None)
            conn.execute("""
                UPDATE agent_room_work_items
                SET state=?, current_owner_participant_id=?, accountable_participant_id=?,
                    offered_to_participant_id=?, assignment_key=?, accepted_turn_id=?, updated_at_ms=?
                WHERE id=?
            """, (next_state, next_owner, next_accountable, next_offered,
                  f"{row['room_id']}:{work_id}:assignment:{uuid.uuid4()}" if move_owner else row["assignment_key"],
                  "" if move_owner else row["accepted_turn_id"], now, work_id))
            changed = self._row(conn, work_id)
            self._append_event(conn, changed, event_type="reassigned",
                actor_participant_id=target, created_at_ms=now,
                payload={"source": "jev_participant_removal", "reason": _bounded(reason, 500),
                         "releasedFromParticipantId": removed,
                         "previousOwnerParticipantId": expected_owner_id,
                         "previousAccountableParticipantId": str(row["accountable_participant_id"]),
                         "previousOfferedToParticipantId": str(row["offered_to_participant_id"] or "")})
            return work_item_payload(changed)

    def release_for_participant(
        self,
        room_id: str,
        participant_id: str,
        *,
        replacement_participant_id: str,
        reason: str = "",
        updated_at_ms: int | None = None,
    ) -> list[dict[str, object]]:
        """Release open work before a Room participant leaves.

        The Room keeps the original creator and event history, while the
        active facilitator becomes the temporary owner of released work.  A
        blocked state makes the need for a new assignment visible and lets
        the normal retry/reassign lifecycle continue after the participant
        row is soft-removed.
        """

        normalized_room_id = _required_text(room_id, "room_id", maximum=320)
        released_id = _required_text(participant_id, "participant_id", maximum=320)
        replacement_id = _required_text(
            replacement_participant_id,
            "replacement_participant_id",
            maximum=320,
        )
        if released_id == replacement_id:
            raise ValueError("replacement participant must differ from the removed participant")
        timestamp = _timestamp(updated_at_ms)
        release_reason = _bounded(reason, 500) or "participant removed; Room work needs reassignment"
        with self._connect(immediate=True) as conn:
            replacement = conn.execute(
                """
                SELECT id, participant_status FROM agent_room_participants
                WHERE room_id = ? AND id = ?
                """,
                (normalized_room_id, replacement_id),
            ).fetchone()
            if replacement is None or str(replacement["participant_status"]) != "active":
                raise ValueError("replacement participant must be active in this Room")
            rows = conn.execute(
                """
                SELECT * FROM agent_room_work_items
                WHERE room_id = ? AND state IN ('queued', 'active', 'review', 'blocked')
                  AND (
                    accountable_participant_id = ?
                    OR current_owner_participant_id = ?
                    OR offered_to_participant_id = ?
                  )
                ORDER BY created_at_ms ASC, id ASC
                """,
                (
                    normalized_room_id,
                    released_id,
                    released_id,
                    released_id,
                ),
            ).fetchall()
            released: list[dict[str, object]] = []
            # Membership changes must not silently replace a Jev assignment or
            # clear its accepted turn while verification/admission is pending.
            # Keep the check in this write transaction, before any work changes.
            for current in rows:
                managed = conn.execute(
                    "SELECT 1 FROM agent_jev_graphs g JOIN agent_jev_host_roots h USING(graph_id) "
                    "WHERE g.room_id=? AND g.root_turn_id=? AND h.stopped=0 AND h.final_json='{}'",
                    (normalized_room_id, str(current["root_turn_id"])),
                ).fetchone()
                if managed:
                    raise ValueError("active Jev responsibility must be reclaimed by its graph before removing this participant")
            for current in rows:
                previous_owner_id = str(current["current_owner_participant_id"])
                previous_accountable_id = str(current["accountable_participant_id"])
                previous_offered_id = str(current["offered_to_participant_id"] or "")
                blocker = dict(json.loads(str(current["blocker_json"] or "{}")))
                blocker.update(
                    {
                        "reason": release_reason,
                        "nextStep": "Facilitator must assign this WorkItem to an active Room participant",
                        "releasedFromParticipantId": released_id,
                        "needsReassignment": True,
                    }
                )
                conn.execute(
                    """
                    UPDATE agent_room_work_items
                    SET state = 'blocked',
                        accountable_participant_id = ?,
                        current_owner_participant_id = ?,
                        offered_to_participant_id = NULL,
                        assignment_key = ?,
                        accepted_turn_id = '',
                        blocker_json = ?,
                        updated_at_ms = ?,
                        completed_at_ms = NULL
                    WHERE id = ?
                    """,
                    (
                        replacement_id,
                        replacement_id,
                        f"{normalized_room_id}:{current['id']}:assignment:{uuid.uuid4()}",
                        json.dumps(blocker, ensure_ascii=False, separators=(",", ":")),
                        timestamp,
                        str(current["id"]),
                    ),
                )
                updated = self._row(conn, str(current["id"]))
                self._append_event(
                    conn,
                    updated,
                    event_type="reassigned",
                    actor_participant_id=replacement_id,
                    created_at_ms=timestamp,
                    payload={
                        "reason": release_reason,
                        "releasedFromParticipantId": released_id,
                        "previousOwnerParticipantId": previous_owner_id,
                        "previousAccountableParticipantId": previous_accountable_id,
                        "previousOfferedToParticipantId": previous_offered_id,
                        "needsReassignment": True,
                    },
                )
                released.append(work_item_payload(updated))
        return released

    def retry(
        self,
        work_id: str,
        *,
        actor_participant_id: str,
        current_owner_participant_id: str,
        expected_revision: int,
        reason: str,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Reopen one blocked or failed WorkItem under the same contract."""

        actor_id = _required_text(
            actor_participant_id,
            "actor_participant_id",
            maximum=320,
        )
        owner_id = _required_text(
            current_owner_participant_id,
            "current_owner_participant_id",
            maximum=320,
        )
        expected = _revision(expected_revision, "expected_revision")
        retry_reason = _required_text(reason, "reason", maximum=2_000)
        timestamp = _timestamp(updated_at_ms)
        with self._connect(immediate=True) as conn:
            row = self._row(conn, work_id)
            if str(row["state"]) not in {"blocked", "failed"}:
                raise ValueError("only blocked or failed work may be retried")
            if int(row["revision"]) != expected:
                raise ValueError("Room work revision changed; refresh before retry")
            if expected >= MAX_REVISIONS:
                raise ValueError(
                    "Room retry limit is 2; close with a truthful unresolved result"
                )
            if str(row["accountable_participant_id"]) != actor_id:
                raise ValueError("only the accountable participant may retry work")
            room_id = str(row["room_id"])
            participant = conn.execute(
                """
                SELECT participant_status FROM agent_room_participants
                WHERE room_id = ? AND id = ?
                """,
                (room_id, owner_id),
            ).fetchone()
            if participant is None or str(participant["participant_status"]) != "active":
                raise ValueError("retry owner must be an active room participant")
            revision = expected + 1
            cursor = conn.execute(
                """
                UPDATE agent_room_work_items
                SET state = 'active', revision = ?,
                    current_owner_participant_id = ?,
                    offered_to_participant_id = NULL,
                    assignment_key = ?, accepted_turn_id = '',
                    result_summary = '', artifact_refs_json = '[]',
                    evidence_refs_json = '[]',
                    blocker_json = ?,
                    proposed_operability_verdict = '',
                    proposed_requirement_verdict = '',
                    review_operability_verdict = '',
                    review_requirement_verdict = '',
                    review_evidence_refs_json = '[]', review_reason = '',
                    reviewer_participant_id = '', reviewed_at_ms = NULL,
                    updated_at_ms = ?, completed_at_ms = NULL
                WHERE id = ? AND state IN ('blocked', 'failed') AND revision = ?
                """,
                (
                    revision,
                    owner_id,
                    f"{room_id}:{work_id}:assignment:{uuid.uuid4()}",
                    json.dumps(
                        {"retryReason": retry_reason},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    work_id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Room work revision changed; refresh before retry")
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                # Reuse the persisted ledger's existing terminal-to-active
                # transition event instead of widening the SQLite CHECK
                # constraint for a synonym.
                event_type="resumed",
                actor_participant_id=actor_id,
                created_at_ms=timestamp,
                payload={
                    "reason": retry_reason,
                    "currentOwnerParticipantId": owner_id,
                },
            )
        return work_item_payload(row)

    def claim_dispatch(
        self,
        work_id: str,
        *,
        room_id: str,
        owner_participant_id: str,
        assignment_key: str,
        previous_accepted_turn_id: str,
        room_turn_id: str,
        root_turn_id: str = "",
        claimed_at_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _timestamp(claimed_at_ms)
        public_root_id = _optional_text(root_turn_id, maximum=320)
        with self._connect(immediate=True) as conn:
            row = self._row(conn, work_id)
            if str(row["room_id"]) != str(room_id):
                raise ValueError("work item does not belong to this room")
            if str(row["state"]) not in AUTHORITATIVE_WORK_STATES:
                raise ValueError("only active or review work items can be dispatched")
            if (
                str(row["current_owner_participant_id"]) != str(owner_participant_id)
                or str(row["assignment_key"]) != str(assignment_key)
                or str(row["accepted_turn_id"] or "") != str(previous_accepted_turn_id or "")
            ):
                raise AgentRoomWorkAssignmentChanged("WorkItem assignment changed before dispatch")
            cursor = conn.execute(
                """
                UPDATE agent_room_work_items
                SET accepted_turn_id = ?,
                    root_turn_id = CASE
                        WHEN ? != '' THEN ?
                        WHEN root_turn_id = '' THEN ?
                        ELSE root_turn_id
                    END,
                    updated_at_ms = ?
                WHERE id = ? AND room_id = ? AND current_owner_participant_id = ?
                  AND assignment_key = ? AND accepted_turn_id = ?
                """,
                (
                    str(room_turn_id),
                    public_root_id,
                    public_root_id,
                    str(room_turn_id),
                    timestamp,
                    work_id,
                    room_id,
                    owner_participant_id,
                    assignment_key,
                    str(previous_accepted_turn_id or ""),
                ),
            )
            if cursor.rowcount != 1:
                raise AgentRoomWorkAssignmentChanged("WorkItem assignment changed before dispatch")
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                event_type="accepted",
                actor_participant_id=str(owner_participant_id),
                created_at_ms=timestamp,
            )
        return work_item_payload(row)

    def fail_dispatch(
        self,
        work_id: str,
        *,
        room_id: str,
        actor_participant_id: str,
        room_turn_id: str,
        previous_accepted_turn_id: str,
        reason: str,
        failed_at_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _timestamp(failed_at_ms)
        with self._connect(immediate=True) as conn:
            row = self._row(conn, work_id)
            if str(row["room_id"]) != str(room_id):
                raise ValueError("work item does not belong to this room")
            claim_is_current = str(row["accepted_turn_id"] or "") == str(room_turn_id)
            if claim_is_current:
                conn.execute(
                    """
                    UPDATE agent_room_work_items SET accepted_turn_id = ?, updated_at_ms = ?
                    WHERE id = ? AND accepted_turn_id = ?
                    """,
                    (str(previous_accepted_turn_id or ""), timestamp, work_id, room_turn_id),
                )
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                event_type="assignment_failed",
                actor_participant_id=str(actor_participant_id),
                created_at_ms=timestamp,
            )
        return work_item_payload(row)

    def cancel_root(self, *, room_id: str, root_turn_id: str,
                    actor_participant_id: str) -> list[dict[str, object]]:
        """Persist a Stop fence; execution drain remains the cancellation owner's job."""
        changed = []
        with self._connect(immediate=True) as conn:
            rows = conn.execute(
                "SELECT * FROM agent_room_work_items WHERE room_id=? AND root_turn_id=?",
                (room_id, root_turn_id)).fetchall()
            if not rows or any(str(row["accountable_participant_id"]) != actor_participant_id
                               for row in rows):
                raise ValueError("Root cancellation requires its accountable participant")
            for row in rows:
                if row["state"] not in OPEN_WORK_STATES:
                    continue
                conn.execute("UPDATE agent_room_work_items SET state='cancelled',updated_at_ms=? WHERE id=?",
                             (_timestamp(None), row["id"]))
                current = self._row(conn, row["id"])
                self._append_event(conn, current, event_type="cancelled",
                                   actor_participant_id=actor_participant_id,
                                   created_at_ms=_timestamp(None))
                changed.append(work_item_payload(current))
        return changed

    def list_for_session(
        self,
        session_id: str,
        *,
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, object]]:
        normalized_status = str(status or "").strip()
        if normalized_status and normalized_status not in WORK_STATES:
            raise ValueError("unsupported Room work state")
        bounded = max(1, min(int(limit), 200))
        with self._connect() as conn:
            participant = _participant_for_session(conn, session_id, active_only=False)
            params: list[object] = [str(participant["room_id"])]
            clause = ""
            if normalized_status:
                clause = "AND state = ?"
                params.append(normalized_status)
            params.append(bounded)
            rows = conn.execute(
                f"""
                SELECT * FROM agent_room_work_items
                WHERE room_id = ? {clause}
                ORDER BY
                    CASE state
                        WHEN 'blocked' THEN 0
                        WHEN 'review' THEN 1
                        WHEN 'active' THEN 2
                        WHEN 'queued' THEN 3
                        ELSE 4
                    END,
                    updated_at_ms DESC
                LIMIT ?
                """,  # noqa: S608 - clause is selected from a fixed string
                tuple(params),
            ).fetchall()
        return [work_item_payload(row) for row in rows]

    def list_for_room(
        self,
        room_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        bounded = max(1, min(int(limit), 200))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_room_work_items
                WHERE room_id = ?
                ORDER BY
                    CASE state
                        WHEN 'blocked' THEN 0
                        WHEN 'review' THEN 1
                        WHEN 'active' THEN 2
                        WHEN 'queued' THEN 3
                        ELSE 4
                    END,
                    updated_at_ms DESC
                LIMIT ?
                """,
                (room_id, bounded),
            ).fetchall()
        return [work_item_payload(row) for row in rows]

    def get(self, work_id: str, *, room_id: str = "") -> dict[str, object]:
        with self._connect() as conn:
            row = self._row(conn, work_id)
        payload = work_item_payload(row)
        if room_id and payload["roomId"] != room_id:
            raise ValueError("work item does not belong to this room")
        return payload

    def list_events(self, work_id: str) -> list[dict[str, object]]:
        self.get(work_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_room_work_events
                WHERE work_id = ? ORDER BY sequence ASC
                """,
                (work_id,),
            ).fetchall()
        return [
            {
                "eventId": str(row["event_id"]),
                "workId": str(row["work_id"]),
                "roomId": str(row["room_id"]),
                "sequence": int(row["sequence"]),
                "eventType": str(row["event_type"]),
                "actorParticipantId": str(row["actor_participant_id"]),
                "payload": dict(json.loads(str(row["payload_json"] or "{}"))),
                "createdAtMs": int(row["created_at_ms"]),
            }
            for row in rows
        ]

    def reviewer_participant_id(self, work_id: str) -> str:
        with self._connect() as conn:
            row = self._row(conn, work_id)
            if row["parent_work_id"]:
                parent = self._row(conn, str(row["parent_work_id"]))
                return str(parent["current_owner_participant_id"])
        return str(row["accountable_participant_id"])

    def reconcile_intercom_outcomes(self) -> int:
        """Repair the rare crash window between durable delivery and Room projection."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.work_item_id, m.status, m.target_participant_id,
                       m.source_participant_id, m.accepted_turn_id, m.error
                FROM agent_room_intercom_messages m
                JOIN agent_room_work_items w ON w.id = m.work_item_id
                WHERE m.work_action = 'assignment'
                  AND w.state = 'queued'
                  AND m.status IN ('delivered', 'failed', 'stale', 'cancelled')
                ORDER BY m.updated_at_ms ASC
                """
            ).fetchall()
        repaired = 0
        for row in rows:
            work_id = str(row["work_item_id"])
            if str(row["status"]) == "delivered":
                self.accept_assignment(
                    work_id,
                    target_participant_id=str(row["target_participant_id"]),
                    accepted_turn_id=str(row["accepted_turn_id"] or ""),
                )
            else:
                self.fail_assignment(
                    work_id,
                    actor_participant_id=str(row["source_participant_id"]),
                    reason=str(row["error"] or row["status"]),
                )
            repaired += 1
        return repaired

    def _review_transition(
        self,
        session_id: str,
        payload: Mapping[str, object],
        *,
        accept: bool,
        updated_at_ms: int | None,
    ) -> dict[str, object]:
        timestamp = _timestamp(updated_at_ms)
        work_id = _required_text(payload.get("workId"), "workId", maximum=240)
        expected_revision = _revision(
            payload.get("expectedRevision"),
            "expectedRevision",
        )
        operability_verdict = _verdict(
            payload.get("operabilityVerdict"),
            "operabilityVerdict",
            allowed=OPERABILITY_VERDICTS,
        )
        requirement_verdict = _verdict(
            payload.get("requirementVerdict"),
            "requirementVerdict",
            allowed=REQUIREMENT_VERDICTS,
        )
        evidence_refs = _text_list(
            payload.get("evidenceRefs"),
            "evidenceRefs",
            maximum_items=24,
            maximum_length=1_000,
            required=True,
        )
        feedback = _optional_text(payload.get("reason"), maximum=2_000)
        if accept and (
            operability_verdict != "passed"
            or requirement_verdict != "satisfied"
        ):
            raise ValueError(
                "Room work may be accepted only when operability is passed "
                "and the requirement is satisfied"
            )
        if accept and not feedback:
            raise ValueError(
                "accept requires a concrete reason stating what was verified"
            )
        if not accept and not feedback:
            raise ValueError("revision return requires a concrete reason")
        if (
            not accept
            and operability_verdict == "passed"
            and requirement_verdict == "satisfied"
        ):
            raise ValueError(
                "a passed and satisfied review must be accepted, not returned"
            )
        superseded_by_work_id = _optional_text(
            payload.get("supersededByWorkId"),
            maximum=240,
        )
        with self._connect(immediate=True) as conn:
            actor = _participant_for_session(conn, session_id)
            row = self._row(conn, work_id)
            self._require_reviewer(conn, row, str(actor["id"]))
            if str(row["state"]) != "review":
                raise ValueError("Room work must be in review")
            if int(row["revision"]) != expected_revision:
                raise ValueError("Room work revision changed; refresh before review")
            event_payload: dict[str, object] | None = None
            if accept:
                event_payload = self._accept_over_proposed_payload(
                    conn,
                    row,
                    superseded_by_work_id=superseded_by_work_id,
                )
            review_values = (
                operability_verdict,
                requirement_verdict,
                json.dumps(
                    evidence_refs,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                feedback,
                str(actor["id"]),
                timestamp,
            )
            if accept:
                cursor = conn.execute(
                    """
                    UPDATE agent_room_work_items
                    SET state = 'done', blocker_json = '{}',
                        review_operability_verdict = ?,
                        review_requirement_verdict = ?,
                        review_evidence_refs_json = ?, review_reason = ?,
                        reviewer_participant_id = ?, reviewed_at_ms = ?,
                        updated_at_ms = ?, completed_at_ms = ?
                    WHERE id = ? AND state = 'review' AND revision = ?
                    """,
                    (
                        *review_values,
                        timestamp,
                        timestamp,
                        work_id,
                        expected_revision,
                    ),
                )
                event_type = "completed"
            else:
                revision = int(row["revision"]) + 1
                if revision > MAX_REVISIONS:
                    raise ValueError(
                        "Room revision limit is 2; escalate to the accountable participant"
                    )
                cursor = conn.execute(
                    """
                    UPDATE agent_room_work_items
                    SET state = 'active', revision = ?, blocker_json = ?,
                        review_operability_verdict = ?,
                        review_requirement_verdict = ?,
                        review_evidence_refs_json = ?, review_reason = ?,
                        reviewer_participant_id = ?, reviewed_at_ms = ?,
                        updated_at_ms = ?
                    WHERE id = ? AND state = 'review' AND revision = ?
                    """,
                    (
                        revision,
                        json.dumps(
                            {"reviewFeedback": feedback},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        *review_values,
                        timestamp,
                        work_id,
                        expected_revision,
                    ),
                )
                event_type = "returned"
            if cursor.rowcount != 1:
                raise ValueError("Room work revision changed; refresh before review")
            row = self._row(conn, work_id)
            self._append_event(
                conn,
                row,
                event_type=event_type,
                actor_participant_id=str(actor["id"]),
                created_at_ms=timestamp,
                payload=event_payload,
            )
        result = work_item_payload(row)
        self._notify_terminal(result)
        return result

    def _accept_over_proposed_payload(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        superseded_by_work_id: str,
    ) -> dict[str, object] | None:
        proposed_operability = str(row["proposed_operability_verdict"] or "")
        proposed_requirement = str(row["proposed_requirement_verdict"] or "")
        blocking_operability = proposed_operability in {"failed", "unverified"}
        blocking_requirement = proposed_requirement in {
            "not_satisfied",
            "unverified",
        }
        if not blocking_operability and not blocking_requirement:
            return None
        # Jev's verifier is an ordinary, separately bound Pi
        # purpose, not a synthetic child WorkItem. Its durable receipt must
        # cover this exact task/result; worker prose cannot create this record.
        from rag_ime.jev_tasks.ledger import task_from_row
        from rag_ime.jev_tasks.types import canonical, digest
        from dataclasses import asdict
        task_hash = digest(asdict(task_from_row(dict(row))))
        verification = conn.execute(
            "SELECT v.dispatch_id,v.result_json,json_extract(e.request_json,'$.ownerId') AS verifier_id FROM agent_jev_verifications v "
            "JOIN agent_jev_runtime_effects e ON e.effect_id=v.dispatch_id "
            "JOIN agent_jev_aux_settlements s ON s.dispatch_id=v.dispatch_id "
            "WHERE v.task_id=? AND v.task_hash=? AND e.state='accepted' "
            "AND json_extract(e.request_json,'$.purpose')='verify' "
            "AND json_extract(s.result_json,'$.status')='applied'",
            (row["id"], task_hash)).fetchone()
        if verification:
            verdict = json.loads(verification["result_json"])
            if verdict.get("operabilityVerdict") == "passed" and verdict.get("requirementVerdict") == "satisfied":
                if verification["verifier_id"] == row["current_owner_participant_id"]:
                    return {"verificationDispatchId": verification["dispatch_id"],
                            "verificationHash": digest(canonical(verdict)),
                            "verificationMode": "same_participant",
                            "verifierParticipantId": verification["verifier_id"]}
                return {"independentVerificationDispatchId": verification["dispatch_id"],
                        "independentVerificationHash": digest(canonical(verdict))}
        if not superseded_by_work_id:
            raise ValueError(
                "cannot accept passed/satisfied over Partner proposed "
                f"operability={proposed_operability or 'empty'} "
                f"requirement={proposed_requirement or 'empty'} "
                "without a superseding review WorkItem; return the item "
                "or pass supersededByWorkId"
            )
        review = conn.execute(
            "SELECT * FROM agent_room_work_items WHERE id = ?",
            (superseded_by_work_id,),
        ).fetchone()
        if review is None:
            raise ValueError("superseding review WorkItem was not found")
        if str(review["id"]) == str(row["id"]):
            raise ValueError(
                "superseding review WorkItem must be a distinct WorkItem"
            )
        if str(review["room_id"]) != str(row["room_id"]):
            raise ValueError(
                "superseding review WorkItem must belong to the same Room"
            )
        if str(review["parent_work_id"] or "") != str(row["id"]):
            raise ValueError(
                "superseding review WorkItem must be a direct child of the "
                "failed or unverified WorkItem"
            )
        if int(review["created_at_ms"]) <= int(row["updated_at_ms"]):
            raise ValueError(
                "superseding review WorkItem must be created after the "
                "failed or unverified submission"
            )
        if str(review["state"]) not in {"review", "done"}:
            raise ValueError(
                "superseding review WorkItem must already be submitted "
                "with honest dual-axis evidence"
            )
        if (
            str(review["proposed_operability_verdict"] or "") != "passed"
            or str(review["proposed_requirement_verdict"] or "") != "satisfied"
        ):
            raise ValueError(
                "superseding review WorkItem must propose "
                "operability=passed and requirement=satisfied"
            )
        return {"supersededByWorkId": superseded_by_work_id}

    def _notify_terminal(self, work: Mapping[str, object]) -> None:
        if str(work.get("state") or "") not in {"done", "failed", "cancelled"}:
            return
        observer = self._terminal_observer
        if observer is None:
            return
        try:
            observer("room_work_item", str(work["id"]))
        except Exception:
            # The WorkItem event is canonical. Its observer owns durable retry.
            pass

    def _require_reviewer(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        participant_id: str,
    ) -> None:
        expected = str(row["accountable_participant_id"])
        if row["parent_work_id"]:
            parent = self._row(conn, str(row["parent_work_id"]))
            expected = str(parent["current_owner_participant_id"])
        if participant_id != expected:
            raise ValueError("only the accountable parent owner may review this work")

    def _owned_row(
        self,
        conn: sqlite3.Connection,
        work_id: str,
        actor: sqlite3.Row,
    ) -> sqlite3.Row:
        row = self._row(conn, work_id)
        if str(row["room_id"]) != str(actor["room_id"]):
            raise ValueError("WorkItem does not belong to the current Room")
        if str(row["current_owner_participant_id"]) != str(actor["id"]):
            raise ValueError("only the current owner may change this WorkItem")
        return row

    def _reject_ancestor_bounce(
        self,
        conn: sqlite3.Connection,
        parent: sqlite3.Row,
        *,
        target_participant_id: str,
    ) -> None:
        current: sqlite3.Row | None = parent
        while current is not None:
            ancestor_participants = {
                str(current["current_owner_participant_id"]),
                str(current["created_by_participant_id"]),
                str(current["accountable_participant_id"]),
            }
            if target_participant_id in ancestor_participants:
                raise ValueError(
                    "cannot assign work back to an ancestor owner; submit or escalate it"
                )
            parent_id = str(current["parent_work_id"] or "")
            current = self._row(conn, parent_id) if parent_id else None

    def _append_event(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        event_type: str,
        actor_participant_id: str,
        created_at_ms: int,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        sequence_row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_room_work_events WHERE work_id = ?",
            (str(row["id"]),),
        ).fetchone()
        sequence = int(sequence_row[0] if sequence_row else 1)
        snapshot = work_item_payload(row)
        event_payload = {"work": snapshot, **dict(payload or {})}
        conn.execute(
            """
            INSERT INTO agent_room_work_events(
                event_id, work_id, room_id, sequence, event_type,
                actor_participant_id, payload_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"room-work-event:{uuid.uuid4()}",
                str(row["id"]),
                str(row["room_id"]),
                sequence,
                event_type,
                actor_participant_id,
                json.dumps(
                    event_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                created_at_ms,
            ),
        )

    @staticmethod
    def _row(conn: sqlite3.Connection, work_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM agent_room_work_items WHERE id = ?",
            (str(work_id or "").strip(),),
        ).fetchone()
        if row is None:
            raise KeyError(work_id)
        return row

    @contextmanager
    def _creation_connection(self, connection: sqlite3.Connection | None):
        """Internal batch owner: all creates enlist in the caller's canonical transaction."""
        if connection is None:
            with self._connect(immediate=True) as conn:
                yield conn
            return
        databases = connection.execute("PRAGMA database_list").fetchall()
        main = next((row[2] for row in databases if row[1] == "main"), "")
        if not connection.in_transaction or Path(main).resolve() != self.db_path.resolve():
            raise ValueError("task batch requires the canonical active transaction")
        yield connection

    def finalize_root_in_transaction(self, conn, *, work_id: str, actor_participant_id: str,
                                     summary: str, success: bool, evidence_refs: list[str],
                                     current_child_work_ids: list[str] | None = None):
        with self._creation_connection(conn):
            row = self._row(conn, work_id)
            self._require_reviewer(conn, row, actor_participant_id)
            if row["state"] == "cancelled":
                raise ValueError("cancelled Root cannot finalize")
            children = conn.execute("SELECT id,state FROM agent_room_work_items WHERE root_work_id=? AND id<>?", (work_id, work_id)).fetchall()
            if current_child_work_ids is not None:
                current_ids = set(current_child_work_ids)
                all_ids = {child["id"] for child in children}
                superseded = {old_id for (old_id,) in conn.execute(
                    "SELECT old_task_id FROM agent_jev_task_supersessions WHERE graph_id IN "
                    "(SELECT graph_id FROM agent_jev_graphs WHERE root_work_id=?)", (work_id,))}
                if current_ids != all_ids - superseded:
                    raise ValueError("current Jev child set does not match the active graph version")
                children = [child for child in children if child["id"] in current_ids]
            if success and children and any(child["state"] != "done" for child in children):
                raise ValueError("Root has unaccepted child work")
            if success and not children and row["state"] != "done":
                raise ValueError("direct Root must be verified before finalization")
            now = _timestamp(None)
            conn.execute("UPDATE agent_room_work_items SET state=?,result_summary=?,evidence_refs_json=?,updated_at_ms=?,completed_at_ms=? WHERE id=?",
                         ("done" if success else "failed", summary, json.dumps(evidence_refs), now, now, work_id))
            current = self._row(conn, work_id)
            self._append_event(conn, current, event_type="completed" if success else "failed",
                               actor_participant_id=actor_participant_id, created_at_ms=now,
                               payload={"source": "jev_finalization"})

    def close_unresolved_in_transaction(self, conn, *, work_id: str,
                                        actor_participant_id: str, reason: str) -> dict[str, object]:
        with self._creation_connection(conn):
            row = self._row(conn, work_id)
            self._require_reviewer(conn, row, actor_participant_id)
            if row["state"] in {"done", "cancelled"}:
                return work_item_payload(row)
            if row["state"] == "failed" and json.loads(row["blocker_json"] or "{}").get("terminal"):
                return work_item_payload(row)
            now = _timestamp(None)
            conn.execute("UPDATE agent_room_work_items SET state='failed',blocker_json=?,updated_at_ms=?,completed_at_ms=? WHERE id=?",
                         (json.dumps({"reason": _required_text(reason, "reason", maximum=2000), "terminal": True}), now, now, work_id))
            current = self._row(conn, work_id)
            self._append_event(conn, current, event_type="failed", actor_participant_id=actor_participant_id,
                               created_at_ms=now)
            return work_item_payload(current)

    def retire_superseded_in_transaction(self, conn, *, work_id: str,
                                         actor_participant_id: str, reason: str) -> dict[str, object]:
        """Retire one old Jev responsibility after its exact execution drained.

        Accepted historical work keeps its done state and evidence. Open work
        receives a formal cancellation event; the version map records why it
        is no longer in the current graph.
        """
        with self._creation_connection(conn):
            row = self._row(conn, work_id)
            self._require_reviewer(conn, row, actor_participant_id)
            if row["state"] not in OPEN_WORK_STATES:
                return work_item_payload(row)
            now = _timestamp(None)
            conn.execute(
                "UPDATE agent_room_work_items SET state='cancelled',blocker_json=?,"
                "updated_at_ms=?,completed_at_ms=? WHERE id=? AND state IN ('queued','active','review','blocked')",
                (json.dumps({"reason": _required_text(reason, "reason", maximum=2000),
                             "superseded": True}), now, now, work_id),
            )
            current = self._row(conn, work_id)
            self._append_event(conn, current, event_type="cancelled",
                               actor_participant_id=actor_participant_id,
                               created_at_ms=now, payload={"source": "jev_task_revision"})
            return work_item_payload(current)

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            with participate_in_work_transaction(conn, self.db_path, immediate=immediate):
                yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def work_item_payload(row: sqlite3.Row) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": "rag-ime.agent-room-work-item.v1",
        "id": str(row["id"]),
        "roomId": str(row["room_id"]),
        "topicId": str(row["topic_id"] or ""),
        "rootTurnId": str(row["root_turn_id"] or ""),
        "rootWorkId": str(row["root_work_id"]),
        "parentWorkId": str(row["parent_work_id"] or ""),
        "objective": str(row["objective"]),
        "expectedOutput": str(row["expected_output"]),
        "acceptanceCriteria": [
            str(value)
            for value in json.loads(str(row["acceptance_criteria_json"] or "[]"))
        ],
        "accountableParticipantId": str(row["accountable_participant_id"]),
        "currentOwnerParticipantId": str(row["current_owner_participant_id"]),
        "offeredToParticipantId": str(row["offered_to_participant_id"] or ""),
        "createdByParticipantId": str(row["created_by_participant_id"]),
        "clientMessageId": str(row["client_message_id"]),
        "assignmentKey": str(row["assignment_key"]),
        "state": str(row["state"]),
        "depth": int(row["depth"]),
        "revision": int(row["revision"]),
        "resultSummary": str(row["result_summary"] or ""),
        "artifactRefs": [
            str(value)
            for value in json.loads(str(row["artifact_refs_json"] or "[]"))
        ],
        "evidenceRefs": [
            str(value)
            for value in json.loads(str(row["evidence_refs_json"] or "[]"))
        ],
        "proposedOperabilityVerdict": str(
            row["proposed_operability_verdict"] or ""
        ),
        "proposedRequirementVerdict": str(
            row["proposed_requirement_verdict"] or ""
        ),
        "review": {
            "operabilityVerdict": str(
                row["review_operability_verdict"] or ""
            ),
            "requirementVerdict": str(
                row["review_requirement_verdict"] or ""
            ),
            "evidenceRefs": [
                str(value)
                for value in json.loads(
                    str(row["review_evidence_refs_json"] or "[]")
                )
            ],
            "reason": str(row["review_reason"] or ""),
            "reviewerParticipantId": str(
                row["reviewer_participant_id"] or ""
            ),
            "reviewedAtMs": (
                int(row["reviewed_at_ms"])
                if row["reviewed_at_ms"] is not None
                else None
            ),
        },
        "blocker": dict(json.loads(str(row["blocker_json"] or "{}"))),
        "acceptedTurnId": str(row["accepted_turn_id"] or ""),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "completedAtMs": (
            int(row["completed_at_ms"])
            if row["completed_at_ms"] is not None
            else None
        ),
    }
    validate_contract(payload, "agent-room-work-item.v1.json")
    return payload


def _participant_for_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    active_only: bool = True,
) -> sqlite3.Row:
    clause = (
        "AND p.participant_status = 'active' AND r.status = 'active'"
        if active_only
        else ""
    )
    row = conn.execute(
        f"""
        SELECT p.*, r.status AS room_status
        FROM agent_room_participants p
        JOIN agent_rooms r ON r.id = p.room_id
        WHERE p.session_id = ? {clause}
        """,  # noqa: S608 - clause is a fixed internal string
        (str(session_id or "").strip(),),
    ).fetchone()
    if row is None:
        raise ValueError("session is not an active Room participant")
    return row


def _participant(
    conn: sqlite3.Connection,
    room_id: str,
    participant_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM agent_room_participants
        WHERE id = ? AND room_id = ? AND participant_status = 'active'
        """,
        (participant_id, room_id),
    ).fetchone()
    if row is None:
        raise ValueError("target is not an active participant in the same Room")
    return row


def _text_list(
    value: object,
    name: str,
    *,
    maximum_items: int,
    maximum_length: int,
    required: bool = False,
) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be an array")
    items = [
        _required_text(item, name, maximum=maximum_length)
        for item in value
    ]
    if required and not items:
        raise ValueError(f"{name} must not be empty")
    if len(items) > maximum_items:
        raise ValueError(f"{name} accepts at most {maximum_items} items")
    return items


def _required_text(value: object, name: str, *, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    if len(text) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return text


def _revision(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if not 0 <= value <= MAX_REVISIONS:
        raise ValueError(f"{name} must be between 0 and {MAX_REVISIONS}")
    return value


def _verdict(value: object, name: str, *, allowed: frozenset[str]) -> str:
    verdict = _required_text(value, name, maximum=40)
    if verdict not in allowed:
        raise ValueError(
            f"{name} must be one of: {', '.join(sorted(allowed))}"
        )
    return verdict


def _optional_verdict(
    value: object,
    name: str,
    *,
    allowed: frozenset[str],
) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text not in allowed:
        raise ValueError(
            f"{name} must be one of: {', '.join(sorted(allowed))}"
        )
    return text


def infer_proposed_verdicts_from_summary(summary: str) -> tuple[str, str]:
    """Derive honest Partner-proposed axes from submission prose.

    Explicit axis tokens win. Standalone FAILED / UNVERIFIED markers only
    populate non-passing proposals so Facilitator accept cannot paper over
    a Partner failure claim. Passing claims are never inferred.
    """

    text = str(summary or "")
    operability = ""
    requirement = ""
    for match in re.finditer(
        r"(?:proposed)?operability(?:Verdict)?\s*[:=]\s*"
        r"(passed|failed|unverified)",
        text,
        flags=re.IGNORECASE,
    ):
        operability = match.group(1).lower()
    for match in re.finditer(
        r"(?:proposed)?requirement(?:Verdict)?\s*[:=]\s*"
        r"(satisfied|not_satisfied|unverified)",
        text,
        flags=re.IGNORECASE,
    ):
        requirement = match.group(1).lower()
    if operability or requirement:
        return operability, requirement
    if re.search(r"\bUNVERIFIED\b", text):
        return "unverified", "unverified"
    if re.search(r"\bFAILED\b", text):
        return "failed", "not_satisfied"
    return "", ""


def _optional_text(value: object, *, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError(f"value exceeds {maximum} characters")
    return text


def _bounded(value: object, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _wake_condition(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("wakeCondition must be an object")
    allowed = {"kind", "sourceId", "description"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"unsupported wakeCondition fields: {', '.join(sorted(str(key) for key in unknown))}"
        )
    kind = str(value.get("kind") or "").strip()
    if kind and kind not in {"manual", "message", "artifact", "time", "external"}:
        raise ValueError("unsupported wakeCondition kind")
    result: dict[str, str] = {}
    if kind:
        result["kind"] = kind
    for key, maximum in (("sourceId", 240), ("description", 500)):
        text = str(value.get(key) or "").strip()
        if len(text) > maximum:
            raise ValueError(f"wakeCondition {key} exceeds {maximum} characters")
        if text:
            result[key] = text
    return result


def _timestamp(value: int | None = None) -> int:
    return int(value if value is not None else time.time() * 1000)
