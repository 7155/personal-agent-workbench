from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager, redirect_stdout
from pathlib import Path

from rag_ime.agent_memory_sources import AgentMemorySourceStore
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.cli import main
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_book_compiler import apply_memory_book_plan, memory_book_plan_from_compile_output
from rag_ime.memory_ingest import normalize_text, upsert_memory_item
from rag_ime.memory_evidence_admission import transition_evidence_admission
from rag_ime.models import InputEvent
from rag_ime.retrieval_docs import rebuild_retrieval_docs
from rag_ime.text_utils import now_ms


class RetrievalDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-retrieval-docs-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def test_retrieval_docs_include_memory_item_tags_aliases(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=now_ms(),
                source="manual",
                committed_text="连续预测",
                privacy_disposition="allowed",
                recent_context="RAG 输入法需要连续预测候选",
                project="wisdom-weasel-rag-ime",
                tags=("phrase-memory", "RAG"),
            )
        )
        event_id = int(event_ref.split(":", 1)[1])

        with self.connect() as conn:
            apply_memory_book_plan(
                conn,
                memory_book_plan_from_compile_output(
                    {
                        "phraseCandidates": [
                            {
                                "text": "连续预测",
                                "tags": ["phrase-memory", "RAG"],
                                "sourceEventIds": [event_id],
                                "weight": 0.8,
                            }
                        ]
                    },
                    project="wisdom-weasel-rag-ime",
                    provider="deepseek",
                    model="deepseek-v4-flash",
                ),
            )
            report = rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            row = conn.execute(
                "SELECT * FROM memory_retrieval_docs WHERE doc_type = 'phrase' AND source_id = 'phrase:连续预测'"
            ).fetchone()

        self.assertGreaterEqual(report["counts"]["phrase"], 1)
        self.assertIsNotNone(row)
        self.assertIn("连续预测", row["raw_text"])
        self.assertIn("phrase-memory", row["tags_text"])
        self.assertIn("RAG", row["tags_text"])
        self.assertIn("连续预测", row["surface_hints_text"])

    def test_retrieval_docs_include_memory_books(self) -> None:
        event_id = self._record_seed_event()
        plan = memory_book_plan_from_compile_output(
            sample_compile_output(event_id),
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        with self.connect() as conn:
            apply_memory_book_plan(conn, plan)
            report = rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            row = conn.execute("SELECT * FROM memory_retrieval_docs WHERE doc_type = 'book'").fetchone()

        self.assertEqual(report["counts"]["book"], 1)
        self.assertIsNotNone(row)
        self.assertIn("多路召回偏好", row["raw_text"])
        self.assertIn("VCP", row["tags_text"])
        self.assertIn("多路召回", row["surface_hints_text"])
        self.assertIn("Topic Book", row["query_expansions_text"])
        self.assertEqual(row["time_key"], "topic:rag-retrieval")
        metadata = json.loads(row["metadata_json"])
        self.assertTrue(metadata["inlineAtomsComplete"])
        self.assertEqual(metadata["memoryAtomCount"], 1)
        self.assertEqual(
            metadata["inlineAtoms"],
            [
                {
                    "atomId": "atom:vcp-style-rag-core",
                    "text": "用户偏好检索系统同时使用 BM25、向量、标签和时间召回。",
                }
            ],
        )

    def test_activity_timeline_has_its_own_retrieval_doc_type(self) -> None:
        event_id = self._record_seed_event()
        timeline_id = "activity-timeline:independent-index"
        segment = {
            "title": "验证时间线独立索引",
            "summary": "将每日活动从主题书中拆出并按时间问题召回。",
            "app": "com.openai.codex",
            "apps": ["com.openai.codex"],
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_activity_timelines(
                    timeline_id, project, timeline_date, timezone, status,
                    source_event_ids_json, source_event_hash, segments_json,
                    summary_text, event_count, segment_count, approved_by,
                    approved_at_ms, metadata_json, created_at_ms, updated_at_ms
                ) VALUES (
                    ?, 'wisdom-weasel-rag-ime', '2026-07-18', 'Asia/Shanghai',
                    'approved', ?, ?, ?, ?, 1, 1, 'system:test', 200,
                    '{"derivedArtifactType":"daily_activity_timeline"}', 100, 200
                )
                """,
                (
                    timeline_id,
                    json.dumps([event_id]),
                    "a" * 64,
                    json.dumps([segment], ensure_ascii=False),
                    "验证时间线独立索引。",
                ),
            )

            report = rebuild_retrieval_docs(
                conn,
                project="wisdom-weasel-rag-ime",
            )
            row = conn.execute(
                """
                SELECT * FROM memory_retrieval_docs
                WHERE doc_type = 'timeline' AND source_id = ?
                """,
                (timeline_id,),
            ).fetchone()

        self.assertEqual(report["counts"]["timeline"], 1)
        self.assertEqual(report["counts"]["book"], 0)
        self.assertTrue(report["includeTimelines"])
        self.assertIsNotNone(row)
        self.assertEqual(row["time_key"], "timeline:2026-07-18")
        self.assertIn("验证时间线独立索引", row["surface_hints_text"])
        self.assertEqual(
            json.loads(row["metadata_json"])["derivedArtifactType"],
            "daily_activity_timeline",
        )

    def test_retrieval_docs_exclude_raw_app_archives_and_superseded_baselines(self) -> None:
        with self.connect() as conn:
            timestamp = now_ms()
            conn.executemany(
                """
                INSERT INTO memory_books(
                    book_id, book_type, book_key, title, summary, normalized_text,
                    project, status, confidence, quality_score, created_at_ms,
                    updated_at_ms, metadata_json, archived_at_ms,
                    last_active_at_ms, archive_reason
                ) VALUES (?, ?, ?, ?, ?, ?, 'wisdom-weasel-rag-ime', 'archived',
                          1.0, 1.0, ?, ?, '{}', ?, ?, ?)
                """,
                (
                    (
                        "book:app-history",
                        "app_archive",
                        "app-history",
                        "Codex 完整输入归档",
                        "仅用于追溯",
                        "codex 完整输入归档",
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp,
                        "complete_input_history",
                    ),
                    (
                        "book:old-baseline",
                        "topic",
                        "old-baseline",
                        "旧自动主题",
                        "已被人工基线替代",
                        "旧自动主题",
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp,
                        "superseded_by_curated_baseline",
                    ),
                    (
                        "book:user-archive",
                        "topic",
                        "user-archive",
                        "用户归档主题",
                        "显式历史检索时仍可召回",
                        "用户归档主题",
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp,
                        "user-requested-archive",
                    ),
                ),
            )
            report = rebuild_retrieval_docs(
                conn,
                project="wisdom-weasel-rag-ime",
            )
            indexed = {
                str(row["source_id"])
                for row in conn.execute(
                    "SELECT source_id FROM memory_retrieval_docs WHERE doc_type = 'book'"
                ).fetchall()
            }

        self.assertEqual(report["counts"]["book"], 1)
        self.assertEqual(indexed, {"book:user-archive"})

    def test_retrieval_docs_include_surface_hints_and_query_expansions(self) -> None:
        event_id = self._record_seed_event()
        plan = memory_book_plan_from_compile_output(
            sample_compile_output(event_id),
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        with self.connect() as conn:
            apply_memory_book_plan(conn, plan)
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            row = conn.execute("SELECT * FROM memory_retrieval_docs WHERE doc_type = 'atom'").fetchone()

        self.assertIsNotNone(row)
        self.assertIn("用户偏好检索系统同时使用", row["raw_text"])
        self.assertIn("RAG core", row["tags_text"])
        self.assertIn("VCP式RAG", row["aliases_text"])
        self.assertIn("混合召回", row["surface_hints_text"])
        self.assertIn("千问三 Qwen3", row["query_expansions_text"])

    def test_secondary_projection_change_advances_source_revision(self) -> None:
        event_id = self._record_seed_event()
        plan = memory_book_plan_from_compile_output(
            sample_compile_output(event_id),
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        with self.connect() as conn:
            apply_memory_book_plan(conn, plan)
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            before = conn.execute(
                """
                SELECT doc_id, source_revision, projection_version
                FROM memory_retrieval_docs
                WHERE doc_type = 'atom'
                """
            ).fetchone()
            conn.execute(
                """
                INSERT INTO memory_retrieval_doc_vectors(
                    doc_id, provider_fingerprint, raw_vector_json,
                    tag_vector_json, group_vector_json, dimensions,
                    source_revision, projection_version, built_at_ms,
                    updated_at_ms
                ) VALUES (?, 'test:old', '[1,0]', '[1,0]', '[]', 2, ?, ?, 1, 1)
                """,
                (
                    str(before["doc_id"]),
                    int(before["source_revision"]),
                    int(before["projection_version"]),
                ),
            )
            conn.execute(
                """
                UPDATE memory_aliases
                SET alias = '新版 VCP RAG'
                WHERE memory_atom_id = 'atom:vcp-style-rag-core'
                  AND alias_type = 'alias'
                """
            )

            report = rebuild_retrieval_docs(
                conn,
                project="wisdom-weasel-rag-ime",
            )
            after = conn.execute(
                """
                SELECT source_revision, aliases_text
                FROM memory_retrieval_docs
                WHERE doc_id = ?
                """,
                (str(before["doc_id"]),),
            ).fetchone()

        self.assertIn(str(before["doc_id"]), report["changedDocIds"])
        self.assertGreater(
            int(after["source_revision"]),
            int(before["source_revision"]),
        )
        self.assertIn("新版 VCP RAG", str(after["aliases_text"]))
        with self.connect() as conn:
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_retrieval_doc_vectors
                    WHERE doc_id = ?
                    """,
                    (str(before["doc_id"]),),
                ).fetchone()[0],
                0,
            )

    def test_retrieval_docs_exclude_tombstoned_and_sensitive(self) -> None:
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=now_ms(),
                source="manual",
                committed_text="Bearer sk-secret-value",
                privacy_disposition="allowed",
                recent_context="敏感信息",
                project="wisdom-weasel-rag-ime",
            )
        )
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=now_ms(),
                source="manual",
                committed_text="噪声短语",
                privacy_disposition="allowed",
                recent_context="应该被 tombstone 排除",
                project="wisdom-weasel-rag-ime",
                tags=("phrase-memory",),
            )
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_tombstones(created_at_ms, target_type, target_value, reason, active, metadata_json)
                VALUES (?, 'memory_id', 'phrase:噪声短语', 'test', 1, '{}')
                """,
                (now_ms(),),
            )
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            texts = [row["raw_text"] for row in conn.execute("SELECT raw_text FROM memory_retrieval_docs").fetchall()]

        self.assertFalse(any("sk-secret-value" in text for text in texts))
        self.assertFalse(any("噪声短语" in text for text in texts))

    def test_retrieval_docs_remove_all_projections_for_forgotten_source_event(self) -> None:
        event_id = self._record_seed_event()
        plan = memory_book_plan_from_compile_output(
            sample_compile_output(event_id),
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )

        def linked_doc_types(conn: sqlite3.Connection) -> set[str]:
            result: set[str] = set()
            for row in conn.execute(
                "SELECT doc_type, metadata_json FROM memory_retrieval_docs"
            ).fetchall():
                metadata = json.loads(str(row["metadata_json"] or "{}"))
                raw_ids = list(metadata.get("sourceEventIds") or [])
                raw_ids.append(metadata.get("sourceEventId"))
                source_ids: set[int] = set()
                for value in raw_ids:
                    try:
                        parsed = int(value)
                    except (TypeError, ValueError):
                        continue
                    if parsed > 0:
                        source_ids.add(parsed)
                if event_id in source_ids:
                    result.add(str(row["doc_type"]))
            return result

        with self.connect() as conn:
            apply_memory_book_plan(conn, plan)
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            self.assertTrue(
                {"phrase", "atom", "book"}.issubset(linked_doc_types(conn))
            )

            for target_type, target_value in (
                ("source_event_id", str(event_id)),
                ("memory_id", f"event:{event_id}"),
            ):
                with self.subTest(target_type=target_type):
                    cursor = conn.execute(
                        """
                        INSERT INTO memory_tombstones(
                            created_at_ms, target_type, target_value, reason,
                            active, metadata_json
                        ) VALUES (?, ?, ?, 'test-forget-source', 1, '{}')
                        """,
                        (now_ms(), target_type, target_value),
                    )
                    tombstone_id = int(cursor.lastrowid)
                    rebuild_retrieval_docs(
                        conn,
                        project="wisdom-weasel-rag-ime",
                    )
                    self.assertEqual(linked_doc_types(conn), set())

                    conn.execute(
                        "UPDATE memory_tombstones SET active = 0 WHERE id = ?",
                        (tombstone_id,),
                    )
                    rebuild_retrieval_docs(
                        conn,
                        project="wisdom-weasel-rag-ime",
                    )
                    self.assertTrue(
                        {"phrase", "atom", "book"}.issubset(
                            linked_doc_types(conn)
                        )
                    )

    def test_retrieval_docs_do_not_publish_governed_excluded_evidence(self) -> None:
        sessions = AgentSessionStore(self.db_path)
        session = sessions.create(title="projection-governance", created_at_ms=1)
        sources = AgentMemorySourceStore(self.db_path, project="wisdom-weasel-rag-ime")
        sources.checkpoint_user_message(
            session_id=str(session["id"]),
            pi_entry_id="entry:excluded",
            turn_id="turn:excluded",
            text="候选数据库必须先完成验证再激活。",
            created_at_ms=1_700_000_000_000,
        )
        with self.connect() as conn:
            event_id = int(
                conn.execute(
                    """SELECT id FROM input_events
                       WHERE committed_text = '候选数据库必须先完成验证再激活。'"""
                ).fetchone()[0]
            )
            apply_memory_book_plan(
                conn,
                memory_book_plan_from_compile_output(
                    sample_compile_output(event_id),
                    project="wisdom-weasel-rag-ime",
                    provider="deepseek",
                    model="deepseek-v4-flash",
                ),
            )
            conn.execute(
                """UPDATE agent_memory_sources
                   SET disposition = 'not_for_memory',
                       disposition_reason = 'manual_review_excluded'
                   WHERE input_event_id = ?""",
                (event_id,),
            )

            report = rebuild_retrieval_docs(
                conn,
                project="wisdom-weasel-rag-ime",
            )
            linked = {
                str(row[0])
                for row in conn.execute(
                    """SELECT doc_type FROM memory_retrieval_docs
                       WHERE json_extract(metadata_json, '$.sourceEventId') = ?
                          OR metadata_json LIKE ?""",
                    (event_id, f"%{event_id}%"),
                ).fetchall()
            }

        self.assertEqual(linked, set())
        self.assertEqual(report["counts"]["phrase"], 0)
        self.assertEqual(report["counts"]["atom"], 0)
        self.assertEqual(report["counts"]["book"], 0)

    def test_hidden_raw_event_cannot_keep_personal_derived_artifacts_alive(self) -> None:
        sessions = AgentSessionStore(self.db_path)
        session = sessions.create(title="projection-remember", created_at_ms=1)
        sources = AgentMemorySourceStore(self.db_path, project="wisdom-weasel-rag-ime")
        sources.checkpoint_user_message(
            session_id=str(session["id"]),
            pi_entry_id="entry:remembered",
            turn_id="turn:remembered",
            text="整理后的记忆应保留可追溯证据。",
            created_at_ms=1_700_000_000_000,
        )
        with self.connect() as conn:
            event_id = int(
                conn.execute(
                    """SELECT id FROM input_events
                       WHERE committed_text = '整理后的记忆应保留可追溯证据。'"""
                ).fetchone()[0]
            )
            apply_memory_book_plan(
                conn,
                memory_book_plan_from_compile_output(
                    sample_compile_output(event_id),
                    project="wisdom-weasel-rag-ime",
                    provider="deepseek",
                    model="deepseek-v4-flash",
                ),
            )
            conn.execute(
                """UPDATE agent_memory_sources
                   SET disposition = 'remember',
                       disposition_reason = 'manual_review_remembered'
                   WHERE input_event_id = ?""",
                (event_id,),
            )
            # Hiding the raw row prevents direct raw-history recall; it does
            # not revoke the reviewed artifacts derived from that evidence.
            conn.execute(
                "UPDATE memory_state SET deleted = 1 WHERE event_id = ?",
                (event_id,),
            )

            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            projected = {
                (str(row[0]), str(row[1]))
                for row in conn.execute(
                    """SELECT doc_type, source_id FROM memory_retrieval_docs
                       WHERE status = 'active'"""
                ).fetchall()
            }

        self.assertIn(("phrase", "phrase:多路召回"), projected)
        self.assertNotIn(("atom", "atom:vcp-style-rag-core"), projected)
        self.assertFalse(any(kind == "book" for kind, _ in projected))

    def test_retrieval_docs_rebuild_is_idempotent(self) -> None:
        self._record_seed_event()

        with self.connect() as conn:
            first = rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            first_rows = conn.execute("SELECT COUNT(*) FROM memory_retrieval_docs").fetchone()[0]
            second = rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            second_rows = conn.execute("SELECT COUNT(*) FROM memory_retrieval_docs").fetchone()[0]

        self.assertEqual(first["docCount"], second["docCount"])
        self.assertEqual(first_rows, second_rows)

    def test_default_rebuild_prunes_legacy_item_projection_but_keeps_phrase_and_source(self) -> None:
        with self.connect() as conn:
            self._insert_memory_item(
                conn,
                memory_id="stable:legacy-policy",
                kind="stable_memory",
                text="旧 stable memory 仍保留作治理来源",
            )
            self._insert_memory_item(
                conn,
                memory_id="phrase:保留候选",
                kind="phrase",
                text="保留候选",
            )
            compatibility = rebuild_retrieval_docs(
                conn,
                project="wisdom-weasel-rag-ime",
                include_items=True,
            )
            legacy_doc_id = "item:stable:legacy-policy"
            legacy_rowid = int(
                conn.execute(
                    "SELECT rowid FROM memory_retrieval_docs WHERE doc_id = ?",
                    (legacy_doc_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO memory_retrieval_doc_vectors(
                    doc_id, provider_fingerprint, raw_vector_json,
                    tag_vector_json, group_vector_json, dimensions, updated_at_ms
                ) VALUES (?, 'test-provider', '[]', '[]', '[]', 0, ?)
                """,
                (legacy_doc_id, now_ms()),
            )
            source_before = tuple(
                conn.execute(
                    """
                    SELECT kind, text, status, privacy_class
                    FROM memory_items WHERE memory_id = 'stable:legacy-policy'
                    """
                ).fetchone()
            )

            report = rebuild_retrieval_docs(
                conn,
                project="wisdom-weasel-rag-ime",
            )
            source_after = tuple(
                conn.execute(
                    """
                    SELECT kind, text, status, privacy_class
                    FROM memory_items WHERE memory_id = 'stable:legacy-policy'
                    """
                ).fetchone()
            )
            remaining_types = {
                str(row[0])
                for row in conn.execute(
                    "SELECT doc_type FROM memory_retrieval_docs"
                ).fetchall()
            }
            legacy_vector_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_retrieval_doc_vectors WHERE doc_id = ?",
                    (legacy_doc_id,),
                ).fetchone()[0]
            )
            legacy_fts_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_retrieval_docs_fts WHERE rowid = ?",
                    (legacy_rowid,),
                ).fetchone()[0]
            )

        self.assertTrue(compatibility["includeLegacyItems"])
        self.assertIn(legacy_doc_id, report["removedDocIds"])
        self.assertTrue(report["includePhrases"])
        self.assertFalse(report["includeLegacyItems"])
        self.assertEqual(remaining_types, {"phrase"})
        self.assertEqual(legacy_vector_count, 0)
        self.assertEqual(legacy_fts_count, 0)
        self.assertEqual(source_after, source_before)

    def test_project_rebuild_preserves_other_project_fts_rows(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_retrieval_docs(
                    doc_id, doc_type, source_id, raw_text, tags_text, aliases_text,
                    surface_hints_text, query_expansions_text, time_key, project, app,
                    owner_kind, owner_id, status, updated_at_ms, metadata_json
                ) VALUES (
                    'phrase:other-project', 'phrase', 'phrase:other-project',
                    '另一个项目的专属记忆', '', '', '专属记忆', '', '',
                    'other-project', '', 'user', 'default', 'active', ?, '{}'
                )
                """,
                (now_ms(),),
            )
            rowid = int(
                conn.execute(
                    "SELECT rowid FROM memory_retrieval_docs WHERE doc_id = 'phrase:other-project'"
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO memory_retrieval_docs_fts(
                    rowid, raw_text, tags_text, aliases_text, surface_hints_text,
                    query_expansions_text, time_key, project, app
                ) VALUES (?, '另一个项目的专属记忆', '', '', '专属记忆', '', '', 'other-project', '')
                """,
                (rowid,),
            )

            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")

            normalized = conn.execute(
                "SELECT COUNT(*) FROM memory_retrieval_docs WHERE doc_id = 'phrase:other-project'"
            ).fetchone()[0]
            indexed = conn.execute(
                "SELECT COUNT(*) FROM memory_retrieval_docs_fts WHERE rowid = ?",
                (rowid,),
            ).fetchone()[0]

        self.assertEqual(normalized, 1)
        self.assertEqual(indexed, 1)

    def test_retrieval_docs_never_publish_raw_event_items(self) -> None:
        self._record_seed_event()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE memory_items
                SET status = 'active'
                WHERE kind = 'raw_event'
                """
            )
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            raw_docs = conn.execute(
                """
                SELECT COUNT(*)
                FROM memory_retrieval_docs
                WHERE doc_type = 'item'
                  AND json_extract(metadata_json, '$.kind') = 'raw_event'
                """
            ).fetchone()[0]

        self.assertEqual(raw_docs, 0)

    def test_rebuild_retrieval_docs_cli(self) -> None:
        self._record_seed_event()
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            code = main(["--db-path", str(self.db_path), "rebuild-retrieval-docs", "--project", "wisdom-weasel-rag-ime"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["schemaVersion"], "rag-ime.retrieval-docs-rebuild.v1")
        self.assertGreaterEqual(payload["docCount"], 1)

    def _record_seed_event(self) -> int:
        text = "我偏好检索系统同时使用 BM25、向量、标签和时间四路召回。"
        timestamp = now_ms()
        # Tag numeric identifiers so they cannot resemble a payment-card value.
        capture_id = f"capture:retrieval-docs:t{timestamp}"
        metadata = {
            "schemaVersion": "rag-ime.input-capture.v2",
            "captureId": capture_id,
            "transactionId": f"transaction:retrieval-docs:t{timestamp}",
            "sequence": 1,
            "channel": "input_method",
            "boundaryKind": "host_return",
            "boundaryConfidence": "strong",
            "nativeCompositionBefore": False,
            "rimeHandled": False,
            "hostForwarded": True,
            "modifiedReturn": False,
            "finalCommitted": True,
            "controllerEpoch": 1,
            "focusEpoch": 1,
            "appBundleId": "com.apple.TextEdit",
            "fieldIdentitySha256": hashlib.sha256(
                b"field:retrieval-docs"
            ).hexdigest(),
            "privacyRevision": "foreground-privacy.v1",
            "occurredStartMs": timestamp,
            "occurredEndMs": timestamp + 1,
            "contentSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "captureSource": "text_input_client",
            "fallbackReason": "",
            "fieldContextChars": len(text),
            "imeBufferChars": len(text),
            "selectionRule": "final_committed_segment",
        }
        memory_id, receipt = self.core.record_event_with_capture_receipt(
            InputEvent(
                event_id=None,
                created_at_ms=timestamp,
                source="squirrel_input_segment",
                committed_text=text,
                privacy_disposition="allowed",
                recent_context="BM25 向量 TagMemo Time DeepSeek",
                project="wisdom-weasel-rag-ime",
                tags=("RAG", "输入法"),
                app="com.apple.TextEdit",
                capture_metadata=metadata,
            )
        )
        event_id = int(memory_id.split(":", 1)[1])
        with self.connect() as conn:
            transition_evidence_admission(
                conn,
                str(receipt["evidenceId"]),
                new_state="admitted",
                reason_code="luna_personal_memory_confirmed",
                actor_kind="luna",
                created_at_ms=timestamp + 2,
            )
            apply_memory_book_plan(
                conn,
                memory_book_plan_from_compile_output(
                    {
                        "phraseCandidates": [
                            {
                                "text": "多路召回",
                                "tags": ["RAG", "输入法"],
                                "sourceEventIds": [event_id],
                                "weight": 0.8,
                            }
                        ]
                    },
                    project="wisdom-weasel-rag-ime",
                    provider="deepseek",
                    model="deepseek-v4-flash",
                ),
            )
        return event_id

    def _insert_memory_item(
        self,
        conn: sqlite3.Connection,
        *,
        memory_id: str,
        kind: str,
        text: str,
    ) -> None:
        timestamp = now_ms()
        upsert_memory_item(
            conn,
            memory_id=memory_id,
            kind=kind,
            text=text,
            normalized_text=normalize_text(text),
            summary="",
            source_event_id=None,
            project="wisdom-weasel-rag-ime",
            app="",
            confidence=0.9,
            quality_score=0.9,
            status="approved",
            privacy_class="normal",
            created_at_ms=timestamp,
            updated_at_ms=timestamp,
            metadata={},
            tags=("RAG",),
            embedding_provider=None,
            tag_source="manual",
        )


def sample_compile_output(event_id: int) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.memory-book-compile.v1",
        "topicBooks": [
            {
                "bookKey": "rag-retrieval",
                "title": "多路召回偏好",
                "summary": "用户长期偏好同时使用 BM25、向量、标签和时间召回。",
                "tags": ["RAG", "输入法", "VCP"],
                "surfaceHints": ["多路召回", "TagMemo"],
                "queryExpansions": ["VCP RAG", "Topic Book"],
                "sourceEventIds": [event_id],
                "memoryAtomIds": ["atom:vcp-style-rag-core"],
                "confidence": 0.86,
            }
        ],
        "memoryAtoms": [
            {
                "atomId": "atom:vcp-style-rag-core",
                "kind": "durable_preference",
                "claimKey": "preference:retrieval.multichannel",
                "canonicalText": "用户偏好检索系统同时使用 BM25、向量、标签和时间召回。",
                "summary": "这是跨项目复用的长期检索偏好。",
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
            {"text": "多路召回", "tags": ["RAG", "检索"], "sourceEventIds": [event_id], "weight": 0.74}
        ],
        "warnings": [],
    }


if __name__ == "__main__":
    unittest.main()
