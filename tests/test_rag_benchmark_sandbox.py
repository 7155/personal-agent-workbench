from __future__ import annotations

import errno
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.rag_benchmark_sandbox import (
    RUN_MARKER_NAME,
    RagBenchmarkSandbox,
    RagBenchmarkSandboxError,
    RagBenchmarkSandboxPolicy,
    RagBenchmarkSandboxTool,
    _diversify_reranked_hits,
)


class _FakeKnowledgeReranker:
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    def rerank(self, _query, candidates, *, limit, candidate_limit=100):
        self.calls += 1
        ranked = list(reversed([dict(item) for item in candidates[:candidate_limit]]))
        return ranked[:limit]

    def status(self):
        return {
            "provider": "fixture-reranker",
            "configured": True,
            "fingerprint": "fixture:sha256:" + "1" * 64,
            "calls": self.calls,
            "errorCount": 0,
            "fallbackCount": 0,
            "independentStage": True,
            "subagentSubstitute": False,
        }


class RagBenchmarkSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "rag-benchmark-runs"
        self.outside = Path(self.temporary.name) / "outside.txt"
        self.outside.write_text("must survive sandbox cleanup\n", encoding="utf-8")
        self.sandbox = RagBenchmarkSandbox(self.root)
        self.addCleanup(self.sandbox.close)

    def test_lifecycle_builds_tunes_rebuilds_searches_and_cleans_one_owned_run(self) -> None:
        run = self.sandbox.create_run("session-a", label="knowledge-held-out")
        run_id = run["runId"]
        base = self.sandbox.create_base(
            "session-a",
            run_id,
            alias="enterprise",
            name="Enterprise handbook",
        )

        imported = self.sandbox.import_documents(
            "session-a",
            run_id,
            base_alias="enterprise",
            documents=[
                {
                    "externalId": "policy-001",
                    "name": "travel-policy.md",
                    "text": (
                        "# 差旅制度\n\n北斗项目的差旅审批人是林岚，报销期限为三十天。\n\n"
                        "Beidou project travel approver: Lin Lan.\n"
                    ),
                },
                {
                    "externalId": "policy-002",
                    "name": "security-policy.md",
                    "text": "# 安全制度\n\n生产密钥不得写入日志或知识库文档。\n",
                },
            ],
        )

        self.assertEqual("enterprise", base["baseAlias"])
        self.assertEqual(2, imported["importedCount"])
        self.assertEqual(["policy-001", "policy-002"], [item["externalDocumentId"] for item in imported["documents"]])
        self.assertTrue(all(item["status"] == "ready" for item in imported["documents"]))

        search = self.sandbox.search(
            "session-a",
            run_id,
            base_alias="enterprise",
            query="Beidou travel approver",
            top_k=2,
            mode="lexical",
        )

        self.assertGreaterEqual(search["total"], 1)
        self.assertEqual("policy-001", search["hits"][0]["externalDocumentId"])
        self.assertEqual("policy-001", search["hits"][0]["citation"]["externalDocumentId"])
        self.assertRegex(search["hits"][0]["citationRef"], r"^K-[0-9a-f]{10}$")
        self.assertEqual(
            search["hits"][0]["citationRef"],
            search["hits"][0]["citation"]["citationRef"],
        )
        self.assertNotIn(str(self.root), str(search))

        configured = self.sandbox.configure_base(
            "session-a",
            run_id,
            base_alias="enterprise",
            expected_revision=base["configRevision"],
            chunking_config={"strategy": "fixed", "size": 240, "overlap": 24},
            retrieval_config={
                "mode": "hybrid",
                "topK": 6,
                "threshold": 0.0,
                "lexicalWeight": 1.0,
                "denseWeight": 1.0,
                "rrfK": 60,
                "candidateMultiplier": 4,
            },
        )
        self.assertTrue(configured["reindexRequired"])
        preview = self.sandbox.rebuild_preview(
            "session-a",
            run_id,
            base_alias="enterprise",
        )
        rebuilt = self.sandbox.rebuild(
            "session-a",
            run_id,
            base_alias="enterprise",
            preview_token=preview["previewToken"],
            expected_revision=preview["configRevision"],
            confirm_text="REBUILD",
        )
        self.assertEqual(2, rebuilt["ready"])

        graph = self.sandbox.rebuild_graph(
            "session-a",
            run_id,
            base_alias="enterprise",
            expected_revision=0,
            extractor_mode="deterministic",
        )
        self.assertEqual("ready", graph["status"])
        self.assertEqual(1, graph["revision"])
        self.assertTrue(graph["knowledgeOnly"])
        self.assertFalse(graph["memoryMutationPerformed"])
        self.assertGreaterEqual(graph["stats"]["documentCount"], 2)

        status = self.sandbox.status("session-a", run_id)
        self.assertEqual(1, status["baseCount"])
        self.assertEqual(2, status["documentCount"])
        self.assertEqual(1, status["usage"]["searchCalls"])
        self.assertEqual(0, status["usage"]["rerankCalls"])
        self.assertEqual(1, status["usage"]["rebuilds"])
        self.assertEqual(1, status["usage"]["graphRebuilds"])
        self.assertEqual("ready", status["bases"][0]["knowledgeGraph"]["status"])
        self.assertFalse(status["reranker"]["configured"])
        self.assertNotIn(str(self.root), str(status))

        cleaned = self.sandbox.cleanup(
            "session-a",
            run_id,
            confirm_text="DELETE_BENCHMARK_RUN",
        )
        self.assertTrue(cleaned["deleted"])
        self.assertFalse((self.root / f"run-{run_id}").exists())
        self.assertEqual("must survive sandbox cleanup\n", self.outside.read_text(encoding="utf-8"))

    def test_cleanup_retries_partial_directory_not_empty_race(self) -> None:
        run_id = self.sandbox.create_run("session-retry")["runId"]
        run_path = self.root / f"run-{run_id}"
        original_rmtree = shutil.rmtree
        calls = 0

        def flaky_rmtree(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                original_rmtree(path)
                Path(path).mkdir(mode=0o700)
                (Path(path) / ".DS_Store").write_bytes(b"finder race")
                raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
            original_rmtree(path)

        with patch(
            "rag_ime.rag_benchmark_sandbox.shutil.rmtree",
            side_effect=flaky_rmtree,
        ):
            cleaned = self.sandbox.cleanup(
                "session-retry",
                run_id,
                confirm_text="DELETE_BENCHMARK_RUN",
            )

        self.assertTrue(cleaned["deleted"])
        self.assertEqual(2, calls)
        self.assertFalse(run_path.exists())

    def test_search_projection_preserves_score_tristate_and_rejects_invalid_measurements(self) -> None:
        run_id = self.sandbox.create_run("session-score")["runId"]
        self.sandbox.create_base("session-score", run_id, alias="kb", name="Score fixture")
        self.sandbox.import_documents(
            "session-score",
            run_id,
            base_alias="kb",
            documents=[{"externalId": "doc-1", "name": "doc.md", "text": "score fixture"}],
        )
        marker = json.loads((self.root / f"run-{run_id}" / RUN_MARKER_NAME).read_text(encoding="utf-8"))
        document_id = marker["documents"]["doc-1"]["documentId"]
        service = self.sandbox._services[run_id]
        raw_hit = {
            "documentId": document_id,
            "chunkId": "chunk-1",
            "documentName": "doc.md",
            "ordinal": 0,
            "content": "score fixture",
            "diagnostics": {
                "lexicalRank": 2, "lexicalScore": 0.0, "retrievalRank": 1,
                "denseScore": math.nan, "graphScore": True,
                "workerPath": "/private/worker/path", "graphPaths": ["private context"],
            },
        }
        with patch.object(
            service,
            "search",
            side_effect=[
                {"hits": [raw_hit]},
                {"hits": [{**raw_hit, "score": 0}]},
                {"hits": [{**raw_hit, "score": -2.5}]},
            ],
        ):
            missing = self.sandbox.search("session-score", run_id, base_alias="kb", query="score", mode="lexical")
            zero = self.sandbox.search("session-score", run_id, base_alias="kb", query="score", mode="lexical")
            negative = self.sandbox.search("session-score", run_id, base_alias="kb", query="score", mode="lexical")

        self.assertNotIn("score", missing["hits"][0])
        self.assertEqual(0.0, zero["hits"][0]["score"])
        self.assertEqual(-2.5, negative["hits"][0]["score"])
        self.assertEqual(
            {"lexicalRank": 2, "lexicalScore": 0.0, "retrievalRank": 1},
            zero["hits"][0]["diagnostics"],
        )
        self.assertNotIn("private", json.dumps(zero))

        for invalid in (True, "0.5", math.nan, math.inf, -math.inf):
            with self.subTest(invalid=invalid):
                with patch.object(service, "search", return_value={"hits": [{**raw_hit, "score": invalid}]}):
                    with self.assertRaisesRegex(RagBenchmarkSandboxError, "invalid score"):
                        self.sandbox.search(
                            "session-score",
                            run_id,
                            base_alias="kb",
                            query="score",
                            mode="lexical",
                        )

    def test_lexical_and_hashing_dense_search_return_real_hits_and_finite_scores(self) -> None:
        run_id = self.sandbox.create_run("session-modes")["runId"]
        self.sandbox.create_base("session-modes", run_id, alias="kb", name="Modes fixture")
        self.sandbox.import_documents(
            "session-modes",
            run_id,
            base_alias="kb",
            documents=[
                {"externalId": "doc-1", "name": "doc.md", "text": "alpha beta retrieval"},
                {"externalId": "doc-2", "name": "other.md", "text": "unrelated text"},
            ],
        )
        for mode in ("lexical", "dense"):
            with self.subTest(mode=mode):
                result = self.sandbox.search(
                    "session-modes",
                    run_id,
                    base_alias="kb",
                    query="alpha beta",
                    top_k=1,
                    mode=mode,
                )
                self.assertEqual(1, result["total"])
                self.assertEqual("doc-1", result["hits"][0]["externalDocumentId"])
                self.assertIsInstance(result["hits"][0]["score"], (int, float))
                self.assertTrue(math.isfinite(result["hits"][0]["score"]))

    def test_owner_binding_and_marker_integrity_fail_closed(self) -> None:
        run = self.sandbox.create_run("session-owner")
        run_id = run["runId"]

        with self.assertRaises(RagBenchmarkSandboxError) as owner_error:
            self.sandbox.status("session-other", run_id)
        self.assertEqual("owner_mismatch", owner_error.exception.code)

        marker_path = self.root / f"run-{run_id}" / RUN_MARKER_NAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["runId"] = "0" * 32
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

        with self.assertRaises(RagBenchmarkSandboxError) as marker_error:
            self.sandbox.cleanup(
                "session-owner",
                run_id,
                confirm_text="DELETE_BENCHMARK_RUN",
            )
        self.assertEqual("marker_mismatch", marker_error.exception.code)
        self.assertTrue((self.root / f"run-{run_id}").is_dir())
        self.assertTrue(self.outside.is_file())

    def test_search_can_use_an_independent_bounded_reranker(self) -> None:
        reranker = _FakeKnowledgeReranker()
        sandbox = RagBenchmarkSandbox(
            self.root / "rerank",
            reranker=reranker,
        )
        self.addCleanup(sandbox.close)
        run_id = sandbox.create_run("session-rerank")["runId"]
        sandbox.create_base(
            "session-rerank",
            run_id,
            alias="kb",
            name="Rerank fixture",
        )
        sandbox.import_documents(
            "session-rerank",
            run_id,
            base_alias="kb",
            documents=[
                {
                    "externalId": "doc-a",
                    "name": "a.txt",
                    "text": "共同检索词 第一份证据",
                },
                {
                    "externalId": "doc-b",
                    "name": "b.txt",
                    "text": "共同检索词 第二份证据",
                },
            ],
        )

        result = sandbox.search(
            "session-rerank",
            run_id,
            base_alias="kb",
            query="共同检索词",
            top_k=1,
            mode="lexical",
            rerank=True,
            rerank_candidate_depth=2,
        )
        status = sandbox.status("session-rerank", run_id)

        self.assertEqual(1, result["total"])
        self.assertTrue(result["retrieval"]["rerank"]["enabled"])
        self.assertEqual(2, result["retrieval"]["rerank"]["candidateDepth"])
        self.assertEqual(1, result["retrieval"]["rerank"]["finalDepth"])
        self.assertTrue(result["retrieval"]["rerank"]["independentStage"])
        self.assertFalse(result["retrieval"]["rerank"]["subagentSubstitute"])
        self.assertEqual("externalDocumentId", result["retrieval"]["rerank"]["diversityKey"])
        self.assertEqual(1, status["usage"]["rerankCalls"])
        self.assertEqual(1, status["reranker"]["calls"])

    def test_rerank_packing_keeps_one_best_chunk_per_document(self) -> None:
        packed, dropped = _diversify_reranked_hits(
            [
                {"externalDocumentId": "doc-a", "chunkId": "a-1", "rerankScore": 0.9},
                {"externalDocumentId": "doc-a", "chunkId": "a-2", "rerankScore": 0.8},
                {"externalDocumentId": "doc-b", "chunkId": "b-1", "rerankScore": 0.7},
            ],
            limit=2,
        )

        self.assertEqual(["a-1", "b-1"], [item["chunkId"] for item in packed])
        self.assertEqual([1, 2], [item["packedRank"] for item in packed])
        self.assertEqual(1, dropped)

    def test_document_and_search_budgets_reject_duplicates_oversize_and_extra_calls(self) -> None:
        sandbox = RagBenchmarkSandbox(
            self.root / "bounded",
            policy=RagBenchmarkSandboxPolicy(
                max_runs=1,
                max_bases_per_run=1,
                max_documents_per_run=2,
                max_documents_per_call=1,
                max_document_bytes=96,
                max_total_source_bytes=128,
                max_search_calls=1,
                max_rebuilds=1,
            ),
        )
        self.addCleanup(sandbox.close)
        run_id = sandbox.create_run("session-a")["runId"]
        sandbox.create_base("session-a", run_id, alias="kb", name="Bounded")

        with self.assertRaises(RagBenchmarkSandboxError) as batch_error:
            sandbox.import_documents(
                "session-a",
                run_id,
                base_alias="kb",
                documents=[
                    {"externalId": "a", "name": "a.md", "text": "first"},
                    {"externalId": "b", "name": "b.md", "text": "second"},
                ],
            )
        self.assertEqual("budget_exceeded", batch_error.exception.code)

        sandbox.import_documents(
            "session-a",
            run_id,
            base_alias="kb",
            documents=[{"externalId": "a", "name": "a.md", "text": "alpha evidence"}],
        )
        with self.assertRaises(RagBenchmarkSandboxError) as duplicate_error:
            sandbox.import_documents(
                "session-a",
                run_id,
                base_alias="kb",
                documents=[{"externalId": "a", "name": "copy.md", "text": "other evidence"}],
            )
        self.assertEqual("duplicate_document", duplicate_error.exception.code)

        with self.assertRaises(RagBenchmarkSandboxError) as size_error:
            sandbox.import_documents(
                "session-a",
                run_id,
                base_alias="kb",
                documents=[{"externalId": "large", "name": "large.md", "text": "x" * 97}],
            )
        self.assertEqual("budget_exceeded", size_error.exception.code)

        sandbox.search("session-a", run_id, base_alias="kb", query="alpha")
        with self.assertRaises(RagBenchmarkSandboxError) as search_error:
            sandbox.search("session-a", run_id, base_alias="kb", query="alpha again")
        self.assertEqual("budget_exceeded", search_error.exception.code)

    def test_tool_is_benchmark_local_and_never_accepts_arbitrary_source_paths(self) -> None:
        tool = RagBenchmarkSandboxTool(self.sandbox)
        manifest = tool.manifest()

        self.assertEqual("rag_benchmark", manifest["id"])
        self.assertEqual(
            [
                "create_run",
                "create_base",
                "import_documents",
                "configure_base",
                "rebuild_preview",
                "rebuild",
                "graph_rebuild",
                "search",
                "evaluate_validation",
                "status",
                "cleanup",
            ],
            manifest["operations"],
        )
        self.assertEqual("benchmark-only", manifest["scope"])
        self.assertEqual("object", manifest["parameters"]["type"])

        run = tool.execute("session-a", {"op": "create_run", "label": "tool-test"})
        tool.execute(
            "session-a",
            {"op": "create_base", "runId": run["runId"], "baseAlias": "kb", "name": "Tool KB"},
        )
        with self.assertRaises(RagBenchmarkSandboxError) as path_error:
            tool.execute(
                "session-a",
                {
                    "op": "import_documents",
                    "runId": run["runId"],
                    "baseAlias": "kb",
                    "documents": [
                        {
                            "externalId": "forbidden",
                            "name": "forbidden.md",
                            "text": "inline content",
                            "sourcePath": "/etc/passwd",
                        }
                    ],
                },
            )
        self.assertEqual("invalid_argument", path_error.exception.code)
        self.assertNotIn(str(self.root), str(run))

    def test_agent_can_score_registered_validation_without_receiving_qrels(self) -> None:
        sandbox = RagBenchmarkSandbox(
            self.root / "validation-eval",
            evaluation_suites={
                "interview-validation": [
                    {
                        "caseId": "approver",
                        "split": "validation",
                        "query": "Beidou travel approver",
                        "relevant": {"policy-001": 1},
                    },
                    {
                        "caseId": "secret-policy",
                        "split": "validation",
                        "query": "production keys logs",
                        "relevant": {"policy-002": 1},
                    },
                ]
            },
        )
        self.addCleanup(sandbox.close)
        tool = RagBenchmarkSandboxTool(sandbox)
        run_id = tool.execute("session-eval", {"op": "create_run"})["runId"]
        tool.execute(
            "session-eval",
            {
                "op": "create_base",
                "runId": run_id,
                "baseAlias": "kb",
                "name": "Validation fixture",
            },
        )
        tool.execute(
            "session-eval",
            {
                "op": "import_documents",
                "runId": run_id,
                "baseAlias": "kb",
                "documents": [
                    {
                        "externalId": "policy-001",
                        "name": "travel.md",
                        "text": "Beidou travel approver is Lin Lan.",
                    },
                    {
                        "externalId": "policy-002",
                        "name": "security.md",
                        "text": "Production keys must never be written to logs.",
                    },
                ],
            },
        )

        result = tool.execute(
            "session-eval",
            {
                "op": "evaluate_validation",
                "runId": run_id,
                "baseAlias": "kb",
                "evaluationSuiteId": "interview-validation",
                "topK": 2,
                "mode": "lexical",
            },
        )

        self.assertEqual("validation", result["split"])
        self.assertEqual(2, result["caseCount"])
        self.assertEqual([1, 2], result["kValues"])
        self.assertEqual({"1", "2"}, set(result["metrics"]["recallAtK"]))
        self.assertEqual(1.0, result["metrics"]["mrr"])
        self.assertEqual(1.0, result["metrics"]["recallAtK"]["1"])
        self.assertFalse(result["qrelsVisibleToAgent"])
        self.assertFalse(result["perCaseResultsVisible"])
        self.assertFalse(result["heldOutLabelsObserved"])
        self.assertNotIn("perQuery", result)
        self.assertNotIn("policy-001", str(result))
        self.assertEqual(1, result["usage"]["evaluationCalls"])
        self.assertEqual(2, result["usage"]["searchCalls"])

    def test_agent_callable_evaluation_rejects_held_out_suites(self) -> None:
        with self.assertRaises(RagBenchmarkSandboxError) as error:
            RagBenchmarkSandbox(
                self.root / "held-out-forbidden",
                evaluation_suites={
                    "forbidden": [
                        {
                            "caseId": "held-1",
                            "split": "held_out",
                            "query": "hidden question",
                            "relevant": {"doc-1": 1},
                        }
                    ]
                },
            )

        self.assertEqual("held_out_forbidden", error.exception.code)

    def test_status_preserves_dense_degradation_evidence(self) -> None:
        class DenseFailureService:
            def status(self) -> dict[str, object]:
                return {
                    "dense": {
                        "available": True,
                        "degraded": True,
                        "kind": "sqlite-exact-vector-scan",
                        "backend": "sqlite-exact",
                        "ann": False,
                        "scalable": False,
                        "provider": {
                            "provider": "mlx-bert",
                            "semantic": True,
                        },
                        "vectorCount": 0,
                        "fallbackFrom": "usearch",
                        "reason": "USearch ANN dependency is missing",
                    }
                }

            def close(self) -> None:
                return None

        sandbox = RagBenchmarkSandbox(
            self.root / "dense-status",
            service_factory=lambda _root, _policy: DenseFailureService(),  # type: ignore[arg-type]
        )
        self.addCleanup(sandbox.close)
        run_id = sandbox.create_run("session-a")["runId"]

        dense = sandbox.status("session-a", run_id)["dense"]

        self.assertTrue(dense["degraded"])
        self.assertEqual("usearch", dense["fallbackFrom"])
        self.assertEqual("USearch ANN dependency is missing", dense["reason"])


if __name__ == "__main__":
    unittest.main()
