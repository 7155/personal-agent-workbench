"""Isolated OCI runtime for project preview deployments.

Project previews are deployment-scoped services rather than Agent Sessions.
The trusted server supplies the image and the launcher; a checked-in
``paw-preview.json`` supplies only the application command and fixed HTTP
shape.  The source snapshot is mounted read-only at ``/workspace``.  The
worker copies it into its private ``/tmp/app`` before running preparation and
the application, then exposes the application's loopback port through a Unix
socket in the mounted logs directory.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from http.client import HTTPConnection, HTTPException
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
from threading import RLock
import time
import uuid
from urllib.parse import urlsplit

from ..secure_files import atomic_write, regular_reader
from .execution import (
    DockerExecutionLauncher,
    ExecutionLauncher,
    ExecutionProcess,
    ExecutionReceipt,
    ExecutionSpec,
    TeamExecutionError,
)


__all__ = [
    "PreviewHandle",
    "PreviewManifest",
    "PreviewManifestError",
    "PreviewResponse",
    "PreviewRuntime",
    "TeamPreviewRuntime",
    "PreviewRuntimeError",
    "PreviewUnavailable",
    "load_preview_manifest",
    "read_preview_manifest",
    "validate_preview_manifest",
]


MANIFEST_FILENAME = "paw-preview.json"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_PREVIEW_PORT = 3000
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0
DEFAULT_WORKER_COMMAND = (
    "python3",
    "-m",
    "rag_ime.team.preview_worker",
    "--manifest",
    "/run/paw/agent/paw-preview.json",
)
PREVIEW_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"}
)
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ATTEMPT_ID = re.compile(r"^preview-[0-9a-f]{32}$")
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_ARG_COUNT = 64
_MAX_ARG_CHARS = 4_096
_MAX_HEALTH_PATH_CHARS = 2_048
_MAX_HEADER_COUNT = 64
_MAX_HEADER_CHARS = 8_192
_MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
_MAX_RESPONSE_BODY_BYTES = 16 * 1024 * 1024
_MAX_UNIX_SOCKET_PATH = 100
_MAX_LOG_TAIL_BYTES = 64 * 1024
_DIAGNOSTIC_LOGS = (
    "prepare.stdout.log",
    "prepare.stderr.log",
    "app.stdout.log",
    "app.stderr.log",
)


class PreviewRuntimeError(TeamExecutionError):
    """A preview manifest, worker or HTTP boundary failed closed."""


class PreviewManifestError(PreviewRuntimeError):
    """The source snapshot does not contain a supported preview manifest."""


class PreviewUnavailable(PreviewRuntimeError):
    """The requested preview is not resident or its worker is unavailable."""


def _safe_text(value: object, *, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PreviewManifestError(f"{name} must be a non-empty bounded string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise PreviewManifestError(f"{name} contains control characters")
    return value


def _safe_argv(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_ARG_COUNT:
        raise PreviewManifestError(
            f"{name} must contain one to {_MAX_ARG_COUNT} argv items"
        )
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(
            _safe_text(item, name=f"{name}[{index}]", maximum=_MAX_ARG_CHARS)
        )
    return tuple(result)


def _safe_health_path(value: object) -> str:
    path = _safe_text(value, name="healthPath", maximum=_MAX_HEALTH_PATH_CHARS)
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not path.startswith("/"):
        raise PreviewManifestError("healthPath must be an origin-form path")
    if any(part in {".", ".."} for part in parsed.path.split("/")):
        raise PreviewManifestError("healthPath cannot contain dot path segments")
    return path


@dataclass(frozen=True)
class PreviewManifest:
    """Validated application configuration from one source snapshot."""

    schema_version: int
    command: tuple[str, ...]
    port: int
    health_path: str
    prepare_command: tuple[str, ...] | None = None
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS

    def as_dict(self) -> dict[str, object]:
        timeout: int | float = self.startup_timeout_seconds
        if timeout == int(timeout):
            timeout = int(timeout)
        value: dict[str, object] = {
            "schemaVersion": self.schema_version,
            "command": list(self.command),
            "port": self.port,
            "healthPath": self.health_path,
            "startupTimeoutSeconds": timeout,
        }
        if self.prepare_command is not None:
            value["prepareCommand"] = list(self.prepare_command)
        return value


def validate_preview_manifest(value: object) -> PreviewManifest:
    """Validate the versioned manifest without consulting host configuration."""

    if not isinstance(value, Mapping):
        raise PreviewManifestError("Preview manifest must be a JSON object")
    allowed = {
        "schemaVersion",
        "command",
        "port",
        "healthPath",
        "prepareCommand",
        "startupTimeoutSeconds",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PreviewManifestError("Preview manifest has unknown fields: " + ", ".join(map(str, unknown)))
    version = value.get("schemaVersion")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != MANIFEST_SCHEMA_VERSION
    ):
        raise PreviewManifestError("Preview manifest schemaVersion must be 1")
    command = _safe_argv(value.get("command"), name="command")
    port = value.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise PreviewManifestError("port must be an integer between 1 and 65535")
    health_path = _safe_health_path(value.get("healthPath"))
    prepare_value = value.get("prepareCommand")
    prepare_command = (
        None
        if prepare_value is None
        else _safe_argv(prepare_value, name="prepareCommand")
    )
    timeout_value = value.get(
        "startupTimeoutSeconds", DEFAULT_STARTUP_TIMEOUT_SECONDS
    )
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise PreviewManifestError("startupTimeoutSeconds must be a bounded number")
    timeout = float(timeout_value)
    if not 1.0 <= timeout <= 120.0:
        raise PreviewManifestError("startupTimeoutSeconds must be between 1 and 120")
    return PreviewManifest(
        schema_version=version,
        command=command,
        port=port,
        health_path=health_path,
        prepare_command=prepare_command,
        startup_timeout_seconds=timeout,
    )


def read_preview_manifest(path: Path) -> PreviewManifest:
    """Read one regular manifest through descriptor-relative no-follow I/O."""

    raw_candidate = Path(path).expanduser()
    # macOS exposes temporary directories through the /var -> /private/var
    # compatibility link. Canonicalize only the parent; retaining the final
    # name lets regular_reader enforce O_NOFOLLOW for the manifest itself.
    candidate = raw_candidate.parent.resolve(strict=False) / raw_candidate.name
    if not candidate.is_absolute():
        raise PreviewManifestError("Preview manifest path must be absolute")
    try:
        with regular_reader(candidate) as source:
            raw = source.read(_MAX_MANIFEST_BYTES + 1)
    except FileNotFoundError as exc:
        raise PreviewManifestError("Preview manifest is missing") from exc
    except (OSError, ValueError) as exc:
        raise PreviewManifestError(
            "Preview manifest is unavailable or is not a regular file"
        ) from exc
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise PreviewManifestError("Preview manifest exceeds its size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreviewManifestError("Preview manifest is not valid UTF-8 JSON") from exc
    return validate_preview_manifest(value)


def load_preview_manifest(
    source_dir: Path, *, manifest_name: str = MANIFEST_FILENAME
) -> PreviewManifest:
    """Load a manifest from a real source snapshot directory."""

    source = Path(source_dir).expanduser()
    if not source.is_absolute():
        raise PreviewManifestError("Preview source directory must be absolute")
    if source.is_symlink() or not source.is_dir():
        raise PreviewManifestError("Preview source directory must be a real directory")
    name = _safe_text(manifest_name, name="manifestName", maximum=128)
    if Path(name).name != name or name in {".", ".."}:
        raise PreviewManifestError("manifestName must be one file name")
    return read_preview_manifest(source / name)


class _UnixHTTPConnection(HTTPConnection):
    """HTTPConnection whose only peer is a deployment's Unix socket."""

    def __init__(self, socket_path: Path, *, timeout: float) -> None:
        super().__init__("paw-preview", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        try:
            metadata = self.socket_path.lstat()
        except OSError as exc:
            raise PreviewUnavailable("Preview HTTP socket is unavailable") from exc
        if not stat.S_ISSOCK(metadata.st_mode):
            raise PreviewRuntimeError("Preview HTTP path is not a Unix socket")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.timeout)
            connection.connect(str(self.socket_path))
        except OSError as exc:
            connection.close()
            raise PreviewUnavailable("Preview HTTP socket could not be reached") from exc
        self.sock = connection


