from __future__ import annotations

import concurrent.futures
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.pi.config import PiRuntimeConfig


class RoomSendAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-room-send-admission-")
        self.root = Path(self.tmp.name)
        self.service = AgentService(
            db_path=self.root / "rag-ime.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config",
                session_dir=self.root / "sessions",
                logs_dir=self.root / "logs",
            ),
        )
        self.room = self.service.create_room({
            "title": "发送恢复",
            "workspaceRoots": [str(self.root)],
            "participants": [
                {"roleId": "companion-present-v1", "roleVersion": "1"},
                {"roleId": "companion-future-v1", "roleVersion": "1"},
            ],
        })["room"]
        self.target = self.room["participants"][0]
        self.session_id = str(self.target["sessionId"])

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def send(self, client_message_id: str) -> dict[str, object]:
        return self.service.post_room_message(str(self.room["id"]), {
            "message": "是什么问题呀",
            "clientMessageId": client_message_id,
            "participantIds": [str(self.target["id"])],
        })

    def test_session_prewarm_memory_probe_does_not_reserve_room_send(self) -> None:
        probe_entered = threading.Event()
        release_probe = threading.Event()

        def probe(*_args: object, **_kwargs: object) -> dict[str, object]:
            probe_entered.set()
            self.assertTrue(release_probe.wait(timeout=5))
            return {"ok": True}

        with (
            patch.object(self.service.runtime, "ensure", return_value={"state": {"isIdle": True}}),
            patch.object(self.service, "_probe_memory_maintenance", side_effect=probe),
            patch.object(self.service, "prompt", return_value={"turnId": "turn:after-prewarm"}) as prompt,
            concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor,
        ):
            warmup = executor.submit(self.service.ensure_runtime, {"sessionId": self.session_id})
            try:
                self.assertTrue(probe_entered.wait(timeout=5))
                accepted = self.send("send-during-prewarm")
                self.assertTrue(accepted["accepted"])
                prompt.assert_called_once()
            finally:
                release_probe.set()
                warmup.result(timeout=5)

    def test_failed_availability_probe_releases_only_its_own_reservation(self) -> None:
        unrelated_session = str(self.room["participants"][1]["sessionId"])
        self.service.room_turns.hold_priority((unrelated_session,))
        baseline = self.service.rooms.get(str(self.room["id"]))["lastEventSequence"]
        with patch.object(
            self.service, "_room_target_idle",
            side_effect=sqlite3.OperationalError("unable to open database file"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "unable to open database"):
                self.send("failed-read-before-dispatch")

        self.assertEqual(self.service.rooms.get(str(self.room["id"]))["lastEventSequence"], baseline)
        self.assertEqual(self.service.room_turns.user_priority_sessions, {unrelated_session})
        with patch.object(self.service, "prompt", return_value={"turnId": "turn:after-probe-error"}) as prompt:
            self.assertTrue(self.send("manual-send-after-probe-error")["accepted"])
            prompt.assert_called_once()

    def test_real_inflight_room_turn_stays_reserved_with_a_typed_rejection(self) -> None:
        self.service.room_turns.begin(self.session_id, "root:inflight", dispatch_id="dispatch:inflight")
        self.service.room_turns.accept(self.session_id, "turn:inflight", "root:inflight")
        baseline = self.service.rooms.get(str(self.room["id"]))["lastEventSequence"]
        with patch.object(self.service, "prompt") as prompt:
            with self.assertRaises(ValueError) as failure:
                self.send("rejected-real-busy")
            prompt.assert_not_called()
        payload = getattr(failure.exception, "response_payload", lambda: {})()
        self.assertEqual(payload.get("code"), "AGENT_COMMAND_FAILED")
        self.assertEqual(payload.get("commandReceipt"), {
            "state": "failed",
            "clientMessageId": "rejected-real-busy",
            "causeCode": "ROOM_PARTICIPANT_BUSY",
        })
        self.assertEqual(self.service.room_turns.active_turn(self.session_id), ("root:inflight", "dispatch:inflight"))
        self.assertEqual(self.service.rooms.get(str(self.room["id"]))["lastEventSequence"], baseline)

    def test_delivery_metadata_failure_keeps_accepted_pi_turn_and_replay_identity(self) -> None:
        for operation in ("advance_delivery_cursor", "commit_route"):
            with self.subTest(operation=operation):
                client_id = f"accepted-before-{operation}-failure"
                with patch.object(self.service, "prompt", return_value={"accepted": True, "turnId": f"turn:{operation}"}) as prompt, patch.object(
                    self.service.rooms, operation, side_effect=sqlite3.OperationalError("fixture metadata write failed"),
                ):
                    accepted = self.send(client_id)
                    replay = self.send(client_id)
                prompt.assert_called_once()
                self.assertTrue(accepted["accepted"])
                self.assertTrue(replay["idempotentReplay"])
                root_id = str(accepted["roomTurnId"])
                self.assertEqual(replay["roomTurnId"], root_id)
                self.assertEqual(self.service.room_turns.active_turn(self.session_id)[0], root_id)
                self.assertFalse(self.service.room_turns.user_priority_sessions)
                self.assertEqual(accepted["dispatches"][0]["projectionSync"], {"state": "pending", "failedOperations": [operation]})
                failures = self.service.rooms.list_events_for_turn(str(self.room["id"]), root_id, event_types=("turn_failed",))
                self.assertEqual(failures, [])
                # Only the later, actual Pi terminal may end this Room turn.
                self.service.events.publish(self.session_id, "turn_failed", {"error": "fixture Provider failure"}, turn_id=f"turn:{operation}")
                self.assertTrue(self.service.events.flush())
                self.assertEqual(self.service.room_turns.active_turn(self.session_id), ("", ""))
                failures = self.service.rooms.list_events_for_turn(str(self.room["id"]), root_id, event_types=("turn_failed",))
                self.assertEqual(len(failures), 1)

    def test_worker_exception_closes_room_turn_and_releases_admission(self) -> None:
        """An untyped dispatch failure must not strand the Room as running."""
        with patch.object(
            self.service.room_dispatch,
            "dispatch_target",
            side_effect=RuntimeError("worker crashed before dispatch receipt"),
        ):
            with self.assertRaisesRegex(RuntimeError, "worker crashed"):
                self.send("worker-exception-cleanup")

        self.assertEqual(self.service.room_turns.active_turn(self.session_id), ("", ""))
        self.assertNotIn(self.session_id, self.service.room_turns.user_priority_sessions)
        failed = [
            event for event in self.service.rooms.list_events(
                str(self.room["id"]), after_sequence=0, limit=500
            ) if event.get("eventType") == "turn_failed"
        ]
        self.assertTrue(failed)

    def test_raised_dispatch_cleanup_preserves_a_later_send_reservation(self) -> None:
        self._assert_later_send_reservation_survives_cleanup(raised=True)

    def test_rejected_dispatch_cleanup_preserves_a_later_send_reservation(self) -> None:
        self._assert_later_send_reservation_survives_cleanup(raised=False)

    def test_partner_batch_cleanup_preserves_a_later_send_reservation(self) -> None:
        self._assert_later_send_reservation_survives_cleanup(raised=True, partner_batch=True)

    def _partner_request(self, *, batch: bool):
        source = self.room["participants"][1]
        targets = [self.target]
        if batch:
            added = self.service.add_room_participant(str(self.room["id"]), {
                "roleId": "companion-firstlight-v1", "roleVersion": "1",
            })
            targets.append(added["participant"])
        self.service.room_turns.begin(str(source["sessionId"]), "root:partner", dispatch_id="dispatch:partner")
        tasks = [{
            "targetParticipantId": target["id"], "task": "check reservation",
            "expectedOutput": "reservation evidence", "acceptanceCriteria": ["later sends remain available"],
        } for target in targets]
        args = ({"op": "delegate_batch", "phase": "check reservation", "tasks": tasks}
                if batch else {"op": "delegate", **tasks[0]})
        return source, targets, args

    def test_partner_batch_validation_failure_releases_all_reservations(self) -> None:
        source, targets, args = self._partner_request(batch=True)
        args.pop("phase")
        with self.assertRaisesRegex(ValueError, "phase"):
            self.service.room_partner_application.execute(str(source["sessionId"]), args,
                                                          tool_call_id="invalid-wave")
        self.assertFalse(self.service.room_turns.user_priority_sessions)
        for target in targets:
            self.assertEqual(self.service.room_turns.active_turn(str(target["sessionId"])), ("", ""))

    def test_partner_batch_availability_failure_releases_all_reservations(self) -> None:
        source, targets, args = self._partner_request(batch=True)
        with patch.object(self.service.room_partner_application, "room_target_idle",
                          side_effect=RuntimeError("availability read failed")):
            with self.assertRaisesRegex(RuntimeError, "availability read failed"):
                self.service.room_partner_application.execute(str(source["sessionId"]), args,
                                                              tool_call_id="unreadable-wave")
        self.assertFalse(self.service.room_turns.user_priority_sessions)
        for target in targets:
            self.assertEqual(self.service.room_turns.active_turn(str(target["sessionId"])), ("", ""))

    def test_partner_preparation_failures_release_the_exact_pending_reservation(self) -> None:
        source, _targets, args = self._partner_request(batch=False)
        application = self.service.room_partner_application
        for owner, operation in (
            (application, "room_target_idle"),
            (self.service.rooms, "plan_routes"),
            (application, "_create_delegated_work"),
            (self.service.room_events, "publish"),
            (self.service.room_work, "claim_dispatch"),
            (self.service.rooms, "unread_public_messages"),
        ):
            with self.subTest(operation=operation):
                with patch.object(owner, operation, side_effect=RuntimeError(operation)), \
                     patch.object(self.service, "prompt") as prompt:
                    with self.assertRaisesRegex(RuntimeError, operation):
                        application.execute(str(source["sessionId"]), args,
                                            tool_call_id="failed-preparation:" + operation)
                    prompt.assert_not_called()
                self.assertFalse(self.service.room_turns.user_priority_sessions)
                self.assertEqual(self.service.room_turns.active_turn(self.session_id), ("", ""))
                self.assertEqual(self.service.room_turns.active_turn(str(source["sessionId"])),
                                 ("root:partner", "dispatch:partner"))

    def _assert_later_send_reservation_survives_cleanup(
        self, *, raised: bool, partner_batch: bool = False,
    ) -> None:
        first_released = threading.Event()
        second_reserved = threading.Event()
        resume_second = threading.Event()
        release_observed = threading.Event()
        release_session = self.service.room_turns.release_priority_session
        release_batch = self.service.room_turns.release_priority
        target_idle = self.service._room_target_idle
        source = self.room["participants"][1]
        wave_targets = [self.target]
        if partner_batch:
            added = self.service.add_room_participant(str(self.room["id"]), {
                "roleId": "companion-firstlight-v1", "roleVersion": "1",
            })
            wave_targets.append(added["participant"])
            self.service.room_turns.begin(str(source["sessionId"]), "root:partner", dispatch_id="dispatch:partner")

        def pause_first_release() -> None:
            if release_observed.is_set():
                return
            release_observed.set()
            first_released.set()
            self.assertTrue(second_reserved.wait(timeout=10))

        def release_one(session_id, **kwargs):
            release_session(session_id, **kwargs)
            if session_id == self.session_id:
                pause_first_release()

        def release_many(session_ids, **kwargs):
            release_batch(session_ids, **kwargs)
            pause_first_release()

        def idle(session_id, *, allow_user_priority=False):
            if threading.current_thread().name.startswith("second-room-send"):
                second_reserved.set()
                self.assertTrue(resume_second.wait(timeout=10))
            return target_idle(session_id, allow_user_priority=allow_user_priority)

        def prompt(_session_id, payload):
            if payload["message"] != "second":
                if raised:
                    raise RuntimeError("first dispatch rejected")
                return {"accepted": False, "error": "first dispatch rejected"}
            return {"accepted": True, "turnId": "turn:later-send"}

        def send(message):
            if partner_batch and message == "first":
                return self.service.room_partner_application.execute(str(source["sessionId"]), {
                    "op": "delegate_batch",
                    "phase": "check reservation",
                    "tasks": [{
                        "targetParticipantId": target["id"],
                        "task": "first",
                        "expectedOutput": "reservation evidence",
                        "acceptanceCriteria": ["later sends keep their reservation"],
                    } for target in wave_targets],
                }, tool_call_id="tool:reservation-wave")
            return self.service.post_room_message(str(self.room["id"]), {
                "message": message,
                "clientMessageId": message,
                "participantIds": [self.target["id"]],
            })

        with (
            patch.object(self.service.room_turns, "release_priority_session", side_effect=release_one),
            patch.object(self.service.room_turns, "release_priority", side_effect=release_many),
            patch.object(self.service, "_room_target_idle", side_effect=idle),
            patch.object(self.service, "prompt", side_effect=prompt),
            concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="first-room-send") as first_executor,
            concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="second-room-send") as second_executor,
        ):
            first = first_executor.submit(send, "first")
            second = None
            try:
                self.assertTrue(first_released.wait(timeout=10))
                second = second_executor.submit(send, "second")
                self.assertTrue(second_reserved.wait(timeout=10))
                if partner_batch:
                    self.assertEqual(first.result(timeout=10)["failed"], 2)
                elif raised:
                    with self.assertRaisesRegex(RuntimeError, "first dispatch rejected"):
                        first.result(timeout=10)
                else:
                    self.assertFalse(first.result(timeout=10)["accepted"])
                self.assertIn(self.session_id, self.service.room_turns.user_priority_sessions)
                self.assertEqual(self.service.room_turns.active_turn(self.session_id), ("", ""))
                with self.assertRaises(ValueError) as failure:
                    send("third")
                self.assertEqual(failure.exception.cause_code, "ROOM_PARTICIPANT_BUSY")
            finally:
                resume_second.set()
                if second is not None:
                    self.assertTrue(second.result(timeout=10)["accepted"])
        self.assertFalse(self.service.room_turns.user_priority_sessions)

    def test_worker_exception_preserves_another_accepted_dispatch(self) -> None:
        other = self.room["participants"][1]
        dispatch = self.service.room_dispatch.dispatch_target

        def dispatch_one(**kwargs):
            if kwargs["target"]["id"] == other["id"]:
                raise RuntimeError("one worker crashed before admission")
            return dispatch(**kwargs)

        with (
            patch.object(self.service, "prompt", return_value={"accepted": True, "turnId": "turn:still-running"}),
            patch.object(self.service.room_dispatch, "dispatch_target", side_effect=dispatch_one),
        ):
            response = self.service.post_room_message(str(self.room["id"]), {
                "message": "分别核对两个部分",
                "clientMessageId": "mixed-worker-outcome",
                "participantIds": [self.target["id"], other["id"]],
            })

        self.assertTrue(response["accepted"])
        root_id = str(response["roomTurnId"])
        self.assertEqual(self.service.room_turns.active_turn(self.session_id)[0], root_id)
        self.assertEqual(self.service.room_turns.active_turn(str(other["sessionId"])), ("", ""))
        self.assertFalse(self.service.room_turns.user_priority_sessions)
        self.assertEqual([item["accepted"] for item in response["dispatches"]], [True, False])
        failures = self.service.rooms.list_events_for_turn(
            str(self.room["id"]), root_id, event_types=("turn_failed",)
        )
        self.assertEqual([item["participantId"] for item in failures], [other["id"]])

    def test_worker_failure_releases_reservation_when_terminal_write_fails(self) -> None:
        publish = self.service.room_events.publish

        def fail_terminal(**kwargs):
            if kwargs["event_type"] == "turn_failed":
                raise sqlite3.OperationalError("terminal write unavailable")
            return publish(**kwargs)

        with (
            patch.object(self.service.room_dispatch, "dispatch_target", side_effect=RuntimeError("worker crash")),
            patch.object(self.service.room_events, "publish", side_effect=fail_terminal),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "terminal write unavailable"):
                self.send("worker-terminal-write-failure")

        self.assertFalse(self.service.room_turns.user_priority_sessions)
        self.assertEqual(self.service.room_turns.active_turn(self.session_id), ("", ""))

    def test_claim_failure_releases_all_targets_when_terminal_write_fails(self) -> None:
        room_id = str(self.room["id"])
        self.service.rooms.update_config(room_id, {"routingPolicy": "parallel"})
        owner, accountable = self.room["participants"]
        work = self.service.create_room_work_item(room_id, {
            "objective": "检查发送恢复",
            "expectedOutput": "诊断报告",
            "acceptanceCriteria": ["可重复发送"],
            "currentOwnerParticipantId": owner["id"],
            "accountableParticipantId": accountable["id"],
            "createdByParticipantId": owner["id"],
            "clientMessageId": "claim-cleanup-work",
        })["workItem"]
        publish = self.service.room_events.publish

        def fail_terminal(**kwargs):
            if kwargs["event_type"] == "turn_failed":
                raise sqlite3.OperationalError("claim terminal write unavailable")
            return publish(**kwargs)

        # Another accepted Root must survive this request's cleanup.
        self.service.room_turns.begin("unrelated-session", "unrelated-root")
        self.service.room_turns.accept("unrelated-session", "accepted-turn", "unrelated-root")
        with (
            patch.object(self.service.room_work, "claim_dispatch", side_effect=ValueError("assignment changed")),
            patch.object(self.service.room_events, "publish", side_effect=fail_terminal),
            patch.object(self.service, "prompt") as prompt,
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "claim terminal write unavailable"):
                self.service.post_room_message(room_id, {
                    "message": "执行检查",
                    "clientMessageId": "claim-terminal-write-failure",
                    "participantIds": [owner["id"], accountable["id"]],
                    "workItemId": work["id"],
                })
            prompt.assert_not_called()

        for target in (owner, accountable):
            self.assertEqual(self.service.room_turns.active_turn(str(target["sessionId"])), ("", ""))
        self.assertFalse(self.service.room_turns.user_priority_sessions)
        self.assertEqual(self.service.room_turns.active_turn("unrelated-session")[0], "unrelated-root")
        with patch.object(self.service, "prompt", return_value={"accepted": True, "turnId": "turn:after-claim-error"}) as prompt:
            self.assertTrue(self.send("manual-send-after-claim-error")["accepted"])
            prompt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
