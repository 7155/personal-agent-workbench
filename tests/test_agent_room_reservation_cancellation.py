from __future__ import annotations

import concurrent.futures
import threading
import unittest
from unittest.mock import patch

from tests import test_agent_room_send_admission as admission_fixtures


class RoomReservationCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = admission_fixtures.RoomSendAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    @staticmethod
    def typed_abort(session_id: str) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.agent-abort.v1",
            "ok": True,
            "sessionId": session_id,
            "runtimeReceipt": {
                "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                "sessionId": session_id,
                "turnId": "turn:before-stop",
                "lifecycle": {
                    "schemaVersion": "pi.agent-abort-receipt.v1",
                    "scopeId": session_id,
                    "operations": [],
                    "pendingOperations": [],
                    "failedOperationIds": [],
                    "cancelledContinuationIds": [],
                    "drained": True,
                    "idle": True,
                },
            },
        }

    def test_late_send_cleanup_after_stop_preserves_a_new_prebinding_reservation(self) -> None:
        fixture = self.fixture
        metadata_entered = threading.Event()
        resume_metadata = threading.Event()
        second_reserved = threading.Event()
        resume_second = threading.Event()
        advance = fixture.service.rooms.advance_delivery_cursor
        target_idle = fixture.service._room_target_idle

        def pause_accepted_metadata(*args, **kwargs):
            if not metadata_entered.is_set():
                metadata_entered.set()
                self.assertTrue(resume_metadata.wait(timeout=10))
            return advance(*args, **kwargs)

        def idle(session_id, *, allow_user_priority=False):
            if threading.current_thread().name.startswith("reservation-second"):
                second_reserved.set()
                self.assertTrue(resume_second.wait(timeout=10))
            return target_idle(session_id, allow_user_priority=allow_user_priority)

        with (
            patch.object(fixture.service.rooms, "advance_delivery_cursor", side_effect=pause_accepted_metadata),
            patch.object(fixture.service, "_room_target_idle", side_effect=idle),
            patch.object(fixture.service, "prompt", return_value={"accepted": True, "turnId": "turn:before-stop"}),
            patch.object(fixture.service, "abort", side_effect=self.typed_abort),
            concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="reservation-first") as first_executor,
            concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="reservation-second") as second_executor,
        ):
            first = first_executor.submit(fixture.send, "first-before-stop")
            second = None
            try:
                self.assertTrue(metadata_entered.wait(timeout=10))
                root_id, _ = fixture.service.room_turns.active_turn(fixture.session_id)
                self.assertTrue(root_id)
                stopped = fixture.service.abort_room_turn(str(fixture.room["id"]), {
                    "roomTurnId": root_id,
                    "clientRequestId": "stop-during-accepted-metadata",
                })
                self.assertEqual(stopped["status"], "terminated")
                self.assertEqual(fixture.service.room_turns.active_turn(fixture.session_id), ("", ""))

                second = second_executor.submit(fixture.send, "second-after-stop")
                self.assertTrue(second_reserved.wait(timeout=10))
                self.assertIn(fixture.session_id, fixture.service.room_turns.user_priority_sessions)
                resume_metadata.set()
                self.assertTrue(first.result(timeout=10)["accepted"])

                # The old sender has now left its finally block. The newer
                # reservation is still between acquisition and Root binding.
                self.assertIn(fixture.session_id, fixture.service.room_turns.user_priority_sessions)
                self.assertEqual(fixture.service.room_turns.active_turn(fixture.session_id), ("", ""))
                with self.assertRaises(ValueError) as failure:
                    fixture.send("third-must-remain-blocked")
                self.assertEqual(failure.exception.cause_code, "ROOM_PARTICIPANT_BUSY")
            finally:
                resume_metadata.set()
                resume_second.set()
                first.result(timeout=10)
                if second is not None:
                    self.assertTrue(second.result(timeout=10)["accepted"])
        self.assertFalse(fixture.service.room_turns.user_priority_sessions)

    def test_late_stop_receipt_preserves_a_new_prebinding_reservation(self) -> None:
        fixture = self.fixture
        stop_entered = threading.Event()
        resume_stop = threading.Event()
        second_reserved = threading.Event()
        resume_second = threading.Event()
        target_idle = fixture.service._room_target_idle

        def pause_abort_receipt(session_id):
            stop_entered.set()
            self.assertTrue(resume_stop.wait(timeout=10))
            return self.typed_abort(session_id)

        def idle(session_id, *, allow_user_priority=False):
            if threading.current_thread().name.startswith("reservation-second"):
                second_reserved.set()
                self.assertTrue(resume_second.wait(timeout=10))
            return target_idle(session_id, allow_user_priority=allow_user_priority)

        with (
            patch.object(fixture.service, "_room_target_idle", side_effect=idle),
            patch.object(fixture.service, "prompt", return_value={"accepted": True, "turnId": "turn:before-stop"}),
            patch.object(fixture.service, "abort", side_effect=pause_abort_receipt),
            concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="reservation-stop") as stop_executor,
            concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="reservation-second") as second_executor,
        ):
            first = fixture.send("accepted-before-stop")
            self.assertTrue(first["accepted"])
            stop = stop_executor.submit(fixture.service.abort_room_turn, str(fixture.room["id"]), {
                "roomTurnId": first["roomTurnId"],
                "clientRequestId": "delayed-stop-receipt",
            })
            second = None
            try:
                self.assertTrue(stop_entered.wait(timeout=10))
                # The Session can finish before the concurrent Stop request
                # returns its already-observed cancellation receipt.
                fixture.service._finish_room_turn(
                    fixture.session_id, "turn:before-stop", str(first["roomTurnId"]),
                )
                self.assertEqual(fixture.service.room_turns.active_turn(fixture.session_id), ("", ""))

                second = second_executor.submit(fixture.send, "new-send-during-stop-receipt")
                self.assertTrue(second_reserved.wait(timeout=10))
                self.assertIn(fixture.session_id, fixture.service.room_turns.user_priority_sessions)
                resume_stop.set()
                self.assertEqual(stop.result(timeout=10)["status"], "terminated")

                # Stop captured the old Root. It cannot release a priority
                # reservation acquired after that Root actually finished.
                self.assertIn(fixture.session_id, fixture.service.room_turns.user_priority_sessions)
                with self.assertRaises(ValueError) as failure:
                    fixture.send("third-must-remain-blocked")
                self.assertEqual(failure.exception.cause_code, "ROOM_PARTICIPANT_BUSY")
            finally:
                resume_stop.set()
                resume_second.set()
                stop.result(timeout=10)
                if second is not None:
                    self.assertTrue(second.result(timeout=10)["accepted"])
        self.assertFalse(fixture.service.room_turns.user_priority_sessions)


if __name__ == "__main__":
    unittest.main()
