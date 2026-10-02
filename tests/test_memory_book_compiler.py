from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import rag_ime.cli as cli_module
from rag_ime.cli import main
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_curation import (
    build_memory_curation_model_bundle,
    curation_decisions_to_compile_output,
)
from rag_ime.memory_book_compiler import (
    _catalog_supersede_graph_errors,
    apply_memory_book_plan,
    apply_stored_memory_book_run,
    build_memory_book_source_bundle,
    collapse_rime_fragment_run,
    find_newer_applied_memory_book_run,
    find_memory_book_draft_for_bundle,
    inspect_memory_book_plan,
    memory_compile_state,
    memory_book_plan_from_compile_output,
    memory_book_plan_from_stored_run,
    rollback_memory_book_run,
    seal_global_memory_book_plan,
    store_memory_book_plan,
    update_stored_memory_book_diff,
)
from rag_ime.models import InputEvent
from rag_ime.rime_rank_export import record_rime_rank_feedback
from rag_ime.text_utils import now_ms



class FakeManagedMemoryExecutor:
    provider = "deepseek"
    model_id = "deepseek-v4-flash"

    def close(self) -> None:
        return None

class FakeMemoryBookOrganizer:
    def __init__(self, config) -> None:
        self.config = config

    @property
    def provider_name(self) -> str:
        return "deepseek"

    def close(self) -> None:
        return None

    def compile_memory_curation(
        self,
        *,
        bundle: dict[str, object],
        project: str,
        policy: str = "conservative",
    ) -> dict[str, object]:
        del bundle, project, policy
        return {
            "schemaVersion": "rag-ime.memory-curation-decisions.v1",
            "decisions": [
                {
                    "action": "create",
                    "evidenceRefs": ["E1"],
                    "canonicalText": "RAG 输入法使用多路召回。",
                    "kind": "project_requirement",
                    "topicRef": "new:input-method",
                    "topicTitle": "输入法",
                    "tags": ["new:RAG", "new:多路召回"],
                    "confidence": 0.9,
                    "qualityScore": 0.9,
                }
            ],
            "tagMerges": [],
            "warnings": [],
        }


class MemoryBookCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-memory-book-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()
        self.event_id = int(
            self.core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=now_ms(),
                    source="squirrel_assistant_remember",
                    committed_text="RAG 输入法多路召回方案",
                    privacy_disposition="allowed",
                    recent_context="BM25 向量 TagMemo Time DeepSeek",
                    project="wisdom-weasel-rag-ime",
                    tags=("RAG", "输入法"),
                )
            ).split(":", 1)[1]
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_cli_json(self, *args: str) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["--db-path", str(self.db_path), *args])
        return code, json.loads(stdout.getvalue())

    def test_rime_reconstruction_retains_all_physical_source_ids(self) -> None:
        collapsed = collapse_rime_fragment_run(
            [
                {
                    "sourceIds": [f"input-memory:{index}"],
                    "evidenceIds": [f"evidence:input:{index}"],
                    "sourceEventIds": [index],
                    "text": "片",
                    "recentContext": f"累计上下文{index}",
                    "createdAtMs": index,
                }
                for index in range(1, 301)
            ]
        )

        self.assertEqual(len(collapsed["sourceIds"]), 300)
        self.assertEqual(collapsed["sourceIds"][-1], "input-memory:300")
        self.assertEqual(len(collapsed["evidenceIds"]), 300)
        self.assertEqual(collapsed["evidenceId"], "")
        self.assertEqual(len(collapsed["sourceEventIds"]), 300)

    def test_rime_reconstruction_retains_only_surrounding_ax_context(self) -> None:
        prefix = "终端中上一条命令与当前目录信息 " * 4
        collapsed = collapse_rime_fragment_run(
            [
                {
                    "sourceIds": ["input-memory:1"],
                    "evidenceIds": ["evidence:input:1"],
                    "sourceEventIds": [1],
                    "text": "git status",
                    "recentContext": prefix + "git status",
                    "createdAtMs": 1,
                }
            ]
        )

        self.assertEqual(collapsed["text"], "git status")
        self.assertEqual(collapsed["recentContext"], prefix.strip())
        self.assertNotIn("git status", collapsed["recentContext"])

    def test_memory_book_preview_is_dry_run(self) -> None:
        builder_calls: list[
            tuple[tuple[object, ...], dict[str, object]]
        ] = []

        def build_executor(
            *args: object,
            **kwargs: object,
        ) -> FakeManagedMemoryExecutor:
            builder_calls.append((args, kwargs))
            return FakeManagedMemoryExecutor()

        original_builder = cli_module.build_managed_pi_memory_model_executor
        original_organizer = cli_module.ManagedPiMemoryOrganizer
        cli_module.build_managed_pi_memory_model_executor = build_executor
        cli_module.ManagedPiMemoryOrganizer = FakeMemoryBookOrganizer
        try:
            output = Path(self.tmp.name) / "memory-book-preview.json"

            code, payload = self._run_cli_json(
                "memory-book-preview",
                "--project",
                "wisdom-weasel-rag-ime",
                "--output",
                str(output),
            )
        finally:
            cli_module.build_managed_pi_memory_model_executor = original_builder
            cli_module.ManagedPiMemoryOrganizer = original_organizer

        self.assertEqual(code, 0)
        self.assertEqual(
            builder_calls,
            [((self.db_path, "openai-codex/gpt-6.1-sol", "max"), {})],
        )
        self.assertTrue(payload["dryRun"])
        self.assertTrue(payload["validation"]["ok"])
        self.assertTrue(output.exists())
        plan = json.loads(output.read_text(encoding="utf-8"))
        atom = next(item for item in plan["diffs"] if item["op"] == "upsert_memory_atom")
        self.assertEqual(atom["payload"]["canonicalText"], "RAG 输入法使用多路召回。")
        self.assertEqual(plan["metadata"]["curationArchitecture"], "atom-first-v1")
        self.assertFalse(any(item["op"] == "add_phrase_candidate" for item in plan["diffs"]))
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_books").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_cleanup_runs WHERE run_id LIKE 'memory_book_%'").fetchone()[0], 0)

    def test_memory_book_preview_saved_draft_reuses_identical_source_bundle(self) -> None:
        class CountingOrganizer(FakeMemoryBookOrganizer):
            calls = 0

            def compile_memory_curation(
                self,
                *,
                bundle: dict[str, object],
                project: str,
                policy: str = "conservative",
            ) -> dict[str, object]:
                type(self).calls += 1
                return super().compile_memory_curation(
                    bundle=bundle,
                    project=project,
                    policy=policy,
                )

        original_builder = cli_module.build_managed_pi_memory_model_executor
        original_organizer = cli_module.ManagedPiMemoryOrganizer
        cli_module.build_managed_pi_memory_model_executor = (
            lambda *args, **kwargs: FakeManagedMemoryExecutor()
        )
        cli_module.ManagedPiMemoryOrganizer = CountingOrganizer
        try:
            output = Path(self.tmp.name) / "memory-book-review.json"
            arguments = (
                "memory-book-preview",
                "--project",
                "wisdom-weasel-rag-ime",
                "--model",
                "deepseek/deepseek-v4-flash",
                "--output",
                str(output),
                "--save-draft",
            )
            first_code, first = self._run_cli_json(*arguments)
            second_code, second = self._run_cli_json(*arguments)
        finally:
            cli_module.build_managed_pi_memory_model_executor = original_builder
            cli_module.ManagedPiMemoryOrganizer = original_organizer

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertTrue(first["storedDraft"])
        self.assertFalse(first["reusedDraft"])
        self.assertTrue(second["storedDraft"])
        self.assertTrue(second["reusedDraft"])
        self.assertEqual(CountingOrganizer.calls, 1)
        self.assertEqual(first["run"]["runId"], second["run"]["runId"])
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            existing = find_memory_book_draft_for_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                bundle_hash=str(first["source"]["bundleHash"]),
            )
            assert existing is not None
            restored = memory_book_plan_from_stored_run(existing)
            draft_count = conn.execute(
                "SELECT COUNT(*) FROM memory_cleanup_runs WHERE status = 'draft' AND run_id LIKE 'memory_book_%'"
            ).fetchone()[0]
        self.assertEqual(restored["runId"], first["run"]["runId"])
        self.assertTrue(inspect_memory_book_plan(restored)["ok"])
        self.assertEqual(draft_count, 1)

    def test_coalesced_memory_book_draft_supersedes_older_project_draft(self) -> None:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                since_days=7,
                limit=80,
            )
            first_plan = memory_book_plan_from_compile_output(
                sample_compile_output(self.event_id),
                project="wisdom-weasel-rag-ime",
                provider="deepseek",
                model="deepseek-v4-flash",
                source_bundle=bundle,
            )
            store_memory_book_plan(conn, first_plan, supersede_project_drafts=True)
            second_plan = json.loads(json.dumps(first_plan))
            second_plan["runId"] = "memory_book_newer_review"
            second_plan["metadata"]["bundleHash"] = "sha256:newer-bundle"
            store_memory_book_plan(conn, second_plan, supersede_project_drafts=True)
            statuses = {
                str(row["run_id"]): str(row["status"])
                for row in conn.execute(
                    "SELECT run_id, status FROM memory_cleanup_runs WHERE run_id IN (?, ?)",
                    (first_plan["runId"], second_plan["runId"]),
                ).fetchall()
            }
            rejected = conn.execute(
                "SELECT COUNT(*) FROM memory_cleanup_diffs WHERE run_id = ? AND status = 'rejected'",
                (first_plan["runId"],),
            ).fetchone()[0]

        self.assertEqual(statuses[first_plan["runId"]], "superseded")
        self.assertEqual(statuses[second_plan["runId"]], "draft")
        self.assertGreater(rejected, 0)

    def test_catalog_draft_does_not_supersede_incremental_draft(self) -> None:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                since_days=7,
                limit=80,
            )
            incremental = memory_book_plan_from_compile_output(
                sample_compile_output(self.event_id),
                project="wisdom-weasel-rag-ime",
                provider="test",
                model="test",
                source_bundle=bundle,
            )
            incremental["runId"] = "memory_book_incremental_draft"
            store_memory_book_plan(
                conn,
                incremental,
                supersede_project_drafts=True,
            )
            catalog = json.loads(json.dumps(incremental))
            catalog["runId"] = "memory_book_catalog_draft"
            catalog["metadata"]["runKind"] = "catalog_consolidation"
            catalog["metadata"]["bundleHash"] = "sha256:catalog-draft"
            store_memory_book_plan(
                conn,
                catalog,
                supersede_project_drafts=True,
            )
            statuses = {
                str(row["run_id"]): str(row["status"])
                for row in conn.execute(
                    "SELECT run_id, status FROM memory_cleanup_runs "
                    "WHERE run_id IN (?, ?)",
                    (incremental["runId"], catalog["runId"]),
                ).fetchall()
            }

        self.assertEqual(statuses[incremental["runId"]], "draft")
        self.assertEqual(statuses[catalog["runId"]], "draft")

    def test_catalog_supersede_graph_rejects_non_root_targets_in_any_order(
        self,
    ) -> None:
        atoms = {
            atom_id: {"atomId": atom_id}
            for atom_id in ("atom:a", "atom:b", "atom:c")
        }
        accepted_upserts = {
            "atom:b": (0, {}),
            "atom:c": (1, {}),
        }
        forward = [
            (0, {}, "atom:a", "atom:b"),
            (1, {}, "atom:b", "atom:c"),
        ]
        for edges in (forward, list(reversed(forward))):
            with self.subTest(edges=edges):
                errors = _catalog_supersede_graph_errors(
                    edges,
                    atoms_by_id=atoms,
                    accepted_upserts=accepted_upserts,
                )
                self.assertIn("non_root_target:atom:b", errors)


    def test_stale_memory_book_draft_cannot_apply_or_be_reused(self) -> None:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            first_bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                since_days=7,
                limit=80,
            )
            stale_plan = memory_book_plan_from_compile_output(
                sample_compile_output(self.event_id),
                project="wisdom-weasel-rag-ime",
                provider="deepseek",
                model="deepseek-v4-flash",
                source_bundle=first_bundle,
            )
            stale_plan["runId"] = "memory_book_stale_review"
            store_memory_book_plan(conn, stale_plan)

        second_event_id = int(
            self.core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=now_ms(),
                    source="squirrel_assistant_remember",
                    committed_text="后续证据已经生成并应用新的记忆草案",
                    privacy_disposition="allowed",
                    recent_context="",
                    project="wisdom-weasel-rag-ime",
                )
            ).split(":", 1)[1]
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            newer_bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                since_days=7,
                limit=80,
            )
            newer_plan = memory_book_plan_from_compile_output(
                sample_compile_output(second_event_id),
                project="wisdom-weasel-rag-ime",
                provider="deepseek",
                model="deepseek-v4-flash",
                source_bundle=newer_bundle,
            )
            newer_plan["runId"] = "memory_book_newer_apply"
            apply_memory_book_plan(conn, newer_plan)

            with self.assertRaisesRegex(ValueError, "draft is stale"):
                apply_stored_memory_book_run(conn, run_id="memory_book_stale_review")
            reused = find_memory_book_draft_for_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                bundle_hash=str(stale_plan["metadata"]["bundleHash"]),
            )

        self.assertIsNone(reused)

    def test_source_bundle_exposes_delete_and_correction_feedback_to_dsv4(self) -> None:
        self.core.record_memory_feedback(
            {
                "event": "backspace_after_accept",
                "candidateId": "candidate:错别字",
                "candidateText": "错别宇",
                "sourceType": "rime",
                "sourceEventId": self.event_id,
                "project": "wisdom-weasel-rag-ime",
                "metadata": {"deleteCount": 3},
            }
        )
        record_rime_rank_feedback(
            self.db_path,
            preedit="cuo bie zi",
            rejected_text="错别宇",
            accepted_text="错别字",
            action="correction_pair",
            project="wisdom-weasel-rag-ime",
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                since_days=7,
                limit=20,
                after_event_id=0,
            )

        feedback = bundle["feedback"]
        self.assertTrue(any(item["action"] == "backspace_after_accept" for item in feedback))
        self.assertTrue(any(item["deleteCount"] == 3 for item in feedback))
        rank_feedback = bundle["rimeRankFeedback"]
        correction = next(item for item in rank_feedback if item["action"] == "correction_pair")
        self.assertEqual(correction["rejectedText"], "错别宇")
        self.assertEqual(correction["acceptedText"], "错别字")
        self.assertEqual(correction["preedit"], "cuo bie zi")

    def test_source_bundle_excludes_progressive_tool_disclosure_acceptance_probes(self) -> None:
        prompts = (
            "这是渐进工具披露验收。必须先调用 tool_search，再调用 tool_load。",
            "上一轮得到的插件数量是多少？只回复数字，不要调用工具。",
        )
        for prompt in prompts:
            self.core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=now_ms(),
                    source="pi_agent_user",
                    committed_text=prompt,
                    privacy_disposition="allowed",
                    project="wisdom-weasel-rag-ime",
                )
            )
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=now_ms(),
                source="squirrel_assistant_remember",
                committed_text="我长期希望先解释 tool_search 和 tool_load 的职责边界。",
                privacy_disposition="allowed",
                project="wisdom-weasel-rag-ime",
            )
        )

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                since_days=7,
                limit=80,
            )

        texts = [str(item["text"]) for item in bundle["recentEvents"]]
        self.assertNotIn(prompts[0], texts)
        self.assertNotIn(prompts[1], texts)
        self.assertIn("我长期希望先解释 tool_search 和 tool_load 的职责边界。", texts)
        self.assertEqual(bundle["reconstruction"]["droppedRuntimeProbeCount"], 2)

    def test_memory_book_compile_requires_source_event_ids(self) -> None:
        output = sample_compile_output(self.event_id)
        output["dailyBooks"][0]["sourceEventIds"] = []
        plan = memory_book_plan_from_compile_output(output, project="wisdom-weasel-rag-ime", provider="deepseek", model="deepseek-v4-flash")

        report = inspect_memory_book_plan(plan)

        self.assertFalse(report["ok"])
        self.assertTrue(any(item["code"] == "missing_source_event_ids" for item in report["errors"]))

    def test_memory_book_compile_limits_new_semantic_groups(self) -> None:
        output = sample_compile_output(self.event_id)
        output["semanticGroups"] = [
            {
                "groupId": f"group:topic-{index}",
                "title": f"主题 {index}",
                "description": f"第 {index} 个粗粒度内容主题",
                "sourceEventIds": [self.event_id],
            }
            for index in range(5)
        ]
        bundle = {
            "recentEvents": [{"eventId": self.event_id, "text": "RAG 输入法多路召回方案"}],
            "existingSemanticGroups": [],
        }

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        report = inspect_memory_book_plan(plan)

        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["semanticGroups"], 3)
        self.assertIn("semantic_group_new_limit_applied", plan["metadata"]["warnings"])

    def test_memory_atom_uses_external_source_occurrence_time_not_import_time(
        self,
    ) -> None:
        output = sample_compile_output(self.event_id)
        import_time_ms = 1_752_900_000_000
        source_time_ms = 1_752_036_400_000
        bundle = {
            "recentEvents": [
                {
                    "eventId": self.event_id,
                    "sourceEventIds": [self.event_id],
                    "createdAtMs": import_time_ms,
                    "sourceOccurredAtMs": source_time_ms,
                    "text": "Codex 已整理的外部记忆摘要",
                }
            ],
            "inputs": [
                {
                    "sourceEventIds": [self.event_id],
                    "createdAtMs": import_time_ms,
                    "sourceOccurredAtMs": source_time_ms,
                }
            ],
        }

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        atom = next(
            item["payload"]
            for item in plan["diffs"]
            if item["op"] == "upsert_memory_atom"
        )

        self.assertEqual(atom["validFromMs"], source_time_ms)
        self.assertNotEqual(atom["validFromMs"], import_time_ms)

    def test_tag_edge_accepts_dsv4_descriptive_field_names(self) -> None:
        output = sample_compile_output(self.event_id)
        output["tagEdges"] = [
            {
                "sourceTagName": "输入法",
                "targetTagName": "记忆清洗",
                "relationType": "depends_on",
                "confidence": 0.83,
                "evidenceEventIds": [self.event_id],
            }
        ]
        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        edge = next(item for item in plan["diffs"] if item["op"] == "upsert_tag_edge")
        self.assertEqual(edge["payload"]["src"], "输入法")
        self.assertEqual(edge["payload"]["dst"], "记忆清洗")
        self.assertEqual(edge["payload"]["edgeType"], "depends_on")
        self.assertAlmostEqual(edge["payload"]["weight"], 0.83)

    def test_memory_book_compile_rejects_hallucinated_source_event_id(self) -> None:
        output = sample_compile_output(self.event_id)
        output["memoryAtoms"][0]["sourceEventIds"] = [self.event_id + 999]
        bundle = {
            "recentEvents": [{"eventId": self.event_id, "text": "RAG 输入法多路召回方案"}],
            "existingMemoryBooks": [],
            "existingSemanticGroups": [],
            "existingSemanticTags": [],
        }

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        report = inspect_memory_book_plan(plan)

        self.assertFalse(report["ok"])
        self.assertTrue(any(item["code"] == "source_event_not_in_bundle" for item in report["errors"]))

    def test_memory_book_compile_allows_historical_owner_book_lineage(self) -> None:
        historical_event_id = self.event_id + 100
        output = {
            "topicBooks": [
                {
                    "bookId": "book:owner:default:topic:memory",
                    "title": "记忆整理",
                    "summary": "记忆整理的长期主题与约束。",
                    "sourceEventIds": [self.event_id],
                }
            ],
            "memoryAtoms": [],
        }
        bundle = {
            "schemaVersion": "rag-ime.owner-memory-source-bundle.v1",
            "recentEvents": [{"eventId": self.event_id, "text": "继续整理记忆"}],
            "existingMemoryBookIndex": [
                {
                    "bookId": "book:owner:default:topic:memory",
                    "bookKey": "owner-default-topic-memory",
                    "title": "记忆整理",
                    "summary": "记忆整理的长期主题与约束。",
                    "sourceEventIds": [historical_event_id],
                    "memoryAtomIds": [],
                    "status": "active",
                    "ownerKind": "user",
                    "ownerId": "default",
                    "project": "wisdom-weasel-rag-ime",
                }
            ],
        }

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="test",
            model="test",
            source_bundle=bundle,
        )

        report = inspect_memory_book_plan(plan)
        self.assertTrue(report["ok"], report)
        book = next(
            item["payload"]
            for item in plan["diffs"]
            if item["op"] == "upsert_memory_book"
        )
        self.assertEqual(
            set(book["sourceEventIds"]),
            {self.event_id, historical_event_id},
        )

    def test_memory_book_compile_backfills_source_ids_from_bundle(self) -> None:
        output = sample_compile_output(self.event_id)
        output["dailyBooks"][0]["sourceEventIds"] = []
        output["memoryAtoms"][0]["sourceEventIds"] = []
        output["tagEdges"][0]["evidenceEventIds"] = []
        output["phraseCandidates"][0]["sourceEventIds"] = []
        bundle = {
            "bundleHash": "sha256:test-empty-organizer",
            "cursor": {
                "fromEventId": 0,
                "toEventId": self.event_id + 2,
                "pendingEventCount": 3,
            },
            "recentEvents": [
                {
                    "eventId": self.event_id,
                    "createdAtMs": now_ms(),
                    "text": "RAG 输入法多路召回方案，需要用 DeepSeek 整理真实历史。",
                    "recentContext": "BM25 向量 TagMemo Time",
                    "tags": ["RAG", "输入法"],
                }
            ]
        }

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        report = inspect_memory_book_plan(plan)

        self.assertTrue(report["ok"])
        payloads = [item["payload"] for item in plan["diffs"]]
        self.assertTrue(all(self.event_id in item.get("sourceEventIds", item.get("evidenceEventIds", [])) for item in payloads))

    def test_memory_book_compile_drops_empty_edges_and_bad_phrases(self) -> None:
        output = sample_compile_output(self.event_id)
        output["dailyBooks"] = []
        output["tagEdges"] = [{}, {"src": "", "dst": "RAG"}, {"src": "RAG", "dst": ""}]
        output["phraseCandidates"] = [{}, {"text": ""}, {"text": "这是一个很长很长的历史原句不应该作为短候选"}]

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        report = inspect_memory_book_plan(plan)

        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["tagEdges"], 0)
        self.assertEqual(report["counts"]["phraseCandidates"], 0)

    def test_memory_book_compile_synthesizes_daily_book_when_model_omits_books(self) -> None:
        output = sample_compile_output(self.event_id)
        output["dailyBooks"] = []
        bundle = {
            "recentEvents": [
                {
                    "eventId": self.event_id,
                    "createdAtMs": now_ms(),
                    "text": "RAG 输入法多路召回方案，需要用 DeepSeek 整理真实历史。",
                    "recentContext": "Memory Book preview validate apply",
                    "tags": ["RAG", "输入法"],
                }
            ]
        }

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        report = inspect_memory_book_plan(plan)

        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["memoryBooks"], 1)
        self.assertIn("daily_book_synthesized_from_atoms", plan["metadata"]["warnings"])
        book = next(item["payload"] for item in plan["diffs"] if item["op"] == "upsert_memory_book")
        self.assertEqual(book["title"], f"本地记忆归档 {book['bookKey']}")
        self.assertEqual(book["queryExpansions"], [])
        self.assertNotIn("真实 Codex 历史", book["summary"])

    def test_memory_book_compile_accepts_stable_topic_books(self) -> None:
        output = sample_compile_output(self.event_id)
        output["dailyBooks"] = []
        output["topicBooks"] = [
            {
                "bookId": "book:topic:ai-input-method",
                "bookKey": "ai-input-method",
                "bookType": "topic",
                "title": "AI 输入法项目",
                "summary": "持续开发基于 Squirrel 的本地预测、RAG 记忆和显式知识生成链路。",
                "tags": ["输入法", "RAG", "Squirrel"],
                "queryExpansions": ["AI 输入法", "个人记忆输入法"],
                "sourceEventIds": [self.event_id],
                "confidence": 0.9,
                "qualityScore": 0.88,
            }
        ]

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        report = inspect_memory_book_plan(plan)

        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["memoryBooks"], 1)
        book = next(item["payload"] for item in plan["diffs"] if item["op"] == "upsert_memory_book")
        self.assertEqual(book["bookId"], "book:topic:ai-input-method")
        self.assertEqual(book["bookType"], "topic")
        self.assertEqual(book["bookKey"], "ai-input-method")

    def test_similar_topic_book_reuses_existing_id_even_when_model_invents_a_new_one(self) -> None:
        output = sample_compile_output(self.event_id)
        output["dailyBooks"] = []
        output["topicBooks"] = [
            {
                "bookId": "book:topic:new-invented-id",
                "bookKey": "new-invented-key",
                "bookType": "topic",
                "title": "AI 输入法项目优化",
                "summary": "继续优化 Squirrel 输入法、RAG 记忆与本地预测。",
                "tags": ["输入法", "RAG"],
                "sourceEventIds": [self.event_id],
            }
        ]
        bundle = {
            "recentEvents": [{"eventId": self.event_id, "text": "继续优化 AI 输入法项目"}],
            "existingMemoryBooks": [
                {
                    "bookId": "book:topic:ai-input-method",
                    "bookKey": "ai-input-method",
                    "bookType": "topic",
                    "title": "AI 输入法项目",
                    "summary": "Squirrel 输入法、RAG 记忆与本地预测的长期主题。",
                    "tags": ["Squirrel", "RAG"],
                    "sourceEventIds": [self.event_id + 100],
                    "memoryAtomIds": ["atom:existing"],
                    "status": "archived",
                }
            ],
        }

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        book = next(item["payload"] for item in plan["diffs"] if item["op"] == "upsert_memory_book")

        self.assertEqual(book["bookId"], "book:topic:ai-input-method")
        self.assertEqual(book["bookKey"], "ai-input-method")
        self.assertEqual(book["reusedExistingBookId"], "book:topic:ai-input-method")
        self.assertEqual(book["previousStatus"], "archived")
        self.assertEqual(set(book["sourceEventIds"]), {self.event_id, self.event_id + 100})
        self.assertIn("atom:existing", book["memoryAtomIds"])

    def test_owner_scoped_topic_identity_does_not_fall_back_to_legacy_umbrella_book(
        self,
    ) -> None:
        output = sample_compile_output(self.event_id)
        output["dailyBooks"] = []
        output["topicBooks"] = [
            {
                "bookId": "book:owner:abc:topic:rag-ime",
                "bookKey": "owner-abc-topic-rag-ime",
                "bookType": "topic",
                "title": "RAG IME 记忆架构",
                "summary": "Atom、Topic Book 和 Timeline 分为三条独立产物线。",
                "sourceEventIds": [self.event_id],
            }
        ]
        bundle = {
            "schemaVersion": "rag-ime.owner-memory-source-bundle.v1",
            "recentEvents": [
                {"eventId": self.event_id, "text": "整理 RAG IME 记忆架构"}
            ],
            "existingMemoryBooks": [
                {
                    "bookId": "book:owner:abc",
                    "bookKey": "owner-abc",
                    "bookType": "topic",
                    "title": "个人长期记忆",
                    "summary": "包含 RAG IME 的长期记忆架构。",
                    "sourceEventIds": [self.event_id + 100],
                    "memoryAtomIds": ["atom:legacy"],
                    "status": "active",
                }
            ],
        }

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        book = next(
            item["payload"]
            for item in plan["diffs"]
            if item["op"] == "upsert_memory_book"
        )

        self.assertEqual(book["bookId"], "book:owner:abc:topic:rag-ime")
        self.assertEqual(book["reusedExistingBookId"], "")
        self.assertNotIn("atom:legacy", book["memoryAtomIds"])

    def test_memory_book_compile_keeps_raw_history_pending_when_model_returns_empty(self) -> None:
        bundle = {
            "bundleHash": "sha256:test-empty-organizer",
            "cursor": {
                "fromEventId": 0,
                "toEventId": self.event_id + 2,
                "pendingEventCount": 3,
            },
            "recentEvents": [
                {
                    "eventId": self.event_id,
                    "createdAtMs": now_ms(),
                    "text": "周五上午十点和设计团队复盘新版支付流程。",
                    "recentContext": "会议安排",
                    "tags": ["团队复盘", "支付流程"],
                },
                {
                    "eventId": self.event_id + 1,
                    "createdAtMs": now_ms(),
                    "text": "周五上午十点和设计团队复盘新版支付流程。",
                    "recentContext": "重复记录应该合并",
                    "tags": ["团队复盘", "支付流程"],
                },
                {
                    "eventId": self.event_id + 2,
                    "createdAtMs": now_ms(),
                    "text": "采购清单需要补充燕麦和咖啡豆。",
                    "recentContext": "生活记录",
                    "tags": ["采购清单"],
                },
            ]
        }
        empty_output = {
            "schemaVersion": "rag-ime.memory-book-compile.v1",
            "dailyBooks": [],
            "memoryAtoms": [],
            "tagEdges": [],
            "phraseCandidates": [],
            "warnings": [],
        }

        plan = memory_book_plan_from_compile_output(
            empty_output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        report = inspect_memory_book_plan(plan)

        self.assertFalse(report["ok"])
        self.assertEqual(report["diffCount"], 0)
        self.assertEqual(report["counts"]["memoryBooks"], 0)
        self.assertEqual(report["counts"]["memoryAtoms"], 0)
        self.assertEqual(report["counts"]["phraseCandidates"], 0)
        self.assertIn("organizer_returned_no_governed_memory", plan["metadata"]["warnings"])
        self.assertTrue(any(item["code"] == "organizer_returned_no_governed_memory" for item in report["errors"]))
        serialized = json.dumps(plan, ensure_ascii=False)
        for injected_term in (
            "周五上午十点",
            "采购清单",
            "DeepSeek",
            "Squirrel",
            "RAG 输入法",
            "面试展示",
            "eval gate",
        ):
            self.assertNotIn(injected_term, serialized)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            with self.assertRaises(ValueError):
                apply_memory_book_plan(conn, plan)
            state = memory_compile_state(conn, project="wisdom-weasel-rag-ime")
        self.assertEqual(state["lastCompiledEventId"], 0)
        self.assertGreaterEqual(state["pendingEventCount"], 1)

    def test_memory_book_compile_rejects_long_surface_hint(self) -> None:
        output = sample_compile_output(self.event_id)
        output["memoryAtoms"][0]["surfaceHints"] = ["这是一个很长很长的历史原句不应该作为候选"]
        plan = memory_book_plan_from_compile_output(output, project="wisdom-weasel-rag-ime", provider="deepseek", model="deepseek-v4-flash")

        report = inspect_memory_book_plan(plan)

        self.assertFalse(report["ok"])
        self.assertTrue(any(item["code"] == "term_length_out_of_range" for item in report["errors"]))

    def test_memory_book_compile_rejects_secret_path_email(self) -> None:
        output = sample_compile_output(self.event_id)
        output["memoryAtoms"][0]["aliases"] = ["sk-secret-value"]
        plan = memory_book_plan_from_compile_output(output, project="wisdom-weasel-rag-ime", provider="deepseek", model="deepseek-v4-flash")

        report = inspect_memory_book_plan(plan)

        self.assertFalse(report["ok"])
        self.assertTrue(any(item["code"] == "sensitive_text_detected" for item in report["errors"]))

    def test_memory_book_compile_rejects_extended_sensitive_values(self) -> None:
        cases = {
            "phone": "13812345678",
            "identity": "11010519491231002X",
            "payment_card": "6222021234567890123",
            "ipv4": "192.168.1.10",
            "ipv6": "2001:db8::1",
            "url_query": "https://example.com/cb?access_token=supersecret&x=1",
            "private_key": "-----BEGIN PRIVATE KEY----- material -----END PRIVATE KEY-----",
            "linux_path": "/home/alice/private/note.txt",
            "windows_path": r"C:\Users\alice\private\note.txt",
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                output = sample_compile_output(self.event_id)
                output["memoryAtoms"][0]["aliases"] = [value]
                plan = memory_book_plan_from_compile_output(
                    output,
                    project="wisdom-weasel-rag-ime",
                    provider="deepseek",
                    model="deepseek-v4-flash",
                )

                report = inspect_memory_book_plan(plan)

                self.assertFalse(report["ok"])
                self.assertTrue(any(item["code"] == "sensitive_text_detected" for item in report["errors"]))

    def test_memory_book_compile_allows_token_and_api_key_concepts_without_values(
        self,
    ) -> None:
        output = sample_compile_output(self.event_id)
        output["dailyBooks"][0]["summary"] = (
            "需要解释 token 成本、上下文预算和 API key 管理策略，但不保存凭据值。"
        )

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        report = inspect_memory_book_plan(plan)

        self.assertTrue(report["ok"], report)

    def test_memory_book_apply_requires_apply_flag(self) -> None:
        plan_path = self._write_sample_plan()

        code, payload = self._run_cli_json("memory-book-apply", "--run", str(plan_path))

        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("--apply", payload["error"])

    def test_memory_book_apply_and_rollback(self) -> None:
        plan_path = self._write_sample_plan()
        plan = json.loads(plan_path.read_text(encoding="utf-8"))

        code, applied = self._run_cli_json("memory-book-apply", "--run", str(plan_path), "--apply")

        self.assertEqual(code, 0)
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["run"]["status"], "applied")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_books").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_atoms").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_aliases").fetchone()[0], 5)
            edge_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM memory_tag_edges e
                JOIN memory_tags s ON s.id = e.src_tag_id
                JOIN memory_tags d ON d.id = e.dst_tag_id
                WHERE s.tag = '输入法' AND d.tag = '大模型' AND e.edge_type = 'related'
                """
            ).fetchone()[0]
            self.assertEqual(edge_count, 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_items WHERE memory_id = 'phrase:多路召回'").fetchone()[0], 1)

        code, rolled_back = self._run_cli_json("memory-book-rollback", "--run-id", plan["runId"])

        self.assertEqual(code, 0)
        self.assertEqual(rolled_back["run"]["status"], "rolled_back")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_books").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_atoms").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_aliases").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_items WHERE memory_id = 'phrase:多路召回'").fetchone()[0], 0)

    def test_owner_scoped_atom_cannot_be_taken_over_by_another_role(self) -> None:
        compile_output = {
            "memoryAtoms": [
                {
                    "atomId": "atom:role-owned",
                    "kind": "preference",
                    "canonicalText": "只属于甲角色的长期偏好",
                    "aliases": ["甲角色偏好"],
                    "sourceEventIds": [self.event_id],
                    "confidence": 0.9,
                    "qualityScore": 0.9,
                }
            ]
        }
        first = memory_book_plan_from_compile_output(
            compile_output,
            project="wisdom-weasel-rag-ime",
            provider="test",
            model="test",
            owner_kind="agent",
            owner_id="role-a",
            run_kind="daily_curation",
        )
        second = memory_book_plan_from_compile_output(
            compile_output,
            project="wisdom-weasel-rag-ime",
            provider="test",
            model="test",
            owner_kind="agent",
            owner_id="role-b",
            run_kind="daily_curation",
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            apply_memory_book_plan(conn, first)
            with self.assertRaisesRegex(ValueError, "owned by another scope"):
                apply_memory_book_plan(conn, second)
            owner = conn.execute(
                "SELECT owner_kind, owner_id FROM memory_atoms WHERE id = 'atom:role-owned'"
            ).fetchone()

        self.assertEqual((owner["owner_kind"], owner["owner_id"]), ("agent", "role-a"))

    def test_atom_upsert_does_not_cascade_delete_existing_aliases(self) -> None:
        first_output = {
            "memoryAtoms": [
                {
                    "atomId": "atom:stable-upsert",
                    "kind": "project_fact",
                    "canonicalText": "第一次整理的稳定事实",
                    "aliases": ["旧称"],
                    "sourceEventIds": [self.event_id],
                }
            ]
        }
        second_output = {
            "memoryAtoms": [
                {
                    "atomId": "atom:stable-upsert",
                    "kind": "project_fact",
                    "canonicalText": "第二次整理后的稳定事实",
                    "aliases": ["新称"],
                    "sourceEventIds": [self.event_id],
                }
            ]
        }
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            apply_memory_book_plan(
                conn,
                memory_book_plan_from_compile_output(
                    first_output,
                    project="wisdom-weasel-rag-ime",
                    provider="test",
                    model="test",
                ),
            )
            second_plan = memory_book_plan_from_compile_output(
                second_output,
                project="wisdom-weasel-rag-ime",
                provider="test",
                model="test",
            )
            apply_memory_book_plan(conn, second_plan)
            aliases = {
                str(row["alias"])
                for row in conn.execute(
                    "SELECT alias FROM memory_aliases WHERE memory_atom_id = 'atom:stable-upsert'"
                ).fetchall()
            }
            rollback_memory_book_run(conn, run_id=str(second_plan["runId"]))
            restored = conn.execute(
                "SELECT canonical_text FROM memory_atoms WHERE id = 'atom:stable-upsert'"
            ).fetchone()
            restored_aliases = {
                str(row["alias"])
                for row in conn.execute(
                    "SELECT alias FROM memory_aliases WHERE memory_atom_id = 'atom:stable-upsert'"
                ).fetchall()
            }

        self.assertEqual(aliases, {"旧称", "新称"})
        self.assertEqual(restored["canonical_text"], "第一次整理的稳定事实")
        self.assertEqual(restored_aliases, {"旧称"})

    def test_stored_workbench_draft_can_be_applied_and_rolled_back(self) -> None:
        plan = memory_book_plan_from_compile_output(
            sample_compile_output(self.event_id),
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            draft = store_memory_book_plan(conn, plan)
            applied = apply_stored_memory_book_run(conn, run_id=plan["runId"])
            rolled_back = rollback_memory_book_run(conn, run_id=plan["runId"])

        self.assertEqual(draft["status"], "draft")
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(rolled_back["status"], "rolled_back")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_books").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_atoms").fetchone()[0], 0)

    def test_owner_draft_does_not_replay_legacy_compile_state_migrations(self) -> None:
        plan = memory_book_plan_from_compile_output(
            sample_compile_output(self.event_id),
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            run_kind="daily_curation",
        )

        with self.core._connect() as conn, patch(
            "rag_ime.memory_book_compiler.apply_database_migrations",
            side_effect=AssertionError("owner draft must use its existing schema"),
        ):
            draft = store_memory_book_plan(conn, plan)

        self.assertEqual(draft["status"], "draft")

    def test_owner_run_transitions_every_physical_source_beyond_legacy_cap(self) -> None:
        first_event_id = int(
            self.core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=10_000,
                    source="squirrel_assistant_remember",
                    committed_text="一条重建后的长输入",
                    privacy_disposition="allowed",
                    project="wisdom-weasel-rag-ime",
                )
            ).split(":", 1)[1]
        )
        run_id = "memory_book_owner_many_physical_sources"
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            first_source = conn.execute(
                """
                SELECT source_id, session_id
                FROM agent_memory_sources
                WHERE input_event_id = ?
                """,
                (first_event_id,),
            ).fetchone()
            assert first_source is not None
            source_ids = [str(first_source["source_id"])]
            for index in range(1, 300):
                event = conn.execute(
                    """
                    INSERT INTO input_events(
                        created_at_ms, source, committed_text, project
                    ) VALUES (?, 'squirrel_rime_commit_burst', ?, ?)
                    """,
                    (
                        10_000 + index,
                        f"片段{index}",
                        "wisdom-weasel-rag-ime",
                    ),
                )
                event_id = int(event.lastrowid)
                source_id = f"input-memory:bulk:{index}"
                source_ids.append(source_id)
                conn.execute(
                    """
                    INSERT INTO agent_memory_sources(
                        source_id, session_id, pi_entry_id, input_event_id,
                        source_role, canonical_text_sha256, created_at_ms
                    ) VALUES (?, ?, ?, ?, 'user', ?, ?)
                    """,
                    (
                        source_id,
                        str(first_source["session_id"]),
                        f"input-event:bulk:{index}",
                        event_id,
                        "a" * 64,
                        10_000 + index,
                    ),
                )
            conn.execute(
                """
                UPDATE agent_memory_sources
                SET disposition = 'remember',
                    disposition_reason = 'durable_user_intent',
                    curation_run_id = ?
                WHERE source_id IN (
                    SELECT CAST(value AS TEXT) FROM json_each(?)
                )
                """,
                (run_id, json.dumps(source_ids)),
            )
            plan = memory_book_plan_from_compile_output(
                {
                    "memoryAtoms": [
                        {
                            "canonicalText": "重建后的长输入仍是一个可审阅事实",
                            "sourceEventIds": [first_event_id],
                        }
                    ]
                },
                project="wisdom-weasel-rag-ime",
                provider="fixture",
                model="fixture",
                owner_kind="user",
                owner_id="default",
                run_kind="daily_curation",
            )
            plan["runId"] = run_id
            plan["metadata"].update(
                {
                    "ownerKind": "user",
                    "ownerId": "default",
                    "runKind": "daily_curation",
                    "sourceIds": source_ids,
                }
            )
            store_memory_book_plan(conn, plan)
            apply_stored_memory_book_run(conn, run_id=run_id)
            consolidated = conn.execute(
                """
                SELECT COUNT(*)
                FROM agent_memory_sources
                WHERE curation_run_id = ? AND disposition = 'consolidated'
                """,
                (run_id,),
            ).fetchone()[0]
            rollback_memory_book_run(conn, run_id=run_id)
            remembered = conn.execute(
                """
                SELECT COUNT(*)
                FROM agent_memory_sources
                WHERE curation_run_id = ? AND disposition = 'remember'
                """,
                (run_id,),
            ).fetchone()[0]

        self.assertEqual(consolidated, 300)
        self.assertEqual(remembered, 300)

    def test_memory_book_runs_must_roll_back_in_reverse_apply_order(self) -> None:
        first = memory_book_plan_from_compile_output(
            sample_compile_output(self.event_id),
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        first["runId"] = "memory_book_first_apply"
        second = json.loads(json.dumps(first))
        second["runId"] = "memory_book_second_apply"
        second["summary"] = "later memory state"

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            apply_memory_book_plan(conn, first)
            apply_memory_book_plan(conn, second)
            newer = find_newer_applied_memory_book_run(conn, run_id=first["runId"])
            with self.assertRaisesRegex(ValueError, "newer applied run"):
                rollback_memory_book_run(conn, run_id=first["runId"])
            rollback_memory_book_run(conn, run_id=second["runId"])
            rolled_back = rollback_memory_book_run(conn, run_id=first["runId"])

        self.assertEqual(newer["runId"], second["runId"])
        self.assertEqual(rolled_back["status"], "rolled_back")

    def test_stored_workbench_draft_supports_review_edit_and_exclusion(self) -> None:
        plan = memory_book_plan_from_compile_output(
            sample_compile_output(self.event_id),
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            draft = store_memory_book_plan(conn, plan)
            book = next(item for item in draft["diffs"] if item["op"] == "upsert_memory_book")
            atom = next(item for item in draft["diffs"] if item["op"] == "upsert_memory_atom")
            edited_payload = dict(book["payload"])
            edited_payload["title"] = "输入法记忆与召回"
            reviewed = update_stored_memory_book_diff(
                conn,
                run_id=plan["runId"],
                diff_id=book["diffId"],
                payload=edited_payload,
                selected=True,
            )
            excluded = update_stored_memory_book_diff(
                conn,
                run_id=plan["runId"],
                diff_id=atom["diffId"],
                selected=False,
            )
            applied = apply_stored_memory_book_run(conn, run_id=plan["runId"])

        reviewed_book = next(item for item in reviewed["diffs"] if item["diffId"] == book["diffId"])
        excluded_atom = next(item for item in excluded["diffs"] if item["diffId"] == atom["diffId"])
        self.assertEqual(reviewed_book["payload"]["title"], "输入法记忆与召回")
        self.assertEqual(reviewed_book["status"], "approved")
        self.assertEqual(excluded_atom["status"], "rejected")
        self.assertEqual(next(item for item in applied["diffs"] if item["diffId"] == atom["diffId"])["status"], "rejected")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT title FROM memory_books LIMIT 1").fetchone()
            self.assertEqual(row["title"], "输入法记忆与召回")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_atoms").fetchone()[0], 0)

    def test_dsv4_semantic_groups_tags_and_members_are_applied_and_rolled_back(self) -> None:
        output = sample_compile_output(self.event_id)
        output["semanticGroups"] = [
            {
                "groupId": "group:input-method",
                "title": "输入法",
                "description": "输入法、候选预测、语音与个人知识召回相关的长期主题。",
                "aliases": ["RAG 输入法"],
                "tags": ["输入法"],
                "sourceEventIds": [self.event_id],
                "confidence": 0.92,
                "qualityScore": 0.9,
            }
        ]
        output["semanticTags"] = [
            {
                "name": "混合召回",
                "description": "BM25、向量、标签与时间信号共同参与的检索策略。",
                "aliases": ["Hybrid RAG"],
                "semanticGroupIds": ["group:input-method"],
                "sourceEventIds": [self.event_id],
                "confidence": 0.88,
                "qualityScore": 0.86,
            }
        ]
        output["memoryAtoms"][0]["semanticGroupIds"] = ["group:input-method"]
        output["phraseCandidates"][0]["semanticGroupIds"] = ["group:input-method"]
        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        report = inspect_memory_book_plan(plan)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["counts"]["semanticGroups"], 1)
        self.assertEqual(report["counts"]["semanticTags"], 1)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            apply_memory_book_plan(conn, plan)
            self.assertEqual(
                conn.execute("SELECT title FROM memory_semantic_groups WHERE group_id = 'group:input-method'").fetchone()[0],
                "输入法",
            )
            tag = conn.execute("SELECT description, source FROM memory_tags WHERE tag = '混合召回'").fetchone()
            self.assertEqual(tag["source"], "dsv4")
            self.assertIn("BM25", tag["description"])
            members = conn.execute(
                "SELECT member_type FROM memory_semantic_group_members WHERE group_id = 'group:input-method' ORDER BY member_type"
            ).fetchall()
            self.assertEqual({row[0] for row in members}, {"atom", "book", "phrase", "tag"})
            rollback_memory_book_run(conn, run_id=plan["runId"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_semantic_groups").fetchone()[0], 0)

    def test_group_membership_is_inferred_from_shared_source_evidence(self) -> None:
        output = sample_compile_output(self.event_id)
        output["semanticGroups"] = [
            {
                "groupId": "group:input-method",
                "title": "输入法",
                "description": "输入法、候选预测、语音与个人知识召回相关的长期主题。",
                "sourceEventIds": [self.event_id],
            }
        ]
        output["semanticTags"] = [
            {
                "name": "混合召回",
                "description": "BM25 与向量共同参与的检索策略。",
                "sourceEventIds": [self.event_id],
            }
        ]
        bundle = {
            "recentEvents": [
                {
                    "eventId": self.event_id,
                    "sourceEventIds": [self.event_id],
                    "text": "RAG 输入法多路召回方案",
                }
            ],
            "existingSemanticGroups": [],
        }

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )

        for op in ("upsert_semantic_tag", "upsert_memory_book", "upsert_memory_atom", "add_phrase_candidate"):
            payload = next(item["payload"] for item in plan["diffs"] if item["op"] == op)
            self.assertEqual(payload["semanticGroupIds"], ["group:input-method"])

    def test_source_bundle_exposes_existing_tag_graph_to_dsv4(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            first = int(
                conn.execute(
                    "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                    "description, source, status, metadata_json) VALUES ('BM25', 'bm25', 'tech', 0.9, 1, 1, "
                    "'词法召回', 'dsv4', 'active', '{}')"
                ).lastrowid
            )
            second = int(
                conn.execute(
                    "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                    "description, source, status, metadata_json) VALUES ('混合召回', '混合召回', 'concept', 0.85, 1, 1, "
                    "'多路融合', 'dsv4', 'active', '{}')"
                ).lastrowid
            )
            conn.execute(
                "INSERT INTO memory_tag_edges(src_tag_id, dst_tag_id, edge_type, weight, direction_bias, evidence_count, "
                "updated_at_ms, metadata_json) VALUES (?, ?, 'part_of', 0.88, 0, 3, 1, '{}')",
                (first, second),
            )
            bundle = build_memory_book_source_bundle(conn, project="wisdom-weasel-rag-ime")

        self.assertEqual(len(bundle["existingTagEdges"]), 1)
        self.assertEqual(bundle["existingTagEdges"][0]["src"], "BM25")
        self.assertEqual(bundle["existingTagEdges"][0]["dst"], "混合召回")
        self.assertEqual(bundle["existingTagEdges"][0]["srcTagId"], first)
        self.assertEqual(bundle["existingTagEdges"][0]["dstTagId"], second)
        bm25 = next(item for item in bundle["existingSemanticTags"] if item["name"] == "BM25")
        self.assertEqual(bm25["degree"], 1)

    def test_global_catalog_source_bundle_is_uncapped_and_digest_sealed(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.executemany(
                "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                "description, source, status, metadata_json) VALUES (?, ?, 'concept', 0.8, 1, 1, "
                "'catalog test tag', 'dsv4', 'active', '{}')",
                [
                    (f"catalog-tag-{index}", f"catalog-tag-{index}")
                    for index in range(170)
                ],
            )
            conn.execute(
                """
                INSERT INTO memory_atoms(
                    id, kind, text, canonical_text, source_event_ids_json,
                    scope_project, language, confidence, quality_score,
                    privacy_level, owner_kind, owner_id, knowledge_domain,
                    scope_kind, scope_id, visibility, authorization_revision,
                    binding_id, scope_mode, status, created_at_ms, updated_at_ms
                ) VALUES (
                    'atom:second-project', 'project_fact', '第二项目事实',
                    '第二项目事实', ?, 'other-project', 'zh', 0.9, 0.9,
                    'local', 'user', 'default', 'legacy', 'project',
                    'other-project', 'private', 'auth:catalog',
                    'binding:catalog-atom', 'authoritative', 'active', 1, 1
                )
                """,
                (json.dumps([self.event_id]),),
            )
            conn.execute(
                """
                INSERT INTO memory_books(
                    book_id, book_type, book_key, title, summary, project,
                    status, created_at_ms, updated_at_ms
                ) VALUES (
                    'book:second-project', 'topic', 'second-project',
                    '第二项目书', '第二项目摘要', 'other-project',
                    'active', 1, 1
                )
                """
            )
            conn.execute(
                """
                INSERT INTO memory_semantic_groups(
                    group_id, title, description, project, status,
                    created_at_ms, updated_at_ms
                ) VALUES (
                    'group:second-project', '第二项目组', '第二项目主题',
                    'other-project', 'active', 1, 1
                )
                """
            )
            curated_id = int(
                conn.execute(
                    "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                    "description, source, status, metadata_json) VALUES ('curated-import-tag', 'curated-import-tag', "
                    "'concept', 0.95, 1, 1, 'curated catalog tag', 'curated_import', 'active', '{}')"
                ).lastrowid
            )
            catalog_tag_id = int(
                conn.execute(
                    "SELECT id FROM memory_tags WHERE tag = 'catalog-tag-0'"
                ).fetchone()[0]
            )
            conn.execute(
                "INSERT INTO memory_tag_edges(src_tag_id, dst_tag_id, edge_type, weight, direction_bias, "
                "evidence_count, updated_at_ms, metadata_json) VALUES (?, ?, 'imported_relation', 0.91, 0, 2, 1, '{}')",
                (curated_id, catalog_tag_id),
            )
            incremental = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
            )
            catalog = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                curation_scope="global",
                catalog_only=True,
            )

        self.assertEqual(len(incremental["existingSemanticTags"]), 160)
        self.assertFalse(any(item["atomId"] == "atom:second-project" for item in incremental["existingMemoryAtoms"]))
        self.assertFalse(any(item["bookId"] == "book:second-project" for item in incremental["existingMemoryBooks"]))
        self.assertFalse(any(item["groupId"] == "group:second-project" for item in incremental["existingSemanticGroups"]))
        self.assertNotIn(
            "curated-import-tag",
            {item["name"] for item in incremental["existingSemanticTags"]},
        )
        catalog_atom = next(
            item
            for item in catalog["existingMemoryAtoms"]
            if item["atomId"] == "atom:second-project"
        )
        self.assertEqual(catalog_atom["project"], "other-project")
        self.assertEqual(catalog_atom["ownerKind"], "user")
        self.assertEqual(catalog_atom["ownerId"], "default")
        self.assertEqual(catalog_atom["knowledgeDomain"], "legacy")
        self.assertEqual(catalog_atom["scopeKind"], "project")
        self.assertEqual(catalog_atom["scopeId"], "other-project")
        self.assertEqual(catalog_atom["visibility"], "private")
        self.assertEqual(catalog_atom["authorizationRevision"], "auth:catalog")
        self.assertEqual(catalog_atom["bindingId"], "binding:catalog-atom")
        self.assertEqual(catalog_atom["scopeMode"], "authoritative")
        self.assertEqual(catalog_atom["privacyLevel"], "local")
        self.assertTrue(any(item["bookId"] == "book:second-project" and item["project"] == "other-project" for item in catalog["existingMemoryBooks"]))
        self.assertTrue(any(item["groupId"] == "group:second-project" and item["project"] == "other-project" for item in catalog["existingSemanticGroups"]))
        self.assertIn(
            "curated-import-tag",
            {item["name"] for item in catalog["existingSemanticTags"]},
        )
        self.assertTrue(
            any(
                item["src"] == "curated-import-tag"
                and item["dst"] == "catalog-tag-0"
                for item in catalog["existingTagEdges"]
            )
        )
        self.assertFalse(
            any(item["src"] == "curated-import-tag" for item in incremental["existingTagEdges"])
        )
        self.assertGreaterEqual(len(catalog["existingSemanticTags"]), 170)
        self.assertEqual(catalog["recentEvents"], [])
        self.assertEqual(catalog["curationScope"], "global")
        self.assertTrue(catalog["catalogAudit"])
        self.assertEqual(catalog["evidenceOrder"], "catalog_only")
        self.assertTrue(catalog["catalogComplete"])
        self.assertFalse(any(catalog["catalogTruncated"].values()))
        self.assertTrue(catalog["catalogDigest"])

    def test_catalog_maintenance_compiles_governed_topic_book_merge(self) -> None:
        source_bundle = {
            "project": "wisdom-weasel-rag-ime",
            "curationScope": "global",
            "catalogAudit": True,
            "catalogComplete": True,
            "catalogTruncated": {},
            "catalogDigest": "sha256:" + "0" * 64,
            "existingMemoryBooks": [
                {
                    "bookId": "book:owner:550ccb5c952c95ca:topic:0858f8e09639d58f",
                    "bookKey": "owner-topic-rag",
                    "bookType": "topic",
                    "title": "输入法架构",
                    "summary": "RAG 输入法的长期架构主题。",
                    "tags": ["输入法", "RAG"],
                    "memoryAtomIds": ["atom:target"],
                    "sourceEventIds": [101],
                    "ownerKind": "user",
                    "ownerId": "default",
                    "project": "wisdom-weasel-rag-ime",
                    "status": "active",
                },
                {
                    "bookId": "book:owner:550ccb5c952c95ca:topic:058c7f41df478bd3",
                    "bookKey": "owner-topic-squirrel",
                    "bookType": "topic",
                    "title": "Squirrel 预测",
                    "summary": "输入法预测与 RAG 记忆属于同一长期主题。",
                    "tags": ["输入法", "RAG"],
                    "memoryAtomIds": ["atom:source"],
                    "sourceEventIds": [102],
                    "ownerKind": "user",
                    "ownerId": "default",
                    "project": "wisdom-weasel-rag-ime",
                    "status": "active",
                },
            ],
            "existingMemoryAtoms": [],
            "existingSemanticGroups": [],
            "existingSemanticTags": [],
            "existingTagEdges": [],
        }
        compiled = curation_decisions_to_compile_output(
            {
                "bookMerges": [
                    {
                        "sourceRef": "B2",
                        "targetRef": "B1",
                        "reason": "不同标题但成员和语义都证明是同一输入法 RAG 主题",
                        "confidence": 0.94,
                    }
                ]
            },
            source_bundle=source_bundle,
            project="wisdom-weasel-rag-ime",
        )
        self.assertEqual(len(compiled["bookMerges"]), 1)
        plan = memory_book_plan_from_compile_output(
            compiled,
            project="wisdom-weasel-rag-ime",
            provider="test",
            model="test",
            source_bundle=source_bundle,
            run_id="memory_book_topic_merge_compile",
        )
        merges = [item for item in plan["diffs"] if item["op"] == "merge_memory_books"]
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]["payload"]["targetBookId"], source_bundle["existingMemoryBooks"][0]["bookId"])
        self.assertEqual(merges[0]["payload"]["sourceBookIds"], [source_bundle["existingMemoryBooks"][1]["bookId"]])
        self.assertTrue(inspect_memory_book_plan(plan)["ok"])

    def test_catalog_skips_member_scope_mismatch_but_keeps_independent_merges(self) -> None:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            for name in ("target", "source", "bad-target", "bad-source"):
                conn.execute(
                    "INSERT INTO memory_atoms(id, kind, text, canonical_text, scope_project, "
                    "owner_kind, owner_id, status, created_at_ms, updated_at_ms) "
                    "VALUES (?, 'project_fact', ?, ?, ?, 'user', 'default', 'active', 1, 1)",
                    (f"atom:{name}", f"事实 {name}", f"事实 {name}",
                     "foreign-project" if name == "bad-source" else "wisdom-weasel-rag-ime"),
                )
                conn.execute(
                    "INSERT INTO memory_books(book_id, book_type, book_key, title, project, "
                    "owner_kind, owner_id, memory_atom_ids_json, status, created_at_ms, updated_at_ms) "
                    "VALUES (?, 'topic', ?, ?, 'wisdom-weasel-rag-ime', 'user', 'default', ?, 'active', 1, 1)",
                    (f"book:{name}", name, f"主题 {name}", json.dumps([f"atom:{name}"])),
                )
            bundle = build_memory_book_source_bundle(
                conn, project="wisdom-weasel-rag-ime", curation_scope="global", catalog_only=True,
            )
            output = {"bookMerges": [
                {"targetBookId": f"book:{prefix}target", "sourceBookIds": [f"book:{prefix}source"],
                 "reason": "相关子问题属于同一长期主题", "confidence": 0.95}
                for prefix in ("", "bad-")
            ]}
            plan = memory_book_plan_from_compile_output(
                output, project="wisdom-weasel-rag-ime", provider="test", model="test", source_bundle=bundle,
            )
            validation = inspect_memory_book_plan(plan)
            self.assertTrue(validation["ok"], validation)
            self.assertEqual([diff["targetId"] for diff in plan["diffs"]], ["book:target"])
            self.assertTrue(any("book_merge_member_scope_mismatch" in warning for warning in plan["metadata"]["warnings"]))
            atoms_before = [tuple(row) for row in conn.execute("SELECT * FROM memory_atoms ORDER BY id")]
            applied = apply_memory_book_plan(conn, plan)
            self.assertEqual(applied["status"], "applied")
            target = conn.execute("SELECT memory_atom_ids_json FROM memory_books WHERE book_id='book:target'").fetchone()
            self.assertEqual(set(json.loads(target[0])), {"atom:source", "atom:target"})
            self.assertEqual(
                [row[0] for row in conn.execute("SELECT status FROM memory_books WHERE book_id LIKE 'book:bad-%' ORDER BY book_id")],
                ["active", "active"],
            )
            self.assertEqual(atoms_before, [tuple(row) for row in conn.execute("SELECT * FROM memory_atoms ORDER BY id")])

    def test_topic_book_merge_apply_preserves_members_redirects_and_rolls_back(self) -> None:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            conn.execute(
                """
                INSERT INTO memory_atoms(
                    id, kind, text, canonical_text, scope_project, status,
                    created_at_ms, updated_at_ms, claim_key, lineage_id,
                    claim_state, valid_from_ms, owner_kind, owner_id,
                    privacy_level
                ) VALUES
                    ('atom:target', 'project_fact', '输入法使用 RAG', '输入法使用 RAG',
                     'wisdom-weasel-rag-ime', 'active', 1, 1, 'claim:target',
                     'lineage:target', 'current', 1, 'user', 'default', 'local'),
                    ('atom:source', 'project_fact', 'Squirrel 预测复用 RAG', 'Squirrel 预测复用 RAG',
                     'wisdom-weasel-rag-ime', 'active', 2, 2, 'claim:source',
                     'lineage:source', 'current', 2, 'user', 'default', 'local')
                """
            )
            conn.executemany(
                """
                INSERT INTO memory_books(
                    book_id, book_type, book_key, title, summary, normalized_text,
                    project, tags_json, surface_hints_json, query_expansions_json,
                    source_event_ids_json, memory_atom_ids_json, owner_kind, owner_id,
                    status, confidence, quality_score, created_at_ms, updated_at_ms,
                    metadata_json
                ) VALUES (?, 'topic', ?, ?, ?, ?, ?, ?, '[]', '[]', ?, ?,
                          'user', 'default', 'active', 0.9, 0.9, 1, 1, '{}')
                """,
                [
                    (
                        "book:target",
                        "topic-target",
                        "输入法架构",
                        "旧摘要不应成为合并后的权威摘要",
                        "输入法架构 旧摘要",
                        "wisdom-weasel-rag-ime",
                        json.dumps(["目标标签"], ensure_ascii=False),
                        json.dumps([self.event_id]),
                        json.dumps(["atom:target"]),
                    ),
                    (
                        "book:source",
                        "topic-source",
                        "Squirrel 预测",
                        "来源摘要",
                        "Squirrel 预测 来源摘要",
                        "wisdom-weasel-rag-ime",
                        json.dumps(["来源标签"], ensure_ascii=False),
                        json.dumps([self.event_id]),
                        json.dumps(["atom:source"]),
                    ),
                ],
            )
            # A stable target ID remains valid even when it is newer than the
            # source; createdAtMs is only a model-side default preference.
            conn.execute(
                "UPDATE memory_books SET created_at_ms = 2, updated_at_ms = 2 "
                "WHERE book_id = 'book:target'"
            )
            conn.execute(
                "UPDATE memory_books SET created_at_ms = 1, updated_at_ms = 1 "
                "WHERE book_id = 'book:source'"
            )
            bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                curation_scope="global",
                catalog_only=True,
            )
            model_bundle = build_memory_curation_model_bundle(bundle)
            refs = {item["bookId"]: item["ref"] for item in model_bundle["existingBooks"]}
            plan = memory_book_plan_from_compile_output(
                {
                    "bookMerges": [
                        {
                            "sourceRef": refs["book:source"],
                            "targetRef": refs["book:target"],
                            "reason": "同一长期主题",
                            "confidence": 0.95,
                        }
                    ]
                },
                project="wisdom-weasel-rag-ime",
                provider="test",
                model="test",
                source_bundle=bundle,
                run_id="memory_book_topic_merge_apply",
            )
            self.assertTrue(inspect_memory_book_plan(plan)["ok"], inspect_memory_book_plan(plan))
            applied = apply_memory_book_plan(conn, plan)
            self.assertEqual(applied["status"], "applied")
            target = conn.execute(
                "SELECT summary, memory_atom_ids_json, tags_json, metadata_json FROM memory_books WHERE book_id = 'book:target'"
            ).fetchone()
            source = conn.execute(
                "SELECT status, metadata_json FROM memory_books WHERE book_id = 'book:source'"
            ).fetchone()
            self.assertEqual(set(json.loads(target["memory_atom_ids_json"])), {"atom:target", "atom:source"})
            self.assertIn("输入法使用 RAG", target["summary"])
            self.assertIn("Squirrel 预测复用 RAG", target["summary"])
            self.assertEqual(set(json.loads(target["tags_json"])), {"目标标签", "来源标签"})
            self.assertEqual(source["status"], "superseded")
            self.assertEqual(json.loads(source["metadata_json"])["supersededByBookId"], "book:target")
            rollback_memory_book_run(conn, run_id=plan["runId"])
            target_after = conn.execute(
                "SELECT summary, memory_atom_ids_json, tags_json FROM memory_books WHERE book_id = 'book:target'"
            ).fetchone()
            source_after = conn.execute(
                "SELECT status, metadata_json FROM memory_books WHERE book_id = 'book:source'"
            ).fetchone()
            self.assertEqual(target_after["summary"], "旧摘要不应成为合并后的权威摘要")
            self.assertEqual(json.loads(target_after["memory_atom_ids_json"]), ["atom:target"])
            self.assertEqual(source_after["status"], "active")
            self.assertEqual(json.loads(source_after["metadata_json"]), {})

    def test_global_catalog_apply_rejects_a_changed_frozen_digest(self) -> None:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                curation_scope="global",
                catalog_only=True,
            )
            plan = memory_book_plan_from_compile_output(
                {
                    "schemaVersion": "rag-ime.memory-book-compile.v1",
                    "curationArchitecture": "atom-first-v2",
                    "semanticGroups": [],
                    "semanticTags": [],
                    "tagMerges": [],
                    "tagEdges": [],
                    "memoryAtoms": [],
                    "supersedes": [],
                },
                project="wisdom-weasel-rag-ime",
                provider="test",
                model="test",
                source_bundle=bundle,
            )
            self.assertEqual(
                plan["metadata"]["runKind"],
                "catalog_consolidation",
            )
            conn.execute(
                "INSERT INTO memory_tags("
                "tag, normalized_tag, tag_type, quality_score, "
                "created_at_ms, updated_at_ms, description, source, status, metadata_json"
                ") VALUES ('changed-after-freeze', 'changed-after-freeze', "
                "'concept', 0.8, 1, 1, 'changed', 'user', 'active', '{}')"
            )
            conn.commit()

            with self.assertRaisesRegex(ValueError, "catalog digest changed"):
                apply_memory_book_plan(conn, plan)
            persisted = conn.execute(
                "SELECT COUNT(*) FROM memory_cleanup_runs WHERE run_id = ?",
                (plan["runId"],),
            ).fetchone()[0]

        self.assertEqual(persisted, 0)
    def test_global_catalog_store_rejects_a_changed_digest_for_no_diff_plan(self) -> None:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                curation_scope="global",
                catalog_only=True,
            )
            plan = memory_book_plan_from_compile_output(
                {
                    "schemaVersion": "rag-ime.memory-book-compile.v1",
                    "curationArchitecture": "atom-first-v2",
                    "curationOutcome": "no_changes",
                    "semanticGroups": [],
                    "semanticTags": [],
                    "tagMerges": [],
                    "tagEdges": [],
                    "memoryAtoms": [],
                    "supersedes": [],
                },
                project="wisdom-weasel-rag-ime",
                provider="test",
                model="test",
                source_bundle=bundle,
                run_id="memory_book_catalog_no_diff_stale",
            )
            conn.execute(
                "INSERT INTO memory_tags("
                "tag, normalized_tag, tag_type, quality_score, "
                "created_at_ms, updated_at_ms, description, source, status, metadata_json"
                ") VALUES ('changed-before-no-diff-store', 'changed-before-no-diff-store', "
                "'concept', 0.8, 1, 1, 'changed', 'user', 'active', '{}')"
            )
            conn.commit()
            with self.assertRaisesRegex(ValueError, "catalog digest changed"):
                store_memory_book_plan(conn, plan)
            persisted = conn.execute(
                "SELECT COUNT(*) FROM memory_cleanup_runs WHERE run_id = ?",
                (plan["runId"],),
            ).fetchone()[0]
        self.assertEqual(persisted, 0)


    def test_global_tag_merge_chain_targets_terminal_canonical_tag(self) -> None:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            conn.row_factory = sqlite3.Row
            tag_ids: dict[str, int] = {}
            for name in ("A", "B", "C"):
                tag_ids[name] = int(
                    conn.execute(
                        "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                        "description, source, status, metadata_json) VALUES (?, ?, 'concept', ?, 1, 1, ?, 'dsv4', 'active', '{}')",
                        (name, name.lower(), 1.0 - 0.1 * ("A", "B", "C").index(name), f"{name} tag"),
                    ).lastrowid
                )
            conn.execute(
                "INSERT INTO memory_tag_profiles(tag_id, color_token, aliases_json, updated_at_ms) VALUES (?, 'blue', ?, 1)",
                (tag_ids["A"], json.dumps(["B"], ensure_ascii=False)),
            )
            conn.execute(
                "INSERT INTO memory_tag_profiles(tag_id, color_token, aliases_json, updated_at_ms) VALUES (?, 'blue', ?, 1)",
                (tag_ids["B"], json.dumps(["C"], ensure_ascii=False)),
            )
            bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                curation_scope="global",
                catalog_only=True,
            )
            plan = memory_book_plan_from_compile_output(
                {
                    "schemaVersion": "rag-ime.memory-book-compile.v1",
                    "tagMerges": [
                        {
                            "source": "C",
                            "target": "B",
                            "sourceTagId": tag_ids["C"],
                            "targetTagId": tag_ids["B"],
                            "reason": "链式别名",
                            "evidenceEventIds": [self.event_id],
                        },
                        {
                            "source": "B",
                            "target": "A",
                            "sourceTagId": tag_ids["B"],
                            "targetTagId": tag_ids["A"],
                            "reason": "链式别名",
                            "evidenceEventIds": [self.event_id],
                        },
                    ],
                },
                project="wisdom-weasel-rag-ime",
                provider="test",
                model="test",
                source_bundle=bundle,
            )
            merges = {
                str(item["payload"]["source"]): item["payload"]
                for item in plan["diffs"]
                if item["op"] == "merge_semantic_tag"
            }
            self.assertEqual(merges["C"]["target"], "A")
            self.assertEqual(merges["C"]["targetTagId"], tag_ids["A"])
            self.assertEqual(merges["B"]["target"], "A")
            self.assertTrue(inspect_memory_book_plan(plan)["ok"])
            applied = apply_memory_book_plan(conn, plan)
            self.assertTrue(applied["sealedCatalogDigest"])
            self.assertIsNotNone(
                conn.execute("SELECT id FROM memory_tags WHERE id = ?", (tag_ids["A"],)).fetchone()
            )
            self.assertIsNone(
                conn.execute("SELECT id FROM memory_tags WHERE id = ?", (tag_ids["B"],)).fetchone()
            )
            self.assertIsNone(
                conn.execute("SELECT id FROM memory_tags WHERE id = ?", (tag_ids["C"],)).fetchone()
            )
            rollback_rows = conn.execute(
                "SELECT rollback_json FROM memory_cleanup_diffs WHERE run_id = ? ORDER BY id",
                (plan["runId"],),
            ).fetchall()
            self.assertTrue(rollback_rows)
            self.assertTrue(all(not json.loads(row["rollback_json"]).get("noOp") for row in rollback_rows))

    def test_global_atom_merge_preserves_complete_relation_payloads(self) -> None:
        initial = {
            "schemaVersion": "rag-ime.memory-book-compile.v1",
            "semanticGroups": [
                {
                    "groupId": group_id,
                    "title": group_id,
                    "description": group_id,
                    "sourceEventIds": [self.event_id],
                }
                for group_id in ("group:target", "group:source")
            ],
            "memoryAtoms": [
                {
                    "atomId": "atom:merge-target",
                    "kind": "project_fact",
                    "claimKey": "claim:exact-merge",
                    "lineageId": "lineage:exact-merge",
                    "claimState": "current",
                    "canonicalText": "Exact catalog duplicate",
                    "aliases": ["target-alias"],
                    "surfaceHints": ["target-surface"],
                    "queryExpansions": ["target-query"],
                    "sourceMemoryIds": ["memory:target"],
                    "sourceEventIds": [self.event_id],
                    "tags": ["target-tag"],
                    "semanticGroupIds": ["group:target"],
                },
                {
                    "atomId": "atom:merge-source",
                    "kind": "project_fact",
                    "claimKey": "claim:exact-merge",
                    "lineageId": "lineage:exact-merge",
                    "claimState": "current",
                    "canonicalText": "Exact catalog duplicate",
                    "aliases": ["source-alias"],
                    "surfaceHints": ["source-surface"],
                    "queryExpansions": ["source-query"],
                    "sourceMemoryIds": ["memory:source"],
                    "sourceEventIds": [self.event_id],
                    "tags": ["source-tag"],
                    "semanticGroupIds": ["group:source"],
                },
            ],
        }
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            apply_memory_book_plan(
                conn,
                memory_book_plan_from_compile_output(
                    initial,
                    project="wisdom-weasel-rag-ime",
                    provider="test",
                    model="test",
                    run_id="memory_book_relation_seed",
                ),
            )
            conn.execute(
                "UPDATE memory_atoms "
                "SET status = 'superseded', claim_state = 'superseded', "
                "valid_from_ms = 1, valid_to_ms = 123456789, supersedes_id = NULL "
                "WHERE id IN ('atom:merge-target', 'atom:merge-source')"
            )
            conn.execute(
                "DELETE FROM memory_supersessions "
                "WHERE old_memory_id IN ('atom:merge-target', 'atom:merge-source') "
                "OR new_memory_id IN ('atom:merge-target', 'atom:merge-source')"
            )
            conn.commit()
            bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
                curation_scope="global",
                catalog_only=True,
            )
            model_bundle = build_memory_curation_model_bundle(bundle)
            refs = {
                str(item["atomId"]): str(item["ref"])
                for item in model_bundle["existingAtoms"]
            }
            compiled = curation_decisions_to_compile_output(
                {
                    "merge": [
                        [
                            refs["atom:merge-source"],
                            refs["atom:merge-target"],
                        ]
                    ]
                },
                source_bundle=bundle,
                project="wisdom-weasel-rag-ime",
            )
            plan = memory_book_plan_from_compile_output(
                compiled,
                project="wisdom-weasel-rag-ime",
                provider="test",
                model="test",
                source_bundle=bundle,
                run_id="memory_book_relation_merge",
            )
            validation = inspect_memory_book_plan(plan)
            self.assertTrue(validation["ok"], validation)
            apply_memory_book_plan(conn, plan)

            target = conn.execute(
                "SELECT source_event_ids_json, source_memory_ids_json "
                "FROM memory_atoms WHERE id = 'atom:merge-target'"
            ).fetchone()
            source = conn.execute(
                "SELECT status FROM memory_atoms WHERE id = 'atom:merge-source'"
            ).fetchone()
            aliases = {
                (str(row["alias_type"]), str(row["alias"]))
                for row in conn.execute(
                    "SELECT alias_type, alias FROM memory_aliases "
                    "WHERE memory_atom_id = 'atom:merge-target'"
                ).fetchall()
            }
            tags = {
                str(row["tag"])
                for row in conn.execute(
                    "SELECT mt.tag FROM memory_atom_tags mat "
                    "JOIN memory_tags mt ON CAST(mt.id AS TEXT) = mat.tag_id "
                    "WHERE mat.memory_atom_id = 'atom:merge-target'"
                ).fetchall()
            }
            groups = {
                str(row["group_id"])
                for row in conn.execute(
                    "SELECT group_id FROM memory_semantic_group_members "
                    "WHERE member_type = 'atom' "
                    "AND member_id = 'atom:merge-target'"
                ).fetchall()
            }

        self.assertEqual(json.loads(target["source_event_ids_json"]), [self.event_id])
        self.assertEqual(
            set(json.loads(target["source_memory_ids_json"])),
            {"memory:target", "memory:source"},
        )
        self.assertEqual(source["status"], "superseded")
        self.assertTrue(
            {
                ("alias", "target-alias"),
                ("alias", "source-alias"),
                ("surface_hint", "target-surface"),
                ("surface_hint", "source-surface"),
                ("query_expansion", "target-query"),
                ("query_expansion", "source-query"),
            }.issubset(aliases)
        )
        self.assertEqual(tags, {"target-tag", "source-tag"})
        self.assertEqual(groups, {"group:target", "group:source"})

    def test_reviewed_tag_merge_moves_graph_and_rolls_back(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            canonical_id = int(
                conn.execute(
                    "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                    "description, source, status, metadata_json) VALUES ('BM25', 'bm25', 'tech', 0.9, 1, 1, "
                    "'词法召回', 'dsv4', 'active', '{}')"
                ).lastrowid
            )
            duplicate_id = int(
                conn.execute(
                    "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                    "description, source, status, metadata_json) VALUES ('BM25算法', 'bm25算法', 'tech', 0.8, 1, 1, "
                    "'BM25 别名', 'dsv4', 'active', '{}')"
                ).lastrowid
            )
            related_id = int(
                conn.execute(
                    "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                    "description, source, status, metadata_json) VALUES ('混合召回', '混合召回', 'concept', 0.85, 1, 1, "
                    "'多路融合', 'dsv4', 'active', '{}')"
                ).lastrowid
            )
            conn.execute(
                "INSERT INTO memory_tag_profiles(tag_id, color_token, aliases_json, updated_at_ms) VALUES (?, 'blue', '[\"BM25算法\"]', 1)",
                (canonical_id,),
            )
            conn.execute(
                "INSERT INTO memory_tag_edges(src_tag_id, dst_tag_id, edge_type, weight, direction_bias, evidence_count, "
                "updated_at_ms, metadata_json) VALUES (?, ?, 'part_of', 0.8, 0.7, 2, 1, '{\"source\":\"duplicate\"}')",
                (duplicate_id, related_id),
            )
            conn.execute(
                "INSERT INTO memory_tag_edges(src_tag_id, dst_tag_id, edge_type, weight, direction_bias, evidence_count, "
                "updated_at_ms, metadata_json) VALUES (?, ?, 'part_of', 0.9, -0.25, 3, 1, '{\"source\":\"canonical\"}')",
                (canonical_id, related_id),
            )
            bundle = build_memory_book_source_bundle(conn, project="wisdom-weasel-rag-ime")

        output = {
            "schemaVersion": "rag-ime.memory-book-compile.v1",
            "semanticGroups": [],
            "semanticTags": [],
            "tagMerges": [
                {
                    "source": "BM25算法",
                    "target": "BM25",
                    "reason": "同义术语",
                    "evidenceEventIds": [self.event_id],
                    "confidence": 0.98,
                }
            ],
            "tagEdges": [],
        }
        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        report = inspect_memory_book_plan(plan)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["counts"]["tagMerges"], 1)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            applied = apply_memory_book_plan(conn, plan)
            self.assertEqual(applied["status"], "applied")
            self.assertIsNone(conn.execute("SELECT id FROM memory_tags WHERE id = ?", (duplicate_id,)).fetchone())
            moved = conn.execute(
                "SELECT weight, direction_bias, evidence_count, metadata_json "
                "FROM memory_tag_edges "
                "WHERE src_tag_id = ? AND dst_tag_id = ? AND edge_type = 'part_of'",
                (canonical_id, related_id),
            ).fetchone()
            self.assertIsNotNone(moved)
            self.assertEqual(moved["weight"], 0.9)
            self.assertEqual(moved["direction_bias"], -0.25)
            self.assertEqual(moved["evidence_count"], 5)
            self.assertIn("duplicate", moved["metadata_json"])
            self.assertIn("canonical", moved["metadata_json"])
            self.assertIn("directionBias", moved["metadata_json"])
            aliases = json.loads(
                conn.execute("SELECT aliases_json FROM memory_tag_profiles WHERE tag_id = ?", (canonical_id,)).fetchone()[0]
            )
            self.assertIn("BM25算法", aliases)
            rollback_memory_book_run(conn, run_id=plan["runId"])
            self.assertIsNotNone(conn.execute("SELECT id FROM memory_tags WHERE id = ?", (duplicate_id,)).fetchone())
            restored = conn.execute(
                "SELECT 1 FROM memory_tag_edges WHERE src_tag_id = ? AND dst_tag_id = ? AND edge_type = 'part_of'",
                (duplicate_id, related_id),
            ).fetchone()
            self.assertIsNotNone(restored)

    def test_equal_name_tag_merge_uses_physical_ids(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            canonical_id = int(
                conn.execute(
                    "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                    "description, source, status, metadata_json) VALUES ('Agent', 'agent', 'concept', 0.9, 1, 1, "
                    "'canonical Agent tag', 'dsv4', 'active', '{}')"
                ).lastrowid
            )
            duplicate_id = int(
                conn.execute(
                    "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                    "description, source, status, metadata_json) VALUES ('agent', 'agent', 'concept', 0.8, 1, 1, "
                    "'duplicate Agent tag', 'dsv4', 'active', '{}')"
                ).lastrowid
            )
            bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
            )

        plan = memory_book_plan_from_compile_output(
            {
                "schemaVersion": "rag-ime.memory-book-compile.v1",
                "tagMerges": [
                    {
                        "source": "agent",
                        "target": "Agent",
                        "sourceTagId": duplicate_id,
                        "targetTagId": canonical_id,
                        "reason": "normalized duplicate",
                        "evidenceEventIds": [self.event_id],
                    }
                ],
            },
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        report = inspect_memory_book_plan(plan)
        self.assertTrue(report["ok"], report)
        merge = next(
            item for item in plan["diffs"] if item["op"] == "merge_semantic_tag"
        )
        self.assertEqual(merge["payload"]["sourceTagId"], duplicate_id)
        self.assertEqual(merge["payload"]["targetTagId"], canonical_id)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            applied = apply_memory_book_plan(conn, plan)
            self.assertEqual(applied["status"], "applied")
            self.assertIsNone(
                conn.execute(
                    "SELECT id FROM memory_tags WHERE id = ?",
                    (duplicate_id,),
                ).fetchone()
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT id FROM memory_tags WHERE id = ?",
                    (canonical_id,),
                ).fetchone()
            )
            rollback_memory_book_run(conn, run_id=plan["runId"])
            self.assertIsNotNone(
                conn.execute(
                    "SELECT id FROM memory_tags WHERE id = ?",
                    (duplicate_id,),
                ).fetchone()
            )

    def test_physical_tag_merge_rejects_self_edge_before_deletion(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            source_id = int(
                conn.execute(
                    "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                    "description, source, status, metadata_json) VALUES ('source-tag', 'source-tag', 'concept', 0.8, 1, 1, "
                    "'source', 'dsv4', 'active', '{}')"
                ).lastrowid
            )
            target_id = int(
                conn.execute(
                    "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                    "description, source, status, metadata_json) VALUES ('target-tag', 'target-tag', 'concept', 0.9, 1, 1, "
                    "'target', 'dsv4', 'active', '{}')"
                ).lastrowid
            )
            conn.execute(
                "INSERT INTO memory_tag_edges(src_tag_id, dst_tag_id, edge_type, weight, direction_bias, evidence_count, "
                "updated_at_ms, metadata_json) VALUES (?, ?, 'related_to', 0.8, 0, 1, 1, '{}')",
                (source_id, target_id),
            )
            bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
            )
        plan = memory_book_plan_from_compile_output(
            {
                "schemaVersion": "rag-ime.memory-book-compile.v1",
                "tagMerges": [
                    {
                        "source": "source-tag",
                        "target": "target-tag",
                        "sourceTagId": source_id,
                        "targetTagId": target_id,
                        "reason": "test",
                    }
                ],
            },
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            with self.assertRaisesRegex(ValueError, "self edge"):
                apply_memory_book_plan(conn, plan)
            self.assertIsNotNone(
                conn.execute(
                    "SELECT id FROM memory_tags WHERE id = ?",
                    (source_id,),
                ).fetchone()
            )

    def test_virtual_tag_merge_canonicalizes_without_creating_an_unapplicable_diff(self) -> None:
        output = {
            "schemaVersion": "rag-ime.memory-book-compile.v1",
            "semanticGroups": [],
            "semanticTags": [
                {
                    "name": "BM25算法",
                    "description": "SQLite FTS5 的词法相关性算法。",
                    "sourceEventIds": [self.event_id],
                }
            ],
            "tagMerges": [
                {
                    "source": "BM25",
                    "target": "BM25算法",
                    "reason": "本批术语规范化",
                    "evidenceEventIds": [self.event_id],
                }
            ],
        }
        source_bundle = {
            "recentEvents": [{"eventId": self.event_id, "sourceEventIds": [self.event_id], "text": "BM25 算法"}],
            "existingSemanticGroups": [],
            "existingSemanticTags": [],
        }

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=source_bundle,
        )

        self.assertFalse(any(item["op"] == "merge_semantic_tag" for item in plan["diffs"]))
        tag = next(item for item in plan["diffs"] if item["op"] == "upsert_semantic_tag")
        self.assertEqual(tag["payload"]["name"], "BM25算法")
        self.assertIn("virtual_tag_merge_canonicalized:BM25->BM25算法", plan["metadata"]["warnings"])

    def test_tag_merge_plan_drops_reverse_cycles(self) -> None:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            conn.execute(
                "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                "description, source, status, metadata_json) VALUES ('BM25', 'bm25', 'tech', 0.9, 1, 1, "
                "'词法召回', 'dsv4', 'active', '{}')"
            )
            conn.execute(
                "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, "
                "description, source, status, metadata_json) VALUES ('BM25算法', 'bm25算法', 'tech', 0.8, 1, 1, "
                "'BM25 别名', 'dsv4', 'active', '{}')"
            )
            bundle = build_memory_book_source_bundle(conn, project="wisdom-weasel-rag-ime")
        output = {
            "schemaVersion": "rag-ime.memory-book-compile.v1",
            "tagMerges": [
                {"source": "BM25算法", "target": "BM25", "reason": "规范到短名称"},
                {"source": "BM25", "target": "BM25算法", "reason": "反向循环"},
            ],
        }

        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
            source_bundle=bundle,
        )
        merges = [item for item in plan["diffs"] if item["op"] == "merge_semantic_tag"]

        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]["payload"]["source"], "BM25算法")
        self.assertEqual(merges[0]["payload"]["target"], "BM25")

    def test_legacy_stored_draft_skips_a_missing_merge_source_instead_of_aborting(self) -> None:
        output = {
            "schemaVersion": "rag-ime.memory-book-compile.v1",
            "semanticGroups": [],
            "semanticTags": [
                {
                    "name": "BM25算法",
                    "description": "SQLite FTS5 的词法相关性算法。",
                    "sourceEventIds": [self.event_id],
                }
            ],
        }
        plan = memory_book_plan_from_compile_output(
            output,
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        plan["diffs"].append(
            {
                "op": "merge_semantic_tag",
                "targetId": "tag-merge:BM25->BM25算法",
                "payload": {
                    "source": "BM25",
                    "target": "BM25算法",
                    "reason": "旧版草案留下的虚拟合并",
                    "evidenceEventIds": [self.event_id],
                },
                "status": "pending",
            }
        )

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            store_memory_book_plan(conn, plan)
            applied = apply_stored_memory_book_run(conn, run_id=plan["runId"])
            merge = next(item for item in applied["diffs"] if item["op"] == "merge_semantic_tag")
            rollback = json.loads(
                conn.execute(
                    "SELECT rollback_json FROM memory_cleanup_diffs WHERE id = ?",
                    (merge["diffId"],),
                ).fetchone()[0]
            )

        self.assertEqual(applied["status"], "applied")
        self.assertEqual(merge["status"], "applied")
        self.assertTrue(rollback["noOp"])
        self.assertEqual(rollback["reason"], "missing_source")

    def _write_sample_plan(self) -> Path:
        plan = memory_book_plan_from_compile_output(
            sample_compile_output(self.event_id),
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        path = Path(self.tmp.name) / "memory-book-plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def sample_compile_output(event_id: int) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.memory-book-compile.v1",
        "dailyBooks": [
            {
                "bookKey": "2026-07-06",
                "title": "RAG 输入法多路召回方案",
                "summary": "用户希望借鉴 VCP 的 BM25、向量、TagMemo 和 Time。",
                "tags": ["RAG", "输入法", "VCP"],
                "surfaceHints": ["多路召回", "TagMemo"],
                "queryExpansions": ["VCP RAG", "Daily Book"],
                "sourceEventIds": [event_id],
                "confidence": 0.86,
            }
        ],
        "memoryAtoms": [
            {
                "atomId": "atom:vcp-style-rag-core",
                "kind": "project_fact",
                "claimKey": "project:rag-ime.retrieval-architecture",
                "canonicalText": "RAG core 应使用 BM25、向量、TagMemo 和 Time 多路召回。",
                "summary": "用户希望底层 RAG core 成为通用上下文预测层。",
                "tags": ["RAG core", "BM25", "TagMemo"],
                "aliases": ["VCP式RAG"],
                "surfaceHints": ["混合召回", "语义图召回"],
                "queryExpansions": ["输入法 RAG", "千问三 Qwen3"],
                "sourceEventIds": [event_id],
                "directCandidateAllowed": False,
                "confidence": 0.88,
                "qualityScore": 0.82,
            }
        ],
        "tagEdges": [
            {"src": "输入法", "dst": "大模型", "edgeType": "related", "weight": 0.72, "evidenceEventIds": [event_id]}
        ],
        "phraseCandidates": [
            {
                "text": "多路召回",
                "pinyin": "duo lu zhao hui",
                "tags": ["RAG", "检索"],
                "sourceEventIds": [event_id],
                "weight": 0.74,
                "reason": "用户反复讨论的领域术语",
            }
        ],
        "warnings": [],
    }


if __name__ == "__main__":
    unittest.main()
