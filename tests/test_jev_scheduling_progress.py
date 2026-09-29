"""Finite scheduling regressions; no Provider calls or live Pi execution.

Candidate tests use immutable owner facts. Observation tests read actual stores
through the host fixture; inserted approval/verdict rows are explicit fixtures.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import unittest
from unittest.mock import Mock, patch

from rag_ime.jev_tasks.candidates import Verification, build_candidates
from rag_ime.jev_tasks.decider import ABSTAIN, JevChoices
from rag_ime.jev_tasks.ledger import Snapshot
from rag_ime.jev_tasks.types import Edge, ExecutionFact, Task, canonical, digest
from tests.test_jev_host_application import JevHostFixture


class SchedulingCandidateTests(unittest.TestCase):
    def setUp(self):
        def task(identity, state="active", parent=""):
            return Task(identity, "root", "room", state, 0, "owner-" + identity,
                "coordinator", "assignment-" + identity, "dispatch-" + identity if state == "review" else "",
                identity + "：整份方案经用户一次确认后才执行", "可验收结果", ("有证据",), parent)

        tasks = (task("root-task"), task("A", "review", "root-task"),
                 task("B", "review", "root-task"), task("C", parent="root-task"))
        self.snapshot = Snapshot("graph", "room", "root", "root-task", "controller", "coordinator",
            "coordinator-session", "snapshot", 1, tasks,
            (Edge("A", "C"), Edge("B", "C")), "[]")
        self.executions = {task.id: ExecutionFact(task.id, "drained" if task.accepted_turn_id else "idle",
            task.accepted_turn_id, "session-" + task.id, task.revision, task.owner_id,
            task.assignment_key, task.accepted_turn_id, "owner-proof:" + task.id, True) for task in tasks}
        self.verifications = {task.id: Verification(task.id, digest(asdict(task)),
            "unverified" if task.id == "A" else "passed", "unverified" if task.id == "A" else "satisfied",
            "需要补齐执行证据" if task.id == "A" else "独立核验通过", ("tool-result:" + task.id,))
            for task in tasks if task.state == "review"}

    def candidates(self):
        return build_candidates(self.snapshot, event_id="event", executions=self.executions,
            executors=(), eligible_pairs=frozenset(), manifests={}, verifications=self.verifications)

    def test_current_bound_verdicts_progress_without_an_unwakeable_wait(self):
        candidates = self.candidates()
        self.assertEqual({(c.operation, c.task_id) for c in candidates.actions}, {("accept", "B")})
        self.assertTrue(any("additional verification evidence" in item for item in candidates.missing))

    def test_observed_failure_still_requires_repair_when_other_axis_is_unknown(self):
        for operability, requirement in (("failed", "unverified"), ("unverified", "not_satisfied")):
            with self.subTest(operability=operability, requirement=requirement):
                self.verifications["A"] = replace(self.verifications["A"], operability=operability, requirement=requirement)
                self.assertEqual({(c.operation, c.task_id) for c in self.candidates().actions},
                                 {("return", "A"), ("accept", "B")})

    def test_real_running_work_keeps_wait_available_alongside_legal_progress(self):
        self.executions["A"] = replace(self.executions["A"], status="running")
        self.assertEqual({(c.operation, c.task_id) for c in self.candidates().actions}, {("accept", "B"), ("wait", "")})

    def test_unknown_admission_waits_for_reconciliation_and_never_redispatches(self):
        self.snapshot = replace(self.snapshot, effects_json=canonical([{
            "effectId": "unknown-command", "operation": "dispatch", "taskId": "A", "state": "unknown"}]))
        candidates = self.candidates()
        self.assertEqual({(c.operation, c.task_id) for c in candidates.actions}, {("accept", "B"), ("wait", "")})
        self.assertTrue(any("reconciliation" in missing for missing in candidates.missing))

    def test_missing_or_stale_verdicts_do_not_manufacture_acceptance(self):
        self.verifications.pop("A")
        self.verifications["B"] = replace(self.verifications["B"], task_hash="stale")
        candidates = self.candidates()
        self.assertEqual(candidates.actions, ())
        self.assertEqual(len(candidates.missing), 2)

    def test_scheduler_descriptions_do_not_reinterpret_verifier_prose(self):
        candidates = self.candidates()
        for candidate in candidates.actions:
            proof = self.verifications[candidate.task_id]
            self.assertNotIn(proof.reason, candidate.description)
            # The original evidence remains intact at the guarded owner seam.
            self.assertEqual(candidate.arguments()["reason"], proof.reason)
            self.assertEqual(candidate.arguments()["evidenceRefs"], list(proof.evidence_refs))

    def test_dependency_facts_keep_current_acceptance_separate_from_old_prose(self):
        from rag_ime.jev_tasks.application import JevRoomApplication
        snapshot = replace(self.snapshot, edges=(*self.snapshot.edges, Edge("C", "A", "context")), tasks=tuple(
            replace(task, state="done", result="等待独立复核") if task.id == "A" else task
            for task in self.snapshot.tasks))
        facts = JevRoomApplication.scheduling_dependency_facts(snapshot)
        self.assertEqual(set(facts), {"C"})
        self.assertEqual([row["taskId"] for row in facts["C"]], ["A", "B"])
        self.assertTrue(facts["C"][0]["accepted"])
        self.assertTrue(facts["C"][0]["resultAvailable"])
        self.assertFalse(facts["C"][1]["accepted"])
        self.assertNotIn("等待独立复核", canonical(facts))
        self.assertEqual(facts["C"][0]["taskRevision"], snapshot.task("A").revision)

    def test_removing_wait_does_not_remove_explicit_abstention(self):
        captured = []
        def abstain(state, questions):
            captured.append(json.loads(state))
            criteria = questions["decision"]["criteria"]
            return {"model": "fixture", "answers": {"decision": {"type": "choice", "choice": ABSTAIN,
                "confidence": 1.0, "probabilities": {key: float(key == ABSTAIN) for key in criteria}}}}
        action, answer = JevChoices(abstain).choose_action({}, self.candidates().actions)
        self.assertIsNone(action)
        self.assertEqual(answer.choice, ABSTAIN)
        self.assertEqual({a["operation"] for a in captured[0]["actions"]}, {"accept"})


class SchedulingObservationTests(JevHostFixture):
    def test_auto_route_uses_the_same_context_envelope_as_task_scheduling(self):
        from tests.test_jev_host_application import choose_execution

        captured = []
        def choose(state, questions):
            captured.append(json.loads(state))
            return choose_execution(state, questions)
        self.app.driver.controller.decider = JevChoices(choose)
        created = self.app.create(self.room["id"], {
            "clientMessageId": "auto-envelope", "message": "完成单项测试", "strategy": "auto",
            "modelRouting": "participant", "controllerParticipantId": self.room["participants"][0]["id"],
        })
        self.app.tick(limit=1)
        root = self.snapshot(created).task(created["workItemId"])
        self.assertEqual(captured[0]["work"]["context"], {
            "objective": root.objective, "acceptance": list(root.acceptance),
            "attachmentCount": 0,
        })
        self.assertEqual(self.app.lifecycle.policy(created["graphId"])["phase"], "execute")
        self.prompt.assert_not_called()

    def approve_fixture(self, created, *, status="approved", revision=1):
        with self.app.ledger.connection(write=True) as conn:
            conn.execute("INSERT INTO agent_jev_plan_approvals VALUES(?,?,?,?,?,?,?,?)", (
                created["graphId"], status, revision, "fixture-plan-hash", "{}", "fixture-planner", "[]", 1,
            ))

    def test_legacy_root_without_approval_row_keeps_request_authority(self):
        created = self.create()
        state = self.app.observe(self.snapshot(created), None).decision_state
        self.assertEqual(state["planApproval"]["status"], "not_required")
        self.assertTrue(state["planApproval"]["executionAuthorized"])
        self.prompt.assert_not_called()

    def test_approved_current_plan_is_an_explicit_scheduling_fact(self):
        created = self.create()
        self.approve_fixture(created)
        state = self.app.observe(self.snapshot(created), None).decision_state
        self.assertEqual(state["planApproval"], {"status": "approved", "planHash": "fixture-plan-hash",
            "requirementsRevision": 1, "currentRequirementsRevision": 1, "executionAuthorized": True})
        self.assertEqual(state["schedulingFacts"]["runningTaskIds"], [])
        self.prompt.assert_not_called()

    def test_pending_or_stale_approval_is_not_reported_as_authorized(self):
        created = self.create()
        self.approve_fixture(created, status="awaiting_approval")
        self.assertFalse(self.app.observe(self.snapshot(created), None).decision_state["planApproval"]["executionAuthorized"])
        with self.app.ledger.connection(write=True) as conn:
            conn.execute("UPDATE agent_jev_plan_approvals SET status='approved',requirements_revision=0")
        self.assertFalse(self.app.observe(self.snapshot(created), None).decision_state["planApproval"]["executionAuthorized"])

    def test_decision_context_contains_only_current_hash_bound_verification(self):
        created = self.create()
        self.app.tick()  # The fixture transport admits a real persisted effect.
        task = self.snapshot(created).tasks[0]
        worker = self.app.effects.get(task.accepted_turn_id)
        self.app.submit_execution(worker, {"resultSummary": "fixture output", "artifactRefs": [],
                                          "evidenceRefs": ["tool-result:fixture"]})
        self.service.runtime.release_prompt_admission(worker["request"]["sessionId"],
                                                     client_message_id=worker["effectId"])
        with patch.object(self.app, "execution_terminal", return_value={
                "eventId": "fixture-worker-drained", "eventType": "turn_completed", "status": "completed"}):
            self.app.reconcile_graph(self.app.binding_by_graph(created["graphId"]))
        snapshot = self.snapshot(created)
        task = snapshot.task(task.id)
        # Project a verdict bound to an actual prepared verifier subject, not
        # the worker's execution effect (which carries no verifier identity).
        verifier_id = self.app.lifecycle.prepare(snapshot, task, "verify", self.room["participants"][1])
        self.assertIsNotNone(verifier_id)
        verdict = {"operabilityVerdict": "unverified", "requirementVerdict": "unverified",
            "reason": "需要执行证据", "evidenceRefs": ["tool-result:fixture"]}
        with self.app.ledger.connection(write=True) as conn:
            conn.execute("INSERT INTO agent_jev_verifications VALUES(?,?,?,?,?)", (
                created["graphId"], task.id, digest(asdict(task)), verifier_id, canonical(verdict)))
        state = self.app.observe(self.snapshot(created), None).decision_state
        self.assertEqual(state["verificationFacts"][task.id]["operabilityVerdict"], "unverified")
        self.assertEqual(state["verificationFacts"][task.id]["taskHash"], digest(asdict(task)))
        self.assertEqual(state["verificationFacts"][task.id]["source"], "current_task_bound_verification")
        self.assertNotIn("reason", state["verificationFacts"][task.id])
        self.assertNotIn("evidenceRefs", state["verificationFacts"][task.id])
        with self.app.ledger.connection() as conn:
            stored = json.loads(conn.execute("SELECT result_json FROM agent_jev_verifications WHERE task_id=?", (task.id,)).fetchone()[0])
        self.assertEqual(stored, verdict)
        with self.app.ledger.connection(write=True) as conn:
            conn.execute("UPDATE agent_jev_verifications SET task_hash='stale'")
        self.assertEqual(self.app.observe(self.snapshot(created), None).decision_state["verificationFacts"], {})

    def test_no_legal_action_records_noop_without_a_model_call(self):
        from rag_ime.jev_tasks.room_driver import OwnerEvent

        created = self.create()
        self.app.driver.controller.decider = Mock(side_effect=AssertionError("model must not run"))
        with self.app.ledger.connection(write=True) as conn:
            conn.execute("UPDATE agent_room_work_items SET state='review' WHERE id=?", (created["workItemId"],))
        event = OwnerEvent("fixture-no-proof", "work_submitted", created["graphId"], created["graphId"],
            self.room["id"], created["rootId"])
        result = self.app.driver.handle(event)
        self.assertEqual(result["status"], "waiting")
        self.assertTrue(result["missing"])
        self.app.driver.controller.decider.choose_action.assert_not_called()
        self.prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
