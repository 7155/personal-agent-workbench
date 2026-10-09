from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.pi.config import PiRuntimeConfig
from tests.sqlite_fixtures import copy_current_database


class CoordinatorPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-source-preflight-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.sqlite"
        copy_current_database(self.db)
        self.service = AgentService(db_path=self.db, runtime_config=PiRuntimeConfig(enabled=False, executable=None,
            agent_dir=self.root / "config", session_dir=self.root / "sessions", logs_dir=self.root / "logs"))
        self.addCleanup(self.service.close)

    def counts(self):
        with self.service.sessions._read_connect() as conn:
            return tuple(conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
                         for table in ("agent_sessions", "agent_coordinators"))

    def test_durable_capability_check_holds_no_sqlite_writer(self):
        probes = []
        def require_engine(engine):
            self.assertEqual(engine, "durable")
            with sqlite3.connect(self.db, timeout=0.1) as second:
                try:
                    second.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as error:
                    self.fail(f"Durable capability preflight held the Source SQLite write lock: {error}")
                probes.append("second connection acquired writer")
                second.rollback()
        with patch.object(self.service.runtime, "require_session_engine", side_effect=require_engine) as check:
            result = self.service.ensure_coordinator({})
        self.assertTrue(result["created"])
        self.assertEqual(result["session"]["runtimeEngine"], "durable")
        self.assertEqual(check.call_count, 1)
        self.assertEqual(probes, ["second connection acquired writer"])
        self.assertEqual(self.counts(), (1, 1))

    def test_unavailable_engine_leaves_no_source_or_fallback_session(self):
        with patch.object(self.service.runtime, "require_session_engine", side_effect=ValueError("Durable unavailable")):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.service.ensure_coordinator({})
        self.assertEqual(self.counts(), (0, 0))

    def test_parallel_ensure_creates_only_one_source(self):
        with patch.object(self.service.runtime, "require_session_engine"):
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: self.service.ensure_coordinator({}), range(12)))
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(len({result["sourceSessionId"] for result in results}), 1)
        self.assertEqual(len({result["coordinatorId"] for result in results}), 1)
        self.assertEqual(self.counts(), (1, 1))

    def test_existing_classic_source_never_requires_durable_and_retains_history(self):
        old = self.service.create_session({"title": "Original Classic", "runtimeEngine": "classic"})["session"]
        history = self.root / "sessions" / "original.jsonl"
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text('{"type":"session","id":"physical"}\n')
        self.service.sessions.bind_runtime_session(old["id"], driver_id="managed-pi", runtime_kind="pi_rpc",
            external_session_id="physical", transcript_ref=str(history), branch_anchor="", metadata={"resourceSnapshot": {"original": True}})
        with self.service.sessions._connect() as conn:
            conn.execute("INSERT INTO agent_coordinators VALUES (1, ?, ?, ?)", ("coordinator:legacy", old["id"], 1))
        record = self.service.sessions.get(old["id"])
        binding = self.service.sessions.runtime_binding(old["id"])
        contents = history.read_bytes()
        with patch.object(self.service.runtime, "require_session_engine", side_effect=AssertionError("must not negotiate existing Classic")):
            result = self.service.ensure_coordinator({})
        self.assertFalse(result["created"])
        self.assertEqual(result["session"], record)
        self.assertEqual(self.service.sessions.runtime_binding(old["id"]), binding)
        self.assertEqual(history.read_bytes(), contents)
        self.assertEqual(self.counts(), (1, 1))

    def test_archive_rotation_preflight_also_holds_no_writer(self):
        with patch.object(self.service.runtime, "require_session_engine"):
            old = self.service.ensure_coordinator({})
        self.service.sessions.archive(old["sourceSessionId"])
        def probe(_engine):
            with sqlite3.connect(self.db, timeout=0.1) as second:
                try:
                    second.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as error:
                    self.fail(f"Archived Source replacement holds a writer during capability preflight: {error}")
                second.rollback()
        with patch.object(self.service.runtime, "require_session_engine", side_effect=probe):
            new = self.service.ensure_coordinator({})
        self.assertNotEqual(new["sourceSessionId"], old["sourceSessionId"])
        self.assertNotEqual(new["coordinatorId"], old["coordinatorId"])

    def test_internal_preparation_is_bound_to_application_runtime_and_engine(self):
        from dataclasses import replace
        application = self.service.session_application
        with patch.object(self.service.runtime, "require_session_engine"):
            prepared = application._prepare_session_engine("durable")
        invalid = [object(), replace(prepared, application=object()), replace(prepared, engine="classic")]
        with patch.object(self.service.runtime, "require_session_engine", side_effect=AssertionError("no Host wait inside validation")):
            for proof in invalid:
                with self.subTest(proof=type(proof).__name__), self.assertRaisesRegex(ValueError, "different application"):
                    application._create_session_record({"runtimeEngine": "durable"}, _prepared_engine=proof)
            with patch.object(application, "_runtime_provider", return_value=object()):
                with self.assertRaisesRegex(ValueError, "different application"):
                    application._create_session_record({"runtimeEngine": "durable"}, _prepared_engine=prepared)
        self.assertEqual(self.counts(), (0, 0))

    def test_preparation_does_not_bypass_durable_creation_field_rules(self):
        application = self.service.session_application
        with patch.object(self.service.runtime, "require_session_engine"):
            prepared = application._prepare_session_engine("durable")
        with patch.object(self.service.runtime, "require_session_engine", side_effect=AssertionError("no second negotiation")):
            for extra in [{"ownerAppId": "extension:example"}, {"piSkillsEnabled": True}, {"surfaceKind": "extension"}]:
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    application._create_session_record({"runtimeEngine": "durable", **extra}, _prepared_engine=prepared)
        self.assertEqual(self.counts(), (0, 0))

    def test_user_payload_cannot_skip_ordinary_durable_preflight(self):
        with patch.object(self.service.runtime, "require_session_engine", side_effect=ValueError("Durable unavailable")) as check:
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.service.create_session({"runtimeEngine": "durable", "_prepared_engine": {"runtimeEngine": "durable"}})
        check.assert_called_once_with("durable")
        self.assertEqual(self.counts(), (0, 0))
