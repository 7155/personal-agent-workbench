"""Container-local worker and bounded Unix HTTP relay for project previews."""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from .preview_runtime import (
    MANIFEST_FILENAME,
    PREVIEW_METHODS,
    PreviewManifest,
    PreviewRuntimeError,
    read_preview_manifest,
)


__all__ = ["PreviewRelay", "run_worker"]


SOURCE_ROOT = Path("/workspace")
APP_ROOT = Path("/tmp/app")
SOCKET_PATH = Path("/run/paw/logs/preview.sock")
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
_MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
_MAX_RESPONSE_BODY_BYTES = 16 * 1024 * 1024
_MAX_COPY_FILES = 4_096
_MAX_COPY_FILE_BYTES = 8 * 1024 * 1024
_MAX_COPY_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_LOG_TAIL_BYTES = 64 * 1024


def _safe_path(path: str) -> str:
    value = str(path)
    parsed = urlsplit(value)
    if (
        not value.startswith("/")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or len(value) > 8_192
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise PreviewRuntimeError("Preview request path is invalid")
    return value


def _copy_snapshot(source: Path, destination: Path) -> None:
    """Copy a fixed snapshot without following symlinks into app state."""

    if source.is_symlink() or not source.is_dir():
        raise PreviewRuntimeError("Preview source mount is not a real directory")
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink():
            destination.unlink()
        else:
            shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    count = 0
    total = 0

    def copy_tree(current_source: Path, current_destination: Path) -> None:
        nonlocal count, total
        for entry in os.scandir(current_source):
            source_entry = Path(entry.path)
            destination_entry = current_destination / entry.name
            metadata = source_entry.lstat()
            if entry.is_symlink():
                raise PreviewRuntimeError("Preview source snapshots cannot contain symlinks")
            if stat_is_directory(metadata.st_mode):
                destination_entry.mkdir(mode=0o700)
                copy_tree(source_entry, destination_entry)
                continue
            if not stat_is_regular(metadata.st_mode):
                raise PreviewRuntimeError("Preview source contains a non-regular entry")
            count += 1
            total += metadata.st_size
            if count > _MAX_COPY_FILES:
                raise PreviewRuntimeError("Preview source contains too many files")
            if metadata.st_size > _MAX_COPY_FILE_BYTES or total > _MAX_COPY_TOTAL_BYTES:
                raise PreviewRuntimeError("Preview source exceeds its copy limits")
            shutil.copyfile(source_entry, destination_entry)
            # The snapshot mount is read-only and source files may be 0444;
            # the private app copy must still let prepare/build steps update
            # their own files. Preserve executability while granting only the
            # container user read/write access.
            mode = metadata.st_mode & 0o777
            os.chmod(destination_entry, 0o600 | (0o100 if mode & 0o111 else 0))

    copy_tree(source, destination)


def stat_is_directory(mode: int) -> bool:
    import stat

    return stat.S_ISDIR(mode)


def stat_is_regular(mode: int) -> bool:
    import stat

    return stat.S_ISREG(mode)


class _LogTail:
    """Bounded in-memory tail for one child stream."""

    def __init__(self, maximum: int = _MAX_LOG_TAIL_BYTES) -> None:
        self.maximum = maximum
        self._data = bytearray()

    def append(self, value: bytes) -> None:
        if not value:
            return
        if len(value) >= self.maximum:
            self._data = bytearray(value[-self.maximum :])
            return
        self._data.extend(value)
        if len(self._data) > self.maximum:
            del self._data[: len(self._data) - self.maximum]

    def data(self) -> bytes:
        return bytes(self._data)


def _capture_stream(stream: object, tail: _LogTail, log_path: Path | None = None) -> None:
    read = getattr(stream, "read", None)
    close = getattr(stream, "close", None)
    if not callable(read):
        return
    try:
        while True:
            chunk = read(16 * 1024)
            if not chunk:
                return
            if isinstance(chunk, bytes):
                tail.append(chunk)
                if log_path is not None:
                    # Docker may remove the worker with SIGKILL before its
                    # finally block runs.  Persist each bounded update so the
                    # operator still has the latest tail after that stop.
                    _write_log_tail(log_path, tail)
    except (OSError, ValueError):
        # The parent may close a pipe while terminating a timed-out command.
        # The bounded data collected before that point is still useful.
        return
    finally:
        if callable(close):
            close()


def _write_log_tail(path: Path, tail: _LogTail) -> bool:
    """Write one bounded tail without following a worker-created symlink."""

    try:
        path = Path(path)
        if path.parent.is_symlink() or not path.parent.is_dir():
            return False
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(tail.data())
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            # fdopen owns the descriptor after entering its context; this
            # branch only preserves a safe failure result for diagnostics.
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            return False
        return True
    except OSError:
        return False


def _capture_finished(
    logs_dir: Path,
    label: str,
    captures: tuple[_LogTail, _LogTail, tuple[threading.Thread, ...]],
) -> None:
    stdout_tail, stderr_tail, threads = captures
    for thread in threads:
        thread.join(timeout=2)
    _write_log_tail(Path(logs_dir) / f"{label}.stdout.log", stdout_tail)
    _write_log_tail(Path(logs_dir) / f"{label}.stderr.log", stderr_tail)


def _start_captured(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: Mapping[str, str],
    logs_dir: Path | None = None,
    label: str = "app",
) -> tuple[
    subprocess.Popen[bytes],
    tuple[_LogTail, _LogTail, tuple[threading.Thread, ...]],
]:
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout_tail = _LogTail()
    stderr_tail = _LogTail()
    threads: list[threading.Thread] = []
    for stream, tail, name, stream_name in (
        (process.stdout, stdout_tail, "preview-stdout", "stdout"),
        (process.stderr, stderr_tail, "preview-stderr", "stderr"),
    ):
        if stream is None:
            continue
        log_path = (
            Path(logs_dir) / f"{label}.{stream_name}.log"
            if logs_dir is not None
            else None
        )
        if log_path is not None:
            _write_log_tail(log_path, tail)
        thread = threading.Thread(
            target=_capture_stream,
            args=(stream, tail, log_path),
            name=name,
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return process, (stdout_tail, stderr_tail, tuple(threads))


class _PreviewUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32


class PreviewRelay:
    """Relay bounded HTTP requests from one Unix socket to app loopback."""

    def __init__(
        self,
        socket_path: Path,
        port: int,
        *,
        request_timeout_seconds: float = 15.0,
    ) -> None:
        if not 1 <= int(port) <= 65_535:
            raise ValueError("Preview app port is out of bounds")
        timeout = float(request_timeout_seconds)
        if not 1.0 <= timeout <= 120.0:
            raise ValueError("Preview relay timeout is out of bounds")
        path = Path(socket_path)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("Preview relay socket must be an absolute non-symlink path")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            raise ValueError("Preview relay socket already exists")
        self.socket_path = path
        self.port = int(port)
        self.request_timeout_seconds = timeout
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                owner._handle(self)

            def do_HEAD(self) -> None:  # noqa: N802
                owner._handle(self)

            def do_POST(self) -> None:  # noqa: N802
                owner._handle(self)

            def do_PATCH(self) -> None:  # noqa: N802
                owner._handle(self)

            def do_PUT(self) -> None:  # noqa: N802
                owner._handle(self)

            def do_DELETE(self) -> None:  # noqa: N802
                owner._handle(self)

            def do_OPTIONS(self) -> None:  # noqa: N802
                owner._handle(self)

            def _unsupported(self) -> None:
                owner._reply_error(self, HTTPStatus.NOT_IMPLEMENTED, "HTTP method unsupported")

            def do_CONNECT(self) -> None:  # noqa: N802
                self._unsupported()

            def do_TRACE(self) -> None:  # noqa: N802
                self._unsupported()

        try:
            self.server = _PreviewUnixServer(str(path), Handler)
            # The parent runtime is the only expected peer.  The logs mount
            # is already private, but make the socket mode explicit rather
            # than inheriting a daemon or image umask.
            os.chmod(path, 0o600)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self._closed = False
        self._lifecycle_lock = threading.Lock()
        self._serve_started = threading.Event()

    def start(self) -> "PreviewRelay":
        return self

    def serve_forever(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._serve_started.set()
        self.server.serve_forever(poll_interval=0.05)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            serving = self._serve_started.is_set()
        # shutdown() waits for serve_forever's loop and must only be used once
        # that loop has started; close() is also safe for an unstarted relay.
        if serving:
            self.server.shutdown()
        self.server.server_close()
        self.socket_path.unlink(missing_ok=True)

    def _reply_error(
        self, handler: BaseHTTPRequestHandler, status: HTTPStatus, message: str
    ) -> None:
        body = json.dumps(
            {"ok": False, "error": message[:240]}, separators=(",", ":")
        ).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)
        handler.close_connection = True

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            method = str(handler.command).upper()
            if method not in PREVIEW_METHODS:
                self._reply_error(handler, HTTPStatus.NOT_IMPLEMENTED, "HTTP method unsupported")
                return
            path = _safe_path(handler.path)
            if "upgrade" in handler.headers or "websocket" in str(handler.headers.get("Connection", "")).lower():
                self._reply_error(handler, HTTPStatus.NOT_IMPLEMENTED, "WebSockets are unsupported")
                return
            if handler.headers.get("Transfer-Encoding"):
                raise PreviewRuntimeError("Chunked preview requests are unsupported")
            content_lengths = handler.headers.get_all("Content-Length", [])
            if len(content_lengths) > 1:
                raise PreviewRuntimeError("Preview request has ambiguous content length")
            raw_length = handler.headers.get("Content-Length", "0")
            if not raw_length.isdigit():
                raise PreviewRuntimeError("Preview request content length is invalid")
            length = int(raw_length)
            if not 0 <= length <= _MAX_REQUEST_BODY_BYTES:
                raise PreviewRuntimeError("Preview request body exceeds its limit")
            body = handler.rfile.read(length)
            if len(body) != length:
                raise PreviewRuntimeError("Preview request body was truncated")
            headers: dict[str, str] = {}
            for key, value in handler.headers.items():
                lower = key.lower()
                if lower in _HOP_BY_HOP_HEADERS or lower in {"host", "content-length"}:
                    continue
                if any(ord(char) in {10, 13} for char in value):
                    raise PreviewRuntimeError("Preview request header is invalid")
                headers[key] = value
            connection = HTTPConnection("127.0.0.1", self.port, timeout=self.request_timeout_seconds)
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                if response.status == 101 or response.getheader("Upgrade"):
                    self._reply_error(handler, HTTPStatus.NOT_IMPLEMENTED, "WebSockets are unsupported")
                    return
                content_length = response.getheader("Content-Length")
                if content_length and int(content_length) > _MAX_RESPONSE_BODY_BYTES:
                    self._reply_error(handler, HTTPStatus.BAD_GATEWAY, "Preview response exceeds its limit")
                    return
                result = response.read(_MAX_RESPONSE_BODY_BYTES + 1)
                if len(result) > _MAX_RESPONSE_BODY_BYTES:
                    self._reply_error(handler, HTTPStatus.BAD_GATEWAY, "Preview response exceeds its limit")
                    return
                handler.send_response(response.status)
                for key, value in response.getheaders():
                    lower = key.lower()
                    if lower in _HOP_BY_HOP_HEADERS or lower == "content-length":
                        continue
                    handler.send_header(key, value)
                handler.send_header("Content-Length", str(len(result) if method != "HEAD" else 0))
                handler.send_header("Connection", "close")
                handler.end_headers()
                if method != "HEAD":
                    handler.wfile.write(result)
                handler.close_connection = True
            finally:
                connection.close()
        except OSError as exc:
            self._reply_error(handler, HTTPStatus.BAD_GATEWAY, str(exc))
        except (ValueError, TypeError, HTTPException, PreviewRuntimeError) as exc:
            self._reply_error(handler, HTTPStatus.BAD_REQUEST, str(exc))


def _minimal_environment(manifest: PreviewManifest) -> dict[str, str]:
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/run/paw/home",
        "TMPDIR": "/tmp",
        "PORT": str(manifest.port),
        "HOST": "127.0.0.1",
        "RAG_IME_TEAM_PREVIEW": "1",
    }


def _run_prepare(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    logs_dir: Path | None = None,
) -> None:
    captures: tuple[_LogTail, _LogTail, tuple[threading.Thread, ...]] | None = None
    try:
        process, captures = _start_captured(
            command,
            cwd=cwd,
            env=env,
            logs_dir=logs_dir,
            label="prepare",
        )
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=2)
        suffix = "; see /run/paw/logs/prepare.stderr.log" if logs_dir is not None else ""
        raise PreviewRuntimeError(
            "Preview prepare command exceeded its deadline" + suffix
        ) from exc
    except OSError as exc:
        if logs_dir is not None:
            tail = _LogTail()
            tail.append(f"Preview prepare command could not start: {exc}\n".encode("utf-8", "replace"))
            _write_log_tail(Path(logs_dir) / "prepare.stderr.log", tail)
        raise PreviewRuntimeError(f"Preview prepare command could not start: {exc}") from exc
    finally:
        if captures is not None:
            _terminate_child(process)
            if logs_dir is not None:
                _capture_finished(logs_dir, "prepare", captures)
    if code != 0:
        suffix = "; see /run/paw/logs/prepare.stderr.log" if logs_dir is not None else ""
        raise PreviewRuntimeError(
            f"Preview prepare command failed with exit code {code}{suffix}"
        )


