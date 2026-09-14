"""Pi Host process transport: JSONL response routing and the ordered event lane.

The manager owns Session lifecycle. This client owns only its process, streams,
request waiters and event queue; callbacks are its entire product boundary.
"""
from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping

from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.protocols import PI_HOST_PROTOCOL_VERSION
from rag_ime.pi.values import (
    PiRuntimeError, PiRuntimeCommandAcceptanceUnknown, PiRuntimeCommandRejected,
    as_mapping, redact_runtime_text,
)
from rag_ime.room_runtime_host_kill_gate import RuntimeHostKillGate, process_birth_token
from rag_ime.team.execution import (
    ExecutionLauncher,
    ExecutionProcess,
    ExecutionReceipt,
    TeamExecutionError,
)

__all__ = ["PiRuntimeHostClient"]


class PiRuntimeHostClient:
    """One long-lived process connection shared by the bounded Session host."""

    def __init__(
        self,
        config: PiRuntimeConfig,
        *,
        on_event: Callable[[dict[str, object]], None],
        on_exit: Callable[[int | None, str], None],
        kill_gate: RuntimeHostKillGate,
        owner_instance_id: str,
        launcher: ExecutionLauncher | None = None,
    ) -> None:
        self.config = config
        self.on_event = on_event
        self.on_exit = on_exit
        self.kill_gate = kill_gate
        self.owner_instance_id = owner_instance_id
        self.launcher = launcher
        self.host_identity = f"pi-host:{uuid.uuid4()}"
        self.job_identity = f"pi-job:{uuid.uuid4()}"
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[str, queue.Queue[object]] = {}
        # Host responses and host events share stdout, but they must not share
        # one execution lane. Event projection can touch SQLite, Room state, or
        # even issue a nested control RPC; running it in the stdout reader can
        # therefore delay or deadlock an otherwise immediate command ACK.
        self._event_queue: queue.Queue[object] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=32)
        self._process: ExecutionProcess | None = None
        self._stdout_thread: threading.Thread | None = None
        self._event_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stopping = False
        self._execution_cleanup_done = False
        self._execution_stop_receipt: ExecutionReceipt | None = None
        self._execution_stop_error = ""

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def execution_stop_receipt(self) -> ExecutionReceipt | None:
        """Return the last verified team launcher stop receipt, if any."""

        with self._lock:
            return self._execution_stop_receipt

    @property
    def execution_stop_error(self) -> str:
        with self._lock:
            return self._execution_stop_error

    def start(self) -> dict[str, object]:
        with self._lock:
            if self.running:
                raise PiRuntimeError("Pi Runtime Host is already running")
            self.config.agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.config.session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.config.logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.config.prepare_agent_config()
            command = self.config.launch_host_command()
            execution_spec = self.config.execution_spec
            if execution_spec is not None and self.launcher is None:
                raise PiRuntimeError(
                    "isolated team execution requires a trusted Host launcher"
                )
            # Reset lifecycle cleanup state before attempting a replacement
            # process. A failed start after a prior explicit stop must still
            # tear down the newly-created process/container.
            self._execution_cleanup_done = False
            self._execution_stop_receipt = None
            self._execution_stop_error = ""
            try:
                environment = self.config.child_environment()
                environment["RAG_IME_RUNTIME_HOST_IDENTITY"] = self.host_identity
                environment["RAG_IME_RUNTIME_JOB_IDENTITY"] = self.job_identity
                if execution_spec is not None:
                    assert self.launcher is not None
                    process = self.launcher.start(
                        execution_spec,
                        command,
                        environment,
                    )
                else:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=self.config.agent_dir,
                        env=environment,
                        bufsize=0,
                        start_new_session=os.name == "posix",
                    )
                if not isinstance(process, ExecutionProcess):
                    raise TeamExecutionError(
                        "Host launcher did not return a process-compatible pipe handle"
                    )
                self._process = process
            except OSError as exc:
                raise PiRuntimeError(f"failed to start managed Pi Runtime Host: {exc}") from exc
            process_group_id = (
                os.getpgid(self._process.pid) if os.name == "posix" else self._process.pid
            )
            try:
                self.kill_gate.register_process(
                    host_identity=self.host_identity,
                    owner_instance_id=self.owner_instance_id,
                    pid=self._process.pid,
                    process_group_id=process_group_id,
                    job_identity=self.job_identity,
                    process_birth_token=process_birth_token(self._process.pid),
                    executable_ref=str(
                        self.config.executable
                        or (
                            self.config.execution_spec.runtime_command[0]
                            if self.config.execution_spec is not None
                            else ""
                        )
                    ),
                    now_ms=int(time.time() * 1000),
                )
            except Exception:
                process = self._process
                if process is not None:
                    self._cleanup_execution(process)
                for stream in (
                    process.stdin if process is not None else None,
                    process.stdout if process is not None else None,
                    process.stderr if process is not None else None,
                ):
                    if stream is not None:
                        stream.close()
                self._process = None
                raise
            self._stopping = False
            # A client is normally one-shot, but resetting the lane here keeps
            # an explicit stop/start from inheriting the previous sentinel.
            self._event_queue = queue.Queue()
            self._stdout_thread = threading.Thread(
                target=self._read_stdout,
                name="rag-ime-pi-host-stdout",
                daemon=True,
            )
            self._event_thread = threading.Thread(
                target=self._read_events,
                name="rag-ime-pi-host-events",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                name="rag-ime-pi-host-stderr",
                daemon=True,
            )
            self._event_thread.start()
            self._stdout_thread.start()
            self._stderr_thread.start()
        return self.send("hello")

    def send(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        request_id = str(uuid.uuid4())
        request = {
            "protocolVersion": PI_HOST_PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        response_queue: queue.Queue[object] = queue.Queue(maxsize=1)
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                raise PiRuntimeCommandRejected(
                    "Pi Runtime Host is not running",
                    host_error_code="RUNTIME_NOT_RUNNING",
                )
            self._pending[request_id] = response_queue
        try:
            self._write_record(
                process,
                request,
                before_write=before_write,
            )
        except (BrokenPipeError, OSError) as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise PiRuntimeCommandAcceptanceUnknown(
                "Pi Runtime Host stdin closed after command dispatch"
            ) from exc
        except BaseException:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        try:
            response = response_queue.get(timeout=timeout or self.config.command_timeout_seconds)
        except queue.Empty as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise PiRuntimeCommandAcceptanceUnknown(
                f"Pi Runtime Host command timed out: {method}"
            ) from exc
        if isinstance(response, BaseException):
            raise PiRuntimeCommandAcceptanceUnknown(str(response)) from response
        assert isinstance(response, dict)
        if response.get("ok") is not True:
            error = as_mapping(response.get("error"))
            raise PiRuntimeCommandRejected(
                str(
                    error.get("message")
                    or f"Pi Runtime Host command failed: {method}"
                ),
                host_error_code=str(
                    error.get("code") or "RUNTIME_REJECTED"
                ),
            )
        return dict(as_mapping(response.get("result")))

    def _write_record(
        self,
        process: ExecutionProcess,
        request: Mapping[str, object],
        *,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        encoded = (
            json.dumps(
                dict(request),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        with self._write_lock:
            if process.stdin is None:
                raise BrokenPipeError("Pi Runtime Host stdin is unavailable")
            if before_write is not None:
                before_write()
            process.stdin.write(encoded)
            process.stdin.flush()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            process = self._process
        if process is None:
            return
        cleanup_error = self._cleanup_execution(process)
        self._fail_pending(PiRuntimeError("Pi Runtime Host stopped"))
        current = threading.current_thread()
        for thread in (self._stdout_thread, self._event_thread, self._stderr_thread):
            if thread is not None and thread is not current:
                thread.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        with self._lock:
            self._process = None
        self.kill_gate.mark_terminated(self.host_identity, now_ms=int(time.time() * 1000))
        if cleanup_error is not None:
            raise cleanup_error

    def _cleanup_execution(self, process: ExecutionProcess) -> TeamExecutionError | None:
        """Stop the trusted execution backend before closing the Pi pipes."""

        execution_spec = self.config.execution_spec
        launcher = self.launcher
        with self._lock:
            if self._execution_cleanup_done:
                return (
                    TeamExecutionError("isolated execution stop could not be verified")
                    if self._execution_stop_error
                    else None
                )
            self._execution_cleanup_done = True
        cleanup_error: TeamExecutionError | None = None
        if execution_spec is not None and launcher is not None:
            try:
                receipt = launcher.stop(execution_spec, process)
            except Exception as exc:
                # A failed verification is observable and never represented as
                # a successful receipt.  Still terminate the local Docker
                # client so reader threads and the kill gate can converge.
                with self._lock:
                    self._execution_stop_error = redact_runtime_text(str(exc))
                cleanup_error = TeamExecutionError(
                    "isolated execution stop could not be verified"
                )
            else:
                with self._lock:
                    self._execution_stop_receipt = receipt
            if process.poll() is None:
                self._terminate_local_process(process)
            return cleanup_error
        self._terminate_local_process(process)
        return None

    @staticmethod
    def _terminate_local_process(process: ExecutionProcess) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def diagnostic_error(self) -> str:
        return redact_runtime_text(self._stderr[-1] if self._stderr else "")

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        # stdin stays unbuffered for immediate control dispatch, but a raw
        # FileIO.readline() reads large JSONL replies one byte at a time and
        # holds every following command response behind optional inspection.
        # Popen(bufsize=0) creates a raw FileIO stream, not a buffered pipe.
        assert isinstance(process.stdout, io.RawIOBase)
        stdout = io.BufferedReader(process.stdout, buffer_size=64 * 1024)
        protocol_error = ""
        while True:
            raw = stdout.readline()
            if not raw:
                break
            try:
                value = json.loads(raw.rstrip(b"\r\n").decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("host record is not an object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                protocol_error = f"invalid Pi Runtime Host JSONL: {exc}"
                continue
            if value.get("id") and isinstance(value.get("ok"), bool):
                with self._lock:
                    waiter = self._pending.pop(str(value["id"]), None)
                if waiter is not None:
                    waiter.put_nowait(value)
                continue
            # Never run product event projection in the response reader. A slow
            # observer must not hold a Room dispatch lease open while the typed
            # dispatch ACK is already waiting on the next JSONL line.
            self._event_queue.put(value)
        # The stdout reader owns the event-lane sentinel. It is emitted only
        # after every preceding JSONL event has been queued, so stop/EOF cannot
        # cut in front of a terminal agent_settled projection.
        self._event_queue.put(None)
        exit_code = process.poll()
        if exit_code is None:
            try:
                exit_code = process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                exit_code = None
        error = protocol_error or self.diagnostic_error()
        self.on_exit(exit_code, error)
        self.kill_gate.mark_terminated(self.host_identity, now_ms=int(time.time() * 1000))
        self._cleanup_execution(process)
        # Wake RPC callers only after the manager and durable process registry
        # agree the Host is terminal; callers must not observe a stale ready state.
        self._fail_pending(PiRuntimeError(error or "Pi Runtime Host exited"))
        event_thread = self._event_thread
        if event_thread is not None and event_thread is not threading.current_thread():
            event_thread.join(timeout=2)
        stderr_thread = self._stderr_thread
        if stderr_thread is not None and stderr_thread is not threading.current_thread():
            stderr_thread.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        with self._lock:
            if self._process is process:
                self._process = None

    def _read_events(self) -> None:
        while True:
            value = self._event_queue.get()
            if value is None:
                return
            assert isinstance(value, dict)
            try:
                self.on_event(value)
            except Exception:
                # Event projections are secondary to the Runtime protocol. A
                # failed projection cannot stop response routing or other
                # Sessions; the event stores keep their own observable errors.
                continue

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            raw = process.stderr.readline()
            if not raw:
                return
            self._stderr.append(raw.decode("utf-8", errors="replace").strip())

    def _fail_pending(self, error: BaseException) -> None:
        with self._lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
        for waiter in pending:
            try:
                waiter.put_nowait(error)
            except queue.Full:
                pass
