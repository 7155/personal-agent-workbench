"""Real SQLite/Gateway owner tests, explicitly zero-model and not native UI."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_lab import acceptance_fixtures as f
from rag_ime.db import apply_database_migrations


class AcceptanceMemoryStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with sqlite3.connect(":memory:") as conn:
            apply_database_migrations(conn)
            cls.pristine = conn.serialize()

    def test_correction_and_source_withdrawal_use_production_owners(self):
        for case in sorted(f.MEMORY_CASES):
            with self.subTest(case=case), sqlite3.connect(":memory:") as conn:
                conn.deserialize(self.pristine)
                conn.execute("PRAGMA foreign_keys=ON")
                result = f.memory_exercise_storage(conn, case)
                self.assertTrue(result["passed"], result["checks"])
                self.assertEqual(result["providerCalls"], 0)
                self.assertEqual(len(result["checks"]), 17 if case == "memory-source-forget-v1" else 10)


class AcceptanceGatewayStorageTests(unittest.TestCase):
    def setUp(self):
        from rag_ime.agent_service import AgentService
        from rag_ime.debug_server import DebugImeService, DebugServerConfig
        from rag_ime.pi.config import PiRuntimeConfig
        from tests.sqlite_fixtures import copy_current_database
        temporary = tempfile.TemporaryDirectory(prefix="paw-acceptance-offline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "state.sqlite"
        copy_current_database(self.db)
        self.service = AgentService(db_path=self.db, runtime_config=PiRuntimeConfig(
            enabled=False, executable=None, agent_dir=self.root / "unused-config",
            session_dir=self.root / "sessions", logs_dir=self.root / "logs"),
            startup_recovery_enabled=False, wake_scheduler_enabled=False)
        self.addCleanup(self.service.close)
        self.app = DebugImeService(DebugServerConfig(db_path=self.db, agent_service=self.service,
            seed_if_empty=False, memory_projection_worker_enabled=False,
            rime_user_dir=self.root / "Rime", rime_lexicon_backup_root=self.root / "RimeBackups"))
        prompt = patch.object(self.service.runtime, "prompt", side_effect=AssertionError("offline test must not call provider"))
        prompt.start()
        self.addCleanup(prompt.stop)

    def test_authorized_a_read_write_and_b_absolute_traversal_denials(self):
        a, b = f.scope_materialize(self.root / "fixture")
        task = f.scope_create_task(self.service, a, "scope-offline")
        proof = f.scope_gateway_proof(self.app.agent_tools, task["session"]["id"], a, b)
        self.assertTrue(proof["passed"], proof["checks"])
        self.assertEqual(len(proof["checks"]), 9)
        self.assertEqual(proof["providerCalls"], 0)

    def test_memory_is_opt_in_and_disabling_removes_projection(self):
        storage = f.memory_prepare_storage(self.db, "personal-profile-update-v1")
        self.assertTrue(storage["passed"])
        sid = self.service.ensure_primary_assistant({})["session"]["id"]
        self.assertEqual(self.service.memory_context_application.personal_profile_context(sid), "")
        self.service.update_session(sid, {"capabilityDisclosurePreferences": {"tool:memory": "enabled"}})
        checks, context = f.memory_projection_checks(self.service, sid, "personal-profile-update-v1")
        self.assertTrue(all(checks.values()), checks)
        self.assertIn(f.MEMORY_FIXTURE["new"], context)
        self.service.update_session(sid, {"capabilityDisclosurePreferences": {"tool:memory": "disabled"}})
        self.assertEqual(self.service.memory_context_application.personal_profile_context(sid), "")

    def test_invoice_scope_allows_only_fixture_file_tools_and_goal(self):
        work = self.root / "invoices"
        f.invoice_materialize(work)
        from rag_ime.agent_session_application import WORKSPACE_SCOPE_CONFIRMATION
        source = self.service.ensure_primary_assistant({"workspaceRoots": [str(work)]})["session"]
        task = self.service.create_primary_task({"clientRequestId": "invoice-offline", "sourceSessionId": source["id"],
            "objective": f.INVOICE_PROMPT, "acceptanceCriteria": ["Exact immutable-input reconciliation"],
            "workspaceRoots": [str(work)], "workspaceScopeConfirmation": WORKSPACE_SCOPE_CONFIRMATION})["session"]
        allowed = ["workspace_read", "workspace_write", "workspace_edit", "workspace_list", "workspace_search", "agent_goal"]
        changed = self.service.update_session(task["id"], {"allowedTools": allowed})["session"]
        self.assertEqual(set(changed["allowedTools"]), set(allowed))
        self.assertEqual(changed["workspaceRoots"], [str(work)])
        self.assertEqual(changed["executionMode"], "workspace_managed")
        self.assertEqual(self.service.sessions.agent_goal(task["id"])["status"], "active")


if __name__ == "__main__":
    unittest.main()
