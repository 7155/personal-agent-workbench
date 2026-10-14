from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing

from rag_ime.memory_book_models import MemoryBook
from rag_ime.memory_book_schema import ensure_memory_book_schema, memory_book_table_names
from rag_ime.memory_schema_v2 import ensure_memory_v2_schema, memory_v2_table_names


class MemoryBookSchemaTests(unittest.TestCase):
    def test_memory_book_schema_created(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            ensure_memory_book_schema(conn)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_books)")}

        self.assertIn("book_id", columns)
        self.assertIn("book_type", columns)
        self.assertIn("surface_hints_json", columns)
        self.assertIn("query_expansions_json", columns)
        self.assertIn("source_event_ids_json", columns)

    def test_memory_retrieval_docs_schema_created(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            ensure_memory_book_schema(conn)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_retrieval_docs)")}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}

        self.assertIn("raw_text", columns)
        self.assertIn("tags_text", columns)
        self.assertIn("aliases_text", columns)
        self.assertIn("surface_hints_text", columns)
        self.assertIn("query_expansions_text", columns)
        self.assertIn("memory_retrieval_docs_fts", tables)

    def test_memory_book_table_added_to_memory_v2_table_names(self) -> None:
        names = set(memory_v2_table_names())

        for table_name in memory_book_table_names():
            self.assertIn(table_name, names)

    def test_memory_book_schema_is_idempotent(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            ensure_memory_v2_schema(conn)
            ensure_memory_v2_schema(conn)
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}

        for table_name in memory_book_table_names():
            self.assertIn(table_name, names)

    def test_memory_book_dataclass_matches_compile_contract(self) -> None:
        book = MemoryBook(
            book_id="book:daily:2026-07-06",
            book_type="daily",
            book_key="2026-07-06",
            title="RAG 输入法多路召回方案",
            summary="用户希望结合 BM25、向量、TagMemo 和 Time。",
            tags=("RAG", "输入法"),
            surface_hints=("多路召回", "TagMemo"),
            query_expansions=("混合 RAG", "Daily Book"),
            source_event_ids=(123,),
            memory_atom_ids=("atom:hybrid-rag-core",),
            project="wisdom-weasel-rag-ime",
            confidence=0.86,
            quality_score=0.82,
        )

        self.assertEqual(book.book_type, "daily")
        self.assertEqual(book.surface_hints, ("多路召回", "TagMemo"))
        self.assertEqual(book.source_event_ids, (123,))
