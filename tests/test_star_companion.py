from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rag_ime.agent_service import AgentService
from rag_ime.agent_tool_ids import DANGEROUS_AUTO_APPROVE_TOOL_PROFILE
from rag_ime.agent_tools import ControlToolGateway
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.values import PiRuntimeError
from tests.sqlite_fixtures import copy_current_database


class StarCompanionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-star-companion-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        db = self.root / "state.sqlite"
        copy_current_database(db)
        self.service = AgentService(db_path=db, runtime_config=PiRuntimeConfig(
            enabled=False, executable=None, agent_dir=self.root / "config",
            session_dir=self.root / "sessions", logs_dir=self.root / "logs",
        ))
        self.addCleanup(self.service.close)
        self.runtime = self.service.runtime
        self.runtime._host_capabilities = {
            "sessionEngines": {"durable": {"available": True, "version": "1"}},
        }
        self.host = Mock()
        host_patch = patch.object(self.runtime, "_host", return_value=self.host)
        host_patch.start()
        self.addCleanup(host_patch.stop)

    def prompt(self, session):
        enriched = {**session, **self.service._runtime_session_context(session)}
        return self.runtime.config.system_prompt_for_session(enriched)

    def test_new_source_is_durable_without_path_or_goal_and_children_stay_classic(self):
        source = self.service.ensure_coordinator({})["session"]
        self.assertEqual(source["runtimeEngine"], "durable")
        self.assertEqual(source["title"], "星伴")
        # Empty creation input keeps the existing full-disk policy's internal root.
        self.assertEqual(source["workspaceRoots"], ["/"])
        self.assertEqual(source["executionMode"], "full_trust")
        self.assertFalse(source["piSkillsEnabled"])
        self.assertFalse(source["codexSkillsEnabled"])
        binding = self.service.sessions.runtime_binding(source["id"])
        self.assertEqual((binding["runtimeKind"], binding["state"]), ("pi_durable", "prepared"))
        self.assertFalse(self.service.sessions.require_goal_execution(source["id"])["goal"]["configured"])
        child = self.service.coordinator_command({
            "sourceSessionId": source["id"], "action": "create_session", "clientRequestId": "child",
            "input": {"task": "Inspect notes"},
        })["target"]
        self.assertEqual(child["runtimeEngine"], "classic")
        self.assertEqual(child["executionMode"], "full_trust")
        daily = self.service.ensure_primary_assistant({})["session"]
        self.assertEqual((daily["runtimeEngine"], daily["executionMode"]), ("classic", "read_only"))
        self.host.send.assert_not_called()

    def test_existing_classic_source_preserves_binding_snapshot_and_history(self):
        legacy = self.service.create_session({"title": "Existing Agent", "mode": "coordinator",
            "runtimeEngine": "classic", "executionMode": "full_trust",
            "toolProfileVersion": DANGEROUS_AUTO_APPROVE_TOOL_PROFILE})["session"]
        transcript = self.root / "sessions" / "legacy.jsonl"
        transcript.parent.mkdir()
        transcript.write_text('{"type":"message","message":{"role":"user","content":"Keep history"}}\n')
        snapshot = {"schemaVersion": "rag-ime.pi-session-resource-snapshot.v1", "skillPolicy": "allowlist",
                    "skillRefs": ["existing-skill"], "promptSettings": {"systemInstructions": "Keep original"}}
        self.service.sessions.bind_runtime_session(legacy["id"], driver_id="managed-pi", runtime_kind="pi_rpc",
            external_session_id="legacy-native", transcript_ref=str(transcript), branch_anchor="legacy-leaf",
            metadata={"resourceSnapshot": snapshot}, message_count=1)
        with self.service.sessions._connect() as conn:
            conn.execute("INSERT INTO agent_coordinators VALUES (1, ?, ?, ?)",
                         ("coordinator:legacy", legacy["id"], 1))
        prior = self.service.sessions.get(legacy["id"])
        binding = self.service.sessions.runtime_binding(legacy["id"])
        contents = transcript.read_bytes()
        self.runtime._host_capabilities = {}
        result = self.service.ensure_coordinator({})
        self.assertFalse(result["created"])
        self.assertEqual(result["session"], prior)
        self.assertEqual(self.service.sessions.runtime_binding(legacy["id"]), binding)
        self.assertEqual(transcript.read_bytes(), contents)
        self.assertEqual(result["session"]["runtimeEngine"], "classic")
        self.assertIn('runtime-engine="classic"', self.prompt(result["session"]))
        self.host.send.assert_not_called()

    def test_persona_is_scoped_to_actual_source_not_coordinator_mode_or_role(self):
        source = self.service.ensure_coordinator({})["session"]
        prompt = self.prompt(source)
        self.assertIn('<persistent-agent-policy version="1" runtime-engine="durable">', prompt)
        for term in ("星伴", "普通聊天", "Goal", "受治理", "Session", "Room", "回执", "原执行", "重复", "后台", "Code Mode"):
            self.assertIn(term, prompt)
        ordinary = self.service.create_session({"title": "Ordinary coordinator", "mode": "coordinator"})["session"]
        self.assertNotIn("<persistent-agent-policy", self.prompt(ordinary))
        self.assertNotIn("<persistent-agent-policy", self.prompt(self.service.ensure_primary_assistant({})["session"]))
        spoofed = {**ordinary, "_persistentCoordinator": self.service._runtime_session_context(source).get("_persistentCoordinator")}
        self.assertNotIn("<persistent-agent-policy", self.prompt(spoofed))
        self.assertNotIn("<persistent-agent-policy", self.runtime.config.system_prompt_for_session(spoofed))

    def test_archived_source_no_longer_receives_persona(self):
        source = self.service.ensure_coordinator({})["session"]
        self.service.sessions.archive(source["id"], archived=True)
        self.assertNotIn("<persistent-agent-policy", self.prompt(self.service.sessions.get(source["id"])))

    def test_missing_durable_capability_never_creates_classic_fallback(self):
        self.runtime._host_capabilities = {}
        with self.assertRaisesRegex(PiRuntimeError, "Durable"):
            self.service.ensure_coordinator({})
        self.assertEqual(self.service.sessions.list(include_internal=True), [])
        with self.service.sessions._read_connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM agent_coordinators").fetchone()[0], 0)
        self.host.send.assert_not_called()

    def test_durable_source_uses_direct_gateway_and_rejects_codemode(self):
        source = self.service.ensure_coordinator({})["session"]
        gateway = ControlToolGateway(sessions=self.service.sessions, management=object(), core=object(),
                                     project="test", collaboration=self.service)
        manifest = next(item for item in gateway.runtime_manifests(source) if item["name"] == "agents")
        self.assertIn("coordinator", manifest["parameters"]["properties"]["op"]["enum"])
        result = gateway.execute({"schemaVersion": "rag-ime.agent-tool-call.v1", "sessionId": source["id"],
            "toolCallId": "direct-create", "tool": "agents", "args": {"op": "coordinator", "action": "create_session",
            "clientRequestId": "direct-child", "input": {"task": "Direct Gateway task"}}})
        self.assertEqual(result["operation"], "coordinator")
        self.assertTrue(result["result"]["created"])
        self.assertEqual(result["result"]["target"]["runtimeEngine"], "classic")
        with self.assertRaisesRegex(PiRuntimeError, "Durable.*Code Mode"):
            self.runtime.set_codemode_mode(source["id"], mode="on")
        self.host.send.assert_not_called()

    def test_capture_is_owned_passive_classic_with_original_assistant_permissions(self):
        source = self.service.ensure_coordinator({})["session"]
        self.service.sessions.set_model_profile(source["id"], "openai-codex/gpt-6.1-sol")
        self.service.sessions.set_thinking_level(source["id"], "xhigh")
        gateway = ControlToolGateway(sessions=self.service.sessions, management=object(), core=object(),
                                     project="test", collaboration=self.service)
        request = {"schemaVersion": "rag-ime.agent-tool-call.v1", "sessionId": source["id"],
            "toolCallId": "capture", "tool": "agents", "args": {"op": "coordinator", "action": "create_session",
            "clientRequestId": "capture-session", "input": {"purpose": "screen_capture", "title": "选区对话",
            "task": "Discuss this screen selection"}}}
        result = gateway.execute(request)
        capture = result["result"]["target"]
        self.assertEqual((capture["runtimeEngine"], capture["mode"], capture["executionMode"]),
                         ("classic", "assistant", "per_action"))
        defaults = self.service.configuration_store.snapshot()["configuration"]["sessionDefaults"]
        self.assertEqual(capture["toolProfileVersion"], defaults["toolProfileVersion"])
        self.assertEqual(capture["workspaceRoots"], [])
        self.assertEqual((capture["modelProfile"], capture["thinkingLevel"]), ("openai-codex/gpt-6.1-sol", "xhigh"))
        self.assertFalse(self.service.sessions.agent_goal(capture["id"])["configured"])
        self.assertEqual(capture["messageCount"], 0)
        self.assertNotIn("<persistent-agent-policy", self.prompt(capture))
        self.assertEqual(gateway.execute(request), result)
        objects = self.service.coordinator_command({"sourceSessionId": source["id"], "action": "read"})["objects"]
        self.assertEqual([(o["id"], o["sourceSessionId"]) for o in objects], [(capture["id"], source["id"])])
        with self.assertRaisesRegex(ValueError, "different"):
            self.service.coordinator_command({"sourceSessionId": source["id"], "action": "create_session",
                "clientRequestId": "capture-session", "input": {"title": "选区对话", "task": "Discuss this screen selection"}})
        self.host.send.assert_not_called()

    def test_capture_purpose_cannot_adopt_existing_sessions_or_enter_room_creation(self):
        source = self.service.ensure_coordinator({})["session"]
        existing = self.service.create_session({"title": "Existing"})["session"]
        for action, input_value in (
            ("create_session", {"purpose": "screen_capture", "sessionId": existing["id"], "task": "Capture"}),
            ("create_session", {"purpose": "unknown", "task": "Capture"}),
            ("create_room", {"purpose": "screen_capture", "task": "Capture"}),
        ):
            with self.subTest(action=action, input=input_value), self.assertRaises(ValueError):
                self.service.coordinator_command({"sourceSessionId": source["id"], "action": action,
                    "clientRequestId": "invalid-capture", "input": input_value})
        self.assertEqual(self.service.coordinator_command({"sourceSessionId": source["id"], "action": "read"})["objects"], [])
