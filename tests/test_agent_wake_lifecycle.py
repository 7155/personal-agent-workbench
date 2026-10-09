from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.agent_wake_scheduler import AgentWakeScheduleStore, AgentWakeScheduler
from rag_ime.pi.config import PiRuntimeConfig
from tests.sqlite_fixtures import copy_current_database


class AgentWakeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-wake-lifecycle-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "fixture.sqlite"
        copy_current_database(self.db)

    def service(self, **kwargs) -> AgentService:
        service = AgentService(
            db_path=self.db,
            runtime_config=PiRuntimeConfig(
                enabled=False, executable=None, agent_dir=self.root / "config",
                session_dir=self.root / "sessions", logs_dir=self.root / "logs",
            ),
            **kwargs,
        )
        self.addCleanup(service.close)
        return service

    def due_schedule(self, store: AgentWakeScheduleStore) -> dict[str, object]:
        created_at = int(time.time() * 1000) - 2_000
        return store.create({
            "title": "Lifecycle fixture", "instruction": "Fixture only",
            "targetType": "session", "targetSessionId": "fixture-session",
            "wakeAtMs": created_at + 1_000,
        }, now_ms=created_at)

    def test_constructor_is_passive_and_explicit_start_dispatches_original_due_run(self):
        store = AgentWakeScheduleStore(self.db)
        schedule = self.due_schedule(store)
        dispatched = Event()
        claims: list[dict[str, object]] = []

        def dispatch(claim):
            claims.append(dict(claim))
            dispatched.set()

        scheduler = AgentWakeScheduler(store=store, dispatch=dispatch, poll_seconds=0.1)
        self.addCleanup(scheduler.close)
        self.assertFalse(dispatched.wait(0.25), "constructor dispatched before its owner was ready")
        self.assertEqual(store.runs(str(schedule["id"])), [])
        scheduler.start()
        scheduler.start()  # The owner's retry must not create another poller.
        self.assertTrue(dispatched.wait(2))
        scheduler.close()
        self.assertEqual(len(claims), 1)
        self.assertEqual(len(store.runs(str(schedule["id"]))), 1)
        self.assertEqual(claims[0]["runId"], store.runs(str(schedule["id"]))[0]["id"])

    def test_closed_scheduler_claims_nothing_even_through_explicit_manual_tick(self):
        store = AgentWakeScheduleStore(self.db)
        schedule = self.due_schedule(store)
        scheduler = AgentWakeScheduler(store=store, dispatch=lambda _: self.fail("closed dispatch"), enabled=False)
        scheduler.close()
        self.assertEqual(scheduler.run_due_once(), 0)
        scheduler.start()
        self.assertEqual(store.runs(str(schedule["id"])), [])

    def test_deferred_and_failed_recovery_keep_automatic_maintenance_inactive(self):
        service = self.service(wake_scheduler_enabled=True, wake_scheduler_poll_seconds=0.1,
                               defer_startup_recovery=True)
        observed = Event()
        statuses: list[str] = []

        def tick(_):
            statuses.append(str(service.startup_recovery_status()["status"]))
            self.assertTrue(hasattr(service, "room_partner_application"))
            self.assertTrue(hasattr(service, "jev_application"))
            observed.set()

        service.wake_scheduler.on_tick = tick
        service.wake_scheduler.wake()
        self.assertFalse(observed.wait(0.25), "pending recovery already ran maintenance")
        with patch.object(service.jev_application, "recover", side_effect=ValueError("fixture recovery failure")):
            with self.assertRaisesRegex(ValueError, "fixture recovery failure"):
                service.run_startup_recovery()
        service.wake_scheduler.wake()
        self.assertFalse(observed.wait(0.25), "failed recovery already ran maintenance")
        self.assertEqual(service.startup_recovery_status()["status"], "failed")
        service.run_startup_recovery()
        service.wake_scheduler.wake()
        self.assertTrue(observed.wait(2))
        service.close()
        self.assertTrue(statuses and set(statuses) == {"complete"})

    def test_non_execution_owner_does_not_start_automatic_maintenance(self):
        service = self.service(wake_scheduler_enabled=True, wake_scheduler_poll_seconds=0.1,
                               startup_recovery_enabled=False, background_job_execution_owner=False)
        observed = Event()
        service.wake_scheduler.on_tick = lambda _: observed.set()
        service.run_startup_recovery()
        service.wake_scheduler.wake()
        self.assertFalse(observed.wait(0.25), "borrowed service ran execution-owner maintenance")

    def test_close_drains_maintenance_before_releasing_runtime_and_stores(self):
        service = self.service(wake_scheduler_enabled=False)
        entered, release, closed = Event(), Event(), Event()
        order: list[str] = []

        def maintenance(_):
            entered.set()
            if not release.wait(3):
                raise AssertionError("fixture maintenance was not released")
            with service.sessions._read_connect() as conn:
                conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()
            order.append("maintenance drained")

        service.wake_scheduler.on_tick = maintenance
        service.wake_scheduler.run_due_once()
        self.assertTrue(entered.wait(2))

        def close():
            service.close()
            closed.set()

        with patch.object(service.runtime, "stop", side_effect=lambda: order.append("Runtime released")):
            closer = Thread(target=close)
            closer.start()
            try:
                self.assertFalse(closed.wait(0.15))
                self.assertNotIn("Runtime released", order, "Runtime closed while maintenance was still using it")
                self.assertEqual(service.wake_scheduler.run_due_once(), 0)
            finally:
                release.set()
                closer.join(timeout=3)
            self.assertTrue(closed.is_set())
        self.assertEqual(order, ["maintenance drained", "Runtime released"])

    def test_close_is_idempotent_at_the_runtime_owner_boundary(self):
        service = self.service()
        with patch.object(service.runtime, "stop") as stop:
            service.close()
            service.close()
            self.assertEqual(stop.call_count, 1)

    def test_failed_poller_start_is_retryable_and_close_is_safe_without_a_started_thread(self):
        store = AgentWakeScheduleStore(self.db)
        scheduler = AgentWakeScheduler(store=store, dispatch=lambda _: None)
        with patch("rag_ime.agent_wake_scheduler.Thread.start", side_effect=RuntimeError("fixture OS thread failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture OS thread failure"):
                scheduler.start()
        self.assertFalse(scheduler.active)
        scheduler.start()
        self.assertTrue(scheduler.active)
        scheduler.close()
        self.assertFalse(scheduler.active)

        another = AgentWakeScheduler(store=store, dispatch=lambda _: None)
        with patch("rag_ime.agent_wake_scheduler.Thread.start", side_effect=RuntimeError("fixture OS thread failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture OS thread failure"):
                another.start()
        another.close()

    def test_failed_poller_start_is_reported_by_recovery_and_can_be_retried(self):
        service = self.service(wake_scheduler_enabled=True, defer_startup_recovery=True)
        with patch("rag_ime.agent_wake_scheduler.Thread.start", side_effect=RuntimeError("fixture OS thread failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture OS thread failure"):
                service.run_startup_recovery()
        self.assertEqual(service.startup_recovery_status()["status"], "failed")
        self.assertFalse(service.startup_recovery_status()["ok"])
        self.assertFalse(service.wake_scheduler.active)
        service.run_startup_recovery()
        self.assertEqual(service.startup_recovery_status()["status"], "complete")
        self.assertTrue(service.wake_scheduler.active)

    def test_shutdown_defers_only_queued_unentered_dispatch_and_keeps_admitted_work(self):
        store = AgentWakeScheduleStore(self.db)
        schedules = [self.due_schedule(store), self.due_schedule(store)]
        maintenance_entered, dispatch_entered, release = Event(), Event(), Event()
        dispatched: list[str] = []

        def maintenance(_):
            maintenance_entered.set()
            if not release.wait(3):
                raise AssertionError("fixture maintenance was not released")

        def dispatch(claim):
            dispatched.append(str(claim["runId"]))
            dispatch_entered.set()
            if not release.wait(3):
                raise AssertionError("fixture dispatch was not released")
            store.accept(str(claim["runId"]), session_id="fixture-session", turn_id="original-turn")

        scheduler = AgentWakeScheduler(store=store, dispatch=dispatch, on_tick=maintenance,
                                       enabled=False, max_parallel=2)
        self.addCleanup(scheduler.close)
        self.assertEqual(scheduler.run_due_once(), 2)
        self.assertTrue(maintenance_entered.wait(2))
        self.assertTrue(dispatch_entered.wait(2))
        closer = Thread(target=scheduler.close)
        closer.start()
        try:
            self.assertTrue(scheduler._stop.wait(2))
        finally:
            release.set()
            closer.join(timeout=3)
        self.assertFalse(closer.is_alive())
        self.assertEqual(len(dispatched), 1)
        runs = [store.runs(str(schedule["id"]))[0] for schedule in schedules]
        self.assertEqual(sorted(run["state"] for run in runs), ["accepted", "deferred"])
        admitted = next(run for run in runs if run["state"] == "accepted")
        self.assertEqual(admitted["id"], dispatched[0])
        self.assertEqual(admitted["turnId"], "original-turn")
        self.assertEqual(next(run for run in runs if run["state"] == "deferred")["turnId"], "")
