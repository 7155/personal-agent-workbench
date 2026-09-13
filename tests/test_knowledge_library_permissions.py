from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rag_ime.knowledge_library.permissions import harden_knowledge_tree


class KnowledgeStoragePermissionTests(unittest.TestCase):
    def test_hardening_tolerates_a_disappearing_sqlite_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = root / 'document.txt'
            document.write_text('Retained source')
            document.chmod(0o644)
            # Another SQLite connection can close its last WAL handle after
            # os.walk lists these names, before permission hardening stats them.
            with patch('rag_ime.knowledge_library.permissions.os.walk', return_value=[
                (str(root), [], ['knowledge.sqlite-wal', 'knowledge.sqlite-shm', 'document.txt'])
            ]):
                harden_knowledge_tree(root)
            self.assertEqual(document.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    def test_hardening_still_surfaces_permission_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'document.txt').write_text('Retained source')
            with patch('rag_ime.knowledge_library.permissions.secure_file', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    harden_knowledge_tree(root)

    def test_hardening_still_surfaces_a_missing_persistent_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('rag_ime.knowledge_library.permissions.os.walk', return_value=[
                (directory, [], ['document.txt'])
            ]):
                with self.assertRaises(FileNotFoundError):
                    harden_knowledge_tree(Path(directory))
