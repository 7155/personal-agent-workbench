from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from rag_ime.db import apply_database_migrations
from rag_ime.personal_profile import read_personal_profile, save_personal_profile
from rag_ime.retrieval_docs import rebuild_retrieval_docs
from rag_ime.session_memory_recall import SessionMemoryRecallBuilder
from rag_ime.text_utils import now_ms


class PrimaryRecallRevalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        apply_database_migrations(self.conn)
        self.addCleanup(self.conn.close)
        result = save_personal_profile(self.conn, {
            "expectedRevision": read_personal_profile(self.conn)["revision"],
            "clientRequestId": "profile:fixture", "paragraphs": [{"id": None, "memoryIds": [],
                                                                       "text": "我偏好简短中文回复。"}],
        }, timestamp=100)
        self.card = result["profile"]["paragraphs"][0]
        # This is the existing retrieval-visible personal scope. The fixed
        # profile also accepts canonical personal_memory cards directly.
        self.conn.execute("UPDATE memory_atoms SET knowledge_domain='user_profile_preference' WHERE id=?", (self.card["id"],))
        rebuild_retrieval_docs(self.conn, preverified_schema=True)
        self.generated = now_ms() + 1
        self.item = {"sourceType": "memory_atom", "sourceId": self.card["id"], "text": self.card["text"]}
        self.payload = {"items": [self.item], "generatedAtMs": self.generated,
                        "query": {"preview": "回复偏好"}}
        self.builder = SessionMemoryRecallBuilder("unused.sqlite")

        @contextmanager
        def connection():
            yield self.conn

        self.builder._connect = connection

    def validate(self):
        with patch("rag_ime.session_memory_recall.retrieve_hybrid_rag_candidates",
                   side_effect=AssertionError("Revalidation must not search, embed or call a model")):
            return self.builder.revalidate_items("session:primary", self.payload)

    def test_valid_source_is_kept_and_source_revocation_removes_same_cached_fact(self) -> None:
        self.assertEqual(self.validate(), [self.item])
        self.conn.execute("UPDATE agent_memory_evidence SET admission_state='forgotten'")
        self.assertEqual(self.validate(), [])
        self.assertEqual(self.conn.execute("SELECT canonical_text FROM memory_atoms WHERE id=?", (self.card["id"],)).fetchone()[0], self.card["text"])

    def test_content_edit_after_pack_and_same_timestamp_edit_are_both_rejected(self) -> None:
        self.assertEqual(self.validate(), [self.item])
        self.conn.execute("UPDATE memory_atoms SET canonical_text='现在偏好详细回复。', updated_at_ms=? WHERE id=?",
                          (self.generated + 1, self.card["id"]))
        self.assertEqual(self.validate(), [])
        self.conn.execute("UPDATE memory_atoms SET updated_at_ms=100 WHERE id=?", (self.card["id"],))
        self.assertEqual(self.validate(), [])

    def test_scope_revocation_is_checked_again_even_when_old_doc_remains_active(self) -> None:
        self.assertEqual(self.validate(), [self.item])
        self.conn.execute("UPDATE memory_atoms SET visibility='room', scope_kind='room', scope_id='room:private' WHERE id=?", (self.card["id"],))
        self.assertEqual(self.validate(), [])
        self.assertEqual(self.conn.execute("SELECT status FROM memory_retrieval_docs WHERE source_id=?", (self.card["id"],)).fetchone()[0], "active")

    def test_deleted_or_demoted_card_is_not_replayed(self) -> None:
        self.conn.execute("UPDATE memory_atoms SET claim_state='superseded' WHERE id=?", (self.card["id"],))
        self.assertEqual(self.validate(), [])
        self.conn.execute("DELETE FROM memory_retrieval_docs WHERE source_id=?", (self.card["id"],))
        self.assertEqual(self.validate(), [])

    def test_book_rechecks_personal_atom_support(self) -> None:
        self.conn.execute("""INSERT INTO memory_books(book_id,book_type,book_key,title,summary,normalized_text,
            memory_atom_ids_json,status,created_at_ms,updated_at_ms)
            VALUES ('book:profile','topic','profile','偏好',?,? ,?,'active',100,100)""",
            (self.card["text"], self.card["text"], json.dumps([self.card["id"]])))
        rebuild_retrieval_docs(self.conn, preverified_schema=True)
        book = {"sourceType": "memory_book", "sourceId": "book:profile", "text": self.card["text"]}
        self.payload.update(items=[book], generatedAtMs=now_ms() + 1)
        self.assertEqual(self.validate(), [book])
        self.conn.execute("UPDATE agent_memory_evidence SET admission_state='forgotten'")
        self.assertEqual(self.validate(), [])

    def test_cached_book_rechecks_member_authority_without_timestamp_change(self) -> None:
        # PR135 / discussion_r4180769523: stale book metadata cannot grant member access.
        self.conn.execute("""INSERT INTO memory_books(book_id,book_type,book_key,title,summary,normalized_text,
            memory_atom_ids_json,status,created_at_ms,updated_at_ms)
            VALUES ('book:scope','topic','scope','偏好',?,?,?,'active',100,100)""",
            (self.card["text"], self.card["text"], json.dumps([self.card["id"]])))
        rebuild_retrieval_docs(self.conn, preverified_schema=True)
        book = {"sourceType": "memory_book", "sourceId": "book:scope", "text": self.card["text"]}
        self.payload.update(items=[book], generatedAtMs=now_ms() + 1)
        self.assertEqual(self.validate(), [book])
        original = dict(self.conn.execute("SELECT * FROM memory_atoms WHERE id=?", (self.card["id"],)).fetchone())
        for changes in ({"owner_kind": "room", "owner_id": "private-room"},
                        {"scope_kind": "room", "scope_id": "private-room", "visibility": "room"},
                        {"visibility": "room"}, {"scope_mode": "quarantined"},
                        {"status": "superseded"}, {"claim_state": "superseded"},
                        {"privacy_level": "sensitive"}):
            with self.subTest(changes=changes):
                columns = list(changes)
                update = "UPDATE memory_atoms SET " + ",".join(key + "=?" for key in columns) + " WHERE id=?"
                self.conn.execute(update, (*changes.values(), self.card["id"]))
                self.assertEqual(self.validate(), [])
                self.assertEqual(self.conn.execute("SELECT updated_at_ms FROM memory_atoms WHERE id=?",
                    (self.card["id"],)).fetchone()[0], original["updated_at_ms"])
                self.conn.execute(update, (*(original[key] for key in columns), self.card["id"]))
                self.assertEqual(self.validate(), [book])

    def test_validation_only_reads_bounded_source_ids_and_rejects_unknown_receipts(self) -> None:
        statements = []
        self.conn.set_trace_callback(statements.append)
        self.assertEqual(self.validate(), [self.item])
        docs_query = next(statement for statement in statements if "FROM memory_retrieval_docs" in statement)
        self.assertIn("source_id IN (", docs_query)
        self.assertIn(self.card["id"], docs_query)
        self.payload["generatedAtMs"] = None
        self.assertEqual(self.validate(), [])
        self.payload.update(generatedAtMs=self.generated, items=[{"sourceType": "notification", "sourceId": "x", "text": "ordinary context"}])
        self.assertEqual(self.validate(), [])

    def test_timeline_requires_current_admitted_sources(self) -> None:
        events = self.conn.execute("SELECT source_event_ids_json FROM memory_atoms WHERE id=?", (self.card["id"],)).fetchone()[0]
        today = datetime.now().astimezone().date().isoformat()
        self.conn.execute("""INSERT INTO daily_activity_timelines(timeline_id,timeline_date,status,
            source_event_ids_json,source_event_hash,summary_text,created_at_ms,updated_at_ms)
            VALUES ('timeline:today',?,'approved',?,'fixture','今天整理了回复偏好。',100,100)""", (today, events))
        rebuild_retrieval_docs(self.conn, preverified_schema=True)
        raw = self.conn.execute("SELECT raw_text FROM memory_retrieval_docs WHERE source_id='timeline:today'").fetchone()[0]
        self.payload.update(items=[{"sourceType": "memory_timeline", "sourceId": "timeline:today", "text": raw}],
                            generatedAtMs=now_ms() + 1, query={"preview": "今天做了什么"})
        self.assertEqual(len(self.validate()), 1)
        self.conn.execute("UPDATE agent_memory_evidence SET admission_state='forgotten'")
        self.assertEqual(self.validate(), [])


if __name__ == "__main__":
    unittest.main()
