"""Graph topology and exact-command receipts next to canonical WorkItems.

Only control metadata is written here. Task status/owner/result mutations are
performed through AgentRoomWorkStore under work_transaction hooks.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .graph import TaskGraph
from .types import Edge, GraphConflict, GraphError, Task, canonical, digest, text


@dataclass(frozen=True)
class Snapshot:
    graph_id: str
    room_id: str
    root_id: str
    root_work_id: str
    controller_id: str
    participant_id: str
    session_id: str
    fingerprint: str
    topology_revision: int
    tasks: tuple[Task, ...]
    edges: tuple[Edge, ...]
    reclaims_json: str
    effects_json: str = "[]"
    reserved_sessions: tuple[str, ...] = ()
    active_task_ids: tuple[str, ...] = ()
    pending_revision_task_ids: tuple[str, ...] = ()

    @property
    def active_tasks(self) -> tuple[Task, ...]:
        active = set(self.active_task_ids) if self.active_task_ids else {t.id for t in self.tasks}
        return tuple(task for task in self.tasks if task.id in active)

    def is_active(self, task_id: str) -> bool:
        return task_id in {task.id for task in self.active_tasks}

    def graph(self) -> TaskGraph:
        active = {task.id for task in self.active_tasks}
        edges = tuple(edge for edge in self.edges
                      if edge.prerequisite in active and edge.dependent in active)
        return TaskGraph(self.active_tasks, edges, root_id=self.root_id, room_id=self.room_id)

    def admission_hash(self, task_id: str) -> str:
        """Freeze only the inputs relevant to one prepared execution.

        Claiming or revising an independent sibling must not invalidate this
        effect. The task, its current dependency IDs/results and incoming
        context edges are the relevant plan facts; Runtime still rechecks
        permissions and reservations at admission.
        """
        graph = self.graph()
        related = {task_id}
        queue = [task_id]
        visited = set()
        while queue:
            node = queue.pop()
            if node in visited:
                continue
            visited.add(node)
            prerequisites = graph.requires[node] | set(graph.parents[node])
            related.update(prerequisites)
            queue.extend(prerequisites - visited)
        inbound = [asdict(edge) for edge in self.edges
                   if edge.dependent in related and edge.prerequisite in graph.tasks]
        return digest([self.graph_id, self.room_id, self.root_id,
                       self.controller_id, self.participant_id, self.session_id,
                       [asdict(graph.tasks[key]) for key in sorted(related)], inbound])

    def pending_dispatches(self, task_id: str) -> tuple[dict[str, object], ...]:
        return tuple(effect for effect in json.loads(self.effects_json)
                     if effect["operation"] == "dispatch" and effect.get("purpose", "execute") == "execute" and effect["taskId"] == task_id
                     and effect["state"] in {"pending", "sending", "unknown"})

    def task(self, task_id: str) -> Task:
        for item in self.tasks:
            if item.id == task_id:
                return item
        raise GraphError("task is not in this graph")


def task_from_row(row: Mapping[str, object]) -> Task:
    def unpack(column: str) -> object:
        return json.loads(str(row.get(column) or "[]"))
    return Task.from_payload({
        "id": row["id"], "rootTurnId": row["root_turn_id"], "roomId": row["room_id"],
        "state": row["state"], "revision": row["revision"],
        "currentOwnerParticipantId": row["current_owner_participant_id"],
        "accountableParticipantId": row["accountable_participant_id"],
        "assignmentKey": row["assignment_key"], "acceptedTurnId": row.get("accepted_turn_id") or "",
        "objective": row["objective"], "expectedOutput": row["expected_output"],
        "acceptanceCriteria": unpack("acceptance_criteria_json"),
        "parentWorkId": row.get("parent_work_id") or "",
        "artifactRefs": unpack("artifact_refs_json"), "evidenceRefs": unpack("evidence_refs_json"),
        "resultSummary": row.get("result_summary") or "",
        "completedAtMs": row.get("completed_at_ms"),
    })


class GraphLedger:
    def __init__(self, db_path: str | Path, *, clock_ms: Callable[[], int] | None = None):
        self.db_path = Path(db_path).resolve()
        if not self.db_path.is_file():
            raise GraphError("the canonical PAW database must already exist")
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        # The normal PAW migration owner, never this constructor, installs DDL.
        with self.connection() as conn:
            conn.execute("SELECT graph_id FROM agent_jev_graphs LIMIT 0")

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def register_created_root(self, *, graph_id: str, room_id: str, root_id: str,
                              root_work_id: str, controller_id: str,
                              participant_id: str, session_id: str) -> Snapshot:
        """Internal bootstrap, called only by the *new Jev Root* creation owner.

        This is deliberately not an HTTP mode-toggle or an adoption API for a
        running legacy Room. The host must create the root with Jev ownership
        before enabling its worker callbacks; that host wiring is a later batch.
        """
        values = (graph_id, room_id, root_id, root_work_id, controller_id, participant_id, session_id)
        for value in values:
            text(value, "root binding")
        with self.connection(write=True) as conn:
            old = conn.execute("SELECT * FROM agent_jev_graphs WHERE graph_id=?", (graph_id,)).fetchone()
            if old:
                columns = ("graph_id", "room_id", "root_turn_id", "root_work_id", "controller_id",
                           "controller_participant_id", "controller_session_id")
                if tuple(old[k] for k in columns) != values:
                    raise GraphConflict("a Root's controller binding cannot be changed")
                return self.read_in_transaction(conn, graph_id, controller_id)
            root = conn.execute("SELECT * FROM agent_room_work_items WHERE id=?", (root_work_id,)).fetchone()
            room = conn.execute("SELECT status FROM agent_rooms WHERE id=?", (room_id,)).fetchone()
            actor = conn.execute("SELECT * FROM agent_room_participants WHERE id=? AND room_id=?",
                                 (participant_id, room_id)).fetchone()
            if (root is None or root["room_id"] != room_id or root["root_turn_id"] != root_id
                or root["state"] != "active" or room is None or room["status"] != "active"
                or actor is None or actor["participant_status"] != "active"
                or actor["session_id"] != session_id or root["accountable_participant_id"] != participant_id):
                raise GraphError("invalid new Jev Root owner binding")
            # Do not bind a root that already has real delegated work in flight.
            table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_room_partner_dispatches'").fetchone()
            if table and conn.execute(
                "SELECT 1 FROM agent_room_partner_dispatches WHERE room_id=? AND root_id=? LIMIT 1",
                (room_id, root_id),
            ).fetchone():
                raise GraphConflict("cannot attach Jev to an already delegated legacy root")
            conn.execute("INSERT INTO agent_jev_graphs VALUES(?,?,?,?,?,?,?,'jev',0,?)",
                         (*values, self.clock_ms()))
            return self.read_in_transaction(conn, graph_id, controller_id)

    def snapshot(self, graph_id: str, controller_id: str) -> Snapshot:
        with self.connection() as conn:
            return self.read_in_transaction(conn, graph_id, controller_id)

    def read_in_transaction(self, conn: sqlite3.Connection, graph_id: str, controller_id: str) -> Snapshot:
        g = conn.execute("SELECT * FROM agent_jev_graphs WHERE graph_id=?", (graph_id,)).fetchone()
        if g is None or g["mode"] != "jev" or g["controller_id"] != controller_id:
            raise GraphConflict("Jev graph/controller binding unavailable")
        room = conn.execute("SELECT * FROM agent_rooms WHERE id=?", (g["room_id"],)).fetchone()
        actors = conn.execute("SELECT * FROM agent_room_participants WHERE room_id=? ORDER BY id", (g["room_id"],)).fetchall()
        if room is None:
            raise GraphConflict("room authority unavailable")
        rows = conn.execute(
            "SELECT * FROM agent_room_work_items WHERE room_id=? AND root_turn_id=? ORDER BY id",
            (g["room_id"], g["root_turn_id"])).fetchall()
        if len(rows) > 2000:
            raise GraphError("graph exceeds task bound; never treat a truncated list as complete")
        raw_edges = conn.execute("SELECT prerequisite,dependent,kind FROM agent_jev_edges WHERE graph_id=? ORDER BY prerequisite,dependent,kind", (graph_id,)).fetchall()
        reclaims = [dict(r) for r in conn.execute(
            "SELECT * FROM agent_jev_reclaims WHERE graph_id=? AND status='requested' ORDER BY reclaim_id", (graph_id,))]
        effects = []
        for effect in conn.execute(
            "SELECT effect_id,operation,state,request_json FROM agent_jev_runtime_effects "
            "WHERE graph_id=? AND state IN ('pending','sending','unknown') ORDER BY effect_id", (graph_id,)
        ):
            request = json.loads(effect["request_json"])
            effects.append({"effectId": effect["effect_id"], "operation": effect["operation"],
                            "state": effect["state"], "taskId": request["taskId"], "purpose": request.get("purpose", "execute")})
        reservations = tuple(row[0] for row in conn.execute(
            "SELECT session_id FROM agent_jev_executor_claims WHERE session_id IN "
            "(SELECT session_id FROM agent_room_participants WHERE room_id=?) ORDER BY session_id", (g["room_id"],)))
        supersessions = [dict(r) for r in conn.execute(
            "SELECT old_task_id,new_task_id,revision_id FROM agent_jev_task_supersessions "
            "WHERE graph_id=? ORDER BY old_task_id", (graph_id,))]
        pending_removals = [tuple(r) for r in conn.execute(
            "SELECT removal_id,participant_id,stop_root FROM agent_jev_participant_removals "
            "WHERE room_id=? AND status='pending' ORDER BY removal_id", (g["room_id"],))]
        pending_revisions = [dict(r) for r in conn.execute(
            "SELECT revision_id,status,affected_json FROM agent_jev_task_revisions "
            "WHERE graph_id=? AND status='awaiting_drain'", (graph_id,))]
        pending_target_ids = tuple(sorted({task_id for row in pending_revisions
                                           for task_id in json.loads(row["affected_json"])}))
        # Public activity advances these display fields on every tool event.
        # A live sibling (or this worker's own tool callback) is not a change
        # to execution authority. Keep all Room policy/identity fields and the
        # complete task, assignment, topology and reservation observations.
        room_authority = {key: room[key] for key in room.keys()
                          if key not in {"last_event_sequence", "updated_at_ms"}}
        value = {"binding": dict(g), "room": room_authority, "participants": [dict(a) for a in actors],
                 "tasks": [dict(r) for r in rows], "edges": [list(e) for e in raw_edges], "reclaims": reclaims, "effects": effects,
                 "reservedSessions": reservations, "supersessions": supersessions,
                 "pendingRevisions": pending_revisions, "pendingRemovals": pending_removals}
        tasks = tuple(task_from_row(dict(row)) for row in rows)
        if not any(t.id == g["root_work_id"] for t in tasks):
            raise GraphError("graph's Root WorkItem missing")
        old_ids = {row["old_task_id"] for row in supersessions}
        active_ids = tuple(sorted(task.id for task in tasks if task.id not in old_ids))
        snapshot = Snapshot(graph_id, g["room_id"], g["root_turn_id"], g["root_work_id"],
                            controller_id, g["controller_participant_id"], g["controller_session_id"],
                            digest(value), g["topology_revision"], tasks,
                            tuple(Edge(*tuple(e)) for e in raw_edges), canonical(reclaims), canonical(effects),
                            reservations, active_ids, pending_target_ids)
        snapshot.graph()  # Inconsistent authoritative input is not an empty frontier.
        return snapshot

    def require_unchanged(self, conn: sqlite3.Connection, expected: Snapshot, *, allowed_root_states: tuple[str, ...] = ("active",)) -> Snapshot:
        current = self.read_in_transaction(conn, expected.graph_id, expected.controller_id)
        if current.fingerprint != expected.fingerprint:
            raise GraphConflict("task/assignment/requirements/topology changed")
        root = current.task(current.root_work_id)
        room = conn.execute("SELECT status FROM agent_rooms WHERE id=?", (current.room_id,)).fetchone()
        if room[0] != "active" or root.state not in allowed_root_states:
            raise GraphConflict("root is not accepting new work")
        actor = conn.execute("SELECT participant_status,session_id FROM agent_room_participants WHERE id=? AND room_id=?",
                             (current.participant_id, current.room_id)).fetchone()
        if actor is None or actor[0] != "active" or actor[1] != current.session_id:
            raise GraphConflict("controller's accountable identity is no longer active")
        return current

    @staticmethod
    def prior(conn: sqlite3.Connection, command_id: str, graph_id: str, intent_hash: str) -> dict[str, object] | None:
        row = conn.execute("SELECT graph_id,intent_hash,result_json FROM agent_jev_commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            return None
        if row[0] != graph_id or row[1] != intent_hash:
            raise GraphConflict("command ID reused with different intent")
        value = json.loads(row[2])
        return {**value, "replayed": True}

    def save_receipt(self, conn: sqlite3.Connection, command_id: str, snapshot: Snapshot,
                     intent_hash: str, operation: str, task_id: str, result: Mapping[str, object]) -> None:
        conn.execute("INSERT INTO agent_jev_commands VALUES(?,?,?,?,?,?,?)",
                     (command_id, snapshot.graph_id, intent_hash, operation, task_id, canonical(dict(result)), self.clock_ms()))

    def change_edges(self, expected: Snapshot, *, command_id: str, add: Sequence[Edge] = (),
                     remove: Sequence[Edge] = (), command_intent_hash: str | None = None,
                     on_applied: Callable[[sqlite3.Connection], None] | None = None) -> dict[str, object]:
        text(command_id, "command id")
        if len(set(add)) != len(add) or len(set(remove)) != len(remove) or set(add) & set(remove):
            raise GraphError("ambiguous graph patch")
        intent_hash = (text(command_intent_hash, "command intent hash") if command_intent_hash is not None else
                       digest(["edges", expected.fingerprint, [vars(e) for e in sorted(add)], [vars(e) for e in sorted(remove)]]))
        with self.connection(write=True) as conn:
            old = self.prior(conn, command_id, expected.graph_id, intent_hash)
            if old:
                return old
            self.require_unchanged(conn, expected)
            edges = set(expected.edges)
            if not set(remove) <= edges or set(add) & edges:
                raise GraphConflict("edge patch no longer matches graph")
            active = {task.id for task in expected.active_tasks}
            if any(edge.prerequisite not in active or edge.dependent not in active
                   for edge in (*add, *remove)):
                raise GraphConflict("historical task edges cannot be changed")
            final = (edges - set(remove)) | set(add)
            TaskGraph(expected.active_tasks,
                      tuple(edge for edge in final if edge.prerequisite in active and edge.dependent in active),
                      root_id=expected.root_id, room_id=expected.room_id)
            for edge in (*add, *remove):
                # A changed dependency may invalidate a running/completed result.
                # This batch requires explicit plan revision before changing any
                # downstream obligation that has already started.
                target = expected.task(edge.dependent)
                if edge.kind == "requires" and (target.accepted_turn_id or target.state not in {"active", "queued"}):
                    raise GraphConflict("revise/reconcile started work before changing its hard dependencies")
            for e in remove:
                conn.execute("DELETE FROM agent_jev_edges WHERE graph_id=? AND prerequisite=? AND dependent=? AND kind=?",
                             (expected.graph_id, e.prerequisite, e.dependent, e.kind))
            for e in add:
                conn.execute("INSERT INTO agent_jev_edges VALUES(?,?,?,?)", (expected.graph_id, e.prerequisite, e.dependent, e.kind))
            revision = expected.topology_revision + bool(add or remove)
            conn.execute("UPDATE agent_jev_graphs SET topology_revision=? WHERE graph_id=?", (revision, expected.graph_id))
            result = {"commandId": command_id, "status": "applied", "operation": "edges", "topologyRevision": revision, "replayed": False}
            self.save_receipt(conn, command_id, expected, intent_hash, "edges", "", result)
            if on_applied is not None:
                on_applied(conn)
            return result
