"""Construct existing Room stores and Session services from explicit dependencies.

These builders do not start a Runtime, execute recovery or own business rules.
AgentService retains their objects and the established process shutdown order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .agent_command_receipts import AgentCommandReceiptStore
from .agent_configuration import AgentConfigurationStore
from .agent_delegation import AgentDelegationCoordinator
from .agent_events import AgentEventHub
from .agent_media import AgentMediaStore
from rag_ime.rooms.partner_dispatch_store import AgentRoomPartnerDispatchStore
from rag_ime.rooms.start_gate import AgentRoomStartGateStore
from rag_ime.rooms.work import AgentRoomWorkStore
from rag_ime.rooms.store import AgentRoomStore
from .agent_runtime_driver import AgentRuntimeDriver, RuntimeDriverFactory
from .agent_session_application import AgentSessionApplicationService
from .agent_session_branching import AgentSessionBranchingService
from .agent_session_policy import AgentSessionPolicyService
from .agent_sessions import AgentSessionStore
from .agent_task_context import AgentTaskContextResolver


class MemoryMaintenanceProbe(Protocol):
    def __call__(self, session_id: str, *, trigger: str) -> Mapping[str, object]: ...


class RewriteCheckpoint(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        message: str,
        checkpoint_text: str,
        attachment_ids: list[str],
        client_message_id: str,
        context_source: str,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class RoomStores:
    rooms: AgentRoomStore
    start_gates: AgentRoomStartGateStore
    work: AgentRoomWorkStore
    partner_dispatches: AgentRoomPartnerDispatchStore


def build_room_stores(
    db_path: str | Path,
    *,
    session_root: Path,
    participant_identity_provider: Callable[[str], str] | None = None,
) -> RoomStores:
    rooms = AgentRoomStore(
        db_path,
        room_dir=session_root.expanduser().resolve(strict=False).parent / "rooms",
        persistent_reads=True,
        participant_identity_provider=participant_identity_provider,
    )
    try:
        rooms.initialize()
        start_gates = AgentRoomStartGateStore(db_path)
        start_gates.initialize()
        work = AgentRoomWorkStore(db_path)
        work.initialize()
        partner_dispatches = AgentRoomPartnerDispatchStore(db_path)
        partner_dispatches.initialize()
    except BaseException:
        # This is the only persistent connection owned by this construction
        # group. Failed construction cannot transfer its cleanup to the caller.
        rooms.close()
        raise
    return RoomStores(rooms, start_gates, work, partner_dispatches)


@dataclass(frozen=True)
class SessionApplications:
    application: AgentSessionApplicationService
    policy: AgentSessionPolicyService
    branching: AgentSessionBranchingService
    task_context: AgentTaskContextResolver


def build_session_applications(
    *,
    sessions: AgentSessionStore,
    runtime_provider: Callable[[], AgentRuntimeDriver],
    runtime_factory: RuntimeDriverFactory,
    configuration_store: AgentConfigurationStore,
    rooms: AgentRoomStore,
    delegation: AgentDelegationCoordinator,
    media: AgentMediaStore,
    events: AgentEventHub,
    command_receipts: AgentCommandReceiptStore,
    runtime_status: Callable[[], Mapping[str, object]],
    pending_memory_bootstrap: Callable[[Mapping[str, object]], Mapping[str, object]],
    probe_memory_maintenance: MemoryMaintenanceProbe,
    prompt_with_checkpoint: RewriteCheckpoint,
    cancel_pending_approvals: Callable[..., Mapping[str, object]],
    recent_recall_messages: Callable[[str], list[dict[str, object]]],
    checkpoint_runtime_compaction: Callable[
        [str, Mapping[str, object], str], Mapping[str, object]
    ],
    refresh_session_context: Callable[[Mapping[str, object]], Mapping[str, object]],
    public_error: Callable[[BaseException], str],
) -> SessionApplications:
    application = AgentSessionApplicationService(
        sessions=sessions,
        runtime_provider=runtime_provider,
        runtime_factory=runtime_factory,
        configuration_store=configuration_store,
        rooms=rooms,
        delegation=delegation,
        media=media,
        events=events,
        runtime_status=runtime_status,
        pending_memory_bootstrap=pending_memory_bootstrap,
        probe_memory_maintenance=probe_memory_maintenance,
        cancel_pending_approvals=cancel_pending_approvals,
        recent_recall_messages=recent_recall_messages,
        checkpoint_runtime_compaction=checkpoint_runtime_compaction,
        refresh_session_context=refresh_session_context,
        public_error=public_error,
    )
    policy = AgentSessionPolicyService(
        sessions=sessions,
        runtime_provider=runtime_provider,
        rooms=rooms,
        events=events,
        runtime_status=runtime_status,
        probe_memory_maintenance=probe_memory_maintenance,
    )
    branching = AgentSessionBranchingService(
        sessions=sessions,
        runtime_provider=runtime_provider,
        runtime_factory=runtime_factory,
        rooms=rooms,
        delegation=delegation,
        media=media,
        events=events,
        command_receipts=command_receipts,
        prompt_with_checkpoint=prompt_with_checkpoint,
    )
    return SessionApplications(
        application,
        policy,
        branching,
        AgentTaskContextResolver(delegation=delegation, rooms=rooms),
    )
