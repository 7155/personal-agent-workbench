from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from rag_ime.team.files import TeamFiles


class TeamFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'alice'
        self.root.mkdir()
        self.other = Path(self.tmp.name) / 'bob'
        self.other.mkdir()
        (self.root / 'readme.txt').write_text('Alice project')
        (self.other / 'secret.txt').write_text('Bob private')
        self.files = TeamFiles(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_only_bound_files_are_listed_without_host_paths(self) -> None:
        listing = self.files.list({})
        self.assertEqual(listing['path'], '/')
        self.assertEqual(listing['homePath'], '/')
        self.assertEqual([i['path'] for i in listing['items']], ['/readme.txt'])
        self.assertEqual(self.files.read({'path': '/readme.txt'})['content'], 'Alice project')

    def test_traversal_and_symlinks_cannot_read_another_space(self) -> None:
        (self.root / 'link').symlink_to(self.other, target_is_directory=True)
        for path in ('../bob/secret.txt', '/../../bob/secret.txt', '/link/secret.txt', str(self.other / 'secret.txt')):
            with self.subTest(path=path), self.assertRaises((PermissionError, FileNotFoundError, ValueError)):
                self.files.read({'path': path})
        with self.assertRaises((PermissionError, ValueError)):
            self.files.list({'path': '/link'})

    def test_special_files_and_file_symlinks_are_not_read(self) -> None:
        import os
        (self.root / 'shortcut').symlink_to(self.other / 'secret.txt')
        os.mkfifo(self.root / 'pipe')
        for path in ('/shortcut', '/pipe'):
            with self.subTest(path=path), self.assertRaises((PermissionError, ValueError)):
                self.files.read({'path': path})

    def test_utf8_paging_uses_bytes_and_preserves_text(self) -> None:
        (self.root / 'text.txt').write_text('中文abc', encoding='utf-8')
        result = self.files.read({'path': '/text.txt', 'limit': 4})
        self.assertEqual(result['content'], '中')
        self.assertEqual(result['nextOffset'], 3)
        self.assertTrue(result['truncated'])


if __name__ == '__main__':
    unittest.main()
