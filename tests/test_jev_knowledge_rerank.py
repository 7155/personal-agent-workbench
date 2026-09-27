import unittest
from unittest.mock import patch

from rag_ime.knowledge_library.jev_rerank import JevKnowledgeReranker
from rag_ime.knowledge_library.rerank import knowledge_reranker_from_env


class JevRerankTests(unittest.TestCase):
    def test_factory_and_source_preserving_order(self):
        reranker = knowledge_reranker_from_env("/tmp", environ={"RAG_IME_KNOWLEDGE_RERANK_PROVIDER": "typesafe-jev"})
        self.assertIsInstance(reranker, JevKnowledgeReranker)
        candidates = [{"chunkId": "a", "content": "weather", "citation": "A"},
                      {"chunkId": "b", "content": "Jev key uses Keychain", "citation": "B"}]
        with patch("rag_ime.jev.api_key", return_value="fake"), patch("rag_ime.jev.evaluate", return_value={"answers": {
            "p0": {"type": "score", "score": 0, "confidence": .99},
            "p1": {"type": "score", "score": 3, "confidence": .99}}}):
            result = reranker.rerank("Where is the key stored?", candidates, limit=2)
        self.assertEqual([item["chunkId"] for item in result], ["b", "a"])
        self.assertEqual(result[0]["citation"], "B")
        self.assertEqual(result[0]["content"], candidates[1]["content"])
        self.assertEqual(result[0]["rerankOriginalRank"], 2)
        self.assertNotIn("rerankScore", candidates[0])

    def test_invalid_or_incomplete_answers_fail_without_false_rerank(self):
        for answer in [{}, {"p0": {"type": "score", "score": True, "confidence": 1}},
                       {"p0": {"type": "score", "score": 4, "confidence": 1}}]:
            reranker = JevKnowledgeReranker()
            with patch("rag_ime.jev.api_key", return_value="fake"), patch("rag_ime.jev.evaluate", return_value={"answers": answer}):
                with self.assertRaises(ValueError):
                    reranker.rerank("query", [{"chunkId": "a", "content": "text"}], limit=1)
            self.assertEqual(reranker._errors, 1)

    def test_oversized_payload_is_rejected_before_network(self):
        with patch("rag_ime.jev.evaluate") as evaluate:
            with self.assertRaises(ValueError):
                JevKnowledgeReranker().rerank("query", [{"chunkId": "a", "content": "x" * 49_000}], limit=1)
            evaluate.assert_not_called()
