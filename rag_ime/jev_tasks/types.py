"""Small, immutable contracts. WorkItem state is always read from its owner."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal


class GraphError(ValueError):
    """Invalid input, never a successful empty graph."""


class GraphConflict(GraphError):
    """The observed task, topology, requirement or assignment changed."""

    http_status = 409


class DecisionUnavailable(RuntimeError):
    """A decision was unavailable; this does not grant permission to execute."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def text(value: object, label: str, maximum: int = 320, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum:
        raise GraphError(f"invalid {label}")
    if "\x00" in value:
        raise GraphError(f"NUL in {label}")
    return value


def integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise GraphError(f"invalid {label}")
    return value


def probability(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphError(f"invalid {label}")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise GraphError(f"invalid {label}")
    return result


def string_tuple(value: object, label: str, maximum: int = 2048) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise GraphError(f"invalid {label}")
    return tuple(text(v, label, 8000) for v in value)


STATES = frozenset({"queued", "active", "review", "blocked", "done", "failed", "cancelled"})


@dataclass(frozen=True)
class Task:
    id: str
    root_id: str
    room_id: str
    state: str
    revision: int
    owner_id: str
    accountable_id: str
    assignment_key: str
    accepted_turn_id: str
    objective: str
    expected_output: str
    acceptance: tuple[str, ...]
    parent_id: str = ""
    artifacts: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    result: str = ""

    @classmethod
    def from_payload(cls, item: Mapping[str, object]) -> Task:
        state = text(item.get("state"), "task state", 40)
        if state not in STATES:
            raise GraphError("unknown WorkItem state")
        return cls(
            id=text(item.get("id"), "task id"),
            root_id=text(item.get("rootTurnId"), "root id"),
            room_id=text(item.get("roomId"), "room id"), state=state,
            revision=integer(item.get("revision"), "task revision"),
            owner_id=text(item.get("currentOwnerParticipantId"), "owner id"),
            accountable_id=text(item.get("accountableParticipantId"), "accountable id"),
            assignment_key=text(item.get("assignmentKey"), "assignment key", 1000),
            accepted_turn_id=text(item.get("acceptedTurnId", ""), "accepted turn", empty=True),
            objective=text(item.get("objective"), "objective", 8000),
            expected_output=text(item.get("expectedOutput"), "expected output", 8000),
            acceptance=string_tuple(item.get("acceptanceCriteria"), "acceptance", 32),
            parent_id=text(item.get("parentWorkId", ""), "parent id", empty=True),
            artifacts=string_tuple(item.get("artifactRefs", []), "artifacts", 64),
            evidence=string_tuple(item.get("evidenceRefs", []), "evidence", 64),
            result=text(item.get("resultSummary", ""), "result", 16000, empty=True),
        )

    @property
    def binding(self) -> tuple[str, int, str, str, str]:
        return self.id, self.revision, self.owner_id, self.assignment_key, self.accepted_turn_id


@dataclass(frozen=True, order=True)
class Edge:
    prerequisite: str
    dependent: str
    kind: Literal["requires", "context"] = "requires"

    def __post_init__(self) -> None:
        text(self.prerequisite, "prerequisite")
        text(self.dependent, "dependent")
        if self.kind not in {"requires", "context"}:
            raise GraphError("unsupported edge kind")
        if self.prerequisite == self.dependent:
            raise GraphError("self edge")


@dataclass(frozen=True)
class ExecutionFact:
    """Runtime-owner observation, not an assertion made by an LLM or the UI.

    `idle` means verified idle for this task, not merely absence from a short list.
    `drained` means no live execution remains; it does not mean acceptance.
    """
    task_id: str
    status: Literal["idle", "running", "drained", "unknown"]
    dispatch_id: str = ""
    session_id: str = ""
    task_revision: int | None = None
    owner_id: str = ""
    assignment_key: str = ""
    accepted_turn_id: str = ""
    proof_ref: str = ""
    effects_reconciled: bool = False

    def matches(self, task: Task) -> bool:
        return (
            self.task_id == task.id and self.task_revision == task.revision
            and self.owner_id == task.owner_id and self.assignment_key == task.assignment_key
            and self.accepted_turn_id == task.accepted_turn_id
        )

    def __post_init__(self) -> None:
        text(self.task_id, "execution task")
        if self.status not in {"idle", "running", "drained", "unknown"}:
            raise GraphError("unknown execution status")
        if not isinstance(self.effects_reconciled, bool):
            raise GraphError("invalid effect reconciliation flag")
        if self.task_revision is not None:
            integer(self.task_revision, "execution task revision")
        for name in ("dispatch_id", "session_id", "owner_id", "assignment_key", "accepted_turn_id", "proof_ref"):
            text(getattr(self, name), name, 1000, empty=True)
        if self.status != "unknown" and (not self.proof_ref or self.task_revision is None
                                         or not self.owner_id or not self.assignment_key):
            raise GraphError("known execution fact requires an exact owner proof")
        if self.status in {"running", "drained"} and (not self.dispatch_id or not self.session_id or not self.accepted_turn_id):
            raise GraphError("attempt observation requires dispatch/session identity")


@dataclass(frozen=True)
class Executor:
    participant_id: str
    session_id: str
    available: bool
    capabilities: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        text(self.participant_id, "participant")
        text(self.session_id, "session")
        if not isinstance(self.available, bool):
            raise GraphError("availability must be authoritative boolean")
        string_tuple(self.capabilities, "capabilities", 64)
        text(self.description, "executor description", 4000, empty=True)


@dataclass(frozen=True)
class Candidate:
    """Opaque choice mapped to a server-constructed action; never model arguments."""
    id: str
    operation: str
    task_id: str
    description: str
    arguments_json: str

    @classmethod
    def make(cls, operation: str, task_id: str, description: str,
             arguments: Mapping[str, object]) -> Candidate:
        text(operation, "operation", 80)
        text(task_id, "candidate task", empty=True)
        text(description, "candidate description", 16000)
        packed = canonical(dict(arguments))
        if len(packed.encode()) > 65536:
            raise GraphError("candidate too large")
        identity = "action_" + digest([operation, task_id, packed])[:32]
        return cls(identity, operation, task_id, description, packed)

    def arguments(self) -> dict[str, object]:
        result = json.loads(self.arguments_json)
        if not isinstance(result, dict):
            raise GraphError("invalid candidate arguments")
        return result


def unique_by_id(items: Sequence[Task]) -> dict[str, Task]:
    result = {item.id: item for item in items}
    if len(result) != len(items):
        raise GraphError("duplicate task id")
    return result
