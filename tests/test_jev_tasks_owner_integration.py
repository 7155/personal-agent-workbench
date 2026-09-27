"""Run IN the full PAW checkout after applying the WorkStore hook.

Uses real PAW migrations, Session/Room/WorkItem stores. Runtime observations are
explicit test data; this suite does not contact Jev or start Pi/model execution.
This file is not included in the standalone package's test count.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from dataclasses import asdict
from pathlib import Path

from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.rooms.store import AgentRoomStore
from rag_ime.rooms.work import AgentRoomWorkStore
from rag_ime.jev_tasks.ledger import GraphLedger
from rag_ime.jev_tasks.owner import GuardedWorkOwner, execution_dict
from rag_ime.jev_tasks.types import Candidate, Edge, ExecutionFact, GraphConflict, digest


class JevTasksOwnerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-jev-owner-integration-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "acceptance.sqlite"
        self.sessions = AgentSessionStore(self.db_path)
        self.sessions.initialize()
        self.session_records = [self.sessions.create(title="Jev 集成测试 " + name)
                                for name in ("责任身份", "执行者 A", "执行者 B")]
        self.rooms = AgentRoomStore(self.db_path, room_dir=self.root / "rooms")
        self.rooms.initialize()
        self.room = self.rooms.create(title="隔离 Jev 原任务内核验证", routing_policy="moderator", participants=[
            {"sessionId": record["id"], "roleId": role, "roleVersion": "1",
             "displayName": record["title"], "collaborationRole": role}
            for record, role in zip(self.session_records, ("coordinator", "implementer", "researcher"), strict=True)
        ])
        self.lead, self.a, self.b = self.room["participants"]
        self.work = AgentRoomWorkStore(self.db_path)
        self.work.initialize()
        self.root_work = self.create_work("root", self.lead)
        self.child = self.create_work("child", self.a, parent=self.root_work["id"])
        self.second = self.create_work("second", self.b, parent=self.root_work["id"])
        self.ledger = GraphLedger(self.db_path)
        self.owner = GuardedWorkOwner(self.work, self.ledger)
        self.ledger.register_created_root(graph_id="jev-graph:isolated-test", room_id=self.room["id"],
            root_id="jev-root:isolated-test", root_work_id=self.root_work["id"], controller_id="jev-controller:test",
            participant_id=self.lead["id"], session_id=self.lead["sessionId"])

    def create_work(self, name, participant, *, parent=""):
        return self.work.create(room_id=self.room["id"], objective="隔离任务 " + name,
            expected_output="可核实的测试成果", acceptance_criteria=["验证操作与当前要求一致"],
            current_owner_participant_id=participant["id"], created_by_participant_id=self.lead["id"],
            accountable_participant_id=self.lead["id"], client_message_id="fixture-create-" + name,
            root_turn_id="jev-root:isolated-test", parent_work_id=parent, depth=2 if parent else 1)

    def snapshot(self):
        return self.ledger.snapshot("jev-graph:isolated-test", "jev-controller:test")

    def fact(self, task_id, status="idle"):
        task = self.snapshot().task(task_id)
        participant = self.rooms.participant(task.owner_id)
        return ExecutionFact(task.id, status, dispatch_id="fixture-dispatch" if status != "idle" else "",
            session_id=participant["sessionId"], task_revision=task.revision, owner_id=task.owner_id,
            assignment_key=task.assignment_key, accepted_turn_id=task.accepted_turn_id,
            proof_ref="fixture:runtime-observation-not-live", effects_reconciled=True)

    def reassign_candidate(self, target=None):
        return Candidate.make("reassign", self.child["id"], "将原责任交给另一位执行者", {
            "targetParticipantId": target or self.b["id"], "reason": "isolated integration test",
            "execution": execution_dict(self.fact(self.child["id"]))})

    def test_uses_real_workitem_table_and_original_event_ledger(self):
        before = len(self.work.list_events(self.child["id"]))
        self.owner.apply(self.snapshot(), self.reassign_candidate(), command_id="reassign:1")
        item = self.work.get(self.child["id"])
        self.assertEqual(item["currentOwnerParticipantId"], self.b["id"])
        self.assertEqual(len(self.work.list_events(self.child["id"])), before + 1)
        self.assertEqual(len(self.work.list_for_root(room_id=self.room["id"], root_turn_id="jev-root:isolated-test")), 3)

    def test_replay_and_restart_preserve_one_actual_assignment(self):
        s, candidate = self.snapshot(), self.reassign_candidate()
        self.owner.apply(s, candidate, command_id="reassign:1")
        count = len(self.work.list_events(self.child["id"]))
        restarted = GuardedWorkOwner(AgentRoomWorkStore(self.db_path), GraphLedger(self.db_path))
        self.assertTrue(restarted.apply(s, candidate, command_id="reassign:1")["replayed"])
        self.assertEqual(len(self.work.list_events(self.child["id"])), count)

    def test_original_owner_rejection_leaves_no_command_receipt(self):
        with self.assertRaises(ValueError):
            self.owner.apply(self.snapshot(), self.reassign_candidate("missing-participant"), command_id="invalid")
        with self.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_commands").fetchone()[0], 0)
        self.assertEqual(self.work.get(self.child["id"])["currentOwnerParticipantId"], self.a["id"])

    def test_receipt_failure_rolls_back_owner_and_transition_evidence(self):
        before = self.work.get(self.child["id"])
        with self.ledger.connection() as conn:
            events_before = conn.execute("SELECT COUNT(*) FROM agent_room_work_events").fetchone()[0]
        with patch.object(self.ledger, "save_receipt", side_effect=RuntimeError("receipt write failed")):
            with self.assertRaisesRegex(RuntimeError, "receipt write failed"):
                self.owner.apply(self.snapshot(), self.reassign_candidate(), command_id="rollback-receipt")
        self.assertEqual(self.work.get(self.child["id"]), before)
        with self.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_room_work_events").fetchone()[0], events_before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_commands").fetchone()[0], 0)

    def test_original_reassignment_without_revision_bump_invalidates_snapshot(self):
        s, candidate = self.snapshot(), self.reassign_candidate()
        self.work.reassign(self.child["id"], actor_participant_id=self.lead["id"],
                           current_owner_participant_id=self.b["id"], reason="fixture concurrent change")
        self.assertEqual(self.work.get(self.child["id"])["revision"], s.task(self.child["id"]).revision)
        with self.assertRaises(GraphConflict): self.owner.apply(s, candidate, command_id="stale")

    def test_room_activity_is_not_authority_but_room_status_still_is(self):
        snapshot = self.snapshot()
        self.rooms.append_event(room_id=self.room["id"], event_type="participant_activity",
            payload={"kind": "tool_started"}, participant_id=self.b["id"],
            source_session_id=self.b["sessionId"], turn_id=snapshot.root_id)
        self.assertEqual(self.snapshot().fingerprint, snapshot.fingerprint)
        with self.ledger.connection(write=True) as conn:
            conn.execute("UPDATE agent_rooms SET status='archived' WHERE id=?", (self.room["id"],))
        with self.assertRaises(GraphConflict):
            self.owner.apply(snapshot, self.reassign_candidate(), command_id="archived-room")

    def test_task_binding_changes_remain_fenced_after_public_activity(self):
        # These changes cannot be hidden by advancing the Room display cursor.
        for column, value in (("revision", 1), ("assignment_key", "new-assignment"),
                              ("accepted_turn_id", "new-dispatch"),
                              ("current_owner_participant_id", self.b["id"])):
            with self.subTest(column=column):
                snapshot, candidate = self.snapshot(), self.reassign_candidate()
                with self.ledger.connection(write=True) as conn:
                    old = conn.execute(f"SELECT {column} FROM agent_room_work_items WHERE id=?",
                                       (self.child["id"],)).fetchone()[0]
                    conn.execute(f"UPDATE agent_room_work_items SET {column}=? WHERE id=?",
                                 (value, self.child["id"]))
                self.rooms.append_event(room_id=self.room["id"], event_type="participant_activity",
                    payload={"kind": "tool_started"}, turn_id=snapshot.root_id)
                try:
                    with self.assertRaises(GraphConflict):
                        self.owner.apply(snapshot, candidate, command_id="changed-" + column)
                finally:
                    with self.ledger.connection(write=True) as conn:
                        conn.execute(f"UPDATE agent_room_work_items SET {column}=? WHERE id=?",
                                     (old, self.child["id"]))

    def test_submit_and_accept_reuse_original_two_axis_acceptance(self):
        task = self.snapshot().task(self.child["id"])
        self.work.claim_dispatch(task.id, room_id=self.room["id"], owner_participant_id=task.owner_id,
            assignment_key=task.assignment_key, previous_accepted_turn_id="", room_turn_id="fixture-dispatch",
            root_turn_id="jev-root:isolated-test")
        submit = Candidate.make("submit", task.id, "接收实际提交", {
            "resultSummary": "测试提交，不代表真实模型已执行", "artifactRefs": ["fixture:artifact@1"],
            "evidenceRefs": ["fixture:test@1"], "execution": execution_dict(self.fact(task.id, "running"))})
        self.owner.apply(self.snapshot(), submit, command_id="submit:1")
        self.assertEqual(self.work.get(task.id)["state"], "review")
        current = self.snapshot().task(task.id)
        accept = Candidate.make("accept", task.id, "核对当前成果", {
            "reason": "fixture verifier: 两个轴均已单独核对", "evidenceRefs": ["fixture:verification@1"],
            "operabilityVerdict": "passed", "requirementVerdict": "satisfied",
            "verifiedTaskHash": digest(asdict(current)), "execution": execution_dict(self.fact(task.id, "drained"))})
        self.owner.apply(self.snapshot(), accept, command_id="accept:1")
        result = self.work.get(task.id)
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["review"]["operabilityVerdict"], "passed")
        self.assertEqual(result["review"]["requirementVerdict"], "satisfied")

    def test_real_hard_dependency_prevents_early_claim(self):
        from rag_ime.jev_tasks.context import build_manifest
        a, b = self.child["id"], self.second["id"]
        self.ledger.change_edges(self.snapshot(), command_id="edges", add=[Edge(a, b)])
        task = self.snapshot().task(b)
        c = Candidate.make("claim_dispatch", b, "此时不应执行下游", {
            "dispatchId": "fixture:early", "contextManifest": build_manifest(b, task.revision, [], {}).for_executor(),
            "execution": execution_dict(self.fact(b))})
        with self.assertRaises(GraphConflict): self.owner.apply(self.snapshot(), c, command_id="early")
        self.assertEqual(self.work.get(b)["acceptedTurnId"], "")


if __name__ == "__main__": unittest.main()
