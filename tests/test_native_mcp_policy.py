from __future__ import annotations

import json
import unittest

from rag_ime.agent_execution_policy import native_mcp_execution_allowed
from rag_ime.pi.values import PiRuntimeError
from tests import test_pi_runtime_v2 as fixtures


class NativeMcpPolicyTests(unittest.TestCase):
    setUp = fixtures.PiRuntimeV2Tests.setUp
    tearDown = fixtures.PiRuntimeV2Tests.tearDown
    _record_event = fixtures.PiRuntimeV2Tests._record_event
    _observe_compaction = fixtures.PiRuntimeV2Tests._observe_compaction

    def requests(self, method):
        return [row["params"] for row in map(json.loads,
            (self.root / "agent" / "host-requests.jsonl").read_text().splitlines())
            if row["method"] == method]

    def policy(self, session_id, profile, execution_mode=None):
        return self.store.set_runtime_policy(session_id, mode="coordinator",
            tool_profile_version=profile, execution_mode=execution_mode,
            allowed_tools=["memory"], workspace_roots=[str(self.root)])

    def test_only_existing_unrestricted_profiles_grant_native_mcp(self):
        for profile, mode, allowed in [
            ("control-center-full-access-v1", "per_action", True),
            ("control-center-auto-approve-v1", "full_trust", True),
            ("control-center-full-access-v1", "read_only", False),
            ("subagent-readonly-v1", "full_trust", False),
            ("memory-curation-v1", "full_trust", False),
            ("control-center-v1", "workspace_managed", False),
            ("control-center-v1", "per_action", False),
            ("", "", False),
        ]:
            with self.subTest(profile=profile, mode=mode):
                self.assertEqual(native_mcp_execution_allowed({
                    "toolProfileVersion": profile, "executionMode": mode}), allowed)

    def test_open_and_prompt_sync_derive_current_policy_without_removing_backend_reads(self):
        sid = str(self.first["id"])
        self.policy(sid, "control-center-full-access-v1")
        self.runtime.ensure(sid)
        self.assertTrue(self.requests("session.open")[-1]["nativeMcpExecutionAllowed"])
        self.policy(sid, "subagent-readonly-v1", "read_only")
        self.runtime.prompt(sid, "inspect only", client_message_id="mcp-denied")
        fixtures._wait_until(lambda: self.store.get(sid)["status"] == "idle")
        sync = self.requests("tools.sync")[-1]
        self.assertFalse(sync["nativeMcpExecutionAllowed"])
        self.assertEqual(sync["tools"][0]["name"], "memory")

    def test_memory_curation_denies_native_mcp_with_empty_gateway_tools(self):
        sid = str(self.first["id"])
        self.policy(sid, "memory-curation-v1")
        self.runtime.ensure(sid)
        self.runtime.prompt(sid, "curate only", client_message_id="mcp-curation")
        fixtures._wait_until(lambda: self.store.get(sid)["status"] == "idle")
        for params in [self.requests("session.open")[-1], self.requests("tools.sync")[-1]]:
            self.assertFalse(params["nativeMcpExecutionAllowed"])
            self.assertEqual(params.get("toolManifest", params.get("tools")), [])

    def test_fork_uses_restricted_target_policy_instead_of_source_grant(self):
        source, target = str(self.first["id"]), str(self.second["id"])
        self.policy(source, "control-center-full-access-v1")
        self.policy(target, "subagent-readonly-v1", "read_only")
        self.runtime.prompt(source, "fork this", client_message_id="mcp-source")
        fixtures._wait_until(lambda: self.store.get(source)["status"] == "idle")
        self.runtime.fork_session(source, target, entry_id="entry-user-1")
        self.assertFalse(self.requests("session.fork")[-1]["nativeMcpExecutionAllowed"])

    def test_old_host_cannot_silently_ignore_restricted_policy(self):
        self.runtime.available_models()
        self.runtime._host_capabilities.pop("nativeMcpExecutionPolicy")
        with self.assertRaisesRegex(PiRuntimeError, "cannot enforce"):
            self.runtime.ensure(str(self.first["id"]))
        self.assertEqual(self.requests("session.open"), [])

    def test_sync_without_manifest_provider_still_transmits_policy(self):
        sid = str(self.first["id"])
        self.runtime.ensure(sid)
        self.runtime._tool_manifest_provider = None
        self.runtime.prompt(sid, "no backend tools", client_message_id="mcp-no-tools")
        fixtures._wait_until(lambda: self.store.get(sid)["status"] == "idle")
        self.assertEqual(self.requests("tools.sync")[-1], {
            "sessionId": sid, "tools": [], "nativeMcpExecutionAllowed": False,
        })

    def test_mcp_command_rechecks_revoked_durable_policy_before_reconnect(self):
        sid = str(self.first["id"])
        self.policy(sid, "control-center-full-access-v1")
        self.runtime.ensure(sid)
        self.policy(sid, "subagent-readonly-v1", "read_only")
        with self.assertRaisesRegex(PiRuntimeError, "denied"):
            self.runtime.invoke_command(sid, "/mcp reconnect fixture")
        self.assertEqual(self.requests("session.command.invoke"), [])
