from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from rag_ime.db import sqlite_connection
from rag_ime.jev_tasks.runtime_adapter import causal_descendants_proof, cancel_attempt, execution_drained, execution_settlement, recover_retired_attempt
from rag_ime.jev_tasks.types import GraphConflict


class RuntimeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.service = SimpleNamespace(runtime=Mock(), sessions=Mock())
        self.request = {"taskId": "task", "sessionId": "session", "dispatchId": "dispatch", "reclaimId": "reclaim"}
        self.accepted = {**self.request, "state": "accepted", "turnId": "turn", "receiptId": "admitted"}
        self.children = {"settled": True, "sessionId": "session", "dispatchId": "dispatch", "turnId": "turn",
                         "settlementReceiptId": "terminal-proof", "proofRef": "causal:proof"}
        self.settlement = {
            "schemaVersion": "rag-ime.pi-turn-settlement.v1", "sessionId": "session", "turnId": "turn",
            "clientMessageId": "dispatch", "runtimeSessionId": "runtime-session",
            "receipt": {"schemaVersion": "pi.agent-settled.v2", "sessionId": "runtime-session", "runId": "turn",
                        "scopeId": "runtime-session:turn", "receiptId": "terminal-proof", "disposition": "completed",
                        "aborted": False, "pendingOperations": 0, "operations": {"pending": 0},
                        "continuations": {"pendingIds": [], "readyIds": [], "scheduledIds": [], "leasedIds": [],
                                          "counts": {"pending": 0, "leased": 0}}}}
        self.service.runtime.await_turn_settled.return_value = self.settlement
        self.cancel_receipt = {"schemaVersion": "rag-ime.pi-exact-turn-cancel.v1", "sessionId": "session",
            "turnId": "turn", "clientMessageId": "dispatch", "cancelId": "reclaim",
            "state": "accepted", "phase": "requested", "receiptId": "pi-cancel-receipt"}
        self.service.runtime.abort_turn.return_value = self.cancel_receipt

    def test_cancel_contract_needs_no_owner_id_and_preserves_real_receipt(self):
        result = cancel_attempt(self.service, self.request, self.accepted)
        self.assertEqual(result["receiptId"], "pi-cancel-receipt")
        self.assertEqual(result["reclaimId"], "reclaim")
        self.assertNotIn("drained", result)
        self.service.runtime.abort.assert_not_called()
        self.service.runtime.abort_turn.assert_called_once_with("session", "turn", client_message_id="dispatch",
            cancel_id="reclaim", lookup_only=False)

    def recovery_receipt(self):
        return {**self.cancel_receipt, "cancelId": "jev-retired-recovery:dispatch", "phase": "settled",
            "runtimeReceipt": {"schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                "sessionId": "session", "turnId": "turn", "lifecycle": {
                    "schemaVersion": "pi.agent-abort-receipt.v1", "reason": "retired_turn_recovery",
                    "idle": True, "drained": True, "operations": [], "pendingOperations": [],
                    "failedOperationIds": [], "cancelledContinuationIds": []}}}

    def test_retired_recovery_is_exact_and_does_not_itself_claim_drain(self):
        self.service.runtime.abort_turn.return_value = self.recovery_receipt()
        self.assertTrue(recover_retired_attempt(self.service, self.request, self.accepted))
        self.service.runtime.abort_turn.assert_called_once_with("session", "turn",
            client_message_id="dispatch", cancel_id="jev-retired-recovery:dispatch", recover_retired_only=True)
        self.service.runtime.abort.assert_not_called()
        self.service.runtime.await_turn_settled.assert_not_called()

    def test_retired_recovery_rejects_inexact_or_unsettled_receipts(self):
        for field, value in (("turnId", "new-turn"), ("clientMessageId", "new-dispatch"),
                             ("cancelId", "other-recovery"), ("state", "rejected"), ("phase", "requested")):
            with self.subTest(field=field):
                self.service.runtime.abort_turn.return_value = {**self.recovery_receipt(), field: value}
                self.assertFalse(recover_retired_attempt(self.service, self.request, self.accepted))
        for field, value in (("idle", False), ("drained", False), ("pendingOperations", ["tool"]),
                             ("cancelledContinuationIds", ["timer"]), ("reason", "user_abort")):
            with self.subTest(lifecycle=field):
                receipt = self.recovery_receipt()
                receipt["runtimeReceipt"]["lifecycle"][field] = value
                self.service.runtime.abort_turn.return_value = receipt
                self.assertFalse(recover_retired_attempt(self.service, self.request, self.accepted))

    def test_retired_recovery_unavailable_is_unknown_without_abort_fallback(self):
        self.service.runtime.abort_turn.side_effect = TimeoutError("no recovery proof")
        self.assertFalse(recover_retired_attempt(self.service, self.request, self.accepted))
        self.service.runtime.abort.assert_not_called()

    def test_cancel_lookup_never_sends_another_cancel(self):
        cancel_attempt(self.service, self.request, self.accepted, lookup_only=True)
        self.assertTrue(self.service.runtime.abort_turn.call_args.kwargs["lookup_only"])

    def test_cancel_timeout_is_unknown_without_broad_fallback(self):
        self.service.runtime.abort_turn.side_effect = TimeoutError("lost receipt")
        self.assertEqual(cancel_attempt(self.service, self.request, self.accepted)["state"], "unknown")
        self.service.runtime.abort.assert_not_called()

    def test_mismatched_real_receipt_is_not_acceptance(self):
        self.service.runtime.abort_turn.return_value = {**self.cancel_receipt, "clientMessageId": "other"}
        self.assertEqual(cancel_attempt(self.service, self.request, self.accepted), {"state": "unknown"})

    def test_dispatch_identity_mismatch_rejects_before_cancel(self):
        with self.assertRaises(GraphConflict):
            cancel_attempt(self.service, self.request, {**self.accepted, "dispatchId": "new-dispatch"})
        self.service.runtime.abort_turn.assert_not_called()

    def test_terminal_event_alone_cannot_prove_drain(self):
        self.service.runtime.await_turn_settled.side_effect = TimeoutError()
        self.service.sessions.runtime_turn_terminal_event.return_value = {"eventId": "completed"}
        self.assertIsNone(execution_drained(self.service, self.request, self.accepted, descendants_proof=self.children))
        self.service.sessions.runtime_turn_terminal_event.assert_not_called()

    def test_exact_old_settlement_does_not_depend_on_reused_session_idle(self):
        result = execution_drained(self.service, self.request, self.accepted, descendants_proof=self.children)
        self.assertEqual(result["proofRef"], "pi-settlement:terminal-proof")
        self.assertTrue(result["effectsReconciled"])
        self.service.runtime.status.assert_not_called()
        self.service.runtime.await_turn_settled.assert_called_once_with("session", "turn",
            client_message_id="dispatch", timeout_seconds=1.0)

    def test_runtime_settlement_does_not_prove_causal_children_settled(self):
        for proof in (None, {**self.children, "settled": False}, {**self.children, "dispatchId": "different"}):
            with self.subTest(proof=proof):
                self.assertIsNone(execution_drained(self.service, self.request, self.accepted, descendants_proof=proof))

    def test_pending_operations_and_continuations_block_settlement(self):
        for field, value in (("pendingOperations", 1), ("operations", {"pending": 1}),
                             ("disposition", "suspended"), ("runId", "other"),
                             ("continuations", {**self.settlement["receipt"]["continuations"], "pendingIds": ["timer"]})):
            with self.subTest(field=field):
                actual = copy.deepcopy(self.settlement)
                actual["receipt"][field] = value
                self.service.runtime.await_turn_settled.return_value = actual
                self.assertIsNone(execution_settlement(self.service, self.request, self.accepted))


