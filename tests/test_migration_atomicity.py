from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from rag_ime.db.migration_runner import (
    MigrationChecksumError,
    apply_database_migrations,
    load_migrations,
    migration_status,
)


class MigrationAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="paw-migration-atomicity-"))
        self.root = Path(temporary)
        self.database = self.root / "database.sqlite3"
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()

    def migration(self, version: int, sql: str, *, root: Path | None = None) -> Path:
        path = (root or self.migrations) / f"{version:04d}_fixture.sql"
        path.write_text(sql, encoding="utf-8")
        return path

    def test_failed_ddl_rolls_back_schema_and_marker_then_repaired_retry_succeeds(self) -> None:
        migration = self.migration(400, "CREATE TABLE fragment(value TEXT);\nINSERT INTO absent VALUES (1);\n")
        with closing(sqlite3.connect(self.database)) as conn:
            with self.assertRaisesRegex(sqlite3.OperationalError, "absent"):
                apply_database_migrations(conn, migrations_dir=self.migrations)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='fragment'").fetchone())
            self.assertEqual(conn.execute("SELECT version FROM schema_migrations").fetchall(), [])
            migration.write_text("CREATE TABLE fragment(value TEXT);\nINSERT INTO fragment VALUES ('repaired');\n")
            result = apply_database_migrations(conn, migrations_dir=self.migrations)
            self.assertEqual(result.applied_versions, (400,))
            self.assertEqual(conn.execute("SELECT value FROM fragment").fetchone()[0], "repaired")
            self.assertEqual(apply_database_migrations(conn, migrations_dir=self.migrations).applied_versions, ())

    def test_failure_retains_prior_migration_and_rolls_back_alter_and_data(self) -> None:
        self.migration(400, "CREATE TABLE retained(value TEXT);\nINSERT INTO retained VALUES ('original');\n")
        failed = self.migration(401, "ALTER TABLE retained ADD COLUMN fragment TEXT;\nUPDATE retained SET value='changed';\nINSERT INTO absent VALUES (1);\n")
        with closing(sqlite3.connect(self.database)) as conn:
            with self.assertRaisesRegex(sqlite3.OperationalError, "absent"):
                apply_database_migrations(conn, migrations_dir=self.migrations)
            self.assertEqual(conn.execute("SELECT version FROM schema_migrations").fetchall(), [(400,)])
            self.assertEqual(conn.execute("SELECT value FROM retained").fetchone()[0], "original")
            self.assertEqual([row[1] for row in conn.execute("PRAGMA table_info(retained)")], ["value"])
            failed.write_text("ALTER TABLE retained ADD COLUMN fragment TEXT;\nUPDATE retained SET value='repaired';\n")
            self.assertEqual(apply_database_migrations(conn, migrations_dir=self.migrations).applied_versions, (401,))

    def test_existing_caller_transaction_keeps_commit_rollback_and_noop_semantics(self) -> None:
        migration = self.migration(400, "CREATE TABLE fragment(value TEXT);\nINSERT INTO absent VALUES (1);\n")
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("CREATE TABLE caller(value TEXT)")
            conn.execute("INSERT INTO caller VALUES ('pending')")
            with self.assertRaisesRegex(sqlite3.OperationalError, "absent"):
                apply_database_migrations(conn, migrations_dir=self.migrations)
            self.assertFalse(conn.in_transaction)
            self.assertEqual(conn.execute("SELECT value FROM caller").fetchall(), [])
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='fragment'").fetchone())
            conn.execute("INSERT INTO caller VALUES ('committed with migration')")
            migration.write_text("CREATE TABLE fragment(value TEXT);\n")
            apply_database_migrations(conn, migrations_dir=self.migrations)
            self.assertFalse(conn.in_transaction)
            with closing(sqlite3.connect(self.database)) as observer:
                self.assertEqual(observer.execute("SELECT value FROM caller").fetchall(), [("committed with migration",)])
                conn.execute("INSERT INTO caller VALUES ('pending after noop')")
                self.assertEqual(apply_database_migrations(conn, migrations_dir=self.migrations).applied_versions, ())
                self.assertTrue(conn.in_transaction)
                self.assertEqual(observer.execute("SELECT COUNT(*) FROM caller").fetchone()[0], 1)

    def test_checksum_preflight_preserves_pending_caller_transaction(self) -> None:
        migration = self.migration(400, "CREATE TABLE caller(value TEXT);\n")
        with closing(sqlite3.connect(self.database)) as conn:
            apply_database_migrations(conn, migrations_dir=self.migrations)
            conn.execute("INSERT INTO caller VALUES ('pending')")
            migration.write_text("CREATE TABLE caller(value TEXT, changed TEXT);\n")
            self.migration(401, "CREATE TABLE should_not_apply(value TEXT);\n")
            with self.assertRaises(MigrationChecksumError):
                apply_database_migrations(conn, migrations_dir=self.migrations)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute("SELECT value FROM caller").fetchone()[0], "pending")
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='should_not_apply'").fetchone())
            with closing(sqlite3.connect(self.database)) as observer:
                self.assertEqual(observer.execute("SELECT value FROM caller").fetchall(), [])

    def concurrent_connections(self, roots: list[Path]) -> list[object]:
        barrier = threading.Barrier(len(roots))

        class SnapshotConnection(sqlite3.Connection):
            def execute(self, sql: str, parameters=()):
                cursor = super().execute(sql, parameters)
                if sql == "SELECT version, checksum FROM schema_migrations ORDER BY version":
                    # Both initializers have the same stale preflight snapshot.
                    rows = cursor.fetchall()
                    barrier.wait(timeout=10)
                    return iter(rows)
                return cursor

        def initialize(root: Path) -> object:
            with closing(sqlite3.connect(self.database, timeout=10, factory=SnapshotConnection)) as conn:
                try:
                    return apply_database_migrations(conn, migrations_dir=root)
                except MigrationChecksumError as error:
                    return error

        with ThreadPoolExecutor(max_workers=len(roots)) as pool:
            return list(pool.map(initialize, roots))

    def test_concurrent_fresh_initializers_apply_each_packaged_migration_once(self) -> None:
        from rag_ime.db.migration_runner import DEFAULT_MIGRATIONS_DIR

        results = self.concurrent_connections([DEFAULT_MIGRATIONS_DIR] * 2)
        versions = [version for result in results for version in result.applied_versions]
        expected = [migration.version for migration in load_migrations()]
        self.assertEqual(sorted(versions), expected)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertTrue(migration_status(conn)["ok"])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_concurrent_source_disagreement_still_fails_checksum_check_under_lock(self) -> None:
        other = self.root / "other"
        other.mkdir()
        self.migration(400, "CREATE TABLE first(value TEXT);\n")
        self.migration(400, "CREATE TABLE second(value TEXT);\n", root=other)
        results = self.concurrent_connections([self.migrations, other])
        self.assertEqual(sum(isinstance(result, MigrationChecksumError) for result in results), 1)
        self.assertEqual(sum(not isinstance(result, MigrationChecksumError) for result in results), 1)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)

    def test_0185_completed_hook_checkpoint_and_failed_suffix_are_retryable(self) -> None:
        for migration in load_migrations():
            if migration.version <= 184:
                shutil.copy2(migration.path, self.migrations / migration.path.name)
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            apply_database_migrations(conn, migrations_dir=self.migrations)
            conn.execute("INSERT INTO memory_cleanup_runs(run_id,created_at_ms,run_kind) VALUES ('retained',1,'manual_curation')")
            conn.execute("INSERT INTO memory_cleanup_diffs(run_id,op,payload_json,created_at_ms) VALUES ('retained','noop','{}',1)")
            conn.commit()
            suffix = self.migration(185, "CREATE TABLE suffix_fragment(value TEXT);\nINSERT INTO absent VALUES (1);\n")
            with self.assertRaisesRegex(sqlite3.OperationalError, "absent"):
                apply_database_migrations(conn, migrations_dir=self.migrations)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='suffix_fragment'").fetchone())
            self.assertIsNone(conn.execute("SELECT version FROM schema_migrations WHERE version=185").fetchone())
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA legacy_alter_table").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT run_kind FROM memory_cleanup_runs WHERE run_id='retained'").fetchone()[0], "manual_curation")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_cleanup_diffs WHERE run_id='retained'").fetchone()[0], 1)
            self.assertIn("catalog_consolidation", conn.execute("SELECT sql FROM sqlite_master WHERE name='memory_cleanup_runs'").fetchone()[0])
            suffix.write_text("CREATE TABLE suffix_fragment(value TEXT);\n")
            self.assertEqual(apply_database_migrations(conn, migrations_dir=self.migrations).applied_versions, (185,))
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_0185_hook_failure_restores_legacy_data_schema_and_foreign_key_flags(self) -> None:
        for migration in load_migrations():
            if migration.version <= 184:
                shutil.copy2(migration.path, self.migrations / migration.path.name)
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            apply_database_migrations(conn, migrations_dir=self.migrations)
            conn.execute("INSERT INTO memory_cleanup_runs(run_id,created_at_ms,run_kind) VALUES ('retained',1,'manual_curation')")
            conn.commit()
            # A pre-existing corrupt legacy child must cause the real rebuild
            # to fail and roll back, rather than silently adopting bad data.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("INSERT INTO memory_cleanup_diffs(run_id,op,payload_json,created_at_ms) VALUES ('missing-parent','noop','{}',1)")
            conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")
            self.migration(185, "-- Real 0185 hook, marker only.\n")
            with self.assertRaisesRegex(RuntimeError, "foreign-key violations"):
                apply_database_migrations(conn, migrations_dir=self.migrations)
            self.assertIsNone(conn.execute("SELECT version FROM schema_migrations WHERE version=185").fetchone())
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='memory_cleanup_runs_before_catalog'").fetchone())
            self.assertNotIn("catalog_consolidation", conn.execute("SELECT sql FROM sqlite_master WHERE name='memory_cleanup_runs'").fetchone()[0])
            self.assertEqual(conn.execute("SELECT run_kind FROM memory_cleanup_runs WHERE run_id='retained'").fetchone()[0], "manual_curation")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA legacy_alter_table").fetchone()[0], 0)
            conn.execute("DELETE FROM memory_cleanup_diffs WHERE run_id='missing-parent'")
            conn.commit()
            self.assertEqual(apply_database_migrations(conn, migrations_dir=self.migrations).applied_versions, (185,))
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
