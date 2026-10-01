from __future__ import annotations

import json
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

    def test_default_room_history_is_not_destructively_pruned(self) -> None:
        root_id = "jev-root:unbounded"
        user = self.service.rooms.append_event(
            room_id=self.room_id,
            event_type="user_message",
            payload={
                "mode": "jev",
                "graphId": "jev-graph:unbounded",
                "text": "保留完整 Root 锚点",
            },
            turn_id=root_id,
            created_at_ms=2,
        )
        for index in range(2_104):
            self.append(
                root_id,
                "participant_activity",
                {"summary": str(index)},
            )

        self.assertEqual(
            self.service.rooms.event_bounds(self.room_id),
            (1, 2_106),
        )
        snapshot = self.service.rooms.snapshot(self.room_id)
        self.assertEqual(snapshot["firstSequence"], 107)
        self.assertEqual(snapshot["lastSequence"], 2_106)
        self.assertEqual(len(snapshot["events"]), 2_000)
        self.assertTrue(snapshot["truncated"])

        conversation = self.service.rooms.conversation_snapshot(self.room_id)
        self.assertFalse(conversation["truncated"])
        self.assertEqual(conversation["firstEventSequence"], 1)
        self.assertIn(
            user["eventId"],
            [event["eventId"] for event in conversation["events"]],
        )

        with patch.object(
            self.service.rooms,
            "_validated_room_file_events",
            wraps=self.service.rooms._validated_room_file_events,
        ) as archive_reader:
            application_page = self.service.room_history(
                self.room_id,
                {"beforeSequence": 107, "limit": 100},
            )
        archive_reader.assert_not_called()
        self.assertEqual(application_page["firstSequence"], 7)
        self.assertEqual(application_page["lastSequence"], 106)

        earlier = self.service.rooms.history_page(
            self.room_id,
            before_sequence=107,
            limit=100,
            full_history=True,
        )
        self.assertEqual(earlier["firstSequence"], 7)
        self.assertEqual(earlier["lastSequence"], 106)
        self.assertTrue(earlier["hasMore"])
        first_page = self.service.rooms.history_page(
            self.room_id,
            before_sequence=7,
            limit=100,
            full_history=True,
        )
        self.assertEqual(first_page["firstSequence"], 1)
        self.assertEqual(first_page["lastSequence"], 6)
        self.assertIn(
            user["eventId"],
            [event["eventId"] for event in first_page["items"]],
        )

    def test_conversation_snapshot_recovers_pruned_jev_root_from_sidecar(self) -> None:
        root_id = "jev-root:sidecar"
        user = self.service.rooms.append_event(
            room_id=self.room_id,
            event_type="user_message",
            payload={
                "mode": "jev",
                "graphId": "jev-graph:sidecar",
                "text": "恢复原始 Jev Root",
            },
            turn_id=root_id,
            created_at_ms=2,
            retain_per_room=100,
        )
        for index in range(120):
            self.append(
                root_id,
                "participant_activity",
                {"summary": str(index)},
                created_at_ms=index + 3,
                retain_per_room=100,
            )

        self.assertNotIn(
            user["eventId"],
            [event["eventId"] for event in self.service.rooms.list_events(self.room_id)],
        )
        snapshot = self.service.rooms.conversation_snapshot(self.room_id)
        recovered = [
            event
            for event in snapshot["events"]
            if event["eventType"] == "user_message"
        ]

        self.assertEqual(snapshot["firstEventSequence"], 1)
        self.assertFalse(snapshot["truncated"])
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["eventId"], user["eventId"])
        self.assertEqual(recovered[0]["payload"]["mode"], "jev")
        self.assertEqual(recovered[0]["payload"]["graphId"], "jev-graph:sidecar")
        self.assertEqual(snapshot["deferredEventCount"], 120)

    def test_full_history_page_uses_validated_sidecar_and_ignores_rolled_back_tail(self) -> None:
        for index in range(120):
            self.append(
                "root:full-history",
                "participant_activity",
                {"summary": str(index)},
                retain_per_room=100,
            )
        last_sequence = int(self.service.rooms.get(self.room_id)["lastEventSequence"])
        room_file = self.service.rooms._room_file(self.room_id)
        last_line = json.loads(room_file.read_text(encoding="utf-8").splitlines()[-1])
        with room_file.open("a", encoding="utf-8") as stream:
            # A failed append can leave an audit line after the committed
            # SQLite high-water mark. It must never become a replayed event.
            stream.write(json.dumps(last_line, ensure_ascii=False) + "\n")
            rolled_back = dict(last_line)
            rolled_back["sequence"] = last_sequence + 1
            rolled_back["eventId"] = f"{self.room_id}:{last_sequence + 1}"
            rolled_back["resumeToken"] = rolled_back["eventId"]
            rolled_back["payload"] = {"summary": "rolled-back"}
            stream.write(json.dumps(rolled_back, ensure_ascii=False) + "\n")

        page = self.service.rooms.history_page(
            self.room_id,
            full_history=True,
            limit=5,
        )
        application_page = self.service.room_history(
            self.room_id,
            {"beforeSequence": 0, "limit": 5},
        )

        self.assertEqual(page["retainedFirstSequence"], 1)
        self.assertEqual(page["retainedLastSequence"], last_sequence)
        self.assertFalse(page["retainedPrefixTruncated"])
        self.assertEqual(application_page["retainedFirstSequence"], 1)
        self.assertEqual(application_page["retainedLastSequence"], last_sequence)
        self.assertEqual(
            [event["sequence"] for event in page["items"]],
            list(range(last_sequence - 4, last_sequence + 1)),
        )
        self.assertNotIn(
            "rolled-back",
            [event["payload"].get("summary") for event in page["items"]],
        )

        # A structurally valid but forged retained anchor must fail closed to
        # the SQLite projection instead of claiming that the archive is whole.
        lines = room_file.read_text(encoding="utf-8").splitlines()
        retained_first = last_sequence - 99
        rewritten: list[str] = []
        for line in lines:
            event = json.loads(line)
            if int(event["sequence"]) == retained_first:
                event["payload"] = {"summary": "forged-anchor"}
            rewritten.append(json.dumps(event, ensure_ascii=False))
        room_file.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        rejected = self.service.rooms.history_page(
            self.room_id,
            full_history=True,
            limit=5,
        )
        self.assertEqual(rejected["retainedFirstSequence"], retained_first)
        self.assertTrue(rejected["retainedPrefixTruncated"])
        self.assertEqual(rejected["firstSequence"], last_sequence - 4)
