"""Trusted execution boundaries for team-scoped Pi Hosts.

The normal personal runtime still launches a local managed Pi process.  Team
execution supplies an :class:`ExecutionSpec` and a trusted launcher so the
same Pi JSONL protocol can run inside one short-lived, scope-specific Linux
container.  This module deliberately owns container construction only; user
identity, grants, broker authorization, and repository integration stay in
the team/server layers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import subprocess
from typing import IO, Protocol, runtime_checkable
from urllib.parse import urlsplit


__all__ = [
    "DockerExecutionLauncher",
    "ExecutionProcess",
    "ExecutionLauncher",
    "ExecutionReceipt",
    "ExecutionSpec",
    "TeamExecutionError",
]


_CONTAINER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SECRET_ENV = re.compile(
    r"(?:API[_-]?KEY|PASSWORD|PASSWD|SECRET|PRIVATE[_-]?KEY|CREDENTIAL|TOKEN)",
    re.IGNORECASE,
)
_ALLOWED_SHORT_LIVED_ENV = frozenset(
    {
        "RAG_IME_AGENT_TOOL_TOKEN",
        "RAG_IME_TOOL_GATEWAY_TOKEN",
        "RAG_IME_AGENT_TOOL_URL",
        "RAG_IME_TOOL_GATEWAY_URL",
        "RAG_IME_PLUGIN_APPROVAL_TOKEN",
    }
)
_CONTAINER_USER = re.compile(r"^[1-9][0-9]{0,8}:[1-9][0-9]{0,8}$")
_MEMORY_LIMIT = re.compile(r"^[1-9][0-9]{0,7}(?:[bkmg])?$", re.IGNORECASE)
_CPU_LIMIT = re.compile(r"^(?:0\.[0-9]{1,3}|[1-9][0-9]{0,3}(?:\.[0-9]{1,3})?)$")


def _require_local_url(value: str, *, message: str) -> str:
    url = str(value).strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is None
        or not 1 <= parsed.port <= 65_535
    ):
        raise ValueError(message)
    return url


class TeamExecutionError(RuntimeError):
    """A trusted launcher rejected or could not verify a team execution."""


@runtime_checkable
class ExecutionProcess(Protocol):
    """The pipe/process subset consumed by the Pi JSONL client."""

    pid: int
    stdin: IO[bytes] | None
    stdout: IO[bytes] | None
    stderr: IO[bytes] | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


@dataclass(frozen=True)
class ExecutionSpec:
    """Immutable server-generated scope for one Pi Host attempt.

    The host paths are mounted individually into fixed container paths.  The
    server must create the IDs, image, command, and paths before this object
    reaches a launcher.  No value is inferred from the request or from the
    host's current HOME.
    """

    attempt_id: str
    session_id: str
    scope_root: Path
    workspace_root: Path
    agent_dir: Path
    session_dir: Path
    logs_dir: Path
    home_dir: Path
    tmp_dir: Path
    container_id: str
    runtime_image: str
    runtime_command: tuple[str, ...]
    broker_socket: Path | None = None
    gateway_url: str = "http://127.0.0.1:8766/api/agent/tool/execute"
    container_workspace: str = "/workspace"
    container_agent_dir: str = "/run/paw/agent"
    container_session_dir: str = "/run/paw/sessions"
    container_logs_dir: str = "/run/paw/logs"
    container_home_dir: str = "/run/paw/home"
    container_tmp_dir: str = "/tmp"
    container_broker_socket: str = "/run/paw/broker.sock"
    workspace_read_only: bool = False
    network_mode: str = "none"
    memory_limit: str = "1g"
    cpu_limit: str = "1.0"
    pids_limit: int = 256
    container_user: str = "65532:65532"
    container_environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    resource_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        for name in ("attempt_id", "session_id", "runtime_image"):
            value = str(getattr(self, name)).strip()
            if (
                not value
                or len(value) > 256
                or any(ord(char) < 32 for char in value)
            ):
                raise ValueError(f"execution {name} must be non-empty and safe")
            object.__setattr__(self, name, value)

        container_id = str(self.container_id).strip()
        if not _CONTAINER_ID.fullmatch(container_id):
            raise ValueError("execution container id is invalid")
        object.__setattr__(self, "container_id", container_id)

        command = tuple(str(part) for part in self.runtime_command)
        if not command or any(
            not part
            or len(part) > 4096
            or any(ord(char) < 32 for char in part)
            for part in command
        ):
            raise ValueError("execution runtime command must be non-empty and safe")
        object.__setattr__(self, "runtime_command", command)

        normalized_paths: dict[str, Path] = {}
        for name in (
            "scope_root",
            "workspace_root",
            "agent_dir",
            "session_dir",
            "logs_dir",
            "home_dir",
            "tmp_dir",
        ):
            path = Path(getattr(self, name)).expanduser()
            if not path.is_absolute():
                raise ValueError(f"execution {name} must be absolute")
            if "," in str(path) or (path.exists() and path.is_symlink()):
                raise ValueError(f"execution {name} path is not safe")
            normalized_paths[name] = path
            object.__setattr__(self, name, path)

        scope_root = normalized_paths["scope_root"].resolve(strict=False)
        if scope_root == Path("/"):
            raise ValueError("execution scope root is too broad")
        object.__setattr__(self, "scope_root", scope_root)
        for name in ("agent_dir", "session_dir", "logs_dir", "home_dir", "tmp_dir"):
            path = normalized_paths[name].resolve(strict=False)
            if path == scope_root or not path.is_relative_to(scope_root):
                raise ValueError(f"execution {name} must remain inside scope root")
            object.__setattr__(self, name, path)

        workspace = normalized_paths["workspace_root"]
        object.__setattr__(self, "workspace_root", workspace.resolve(strict=False))
        state_paths = (
            self.agent_dir,
            self.session_dir,
            self.logs_dir,
            self.home_dir,
            self.tmp_dir,
        )
        for index, left in enumerate(state_paths):
            for right in state_paths[index + 1 :]:
                if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                    raise ValueError("execution state directories must not overlap")
            if left.is_relative_to(self.workspace_root) or self.workspace_root.is_relative_to(left):
                raise ValueError("execution state and workspace roots must not overlap")
        if len(self.resource_roots) > 16:
            raise ValueError('execution supports at most 16 fixed Package roots')
        resources: list[Path] = []
        for candidate in self.resource_roots:
            path = Path(candidate)
            if not path.is_absolute() or ',' in str(path) or '..' in path.parts or path.is_symlink():
                raise ValueError('execution resource path is unsafe')
            path = path.resolve(strict=True)
            if not path.is_dir() or path == Path('/'):
                raise ValueError('execution resource must be a fixed directory')
            for other in (self.scope_root, self.workspace_root, *resources):
                if path.is_relative_to(other) or other.is_relative_to(path):
                    raise ValueError('execution resources must not overlap workspace, state or other resources')
            resources.append(path)
        object.__setattr__(self, 'resource_roots', tuple(resources))
        if self.broker_socket is not None:
            socket_path = Path(self.broker_socket).expanduser()
            if not socket_path.is_absolute() or "," in str(socket_path):
                raise ValueError("execution broker socket must be absolute")
            object.__setattr__(self, "broker_socket", socket_path.resolve(strict=False))

        for name in (
            "container_workspace",
            "container_agent_dir",
            "container_session_dir",
            "container_logs_dir",
            "container_home_dir",
            "container_tmp_dir",
            "container_broker_socket",
        ):
            value = str(getattr(self, name)).strip()
            if (
                not value.startswith("/")
                or "\x00" in value
                or "," in value
                or ".." in Path(value).parts
                or Path(value) == Path("/")
            ):
                raise ValueError(f"execution {name} must be an absolute container path")
            object.__setattr__(self, name, value)
        container_dirs = tuple(
            Path(getattr(self, name))
            for name in (
                "container_workspace",
                "container_agent_dir",
                "container_session_dir",
                "container_logs_dir",
                "container_home_dir",
                "container_tmp_dir",
            )
        )
        for index, left in enumerate(container_dirs):
            for right in container_dirs[index + 1 :]:
                if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                    raise ValueError("execution container mount destinations must not overlap")
        broker_destination = Path(self.container_broker_socket)
        if self.resource_roots:
            resource_destination = Path('/run/paw/resources')
            for destination in (*container_dirs, broker_destination):
                if destination.is_relative_to(resource_destination) or resource_destination.is_relative_to(destination):
                    raise ValueError('execution resource mount must not overlap other container mounts')
        if any(
            broker_destination == directory
            or broker_destination.is_relative_to(directory)
            for directory in container_dirs
        ):
            raise ValueError("execution broker mount must not overlap state mounts")

        if self.network_mode != "none":
            raise ValueError("team execution currently requires network_mode=none")
        object.__setattr__(
            self,
            "gateway_url",
            _require_local_url(
                self.gateway_url,
                message="team execution gateway must point to the local broker",
            ),
        )
        if not 1 <= int(self.pids_limit) <= 32_768:
            raise ValueError("execution pids limit is out of bounds")
        object.__setattr__(self, "pids_limit", int(self.pids_limit))
        container_user = str(self.container_user).strip()
        if not _CONTAINER_USER.fullmatch(container_user):
            raise ValueError("execution container user must be a non-root uid:gid")
        object.__setattr__(self, "container_user", container_user)
        memory_limit = str(self.memory_limit).strip()
        cpu_limit = str(self.cpu_limit).strip()
        if not _MEMORY_LIMIT.fullmatch(memory_limit):
            raise ValueError("execution memory limit is invalid")
        if not _CPU_LIMIT.fullmatch(cpu_limit):
            raise ValueError("execution CPU limit is invalid")
        object.__setattr__(self, "memory_limit", memory_limit)
        object.__setattr__(self, "cpu_limit", cpu_limit)

        environment: dict[str, str] = {}
        for raw_key, raw_value in dict(self.container_environment).items():
            key = str(raw_key)
            value = str(raw_value)
            if not _ENV_NAME.fullmatch(key):
                raise ValueError("execution environment name is invalid")
            if "\x00" in value or "\n" in value or len(value) > 16_384:
                raise ValueError("execution environment value is invalid")
            if _SECRET_ENV.search(key) and key not in _ALLOWED_SHORT_LIVED_ENV:
                raise ValueError("long-lived provider credentials cannot enter execution")
            if key == 'RAG_IME_PLUGIN_APPROVAL_TOKEN' and not self.resource_roots:
                raise ValueError('Package activation requires fixed task resources')
            environment[key] = value
        object.__setattr__(self, "container_environment", environment)

    def container_resource_path(self, index: int) -> str:
        if not 0 <= index < len(self.resource_roots):
            raise ValueError('execution resource index is unavailable')
        return f'/run/paw/resources/{index}'

    def validate_for_launch(self, *, create_state_dirs: bool = True) -> None:
        """Validate physical paths immediately before mounting them."""

        if self.scope_root.exists() and self.scope_root.is_symlink():
            raise TeamExecutionError("execution scope root must not be a symlink")
        if not self.scope_root.is_dir():
            raise TeamExecutionError("execution scope root is unavailable")
        for resource in self.resource_roots:
            if resource.is_symlink() or not resource.is_dir() or resource.resolve(strict=True) != resource:
                raise TeamExecutionError('execution fixed resource is unavailable or changed')
        if self.workspace_root.is_symlink() or not self.workspace_root.is_dir():
            raise TeamExecutionError("execution workspace must be a real directory")
        state_dirs = (
            self.agent_dir,
            self.session_dir,
            self.logs_dir,
            self.home_dir,
            self.tmp_dir,
        )
        if create_state_dirs:
            for path in state_dirs:
                if path.exists() and path.is_symlink():
                    raise TeamExecutionError("execution state directories cannot be symlinks")
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                if path.is_symlink() or not path.is_dir():
                    raise TeamExecutionError("execution state directory is unavailable")
                os.chmod(path, 0o700)
        elif any(path.is_symlink() for path in state_dirs):
            raise TeamExecutionError("execution state directories cannot be symlinks")
        if self.broker_socket is not None and self.broker_socket.is_symlink():
            raise TeamExecutionError("execution broker socket must not be a symlink")
        if self.broker_socket is not None and not self.broker_socket.exists():
            raise TeamExecutionError("execution broker socket is unavailable")

    def container_environment_for(
        self,
        *,
        gateway_url: str | None = None,
        gateway_token: str = "",
    ) -> dict[str, str]:
        """Return only scope paths and short-lived broker variables."""

        environment = dict(self.container_environment)
        url = gateway_url or self.gateway_url
        try:
            url = _require_local_url(
                url,
                message="execution gateway must remain local to the container",
            )
        except ValueError as exc:
            raise TeamExecutionError(str(exc)) from exc
        environment.update(
            {
                "HOME": self.container_home_dir,
                "TMPDIR": self.container_tmp_dir,
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "RAG_IME_APP_SUPPORT_DIR": "/run/paw",
                "PI_CODING_AGENT_DIR": self.container_agent_dir,
                "RAG_IME_PI_AGENT_DIR": self.container_agent_dir,
                "RAG_IME_PI_SESSION_DIR": self.container_session_dir,
                "RAG_IME_PI_PLUGINS_DIR": f"{self.container_agent_dir}/plugins",
                "RAG_IME_PI_PLUGIN_INBOX": f"{self.container_agent_dir}/plugin-inbox",
                "RAG_IME_TEAM_WORKER": "1",
                "RAG_IME_AGENT_TOOL_URL": url,
                "RAG_IME_TOOL_GATEWAY_URL": url,
            }
        )
        if self.broker_socket is not None:
            environment["RAG_IME_TEAM_BROKER_SOCKET"] = self.container_broker_socket
        if gateway_token:
            environment.update(
                {
                    "RAG_IME_AGENT_TOOL_TOKEN": gateway_token,
                    "RAG_IME_TOOL_GATEWAY_TOKEN": gateway_token,
                }
            )
        return environment

    def manifest(self) -> dict[str, object]:
        """Return a redacted, auditable launcher manifest."""

        return {
            "schemaVersion": "rag-ime.team-execution-spec.v1",
            "attemptId": self.attempt_id,
            "sessionId": self.session_id,
            "containerId": self.container_id,
            "runtimeImage": self.runtime_image,
            "runtimeCommand": list(self.runtime_command),
            "scopeRoot": str(self.scope_root),
            "workspaceRoot": str(self.workspace_root),
            "stateRoots": {
                "agent": str(self.agent_dir),
                "sessions": str(self.session_dir),
                "logs": str(self.logs_dir),
                "home": str(self.home_dir),
                "tmp": str(self.tmp_dir),
            },
            "brokerSocket": str(self.broker_socket) if self.broker_socket else None,
            "networkMode": self.network_mode,
            "workspaceReadOnly": self.workspace_read_only,
            "limits": {
                "memory": self.memory_limit,
                "cpus": self.cpu_limit,
                "pids": self.pids_limit,
                "user": self.container_user,
            },
            "environmentNames": sorted(self.container_environment),
        }

    def to_manifest(self) -> dict[str, object]:
        """Stable alias used by execution receipt and ledger adapters."""

        return self.manifest()


@dataclass(frozen=True)
class ExecutionReceipt:
    """Outcome of a launcher lifecycle operation."""

    attempt_id: str
    session_id: str
    container_id: str
    action: str
    state: str
    verified: bool
    detail: str = ""
    exit_code: int | None = None
    schema_version: str = "rag-ime.team-execution-receipt.v1"

    def as_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "attemptId": self.attempt_id,
            "sessionId": self.session_id,
            "containerId": self.container_id,
            "action": self.action,
            "state": self.state,
            "verified": self.verified,
            "detail": self.detail,
            "exitCode": self.exit_code,
        }


@runtime_checkable
class ExecutionLauncher(Protocol):
    """Trusted process boundary consumed by :class:`PiRuntimeHostClient`."""

    def start(
        self,
        spec: ExecutionSpec,
        command: Sequence[str],
        environment: Mapping[str, str],
    ) -> ExecutionProcess: ...

    def stop(self, spec: ExecutionSpec, process: ExecutionProcess) -> ExecutionReceipt: ...


class DockerExecutionLauncher:
    """Launch one Pi Host through a constrained Docker/OCI-compatible daemon.

    The image and command are constructor configuration owned by the trusted
    server.  The per-attempt spec only selects server-created mounts and an
    exact pre-authorized container identity.  The launcher never uses shell
    interpolation and never mounts a host HOME, database, or Docker socket.
    """

    def __init__(
        self,
        *,
        image: str,
        command: Sequence[str],
        docker_executable: str = "docker",
        stop_timeout_seconds: float = 10.0,
    ) -> None:
        self.image = str(image).strip()
        self.command = tuple(str(part) for part in command)
        self.docker_executable = str(docker_executable).strip()
        if not self.image or not self.command or not self.docker_executable:
            raise ValueError("Docker execution launcher requires image, command and executable")
        self.stop_timeout_seconds = max(1.0, float(stop_timeout_seconds))

    def start(
        self,
        spec: ExecutionSpec,
        command: Sequence[str],
        environment: Mapping[str, str],
    ) -> subprocess.Popen[bytes]:
        if spec.runtime_image != self.image:
            raise TeamExecutionError("execution image is not the trusted server image")
        if tuple(str(part) for part in command) != self.command:
            raise TeamExecutionError("execution command is not the trusted server command")
        if spec.runtime_command != self.command:
            raise TeamExecutionError("execution spec command is not the trusted server command")
        spec.validate_for_launch()
        normalized_environment = self._validate_environment(environment, spec=spec)
        args = self._docker_run_arguments(spec, normalized_environment)
        try:
            return subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=spec.scope_root,
                env={"PATH": os.environ.get("PATH", "")},
                bufsize=0,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise TeamExecutionError(f"failed to start isolated Pi Host: {exc}") from exc

    def stop(self, spec: ExecutionSpec, process: ExecutionProcess) -> ExecutionReceipt:
        """Remove the container and verify absence before returning success."""

        self._run_docker(
            [self.docker_executable, "rm", "-f", spec.container_id],
            allow_failure=True,
        )
        inspected = self._run_docker(
            [self.docker_executable, "inspect", spec.container_id],
            allow_failure=True,
        )
        if inspected.returncode == 0:
            self._terminate_client(process)
            raise TeamExecutionError(
                f"execution container still exists after stop: {spec.container_id}"
            )
        detail = (inspected.stderr or inspected.stdout or "").strip()
        if not self._is_missing_container(inspected):
            self._terminate_client(process)
            raise TeamExecutionError(
                f"could not verify isolated container removal: {detail[:240]}"
            )
        self._terminate_client(process)
        return ExecutionReceipt(
            attempt_id=spec.attempt_id,
            session_id=spec.session_id,
            container_id=spec.container_id,
            action="stop",
            state="stopped",
            verified=True,
            detail="container absent after docker rm -f",
            exit_code=self._poll(process),
        )

    def _docker_run_arguments(
        self,
        spec: ExecutionSpec,
        environment: Mapping[str, str],
    ) -> list[str]:
        args = [
            self.docker_executable,
            "run",
            "--init",
            "--interactive",
            "--read-only",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            "--user",
            spec.container_user,
            "--pids-limit",
            str(spec.pids_limit),
            "--memory",
            spec.memory_limit,
            "--cpus",
            spec.cpu_limit,
            "--name",
            spec.container_id,
            "--workdir",
            spec.container_workspace,
        ]
        mounts = [
            (spec.workspace_root, spec.container_workspace, not spec.workspace_read_only),
            (spec.agent_dir, spec.container_agent_dir, True),
            (spec.session_dir, spec.container_session_dir, True),
            (spec.logs_dir, spec.container_logs_dir, True),
            (spec.home_dir, spec.container_home_dir, True),
            (spec.tmp_dir, spec.container_tmp_dir, True),
            *((root, spec.container_resource_path(index), False) for index, root in enumerate(spec.resource_roots)),
        ]
        for source, destination, writable in mounts:
            mount = f"type=bind,source={source},destination={destination}"
            if not writable:
                mount += ",readonly"
            args.extend(
                [
                    "--mount",
                    mount,
                ]
            )
        if spec.broker_socket is not None:
            args.extend(
                [
                    "--mount",
                    f"type=bind,source={spec.broker_socket},destination={spec.container_broker_socket}",
                ]
            )
        for key, value in sorted(environment.items()):
            args.extend(["--env", f"{key}={value}"])
        args.extend([self.image, *self.command])
        return args

    @staticmethod
    def _validate_environment(
        environment: Mapping[str, str],
        *,
        spec: ExecutionSpec,
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        fixed_paths = {
            "HOME": spec.container_home_dir,
            "TMPDIR": spec.container_tmp_dir,
            "PI_CODING_AGENT_DIR": spec.container_agent_dir,
            "RAG_IME_PI_AGENT_DIR": spec.container_agent_dir,
            "RAG_IME_PI_SESSION_DIR": spec.container_session_dir,
            "RAG_IME_PI_PLUGINS_DIR": f"{spec.container_agent_dir}/plugins",
            "RAG_IME_PI_PLUGIN_INBOX": f"{spec.container_agent_dir}/plugin-inbox",
            "RAG_IME_APP_SUPPORT_DIR": "/run/paw",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
        for raw_key, raw_value in environment.items():
            key = str(raw_key)
            value = str(raw_value)
            if not _ENV_NAME.fullmatch(key):
                raise TeamExecutionError("execution environment name is invalid")
            if "\x00" in value or "\n" in value or len(value) > 16_384:
                raise TeamExecutionError("execution environment value is invalid")
            if _SECRET_ENV.search(key) and key not in _ALLOWED_SHORT_LIVED_ENV:
                raise TeamExecutionError("provider credentials cannot enter isolated Pi Host")
            if key == 'RAG_IME_PLUGIN_APPROVAL_TOKEN' and not spec.resource_roots:
                raise TeamExecutionError('Package activation requires fixed task resources')
            expected = fixed_paths.get(key)
            if expected is not None and value != expected:
                raise TeamExecutionError(
                    f"isolated execution environment {key} must use its container path"
                )
            if key == "RAG_IME_TEAM_BROKER_SOCKET":
                if spec.broker_socket is None or value != spec.container_broker_socket:
                    raise TeamExecutionError(
                        "isolated execution broker socket must use the scoped mount"
                    )
            result[key] = value
        return result

    def _run_docker(
        self,
        command: list[str],
        *,
        allow_failure: bool,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.stop_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TeamExecutionError(f"Docker lifecycle command failed: {exc}") from exc
        if not allow_failure and result.returncode != 0:
            raise TeamExecutionError((result.stderr or result.stdout or "Docker command failed").strip())
        return result

    @staticmethod
    def _is_missing_container(result: subprocess.CompletedProcess[str]) -> bool:
        text = f"{result.stdout}\n{result.stderr}".lower()
        return result.returncode == 1 and (
            "no such object" in text
            or "no such container" in text
            or "not found" in text
        )

    @staticmethod
    def _poll(process: ExecutionProcess) -> int | None:
        poll = getattr(process, "poll", None)
        result = poll() if callable(poll) else None
        return int(result) if isinstance(result, int) else None

    @classmethod
    def _terminate_client(cls, process: ExecutionProcess) -> None:
        if cls._poll(process) is not None:
            return
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            terminate()
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                wait(timeout=3)
            except subprocess.TimeoutExpired:
                kill = getattr(process, "kill", None)
                if callable(kill):
                    kill()
                wait(timeout=2)
