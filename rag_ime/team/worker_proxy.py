"""Loopback HTTP bridge for an isolated team worker.

Pi's Provider and tool adapters already speak ordinary HTTP.  The team broker
is deliberately reachable only through one per-attempt Unix socket, so this
small process exposes a container-local loopback listener and forwards each
request to that socket.  It never resolves a remote URL, follows redirects,
or opens a network socket other than the fixed loopback listener and broker
Unix socket.

An image can use :func:`run_worker` as its immutable entrypoint and keep the
server-selected Pi command unchanged::

    python -m rag_ime.team.worker_proxy -- --usr/local/bin/pi-host

The Docker image, command, and entrypoint are trusted server configuration;
request data cannot select any of them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from threading import Thread, current_thread
from typing import Any
from urllib.parse import urlsplit

from .execution import TeamExecutionError


__all__ = ["TeamBrokerForwarder", "run_worker"]


_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8766
_DEFAULT_SOCKET = "/run/paw/broker.sock"
_MAX_REQUEST_BODY = 2 * 1024 * 1024
_MAX_RESPONSE_BODY = 32 * 1024 * 1024
_HOP_BY_HOP = frozenset(
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


def _bounded_port(value: object) -> int:
    try:
        port = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("worker proxy port must be an integer") from exc
    if not 0 <= port <= 65_535:
        raise ValueError("worker proxy port is out of bounds")
    return port


def _safe_request_path(value: str) -> str:
    path = str(value or "")
    parsed = urlsplit(path)
    # BaseHTTPRequestHandler accepts absolute-form request targets.  The
    # bridge must not become an arbitrary HTTP proxy, even for a localhost URL.
    if parsed.scheme or parsed.netloc or not path.startswith("/"):
        raise TeamExecutionError("worker broker request must use an origin path")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise TeamExecutionError("worker broker request path contains controls")
    if len(path) > 4096:
        raise TeamExecutionError("worker broker request path is too long")
    return path


def _json_error(message: str) -> bytes:
    return json.dumps(
        {"ok": False, "error": str(message)[:240]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class _UnixHTTPConnection(HTTPConnection):
    """``http.client`` connection whose only peer is a Unix socket."""

    def __init__(self, socket_path: Path, *, timeout: float) -> None:
        super().__init__("paw-team-broker", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        path = self.socket_path
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise TeamExecutionError("team broker socket is unavailable") from exc
        if not stat_is_socket(metadata.st_mode):
            raise TeamExecutionError("team broker path is not a Unix socket")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.timeout)
            connection.connect(str(path))
        except OSError as exc:
            connection.close()
            raise TeamExecutionError("team broker socket could not be reached") from exc
        self.sock = connection


def stat_is_socket(mode: int) -> bool:
    """Keep the worker helper independent of platform-specific stat imports."""

    # ``stat.S_ISSOCK`` is intentionally imported lazily: this module also
    # ships in a minimal worker image where importing the full PAW stack is
    # undesirable, while the standard library remains available.
    import stat

    return stat.S_ISSOCK(mode)


class TeamBrokerForwarder:
    """Forward loopback HTTP requests to one mounted attempt broker socket."""

    def __init__(
        self,
        socket_path: str | Path = _DEFAULT_SOCKET,
        *,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        request_timeout_seconds: float = 90.0,
    ) -> None:
        normalized_host = str(host).strip()
        if normalized_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("worker proxy must listen on loopback")
        normalized_socket = Path(socket_path).expanduser()
        if not normalized_socket.is_absolute() or normalized_socket.is_symlink():
            raise ValueError("worker proxy broker socket must be an absolute non-symlink path")
        timeout = float(request_timeout_seconds)
        if not 1.0 <= timeout <= 300.0:
            raise ValueError("worker proxy request timeout is out of bounds")
        self.socket_path = normalized_socket
        self.host = normalized_host
        self.port = _bounded_port(port)
        self.request_timeout_seconds = timeout
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                owner._handle_post(self)

            def do_GET(self) -> None:  # noqa: N802
                owner._reject(self, HTTPStatus.METHOD_NOT_ALLOWED, "Broker accepts POST only")

            def do_PUT(self) -> None:  # noqa: N802
                owner._reject(self, HTTPStatus.METHOD_NOT_ALLOWED, "Broker accepts POST only")

            def do_PATCH(self) -> None:  # noqa: N802
                owner._reject(self, HTTPStatus.METHOD_NOT_ALLOWED, "Broker accepts POST only")

            def do_DELETE(self) -> None:  # noqa: N802
                owner._reject(self, HTTPStatus.METHOD_NOT_ALLOWED, "Broker accepts POST only")

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server((self.host, self.port), Handler)
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        value = self._server.server_address
        return str(value[0]), int(value[1])

    def start(self) -> "TeamBrokerForwarder":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="paw-team-worker-broker-proxy",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None and self._thread is not current_thread():
            self._thread.join(timeout=5)
        self._thread = None

    def _handle_post(
        self,
        handler: BaseHTTPRequestHandler,
    ) -> None:
        try:
            path = _safe_request_path(handler.path)
            if handler.headers.get("Transfer-Encoding"):
                raise TeamExecutionError("chunked worker broker requests are not supported")
            raw_length = handler.headers.get("Content-Length")
            if raw_length is None:
                raise TeamExecutionError("worker broker request requires Content-Length")
            length = int(raw_length)
            if not 0 <= length <= _MAX_REQUEST_BODY:
                raise TeamExecutionError("worker broker request body is too large")
            body = handler.rfile.read(length)
            if len(body) != length:
                raise TeamExecutionError("worker broker request body was truncated")
            headers = {
                str(key): str(value)
                for key, value in handler.headers.items()
                if str(key).lower() not in _HOP_BY_HOP
            }
            headers["Content-Length"] = str(length)
            response = self._forward(path, body, headers)
            with response:
                handler.send_response(response.status, response.reason)
                response_headers = {
                    str(key): str(value)
                    for key, value in response.getheaders()
                    if str(key).lower() not in _HOP_BY_HOP
                    and str(key).lower() != "content-length"
                }
                for key, value in response_headers.items():
                    handler.send_header(key, value)
                handler.send_header("Connection", "close")
                handler.end_headers()
                handler.close_connection = True
                total = 0
                while True:
                    chunk = response.read(16 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BODY:
                        break
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
        except (ValueError, TypeError, HTTPException, TeamExecutionError) as exc:
            self._reject(handler, HTTPStatus.BAD_REQUEST, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            handler.close_connection = True
        except OSError as exc:
            self._reject(handler, HTTPStatus.BAD_GATEWAY, str(exc))

    def _forward(
        self,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> HTTPResponse:
        connection = _UnixHTTPConnection(
            self.socket_path,
            timeout=self.request_timeout_seconds,
        )
        try:
            connection.request("POST", path, body=body, headers=dict(headers))
            return connection.getresponse()
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _reject(
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        message: str,
    ) -> None:
        body = _json_error(message)
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(body)
            handler.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            handler.close_connection = True


def run_worker(
    command: Sequence[str],
    *,
    socket_path: str | Path = _DEFAULT_SOCKET,
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Run a trusted Pi command beside its broker forwarder.

    The child receives the worker's stdio directly, so the surrounding Docker
    launcher still exposes the normal Pi JSONL process-compatible pipe.  The
    command is supplied by the immutable image/entrypoint configuration; this
    function does not parse or interpolate shell text.
    """

    normalized = tuple(str(part) for part in command)
    if not normalized or any(not part or any(ord(char) < 32 for char in part) for part in normalized):
        raise ValueError("worker command must be a non-empty safe argv")
    forwarder = TeamBrokerForwarder(socket_path, host=host, port=port).start()
    child: subprocess.Popen[bytes] | None = None
    old_handlers: dict[signal.Signals, Any] = {}

    def stop_child(_signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            child.terminate()

    try:
        child = subprocess.Popen(
            list(normalized),
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=(dict(environment) if environment is not None else None),
            start_new_session=False,
        )
        if os.name == "posix":
            for signal_number in (signal.SIGTERM, signal.SIGINT):
                old_handlers[signal_number] = signal.getsignal(signal_number)
                signal.signal(signal_number, stop_child)
        return int(child.wait())
    except KeyboardInterrupt:
        stop_child(signal.SIGINT, None)
        return int(child.wait()) if child is not None else 130
    finally:
        for signal_number, handler in old_handlers.items():
            signal.signal(signal_number, handler)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        forwarder.close()


def _main(argv: Iterable[str]) -> int:
    values = list(argv)
    try:
        separator = values.index("--")
    except ValueError:
        raise SystemExit("worker proxy requires -- followed by the Pi command")
    options = values[:separator]
    command = values[separator + 1 :]
    socket_path = os.environ.get("RAG_IME_TEAM_BROKER_SOCKET", _DEFAULT_SOCKET)
    host = _DEFAULT_HOST
    port = _DEFAULT_PORT
    index = 0
    while index < len(options):
        option = options[index]
        if option == "--broker-socket" and index + 1 < len(options):
            socket_path = options[index + 1]
            index += 2
            continue
        if option == "--listen-host" and index + 1 < len(options):
            host = options[index + 1]
            index += 2
            continue
        if option == "--listen-port" and index + 1 < len(options):
            port = _bounded_port(options[index + 1])
            index += 2
            continue
        raise SystemExit(f"unknown worker proxy option: {option}")
    return run_worker(command, socket_path=socket_path, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover - exercised by image canary
    raise SystemExit(_main(sys.argv[1:]))