class CausalDescendantTests(unittest.TestCase):
    def setUp(self):
        fixture = RuntimeAdapterTests()
        fixture.setUp()
        self.service, self.request, self.accepted = fixture.service, fixture.request, fixture.accepted
        self.tmp = tempfile.TemporaryDirectory(prefix="jev-causal-")
        self.addCleanup(self.tmp.cleanup)
        self.service.db_path = Path(self.tmp.name) / "owners.sqlite"
        with sqlite_connection(self.service.db_path) as conn:
            conn.execute("CREATE TABLE agent_subagent_batches(id TEXT, parent_session_id TEXT, parent_run_id TEXT, causal_dispatch_id TEXT, state TEXT)")
            conn.execute("CREATE TABLE agent_subagent_runs(id TEXT, batch_id TEXT)")
            conn.execute("CREATE TABLE agent_background_jobs(job_id TEXT, session_id TEXT, causal_dispatch_id TEXT, causal_turn_id TEXT, status TEXT)")
        self.batches, self.jobs, self.snapshots = {}, {}, {}
        self.service.delegation = SimpleNamespace(store=SimpleNamespace(
            get_batch=Mock(side_effect=lambda key, **kwargs: self.batches[key]),
            artifacts=SimpleNamespace(snapshot=lambda **kwargs: self.snapshots.get(kwargs["owner_id"])) ))
        self.service.background_jobs = SimpleNamespace(status=Mock(side_effect=lambda sid, job: {"job": self.jobs[job]}))

    def child(self, run_id="child", dispatch="dispatch", state="running", *, parent_run=""):
        batch_id = "batch:" + run_id
        with sqlite_connection(self.service.db_path) as conn:
            conn.execute("INSERT INTO agent_subagent_batches VALUES(?,?,?,?,?)", (batch_id, "session", parent_run, dispatch, state))
            conn.execute("INSERT INTO agent_subagent_runs VALUES(?,?)", (run_id, batch_id))
        self.batches[batch_id] = {"runs": [{"id": run_id, "childSessionId": "session:" + run_id,
            "state": state, "startedAtMs": 1, "completedAtMs": 2 if state not in {"running", "queued"} else None,
            "updatedAtMs": 2}]}

    def job(self, job_id="job", dispatch="dispatch", state="running", *, session="session", turn=""):
        with sqlite_connection(self.service.db_path) as conn:
            conn.execute("INSERT INTO agent_background_jobs VALUES(?,?,?,?,?)", (job_id, session, dispatch, turn, state))
        self.jobs[job_id] = {"status": state, "updatedAtMs": 3,
                             "endedAtMs": 4 if state not in {"running", "queued", "cancelling"} else 0}

    def proof(self):
        return causal_descendants_proof(self.service, self.request, self.accepted)

    def test_no_owned_resources_is_a_real_empty_owner_snapshot(self):
        proof = self.proof()
        self.assertTrue(proof["settled"])
        self.assertTrue(proof["proofRef"].startswith("causal-owners:"))
        self.assertEqual(proof["settlementReceiptId"], "terminal-proof")
        self.assertTrue(execution_drained(self.service, self.request, self.accepted)["effectsReconciled"])

    def test_scoped_sibling_and_new_session_dispatch_do_not_block_old_attempt(self):
        self.child("sibling", "new-dispatch")
        self.job("sibling-job", "new-dispatch", turn="new-turn")
        self.assertTrue(self.proof()["settled"])
        self.service.delegation.store.get_batch.assert_not_called()
        self.service.background_jobs.status.assert_not_called()

    def test_exact_running_child_and_job_keep_independent_blockers(self):
        self.child()
        self.job()
        proof = self.proof()
        self.assertFalse(proof["settled"])
        self.assertEqual(set(proof["pending"]), {"delegation:child", "delegation_batch:batch:child", "background_job:job"})

    def test_nested_legacy_child_and_its_unscoped_jobs_remain_in_lineage(self):
        self.child(state="completed")
        self.child("grandchild", "", parent_run="child")
        self.job("grandchild-job", "", session="session:grandchild")
        proof = self.proof()
        self.assertIn("delegation:grandchild", proof["pending"])
        self.assertIn("background_job:grandchild-job", proof["pending"])

    def test_orphaned_job_and_forced_child_terminal_are_not_drain_proof(self):
        self.child(state="aborted")
        self.snapshots["child"] = {"supervision": {"phase": "forced"}}
        self.job(state="orphaned")
        proof = self.proof()
        self.assertIn("delegation_forced_unproven:child", proof["pending"])
        self.assertIn("background_job:job", proof["pending"])

    def preview(self):
        self.request.update(purpose="execute", roomId="room", rootId="root", ownerId="partner",
                            taskRevision=0, assignmentKey="assignment")
        self.job(turn="turn")
        self.jobs["job"].update(jobId="job", sessionId="session", command="npm run start -- --port 8787",
            cwd="/tmp/project", pid=123, startedAtMs=1,
            causalMetadata={"turnId": "turn", "roomBound": True},
            roomLineage={"roomId": "room", "rootId": "root", "dispatchId": "dispatch", "taskId": ""})
        self.submission = {"id": "task", "roomId": "room", "rootTurnId": "root", "state": "review",
            "revision": 0, "acceptedTurnId": "dispatch", "currentOwnerParticipantId": "partner",
            "assignmentKey": "assignment", "resultSummary": "Delivered preview retained by its normal job owner",
            "artifactRefs": [], "evidenceRefs": ["job"]}
        self.service.room_work = SimpleNamespace(get=Mock(side_effect=lambda identity: copy.deepcopy(self.submission)))

    def test_submitted_owned_live_preview_does_not_block_completed_pi_turn(self):
        self.preview()
        proof = self.proof()
        self.assertTrue(proof["settled"], proof)
        resource = proof["resources"][0]
        self.assertEqual(resource["state"], "running")
        self.assertEqual(resource["endedAtMs"], 0)
        self.assertEqual(resource["retention"]["kind"], "retained_preview")
        self.assertTrue(resource["retention"]["submissionRef"].startswith("work-submission:"))
        self.assertTrue(execution_drained(self.service, self.request, self.accepted)["effectsReconciled"])
        self.service.runtime.abort_turn.assert_not_called()

    def test_preview_reference_does_not_release_other_live_jobs_or_children(self):
        self.preview()
        self.job("unfinished-test", turn="turn")
        self.child()
        proof = self.proof()
        self.assertFalse(proof["settled"])
        self.assertNotIn("background_job:job", proof["pending"])
        self.assertIn("background_job:unfinished-test", proof["pending"])
        self.assertIn("delegation:child", proof["pending"])

    def test_missing_or_stale_submission_and_wrong_preview_identity_remain_blocking(self):
        self.preview()
        original_job, original_submission = copy.deepcopy(self.jobs["job"]), copy.deepcopy(self.submission)
        variants = [
            ("submission", "evidenceRefs", []), ("submission", "state", "active"),
            ("submission", "acceptedTurnId", "other-dispatch"), ("submission", "revision", 1),
            ("submission", "assignmentKey", "other-assignment"),
            ("job", "command", "npm run test"), ("job", "command", "npm run start; echo other"),
            ("job", "status", "cancelling"), ("job", "status", "orphaned"),
            ("job", "sessionId", "other-session"),
            ("job", "roomLineage", {**original_job["roomLineage"], "dispatchId": "other-dispatch"}),
            ("job", "causalMetadata", {"turnId": "other-turn", "roomBound": True}),
        ]
        for target, key, value in variants:
            with self.subTest(target=target, key=key, value=value):
                self.jobs["job"] = copy.deepcopy(original_job)
                self.submission = copy.deepcopy(original_submission)
                (self.jobs["job"] if target == "job" else self.submission)[key] = value
                self.assertIn("background_job:job", self.proof()["pending"])

    def test_failed_or_aborted_pi_turn_cannot_retain_preview_as_successful_delivery(self):
        self.preview()
        for disposition in ("failed", "aborted"):
            with self.subTest(disposition=disposition):
                self.service.runtime.await_turn_settled.return_value["receipt"].update(
                    disposition=disposition, aborted=disposition == "aborted")
                self.assertIn("background_job:job", self.proof()["pending"])

    def test_preview_submission_changed_during_snapshot_prevents_immutable_drain(self):
        self.preview()
        self.service.room_work.get.side_effect = [copy.deepcopy(self.submission),
            {**self.submission, "acceptedTurnId": "new-dispatch"}]
        proof = self.proof()
        self.assertFalse(proof["settled"])
        self.assertIn("retained_preview_submission_changed:job", proof["pending"])
        self.assertEqual(proof["proofRef"], "")

    def test_actual_terminal_owner_resources_allow_drain(self):
        self.child(state="completed")
        self.job(state="cancelled")
        self.assertTrue(self.proof()["settled"])
        self.assertEqual(len(self.proof()["resources"]), 2)

    def test_unknown_owner_data_remains_blocking(self):
        self.child(state="failed")
        self.assertIn("delegation_terminal_unproven:child", self.proof()["pending"])
        self.service.delegation.store.get_batch.side_effect = KeyError("lost owner")
        self.assertFalse(self.proof()["settled"])

    def test_new_grandchild_during_terminal_owner_read_prevents_drain(self):
        self.child()
        def finish_while_spawning(key, **kwargs):
            if key == "batch:child" and "batch:grandchild" not in self.batches:
                self.child("grandchild", "", parent_run="child")
                self.batches[key]["runs"][0].update(state="completed", completedAtMs=4)
            return self.batches[key]
        self.service.delegation.store.get_batch.side_effect = finish_while_spawning
        proof = self.proof()
        self.assertFalse(proof["settled"])
        self.assertIn("causal_membership_changed", proof["pending"])
        self.assertEqual(proof["proofRef"], "")
        self.assertIn("delegation:grandchild", self.proof()["pending"])

    def test_new_run_in_existing_batch_is_not_missed_by_membership_fence(self):
        self.child(state="completed")
        def append_after_snapshot(key, **kwargs):
            with sqlite_connection(self.service.db_path) as conn:
                conn.execute("INSERT INTO agent_subagent_runs VALUES(?,?)", ("late-run", key))
            return self.batches[key]
        self.service.delegation.store.get_batch.side_effect = append_after_snapshot
        self.assertIn("causal_membership_changed", self.proof()["pending"])

    def test_active_batch_with_terminal_runs_still_blocks_future_spawning(self):
        self.child(state="completed")
        with sqlite_connection(self.service.db_path) as conn:
            conn.execute("UPDATE agent_subagent_batches SET state='running'")
        self.assertIn("delegation_batch:batch:child", self.proof()["pending"])

    def test_job_created_during_owner_status_read_prevents_drain(self):
        self.job(state="completed")
        def append_job(session, identity):
            self.job("late-job")
            return {"job": self.jobs[identity]}
        self.service.background_jobs.status.side_effect = append_job
        proof = self.proof()
        self.assertFalse(proof["settled"])
        self.assertIn("causal_jobs_changed", proof["pending"])

    def test_descendants_are_read_only_after_parent_settlement(self):
        self.child()
        self.service.runtime.await_turn_settled.side_effect = TimeoutError("still running")
        self.assertEqual(self.proof()["pending"], ["parent_turn_not_settled"])
        self.service.delegation.store.get_batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
