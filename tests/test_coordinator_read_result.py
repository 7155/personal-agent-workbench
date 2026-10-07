from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.agent_tools import ControlToolGateway
from rag_ime.pi.config import PiRuntimeConfig
from tests.sqlite_fixtures import copy_current_database


class CoordinatorReadResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-owned-result-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        db = self.root / "test.sqlite"
        copy_current_database(db)
        self.service = AgentService(db_path=db, runtime_config=PiRuntimeConfig(enabled=False, executable=None,
            agent_dir=self.root / "config", session_dir=self.root / "sessions", logs_dir=self.root / "logs"))
        self.addCleanup(self.service.close)
        with patch.object(self.service.runtime, "require_session_engine"):
            self.source = self.service.ensure_coordinator({})["sourceSessionId"]
        self.target = self.command("create_session", clientRequestId="worker", input={"task": "Read notes"})["target"]["id"]

    def command(self, action, **fields):
        return self.service.coordinator_command({"sourceSessionId": self.source, "action": action, **fields})

    def accept(self, turn="turn-old", client="input-old"):
        receipts = self.service.command_receipts
        claim = receipts.begin(command_scope="session_prompt", scope_id=self.target, client_message_id=client, payload={"message": client})
        receipts.record_acceptance_evidence(claim, command_scope="session_prompt", scope_id=self.target,
            client_message_id=client, accepted={"turnId": turn, "piEntryId": f"{turn}-user"})
        # Intentionally do not save final ACK: persistent acceptance is sufficient.

    def terminal(self, turn="turn-old", sequence=1, event_type="turn_completed", status="completed"):
        self.service.sessions.record_runtime_event(event_id=f"event-{turn}", session_id=self.target, turn_id=turn,
            sequence=sequence, event_type=event_type, created_at_ms=1234, redacted_summary=status)

    def transcript(self, text="Original final reply"):
        entries = [{"type": "session", "id": "physical"}]
        parent = "physical"
        for turn, client, body in [("turn-old", "input-old", text), ("turn-new", "input-new", "New unrelated reply")]:
            for entry in [{"type": "custom", "id": f"{turn}-binding", "customType": "rag-ime.pi-turn-binding",
                "data": {"schemaVersion": "rag-ime.pi-turn-binding.v1", "turnId": turn, "clientMessageId": client}},
                {"type": "message", "id": f"{turn}-user", "message": {"role": "user", "content": client}},
                {"type": "message", "id": f"{turn}-assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": body}]}}]:
                entry["parentId"] = parent
                entries.append(entry)
                parent = entry["id"]
        path = self.root / "sessions" / "worker.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        self.service.sessions.bind_runtime_session(self.target, driver_id="managed-pi", runtime_kind="pi_rpc",
            external_session_id="physical", transcript_ref=str(path), branch_anchor=parent, binding_state="active", message_count=4)
        return path

    def result(self, **input):
        return self.command("read_result", targetId=self.target, input=input or {"turnId": "turn-old", "clientMessageId": "input-old"})

    def test_original_final_message_without_audited_goal_is_read_from_real_transcript(self):
        self.accept()
        self.terminal()
        path = self.transcript()
        before = path.read_bytes()
        with patch.object(self.service.runtime, "_host", side_effect=AssertionError("must not start Host")), \
             patch.object(self.service.runtime, "prompt", side_effect=AssertionError("must not execute")), \
             patch.object(self.service.runtime, "resume_session", side_effect=AssertionError("must not resume")):
            result = self.result()
        self.assertTrue(result["evidenceOnly"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["terminalRefs"][0]["eventId"], "event-turn-old")
        self.assertEqual(result["finalMessages"][0]["messageId"], "turn-old-assistant")
        self.assertEqual(result["finalMessages"][0]["text"], "Original final reply")
        self.assertEqual(result["artifacts"], [])
        self.assertNotIn("completionAudit", result)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotEqual(self.service.sessions.agent_goal(self.target)["status"], "completed")

    def test_newer_turn_and_file_existence_do_not_replace_original_result(self):
        self.accept()
        self.accept("turn-new", "input-new")
        self.terminal()
        self.terminal("turn-new", 2)
        self.transcript("I created imaginary.txt")
        (self.root / "imaginary.txt").write_text("Exists independently")
        result = self.result()
        self.assertEqual(len(result["finalMessages"]), 1)
        self.assertNotIn("New unrelated", result["finalMessages"][0]["text"])
        self.assertEqual(result["artifacts"], [])

    def test_missing_acceptance_or_terminal_is_truthful(self):
        self.terminal()
        self.assertEqual(self.result()["state"], "unknown")
        self.accept("turn-pending", "input-pending")
        result = self.result(turnId="turn-pending", clientMessageId="input-pending")
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["terminalRefs"], [])
        self.assertEqual(result["finalMessages"], [])

    def test_original_persisted_user_event_recovers_acceptance_without_saved_command_ack(self):
        self.service.sessions.record_runtime_event(event_id="accepted-user", session_id=self.target, turn_id="turn-old",
            sequence=1, event_type="message_completed", created_at_ms=1222,
            metrics={"promptAcceptance": {"clientMessageId": "input-old", "turnId": "turn-old", "messageId": "turn-old-user"}})
        self.terminal(sequence=2)
        self.transcript()
        result = self.result()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["acceptanceRef"]["eventId"], "accepted-user")
        self.assertEqual(result["finalMessages"][0]["text"], "Original final reply")

    def test_mismatched_and_missing_exact_ids_are_rejected(self):
        self.accept()
        for value in [{"turnId": "wrong", "clientMessageId": "input-old"}, {"turnId": "turn-old"},
                      {"turnId": "turn-old", "clientMessageId": "input-old", "latest": True}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.result(**value)

    def test_nonowned_or_fake_source_cannot_read(self):
        other = self.service.create_session({})["session"]["id"]
        with self.assertRaisesRegex(ValueError, "not controlled"):
            self.command("read_result", targetId=other, input={"turnId": "x", "clientMessageId": "y"})
        with self.assertRaisesRegex(ValueError, "active persistent"):
            self.service.coordinator_command({"sourceSessionId": self.target, "action": "read_result", "targetId": self.target,
                "input": {"turnId": "x", "clientMessageId": "y"}})

    def test_aborted_is_not_completed_and_archived_owned_history_remains_readable(self):
        self.accept()
        self.terminal(status="aborted")
        self.transcript()
        self.service.sessions.archive(self.target)
        result = self.result()
        self.assertEqual(result["state"], "aborted")
        self.assertEqual(result["terminalRefs"][0]["status"], "aborted")
        self.assertNotIn("user_stopped", json.dumps(result))

    def test_reply_text_is_bounded_and_missing_history_does_not_erase_terminal(self):
        self.accept()
        self.terminal()
        result = self.result()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["finalMessages"], [])
        self.assertTrue(result["messagesUnavailable"])
        self.transcript("x" * 20000)
        result = self.result()
        self.assertLessEqual(len(result["finalMessages"][0]["text"]), 8000)
        self.assertTrue(result["finalMessages"][0]["truncated"])

    def test_room_reads_only_original_turn_events_without_claiming_global_assets(self):
        room = self.command("create_room", clientRequestId="room", input={"task": "Read together", "routingPolicy": "manual_mentions",
            "participants": [{"roleId": "companion-present-v1", "collaborationRole": "coordinator"},
                             {"roleId": "companion-future-v1", "collaborationRole": "reviewer"}]})["target"]
        self.service.rooms.append_event(room_id=room["id"], event_type="user_message", turn_id="room-original", payload={"message": "Read"})
        from rag_ime.agent_event_projection import room_event_projection
        from rag_ime.agent_protocol import AgentEventEnvelope
        event_type, payload = room_event_projection(AgentEventEnvelope(event_id="native-room-message", session_id=room["participants"][0]["sessionId"],
            turn_id="participant-turn", sequence=1, event_type="message_completed", created_at_ms=123, resume_token="native-room-message",
            payload={"message": {"id": "native-final", "role": "assistant", "blocks": [{"type": "text", "data": {"text": "Original Room reply"}}]}}))
        message = self.service.rooms.append_event(room_id=room["id"], event_type=event_type, turn_id="room-original",
            participant_id=room["participants"][0]["id"], payload=payload)
        terminal = self.service.rooms.append_event(room_id=room["id"], event_type="turn_completed", turn_id="room-original",
            participant_id=room["participants"][0]["id"], payload={"status": "completed"})
        self.service.rooms.append_event(room_id=room["id"], event_type="participant_message", turn_id="room-new", payload={"text": "New reply"})
        with patch.object(self.service, "room_artifacts", side_effect=AssertionError("whole Room assets cannot prove this turn")):
            result = self.command("read_result", targetId=room["id"], input={"roomTurnId": "room-original"})
        self.assertEqual(result["finalMessages"][0]["eventId"], message["eventId"])
        self.assertEqual(result["finalMessages"][0]["messageId"], "native-final")
        self.assertEqual(result["finalMessages"][0]["text"], "Original Room reply")
        self.assertEqual(result["terminalRefs"][0]["eventId"], terminal["eventId"])
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(result["state"], "unknown")  # Participant completed is not overall Root completed.
        self.assertTrue(result["evidenceOnly"])

    def test_managed_artifact_reference_requires_exact_original_message_and_receipt(self):
        from rag_ime.agent_blocks import normalize_trusted_agent_blocks
        from rag_ime.agent_tool_artifacts import managed_file_block
        self.accept()
        self.terminal()
        self.transcript()
        receipt = self.service.media.import_bytes(session_id=self.target, data=b"verified result", mime_type="text/plain",
            file_name="result.txt", origin="tool_result", origin_tool="workspace_write", origin_receipt_id="verified-write")
        blocks = normalize_trusted_agent_blocks([managed_file_block(receipt)], source_kind="tool_receipt", source_ref="verified-write")
        message = {"sessionId": self.target, "id": "turn-new-assistant", "turnId": "turn-new", "role": "assistant", "blocks": list(blocks)}
        self.service.agent_blocks.persist_message(message)
        self.assertEqual(self.result()["artifacts"], [])
        self.service.agent_blocks.persist_message({**message, "id": "turn-old-assistant", "turnId": "turn-old"})
        result = self.result()
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertEqual(result["artifacts"][0]["mediaId"], receipt["mediaId"])
        self.assertEqual(result["artifacts"][0]["originReceiptId"], "verified-write")
        self.assertEqual(result["artifacts"][0]["relation"], "referenced_in_original_message")

    def test_persistent_original_tool_references_are_content_free_and_other_turn_is_excluded(self):
        self.accept()
        self.service.sessions.record_runtime_event(event_id="original-tool", session_id=self.target, turn_id="turn-old", sequence=1,
            event_type="tool_finished", created_at_ms=1230, metrics={"toolIdentity": {"toolCallId": "original-call", "toolName": "workspace_read"}})
        self.service.sessions.record_runtime_event(event_id="other-tool", session_id=self.target, turn_id="turn-new", sequence=2,
            event_type="tool_finished", created_at_ms=1231, metrics={"toolIdentity": {"toolCallId": "other-call", "toolName": "workspace_write"}})
        self.terminal(sequence=3)
        result = self.result()
        self.assertEqual([item["eventId"] for item in result["toolRefs"]], ["original-tool"])
        self.assertEqual(result["toolRefs"][0]["toolCallId"], "original-call")
        self.assertNotIn("arguments", result["toolRefs"][0])
        self.assertNotIn("result", result["toolRefs"][0])

    def test_room_requires_exact_room_turn_and_missing_original_stays_unknown(self):
        room = self.command("create_room", clientRequestId="room-input", input={"task": "Read together", "routingPolicy": "manual_mentions",
            "participants": [{"roleId": "companion-present-v1", "collaborationRole": "coordinator"},
                             {"roleId": "companion-future-v1", "collaborationRole": "reviewer"}]})["target"]
        for value in [{}, {"roomTurnId": "old", "latest": True}, {"turnId": "old", "clientMessageId": "wrong"}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.command("read_result", targetId=room["id"], input=value)
        result = self.command("read_result", targetId=room["id"], input={"roomTurnId": "absent"})
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["finalMessages"], [])
        self.assertEqual(result["terminalRefs"], [])

    def test_same_block_id_from_real_pi_producer_and_sidecar_is_returned_once(self):
        from rag_ime.agent_protocol import AgentEventEnvelope
        from rag_ime.pi.public import pi_message_payload
        self.accept()
        native = pi_message_payload({"id": "native-original-message", "role": "assistant", "content": ""},
            session_id=self.target, turn_id="turn-old", trusted_blocks=[
                {"id": "shared-block", "type": "text", "data": {"text": "Exactly once"}}]).to_payload()
        event = AgentEventEnvelope(event_id="original-message-event", session_id=self.target, turn_id="turn-old",
            sequence=1, event_type="message_completed", created_at_ms=1233, resume_token="original-message-event", payload={"message": native})
        self.service.event_projection_application.record(event)
        self.assertEqual(self.service.agent_blocks.blocks_for_message(self.target, "native-original-message")[0]["id"], "shared-block")
        self.terminal(sequence=2)
        with patch.object(self.service.runtime, "messages", return_value=[native]):
            result = self.result()
        self.assertEqual(result["finalMessages"][0]["text"], "Exactly once")

    def test_distinct_original_block_ids_with_identical_text_are_preserved(self):
        from rag_ime.pi.public import pi_message_payload
        self.accept()
        self.terminal()
        native = pi_message_payload({"id": "native-distinct-message", "role": "assistant", "content": ""},
            session_id=self.target, turn_id="turn-old", trusted_blocks=[
                {"id": "first-block", "type": "text", "data": {"text": "Same text"}},
                {"id": "second-block", "type": "text", "data": {"text": "Same text"}}]).to_payload()
        self.service.agent_blocks.persist_message(native)
        with patch.object(self.service.runtime, "messages", return_value=[native]):
            result = self.result()
        self.assertEqual(result["finalMessages"][0]["text"], "Same text\nSame text")

    def test_unknown_terminal_status_is_not_promoted_to_completed(self):
        self.accept()
        self.terminal(status="future_or_unrecognized_status")
        self.transcript()
        result = self.result()
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["terminalRefs"][0]["status"], "future_or_unrecognized_status")
        self.assertEqual(result["finalMessages"][0]["text"], "Original final reply")

    def test_statusless_terminal_preserves_unknown_and_original_terminal_reference(self):
        self.accept()
        self.terminal(status="")  # A retained event type is not a retained disposition.
        result = self.result()
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["terminalRefs"][0]["status"], "")
        self.assertEqual(result["reason"], "original_terminal_status_not_retained")

    def test_room_total_text_budget_marks_top_level_truncated(self):
        room = self.command("create_room", clientRequestId="room-budget", input={"task": "Read together", "routingPolicy": "manual_mentions",
            "participants": [{"roleId": "companion-present-v1", "collaborationRole": "coordinator"},
                             {"roleId": "companion-future-v1", "collaborationRole": "reviewer"}]})["target"]
        for character in ("a", "b"):
            self.service.rooms.append_event(room_id=room["id"], event_type="participant_message", turn_id="room-budget-turn",
                participant_id=room["participants"][0]["id"], payload={"text": character * 6000})
        result = self.command("read_result", targetId=room["id"], input={"roomTurnId": "room-budget-turn"})
        self.assertEqual(sum(len(message["text"]) for message in result["finalMessages"]), 8000)
        self.assertTrue(any(message["truncated"] for message in result["finalMessages"]))
        self.assertTrue(result["truncated"])

    def test_runtime_schema_and_actual_gateway_envelope_expose_read_result(self):
        self.accept()
        self.terminal()
        gateway = ControlToolGateway(sessions=self.service.sessions, management=object(), core=object(), project="test", collaboration=self.service)
        manifest = next(x for x in gateway.runtime_manifests(self.service.sessions.get(self.source)) if x["name"] == "agents")
        self.assertIn("read_result", manifest["parameters"]["properties"]["action"]["enum"])
        result = gateway.execute({"schemaVersion": "rag-ime.agent-tool-call.v1", "sessionId": self.source, "toolCallId": "read-original",
            "tool": "agents", "args": {"op": "coordinator", "action": "read_result", "targetId": self.target,
                "input": {"turnId": "turn-old", "clientMessageId": "input-old"}}})
        self.assertEqual(result["operation"], "coordinator")
        self.assertTrue(result["result"]["evidenceOnly"])
