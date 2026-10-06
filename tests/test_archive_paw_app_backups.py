from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/archive_paw_app_backups.py"
SPEC = importlib.util.spec_from_file_location("archive_paw_app_backups", SCRIPT)
assert SPEC and SPEC.loader
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class ArchivePawAppBackupsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.apps = self.root / "Applications"
        self.external = self.root / "external"
        self.apps.mkdir()
        self.external.mkdir()

    def backup(self, name: str, age_hours: int) -> Path:
        path = self.apps / f".paw-update.{name}"
        (path / "Personal Agent Workbench.app").mkdir(parents=True)
        (path / "Personal Agent Workbench.app/Contents.txt").write_text(name)
        (path / "dock-before.plist").write_text(f"dock-{name}")
        when = time.time() - age_hours * 3600
        os.utime(path, (when, when))
        return path

    def test_archives_only_old_inactive_backups_and_preserves_paths(self) -> None:
        old = self.backup("old", 24)
        middle = self.backup("middle", 12)
        recent = self.backup("recent", 2)
        newest = self.backup("newest", 0)
        self.assertEqual(archive.archive_backups(self.apps, self.external, require_external=False), 2)
        for path in (old, middle):
            self.assertTrue(path.is_symlink())
            self.assertEqual(path.resolve(), (self.external / path.name).resolve())
            self.assertEqual((path / "dock-before.plist").read_text(),
                             f"dock-{path.name.removeprefix('.paw-update.')}")
        for path in (recent, newest):
            self.assertTrue(path.is_dir())
            self.assertFalse(path.is_symlink())
        rows = [json.loads(line) for line in (self.external / "migration.jsonl").read_text().splitlines()]
        self.assertEqual(sum(row["removed"] for row in rows), 2)

    def test_busy_backup_and_unavailable_archive_are_left_local(self) -> None:
        busy = self.backup("busy", 24)
        other = self.backup("other", 12)
        self.backup("recent", 2)
        self.backup("newest", 0)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            archive.archive_backups(self.apps, self.root / "missing", require_external=False)
        with self.assertRaisesRegex(RuntimeError, "system disk"):
            archive.archive_backups(self.apps, self.external)
        with mock.patch.object(archive, "has_open_files", side_effect=lambda path: path == busy):
            self.assertEqual(archive.archive_backups(self.apps, self.external, require_external=False), 1)
        self.assertTrue(busy.is_dir())
        self.assertFalse(busy.is_symlink())
        self.assertTrue(other.is_symlink())

    def test_archive_destination_cannot_redirect_to_another_path(self) -> None:
        old = self.backup("old", 24)
        self.backup("middle", 12)
        self.backup("newest", 0)
        (self.external / old.name).symlink_to(self.apps, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "invalid archive destination"):
            archive.archive_backups(self.apps, self.external, require_external=False)
        self.assertTrue(old.is_dir())
        self.assertFalse(old.is_symlink())


if __name__ == "__main__":
    unittest.main()
