from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path

from rag_ime.agent_events import AgentEventHub
from rag_ime.agent_execution_policy import native_mcp_execution_allowed
from rag_ime.agent_plugin_usage import AgentPluginUsageStore
from rag_ime.agent_prompt_settings import normalize_prompt_settings
from rag_ime.agent_runtime_failure import classify_runtime_failure
from rag_ime.agent_tool_block_bridge import AgentToolBlockBuffer
from rag_ime.agent_tool_ids import MEMORY_CURATION_TOOL_PROFILE
from rag_ime.agent_runtime_driver import (
    AgentRuntimeError,
    CompactionObserver,
    SkillAllowlistProvider,
)
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.pi.config import (
    CODEMODE_MODES,
    PiRuntimeConfig,
    normalize_codemode_mode,
)
from rag_ime.pi.host_client import PiRuntimeHostClient
from rag_ime.pi.protocols import PI_HOST_PROTOCOL_VERSION
from rag_ime.pi.ui_requests import grouped_question_request, public_ui_request, resolve_ui_response
from rag_ime.pi.transcript_io import (
    read_recent_transcript_tail, transcript_boundary_sha256,
    DURABLE_TRANSCRIPT_MAX_LINE_BYTES,
)
from rag_ime.pi.event_projection import (
    codemode_capability,
    failed_settlement_receipt, runtime_primitive_capabilities,
    tool_event_payload, text_delta_payload,
)
from rag_ime.pi.values import PiRuntimeError
from rag_ime.pi.public import (
    pi_message_payload,
    APPROVAL_TITLE_PREFIX,
    GROUPED_QUESTIONS_TITLE_PREFIX,
    REVIEW_TITLE_PREFIX,
    last_assistant_error,
    last_assistant_preview,
    pi_message_id,
    pi_message_completes_public_turn,
    pi_message_continues_public_turn,
    pi_message_is_public,
    provider_request_receipt,
    provider_retry_status,
    public_fork_candidate_text,
    public_pi_model,
    public_reasoning_summaries,
    public_usage,
    public_usage_evidence,
    redact_mapping,
    visible_message_text,
)
from rag_ime.pi.transcript import (
    durable_branch_messages,
    durable_tool_history_events,
    recent_messages_from_proven_tail,
    recent_public_message_window,
    recent_tool_history_events,
    latest_terminal_descendant_leaf,
    terminal_branch_anchor,
    unambiguous_descendant_leaf,
    history_entry_timestamps,
    history_entry_ordinals,
    durable_public_assistant_counts,
    assistant_projection_fingerprint,
    history_message_fingerprint,
    DURABLE_TURN_ID_KEY,
)
from rag_ime.pi.values import (
    PiRuntimeCommandAcceptanceUnknown,
    PiRuntimeCommandRejected,
    PiRuntimeSettlementLookupTimeout,
    PiRuntimeTurnConflict,
    effective_thinking_level,
    as_integer,
    path_is_within,
    as_mapping,
    message_delivery,
    model_reference_part,
    public_message_queue,
    redact_runtime_text,
)
from rag_ime.room_runtime_host_kill_gate import RuntimeHostKillGate
from rag_ime.contracts.compaction_target import CompactionTarget, validate_compaction_target


__all__ = [
    "PiRuntimeHostManager",
]



_MODEL_CATALOG_CACHE_SECONDS = 1_800.0
_PROMPT_TIMEOUT_SECONDS = 60.0 * 60.0
_DURABLE_TRANSCRIPT_MAX_BYTES = 64 * 1024 * 1024
_DURABLE_TRANSCRIPT_MAX_LINES = 200_000
_SESSION_RESOURCE_SNAPSHOT_SCHEMA = "rag-ime.pi-session-resource-snapshot.v1"
_DURABLE_ENGINE_CAPABILITIES = frozenset({"gatewayTools", "compaction", "resume", "exactAbort",
    "nativeMcp", "codemode", "managedPlugins", "conversationFork", "conversationRewrite", "commandCatalog", "images"})
_DURABLE_UNSUPPORTED_CAPABILITIES = frozenset({"nativeMcp", "codemode", "managedPlugins",
    "conversationFork", "conversationRewrite", "commandCatalog", "images"})


@dataclass
class _ClassificationCall:
    client: PiRuntimeHostClient
    dispatch_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    dispatched: bool = False
    cancel_requested: bool = False
    on_settled: Callable[[], None] | None = None


def _session_resource_snapshot(
    skill_allowlist: list[str] | None,
) -> dict[str, object]:
    return {
        "schemaVersion": _SESSION_RESOURCE_SNAPSHOT_SCHEMA,
        "skillPolicy": "all_enabled" if skill_allowlist is None else "allowlist",
        "skillRefs": list(skill_allowlist or []),
    }


def _bound_session_resource_snapshot(
    binding: Mapping[str, object] | None,
) -> dict[str, object] | None:
    metadata = as_mapping((binding or {}).get("metadata"))
    snapshot = as_mapping(metadata.get("resourceSnapshot"))
    policy = str(snapshot.get("skillPolicy") or "")
    refs = snapshot.get("skillRefs")
    if (
        snapshot.get("schemaVersion") != _SESSION_RESOURCE_SNAPSHOT_SCHEMA
        or policy not in {"allowlist", "all_enabled"}
        or not isinstance(refs, list)
        or any(not isinstance(ref, str) or not ref for ref in refs)
        or (policy == "all_enabled" and refs)
    ):
        return None
    result: dict[str, object] = {
        "schemaVersion": _SESSION_RESOURCE_SNAPSHOT_SCHEMA,
        "skillPolicy": policy,
        "skillRefs": list(refs),
    }
    if "promptSettings" in snapshot:
        result["promptSettings"] = normalize_prompt_settings(snapshot["promptSettings"])
    if "candidateSkillPaths" in snapshot:
        paths = snapshot["candidateSkillPaths"]
        if not isinstance(paths, list) or len(paths) > 8 or any(not isinstance(path, str) or not path or len(path) > 4096 for path in paths):
            return None
        result["candidateSkillPaths"] = list(paths)
    if "scenarioPolicy" in snapshot:
        policy = snapshot["scenarioPolicy"]
        if not isinstance(policy, Mapping):
            return None
        result["scenarioPolicy"] = dict(policy)
    return result


def _record_plugin_usage_notice(
    store: AgentPluginUsageStore,
    *,
    event: object,
    session_id: object,
    payload: Mapping[str, object],
) -> bool:
    if event != "runtime.notice" or payload.get("schemaVersion") != "paw.plugin-usage.v1":
        return False
    if not session_id or payload.get("sessionId") != session_id:
        return True
    try:
        store.record(payload)
    except (ValueError, sqlite3.Error):
        # Usage telemetry is fail-closed for privacy and fail-open for the
        # Agent loop: invalid/unknown fields are not retained.
        pass
    return True






@dataclass
class _HostedSessionState:
    runtime_engine: str = "classic"
    recoverable: bool = False
    compaction_target: CompactionTarget | None = None
    turn_id: str = ""
    client_message_id: str = ""
    prompt_admission_in_flight: bool = False
    admission_client_message_id: str = ""
    admission_preserve_archived: bool = False
    abort_pending_admission: bool = False
    admission_abort_dispatched: bool = False
    prompt_dispatched: bool = False
    prompt_dispatch_signal: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )
    stream_pi_message_id: str = ""
    # One product Turn can contain many Pi assistant/tool loops. This identity
    # is advanced by each assistant message_start and inherited by the Tool
    # events produced from that assistant message.
    source_loop_id: str = ""
    tool_source_loops: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    provider_request_ids: set[str] = field(default_factory=set)
    tool_blocks: AgentToolBlockBuffer = field(default_factory=AgentToolBlockBuffer)
    last_agent_messages: list[object] = field(default_factory=list)
    final_error: str = ""
    final_failure_context: dict[str, object] = field(default_factory=dict)
    provider_retry_attempt: int = 0
    provider_retry_max_attempts: int = 0
    had_tool_activity: bool = False
    pending_approvals: dict[str, str] = field(default_factory=dict)
    pending_reviews: dict[str, str] = field(default_factory=dict)
    pending_ui_requests: dict[str, dict[str, object]] = field(default_factory=dict)
    abort_timer: threading.Timer | None = field(default=None, repr=False)
    settle_timer: threading.Timer | None = field(default=None, repr=False)
    settle_extension_failed: bool = False
    abort_requested_turn_id: str = ""
    retired_turn_ids: set[str] = field(default_factory=set)


