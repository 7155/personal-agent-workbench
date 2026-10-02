"""Failure recovery through real stores, admission and the typed Tool gateway.

Only Jev's finite choice transport, Pi prompt acknowledgement and exact terminal
proof are doubles. No task transition, graph guard, claim, Tool submission,
revision, auxiliary retry, legacy API guard or final publication is mocked.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_tools import ControlToolGateway
from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.types import GraphConflict
from tests import test_jev_host_application as host


def choose_valid_progress(state, questions):
    """Deterministically pick a valid offered action, never invent a transition."""
    actions = json.loads(state)["actions"]
    if json.loads(state).get("work", {}).get("context", {}).get("verificationRoute"):
        actions = [action for action in actions if action["operation"] == "verify"]
    order = {name: rank for rank, name in enumerate(("accept", "return", "claim_dispatch", "retry", "reassign", "direct", "plan", "wait"))}
    chosen = min(actions, key=lambda action: (order.get(action["operation"], 99), action["id"]))["id"]
    criteria = questions["decision"]["criteria"]
    return {"model": "test-only-finite-choice", "answers": {"decision": {
        "type": "choice", "choice": chosen, "confidence": 1.0,
        "probabilities": {key: 1.0 if key == chosen else 0.0 for key in criteria},
    }}}


class JevFailureLifecycleTests(host.JevHostFixture):
    def setUp(self):
        super().setUp()
        self.gateway = ControlToolGateway(
            sessions=self.service.sessions, management=object(), core=object(),
            project=self.service.project, collaboration=self.service,
            background_jobs=self.service.background_jobs, delegation=self.service.delegation,
            work_documents=self.service.work_documents,
        )
        self.service.bind_tool_manifest_provider(self.gateway.runtime_manifests)
        self.app.driver.controller.decider = JevChoices(choose_valid_progress)
        self.terminals = {}
        original = self.app.execution_terminal
        terminal = patch.object(self.app, "execution_terminal", side_effect=lambda effect, **kwargs:
            self.terminals.get(effect["effectId"]) or original(effect, **kwargs))
        terminal.start()
        self.addCleanup(terminal.stop)

    def effects(self, created, purpose=None):
        return [effect for effect in self.app.projection(self.room["id"], created["graphId"])["effects"]
                if effect["operation"] == "dispatch"
                and (purpose is None or effect["request"].get("purpose", "execute") == purpose)]

    def active_effect(self, created, purpose):
        for _ in range(6):
            current = [effect for effect in self.effects(created, purpose)
                       if effect["state"] == "accepted" and effect["effectId"] not in self.terminals]
            if current:
                self.assertEqual(len(current), 1, self.app.projection(self.room["id"], created["graphId"]))
                return current[0]
            self.app.tick(limit=16)
        self.fail("No live " + purpose + " execution: " + json.dumps(self.app.projection(self.room["id"], created["graphId"]), ensure_ascii=False))

    def submit(self, effect, operation, proposal):
        self._submission_sequence = getattr(self, "_submission_sequence", 0) + 1
        return self.gateway.execute({"schemaVersion": "rag-ime.agent-tool-call.v1",
            "sessionId": effect["request"]["sessionId"], "tool": "room_partner",
            "toolCallId": f"typed:{effect['effectId']}:{self._submission_sequence}",
            "args": {"op": operation, "proposal": proposal}})["result"]

    def finish(self, effect, *, failed=False):
        request = effect["request"]
        self.terminals[effect["effectId"]] = {
            "eventId": "terminal:" + effect["effectId"],
            "eventType": "turn_failed" if failed else "turn_completed",
            "status": "failed" if failed else "completed",
        }
        self.service.runtime.release_prompt_admission(request["sessionId"], client_message_id=request["dispatchId"])
        self.service.room_turns.finish(request["sessionId"], effect["receipt"]["turnId"], request["rootId"])
        self.app.reconcile_graph(self.app.binding_by_graph(request["graphId"]))

    def planned(self, *, dependent=False, shared_target=False):
        if shared_target:
            for session in self.sessions:
                self.service.sessions.set_runtime_policy(session["id"], mode="coordinator",
                    tool_profile_version="control-center-v1", execution_mode="workspace_managed",
                    workspace_roots=[self.tmp.name], allowed_tools=None)
        created = self.app.create(self.room["id"], {"clientMessageId": "failure-plan", "message": "完成两个有独立验收条件的责任",
            "strategy": "plan", "modelRouting": "participant"})
        planner = self.active_effect(created, "plan")
        tasks = [{"key": key, "objective": "责任 " + key, "expectedOutput": "成果 " + key,
                  "acceptanceCriteria": ["通过实际核验 " + key], "dependsOn": ["a"] if dependent and key == "b" else [],
                  "ownerParticipantId": self.room["participants"][index]["id"],
                  "writeTargets": [str(Path(self.tmp.name) / "shared.txt")] if shared_target else []}
                 for index, key in enumerate(("a", "b"))]
        self.submit(planner, "plan_submit", {"requirementsRevision": 1, "topologyRevision": 0, "tasks": tasks})
        self.finish(planner)
        return created

    @staticmethod
    def result(label):
        return {"resultSummary": "结果 " + label, "artifactRefs": [], "evidenceRefs": ["test:" + label]}

    @staticmethod
    def verdict(*, passed=True):
        return {"operabilityVerdict": "passed" if passed else "failed",
                "requirementVerdict": "satisfied" if passed else "not_satisfied",
                "reason": "实际检查通过" if passed else "真实检查失败，需修正当前成果",
                "evidenceRefs": ["test:verification-passed" if passed else "test:verification-failed"]}

    def assert_failed_final(self, created):
        for _ in range(4):
            self.app.tick(limit=16)
        self.app.recover()
        self.app.tick(limit=16)
        view = self.app.projection(self.room["id"], created["graphId"])
        self.assertEqual(view["final"].get("status"), "failed", view)
        self.assertTrue(view["final"].get("content"))
        events = [event for event in self.service.rooms.list_events(self.room["id"])
                  if event["turnId"] == created["rootId"]]
        self.assertEqual(sum(event["eventType"] == "turn_failed" for event in events), 1)
        self.assertEqual(sum(event["eventType"] == "turn_completed" for event in events), 0)
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_executor_claims WHERE graph_id=?",
                                          (created["graphId"],)).fetchone()[0], 0)
        return view

    def test_failed_verification_revises_same_task_and_rejects_changed_old_callback(self):
        created = self.create()
        worker = self.active_effect(created, "execute")
        initial = self.snapshot(created).task(worker["request"]["taskId"])
        self.submit(worker, "result_submit", self.result("first"))
        self.finish(worker)
        verifier = self.active_effect(created, "verify")
        self.assertNotEqual(verifier["request"]["ownerId"], worker["request"]["ownerId"])
        verdict = {**self.verdict(passed=False), "reason": "指定交付文件 docs/report.md 实际不存在，需要写入后重新检查。"}
        self.submit(verifier, "verification_submit", verdict)
        self.finish(verifier)
        replacement = self.active_effect(created, "execute")
        current = self.snapshot(created).task(initial.id)
        self.assertEqual(current.revision, initial.revision + 1)
        self.assertEqual(len(self.snapshot(created).tasks), 1)
        self.assertEqual(replacement["request"]["taskId"], initial.id)
        self.assertNotEqual(replacement["effectId"], worker["effectId"])
        replacement_prompt = next(payload for _, payload in self.calls
                                  if payload['clientMessageId'] == replacement['effectId'])
        self.assertIn(verdict['reason'], replacement_prompt['_transientContext'])
        self.assertIn(verdict['reason'], replacement_prompt['message'])
        self.assertIn('上一版本未通过验收', replacement_prompt['message'])
        self.assertIn(f'第 {current.revision} 次修订', replacement_prompt['message'])
        first_prompt = next(payload for _, payload in self.calls if payload['clientMessageId'] == worker['effectId'])
        self.assertNotIn('上一版本未通过验收', first_prompt['message'])
        before = self.snapshot(created).fingerprint
        # This is the exact old dispatch callback boundary, not a new Session
        # Tool call pretending to belong to a previous turn.
        with self.assertRaises(GraphConflict):
            self.app.submit_execution(worker, self.result("late-old-result"))
        old_record = self.service.room_partner_dispatches.get(worker["effectId"])
        self.app.settle_dispatch(old_record, phase="failed", result="late old failure", completion_source="session_terminal")
        self.assertEqual(self.snapshot(created).fingerprint, before)
        self.assertEqual(self.snapshot(created).task(initial.id).state, "active")

    def test_independent_tasks_hold_two_live_session_dispatches(self):
        created = self.planned()
        for _ in range(4):
            self.app.tick(limit=16)
        running = [effect for effect in self.effects(created, "execute") if effect["effectId"] not in self.terminals]
        self.assertEqual(len(running), 2, self.app.projection(self.room["id"], created["graphId"]))
        self.assertEqual(len({effect["request"]["sessionId"] for effect in running}), 2)
        for effect in running:
            self.assertEqual(effect["state"], "accepted")
            self.assertEqual(self.service.room_turns.active_turn(effect["request"]["sessionId"]),
                             (created["rootId"], effect["effectId"]))
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_executor_claims WHERE graph_id=?",
                                          (created["graphId"],)).fetchone()[0], 2)

    def test_parallel_worker_activity_during_result_submission_does_not_invalidate_task(self):
        created = self.planned()
        for _ in range(4):
            self.app.tick(limit=16)
        workers = self.effects(created, "execute")
        self.assertEqual(len(workers), 2)
        first, sibling = workers
        self.assertNotEqual(first["request"]["sessionId"], sibling["request"]["sessionId"])
        before = self.snapshot(created)

        def parallel_activity(effect, **kwargs):
            # The real Room event owner advances its cursor between the result
            # snapshot and the WorkItem transaction, as a live sibling does.
            self.service.rooms.append_event(
                room_id=self.room["id"], event_type="participant_activity",
                participant_id=sibling["request"]["ownerId"],
                source_session_id=sibling["request"]["sessionId"],
                turn_id=created["rootId"], payload={"kind": "tool_started"})
            return None

        with patch.object(self.app, "execution_terminal", side_effect=parallel_activity):
            result = self.submit(first, "result_submit", self.result("parallel-first"))
        self.assertEqual(result["status"], "applied")
        current = self.snapshot(created)
        self.assertEqual(current.task(first["request"]["taskId"]).state, "review")
        self.assertEqual(current.task(sibling["request"]["taskId"]),
                         before.task(sibling["request"]["taskId"]))
        self.assertEqual(self.service.room_turns.active_turn(sibling["request"]["sessionId"]),
                         (created["rootId"], sibling["effectId"]))
        self.assertTrue(self.submit(first, "result_submit", self.result("parallel-first"))["replayed"])
        self.assertEqual(self.submit(sibling, "result_submit", self.result("parallel-second"))["status"], "applied")

    def test_same_write_target_cannot_have_two_live_executors(self):
        created = self.planned(shared_target=True)
        for _ in range(4):
            self.app.tick(limit=16)
        workers = self.effects(created, "execute")
        self.assertEqual(len(workers), 1, self.app.projection(self.room["id"], created["graphId"]))
        self.assertEqual(workers[0]["state"], "accepted")
        undispatched = [task for task in self.snapshot(created).tasks
                        if task.id != created["workItemId"] and not task.accepted_turn_id]
        self.assertEqual(len(undispatched), 1)
        self.assertNotEqual(undispatched[0].owner_id, workers[0]["request"]["ownerId"])
        self.assertEqual(len(self.calls), 2)  # One planner and one overlapping worker.

    def test_missing_plan_output_stops_at_three_attempts_with_failed_final(self):
        created = self.app.create(self.room["id"], {"clientMessageId": "no-plan", "message": "先规划再执行",
            "strategy": "plan", "modelRouting": "participant"})
        for attempt in range(3):
            planner = self.active_effect(created, "plan")
            self.assertEqual(len(self.effects(created, "plan")), attempt + 1)
            self.finish(planner)
        self.assert_failed_final(created)
        self.assertEqual(len(self.effects(created, "plan")), 3)
        self.assertEqual(self.effects(created, "execute"), [])

    def test_missing_verification_output_stops_at_three_attempts_with_failed_final(self):
        created = self.create()
        worker = self.active_effect(created, "execute")
        self.submit(worker, "result_submit", self.result("unverified"))
        self.finish(worker)
        for attempt in range(3):
            verifier = self.active_effect(created, "verify")
            self.assertEqual(len(self.effects(created, "verify")), attempt + 1)
            self.finish(verifier)
        self.assert_failed_final(created)
        self.assertEqual(len(self.effects(created, "verify")), 3)
        self.assertEqual(len(self.effects(created, "execute")), 1)

    def test_missing_synthesis_output_fails_without_losing_accepted_children(self):
        created = self.planned(dependent=True)
        for label in ("a", "b"):
            worker = self.active_effect(created, "execute")
            self.submit(worker, "result_submit", self.result(label))
            self.finish(worker)
            verifier = self.active_effect(created, "verify")
            self.submit(verifier, "verification_submit", self.verdict())
            self.finish(verifier)
        for attempt in range(3):
            synthesizer = self.active_effect(created, "synthesize")
            self.assertEqual(len(self.effects(created, "synthesize")), attempt + 1)
            self.finish(synthesizer)
        self.assert_failed_final(created)
        self.assertEqual(len(self.effects(created, "synthesize")), 3)
        children = [task for task in self.snapshot(created).tasks if task.id != created["workItemId"]]
        self.assertEqual(len(children), 2)
        self.assertTrue(all(task.state == "done" and task.evidence for task in children))

    def test_long_final_report_settles_and_publishes_without_overflowing_work_summary(self):
        created = self.planned(dependent=True)
        for label in ("a", "b"):
            worker = self.active_effect(created, "execute")
            self.submit(worker, "result_submit", self.result(label))
            self.finish(worker)
            verifier = self.active_effect(created, "verify")
            self.submit(verifier, "verification_submit", self.verdict())
            self.finish(verifier)
        synthesizer = self.active_effect(created, "synthesize")
        narrative = "真实验证与成果说明。" * 500 + "报告末尾保留"
        self.submit(synthesizer, "final_submit", {"content": narrative, "evidenceRefs": ["test:full-report"]})
        self.finish(synthesizer)
        for _ in range(3):
            self.app.tick(limit=16)
        view = self.app.projection(self.room["id"], created["graphId"])
        self.assertEqual(view["phase"], "final")
        self.assertEqual(view["final"]["status"], "completed")
        self.assertIn(narrative, view["final"]["content"])
        self.assertLessEqual(len(self.service.room_work.get(created["workItemId"])["resultSummary"]), 4000)
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_executor_claims WHERE graph_id=?", (created["graphId"],)).fetchone()[0], 0)

    def test_failed_dependency_is_never_dispatched(self):
        created = self.planned(dependent=True)
        worker = self.active_effect(created, "execute")
        upstream = worker["request"]["taskId"]
        downstream = next(edge.dependent for edge in self.snapshot(created).edges if edge.prerequisite == upstream)
        self.submit(worker, "result_submit", self.result("upstream"))
        self.finish(worker)
        for _ in range(3):
            self.finish(self.active_effect(created, "verify"))
        self.assertEqual(self.snapshot(created).task(upstream).state, "failed")
        for _ in range(4):
            self.app.tick(limit=16)
        self.app.recover()
        self.app.tick(limit=16)
        self.assertFalse(any(effect["request"]["taskId"] == downstream for effect in self.effects(created, "execute")))
        self.assertEqual(self.snapshot(created).task(downstream).accepted_turn_id, "")

    def test_legacy_mutations_cannot_bypass_jev_owner(self):
        created = self.create()
        worker = self.active_effect(created, "execute")
        before = self.snapshot(created).fingerprint
        sid = worker["request"]["sessionId"]
        for method in (self.service.assign_room_work, self.service.submit_room_work, self.service.accept_room_work,
                       self.service.return_room_work, self.service.block_room_work, self.service.escalate_room_work):
            with self.subTest(method=method.__name__), self.assertRaises(GraphConflict):
                method(sid, {"workId": created["workItemId"]})
        with self.assertRaises(GraphConflict):
            self.service.create_room_work_item(self.room["id"], {"parentWorkId": created["workItemId"]})
        with self.assertRaises(GraphConflict):
            self.service.reassign_room_work_item(self.room["id"], created["workItemId"], {})
        for operation in ("delegate", "delegate_batch", "retry", "accept", "return", "remove_participant"):
            with self.subTest(operation=operation), self.assertRaises((GraphConflict, ValueError)):
                self.gateway.execute({"schemaVersion": "rag-ime.agent-tool-call.v1", "sessionId": sid,
                    "tool": "room_partner", "toolCallId": "legacy:" + operation, "args": {"op": operation}})
        self.assertEqual(self.snapshot(created).fingerprint, before)
        self.assertEqual(len(self.effects(created)), 1)

    def test_legacy_resume_control_rejects_jev_before_legacy_dispatch(self):
        created = self.create()
        with self.assertRaises(GraphConflict):
            self.service.resume_room_work_item(self.room["id"], created["workItemId"], {
                "actorParticipantId": self.room["participants"][0]["id"], "clientActionId": "legacy-resume"})
        self.assertEqual(self.effects(created), [])
