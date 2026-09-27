"""Real stores/service/admission with explicit provider and Pi test boundaries."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.types import GraphConflict, digest
from rag_ime.pi.config import PiRuntimeConfig


def choose_execution(state, questions):
    import json

    actions = json.loads(state)["actions"]
    selected = next(a["id"] for a in actions if a["operation"] != "wait")
    keys = questions["decision"]["criteria"]
    return {
        "model": "test-choice",
        "answers": {
            "decision": {
                "type": "choice",
                "choice": selected,
                "probabilities": {k: 1.0 if k == selected else 0.0 for k in keys},
                "confidence": 1.0,
            }
        },
    }


class JevHostFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-jev-host-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.service = AgentService(
            db_path=root / "test.sqlite",
            startup_recovery_enabled=False,
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=root / "config",
                session_dir=root / "sessions",
                logs_dir=root / "logs",
            ),
        )
        self.addCleanup(self.service.close)
        self.sessions = [
            self.service.sessions.create(title=name)
            for name in ("Controller", "Executor")
        ]
        self.room = self.service.rooms.create(
            title="Jev test",
            routing_policy="moderator",
            participants=[
                {
                    "sessionId": s["id"],
                    "roleId": role,
                    "roleVersion": "1",
                    "displayName": s["title"],
                    "collaborationRole": role,
                }
                for s, role in zip(self.sessions, ("coordinator", "implementer"))
            ],
        )
        self.app = self.service.jev_application
        self.app.driver.controller.decider = JevChoices(choose_execution)
        for name in ("_restore_room_participant_sessions",):
            p = patch.object(self.service, name)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(self.service, "_room_target_idle", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        # Real Runtime reservation/active/release callbacks remain paired.
        p = patch.object(self.service, "prompt", side_effect=self.accept)
        self.prompt = p.start()
        self.addCleanup(p.stop)
        self.calls = []

    def accept(self, session_id, payload):
        self.calls.append((session_id, dict(payload)))
        return {"accepted": True, "turnId": "pi-turn:" + payload["clientMessageId"]}

    def create(self, key="request-1"):
        return self.service.jev_command(
            self.room["id"],
            {
                "action": "create",
                "clientMessageId": key,
                "message": "完成这项后端测试",
                "strategy": "direct",
                "modelRouting": "participant",
                "controllerParticipantId": self.room["participants"][0]["id"],
            },
        )

    def snapshot(self, created):
        return self.app.ledger.snapshot(created["graphId"], created["graphId"])


class JevHostTests(JevHostFixture):
    def test_room_tool_list_does_not_reinject_dispatch_context_or_decision_journal(self):
        import json
        created = self.create()
        self.app.tick()
        view = self.app.projection(self.room["id"], created["graphId"])
        effect = next(item for item in view["effects"] if item["operation"] == "dispatch")
        with self.app.ledger.connection(write=True) as conn:
            request = dict(effect["request"])
            request["privateMaterialProbe"] = "not-another-executors-context"
            conn.execute("UPDATE agent_jev_runtime_effects SET request_json=? WHERE effect_id=?",
                         (json.dumps(request), effect["effectId"]))
        result = self.app.tool_operation(effect["request"]["sessionId"], {"op": "list"}, tool_call_id="list")
        self.assertEqual(result["tasks"], view["tasks"])
        self.assertEqual(result["submissionOperation"], "result_submit")
        self.assertNotIn("effects", result)
        self.assertNotIn("events", result)
        self.assertNotIn("contextManifest", json.dumps(result))
        self.assertNotIn("not-another-executors-context", json.dumps(result))
        self.assertEqual(result["executions"][0]["dispatchId"], effect["effectId"])

    def test_ready_owner_progresses_without_equivalent_reassignment_churn(self):
        from dataclasses import replace
        from rag_ime.jev_tasks.candidates import build_candidates

        created = self.create()
        snapshot = self.snapshot(created)
        observation = self.app.observe(snapshot, None)

        def offered(executors):
            return build_candidates(snapshot, event_id="candidate-regression",
                executions=observation.executions, executors=executors,
                eligible_pairs=observation.eligible_pairs,
                manifests=observation.manifests).actions

        actions = offered(observation.executors)
        self.assertEqual([a.operation for a in actions if a.operation != "wait"], ["claim_dispatch"])
        owner = snapshot.task(created["workItemId"]).owner_id
        actions = offered(tuple(replace(actor, available=False) if actor.participant_id == owner
                                else actor for actor in observation.executors))
        replacements = [a for a in actions if a.operation == "reassign"]
        self.assertEqual(len(replacements), 1)
        self.assertFalse(any(a.operation == "claim_dispatch" for a in actions))
        self.prompt.assert_not_called()

    def test_create_is_atomic_and_idempotent_and_get_never_dispatches(self):
        created = self.create()
        self.assertTrue(self.create()["idempotentReplay"])
        self.service.jev_workspace(self.room["id"], created["graphId"])
        self.assertEqual(len(self.snapshot(created).tasks), 1)
        self.prompt.assert_not_called()
        self.app.tick()
        self.assertEqual(len(self.calls), 1)
        self.app.tick()
        self.assertEqual(len(self.calls), 1)

    def test_real_admission_keeps_root_task_dispatch_and_execution_context(self):
        created = self.create()
        self.app.tick()
        self.assertEqual(len(self.calls), 1)
        sid, payload = self.calls[0]
        snap = self.snapshot(created)
        dispatch = snap.tasks[0].accepted_turn_id
        effect = self.app.effects.get(dispatch)
        self.assertEqual(effect["state"], "accepted")
        self.assertEqual(effect["request"]["rootId"], created["rootId"])
        self.assertEqual(payload["clientMessageId"], dispatch)
        self.assertIn("Jev ExecutionPack", payload["_transientContext"])
        self.assertIn("resultSummary is the final user-facing answer", payload["_transientContext"])
        self.assertIn("submit only the natural conversational reply", payload["_transientContext"])
        self.assertIn("完成这项后端测试", payload["_transientContext"])
        self.assertEqual(self.service.room_turns.active_turn(sid)[0], created["rootId"])
        record = self.service.room_partner_dispatches.get(dispatch)
        self.assertEqual(record["workItemId"], created["workItemId"])
        self.assertEqual(record["targetSessionTurnId"], effect["receipt"]["turnId"])

    def test_provider_failure_is_visible_and_not_an_execution(self):
        self.app.driver.controller.decider = JevChoices(
            lambda *_: (_ for _ in ()).throw(TimeoutError())
        )
        created = self.create()
        self.app.tick()
        view = self.service.jev_workspace(self.room["id"], created["graphId"])
        self.assertIn("decision_unavailable", str(view["events"]))
        self.prompt.assert_not_called()

    def test_stop_during_choice_fences_stale_decision(self):
        created = self.create()

        def stop_then_choose(state, questions):
            self.app.stop(self.room["id"], created["rootId"])
            return choose_execution(state, questions)

        self.app.driver.controller.decider = JevChoices(stop_then_choose)
        self.app.tick()
        self.assertEqual(self.snapshot(created).tasks[0].state, "cancelled")
        self.prompt.assert_not_called()

    def test_shared_session_busy_does_not_create_second_pi_turn(self):
        first = self.create()
        self.app.tick()
        second = self.create("second")
        # Pin both responsibilities to the same Session: another qualified
        # participant is otherwise legitimately eligible for reassignment.
        from rag_ime.jev_tasks.types import canonical

        with self.app.ledger.connection(write=True) as conn:
            conn.execute(
                "INSERT INTO agent_jev_task_requirements VALUES(?,?,?)",
                (
                    second["workItemId"],
                    second["graphId"],
                    canonical(
                        {"ownerParticipantId": self.snapshot(first).tasks[0].owner_id}
                    ),
                ),
            )
        self.app.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.snapshot(first).tasks[0].state, "active")

    def test_timeout_stays_unknown_and_never_replays(self):
        created = self.create()
        self.prompt.side_effect = TimeoutError("ambiguous transport")
        self.app.tick()
        dispatch = self.snapshot(created).tasks[0].accepted_turn_id
        self.assertEqual(self.app.effects.get(dispatch)["state"], "unknown")
        calls = self.prompt.call_count
        self.app.recover()
        self.app.tick()
        self.assertEqual(self.prompt.call_count, calls)
        self.assertEqual(self.app.effects.get(dispatch)["state"], "unknown")

    def test_terminal_result_enters_review_and_requires_bound_acceptance(self):
        created = self.create()
        self.app.tick()
        task = self.snapshot(created).tasks[0]
        effect = self.app.effects.get(task.accepted_turn_id)
        record = self.service.room_partner_dispatches.get(task.accepted_turn_id)
        self.service.room_partner_application._settle_dispatch(
            record,
            phase="completed",
            result="可核验结果",
            completion_source="room_post",
        )
        self.assertEqual(self.snapshot(created).tasks[0].state, "review")
        self.assertEqual(self.service.wake_schedules.list(limit=20), [])
        terminal = {
            "eventId": "terminal-1",
            "eventType": "turn_completed",
            "status": "completed",
        }
        with patch.object(self.app, "execution_terminal", return_value=terminal):
            self.app.reconcile_graph(self.app.binding_by_graph(created["graphId"]))
            task = self.snapshot(created).tasks[0]
            result = self.service.jev_command(
                self.room["id"],
                {
                    "action": "accept",
                    "graphId": created["graphId"],
                    "clientMessageId": "review-1",
                    "taskId": task.id,
                    "taskHash": digest(asdict(task)),
                    "reason": "检查证据及要求",
                    "evidenceRefs": ["test:result"],
                    "operabilityVerdict": "passed",
                    "requirementVerdict": "satisfied",
                },
            )
        self.assertEqual(result["task"]["state"], "done")
        self.assertEqual(
            self.app.effects.get(task.accepted_turn_id)["receipt"]["turnId"],
            effect["receipt"]["turnId"],
        )

    def test_stop_before_admission_rejects_and_releases_prepared_slot(self):
        created = self.create()
        original = self.service.room_dispatch.dispatch_prepared

        def stop_first(request, **kwargs):
            self.app.stop(self.room["id"], created["rootId"])
            return original(request, **kwargs)

        with patch.object(
            self.service.room_dispatch, "dispatch_prepared", side_effect=stop_first
        ):
            self.app.tick()
        self.prompt.assert_not_called()
        with self.app.ledger.connection() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_jev_executor_claims"
                ).fetchone()[0],
                0,
            )

    def test_outbox_rolls_back_with_work_owner_transaction(self):
        created = self.create()
        task = self.snapshot(created).tasks[0]
        with self.app.ledger.connection() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM agent_jev_owner_events"
            ).fetchone()[0]
        with patch.object(
            self.service.room_work,
            "_append_event",
            side_effect=RuntimeError("write failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.room_work.reassign(
                    task.id,
                    actor_participant_id=task.owner_id,
                    current_owner_participant_id=self.room["participants"][1]["id"],
                    reason="test",
                )
        with self.app.ledger.connection() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM agent_jev_owner_events").fetchone()[
                    0
                ],
                before,
            )
        self.assertEqual(self.snapshot(created).tasks[0].owner_id, task.owner_id)

    def test_idempotency_conflict_does_not_mutate_root(self):
        created = self.create()
        with self.assertRaises(GraphConflict):
            self.app.create(
                self.room["id"],
                {"clientMessageId": "request-1", "message": "different"},
            )
        self.assertEqual(len(self.snapshot(created).tasks), 1)


if __name__ == "__main__":
    unittest.main()
