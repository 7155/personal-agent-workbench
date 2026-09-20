from __future__ import annotations

import json
import math
import threading
import uuid
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .active_rag_candidate_compiler import (
    ActiveRagCandidate,
    compile_active_rag_candidates,
    compile_active_rag_candidates_from_evidence,
)
from .active_rag_models import ActiveRagEvidence, ActiveRagFrame, ACTIVE_RAG_MODE, active_rag_key_policy
from .active_rag_retriever import retrieve_active_rag_evidence
from .contracts.context_observability import (
    ACTIVE_RAG_ROUTE_STATUS_SCHEMA_VERSION,
    build_context_injection_trace,
    evidence_injection_diagnostics,
    text_fingerprint,
)
from .deepseek_completion import (
    CompletionCandidateDelta,
    DeepSeekCompletionError,
    DeepSeekCompletionRequest,
    active_rag_text_is_complete,
    build_deepseek_completion_messages,
    resolved_active_rag_current_request,
)
from .local_sqlite_core import LocalSqliteCoreClient
from .prediction_status import thinking_animation_frame, thinking_animation_suffix
from .runtime_flags import assert_deepseek_scene_allowed
from .smart_rag_context_packet import build_active_rag_context_packet
from .input_task import selection_source, selection_task_policy
from .text_utils import compact_whitespace, now_ms, stable_text_hash, token_terms
from .timeline_context import timeline_context_preferences, timeline_evidence_pack_from_core
from .window_context import project_window_context_for_generation


ACTIVE_RAG_SERVICE_SCHEMA_VERSION = "rag-ime.active-rag-service.v1"
ACTIVE_RAG_VISIBLE_READY_TIMEOUT_MS = 120_000
ACTIVE_RAG_DEFAULT_MAX_CHARS = 0
ACTIVE_RAG_LOCAL_EVIDENCE_MAX_CHARS = 120
ACTIVE_RAG_CHAIN_TRACE_SCHEMA_VERSION = "rag-ime.active-rag-chain-trace.v1"
ACTIVE_RAG_CHAIN_TRACE_MAX_BYTES = 8_000_000
ACTIVE_RAG_CHAIN_TRACE_TEXT_LIMIT = 24_000
ACTIVE_RAG_PROGRESS_MAX_ITEMS = 3
ACTIVE_RAG_PROGRESS_PREVIEW_CHARS = 96
SENSITIVE_FIELD_BLOCK_REASON = "sensitive_field_blocked"

# DeepSeekCompletionError exposes producer diagnostics as structured
# ``terminalReason``/``transportReason`` fields.  Only the combinations below
# mean that the provider completed normally but produced no insertable result;
# an absent or unfamiliar code must remain a real Trace failure.
_ACTIVE_RAG_GOVERNED_TERMINAL_REASONS = frozenset(
    {"governor_rejected_content", "empty_remote_content"}
)
_ACTIVE_RAG_GOVERNED_TRANSPORT_REASONS = frozenset(
    {
        "empty_remote_content",
        "stream_completed",
        "finish_stop",
        "finish_length",
        "finish_tool_calls",
        "finish_function_call",
        "finish_content_filter",
        "continuation_empty_remote_content",
        "continuation_stream_completed",
        "continuation_finish_stop",
        "continuation_finish_length",
        "continuation_finish_tool_calls",
        "continuation_finish_function_call",
        "continuation_finish_content_filter",
    }
)

_TRACE_SECRET_FIELD_TOKENS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "accesstoken",
    "secret",
    "password",
    "cookie",
)
_TRACE_TEXT_FIELD_TOKENS = (
    "candidate",
    "content",
    "label",
    "message",
    "prompt",
    "query",
    "response",
    "summary",
    "tag",
    "text",
    "title",
    "value",
)


def _local_evidence_max_chars(requested_max_chars: int) -> int:
    requested = int(requested_max_chars or 0)
    return requested if requested > 0 else ACTIVE_RAG_LOCAL_EVIDENCE_MAX_CHARS

_TIMELINE_GENERIC_TERMS = {
    "这里",
    "当前",
    "前台",
    "上下文",
    "测试",
    "内容",
    "应该",
    "没有",
    "功能",
    "一下",
    "一个",
    "这个",
    "那个",
    "目前",
    "现在",
    "可以",
    "需要",
    "进行",
    "问题",
    "输入",
    "输出",
    "输入法",
    "候选",
    "rag",
    "ime",
    "squirrel",
    "codex",
}
# ``token_terms`` emits CJK bigrams/trigrams. A literal stop-word check alone
# therefore lets fragments such as ``下文`` or ``台上`` manufacture relevance
# for an unrelated memory book. Drop n-grams made entirely from generic UI words.
_TIMELINE_GENERIC_CJK_CHARS = frozenset(
    "这里当前前台上下文测试内容应该没有功能一下一个这个那个目前现在可以需要进行问题输入输出法候选用于"
)


@dataclass(frozen=True)
class ActiveRagStartRequest:
    selected_text: str
    selected_text_hash: str
    frontend_revision: int
    selection_epoch: int
    panel_session_id: str = ""
    front_app_bundle_id: str = ""
    surrounding_before: str = ""
    surrounding_after: str = ""
    intent: str = "rewrite"
    placement: str = "replace_selection"
    context: str = ""
    context_source: str = ""
    frontend_context_hash: str = ""
    frontend_context_chars: int = 0
    frontend_selected_text_chars: int = 0
    evidence_pack: tuple[dict[str, object], ...] = ()
    project: str = "wisdom-weasel-rag-ime"
    app: str = ""
    max_candidates: int = 1
    max_chars: int = ACTIVE_RAG_DEFAULT_MAX_CHARS
    latency_budget_ms: int = ACTIVE_RAG_VISIBLE_READY_TIMEOUT_MS
    remote_model_allowed: bool | None = None
    remote_model_skip_reason: str = ""
    remote_model_gates: dict[str, bool] = field(default_factory=dict)
    sensitive_field: bool = False
    secure_input: bool = False
    sensitive_text_guard_enabled: bool = True
    sensitive_text_guard_hit: bool = False
    local_retrieval_allowed: bool = True
    local_retrieval_skip_reason: str = ""
    rag_enabled_lanes: tuple[tuple[str, bool], ...] = ()
    rag_lane_weights: tuple[tuple[str, float], ...] = ()
    window_context: dict[str, object] = field(default_factory=dict)
    operation: str = ""
    target_language: str = ""


@dataclass
class ActiveRagSession:
    session_id: str
    request: ActiveRagStartRequest
    status: str = "pending"
    evidence: tuple[ActiveRagEvidence, ...] = ()
    candidates: tuple[ActiveRagCandidate, ...] = ()
    error: str = ""
    diagnostics: dict[str, object] = field(default_factory=dict)
    trace_events: list[dict[str, object]] = field(default_factory=list)
    trace_partial_chars: int = 0
    created_at_ms: int = field(default_factory=now_ms)
    updated_at_ms: int = field(default_factory=now_ms)
    # Lifecycle timing is kept separately from the user-facing session
    # elapsed counter.  The latter starts at request capture and therefore
    # cannot identify when retrieval or generation actually began.  Internal
    # monotonic anchors are stripped before this map crosses the observation
    # boundary; only explicit wall ordering points and monotonic-derived
    # durations are published.
    stage_timings: dict[str, dict[str, object]] = field(default_factory=dict)
    # UI status is separate from execution/trace outcome. A visible timeout
    # can expose a retry row while the provider is still open.
    trace_outcome: str = "building"
    visible_timeout: bool = False


def _initialize_active_rag_context_timing(session: ActiveRagSession) -> None:
    """Record the context capture point without inventing its duration."""

    session.stage_timings["context"] = {
        "startedAtMs": max(0, int(session.created_at_ms)),
    }


def _begin_active_rag_stage(session: ActiveRagSession, stage: str) -> None:
    """Start one producer-owned stage with a wall ordering point and monotonic anchor."""

    if stage not in {"retrieval", "generation"}:
        return
    existing = session.stage_timings.get(stage)
    if isinstance(existing, dict) and "startedAtMs" in existing:
        return
    session.stage_timings[stage] = {
        "startedAtMs": max(0, int(now_ms())),
        "_startedMonotonic": time.perf_counter(),
    }


def _finish_active_rag_stage(session: ActiveRagSession, stage: str) -> None:
    """Finish a stage using monotonic elapsed time, never session elapsed time.

    Common Trace requires an integer wall-clock start/end pair whose difference
    equals ``durationMs``.  The start is the producer's wall ordering point;
    the endpoint is projected from that point using the monotonic measurement.
    This keeps duration immune to wall-clock adjustments while retaining a
    stable display ordering coordinate.
    """

    timing = session.stage_timings.get(stage)
    if not isinstance(timing, dict) or "startedAtMs" not in timing:
        return
    if "durationMs" in timing:
        return
    started_monotonic = timing.get("_startedMonotonic")
    if isinstance(started_monotonic, bool) or not isinstance(started_monotonic, (int, float)):
        return
    elapsed_ms = max(0.0, (time.perf_counter() - float(started_monotonic)) * 1000.0)
    duration_ms = max(0, int(round(elapsed_ms)))
    started_at_ms = max(0, int(timing["startedAtMs"]))
    timing.update(
        {
            "endedAtMs": started_at_ms + duration_ms,
            "durationMs": duration_ms,
        }
    )


def _finish_active_rag_open_stages(session: ActiveRagSession) -> None:
    for stage in ("retrieval", "generation"):
        _finish_active_rag_stage(session, stage)


def _active_rag_worker_pending(session: ActiveRagSession) -> bool:
    """Whether the worker still owns an unfinished request.

    ``visible_timeout`` is a UI fallback only. The provider worker remains
    active and must be allowed to close its generation stage (or report a
    truthful failure) after the retry row has been shown.
    """

    return session.status == "pending" or (
        session.visible_timeout and session.trace_outcome == "building"
    )


