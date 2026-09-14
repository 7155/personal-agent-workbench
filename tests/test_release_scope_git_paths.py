from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_project_harness import (
    RELEASE_SCOPE_SCHEMA,
    _git_candidate_paths,
    validate_release_candidate_scope,
)


class ReleaseScopeGitPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Scope Test")
        self.git("config", "user.email", "scope-test@example.invalid")
        self.git("config", "core.quotePath", "true")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        self.write("seed.txt", "base\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.root), *args],
            text=True,
            stderr=subprocess.PIPE,
        )

    def write(self, name: str, value: str = "new\n") -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def commit(self) -> None:
        self.git("add", "--all")
        self.git("commit", "-qm", "fixture")

    def test_empty_diff(self) -> None:
        self.assertEqual(_git_candidate_paths(self.root, self.base), ({}, ""))

    def test_non_ascii_paths_are_not_c_quoted(self) -> None:
        self.write("分析/地图.ts")
        self.commit()
        self.assertEqual(
            _git_candidate_paths(self.root, self.base),
            ({"分析/地图.ts": "added"}, ""),
        )

    def test_control_characters_and_quotes_are_preserved(self) -> None:
        names = ["tab\tname.ts", "line\nbreak.ts", "cr\rname.ts", 'quote"name.ts']
        for name in names:
            self.write(name)
        self.commit()
        self.assertEqual(
            _git_candidate_paths(self.root, self.base),
            ({name: "added" for name in names}, ""),
        )

    def test_added_modified_deleted_and_spaces(self) -> None:
        self.write("removed.txt")
        self.commit()
        base = self.git("rev-parse", "HEAD").strip()
        (self.root / "removed.txt").unlink()
        self.write("seed.txt", "changed\n")
        self.write("a directory/new file.ts")
        self.commit()
        self.assertEqual(
            _git_candidate_paths(self.root, base),
            ({"removed.txt": "deleted", "seed.txt": "modified",
              "a directory/new file.ts": "added"}, ""),
        )

    def test_rename_remains_add_and_delete(self) -> None:
        (self.root / "seed.txt").rename(self.root / "renamed.txt")
        self.commit()
        self.assertEqual(
            _git_candidate_paths(self.root, self.base),
            ({"seed.txt": "deleted", "renamed.txt": "added"}, ""),
        )

    def test_invalid_base_is_an_error_not_a_clean_scope(self) -> None:
        paths, error = _git_candidate_paths(self.root, "0" * 40)
        self.assertEqual(paths, {})
        self.assertIn("Git diff failed", error)

    def test_malformed_and_unsupported_rows_fail_closed(self) -> None:
        for output in (b"A\0", b"A\0\0", b"A\0unterminated", b"T\0type.ts\0"):
            with self.subTest(output=output):
                with patch("scripts.check_project_harness.subprocess.run") as run:
                    run.return_value.stdout = output
                    paths, error = _git_candidate_paths(self.root, self.base)
                self.assertEqual(paths, {})
                self.assertTrue(error)

    def test_scope_validator_accepts_exact_unicode_paths(self) -> None:
        self.write("分析/地图.ts")
        self.commit()
        item = {"path": "分析/地图.ts", "status": "added", "group": "test",
                "disposition": "keep_release", "reason": "Reviewed fixture"}
        payload = {
            "schemaVersion": RELEASE_SCOPE_SCHEMA,
            "source": {"baseCommit": self.base},
            "items": [item],
            "groups": {"test": {"pathCount": 1}},
            "summary": {"pathCount": 1, "dispositionCounts": {"keep_release": 1},
                        "groupCounts": {"test": 1}, "unclassifiedPathCount": 0,
                        "otherWorkNotInReleasePathCount": 0},
        }
        self.write("release/release-candidate-scope.json", json.dumps(payload))
        self.assertEqual(validate_release_candidate_scope(self.root), [])
        # The repair must NOT bypass genuine missing release entries.
        self.write("extra.ts")
        self.commit()
        errors = validate_release_candidate_scope(self.root)
        self.assertTrue(any("misses base-to-HEAD paths" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
