from __future__ import annotations

import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_agent_coordinator_work as work_cases
from tests import test_agent_coordinator_delivery as delivery_cases
from rag_ime.agent_command_receipts import AgentCommandReceiptPending
from rag_ime.agent_service import AgentService
from concurrent.futures import ThreadPoolExecutor


class CoordinatorRoomWorkTests(unittest.TestCase):
    setUp = work_cases.CoordinatorWorkTests.setUp
    command = work_cases.CoordinatorWorkTests.command
    items = work_cases.CoordinatorWorkTests.items
    attempts = work_cases.CoordinatorWorkTests.attempts

    def room(self, client="room-create"):
        return self.command("create_room", clientRequestId=client, input={
            "task": "Read original Room evidence", "routingPolicy": "manual_mentions",
            "participants": [{"roleId": "companion-present-v1", "collaborationRole": "coordinator"},
                             {"roleId": "companion-future-v1", "collaborationRole": "reviewer"}]})["target"]

    def post(self, room_id, payload, root="original-root"):
        client = payload["clientMessageId"]
        claim = self.service.command_receipts.begin(command_scope="room_message", scope_id=room_id,
            client_message_id=client, payload=dict(payload))
        self.service.rooms.append_event(room_id=room_id, event_type="user_message", turn_id=root,
            payload={"text": payload["message"], "clientMessageId": client})
        response = {"ok": True, "accepted": True, "roomId": room_id, "roomTurnId": root,
                    "clientMessageId": client}
        return self.service.command_receipts.complete(claim, command_scope="room_message", scope_id=room_id,
            client_message_id=client, response=response)

    def send_room(self, room, client="original-client", callback=None):
        with patch.object(self.service, "post_room_message", side_effect=callback or self.post):
            return self.command("prompt", targetId=room["id"],
                input={"message": "Original task", "clientMessageId": client})

    def final(self, room, root="original-root", content="Original Room Root report"):
        value = {"finalizationId": "jev-final:" + root, "content": content,
                 "status": "completed", "createdAtMs": 456}
        snapshot = SimpleNamespace(graph_id=root, room_id=room["id"], root_id=root,
                                   participant_id=room["participants"][0]["id"])
        # Real publication/persistence; an explicit finalized-policy seam,
        # not a model execution or a invented coordinator settlement.
        with patch.object(self.service.jev_application.lifecycle, "policy",
                          return_value={"final_json": json.dumps(value), "stopped": 0, "epoch": 1}):
            self.service.jev_application.lifecycle.publish_final(snapshot)

    def test_original_room_attempt_is_journaled_before_original_dispatch(self):
        room = self.room()
        def dispatch(target, payload):
            self.assertEqual(len(self.attempts()), 1)
            attempt = self.attempts()[0]
            self.assertEqual(attempt["targetRoomId"], room["id"])
            self.assertEqual(attempt["clientMessageId"], "original-client")
            self.assertEqual(attempt["roomTurnId"], "")
            return self.post(target, payload)
        self.send_room(room, callback=dispatch)
        self.assertEqual(self.attempts()[0]["roomTurnId"], "original-root")

    def test_original_root_is_returned_once_to_original_source(self):
        room = self.room()
        self.send_room(room)
        self.final(room)
        self.final(room, root="newer-root", content="Do not substitute newer result")
        with patch.object(self.service.runtime, "_host", side_effect=AssertionError("passive harvest cannot open Host")):
            self.assertEqual(self.service.coordinator_work.reconcile_once(limit=10), 1)
            self.assertEqual(self.service.coordinator_work.reconcile_once(limit=10), 0)
        self.assertEqual(len(self.items()), 1)
        item = self.items()[0]
        self.assertEqual(item["payload"]["sourceSessionId"], self.source)
        self.assertEqual(item["payload"]["targetRoomId"], room["id"])
        self.assertEqual(item["payload"]["result"]["execution"]["roomTurnId"], "original-root")
        self.assertEqual(item["payload"]["result"]["finalMessages"][0]["text"], "Original Room Root report")
        self.assertEqual(item["lifecycle"], "until_ack")
        self.assertEqual(item["payload"]["authority"], "evidence_only")

    resident = delivery_cases.CoordinatorDeliveryTests.resident
    rows = delivery_cases.CoordinatorDeliveryTests.rows

    def harvest_room(self):
        room = self.room()
        self.send_room(room)
        self.final(room)
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        return room, self.items()[0]

    def test_lost_returned_ack_and_reopen_never_redispatch_original_room(self):
        room = self.room()
        def lost(target, payload):
            self.post(target, payload)
            raise RuntimeError("known native acceptance; returned ACK lost")
        with self.assertRaises(RuntimeError):
            self.send_room(room, callback=lost)
        self.service.close()
        self.service = AgentService(db_path=self.db, runtime_config=self.config)
        self.addCleanup(self.service.close)
        with patch.object(self.service, "post_room_message", side_effect=AssertionError("cannot redispatch")):
            replay = self.command("prompt", targetId=room["id"],
                input={"message": "Original task", "clientMessageId": "original-client"})
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(replay["roomTurnId"], "original-root")
        self.final(room)
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        self.assertEqual(len(self.items()), 1)

    def test_unknown_dispatch_before_any_receipt_is_not_replayed_or_harvested(self):
        room = self.room()
        with self.assertRaises(RuntimeError):
            self.send_room(room, callback=lambda *_: (_ for _ in ()).throw(RuntimeError("unknown")))
        with patch.object(self.service, "post_room_message", side_effect=AssertionError("cannot repeat unknown")):
            with self.assertRaises(AgentCommandReceiptPending):
                self.command("prompt", targetId=room["id"], input={"message": "Original task", "clientMessageId": "original-client"})
        self.final(room)
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.items(), [])

    def test_repeat_callback_and_changed_input_cannot_create_a_second_attempt(self):
        room = self.room()
        self.send_room(room)
        with patch.object(self.service, "post_room_message", side_effect=AssertionError("duplicate effect")):
            replay = self.command("prompt", targetId=room["id"], input={"message": "Original task", "clientMessageId": "original-client"})
            self.assertTrue(replay["idempotentReplay"])
            with self.assertRaises(ValueError):
                self.command("prompt", targetId=room["id"], input={"message": "Different task", "clientMessageId": "original-client"})
        self.assertEqual(len(self.attempts()), 1)

    def test_preexisting_room_client_cannot_be_borrowed_by_source(self):
        room = self.room()
        self.post(room["id"], {"message": "Original task", "clientMessageId": "original-client"})
        with self.assertRaisesRegex(ValueError, "predates"):
            self.send_room(room)
        self.final(room)
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.attempts(), [])

    def test_other_source_cannot_register_owned_room(self):
        room = self.room()
        with self.assertRaises(ValueError):
            self.service.coordinator_work.register_room_prompt(self.target, room["id"], {"message": "Task", "clientMessageId": "x"})
        self.assertEqual(self.attempts(), [])

    def test_shared_client_string_in_different_rooms_has_independent_original_roots(self):
        first, second = self.room("first-room"), self.room("second-room")
        self.send_room(first, callback=lambda target, payload: self.post(target, payload, "first-root"))
        self.send_room(second, callback=lambda target, payload: self.post(target, payload, "second-root"))
        self.final(second, "second-root", "Second Room report")
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        self.assertEqual(self.items()[0]["payload"]["targetRoomId"], second["id"])
        self.final(first, "first-root", "First Room report")
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        self.assertEqual({i["payload"]["roomTurnId"] for i in self.items()}, {"first-root", "second-root"})

    def test_participant_terminal_does_not_invent_room_root_settlement(self):
        room = self.room()
        self.send_room(room)
        self.service.rooms.append_event(room_id=room["id"], event_type="turn_completed", turn_id="original-root",
            participant_id=room["participants"][0]["id"], source_session_id=room["participants"][0]["sessionId"],
            payload={"status": "completed", "rootId": "original-root"})
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.items(), [])

    def test_wrong_root_in_accepted_receipt_never_rebinds_original_attempt(self):
        room = self.room()
        self.send_room(room)
        with self.service.sessions._connect() as conn:
            conn.execute("UPDATE agent_command_receipts SET response_json=? WHERE command_scope='room_message' AND scope_id=?",
                (json.dumps({"roomId": room["id"], "clientMessageId": "original-client", "roomTurnId": "foreign-root"}), room["id"]))
        self.final(room, "foreign-root")
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.attempts()[0]["roomTurnId"], "original-root")
        self.assertEqual(self.attempts()[0]["lastErrorCode"], "ValueError")

    def test_atomic_concurrent_harvest_enqueues_original_root_once(self):
        room = self.room()
        self.send_room(room)
        self.final(room)
        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(lambda _: self.service.coordinator_work.reconcile_once(), range(2)))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(len(self.items()), 1)

    def test_archive_during_passive_read_retires_without_source_inbox_write(self):
        room = self.room()
        self.send_room(room)
        self.final(room)
        original = self.service.coordinator_work.read_room_result
        def retire(target, root):
            result = original(target, root)
            self.service.sessions.set_status(self.source, "archived")
            return result
        with patch.object(self.service.coordinator_work, "read_room_result", side_effect=retire):
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.attempts()[0]["retiredReason"], "ownership_retired")

    def test_original_room_result_uses_same_source_native_admission_once(self):
        room, item = self.harvest_room()
        native = self.resident()
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 1)
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        prompts = [params for method, params in native.calls if method == "session.prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["sessionId"], self.source)
        self.assertIn("Original Room Root report", prompts[0]["message"])
        self.assertEqual(self.rows()[0]["target_room_id"], room["id"])
        self.assertEqual(self.rows()[0]["target_session_id"], "")
        self.assertEqual(self.rows()[0]["phase"], "accepted")
        with self.service.sessions._read_connect() as conn:
            self.assertEqual(conn.execute("SELECT acknowledged_at_ms FROM agent_context_items WHERE item_id=?", (item["itemId"],)).fetchone()[0], None)

    def test_stop_before_source_native_write_does_not_wake_or_redispatch_room(self):
        self.harvest_room()
        native = self.resident(stop_before_write=True)
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        self.assertEqual([m for m, _ in native.calls if m == "session.prompt"], [])
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        self.assertEqual(len(self.attempts()), 1)

    def test_expired_or_acknowledged_room_result_does_not_automatically_wake(self):
        _, item = self.harvest_room()
        native = self.resident()
        self.service.context_runtime.acknowledge(self.source, item["itemId"])
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        self.assertEqual(native.calls, [])
        self.assertEqual(self.rows()[0]["phase"], "retired")

    def test_room_and_source_delivery_identities_are_immutable_tombstones(self):
        room, _ = self.harvest_room()
        self.service.coordinator_delivery._index_results()
        with self.service.sessions._connect() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE agent_coordinator_room_work_attempts SET root_id='other-root'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM agent_coordinator_room_work_attempts")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE agent_coordinator_result_deliveries SET target_room_id='other-room'")
        self.assertEqual(self.attempts()[0]["targetRoomId"], room["id"])

    def test_expired_original_room_result_never_admits_a_source_notice(self):
        _, item = self.harvest_room()
        native = self.resident()
        with self.service.sessions._connect() as conn:
            conn.execute("UPDATE agent_context_items SET expires_at_ms=1 WHERE item_id=?", (item["itemId"],))
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        self.assertEqual(native.calls, [])
        self.assertEqual(self.rows()[0]["retired_reason"], "result_withdrawn")

    def test_dense_context_still_includes_exact_original_room_item(self):
        for index in range(32):
            self.service.context_runtime.enqueue(session_id=self.source, source_kind="manual", lane="fact",
                lifecycle="persistent", title=f"Public fact {index}", payload={"fact": index})
        self.harvest_room()
        native = self.resident()
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 1)
        message = [p["message"] for m, p in native.calls if m == "session.prompt"][0]
        self.assertIn("Original Room Root report", message)
        self.assertEqual(len(self.rows()), 1)

    def test_room_receipt_with_foreign_room_or_client_is_never_harvested(self):
        room = self.room()
        self.send_room(room)
        self.final(room)
        with self.service.sessions._connect() as conn:
            conn.execute("UPDATE agent_command_receipts SET response_json=? WHERE command_scope='room_message' AND scope_id=?",
                (json.dumps({"roomId": "foreign-room", "clientMessageId": "original-client", "roomTurnId": "original-root"}), room["id"]))
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.attempts()[0]["lastErrorCode"], "ValueError")

    def test_unknown_source_notice_is_not_resubmitted_after_cold_reopen(self):
        self.harvest_room()
        native = self.resident(lost_ack=True)
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        self.assertEqual(len([m for m, _ in native.calls if m == "session.prompt"]), 1)
        self.assertEqual(self.rows()[0]["phase"], "uncertain")
        self.service.close()
        self.service = AgentService(db_path=self.db, runtime_config=self.config)
        self.addCleanup(self.service.close)
        with patch.object(self.service, "_deliver_coordinator_result", side_effect=AssertionError("uncertain Source effect cannot replay")):
            self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        self.assertEqual(self.rows()[0]["phase"], "uncertain")

    def test_stopped_recoverable_source_does_not_automatically_wake_for_room_result(self):
        self.harvest_room()
        native = self.resident()
        # Existing Durable Stop/recovery eligibility is the original Runtime
        # gate; the Room harvester must not bypass it or open a replacement.
        self.service.runtime._states[self.source].recoverable = True
        for _ in range(2):
            self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        self.assertEqual(native.calls, [])
        self.assertEqual(self.rows()[0]["phase"], "pending")


