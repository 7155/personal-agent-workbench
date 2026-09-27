"""Direct tasks keep task-bound verification without requiring a second partner.

Stores, task transitions, Pi admission and submissions are real. The finite
choice, prompt acknowledgement and terminal event remain test fixtures.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from rag_ime.agent_tools import ControlToolGateway
from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.types import canonical
from tests import test_jev_failure_lifecycle as failure
from tests import test_jev_host_application as host


class JevDirectCompletionTests(host.JevHostFixture):
    effects = failure.JevFailureLifecycleTests.effects
    active_effect = failure.JevFailureLifecycleTests.active_effect
    submit = failure.JevFailureLifecycleTests.submit
    finish = failure.JevFailureLifecycleTests.finish
    result = staticmethod(failure.JevFailureLifecycleTests.result)
    verdict = staticmethod(failure.JevFailureLifecycleTests.verdict)

    def setUp(self):
        super().setUp()
        self.gateway = ControlToolGateway(
            sessions=self.service.sessions, management=object(), core=object(),
            project=self.service.project, collaboration=self.service,
            background_jobs=self.service.background_jobs, delegation=self.service.delegation,
            work_documents=self.service.work_documents,
        )
        self.service.bind_tool_manifest_provider(self.gateway.runtime_manifests)
        self.app.driver.controller.decider = JevChoices(failure.choose_valid_progress)
        self.terminals = {}
        original = self.app.execution_terminal
        terminal = patch.object(self.app, "execution_terminal", side_effect=lambda effect, **kwargs:
            self.terminals.get(effect["effectId"]) or original(effect, **kwargs))
        terminal.start()
        self.addCleanup(terminal.stop)

    def remove_stored_verification_mode(self, created):
        # Simulate a Root persisted before verificationMode existed. New Roots
        # below use the public app.create request and its normalizer.
        with self.app.ledger.connection(write=True) as conn:
            row = conn.execute("SELECT policy_json FROM agent_jev_host_roots WHERE graph_id=?",
                               (created["graphId"],)).fetchone()
            policy = json.loads(row[0])
            policy.pop("verificationMode", None)
            conn.execute("UPDATE agent_jev_host_roots SET policy_json=? WHERE graph_id=?",
                         (canonical(policy), created["graphId"]))

    def only_controller_available(self):
        controller_session = self.room["participants"][0]["sessionId"]
        availability = {session["id"]: session["id"] == controller_session
                        for session in self.sessions}
        available = patch.object(self.service, "_room_target_idle",
                                 side_effect=lambda session_id, **_kwargs: availability[session_id])
        available.start()
        self.addCleanup(available.stop)
        return availability

    def submitted_direct(self, mode, *, summary="结果 direct", message="完成这项后端测试"):
        payload = {
            "clientMessageId": "direct-" + (mode or "legacy"),
            "message": message, "strategy": "direct",
            "modelRouting": "participant",
            "controllerParticipantId": self.room["participants"][0]["id"],
        }
        if mode is not None:
            payload["verificationMode"] = mode
        created = self.app.create(self.room["id"], payload)
        if mode is None:
            self.remove_stored_verification_mode(created)
        else:
            self.assertEqual(json.loads(self.app.lifecycle.policy(created["graphId"])["policy_json"])["verificationMode"], mode)
        worker = self.active_effect(created, "execute")
        self.submit(worker, "result_submit", {
            "resultSummary": summary, "evidenceRefs": ["test:direct"], "artifactRefs": [],
        })
        return created, worker

    def test_text_only_greeting_verifier_pack_does_not_require_worker_tool_receipts(self):
        created, worker = self.submitted_direct("auto", message="hi", summary="Hi! 👋")
        self.finish(worker)
        verifier = self.active_effect(created, "verify")
        prompt = verifier["request"]["taskBrief"]["objective"]
        instructions, encoded_pack = prompt.split("\nExecutionPack:\n", 1)
        pack = json.loads(encoded_pack)
        self.assertEqual(pack["task"]["objective"], "hi")
        self.assertEqual(pack["task"]["result"], "Hi! 👋")
        self.assertEqual(pack["workerToolEvidence"]["status"], "unavailable")
        self.assertIn("纯文字问候", instructions)
        self.assertIn("不因 workerToolEvidence 不可用而单独判为 unverified", instructions)
        self.assertIn("文件、命令或外部操作", instructions)
        self.assertIn("仍须核对对应的真实回执和文件版本", instructions)

    def test_auto_direct_uses_separate_bound_verify_turn_with_only_worker_available(self):
        self.only_controller_available()
        created, worker = self.submitted_direct("auto", summary="结果 direct；UNVERIFIED")
        task_id = worker["request"]["taskId"]
        self.assertEqual(self.snapshot(created).task(task_id).state, "review")
        self.assertEqual(self.effects(created, "verify"), [])
        self.assertEqual(self.app.projection(self.room["id"], created["graphId"])["final"], {})

        self.finish(worker)
        verifier = self.active_effect(created, "verify")
        self.assertEqual(verifier["request"]["ownerId"], worker["request"]["ownerId"])
        self.assertNotEqual(verifier["effectId"], worker["effectId"])
        self.assertEqual(verifier["request"]["purpose"], "verify")
        self.assertIn("同一伙伴", verifier["request"]["taskBrief"]["objective"])
        self.assertEqual(self.snapshot(created).task(task_id).state, "review")
        self.assertEqual(self.app.projection(self.room["id"], created["graphId"])["final"], {})

        self.submit(verifier, "verification_submit", self.verdict())
        self.finish(verifier)
        for _ in range(4):
            self.app.tick(limit=16)
        view = self.app.projection(self.room["id"], created["graphId"])
        self.assertEqual(self.snapshot(created).task(task_id).state, "done")
        self.assertEqual(view["final"]["status"], "completed")
        completed = [event for event in self.service.room_work.list_events(task_id)
                     if event["eventType"] == "completed"
                     and "verificationDispatchId" in event["payload"]]
        self.assertEqual(len(completed), 1)
        receipt = completed[0]["payload"]
        self.assertEqual(receipt["verificationDispatchId"], verifier["effectId"])
        self.assertEqual(receipt["verificationMode"], "same_participant")
        self.assertEqual(receipt["verifierParticipantId"], worker["request"]["ownerId"])
        self.assertTrue(receipt["verificationHash"])
        self.assertNotIn("independentVerificationDispatchId", receipt)
        self.assertNotIn("independentVerificationHash", receipt)
        self.assertEqual(receipt["work"]["review"]["reviewerParticipantId"],
                         worker["request"]["ownerId"])
        with self.app.ledger.connection() as conn:
            proof = conn.execute("SELECT dispatch_id FROM agent_jev_verifications WHERE task_id=?",
                                 (task_id,)).fetchone()
            self.assertEqual(proof[0], verifier["effectId"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_executor_claims WHERE graph_id=?",
                                          (created["graphId"],)).fetchone()[0], 0)

    def test_explicit_independent_direct_waits_for_other_partner(self):
        availability = self.only_controller_available()
        created, worker = self.submitted_direct("independent")
        self.finish(worker)
        self.app.tick(limit=16)
        self.assertEqual(self.effects(created, "verify"), [])
        self.assertEqual(self.snapshot(created).task(worker["request"]["taskId"]).state, "review")
        self.assertEqual(self.app.projection(self.room["id"], created["graphId"])["final"], {})

        other_session = self.room["participants"][1]["sessionId"]
        availability[other_session] = True
        with self.app.ledger.connection(write=True) as conn:
            self.app._enqueue(conn, created["graphId"], "test-partner-available", "executor_available")
        verifier = self.active_effect(created, "verify")
        self.assertNotEqual(verifier["request"]["ownerId"], worker["request"]["ownerId"])

    def test_auto_direct_prefers_other_partner_when_available(self):
        created, worker = self.submitted_direct("auto")
        self.finish(worker)
        verifier = self.active_effect(created, "verify")
        self.assertNotEqual(verifier["request"]["ownerId"], worker["request"]["ownerId"])

    def test_missing_mode_preserves_independent_verification_for_old_roots(self):
        self.only_controller_available()
        created, worker = self.submitted_direct(None)
        self.assertEqual(self.app.projection(self.room["id"], created["graphId"])["policy"]["verificationMode"], "independent")
        self.finish(worker)
        self.app.tick(limit=16)
        self.assertEqual(self.effects(created, "verify"), [])
        self.assertEqual(self.snapshot(created).task(worker["request"]["taskId"]).state, "review")

    def test_auto_plan_still_uses_non_worker_verifier(self):
        created = self.app.create(self.room["id"], {
            "clientMessageId": "single-step-plan", "message": "先规划再执行一项责任",
            "strategy": "plan", "modelRouting": "participant", "verificationMode": "auto",
        })
        planner = self.active_effect(created, "plan")
        self.submit(planner, "plan_submit", {
            "requirementsRevision": 1, "topologyRevision": 0,
            "tasks": [{
                "key": "a", "objective": "执行一项计划责任", "expectedOutput": "可核对的成果",
                "acceptanceCriteria": ["实际检查成果"], "dependsOn": [],
                "ownerParticipantId": self.room["participants"][0]["id"],
            }],
        })
        self.finish(planner)
        worker = self.active_effect(created, "execute")
        self.submit(worker, "result_submit", self.result("planned"))
        self.finish(worker)
        verifier = self.active_effect(created, "verify")
        self.assertNotEqual(verifier["request"]["ownerId"], worker["request"]["ownerId"])


if __name__ == "__main__":
    import unittest
    unittest.main()
