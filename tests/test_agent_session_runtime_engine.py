from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from rag_ime.agent_sessions import AgentSessionStore
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
