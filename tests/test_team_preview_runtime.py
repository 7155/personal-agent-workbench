from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import socket
import socketserver
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rag_ime.team.execution import ExecutionReceipt, TeamExecutionError
from rag_ime.team.preview_runtime import (
    MANIFEST_FILENAME,
    PreviewManifestError,
    PreviewRuntime,
    PreviewRuntimeError,
    PreviewUnavailable,
    load_preview_manifest,
    validate_preview_manifest,
)
from rag_ime.team.preview_worker import (
    _MAX_LOG_TAIL_BYTES,
    _copy_snapshot,
    _run_prepare,
    run_worker,
)


class _SyntheticProcess:
    pid = 1234

    def __init__(self) -> None:
        self._code: int | None = None
        self.stdin = None
        self.stdout = None
        self.stderr = None

    def poll(self) -> int | None:
        return self._code

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self._code = 0
        return 0

    def terminate(self) -> None:
        self._code = 0

    def kill(self) -> None:
        self._code = -9


class _SyntheticHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


class _SyntheticLauncher:
    def __init__(self, *, status: int = 200, start_error: Exception | None = None) -> None:
        self.status = status
        self.start_error = start_error
        self.starts: list[tuple[object, object, object]] = []
        self.stops: list[object] = []
        self.processes: list[_SyntheticProcess] = []
        self.servers: list[_SyntheticHTTPServer] = []
        self.server_threads: list[threading.Thread] = []

    def start(self, spec, command, environment):
        self.starts.append((spec, tuple(command), dict(environment)))
        if self.start_error is not None:
            raise self.start_error
        spec.logs_dir.mkdir(parents=True, exist_ok=True)
        path = spec.logs_dir / "preview.sock"
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                request = self.rfile.readline(64 * 1024)
                if not request:
                    return
                headers: dict[str, str] = {}
                while True:
                    line = self.rfile.readline(64 * 1024)
                    if line in {b"\r\n", b"\n", b""}:
                        break
                    if b":" in line:
                        key, value = line.decode().split(":", 1)
                        headers[key.lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                body = self.rfile.read(length)
                payload = json.dumps(
                    {"method": request.decode().split(" ", 1)[0], "body": body.decode()},
                    separators=(",", ":"),
                ).encode()
                status = owner.status
                self.wfile.write(
                    f"HTTP/1.1 {status} {'OK' if status == 200 else 'FAIL'}\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
                    "Connection: close\r\n\r\n".encode()
                    + payload
                )

        server = _SyntheticHTTPServer(str(path), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        process = _SyntheticProcess()
        self.servers.append(server)
        self.server_threads.append(thread)
        self.processes.append(process)
        return process

    def stop(self, spec, process):
        self.stops.append(spec.container_id)
        process.terminate()
        index = len(self.stops) - 1
        server = self.servers[index]
        server.shutdown()
        server.server_close()
        (spec.logs_dir / "preview.sock").unlink(missing_ok=True)
        return ExecutionReceipt(
            spec.attempt_id,
            spec.session_id,
            spec.container_id,
            "stop",
            "stopped",
            True,
            detail="synthetic removal verified",
        )


class _UnverifiedLauncher(_SyntheticLauncher):
    def stop(self, spec, process):
        self.stops.append(spec.container_id)
        return ExecutionReceipt(
            spec.attempt_id,
            spec.session_id,
            spec.container_id,
            "stop",
            "unknown",
            False,
            detail="synthetic container remains",
        )


class _DiagnosticLauncher(_SyntheticLauncher):
    def start(self, spec, command, environment):
        process = super().start(spec, command, environment)
        (spec.logs_dir / "app.stderr.log").write_bytes(b"x" * (2 * _MAX_LOG_TAIL_BYTES))
        return process


class PreviewManifestTests(unittest.TestCase):
    def test_manifest_is_versioned_and_bounded(self) -> None:
        manifest = validate_preview_manifest(
            {
                "schemaVersion": 1,
                "command": ["python3", "-m", "demo"],
                "port": 3000,
                "healthPath": "/health?ready=1",
                "prepareCommand": ["python3", "-c", "pass"],
                "startupTimeoutSeconds": 30,
            }
        )
        self.assertEqual(manifest.command[0], "python3")
        self.assertEqual(manifest.as_dict()["port"], 3000)

    def test_manifest_rejects_unknown_fields_controls_and_bad_paths(self) -> None:
        base = {
            "schemaVersion": 1,
            "command": ["python3"],
            "port": 3000,
            "healthPath": "/",
        }
        for change in (
            {"unexpected": True},
            {"healthPath": "https://host/"},
            {"command": ["python3\n"]},
            {"port": 0},
            {"startupTimeoutSeconds": 121},
        ):
            with self.subTest(change=change), self.assertRaises(PreviewManifestError):
                validate_preview_manifest({**base, **change})

    def test_manifest_file_is_regular_and_loaded_from_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-preview-manifest-") as raw:
            root = Path(raw)
            (root / MANIFEST_FILENAME).write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "command": ["python3"],
                        "port": 3000,
                        "healthPath": "/",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_preview_manifest(root).port, 3000)
            (root / "manifest-link").symlink_to(root / MANIFEST_FILENAME)
            with self.assertRaises(PreviewManifestError):
                from rag_ime.team.preview_runtime import read_preview_manifest

                read_preview_manifest(root / "manifest-link")


class PreviewRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="paw-preview-runtime-", dir="/tmp"
        )
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "command": ["python3", "-m", "preview_app"],
                    "port": 3000,
                    "healthPath": "/",
                    "startupTimeoutSeconds": 1,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _runtime(self, launcher=None, **kwargs):
        return PreviewRuntime(
            runtime_root=self.root / "runtime",
            image="reviewed-preview:1",
            launcher=launcher or _SyntheticLauncher(),
            container_user="501:20",
            **kwargs,
        )

    def test_start_mounts_source_readonly_and_request_uses_unix_relay(self) -> None:
        launcher = _SyntheticLauncher()
        runtime = self._runtime(launcher)
        handle = runtime.start("deployment-1", self.source)
        self.assertTrue(handle.healthy)
        self.assertTrue(handle.spec.workspace_read_only)
        self.assertEqual(handle.spec.workspace_root, self.source.resolve())
        self.assertNotEqual(handle.spec.scope_root, handle.spec.workspace_root)
        self.assertNotIn("RAG_IME_TOOL_GATEWAY_TOKEN", launcher.starts[0][2])
        response = runtime.request(
            "deployment-1",
            "POST",
            "/submit",
            {"Content-Type": "application/json"},
            b"ok",
        )
        self.assertEqual(response.status, 200)
        self.assertIn(b"POST", response.body)
        receipt = runtime.stop("deployment-1")
        self.assertTrue(receipt.verified)
        self.assertFalse(runtime.is_running("deployment-1"))

    def test_duplicate_deployment_and_invalid_source_fail_closed(self) -> None:
        runtime = self._runtime()
        runtime.start("deployment-1", self.source)
        with self.assertRaises(PreviewRuntimeError):
            runtime.start("deployment-1", self.source)
        with self.assertRaises(PreviewRuntimeError):
            runtime.start("deployment-2", self.root / "missing")
        runtime.stop("deployment-1")

    def test_health_failure_stops_and_cleans_worker(self) -> None:
        launcher = _SyntheticLauncher(status=503)
        runtime = self._runtime(launcher)
        with self.assertRaisesRegex(PreviewRuntimeError, "health"):
            runtime.start("unhealthy", self.source)
        self.assertEqual(len(launcher.stops), 1)
        self.assertFalse(runtime.handles())
        self.assertTrue(runtime.stop("unhealthy").verified)

    def test_failed_start_retains_bounded_private_diagnostics(self) -> None:
        launcher = _DiagnosticLauncher(status=503)
        runtime = self._runtime(launcher)
        with self.assertRaisesRegex(PreviewRuntimeError, "health"):
            runtime.start("diagnostic-failure", self.source)
        diagnostic = runtime.diagnostics_root / "diagnostic-failure" / "app.stderr.log"
        self.assertLessEqual(diagnostic.stat().st_size, _MAX_LOG_TAIL_BYTES)
        self.assertTrue(diagnostic.read_bytes().endswith(b"x" * _MAX_LOG_TAIL_BYTES))

    def test_start_error_does_not_claim_a_running_preview(self) -> None:
        launcher = _SyntheticLauncher(start_error=TeamExecutionError("daemon unavailable"))
        runtime = self._runtime(launcher)
        with self.assertRaises(PreviewRuntimeError):
            runtime.start("failed-start", self.source)
        self.assertFalse(runtime.handles())
        self.assertFalse(runtime.is_running("failed-start"))
        self.assertTrue(runtime.stop("failed-start").verified)

    def test_request_limits_and_missing_worker_are_explicit(self) -> None:
        runtime = self._runtime(max_request_body_bytes=4)
        runtime.start("limits", self.source)
        with self.assertRaisesRegex(PreviewRuntimeError, "body"):
            runtime.request("limits", "POST", "/", body=b"12345")
        with self.assertRaisesRegex(PreviewRuntimeError, "unsupported"):
            runtime.request("limits", "CONNECT", "/")
        with self.assertRaisesRegex(PreviewRuntimeError, "path"):
            runtime.request("limits", "GET", "http://host/")
        runtime.stop("limits")
        with self.assertRaises(PreviewUnavailable):
            runtime.request("limits", "GET", "/")

    def test_unverified_stop_is_not_reported_as_success(self) -> None:
        launcher = _UnverifiedLauncher()
        runtime = self._runtime(launcher)
        runtime.start("stop-fail", self.source)
        with self.assertRaisesRegex(PreviewRuntimeError, "not verified"):
            runtime.stop("stop-fail")
        self.assertTrue(runtime.handles())

    def test_recover_stops_owned_journal_without_launching(self) -> None:
        launcher = _SyntheticLauncher()
        runtime = self._runtime(launcher)
        handle = runtime.start("recoverable", self.source)
        runtime._write_journal(
            handle.journal_path,
            runtime._journal_payload(
                handle,
                deployment_id="recoverable",
                manifest=handle.manifest,
                spec=handle.spec,
                state="healthy",
            ),
        )
        runtime._handles.clear()
        recovered = runtime.recover("recoverable")
        self.assertEqual(len(recovered), 1)
        self.assertFalse(runtime.handles())

    def test_shutdown_is_a_permanent_start_barrier(self) -> None:
        launcher = _SyntheticLauncher()
        runtime = self._runtime(launcher)
        runtime.shutdown()
        with self.assertRaisesRegex(PreviewRuntimeError, "shut down"):
            runtime.start("after-shutdown", self.source)
        self.assertEqual(launcher.starts, [])

    def test_shutdown_during_health_stops_without_late_healthy_state(self) -> None:
        launcher = _SyntheticLauncher(status=503)
        runtime = self._runtime(launcher)
        failures: list[Exception] = []

        def start() -> None:
            try:
                runtime.start("shutdown-race", self.source)
            except Exception as exc:  # noqa: BLE001 - assertion captures the boundary
                failures.append(exc)

        thread = threading.Thread(target=start, daemon=True)
        thread.start()
        deadline = time.time() + 3
        while not runtime.handles() and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(runtime.handles())
        runtime.shutdown()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(failures)
        self.assertFalse(runtime.handles())
        self.assertEqual(len(launcher.stops), 1)


