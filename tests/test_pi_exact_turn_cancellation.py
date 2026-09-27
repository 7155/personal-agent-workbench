from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock

from rag_ime.pi.values import PiRuntimeError, PiRuntimeTurnConflict
from rag_ime.pi.runtime import PiRuntimeHostManager, _HostedSessionState


class ExactTurnCancellationTests(unittest.TestCase):
    def setUp(self):
        self.runtime = object.__new__(PiRuntimeHostManager)
        self.runtime._lock = threading.RLock()
        self.runtime._lifecycle_lock = threading.RLock()
        self.runtime._open_sessions = {"session"}
        self.runtime._host_capabilities = {"runtimePrimitives": {"sessionExactTurnCancel": True}}
        self.runtime._states = {"session": _HostedSessionState(turn_id="turn", client_message_id="dispatch")}
        self.client = Mock(running=True)
        self.runtime._client = self.client
        self.runtime._require_client = Mock(return_value=self.client)
        self.runtime.events = Mock()
        self.runtime.ensure = Mock()
        self.runtime.abort = Mock(side_effect=AssertionError("broad abort forbidden"))
        self.receipt = {"schemaVersion": "rag-ime.pi-exact-turn-cancel.v1", "sessionId": "session",
                        "turnId": "turn", "clientMessageId": "dispatch", "cancelId": "cancel",
                        "receiptId": "host-receipt", "state": "accepted", "phase": "requested"}
        self.client.send.return_value = self.receipt

    def cancel(self, **kwargs):
        return self.runtime.abort_turn("session", "turn", client_message_id="dispatch",
                                       cancel_id="cancel", **kwargs)

    def test_host_receives_exact_compare_and_cancel_identity(self):
        result = self.cancel()
        self.assertEqual(result["receiptId"], "host-receipt")
        self.assertNotIn("drained", result)
        self.client.send.assert_called_once_with("session.abort", {
            "sessionId": "session", "expectedTurnId": "turn", "clientMessageId": "dispatch",
            "cancelId": "cancel", "lookupOnly": False}, timeout=1.0)
        self.runtime.abort.assert_not_called()

    def test_interrupted_recovery_requires_explicit_new_host_capability(self):
        with self.assertRaisesRegex(PiRuntimeError, "interrupted turn recovery"):
            self.cancel(recover_interrupted_only=True)
        self.client.send.assert_not_called()
        self.runtime._host_capabilities['runtimePrimitives']['sessionInterruptedTurnRecovery'] = True
        self.client.send.return_value = {**self.receipt, 'phase': 'settled'}
        self.cancel(recover_interrupted_only=True)
        self.assertTrue(self.client.send.call_args.args[1]['recoverInterruptedOnly'])
        self.runtime.events.publish.assert_not_called()
        self.runtime.abort.assert_not_called()
        self.client.send.reset_mock()
        with self.assertRaises(ValueError):
            self.cancel(recover_interrupted_only=True, recover_retired_only=True)
        self.client.send.assert_not_called()

    def test_retired_recovery_is_explicit_and_capability_gated(self):
        with self.assertRaisesRegex(PiRuntimeError, "retired turn recovery"):
            self.cancel(recover_retired_only=True)
        self.client.send.assert_not_called()
        self.runtime._host_capabilities["runtimePrimitives"]["sessionRetiredTurnRecovery"] = True
        self.client.send.return_value = {**self.receipt, "phase": "settled"}
        self.cancel(recover_retired_only=True)
        self.assertTrue(self.client.send.call_args.args[1]["recoverRetiredOnly"])
        self.runtime.events.publish.assert_not_called()
        self.assertEqual(self.runtime._states["session"].abort_requested_turn_id, "")
        self.runtime.abort.assert_not_called()

    def test_retired_recovery_rejection_is_retryable_without_fabricated_receipt(self):
        self.runtime._host_capabilities["runtimePrimitives"]["sessionRetiredTurnRecovery"] = True
        rejected = {key: value for key, value in self.receipt.items() if key not in {"receiptId", "phase"}}
        self.client.send.return_value = {**rejected, "state": "rejected", "reason": "pending_resources"}
        self.assertEqual(self.cancel(recover_retired_only=True)["state"], "rejected")
        with self.assertRaises(PiRuntimeError):
            self.cancel()  # Ordinary cancellation still requires its durable receipt.
        self.runtime.abort.assert_not_called()

    def test_unsupported_host_never_falls_back_to_broad_abort(self):
        self.runtime._host_capabilities = {}
        with self.assertRaisesRegex(PiRuntimeError, "exact turn cancellation"):
            self.cancel()
        self.client.send.assert_not_called()
        self.runtime.abort.assert_not_called()

    def test_old_cancel_does_not_mark_reused_local_session_aborting(self):
        self.runtime._states["session"].turn_id = "new-turn"
        self.runtime._states["session"].client_message_id = "new-dispatch"
        self.cancel(lookup_only=True)
        self.runtime.events.publish.assert_not_called()
        self.assertEqual(self.runtime._states["session"].abort_requested_turn_id, "")
        self.assertTrue(self.client.send.call_args.args[1]["lookupOnly"])

    def test_wrong_host_identity_is_not_accepted(self):
        self.client.send.return_value = {**self.receipt, "turnId": "another-turn"}
        with self.assertRaises(PiRuntimeError):
            self.cancel()
        self.runtime.events.publish.assert_not_called()

    def test_timeout_never_calls_broad_abort(self):
        self.client.send.side_effect = TimeoutError("lost cancellation ACK")
        with self.assertRaises(TimeoutError):
            self.cancel()
        self.runtime.abort.assert_not_called()

    def test_session_restore_never_retires_another_turn(self):
        self.runtime._open_sessions = set()
        self.cancel(lookup_only=True)
        self.runtime.ensure.assert_called_once_with("session", retire_recovered_turn=False)

    def test_projection_failure_keeps_real_acceptance(self):
        self.runtime.events.publish.side_effect = RuntimeError("event failed")
        result = self.cancel()
        self.assertEqual(result["state"], "accepted")
        self.assertEqual(result["projectionSync"]["state"], "pending")

    def test_release_never_clears_dispatched_unknown_admission(self):
        self.runtime._states["session"] = _HostedSessionState(
            prompt_admission_in_flight=True, admission_client_message_id="dispatch", prompt_dispatched=True)
        self.runtime.sessions = Mock()
        self.runtime._schedule_idle_locked = Mock()
        self.assertFalse(self.runtime.release_prompt_admission("session", client_message_id="dispatch"))
        self.assertTrue(self.runtime._states["session"].prompt_admission_in_flight)
        self.runtime.sessions.set_status.assert_not_called()

    def test_release_write_failure_keeps_exact_claim_retryable(self):
        self.runtime._states["session"] = _HostedSessionState(
            prompt_admission_in_flight=True, admission_client_message_id="dispatch")
        self.runtime.sessions = Mock()
        self.runtime._schedule_idle_locked = Mock()
        self.runtime.sessions.set_status.side_effect = [RuntimeError("write failed"), None]
        with self.assertRaises(RuntimeError):
            self.runtime.release_prompt_admission("session", client_message_id="dispatch")
        self.assertTrue(self.runtime._states["session"].prompt_admission_in_flight)
        self.assertTrue(self.runtime.release_prompt_admission("session", client_message_id="dispatch"))

    def projection_guard(self, *, active=None):
        self.runtime._retired_host_turns = set()
        self.runtime._cancel_idle_locked = Mock()
        self.runtime._schedule_idle_locked = Mock()
        self.runtime.sessions = Mock()
        self.runtime.ensure.return_value = {"state": {
            "schemaVersion": "rag-ime.pi-session-control-state.v1", "sessionId": "session",
            "isIdle": False, "activeTurn": active or {"turnId": "turn", "clientMessageId": "dispatch"}}}
        return self.runtime.prepare_accepted_turn_projection("session", "turn", client_message_id="dispatch")

    def test_projection_guard_restores_actual_host_turn_and_consumes_only_its_admission(self):
        guard = self.projection_guard()
        self.runtime._states["session"] = _HostedSessionState(prompt_admission_in_flight=True,
            admission_client_message_id="dispatch", prompt_dispatched=True)
        project = Mock()
        self.assertEqual(guard(project), "active")
        self.runtime.ensure.assert_called_once_with("session", retire_recovered_turn=False)
        project.assert_called_once_with("active")
        self.assertEqual(self.runtime._states["session"].turn_id, "turn")
        self.assertFalse(self.runtime._states["session"].prompt_admission_in_flight)

    def test_projection_guard_rejects_new_admission_between_observation_and_projection(self):
        guard = self.projection_guard()
        self.runtime._states["session"] = _HostedSessionState(prompt_admission_in_flight=True,
            admission_client_message_id="new-dispatch")
        project = Mock()
        with self.assertRaises(PiRuntimeTurnConflict):
            guard(project)
        project.assert_not_called()
        self.assertEqual(self.runtime._states["session"].admission_client_message_id, "new-dispatch")

    def test_projection_guard_rejects_another_turn_even_when_local_registry_would_be_empty(self):
        self.runtime._states["session"] = _HostedSessionState()
        with self.assertRaises(PiRuntimeTurnConflict):
            self.projection_guard(active={"turnId": "new-turn", "clientMessageId": "new-dispatch"})
        self.assertEqual(self.runtime._states["session"].turn_id, "")

    def test_projection_guard_rejects_host_replacement_and_new_local_turn(self):
        for host_replaced in (False, True):
            with self.subTest(host_replaced=host_replaced):
                self.setUp()
                guard = self.projection_guard()
                if host_replaced:
                    self.runtime._client = Mock(running=True)
                else:
                    self.runtime._states["session"].turn_id = "new-turn"
                project = Mock()
                with self.assertRaises(PiRuntimeTurnConflict):
                    guard(project)
                project.assert_not_called()

    def test_projection_guard_does_not_resurrect_a_turn_retired_after_observation(self):
        guard = self.projection_guard()
        self.runtime._states["session"] = _HostedSessionState(retired_turn_ids={"turn"})
        project = Mock()
        self.assertEqual(guard(project), "retired")
        project.assert_called_once_with("retired")
        self.assertEqual(self.runtime._states["session"].turn_id, "")

    def test_projection_guard_does_not_infer_old_turn_terminal_from_current_idle(self):
        self.projection_guard()
        self.runtime.ensure.return_value["state"].update(isIdle=True, activeTurn=None)
        with self.assertRaises(PiRuntimeError):
            self.runtime.prepare_accepted_turn_projection("session", "turn", client_message_id="dispatch")

    def test_projection_guard_requires_exact_settlement_before_terminal_reconciliation(self):
        self.projection_guard()
        settlement = {"schemaVersion": "rag-ime.pi-turn-settlement.v1", "sessionId": "session",
            "turnId": "turn", "clientMessageId": "dispatch", "runtimeSessionId": "runtime-session",
            "receipt": {"schemaVersion": "pi.agent-settled.v2", "sessionId": "runtime-session",
                "receiptId": "actual-settlement", "runId": "turn", "scopeId": "runtime-session:turn",
                "disposition": "aborted", "aborted": True, "pendingOperations": 0, "operations": {"pending": 0}}}
        self.runtime.ensure.return_value["state"].update(isIdle=True, activeTurn=None, turnSettlement=settlement)
        self.runtime._reconcile_turn_settlement = Mock()
        guard = self.runtime.prepare_accepted_turn_projection("session", "turn", client_message_id="dispatch")
        project = Mock()
        self.assertEqual(guard(project), "settled")
        project.assert_called_once_with("settled")
        self.runtime._reconcile_turn_settlement.assert_called_once_with(settlement)
        self.runtime._states["session"] = _HostedSessionState(prompt_admission_in_flight=True,
            admission_client_message_id="dispatch", prompt_dispatched=True)
        self.runtime._reconcile_turn_settlement.reset_mock()
        self.assertEqual(guard(project), "settled")
        self.assertEqual(self.runtime._states["session"].turn_id, "")
        self.assertFalse(self.runtime._states["session"].prompt_admission_in_flight)
        self.runtime._reconcile_turn_settlement.assert_not_called()
        settlement["turnId"] = "new-turn"
        with self.assertRaises(PiRuntimeError):
            self.runtime.prepare_accepted_turn_projection("session", "turn", client_message_id="dispatch")

    def test_projection_guard_holds_admission_lock_through_mapping(self):
        guard = self.projection_guard()
        observed = []
        def project(mode):
            def another_thread():
                acquired = self.runtime._lock.acquire(blocking=False)
                observed.append(acquired)
                if acquired:
                    self.runtime._lock.release()
            contender = threading.Thread(target=another_thread)
            contender.start()
            contender.join(timeout=1)
        guard(project)
        self.assertEqual(observed, [False])


if __name__ == "__main__":
    unittest.main()