class OriginalRoomReturnIntegration(unittest.TestCase):
    setUp = CoordinatorRoomWorkTests.setUp
    command = CoordinatorRoomWorkTests.command
    room = CoordinatorRoomWorkTests.room
    resident = CoordinatorRoomWorkTests.resident
    rows = CoordinatorRoomWorkTests.rows
    items = CoordinatorRoomWorkTests.items

    def original_room(self):
        room = self.room()
        moderator = next(p for p in room['participants'] if p['id'] == room['moderatorParticipantId'])
        payload = {'message': 'Original complex work', 'clientMessageId': 'original-room-client'}
        with patch.object(self.service, 'prompt', return_value={'accepted': True, 'turnId': 'original-pi-turn'}) as prompt:
            accepted = self.command('prompt', targetId=room['id'], input=payload)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(accepted['executionOwner'], 'session')
        original_root = accepted['roomTurnId']
        with patch.object(self.service, 'prompt', side_effect=AssertionError('Room input must never repeat')):
            replay = self.command('prompt', targetId=room['id'], input=payload)
        self.assertEqual(replay['roomTurnId'], original_root)
        self.assertTrue(replay['idempotentReplay'])
        self.service.execute_room_partner_tool(moderator['sessionId'],
            {'op': 'post', 'kind': 'result', 'content': 'Original ordinary Room formal report'}, tool_call_id='original-final')
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        item = self.items()[0]
        self.assertEqual(item['payload']['roomTurnId'], original_root)
        self.assertEqual(item['payload']['targetRoomId'], room['id'])
        self.assertEqual(item['payload']['sourceSessionId'], self.source)
        return room, original_root

    def test_ordinary_room_formal_result_returns_to_same_source_once(self):
        room, root = self.original_room()
        native = self.resident()
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 1)
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        prompts = [params for method, params in native.calls if method == 'session.prompt']
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]['sessionId'], self.source)
        self.assertIn('Original ordinary Room formal report', prompts[0]['message'])
        self.assertEqual(self.rows()[0]['target_room_id'], room['id'])
        self.assertEqual(self.rows()[0]['target_session_id'], '')
        self.assertEqual(self.rows()[0]['phase'], 'accepted')

    def test_original_source_stop_before_write_does_not_redispatch_or_notify(self):
        self.original_room()
        native = self.resident(stop_before_write=True)
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        self.assertEqual([method for method, _ in native.calls if method == 'session.prompt'], [])

    def test_original_source_unknown_ack_does_not_repeat_notice(self):
        self.original_room()
        native = self.resident(lost_ack=True)
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        self.assertEqual(self.service.coordinator_delivery.reconcile_once(), 0)
        self.assertEqual(len([method for method, _ in native.calls if method == 'session.prompt']), 1)
        self.assertEqual(self.rows()[0]['phase'], 'uncertain')
