from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from rag_ime.cli import main
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_compiler import build_memory_compile_bundle
from rag_ime.memory_generator import (
    CoreOptimizationReport,
    GeneratedLexiconPhrase,
    GeneratedMemoryItem,
    SuggestedHiddenEvent,
    MemoryGeneratorConfig,
)
from rag_ime.memory_models import ImeQueryContext
from rag_ime.models import InputEvent
import rag_ime.memory_compiler as memory_compiler_module


class FakeCompilerGenerator:
    calls: list[dict[str, object]] = []

    def __init__(self, config: MemoryGeneratorConfig):
        self.config = config

    @property
    def provider_name(self) -> str:
        return "deepseek-v4"

    @classmethod
    def from_env_path(cls, env_path=None):
        cls.calls.append({"envPath": str(env_path or "")})
        return cls(
            MemoryGeneratorConfig(
                api_base_url="https://api.example.com/v1",
                api_key="fake-key",
                model="deepseek-v4-flash",
            )
        )

    def optimize_core(
        self,
        *,
        snapshot: dict[str, object],
        project: str = "wisdom-weasel-rag-ime",
        max_memories: int = 4,
        max_lexicon_phrases: int = 8,
        max_hide_suggestions: int = 12,
    ) -> CoreOptimizationReport:
        self.__class__.calls.append(
            {
                "project": project,
                "snapshot": snapshot,
                "maxMemories": max_memories,
                "maxLexiconPhrases": max_lexicon_phrases,
                "maxHideSuggestions": max_hide_suggestions,
                "model": self.config.model,
            }
        )
        return CoreOptimizationReport(
            provider="deepseek-v4",
            model=self.config.model,
            elapsed_ms=23,
            memories=(
                GeneratedMemoryItem(
                    text="本地检索优先",
                    tags=("project_requirement", "memory"),
                    importance=0.91,
                    reason="稳定项目要求",
                    source="compiler",
                ),
            ),
            lexicon_phrases=(
                GeneratedLexiconPhrase(
                    text="连续预测",
                    tags=("continuation",),
                    weight=0.83,
                    reason="高频短语",
                ),
            ),
            hide_events=(
                SuggestedHiddenEvent(event_id=1, reason="raw echo pollution"),
            ),
            raw_text='{"ok":true}',
            metadata={"wireApi": "responses"},
        )


class MemoryCompilerDiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-memory-compiler-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_cli_json(self, *args: str) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["--db-path", str(self.db_path), *args])
        return code, json.loads(stdout.getvalue())

    def test_build_memory_compile_bundle_redacts_secrets_and_paths(self) -> None:
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_000_100,
                source="manual",
                committed_text="token sk-abcdef1234567890 路径 /Users/example/private/note.txt",
                privacy_disposition="allowed",
                recent_context="联系我: test@example.com",
                project="wisdom-weasel-rag-ime",
                app="manual",
                tags=("sensitive-like",),
            )
        )

        bundle = build_memory_compile_bundle(
            self.core,
            project="wisdom-weasel-rag-ime",
            recent_limit=20,
            phrase_limit=20,
            memory_limit=20,
            governance_limit=20,
            allow_private_paths=False,
        )
        serialized = json.dumps(bundle, ensure_ascii=False)

        self.assertNotIn("sk-abcdef1234567890", serialized)
        self.assertNotIn("/Users/example/private/note.txt", serialized)
        self.assertNotIn("test@example.com", serialized)
        self.assertIn("[REDACTED_SECRET]", serialized)
        self.assertIn("[REDACTED_PATH]", serialized)
        # Email has already been removed by the capture ingress policy.
        self.assertIn("[REDACTED]", serialized)

    def test_memory_compile_cli_is_dry_run_by_default_and_apply_rollback_uses_diff(self) -> None:
        original = memory_compiler_module.DeepSeekMemoryGenerator
        FakeCompilerGenerator.calls = []
        memory_compiler_module.DeepSeekMemoryGenerator = FakeCompilerGenerator
        try:
            self.core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=1_900_000_000_101,
                    source="manual",
                    committed_text="这是旧的 raw input 长句，后续应该被编译器建议 tombstone",
                    privacy_disposition="allowed",
                    recent_context="RAG 输入法 old raw event",
                    project="wisdom-weasel-rag-ime",
                    app="manual",
                    tags=("user-input",),
                )
            )
            self.core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=1_900_000_000_102,
                    source="manual",
                    committed_text="本地检索优先",
                    privacy_disposition="allowed",
                    recent_context="这是项目里长期稳定的输入法偏好",
                    project="wisdom-weasel-rag-ime",
                    app="manual",
                    tags=("project_requirement", "memory"),
                )
            )
            plan_path = Path(self.tmp.name) / "memory-compile-plan.json"

            code, compile_payload = self._run_cli_json(
                "memory-compile",
                "--project",
                "wisdom-weasel-rag-ime",
                "--model-env-path",
                str(Path(self.tmp.name) / "fake.env"),
                "--provider",
                "deepseek-v4",
                "--model",
                "deepseek-v4-flash",
                "--output",
                str(plan_path),
            )
            self.assertEqual(code, 0)
            self.assertTrue(compile_payload["dryRun"])
            self.assertFalse(compile_payload["storedDraft"])
            self.assertTrue(plan_path.exists())
            self.assertTrue(compile_payload["run"]["runId"])
            self.assertEqual(self.core.list_memory_cleanup_runs(limit=10)["items"], [])
            ops = [item["op"] for item in compile_payload["run"]["diffs"]]
            self.assertEqual(ops, ["add_stable_memory", "add_phrase", "tombstone"])
            self.assertEqual(FakeCompilerGenerator.calls[-1]["model"], "deepseek-v4-flash")
            stable_diff = next(item for item in compile_payload["run"]["diffs"] if item["op"] == "add_stable_memory")
            self.assertEqual(stable_diff["payload"]["evidenceEventIds"], [2])
            self.assertEqual(stable_diff["payload"]["sourceStats"]["strategy"], "auto-backfill")
            self.assertEqual(stable_diff["payload"]["sourceStats"]["selectedEventIds"], [2])
            self.assertGreaterEqual(stable_diff["payload"]["sourceStats"]["bestScore"], 3.0)

            code, apply_payload = self._run_cli_json(
                "memory-compile-apply",
                "--diff",
                str(plan_path),
            )
            self.assertEqual(code, 0)
            self.assertEqual(apply_payload["run"]["status"], "applied")

            inspect_payload = self.core.inspect_memory_v2(project="wisdom-weasel-rag-ime", limit=20)
            memory_ids = {item["memoryId"] for item in inspect_payload["items"]}
            self.assertIn("stable:本地检索优先", memory_ids)
            self.assertIn("phrase:连续预测", memory_ids)

            phrase_candidates = self.core.retrieve_candidates_v2(
                context=ImeQueryContext(current_input="连续", project="wisdom-weasel-rag-ime", top_k=5)
            )
            phrase_texts = [item["text"] for item in phrase_candidates["candidates"]]
            self.assertIn("连续预测", phrase_texts)

            stable_candidates = self.core.retrieve_candidates_v2(
                context=ImeQueryContext(current_input="本地检索", project="wisdom-weasel-rag-ime", top_k=5)
            )
            stable_texts = [item["text"] for item in stable_candidates["candidates"]]
            self.assertIn("本地检索优先", stable_texts)

            governance = self.core.inspect_memory_governance(limit=20)
            self.assertTrue(any(item["targetType"] == "source_event_id" and item["targetValue"] == "1" for item in governance["tombstones"]))

            code, rollback_payload = self._run_cli_json(
                "memory-compile-rollback",
                "--run-id",
                compile_payload["run"]["runId"],
            )
            self.assertEqual(code, 0)
            self.assertEqual(rollback_payload["run"]["status"], "rolled_back")

            inspect_after = self.core.inspect_memory_v2(project="wisdom-weasel-rag-ime", limit=20)
            memory_ids_after = {item["memoryId"] for item in inspect_after["items"]}
            self.assertNotIn("stable:本地检索优先", memory_ids_after)
            self.assertNotIn("phrase:连续预测", memory_ids_after)
        finally:
            memory_compiler_module.DeepSeekMemoryGenerator = original

    def test_memory_compile_save_draft_preserves_review_workflow(self) -> None:
        original = memory_compiler_module.DeepSeekMemoryGenerator
        FakeCompilerGenerator.calls = []
        memory_compiler_module.DeepSeekMemoryGenerator = FakeCompilerGenerator
        try:
            self.core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=1_900_000_000_201,
                    source="manual",
                    committed_text="这是旧的 raw input 长句，后续应该被编译器建议 tombstone",
                    privacy_disposition="allowed",
                    recent_context="RAG 输入法 old raw event",
                    project="wisdom-weasel-rag-ime",
                    app="manual",
                    tags=("user-input",),
                )
            )
            self.core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=1_900_000_000_202,
                    source="manual",
                    committed_text="本地检索优先",
                    privacy_disposition="allowed",
                    recent_context="这是项目里长期稳定的输入法偏好",
                    project="wisdom-weasel-rag-ime",
                    app="manual",
                    tags=("project_requirement", "memory"),
                )
            )

            code, compile_payload = self._run_cli_json(
                "memory-compile",
                "--project",
                "wisdom-weasel-rag-ime",
                "--model-env-path",
                str(Path(self.tmp.name) / "fake.env"),
                "--provider",
                "deepseek-v4",
                "--model",
                "deepseek-v4-flash",
                "--save-draft",
            )
            self.assertEqual(code, 0)
            self.assertTrue(compile_payload["storedDraft"])
            self.assertTrue(
                any(
                    item["runId"] == compile_payload["run"]["runId"]
                    for item in self.core.list_memory_cleanup_runs(limit=10)["items"]
                )
            )

            code, review_payload = self._run_cli_json(
                "memory-compile-review",
                "--run-id",
                compile_payload["run"]["runId"],
                "--status",
                "approved",
            )
            self.assertEqual(code, 0)
            self.assertEqual(review_payload["schemaVersion"], "rag-ime.memory-cleanup-review.v1")
            self.assertEqual(review_payload["run"]["status"], "reviewed")
            self.assertTrue(all(item["status"] == "approved" for item in review_payload["run"]["diffs"]))

            code, apply_payload = self._run_cli_json(
                "memory-compile-apply",
                "--run-id",
                compile_payload["run"]["runId"],
                "--only-approved",
            )
            self.assertEqual(code, 0)
            self.assertEqual(apply_payload["run"]["status"], "applied")
        finally:
            memory_compiler_module.DeepSeekMemoryGenerator = original