class ActiveRagService:
    def __init__(
        self,
        *,
        core: LocalSqliteCoreClient | None = None,
        completion_provider=None,
        trace_path: Path | None = None,
        trace_include_text: bool | Callable[[], bool] = False,
        trace_observer: Callable[[dict[str, object]], None] | None = None,
        observation_observer: Callable[[dict[str, object]], None] | None = None,
    ):
        self.core = core
        if self.core is not None:
            self.core.initialize()
        self.completion_provider = completion_provider
        self.trace_path = Path(trace_path).expanduser() if trace_path is not None else None
        self.trace_include_text = trace_include_text
        self.trace_observer = trace_observer
        self.observation_observer = observation_observer
        self._lock = threading.RLock()
        self._trace_lock = threading.RLock()
        self._sessions: dict[str, ActiveRagSession] = {}
        self._blocked_responses: dict[str, dict[str, object]] = {}
        self._threads: set[threading.Thread] = set()
        self._closed = False

    def bind_trace_observer(
        self,
        observer: Callable[[dict[str, object]], None] | None,
    ) -> None:
        self.trace_observer = observer

    def bind_observation_observer(
        self,
        observer: Callable[[dict[str, object]], None] | None,
    ) -> None:
        self.observation_observer = observer

    def start(self, request: ActiveRagStartRequest) -> dict[str, object]:
        sensitive_reason = active_rag_sensitive_block_reason(request)
        if sensitive_reason:
            session_id = f"active-rag:blocked:{uuid.uuid4().hex[:12]}"
            response = _sensitive_blocked_payload(session_id=session_id, reason=sensitive_reason)
            with self._lock:
                self._blocked_responses[session_id] = response
                while len(self._blocked_responses) > 16:
                    self._blocked_responses.pop(next(iter(self._blocked_responses)))
            return dict(response)
        _validate_selected_text_hash(request)
        if request.operation:
            selection_task_policy({"outputContract": {"operation": request.operation, "targetLanguage": request.target_language}})
            selection_source(request.selected_text)
        session = ActiveRagSession(
            session_id=f"active-rag:{uuid.uuid4().hex[:16]}",
            request=request,
            diagnostics=_initial_session_diagnostics(request, completion_provider=self.completion_provider),
        )
        _initialize_active_rag_context_timing(session)
        _append_trace_event(session, "active_rag_context_captured", requestCapture=session.diagnostics["requestCapture"])
        with self._lock:
            if self._closed:
                raise RuntimeError("active RAG service is closed")
            self._drop_stale_sessions_locked(request)
            self._sessions[session.session_id] = session
            self._persist_session_trace_locked(session, phase="started")
            thread = threading.Thread(target=self._run_session, args=(session.session_id,), daemon=True)
            self._threads.add(thread)
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._threads.discard(thread)
            raise
        return self.status(session.session_id)

    def start_reference(self, request, reference, validator):
        """Explicit local note reuse through the ordinary native candidate/accept path."""
        if active_rag_sensitive_block_reason(request):
            return self.start(request)
        _validate_selected_text_hash(request)
        from dataclasses import replace
        request = replace(request, placement="insert", remote_model_allowed=False)
        candidate = ActiveRagCandidate(candidate_id="vault:"+reference['id'],
            text=reference['markdown'], insert_text=reference['markdown'],
            source_type="knowledge", source_lane="local_notes",
            metadata={"referenceOnly":True})
        session = ActiveRagSession(session_id="active-rag:"+uuid.uuid4().hex[:16],
            request=request,status="ready",candidates=(candidate,),
            diagnostics=_initial_session_diagnostics(request,completion_provider=self.completion_provider))
        session.reference_validator = validator
        with self._lock:
            if self._closed:
                raise RuntimeError("active RAG service is closed")
            self._drop_stale_sessions_locked(request)
            self._sessions[session.session_id]=session
        return self.status(session.session_id)

    def close(self) -> None:
        """Stop accepting work and wait briefly for owned session threads."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            threads = tuple(self._threads)
        current = threading.current_thread()
        for thread in threads:
            if thread is current:
                continue
            thread.join(timeout=5.0)

    def preview(self, request: ActiveRagStartRequest, *, local_only: bool = True) -> dict[str, object]:
        sensitive_reason = active_rag_sensitive_block_reason(request)
        if sensitive_reason:
            return {
                "dryRun": True,
                **_sensitive_blocked_payload(
                    session_id="active-rag:blocked-preview",
                    reason=sensitive_reason,
                ),
            }
        _validate_selected_text_hash(request)
        diagnostics = _initial_session_diagnostics(request, completion_provider=self.completion_provider)
        trace_events: list[dict[str, object]] = []
        evidence = self._retrieve_local_evidence(request, diagnostics=diagnostics)
        trace_events.append(_trace_event("active_rag_retrieval_completed", retrieval=diagnostics.get("retrieval", {})))
        local_candidates = compile_active_rag_candidates_from_evidence(
            evidence,
            frame=_frame_from_request(request),
            max_candidates=request.max_candidates,
            max_chars=_local_evidence_max_chars(request.max_chars),
        )
        if not local_only:
            route = _remote_active_rag_route(self.completion_provider, request=request)
            diagnostics["route"] = route
            deepseek_candidates = (
                self._deepseek_candidates(request, evidence=evidence, diagnostics=diagnostics, trace_events=trace_events)
                if route["ready"]
                else ()
            )
            candidates = (
                deepseek_candidates
                if route["ready"]
                else _merge_candidates(
                    local_candidates,
                    deepseek_candidates,
                    max_candidates=request.max_candidates,
                    prefer_deepseek=True,
                )
            )
        else:
            diagnostics["route"] = {
                **_remote_active_rag_route(self.completion_provider, request=request),
                "ready": False,
                "attempted": False,
                "skipReason": "local_only_request",
            }
            diagnostics["remoteModel"] = {
                **dict(diagnostics.get("remoteModel") or {}),
                "requested": False,
                "allowed": False,
                "skipReason": "local_only_request",
            }
            candidates = local_candidates
        diagnostics["generation"] = {
            **dict(diagnostics.get("generation") or {}),
            "localCandidateCount": len(local_candidates),
            "displayedCandidateCount": len(candidates),
        }
        # Management previews stay text-redacted. The native inspector receives
        # this ephemeral payload only from a live foreground session.
        diagnostics.pop("contextView", None)
        trace_events.append(
            _trace_event(
                "active_rag_candidates_displayed",
                candidateCount=len(candidates),
                sourceCounts=_candidate_source_counts(candidates),
            )
        )
        session = ActiveRagSession(
            session_id="active-rag:preview",
            request=request,
            status="ready",
            evidence=evidence,
            candidates=candidates,
            diagnostics=diagnostics,
            trace_events=trace_events,
        )
        return {"ok": True, "dryRun": True, **_session_payload(session)}

    def status(self, session_id: str) -> dict[str, object]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                blocked = self._blocked_responses.get(session_id)
                if blocked is not None:
                    return dict(blocked)
                return {"schemaVersion": ACTIVE_RAG_SERVICE_SCHEMA_VERSION, "sessionId": session_id, "status": "missing"}
            if session.status == "pending" and _should_force_visible_fallback(session):
                self._force_visible_fallback_locked(session, reason="visible_timeout")
                self._persist_session_trace_locked(session, phase="visible_timeout")
            return _session_payload(session)

    def diagnostics(self, session_id: str) -> dict[str, object]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                blocked = self._blocked_responses.get(session_id)
                if blocked is not None:
                    return {
                        "schemaVersion": "rag-ime.active-rag-diagnostics.v1",
                        "ok": False,
                        "sessionId": session_id,
                        "status": "blocked",
                        "diagnostics": dict(blocked.get("diagnostics") or {}),
                        "traceEvents": list(blocked.get("traceEvents") or []),
                        "error": SENSITIVE_FIELD_BLOCK_REASON,
                    }
                return {
                    "schemaVersion": "rag-ime.active-rag-diagnostics.v1",
                    "ok": False,
                    "sessionId": session_id,
                    "status": "missing",
                    "error": "Active RAG session not found",
                }
            return {
                "schemaVersion": "rag-ime.active-rag-diagnostics.v1",
                "ok": True,
                "sessionId": session.session_id,
                "status": session.status,
                "diagnostics": dict(session.diagnostics),
                "traceEvents": list(session.trace_events),
                "error": session.error,
            }

    def trace_records(self, *, limit: int = 50, session_id: str = "") -> dict[str, object]:
        path = self.trace_path
        if path is None:
            return {
                "schemaVersion": ACTIVE_RAG_CHAIN_TRACE_SCHEMA_VERSION,
                "enabled": False,
                "rawTextIncluded": self._trace_include_text_enabled(),
                "records": [],
            }
        bounded_limit = max(1, min(500, int(limit or 50)))
        records: list[dict[str, object]] = []
        with self._trace_lock:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
            except OSError as exc:
                return {
                    "schemaVersion": ACTIVE_RAG_CHAIN_TRACE_SCHEMA_VERSION,
                    "enabled": True,
                    "rawTextIncluded": self._trace_include_text_enabled(),
                    "records": [],
                    "error": _safe_failure_reason(exc),
                }
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if session_id and str(record.get("sessionId") or "") != session_id:
                continue
            records.append(record)
            if len(records) >= bounded_limit:
                break
        records.reverse()
        return {
            "schemaVersion": ACTIVE_RAG_CHAIN_TRACE_SCHEMA_VERSION,
            "enabled": True,
            "rawTextIncluded": self._trace_include_text_enabled(),
            "records": records,
        }

    def cancel(self, session_id: str) -> dict[str, object]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and _active_rag_worker_pending(session):
                _finish_active_rag_open_stages(session)
                session.status = "cancelled"
                session.trace_outcome = "cancelled"
                session.updated_at_ms = now_ms()
                self._record_cancel_feedback(session=session)
                self._persist_session_trace_locked(session, phase="cancelled")
                cancel = getattr(self.completion_provider, "cancel", None)
                if callable(cancel):
                    model_request = session.diagnostics.get("modelRequest")
                    provider_request_id = (
                        compact_whitespace(str(model_request.get("surfaceRequestId") or ""))
                        if isinstance(model_request, dict)
                        else ""
                    )
                    cancel(provider_request_id or session.request.panel_session_id)
            return _session_payload(session) if session is not None else {"sessionId": session_id, "status": "missing"}

    def accept(
        self,
        *,
        session_id: str,
        candidate_id: str,
        selected_text_hash: str,
        frontend_revision: int,
        selection_epoch: int,
        panel_session_id: str = "",
        front_app_bundle_id: str = "",
    ) -> dict[str, object]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return {"ok": False, "reason": "missing_session"}
            if selected_text_hash != session.request.selected_text_hash:
                return {"ok": False, "reason": "selected_text_hash_mismatch"}
            if int(frontend_revision) != int(session.request.frontend_revision):
                return {"ok": False, "reason": "frontend_revision_mismatch"}
            if int(selection_epoch) != int(session.request.selection_epoch):
                return {"ok": False, "reason": "selection_epoch_mismatch"}
            if session.request.panel_session_id and panel_session_id and panel_session_id != session.request.panel_session_id:
                return {"ok": False, "reason": "panel_session_id_mismatch"}
            if session.request.front_app_bundle_id and front_app_bundle_id and front_app_bundle_id != session.request.front_app_bundle_id:
                return {"ok": False, "reason": "front_app_bundle_id_mismatch"}
            candidate = next((item for item in session.candidates if item.candidate_id == candidate_id), None)
            if candidate is None:
                return {"ok": False, "reason": "missing_candidate"}
            validator = getattr(session, 'reference_validator', None)
            if validator is not None:
                if session.status != 'ready' or panel_session_id != session.request.panel_session_id or front_app_bundle_id != session.request.front_app_bundle_id:
                    return {"ok":False,"reason":"note_target_changed"}
                try:
                    if not validator():
                        return {"ok":False,"reason":"note_reference_expired"}
                except Exception:
                    return {"ok":False,"reason":"note_reference_unavailable"}
            self._record_accept_feedback(session=session, candidate=candidate)
            return {
                "ok": True,
                "sessionId": session_id,
                "candidateId": candidate_id,
                "insertText": candidate.insert_text,
                "placement": session.request.placement,
            }

    def _trace_include_text_enabled(self) -> bool:
        configured = self.trace_include_text
        if callable(configured):
            try:
                return bool(configured())
            except Exception:
                return False
        return bool(configured)

    def _persist_session_trace_locked(self, session: ActiveRagSession, *, phase: str) -> None:
        """Persist one bounded, replayable snapshot of the explicit RAG chain.

        This journal is deliberately independent from the in-memory diagnostics
        endpoint. A Sidecar restart must not erase the only explanation for a
        paid generation failure. Sensitive-field requests never reach this
        method; normal requests keep text fingerprint-only unless the explicit
        trace-text switch is enabled.
        """

        if active_rag_sensitive_block_reason(session.request):
            return
        path = self.trace_path
        observer = self.trace_observer
        if path is not None or observer is not None:
            include_text = self._trace_include_text_enabled()
            record = _active_rag_chain_trace_record(
                session,
                phase=phase,
                include_text=include_text,
            )
        else:
            include_text = False
            record = None
        if path is not None and record is not None:
            try:
                with self._trace_lock:
                    _append_active_rag_chain_trace(path, record)
                session.diagnostics["tracePersistence"] = {
                    "enabled": True,
                    "lastPhase": phase,
                    "lastWrittenAtMs": int(record["timestampMs"]),
                    "rawTextIncluded": include_text,
                    "error": "",
                }
            except OSError as exc:
                session.diagnostics["tracePersistence"] = {
                    "enabled": True,
                    "lastPhase": phase,
                    "rawTextIncluded": include_text,
                    "error": _safe_failure_reason(exc),
                }
        if observer is not None and record is not None:
            try:
                observer(record)
            except Exception:
                # Trace projection is passive and cannot fail an Active RAG run.
                pass
        observation_observer = self.observation_observer
        if observation_observer is not None:
            try:
                observation_observer(
                    _active_rag_observation_record(session, phase=phase)
                )
            except Exception:
                # Runtime observation is best-effort and must not add latency
                # or failure coupling to the explicit Active RAG request.
                pass

    def _run_session(self, session_id: str) -> None:
        try:
            self._run_session_inner(session_id)
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    def _run_session_inner(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return
        try:
            with self._lock:
                current = self._sessions.get(session_id)
                if current is None or not _active_rag_worker_pending(current):
                    return
                _begin_active_rag_stage(current, "retrieval")
            try:
                evidence = self._retrieve_local_evidence(session.request, diagnostics=session.diagnostics)
            except Exception as exc:
                # The normal retriever records an internal failure itself, but
                # adapters/tests may fail at this boundary before that helper
                # can write diagnostics.  Preserve the stage attribution so a
                # failed retrieval cannot be projected as a completed or
                # fabricated generation span.
                with self._lock:
                    current = self._sessions.get(session_id)
                    if current is not None:
                        current.diagnostics["retrieval"] = {
                            **dict(current.diagnostics.get("retrieval") or {}),
                            "error": f"{type(exc).__name__}: {_safe_failure_reason(exc)}",
                        }
                raise
            finally:
                with self._lock:
                    current = self._sessions.get(session_id)
                    if current is not None:
                        _finish_active_rag_stage(current, "retrieval")
            _append_trace_event(
                session,
                "active_rag_retrieval_completed",
                retrieval=session.diagnostics.get("retrieval", {}),
            )
            with self._lock:
                current = self._sessions.get(session_id)
                if current is None or not _active_rag_worker_pending(current):
                    return
                current.evidence = evidence
                current.updated_at_ms = now_ms()
                self._persist_session_trace_locked(current, phase="retrieval_complete")
            route = _remote_active_rag_route(self.completion_provider, request=session.request)
            session.diagnostics["route"] = route
            remote_model_enabled = bool(route["ready"])
            _seed_active_rag_context_diagnostics(
                session.diagnostics,
                request=session.request,
                evidence=evidence,
                remote_model_ready=remote_model_enabled,
            )
            with self._lock:
                current = self._sessions.get(session_id)
                if current is None or not _active_rag_worker_pending(current):
                    return
                _begin_active_rag_stage(current, "generation")
            try:
                if remote_model_enabled:
                    candidates = self._deepseek_candidates(
                        session.request,
                        evidence=evidence,
                        diagnostics=session.diagnostics,
                        trace_events=session.trace_events,
                        stream_sink=lambda text: self._publish_deepseek_partial(session_id, text),
                    )
                else:
                    candidates = self._local_candidates(session.request, evidence=evidence)
            finally:
                with self._lock:
                    current = self._sessions.get(session_id)
                    if current is not None:
                        _finish_active_rag_stage(current, "generation")
            with self._lock:
                current = self._sessions.get(session_id)
                if current is None or not _active_rag_worker_pending(current):
                    return
                current.evidence = evidence
                if candidates:
                    current.candidates = candidates
                    current.status = "ready"
                    current.error = ""
                else:
                    empty_reason = (
                        "remote_generation_returned_no_insertable_content"
                        if remote_model_enabled
                        else "local_rag_returned_no_insertable_content"
                    )
                    if not self._recover_streamed_partial_locked(current, reason=empty_reason):
                        self._mark_no_suitable_suggestion_locked(current, reason=empty_reason)
                current.diagnostics["generation"] = {
                    **dict(current.diagnostics.get("generation") or {}),
                    "displayedCandidateCount": len(current.candidates),
                    "displayedSourceCounts": _candidate_source_counts(current.candidates),
                }
                _append_trace_event(
                    current,
                    "active_rag_candidates_displayed",
                    candidateCount=len(current.candidates),
                    sourceCounts=_candidate_source_counts(current.candidates),
                )
                if current.status == "ready":
                    self._record_shown_feedback(session=current, candidates=current.candidates)
                if current.trace_outcome == "building":
                    current.trace_outcome = "completed"
                current.updated_at_ms = now_ms()
                self._persist_session_trace_locked(current, phase=current.status)
        except Exception as exc:
            with self._lock:
                current = self._sessions.get(session_id)
                if current is not None and _active_rag_worker_pending(current):
                    current.diagnostics["failure"] = {
                        "stage": "active_rag_session",
                        "errorType": type(exc).__name__,
                        "reason": _safe_failure_reason(exc),
                    }
                    provider_failure = not _no_suitable_generation_error(
                        exc,
                        diagnostics=current.diagnostics,
                    )
                    if provider_failure:
                        current.trace_outcome = "failed"
                    if not self._recover_streamed_partial_locked(current, reason=type(exc).__name__):
                        if not provider_failure:
                            self._mark_no_suitable_suggestion_locked(
                                current,
                                reason=_no_suitable_generation_reason(exc),
                            )
                        else:
                            self._force_visible_fallback_locked(current, reason=type(exc).__name__)
                            if current.status == "error":
                                current.error = _safe_failure_reason(exc)
                    current.updated_at_ms = now_ms()
                    self._persist_session_trace_locked(
                        current,
                        phase="failed" if current.trace_outcome == "failed" else current.status,
                    )

    def _retrieve_local_evidence(
        self,
        request: ActiveRagStartRequest,
        *,
        diagnostics: dict[str, object] | None = None,
    ) -> tuple[ActiveRagEvidence, ...]:
        started = time.perf_counter()
        evidence: tuple[ActiveRagEvidence, ...] = ()
        primary_raw_count = 0
        primary_count = 0
        timeline_count = 0
        retrieval_error = ""
        retrieval_query = _resolved_active_rag_request_text(request)
        retrieval_query_source = (
            "selected_text"
            if (request.operation or request.placement == "replace_selection") and compact_whitespace(request.selected_text)
            else (
                "foreground_context"
                if compact_whitespace(request.context or request.surrounding_before)
                else "selected_text_fallback"
            )
        )
        retrieval_allowed = self.core is not None and request.local_retrieval_allowed and not request.operation
        if retrieval_allowed:
            try:
                evidence = retrieve_active_rag_evidence(
                    self.core,
                    _frame_from_request(request),
                    enabled_lanes=request.rag_enabled_lanes,
                    lane_weights=request.rag_lane_weights,
                )
                primary_raw_count = len(evidence)
                evidence = _filter_primary_active_rag_evidence(
                    evidence,
                    query=retrieval_query,
                )
                primary_count = len(evidence)
            except Exception as exc:
                retrieval_error = f"{type(exc).__name__}: {_safe_failure_reason(exc)}"
                evidence = ()
            try:
                timeline_evidence = _timeline_evidence_from_core(self.core, request)
                timeline_count = len(timeline_evidence)
                evidence = _merge_evidence(evidence, timeline_evidence)
            except Exception as exc:
                if not retrieval_error:
                    retrieval_error = f"{type(exc).__name__}: {_safe_failure_reason(exc)}"
        fallback_used = not request.operation and not _grounding_evidence(evidence) and bool(request.evidence_pack)
        result = (
            _merge_evidence(evidence, _evidence_from_pack(request.evidence_pack))
            if fallback_used
            else evidence
        )
        if diagnostics is not None:
            evidence_payloads = tuple(_evidence_payload(item) for item in result)
            evidence_diagnostics = evidence_injection_diagnostics(evidence_payloads)
            diagnostics["retrieval"] = {
                "called": retrieval_allowed,
                "attempted": retrieval_allowed,
                "coreAvailable": self.core is not None,
                "skipReason": "selection_task" if request.operation else request.local_retrieval_skip_reason if not retrieval_allowed else "",
                "primaryRawRetrievedCount": primary_raw_count,
                "primaryRetrievedCount": primary_count,
                "timelineRetrievedCount": timeline_count,
                "requestEvidenceCount": len(request.evidence_pack),
                "querySource": retrieval_query_source,
                "queryChars": len(retrieval_query),
                "queryHash": stable_text_hash(retrieval_query) if retrieval_query else "",
                "anchorChars": len(compact_whitespace(request.selected_text)),
                "fallbackToRequestEvidence": fallback_used,
                "retrievedCount": len(result),
                "evidenceCount": len(_grounding_evidence(result)),
                "contextEvidenceCount": sum(1 for item in result if _is_context_only_evidence(item)),
                "surfaceCandidateCount": sum(
                    1 for item in result if item.source_type in {"phrase", "surface_phrase"}
                ),
                "error": retrieval_error,
                **evidence_diagnostics,
                "lanes": evidence_diagnostics["sourceLaneCounts"],
                "configuredLanes": dict(request.rag_enabled_lanes),
                "configuredWeights": dict(request.rag_lane_weights),
                "embeddingProvider": getattr(getattr(self.core, "embedding_provider", None), "fingerprint", "none")
                if self.core is not None
                else "none",
                "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
            }
        return result

    def _deepseek_candidates(
        self,
        request: ActiveRagStartRequest,
        *,
        evidence: tuple[ActiveRagEvidence, ...],
        diagnostics: dict[str, object] | None = None,
        trace_events: list[dict[str, object]] | None = None,
        stream_sink=None,
    ) -> tuple[ActiveRagCandidate, ...]:
        provider = self.completion_provider
        if provider is None:
            return ()
        surface_request_id = request.panel_session_id or f"active-rag-surface:{uuid.uuid4().hex[:16]}"
        try:
            if not bool(getattr(provider, "uses_managed_pi", False)):
                assert_deepseek_scene_allowed("active_rag")
        except RuntimeError as exc:
            if diagnostics is not None:
                diagnostics["route"] = {
                    **_remote_active_rag_route(provider, request=request),
                    "ready": False,
                    "skipReason": "scene_flag_disabled",
                    "reason": str(exc),
                }
            return ()
        model_evidence = _deepseek_evidence_pack(request=request, evidence=evidence)
        effective_context, effective_source, effective_context_meta = _effective_active_rag_context(
            request,
            evidence=evidence,
        )
        grounding_evidence = _grounding_evidence(evidence)
        recent_input_history = _recent_input_history_evidence(evidence)
        context_preferences = timeline_context_preferences(self.core) if self.core is not None else {}
        context_packet = build_active_rag_context_packet(
            scene="active_rag",
            current_context=effective_context,
            selected_text=request.selected_text,
            selected_text_hash=request.selected_text_hash,
            frontend_revision=request.frontend_revision,
            selection_epoch=request.selection_epoch,
            panel_session_id=surface_request_id,
            project=request.project,
            app=request.app or request.front_app_bundle_id,
            # The packet owns the separation between context-only lanes
            # (planning, approved activity, recent input) and factual hybrid
            # retrieval. Passing the typed set preserves that boundary.
            evidence=evidence,
            recent_input_history=recent_input_history,
            intent=request.intent,
            placement=request.placement,
            max_candidates=request.max_candidates,
            max_chars=request.max_chars,
            latency_budget_ms=request.latency_budget_ms,
            remote_model_allowed=True,
            context_token_budget=int(context_preferences.get("tokenBudget") or 4096),
            reserved_output_tokens=int(context_preferences.get("reservedOutputTokens") or 1024),
            recent_input_baseline=int(context_preferences.get("recentInputBaseline") or 4),
            recent_input_maximum=int(context_preferences.get("recentInputMaximum") or 4),
            recent_input_char_maximum=int(context_preferences.get("recentInputCharMaximum") or 12000),
            ax_node_maximum=int(context_preferences.get("axNodeMaximum") or 160),
            ax_char_maximum=int(context_preferences.get("axCharMaximum") or 12000),
            window_context=request.window_context,
        )
        if request.operation:
            context_packet["outputContract"].update({
                "operation": request.operation, "targetLanguage": request.target_language,
            })
            selection_task_policy(context_packet)
        packet_current_input = context_packet.get("currentInput") if isinstance(context_packet.get("currentInput"), dict) else {}
        injected_context = compact_whitespace(str(packet_current_input.get("committedTail") or effective_context))
        effective_context_meta["effectiveContextChars"] = len(injected_context)
        effective_context_meta["contextTruncatedToBudget"] = injected_context != effective_context
        completion_request = DeepSeekCompletionRequest(
            scene="active_rag",
            current_context=injected_context,
            selected_text=request.selected_text,
            evidence_pack=model_evidence,
            context_packet=context_packet,
            max_candidates=request.max_candidates,
            max_chars=request.max_chars,
            latency_budget_ms=_remote_completion_budget_ms(request),
            surface_request_id=surface_request_id,
            front_app_bundle_id=request.front_app_bundle_id,
        )
        resolved_model_request = resolved_active_rag_current_request(completion_request)
        messages = build_deepseek_completion_messages(completion_request)
        include_trace_text = self._trace_include_text_enabled()
        if diagnostics is not None:
            # This is the redacted, budgeted user payload that is actually sent
            # to the provider. Keep it in memory for the foreground context
            # viewer; the persisted chain trace intentionally omits this field.
            diagnostics["contextView"] = _active_rag_context_view_from_messages(messages)
            context_trace = build_context_injection_trace(
                current_context=completion_request.current_context,
                selected_text=completion_request.selected_text,
                surrounding_before=request.surrounding_before,
                surrounding_after=request.surrounding_after,
                evidence_pack=model_evidence,
                context_packet=context_packet,
                messages=messages,
                include_text=include_trace_text,
            )
            context_fingerprint = text_fingerprint(completion_request.current_context)
            selected_fingerprint = text_fingerprint(completion_request.selected_text)
            injection = context_trace.get("injection") if isinstance(context_trace.get("injection"), dict) else {}
            missing = list(injection.get("missing") or []) if isinstance(injection, dict) else []
            warnings = [
                *missing,
                *_capture_validation_warnings(request, context_text=request.context or request.surrounding_before),
            ]
            if not request.context and request.surrounding_before:
                warnings.append("used_surrounding_before_fallback")
            if effective_context_meta["augmentedWithTimelineRecentInput"]:
                warnings.append("augmented_with_timeline_recent_input")
            if effective_context_meta["timelineRecentInputUsedForGeneration"]:
                warnings.append("recent_input_history_injected_as_secondary_context")
            diagnostics["contextInjection"] = {
                "applied": bool(injection.get("success")) if isinstance(injection, dict) else False,
                "source": effective_source,
                "contextChars": context_fingerprint["chars"],
                "foregroundContextChars": effective_context_meta["foregroundContextChars"],
                "effectiveContextChars": effective_context_meta["effectiveContextChars"],
                "timelineRecentInputChars": effective_context_meta["timelineRecentInputChars"],
                "timelineRecentInputRecordCount": effective_context_meta["timelineRecentInputRecordCount"],
                "augmentedWithTimelineRecentInput": effective_context_meta["augmentedWithTimelineRecentInput"],
                "timelineRecentInputUsedForGeneration": effective_context_meta[
                    "timelineRecentInputUsedForGeneration"
                ],
                "contextPolicy": effective_context_meta["contextPolicy"],
                "contextTruncatedToBudget": effective_context_meta["contextTruncatedToBudget"],
                "contextBudget": dict(context_packet.get("trace") or {}),
                "contextHash": stable_text_hash(completion_request.current_context),
                "selectedTextChars": selected_fingerprint["chars"],
                "selectedTextHash": request.selected_text_hash,
                "resolvedRequestChars": len(resolved_model_request),
                "resolvedRequestHash": stable_text_hash(resolved_model_request),
                "shortForegroundCapture": effective_context_meta["foregroundContextChars"] < 8,
                "fullForegroundDocumentCaptured": False,
                "warnings": warnings,
                "trace": context_trace,
            }
            diagnostics["modelRequest"] = {
                "provider": _provider_name(provider),
                "model": _provider_model(provider),
                "surfaceRequestId": surface_request_id,
                "scene": "active_rag",
                "attempted": True,
                "completed": False,
                "startedAtMs": now_ms(),
                "latencyBudgetMs": completion_request.latency_budget_ms,
                "requestedCandidateCount": completion_request.max_candidates,
                "stream": True,
                "evidenceRetrievedCount": len(grounding_evidence),
                "evidenceInjectedCount": len(model_evidence),
                "recentInputHistoryCount": len(recent_input_history),
                "evidenceGovernedOutCount": max(0, len(request.evidence_pack) + len(evidence[:10]) - len(model_evidence)),
            }
            diagnostics["modelInput"] = _active_rag_model_input_trace(
                resolved_request=resolved_model_request,
                context_packet=context_packet,
                evidence_pack=model_evidence,
                messages=messages,
                include_text=include_trace_text,
            )
            diagnostics["remoteModel"] = {
                "requested": True,
                "allowed": True,
                "provider": _provider_name(provider),
                "model": _provider_model(provider),
                "skipReason": "",
                "elapsedMs": 0.0,
            }
        if trace_events is not None:
            trace_events.append(
                _trace_event(
                    "deepseek_request_context_built",
                    injection=(diagnostics or {}).get("contextInjection", {}),
                )
            )
            trace_events.append(
                _trace_event("deepseek_request_started", provider=_provider_name(provider), model=_provider_model(provider))
            )
        model_started = time.perf_counter()
        retry_attempted = False
        retry_reason = ""
        initial_attempt_transport: dict[str, object] = {}

        def run_completion(active_request: DeepSeekCompletionRequest) -> tuple[CompletionCandidateDelta, ...]:
            if stream_sink is not None and bool(getattr(provider, "supports_text_delta_callback", False)):
                return tuple(provider.stream_candidates(active_request, on_text_delta=stream_sink))
            return tuple(provider.stream_candidates(active_request))

        recovery_selected_text = (
            request.selected_text
            if request.operation or (request.placement == "replace_selection" and len(compact_whitespace(request.selected_text)) >= 8)
            else ""
        )
        recovery_context_packet = dict(context_packet)
        # The quality retry is intentionally context-only: preserve bounded
        # continuity lanes, but do not let rejected factual grounding survive
        # through the context packet after ``evidence_pack`` is cleared.
        recovery_context_packet.pop("groundingEvidence", None)
        recovery_context_packet.pop("ragEvidenceHints", None)
        recovery_current_input = (
            dict(recovery_context_packet.get("currentInput"))
            if isinstance(recovery_context_packet.get("currentInput"), dict)
            else {}
        )
        if not recovery_selected_text:
            recovery_current_input.pop("selectedText", None)
        recovery_context_packet["currentInput"] = recovery_current_input
        recovery_request = replace(
            completion_request,
            selected_text=recovery_selected_text,
            evidence_pack=(),
            context_packet=recovery_context_packet,
            recovery_mode=True,
        )
        if diagnostics is not None:
            diagnostics["modelInput"] = {
                **dict(diagnostics.get("modelInput") or {}),
                "recovery": _active_rag_model_input_trace(
                    resolved_request=resolved_active_rag_current_request(recovery_request),
                    context_packet=recovery_context_packet,
                    evidence_pack=(),
                    messages=build_deepseek_completion_messages(recovery_request),
                    include_text=include_trace_text,
                ),
            }

        try:
            try:
                raw_deltas = run_completion(completion_request)
            except DeepSeekCompletionError as exc:
                initial_attempt_transport = _completion_error_trace(exc, include_text=include_trace_text)
                if not _retryable_empty_generation_error(exc):
                    raise
                retry_attempted = True
                retry_reason = _safe_failure_reason(exc)
                _mark_active_rag_quality_retry(
                    diagnostics,
                    reason=retry_reason,
                )
                if trace_events is not None:
                    trace_events.append(_trace_event("deepseek_context_only_retry_started", reason=retry_reason))
                raw_deltas = run_completion(recovery_request)
            candidates = compile_active_rag_candidates(
                raw_deltas,
                selected_text=request.selected_text,
                max_candidates=request.max_candidates,
                max_chars=request.max_chars,
                operation=request.operation,
            )
            if not candidates and not retry_attempted:
                retry_attempted = True
                retry_reason = "empty_or_governed_remote_candidates"
                _mark_active_rag_quality_retry(
                    diagnostics,
                    reason=retry_reason,
                )
                if trace_events is not None:
                    trace_events.append(_trace_event("deepseek_context_only_retry_started", reason=retry_reason))
                raw_deltas = run_completion(recovery_request)
                candidates = compile_active_rag_candidates(
                    raw_deltas,
                    selected_text=request.selected_text,
                    max_candidates=request.max_candidates,
                    max_chars=request.max_chars,
                    operation=request.operation,
                )
        except Exception as exc:
            if diagnostics is not None:
                transport = _completion_error_trace(exc, include_text=include_trace_text)
                diagnostics["modelRequest"] = {
                    **dict(diagnostics.get("modelRequest") or {}),
                    "completed": False,
                    "errorType": type(exc).__name__,
                    "failureReason": _safe_failure_reason(exc),
                    "contentRetryAttempted": retry_attempted,
                    "contentRetryReason": retry_reason,
                    "contentRetryCompleted": False,
                    "transport": transport,
                    "initialAttemptTransport": initial_attempt_transport,
                }
                diagnostics["remoteModel"] = {
                    **dict(diagnostics.get("remoteModel") or {}),
                    "skipReason": type(exc).__name__,
                    "failureReason": _safe_failure_reason(exc),
                    "elapsedMs": round((time.perf_counter() - model_started) * 1000, 2),
                }
            if trace_events is not None:
                trace_events.append(
                    _trace_event(
                        "deepseek_request_failed",
                        errorType=type(exc).__name__,
                        failureReason=_safe_failure_reason(exc),
                        contentRetryAttempted=retry_attempted,
                    )
                )
            raise
        completion_transport = _completion_delta_diagnostics(raw_deltas, provider=provider)
        if diagnostics is not None:
            diagnostics["modelRequest"] = {
                **dict(diagnostics.get("modelRequest") or {}),
                "completed": True,
                "rawCandidateCount": len(raw_deltas),
                "governedCandidateCount": len(candidates),
                "candidateFilteredCount": max(0, len(raw_deltas) - len(candidates)),
                "contentRetryAttempted": retry_attempted,
                "contentRetryReason": retry_reason,
                "contentRetryCompleted": bool(retry_attempted and candidates),
                "initialAttemptTransport": initial_attempt_transport,
                "fallbackReasons": sorted(
                    {
                        str(item.metadata.get("fallbackReason"))
                        for item in raw_deltas
                        if item.metadata.get("fallbackReason")
                    }
                ),
                **completion_transport,
            }
            diagnostics["remoteModel"] = {
                **dict(diagnostics.get("remoteModel") or {}),
                "skipReason": "",
                "elapsedMs": round((time.perf_counter() - model_started) * 1000, 2),
            }
        if trace_events is not None:
            trace_events.append(
                _trace_event(
                    "deepseek_request_completed",
                    rawCandidateCount=len(raw_deltas),
                    governedCandidateCount=len(candidates),
                    **completion_transport,
                )
            )
            trace_events.append(
                _trace_event(
                    "active_rag_evidence_governed",
                    retrievedCount=len(grounding_evidence),
                    injectedCount=len(model_evidence),
                    candidateFilteredCount=max(0, len(raw_deltas) - len(candidates)),
                    contentRetryAttempted=retry_attempted,
                    contentRetryCompleted=bool(retry_attempted and candidates),
                )
            )
        return candidates

    def _publish_deepseek_partial(self, session_id: str, text: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.status != "pending":
                return
            model_request = dict(session.diagnostics.get("modelRequest") or {})
            if int(model_request.get("firstTokenMs") or 0) <= 0:
                started_at_ms = int(model_request.get("startedAtMs") or session.created_at_ms)
                model_request["firstTokenMs"] = max(1, now_ms() - started_at_ms)
                model_request["firstTokenAtMs"] = now_ms()
            model_request["stream"] = True
            partial = compile_active_rag_candidates(
                (
                    CompletionCandidateDelta(
                        text=text,
                        insert_text=text,
                        done=False,
                        metadata={"streamingPartial": True},
                    ),
                ),
                selected_text=session.request.selected_text,
                max_candidates=1,
                max_chars=session.request.max_chars,
                operation=session.request.operation,
            )
            if not partial:
                session.diagnostics["modelRequest"] = model_request
                session.updated_at_ms = now_ms()
                return
            session.candidates = partial
            model_request.update(
                {
                    "stream": True,
                    "partialVisible": True,
                    "partialChars": len(partial[0].text),
                }
            )
            session.diagnostics["modelRequest"] = model_request
            session.updated_at_ms = now_ms()
            partial_chars = len(partial[0].text)
            if session.trace_partial_chars == 0 or partial_chars - session.trace_partial_chars >= 128:
                session.trace_partial_chars = partial_chars
                self._persist_session_trace_locked(session, phase="stream_partial")

    def _local_candidates(
        self,
        request: ActiveRagStartRequest,
        *,
        evidence: tuple[ActiveRagEvidence, ...],
    ) -> tuple[ActiveRagCandidate, ...]:
        """Use RAG evidence as context, not as a raw display lane, for model-like requests."""

        local_candidates = compile_active_rag_candidates_from_evidence(
            evidence,
            frame=_frame_from_request(request),
            max_candidates=request.max_candidates,
            max_chars=_local_evidence_max_chars(request.max_chars),
        )
        return local_candidates

    def _force_visible_fallback_locked(self, session: ActiveRagSession, *, reason: str) -> None:
        if reason == "visible_timeout":
            session.visible_timeout = True
        if self._recover_streamed_partial_locked(session, reason=reason):
            return
        evidence = session.evidence or _evidence_from_pack(session.request.evidence_pack)
        session.evidence = evidence
        # Keep the explicit generation surface alive and retriable. Transport,
        # timeout and provider errors remain available in diagnostics, but the
        # foreground must not collapse into the alarming generic "生成失败"
        # card after the user has already waited for a paid request.
        self._mark_no_suitable_suggestion_locked(
            session,
            reason=compact_whitespace(reason) or "generation_not_completed",
        )

    def _recover_streamed_partial_locked(self, session: ActiveRagSession, *, reason: str) -> bool:
        """Keep only a complete sentence after an interrupted visible stream.

        The candidate governor validates content but cannot prove that a stream
        reached a semantic boundary. A clause such as ``...同时避免`` must stay
        non-final and non-insertable even when it was briefly visible while the
        provider was streaming.
        """

        if session.request.operation:
            # A complete sentence is not proof of a complete translation or
            # summary. Keep the existing retry surface until a task settles.
            return False
        visible_partials = tuple(
            candidate
            for candidate in session.candidates
            if bool(candidate.metadata.get("streamingPartial")) and compact_whitespace(candidate.text)
            and _streamed_partial_is_complete(candidate.text)
        )
        if not visible_partials:
            return False
        recovered = tuple(
            replace(
                candidate,
                metadata={
                    **candidate.metadata,
                    "streamingPartial": False,
                    "streamInterrupted": True,
                    "partialRecovered": True,
                    "fallbackReason": compact_whitespace(reason) or "stream_interrupted",
                },
            )
            for candidate in visible_partials
        )
        session.candidates = recovered
        session.status = "ready"
        session.error = ""
        session.diagnostics["modelRequest"] = {
            **dict(session.diagnostics.get("modelRequest") or {}),
            "completed": True,
            "partialVisible": True,
            "partialRecovered": True,
            "streamInterrupted": True,
            "failureReason": compact_whitespace(reason) or "stream_interrupted",
        }
        session.diagnostics["generation"] = {
            **dict(session.diagnostics.get("generation") or {}),
            "displayedCandidateCount": len(recovered),
            "displayedSourceCounts": _candidate_source_counts(recovered),
            "partialRecovered": True,
        }
        _append_trace_event(
            session,
            "active_rag_partial_recovered",
            candidateCount=len(recovered),
            reason=compact_whitespace(reason) or "stream_interrupted",
        )
        session.updated_at_ms = now_ms()
        return True

    def _mark_no_suitable_suggestion_locked(self, session: ActiveRagSession, *, reason: str) -> None:
        """Finish a valid request without pretending governance was a transport failure."""

        normalized_reason = compact_whitespace(reason) or "no_insertable_content"
        session.candidates = ()
        session.status = "ready"
        session.error = ""
        session.diagnostics.pop("failure", None)
        session.diagnostics["generation"] = {
            **dict(session.diagnostics.get("generation") or {}),
            "displayedCandidateCount": 0,
            "displayedSourceCounts": {},
            "noSuitableSuggestion": True,
            "noSuitableReason": normalized_reason,
        }
        session.diagnostics["modelRequest"] = {
            **dict(session.diagnostics.get("modelRequest") or {}),
            "completed": True,
            "noSuitableSuggestion": True,
            "outcome": "no_suitable_suggestion",
        }
        session.diagnostics["remoteModel"] = {
            **dict(session.diagnostics.get("remoteModel") or {}),
            "skipReason": "",
            "outcome": "no_suitable_suggestion",
        }
        _append_trace_event(
            session,
            "active_rag_no_suitable_suggestion",
            reason=normalized_reason,
        )
        session.updated_at_ms = now_ms()

    def _record_accept_feedback(self, *, session: ActiveRagSession, candidate: ActiveRagCandidate) -> None:
        if self.core is None:
            return
        self.core.record_memory_feedback(
            {
                "event": "active_rag_accept",
                "candidateId": candidate.candidate_id,
                "candidateText": candidate.text,
                "sourceType": candidate.source_type,
                "sessionId": session.session_id,
                "project": session.request.project,
                "app": session.request.app or session.request.front_app_bundle_id,
                "frontAppBundleId": session.request.front_app_bundle_id,
                "contextHash": session.request.selected_text_hash,
                "metadata": {
                    "uiMode": ACTIVE_RAG_MODE,
                    "placement": session.request.placement,
                    "intent": session.request.intent,
                    "selectedTextHash": session.request.selected_text_hash,
                    "panelSessionId": session.request.panel_session_id,
                    "frontendRevision": session.request.frontend_revision,
                    "selectionEpoch": session.request.selection_epoch,
                },
            }
        )

    def _record_cancel_feedback(self, *, session: ActiveRagSession) -> None:
        if self.core is None:
            return
        self.core.record_memory_feedback(
            {
                "event": "active_rag_cancel",
                "candidateId": f"{session.session_id}:cancel",
                "candidateText": "active_rag_cancel",
                "sourceType": "active_rag",
                "sessionId": session.session_id,
                "project": session.request.project,
                "app": session.request.app or session.request.front_app_bundle_id,
                "frontAppBundleId": session.request.front_app_bundle_id,
                "contextHash": session.request.selected_text_hash,
                "metadata": {
                    "uiMode": ACTIVE_RAG_MODE,
                    "placement": session.request.placement,
                    "intent": session.request.intent,
                    "selectedTextHash": session.request.selected_text_hash,
                    "panelSessionId": session.request.panel_session_id,
                    "frontendRevision": session.request.frontend_revision,
                    "selectionEpoch": session.request.selection_epoch,
                },
            }
        )

    def _record_shown_feedback(self, *, session: ActiveRagSession, candidates: tuple[ActiveRagCandidate, ...]) -> None:
        if self.core is None or not candidates:
            return
        shown_ids = [candidate.candidate_id for candidate in candidates]
        for rank, candidate in enumerate(candidates, start=1):
            self.core.record_memory_feedback(
                {
                    "event": "active_rag_shown",
                    "candidateId": candidate.candidate_id,
                    "candidateText": candidate.text,
                    "sourceType": candidate.source_type,
                    "sessionId": session.session_id,
                    "project": session.request.project,
                    "app": session.request.app or session.request.front_app_bundle_id,
                    "frontAppBundleId": session.request.front_app_bundle_id,
                    "contextHash": session.request.selected_text_hash,
                    "rank": rank,
                    "shownCandidateIds": shown_ids,
                    "shownCandidateCount": len(candidates),
                    "metadata": {
                        "uiMode": ACTIVE_RAG_MODE,
                        "placement": session.request.placement,
                        "intent": session.request.intent,
                        "selectedTextHash": session.request.selected_text_hash,
                        "panelSessionId": session.request.panel_session_id,
                        "frontendRevision": session.request.frontend_revision,
                        "selectionEpoch": session.request.selection_epoch,
                    },
                }
            )

    def _drop_stale_sessions_locked(self, request: ActiveRagStartRequest) -> None:
        for session in self._sessions.values():
            if not _active_rag_worker_pending(session):
                continue
            if session.request.project != request.project or session.request.app != request.app:
                continue
            if (
                int(session.request.frontend_revision) < int(request.frontend_revision)
                or int(session.request.selection_epoch) < int(request.selection_epoch)
            ):
                _finish_active_rag_open_stages(session)
                session.status = "stale_dropped"
                session.trace_outcome = "cancelled"
                session.updated_at_ms = now_ms()
                self._persist_session_trace_locked(session, phase="stale_dropped")


def _validate_selected_text_hash(request: ActiveRagStartRequest) -> None:
    expected = stable_text_hash(compact_whitespace(request.selected_text))
    if request.selected_text_hash != expected:
        raise ValueError("selectedTextHash does not match selectedText")


def active_rag_sensitive_block_reason(request: ActiveRagStartRequest) -> str:
    if request.sensitive_field or request.secure_input or request.sensitive_text_guard_hit:
        return SENSITIVE_FIELD_BLOCK_REASON
    if _looks_like_sensitive_account_text(
        request.selected_text,
        request.context,
        request.surrounding_before,
        request.surrounding_after,
    ):
        return SENSITIVE_FIELD_BLOCK_REASON
    return ""


def active_rag_sensitive_text_blocked(*texts: str, guard_enabled: bool = True) -> bool:
    _ = guard_enabled
    # Credential-shaped text is a non-overridable backend boundary. The setting
    # may tune broader heuristics later, but it cannot permit passwords/tokens.
    return _looks_like_sensitive_account_text(*texts)


def _looks_like_sensitive_account_text(*texts: str) -> bool:
    combined = " ".join(str(text or "") for text in texts)
    if not combined:
        return False
    # Product and code discussions routinely mention words such as "API key",
    # "token" and "password". A noun alone is not a credential. Keep the
    # backend fail-closed for credential-shaped values while the native secure
    # field flags remain the authoritative boundary for password fields.
    if re.search(
        r"(?ix)(?:"
        r"\bbearer\s+[a-z0-9._~+/=-]{8,}"
        r"|\bsk-[a-z0-9_-]{8,}"
        r"|\b(?:gh[opsu]_|github_pat_)[a-z0-9_-]{8,}"
        r"|\bxox[baprs]-[a-z0-9-]{8,}"
        r"|\bAKIA[0-9A-Z]{16}\b"
        r"|\beyJ[a-z0-9_-]{6,}\.[a-z0-9_-]{6,}\.[a-z0-9_-]{6,}"
        r")",
        combined,
    ):
        return True
    if re.search(
        r"(?ix)(?:password|passwd|passcode|credential|api[ _-]?key|access[ _-]?token|"
        r"secret|one[ _-]?time[ _-]?code|账号|帐号|密码|口令|验证码)"
        r"\s*(?::|=|\bis\b|为|是)\s*[\"']?[^\s,;，；\"']{4,}",
        combined,
    ):
        return True
    return bool(
        re.search(
            r"(?ix)(?:银行卡|信用卡|card[ _-]?number)"
            r"\s*(?::|=|为|是)?\s*(?:\d[\s-]?){12,19}\b",
            combined,
        )
    )


def _sensitive_blocked_payload(*, session_id: str, reason: str) -> dict[str, object]:
    diagnostics = {
        "schemaVersion": "rag-ime.active-rag-diagnostics.v1",
        "privacy": {
            "rawTextIncluded": False,
            "hashAlgorithm": "none_for_sensitive_fields",
            "sensitiveFieldBlocked": True,
        },
        "contextInjection": {
            "applied": False,
            "source": "sensitive_field",
            "contextChars": 0,
            "contextHash": "",
            "selectedTextChars": 0,
            "selectedTextHash": "",
            "warnings": [reason],
        },
        "retrieval": {
            "called": False,
            "attempted": False,
            "retrievedCount": 0,
            "evidenceCount": 0,
            "lanes": {},
            "elapsedMs": 0.0,
            "error": reason,
        },
        "remoteModel": {
            "requested": False,
            "allowed": False,
            "provider": "",
            "model": "",
            "skipReason": reason,
            "elapsedMs": 0.0,
        },
        "generation": {"displayedCandidateCount": 0},
    }
    trace_events = [
        {
            "name": "active_rag_sensitive_field_blocked",
            "atMs": now_ms(),
            "fields": {"reason": reason, "textRecorded": False, "hashRecorded": False},
        }
    ]
    return {
        "schemaVersion": ACTIVE_RAG_SERVICE_SCHEMA_VERSION,
        "ok": False,
        "sessionId": session_id,
        "status": "blocked",
        "thinkingRow": False,
        "uiMode": ACTIVE_RAG_MODE,
        "keyPolicy": active_rag_key_policy(ready=False),
        "selectedTextHash": "",
        "frontendRevision": 0,
        "selectionEpoch": 0,
        "panelSessionId": "",
        "frontAppBundleId": "",
        "placement": "show_only",
        "intent": "blocked",
        "evidenceCount": 0,
        "candidateCount": 0,
        "candidates": [],
        "evidence": [],
        "diagnostics": diagnostics,
        "traceEvents": trace_events,
        "error": reason,
        "createdAtMs": now_ms(),
        "updatedAtMs": now_ms(),
        "elapsedMs": 0,
        "pollAfterMs": 0,
    }


def _resolved_active_rag_request_text(request: ActiveRagStartRequest) -> str:
    """Resolve the semantic request while keeping caret anchors subordinate."""

    if request.operation:
        return selection_source(request.selected_text)
    selected = compact_whitespace(request.selected_text)
    context = compact_whitespace(request.context or request.surrounding_before)
    if request.placement == "replace_selection" and selected:
        return selected[-4_000:]
    # The lightning button sends a short selectedText value to identify the
    # caret region. In insert/append mode the complete editable foreground is
    # the user's request, so an in-context anchor must never replace it.
    return (context or selected)[-4_000:]


def _timeline_item_relevant(item: dict[str, object], query: str) -> bool:
    if not query:
        return False
    surface_hints = item.get("surfaceHints") if isinstance(item.get("surfaceHints"), list) else []
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    haystack = compact_whitespace(
        " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("summary") or item.get("evidencePreview") or ""),
                *(str(value) for value in surface_hints),
                *(str(value) for value in tags),
            ]
        )
    ).lower()
    matches = {
        term
        for term in token_terms(query, max_terms=48)
        if len(term) >= 2
        and term not in _TIMELINE_GENERIC_TERMS
        and not (
            all("\u3400" <= char <= "\u9fff" for char in term)
            and all(char in _TIMELINE_GENERIC_CJK_CHARS for char in term)
        )
        and term.lower() in haystack
    }
    return bool(matches)


def _is_context_only_evidence(item: ActiveRagEvidence) -> bool:
    return (
        item.source_type in {
            "recent_input_context",
            "daily_plan",
            "todo",
            "goal",
            "activity_timeline",
        }
        or item.source_lane == "timeline_recent_input"
        or item.source_lane.startswith("planning_")
        or item.source_lane == "timeline_approved_activity"
    )


def _grounding_evidence(evidence: tuple[ActiveRagEvidence, ...]) -> tuple[ActiveRagEvidence, ...]:
    return tuple(
        item
        for item in evidence
        if not _is_context_only_evidence(item) and item.source_type not in {"phrase", "surface_phrase"}
    )


def _filter_primary_active_rag_evidence(
    evidence: tuple[ActiveRagEvidence, ...],
    *,
    query: str,
) -> tuple[ActiveRagEvidence, ...]:
    result: list[ActiveRagEvidence] = []
    for item in evidence:
        if item.source_type in {"phrase", "surface_phrase"}:
            continue
        if item.source_lane.startswith("vector_"):
            result.append(item)
            continue
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        surface_hints = metadata.get("surfaceHints") if isinstance(metadata.get("surfaceHints"), list) else []
        haystack = compact_whitespace(
            " ".join(
                [
                    item.text,
                    item.preview,
                    " ".join(item.tags),
                    str(metadata.get("title") or metadata.get("summary") or ""),
                    *(str(value) for value in surface_hints),
                ]
            )
        )
        synthetic_item = {"summary": haystack, "surfaceHints": [], "tags": []}
        if _timeline_item_relevant(synthetic_item, query):
            result.append(item)
    return tuple(result)


def _evidence_pack_item_is_context_only(item: dict[str, object]) -> bool:
    source_type = compact_whitespace(str(item.get("sourceType") or ""))
    source_lane = compact_whitespace(str(item.get("sourceLane") or ""))
    return (
        source_type in {
            "recent_input_context",
            "daily_plan",
            "todo",
            "goal",
            "activity_timeline",
        }
        or source_lane == "timeline_recent_input"
        or source_lane.startswith("planning_")
        or source_lane == "timeline_approved_activity"
    )


def _evidence_pack_item_echoes_request(item: dict[str, object], request_text: str) -> bool:
    request_value = compact_whitespace(request_text).lower()
    if not request_value:
        return False
    hints = item.get("surfaceHints") if isinstance(item.get("surfaceHints"), list) else []
    values = [
        str(item.get("text") or ""),
        str(item.get("candidateText") or ""),
        str(item.get("evidencePreview") or item.get("preview") or ""),
        *(str(value) for value in hints),
    ]
    normalized_values = [compact_whitespace(value).lower() for value in values if compact_whitespace(value)]
    return bool(normalized_values) and all(
        value == request_value or (len(value) >= 12 and request_value.endswith(value))
        for value in normalized_values
    )


def _retryable_empty_generation_error(exc: DeepSeekCompletionError) -> bool:
    return bool(_structured_governed_generation_reason(exc))


def _structured_governed_generation_reason(error: BaseException) -> str:
    """Return a governed no-suggestion code only from producer diagnostics."""

    if not isinstance(error, DeepSeekCompletionError):
        return ""
    details = getattr(error, "diagnostics", None)
    if not isinstance(details, dict):
        return ""
    terminal = details.get("terminalReason")
    transport = details.get("transportReason")
    if not isinstance(terminal, str) or not isinstance(transport, str):
        return ""
    if (
        terminal in _ACTIVE_RAG_GOVERNED_TERMINAL_REASONS
        and transport in _ACTIVE_RAG_GOVERNED_TRANSPORT_REASONS
    ):
        return terminal
    return ""


def _no_suitable_generation_reason(error: BaseException) -> str:
    return _structured_governed_generation_reason(error) or "no_suitable_suggestion"


def _no_suitable_generation_error(error: BaseException, *, diagnostics: dict[str, object]) -> bool:
    """Keep provider failures observable without turning them into a dead UI."""

    _ = diagnostics
    # A provider can use the same compact public message for a governed empty
    # result and a transport/stream failure.  Only the structured producer
    # codes above can make the former a completed no-suggestion Trace.
    return bool(_structured_governed_generation_reason(error))


def _timeline_evidence_from_core(core: LocalSqliteCoreClient, request: ActiveRagStartRequest) -> tuple[ActiveRagEvidence, ...]:
    items = timeline_evidence_pack_from_core(
        core,
        project=request.project,
        app=request.app or request.front_app_bundle_id,
        current_context=request.context or request.surrounding_before,
        selected_text=request.selected_text,
        max_items=96,
    )
    result: list[ActiveRagEvidence] = []
    for index, item in enumerate(items, start=1):
        source_type = compact_whitespace(str(item.get("sourceType") or "timeline"))
        source_lane = compact_whitespace(str(item.get("sourceLane") or "timeline_context"))
        if source_type not in {
            "recent_input_context",
            "daily_plan",
            "todo",
            "goal",
            "activity_timeline",
        }:
            # Atom and Book facts must come through the normal hybrid
            # retriever. Timeline context only carries continuity and intent.
            continue
        title = compact_whitespace(str(item.get("title") or ""))
        summary = compact_whitespace(str(item.get("summary") or item.get("evidencePreview") or ""))
        if source_type == "recent_input_context":
            text = _timeline_recent_input_text(summary)
        else:
            surface_hints = item.get("surfaceHints") if isinstance(item.get("surfaceHints"), list) else []
            text = compact_whitespace(str(surface_hints[0])) if surface_hints else (summary or title)
        if not text:
            continue
        result.append(
            ActiveRagEvidence(
                evidence_id=compact_whitespace(
                    str(
                        item.get("activityId")
                        or item.get("timelineId")
                        or item.get("bookId")
                        or f"timeline:{index}"
                    )
                ),
                text=text,
                source_type=source_type,
                source_lane=source_lane,
                score=0.55,
                confidence=0.6,
                tags=tuple(str(tag) for tag in item.get("tags", []) if compact_whitespace(str(tag)))
                if isinstance(item.get("tags"), list)
                else (),
                book_ids=(str(item.get("bookId")),) if item.get("bookId") else (),
                evidence_event_ids=tuple(int(value) for value in item.get("sourceEventIds", []) if isinstance(value, int))
                if isinstance(item.get("sourceEventIds"), list)
                else (),
                preview=summary,
                metadata={
                    **dict(item.get("metadata") if isinstance(item.get("metadata"), dict) else {}),
                    "title": title,
                    "summary": summary,
                    "surfaceHints": item.get("surfaceHints") if isinstance(item.get("surfaceHints"), list) else [],
                    "timelineContext": True,
                    "sourceType": source_type,
                    "contextOnly": True,
                    "maySupportIntent": True,
                    "maySupportFacts": False,
                },
            )
        )
    return tuple(result)


def _timeline_recent_input_text(summary: str) -> str:
    """Keep committed text and useful field context from recent-input rows.

    ``recent_input_context`` serializes each row as
    ``committed | context: surrounding | preedit: ...``.  Dropping everything
    after the first context marker makes a two-character commit look like the
    whole foreground request.  Preedit is not semantic context, but the
    surrounding field text is, so preserve it as a separate clause.
    """

    clauses: list[str] = []
    for raw_segment in summary.split(" / "):
        segment = compact_whitespace(raw_segment.split(" | preedit:", 1)[0])
        committed, marker, surrounding = segment.partition(" | context:")
        committed = compact_whitespace(committed)
        values = (committed, surrounding if marker and len(committed) < 8 else "")
        for value in values:
            text = compact_whitespace(value)
            if text and text not in clauses:
                clauses.append(text)
    return compact_whitespace("。".join(clauses))


def _effective_active_rag_context(
    request: ActiveRagStartRequest,
    *,
    evidence: tuple[ActiveRagEvidence, ...],
) -> tuple[str, str, dict[str, object]]:
    foreground = compact_whitespace(request.context or request.surrounding_before)
    recent_items = _recent_input_history_evidence(evidence)
    recent_chars = sum(len(compact_whitespace(item.text)) for item in recent_items)
    # Explicit generation must never silently replace the live editable field
    # with an older Timeline row.  A short foreground capture is degraded
    # evidence, not permission to promote recent input into CurrentInput.
    # Recent input is injected separately as continuity context. It never
    # replaces CurrentInput and never becomes grounding evidence.
    effective = foreground or compact_whitespace(request.selected_text)
    augmented = False
    effective = compact_whitespace(effective)
    source = _active_rag_context_source(request)
    if not foreground and effective:
        source = "selected_text_fallback"
    return effective, source, {
        "foregroundContextChars": len(foreground),
        "effectiveContextChars": len(effective),
        "timelineRecentInputChars": recent_chars,
        "timelineRecentInputRecordCount": len(recent_items),
        "augmentedWithTimelineRecentInput": augmented,
        "timelineRecentInputUsedForGeneration": bool(recent_items),
        "contextPolicy": "foreground_primary_history_secondary",
        "contextTruncatedToBudget": False,
    }


def _seed_active_rag_context_diagnostics(
    diagnostics: dict[str, object],
    *,
    request: ActiveRagStartRequest,
    evidence: tuple[ActiveRagEvidence, ...],
    remote_model_ready: bool,
) -> None:
    """Expose foreground and history truth before a remote request is built."""

    effective, source, meta = _effective_active_rag_context(request, evidence=evidence)
    existing = dict(diagnostics.get("contextInjection") or {})
    warnings = [str(item) for item in existing.get("warnings") or []]
    history_available = bool(meta["timelineRecentInputRecordCount"])
    history_used = bool(remote_model_ready and history_available)
    if history_available and not remote_model_ready:
        warnings.append("recent_input_history_available_not_injected")
    diagnostics["contextInjection"] = {
        **existing,
        "applied": False,
        "source": source,
        "contextChars": len(effective),
        "foregroundContextChars": meta["foregroundContextChars"],
        "effectiveContextChars": meta["effectiveContextChars"],
        "timelineRecentInputChars": meta["timelineRecentInputChars"],
        "timelineRecentInputRecordCount": meta["timelineRecentInputRecordCount"],
        "augmentedWithTimelineRecentInput": False,
        "timelineRecentInputUsedForGeneration": history_used,
        "contextPolicy": meta["contextPolicy"],
        "contextHash": stable_text_hash(effective) if effective else "",
        "selectedTextChars": len(request.selected_text),
        "selectedTextHash": request.selected_text_hash,
        "resolvedRequestChars": len(effective),
        "shortForegroundCapture": int(meta["foregroundContextChars"]) < 8,
        "fullForegroundDocumentCaptured": False,
        "remoteModelReady": remote_model_ready,
        "warnings": list(dict.fromkeys(warnings)),
    }
    diagnostics["contextView"] = _active_rag_request_context_view(request)


def _recent_input_history_evidence(
    evidence: tuple[ActiveRagEvidence, ...],
) -> tuple[ActiveRagEvidence, ...]:
    result: list[ActiveRagEvidence] = []
    seen: set[str] = set()
    for item in evidence:
        if item.source_type != "recent_input_context" and item.source_lane != "timeline_recent_input":
            continue
        text = compact_whitespace(item.text)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(item)
    # The timeline/context packet owns the configured count and character
    # budget.  Keeping a second historical four-item cap here made the
    # settings consumer silently discard newer records before that packet
    # could report requested/effective/actual receipt fields.
    return tuple(result[-200:])


def _recent_context_overlap(recent: str, foreground: str) -> int:
    maximum = min(len(recent), len(foreground), 120)
    for width in range(maximum, 7, -1):
        if recent.endswith(foreground[:width]):
            return width
    return 0


def _merge_evidence(
    primary: tuple[ActiveRagEvidence, ...],
    secondary: tuple[ActiveRagEvidence, ...],
) -> tuple[ActiveRagEvidence, ...]:
    result: list[ActiveRagEvidence] = []
    seen: set[str] = set()
    for item in (*primary, *secondary):
        key = item.evidence_id or f"{item.source_lane}:{compact_whitespace(item.text).lower()}"
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


def _deepseek_evidence_pack(
    *,
    request: ActiveRagStartRequest,
    evidence: tuple[ActiveRagEvidence, ...],
) -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    resolved_query = _resolved_active_rag_request_text(request)
    for item in (*request.evidence_pack, *(_evidence_payload(evidence_item) for evidence_item in evidence[:10])):
        if not isinstance(item, dict):
            continue
        if _evidence_pack_item_is_context_only(item) or _evidence_pack_item_echoes_request(item, resolved_query):
            continue
        key = compact_whitespace(
            "|".join(
                str(item.get(field) or "")
                for field in ("sourceType", "sourceLane", "title", "text", "evidencePreview", "summary")
            )
        ).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= 12:
            break
    return tuple(result)


def _session_payload(session: ActiveRagSession) -> dict[str, object]:
    ready = session.status == "ready" and not _active_rag_worker_pending(session)
    elapsed_ms = max(0, now_ms() - session.created_at_ms)
    poll_after_ms = _active_rag_poll_after_ms(status=session.status, elapsed_ms=elapsed_ms)
    candidates = [
        _active_rag_display_candidate_payload(item, session=session, index=index)
        for index, item in enumerate(session.candidates, start=1)
    ]
    if session.status == "pending" and not candidates:
        candidates = [_active_rag_thinking_candidate_payload(session)]
    if (
        session.status == "ready"
        and not candidates
        and bool((session.diagnostics.get("generation") or {}).get("noSuitableSuggestion"))
    ):
        candidates = [_active_rag_no_suggestion_candidate_payload(session)]
    if session.status == "error" and not candidates:
        candidates = [_active_rag_error_candidate_payload(session)]
    grounding_evidence = _grounding_evidence(session.evidence)
    diagnostics = dict(session.diagnostics)
    diagnostics["progress"] = _active_rag_progress_payload(session)
    return {
        "schemaVersion": ACTIVE_RAG_SERVICE_SCHEMA_VERSION,
        "sessionId": session.session_id,
        "status": session.status,
        "thinkingRow": session.status == "pending",
        "uiMode": ACTIVE_RAG_MODE,
        "keyPolicy": active_rag_key_policy(ready=ready),
        "selectedTextHash": session.request.selected_text_hash,
        "frontendRevision": session.request.frontend_revision,
        "selectionEpoch": session.request.selection_epoch,
        "panelSessionId": session.request.panel_session_id,
        "frontAppBundleId": session.request.front_app_bundle_id,
        "placement": session.request.placement,
        "intent": session.request.intent,
        "evidenceCount": len(grounding_evidence),
        "candidateCount": len(session.candidates),
        "candidates": candidates,
        "evidence": [_redacted_evidence_payload(item) for item in grounding_evidence[:8]],
        "diagnostics": diagnostics,
        "traceEvents": list(session.trace_events),
        "error": session.error,
        "createdAtMs": session.created_at_ms,
        "updatedAtMs": session.updated_at_ms,
        "elapsedMs": elapsed_ms,
        "pollAfterMs": poll_after_ms,
        "expiresAfterMs": session.request.latency_budget_ms,
    }


def _active_rag_progress_payload(session: ActiveRagSession) -> dict[str, object]:
    diagnostics = session.diagnostics
    retrieval = diagnostics.get("retrieval") if isinstance(diagnostics.get("retrieval"), dict) else {}
    context = (
        diagnostics.get("contextInjection")
        if isinstance(diagnostics.get("contextInjection"), dict)
        else {}
    )
    model = diagnostics.get("modelRequest") if isinstance(diagnostics.get("modelRequest"), dict) else {}
    retrying = bool(model.get("contentRetryAttempted")) and not bool(model.get("completed"))
    worker_pending = _active_rag_worker_pending(session)
    # A visible-timeout fallback is a UI result row, not producer settlement:
    # the worker remains pending while the provider closes its open stage. Keep
    # projecting that stage until the worker actually settles, otherwise the
    # foreground card claims "ready" while generation is still in flight.
    if session.status == "ready" and not worker_pending:
        # Error fallback intentionally keeps the retriable UI row in ``ready``
        # status, but its producer outcome is still failed. Project that
        # terminal fact through the stage so the native card cannot present a
        # transport failure as a successful generation.
        stage = "error" if session.trace_outcome == "failed" else "ready"
    elif session.status in {"error", "cancelled", "stale_dropped"}:
        stage = session.status
    elif retrying:
        stage = "quality_retry"
    elif bool(model.get("partialVisible")):
        stage = "streaming"
    elif bool(model.get("attempted")):
        stage = "generating"
    elif bool(retrieval.get("attempted")):
        stage = "retrieval_complete"
    else:
        stage = "capturing_context"

    window_context = session.request.window_context if isinstance(session.request.window_context, dict) else {}
    grounding = _grounding_evidence(session.evidence)
    return {
        "stage": stage,
        "workerPending": worker_pending,
        "elapsedMs": max(0, now_ms() - session.created_at_ms),
        "context": {
            "foregroundChars": max(
                0,
                int(
                    context.get("foregroundContextChars")
                    or session.request.frontend_context_chars
                    or len(session.request.context)
                ),
            ),
            "windowNodeCount": max(0, int(window_context.get("nodeCount") or 0)),
            "windowCaptureMode": compact_whitespace(str(window_context.get("captureMode") or "")),
            "recentInputCount": max(0, int(context.get("timelineRecentInputRecordCount") or 0)),
            "recentInputChars": max(0, int(context.get("timelineRecentInputChars") or 0)),
            "recentInputUsed": bool(context.get("timelineRecentInputUsedForGeneration")),
        },
        "retrieval": {
            "attempted": bool(retrieval.get("attempted")),
            "elapsedMs": max(0.0, float(retrieval.get("elapsedMs") or 0.0)),
            "retrievedCount": max(0, int(retrieval.get("retrievedCount") or 0)),
            "evidenceCount": len(grounding),
            "contextEvidenceCount": max(0, int(retrieval.get("contextEvidenceCount") or 0)),
            "items": [_active_rag_progress_evidence_item(item) for item in grounding[:ACTIVE_RAG_PROGRESS_MAX_ITEMS]],
        },
        "model": {
            "attempted": bool(model.get("attempted")),
            "partialVisible": bool(model.get("partialVisible")),
            "partialChars": max(0, int(model.get("partialChars") or 0)),
            "firstTokenMs": max(0, int(model.get("firstTokenMs") or 0)),
            "providerFirstTokenMs": max(0, int(model.get("providerFirstTokenMs") or 0)),
            "providerElapsedMs": max(0, int(model.get("providerElapsedMs") or 0)),
            "qualityRetry": bool(model.get("contentRetryAttempted")),
            "qualityRetryReason": compact_whitespace(str(model.get("contentRetryReason") or "")),
        },
    }


def _active_rag_progress_evidence_item(evidence: ActiveRagEvidence) -> dict[str, object]:
    payload = _evidence_payload(evidence)
    title = compact_whitespace(str(payload.get("title") or ""))
    preview = compact_whitespace(
        str(payload.get("evidencePreview") or payload.get("text") or evidence.preview or evidence.text)
    )
    if not title:
        surface_hints = payload.get("surfaceHints") if isinstance(payload.get("surfaceHints"), list) else []
        title = next(
            (compact_whitespace(str(item)) for item in surface_hints if compact_whitespace(str(item))),
            "",
        )
    if not title:
        generic_tags = {"rag", "memory", "记忆", "todo", "planning"}
        tags = [
            compact_whitespace(str(item))
            for item in evidence.tags
            if compact_whitespace(str(item))
            and compact_whitespace(str(item)).lower() not in generic_tags
        ]
        title = " · ".join(tags[:2])
    if not title:
        title = _active_rag_progress_source_label(evidence.source_type, evidence.source_lane)
    return {
        "sourceType": evidence.source_type,
        "sourceLane": evidence.source_lane,
        "title": title[:64],
        "preview": preview[:ACTIVE_RAG_PROGRESS_PREVIEW_CHARS],
    }


def _active_rag_progress_source_label(source_type: str, source_lane: str) -> str:
    normalized = f"{source_type} {source_lane}".lower()
    if "todo" in normalized or "planning" in normalized:
        return "计划任务"
    if "book" in normalized:
        return "记忆工具书"
    if "memory" in normalized or "atom" in normalized:
        return "个人记忆"
    return "检索依据"


def _active_rag_request_context_view(request: ActiveRagStartRequest) -> dict[str, object]:
    """Build the truthful pre-provider fallback shown by the native surface."""

    current_context = compact_whitespace(request.context or request.surrounding_before)
    return {
        "schemaVersion": "rag-ime.active-rag-context-view.v1",
        "source": "frontend_request",
        "currentRequest": _resolved_active_rag_request_text(request),
        "currentContext": current_context,
        "selectedText": compact_whitespace(request.selected_text),
        "taskMode": compact_whitespace(request.intent),
        "groundingMode": "pending",
        "windowContext": project_window_context_for_generation(request.window_context),
        "recentCompleteInputs": [],
        "planning": {},
        "activityTimeline": {},
        "groundingEvidence": [],
        "evidenceHints": [],
        "contextBudget": {},
    }


def _active_rag_context_view_from_messages(messages: list[dict[str, str]]) -> dict[str, object]:
    """Project the exact redacted user message into a foreground-safe viewer.

    The full system prompt is deliberately excluded. The returned sections are
    copied from the already-built provider request, so the UI never reconstructs
    or guesses what Pi/model transport received.
    """

    user_message = next(
        (item for item in reversed(messages) if str(item.get("role") or "") == "user"),
        {},
    )
    try:
        payload = json.loads(str(user_message.get("content") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    context_packet = payload.get("contextPacket") if isinstance(payload.get("contextPacket"), dict) else {}
    return {
        "schemaVersion": "rag-ime.active-rag-context-view.v1",
        "source": "provider_request",
        "currentRequest": str(payload.get("currentRequest") or ""),
        "currentContext": str(payload.get("currentContext") or ""),
        "selectedText": str(payload.get("selectedText") or ""),
        "taskMode": str(payload.get("taskMode") or ""),
        "groundingMode": str(payload.get("groundingMode") or ""),
        "windowContext": dict(context_packet.get("windowContext") or {})
        if isinstance(context_packet.get("windowContext"), dict)
        else {},
        "recentCompleteInputs": list(context_packet.get("recentCompleteInputs") or [])
        if isinstance(context_packet.get("recentCompleteInputs"), list)
        else [],
        "planning": dict(context_packet.get("planning") or {})
        if isinstance(context_packet.get("planning"), dict)
        else {},
        "activityTimeline": dict(context_packet.get("activityTimeline") or {})
        if isinstance(context_packet.get("activityTimeline"), dict)
        else {},
        "groundingEvidence": list(
            payload.get("groundingEvidence")
            or context_packet.get("groundingEvidence")
            or []
        )
        if isinstance(
            payload.get("groundingEvidence")
            or context_packet.get("groundingEvidence"),
            list,
        )
        else [],
        "evidenceHints": list(payload.get("evidenceHints") or [])
        if isinstance(payload.get("evidenceHints"), list)
        else [],
        "contextBudget": dict(context_packet.get("contextBudget") or {})
        if isinstance(context_packet.get("contextBudget"), dict)
        else {},
    }


def _mark_active_rag_quality_retry(
    diagnostics: dict[str, object] | None,
    *,
    reason: str,
) -> None:
    if diagnostics is None:
        return
    model_request = dict(diagnostics.get("modelRequest") or {})
    model_request.update(
        {
            "contentRetryAttempted": True,
            "contentRetryReason": compact_whitespace(reason),
            "retryStartedAtMs": now_ms(),
        }
    )
    diagnostics["modelRequest"] = model_request


def _active_rag_poll_after_ms(*, status: str, elapsed_ms: int) -> int:
    if status != "pending":
        return 0
    # The status payload is local and lightweight. Poll at UI-frame-friendly
    # cadence so streamed text does not arrive in two-to-three-second jumps.
    if elapsed_ms < 30_000:
        return 100
    return 160


def _should_force_visible_fallback(session: ActiveRagSession) -> bool:
    elapsed_ms = max(0, now_ms() - session.created_at_ms)
    request_budget = max(100, int(session.request.latency_budget_ms or ACTIVE_RAG_VISIBLE_READY_TIMEOUT_MS))
    visible_budget = min(ACTIVE_RAG_VISIBLE_READY_TIMEOUT_MS, max(1_200, request_budget))
    return elapsed_ms >= visible_budget


def _remote_completion_budget_ms(request: ActiveRagStartRequest) -> int:
    request_budget = max(100, int(request.latency_budget_ms or ACTIVE_RAG_VISIBLE_READY_TIMEOUT_MS))
    return min(ACTIVE_RAG_VISIBLE_READY_TIMEOUT_MS, max(1_200, request_budget))


def _remote_active_rag_route(
    provider: object | None,
    *,
    request: ActiveRagStartRequest,
) -> dict[str, object]:
    request_allowed = request.remote_model_allowed is not False
    provider_available = provider is not None
    credentials_configured = _provider_credentials_configured(provider)
    scene_enabled = True
    scene_reason = ""
    if not bool(getattr(provider, "uses_managed_pi", False)):
        try:
            assert_deepseek_scene_allowed("active_rag")
        except RuntimeError as exc:
            scene_enabled = False
            scene_reason = str(exc)
    gates = {
        "explicitActiveRagOnly": True,
        "requestAllowsRemoteModel": request_allowed,
        "sceneEnvEnabled": scene_enabled,
        "providerAvailable": provider_available,
        "credentialsConfigured": credentials_configured,
    }
    skip_reason = ""
    if not request_allowed:
        skip_reason = request.remote_model_skip_reason or "settings_remote_model_disabled"
    elif not provider_available:
        skip_reason = "provider_unavailable"
    elif not scene_enabled:
        skip_reason = "scene_flag_disabled"
    elif not credentials_configured:
        skip_reason = "credentials_missing"
    return {
        "schemaVersion": ACTIVE_RAG_ROUTE_STATUS_SCHEMA_VERSION,
        "route": (
            "explicit_active_rag_pi_surface"
            if bool(getattr(provider, "uses_managed_pi", False))
            else "explicit_active_rag_knowledge_provider"
        ),
        "provider": _provider_name(provider),
        "model": _provider_model(provider),
        "ready": all(gates.values()),
        "attempted": False,
        "skipReason": skip_reason,
        "reason": scene_reason if not scene_enabled else "",
        "gates": gates,
        "managementGates": dict(request.remote_model_gates),
        "stream": True,
        "passivePostCommitRemoteAllowed": False,
    }


def _initial_session_diagnostics(
    request: ActiveRagStartRequest,
    *,
    completion_provider: object | None,
) -> dict[str, object]:
    route = _remote_active_rag_route(completion_provider, request=request)
    context_text = request.context or request.surrounding_before
    context_fingerprint = text_fingerprint(context_text)
    selected_fingerprint = text_fingerprint(request.selected_text)
    capture_warnings = _capture_validation_warnings(request, context_text=context_text)
    return {
        "schemaVersion": "rag-ime.active-rag-diagnostics.v1",
        "privacy": {"rawTextIncluded": False, "hashAlgorithm": "sha256-16"},
        "contextView": _active_rag_request_context_view(request),
        "requestCapture": {
            "contextSource": _active_rag_context_source(request),
            "selectedText": text_fingerprint(request.selected_text),
            "currentContext": text_fingerprint(request.context),
            "surroundingBefore": text_fingerprint(request.surrounding_before),
            "surroundingAfter": text_fingerprint(request.surrounding_after),
            "providedEvidenceCount": len(request.evidence_pack),
            "frontendContextChars": request.frontend_context_chars,
            "frontendContextHash": request.frontend_context_hash,
            "captureWarnings": capture_warnings,
            "windowContext": {
                "present": bool(request.window_context),
                "captureMode": str(request.window_context.get("captureMode") or ""),
                "nodeCount": int(request.window_context.get("nodeCount") or 0),
            },
        },
        "route": route,
        "contextInjection": {
            "applied": False,
            "source": _active_rag_context_source(request),
            "contextChars": context_fingerprint["chars"],
            "contextHash": stable_text_hash(context_text) if context_text else "",
            "selectedTextChars": selected_fingerprint["chars"],
            "selectedTextHash": request.selected_text_hash,
            "warnings": ["model_request_not_built", *capture_warnings],
        },
        "retrieval": {
            "called": False,
            "attempted": False,
            "retrievedCount": 0,
            "evidenceCount": 0,
            "lanes": {},
            "elapsedMs": 0.0,
            "requestEvidenceCount": len(request.evidence_pack),
        },
        "remoteModel": {
            "requested": request.remote_model_allowed is True,
            "allowed": bool(route["ready"]),
            "provider": route["provider"],
            "model": route["model"],
            "skipReason": route["skipReason"],
            "elapsedMs": 0.0,
        },
        "generation": {"displayedCandidateCount": 0},
    }


def _active_rag_context_source(request: ActiveRagStartRequest) -> str:
    if request.context_source:
        return request.context_source
    sources: list[str] = []
    if request.context:
        sources.append("currentContext")
    if request.surrounding_before:
        sources.append("surroundingBefore")
    if request.surrounding_after:
        sources.append("surroundingAfter")
    if request.selected_text:
        sources.append("selectedText")
    if request.evidence_pack:
        sources.append("requestEvidencePack")
    return "+".join(sources) or "none"


def _capture_validation_warnings(request: ActiveRagStartRequest, *, context_text: str) -> list[str]:
    warnings: list[str] = []
    if request.frontend_context_chars and request.frontend_context_chars != len(context_text):
        warnings.append("frontend_context_chars_mismatch")
    if request.frontend_context_hash and request.frontend_context_hash != stable_text_hash(context_text):
        warnings.append("frontend_context_hash_mismatch")
    if request.frontend_selected_text_chars and request.frontend_selected_text_chars != len(request.selected_text):
        warnings.append("frontend_selected_text_chars_mismatch")
    return warnings


def _active_rag_model_input_trace(
    *,
    resolved_request: str,
    context_packet: dict[str, object],
    evidence_pack: tuple[dict[str, object], ...] | list[dict[str, object]],
    messages: list[dict[str, str]],
    include_text: bool,
) -> dict[str, object]:
    packet_json = json.dumps(context_packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    evidence_items = tuple(dict(item) for item in evidence_pack)
    messages_json = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload: dict[str, object] = {
        "resolvedRequest": _trace_text_snapshot(resolved_request, include_text=include_text),
        "contextPacket": {
            "fingerprint": _trace_text_snapshot(packet_json, include_text=False),
            "sectionCounts": {
                key: len(value.get("items") or value.get("events") or value.get("recentDecisions") or [])
                for key in ("oneRing", "notebook", "timeline")
                if isinstance((value := context_packet.get(key)), dict)
            },
        },
        "evidencePack": evidence_injection_diagnostics(evidence_items, include_text=include_text),
        "messages": {
            "count": len(messages),
            "fingerprint": _trace_text_snapshot(messages_json, include_text=False),
            "roles": [str(item.get("role") or "") for item in messages],
        },
    }
    if include_text:
        payload["contextPacket"]["value"] = _bounded_trace_value(context_packet, include_text=True)
        payload["evidencePack"]["value"] = _bounded_trace_value(evidence_items, include_text=True)
        payload["messages"]["value"] = _bounded_trace_value(messages, include_text=True)
    return payload


def _completion_error_trace(error: BaseException, *, include_text: bool) -> dict[str, object]:
    details = dict(getattr(error, "diagnostics", {}) or {})
    if not include_text:
        details.pop("responsePreview", None)
    return {
        "errorType": type(error).__name__,
        "failureReason": _safe_failure_reason(error),
        **_bounded_trace_value(details, include_text=include_text),
    }


def _active_rag_chain_trace_record(
    session: ActiveRagSession,
    *,
    phase: str,
    include_text: bool,
) -> dict[str, object]:
    request = session.request
    diagnostics = dict(session.diagnostics or {})
    evidence = [_trace_evidence_item(item, include_text=include_text) for item in session.evidence]
    candidates = [_trace_candidate_item(item, include_text=include_text) for item in session.candidates]
    return {
        "schemaVersion": ACTIVE_RAG_CHAIN_TRACE_SCHEMA_VERSION,
        "timestampMs": now_ms(),
        "phase": compact_whitespace(phase),
        "sessionId": session.session_id,
        "status": session.status,
        "traceStatus": _active_rag_trace_status(session, phase),
        "terminalStage": _active_rag_terminal_stage(session, phase),
        "stageStatus": _active_rag_stage_statuses(session, phase),
        "stageTiming": _active_rag_observation_stage_timing(session),
        "error": session.error,
        "elapsedMs": max(0, now_ms() - int(session.created_at_ms)),
        "privacy": {
            "rawTextIncluded": include_text,
            "sensitiveFieldBlocked": False,
            "hashAlgorithm": "sha256-16",
        },
        "request": {
            "frontendRevision": request.frontend_revision,
            "selectionEpoch": request.selection_epoch,
            "panelSessionId": request.panel_session_id,
            "frontAppBundleId": request.front_app_bundle_id,
            "project": request.project,
            "app": request.app,
            "intent": request.intent,
            "placement": request.placement,
            "contextSource": request.context_source,
            "maxCandidates": request.max_candidates,
            "maxChars": request.max_chars,
            "latencyBudgetMs": request.latency_budget_ms,
            "selectedText": _trace_text_snapshot(request.selected_text, include_text=include_text),
            "currentContext": _trace_text_snapshot(request.context, include_text=include_text),
            "surroundingBefore": _trace_text_snapshot(request.surrounding_before, include_text=include_text),
            "surroundingAfter": _trace_text_snapshot(request.surrounding_after, include_text=include_text),
            "providedEvidenceCount": len(request.evidence_pack),
        },
        "retrieval": {
            "diagnostics": _bounded_trace_value(diagnostics.get("retrieval") or {}, include_text=include_text),
            "evidenceCount": len(evidence),
            "evidence": evidence,
        },
        "model": {
            "route": _bounded_trace_value(diagnostics.get("route") or {}, include_text=include_text),
            "contextInjection": _bounded_trace_value(
                diagnostics.get("contextInjection") or {}, include_text=include_text
            ),
            "modelInput": _bounded_trace_value(diagnostics.get("modelInput") or {}, include_text=include_text),
            "request": _bounded_trace_value(diagnostics.get("modelRequest") or {}, include_text=include_text),
            "remote": _bounded_trace_value(diagnostics.get("remoteModel") or {}, include_text=include_text),
        },
        "generation": {
            "diagnostics": _bounded_trace_value(diagnostics.get("generation") or {}, include_text=include_text),
            "candidateCount": len(candidates),
            "candidates": candidates,
        },
        "failure": _bounded_trace_value(diagnostics.get("failure") or {}, include_text=include_text),
        "traceEvents": _bounded_trace_value(session.trace_events, include_text=include_text),
    }


def _active_rag_observation_record(
    session: ActiveRagSession,
    *,
    phase: str,
) -> dict[str, object]:
    request = session.request
    timestamp_ms = now_ms()
    retrieval: dict[str, object] = {"evidenceCount": len(session.evidence)}
    if phase == "retrieval_complete":
        retrieval.update(
            {
                "evidenceStage": "retrieval_output",
                "traceEvidence": [
                    _active_rag_observation_evidence(item, rank_after=index)
                    for index, item in enumerate(session.evidence, start=1)
                ],
            }
        )
    stage_timing = _active_rag_observation_stage_timing(session)
    trace_status = _active_rag_trace_status(session, phase)
    return {
        "schemaVersion": "rag-ime.active-rag-observation.v1",
        "timestampMs": timestamp_ms,
        "phase": compact_whitespace(phase),
        "sessionId": session.session_id,
        # ``status`` is the common Trace status. ``uiStatus`` keeps the
        # foreground fallback observable without letting it settle an open
        # provider stage as completed.
        "status": trace_status,
        "uiStatus": session.status,
        "traceStatus": trace_status,
        "terminalStage": _active_rag_terminal_stage(session, phase),
        "stageStatus": _active_rag_stage_statuses(session, phase),
        "elapsedMs": max(0, timestamp_ms - int(session.created_at_ms)),
        "stageTiming": stage_timing,
        "privacy": {"rawTextIncluded": False},
        "request": {
            "frontAppBundleId": request.front_app_bundle_id,
            "project": request.project,
            "app": request.app,
            "intent": request.intent,
            "selectedText": {"chars": len(request.selected_text)},
            "currentContext": {"chars": len(request.context)},
            "providedEvidenceCount": len(request.evidence_pack),
        },
        "retrieval": retrieval,
        "generation": {"candidateCount": len(session.candidates)},
    }


def _active_rag_observation_stage_timing(
    session: ActiveRagSession,
) -> dict[str, dict[str, int]]:
    """Project only explicit, bounded lifecycle timing into the observer record."""

    projected: dict[str, dict[str, int]] = {}
    for stage in ("context", "retrieval", "generation"):
        timing = session.stage_timings.get(stage)
        if not isinstance(timing, dict):
            continue
        started = timing.get("startedAtMs")
        if isinstance(started, bool) or not isinstance(started, int) or started < 0:
            continue
        item: dict[str, int] = {"startedAtMs": started}
        ended = timing.get("endedAtMs")
        duration = timing.get("durationMs")
        if (
            isinstance(ended, int)
            and not isinstance(ended, bool)
            and ended >= started
            and isinstance(duration, int)
            and not isinstance(duration, bool)
            and duration >= 0
            and ended - started == duration
        ):
            item.update({"endedAtMs": ended, "durationMs": duration})
        projected[stage] = item
    return projected


def _active_rag_retrieval_failed(session: ActiveRagSession) -> bool:
    retrieval = session.diagnostics.get("retrieval")
    return isinstance(retrieval, dict) and bool(
        compact_whitespace(str(retrieval.get("error") or ""))
    )


def _active_rag_stage_started(session: ActiveRagSession, stage: str) -> bool:
    return isinstance(session.stage_timings.get(stage), dict) and "startedAtMs" in session.stage_timings[stage]


def _active_rag_stage_finished(session: ActiveRagSession, stage: str) -> bool:
    timing = session.stage_timings.get(stage)
    return isinstance(timing, dict) and "durationMs" in timing and "endedAtMs" in timing


def _active_rag_terminal_stage(session: ActiveRagSession, phase: str) -> str:
    """Return the producer-owned stage represented by this transition."""

    if phase == "started":
        return "context"
    if phase == "retrieval_complete":
        return "retrieval"
    if _active_rag_stage_started(session, "generation"):
        return "generation"
    if _active_rag_stage_started(session, "retrieval"):
        return "retrieval"
    return "context"


def _active_rag_stage_statuses(
    session: ActiveRagSession,
    phase: str,
) -> dict[str, str]:
    """Expose only bounded stage outcomes, never provider/private reasons."""

    result: dict[str, str] = {}
    if _active_rag_stage_started(session, "context"):
        result["context"] = "completed"
    if _active_rag_stage_started(session, "retrieval"):
        if phase in {"cancelled", "stale_dropped"} and not _active_rag_stage_started(session, "generation"):
            result["retrieval"] = "cancelled"
        elif _active_rag_retrieval_failed(session):
            result["retrieval"] = "failed"
        elif _active_rag_stage_finished(session, "retrieval"):
            result["retrieval"] = "completed"
        else:
            result["retrieval"] = "running"
    if _active_rag_stage_started(session, "generation"):
        if phase in {"cancelled", "stale_dropped"}:
            result["generation"] = "cancelled"
        elif session.trace_outcome == "failed":
            result["generation"] = "failed"
        elif _active_rag_stage_finished(session, "generation"):
            result["generation"] = "completed"
        else:
            result["generation"] = "running"
    terminal_stage = _active_rag_terminal_stage(session, phase)
    if phase in {"cancelled", "stale_dropped"}:
        # Context-only cancellation needs a terminal span so the envelope is
        # cancelled rather than looking like a completed capture.
        if terminal_stage == "context":
            result = {"context": "cancelled"}
        elif terminal_stage == "retrieval":
            result.pop("generation", None)
            result["retrieval"] = "cancelled"
    return result


def _active_rag_trace_status(session: ActiveRagSession, phase: str) -> str:
    if session.trace_outcome == "failed" or _active_rag_retrieval_failed(session):
        return "failed"
    if session.trace_outcome == "cancelled" or phase in {"cancelled", "stale_dropped"}:
        return "cancelled"
    if any(
        _active_rag_stage_started(session, stage)
        and not _active_rag_stage_finished(session, stage)
        for stage in ("retrieval", "generation")
    ):
        return "running"
    return "completed" if session.status == "ready" else "running"


def _active_rag_observation_evidence(
    evidence: ActiveRagEvidence,
    *,
    rank_after: int,
) -> dict[str, object]:
    """Return only bounded, non-content metadata for a retrieval receipt."""

    metadata = evidence.metadata if isinstance(evidence.metadata, dict) else {}
    rank_before = _positive_exact_rank(metadata.get("rankBefore", metadata.get("rank")))

    source_ref = next(
        (
            compact_whitespace(str(value))[:160]
            for values in (
                evidence.memory_ids,
                evidence.atom_ids,
                evidence.book_ids,
                evidence.evidence_event_ids,
            )
            for value in values
            if compact_whitespace(str(value))
        ),
        compact_whitespace(evidence.evidence_id)[:160],
    )
    return {
        "evidenceId": compact_whitespace(evidence.evidence_id)[:160] or f"active-rag:{rank_after}",
        "sourceKind": compact_whitespace(evidence.source_type)[:80] or "unknown",
        "sourceRef": source_ref,
        "sourceLane": compact_whitespace(evidence.source_lane)[:80] or "unknown",
        "disposition": "included",
        "scores": {
            key: round(max(-1_000_000.0, min(1_000_000.0, float(value))), 6)
            for key, value in (("score", evidence.score), ("confidence", evidence.confidence))
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        },
        "rankBefore": rank_before,
        "rankAfter": max(1, int(rank_after)),
        "omissionReason": "",
    }


def _positive_exact_rank(value: object) -> int | None:
    """Keep only measured, positive integer ranks; never truncate a float."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 1 or not number.is_integer():
        return None
    return int(number)


