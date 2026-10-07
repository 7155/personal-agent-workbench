from __future__ import annotations

import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.runtime import PiRuntimeTurnConflict, _HostedSessionState
from tests.sqlite_fixtures import copy_current_database


class RoomCoordinatorThreadFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-room-source-fence-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        db = root / "state.sqlite"
        copy_current_database(db)
        self.service = AgentService(
            db_path=db,
            runtime_config=PiRuntimeConfig(
                enabled=False, executable=None, agent_dir=root / "config",
                session_dir=root / "sessions", logs_dir=root / "logs",
            ),
        )
        self.addCleanup(self.service.close)
        self.source = self.service.ensure_coordinator({})["session"]
        self.binding = {"turnId": "controller-turn", "clientMessageId": "controller-client"}
        self.source_state = _HostedSessionState(
            turn_id=self.binding["turnId"], client_message_id=self.binding["clientMessageId"],
        )
        self.runtime = self.service.runtime
        self.runtime._states[self.source["id"]] = self.source_state
        self.runtime._open_sessions.add(self.source["id"])
        self.room = self.service.coordinator_command({
            "sourceSessionId": self.source["id"], "action": "create_room",
            "clientRequestId": "thread-room", "input": {
                "task": "Exercise original source fences", "routingPolicy": "manual_mentions",
                "participants": [
                    {"roleId": "companion-present-v1", "collaborationRole": "coordinator"},
                    {"roleId": "companion-future-v1", "collaborationRole": "reviewer"},
                ],
            },
        })["target"]

    def post(self):
        return self.service.room_dispatch.post_message(
            self.room["id"], message="Exercise both configured participants",
            client_message_id="room-thread-client", retry_of_root_id="",
            requested_participant_ids=[p["id"] for p in self.room["participants"]],
            work_item_id="", attachment_ids=[],
            admission_fence=self.runtime.gateway_dispatch_fence,
        )

    def test_source_stop_after_root_and_worker_preflight_prevents_host_write(self):
        barrier = threading.Barrier(2)
        writes = []
        worker_threads = set()
        caller = threading.get_ident()

        def prompt(session_id, payload):
            worker_threads.add(threading.get_ident())
            # This is the real Room worker after its durable Root admission.
            events = self.service.rooms.list_events(self.room["id"])
            self.assertTrue(any(e["eventType"] == "user_message" for e in events))
            client_id = payload["clientMessageId"]
            self.runtime.reserve_prompt_admission(session_id, client_message_id=client_id)
            barrier.wait(timeout=5)
            # Stop wins during target preflight, before Host's write callback.
            with self.runtime._lock:
                self.source_state.abort_requested_turn_id = self.binding["turnId"]
            try:
                self.runtime._mark_prompt_dispatched(session_id, client_id)
                writes.append(session_id)
                return {"accepted": True, "turnId": "unexpected-native-turn"}
            finally:
                self.runtime.release_prompt_admission(session_id, client_message_id=client_id)

        with patch.object(self.runtime, "_client", SimpleNamespace(running=True)), \
             patch.object(self.service.room_dispatch, "prompt", side_effect=prompt), \
             self.runtime.gateway_control_scope(self.source["id"], self.binding):
            with self.assertRaises(PiRuntimeTurnConflict):
                self.post()
        self.assertEqual(writes, [])
        self.assertEqual(len(worker_threads), 2)
        self.assertNotIn(caller, worker_threads)
        events = self.service.rooms.list_events(self.room["id"])
        failed = [e for e in events if e["eventType"] == "turn_failed"]
        self.assertEqual(len(failed), 2)
        for participant in self.room["participants"]:
            self.assertEqual(self.service.room_turns.active_turn(participant["sessionId"]), ("", ""))
            self.assertFalse(self.runtime._states[participant["sessionId"]].prompt_dispatched)
        self.assertIsNone(self.runtime._gateway_control_source.get())

    def test_parallel_contexts_preserve_ack_after_source_completion_and_ui_dispatch(self):
        for controlled in (True, False):
            with self.subTest(controlled=controlled):
                self.source_state.abort_requested_turn_id = ""
                barrier = threading.Barrier(2)
                writes = []
                sources = []

                def prompt(session_id, payload, *, sources=sources, barrier=barrier, writes=writes):
                    sources.append(self.runtime._gateway_control_source.get())
                    client_id = payload["clientMessageId"]
                    self.runtime.reserve_prompt_admission(session_id, client_message_id=client_id)
                    # Both workers remain in their copied context concurrently.
                    barrier.wait(timeout=5)
                    self.runtime._mark_prompt_dispatched(session_id, client_id)
                    writes.append(session_id)
                    barrier.wait(timeout=5)
                    # An already accepted target survives source completion.
                    with self.runtime._lock:
                        self.source_state.turn_id = ""
                        self.source_state.client_message_id = ""
                    return {"accepted": True, "turnId": "native-" + session_id}

                scope = (self.runtime.gateway_control_scope(self.source["id"], self.binding)
                         if controlled else nullcontext())
                self.source_state.turn_id = self.binding["turnId"]
                self.source_state.client_message_id = self.binding["clientMessageId"]
                with patch.object(self.runtime, "_client", SimpleNamespace(running=True)), \
                     patch.object(self.service.room_dispatch, "prompt", side_effect=prompt), \
                     patch.object(self.service.room_dispatch, "_accept_room_turn"), scope:
                    response = self.post()
                self.assertTrue(response["accepted"])
                self.assertEqual(len(writes), 2)
                self.assertTrue(all(d["accepted"] for d in response["dispatches"]))
                self.assertEqual(sources, [(self.source["id"], self.binding)] * 2 if controlled else [None, None])
                for p in self.room["participants"]:
                    self.service.room_turns.cancel(p["sessionId"], response["roomTurnId"])
                    self.runtime._states.pop(p["sessionId"])
                    self.service.sessions.set_status(p["sessionId"], "idle")

    def test_one_worker_failure_keeps_other_ack_and_releases_failed_binding(self):
        barrier = threading.Barrier(2)
        failing_session = self.room["participants"][1]["sessionId"]

        def prompt(session_id, payload):
            barrier.wait(timeout=5)
            if session_id == failing_session:
                raise RuntimeError("one participant preflight failed")
            return {"accepted": True, "turnId": "accepted-native-turn"}

        with patch.object(self.runtime, "_client", SimpleNamespace(running=True)), \
             patch.object(self.service.room_dispatch, "prompt", side_effect=prompt), \
             patch.object(self.service.room_dispatch, "_accept_room_turn"), \
             self.runtime.gateway_control_scope(self.source["id"], self.binding):
            response = self.post()
        self.assertTrue(response["accepted"])
        self.assertEqual([d["accepted"] for d in response["dispatches"]], [True, False])
        self.assertEqual(self.service.room_turns.active_turn(failing_session), ("", ""))
        self.assertEqual(self.service.room_turns.active_turn(self.room["participants"][0]["sessionId"])[0], response["roomTurnId"])


if __name__ == "__main__":
    unittest.main()
