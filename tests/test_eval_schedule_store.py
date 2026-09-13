from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from rag_ime.eval_schedule_store import EvalScheduleRunner, EvalScheduleStore
from rag_ime.trace_runtime import build_eval_run


class EvalScheduleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-eval-schedule-")
        self.db_path = Path(self.tmp.name) / "eval.sqlite"
        self.store = EvalScheduleStore(self.db_path)
        self.store.initialize()
        self.now = 1_800_000_000_000

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pause_resume_cancel_survive_restart_and_control_actual_claims(self) -> None:
        schedule = self.store.create({"suiteId": "sgg", "suiteRevision": "fixture-v2", "recurrenceKind": "daily", "nextDueAtMs": self.now + 1000}, now_ms=self.now)
        identifier = str(schedule["id"])
        self.assertEqual(self.store.action(identifier, "pause", now_ms=self.now)["status"], "paused")
        reopened = EvalScheduleStore(self.db_path)
        self.assertEqual(reopened.claim_due(now_ms=self.now + 10_000), [])
        resumed = reopened.action(identifier, "resume", now_ms=self.now + 10_000)
        self.assertEqual(resumed["nextDueAtMs"], self.now + 11_000)
        reopened.action(identifier, "cancel", now_ms=self.now + 10_000)
        self.assertEqual(reopened.claim_due(now_ms=self.now + 20_000), [])
        self.assertEqual(reopened.get(identifier)["status"], "cancelled")
        self.assertEqual(reopened.runs(identifier), [])

    def test_retry_preserves_pinned_definition_and_old_run_receipts(self) -> None:
        definition = {"scheduleId": "eval-schedule:retry", "suiteId": "sgg", "suiteRevision": "fixture-v2", "recurrenceKind": "daily", "nextDueAtMs": self.now + 1000, "maxRuns": 1}
        self.store.create(definition, now_ms=self.now)
        claim = self.store.claim_due(now_ms=self.now + 1000)[0]
        self.store.fail(str(claim["runId"]), lease_token=str(claim["leaseToken"]), error_code="test_failure", now_ms=self.now + 1100)
        retried = self.store.action("eval-schedule:retry", "retry", now_ms=self.now + 1200)
        self.assertEqual(retried["maxRuns"], 1)
        self.assertEqual(retried["runCount"], 1)
        self.assertEqual(self.store.runs("eval-schedule:retry")[0]["state"], "failed")
        replayed = self.store.create(definition, now_ms=self.now + 1500)
        self.assertEqual(replayed["id"], "eval-schedule:retry")
        self.assertEqual(replayed["status"], "scheduled")
        self.assertEqual(self.store.claim_due(now_ms=self.now + 2200)[0]["attempt"], 2)

    def test_running_eval_cannot_be_marked_cancelled_before_its_receipt(self) -> None:
        schedule = self.store.create({"suiteId": "sgg", "suiteRevision": "fixture-v2", "recurrenceKind": "daily", "nextDueAtMs": self.now + 1000}, now_ms=self.now)
        identifier = str(schedule["id"])
        self.store.claim_due(now_ms=self.now + 1000)
        for action in ("pause", "cancel", "retry", "resume"):
            with self.assertRaises(ValueError):
                self.store.action(identifier, action, now_ms=self.now + 2000)
        self.assertEqual(self.store.get(identifier)["status"], "running")
        self.assertEqual(len(self.store.runs(identifier)), 1)

    def test_create_requires_an_explicit_suite_revision(self) -> None:
        with self.assertRaisesRegex(ValueError, "suiteRevision is required"):
            self.store.create(
                {
                    "suiteId": "sgg",
                    "recurrenceKind": "daily",
                    "nextDueAtMs": self.now + 1_000,
                },
                now_ms=self.now,
            )

    def test_create_rejects_unknown_suite_and_revision_before_insert(self) -> None:
        for suite_id, suite_revision, expected in (
            ("not-allowlisted", "fixture-v1", "allowlisted"),
            ("sgg", "fixture-v1", "revision"),
        ):
            with self.subTest(suite_id=suite_id, suite_revision=suite_revision):
                with self.assertRaisesRegex(ValueError, expected):
                    self.store.create(
                        {
                            "scheduleId": f"eval-schedule:invalid-{suite_id}",
                            "suiteId": suite_id,
                            "suiteRevision": suite_revision,
                            "recurrenceKind": "daily",
                            "nextDueAtMs": self.now + 1_000,
                        },
                        now_ms=self.now,
                    )
                self.assertEqual(self.store.list(), [])

    def test_claim_does_not_mark_next_due_as_executed_and_success_advances(self) -> None:
        definition = {
                "scheduleId": "eval-schedule:daily",
                "suiteId": "sgg",
                "suiteRevision": "fixture-v2",
                "recurrenceKind": "daily",
                "nextDueAtMs": self.now + 1_000,
                "maxRuns": 2,
        }
        schedule = self.store.create(
            definition,
            now_ms=self.now,
        )

        claim = self.store.claim_due(now_ms=self.now + 1_000)[0]
        claimed = self.store.get(str(schedule["id"]))
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["nextDueAtMs"], self.now + 1_000)
        self.assertEqual(claimed["runCount"], 1)

        self.store.succeed(
            str(claim["runId"]),
            lease_token=str(claim["leaseToken"]),
            eval_run_id="eval:sgg:1",
            now_ms=self.now + 2_000,
        )
        # Re-delivery of the same terminal acknowledgement is a no-op.
        duplicate = self.store.succeed(
            str(claim["runId"]),
            lease_token=str(claim["leaseToken"]),
            eval_run_id="eval:sgg:1",
            now_ms=self.now + 2_100,
        )
        self.assertEqual(duplicate["state"], "succeeded")
        settled = self.store.get(str(schedule["id"]))
        self.assertEqual(settled["status"], "scheduled")
        self.assertEqual(settled["initialDueAtMs"], self.now + 1_000)
        self.assertEqual(settled["nextDueAtMs"], self.now + 1_000 + 86_400_000)
        self.assertEqual(settled["latestRun"]["state"], "succeeded")
        self.assertEqual(settled["latestRun"]["evalRunId"], "eval:sgg:1")
        # A retried create request compares immutable definition fields, not
        # the schedule's advanced next-due cursor.
        self.assertEqual(
            self.store.create(definition, now_ms=self.now + 2_100)["id"],
            schedule["id"],
        )

    def test_terminal_failure_replay_must_not_rebind_its_error_code(self) -> None:
        self.store.create(
            {
                "scheduleId": "eval-schedule:failure-replay",
                "suiteId": "sgg",
                "suiteRevision": "fixture-v2",
                "recurrenceKind": "daily",
                "nextDueAtMs": self.now + 1_000,
                "maxRuns": 1,
            },
            now_ms=self.now,
        )
        claim = self.store.claim_due(now_ms=self.now + 1_000)[0]
        self.store.fail(
            str(claim["runId"]),
            lease_token=str(claim["leaseToken"]),
            error_code="fixture_failed",
            now_ms=self.now + 2_000,
        )
        with self.assertRaisesRegex(ValueError, "no longer active"):
            self.store.fail(
                str(claim["runId"]),
                lease_token=str(claim["leaseToken"]),
                error_code="different_failure",
                now_ms=self.now + 2_100,
            )

    def test_weekly_failure_advances_until_max_runs_then_is_terminal(self) -> None:
        schedule = self.store.create(
            {
                "suiteId": "zhanggui-wenshu",
                "suiteRevision": "fixture-v2",
                "recurrenceKind": "weekly",
                "nextDueAtMs": self.now + 1_000,
                "maxRuns": 2,
            },
            now_ms=self.now,
        )
        first = self.store.claim_due(now_ms=self.now + 1_000)[0]
        self.store.fail(
            str(first["runId"]),
            lease_token=str(first["leaseToken"]),
            error_code="fixture_failed",
            now_ms=self.now + 2_000,
        )
        after_first = self.store.get(str(schedule["id"]))
        self.assertEqual(after_first["status"], "scheduled")
        self.assertEqual(after_first["nextDueAtMs"], self.now + 1_000 + 7 * 86_400_000)

        second_due = int(after_first["nextDueAtMs"])
        second = self.store.claim_due(now_ms=second_due)[0]
        self.store.fail(
            str(second["runId"]),
            lease_token=str(second["leaseToken"]),
            error_code="fixture_failed_again",
            now_ms=second_due + 1,
        )
        final = self.store.get(str(schedule["id"]))
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["nextDueAtMs"], 0)

    def test_due_claim_is_atomic_across_store_instances(self) -> None:
        self.store.create(
            {
                "suiteId": "sgg",
                "suiteRevision": "fixture-v2",
                "recurrenceKind": "daily",
                "nextDueAtMs": self.now + 1_000,
            },
            now_ms=self.now,
        )
        peer = EvalScheduleStore(self.db_path)
        barrier = threading.Barrier(2)
        results: list[list[dict[str, object]]] = []

        def claim(store: EvalScheduleStore) -> None:
            barrier.wait()
            results.append(store.claim_due(now_ms=self.now + 1_000))

        first = threading.Thread(target=claim, args=(self.store,))
        second = threading.Thread(target=claim, args=(peer,))
        first.start()
        second.start()
        first.join()
        second.join()
        self.assertEqual(sum(len(items) for items in results), 1)

    def test_expired_claim_is_failed_on_reopen_and_old_worker_cannot_settle_it(self) -> None:
        schedule = self.store.create(
            {
                "suiteId": "sgg",
                "suiteRevision": "fixture-v2",
                "recurrenceKind": "daily",
                "nextDueAtMs": self.now + 1_000,
                "maxRuns": 2,
            },
            now_ms=self.now,
        )
        claim = self.store.claim_due(
            now_ms=self.now + 1_000,
            lease_ms=60_000,
        )[0]
        reopened = EvalScheduleStore(self.db_path)
        self.assertEqual(reopened.claim_due(now_ms=self.now + 61_001), [])
        recovered = reopened.get(str(schedule["id"]))
        self.assertEqual(recovered["latestRun"]["state"], "failed")
        self.assertEqual(recovered["latestRun"]["errorCode"], "lease_expired")
        self.assertEqual(recovered["status"], "scheduled")
        with self.assertRaises(ValueError):
            reopened.succeed(
                str(claim["runId"]),
                lease_token=str(claim["leaseToken"]),
                eval_run_id="eval:stale",
                now_ms=self.now + 61_002,
            )

    def test_runner_uses_injected_executor_and_only_persists_eval_run_reference(self) -> None:
        self.store.create(
            {
                "suiteId": "sgg",
                "suiteRevision": "fixture-v2",
                "recurrenceKind": "daily",
                "nextDueAtMs": self.now + 1_000,
            },
            now_ms=self.now,
        )
        seen: list[dict[str, object]] = []

        def execute(claim: dict[str, object]) -> dict[str, object]:
            seen.append(dict(claim))
            return {"evalRunId": "eval:injected" , "ignoredPayload": "not persisted"}

        eval_payload = build_eval_run(
            eval_run_id="eval:injected",
            trace_ids=["trace:injected"],
            mode="ground_truth",
            truth_kind="frozen",
            dataset_id="dataset:v1",
            label_revision="labels:v1",
            metrics={"f1": 1.0},
            suite_binding={"suiteId": "sgg", "suiteRevision": "fixture-v2"},
            now_ms=self.now + 1_001,
        ).to_dict()
        runner = EvalScheduleRunner(
            store=self.store,
            execute=execute,
            eval_run_loader=lambda _eval_run_id: eval_payload,
            max_parallel=1,
        )
        self.assertEqual(runner.run_due_once(now_ms=self.now + 1_000), 1)
        self.assertEqual(len(seen), 1)
        latest = self.store.get(str(seen[0]["scheduleId"]))["latestRun"]
        self.assertEqual(latest["evalRunId"], "eval:injected")
        self.assertNotIn("ignoredPayload", latest)

    def test_runner_rejects_missing_or_unpersisted_eval_run_identity(self) -> None:
        self.store.create(
            {
                "scheduleId": "eval-schedule:missing-result",
                "suiteId": "sgg",
                "suiteRevision": "fixture-v2",
                "recurrenceKind": "daily",
                "nextDueAtMs": self.now + 1_000,
            },
            now_ms=self.now,
        )
        runner = EvalScheduleRunner(
            store=self.store,
            execute=lambda _claim: {},
            eval_run_exists=lambda _eval_run_id: False,
        )

        self.assertEqual(runner.run_due_once(now_ms=self.now + 1_000), 1)
        missing = self.store.get("eval-schedule:missing-result")
        assert missing is not None
        self.assertEqual(missing["status"], "scheduled")
        self.assertEqual(missing["latestRun"]["state"], "failed")
        self.assertEqual(missing["latestRun"]["errorCode"], "eval_run_missing")

        self.store.create(
            {
                "scheduleId": "eval-schedule:foreign-result",
                "suiteId": "sgg",
                "suiteRevision": "fixture-v2",
                "recurrenceKind": "daily",
                "nextDueAtMs": self.now + 1_000,
            },
            now_ms=self.now,
        )
        runner = EvalScheduleRunner(
            store=self.store,
            execute=lambda _claim: {"evalRunId": "eval:not-persisted"},
            eval_run_exists=lambda _eval_run_id: False,
        )

        self.assertEqual(runner.run_due_once(now_ms=self.now + 1_000), 1)
        foreign = self.store.get("eval-schedule:foreign-result")
        assert foreign is not None
        self.assertEqual(foreign["latestRun"]["errorCode"], "eval_run_not_found")

    def test_runner_requires_exact_builtin_suite_binding_before_success(self) -> None:
        cases = (
            ("missing", None, "eval_run_suite_binding_missing"),
            (
                "suite-mismatch",
                {"suiteId": "zhanggui-wenshu", "suiteRevision": "fixture-v2"},
                "eval_run_suite_mismatch",
            ),
            (
                "revision-mismatch",
                {"suiteId": "sgg", "suiteRevision": "fixture-v1"},
                "eval_run_suite_mismatch",
            ),
        )
        for suffix, binding, expected_error in cases:
            with self.subTest(suffix=suffix):
                schedule_id = f"eval-schedule:binding-{suffix}"
                self.store.create(
                    {
                        "scheduleId": schedule_id,
                        "suiteId": "sgg",
                        "suiteRevision": "fixture-v2",
                        "recurrenceKind": "daily",
                        "nextDueAtMs": self.now + 1_000,
                    },
                    now_ms=self.now,
                )
                eval_run = build_eval_run(
                    eval_run_id=f"eval:binding:{suffix}",
                    trace_ids=[f"trace:binding:{suffix}"],
                    mode="ground_truth",
                    truth_kind="frozen",
                    dataset_id="dataset:v1",
                    label_revision="labels:v1",
                    metrics={"f1": 1.0},
                    suite_binding=binding,
                    now_ms=self.now + 1_001,
                ).to_dict()
                runner = EvalScheduleRunner(
                    store=self.store,
                    execute=lambda _claim, eval_id=eval_run["evalRunId"]: {"evalRunId": eval_id},
                    eval_run_loader=lambda _eval_id, payload=eval_run: payload,
                )

                self.assertEqual(runner.run_due_once(now_ms=self.now + 1_000), 1)
                settled = self.store.get(schedule_id)
                self.assertEqual(settled["latestRun"]["state"], "failed")
                self.assertEqual(settled["latestRun"]["errorCode"], expected_error)

        exact_schedule_id = "eval-schedule:binding-exact"
        self.store.create(
            {
                "scheduleId": exact_schedule_id,
                "suiteId": "sgg",
                "suiteRevision": "fixture-v2",
                "recurrenceKind": "daily",
                "nextDueAtMs": self.now + 1_000,
            },
            now_ms=self.now,
        )
        exact_eval = build_eval_run(
            eval_run_id="eval:binding:exact",
            trace_ids=["trace:binding:exact"],
            mode="ground_truth",
            truth_kind="frozen",
            dataset_id="dataset:v1",
            label_revision="labels:v1",
            metrics={"f1": 1.0},
            suite_binding={"suiteId": "sgg", "suiteRevision": "fixture-v2"},
            now_ms=self.now + 1_001,
        ).to_dict()
        runner = EvalScheduleRunner(
            store=self.store,
            execute=lambda _claim: {"evalRunId": "eval:binding:exact"},
            eval_run_loader=lambda _eval_id: exact_eval,
        )
        self.assertEqual(runner.run_due_once(now_ms=self.now + 1_000), 1)
        settled = self.store.get(exact_schedule_id)
        self.assertEqual(settled["latestRun"]["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
