"""Deterministic graph facts. A parent relationship is not a dependency edge."""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .types import Edge, ExecutionFact, GraphError, Task, unique_by_id


@dataclass(frozen=True)
class Frontier:
    ready: tuple[str, ...]
    review: tuple[str, ...]
    running: tuple[str, ...]
    blocked: tuple[tuple[str, tuple[str, ...]], ...]


class TaskGraph:
    def __init__(self, tasks: Sequence[Task], edges: Sequence[Edge], *, root_id: str, room_id: str):
        if len(tasks) > 2000 or len(edges) > 10000:
            raise GraphError("graph exceeds bounded kernel limits")
        self.tasks = unique_by_id(tasks)
        if any(t.root_id != root_id or t.room_id != room_id for t in tasks):
            raise GraphError("cross-root/cross-room task")
        if len(set(edges)) != len(edges):
            raise GraphError("duplicate edge")
        self.edges = tuple(sorted(edges))
        self.parents: dict[str, list[str]] = {key: [] for key in self.tasks}
        self.requires: dict[str, set[str]] = {key: set() for key in self.tasks}
        self.downstream: dict[str, set[str]] = {key: set() for key in self.tasks}
        parent_edges = []
        for task in tasks:
            if task.parent_id:
                if task.parent_id not in self.tasks:
                    raise GraphError("missing parent: graph is not complete")
                self.parents[task.parent_id].append(task.id)
                parent_edges.append((task.parent_id, task.id))
        hard_edges = []
        for edge in edges:
            if edge.prerequisite not in self.tasks or edge.dependent not in self.tasks:
                raise GraphError("missing edge endpoint")
            if edge.kind == "requires":
                hard_edges.append((edge.prerequisite, edge.dependent))
                self.requires[edge.dependent].add(edge.prerequisite)
                self.downstream[edge.prerequisite].add(edge.dependent)
        self._acyclic(parent_edges, "parent")
        self._acyclic(hard_edges, "hard dependency")
        # A parent aggregates its children. A child waiting for that parent's
        # completion creates a real execution deadlock even if each graph alone
        # is acyclic. Child -> parent is an implicit completion requirement only.
        self._acyclic(hard_edges + [(child, parent) for parent, child in parent_edges], "execution")

    def _acyclic(self, edges: Sequence[tuple[str, str]], name: str) -> None:
        outgoing = {key: set() for key in self.tasks}
        indegree = dict.fromkeys(self.tasks, 0)
        for start, end in edges:
            if end not in outgoing[start]:
                outgoing[start].add(end)
                indegree[end] += 1
        queue = deque(sorted(key for key, count in indegree.items() if count == 0))
        count = 0
        while queue:
            node = queue.popleft()
            count += 1
            for end in sorted(outgoing[node]):
                indegree[end] -= 1
                if indegree[end] == 0:
                    queue.append(end)
        if count != len(self.tasks):
            raise GraphError(f"cycle in {name} graph")

    def impacted(self, changed: Sequence[str]) -> tuple[str, ...]:
        if any(key not in self.tasks for key in changed):
            raise GraphError("unknown changed task")
        visited = set(changed)
        queue = deque(sorted(visited))
        while queue:
            node = queue.popleft()
            parent = self.tasks[node].parent_id
            related = set(self.downstream[node])
            if parent:
                related.add(parent)
            for candidate in sorted(related - visited):
                visited.add(candidate)
                queue.append(candidate)
        return tuple(sorted(visited))

    def frontier(self, executions: Mapping[str, ExecutionFact]) -> Frontier:
        if any(key != fact.task_id or key not in self.tasks for key, fact in executions.items()):
            raise GraphError("execution facts do not match this graph")
        ready, review, running, blocked = [], [], [], []
        for key, task in sorted(self.tasks.items()):
            if task.state in {"done", "cancelled", "failed"}:
                continue
            reasons = []
            fact = executions.get(key)
            if fact is None or fact.status == "unknown" or not fact.matches(task):
                reasons.append("execution_unknown")
            elif fact.status == "running":
                running.append(key)
                continue
            if task.state == "queued":
                reasons.append("assignment_not_accepted")
            elif task.state == "blocked":
                reasons.append("work_blocked")
            for dependency in sorted(self.requires[key]):
                if self.tasks[dependency].state != "done":
                    reasons.append("dependency:" + dependency)
            for child in sorted(self.parents[key]):
                if self.tasks[child].state != "done":
                    reasons.append("child:" + child)
            if reasons:
                blocked.append((key, tuple(reasons)))
            elif task.state == "review":
                review.append(key)
            elif task.state == "active":
                ready.append(key)
        return Frontier(tuple(ready), tuple(review), tuple(running), tuple(blocked))
