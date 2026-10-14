from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from rag_ime.cli import main
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_generator import (
    CoreOptimizationReport,
    GeneratedLexiconPhrase,
    GeneratedMemoryItem,
    MemoryGeneratorConfig,
)
from rag_ime.models import InputEvent, MemoryAction
import rag_ime.memory_compiler as memory_compiler_module


class FakeCleanupCompilerGenerator:
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
            elapsed_ms=19,
            memories=(
                GeneratedMemoryItem(
                    text="输入法优先使用本地检索",
                    tags=("memory",),
                    importance=0.9,
                    reason="归纳项目偏好: 本地检索优先",
                    source="cleanup-preview",
                ),
            ),
            lexicon_phrases=(
                GeneratedLexiconPhrase(
                    text="连续预测",
                    tags=("continuation",),
                    weight=0.8,
                    reason="高频短语",
                ),
            ),
            raw_text='{"ok":true}',
            metadata={"wireApi": "chat_completions"},
        )


class CleanupPipelineCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-cleanup-pipeline-")
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

    def test_cleanup_preview_local_rule_is_dry_run_and_apply_requires_flag(self) -> None:
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_001,
                source="manual",
                committed_text="连续预测",
                privacy_disposition="allowed",
                recent_context="RAG 输入法",
                project="wisdom-weasel-rag-ime",
                tags=("phrase-memory",),
            )
        )
        self.core.apply_action(
            MemoryAction(
                action_id=None,
                created_at_ms=1_900_000_100_002,
                memory_id="event:1",
                action_type="accepted",
                query="连续",
                metadata={"project": "wisdom-weasel-rag-ime"},
            )
        )
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_003,
                source="manual",
                committed_text="这是一条很长且没有被接受过的历史输入，应该进入 cleanup tombstone",
                privacy_disposition="allowed",
                recent_context="old raw event",
                project="wisdom-weasel-rag-ime",
                tags=("user-input",),
            )
        )
        plan_path = Path(self.tmp.name) / "cleanup-preview.json"

        code, preview = self._run_cli_json(
            "cleanup-preview",
            "--provider",
            "local-rule",
            "--project",
            "wisdom-weasel-rag-ime",
            "--output",
            str(plan_path),
        )

        self.assertEqual(code, 0)
        self.assertTrue(preview["dryRun"])
        self.assertEqual(preview["provider"], "local-rule")
        self.assertTrue(preview["validation"]["ok"])
        self.assertTrue(plan_path.exists())
        self.assertEqual(self.core.list_memory_cleanup_runs(limit=10)["items"], [])

        code, validate = self._run_cli_json("cleanup-validate", "--run", str(plan_path))
        self.assertEqual(code, 0)
        self.assertTrue(validate["ok"])

        code, apply_missing = self._run_cli_json("cleanup-apply", "--run", str(plan_path))
        self.assertEqual(code, 2)
        self.assertFalse(apply_missing["ok"])
        inspect_before = self.core.inspect_memory_v2(project="wisdom-weasel-rag-ime", limit=20)
        self.assertNotIn("stable:连续预测", {item["memoryId"] for item in inspect_before["items"]})

        code, applied = self._run_cli_json("cleanup-apply", "--run", str(plan_path), "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["run"]["status"], "applied")
        inspect_after = self.core.inspect_memory_v2(project="wisdom-weasel-rag-ime", limit=20)
        self.assertIn("stable:连续预测", {item["memoryId"] for item in inspect_after["items"]})

        code, rolled_back = self._run_cli_json("cleanup-rollback", "--run-id", preview["run"]["runId"])
        self.assertEqual(code, 0)
        self.assertTrue(rolled_back["ok"])
        inspect_final = self.core.inspect_memory_v2(project="wisdom-weasel-rag-ime", limit=20)
        self.assertNotIn("stable:连续预测", {item["memoryId"] for item in inspect_final["items"]})

    def test_cleanup_validate_rejects_stable_memory_without_evidence(self) -> None:
        plan_path = Path(self.tmp.name) / "invalid-cleanup.json"
        plan_path.write_text(
            json.dumps(
                {
                    "runId": "cleanup_invalid",
                    "provider": "deepseek-v4",
                    "model": "deepseek-v4-flash",
                    "summary": "stable=1",
                    "diffs": [
                        {
                            "op": "add_stable_memory",
                            "targetMemoryId": "stable:本地检索优先",
                            "payload": {
                                "memoryId": "stable:本地检索优先",
                                "text": "本地检索优先",
                                "project": "wisdom-weasel-rag-ime",
                                "confidence": 0.9,
                                "evidenceEventIds": [],
                            },
                            "status": "pending",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        code, payload = self._run_cli_json("cleanup-validate", "--run", str(plan_path))

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertTrue(any(item["code"] == "missing_evidence_event_ids" for item in payload["errors"]))

    def test_cleanup_downrank_apply_and_rollback_for_repeated_skips(self) -> None:
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_200_001,
                source="manual",
                committed_text="噪声短语",
                privacy_disposition="allowed",
                recent_context="RAG 输入法",
                project="wisdom-weasel-rag-ime",
                tags=("phrase-memory",),
            )
        )
        for offset in range(2):
            self.core.apply_action(
                MemoryAction(
                    action_id=None,
                    created_at_ms=1_900_000_200_010 + offset,
                    memory_id="event:1",
                    action_type="skipped",
                    query="噪声",
                    metadata={"project": "wisdom-weasel-rag-ime"},
                )
            )
        plan_path = Path(self.tmp.name) / "cleanup-downrank.json"

        code, preview = self._run_cli_json(
            "cleanup-preview",
            "--provider",
            "local-rule",
            "--project",
            "wisdom-weasel-rag-ime",
            "--output",
            str(plan_path),
        )

        self.assertEqual(code, 0)
        self.assertTrue(preview["validation"]["ok"])
        self.assertGreaterEqual(preview["validation"]["counts"]["downrank"], 1)
        downrank = next(
            item
            for item in preview["run"]["diffs"]
            if item["op"] == "downrank" and item["targetMemoryId"] == "phrase:噪声短语"
        )
        self.assertEqual(downrank["payload"]["targetType"], "memory_id")
        self.assertEqual(downrank["payload"]["reason"], "cleanup:repeated-negative-feedback")
        before_item = next(
            item
            for item in self.core.inspect_memory_v2(project="wisdom-weasel-rag-ime", limit=20)["items"]
            if item["memoryId"] == "phrase:噪声短语"
        )

        code, applied = self._run_cli_json("cleanup-apply", "--run", str(plan_path), "--apply")

        self.assertEqual(code, 0)
        self.assertTrue(applied["ok"])
        after_item = next(
            item
            for item in self.core.inspect_memory_v2(project="wisdom-weasel-rag-ime", limit=20)["items"]
            if item["memoryId"] == "phrase:噪声短语"
        )
        self.assertLess(after_item["qualityScore"], before_item["qualityScore"])
        governance = self.core.inspect_memory_governance(limit=20)
        self.assertTrue(
            any(
                item["matchType"] == "memory_id"
                and item["matchValue"] == "phrase:噪声短语"
                and item["action"] == "downrank"
                for item in governance["suppressions"]
            )
        )

        code, rolled_back = self._run_cli_json("cleanup-rollback", "--run-id", preview["run"]["runId"])

        self.assertEqual(code, 0)
        self.assertTrue(rolled_back["ok"])
        final_item = next(
            item
            for item in self.core.inspect_memory_v2(project="wisdom-weasel-rag-ime", limit=20)["items"]
            if item["memoryId"] == "phrase:噪声短语"
        )
        self.assertEqual(final_item["qualityScore"], before_item["qualityScore"])
        final_governance = self.core.inspect_memory_governance(limit=20)
        self.assertFalse(
            any(
                item["matchType"] == "memory_id"
                and item["matchValue"] == "phrase:噪声短语"
                and item["action"] == "downrank"
                for item in final_governance["suppressions"]
            )
        )

    def test_cleanup_preview_deepseek_v4_is_dry_run_and_surfaces_validation(self) -> None:
        original = memory_compiler_module.DeepSeekMemoryGenerator
        FakeCleanupCompilerGenerator.calls = []
        memory_compiler_module.DeepSeekMemoryGenerator = FakeCleanupCompilerGenerator
        try:
            self.core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=1_900_000_100_010,
                    source="manual",
                    committed_text="本地检索优先",
                    privacy_disposition="allowed",
                    recent_context="这是项目里长期稳定的输入法偏好",
                    project="wisdom-weasel-rag-ime",
                    tags=("memory",),
                )
            )
            fake_env = Path(self.tmp.name) / "fake.env"
            fake_env.write_text("DEEPSEEK_API_KEY=fake-key\nRAG_IME_DEEPSEEK_MODEL=deepseek-v4-flash\n", encoding="utf-8")
            output_path = Path(self.tmp.name) / "cleanup-deepseek-v4.json"

            code, payload = self._run_cli_json(
                "cleanup-preview",
                "--provider",
                "deepseek-v4",
                "--project",
                "wisdom-weasel-rag-ime",
                "--model-env-path",
                str(fake_env),
                "--output",
                str(output_path),
            )
        finally:
            memory_compiler_module.DeepSeekMemoryGenerator = original

        self.assertEqual(code, 0)
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["provider"], "deepseek-v4")
        self.assertTrue(output_path.exists())
        self.assertEqual(self.core.list_memory_cleanup_runs(limit=10)["items"], [])
        self.assertTrue(payload["validation"]["ok"])
        stable_diff = next(item for item in payload["run"]["diffs"] if item["op"] == "add_stable_memory")
        self.assertEqual(stable_diff["payload"]["evidenceEventIds"], [1])
        self.assertEqual(stable_diff["payload"]["sourceStats"]["strategy"], "auto-backfill")
        self.assertEqual(stable_diff["payload"]["sourceStats"]["selectedEventIds"], [1])
        self.assertGreaterEqual(stable_diff["payload"]["sourceStats"]["bestScore"], 3.0)

    def test_cleanup_validate_rejects_weak_source_stats_even_with_evidence_id(self) -> None:
        plan_path = Path(self.tmp.name) / "weak-source-stats.json"
        plan_path.write_text(
            json.dumps(
                {
                    "runId": "cleanup_weak_source",
                    "provider": "deepseek-v4",
                    "model": "deepseek-v4-flash",
                    "summary": "stable=1",
                    "diffs": [
                        {
                            "op": "add_stable_memory",
                            "targetMemoryId": "stable:输入法优先使用本地检索",
                            "payload": {
                                "memoryId": "stable:输入法优先使用本地检索",
                                "text": "输入法优先使用本地检索",
                                "project": "wisdom-weasel-rag-ime",
                                "confidence": 0.9,
                                "evidenceEventIds": [1],
                                "sourceStats": {
                                    "strategy": "auto-backfill",
                                    "totalEventCount": 20,
                                    "matchedEventCount": 0,
                                    "selectedEventIds": [1],
                                    "bestScore": 1.2,
                                    "minAcceptedScore": 3.0,
                                },
                            },
                            "status": "pending",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        code, payload = self._run_cli_json("cleanup-validate", "--run", str(plan_path))

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        codes = {item["code"] for item in payload["errors"]}
        self.assertIn("source_stats_no_matches", codes)
        self.assertIn("source_stats_weak_match", codes)
