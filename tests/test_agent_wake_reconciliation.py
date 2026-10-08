from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from rag_ime.agent_command_receipts import AgentCommandReceiptPending, AgentCommandReceiptFailed
from rag_ime.agent_service import AgentService
from rag_ime.agent_prompt_delivery import AgentPromptAcceptanceUnknown
from rag_ime.agent_runtime_driver import AgentRuntimeError
from rag_ime.pi.config import PiRuntimeConfig
from tests.sqlite_fixtures import copy_current_database


class AgentWakeReconciliationTests(unittest.TestCase):
    """Real Wake ledger/application/receipt/journal, passive native boundary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-wake-original-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        db = root / "fixture.sqlite"
        copy_current_database(db)
        self.service = AgentService(db_path=db, runtime_config=PiRuntimeConfig(
            enabled=False, executable=None, agent_dir=root / "config",
            session_dir=root / "sessions", logs_dir=root / "logs"),
            wake_scheduler_enabled=False)
        self.addCleanup(self.service.close)
        self.session = self.service.sessions.create(title="Original target")
        self.now = 1_800_000_000_000
        self.store = self.service.wake_schedules
        self.schedule = self.store.create({"title": "Original instruction", "instruction": "Read only fixture",
            "targetType": "session", "targetSessionId": self.session["id"],
            "wakeAtMs": self.now + 1000, "recurrenceKind": "daily", "maxRuns": 2}, now_ms=self.now)
        self.claim = self.store.claim_due(now_ms=self.now + 1000, lease_ms=60000)[0]
        self.run = str(self.claim["runId"])
        self.receipt_claim = self.service.command_receipts.begin(command_scope="session_prompt",
            scope_id=self.session["id"], client_message_id=self.run, payload={"message": "fixture"})

    def state(self):
        return self.store.get(self.schedule["id"])

    def dispatch_unknown(self):
        with patch.object(self.service, "prompt", side_effect=AgentCommandReceiptPending(
                "ACK unknown", client_message_id=self.run, recovery_state="in_flight")) as prompt:
            self.service.wake_scheduler._dispatch_safely(self.claim)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(prompt.call_args.args[1]["clientMessageId"], self.run)

    def acceptance(self, *, session_id=None, client=None, turn="turn:original", sequence=1):
        target = session_id or self.session["id"]
        self.service.sessions.record_runtime_event(event_id=f"event:accept:{target}:{sequence}",
            session_id=target, turn_id=turn, sequence=sequence, event_type="message_completed",
            created_at_ms=self.now + 2000, metrics={"promptAcceptance": {
                "clientMessageId": client or self.run, "turnId": turn, "messageId": "message:fixture"}})

    def terminal(self, *, turn="turn:original", sequence=2):
        self.service.sessions.record_runtime_event(event_id=f"event:terminal:{sequence}",
            session_id=self.session["id"], turn_id=turn, sequence=sequence,
            event_type="turn_completed", created_at_ms=self.now + 3000)

    def reconcile(self):
        with patch.object(self.service, "prompt", side_effect=AssertionError("passive recovery prompted")), \
                patch.object(self.service, "create_session", side_effect=AssertionError("passive recovery created target")), \
                patch.object(self.service, "resume_session", side_effect=AssertionError("passive recovery resumed")), \
                patch.object(self.service.runtime, "ensure", side_effect=AssertionError("passive recovery ensured Host")), \
                patch.object(self.service.runtime, "prepare_accepted_turn_projection", side_effect=AssertionError("passive recovery opened Host")), \
                patch.object(self.service.runtime, "await_turn_settled", side_effect=AssertionError("passive recovery waited on Host")):
            return self.service.wake_application.reconcile_once()

    def test_unknown_ack_keeps_original_bound_run_and_late_terminal_recovers_once(self):
        self.dispatch_unknown()
        state = self.state()
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["latestRun"]["state"], "claimed")
        self.assertEqual(state["latestRun"]["sessionId"], self.session["id"])
        self.assertEqual(self.store.claim_due(now_ms=self.now + 86_400_001), [])
        self.acceptance()
        self.terminal()  # Terminal preceded local Wake acceptance; durable replay survives cache loss.
        self.assertEqual(self.reconcile(), 1)
        self.assertEqual(self.state()["latestRun"]["state"], "completed")
        self.assertEqual(self.state()["runCount"], 1)
        self.assertEqual(self.reconcile(), 0)

    def test_exact_receipts_for_wrong_session_or_client_cannot_accept_original(self):
        self.dispatch_unknown()
        other = self.service.sessions.create(title="Other exact scope")
        for target, client in ((other["id"], self.run), (self.session["id"], "other-client")):
            claim = self.service.command_receipts.begin(command_scope="session_prompt",
                scope_id=target, client_message_id=client, payload={"message": "fixture"})
            self.service.command_receipts.complete(claim, command_scope="session_prompt",
                scope_id=target, client_message_id=client,
                response={"accepted": True, "clientMessageId": client, "turnId": "turn:unrelated"})
        self.assertEqual(self.reconcile(), 0)
        self.assertEqual(self.state()["latestRun"]["state"], "claimed")

    def test_typed_pre_send_busy_defers_without_budget_but_raw_text_does_not(self):
        def typed_busy(*args):
            self.service.sessions.set_status(self.session["id"], "busy")
            raise AgentCommandReceiptFailed("Busy fixture", cause_code="SESSION_BUSY", client_message_id=self.run)
        with patch.object(self.service, "prompt", side_effect=typed_busy):
            self.service.wake_scheduler._dispatch_safely(self.claim)
        self.assertEqual(self.state()["latestRun"]["state"], "deferred")
        self.assertEqual(self.state()["runCount"], 0)
        self.service.sessions.set_status(self.session["id"], "idle")
        next_claim = self.store.claim_due(now_ms=self.now + 86_400_000)[0]
        def raw_text(*args):
            self.service.sessions.set_status(self.session["id"], "busy")
            raise AgentRuntimeError("上一轮仍执行；未知传输结果")
        with patch.object(self.service, "prompt", side_effect=raw_text):
            self.service.wake_scheduler._dispatch_safely(next_claim)
        self.assertEqual(self.state()["status"], "running")
        self.assertEqual(self.state()["latestRun"]["state"], "claimed")

    def test_readonly_target_validation_fails_before_prompt(self):
        self.store.cancel_for_root(self.schedule["id"], reason="separate fixture")
        readonly = self.service.sessions.create(title="Eval read only", evaluation_snapshot=True)
        schedule = self.store.create({"title": "Readonly fixture", "instruction": "Fixture only",
            "targetType": "session", "targetSessionId": readonly["id"],
            "wakeAtMs": self.now + 1000}, now_ms=self.now)
        claim = self.store.claim_due(now_ms=self.now + 1000)[0]
        with patch.object(self.service, "prompt", side_effect=AssertionError("readonly target prompted")) as prompt:
            self.service.wake_scheduler._dispatch_safely(claim)
        prompt.assert_not_called()
        self.assertEqual(self.store.get(schedule["id"])["latestRun"]["state"], "failed")

    def test_missing_receipt_or_wrong_session_client_turn_never_settles_original(self):
        self.dispatch_unknown()
        other = self.service.sessions.create(title="Same title is not identity")
        self.acceptance(session_id=other["id"])
        self.acceptance(client="another-client")
        self.terminal(turn="turn:unrelated")
        self.assertEqual(self.reconcile(), 0)
        self.assertEqual(self.state()["latestRun"]["state"], "claimed")
        self.acceptance(sequence=3)
        self.assertEqual(self.reconcile(), 1)
        self.assertEqual(self.state()["latestRun"]["state"], "accepted")
        self.assertEqual(self.reconcile(), 0)

    def test_accept_projection_fault_keeps_binding_and_receipt_recovers_without_reprompt(self):
        self.service.command_receipts.complete(self.receipt_claim, command_scope="session_prompt",
            scope_id=self.session["id"], client_message_id=self.run,
            response={"accepted": True, "turnId": "turn:original", "clientMessageId": self.run})
        with patch.object(self.service, "prompt", return_value={"accepted": True, "turnId": "turn:original"}), \
                patch.object(self.store, "accept", side_effect=RuntimeError("fixture local projection fault")):
            self.service.wake_scheduler._dispatch_safely(self.claim)
        self.assertEqual(self.state()["status"], "running")
        self.assertEqual(self.state()["latestRun"]["sessionId"], self.session["id"])
        self.assertEqual(self.reconcile(), 1)
        self.assertEqual(self.state()["latestRun"]["turnId"], "turn:original")

    def test_cancel_during_exact_acceptance_lookup_fences_late_recovery(self):
        self.dispatch_unknown()
        def lookup(**kwargs):
            self.store.cancel_for_root(self.schedule["id"], reason="Stopped", now_ms=self.now + 2000)
            return {"accepted": True, "turnId": "turn:original", "clientMessageId": self.run}
        with patch.object(self.service.command_receipts, "acceptance_evidence_for_exact_command", side_effect=lookup):
            self.reconcile()
        self.acceptance()
        self.terminal()
        self.assertEqual(self.reconcile(), 0)
        self.assertEqual(self.state()["status"], "cancelled")

    def test_typed_definitive_rejection_remains_failed(self):
        with patch.object(self.service, "prompt", side_effect=AgentCommandReceiptFailed(
                "Definitively rejected", cause_code="PI_RUNTIME_DISABLED", client_message_id=self.run)):
            self.service.wake_scheduler._dispatch_safely(self.claim)
        self.assertEqual(self.state()["latestRun"]["state"], "failed")

    def test_untyped_failed_receipt_is_not_definitive_rejection(self):
        with patch.object(self.service, "prompt", side_effect=AgentCommandReceiptFailed(
                "Unknown local failure", client_message_id=self.run)):
            self.service.wake_scheduler._dispatch_safely(self.claim)
        self.assertEqual(self.state()["status"], "running")

    def test_maintenance_uses_existing_owner_tick_for_passive_recovery(self):
        self.dispatch_unknown()
        self.acceptance()
        with patch.object(self.service.wake_application, "reconcile_once", wraps=self.service.wake_application.reconcile_once) as reconcile:
            self.service._run_scheduled_work_once(self.now + 2500)
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(self.state()["latestRun"]["state"], "accepted")

    def test_terminal_before_normal_accept_still_completes(self):
        def prompt(*args):
            self.service.events.publish(self.session["id"], "turn_completed", turn_id="turn:original", payload={})
            return {"accepted": True, "turnId": "turn:original"}
        with patch.object(self.service, "prompt", side_effect=prompt):
            self.service.wake_scheduler._dispatch_safely(self.claim)
        self.assertEqual(self.state()["latestRun"]["state"], "completed")

    def test_duplicate_bound_dispatch_never_reprompts_or_creates_another_role_target(self):
        schedule = self.store.create({"title": "Role original", "instruction": "Fixture only",
            "targetType": "role", "targetRoleId": "companion-firstlight-v1",
            "wakeAtMs": self.now + 1000}, now_ms=self.now)
        claim = self.store.claim_due(now_ms=self.now + 1000)[0]
        with patch.object(self.service, "prompt", side_effect=AgentCommandReceiptPending("unknown")) as prompt, \
                patch.object(self.service, "create_session", wraps=self.service.create_session) as create:
            self.service.wake_scheduler._dispatch_safely(claim)
            original = self.store.get(schedule["id"])["latestRun"]["sessionId"]
            self.service.wake_scheduler._dispatch_safely(claim)
        self.assertTrue(original)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(self.store.get(schedule["id"])["latestRun"]["sessionId"], original)

    def test_real_prompt_owner_unknown_ack_retains_receipt_and_runtime_admission(self):
        self.store.cancel_for_root(self.schedule["id"], reason="separate fixture")
        schedule = self.store.create({"title": "Actual Prompt seam", "instruction": "Fixture only",
            "targetType": "session", "targetSessionId": self.session["id"],
            "wakeAtMs": self.now + 1000}, now_ms=self.now)
        claim = self.store.claim_due(now_ms=self.now + 1000)[0]
        run = claim["runId"]
        with patch.object(self.service.prompt_application, "dispatch_checkpoint",
                side_effect=AgentPromptAcceptanceUnknown("native ACK unknown fixture")) as native, \
                patch.object(self.service.runtime, "prompt", side_effect=AssertionError("real Host forbidden")):
            self.service.wake_scheduler._dispatch_safely(claim)
        self.assertEqual(native.call_count, 1)
        self.assertEqual(self.store.get(schedule["id"])["status"], "running")
        self.service.runtime.require_prompt_admission_active(self.session["id"], client_message_id=run)
        self.assertIsNone(self.service.command_receipts.failure_evidence_for_exact_command(
            command_scope="session_prompt", scope_id=self.session["id"], client_message_id=run))
        self.service.runtime.release_prompt_admission(self.session["id"], client_message_id=run)

    def test_untyped_durable_failed_receipt_stays_unknown_until_original_acceptance(self):
        self.dispatch_unknown()
        self.service.command_receipts.fail(self.receipt_claim, command_scope="session_prompt",
            scope_id=self.session["id"], client_message_id=self.run, error=RuntimeError("local error without proof"))
        self.assertEqual(self.reconcile(), 0)
        self.assertEqual(self.state()["status"], "running")
        self.acceptance()
        self.terminal()
        self.assertEqual(self.reconcile(), 1)
        self.assertEqual(self.state()["latestRun"]["state"], "completed")

    def test_accepted_expired_run_passively_finishes_from_exact_persisted_terminal(self):
        with patch.object(self.service, "prompt", return_value={"accepted": True, "turnId": "turn:original"}):
            self.service.wake_scheduler._dispatch_safely(self.claim)
        self.assertEqual(self.store.claim_due(now_ms=self.now + 86_400_001), [])
        self.terminal()
        self.assertEqual(self.reconcile(), 1)
        self.assertEqual(self.state()["latestRun"]["state"], "completed")

    def test_post_accept_replay_fault_does_not_fail_the_accepted_original(self):
        with patch.object(self.service, "prompt", return_value={"accepted": True, "turnId": "turn:original"}), \
                patch.object(self.service.events, "replay", side_effect=RuntimeError("local replay projection fault")):
            self.service.wake_scheduler._dispatch_safely(self.claim)
        self.assertEqual(self.state()["status"], "running")
        self.assertEqual(self.state()["latestRun"]["turnId"], "turn:original")
        self.terminal()
        self.assertEqual(self.reconcile(), 1)

    def test_uncertainty_note_fault_never_falls_through_to_definitive_failure(self):
        with patch.object(self.service, "prompt", side_effect=AgentCommandReceiptPending("unknown")), \
                patch.object(self.store, "retain_uncertain", side_effect=RuntimeError("note write failed")):
            self.service.wake_scheduler._dispatch_safely(self.claim)
        self.assertEqual(self.state()["status"], "running")
        self.assertEqual(self.state()["latestRun"]["sessionId"], self.session["id"])

    def test_exact_durable_typed_rejection_passively_finishes_original(self):
        self.dispatch_unknown()
        self.service.command_receipts.fail(self.receipt_claim, command_scope="session_prompt",
            scope_id=self.session["id"], client_message_id=self.run,
            error=RuntimeError("Host rejected fixture"), cause_code="PI_RUNTIME_DISABLED")
        self.assertEqual(self.reconcile(), 1)
        self.assertEqual(self.state()["latestRun"]["state"], "failed")
        self.assertEqual(self.reconcile(), 0)

    def test_pre_send_archived_target_fails_without_prompt_or_unknown_binding(self):
        self.service.sessions.archive(self.session["id"])
        with patch.object(self.service, "prompt", side_effect=AssertionError("pre-send failure prompted")) as prompt:
            self.service.wake_scheduler._dispatch_safely(self.claim)
        prompt.assert_not_called()
        self.assertEqual(self.state()["latestRun"]["state"], "failed")
        self.assertEqual(self.state()["latestRun"]["sessionId"], "")

    def test_goal_budget_is_definitive_pre_send_owner_rejection(self):
        self.service.sessions.mutate_agent_goal(self.session["id"], {
            "action": "confirm_setup", "confirmed": True, "expectedRevision": 0,
            "objective": "Bounded fixture", "tokenBudget": 10})
        self.service.sessions.record_agent_goal_usage(self.session["id"],
            idempotency_key="budget:fixture", turn_id="turn:budget", event_id="event:budget",
            token_delta=10, elapsed_delta_ms=0)
        with patch.object(self.service.prompt_application, "dispatch_checkpoint", side_effect=AssertionError("budget rejection sent")) as native:
            # Use a new run key so the real Prompt owner can create its receipt.
            self.store.cancel_for_root(self.schedule["id"], reason="separate fixture")
            schedule = self.store.create({"title": "Budget fixture", "instruction": "Fixture only",
                "targetType": "session", "targetSessionId": self.session["id"],
                "wakeAtMs": self.now + 1000}, now_ms=self.now)
            claim = self.store.claim_due(now_ms=self.now + 1000)[0]
            self.service.wake_scheduler._dispatch_safely(claim)
        native.assert_not_called()
        self.assertEqual(self.store.get(schedule["id"])["latestRun"]["state"], "failed")
        failure = self.service.command_receipts.failure_evidence_for_exact_command(
            command_scope="session_prompt", scope_id=self.session["id"], client_message_id=claim["runId"])
        self.assertEqual(failure["causeCode"], "goal_budget_exhausted")

    def test_original_admission_cancel_response_is_not_unknown(self):
        with patch.object(self.service, "prompt", return_value={"accepted": False,
                "admissionCancelled": True, "cancelled": True, "turnId": ""}):
            self.service.wake_scheduler._dispatch_safely(self.claim)
        self.assertEqual(self.state()["latestRun"]["state"], "failed")
        self.assertIn("cancelled", self.state()["latestRun"]["error"])

    def test_cancel_wins_between_passive_read_and_accept_transaction(self):
        self.dispatch_unknown()
        self.acceptance()
        entered, release = Event(), Event()
        original = self.store.accept
        errors = []
        def accept(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("fixture latch timed out")
            return original(*args, **kwargs)
        def reconcile():
            try:
                self.reconcile()
            except BaseException as error:
                errors.append(error)
        with patch.object(self.store, "accept", side_effect=accept):
            worker = Thread(target=reconcile, name="fixture-original-wake-cas")
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.store.cancel_for_root(self.schedule["id"], reason="Root cancellation wins")
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.state()["status"], "cancelled")
        self.assertEqual(self.reconcile(), 0)
