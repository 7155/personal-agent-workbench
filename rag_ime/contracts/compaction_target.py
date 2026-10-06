"""Exact native standalone-compaction identity shared across control boundaries."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, TypedDict


class CompactionTarget(TypedDict):
    kind: Literal["compaction"]
    runtimeSessionId: str
    taskIds: list[str]


def validate_compaction_target(value: object) -> CompactionTarget:
    """Reject ambiguous identities; never repair, sort, or infer caller targets."""
    if not isinstance(value, Mapping) or set(value) != {"kind", "runtimeSessionId", "taskIds"}:
        raise ValueError("compactionTarget requires an exact native task identity")
    runtime_id = value.get("runtimeSessionId")
    task_ids = value.get("taskIds")
    if (value.get("kind") != "compaction" or not isinstance(runtime_id, str)
        or not runtime_id or runtime_id != runtime_id.strip()
        or not isinstance(task_ids, list) or not task_ids
        or any(not isinstance(task_id, str) or not re.fullmatch(r"durable:task:[1-9][0-9]*", task_id)
               or len(task_id) > 29 or int(task_id[13:]) > 9_007_199_254_740_991 for task_id in task_ids)
        or task_ids != sorted(set(task_ids))):
        raise ValueError("compactionTarget requires canonical unique native task IDs")
    return {"kind": "compaction", "runtimeSessionId": runtime_id, "taskIds": list(task_ids)}


def compaction_control_target(payload: Mapping[str, object]) -> CompactionTarget:
    if set(payload) != {"compactionTarget"}:
        raise ValueError("compactionTarget cannot be mixed with turn or cancellation identities")
    return validate_compaction_target(payload["compactionTarget"])
