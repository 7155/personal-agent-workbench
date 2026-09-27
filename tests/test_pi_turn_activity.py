"""Exact live-turn observation is local and never opens, cancels or settles Pi."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock

from rag_ime.pi.runtime import PiRuntimeHostManager, _HostedSessionState
from rag_ime.pi.event_projection import runtime_primitive_capabilities


class PiTurnActivityTests(unittest.TestCase):
    def setUp(self):
        self.runtime = object.__new__(PiRuntimeHostManager)
        self.runtime._lock = threading.RLock()
        self.runtime._client = Mock(running=True)
        self.runtime._open_sessions = {"session"}
        self.runtime._states = {"session": _HostedSessionState(
            turn_id="turn", client_message_id="dispatch")}
        self.runtime.ensure = Mock(side_effect=AssertionError("observation cannot open Pi"))

    def active(self, turn="turn", dispatch="dispatch"):
        return self.runtime.is_turn_active("session", turn, client_message_id=dispatch)

    def tearDown(self):
        self.runtime.ensure.assert_not_called()
        if self.runtime._client is not None:
            self.runtime._client.send.assert_not_called()

    def test_exact_current_turn_is_active(self):
        self.assertTrue(self.active())

    def test_reused_session_cannot_supply_old_turn_or_dispatch_activity(self):
        self.assertFalse(self.active(turn="previous-turn"))
        self.assertFalse(self.active(dispatch="previous-dispatch"))

    def test_restored_idle_session_is_not_active(self):
        self.runtime._states["session"] = _HostedSessionState()
        self.assertFalse(self.active())

    def test_dead_or_missing_host_cannot_prove_activity(self):
        self.runtime._client.running = False
        self.assertFalse(self.active())
        self.runtime._client = None
        self.assertFalse(self.active())

    def test_evicted_session_and_missing_state_cannot_prove_activity(self):
        self.runtime._open_sessions.clear()
        self.assertFalse(self.active())
        self.runtime._open_sessions.add("session")
        self.runtime._states.clear()
        self.assertFalse(self.active())

    def test_cancellation_in_progress_is_not_running_or_drained(self):
        self.runtime._states["session"].abort_requested_turn_id = "turn"
        self.assertFalse(self.active())

    def test_missing_identity_cannot_match(self):
        self.assertFalse(self.active(turn=""))
        self.assertFalse(self.active(dispatch=""))

    def test_runtime_status_exposes_only_explicit_retired_recovery_capability(self):
        for value in (None, False, "true", 1):
            with self.subTest(value=value):
                self.assertFalse(runtime_primitive_capabilities(
                    {"sessionRetiredTurnRecovery": value})["sessionRetiredTurnRecovery"])
        self.assertTrue(runtime_primitive_capabilities(
            {"sessionRetiredTurnRecovery": True})["sessionRetiredTurnRecovery"])


if __name__ == "__main__":
    unittest.main()
