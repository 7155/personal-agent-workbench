from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from rag_ime.team.__main__ import load_execution, load_preview, main
from rag_ime.team.errors import TeamError
from rag_ime.team.grants import TeamGrantStore
from rag_ime.team.identity import TeamIdentityStore
from rag_ime.team.workspaces import TeamWorkspaceService


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": "/usr/bin:/bin",
        }
    )
    return env


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(f"git {args!r} failed: {completed.stderr}")
    return completed.stdout.strip()


class TeamProvisionProjectCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="paw-team-cli-")
        self.root = Path(self.tmp.name)
        self.data_root = self.root / "team-data"
        self.data_root.mkdir(mode=0o700)
        self.identity = TeamIdentityStore(self.data_root / "team.sqlite")
        self.owner = self.identity.bootstrap_admin(
            "operator",
            "operator-password-123",
            display_name="Team operator",
        )
        self.source = self.root / "source"
        self.source.mkdir()
        _git(self.source, "init", "--initial-branch=main")
        _git(self.source, "config", "user.name", "fixture")
        _git(self.source, "config", "user.email", "fixture@example.test")
        (self.source / "README.md").write_text("committed main\n", encoding="utf-8")
        _git(self.source, "add", "README.md")
        _git(self.source, "commit", "-m", "main")
        _git(self.source, "switch", "-c", "release")
        (self.source / "release.txt").write_text("committed release\n", encoding="utf-8")
        _git(self.source, "add", "release.txt")
        _git(self.source, "commit", "-m", "release")
        self.source_head = _git(self.source, "rev-parse", "refs/heads/release")
        (self.source / "uncommitted.txt").write_text("must stay local\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *extra: str) -> tuple[int, str, str]:
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(
                [
                    "provision-project",
                    "--data-root",
                    str(self.data_root),
                    "--owner",
                    "operator",
                    "--name",
                    "Imported project",
                    "--source-repo",
                    str(self.source),
                    *extra,
                ]
            )
        return result, stdout.getvalue(), stderr.getvalue()

    def project_count(self) -> int:
        with self.identity._connection() as conn:  # type: ignore[attr-defined]
            return int(conn.execute("SELECT COUNT(*) FROM team_spaces WHERE kind = 'project'").fetchone()[0])

    def test_imports_selected_committed_branch_without_dirty_files_or_host(self) -> None:
        source_status_before = _git(self.source, "status", "--porcelain")
        with patch("rag_ime.pi.host_client.PiRuntimeHostClient.start") as start:
            code, stdout, stderr = self.run_cli("--branch", "release")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        result = json.loads(stdout)
        self.assertEqual(result["branch"], "release")
        self.assertEqual(result["headCommit"], self.source_head)
        self.assertEqual(result["revision"], 1)
        self.assertTrue(str(result["projectId"]).startswith("space_"))
        start.assert_not_called()

        service = TeamWorkspaceService(
            self.data_root / "projects",
            self.identity,
            TeamGrantStore(self.identity.db_path, self.identity),
        )
        project = service.ensure_project(result["projectId"], target_branch="release")
        repository = Path(str(project["repositoryPath"]))
        self.assertEqual((repository / "README.md").read_text(encoding="utf-8"), "committed main\n")
        self.assertEqual((repository / "release.txt").read_text(encoding="utf-8"), "committed release\n")
        self.assertFalse((repository / "uncommitted.txt").exists())
        self.assertEqual(_git(self.source, "status", "--porcelain"), source_status_before)
        self.assertEqual(_git(self.source, "rev-parse", "refs/heads/release"), self.source_head)

    def test_missing_owner_or_branch_fails_before_creating_a_project(self) -> None:
        before = self.project_count()
        code, stdout, stderr = self.run_cli("--branch", "missing")
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("source_branch_not_found", stderr)
        self.assertEqual(self.project_count(), before)

        code, stdout, stderr = self.run_cli("--branch", "main", "--owner", "missing-owner")
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("owner_not_found", stderr)
        self.assertEqual(self.project_count(), before)

    def test_invalid_source_fails_without_project_creation_and_without_traceback(self) -> None:
        before = self.project_count()
        self.source.joinpath(".git").rename(self.source.parent / "not-a-git-dir")
        code, stdout, stderr = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("invalid_source_repo", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.project_count(), before)

    def test_inactive_owner_is_rejected_before_project_creation(self) -> None:
        member = self.identity.create_member(
            self.owner["id"],
            "inactive-operator",
            "inactive-password-123",
        )
        self.identity.set_member_active(self.owner["id"], member["id"], False)
        before = self.project_count()
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                [
                    "provision-project",
                    "--data-root",
                    str(self.data_root),
                    "--owner",
                    "inactive-operator",
                    "--name",
                    "Should not exist",
                    "--source-repo",
                    str(self.source),
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("owner_not_active", stderr.getvalue())
        self.assertEqual(self.project_count(), before)

    def test_final_import_failure_names_the_retained_empty_project(self) -> None:
        before = self.project_count()
        with patch.object(
            TeamWorkspaceService,
            "ensure_project",
            side_effect=TeamError(500, "project_import_failed", "fixture import failed"),
        ):
            code, stdout, stderr = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("project_import_failed", stderr)
        self.assertIn("empty project space_", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.project_count(), before + 1)


class TeamPreviewConfigTests(unittest.TestCase):
    def test_operator_config_selects_runtime_and_dedicated_origin_without_starting_processes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'preview.json'
            config.write_text(json.dumps({'image': 'fixture-approved-image', 'originTemplate': 'https://{deployment}.previews.example.test'}))
            config.chmod(0o600)
            with patch('rag_ime.team.preview_runtime.PreviewRuntime') as runtime_type:
                runtime, origin, listener = load_preview(config, root)
                self.assertIs(runtime, runtime_type.return_value)
                self.assertEqual(origin, 'https://{deployment}.previews.example.test')
                self.assertEqual(listener, {'host': '127.0.0.1', 'port': 8771})
                runtime_type.return_value.start.assert_not_called()
                self.assertEqual(runtime_type.call_args.kwargs['image'], 'fixture-approved-image')

    def test_preview_config_rejects_shared_readable_file_and_project_injected_commands(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'preview.json'
            data = {'image': 'fixture-image', 'originTemplate': 'https://{deployment}.previews.example.test'}
            config.write_text(json.dumps(data))
            config.chmod(0o644)
            with self.assertRaises(ValueError):
                load_preview(config, root)
            config.chmod(0o600)
            config.write_text(json.dumps({**data, 'command': ['host-command']}))
            with self.assertRaises(ValueError):
                load_preview(config, root)


class TeamExecutionConfigTests(unittest.TestCase):
    def _write_config(self, path, broker_root, **extra):
        data = {
            'image': 'fixture-image',
            'model': {
                'base_url': 'http://127.0.0.1:9/v1',
                'model': 'fixture-model',
                'api_key': 'fixture-key',
                'allow_loopback_http': True,
            },
            'brokerSocketRoot': str(broker_root),
            **extra,
        }
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def test_load_execution_passes_operator_broker_root_without_starting_worker(self):
        with TemporaryDirectory(prefix='r-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            broker_root = root / 'daemon-brokers'
            broker_root.mkdir(mode=0o700)
            config = root / 'runtime.json'
            self._write_config(config, broker_root)
            with patch('rag_ime.team.execution.DockerExecutionLauncher.start') as start:
                execution = load_execution(config)
            try:
                self.assertEqual(execution._broker_socket_root_config, broker_root)
                start.assert_not_called()
            finally:
                execution.close()

    def test_load_execution_rejects_unpublished_snake_case_alias(self):
        with TemporaryDirectory(prefix='r-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            first = root / 'first'
            first.mkdir()
            config = root / 'runtime.json'
            self._write_config(config, first)
            data = json.loads(config.read_text())
            data.pop('brokerSocketRoot')
            data['broker_socket_root'] = str(first)
            config.write_text(json.dumps(data))
            config.chmod(0o600)
            with self.assertRaises(ValueError):
                load_execution(config)


if __name__ == "__main__":
    unittest.main()
