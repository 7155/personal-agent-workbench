from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.pi.config import PiRuntimeConfig


class RoomControlRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-room-retention-")
        self.root = Path(self.tmp.name)
        self.service = AgentService(
            db_path=self.root / "test.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False, executable=None,
                agent_dir=self.root / "config", session_dir=self.root / "sessions",
                logs_dir=self.root / "logs",
            ),
        )
        self.room = self.service.create_room({
            "title": "Retention regression", "workspaceRoots": [str(self.root)],
            "participants": [
                {"roleId": "companion-present-v1", "roleVersion": "1"},
                {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
            ],
        })["room"]
        self.room_id = str(self.room["id"])
        self.participant = self.room["participants"][0]
        self.session_id = str(self.participant["sessionId"])

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def start(self, key: str = "first") -> dict[str, object]:
        with patch.object(self.service, "prompt", return_value={"turnId": f"pi:{key}"}):
            return self.service.post_room_message(self.room_id, {
                "message": "Check the fixture", "clientMessageId": key,
            })

    def append(self, root: str, kind: str, payload: dict[str, object], **kwargs):
        return self.service.rooms.append_event(
            room_id=self.room_id, turn_id=root, event_type=kind, payload=payload,
            participant_id=str(self.participant["id"]),
            source_session_id=self.session_id, **kwargs,
        )

    def prune_anchor(self, root: str) -> None:
        for i in range(105):
            self.append(root, "participant_activity", {"summary": str(i)}, retain_per_room=100)
        self.assertFalse(any(e["eventType"] == "user_message" for e in
                             self.service.rooms.list_events(self.room_id)))

    def test_snapshot_keeps_terminal_of_every_root_represented_in_tail(self) -> None:
        accepted = self.start()
        old_root = str(accepted["roomTurnId"])
        terminal = self.append(old_root, "turn_completed", {"status": "completed"})
        self.service.room_turns.finish(self.session_id, "pi:first", old_root)
        latest = self.start("second")
        root = str(latest["roomTurnId"])
        for i in range(210):
            self.append(root, "participant_activity", {"summary": str(i)})
        self.append(root, "turn_completed", {"status": "completed"})
        self.append(old_root, "participant_activity", {
            "data": {"recoveredExecutionTerminal": True, "status": "completed"},
        })
        snapshot = self.service.rooms.snapshot(self.room_id)
        self.assertIn(terminal["eventId"], [e["eventId"] for e in snapshot["events"]])
        self.assertEqual(snapshot["events"][0]["turnId"], old_root)

    def test_active_root_can_be_steered_after_its_anchor_is_pruned(self) -> None:
        accepted = self.start()
        root = str(accepted["roomTurnId"])
        self.prune_anchor(root)
        with patch.object(self.service, "prompt", return_value={"turnId": "pi:first", "queued": True}) as prompt:
            result = self.service.steer_room_participant(self.room_id, {
                "action": "steer_participant", "rootId": root,
                "participantId": self.participant["id"], "message": "Check cancellation",
                "clientActionId": "steer-after-retention",
            })
        self.assertTrue(result["accepted"])
        prompt.assert_called_once()

    def test_terminal_root_can_be_stopped_after_its_anchor_is_pruned(self) -> None:
        accepted = self.start()
        root = str(accepted["roomTurnId"])
        self.prune_anchor(root)
        self.append(root, "turn_completed", {"status": "completed"})
        self.service.room_turns.finish(self.session_id, "pi:first", root)
        with patch.object(self.service, "abort") as abort:
            result = self.service.abort_room_turn(self.room_id, {
                "roomTurnId": root, "clientRequestId": "abort-after-retention",
            })
        self.assertEqual(result["status"], "already_terminal")
        abort.assert_not_called()

    def test_steer_of_finished_root_cannot_start_an_unbound_session_turn(self) -> None:
        accepted = self.start()
        root = str(accepted["roomTurnId"])
        self.service.room_turns.finish(self.session_id, "pi:first", root)
        before = self.service.rooms.get(self.room_id)["lastEventSequence"]
        with patch.object(self.service, "prompt") as prompt:
            with self.assertRaisesRegex(ValueError, "当前执行轮次"):
                self.service.steer_room_participant(self.room_id, {
                    "action": "steer_participant", "rootId": root,
                    "participantId": self.participant["id"], "message": "Do not misroute",
                    "clientActionId": "stale-steer",
                })
        prompt.assert_not_called()
        self.assertEqual(self.service.rooms.get(self.room_id)["lastEventSequence"], before)

    def test_unrelated_root_remains_rejected(self) -> None:
        self.start()
        with patch.object(self.service, "abort") as abort:
            with self.assertRaisesRegex(ValueError, "does not belong"):
                self.service.abort_room_turn(self.room_id, {
                    "roomTurnId": "room-turn:unrelated", "clientRequestId": "wrong-root",
                })
        abort.assert_not_called()

    def test_session_send_conflict_does_not_strand_room_admission(self) -> None:
        accepted = self.start()
        root = str(accepted["roomTurnId"])
        with patch.object(self.service.runtime, "prompt") as prompt:
            with self.assertRaises(ValueError) as failure:
                self.service.prompt(self.session_id, {
                    "message": "Direct Session input", "clientMessageId": "direct-conflict",
                })
        self.assertEqual(getattr(failure.exception, "cause_code", ""), "AGENT_TURN_CONFLICT")
        prompt.assert_not_called()
        self.assertEqual(self.service.room_turns.active_turn(self.session_id)[0], root)
        self.service.events.publish(self.session_id, "turn_completed", {}, turn_id="pi:first")
        self.assertTrue(self.service.events.flush())
        self.assertEqual(self.service.room_turns.active_turn(self.session_id), ("", ""))
        self.assertTrue(self.start("after-session-conflict")["accepted"])

    def test_abort_reaches_active_wake_after_an_earlier_dispatch_completed(self) -> None:
        accepted = self.start()
        root = str(accepted["roomTurnId"])
        self.append(root, "turn_completed", {"status": "completed"})
        self.service.room_turns.finish(self.session_id, "pi:first", root)
        self.service.room_turns.begin(self.session_id, root, dispatch_id="dispatch:wake")
        self.service.room_turns.accept(self.session_id, "pi:wake", root)
        with patch.object(self.service, "abort", return_value={"ok": True, "sessionId": self.session_id}) as abort:
            self.service.abort_room_turn(self.room_id, {
                "roomTurnId": root, "clientRequestId": "abort-live-wake",
            })
        abort.assert_called_once_with(self.session_id)

    def test_old_root_abort_cannot_cancel_a_different_session_turn(self) -> None:
        accepted = self.start()
        root = str(accepted["roomTurnId"])
        self.prune_anchor(root)
        self.service.room_turns.finish(self.session_id, "pi:first", root)
        self.service.room_turns.begin(self.session_id, "root:new", dispatch_id="dispatch:new")
        self.service.room_turns.accept(self.session_id, "pi:new", "root:new")
        with patch.object(self.service, "abort") as abort:
            result = self.service.abort_room_turn(self.room_id, {
                "roomTurnId": root, "clientRequestId": "old-root-abort",
            })
        abort.assert_not_called()
        self.assertEqual(result["status"], "already_terminal")
        self.assertEqual(self.service.room_turns.active_turn(self.session_id)[0], "root:new")
