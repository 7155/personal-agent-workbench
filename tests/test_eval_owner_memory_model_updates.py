from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_memory_sources import AgentMemorySourceStore
from rag_ime.deepseek_memory_organizer import ManagedPiMemoryOrganizer
from scripts.eval_owner_memory_model_updates import (
    CapturingOrganizer,
    CONFLICT_BASELINE,
    CONFLICT_INPUT,
    FORGET_INPUT,
    PROJECT,
    _build_organizer,
    _checkpoint,
    _evaluate,
    _model_execution_summary,
    _seed_current_memory,
    build_parser,
)


class OwnerMemoryModelUpdateEvaluatorTests(unittest.TestCase):
    def test_provider_failure_is_not_score_eligible_model_execution(self) -> None:
        organizer = SimpleNamespace(calls=[])
        summary = _model_execution_summary(
            organizer,  # type: ignore[arg-type]
            [
                {
                    "ok": False,
                    "results": [
                        {
                            "ok": False,
                            "error": "provider process exited before completion",
                        }
                    ],
                }
            ],
        )

        self.assertEqual("failed_before_completion", summary["status"])
        self.assertFalse(summary["completed"])
        self.assertEqual(0, summary["modelCallCount"])
        self.assertEqual(1, summary["failedResultCount"])

    def test_capturing_wrapper_preserves_protocol_and_model_run_lifecycle(self) -> None:
        class Delegate:
            provider_name = "openai-codex"
            curation_protocol_version = "atom-first-v1"

            def __init__(self) -> None:
                self.events: list[object] = []

            def begin_run(self, run_id: str, *, frozen_input_sha256: str):
                self.events.append(("begin", run_id, frozen_input_sha256))
                return {"runId": run_id}

            def finish_run(self):
                self.events.append("finish")
                return {"state": "completed"}

            def fail_run(self, error: BaseException):
                self.events.append(("fail", type(error).__name__))
                return {"state": "failed"}

            def close(self) -> None:
                self.events.append("close")

        delegate = Delegate()
        organizer = CapturingOrganizer(delegate)  # type: ignore[arg-type]

        self.assertEqual("atom-first-v1", organizer.curation_protocol_version)
        self.assertEqual(
            {"runId": "run-1"},
            organizer.begin_run("run-1", frozen_input_sha256="a" * 64),
        )
        self.assertEqual({"state": "completed"}, organizer.finish_run())
        self.assertEqual({"state": "failed"}, organizer.fail_run(RuntimeError("boom")))
        organizer.close()
        self.assertEqual(
            [
                ("begin", "run-1", "a" * 64),
                "finish",
                ("fail", "RuntimeError"),
                "close",
            ],
            delegate.events,
        )

    def test_luna_organizer_identity_is_fixed_without_model_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owner-memory-luna-binding-") as temporary:
            root = Path(temporary)
            db_path = root / "memory.sqlite"
            AgentSessionStore(db_path).initialize()

            organizer, identity = _build_organizer(
                organizer_kind="luna",
                db_path=db_path,
                artifact_root=root / "artifacts",
                model_env_path=root / "unused.env",
                codex_bin="codex",
                timeout_seconds=321,
            )

            self.assertIsInstance(organizer.delegate, ManagedPiMemoryOrganizer)
            self.assertEqual(identity["provider"], "openai-codex")
            self.assertEqual(identity["model"], "gpt-6.1-sol")
            self.assertEqual(identity["thinking"], "max")
            self.assertEqual(identity["transport"], "codex_cli_ephemeral")
            executor = organizer.delegate.completion_executor
            self.assertEqual(executor.timeout_seconds, 321.0)
            self.assertEqual(executor.audit_db_path, db_path.resolve())

    def test_cli_keeps_configured_default_and_exposes_explicit_luna_lane(self) -> None:
        parser = build_parser()

        self.assertEqual(parser.parse_args([]).organizer, "configured")
        args = parser.parse_args(
            [
                "--organizer",
                "luna",
                "--timeout-seconds",
                "600",
                "--codex-bin",
                "codex-local",
                "--artifact-root",
                ".rag-ime-data/private-memory-eval",
            ]
        )
        self.assertEqual(args.organizer, "luna")
        self.assertEqual(args.timeout_seconds, 600.0)
        self.assertEqual(args.codex_bin, "codex-local")
        self.assertEqual(args.artifact_root, ".rag-ime-data/private-memory-eval")

    def test_unknown_organizer_fails_before_credentials_or_model_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owner-memory-invalid-binding-") as temporary:
            root = Path(temporary)
            db_path = root / "memory.sqlite"
            AgentSessionStore(db_path).initialize()

            with self.assertRaisesRegex(ValueError, "unsupported organizer"):
                _build_organizer(
                    organizer_kind="other",
                    db_path=db_path,
                    artifact_root=root / "artifacts",
                    model_env_path=root / "unused.env",
                    codex_bin="codex",
                    timeout_seconds=60,
                )

    def test_explicit_forget_gate_requires_retraction_tombstone_and_book_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owner-memory-forget-gate-") as temporary:
            db_path = Path(temporary) / "memory.sqlite"
            AgentSessionStore(db_path).initialize()
            _seed_current_memory(db_path, ())
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    UPDATE memory_atoms
                    SET status = 'tombstoned', claim_state = 'retracted', valid_to_ms = 20
                    WHERE id = 'atom:live-eval:forget:v1'
                    """
                )
                conn.execute(
                    """
                    UPDATE memory_books
                    SET status = 'archived', memory_atom_ids_json = '[]'
                    WHERE title = '个人协作偏好'
                    """
                )
                conn.execute(
                    """
                    INSERT INTO memory_tombstones(
                        created_at_ms, target_type, target_value, reason, active,
                        metadata_json
                    ) VALUES (20, 'memory_id', 'atom:live-eval:forget:v1',
                              'explicit_user_forget', 1, '{}')
                    """
                )
                conn.commit()

            with patch(
                "scripts.eval_owner_memory_model_updates._source_rows",
                return_value={
                    "source:forget": {"disposition": "consolidated"},
                    "source:conflict": {"disposition": "not_for_memory"},
                },
            ):
                report = _evaluate(
                    db_path=db_path,
                    scenarios=(),
                    v3_count=0,
                    update_sources={},
                    semantic_noise_sources={},
                    deterministic_noise_sources={},
                    forget_source_id="source:forget",
                    conflict_source_id="source:conflict",
                    organizer=SimpleNamespace(calls=[]),
                    reports=[],
                )

            self.assertTrue(report["passed"], report)
            self.assertEqual(report["metrics"]["forgetCaseCount"], 1)
            self.assertEqual(report["metrics"]["forgetRetractedCount"], 1)
            self.assertEqual(report["metrics"]["invalidBookMemberCount"], 0)

    def test_temporary_conflict_gate_preserves_current_claim_and_rejects_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owner-memory-conflict-gate-") as temporary:
            db_path = Path(temporary) / "memory.sqlite"
            AgentSessionStore(db_path).initialize()
            _seed_current_memory(db_path, ())
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    UPDATE memory_atoms
                    SET status = 'tombstoned', claim_state = 'retracted', valid_to_ms = 20
                    WHERE id = 'atom:live-eval:forget:v1'
                    """
                )
                conn.execute(
                    """
                    UPDATE memory_books
                    SET memory_atom_ids_json = '["atom:live-eval:conflict:v1"]'
                    WHERE title = '个人协作偏好'
                    """
                )
                conn.execute(
                    """
                    INSERT INTO memory_tombstones(
                        created_at_ms, target_type, target_value, reason, active,
                        metadata_json
                    ) VALUES (20, 'memory_id', 'atom:live-eval:forget:v1',
                              'explicit_user_forget', 1, '{}')
                    """
                )
                conn.commit()

            with patch(
                "scripts.eval_owner_memory_model_updates._source_rows",
                return_value={
                    "source:forget": {"disposition": "consolidated"},
                    "source:conflict": {"disposition": "not_for_memory"},
                },
            ):
                report = _evaluate(
                    db_path=db_path,
                    scenarios=(),
                    v3_count=0,
                    update_sources={},
                    semantic_noise_sources={},
                    deterministic_noise_sources={},
                    forget_source_id="source:forget",
                    conflict_source_id="source:conflict",
                    organizer=SimpleNamespace(calls=[]),
                    reports=[],
                )

            self.assertTrue(report["passed"], report)
            self.assertEqual(
                report["currentFacts"].get("user:communication:answer-length"),
                None,
            )
            self.assertEqual(report["metrics"]["temporaryConflictCaseCount"], 1)
            self.assertEqual(report["metrics"]["temporaryConflictPreservedCount"], 1)
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT canonical_text, status, claim_state
                    FROM memory_atoms
                    WHERE id = 'atom:live-eval:conflict:v1'
                    """
                ).fetchone()
            self.assertEqual(tuple(row), (CONFLICT_BASELINE, "active", "current"))

    def test_forget_and_temporary_conflict_inputs_enter_the_isolated_review_batch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owner-memory-lifecycle-input-") as temporary:
            db_path = Path(temporary) / "memory.sqlite"
            sessions = AgentSessionStore(db_path)
            sessions.initialize()
            session = sessions.create(
                title="lifecycle",
                role_id="companion-present-v1",
                role_version="1",
                created_at_ms=1,
            )
            store = AgentMemorySourceStore(db_path, project=PROJECT)
            store.initialize()

            source_ids = [
                _checkpoint(
                    store,
                    session_id=str(session["id"]),
                    label=f"lifecycle:{index}",
                    text=text,
                    created_at_ms=100 + index,
                    capture_candidate=True,
                )
                for index, text in enumerate((FORGET_INPUT, CONFLICT_INPUT), start=1)
            ]

            self.assertTrue(all(source_ids))
            self.assertEqual(
                [store.get(source_id)["disposition"] for source_id in source_ids],
                ["pending", "pending"],
            )
            with sqlite3.connect(db_path) as conn:
                evidence_rows = conn.execute(
                    """
                    SELECT knowledge_domain, scope_mode, evidence_domain,
                           origin_kind, admission_state
                    FROM agent_memory_evidence
                    ORDER BY evidence_id
                    """
                ).fetchall()
            self.assertEqual(len(evidence_rows), 2)
            self.assertTrue(
                all(
                    tuple(row)
                    == (
                        "personal_memory",
                        "authoritative",
                        "personal_memory",
                        "explicit_user_memory",
                        "candidate",
                    )
                    for row in evidence_rows
                )
            )


if __name__ == "__main__":
    unittest.main()
