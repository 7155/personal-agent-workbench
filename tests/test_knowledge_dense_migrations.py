from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

from rag_ime.embeddings import HashingEmbeddingProvider
from rag_ime.knowledge_library.dense import SqliteDenseIndex, USearchDenseIndex, dense_index_from_env


DENSE = 'knowledge_dense_chunks'
ANN = 'knowledge_ann_keys'
DENSE_ROW = ('chunk', 'document', 'base', 'local-hash:32:v1', '[1.0,0.0]')
ANN_ROW = (41, *DENSE_ROW[:4])


class SchemaOnlyANN(USearchDenseIndex):
    def _synchronize_projection(self):
        return None


def interrupted_connect(owner, boundary, fired):
    class InterruptedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            index_name = f'idx_knowledge_{"dense" if owner == DENSE else "ann"}_base'
            if sql.startswith(boundary) and (owner in sql or index_name in sql):
                fired.append(sql)
                raise RuntimeError('injected after actual SQLite statement')
            return result

    def connect(index):
        connection = sqlite3.connect(index.database_path, timeout=10, factory=InterruptedConnection)
        connection.row_factory = sqlite3.Row
        return connection

    return connect


class KnowledgeDenseMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='paw-dense-migration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.provider = HashingEmbeddingProvider(dimensions=32)

    def seed(self, path, table, *, current=False, rows=None):
        owner = DENSE if table.startswith(DENSE) else ANN
        with closing(sqlite3.connect(path)) as connection, connection:
            if owner == DENSE:
                key = 'PRIMARY KEY(chunk_id,fingerprint)' if current else 'PRIMARY KEY(chunk_id)'
                connection.execute(
                    f'CREATE TABLE {table} (chunk_id TEXT NOT NULL, document_id TEXT NOT NULL, '
                    f'base_id TEXT NOT NULL, fingerprint TEXT NOT NULL, vector_json TEXT NOT NULL, {key})'
                )
                values = [DENSE_ROW] if rows is None else rows
            else:
                key = 'UNIQUE(chunk_id,fingerprint)' if current else 'UNIQUE(chunk_id)'
                connection.execute(
                    f'CREATE TABLE {table} (ann_key INTEGER PRIMARY KEY AUTOINCREMENT, '
                    'chunk_id TEXT NOT NULL, document_id TEXT NOT NULL, base_id TEXT NOT NULL, '
                    f'fingerprint TEXT NOT NULL, {key})'
                )
                values = [ANN_ROW] if rows is None else rows
            connection.executemany(f'INSERT INTO {table} VALUES (?,?,?,?,?)', values)
            if owner == DENSE and table == DENSE and current:
                connection.execute(f'CREATE INDEX idx_knowledge_dense_base ON {DENSE}(base_id,fingerprint)')

    def rows(self, path, table):
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute(f'SELECT * FROM {table} ORDER BY 1,4').fetchall()

    def snapshot(self, path):
        with closing(sqlite3.connect(path)) as connection:
            return list(connection.iterdump())

    def initialize(self, path, owner):
        if owner == DENSE:
            return SqliteDenseIndex(path, self.provider)
        # Isolate schema migration from rebuildable HNSW files here. Actual
        # USearch file/search integration remains covered by the neighbor suite.
        return SchemaOnlyANN(path, self.provider)

    def test_every_dense_ddl_and_copy_boundary_rolls_back_and_reopens(self):
        for journal_mode in ('DELETE', 'WAL'):
            self.assert_interruption_recovery(DENSE, journal_mode)

    def test_every_ann_ddl_and_copy_boundary_rolls_back_and_reopens(self):
        for journal_mode in ('DELETE', 'WAL'):
            self.assert_interruption_recovery(ANN, journal_mode)

    def assert_interruption_recovery(self, owner, journal_mode):
        boundaries = ('DROP INDEX', 'ALTER TABLE', 'CREATE TABLE', 'INSERT', 'DROP TABLE', 'CREATE INDEX')
        if owner == ANN:
            boundaries += ('UPDATE sqlite_sequence',)
        for boundary in boundaries:
            with self.subTest(owner=owner, boundary=boundary, journal_mode=journal_mode):
                path = self.root / f'{owner}-{journal_mode}-{boundary.replace(" ", "-")}.sqlite'
                if owner == ANN:
                    self.seed(path, DENSE, current=True)
                self.seed(path, owner)
                with closing(sqlite3.connect(path)) as connection, connection:
                    self.assertEqual(journal_mode.lower(), connection.execute(f'PRAGMA journal_mode={journal_mode}').fetchone()[0])
                    connection.execute(f'CREATE INDEX idx_knowledge_{"dense" if owner == DENSE else "ann"}_base ON {owner}(base_id,fingerprint)')
                before = self.snapshot(path)
                fired = []

                connect = interrupted_connect(owner, boundary, fired)

                with mock.patch.object(SqliteDenseIndex, '_connect', connect):
                    with self.assertRaisesRegex(RuntimeError, 'injected'):
                        self.initialize(path, owner)
                self.assertEqual(1, len(fired))
                self.assertEqual(before, self.snapshot(path), 'DDL and rows must roll back together')
                self.initialize(path, owner)
                self.assertEqual([DENSE_ROW if owner == DENSE else ANN_ROW], self.rows(path, owner))

    def test_recovery_ddl_and_copy_boundaries_preserve_shadow_on_interruption(self):
        for owner in (DENSE, ANN):
            for boundary in ('DROP INDEX', 'CREATE TABLE', 'INSERT', 'DROP TABLE', 'CREATE INDEX'):
                with self.subTest(owner=owner, boundary=boundary):
                    path = self.root / f'recover-{owner}-{boundary.replace(" ", "-")}.sqlite'
                    if owner == ANN:
                        self.seed(path, DENSE, current=True)
                    self.seed(path, owner + '_legacy_v1')
                    before = self.snapshot(path)
                    fired = []

                    connect = interrupted_connect(owner, boundary, fired)

                    with mock.patch.object(SqliteDenseIndex, '_connect', connect):
                        with self.assertRaisesRegex(RuntimeError, 'injected'):
                            self.initialize(path, owner)
                    self.assertEqual(1, len(fired))
                    self.assertEqual(before, self.snapshot(path))
                    self.initialize(path, owner)
                    self.assertEqual([DENSE_ROW if owner == DENSE else ANN_ROW], self.rows(path, owner))

    def test_process_exit_after_rename_rolls_back_on_cold_reopen(self):
        script = """
import os, sqlite3, sys
from tests.test_knowledge_dense_migrations import SchemaOnlyANN, DENSE
from rag_ime.embeddings import HashingEmbeddingProvider
from rag_ime.knowledge_library.dense import SqliteDenseIndex
path, owner = sys.argv[1:]
class InterruptedConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if sql.startswith('ALTER TABLE ' + owner + ' '):
            os._exit(42)
        return result
def connect(index):
    connection = sqlite3.connect(index.database_path, factory=InterruptedConnection)
    connection.row_factory = sqlite3.Row
    return connection
SqliteDenseIndex._connect = connect
index_type = SqliteDenseIndex if owner == DENSE else SchemaOnlyANN
index_type(path, HashingEmbeddingProvider(dimensions=32))
"""
        for owner in (DENSE, ANN):
            with self.subTest(owner=owner):
                path = self.root / f'process-exit-{owner}.sqlite'
                if owner == ANN:
                    self.seed(path, DENSE, current=True)
                self.seed(path, owner)
                before = self.snapshot(path)
                result = subprocess.run([sys.executable, '-c', script, str(path), owner], cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10)
                self.assertEqual(42, result.returncode, result.stderr.decode())
                self.assertEqual(before, self.snapshot(path))
                self.initialize(path, owner)
                self.assertEqual([DENSE_ROW if owner == DENSE else ANN_ROW], self.rows(path, owner))

    def test_empty_and_current_schemas_reopen_without_losing_profiles_or_keys(self):
        for owner in (DENSE, ANN):
            with self.subTest(owner=owner):
                path = self.root / f'empty-{owner}.sqlite'
                self.initialize(path, owner)
                self.assertEqual([], self.rows(path, owner))
                other = ('chunk', 'document', 'base', 'other-profile', '[0.0,1.0]')
                records = [DENSE_ROW, other] if owner == DENSE else [ANN_ROW, (87, *other[:4])]
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.executemany(f'INSERT INTO {owner} VALUES (?,?,?,?,?)', records)
                before = self.snapshot(path)
                self.initialize(path, owner)
                self.assertEqual(before, self.snapshot(path))
                self.assertCountEqual(records, self.rows(path, owner))

    def test_shadow_only_and_current_plus_shadow_recover_complete_rows(self):
        for owner in (DENSE, ANN):
            for current in (False, True):
                with self.subTest(owner=owner, current=current):
                    path = self.root / f'shadow-{owner}-{current}.sqlite'
                    if owner == ANN:
                        self.seed(path, DENSE, current=True)
                    self.seed(path, owner + '_legacy_v1')
                    if current:
                        self.seed(path, owner, current=True)
                    self.initialize(path, owner)
                    self.assertEqual([DENSE_ROW if owner == DENSE else ANN_ROW], self.rows(path, owner))
                    with closing(sqlite3.connect(path)) as connection:
                        self.assertIsNone(connection.execute('SELECT 1 FROM sqlite_master WHERE name=?', (owner + '_legacy_v1',)).fetchone())

    def test_shadow_merge_preserves_disjoint_fingerprints_and_ann_keys(self):
        for owner in (DENSE, ANN):
            with self.subTest(owner=owner):
                path = self.root / f'merge-{owner}.sqlite'
                if owner == ANN:
                    self.seed(path, DENSE, current=True)
                original = DENSE_ROW if owner == DENSE else ANN_ROW
                other_dense = ('chunk', 'document', 'base', 'other-profile', '[0.0,1.0]')
                other = other_dense if owner == DENSE else (87, *other_dense[:4])
                self.seed(path, owner, current=True, rows=[other])
                self.seed(path, owner + '_legacy_v1', rows=[original])
                self.initialize(path, owner)
                self.assertCountEqual([original, other], self.rows(path, owner))

    def test_conflicting_shadow_requires_rebuild_and_preserves_all_original_data(self):
        for owner in (DENSE, ANN):
            with self.subTest(owner=owner):
                path = self.root / f'conflict-{owner}.sqlite'
                if owner == ANN:
                    self.seed(path, DENSE, current=True)
                original = DENSE_ROW if owner == DENSE else ANN_ROW
                conflicting = (*DENSE_ROW[:4], '[0.0,1.0]') if owner == DENSE else (99, *ANN_ROW[1:])
                self.seed(path, owner, current=True, rows=[conflicting])
                self.seed(path, owner + '_legacy_v1', rows=[original])
                before = self.snapshot(path)
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'rebuild-required'):
                        self.initialize(path, owner)
                    self.assertEqual(before, self.snapshot(path))

    def test_malformed_shadow_does_not_create_empty_success(self):
        for owner in (DENSE, ANN):
            with self.subTest(owner=owner):
                path = self.root / f'malformed-{owner}.sqlite'
                if owner == ANN:
                    self.seed(path, DENSE, current=True)
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(f'CREATE TABLE {owner}_legacy_v1 (chunk_id TEXT)')
                    connection.execute(f"INSERT INTO {owner}_legacy_v1 VALUES ('survivor')")
                before = self.snapshot(path)
                with self.assertRaisesRegex(RuntimeError, 'rebuild-required'):
                    self.initialize(path, owner)
                self.assertEqual(before, self.snapshot(path))

    def test_invalid_shadow_row_cannot_be_ignored_as_success(self):
        for owner in (DENSE, ANN):
            with self.subTest(owner=owner):
                path = self.root / f'null-shadow-{owner}.sqlite'
                if owner == ANN:
                    self.seed(path, DENSE, current=True)
                fields = ('chunk_id TEXT, document_id TEXT, base_id TEXT, fingerprint TEXT, vector_json TEXT'
                          if owner == DENSE else 'ann_key INTEGER, chunk_id TEXT, document_id TEXT, base_id TEXT, fingerprint TEXT')
                record = ('chunk', 'document', None, 'profile', '[1.0]') if owner == DENSE else (41, 'chunk', 'document', None, 'profile')
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(f'CREATE TABLE {owner}_legacy_v1 ({fields})')
                    connection.execute(f'INSERT INTO {owner}_legacy_v1 VALUES (?,?,?,?,?)', record)
                before = self.snapshot(path)
                with self.assertRaisesRegex(RuntimeError, 'rebuild-required'):
                    self.initialize(path, owner)
                self.assertEqual(before, self.snapshot(path))

    def test_factory_reports_ann_rebuild_requirement_without_mutating_ambiguous_mapping(self):
        path = self.root / 'fallback.sqlite'
        self.seed(path, DENSE, current=True)
        self.seed(path, ANN, current=True, rows=[(99, *ANN_ROW[1:])])
        self.seed(path, ANN + '_legacy_v1')
        before = self.snapshot(path)
        fallback = dense_index_from_env(path, self.provider, {'RAG_IME_KNOWLEDGE_DENSE_BACKEND': 'usearch'})
        result = fallback.status()
        self.assertEqual('sqlite-exact-vector-scan', result['kind'])
        self.assertEqual('usearch', result['fallbackFrom'])
        self.assertIn('rebuild-required', result['reason'])
        self.assertFalse(result['ann'])
        self.assertTrue(result['degraded'])
        self.assertEqual(1, result['vectorCount'])
        self.assertEqual(before, self.snapshot(path))

    def test_two_old_tables_require_rebuild_without_overwriting_either(self):
        for owner in (DENSE, ANN):
            with self.subTest(owner=owner):
                path = self.root / f'two-old-{owner}.sqlite'
                if owner == ANN:
                    self.seed(path, DENSE, current=True)
                self.seed(path, owner)
                self.seed(path, owner + '_legacy_v1')
                before = self.snapshot(path)
                with self.assertRaisesRegex(RuntimeError, 'rebuild-required'):
                    self.initialize(path, owner)
                self.assertEqual(before, self.snapshot(path))

    def test_ann_legacy_mapping_without_exact_dense_lineage_requires_rebuild(self):
        cases = {
            'missing-vector': [],
            'wrong-document': [('chunk', 'foreign-document', 'base', DENSE_ROW[3], DENSE_ROW[4])],
            'wrong-base': [('chunk', 'document', 'foreign-base', DENSE_ROW[3], DENSE_ROW[4])],
            'wrong-fingerprint': [('chunk', 'document', 'base', 'foreign-profile', DENSE_ROW[4])],
        }
        for case, vectors in cases.items():
            for shadow in (False, True):
                with self.subTest(case=case, shadow=shadow):
                    path = self.root / f'lineage-{case}-{shadow}.sqlite'
                    self.seed(path, DENSE, current=True, rows=vectors)
                    self.seed(path, ANN + '_legacy_v1' if shadow else ANN)
                    before = self.snapshot(path)
                    with self.assertRaisesRegex(RuntimeError, 'rebuild-required'):
                        self.initialize(path, ANN)
                    self.assertEqual(before, self.snapshot(path))

    def test_ann_numeric_key_collision_is_not_reassigned_or_replaced(self):
        path = self.root / 'key-collision.sqlite'
        self.seed(path, DENSE, current=True, rows=[DENSE_ROW, ('other-chunk', 'other-document', 'other-base', 'other-profile', '[0.0,1.0]')])
        self.seed(path, ANN, current=True)
        self.seed(path, ANN + '_legacy_v1', rows=[(41, 'other-chunk', 'other-document', 'other-base', 'other-profile')])
        before = self.snapshot(path)
        with self.assertRaisesRegex(RuntimeError, 'rebuild-required'):
            self.initialize(path, ANN)
        self.assertEqual(before, self.snapshot(path))

    def test_recovered_vectors_are_searchable_and_ann_key_sequence_is_retained(self):
        path = self.root / 'searchable.sqlite'
        vector = str(self.provider.embed('glacier evidence'))
        record = (*DENSE_ROW[:4], vector)
        self.seed(path, DENSE + '_legacy_v1', rows=[record])
        dense = self.initialize(path, DENSE)
        self.assertEqual(1, dense.status()['vectorCount'])
        self.assertEqual('chunk', dense.search('glacier evidence', base_ids=('base',), limit=1)[0][0])
        self.seed(path, ANN + '_legacy_v1')
        self.initialize(path, ANN)
        with closing(sqlite3.connect(path)) as connection, connection:
            cursor = connection.execute(f'INSERT INTO {ANN}(chunk_id,document_id,base_id,fingerprint) VALUES (?,?,?,?)', ('next', 'document', 'base', 'other-profile'))
            self.assertGreater(cursor.lastrowid, 41)
        self.assertEqual(ANN_ROW, self.rows(path, ANN)[0])

    def test_ann_recovery_preserves_previously_allocated_key_high_watermark(self):
        for shadow in (False, True):
            with self.subTest(shadow=shadow):
                path = self.root / f'ann-sequence-{shadow}.sqlite'
                self.seed(path, DENSE, current=True)
                table = ANN + '_legacy_v1' if shadow else ANN
                self.seed(path, table)
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(f'INSERT INTO {table} VALUES (?,?,?,?,?)', (99, 'deleted', 'document', 'base', 'other-profile'))
                    connection.execute(f'DELETE FROM {table} WHERE ann_key=99')
                self.initialize(path, ANN)
                with closing(sqlite3.connect(path)) as connection, connection:
                    cursor = connection.execute(f'INSERT INTO {ANN}(chunk_id,document_id,base_id,fingerprint) VALUES (?,?,?,?)', ('next', 'document', 'base', 'other-profile'))
                    self.assertGreater(cursor.lastrowid, 99, 'migration must not reuse an original allocated ANN identity')
                self.assertEqual(ANN_ROW, self.rows(path, ANN)[0])

    @unittest.skipUnless(importlib.util.find_spec('usearch'), 'knowledge-ann extra is not installed')
    def test_actual_ann_shadow_recovery_rebuilds_and_searches_original_key(self):
        path = self.root / 'actual-ann-shadow.sqlite'
        self.seed(path, DENSE + '_legacy_v1', rows=[(*DENSE_ROW[:4], str(self.provider.embed('glacier evidence')))])
        self.seed(path, ANN + '_legacy_v1')
        recovered = USearchDenseIndex(path, self.provider)
        self.assertEqual([ANN_ROW], self.rows(path, ANN))
        self.assertEqual('chunk', recovered.search('glacier evidence', base_ids=('base',), limit=1)[0][0])
        self.assertTrue(recovered.status()['projectionConsistent'])
        self.assertTrue(list(recovered.index_root.rglob('*.usearch')))

    def test_eight_concurrent_initializers_preserve_one_projection_and_original_keys(self):
        for owner in (DENSE, ANN):
            with self.subTest(owner=owner):
                path = self.root / f'concurrent-{owner}.sqlite'
                if owner == ANN:
                    self.seed(path, DENSE, current=True)
                self.seed(path, owner)
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual('wal', connection.execute('PRAGMA journal_mode=WAL').fetchone()[0])
                barrier = threading.Barrier(8)

                def initialize(_, current_path=path, current_owner=owner, current_barrier=barrier):
                    current_barrier.wait(timeout=10)
                    self.initialize(current_path, current_owner)

                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(initialize, range(8)))
                self.assertEqual([DENSE_ROW if owner == DENSE else ANN_ROW], self.rows(path, owner))


if __name__ == '__main__':
    unittest.main()
