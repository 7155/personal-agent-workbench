from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from rag_ime.agent_tool_ids import MEMORY_CURATION_TOOL_PROFILE
from rag_ime.pi.runtime import PiRuntimeHostManager, _HostedSessionState
from rag_ime.pi.values import PiRuntimeCommandAcceptanceUnknown, PiRuntimeCommandRejected


class PromptToolSyncTests(unittest.TestCase):
    def setUp(self):
        self.runtime = object.__new__(PiRuntimeHostManager)
        self.runtime._lock = threading.RLock()
        self.runtime._open_sessions = {"session"}
        self.runtime._states = {"session": _HostedSessionState()}
        self.runtime._retired_host_turns = set()
        self.runtime._host_capabilities = {"nativeMcpExecutionPolicy": True}
        self.runtime._cancel_idle_locked = Mock()
        self.runtime._schedule_idle_locked = Mock()
        self.runtime.config = SimpleNamespace(command_timeout_seconds=1)
        self.runtime.sessions = Mock()
        self.runtime.sessions.get.return_value = {"id": "session"}
        self.runtime.events = Mock()
        self.operation = "plan_submit"
        self.manifests = []
        self.methods = []
        self.runtime._tool_manifest_provider = Mock(side_effect=self.manifest)
        self.client = Mock(running=True)
        self.client.send.side_effect = self.send
        self.runtime._client = self.client
        self.runtime._require_client = Mock(return_value=self.client)

    def manifest(self, session):
        state = self.runtime._states["session"]
        self.assertTrue(state.prompt_admission_in_flight)
        self.assertFalse(state.prompt_dispatched)
        return [{"name": "room_partner", "description": "Current purpose", "parameters": {
            "type": "object", "properties": {"op": {"const": self.operation}}, "required": ["op"]}}]

    def send(self, method, params, *, before_write=None, **kwargs):
        if before_write is not None:
            before_write()
        self.methods.append(method)
        if method == "tools.sync":
            self.manifests.append(params["tools"])
            self.assertFalse(self.runtime._states["session"].prompt_dispatched)
            return {"tools": params["tools"]}
        if method == "session.prompt":
            return {"turnId": "turn-" + params["clientMessageId"]}
        raise AssertionError("unexpected method " + method)

    def prompt(self):
        return self.runtime.prompt("session", "one responsibility", client_message_id=self.operation)

    def test_reused_session_refreshes_current_manifest_before_each_purpose_prompt(self):
        for operation in ("plan_submit", "verification_submit", "final_submit"):
            self.operation = operation
            self.runtime._states["session"] = _HostedSessionState()
            self.assertTrue(self.prompt()["accepted"])
        self.assertEqual(self.methods, ["tools.sync", "session.prompt"] * 3)
        self.assertEqual([m[0]["parameters"]["properties"]["op"]["const"] for m in self.manifests],
                         ["plan_submit", "verification_submit", "final_submit"])

    def test_delayed_retired_prompt_ack_preserves_newer_admission_and_turn(self):
        for replacement in ("reserved", "stopping", "running", "host_replaced"):
            with self.subTest(replacement=replacement):
                self.setUp()
                self.operation = "old-dispatch"
                def send(method, params, *, before_write=None, replacement=replacement, **kwargs):
                    if before_write:
                        before_write()
                    if method == "tools.sync":
                        return {"tools": params["tools"]}
                    self.runtime._states["session"] = _HostedSessionState(retired_turn_ids={"turn-old"})
                    self.runtime._retired_host_turns.add(("session", "turn-old"))
                    self.runtime.reserve_prompt_admission("session", client_message_id="new-dispatch")
                    state = self.runtime._states["session"]
                    if replacement == "stopping":
                        state.abort_pending_admission = True
                    elif replacement == "running":
                        state.prompt_admission_in_flight = False
                        state.admission_client_message_id = ""
                        state.turn_id, state.client_message_id = "turn-new", "new-dispatch"
                    elif replacement == "host_replaced":
                        self.runtime._client = Mock(running=True)
                    self.runtime.sessions.set_status.reset_mock()
                    return {"turnId": "turn-old"}
                self.client.send.side_effect = send
                result = self.prompt()
                self.assertEqual(result["turnId"], "turn-old")
                state = self.runtime._states["session"]
                if replacement == "running":
                    self.assertEqual((state.turn_id, state.client_message_id), ("turn-new", "new-dispatch"))
                else:
                    self.assertTrue(state.prompt_admission_in_flight)
                    self.assertEqual(state.admission_client_message_id, "new-dispatch")
                self.assertEqual(state.abort_pending_admission, replacement == "stopping")
                self.runtime.sessions.set_status.assert_not_called()

    def test_manifest_and_sync_failures_are_proven_pre_prompt_rejections(self):
        for site in ("manifest", "sync", "timeout", "invalid_ack"):
            with self.subTest(site=site):
                self.setUp()
                if site == "manifest":
                    self.runtime._tool_manifest_provider.side_effect = ValueError("manifest unavailable")
                elif site == "invalid_ack":
                    self.client.send.side_effect = lambda *args, **kwargs: {}
                else:
                    self.client.send.side_effect = (PiRuntimeCommandAcceptanceUnknown("sync ACK lost")
                        if site == "timeout" else RuntimeError("reload failed"))
                with self.assertRaises(PiRuntimeCommandRejected) as caught:
                    self.prompt()
                self.assertEqual(caught.exception.host_error_code, "TOOL_MANIFEST_SYNC_FAILED")
                self.assertNotIn("session.prompt", self.methods)
                self.assertFalse(self.runtime._states["session"].prompt_admission_in_flight)

    def test_stop_during_sync_fences_prompt_without_marking_prompt_dispatched(self):
        def send(method, params, **kwargs):
            result = self.send(method, params, **kwargs)
            if method == "tools.sync":
                self.runtime._states["session"].abort_pending_admission = True
            return result
        self.client.send.side_effect = send
        with self.assertRaises(PiRuntimeCommandRejected) as caught:
            self.prompt()
        self.assertEqual(caught.exception.host_error_code, "PROMPT_ADMISSION_CANCELLED")
        self.assertEqual(self.methods, ["tools.sync"])
        self.assertFalse(self.runtime._states["session"].prompt_admission_in_flight)

    def test_sync_write_guard_rejects_replacement_admission_without_clearing_it(self):
        for phase in ("before_sync", "after_sync", "before_prompt"):
            with self.subTest(phase=phase):
                self.setUp()
                self.replace_admission_during_sync(phase)

    def replace_admission_during_sync(self, phase):
        def replace_admission():
            self.runtime._states["session"] = _HostedSessionState(prompt_admission_in_flight=True,
                admission_client_message_id="new-dispatch")
        def send(method, params, **kwargs):
            if (phase == "before_sync" and method == "tools.sync") or (
                phase == "before_prompt" and method == "session.prompt"):
                replace_admission()
            result = self.send(method, params, **kwargs)
            if phase == "after_sync" and method == "tools.sync":
                replace_admission()
            return result
        self.client.send.side_effect = send
        with self.assertRaises(PiRuntimeCommandRejected):
            self.prompt()
        self.assertEqual(self.methods, [] if phase == "before_sync" else ["tools.sync"])
        self.assertEqual(self.runtime._states["session"].admission_client_message_id, "new-dispatch")
        self.assertTrue(self.runtime._states["session"].prompt_admission_in_flight)
        self.assertTrue(all(call.args[:2] != ("session", "idle")
                            for call in self.runtime.sessions.set_status.call_args_list))

    def test_memory_curator_does_not_gain_product_tools_from_dynamic_sync(self):
        self.runtime.sessions.get.return_value = {"id": "session", "toolProfileVersion": MEMORY_CURATION_TOOL_PROFILE}
        self.assertTrue(self.prompt()["accepted"])
        self.assertEqual(self.manifests, [[]])
        self.runtime._tool_manifest_provider.assert_not_called()

    def test_unknown_dispatched_admission_is_neither_resynchronized_nor_reprompted(self):
        self.runtime._states["session"] = _HostedSessionState(prompt_admission_in_flight=True,
            admission_client_message_id=self.operation, prompt_dispatched=True)
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.prompt()
        self.assertEqual(self.methods, [])
        self.assertTrue(self.runtime._states["session"].prompt_dispatched)


if __name__ == "__main__":
    unittest.main()
