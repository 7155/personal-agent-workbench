"""Completion Kit JEV-008/071/073 fault windows on real isolated owners.

Pi prompt admission, finite choice and terminal proof are fixtures. Room event
storage, Jev transactions, publication, Stop and recovery remain real.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from rag_ime.agent_tools import ControlToolGateway
from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.types import GraphConflict, digest
from tests import test_jev_failure_lifecycle as failure
from tests import test_jev_host_application as host


class JevPublicationRecoveryTests(host.JevHostFixture):
    effects = failure.JevFailureLifecycleTests.effects
    active_effect = failure.JevFailureLifecycleTests.active_effect
    submit = failure.JevFailureLifecycleTests.submit
    finish = failure.JevFailureLifecycleTests.finish
    planned = failure.JevFailureLifecycleTests.planned
    result = staticmethod(failure.JevFailureLifecycleTests.result)
    verdict = staticmethod(failure.JevFailureLifecycleTests.verdict)

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
        self.app.driver.controller.decider = JevChoices(failure.choose_valid_progress)
        self.terminals = {}
        original = self.app.execution_terminal
        terminal = patch.object(self.app, "execution_terminal", side_effect=lambda effect, **kwargs:
            self.terminals.get(effect["effectId"]) or original(effect, **kwargs))
        terminal.start()
        self.addCleanup(terminal.stop)

    def root_events(self, root_id, event_type):
        return [event for event in self.service.rooms.list_events(self.room["id"])
                if event["turnId"] == root_id and event["eventType"] == event_type]

    def prepared_synthesizer(self):
        created = self.planned(dependent=True)
        for label in ("a", "b"):
            worker = self.active_effect(created, "execute")
            self.submit(worker, "result_submit", self.result(label))
            self.finish(worker)
            verifier = self.active_effect(created, "verify")
            self.submit(verifier, "verification_submit", self.verdict())
            self.finish(verifier)
        return created, self.active_effect(created, "synthesize")

    def test_root_commit_survives_public_input_failure_and_recovers_one_message(self):
        payload = {"clientMessageId": "publication-root", "message": "Isolated Root",
                   "strategy": "direct", "modelRouting": "participant"}
        graph_id = "jev-graph:" + digest([self.room["id"], payload["clientMessageId"]])[:40]
        original = self.service.room_events.publish_projection

        def fail_input(*, projection_key, **values):
            if projection_key == "jev-input:" + graph_id:
                raise RuntimeError("public input store unavailable after Root commit")
            return original(projection_key=projection_key, **values)

        with patch.object(self.service.room_events, "publish_projection", side_effect=fail_input):
            with self.assertRaisesRegex(RuntimeError, "public input store unavailable"):
                self.app.create(self.room["id"], payload)
        with self.app.ledger.connection() as conn:
            graph = conn.execute("SELECT root_turn_id,root_work_id FROM agent_jev_graphs WHERE graph_id=?",
                                 (graph_id,)).fetchone()
            self.assertIsNotNone(graph)
            root_id, root_work_id = graph
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_graphs WHERE graph_id=?",
                                          (graph_id,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_owner_events WHERE graph_id=?",
                                          (graph_id,)).fetchone()[0], 1)
        self.assertEqual(self.root_events(root_id, "user_message"), [])

        self.app.recover()  # Startup recovery republishes from the committed Root.
        messages = self.root_events(root_id, "user_message")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"]["graphId"], graph_id)
        self.assertEqual(messages[0]["payload"]["clientMessageId"], payload["clientMessageId"])
        replay = self.app.create(self.room["id"], payload)
        self.assertEqual((replay["graphId"], replay["rootId"], replay["workItemId"]),
                         (graph_id, root_id, root_work_id))
        self.app.recover()
        self.assertEqual(len(self.root_events(root_id, "user_message")), 1)
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_graphs WHERE graph_id=?",
                                          (graph_id,)).fetchone()[0], 1)
        self.prompt.assert_not_called()

    def test_ambiguous_input_publish_after_append_does_not_duplicate_on_recovery(self):
        payload = {"clientMessageId": "publication-ambiguous", "message": "Isolated Root",
                   "strategy": "direct", "modelRouting": "participant"}
        graph_id = "jev-graph:" + digest([self.room["id"], payload["clientMessageId"]])[:40]
        original = self.service.room_events.publish_projection

        def append_then_exit(*, projection_key, **values):
            event = original(projection_key=projection_key, **values)
            if projection_key == "jev-input:" + graph_id:
                raise SystemExit("injected exit after durable public append")
            return event

        with patch.object(self.service.room_events, "publish_projection", side_effect=append_then_exit):
            with self.assertRaisesRegex(SystemExit, "after durable public append"):
                self.app.create(self.room["id"], payload)
        replay = self.app.create(self.room["id"], payload)
        self.app.recover()
        self.assertEqual(len(self.root_events(replay["rootId"], "user_message")), 1)
        self.assertEqual(replay["graphId"], graph_id)

    def test_final_publication_exit_after_post_recovers_one_terminal(self):
        created, synthesizer = self.prepared_synthesizer()
        self.submit(synthesizer, "final_submit", {
            "content": "两项成果均已独立验收。", "evidenceRefs": ["test:a", "test:b"],
        })
        finalization_id = "jev-final:" + created["graphId"]
        original = self.service.room_events.publish_projection

        def exit_before_terminal(*, projection_key, **values):
            if projection_key == finalization_id + ":terminal":
                raise SystemExit("injected exit between final post and terminal")
            return original(projection_key=projection_key, **values)

        with patch.object(self.service.room_events, "publish_projection", side_effect=exit_before_terminal):
            with self.assertRaisesRegex(SystemExit, "between final post and terminal"):
                self.finish(synthesizer)
        persisted = self.app.projection(self.room["id"], created["graphId"])["final"]
        self.assertEqual((persisted["status"], persisted["finalizationId"]),
                         ("completed", finalization_id))
        posts = self.root_events(created["rootId"], "room_post")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["payload"]["post"]["postId"], finalization_id)
        self.assertEqual(self.root_events(created["rootId"], "turn_completed"), [])

        self.app.recover()
        self.app.tick(limit=16)
        self.app.recover()
        self.assertEqual(len(self.root_events(created["rootId"], "room_post")), 1)
        terminal = self.root_events(created["rootId"], "turn_completed")
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["payload"]["finalizationId"], finalization_id)
        self.assertEqual(self.root_events(created["rootId"], "turn_failed"), [])

    def test_stop_during_synthesis_fences_submitted_and_late_final(self):
        created, synthesizer = self.prepared_synthesizer()
        output = {"content": "这条迟到成功消息不应发布。", "evidenceRefs": ["test:late-final"]}
        self.submit(synthesizer, "final_submit", output)
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM agent_jev_execution_outputs WHERE dispatch_id=?",
                (synthesizer["effectId"],)).fetchone()[0], 1)
        self.app.stop(self.room["id"], created["rootId"])
        with self.assertRaises(GraphConflict):
            self.app.lifecycle.submit(synthesizer["request"]["sessionId"], "final_submit", output,
                                      tool_call_id="late-final")
        self.finish(synthesizer)  # Late exact Pi terminal after Stop.
        self.app.recover()
        self.app.tick(limit=16)
        view = self.app.projection(self.room["id"], created["graphId"])
        self.assertTrue(view["stopped"])
        self.assertNotEqual(view["final"].get("status"), "completed")
        self.assertEqual(self.root_events(created["rootId"], "room_post"), [])
        terminals = self.root_events(created["rootId"], "turn_completed")
        self.assertTrue(terminals)
        self.assertTrue(all(event["payload"].get("status") == "aborted"
                            and "finalizationId" not in event["payload"]
                            for event in terminals))
        with self.app.ledger.connection() as conn:
            self.assertEqual(json.loads(conn.execute(
                "SELECT final_json FROM agent_jev_host_roots WHERE graph_id=?",
                (created["graphId"],)).fetchone()[0]), {})


if __name__ == "__main__":
    import unittest
    unittest.main()