class PreviewWorkerTests(unittest.TestCase):
    def test_prepare_failure_and_private_copy_leave_bounded_operator_logs(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="paw-preview-worker-", dir="/tmp"
        ) as raw:
            root = Path(raw)
            source = root / "source"
            destination = root / "app"
            logs = root / "logs"
            source.mkdir()
            logs.mkdir()
            source_file = source / "readonly.txt"
            source_file.write_text("snapshot\n", encoding="utf-8")
            source_file.chmod(0o444)
            _copy_snapshot(source, destination)
            copied_mode = (destination / source_file.name).stat().st_mode
            self.assertTrue(copied_mode & stat.S_IWUSR)
            self.assertFalse(copied_mode & (stat.S_IWGRP | stat.S_IWOTH))
            with self.assertRaisesRegex(PreviewRuntimeError, "prepare command"):
                _run_prepare(
                    (
                        sys.executable,
                        "-c",
                        "import sys; print('o'*200000); print('e'*200000, file=sys.stderr); sys.exit(7)",
                    ),
                    cwd=destination,
                    env={"PATH": "/usr/bin"},
                    timeout=5,
                    logs_dir=logs,
                )
            self.assertLessEqual(
                (logs / "prepare.stdout.log").stat().st_size,
                _MAX_LOG_TAIL_BYTES,
            )
            self.assertLessEqual(
                (logs / "prepare.stderr.log").stat().st_size,
                _MAX_LOG_TAIL_BYTES,
            )
            self.assertTrue(
                (logs / "prepare.stderr.log").read_bytes().endswith(b"e" * 65_000 + b"\n")
            )

    def test_app_failure_leaves_bounded_logs_without_forwarding_raw_output(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="paw-preview-worker-", dir="/tmp"
        ) as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            manifest = {
                "schemaVersion": 1,
                "command": [
                    sys.executable,
                    "-c",
                    "import sys; print('o'*200000); print('e'*200000, file=sys.stderr); sys.exit(7)",
                ],
                "port": 3000,
                "healthPath": "/",
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            logs = root / "logs"
            logs.mkdir()
            code = run_worker(
                manifest_path,
                source_root=source,
                app_root=root / "app",
                socket_path=logs / "preview.sock",
            )
            self.assertEqual(code, 7)
            self.assertLessEqual(
                (logs / "app.stdout.log").stat().st_size,
                _MAX_LOG_TAIL_BYTES,
            )
            self.assertLessEqual(
                (logs / "app.stderr.log").stat().st_size,
                _MAX_LOG_TAIL_BYTES,
            )


if __name__ == "__main__":
    unittest.main()
