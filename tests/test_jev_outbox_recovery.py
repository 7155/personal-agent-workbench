"""Durable recovery against real migrations and canonical WorkItem stores.

Providers and Runtime effects are controlled fixtures; no live Pi is started.
"""
from __future__ import annotations

import json
import multiprocessing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.jev_tasks.controller import JevTaskController
from rag_ime.db.migration_runner import apply_database_migrations, load_migrations
from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.context import build_manifest
from rag_ime.jev_tasks.effects import RuntimeEffects
from rag_ime.jev_tasks.event_queue import DurableEventQueue
from rag_ime.jev_tasks.ledger import GraphLedger
from rag_ime.jev_tasks.owner import execution_dict
from rag_ime.jev_tasks.room_driver import JevRoomDriver
from rag_ime.jev_tasks.types import Candidate
from tests import test_jev_tasks_owner_integration as fixtures


def choose_first(_state, questions):
    choices = questions["decision"]["criteria"]
    selected = next(iter(choices))
    return {"model": "recovery-fixture", "answers": {"decision": {
        "type": "choice", "choice": selected, "confidence": 1.0,
        "probabilities": {key: float(key == selected) for key in choices}}}}


def claim_in_process(path, owner, start, output):
    queue = DurableEventQueue(GraphLedger(path), owner_id=owner)
    start.wait(10)
    claim = queue.claim_next()
    output.put(None if claim is None else claim.event.source_id)


