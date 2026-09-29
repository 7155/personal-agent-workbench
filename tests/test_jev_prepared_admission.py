"""Prepared admission keeps exact identity through failures and projections."""
from __future__ import annotations

import unittest
from unittest.mock import Mock

from rag_ime.agent_command_receipts import AgentCommandReceiptFailed
from rag_ime.rooms.session_dispatch import RoomSessionDispatchService
from rag_ime.rooms.turn_registry import RoomTurnRegistry


class PreparedAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.registry = RoomTurnRegistry()
        self.room = {"id": "room", "activeTopicId": "topic"}
        self.actor = {"id": "actor", "sessionId": "session", "status": "active"}
        self.work = {"id": "task", "revision": 3, "topicId": "topic",
                     "accountableParticipantId": "actor"}
        self.rooms = Mock()
        self.rooms.get.return_value = self.room
        self.rooms.participant.return_value = self.actor
        self.work_store = Mock()
        self.work_store.get.return_value = self.work
        self.partners = Mock()
        self.events = Mock()
        self.prompt = Mock(return_value={"accepted": True, "turnId": "pi-turn"})
        self.validate = Mock()
        self.reserve = Mock()
        self.release = Mock(return_value=True)
        self.require_active = Mock()
        self.configure = Mock()
        self.build_context = Mock(return_value="room context")
        self.accept = Mock(side_effect=self.registry.accept)
        self.service = RoomSessionDispatchService(
            rooms=self.rooms, room_work=self.work_store, room_events=self.events,
            room_turns=self.registry, room_partner_dispatches=self.partners,
            context_source_token=object(), restore_participant_sessions=Mock(),
            guard_session_route=Mock(), recover_faulted_session=Mock(),
            resume_goal_if_paused=Mock(), target_idle=Mock(return_value=True),
            record_room_evidence=Mock(), accept_turn=self.accept, prompt=self.prompt,
            build_participant_prompt=self.build_context, resolve_attachments=Mock())
        self.request = {"sessionId": "session", "rootId": "root", "dispatchId": "dispatch",
                        "roomId": "room", "ownerId": "actor", "taskId": "task", "graphId": "graph",
                        "taskBrief": {"objective": "run exact responsibility"},
                        "contextManifest": {"materials": []}}

    def dispatch(self):
        return self.service.dispatch_prepared(self.request, validate=self.validate,
            reserve=self.reserve, release=self.release, require_active=self.require_active,
            configure=self.configure)

    def test_each_pretransport_failure_releases_partial_reservation(self):
        for site in ("reserve", "configure", "register", "route", "context", "require_active"):
            with self.subTest(site=site):
                self.setUp()
                operation = {"reserve": self.reserve, "configure": self.configure, "register": self.partners.register,
                             "route": self.events.publish, "context": self.build_context,
                             "require_active": self.require_active}[site]
                operation.side_effect = RuntimeError(site + " failed")
                result = self.dispatch()
                self.assertFalse(result["accepted"])
                self.assertTrue(result["notSent"])
                self.assertEqual(result["status"], "not_sent")
                self.assertIn(site, result["error"])
                self.prompt.assert_not_called()
                self.release.assert_called_once()
                self.assertEqual(self.registry.active_turn("session"), ("", ""))
                self.assertNotIn("session", self.registry.user_priority_sessions)

    def test_failure_before_reserve_never_releases_another_reservation(self):
        self.validate.side_effect = ValueError("stale binding")
        result = self.dispatch()
        self.assertTrue(result["notSent"])
        self.reserve.assert_not_called()
        self.release.assert_not_called()
        self.assertFalse(self.registry.session_turn_active("session"))

    def test_final_validation_covers_changes_during_context_assembly(self):
        self.validate.side_effect = [None, ValueError("context revision changed")]
        result = self.dispatch()
        self.assertTrue(result["notSent"])
        self.prompt.assert_not_called()
        self.release.assert_called_once()

    def test_stop_during_context_assembly_is_not_sent(self):
        def stop(*args, **kwargs):
            self.registry.record_cancellation("root", "stop-receipt")
            return "room context"
        self.build_context.side_effect = stop
        result = self.dispatch()
        self.assertTrue(result["notSent"])
        self.prompt.assert_not_called()
        self.release.assert_called_once()

    def test_transport_timeout_keeps_exact_registry_quarantine(self):
        self.prompt.side_effect = TimeoutError("receipt lost")
        result = self.dispatch()
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["notSent"])
        self.assertEqual(self.registry.active_turn("session"), ("root", "dispatch"))
        self.assertNotIn("session", self.registry.user_priority_sessions)
        self.release.assert_not_called()
        self.dispatch()
        self.assertEqual(self.prompt.call_count, 1)

    def test_missing_turn_receipt_is_unknown(self):
        self.prompt.return_value = {"accepted": True}
        self.assertEqual(self.dispatch()["status"], "unknown")
        self.release.assert_not_called()
        self.assertEqual(self.registry.active_turn("session"), ("root", "dispatch"))

    def test_tool_manifest_failure_is_proven_not_sent_and_releases_exact_reservation(self):
        self.prompt.side_effect = AgentCommandReceiptFailed("schema refresh failed",
            client_message_id="dispatch", cause_code="TOOL_MANIFEST_SYNC_FAILED")
        result = self.dispatch()
        self.assertTrue(result["notSent"])
        self.assertEqual(result["status"], "not_sent")
        self.release.assert_called_once()
        self.assertEqual(self.registry.active_turn("session"), ("", ""))

    def test_explicit_rejection_releases_reservation(self):
        self.prompt.return_value = {"accepted": False, "admissionCancelled": True}
        result = self.dispatch()
        self.assertFalse(result["accepted"])
        self.assertTrue(result["notSent"])
        self.release.assert_called_once()
        self.assertFalse(self.registry.session_turn_active("session"))

    def test_projection_failures_do_not_revoke_admission(self):
        for operation in ("accept_turn", "mark_dispatched", "commit_route", "advance_delivery_cursor"):
            with self.subTest(operation=operation):
                self.setUp()
                effect = {"accept_turn": self.accept, "mark_dispatched": self.partners.mark_dispatched,
                          "commit_route": self.rooms.commit_route,
                          "advance_delivery_cursor": self.rooms.advance_delivery_cursor}[operation]
                effect.side_effect = RuntimeError("projection failed")
                result = self.dispatch()
                self.assertTrue(result["accepted"])
                self.assertEqual(result["sessionTurnId"], "pi-turn")
                self.assertIn(operation, result["projectionSync"]["failedOperations"])
                self.release.assert_not_called()

    def test_auxiliary_purposes_do_not_claim_business_work_identity(self):
        for purpose in ("plan", "verify", "synthesize"):
            with self.subTest(purpose=purpose):
                self.setUp()
                self.request.update(purpose=purpose, subjectHash="current-subject")
                result = self.dispatch()
                self.assertTrue(result["accepted"])
                self.work_store.get.assert_not_called()
                self.partners.register.assert_not_called()
                self.partners.mark_dispatched.assert_not_called()
                self.assertEqual(self.registry.active_turn("session"), ("root", "dispatch"))
                self.assertEqual(self.registry.work_by_session_turn, {})
                self.assertIsNone(self.build_context.call_args.kwargs["work_item"])
                self.assertEqual(self.build_context.call_args.args[0]["_jevExecutionPurpose"], purpose)
                self.assertNotIn("_jevExecutionPurpose", self.room)
                decision = self.rooms.commit_route.call_args.args[1]
                self.assertEqual(decision["purpose"], purpose)
                self.assertNotIn("workItemId", decision)

    def test_old_cleanup_never_removes_replacement_same_root_dispatch(self):
        self.build_context.side_effect = RuntimeError("old context failed")
        def replacement():
            self.registry.cancel("session", "root")
            self.registry.begin("session", "root", dispatch_id="new-dispatch")
            self.registry.hold_priority(("session",))
            return False
        self.release.side_effect = replacement
        self.assertTrue(self.dispatch()["notSent"])
        self.assertEqual(self.registry.active_turn("session"), ("root", "new-dispatch"))
        self.assertIn("session", self.registry.user_priority_sessions)

    def test_release_error_remains_visible_and_does_not_escape(self):
        self.reserve.side_effect = RuntimeError("reserve write failed")
        self.release.side_effect = RuntimeError("release write failed")
        result = self.dispatch()
        self.assertTrue(result["notSent"])
        self.assertIn("release_prompt_admission", result["cleanupSync"]["failedOperations"])
        self.prompt.assert_not_called()

    def test_late_acceptance_does_not_take_new_dispatch_registry_identity(self):
        def reused(*args, **kwargs):
            self.registry.cancel("session", "root")
            self.registry.begin("session", "root", dispatch_id="new-dispatch")
            return {"accepted": True, "turnId": "old-pi-turn"}
        self.prompt.side_effect = reused
        result = self.dispatch()
        self.assertTrue(result["accepted"])
        self.assertEqual(self.registry.active_turn("session"), ("root", "new-dispatch"))
        self.accept.assert_not_called()
        self.assertIn("accept_turn", result["projectionSync"]["failedOperations"])

    def test_synchronous_terminal_projection_cannot_leave_priority_busy(self):
        def complete(session, turn, root):
            self.registry.accept(session, turn, root)
            self.registry.cancel(session, root)
        self.accept.side_effect = complete
        self.assertTrue(self.dispatch()["accepted"])
        self.assertNotIn("session", self.registry.user_priority_sessions)

    def test_priority_projection_error_cannot_revoke_known_acceptance(self):
        self.registry.release_priority_session = Mock(side_effect=RuntimeError("registry fault"))
        result = self.dispatch()
        self.assertTrue(result["accepted"])
        self.assertIn("release_room_priority", result["projectionSync"]["failedOperations"])

    def repair(self, *, mode="active", runtime=None):
        self.receipt = {"state": "accepted", "receiptId": "actual-receipt", "dispatchId": "dispatch",
                        "taskId": "task", "sessionId": "session", "turnId": "pi-turn"}
        if runtime is None:
            runtime = Mock()
            runtime.prepare_accepted_turn_projection.return_value = lambda project: (project(mode), mode)[1]
        return self.service.repair_prepared_projection(self.request, self.receipt,
            validate=self.validate, runtime=runtime)

    def test_repair_restores_empty_registry_from_exact_host_guard_without_prompt(self):
        result = self.repair()
        self.assertEqual(result["state"], "accepted")
        self.assertEqual(result["receiptId"], "actual-receipt")
        self.assertEqual(result["projectionSync"]["state"], "synced")
        self.assertEqual(self.registry.active_turn("session"), ("root", "dispatch"))
        self.assertEqual(self.registry.work_by_session_turn[("session", "pi-turn")]["attemptId"], "dispatch")
        self.partners.mark_dispatched.assert_called_once_with("dispatch", target_session_turn_id="pi-turn")
        self.prompt.assert_not_called()
        self.reserve.assert_not_called()

    def test_repair_promotes_unknown_pending_and_is_idempotent(self):
        self.registry.begin("session", "root", dispatch_id="dispatch", child=True)
        self.assertEqual(self.repair()["projectionSync"]["state"], "synced")
        self.assertEqual(self.repair()["projectionSync"]["state"], "synced")
        self.assertEqual(self.registry.dispatch_by_session_turn, {("session", "pi-turn"): "dispatch"})
        self.prompt.assert_not_called()

    def test_repair_cannot_touch_another_same_root_dispatch_or_turn(self):
        for dispatch, turn in (("new-dispatch", "new-turn"), ("dispatch", "new-turn")):
            with self.subTest(dispatch=dispatch):
                self.setUp()
                self.registry.begin("session", "root", dispatch_id=dispatch)
                self.registry.accept("session", turn, "root")
                result = self.repair()
                self.assertEqual(result["state"], "accepted")
                self.assertEqual(result["projectionSync"]["state"], "pending")
                self.assertEqual(self.registry.dispatch_by_session_turn, {("session", turn): dispatch})
                self.partners.register.assert_not_called()

    def test_registry_conflict_blocks_runtime_reconciliation_before_any_old_events(self):
        self.registry.begin("session", "root", dispatch_id="new-dispatch")
        guard = Mock()
        runtime = Mock()
        runtime.prepare_accepted_turn_projection.return_value = guard
        self.assertEqual(self.repair(runtime=runtime)["projectionSync"]["state"], "pending")
        guard.assert_not_called()

    def test_repair_does_not_take_private_or_unbound_priority_reservation(self):
        for private in (False, True):
            with self.subTest(private=private):
                self.setUp()
                if private:
                    self.registry.begin_private_intercom("session", "new-notification")
                else:
                    self.registry.hold_priority(("session",))
                self.assertEqual(self.repair()["projectionSync"]["state"], "pending")
                self.assertEqual(self.registry.active_turn("session"), ("", ""))
                self.accept.assert_not_called()

    def test_repair_retired_turn_only_repairs_durable_metadata(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                self.setUp()
                if pending:
                    self.registry.begin("session", "root", dispatch_id="dispatch")
                result = self.repair(mode="retired")
                self.assertEqual(result["projectionSync"]["state"], "synced")
                self.assertEqual(self.registry.active_turn("session"), ("", ""))
                self.accept.assert_not_called()
                self.partners.mark_dispatched.assert_called_once()

    def test_repair_auxiliary_turn_never_claims_business_work(self):
        for purpose in ("plan", "verify", "synthesize"):
            with self.subTest(purpose=purpose):
                self.setUp()
                self.request.update(purpose=purpose, subjectHash="subject")
                self.assertEqual(self.repair()["projectionSync"]["state"], "synced")
                self.work_store.get.assert_not_called()
                self.partners.register.assert_not_called()
                self.partners.mark_dispatched.assert_not_called()
                self.assertEqual(self.registry.work_by_session_turn, {})

    def test_repair_stale_binding_and_projection_errors_never_revoke_acceptance(self):
        for failing in ("binding", "metadata", "runtime"):
            with self.subTest(failing=failing):
                self.setUp()
                runtime = Mock()
                runtime.prepare_accepted_turn_projection.return_value = lambda project: (project("active"), "active")[1]
                if failing == "binding":
                    self.validate.side_effect = ValueError("owner changed")
                elif failing == "metadata":
                    self.partners.mark_dispatched.side_effect = RuntimeError("write failed")
                else:
                    runtime.prepare_accepted_turn_projection.side_effect = RuntimeError("Host unavailable")
                result = self.repair(runtime=runtime)
                self.assertEqual(result["state"], "accepted")
                self.assertEqual(result["projectionSync"]["state"], "pending")
                self.prompt.assert_not_called()
                self.reserve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
