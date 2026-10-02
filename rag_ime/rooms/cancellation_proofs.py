from __future__ import annotations

from collections.abc import Mapping, Sequence


_SESSION_ABORT_OPERATION_KINDS: dict[str, frozenset[str]] = {
    "provider": frozenset({"provider"}),
    "tool": frozenset({"tool"}),
    "shell": frozenset({"bash_process"}),
    "retry": frozenset({"retry_sleep"}),
    "compaction": frozenset({"manual_compaction", "auto_compaction"}),
    "branch_summary": frozenset({"branch_summary"}),
    "timer": frozenset({"continuation_timer"}),
}
_ROOT_ABORT_SESSION_SURFACES = (
    "provider",
    "tool",
    "shell",
    "retry",
    "compaction",
    "branch_summary",
    "timer",
    "continuation",
    "session",
)

def _root_resource_surface(
    surface: str,
    state: str,
    target_ids: Sequence[str],
    errors: Sequence[str],
) -> dict[str, object]:
    normalized_state = state if state in {"terminated", "requested", "unknown"} else "unknown"
    return {
        "schemaVersion": "rag-ime.root-cancellation-surface.v1",
        "surface": surface,
        "state": normalized_state,
        "targetIds": _unique_nonempty_strings(target_ids),
        "errors": _unique_nonempty_strings(errors),
    }

def _unique_nonempty_strings(values: Sequence[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result

def _merge_root_surface(
    left: Mapping[str, object] | None,
    right: Mapping[str, object] | None,
) -> dict[str, object]:
    if left is None:
        return dict(right or {})
    if right is None:
        return dict(left)
    priority = {"terminated": 0, "requested": 1, "unknown": 2}
    left_state = str(left.get("state") or "unknown")
    right_state = str(right.get("state") or "unknown")
    state = (
        left_state
        if priority.get(left_state, 2) >= priority.get(right_state, 2)
        else right_state
    )
    return _root_resource_surface(
        str(left.get("surface") or right.get("surface") or ""),
        state,
        [
            *(
                str(item)
                for item in left.get("targetIds", [])
                if str(item or "").strip()
            ),
            *(
                str(item)
                for item in right.get("targetIds", [])
                if str(item or "").strip()
            ),
        ],
        [
            *(
                str(item)
                for item in left.get("errors", [])
                if str(item or "").strip()
            ),
            *(
                str(item)
                for item in right.get("errors", [])
                if str(item or "").strip()
            ),
        ],
    )

def _session_abort_surfaces(
    receipt: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    session_id = str(receipt.get("sessionId") or "")
    runtime = receipt.get("runtimeReceipt")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    lifecycle = runtime.get("lifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
    valid = (
        runtime.get("schemaVersion") == "rag-ime.pi-session-abort-receipt.v1"
        and lifecycle.get("schemaVersion") == "pi.agent-abort-receipt.v1"
    )
    if not valid:
        return {
            surface: _root_resource_surface(
                surface,
                "unknown",
                [session_id],
                ["runtime did not return typed cancellation proof"],
            )
            for surface in _ROOT_ABORT_SESSION_SURFACES
        }

    operations = [
        operation
        for operation in lifecycle.get("operations", [])
        if isinstance(operation, Mapping)
    ]
    pending = {
        str(operation.get("operationId") or "")
        for operation in lifecycle.get("pendingOperations", [])
        if isinstance(operation, Mapping)
    }
    failed = {
        str(operation_id or "")
        for operation_id in lifecycle.get("failedOperationIds", [])
    }
    result: dict[str, dict[str, object]] = {}
    for surface, kinds in _SESSION_ABORT_OPERATION_KINDS.items():
        relevant = [
            str(operation.get("operationId") or "")
            for operation in operations
            if str(operation.get("kind") or "") in kinds
            and str(operation.get("operationId") or "")
        ]
        state = (
            "unknown"
            if any(operation_id in failed for operation_id in relevant)
            else "requested"
            if any(operation_id in pending for operation_id in relevant)
            else "terminated"
        )
        result[surface] = _root_resource_surface(
            surface,
            state,
            [session_id, *relevant],
            [],
        )

    continuation_ids = [
        str(value or "")
        for value in lifecycle.get("cancelledContinuationIds", [])
        if str(value or "")
    ]
    result["continuation"] = _root_resource_surface(
        "continuation",
        "terminated",
        [session_id, *continuation_ids],
        [],
    )
    session_state = (
        "terminated"
        if lifecycle.get("drained") is True and lifecycle.get("idle") is True
        else "requested"
    )
    result["session"] = _root_resource_surface(
        "session",
        session_state,
        [session_id, str(runtime.get("turnId") or "")],
        [],
    )
    commands = receipt.get("workspaceCommands")
    if isinstance(commands, Mapping):
        command_surface = _root_resource_surface("shell", "terminated" if commands.get("drained") is True else "requested",
            [str(item) for item in commands.get("operationIds", [])],
            [str(item) for item in commands.get("pendingOperationIds", [])])
        result["shell"] = _merge_root_surface(result.get("shell"), command_surface)
    return result

def _aggregate_session_abort_surfaces(
    receipts: Sequence[Mapping[str, object]],
    *,
    expected_session_ids: set[str],
    errors: Sequence[str],
) -> dict[str, dict[str, object]]:
    aggregate = {
        surface: _root_resource_surface(surface, "terminated", [], [])
        for surface in _ROOT_ABORT_SESSION_SURFACES
    }
    received: set[str] = set()
    for receipt in receipts:
        session_id = str(receipt.get("sessionId") or "")
        if session_id:
            received.add(session_id)
        for surface, proof in _session_abort_surfaces(receipt).items():
            aggregate[surface] = _merge_root_surface(aggregate.get(surface), proof)
    missing = sorted(expected_session_ids - received)
    if missing or errors:
        failure_targets = [*missing, *errors]
        for surface in _ROOT_ABORT_SESSION_SURFACES:
            aggregate[surface] = _merge_root_surface(
                aggregate.get(surface),
                _root_resource_surface(
                    surface,
                    "unknown",
                    failure_targets,
                    list(errors),
                ),
            )
    return aggregate
