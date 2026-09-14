#!/usr/bin/env python3
"""Run a real Docker/OCI canary for the reviewed PAW Team worker image.

The canary intentionally does not discover, build, or pull an image.  The
operator must pass ``--image`` for the reviewed Pi payload.  When a daemon is
available the script uses :class:`DockerExecutionLauncher` for real container
creation and verified removal, a temporary Unix broker for deterministic
localhost-only model/tool responses, and two temporary actor workspaces.

The Pi protocol stage is deliberately optional in the result only when the
operator passes ``--skip-pi``.  A skipped stage, or any other unverified
boundary, returns a non-zero status; the script never turns a kernel smoke test
into a claim that Pi or Team grants were exercised.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import queue
import selectors
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

# Keep the canary directly runnable from a source checkout without installing
# PAW as a package. This only imports the existing launcher adapter; it does
# not mutate the checkout or discover a different Pi runtime.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from rag_ime.team.execution import (
    DockerExecutionLauncher,
    ExecutionProcess,
    ExecutionReceipt,
    ExecutionSpec,
    TeamExecutionError,
)


DEFAULT_PI_COMMAND = (
    "python3",
    "-m",
    "rag_ime.team.worker_proxy",
    "--",
    "node",
    "/opt/pi/runtime-host/cli.mjs",
)
CANARY_MODEL = "paw-team-oci-canary"
CANARY_PROVIDER = "canary"
CANARY_TOKEN_PREFIX = "paw-canary-token-"
PI_SESSION_ID = "oci-canary-session"
PI_SESSION_FILE = "oci-canary-session.jsonl"
PI_TOOL_NAME = "canary_echo"
_MAX_REPORT_DETAIL = 800
_REQUIRED_MOUNTS = {
    "/workspace",
    "/run/paw/agent",
    "/run/paw/sessions",
    "/run/paw/logs",
    "/run/paw/home",
    "/tmp",
    "/run/paw/broker.sock",
}


def _safe_detail(value: object) -> str:
    text = " ".join(str(value or "").split())
    return text[:_MAX_REPORT_DETAIL]


def _json_response(handler: Any, status: int, value: Mapping[str, object]) -> None:
    encoded = json.dumps(
        dict(value), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(encoded)
    handler.close_connection = True


class _ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16


class _FakeModelBroker:
    """A deterministic broker/upstream reachable only through one Unix socket."""

    def __init__(self, socket_path: Path, *, token: str) -> None:
        self.socket_path = socket_path
        self.token = token
        self.model_requests: list[dict[str, object]] = []
        self.tool_requests: list[dict[str, object]] = []
        self._lock = threading.Lock()
        self._server: _ThreadedUnixServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise RuntimeError("canary broker socket already exists")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                owner._handle_http(self)

        self._server = _ThreadedUnixServer(str(self.socket_path), Handler)
        os.chmod(self.socket_path, 0o600)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="paw-team-oci-canary-broker",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self.socket_path.unlink(missing_ok=True)
        self._server = None
        self._thread = None

    def _record_model(self, payload: Mapping[str, object]) -> None:
        with self._lock:
            self.model_requests.append(dict(payload))

    def _record_tool(self, payload: Mapping[str, object]) -> None:
        with self._lock:
            self.tool_requests.append(dict(payload))

    def _handle_http(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            raw_length = handler.headers.get("Content-Length")
            if raw_length is None:
                raise ValueError("Content-Length is required")
            length = int(raw_length)
            if length < 0 or length > 2 * 1024 * 1024:
                raise ValueError("invalid body length")
            body = handler.rfile.read(length)
            if len(body) != length:
                raise ValueError("request body was truncated")
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("JSON object required")
            self._handle(handler, handler.path, payload)
        except Exception as exc:
            _json_response(handler, 400, {"ok": False, "error": _safe_detail(exc)})

    def _model_response(self, payload: Mapping[str, object]) -> bytes:
        self._record_model(payload)
        messages = payload.get("messages")
        message_rows = messages if isinstance(messages, list) else []
        last_tool_name = ""
        for item in reversed(message_rows):
            if not isinstance(item, Mapping) or item.get("role") != "assistant":
                continue
            calls = item.get("tool_calls")
            if not isinstance(calls, list):
                break
            for call in reversed(calls):
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function")
                if isinstance(function, Mapping):
                    last_tool_name = str(function.get("name") or "")
                    break
            break
        completion_id = "canary-completion-" + uuid.uuid4().hex[:16]
        created = int(time.time())
        if last_tool_name == PI_TOOL_NAME:
            chunks = [
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": CANARY_MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "CANARY_PI_OK"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": CANARY_MODEL,
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 8,
                        "completion_tokens": 1,
                        "total_tokens": 9,
                    },
                },
            ]
        else:
            next_tool = {
                "": "tool_search",
                "tool_search": "tool_load",
                "tool_load": PI_TOOL_NAME,
            }.get(last_tool_name, "tool_search")
            next_arguments = {
                "tool_search": '{"query":"canary_echo","limit":4}',
                "tool_load": '{"name":"canary_echo"}',
                PI_TOOL_NAME: '{"value":"oci"}',
            }[next_tool]
            call_id = f"canary-call-{len(self.model_requests)}"
            chunks = [
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": CANARY_MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": CANARY_MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": call_id,
                                        "type": "function",
                                        "function": {
                                            "name": next_tool,
                                            "arguments": next_arguments,
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": CANARY_MODEL,
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                    ],
                    "usage": {
                        "prompt_tokens": 8,
                        "completion_tokens": 2,
                        "total_tokens": 10,
                    },
                },
            ]
        body = b"".join(
            (
                b"data: "
                + json.dumps(chunk, separators=(",", ":")).encode("utf-8")
                + b"\n\n"
            )
            for chunk in chunks
        ) + b"data: [DONE]\n\n"
        return body

    def _handle(self, handler: Any, path: str, payload: Mapping[str, object]) -> None:
        token = str(handler.headers.get("X-RAG-IME-Agent-Token") or "")
        if not token:
            token = str(handler.headers.get("Authorization") or "").removeprefix(
                "Bearer "
            )
        if token != self.token:
            _json_response(handler, 401, {"ok": False, "error": "invalid canary token"})
            return
        if path == "/v1/chat/completions":
            model = str(payload.get("model") or "")
            if model != CANARY_MODEL:
                _json_response(handler, 403, {"ok": False, "error": "model not allowed"})
                return
            body = self._model_response(payload)
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Connection", "close")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            handler.close_connection = True
            return
        if path == "/api/agent/tool/execute":
            self._record_tool(payload)
            if payload.get("tool") != PI_TOOL_NAME:
                _json_response(handler, 403, {"ok": False, "error": "tool not allowed"})
                return
            _json_response(
                handler,
                200,
                {
                    "ok": True,
                    "result": {
                        "schemaVersion": "paw-team-oci-canary-tool-result.v1",
                        "summary": "deterministic canary tool completed",
                        "value": "OCI_TOOL_OK",
                    },
                },
            )
            return
        _json_response(handler, 404, {"ok": False, "error": "canary path not found"})


@dataclass
class _RunningContainer:
    label: str
    spec: ExecutionSpec
    launcher: DockerExecutionLauncher
    process: ExecutionProcess
    stopped: bool = False
    receipt: ExecutionReceipt | None = None


class _PiPipe:
    """Minimal protocol-2 client used only to exercise the real Pi Host."""

    def __init__(self, process: ExecutionProcess) -> None:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("Pi canary process has no JSONL pipes")
        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self._messages: queue.Queue[object] = queue.Queue()
        self._events: list[dict[str, object]] = []
        self.stderr_lines: list[str] = []
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        assert self.stdout is not None
        try:
            for line in self.stdout:
                try:
                    value = json.loads(line.decode("utf-8"))
                except Exception as exc:
                    self._messages.put({"_canary_parse_error": _safe_detail(exc)})
                else:
                    self._messages.put(value)
        finally:
            self._messages.put(None)

    def _read_stderr(self) -> None:
        if self.stderr is None:
            return
        for line in self.stderr:
            self.stderr_lines.append(_safe_detail(line.decode("utf-8", errors="replace")))
            del self.stderr_lines[:-20]

    def _next(self, deadline: float) -> Mapping[str, object]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Pi protocol canary timed out")
        value = self._messages.get(timeout=remaining)
        if value is None:
            raise RuntimeError(
                "Pi Host closed its protocol stream"
                + (": " + self.stderr_lines[-1] if self.stderr_lines else "")
            )
        if not isinstance(value, Mapping):
            raise RuntimeError("Pi Host returned a non-object protocol record")
        return value

    def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float,
    ) -> dict[str, object]:
        request_id = "canary-request-" + uuid.uuid4().hex
        record = {
            "protocolVersion": "2",
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        self.stdin.write(
            json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        self.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            value = self._next(deadline)
            if str(value.get("protocolVersion") or "") != "2":
                raise RuntimeError("Pi Host did not return protocolVersion 2")
            if value.get("id") != request_id:
                self._events.append(dict(value))
                continue
            if value.get("ok") is not True:
                raise RuntimeError(f"Pi {method} rejected: {_safe_detail(value)}")
            result = value.get("result")
            return dict(result) if isinstance(result, Mapping) else {}

    def wait_for_settled(self, *, session_id: str, timeout: float) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        # A Host may publish ``agent_settled`` before the response to the
        # request which started the turn.  ``request`` keeps such records so
        # the protocol client does not lose them; inspect that backlog before
        # blocking on stdout, while leaving unrelated records available for a
        # later request.
        pending = self._events
        self._events = []
        while True:
            value = pending.pop(0) if pending else self._next(deadline)
            if str(value.get("protocolVersion") or "") != "2":
                raise RuntimeError("Pi Host event did not use protocolVersion 2")
            if value.get("event") != "agent.event":
                self._events.append(dict(value))
                continue
            if value.get("sessionId") != session_id:
                self._events.append(dict(value))
                continue
            payload = value.get("payload")
            if not isinstance(payload, Mapping):
                continue
            kind = str(payload.get("type") or "")
            if kind == "agent_settled":
                return dict(payload)
            if kind in {"agent_failed", "agent_error"}:
                raise RuntimeError(f"Pi agent failed: {_safe_detail(payload)}")


def _run_process_line(process: ExecutionProcess, *, timeout: float) -> dict[str, object]:
    stream = process.stdout
    if stream is None:
        raise RuntimeError("canary process has no stdout")
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            events = selector.select(max(0.01, deadline - time.monotonic()))
            if not events:
                continue
            line = stream.readline(1_048_576)
            if not line:
                break
            value = json.loads(line.decode("utf-8"))
            if not isinstance(value, Mapping):
                raise RuntimeError("kernel canary returned a non-object result")
            return dict(value)
    finally:
        selector.close()
    raise TimeoutError("kernel canary did not report readiness")


def _docker_json(
    launcher: DockerExecutionLauncher,
    container_id: str,
) -> dict[str, object]:
    result = launcher._run_docker(
        [launcher.docker_executable, "inspect", container_id],
        allow_failure=True,
    )
    if result.returncode != 0:
        raise TeamExecutionError(
            "container inspect failed: " + _safe_detail(result.stderr or result.stdout)
        )
    value = json.loads(result.stdout)
    if not isinstance(value, list) or not value or not isinstance(value[0], Mapping):
        raise RuntimeError("container inspect returned an invalid object")
    return dict(value[0])


def _make_spec(
    root: Path,
    *,
    run_id: str,
    actor: str,
    attempt: str,
    image: str,
    command: Sequence[str],
    broker_socket: Path | None = None,
    workspace_read_only: bool = False,
    container_user: str | None = None,
) -> ExecutionSpec:
    actor_root = root / actor
    workspace = actor_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    attempt_root = actor_root / "attempts" / attempt
    session_dir = actor_root / "sessions"
    container_id = f"paw-team-canary-{run_id}-{actor}-{attempt}-{uuid.uuid4().hex[:10]}"
    return ExecutionSpec(
        attempt_id=f"{actor}-{attempt}",
        session_id=f"canary-{actor}",
        scope_root=actor_root,
        workspace_root=workspace,
        agent_dir=attempt_root / "agent",
        session_dir=session_dir,
        logs_dir=attempt_root / "logs",
        home_dir=attempt_root / "home",
        tmp_dir=attempt_root / "tmp",
        container_id=container_id,
        runtime_image=image,
        runtime_command=tuple(command),
        broker_socket=broker_socket,
        workspace_read_only=workspace_read_only,
        # Match the deployed coordinator's default: bind-mounted 0700 state
        # belongs to the server service account, while the container still
        # runs as that non-root numeric identity.
        container_user=container_user or _canary_container_user(),
        container_environment={
            "PAW_CANARY_ACTOR": actor,
            "PAW_CANARY_RUN_ID": run_id,
        },
    )


def _canary_container_user() -> str:
    uid = os.getuid()
    gid = os.getgid()
    if uid <= 0 or gid <= 0:
        raise RuntimeError(
            "the OCI canary must run as a non-root service account with a non-root primary group"
        )
    return f"{uid}:{gid}"


def _probe_command() -> tuple[str, ...]:
    # ExecutionSpec rejects control characters in server-generated argv.  Keep
    # the argv safe by carrying a small multi-line probe as base64; the probe
    # itself runs inside the supplied image and never reaches a host shell or
    # Docker socket.
    source = """\
