from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_projection_consistency import (
    authoritative_retrieval_doc,
    authoritative_retrieval_docs,
    invalidate_superseded_atom_dependencies,
    restore_dependency_invalidation,
)
from rag_ime.retrieval_docs import rebuild_retrieval_docs


class MemoryProjectionConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-projection-consistency-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        LocalSqliteCoreClient(self.db_path).initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def test_bounded_pack_authority_queries_are_batched(self) -> None:
        from rag_ime.hybrid_rag_models import HybridRagQuery
        from rag_ime.hybrid_rag_retriever import _active_docs

        with closing(self.connect()) as conn:
            for index in range(100):
                self._insert_atom(conn, f"batch:{index}", f"public fact {index}", 10)
            rebuild_retrieval_docs(conn, project="test")
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            docs = _active_docs(conn, query=HybridRagQuery(query_text="public", project="test"),
                                source_ids=tuple(f"batch:{index}" for index in range(64)))
            conn.set_trace_callback(None)
            self.assertEqual(len(docs), 64)
            authority = [sql for sql in statements if "FROM memory_atoms" in sql or "FROM memory_source_generations" in sql]
            self.assertLessEqual(len(authority), 2, authority)

    def test_batch_preserves_duplicates_revisions_and_caller_transaction(self) -> None:
        with closing(self.connect()) as conn:
            self._insert_atom(conn, "batch:current", "public fact", 10)
            rebuild_retrieval_docs(conn, project="test")
            doc = dict(conn.execute("SELECT * FROM memory_retrieval_docs WHERE source_id = 'batch:current'").fetchone())
            conn.commit()
            inputs = [doc, {**doc, "source_id": "absent"}, {**doc, "projection_version": 2},
                      {**doc, "source_revision": 999}, {**doc, "source_revision": "bad"}, doc]
            self.assertEqual(authoritative_retrieval_docs(conn, inputs), [True, False, False, False, False, True])
            conn.execute("BEGIN")
            conn.execute("UPDATE memory_atoms SET status = 'forgotten' WHERE id = 'batch:current'")
            self.assertEqual(authoritative_retrieval_docs(conn, [doc]), [False])
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            self.assertEqual(authoritative_retrieval_docs(conn, [doc]), [True])

    def test_batch_book_and_phrase_dependencies_invalidate_without_projection_rebuild(self) -> None:
        with closing(self.connect()) as conn:
            self._insert_atom(conn, "atom:old", "public supporting fact", 10)
            self._insert_atom(conn, "atom:other", "second public support", 10)
            self._insert_book(conn, "book:batch", "active")
            conn.execute("INSERT INTO memory_items(memory_id, kind, text, normalized_text, summary, project, status, privacy_class, created_at_ms, updated_at_ms, metadata_json) VALUES ('phrase:batch','phrase','public phrase','public phrase','support','test','approved','local',10,10,'{\"sourceEventIds\":[10]}')")
            rebuild_retrieval_docs(conn, project="test")
            docs = [dict(row) for row in conn.execute("SELECT * FROM memory_retrieval_docs WHERE source_id IN ('book:batch','phrase:batch') ORDER BY doc_type").fetchall()]
            self.assertEqual([doc["doc_type"] for doc in docs], ["book", "phrase"])
            self.assertEqual(authoritative_retrieval_docs(conn, docs), [True, True])
            conn.execute("UPDATE memory_atoms SET privacy_level = 'sensitive' WHERE id = 'atom:old'")
            self.assertEqual(authoritative_retrieval_docs(conn, docs), [False, True])
            conn.execute("UPDATE memory_atoms SET claim_state = 'superseded' WHERE id = 'atom:other'")
            self.assertEqual(authoritative_retrieval_docs(conn, docs), [False, False])
            conn.execute("UPDATE memory_atoms SET privacy_level = 'local' WHERE id = 'atom:old'")
            conn.execute("UPDATE memory_source_generations SET generation = generation + 1 WHERE source_type = 'atom' AND source_id = 'atom:old'")
            self.assertEqual(authoritative_retrieval_docs(conn, docs), [True, False])
            conn.execute("UPDATE memory_books SET metadata_json = '{\"retrievalStale\":true}' WHERE book_id = 'book:batch'")
            self.assertEqual(authoritative_retrieval_docs(conn, docs), [False, False])

    def test_in_place_revision_keeps_group_membership_and_can_restore_book(self) -> None:
        with closing(self.connect()) as conn:
            self._insert_atom(conn, "atom:old", "默认直接显示时间", 10)
            self._insert_book(conn, "book:current", "active")
            conn.execute("INSERT INTO memory_semantic_groups(group_id, title, project, status, created_at_ms, updated_at_ms) VALUES ('group:report', '报告', 'test', 'active', 1, 1)")
            conn.execute("INSERT INTO memory_semantic_group_members(group_id, member_type, member_id, weight, source, updated_at_ms) VALUES ('group:report', 'atom', 'atom:old', 0.9, 'test', 1)")
            before = tuple(conn.execute("SELECT * FROM memory_semantic_group_members").fetchone())
            old_summary = conn.execute("SELECT summary FROM memory_books WHERE book_id = 'book:current'").fetchone()[0]
            conn.execute("UPDATE memory_atoms SET canonical_text = '展开后显示时间', source_event_ids_json = '[20]' WHERE id = 'atom:old'")
            receipt = invalidate_superseded_atom_dependencies(
                conn, ["atom:old"], new_atom_id="atom:old", timestamp=30)
            self.assertEqual(tuple(conn.execute("SELECT * FROM memory_semantic_group_members").fetchone()), before)
            book = conn.execute("SELECT summary, source_event_ids_json FROM memory_books WHERE book_id = 'book:current'").fetchone()
            self.assertEqual(book[0], "展开后显示时间")
            self.assertEqual(json.loads(book[1]), [20])
            restore_dependency_invalidation(conn, receipt)
            self.assertEqual(tuple(conn.execute("SELECT * FROM memory_semantic_group_members").fetchone()), before)
            self.assertEqual(conn.execute("SELECT summary FROM memory_books WHERE book_id = 'book:current'").fetchone()[0], old_summary)

    def test_supersession_invalidates_docs_rebuilds_current_book_and_moves_group(self) -> None:
        with closing(self.connect()) as conn:
            self._insert_atom(conn, "atom:old", "公司 VPN 账号是 account-A", 10)
            self._insert_atom(conn, "atom:new", "公司 VPN 账号是 account-B", 20)
            self._insert_book(conn, "book:current", "active")
            self._insert_book(conn, "book:archive", "archived")
            conn.execute(
                """
                INSERT INTO memory_items(
                    memory_id, kind, text, normalized_text, summary, project,
                    status, privacy_class, created_at_ms, updated_at_ms,
                    metadata_json
                ) VALUES (
                    'phrase:account-a', 'phrase', 'account-A', 'account-a',
                    '旧 VPN 账号', 'test', 'approved', 'local', 10, 10,
                    '{"source":"memory_book_compile","sourceEventIds":[10]}'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO memory_semantic_groups(
                    group_id, title, project, status, created_at_ms, updated_at_ms
                ) VALUES ('group:vpn', 'VPN', 'test', 'active', 1, 1)
                """
            )
            conn.execute(
                """
                INSERT INTO memory_semantic_group_members(
                    group_id, member_type, member_id, weight, source, updated_at_ms
                ) VALUES ('group:vpn', 'atom', 'atom:old', 0.9, 'test', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO memory_semantic_group_members(
                    group_id, member_type, member_id, weight, source, updated_at_ms
                ) VALUES ('group:vpn', 'phrase', 'phrase:account-a', 0.8, 'test', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO candidate_feedback(
                    created_at_ms, query_hash, candidate_text, source_type,
                    memory_id, action, project, metadata_json
                ) VALUES (10, 'query:vpn', 'account-A', 'phrase',
                          'phrase:account-a', 'accepted', 'test', '{}')
                """
            )
            rebuild_retrieval_docs(conn, project="test")
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_projection_dependencies
                    WHERE source_type = 'atom' AND source_id = 'atom:old'
                      AND dependent_type = 'phrase'
                      AND dependent_id = 'phrase:account-a'
                    """
                ).fetchone()[0],
                1,
            )
            old_book_revision = int(
                conn.execute(
                    """
                    SELECT source_revision FROM memory_retrieval_docs
                    WHERE doc_id = 'book:book:current'
                    """
                ).fetchone()[0]
            )
            conn.execute(
                """
                UPDATE memory_atoms
                SET status = 'superseded', claim_state = 'superseded',
                    valid_to_ms = 20, updated_at_ms = 20
                WHERE id = 'atom:old'
                """
            )
            conn.execute(
                """
                INSERT INTO memory_supersessions(
                    supersession_id, old_memory_id, new_memory_id, reason,
                    status, created_at_ms
                ) VALUES ('sup:vpn', 'atom:old', 'atom:new', 'corrected', 'active', 20)
                """
            )

            result = invalidate_superseded_atom_dependencies(
                conn,
                ["atom:old"],
                new_atom_id="atom:new",
                timestamp=30,
            )

            self.assertEqual(result["movedGroupMemberships"], 1)
            self.assertEqual(result["suppressedPhraseIds"], ["phrase:account-a"])
            current_book = conn.execute(
                "SELECT * FROM memory_books WHERE book_id = 'book:current'"
            ).fetchone()
            archived_book = conn.execute(
                "SELECT * FROM memory_books WHERE book_id = 'book:archive'"
            ).fetchone()
            self.assertEqual(
                json.loads(current_book["memory_atom_ids_json"]),
                ["atom:new"],
            )
            self.assertIn("account-B", str(current_book["summary"]))
            self.assertNotIn("account-A", str(current_book["summary"]))
            self.assertFalse(
                json.loads(current_book["metadata_json"]).get("retrievalStale", False)
            )
            self.assertTrue(
                json.loads(archived_book["metadata_json"])["retrievalStale"]
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_semantic_group_members
                    WHERE member_type = 'atom' AND member_id = 'atom:old'
                    """
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_semantic_group_members
                    WHERE group_id = 'group:vpn' AND member_type = 'atom'
                      AND member_id = 'atom:new'
                    """
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM memory_items WHERE memory_id = 'phrase:account-a'"
                ).fetchone()[0],
                "hidden",
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_semantic_group_members
                    WHERE member_type = 'phrase' AND member_id = 'phrase:account-a'
                    """
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_candidate_suppressions
                    WHERE reason = 'superseded_atom_dependency'
                      AND match_value IN ('phrase:account-a', 'account-A')
                    """
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM candidate_feedback
                    WHERE memory_id = 'phrase:account-a' AND action = 'accepted'
                    """
                ).fetchone()[0],
                1,
            )
            remaining_docs = {
                str(row[0])
                for row in conn.execute(
                    "SELECT doc_id FROM memory_retrieval_docs"
                ).fetchall()
            }
            self.assertNotIn("atom:atom:old", remaining_docs)
            self.assertNotIn("book:book:current", remaining_docs)
            self.assertNotIn("book:book:archive", remaining_docs)
            self.assertNotIn("phrase:phrase:account-a", remaining_docs)

            rebuild_retrieval_docs(conn, project="test")
            projected = conn.execute(
                """
                SELECT doc_type, source_id, source_revision, projection_version
                FROM memory_retrieval_docs
                WHERE doc_id = 'book:book:current'
                """
            ).fetchone()
            self.assertIsNotNone(projected)
            self.assertGreater(int(projected["source_revision"]), old_book_revision)
            self.assertTrue(authoritative_retrieval_doc(conn, dict(projected)))
            self.assertIsNone(
                conn.execute(
                    """
                    SELECT 1 FROM memory_retrieval_docs
                    WHERE doc_id = 'book:book:archive'
                    """
                ).fetchone()
            )

    def test_phrase_dependency_invalidation_is_rollbackable(self) -> None:
        with closing(self.connect()) as conn:
            self._insert_atom(conn, "atom:old", "公司 VPN 账号是 account-A", 10)
            conn.execute(
                """
                INSERT INTO memory_items(
                    memory_id, kind, text, normalized_text, summary, project,
                    status, privacy_class, created_at_ms, updated_at_ms,
                    metadata_json
                ) VALUES (
                    'phrase:account-a', 'phrase', 'account-A', 'account-a',
                    '旧 VPN 账号', 'test', 'approved', 'local', 10, 10,
                    '{"source":"memory_book_compile","sourceEventIds":[10]}'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO memory_semantic_groups(
                    group_id, title, project, status, created_at_ms, updated_at_ms
                ) VALUES ('group:vpn', 'VPN', 'test', 'active', 1, 1)
                """
            )
            conn.execute(
                """
                INSERT INTO memory_semantic_group_members(
                    group_id, member_type, member_id, weight, source, updated_at_ms
                ) VALUES ('group:vpn', 'phrase', 'phrase:account-a', 0.8, 'test', 1)
                """
            )
            rebuild_retrieval_docs(conn, project="test")
            conn.execute(
                """
                UPDATE memory_atoms
                SET status = 'superseded', claim_state = 'superseded',
                    valid_to_ms = 20, updated_at_ms = 20
                WHERE id = 'atom:old'
                """
            )
            invalidation = invalidate_superseded_atom_dependencies(
                conn,
                ["atom:old"],
                timestamp=30,
            )

            conn.execute(
                """
                UPDATE memory_atoms
                SET status = 'active', claim_state = 'current', valid_to_ms = NULL,
                    updated_at_ms = 40
                WHERE id = 'atom:old'
                """
            )
            restore_dependency_invalidation(conn, invalidation, timestamp=40)
            rebuild_retrieval_docs(conn, project="test")

            self.assertEqual(
                conn.execute(
                    "SELECT status FROM memory_items WHERE memory_id = 'phrase:account-a'"
                ).fetchone()[0],
                "approved",
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_candidate_suppressions
                    WHERE reason = 'superseded_atom_dependency'
                    """
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_semantic_group_members
                    WHERE member_type = 'phrase' AND member_id = 'phrase:account-a'
                    """
                ).fetchone()[0],
                1,
            )
            projected = conn.execute(
                """
                SELECT doc_type, source_id, source_revision, projection_version
                FROM memory_retrieval_docs
                WHERE doc_id = 'phrase:phrase:account-a'
                """
            ).fetchone()
            self.assertIsNotNone(projected)
            self.assertTrue(authoritative_retrieval_doc(conn, dict(projected)))

    def test_phrase_authority_gate_fails_closed_before_async_cleanup(self) -> None:
        with closing(self.connect()) as conn:
            self._insert_atom(
                conn,
                "atom:old",
                "公司 VPN 账号是 account-A",
                10,
            )
            conn.execute(
                """
                INSERT INTO memory_items(
                    memory_id, kind, text, normalized_text, summary, project,
                    status, privacy_class, created_at_ms, updated_at_ms,
                    metadata_json
                ) VALUES (
                    'phrase:account-a', 'phrase', 'account-A', 'account-a',
                    '旧 VPN 账号', 'test', 'approved', 'local', 10, 10,
                    '{"source":"memory_book_compile","sourceEventIds":[10]}'
                )
                """
            )
            rebuild_retrieval_docs(conn, project="test")
            projected = conn.execute(
                """
                SELECT doc_type, source_id, source_revision, projection_version
                FROM memory_retrieval_docs
                WHERE doc_id = 'phrase:phrase:account-a'
                """
            ).fetchone()
            self.assertTrue(authoritative_retrieval_doc(conn, dict(projected)))

            conn.execute(
                """
                UPDATE memory_atoms
                SET status = 'superseded', claim_state = 'superseded',
                    valid_to_ms = 20, updated_at_ms = 20
                WHERE id = 'atom:old'
                """
            )

            self.assertFalse(authoritative_retrieval_doc(conn, dict(projected)))

    @staticmethod
    def _insert_atom(
        conn: sqlite3.Connection,
        atom_id: str,
        text: str,
        timestamp: int,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO input_events(
                id, created_at_ms, source, committed_text, project
            ) VALUES (?, ?, 'test', ?, 'test')
            """,
            (timestamp, timestamp, text),
        )
        conn.execute(
            """
            INSERT INTO memory_atoms(
                id, kind, text, canonical_text, scope_project,
                status, created_at_ms, updated_at_ms, claim_key,
                lineage_id, claim_state, valid_from_ms
            ) VALUES (?, 'project_fact', ?, ?, 'test', 'active', ?, ?,
                      ?, 'lineage:vpn', 'current', ?)
            """,
            (atom_id, text, text, timestamp, timestamp, atom_id, timestamp),
        )
        conn.execute(
            "UPDATE memory_atoms SET source_event_ids_json = ? WHERE id = ?",
            (json.dumps([timestamp]), atom_id),
        )

    @staticmethod
    def _insert_book(
        conn: sqlite3.Connection,
        book_id: str,
        status: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO memory_books(
                book_id, book_type, book_key, title, summary, normalized_text,
                project, tags_json, surface_hints_json, query_expansions_json,
                memory_atom_ids_json, status, created_at_ms, updated_at_ms,
                metadata_json
            ) VALUES (?, 'topic', ?, 'VPN 账号', '公司 VPN 账号是 account-A',
                      'vpn account-a', 'test', '["account-A"]',
                      '["旧 VPN 账号"]', '["account-A"]', '["atom:old"]',
                      ?, 10, 10, '{}')
            """,
            (book_id, book_id, status),
        )


if __name__ == "__main__":
    unittest.main()
