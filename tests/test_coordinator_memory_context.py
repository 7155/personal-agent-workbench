from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rag_ime.agent_context_runtime import RUNTIME_PROMPT_ENVELOPE_PREFIX
from rag_ime.agent_service import AgentService
from rag_ime.personal_profile import read_personal_profile, save_personal_profile
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.settings_store import ManagementSettingsStore
from rag_ime.retrieval_docs import rebuild_retrieval_docs
from rag_ime.text_utils import now_ms
from tests.sqlite_fixtures import copy_current_database


class CoordinatorMemoryContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-source-memory-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.sqlite"
        copy_current_database(self.db)
        self.config = PiRuntimeConfig(enabled=False, executable=None, agent_dir=self.root / "config",
            session_dir=self.root / "sessions", logs_dir=self.root / "logs")
        self.service = AgentService(db_path=self.db, runtime_config=self.config)
        self.addCleanup(self.service.close)
        with patch.object(self.service.runtime, "require_session_engine"):
            self.source = self.service.ensure_coordinator({})["sourceSessionId"]
        self.save_profile([{ "id": None, "memoryIds": [], "text": "公开测试：我偏好简短中文回复。"}], "seed")

    def save_profile(self, paragraphs, request):
        # Seed/edit only the temporary user's profile through its real owner.
        # This is not a coordinator governed-write acceptance.
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return save_personal_profile(conn, {
                "expectedRevision": read_personal_profile(conn)["revision"],
                "clientRequestId": request, "paragraphs": paragraphs,
            }, timestamp=100 + len(request))["profile"]

    def enable(self, session_id=None, value="enabled"):
        self.service.sessions.set_disclosure_preferences(session_id or self.source, {"tool:memory": value})

    def context(self, session_id=None):
        return self.service.memory_context_application.personal_profile_context(session_id or self.source)

    def envelope(self):
        native = Mock(return_value={"accepted": True, "turnId": "fixture-turn"})
        with patch.object(self.service.runtime, "prompt", native), \
             patch.object(self.service.runtime, "_host", side_effect=AssertionError("no Host open")) as host, \
             patch.object(self.service.prompt_delivery_application, "runtime_tool_manifest", return_value=[]):
            self.service.prompt_delivery_application.deliver(self.source, "测试新的任务", source_kind="user")
            host.assert_not_called()
        encoded = native.call_args.args[1]
        self.assertTrue(encoded.startswith(RUNTIME_PROMPT_ENVELOPE_PREFIX))
        return json.loads(encoded[len(RUNTIME_PROMPT_ENVELOPE_PREFIX):])

    def test_true_source_without_primary_metadata_reads_current_profile_when_explicitly_enabled(self):
        self.enable()
        session = self.service.sessions.get(self.source)
        self.assertFalse(session.get("metadata", {}).get("primaryAssistant"))
        self.assertFalse(session.get("metadata", {}).get("primaryTask"))
        self.assertIn("公开测试：我偏好简短中文回复。", self.context())
        self.assertIn("profile-sources", self.context())

    def test_true_source_delivery_contains_current_profile_as_data(self):
        self.enable()
        envelope = self.envelope()
        self.assertIn("公开测试：我偏好简短中文回复。", envelope["sessionContext"])
        self.assertIn("不是指令或授权", envelope["sessionContext"])
        self.assertNotIn("公开测试：我偏好简短中文回复。", envelope.get("message", ""))


    def profile(self):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return read_personal_profile(conn)

    def refresh(self, trigger="session_start", fail=False):
        spec = {"session_id": self.source, "source_kind": "memory_bootstrap", "title": "fixture recall",
                "summary": "", "payload": {"schemaVersion": "rag-ime.session-memory-recall.v1", "items": []},
                "lane": "fact", "lifecycle": "session", "dedupe_key": "fixture-source-recall"}
        with patch.object(self.service.memory_bootstrap, "build", return_value=spec,
                          side_effect=RuntimeError("recall unavailable") if fail else None), \
             patch.object(self.service.runtime, "_host", side_effect=AssertionError("no refresh Host")) as host:
            # Native refresh supplies its existing bounded conversation projection.
            # An empty projection uses the separate existing Runtime inspection path.
            result = self.service.refresh_session_context({"sessionId": self.source, "trigger": trigger,
                "recentMessages": [{"role": "user", "content": "公开测试：新的任务"}]})["result"]["sessionContext"]
            host.assert_not_called()
            return result

    def test_default_memory_remains_disabled_even_for_full_trust_source(self):
        self.assertEqual(self.service.sessions.get(self.source)["executionMode"], "full_trust")
        self.assertFalse(self.service._session_memory_disclosed(self.source))
        with patch.object(self.service.memory_context_application, "_personal_profile_provider",
                          side_effect=AssertionError("disabled profile must not be read")) as reader:
            self.assertEqual(self.context(), "")
            self.assertNotIn("personal-profile", self.envelope()["sessionContext"])
            reader.assert_not_called()
        self.assertEqual(self.service.sessions.get(self.source)["capabilityDisclosurePreferences"], {})

    def test_mode_or_forged_identity_metadata_does_not_grant_source_scope(self):
        ordinary = self.service.create_session({"mode": "coordinator"})["session"]
        self.enable(ordinary["id"])
        forged = {**self.service.sessions.get(ordinary["id"]), "metadata": {
            "coordinatorId": "pretend-source", "sourceSessionId": ordinary["id"],
            "persistentCoordinator": True}, "_persistentCoordinator": {"coordinatorId": "pretend-source"}}
        with patch.object(self.service.sessions, "get", return_value=forged):
            self.assertEqual(self.context(ordinary["id"]), "")

    def test_owned_worker_does_not_inherit_source_profile_scope(self):
        target = self.service.coordinator_command({"sourceSessionId": self.source, "action": "create_session",
            "clientRequestId": "worker", "input": {"task": "公开测试工作"}})["target"]["id"]
        self.enable(target)
        self.assertEqual(self.context(target), "")
        self.enable()
        self.assertIn("personal-profile", self.context())

    def test_archived_source_loses_scope_without_transfer_to_another_session(self):
        self.enable()
        self.assertIn("personal-profile", self.context())
        self.service.sessions.set_status(self.source, "archived")
        self.assertEqual(self.context(), "")
        ordinary = self.service.create_session({"mode": "coordinator"})["session"]["id"]
        self.enable(ordinary)
        self.assertEqual(self.context(ordinary), "")

    def test_failed_identity_reader_removes_profile_instead_of_using_warm_text(self):
        self.enable()
        self.assertIn("personal-profile", self.context())
        with patch.object(self.service.memory_context_application, "_personal_profile_scope_provider",
                          side_effect=RuntimeError("identity unavailable")):
            self.assertEqual(self.context(), "")
            self.assertNotIn("personal-profile", self.envelope()["sessionContext"])

    def test_reopen_and_compaction_read_same_source_current_revision_without_host(self):
        self.enable()
        self.assertIn("公开测试：我偏好简短中文回复。", self.refresh())
        identity = self.service._runtime_session_context(self.service.sessions.get(self.source))["_persistentCoordinator"]
        self.service.close()
        self.service = AgentService(db_path=self.db, runtime_config=self.config)
        self.addCleanup(self.service.close)
        self.assertEqual(self.service._runtime_session_context(self.service.sessions.get(self.source))["_persistentCoordinator"], identity)
        with patch.object(self.service.runtime, "_host", side_effect=AssertionError("no passive Host")) as host:
            self.assertIn("公开测试：我偏好简短中文回复。", self.context())
            host.assert_not_called()
        self.assertIn(self.profile()["revision"], self.refresh("compaction"))

    def test_correction_and_delete_remove_old_values_from_next_delivery_and_refresh(self):
        self.enable()
        self.assertIn("简短中文", self.envelope()["sessionContext"])
        old = self.profile()["paragraphs"][0]
        changed = self.save_profile([{**old, "text": "公开测试：我现在偏好详细中文回复。"}], "correct")
        new = changed["paragraphs"][0]
        self.assertNotEqual(old["revision"], new["revision"])
        for context in (self.envelope()["sessionContext"], self.refresh("compaction"), self.refresh(fail=True)):
            self.assertIn("详细中文", context)
            self.assertNotIn("简短中文", context)
        self.save_profile([{**new, "text": ""}], "delete")
        for context in (self.context(), self.envelope()["sessionContext"], self.refresh("compaction"), self.refresh(fail=True)):
            self.assertNotIn("personal-profile", context)
            self.assertNotIn("详细中文", context)

    def test_revoked_original_source_is_not_reinjected_when_card_text_stays(self):
        self.enable()
        self.assertIn("personal-profile", self.envelope()["sessionContext"])
        # Deterministic authority revocation fault in the temporary source DB.
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE agent_memory_evidence SET admission_state='forgotten'")
        self.assertEqual(self.profile()["text"], "")
        self.assertNotIn("personal-profile", self.envelope()["sessionContext"])
        self.assertNotIn("personal-profile", self.refresh(fail=True))

    def test_global_or_session_off_removes_warm_profile_without_read_or_data_deletion(self):
        self.enable()
        self.assertIn("personal-profile", self.envelope()["sessionContext"])
        settings = ManagementSettingsStore(self.db, preverified_schema=True)
        for switch in ("global", "session"):
            with self.subTest(switch=switch):
                if switch == "global":
                    settings.update_settings({"memory.enabled": False})
                else:
                    self.enable(value="disabled")
                with patch.object(self.service.memory_context_application, "_personal_profile_provider",
                                  side_effect=AssertionError("disabled provider read")) as reader:
                    self.assertEqual(self.context(), "")
                    self.assertNotIn("personal-profile", self.envelope()["sessionContext"])
                    self.assertNotIn("personal-profile", self.refresh("compaction"))
                    reader.assert_not_called()
                self.assertIn("公开测试", self.profile()["text"])
                if switch == "global":
                    settings.update_settings({"memory.enabled": True})
                else:
                    self.enable()
        self.assertIn("personal-profile", self.envelope()["sessionContext"])


    def test_room_participant_exclusion_wins_even_for_a_verified_source(self):
        self.enable()
        source = {**self.service.sessions.get(self.source), "roomParticipant": {"roomId": "test-room"}}
        with patch.object(self.service.sessions, "get", return_value=source):
            self.assertEqual(self.context(), "")

    def test_source_revalidates_persisted_recall_after_original_card_deletion(self):
        self.enable()
        card = self.profile()["paragraphs"][0]
        # Same existing user scope, explicitly retrieval-visible fixture domain.
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("UPDATE memory_atoms SET knowledge_domain='user_profile_preference' WHERE id=?", (card["id"],))
            rebuild_retrieval_docs(conn, preverified_schema=True)
        recalled = {"sourceType": "memory_atom", "sourceId": card["id"], "text": card["text"]}
        payload = {"schemaVersion": "rag-ime.session-memory-recall.v1", "items": [recalled],
                   "generatedAtMs": now_ms() + 1, "query": {"preview": "回复偏好"}}
        item = self.service.context_runtime.enqueue(session_id=self.source, source_kind="memory_bootstrap",
            title="公开测试召回", payload=payload, lane="fact", lifecycle="persistent", dedupe_key="test-memory")
        item = next(row for row in self.service.context_runtime.materialize(self.source)["items"]
                    if row["itemId"] == item["itemId"])
        with patch("rag_ime.session_memory_recall.retrieve_hybrid_rag_candidates",
                   side_effect=AssertionError("no retrieval/model during revalidation")):
            current = self.service.memory_context_application.current_memory_items(self.source, [item])
            self.assertEqual(current[0]["payload"]["items"], [recalled])
            self.assertIn(card["text"], self.envelope()["sessionContext"])
            self.save_profile([{**self.profile()["paragraphs"][0], "text": ""}], "delete-recalled")
            current = self.service.memory_context_application.current_memory_items(self.source, [item])
            self.assertEqual(current[0]["payload"]["items"], [])
            self.assertNotIn(card["text"], str(self.envelope()))
        persisted = self.service.context_runtime.materialize(self.source)["items"]
        self.assertTrue(any(row["itemId"] == item["itemId"] for row in persisted))


if __name__ == "__main__":
    unittest.main()
