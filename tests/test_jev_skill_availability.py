"""Jev routes explicit Skill requirements using one Session's loaded Pi catalog.

The catalog is a test double here; no Provider or installed Pi Host is started.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from rag_ime.jev_tasks.types import GraphConflict, GraphError, canonical
from rag_ime.pi.values import PiRuntimeError
from tests.test_jev_host_application import JevHostFixture


class JevSkillAvailabilityTests(JevHostFixture):
    def setUp(self):
        super().setUp()
        self.catalogs: dict[str, dict[str, object]] = {}
        self.catalog_calls: list[str] = []

        def catalog(session_id: str) -> dict[str, object]:
            self.catalog_calls.append(session_id)
            return self.catalogs.get(session_id, {
                "runtimeAvailable": True, "items": [],
            })

        patcher = patch.object(self.service.session_policy, "skill_catalog", side_effect=catalog)
        patcher.start()
        self.addCleanup(patcher.stop)

    def expose(self, session_id: str, *skills: str, runtime_available: bool = True) -> None:
        self.catalogs[session_id] = {
            "runtimeAvailable": runtime_available,
            "items": [
                {"source": "skill", "name": "skill:" + skill}
                for skill in skills
            ],
        }

    def require_skill(self, created: dict[str, object], skill: str) -> None:
        task = self.snapshot(created).task(str(created["workItemId"]))
        with self.app.ledger.connection(write=True) as conn:
            conn.execute(
                "INSERT INTO agent_jev_task_requirements VALUES(?,?,?)",
                (task.id, created["graphId"], canonical({
                    "requiredCapabilities": ["skill:" + skill],
                })),
            )

    def test_only_real_enabled_skill_catalog_qualifies_a_partner(self):
        created = self.create()
        snapshot = self.snapshot(created)
        owner = snapshot.task(str(created["workItemId"]))
        target = next(p for p in self.room["participants"] if p["id"] != owner.owner_id)
        self.expose(target["sessionId"], "systematic-debugging")
        eligible = self.app.eligible_participants(
            snapshot, {"requiredCapabilities": ["skill:systematic-debugging"]},
            include_busy=True,
        )
        self.assertEqual([p["id"] for p in eligible], [target["id"]])
        self.assertIn(target["sessionId"], self.catalog_calls)

        self.expose(target["sessionId"], "systematic-debugging", runtime_available=False)
        self.assertEqual(self.app.eligible_participants(
            snapshot, {"requiredCapabilities": ["skill:systematic-debugging"]},
            include_busy=True,
        ), [])

    def test_tool_only_tasks_do_not_query_the_skill_catalog(self):
        created = self.create()
        self.assertEqual(len(self.app.eligible_participants(
            self.snapshot(created), {}, include_busy=True,
        )), 2)
        self.assertEqual(self.catalog_calls, [])

    def test_one_observation_reuses_the_session_catalog_for_multiple_tasks(self):
        created = self.create()
        snapshot = self.snapshot(created)
        catalogs: dict[str, dict[str, object]] = {}
        for _ in range(2):
            self.app.eligible_participants(
                snapshot, {"requiredCapabilities": ["skill:systematic-debugging"]},
                include_busy=True, skill_catalogs=catalogs,
            )
        self.assertEqual(sorted(self.catalog_calls), sorted(
            p["sessionId"] for p in self.room["participants"]
        ))

    def test_missing_skill_does_not_invalidate_an_otherwise_authorized_plan(self):
        created = self.app.create(self.room["id"], {
            "clientMessageId": "skill-plan", "message": "Use a specialized Skill",
            "strategy": "plan", "modelRouting": "participant",
        })
        snapshot = self.snapshot(created)
        proposal = {
            "requirementsRevision": 1,
            "topologyRevision": snapshot.topology_revision,
            "tasks": [{
                "key": "specialized", "objective": "Perform the Skill task",
                "expectedOutput": "Checked result", "acceptanceCriteria": ["Skill used"],
                "dependsOn": [], "requiredCapabilities": ["skill:systematic-debugging"],
            }],
        }
        with self.app.ledger.connection() as conn:
            self.assertEqual(
                self.app.lifecycle.validate_plan(conn, snapshot, proposal),
                proposal["tasks"],
            )
        self.assertEqual(self.catalog_calls, [])

    def test_plan_rejects_invalid_skill_name_and_still_requires_real_tool_scope(self):
        created = self.app.create(self.room["id"], {
            "clientMessageId": "skill-plan-invalid", "message": "Use a specialized Skill",
            "strategy": "plan", "modelRouting": "participant",
        })
        snapshot = self.snapshot(created)
        task = {"key": "specialized", "objective": "Perform the Skill task",
                "expectedOutput": "Checked result", "acceptanceCriteria": ["Skill used"],
                "dependsOn": []}
        proposal = {"requirementsRevision": 1,
                    "topologyRevision": snapshot.topology_revision, "tasks": [task]}
        with self.app.ledger.connection() as conn:
            for capability in ("skill:", "skill:wrong/name", "unknown_tool"):
                with self.subTest(capability=capability), self.assertRaises(GraphError):
                    self.app.lifecycle.validate_plan(conn, snapshot, {
                        **proposal, "tasks": [{**task, "requiredCapabilities": [capability]}],
                    })

    def test_missing_owner_skill_reassigns_only_to_live_catalog_match(self):
        created = self.create()
        self.require_skill(created, "systematic-debugging")
        before = self.snapshot(created)
        task = before.task(str(created["workItemId"]))
        target = next(p for p in self.room["participants"] if p["id"] != task.owner_id)
        self.expose(target["sessionId"], "systematic-debugging")
        observation = self.app.observe(before, None)
        self.assertEqual(observation.eligible_pairs, frozenset({(task.id, target["id"])}))
        self.app.tick(limit=1)
        self.assertEqual(self.snapshot(created).task(task.id).owner_id, target["id"])
        self.assertEqual(self.calls, [])
        self.app.tick(limit=1)
        self.assertEqual([session_id for session_id, _ in self.calls], [target["sessionId"]])

    def test_missing_or_unreadable_catalog_waits_with_exact_reason_then_advance_rechecks(self):
        created = self.create()
        self.require_skill(created, "systematic-debugging")
        self.app.tick(limit=1)
        self.assertEqual(self.calls, [])
        with self.app.ledger.connection() as conn:
            result = json.loads(conn.execute(
                "SELECT result_json FROM agent_jev_owner_events WHERE graph_id=? ORDER BY created_at_ms LIMIT 1",
                (created["graphId"],),
            ).fetchone()[0])
        self.assertEqual(result["status"], "waiting")
        self.assertIn("required Skill absent from Pi catalog: systematic-debugging",
                      " ".join(result["missing"]))

        owner = self.snapshot(created).task(str(created["workItemId"]))
        for participant in self.room["participants"]:
            self.expose(participant["sessionId"], runtime_available=False)
        self.app.command(self.room["id"], {
            "action": "advance", "graphId": created["graphId"],
            "clientMessageId": "skill-catalog-unavailable",
        })
        self.app.tick(limit=1)
        with self.app.ledger.connection() as conn:
            result = json.loads(conn.execute(
                "SELECT result_json FROM agent_jev_owner_events WHERE graph_id=? AND source_id=?",
                (created["graphId"], "resume:skill-catalog-unavailable"),
            ).fetchone()[0])
        self.assertIn("Pi Skill catalog unavailable", " ".join(result["missing"]))
        self.assertEqual(self.calls, [])

        owner_session = self.service.rooms.participant(owner.owner_id)["sessionId"]
        self.expose(owner_session, "systematic-debugging")
        self.app.command(self.room["id"], {
            "action": "advance", "graphId": created["graphId"],
            "clientMessageId": "skill-restored",
        })
        self.app.tick(limit=1)
        self.assertEqual([session_id for session_id, _ in self.calls], [owner_session])

    def test_prepared_dispatch_rechecks_skill_after_catalog_revocation(self):
        created = self.create()
        self.require_skill(created, "systematic-debugging")
        for participant in self.room["participants"]:
            self.expose(participant["sessionId"], "systematic-debugging")
        self.app.tick(limit=1)
        task = self.snapshot(created).task(str(created["workItemId"]))
        self.assertTrue(task.accepted_turn_id)
        request = self.app.effects.get(task.accepted_turn_id)["request"]
        owner = self.service.rooms.participant(task.owner_id)
        self.expose(owner["sessionId"])
        with self.assertRaisesRegex(GraphConflict, "capability|scope"):
            self.app.validate_current("dispatch", request)


class JevEffectiveSkillCatalogTests(JevHostFixture):
    def test_runtime_failure_is_unknown_and_loaded_skill_is_preserved(self):
        session_id = self.sessions[0]["id"]
        with patch.object(self.service.runtime, "skill_catalog", side_effect=PiRuntimeError("unavailable")):
            self.assertEqual(self.service.session_policy.skill_catalog(session_id), {
                "runtimeAvailable": False, "items": [],
            })
        with patch.object(self.service.runtime, "skill_catalog", return_value=[
            {"name": "skill:systematic-debugging", "source": "skill"},
        ]):
            self.assertEqual(self.service.session_policy.skill_catalog(session_id), {
                "runtimeAvailable": True,
                "items": [{"name": "skill:systematic-debugging", "source": "skill"}],
            })