def _trace_text_snapshot(text: str, *, include_text: bool) -> dict[str, object]:
    value = str(text or "")
    payload = text_fingerprint(value)
    if include_text:
        payload["text"] = value[:ACTIVE_RAG_CHAIN_TRACE_TEXT_LIMIT]
        payload["truncated"] = len(value) > ACTIVE_RAG_CHAIN_TRACE_TEXT_LIMIT
    return payload


def _trace_evidence_item(item: ActiveRagEvidence, *, include_text: bool) -> dict[str, object]:
    return {
        "evidenceId": item.evidence_id,
        "sourceType": item.source_type,
        "sourceLane": item.source_lane,
        "score": item.score,
        "confidence": item.confidence,
        "tags": _bounded_trace_value(list(item.tags), include_text=include_text, field_name="tags"),
        "memoryIds": list(item.memory_ids),
        "atomIds": list(item.atom_ids),
        "bookIds": list(item.book_ids),
        "sourceEventIds": list(item.evidence_event_ids),
        "text": _trace_text_snapshot(item.text, include_text=include_text),
        "preview": _trace_text_snapshot(item.preview, include_text=include_text),
        "metadata": _bounded_trace_value(item.metadata, include_text=include_text),
    }


def _trace_candidate_item(item: ActiveRagCandidate, *, include_text: bool) -> dict[str, object]:
    return {
        "candidateId": item.candidate_id,
        "sourceType": item.source_type,
        "sourceLane": item.source_lane,
        "text": _trace_text_snapshot(item.text, include_text=include_text),
        "insertText": _trace_text_snapshot(item.insert_text, include_text=include_text),
        "metadata": _bounded_trace_value(item.metadata, include_text=include_text),
    }


