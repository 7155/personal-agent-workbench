"""Host-side WorkspaceHarness adapter for the isolated team worker.

The existing AgentTool approval flow expects a WorkspaceHarness-shaped object:
prepare a preview, persist its action/base state, then apply the exact payload
after approval.  This adapter keeps that shape while every operation is
performed by ``workspace_rpc`` through ``TeamExecutionCoordinator``.  It
never opens a project file in the server process.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any

from rag_ime.agent_workspace import WorkspaceHarness, WorkspaceHarnessError

from .workspace import RemotePreparedWorkspace


__all__ = ["TeamRemoteWorkspaceHarness"]


_WORKER_ROOT = Path("/workspace")
_PATH_KEYS = frozenset(
    {"path", "cwd", "root", "workspaceRoot", "workspaceRoots", "requestedPath"}
)
_SESSION_KEYS = frozenset(
    {
        "id",
        "mode",
        "executionMode",
        "toolProfileVersion",
        "allowedTools",
        "workspaceRoots",
        "workspaceReadOnly",
        "projectContextEnabled",
    }
)
_MAX_RPC_OUTPUT = 2 * 1024 * 1024


class TeamRemoteWorkspaceHarness:
    """WorkspaceHarness-compatible RPC facade rooted in one team Session."""

    def __init__(
        self,
        coordinator: Any,
        space_id: str,
        *,
        command: tuple[str, ...] | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        if coordinator is None or not callable(getattr(coordinator, "run_isolated", None)):
            raise ValueError("team workspace coordinator must expose run_isolated")
        normalized_space = str(space_id).strip()
        if not normalized_space:
            raise ValueError("team workspace adapter requires a space id")
        timeout = float(timeout_seconds)
        if not 1.0 <= timeout <= 300.0:
            raise ValueError("team workspace RPC timeout is out of bounds")
        configured_command: object = command or getattr(
            coordinator,
            "workspace_rpc_command",
            ("python3", "-m", "rag_ime.team.workspace_rpc"),
        )
        if isinstance(configured_command, (str, bytes)) or not isinstance(
            configured_command, Sequence
        ):
            raise ValueError("team workspace RPC command must be a sequence")
        normalized_command = tuple(str(part) for part in configured_command)
        if not normalized_command or any(
            not part or any(ord(char) < 32 for char in part)
            for part in normalized_command
        ):
            raise ValueError("team workspace RPC command must be a safe argv")
        self.coordinator = coordinator
        self.space_id = normalized_space
        self.command = normalized_command
        self.timeout_seconds = timeout

    @staticmethod
    def _session_id(session: Mapping[str, object]) -> str:
        session_id = str(session.get("id") or "").strip()
        if not session_id:
            raise WorkspaceHarnessError("team workspace requests require a Session id")
        return session_id

    @staticmethod
    def _workspace_root(session: Mapping[str, object]) -> Path:
        values = session.get("workspaceRoots")
        if not isinstance(values, list) or len(values) != 1:
            raise WorkspaceHarnessError(
                "team workspace execution requires exactly one assigned checkout"
            )
        raw = str(values[0] or "").strip()
        if not raw:
            raise WorkspaceHarnessError("team Session has no assigned checkout")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute() or candidate.is_symlink():
            raise WorkspaceHarnessError("team Session checkout is unavailable")
        try:
            root = candidate.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceHarnessError("team Session checkout is unavailable") from exc
        # ``resolve`` may normalize a platform-managed parent such as macOS
        # ``/var``.  The final checkout itself was checked above and the
        # resolved target must still be a real directory.
        if root.is_symlink() or not root.is_dir():
            raise WorkspaceHarnessError("team Session checkout is unavailable")
        return root

    @classmethod
    def _worker_session(cls, session: Mapping[str, object]) -> dict[str, object]:
        """Project only policy fields required by WorkspaceHarness into RPC.

        A persisted Session can contain host transcript/database references.
        Those values are not needed for a workspace operation and must not be
        copied into a container request.  The assigned checkout is projected
        to the worker mount by the normal path mapper below.
        """

        projected = {
            key: session[key]
            for key in _SESSION_KEYS
            if key in session and key != "workspaceRoots"
        }
        roots = session.get("workspaceRoots")
        if isinstance(roots, list):
            projected["workspaceRoots"] = list(roots)
        return projected

    @classmethod
    def _to_worker_path(cls, session: Mapping[str, object], value: str) -> str:
        raw = str(value or "")
        if not raw or not raw.startswith("/"):
            return raw
        # Already-projected worker paths can occur in an idempotent apply
        # replay. Keep them inside the only mount without resolving symlinks.
        candidate = Path(raw)
        if candidate == _WORKER_ROOT or candidate.is_relative_to(_WORKER_ROOT):
            if any(part in {".", ".."} for part in candidate.parts):
                raise WorkspaceHarnessError("worker workspace path is malformed")
            return candidate.as_posix()
        root = cls._workspace_root(session)
        try:
            candidate = candidate.expanduser().resolve(strict=False)
        except OSError as exc:
            raise WorkspaceHarnessError("team workspace path is unavailable") from exc
        if any(part in {".", ".."} for part in candidate.parts):
            raise WorkspaceHarnessError("team workspace path must be canonical")
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise WorkspaceHarnessError(
                "team workspace path is outside the assigned checkout"
            ) from exc
        return (_WORKER_ROOT / relative).as_posix()

    @classmethod
    def _to_host_path(cls, session: Mapping[str, object], value: str) -> str:
        raw = str(value or "")
        if not raw.startswith("/"):
            return raw
        candidate = Path(raw)
        if not (candidate == _WORKER_ROOT or candidate.is_relative_to(_WORKER_ROOT)):
            raise WorkspaceHarnessError(
                "worker returned a path outside the assigned checkout"
            )
        if any(part in {".", ".."} for part in candidate.parts):
            raise WorkspaceHarnessError("worker workspace path is malformed")
        relative = candidate.relative_to(_WORKER_ROOT)
        return (cls._workspace_root(session) / relative).as_posix()

    @classmethod
    def _map_paths(
        cls,
        session: Mapping[str, object],
        value: object,
        *,
        to_worker: bool,
        key: str = "",
    ) -> object:
        if isinstance(value, Mapping):
            return {
                str(child_key): cls._map_paths(
                    session,
                    child_value,
                    to_worker=to_worker,
                    key=str(child_key),
                )
                for child_key, child_value in value.items()
            }
        if isinstance(value, list):
            return [
                cls._map_paths(session, item, to_worker=to_worker, key=key)
                for item in value
            ]
        if key in _PATH_KEYS and isinstance(value, str):
            return (
                cls._to_worker_path(session, value)
                if to_worker
                else cls._to_host_path(session, value)
            )
        return value

    def _request(
        self,
        session: Mapping[str, object],
        tool: str,
        operation: str,
        args: Mapping[str, object] | None = None,
        *,
        base_state: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        session_id = self._session_id(session)
        request: dict[str, object] = {
            "schemaVersion": "rag-ime.team-workspace-request.v1",
            "spaceId": self.space_id,
            "sessionId": session_id,
            "session": self._worker_session(session),
            "tool": str(tool),
            "operation": str(operation),
            "args": dict(args or {}),
        }
        if base_state is not None:
            request["baseState"] = dict(base_state)
        worker_request = self._map_paths(session, request, to_worker=True)
        if not isinstance(worker_request, Mapping):  # pragma: no cover - helper invariant
            raise WorkspaceHarnessError("invalid worker workspace request")
        try:
            raw = self.coordinator.run_isolated(
                self._workspace_root(session),
                self.command,
                payload=dict(worker_request),
                timeout=self.timeout_seconds,
            )
        except WorkspaceHarnessError:
            raise
        except Exception as exc:
            raise WorkspaceHarnessError(
                f"team workspace worker could not be reached: {exc}"
            ) from exc
        if not isinstance(raw, Mapping):
            raise WorkspaceHarnessError("team workspace worker returned an invalid process receipt")
        exit_code = raw.get("exitCode")
        stdout = str(raw.get("stdout") or "")
        stderr = str(raw.get("stderr") or "")
        if len(stdout.encode("utf-8")) > _MAX_RPC_OUTPUT:
            raise WorkspaceHarnessError("team workspace worker output exceeded its limit")
        if exit_code != 0:
            detail = stderr.strip() or stdout.strip()
            raise WorkspaceHarnessError(
                f"team workspace worker exited with {exit_code}: {detail[:500]}"
            )
        try:
            result = json.loads(stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise WorkspaceHarnessError(
                "team workspace worker returned malformed JSON"
            ) from exc
        if not isinstance(result, Mapping):
            raise WorkspaceHarnessError("team workspace worker result must be an object")
        if result.get("ok") is False:
            error = result.get("error")
            if isinstance(error, Mapping):
                message = str(error.get("message") or error.get("error") or "worker rejected workspace operation")
            else:
                message = str(error or "worker rejected workspace operation")
            raise WorkspaceHarnessError(message[:500])
        value = result.get("result") if "result" in result else result
        if not isinstance(value, Mapping):
            raise WorkspaceHarnessError("team workspace worker returned no operation result")
        mapped = self._map_paths(session, value, to_worker=False)
        if not isinstance(mapped, Mapping):  # pragma: no cover - helper invariant
            raise WorkspaceHarnessError("invalid mapped workspace result")
        return dict(mapped)

    def list(self, session: Mapping[str, object], args: Mapping[str, object]) -> dict[str, object]:
        return self._request(session, "workspace_list", "list", args)

    def read(self, session: Mapping[str, object], args: Mapping[str, object]) -> dict[str, object]:
        return self._request(session, "workspace_read", "read", args)

    def search(self, session: Mapping[str, object], args: Mapping[str, object]) -> dict[str, object]:
        return self._request(session, "workspace_search", "search", args)

    def file_editability(self, session: Mapping[str, object], path: str) -> dict[str, object]:
        try:
            return self._request(
                session,
                "workspace_read",
                "file_editability",
                {"path": str(path)},
            )
        except WorkspaceHarnessError as exc:
            return {
                "editable": False,
                "maxBytes": 2 * 1024 * 1024,
                "reason": str(exc)[:500],
            }

    def save_file(self, session: Mapping[str, object], args: Mapping[str, object]) -> dict[str, object]:
        return self._request(session, "workspace_write", "save_file", args)

    def _prepare(
        self,
        session: Mapping[str, object],
        tool: str,
        operation: str,
        args: Mapping[str, object],
    ) -> RemotePreparedWorkspace:
        preview = self._request(session, tool, "prepare", args)
        if not isinstance(preview.get("actionPayload"), Mapping) or not isinstance(
            preview.get("baseState"), Mapping
        ):
            raise WorkspaceHarnessError(
                "team workspace worker preparation omitted actionPayload/baseState"
            )
        return RemotePreparedWorkspace(
            session_id=self._session_id(session),
            tool=tool,
            operation=operation,
            args=dict(args),
            preview_payload=preview,
            workspace_roots=(str(self._workspace_root(session)),),
        )

    def prepare_command(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
    ) -> RemotePreparedWorkspace:
        return self._prepare(session, "workspace_shell", "run", args)

    def prepare_background_command(
        self,
        _session: Mapping[str, object],
        _args: Mapping[str, object],
    ) -> RemotePreparedWorkspace:
        raise WorkspaceHarnessError(
            "worker-side durable workspace jobs are not attached"
        )

    @staticmethod
    def preview(prepared: RemotePreparedWorkspace) -> dict[str, object]:
        return prepared.preview

    def execute(self, prepared: RemotePreparedWorkspace) -> dict[str, object]:
        return self.execute_prepared(prepared)

    def execute_prepared(self, prepared: RemotePreparedWorkspace) -> dict[str, object]:
        return self._request(
            {
                "id": prepared.session_id,
                "workspaceRoots": list(prepared.workspace_roots),
            },
            prepared.tool,
            prepared.operation,
            prepared.action_payload,
            base_state=prepared.base_state,
        )

    def execute_cancellable(
        self,
        prepared: RemotePreparedWorkspace,
        cancelled: Any,
    ) -> dict[str, object]:
        if callable(cancelled) and cancelled():
            raise WorkspaceHarnessError(
                "workspace execution was cancelled before worker dispatch"
            )
        return self.execute_prepared(prepared)

    def _apply(
        self,
        session: Mapping[str, object],
        tool: str,
        args: Mapping[str, object],
        base_state: Mapping[str, object],
    ) -> dict[str, object]:
        return self._request(session, tool, "apply", args, base_state=base_state)

    def prepare_patch(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
    ) -> RemotePreparedWorkspace:
        return self._prepare(session, "workspace_patch", "apply", args)

    def patch_preview(self, prepared: RemotePreparedWorkspace) -> dict[str, object]:
        return prepared.preview

    def apply_patch(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
        base_state: Mapping[str, object],
    ) -> dict[str, object]:
        return self._apply(session, "workspace_patch", args, base_state)

    def prepare_edit(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
    ) -> RemotePreparedWorkspace:
        return self._prepare(session, "workspace_edit", "apply", args)

    def edit_preview(self, prepared: RemotePreparedWorkspace) -> dict[str, object]:
        return prepared.preview

    def apply_edit(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
        base_state: Mapping[str, object],
    ) -> dict[str, object]:
        return self._apply(session, "workspace_edit", args, base_state)

    def prepare_write(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
        *,
        include_review_diff: bool = True,
    ) -> RemotePreparedWorkspace:
        payload = dict(args)
        if not include_review_diff:
            payload["includeReviewDiff"] = False
        return self._prepare(session, "workspace_write", "apply", payload)

    def write_preview(self, prepared: RemotePreparedWorkspace) -> dict[str, object]:
        return prepared.preview

    def apply_write(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
        base_state: Mapping[str, object],
    ) -> dict[str, object]:
        return self._apply(session, "workspace_write", args, base_state)

    def lsp_status(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
    ) -> dict[str, object]:
        return self._request(session, "workspace_lsp", "status", args)

    def lsp_read(
        self,
        session: Mapping[str, object],
        operation: str,
        args: Mapping[str, object],
    ) -> dict[str, object]:
        return self._request(session, "workspace_lsp", operation, args)

    def prepare_lsp_mutation(
        self,
        session: Mapping[str, object],
        operation: str,
        args: Mapping[str, object],
    ) -> RemotePreparedWorkspace:
        return self._prepare(session, "workspace_lsp", operation, args)

    def lsp_mutation_preview(self, prepared: RemotePreparedWorkspace) -> dict[str, object]:
        return prepared.preview

    def apply_lsp_mutation(
        self,
        session: Mapping[str, object],
        operation: str,
        args: Mapping[str, object],
        base_state: Mapping[str, object],
    ) -> dict[str, object]:
        return self._request(
            session,
            "workspace_lsp",
            operation,
            args,
            base_state=base_state,
        )

    def spawn_background(self, *_args: object, **_kwargs: object) -> object:
        raise WorkspaceHarnessError(
            "worker-side durable workspace jobs are not attached"
        )

    @staticmethod
    def terminate_background(_launched: object) -> None:
        raise WorkspaceHarnessError(
            "worker-side durable workspace jobs are not attached"
        )

    @staticmethod
    def redact_output(text: str) -> str:
        return WorkspaceHarness.redact_output(text)
