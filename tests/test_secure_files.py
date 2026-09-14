from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from rag_ime.secure_files import atomic_write, regular_reader, replace_file


class SecureWorkspaceFileTests(unittest.TestCase):
    def test_changed_parent_symlink_cannot_read_or_overwrite_another_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / 'workspace'
            outside = root / 'outside'
            workspace.mkdir()
            outside.mkdir()
            (outside / 'secret.md').write_text('private')
            (workspace / 'docs').symlink_to(outside, target_is_directory=True)
            for action in (
                lambda: atomic_write(workspace / 'docs/secret.md', b'overwrite'),
                lambda: self.read(workspace / 'docs/secret.md'),
            ):
                with self.assertRaises(OSError):
                    action()
            self.assertEqual((outside / 'secret.md').read_text(), 'private')

    @staticmethod
    def read(path):
        with regular_reader(path) as source:
            return source.read()

    def test_atomic_projection_replaces_a_file_symlink_without_following_it(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            secret = root / 'secret'
            secret.write_text('private')
            path = root / 'projection.json'
            path.symlink_to(secret)
            atomic_write(path, b'{}')
            self.assertFalse(path.is_symlink())
            self.assertEqual(secret.read_text(), 'private')
            self.assertEqual(self.read(path), b'{}')

    def test_move_creates_only_real_parent_directories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'draft.md'
            source.write_text('draft')
            target = root / 'docs/archive/draft.md'
            replace_file(source, target)
            self.assertFalse(source.exists())
            self.assertEqual(self.read(target), b'draft')
