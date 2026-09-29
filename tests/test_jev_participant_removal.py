"""Draft acceptance tests for managed JEV membership removal.

These use the existing isolated service fixture and explicit Pi test doubles.
"""
from dataclasses import asdict
from unittest.mock import patch

from rag_ime.jev_tasks.removal import JevParticipantRemoval
from rag_ime.pi.values import PiRuntimeCommandRejected
from rag_ime.jev_tasks.types import digest
from tests.test_jev_host_application import JevHostFixture


class JevParticipantRemovalTests(JevHostFixture):
    def add_third(self):
        session = self.service.sessions.create(title="Replacement")
        return self.service.rooms.add_participant(self.room["id"], session_id=session["id"],
            role_id="reviewer", role_version="1", display_name="Replacement",
            collaboration_role="reviewer")

    def add_fourth(self):
        session = self.service.sessions.create(title="Fourth")
        return self.service.rooms.add_participant(self.room["id"], session_id=session["id"],
            role_id="researcher", role_version="1", display_name="Fourth",
            collaboration_role="researcher")

    def move_root_to_worker(self, created):
        task = self.snapshot(created).task(created["workItemId"])
        worker = self.room["participants"][1]
        self.app.command(self.room["id"], {
            "action": "reassign", "graphId": created["graphId"], "taskId": task.id,
            "taskHash": digest(asdict(task)), "targetParticipantId": worker["id"],
            "reason": "fixture responsibility", "clientMessageId": "fixture-reassign"})
        return worker

    def remove(self, participant, *, replacement=None, **extra):
        return self.service.remove_room_participant(self.room["id"], {
            "participantId": participant["id"], "clientMessageId": "remove-worker",
            **({"replacementParticipantId": replacement["id"]} if replacement else {}),
            **extra})

    def test_idle_task_transfers_exact_responsibility_before_membership_removal(self):
        replacement = self.add_third()
        created = self.create()
        worker = self.move_root_to_worker(created)
        response = self.remove(worker, replacement=replacement)
        self.assertEqual(response["status"], "pending")
        self.assertEqual(response["participant"]["status"], "active")
        self.app.removal.advance(response["removal"]["removalId"])
        task = self.snapshot(created).task(created["workItemId"])
        self.assertEqual(task.owner_id, replacement["id"])
        self.assertEqual(task.accepted_turn_id, "")
        self.app.removal.advance(response["removal"]["removalId"])
        self.assertEqual(self.service.rooms.participant(worker["id"])["status"], "removed")
        self.assertEqual(self.service.sessions.get(worker["sessionId"])["status"], "archived")
        self.assertEqual(self.app.removal.projection(self.room["id"]), [])
        self.prompt.assert_not_called()

    def test_running_cancel_acceptance_does_not_claim_drain_or_remove(self):
        replacement = self.add_third()
        created = self.create()
        worker = self.move_root_to_worker(created)
        self.app.tick()
        original = self.snapshot(created).task(created["workItemId"])
        self.assertTrue(original.accepted_turn_id)
        with patch.object(self.service.runtime, "is_turn_active", return_value=True):
            response = self.remove(worker, replacement=replacement)
            self.app.removal.advance(response["removal"]["removalId"])
        with self.app.ledger.connection() as conn:
            cancel = conn.execute("SELECT state FROM agent_jev_runtime_effects WHERE effect_id=?",
                ("cancel:" + response["removal"]["removalId"] + ":" + original.id,)).fetchone()
        self.assertEqual(cancel[0], "pending")
        self.assertEqual(self.snapshot(created).task(original.id).binding, original.binding)
        self.assertEqual(self.service.rooms.participant(worker["id"])["status"], "active")
        self.assertNotEqual(self.service.sessions.get(worker["sessionId"])["status"], "archived")

    def test_review_waits_for_real_acceptance_and_keeps_historical_owner(self):
        replacement = self.add_third()
        created = self.create()
        worker = self.move_root_to_worker(created)
        self.app.tick()
        task = self.snapshot(created).task(created["workItemId"])
        self.assertTrue(task.accepted_turn_id)
        record = self.service.room_partner_dispatches.get(task.accepted_turn_id)
        self.service.room_partner_application._settle_dispatch(
            record, phase="completed", result="可核验结果", completion_source="room_post")
        self.assertEqual(self.snapshot(created).task(task.id).state, "review")
        response = self.remove(worker, replacement=replacement)
        self.app.removal.advance(response["removal"]["removalId"])
        self.assertEqual(self.app.removal.projection(self.room["id"])[0]["stage"], "awaiting_review")
        self.assertEqual(self.service.rooms.participant(worker["id"])["status"], "active")
        terminal = {"eventId": "review-terminal", "eventType": "turn_completed", "status": "completed"}
        with patch.object(self.app, "execution_terminal", return_value=terminal):
            self.app.reconcile_graph(self.app.binding_by_graph(created["graphId"]))
            task = self.snapshot(created).task(task.id)
            accepted = self.service.jev_command(self.room["id"], {
                "action": "accept", "graphId": created["graphId"],
                "clientMessageId": "review-during-removal", "taskId": task.id,
                "taskHash": digest(asdict(task)), "reason": "检查原执行的证据",
                "evidenceRefs": ["test:result"], "operabilityVerdict": "passed",
                "requirementVerdict": "satisfied"})
        self.assertEqual(accepted["task"]["state"], "done")
        # The fixture has no native Pi terminal event to clear Session busy.
        # Model that separate terminal projection after exact drain/acceptance.
        self.service.sessions.set_status(worker["sessionId"], "idle")
        with patch.object(self.service.room_management.participants,
                          "active_runtime_session_ids", return_value=set()):
            self.app.removal.advance(response["removal"]["removalId"])
        self.assertEqual(self.snapshot(created).task(task.id).owner_id, worker["id"])
        self.assertEqual(self.service.rooms.participant(worker["id"])["status"], "removed")

    def test_owner_lock_or_no_eligible_partner_never_enqueues_cancel(self):
        replacement = self.add_third()
        created = self.create()
        worker = self.move_root_to_worker(created)
        with self.app.ledger.connection(write=True) as conn:
            conn.execute("INSERT INTO agent_jev_task_requirements VALUES(?,?,?)",
                (created["workItemId"], created["graphId"],
                 '{"ownerParticipantId":"' + worker["id"] + '"}'))
        response = self.remove(worker, replacement=replacement)
        self.assertEqual(response["status"], "pending")
        self.app.removal.advance(response["removal"]["removalId"])
        self.assertEqual(self.app.removal.projection(self.room["id"])[0]["stage"], "revise_owner_lock")
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_reclaims").fetchone()[0], 0)
        self.assertEqual(self.service.rooms.participant(worker["id"])["status"], "active")

    def test_controller_has_no_implicit_stop_and_pending_intent_survives_new_owner_object(self):
        self.add_third()
        created = self.create()
        controller = self.room["participants"][0]
        response = self.remove(controller)
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["removal"]["stage"], "change_moderator")
        self.assertFalse(self.app.lifecycle.policy(created["graphId"])["stopped"])
        worker = self.move_root_to_worker(created)
        accepted = self.remove(worker)
        restored = JevParticipantRemoval(self.app)
        self.assertEqual(restored.projection(self.room["id"])[0]["removalId"],
                         accepted["removal"]["removalId"])
        self.assertEqual(self.remove(worker)["removal"]["removalId"],
                         accepted["removal"]["removalId"])

    def test_explicit_controller_stop_precedes_removal_and_never_rebinds_graph(self):
        self.add_third()
        self.service.rooms.update_config(self.room["id"], {"routingPolicy": "jev"})
        created = self.create()
        controller = self.room["participants"][0]
        response = self.remove(controller, stopRoot=True)
        self.assertEqual(response["status"], "pending")
        self.assertFalse(self.app.lifecycle.policy(created["graphId"])["stopped"])
        self.app.removal.advance(response["removal"]["removalId"])
        self.assertTrue(self.app.lifecycle.policy(created["graphId"])["stopped"])
        self.assertEqual(self.service.rooms.participant(controller["id"])["status"], "active")
        self.app.removal.advance(response["removal"]["removalId"])
        self.assertEqual(self.service.rooms.participant(controller["id"])["status"], "removed")
        self.assertEqual(self.app.binding_by_graph(created["graphId"])["controller_participant_id"],
                         controller["id"])

    def test_pending_removal_reserves_minimum_two_member_slots(self):
        third = self.add_third()
        self.create()
        first = self.room["participants"][1]
        with patch.object(self.app, "eligible_participants", side_effect=AssertionError("catalog during request")):
            accepted = self.remove(first)
        self.assertEqual(accepted["status"], "pending")
        second = self.service.remove_room_participant(self.room["id"], {
            "participantId": third["id"], "clientMessageId": "remove-second"})
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(second["removal"]["stage"], "minimum_participants")
        self.assertEqual([p["participantId"] for p in self.app.removal.projection(self.room["id"])],
                         [first["id"]])
        self.assertEqual(self.service.rooms.participant(third["id"])["status"], "active")
        with self.assertRaisesRegex(ValueError, "pending removals"):
            self.service.rooms.remove_participant(self.room["id"], third["id"])

    def test_pending_member_is_excluded_from_legacy_routing_partner_delegate_and_pi_admission(self):
        replacement = self.add_third()
        created = self.create()
        worker = self.move_root_to_worker(created)
        accepted = self.remove(worker, replacement=replacement)
        self.assertEqual(accepted["status"], "pending")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.service.room_dispatch.post_message(self.room["id"],
                message="new task", client_message_id="new-legacy-work",
                retry_of_root_id="", requested_participant_ids=[worker["id"]],
                work_item_id="", attachment_ids=[])
        partner = self.service.room_partner_application
        with patch.object(partner, "_active_root", return_value=("root:test", "dispatch:test")), \
             patch.object(partner, "_create_delegated_work") as create_work:
            with self.assertRaisesRegex(ValueError, "pending Room removal"):
                partner._delegate(self.room["participants"][0], {
                    "targetParticipantId": worker["id"], "task": "new task",
                    "expectedOutput": "result", "acceptanceCriteria": ["done"]},
                    tool_call_id="pending-target")
            create_work.assert_not_called()
        with self.assertRaises(PiRuntimeCommandRejected):
            with self.service._room_prompt_admission_gate(worker["sessionId"], self.room["id"]):
                pass
        self.prompt.assert_not_called()

    def test_revoked_explicit_target_waits_and_new_client_message_retargets_same_intent(self):
        original_target = self.add_third()
        new_target = self.add_fourth()
        created = self.create()
        worker = self.move_root_to_worker(created)
        pending = self.remove(worker, replacement=original_target)
        self.assertEqual(pending["status"], "pending")
        revoking = self.service.remove_room_participant(self.room["id"], {
            "participantId": original_target["id"], "clientMessageId": "remove-original-target"})
        self.assertEqual(revoking["status"], "pending")
        self.app.removal.advance(pending["removal"]["removalId"])
        worker_removal = next(item for item in self.app.removal.projection(self.room["id"])
                              if item["participantId"] == worker["id"])
        self.assertEqual(worker_removal["stage"], "requires_partner")
        self.assertEqual(self.snapshot(created).task(created["workItemId"]).owner_id, worker["id"])
        retarget = {"participantId": worker["id"], "clientMessageId": "retarget-worker",
                    "replacementParticipantId": new_target["id"]}
        changed = self.service.remove_room_participant(self.room["id"], retarget)
        self.assertEqual(changed["removal"]["removalId"], pending["removal"]["removalId"])
        self.assertEqual(changed["removal"]["targetParticipantId"], new_target["id"])
        self.assertEqual(self.service.remove_room_participant(self.room["id"], retarget)["removal"]["removalId"],
                         pending["removal"]["removalId"])
        self.assertEqual(self.remove(worker, replacement=original_target)["removal"]["targetParticipantId"],
                         new_target["id"])
        self.app.removal.advance(pending["removal"]["removalId"])
        self.assertEqual(self.snapshot(created).task(created["workItemId"]).owner_id, new_target["id"])

    def test_stopped_controller_waits_for_another_executors_running_turn_to_drain(self):
        self.add_third()
        self.service.rooms.update_config(self.room["id"], {"routingPolicy": "jev"})
        created = self.create()
        worker = self.move_root_to_worker(created)
        self.app.tick()
        task = self.snapshot(created).task(created["workItemId"])
        self.assertTrue(task.accepted_turn_id)
        controller = self.room["participants"][0]
        request = self.remove(controller, stopRoot=True)
        self.app.removal.advance(request["removal"]["removalId"])
        self.assertTrue(self.app.lifecycle.policy(created["graphId"])["stopped"])
        with self.app.ledger.connection() as conn:
            claim = conn.execute("SELECT 1 FROM agent_jev_executor_claims WHERE graph_id=? AND session_id=?",
                (created["graphId"], worker["sessionId"])).fetchone()
        self.assertIsNotNone(claim)
        self.app.removal.advance(request["removal"]["removalId"])
        self.assertEqual(self.service.rooms.participant(controller["id"])["status"], "active")
        self.assertEqual(self.app.removal.projection(self.room["id"])[0]["stage"], "awaiting_stop")

    def test_retarget_during_finalization_rechecks_destination_before_membership_write(self):
        original_target = self.add_third()
        self.create()
        worker = self.room["participants"][1]
        controller = self.room["participants"][0]
        pending = self.remove(worker, replacement=original_target)
        lifecycle = self.service.room_management.participants
        original_busy = lifecycle.session_is_busy
        changed = False

        def retarget_then_check(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                self.service.remove_room_participant(self.room["id"], {
                    "participantId": worker["id"], "clientMessageId": "retarget-before-final-write",
                    "replacementParticipantId": controller["id"]})
            return original_busy(*args, **kwargs)

        with patch.object(lifecycle, "session_is_busy", side_effect=retarget_then_check):
            self.app.removal.advance(pending["removal"]["removalId"])
        self.assertEqual(self.service.rooms.participant(worker["id"])["status"], "active")
        current = self.app.removal.projection(self.room["id"])[0]
        self.assertEqual(current["targetParticipantId"], controller["id"])
        self.app.removal.advance(pending["removal"]["removalId"])
        self.assertEqual(self.service.rooms.participant(worker["id"])["status"], "removed")

    def test_first_remove_after_stop_keeps_idle_controller_until_other_executor_drains(self):
        replacement = self.add_third()
        self.service.rooms.update_config(self.room["id"], {"routingPolicy": "jev"})
        created = self.create()
        worker = self.move_root_to_worker(created)
        self.app.tick()
        self.assertTrue(self.snapshot(created).task(created["workItemId"]).accepted_turn_id)
        controller = self.room["participants"][0]
        self.assertEqual(self.service.sessions.get(controller["sessionId"])["status"], "idle")
        self.app.stop(self.room["id"], created["rootId"])
        with self.app.ledger.connection() as conn:
            claim = conn.execute("SELECT 1 FROM agent_jev_executor_claims "
                "WHERE graph_id=? AND session_id=?", (created["graphId"], worker["sessionId"])).fetchone()
        self.assertIsNotNone(claim)
        response = self.remove(controller, replacement=replacement)
        self.assertEqual(response["status"], "pending")
        self.app.removal.advance(response["removal"]["removalId"])
        self.assertEqual(self.app.removal.projection(self.room["id"])[0]["stage"], "awaiting_stop")
        self.assertEqual(self.service.rooms.participant(controller["id"])["status"], "active")
        terminal = {"eventId": "post-stop-terminal", "eventType": "turn_completed", "status": "completed"}
        with patch.object(self.app, "execution_terminal", return_value=terminal):
            self.app.reconcile_graph(self.app.binding_by_graph(created["graphId"]))
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_executor_claims "
                "WHERE graph_id=?", (created["graphId"],)).fetchone()[0], 0)
        self.service.sessions.set_status(worker["sessionId"], "idle")
        with patch.object(self.service.room_management.participants,
                          "active_runtime_session_ids", return_value=set()):
            self.app.removal.advance(response["removal"]["removalId"])
        self.assertEqual(self.service.rooms.participant(controller["id"])["status"], "removed")

    def test_first_remove_after_stop_keeps_worker_with_own_undrained_claim(self):
        replacement = self.add_third()
        self.service.rooms.update_config(self.room["id"], {"routingPolicy": "jev"})
        created = self.create()
        worker = self.move_root_to_worker(created)
        self.app.tick()
        self.app.stop(self.room["id"], created["rootId"])
        self.assertNotEqual(self.app.binding_by_graph(created["graphId"])["controller_participant_id"],
                            worker["id"])
        with self.app.ledger.connection() as conn:
            claim = conn.execute("SELECT 1 FROM agent_jev_executor_claims "
                "WHERE graph_id=? AND session_id=?", (created["graphId"], worker["sessionId"])).fetchone()
        self.assertIsNotNone(claim)
        response = self.remove(worker, replacement=replacement)
        self.assertEqual(response["status"], "pending")
        self.app.removal.advance(response["removal"]["removalId"])
        self.assertEqual(self.app.removal.projection(self.room["id"])[0]["stage"], "awaiting_stop")
        self.assertEqual(self.service.rooms.participant(worker["id"])["status"], "active")

    def test_stopped_root_without_outstanding_effect_uses_legacy_removal(self):
        self.add_third()
        self.service.rooms.update_config(self.room["id"], {"routingPolicy": "jev"})
        created = self.create()
        self.app.stop(self.room["id"], created["rootId"])
        controller = self.room["participants"][0]
        response = self.remove(controller)
        self.assertEqual(response["schemaVersion"], "rag-ime.agent-room-participant-remove.v1")
        self.assertNotIn("removal", response)
        self.assertEqual(response["participant"]["status"], "removed")
        self.assertEqual(self.app.removal.projection(self.room["id"]), [])
