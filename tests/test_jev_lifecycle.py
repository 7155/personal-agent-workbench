"""Full canonical task lifecycle with explicit JEV/Pi transport doubles.

Stores, admission reservations, task graph, typed tool operations and Room event
publication are real. Terminal Pi proof is isolated here and verified separately
by the exact-runtime adapter tests.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from tests import test_jev_host_application as host
from rag_ime.jev_tasks.types import GraphConflict, GraphError


class JevLifecycleTests(host.JevHostFixture):
    # Host base tests intentionally also exercise this terminal adapter fixture.
    def setUp(self):
        super().setUp()
        from rag_ime.agent_tools import ControlToolGateway

        self.gateway = ControlToolGateway(
            sessions=self.service.sessions,
            management=object(),
            core=object(),
            project=self.service.project,
            collaboration=self.service,
            background_jobs=self.service.background_jobs,
            delegation=self.service.delegation,
            work_documents=self.service.work_documents,
        )
        self.service.bind_tool_manifest_provider(self.gateway.runtime_manifests)
        self.terminals = {}
        self.original_terminal = self.app.execution_terminal
        p = patch.object(
            self.app,
            "execution_terminal",
            side_effect=lambda effect, **kwargs: self.terminals.get(effect["effectId"])
            or self.original_terminal(effect, **kwargs),
        )
        p.start()
        self.addCleanup(p.stop)

    def effects(self, created, purpose=None):
        effects = self.app.projection(self.room["id"], created["graphId"])["effects"]
        return [
            e
            for e in effects
            if e["operation"] == "dispatch"
            and (purpose is None or e["request"].get("purpose", "execute") == purpose)
        ]

    def submit(self, effect, operation, proposal):
        # Pi validates the projected tool schema before any HTTP call. Checking
        # only gateway.execute misses a dropped oneOf operation branch.
        from jsonschema import Draft202012Validator

        session = self.service.sessions.get(effect["request"]["sessionId"])
        tool = next(
            t
            for t in self.gateway.runtime_manifests(session)
            if t["name"] == "room_partner"
        )
        Draft202012Validator(tool["parameters"]).validate(
            {"op": operation, "proposal": proposal}
        )
        return self.gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": effect["request"]["sessionId"],
                "tool": "room_partner",
                "toolCallId": "tool:" + effect["effectId"],
                "args": {"op": operation, "proposal": proposal},
            }
        )["result"]

    def finish(self, effect):
        request = effect["request"]
        self.terminals[effect["effectId"]] = {
            "eventId": "terminal:" + effect["effectId"],
            "eventType": "turn_completed",
            "status": "completed",
        }
        self.service.runtime.release_prompt_admission(
            request["sessionId"], client_message_id=request["dispatchId"]
        )
        self.service.room_turns.finish(
            request["sessionId"], effect["receipt"]["turnId"], request["rootId"]
        )
        self.app.reconcile_graph(self.app.binding_by_graph(request["graphId"]))

    def planned(self):
        created = self.app.create(
            self.room["id"],
            {
                "clientMessageId": "plan-1",
                "message": "先分析再实现并验证",
                "strategy": "plan",
                "modelRouting": "participant",
            },
        )
        self.app.tick()
        effect = self.effects(created, "plan")[0]
        self.assertEqual(effect["state"], "accepted")
        proposal = {
            "requirementsRevision": 1,
            "topologyRevision": 0,
            "tasks": [
                {
                    "key": "a",
                    "objective": "分析",
                    "expectedOutput": "分析结论",
                    "acceptanceCriteria": ["提供事实"],
                    "dependsOn": [],
                },
                {
                    "key": "b",
                    "objective": "实现",
                    "expectedOutput": "实现结果",
                    "acceptanceCriteria": ["依据分析完成"],
                    "dependsOn": ["a"],
                },
            ],
        }
        return created, effect, proposal

    def test_plan_atomic_validation_and_purpose_identity(self):
        created, effect, proposal = self.planned()
        root = self.snapshot(created).tasks[0]
        self.assertEqual(root.accepted_turn_id, "")
        self.assertEqual(root.state, "active")
        invalid = json.loads(json.dumps(proposal))
        invalid["tasks"][0]["dependsOn"] = ["b"]
        with self.assertRaises((GraphError, ValueError)):
            self.submit(effect, "plan_submit", invalid)
        self.assertEqual(len(self.snapshot(created).tasks), 1)
        result = self.submit(effect, "plan_submit", proposal)
        self.assertTrue(result["ok"])
        self.assertTrue(self.submit(effect, "plan_submit", proposal)["replayed"])
        snapshot = self.snapshot(created)
        self.assertEqual(len(snapshot.tasks), 3)
        self.assertEqual(len(snapshot.edges), 1)
        self.assertEqual(snapshot.task(root.id).accepted_turn_id, "")

    def test_plan_execute_verify_dependencies_and_single_final(self):
        created, planner, proposal = self.planned()
        self.submit(planner, "plan_submit", proposal)
        self.finish(planner)
        for key in ("a", "b"):
            self.app.tick()
            active = [
                e
                for e in self.effects(created, "execute")
                if e["effectId"] not in self.terminals
            ]
            self.assertEqual(
                len(active), 1, self.app.projection(self.room["id"], created["graphId"])
            )
            worker = active[0]
            self.submit(
                worker,
                "result_submit",
                {
                    "resultSummary": key + " 已完成",
                    "evidenceRefs": ["test:" + key],
                    "artifactRefs": [],
                },
            )
            self.finish(worker)
            self.app.tick()
            verifier = [
                e
                for e in self.effects(created, "verify")
                if e["effectId"] not in self.terminals
            ][0]
            self.assertNotEqual(
                verifier["request"]["ownerId"], worker["request"]["ownerId"]
            )
            self.submit(
                verifier,
                "verification_submit",
                {
                    "operabilityVerdict": "passed",
                    "requirementVerdict": "satisfied",
                    "reason": "已实际核对测试成果",
                    "evidenceRefs": ["check:" + key],
                },
            )
            self.finish(verifier)
            self.app.tick()
            self.assertEqual(
                self.snapshot(created).task(worker["request"]["taskId"]).state, "done"
            )
        synthesizer = self.effects(created, "synthesize")[0]
        self.submit(
            synthesizer,
            "final_submit",
            {
                "content": "已完成分析和实现，并核验两项成果。",
                "evidenceRefs": ["test:a", "test:b"],
            },
        )
        self.finish(synthesizer)
        self.app.tick()
        self.app.recover()
        self.app.tick()
        view = self.app.projection(self.room["id"], created["graphId"])
        self.assertEqual(view["final"]["status"], "completed")
        with self.app.ledger.connection() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_jev_executor_claims WHERE graph_id=?",
                    (created["graphId"],),
                ).fetchone()[0],
                0,
            )
        events = self.service.rooms.list_events(self.room["id"])
        self.assertEqual(
            sum(
                e["eventType"] == "turn_completed" and e["turnId"] == created["rootId"]
                for e in events
            ),
            1,
        )