@dataclass(frozen=True)
class PreviewResponse:
    """Bounded response returned by a preview deployment."""

    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str, default: str | None = None) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return default

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "headers": list(self.headers),
            "body": self.body,
        }


@dataclass
class PreviewHandle:
    """Resident deployment handle independent of any initiating Session."""

    deployment_id: str
    manifest: PreviewManifest
    spec: ExecutionSpec
    process: ExecutionProcess
    launcher: ExecutionLauncher
    socket_path: Path
    journal_path: Path
    healthy: bool = False
    stopping: bool = False


class _DetachedProcess:
    """Process-shaped value used when recovering a container after a restart."""

    pid = 0
    stdin = None
    stdout = None
    stderr = None

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def terminate(self) -> None:
        return

    def kill(self) -> None:
        return


class PreviewRuntime:
    """Own deployment-scoped preview workers and Unix HTTP relays."""

    def __init__(
        self,
        runtime_root: Path | None = None,
        *,
        root: Path | None = None,
        image: str,
        worker_command: Sequence[str] | None = None,
        docker_executable: str = "docker",
        launcher: ExecutionLauncher | None = None,
        container_user: str | None = None,
        worker_user: str | None = None,
        memory_limit: str = "2g",
        cpu_limit: str = "2.0",
        pids_limit: int = 256,
        request_timeout_seconds: float = 10.0,
        max_request_body_bytes: int = _MAX_REQUEST_BODY_BYTES,
        max_response_body_bytes: int = _MAX_RESPONSE_BODY_BYTES,
    ) -> None:
        if runtime_root is not None and root is not None:
            raise ValueError("Specify only one Preview runtime root")
        supplied_root = runtime_root if runtime_root is not None else root
        if supplied_root is None:
            raise ValueError("Preview runtime root is required")
        root = Path(supplied_root).expanduser()
        if not root.is_absolute() or root == Path("/"):
            raise ValueError("Preview runtime root must be an absolute private directory")
        if root.exists() and root.is_symlink():
            raise ValueError("Preview runtime root must not be a symlink")
        root = root.resolve(strict=False)
        self._ensure_private_dir(root, "Preview runtime root")
        normalized_image = _safe_text(image.strip(), name="image", maximum=256)
        normalized_docker = _safe_text(
            str(docker_executable).strip(), name="dockerExecutable", maximum=256
        )
        command = tuple(str(part) for part in (worker_command or DEFAULT_WORKER_COMMAND))
        if not command or any(
            not part
            or len(part) > _MAX_ARG_CHARS
            or any(ord(char) < 32 for char in part)
            for part in command
        ):
            raise ValueError("Preview worker command must be a bounded safe argv")
        timeout = float(request_timeout_seconds)
        if not 1.0 <= timeout <= 120.0:
            raise ValueError("Preview HTTP timeout is out of bounds")
        if not 1 <= int(max_request_body_bytes) <= _MAX_REQUEST_BODY_BYTES:
            raise ValueError("Preview request body limit is out of bounds")
        if not 1 <= int(max_response_body_bytes) <= _MAX_RESPONSE_BODY_BYTES:
            raise ValueError("Preview response body limit is out of bounds")
        self.runtime_root = root
        # Keep both names available to the server-facing adapter.  ``root``
        # and ``worker_user`` were used by early preview configuration
        # call-sites, while the runtime internally uses the more explicit
        # names below.
        self.root = root
        # Hashing shortens deployment components.  AF_UNIX still has a
        # platform path limit; _new_spec rejects an overlong configured root
        # rather than falling back to a less-scoped transport.
        self.deployments_root = root / "d"
        self.journal_root = root / "journal"
        self.diagnostics_root = root / "diagnostics"
        self._ensure_private_dir(self.deployments_root, "Preview deployments root")
        self._ensure_private_dir(self.journal_root, "Preview journal root")
        self._ensure_private_dir(self.diagnostics_root, "Preview diagnostics root")
        self.image = normalized_image
        self.docker_executable = normalized_docker
        self.worker_command = command
        self.container_user = (
            container_user
            or worker_user
            or self._default_container_user()
        )
        self.worker_user = self.container_user
        self.memory_limit = str(memory_limit)
        self.cpu_limit = str(cpu_limit)
        self.pids_limit = int(pids_limit)
        self.request_timeout_seconds = timeout
        self.max_request_body_bytes = int(max_request_body_bytes)
        self.max_response_body_bytes = int(max_response_body_bytes)
        self.launcher = launcher or DockerExecutionLauncher(
            image=self.image,
            command=self.worker_command,
            docker_executable=self.docker_executable,
        )
        self._handles: dict[str, PreviewHandle] = {}
        self._lock = RLock()
        self._closed = False

    @staticmethod
    def _ensure_private_dir(path: Path, label: str) -> None:
        if path.exists() and path.is_symlink():
            raise ValueError(f"{label} must not be a symlink")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"{label} is unavailable")
        os.chmod(path, 0o700)

    @staticmethod
    def _default_container_user() -> str:
        uid, gid = os.getuid(), os.getgid()
        if uid <= 0 or gid <= 0:
            raise ValueError(
                "Preview runtime requires a non-root service account and primary group"
            )
        return f"{uid}:{gid}"

    @staticmethod
    def _deployment_id(value: str) -> str:
        deployment_id = str(value).strip()
        if not _DEPLOYMENT_ID.fullmatch(deployment_id):
            raise PreviewRuntimeError("Preview deployment id is invalid")
        return deployment_id

    def _deployment_root(self, deployment_id: str) -> Path:
        normalized = self._deployment_id(deployment_id)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        return self.deployments_root / digest

    def _journal_path(self, deployment_id: str) -> Path:
        return self.journal_root / (self._deployment_id(deployment_id) + ".json")

    def _diagnostics_path(self, deployment_id: str) -> Path:
        return self.diagnostics_root / self._deployment_id(deployment_id)

    @staticmethod
    def _preview_environment(spec: ExecutionSpec) -> dict[str, str]:
        # Do not use container_environment_for(): preview workers do not need
        # PAW's Agent gateway variables.  Keeping this list explicit prevents
        # model, Team, host HOME and provider credentials from entering the app.
        return {
            "HOME": spec.container_home_dir,
            "TMPDIR": spec.container_tmp_dir,
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "RAG_IME_APP_SUPPORT_DIR": "/run/paw",
            "RAG_IME_TEAM_PREVIEW": "1",
        }

    def _journal_payload(
        self,
        handle: PreviewHandle | None,
        *,
        deployment_id: str,
        manifest: PreviewManifest,
        spec: ExecutionSpec,
        state: str,
        error: str = "",
        receipt: ExecutionReceipt | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "schemaVersion": "rag-ime.team-preview-journal.v1",
            "deploymentId": deployment_id,
            "state": state,
            "image": self.image,
            "workerCommand": list(self.worker_command),
            "manifest": manifest.as_dict(),
            "sourceDir": str(spec.workspace_root),
            "attemptRoot": str(spec.scope_root),
            "attemptId": spec.attempt_id,
            "sessionId": spec.session_id,
            "containerId": spec.container_id,
            "agentDir": str(spec.agent_dir),
            "sessionDir": str(spec.session_dir),
            "logsDir": str(spec.logs_dir),
            "homeDir": str(spec.home_dir),
            "tmpDir": str(spec.tmp_dir),
            "diagnosticsDir": str(self._diagnostics_path(deployment_id)),
            "containerUser": spec.container_user,
            "updatedAtMs": int(time.time() * 1000),
        }
        if handle is not None:
            value["healthy"] = handle.healthy
        value["started"] = handle is not None
        if error:
            value["error"] = error[:512]
        if receipt is not None:
            value["receipt"] = receipt.as_dict()
        return value

    def _write_journal(self, path: Path, value: Mapping[str, object]) -> None:
        raw_path = Path(path)
        safe_path = raw_path.parent.resolve(strict=False) / raw_path.name
        atomic_write(
            safe_path,
            (json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )

    def _read_journal(self, path: Path) -> dict[str, object] | None:
        raw_path = Path(path)
        safe_path = raw_path.parent.resolve(strict=False) / raw_path.name
        try:
            with regular_reader(safe_path) as source:
                raw = source.read(_MAX_MANIFEST_BYTES + 1)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise PreviewRuntimeError("Preview journal is unavailable") from exc
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise PreviewRuntimeError("Preview journal exceeds its size limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PreviewRuntimeError("Preview journal is corrupt") from exc
        if not isinstance(value, dict):
            raise PreviewRuntimeError("Preview journal must be an object")
        return value

    def _new_spec(
        self,
        deployment_id: str,
        source_dir: Path,
        manifest: PreviewManifest,
    ) -> tuple[ExecutionSpec, Path]:
        deployment_root = self._deployment_root(deployment_id)
        deployment_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        attempt_id = "preview-" + uuid.uuid4().hex
        attempt_root = deployment_root / "a" / uuid.uuid4().hex[:12]
        attempt_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        container_id = "paw-preview-" + deployment_id + "-" + uuid.uuid4().hex[:16]
        spec = ExecutionSpec(
            attempt_id=attempt_id,
            session_id="preview:" + deployment_id,
            scope_root=attempt_root,
            workspace_root=source_dir,
            agent_dir=attempt_root / "g",
            session_dir=attempt_root / "s",
            logs_dir=attempt_root / "l",
            home_dir=attempt_root / "h",
            tmp_dir=attempt_root / "t",
            container_id=container_id,
            runtime_image=self.image,
            runtime_command=self.worker_command,
            workspace_read_only=True,
            memory_limit=self.memory_limit,
            cpu_limit=self.cpu_limit,
            pids_limit=self.pids_limit,
            container_user=self.container_user,
        )
        socket_path = spec.logs_dir / "preview.sock"
        if len(os.fsencode(str(socket_path))) > _MAX_UNIX_SOCKET_PATH:
            self._cleanup_attempt(spec.scope_root)
            raise PreviewRuntimeError(
                "Preview runtime root is too deep for its Unix HTTP socket path"
            )
        # The worker gets a normalized copy rather than opening a source file
        # after the read-only snapshot is mounted.
        spec.agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(
            spec.agent_dir / MANIFEST_FILENAME,
            (json.dumps(manifest.as_dict(), separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        return spec, self._journal_path(deployment_id)

    def _request_handle(
        self,
        handle: PreviewHandle,
        method: str,
        path: str,
        headers: Mapping[str, str] | None,
        body: bytes,
        *,
        timeout: float | None = None,
    ) -> PreviewResponse:
        normalized_method = str(method).upper()
        if normalized_method not in PREVIEW_METHODS:
            raise PreviewRuntimeError("Preview HTTP method is unsupported")
        if not isinstance(body, bytes):
            raise PreviewRuntimeError("Preview HTTP body must be bytes")
        if len(body) > self.max_request_body_bytes:
            raise PreviewRuntimeError("Preview HTTP request body exceeds its limit")
        target = str(path)
        parsed = urlsplit(target)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not target.startswith("/")
            or any(ord(char) < 32 or ord(char) == 127 for char in target)
            or len(target) > 8_192
        ):
            raise PreviewRuntimeError("Preview HTTP path must be a bounded origin path")
        if any(part in {".", ".."} for part in parsed.path.split("/")):
            raise PreviewRuntimeError("Preview HTTP path cannot contain dot segments")
        normalized_headers: dict[str, str] = {}
        supplied = headers or {}
        if not isinstance(supplied, Mapping) or len(supplied) > _MAX_HEADER_COUNT:
            raise PreviewRuntimeError("Preview HTTP headers exceed their limit")
        total_header_chars = 0
        for raw_name, raw_value in supplied.items():
            name = str(raw_name)
            value = str(raw_value)
            lower = name.lower()
            if not name or any(ord(char) < 33 or ord(char) == 127 for char in name):
                raise PreviewRuntimeError("Preview HTTP header name is invalid")
            if any(ord(char) in {10, 13} for char in value) or len(value) > _MAX_HEADER_CHARS:
                raise PreviewRuntimeError("Preview HTTP header value is invalid")
            if lower in _HOP_BY_HOP_HEADERS or lower in {"host", "content-length"}:
                continue
            normalized_headers[name] = value
            total_header_chars += len(name) + len(value)
        if total_header_chars > 64 * 1024:
            raise PreviewRuntimeError("Preview HTTP headers exceed their total limit")
        connection = _UnixHTTPConnection(
            handle.socket_path,
            timeout=timeout or self.request_timeout_seconds,
        )
        try:
            connection.request(
                normalized_method,
                target,
                body=body,
                headers=normalized_headers,
            )
            response = connection.getresponse()
            if response.status == 101 or response.getheader("Upgrade"):
                raise PreviewRuntimeError("Preview WebSockets are unsupported")
            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    if int(content_length) > self.max_response_body_bytes:
                        raise PreviewRuntimeError("Preview HTTP response exceeds its limit")
                except ValueError as exc:
                    raise PreviewRuntimeError("Preview response Content-Length is invalid") from exc
            result_body = response.read(self.max_response_body_bytes + 1)
            if len(result_body) > self.max_response_body_bytes:
                raise PreviewRuntimeError("Preview HTTP response exceeds its limit")
            response_headers = [
                (str(name), str(value))
                for name, value in response.getheaders()
                if str(name).lower() not in _HOP_BY_HOP_HEADERS
            ]
            return PreviewResponse(response.status, response_headers, result_body)
        except (OSError, HTTPException) as exc:
            raise PreviewUnavailable("Preview HTTP request could not reach the worker") from exc
        finally:
            connection.close()

    def _wait_healthy(self, handle: PreviewHandle) -> None:
        deadline = time.monotonic() + handle.manifest.startup_timeout_seconds
        last_error = "health endpoint did not respond"
        while True:
            if handle.stopping or self._closed:
                raise PreviewRuntimeError("Preview deployment was stopped during startup")
            if handle.process.poll() is not None:
                raise PreviewRuntimeError("Preview worker exited before becoming healthy")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreviewRuntimeError(
                    "Preview health check timed out: " + last_error
                )
            try:
                response = self._request_handle(
                    handle,
                    "GET",
                    handle.manifest.health_path,
                    None,
                    b"",
                    timeout=min(self.request_timeout_seconds, max(0.1, remaining)),
                )
                if 200 <= response.status < 400:
                    return
                last_error = f"health endpoint returned HTTP {response.status}"
            except PreviewRuntimeError as exc:
                last_error = str(exc)
            time.sleep(min(0.05, max(0.0, remaining)))

    @staticmethod
    def _cleanup_attempt(root: Path) -> None:
        if root.is_symlink():
            root.unlink()
        elif root.exists():
            shutil.rmtree(root)

    def _retain_diagnostics(self, deployment_id: str, spec: ExecutionSpec) -> None:
        """Copy bounded worker tails before the private attempt is removed.

        This is an operator-local projection.  It is deliberately best effort:
        inability to copy a diagnostic must never turn a verified container
        stop into a false lifecycle failure.  Each known stream is capped at
        the same limit used by the worker, keeping the deployment projection
        at most 256 KiB.
        """

        logs_dir = Path(spec.logs_dir)
        if logs_dir.is_symlink() or not logs_dir.is_dir():
            return
        destination = self._diagnostics_path(deployment_id)
        try:
            self._ensure_private_dir(destination, "Preview diagnostics deployment root")
        except (OSError, ValueError):
            return
        for filename in _DIAGNOSTIC_LOGS:
            source = logs_dir / filename
            safe_source = source.parent.resolve(strict=False) / source.name
            try:
                with regular_reader(safe_source) as stream:
                    raw = stream.read(_MAX_LOG_TAIL_BYTES + 1)
                if len(raw) > _MAX_LOG_TAIL_BYTES:
                    raw = raw[-_MAX_LOG_TAIL_BYTES:]
                atomic_write(destination / filename, raw)
            except (OSError, ValueError):
                # A missing stream is normal when a command never reached the
                # corresponding phase; a symlink or malformed entry is not
                # copied into the controller-owned diagnostics projection.
                continue

    def _stop_handle(self, handle: PreviewHandle) -> ExecutionReceipt:
        receipt = handle.launcher.stop(handle.spec, handle.process)
        if not receipt.verified:
            raise PreviewRuntimeError(
                "Preview container stop was not verified: " + (receipt.detail or "unknown state")
            )
        return receipt

    def start(self, deployment_id: str, source_dir: Path) -> PreviewHandle:
        deployment_id = self._deployment_id(deployment_id)
        source = Path(source_dir).expanduser()
        if not source.is_absolute():
            raise PreviewRuntimeError("Preview source directory must be absolute")
        if source.is_symlink() or not source.is_dir():
            raise PreviewRuntimeError("Preview source directory must be a real directory")
        source = source.resolve(strict=True)
        manifest = load_preview_manifest(source)
        with self._lock:
            if self._closed:
                raise PreviewRuntimeError("Preview runtime is shut down")
            if deployment_id in self._handles:
                raise PreviewRuntimeError("Preview deployment is already resident")
            prior = self._read_journal(self._journal_path(deployment_id))
            if prior is not None and str(prior.get("state") or "") in {
                "starting",
                "running",
                "healthy",
                "ready",
                "retained",
                "recovery_required",
            }:
                self.recover(deployment_id)
            spec, journal_path = self._new_spec(deployment_id, source, manifest)
            self._write_journal(
                journal_path,
                self._journal_payload(
                    None,
                    deployment_id=deployment_id,
                    manifest=manifest,
                    spec=spec,
                    state="starting",
                ),
            )
            try:
                process = self.launcher.start(
                    spec,
                    self.worker_command,
                    self._preview_environment(spec),
                )
                handle = PreviewHandle(
                    deployment_id=deployment_id,
                    manifest=manifest,
                    spec=spec,
                    process=process,
                    launcher=self.launcher,
                    socket_path=spec.logs_dir / "preview.sock",
                    journal_path=journal_path,
                )
                self._handles[deployment_id] = handle
                self._write_journal(
                    journal_path,
                    self._journal_payload(
                        handle,
                        deployment_id=deployment_id,
                        manifest=manifest,
                        spec=spec,
                        state="running",
                    ),
                )
            except Exception as exc:
                self._handles.pop(deployment_id, None)
                self._write_journal(
                    journal_path,
                    self._journal_payload(
                        None,
                        deployment_id=deployment_id,
                        manifest=manifest,
                        spec=spec,
                        state="failed",
                        error=str(exc),
                    ),
                )
                self._cleanup_attempt(spec.scope_root)
                raise PreviewRuntimeError(
                    "Preview deployment failed to start: " + str(exc)
                ) from exc

        assert handle is not None
        try:
            # Health probing is deliberately outside the runtime-wide lock so
            # another deployment can still receive requests or be stopped.
            self._wait_healthy(handle)
            with self._lock:
                if self._handles.get(deployment_id) is not handle or handle.stopping:
                    raise PreviewRuntimeError("Preview deployment was stopped during startup")
                handle.healthy = True
                self._write_journal(
                    journal_path,
                    self._journal_payload(
                        handle,
                        deployment_id=deployment_id,
                        manifest=manifest,
                        spec=spec,
                        state="healthy",
                    ),
                )
            return handle
        except Exception as exc:
            cleanup_error: Exception | None = None
            with self._lock:
                current = self._handles.get(deployment_id)
                should_stop = current is handle and not handle.stopping
                if should_stop:
                    handle.stopping = True
                    self._handles.pop(deployment_id, None)
            if should_stop:
                try:
                    receipt = self._stop_handle(handle)
                    self._retain_diagnostics(deployment_id, spec)
                    self._write_journal(
                        journal_path,
                        self._journal_payload(
                            handle,
                            deployment_id=deployment_id,
                            manifest=manifest,
                            spec=spec,
                            state="failed",
                            error=str(exc),
                            receipt=receipt,
                        ),
                    )
                    self._cleanup_attempt(spec.scope_root)
                except Exception as stop_error:
                    cleanup_error = stop_error
                    handle.stopping = False
                    with self._lock:
                        self._handles[deployment_id] = handle
                    self._write_journal(
                        journal_path,
                        self._journal_payload(
                            handle,
                            deployment_id=deployment_id,
                            manifest=manifest,
                            spec=spec,
                            state="recovery_required",
                            error=f"{exc}; cleanup: {stop_error}",
                        ),
                    )
            raise PreviewRuntimeError(
                "Preview deployment failed: "
                + str(exc)
                + (
                    "; container cleanup was not verified: " + str(cleanup_error)
                    if cleanup_error is not None
                    else ""
                )
            ) from exc

    def request(
        self,
        deployment_id: str,
        method: str,
        path: str,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> PreviewResponse:
        deployment_id = self._deployment_id(deployment_id)
        with self._lock:
            handle = self._handles.get(deployment_id)
            closed = self._closed
        if (
            closed
            or handle is None
            or handle.stopping
            or not handle.healthy
            or handle.process.poll() is not None
        ):
            raise PreviewUnavailable("Preview deployment is not healthy")
        return self._request_handle(handle, method, path, headers, body)

    def stop(self, deployment_id: str) -> ExecutionReceipt:
        deployment_id = self._deployment_id(deployment_id)
        with self._lock:
            handle = self._handles.get(deployment_id)
            if handle is not None:
                if handle.stopping:
                    raise PreviewRuntimeError("Preview deployment stop is already in progress")
                handle.stopping = True
        if handle is not None:
            try:
                receipt = self._stop_handle(handle)
            except Exception:
                handle.stopping = False
                raise
            with self._lock:
                if self._handles.get(deployment_id) is handle:
                    self._handles.pop(deployment_id, None)
            self._write_journal(
                handle.journal_path,
                self._journal_payload(
                    handle,
                    deployment_id=deployment_id,
                    manifest=handle.manifest,
                    spec=handle.spec,
                    state="stopped",
                    receipt=receipt,
                ),
            )
            self._cleanup_attempt(handle.spec.scope_root)
            return receipt
        record = self._read_journal(self._journal_path(deployment_id))
        if record is None:
            raise PreviewUnavailable("Preview deployment is not resident")
        state = str(record.get("state") or "")
        if state in {"failed", "stopped", "recovered"}:
            saved = self._saved_verified_receipt(deployment_id, record)
            if saved is not None:
                return saved
            if state == "failed" and record.get("started") is False:
                return self._absent_receipt(deployment_id, record)
        if state not in {
            "starting",
            "running",
            "healthy",
            "ready",
            "retained",
            "recovery_required",
        }:
            raise PreviewUnavailable("Preview deployment is not resident")
        return self._stop_journal_record(deployment_id, record, state="recovered")

    def _saved_verified_receipt(
        self,
        deployment_id: str,
        record: Mapping[str, object],
    ) -> ExecutionReceipt | None:
        # A receipt is an idempotency record, not authorization to remove an
        # arbitrary container.  Reconstruct the server-generated spec first
        # and require every identity field in the saved receipt to match it.
        # This also rejects a journal whose trusted image/worker command was
        # replaced while the runtime was down.
        spec = self._spec_from_journal(deployment_id, record)
        raw = record.get("receipt")
        if not isinstance(raw, Mapping) or raw.get("verified") is not True:
            return None
        if raw.get("schemaVersion") != "rag-ime.team-execution-receipt.v1":
            raise PreviewRuntimeError("Preview journal receipt schema is invalid")
        if raw.get("action") != "stop":
            raise PreviewRuntimeError("Preview journal receipt action is invalid")
        if any(
            raw.get(key) != expected
            for key, expected in (
                ("attemptId", spec.attempt_id),
                ("sessionId", spec.session_id),
                ("containerId", spec.container_id),
            )
        ):
            return None
        state = raw.get("state")
        if state not in {"stopped", "absent"}:
            return None
        exit_code = raw.get("exitCode")
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            return None
        return ExecutionReceipt(
            attempt_id=spec.attempt_id,
            session_id=spec.session_id,
            container_id=spec.container_id,
            action="stop",
            state=state,
            verified=True,
            detail=str(raw.get("detail") or ""),
            exit_code=exit_code,
        )

    def _absent_receipt(
        self, deployment_id: str, record: Mapping[str, object]
    ) -> ExecutionReceipt:
        manifest = validate_preview_manifest(record.get("manifest"))
        spec = self._spec_from_journal(deployment_id, record)
        receipt = ExecutionReceipt(
            attempt_id=spec.attempt_id,
            session_id=spec.session_id,
            container_id=spec.container_id,
            action="stop",
            state="absent",
            verified=True,
            detail="Preview worker did not start; no container required removal",
        )
        self._write_journal(
            self._journal_path(deployment_id),
            self._journal_payload(
                None,
                deployment_id=deployment_id,
                manifest=manifest,
                spec=spec,
                state="recovered",
                receipt=receipt,
            ),
        )
        self._cleanup_attempt(spec.scope_root)
        return receipt

    def _stop_journal_record(
        self,
        deployment_id: str,
        record: Mapping[str, object],
        *,
        state: str,
    ) -> ExecutionReceipt:
        """Remove a worker left by an earlier process, with no new launch."""

        spec = self._spec_from_journal(deployment_id, record)
        manifest = validate_preview_manifest(record.get("manifest"))
        receipt = self.launcher.stop(spec, _DetachedProcess())
        if not receipt.verified:
            raise PreviewRuntimeError(
                "Preview recovery stop was not verified: "
                + (receipt.detail or "unknown state")
            )
        self._retain_diagnostics(deployment_id, spec)
        self._write_journal(
            self._journal_path(deployment_id),
            self._journal_payload(
                None,
                deployment_id=deployment_id,
                manifest=manifest,
                spec=spec,
                state=state,
                receipt=receipt,
            ),
        )
        self._cleanup_attempt(spec.scope_root)
        return receipt

    def is_running(self, deployment_id: str) -> bool:
        deployment_id = self._deployment_id(deployment_id)
        with self._lock:
            handle = self._handles.get(deployment_id)
        return handle is not None and handle.process.poll() is None

    def handles(self) -> Mapping[str, PreviewHandle]:
        with self._lock:
            return dict(self._handles)

    def recover(self, deployment_id: str | None = None) -> list[ExecutionReceipt]:
        """Stop active journal records after a server restart, without launch."""

        if deployment_id is not None:
            deployment_ids = [self._deployment_id(deployment_id)]
        else:
            deployment_ids = [
                path.stem
                for path in self.journal_root.glob("*.json")
                if _DEPLOYMENT_ID.fullmatch(path.stem)
            ]
        receipts: list[ExecutionReceipt] = []
        for current_id in deployment_ids:
            with self._lock:
                handle = self._handles.get(current_id)
            if handle is not None:
                receipts.append(self.stop(current_id))
                continue
            journal_path = self._journal_path(current_id)
            record = self._read_journal(journal_path)
            if record is None or str(record.get("state") or "") not in {
                "starting",
                "running",
                "healthy",
                "recovery_required",
            }:
                continue
            receipt = self._stop_journal_record(
                current_id, record, state="recovered"
            )
            receipts.append(receipt)
        return receipts

    def _spec_from_journal(
        self, deployment_id: str, record: Mapping[str, object]
    ) -> ExecutionSpec:
        if record.get("schemaVersion") != "rag-ime.team-preview-journal.v1":
            raise PreviewRuntimeError("Preview journal schema is invalid")
        if record.get("deploymentId") != deployment_id:
            raise PreviewRuntimeError("Preview journal deployment identity does not match its path")
        if record.get("image") != self.image or tuple(record.get("workerCommand") or ()) != self.worker_command:
            raise PreviewRuntimeError("Preview journal does not match trusted worker configuration")
        raw_attempt_root = Path(str(record.get("attemptRoot") or ""))
        if raw_attempt_root.is_symlink():
            raise PreviewRuntimeError("Preview journal attempt root must not be a symlink")
        deployment_root = self._deployment_root(deployment_id).resolve(strict=False)
        attempt_root = raw_attempt_root.resolve(strict=False)
        if not attempt_root.is_relative_to(deployment_root):
            raise PreviewRuntimeError("Preview journal attempt root escaped its deployment")
        attempt_parts = attempt_root.relative_to(deployment_root).parts
        if (
            len(attempt_parts) != 2
            or attempt_parts[0] != "a"
            or not re.fullmatch(r"[0-9a-f]{12}", attempt_parts[1])
        ):
            raise PreviewRuntimeError("Preview journal attempt root is invalid")
        attempt_id = str(record.get("attemptId") or "")
        if not _ATTEMPT_ID.fullmatch(attempt_id):
            raise PreviewRuntimeError("Preview journal attempt identity is invalid")
        session_id = str(record.get("sessionId") or "")
        if session_id != "preview:" + deployment_id:
            raise PreviewRuntimeError("Preview journal session identity is invalid")
        container_id = str(record.get("containerId") or "")
        if not re.fullmatch(
            r"paw-preview-" + re.escape(deployment_id) + r"-[0-9a-f]{16}",
            container_id,
        ):
            raise PreviewRuntimeError("Preview journal container identity is invalid")
        expected_state_paths = {
            "agentDir": attempt_root / "g",
            "sessionDir": attempt_root / "s",
            "logsDir": attempt_root / "l",
            "homeDir": attempt_root / "h",
            "tmpDir": attempt_root / "t",
        }
        for field_name, expected in expected_state_paths.items():
            raw_path = Path(str(record.get(field_name) or "")).expanduser()
            if raw_path.is_symlink():
                raise PreviewRuntimeError("Preview journal state path must not be a symlink")
            if raw_path.resolve(strict=False) != expected:
                raise PreviewRuntimeError(
                    "Preview journal state path escaped its attempt"
                )
        return ExecutionSpec(
            attempt_id=attempt_id,
            session_id=session_id,
            scope_root=attempt_root,
            workspace_root=Path(str(record.get("sourceDir") or "/")),
            agent_dir=Path(str(record.get("agentDir") or attempt_root / "agent")),
            session_dir=Path(str(record.get("sessionDir") or attempt_root / "sessions")),
            logs_dir=Path(str(record.get("logsDir") or attempt_root / "logs")),
            home_dir=Path(str(record.get("homeDir") or attempt_root / "home")),
            tmp_dir=Path(str(record.get("tmpDir") or attempt_root / "tmp")),
            container_id=container_id,
            runtime_image=self.image,
            runtime_command=self.worker_command,
            workspace_read_only=True,
            container_user=str(record.get("containerUser") or self.container_user),
        )

    def shutdown(self) -> None:
        """Stop resident deployments and recover owned active journals."""

        # Set the barrier before taking a snapshot of handles.  A start that
        # has not acquired the launch lock will fail before Popen; one that
        # already registered a handle is stopped below and cannot publish a
        # late healthy state.
        with self._lock:
            self._closed = True
        first_error: Exception | None = None
        for deployment_id in list(self.handles()):
            try:
                self.stop(deployment_id)
            except Exception as exc:
                first_error = first_error or exc
        try:
            self.recover()
        except Exception as exc:
            first_error = first_error or exc
        if first_error is not None:
            raise PreviewRuntimeError("Preview runtime shutdown was incomplete") from first_error


# Parent TeamPreviewManager and the operator CLI use the product-facing name;
# retain the shorter class name for direct runtime consumers and tests.
TeamPreviewRuntime = PreviewRuntime
