from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_book_compiler import apply_memory_book_plan, memory_book_plan_from_compile_output
from rag_ime.models import InputEvent
from rag_ime.rag_core_v3 import memory_candidates_v2_to_input_suggestions
from rag_ime.retrieval_docs import rebuild_retrieval_docs
from rag_ime.text_utils import now_ms


class RagCoreV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-rag-core-v3-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hybrid_core_output_converts_to_input_suggestion(self) -> None:
        self._record_event("多路召回", recent_context="RAG 输入法", tags=("RAG", "检索"))

        candidates = self.core.retrieve_candidates_v3(
            current_input="多路召回",
            project="wisdom-weasel-rag-ime",
            top_k=3,
        )
        suggestions = memory_candidates_v2_to_input_suggestions(candidates)

        self.assertEqual(candidates[0].text, "多路召回")
        self.assertEqual(candidates[0].diagnostics["schemaVersion"], "rag-ime.rag-core-v3.v1")
        self.assertEqual(suggestions[0].surface_text, "多路召回")
        self.assertEqual(suggestions[0].metadata["rag_core"], "v3")

    def test_hybrid_core_surfaces_timeline_task_not_raw_summary(self) -> None:
        event_id = self._record_event("RAG 输入法多路召回方案", tags=("RAG",))
        with self.core._connect() as conn:
            timestamp = now_ms()
            conn.execute(
                """
                INSERT INTO daily_activity_timelines(
                    timeline_id, project, timeline_date, timezone, status,
                    source_event_ids_json, source_event_hash, segments_json,
                    summary_text, event_count, segment_count, approved_book_id,
                    approved_by, approved_at_ms, metadata_json,
                    created_at_ms, updated_at_ms
                ) VALUES (
                    'activity-timeline:rag-core-v3', 'wisdom-weasel-rag-ime',
                    '2026-07-06', 'Asia/Shanghai', 'approved', ?, 'eval:rag-core-v3',
                    ?, ?, 1, 1, '', 'test:auto', ?,
                    '{"derivedArtifactType":"daily_activity_timeline"}', ?, ?
                )
                """,
                (
                    json.dumps([event_id]),
                    json.dumps(
                        [
                            {
                                "title": "多路召回",
                                "summary": "当天继续完善 RAG 输入法。",
                                "app": "com.openai.codex",
                                "apps": ["com.openai.codex"],
                                "sourceEventIds": [event_id],
                            }
                        ],
                        ensure_ascii=False,
                    ),
                    "用户希望结合 BM25、向量、TagMemo 和 Time。",
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")

        candidates = self.core.retrieve_candidates_v3(
            current_input="Daily Book",
            project="wisdom-weasel-rag-ime",
            top_k=3,
        )
        texts = [item.text for item in candidates]

        self.assertIn("多路召回", texts)
        self.assertNotIn("RAG 输入法多路召回方案", texts)
        self.assertNotIn("用户希望结合 BM25、向量、TagMemo 和 Time。", texts)
        self.assertTrue(all("用户希望借鉴" not in text for text in texts))

    def _record_event(self, text: str, *, recent_context: str = "", tags: tuple[str, ...] = ()) -> int:
        memory_id = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=now_ms(),
                source="manual",
                committed_text=text,
                privacy_disposition="allowed",
                recent_context=recent_context,
                project="wisdom-weasel-rag-ime",
                tags=tags,
            )
        )
        event_id = int(memory_id.split(":", 1)[1])
        if 2 <= len(text) <= 18:
            with self.core._connect() as conn:
                apply_memory_book_plan(
                    conn,
                    memory_book_plan_from_compile_output(
                        {
                            "phraseCandidates": [
                                {"text": text, "tags": list(tags), "sourceEventIds": [event_id], "weight": 0.8}
                            ]
                        },
                        project="wisdom-weasel-rag-ime",
                        provider="deepseek",
                        model="deepseek-v4-flash",
                    ),
                )
                rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
        return event_id

if __name__ == "__main__":
    unittest.main()
