from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from rag_ime.db.migration_runner import apply_database_migrations, migration_status
from tests.sqlite_fixtures import copy_current_database


class SQLiteFixtureTests(unittest.TestCase):
    def test_copies_preserve_schema_and_isolate_writes_and_normal_durability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.sqlite3"
            second = Path(temporary) / "second.sqlite3"
            copy_current_database(first)
            copy_current_database(second)
            with closing(sqlite3.connect(first)) as left, closing(sqlite3.connect(second)) as right:
                self.assertEqual(left.execute("PRAGMA synchronous").fetchone()[0], 2)
                self.assertEqual(left.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(left.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertTrue(migration_status(left)["ok"])
                self.assertEqual(apply_database_migrations(left).applied_versions, ())
                left.execute("CREATE TABLE fixture_isolation(value TEXT)")
                left.execute("INSERT INTO fixture_isolation VALUES ('first')")
                left.commit()
                self.assertIsNone(right.execute(
                    "SELECT name FROM sqlite_master WHERE name='fixture_isolation'"
                ).fetchone())
                self.assertEqual(left.execute("SELECT value FROM fixture_isolation").fetchone()[0], "first")

    def test_existing_database_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "existing.sqlite3"
            destination.write_bytes(b"preserve existing fixture")
            with self.assertRaises(FileExistsError):
                copy_current_database(destination)
            self.assertEqual(destination.read_bytes(), b"preserve existing fixture")
