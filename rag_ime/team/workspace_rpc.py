"""One-shot worker entrypoint for team workspace operations.

The host-side adapter sends one JSON request to this command through the
existing ``TeamExecutionCoordinator.run_isolated`` pipe.  The command runs
inside the same Linux workspace mount as Pi, delegates to
``TeamWorkspaceExecutor``/``WorkspaceHarness``, emits exactly one JSON result,
and exits.  It has no server database or host path access.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sys
import uuid

from .execution import ExecutionSpec
from .workspace import TeamWorkspaceExecutor


def _worker_spec() -> ExecutionSpec:
    scope = Path("/tmp/paw-workspace-rpc-" + uuid.uuid4().hex)
    return ExecutionSpec(
        attempt_id="workspace-rpc",
        session_id="workspace-rpc",
        scope_root=scope,
        workspace_root=Path("/workspace"),
        agent_dir=scope / "agent",
        session_dir=scope / "sessions",
        logs_dir=scope / "logs",
        home_dir=scope / "home",
        tmp_dir=scope / "tmp",
        container_id="workspace-rpc",
        runtime_image="workspace-rpc",
        runtime_command=("/usr/local/bin/paw-workspace-rpc",),
        container_workspace="/workspace",
    )


def _read_request() -> Mapping[str, object]:
    line = sys.stdin.buffer.readline(2 * 1024 * 1024 + 1)
    if not line:
        raise ValueError("workspace worker request is empty")
    if len(line) > 2 * 1024 * 1024:
        raise ValueError("workspace worker request is too large")
    value = json.loads(line.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("workspace worker request must be an object")
    return value


def _run(request: Mapping[str, object]) -> dict[str, object]:
    if request.get("schemaVersion") != "rag-ime.team-workspace-request.v1":
        raise ValueError("unsupported workspace worker request schema")
    session = request.get("session")
    if not isinstance(session, Mapping):
        raise ValueError("workspace worker request has no Session projection")
    session_id = str(session.get("id") or "").strip()
    if not session_id or session_id != str(request.get("sessionId") or "").strip():
        raise ValueError("workspace worker request Session identity is inconsistent")
    args = request.get("args")
    if not isinstance(args, Mapping):
        raise ValueError("workspace worker request args must be an object")
    base_state = request.get("baseState")
    if not isinstance(base_state, Mapping):
        base_state = None
    return TeamWorkspaceExecutor().execute_workspace(
        _worker_spec(),
        session,
        str(request.get("tool") or ""),
        args,
        operation=str(request.get("operation") or "") or None,
        base_state=base_state,
    )


def main() -> int:
    try:
        result = _run(_read_request())
    except Exception as exc:
        result = {
            "ok": False,
            "error": {
                "code": exc.__class__.__name__,
                "message": str(exc)[:500],
            },
        }
    else:
        result = {"ok": True, "result": result}
    sys.stdout.write(
        json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
        + "\n"
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised inside worker image
    raise SystemExit(main())