class JevOutboxRecoveryTests(unittest.TestCase):
    setUp = fixtures.JevTasksOwnerIntegrationTests.setUp
    create_work = fixtures.JevTasksOwnerIntegrationTests.create_work
    snapshot = fixtures.JevTasksOwnerIntegrationTests.snapshot
    fact = fixtures.JevTasksOwnerIntegrationTests.fact
    reassign_candidate = fixtures.JevTasksOwnerIntegrationTests.reassign_candidate

    def queue(self, **options):
        self.now = [100000]
        self.ledger.clock_ms = lambda: self.now[0]
        snapshot = self.snapshot()
        with self.ledger.connection(write=True) as conn:
            conn.execute("INSERT INTO agent_jev_host_roots(graph_id,request_hash) VALUES(?,?)",
                         (snapshot.graph_id, "fixture:request"))
        queue = DurableEventQueue(self.ledger, owner_id="worker:first", **options)
        with self.ledger.connection(write=True) as conn:
            queue.enqueue(conn, snapshot.graph_id, "source:durable", "work_created")
        return queue

    def event_row(self):
        with self.ledger.connection() as conn:
            return dict(conn.execute("SELECT * FROM agent_jev_owner_events WHERE source_id='source:durable'").fetchone())

    def test_expired_worker_cannot_ack_reclaimed_event(self):
        queue = self.queue(lease_ms=1000)
        old = queue.claim_next()
        self.assertIsNone(queue.claim_next())
        self.now[0] += 1001
        restarted = DurableEventQueue(self.ledger, owner_id="worker:restarted", lease_ms=1000)
        self.assertEqual(restarted.recover_expired(), 1)
        current = restarted.claim_next()
        self.assertEqual(current.event.source_id, old.event.source_id)
        self.assertEqual(current.generation, old.generation + 1)
        self.assertFalse(queue.finish(old, {"status": "applied"}))
        self.assertTrue(restarted.finish(current, {"status": "applied"}))
        self.assertEqual(self.event_row()["state"], "done")

    def test_two_processes_cannot_claim_the_same_event(self):
        self.queue()
        context = multiprocessing.get_context("spawn")
        start, output = context.Event(), context.Queue()
        workers = [context.Process(target=claim_in_process, args=(str(self.db_path), "worker:" + str(i), start, output))
                   for i in range(2)]
        for worker in workers:
            worker.start()
            self.addCleanup(lambda p=worker: p.terminate() if p.is_alive() else None)
        start.set()
        results = [output.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(10)
            self.assertEqual(worker.exitcode, 0)
        output.close()
        self.assertCountEqual(results, ["source:durable", None])

    def test_host_failure_backoff_and_budget_survive_recreation(self):
        queue = self.queue(max_attempts=2, base_retry_ms=100, max_retry_ms=1000)
        first = queue.claim_next()
        self.assertTrue(queue.finish(first, {"status": "host_error"}))
        row = self.event_row()
        self.assertEqual((row["state"], row["attempt_count"], row["next_retry_at_ms"]), ("retry", 1, 100100))
        self.assertIsNone(queue.claim_next())
        self.now[0] += 100
        restarted = DurableEventQueue(self.ledger, owner_id="worker:restarted")
        second = restarted.claim_next()
        self.assertTrue(restarted.finish(second, {"status": "decision_unavailable"}))
        self.assertEqual(self.event_row()["state"], "blocked")
        self.assertEqual(self.event_row()["wake_condition"], "owner_event")
        self.now[0] += 1000000
        self.assertIsNone(restarted.claim_next())
        self.assertEqual(restarted.wake(first.event.graph_id, condition="owner_event"), 1)
        self.assertEqual(restarted.claim_next().attempt_count, 3)

    def test_unknown_effect_is_reconciled_instead_of_acked(self):
        queue = self.queue()
        claim = queue.claim_next()
        queue.finish(claim, {"status": "applied", "effects": [{"state": "unknown", "effectId": "exact:1"}]})
        row = self.event_row()
        self.assertEqual(row["state"], "reconcile")
        self.assertEqual(row["wake_condition"], "exact_receipt_available")
        self.assertIsNone(queue.claim_next())

    def test_stopped_root_never_recovers_processing_work(self):
        queue = self.queue(lease_ms=1000)
        claim = queue.claim_next()
        with self.ledger.connection(write=True) as conn:
            conn.execute("UPDATE agent_jev_host_roots SET stopped=1 WHERE graph_id=?", (claim.event.graph_id,))
        self.now[0] += 1001
        self.assertEqual(queue.recover_expired(), 0)
        self.assertIsNone(queue.claim_next())

    def test_configuration_failure_waits_for_explicit_configuration_wake(self):
        queue = self.queue()
        claim = queue.claim_next()
        queue.finish(claim, {"status": "configuration_missing"})
        self.assertEqual(self.event_row()["state"], "blocked")
        self.assertEqual(queue.wake(claim.event.graph_id, condition="owner_event"), 0)
        self.assertIsNone(queue.claim_next())
        self.assertEqual(queue.wake(claim.event.graph_id), 1)
        self.assertIsNotNone(queue.claim_next())

    def test_transient_provider_failure_can_make_a_bounded_new_decision(self):
        calls = []

        def temporary_failure(state, questions):
            calls.append(state)
            if len(calls) == 1:
                raise TimeoutError("temporary fixture failure")
            return choose_first(state, questions)

        controller = JevTaskController(self.ledger, self.owner, JevChoices(temporary_failure))
        before = self.snapshot()
        candidate = self.reassign_candidate()
        failed = controller.handle_event(before, event_id="source:stable", state={"revision": 1},
                                         candidates=[candidate])
        self.assertEqual(failed["status"], "decision_unavailable")
        self.assertIsNone(controller.event_receipt(before.graph_id, before.controller_id, "source:stable"))
        applied = controller.handle_event(self.snapshot(), event_id="source:stable", state={"revision": 2},
                                          candidates=[candidate])
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(len(calls), 2)
        with self.ledger.connection() as conn:
            history = conn.execute(
                "SELECT generation,input_hash,observation_json,phase FROM agent_jev_decision_generations "
                "WHERE event_id='source:stable' ORDER BY generation").fetchall()
        self.assertEqual([row["generation"] for row in history], [1, 2])
        self.assertNotEqual(history[0]["input_hash"], history[1]["input_hash"])
        self.assertEqual(json.loads(history[0]["observation_json"]), {"revision": 1})
        self.assertEqual([row["phase"] for row in history], ["failed", "applied"])

    def test_committed_owner_is_recovered_after_death_before_journal_ack(self):
        decider = JevChoices(choose_first)
        controller = JevTaskController(self.ledger, self.owner, decider)
        before, action = self.snapshot(), self.reassign_candidate()
        apply = self.owner.apply

        def die_after_commit(*args, **kwargs):
            apply(*args, **kwargs)
            raise SystemExit("fixture crash after owner commit")

        with patch.object(self.owner, "apply", side_effect=die_after_commit):
            with self.assertRaises(SystemExit):
                controller.handle_event(before, event_id="source:commit", state={}, candidates=[action])
        with patch.object(decider, "choose_action", side_effect=AssertionError("must not call Provider")):
            receipt = controller.event_receipt(before.graph_id, before.controller_id, "source:commit")
            replay = controller.handle_event(self.snapshot(), event_id="source:commit", state={"fresh": True}, candidates=[])
        self.assertEqual(receipt["status"], "applied")
        self.assertTrue(receipt["recoveredFromOwnerReceipt"])
        self.assertTrue(replay["replayed"])
        with self.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_commands").fetchone()[0], 1)

    def test_choice_before_crash_reuses_only_identical_frozen_observation(self):
        controller = JevTaskController(self.ledger, self.owner, JevChoices(choose_first))
        before, action = self.snapshot(), self.reassign_candidate()
        with patch.object(self.owner, "apply", side_effect=SystemExit("fixture crash before apply")):
            with self.assertRaises(SystemExit):
                controller.handle_event(before, event_id="source:choice", state={"stable": True}, candidates=[action])
        with patch.object(controller.decider, "choose_action", side_effect=AssertionError("must reuse frozen choice")):
            result = controller.handle_event(before, event_id="source:choice", state={"stable": True}, candidates=[action])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["generation"], 1)

    def test_expired_evaluation_uses_new_generation_and_fresh_input_hash(self):
        self.queue()
        controller = JevTaskController(self.ledger, self.owner, JevChoices(choose_first), decision_lease_ms=1000)
        before, action = self.snapshot(), self.reassign_candidate()
        with patch.object(controller.decider, "choose_action", side_effect=SystemExit("fixture lost evaluation")):
            with self.assertRaises(SystemExit):
                controller.handle_event(before, event_id="source:expired", state={"v": 1}, candidates=[action])
        self.assertEqual(controller.event_receipt(before.graph_id, before.controller_id, "source:expired")["status"], "pending")
        self.now[0] += 1001
        result = controller.handle_event(before, event_id="source:expired", state={"v": 2}, candidates=[action])
        self.assertEqual((result["status"], result["generation"]), ("applied", 2))
        with self.ledger.connection() as conn:
            rows = conn.execute("SELECT input_hash,observation_json,phase FROM agent_jev_decision_generations ORDER BY generation").fetchall()
        self.assertNotEqual(rows[0]["input_hash"], rows[1]["input_hash"])
        self.assertEqual(rows[0]["phase"], "superseded")
        self.assertEqual(json.loads(rows[0]["observation_json"]), {"v": 1})

    def test_late_decided_worker_cannot_commit_after_new_generation_noop(self):
        first = JevTaskController(self.ledger, self.owner, JevChoices(choose_first))
        second = JevTaskController(self.ledger, self.owner, JevChoices(choose_first))
        before, action = self.snapshot(), self.reassign_candidate()
        wait = Candidate.make("wait", before.root_work_id, "fixture wait", {})
        original_apply = self.owner.apply

        def newer_decision_then_old_apply(*args, **kwargs):
            second.handle_event(before, event_id="source:race", state={"v": 2}, candidates=[wait])
            return original_apply(*args, **kwargs)

        with patch.object(self.owner, "apply", side_effect=newer_decision_then_old_apply):
            first.handle_event(before, event_id="source:race", state={"v": 1}, candidates=[action])
        self.assertEqual(self.work.get(self.child["id"])["currentOwnerParticipantId"], self.a["id"])
        with self.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_commands").fetchone()[0], 0)

    def test_no_progress_budget_counts_paid_generations_and_noop_never_calls_provider(self):
        controller = JevTaskController(self.ledger, self.owner, JevChoices(lambda *_: (_ for _ in ()).throw(TimeoutError())),
                                       max_decisions_per_snapshot=2)
        before, action = self.snapshot(), self.reassign_candidate()
        for _ in range(2):
            self.assertEqual(controller.handle_event(before, event_id="source:budget", state={}, candidates=[action])["status"],
                             "decision_unavailable")
        self.assertEqual(controller.handle_event(before, event_id="source:budget", state={}, candidates=[action])["status"],
                         "no_progress_budget")
        with patch.object(controller.decider, "choose_action", side_effect=AssertionError("empty frontier costs no Provider call")):
            controller.record_noop(before, event_id="source:empty", status="waiting")
            replay = controller.record_noop(before, event_id="source:empty", status="waiting")
        self.assertTrue(replay["replayed"])
        with self.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT SUM(model_call) FROM agent_jev_decision_generations").fetchone()[0], 2)

    def test_prepared_recovery_resumes_exact_effect_then_only_looks_up_unknown(self):
        queue = self.queue()
        event = queue.claim_next().event
        before = self.snapshot()
        task = before.task(self.child["id"])
        action = Candidate.make("claim_dispatch", task.id, "fixture authorized dispatch", {
            "dispatchId": "effect:stable", "contextManifest": build_manifest(task.id, task.revision, [], {}).for_executor(),
            "execution": execution_dict(self.fact(task.id))})
        controller = JevTaskController(self.ledger, self.owner, JevChoices(choose_first))
        committed = controller.handle_event(before, event_id=event.journal_key, state={}, candidates=[action])
        self.assertEqual(committed["status"], "applied")
        sent, looked_up = [], []

        def perform(_operation, request):
            sent.append(request["dispatchId"])
            return {"state": "unknown"}

        def lookup(_operation, request):
            looked_up.append(request["dispatchId"])
            return {"state": "accepted", "receiptId": "receipt:exact", "dispatchId": request["dispatchId"],
                    "taskId": request["taskId"], "sessionId": request["sessionId"], "turnId": "turn:exact"}

        effects = RuntimeEffects(self.ledger, perform, lookup, lambda *_: None)
        driver = JevRoomDriver(ledger=self.ledger, controller=controller, effects=effects,
            observe=lambda *_: (_ for _ in ()).throw(AssertionError("committed event must not observe again")), enabled=True)
        first = driver.handle(event)
        self.assertEqual(first["effects"][0]["state"], "unknown")
        barrier = controller.event_receipt(event.graph_id, event.controller_id, "source:different")
        self.assertEqual(barrier["status"], "reconciliation_required")
        second = driver.handle(event)
        self.assertEqual(second["effects"][0]["state"], "accepted")
        driver.handle(event)
        self.assertEqual(sent, ["effect:stable"])
        self.assertEqual(looked_up, ["effect:stable"])

    def test_fair_claim_rotates_between_graphs_with_backlogs(self):
        queue = self.queue()
        before = self.snapshot()
        other_root = self.work.create(room_id=self.room["id"], objective="other root", expected_output="fixture",
            acceptance_criteria=["fixture"], current_owner_participant_id=self.lead["id"],
            created_by_participant_id=self.lead["id"], accountable_participant_id=self.lead["id"],
            client_message_id="fixture:second-root", root_turn_id="root:other")
        self.ledger.register_created_root(graph_id="graph:other", room_id=self.room["id"], root_id="root:other",
            root_work_id=other_root["id"], controller_id="controller:other", participant_id=self.lead["id"],
            session_id=self.lead["sessionId"])
        with self.ledger.connection(write=True) as conn:
            conn.execute("INSERT INTO agent_jev_host_roots(graph_id,request_hash) VALUES('graph:other','fixture')")
            queue.enqueue(conn, before.graph_id, "source:backlog", "work_created")
            queue.enqueue(conn, "graph:other", "source:other", "work_created")
        first = queue.claim_next()
        queue.finish(first, {"status": "applied"})
        second = queue.claim_next()
        self.assertNotEqual(first.event.graph_id, second.event.graph_id)


