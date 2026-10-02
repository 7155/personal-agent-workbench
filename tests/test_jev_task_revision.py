"""Selective graph revision through real WorkStore/ledger and exact Pi doubles."""
from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import patch

from rag_ime.agent_tools import ControlToolGateway
from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.candidates import build_candidates
from rag_ime.jev_tasks.types import ExecutionFact, GraphConflict, canonical, digest
from tests.test_jev_host_application import JevHostFixture
from tests import test_jev_lifecycle as lifecycle_fixtures


class JevTaskRevisionTests(JevHostFixture):
    effects = lifecycle_fixtures.JevLifecycleTests.effects
    submit = lifecycle_fixtures.JevLifecycleTests.submit
    finish = lifecycle_fixtures.JevLifecycleTests.finish

    def setUp(self):
        super().setUp()
        self.gateway = ControlToolGateway(
            sessions=self.service.sessions, management=object(), core=object(),
            project=self.service.project, collaboration=self.service,
            background_jobs=self.service.background_jobs,
            delegation=self.service.delegation,
            work_documents=self.service.work_documents,
        )
        self.service.bind_tool_manifest_provider(self.gateway.runtime_manifests)
        self.terminals = {}
        original = self.app.execution_terminal
        p = patch.object(self.app, "execution_terminal", side_effect=lambda effect, **kwargs:
                         self.terminals.get(effect["effectId"]) or original(effect, **kwargs))
        p.start(); self.addCleanup(p.stop)
        p = patch.object(self.service.runtime, "is_turn_active", return_value=True)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(self.service.runtime, "abort_turn", side_effect=self.cancel_receipt)
        self.abort = p.start(); self.addCleanup(p.stop)
        self.ids = {}
        self.app.driver.controller.decider = JevChoices(self.choose)

    @staticmethod
    def cancel_receipt(session_id, turn_id, *, client_message_id, cancel_id, lookup_only=False):
        return {"schemaVersion": "rag-ime.pi-exact-turn-cancel.v1",
                "sessionId": session_id, "turnId": turn_id,
                "clientMessageId": client_message_id, "cancelId": cancel_id,
                "receiptId": "cancel:" + cancel_id, "state": "accepted"}

    def choose(self, state, questions):
        actions = json.loads(state)["actions"]
        choices = [action for action in actions if action["operation"] != "wait"]
        if not choices:
            selected = next(action for action in actions if action["operation"] == "wait")
        else:
            selected = next((action for action in choices
                             if action["operation"] == "claim_dispatch"
                             and action["taskId"] == self.ids.get("b")), choices[0])
        keys = questions["decision"]["criteria"]
        return {"model": "test-choice", "answers": {"decision": {
            "type": "choice", "choice": selected["id"],
            "probabilities": {key: 1.0 if key == selected["id"] else 0.0 for key in keys},
            "confidence": 1.0}}}

    def planned(self):
        created = self.app.create(self.room["id"], {
            "clientMessageId": "revision-plan", "message": "完成 A 与独立 B，随后完成 C",
            "strategy": "plan", "modelRouting": "participant", "executionApproval": False,
            "verificationMode": "independent",
        })
        self.app.tick()
        planner = self.effects(created, "plan")[0]
        self.submit(planner, "plan_submit", {
            "requirementsRevision": 1, "topologyRevision": 0,
            "tasks": [
                {"key": "a", "objective": "A 原要求", "expectedOutput": "A 原成果",
                 "acceptanceCriteria": ["A 原验收"], "dependsOn": []},
                {"key": "b", "objective": "B 独立要求", "expectedOutput": "B 成果",
                 "acceptanceCriteria": ["B 验收"], "dependsOn": []},
                {"key": "c", "objective": "C 依赖 A", "expectedOutput": "C 成果",
                 "acceptanceCriteria": ["C 验收"], "dependsOn": ["a"]},
            ],
        })
        with self.app.ledger.connection() as conn:
            self.ids = {json.loads(row["specification_json"])["key"]: row["task_id"]
                        for row in conn.execute(
                            "SELECT task_id,specification_json FROM agent_jev_task_requirements WHERE graph_id=?",
                            (created["graphId"],))}
        self.finish(planner)
        return created

    def next_effect(self, created, purpose, task_id, *, limit=32):
        for _ in range(limit):
            found = [effect for effect in self.effects(created, purpose)
                     if effect["request"]["taskId"] == task_id
                     and effect["state"] == "accepted"
                     and effect["effectId"] not in self.terminals]
            if found:
                return found[-1]
            self.app.tick(limit=1)
        self.fail("expected accepted " + purpose + " for " + task_id)

    def complete_task(self, created, task_id, summary):
        worker = self.next_effect(created, "execute", task_id)
        self.submit(worker, "result_submit", {
            "resultSummary": summary, "evidenceRefs": ["fixture:" + task_id],
            "artifactRefs": [],
        })
        self.finish(worker)
        verifier = self.next_effect(created, "verify", task_id)
        self.submit(verifier, "verification_submit", {
            "operabilityVerdict": "passed", "requirementVerdict": "satisfied",
            "reason": "已检查固定结果", "evidenceRefs": ["verify:" + task_id],
        })
        self.finish(verifier)
        for _ in range(32):
            if self.app.ledger.snapshot(created["graphId"], created["graphId"]).task(task_id).state == "done":
                return
            self.app.tick(limit=1)
        self.fail("task was not accepted: " + task_id)

    def revision_payload(self, created):
        options = self.app.command(self.room["id"], {
            "action": "revision_options", "graphId": created["graphId"],
            "taskId": self.ids["a"],
        })
        return {"action": "revise_task", "graphId": created["graphId"],
                "rootId": created["rootId"], "taskId": self.ids["a"],
                "taskHash": options["taskHash"],
                "expectedTopologyRevision": options["expectedTopologyRevision"],
                "expectedRequirementsRevision": options["expectedRequirementsRevision"],
                "clientMessageId": "revision-one", "objective": "A 新要求",
                "expectedOutput": "A 新成果", "acceptanceCriteria": ["A 新验收"],
                "reason": "用户改变 A 的要求", "rootObjective": "新目标：A、B、C 均需验收"}

    def drain_old(self, created, effect, *, status="aborted"):
        dispatch = effect["effectId"]
        self.terminals[dispatch] = {
            "eventId": "physical-drain:" + dispatch,
            "eventType": "turn_completed", "status": status,
        }
        with self.app.ledger.connection(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO agent_jev_execution_drains VALUES(?,?,?)",
                         (dispatch, canonical({"proofRef": "physical-drain:" + dispatch,
                                              "terminal": status}), self.app.ledger.clock_ms()))
        request = effect["request"]
        self.service.runtime.release_prompt_admission(
            request["sessionId"], client_message_id=dispatch)
        self.service.room_turns.finish(
            request["sessionId"], effect["receipt"]["turnId"], request["rootId"])
        self.app.reconcile_graph(self.app.binding_by_graph(created["graphId"]))

    def test_running_branch_revises_after_exact_drain_and_keeps_independent_acceptance(self):
        created = self.planned()
        self.complete_task(created, self.ids["b"], "B 已验收")
        worker_a = self.next_effect(created, "execute", self.ids["a"])
        before = self.app.ledger.snapshot(created["graphId"], created["graphId"])
        accepted_b = before.task(self.ids["b"])
        payload = self.revision_payload(created)
        options = self.app.command(self.room["id"], {
            "action": "revision_options", "graphId": created["graphId"],
            "taskId": self.ids["a"]})
        self.assertEqual(set(options["affectedTaskIds"]), {self.ids["a"], self.ids["c"]})
        self.assertEqual(options["retainedAcceptedTaskIds"], [self.ids["b"]])
        requested = self.app.command(self.room["id"], payload)
        self.assertEqual(requested["status"], "awaiting_drain")
        self.assertIn(worker_a["effectId"], requested["requiredDispatchIds"])
        self.assertTrue(self.app.command(self.room["id"], payload)["idempotentReplay"])
        with self.assertRaises(GraphConflict):
            self.app.command(self.room["id"], {**payload, "reason": "different"})
        with self.assertRaises(GraphConflict):
            self.app.submit_execution(worker_a, {
                "resultSummary": "late", "evidenceRefs": ["fixture:late"], "artifactRefs": []})
        self.app.tick(limit=1)
        self.assertEqual(self.abort.call_count, 1)
        self.assertEqual(len(self.app.ledger.snapshot(created["graphId"], created["graphId"]).active_tasks), 4)
        self.drain_old(created, worker_a)
        view = self.app.projection(self.room["id"], created["graphId"])
        revision = view["revisions"][-1]
        self.assertEqual(revision["status"], "applied")
        new_a, new_c = (revision["successors"][self.ids[key]] for key in ("a", "c"))
        self.assertEqual(set(revision["successorTaskIds"]), {new_a, new_c})
        self.assertEqual(set(view["activeTaskIds"]), {created["workItemId"], self.ids["b"], new_a, new_c})
        self.assertEqual(view["currentRootObjective"], payload["rootObjective"])
        self.assertEqual(self.app.ledger.snapshot(created["graphId"], created["graphId"]).task(self.ids["b"]), accepted_b)
        self.assertEqual(self.app.ledger.snapshot(created["graphId"], created["graphId"]).task(self.ids["a"]).state, "cancelled")
        with self.assertRaises(GraphConflict):
            self.app.submit_execution(worker_a, {
                "resultSummary": "late", "evidenceRefs": ["fixture:late"], "artifactRefs": []})
        self.complete_task(created, new_a, "A 新成果已验收")
        self.complete_task(created, new_c, "C 新成果已验收")
        final_turn = self.next_effect(created, "synthesize", created["workItemId"])
        self.submit(final_turn, "final_submit", {
            "content": "A 新成果、B 原成果、C 新成果均通过。",
            "evidenceRefs": ["fixture:a-new", "fixture:b", "fixture:c-new"],
        })
        self.finish(final_turn)
        self.app.tick()
        final = self.app.projection(self.room["id"], created["graphId"])
        self.assertEqual(final["final"]["status"], "completed")
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_executor_claims WHERE graph_id=?",
                                          (created["graphId"],)).fetchone()[0], 0)

    def test_independent_prepared_dispatch_survives_topology_and_goal_revision(self):
        created = self.planned()
        snapshot = self.app.ledger.snapshot(created["graphId"], created["graphId"])
        observation = self.app.observe(snapshot, None)
        candidates = build_candidates(
            snapshot, event_id="prepare-independent", executions=observation.executions,
            executors=observation.executors, eligible_pairs=observation.eligible_pairs,
            manifests=observation.manifests,
        )
        prepared = next(action for action in candidates.actions
                        if action.operation == "claim_dispatch" and action.task_id == self.ids["b"])
        self.app.owner.apply(snapshot, prepared, command_id="prepared-independent")
        with self.app.ledger.connection() as conn:
            dispatch_id = conn.execute(
                "SELECT effect_id FROM agent_jev_runtime_effects WHERE command_id='prepared-independent'"
            ).fetchone()[0]
        self.assertEqual(self.app.effects.get(dispatch_id)["state"], "pending")
        payload = self.revision_payload(created)
        result = self.app.command(self.room["id"], payload)
        self.assertEqual(result["requiredDispatchIds"], [])
        self.app.reconcile_graph(self.app.binding_by_graph(created["graphId"]))
        view = self.app.projection(self.room["id"], created["graphId"])
        self.assertEqual(view["revisions"][-1]["status"], "applied")
        self.assertEqual(view["requirementsRevision"], 2)
        self.assertIn(self.ids["b"], view["activeTaskIds"])
        self.assertEqual(self.app.effects.deliver(dispatch_id)["state"], "accepted")
        self.assertEqual(self.app.effects.get(dispatch_id)["request"]["taskId"], self.ids["b"])

    def test_independent_verifier_can_submit_after_other_branch_goal_revision(self):
        created = self.planned()
        worker_b = self.next_effect(created, "execute", self.ids["b"])
        self.submit(worker_b, "result_submit", {
            "resultSummary": "B 固定成果", "evidenceRefs": ["fixture:b"], "artifactRefs": []})
        self.finish(worker_b)
        snapshot = self.app.ledger.snapshot(created["graphId"], created["graphId"])
        b = snapshot.task(self.ids["b"])
        verifier_owner = next(participant for participant in self.app.eligible_participants(
            snapshot, {}, include_busy=False) if participant["id"] != b.owner_id)
        verifier_id = self.app.lifecycle.prepare(snapshot, b, "verify", verifier_owner)
        self.assertIsNotNone(verifier_id)
        verifier = self.app.effects.deliver(verifier_id)
        self.assertEqual(verifier["state"], "accepted")
        self.app.command(self.room["id"], self.revision_payload(created))
        self.app.reconcile_graph(self.app.binding_by_graph(created["graphId"]))
        self.assertEqual(self.app.projection(self.room["id"], created["graphId"])["requirementsRevision"], 2)
        self.submit(verifier, "verification_submit", {
            "operabilityVerdict": "passed", "requirementVerdict": "satisfied",
            "reason": "B 独立结果仍符合原任务", "evidenceRefs": ["verify:b"],
        })
        self.finish(verifier)
        for _ in range(24):
            if self.app.ledger.snapshot(created["graphId"], created["graphId"]).task(b.id).state == "done":
                break
            self.app.tick(limit=1)
        self.assertEqual(self.app.ledger.snapshot(created["graphId"], created["graphId"]).task(b.id).state,
                         "done")

    def test_unknown_cancel_restarts_lookup_only_and_waits_for_physical_drain(self):
        created = self.planned()
        self.ids["b"] = "choose-a-instead"
        worker_a = self.next_effect(created, "execute", self.ids["a"])
        payload = self.revision_payload(created)
        lookup_ready = {"value": False}
        self.abort.side_effect = lambda *args, **kwargs: (
            self.cancel_receipt(*args, **kwargs)
            if kwargs.get("lookup_only") and lookup_ready["value"] else None)
        self.app.command(self.room["id"], payload)
        self.app.tick(limit=1)
        cancel_id = self.app.revisions._cancel_id(
            "jev-revision:" + digest([created["graphId"], payload["clientMessageId"]])[:40],
            worker_a["effectId"])
        self.assertEqual(self.app.effects.get("cancel:" + cancel_id)["state"], "unknown")
        lookup_ready["value"] = True
        self.app.recover()
        self.app.tick(limit=1)
        self.assertEqual(sum(not call.kwargs["lookup_only"] for call in self.abort.call_args_list), 1)
        self.assertTrue(any(call.kwargs["lookup_only"] for call in self.abort.call_args_list))
        self.assertEqual(self.app.projection(self.room["id"], created["graphId"])["revisions"][-1]["status"],
                         "awaiting_drain")
        self.drain_old(created, worker_a)
        self.assertEqual(self.app.projection(self.room["id"], created["graphId"])["revisions"][-1]["status"],
                         "applied")

    def test_unsupported_cancel_waits_for_natural_settlement_without_stranding_revision(self):
        created = self.planned()
        self.ids["b"] = "choose-a-instead"
        worker = self.next_effect(created, "execute", self.ids["a"])
        self.abort.side_effect = lambda *args, **kwargs: {
            **self.cancel_receipt(*args, **kwargs), "state": "rejected",
            "source": "paw_runtime_capability_preflight",
            "reason": "sessionExactTurnCancel_unsupported",
        }
        self.app.command(self.room["id"], self.revision_payload(created))
        self.app.tick(limit=1)
        self.assertEqual(self.app.projection(self.room["id"], created["graphId"])["revisions"][-1]["status"], "awaiting_drain")
        self.drain_old(created, worker, status="completed")
        self.assertEqual(self.app.projection(self.room["id"], created["graphId"])["revisions"][-1]["status"], "applied")
        retired = self.service.room_partner_dispatches.get(worker["effectId"])
        self.assertEqual(retired["status"], "returned")
        self.assertIn("未计入", retired["error"])

    def test_drained_old_dispatch_without_claim_still_cannot_submit_after_revision_intent(self):
        created = self.planned()
        self.ids["b"] = "choose-a-instead"
        worker = self.next_effect(created, "execute", self.ids["a"])
        request = worker["request"]
        dispatch = worker["effectId"]
        proof_ref = "physical-drain:" + dispatch
        self.terminals[dispatch] = {"eventId": proof_ref, "eventType": "turn_completed",
                                    "status": "aborted"}
        with self.app.ledger.connection(write=True) as conn:
            conn.execute("INSERT INTO agent_jev_execution_drains VALUES(?,?,?)",
                         (dispatch, canonical({"proofRef": proof_ref, "terminal": "aborted"}),
                          self.app.ledger.clock_ms()))
        self.service.runtime.release_prompt_admission(
            request["sessionId"], client_message_id=dispatch)
        self.service.room_turns.finish(
            request["sessionId"], worker["receipt"]["turnId"], request["rootId"])
        self.app.effects.release_executor(created["graphId"], created["graphId"],
            command_id="drain:" + dispatch, proof=ExecutionFact(
                request["taskId"], "drained", dispatch_id=dispatch,
                session_id=request["sessionId"], task_revision=request["taskRevision"],
                owner_id=request["ownerId"], assignment_key=request["assignmentKey"],
                accepted_turn_id=request["acceptedTurnId"], proof_ref=proof_ref,
                effects_reconciled=True))
        payload = self.revision_payload(created)
        receipt = self.app.command(self.room["id"], payload)
        self.assertEqual(receipt["requiredDispatchIds"], [])
        with self.assertRaises(GraphConflict):
            self.app.submit_execution(worker, {"resultSummary": "stale", "evidenceRefs": ["stale"],
                                               "artifactRefs": []})
        self.app.reconcile_graph(self.app.binding_by_graph(created["graphId"]))
        self.assertEqual(self.app.projection(self.room["id"], created["graphId"])["revisions"][-1]["status"],
                         "applied")
        with self.assertRaises(GraphConflict):
            self.app.submit_execution(worker, {"resultSummary": "stale", "evidenceRefs": ["stale"],
                                               "artifactRefs": []})

    def test_stale_cas_rolls_back_and_stop_cancels_pending_revision_without_successors(self):
        created = self.planned()
        self.ids["b"] = "choose-a-instead"
        worker = self.next_effect(created, "execute", self.ids["a"])
        payload = self.revision_payload(created)
        for wrong in ({"expectedTopologyRevision": payload["expectedTopologyRevision"] + 1},
                      {"expectedRequirementsRevision": payload["expectedRequirementsRevision"] + 1}):
            with self.subTest(wrong=wrong), self.assertRaises(GraphConflict):
                self.app.command(self.room["id"], {**payload, **wrong})
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM agent_jev_task_revisions WHERE graph_id=?",
                (created["graphId"],)).fetchone()[0], 0)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM agent_jev_commands WHERE command_id=?",
                (payload["clientMessageId"],)).fetchone())
        requested = self.app.command(self.room["id"], payload)
        self.assertEqual(requested["status"], "awaiting_drain")
        self.app.tick(limit=1)
        self.app.stop(self.room["id"], created["rootId"])
        self.assertEqual(self.app.projection(self.room["id"], created["graphId"])["revisions"][-1]["status"],
                         "cancelled")
        self.assertEqual(self.app.command(self.room["id"], payload)["status"], "awaiting_drain")
        self.drain_old(created, worker)
        self.app.tick()
        view = self.app.projection(self.room["id"], created["graphId"])
        self.assertEqual(view["revisions"][-1]["status"], "cancelled")
        self.assertEqual(view["revisions"][-1]["successorTaskIds"], [])
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM agent_jev_task_supersessions WHERE graph_id=?",
                (created["graphId"],)).fetchone()[0], 0)

    def test_approved_plan_remains_authorized_after_explicit_task_revision(self):
        created = self.app.create(self.room["id"], {
            "clientMessageId": "approved-revision-plan", "message": "先完成 A",
            "strategy": "plan", "modelRouting": "participant", "executionApproval": True,
        })
        self.app.tick()
        planner = self.effects(created, "plan")[0]
        self.submit(planner, "plan_submit", {
            "requirementsRevision": 1, "topologyRevision": 0,
            "tasks": [{"key": "a", "objective": "A 原要求", "expectedOutput": "A 原成果",
                       "acceptanceCriteria": ["A 原验收"], "dependsOn": []}],
        })
        self.finish(planner)
        hash_value = self.app.projection(self.room["id"], created["graphId"])["planApproval"]["planHash"]
        self.app.command(self.room["id"], {
            "action": "approve_plan", "graphId": created["graphId"],
            "rootId": created["rootId"], "planHash": hash_value,
            "clientMessageId": "approve-revision-plan",
        })
        with self.app.ledger.connection() as conn:
            task_id = conn.execute(
                "SELECT task_id FROM agent_jev_task_requirements WHERE graph_id=?",
                (created["graphId"],)).fetchone()[0]
        options = self.app.command(self.room["id"], {
            "action": "revision_options", "graphId": created["graphId"], "taskId": task_id})
        self.app.command(self.room["id"], {
            "action": "revise_task", "graphId": created["graphId"],
            "rootId": created["rootId"], "taskId": task_id,
            "taskHash": options["taskHash"],
            "expectedTopologyRevision": options["expectedTopologyRevision"],
            "expectedRequirementsRevision": options["expectedRequirementsRevision"],
            "clientMessageId": "approved-task-revision", "objective": "A 新要求",
            "expectedOutput": "A 新成果", "acceptanceCriteria": ["A 新验收"],
            "reason": "明确修订 A",
        })
        self.app.reconcile_graph(self.app.binding_by_graph(created["graphId"]))
        view = self.app.projection(self.room["id"], created["graphId"])
        self.assertEqual(view["revisions"][-1]["status"], "applied")
        self.assertEqual(view["planApproval"]["status"], "approved")
        self.assertEqual(view["planApproval"]["requirementsRevision"], 2)
        self.assertEqual(view["requirementsRevision"], 2)

    def test_route_policy_accepts_revision_options_without_command_id_and_exact_mutation(self):
        from rag_ime.control_api import ControlAccessContext, ControlPathId, ControlRequest, default_route_policy

        created = self.planned()
        options = {"action": "revision_options", "graphId": created["graphId"],
                   "taskId": self.ids["a"]}
        payload = self.revision_payload(created)
        for body in (options, payload):
            default_route_policy().authorize(ControlRequest(
                request_id="revision-route", path_id=ControlPathId.AGENT_JEV_COMMAND.value,
                params={"roomId": self.room["id"]}, body=body), ControlAccessContext.native())