class PiRuntimeHostManager:
    """Product adapter for the v2 SDK host; Pi owns Sessions, Python owns product state."""

    def __init__(
        self,
        *,
        config: PiRuntimeConfig,
        sessions: AgentSessionStore,
        events: AgentEventHub,
        media_resolver: Callable[[str, str, str], str] | None = None,
        session_context_provider: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        tool_manifest_provider: Callable[[Mapping[str, object]], list[Mapping[str, object]]] | None = None,
        skill_allowlist_provider: SkillAllowlistProvider | None = None,
        compaction_observer: CompactionObserver | None = None,
        prompt_settings_provider: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        scenario_policy_provider: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        candidate_skill_paths_provider: Callable[[Mapping[str, object]], list[str]] | None = None,
    ) -> None:
        self.config = config
        self.sessions = sessions
        self.events = events
        self.plugin_usage = AgentPluginUsageStore(self.sessions.db_path)
        self.plugin_usage.initialize()
        self._media_resolver = media_resolver
        self._session_context_provider = session_context_provider
        self._tool_manifest_provider = tool_manifest_provider
        self._skill_allowlist_provider = skill_allowlist_provider
        self._compaction_observer = compaction_observer
        self._prompt_settings_provider = prompt_settings_provider
        self._scenario_policy_provider = scenario_policy_provider
        self._candidate_skill_paths_provider = candidate_skill_paths_provider
        self._lifecycle_lock = threading.RLock()
        self._model_catalog_lock = threading.Lock()
        self._lock = threading.RLock()
        self._gateway_control_source: ContextVar[tuple[str, Mapping[str, object]] | None] = ContextVar("paw_gateway_control_source", default=None)
        self._recent_projection_refreshes: set[str] = set()
        self._recent_projection_threads: set[threading.Thread] = set()
        self._client: PiRuntimeHostClient | None = None
        self._states: dict[str, _HostedSessionState] = {}
        self._open_sessions: set[str] = set()
        self._active_completion_ids: set[str] = set()
        self._classifications: dict[str, _ClassificationCall] = {}
        self._completion_sinks: dict[str, Callable[[str], None]] = {}
        self._status = "stopped" if config.enabled else "disabled"
        self._last_error = ""
        self._host_capabilities: dict[str, object] = {}
        self._idle_timer: threading.Timer | None = None
        self._intentional_stop = False
        self._owner_instance_id = f"runtime:{uuid.uuid4()}"
        self._kill_gate = RuntimeHostKillGate(self.sessions.db_path)
        self._kill_gate.initialize()
        self._orphan_kill_receipts = self._kill_gate.reconcile_orphans(
            owner_instance_id=self._owner_instance_id,
            now_ms=int(time.time() * 1000),
        )
        self._last_kill_receipt: dict[str, object] | None = None
        self._retired_host_turns: set[tuple[str, str]] = set()
        self._available_models_cache: tuple[dict[str, object], ...] = ()
        self._available_models_cached_at = 0.0
        self._available_models_cache_ready = False

    @property
    def runtime_kind(self) -> str:
        # Preserve the stable binding identity so v1 transcripts resume in v2.
        return "pi_rpc"

    @property
    def driver_id(self) -> str:
        return "managed-pi"

    @property
    def session_root(self) -> Path:
        return self.config.session_dir

    @property
    def default_model_profile(self) -> str:
        provider = str(self.config.provider or "").strip()
        model = str(self.config.model or "").strip()
        return f"{provider}/{model}" if provider and model else "pi/default"

    def _effective_codemode_mode(
        self,
        session: Mapping[str, object],
        *,
        binding: Mapping[str, object] | None = None,
        snapshot: Mapping[str, object] | None = None,
    ) -> str | None:
        """Resolve a mode only after native codemode support is verified.

        The configured preference is an input to a future open, not evidence
        that an older Host actually supports native codemode. A persisted
        binding marker or the current Host capability handshake must authorize
        this public projection.
        """

        if session.get("runtimeEngine") == "durable":
            return None

        capability_available = (
            codemode_capability(self._host_capabilities.get("codemode")).get(
                "available"
            )
            is True
        )
        host_negotiated = self._client is not None and self._client.running
        if host_negotiated and not capability_available:
            # A live Host handshake that omits or rejects codemode supersedes
            # an older binding marker. Cold/recent reads without a live Host
            # may still use the persisted verified marker.
            return None
        metadata = as_mapping((binding or {}).get("metadata"))
        binding_available = metadata.get("codemodeAvailable") is True
        for source in (snapshot or {},):
            raw = source.get("codemodeMode")
            if (
                isinstance(raw, str)
                and raw.strip().lower() in CODEMODE_MODES
                and (capability_available or binding_available)
            ):
                return raw.strip().lower()
        if binding_available:
            raw = metadata.get("codemodeMode")
            if isinstance(raw, str) and raw.strip().lower() in CODEMODE_MODES:
                return raw.strip().lower()
        return None

    def _requested_codemode_mode(
        self,
        session: Mapping[str, object],
        *,
        binding: Mapping[str, object] | None = None,
    ) -> str:
        raw = session.get("codemodeMode")
        if isinstance(raw, str) and raw.strip().lower() in {"on", "only", "off"}:
            return raw.strip().lower()
        metadata = as_mapping((binding or {}).get("metadata"))
        if metadata.get("codemodeAvailable") is not False:
            raw = metadata.get("codemodeMode")
            if isinstance(raw, str) and raw.strip().lower() in CODEMODE_MODES:
                return raw.strip().lower()
        return self.config.resolved_codemode_mode(session)

    @staticmethod
    def _codemode_payload(mode: str | None) -> dict[str, object]:
        return (
            {"codemodeMode": mode}
            if isinstance(mode, str) and mode in CODEMODE_MODES
            else {}
        )

    def runtime_status(self) -> dict[str, object]:
        installed = self.config.executable is not None and self.config.executable.expanduser().is_file()
        latest_kill_receipt = None
        if self._last_kill_receipt:
            latest_kill_receipt = self._kill_gate.receipt(
                str(self._last_kill_receipt["killReceiptId"])
            )
        with self._lock:
            busy = sorted(
                session_id
                for session_id, state in self._states.items()
                if state.turn_id or state.compaction_target
                or (
                    state.prompt_admission_in_flight
                    and not state.abort_pending_admission
                )
            )
            open_sessions = sorted(self._open_sessions)
            active_completions = sorted(self._active_completion_ids)
            status = "busy" if busy or active_completions or self._classifications else self._status
            last_error = self._last_error
            capabilities = dict(self._host_capabilities)
            host_negotiated = self._client is not None and self._client.running
        if self.config.enabled and not installed:
            status = "not_installed"
            last_error = last_error or redact_runtime_text(self.config.installation_error)
        elif self.config.enabled and installed and not self.config.model_configured:
            status = "needs_configuration"
            last_error = last_error or redact_runtime_text(self.config.model_configuration_error)
        return {
            "schemaVersion": "rag-ime.agent-runtime.v1",
            "enabled": self.config.enabled,
            "managed": True,
            "status": status,
            "driverId": self.driver_id,
            "runtimeKind": self.runtime_kind,
            "runtimeVersion": self.config.pi_version if installed else "",
            "piVersion": self.config.pi_version if installed else "",
            "protocolVersion": PI_HOST_PROTOCOL_VERSION,
            "idleTimeoutSeconds": self.config.idle_timeout_seconds,
            "activeSessionId": busy[0] if busy else (open_sessions[0] if len(open_sessions) == 1 else None),
            "activeSessionIds": busy,
            "activeCompletionIds": active_completions,
            "openSessionIds": open_sessions,
            "lastError": last_error,
            "capabilities": {
                "rpc": installed,
                "sessions": True,
                "sessionEngines": {
                    "classic": {"available": True},
                    "durable": {"available": host_negotiated and as_mapping(
                        as_mapping(capabilities.get("sessionEngines")).get("durable")
                    ).get("available") is True, "experimental": True, "version": "1"},
                },
                "conversationFork": (
                    bool(capabilities.get("conversationFork"))
                    if host_negotiated
                    else installed and str(self.config.protocol_version or "") == PI_HOST_PROTOCOL_VERSION
                ),
                "conversationRewrite": (
                    bool(capabilities.get("conversationRewrite"))
                    if host_negotiated
                    else False
                ),
                "multiSession": True,
                "maxSessions": int(capabilities.get("maxSessions") or self.config.max_sessions),
                "tools": True,
                "dynamicTools": True,
                "managedPlugins": bool(capabilities.get("managedPlugins", True)),
                "sessionControlState": bool(
                    capabilities.get("sessionControlState")
                ),
                "sessionBoundAbort": capabilities.get("sessionBoundAbort") is True,
                "sessionCompactionRecovery": capabilities.get("sessionCompactionRecovery") is True,
                "sessionSnapshot": True,
                "settledEvents": True,
                "statelessCompletion": (
                    bool(capabilities.get("statelessCompletion"))
                    if host_negotiated
                    else installed and str(self.config.protocol_version or "") == PI_HOST_PROTOCOL_VERSION
                ),
                "statelessClassification": host_negotiated and capabilities.get("statelessClassification") is True,
                "transientContext": bool(capabilities.get("transientContext")),
                "sessionSkillAllowlist": bool(
                    capabilities.get("sessionSkillAllowlist")
                ),
                "persistentDebugContext": bool(capabilities.get("persistentDebugContext")),
                "runtimePrimitives": runtime_primitive_capabilities(
                    capabilities.get("runtimePrimitives")
                ),
                "codemode": codemode_capability(
                    capabilities.get("codemode")
                ),
                "imageAttachments": True,
                "coordinator": True,
                "modelConfigured": self.config.model_configured,
            },
            "runtimeHostKillGate": {
                "ownerInstanceId": self._owner_instance_id,
                "orphanReconcileReceipts": list(self._orphan_kill_receipts),
                "lastKillReceipt": latest_kill_receipt,
            },
        }

    def is_resident_idle(self, session_id: str) -> bool:
        """Observe eligibility without starting, querying or recovering a Host."""
        with self._lock:
            state = self._states.get(session_id)
            return bool(self._client is not None and self._client.running
                and session_id in self._open_sessions and state is not None
                and not (state.turn_id or state.compaction_target or state.recoverable
                         or state.prompt_admission_in_flight or state.abort_pending_admission
                         or state.abort_requested_turn_id))

    def is_turn_active(self, session_id: str, turn_id: str, *, client_message_id: str) -> bool:
        """Observe this Host's exact live turn without opening or querying Pi.

        Persisted activity and a restored idle binding do not prove that a
        Provider run survived a Host restart. False means unconfirmed, not
        physically drained; settlement remains the separate authority.
        """
        if not session_id or not turn_id or not client_message_id:
            return False
        with self._lock:
            state = self._states.get(session_id)
            return bool(
                self._client is not None and self._client.running
                and session_id in self._open_sessions and state is not None
                and state.turn_id == turn_id
                and state.client_message_id == client_message_id
                and state.abort_requested_turn_id != turn_id
                and not state.recoverable
            )

    def is_gateway_turn_active(self, session_id: str, turn_id: str, *, client_message_id: str) -> bool:
        """Observe exact Gateway ownership, including HTTP before prompt ACK.

        Empty clientMessageId is an exact value for native Room turns, never a
        wildcard. Only an unresolved locally dispatched admission may query
        the already-open Host; this path never ensures or restores a Session.
        """
        if not session_id or not turn_id:
            return False
        with self._lock:
            state = self._states.get(session_id)
            client = self._client
            if (client is None or not client.running or session_id not in self._open_sessions
                or state is None or state.recoverable or state.abort_pending_admission
                or state.abort_requested_turn_id or turn_id in state.retired_turn_ids
                or (session_id, turn_id) in self._retired_host_turns):
                return False
            if state.turn_id:
                return state.turn_id == turn_id and state.client_message_id == client_message_id
            if (not state.prompt_admission_in_flight or not state.prompt_dispatched
                or state.admission_client_message_id != client_message_id
                or not self._host_capabilities.get("sessionControlState")):
                return False
        # JSONL request dispatcher handles this read independently of tool
        # execution. The client write lock is released before response waiting.
        try:
            observed = client.send("session.control_state", {"sessionId": session_id}, timeout=5)
        except Exception:
            return False
        active = observed.get("activeTurn")
        if (observed.get("sessionId") != session_id or observed.get("isIdle") is not False
            or not isinstance(active, Mapping) or str(active.get("turnId") or "") != turn_id
            or str(active.get("clientMessageId") or "") != client_message_id):
            return False
        with self._lock:
            state = self._states.get(session_id)
            return bool(self._client is client and client.running and session_id in self._open_sessions
                and state is not None and not state.abort_pending_admission
                and not state.abort_requested_turn_id and turn_id not in state.retired_turn_ids
                and (session_id, turn_id) not in self._retired_host_turns
                and ((state.turn_id == turn_id and state.client_message_id == client_message_id)
                    or (not state.turn_id and state.prompt_admission_in_flight and state.prompt_dispatched
                        and state.admission_client_message_id == client_message_id)))

    @contextmanager
    def gateway_turn_effect_fence(self, session_id: str, binding: Mapping[str, object], *, observe: bool = True):
        """Commit a passive control mutation under the existing Stop fence.

        Observe an unresolved admission outside the lock, then revalidate its
        local identity under the same lock Stop uses. Callers acquire this
        fence before a SQLite writer, never while holding a database lock.
        """
        turn_id = str(binding.get("turnId") or "")
        client_id = str(binding.get("clientMessageId") or "")
        if observe and not self.is_gateway_turn_active(session_id, turn_id, client_message_id=client_id):
            raise PiRuntimeTurnConflict("control mutation belongs to an inactive Runtime turn")
        with self._lock:
            state = self._states.get(session_id)
            if (self._client is None or not self._client.running or session_id not in self._open_sessions
                or state is None or state.recoverable or state.abort_pending_admission
                or state.abort_requested_turn_id or turn_id in state.retired_turn_ids
                or (session_id, turn_id) in self._retired_host_turns
                or not ((state.turn_id == turn_id and state.client_message_id == client_id)
                        or (not state.turn_id and state.prompt_admission_in_flight and state.prompt_dispatched
                            and state.admission_client_message_id == client_id))):
                raise PiRuntimeTurnConflict("control mutation belongs to an inactive Runtime turn")
            yield

    @contextmanager
    def gateway_control_scope(self, source_session_id: str, binding: Mapping[str, object] | None):
        """Carry a native controller's original turn only through its dispatch call.

        The target's ordinary reservation/Root remains its execution authority.
        No lock is held while context, routing or a Provider ACK is awaited.
        """
        token = self._gateway_control_source.set((source_session_id, dict(binding)) if binding is not None else None)
        try:
            yield
        finally:
            self._gateway_control_source.reset(token)

    def gateway_dispatch_fence(self):
        source = self._gateway_control_source.get()
        # Final before_write executes under the Host client write lock. It must
        # never issue a nested control_state JSONL request. The Tool admission
        # already observed its exact binding; revalidate local Stop state only.
        return self.gateway_turn_effect_fence(*source, observe=False) if source is not None else nullcontext()

    def panic_kill(self, *, requested_by: str, reason: str) -> dict[str, object]:
        """Immediately kill the registered Host process tree after admin auth."""
        with self._lifecycle_lock:
            client = self._require_client()
            receipt = self._kill_gate.request_kill(
                client.host_identity,
                request_kind="admin_panic",
                requested_by=str(requested_by).strip(),
                reason=str(reason).strip() or "administrator panic",
                now_ms=int(time.time() * 1000),
            )
            with self._lock:
                self._last_kill_receipt = dict(receipt)
                if self._status != "faulted":
                    self._status = "stopping"
            return receipt

    def reconcile_runtime_hosts(self) -> list[dict[str, object]]:
        """Retry bounded orphan termination before a new Host is admitted."""
        receipts = self._kill_gate.reconcile_orphans(
            owner_instance_id=self._owner_instance_id,
            now_ms=int(time.time() * 1000),
            include_owner=True,
        )
        with self._lock:
            self._orphan_kill_receipts.extend(receipts)
        return receipts

    def _host(self) -> PiRuntimeHostClient:
        # Host admission is a lifecycle transition, not a cache lookup. Model
        # catalogs, Session restore, and role settings can all request the Host
        # concurrently when the UI opens. Without this lock, two callers can
        # both observe `_client is None`, start two child processes, and let the
        # second registration fault on the first one's durable kill-gate row.
        # The losing caller can then clear `_client`, leaving the successfully
        # started Host registered but unreachable. The lifecycle lock is an
        # RLock because ensure/stop paths already hold it before reaching here.
        with self._lifecycle_lock:
            return self._host_locked()

    def _host_locked(self) -> PiRuntimeHostClient:
        with self._lock:
            if self._client is not None and self._client.running:
                return self._client
        if not self.config.enabled:
            raise PiRuntimeError("Pi runtime is disabled")
        if self.config.executable is None:
            raise PiRuntimeError(self.config.installation_error or "managed Pi runtime is not installed")
        self.reconcile_runtime_hosts()
        client = PiRuntimeHostClient(
            self.config,
            on_event=lambda envelope: self._handle_host_event(envelope, source_client=client),
            on_exit=lambda code, error: self._handle_host_exit(code, error, source_client=client),
            kill_gate=self._kill_gate,
            owner_instance_id=self._owner_instance_id,
        )
        with self._lock:
            self._client = client
            self._status = "starting"
            self._last_error = ""
            self._intentional_stop = False
        try:
            hello = client.start()
        except Exception as exc:
            with self._lock:
                if self._client is client:
                    self._client = None
                self._status = "faulted"
                self._last_error = redact_runtime_text(str(exc))
            client.stop()
            raise
        with self._lock:
            self._host_capabilities = dict(as_mapping(hello.get("capabilities")))
            self._status = "ready"
        return client

    @staticmethod
    def _candidate_skill_paths(value: object, cwd: str) -> list[str]:
        if not isinstance(value, list) or len(value) > 8:
            raise PiRuntimeError("Candidate Skill paths must be a bounded host-provided list")
        if not value:
            return []
        root = Path(cwd).expanduser().resolve(strict=True)
        result = []
        for raw in value:
            if not isinstance(raw, str) or len(raw) > 4096 or not Path(raw).is_absolute():
                raise PiRuntimeError("Candidate Skill path must be absolute")
            try:
                path = Path(raw).resolve(strict=True)
                entry = (path / "SKILL.md").resolve(strict=True)
            except OSError as error:
                raise PiRuntimeError("Candidate Skill resource is unavailable") from error
            if path == root or not path.is_dir() or not path.is_relative_to(root) or not entry.is_file() or not entry.is_relative_to(path):
                raise PiRuntimeError("Candidate Skill must remain inside its isolated Session workspace")
            if str(path) in result:
                raise PiRuntimeError("Candidate Skill paths must be unique")
            result.append(str(path))
        return result

    def _session_skill_allowlist(
        self,
        session: Mapping[str, object],
    ) -> list[str] | None:
        provider = self._skill_allowlist_provider
        if provider is None:
            return None
        values = provider(session)
        if len(values) > 128:
            raise PiRuntimeError("Session Skill allowlist contains too many Skills")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            skill_id = str(value).strip()
            if (
                not skill_id
                or len(skill_id) > 128
                or not skill_id[0].isalnum()
                or any(
                    character
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                    for character in skill_id
                )
            ):
                raise PiRuntimeError("Session Skill allowlist contains an invalid Skill ID")
            if skill_id in seen:
                raise PiRuntimeError("Session Skill allowlist contains duplicate Skills")
            seen.add(skill_id)
            normalized.append(skill_id)
        return normalized

    def ensure(
        self,
        session_id: str,
        *,
        retire_recovered_turn: bool = True,
        _sync_codemode: bool = True,
    ) -> dict[str, object]:
        with self._lifecycle_lock:
            session = dict(self.sessions.get(session_id))
            if not self.config.model_configured and session.get("runtimeEngine") != "durable":
                raise PiRuntimeError(
                    self.config.model_configuration_error
                    or "Pi model is not configured"
                )
            client = self._host()
            binding = self.sessions.runtime_binding(session_id)
            if session.get("runtimeEngine") == "durable":
                return self._ensure_durable(session_id, session, binding, client)
            resource_snapshot = _bound_session_resource_snapshot(binding)
            if binding is not None:
                if (
                    binding.get("driverId") != self.driver_id
                    or binding.get("runtimeKind") != self.runtime_kind
                ):
                    raise PiRuntimeError(
                        "Agent session belongs to another runtime driver"
                    )
                session["_runtimeBinding"] = binding
            if self._session_context_provider is not None:
                session.update(dict(self._session_context_provider(session)))

            with self._lock:
                already_open = session_id in self._open_sessions
            if already_open:
                use_control_state = bool(
                    self._host_capabilities.get("sessionControlState")
                )
                snapshot = dict(
                    client.send(
                        (
                            "session.control_state"
                            if use_control_state
                            else "session.snapshot"
                        ),
                        {"sessionId": session_id},
                    )
                )
                if use_control_state and (
                    snapshot.get("schemaVersion")
                    != "rag-ime.pi-session-control-state.v1"
                    or snapshot.get("sessionId") != session_id
                    or not isinstance(snapshot.get("isIdle"), bool)
                ):
                    raise PiRuntimeError(
                        "Pi Runtime Host returned an invalid Session control state"
                    )
                self._sync_idle_snapshot(session_id, snapshot)
                recovered_turn_retirement = None
                restored_turn_id = str(
                    as_mapping(snapshot.get("activeTurn")).get("turnId") or ""
                ).strip()
                if (
                    retire_recovered_turn
                    and snapshot.get("isIdle") is True
                    and restored_turn_id
                ):
                    # Catalog/preflight reads may already have opened this
                    # Session without retiring its restored durable binding.
                    # Reuse must honor the same exact-idle recovery as open.
                    recovered_turn_retirement = self.retire_recovered_turn(
                        session_id, restored_turn_id,
                    )
                    snapshot = dict(as_mapping(recovered_turn_retirement.get("state")))
                effective_codemode_mode = self._effective_codemode_mode(
                    session,
                    binding=binding,
                    snapshot=snapshot,
                )
                requested_codemode_mode = self._requested_codemode_mode(
                    session,
                    binding=binding,
                )
                binding_mode = str(
                    as_mapping((binding or {}).get("metadata")).get("codemodeMode")
                    or ""
                ).strip().lower()
                if (
                    _sync_codemode
                    and snapshot.get("isIdle") is True
                    and not as_mapping(snapshot.get("activeTurn"))
                    and requested_codemode_mode != binding_mode
                    and codemode_capability(self._host_capabilities.get("codemode")).get(
                        "available"
                    ) is True
                ):
                    changed = self._set_codemode_mode_after_ensure(
                        session_id,
                        requested_codemode_mode,
                        state=snapshot,
                        binding=binding,
                    )
                    snapshot = dict(as_mapping(changed.get("state")))
                    binding = self.sessions.runtime_binding(session_id)
                    effective_codemode_mode = str(
                        changed.get("codemodeMode") or requested_codemode_mode
                    )
                with self._lock:
                    self._schedule_idle_locked()
                return {
                    "state": snapshot,
                    "resourceSnapshot": resource_snapshot,
                    "reused": True,
                    **self._codemode_payload(effective_codemode_mode),
                    **({"recoveredTurnRetirement": recovered_turn_retirement}
                       if recovered_turn_retirement is not None else {}),
                }

            roots = [
                str(value)
                for value in session.get("workspaceRoots") or []
                if str(value).strip()
            ]
            cwd = roots[0] if roots else str(self.config.agent_dir)
            provider, model_id = self.config.resolved_model_reference(session)
            session_file = str(
                (binding or {}).get("transcriptRef")
                or session.get("sessionFile")
                or ""
            ).strip()
            if session_file:
                # Fork bindings store canonical paths, while the Host checks
                # against its configured directory spelling. A managed root
                # may be a symlink (for example, sessions on an external disk).
                # Map only files inside that same physical root back to the
                # Host's spelling; leave outside paths for its rejection gate.
                session_root = self.config.session_dir.expanduser()
                try:
                    relative = Path(session_file).expanduser().resolve(strict=False).relative_to(
                        session_root.resolve(strict=False)
                    )
                except ValueError:
                    pass
                else:
                    session_file = (session_root / relative).as_posix()
            memory_curation_session = (
                str(session.get("toolProfileVersion") or "")
                == MEMORY_CURATION_TOOL_PROFILE
            )
            if memory_curation_session:
                skill_allowlist: list[str] | None = []
            elif resource_snapshot is not None:
                skill_allowlist = (
                    list(resource_snapshot["skillRefs"])
                    if resource_snapshot["skillPolicy"] == "allowlist"
                    else None
                )
            else:
                skill_allowlist = self._session_skill_allowlist(session)
            prompt_settings: Mapping[str, object] | None = None
            scenario_settings: Mapping[str, object] | None = None
            specialized_session = str(session.get("toolProfileVersion") or "") in {
                "ime-surface-v1", "voice-refinement-v1", MEMORY_CURATION_TOOL_PROFILE,
            }
            if not specialized_session:
                if resource_snapshot is not None:
                    # A pre-existing binding without prompt settings keeps its original policy.
                    prompt_settings = as_mapping(resource_snapshot.get("promptSettings")) or None
                elif self._prompt_settings_provider is not None:
                    prompt_settings = normalize_prompt_settings(self._prompt_settings_provider(session))
            if resource_snapshot is not None:
                scenario_settings = as_mapping(resource_snapshot.get("scenarioPolicy")) or None
            elif self._scenario_policy_provider is not None:
                scenario_settings = dict(self._scenario_policy_provider(session))
            if resource_snapshot is None:
                resource_snapshot = _session_resource_snapshot(skill_allowlist)
                if prompt_settings is not None:
                    resource_snapshot["promptSettings"] = dict(prompt_settings)
                if scenario_settings is not None:
                    resource_snapshot["scenarioPolicy"] = dict(scenario_settings)
                candidate_paths_provider = self._candidate_skill_paths_provider
                paths = candidate_paths_provider(session) if candidate_paths_provider is not None else []
                if paths:
                    resource_snapshot["candidateSkillPaths"] = self._candidate_skill_paths(paths, cwd)
            candidate_skill_paths = self._candidate_skill_paths(resource_snapshot.get("candidateSkillPaths", []), cwd)
            if candidate_skill_paths and (not self._host_capabilities.get("sessionCandidateSkillPaths") or not skill_allowlist or not bool(session.get("piSkillsEnabled"))):
                raise PiRuntimeError("Pi Runtime Host does not support isolated candidate Skill loading; update the managed Runtime before evaluating this Skill")
            if (
                skill_allowlist is not None
                and not bool(self._host_capabilities.get("sessionSkillAllowlist"))
            ):
                skill_systems_enabled = (
                    not memory_curation_session
                    and (
                        bool(session.get("piSkillsEnabled", False))
                        or bool(session.get("codexSkillsEnabled", False))
                    )
                )
                if skill_allowlist and skill_systems_enabled:
                    raise PiRuntimeError(
                        "Pi Runtime Host does not support per-Session Skill allowlists"
                    )
                # The allowlist adds no authority when both Skill systems are
                # disabled (Memory curation also forces them off). Older Hosts
                # do not understand the field, so omit it while retaining
                # fail-closed behavior whenever a Skill system can load it.
                skill_allowlist = None
            params: dict[str, object] = {
                "sessionId": session_id,
                "cwd": cwd,
                "systemPrompt": self.config.system_prompt_for_session(
                    session,
                    prompt_settings=prompt_settings,
                    scenario_settings=scenario_settings,
                ),
                "toolManifest": (
                    []
                    if memory_curation_session
                    else self.tool_catalog(session_id)
                ),
                "nativeMcpExecutionAllowed": self._native_mcp_execution_policy(session),
                "noContextFiles": (
                    str(session.get("toolProfileVersion") or "")
                    in {
                        "ime-surface-v1",
                        "voice-refinement-v1",
                        MEMORY_CURATION_TOOL_PROFILE,
                    }
                    or not bool(session.get("projectContextEnabled", False))
                ),
                "piSkillsEnabled": (
                    False
                    if memory_curation_session
                    else bool(session.get("piSkillsEnabled", False))
                ),
                "codexSkillsEnabled": (
                    False
                    if memory_curation_session
                    else bool(session.get("codexSkillsEnabled", False))
                ),
            }
            if codemode_capability(self._host_capabilities.get("codemode")).get(
                "available"
            ) is True:
                params["codemodeMode"] = self._requested_codemode_mode(
                    session,
                    binding=binding,
                )
            if skill_allowlist is not None:
                params["skillAllowlist"] = skill_allowlist
            resource_policy = as_mapping(session.get("resourceDisclosurePolicy"))
            if any(resource_policy.get(key) for key in ("disabledSkillNames", "disabledPluginIds")):
                if not self._host_capabilities.get("sessionResourceDisclosure"):
                    raise PiRuntimeError("请更新 PAW，以应用当前对话的插件和技能开关")
                params["resourceDisclosurePolicy"] = dict(resource_policy)
            if candidate_skill_paths:
                params["candidateSkillPaths"] = candidate_skill_paths
            compaction_instructions = str((prompt_settings or {}).get("compactionInstructions") or "")
            if prompt_settings is not None:
                if not self._host_capabilities.get("sessionPromptSettings"):
                    raise PiRuntimeError("Pi Runtime Host does not support prompt settings; update the managed Runtime before opening this Session")
                params["compactionInstructions"] = compaction_instructions
            session_context = str(session.get("sessionContext") or "").strip()
            if session_context:
                params["sessionContext"] = session_context
            if provider and model_id:
                params.update({"provider": provider, "modelId": model_id})
            thinking_level = str(
                session.get("thinkingLevel") or ""
            ).strip().lower()
            if thinking_level:
                params["thinkingLevel"] = thinking_level
            if session_file:
                params["sessionFile"] = session_file

            result = client.send(
                "session.open",
                params,
                timeout=max(
                    60.0,
                    self.config.command_timeout_seconds,
                ),
            )
            snapshot = dict(as_mapping(result.get("snapshot")))
            codemode_available = (
                codemode_capability(self._host_capabilities.get("codemode")).get(
                    "available"
                )
                is True
            )
            effective_codemode_mode = self._effective_codemode_mode(
                session,
                binding=binding,
                snapshot=snapshot,
            )
            model = as_mapping(snapshot.get("model"))
            if model.get("provider") and model.get("id"):
                self.sessions.set_model_profile(
                    session_id,
                    f"{model['provider']}/{model['id']}",
                )
            binding_metadata = dict(as_mapping((binding or {}).get("metadata")))
            binding_metadata.update(
                {
                    "protocolVersion": PI_HOST_PROTOCOL_VERSION,
                    "resourceSnapshot": resource_snapshot,
                    "codemodeAvailable": codemode_available,
                }
            )
            if effective_codemode_mode is not None:
                binding_metadata["codemodeMode"] = effective_codemode_mode
            else:
                binding_metadata.pop("codemodeMode", None)
            bound = self.sessions.bind_runtime_session(
                session_id,
                driver_id=self.driver_id,
                runtime_kind=self.runtime_kind,
                external_session_id=str(
                    snapshot.get("piSessionId") or session_id
                ),
                transcript_ref=str(snapshot.get("sessionFile") or ""),
                branch_anchor=str(snapshot.get("leafId") or ""),
                binding_state="active",
                metadata=binding_metadata,
                # The Host count describes Pi's Provider transcript and can
                # include Tool/protocol entries.  AgentMessageSnapshot owns
                # the public conversation count, so opening a resident Pi
                # Session must preserve that product projection instead of
                # overwriting it with a different unit.
                message_count=max(0, int(session.get("messageCount") or 0)),
            )
            idle_session = self._sync_idle_snapshot(
                session_id,
                snapshot,
            )
            if idle_session is not None:
                bound = idle_session
            evicted = str(result.get("evictedSessionId") or "")
            with self._lock:
                self._open_sessions.add(session_id)
                hosted_state = self._states.setdefault(
                    session_id,
                    _HostedSessionState(),
                )
                if evicted:
                    self._open_sessions.discard(evicted)
                    evicted_state = self._states.pop(evicted, None)
                    if evicted_state is not None:
                        if evicted_state.abort_timer is not None:
                            evicted_state.abort_timer.cancel()
                        if evicted_state.settle_timer is not None:
                            evicted_state.settle_timer.cancel()
                admission_in_flight = (
                    hosted_state.prompt_admission_in_flight
                    and not hosted_state.abort_pending_admission
                )
                self._status = (
                    "busy" if admission_in_flight else "ready"
                )
                if not admission_in_flight:
                    self._schedule_idle_locked()
            recovered_turn_retirement: dict[str, object] | None = None
            restored_turn = as_mapping(snapshot.get("activeTurn"))
            restored_turn_id = str(
                restored_turn.get("turnId") or ""
            ).strip()
            if (
                retire_recovered_turn
                and snapshot.get("isIdle") is True
                and restored_turn_id
            ):
                # A restarted Host can restore Pi's durable turn binding after
                # the native Provider run has already disappeared. Leaving
                # that idle binding in place makes every later prompt fail
                # SESSION_BUSY even though there is no work left to resume.
                # Retire only the exact turn proved by both the open snapshot
                # and the Host control state before admitting new work.
                recovered_turn_retirement = (
                    self.retire_recovered_turn(
                        session_id,
                        restored_turn_id,
                    )
                )
                snapshot = dict(
                    as_mapping(
                        recovered_turn_retirement.get("state")
                    )
                )
                bound = self.sessions.get(session_id)
            # Opening a cold Pi Session is part of prompt admission. Do not
            # publish a late `ready` after a concurrent Stop already exposed
            # `aborting`; that would regress the UI while the same admission
            # is still being fenced.
            if not admission_in_flight:
                self.events.publish(
                    session_id,
                    "status_changed",
                    {"status": "ready"},
                )
            return {
                "state": snapshot,
                "session": bound,
                "resourceSnapshot": resource_snapshot,
                "evictedSessionId": evicted or None,
                "reused": False,
                **self._codemode_payload(effective_codemode_mode),
                **(
                    {
                        "recoveredTurnRetirement": (
                            recovered_turn_retirement
                        )
                    }
                    if recovered_turn_retirement is not None
                    else {}
                ),
            }

    def _is_durable(self, session_id: str) -> bool:
        with self._lock:
            state = self._states.get(session_id)
            if state is not None:
                return state.runtime_engine == "durable"
        sessions = getattr(self, "sessions", None)
        return sessions is not None and sessions.get(session_id).get("runtimeEngine") == "durable"

    def require_session_engine(self, engine: str) -> None:
        """Negotiate an opt-in before creating or opening execution storage."""
        if engine == "classic":
            return
        if engine != "durable":
            raise ValueError("runtime engine must be classic or durable")
        with self._lifecycle_lock:
            self._host()
            capability = as_mapping(as_mapping(self._host_capabilities.get("sessionEngines")).get("durable"))
            if capability.get("available") is not True or capability.get("version") != "1":
                raise PiRuntimeError("Pi Runtime Host does not support Durable Sessions")

    def _require_classic_control(self, session_id: str, control: str) -> None:
        if self._is_durable(session_id):
            raise PiRuntimeError(f"Durable Sessions do not support {control}")

    def _ensure_durable(
        self, session_id: str, session: Mapping[str, object],
        binding: Mapping[str, object] | None, client: PiRuntimeHostClient,
    ) -> dict[str, object]:
        self.require_session_engine("durable")
        if binding is None or binding.get("driverId") != self.driver_id or binding.get("runtimeKind") != "pi_durable":
            raise PiRuntimeError("Durable Session has an incompatible runtime binding")
        durable_root = self.config.session_dir.expanduser().resolve(strict=False) / "durable"
        if durable_root.is_symlink():
            raise PiRuntimeError("Durable Session storage root must not be a symlink")
        root = durable_root.resolve(strict=False)
        # Store-created opaque IDs are the directory identity, never a caller path.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,239}", session_id):
            raise PiRuntimeError("Durable Session identity is invalid")
        store_ref = (root / session_id).resolve(strict=False)
        if not path_is_within(store_ref, root) or (root / session_id).is_symlink():
            raise PiRuntimeError("Durable Session storage is outside its managed root")
        prior_ref = str(binding.get("transcriptRef") or "")
        if prior_ref and Path(prior_ref).expanduser().resolve(strict=False) != store_ref:
            raise PiRuntimeError("Durable Session storage does not match its immutable binding")
        with self._lock:
            opened = session_id in self._open_sessions
        evicted = ""
        if opened:
            snapshot = dict(client.send("session.control_state", {"sessionId": session_id}))
        else:
            roots = list(session.get("workspaceRoots") or [])
            enriched = dict(session)
            if self._session_context_provider is not None:
                enriched.update(dict(self._session_context_provider(enriched)))
            provider, model_id = self.config.resolved_model_reference(session)
            params: dict[str, object] = {
                "sessionId": session_id, "runtimeEngine": "durable", "durableStoreRef": store_ref.as_posix(),
                "cwd": str(roots[0]) if roots else str(self.config.agent_dir),
                "systemPrompt": self.config.system_prompt_for_session(enriched),
                "toolManifest": self.tool_catalog(session_id), "nativeMcpExecutionAllowed": False,
                "piSkillsEnabled": False, "codexSkillsEnabled": False,
            }
            if enriched.get("sessionContext"):
                params["sessionContext"] = enriched["sessionContext"]
            if provider and model_id:
                params.update(provider=provider, modelId=model_id)
            if session.get("thinkingLevel"):
                params["thinkingLevel"] = session["thinkingLevel"]
            opened_result = client.send("session.open", params, timeout=max(60.0, self.config.command_timeout_seconds))
            snapshot = dict(as_mapping(opened_result.get("snapshot")))
            evicted = str(opened_result.get("evictedSessionId") or "")
        self._validate_durable_state(session_id, snapshot, store_ref=store_ref, control=opened)
        self._validate_durable_binding(snapshot, binding)
        metadata = {**dict(as_mapping(binding.get("metadata"))), "runtimeEngine": "durable",
                    "durableConversationId": snapshot["durableConversationId"],
                    "engineCapabilities": dict(as_mapping(snapshot.get("engineCapabilities"))),
                    "protocolVersion": PI_HOST_PROTOCOL_VERSION}
        if not opened:
            self.sessions.bind_runtime_session(session_id, driver_id=self.driver_id, runtime_kind="pi_durable",
                external_session_id=str(snapshot["piSessionId"]), transcript_ref=store_ref.as_posix(),
                branch_anchor=str(snapshot.get("leafId") or ""), metadata=metadata,
                message_count=as_integer(session.get("messageCount")))
        self._observe_durable_state(session_id, snapshot)
        with self._lock:
            self._open_sessions.add(session_id)
            if evicted:
                self._open_sessions.discard(evicted)
                evicted_state = self._states.pop(evicted, None)
                if evicted_state is not None:
                    if evicted_state.abort_timer is not None:
                        evicted_state.abort_timer.cancel()
                    if evicted_state.settle_timer is not None:
                        evicted_state.settle_timer.cancel()
            self._schedule_idle_locked()
        return {"state": snapshot, "session": self.sessions.get(session_id), "reused": opened,
                "evictedSessionId": evicted or None,
                "engineCapabilities": metadata["engineCapabilities"]}

    @staticmethod
    def _validate_durable_binding(snapshot: Mapping[str, object], binding: Mapping[str, object]) -> None:
        if binding.get("state") != "prepared":
            metadata = as_mapping(binding.get("metadata"))
            if (snapshot.get("piSessionId") != binding.get("externalSessionId")
                or snapshot.get("durableConversationId") != metadata.get("durableConversationId")
                or snapshot.get("durableStoreRef") != binding.get("transcriptRef")):
                raise PiRuntimeError("Pi Durable state belongs to another storage binding")

    @staticmethod
    def _validate_durable_state(
        session_id: str, state: Mapping[str, object], *, store_ref: Path | None = None, control: bool = False,
    ) -> None:
        if (state.get("sessionId") != session_id or state.get("runtimeEngine") != "durable"
            or not isinstance(state.get("isIdle"), bool)
            or not isinstance(state.get("paused"), bool) or not isinstance(state.get("recoverable"), bool)
            or not str(state.get("piSessionId") or "") or not str(state.get("durableConversationId") or "")
            or not isinstance(state.get("engineCapabilities"), Mapping)
            or not str(state.get("durableStoreRef") or "")
            or (control and state.get("schemaVersion") != "rag-ime.pi-session-control-state.v1")
            or (store_ref is not None and str(state["durableStoreRef"]) != store_ref.as_posix())):
            raise PiRuntimeError("Pi returned invalid Durable Session state")
        active = as_mapping(state.get("activeTurn"))
        if active and (not str(active.get("turnId") or "") or not str(active.get("clientMessageId") or "")):
            raise PiRuntimeError("Pi Durable active input has no exact identity")
        target = None
        if state.get("compactionTarget") is not None:
            try:
                target = validate_compaction_target(state["compactionTarget"])
            except ValueError as exc:
                raise PiRuntimeError("Pi returned an invalid Durable compaction target") from exc
            if state.get("activeTurn") is not None or state["isIdle"] or target["runtimeSessionId"] != state["piSessionId"]:
                raise PiRuntimeError("Pi Durable compaction state is inconsistent")
        if state["recoverable"] and (not state["paused"] or state["isIdle"] or not (active or target)):
            raise PiRuntimeError("Pi Durable recovery state is inconsistent")
        capabilities = as_mapping(state.get("engineCapabilities"))
        if (set(capabilities) - {"compactionRecovery"} != _DURABLE_ENGINE_CAPABILITIES
            or (target is not None and capabilities.get("compactionRecovery") is not True)
            or any(not isinstance(value, bool) for value in capabilities.values())
            or any(capabilities[key] for key in _DURABLE_UNSUPPORTED_CAPABILITIES)):
            raise PiRuntimeError("Pi returned incompatible Durable engine capabilities")

    def _observe_durable_state(self, session_id: str, snapshot: Mapping[str, object]) -> None:
        # A previous input's receipt does not own a subsequent standalone task.
        settlement = as_mapping(snapshot.get("turnSettlement"))
        if settlement and as_mapping(settlement.get("receipt")).get("disposition") != "suspended":
            validated = self._validate_turn_settlement(settlement, session_id=session_id,
                turn_id=str(settlement.get("turnId") or ""), client_message_id=str(settlement.get("clientMessageId") or ""))
            self._reconcile_turn_settlement(validated)
        active = as_mapping(snapshot.get("activeTurn"))
        target = validate_compaction_target(snapshot["compactionTarget"]) if snapshot.get("compactionTarget") is not None else None
        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
            state.runtime_engine = "durable"
            state.recoverable = snapshot.get("recoverable") is True
            state.compaction_target = target
            if active:
                if state.turn_id and (state.turn_id != active["turnId"] or state.client_message_id != active["clientMessageId"]):
                    raise PiRuntimeTurnConflict("Durable observation belongs to a different input")
                state.turn_id = str(active["turnId"])
                state.client_message_id = str(active["clientMessageId"])
            elif target:
                # No fabricated turn or old input may act as cancellation authority.
                state.turn_id = ""
                state.client_message_id = ""
            if active or target:
                self._status = "busy"
                self._cancel_idle_locked()
                self.sessions.set_status(session_id, "busy", **(
                    {"_preserve_archived": True} if state.admission_preserve_archived else {}))
            elif not state.turn_id and not state.prompt_admission_in_flight and snapshot.get("isIdle") is True:
                self.sessions.set_status(session_id, "idle", **(
                    {"_preserve_archived": True} if state.admission_preserve_archived else {}))
                if not any(item.turn_id or item.compaction_target or item.prompt_admission_in_flight
                           for item in self._states.values()):
                    self._status = "ready"
                self._schedule_idle_locked()

    def resume_compaction(self, session_id: str, target: object) -> dict[str, object]:
        """Resume the named original native task set without admitting input."""
        return self._control_compaction(session_id, target, action="resume")

    def abort_compaction(self, session_id: str, target: object) -> dict[str, object]:
        """Stop only named compaction tasks, without turn/job cancellation authority."""
        return self._control_compaction(session_id, target, action="abort")

    def _control_compaction(self, session_id: str, target: object, *, action: str) -> dict[str, object]:
        with self._lifecycle_lock:
            identity = validate_compaction_target(target)
            if not self._is_durable(session_id):
                raise PiRuntimeError("Compaction recovery requires a Durable Session")
            self.require_session_engine("durable")
            if self._host_capabilities.get("sessionCompactionRecovery") is not True:
                raise PiRuntimeError("Pi Runtime Host does not support compaction recovery")
            prepared = self.ensure(session_id, retire_recovered_turn=False)
            before = as_mapping(prepared.get("state"))
            if (as_mapping(before.get("engineCapabilities")).get("compactionRecovery") is not True
                or before.get("piSessionId") != identity["runtimeSessionId"]):
                raise PiRuntimeError("Durable compaction target has an incompatible native binding")
            client = self._require_client()
            def require_original_host() -> None:
                with self._lock:
                    if self._client is not client or session_id not in self._open_sessions:
                        raise PiRuntimeTurnConflict("Durable compaction control lost its native Host binding")
            # Terminal retries can coexist with newer work. Only Pi's native task
            # ledger may decide the original outcome; never retarget to current work.
            result = client.send(f"session.{action}", {"sessionId": session_id, "compactionTarget": identity},
                                 before_write=require_original_host)
            if (result.get("schemaVersion") != f"rag-ime.pi-compaction-{action}.v1"
                or result.get("accepted") is not True or result.get("runtimeEngine") != "durable"
                or result.get("compactionTarget") != identity):
                raise PiRuntimeError("Pi returned an invalid Durable compaction receipt")
            if action == "resume":
                if not isinstance(result.get("resumed"), bool):
                    raise PiRuntimeError("Pi returned an invalid compaction resume outcome")
            else:
                outcomes = result.get("outcomes")
                if (result.get("drained") is not True or not isinstance(outcomes, list)
                    or any(not isinstance(outcome, Mapping) or set(outcome) != {"taskId", "status"}
                           or outcome.get("status") not in {"completed", "aborted", "failed"} for outcome in outcomes)
                    or [outcome["taskId"] for outcome in outcomes] != identity["taskIds"]):
                    raise PiRuntimeError("Pi returned an unsettled or invalid compaction abort outcome")
            snapshot = as_mapping(result.get("state"))
            self._validate_durable_state(session_id, snapshot, control=True)
            self._validate_durable_binding(snapshot, self.sessions.runtime_binding(session_id) or {})
            self._observe_durable_state(session_id, snapshot)
            return dict(result)

    def resume_session(self, session_id: str, *, turn_id: str, client_message_id: str) -> dict[str, object]:
        """Resume one admitted native input; never submit it again."""
        if not self._is_durable(session_id):
            raise PiRuntimeError("Session resume requires a Durable Session")
        if not turn_id or not client_message_id:
            raise ValueError("resume requires the original turn and client identity")
        prepared = self.ensure(session_id, retire_recovered_turn=False)
        active = as_mapping(as_mapping(prepared.get("state")).get("activeTurn"))
        if active and (active.get("turnId") != turn_id or active.get("clientMessageId") != client_message_id):
            raise PiRuntimeTurnConflict("Durable resume belongs to a different input")
        identity = {"sessionId": session_id, "turnId": turn_id, "clientMessageId": client_message_id}
        client = self._require_client()
        def require_original_input() -> None:
            with self.gateway_dispatch_fence(), self._lock:
                state = self._states.get(session_id)
                if (self._client is not client or state is None or state.abort_requested_turn_id
                    or state.abort_pending_admission or (state.turn_id and (
                        state.turn_id != turn_id or state.client_message_id != client_message_id))):
                    raise PiRuntimeTurnConflict("Durable resume lost its original input ownership")
        result = client.send("session.resume", identity, before_write=require_original_input)
        if (result.get("schemaVersion") != "rag-ime.pi-session-resume.v1" or result.get("runtimeEngine") != "durable"
            or result.get("accepted") is not True or not isinstance(result.get("resumed"), bool)
            or result.get("turnId") != turn_id or result.get("clientMessageId") != client_message_id):
            raise PiRuntimeError("Pi returned an invalid Durable resume receipt")
        snapshot = as_mapping(result.get("state"))
        self._validate_durable_state(session_id, snapshot, control=True)
        if result["resumed"] is False:
            settlement = as_mapping(result.get("settlement"))
            validated = self._validate_turn_settlement(settlement, session_id=session_id, turn_id=turn_id, client_message_id=client_message_id)
            self._reconcile_turn_settlement(validated)
        self._observe_durable_state(session_id, snapshot)
        return dict(result)

    def retire_recovered_turn(
        self,
        session_id: str,
        expected_turn_id: str,
    ) -> dict[str, object]:
        """Explicitly retire one interrupted durable turn after Host restart."""

        self._require_classic_control(session_id, "Classic recovered-turn retirement")

        normalized_turn_id = str(expected_turn_id).strip()
        if not normalized_turn_id:
            raise ValueError("expected recovered turn id must not be empty")
        with self._lifecycle_lock:
            if not bool(self._host_capabilities.get("sessionControlState")):
                raise PiRuntimeError(
                    "Pi Runtime Host does not support Session control state"
                )
            with self._lock:
                if session_id not in self._open_sessions:
                    raise PiRuntimeError(
                        "Pi Runtime Session must be open before recovered turn retirement"
                    )
            client = self._require_client()

            control = dict(
                client.send(
                    "session.control_state",
                    {"sessionId": session_id},
                )
            )
            if (
                control.get("schemaVersion")
                != "rag-ime.pi-session-control-state.v1"
                or control.get("sessionId") != session_id
                or not isinstance(control.get("isIdle"), bool)
            ):
                raise PiRuntimeError(
                    "Pi Runtime Host returned an invalid Session control state"
                )
            active_turn = control.get("activeTurn")
            active_turn_id = (
                str(active_turn.get("turnId") or "").strip()
                if isinstance(active_turn, Mapping)
                else ""
            )
            if (
                control.get("isIdle") is not True
                or active_turn_id != normalized_turn_id
            ):
                raise PiRuntimeError(
                    "Pi Runtime active turn does not match the expected recovered turn"
                )

            receipt = dict(
                client.send(
                    "session.abort",
                    {"sessionId": session_id},
                    timeout=1.0,
                )
            )
            lifecycle = receipt.get("lifecycle")
            lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
            pending_operations = lifecycle.get("pendingOperations")
            if (
                receipt.get("schemaVersion")
                != "rag-ime.pi-session-abort-receipt.v1"
                or receipt.get("sessionId") != session_id
                or str(receipt.get("turnId") or "") != normalized_turn_id
                or lifecycle.get("schemaVersion")
                != "pi.agent-abort-receipt.v1"
                or lifecycle.get("idle") is not True
                or lifecycle.get("drained") is not True
                or not isinstance(pending_operations, list)
                or pending_operations
            ):
                raise PiRuntimeError(
                    "Pi Runtime Host returned an invalid recovered Session abort receipt"
                )

            refreshed = dict(
                client.send(
                    "session.control_state",
                    {"sessionId": session_id},
                )
            )
            if (
                refreshed.get("schemaVersion")
                != "rag-ime.pi-session-control-state.v1"
                or refreshed.get("sessionId") != session_id
                or refreshed.get("isIdle") is not True
                or refreshed.get("activeTurn") is not None
            ):
                raise PiRuntimeError(
                    "Pi Runtime Host did not retire the recovered Session turn"
                )
            self._sync_idle_snapshot(session_id, refreshed)
            with self._lock:
                self._schedule_idle_locked()
            return {
                "schemaVersion": "rag-ime.pi-recovered-turn-retirement.v1",
                "sessionId": session_id,
                "turnId": normalized_turn_id,
                "retired": True,
                "receipt": receipt,
                "state": refreshed,
            }

    def _sync_idle_snapshot(
        self,
        session_id: str,
        snapshot: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Project Pi's run lifecycle into product status, not Host residency."""

        if self._is_durable(session_id):
            self._validate_durable_state(session_id, snapshot)
            self._observe_durable_state(session_id, snapshot)
            return self.sessions.get(session_id)

        if not bool(snapshot.get("isIdle")):
            return None
        with self._lock:
            state = self._states.get(session_id)
            if state is not None and (
                state.turn_id
                or (
                    state.prompt_admission_in_flight
                    and not state.abort_pending_admission
                )
            ):
                return None
        session = self.sessions.get(session_id)
        if str(session.get("status") or "") == "idle":
            return session
        # Pi's snapshot count includes Provider-loop and Tool protocol entries.
        # The public message snapshot owner reconciles the durable human
        # transcript count after projection; writing the Provider count here
        # caused two competing SQLite updates on every history poll.
        return self.sessions.set_status(session_id, "idle")

    def reserve_prompt_admission(
        self,
        session_id: str,
        *,
        client_message_id: str = "",
        _resident_only: bool = False,
    ) -> dict[str, object]:
        """Fence Stop before prompt preparation reaches the Pi Host.

        The application service can spend noticeable time assembling context
        before ``prompt()`` is called. Reserving that admission here gives a
        concurrent Stop request one Runtime-owned state to mark, without
        inventing a second turn or cancellation state machine.
        """

        normalized_client_message_id = str(client_message_id).strip()
        runtime_engine = "durable" if self.sessions.get(session_id).get("runtimeEngine") == "durable" else "classic"
        with self.gateway_dispatch_fence(), self._lock:
            if _resident_only and not self.is_resident_idle(session_id):
                raise PiRuntimeTurnConflict("Automatic result delivery requires an already resident idle Source")
            state = self._states.setdefault(
                session_id,
                _HostedSessionState(),
            )
            state.runtime_engine = runtime_engine
            same_reservation = (
                state.prompt_admission_in_flight
                and state.admission_client_message_id
                == normalized_client_message_id
            )
            if state.turn_id or state.compaction_target or (
                state.prompt_admission_in_flight
                and not same_reservation
            ):
                raise PiRuntimeTurnConflict(
                    "Pi 正在处理上一轮，请等待结束或停止完成后再发送"
                )
            if not same_reservation:
                state.prompt_admission_in_flight = True
                state.admission_preserve_archived = _resident_only
                state.admission_client_message_id = (
                    normalized_client_message_id
                )
                state.abort_pending_admission = False
                state.admission_abort_dispatched = False
                state.prompt_dispatched = False
                state.prompt_dispatch_signal.clear()
            self._cancel_idle_locked()
            projection = self.sessions.set_status(session_id, "busy", **(
                {"_preserve_archived": True} if state.admission_preserve_archived else {}))
            if state.admission_preserve_archived and projection.get("status") == "archived":
                state.prompt_admission_in_flight = False
                state.admission_client_message_id = ""
                state.prompt_dispatch_signal.set()
                raise PiRuntimeTurnConflict("Automatic result Source is archived")
        return {
            "reserved": True,
            "reused": same_reservation,
            "sessionId": session_id,
            "clientMessageId": normalized_client_message_id,
        }

    def require_prompt_admission_active(
        self,
        session_id: str,
        *,
        client_message_id: str = "",
    ) -> None:
        """Reject product preflight after Stop fenced this exact admission."""

        normalized_client_message_id = str(client_message_id).strip()
        with self._lock:
            state = self._states.get(session_id)
            active = (
                state is not None
                and state.prompt_admission_in_flight
                and state.admission_client_message_id
                == normalized_client_message_id
                and not state.abort_pending_admission
            )
        if active:
            return
        raise PiRuntimeCommandRejected(
            "当前消息已停止，未发送给 Pi",
            host_error_code="PROMPT_ADMISSION_CANCELLED",
        )

    def release_prompt_admission(
        self,
        session_id: str,
        *,
        client_message_id: str = "",
    ) -> bool:
        """Release an unconsumed application admission after preparation fails."""

        normalized_client_message_id = str(client_message_id).strip()
        with self._lock:
            state = self._states.get(session_id)
            if (
                state is None
                or state.turn_id
                or state.prompt_dispatched
                or not state.prompt_admission_in_flight
                or state.admission_client_message_id
                != normalized_client_message_id
            ):
                return False
            # Persist under the same identity fence. If persistence fails the
            # exact reservation remains retryable; an old release must not mark
            # a newly reserved or already dispatched Session idle.
            self.sessions.set_status(session_id, "busy" if state.compaction_target else "idle", **(
                {"_preserve_archived": True} if state.admission_preserve_archived else {}))
            state.prompt_admission_in_flight = False
            state.admission_client_message_id = ""
            state.abort_pending_admission = False
            state.admission_abort_dispatched = False
            state.prompt_dispatched = False
            state.prompt_dispatch_signal.set()
            self._schedule_idle_locked()
        return True

    def prepare_accepted_turn_projection(
        self, session_id: str, turn_id: str, *, client_message_id: str,
    ) -> Callable[[Callable[[str], None]], str]:
        """Observe the real Host, then return a local projection-only guard.

        Observe before taking a Room lock: ensure can itself consult Room
        context. Invoke the returned callback under the Room lock so the
        existing Room -> Runtime admission lock order is preserved. The guard
        holds Runtime's lock through the projection, rejects a newer admission
        or Host instance, and never sends prompt or cancellation commands.
        Its active/settled/retired value is not descendant-drain evidence.
        """
        if not session_id or not turn_id or not client_message_id:
            raise ValueError("accepted projection requires exact turn identity")
        with self._lifecycle_lock:
            observed = as_mapping(self.ensure(session_id, retire_recovered_turn=False).get("state"))
            client = self._require_client()
        if observed.get("sessionId") != session_id or not isinstance(observed.get("isIdle"), bool):
            raise PiRuntimeError("Pi returned an invalid projection control state")
        active = as_mapping(observed.get("activeTurn"))
        if active and (active.get("turnId") != turn_id or active.get("clientMessageId") != client_message_id):
            raise PiRuntimeTurnConflict("Pi projection belongs to another active turn")
        settlement = None
        candidate = as_mapping(observed.get("turnSettlement"))
        if candidate and as_mapping(candidate.get("receipt")).get("disposition") != "suspended":
            settlement = self._validate_turn_settlement(candidate, session_id=session_id,
                turn_id=turn_id, client_message_id=client_message_id)
            if not str(as_mapping(settlement.get("receipt")).get("receiptId") or ""):
                raise PiRuntimeError("Pi projection settlement has no receipt identity")
        if not active and settlement is None:
            raise PiRuntimeError("Pi has no exact active or settled turn for this projection")

        def guarded_project(project: Callable[[str], None]) -> str:
            with self._lock:
                if self._client is not client or not client.running or session_id not in self._open_sessions:
                    raise PiRuntimeTurnConflict("Pi Host changed after projection observation")
                state = self._states.setdefault(session_id, _HostedSessionState())
                if (state.turn_id and (state.turn_id != turn_id or state.client_message_id != client_message_id)) or (
                    state.prompt_admission_in_flight and state.admission_client_message_id != client_message_id
                ):
                    raise PiRuntimeTurnConflict("Pi acquired another turn or admission before projection")
                retired = turn_id in state.retired_turn_ids or (session_id, turn_id) in self._retired_host_turns
                mode = "retired" if retired else "settled" if settlement is not None else "active"
                if not retired:
                    if mode == "active":
                        # Restore only the active identity observed at Pi,
                        # fencing a direct prompt whose Room preflight passed
                        # while this accepted mapping was still missing.
                        state.turn_id = turn_id
                        state.client_message_id = client_message_id
                    if state.abort_pending_admission and state.turn_id:
                        state.abort_requested_turn_id = turn_id
                    state.prompt_admission_in_flight = False
                    state.admission_client_message_id = ""
                    state.abort_pending_admission = False
                    state.admission_abort_dispatched = False
                    state.prompt_dispatched = False
                    state.prompt_dispatch_signal.set()
                    if settlement is not None and state.turn_id:
                        self._reconcile_turn_settlement(settlement)
                    elif mode == "active":
                        self._status = "busy"
                        self._cancel_idle_locked()
                        self.sessions.set_status(session_id, "busy")
                    else:
                        # Historical receipt with no live local turn: release
                        # its exact unknown admission without re-emitting old
                        # transcript events or manufacturing a live turn.
                        self.sessions.set_status(session_id, "idle")
                        self._schedule_idle_locked()
                project(mode)
                return mode

        return guarded_project

    def prompt(
        self,
        session_id: str,
        message: str,
        *,
        images: list[Mapping[str, str]] | None = None,
        client_message_id: str = "",
        delivery: str = "prompt",
        _resident_only: bool = False,
        _before_native_write: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        if self._is_durable(session_id) and not str(client_message_id).strip():
            raise ValueError("Durable prompt requires a client message identity")
        if images and self._is_durable(session_id):
            raise PiRuntimeError("Durable Sessions do not support images")
        text = str(message).strip()
        if not text:
            raise ValueError("agent prompt must not be empty")
        normalized_delivery = message_delivery(delivery)
        public_prompt_preview = visible_message_text("user", text)
        params: dict[str, object] = {
            "sessionId": session_id,
            "message": text,
            "clientMessageId": str(client_message_id).strip(),
        }
        if images:
            params["images"] = [dict(image) for image in images]
        if normalized_delivery != "prompt":
            with self._lock:
                state = self._states.setdefault(session_id, _HostedSessionState())
                turn_id = state.turn_id
                pending_admission = state.prompt_admission_in_flight
                dispatch_signal = state.prompt_dispatch_signal
                if (
                    state.abort_requested_turn_id
                    or state.abort_pending_admission
                    or (not turn_id and not pending_admission)
                ):
                    raise PiRuntimeCommandRejected(
                        "Pi 当前没有可接收排队消息的活动回合",
                        host_error_code="SESSION_IDLE",
                    )
                self._cancel_idle_locked()
            if not turn_id:
                # The application reserves a prompt before it assembles
                # context. A Steer can therefore arrive while the product is
                # already busy but before the prompt JSONL reaches Pi. Wait for
                # that single ownership hand-off; the Host remains the source
                # of truth for whether the turn can accept the message.
                if not dispatch_signal.wait(
                    timeout=max(1.0, self.config.command_timeout_seconds)
                ):
                    raise PiRuntimeCommandRejected(
                        "Pi 仍在准备当前回合，尚不能接收排队消息",
                        host_error_code="PROMPT_ADMISSION_PENDING",
                    )
                with self._lock:
                    state = self._states.setdefault(
                        session_id,
                        _HostedSessionState(),
                    )
                    turn_id = state.turn_id
                    if (
                        state.abort_requested_turn_id
                        or state.abort_pending_admission
                        or (
                            not turn_id
                            and not state.prompt_admission_in_flight
                        )
                    ):
                        raise PiRuntimeCommandRejected(
                            "Pi 当前没有可接收排队消息的活动回合",
                            host_error_code="SESSION_IDLE",
                        )
            client = self._require_client()
            method = "session.steer" if normalized_delivery == "steer" else "session.follow_up"
            response = client.send(method, params)
            if self._is_durable(session_id):
                if (response.get("accepted") is not True or not str(response.get("turnId") or "")
                    or response.get("clientMessageId") != str(client_message_id).strip()):
                    raise PiRuntimeError("Pi returned an invalid Durable queued input receipt")
                # A follow-up is a distinct submission. It does not steal the
                # predecessor's current run/Gateway ownership at queue ACK.
                return {"accepted": True, "queued": True, "delivery": normalized_delivery,
                    "turnId": response["turnId"], "clientMessageId": response["clientMessageId"],
                    "piEntryId": str(response.get("piEntryId") or f"queue:{client_message_id}"),
                    "response": response}
            response_turn_id = str(response.get("turnId") or turn_id)
            with self._lock:
                state = self._states.setdefault(
                    session_id,
                    _HostedSessionState(),
                )
                active_turn_id = state.turn_id
                if (
                    active_turn_id
                    and response_turn_id != active_turn_id
                ):
                    raise PiRuntimeError(
                        "Pi 返回了不匹配的排队消息回合"
                    )
                if not active_turn_id and response_turn_id:
                    state.turn_id = response_turn_id
                    active_turn_id = response_turn_id
                turn_id = active_turn_id or response_turn_id
            if not turn_id:
                raise PiRuntimeError("Pi 未返回排队消息所属的活动回合")
            result: dict[str, object] = {
                "accepted": True,
                "queued": True,
                "delivery": normalized_delivery,
                "turnId": turn_id,
                "piEntryId": f"queue:{str(client_message_id).strip()}" if client_message_id else "",
                "response": response,
            }
            if client_message_id:
                result["clientMessageId"] = str(client_message_id).strip()
            return result
        with self._lock:
            resident = session_id in self._open_sessions
        if not resident:
            if _resident_only:
                raise PiRuntimeTurnConflict("Automatic result delivery cannot open a cold Source")
            self.ensure(session_id)
        client = self._require_client()
        normalized_client_message_id = str(client_message_id).strip()
        cancelled_before_dispatch = False
        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
            pre_reserved = (
                state.prompt_admission_in_flight
                and state.admission_client_message_id
                == normalized_client_message_id
            )
            if pre_reserved and state.prompt_dispatched:
                raise PiRuntimeCommandAcceptanceUnknown(
                    "Pi prompt acceptance is unresolved; inspect the exact command before retrying"
                )
            if state.turn_id or state.compaction_target or (
                state.prompt_admission_in_flight and not pre_reserved
            ):
                raise PiRuntimeTurnConflict(
                    "Pi 正在处理上一轮，请等待结束或停止完成后再发送"
                )
            self._cancel_idle_locked()
            if not pre_reserved:
                state.admission_preserve_archived = _resident_only
                state.prompt_admission_in_flight = True
                state.admission_client_message_id = (
                    normalized_client_message_id
                )
                state.abort_pending_admission = False
                state.admission_abort_dispatched = False
                state.prompt_dispatched = False
                state.prompt_dispatch_signal.clear()
            elif state.abort_pending_admission:
                state.prompt_admission_in_flight = False
                state.admission_client_message_id = ""
                state.abort_pending_admission = False
                state.admission_abort_dispatched = False
                state.prompt_dispatched = False
                state.prompt_dispatch_signal.set()
                cancelled_before_dispatch = True
            state.stream_pi_message_id = ""
            state.tool_source_loops.clear()
            state.provider_request_ids.clear()
            state.tool_blocks.clear()
            state.last_agent_messages = []
            state.final_error = ""
            state.final_failure_context.clear()
            state.provider_retry_attempt = 0
            state.provider_retry_max_attempts = 0
            state.had_tool_activity = False
            state.settle_extension_failed = False
            state.abort_requested_turn_id = ""
        if cancelled_before_dispatch:
            self.sessions.set_status(
                session_id,
                "idle",
                last_message_preview="已停止。",
                **({"_preserve_archived": True} if _resident_only else {}),
            )
            raise PiRuntimeCommandRejected(
                "当前消息已停止，未发送给 Pi",
                host_error_code="PROMPT_ADMISSION_CANCELLED",
            )
        # The browser renders Stop from its optimistic turn before the Host
        # returns a turnId. Persist the admission as busy so a concurrent Stop
        # and snapshot cannot mistake that short window for an idle Session.
        self.sessions.set_status(
            session_id,
            "busy",
            last_message_preview=public_prompt_preview,
            **({"_preserve_archived": True} if _resident_only else {}),
        )
        dispatch_attempted = False

        def mark_prompt_dispatched() -> None:
            nonlocal dispatch_attempted
            self._mark_prompt_dispatched(
                session_id,
                normalized_client_message_id,
                **({"_resident_client": client} if _resident_only else {}),
                **({"_before_native_write": _before_native_write} if _before_native_write is not None else {}),
            )
            # This callback runs under the Host client's write lock immediately
            # before its JSONL write.  Once it returns, a missing response is an
            # acceptance-unknown outcome, never proof that Pi rejected the turn.
            dispatch_attempted = True

        try:
            # A resident Session can serve a different Room responsibility on
            # its next turn. Its native execution schema was registered at
            # session.open, so refresh the current catalog while this exact
            # prompt admission still owns the Session, before any model work.
            self._sync_prompt_tool_manifest(session_id, normalized_client_message_id, client)
            # The Runtime Host resolves `session.prompt` after Pi accepts the
            # turn preflight. Stop and Steer remain responsive through the
            # Host's concurrent request dispatcher while that ACK is pending.
            accepted = client.send(
                "session.prompt",
                params,
                timeout=max(
                    _PROMPT_TIMEOUT_SECONDS,
                    self.config.command_timeout_seconds,
                ),
                before_write=mark_prompt_dispatched,
            )
        except Exception as exc:
            explicit_rejection = isinstance(exc, PiRuntimeCommandRejected)
            acceptance_unknown = dispatch_attempted and not explicit_rejection
            with self._lock:
                state = self._states.setdefault(
                    session_id,
                    _HostedSessionState(),
                )
                if (not acceptance_unknown and not state.turn_id
                    and state.prompt_admission_in_flight
                    and state.admission_client_message_id == normalized_client_message_id):
                    self.sessions.set_status(session_id, "busy" if state.compaction_target else "idle", **(
                        {"last_message_preview": "已停止。"} if isinstance(exc, PiRuntimeCommandRejected)
                        and exc.host_error_code == "PROMPT_ADMISSION_CANCELLED" else {}), **({"_preserve_archived": True} if _resident_only else {}))
                    state.prompt_admission_in_flight = False
                    state.admission_client_message_id = ""
                    state.abort_pending_admission = False
                    state.admission_abort_dispatched = False
                    state.prompt_dispatched = False
                    state.prompt_dispatch_signal.set()
            if acceptance_unknown:
                # The Host can complete the real turn before this call notices
                # that its ACK was lost.  Keep the exact admission alive until
                # a Host event binds it (or application recovery finds the
                # durable message), and never invent an empty-turn failure.
                if isinstance(exc, PiRuntimeCommandAcceptanceUnknown):
                    raise
                raise PiRuntimeCommandAcceptanceUnknown(str(exc)) from exc
            # A proven rejection has no Pi turn. Its exact admission was
            # released above; a delayed failure must not clear a newer owner.
            raise
        turn_id = str(accepted.get("turnId") or "")
        disposition = accepted.get("disposition", "started")
        if not isinstance(disposition, str) or disposition not in {"started", "queued", "handled"}:
            raise PiRuntimeCommandAcceptanceUnknown("Pi returned an unknown prompt disposition")
        handled_settlement: dict[str, object] | None = None
        if disposition == "handled":
            try:
                handled_settlement = self._validate_turn_settlement(
                    as_mapping(accepted.get("settlement")), session_id=session_id,
                    turn_id=turn_id, client_message_id=normalized_client_message_id,
                )
            except PiRuntimeError as exc:
                # The extension may already have performed an effect. A bad
                # terminal receipt is not a rejection that permits resending.
                raise PiRuntimeCommandAcceptanceUnknown(
                    "Pi handled the input but returned no matching terminal receipt"
                ) from exc
        admission_fence_error: Exception | None = None
        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
            owns_admission = (state.prompt_admission_in_flight
                and state.admission_client_message_id == normalized_client_message_id
                and self._client is client)
            newer_owner = (self._client is not client
                or (state.prompt_admission_in_flight and not owns_admission)
                or bool(state.turn_id and state.turn_id != turn_id)
                or bool(state.turn_id == turn_id and state.client_message_id
                        and state.client_message_id != normalized_client_message_id))
            abort_after_admission = owns_admission and state.abort_pending_admission
            if owns_admission:
                state.prompt_admission_in_flight = False
                state.admission_client_message_id = ""
                state.abort_pending_admission = False
                state.admission_abort_dispatched = False
                state.prompt_dispatched = False
            already_retired = (
                turn_id in state.retired_turn_ids
                or (session_id, turn_id) in self._retired_host_turns
            )
            already_aborting = state.abort_requested_turn_id == turn_id
            project_accepted = not already_retired and not newer_owner
            project_status = project_accepted
            if project_accepted:
                state.turn_id = turn_id
                state.client_message_id = normalized_client_message_id
                if abort_after_admission:
                    # ACK supplies the exact identity that pending Stop lacked.
                    # Keep authority cancelled while handing it to bound abort;
                    # a Gateway request may already have passed observation and
                    # still be waiting to INSERT its approval.
                    state.abort_requested_turn_id = turn_id
                    try:
                        self.sessions.cancel_pending_approvals(session_id, turn_id=turn_id)
                    except Exception as exc:
                        # Keep local authority closed and still deliver native
                        # cancellation below; storage failure is not settlement.
                        admission_fence_error = exc
                self._status = "busy"
                try:
                    projection = self.sessions.set_status(session_id, "busy", last_message_preview=public_prompt_preview,
                        **({"_preserve_archived": True} if _resident_only else {}))
                    if _resident_only and projection.get("status") == "archived":
                        project_status = False
                except Exception as exc:
                    if not abort_after_admission:
                        raise
                    admission_fence_error = admission_fence_error or exc
        if project_accepted:
            if project_status and not already_aborting and not abort_after_admission:
                self.events.publish(
                    session_id,
                    "status_changed",
                    {"status": "busy"},
                    turn_id=turn_id,
                )
            if abort_after_admission:
                # The original Stop request already returned immediately. Now
                # that Pi supplied the exact turn fence, deliver cancellation
                # through the ordinary Pi Session abort path. Its timer owns
                # the existing one-second escalation if the Host never settles.
                try:
                    self.abort(session_id, _expected_identity={"turnId": turn_id,
                        "clientMessageId": normalized_client_message_id})
                except Exception:
                    pass
        if admission_fence_error is not None:
            raise PiRuntimeCommandAcceptanceUnknown(
                "Pi accepted the cancelled admission, but its durable Stop fence could not be persisted"
            ) from admission_fence_error
        # A retired turn's delayed ACK is still an acceptance receipt, but
        # terminal reconciliation owns its status. It must not clear or publish
        # idle over a subsequent reservation/turn, including a pending Stop.
        result: dict[str, object] = {
            "accepted": True,
            "disposition": disposition,
            "turnId": turn_id,
            "piEntryId": turn_id,
            "response": accepted,
        }
        if handled_settlement is not None:
            # Reuse the exact terminal owner even when the live event was
            # lost, arrived before ACK, or a newer admission now owns the UI.
            self._reconcile_turn_settlement(handled_settlement)
        if (abort_after_admission or already_aborting
            or (already_retired and disposition != "handled")
            or (handled_settlement is not None
                and as_mapping(handled_settlement.get("receipt")).get("aborted") is True)):
            result["abortRequested"] = True
        if client_message_id:
            result["clientMessageId"] = str(client_message_id).strip()
        return result

    def await_turn_settled(
        self,
        session_id: str,
        turn_id: str,
        *,
        client_message_id: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Await Pi's durable settlement and reconcile the product turn once.

        Agent events remain the live projection lane. The settlement receipt is
        the terminal authority for long-running internal consumers such as
        Memory maintenance, so a lost event cannot leave their frozen request
        running after Pi has already persisted completion.
        """

        normalized_session_id = str(session_id or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        normalized_client_message_id = str(client_message_id or "").strip()
        if not normalized_session_id or not normalized_turn_id:
            raise ValueError("session_id and turn_id are required")
        if not normalized_client_message_id:
            raise ValueError("client_message_id is required")
        bounded_timeout = max(1.0, min(3_600.0, float(timeout_seconds)))
        deadline = time.monotonic() + bounded_timeout
        with self._lifecycle_lock:
            with self._lock:
                resident = (
                    normalized_session_id in self._open_sessions
                    and self._client is not None
                    and self._client.running
                )
            if not resident:
                # Pi loads durable settlements when the original transcript
                # is opened. Reading an accepted turn must not use admission's
                # idle-turn retirement: an absent receipt remains unresolved.
                self.ensure(normalized_session_id, retire_recovered_turn=False)
            client = self._require_client()
        if time.monotonic() >= deadline:
            raise PiRuntimeSettlementLookupTimeout(
                "Pi Session settlement restore timed out"
            )
        settlement_get_timeout_retries = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Pi Session turn settlement timed out")
            identity = {
                "sessionId": normalized_session_id,
                "turnId": normalized_turn_id,
                "clientMessageId": normalized_client_message_id,
            }
            try:
                current = client.send(
                    "session.settlement.get",
                    identity,
                    timeout=max(
                        0.05,
                        min(self.config.command_timeout_seconds, remaining),
                    ),
                )
            except PiRuntimeError as exc:
                lookup_timed_out = str(exc) == (
                    "Pi Runtime Host command timed out: "
                    "session.settlement.get"
                )
                if (
                    lookup_timed_out
                    and settlement_get_timeout_retries < 1
                    and time.monotonic() < deadline
                ):
                    # One idempotent lookup retry uses the exact same product
                    # turn identity.  It covers a transient Host response gap
                    # without extending the caller's deadline or replaying the
                    # model request.
                    settlement_get_timeout_retries += 1
                    continue
                if lookup_timed_out:
                    raise PiRuntimeSettlementLookupTimeout(
                        "Pi Session settlement lookup timed out"
                    ) from exc
                raise
            persisted = current.get("settlement")
            if isinstance(persisted, Mapping):
                candidate = self._validate_turn_settlement(
                    persisted,
                    session_id=normalized_session_id,
                    turn_id=normalized_turn_id,
                    client_message_id=normalized_client_message_id,
                    allow_suspended=True,
                )
                if (
                    as_mapping(candidate.get("receipt")).get("disposition")
                    != "suspended"
                ):
                    settlement = candidate
                    break
            elif persisted is not None:
                raise PiRuntimeError("Pi settlement lookup returned invalid data")
            remaining = deadline - time.monotonic()
            if remaining < 1.0:
                raise TimeoutError("Pi Session turn settlement timed out")
            response_margin = min(5.0, max(0.1, remaining - 1.0))
            host_wait_seconds = min(
                290.0,
                max(1.0, remaining - response_margin),
            )
            host_timeout_ms = int(host_wait_seconds * 1_000)
            try:
                settlement = client.send(
                    "session.await_settled",
                    {
                        **identity,
                        "allowSuspended": False,
                        "timeoutMs": host_timeout_ms,
                    },
                    timeout=min(
                        remaining,
                        host_wait_seconds + response_margin,
                    ),
                )
                break
            except PiRuntimeCommandRejected as exc:
                if (
                    exc.host_error_code == "SETTLED_TIMEOUT"
                    and time.monotonic() < deadline
                ):
                    continue
                if exc.host_error_code == "SETTLED_TIMEOUT":
                    raise TimeoutError(
                        "Pi Session turn settlement timed out"
                    ) from exc
                raise
            except PiRuntimeError as exc:
                if str(exc) == (
                    "Pi Runtime Host command timed out: "
                    "session.await_settled"
                ):
                    if time.monotonic() >= deadline:
                        raise PiRuntimeSettlementLookupTimeout(
                            "Pi Session settlement lookup timed out"
                        ) from exc
                    continue
                raise

        validated = self._validate_turn_settlement(
            settlement,
            session_id=normalized_session_id,
            turn_id=normalized_turn_id,
            client_message_id=normalized_client_message_id,
        )
        self._reconcile_turn_settlement(validated)
        return validated

    @staticmethod
    def _validate_turn_settlement(
        settlement: Mapping[str, object],
        *,
        session_id: str,
        turn_id: str,
        client_message_id: str,
        allow_suspended: bool = False,
    ) -> dict[str, object]:
        value = dict(settlement)
        if (
            value.get("schemaVersion") != "rag-ime.pi-turn-settlement.v1"
            or str(value.get("sessionId") or "") != session_id
            or str(value.get("turnId") or "") != turn_id
            or str(value.get("clientMessageId") or "") != client_message_id
        ):
            raise PiRuntimeError(
                "Pi settlement does not match the requested product turn"
            )
        runtime_session_id = str(value.get("runtimeSessionId") or "").strip()
        receipt = as_mapping(value.get("receipt"))
        if (
            not runtime_session_id
            or receipt.get("schemaVersion") != "pi.agent-settled.v2"
            or str(receipt.get("sessionId") or "") != runtime_session_id
        ):
            raise PiRuntimeError("Pi settlement receipt is invalid")
        disposition = str(receipt.get("disposition") or "")
        allowed_dispositions = {"completed", "failed", "aborted"}
        if allow_suspended:
            allowed_dispositions.add("suspended")
        if disposition not in allowed_dispositions:
            raise PiRuntimeError("Pi settlement receipt is not terminal")
        if str(receipt.get("runId") or "") != turn_id:
            raise PiRuntimeError("Pi settlement run does not match the requested turn")
        if str(receipt.get("scopeId") or "") != f"{runtime_session_id}:{turn_id}":
            raise PiRuntimeError("Pi settlement scope does not match the requested turn")
        pending_operations = receipt.get("pendingOperations")
        operations = as_mapping(receipt.get("operations"))
        operation_pending = operations.get("pending")
        if (
            not isinstance(pending_operations, int)
            or isinstance(pending_operations, bool)
            or pending_operations < 0
            or not isinstance(operation_pending, int)
            or isinstance(operation_pending, bool)
            or operation_pending < 0
            or operation_pending != pending_operations
            or (disposition != "suspended" and pending_operations != 0)
        ):
            raise PiRuntimeError("Pi settlement still owns pending operations")
        aborted = receipt.get("aborted")
        if not isinstance(aborted, bool) or aborted != (disposition == "aborted"):
            raise PiRuntimeError("Pi settlement abort state is inconsistent")
        final_message = as_mapping(receipt.get("finalMessage"))
        handled_without_run = (
            receipt.get("origin") == "prompt_preflight"
            and receipt.get("stopReason") == "prompt_handled"
            and "finalMessage" not in receipt
        )
        if (
            disposition == "completed"
            and not handled_without_run
            and (
                str(final_message.get("role") or "").lower() != "assistant"
                or not isinstance(final_message.get("content"), list)
            )
        ):
            raise PiRuntimeError(
                "completed Pi settlement has no authoritative assistant message"
            )
        return value

    def _reconcile_turn_settlement(
        self,
        settlement: Mapping[str, object],
    ) -> None:
        session_id = str(settlement.get("sessionId") or "")
        turn_id = str(settlement.get("turnId") or "")
        client_message_id = str(settlement.get("clientMessageId") or "")
        receipt = dict(as_mapping(settlement.get("receipt")))
        disposition = str(receipt.get("disposition") or "")
        final_message = as_mapping(receipt.get("finalMessage"))
        with self._lock:
            state = self._states.get(session_id)
            if (session_id, turn_id) in self._retired_host_turns or (
                state is not None and turn_id in state.retired_turn_ids
            ):
                return
            if state is None or state.turn_id != turn_id:
                # An exact, validated durable receipt outlives the in-memory
                # projection, and can be read while a later turn is active.
                # Its consumer may recover the result without recreating old
                # live events or changing that later turn's state.
                return
            prior_messages = list(state.last_agent_messages)
            messages = prior_messages or (
                [dict(final_message)] if final_message else []
            )
            state.last_agent_messages = list(messages)
            if disposition == "completed":
                state.final_error = ""
                state.final_failure_context.clear()
            if disposition == "aborted":
                state.abort_requested_turn_id = turn_id
            self._fence_retired_turn_locked(state, session_id, turn_id)

        if disposition == "failed":
            self._turn_failed(
                session_id,
                turn_id,
                PiRuntimeError(
                    str(receipt.get("stopReason") or f"Pi turn {disposition}")
                ),
            )
            return

        projected_events = self.events.replay(session_id)[0]
        has_completed_message = any(
            event.turn_id == turn_id and event.event_type == "message_completed"
            for event in projected_events
        )
        if disposition == "completed" and final_message and not has_completed_message:
            try:
                self._handle_host_event(
                    {
                        "protocolVersion": PI_HOST_PROTOCOL_VERSION,
                        "event": "agent.event",
                        "sessionId": session_id,
                        "turnId": turn_id,
                        "clientMessageId": client_message_id,
                        "payload": {
                            "type": "message_end",
                            "message": dict(final_message),
                        },
                    },
                    allow_retired_turn=True,
                )
            except Exception:
                # The durable settlement remains the terminal authority. Live
                # message projection is secondary and cannot reopen the turn.
                pass
        try:
            self._handle_host_event(
                {
                    "protocolVersion": PI_HOST_PROTOCOL_VERSION,
                    "event": "agent.event",
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "clientMessageId": client_message_id,
                    "payload": {
                        "type": "agent_settled",
                        "receipt": receipt,
                    },
                },
                allow_retired_turn=True,
            )
        except Exception:
            with self._lock:
                current = self._states.get(session_id)
                turn_was_retired = current is None or current.turn_id != turn_id
            if not turn_was_retired:
                raise

    def messages(self, session_id: str, *, _allow_host_open: bool = True) -> list[dict[str, object]]:
        return list(self.session_snapshot(session_id, _allow_host_open=_allow_host_open).get("messages") or [])

    def _persist_terminal_branch_cursor(
        self,
        session_id: str,
        binding: Mapping[str, object],
        branch_anchor: str,
    ) -> Mapping[str, object]:
        """Persist one exact settlement cursor without rotating its epoch."""

        if str(binding.get("branchAnchor") or "") == str(branch_anchor or ""):
            return binding
        updater = getattr(self.sessions, "advance_runtime_branch_cursor", None)
        if not callable(updater):
            return binding
        try:
            updated = updater(
                session_id,
                branch_anchor=str(branch_anchor),
                expected_generation=as_integer(binding.get("generation")),
                expected_external_session_id=str(
                    binding.get("externalSessionId") or ""
                ),
                expected_transcript_ref=str(
                    binding.get("transcriptRef") or ""
                ),
                expected_branch_anchor=str(binding.get("branchAnchor") or ""),
            )
        except (KeyError, OSError, ValueError, sqlite3.Error):
            return binding
        return updated if isinstance(updated, Mapping) else binding

    def _durable_history_snapshot(
        self,
        session_id: str,
    ) -> dict[str, object] | None:
        """Read an idle managed Pi transcript without opening Provider context."""

        if self._is_durable(session_id):
            return None

        try:
            session = self.sessions.get(session_id)
            binding = self.sessions.runtime_binding(session_id) or {}
            raw_path = str(
                binding.get("transcriptRef")
                or session.get("sessionFile")
                or ""
            ).strip()
            if not raw_path:
                return None
            candidate = Path(raw_path).expanduser()
            if candidate.is_symlink():
                return None
            transcript = candidate.resolve(strict=True)
            session_root = self.config.session_dir.expanduser().resolve(
                strict=False
            )
            if not path_is_within(transcript, session_root):
                return None
            stat = transcript.stat()
            if not transcript.is_file() or stat.st_size > _DURABLE_TRANSCRIPT_MAX_BYTES:
                return None
            entries: list[dict[str, object]] = []
            with transcript.open("r", encoding="utf-8") as source:
                for index, line in enumerate(source):
                    if (
                        index >= _DURABLE_TRANSCRIPT_MAX_LINES
                        or len(line) > DURABLE_TRANSCRIPT_MAX_LINE_BYTES
                    ):
                        return None
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, Mapping):
                        entries.append(dict(value))
            if not entries:
                return None
            external_session_id = str(
                binding.get("externalSessionId")
                or session.get("piSessionId")
                or ""
            )
            header = entries[0]
            if (
                str(header.get("type") or "") == "session"
                and external_session_id
                and str(header.get("id") or "") != external_session_id
            ):
                return None
            leaf_id = str(binding.get("branchAnchor") or "")
            binding_updated_at_ms = as_integer(binding.get("updatedAtMs"))
            if stat.st_mtime_ns // 1_000_000 > binding_updated_at_ms:
                terminal_leaf = latest_terminal_descendant_leaf(
                    entries,
                    ancestor_id=leaf_id,
                )
                if terminal_leaf:
                    leaf_id = terminal_leaf
                    binding = self._persist_terminal_branch_cursor(
                        session_id,
                        binding,
                        leaf_id,
                    )
                else:
                    advanced_leaf = unambiguous_descendant_leaf(
                        entries,
                        ancestor_id=leaf_id,
                    )
                    if advanced_leaf:
                        leaf_id = advanced_leaf
            if not leaf_id:
                header_id = str(header.get("id") or "")
                leaf_id = latest_terminal_descendant_leaf(
                    entries,
                    ancestor_id=header_id,
                ) or unambiguous_descendant_leaf(
                    entries,
                    ancestor_id=header_id,
                )
            messages, _selected_entries = durable_branch_messages(
                entries,
                leaf_id=leaf_id,
            )
            return {
                "sessionId": session_id,
                "piSessionId": external_session_id,
                "sessionFile": transcript.as_posix(),
                "leafId": leaf_id,
                "isIdle": True,
                "activeTurn": None,
                "messages": messages,
                "entries": entries,
                "messageQueue": {
                    "steering": [],
                    "followUp": [],
                    "steeringMode": "",
                    "followUpMode": "",
                },
            }
        except (KeyError, OSError, ValueError):
            return None

    def _recent_durable_history_messages(
        self,
        session_id: str,
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        bool,
    ] | None:
        """Read a proven recent branch window without scanning the full JSONL.

        A tail is usable only when it contains the exact selected leaf and its
        parent chain either reaches the Session header or contains one extra
        complete public turn beyond the visible window.  Anything ambiguous
        falls back to ``_durable_history_snapshot``.
        """

        if self._is_durable(session_id):
            return None

        try:
            session = self.sessions.get(session_id)
            binding = self.sessions.runtime_binding(session_id) or {}
            raw_path = str(
                binding.get("transcriptRef")
                or session.get("sessionFile")
                or ""
            ).strip()
            if not raw_path:
                return None
            candidate = Path(raw_path).expanduser()
            if candidate.is_symlink():
                return None
            transcript = candidate.resolve(strict=True)
            session_root = self.config.session_dir.expanduser().resolve(
                strict=False
            )
            if not path_is_within(transcript, session_root):
                return None
            stat = transcript.stat()
            if (
                not transcript.is_file()
                or stat.st_size > _DURABLE_TRANSCRIPT_MAX_BYTES
            ):
                return None
            tail = read_recent_transcript_tail(transcript, stat.st_size)
            if tail is None:
                return None
            header, entries = tail
            external_session_id = str(
                binding.get("externalSessionId")
                or session.get("piSessionId")
                or ""
            )
            if (
                str(header.get("type") or "") == "session"
                and external_session_id
                and str(header.get("id") or "") != external_session_id
            ):
                return None
            leaf_id = str(binding.get("branchAnchor") or "")
            binding_updated_at_ms = as_integer(binding.get("updatedAtMs"))
            if stat.st_mtime_ns // 1_000_000 > binding_updated_at_ms:
                terminal_leaf = latest_terminal_descendant_leaf(
                    entries,
                    ancestor_id=leaf_id,
                )
                if terminal_leaf:
                    leaf_id = terminal_leaf
                    binding = self._persist_terminal_branch_cursor(
                        session_id,
                        binding,
                        leaf_id,
                    )
                else:
                    advanced_leaf = unambiguous_descendant_leaf(
                        entries,
                        ancestor_id=leaf_id,
                    )
                    if advanced_leaf:
                        leaf_id = advanced_leaf
            if not leaf_id:
                header_id = str(header.get("id") or "")
                leaf_id = latest_terminal_descendant_leaf(
                    entries,
                    ancestor_id=header_id,
                ) or unambiguous_descendant_leaf(
                    entries,
                    ancestor_id=header_id,
                )
            if leaf_id and not any(
                str(entry.get("id") or "") == leaf_id
                for entry in entries
            ):
                return None
            if not leaf_id:
                return None
            return recent_messages_from_proven_tail(
                entries,
                leaf_id=leaf_id,
                header_id=str(header.get("id") or ""),
            )
        except (KeyError, OSError, ValueError):
            return None

    def _refresh_terminal_recent_projection(
        self,
        session_id: str,
        turn_id: str,
    ) -> None:
        """Advance Pi's branch cursor from the exact terminal settlement.

        A terminal event is the first point at which the current turn's
        durable branch is known.  Do not infer it from the last JSONL row: an
        append-only transcript can contain a later fork.  The settlement entry
        for this turn is itself the canonical branch anchor; refreshing the
        binding and bounded projection together prevents a first recent read
        from reusing the prior branch's window.
        """

        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return
        identity = self._recent_projection_identity(session_id)
        if identity is None:
            return
        if self._is_durable(session_id):
            return
        try:
            transcript = Path(str(identity["transcriptRef"]))
            tail = read_recent_transcript_tail(
                transcript,
                int(identity["transcriptSize"]),
            )
            if tail is None:
                return
            _header, entries = tail
            anchor = terminal_branch_anchor(
                entries,
                turn_id=normalized_turn_id,
            )
            if not anchor:
                return
            binding = self.sessions.runtime_binding(session_id)
            if binding is None:
                return
            if str(binding.get("branchAnchor") or "") != anchor:
                self.sessions.advance_runtime_branch_cursor(
                    session_id,
                    branch_anchor=anchor,
                    expected_generation=as_integer(binding.get("generation")),
                    expected_external_session_id=str(
                        binding.get("externalSessionId") or ""
                    ),
                    expected_transcript_ref=str(
                        binding.get("transcriptRef") or ""
                    ),
                    expected_branch_anchor=str(
                        binding.get("branchAnchor") or ""
                    ),
                )
                identity = self._recent_projection_identity(session_id)
                if identity is None:
                    return
            # Re-enter the bounded reader so a large transcript can return a
            # proven suffix immediately and leave any full-history repair on
            # its existing background lane.  Small/complete tails persist the
            # exact projection here; incomplete tails still align the first
            # visible window with the refreshed branch cursor.
            self.recent_session_snapshot(session_id)
        except (KeyError, OSError, ValueError, sqlite3.Error):
            # Terminal projection is an acceleration.  The durable transcript
            # remains the recovery source if the bounded refresh races a write.
            return

    def _inspection_snapshot(
        self,
        session_id: str,
        *,
        durable_fallback: bool = True,
        view: str = "",
        _allow_host_open: bool = True,
    ) -> dict[str, object]:
        """Read one Session without rebuilding context when it is resident.

        Dynamic memory and Room projections are compiled when a Session opens
        or rebinds. Historical display is instead reconstructed from Pi's
        managed durable JSONL before taking the Host lifecycle lock, so a slow
        command/context open cannot hold the conversation rail behind it.
        Internal background reads may forbid Host/Session opening entirely;
        they can only use that same JSONL reader or a resident native snapshot.
        """

        if self._is_durable(session_id):
            with self._lifecycle_lock:
                with self._lock:
                    opened = session_id in self._open_sessions
                if not opened:
                    if not _allow_host_open:
                        raise AgentRuntimeError("passive Durable history requires an already open Session")
                    self.ensure(session_id, retire_recovered_turn=False)
                snapshot = dict(self._require_client().send("session.snapshot", {"sessionId": session_id,
                    **({"view": view} if view else {})}))
                self._validate_durable_state(session_id, snapshot)
                self._validate_durable_binding(snapshot, self.sessions.runtime_binding(session_id) or {})
                self._observe_durable_state(session_id, snapshot)
                return snapshot

        with self._lock:
            active_turn = self._states.get(session_id)
            turn_id = active_turn.turn_id if active_turn is not None else ""
            already_open = session_id in self._open_sessions
        # The append-only transcript is the cheap authority for idle history,
        # including an already resident Session. Large live snapshots may need
        # to rebuild Pi's Provider context and can otherwise hold the timeline
        # blank for several seconds. A resident header-only transcript is the
        # exception: its just-settled messages may still exist only in the Host,
        # so an empty durable projection must not erase that live body.
        if durable_fallback and not turn_id:
            durable = self._durable_history_snapshot(session_id)
            if durable is not None and (
                not already_open or bool(durable.get("messages"))
            ):
                return durable

        with self._lifecycle_lock:
            with self._lock:
                already_open = session_id in self._open_sessions
            if not _allow_host_open:
                if not already_open:
                    raise AgentRuntimeError("passive Classic history is unavailable without opening its Session")
                client = self._require_client()
            else:
                client = self._host()
            if already_open:
                snapshot = dict(
                    client.send(
                        "session.snapshot",
                        {"sessionId": session_id},
                    )
                )
                self._sync_idle_snapshot(session_id, snapshot)
                with self._lock:
                    self._schedule_idle_locked()
                return snapshot
            prepared = self.ensure(session_id)
            prepared_state = prepared.get("state")
            if isinstance(prepared_state, Mapping):
                return dict(prepared_state)
            return dict(
                self._require_client().send(
                    "session.snapshot",
                    {"sessionId": session_id},
                )
            )

    def session_tool_evidence(
        self, session_id: str, *, turn_id: str, client_message_id: str = "",
    ) -> dict[str, object]:
        """Read one bound turn's durable tools without starting a model or Host.

        Callers validate task/dispatch authority before asking for this internal
        projection. Original arguments survive UI preview limits; credentials
        remain masked and private conversation/reasoning is never returned.
        """
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("tool evidence requires an exact turn")
        snapshot = self._durable_history_snapshot(session_id)
        if snapshot is None:
            raise AgentRuntimeError("durable tool evidence is unavailable")
        messages, entries = durable_branch_messages(snapshot.get("entries") or [],
            leaf_id=str(snapshot.get("leafId") or ""))
        if client_message_id and not any(
            message.get("role") == "user" and message.get(DURABLE_TURN_ID_KEY) == turn_id
            and message.get("clientMessageId") == client_message_id for message in messages
        ):
            raise AgentRuntimeError("durable tool evidence dispatch binding does not match")
        events = durable_tool_history_events(messages, session_id=session_id, raw_entries=entries,
            maximum_tools=None, maximum_public_chars=None, evidence_turn_id=turn_id,
            runtime_session_id=(str(snapshot.get("piSessionId") or "") if snapshot.get("runtimeEngine") == "durable" else ""))
        return {"sessionId": session_id, "turnId": turn_id, "toolHistoryEvents": [
            event for event in events if event.get("eventType") in {"tool_started", "tool_finished"}]}

    def session_snapshot(self, session_id: str, *, _view: str = "", _allow_host_open: bool = True) -> dict[str, object]:
        session = self.sessions.get(session_id)
        binding = self.sessions.runtime_binding(session_id)
        durable_engine = session.get("runtimeEngine") == "durable"
        if session.get("evaluationSnapshot") is True:
            # Imported evaluation transcripts are immutable evidence.  Reading
            # one must never start, resume, or rebind a Provider Runtime; the
            # exact PAW-managed JSONL copy is the sole snapshot authority.
            snapshot = self._durable_history_snapshot(session_id)
            if snapshot is None:
                raise AgentRuntimeError(
                    "evaluation snapshot transcript is unavailable"
                )
        else:
            try:
                snapshot = self._inspection_snapshot(session_id, _allow_host_open=_allow_host_open,
                    **({"view": _view} if durable_engine and _view else {}))
            except AgentRuntimeError:
                if durable_engine:
                    raise
                # A transient Host failure is not evidence that the Session has no
                # history. The append-only Pi transcript remains readable even when
                # Provider context inspection is unavailable; use it as the
                # recovery source instead of publishing a successful empty
                # snapshot that would make the API and UI erase the conversation.
                snapshot = self._durable_history_snapshot(session_id)
                if snapshot is None:
                    raise
        raw_messages = snapshot.get("messages") if isinstance(snapshot.get("messages"), list) else []
        raw_entries = (snapshot.get("entries") if isinstance(snapshot.get("entries"), list) else []) if not durable_engine else []
        durable_messages, durable_entries = durable_branch_messages(
            raw_entries,
            leaf_id=str(snapshot.get("leafId") or ""),
        )
        # `snapshot.messages` is Pi's current Provider context.  After
        # compaction or branch restoration it may omit older human messages,
        # even though the durable entries still contain the selected branch.
        # The conversation UI must project that durable branch rather than
        # turning a populated Session into the welcome screen.
        projection_messages = durable_messages or raw_messages
        projection_entries = durable_entries or raw_entries
        tool_history_events = durable_tool_history_events(
            projection_messages,
            session_id=session_id,
            raw_entries=projection_entries,
            runtime_session_id=(str(snapshot.get("piSessionId") or "") if durable_engine else ""),
            # This is the durable transcript projection, not the bounded live
            # replay tail. Keep every historical thinking/Tool row visible;
            # each individual result is still passed through the existing
            # redaction and local inspector bounds. The UI may virtualize or
            # paginate this canonical list, but a refresh must not silently
            # erase older activity identities.
            maximum_tools=None,
            maximum_public_chars=None,
        )
        entry_timestamps = history_entry_timestamps(projection_entries)
        entry_ordinals = history_entry_ordinals(projection_entries)
        result: list[dict[str, object]] = []
        current_turn_id = ""
        last_assistant_fingerprint: tuple[str, str] | None = None
        durable_assistant_counts = durable_public_assistant_counts(
            projection_entries,
            session_id=session_id,
        )
        emitted_assistant_counts: dict[str, int] = {}
        for raw in projection_messages:
            if not isinstance(raw, Mapping) or not pi_message_is_public(raw):
                continue
            # Pi's message timestamp is Provider/request time and can move
            # backwards when a user/Steer entry is appended after a replayed
            # response. The JSONL entry timestamp is the authoritative append
            # order used by the restored timeline.
            timestamp_queue = entry_timestamps.get(
                history_message_fingerprint(raw)
            )
            ordinal_queue = entry_ordinals.get(
                history_message_fingerprint(raw)
            )
            timeline_raw = dict(raw)
            if timestamp_queue:
                timeline_raw["timestamp"] = timestamp_queue.popleft()
            timeline_sequence = ordinal_queue.popleft() if ordinal_queue else None
            role = str(raw.get("role") or "assistant").lower()
            message_id = pi_message_id(raw, "history")
            if durable_engine and raw.get(DURABLE_TURN_ID_KEY):
                current_turn_id = str(raw[DURABLE_TURN_ID_KEY])
            if (
                role == "user"
                and not pi_message_continues_public_turn(raw)
            ) or not current_turn_id:
                current_turn_id = str(
                    raw.get(DURABLE_TURN_ID_KEY) or f"history:{message_id}"
                )
                last_assistant_fingerprint = None
            payload = pi_message_payload(
                timeline_raw,
                session_id=session_id,
                turn_id=current_turn_id,
                media_resolver=self._media_resolver,
                message_id=message_id,
            ).to_payload()
            if role == "assistant":
                if timeline_sequence is not None:
                    # One durable assistant entry can contain public reasoning,
                    # Tool calls/results and the final text.  Fractional event
                    # receipts occupy .1-.8; the final body is the last item in
                    # that append entry, immediately before the next JSONL row.
                    payload["timelineSequence"] = float(timeline_sequence) + 0.9
                projection_fingerprint = assistant_projection_fingerprint(
                    payload
                )
                durable_count = durable_assistant_counts.get(
                    projection_fingerprint,
                    0,
                )
                emitted_count = emitted_assistant_counts.get(
                    projection_fingerprint,
                    0,
                )
                # session.messages can replay a previously settled assistant
                # object after a native follow-up. The durable transcript is
                # authoritative for how many semantic copies really exist.
                # This removes a replay even when it crosses a new user turn,
                # while preserving two intentionally identical replies when
                # the transcript contains both.
                if durable_count and emitted_count >= durable_count:
                    continue
                fingerprint = (
                    current_turn_id,
                    projection_fingerprint,
                )
                # Pi may transiently project the same settled assistant object
                # twice after a native follow-up. Its JSONL transcript contains
                # one message, so collapse only an adjacent exact duplicate in
                # the same user turn. Identical replies in later turns remain.
                if not durable_engine and fingerprint == last_assistant_fingerprint:
                    continue
                last_assistant_fingerprint = fingerprint
                emitted_assistant_counts[projection_fingerprint] = (
                    emitted_count + 1
                )
            elif timeline_sequence is not None:
                payload["timelineSequence"] = timeline_sequence
            result.append(
                payload
            )
        result = self._restore_aborted_history_messages(session_id, result)
        telemetry = snapshot.get("telemetry")
        raw_queue = as_mapping(snapshot.get("messageQueue"))
        message_queue = {
            "steering": public_message_queue(raw_queue.get("steering")),
            "followUp": public_message_queue(raw_queue.get("followUp")),
            "steeringMode": str(raw_queue.get("steeringMode") or ""),
            "followUpMode": str(raw_queue.get("followUpMode") or ""),
        }
        effective_codemode_mode = self._effective_codemode_mode(
            session,
            binding=binding,
            snapshot=snapshot,
        )
        return {
            "messages": result,
            "toolHistoryEvents": tool_history_events,
            "telemetry": dict(telemetry) if isinstance(telemetry, Mapping) else None,
            "messageQueue": message_queue,
            **({"runtimeEngine": "durable", "recoverable": snapshot.get("recoverable") is True,
                "paused": snapshot.get("paused") is True,
                "activeTurn": dict(as_mapping(snapshot.get("activeTurn"))) or None,
                "compactionTarget": (validate_compaction_target(snapshot["compactionTarget"])
                                     if snapshot.get("compactionTarget") is not None else None),
                "engineCapabilities": dict(as_mapping(snapshot.get("engineCapabilities"))),
                "partial": snapshot.get("partial") is True,
                "historyCursor": snapshot.get("historyCursor"),
                "projectionCurrent": snapshot.get("projectionCurrent") is True} if durable_engine else {}),
            **self._codemode_payload(effective_codemode_mode),
        }

    def recent_session_snapshot(self, session_id: str) -> dict[str, object]:
        """Project a bounded durable first paint without contacting Pi Host.

        The append-only transcript is the only authority used here. Missing,
        untrusted, malformed, or oversized transcript state therefore yields
        an honestly empty window; this read never falls through to the live
        Host or the full historical Tool-event reconstruction.
        """

        if self._is_durable(session_id):
            # Native committed history is the authority. No local JSONL or
            # main-file fingerprint is valid for SQLite/WAL state.
            return self.session_snapshot(session_id, _view="recent")

        session = self.sessions.get(session_id)
        binding = self.sessions.runtime_binding(session_id)
        effective_codemode_mode = self._effective_codemode_mode(
            session,
            binding=binding,
        )
        projection_identity = self._recent_projection_identity(session_id)
        cached_projection = self._recent_projected_messages(
            session_id,
            projection_identity,
        )
        recent_candidate = None
        cache_identity_changed = False
        if cached_projection is not None:
            cached_messages, cached_tool_history, exact = cached_projection
            if exact:
                # Even an exact cache can be stale when a prior repair saved
                # the old branch against the post-append file identity. The
                # bounded reader is the only cheap way to notice a durable
                # terminal settlement before trusting that cache.
                try:
                    binding = self.sessions.runtime_binding(session_id) or {}
                    probe_terminal = (
                        int(projection_identity.get("transcriptMtimeNs") or 0)
                        // 1_000_000
                        > as_integer(binding.get("updatedAtMs"))
                    )
                except (KeyError, OSError, ValueError, sqlite3.Error):
                    probe_terminal = False
                if not probe_terminal:
                    return {
                        "messages": cached_messages,
                        "toolHistoryEvents": cached_tool_history,
                        "projectionCurrent": True,
                        **self._codemode_payload(effective_codemode_mode),
                    }
                recent_candidate = self._recent_durable_history_messages(session_id)
                refreshed_identity = self._recent_projection_identity(session_id)
                if refreshed_identity == projection_identity:
                    return {
                        "messages": cached_messages,
                        "toolHistoryEvents": cached_tool_history,
                        "projectionCurrent": True,
                        **self._codemode_payload(effective_codemode_mode),
                    }
                projection_identity = refreshed_identity
                cache_identity_changed = True
            # A stale cache may predate a terminal settlement. Give the
            # bounded tail reader one chance to recover that exact cursor
            # before returning the old provisional window. Ordinary appends
            # keep the existing non-blocking cache path.
            if not cache_identity_changed:
                if recent_candidate is None:
                    recent_candidate = self._recent_durable_history_messages(session_id)
                refreshed_identity = self._recent_projection_identity(session_id)
            else:
                refreshed_identity = projection_identity
            if not cache_identity_changed and refreshed_identity == projection_identity:
                self._schedule_recent_projection_refresh(
                    session_id,
                    projection_identity,
                )
                return {
                    "messages": cached_messages,
                    "toolHistoryEvents": cached_tool_history,
                    "projectionCurrent": False,
                    **self._codemode_payload(effective_codemode_mode),
                }
            projection_identity = refreshed_identity

        if recent_candidate is None:
            recent_candidate = self._recent_durable_history_messages(session_id)
            projection_identity = self._recent_projection_identity(session_id)
        if recent_candidate is None:
            durable = self._durable_history_snapshot(session_id)
            projection_identity = self._recent_projection_identity(session_id)
            if durable is None:
                return {"messages": [], "projectionCurrent": False}
            raw_messages = durable.get("messages")
            if not isinstance(raw_messages, list):
                return {"messages": [], "projectionCurrent": False}
            raw_entries = (
                list(durable.get("entries") or [])
                if isinstance(durable.get("entries"), list)
                else []
            )
        else:
            raw_messages, raw_entries, complete_window = recent_candidate
            if not complete_window:
                messages = recent_public_message_window(
                    raw_messages,
                    session_id=session_id,
                    media_resolver=self._media_resolver,
                    raw_entries=raw_entries,
                )
                messages = self._restore_aborted_history_messages(session_id, messages)
                tool_history_events = recent_tool_history_events(
                    raw_messages,
                    raw_entries=raw_entries,
                    projected_messages=messages,
                    session_id=session_id,
                )
                self._schedule_recent_projection_refresh(
                    session_id,
                    projection_identity,
                )
                return {
                    "messages": messages,
                    "toolHistoryEvents": tool_history_events,
                    "projectionCurrent": bool(projection_identity is not None and projection_identity == self._recent_projection_identity(session_id)),
                    **self._codemode_payload(effective_codemode_mode),
                }
        messages = recent_public_message_window(
            raw_messages,
            session_id=session_id,
            media_resolver=self._media_resolver,
            raw_entries=raw_entries,
        )
        messages = self._restore_aborted_history_messages(session_id, messages)
        tool_history_events = recent_tool_history_events(
            raw_messages,
            raw_entries=raw_entries,
            projected_messages=messages,
            session_id=session_id,
        )
        self._save_recent_message_projection(
            session_id,
            projection_identity,
            messages,
            tool_history_events,
        )
        return {
            "messages": messages,
            "toolHistoryEvents": tool_history_events,
            "projectionCurrent": bool(projection_identity is not None and projection_identity == self._recent_projection_identity(session_id)),
            **self._codemode_payload(effective_codemode_mode),
        }

    def _recent_projection_identity(
        self,
        session_id: str,
    ) -> dict[str, object] | None:
        if self._is_durable(session_id):
            return None
        """Resolve the exact immutable file view that may reuse a projection."""

        try:
            session = self.sessions.get(session_id)
            binding = self.sessions.runtime_binding(session_id) or {}
            raw_path = str(
                binding.get("transcriptRef")
                or session.get("sessionFile")
                or ""
            ).strip()
            if not raw_path:
                return None
            candidate = Path(raw_path).expanduser()
            if candidate.is_symlink():
                return None
            transcript = candidate.resolve(strict=True)
            session_root = self.config.session_dir.expanduser().resolve(
                strict=False
            )
            if not path_is_within(transcript, session_root):
                return None
            stat = transcript.stat()
            if (
                not transcript.is_file()
                or stat.st_size > _DURABLE_TRANSCRIPT_MAX_BYTES
            ):
                return None
            return {
                "transcriptRef": transcript.as_posix(),
                "externalSessionId": str(
                    binding.get("externalSessionId")
                    or session.get("piSessionId")
                    or ""
                ),
                "branchAnchor": str(binding.get("branchAnchor") or ""),
                "transcriptDevice": int(stat.st_dev),
                "transcriptInode": int(stat.st_ino),
                "transcriptSize": int(stat.st_size),
                "transcriptMtimeNs": int(stat.st_mtime_ns),
                "transcriptBoundarySha256": transcript_boundary_sha256(
                    transcript,
                    int(stat.st_size),
                ),
            }
        except (KeyError, OSError, ValueError):
            return None

    def _restore_aborted_history_messages(self, session_id: str, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        """Project exact Stop receipts without rewriting Pi's committed history.

        A cancelled turn can contain only its user input and completed Tool
        receipts: Pi need not commit a final assistant message. Idle snapshots
        intentionally omit old streaming events, so retain the product's exact
        terminal as a display receipt. This says nothing about process drain.
        """
        reader = getattr(self.sessions, "runtime_turn_terminal_event", None)
        if not callable(reader):
            return messages
        terminals: dict[str, object] = {}
        last_positions: dict[str, int] = {}
        aborted_assistants: set[str] = set()
        for index, message in enumerate(messages):
            turn_id = str(message.get("turnId") or "")
            if (not turn_id or message.get("role") not in {"user", "assistant"}
                or message.get("sessionId") not in {None, session_id}):
                continue
            last_positions[turn_id] = index
            if message.get("role") == "assistant" and message.get("status") == "aborted":
                aborted_assistants.add(turn_id)
        restored = []
        for index, message in enumerate(messages):
            turn_id = str(message.get("turnId") or "")
            if turn_id not in last_positions or message.get("sessionId") not in {None, session_id}:
                restored.append(message)
                continue
            if turn_id not in terminals:
                try:
                    terminals[turn_id] = reader(session_id, turn_id)
                except (KeyError, OSError, ValueError, sqlite3.Error):
                    terminals[turn_id] = None
            terminal = terminals[turn_id]
            exact_stop = (isinstance(terminal, Mapping) and terminal.get("sessionId") == session_id
                and terminal.get("turnId") == turn_id and terminal.get("eventType") == "turn_completed"
                and terminal.get("status") == "aborted")
            if not exact_stop:
                restored.append(message)
                continue
            if message.get("role") == "assistant" and message.get("status") == "failed":
                blocks = [dict(block) for block in message.get("blocks", []) if isinstance(block, Mapping)
                          and str(block.get("id") or "") not in {f"{turn_id}:failure-text:0", f"{turn_id}:error:0"}]
                if not any(block.get("type") == "text" for block in blocks):
                    blocks.append({"id": f"{turn_id}:aborted:0", "type": "text", "status": "aborted",
                                   "presentationKind": "markdown", "data": {"text": "已停止。"}})
                restored.append({**message, "status": "aborted", "blocks": blocks})
                aborted_assistants.add(turn_id)
            else:
                restored.append(message)
            if index == last_positions[turn_id] and turn_id not in aborted_assistants:
                timestamp = int(terminal.get("createdAtMs") or message.get("createdAtMs") or 0)
                restored.append({"schemaVersion": "rag-ime.agent-message.v1", "id": f"paw-stop:{turn_id}",
                    "sessionId": session_id, "turnId": turn_id, "role": "assistant", "status": "aborted",
                    "blocks": [{"id": f"{turn_id}:stop-receipt:0", "type": "text", "status": "aborted",
                                "presentationKind": "markdown", "data": {"text": "已停止。"}}],
                    "attachments": [], "citations": [], "createdAtMs": timestamp, "completedAtMs": timestamp})
                aborted_assistants.add(turn_id)
        return restored

    def _recent_projected_messages(
        self,
        session_id: str,
        identity: Mapping[str, object] | None,
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        bool,
    ] | None:
        if identity is None:
            return None
        reader = getattr(self.sessions, "recent_message_projection", None)
        if not callable(reader):
            return None
        try:
            projection = reader(session_id)
        except Exception:
            return None
        if not isinstance(projection, Mapping):
            return None
        stable_identity_keys = (
            "transcriptRef",
            "externalSessionId",
            "branchAnchor",
            "transcriptDevice",
            "transcriptInode",
        )
        if any(
            projection.get(key) != identity.get(key)
            for key in stable_identity_keys
        ):
            return None
        projected_size = as_integer(projection.get("transcriptSize"))
        current_size = as_integer(identity.get("transcriptSize"))
        projected_mtime_ns = as_integer(projection.get("transcriptMtimeNs"))
        current_mtime_ns = as_integer(identity.get("transcriptMtimeNs"))
        exact = (
            projected_size == current_size
            and projected_mtime_ns == current_mtime_ns
            and projection.get("transcriptBoundarySha256")
            == identity.get("transcriptBoundarySha256")
        )
        monotonic_append = (
            current_size > projected_size
            and current_mtime_ns >= projected_mtime_ns
            and str(projection.get("transcriptBoundarySha256") or "")
            == transcript_boundary_sha256(
                Path(str(identity["transcriptRef"])),
                projected_size,
            )
        )
        if not exact and not monotonic_append:
            return None
        messages = projection.get("messages")
        if not isinstance(messages, list) or not all(
            isinstance(message, Mapping)
            for message in messages
        ):
            return None
        tool_history_events = projection.get("toolHistoryEvents")
        if not isinstance(tool_history_events, list) or not all(
            isinstance(event, Mapping)
            for event in tool_history_events
        ):
            return None
        return (
            self._restore_aborted_history_messages(session_id, [dict(message) for message in messages]),
            [dict(event) for event in tool_history_events],
            exact,
        )

    def _schedule_recent_projection_refresh(
        self,
        session_id: str,
        identity: Mapping[str, object] | None,
    ) -> None:
        if identity is None:
            return
        with self._lock:
            if session_id in self._recent_projection_refreshes:
                return
            self._recent_projection_refreshes.add(session_id)
        worker = threading.Thread(
            target=self._refresh_recent_message_projection,
            args=(session_id, dict(identity)),
            name=f"pi-recent-projection-{session_id[-12:]}",
            daemon=True,
        )
        with self._lock:
            self._recent_projection_threads.add(worker)
        try:
            worker.start()
        except Exception:
            with self._lock:
                self._recent_projection_refreshes.discard(session_id)
                self._recent_projection_threads.discard(worker)

    def _refresh_recent_message_projection(
        self,
        session_id: str,
        identity: Mapping[str, object],
    ) -> None:
        saved = False
        try:
            recent_candidate = self._recent_durable_history_messages(session_id)
            raw_messages = (
                recent_candidate[0]
                if recent_candidate is not None and recent_candidate[2]
                else None
            )
            raw_entries = (
                recent_candidate[1]
                if recent_candidate is not None and recent_candidate[2]
                else []
            )
            if raw_messages is None:
                durable = self._durable_history_snapshot(session_id)
                if durable is None:
                    return
                raw_messages = durable.get("messages")
                if not isinstance(raw_messages, list):
                    return
                raw_entries = (
                    list(durable.get("entries") or [])
                    if isinstance(durable.get("entries"), list)
                    else []
                )
            messages = recent_public_message_window(
                raw_messages,
                session_id=session_id,
                media_resolver=self._media_resolver,
                raw_entries=raw_entries,
            )
            tool_history_events = recent_tool_history_events(
                raw_messages,
                raw_entries=raw_entries,
                projected_messages=messages,
                session_id=session_id,
            )
            saved = self._save_recent_message_projection(
                session_id,
                identity,
                messages,
                tool_history_events,
            )
        except Exception:
            # A repair is secondary to the already-returned recent window.
            # The next read may retry; never leak a daemon traceback or turn a
            # cache failure into a Runtime failure.
            saved = False
        finally:
            with self._lock:
                self._recent_projection_refreshes.discard(session_id)
                self._recent_projection_threads.discard(
                    threading.current_thread()
                )
        if not saved:
            return
        try:
            self.events.publish(
                session_id,
                "snapshot_required",
                {"reason": "recent_projection_refreshed"},
                turn_id="",
            )
        except Exception:
            return

    def _save_recent_message_projection(
        self,
        session_id: str,
        identity: Mapping[str, object] | None,
        messages: list[dict[str, object]],
        tool_history_events: list[dict[str, object]],
    ) -> bool:
        if identity is None:
            return False
        writer = getattr(self.sessions, "save_recent_message_projection", None)
        if not callable(writer):
            return False
        refreshed = self._recent_projection_identity(session_id)
        if refreshed != identity:
            return False
        try:
            writer(
                session_id,
                transcript_ref=str(identity["transcriptRef"]),
                external_session_id=str(identity["externalSessionId"]),
                branch_anchor=str(identity["branchAnchor"]),
                transcript_device=int(identity["transcriptDevice"]),
                transcript_inode=int(identity["transcriptInode"]),
                transcript_size=int(identity["transcriptSize"]),
                transcript_mtime_ns=int(identity["transcriptMtimeNs"]),
                transcript_boundary_sha256=str(
                    identity["transcriptBoundarySha256"]
                ),
                messages=messages,
                tool_history_events=tool_history_events,
            )
            return True
        except Exception:
            # The projection is an acceleration only. Its failure cannot make
            # the canonical Pi transcript unavailable to the conversation UI.
            return False

    def debug_context(self, session_id: str, turn_id: str = "") -> dict[str, object]:
        with self._lock:
            already_open = session_id in self._open_sessions
        if not already_open:
            # Context inspection is observational. Reopening an idle historical
            # Session here can restore a large Pi transcript, block the local
            # control server, and evict an actively used Session. The UI can
            # truthfully report that raw Provider context is unavailable until
            # the Session is resident again.
            return {
                "schemaVersion": "rag-ime.pi-debug-context-response.v1",
                "sessionId": session_id,
                "turnId": str(turn_id or "").strip(),
                "available": False,
                "transient": True,
                "context": None,
                "telemetry": None,
                "reason": "session_not_resident",
            }
        params: dict[str, object] = {"sessionId": session_id}
        if str(turn_id).strip():
            params["turnId"] = str(turn_id).strip()
        try:
            result = self._require_client().send(
                "session.debug.context",
                params,
                # Context inspection is optional observability. It must never
                # inherit the ordinary command timeout and hold a control
                # request open while a large or unhealthy Session is resident.
                timeout=min(1.0, max(0.1, self.config.command_timeout_seconds)),
            )
        except PiRuntimeError as exc:
            if "timed out: session.debug.context" not in str(exc):
                raise
            return {
                "schemaVersion": "rag-ime.pi-debug-context-response.v1",
                "sessionId": session_id,
                "turnId": str(turn_id or "").strip(),
                "available": False,
                "transient": True,
                "context": None,
                "telemetry": None,
                "reason": "runtime_unresponsive",
            }
        return dict(result)

    def rewind_session(self, session_id: str, *, entry_id: str) -> dict[str, object]:
        self._require_classic_control(session_id, "conversation rewrite")
        normalized_entry_id = str(entry_id or "").strip()
        if not normalized_entry_id:
            raise ValueError("conversation rewrite entryId must not be empty")
        with self._lifecycle_lock:
            self._require_idle_fork_session(session_id)
            try:
                self.ensure(session_id)
                client = self._require_client()
                with self._lock:
                    self._require_quiescent_fork_locked(session_id)
                    self._cancel_idle_locked()
                candidates = self._fork_candidates_from_result(
                    client.send("session.fork.candidates", {"sessionId": session_id})
                )
                selected = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate["entryId"] == normalized_entry_id
                        and candidate["role"] == "user"
                    ),
                    None,
                )
                if selected is None:
                    raise PiRuntimeError(
                        "conversation rewrite entry must identify a public user message"
                    )
                response = client.send(
                    "session.rewind",
                    {"sessionId": session_id, "entryId": normalized_entry_id},
                )
                with self._lock:
                    self._schedule_idle_locked()
                return {
                    "entryId": normalized_entry_id,
                    "editorText": str(response.get("editorText") or selected["text"]),
                    "leafId": str(response.get("leafId") or ""),
                }
            finally:
                if str(self.sessions.get(session_id).get("status") or "") == "active":
                    self.sessions.set_status(session_id, "idle")

    def fork_candidates(self, session_id: str) -> list[dict[str, object]]:
        self._require_classic_control(session_id, "conversation fork")
        with self._lifecycle_lock:
            self._require_idle_fork_session(session_id)
            try:
                self.ensure(session_id)
                with self._lock:
                    self._require_quiescent_fork_locked(session_id)
                result = self._require_client().send(
                    "session.fork.candidates",
                    {"sessionId": session_id},
                )
                with self._lock:
                    self._schedule_idle_locked()
                return self._fork_candidates_from_result(result)
            finally:
                if str(self.sessions.get(session_id).get("status") or "") == "active":
                    self.sessions.set_status(session_id, "idle")

    def fork_session(
        self,
        source_session_id: str,
        target_session_id: str,
        *,
        entry_id: str,
    ) -> dict[str, object]:
        self._require_classic_control(source_session_id, "conversation fork")
        self._require_classic_control(target_session_id, "conversation fork")
        normalized_entry_id = str(entry_id or "").strip()
        if not normalized_entry_id:
            raise ValueError("conversation fork entryId must not be empty")
        with self._lifecycle_lock:
            self._require_idle_fork_session(source_session_id)
            target = self.sessions.get(target_session_id)
            if str(target.get("status") or "") != "idle":
                raise PiRuntimeError("conversation fork target must be idle")
            if self.sessions.runtime_binding(target_session_id) is not None:
                raise PiRuntimeError("conversation fork target is already bound")
            self.ensure(source_session_id)
            source_binding = self.sessions.runtime_binding(source_session_id)
            if not isinstance(source_binding, Mapping):
                raise PiRuntimeError("conversation fork source has no runtime binding")
            source_transcript = str(source_binding.get("transcriptRef") or "").strip()
            client = self._require_client()
            try:
                with self._lock:
                    self._require_quiescent_fork_locked(source_session_id)
                    self._cancel_idle_locked()
                candidates = self._fork_candidates_from_result(
                    client.send("session.fork.candidates", {"sessionId": source_session_id})
                )
            except Exception:
                self.sessions.set_status(source_session_id, "idle")
                self.sessions.set_status(target_session_id, "idle")
                with self._lock:
                    self._schedule_idle_locked()
                raise
            selected = next(
                (candidate for candidate in candidates if candidate["entryId"] == normalized_entry_id),
                None,
            )
            if selected is None:
                self.sessions.set_status(source_session_id, "idle")
                with self._lock:
                    self._schedule_idle_locked()
                raise PiRuntimeError("conversation fork entry is not available in the source Session")

            opened_target = False
            branch_transcript: Path | None = None
            branch_cleanup_safe = False
            try:
                forked = client.send(
                    "session.fork",
                    {
                        "sessionId": source_session_id,
                        "targetSessionId": target_session_id,
                        "entryId": normalized_entry_id,
                        "nativeMcpExecutionAllowed": self._native_mcp_execution_policy(target),
                    },
                    timeout=max(60.0, self.config.command_timeout_seconds),
                )
                opened_target = True
                if str(forked.get("sourceSessionId") or "") != source_session_id:
                    raise PiRuntimeError("Pi returned a mismatched conversation fork source")
                if str(forked.get("targetSessionId") or "") != target_session_id:
                    raise PiRuntimeError("Pi returned a mismatched conversation fork target")
                snapshot = dict(as_mapping(forked.get("snapshot")))
                external_session_id = str(snapshot.get("piSessionId") or "").strip()
                transcript_ref = str(snapshot.get("sessionFile") or "").strip()
                if not external_session_id or not transcript_ref:
                    raise PiRuntimeError("Pi returned an incomplete conversation fork identity")
                branch_candidate = Path(transcript_ref).expanduser()
                if branch_candidate.is_symlink():
                    raise PiRuntimeError("Pi conversation fork file must not be a symlink")
                branch_transcript = branch_candidate.resolve(strict=False)
                session_root = self.config.session_dir.expanduser().resolve(strict=False)
                if not path_is_within(branch_transcript, session_root):
                    raise PiRuntimeError("Pi conversation fork file is outside the managed session directory")
                source_path = Path(source_transcript).expanduser().resolve(strict=False) if source_transcript else None
                if source_path is not None and branch_transcript == source_path:
                    raise PiRuntimeError("Pi conversation fork reused the source transcript")
                if external_session_id == str(source_binding.get("externalSessionId") or ""):
                    raise PiRuntimeError("Pi conversation fork reused the source runtime identity")
                branch_cleanup_safe = True

                bound = self.sessions.bind_runtime_session(
                    target_session_id,
                    driver_id=self.driver_id,
                    runtime_kind=self.runtime_kind,
                    external_session_id=external_session_id,
                    transcript_ref=branch_transcript.as_posix(),
                    branch_anchor=str(forked.get("branchAnchor") or normalized_entry_id),
                    binding_state="active",
                    metadata={
                        "protocolVersion": PI_HOST_PROTOCOL_VERSION,
                        "forkedFromSessionId": source_session_id,
                        "forkEntryId": normalized_entry_id,
                    },
                    message_count=len(snapshot.get("messages") or []),
                )
                evicted = str(forked.get("evictedSessionId") or "")
                with self._lock:
                    self._open_sessions.add(target_session_id)
                    self._states.setdefault(target_session_id, _HostedSessionState())
                    if evicted:
                        self._open_sessions.discard(evicted)
                        evicted_state = self._states.pop(evicted, None)
                        if evicted_state is not None:
                            if evicted_state.abort_timer is not None:
                                evicted_state.abort_timer.cancel()
                            if evicted_state.settle_timer is not None:
                                evicted_state.settle_timer.cancel()
                    self._status = "ready"
                    self._schedule_idle_locked()
                self.sessions.set_status(source_session_id, "idle")
                bound = self.sessions.set_status(target_session_id, "idle")
                self.events.publish(
                    target_session_id,
                    "session_configuration_changed",
                    {
                        "kind": "fork",
                        "sourceSessionId": source_session_id,
                        "entryId": normalized_entry_id,
                    },
                )
                return {
                    "sourceSessionId": source_session_id,
                    "targetSessionId": target_session_id,
                    "entryId": normalized_entry_id,
                    # The host response can contain the raw transport prompt.
                    # Restore only the public text confirmed by the catalog.
                    "selectedText": str(selected["text"]) if selected["role"] == "user" else "",
                    "state": snapshot,
                    "session": bound,
                }
            except Exception:
                if opened_target:
                    try:
                        client.send("session.close", {"sessionId": target_session_id})
                    except AgentRuntimeError:
                        pass
                    with self._lock:
                        self._open_sessions.discard(target_session_id)
                        target_state = self._states.pop(target_session_id, None)
                        if target_state is not None:
                            if target_state.abort_timer is not None:
                                target_state.abort_timer.cancel()
                            if target_state.settle_timer is not None:
                                target_state.settle_timer.cancel()
                if (
                    branch_transcript is not None
                    and branch_cleanup_safe
                    and branch_transcript.is_file()
                    and not branch_transcript.is_symlink()
                ):
                    try:
                        branch_transcript.unlink()
                    except OSError:
                        pass
                self.sessions.set_status(source_session_id, "idle")
                self.sessions.set_status(target_session_id, "idle")
                with self._lock:
                    self._schedule_idle_locked()
                raise

    def _require_idle_fork_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if str(session.get("status") or "") not in {"idle", "active"}:
            raise PiRuntimeError("conversation forks are only available for idle Sessions")

    def _require_quiescent_fork_locked(self, session_id: str) -> None:
        state = self._states.get(session_id)
        if state is None:
            return
        if state.turn_id:
            raise PiRuntimeError("conversation forks are unavailable during an Agent turn")
        if state.pending_approvals or state.pending_reviews or state.pending_ui_requests:
            raise PiRuntimeError("conversation forks are unavailable while user input is pending")

    @staticmethod
    def _fork_candidates_from_result(result: Mapping[str, object]) -> list[dict[str, object]]:
        raw_items = result.get("items")
        if not isinstance(raw_items, list):
            raise PiRuntimeError("Pi returned an invalid conversation fork catalog")
        candidates: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in raw_items[:500]:
            if not isinstance(raw, Mapping):
                continue
            entry_id = str(raw.get("entryId") or "").strip()[:240]
            role = str(raw.get("role") or "").strip().lower()
            created_at_ms = as_integer(raw.get("createdAtMs"))
            text = public_fork_candidate_text(raw.get("text"), role=role)
            if (
                not entry_id
                or role not in {"user", "assistant"}
                or not text
                or entry_id in seen
            ):
                continue
            seen.add(entry_id)
            candidates.append(
                {
                    "entryId": entry_id,
                    "text": text,
                    "role": role,
                    "createdAtMs": max(0, created_at_ms),
                }
            )
        return candidates

    def command_catalog(self, session_id: str) -> list[dict[str, object]]:
        if self._is_durable(session_id):
            return []
        # Catalog inspection must not rebuild generic RAG/memory context for an
        # already resident Session. That projection is needed when opening or
        # rebinding a Provider turn, not when listing slash commands.
        self._inspection_snapshot(session_id, durable_fallback=False)
        response = self._require_client().send("session.commands", {"sessionId": session_id})
        raw_commands = response.get("commands")
        if not isinstance(raw_commands, list):
            return []
        commands: list[dict[str, object]] = []
        seen: set[str] = set()
        for value in raw_commands[:200]:
            if not isinstance(value, Mapping):
                continue
            source = str(value.get("source") or "").strip()
            name = str(value.get("name") or "").strip()
            if source not in {"extension", "prompt", "skill"}:
                continue
            if not re.fullmatch(r"[\w][\w.:-]{0,79}", name, flags=re.UNICODE):
                continue
            identity = name.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            commands.append(
                {
                    "name": name,
                    "invocation": f"/{name}",
                    "description": " ".join(str(value.get("description") or "").split())[:240],
                    "source": source,
                }
            )
        return commands

    def native_capabilities(self, session_id: str) -> dict[str, object]:
        """Inspect the resident Pi owner, without a Provider turn or MCP reconnect."""
        if self._is_durable(session_id):
            return {"schemaVersion": "rag-ime.pi-native-capabilities.v1", "sessionId": session_id,
                "runtimeEngine": "durable", "codemodeMode": None,
                "mcp": {"available": False, "active": False, "configErrorCount": 0, "servers": []}, "tools": []}
        self._inspection_snapshot(session_id, durable_fallback=False)
        response = self._require_client().send("tools.list", {"sessionId": session_id})
        raw = response.get("nativeCapabilities")
        if not isinstance(raw, Mapping) or raw.get("schemaVersion") != "rag-ime.pi-native-capabilities.v1":
            raise PiRuntimeError("Pi native capability inspection is unavailable")
        mcp = raw.get("mcp")
        if not isinstance(mcp, Mapping) or mcp.get("available") is not True:
            raise PiRuntimeError("Pi native MCP owner is unavailable")
        states = {"starting", "disabled", "connecting", "connected", "disconnected", "needs-auth", "failed", "closed"}
        exposures = {"direct", "codemode", "deferred", "hidden"}
        raw_servers = mcp.get("servers")
        raw_tools = raw.get("tools")
        if not isinstance(raw_servers, list) or not isinstance(raw_tools, list):
            raise PiRuntimeError("Pi returned invalid native capabilities")
        servers = []
        for value in raw_servers:
            if not isinstance(value, Mapping) or value.get("state") not in states or value.get("exposure") not in exposures:
                raise PiRuntimeError("Pi returned invalid MCP state")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", str(value.get("name") or "")):
                raise PiRuntimeError("Pi returned invalid MCP server identity")
            servers.append({
                "name": str(value.get("name") or "")[:200],
                "namespace": str(value.get("namespace") or "")[:240],
                "scope": str(value.get("scope") or "")[:40],
                "enabled": value.get("enabled") is True,
                "state": value["state"], "exposure": value["exposure"],
                **{key: max(0, as_integer(value.get(key))) for key in ("toolCount", "resourceCount", "resourceTemplateCount")},
            })
        tools = []
        for value in raw_tools:
            if not isinstance(value, Mapping) or value.get("exposure") not in exposures:
                raise PiRuntimeError("Pi returned invalid native tool catalog")
            namespace = value.get("namespace")
            if not isinstance(namespace, Mapping):
                continue
            parameters = value.get("parameters")
            if not isinstance(parameters, Mapping):
                raise PiRuntimeError("Pi returned invalid native tool parameters")
            tools.append({
                "name": str(value.get("name") or "")[:240],
                "namespace": {"name": str(namespace.get("name") or "")[:240]},
                "description": redact_mapping({"text": str(value.get("description") or "")}).get("text", ""),
                "parameters": redact_mapping(parameters),
                "exposure": value["exposure"], "active": value.get("active") is True,
                "routable": value.get("routable") is True,
            })
        mode = raw.get("codemodeMode")
        return {"schemaVersion": "rag-ime.pi-native-capabilities.v1", "sessionId": session_id,
                "codemodeMode": mode if mode in {"on", "only", "off"} else None,
                "mcp": {"available": True, "active": mcp.get("active") is True,
                        "configErrorCount": max(0, as_integer(mcp.get("configErrorCount"))), "servers": servers},
                "tools": tools}

    def skill_catalog(self, session_id: str) -> list[dict[str, object]]:
        """Read the effective Pi Skill loader without projecting transcript history.

        Opening an idle Session loads its frozen resource policy, but this read
        never starts a turn or retires a recovered one. The Host's command list
        is built from that Session's actual resource loader after Skill routing.
        """
        if self._is_durable(session_id):
            return []
        self.ensure(session_id, retire_recovered_turn=False)
        response = self._require_client().send(
            "session.commands", {"sessionId": session_id}
        )
        raw_commands = response.get("commands")
        if not isinstance(raw_commands, list) or len(raw_commands) > 512:
            raise PiRuntimeError("Pi returned an invalid Session Skill catalog")
        skills: list[dict[str, object]] = []
        seen: set[str] = set()
        for value in raw_commands:
            if not isinstance(value, Mapping) or value.get("source") != "skill":
                continue
            name = str(value.get("name") or "")
            if not re.fullmatch(r"skill:[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
                continue
            if name in seen:
                raise PiRuntimeError("Pi returned duplicate Session Skills")
            seen.add(name)
            skills.append({"name": name, "source": "skill"})
        return skills

    def invoke_command(self, session_id: str, command: str) -> dict[str, object]:
        self._require_classic_control(session_id, "slash commands")
        text = str(command).strip()
        if not text.startswith("/") or "\n" in text or "\r" in text:
            raise ValueError("Pi Package command must be one slash-command line")
        if text.split()[0] == "/mcp" and not native_mcp_execution_allowed(self.sessions.get(session_id)):
            raise PiRuntimeError("Native MCP is denied by this Session's execution policy")
        self._inspection_snapshot(session_id, durable_fallback=False)
        response = self._require_client().send(
            "session.command.invoke",
            {"sessionId": session_id, "command": text},
        )
        if response.get("schemaVersion") != "rag-ime.pi-package-command-invocation.v1":
            raise PiRuntimeError("Pi returned an invalid Package command receipt")
        if response.get("handled") is not True:
            raise PiRuntimeError("Pi did not handle the Package command")
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise PiRuntimeError("Pi Package command receipt has no result")
        return {
            "schemaVersion": "rag-ime.pi-package-command-invocation.v1",
            "command": text,
            "name": str(response.get("name") or "")[:80],
            "handled": True,
            "result": dict(result),
            "leafId": str(response.get("leafId") or "")[:240],
        }

    def model_catalog(self, session_id: str) -> dict[str, object]:
        # Model selection and thinking level are persisted after every Pi-owned
        # change. Reading that desired Session state avoids opening a large
        # transcript merely to render the picker; Pi still owns and supplies
        # the live capability catalog below.
        session = self.sessions.get(session_id)
        models = self.available_models()
        engine_capabilities = None
        if session.get("runtimeEngine") == "durable":
            snapshot = as_mapping(self.ensure(session_id, retire_recovered_turn=False).get("state"))
            engine_capabilities = dict(as_mapping(snapshot.get("engineCapabilities")))
        provider, model_id = self.config.resolved_model_reference(session)
        selected = next(
            (
                dict(model)
                for model in models
                if model.get("provider") == provider
                and model.get("id") == model_id
            ),
            None,
        )
        return {
            "selected": selected,
            "models": models,
            "thinkingLevel": effective_thinking_level(
                session.get("thinkingLevel"),
                selected or {},
            ),
            "runtimeEngine": str(session.get("runtimeEngine") or "classic"),
            **({"engineCapabilities": engine_capabilities} if engine_capabilities is not None else {}),
        }

    def available_models(self) -> list[dict[str, object]]:
        # Opening the Agent page requests roles and the selected Session model
        # concurrently. Pi's catalog refresh may perform Provider discovery, so
        # coalesce those reads and keep one short-lived Pi-confirmed snapshot.
        # This cache never selects a model or fabricates capabilities.
        # Provider discovery is not a Host lifecycle transition. Holding the
        # lifecycle lock across ``models.list`` lets an optional picker refresh
        # block Session open/prompt admission for the full Provider timeout.
        # Host creation remains lifecycle-fenced; only catalog singleflight has
        # its own lock after the shared Host has been admitted.
        with self._model_catalog_lock:
            now = time.monotonic()
            with self._lock:
                if (
                    self._available_models_cache_ready
                    and now - self._available_models_cached_at
                    < _MODEL_CATALOG_CACHE_SECONDS
                ):
                    return [
                        dict(model)
                        for model in self._available_models_cache
                    ]
            try:
                catalog = self._host().send(
                    "models.list",
                    timeout=max(30.0, self.config.command_timeout_seconds),
                )
            except Exception:
                # A previously Pi-confirmed catalog is safer and more useful
                # than turning a transient Provider-discovery failure into an
                # empty picker. It never changes the selected Session model;
                # a process with no confirmed cache still fails explicitly.
                with self._lock:
                    if self._available_models_cache_ready:
                        return [
                            dict(model)
                            for model in self._available_models_cache
                        ]
                raise
            models = [
                model
                for value in catalog.get("models") or []
                if isinstance(value, Mapping)
                for model in [public_pi_model(value)]
                if model
            ]
            models.sort(
                key=lambda item: (
                    str(item["provider"]).lower(),
                    str(item["name"]).lower(),
                )
            )
            with self._lock:
                self._available_models_cache = tuple(
                    dict(model) for model in models
                )
                self._available_models_cached_at = time.monotonic()
                self._available_models_cache_ready = True
            return [dict(model) for model in models]

    def classify_once(
        self,
        *,
        request_id: str,
        state: Mapping[str, object],
        questions: Mapping[str, object],
        api_key: str = "",
        endpoint: str = "",
        timeout_seconds: float = 12.0,
        cancellation_event: threading.Event | None = None,
        on_settled: Callable[[], None] | None = None,
    ) -> dict[str, object] | None:
        """Use Pi's native TypeSafe classifier without creating an Agent turn.

        None means the negotiated Host lacks this operation, before any
        classification was sent. Every outcome after dispatch, including an
        unknown answer, stays on that original native request's path.
        Credentials travel only over the private Host pipe, never public state.
        """
        try:
            if cancellation_event is not None and cancellation_event.is_set():
                raise PiRuntimeCommandRejected("Pi classification cancelled before admission")
            identity = model_reference_part(request_id, field="requestId", maximum=200)
            if not isinstance(state, Mapping) or not isinstance(questions, Mapping) or not questions:
                raise ValueError("classification requires an object state and nonempty questions")
            if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                    or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
                raise ValueError("classification timeout must be finite and positive")
            bounded_timeout = max(1.0, min(300.0, float(timeout_seconds)))
            params: dict[str, object] = {
                "requestId": identity, "state": dict(state), "questions": dict(questions),
                "timeoutMs": int(bounded_timeout * 1000),
            }
            # Validate JSON before admission, without truncating the user's state.
            json.dumps(params, allow_nan=False)
            if api_key:
                params["apiKey"] = api_key
            if endpoint:
                params["endpoint"] = endpoint
            with self._lifecycle_lock:
                client = self._host()
                with self._lock:
                    if self._host_capabilities.get("statelessClassification") is not True:
                        unsupported = True
                    else:
                        unsupported = False
                    if not unsupported:
                        if identity in self._classifications:
                            raise PiRuntimeError("Pi classification request is already active")
                        self._cancel_idle_locked()
                        call = _ClassificationCall(client, on_settled=on_settled)
                        self._classifications[identity] = call
                        params["dispatchId"] = call.dispatch_id
                        self._status = "busy"
        except Exception:
            self._notify_classification_settled(on_settled)
            raise
        if unsupported:
            # No native scope was created. Its caller may still enter the
            # compatible direct adapter and owns that adapter's settlement.
            return None

        def before_write() -> None:
            # Runs under the pipe write lock: cancellation either prevents
            # admission or follows the once command on that same ordered pipe.
            with self._lock:
                if (call.cancel_requested or self._classifications.get(identity) is not call
                        or (cancellation_event is not None and cancellation_event.is_set())):
                    raise PiRuntimeCommandRejected("Pi classification cancelled before dispatch")
                call.dispatched = True

        settled = False
        try:
            result = client.send("classification.once", params, timeout=bounded_timeout + 5.0,
                                 before_write=before_write)
            if (result.get("requestId") != identity
                    or result.get("dispatchId") != call.dispatch_id
                    or result.get("stopReason") not in {"stop", "error", "aborted"}):
                raise PiRuntimeCommandAcceptanceUnknown("Pi classification returned an unbound receipt")
            settled = True
            return result
        except PiRuntimeCommandRejected:
            settled = True
            raise
        except PiRuntimeCommandAcceptanceUnknown:
            # An uncertain answer permits cancellation of the original call,
            # never fallback or replay. A signal is not proof of drain.
            try:
                self._abort_classification(identity, call)
            except Exception:
                with self._lock:
                    if self._client is client:
                        self._last_error = "Pi classification answer and cancellation remain unconfirmed"
            raise
        finally:
            if settled or not call.dispatched or not client.running:
                self._release_classification(identity, call)

    def _release_classification(self, identity: str, call: _ClassificationCall) -> None:
        with self._lock:
            if self._classifications.get(identity) is not call:
                return
            del self._classifications[identity]
            if (self._client is call.client and call.client.running
                    and not self._classifications and not self._active_completion_ids
                    and not any(item.turn_id or item.prompt_admission_in_flight for item in self._states.values())):
                self._status = "ready"
                self._schedule_idle_locked()
        self._notify_classification_settled(call.on_settled)

    @staticmethod
    def _notify_classification_settled(callback: Callable[[], None] | None) -> None:
        if callback is not None:
            try:
                callback()
            except Exception:
                # A consumer projection cannot prevent the Runtime settling.
                pass

    def _abort_classification(self, identity: str, call: _ClassificationCall) -> bool:
        result = call.client.send("classification.abort", {"requestId": identity, "dispatchId": call.dispatch_id},
                                  timeout=min(5.0, max(1.0, self.config.command_timeout_seconds)))
        if result.get("requestId") != identity or result.get("dispatchId") != call.dispatch_id:
            return False
        if result.get("drained") is True:
            self._release_classification(identity, call)
        return result.get("aborted") is True

    def cancel_classification(self, request_id: str) -> bool:
        """Cancel exactly one call; a signal alone never releases its Host."""
        try:
            identity = model_reference_part(request_id, field="requestId", maximum=200)
        except ValueError:
            return False
        with self._lock:
            call = self._classifications.get(identity)
            if call is None:
                return False
            call.cancel_requested = True
            if not call.dispatched:
                return True
        if not call.client.running:
            self._release_classification(identity, call)
            return False
        return self._abort_classification(identity, call)

    def complete_once(
        self,
        *,
        request_id: str,
        provider: str,
        model_id: str,
        thinking_level: str,
        message: str,
        on_text_delta: Callable[[str], None] | None = None,
        timeout_seconds: float = 120.0,
    ) -> dict[str, object]:
        normalized_request_id = model_reference_part(
            request_id,
            field="requestId",
            maximum=200,
        )
        normalized_provider = model_reference_part(provider, field="provider", maximum=80)
        normalized_model = model_reference_part(model_id, field="modelId", maximum=160)
        normalized_thinking = str(thinking_level or "").strip().lower()
        if normalized_thinking not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("stateless Pi completion received an unsupported thinking level")
        normalized_message = str(message or "").strip()
        if not normalized_message:
            raise ValueError("stateless Pi completion message is required")
        bounded_timeout = max(1.0, min(300.0, float(timeout_seconds)))
        params: dict[str, object] = {
            "requestId": normalized_request_id,
            "provider": normalized_provider,
            "modelId": normalized_model,
            "thinkingLevel": normalized_thinking,
            "message": normalized_message[:64_000],
            "timeoutMs": int(bounded_timeout * 1000),
        }
        # Responses can overtake the secondary event-projection lane. Keep a
        # request-local stream fence so the authoritative result can deliver
        # its remaining suffix before cleanup, without draining unrelated
        # Session events or blocking the stdout response reader.
        stream_lock = threading.RLock()
        streamed: list[str] = []
        stream_finished = False

        def deliver_delta(delta: str) -> None:
            with stream_lock:
                if stream_finished:
                    return
                streamed.append(delta)
                if on_text_delta is not None:
                    on_text_delta(delta)

        with self._lifecycle_lock:
            client = self._host()
            with self._lock:
                if normalized_request_id in self._active_completion_ids:
                    raise PiRuntimeError("Pi stateless completion request is already active")
                self._cancel_idle_locked()
                self._active_completion_ids.add(normalized_request_id)
                if on_text_delta is not None:
                    self._completion_sinks[normalized_request_id] = deliver_delta
                self._status = "busy"
        try:
            result = client.send(
                "completion.once",
                params,
                timeout=bounded_timeout + 5.0,
            )
            with stream_lock:
                stream_finished = True
                prefix = "".join(streamed)
                final_text = str(result.get("text") or "")
                if on_text_delta is not None and final_text.startswith(prefix):
                    suffix = final_text[len(prefix):]
                    if suffix:
                        try:
                            on_text_delta(suffix)
                        except Exception:
                            pass
            return result
        except PiRuntimeError as exc:
            if str(exc) == "Pi Runtime Host command timed out: completion.once":
                self._cancel_timed_out_completion(
                    client,
                    request_id=normalized_request_id,
                    error=exc,
                )
            raise
        finally:
            with stream_lock:
                stream_finished = True
            with self._lock:
                self._active_completion_ids.discard(normalized_request_id)
                self._completion_sinks.pop(normalized_request_id, None)
                if (
                    self._client is client
                    and client.running
                    and not any(state.turn_id for state in self._states.values())
                ):
                    self._status = "ready"
                    self._schedule_idle_locked()

    def _cancel_timed_out_completion(
        self,
        client: PiRuntimeHostClient,
        *,
        request_id: str,
        error: PiRuntimeError,
    ) -> None:
        """Cancel one stateless completion without killing resident Sessions.

        ``completion.once`` is used by approval and other bounded model lanes.
        It shares the Host process with durable Room and conversation Sessions,
        but the Runtime Host dispatches ``completion.cancel`` concurrently and
        owns an AbortController per request.  A one-shot timeout therefore must
        stay inside that request's cancellation domain: killing the Host here
        would turn one slow arbiter into simultaneous, unrelated Session loss.

        A cancel RPC failure is retained as runtime diagnostics.  It still does
        not authorize a process-wide kill; an actually unhealthy Session will
        reach its own typed timeout/cancellation boundary independently.
        """

        message = redact_runtime_text(str(error))
        cancel_error = ""
        try:
            client.send(
                "completion.cancel",
                {"requestId": request_id},
                timeout=min(5.0, max(1.0, self.config.command_timeout_seconds)),
            )
        except Exception as exc:
            cancel_error = redact_runtime_text(str(exc))
        with self._lock:
            if self._client is client:
                self._last_error = (
                    message
                    if not cancel_error
                    else f"{message}; completion cancel failed: {cancel_error}"
                )

    def _retire_timed_out_host(
        self,
        client: PiRuntimeHostClient,
        *,
        requested_by: str,
        error: PiRuntimeError,
    ) -> None:
        """Fence a Host that stopped answering before an RPC boundary.

        A timed-out RPC has no trustworthy completion boundary: the Host may
        still emit a late response after the caller has returned. Reusing it
        also leaves its durable process row registered, so the next request
        either hangs behind the same process or cannot admit a replacement.
        The cancellation kill gate gives this failure a durable receipt;
        stopping the client then drains its reader threads and lets the normal
        Host-exit path fault resident Sessions.
        """

        message = redact_runtime_text(str(error))
        with self._lifecycle_lock:
            with self._lock:
                if self._client is not client:
                    return
                self._status = "stopping"
                self._last_error = message
            receipt: dict[str, object] | None = None
            kill_error = ""
            try:
                receipt = self._kill_gate.request_kill(
                    client.host_identity,
                    request_kind="cancel_timeout",
                    requested_by=requested_by,
                    reason=message,
                    now_ms=int(time.time() * 1000),
                )
            except Exception as exc:  # pragma: no cover - defensive local cleanup
                kill_error = redact_runtime_text(str(exc))
            finally:
                # request_kill is bounded and may return while the process is
                # only acknowledged. stop() completes the local teardown and
                # marks both the process row and any receipt terminal.
                client.stop()
            with self._lock:
                if receipt is not None:
                    self._last_kill_receipt = dict(
                        self._kill_gate.receipt(str(receipt["killReceiptId"]))
                    )
                self._status = "faulted"
                self._last_error = (
                    message
                    if not kill_error
                    else f"{message}; Runtime Host kill receipt failed: {kill_error}"
                )

    def cancel_completion(self, request_id: str) -> bool:
        try:
            normalized = model_reference_part(request_id, field="requestId", maximum=200)
        except ValueError:
            return False
        with self._lock:
            if normalized not in self._active_completion_ids:
                return False
            client = self._client
        if client is None or not client.running:
            return False
        result = client.send(
            "completion.cancel",
            {"requestId": normalized},
            timeout=min(5.0, max(1.0, self.config.command_timeout_seconds)),
        )
        return result.get("cancelled") is True

    def set_model(
        self,
        session_id: str,
        *,
        provider: str,
        model_id: str,
        max_tokens: int | None = None,
    ) -> dict[str, object]:
        normalized_provider = model_reference_part(provider, field="provider", maximum=80)
        normalized_model = model_reference_part(model_id, field="modelId", maximum=160)
        if max_tokens is not None and (
            isinstance(max_tokens, bool)
            or int(max_tokens) < 16
            or int(max_tokens) > 262_144
        ):
            raise ValueError("Pi model output budget must be between 16 and 262144")
        self.ensure(session_id)
        params: dict[str, object] = {
            "sessionId": session_id,
            "provider": normalized_provider,
            "modelId": normalized_model,
        }
        if max_tokens is not None:
            params["maxTokens"] = int(max_tokens)
        result = self._require_client().send("session.model.set", params)
        # Durable returns its updated Session snapshot, while Classic returns
        # the selected model directly. Keep the engine-specific wire shapes
        # explicit rather than treating malformed responses as a fallback.
        model = as_mapping(result.get("model")) if self._is_durable(session_id) else result
        selected = public_pi_model(model)
        if not selected:
            raise PiRuntimeError("Pi did not return the selected model")
        session = self.sessions.set_model_profile(session_id, f"{selected['provider']}/{selected['id']}")
        return {"selected": selected, "session": session}

    def set_thinking_level(self, session_id: str, *, level: str) -> dict[str, object]:
        normalized = str(level or "").strip().lower()
        if normalized not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("unsupported Pi thinking level")
        self.ensure(session_id)
        result = self._require_client().send(
            "session.thinking.set",
            {"sessionId": session_id, "level": normalized},
        )
        effective = str(result.get("level") or normalized)
        self.sessions.set_thinking_level(session_id, effective)
        return {"thinkingLevel": effective}

    def set_codemode_mode(
        self,
        session_id: str,
        *,
        mode: str,
    ) -> dict[str, object]:
        """Change native codemode only after proving the Session is idle.

        Re-opening an already resident Pi Session does not apply new open
        options. The dedicated Host mutation is therefore the only warm-path
        setter, and its idle fence prevents a preference change from
        restarting or altering an active paid turn.
        """

        self._require_classic_control(session_id, "Code Mode")

        requested = normalize_codemode_mode(mode)
        prepared = self.ensure(
            session_id,
            retire_recovered_turn=False,
            _sync_codemode=False,
        )
        return self._set_codemode_mode_after_ensure(
            session_id,
            requested,
            state=as_mapping(prepared.get("state")),
        )

    def _set_codemode_mode_after_ensure(
        self,
        session_id: str,
        requested: str,
        *,
        state: Mapping[str, object],
        binding: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Apply one validated idle codemode mutation without re-opening Pi."""

        if state.get("isIdle") is not True or as_mapping(state.get("activeTurn")):
            raise PiRuntimeTurnConflict(
                "Pi codemode mode can only change while the Session is idle"
            )
        capability = codemode_capability(
            self._host_capabilities.get("codemode")
        )
        if capability.get("available") is not True:
            raise PiRuntimeError(
                "Pi Runtime Host does not support native codemode"
            )
        modes = {
            str(value).strip().lower()
            for value in capability.get("modes") or []
            if str(value).strip().lower() in CODEMODE_MODES
        }
        if requested not in modes:
            raise PiRuntimeError(
                f"Pi Runtime Host does not support codemode mode: {requested}"
            )
        response = self._require_client().send(
            "session.codemode.set",
            {"sessionId": session_id, "mode": requested},
        )
        response_snapshot = as_mapping(response.get("snapshot"))
        response_mode = response.get("mode")
        if response_mode is None:
            response_mode = response.get("codemodeMode")
        if response_mode is None:
            response_mode = response_snapshot.get("codemodeMode")
        try:
            effective = normalize_codemode_mode(response_mode, default=requested)
        except ValueError as exc:
            raise PiRuntimeError(
                "Pi Runtime Host returned an invalid codemode mode"
            ) from exc
        if effective not in modes:
            raise PiRuntimeError(
                "Pi Runtime Host returned an unsupported codemode mode"
            )
        if response_snapshot:
            state = dict(response_snapshot)
        else:
            state = dict(state)
        state["codemodeMode"] = effective

        binding = binding or self.sessions.runtime_binding(session_id)
        if binding is None:
            raise PiRuntimeError(
                "Pi codemode changed without a durable runtime binding"
            )
        metadata = dict(as_mapping(binding.get("metadata")))
        metadata["codemodeAvailable"] = True
        metadata["codemodeMode"] = effective
        updater = getattr(self.sessions, "update_runtime_binding_metadata", None)
        if not callable(updater):
            raise PiRuntimeError(
                "Agent Session store cannot persist codemode preferences"
            )
        try:
            updated_binding = updater(
                session_id,
                metadata,
                expected_generation=as_integer(binding.get("generation")),
                expected_external_session_id=str(
                    binding.get("externalSessionId") or ""
                ),
                expected_transcript_ref=str(binding.get("transcriptRef") or ""),
                expected_branch_anchor=str(binding.get("branchAnchor") or ""),
            )
        except (KeyError, OSError, ValueError, sqlite3.Error) as exc:
            raise PiRuntimeError(
                "Pi codemode changed but the preference could not be persisted"
            ) from exc
        return {
            "sessionId": session_id,
            "codemodeMode": effective,
            "capability": capability,
            "state": state,
            "binding": dict(updated_binding),
            "session": self.sessions.get(session_id),
        }

    def tool_catalog(self, session_id: str) -> list[dict[str, object]]:
        session = dict(self.sessions.get(session_id))
        if self._tool_manifest_provider is None:
            return []
        return [dict(item) for item in self._tool_manifest_provider(session)]

    def _native_mcp_execution_policy(self, session: Mapping[str, object]) -> bool:
        if session.get("runtimeEngine") == "durable":
            return False
        allowed = native_mcp_execution_allowed(session)
        if not allowed and self._host_capabilities.get("nativeMcpExecutionPolicy") is not True:
            raise PiRuntimeError(
                "Pi Runtime Host cannot enforce this Session's native MCP policy; update the managed Runtime"
            )
        return allowed

    def _sync_prompt_tool_manifest(
        self, session_id: str, client_message_id: str, client: PiRuntimeHostClient,
    ) -> None:
        def require_exact_admission() -> None:
            with self._lock:
                if self._client is not client:
                    raise PiRuntimeCommandRejected("Pi Host changed during tool preparation",
                        host_error_code="TOOL_MANIFEST_SYNC_FAILED")
                self.require_prompt_admission_active(session_id, client_message_id=client_message_id)

        try:
            require_exact_admission()
            session = self.sessions.get(session_id)
            tools = ([] if session.get("toolProfileVersion") == MEMORY_CURATION_TOOL_PROFILE
                     else self.tool_catalog(session_id))
            # Manifest construction may consult Room state. Do it outside the
            # Runtime lock, then recheck at the actual JSONL write boundary.
            response = client.send("tools.sync", {
                "sessionId": session_id,
                "tools": tools,
                "nativeMcpExecutionAllowed": self._native_mcp_execution_policy(session),
            },
                before_write=require_exact_admission)
            if not isinstance(response.get("tools"), list):
                raise PiRuntimeError("Pi returned an invalid tool synchronization receipt")
            require_exact_admission()
        except Exception as exc:
            if isinstance(exc, PiRuntimeCommandRejected) and exc.host_error_code == "PROMPT_ADMISSION_CANCELLED":
                raise
            # Even a lost tools.sync ACK cannot have sent this prompt. Report
            # that proven boundary instead of quarantining it as model work.
            raise PiRuntimeCommandRejected("Pi tool manifest refresh failed before prompt: " + str(exc),
                host_error_code="TOOL_MANIFEST_SYNC_FAILED") from exc

    def _mark_prompt_dispatched(
        self,
        session_id: str,
        admission_client_message_id: str,
        *, _resident_client: PiRuntimeHostClient | None = None,
        _before_native_write: Callable[[], None] | None = None,
    ) -> None:
        """Atomically fence Stop against the Host JSONL hand-off.

        ``PiRuntimeHostClient`` invokes this callback while its JSONL write
        lock is held. A background abort therefore cannot overtake the prompt
        record. If Stop won the state lock, raising here prevents the prompt
        record from being written. If this callback wins, a later Stop sees
        ``prompt_dispatched`` and follows Pi's native abort path.
        """
        with self.gateway_dispatch_fence(), self._lock:
            if _resident_client is not None and (self._client is not _resident_client
                or not _resident_client.running or session_id not in self._open_sessions):
                raise PiRuntimeCommandRejected("Automatic result Source is no longer resident",
                                               host_error_code="SOURCE_NOT_RESIDENT")
            state = self._states.setdefault(session_id, _HostedSessionState())
            if (
                state.turn_id or not state.prompt_admission_in_flight
                or state.admission_client_message_id != admission_client_message_id
                or state.abort_pending_admission
            ):
                raise PiRuntimeCommandRejected(
                    "当前消息已停止，未发送给 Pi",
                    host_error_code="PROMPT_ADMISSION_CANCELLED",
                )
            if _before_native_write is not None:
                _before_native_write()
            state.prompt_dispatched = True
            state.prompt_dispatch_signal.set()

    def _deliver_pending_admission_abort(
        self,
        session_id: str,
        admission_client_message_id: str,
    ) -> None:
        """Use Pi's native abort while prompt preflight ACK is still pending."""

        try:
            if self._host_capabilities.get("sessionBoundAbort") is not True:
                raise PiRuntimeError("Pi Runtime Host cannot bind Stop to its original admission; update the managed Runtime")
            target: dict[str, object] = {"sessionId": session_id}
            if admission_client_message_id:
                target["expectedClientMessageId"] = admission_client_message_id
            else:
                turn_id = self.sessions.cancelled_gateway_admission_turn(session_id, "")
                if not turn_id:
                    raise PiRuntimeError("pending Room Stop needs its exact accepted turn before cancellation")
                target.update(expectedTurnId=turn_id, clientMessageId="")
            self._require_client().send(
                "session.abort",
                target,
                timeout=1.0,
            )
        except Exception as exc:
            with self._lock:
                state = self._states.get(session_id)
                if (state is None or not state.prompt_admission_in_flight
                    or state.admission_client_message_id != admission_client_message_id):
                    return
                state.admission_abort_dispatched = False
                if isinstance(exc, PiRuntimeCommandRejected) and exc.host_error_code == "ABORT_TARGET_MISMATCH":
                    return
                self.events.publish(session_id, "status_changed", {
                    "status": "aborting", "pendingAdmission": True, "escalated": True,
                })

    def abort_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        client_message_id: str,
        cancel_id: str,
        lookup_only: bool = False,
        recover_retired_only: bool = False,
        recover_interrupted_only: bool = False,
    ) -> dict[str, object]:
        """Compare-and-cancel at the actual Host owner, or query that command.

        A Session-wide abort can target a newer turn between a local lookup and
        the RPC. This method requires the Host's exact contract and never falls
        back to ordinary abort, idle inference, or shared-process termination.
        Cancellation acceptance and exact physical settlement remain separate.
        """
        if recover_retired_only and recover_interrupted_only:
            raise ValueError("choose one exact recovery phase")
        identity = {
            "sessionId": str(session_id or "").strip(),
            "turnId": str(turn_id or "").strip(),
            "clientMessageId": str(client_message_id or "").strip(),
            "cancelId": str(cancel_id or "").strip(),
        }
        if any(not value for value in identity.values()):
            raise ValueError("exact cancellation requires session, turn, command and cancel identities")
        with self._lifecycle_lock:
            with self._lock:
                resident = (identity["sessionId"] in self._open_sessions
                    and self._client is not None and self._client.running)
            if not resident:
                self.ensure(identity["sessionId"], retire_recovered_turn=False)
            primitives = as_mapping(self._host_capabilities.get("runtimePrimitives"))
            if primitives.get("sessionExactTurnCancel") is not True:
                # A capability refusal happens before any abort RPC. Keep it
                # distinct from a lost acknowledgement: the Room may wait for
                # this exact turn's natural settlement, then apply its revision.
                # This is a PAW preflight rejection, never a Host cancel/drain
                # receipt and never permission to issue a broad Session abort.
                return {
                    "schemaVersion": "rag-ime.pi-exact-turn-cancel.v1",
                    **identity,
                    "receiptId": "pi-cancel-unsupported:" + identity["cancelId"],
                    "state": "rejected",
                    "source": "paw_runtime_capability_preflight",
                    "reason": "sessionExactTurnCancel_unsupported",
                }
            if recover_retired_only and primitives.get("sessionRetiredTurnRecovery") is not True:
                raise PiRuntimeError("Pi Runtime Host does not support exact retired turn recovery")
            if recover_interrupted_only and primitives.get("sessionInterruptedTurnRecovery") is not True:
                raise PiRuntimeError("Pi Runtime Host does not support exact interrupted turn recovery")
            client = self._require_client()
        params = {
            "sessionId": identity["sessionId"], "expectedTurnId": identity["turnId"],
            "clientMessageId": identity["clientMessageId"], "cancelId": identity["cancelId"],
            "lookupOnly": bool(lookup_only),
        }
        if recover_retired_only:
            params["recoverRetiredOnly"] = True
        if recover_interrupted_only:
            params["recoverInterruptedOnly"] = True
        result = client.send("session.abort", params, timeout=1.0)
        if (result.get("schemaVersion") != "rag-ime.pi-exact-turn-cancel.v1"
            or any(result.get(key) != value for key, value in identity.items())
            or result.get("state") not in {"accepted", "rejected", "unknown"}
            or (result.get("state") != "unknown" and not str(result.get("receiptId") or "")
                and not ((recover_retired_only or recover_interrupted_only) and result.get("state") == "rejected"))):
            raise PiRuntimeError("Pi Runtime Host returned an invalid exact cancellation receipt")
        value = dict(result)
        if value["state"] == "accepted":
            if value.get("phase") not in {"requested", "settled", "failed"}:
                raise PiRuntimeError("Pi Runtime exact cancellation phase is invalid")
            runtime_receipt = value.get("runtimeReceipt")
            if runtime_receipt is not None and (
                not isinstance(runtime_receipt, Mapping)
                or runtime_receipt.get("schemaVersion") != "rag-ime.pi-session-abort-receipt.v1"
                or runtime_receipt.get("sessionId") != identity["sessionId"]
                or runtime_receipt.get("turnId") != identity["turnId"]
            ):
                raise PiRuntimeError("Pi Runtime exact cancellation carries another turn receipt")
            with self._lock:
                state = self._states.get(identity["sessionId"])
                if (not recover_retired_only and not recover_interrupted_only and state is not None and state.turn_id == identity["turnId"]
                    and state.client_message_id == identity["clientMessageId"]):
                    state.abort_requested_turn_id = identity["turnId"]
                    try:
                        self.events.publish(identity["sessionId"], "status_changed",
                            {"status": "aborting", "cancelId": identity["cancelId"]}, turn_id=identity["turnId"])
                    except Exception:
                        value["projectionSync"] = {"state": "pending", "failedOperations": ["status_changed"]}
        return value

    def require_turn_abort_target(self, session_id: str) -> None:
        """Reject compaction before the application captures turn/job cancellation."""
        if not self._is_durable(session_id):
            return
        with self._lock:
            state = self._states.get(session_id)
            if state is not None and state.compaction_target:
                raise PiRuntimeTurnConflict("Stop requires the exact compactionTarget")
            local_unsent = state is not None and state.prompt_admission_in_flight and not state.prompt_dispatched
        if not local_unsent:
            prepared = self.ensure(session_id, retire_recovered_turn=False)
            if as_mapping(prepared.get("state")).get("compactionTarget") is not None:
                raise PiRuntimeTurnConflict("Stop requires the exact compactionTarget")

    def abort_with_approval_fence(
        self,
        session_id: str,
        before_abort: Callable[[Mapping[str, object]], None],
        *, expected_identity: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        """Let the Session owner persist the selected Stop before Host RPC."""
        return self.abort(session_id, _before_abort=before_abort, _expected_identity=expected_identity)

    def abort(
        self,
        session_id: str,
        *,
        _before_abort: Callable[[Mapping[str, object]], None] | None = None,
        _expected_identity: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        if self._is_durable(session_id):
            with self._lock:
                state = self._states.get(session_id)
                local_unsent = state is not None and state.prompt_admission_in_flight and not state.prompt_dispatched
                resident = session_id in self._open_sessions and self._client is not None and self._client.running
            if not resident and not local_unsent:
                # Cold Stop observes the original persisted input without
                # resuming it; absence from Python memory does not prove idle.
                self.ensure(session_id, retire_recovered_turn=False)
        abort_projection_failed = False
        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
            if state.compaction_target:
                raise PiRuntimeTurnConflict("Stop requires the exact compactionTarget")
            turn_id = state.turn_id
            client_message_id = state.client_message_id
            if _expected_identity is not None and (
                turn_id != _expected_identity["turnId"]
                or client_message_id != _expected_identity["clientMessageId"]
            ):
                raise PiRuntimeTurnConflict("Stop target changed before its exact turn could be cancelled")
            if _before_abort is not None:
                _before_abort({
                    "turnId": turn_id,
                    "pendingAdmission": bool(not turn_id and state.prompt_admission_in_flight),
                    "clientMessageId": (state.client_message_id if turn_id
                        else state.admission_client_message_id),
                })
            if turn_id and (
                (session_id, turn_id) in self._retired_host_turns
                or turn_id in state.retired_turn_ids
            ):
                return {
                    "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "alreadySettled": True,
                    "cancelledDecisionIds": [],
                    "cancelledUIRequestIds": [],
                    "lifecycle": {
                        "schemaVersion": "pi.agent-abort-receipt.v1",
                        "scopeId": f"{session_id}:{turn_id}",
                        "generation": 0,
                        "reason": "already_settled",
                        "cancelledContinuationIds": [],
                        "cancelledOperationIds": [],
                        "failedOperationIds": [],
                        "operations": [],
                        "pendingOperations": [],
                        "drained": True,
                        "idle": True,
                    },
                }
            if not turn_id:
                if state.prompt_admission_in_flight:
                    state.abort_pending_admission = True
                    admission_client_message_id = state.admission_client_message_id
                    dispatched = state.prompt_dispatched
                    if (
                        dispatched
                        and not state.admission_abort_dispatched
                    ):
                        state.admission_abort_dispatched = True
                        worker = threading.Thread(
                            target=self._deliver_pending_admission_abort,
                            args=(session_id, admission_client_message_id),
                            name=f"rag-ime-pi-admission-abort-{session_id[-8:]}",
                            daemon=True,
                        )
                        worker.start()
                    state.prompt_dispatch_signal.set()
                    self.events.publish(
                        session_id,
                        "status_changed",
                        {
                            "status": "aborting",
                            "pendingAdmission": True,
                        },
                    )
                    if not dispatched:
                        self.sessions.set_status(
                            session_id,
                            "idle",
                            last_message_preview="已停止。",
                        )
                        if (
                            self._client is not None
                            and self._client.running
                        ):
                            self._status = "ready"
                    return {
                        "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                        "sessionId": session_id,
                        "turnId": "",
                        "pendingAdmission": True,
                        "admissionCancelled": not dispatched,
                        "cancelledDecisionIds": [],
                        "cancelledUIRequestIds": [],
                        "lifecycle": {
                            "schemaVersion": "pi.agent-abort-receipt.v1",
                            "scopeId": session_id,
                            "generation": 0,
                            "reason": "user_abort",
                            "cancelledContinuationIds": [],
                            "cancelledOperationIds": [],
                            "failedOperationIds": [],
                            "operations": [],
                            "pendingOperations": (
                                ["prompt_admission"] if dispatched else []
                            ),
                            "drained": not dispatched,
                            "idle": not dispatched,
                        },
                    }
                self.sessions.set_status(session_id, "idle")
                return {
                    "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                    "sessionId": session_id,
                    "turnId": "",
                    "cancelledDecisionIds": [],
                    "cancelledUIRequestIds": [],
                    "lifecycle": {
                        "schemaVersion": "pi.agent-abort-receipt.v1",
                        "scopeId": "",
                        "generation": 0,
                        "reason": "user_abort",
                        "cancelledContinuationIds": [],
                        "cancelledOperationIds": [],
                        "failedOperationIds": [],
                        "operations": [],
                        "pendingOperations": [],
                        "drained": True,
                        "idle": True,
                    },
                }
            # Mark the exact turn before sending the RPC. The host is allowed
            # to emit agent_settled before the abort ACK reaches this thread.
            if self._host_capabilities.get("sessionBoundAbort") is not True:
                raise PiRuntimeError("Pi Runtime Host cannot bind Stop to its original turn; update the managed Runtime")
            state.abort_requested_turn_id = turn_id
            if state.abort_timer is not None:
                state.abort_timer.cancel()
            timer = threading.Timer(
                1.0,
                self._abort_fallback_expired,
                args=(session_id, turn_id),
            )
            timer.daemon = True
            state.abort_timer = timer
            timer.start()
            # Publish the user-visible transition before waiting up to one
            # second for the Host ACK. Stop feedback must not depend on a
            # provider, Tool, or process that is precisely what we are
            # attempting to cancel.
            try:
                self.events.publish(
                    session_id,
                    "status_changed",
                    {"status": "aborting"},
                    turn_id=turn_id,
                )
            except Exception:
                if _expected_identity is None:
                    raise
                # A cancelled admission's ACK must still deliver exact native
                # Stop even when its durable status projection is unavailable.
                abort_projection_failed = True
        client = self._require_client()
        try:
            result = client.send(
                "session.abort",
                {"sessionId": session_id, "expectedTurnId": turn_id,
                    "clientMessageId": client_message_id},
                timeout=1.0,
            )
        except Exception as exc:
            if isinstance(exc, PiRuntimeCommandRejected) and exc.host_error_code == "ABORT_TARGET_MISMATCH":
                # The Host proved this Stop did not touch its current target.
                # Do not turn that rejection into a later shared-Host kill.
                timer.cancel()
                with self._lock:
                    state = self._states.get(session_id)
                    if state is not None and state.abort_timer is timer:
                        state.abort_timer = None
                raise
            with self._lock:
                state = self._states.get(session_id)
                if state is not None and state.abort_requested_turn_id == turn_id:
                    # Keep the exact-turn fence and let the already armed
                    # fallback retire the turn and request a governed host kill.
                    self.events.publish(
                        session_id,
                        "status_changed",
                        {"status": "aborting", "escalated": True},
                        turn_id=turn_id,
                    )
            raise
        lifecycle = result.get("lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
        response_turn_id = str(result.get("turnId") or "")
        pending_operations = lifecycle.get("pendingOperations")
        host_already_idle = (
            not self._is_durable(session_id)
            and
            not response_turn_id
            and lifecycle.get("schemaVersion") == "pi.agent-abort-receipt.v1"
            and lifecycle.get("idle") is True
            and lifecycle.get("drained") is True
            and isinstance(pending_operations, list)
            and not pending_operations
        )
        if (
            result.get("schemaVersion") != "rag-ime.pi-session-abort-receipt.v1"
            or result.get("sessionId") != session_id
            or not lifecycle
            or (response_turn_id != turn_id and not host_already_idle)
        ):
            raise PiRuntimeError("Pi Runtime Host returned an invalid Session abort receipt")
        if abort_projection_failed:
            result = dict(result)
            result["projectionSync"] = {"state": "pending", "failedOperations": ["status_changed"]}
        if host_already_idle:
            # Pi owns the live Run. It can settle between PAW reading the local
            # turn fence and handling session.abort, in which case there is no
            # active Host turn left to echo. A typed, drained, idle receipt with
            # no pending operations is sufficient proof to retire only the
            # exact local turn; arbitrary empty or mismatched receipts remain
            # invalid and never reach this branch.
            normalized = dict(result)
            normalized["turnId"] = turn_id
            retired = False
            with self._lock:
                state = self._states.setdefault(session_id, _HostedSessionState())
                if state.turn_id == turn_id:
                    if state.abort_timer is not None:
                        state.abort_timer.cancel()
                        state.abort_timer = None
                    if state.settle_timer is not None:
                        state.settle_timer.cancel()
                        state.settle_timer = None
                    if len(state.retired_turn_ids) >= 64:
                        state.retired_turn_ids.pop()
                    state.retired_turn_ids.add(turn_id)
                    if len(self._retired_host_turns) >= 256:
                        self._retired_host_turns.pop()
                    self._retired_host_turns.add((session_id, turn_id))
                    state.turn_id = ""
                    state.client_message_id = ""
                    state.stream_pi_message_id = ""
                    self._clear_turn_projection_locked(state)
                    state.had_tool_activity = False
                    state.settle_extension_failed = False
                    state.abort_requested_turn_id = ""
                    state.pending_approvals.clear()
                    state.pending_reviews.clear()
                    state.pending_ui_requests.clear()
                    self._status = "ready"
                    self._schedule_idle_locked()
                    retired = True
            if retired:
                self.sessions.set_status(
                    session_id,
                    "idle",
                    last_message_preview="已停止。",
                )
                self.events.publish(
                    session_id,
                    "turn_completed",
                    {
                        "status": "aborted",
                        "aborted": True,
                        "terminalEvent": "idle_abort_receipt",
                    },
                    turn_id=turn_id,
                )
            return normalized
        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
            # The host can emit agent_settled before the abort ACK arrives.
            # Do not regress an already terminal turn back to "aborting".
            if state.turn_id != turn_id:
                return dict(result)
        return dict(result)

    def compact(self, session_id: str, instructions: str = "") -> dict[str, object]:
        self.ensure(session_id)
        result = self._require_client().send(
            "session.compact",
            {"sessionId": session_id, "instructions": str(instructions).strip()[:2000]},
            timeout=max(300.0, self.config.command_timeout_seconds),
        )
        checkpoint = self._observe_compaction(session_id, result, "manual")
        if checkpoint:
            result["memoryCheckpoint"] = checkpoint
        return result

    def has_pending_approval(self, session_id: str, approval_id: str) -> bool:
        with self._lock:
            return approval_id in self._states.get(session_id, _HostedSessionState()).pending_approvals

    def has_pending_review(self, session_id: str, run_id: str) -> bool:
        with self._lock:
            return run_id in self._states.get(session_id, _HostedSessionState()).pending_reviews

    def pending_ui_requests(self, session_id: str) -> list[dict[str, object]]:
        with self._lock:
            state = self._states.get(session_id)
            if state is None:
                return []
            return [
                {
                    **{
                        key: value
                        for key, value in request.items()
                        if not key.startswith("_")
                    },
                    "turnId": str(request.get("_turnId") or state.turn_id),
                    "createdAtMs": int(request.get("_createdAtMs") or 0),
                }
                for request in state.pending_ui_requests.values()
            ]

    def _expire_ui_request(self, session_id: str, request_id: str) -> None:
        try:
            self.resolve_ui_request(
                session_id,
                request_id,
                response={
                    "cancelled": True,
                    "resolutionSource": "timeout",
                },
            )
        except (PiRuntimeError, ValueError):
            return

    def resolve_review(self, session_id: str, run_id: str, *, reviewed: bool) -> None:
        with self._lock:
            state = self._states.get(session_id)
            request_id = state.pending_reviews.get(run_id) if state else None
            turn_id = state.turn_id if state else ""
        if not request_id:
            raise PiRuntimeError("review request is no longer pending")
        self._require_client().send(
            "review.resolve",
            {"sessionId": session_id, "runId": run_id, "reviewed": bool(reviewed)},
        )
        with self._lock:
            if state:
                state.pending_reviews.pop(run_id, None)
        self.events.publish(
            session_id,
            "approval_resolved",
            {
                "requestId": request_id,
                "runId": run_id,
                "state": "approved",
                "reviewState": "reviewed" if reviewed else "deferred",
            },
            turn_id=turn_id,
        )

    def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        *,
        approved: bool,
        resolution_state: str = "",
    ) -> None:
        with self._lock:
            state = self._states.get(session_id)
            request_id = state.pending_approvals.get(approval_id) if state else None
            turn_id = state.turn_id if state else ""
        approval = self.sessions.get_approval(approval_id)
        tool_call_id = str(approval.get("toolCallId") or "").strip()
        causal = approval.get("causalMetadata")
        if isinstance(causal, Mapping):
            turn_id = str(causal.get("turnId") or "").strip() or turn_id
        if not request_id:
            raise PiRuntimeError("approval request is no longer pending")
        self._require_client().send(
            "approval.resolve",
            {"sessionId": session_id, "approvalId": approval_id, "approved": bool(approved)},
        )
        with self._lock:
            if state:
                state.pending_approvals.pop(approval_id, None)
        self.events.publish(
            session_id,
            "approval_resolved",
            {
                "requestId": request_id,
                "approvalId": approval_id,
                "state": resolution_state or ("approved" if approved else "rejected"),
                **({"toolCallId": tool_call_id} if tool_call_id else {}),
            },
            turn_id=turn_id,
        )

    def resolve_ui_request(
        self,
        session_id: str,
        request_id: str,
        *,
        response: Mapping[str, object],
    ) -> dict[str, object]:
        normalized_request_id = str(request_id or "").strip()
        with self._lock:
            state = self._states.get(session_id)
            request = state.pending_ui_requests.get(normalized_request_id) if state else None
            review_run_id = (
                next(
                    (
                        run_id
                        for run_id, pending_id in state.pending_reviews.items()
                        if pending_id == normalized_request_id
                    ),
                    "",
                )
                if state
                else ""
            )
            if request is None and review_run_id:
                request = {
                    "requestId": normalized_request_id,
                    "method": "confirm",
                    "_turnId": state.turn_id if state else "",
                }
            if request is None or request.get("_resolving") is True:
                raise PiRuntimeError("UI request is no longer pending")
            request["_resolving"] = True
        method = str(request.get("method") or "")
        cancelled = response.get("cancelled") is True
        try:
            resolved, resolution_source = resolve_ui_response(request, response)
        except ValueError:
            with self._lock:
                request.pop("_resolving", None)
            raise
        except PiRuntimeError:
            # Preserve the existing confirmation-parser failure boundary;
            # select/group validation releases its claim for a corrected answer.
            if method != "confirm":
                with self._lock:
                    request.pop("_resolving", None)
            raise
        try:
            result = self._require_client().send(
                "ui.resolve",
                {
                    "sessionId": session_id,
                    "requestId": normalized_request_id,
                    "response": resolved,
                },
            )
        except Exception:
            with self._lock:
                request.pop("_resolving", None)
            raise
        timeout_timer = request.get("_timeoutTimer")
        if isinstance(timeout_timer, threading.Timer):
            timeout_timer.cancel()
        turn_id = str(request.get("_turnId") or (state.turn_id if state else ""))
        with self._lock:
            if state:
                state.pending_ui_requests.pop(normalized_request_id, None)
                if review_run_id:
                    state.pending_reviews.pop(review_run_id, None)
        self.events.publish(
            session_id,
            "user_input_required",
            {
                "requestId": normalized_request_id,
                "method": method,
                "resolutionState": "cancelled" if cancelled else "resolved",
                "resolutionSource": resolution_source,
            },
            turn_id=turn_id,
        )
        return {
            "requestId": normalized_request_id,
            "resolved": True,
            "method": method,
            "resolutionState": "cancelled" if cancelled else "resolved",
            "resolutionSource": resolution_source,
            "host": dict(result),
        }

    def plugin_list(self) -> list[dict[str, object]]:
        return [dict(value) for value in self._require_host_result("plugins.list").get("plugins") or [] if isinstance(value, Mapping)]

    def plugin_catalog(self) -> list[dict[str, object]]:
        return [
            dict(value)
            for value in self._require_host_result("plugins.catalog").get("packages") or []
            if isinstance(value, Mapping)
        ]

    def plugin_create_package(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._require_host_result("plugins.package.create", payload)

    def plugin_validate(self, source_path: str) -> dict[str, object]:
        return self._require_host_result("plugins.validate", {"sourcePath": source_path})

    def plugin_prepare_package(self, source: str) -> dict[str, object]:
        return self._require_host_result(
            "plugins.package.prepare", {"source": source}
        )

    def plugin_preview_install(
        self, payload: Mapping[str, object]
    ) -> dict[str, object]:
        return self._require_host_result("plugins.install.preview", payload)

    def plugin_install(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._require_host_result(
            "plugins.install",
            {**dict(payload), "approvalToken": self.config.plugin_approval_token},
        )

    def plugin_enable(
        self,
        plugin_id: str,
        *,
        enabled: bool,
        expected_active_digest: str,
        expected_enabled: bool,
    ) -> dict[str, object]:
        return self._require_host_result(
            "plugins.enable" if enabled else "plugins.disable",
            {
                "pluginId": plugin_id,
                "approvalToken": self.config.plugin_approval_token,
                "expectedActiveDigest": expected_active_digest,
                "expectedEnabled": expected_enabled,
            },
        )

    def plugin_uninstall(
        self,
        plugin_id: str,
        *,
        expected_active_digest: str,
        expected_enabled: bool,
    ) -> dict[str, object]:
        return self._require_host_result(
            "plugins.uninstall",
            {
                "pluginId": plugin_id,
                "expectedActiveDigest": expected_active_digest,
                "expectedEnabled": expected_enabled,
                "approvalToken": self.config.plugin_approval_token,
            },
        )

    def plugin_rollback(
        self,
        plugin_id: str,
        *,
        expected_active_digest: str,
        target_digest: str,
    ) -> dict[str, object]:
        return self._require_host_result(
            "plugins.rollback",
            {
                "pluginId": plugin_id,
                "expectedActiveDigest": expected_active_digest,
                "targetDigest": target_digest,
                "approvalToken": self.config.plugin_approval_token,
            },
        )

    def _require_host_result(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        with self._lifecycle_lock:
            return self._host().send(method, params, timeout=max(30.0, self.config.command_timeout_seconds))

    def stop(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                self._cancel_idle_locked()
                client = self._client
                self._client = None
                self._intentional_stop = True
                session_ids = tuple(self._open_sessions)
                for state in self._states.values():
                    if state.abort_timer is not None:
                        state.abort_timer.cancel()
                    if state.settle_timer is not None:
                        state.settle_timer.cancel()
                self._open_sessions.clear()
                self._active_completion_ids.clear()
                classifications = tuple(self._classifications.values())
                self._classifications.clear()
                self._completion_sinks.clear()
                self._states = {session_id: state for session_id, state in self._states.items()
                    if state.runtime_engine == "durable" and (state.turn_id or state.compaction_target or state.prompt_admission_in_flight)}
                for state in self._states.values():
                    state.recoverable = True
                self._status = "stopped" if self.config.enabled else "disabled"
                projection_threads = tuple(self._recent_projection_threads)
            if client is not None:
                client.stop()
            for classification in classifications:
                self._notify_classification_settled(classification.on_settled)
            current_thread = threading.current_thread()
            for thread in projection_threads:
                if thread is not current_thread:
                    thread.join(timeout=2)
            for session_id in session_ids:
                try:
                    if not self._is_durable(session_id):
                        self.sessions.set_status(session_id, "idle")
                except KeyError:
                    pass

    def close_session(self, session_id: str) -> bool:
        """Retire one idle hosted Session without restarting the shared Host."""

        normalized = str(session_id or "").strip()
        if not normalized:
            raise ValueError("session_id is required")
        with self._lifecycle_lock:
            with self._lock:
                state = self._states.get(normalized)
                if state is not None and (state.turn_id or state.compaction_target):
                    raise PiRuntimeError(
                        "Session must settle before its runtime policy changes"
                    )
                client = self._client
                opened = normalized in self._open_sessions
            if opened and client is not None and client.running:
                response = client.send(
                    "session.close",
                    {"sessionId": normalized},
                )
                if response.get("closed") is not True:
                    raise PiRuntimeError(
                        "managed Pi Host did not close the requested Session"
                    )
            with self._lock:
                self._open_sessions.discard(normalized)
                retired = self._states.pop(normalized, None)
                if retired is not None:
                    if retired.abort_timer is not None:
                        retired.abort_timer.cancel()
                    if retired.settle_timer is not None:
                        retired.settle_timer.cancel()
                self._schedule_idle_locked()
            try:
                self.sessions.set_status(normalized, "idle")
            except KeyError:
                pass
            return opened

    def _require_client(self) -> PiRuntimeHostClient:
        with self._lock:
            client = self._client
        if client is None or not client.running:
            raise PiRuntimeError("Pi Runtime Host is not running")
        return client

    def _publish_provider_request(
        self,
        session_id: str,
        turn_id: str,
        raw_message: Mapping[str, object],
        *,
        status: str,
    ) -> None:
        completed_at_ms = int(time.time() * 1000)
        payload = provider_request_receipt(
            raw_message,
            turn_id=turn_id,
            provider=self.config.provider,
            model=self.config.model,
            status=status,
            completed_at_ms=completed_at_ms,
        )
        request_id = str(payload.get("requestId") or "")
        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
            if not request_id or request_id in state.provider_request_ids:
                return
            state.provider_request_ids.add(request_id)
        self.events.publish(
            session_id,
            "provider_request_failed"
            if str(payload.get("status") or "") == "failed"
            else "provider_request_completed",
            payload,
            turn_id=turn_id,
            created_at_ms=completed_at_ms,
        )

    def _handle_host_event(
        self,
        envelope: dict[str, object],
        *,
        allow_retired_turn: bool = False,
        source_client: PiRuntimeHostClient | None = None,
    ) -> None:
        if envelope.get("protocolVersion") != PI_HOST_PROTOCOL_VERSION or envelope.get("event") not in {
            "agent.event",
            "runtime.notice",
        }:
            return
        raw = dict(as_mapping(envelope.get("payload")))
        if envelope.get("event") == "runtime.notice" and raw.get("type") == "classification_settled":
            identity = str(raw.get("requestId") or "")
            with self._lock:
                call = self._classifications.get(identity)
            if (call is not None and call.client is source_client
                    and raw.get("dispatchId") == call.dispatch_id):
                self._release_classification(identity, call)
            return
        if _record_plugin_usage_notice(
            self.plugin_usage,
            event=envelope.get("event"),
            session_id=envelope.get("sessionId"),
            payload=raw,
        ):
            return
        if envelope.get("event") == "runtime.notice" and str(raw.get("type") or "") == "completion_text_delta":
            request_id = str(raw.get("requestId") or "")
            delta = str(raw.get("delta") or "")
            with self._lock:
                sink = self._completion_sinks.get(request_id)
            if sink is not None and delta:
                try:
                    sink(delta)
                except Exception:
                    pass
            return
        session_id = str(envelope.get("sessionId") or "")
        if not session_id:
            return
        turn_id = str(envelope.get("turnId") or "")
        client_message_id = str(envelope.get("clientMessageId") or "")
        event_type = str(raw.get("type") or "")
        durable_engine = self._is_durable(session_id)
        if (durable_engine and not turn_id and (event_type == "compaction_settled"
            or (event_type == "compaction_start" and self._host_capabilities.get("sessionCompactionRecovery") is True))):
            if event_type == "compaction_settled":
                target = validate_compaction_target(raw.get("compactionTarget"))
                observed = as_mapping(raw.get("state"))
                self._validate_durable_state(session_id, observed, control=True)
                self._validate_durable_binding(observed, self.sessions.runtime_binding(session_id) or {})
                if observed.get("piSessionId") != target["runtimeSessionId"]:
                    raise PiRuntimeError("Pi compaction settlement belongs to another native Session")
            # The ordered event lane may lag an explicit control response or
            # successor admission. Read current authority under the same lock
            # as snapshots/controls; a delayed old event must never restore it.
            with self._lifecycle_lock:
                snapshot = dict(self._require_client().send("session.control_state", {"sessionId": session_id}))
                self._validate_durable_state(session_id, snapshot, control=True)
                self._validate_durable_binding(snapshot, self.sessions.runtime_binding(session_id) or {})
                self._observe_durable_state(session_id, snapshot)
                self.events.publish(session_id, "status_changed", {
                    "status": "idle" if snapshot["isIdle"] else "busy", "runtimeEngine": "durable",
                    "paused": snapshot["paused"], "recoverable": snapshot["recoverable"],
                    "activeTurn": dict(as_mapping(snapshot.get("activeTurn"))) or None,
                    "compactionTarget": snapshot.get("compactionTarget"), "projectionCurrent": True,
                })
            if event_type == "compaction_settled":
                return
        with self._lock:
            if (
                turn_id
                and not allow_retired_turn
                and (session_id, turn_id) in self._retired_host_turns
            ):
                return
            state = self._states.get(session_id)
            if (
                turn_id
                and not allow_retired_turn
                and state is not None
                and turn_id in state.retired_turn_ids
            ):
                return
            if state is not None and state.compaction_target and turn_id:
                return
            # Queue notifications are observations, including after opening a
            # historical Session. Their correlation ids cannot admit a prompt
            # or reopen/replace a live turn; terminal fences still apply.
            if event_type != "queue_update":
                state = self._states.setdefault(session_id, _HostedSessionState())
                if durable_engine:
                    state.runtime_engine = "durable"
                    if not state.compaction_target:
                        state.recoverable = False
                if turn_id:
                    state.turn_id = turn_id
                    if state.abort_pending_admission:
                        # Stop can reach Pi before PAW receives prompt's turnId.
                        # Fence the first Host event for that admission as aborted
                        # so an early agent_settled cannot be projected completed.
                        state.abort_requested_turn_id = turn_id
                if client_message_id:
                    state.client_message_id = client_message_id
                if (
                    turn_id
                    and client_message_id
                    and state.prompt_admission_in_flight
                    and state.admission_client_message_id == client_message_id
                ):
                    # A correlated execution event is durable acceptance evidence.
                    # This also closes an admission whose command ACK was lost,
                    # without waiting for an HTTP retry or guessing by text/time.
                    state.prompt_admission_in_flight = False
                    state.admission_client_message_id = ""
                    state.abort_pending_admission = False
                    state.admission_abort_dispatched = False
                    state.prompt_dispatched = False
                    state.prompt_dispatch_signal.set()
        if event_type == "queue_update":
            self.events.publish(
                session_id,
                "message_queue_updated",
                {
                    "steering": public_message_queue(raw.get("steering")),
                    "followUp": public_message_queue(raw.get("followUp")),
                },
                turn_id=turn_id,
            )
            return
        if event_type == "message_start":
            raw_message = as_mapping(raw.get("message"))
            if str(raw_message.get("role") or "").lower() == "assistant":
                source_loop_id = pi_message_id(raw_message, turn_id)
                with self._lock:
                    state.source_loop_id = source_loop_id
                    state.stream_pi_message_id = source_loop_id
            return
        if event_type == "message_update":
            update = as_mapping(raw.get("assistantMessageEvent"))
            update_type = str(update.get("type") or "")
            if update_type == "text_delta":
                raw_message = as_mapping(raw.get("message"))
                streamed_message_id = pi_message_id(raw_message, turn_id)
                with self._lock:
                    replace_block = (
                        streamed_message_id != state.stream_pi_message_id
                    )
                    state.stream_pi_message_id = streamed_message_id
                self.events.publish(
                    session_id,
                    "text_delta",
                    text_delta_payload(
                        raw_message, update, turn_id=turn_id,
                        replace_block=replace_block, source_loop_id=state.source_loop_id,
                    ),
                    turn_id=turn_id,
                )
            elif update_type == "thinking_start":
                self.events.publish(
                    session_id,
                    "status_changed",
                    {
                        "status": "analyzing",
                        "phase": "reasoning",
                        "summary": "正在等待 Provider 的公开思考摘要",
                    },
                    turn_id=turn_id,
                )
            elif update_type == "thinking_end":
                raw_message = as_mapping(raw.get("message"))
                summaries = public_reasoning_summaries(
                    raw_message,
                    completed_content_index=as_integer(update.get("contentIndex")),
                )
                if summaries:
                    message_id = pi_message_id(raw_message, turn_id)
                    # Match the one cumulative summary per durable Pi message.
                    reasoning_id = f"reasoning:{message_id}:0"
                    self.events.publish(
                        session_id,
                        "reasoning_summary",
                        {
                            "requestId": reasoning_id,
                            "sourceMessageId": message_id,
                            "summary": summaries[-1],
                            "items": summaries,
                            "source": "provider_reasoning_summary",
                            "state": "completed",
                            **(
                                {"sourceLoopId": state.source_loop_id}
                                if state.source_loop_id
                                else {}
                            ),
                        },
                        turn_id=turn_id,
                    )
            return
        if event_type == "message_end":
            raw_message = as_mapping(raw.get("message"))
            role = str(raw_message.get("role") or "assistant").lower()
            if role == "assistant":
                self._publish_provider_request(
                    session_id,
                    turn_id,
                    raw_message,
                    status=(
                        "failed"
                        if str(raw_message.get("stopReason") or "").lower()
                        == "error"
                        or bool(raw_message.get("errorMessage"))
                        else "completed"
                    ),
                )
            if role == "user" or not pi_message_is_public(raw_message):
                return
            trusted_blocks = raw.get("agentBlocks")
            if role == "assistant":
                trusted_blocks = state.tool_blocks.blocks_for_message(
                    raw_message,
                    trusted_blocks,
                )
            message = pi_message_payload(
                raw_message,
                session_id=session_id,
                turn_id=turn_id,
                media_resolver=self._media_resolver,
                message_id=(pi_message_id(raw_message, turn_id) if durable_engine else f"{turn_id}:assistant")
                    if role == "assistant" else None,
                trusted_blocks=trusted_blocks,
            )
            if not pi_message_completes_public_turn(raw_message):
                # Some Providers return a complete public explanation together
                # with another Tool call but emit no text_delta events. Hiding
                # that mixed message leaves the UI blank until Stop forces a
                # snapshot. Project its public text into one replaceable live
                # bubble; a later mixed/final message updates the same bubble.
                progress_text = "\n\n".join(
                    str(as_mapping(block.get("data")).get("text") or "").strip()
                    for block in message.to_payload().get("blocks") or []
                    if isinstance(block, Mapping)
                    and str(block.get("type") or "") == "text"
                    and str(as_mapping(block.get("data")).get("text") or "").strip()
                )
                if progress_text:
                    self.events.publish(
                        session_id,
                        "text_delta",
                        {
                            "messageId": f"{turn_id}:assistant",
                            "blockId": f"{turn_id}:assistant:text",
                            "delta": progress_text,
                            "replaceContent": True,
                            **(
                                {"sourceLoopId": state.source_loop_id}
                                if state.source_loop_id
                                else {}
                            ),
                        },
                        turn_id=turn_id,
                    )
                return
            self.events.publish(
                session_id,
                "message_completed",
                {
                    "message": message.to_payload(),
                    "usage": public_usage(raw.get("message")),
                    **public_usage_evidence(raw_message),
                    "telemetry": dict(as_mapping(raw.get("telemetry"))),
                    **(
                        {"sourceLoopId": state.source_loop_id}
                        if state.source_loop_id
                        else {}
                    ),
                },
                turn_id=turn_id,
            )
            return
        if event_type == "compaction_start":
            self.events.publish(
                session_id,
                "compaction_started",
                {
                    "reason": str(raw.get("reason") or "threshold"),
                    "telemetry": dict(as_mapping(raw.get("telemetry"))),
                },
                turn_id=turn_id,
            )
            return
        if event_type == "compaction_end":
            result = as_mapping(raw.get("result"))
            payload: dict[str, object] = {
                "reason": str(raw.get("reason") or "threshold"),
                "aborted": bool(raw.get("aborted")),
                "willRetry": bool(raw.get("willRetry")),
                "tokensBefore": as_integer(result.get("tokensBefore")),
                "estimatedTokensAfter": as_integer(result.get("estimatedTokensAfter")),
                "telemetry": dict(as_mapping(raw.get("telemetry"))),
            }
            if raw.get("errorMessage"):
                payload["error"] = redact_runtime_text(str(raw.get("errorMessage")))
            self.events.publish(
                session_id,
                "compaction_completed",
                payload,
                turn_id=turn_id,
            )
            self._observe_compaction(
                session_id,
                dict(result) if result else dict(raw),
                str(raw.get("trigger") or raw.get("reason") or "").strip() or "automatic",
            )
            return
        if event_type in {"auto_retry_start", "auto_retry_end"}:
            attempt = max(0, as_integer(raw.get("attempt")))
            with self._lock:
                if event_type == "auto_retry_start":
                    state.provider_retry_attempt = max(
                        state.provider_retry_attempt,
                        attempt,
                    )
                    state.provider_retry_max_attempts = max(
                        state.provider_retry_max_attempts,
                        as_integer(raw.get("maxAttempts")),
                    )
                elif raw.get("success") is True:
                    state.provider_retry_attempt = 0
                    state.provider_retry_max_attempts = 0
                else:
                    state.provider_retry_attempt = max(
                        state.provider_retry_attempt,
                        attempt,
                    )
                    maximum = state.provider_retry_max_attempts
                    if maximum > 0 and attempt >= maximum:
                        state.final_failure_context.update({
                            "retryExhausted": True,
                            "providerRetryAttempts": attempt,
                            "providerRetryMaxAttempts": maximum,
                            "nextStep": (
                                "模型连接在自动重试后仍未恢复，请稍后继续或切换模型。"
                            ),
                        })
            self.events.publish(
                session_id,
                "status_changed",
                provider_retry_status(
                    raw,
                    started=event_type == "auto_retry_start",
                ),
                turn_id=turn_id,
            )
            return
        if event_type in {"tool_execution_start", "tool_execution_update", "tool_execution_end"}:
            with self._lock:
                state.had_tool_activity = True
                tool_key = (turn_id, str(raw.get("toolCallId") or ""))
                if durable_engine and event_type == "tool_execution_start":
                    if len(state.tool_source_loops) >= 512:
                        state.tool_source_loops.pop(next(iter(state.tool_source_loops)))
                    state.tool_source_loops.setdefault(tool_key,
                        (state.source_loop_id, str(raw.get("toolName") or "")))
                original_tool = state.tool_source_loops.get(tool_key) if durable_engine else None
            # The latest assistant loop is presentation state. Late Tool events
            # retain the original call's generation, even after another start.
            source_loop_id = original_tool[0] if original_tool else ("" if durable_engine else state.source_loop_id)
            mapped_type, payload = tool_event_payload(
                raw, event_type=event_type, source_loop_id=source_loop_id,
                durable_context=({
                    "session_id": session_id,
                    "runtime_session_id": str((self.sessions.runtime_binding(session_id) or {}).get("externalSessionId") or ""),
                    "turn_id": turn_id, "client_message_id": client_message_id,
                } if durable_engine and (original_tool is None or original_tool[1] == raw.get("toolName")) else None),
            )
            if event_type == "tool_execution_end" and not payload["isError"]:
                captured = state.tool_blocks.capture(
                    raw.get("result"),
                    source_ref=(
                        f"{session_id}:{turn_id}:"
                        f"{str(raw.get('toolCallId') or '')}"
                    ),
                )
                if captured:
                    payload["agentBlocks"] = [dict(block) for block in captured]
            self.events.publish(session_id, mapped_type, payload, turn_id=turn_id)
            return
        if event_type == "extension_ui_request":
            self._handle_ui_request(session_id, turn_id, raw)
            return
        if event_type == "tool_loop_no_progress":
            message = redact_runtime_text(
                str(raw.get("message") or "Tool Loop 未产生新进展，已停止。")
            )
            reason = redact_runtime_text(
                str(raw.get("reason") or "no_progress")
            )
            tool_names = [
                redact_runtime_text(str(name))
                for name in (
                    raw.get("toolNames")
                    if isinstance(raw.get("toolNames"), list)
                    else []
                )
                if isinstance(name, str)
            ][:16]
            next_step = (
                f"检查工具 {tool_names[-1]} 的调用参数或权限，修正后在当前任务上重试。"
                if tool_names
                else "检查最后一次失败操作的输入或权限，修正后在当前任务上重试。"
            )
            with self._lock:
                state.final_error = message
                state.final_failure_context = {
                    "reason": reason,
                    "toolNames": tool_names,
                    "nextStep": next_step,
                }
            self.events.publish(
                session_id,
                "status_changed",
                {
                    "status": "working",
                    "phase": "tool_loop_no_progress",
                    "activityState": "failed",
                    "message": message,
                    "reason": reason,
                    "consecutiveAllErrorTurns": as_integer(
                        raw.get("consecutiveAllErrorTurns")
                    ),
                    "repeatedFailureSignature": as_integer(
                        raw.get("repeatedFailureSignature")
                    ),
                    "toolNames": tool_names,
                    "nextStep": next_step,
                },
                turn_id=turn_id,
            )
            return
        if event_type == "agent_end":
            messages = raw.get("messages") if isinstance(raw.get("messages"), list) else []
            with self._lock:
                state.last_agent_messages = list(messages)
                assistant_error = last_assistant_error(messages)
                if raw.get("willRetry") is True:
                    state.final_error = ""
                    state.final_failure_context.clear()
                elif assistant_error:
                    state.final_error = assistant_error
                    state.final_failure_context.clear()
                # agent_end is normally followed by agent_settled. Probe the
                # lightweight Host control state after a grace period so a
                # lost terminal event cannot leave a Tool-complete turn busy.
                self._schedule_settle_probe_locked(
                    state,
                    session_id,
                    turn_id,
                    delay_seconds=1.0,
                )
            if raw.get("willRetry") is not True and assistant_error:
                last_assistant = next(
                    (
                        as_mapping(item)
                        for item in reversed(messages)
                        if str(as_mapping(item).get("role") or "").lower()
                        == "assistant"
                    ),
                    {},
                )
                self._publish_provider_request(
                    session_id,
                    turn_id,
                    last_assistant,
                    status="failed",
                )
            # agent_end is not terminal: retries, follow-ups, and extension work can continue.
            return
        if event_type == "agent_settle_failed":
            failed_settlement = failed_settlement_receipt(
                raw,
                allow_aborted=True,
            )
            settle_error = redact_runtime_text(
                str(raw.get("error") or "Pi settlement hook failed")
            )
            recovery_turn_id = turn_id
            with self._lock:
                state = self._states.get(session_id)
                if state is not None and state.turn_id:
                    recovery_turn_id = state.turn_id
            if (
                recovery_turn_id
                and failed_settlement is not None
                and failed_settlement[0]
            ):
                self._turn_failed_once(
                    session_id,
                    recovery_turn_id,
                    PiRuntimeError(failed_settlement[1]),
                )
                return
            with self._lock:
                state = self._states.get(session_id)
                if state is not None and state.turn_id:
                    state.final_error = settle_error
                    state.settle_extension_failed = True
                    self._schedule_settle_probe_locked(
                        state,
                        session_id,
                        state.turn_id,
                        delay_seconds=0.1,
                    )
            self.events.publish(
                session_id,
                "status_changed",
                {
                    "status": "working" if recovery_turn_id else "ready",
                    "phase": "settlement_warning",
                    "warning": settle_error,
                },
                turn_id=recovery_turn_id,
            )
            return

        if event_type == "agent_settled":
            if durable_engine:
                self._validate_turn_settlement({
                    "schemaVersion": "rag-ime.pi-turn-settlement.v1", "sessionId": session_id,
                    "turnId": turn_id, "clientMessageId": client_message_id,
                    "runtimeSessionId": str(as_mapping(raw.get("receipt")).get("sessionId") or ""),
                    "receipt": raw.get("receipt"),
                }, session_id=session_id, turn_id=turn_id, client_message_id=client_message_id)
            failed_settlement = failed_settlement_receipt(
                raw,
                allow_aborted=False,
            )
            terminal_turn_id = turn_id or state.turn_id
            with self._lock:
                owns_terminal_turn = bool(
                    terminal_turn_id
                    and state.turn_id == terminal_turn_id
                )
            if owns_terminal_turn:
                self._refresh_terminal_recent_projection(
                    session_id,
                    terminal_turn_id,
                )
            if failed_settlement is not None:
                if failed_settlement[0] and terminal_turn_id:
                    self._turn_failed_once(
                        session_id,
                        terminal_turn_id,
                        PiRuntimeError(failed_settlement[1]),
                    )
                    return
                # A failed receipt with pending work is not a completion. Keep
                # the existing bounded control-state reconciliation path.
                with self._lock:
                    if state.turn_id == terminal_turn_id and terminal_turn_id:
                        state.final_error = failed_settlement[1]
                        state.settle_extension_failed = True
                        self._schedule_settle_probe_locked(
                            state,
                            session_id,
                            terminal_turn_id,
                            delay_seconds=0.1,
                        )
                return
            with self._lock:
                if state.turn_id != turn_id:
                    return
                self._fence_retired_turn_locked(
                    state,
                    session_id,
                    turn_id,
                )
                messages = list(state.last_agent_messages)
                final_error = state.final_error
                aborted = state.abort_requested_turn_id == turn_id
                if state.abort_timer is not None:
                    state.abort_timer.cancel()
                    state.abort_timer = None
                if state.settle_timer is not None:
                    state.settle_timer.cancel()
                    state.settle_timer = None
                public_message_count = sum(
                    isinstance(message, Mapping)
                    and pi_message_is_public(message)
                    for message in messages
                )
                if aborted or not final_error:
                    # Persist the terminal projection under the admission lock.
                    # Releasing ownership first lets a new reservation's busy
                    # status be overwritten by this old turn's idle write.
                    if aborted:
                        self.sessions.set_status(
                            session_id,
                            "idle",
                            last_message_preview="已停止。",
                            **({"_preserve_archived": True} if state.admission_preserve_archived else {}),
                        )
                    else:
                        self.sessions.set_status(
                            session_id,
                            "idle",
                            **({"message_count": public_message_count} if not durable_engine else {}),
                            last_message_preview=last_assistant_preview(messages),
                            **({"_preserve_archived": True} if state.admission_preserve_archived else {}),
                        )
                    state.turn_id = ""
                    state.client_message_id = ""
                    state.stream_pi_message_id = ""
                    state.source_loop_id = ""
                    self._clear_turn_projection_locked(state)
                    state.had_tool_activity = False
                    state.settle_extension_failed = False
                    state.abort_requested_turn_id = ""
                    state.pending_approvals.clear()
                    state.pending_reviews.clear()
                    state.pending_ui_requests.clear()
                    self._status = "ready"
                    self._schedule_idle_locked()
            # Observers may acquire Room locks; publish outside the Runtime
            # lock, retaining the exact old turn identity for late consumers.
            if aborted:
                self.events.publish(
                    session_id,
                    "turn_completed",
                    {"status": "aborted", "aborted": True, "terminalEvent": "agent_settled"},
                    turn_id=turn_id,
                )
                return
            if final_error:
                self._turn_failed(session_id, turn_id, PiRuntimeError(final_error))
                return
            self.events.publish(
                session_id,
                "turn_completed",
                {
                    "messageCount": public_message_count,
                    "terminalEvent": "agent_settled",
                },
                turn_id=turn_id,
            )
            return
        if event_type == "extension_error":
            extension_event = str(raw.get("event") or "")
            if extension_event == "agent_settled":
                # Pi is already idle when agent_settled extensions run. If one
                # fails, the Host can retain only its correlation identity and
                # omit the public terminal event. Reconcile that exact turn
                # through control state instead of leaving it permanently busy.
                with self._lock:
                    state = self._states.get(session_id)
                    if state is not None and state.turn_id:
                        state.settle_extension_failed = True
                        self._schedule_settle_probe_locked(
                            state,
                            session_id,
                            state.turn_id,
                            delay_seconds=0.1,
                        )
            self.events.publish(
                session_id,
                "status_changed",
                {
                    "status": "working" if turn_id else "ready",
                    "phase": "extension_warning",
                    "extensionEvent": extension_event,
                    "warning": redact_runtime_text(
                        str(raw.get("error") or "Pi extension failed")
                    ),
                },
                turn_id=turn_id,
            )
            return

    def _observe_compaction(
        self,
        session_id: str,
        result: Mapping[str, object],
        trigger: str,
    ) -> dict[str, object]:
        if self._compaction_observer is None:
            return {}
        try:
            checkpoint = self._compaction_observer(session_id, result, trigger)
        except Exception as exc:
            return {
                "schemaVersion": "rag-ime.agent-memory-checkpoint.v1",
                "ok": False,
                "stored": False,
                "status": "checkpoint_failed",
                "error": redact_runtime_text(str(exc)),
            }
        return dict(checkpoint or {})

    def _handle_ui_request(self, session_id: str, turn_id: str, raw: Mapping[str, object]) -> None:
        method = str(raw.get("method") or "")
        request_id = str(raw.get("id") or "")
        title = str(raw.get("title") or "")
        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
        if method == "confirm" and title.startswith(APPROVAL_TITLE_PREFIX):
            approval_id = title[len(APPROVAL_TITLE_PREFIX) :].strip()
            try:
                approval = self.sessions.get_approval(approval_id)
            except (KeyError, ValueError):
                self._require_client().send(
                    "approval.resolve",
                    {"sessionId": session_id, "approvalId": approval_id, "approved": False},
                )
                return
            if approval.get("sessionId") != session_id or approval.get("state") != "pending":
                self._require_client().send(
                    "approval.resolve",
                    {"sessionId": session_id, "approvalId": approval_id, "approved": False},
                )
                return
            with self._lock:
                state.pending_approvals[approval_id] = request_id
            self.events.publish(
                session_id,
                "approval_required",
                {**approval, "requestId": request_id},
                turn_id=turn_id,
            )
            return
        if method == "confirm" and title.startswith(REVIEW_TITLE_PREFIX):
            run_id = title[len(REVIEW_TITLE_PREFIX) :].strip()
            if not run_id:
                return
            with self._lock:
                state.pending_reviews[run_id] = request_id
            self.events.publish(
                session_id,
                "user_input_required",
                {
                    "requestId": request_id,
                    "requestKind": "memory_review",
                    "method": "confirm",
                    "runId": run_id,
                    "title": "审阅记忆草案",
                    "message": "记忆草案已准备好，请逐项审阅后继续本轮。",
                },
                turn_id=turn_id,
            )
            return
        if method == "editor" and title.startswith(GROUPED_QUESTIONS_TITLE_PREFIX):
            try:
                safe = grouped_question_request(request_id, raw.get("prefill"))
            except ValueError:
                self._require_client().send(
                    "ui.resolve",
                    {
                        "sessionId": session_id,
                        "requestId": request_id,
                        "response": {"cancelled": True},
                    },
                )
                return
            with self._lock:
                state.pending_ui_requests[request_id] = {
                    **safe,
                    "_turnId": turn_id,
                    "_createdAtMs": int(time.time() * 1000),
                }
            self.events.publish(
                session_id,
                "user_input_required",
                safe,
                turn_id=turn_id,
            )
            return
        if method in {"select", "confirm", "input", "editor"}:
            safe = public_ui_request(
                raw, request_id=request_id, method=method, title=title,
            )
            timeout_timer: threading.Timer | None = None
            stored_request = {
                **safe,
                "_turnId": turn_id,
                "_createdAtMs": int(time.time() * 1000),
            }
            timeout_ms = int(safe.get("timeout") or 0)
            if timeout_ms > 0:
                timeout_timer = threading.Timer(
                    timeout_ms / 1000,
                    self._expire_ui_request,
                    args=(session_id, request_id),
                )
                timeout_timer.daemon = True
                stored_request["_timeoutTimer"] = timeout_timer
            with self._lock:
                state.pending_ui_requests[request_id] = stored_request
            self.events.publish(
                session_id,
                "user_input_required",
                safe,
                turn_id=turn_id,
            )
            if timeout_timer is not None:
                timeout_timer.start()

    def _schedule_settle_probe_locked(
        self,
        state: _HostedSessionState,
        session_id: str,
        turn_id: str,
        *,
        delay_seconds: float,
    ) -> None:
        if state.settle_timer is not None:
            state.settle_timer.cancel()
        settle_timer = threading.Timer(
            delay_seconds,
            self._settle_fallback_probe,
            args=(session_id, turn_id),
        )
        settle_timer.daemon = True
        state.settle_timer = settle_timer
        settle_timer.start()

    def _reconcile_durable_settlement(self, session_id: str, turn_id: str) -> None:
        """One observational probe; idle/process loss never stands for drain."""
        with self._lock:
            state = self._states.get(session_id)
            client = self._client
            if state is None or state.turn_id != turn_id or client is None or not client.running:
                return
            client_message_id = state.client_message_id
            state.settle_timer = None
            state.abort_timer = None
        try:
            result = client.send("session.settlement.get", {"sessionId": session_id,
                "turnId": turn_id, "clientMessageId": client_message_id}, timeout=2)
            settlement = as_mapping(result.get("settlement"))
            if settlement and as_mapping(settlement.get("receipt")).get("disposition") != "suspended":
                validated = self._validate_turn_settlement(settlement, session_id=session_id,
                    turn_id=turn_id, client_message_id=client_message_id)
                self._reconcile_turn_settlement(validated)
        except Exception:
            # A missing terminal receipt remains recoverable/unknown; native
            # settlement notification or an explicit lookup can close it later.
            return

    def _settle_fallback_probe(self, session_id: str, turn_id: str) -> None:
        """Retire a turn only when the Host confirms it has no active work."""

        with self._lock:
            state = self._states.get(session_id)
            if state is None or state.turn_id != turn_id:
                return
            durable_engine = state.runtime_engine == "durable"
        if durable_engine:
            self._reconcile_durable_settlement(session_id, turn_id)
            return

        with self._lock:
            state = self._states.get(session_id)
            if state is None or state.turn_id != turn_id:
                return
            state.settle_timer = None
            client = self._client
        if client is None or not client.running:
            return
        try:
            control = client.send(
                "session.control_state",
                {"sessionId": session_id},
                timeout=min(
                    2.0,
                    max(1.0, self.config.command_timeout_seconds),
                ),
            )
        except Exception:
            # This is a recovery probe, not the owner of Host lifecycle. A
            # later Stop or Host-exit path remains authoritative on failure.
            return
        active_turn_value = control.get("activeTurn")
        active_turn = as_mapping(active_turn_value)
        with self._lock:
            state = self._states.get(session_id)
            settle_extension_failed = bool(
                state is not None
                and state.turn_id == turn_id
                and state.settle_extension_failed
            )
        if (
            control.get("isIdle") is not True
            or "activeTurn" not in control
            or (
                active_turn_value
                and (
                    not settle_extension_failed
                    or str(active_turn.get("turnId") or "") != turn_id
                )
            )
        ):
            return

        with self._lock:
            state = self._states.get(session_id)
            if state is None or state.turn_id != turn_id:
                return
            messages = list(state.last_agent_messages)
            final_error = state.final_error
            final_failure_context = dict(state.final_failure_context)
            had_tool_activity = state.had_tool_activity
            aborted = state.abort_requested_turn_id == turn_id
            if state.abort_timer is not None:
                state.abort_timer.cancel()
                state.abort_timer = None
            if len(state.retired_turn_ids) >= 64:
                state.retired_turn_ids.pop()
            state.retired_turn_ids.add(turn_id)
            if len(self._retired_host_turns) >= 256:
                self._retired_host_turns.pop()
            self._retired_host_turns.add((session_id, turn_id))
            state.turn_id = ""
            state.client_message_id = ""
            state.stream_pi_message_id = ""
            state.source_loop_id = ""
            self._clear_turn_projection_locked(state)
            state.had_tool_activity = False
            state.settle_extension_failed = False
            state.abort_requested_turn_id = ""
            state.pending_approvals.clear()
            state.pending_reviews.clear()
            state.pending_ui_requests.clear()
            self._status = "ready"
            self._schedule_idle_locked()

        if final_error and not aborted:
            message = redact_runtime_text(final_error)
            classification = classify_runtime_failure(
                final_error,
                had_tool_activity=had_tool_activity,
            )
            self.sessions.set_status(
                session_id,
                "faulted",
                last_message_preview=message,
            )
            self.events.publish(
                session_id,
                "turn_failed",
                {
                    "error": message,
                    **classification.event_payload(),
                    **final_failure_context,
                    "terminalEvent": "idle_control_reconciliation",
                },
                turn_id=turn_id,
            )
            return
        public_message_count = sum(
            isinstance(message, Mapping) and pi_message_is_public(message)
            for message in messages
        )
        self.sessions.set_status(
            session_id,
            "idle",
            message_count=public_message_count,
            last_message_preview=(
                "已停止。"
                if aborted
                else last_assistant_preview(messages)
            ),
        )
        self.events.publish(
            session_id,
            "turn_completed",
            {
                "status": "aborted" if aborted else "completed",
                "aborted": aborted,
                "messageCount": public_message_count,
                "terminalEvent": "idle_control_reconciliation",
            },
            turn_id=turn_id,
        )

    def _turn_failed_once(
        self,
        session_id: str,
        turn_id: str,
        error: BaseException,
    ) -> bool:
        """Project one exact terminal failure and ignore receipt replays."""

        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
            if (
                (session_id, turn_id) in self._retired_host_turns
                or turn_id in state.retired_turn_ids
            ):
                return False
            self._fence_retired_turn_locked(state, session_id, turn_id)
        self._turn_failed(session_id, turn_id, error)
        return True

    def _clear_turn_projection_locked(self, state: _HostedSessionState) -> None:
        """Clear shared projection after terminal proof while holding self._lock.

        Admission, identity, timers, fences and path-specific flags remain with
        the caller. This helper cannot settle a turn or publish its outcome.
        """
        state.tool_source_loops.clear()
        state.provider_request_ids.clear()
        state.tool_blocks.clear()
        state.last_agent_messages = []
        state.final_error = ""
        state.final_failure_context.clear()
        state.provider_retry_attempt = 0
        state.provider_retry_max_attempts = 0

    def _fence_retired_turn_locked(
        self,
        state: _HostedSessionState,
        session_id: str,
        turn_id: str,
    ) -> None:
        """Fence one terminal turn while the caller owns ``self._lock``."""

        if len(state.retired_turn_ids) >= 64:
            state.retired_turn_ids.pop()
        state.retired_turn_ids.add(turn_id)
        if len(self._retired_host_turns) >= 256:
            self._retired_host_turns.pop()
        self._retired_host_turns.add((session_id, turn_id))

    def _turn_failed(self, session_id: str, turn_id: str, error: BaseException) -> None:
        message = redact_runtime_text(str(error))
        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
            aborted = bool(turn_id and state.abort_requested_turn_id == turn_id)
            classification = classify_runtime_failure(
                error,
                had_tool_activity=state.had_tool_activity,
            )
            final_failure_context = dict(state.final_failure_context)
            if state.abort_timer is not None:
                state.abort_timer.cancel()
                state.abort_timer = None
            if state.settle_timer is not None:
                state.settle_timer.cancel()
                state.settle_timer = None
            # The durable terminal projection and admission ownership change
            # are one transition, just as for successful agent_settled events.
            self.sessions.set_status(
                session_id,
                "idle" if aborted else "faulted",
                last_message_preview="已停止。" if aborted else message,
                **({"_preserve_archived": True} if state.admission_preserve_archived else {}),
            )
            state.turn_id = ""
            state.client_message_id = ""
            state.stream_pi_message_id = ""
            self._clear_turn_projection_locked(state)
            state.had_tool_activity = False
            state.settle_extension_failed = False
            state.abort_requested_turn_id = ""
            state.pending_approvals.clear()
            state.pending_reviews.clear()
            state.pending_ui_requests.clear()
            if not aborted:
                self._last_error = message
            self._status = "ready" if self._client is not None and self._client.running else "faulted"
            self._schedule_idle_locked()
        if aborted:
            # A cancelled Provider request may report a transport error after
            # Stop fenced this exact turn. The user action owns the terminal
            # meaning; late cancellation noise must not become a model error.
            self.events.publish(
                session_id,
                "turn_completed",
                {
                    "status": "aborted",
                    "aborted": True,
                    "terminalEvent": "abort_failure_race",
                },
                turn_id=turn_id,
            )
            return
        self.events.publish(
            session_id,
            "turn_failed",
            {
                "error": message,
                **classification.event_payload(),
                **final_failure_context,
            },
            turn_id=turn_id,
        )

    def _handle_host_exit(self, exit_code: int | None, error: str, *,
                          source_client: PiRuntimeHostClient | None = None) -> None:
        with self._lock:
            classifications = [(identity, call) for identity, call in self._classifications.items()
                               if source_client is None or call.client is source_client]
        for identity, call in classifications:
            self._release_classification(identity, call)
        with self._lock:
            if source_client is not None and self._client is not source_client:
                return
            if self._intentional_stop:
                return
            message = redact_runtime_text(error or f"Pi Runtime Host exited with code {exit_code}")
            active = [(session_id, state.turn_id) for session_id, state in self._states.items()
                      if state.turn_id and state.runtime_engine != "durable"]
            for state in self._states.values():
                if state.abort_timer is not None:
                    state.abort_timer.cancel()
                if state.settle_timer is not None:
                    state.settle_timer.cancel()
            self._client = None
            self._open_sessions.clear()
            self._states = {session_id: state for session_id, state in self._states.items()
                if state.runtime_engine == "durable" and (state.turn_id or state.compaction_target or state.prompt_admission_in_flight)}
            for state in self._states.values():
                state.recoverable = True
            self._status = "faulted"
            self._last_error = message
        for session_id, turn_id in active:
            self.sessions.set_status(session_id, "faulted", last_message_preview=message)
            self.events.publish(
                session_id,
                "turn_failed",
                {
                    "error": message,
                    "failureKind": "runtime_host_exit",
                    "exitCode": exit_code,
                },
                turn_id=turn_id,
            )

    def _abort_fallback_expired(self, session_id: str, turn_id: str) -> None:
        # Pi's session.abort receipt is a settled cancellation receipt, not a
        # quick ACK: it may wait for tools/providers to drain. The Runtime Host
        # dispatches control-plane requests concurrently, so first distinguish
        # one slow Session cancellation from an actually unresponsive shared
        # Host. A responsive Host must never be killed because one child Agent
        # missed PAW's short UI feedback deadline.
        with self._lock:
            state = self._states.get(session_id)
            if state is None or state.turn_id != turn_id:
                return
            durable_engine = state.runtime_engine == "durable"
        if durable_engine:
            self._reconcile_durable_settlement(session_id, turn_id)
            return
        with self._lock:
            state = self._states.get(session_id)
            if state is None or state.turn_id != turn_id:
                return
            state.abort_timer = None
            client = self._client
        if client is None or not client.running:
            return
        host_responsive = False
        try:
            health = client.send("health", {}, timeout=1.0)
            host_responsive = health.get("ok") is True
        except Exception:
            # The process-level kill gate remains the bounded recovery path
            # when even the independent health lane does not answer.
            host_responsive = False
        runtime_status = "ready"
        shared_host_protected = False
        with self._lock:
            state = self._states.get(session_id)
            if state is not None and state.turn_id == turn_id:
                # ``health`` is dispatched concurrently inside the Host, but
                # its reply still shares the serialized stdout JSONL lane with
                # every Session event. A saturated output lane can therefore
                # make the health RPC time out while unrelated Session turns
                # are still alive. Never turn that ambiguous signal into a
                # process-wide kill that sacrifices active peers. When there
                # are no active peers, the existing kill gate remains the
                # bounded recovery path for a genuinely stuck Host.
                shared_host_protected = any(
                    candidate_session_id != session_id
                    and bool(candidate.turn_id)
                    for candidate_session_id, candidate in self._states.items()
                )
                if state.settle_timer is not None:
                    state.settle_timer.cancel()
                    state.settle_timer = None
                if len(state.retired_turn_ids) >= 64:
                    state.retired_turn_ids.pop()
                state.retired_turn_ids.add(turn_id)
                if len(self._retired_host_turns) >= 256:
                    self._retired_host_turns.pop()
                self._retired_host_turns.add((session_id, turn_id))
                state.turn_id = ""
                state.client_message_id = ""
                state.stream_pi_message_id = ""
                self._clear_turn_projection_locked(state)
                state.abort_requested_turn_id = ""
                state.pending_approvals.clear()
                state.pending_reviews.clear()
                state.pending_ui_requests.clear()
                if (
                    (host_responsive or shared_host_protected)
                    and self._client is client
                    and client.running
                ):
                    self._status = (
                        "busy"
                        if any(candidate.turn_id for candidate in self._states.values())
                        else "ready"
                    )
                    runtime_status = self._status
                    self._schedule_idle_locked()
            else:
                # The exact turn settled while the health probe was in flight.
                return
        self.sessions.set_status(
            session_id,
            "idle",
            last_message_preview="已停止。",
        )
        self.events.publish(
            session_id,
            "turn_completed",
            {
                "status": "aborted",
                "aborted": True,
                "terminalEvent": (
                    "abort_timeout_isolated"
                    if host_responsive or shared_host_protected
                    else "abort_timeout_kill"
                ),
            },
            turn_id=turn_id,
        )
        if host_responsive or shared_host_protected:
            self.events.publish(
                session_id,
                "status_changed",
                {
                    "status": "idle",
                    "runtimeStatus": runtime_status,
                    "escalated": False,
                    # Pi is still draining the already-requested cancellation;
                    # late events stay fenced to the retired turn above.
                    "cancellationPending": True,
                    "hostHealthConfirmed": host_responsive,
                    "sharedHostProtected": shared_host_protected,
                },
                turn_id=turn_id,
            )
            return
        receipt = self._kill_gate.request_kill(
            client.host_identity,
            request_kind="cancel_timeout",
            requested_by=f"session:{session_id}",
            reason=f"session.abort did not settle turn {turn_id}",
            now_ms=int(time.time() * 1000),
        )
        with self._lock:
            self._last_kill_receipt = dict(receipt)
            if self._status != "faulted":
                self._status = "stopping"
        self.events.publish(
            session_id,
            "status_changed",
            {
                # The Session turn is already durably terminal above. The
                # Runtime Host process may still be stopping, but projecting
                # that process state as Session "aborting" would reopen the
                # completed turn in the frontend reducer and recreate the
                # permanent busy/unstoppable UI this fallback exists to fix.
                "status": "idle",
                "runtimeStatus": "stopping",
                "escalated": True,
                "killReceiptId": receipt["killReceiptId"],
                "pendingTargets": receipt["pendingTargets"],
            },
            turn_id=turn_id,
        )

    def _schedule_idle_locked(self) -> None:
        self._cancel_idle_locked()
        if (
            self.config.idle_timeout_seconds <= 0
            or self._active_completion_ids
            or self._classifications
            or any(
                state.turn_id or state.compaction_target or state.prompt_admission_in_flight
                for state in self._states.values()
            )
        ):
            return
        timer = threading.Timer(self.config.idle_timeout_seconds, self.stop)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _cancel_idle_locked(self) -> None:
        timer = self._idle_timer
        self._idle_timer = None
        if timer is not None:
            timer.cancel()
