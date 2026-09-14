from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from rag_ime.agent_events import AgentEventHub
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
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.host_client import PiRuntimeHostClient
from rag_ime.pi.protocols import PI_HOST_PROTOCOL_VERSION
from rag_ime.pi.ui_requests import grouped_question_request, public_ui_request, resolve_ui_response
from rag_ime.pi.transcript_io import (
    read_recent_transcript_tail, transcript_boundary_sha256,
    DURABLE_TRANSCRIPT_MAX_LINE_BYTES,
)
from rag_ime.pi.event_projection import (
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
from rag_ime.secure_files import regular_reader, unlink_file
from rag_ime.team.execution import ExecutionLauncher


__all__ = [
    "DurableHistoryUnavailable",
    "PiRuntimeHostManager",
]



_MODEL_CATALOG_CACHE_SECONDS = 1_800.0
_PROMPT_TIMEOUT_SECONDS = 60.0 * 60.0
_DURABLE_TRANSCRIPT_MAX_BYTES = 64 * 1024 * 1024
_DURABLE_TRANSCRIPT_MAX_LINES = 200_000
_SESSION_RESOURCE_SNAPSHOT_SCHEMA = "rag-ime.pi-session-resource-snapshot.v1"


class DurableHistoryUnavailable(AgentRuntimeError):
    """A durable transcript exists or was expected but cannot be trusted.

    ``missing`` distinguishes a normal cold Session with no transcript yet
    from a path that was present but failed the descriptor-relative safety or
    transcript-integrity checks.  Callers rendering a cold projection can
    keep the former as an empty history while exposing the latter as an
    explicit unavailable state.
    """

    def __init__(self, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.missing = bool(missing)


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
    turn_id: str = ""
    client_message_id: str = ""
    prompt_admission_in_flight: bool = False
    admission_client_message_id: str = ""
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
        candidate_skill_paths_provider: Callable[[Mapping[str, object]], list[str]] | None = None,
        execution_launcher: ExecutionLauncher | None = None,
        kill_gate: RuntimeHostKillGate | None = None,
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
        self._candidate_skill_paths_provider = candidate_skill_paths_provider
        self._execution_launcher = execution_launcher
        self._lifecycle_lock = threading.RLock()
        self._retired = False
        self._model_catalog_lock = threading.Lock()
        self._lock = threading.RLock()
        self._recent_projection_refreshes: set[str] = set()
        self._recent_projection_threads: set[threading.Thread] = set()
        self._client: PiRuntimeHostClient | None = None
        self._states: dict[str, _HostedSessionState] = {}
        self._open_sessions: set[str] = set()
        self._active_completion_ids: set[str] = set()
        self._completion_sinks: dict[str, Callable[[str], None]] = {}
        self._status = "stopped" if config.enabled else "disabled"
        self._last_error = ""
        self._host_capabilities: dict[str, object] = {}
        self._idle_timer: threading.Timer | None = None
        self._intentional_stop = False
        self._owner_instance_id = f"runtime:{uuid.uuid4()}"
        self._kill_gate = kill_gate or RuntimeHostKillGate(self.sessions.db_path)
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

    def runtime_status(self) -> dict[str, object]:
        installed = (
            (
                self.config.execution_spec is not None
                and self._execution_launcher is not None
            )
            if self.config.isolated_execution
            else self.config.executable is not None
            and self.config.executable.expanduser().is_file()
        )
        latest_kill_receipt = None
        if self._last_kill_receipt:
            latest_kill_receipt = self._kill_gate.receipt(
                str(self._last_kill_receipt["killReceiptId"])
            )
        with self._lock:
            busy = sorted(
                session_id
                for session_id, state in self._states.items()
                if state.turn_id
                or (
                    state.prompt_admission_in_flight
                    and not state.abort_pending_admission
                )
            )
            open_sessions = sorted(self._open_sessions)
            active_completions = sorted(self._active_completion_ids)
            status = "busy" if busy or active_completions else self._status
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
                "sessionSnapshot": True,
                "settledEvents": True,
                "statelessCompletion": (
                    bool(capabilities.get("statelessCompletion"))
                    if host_negotiated
                    else installed and str(self.config.protocol_version or "") == PI_HOST_PROTOCOL_VERSION
                ),
                "transientContext": bool(capabilities.get("transientContext")),
                "sessionSkillAllowlist": bool(
                    capabilities.get("sessionSkillAllowlist")
                ),
                "persistentDebugContext": bool(capabilities.get("persistentDebugContext")),
                "runtimePrimitives": runtime_primitive_capabilities(
                    capabilities.get("runtimePrimitives")
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
        if self._retired:
            raise PiRuntimeError('Pi Runtime manager was retired; a new execution binding is required')
        with self._lock:
            if self._client is not None and self._client.running:
                return self._client
        if not self.config.enabled:
            raise PiRuntimeError("Pi runtime is disabled")
        if self.config.executable is None and not self.config.isolated_execution:
            raise PiRuntimeError(self.config.installation_error or "managed Pi runtime is not installed")
        if self.config.isolated_execution and self._execution_launcher is None:
            raise PiRuntimeError("isolated team execution requires a trusted Host launcher")
        self.reconcile_runtime_hosts()
        client = PiRuntimeHostClient(
            self.config,
            on_event=self._handle_host_event,
            on_exit=self._handle_host_exit,
            kill_gate=self._kill_gate,
            owner_instance_id=self._owner_instance_id,
            launcher=self._execution_launcher,
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
            try:
                client.stop()
            except Exception as cleanup_error:
                # Preserve the admission failure while retaining the cleanup
                # verification error in diagnostics.  A failed container
                # removal must remain observable without masking the original
                # Host-start cause.
                with self._lock:
                    self._last_error = redact_runtime_text(
                        f"{exc}; isolated cleanup failed: {cleanup_error}"
                    )
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
    ) -> dict[str, object]:
        with self._lifecycle_lock:
            if not self.config.model_configured:
                raise PiRuntimeError(
                    self.config.model_configuration_error
                    or "Pi model is not configured"
                )
            client = self._host()
            session = dict(self.sessions.get(session_id))
            binding = self.sessions.runtime_binding(session_id)
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
                with self._lock:
                    self._schedule_idle_locked()
                return {
                    "state": snapshot,
                    "resourceSnapshot": resource_snapshot,
                    "reused": True,
                }

            host_roots = [
                str(value)
                for value in session.get("workspaceRoots") or []
                if str(value).strip()
            ]
            # The persisted Session keeps server-side checkout paths.  A team
            # Host sees only the attempt mount, so translate every root before
            # choosing cwd and fail closed instead of falling back to the
            # Host's agent directory (which would be outside the workspace).
            roots = self.config.workspace_paths_for_host(host_roots)
            cwd = roots[0] if roots else str(self.config.agent_dir)
            provider, model_id = self.config.resolved_model_reference(session)
            session_file = str(
                (binding or {}).get("transcriptRef")
                or session.get("sessionFile")
                or ""
            ).strip()
            if session_file:
                if self.config.isolated_execution:
                    session_file = self.config.container_session_path(session_file)
                else:
                    # Fork bindings store canonical paths, while the Host
                    # checks against its configured directory spelling. A
                    # managed root may be a symlink (for example, sessions on
                    # an external disk). Map only files inside that same
                    # physical root back to the Host's spelling; leave
                    # outside paths for its rejection gate.
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
            specialized_session = str(session.get("toolProfileVersion") or "") in {
                "ime-surface-v1", "voice-refinement-v1", MEMORY_CURATION_TOOL_PROFILE,
            }
            if not specialized_session:
                if resource_snapshot is not None:
                    # A pre-existing binding without prompt settings keeps its original policy.
                    prompt_settings = as_mapping(resource_snapshot.get("promptSettings")) or None
                elif self._prompt_settings_provider is not None:
                    prompt_settings = normalize_prompt_settings(self._prompt_settings_provider(session))
            if resource_snapshot is None:
                resource_snapshot = _session_resource_snapshot(skill_allowlist)
                if prompt_settings is not None:
                    resource_snapshot["promptSettings"] = dict(prompt_settings)
                candidate_paths_provider = self._candidate_skill_paths_provider
                paths = candidate_paths_provider(session) if candidate_paths_provider is not None else []
                if paths:
                    if self.config.isolated_execution:
                        raise PiRuntimeError(
                            "isolated team Runtime does not mount candidate Skills"
                        )
                    resource_snapshot["candidateSkillPaths"] = self._candidate_skill_paths(paths, cwd)
            if self.config.isolated_execution and resource_snapshot.get("candidateSkillPaths"):
                raise PiRuntimeError(
                    "isolated team Runtime does not mount candidate Skills"
                )
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
                "systemPrompt": self.config.system_prompt_for_session(session, prompt_settings=prompt_settings),
                "toolManifest": (
                    []
                    if memory_curation_session
                    else self.tool_catalog(session_id)
                ),
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
                }
            )
            transcript_ref = str(snapshot.get("sessionFile") or "")
            if transcript_ref and self.config.isolated_execution:
                transcript_ref = self.config.host_session_path(transcript_ref)
            bound = self.sessions.bind_runtime_session(
                session_id,
                driver_id=self.driver_id,
                runtime_kind=self.runtime_kind,
                external_session_id=str(
                    snapshot.get("piSessionId") or session_id
                ),
                transcript_ref=transcript_ref,
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

    def retire_recovered_turn(
        self,
        session_id: str,
        expected_turn_id: str,
    ) -> dict[str, object]:
        """Explicitly retire one interrupted durable turn after Host restart."""

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
    ) -> dict[str, object]:
        """Fence Stop before prompt preparation reaches the Pi Host.

        The application service can spend noticeable time assembling context
        before ``prompt()`` is called. Reserving that admission here gives a
        concurrent Stop request one Runtime-owned state to mark, without
        inventing a second turn or cancellation state machine.
        """

        normalized_client_message_id = str(client_message_id).strip()
        with self._lock:
            state = self._states.setdefault(
                session_id,
                _HostedSessionState(),
            )
            same_reservation = (
                state.prompt_admission_in_flight
                and state.admission_client_message_id
                == normalized_client_message_id
            )
            if state.turn_id or (
                state.prompt_admission_in_flight
                and not same_reservation
            ):
                raise PiRuntimeTurnConflict(
                    "Pi 正在处理上一轮，请等待结束或停止完成后再发送"
                )
            if not same_reservation:
                state.prompt_admission_in_flight = True
                state.admission_client_message_id = (
                    normalized_client_message_id
                )
                state.abort_pending_admission = False
                state.admission_abort_dispatched = False
                state.prompt_dispatched = False
                state.prompt_dispatch_signal.clear()
            self._cancel_idle_locked()
        self.sessions.set_status(session_id, "busy")
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
                or not state.prompt_admission_in_flight
                or state.admission_client_message_id
                != normalized_client_message_id
            ):
                return False
            state.prompt_admission_in_flight = False
            state.admission_client_message_id = ""
            state.abort_pending_admission = False
            state.admission_abort_dispatched = False
            state.prompt_dispatched = False
            state.prompt_dispatch_signal.set()
            self._schedule_idle_locked()
        self.sessions.set_status(session_id, "idle")
        return True

    def prompt(
        self,
        session_id: str,
        message: str,
        *,
        images: list[Mapping[str, str]] | None = None,
        client_message_id: str = "",
        delivery: str = "prompt",
    ) -> dict[str, object]:
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
            if state.turn_id or (
                state.prompt_admission_in_flight and not pre_reserved
            ):
                raise PiRuntimeTurnConflict(
                    "Pi 正在处理上一轮，请等待结束或停止完成后再发送"
                )
            self._cancel_idle_locked()
            if not pre_reserved:
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
        )
        dispatch_attempted = False

        def mark_prompt_dispatched() -> None:
            nonlocal dispatch_attempted
            self._mark_prompt_dispatched(
                session_id,
                normalized_client_message_id,
            )
            # This callback runs under the Host client's write lock immediately
            # before its JSONL write.  Once it returns, a missing response is an
            # acceptance-unknown outcome, never proof that Pi rejected the turn.
            dispatch_attempted = True

        try:
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
                if not acceptance_unknown:
                    state.prompt_admission_in_flight = False
                    state.admission_client_message_id = ""
                    state.abort_pending_admission = False
                    state.admission_abort_dispatched = False
                    state.prompt_dispatched = False
                    state.prompt_dispatch_signal.set()
            if (
                isinstance(exc, PiRuntimeCommandRejected)
                and exc.host_error_code
                == "PROMPT_ADMISSION_CANCELLED"
            ):
                self.sessions.set_status(
                    session_id,
                    "idle",
                    last_message_preview="已停止。",
                )
            elif acceptance_unknown:
                # The Host can complete the real turn before this call notices
                # that its ACK was lost.  Keep the exact admission alive until
                # a Host event binds it (or application recovery finds the
                # durable message), and never invent an empty-turn failure.
                if isinstance(exc, PiRuntimeCommandAcceptanceUnknown):
                    raise
                raise PiRuntimeCommandAcceptanceUnknown(str(exc)) from exc
            else:
                # No Pi turn exists for a proven rejection.  The HTTP command
                # receipt owns the failed optimistic message; publishing a
                # turn_failed with an empty id would create an unrelated
                # `unscoped` failure in every connected frontend.
                self.sessions.set_status(session_id, "idle")
            raise
        turn_id = str(accepted.get("turnId") or "")
        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
            abort_after_admission = state.abort_pending_admission
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
            if not already_retired:
                state.turn_id = turn_id
                state.client_message_id = normalized_client_message_id
                self._status = "busy"
        if not already_retired:
            self.sessions.set_status(
                session_id,
                "busy",
                last_message_preview=public_prompt_preview,
            )
            if not already_aborting and not abort_after_admission:
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
                    self.abort(session_id)
                except Exception:
                    pass
        else:
            self.sessions.set_status(
                session_id,
                "idle",
                last_message_preview="已停止。",
            )
        result: dict[str, object] = {
            "accepted": True,
            "turnId": turn_id,
            "piEntryId": turn_id,
            "response": accepted,
        }
        if abort_after_admission or already_aborting or already_retired:
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
        if (
            disposition == "completed"
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

    def messages(self, session_id: str) -> list[dict[str, object]]:
        return list(self.session_snapshot(session_id).get("messages") or [])

    def _scoped_transcript_path(
        self,
        raw_path: str,
        *,
        transcript_root: Path | None = None,
    ) -> Path | None:
        """Build a canonical scoped path without following the transcript chain.

        Persisted paths can use a platform alias such as macOS ``/var`` while
        the Runtime's configured root is already canonicalized to
        ``/private/var``.  Normalize only that known root spelling, preserve
        the user/Host supplied relative components, and let
        :func:`regular_reader` open every component with O_NOFOLLOW.  This
        keeps both directory and final-file symlink swaps fail-closed.
        """

        value = str(raw_path or "").strip()
        if not value:
            return None
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return None
        configured_root = Path(
            transcript_root
            if transcript_root is not None
            else self.config.session_dir
        ).expanduser()
        try:
            root_absolute = Path(os.path.abspath(str(configured_root)))
            root_canonical = configured_root.resolve(strict=False)
            candidate_absolute = Path(os.path.abspath(str(candidate)))
        except (OSError, RuntimeError):
            return None
        if root_canonical == Path("/"):
            return None
        relative: Path | None = None
        for root in (root_absolute, root_canonical):
            try:
                relative = candidate_absolute.relative_to(root)
            except ValueError:
                continue
            break
        if relative is None or not relative.parts:
            return None
        if any(part in {".", ".."} for part in relative.parts):
            return None
        return root_canonical / relative

    @staticmethod
    def _secure_transcript_stat(transcript: Path) -> os.stat_result:
        """Stat the same regular-file boundary used by transcript readers."""

        with regular_reader(transcript) as source:
            return os.fstat(source.fileno())

    def _durable_history_snapshot(
        self,
        session_id: str,
        *,
        transcript_root: Path | None = None,
        strict: bool = False,
    ) -> dict[str, object] | None:
        """Read an idle managed Pi transcript without opening Provider context."""

        try:
            session = self.sessions.get(session_id)
            binding = self.sessions.runtime_binding(session_id) or {}
            raw_path = str(
                binding.get("transcriptRef")
                or session.get("sessionFile")
                or ""
            ).strip()
            if not raw_path and transcript_root is not None:
                safe_session_id = str(session_id)
                if Path(safe_session_id).name == safe_session_id:
                    raw_path = str(
                        Path(transcript_root).expanduser()
                        / f"{safe_session_id}.jsonl"
                    )
            transcript = self._scoped_transcript_path(
                raw_path,
                transcript_root=transcript_root,
            )
            if transcript is None:
                if strict and raw_path:
                    raise DurableHistoryUnavailable(
                        "durable Session transcript is outside its scoped history root"
                    )
                return None
            entries: list[dict[str, object]] = []
            try:
                with regular_reader(transcript) as source:
                    info = os.fstat(source.fileno())
                    if info.st_size > _DURABLE_TRANSCRIPT_MAX_BYTES:
                        if strict:
                            raise DurableHistoryUnavailable(
                                "durable Session transcript exceeds its safety limit"
                            )
                        return None
                    for index, line in enumerate(source):
                        if (
                            index >= _DURABLE_TRANSCRIPT_MAX_LINES
                            or len(line) > DURABLE_TRANSCRIPT_MAX_LINE_BYTES
                        ):
                            if strict:
                                raise DurableHistoryUnavailable(
                                    "durable Session transcript exceeds its safety limit"
                                )
                            return None
                        try:
                            value = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                            if strict:
                                raise DurableHistoryUnavailable(
                                    "durable Session transcript is malformed"
                                ) from exc
                            continue
                        if isinstance(value, Mapping):
                            entries.append(dict(value))
                        elif strict:
                            raise DurableHistoryUnavailable(
                                "durable Session transcript contains an invalid entry"
                            )
            except FileNotFoundError:
                # A not-yet-created Session transcript is a normal cold state.
                return None
            except DurableHistoryUnavailable:
                raise
            except (OSError, ValueError, TypeError) as exc:
                if strict:
                    raise DurableHistoryUnavailable(
                        "durable Session transcript could not be read safely"
                    ) from exc
                return None
            if not entries:
                if strict:
                    raise DurableHistoryUnavailable(
                        "durable Session transcript is empty or malformed"
                    )
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
                if strict:
                    raise DurableHistoryUnavailable(
                        "durable Session transcript belongs to a different Pi Session"
                    )
                return None
            leaf_id = str(binding.get("branchAnchor") or "")
            binding_updated_at_ms = as_integer(binding.get("updatedAtMs"))
            # The binding is refreshed when the Session opens or rewinds. If
            # Pi appended newer entries after that point, the append-only
            # transcript's final entry is the current selected leaf and avoids
            # projecting a stale pre-turn anchor.
            if info.st_mtime_ns // 1_000_000 > binding_updated_at_ms:
                leaf_id = next(
                    (
                        str(entry.get("id") or "")
                        for entry in reversed(entries)
                        if str(entry.get("id") or "")
                    ),
                    leaf_id,
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
        except DurableHistoryUnavailable:
            raise
        except (KeyError, OSError, ValueError, TypeError) as exc:
            if strict:
                raise DurableHistoryUnavailable(
                    "durable Session transcript could not be projected safely"
                ) from exc
            return None

    def _recent_durable_history_messages(
        self,
        session_id: str,
        *,
        transcript_root: Path | None = None,
        strict: bool = False,
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

        try:
            session = self.sessions.get(session_id)
            binding = self.sessions.runtime_binding(session_id) or {}
            raw_path = str(
                binding.get("transcriptRef")
                or session.get("sessionFile")
                or ""
            ).strip()
            if not raw_path and transcript_root is not None:
                safe_session_id = str(session_id)
                if Path(safe_session_id).name == safe_session_id:
                    raw_path = str(
                        Path(transcript_root).expanduser()
                        / f"{safe_session_id}.jsonl"
                    )
            transcript = self._scoped_transcript_path(
                raw_path,
                transcript_root=transcript_root,
            )
            if transcript is None:
                if strict and raw_path:
                    raise DurableHistoryUnavailable(
                        "durable Session transcript is outside its scoped history root"
                    )
                return None
            try:
                info = self._secure_transcript_stat(transcript)
            except FileNotFoundError:
                return None
            except (OSError, ValueError, TypeError) as exc:
                if strict:
                    raise DurableHistoryUnavailable(
                        "durable Session transcript could not be read safely"
                    ) from exc
                return None
            if info.st_size > _DURABLE_TRANSCRIPT_MAX_BYTES:
                if strict:
                    raise DurableHistoryUnavailable(
                        "durable Session transcript exceeds its safety limit"
                    )
                return None
            try:
                tail = read_recent_transcript_tail(transcript, int(info.st_size))
            except FileNotFoundError:
                return None
            except (OSError, ValueError, TypeError, UnicodeError) as exc:
                if strict:
                    raise DurableHistoryUnavailable(
                        "durable Session transcript could not be read safely"
                    ) from exc
                return None
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
                if strict:
                    raise DurableHistoryUnavailable(
                        "durable Session transcript belongs to a different Pi Session"
                    )
                return None
            leaf_id = str(binding.get("branchAnchor") or "")
            binding_updated_at_ms = as_integer(binding.get("updatedAtMs"))
            if info.st_mtime_ns // 1_000_000 > binding_updated_at_ms:
                leaf_id = _latest_entry_id(entries)
            elif leaf_id and not any(
                str(entry.get("id") or "") == leaf_id
                for entry in entries
            ):
                return None
            if not leaf_id:
                leaf_id = _latest_entry_id(entries)
            return recent_messages_from_proven_tail(
                entries,
                leaf_id=leaf_id,
                header_id=str(header.get("id") or ""),
            )
        except DurableHistoryUnavailable:
            raise
        except (KeyError, OSError, ValueError, TypeError, UnicodeError) as exc:
            if strict:
                raise DurableHistoryUnavailable(
                    "durable Session transcript could not be projected safely"
                ) from exc
            return None

    def _inspection_snapshot(
        self,
        session_id: str,
        *,
        durable_fallback: bool = True,
    ) -> dict[str, object]:
        """Read one Session without rebuilding context when it is resident.

        Dynamic memory and Room projections are compiled when a Session opens
        or rebinds. Historical display is instead reconstructed from Pi's
        managed durable JSONL before taking the Host lifecycle lock, so a slow
        command/context open cannot hold the conversation rail behind it.
        """

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
            client = self._host()
            with self._lock:
                already_open = session_id in self._open_sessions
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

    def session_snapshot(
        self,
        session_id: str,
        *,
        durable_only: bool = False,
        transcript_root: Path | None = None,
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        if durable_only:
            # The team cold-restart projection deliberately shares this
            # manager's durable branch/timeline projection while forbidding
            # every Host lifecycle transition.
            snapshot = self._durable_history_snapshot(
                session_id,
                transcript_root=transcript_root,
                strict=True,
            )
            if snapshot is None:
                raise DurableHistoryUnavailable(
                    "durable Session transcript is unavailable",
                    missing=True,
                )
        elif session.get("evaluationSnapshot") is True:
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
                snapshot = self._inspection_snapshot(session_id)
            except AgentRuntimeError:
                # A transient Host failure is not evidence that the Session has no
                # history. The append-only Pi transcript remains readable even when
                # Provider context inspection is unavailable; use it as the
                # recovery source instead of publishing a successful empty
                # snapshot that would make the API and UI erase the conversation.
                snapshot = self._durable_history_snapshot(session_id)
                if snapshot is None:
                    raise
        raw_messages = snapshot.get("messages") if isinstance(snapshot.get("messages"), list) else []
        raw_entries = snapshot.get("entries") if isinstance(snapshot.get("entries"), list) else []
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
                if fingerprint == last_assistant_fingerprint:
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
        telemetry = snapshot.get("telemetry")
        raw_queue = as_mapping(snapshot.get("messageQueue"))
        message_queue = {
            "steering": public_message_queue(raw_queue.get("steering")),
            "followUp": public_message_queue(raw_queue.get("followUp")),
            "steeringMode": str(raw_queue.get("steeringMode") or ""),
            "followUpMode": str(raw_queue.get("followUpMode") or ""),
        }
        return {
            "messages": result,
            "toolHistoryEvents": tool_history_events,
            "telemetry": dict(telemetry) if isinstance(telemetry, Mapping) else None,
            "messageQueue": message_queue,
        }

    def recent_session_snapshot(
        self,
        session_id: str,
        *,
        durable_only: bool = False,
        transcript_root: Path | None = None,
    ) -> dict[str, object]:
        """Project a bounded durable first paint without contacting Pi Host.

        The append-only transcript is the only authority used here. A missing
        transcript is represented by ``DurableHistoryUnavailable(missing=True)``
        for callers that need to distinguish an empty cold Session from an
        unsafe or corrupt existing file. This read never falls through to the
        live Host or the full historical Tool-event reconstruction.
        """

        projection_identity = (
            None
            if durable_only
            else self._recent_projection_identity(session_id)
        )
        cached_projection = (
            None
            if durable_only
            else self._recent_projected_messages(
                session_id,
                projection_identity,
            )
        )
        if cached_projection is not None:
            cached_messages, cached_tool_history, exact = cached_projection
            if not exact:
                self._schedule_recent_projection_refresh(
                    session_id,
                    projection_identity,
                )
            return {
                "messages": cached_messages,
                "toolHistoryEvents": cached_tool_history,
            }

        recent_candidate = self._recent_durable_history_messages(
            session_id,
            transcript_root=transcript_root,
            strict=durable_only,
        )
        if recent_candidate is None:
            durable = self._durable_history_snapshot(
                session_id,
                transcript_root=transcript_root,
                strict=durable_only,
            )
            if durable is None:
                if durable_only:
                    raise DurableHistoryUnavailable(
                        "durable Session transcript is unavailable",
                        missing=True,
                    )
                return {"messages": []}
            raw_messages = durable.get("messages")
            if not isinstance(raw_messages, list):
                if durable_only:
                    raise DurableHistoryUnavailable(
                        "durable Session transcript has no valid message projection"
                    )
                return {"messages": []}
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
                tool_history_events = recent_tool_history_events(
                    raw_messages,
                    raw_entries=raw_entries,
                    projected_messages=messages,
                    session_id=session_id,
                )
                if not durable_only:
                    self._schedule_recent_projection_refresh(
                        session_id,
                        projection_identity,
                    )
                return {
                    "messages": messages,
                    "toolHistoryEvents": tool_history_events,
                }
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
        if not durable_only:
            self._save_recent_message_projection(
                session_id,
                projection_identity,
                messages,
                tool_history_events,
            )
        return {
            "messages": messages,
            "toolHistoryEvents": tool_history_events,
        }

    def _recent_projection_identity(
        self,
        session_id: str,
        *,
        transcript_root: Path | None = None,
    ) -> dict[str, object] | None:
        """Resolve the exact immutable file view that may reuse a projection."""

        try:
            session = self.sessions.get(session_id)
            binding = self.sessions.runtime_binding(session_id) or {}
            raw_path = str(
                binding.get("transcriptRef")
                or session.get("sessionFile")
                or ""
            ).strip()
            if not raw_path and transcript_root is not None:
                safe_session_id = str(session_id)
                if Path(safe_session_id).name == safe_session_id:
                    raw_path = str(
                        Path(transcript_root).expanduser()
                        / f"{safe_session_id}.jsonl"
                    )
            transcript = self._scoped_transcript_path(
                raw_path,
                transcript_root=transcript_root,
            )
            if transcript is None:
                return None
            info = self._secure_transcript_stat(transcript)
            if info.st_size > _DURABLE_TRANSCRIPT_MAX_BYTES:
                return None
            return {
                "transcriptRef": transcript.as_posix(),
                "externalSessionId": str(
                    binding.get("externalSessionId")
                    or session.get("piSessionId")
                    or ""
                ),
                "branchAnchor": str(binding.get("branchAnchor") or ""),
                "transcriptDevice": int(info.st_dev),
                "transcriptInode": int(info.st_ino),
                "transcriptSize": int(info.st_size),
                "transcriptMtimeNs": int(info.st_mtime_ns),
                "transcriptBoundarySha256": transcript_boundary_sha256(
                    transcript,
                    int(info.st_size),
                ),
            }
        except (KeyError, OSError, ValueError):
            return None

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
            [dict(message) for message in messages],
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
                if self.config.isolated_execution:
                    # Pi reports the path in its worker namespace. Persist the
                    # host-side scoped path so later durable-history reads and
                    # the workspace coordinator never see /run/paw paths.
                    transcript_ref = self.config.host_session_path(transcript_ref)
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

    def invoke_command(self, session_id: str, command: str) -> dict[str, object]:
        text = str(command).strip()
        if not text.startswith("/") or "\n" in text or "\r" in text:
            raise ValueError("Pi Package command must be one slash-command line")
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
        selected = public_pi_model(
            self._require_client().send(
                "session.model.set",
                params,
            )
        )
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

    def tool_catalog(self, session_id: str) -> list[dict[str, object]]:
        session = dict(self.sessions.get(session_id))
        if self._tool_manifest_provider is None:
            return []
        return [dict(item) for item in self._tool_manifest_provider(session)]

    def _mark_prompt_dispatched(
        self,
        session_id: str,
        admission_client_message_id: str,
    ) -> None:
        """Atomically fence Stop against the Host JSONL hand-off.

        ``PiRuntimeHostClient`` invokes this callback while its JSONL write
        lock is held. A background abort therefore cannot overtake the prompt
        record. If Stop won the state lock, raising here prevents the prompt
        record from being written. If this callback wins, a later Stop sees
        ``prompt_dispatched`` and follows Pi's native abort path.
        """
        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
            if (
                state.prompt_admission_in_flight
                and state.admission_client_message_id
                == admission_client_message_id
                and state.abort_pending_admission
            ):
                raise PiRuntimeCommandRejected(
                    "当前消息已停止，未发送给 Pi",
                    host_error_code="PROMPT_ADMISSION_CANCELLED",
                )
            state.prompt_dispatched = True
            state.prompt_dispatch_signal.set()

    def _deliver_pending_admission_abort(
        self,
        session_id: str,
        admission_client_message_id: str,
    ) -> None:
        """Use Pi's native abort while prompt preflight ACK is still pending."""

        try:
            self._require_client().send(
                "session.abort",
                {"sessionId": session_id},
                timeout=1.0,
            )
        except Exception:
            with self._lock:
                state = self._states.get(session_id)
                if (
                    state is not None
                    and state.admission_client_message_id
                    == admission_client_message_id
                ):
                    state.admission_abort_dispatched = False
            self.events.publish(
                session_id,
                "status_changed",
                {
                    "status": "aborting",
                    "pendingAdmission": True,
                    "escalated": True,
                },
            )

    def abort(self, session_id: str) -> dict[str, object]:
        with self._lock:
            state = self._states.setdefault(session_id, _HostedSessionState())
            turn_id = state.turn_id
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
            self.events.publish(
                session_id,
                "status_changed",
                {"status": "aborting"},
                turn_id=turn_id,
            )
        client = self._require_client()
        try:
            result = client.send(
                "session.abort",
                {"sessionId": session_id},
                timeout=1.0,
            )
        except Exception:
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

    def retire(self) -> None:
        """Permanently close this manager's Host admission before stopping it.

        A discarded Team attempt may still have callers holding its manager.
        Unlike the restartable personal ``stop`` lifecycle, those references
        must never reopen a Host, including during Package preflight.
        """
        with self._lifecycle_lock:
            self._retired = True
            self.stop()

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
                self._completion_sinks.clear()
                self._states.clear()
                self._status = "stopped" if self.config.enabled else "disabled"
                projection_threads = tuple(self._recent_projection_threads)
            cleanup_error: BaseException | None = None
            if client is not None:
                try:
                    client.stop()
                except BaseException as exc:
                    cleanup_error = exc
            current_thread = threading.current_thread()
            for thread in projection_threads:
                if thread is not current_thread:
                    thread.join(timeout=2)
            for session_id in session_ids:
                try:
                    self.sessions.set_status(session_id, "idle")
                except KeyError:
                    pass
            if cleanup_error is not None:
                raise cleanup_error

    def close_session(self, session_id: str) -> bool:
        """Retire one idle hosted Session without restarting the shared Host."""

        normalized = str(session_id or "").strip()
        if not normalized:
            raise ValueError("session_id is required")
        with self._lifecycle_lock:
            with self._lock:
                state = self._states.get(normalized)
                if state is not None and state.turn_id:
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
    ) -> None:
        if envelope.get("protocolVersion") != PI_HOST_PROTOCOL_VERSION or envelope.get("event") not in {
            "agent.event",
            "runtime.notice",
        }:
            return
        raw = dict(as_mapping(envelope.get("payload")))
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
            # Queue notifications are observations, including after opening a
            # historical Session. Their correlation ids cannot admit a prompt
            # or reopen/replace a live turn; terminal fences still apply.
            if event_type != "queue_update":
                state = self._states.setdefault(session_id, _HostedSessionState())
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
                message_id=f"{turn_id}:assistant" if role == "assistant" else None,
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
            mapped_type, payload = tool_event_payload(
                raw, event_type=event_type, source_loop_id=state.source_loop_id,
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
            failed_settlement = failed_settlement_receipt(
                raw,
                allow_aborted=False,
            )
            if failed_settlement is not None:
                terminal_turn_id = turn_id or state.turn_id
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
                if aborted or not final_error:
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
            if aborted:
                self.sessions.set_status(
                    session_id,
                    "idle",
                    last_message_preview="已停止。",
                )
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
            public_message_count = sum(
                isinstance(message, Mapping)
                and pi_message_is_public(message)
                for message in messages
            )
            self.sessions.set_status(
                session_id,
                "idle",
                message_count=public_message_count,
                last_message_preview=last_assistant_preview(messages),
            )
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

    def _settle_fallback_probe(self, session_id: str, turn_id: str) -> None:
        """Retire a turn only when the Host confirms it has no active work."""

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
                    "terminalEvent": "abort_failure_race",
                },
                turn_id=turn_id,
            )
            return
        self.sessions.set_status(session_id, "faulted", last_message_preview=message)
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

    def _handle_host_exit(self, exit_code: int | None, error: str) -> None:
        with self._lock:
            if self._intentional_stop:
                return
            message = redact_runtime_text(error or f"Pi Runtime Host exited with code {exit_code}")
            active = [(session_id, state.turn_id) for session_id, state in self._states.items() if state.turn_id]
            for state in self._states.values():
                if state.abort_timer is not None:
                    state.abort_timer.cancel()
                if state.settle_timer is not None:
                    state.settle_timer.cancel()
            self._client = None
            self._open_sessions.clear()
            self._states.clear()
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
            or any(
                state.turn_id or state.prompt_admission_in_flight
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






def _latest_entry_id(entries: list[dict[str, object]]) -> str:
    return next(
        (
            str(entry.get("id") or "")
            for entry in reversed(entries)
            if str(entry.get("id") or "")
        ),
        "",
    )
