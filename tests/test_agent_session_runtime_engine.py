from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tool_ids import DANGEROUS_AUTO_APPROVE_TOOL_PROFILE
from rag_ime.db import apply_database_migrations


class AgentSessionRuntimeEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-session-engine-")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "product.sqlite"
        self.store = AgentSessionStore(self.db)
        self.addCleanup(self.store.close)
        self.store.initialize()

    def test_new_durable_choice_survives_restart_and_projection_only_listing(self) -> None:
        created = self.store.create(title="持续工作", runtime_engine="durable")
        prepared = self.store.runtime_binding(created["id"])
        self.assertEqual(prepared["runtimeKind"], "pi_durable")
        self.assertEqual(prepared["state"], "prepared")
        self.assertEqual(prepared["transcriptRef"], "")
        restarted = AgentSessionStore(self.db)
        self.addCleanup(restarted.close)
        restarted.initialize()
        self.assertEqual(restarted.get(created["id"])["runtimeEngine"], "durable")
        listed = restarted.list_page(projection_only=True)["items"]
        self.assertEqual(listed[0]["runtimeEngine"], "durable")
        self.assertEqual(listed[0]["id"], created["id"])
        with self.assertRaisesRegex(ValueError, "cannot change runtime driver"):
            restarted.bind_runtime_session(created["id"], driver_id="managed-pi",
                                            runtime_kind="pi_rpc", external_session_id=created["id"])

    def test_ordinary_sessions_keep_classic_engine_and_existing_status_changes(self) -> None:
        created = self.store.create(title="已有工作")
        self.assertEqual(created["runtimeEngine"], "classic")
        self.store.set_status(created["id"], "busy")
        self.assertEqual(self.store.get(created["id"])["runtimeEngine"], "classic")

    def test_invalid_engine_never_creates_a_record(self) -> None:
        with self.assertRaisesRegex(ValueError, "runtime engine"):
            self.store.create(title="错误配置", runtime_engine="auto")
        self.assertEqual(self.store.list(), [])

    def test_full_trust_skill_flags_match_engine_at_creation_and_policy_updates(self) -> None:
        for engine in ("classic", "durable"):
            with self.subTest(engine=engine):
                session = self.store.create(title=f"Full trust {engine}", mode="coordinator",
                    runtime_engine=engine, tool_profile_version=DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
                    execution_mode="full_trust", workspace_roots=[], pi_skills_enabled=False,
                    codex_skills_enabled=False)
                self.assertEqual(session["workspaceRoots"], ["/"])
                self.assertEqual(session["executionMode"], "full_trust")
                self.assertEqual(session["toolProfileVersion"], DANGEROUS_AUTO_APPROVE_TOOL_PROFILE)
                self.assertEqual(session["piSkillsEnabled"], engine == "classic")
                self.assertEqual(session["codexSkillsEnabled"], engine == "classic")
                binding = self.store.runtime_binding(session["id"])
                updated = self.store.set_runtime_policy(session["id"], mode="coordinator", allowed_tools=None,
                    tool_profile_version=DANGEROUS_AUTO_APPROVE_TOOL_PROFILE, execution_mode="full_trust",
                    workspace_roots=[], pi_skills_enabled=True, codex_skills_enabled=True)
                self.assertEqual(updated["runtimeEngine"], engine)
                self.assertEqual(updated["workspaceRoots"], ["/"])
                self.assertEqual(updated["executionMode"], "full_trust")
                self.assertEqual(updated["piSkillsEnabled"], engine == "classic")
                self.assertEqual(updated["codexSkillsEnabled"], engine == "classic")
                self.assertEqual(self.store.runtime_binding(session["id"]), binding)

    def test_existing_durable_policy_update_cannot_enable_unsupported_skills(self) -> None:
        for engine in ("classic", "durable"):
            with self.subTest(engine=engine):
                session = self.store.create(title=f"Existing {engine}", mode="coordinator", runtime_engine=engine)
                self.assertFalse(session["piSkillsEnabled"])
                self.assertFalse(session["codexSkillsEnabled"])
                updated = self.store.set_runtime_policy(session["id"], mode="coordinator", allowed_tools=None,
                    tool_profile_version=DANGEROUS_AUTO_APPROVE_TOOL_PROFILE, execution_mode="full_trust",
                    workspace_roots=[], grant_workspace_scope=True, pi_skills_enabled=True, codex_skills_enabled=True)
                self.assertEqual(updated["workspaceRoots"], ["/"])
                self.assertEqual(updated["runtimeEngine"], engine)
                self.assertEqual(updated["executionMode"], "full_trust")
                self.assertEqual(updated["piSkillsEnabled"], engine == "classic")
                self.assertEqual(updated["codexSkillsEnabled"], engine == "classic")

    def test_durable_is_not_implicitly_adopted_by_internal_or_app_owned_sessions(self) -> None:
        with self.assertRaisesRegex(ValueError, "standalone"):
            self.store.create(title="后台工具", session_kind="subagent_runtime", runtime_engine="durable")
        with self.assertRaisesRegex(ValueError, "standalone"):
            self.store.create(title="记忆整理", surface_kind="builtin_app", owner_app_id="memory",
                              surface_key="timeline", runtime_engine="durable")
        self.assertEqual(self.store.list(include_internal=True), [])

    def test_append_only_migration_retains_old_session_and_transcript_binding(self) -> None:
        legacy_dir = Path(self.tmp.name) / "legacy-migrations"
        legacy_dir.mkdir()
        source = Path(__file__).resolve().parents[1] / "rag_ime/db/migrations"
        for migration in source.glob("*.sql"):
            if int(migration.name[:4]) <= 217:
                shutil.copyfile(migration, legacy_dir / migration.name)
        legacy_db = Path(self.tmp.name) / "legacy.sqlite"
        with closing(sqlite3.connect(legacy_db)) as conn:
            apply_database_migrations(conn, migrations_dir=legacy_dir)
            conn.execute("""INSERT INTO agent_sessions(
                id, title, session_mode, role_id, role_version, model_profile,
                tool_profile_version, created_at_ms, updated_at_ms, last_opened_at_ms,
                status, pi_session_id, session_file, last_message_preview
            ) VALUES ('agent:legacy', '原始标题', 'assistant', 'default', '1',
                'openai-codex/gpt-6.1-sol', 'control-center-v1', 1, 2, 1, 'idle',
                'pi-legacy', '/owned/legacy.jsonl', '原始回执')""")
            conn.commit()
        migrated = AgentSessionStore(legacy_db)
        self.addCleanup(migrated.close)
        migrated.initialize()
        migrated.initialize()
        session = migrated.get("agent:legacy")
        self.assertEqual(session["runtimeEngine"], "classic")
        self.assertEqual(session["title"], "原始标题")
        self.assertEqual(session["piSessionId"], "pi-legacy")
        self.assertEqual(session["sessionFile"], "/owned/legacy.jsonl")
        self.assertEqual(session["lastMessagePreview"], "原始回执")


if __name__ == "__main__":
    unittest.main()