class JevOutboxUpgradeTests(unittest.TestCase):
    create_work = fixtures.JevTasksOwnerIntegrationTests.create_work
    snapshot = fixtures.JevTasksOwnerIntegrationTests.snapshot

    def setUp(self):
        old = tuple(migration for migration in load_migrations() if migration.version <= 208)
        with patch("rag_ime.db.migration_runner.load_migrations", return_value=old):
            self.tmp = tempfile.TemporaryDirectory(prefix="paw-jev-upgrade-")
            self.addCleanup(self.tmp.cleanup)
            self.root = Path(self.tmp.name)
            self.db_path = self.root / "acceptance.sqlite"
            self.sessions = fixtures.AgentSessionStore(self.db_path)
            self.sessions.initialize()
            self.session_records = [self.sessions.create(title="Jev 旧库 " + name)
                                    for name in ("责任身份", "执行者 A", "执行者 B")]
            self.rooms = fixtures.AgentRoomStore(self.db_path, room_dir=self.root / "rooms")
            self.rooms.initialize()
            self.room = self.rooms.create(title="旧 Jev Root", routing_policy="moderator", participants=[
                {"sessionId": record["id"], "roleId": role, "roleVersion": "1",
                 "displayName": record["title"], "collaborationRole": role}
                for record, role in zip(self.session_records,
                                        ("coordinator", "implementer", "researcher"), strict=True)])
            self.lead, self.a, self.b = self.room["participants"]
            self.work = fixtures.AgentRoomWorkStore(self.db_path)
            self.work.initialize()
            self.root_work = self.create_work("root", self.lead)
            self.child = self.create_work("child", self.a, parent=self.root_work["id"])
            self.second = self.create_work("second", self.b, parent=self.root_work["id"])
            self.ledger = GraphLedger(self.db_path)
            # Seed through the 0207 graph columns. The current ledger's
            # snapshot reader intentionally requires migration 0214.
            with self.ledger.connection(write=True) as conn:
                conn.execute(
                    "INSERT INTO agent_jev_graphs "
                    "(graph_id,room_id,root_turn_id,root_work_id,controller_id,"
                    "controller_participant_id,controller_session_id,mode,topology_revision,created_at_ms) "
                    "VALUES(?,?,?,?,?,?,?,'jev',0,?)",
                    ("jev-graph:isolated-test", self.room["id"], "jev-root:isolated-test",
                     self.root_work["id"], "jev-controller:test", self.lead["id"],
                     self.lead["sessionId"], 100),
                )

    def test_upgrade_recovers_old_failure_and_interruption_without_rewriting_history(self):
        graph_id, old_snapshot_hash = "jev-graph:isolated-test", "legacy:snapshot:208"
        with self.ledger.connection(write=True) as conn:
            for source, state, result in (("failed", "done", {"status": "host_error"}),
                                           ("interrupted", "interrupted", {}),
                                           ("success", "done", {"status": "waiting"})):
                conn.execute("INSERT INTO agent_jev_owner_events VALUES(?,?,?,?,?,?)",
                             (source, graph_id, "work_created", state, json.dumps(result), 100))
            conn.execute("INSERT INTO agent_jev_decisions VALUES(?,?,?,?,0,0,'applied','',?,'legacy:policy',100)",
                         (graph_id, "source:policy", "legacy:hash", old_snapshot_hash,
                          json.dumps({"status": "decision_external_not_allowed"})))
            checksums = list(conn.execute("SELECT version,checksum FROM schema_migrations ORDER BY version"))
            old_results = dict(conn.execute("SELECT source_id,result_json FROM agent_jev_owner_events"))
        with sqlite3.connect(self.db_path) as conn:
            upgraded = apply_database_migrations(conn)
            replay = apply_database_migrations(conn)
            self.assertIn(209, upgraded.applied_versions)
            self.assertEqual(replay.applied_versions, ())
            self.assertEqual(conn.execute("SELECT version,checksum FROM schema_migrations WHERE version<=208 ORDER BY version").fetchall(),
                             [tuple(row) for row in checksums])
            states = dict(conn.execute("SELECT source_id,state FROM agent_jev_owner_events"))
            self.assertEqual(states, {"failed": "retry", "interrupted": "retry", "success": "done"})
            self.assertEqual(conn.execute("SELECT phase FROM agent_jev_decisions WHERE event_id='source:policy'").fetchone()[0], "failed")
            self.assertEqual(conn.execute("SELECT input_hash FROM agent_jev_decision_generations").fetchone()[0], "legacy:hash")
            self.assertEqual(conn.execute("SELECT snapshot_hash FROM agent_jev_decision_generations").fetchone()[0], old_snapshot_hash)
            self.assertEqual(dict(conn.execute("SELECT source_id,result_json FROM agent_jev_owner_events")), old_results)
        fresh = self.snapshot()
        self.assertEqual(fresh.graph_id, graph_id)
        self.assertEqual(fresh.root_work_id, self.root_work["id"])
        self.assertEqual({task.id for task in fresh.active_tasks},
                         {self.root_work["id"], self.child["id"], self.second["id"]})


if __name__ == "__main__":
    unittest.main()