def _bounded_trace_value(
    value: object,
    *,
    include_text: bool,
    field_name: str = "",
) -> object:
    normalized_field = re.sub(r"[^a-z0-9]", "", field_name.lower())
    if not include_text and _trace_field_matches(normalized_field, _TRACE_SECRET_FIELD_TOKENS):
        return "[REDACTED]"
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in list(value.items())[:160]:
            name = str(key)
            result[name] = _bounded_trace_value(
                item,
                include_text=include_text,
                field_name=name,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _bounded_trace_value(
                item,
                include_text=include_text,
                field_name=field_name,
            )
            for item in list(value)[:160]
        ]
    if isinstance(value, str):
        structural_string = normalized_field.endswith("hash") or "reason" in normalized_field
        if (
            not include_text
            and not structural_string
            and _trace_field_matches(normalized_field, _TRACE_TEXT_FIELD_TOKENS)
        ):
            return _trace_text_snapshot(value, include_text=False)
        return value[:ACTIVE_RAG_CHAIN_TRACE_TEXT_LIMIT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _trace_field_matches(normalized_field: str, tokens: tuple[str, ...]) -> bool:
    return any(re.sub(r"[^a-z0-9]", "", token.lower()) in normalized_field for token in tokens)


def _append_active_rag_chain_trace(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    current_size = path.stat().st_size if path.exists() else 0
    if current_size and current_size + len(encoded) > ACTIVE_RAG_CHAIN_TRACE_MAX_BYTES:
        rotated = Path(f"{path}.1")
        rotated.unlink(missing_ok=True)
        path.replace(rotated)
    with path.open("ab") as handle:
        handle.write(encoded)


def _provider_model(provider: object | None) -> str:
    config = getattr(provider, "config", None)
    model = getattr(config, "model", "") if config is not None else ""
    return compact_whitespace(str(model)) or ("test-or-custom-provider" if provider is not None else "")


def _provider_name(provider: object | None) -> str:
    config = getattr(provider, "config", None)
    value = getattr(config, "provider_name", "") if config is not None else ""
    return compact_whitespace(str(value)) or ("custom" if provider is not None else "")


def _completion_delta_diagnostics(
    deltas: tuple[CompletionCandidateDelta, ...],
    *,
    provider: object | None,
) -> dict[str, object]:
    metadata = [dict(item.metadata or {}) for item in deltas]
    summary: dict[str, object] = {
        "parseModes": sorted(
            {compact_whitespace(str(item.get("parseMode") or "")) for item in metadata}
            - {""}
        ),
        "finishReasons": sorted(
            {compact_whitespace(str(item.get("finishReason") or "")) for item in metadata}
            - {""}
        ),
        "doneMarkerSeen": any(bool(item.get("doneMarkerSeen")) for item in metadata),
        "streamInterrupted": any(bool(item.get("streamInterrupted")) for item in metadata),
        "continuationAttempted": any(bool(item.get("continuationAttempted")) for item in metadata),
        "continuationAdvanced": any(bool(item.get("continuationAdvanced")) for item in metadata),
        "continuationCompleted": any(bool(item.get("continuationCompleted")) for item in metadata),
        "continuationRounds": max((int(item.get("continuationRounds") or 0) for item in metadata), default=0),
    }
    transport_modes = sorted(
        {compact_whitespace(str(item.get("transportMode") or "")) for item in metadata}
        - {""}
    )
    if transport_modes:
        summary["transportModes"] = transport_modes
    thinking_levels = sorted(
        {compact_whitespace(str(item.get("thinkingLevel") or "")) for item in metadata}
        - {""}
    )
    if thinking_levels:
        summary["thinkingLevels"] = thinking_levels
    first_token_values = [
        int(item.get("firstTokenMs") or 0)
        for item in metadata
        if int(item.get("firstTokenMs") or 0) > 0
    ]
    elapsed_values = [
        int(item.get("elapsedMs") or 0)
        for item in metadata
        if int(item.get("elapsedMs") or 0) > 0
    ]
    if first_token_values:
        summary["providerFirstTokenMs"] = min(first_token_values)
    if elapsed_values:
        summary["providerElapsedMs"] = max(elapsed_values)
    proxy_values = [bool(item.get("proxyBypassed")) for item in metadata if "proxyBypassed" in item]
    if proxy_values:
        summary["proxyBypassed"] = all(proxy_values)
    elif hasattr(provider, "proxy_bypassed"):
        summary["proxyBypassed"] = bool(getattr(provider, "proxy_bypassed"))
    return summary


def _streamed_partial_is_complete(text: str) -> bool:
    return active_rag_text_is_complete(text)


def _provider_credentials_configured(provider: object | None) -> bool:
    if provider is None:
        return False
    config = getattr(provider, "config", None)
    if config is None or not hasattr(config, "api_key"):
        return True
    return bool(compact_whitespace(str(getattr(config, "api_key", ""))))


def _candidate_source_counts(candidates: tuple[ActiveRagCandidate, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in candidates:
        key = item.source_lane or item.source_type or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _trace_event(name: str, **fields: object) -> dict[str, object]:
    return {"name": name, "atMs": now_ms(), "fields": fields}


def _append_trace_event(session: ActiveRagSession, name: str, **fields: object) -> None:
    session.trace_events.append(_trace_event(name, **fields))


def _safe_failure_reason(error: BaseException) -> str:
    message = compact_whitespace(str(error))[:240]
    message = re.sub(r"\bsk-[A-Za-z0-9_-]{6,}\b", "[REDACTED_SECRET]", message)
    message = re.sub(r"(?:/Users/|/Volumes/|/var/folders/)[^\s，。；;]+", "[REDACTED_PATH]", message)
    return message or type(error).__name__


def _active_rag_thinking_candidate_payload(session: ActiveRagSession) -> dict[str, object]:
    waiting_ms = max(0, now_ms() - int(session.created_at_ms))
    suffix = thinking_animation_suffix(waiting_ms)
    metadata = {
        "activeRag": True,
        "activeRagThinking": True,
        "activeRagSessionId": session.session_id,
        "selectedTextHash": session.request.selected_text_hash,
        "frontendRevision": session.request.frontend_revision,
        "selectionEpoch": session.request.selection_epoch,
        "panelSessionId": session.request.panel_session_id,
        "frontAppBundleId": session.request.front_app_bundle_id,
        "candidateOrdinal": 0,
        "visibleLabel": "",
        "animated": True,
        "animationFrame": thinking_animation_frame(waiting_ms),
        "waitingMs": waiting_ms,
    }
    return {
        "candidateId": f"{session.session_id}:thinking",
        "label": "",
        "visibleLabel": "",
        "selectionKey": None,
        "selectionRank": 0,
        "candidateOrdinal": 0,
        "candidateStableId": f"{session.session_id}:thinking",
        "snapshotId": session.session_id,
        "snapshotGeneration": 1,
        "text": f"正在生成{suffix}",
        "insertText": "",
        "sourceType": "status",
        "sourceLane": "active_rag_status",
        "selectionAction": "none",
        "sourceIndex": 0,
        "comment": "active_rag",
        "badge": "查忆",
        "sourceBadge": "查忆",
        "colorToken": "statusGray",
        "sourceStability": "fresh",
        "hardContextAnchor": "",
        "queryAnchor": "",
        "displayAnchor": "",
        "expiresAtMs": 0,
        "minVisibleUntilMs": 0,
        "evidencePreview": "",
        "expandedEvidence": "",
        "suggestionId": "",
        "memoryId": "",
        "sourceEventId": None,
        "rimeIndex": None,
        "displayLayout": "status_row",
        "displayLane": "active_rag_status",
        "group": "status",
        "groupLabel": "状态",
        "isSelectable": False,
        "isStatus": True,
        "metadata": metadata,
    }


def _active_rag_error_candidate_payload(session: ActiveRagSession) -> dict[str, object]:
    metadata = {
        "activeRag": True,
        "activeRagError": True,
        "activeRagSessionId": session.session_id,
        "selectedTextHash": session.request.selected_text_hash,
        "frontendRevision": session.request.frontend_revision,
        "selectionEpoch": session.request.selection_epoch,
        "panelSessionId": session.request.panel_session_id,
        "frontAppBundleId": session.request.front_app_bundle_id,
        "candidateOrdinal": 0,
        "visibleLabel": "",
        "error": session.error,
    }
    return {
        "candidateId": f"{session.session_id}:error",
        "label": "",
        "visibleLabel": "",
        "selectionKey": None,
        "selectionRank": 0,
        "candidateOrdinal": 0,
        "candidateStableId": f"{session.session_id}:error",
        "snapshotId": session.session_id,
        "snapshotGeneration": 1,
        "text": "暂未完成，可以重试",
        "insertText": "",
        "sourceType": "status",
        "sourceLane": "active_rag_status",
        "selectionAction": "none",
        "sourceIndex": 0,
        "comment": "active_rag_error",
        "badge": "查忆",
        "sourceBadge": "查忆",
        "colorToken": "statusGray",
        "sourceStability": "fresh",
        "hardContextAnchor": "",
        "queryAnchor": "",
        "displayAnchor": "",
        "expiresAtMs": 0,
        "minVisibleUntilMs": 0,
        "evidencePreview": "",
        "expandedEvidence": "",
        "suggestionId": "",
        "memoryId": "",
        "sourceEventId": None,
        "rimeIndex": None,
        "displayLayout": "status_row",
        "displayLane": "active_rag_status",
        "group": "status",
        "groupLabel": "状态",
        "isSelectable": False,
        "isStatus": True,
        "metadata": metadata,
    }


def _active_rag_no_suggestion_candidate_payload(session: ActiveRagSession) -> dict[str, object]:
    reason = str((session.diagnostics.get("generation") or {}).get("noSuitableReason") or "")
    status_text = (
        "这次没有合适建议"
        if reason
        in {
            "empty_remote_content",
            "governor_rejected_content",
            "no_insertable_content",
            "remote_generation_returned_no_insertable_content",
            "local_rag_returned_no_insertable_content",
        }
        else "暂未完成，可以重试"
    )
    metadata = {
        "activeRag": True,
        "activeRagNoSuggestion": True,
        "activeRagSessionId": session.session_id,
        "selectedTextHash": session.request.selected_text_hash,
        "frontendRevision": session.request.frontend_revision,
        "selectionEpoch": session.request.selection_epoch,
        "panelSessionId": session.request.panel_session_id,
        "frontAppBundleId": session.request.front_app_bundle_id,
        "candidateOrdinal": 0,
        "visibleLabel": "",
        "reason": reason,
    }
    return {
        "candidateId": f"{session.session_id}:no-suggestion",
        "label": "",
        "visibleLabel": "",
        "selectionKey": None,
        "selectionRank": 0,
        "candidateOrdinal": 0,
        "candidateStableId": f"{session.session_id}:no-suggestion",
        "snapshotId": session.session_id,
        "snapshotGeneration": 1,
        "text": status_text,
        "insertText": "",
        "sourceType": "status",
        "sourceLane": "active_rag_status",
        "selectionAction": "none",
        "sourceIndex": 0,
        "comment": "active_rag_no_suggestion",
        "badge": "查忆",
        "sourceBadge": "查忆",
        "colorToken": "statusGray",
        "sourceStability": "fresh",
        "hardContextAnchor": "",
        "queryAnchor": "",
        "displayAnchor": "",
        "expiresAtMs": 0,
        "minVisibleUntilMs": 0,
        "evidencePreview": "",
        "expandedEvidence": "",
        "suggestionId": "",
        "memoryId": "",
        "sourceEventId": None,
        "rimeIndex": None,
        "displayLayout": "status_row",
        "displayLane": "active_rag_status",
        "group": "status",
        "groupLabel": "状态",
        "isSelectable": False,
        "isStatus": True,
        "metadata": metadata,
    }


def _active_rag_display_candidate_payload(
    item: ActiveRagCandidate,
    *,
    session: ActiveRagSession,
    index: int,
) -> dict[str, object]:
    streaming_partial = bool(item.metadata.get("streamingPartial")) or session.status == "pending"
    label = "" if streaming_partial else str(index % 10 or 0)
    metadata = {
        **dict(item.metadata),
        "activeRag": True,
        "activeRagSessionId": session.session_id,
        "sourceLane": item.source_lane,
        "selectedTextHash": session.request.selected_text_hash,
        "frontendRevision": session.request.frontend_revision,
        "selectionEpoch": session.request.selection_epoch,
        "panelSessionId": session.request.panel_session_id,
        "frontAppBundleId": session.request.front_app_bundle_id,
        "compositionHash": "",
        "committedContextHash": "",
        "candidateOrdinal": index,
        "visibleLabel": label,
        "sourceBadge": "模" if item.source_type == "model" else "忆",
        # A streaming candidate changes text several times before it is final.
        # Keep its UI identity tied to the session slot so AppKit can update the
        # row in place instead of treating every delta as a different choice.
        "candidateStableId": f"{session.session_id}:result:{index}",
        "placement": session.request.placement,
        "intent": session.request.intent,
    }
    return {
        "candidateId": item.candidate_id,
        "label": label,
        "visibleLabel": label,
        "selectionKey": None if streaming_partial else label,
        "selectionRank": 0 if streaming_partial else index,
        "candidateOrdinal": index,
        "candidateStableId": metadata["candidateStableId"],
        "snapshotId": session.session_id,
        "snapshotGeneration": 1,
        "text": item.text,
        "insertText": item.insert_text,
        "sourceType": item.source_type,
        "sourceLane": item.source_lane,
        "selectionAction": "none" if streaming_partial else "commit_side_candidate",
        "sourceIndex": index - 1,
        "comment": item.source_lane,
        "badge": metadata["sourceBadge"],
        "sourceBadge": metadata["sourceBadge"],
        "colorToken": "modelBlue" if item.source_type == "model" else "memoryPurple",
        "sourceStability": "fresh",
        "hardContextAnchor": "",
        "queryAnchor": "",
        "displayAnchor": "",
        "expiresAtMs": 0,
        "minVisibleUntilMs": 0,
        "evidencePreview": "",
        "expandedEvidence": "",
        "suggestionId": item.candidate_id,
        "memoryId": str(item.metadata.get("memoryId") or item.candidate_id),
        "sourceEventId": None,
        "rimeIndex": None,
        "displayLayout": "block",
        "displayLane": item.source_lane or "active_rag",
        "group": "prediction",
        "groupLabel": "预测",
        # Streaming text is feedback, not a commit-ready answer. Its stable row
        # may update smoothly while pending, but Tab becomes active only after
        # the provider and semantic-boundary governor finalize the document.
        "isSelectable": not streaming_partial,
        "isStatus": False,
        "metadata": metadata,
    }


def _frame_from_request(request: ActiveRagStartRequest) -> ActiveRagFrame:
    return ActiveRagFrame(
        # Keep the transport anchor separate. Retrieval chooses the complete
        # foreground as its primary query for insert/append placements.
        selected_text=request.selected_text,
        selected_text_hash=request.selected_text_hash,
        frontend_revision=int(request.frontend_revision),
        selection_epoch=int(request.selection_epoch),
        panel_session_id=request.panel_session_id,
        front_app_bundle_id=request.front_app_bundle_id,
        surrounding_before=request.context or request.surrounding_before,
        surrounding_after=request.surrounding_after,
        intent=request.intent,
        placement=request.placement,
        project=request.project,
        app=request.app or request.front_app_bundle_id,
        max_candidates=request.max_candidates,
    )


def _evidence_from_pack(pack: tuple[dict[str, object], ...]) -> tuple[ActiveRagEvidence, ...]:
    result: list[ActiveRagEvidence] = []
    for index, item in enumerate(pack, start=1):
        hints = item.get("surfaceHints") if isinstance(item, dict) else None
        text = ""
        if isinstance(hints, list):
            text = compact_whitespace(str(hints[0] if hints else ""))
        if not text and isinstance(item, dict):
            text = compact_whitespace(str(item.get("text") or item.get("candidateText") or ""))
        if not text:
            continue
        tags = item.get("tags") if isinstance(item, dict) else ()
        source_type = compact_whitespace(str(item.get("sourceType") or "rag"))
        source_lane = compact_whitespace(str(item.get("sourceLane") or "evidence_pack"))
        evidence_id = compact_whitespace(
            str(item.get("evidenceId") or item.get("id") or f"pack:{index}")
        )
        atom_ids = _pack_string_ids(item.get("atomIds"))
        book_ids = _pack_string_ids(item.get("bookIds"))
        metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
        for key in (
            "title",
            "summary",
            "ref",
            "maySupportFacts",
            "maySupportIntent",
            "contextOnly",
        ):
            if key in item and key not in metadata:
                metadata[key] = item[key]
        metadata.update(
            {
                "source": "evidence_pack",
                "sourceType": source_type,
                "surfaceHints": [
                    str(value)
                    for value in hints
                    if compact_whitespace(str(value))
                ]
                if isinstance(hints, list)
                else [],
                "title": compact_whitespace(str(item.get("title") or metadata.get("title") or "")),
                "summary": compact_whitespace(
                    str(
                        item.get("summary")
                        or item.get("evidencePreview")
                        or metadata.get("summary")
                        or ""
                    )
                ),
            }
        )
        result.append(
            ActiveRagEvidence(
                evidence_id=evidence_id,
                text=text,
                source_type=source_type,
                source_lane=source_lane,
                tags=tuple(str(tag) for tag in tags) if isinstance(tags, list) else (),
                atom_ids=atom_ids,
                book_ids=book_ids,
                evidence_event_ids=tuple(
                    int(value)
                    for value in item.get("sourceEventIds", [])
                    if isinstance(value, int) and not isinstance(value, bool)
                )
                if isinstance(item.get("sourceEventIds"), list)
                else (),
                preview=compact_whitespace(
                    str(item.get("evidencePreview") or item.get("preview") or metadata.get("summary") or "")
                ),
                metadata=metadata,
            )
        )
    return tuple(result)


def _pack_string_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        text
        for item in value
        if (text := compact_whitespace(str(item or "")))
    )


def _evidence_payload(evidence: ActiveRagEvidence) -> dict[str, object]:
    metadata = dict(evidence.metadata or {})
    surface_hints = metadata.get("surfaceHints") if isinstance(metadata.get("surfaceHints"), list) else []
    return {
        "id": evidence.evidence_id,
        "text": evidence.text,
        "sourceType": compact_whitespace(str(metadata.get("sourceType") or evidence.source_type)),
        "sourceLane": evidence.source_lane,
        "title": compact_whitespace(str(metadata.get("title") or metadata.get("bookTitle") or "")),
        "evidencePreview": evidence.preview or compact_whitespace(str(metadata.get("summary") or "")),
        "surfaceHints": [str(item) for item in surface_hints if compact_whitespace(str(item))][:8],
        "tags": list(evidence.tags),
        "bookIds": list(evidence.book_ids),
        "sourceEventIds": list(evidence.evidence_event_ids),
    }


def _redacted_evidence_payload(evidence: ActiveRagEvidence) -> dict[str, object]:
    return {
        "evidenceId": evidence.evidence_id,
        "sourceType": evidence.source_type,
        "sourceLane": evidence.source_lane,
        "score": evidence.score,
        "confidence": evidence.confidence,
        "tags": list(evidence.tags[:6]),
        "hasPreview": bool(evidence.preview),
    }


def _merge_candidates(
    local_candidates: tuple[ActiveRagCandidate, ...],
    deepseek_candidates: tuple[ActiveRagCandidate, ...],
    *,
    max_candidates: int,
    prefer_deepseek: bool = False,
) -> tuple[ActiveRagCandidate, ...]:
    seen: set[str] = set()
    result: list[ActiveRagCandidate] = []
    ordered = (*deepseek_candidates, *local_candidates) if prefer_deepseek else (*local_candidates, *deepseek_candidates)
    for candidate in ordered:
        key = compact_whitespace(candidate.text).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
        if len(result) >= max(1, int(max_candidates)):
            break
    return tuple(result)
