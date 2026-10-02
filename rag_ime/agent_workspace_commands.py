"""Physical ownership of foreground Gateway commands through their call scope."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import time
import uuid

from .agent_workspace import PreparedWorkspaceCommand, WorkspaceHarness, WorkspaceHarnessError


@dataclass
class _CommandCall:
    session_id: str
    cancelled: Callable[[], bool]
    operation_id: str = field(default_factory=lambda: 'workspace-command:' + str(uuid.uuid4()))
    stop: threading.Event = field(default_factory=threading.Event)
    drained: threading.Event = field(default_factory=threading.Event)

    def stopping(self) -> bool:
        if self.stop.is_set():
            return True
        try:
            return self.cancelled()
        except Exception:
            # Losing the exact owner must stop this command, never leave an
            # untracked process behind when a cancellation projection fails.
            self.stop.set()
            return True


class WorkspaceCommandOwner:
    def __init__(self, harness: WorkspaceHarness) -> None:
        self.harness = harness
        self._lock = threading.Lock()
        self._calls: dict[str, _CommandCall] = {}
        self._thread = threading.local()

    @contextmanager
    def call_scope(self, session_id: str, cancelled: Callable[[], bool] = lambda: False) -> Iterator[_CommandCall]:
        previous = getattr(self._thread, 'call', None)
        if isinstance(previous, _CommandCall) and previous.session_id == session_id:
            yield previous
            return
        call = _CommandCall(session_id, cancelled)
        with self._lock:
            self._calls[call.operation_id] = call
        self._thread.call = call
        try:
            yield call
        finally:
            self._thread.call = previous
            with self._lock:
                self._calls.pop(call.operation_id, None)
                call.drained.set()

    def execute(self, session_id: str, prepared: PreparedWorkspaceCommand) -> dict[str, object]:
        with self.call_scope(session_id) as call:
            if call.stopping():
                raise WorkspaceHarnessError('workspace command cancelled before execution')
            return self.harness.execute_cancellable(prepared, call.stopping)

    def request_cancel(self, session_id: str) -> Callable[[], dict[str, object]]:
        # Capture only this request's calls. A later Session turn receives a
        # distinct lease, and waiting must never cancel its process generation.
        with self._lock:
            calls = tuple(call for call in self._calls.values() if call.session_id == session_id)
            for call in calls:
                call.stop.set()

        def wait() -> dict[str, object]:
            deadline = time.monotonic() + 2
            for call in calls:
                call.drained.wait(max(0, deadline - time.monotonic()))
            pending = [call.operation_id for call in calls if not call.drained.is_set()]
            return {'schemaVersion': 'rag-ime.workspace-command-cancellation.v1', 'sessionId': session_id,
                    'operationIds': [call.operation_id for call in calls], 'pendingOperationIds': pending,
                    'drained': not pending}

        return wait
