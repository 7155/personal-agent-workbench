"""Real Store/gateway approval transactions; Pi drain remains an explicit test boundary."""
from __future__ import annotations

from unittest.mock import patch

from rag_ime.agent_tools import ControlToolGateway
from rag_ime.jev_tasks.application import JevRoomApplication
from rag_ime.jev_tasks.types import GraphConflict, GraphError
from tests import test_jev_lifecycle as lifecycle_fixtures
from tests.test_jev_host_application import JevHostFixture


class JevPlanApprovalTests(JevHostFixture):
    effects = lifecycle_fixtures.JevLifecycleTests.effects
    submit = lifecycle_fixtures.JevLifecycleTests.submit
    finish = lifecycle_fixtures.JevLifecycleTests.finish

    def setUp(self):
        super().setUp()
        self.gateway = ControlToolGateway(sessions=self.service.sessions, management=object(), core=object(),
            project=self.service.project, collaboration=self.service, background_jobs=self.service.background_jobs,
            delegation=self.service.delegation, work_documents=self.service.work_documents)
        self.service.bind_tool_manifest_provider(self.gateway.runtime_manifests)
        self.terminals = {}
        self.patch_terminal()

    def patch_terminal(self):
        original = self.app.execution_terminal
        p = patch.object(self.app, "execution_terminal", side_effect=lambda effect, **kwargs:
            self.terminals.get(effect["effectId"]) or original(effect, **kwargs))
        p.start()
        self.addCleanup(p.stop)

    def start_planner(self):
        created = self.app.create(self.room["id"], {"clientMessageId": "approved-root", "message": "核对资料并交付结论",
            "strategy": "direct", "modelRouting": "participant", "executionApproval": True})
        self.app.tick()
        planner = self.effects(created, "plan")[0]
        return created, planner

    def test_auto_approval_routes_greeting_before_planning(self):
        created = self.app.create(self.room["id"], {"clientMessageId": "hello-auto", "message": "hi",
            "strategy": "auto", "modelRouting": "participant", "executionApproval": True})
        self.assertEqual(self.view(created)["phase"], "route")
        self.app.tick()
        self.assertEqual(self.view(created)["phase"], "execute")
        self.assertIsNone(self.view(created).get("planApproval"))
        self.assertEqual(self.effects(created, "plan"), [])
        self.app.tick()
        self.assertEqual(len(self.effects(created, "execute")), 1)

    def test_auto_multistep_keeps_plan_approval(self):
        created = self.app.create(self.room["id"], {"clientMessageId": "multi-auto", "message": "先调研，再实现并测试",
            "strategy": "auto", "modelRouting": "participant", "executionApproval": True})
        with patch.object(self.app.driver.controller.decider, "choose_action",
                          side_effect=lambda state, actions: (next(a for a in actions if a.operation == "plan"), {})):
            self.app.tick()
        self.assertEqual(self.view(created)["phase"], "plan")
        self.assertEqual(self.view(created)["planApproval"]["status"], "planning")
        self.assertEqual(self.effects(created, "execute"), [])
        self.app.tick()
        self.assertEqual(len(self.effects(created, "plan")), 1)

    def planned(self, *, drain=True, questions=False):
        created, planner = self.start_planner()
        proposal = {"requirementsRevision": 1, "topologyRevision": 0}
        if questions:
            proposal["questions"] = [{"id": "scope", "question": "核对哪一份资料？", "options": ["资料 A", "资料 B"]}]
        else:
            proposal["tasks"] = [{"key": "check", "objective": "核对资料", "expectedOutput": "可复核结论",
                                  "acceptanceCriteria": ["提供源证据"], "dependsOn": []}]
        self.submit(planner, "plan_submit", proposal)
        if drain:
            self.finish(planner)
        return created, planner, proposal

    def view(self, created):
        return self.app.projection(self.room["id"], created["graphId"])

    def command(self, created, action, *, key=None, plan_hash=None, **fields):
        return self.service.jev_command(self.room["id"], {"action": action, "graphId": created["graphId"],
            "rootId": created["rootId"], "planHash": plan_hash or self.view(created)["planApproval"]["planHash"],
            "clientMessageId": key or action, **fields})

    def test_no_workers_before_one_complete_plan_is_approved(self):
        created, planner, _ = self.planned(drain=False)
        self.assertEqual(self.view(created)["planApproval"]["status"], "planning")
        with self.assertRaises(GraphConflict):
            self.command(created, "approve_plan")
        self.finish(planner)
        self.assertEqual(self.view(created)["phase"], "awaiting_approval")
        for _ in range(3):
            self.app.tick()
            self.view(created)
        self.assertEqual(len(self.snapshot(created).tasks), 1)
        self.assertEqual(self.effects(created, "execute"), [])
        result = self.command(created, "approve_plan")
        self.assertEqual(result["planApproval"]["status"], "approved")
        self.app.tick()
        self.assertEqual(len(self.effects(created, "execute")), 1)

    def test_duplicate_approval_is_atomic_and_replays_the_same_receipt(self):
        created, _, _ = self.planned()
        first = self.command(created, "approve_plan")
        replay = self.command(created, "approve_plan")
        self.assertFalse(first["idempotentReplay"])
        self.assertTrue(replay["replayed"])
        self.assertTrue(replay["idempotentReplay"])
        self.assertTrue(self.command(created, "approve_plan", key="second-click")["replayed"])
        self.assertEqual(len(self.snapshot(created).tasks), 2)
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_plan_receipts").fetchone()[0], 1)

    def test_stale_hash_and_wrong_root_cannot_approve(self):
        created, _, _ = self.planned()
        with self.assertRaises(GraphConflict) as stale:
            self.command(created, "approve_plan", plan_hash="old-plan")
        self.assertEqual(stale.exception.http_status, 409)
        with self.assertRaises(GraphConflict):
            self.command(created, "approve_plan", rootId="another-root")
        self.assertEqual(len(self.snapshot(created).tasks), 1)

    def test_defer_and_restart_keep_plan_without_worker_dispatch(self):
        created, _, _ = self.planned()
        result = self.command(created, "defer_plan")
        plan_hash = result["planApproval"]["planHash"]
        self.app = JevRoomApplication(self.service, decider=self.app.driver.controller.decider)
        self.service.jev_application = self.app
        self.patch_terminal()
        self.app.recover()
        self.app.tick()
        view = self.view(created)
        self.assertEqual(view["phase"], "deferred")
        self.assertEqual(view["planApproval"]["planHash"], plan_hash)
        self.assertEqual(view["planApproval"]["lastActionClientMessageId"], "defer_plan")
        self.assertEqual(self.effects(created, "execute"), [])
        self.assertEqual(self.command(created, "approve_plan")["planApproval"]["status"], "approved")

    def test_adjust_revises_requirements_and_rejects_old_plan_hash(self):
        created, old_planner, _ = self.planned()
        old_hash = self.view(created)["planApproval"]["planHash"]
        adjustment = self.command(created, "adjust_plan", message="仅核对资料 A，不写文件")
        self.assertEqual(adjustment["planApproval"]["requirementsRevision"], 2)
        self.assertEqual(adjustment["planApproval"]["planHash"], "")
        self.assertTrue(self.command(created, "adjust_plan", plan_hash=old_hash,
            message="仅核对资料 A，不写文件")["replayed"])
        with self.assertRaises(GraphConflict):
            self.command(created, "adjust_plan", plan_hash=old_hash, message="reuse this ID for different requirements")
        with self.assertRaises(GraphConflict):
            self.command(created, "approve_plan", plan_hash=old_hash)
        self.app.tick()
        planners = self.effects(created, "plan")
        self.assertEqual(len(planners), 2)
        fresh = next(effect for effect in planners if effect["effectId"] != old_planner["effectId"])
        self.assertIn("仅核对资料 A，不写文件", fresh["request"]["taskBrief"]["objective"])
        self.assertEqual(self.effects(created, "execute"), [])
        messages = [event for event in self.service.rooms.list_events(self.room["id"])
                    if event["eventType"] == "user_message" and event["payload"].get("clientMessageId") == "adjust_plan"]
        self.assertEqual(len(messages), 1)

    def test_clarification_waits_for_input_then_replans(self):
        created, _, _ = self.planned(questions=True)
        approval = self.view(created)["planApproval"]
        self.assertEqual(approval["status"], "awaiting_input")
        self.assertEqual(approval["clarifications"][0]["id"], "scope")
        with self.assertRaises(GraphConflict):
            self.command(created, "approve_plan")
        self.command(created, "adjust_plan", message="请核对资料 A")
        self.app.tick()
        self.assertEqual(len(self.effects(created, "plan")), 2)
        self.assertEqual(self.effects(created, "execute"), [])

    def test_failed_apply_rolls_back_approval_and_tasks(self):
        created, _, _ = self.planned()
        with patch.object(self.service.room_work, "create", side_effect=RuntimeError("write failure")):
            with self.assertRaises(RuntimeError):
                self.command(created, "approve_plan")
        self.assertEqual(self.view(created)["planApproval"]["status"], "awaiting_approval")
        self.assertEqual(len(self.snapshot(created).tasks), 1)
        self.assertFalse(self.command(created, "approve_plan")["replayed"])

    def test_historical_auto_root_keeps_existing_behavior(self):
        created = self.create()
        self.assertNotIn("planApproval", self.view(created))
        self.app.tick()
        self.assertEqual(len(self.effects(created, "execute")), 1)

    def test_staged_cycle_is_rejected_without_any_task_creation(self):
        created, planner = self.start_planner()
        proposal = {"requirementsRevision": 1, "topologyRevision": 0, "tasks": [
            {"key": "first", "objective": "First", "expectedOutput": "One", "acceptanceCriteria": ["Evidence"], "dependsOn": ["second"]},
            {"key": "second", "objective": "Second", "expectedOutput": "Two", "acceptanceCriteria": ["Evidence"], "dependsOn": ["first"]},
        ]}
        with self.assertRaises((GraphConflict, GraphError)):
            self.submit(planner, "plan_submit", proposal)
        self.assertEqual(len(self.snapshot(created).tasks), 1)
        self.assertEqual(self.view(created)["planApproval"]["planHash"], "")

    def test_adjustment_includes_real_room_attachment_in_next_execution_pack(self):
        created, old, _ = self.planned()
        media = self.service.import_media(room_id=self.room["id"], data=b"Read only scope A",
            mime_type="text/plain", file_name="scope.txt")["media"]
        result = self.command(created, "adjust_plan", message="Use this scope", attachmentIds=[media["mediaId"]])
        self.assertEqual(result["planApproval"]["revisions"][-1]["attachmentIds"], [media["mediaId"]])
        self.app.tick()
        planner = next(e for e in self.effects(created, "plan") if e["effectId"] != old["effectId"])
        self.assertIn(media["mediaId"], planner["request"]["contextManifest"]["executionScope"]["attachmentIds"])

    def test_adjustment_cannot_use_an_attachment_owned_by_a_private_session(self):
        created, _, _ = self.planned()
        media = self.service.import_media(session_id=self.sessions[0]["id"], data=b"Private scope",
            mime_type="text/plain", file_name="private.txt")["media"]
        with self.assertRaises((KeyError, ValueError)):
            self.command(created, "adjust_plan", message="invalid source", attachmentIds=[media["mediaId"]])
        self.assertEqual(self.view(created)["planApproval"]["requirementsRevision"], 1)

    def test_approved_plan_cannot_be_adjusted_over_existing_execution(self):
        created, _, _ = self.planned()
        self.command(created, "approve_plan")
        with self.assertRaises(GraphConflict):
            self.command(created, "adjust_plan", message="replace the existing approved execution")

    def test_missing_planner_outputs_exhaust_without_starting_business_execution(self):
        created, planner = self.start_planner()
        for attempt in range(3):
            self.finish(planner)
            self.app.tick()
            if attempt < 2:
                planner = next(e for e in self.effects(created, "plan") if e["effectId"] not in self.terminals)
        self.assertEqual(len(self.effects(created, "plan")), 3)
        self.assertEqual(self.effects(created, "execute"), [])
        self.assertEqual(self.view(created)["final"]["status"], "failed")