def _terminate_child(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def run_worker(
    manifest_path: Path = Path("/run/paw/agent") / MANIFEST_FILENAME,
    *,
    source_root: Path = SOURCE_ROOT,
    app_root: Path = APP_ROOT,
    socket_path: Path = SOCKET_PATH,
) -> int:
    """Run one manifest-defined app and relay until the app exits."""

    process: subprocess.Popen[bytes] | None = None
    captures: tuple[_LogTail, _LogTail, tuple[threading.Thread, ...]] | None = None
    relay: PreviewRelay | None = None
    try:
        manifest = read_preview_manifest(Path(manifest_path))
        _copy_snapshot(Path(source_root), Path(app_root))
        relay = PreviewRelay(socket_path, manifest.port)
        relay_thread = threading.Thread(
            target=relay.serve_forever,
            name="paw-preview-relay",
            daemon=True,
        )
        relay_thread.start()
        environment = _minimal_environment(manifest)
        if manifest.prepare_command is not None:
            _run_prepare(
                manifest.prepare_command,
                cwd=Path(app_root),
                env=environment,
                timeout=manifest.startup_timeout_seconds,
                logs_dir=Path(socket_path).parent,
            )
        try:
            process, captures = _start_captured(
                manifest.command,
                cwd=Path(app_root),
                env=environment,
                logs_dir=Path(socket_path).parent,
                label="app",
            )
        except OSError as exc:
            tail = _LogTail()
            tail.append(f"Preview app could not start: {exc}\n".encode("utf-8", "replace"))
            _write_log_tail(
                Path(socket_path).parent / "app.stderr.log",
                tail,
            )
            raise PreviewRuntimeError(f"Preview app could not start: {exc}") from exc
        code = int(process.wait())
        if code != 0:
            print(
                f"Preview app exited with code {code}; see /run/paw/logs/app.stderr.log",
                file=sys.stderr,
            )
        return code
    except (OSError, ValueError, PreviewRuntimeError) as exc:
        print(f"preview worker failed: {exc}", file=sys.stderr)
        return 1
    finally:
        _terminate_child(process)
        if captures is not None:
            _capture_finished(Path(socket_path).parent, "app", captures)
        if relay is not None:
            relay.close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run one isolated PAW project preview")
    parser.add_argument(
        "--manifest",
        default=str(Path("/run/paw/agent") / MANIFEST_FILENAME),
        help="trusted worker-local manifest path",
    )
    args = parser.parse_args(argv)
    return run_worker(Path(args.manifest))


if __name__ == "__main__":
    raise SystemExit(main())
