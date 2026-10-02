from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.rooms.start_gate import AgentRoomStartGateStore
from rag_ime.agent_service import AgentService
from rag_ime.pi.config import PiRuntimeConfig

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class AgentRoomStartGateStoreTests(unittest.TestCase):
    def test_claim_is_durable_and_replays_same_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AgentRoomStartGateStore(Path(directory) / "agent.sqlite")
            store.initialize()

            first = store.claim(
                room_id="room:one",
                objective_text="修复 trace",
                client_message_id="client:one",
                target_participant_ids=["participant:one"],
                work_item_id="work:one",
                now_ms=100,
            )
            replay = store.claim(
                room_id="room:one",
                objective_text="修复 trace",
                client_message_id="client:one",
                target_participant_ids=["participant:one"],
                work_item_id="work:one",
                now_ms=200,
            )

            self.assertEqual(first["status"], "pending")
            self.assertEqual(replay["status"], "pending")
            self.assertTrue(replay["idempotentReplay"])
            self.assertEqual(replay["gateId"], first["gateId"])

    def test_confirmed_room_ids_only_returns_confirmed_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AgentRoomStartGateStore(Path(directory) / "agent.sqlite")
            store.initialize()
            for suffix in ("pending", "confirmed"):
                store.claim(
                    room_id=f"room:{suffix}",
                    objective_text=f"任务 {suffix}",
                    client_message_id=f"client:{suffix}",
                    target_participant_ids=["participant:one"],
                    work_item_id=f"work:{suffix}",
                    now_ms=100,
                )
            store.confirm("room:confirmed", now_ms=200)

            self.assertEqual(store.confirmed_room_ids(), ["room:confirmed"])

    def test_retire_pending_removes_every_legacy_gate_without_a_room_page_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AgentRoomStartGateStore(Path(directory) / "agent.sqlite")
            store.initialize()
            for ordinal in range(125):
                store.claim(
                    room_id=f"room:{ordinal:03d}",
                    objective_text=f"任务 {ordinal}",
                    client_message_id=f"client:{ordinal}",
                    target_participant_ids=["participant:one"],
                    work_item_id=f"work:{ordinal}",
                    now_ms=100 + ordinal,
                )
            store.confirm("room:124", now_ms=300)

            self.assertEqual(store.retire_pending(), 124)
            self.assertEqual(store.confirmed_room_ids(), ["room:124"])
            self.assertEqual(store.retire_pending(), 0)


class AgentRoomStartGateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-room-start-gate-")
        root = Path(self.tmp.name)
        self.service = AgentService(
            db_path=root / "rag-ime.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=root / "agent-config",
                session_dir=root / "sessions",
                logs_dir=root / "logs",
            ),
        )
        self.root = root

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_room_defaults_to_unrestricted_and_first_work_dispatches_without_approval(self) -> None:
        room = self.service.create_room(
            {
                "title": "Trace 修复",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        participant = room["participants"][0]
        self.service.sessions.bind_runtime_session(
            str(participant["sessionId"]),
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-room-default-dispatch",
        )
        work = self.service.room_work.create(
            room_id=str(room["id"]),
            objective="修复 trace",
            expected_output="通过测试",
            acceptance_criteria=["测试通过"],
            current_owner_participant_id=str(participant["id"]),
            created_by_participant_id=str(participant["id"]),
            client_message_id="work:one",
            topic_id=str(room["activeTopicId"]),
            state="active",
        )

        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:one"},
        ) as prompt:
            response = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "请修复 trace",
                    "workItemId": work["id"],
                    "clientMessageId": "client:one",
                },
            )
            replay = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "请修复 trace",
                    "workItemId": work["id"],
                    "clientMessageId": "client:one",
                },
            )

        self.assertTrue(response["accepted"])
        self.assertEqual(response["phase"], "execution")
        self.assertNotIn("startConfirmation", response)
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(replay["roomTurnId"], response["roomTurnId"])
        prompt.assert_called_once()
        self.assertEqual(
            {
                self.service.sessions.get(str(value["sessionId"]))[
                    "roomExecutionMode"
                ]
                for value in room["participants"]
            },
            {"room_unrestricted"},
        )
        self.assertIsNone(self.service.room_start_gates.get(str(room["id"])))
        self.assertTrue(
            self.service._active_room_dispatch_authorizes_work(
                str(participant["sessionId"])
            )
        )

    def test_startup_retires_a_legacy_pending_gate_before_room_projection(self) -> None:
        room = self.service.create_room(
            {
                "title": "Legacy pending gate",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        participant = room["participants"][0]
        work = self.service.room_work.create(
            room_id=str(room["id"]),
            objective="继续执行",
            expected_output="结果",
            acceptance_criteria=["结果"],
            current_owner_participant_id=str(participant["id"]),
            created_by_participant_id=str(participant["id"]),
            client_message_id="work:legacy-gate",
            topic_id=str(room["activeTopicId"]),
            state="active",
        )
        pending = self.service.room_start_gates.claim(
            room_id=str(room["id"]),
            objective_text="旧版本遗留确认",
            client_message_id="client:legacy-gate",
            target_participant_ids=[str(participant["id"])],
            work_item_id=str(work["id"]),
        )
        self.assertEqual(pending["status"], "pending")

        self.service.close()
        self.service = AgentService(
            db_path=self.root / "rag-ime.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config-restarted",
                session_dir=self.root / "sessions-restarted",
                logs_dir=self.root / "logs-restarted",
            ),
        )

        projected = self.service.rooms.get(str(room["id"]))
        self.assertIsNone(projected["startGate"])
        self.assertIsNone(self.service.room_start_gates.get(str(room["id"])))
        self.assertEqual(
            {
                self.service.sessions.get(str(value["sessionId"]))[
                    "roomExecutionMode"
                ]
                for value in projected["participants"]
            },
            {"room_unrestricted"},
        )
        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:legacy-gate"},
        ) as prompt:
            response = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "继续执行",
                    "workItemId": str(work["id"]),
                    "clientMessageId": "client:after-restart",
                },
            )
        self.assertTrue(response["accepted"])
        self.assertEqual(response["phase"], "execution")
        self.assertNotIn("startConfirmation", response)
        prompt.assert_called_once()

    def test_legacy_accepted_confirmation_receipt_is_upgraded_to_one_dispatch(self) -> None:
        room = self.service.create_room(
            {
                "title": "Legacy accepted confirmation receipt",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        participant = room["participants"][0]
        work = self.service.room_work.create(
            room_id=str(room["id"]),
            objective="执行旧回执任务",
            expected_output="结果",
            acceptance_criteria=["结果"],
            current_owner_participant_id=str(participant["id"]),
            created_by_participant_id=str(participant["id"]),
            client_message_id="work:legacy-receipt",
            topic_id=str(room["activeTopicId"]),
            state="active",
        )
        request = {
            "message": "执行旧回执任务",
            "workItemId": str(work["id"]),
            "clientMessageId": "client:legacy-receipt",
        }
        command_payload = {
            "message": request["message"],
            "retryOfRootId": "",
            "participantIds": [],
            "workItemId": request["workItemId"],
            "attachmentIds": [],
            "answerToPostId": "",
            "answerToRootId": "",
        }
        pending_gate = self.service.room_start_gates.claim(
            room_id=str(room["id"]),
            objective_text=str(request["message"]),
            client_message_id=str(request["clientMessageId"]),
            target_participant_ids=[str(participant["id"])],
            work_item_id=str(work["id"]),
        )
        legacy_claim = self.service.command_receipts.begin(
            command_scope="room_message",
            scope_id=str(room["id"]),
            client_message_id=str(request["clientMessageId"]),
            payload=command_payload,
        )
        self.service.command_receipts.complete(
            legacy_claim,
            command_scope="room_message",
            scope_id=str(room["id"]),
            client_message_id=str(request["clientMessageId"]),
            # Historical wire receipt, independent of current production writers.
            response={
                "schemaVersion": "rag-ime.agent-room-message.v1",
                "ok": True,
                "accepted": False,
                "status": "awaiting_confirmation",
                "phase": "alignment",
                "executionOwner": "session",
                "roomId": pending_gate["roomId"],
                "clientMessageId": pending_gate["clientMessageId"],
                "workItemId": pending_gate["workItemId"],
                "startConfirmation": {
                    "gateId": pending_gate["gateId"],
                    "status": pending_gate["status"],
                    "objective": pending_gate["objective"],
                    "workItemId": pending_gate["workItemId"],
                    "targetParticipantIds": pending_gate["targetParticipantIds"],
                    "requiresConfirmation": True,
                    "afterConfirmExecutionMode": "room_unrestricted",
                },
                "timelineEvents": [],
            },
        )

        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:legacy-receipt"},
        ) as prompt:
            upgraded = self.service.post_room_message(str(room["id"]), request)
            replay = self.service.post_room_message(str(room["id"]), request)

        self.assertTrue(upgraded["accepted"])
        self.assertEqual(upgraded["phase"], "execution")
        self.assertNotIn("startConfirmation", upgraded)
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(replay["roomTurnId"], upgraded["roomTurnId"])
        self.assertIsNone(self.service.room_start_gates.get(str(room["id"])))
        prompt.assert_called_once()

    def test_confirmation_dispatches_original_task_and_is_idempotent(self) -> None:
        room = self.service.create_room(
            {
                "title": "确认后执行",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        participant = room["participants"][0]
        work = self.service.room_work.create(
            room_id=str(room["id"]), objective="执行原任务", expected_output="结果",
            acceptance_criteria=["结果"], current_owner_participant_id=str(participant["id"]),
            created_by_participant_id=str(participant["id"]), client_message_id="work:two",
            topic_id=str(room["activeTopicId"]), state="active",
        )
        media = self.service.import_media(
            room_id=str(room["id"]),
            data=PNG_1X1,
            mime_type="image/png",
            file_name="start-gate.png",
        )["media"]
        with (
            patch.object(
                self.service.runtime,
                "model_catalog",
                return_value={"selected": {"supportsImages": True}},
            ),
            patch.object(self.service, "prompt", return_value={"turnId": "turn:confirmed"}) as prompt,
        ):
            pending = self.service.room_start_gates.claim(
                room_id=str(room["id"]),
                objective_text="原任务",
                work_item_id=str(work["id"]),
                client_message_id="client:two",
                target_participant_ids=[str(participant["id"])],
                attachment_ids=[str(media["mediaId"])],
            )
            confirmed = self.service.confirm_room_start(
                str(room["id"]), {"gateId": pending["gateId"], "decision": "confirm"},
            )
            replay = self.service.confirm_room_start(
                str(room["id"]), {"gateId": pending["gateId"], "decision": "confirm"},
            )
        self.assertTrue(confirmed["accepted"])
        self.assertEqual(confirmed["phase"], "execution")
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(replay["roomTurnId"], confirmed["roomTurnId"])
        self.assertEqual(prompt.call_args.args[1]["attachments"], [media["mediaId"]])
        prompt.assert_called_once()
        self.assertEqual(
            {
                self.service.sessions.get(str(participant["sessionId"]))[
                    "roomExecutionMode"
                ]
                for participant in room["participants"]
            },
            {"room_unrestricted"},
        )

    def test_room_work_items_never_create_repeated_approval_gates(self) -> None:
        room = self.service.create_room(
            {
                "title": "确认一次后继续执行",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        participant = room["participants"][0]
        second_participant = room["participants"][1]
        first_work = self.service.room_work.create(
            room_id=str(room["id"]), objective="执行第一个任务", expected_output="结果一",
            acceptance_criteria=["结果一"], current_owner_participant_id=str(participant["id"]),
            created_by_participant_id=str(participant["id"]), client_message_id="work:first",
            topic_id=str(room["activeTopicId"]), state="active",
        )
        second_work = self.service.room_work.create(
            room_id=str(room["id"]), objective="执行第二个任务", expected_output="结果二",
            acceptance_criteria=["结果二"], current_owner_participant_id=str(second_participant["id"]),
            created_by_participant_id=str(participant["id"]), client_message_id="work:second",
            topic_id=str(room["activeTopicId"]), state="active",
        )
        with patch.object(
            self.service,
            "prompt",
            side_effect=[{"turnId": "turn:first"}, {"turnId": "turn:second"}],
        ) as prompt:
            first = self.service.post_room_message(
                str(room["id"]),
                {"message": "第一个任务", "workItemId": first_work["id"], "clientMessageId": "client:first"},
            )
            second = self.service.post_room_message(
                str(room["id"]),
                {"message": "第二个任务", "workItemId": second_work["id"], "clientMessageId": "client:second"},
            )

        self.assertTrue(first["accepted"])
        self.assertTrue(second["accepted"])
        self.assertEqual(second["phase"], "execution")
        self.assertNotIn("startConfirmation", second)
        self.assertIsNone(self.service.room_start_gates.get(str(room["id"])))
        self.assertEqual(prompt.call_count, 2)

    def test_conversation_only_room_message_does_not_create_gate(self) -> None:
        room = self.service.create_room(
            {
                "title": "只聊天",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        with patch.object(self.service, "prompt", return_value={"turnId": "turn:chat"}):
            response = self.service.post_room_message(
                str(room["id"]),
                {"message": "聊聊今天的安排", "clientMessageId": "client:chat"},
            )
        self.assertTrue(response["accepted"])
        self.assertIsNone(self.service.room_start_gates.get(str(room["id"])))


if __name__ == "__main__":
    unittest.main()
