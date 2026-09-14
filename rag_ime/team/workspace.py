"""Workspace tool execution inside an already-isolated team worker.

The team Gateway must route workspace operations here after the Pi Host has
entered its OCI scope.  Calling this adapter from the server process is
deliberately rejected: the existing ``WorkspaceHarness`` is safe for the
personal macOS path, but its ordinary file methods are not the OS isolation
boundary for a multi-user server.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from rag_ime.agent_workspace import WorkspaceHarness, WorkspaceHarnessError

from .execution import ExecutionSpec, TeamExecutionError


__all__ = [
    "RemotePreparedWorkspace",
    "TeamWorkspaceExecutor",
    "execute_workspace",
]



@dataclass(frozen=True)
class RemotePreparedWorkspace:
    """Serializable preparation handle retained by the host adapter.

    Approval rows persist the preview's action and base state.  This object
    exists only between ``prepare_*`` and ``preview`` in one host request; the
    eventual apply call re-prepares inside the worker and checks the supplied
    fence there, so a host-side object cannot become an authority cache.
    """

    session_id: str
    tool: str
    operation: str
    args: Mapping[str, object]
    preview_payload: Mapping[str, object]
    workspace_roots: tuple[str, ...] = ()

    @property
    def preview(self) -> dict[str, object]:
        return dict(self.preview_payload)

    @property
    def action_payload(self) -> Mapping[str, object]:
        value = self.preview_payload.get("actionPayload")
        return value if isinstance(value, Mapping) else self.args

    @property
    def base_state(self) -> Mapping[str, object]:
        value = self.preview_payload.get("baseState")
        return value if isinstance(value, Mapping) else {}

    @property
    def command(self) -> str:
        return str(self.action_payload.get("command") or self.args.get("command") or "")

    @property
    def cwd(self) -> Path:
        return Path(str(self.action_payload.get("cwd") or "/workspace"))

    @property
    def source_read_only(self) -> bool:
        return bool(self.base_state.get("sourceReadOnly"))

    @property
    def existed_before(self) -> bool:
        return bool(self.base_state.get("existedBefore"))

    @property
    def resource_revision(self) -> str:
        return str(self.action_payload.get("resourceRevision") or "")

    @property
    def roots_digest(self) -> str:
        return str(
            self.base_state.get("workspaceRootsSha256")
            or self.base_state.get("workspaceRootSha256")
            or ""
        )


class TeamWorkspaceExecutor:
    """Dispatch workspace tools to the worker-local WorkspaceHarness.

    ``RAG_IME_TEAM_WORKER=1`` is set only in the OCI worker image.  The marker
    is a guard against accidental server-side use; the actual security
    boundary remains the container mounts and Linux runtime policy.
    """

    def __init__(
        self,
        *,
        harness: WorkspaceHarness | None = None,
        max_output_bytes: int = 1_048_576,
    ) -> None:
        self.max_output_bytes = max(16_384, min(int(max_output_bytes), 4 * 1024 * 1024))
        self.harness = harness or WorkspaceHarness(
            executor=self._execute_command,
            lsp_process_sandbox=False,
            max_output_bytes=self.max_output_bytes,
        )

    def execute_workspace(
        self,
        spec: ExecutionSpec,
        session: Mapping[str, object],
        tool: str,
        args: Mapping[str, object],
        *,
        operation: str | None = None,
        base_state: Mapping[str, object] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Execute one bounded workspace operation in the worker process.

        ``workspace_job`` and ``workspace_lsp`` remain explicit unsupported
        operations until a worker-side durable job/LSP owner is attached. This
        prevents the server from silently falling back to its host services.
        """

        if os.environ.get("RAG_IME_TEAM_WORKER") != "1":
            raise TeamExecutionError(
                "team workspace operations require the isolated worker boundary"
            )
        root = Path(spec.container_workspace)
        if not root.is_dir() or root.is_symlink():
            raise TeamExecutionError("worker workspace mount is unavailable")
        worker_session = self._worker_session(session, root)
        name = str(tool or "").strip()
        action = str(operation or self._default_operation(name)).strip()
        payload = dict(args)

        try:
            if action == "prepare":
                if name == "workspace_shell":
                    return self.harness.preview(
                        self.harness.prepare_command(worker_session, payload)
                    )
                if name == "workspace_patch":
                    prepared_patch = self.harness.prepare_patch(worker_session, payload)
                    return self.harness.patch_preview(prepared_patch)
                if name == "workspace_edit":
                    prepared_edit = self.harness.prepare_edit(worker_session, payload)
                    return self.harness.edit_preview(prepared_edit)
                if name == "workspace_write":
                    prepared_write = self.harness.prepare_write(worker_session, payload)
                    return self.harness.write_preview(prepared_write)
                raise TeamExecutionError(
                    f"unsupported worker workspace preparation: {name}"
                )
            if name == "workspace_read" and action == "file_editability":
                return self.harness.file_editability(
                    worker_session,
                    str(payload.get("path") or ""),
                )
            if name == "workspace_write" and action == "save_file":
                return self.harness.save_file(worker_session, payload)
            if name == "workspace_list" and action == "list":
                return self.harness.list(worker_session, payload)
            if name == "workspace_read" and action == "read":
                return self.harness.read(worker_session, payload)
            if name == "workspace_search" and action == "search":
                return self.harness.search(worker_session, payload)
            if name == "workspace_shell" and action == "run":
                prepared_command = self.harness.prepare_command(worker_session, payload)
                return self._execute_command(prepared_command, cancelled=cancelled)
            if name == "workspace_patch" and action == "apply":
                return self.harness.apply_patch(
                    worker_session,
                    payload,
                    self._required_base_state(base_state),
                )
            if name == "workspace_edit" and action == "apply":
                return self.harness.apply_edit(
                    worker_session,
                    payload,
                    self._required_base_state(base_state),
                )
            if name == "workspace_write" and action == "apply":
                return self.harness.apply_write(
                    worker_session,
                    payload,
                    self._required_base_state(base_state),
                )
        except (WorkspaceHarnessError, OSError, ValueError) as exc:
            raise TeamExecutionError(str(exc)) from exc

        if name == "workspace_job":
            raise TeamExecutionError(
                "workspace_job requires an attached worker-side durable job owner"
            )
        if name == "workspace_lsp":
            raise TeamExecutionError(
                "workspace_lsp requires an attached worker-side LSP owner"
            )
        raise TeamExecutionError(f"unsupported worker workspace operation: {name}/{action}")

    @staticmethod
    def _default_operation(tool: str) -> str:
        return {
            "workspace_list": "list",
            "workspace_read": "read",
            "workspace_search": "search",
            "workspace_shell": "run",
            "workspace_patch": "apply",
            "workspace_edit": "apply",
            "workspace_write": "apply",
        }.get(tool, "")

    @staticmethod
    def _required_base_state(base_state: Mapping[str, object] | None) -> Mapping[str, object]:
        if base_state is None:
            raise TeamExecutionError(
                "workspace mutation requires the approval base state from the server"
            )
        return base_state

    @staticmethod
    def _worker_session(
        session: Mapping[str, object],
        root: Path,
    ) -> dict[str, object]:
        projected = dict(session)
        projected["workspaceRoots"] = [str(root)]
        # A full-access personal policy must not widen the worker Harness to
        # ``/``. OS-level isolation remains the last boundary, while this
        # projection preserves ordinary bounded workspace policy and receipts.
        projected["toolProfileVersion"] = "control-center-v1"
        if str(projected.get("executionMode") or "").strip().lower() not in {
            "read_only",
            "per_action",
        }:
            projected["executionMode"] = "per_action"
        return projected

    def _execute_command(
        self,
        prepared: object,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        # Keep the import boundary narrow while allowing WorkspaceHarness to
        # own command validation and its immutable PreparedWorkspaceCommand.
        command = str(getattr(prepared, "command"))
        cwd = Path(getattr(prepared, "cwd"))
        timeout_seconds = int(getattr(prepared, "timeout_seconds"))
        started_at_ms = int(time.time() * 1_000)
        environment = {
            "HOME": os.environ.get("HOME", "/run/paw/home"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        }
        try:
            process = subprocess.Popen(
                ["/bin/sh", "-lc", command],
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=os.name == "posix",
                bufsize=0,
            )
        except OSError as exc:
            raise TeamExecutionError(f"worker command could not start: {exc}") from exc

        output = bytearray()
        output_limited = False
        timed_out = False
        selector = selectors.DefaultSelector()
        if process.stdout is not None:
            selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + max(1, timeout_seconds)
        try:
            while selector.get_map():
                if cancelled is not None and cancelled():
                    timed_out = True
                    self._terminate(process)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._terminate(process)
                    break
                for key, _ in selector.select(timeout=0.1):
                    file_object = key.fileobj
                    descriptor = (
                        file_object
                        if isinstance(file_object, int)
                        else file_object.fileno()
                    )
                    chunk = os.read(descriptor, 65_536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output_was_full = len(output) >= self.max_output_bytes
                    output_would_overflow = len(output) + len(chunk) > self.max_output_bytes
                    if len(output) < self.max_output_bytes:
                        output.extend(chunk[: self.max_output_bytes - len(output)])
                    if output_was_full or output_would_overflow:
                        output_limited = True
                        self._terminate(process)
                        break
                if process.poll() is not None and not selector.get_map():
                    break
            if process.poll() is None:
                self._terminate(process)
            try:
                exit_code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait(timeout=2)
        finally:
            selector.close()
            if process.stdout is not None:
                process.stdout.close()
        decoded = bytes(output).decode("utf-8", errors="replace")
        return {
            "schemaVersion": "rag-ime.workspace-command-receipt.v1",
            "mutationApplied": False,
            "validationSucceeded": int(exit_code) == 0 and not timed_out and not output_limited,
            "summary": (
                "命令执行超时"
                if timed_out
                else "命令输出超过上限，已停止"
                if output_limited
                else f"命令执行完成，退出码 {exit_code}"
            ),
            "commandSha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "cwd": str(cwd),
            "exitCode": int(exit_code),
            "durationMs": max(0, int(time.time() * 1_000) - started_at_ms),
            "timedOut": timed_out,
            "outputLimited": output_limited,
            "networkAllowed": False,
            "sourceReadOnly": bool(getattr(prepared, "source_read_only", False)),
            "temporaryWritesDiscarded": bool(getattr(prepared, "source_read_only", False)),
            "output": decoded,
            "outputBytes": len(output),
            "undoAvailable": False,
        }

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:  # pragma: no cover - team workers are Linux
                process.terminate()
        except (OSError, ProcessLookupError):
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:  # pragma: no cover - team workers are Linux
                    process.kill()
            except (OSError, ProcessLookupError):
                process.kill()


def execute_workspace(
    spec: ExecutionSpec,
    session: Mapping[str, object],
    tool: str,
    args: Mapping[str, object],
    *,
    operation: str | None = None,
    base_state: Mapping[str, object] | None = None,
    cancelled: Callable[[], bool] | None = None,
    executor: TeamWorkspaceExecutor | None = None,
) -> dict[str, object]:
    """Convenience worker entrypoint with the same narrow execution contract."""

    worker = executor or TeamWorkspaceExecutor()
    return worker.execute_workspace(
        spec,
        session,
        tool,
        args,
        operation=operation,
        base_state=base_state,
        cancelled=cancelled,
    )
