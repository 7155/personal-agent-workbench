"""Read-only JEV UI projection over SQLite events; no Runtime is launched."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from rag_ime.jev_tasks.application import JevRoomApplication
from rag_ime.jev_tasks.ledger import Snapshot
from rag_ime.jev_tasks.types import Task
from rag_ime.pi.runtime import PiRuntimeHostManager, _HostedSessionState


class JevExecutionStatusTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE agent_runtime_events(session_id TEXT,turn_id TEXT,sequence INTEGER,event_type TEXT,metrics_json TEXT)")
        self.conn.execute("CREATE TABLE agent_jev_execution_drains(dispatch_id TEXT,proof_json TEXT)")

        @contextmanager
        def connection():
            yield self.conn

        self.app = object.__new__(JevRoomApplication)
        self.app.ledger = SimpleNamespace(connection=connection)
        self.app.service = Mock()
        self.app.service.runtime.is_turn_active.return_value = True
        self.effect = {"operation": "dispatch", "state": "accepted", "effectId": "dispatch-1",
            "request": {"sessionId": "session-1"}, "receipt": {"turnId": "pi-turn-1"}}

    def tearDown(self):
        self.app.service.runtime.await_turn_settled.assert_not_called()

    def event(self, kind, *, session="session-1", turn="pi-turn-1", metrics=None):
        sequence = self.conn.execute("SELECT COUNT(*) FROM agent_runtime_events").fetchone()[0] + 1
        self.conn.execute("INSERT INTO agent_runtime_events VALUES(?,?,?,?,?)",
            (session, turn, sequence, kind, json.dumps(metrics or {})))

    def test_ack_and_busy_projection_only_mean_admitted(self):
        self.assertEqual(self.app.execution_status(self.effect), "admitted")
        self.event("status_changed", metrics={"status": "busy"})
        self.assertEqual(self.app.execution_status(self.effect), "admitted")

    def test_actual_runtime_activity_marks_exact_turn_running_without_turn_started(self):
        for kind in ("tool_started", "tool_progress", "tool_finished", "provider_request_completed",
                     "provider_request_failed", "text_delta", "reasoning_summary", "message_completed"):
            with self.subTest(kind=kind):
                self.conn.execute("DELETE FROM agent_runtime_events")
                self.event(kind)
                self.assertEqual(self.app.execution_status(self.effect), "running")

    def test_terminal_without_drain_is_unknown_even_with_late_activity(self):
        for kind in ("turn_completed", "turn_failed"):
            with self.subTest(kind=kind):
                self.conn.execute("DELETE FROM agent_runtime_events")
                self.event("tool_started")
                self.event(kind)
                self.event("tool_finished")
                self.assertEqual(self.app.execution_status(self.effect), "unknown")

    def test_owner_drain_proof_wins_without_querying_live_runtime(self):
        self.event("turn_completed")
        proof = {"status": "drained", "terminal": "completed", "proofRef": "pi-settlement:fixture",
            "turnId": "pi-turn-1", "effectsReconciled": True,
            "settlement": {"sessionId": "session-1", "turnId": "pi-turn-1", "receipt": {
                "receiptId": "fixture", "runId": "pi-turn-1", "pendingOperations": 0}},
            "descendantsProof": {"settled": True, "dispatchId": "dispatch-1", "turnId": "pi-turn-1"}}
        self.conn.execute("INSERT INTO agent_jev_execution_drains VALUES(?,?)", ("dispatch-1", json.dumps(proof)))
        self.assertEqual(self.app.execution_status(self.effect), "drained")

    def test_other_session_and_other_turn_cannot_supply_execution_evidence(self):
        self.event("tool_started", turn="another-turn")
        self.event("provider_request_completed", session="another-session")
        self.assertEqual(self.app.execution_status(self.effect), "admitted")
        self.event("tool_started")
        self.event("turn_completed", turn="another-turn")
        self.assertEqual(self.app.execution_status(self.effect), "running")

    def test_missing_pi_turn_identity_cannot_match_unscoped_events(self):
        self.effect["receipt"] = {}
        self.event("tool_started", turn="")
        self.assertEqual(self.app.execution_status(self.effect), "unknown")

    def test_old_activity_after_host_restart_is_unknown_without_current_exact_turn(self):
        self.event("tool_finished")
        self.app.service.runtime.is_turn_active.return_value = False
        self.assertEqual(self.app.execution_status(self.effect), "unknown")
        self.app.service.runtime.is_turn_active.assert_called_once_with(
            "session-1", "pi-turn-1", client_message_id="dispatch-1")

    def test_no_runtime_activity_observer_is_unknown_not_assumed_running(self):
        self.event("provider_request_completed")
        self.app.service.runtime = SimpleNamespace()
        self.assertEqual(self.app.execution_status(self.effect), "unknown")
        self.app.service.runtime = Mock()  # Preserve the no-settlement-query assertion.

    def bound_frontier(self):
        task = Task("task", "root", "room", "active", 1, "owner", "owner",
                    "assignment", "dispatch-1", "objective", "output", ("check",))
        snapshot = Snapshot("graph", "room", "root", "task", "controller", "owner",
                            "session-1", "fingerprint", 0, (task,), (), "[]")
        self.effect["request"].update({"taskId": task.id, "taskRevision": task.revision,
            "ownerId": task.owner_id, "assignmentKey": task.assignment_key,
            "acceptedTurnId": task.accepted_turn_id})
        self.app.lifecycle = SimpleNamespace(effect_for_dispatch=Mock(return_value=self.effect))
        self.app.service.rooms.participant.return_value = {"sessionId": "session-1"}
        self.app.service.room_turns.active_turn.return_value = ("root", "dispatch-1")
        runtime = object.__new__(PiRuntimeHostManager)
        runtime._lock = threading.RLock()
        runtime._client = Mock(running=True)
        runtime._open_sessions = {"session-1"}
        runtime._states = {"session-1": _HostedSessionState(
            turn_id="pi-turn-1", client_message_id="dispatch-1")}
        runtime.ensure = Mock()
        runtime.await_turn_settled = Mock()
        self.app.service.runtime = runtime
        return snapshot, runtime

    def test_frontier_does_not_treat_restored_mapping_as_live_execution(self):
        snapshot, runtime = self.bound_frontier()
        self.event("tool_finished")
        self.conn.execute("CREATE TABLE claims(dispatch_id TEXT)")
        self.conn.execute("INSERT INTO claims VALUES('dispatch-1')")
        writes = self.conn.total_changes
        self.assertEqual(snapshot.graph().frontier(self.app.executions(snapshot)).running, ("task",))
        for state in (_HostedSessionState(),
                      _HostedSessionState(turn_id="new-turn", client_message_id="dispatch-1"),
                      _HostedSessionState(turn_id="pi-turn-1", client_message_id="new-dispatch")):
            with self.subTest(turn=state.turn_id, dispatch=state.client_message_id):
                runtime._states["session-1"] = state
                facts = self.app.executions(snapshot)
                self.assertEqual(facts["task"].status, "unknown")
                self.assertFalse(facts["task"].effects_reconciled)
                frontier = snapshot.graph().frontier(facts)
                self.assertEqual(frontier.running, ())
                self.assertEqual(frontier.ready, ())
                self.assertEqual(frontier.blocked, (("task", ("execution_unknown",)),))
                self.assertEqual(self.app.execution_status(self.effect), "unknown")
        self.assertEqual(self.conn.total_changes, writes)
        self.assertEqual(self.conn.execute("SELECT dispatch_id FROM claims").fetchall(), [("dispatch-1",)])
        self.assertEqual(snapshot.task("task").state, "active")
        runtime.ensure.assert_not_called()
        runtime._client.send.assert_not_called()

    def test_recovered_exact_drain_receipt_unblocks_frontier_without_live_turn(self):
        snapshot, runtime = self.bound_frontier()
        runtime._states["session-1"] = _HostedSessionState()
        self.assertEqual(self.app.executions(snapshot)["task"].status, "unknown")
        # The authoritative settlement and descendant verifier persist this
        # drain; this projection must consume it without issuing more work.
        proof = {"proofRef": "pi-settlement:recovered", "terminal": "aborted"}
        self.conn.execute("INSERT INTO agent_jev_execution_drains VALUES(?,?)",
                          ("dispatch-1", json.dumps(proof)))
        facts = self.app.executions(snapshot)
        self.assertEqual(facts["task"].status, "drained")
        self.assertTrue(facts["task"].effects_reconciled)
        self.assertEqual(facts["task"].proof_ref, "pi-settlement:recovered")
        self.assertEqual(self.app.execution_status(self.effect), "drained")
        self.assertEqual(snapshot.graph().frontier(facts).running, ())
        self.assertEqual(snapshot.task("task").state, "active")
        runtime._client.send.assert_not_called()

    def test_unresolved_or_unadmitted_dispatch_preserves_existing_projection(self):
        self.event("tool_started")
        for state, expected in (("pending", "prepared"), ("sending", "unknown"),
                                ("unknown", "unknown"), ("not_sent", "rejected"), ("rejected", "rejected")):
            with self.subTest(state=state):
                effect = deepcopy(self.effect)
                effect["state"] = state
                self.assertEqual(self.app.execution_status(effect), expected)


if __name__ == "__main__":
    unittest.main()
