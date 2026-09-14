from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from rag_ime.team.execution import (
    DockerExecutionLauncher,
    ExecutionSpec,
    TeamExecutionError,
)
from rag_ime.agent_workspace import WorkspaceHarnessError
from rag_ime.team.workspace_remote import TeamRemoteWorkspaceHarness
from rag_ime.team.workspace import TeamWorkspaceExecutor


class ExecutionSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-team-execution-")
        self.root = Path(self.tmp.name)
        self.scope = self.root / "attempt"
        self.scope.mkdir()
        self.workspace = self.scope / "workspace"
        self.workspace.mkdir()
        self.agent = self.scope / "agent"
        self.sessions = self.scope / "sessions"
        self.logs = self.scope / "logs"
        self.home = self.scope / "home"
        self.tmp_dir = self.scope / "tmp"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _spec(self, **changes: object) -> ExecutionSpec:
        values: dict[str, object] = {
            "attempt_id": "attempt-1",
            "session_id": "session-1",
            "scope_root": self.scope,
            "workspace_root": self.workspace,
            "agent_dir": self.agent,
            "session_dir": self.sessions,
            "logs_dir": self.logs,
            "home_dir": self.home,
            "tmp_dir": self.tmp_dir,
            "container_id": "paw-attempt-1",
            "runtime_image": "paw/pi-runtime:test",
            "runtime_command": ("/opt/paw/pi-runtime-host",),
        }
        values.update(changes)
        return ExecutionSpec(**values)

    def test_scope_state_is_required_to_stay_under_attempt_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "scope"):
            self._spec(agent_dir=self.root / "outside")

    def test_container_identity_and_command_are_immutable_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "container"):
            self._spec(container_id="../escape")
        with self.assertRaisesRegex(ValueError, "runtime command"):
            self._spec(runtime_command=())

    def test_secret_environment_is_rejected_before_launch(self) -> None:
        with self.assertRaisesRegex(ValueError, "credential"):
            self._spec(container_environment={"DEEPSEEK_API_KEY": "secret"})

    def test_shared_resources_are_separate_readonly_mounts(self) -> None:
        package = self.root / 'fixed-package'
        package.mkdir()
        spec = self._spec(resource_roots=(package,))
        args = DockerExecutionLauncher(image=spec.runtime_image, command=spec.runtime_command)._docker_run_arguments(spec, {})
        self.assertIn(
            f'type=bind,source={package.resolve()},destination=/run/paw/resources/0,readonly',
            args,
        )
        self.assertNotIn(f'type=bind,source={self.root}', args)
        self.assertEqual(spec.container_resource_path(0), '/run/paw/resources/0')
        with self.assertRaises(ValueError):
            self._spec(resource_roots=(self.workspace,))
        with self.assertRaises(ValueError):
            self._spec(resource_roots=(self.scope,))
        with self.assertRaises(ValueError):
            replace(spec, container_home_dir='/run/paw/resources')

    def test_pi_runtime_writable_defaults_stay_in_agent_mount(self) -> None:
        spec = self._spec()
        environment = spec.container_environment_for()

        self.assertEqual(
            environment["RAG_IME_PI_PLUGINS_DIR"],
            "/run/paw/agent/plugins",
        )
        self.assertEqual(
            environment["RAG_IME_PI_PLUGIN_INBOX"],
            "/run/paw/agent/plugin-inbox",
        )
        self.assertNotIn("/run/paw/Agent", environment.values())
        validated = DockerExecutionLauncher._validate_environment(environment, spec=spec)
        self.assertEqual(validated, environment)

    def test_shared_resource_link_swap_is_rejected_before_launch(self) -> None:
        package = self.root / 'fixed-package'
        package.mkdir()
        spec = self._spec(resource_roots=(package,))
        package.rmdir()
        package.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(TeamExecutionError):
            spec.validate_for_launch()

    def test_container_path_environment_cannot_reintroduce_host_state(self) -> None:
        spec = self._spec()
        with self.assertRaisesRegex(TeamExecutionError, "HOME"):
            DockerExecutionLauncher(
                image=spec.runtime_image,
                command=spec.runtime_command,
            ).start(
                spec,
                spec.runtime_command,
                {"HOME": str(self.root), "TMPDIR": "/tmp"},
            )


class DockerExecutionLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-docker-launcher-")
        self.root = Path(self.tmp.name)
        self.scope = self.root / "attempt"
        self.scope.mkdir()
        self.workspace = self.scope / "workspace"
        self.workspace.mkdir()
        self.spec = ExecutionSpec(
            attempt_id="attempt-1",
            session_id="session-1",
            scope_root=self.scope,
            workspace_root=self.workspace,
            agent_dir=self.scope / "agent",
            session_dir=self.scope / "sessions",
            logs_dir=self.scope / "logs",
            home_dir=self.scope / "home",
            tmp_dir=self.scope / "tmp",
            container_id="paw-attempt-1",
            runtime_image="paw/pi-runtime:test",
            runtime_command=("/opt/paw/pi-runtime-host",),
        )
        for path in (
            self.spec.agent_dir,
            self.spec.session_dir,
            self.spec.logs_dir,
            self.spec.home_dir,
            self.spec.tmp_dir,
        ):
            path.mkdir()
        self.launcher = DockerExecutionLauncher(
            image="paw/pi-runtime:test",
            command=("/opt/paw/pi-runtime-host",),
            docker_executable="docker",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_start_builds_readonly_networkless_scoped_container(self) -> None:
        process = object()
        with patch("rag_ime.team.execution.subprocess.Popen", return_value=process) as popen:
            result = self.launcher.start(
                self.spec,
                ["/opt/paw/pi-runtime-host"],
                {
                    "HOME": "/run/paw/home",
                    "RAG_IME_AGENT_TOOL_TOKEN": "short-lived",
                },
            )
        self.assertIs(result, process)
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["docker", "run", "--init"])
        self.assertIn("--read-only", command)
        self.assertIn("--network=none", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--security-opt=no-new-privileges=true", command)
        self.assertIn("--user", command)
        self.assertIn("65532:65532", command)
        self.assertNotIn("--privileged", command)
        self.assertIn("--name", command)
        self.assertIn("paw-attempt-1", command)
        self.assertIn("paw/pi-runtime:test", command)
        self.assertIn("/opt/paw/pi-runtime-host", command)
        command_text = " ".join(str(item) for item in command)
        self.assertIn(str(self.workspace), command_text)
        self.assertNotIn(str(Path.home()), command_text)

    def test_bind_mounts_use_docker_mount_boolean_syntax(self) -> None:
        spec = replace(
            self.spec,
            workspace_read_only=True,
            broker_socket=self.scope / "broker.sock",
        )
        args = self.launcher._docker_run_arguments(spec, {})
        mounts = [
            args[index + 1]
            for index, value in enumerate(args[:-1])
            if value == "--mount"
        ]

        self.assertIn(
            f"type=bind,source={spec.workspace_root},destination=/workspace,readonly",
            mounts,
        )
        self.assertIn(
            f"type=bind,source={spec.agent_dir},destination=/run/paw/agent",
            mounts,
        )
        self.assertIn(
            f"type=bind,source={spec.broker_socket},destination=/run/paw/broker.sock",
            mounts,
        )
        self.assertTrue(all(not mount.endswith((",rw", ",ro")) for mount in mounts))

    def test_stop_removes_and_verifies_container_before_returning_receipt(self) -> None:
        process = _ProcessStub()
        run_results = [
            subprocess.CompletedProcess(["docker", "rm"], 0, "paw-attempt-1\n", ""),
            subprocess.CompletedProcess(["docker", "inspect"], 1, "Error: No such object: paw-attempt-1\n", ""),
        ]
        with patch("rag_ime.team.execution.subprocess.run", side_effect=run_results) as run:
            receipt = self.launcher.stop(self.spec, process)
        self.assertTrue(receipt.verified)
        self.assertEqual(receipt.container_id, "paw-attempt-1")
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ["rm", "inspect"])
        self.assertTrue(process.terminated)

    def test_stop_does_not_claim_verified_when_container_remains(self) -> None:
        process = _ProcessStub()
        run_results = [
            subprocess.CompletedProcess(["docker", "rm"], 0, "", ""),
            subprocess.CompletedProcess(["docker", "inspect"], 0, "container-id\n", ""),
        ]
        with patch("rag_ime.team.execution.subprocess.run", side_effect=run_results):
            with self.assertRaisesRegex(TeamExecutionError, "still exists"):
                self.launcher.stop(self.spec, process)


class TeamWorkspaceExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-team-worker-")
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "note.txt").write_text("inside\n", encoding="utf-8")
        self.spec = ExecutionSpec(
            attempt_id="attempt-1",
            session_id="session-1",
            scope_root=self.root,
            workspace_root=self.workspace,
            agent_dir=self.root / "agent",
            session_dir=self.root / "sessions",
            logs_dir=self.root / "logs",
            home_dir=self.root / "home",
            tmp_dir=self.root / "tmp",
            container_id="paw-attempt-1",
            runtime_image="worker-test",
            runtime_command=("/opt/paw/pi-runtime-host",),
            container_workspace=str(self.workspace),
        )
        self.session = {
            "id": "session-1",
            "mode": "coordinator",
            "executionMode": "per_action",
            "workspaceRoots": [str(self.workspace)],
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_file_and_shell_tools_are_worker_local(self) -> None:
        executor = TeamWorkspaceExecutor()
        with patch.dict("os.environ", {"RAG_IME_TEAM_WORKER": "1"}):
            read = executor.execute_workspace(
                self.spec,
                self.session,
                "workspace_read",
                {"path": "note.txt"},
            )
            shell = executor.execute_workspace(
                self.spec,
                self.session,
                "workspace_shell",
                {"command": "printf worker"},
            )
        self.assertEqual(read["content"], "inside\n")
        self.assertEqual(shell["output"], "worker")
        self.assertEqual(shell["networkAllowed"], False)

    def test_server_process_and_unattached_jobs_fail_closed(self) -> None:
        executor = TeamWorkspaceExecutor()
        with self.assertRaisesRegex(TeamExecutionError, "isolated worker"):
            executor.execute_workspace(self.spec, self.session, "workspace_read", {"path": "note.txt"})
        with patch.dict("os.environ", {"RAG_IME_TEAM_WORKER": "1"}):
            with self.assertRaisesRegex(TeamExecutionError, "durable job"):
                executor.execute_workspace(self.spec, self.session, "workspace_job", {}, operation="status")


class TeamRemoteWorkspaceHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-team-remote-workspace-")
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        self.coordinator = _WorkspaceCoordinator()
        self.harness = TeamRemoteWorkspaceHarness(self.coordinator, "space-1")
        self.session = {
            "id": "session-1",
            "mode": "coordinator",
            "executionMode": "per_action",
            "workspaceRoots": [str(self.workspace)],
            "sessionFile": str(self.root / "host.sqlite"),
            "transcriptRef": str(self.root / "transcript.jsonl"),
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rpc_projects_roots_and_does_not_send_host_session_paths(self) -> None:
        result = self.harness.read(
            self.session,
            {"path": str(self.workspace / "note.txt")},
        )
        self.assertEqual(result["path"], str(self.workspace.resolve() / "note.txt"))
        self.assertEqual(result["content"], "worker content\n")
        call = self.coordinator.calls[-1]
        self.assertEqual(call[0], self.workspace.resolve())
        self.assertEqual(call[1], ("worker-rpc",))
        payload = call[2]
        self.assertEqual(payload["args"]["path"], "/workspace/note.txt")
        self.assertEqual(payload["session"]["workspaceRoots"], ["/workspace"])
        self.assertNotIn("sessionFile", payload["session"])
        self.assertNotIn("transcriptRef", payload["session"])

    def test_prepared_apply_reuses_assigned_root_and_maps_paths_back(self) -> None:
        prepared = self.harness.prepare_write(
            self.session,
            {"path": str(self.workspace / "note.txt"), "content": "updated\n"},
        )
        preview = self.harness.write_preview(prepared)
        self.assertEqual(
            preview["actionPayload"]["path"],
            str(self.workspace.resolve() / "note.txt"),
        )
        self.assertEqual(prepared.workspace_roots, (str(self.workspace.resolve()),))
        receipt = self.harness.apply_write(
            self.session,
            preview["actionPayload"],
            preview["baseState"],
        )
        self.assertTrue(receipt["mutationApplied"])
        self.assertEqual(receipt["path"], str(self.workspace.resolve() / "note.txt"))
        apply_payload = self.coordinator.calls[-1][2]
        self.assertEqual(apply_payload["args"]["path"], "/workspace/note.txt")

    def test_outside_path_is_rejected_before_worker_dispatch(self) -> None:
        with self.assertRaisesRegex(WorkspaceHarnessError, "outside"):
            self.harness.read(self.session, {"path": str(self.root / "secret.txt")})
        self.assertEqual(self.coordinator.calls, [])


class _WorkspaceCoordinator:
    workspace_rpc_command = ("worker-rpc",)

    def __init__(self) -> None:
        self.calls: list[tuple[Path, tuple[str, ...], dict[str, object], float]] = []

    def run_isolated(
        self,
        workspace: Path,
        command: tuple[str, ...],
        *,
        payload: dict[str, object] | None,
        timeout: float,
    ) -> dict[str, object]:
        assert payload is not None
        self.calls.append((Path(workspace).resolve(), tuple(command), payload, timeout))
        operation = str(payload["operation"])
        args = payload["args"]
        assert isinstance(args, dict)
        path = str(args.get("path") or "/workspace/note.txt")
        if operation == "read":
            result: dict[str, object] = {
                "path": path,
                "content": "worker content\n",
            }
        elif operation == "prepare":
            result = {
                "actionPayload": {**args, "path": path},
                "baseState": {
                    "workspaceRootsSha256": "roots-before",
                    "preimageSha256": "before",
                },
            }
        elif operation == "apply":
            result = {"path": path, "mutationApplied": True}
        else:
            result = {"path": path}
        stdout = json.dumps({"ok": True, "result": result}) + "\n"
        return {"exitCode": 0, "stdout": stdout, "stderr": ""}


class _ProcessStub:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.returncode = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


if __name__ == "__main__":
    unittest.main()