import json
import os
from pathlib import Path
import socket
import time

actor = os.environ.get("PAW_CANARY_ACTOR", "")
workspace = Path("/workspace")


def try_write(path: Path) -> bool:
    try:
        path.write_text(actor, encoding="utf-8")
    except Exception:
        return False
    return True


workspace_marker = (workspace / "owner.txt").read_text(encoding="utf-8").strip() == actor
workspace_write = try_write(workspace / "probe.txt")
home_write = try_write(Path("/run/paw/home") / (actor + ".txt"))
tmp_write = try_write(Path("/tmp") / (actor + ".txt"))
root_write = not try_write(Path("/etc") / ("paw-team-canary-" + actor))
network = False
try:
    # RFC 5737 TEST-NET-1 is reserved for documentation; this probe must not
    # contact a real public service even if a broken runtime enables a route.
    with socket.create_connection(("192.0.2.1", 80), timeout=0.25):
        network = True
except Exception:
    pass
print(
    json.dumps(
        {
            "actor": actor,
            "uid": os.getuid(),
            "gid": os.getgid(),
            "home": os.environ.get("HOME", ""),
            "cwd": os.getcwd(),
            "workspaceMarker": workspace_marker,
            "workspaceWrite": workspace_write,
            "homeWrite": home_write,
            "tmpWrite": tmp_write,
            "rootWrite": root_write,
            "networkReachable": network,
            "secretEnvNames": [
                key
                for key in os.environ
                if any(
                    marker in key.upper()
                    for marker in ("API_KEY", "PASSWORD", "SECRET", "PRIVATE_KEY")
                )
            ],
        },
        separators=(",", ":"),
    ),
    flush=True,
)
time.sleep(60)
"""
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return (
        "python3",
        "-c",
        "import base64;exec(base64.b64decode(%r))" % encoded,
    )


def _write_models(agent_dir: Path, *, token: str) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "providers": {
            CANARY_PROVIDER: {
                "api": "openai-completions",
                "baseUrl": "http://127.0.0.1:8766/v1",
                "apiKey": token,
                "models": [
                    {
                        "id": CANARY_MODEL,
                        "name": CANARY_MODEL,
                        "reasoning": False,
                        "input": ["text"],
                        "contextWindow": 128_000,
                        "maxTokens": 256,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                    }
                ],
            }
        }
    }
    (agent_dir / "models.json").write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _check_container_config(
    inspected: Mapping[str, object],
    *,
    spec: ExecutionSpec,
) -> tuple[bool, str, dict[str, object]]:
    host_config = inspected.get("HostConfig")
    config = inspected.get("Config")
    if not isinstance(host_config, Mapping) or not isinstance(config, Mapping):
        return False, "Docker inspect omitted HostConfig or Config", {}
    cap_drop = {
        str(item).upper() for item in host_config.get("CapDrop") or []
    }
    security = {str(item).lower() for item in host_config.get("SecurityOpt") or []}
    env = [str(item) for item in config.get("Env") or []]
    mounts = inspected.get("Mounts")
    mount_rows = mounts if isinstance(mounts, list) else []
    destinations = {
        str(item.get("Destination")): item
        for item in mount_rows
        if isinstance(item, Mapping)
    }
    allowed_sources = {
        str(spec.workspace_root.resolve(strict=False)),
        str(spec.agent_dir.resolve(strict=False)),
        str(spec.session_dir.resolve(strict=False)),
        str(spec.logs_dir.resolve(strict=False)),
        str(spec.home_dir.resolve(strict=False)),
        str(spec.tmp_dir.resolve(strict=False)),
    }
    if spec.broker_socket is not None:
        allowed_sources.add(str(spec.broker_socket.resolve(strict=False)))
    source_rows = {
        str(item.get("Source"))
        for item in mount_rows
        if isinstance(item, Mapping)
    }
    required_mounts = set(_REQUIRED_MOUNTS)
    if spec.broker_socket is None:
        required_mounts.discard("/run/paw/broker.sock")
    details = {
        "readonlyRootfs": host_config.get("ReadonlyRootfs"),
        "networkMode": host_config.get("NetworkMode"),
        "pidsLimit": host_config.get("PidsLimit"),
        "memory": host_config.get("Memory"),
        "nanoCpus": host_config.get("NanoCpus"),
        "capDrop": sorted(cap_drop),
        "securityOpt": sorted(security),
        "containerUser": config.get("User"),
        "containerName": inspected.get("Name"),
        "containerId": inspected.get("Id"),
        "mountDestinations": sorted(destinations),
        "containerPid": inspected.get("State", {}).get("Pid") if isinstance(inspected.get("State"), Mapping) else None,
    }
    required = {
        bool(host_config.get("ReadonlyRootfs")),
        str(host_config.get("NetworkMode") or "") == "none",
        int(host_config.get("PidsLimit") or 0) > 0,
        int(host_config.get("Memory") or 0) > 0,
        int(host_config.get("NanoCpus") or 0) > 0,
        "ALL" in cap_drop,
        any("no-new-privileges" in item for item in security),
        str(config.get("User") or "") == spec.container_user,
        str(inspected.get("Name") or "") == "/" + spec.container_id,
        required_mounts.issubset(destinations),
        source_rows.issubset(allowed_sources),
        not any("docker.sock" in item for item in source_rows | set(env)),
    }
    if not all(required):
        return False, "container guardrails differ from the reviewed Team OCI policy", details
    return True, "read-only rootfs, network=none, non-root limits and scoped mounts verified", details


def _stop_container(
    handle: _RunningContainer,
    checks: list[dict[str, object]],
) -> None:
    if handle.stopped:
        return
    try:
        receipt = handle.launcher.stop(handle.spec, handle.process)
        handle.receipt = receipt
        handle.stopped = True
        checks.append(
            {
                "name": f"stop-removal:{handle.label}",
                "status": "passed" if receipt.verified else "failed",
                "detail": receipt.detail,
                "evidence": receipt.as_dict(),
            }
        )
    except Exception as exc:
        checks.append(
            {
                "name": f"stop-removal:{handle.label}",
                "status": "failed",
                "detail": "container removal could not be verified: " + _safe_detail(exc),
            }
        )


def _run_kernel_canary(
    *,
    root: Path,
    run_id: str,
    image: str,
    docker: str,
    timeout: float,
    checks: list[dict[str, object]],
    owned_handles: list[_RunningContainer] | None = None,
) -> list[_RunningContainer]:
    handles: list[_RunningContainer] = []
    command = _probe_command()
    for actor in ("user-a", "user-b"):
        spec = _make_spec(
            root,
            run_id=run_id,
            actor=actor,
            attempt="probe",
            image=image,
            command=command,
        )
        workspace = spec.workspace_root
        (workspace / "owner.txt").write_text(actor + "\n", encoding="utf-8")
        launcher = DockerExecutionLauncher(image=image, command=command, docker_executable=docker)
        process = launcher.start(spec, command, spec.container_environment_for())
        handle = _RunningContainer(actor, spec, launcher, process)
        handles.append(handle)
        if owned_handles is not None:
            owned_handles.append(handle)
        result = _run_process_line(process, timeout=min(30.0, timeout))
        ok, detail, evidence = _check_container_config(
            _docker_json(launcher, spec.container_id),
            spec=spec,
        )
        if not ok:
            raise RuntimeError(f"{actor}: {detail}")
        checks.append(
            {
                "name": f"container-guardrails:{actor}",
                "status": "passed",
                "detail": detail,
                "evidence": evidence,
            }
        )
        expected = {
            "actor": actor,
            "uid": int(spec.container_user.split(":", 1)[0]),
            "gid": int(spec.container_user.split(":", 1)[1]),
            "home": "/run/paw/home",
            "cwd": "/workspace",
            "workspaceMarker": True,
            "workspaceWrite": True,
            "homeWrite": True,
            "tmpWrite": True,
            "rootWrite": True,
            "networkReachable": False,
            "secretEnvNames": [],
        }
        mismatches = {
            key: {"expected": value, "actual": result.get(key)}
            for key, value in expected.items()
            if result.get(key) != value
        }
        if mismatches:
            raise RuntimeError(f"{actor}: kernel isolation probe mismatch {mismatches}")
        checks.append(
            {
                "name": f"workspace-home-network:{actor}",
                "status": "passed",
                "detail": "actor workspace, HOME/TMP writes, non-root identity and network denial verified",
                "evidence": result,
            }
        )
    pids = []
    for handle in handles:
        inspected = _docker_json(handle.launcher, handle.spec.container_id)
        state = inspected.get("State")
        running = isinstance(state, Mapping) and state.get("Running") is True
        pid = state.get("Pid") if isinstance(state, Mapping) else None
        if not running or not isinstance(pid, int) or pid <= 0:
            raise RuntimeError(f"{handle.label}: container is not running while probe is held")
        pids.append(pid)
    if len(set(pids)) != len(pids):
        raise RuntimeError("actor containers do not have independent process identities")
    checks.append(
        {
            "name": "two-actor-process-isolation",
            "status": "passed",
            "detail": "two independently named containers remained live with distinct container PIDs",
            "evidence": {"containerPids": pids},
        }
    )
    for handle in handles:
        _stop_container(handle, checks)
        probe_file = handle.spec.workspace_root / "probe.txt"
        home_file = handle.spec.home_dir / (handle.label + ".txt")
        tmp_file = handle.spec.tmp_dir / (handle.label + ".txt")
        if not probe_file.is_file() or probe_file.read_text(encoding="utf-8") != handle.label:
            raise RuntimeError(f"{handle.label}: workspace write did not land in its own checkout")
        if not home_file.is_file() or not tmp_file.is_file():
            raise RuntimeError(f"{handle.label}: private HOME/TMP write did not persist in its own scope")
    checks.append(
        {
            "name": "two-actor-workspace-state-isolation",
            "status": "passed",
            "detail": "workspace, HOME and TMP artifacts stayed in their actor-specific temporary roots",
        }
    )
    return handles


def _pi_open_params(*, token: str, session_file: str) -> dict[str, object]:
    # ``token`` is included only in the model file; this helper keeps it out of
    # the Pi protocol payload and therefore out of the transcript.
    del token
    return {
        "sessionId": PI_SESSION_ID,
        "cwd": "/workspace",
        "provider": CANARY_PROVIDER,
        "modelId": CANARY_MODEL,
        "systemPrompt": "OCI canary: search and load canary_echo, use it once, then answer with its result.",
        "noContextFiles": True,
        "toolManifest": [
            {
                "name": PI_TOOL_NAME,
                "description": "Return the deterministic OCI canary value.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            }
        ],
        "sessionFile": session_file,
    }


def _run_pi_canary(
    *,
    root: Path,
    run_id: str,
    image: str,
    docker: str,
    command: Sequence[str],
    timeout: float,
    checks: list[dict[str, object]],
) -> None:
    actor = "pi"
    token = CANARY_TOKEN_PREFIX + uuid.uuid4().hex
    socket_path = root / actor / "broker.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    broker = _FakeModelBroker(socket_path, token=token)
    broker.start()
    first: _RunningContainer | None = None
    second: _RunningContainer | None = None
    try:
        spec = _make_spec(
            root,
            run_id=run_id,
            actor=actor,
            attempt="attempt-1",
            image=image,
            command=command,
            broker_socket=socket_path,
        )
        _write_models(spec.agent_dir, token=token)
        environment = spec.container_environment_for(
            gateway_url="http://127.0.0.1:8766/api/agent/tool/execute",
            gateway_token=token,
        )
        environment.update(
            {
                "RAG_IME_PI_PROVIDER": CANARY_PROVIDER,
                "RAG_IME_PI_MODEL": CANARY_MODEL,
                "RAG_IME_PI_PROTOCOL_VERSION": "2",
            }
        )
        launcher = DockerExecutionLauncher(image=image, command=command, docker_executable=docker)
        process = launcher.start(spec, command, environment)
        first = _RunningContainer("pi-attempt-1", spec, launcher, process)
        pipe = _PiPipe(process)
        hello = pipe.request("hello", {}, timeout=min(30.0, timeout))
        if not hello:
            raise RuntimeError("Pi hello returned no capabilities")
        opened = pipe.request(
            "session.open",
            _pi_open_params(token=token, session_file="/run/paw/sessions/" + PI_SESSION_FILE),
            timeout=min(60.0, timeout),
        )
        accepted = pipe.request(
            "session.prompt",
            {
                "sessionId": PI_SESSION_ID,
                "message": "Run the deterministic OCI tool and return its value.",
                "clientMessageId": "oci-canary-client-1",
            },
            timeout=min(60.0, timeout),
        )
        turn_id = str(accepted.get("turnId") or "")
        if not turn_id:
            raise RuntimeError("Pi prompt did not return a turn id")
        settled = pipe.wait_for_settled(
            session_id=PI_SESSION_ID, timeout=min(120.0, timeout)
        )
        snapshot = pipe.request(
            "session.snapshot",
            {"sessionId": PI_SESSION_ID},
            timeout=min(30.0, timeout),
        )
        snapshot_text = json.dumps(snapshot, ensure_ascii=False)
        if "CANARY_PI_OK" not in snapshot_text or "OCI_TOOL_OK" not in snapshot_text:
            raise RuntimeError("Pi snapshot omitted the deterministic tool/result exchange")
        if not broker.tool_requests or not broker.model_requests:
            raise RuntimeError("fake broker did not observe both model and tool traffic")
        if not any(
            isinstance(item.get("args"), Mapping)
            and item.get("tool") == PI_TOOL_NAME
            for item in broker.tool_requests
        ):
            raise RuntimeError("fake broker observed no canary_echo tool invocation")
        checks.append(
            {
                "name": "pi-protocol2-prompt-tool-result",
                "status": "passed",
                "detail": "real Pi protocol 2 Host completed prompt -> fake broker tool -> deterministic result",
                "evidence": {
                    "hello": hello,
                    "opened": bool(opened),
                    "turnId": turn_id,
                    "settled": settled,
                    "modelRequests": len(broker.model_requests),
                    "toolRequests": len(broker.tool_requests),
                },
            }
        )
        # Closing the logical Pi Session leaves the Host/container alive. This
        # is the concrete canary boundary for browser/client close versus an
        # explicit execution Stop.
        pipe.request(
            "session.close", {"sessionId": PI_SESSION_ID}, timeout=min(30.0, timeout)
        )
        inspected = _docker_json(launcher, spec.container_id)
        state = inspected.get("State")
        if not isinstance(state, Mapping) or state.get("Running") is not True:
            raise RuntimeError("closing the Pi Session unexpectedly stopped its container")
        checks.append(
            {
                "name": "client-close-does-not-stop-container",
                "status": "passed",
                "detail": "session.close returned while the isolated Host container remained running",
                "evidence": {"containerPid": state.get("Pid")},
            }
        )
        _stop_container(first, checks)
        transcript = spec.session_dir / PI_SESSION_FILE
        if transcript.is_symlink() or not transcript.is_file():
            raise RuntimeError("Pi did not leave a regular transcript in the stable Session directory")

        # Reuse only stable Session transcript storage. Agent/HOME/TMP and the
        # broker socket are new per-attempt paths, matching Team coordinator
        # recovery semantics.
        socket_path_2 = root / actor / "broker-attempt-2.sock"
        broker_2 = _FakeModelBroker(socket_path_2, token=token + "-restart")
        broker_2.start()
        try:
            spec_2 = _make_spec(
                root,
                run_id=run_id,
                actor=actor,
                attempt="attempt-2",
                image=image,
                command=command,
                broker_socket=socket_path_2,
            )
            _write_models(spec_2.agent_dir, token=token + "-restart")
            environment_2 = spec_2.container_environment_for(
                gateway_url="http://127.0.0.1:8766/api/agent/tool/execute",
                gateway_token=token + "-restart",
            )
            environment_2.update(
                {
                    "RAG_IME_PI_PROVIDER": CANARY_PROVIDER,
                    "RAG_IME_PI_MODEL": CANARY_MODEL,
                    "RAG_IME_PI_PROTOCOL_VERSION": "2",
                }
            )
            launcher_2 = DockerExecutionLauncher(
                image=image, command=command, docker_executable=docker
            )
            process_2 = launcher_2.start(spec_2, command, environment_2)
            second = _RunningContainer("pi-attempt-2", spec_2, launcher_2, process_2)
            pipe_2 = _PiPipe(process_2)
            pipe_2.request("hello", {}, timeout=min(30.0, timeout))
            opened_2 = pipe_2.request(
                "session.open",
                _pi_open_params(
                    token=token + "-restart",
                    session_file="/run/paw/sessions/" + PI_SESSION_FILE,
                ),
                timeout=min(60.0, timeout),
            )
            snapshot_2 = pipe_2.request(
                "session.snapshot",
                {"sessionId": PI_SESSION_ID},
                timeout=min(30.0, timeout),
            )
            if "CANARY_PI_OK" not in json.dumps(snapshot_2, ensure_ascii=False):
                raise RuntimeError("Pi restart did not restore the prior transcript")
            checks.append(
                {
                    "name": "pi-restart-cold-transcript-recovery",
                    "status": "passed",
                    "detail": "a new Pi Host attempt reopened the stable transcript without reusing agent/HOME/TMP",
                    "evidence": {
                        "stableTranscript": str(transcript.name),
                        "attemptRootsDistinct": spec.agent_dir != spec_2.agent_dir
                        and spec.home_dir != spec_2.home_dir
                        and spec.tmp_dir != spec_2.tmp_dir,
                    },
                }
            )
            _stop_container(second, checks)
        finally:
            broker_2.close()
    finally:
        if first is not None:
            _stop_container(first, checks)
        if second is not None:
            _stop_container(second, checks)
        broker.close()


def _docker_probe(docker: str) -> tuple[bool, str, str]:
    try:
        result = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "blocked", "Docker daemon could not be queried: " + _safe_detail(exc)
    if result.returncode != 0:
        return False, "blocked", "Docker daemon is unavailable: " + _safe_detail(result.stderr or result.stdout)
    version = _safe_detail(result.stdout) or "unknown"
    return True, "passed", "Docker daemon available (server " + version + ")"


def _image_probe(docker: str, image: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "reviewed image could not be inspected: " + _safe_detail(exc)
    if result.returncode != 0 or not result.stdout.strip():
        return False, "reviewed image is not present locally; no image pull was attempted"
    return True, "local image identity " + _safe_detail(result.stdout)


def _validate_args(args: argparse.Namespace) -> None:
    image = str(args.image).strip()
    if not image or any(ord(char) < 32 for char in image) or len(image) > 256:
        raise ValueError("--image must be an explicit reviewed image reference")
    if not args.pi_command:
        raise ValueError("Pi command must be a non-empty reviewed argv")
    if args.timeout <= 0 or args.timeout > 900:
        raise ValueError("--timeout must be between 1 and 900 seconds")
    scratch_root = Path(str(args.scratch_root)).expanduser()
    if not scratch_root.is_absolute() or scratch_root.is_symlink() or not scratch_root.is_dir():
        raise ValueError("--scratch-root must be an existing absolute non-symlink directory")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a real Docker/OCI Team isolation canary using an explicit reviewed Pi worker image. "
            "No image is pulled and no real Provider is contacted."
        )
    )
    parser.add_argument(
        "--image",
        required=True,
        help="reviewed local Pi worker image reference (tag or digest); never auto-discovered or pulled",
    )
    parser.add_argument("--docker", default="docker", help="Docker/OCI CLI executable")
    parser.add_argument(
        "--pi-command",
        nargs="+",
        default=list(DEFAULT_PI_COMMAND),
        metavar="ARGV",
        help="reviewed worker argv; defaults to deploy/team/runtime.example.json",
    )
    parser.add_argument(
        "--skip-pi",
        action="store_true",
        help="only run kernel mount/process checks and report Pi protocol as unverified",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="maximum wait for each kernel/Pi canary phase in seconds (1-900)",
    )
    parser.add_argument(
        "--scratch-root",
        default="/tmp",
        help="private directory for this canary's mkdtemp root (default: /tmp)",
    )
    args = parser.parse_args(argv)
    checks: list[dict[str, object]] = []
    run_id = uuid.uuid4().hex[:12]
    report: dict[str, object] = {
        "schemaVersion": "rag-ime.team-oci-canary.v1",
        "status": "blocked",
        "runId": run_id,
        "image": str(args.image),
        "piCommand": list(args.pi_command),
        "checks": checks,
        "passed": [],
        "failed": [],
        "unverified": [],
        "blocked": [],
    }
    temporary: tempfile.TemporaryDirectory[str] | None = None
    active_handles: list[_RunningContainer] = []

    def cleanup_handles() -> None:
        for handle in active_handles:
            _stop_container(handle, checks)

    try:
        _validate_args(args)
        docker_ok, docker_status, docker_detail = _docker_probe(args.docker)
        checks.append(
            {"name": "docker-daemon", "status": docker_status, "detail": docker_detail}
        )
        if not docker_ok:
            report["blocked"] = ["docker-daemon"]
            report["unverified"] = [
                "kernel-isolation",
                "pi-protocol2-prompt-tool-result",
                "stop-removal",
                "pi-restart-cold-transcript-recovery",
                "team-grant-revocation",
            ]
            return _emit_report(report, 2)
        image_ok, image_detail = _image_probe(args.docker, str(args.image))
        checks.append(
            {
                "name": "reviewed-image-present",
                "status": "passed" if image_ok else "blocked",
                "detail": image_detail,
            }
        )
        if not image_ok:
            report["blocked"] = ["reviewed-image-present"]
            report["unverified"] = [
                "kernel-isolation",
                "pi-protocol2-prompt-tool-result",
                "stop-removal",
                "pi-restart-cold-transcript-recovery",
                "team-grant-revocation",
            ]
            return _emit_report(report, 2)
        # A controller running inside Linux must place bind sources on a path
        # visible to the daemon.  The operator passes a daemon-visible volume
        # with --scratch-root; the normal host default remains /tmp.
        temporary = tempfile.TemporaryDirectory(
            prefix="paw-team-oci-canary-",
            dir=str(Path(str(args.scratch_root)).expanduser()),
        )
        root = Path(temporary.name)
        started_at = time.monotonic()
        _run_kernel_canary(
            root=root,
            run_id=run_id,
            image=str(args.image),
            docker=args.docker,
            timeout=args.timeout,
            checks=checks,
            owned_handles=active_handles,
        )
        if args.skip_pi:
            checks.append(
                {
                    "name": "pi-protocol2-prompt-tool-result",
                    "status": "unverified",
                    "detail": "--skip-pi was supplied; no real Pi Host protocol was started",
                }
            )
            checks.append(
                {
                    "name": "pi-restart-cold-transcript-recovery",
                    "status": "unverified",
                    "detail": "--skip-pi was supplied; transcript restart behavior was not exercised",
                }
            )
        else:
            _run_pi_canary(
                root=root,
                run_id=run_id,
                image=str(args.image),
                docker=args.docker,
                command=tuple(args.pi_command),
                timeout=args.timeout,
                checks=checks,
            )
        checks.append(
            {
                "name": "team-grant-revocation",
                "status": "unverified",
                "detail": "standalone OCI canary has no authenticated Team grant store; use Team coordinator revocation tests for this boundary",
            }
        )
        report["elapsedSeconds"] = round(time.monotonic() - started_at, 3)
    except Exception as exc:
        cleanup_handles()
        checks.append(
            {
                "name": "canary-run",
                "status": "failed",
                "detail": _safe_detail(exc),
            }
        )
        report["failure"] = _safe_detail(exc)
        return _emit_report(report, 1)
    finally:
        cleanup_handles()
        if temporary is not None:
            temporary.cleanup()
    return _emit_report(report, 0)


def _emit_report(report: dict[str, object], default_exit: int) -> int:
    checks = report.get("checks")
    rows = checks if isinstance(checks, list) else []
    passed = [
        str(row.get("name"))
        for row in rows
        if isinstance(row, Mapping) and row.get("status") == "passed"
    ]
    failed = [
        str(row.get("name"))
        for row in rows
        if isinstance(row, Mapping) and row.get("status") == "failed"
    ]
    unverified = [
        str(row.get("name"))
        for row in rows
        if isinstance(row, Mapping) and row.get("status") == "unverified"
    ]
    blocked = [
        str(row.get("name"))
        for row in rows
        if isinstance(row, Mapping) and row.get("status") == "blocked"
    ]
    report["passed"] = passed
    report["failed"] = failed
    report["unverified"] = sorted(set(unverified + [str(item) for item in report.get("unverified") or []]))
    report["blocked"] = sorted(set(blocked + [str(item) for item in report.get("blocked") or []]))
    if report["blocked"]:
        report["status"] = "blocked"
        exit_code = 2
    elif report["failed"]:
        report["status"] = "failed"
        exit_code = 1
    elif report["unverified"]:
        report["status"] = "unverified"
        exit_code = 3
    else:
        report["status"] = "passed"
        exit_code = default_exit
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
