from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest import mock

from rag_ime.knowledge_library import KnowledgeLibraryConfig, KnowledgeLibraryService, LocalKnowledgeClient
from rag_ime.knowledge_library.dense import SqliteDenseIndex, USearchDenseIndex


class _FreshnessEmbedding:
    fingerprint = "fixture:freshness:v1"

    @staticmethod
    def _vector(text: str) -> list[float]:
        normalized = text.lower()
        if "query" in normalized or "stale" in normalized:
            return [1.0, 0.0]
        return [0.9, 0.1]

    def embed(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_many(self, texts: list[str], *, batch_size: int = 32) -> list[list[float]]:
        return [self._vector(text) for text in texts]


class _ScoreMapReranker:
    configured = True

    def __init__(self, scores: Mapping[str, float]) -> None:
        self.scores = dict(scores)
        self.calls: list[dict[str, Any]] = []

    def rerank(
        self,
        query: str,
        candidates: Sequence[Mapping[str, Any]],
        *,
        limit: int,
        candidate_limit: int = 100,
    ) -> list[dict[str, Any]]:
        selected = [dict(item) for item in candidates[:candidate_limit]]
        self.calls.append({"ids": [str(item["chunkId"]) for item in selected], "limit": limit})
        ranked = sorted(
            selected,
            key=lambda item: (-self.scores.get(str(item["chunkId"]), 0.0), str(item["chunkId"])),
        )
        return [
            {
                **item,
                "rerankScore": self.scores.get(str(item["chunkId"]), 0.0),
            }
            for item in ranked[:limit]
        ]

    def status(self) -> dict[str, Any]:
        return {
            "provider": "fixture-score-map",
            "configured": True,
            "fingerprint": "fixture:score-map:v1",
            "fallbackCount": 0,
        }


class _CountingFreshnessEmbedding(_FreshnessEmbedding):
    def __init__(self) -> None:
        self.query_calls = 0

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return super().embed_query(text)


class KnowledgeRetrievalEngineeringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="paw-knowledge-retrieval-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = KnowledgeLibraryService(KnowledgeLibraryConfig(self.root / "Knowledge"))
        self.service.dense_index = SqliteDenseIndex(
            self.service.config.database_path,
            _FreshnessEmbedding(),
        )
        self.addCleanup(self.service.close)

    def _base(self, *, reranker: bool = False) -> dict[str, Any]:
        return self.service.create_base(
            "Retrieval fixture",
            agent_enabled=True,
            chunking_config={
                "strategy": "separator",
                "size": 200,
                "overlap": 0,
                "separator": "\n<CUT>\n",
                "respectHeadings": False,
                "respectPageBoundaries": False,
            },
            retrieval_config={
                "mode": "lexical" if reranker else "dense",
                "topK": 1,
                "threshold": 0.8 if reranker else 0.0,
                "rerankEnabled": reranker,
                "rerankCandidateDepth": 2,
            },
        )

    def test_dense_filters_stale_candidates_before_limit_and_keeps_base_scope(self) -> None:
        base = self._base()
        stale_path = self.root / "stale.md"
        stale_path.write_text(
            "\n<CUT>\n".join(
                f"stale target block {index} " + ("filler " * 24)
                for index in range(120)
            ),
            encoding="utf-8",
        )
        ready_path = self.root / "ready.md"
        ready_path.write_text("ready evidence", encoding="utf-8")

        stale = self.service.import_document(base["id"], stale_path)
        ready = self.service.import_document(base["id"], ready_path)
        self.assertGreater(stale["chunkCount"], 100)
        self.service.store.update_document(stale["documentId"], {"status": "stale"})

        result = self.service.search(
            "query",
            base_ids=(base["id"],),
            mode="dense",
            limit=1,
        )

        self.assertEqual([ready["documentId"]], [item["documentId"] for item in result["hits"]])
        self.assertEqual(1, result["retrieval"]["libraries"][0]["returned"])

        hidden_base = self.service.create_base("Hidden base", agent_enabled=False)
        hidden_path = self.root / "hidden.md"
        hidden_path.write_text("query hidden evidence", encoding="utf-8")
        self.service.import_document(hidden_base["id"], hidden_path)
        agent_result = LocalKnowledgeClient(self.service).search({"query": "query", "topK": 2})
        self.assertTrue(agent_result["items"])
        self.assertTrue(all(item["kbId"] == base["id"] for item in agent_result["items"]))

    def test_ready_index_revision_is_required_for_lexical_and_dense_hits(self) -> None:
        base = self._base()
        source = self.root / "revision.md"
        source.write_text("query revision evidence", encoding="utf-8")
        document = self.service.import_document(base["id"], source)
        self.service.store.update_document(document["documentId"], {"indexed_config_revision": 0})

        lexical = self.service.search("query", base_ids=(base["id"],), mode="lexical", limit=1)
        dense = self.service.search("query", base_ids=(base["id"],), mode="dense", limit=1)

        self.assertEqual([], lexical["hits"])
        self.assertEqual([], dense["hits"])

    def test_usearch_falls_back_to_ready_exact_scan_without_double_embedding(self) -> None:
        provider = _CountingFreshnessEmbedding()
        index = USearchDenseIndex(
            self.service.config.database_path,
            provider,
            index_factory=lambda **_kwargs: object(),
            array_factory=lambda values, _dtype: values,
        )
        self.service.dense_index = index
        base = self._base()
        stale_path = self.root / "ann-stale.md"
        ready_path = self.root / "ann-ready.md"
        stale_path.write_text("stale target", encoding="utf-8")
        ready_path.write_text("ready evidence", encoding="utf-8")

        with mock.patch.object(index, "rebuild_base", return_value=None):
            stale = self.service.import_document(base["id"], stale_path)
            ready = self.service.import_document(base["id"], ready_path)
        self.service.store.update_document(stale["documentId"], {"status": "stale"})

        with mock.patch.object(index, "rebuild_base", return_value=None):
            hits = index.search("query", base_ids=(base["id"],), limit=1)

        ready_chunk = self.service.store.all(
            "SELECT id FROM knowledge_chunks WHERE document_id=?",
            (ready["documentId"],),
        )[0]["id"]
        self.assertEqual(ready_chunk, hits[0][0])
        self.assertEqual(1, provider.query_calls)
        self.assertEqual(1, index.status()["readyFilterFallbackCount"])

    def test_rerank_threshold_uses_final_score_and_preserves_candidate_window(self) -> None:
        reranker = _ScoreMapReranker({})
        service = KnowledgeLibraryService(
            KnowledgeLibraryConfig(self.root / "RerankKnowledge"),
            reranker=reranker,
        )
        service.dense_index = SqliteDenseIndex(service.config.database_path, _FreshnessEmbedding())
        self.addCleanup(service.close)
        base = service.create_base(
            "Rerank threshold",
            agent_enabled=True,
            retrieval_config={
                "mode": "lexical",
                "topK": 1,
                "threshold": 0.8,
                "rerankEnabled": True,
                "rerankCandidateDepth": 2,
            },
        )
        strong_path = self.root / "strong.md"
        weak_path = self.root / "weak.md"
        strong_path.write_text(("query " * 12) + "strong", encoding="utf-8")
        weak_path.write_text("query weak", encoding="utf-8")
        strong = service.import_document(base["id"], strong_path)
        weak = service.import_document(base["id"], weak_path)
        weak_chunk = service.store.all(
            "SELECT id FROM knowledge_chunks WHERE document_id=?",
            (weak["documentId"],),
        )[0]["id"]
        strong_chunk = service.store.all(
            "SELECT id FROM knowledge_chunks WHERE document_id=?",
            (strong["documentId"],),
        )[0]["id"]
        reranker.scores = {str(weak_chunk): 0.95, str(strong_chunk): 0.1}

        result = service.search("query", base_ids=(base["id"],), limit=1)

        self.assertEqual([weak["documentId"]], [item["documentId"] for item in result["hits"]])
        self.assertEqual(2, len(reranker.calls[0]["ids"]))
        self.assertEqual("rerank", result["retrieval"]["libraries"][0]["thresholdStage"])
        self.assertLess(result["hits"][0]["diagnostics"]["retrievalScore"], 0.8)

        low_reranker = _ScoreMapReranker({str(weak_chunk): 0.1, str(strong_chunk): 0.1})
        service.reranker = low_reranker
        filtered = service.search("query", base_ids=(base["id"],), limit=1)
        self.assertEqual([], filtered["hits"])


if __name__ == "__main__":
    unittest.main()
