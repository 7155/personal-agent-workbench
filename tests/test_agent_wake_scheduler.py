from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rag_ime.agent_protocol import AgentEventEnvelope
from rag_ime.agent_wake_scheduler import AgentWakeScheduleStore, AgentWakeScheduler


class AgentWakeScheduleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-agent-wake-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.store = AgentWakeScheduleStore(self.db_path)
        self.store.initialize()
        self.now = 1_800_000_000_000

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_edit_preserves_identity_and_paused_state_and_rejects_running(self) -> None:
        schedule = self._create()
        identifier = str(schedule["id"])
        self.store.action(identifier, "pause", now_ms=self.now)
        definition = {**schedule, "title": "Updated PR check", "wakeAtMs": self.now + 60_000}
        edited = self.store.update(identifier, definition, now_ms=self.now)
        self.assertEqual(edited["id"], identifier)
        self.assertEqual(edited["status"], "paused")
        self.assertEqual(edited["title"], "Updated PR check")
        self.assertEqual(self.store.claim_due(now_ms=self.now + 70_000), [])
        self.store.action(identifier, "resume", now_ms=self.now)
        claim = self.store.claim_due(now_ms=self.now + 60_000)[0]
        with self.assertRaisesRegex(ValueError, "can be edited"):
            self.store.update(identifier, {**definition, "wakeAtMs": self.now + 90_000}, now_ms=self.now + 60_000)
        self.assertEqual(self.store.runs(identifier)[0]["id"], claim["runId"])
        self.assertEqual(self.store.get(identifier)["status"], "running")

    def test_one_shot_run_completes_only_after_terminal_agent_event(self) -> None:
        schedule = self._create()
        claim = self.store.claim_due(now_ms=self.now + 1_000)[0]
        self.store.accept(
            str(claim["runId"]),
            session_id="session:target",
            turn_id="turn:scheduled",
            now_ms=self.now + 1_100,
        )

        accepted = self.store.get(str(schedule["id"]))
        self.assertEqual(accepted["status"], "running")
        self.assertEqual(accepted["latestRun"]["state"], "accepted")

        completed = self.store.finish_event(
            self._event("turn_completed", created_at_ms=self.now + 1_200)
        )

        self.assertTrue(completed)
        final = self.store.get(str(schedule["id"]))
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["latestRun"]["state"], "completed")
        self.assertEqual(final["nextWakeAtMs"], 0)

    def test_scheduler_observer_receives_terminal_schedule_after_run_is_immutable(
        self,
    ) -> None:
        schedule = self._create()
        claim = self.store.claim_due(now_ms=self.now + 1_000)[0]
        self.store.accept(
            str(claim["runId"]),
            session_id="session:target",
            turn_id="turn:scheduled",
            now_ms=self.now + 1_100,
        )
        observed: list[tuple[str, dict[str, object]]] = []
        scheduler = AgentWakeScheduler(
            store=self.store,
            dispatch=lambda _claim: None,
            enabled=False,
        )
        scheduler.bind_terminal_observer(
            lambda event, terminal_schedule: observed.append(
                (event.event_type, dict(terminal_schedule))
            )
        )

        scheduler.observe_event(
            AgentEventEnvelope(
                event_id="event:runtime-host-exit",
                session_id="session:target",
                turn_id="turn:scheduled",
                sequence=1,
                created_at_ms=self.now + 1_200,
                event_type="turn_failed",
                payload={
                    "error": "Pi Runtime Host exited",
                    "failureKind": "runtime_host_exit",
                    "exitCode": 1,
                },
                resume_token="1",
            )
        )

        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], "turn_failed")
        self.assertEqual(observed[0][1]["id"], schedule["id"])
        self.assertEqual(observed[0][1]["status"], "failed")
        self.assertEqual(
            observed[0][1]["latestRun"]["state"],
            "failed",
        )
        self.assertEqual(
            observed[0][1]["latestRun"]["result"]["failureKind"],
            "runtime_host_exit",
        )
        self.assertEqual(
            self.store.runs(str(schedule["id"]))[0]["id"],
            claim["runId"],
        )
        self.assertEqual(
            self.store.runs(str(schedule["id"]))[0]["result"]["failureKind"],
            "runtime_host_exit",
        )
        scheduler.close()

    def test_daily_schedule_reschedules_until_max_runs(self) -> None:
        schedule = self._create(recurrence_kind="daily", max_runs=2)
        first = self.store.claim_due(now_ms=self.now + 1_000)[0]
        self.store.accept(
            str(first["runId"]),
            session_id="session:target",
            turn_id="turn:scheduled",
            now_ms=self.now + 1_100,
        )
        self.store.finish_event(self._event("turn_completed", created_at_ms=self.now + 1_200))

        next_schedule = self.store.get(str(schedule["id"]))
        self.assertEqual(next_schedule["status"], "scheduled")
        self.assertEqual(next_schedule["runCount"], 1)
        self.assertEqual(next_schedule["nextWakeAtMs"], self.now + 1_000 + 86_400_000)

        second_due = int(next_schedule["nextWakeAtMs"])
        second = self.store.claim_due(now_ms=second_due)[0]
        self.store.accept(
            str(second["runId"]),
            session_id="session:target",
            turn_id="turn:second",
            now_ms=second_due + 100,
        )
        self.store.finish_event(
            AgentEventEnvelope(
                event_id="event:second",
                session_id="session:target",
                turn_id="turn:second",
                sequence=2,
                created_at_ms=second_due + 200,
                event_type="turn_completed",
                payload={},
                resume_token="2",
            )
        )
        final = self.store.get(str(schedule["id"]))
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["runCount"], 2)

    def test_role_wake_persists_only_the_canonical_builtin_role_id(
        self,
    ) -> None:
        schedule = self.store.create(
            {
                "title": "唤醒未来",
                "instruction": "继续长期任务",
                "targetType": "role",
                "targetRoleId": "vcp-v1",
                "targetRoleVersion": "1",
                "wakeAtMs": self.now + 1_000,
            },
            now_ms=self.now,
        )

        self.assertEqual(
            schedule["targetRoleId"],
            "companion-future-v1",
        )

    def test_busy_target_defer_does_not_consume_run_budget(self) -> None:
        schedule = self._create(recurrence_kind="daily", max_runs=3)
        claim = self.store.claim_due(now_ms=self.now + 1_000)[0]

        self.store.defer(
            str(claim["runId"]),
            reason="目标线程正忙",
            delay_ms=60_000,
            now_ms=self.now + 1_100,
        )

        deferred = self.store.get(str(schedule["id"]))
        self.assertEqual(deferred["status"], "scheduled")
        self.assertEqual(deferred["runCount"], 0)
        self.assertEqual(deferred["nextWakeAtMs"], self.now + 61_100)
        self.assertEqual(deferred["latestRun"]["state"], "deferred")

    def test_deferred_run_persists_typed_cause_without_consuming_budget(
        self,
    ) -> None:
        schedule = self._create()
        claim = self.store.claim_due(now_ms=self.now + 1_000)[0]

        self.store.defer(
            str(claim["runId"]),
            reason="Facilitator turn is temporarily busy",
            cause_code="SESSION_BUSY",
            delay_ms=5_000,
            now_ms=self.now + 1_100,
        )

        deferred = self.store.get(str(schedule["id"]))
        self.assertEqual(deferred["status"], "scheduled")
        self.assertEqual(deferred["runCount"], 0)
        self.assertEqual(deferred["latestRun"]["state"], "deferred")
        self.assertEqual(
            deferred["latestRun"]["result"],
            {"causeCode": "SESSION_BUSY"},
        )

    def test_claim_is_atomic_and_bounded_to_two_active_runs(self) -> None:
        for index in range(3):
            self._create(title=f"任务 {index}")
        peer = AgentWakeScheduleStore(self.db_path)

        first_claims = self.store.claim_due(
            now_ms=self.now + 1_000,
            limit=3,
            max_active=2,
        )
        second_claims = peer.claim_due(
            now_ms=self.now + 1_000,
            limit=3,
            max_active=2,
        )

        self.assertEqual(len(first_claims), 2)
        self.assertEqual(second_claims, [])
        self.assertEqual(
            len({str(item["id"]) for item in first_claims}),
            2,
        )

    def test_expired_accepted_run_fails_closed_after_gateway_restart(self) -> None:
        schedule = self._create()
        claim = self.store.claim_due(
            now_ms=self.now + 1_000,
            lease_ms=60_000,
        )[0]
        self.store.accept(
            str(claim["runId"]),
            session_id="session:target",
            turn_id="turn:scheduled",
            now_ms=self.now + 1_100,
        )

        claims = self.store.claim_due(now_ms=self.now + 61_001)

        self.assertEqual(claims, [])
        recovered = self.store.get(str(schedule["id"]))
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["latestRun"]["state"], "failed")
        self.assertIn("restarted", recovered["lastError"])

    def test_root_cancel_fences_a_running_wake_and_ignores_late_terminal_event(self) -> None:
        schedule = self._create(recurrence_kind="daily", max_runs=3)
        claim = self.store.claim_due(now_ms=self.now + 1_000)[0]
        self.store.accept(
            str(claim["runId"]),
            session_id="session:target",
            turn_id="turn:scheduled",
            now_ms=self.now + 1_100,
        )

        cancelled = self.store.cancel_for_root(
            str(schedule["id"]),
            reason="Room root stopped",
            now_ms=self.now + 1_200,
        )

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["latestRun"]["state"], "failed")
        self.assertEqual(cancelled["nextWakeAtMs"], 0)
        self.assertIn("Room root stopped", cancelled["lastError"])
        self.assertFalse(
            self.store.finish_event(
                self._event("turn_completed", created_at_ms=self.now + 1_300)
            )
        )
        self.assertEqual(self.store.get(str(schedule["id"]))["status"], "cancelled")

    def test_validation_rejects_invalid_horizon_and_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "targetType"):
            self.store.validate_create(
                {
                    "instruction": "test",
                    "targetType": "room",
                    "wakeAtMs": self.now + 1_000,
                },
                now_ms=self.now,
            )
        with self.assertRaisesRegex(ValueError, "366 days"):
            self.store.validate_create(
                {
                    "instruction": "test",
                    "targetType": "session",
                    "targetSessionId": "session:target",
                    "wakeAtMs": self.now + 367 * 86_400_000,
                },
                now_ms=self.now,
            )
        with self.assertRaisesRegex(ValueError, "IANA timezone"):
            self.store.validate_create(
                {
                    "instruction": "test",
                    "targetType": "session",
                    "targetSessionId": "session:target",
                    "wakeAtMs": self.now + 1_000,
                    "timezone": "Mars/Olympus_Mons",
                },
                now_ms=self.now,
            )

    def _create(
        self,
        *,
        title: str = "整理任务",
        recurrence_kind: str = "once",
        max_runs: int = 1,
    ) -> dict[str, object]:
        return self.store.create(
            {
                "title": title,
                "instruction": "整理结果并汇报",
                "targetType": "session",
                "targetSessionId": "session:target",
                "wakeAtMs": self.now + 1_000,
                "recurrenceKind": recurrence_kind,
                "maxRuns": max_runs,
            },
            now_ms=self.now,
        )

    def _event(self, event_type: str, *, created_at_ms: int) -> AgentEventEnvelope:
        return AgentEventEnvelope(
            event_id="event:scheduled",
            session_id="session:target",
            turn_id="turn:scheduled",
            sequence=1,
            created_at_ms=created_at_ms,
            event_type=event_type,
            payload={},
            resume_token="1",
        )


if __name__ == "__main__":
    unittest.main()
