from __future__ import annotations

import copy
import hashlib
import hmac
import inspect
import ipaddress
import json
import math
import mimetypes
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import (
    BoundedSemaphore,
    Event,
    RLock,
    Thread,
    Timer,
    current_thread,
    main_thread,
)
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, quote, unquote, urlparse

from .active_rag_service import (
    ACTIVE_RAG_DEFAULT_MAX_CHARS,
    SENSITIVE_FIELD_BLOCK_REASON,
    ActiveRagService,
    ActiveRagStartRequest,
    active_rag_sensitive_text_blocked,
)
from .activity_timeline import DailyActivityTimelineStore, activity_timeline_date_range
from .agent_extensions import AgentExtensionService
from .agent_lifecycle_hooks import AgentLifecycleHookService
from .agent_runtime_driver import AgentRuntimeError
from .agent_surface_runtime import AgentSurfaceRuntime, PiSurfaceCompletionProvider
from .agent_role_book_control import AgentRoleBookControlService
from .agent_service import AgentService, agent_service_from_settings
from .desktop_files import DesktopFiles
from .agent_routes import (
    agent_collaboration_profile_route,
    agent_approval_route,
    agent_background_job_route,
    agent_artifact_route,
    agent_context_item_route,
    agent_context_trace_route,
    observability_trace_route,
    observability_trace_repair_route,
    observability_trace_replay_route,
    observability_trace_diagnostic_report_route,
    agent_media_route,
    agent_room_route,
    agent_room_work_route,
    agent_session_route,
    agent_work_document_route,
    agent_subagent_route,
    agent_wake_schedule_route,
    observability_eval_schedule_route,
    observability_sandbox_run_route,
)
from .agent_tool_artifacts import AgentToolArtifactProjector
from .agent_tools import ControlToolGateway
from .agent_workspace import WorkspaceHarnessError, WorkspaceSnapshotError
from .adapter import InputMethodAdapter, SuggestionRequest
from .assistant_overlay import build_assistant_overlay_payload, build_candidate_panel_payload
from .browser_control import BrowserControlError, BrowserControlService
from .system_terminal import SystemTerminalService
from .demo_seed import seed_demo_memories
from .core_client import CoreClient, default_fixture_memories
from .contracts.context_observability import build_context_injection_trace
from .contracts.json_schema import validate_contract
from .trace_adapters import envelope_from_browser_trace, envelope_from_prediction_frame
from .trace_runtime import TraceContractError, TraceEnvelope
from .trace_repair import TraceRepairConflict, TraceRepairValidationError
from .trace_replay_verification import (
    TraceVerificationConflict,
    TraceVerificationValidationError,
)
from .vertical_sandbox_connector import VerticalSandboxConnectorService
from .extension_sandbox_experiment import ExtensionSandboxExperimentService
from .control_api import (
    AgentKernelControlFacade,
    ControlAccessContext,
    capability_feature_flags,
    ControlApiError,
    ControlErrorCode,
    default_route_policy,
)
from .control_api.dispatch import DescriptorRouteDispatcher
from .control_api.gateway_access import GatewayAccessDecision, resolve_gateway_access
from .control_api.lab_errors import (
    lab_trial_error_response,
    lab_golden_error_response,
    lab_project_error_response,
    lab_scene_recipe_error_response,
)
from .control_api.route_table import build_arguments, find_route
from .models import InputEvent, InputSuggestion
from .model_profiles import canonical_runtime_profile_id, profile_by_id
from .model_registry import ModelDeployment, ModelRegistry, default_model_registry_path
from .predictor_configuration import (
    PREDICTOR_SETTING_KEYS,
    PredictorConfiguration,
    active_predictor_configuration,
    configuration_matches,
    resolve_predictor_configuration,
)
from .deepseek_completion import DeepSeekCompletionRequest, DeepSeekV4FlashCompletionProvider, build_deepseek_completion_messages
from .deepseek_config import load_deepseek_config
from .deepseek_memory_organizer import ManagedPiMemoryOrganizer
from .memory_catalog_scheduler import (
    CATALOG_CONSOLIDATION_SCHEMA_VERSION,
    MemoryCatalogConsolidationScheduler,
)
from .memory_maintenance_settings import MemoryMaintenanceSettings
from .memory_model_executor import (
    MINIMUM_MEMORY_CONTEXT_TOKENS,
    build_governed_memory_model_executor,
    memory_curation_model_status,
    reconcile_stale_memory_runtime_sessions,
)
from .deployment_status import audit_installed_product
from .embeddings import embed_query, embedding_provider_from_env
from .foreground_app_semantics import enrich_window_context_with_app_semantics
from .foreground_privacy import assess_foreground_write, storage_receipt
from .frontend_gateway import FrontendGateway
from .history_context import build_prediction_context
from .hybrid_rag_models import HybridRagQuery
from .hybrid_rag_retriever import retrieve_hybrid_rag_candidates
from .input_capture_contract import (
    capture_contract_from_metadata,
    sanitize_input_capture_metadata,
)
from .local_sqlite_core import LocalSqliteCoreClient
from .knowledge_workbench import (
    DeepSeekKnowledgeProvider,
    KnowledgeWorkbenchRequest,
    KnowledgeWorkbenchService,
)
from .knowledge_control import KnowledgeControlFacade
from .knowledge_library import AssetBlob
from .knowledge_worker_supervisor import KnowledgeWorkerSupervisor
from .lexicon_organization import run_due_lexicon_organization
from .management_service import ManagementService, page_request
from .management_work_contract import (
    ManagementWorkError,
    StoredReceipt,
    WorkExecution,
)
from .window_context import validate_window_context
from .memory_book_compiler import (
    apply_stored_memory_book_run,
    build_memory_book_source_bundle,
    find_newer_applied_memory_book_run,
    find_memory_book_draft_for_bundle,
    memory_compile_due,
    inspect_memory_book_plan,
    memory_book_plan_from_compile_output,
    memory_book_plan_from_stored_run,
    memory_book_run_is_stale,
    memory_book_run_payload,
    rollback_memory_book_run,
    seal_global_memory_book_plan,
    store_memory_book_plan,
    update_stored_memory_book_diff,
)
from .memory_curation import (
    MEMORY_CURATION_ARCHITECTURE,
    curation_decisions_to_compile_output,
)
from .memory_generator import (
    MemoryGenerationError,
    VcpRebuildMemoryGenerator,
    generated_memory_context,
    generated_memory_dedupe_tag,
)
from .memory_projection import MemoryProjectionWorker
from .models import MemoryAction
from .notion_knowledge import NotionAsyncKnowledgeClient, load_notion_knowledge_config
from .memory_ownership import normalize_memory_owner, resolve_visible_memory_owners
from .owner_memory_curation import (
    DEFAULT_MAX_SOURCES,
    MAX_PERSONAL_V2_INPUT_TOKENS,
    MAX_PERSONAL_V2_SOURCES,
    OwnerMemoryCurator,
    owner_memory_curation_status,
)
from .owner_memory_maintenance import GatewayMemoryMaintenanceJobs
from .memory_lifecycle.common import connect as lifecycle_connect, require_schema as lifecycle_require_schema
from .memory_lifecycle.daily import generate as lifecycle_report
from .memory_lifecycle.forget import forget_source as lifecycle_forget_source, preview_source_forget as lifecycle_forget_preview
from .memory_lifecycle.refresh import (
    RefreshJobs as LifecycleRefreshJobs,
    RefreshWorker as LifecycleRefreshWorker,
)
from .payloads import action_response_payload, suggestions_response_payload
from .personal_memory_books import personal_memory_book_projection_status
from .personal_context_maintenance import (
    PersonalContextMaintenanceConfig,
    PersonalContextMaintenanceRunner,
)
from rag_ime.pi.provider_auth import PiProviderAuthError, PiProviderAuthService
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.values import PiRuntimeCommandRejected
from .personal_context_observability import PersonalContextObservability
from .prediction_anchors import build_prediction_anchors_from_snapshot
from .predictor import (
    PredictionBenchmarkCase,
    PredictionProvider,
    benchmark_streaming_ttft_provider,
    prediction_provider_from_env,
    prediction_provider_status,
)
from .predictor_benchmark import benchmark_predictor_latency, load_predictor_latency_cases
from .predictor_latency import latency_log_path_from_env, latency_report
from .rime_sidecar import (
    build_rime_sidecar_response,
    choose_semantic_query,
    configure_auto_prediction_trigger,
    decide_side_candidate_refresh,
    frontend_transaction_to_payload,
    parse_rime_context_payload,
    prediction_first_merge_enabled,
    record_rime_side_candidate_selection,
    rime_context_to_payload,
    semantic_signal_length,
    sensitive_input_requested,
)
from .rag_core_v3 import memory_candidates_v2_to_input_suggestions
from .retrieval_docs import rebuild_retrieval_docs
from .rime_native_feedback import record_native_rime_selection
from .rime_rank_export import record_rime_rank_feedback
from .lexicon_organization import lexicon_organization_status
from .rime_lexicon_review import (
    apply_reviewed_rime_lexicon,
    review_rime_lexicon,
    rollback_reviewed_rime_lexicon,
)
from .runtime_config import RuntimeConfigResolver, RuntimeConfigSnapshot
from .runtime_flags import load_hybrid_rag_runtime_flags
from .settings_models import SettingsUpdateResult, UserProfile, UserVocabularyItem
from .settings_schema import (
    SENSITIVE_SETTING_SUFFIXES,
    deep_merge_settings,
    flatten_settings,
    stable_settings_hash,
)
from .settings_store import ManagementSettingsStore, ensure_management_tables, settings_response
from .temporal_query import TemporalQuery, parse_temporal_query
from .text_utils import compact_whitespace, now_ms, stable_text_hash, truncate_text
from .voice_control import (
    VoiceHotwordConfigStore,
    read_voice_preferences,
    read_voice_control_status,
    resolve_voice_support_directory,
    voice_hotword_config_from_settings,
    write_voice_preferences_from_settings,
)


_MAX_MANUAL_CURATION_PREPARE_BATCHES = 8
# A foreground "整理" action is still bounded.  Keeping this separate from
# the model's per-batch source limit prevents one click from holding the
# Gateway indefinitely while it drains an old backlog.
_MAX_MANUAL_CURATION_TRIGGER_BATCHES = 8
# Atom-first owner curation admits at most six logical inputs/Atoms per model
# request. Keep the manual HTTP trigger aligned with that downstream gate.
_MAX_MANUAL_CURATION_SOURCE_BATCH = 6
# A timed-out frozen packet is retried with a smaller packet so one pathological
# source group cannot block the whole backlog forever.
_MAX_MANUAL_CURATION_TIMEOUT_RETRY_BATCH = 2
_BROWSER_TRACE_RESOLUTION_LIMIT = 200

GLOBAL_MEMORY_CATALOG_CONSOLIDATION_INSTRUCTION = (
    "Inspect the complete governed P/B/G/T/Tag-edge Memory catalog. "
    "Duplicate Books and over-split relations are candidate signals only: "
    "propose exact-equivalence Atom merges and Tag merges only when two "
    "physical Tags have the same normalized name or an explicit direct alias. "
    "For existing topic Books, a Book merge is allowed only when the complete "
    "owner/project/scope/binding identity matches and the catalog directly "
    "supports one long-lived topic; similarity alone is never authorization. "
    "Preserve every evidence reference. Do not create, update, retract, or "
    "rewrite facts; do not rewrite Book fields, Groups, Tag edges, or "
    "memberships outside a governed Book merge. The dedicated "
    "memory-catalog-consolidation curator and its independent verifier must "
    "reject every other operation."
)
_MAX_EMBEDDING_WARMUP_DELAY_SECONDS = 300.0


def _host_is_loopback(host: str) -> bool:
    normalized = (host or "").strip().lower().removeprefix("[").removesuffix("]")
    if normalized in {"localhost", "localhost.", "::1"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin_matches_host(origin: str, host_header: str) -> bool:
    parsed = urlparse(origin)
    origin_host = parsed.netloc.lower()
    request_host = (host_header or "").lower()
    return bool(origin_host and request_host and origin_host == request_host)


def _memory_book_operation_label(operation: str) -> str:
    return {
        "upsert_semantic_group": "更新主题分组",
        "upsert_semantic_tag": "更新标签",
        "upsert_memory_book": "更新工具书",
        "upsert_memory_atom": "更新记忆条目",
        "upsert_tag_edge": "更新标签关系",
        "merge_semantic_tag": "合并标签",
        "merge_memory_books": "合并主题书",
        "add_phrase_candidate": "新增词表提案",
        "add_negative_phrase": "新增负向记忆",
        "supersede_memory": "替代旧记忆",
    }.get(operation, "整理记忆")


def _knowledge_database_contract_state(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    project: str,
) -> dict[str, object]:
    run = memory_book_run_payload(conn, run_id=run_id)
    if not run.get("provider"):
        raise ManagementWorkError("domain_not_found", "The knowledge database run was not found.")
    metadata = dict(run.get("metadata") or {})
    if _string(metadata.get("project")) != project:
        raise ManagementWorkError(
            "domain_scope_mismatch",
            "The knowledge database run is outside the current project.",
        )
    diffs = [dict(item) for item in list(run.get("diffs") or []) if isinstance(item, dict)]
    pending_count = sum(1 for item in diffs if _string(item.get("status")) in {"pending", "approved"})
    applied_count = sum(1 for item in diffs if _string(item.get("status")) == "applied")
    stale = memory_book_run_is_stale(conn, run=run)
    newer_applied_run = find_newer_applied_memory_book_run(conn, run_id=run_id)
    status = _string(run.get("status"))
    can_apply = status == "draft" and not stale and bool(diffs)
    can_rollback = status in {"applied", "partial"} and applied_count > 0 and newer_applied_run is None
    if stale:
        apply_blocked_reason = "The knowledge database run is stale."
    elif status != "draft":
        apply_blocked_reason = "The knowledge database run is not a draft."
    elif not diffs:
        apply_blocked_reason = "The knowledge database run contains no review decisions."
    else:
        apply_blocked_reason = ""
    rollback_blocked_reason = (
        "A newer knowledge database apply must be rolled back first."
        if newer_applied_run is not None
        else "The knowledge database run is not rollbackable."
    )
    revision_hash = "sha256:" + hashlib.sha256(
        json.dumps(run, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "run": run,
        "revisionHash": revision_hash,
        "summary": _string(run.get("summary")),
        "pendingCount": pending_count,
        "appliedCount": applied_count,
        "canApply": can_apply,
        "canRollback": can_rollback,
        "applyBlockedReason": apply_blocked_reason,
        "rollbackBlockedReason": rollback_blocked_reason,
    }


def _require_management_fields(
    payload: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str],
) -> None:
    keys = {str(key) for key in payload}
    missing = sorted(required - keys)
    if missing:
        raise ManagementWorkError("invalid_request", f"Missing required fields: {', '.join(missing)}.")
    unknown = sorted(keys - required - optional)
    if unknown:
        raise ManagementWorkError("invalid_request", f"Unsupported fields: {', '.join(unknown)}.")


def _strict_management_revision(value: object) -> int:
    if isinstance(value, bool):
        raise ManagementWorkError("invalid_request", "expectedRuntimeRevision must be an integer.")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ManagementWorkError(
            "invalid_request",
            "expectedRuntimeRevision must be an integer.",
        ) from exc
    if parsed < 0:
        raise ManagementWorkError(
            "invalid_request",
            "expectedRuntimeRevision must be non-negative.",
        )
    return parsed


class AgentGatewayRequired(RuntimeError):
    """A passive Sidecar cannot start or steer a Pi-owned Room turn."""

    http_status = HTTPStatus.CONFLICT

    def __init__(self) -> None:
        super().__init__(
            "Room runtime writes must be sent to the Agent Gateway"
        )

    def response_payload(self) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.agent-gateway-required.v1",
            "code": "AGENT_GATEWAY_REQUIRED",
        }


@dataclass(frozen=True)
class DebugServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    db_path: Path = Path(".rag-ime-data/rag-ime.sqlite")
    project: str = "wisdom-weasel-rag-ime"
    static_dir: Path = Path("debug")
    seed_if_empty: bool = True
    core: CoreClient | None = None
    predictor: PredictionProvider | None = None
    server_name: str = "debug server"
    rime_cache_ttl_ms: int = 400
    input_source_id: str = "im.rime.inputmethod.Squirrel.Hans"
    input_source_check_script: Path | None = None
    input_source_require_hitoolbox: bool = True
    vector_auto_rebuild_limit: int = 0
    include_raw_text: bool = False
    runtime_command_runner: Any | None = None
    rime_user_dir: Path = Path.home() / "Library" / "Rime"
    rime_lexicon_backup_root: Path = Path.home() / "Library" / "Application Support" / "RagIme" / "LexiconBackups"
    active_rag_trace_path: Path | None = None
    agent_service: AgentService | None = None
    pi_provider_auth_service: PiProviderAuthService | None = None
    knowledge_client: object | None = None
    knowledge_control: object | None = None
    memory_projection_worker_enabled: bool | None = None
    memory_projection_poll_interval_s: float | None = None
    # Explicit workspace dependencies for server-owned spaces. Local desktop
    # deployments retain their existing environment-derived defaults.
    workspace_root: Path | None = None
    project_skill_roots: tuple[Path, ...] | None = None
    plugin_inbox_root: Path | None = None
    integration_environment: Mapping[str, str] | None = None
    tool_gateway_factory: Callable[..., ControlToolGateway] | None = None


@dataclass
class _RimeSuggestCacheEntry:
    expires_at: float
    response: dict[str, object]


@dataclass
class _RimeSuggestInflightEntry:
    event: Event
    response: dict[str, object] | None = None
    error: BaseException | None = None
    waiters: int = 0


@dataclass
class _PredictorStatusCacheEntry:
    fingerprint: str
    expires_at: float
    status: dict[str, object]


class DebugImeService:
    """Local diagnostic and management API used by the native Control Center."""

    def __init__(self, config: DebugServerConfig):
        self.config = config
        self._lifecycle_lock = RLock()
        self._closed = False
        self._memory_projection_start_error = ""
        self._background_startup_thread: Thread | None = None
        self._background_startup_timer: Timer | None = None
        self._embedding_warmup_delay_s = _embedding_warmup_delay_seconds()
        self.settings_store = ManagementSettingsStore(
            config.db_path,
            persistent_reads=True,
        )
        self.settings_store.initialize()
        self.voice_support_directory = resolve_voice_support_directory(config.db_path)
        self.voice_hotwords = VoiceHotwordConfigStore(self.voice_support_directory)
        self.core = config.core or LocalSqliteCoreClient(
            config.db_path,
            embedding_provider=embedding_provider_from_env(),
        )
        self.predictor = config.predictor or prediction_provider_from_env()
        self.adapter = InputMethodAdapter(self.core, project=config.project)
        self.deepseek_completion_provider = DeepSeekV4FlashCompletionProvider(
            load_deepseek_config(env=config.integration_environment),
            enforce_runtime_flags=True,
        )
        self.active_rag = ActiveRagService(
            core=self.core if isinstance(self.core, LocalSqliteCoreClient) else None,
            completion_provider=self.deepseek_completion_provider,
            trace_path=config.active_rag_trace_path,
            trace_include_text=self._include_active_rag_trace_text,
        )
        self.knowledge_workbench = KnowledgeWorkbenchService(
            evidence_retriever=self._knowledge_workbench_evidence,
            generator=DeepSeekKnowledgeProvider(load_deepseek_config(env=config.integration_environment)),
            database_organizer=self._knowledge_workbench_database_organizer,
            notion_client=NotionAsyncKnowledgeClient(load_notion_knowledge_config(env=config.integration_environment)),
        )
        self.knowledge_worker = None
        if config.knowledge_client is None:
            self.knowledge_worker = KnowledgeWorkerSupervisor(
                settings_provider=lambda: self.settings_store.get_settings(include_sensitive=True),
                intake_db_path=config.db_path,
            )
            self.knowledge_client = self.knowledge_worker
        else:
            self.knowledge_client = config.knowledge_client
        gateway_owns_agent_runtime = (
            config.server_name == "sidecar server"
            and _bool(os.environ.get("RAG_IME_AGENT_GATEWAY_ENABLED"), default=False)
        )
        self._agent_runtime_execution_owner = not gateway_owns_agent_runtime
        self._agent_managed_by_settings = (
            config.agent_service is None
            and self._agent_runtime_execution_owner
        )
        self.agent = config.agent_service or agent_service_from_settings(
            config.db_path,
            self.settings_store.get_settings(include_sensitive=True),
            project=config.project,
            memory_embedding_provider=getattr(self.core, "embedding_provider", None),
            # Only the dedicated 8768 Agent Gateway owns the durable scheduler
            # and managed Pi Host.  The process-local
            # execution fence is deliberately separate from shared SQLite
            # settings, otherwise applying `agent.pi.enabled=true` wakes a
            # second Host in the 8766 Sidecar and faults the Gateway.
            wake_scheduler_enabled=config.server_name == "agent gateway",
            runtime_execution_owner=self._agent_runtime_execution_owner,
            defer_startup_recovery=True,
        )
        self.agent.configure_lab_knowledge(
            client=self.knowledge_client,
            settings_provider=lambda: self.settings_store.get_settings(include_sensitive=True),
        )
        self._memory_runtime_restart_recovery = (
            reconcile_stale_memory_runtime_sessions(
                self.agent.sessions,
                db_path=config.db_path,
            )
            if config.server_name == "agent gateway"
            and self._agent_runtime_execution_owner
            else {
                "schemaVersion": "rag-ime.memory-runtime-restart-recovery.v1",
                "recoveredSessionCount": 0,
                "resumableRequestCount": 0,
                "resumableRunCount": 0,
                "recoveredAtMs": 0,
            }
        )
        self.personal_context_observability = PersonalContextObservability(
            config.db_path,
            project=config.project,
        )
        self.personal_context_observability.initialize()
        self.activity_timelines = DailyActivityTimelineStore(
            config.db_path,
            project=config.project,
            observability=self.personal_context_observability,
        )
        self.agent_surface = AgentSurfaceRuntime(
            self.agent,
            settings_provider=lambda: self.settings_store.get_settings(include_sensitive=True),
            observation_callback=self.agent.observations.enqueue_input_generation_record,
        )
        if config.server_name == "agent gateway":
            self.active_rag.completion_provider = PiSurfaceCompletionProvider(
                local_runtime=self.agent_surface,
            )
        elif config.server_name == "sidecar server":
            self.active_rag.completion_provider = PiSurfaceCompletionProvider(
                gateway_url=os.environ.get(
                    "RAG_IME_AGENT_GATEWAY_URL",
                    "http://127.0.0.1:8768",
                ),
            )
        self.active_rag.bind_observation_observer(
            self.agent.observations.enqueue_active_rag_record
        )
        runtime_factory_config = getattr(self.agent.runtime_factory, "config", None)
        self.pi_provider_auth = config.pi_provider_auth_service or PiProviderAuthService.from_runtime(
            runtime_factory_config
            if isinstance(runtime_factory_config, PiRuntimeConfig)
            else PiRuntimeConfig.from_environment(),
            repo_root=Path(__file__).resolve().parents[1],
        )
        self._runtime_command_runner = config.runtime_command_runner or subprocess.run
        self._rime_cache: dict[str, _RimeSuggestCacheEntry] = {}
        self._rime_inflight: dict[str, _RimeSuggestInflightEntry] = {}
        self._rime_cache_lock = RLock()
        self._rime_cache_hits = 0
        self._rime_cache_misses = 0
        self._rime_inflight_hits = 0
        self._rime_inflight_errors = 0
        self._prediction_live_trace: list[dict[str, object]] = []
        self._predictor_status_cache: dict[bool, _PredictorStatusCacheEntry] = {}
        self._predictor_status_lock = RLock()
        if isinstance(self.core, LocalSqliteCoreClient):
            self.core.enable_persistent_reads()
            self.core.initialize(perform_maintenance=False)
        self._embedding_warmup_report = self._initial_embedding_warmup_report()
        self._startup_recovery_report = self._initial_startup_recovery_report()
        self.runtime_config_resolver = RuntimeConfigResolver(
            self.settings_store,
            environ=os.environ if config.integration_environment is None else config.integration_environment,
        )
        self.management = ManagementService(
            db_path=config.db_path,
            project=config.project,
            repo_root=Path(__file__).resolve().parents[1],
            settings_store=self.settings_store,
            health_provider=self.health,
            input_source_provider=self.input_source_status,
            predictor_provider=self.predictor_status,
            runtime_config_provider=self.runtime_config_snapshot,
            last_prediction_provider=self._last_management_prediction,
            deployment_provider=lambda: audit_installed_product(
                repo_root=Path(os.environ.get("RAG_IME_SOURCE_ROOT") or Path(__file__).resolve().parents[1]),
                app_support=Path(
                    os.environ.get("RAG_IME_APP_SUPPORT_DIR")
                    or Path.home() / "Library" / "Application Support" / "RagIme"
                ),
                verify_pi_files=False,
            ),
            cache_invalidator=self._clear_rime_cache,
            voice_support_directory=self.voice_support_directory,
        )
        self.agent_role_book_control = AgentRoleBookControlService(
            config.db_path,
            project=config.project,
            work_contract=self.management.work_contract,
            role_books=self.agent.role_books,
            observability=self.personal_context_observability,
        )
        self.knowledge_control = config.knowledge_control
        if self.knowledge_control is None and isinstance(self.knowledge_worker, KnowledgeWorkerSupervisor):
            self.knowledge_control = KnowledgeControlFacade(
                worker=self.knowledge_worker,
                work_contract=self.management.work_contract,
            )
        plugin_inbox = (
            os.environ.get("RAG_IME_AGENT_PLUGIN_INBOX_DIR", "").strip()
            or os.environ.get("RAG_IME_PI_PLUGIN_INBOX", "").strip()
        )
        project_skill_paths = os.environ.get("RAG_IME_PROJECT_SKILL_PATHS", "")
        project_skill_roots = tuple(
            Path(value).expanduser()
            for value in project_skill_paths.split(os.pathsep)
            if value.strip()
        )
        if config.project_skill_roots is not None:
            project_skill_roots = config.project_skill_roots
        elif not project_skill_roots:
            project_workspace = Path(
                config.workspace_root
                or os.environ.get("RAG_IME_DEFAULT_WORKSPACE")
                or os.environ.get("RAG_IME_SOURCE_ROOT")
                or Path.cwd()
            ).expanduser()
            project_skill_roots = (
                project_workspace / ".agents" / "skills",
                project_workspace / ".pi" / "skills",
                project_workspace / "skills",
            )
        self.agent_extensions = AgentExtensionService(
            runtime_provider=lambda: self.agent.runtime,
            inbox_root=(
                config.plugin_inbox_root
                if config.plugin_inbox_root is not None
                else Path(plugin_inbox).expanduser()
                if plugin_inbox
                else Path(config.db_path).expanduser().resolve(strict=False).parent
                / "Agent"
                / "plugin-inbox"
            ),
            project_skills_roots=project_skill_roots,
        )
        self.agent.bind_extension_app_skill_owners(
            self.agent_extensions.extension_app_skill_owners
        )
        self.agent_lifecycle_hooks = AgentLifecycleHookService(config.db_path)
        self.agent_lifecycle_hooks.initialize()
        self.vertical_sandbox_connector = VerticalSandboxConnectorService(
            eval_store=self.agent.eval_runs,
            trace_store=self.agent.trace_store,
            sandbox_store=self.agent.sandbox_runs,
            workspace_harness=self.agent.background_jobs.workspace_harness,
            repository_root=(
                config.workspace_root
                if config.workspace_root is not None
                else Path(os.environ["RAG_IME_ROOT"]).expanduser()
                if os.environ.get("RAG_IME_ROOT")
                else Path(__file__).resolve().parents[1]
            ),
        )
        self.extension_sandbox_experiments = ExtensionSandboxExperimentService(
            sessions=self.agent.sessions,
            extensions=self.agent_extensions,
            connector=self.vertical_sandbox_connector,
        )
        browser_control_kwargs: dict[str, object] = {}
        if "trace_observer" in inspect.signature(BrowserControlService).parameters:
            browser_control_kwargs["trace_observer"] = (
                self.agent.observations.enqueue_browser_record
            )
        self.browser_control = BrowserControlService(
            config.db_path,
            **browser_control_kwargs,
        )
        self.agent.bind_external_trace_resolver(self._resolve_external_common_trace)
        self.system_terminal = SystemTerminalService(
            default_cwd=(
                str(config.workspace_root) if config.workspace_root is not None else
                os.environ.get("RAG_IME_DEFAULT_WORKSPACE")
                or os.environ.get("RAG_IME_SOURCE_ROOT")
                or Path.cwd()
            ),
        )
        self.desktop_files = DesktopFiles(editability=lambda session_id, path:
            self.agent_tools.workspace_harness.file_editability(self.agent.sessions.get(session_id), path))
        self.agent_tools = (config.tool_gateway_factory or ControlToolGateway)(
            sessions=self.agent.sessions,
            management=self.management,
            core=self.core,
            project=config.project,
            facade=self,
            knowledge_client=self.knowledge_client,
            knowledge_control=self.knowledge_control,
            workspace_harness=self.agent.background_jobs.workspace_harness,
            background_jobs=self.agent.background_jobs,
            delegation=self.agent.delegation,
            collaboration=self.agent,
            extensions=self.agent_extensions,
            scheduling=self.agent,
            configuration_store=self.agent.configuration_store,
            governed_skills=None,
            browser_control=self.browser_control,
            artifact_projector=AgentToolArtifactProjector(self.agent.media),
            work_documents=self.agent.work_documents,
            lab_projects=self.agent,
            sandbox_connector=self.vertical_sandbox_connector,
            trace_diagnostics=self.agent,
            workflow_publisher=lambda session_id, reason: self.agent.publish_workflow_state(
                session_id,
                reason=reason,
            ),
        )
        self.agent.bind_tool_manifest_provider(self.agent_tools.runtime_manifests)
        self.agent.bind_trace_optimization_services(
            self.agent_extensions, self.agent_tools.trace_optimization_capability_catalog,
        )
        self.control_api = AgentKernelControlFacade(
            agent=self.agent,
            capabilities=self.agent_tools,
            platform_capabilities=lambda: {
                "transport": "http",
                "nativeBridge": False,
                "filePicker": False,
                "revealPath": False,
                "approvedExternalActions": False,
            },
        )
        self.agent.bind_approval_executor(self.agent_tools.apply_approval)
        self.agent_tools.bind_auto_approval_executor(self.agent.auto_approve_pending)
        self.agent.bind_memory_maintenance_probe(self.agent_memory_maintenance_status)
        self.agent.bind_tool_manifest_provider(self.agent_tools.runtime_manifests)
        self.memory_catalog_scheduler = MemoryCatalogConsolidationScheduler(
            config.db_path
        )
        self.memory_maintenance_jobs = GatewayMemoryMaintenanceJobs(
            self._execute_gateway_memory_maintenance,
            db_path=config.db_path,
            event_publisher=self._publish_memory_maintenance_event,
            execution_owner=self._agent_runtime_execution_owner,
        )
        self.memory_lifecycle_jobs = LifecycleRefreshJobs(config.db_path)
        self.memory_lifecycle_worker = (
            LifecycleRefreshWorker(self.memory_lifecycle_jobs)
            if self._agent_runtime_execution_owner
            else None
        )
        self.frontend_gateway = FrontendGateway(
            suggest_handler=self.rime_suggest,
            selection_handler=self.rime_select,
            default_project=config.project,
        )
        initial_settings = self.settings_store.get_settings(include_sensitive=True)
        initial_snapshot = self.runtime_config_snapshot(settings=initial_settings)
        if config.integration_environment is None:
            _apply_pinyin_settings_to_process_env(initial_snapshot.effective_settings(initial_settings))
        if config.seed_if_empty and self._event_count() == 0:
            seed_demo_memories(self.adapter, default_fixture_memories())
        self._vector_auto_rebuild_report = self._maybe_auto_rebuild_vector_index()
        self.memory_projection_worker = self._create_memory_projection_worker()

    def require_agent_runtime_execution_owner(self) -> None:
        if not self._agent_runtime_execution_owner:
            raise AgentGatewayRequired()

    def start_background_services(self) -> None:
        """Start non-critical workers after the HTTP listener owns the process."""

        self._start_background_startup_lane()
        self.memory_maintenance_jobs.resume_persisted_jobs()
        worker = self.memory_lifecycle_worker
        if worker is not None:
            worker.start()

    def close(self) -> None:
        """Stop process-owned workers and release service resources once."""

        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            background_startup_timer = self._background_startup_timer
            self._background_startup_timer = None
            worker = self.memory_projection_worker
            lifecycle_worker = self.memory_lifecycle_worker
        if background_startup_timer is not None:
            background_startup_timer.cancel()
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                # Shutdown continues so one background failure cannot leak the
                # remaining executors and provider clients.
                pass
        if lifecycle_worker is not None:
            try:
                lifecycle_worker.stop()
            except Exception:
                pass
        resources = (
            self.core,
            self.settings_store,
            self.system_terminal,
            self.memory_maintenance_jobs,
            self.active_rag,
            self.knowledge_worker,
            self.management,
            self.pi_provider_auth,
            self.agent,
        )
        for resource in resources:
            closer = getattr(resource, "close", None)
            if not callable(closer):
                continue
            try:
                closer()
            except Exception:
                # Best-effort process teardown must continue through all owned
                # resources even if one background component is already bad.
                pass

    def health(self) -> dict[str, object]:
        settings = self.settings_store.get_settings()
        runtime_config = self.runtime_config_snapshot()
        effective_settings = runtime_config.effective_settings(settings)
        return {
            "ok": True,
            "project": self.config.project,
            "coreMode": "local" if isinstance(self.core, LocalSqliteCoreClient) else "json",
            "dbPath": str(self.config.db_path),
            "management": {
                "schemaVersion": "rag-ime.debug-management.v1",
                "localhostOnly": _host_is_loopback(self.config.host),
                "rawTextVisible": self._include_raw_text(),
                "settings": effective_settings,
                "runtimeConfig": runtime_config.payload(),
            },
            "pinyinRuntime": _pinyin_runtime_status(effective_settings),
            "eventCount": self._event_count(),
            "actionCount": self._action_count(),
            "rimeSuggestCache": {
                "ttlMs": self._cache_ttl_ms(),
                "size": self._rime_cache_size(),
                "hits": self._rime_cache_hits,
                "misses": self._rime_cache_misses,
                "inFlight": self._rime_inflight_size(),
                "inFlightHits": self._rime_inflight_hits,
                "inFlightErrors": self._rime_inflight_errors,
            },
            "predictor": self._predictor_status(probe_capabilities=False),
            "suggestionCache": self._suggestion_cache_stats(),
            "vectorStats": self._vector_index_stats(),
            "embeddingWarmup": self.embedding_warmup_status(),
            "startupRecovery": self.startup_recovery_status(),
            "vectorAutoRebuild": self._vector_auto_rebuild_status(),
            "memoryProjection": self.memory_projection_status(),
        }

    def memory_projection_status(self) -> dict[str, object]:
        worker = self.memory_projection_worker
        if worker is None:
            return {
                "schemaVersion": "rag-ime.memory-projection-runtime.v1",
                "ok": True,
                "configured": False,
                "owner": self.config.server_name,
                "running": False,
                "lastRunAtMs": 0,
                "lastError": "",
                "freshness": {},
                "disabledReason": (
                    "local_sqlite_core_required"
                    if not isinstance(self.core, LocalSqliteCoreClient)
                    else "not_projection_owner"
                ),
            }
        try:
            runtime = worker.status()
        except Exception as exc:  # pragma: no cover - defensive adapter guard
            runtime = {
                "running": False,
                "lastRunAtMs": 0,
                "lastError": _safe_debug_error(exc),
                "lastReport": {},
                "projectionKinds": [],
            }
        try:
            freshness = self.core.memory_projection_freshness()
        except Exception as exc:
            freshness = {
                "available": False,
                "fresh": False,
                "error": _safe_debug_error(exc),
            }
        last_error = (
            self._memory_projection_start_error
            or compact_whitespace(str(runtime.get("lastError") or ""))
            or compact_whitespace(str(freshness.get("error") or ""))
        )
        running = bool(runtime.get("running"))
        return {
            "schemaVersion": "rag-ime.memory-projection-runtime.v1",
            # Runtime degradation is reported here while /api/health remains
            # available to the foreground IME and Control Center.
            "ok": running and not last_error and bool(freshness.get("fresh")),
            "configured": True,
            "owner": self.config.server_name,
            "running": running,
            "projectionKinds": list(runtime.get("projectionKinds") or []),
            "lastRunAtMs": int(runtime.get("lastRunAtMs") or 0),
            "lastError": last_error,
            "lastReport": dict(runtime.get("lastReport") or {}),
            "freshness": freshness,
            "disabledReason": "",
        }

    def activity_timeline_review(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        timeline_id = _string(payload.get("timelineId"))
        if timeline_id:
            timeline = self.activity_timelines.review(timeline_id)
        else:
            timeline_date = _string(payload.get("date"))
            if not timeline_date:
                raise ValueError("timelineId or date is required")
            timeline = self.activity_timelines.latest(
                timeline_date,
                status=_string(payload.get("status")),
            )
        return {
            "schemaVersion": "rag-ime.daily-activity-timeline-review.v1",
            "ok": True,
            "project": self.config.project,
            "timeline": timeline or {},
        }

    def activity_timeline_calendar(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        calendar = dict(
            self.activity_timelines.calendar(_string(payload.get("month")))
        )
        core = getattr(self, "core", None)
        jobs = getattr(self, "memory_maintenance_jobs", None)
        if not isinstance(core, LocalSqliteCoreClient) or not isinstance(
            jobs,
            GatewayMemoryMaintenanceJobs,
        ):
            return calendar
        summary = (
            calendar.get("summary")
            if isinstance(calendar.get("summary"), Mapping)
            else {}
        )
        waiting_day_count = int(summary.get("waitingDayCount") or 0)
        try:
            managed = MemoryMaintenanceSettings.load(
                core.db_path,
                preverified_schema=True,
            )
            job = jobs.activity_timeline_status(project=self.config.project)
            job_state = _string(job.get("state"))
            job_mode = _string(job.get("mode"))
            job_result = (
                job.get("result")
                if isinstance(job.get("result"), Mapping)
                else {}
            )
            if not managed.automatic_organization_enabled:
                state = "disabled"
            elif (
                job_mode == "automatic_catch_up"
                and job_state in {"queued", "running"}
            ):
                state = "running"
            elif (
                job_mode in {"automatic_catch_up", "manual_catch_up", "single_day"}
                and job_result.get("ok") is False
            ):
                state = "retry_scheduled"
            elif waiting_day_count:
                state = "scheduled"
            else:
                state = "caught_up"
            calendar["automation"] = {
                "schemaVersion": "rag-ime.activity-timeline-automation.v1",
                "enabled": managed.automatic_organization_enabled,
                "state": state,
                "batchDayLimit": 1,
                "schedulerPollIntervalMs": 60 * 60 * 1_000,
                "configuredIntervalMs": (
                    managed.automatic_organization_interval_seconds * 1_000
                ),
                "completedDayCount": int(
                    summary.get("organizedDayCount") or 0
                ),
                "totalDayCount": int(summary.get("activityDayCount") or 0),
                "remainingDayCount": waiting_day_count,
                "job": job,
            }
        except Exception:
            # The calendar remains a useful read projection when scheduler
            # settings or its process-local job registry are unavailable.
            calendar["automation"] = {
                "schemaVersion": "rag-ime.activity-timeline-automation.v1",
                "enabled": False,
                "state": "unavailable",
                "batchDayLimit": 1,
                "schedulerPollIntervalMs": 60 * 60 * 1_000,
                "configuredIntervalMs": 0,
                "completedDayCount": int(
                    summary.get("organizedDayCount") or 0
                ),
                "totalDayCount": int(summary.get("activityDayCount") or 0),
                "remainingDayCount": waiting_day_count,
                "job": {},
            }
        return calendar

    def activity_timeline_build(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if (
            self.config.server_name != "agent gateway"
            or not self._agent_runtime_execution_owner
        ):
            raise ValueError(
                "Activity organization must be triggered on the Agent Gateway"
            )
        timeline_date = _string(payload.get("date"))
        if not timeline_date:
            raise ValueError("date is required")
        through_today = _bool(payload.get("throughToday"), default=False)
        range_start_date = _string(payload.get("rangeStartDate"))
        if range_start_date:
            if not through_today:
                raise ValueError("rangeStartDate requires throughToday")
            # Reject an invalid month range before admitting background work.
            activity_timeline_date_range(timeline_date, start_date=range_start_date)
        job = self.memory_maintenance_jobs.trigger(
            {
                "project": self.config.project,
                "manual": True,
                "maxSources": 1,
                "timelineOnly": True,
                "timelineDate": "" if through_today else timeline_date,
                "timelineThroughDate": timeline_date if through_today else "",
                **({"timelineStartDate": range_start_date} if range_start_date else {}),
            }
        )
        if job.get("reused") is True:
            raise RuntimeError(
                "Another Memory organization job is active; retry this day after it settles"
            )
        return job

    def activity_timeline_approve(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        timeline = self.activity_timelines.approve(
            _string(payload.get("timelineId")),
            expected_source_event_hash=_string(
                payload.get("expectedSourceEventHash")
            ),
            approved_by="control-center-user",
            confirm_text=_string(payload.get("confirmText")),
        )
        return {
            "schemaVersion": "rag-ime.daily-activity-timeline-decision.v1",
            "ok": True,
            "decision": "accepted",
            "timeline": timeline,
        }

    def activity_timeline_reject(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if _string(payload.get("confirmText")).lower() != "reject":
            raise ValueError("confirmText must be reject")
        timeline = self.activity_timelines.reject(
            _string(payload.get("timelineId")),
            reason=_string(payload.get("reason")),
            rejected_by="control-center-user",
        )
        return {
            "schemaVersion": "rag-ime.daily-activity-timeline-decision.v1",
            "ok": True,
            "decision": "rejected",
            "timeline": timeline,
        }

    def _initial_embedding_warmup_report(self) -> dict[str, object]:
        provider = getattr(self.core, "embedding_provider", None)
        fingerprint = str(getattr(provider, "fingerprint", "") or "")
        enabled_value = os.environ.get("RAG_IME_EMBEDDING_WARMUP", "1").strip().lower()
        enabled = fingerprint.startswith("mlx-bert:") and enabled_value not in {"0", "false", "no", "off"}
        report: dict[str, object] = {
            "schemaVersion": "rag-ime.embedding-warmup.v1",
            "enabled": enabled,
            "status": "pending" if enabled else "complete",
            "providerFingerprint": fingerprint,
            "ok": False,
            "delayMs": int(round(self._embedding_warmup_delay_s * 1_000)),
            "elapsedMs": 0,
            "modelElapsedMs": 0,
            "vectorCacheElapsedMs": 0,
            "vectorDocuments": 0,
            "dimensions": 0,
        }
        if not enabled or provider is None:
            report["skippedReason"] = "provider_not_local_mlx" if provider is not None else "provider_missing"
        return report

    def embedding_warmup_status(self) -> dict[str, object]:
        with self._lifecycle_lock:
            return dict(self._embedding_warmup_report)

    def _initial_startup_recovery_report(self) -> dict[str, object]:
        core_pending = (
            self.core.startup_maintenance_pending()
            if isinstance(self.core, LocalSqliteCoreClient)
            and self._agent_runtime_execution_owner
            else False
        )
        agent_status_loader = getattr(self.agent, "startup_recovery_status", None)
        agent_status = (
            agent_status_loader()
            if callable(agent_status_loader)
            else {
                "enabled": False,
                "status": "complete",
                "ok": True,
                "skippedReason": "unsupported_agent_service",
            }
        )
        agent_pending = str(agent_status.get("status") or "") == "pending"
        enabled = core_pending or agent_pending
        return {
            "schemaVersion": "rag-ime.startup-recovery.v1",
            "enabled": enabled,
            "status": "pending" if enabled else "complete",
            "ok": not enabled,
            "delayMs": int(round(self._embedding_warmup_delay_s * 1_000)),
            "error": "",
            "components": {
                "coreMaintenance": {
                    "enabled": (
                        isinstance(self.core, LocalSqliteCoreClient)
                        and self._agent_runtime_execution_owner
                    ),
                    "status": "pending" if core_pending else "complete",
                    **(
                        {}
                        if self._agent_runtime_execution_owner
                        else {"skippedReason": "not_execution_owner"}
                    ),
                },
                "agentRecovery": dict(agent_status),
            },
        }

    def startup_recovery_status(self) -> dict[str, object]:
        with self._lifecycle_lock:
            report = copy.deepcopy(self._startup_recovery_report)
        return report

    def _run_startup_recovery(self) -> None:
        try:
            if (
                isinstance(self.core, LocalSqliteCoreClient)
                and self._agent_runtime_execution_owner
            ):
                self.core.run_startup_maintenance()
            agent_recovery = getattr(self.agent, "run_startup_recovery", None)
            if callable(agent_recovery):
                agent_recovery()
        except Exception as exc:
            with self._lifecycle_lock:
                self._startup_recovery_report = {
                    **self._startup_recovery_report,
                    "status": "failed",
                    "ok": False,
                    "error": exc.__class__.__name__,
                    "components": self._startup_recovery_components(),
                }
            return
        with self._lifecycle_lock:
            self._startup_recovery_report = {
                **self._startup_recovery_report,
                "status": "complete",
                "ok": True,
                "error": "",
                "components": self._startup_recovery_components(),
            }

    def _startup_recovery_components(self) -> dict[str, object]:
        agent_status_loader = getattr(self.agent, "startup_recovery_status", None)
        agent_status = (
            agent_status_loader()
            if callable(agent_status_loader)
            else {
                "enabled": False,
                "status": "complete",
                "ok": True,
                "skippedReason": "unsupported_agent_service",
            }
        )
        return {
            "coreMaintenance": {
                "enabled": (
                    isinstance(self.core, LocalSqliteCoreClient)
                    and self._agent_runtime_execution_owner
                ),
                "status": (
                    "pending"
                    if isinstance(self.core, LocalSqliteCoreClient)
                    and self._agent_runtime_execution_owner
                    and self.core.startup_maintenance_pending()
                    else "complete"
                ),
                **(
                    {}
                    if self._agent_runtime_execution_owner
                    else {"skippedReason": "not_execution_owner"}
                ),
            },
            "agentRecovery": dict(agent_status),
        }

    def _start_background_startup_lane(self) -> None:
        with self._lifecycle_lock:
            if (
                self._closed
                or self._background_startup_thread is not None
                or self._background_startup_timer is not None
            ):
                return
            has_pending_heavy_work = (
                self._startup_recovery_report.get("status") == "pending"
                or self._embedding_warmup_report.get("status") == "pending"
            )
            if self._embedding_warmup_delay_s <= 0 or not has_pending_heavy_work:
                timer = None
            else:
                timer = Timer(
                    self._embedding_warmup_delay_s,
                    self._begin_background_startup_lane,
                )
                timer.name = "rag-ime-background-startup-delay"
                timer.daemon = True
                self._background_startup_timer = timer
        if timer is None:
            self._begin_background_startup_lane()
            return
        try:
            timer.start()
        except Exception as exc:  # pragma: no cover - thread start failure is platform-specific
            with self._lifecycle_lock:
                if self._background_startup_timer is timer:
                    self._background_startup_timer = None
                self._fail_pending_background_startup(exc)

    def _begin_background_startup_lane(self) -> None:
        with self._lifecycle_lock:
            self._background_startup_timer = None
            if self._closed or self._background_startup_thread is not None:
                return
            if self._startup_recovery_report.get("status") == "pending":
                self._startup_recovery_report = {
                    **self._startup_recovery_report,
                    "status": "running",
                    "ok": False,
                    "error": "",
                }
            thread = Thread(
                target=self._run_background_startup_lane,
                name="rag-ime-background-startup",
                daemon=True,
            )
            self._background_startup_thread = thread
        try:
            thread.start()
        except Exception as exc:  # pragma: no cover - thread start failure is platform-specific
            with self._lifecycle_lock:
                self._background_startup_thread = None
                self._fail_pending_background_startup(exc)

    def _fail_pending_background_startup(self, exc: Exception) -> None:
        if self._startup_recovery_report.get("status") in {"pending", "running"}:
            self._startup_recovery_report = {
                **self._startup_recovery_report,
                "status": "failed",
                "ok": False,
                "error": exc.__class__.__name__,
            }
        if self._embedding_warmup_report.get("status") == "pending":
            self._embedding_warmup_report = {
                **self._embedding_warmup_report,
                "status": "failed",
                "error": exc.__class__.__name__,
            }

    def _run_background_startup_lane(self) -> None:
        if self._startup_recovery_report.get("status") in {"pending", "running"}:
            self._run_startup_recovery()
        with self._lifecycle_lock:
            if self._closed:
                return
            run_embedding_warmup = (
                self._embedding_warmup_report.get("status") == "pending"
            )
            if run_embedding_warmup:
                self._embedding_warmup_report = {
                    **self._embedding_warmup_report,
                    "status": "running",
                }
        if run_embedding_warmup:
            self._run_embedding_warmup()
        with self._lifecycle_lock:
            if self._closed:
                return
        self._start_memory_projection_worker()

    def _start_memory_projection_worker(self) -> None:
        worker = self.memory_projection_worker
        if worker is None:
            return
        try:
            self._memory_projection_start_error = ""
            worker.start()
        except Exception as exc:  # pragma: no cover - thread start failure is platform-specific
            self._memory_projection_start_error = _safe_debug_error(exc)

    def _run_embedding_warmup(self) -> None:
        report = self._warm_embedding_provider()
        with self._lifecycle_lock:
            self._embedding_warmup_report = report

    def _warm_embedding_provider(self) -> dict[str, object]:
        report = self._initial_embedding_warmup_report()
        report["status"] = "running"
        provider = getattr(self.core, "embedding_provider", None)
        if not report["enabled"] or provider is None:
            report["status"] = "complete"
            return report
        started = time.perf_counter()
        try:
            vector = embed_query(provider, "输入法语义检索预热")
            model_elapsed_ms = int((time.perf_counter() - started) * 1000)
            vector_cache_report = (
                self.core.warm_retrieval_vector_cache(project=self.config.project)
                if isinstance(self.core, LocalSqliteCoreClient)
                else {"ok": True, "elapsedMs": 0, "documents": 0}
            )
        except Exception as exc:  # pragma: no cover - fail-open runtime guard
            report.update(
                {
                    "status": "failed",
                    "elapsedMs": int((time.perf_counter() - started) * 1000),
                    "error": exc.__class__.__name__,
                }
            )
            return report
        report.update(
            {
                "status": "complete",
                "ok": bool(vector) and bool(vector_cache_report.get("ok")),
                "elapsedMs": int((time.perf_counter() - started) * 1000),
                "modelElapsedMs": model_elapsed_ms,
                "vectorCacheElapsedMs": int(vector_cache_report.get("elapsedMs") or 0),
                "vectorDocuments": int(vector_cache_report.get("documents") or 0),
                "dimensions": len(vector),
            }
        )
        if not vector:
            report["error"] = "empty_embedding"
        elif not vector_cache_report.get("ok"):
            report["error"] = "vector_cache_warmup_failed"
        return report

    def frontend_capabilities(self) -> dict[str, object]:
        return self.frontend_gateway.capabilities()

    def control_capabilities(
        self,
        access_context: ControlAccessContext | None = None,
    ) -> dict[str, object]:
        bootstrap = self.control_api.bootstrap()
        routes = (
            default_route_policy().manifest(
                context=access_context,
                include_targets=False,
            )
            if access_context is not None
            else bootstrap["routes"]
        )
        features: dict[str, object] = capability_feature_flags(routes)
        features.update(
            {
                "workDocuments": True,
            }
        )
        return {
            "schemaVersion": "rag-ime.control-capabilities.v1",
            "apiVersion": bootstrap["apiVersion"],
            "features": features,
            "platform": bootstrap["platform"],
            "routes": routes,
        }

    def control_bootstrap(
        self,
        access_context: ControlAccessContext | None = None,
    ) -> dict[str, object]:
        payload = self.control_api.bootstrap()
        routes = (
            default_route_policy().manifest(
                context=access_context,
                include_targets=False,
            )
            if access_context is not None
            else payload["routes"]
        )
        payload["routes"] = routes
        existing = payload.get("features")
        features: dict[str, object] = dict(existing) if isinstance(existing, Mapping) else {}
        features.update(capability_feature_flags(routes))
        features["workDocuments"] = True
        payload["features"] = features
        return payload

    def frontend_suggest(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.frontend_gateway.suggest(payload)

    def frontend_select(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.frontend_gateway.select(payload)

    def predictor_status(self) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.predictor-status.v1",
            "ok": True,
            "predictor": self._predictor_status(probe_capabilities=True),
        }

    def settings(self) -> dict[str, object]:
        settings = self._settings_with_agent_authority(
            self._settings_with_voice_hotword_authority(
                self.settings_store.get_settings()
            )
        )
        snapshot = self.runtime_config_snapshot()
        return {
            **settings_response(settings),
            "effectiveSettings": snapshot.effective_settings(settings),
            "runtimeConfig": snapshot.payload(),
            "voiceControl": read_voice_control_status(self.voice_support_directory),
        }

    def runtime_config_snapshot(
        self,
        *,
        settings: dict[str, object] | None = None,
    ) -> RuntimeConfigSnapshot:
        return self.runtime_config_resolver.resolve(settings=settings)

    def runtime_config(self) -> dict[str, object]:
        return self.management.runtime_config()

    def management_security_settings(self) -> dict[str, object]:
        settings = self.settings_store.get_settings(include_sensitive=True)
        security = settings.get("managementSecurity") if isinstance(settings.get("managementSecurity"), dict) else {}
        return dict(security)

    def settings_schema(self) -> dict[str, object]:
        return {"ok": True, **self.settings_store.schema_payload()}

    def settings_update(self, payload: dict[str, Any]) -> dict[str, object]:
        updated_by = _string(payload.get("updatedBy")) or "local-console"
        result = self.settings_store.update_settings(
            payload,
            updated_by=updated_by,
            confirm_text=_string(payload.get("confirmText")),
        )
        if any(key.startswith("voice.") for key in result.changed_keys):
            persisted = self.settings_store.get_settings(include_sensitive=True)
            self._write_voice_hotwords_from_settings(persisted)
            result = SettingsUpdateResult(
                settings=self._settings_with_voice_hotword_authority(result.settings),
                audit_id=result.audit_id,
                changed_keys=result.changed_keys,
            )
        return self._settings_update_response(result, updated_by=updated_by)

    def configuration_settings_preview(
        self,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        current = {"runtimeRevision": self.management.revision().runtime_revision}
        try:
            _require_management_fields(
                payload,
                required={"changes", "expectedRuntimeRevision"},
                optional=set(),
            )
            expected_runtime = _strict_management_revision(
                payload.get("expectedRuntimeRevision")
            )
            if expected_runtime != current["runtimeRevision"]:
                raise ManagementWorkError(
                    "revision_mismatch",
                    "The configuration snapshot revision is stale.",
                    current_revision=current,
                )
            changes = self._configuration_settings_changes(payload.get("changes"))
            with self.settings_store.connection() as conn:
                settings = self._settings_with_voice_hotword_authority(
                    self.settings_store.get_settings_from_connection(conn)
                )
                current_flat = flatten_settings(settings)
                changed_keys = [
                    key for key, value in changes.items() if current_flat.get(key) != value
                ]
                revision = self.management.management_work_revision(
                    conn,
                    subject_revision=stable_settings_hash(settings),
                )
            if not changed_keys:
                raise ManagementWorkError(
                    "domain_not_applicable",
                    "The requested settings already have these values.",
                    current_revision=revision,
                )
            restart_components = self._configuration_restart_components(changed_keys)
            return self.management.work_contract.create_preview(
                path_id="configuration.settings.apply",
                payload={"changes": changes},
                expected_revision=revision,
                required_confirm="apply",
                summary={
                    "title": "应用控制中心设置",
                    "items": [
                        *(f"更新 {key}" for key in changed_keys[:8]),
                        *(
                            [f"另有 {len(changed_keys) - 8} 项设置"]
                            if len(changed_keys) > 8
                            else []
                        ),
                        *(
                            [f"需要重载: {', '.join(restart_components)}"]
                            if restart_components
                            else []
                        ),
                    ],
                    "risk": "R2" if restart_components else "R1",
                },
            )
        except Exception as exc:
            return self.management.work_contract.error_payload(
                exc,
                current_revision=current,
            )

    def configuration_settings_apply(
        self,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        current = {"runtimeRevision": self.management.revision().runtime_revision}
        try:
            _require_management_fields(
                payload,
                required={
                    "changes",
                    "expectedRuntimeRevision",
                    "previewToken",
                    "payloadSha256",
                    "confirmText",
                },
                optional=set(),
            )
            expected_runtime = _strict_management_revision(
                payload.get("expectedRuntimeRevision")
            )
            if expected_runtime != current["runtimeRevision"]:
                raise ManagementWorkError(
                    "revision_mismatch",
                    "The configuration apply request revision is stale.",
                    current_revision=current,
                )
            changes = self._configuration_settings_changes(payload.get("changes"))
            applied_result: SettingsUpdateResult | None = None

            def current_revision(conn: sqlite3.Connection) -> dict[str, object]:
                settings = self._settings_with_voice_hotword_authority(
                    self.settings_store.get_settings_from_connection(conn)
                )
                return self.management.management_work_revision(
                    conn,
                    subject_revision=stable_settings_hash(settings),
                )

            def execute(conn: sqlite3.Connection) -> WorkExecution:
                nonlocal applied_result
                before = self._settings_with_voice_hotword_authority(
                    self.settings_store.get_settings_from_connection(
                        conn,
                        include_sensitive=True,
                    )
                )
                before_flat = flatten_settings(before)
                changed_keys = [
                    key for key, value in changes.items() if before_flat.get(key) != value
                ]
                if not changed_keys:
                    raise ManagementWorkError(
                        "domain_not_applicable",
                        "The requested settings already have these values.",
                    )
                before_values = {key: before_flat[key] for key in changed_keys}
                stored_result = self.settings_store.update_settings_in_connection(
                    conn,
                    changes,
                    updated_by="control-center-web",
                    audit_action="configuration_settings_apply",
                )
                missing_db_changes = set(changed_keys) - set(stored_result.changed_keys)
                if (
                    not set(stored_result.changed_keys).issubset(changed_keys)
                    or any(not key.startswith("voice.") for key in missing_db_changes)
                ):
                    raise ManagementWorkError(
                        "revision_mismatch",
                        "The settings changed after this preview was created.",
                    )
                persisted_after = self.settings_store.get_settings_from_connection(
                    conn,
                    include_sensitive=True,
                )
                if any(key.startswith("voice.") for key in changed_keys):
                    self._write_voice_hotwords_from_settings(persisted_after)
                after = self._settings_with_voice_hotword_authority(persisted_after)
                after_flat = flatten_settings(after)
                after_values = {key: after_flat[key] for key in changed_keys}
                applied_result = SettingsUpdateResult(
                    settings=after,
                    audit_id=stored_result.audit_id,
                    changed_keys=tuple(changed_keys),
                )
                after_revision = stable_settings_hash(after)
                return WorkExecution(
                    result={
                        "schemaVersion": "rag-ime.configuration-settings-mutation.v1",
                        "ok": True,
                        "changedKeys": list(applied_result.changed_keys),
                        "settingsHash": after_revision,
                    },
                    audit_action="configuration_settings_apply",
                    target_type="settings",
                    target_id=",".join(changed_keys),
                    rollback_available=True,
                    rollback_path_id="configuration.settings.rollback",
                    rollback_confirm="rollback",
                    rollback_authority={"settingKeys": changed_keys},
                    rollback_data={
                        "beforeValues": before_values,
                        "afterValues": after_values,
                        "afterRevision": after_revision,
                    },
                    restart_components=self._configuration_restart_components(changed_keys),
                    audit_id=applied_result.audit_id,
                )

            response = self.management.work_contract.execute_apply(
                path_id="configuration.settings.apply",
                payload={"changes": changes},
                preview_token=_string(payload.get("previewToken")),
                payload_sha256=_string(payload.get("payloadSha256")),
                confirm_text=_string(payload.get("confirmText")),
                current_revision=current_revision,
                executor=execute,
            )
            if applied_result is not None:
                self._attach_settings_update_effects(
                    response,
                    applied_result,
                    updated_by="control-center-web",
                )
            return response
        except Exception as exc:
            return self.management.work_contract.error_payload(
                exc,
                current_revision=current,
            )

    def configuration_settings_rollback(
        self,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        current = {"runtimeRevision": self.management.revision().runtime_revision}
        try:
            _require_management_fields(
                payload,
                required={"receiptId", "rollbackToken", "payloadSha256", "confirmText"},
                optional=set(),
            )
            rollback_result: SettingsUpdateResult | None = None

            def execute(
                conn: sqlite3.Connection,
                receipt: StoredReceipt,
            ) -> WorkExecution:
                nonlocal rollback_result
                raw_keys = receipt.rollback_authority.get("settingKeys")
                if not isinstance(raw_keys, list) or not raw_keys:
                    raise ManagementWorkError(
                        "stored_contract_invalid",
                        "The settings rollback authority is invalid.",
                    )
                setting_keys = [str(key) for key in raw_keys]
                if setting_keys != sorted(set(setting_keys)):
                    raise ManagementWorkError(
                        "stored_contract_invalid",
                        "The settings rollback authority is invalid.",
                    )
                before_values = receipt.rollback_data.get("beforeValues")
                after_values = receipt.rollback_data.get("afterValues")
                if not isinstance(before_values, Mapping) or not isinstance(after_values, Mapping):
                    raise ManagementWorkError(
                        "stored_contract_invalid",
                        "The settings rollback snapshot is invalid.",
                    )
                if set(before_values) != set(setting_keys) or set(after_values) != set(setting_keys):
                    raise ManagementWorkError(
                        "stored_contract_invalid",
                        "The settings rollback snapshot does not match its authority.",
                    )
                current_settings = self._settings_with_voice_hotword_authority(
                    self.settings_store.get_settings_from_connection(
                        conn,
                        include_sensitive=True,
                    )
                )
                current_flat = flatten_settings(current_settings)
                current_revision = stable_settings_hash(current_settings)
                if (
                    current_revision != receipt.rollback_data.get("afterRevision")
                    or any(current_flat.get(key) != after_values[key] for key in setting_keys)
                ):
                    raise ManagementWorkError(
                        "rollback_state_changed",
                        "The settings changed after the apply receipt was issued.",
                        current_revision=self.management.management_work_revision(
                            conn,
                            subject_revision=current_revision,
                        ),
                    )
                stored_result = self.settings_store.update_settings_in_connection(
                    conn,
                    dict(before_values),
                    updated_by="control-center-web",
                    audit_action="configuration_settings_rollback",
                )
                persisted_after = self.settings_store.get_settings_from_connection(
                    conn,
                    include_sensitive=True,
                )
                if any(key.startswith("voice.") for key in setting_keys):
                    self._write_voice_hotwords_from_settings(persisted_after)
                effective_after = self._settings_with_voice_hotword_authority(persisted_after)
                rollback_result = SettingsUpdateResult(
                    settings=effective_after,
                    audit_id=stored_result.audit_id,
                    changed_keys=tuple(setting_keys),
                )
                return WorkExecution(
                    result={
                        "schemaVersion": "rag-ime.configuration-settings-mutation.v1",
                        "ok": True,
                        "changedKeys": list(rollback_result.changed_keys),
                        "settingsHash": stable_settings_hash(effective_after),
                    },
                    audit_action="configuration_settings_rollback",
                    target_type="settings",
                    target_id=",".join(setting_keys),
                    restart_components=self._configuration_restart_components(setting_keys),
                    audit_id=rollback_result.audit_id,
                )

            response = self.management.work_contract.execute_rollback(
                path_id="configuration.settings.rollback",
                receipt_id=_string(payload.get("receiptId")),
                rollback_token=_string(payload.get("rollbackToken")),
                payload_sha256=_string(payload.get("payloadSha256")),
                confirm_text=_string(payload.get("confirmText")),
                expected_apply_path_id="configuration.settings.apply",
                executor=execute,
            )
            if rollback_result is not None:
                self._attach_settings_update_effects(
                    response,
                    rollback_result,
                    updated_by="control-center-web",
                )
            return response
        except Exception as exc:
            return self.management.work_contract.error_payload(
                exc,
                current_revision=current,
            )

    def _configuration_settings_changes(self, value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise ManagementWorkError("invalid_request", "changes must be an object.")
        transport_fields = {
            "auditId",
            "confirmText",
            "previewToken",
            "runtimeRevision",
            "schemaVersion",
            "settings",
            "settingsRevision",
            "updatedBy",
        }
        unsupported = sorted(transport_fields.intersection(str(key) for key in value))
        if unsupported:
            raise ManagementWorkError(
                "invalid_request",
                f"Unsupported changes fields: {', '.join(unsupported)}.",
            )
        try:
            normalized = self.settings_store.normalize_updates(value)
        except ValueError as exc:
            raise ManagementWorkError("invalid_request", str(exc)) from exc
        changes = {
            key: item
            for key, item in sorted(flatten_settings(normalized).items())
        }
        if not changes:
            raise ManagementWorkError("invalid_request", "changes must not be empty.")
        if len(changes) > 32:
            raise ManagementWorkError(
                "invalid_request",
                "A settings mutation may contain at most 32 fields.",
            )
        for key in changes:
            normalized_key = key.lower()
            if normalized_key.startswith("managementsecurity.") or any(
                normalized_key.endswith(suffix.lower())
                for suffix in SENSITIVE_SETTING_SUFFIXES
            ):
                raise ManagementWorkError(
                    "unsupported_mutation",
                    f"Setting {key} must be changed through its dedicated secure flow.",
                )
        if PREDICTOR_SETTING_KEYS.intersection(changes):
            merged = deep_merge_settings(
                self.settings_store.get_settings(include_sensitive=True),
                normalized,
            )
            try:
                resolve_predictor_configuration(
                    merged,
                    registry=ModelRegistry.load(_configured_model_registry_path()),
                    require_model_exists=True,
                )
            except (OSError, ValueError) as exc:
                raise ManagementWorkError("invalid_request", str(exc)) from exc
        return changes

    def _configuration_restart_components(
        self,
        changed_keys: list[str],
    ) -> tuple[str, ...]:
        fields = {
            str(field.get("key")): field
            for section in self.settings_store.schema_payload().get("sections", [])
            if isinstance(section, Mapping)
            for field in section.get("fields", [])
            if isinstance(field, Mapping)
        }
        return tuple(
            sorted(
                {
                    str(fields[key].get("restartComponent"))
                    for key in changed_keys
                    if key in fields and fields[key].get("restartComponent")
                }
            )
        )

    def _settings_update_response(
        self,
        result: SettingsUpdateResult,
        *,
        updated_by: str,
    ) -> dict[str, object]:
        persisted = self._settings_with_voice_hotword_authority(
            self.settings_store.get_settings(include_sensitive=True)
        )
        agent_sync = None
        if self._agent_managed_by_settings and any(key.startswith("agent.pi.") for key in result.changed_keys):
            agent_sync = self._sync_agent_settings(
                persisted,
                updated_by=updated_by,
            )
        snapshot = self.runtime_config_snapshot(settings=persisted)
        _apply_pinyin_settings_to_process_env(snapshot.effective_settings(persisted))
        self._clear_rime_cache()
        response = {
            **settings_response(
                self._settings_with_agent_authority(
                    self._settings_with_voice_hotword_authority(result.settings)
                )
            ),
            "auditId": result.audit_id,
            "changedKeys": list(result.changed_keys),
            **self.management.settings_changed(
                audit_id=result.audit_id,
                changed_keys=list(result.changed_keys),
                snapshot=snapshot,
            ),
        }
        active_settings = (
            result.settings.get("activeRag")
            if isinstance(result.settings.get("activeRag"), dict)
            else {}
        )
        response["runtimeSync"] = self._apply_active_rag_runtime_sync(
            active_settings=active_settings,
            changed_keys=tuple(result.changed_keys),
        )
        if agent_sync is not None:
            response["agentSync"] = agent_sync
        response["voiceControl"] = read_voice_control_status(self.voice_support_directory)
        return response

    def _attach_settings_update_effects(
        self,
        receipt: dict[str, object],
        result: SettingsUpdateResult,
        *,
        updated_by: str,
    ) -> None:
        try:
            effects = self._settings_update_response(result, updated_by=updated_by)
        except Exception as exc:  # pragma: no cover - defensive post-commit boundary
            receipt["runtimeSync"] = {
                "attempted": True,
                "applied": False,
                "error": type(exc).__name__,
            }
            return
        receipt["runtimeRevision"] = effects.get("runtimeRevision", 0)
        receipt["settingsRevision"] = effects.get("settingsRevision", "")
        receipt["runtimeConfig"] = effects.get("runtimeConfig", {})
        receipt["runtimeSync"] = effects.get("runtimeSync", {})
        receipt["voiceControl"] = effects.get("voiceControl", {})
        if "agentSync" in effects:
            receipt["agentSync"] = effects["agentSync"]
        domain = receipt.get("result")
        if isinstance(domain, dict):
            domain["runtimeRevision"] = effects.get("runtimeRevision", 0)
            domain["settingsRevision"] = effects.get("settingsRevision", "")

    def settings_reset_section(self, payload: dict[str, Any]) -> dict[str, object]:
        section = _string(payload.get("section"))
        result = self.settings_store.reset_section(section, updated_by=_string(payload.get("updatedBy")) or "local-console")
        if section == "voice":
            self._write_voice_hotwords_from_settings(
                self.settings_store.get_settings(include_sensitive=True)
            )
            result = SettingsUpdateResult(
                settings=self._settings_with_voice_hotword_authority(result.settings),
                audit_id=result.audit_id,
                changed_keys=result.changed_keys,
            )
        persisted = self.settings_store.get_settings(include_sensitive=True)
        agent_sync = None
        if self._agent_managed_by_settings and section == "agent":
            agent_sync = self._sync_agent_settings(
                persisted,
                updated_by=_string(payload.get("updatedBy")) or "local-console",
            )
        snapshot = self.runtime_config_snapshot(settings=persisted)
        _apply_pinyin_settings_to_process_env(snapshot.effective_settings(persisted))
        self._clear_rime_cache()
        response = {
            **settings_response(self._settings_with_agent_authority(result.settings)),
            "auditId": result.audit_id,
            "changedKeys": list(result.changed_keys),
            "section": section,
            **self.management.settings_changed(
                audit_id=result.audit_id,
                changed_keys=list(result.changed_keys),
                snapshot=snapshot,
            ),
        }
        active_settings = (
            result.settings.get("activeRag")
            if isinstance(result.settings.get("activeRag"), dict)
            else {}
        )
        response["runtimeSync"] = self._apply_active_rag_runtime_sync(
            active_settings=active_settings,
            changed_keys=tuple(result.changed_keys),
        )
        if agent_sync is not None:
            response["agentSync"] = agent_sync
        response["voiceControl"] = read_voice_control_status(self.voice_support_directory)
        return response

    def _settings_with_agent_authority(
        self,
        settings: dict[str, object],
    ) -> dict[str, object]:
        result = copy.deepcopy(settings)
        snapshot = self.agent.configuration()["configuration"]
        configuration = snapshot["configuration"]
        runtime = configuration["runtime"]
        defaults = configuration["sessionDefaults"]
        coordination = configuration["coordination"]
        agent = result.setdefault("agent", {})
        if not isinstance(agent, dict):
            agent = {}
            result["agent"] = agent
        pi = agent.setdefault("pi", {})
        if not isinstance(pi, dict):
            pi = {}
            agent["pi"] = pi
        pi.update(
            {
                "enabled": runtime["enabled"],
                "startup": runtime["startup"],
                "idleTimeoutSeconds": runtime["idleTimeoutSeconds"],
                "resumeLastSession": defaults["resumeLastSession"],
                "defaultRoleId": defaults["roleId"],
                "toolProfile": defaults["toolProfileVersion"],
                "coordinatorEnabled": coordination["enabled"],
            }
        )
        return result

    def _settings_with_voice_hotword_authority(
        self,
        settings: dict[str, object],
    ) -> dict[str, object]:
        result = copy.deepcopy(settings)
        status = self.voice_hotwords.read_status()
        voice = result.setdefault("voice", {})
        if not isinstance(voice, dict):
            voice = {}
            result["voice"] = voice
        voice["hotwordsEnabled"] = status.get("enabled") is True
        words = status.get("words")
        voice["hotwords"] = list(words) if isinstance(words, list) else []
        voice.update(read_voice_preferences(self.voice_support_directory))
        return result

    def _write_voice_hotwords_from_settings(
        self,
        settings: Mapping[str, object],
    ) -> None:
        self.voice_hotwords.write(voice_hotword_config_from_settings(settings))
        write_voice_preferences_from_settings(self.voice_support_directory, settings)

    def _sync_agent_settings(
        self,
        settings: dict[str, object],
        *,
        updated_by: str,
    ) -> dict[str, object]:
        agent = settings.get("agent") if isinstance(settings.get("agent"), dict) else {}
        pi = agent.get("pi") if isinstance(agent.get("pi"), dict) else {}
        snapshot = self.agent.configuration()["configuration"]
        current = snapshot["configuration"]
        desired = {
            "runtime.enabled": bool(pi.get("enabled")),
            "runtime.startup": _string(pi.get("startup")) or "lazy",
            "runtime.idleTimeoutSeconds": _bounded_int(
                pi.get("idleTimeoutSeconds"),
                default=900,
                minimum=0,
                maximum=86_400,
            ),
            "sessionDefaults.resumeLastSession": bool(pi.get("resumeLastSession")),
            "sessionDefaults.roleId": _string(pi.get("defaultRoleId")) or "companion-future-v1",
            "sessionDefaults.toolProfileVersion": (
                _string(pi.get("toolProfile")) or "control-center-v1"
            ),
            "coordination.enabled": bool(pi.get("coordinatorEnabled")),
        }
        changes = {
            key: value
            for key, value in desired.items()
            if _nested_agent_configuration_value(current, key) != value
        }
        if not changes:
            return {
                "schemaVersion": "rag-ime.agent-configuration-update.v1",
                "ok": True,
                "changedKeys": [],
                "configuration": snapshot,
                "event": None,
            }
        return self.agent.update_configuration(
            {
                "expectedRevision": snapshot["revision"],
                "changes": changes,
                "updatedBy": updated_by,
            }
        )

    def _last_management_prediction(self) -> dict[str, object]:
        if not self._prediction_live_trace:
            return {}
        item = next(
            (
                frame
                for frame in reversed(self._prediction_live_trace)
                if not _string(frame.get("sessionId")).startswith(("doctor", "native-doctor", "cache-probe"))
            ),
            None,
        )
        if item is None:
            return {}
        foreground = (
            dict(item.get("foregroundContext"))
            if isinstance(item.get("foregroundContext"), dict)
            else {}
        )
        return {
            "requestId": item.get("requestId", ""),
            "triggerReason": item.get("triggerReason", item.get("reason", "")),
            "contextSource": foreground.get("source", item.get("foregroundContextSource", "")),
            "foregroundContext": foreground,
            "contextInjection": {
                "success": foreground.get("applied") is True,
                "error": (
                    foreground.get("captureFailureReason") or foreground.get("reason") or ""
                    if foreground.get("applied") is not True
                    else ""
                ),
            },
            "sourceTypes": item.get("sourceTypes", []),
            "visibleCandidate": item.get("visibleCandidate", ""),
            "totalLatencyMs": item.get("totalLatencyMs", item.get("elapsedMs", 0)),
            "providerCallCount": item.get("providerCallCount", 0),
            "createdAtMs": item.get("createdAtMs", 0),
        }

    def profiles(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.settings_store.list_profiles(kind=_string(payload.get("kind")))

    def management_audit(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {"schemaVersion": "rag-ime.management-audit.v3", "ok": False, "items": [], "error": "local SQLite core required"}
        limit = _bounded_int(payload.get("limit"), default=50, minimum=1, maximum=200)
        action = _string(payload.get("action"))
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            _ensure_management_audit_schema(conn)
            rows = conn.execute(
                """
                SELECT id, created_at_ms, action, target_type, target_id, payload_json, result_json
                FROM management_audit_log
                WHERE (? = '' OR action = ?)
                ORDER BY id DESC
                LIMIT ?
                """,
                (action, action, limit),
            ).fetchall()
        return {
            "schemaVersion": "rag-ime.management-audit.v3",
            "ok": True,
            "items": [
                {
                    "auditId": int(row["id"]),
                    "createdAtMs": int(row["created_at_ms"]),
                    "action": str(row["action"]),
                    "targetType": str(row["target_type"]),
                    "targetId": str(row["target_id"]),
                    "payload": _redact_mapping(_json_loads_dict(row["payload_json"]), include_text=self._include_raw_text()),
                    "result": _redact_mapping(_json_loads_dict(row["result_json"]), include_text=self._include_raw_text()),
                }
                for row in rows
            ],
        }

    def profile_save(self, payload: dict[str, Any]) -> dict[str, object]:
        profile = UserProfile(
            profile_id=_string(payload.get("id") or payload.get("profileId")),
            profile_kind=_string(payload.get("kind") or payload.get("profileKind")) or "interaction_profile",
            label=_string(payload.get("label")) or _string(payload.get("id") or payload.get("profileId")),
            description=_string(payload.get("description")),
            settings=dict(payload.get("settings") or {}) if isinstance(payload.get("settings"), dict) else {},
            enabled=_bool(payload.get("enabled"), default=True),
        )
        return self.settings_store.save_profile(profile)

    def profile_activate_dry_run(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.settings_store.activate_profile_dry_run(_string(payload.get("id") or payload.get("profileId")))

    def vocabulary_items(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.settings_store.list_vocabulary(status=_string(payload.get("status")), query=_string(payload.get("query")))

    def vocabulary_item_save(self, payload: dict[str, Any], *, action: str) -> dict[str, object]:
        item = UserVocabularyItem(
            vocab_id=_string(payload.get("id") or payload.get("vocabId")),
            surface=_string(payload.get("surface")),
            aliases=tuple(_string_list(payload.get("aliases"))),
            pinyin=_string(payload.get("pinyin")),
            tags=tuple(_string_list(payload.get("tags"))),
            scope=_string(payload.get("scope")) or "global",
            priority=_bounded_int(payload.get("priority"), default=0, minimum=0, maximum=1000),
            status=_string(payload.get("status")) or "active",
        )
        if not item.surface:
            return {"schemaVersion": "rag-ime.user-vocabulary-save.v3", "ok": False, "error": "surface is required"}
        return self.settings_store.save_vocabulary_item(item, action=action)

    def vocabulary_item_delete(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.settings_store.delete_vocabulary_item(_string(payload.get("id") or payload.get("vocabId")))

    def vocabulary_rime_export_preview(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.settings_store.rime_export_preview(status=_string(payload.get("status")) or "active")

    def vocabulary_rime_export_apply(self, payload: dict[str, Any]) -> dict[str, object]:
        target = _string(payload.get("targetFile")) or str(Path.home() / "Library/Rime/rag_ime.user.dict.yaml")
        return self.settings_store.rime_export_apply(target_file=target, confirm_text=_string(payload.get("confirmText")))

    def models_status(self) -> dict[str, object]:
        settings = self.settings_store.get_settings()
        predictor = self._predictor_status(probe_capabilities=True)
        payload: dict[str, object] = {
            "schemaVersion": "rag-ime.models-status.v4",
            "ok": True,
            "settings": settings.get("models", {}),
            "predictor": predictor,
            "activeRagRoute": self.active_rag_route_status(local_only=False),
        }
        try:
            registry = ModelRegistry.load(_configured_model_registry_path())
            desired = resolve_predictor_configuration(
                settings,
                registry=registry,
                require_model_exists=False,
            )
            active = active_predictor_configuration(registry)
            registry_agrees = configuration_matches(desired, active)
            sidecar_agrees = _predictor_status_matches_configuration(
                predictor,
                active,
            )
            capability_probe = (
                predictor.get("capabilityProbe")
                if isinstance(predictor.get("capabilityProbe"), Mapping)
                else {}
            )
            mlx_agrees = _predictor_probe_matches_configuration(
                capability_probe,
                active,
            )
            payload.update(
                {
                    "configurationPending": not registry_agrees,
                    "desiredConfig": desired.payload(),
                    "activeConfig": active.payload(),
                    "availableModels": [
                        _registered_model_option(deployment)
                        for deployment in registry.deployments
                        if deployment.lane == "hot"
                    ],
                    "healthAgreement": {
                        "ok": registry_agrees and sidecar_agrees and mlx_agrees,
                        "desiredMatchesRegistry": registry_agrees,
                        "sidecarMatchesRegistry": sidecar_agrees,
                        "mlxMatchesRegistry": mlx_agrees,
                    },
                }
            )
        except (OSError, ValueError) as exc:
            payload.update(
                {
                    "ok": False,
                    "configurationPending": True,
                    "configurationError": str(exc),
                    "healthAgreement": {
                        "ok": False,
                        "desiredMatchesRegistry": False,
                        "sidecarMatchesRegistry": False,
                        "mlxMatchesRegistry": False,
                    },
                }
            )
        return payload

    def model_profiles(self) -> dict[str, object]:
        active_rag_route = self.active_rag_route_status(local_only=False)
        return {
            "schemaVersion": "rag-ime.model-profiles.v3",
            "ok": True,
            "items": [
                {
                    "id": "qwen3_06b_ime_hot",
                    "label": "Qwen3 0.6B IME Hot",
                    "provider": "mlx",
                    "lane": "hot",
                    "resident": True,
                    "latencyBudgetMs": 500,
                    "enabled": True,
                },
                {
                    "id": "knowledge_provider_active_rag",
                    "label": "Knowledge Provider Active RAG",
                    "provider": active_rag_route["provider"],
                    "lane": "active_rag",
                    "enabled": bool(active_rag_route["remoteReady"]),
                    "requiresExplicitOptIn": True,
                    "skipReason": active_rag_route["skipReason"],
                    "gates": active_rag_route["gates"],
                },
            ],
        }

    def model_probe(self, payload: dict[str, Any]) -> dict[str, object]:
        _ = payload
        return {
            "schemaVersion": "rag-ime.model-probe.v3",
            "ok": True,
            "dryRun": True,
            "predictor": self._predictor_status(probe_capabilities=True, force_refresh=True),
        }

    def model_benchmark_job(self, payload: dict[str, Any]) -> dict[str, object]:
        report = self.predictor_benchmark({**payload, "repeat": _bounded_int(payload.get("repeat"), default=1, minimum=1, maximum=10)})
        return {"schemaVersion": "rag-ime.model-benchmark-job.v3", "ok": True, "job": {"status": "complete", "report": report}}

    def model_activate_dry_run(self, payload: dict[str, Any]) -> dict[str, object]:
        profile_id = _string(payload.get("profileId") or payload.get("id")) or "qwen3_06b_ime_hot"
        return {
            "schemaVersion": "rag-ime.model-activate-dry-run.v3",
            "ok": True,
            "dryRun": True,
            "profileId": profile_id,
            "commands": [
                f"export RAG_IME_PREDICTOR_PROFILE={profile_id}",
                "scripts/restart_rag_ime_runtime.sh",
            ],
        }

    def active_rag_settings(self) -> dict[str, object]:
        settings = self.settings_store.get_settings()
        return {
            "schemaVersion": "rag-ime.active-rag-settings.v3",
            "ok": True,
            "settings": settings.get("activeRag", {}),
            "routeStatus": self.active_rag_route_status(local_only=False),
            "previewRouteStatus": self.active_rag_route_status(),
        }

    def active_rag_route_status(
        self,
        *,
        local_only: bool | None = None,
    ) -> dict[str, object]:
        settings = self.settings_store.get_settings(include_sensitive=True)
        runtime_config = self.runtime_config_snapshot(settings=settings)
        active = settings.get("activeRag") if isinstance(settings.get("activeRag"), dict) else {}
        privacy = settings.get("privacy") if isinstance(settings.get("privacy"), dict) else {}
        resolved_local_only = bool(active.get("localOnlyDefault", True)) if local_only is None else bool(local_only)
        config = load_deepseek_config()
        flags = load_hybrid_rag_runtime_flags()
        gates = {
            "featureEnabled": runtime_config.active_rag.enabled,
            "notLocalOnly": not resolved_local_only,
            "allowRemoteModel": bool(active.get("allowRemoteModel", False)),
            "privacyOptIn": bool(privacy.get("allowRemoteModelForActiveRag", False)),
            "sceneEnvEnabled": bool(flags.deepseek_active_rag),
            "credentialsConfigured": bool(config.api_key),
        }
        skip_reason = ""
        for key, reason in (
            ("featureEnabled", "active_rag_disabled"),
            ("notLocalOnly", "local_only"),
            ("allowRemoteModel", "active_rag_remote_not_allowed"),
            ("privacyOptIn", "privacy_remote_not_allowed"),
            ("sceneEnvEnabled", "scene_flag_disabled"),
            ("credentialsConfigured", "credentials_missing"),
        ):
            if not gates[key]:
                skip_reason = reason
                break
        return {
            "schemaVersion": "rag-ime.active-rag-route-status.v1",
            "route": "explicit_active_rag_knowledge_provider",
            "explicitOnly": True,
            "localOnly": resolved_local_only,
            "remoteReady": all(gates.values()),
            "provider": config.provider_name,
            "model": config.model,
            "selectedModel": (
                f"{config.provider_name}/{config.model}"
                if config.provider_name and config.model
                else "local"
            ),
            "shortcut": runtime_config.active_rag.shortcut,
            "stream": True,
            "skipReason": skip_reason,
            "gates": gates,
            "passivePostCommitRemoteAllowed": False,
        }

    def active_rag_settings_update(self, payload: dict[str, Any]) -> dict[str, object]:
        raw_settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
        settings = {
            key: value
            for key, value in raw_settings.items()
            if key not in {"confirmText", "updatedBy", "previewToken"}
        }
        update_payload = (
            dict(settings)
            if any(str(key).startswith("activeRag.") for key in settings)
            else {"activeRag": dict(settings)}
        )
        update_result = self.settings_store.update_settings(
            update_payload,
            updated_by=_string(payload.get("updatedBy")) or "local-console",
            confirm_text=_string(payload.get("confirmText")),
        )
        self._clear_rime_cache()
        result = {
            **settings_response(update_result.settings),
            "auditId": update_result.audit_id,
            "changedKeys": list(update_result.changed_keys),
        }
        result["runtimeSync"] = self._apply_active_rag_runtime_sync(
            active_settings=(
                result.get("settings", {}).get("activeRag", {})
                if isinstance(result.get("settings"), dict)
                else {}
            ),
            changed_keys=tuple(str(item) for item in result.get("changedKeys", []) if item),
        )
        result["routeStatus"] = self.active_rag_route_status(local_only=False)
        result["previewRouteStatus"] = self.active_rag_route_status()
        return result

    def _apply_active_rag_runtime_sync(
        self,
        *,
        active_settings: object,
        changed_keys: tuple[str, ...],
    ) -> dict[str, object]:
        payload = _active_rag_runtime_sync_payload(
            active_settings=active_settings,
            changed_keys=changed_keys,
        )
        commands = payload.get("commands") if isinstance(payload.get("commands"), list) else []
        relevant = any(key in _ACTIVE_RAG_RUNTIME_SYNC_KEYS for key in changed_keys)
        if not relevant:
            return {**payload, "attempted": False, "applied": True, "results": []}
        results: list[dict[str, object]] = []
        for raw_command in commands:
            command = [str(item) for item in raw_command] if isinstance(raw_command, list) else []
            if not _active_rag_defaults_command_allowed(command):
                results.append({"command": command, "ok": False, "error": "command_not_allowlisted"})
                continue
            try:
                completed = self._runtime_command_runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                return_code = int(getattr(completed, "returncode", 1))
                results.append(
                    {
                        "command": command,
                        "ok": return_code == 0,
                        "returnCode": return_code,
                        "stdout": compact_whitespace(str(getattr(completed, "stdout", "")))[:240],
                        "stderr": compact_whitespace(str(getattr(completed, "stderr", "")))[:240],
                    }
                )
            except Exception as exc:  # pragma: no cover - defensive OS boundary
                results.append(
                    {
                        "command": command,
                        "ok": False,
                        "error": type(exc).__name__,
                    }
                )
        applied = bool(results) and all(bool(item.get("ok")) for item in results)
        return {
            **payload,
            "attempted": True,
            "applied": applied,
            "results": results,
            "error": "" if applied else "one_or_more_defaults_writes_failed",
        }

    def active_rag_management_preview(self, payload: dict[str, Any]) -> dict[str, object]:
        privacy_assessment = assess_foreground_write(payload)
        if privacy_assessment["storeAllowed"] is not True:
            return self._active_rag_privacy_blocked_response(
                privacy_assessment,
                preview=True,
            )
        settings = self.settings_store.get_settings()
        active_settings = settings.get("activeRag") if isinstance(settings.get("activeRag"), dict) else {}
        if not active_settings.get("enabled", True):
            return {"schemaVersion": "rag-ime.active-rag-preview.v3", "ok": False, "error": "Active RAG disabled"}
        max_candidates = _bounded_int(
            payload.get("maxCandidates"),
            default=_bounded_int(active_settings.get("maxCandidates"), default=1, minimum=1, maximum=10),
            minimum=1,
            maximum=10,
        )
        request_payload = {**payload, "maxCandidates": max_candidates}
        local_only = _bool(payload.get("localOnly"), default=bool(active_settings.get("localOnlyDefault", True)))
        route_status = self.active_rag_route_status(local_only=local_only)
        route_gates = route_status.get("gates") if isinstance(route_status.get("gates"), dict) else {}
        request_payload["remoteModelAllowed"] = all(
            bool(route_gates.get(key))
            for key in ("featureEnabled", "notLocalOnly", "allowRemoteModel", "privacyOptIn")
        )
        request_payload["remoteModelSkipReason"] = _string(route_status.get("skipReason"))
        request_payload["remoteModelGates"] = dict(route_gates)
        try:
            request = self._active_rag_request_from_payload(request_payload)
            preview = self.active_rag.preview(request, local_only=local_only)
        except ValueError as exc:
            return {"schemaVersion": "rag-ime.active-rag-preview.v3", "ok": False, "error": str(exc)}
        return _redact_mapping(
            {
                "schemaVersion": "rag-ime.active-rag-preview.v3",
                "localOnly": local_only,
                "routeStatus": route_status,
                "latencyBudgetMs": _bounded_int(
                    payload.get("latencyBudgetMs"),
                    default=_bounded_int(
                        active_settings.get("latencyBudgetMs"),
                        default=120_000,
                        minimum=100,
                        maximum=300_000,
                    ),
                    minimum=100,
                    maximum=300_000,
                ),
                "stored": False,
                "noStore": False,
                "privacyAssessment": privacy_assessment,
                "storageReceipt": storage_receipt(
                    privacy_assessment,
                    stored=False,
                    outcome="no_write",
                    reason="active_rag_preview_is_read_only",
                ),
                **preview,
            },
            include_text=self._include_raw_text(),
        )

    def predictor_latency(self, payload: dict[str, Any]) -> dict[str, object]:
        log_path = Path(_string(payload.get("log")) or latency_log_path_from_env())
        return {
            "ok": True,
            **latency_report(log_path, last=_bounded_int(payload.get("last"), default=200, minimum=1, maximum=5000)),
        }

    def predictor_cache_stats(self) -> dict[str, object]:
        status = self._predictor_status(probe_capabilities=True)
        probe = status.get("capabilityProbe") if isinstance(status.get("capabilityProbe"), dict) else {}
        prompt_cache = probe.get("promptCache") if isinstance(probe.get("promptCache"), dict) else {}
        capabilities = status.get("capabilities") if isinstance(status.get("capabilities"), dict) else {}
        return {
            "schemaVersion": "rag-ime.predictor-cache-stats.v1",
            "ok": True,
            "capabilities": capabilities,
            "promptCache": prompt_cache,
            "clearSupported": False,
        }

    def predictor_cache_clear(self, payload: dict[str, Any]) -> dict[str, object]:
        confirm = _string(payload.get("confirmText") or payload.get("confirm"))
        if confirm != "CLEAR PREDICTOR CACHE":
            return {
                "schemaVersion": "rag-ime.predictor-cache-clear.v1",
                "ok": False,
                "cleared": False,
                "error": "confirmText must be CLEAR PREDICTOR CACHE",
            }
        return {
            "schemaVersion": "rag-ime.predictor-cache-clear.v1",
            "ok": True,
            "cleared": False,
            "reason": "current predictor provider does not expose a remote cache clear API",
        }

    def active_rag_preview(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.active_rag_management_preview(payload)

    def active_rag_service_preview(self, payload: dict[str, Any]) -> dict[str, object]:
        started = self.active_rag_start(payload)
        session_id = _string(started.get("sessionId"))
        return self.active_rag_status({"sessionId": session_id}) if session_id else started

    def _active_rag_request_from_payload(self, payload: dict[str, Any]) -> ActiveRagStartRequest:
        privacy_assessment = assess_foreground_write(payload)
        if privacy_assessment["storeAllowed"] is not True:
            return self._active_rag_privacy_blocked_request()
        from .input_task import selection_source, selection_task_policy

        operation = _string(payload.get("operation"))
        target_language = _string(payload.get("targetLanguage"))
        if operation:
            selection_task_policy({"outputContract": {"operation": operation, "targetLanguage": target_language}})
        selected_text = _string(payload.get("selectedText") or payload.get("selected_text"))
        selected_text = selection_source(selected_text) if operation else compact_whitespace(selected_text)
        context = _string(payload.get("context") or payload.get("currentContext"))
        surrounding_before = _string(payload.get("surroundingBefore"))
        surrounding_after = _string(payload.get("surroundingAfter"))
        sensitive_field, secure_input = _active_rag_secure_flags(payload)
        settings = self.settings_store.get_settings()
        active_settings = settings.get("activeRag") if isinstance(settings.get("activeRag"), dict) else {}
        runtime_config = self.runtime_config_snapshot(settings=settings)
        sensitive_guard_enabled = bool(active_settings.get("sensitiveTextGuard", True))
        sensitive_guard_hit = _bool(payload.get("sensitiveTextGuardHit"), default=False) or active_rag_sensitive_text_blocked(
            selected_text,
            context,
            surrounding_before,
            surrounding_after,
            guard_enabled=sensitive_guard_enabled,
        )
        sensitive_blocked = sensitive_field or secure_input or sensitive_guard_hit
        if not selected_text and not sensitive_blocked:
            raise ValueError("selectedText is required for explicit Active RAG")
        evidence_pack = (
            tuple(item for item in payload.get("evidencePack", []) if isinstance(item, dict))
            if isinstance(payload.get("evidencePack"), list)
            else ()
        )
        if payload.get("visualContext"):
            raise ValueError("visualContext is disabled; provide AX windowContext instead")
        window_context = validate_window_context(payload.get("windowContext"))
        window_context = validate_window_context(
            enrich_window_context_with_app_semantics(window_context)
        )
        requested_latency_budget_ms = _bounded_int(
            payload.get("latencyBudgetMs"),
            default=runtime_config.active_rag.latency_budget_ms,
            minimum=100,
            maximum=300_000,
        )
        return ActiveRagStartRequest(
            selected_text=selected_text,
            selected_text_hash=(
                ""
                if sensitive_blocked
                else _string(payload.get("selectedTextHash") or payload.get("selected_text_hash")) or stable_text_hash(selected_text)
            ),
            frontend_revision=_bounded_int(payload.get("frontendRevision"), default=1, minimum=0, maximum=1_000_000_000),
            selection_epoch=_bounded_int(payload.get("selectionEpoch"), default=1, minimum=0, maximum=1_000_000_000),
            panel_session_id=_string(payload.get("panelSessionId")),
            front_app_bundle_id=_string(payload.get("frontAppBundleId")),
            surrounding_before=surrounding_before,
            surrounding_after=surrounding_after,
            intent=_string(payload.get("intent")) or "rewrite",
            operation=operation,
            target_language=target_language,
            placement=_string(payload.get("placement")) or "replace_selection",
            context=context,
            context_source=_string(payload.get("contextSource")),
            frontend_context_hash=_string(payload.get("contextHash")),
            frontend_context_chars=_bounded_int(
                payload.get("contextChars"), default=0, minimum=0, maximum=1_000_000
            ),
            frontend_selected_text_chars=_bounded_int(
                payload.get("selectedTextChars"), default=0, minimum=0, maximum=1_000_000
            ),
            evidence_pack=evidence_pack,
            project=_string(payload.get("project")) or self.config.project,
            app=_string(payload.get("app")),
            max_candidates=_bounded_int(payload.get("maxCandidates"), default=1, minimum=1, maximum=10),
            max_chars=_bounded_int(
                payload.get("maxChars"),
                default=ACTIVE_RAG_DEFAULT_MAX_CHARS,
                minimum=0,
                maximum=12000,
            ),
            latency_budget_ms=_bounded_int(
                min(
                    requested_latency_budget_ms,
                    runtime_config.active_rag.latency_budget_ms,
                ),
                default=runtime_config.active_rag.latency_budget_ms,
                minimum=100,
                maximum=runtime_config.active_rag.latency_budget_ms,
            ),
            remote_model_allowed=(
                _bool(payload.get("remoteModelAllowed"), default=False)
                if "remoteModelAllowed" in payload
                else None
            ),
            remote_model_skip_reason=_string(payload.get("remoteModelSkipReason")),
            remote_model_gates=(
                {str(key): bool(value) for key, value in payload.get("remoteModelGates", {}).items()}
                if isinstance(payload.get("remoteModelGates"), dict)
                else {}
            ),
            sensitive_field=sensitive_field,
            secure_input=secure_input,
            sensitive_text_guard_enabled=sensitive_guard_enabled,
            sensitive_text_guard_hit=sensitive_guard_hit,
            local_retrieval_allowed=runtime_config.hybrid_rag.enabled and runtime_config.memory.enabled,
            local_retrieval_skip_reason=(
                "memory_disabled"
                if not runtime_config.memory.enabled
                else ("hybrid_rag_disabled" if not runtime_config.hybrid_rag.enabled else "")
            ),
            rag_enabled_lanes=runtime_config.hybrid_rag.query_lanes(),
            rag_lane_weights=runtime_config.hybrid_rag.query_weights(),
            window_context=window_context,
        )

    @staticmethod
    def _active_rag_privacy_blocked_request() -> ActiveRagStartRequest:
        return ActiveRagStartRequest(
            selected_text="",
            selected_text_hash="",
            frontend_revision=0,
            selection_epoch=0,
            remote_model_allowed=False,
            sensitive_field=True,
            secure_input=True,
            local_retrieval_allowed=False,
            local_retrieval_skip_reason=SENSITIVE_FIELD_BLOCK_REASON,
        )

    def _active_rag_privacy_blocked_response(
        self,
        privacy_assessment: dict[str, object],
        *,
        preview: bool,
    ) -> dict[str, object]:
        request = self._active_rag_privacy_blocked_request()
        response = (
            self.active_rag.preview(request, local_only=True)
            if preview
            else self.active_rag.start(request)
        )
        return {
            **response,
            "stored": False,
            "noStore": True,
            "privacyAssessment": privacy_assessment,
            "storageReceipt": storage_receipt(privacy_assessment, stored=False),
        }

    def _active_rag_error_payload(self, error: str) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.active-rag-service.v1",
            "ok": False,
            "error": error,
        }

    def active_rag_start(self, payload: dict[str, Any]) -> dict[str, object]:
        privacy_assessment = assess_foreground_write(payload)
        if privacy_assessment["storeAllowed"] is not True:
            return self._active_rag_privacy_blocked_response(
                privacy_assessment,
                preview=False,
            )
        if not isinstance(self.core, LocalSqliteCoreClient):
            return self._active_rag_error_payload("Active RAG requires local SQLite core")
        runtime_config = self.runtime_config_snapshot()
        if not runtime_config.active_rag.enabled:
            return self._active_rag_error_payload("Active RAG disabled by management settings")
        # The foreground Ctrl+. request is explicit. localOnlyDefault only controls
        # management previews; product start follows the two remote privacy opt-ins.
        local_only = _bool(payload.get("localOnly"), default=False)
        route_status = self.active_rag_route_status(local_only=local_only)
        route_gates = route_status.get("gates") if isinstance(route_status.get("gates"), dict) else {}
        settings_allow_remote = all(
            bool(route_gates.get(key))
            for key in ("featureEnabled", "notLocalOnly", "allowRemoteModel", "privacyOptIn")
        )
        try:
            request = self._active_rag_request_from_payload(
                {
                    **payload,
                    "remoteModelAllowed": settings_allow_remote,
                    "remoteModelSkipReason": _string(route_status.get("skipReason")),
                    "remoteModelGates": dict(route_gates),
                }
            )
            return {
                **self.active_rag.start(request),
                "routeStatus": route_status,
                "stored": False,
                "noStore": False,
                "privacyAssessment": privacy_assessment,
                "storageReceipt": storage_receipt(
                    privacy_assessment,
                    stored=False,
                    outcome="no_write",
                    reason="active_rag_session_is_not_typing_history",
                ),
            }
        except ValueError as exc:
            return self._active_rag_error_payload(str(exc))

    def active_rag_status(self, payload: dict[str, Any]) -> dict[str, object]:
        session_id = _string(payload.get("sessionId") or payload.get("id"))
        if not session_id:
            return {"schemaVersion": "rag-ime.active-rag-service.v1", "status": "missing", "error": "sessionId is required"}
        return self.active_rag.status(session_id)

    def active_rag_diagnostics(self, payload: dict[str, Any]) -> dict[str, object]:
        session_id = _string(payload.get("sessionId") or payload.get("id"))
        if not session_id:
            return {
                "schemaVersion": "rag-ime.active-rag-diagnostics.v1",
                "ok": False,
                "status": "missing",
                "error": "sessionId is required",
            }
        return self.active_rag.diagnostics(session_id)

    def active_rag_traces(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.active_rag.trace_records(
            limit=_bounded_int(payload.get("limit"), default=50, minimum=1, maximum=500),
            session_id=_string(payload.get("sessionId") or payload.get("id")),
        )

    def active_rag_cancel(self, payload: dict[str, Any]) -> dict[str, object]:
        session_id = _string(payload.get("sessionId") or payload.get("id"))
        if not session_id:
            return {"schemaVersion": "rag-ime.active-rag-service.v1", "status": "missing", "error": "sessionId is required"}
        return self.active_rag.cancel(session_id)

    def agent_surface_complete(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.agent_surface.complete(payload)

    def agent_surface_refine_voice(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.agent_surface.refine_voice(payload)

    def agent_surface_cancel(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.agent_surface.cancel(payload)

    def active_rag_accept(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.active_rag.accept(
            session_id=_string(payload.get("sessionId") or payload.get("id")),
            candidate_id=_string(payload.get("candidateId")),
            selected_text_hash=_string(payload.get("selectedTextHash")),
            frontend_revision=_bounded_int(payload.get("frontendRevision"), default=0, minimum=0, maximum=1_000_000_000),
            selection_epoch=_bounded_int(payload.get("selectionEpoch"), default=0, minimum=0, maximum=1_000_000_000),
            panel_session_id=_string(payload.get("panelSessionId")),
            front_app_bundle_id=_string(payload.get("frontAppBundleId")),
        )

    def predictor_benchmark(self, payload: dict[str, Any]) -> dict[str, object]:
        cases_path = Path(_string(payload.get("cases")) or "eval/predictor_latency_cases.jsonl")
        cases = load_predictor_latency_cases(cases_path)
        return benchmark_predictor_latency(
            self.predictor,
            cases,
            profile=_string(payload.get("profile")) or "qwen3_06b_ime_hot",
            repeat=_bounded_int(payload.get("repeat"), default=3, minimum=1, maximum=100),
            max_candidates=_bounded_int(payload.get("maxCandidates"), default=3, minimum=1, maximum=10),
        )

    def rebuild_vector_index(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.vector-rebuild.v1",
                "ok": False,
                "reason": "vector rebuild is only available for local SQLite core",
            }
        project = _string(payload.get("project")) or self.config.project
        limit = _bounded_int(payload.get("limit"), default=0, minimum=0, maximum=200_000)
        report = self.core.rebuild_vector_index(project=project, limit=limit)
        self._vector_auto_rebuild_report = {
            "trigger": "manual",
            "project": project,
            "limit": limit,
            **report,
        }
        return {
            "schemaVersion": "rag-ime.vector-rebuild.v1",
            "ok": bool(report.get("enabled")),
            "project": project,
            "limit": limit,
            **report,
            "vectorStats": self._vector_index_stats(),
        }

    def memory_history(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.memory-history.v1",
                "ok": False,
                "error": "memory history is only available for local SQLite core",
                "items": [],
            }
        report = self.core.list_memory_events(
            project=_string(payload.get("project")) or self.config.project,
            query=_string(payload.get("query")),
            source=_string(payload.get("source")),
            include_deleted=_bool(payload.get("includeDeleted"), default=False),
            generated_only=_bool(payload.get("generatedOnly"), default=False),
            limit=_bounded_int(payload.get("limit"), default=80, minimum=1, maximum=500),
        )
        return {"ok": True, **report}

    def memory_optimizer_trace(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.memory-optimizer-trace.v1",
                "ok": False,
                "error": "optimizer trace is only available for local SQLite core",
            }
        trace_id = _string(payload.get("traceId"))
        if not trace_id:
            return {
                "schemaVersion": "rag-ime.memory-optimizer-trace.v1",
                "ok": False,
                "error": "traceId is required",
            }
        trace = self.core.get_memory_optimizer_trace(trace_id)
        if trace is None:
            return {
                "schemaVersion": "rag-ime.memory-optimizer-trace.v1",
                "ok": False,
                "error": "trace not found",
                "traceId": trace_id,
            }
        return {
            "schemaVersion": "rag-ime.memory-optimizer-trace.v1",
            "ok": True,
            **trace,
        }

    def memory_candidate_explain(self, payload: dict[str, Any]) -> dict[str, object]:
        candidate_id = _string(payload.get("candidateId"))
        if not candidate_id:
            return {
                "schemaVersion": "rag-ime.memory-candidate-explain.v1",
                "ok": False,
                "error": "candidateId is required",
            }
        explainer = getattr(self.core, "explain_memory_candidate", None)
        if not callable(explainer):
            return {
                "schemaVersion": "rag-ime.memory-candidate-explain.v1",
                "ok": False,
                "error": "core does not support candidate explanation",
            }
        explanation = explainer(candidate_id, context_hash=_string(payload.get("contextHash")) or None)
        if explanation is None:
            return {
                "schemaVersion": "rag-ime.memory-candidate-explain.v1",
                "ok": False,
                "error": "candidate not found",
                "candidateId": candidate_id,
            }
        return {
            "schemaVersion": "rag-ime.memory-candidate-explain.v1",
            "ok": True,
            **explanation,
        }

    def memory_governance(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.memory-governance.v1",
                "ok": False,
                "error": "memory governance is only available for local SQLite core",
            }
        report = self.core.inspect_memory_governance(
            limit=_bounded_int(payload.get("limit"), default=20, minimum=1, maximum=200),
            include_inactive=_bool(payload.get("includeInactive"), default=False),
        )
        return {"ok": True, **report}

    def memory_cleanup_runs(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.memory-cleanup-runs.v1",
                "ok": False,
                "error": "cleanup runs are only available for local SQLite core",
            }
        review_status = _cleanup_review_status(payload)
        if review_status:
            run_id = _string(payload.get("runId"))
            if not run_id:
                return {
                    "schemaVersion": "rag-ime.memory-cleanup-runs.v1",
                    "ok": False,
                    "error": "runId is required for cleanup review",
                }
            report = self.core.review_memory_cleanup_plan(
                run_id=run_id,
                status=review_status,
                diff_ids=_int_list(payload.get("diffIds")),
                diff_indexes=_int_list(payload.get("diffIndexes")),
            )
            return {
                "schemaVersion": "rag-ime.memory-cleanup-runs.v1",
                "ok": True,
                **report,
            }
        report = self.core.list_memory_cleanup_runs(
            limit=_bounded_int(payload.get("limit"), default=20, minimum=1, maximum=100),
            run_id=_string(payload.get("runId")),
            status=_string(payload.get("status")),
        )
        return {"ok": True, **report}

    def knowledge_workbench_route_status(self) -> dict[str, object]:
        active_route = self.active_rag_route_status(local_only=False)
        workbench_route = self.knowledge_workbench.route_status()
        return {
            **workbench_route,
            "deepseekReady": bool(workbench_route.get("deepseekReady") and active_route.get("remoteReady")),
            "deepseekRoute": active_route,
        }

    def knowledge_workbench_start(self, payload: dict[str, Any]) -> dict[str, object]:
        mode = _string(payload.get("mode")).lower() or "knowledge_answer"
        question = _string(payload.get("question") or payload.get("query"))
        context = _string(payload.get("context"))
        sensitive_field, secure_input = _active_rag_secure_flags(payload)
        settings = self.settings_store.get_settings()
        active_settings = settings.get("activeRag") if isinstance(settings.get("activeRag"), dict) else {}
        sensitive_guard_enabled = bool(active_settings.get("sensitiveTextGuard", True))
        if sensitive_field or secure_input or active_rag_sensitive_text_blocked(
            question,
            context,
            guard_enabled=sensitive_guard_enabled,
        ):
            return {
                "schemaVersion": "rag-ime.knowledge-workbench.v1",
                "ok": False,
                "status": "blocked",
                "error": SENSITIVE_FIELD_BLOCK_REASON,
                "retrieval": {"called": False},
                "remoteModel": {"requested": False},
            }
        route = self.knowledge_workbench_route_status()
        if not route.get("deepseekReady"):
            deepseek_route = route.get("deepseekRoute") if isinstance(route.get("deepseekRoute"), dict) else {}
            return {
                "schemaVersion": "rag-ime.knowledge-workbench.v1",
                "ok": False,
                "status": "blocked",
                "error": f"DeepSeek knowledge route blocked: {_string(deepseek_route.get('skipReason')) or 'not_configured'}",
                "routeStatus": route,
            }
        request = KnowledgeWorkbenchRequest(
            question=question,
            mode=mode,
            context=context,
            project=_string(payload.get("project")) or self.config.project,
            app=_string(payload.get("app")) or "com.rag-ime.control",
            include_notion=_bool(payload.get("includeNotion"), default=False),
            generation=_bounded_int(payload.get("generation"), default=1, minimum=0, maximum=1_000_000_000),
            context_hash=_string(payload.get("contextHash")),
            client_id=_string(payload.get("clientId")) or "native-control-center",
            max_chars=_bounded_int(payload.get("maxChars"), default=0, minimum=0, maximum=8000),
            latency_budget_ms=_bounded_int(
                payload.get("latencyBudgetMs"),
                default=120_000,
                minimum=1_000,
                maximum=300_000,
            ),
            curation_scope=_string(payload.get("scope")).lower() or "incremental",
            curation_policy=_string(payload.get("policy")).lower() or "conservative",
        )
        try:
            return {**self.knowledge_workbench.start(request), "routeStatus": route}
        except ValueError as exc:
            return {
                "schemaVersion": "rag-ime.knowledge-workbench.v1",
                "ok": False,
                "status": "error",
                "error": str(exc),
                "routeStatus": route,
            }

    def knowledge_workbench_status(self, payload: dict[str, Any]) -> dict[str, object]:
        session_id = _string(payload.get("sessionId") or payload.get("id"))
        if not session_id:
            return {
                "schemaVersion": "rag-ime.knowledge-workbench.v1",
                "ok": False,
                "status": "missing",
                "error": "sessionId is required",
            }
        return self.knowledge_workbench.status(session_id)

    def knowledge_workbench_cancel(self, payload: dict[str, Any]) -> dict[str, object]:
        return self.knowledge_workbench.cancel(_string(payload.get("sessionId") or payload.get("id")))

    def knowledge_workbench_database_apply(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {"schemaVersion": "rag-ime.knowledge-database-action.v1", "ok": False, "error": "local SQLite core required"}
        run_id = _string(payload.get("runId"))
        if _string(payload.get("confirm")) != "apply":
            return {
                "schemaVersion": "rag-ime.knowledge-database-action.v1",
                "ok": False,
                "error": 'confirmation required: set confirm="apply"',
                "requiredConfirm": "apply",
            }
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            _require_expected_memory_run_owner(conn, run_id=run_id, payload=payload)
            run = apply_stored_memory_book_run(conn, run_id=run_id)
            retrieval = rebuild_retrieval_docs(conn, project=self.config.project)
        self._clear_rime_cache()
        return {
            "schemaVersion": "rag-ime.knowledge-database-action.v1",
            "ok": True,
            "action": "apply",
            "run": run,
            "retrieval": retrieval,
        }

    def knowledge_workbench_database_apply_preview(
        self,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        current = {"runtimeRevision": self.management.revision().runtime_revision}
        try:
            _require_management_fields(
                payload,
                required={"runId"},
                optional={"expectedRuntimeRevision"},
            )
            if not isinstance(self.core, LocalSqliteCoreClient):
                raise ManagementWorkError("unsupported_backend", "Local SQLite core is required.")
            run_id = _string(payload.get("runId"))
            if not run_id:
                raise ManagementWorkError("invalid_request", "runId is required.")
            if "expectedRuntimeRevision" in payload:
                expected_runtime = _strict_management_revision(payload.get("expectedRuntimeRevision"))
                if expected_runtime != current["runtimeRevision"]:
                    raise ManagementWorkError(
                        "revision_mismatch",
                        "The knowledge workbench revision is stale.",
                        current_revision=current,
                    )
            with self.core._connect() as conn:  # type: ignore[attr-defined]
                state = _knowledge_database_contract_state(
                    conn,
                    run_id=run_id,
                    project=self.config.project,
                )
            if not state["canApply"]:
                raise ManagementWorkError(
                    "domain_not_applicable",
                    str(state["applyBlockedReason"]),
                    current_revision={
                        **current,
                        "subjectRevision": state["revisionHash"],
                    },
                )
            expected_revision = {
                **current,
                "subjectRevision": state["revisionHash"],
            }
            return self.management.work_contract.create_preview(
                path_id="knowledge.database.apply",
                payload={"runId": run_id},
                expected_revision=expected_revision,
                required_confirm="apply",
                summary={
                    "title": (
                        "应用记忆整理草案"
                        if state["pendingCount"]
                        else "确认排除本批记忆建议"
                    ),
                    "items": [
                        f"运行: {run_id}",
                        (
                            f"待应用变更: {state['pendingCount']}"
                            if state["pendingCount"]
                            else "正式记忆不会发生变化"
                        ),
                        _string(state.get("summary"))[:160],
                    ],
                    "risk": "R2",
                },
            )
        except Exception as exc:
            return self.management.work_contract.error_payload(exc, current_revision=current)

    def knowledge_workbench_database_apply_contract(
        self,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        current = {"runtimeRevision": self.management.revision().runtime_revision}
        try:
            _require_management_fields(
                payload,
                required={
                    "runId",
                    "confirm",
                    "previewToken",
                    "payloadSha256",
                    "expectedRuntimeRevision",
                },
                optional=set(),
            )
            if not isinstance(self.core, LocalSqliteCoreClient):
                raise ManagementWorkError("unsupported_backend", "Local SQLite core is required.")
            run_id = _string(payload.get("runId"))
            expected_runtime = _strict_management_revision(payload.get("expectedRuntimeRevision"))
            if expected_runtime != current["runtimeRevision"]:
                raise ManagementWorkError(
                    "revision_mismatch",
                    "The knowledge apply request revision is stale.",
                    current_revision=current,
                )

            def current_revision(conn: sqlite3.Connection) -> dict[str, object]:
                state = _knowledge_database_contract_state(
                    conn,
                    run_id=run_id,
                    project=self.config.project,
                )
                return self.management.management_work_revision(
                    conn,
                    subject_revision=str(state["revisionHash"]),
                )

            def execute(conn: sqlite3.Connection) -> WorkExecution:
                state = _knowledge_database_contract_state(
                    conn,
                    run_id=run_id,
                    project=self.config.project,
                )
                if not state["canApply"]:
                    raise ManagementWorkError(
                        "domain_not_applicable",
                        str(state["applyBlockedReason"]),
                    )
                run = apply_stored_memory_book_run(conn, run_id=run_id)
                retrieval = rebuild_retrieval_docs(conn, project=self.config.project)
                after_state = _knowledge_database_contract_state(
                    conn,
                    run_id=run_id,
                    project=self.config.project,
                )
                result = {
                    "schemaVersion": "rag-ime.knowledge-database-action.v1",
                    "ok": True,
                    "action": "apply",
                    "run": run,
                    "retrieval": retrieval,
                }
                return WorkExecution(
                    result=result,
                    audit_action="knowledge_database_apply",
                    target_type="memory_book_run",
                    target_id=run_id,
                    rollback_available=bool(after_state["canRollback"]),
                    rollback_path_id="knowledge.database.rollback",
                    rollback_confirm="rollback",
                    rollback_authority={"runId": run_id},
                    rollback_data={
                        "runId": run_id,
                        "afterRevision": after_state["revisionHash"],
                    },
                )

            response = self.management.work_contract.execute_apply(
                path_id="knowledge.database.apply",
                payload={"runId": run_id},
                preview_token=_string(payload.get("previewToken")),
                payload_sha256=_string(payload.get("payloadSha256")),
                confirm_text=_string(payload.get("confirm")),
                current_revision=current_revision,
                executor=execute,
            )
            self._clear_rime_cache()
            self.management.events.publish(
                "knowledge_database_changed",
                {"runId": run_id, "action": "apply", "receiptId": response.get("receiptId")},
            )
            self.agent.observations.emit_memory_event(
                phase="applied",
                status="completed",
                summary="已批准的记忆整理草案已应用",
                run_id=run_id,
                refs=[
                    {
                        "kind": "receipt",
                        "id": _string(response.get("receiptId")),
                        "label": "应用回执",
                    }
                ],
            )
            return response
        except Exception as exc:
            return self.management.work_contract.error_payload(exc, current_revision=current)

    def knowledge_workbench_database_draft_edit(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {"schemaVersion": "rag-ime.knowledge-database-action.v1", "ok": False, "error": "local SQLite core required"}
        run_id = _string(payload.get("runId"))
        diff_id = _bounded_int(payload.get("diffId"), default=0, minimum=1, maximum=2_147_483_647)
        raw_payload = payload.get("payload")
        if raw_payload is not None and not isinstance(raw_payload, dict):
            raise ValueError("draft payload must be an object")
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            run = update_stored_memory_book_diff(
                conn,
                run_id=run_id,
                diff_id=diff_id,
                payload=dict(raw_payload) if isinstance(raw_payload, dict) else None,
                selected=_bool(payload.get("selected"), default=True),
            )
        return {
            "schemaVersion": "rag-ime.knowledge-database-action.v1",
            "ok": True,
            "action": "draft_edit",
            "run": run,
        }

    def knowledge_workbench_database_rollback(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {"schemaVersion": "rag-ime.knowledge-database-action.v1", "ok": False, "error": "local SQLite core required"}
        run_id = _string(payload.get("runId"))
        if _string(payload.get("confirm")) != "rollback":
            return {
                "schemaVersion": "rag-ime.knowledge-database-action.v1",
                "ok": False,
                "error": 'confirmation required: set confirm="rollback"',
                "requiredConfirm": "rollback",
            }
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            _require_expected_memory_run_owner(conn, run_id=run_id, payload=payload)
            run = rollback_memory_book_run(conn, run_id=run_id)
            retrieval = rebuild_retrieval_docs(conn, project=self.config.project)
        self._clear_rime_cache()
        return {
            "schemaVersion": "rag-ime.knowledge-database-action.v1",
            "ok": True,
            "action": "rollback",
            "run": run,
            "retrieval": retrieval,
        }

    def knowledge_workbench_database_rollback_contract(
        self,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        current = {"runtimeRevision": self.management.revision().runtime_revision}
        try:
            _require_management_fields(
                payload,
                required={
                    "runId",
                    "confirm",
                    "receiptId",
                    "rollbackToken",
                    "payloadSha256",
                },
                optional=set(),
            )
            if not isinstance(self.core, LocalSqliteCoreClient):
                raise ManagementWorkError("unsupported_backend", "Local SQLite core is required.")
            run_id = _string(payload.get("runId"))

            def execute(conn: sqlite3.Connection, receipt: StoredReceipt) -> WorkExecution:
                authority = dict(receipt.rollback_authority)
                if authority.get("runId") != run_id:
                    raise ManagementWorkError(
                        "rollback_authority_mismatch",
                        "The knowledge run is not owned by this receipt.",
                    )
                state = _knowledge_database_contract_state(
                    conn,
                    run_id=run_id,
                    project=self.config.project,
                )
                if state["revisionHash"] != receipt.rollback_data.get("afterRevision"):
                    raise ManagementWorkError(
                        "rollback_state_changed",
                        "The knowledge run changed after the apply receipt was issued.",
                        current_revision={
                            **current,
                            "subjectRevision": state["revisionHash"],
                        },
                    )
                if not state["canRollback"]:
                    raise ManagementWorkError(
                        "rollback_unavailable",
                        str(state["rollbackBlockedReason"]),
                    )
                run = rollback_memory_book_run(conn, run_id=run_id)
                retrieval = rebuild_retrieval_docs(conn, project=self.config.project)
                result = {
                    "schemaVersion": "rag-ime.knowledge-database-action.v1",
                    "ok": True,
                    "action": "rollback",
                    "run": run,
                    "retrieval": retrieval,
                }
                return WorkExecution(
                    result=result,
                    audit_action="knowledge_database_rollback",
                    target_type="memory_book_run",
                    target_id=run_id,
                )

            response = self.management.work_contract.execute_rollback(
                path_id="knowledge.database.rollback",
                receipt_id=_string(payload.get("receiptId")),
                rollback_token=_string(payload.get("rollbackToken")),
                payload_sha256=_string(payload.get("payloadSha256")),
                confirm_text=_string(payload.get("confirm")),
                expected_apply_path_id="knowledge.database.apply",
                executor=execute,
            )
            self._clear_rime_cache()
            self.management.events.publish(
                "knowledge_database_changed",
                {"runId": run_id, "action": "rollback", "receiptId": response.get("receiptId")},
            )
            self.agent.observations.emit_memory_event(
                phase="rolled_back",
                status="cancelled",
                summary="记忆整理应用已回滚",
                run_id=run_id,
                refs=[
                    {
                        "kind": "receipt",
                        "id": _string(response.get("receiptId")),
                        "label": "回滚回执",
                    }
                ],
            )
            return response
        except Exception as exc:
            return self.management.work_contract.error_payload(exc, current_revision=current)

    def _knowledge_workbench_evidence(
        self,
        request: KnowledgeWorkbenchRequest,
    ) -> tuple[dict[str, object], ...]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return ()
        runtime_config = self.runtime_config_snapshot()
        if not runtime_config.hybrid_rag.enabled or not runtime_config.memory.enabled:
            return ()
        temporal_query = parse_temporal_query(request.question)
        if temporal_query.matched:
            return self._temporal_knowledge_evidence(request, temporal_query)
        # The native workbench is a global knowledge surface rather than the
        # app that originally produced a memory. Keeping com.rag-ime.control
        # here would hide memories captured in Codex, TextEdit, terminals, and
        # browsers before ranking even starts.
        retrieval_app = "" if request.app.startswith("com.rag-ime.control") else request.app
        query = HybridRagQuery(
            query_text=request.question,
            raw_input=request.question,
            committed_tail=request.context,
            project=request.project,
            app=retrieval_app,
            top_k=12,
            latency_budget_ms=800,
            enabled_lanes=runtime_config.hybrid_rag.query_lanes(),
            lane_weights=runtime_config.hybrid_rag.query_weights(),
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            payload = retrieve_hybrid_rag_candidates(conn, query, self.core.embedding_provider)
        lane_weights = dict(query.lane_weights)
        fused_ranks: dict[str, int] = {}
        for index, item in enumerate(payload.get("candidates", []), start=1):
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            doc_id = _string(metadata.get("docId"))
            if doc_id and doc_id not in fused_ranks:
                fused_ranks[doc_id] = index
        combined: dict[str, dict[str, object]] = {}
        for item in payload.get("hits", []):
            if not isinstance(item, dict):
                continue
            doc_id = _string(item.get("doc_id") or item.get("docId"))
            source_id = _string(item.get("source_id") or item.get("sourceId")) or doc_id
            if not source_id:
                continue
            text = compact_whitespace(_string(item.get("text")))
            metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
            current = combined.get(source_id)
            lane = _string(item.get("source_lane") or item.get("sourceLane")) or "local"
            rank = max(1, int(item.get("rank") or 1))
            # SQLite BM25 values and cosine values are different units. Use
            # weighted reciprocal rank here, just like the foreground fusion,
            # so either score scale cannot drown out the other evidence.
            lane_score = float(lane_weights.get(lane, 1.0)) / (60.0 + rank)
            if current is None:
                combined[source_id] = {
                    "sourceId": source_id,
                    "docId": doc_id,
                    "docType": _string(item.get("doc_type") or item.get("docType")),
                    "sourceLane": lane,
                    "lanes": [lane],
                    "title": _string(metadata.get("bookTitle")) or truncate_text(text, 60),
                    "text": text,
                    "tags": list(item.get("tags") or []),
                    "score": lane_score,
                    "rank": rank,
                    "fusedRank": fused_ranks.get(doc_id, 0),
                    "metadata": metadata,
                }
                continue
            lanes = list(current.get("lanes") or [])
            if lane not in lanes:
                lanes.append(lane)
            current["lanes"] = lanes
            current["score"] = float(current.get("score") or 0.0) + lane_score
            current["rank"] = min(int(current.get("rank") or rank), rank)
            if len(text) > len(_string(current.get("text"))):
                current["text"] = text
                current["title"] = _string(metadata.get("bookTitle")) or truncate_text(text, 60)
        for item in combined.values():
            fused_rank = int(item.get("fusedRank") or 0)
            if fused_rank > 0:
                item["score"] = float(item.get("score") or 0.0) + 0.01 / fused_rank
        ranked = sorted(
            combined.values(),
            key=lambda item: (float(item.get("score") or 0.0), -int(item.get("rank") or 0)),
            reverse=True,
        )
        # A long-form knowledge answer needs the organized Memory Book/Atom
        # contract, not twelve near-duplicate raw events. Reserve one relevant
        # book and one atom when either ranked in the top twelve of a lane, then
        # fill the remaining evidence slots by fused score.
        selected: list[dict[str, object]] = []
        for doc_type in ("book", "atom"):
            structured = next(
                (
                    item
                    for item in ranked
                    if _string(item.get("docType")) == doc_type
                    and int(item.get("rank") or 0) in range(1, 13)
                ),
                None,
            )
            if structured is not None:
                selected.append(structured)
        selected_ids = {_string(item.get("sourceId")) for item in selected}
        for item in ranked:
            if _string(item.get("sourceId")) in selected_ids:
                continue
            selected.append(item)
            if len(selected) >= 12:
                break
        return tuple(self._annotate_knowledge_evidence_times(selected[:12]))

    def _temporal_knowledge_evidence(
        self,
        request: KnowledgeWorkbenchRequest,
        temporal_query: TemporalQuery,
    ) -> tuple[dict[str, object], ...]:
        range_clauses = " OR ".join("(e.created_at_ms >= ? AND e.created_at_ms < ?)" for _ in temporal_query.ranges)
        range_params = [value for item in temporal_query.ranges for value in (item.start_ms, item.end_ms)]
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                f"""
                SELECT e.id, e.created_at_ms, e.source, e.committed_text,
                       e.app, e.project, e.tags_json
                FROM input_events e
                JOIN memory_state s ON s.event_id = e.id
                WHERE s.deleted = 0
                  AND ({range_clauses})
                  AND (? = '' OR e.project = ? OR e.project = '')
                ORDER BY e.created_at_ms DESC, e.id DESC
                LIMIT 1200
                """,
                (*range_params, request.project, request.project),
            ).fetchall()

        candidates: list[dict[str, object]] = []
        seen_text: set[str] = set()
        query_terms = set(re.sub(r"[^\w\u4e00-\u9fff]+", "", temporal_query.cleaned_query).lower())
        for row in rows:
            text = compact_whitespace(str(row["committed_text"] or ""))
            normalized = re.sub(r"[\W_]+", "", text).lower()
            if len(normalized) < 6 or normalized in seen_text:
                continue
            seen_text.add(normalized)
            timestamp_ms = int(row["created_at_ms"] or 0)
            local_time = datetime.fromtimestamp(timestamp_ms / 1000).astimezone().strftime("%H:%M")
            matching_range = next(
                (item for item in temporal_query.ranges if item.start_ms <= timestamp_ms < item.end_ms),
                temporal_query.ranges[0],
            )
            try:
                tags = list(json.loads(str(row["tags_json"] or "[]")))
            except (TypeError, ValueError, json.JSONDecodeError):
                tags = []
            candidates.append(
                {
                    "sourceId": f"event:{int(row['id'])}",
                    "docId": f"event:{int(row['id'])}",
                    "docType": "event",
                    "sourceLane": "temporal_timeline",
                    "lanes": ["temporal_timeline"],
                    "title": f"{matching_range.label} {local_time}",
                    "text": text,
                    "tags": tags[:8],
                    "score": (min(len(text), 240) / 240.0) + (len(query_terms & set(normalized)) / max(1, len(query_terms))),
                    "rank": len(candidates) + 1,
                    "fusedRank": len(candidates) + 1,
                    "sourceCreatedAtMs": timestamp_ms,
                    "app": str(row["app"] or ""),
                    "project": str(row["project"] or ""),
                    "source": str(row["source"] or ""),
                }
            )

        # Like VCP's Time path, rank only inside the explicit range. Semantic
        # terms may reorder the time lane, but cannot leak an old memory into it.
        selected = sorted(candidates, key=lambda item: (float(item["score"]), int(item["sourceCreatedAtMs"])), reverse=True)[:12]
        selected.sort(key=lambda item: int(item["sourceCreatedAtMs"]))
        return tuple(selected)

    def _annotate_knowledge_evidence_times(
        self,
        evidence: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        event_ids: set[int] = set()
        item_event_ids: dict[int, list[int]] = {}
        for index, item in enumerate(evidence):
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            raw_ids = list(metadata.get("sourceEventIds") or [])
            raw_single_id = str(metadata.get("sourceEventId") or "")
            single_id = int(raw_single_id) if raw_single_id.isdigit() else 0
            ids = [int(value) for value in raw_ids if str(value).isdigit() and int(value) > 0]
            if single_id > 0:
                ids.append(single_id)
            item_event_ids[index] = ids
            event_ids.update(ids)
        if not event_ids:
            return evidence
        placeholders = ",".join("?" for _ in event_ids)
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                f"SELECT id, created_at_ms FROM input_events WHERE id IN ({placeholders})",
                tuple(sorted(event_ids)),
            ).fetchall()
        timestamps = {int(row["id"]): int(row["created_at_ms"] or 0) for row in rows}
        for index, item in enumerate(evidence):
            matched = [timestamps[event_id] for event_id in item_event_ids[index] if event_id in timestamps]
            if matched:
                item["sourceCreatedAtMs"] = max(matched)
        return evidence

    def _knowledge_workbench_database_organizer(
        self,
        request: KnowledgeWorkbenchRequest,
        *,
        source_bundle: Mapping[str, object] | None = None,
        catalog_model_run_id: str = "",
    ) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            raise ValueError("database organization requires local SQLite core")
        scope = request.curation_scope
        policy = request.curation_policy
        provided_bundle = (
            dict(source_bundle)
            if isinstance(source_bundle, Mapping)
            else None
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            if provided_bundle is None:
                if scope == "global":
                    conn.execute("BEGIN")
                bundle = build_memory_book_source_bundle(
                    conn,
                    project=request.project,
                    since_days=7 if scope == "global" else 30,
                    limit=500 if scope == "global" else 48,
                    after_event_id=0 if scope == "global" else None,
                    newest_first=scope == "global",
                    curation_scope=scope,
                    catalog_only=scope == "global",
                )
            else:
                bundle = provided_bundle
            if _string(bundle.get("project")) != request.project:
                raise ValueError("Memory catalog bundle project does not match request")
            if scope == "global" and (
                _string(bundle.get("curationScope")) != "global"
                or not _bool(bundle.get("catalogAudit"))
                or not _bool(bundle.get("catalogComplete"))
            ):
                raise ValueError("global Memory catalog bundle is incomplete")
            existing = find_memory_book_draft_for_bundle(
                conn,
                project=request.project,
                bundle_hash=str(bundle.get("bundleHash") or ""),
            )
        managed = MemoryMaintenanceSettings.load(
            self.core.db_path,
            preverified_schema=True,
        )
        if existing is not None:
            plan = memory_book_plan_from_stored_run(existing)
            validation = inspect_memory_book_plan(plan)
            auto_applied = False
            stored_run = existing
            existing_diff_count = int(existing.get("diffCount") or 0) or len(
                existing.get("diffs") or []
            )
            if (
                managed.automatic_organization_auto_apply
                and validation.get("ok")
                and str(existing.get("status") or "") == "draft"
                and existing_diff_count > 0
            ):
                with self.core._connect() as conn:  # type: ignore[attr-defined]
                    stored_run = apply_stored_memory_book_run(
                        conn,
                        run_id=_string(existing.get("runId") or existing.get("run_id")),
                    )
                auto_applied = str(stored_run.get("status") or "") in {"applied", "partial"}
            if scope == "global" and validation.get("ok") and not _string(
                stored_run.get("sealedCatalogDigest")
            ):
                with self.core._connect() as conn:  # type: ignore[attr-defined]
                    sealed_digest = seal_global_memory_book_plan(conn, plan)
                stored_run = {
                    **stored_run,
                    "sealedCatalogDigest": sealed_digest,
                }
            response = {
                "schemaVersion": "rag-ime.knowledge-database-organize.v1",
                "ok": bool(validation.get("ok")),
                "dryRun": not auto_applied,
                "applySupported": not auto_applied,
                "applyRequiresReview": not auto_applied,
                "autoApplied": auto_applied,
                "appliedDiffCount": sum(
                    1
                    for item in stored_run.get("diffs") or []
                    if isinstance(item, Mapping) and item.get("status") == "applied"
                ),
                "storedDraft": not auto_applied,
                "reusedDraft": True,
                "sealedCatalogDigest": _string(
                    stored_run.get("sealedCatalogDigest")
                ),
                "source": {
                    "bundleHash": bundle.get("bundleHash"),
                    "eventCount": len(bundle.get("recentEvents") or []),
                    "redactionStats": bundle.get("redactionStats"),
                    "scope": scope,
                    "architecture": str(
                        dict(plan.get("metadata") or {}).get("curationArchitecture")
                        or MEMORY_CURATION_ARCHITECTURE
                    ),
                },
                "plan": plan,
                "validation": validation,
                "storedRun": stored_run,
            }
            self._emit_memory_organizer_event(
                phase="applied" if auto_applied else "draft_ready",
                status="completed" if auto_applied else "waiting",
                summary=(
                    "记忆整理草案已复用并自动应用"
                    if auto_applied
                    else "记忆整理草案已复用，等待审阅"
                ),
                run_id=_string(existing.get("runId") or existing.get("run_id")),
                metrics={
                    "eventCount": len(bundle.get("recentEvents") or []),
                    "changeCount": int(existing.get("diffCount") or 0),
                    "reused": True,
                },
            )
            return response
        organizer: ManagedPiMemoryOrganizer | None = None
        provider_name = ""
        model_name = ""
        catalog_run_started = False

        def fail_catalog_model_run(error: BaseException) -> None:
            nonlocal catalog_run_started
            if not catalog_run_started or organizer is None:
                return
            # Mark it consumed before invoking the failure path so a failure
            # while retiring the internal Session cannot recurse through this
            # cleanup branch.
            catalog_run_started = False
            try:
                organizer.fail_run(error)
            except Exception:
                # The scheduler's lease failure remains the authoritative
                # catalog receipt when model-session cleanup itself fails.
                pass

        try:
            executor = build_governed_memory_model_executor(
                self.agent.runtime,
                managed.automatic_organization_model,
                managed.automatic_organization_thinking_level,
                db_path=self.core.db_path,
            )
            organizer = ManagedPiMemoryOrganizer(executor)
            provider_name = organizer.provider_name
            model_name = organizer.config.model
            if catalog_model_run_id:
                # The model executor owns the durable internal Session.  Start
                # a named run before its first request and close it only after
                # the plan has been durably stored/applied.
                catalog_run_started = True
                organizer.begin_run(catalog_model_run_id)
            decisions = organizer.compile_memory_curation(
                bundle=bundle,
                project=request.project,
                instruction=request.question,
                policy=policy,
            )
            compile_output = curation_decisions_to_compile_output(
                decisions,
                source_bundle=bundle,
                project=request.project,
            )
            plan = memory_book_plan_from_compile_output(
                compile_output,
                project=request.project,
                provider=provider_name,
                model=model_name,
                source_bundle=bundle,
            )
            validation = inspect_memory_book_plan(plan)
            stored_run: dict[str, object] = {}
            if validation.get("ok"):
                with self.core._connect() as conn:  # type: ignore[attr-defined]
                    stored_run = store_memory_book_plan(
                        conn,
                        plan,
                        supersede_project_drafts=True,
                    )
                    if (
                        managed.automatic_organization_auto_apply
                        and str(stored_run.get("status") or "") == "draft"
                        and (
                            int(stored_run.get("diffCount") or 0) > 0
                            or bool(stored_run.get("diffs"))
                        )
                    ):
                        stored_run = apply_stored_memory_book_run(
                            conn,
                            run_id=_string(
                                stored_run.get("runId")
                                or stored_run.get("run_id")
                            ),
                        )
            elif catalog_run_started:
                fail_catalog_model_run(
                    RuntimeError("memory book plan failed validation")
                )
            auto_applied = str(stored_run.get("status") or "") in {
                "applied",
                "partial",
            }
            stored_draft = bool(
                stored_run
                and str(stored_run.get("status") or "") == "draft"
                and (
                    int(stored_run.get("diffCount") or 0) > 0
                    or bool(stored_run.get("diffs"))
                )
            )
            run_id = _string(stored_run.get("runId") or stored_run.get("run_id")) or _string(plan.get("runId"))
            response = {
                "schemaVersion": "rag-ime.knowledge-database-organize.v1",
                "ok": bool(validation.get("ok")),
                "runId": run_id,
                **({"error": "memory_book_plan_failed_validation"} if not validation.get("ok") else {}),
                "dryRun": not auto_applied,
                "applySupported": not auto_applied,
                "applyRequiresReview": stored_draft,
                "autoApplied": auto_applied,
                "appliedDiffCount": sum(
                    1
                    for item in stored_run.get("diffs") or []
                    if isinstance(item, Mapping) and item.get("status") == "applied"
                ),
                "storedDraft": stored_draft,
                "reviewRequired": stored_draft,
                "reusedDraft": False,
                "sealedCatalogDigest": _string(
                    stored_run.get("sealedCatalogDigest")
                ),
                "source": {
                    "bundleHash": bundle.get("bundleHash"),
                    "eventCount": len(bundle.get("recentEvents") or []),
                    "redactionStats": bundle.get("redactionStats"),
                    "scope": scope,
                    "architecture": MEMORY_CURATION_ARCHITECTURE,
                    "lexicon": dict(
                        compile_output.get("lexiconDiagnostics") or {}
                    ),
                },
                "plan": plan,
                "validation": validation,
                "storedRun": stored_run,
            }
            self._emit_memory_organizer_event(
                phase=(
                    "applied"
                    if auto_applied
                    else "draft_ready"
                    if stored_draft
                    else "draft_finished"
                ),
                status=(
                    "completed"
                    if auto_applied
                    else "waiting"
                    if stored_draft
                    else "completed"
                    if validation.get("ok")
                    else "failed"
                ),
                summary=(
                    "记忆整理变更已通过治理校验并自动应用"
                    if auto_applied
                    else "记忆整理草案已生成，等待审阅"
                    if stored_draft
                    else "本批记忆整理未产生待审变更"
                    if validation.get("ok")
                    else "记忆整理草案校验失败"
                ),
                run_id=run_id,
                metrics={
                    "eventCount": len(bundle.get("recentEvents") or []),
                    "changeCount": int(stored_run.get("diffCount") or 0),
                    "reused": False,
                },
            )
            if catalog_run_started:
                # The surrounding SQLite context has committed the durable
                # memory plan and any applied diffs before this terminal
                # model-run transition.
                organizer.finish_run()
                catalog_run_started = False
            return response
        except Exception as exc:
            fail_catalog_model_run(exc)
            raise
        finally:
            if organizer is not None:
                organizer.close()

    def _emit_memory_organizer_event(self, **payload: Any) -> None:
        try:
            self.agent.observations.emit_memory_event(**payload)
        except Exception:
            # The plan/validation receipt is authoritative. Optional journal
            # projection must not replace it with a second failure or make an
            # already committed merge appear to have failed.
            return

    def agent_memory_maintenance_trigger(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if (
            self.config.server_name != "agent gateway"
            or not self._agent_runtime_execution_owner
        ):
            raise ValueError(
                "Memory model maintenance must be triggered on the Agent Gateway"
            )
        return self.memory_maintenance_jobs.trigger(payload)

    def agent_memory_maintenance_trigger_status(
        self,
        job_id: object,
    ) -> dict[str, object]:
        return self.memory_maintenance_jobs.status(job_id)

    def memory_lifecycle_status(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Read-only lifecycle counters and the persisted refresh queue."""
        project = _string(payload.get("project")) or self.config.project
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {"ok": False, "error": "local SQLite core required"}
        with lifecycle_connect(self.config.db_path) as conn:
            lifecycle_require_schema(conn)
            counts = {
                "sources": int(conn.execute(
                    """SELECT COUNT(*)
                       FROM agent_memory_sources AS source
                       JOIN input_events AS event ON event.id = source.input_event_id
                       WHERE event.project IN ('', ?)""",
                    (project,),
                ).fetchone()[0]),
                "evidence": int(conn.execute(
                    "SELECT COUNT(*) FROM agent_memory_evidence WHERE project IN ('', ?)",
                    (project,),
                ).fetchone()[0]),
                # Personal Atoms intentionally have an empty project scope;
                # include them alongside project-scoped Atoms for this view.
                "atoms": int(conn.execute(
                    "SELECT COUNT(*) FROM memory_atoms WHERE COALESCE(scope_project, '') IN ('', ?)",
                    (project,),
                ).fetchone()[0]),
            }
            jobs = [dict(row) for row in conn.execute(
                "SELECT job_id,state,error,created_at_ms,updated_at_ms,completed_at_ms FROM memory_maintenance_jobs WHERE job_id LIKE 'memory-refresh:%' ORDER BY updated_at_ms DESC LIMIT 20"
            )]
            exclusions = [dict(row) for row in conn.execute(
                "SELECT project,target_kind,target_id,created_at_ms FROM memory_capture_exclusions WHERE project IN ('',?) ORDER BY created_at_ms DESC LIMIT 100", (project,)
            )]
        return {"schemaVersion": "paw.memory-lifecycle-status.v1", "ok": True, "project": project, "counts": counts, "jobs": jobs, "exclusions": exclusions}

    def memory_lifecycle_refresh(self, payload: Mapping[str, object]) -> dict[str, object]:
        worker = self.memory_lifecycle_worker
        if worker is None:
            raise ValueError("Memory lifecycle refresh must run on the Agent Gateway")
        worker.start()
        result = self.memory_lifecycle_jobs.submit(
            operation=_string(payload.get("operation")) or "daily_report",
            project=_string(payload.get("project")) or self.config.project,
            day=_string(payload.get("date")),
            timezone=_string(payload.get("timezone")) or "UTC",
            scheduled=bool(payload.get("scheduled")),
        )
        worker.wake()
        return result

    def memory_lifecycle_report(self, payload: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {"ok": False, "error": "local SQLite core required"}
        with lifecycle_connect(self.config.db_path) as conn:
            lifecycle_require_schema(conn)
            return lifecycle_report(
                conn,
                project=_string(payload.get("project")) or self.config.project,
                day=_string(payload.get("date")),
                timezone=_string(payload.get("timezone")) or "UTC",
                include_timeline=not bool(payload.get("noTimeline")),
            )

    def memory_lifecycle_forget_preview(self, payload: Mapping[str, object]) -> dict[str, object]:
        with lifecycle_connect(self.config.db_path) as conn:
            return lifecycle_forget_preview(conn, project=_string(payload.get("project")) or self.config.project, source_id=_string(payload.get("sourceId")))

    def memory_lifecycle_forget_apply(self, payload: Mapping[str, object]) -> dict[str, object]:
        with lifecycle_connect(self.config.db_path) as conn:
            return lifecycle_forget_source(
                conn,
                project=_string(payload.get("project")) or self.config.project,
                source_id=_string(payload.get("sourceId")),
                expected_plan_digest=_string(payload.get("expectedPlanDigest")),
            )

    def _publish_memory_maintenance_event(
        self,
        event: Mapping[str, object],
    ) -> None:
        """Project the durable Gateway job receipt into the canonical Trace journal."""

        observations = getattr(getattr(self, "agent", None), "observations", None)
        emitter = getattr(observations, "emit_memory_event", None)
        if not callable(emitter):
            return
        job_id = _string(event.get("jobId"))
        run_id = _string(event.get("runId")) or job_id
        if not run_id:
            return
        phase = _string(event.get("phase")) or "updated"
        status = _string(event.get("status")) or "info"
        if status not in {"queued", "running", "waiting", "completed", "failed", "cancelled", "expired", "info"}:
            status = "info"
        refs: list[dict[str, object]] = []
        if job_id:
            refs.append({"kind": "memory_maintenance_job", "id": job_id, "label": "Memory maintenance"})
        source_cursor = event.get("sourceCursor")
        if isinstance(source_cursor, Mapping):
            for key in ("fromSourceId", "toSourceId", "sourceId"):
                value = _string(source_cursor.get(key))
                if value:
                    refs.append({"kind": "memory_source_cursor", "id": value, "label": key})
        result = event.get("result")
        owner_run_ids: list[str] = []
        if isinstance(result, Mapping):
            direct_run_id = _string(result.get("runId"))
            if direct_run_id:
                owner_run_ids.append(direct_run_id)
            result_items = result.get("results")
            if isinstance(result_items, list):
                owner_run_ids.extend(
                    _string(item.get("runId"))
                    for item in result_items
                    if isinstance(item, Mapping) and _string(item.get("runId"))
                )
        for owner_run_id in dict.fromkeys(owner_run_ids):
            if owner_run_id == run_id:
                continue
            refs.append(
                {
                    "kind": "owner_memory_run",
                    "id": owner_run_id,
                    "label": "Owner memory run",
                }
            )
        try:
            emitter(
                phase=phase,
                status=status,
                summary=_string(event.get("summary")) or "Memory maintenance 状态已更新",
                run_id=run_id,
                metrics={
                    "sourceCount": _memory_maintenance_result_count(event.get("result"), "sourceCount"),
                    "changeCount": _memory_maintenance_result_count(event.get("result"), "diffCount"),
                },
                refs=refs,
            )
        except Exception:
            # Observability must not change the already-persisted maintenance
            # result or make the Gateway worker fail closed on shutdown.
            return

    def _execute_gateway_memory_catalog_consolidation(
        self,
        *,
        project: str,
        manual: bool,
        managed: MemoryMaintenanceSettings,
    ) -> dict[str, object]:
        scheduler = self.memory_catalog_scheduler
        base = {
            "schemaVersion": CATALOG_CONSOLIDATION_SCHEMA_VERSION,
            "project": project,
            "manual": bool(manual),
            "modelCalled": False,
        }
        if not managed.catalog_consolidation_enabled:
            status = scheduler.status(project)
            return {
                **base,
                "ok": True,
                "skipped": True,
                "reason": "catalog_consolidation_disabled",
                "due": False,
                "status": status,
                "state": status.get("state"),
                "catalogDigest": status.get("catalogDigest", ""),
                "curationRunId": status.get("curationRunId", ""),
                "nextDueAtMs": status.get("nextDueAtMs", 0),
            }
        if not managed.automatic_organization_enabled:
            status = scheduler.status(project)
            return {
                **base,
                "ok": True,
                "skipped": True,
                "reason": "automatic_organization_disabled",
                "due": False,
                "status": status,
                "state": status.get("state"),
                "catalogDigest": status.get("catalogDigest", ""),
                "curationRunId": status.get("curationRunId", ""),
                "nextDueAtMs": status.get("nextDueAtMs", 0),
            }

        decision = scheduler.admit(
            project,
            manual=manual,
            enabled=managed.catalog_consolidation_enabled,
            automatic_organization_enabled=managed.automatic_organization_enabled,
            cadence_days=managed.catalog_consolidation_cadence_days,
        )
        if not bool(decision.get("admitted")):
            status = (
                decision.get("status")
                if isinstance(decision.get("status"), Mapping)
                else scheduler.status(project)
            )
            return {
                **base,
                "ok": True,
                "skipped": not bool(decision.get("due")),
                "reason": _string(decision.get("reason")) or "not_due",
                "due": bool(decision.get("due")),
                "status": dict(status),
                "state": status.get("state"),
                "catalogDigest": status.get("catalogDigest", ""),
                "curationRunId": status.get("curationRunId", ""),
                "nextDueAtMs": status.get("nextDueAtMs", 0),
            }

        admission_token = _string(decision.get("admissionToken"))
        if not admission_token:
            # ``admit`` is the only producer of a token.  Treat a malformed
            # admission as a local failure rather than attempting an
            # unauthorised transition.
            raise RuntimeError("catalog consolidation admission returned no token")
        digest = ""
        try:
            with self.core._connect() as conn:  # type: ignore[attr-defined]
                conn.execute("BEGIN")
                bundle = build_memory_book_source_bundle(
                    conn,
                    project=project,
                    since_days=7,
                    limit=500,
                    after_event_id=0,
                    newest_first=True,
                    curation_scope="global",
                    catalog_only=True,
                )
            if not isinstance(bundle, Mapping):
                raise ValueError("global Memory catalog bundle is invalid")
            digest = compact_whitespace(
                str(bundle.get("catalogDigest") or bundle.get("bundleHash") or "")
            )[:240]
            if not digest:
                raise ValueError("global Memory catalog bundle has no stable digest")
            scheduler.record_digest(
                project,
                digest,
                admission_token=admission_token,
            )
            previous_status = decision.get("status")
            previous_completed_digest = (
                _string(previous_status.get("lastSuccessfulCatalogDigest"))
                if isinstance(previous_status, Mapping)
                else ""
            )
            previous_completed_at = (
                int(previous_status.get("lastSuccessfulCatalogCommittedAtMs") or 0)
                if isinstance(previous_status, Mapping)
                else 0
            )
            if previous_completed_at > 0 and previous_completed_digest == digest:
                unchanged = {
                    **base,
                    "ok": True,
                    "skipped": True,
                    "due": True,
                    "reason": "catalog_unchanged",
                    "catalogDigest": digest,
                    "curationRunId": (
                        _string(previous_status.get("curationRunId"))
                        if isinstance(previous_status, Mapping)
                        else ""
                    ),
                }
                receipt = scheduler.complete(
                    project,
                    catalog_digest=digest,
                    result=unchanged,
                    curation_run_id=_string(unchanged.get("curationRunId")),
                    ok=True,
                    cadence_days=managed.catalog_consolidation_cadence_days,
                    admission_token=admission_token,
                )
                return {
                    **unchanged,
                    "state": receipt.get("state"),
                    "nextDueAtMs": receipt.get("nextDueAtMs", 0),
                    "receipt": receipt,
                }

            request = KnowledgeWorkbenchRequest(
                question=GLOBAL_MEMORY_CATALOG_CONSOLIDATION_INSTRUCTION[:800],
                mode="database_organize",
                project=project,
                curation_scope="global",
                curation_policy="conservative",
            )
            organizer = self._knowledge_workbench_database_organizer(
                request,
                source_bundle=bundle,
                # Keep the lease token private to this scheduler method.  The
                # durable model run is named by the receipt id, not by
                # curation evidence or model-visible metadata.
                catalog_model_run_id=(
                    _string(
                        dict(decision.get("status") or {}).get("receiptId")
                    )
                ),
            )
            organized_result = (
                dict(organizer) if isinstance(organizer, Mapping) else {}
            )
            organized_ok = organized_result.get("ok") is True
            stored_run = organized_result.get("storedRun")
            run_id = (
                _string(stored_run.get("runId") or stored_run.get("run_id"))
                if isinstance(stored_run, Mapping)
                else ""
            ) or _string(organized_result.get("curationRunId")) or _string(
                organized_result.get("runId")
            )
            model_called = (
                _bool(organized_result.get("modelCalled"))
                if "modelCalled" in organized_result
                else not bool(organized_result.get("reusedDraft"))
            )
            if organized_ok:
                # The compiler seals this digest under the same SQLite write
                # reservation that validates the frozen catalog and stores or
                # applies the governed plan.  Rebuilding after commit could
                # incorrectly receipt a concurrent writer's unreviewed state.
                digest = _string(organized_result.get("sealedCatalogDigest"))
                if not digest:
                    raise ValueError(
                        "organized global Memory catalog has no sealed digest"
                    )
            output = {
                **base,
                "ok": organized_ok,
                "skipped": False,
                "due": True,
                "reason": "completed" if organized_ok else "organizer_failed",
                "modelCalled": model_called,
                "catalogDigest": digest,
                "curationRunId": run_id,
                "organizer": organized_result,
            }
            receipt = scheduler.complete(
                project,
                catalog_digest=digest,
                result=organized_result,
                curation_run_id=run_id,
                ok=organized_ok,
                error=_string(organized_result.get("error")),
                cadence_days=managed.catalog_consolidation_cadence_days,
                admission_token=admission_token,
            )
            return {
                **output,
                "state": receipt.get("state"),
                "nextDueAtMs": receipt.get("nextDueAtMs", 0),
                "receipt": receipt,
            }
        except Exception as exc:
            error = compact_whitespace(str(exc))[:800] or exc.__class__.__name__
            try:
                receipt = scheduler.fail(
                    project,
                    admission_token=admission_token,
                    catalog_digest=digest,
                    result={"ok": False, "error": error},
                    error=error,
                    cadence_days=managed.catalog_consolidation_cadence_days,
                )
            except Exception:
                # A lease can expire while a worker is unwinding.  Its token
                # must not mutate a successor run; expose the current durable
                # status and leave recovery to the next admitted worker.
                receipt = scheduler.status(project)
            return {
                **base,
                "ok": False,
                "skipped": False,
                "due": True,
                "reason": "failed",
                "error": error,
                "catalogDigest": digest,
                "curationRunId": receipt.get("curationRunId", ""),
                "state": receipt.get("state", "failed"),
                "nextDueAtMs": receipt.get("nextDueAtMs", 0),
                "receipt": receipt,
            }

    def _execute_gateway_memory_maintenance(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.owner-memory-curation-run.v1",
                "ok": False,
                "error": "local SQLite core required",
                "results": [],
            }
        project = _string(payload.get("project")) or self.config.project
        manual = bool(payload.get("manual"))
        owner_kind = _string(payload.get("ownerKind"))
        owner_id = _string(payload.get("ownerId"))
        owner_scoped_manual = manual and bool(owner_kind and owner_id)
        managed = MemoryMaintenanceSettings.load(
            self.core.db_path,
            preverified_schema=True,
        )
        if payload.get("catalogOnly") is True:
            catalog = self._execute_gateway_memory_catalog_consolidation(
                project=project, manual=manual, managed=managed,
            )
            return {
                "schemaVersion": "rag-ime.owner-memory-curation-run.v1",
                "ok": catalog.get("ok") is True,
                "catalogOnly": True,
                "runId": _string(catalog.get("curationRunId")),
                "catalogConsolidation": catalog,
                "results": [],
                "managedSettings": managed.as_dict(),
                "executionOwner": "agent_gateway",
                "transport": "gateway_internal_session",
            }
        if bool(payload.get("timelineOnly")):
            return self._execute_gateway_memory_dreaming(
                project=project,
                manual=True,
                managed=managed,
                max_sources=1,
                timeline_date=_string(payload.get("timelineDate")),
                timeline_through_date=_string(
                    payload.get("timelineThroughDate")
                ),
                timeline_start_date=_string(payload.get("timelineStartDate")),
                progress=payload.get("_progressCallback"),
            )
        stage_errors: list[dict[str, object]] = []

        def optional_stage(name: str, callback: Callable[[], Mapping[str, object]]) -> dict[str, object]:
            """Keep one auxiliary lane from hiding the curation receipt."""

            try:
                value = dict(callback())
            except Exception as exc:
                value = {
                    "ok": False,
                    "error": compact_whitespace(str(exc))[:800] or exc.__class__.__name__,
                    "errorCode": f"{name}_failed",
                }
            if value.get("ok") is False:
                stage_errors.append({
                    "stage": name,
                    "errorCode": _string(value.get("errorCode")) or f"{name}_failed",
                    "error": _string(value.get("error"))[:800],
                })
            return value

        lexicon = (
            optional_stage(
                "lexicon",
                lambda: run_due_lexicon_organization(
                    self.core.db_path,
                    project=project,
                    force=manual,
                ),
            )
            if managed.automatic_organization_enabled
            else {
                "ok": True,
                "skipped": True,
                "reason": "automatic_organization_disabled",
            }
        )
        if managed.automatic_organization_enabled:
            executor = build_governed_memory_model_executor(
                self.agent.runtime,
                managed.automatic_organization_model,
                managed.automatic_organization_thinking_level,
                db_path=self.core.db_path,
            )
            organizer = ManagedPiMemoryOrganizer(executor)
            try:
                trace_context = (
                    payload.get("_memoryTraceContext")
                    if isinstance(payload.get("_memoryTraceContext"), Mapping)
                    else {}
                )
                curator = OwnerMemoryCurator(
                    self.core.db_path,
                    organizer=organizer,
                    project=project,
                    # A manual Control Center run is deliberately small and
                    # resumable. Atom-first admits six logical inputs per
                    # request; honour that boundary even if a caller sends
                    # the broader scheduled-run limit (or 1,500) over HTTP.
                    max_sources=(
                        min(
                            _bounded_int(
                                payload.get("maxSources"),
                                default=DEFAULT_MAX_SOURCES,
                                minimum=1,
                                maximum=MAX_PERSONAL_V2_SOURCES,
                            ),
                            _MAX_MANUAL_CURATION_SOURCE_BATCH,
                        )
                        if owner_scoped_manual
                        else _bounded_int(
                            payload.get("maxSources"),
                            default=DEFAULT_MAX_SOURCES,
                            minimum=1,
                            maximum=MAX_PERSONAL_V2_SOURCES,
                        )
                    ),
                    # Validated routine curation is the configured promotion
                    # path; it keeps the stored run and rollback evidence but
                    # does not wait for a second human approval.
                    auto_apply=managed.automatic_organization_auto_apply,
                    include_agent_dialogue=managed.include_agent_dialogue,
                    daily_interval_ms=max(
                        60,
                        managed.automatic_organization_interval_seconds,
                    )
                    * 1_000,
                    embedding_provider=self.core.embedding_provider,
                    observations=self.agent.observations,
                    trace_id=_string(trace_context.get("traceId")),
                    maintenance_job_id=_string(
                        trace_context.get("maintenanceJobId")
                    ),
                    parent_span_id=_string(trace_context.get("parentSpanId")),
                )
                curator.initialize()
                if owner_scoped_manual:
                    # Keep the normal manual throughput at six, but shrink a
                    # retry after a model failure. The cursor/backoff is the
                    # durable signal; no model output is guessed or replayed.
                    retry_after_failure = False
                    try:
                        with self.core._connect() as conn:  # type: ignore[attr-defined]
                            prior = conn.execute(
                                """
                                SELECT consecutive_failures, last_error
                                FROM memory_curation_cursors
                                WHERE owner_kind = ? AND owner_id = ?
                                  AND project = ? AND lane = 'daily'
                                """,
                                (owner_kind, owner_id, project),
                            ).fetchone()
                        retry_after_failure = bool(
                            prior is not None
                            and int(prior[0] or 0) > 0
                            # _claim_scope clears last_error when it takes the
                            # recovery lease, so consecutive failure count is
                            # the durable signal available at this point.
                        )
                    except Exception:
                        retry_after_failure = False
                    if retry_after_failure:
                        curator.max_sources = min(
                            int(curator.max_sources),
                            _MAX_MANUAL_CURATION_TIMEOUT_RETRY_BATCH,
                        )
                instruction = compact_whitespace(
                    _string(payload.get("instruction"))
                )[:800]
                curation_reports: list[dict[str, object]] = []
                batch_summaries: list[dict[str, object]] = []
                seen_progress: set[tuple[int, str, int]] = set()
                progress_callback = payload.get("_progressCallback")
                processed_source_count = 0
                total_source_count = 0
                max_batches = (
                    _MAX_MANUAL_CURATION_TRIGGER_BATCHES
                    if manual and managed.automatic_organization_auto_apply
                    else 1
                )
                for batch_index in range(max_batches):
                    current_report = dict(curator.run_due(
                        manual=manual,
                        owner_kind=owner_kind,
                        owner_id=owner_id,
                        instruction=instruction,
                    ))
                    curation_reports.append(current_report)
                    results = [
                        dict(item)
                        for item in current_report.get("results") or []
                        if isinstance(item, Mapping)
                    ]
                    owner_result = next(
                        (
                            item for item in results
                            if (not owner_kind or _string(item.get("ownerKind")) == owner_kind)
                            and (not owner_id or _string(item.get("ownerId")) == owner_id)
                        ),
                        results[0] if results else {},
                    )
                    status_payload = current_report.get("status")
                    scopes = (
                        status_payload.get("scopes")
                        if isinstance(status_payload, Mapping)
                        else []
                    )
                    owner_scope = next(
                        (
                            dict(item) for item in scopes
                            if isinstance(item, Mapping)
                            and (not owner_kind or _string(item.get("ownerKind")) == owner_kind)
                            and (not owner_id or _string(item.get("ownerId")) == owner_id)
                        ),
                        {},
                    )
                    pending_count = int(
                        owner_result.get("remainingSourceCount")
                        or owner_scope.get("pendingSourceCount")
                        or 0
                    )
                    processed_source_count += int(owner_result.get("sourceCount") or 0)
                    total_source_count = max(
                        total_source_count,
                        int(owner_scope.get("totalSourceCount") or 0),
                        processed_source_count + pending_count,
                    )
                    cursor = owner_scope.get("lastSourceCursor")
                    cursor_map = cursor if isinstance(cursor, Mapping) else {}
                    progress_key = (
                        int(cursor_map.get("createdAtMs") or 0),
                        _string(cursor_map.get("sourceId")),
                        pending_count,
                    )
                    batch_summaries.append({
                        "batch": batch_index + 1,
                        "runId": _string(owner_result.get("runId")),
                        "runStatus": _string(owner_result.get("runStatus")),
                        "sourceCount": int(owner_result.get("sourceCount") or 0),
                        "modelSourceCount": int(owner_result.get("modelSourceCount") or 0),
                        "pendingSourceCount": pending_count,
                        "autoApplied": bool(owner_result.get("autoApplied")),
                    })
                    if callable(progress_callback):
                        try:
                            progress_callback({
                                "phase": "owner_memory_curation",
                                "batch": batch_index + 1,
                                "batchLimit": max_batches,
                                "processedSourceCount": processed_source_count,
                                "totalSourceCount": total_source_count,
                                "pendingSourceCount": pending_count,
                                "completedSourceCount": max(
                                    0, total_source_count - pending_count
                                ),
                            })
                        except Exception:
                            # Progress is a read projection; it cannot change
                            # the durable curation result.
                            pass
                    report = current_report
                    if (
                        not manual
                        or not managed.automatic_organization_auto_apply
                        or current_report.get("ok") is not True
                        or pending_count <= 0
                        or _string(owner_result.get("runStatus"))
                        not in {"applied", "idle", "empty"}
                        or progress_key in seen_progress
                    ):
                        break
                    seen_progress.add(progress_key)
                report = dict(report)
                report["batchSummaries"] = batch_summaries
                report["batchCount"] = len(curation_reports)
                report["pendingSourceCount"] = batch_summaries[-1]["pendingSourceCount"] if batch_summaries else 0
                report["drainLimited"] = bool(
                    batch_summaries
                    and report["pendingSourceCount"] > 0
                    and len(curation_reports) >= max_batches
                )
                report["effectiveModel"] = executor.reference
                report["effectiveThinkingLevel"] = executor.thinking_level
                report["effectiveContextWindow"] = int(
                    executor.selected_model.get("contextWindow") or 0
                )
                report["curationProtocol"] = organizer.curation_protocol_version
            finally:
                organizer.close()
        else:
            report = {
                "schemaVersion": "rag-ime.owner-memory-curation-run.v1",
                "ok": True,
                "skipped": True,
                "reason": "automatic_organization_disabled",
                "results": [],
            }
        catalog = (
            {
                "ok": True,
                "skipped": True,
                "reason": "owner_curation_only",
            }
            if owner_scoped_manual
            else optional_stage(
                "catalog",
                lambda: self._execute_gateway_memory_catalog_consolidation(
                    project=project,
                    manual=manual,
                    managed=managed,
                ),
            )
        )
        dreaming = (
            {
                "ok": True,
                "skipped": True,
                "reason": "owner_curation_only",
            }
            if owner_scoped_manual
            else optional_stage(
                "dreaming",
                lambda: self._execute_gateway_memory_dreaming(
                    project=project,
                    manual=manual,
                    managed=managed,
                    max_sources=payload.get("maxSources"),
                    progress=payload.get("_progressCallback"),
                ),
            )
        )
        report["lexiconOrganization"] = lexicon
        report["dreaming"] = dreaming
        report["catalogConsolidation"] = catalog
        # The owner curation lane is the primary result when it is enabled.
        # Dreaming is the primary lane only when owner curation is disabled;
        # auxiliary failures remain explicit and retryable without changing a
        # committed curation into a false whole-job failure.
        primary_result = report
        if not managed.automatic_organization_enabled:
            primary_result = dreaming
        if primary_result.get("ok") is False:
            stage_errors.insert(0, {
                "stage": "curation" if managed.automatic_organization_enabled else "dreaming",
                "errorCode": _string(primary_result.get("errorCode")) or "memory_curation_failed",
                "error": _string(primary_result.get("error"))[:800],
            })
        report["stageErrors"] = stage_errors
        report["degraded"] = bool(stage_errors) and primary_result.get("ok") is True
        report["ok"] = primary_result.get("ok") is True
        if report["ok"]:
            report["error"] = ""
        report["managedSettings"] = managed.as_dict()
        report["executionOwner"] = "agent_gateway"
        report["transport"] = "gateway_internal_session"
        return report

    def _execute_gateway_memory_dreaming(
        self,
        *,
        project: str,
        manual: bool,
        managed: MemoryMaintenanceSettings,
        max_sources: object,
        timeline_date: str = "",
        timeline_through_date: str = "",
        timeline_start_date: str = "",
        progress: object | None = None,
    ) -> dict[str, object]:
        if not managed.dreaming_enabled and not managed.automatic_organization_enabled:
            return {
                "schemaVersion": "rag-ime.personal-context-maintenance-run.v1",
                "ok": True,
                "skipped": True,
                "reason": "memory_maintenance_disabled",
                "targets": [],
            }
        role_book_organizer: ManagedPiMemoryOrganizer | None = None
        activity_organizer: ManagedPiMemoryOrganizer | None = None
        try:
            # A date-specific rebuild is an Activity-only operation. Do not
            # allocate a second Role/Book executor whose model profile would
            # never be used, and never make the Activity projection inherit a
            # different dreaming profile by accident.
            if (
                managed.dreaming_enabled
                and not timeline_date
                and not timeline_through_date
            ):
                executor = build_governed_memory_model_executor(
                    self.agent.runtime,
                    managed.dreaming_model,
                    managed.dreaming_thinking_level,
                    db_path=self.core.db_path,  # type: ignore[union-attr]
                )
                role_book_organizer = ManagedPiMemoryOrganizer(executor)
            if managed.automatic_organization_enabled:
                activity_executor = build_governed_memory_model_executor(
                    self.agent.runtime,
                    managed.automatic_organization_model,
                    managed.automatic_organization_thinking_level,
                    db_path=self.core.db_path,  # type: ignore[union-attr]
                )
                activity_organizer = ManagedPiMemoryOrganizer(activity_executor)
            runner = PersonalContextMaintenanceRunner(
                self.core.db_path,  # type: ignore[union-attr]
                config=PersonalContextMaintenanceConfig(
                    enabled=True,
                    consolidate_roles=managed.dreaming_enabled,
                    build_timelines=managed.automatic_organization_enabled,
                    project=project,
                    min_interval_ms=managed.dreaming_interval_seconds * 1_000,
                    apply_safe_recent_work=managed.dreaming_enabled,
                    auto_publish_timelines=managed.automatic_organization_enabled,
                    timeline_catch_up_limit=(
                        1
                        if (
                            managed.automatic_organization_enabled
                            and not timeline_date
                            and not timeline_through_date
                        )
                        else 0
                    ),
                    batch_limit=_bounded_int(
                        max_sources,
                        default=500,
                        minimum=1,
                        maximum=1_000,
                    ),
                    model=(
                        managed.automatic_organization_model
                        if timeline_date or timeline_through_date
                        else managed.dreaming_model
                    ),
                    thinking_level=(
                        managed.automatic_organization_thinking_level
                        if timeline_date or timeline_through_date
                        else managed.dreaming_thinking_level
                    ),
                ),
                role_book_organizer=role_book_organizer,
                activity_organizer=activity_organizer,
            )
            if timeline_through_date:
                result = runner.build_activity_timelines_through(
                    timeline_through_date,
                    **({"start_date": timeline_start_date} if timeline_start_date else {}),
                    progress=progress,
                )
            elif timeline_date:
                result = runner.build_activity_timeline(timeline_date)
            else:
                result = (
                    runner.run_once(force=manual, progress=progress)
                    if callable(progress)
                    else runner.run_once(force=manual)
                )
            result["executionOwner"] = "agent_gateway"
            result["transport"] = "gateway_internal_session"
            return result
        except Exception as exc:
            return {
                "schemaVersion": "rag-ime.personal-context-maintenance-run.v1",
                "ok": False,
                "error": compact_whitespace(str(exc))[:800]
                or exc.__class__.__name__,
                "targets": [],
                "executionOwner": "agent_gateway",
                "transport": "gateway_internal_session",
            }
        finally:
            if role_book_organizer is not None:
                role_book_organizer.close()
            if (
                activity_organizer is not None
                and activity_organizer is not role_book_organizer
            ):
                activity_organizer.close()

    def agent_memory_maintenance_prepare(self, payload: dict[str, Any]) -> dict[str, object]:
        instruction = compact_whitespace(_string(payload.get("instruction")))[:800] or (
            "根据新增最终消息和已验证工具回执增量整理长期记忆；通过治理校验后自动应用，并保留可回滚回执。"
        )
        requested_owner_kind = _string(payload.get("ownerKind"))
        requested_owner_id = _string(payload.get("ownerId"))
        if requested_owner_kind or requested_owner_id:
            owner_kind, owner_id = normalize_memory_owner(
                requested_owner_kind,
                requested_owner_id,
            )
            if not isinstance(self.core, LocalSqliteCoreClient):
                return {
                    "schemaVersion": "rag-ime.knowledge-database-organize.v1",
                    "ok": False,
                    "error": "local SQLite core required",
                }
            project = _string(payload.get("project")) or self.config.project
            managed = MemoryMaintenanceSettings.load(
                self.core.db_path,
                preverified_schema=True,
            )
            executor = build_governed_memory_model_executor(
                self.agent.runtime,
                managed.automatic_organization_model,
                managed.automatic_organization_thinking_level,
                db_path=self.core.db_path,
            )
            organizer = ManagedPiMemoryOrganizer(executor)
            try:
                curator = OwnerMemoryCurator(
                    self.core.db_path,
                    organizer=organizer,
                    project=project,
                    max_sources=_bounded_int(
                        payload.get("maxSources"),
                        default=DEFAULT_MAX_SOURCES,
                        minimum=1,
                        maximum=MAX_PERSONAL_V2_SOURCES,
                    ),
                    auto_apply=managed.automatic_organization_auto_apply,
                    embedding_provider=self.core.embedding_provider,
                    observations=self.agent.observations,
                )
                curator.initialize()
                reports: list[dict[str, object]] = []
                batch_summaries: list[dict[str, object]] = []
                report: dict[str, object] = {}
                result: dict[str, object] = {}
                scope: dict[str, object] = {}
                stored_run: dict[str, object] = {}
                stored_draft = False
                seen_cursors: set[tuple[int, str, int]] = set()
                for batch_index in range(_MAX_MANUAL_CURATION_PREPARE_BATCHES):
                    report = curator.run_due(
                        manual=True,
                        owner_kind=owner_kind,
                        owner_id=owner_id,
                        instruction=instruction,
                    )
                    reports.append(dict(report))
                    scopes = list(
                        dict(report.get("status") or {}).get("scopes") or []
                    )
                    scope = next(
                        (
                            dict(item)
                            for item in scopes
                            if isinstance(item, dict)
                            and _string(item.get("ownerKind")) == owner_kind
                            and _string(item.get("ownerId")) == owner_id
                        ),
                        {},
                    )
                    results = [
                        dict(item)
                        for item in report.get("results") or []
                        if isinstance(item, dict)
                    ]
                    result = next(
                        (
                            item
                            for item in results
                            if _string(item.get("ownerKind")) == owner_kind
                            and _string(item.get("ownerId")) == owner_id
                        ),
                        {},
                    )
                    run_id = _string(result.get("runId")) or _string(
                        scope.get("lastRunId")
                    )
                    stored_run = {}
                    if run_id:
                        with self.core._connect() as conn:  # type: ignore[attr-defined]
                            stored_run = memory_book_run_payload(conn, run_id=run_id)
                    stored_draft = _string(stored_run.get("status")) == "draft"
                    pending_count = int(scope.get("pendingSourceCount") or 0)
                    needs_review_count = int(scope.get("needsReviewSourceCount") or 0)
                    cursor = dict(scope.get("lastSourceCursor") or {})
                    cursor_key = (
                        int(cursor.get("createdAtMs") or 0),
                        _string(cursor.get("sourceId")),
                        pending_count,
                    )
                    batch_summaries.append(
                        {
                            "batch": batch_index + 1,
                            "runId": run_id,
                            "runStatus": _string(result.get("runStatus")),
                            "sourceCount": int(result.get("sourceCount") or 0),
                            "modelSourceCount": int(result.get("modelSourceCount") or 0),
                            "deferredModelInputCount": int(
                                result.get("deferredModelInputCount") or 0
                            ),
                            "pendingSourceCount": pending_count,
                            "needsReviewSourceCount": needs_review_count,
                        }
                    )
                    stop_reason = _string(result.get("reason"))
                    if (
                        report.get("ok") is not True
                        or stored_draft
                        or pending_count <= 0
                        or needs_review_count > 0
                        or stop_reason
                        in {
                            "already_running",
                            "draft_pending_review",
                            "no_sources",
                        }
                        or cursor_key in seen_cursors
                    ):
                        break
                    seen_cursors.add(cursor_key)

                pending_count = int(scope.get("pendingSourceCount") or 0)
                needs_review_count = int(scope.get("needsReviewSourceCount") or 0)
                auto_applied = any(
                    bool(item.get("autoApplied"))
                    for report_item in reports
                    for item in report_item.get("results") or []
                    if isinstance(item, dict)
                )
                applied_diff_count = sum(
                    int(item.get("diffCount") or 0)
                    for report_item in reports
                    for item in report_item.get("results") or []
                    if isinstance(item, dict) and bool(item.get("autoApplied"))
                )
                drain_limited = (
                    not stored_draft
                    and pending_count > 0
                    and needs_review_count <= 0
                    and len(reports) >= _MAX_MANUAL_CURATION_PREPARE_BATCHES
                )
                return {
                    "schemaVersion": "rag-ime.knowledge-database-organize.v1",
                    "ok": all(item.get("ok") is True for item in reports),
                    "dryRun": not auto_applied,
                    "applySupported": not auto_applied,
                    "applyRequiresReview": stored_draft and not auto_applied,
                    "autoApplied": auto_applied,
                    "appliedDiffCount": applied_diff_count,
                    "storedDraft": stored_draft,
                    "reusedDraft": bool(
                        result.get("reason") == "draft_pending_review"
                        or (
                            result.get("skipped") is True
                            and _string(scope.get("dueReason")) == "draft_pending_review"
                        )
                    ),
                    "source": {
                        "ownerKind": owner_kind,
                        "ownerId": owner_id,
                        "eventCount": sum(
                            int(item.get("sourceCount") or 0) for item in batch_summaries
                        ),
                        "pendingSourceCount": pending_count,
                        "needsReviewSourceCount": needs_review_count,
                        "autoApplied": auto_applied,
                        "appliedDiffCount": applied_diff_count,
                        "modelSourceCount": sum(
                            int(item.get("modelSourceCount") or 0)
                            for item in batch_summaries
                        ),
                        "batchCount": len(batch_summaries),
                        "drainLimited": drain_limited,
                    },
                    "plan": {},
                    "validation": {
                        "ok": all(item.get("ok") is True for item in reports),
                        "errors": (
                            []
                            if all(item.get("ok") is True for item in reports)
                            else [result.get("error") or "curation_failed"]
                        ),
                    },
                    "storedRun": stored_run,
                    "curation": report,
                    "batchSummaries": batch_summaries,
                }
            finally:
                close = getattr(organizer, "close", None)
                if callable(close):
                    close()
        request = KnowledgeWorkbenchRequest(
            question=instruction,
            mode="database_organize",
            project=_string(payload.get("project")) or self.config.project,
            app="com.rag-ime.control.agent",
            client_id="pi-control-agent",
            curation_scope=_string(payload.get("scope")).lower() or "incremental",
            curation_policy=_string(payload.get("policy")).lower() or "conservative",
        )
        return self._knowledge_workbench_database_organizer(request)

    def agent_memory_maintenance_run(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.agent-memory-maintenance-run.v1",
                "ok": False,
                "error": "local SQLite core required",
            }
        run_id = _string(payload.get("runId"))
        if not run_id:
            raise ValueError("runId is required")
        requested_project = _string(payload.get("project")) or self.config.project
        visible_owners = _memory_visible_owners_from_payload(
            payload,
            project=requested_project,
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            run = memory_book_run_payload(conn, run_id=run_id)
            if not run.get("provider"):
                raise ValueError(f"memory book run not found: {run_id}")
            run_owner = (
                _string(run.get("ownerKind")),
                _string(run.get("ownerId")),
            )
            if visible_owners is not None and run_owner not in visible_owners:
                raise ValueError("memory book run is outside the current owner scope")
            metadata = dict(run.get("metadata") or {})
            project = _string(metadata.get("project"))
            if project != requested_project:
                raise ValueError("memory book run is outside the current project")
            stale = memory_book_run_is_stale(conn, run=run)
            newer_applied_run = find_newer_applied_memory_book_run(conn, run_id=run_id)
        diffs = [dict(item) for item in list(run.get("diffs") or []) if isinstance(item, dict)]
        status_counts: dict[str, int] = {}
        operation_counts: dict[str, int] = {}
        changes: list[dict[str, object]] = []
        for diff in diffs:
            status = _string(diff.get("status")) or "unknown"
            operation = _string(diff.get("op")) or "unknown"
            status_counts[status] = status_counts.get(status, 0) + 1
            operation_counts[operation] = operation_counts.get(operation, 0) + 1
            diff_payload = dict(diff.get("payload") or {})
            title = next(
                (
                    compact_whitespace(_string(diff_payload.get(key)))
                    for key in ("title", "name", "phrase", "bookKey", "tag", "text")
                    if compact_whitespace(_string(diff_payload.get(key)))
                ),
                compact_whitespace(_string(diff.get("targetId"))) or operation,
            )
            detail = next(
                (
                    compact_whitespace(_string(diff_payload.get(key)))
                    for key in ("summary", "description", "reason", "text")
                    if compact_whitespace(_string(diff_payload.get(key)))
                ),
                "",
            )
            source_ids = diff_payload.get("sourceEventIds")
            changes.append(
                {
                    "diffId": int(diff.get("diffId") or 0),
                    "operation": operation,
                    "operationLabel": _memory_book_operation_label(operation),
                    "status": status,
                    "selected": status != "rejected",
                    "title": title[:120],
                    "detail": detail[:240],
                    "sourceCount": len(source_ids) if isinstance(source_ids, list) else 0,
                    "sourceEventIds": [
                        int(source_id)
                        for source_id in (
                            source_ids if isinstance(source_ids, list) else []
                        )
                        if str(source_id).isdigit() and int(source_id) > 0
                    ][:80],
                }
            )
        revision_hash = "sha256:" + hashlib.sha256(
            json.dumps(run, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        run_status = _string(run.get("status"))
        pending_count = status_counts.get("pending", 0) + status_counts.get("approved", 0)
        applied_count = status_counts.get("applied", 0)
        return {
            "schemaVersion": "rag-ime.agent-memory-maintenance-run.v1",
            "ok": True,
            "revisionHash": revision_hash,
            "stale": stale,
            # A draft with every item excluded can still be confirmed to finish
            # review and advance the evidence cursor without mutating memory.
            "canApply": run_status == "draft" and not stale and bool(diffs),
            "canRollback": (
                run_status in {"applied", "partial"}
                and applied_count > 0
                and newer_applied_run is None
            ),
            "newerAppliedRunId": "" if newer_applied_run is None else str(newer_applied_run["runId"]),
            "rollbackBlockedReason": (
                ""
                if newer_applied_run is None
                else "newer_applied_memory_run_must_be_rolled_back_first"
            ),
            "run": {
                "runId": run_id,
                "createdAtMs": int(run.get("createdAtMs") or 0),
                "status": run_status,
                "summary": _string(run.get("summary"))[:240],
                "provider": _string(run.get("provider")),
                "model": _string(run.get("model")),
                "ownerKind": _string(run.get("ownerKind")),
                "ownerId": _string(run.get("ownerId")),
                "runKind": _string(run.get("runKind")),
                "bundleHash": _string(metadata.get("bundleHash")),
                "sourceCursor": dict(metadata.get("sourceCursor") or {}),
                "sourceInputRefs": [
                    dict(item)
                    for item in metadata.get("sourceInputRefs") or []
                    if isinstance(item, dict)
                ][:64],
                "diffCount": len(diffs),
                "pendingDiffCount": pending_count,
                "appliedDiffCount": applied_count,
                "statusCounts": status_counts,
                "operationCounts": operation_counts,
                "changes": changes[:80],
            },
        }

    def _catalog_maintenance_status(
        self,
        project: str,
        managed: MemoryMaintenanceSettings,
    ) -> dict[str, object]:
        raw = self.memory_catalog_scheduler.status(project)
        if not managed.catalog_consolidation_enabled:
            effective_due = False
            effective_reason = "catalog_consolidation_disabled"
        elif not managed.automatic_organization_enabled:
            effective_due = False
            effective_reason = "automatic_organization_disabled"
        else:
            effective_due = bool(raw.get("due"))
            effective_reason = _string(raw.get("dueReason")) or "not_due"
        return {
            **raw,
            "enabled": bool(managed.catalog_consolidation_enabled),
            "automaticOrganizationEnabled": bool(
                managed.automatic_organization_enabled
            ),
            "cadenceDays": int(managed.catalog_consolidation_cadence_days),
            "due": effective_due,
            "dueReason": effective_reason,
        }

    def agent_memory_maintenance_status(self, payload: dict[str, Any]) -> dict[str, object]:
        project = _string(payload.get("project")) or self.config.project
        if _bool(payload.get("projectionOnly")):
            managed = (
                MemoryMaintenanceSettings.load(
                    self.core.db_path,
                    preverified_schema=True,
                )
                if isinstance(self.core, LocalSqliteCoreClient)
                else MemoryMaintenanceSettings()
            )
            return {
                "schemaVersion": "rag-ime.agent-memory-maintenance-status.v1",
                "ok": True,
                "job": self.memory_maintenance_jobs.latest_status(project=project),
                "catalogConsolidation": self._catalog_maintenance_status(
                    project,
                    managed,
                ),
                "projection": self.memory_projection_status(),
            }
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.agent-memory-maintenance-status.v1",
                "ok": False,
                "error": "local SQLite core required",
                "catalogConsolidation": self._catalog_maintenance_status(
                    project,
                    MemoryMaintenanceSettings(),
                ),
                "projection": self.memory_projection_status(),
            }
        limit = _bounded_int(payload.get("limit"), default=8, minimum=1, maximum=30)
        requested_owner_kind = _string(payload.get("ownerKind"))
        requested_owner_id = _string(payload.get("ownerId"))
        owner_filter: tuple[str, str] | None = None
        if requested_owner_kind or requested_owner_id:
            owner_filter = normalize_memory_owner(
                requested_owner_kind,
                requested_owner_id,
            )
        current_ms = int(time.time() * 1000)
        managed = MemoryMaintenanceSettings.load(
            self.core.db_path,
            preverified_schema=True,
        )
        catalog_status = self._catalog_maintenance_status(project, managed)
        automatic_enabled = managed.automatic_organization_enabled
        automatic_interval_ms = (
            managed.automatic_organization_interval_seconds * 1_000
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            last_event_row = conn.execute(
                """
                SELECT MAX(created_at_ms)
                FROM input_events
                WHERE (? = '' OR project = ? OR project = '')
                """,
                (project, project),
            ).fetchone()
            last_event_ms = int(last_event_row[0] or 0)
            idle_ms = max(0, current_ms - last_event_ms) if last_event_ms else 0
            due, reason, compile_state = memory_compile_due(
                conn,
                project=project,
                idle_ms=idle_ms,
                current_ms=current_ms,
            )
            owner_curation = owner_memory_curation_status(
                conn,
                project=project,
                current_ms=current_ms,
                daily_interval_ms=automatic_interval_ms,
                owner_kind="" if owner_filter is None else owner_filter[0],
                owner_id="" if owner_filter is None else owner_filter[1],
                auto_apply=managed.automatic_organization_auto_apply,
                include_agent_dialogue=managed.include_agent_dialogue,
                canonical_personal=True,
            )
            model_curation = memory_curation_model_status(
                conn,
                limit=min(limit, 8),
            )
            book_projection = personal_memory_book_projection_status(conn)
            rows = conn.execute(
                """
                SELECT r.run_id, r.created_at_ms, r.status, r.summary, r.metadata_json,
                       r.owner_kind, r.owner_id, r.run_kind,
                       (SELECT COUNT(*) FROM memory_cleanup_diffs d WHERE d.run_id = r.run_id) AS diff_count
                FROM memory_cleanup_runs r
                WHERE r.run_id LIKE 'memory_book_%'
                  AND (? = '' OR (r.owner_kind = ? AND r.owner_id = ?))
                ORDER BY r.created_at_ms DESC, r.id DESC
                LIMIT 100
                """,
                (
                    "" if owner_filter is None else owner_filter[0],
                    "" if owner_filter is None else owner_filter[0],
                    "" if owner_filter is None else owner_filter[1],
                ),
            ).fetchall()
        runs: list[dict[str, object]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            if compact_whitespace(str(metadata.get("project") or "")) != project:
                continue
            source_cursor = dict(metadata.get("sourceCursor") or {})
            run_status = str(row["status"] or "")
            try:
                to_event_id = int(source_cursor.get("toEventId") or 0)
            except (TypeError, ValueError):
                to_event_id = 0
            if (
                run_status == "draft"
                and compact_whitespace(str(metadata.get("runKind") or "legacy")) == "legacy"
                and to_event_id > 0
                and to_event_id <= int(compile_state.get("lastCompiledEventId") or 0)
            ):
                run_status = "superseded"
            runs.append(
                {
                    "runId": str(row["run_id"]),
                    "createdAtMs": int(row["created_at_ms"] or 0),
                    "status": run_status,
                    "summary": compact_whitespace(str(row["summary"] or ""))[:240],
                    "diffCount": int(row["diff_count"] or 0),
                    "bundleHash": str(metadata.get("bundleHash") or ""),
                    "sourceCursor": source_cursor,
                    "ownerKind": str(row["owner_kind"] or ""),
                    "ownerId": str(row["owner_id"] or ""),
                    "runKind": str(row["run_kind"] or ""),
                }
            )
            if len(runs) >= limit:
                break
        response = {
            "schemaVersion": "rag-ime.agent-memory-maintenance-status.v1",
            "ok": True,
            "policy": "auto_governed" if automatic_enabled else "disabled",
            "autoApply": managed.automatic_organization_auto_apply,
            "scheduledDraftOnly": (
                automatic_enabled and not managed.automatic_organization_auto_apply
            ),
            # The owner-scoped evidence curator is the authoritative scheduled
            # lane. Legacy compile state remains diagnostic only.
            "due": automatic_enabled and bool(owner_curation.get("due")),
            "dueReason": (
                "automatic_organization_disabled"
                if not automatic_enabled
                else "owner_scheduled"
                if owner_curation.get("due")
                else "not_due"
            ),
            "idleMs": idle_ms,
            "compileState": compile_state,
            "draftCoverage": {
                "coveredThroughEventId": int(
                    compile_state.get("lastDraftedEventId") or 0
                ),
                "undraftedEventCount": int(
                    compile_state.get("undraftedEventCount") or 0
                ),
                "coversAllPending": bool(
                    compile_state.get("draftCoversPending")
                ),
                "lastDraftRunId": str(
                    compile_state.get("lastDraftRunId") or ""
                ),
            },
            "automation": {
                "minimumNewEvents": 50,
                "idleThresholdMs": 20 * 60 * 1000,
                "dailyIntervalMs": automatic_interval_ms,
                "schedulerPollIntervalMs": 60 * 60 * 1000,
                "enabled": automatic_enabled,
                "model": managed.automatic_organization_model,
                "thinkingLevel": managed.automatic_organization_thinking_level,
                "runsPerDay": managed.automatic_organization_runs_per_day,
                "autoApply": managed.automatic_organization_auto_apply,
                "curationProtocol": MEMORY_CURATION_ARCHITECTURE,
                "targetSourceCount": DEFAULT_MAX_SOURCES,
                "maximumSourceCount": MAX_PERSONAL_V2_SOURCES,
                "maximumInputTokens": MAX_PERSONAL_V2_INPUT_TOKENS,
                "reservedContextTokens": (
                    MINIMUM_MEMORY_CONTEXT_TOKENS
                    - MAX_PERSONAL_V2_INPUT_TOKENS
                ),
            },
            "catalogConsolidation": catalog_status,
            "pendingDraftCount": sum(1 for item in runs if item["status"] == "draft"),
            "runs": runs,
            "ownerCuration": owner_curation,
            "modelCuration": model_curation,
            "bookProjection": book_projection,
            "projection": self.memory_projection_status(),
        }
        validate_contract(response, "agent-memory-maintenance-status.v1.json")
        return response

    def rag_core_v3_query_preview(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {"schemaVersion": "rag-ime.rag-core-v3-preview.v1", "ok": False, "error": "local SQLite core required"}
        runtime_config = self.runtime_config_snapshot()
        if not runtime_config.hybrid_rag.enabled or not runtime_config.memory.enabled:
            return {
                "schemaVersion": "rag-ime.rag-core-v3-preview.v1",
                "ok": True,
                "retrieval": {"called": False},
                "lanes": {
                    _camel_lane_name(name): {
                        "enabled": False,
                        "configuredEnabled": enabled,
                        "weight": runtime_config.hybrid_rag.lane_weight(name),
                        "count": 0,
                        "docIds": [],
                    }
                    for name, enabled in runtime_config.hybrid_rag.query_lanes()
                },
                "fusedCandidates": [],
                "blocked": [{"reason": "memory_disabled" if not runtime_config.memory.enabled else "hybrid_rag_disabled"}],
                "deepseekEvidencePack": [],
                "deepseekCandidates": [],
                "elapsedMs": 0,
                "rawTextVisible": self._include_raw_text(),
            }
        query = HybridRagQuery(
            query_text=_string(payload.get("query")) or _string(payload.get("currentInput")),
            raw_input=_string(payload.get("rawInput") or payload.get("currentInput")),
            preedit=_string(payload.get("preedit")),
            committed_tail=_string(payload.get("committedContext") or payload.get("recentContext")),
            rime_candidates=tuple(_string_list(payload.get("rimeCandidates"))),
            project=_string(payload.get("project")) or self.config.project,
            app=_string(payload.get("app")),
            top_k=_bounded_int(payload.get("topK"), default=5, minimum=1, maximum=20),
            latency_budget_ms=_bounded_int(payload.get("latencyBudgetMs"), default=400, minimum=1, maximum=5000),
            enabled_lanes=runtime_config.hybrid_rag.query_lanes(),
            lane_weights=runtime_config.hybrid_rag.query_weights(),
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            payload_result = retrieve_hybrid_rag_candidates(conn, query, self.core.embedding_provider)
        include_text = self._include_raw_text()
        candidates = list(payload_result.get("candidates") or [])
        fused = [_debug_redact_rag_candidate(item, include_text=include_text) for item in candidates if isinstance(item, dict)]
        evidence_pack = _debug_deepseek_evidence_pack(candidates, include_text=include_text)
        lane_breakdown = _debug_lane_breakdown(payload_result.get("lanes"))
        disabled_lanes = sorted(
            _camel_lane_name(name)
            for name, enabled in runtime_config.hybrid_rag.query_lanes()
            if not enabled
        )
        for lane in disabled_lanes:
            lane_breakdown.setdefault(lane, {"count": 0})
            lane_breakdown[lane]["disabledBySettings"] = True
        return {
            "schemaVersion": "rag-ime.rag-core-v3-preview.v1",
            "ok": True,
            "query": _debug_query_preview(payload_result.get("query"), include_text=include_text),
            "lanes": lane_breakdown,
            "fusedCandidates": fused,
            "blocked": [{"lane": lane, "reason": "disabled_by_management_settings"} for lane in disabled_lanes],
            "deepseekEvidencePack": evidence_pack,
            "deepseekCandidates": [],
            "elapsedMs": int(payload_result.get("elapsedMs") or 0),
            "retrieval": {"called": True},
            "rawTextVisible": include_text,
        }

    def rag_core_v3_rebuild_retrieval_docs(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {"schemaVersion": "rag-ime.retrieval-docs-rebuild.v1", "ok": False, "error": "local SQLite core required"}
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            report = rebuild_retrieval_docs(
                conn,
                project=_string(payload.get("project")) or self.config.project,
                include_books=not _bool(payload.get("noBooks"), default=False),
                include_atoms=not _bool(payload.get("noAtoms"), default=False),
                include_phrases=not _bool(payload.get("noPhrases"), default=False),
                include_legacy_items=(
                    _bool(payload.get("includeLegacyItems"), default=False)
                    and not _bool(payload.get("noItems"), default=False)
                ),
            )
        self._clear_rime_cache()
        return {"ok": True, **report}

    def rag_core_v3_memory_book_preview(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {"schemaVersion": "rag-ime.memory-book-preview.v1", "ok": False, "error": "local SQLite core required"}
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            bundle = build_memory_book_source_bundle(
                conn,
                project=_string(payload.get("project")) or self.config.project,
                since_days=_bounded_int(payload.get("sinceDays"), default=7, minimum=1, maximum=365),
                limit=_bounded_int(payload.get("limit"), default=80, minimum=1, maximum=500),
            )
        safe_bundle = _debug_memory_book_source_bundle(bundle, include_text=self._include_raw_text())
        return {
            "schemaVersion": "rag-ime.memory-book-preview.v1",
            "ok": True,
            "dryRun": True,
            "sourceBundle": safe_bundle,
            "rawTextVisible": self._include_raw_text(),
        }

    def rag_core_v3_doc(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {"schemaVersion": "rag-ime.rag-core-v3-doc.v1", "ok": False, "error": "local SQLite core required"}
        doc_id = _string(payload.get("id") or payload.get("docId"))
        if not doc_id:
            return {"schemaVersion": "rag-ime.rag-core-v3-doc.v1", "ok": False, "error": "id is required"}
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute("SELECT * FROM memory_retrieval_docs WHERE doc_id = ? LIMIT 1", (doc_id,)).fetchone()
        if row is None:
            return {"schemaVersion": "rag-ime.rag-core-v3-doc.v1", "ok": False, "error": "doc not found", "docId": doc_id}
        item = {key: row[key] for key in row.keys()}
        item["metadata"] = _json_loads_dict(item.pop("metadata_json", "{}"))
        return {
            "schemaVersion": "rag-ime.rag-core-v3-doc.v1",
            "ok": True,
            "doc": _redact_mapping(item, include_text=self._include_raw_text()),
        }

    def rag_core_v3_tag_graph(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {"schemaVersion": "rag-ime.rag-core-v3-tag-graph.v1", "ok": False, "error": "local SQLite core required"}
        tag = compact_whitespace(_string(payload.get("tag")))
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                """
                SELECT src.tag AS src, dst.tag AS dst, e.edge_type, e.weight, e.direction_bias, e.evidence_count
                FROM memory_tag_edges e
                JOIN memory_tags src ON src.id = e.src_tag_id
                JOIN memory_tags dst ON dst.id = e.dst_tag_id
                WHERE (? = '' OR src.tag = ? OR dst.tag = ?)
                ORDER BY e.weight DESC
                LIMIT ?
                """,
                (tag, tag, tag, _bounded_int(payload.get("limit"), default=50, minimum=1, maximum=200)),
            ).fetchall()
        return {
            "schemaVersion": "rag-ime.rag-core-v3-tag-graph.v1",
            "ok": True,
            "tag": tag,
            "edges": [
                {
                    "src": str(row["src"]),
                    "dst": str(row["dst"]),
                    "edgeType": str(row["edge_type"]),
                    "weight": float(row["weight"] or 0.0),
                    "directionBias": float(row["direction_bias"] or 0.0),
                    "evidenceCount": int(row["evidence_count"] or 0),
                }
                for row in rows
            ],
        }

    def deepseek_completion_preview(self, payload: dict[str, Any]) -> dict[str, object]:
        current_context = _string(payload.get("currentContext") or payload.get("context"))
        selected_text = _string(payload.get("selectedText"))
        sensitive_field, secure_input = _active_rag_secure_flags(payload)
        settings = self.settings_store.get_settings()
        active_settings = settings.get("activeRag") if isinstance(settings.get("activeRag"), dict) else {}
        sensitive_guard_enabled = bool(active_settings.get("sensitiveTextGuard", True))
        if (
            sensitive_field
            or secure_input
            or _bool(payload.get("sensitiveTextGuardHit"), default=False)
            or active_rag_sensitive_text_blocked(
                current_context,
                selected_text,
                guard_enabled=sensitive_guard_enabled,
            )
        ):
            return _sensitive_deepseek_preview_payload()
        evidence_pack = (
            tuple(item for item in payload.get("evidencePack", []) if isinstance(item, dict))
            if isinstance(payload.get("evidencePack"), list)
            else ()
        )
        if not evidence_pack:
            evidence_pack = self._deepseek_preview_evidence_pack(
                current_context=current_context,
                selected_text=selected_text,
                payload=payload,
            )
        config = load_deepseek_config(_string(payload.get("modelEnvPath")) or None)
        route_status = self.active_rag_route_status(local_only=False)
        route_status = {
            **route_status,
            "model": config.model,
            "gates": {
                **dict(route_status.get("gates") or {}),
                "credentialsConfigured": bool(config.api_key),
            },
        }
        expected_token = os.environ.get("RAG_IME_DEEPSEEK_PREVIEW_TOKEN", "")
        provided_token = _string(payload.get("previewToken"))
        debug_override = bool(expected_token and provided_token and provided_token == expected_token)
        route_status["debugOverride"] = debug_override
        route_status["remoteReady"] = bool(all(route_status["gates"].values()))  # type: ignore[union-attr]
        if route_status["remoteReady"]:
            route_status["skipReason"] = ""
        elif not config.api_key:
            route_status["skipReason"] = "credentials_missing"
        context_packet = payload.get("contextPacket") if isinstance(payload.get("contextPacket"), dict) else None
        request = DeepSeekCompletionRequest(
            scene="active_rag",
            current_context=current_context,
            selected_text=selected_text,
            evidence_pack=evidence_pack,
            context_packet=dict(context_packet) if context_packet else None,
            max_candidates=_bounded_int(payload.get("maxCandidates"), default=1, minimum=1, maximum=8),
            max_chars=_bounded_int(
                payload.get("maxChars"),
                default=ACTIVE_RAG_DEFAULT_MAX_CHARS,
                minimum=0,
                maximum=12000,
            ),
            latency_budget_ms=_bounded_int(
                payload.get("latencyBudgetMs"),
                default=120_000,
                minimum=100,
                maximum=300_000,
            ),
        )
        messages = build_deepseek_completion_messages(request)
        include_text = self._include_raw_text()
        request_diagnostics = build_context_injection_trace(
            current_context=current_context,
            selected_text=selected_text,
            evidence_pack=evidence_pack,
            context_packet=context_packet,
            messages=messages,
            include_text=include_text,
        )
        safe_messages: object
        safe_evidence: object
        if include_text:
            safe_messages = messages
            safe_evidence = list(evidence_pack)
        else:
            safe_messages = request_diagnostics["prompt"]["messages"]  # type: ignore[index]
            safe_evidence = request_diagnostics["evidence"]["items"]  # type: ignore[index]
        base_response: dict[str, object] = {
            "schemaVersion": "rag-ime.deepseek-completion-preview.v1",
            "routeStatus": route_status,
            "requestDiagnostics": request_diagnostics,
            "messages": safe_messages,
            "evidencePack": safe_evidence,
        }
        authorized = bool(route_status["remoteReady"] or debug_override)
        if not authorized:
            return {
                **base_response,
                "ok": False,
                "dryRun": _bool(payload.get("dryRun"), default=True),
                "error": f"DeepSeek Active RAG route blocked: {route_status['skipReason']}",
                "requires": (
                    "activeRag.allowRemoteModel=true, privacy.allowRemoteModelForActiveRag=true, "
                    "RAG_IME_DEEPSEEK_ACTIVE_RAG=1, configured credentials, or previewToken"
                ),
                "candidates": [],
            }
        if _bool(payload.get("dryRun"), default=True):
            return {
                **base_response,
                "ok": True,
                "dryRun": True,
                "candidates": [],
            }
        provider = DeepSeekV4FlashCompletionProvider(config, enforce_runtime_flags=False)
        candidates: list[dict[str, object]] = []
        stream_events: list[dict[str, object]] = []
        started = time.perf_counter()
        try:
            for item in provider.stream_candidates(request):
                payload_item = item.__dict__
                candidates.append(payload_item)
                stream_events.append(
                    {
                        "text": item.text,
                        "insertText": item.insert_text,
                        "sourceLane": item.source_lane,
                        "done": item.done,
                        "elapsedMs": item.metadata.get("elapsedMs"),
                        "metadata": dict(item.metadata),
                    }
                )
        except Exception as exc:
            return {
                **base_response,
                "ok": False,
                "dryRun": False,
                "remoteModel": {
                    "requested": True,
                    "allowed": authorized,
                    "provider": "deepseek",
                    "model": config.model,
                    "skipReason": type(exc).__name__,
                    "failureReason": _safe_debug_error(exc),
                    "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
                },
                "streamEvents": [],
                "candidates": [],
            }
        return {
            **base_response,
            "ok": True,
            "dryRun": False,
            "remoteModel": {
                "requested": True,
                "allowed": authorized,
                "provider": "deepseek",
                "model": config.model,
                "skipReason": "",
                "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
            },
            "streamEvents": _redact_mapping({"items": stream_events}, include_text=include_text)["items"],
            "candidates": _redact_mapping({"items": candidates}, include_text=include_text)["items"],
        }

    def _deepseek_preview_evidence_pack(
        self,
        *,
        current_context: str,
        selected_text: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, object], ...]:
        query = compact_whitespace(_string(payload.get("query")) or current_context or selected_text)
        if not query:
            return ()
        runtime_config = self.runtime_config_snapshot()
        if (
            not runtime_config.active_rag.enabled
            or not runtime_config.hybrid_rag.enabled
            or not runtime_config.memory.enabled
        ):
            return ()
        top_k = _bounded_int(payload.get("evidenceTopK"), default=8, minimum=1, maximum=20)
        try:
            if isinstance(self.core, LocalSqliteCoreClient):
                candidates = self.core.retrieve_candidates_v3(
                    current_input=query,
                    recent_context=compact_whitespace(selected_text or current_context),
                    project=_string(payload.get("project")) or self.config.project,
                    app=_string(payload.get("app")),
                    top_k=top_k,
                    source_budget_ms=runtime_config.hybrid_rag.budget_ms,
                    enabled_lanes=runtime_config.hybrid_rag.query_lanes(),
                    lane_weights=runtime_config.hybrid_rag.query_weights(),
                )
                suggestions = memory_candidates_v2_to_input_suggestions(candidates)
                if not suggestions and all(
                    enabled
                    for lane, enabled in runtime_config.hybrid_rag.query_lanes()
                    if lane not in {"vector_raw", "vector_tag_boost"}
                ):
                    # Preserve the established legacy evidence fallback only
                    # when every implemented lane is enabled. A customized
                    # lane policy must never be bypassed by that fallback.
                    suggestions = self.adapter.suggest(
                        SuggestionRequest(
                            current_input=query,
                            recent_context=compact_whitespace(selected_text or current_context),
                            project=_string(payload.get("project")) or self.config.project,
                            app=_string(payload.get("app")),
                            top_k=top_k,
                        )
                    )
            else:
                suggestions = self.adapter.suggest(
                    SuggestionRequest(
                        current_input=query,
                        recent_context=compact_whitespace(selected_text or current_context),
                        project=_string(payload.get("project")) or self.config.project,
                        app=_string(payload.get("app")),
                        top_k=top_k,
                    )
                )
        except Exception:
            return ()
        return tuple(_deepseek_evidence_from_suggestion(item) for item in suggestions[:top_k])

    def memory_tombstone(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.memory-tombstone.v1",
                "ok": False,
                "error": "memory tombstone is only available for local SQLite core",
            }
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        try:
            tombstone = self.core.add_memory_tombstone(
                target_type=_string(payload.get("targetType")) or "memory_id",
                target_value=_string(payload.get("targetValue")),
                reason=_string(payload.get("reason")) or "manual",
                metadata=metadata,
                active=_bool(payload.get("active"), default=True),
            )
        except ValueError as exc:
            return {
                "schemaVersion": "rag-ime.memory-tombstone.v1",
                "ok": False,
                "error": str(exc),
            }
        self._clear_rime_cache()
        return {
            "schemaVersion": "rag-ime.memory-tombstone.v1",
            "ok": True,
            **tombstone,
        }

    def memory_cleanup_diff_apply(self, diff_id: int) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.memory-cleanup-diff.v1",
                "ok": False,
                "error": "cleanup diffs are only available for local SQLite core",
            }
        try:
            report = self.core.apply_memory_cleanup_diff(diff_id=diff_id)
        except ValueError as exc:
            return {
                "schemaVersion": "rag-ime.memory-cleanup-diff.v1",
                "ok": False,
                "error": str(exc),
            }
        self._clear_rime_cache()
        return {"ok": True, **report}

    def memory_cleanup_diff_rollback(self, diff_id: int) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.memory-cleanup-diff.v1",
                "ok": False,
                "error": "cleanup diffs are only available for local SQLite core",
            }
        try:
            report = self.core.rollback_memory_cleanup_diff(diff_id=diff_id)
        except ValueError as exc:
            return {
                "schemaVersion": "rag-ime.memory-cleanup-diff.v1",
                "ok": False,
                "error": str(exc),
            }
        self._clear_rime_cache()
        return {"ok": True, **report}

    def candidate_explain(self, payload: dict[str, Any]) -> dict[str, object]:
        query = _string(payload.get("query") or payload.get("currentInput")).strip()
        recent_context = _string(payload.get("recentContext") or payload.get("recent_context"))
        top_k = _bounded_int(payload.get("topK"), default=5, minimum=1, maximum=10)
        if not query:
            return {
                "schemaVersion": "rag-ime.management-candidate-explain.v1",
                "ok": False,
                "error": "query is required",
                "candidates": [],
            }
        response = self.suggest(
            {
                "currentInput": query,
                "recentContext": recent_context,
                "project": _string(payload.get("project")) or self.config.project,
                "topK": top_k,
                "app": _string(payload.get("app")),
            }
        )
        suggestions = response.get("suggestions") if isinstance(response.get("suggestions"), list) else []
        model_predictions = response.get("modelPredictions") if isinstance(response.get("modelPredictions"), list) else []
        candidates: list[dict[str, object]] = []
        for rank, item in enumerate(model_predictions[:top_k], start=1):
            if not isinstance(item, dict):
                continue
            candidates.append(
                {
                    "rank": rank,
                    "text": _string(item.get("text")),
                    "sourceType": "model",
                    "lane": "model",
                    "score": float(item.get("confidence") or 0.0),
                    "penalty": 0.0,
                    "reason": _string(item.get("providerName") or item.get("provider_name")) or "model prediction",
                    "diagnostics": _redact_mapping(dict(item.get("metadata") or {}), include_text=self._include_raw_text()),
                }
            )
        for index, item in enumerate(suggestions[:top_k], start=1):
            if not isinstance(item, dict):
                continue
            metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
            score_breakdown = metadata.get("scoreBreakdown") or metadata.get("score_breakdown") or {}
            source_type = _string(metadata.get("source_type")) or _string(item.get("suggestionType")) or "rag"
            candidates.append(
                {
                    "rank": len(candidates) + 1,
                    "text": _string(item.get("surfaceText") or item.get("text")),
                    "sourceType": source_type,
                    "lane": "rag" if source_type in {"rag", "memory", "stable_memory"} else source_type,
                    "score": float(item.get("confidence") or 0.0),
                    "penalty": _score_penalty(score_breakdown),
                    "reason": _string(metadata.get("reason")) or _string(item.get("evidencePreview"))[:80] or "local memory",
                    "memoryId": _string(item.get("memoryId")),
                    "sourceEventId": int(item.get("sourceEventId") or 0),
                    "diagnostics": _redact_mapping(metadata, include_text=self._include_raw_text()),
                }
            )
        return {
            "schemaVersion": "rag-ime.management-candidate-explain.v1",
            "ok": True,
            "project": _string(payload.get("project")) or self.config.project,
            "queryHash": _stable_debug_hash(query),
            "queryPreview": _privacy_preview(query),
            "rawTextVisible": self._include_raw_text(),
            "candidates": candidates,
        }

    def management_history(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.management-history.v1",
                "ok": False,
                "error": "history audit is only available for local SQLite core",
                "items": [],
            }
        report = self.core.list_memory_events(
            project=_string(payload.get("project")) or self.config.project,
            query=_string(payload.get("query")),
            source=_string(payload.get("source")),
            include_deleted=_bool(payload.get("includeDeleted"), default=False),
            generated_only=_bool(payload.get("generatedOnly"), default=False),
            limit=_bounded_int(payload.get("limit"), default=100, minimum=1, maximum=500),
        )
        include_text = self._include_raw_text()
        items = [
            _redact_history_item(item, include_text=include_text)
            for item in report.get("items", [])
            if isinstance(item, dict)
        ]
        return {
            "schemaVersion": "rag-ime.management-history.v1",
            "ok": True,
            "project": report.get("project"),
            "queryHash": _stable_debug_hash(_string(payload.get("query"))),
            "limit": report.get("limit"),
            "rawTextVisible": include_text,
            "totals": report.get("totals", {}),
            "items": items,
        }

    def management_history_tombstone(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.management-history-tombstone.v1",
                "ok": False,
                "error": "history tombstone is only available for local SQLite core",
            }
        event_id = _optional_int(payload.get("eventId"))
        memory_id = _string(payload.get("memoryId")) or (f"event:{event_id}" if event_id else "")
        if not memory_id:
            return {
                "schemaVersion": "rag-ime.management-history-tombstone.v1",
                "ok": False,
                "error": "eventId or memoryId is required",
            }
        result = self.memory_tombstone(
            {
                "targetType": "memory_id",
                "targetValue": memory_id,
                "reason": _string(payload.get("reason")) or "debug-management-history",
                "metadata": {"source": "debug-management", "eventId": event_id or 0},
            }
        )
        audit_id = self._record_management_audit(
            action="history_tombstone",
            target_type="history",
            target_id=memory_id,
            payload=payload,
            result=result,
        )
        return {
            "schemaVersion": "rag-ime.management-history-tombstone.v1",
            "ok": bool(result.get("ok")),
            "auditId": audit_id,
            "result": result,
        }

    def management_memories(self, payload: dict[str, Any]) -> dict[str, object]:
        report = self._management_memory_items(payload, lexicon=False)
        report["schemaVersion"] = "rag-ime.management-memories.v1"
        return report

    def management_lexicon(self, payload: dict[str, Any]) -> dict[str, object]:
        report = self._management_memory_items(payload, lexicon=True)
        report["schemaVersion"] = "rag-ime.management-lexicon.v1"
        return report

    def management_lexicon_export_rime(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.management-lexicon-rime-export.v1",
                "ok": False,
                "error": "lexicon export is only available for local SQLite core",
            }
        dry_run = _bool(payload.get("dryRun"), default=True)
        if not dry_run:
            return {
                "schemaVersion": "rag-ime.management-lexicon-rime-export.v1",
                "ok": False,
                "error": "Rime dictionary writes are not implemented; run dryRun first",
                "dryRun": False,
            }
        status = _string(payload.get("status")) or "approved"
        allow_non_approved = _bool(payload.get("allowNonApproved"), default=False)
        if status != "approved" and not allow_non_approved:
            return {
                "schemaVersion": "rag-ime.management-lexicon-rime-export.v1",
                "ok": False,
                "error": 'lexicon export requires status="approved" unless allowNonApproved=true',
                "requiredStatus": "approved",
            }
        kind = _string(payload.get("kind")) or "phrase"
        limit = _bounded_int(payload.get("limit"), default=200, minimum=1, maximum=500)
        project = _string(payload.get("project")) or self.config.project
        report = self.core.inspect_memory_v2(
            project=project,
            limit=limit,
            kind=kind,
            status=status if status != "all" else "",
        )
        entries = [
            _rime_lexicon_export_entry(item)
            for item in report.get("items", [])
            if isinstance(item, dict) and _string(item.get("text"))
        ]
        text = _rime_lexicon_export_text(project=project, status=status, entries=entries)
        return {
            "schemaVersion": "rag-ime.management-lexicon-rime-export.v1",
            "ok": True,
            "dryRun": True,
            "applySupported": False,
            "project": project,
            "kind": kind,
            "status": status,
            "format": "rag-ime-rime-custom-phrase-preview.tsv",
            "formatDescription": "dry-run preview: phrase<TAB>weight<TAB>memory_id",
            "entryCount": len(entries),
            "entries": entries,
            "text": text,
            "rawTextVisible": True,
        }

    def management_memory_action(self, payload: dict[str, Any]) -> dict[str, object]:
        return self._management_item_action(payload, lexicon=False)

    def management_lexicon_action(self, payload: dict[str, Any]) -> dict[str, object]:
        return self._management_item_action(payload, lexicon=True)

    def management_cleanup_diff(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.management-cleanup-diff.v1",
                "ok": False,
                "error": "cleanup diff review is only available for local SQLite core",
            }
        diff_id = _optional_int(payload.get("id") or payload.get("diffId"))
        if diff_id:
            try:
                with self.core._connect() as conn:  # type: ignore[attr-defined]
                    row = _cleanup_diff_payload_for_debug(conn, diff_id=diff_id)
            except ValueError as exc:
                return {"schemaVersion": "rag-ime.management-cleanup-diff.v1", "ok": False, "error": str(exc)}
            return {"schemaVersion": "rag-ime.management-cleanup-diff.v1", "ok": True, "diff": row}
        runs = self.core.list_memory_cleanup_runs(
            limit=_bounded_int(payload.get("limit"), default=20, minimum=1, maximum=100),
            run_id=_string(payload.get("runId")),
            status=_string(payload.get("status")),
        )
        return {
            "schemaVersion": "rag-ime.management-cleanup-diff.v1",
            "ok": True,
            **runs,
        }

    def management_cleanup_diff_apply(self, payload: dict[str, Any]) -> dict[str, object]:
        diff_id = _optional_int(payload.get("id") or payload.get("diffId"))
        confirmation = self._management_cleanup_confirmation(payload, expected="apply")
        if confirmation:
            return confirmation
        result = self.memory_cleanup_diff_apply(diff_id or 0)
        audit_id = self._record_management_audit(
            action="cleanup_diff_apply",
            target_type="cleanup_diff",
            target_id=str(diff_id or ""),
            payload=payload,
            result=result,
        )
        return {
            "schemaVersion": "rag-ime.management-cleanup-diff-action.v1",
            "ok": bool(result.get("ok")),
            "auditId": audit_id,
            "result": result,
        }

    def management_cleanup_diff_rollback(self, payload: dict[str, Any]) -> dict[str, object]:
        diff_id = _optional_int(payload.get("id") or payload.get("diffId"))
        confirmation = self._management_cleanup_confirmation(payload, expected="rollback")
        if confirmation:
            return confirmation
        result = self.memory_cleanup_diff_rollback(diff_id or 0)
        audit_id = self._record_management_audit(
            action="cleanup_diff_rollback",
            target_type="cleanup_diff",
            target_id=str(diff_id or ""),
            payload=payload,
            result=result,
        )
        return {
            "schemaVersion": "rag-ime.management-cleanup-diff-action.v1",
            "ok": bool(result.get("ok")),
            "auditId": audit_id,
            "result": result,
        }

    def _management_cleanup_confirmation(self, payload: dict[str, Any], *, expected: str) -> dict[str, object]:
        confirm = _string(payload.get("confirm"))
        if confirm == expected:
            return {}
        return {
            "schemaVersion": "rag-ime.management-cleanup-diff-action.v1",
            "ok": False,
            "error": f'confirmation required: set confirm="{expected}"',
            "requiredConfirm": expected,
        }

    def _management_memory_items(self, payload: dict[str, Any], *, lexicon: bool) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "ok": False,
                "error": "memory item review is only available for local SQLite core",
                "items": [],
            }
        status = _string(payload.get("status")) or "pending"
        kind = _string(payload.get("kind"))
        if lexicon and not kind:
            kind = "phrase"
        report = self.core.inspect_memory_v2(
            project=_string(payload.get("project")) or self.config.project,
            limit=_bounded_int(payload.get("limit"), default=100, minimum=1, maximum=200),
            kind=kind,
            status=status if status != "all" else "",
        )
        include_text = self._include_raw_text()
        items = [
            _redact_memory_item(item, include_text=include_text, lexicon=lexicon)
            for item in report.get("items", [])
            if isinstance(item, dict)
        ]
        return {
            "ok": True,
            "project": report.get("project"),
            "status": status,
            "kind": kind,
            "rawTextVisible": include_text,
            "items": items,
        }

    def _management_item_action(self, payload: dict[str, Any], *, lexicon: bool) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.management-item-action.v1",
                "ok": False,
                "error": "memory item action is only available for local SQLite core",
            }
        memory_id = _string(payload.get("memoryId") or payload.get("id"))
        action = _management_action(_string(payload.get("action") or payload.get("actionType")))
        if not memory_id:
            return {
                "schemaVersion": "rag-ime.management-item-action.v1",
                "ok": False,
                "error": "memoryId is required",
            }
        if action not in {"approve", "reject", "tombstone", "hide", "restore", "pin", "downrank"}:
            return {
                "schemaVersion": "rag-ime.management-item-action.v1",
                "ok": False,
                "error": f"unsupported action: {action}",
            }
        if action == "tombstone":
            result = self.memory_tombstone(
                {
                    "targetType": "memory_id",
                    "targetValue": memory_id,
                    "reason": _string(payload.get("reason")) or "debug-management-item",
                    "metadata": {"source": "debug-management", "lexicon": lexicon},
                }
            )
        else:
            result = self._update_memory_item_status(memory_id=memory_id, action=action, payload=payload)
        audit_id = self._record_management_audit(
            action=("lexicon_" if lexicon else "memory_") + action,
            target_type="lexicon" if lexicon else "memory",
            target_id=memory_id,
            payload=payload,
            result=result,
        )
        self._clear_rime_cache()
        return {
            "schemaVersion": "rag-ime.management-item-action.v1",
            "ok": bool(result.get("ok")),
            "auditId": audit_id,
            "result": result,
        }

    def _update_memory_item_status(self, *, memory_id: str, action: str, payload: dict[str, Any]) -> dict[str, object]:
        status_by_action = {
            "approve": "approved",
            "reject": "rejected",
            "hide": "hidden",
            "restore": "active",
            "pin": "approved",
            "downrank": "active",
        }
        status = status_by_action[action]
        metadata_update = {
            "lastManagementAction": action,
            "managementReason": _string(payload.get("reason")),
            "managedAtMs": now_ms(),
        }
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT metadata_json FROM memory_items WHERE memory_id = ? LIMIT 1",
                (memory_id,),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": f"memory item not found: {memory_id}", "memoryId": memory_id}
            metadata = _json_loads_dict(row["metadata_json"])
            metadata.update({key: value for key, value in metadata_update.items() if value not in ("", None)})
            quality_expr = "quality_score"
            if action == "pin":
                quality_expr = "MIN(1.0, quality_score + 0.12)"
                metadata["pinned"] = True
            elif action == "downrank":
                quality_expr = "MAX(0.05, quality_score - 0.12)"
                metadata["downranked"] = True
            conn.execute(
                f"""
                UPDATE memory_items
                SET status = ?, quality_score = {quality_expr}, updated_at_ms = ?, metadata_json = ?
                WHERE memory_id = ?
                """,
                (
                    status,
                    now_ms(),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    memory_id,
                ),
            )
        return {"ok": True, "memoryId": memory_id, "action": action, "status": status}

    def _include_raw_text(self) -> bool:
        settings = self.settings_store.get_settings(include_sensitive=True)
        privacy = settings.get("privacy") if isinstance(settings.get("privacy"), dict) else {}
        return bool(
            self.config.include_raw_text
            or privacy.get("debugIncludeText") is True
            or os.environ.get("RAG_IME_TRACE_INCLUDE_TEXT") == "1"
            or os.environ.get("RAG_IME_DEBUG_INCLUDE_TEXT") == "1"
        )

    def _include_active_rag_trace_text(self) -> bool:
        settings = self.settings_store.get_settings(include_sensitive=True)
        privacy = settings.get("privacy") if isinstance(settings.get("privacy"), dict) else {}
        return bool(
            self.config.include_raw_text
            or privacy.get("traceIncludeText") is True
            or os.environ.get("RAG_IME_TRACE_INCLUDE_TEXT") == "1"
        )

    def _record_management_audit(
        self,
        *,
        action: str,
        target_type: str,
        target_id: str,
        payload: dict[str, Any],
        result: dict[str, object],
    ) -> int:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return 0
        safe_payload = _redact_mapping(payload, include_text=False)
        safe_result = _redact_mapping(result, include_text=False)
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            _ensure_management_audit_schema(conn)
            cur = conn.execute(
                """
                INSERT INTO management_audit_log(
                    created_at_ms, action, target_type, target_id, payload_json, result_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ms(),
                    action,
                    target_type,
                    target_id,
                    json.dumps(safe_payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(safe_result, ensure_ascii=False, sort_keys=True),
                ),
            )
            return int(cur.lastrowid)

    def organize_rag_database(self, payload: dict[str, Any]) -> dict[str, object]:
        organizer = getattr(self.core, "organize_rag_database", None)
        if not callable(organizer):
            return {
                "schemaVersion": "rag-ime.rag-db-organize.v1",
                "ok": False,
                "error": "core does not support RAG database organization",
            }
        report = organizer(
            project=_string(payload.get("project")) or self.config.project,
            dry_run=_bool(payload.get("dryRun"), default=False),
            min_generated_accepts=_bounded_int(payload.get("minGeneratedAccepts"), default=3, minimum=1, maximum=100),
            sample_size=_bounded_int(payload.get("sampleSize"), default=12, minimum=0, maximum=50),
        )
        if not report.get("dryRun"):
            self._clear_rime_cache()
        return {"schemaVersion": "rag-ime.rag-db-organize.v1", "ok": True, **report}

    def generate_memory(self, payload: dict[str, Any]) -> dict[str, object]:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return {
                "schemaVersion": "rag-ime.generated-memory.v1",
                "ok": False,
                "error": "generated memory writeback is only available for local SQLite core",
            }
        project = _string(payload.get("project")) or self.config.project
        source_text = self._memory_generation_source_text(payload, project=project)
        if not compact_whitespace(source_text):
            return {
                "schemaVersion": "rag-ime.generated-memory.v1",
                "ok": False,
                "error": "text or eventIds are required",
            }
        recent_context = _string(payload.get("recentContext"))
        dry_run = _bool(payload.get("dryRun"), default=False)
        allow_duplicates = _bool(payload.get("allowDuplicates"), default=False)
        try:
            generator = VcpRebuildMemoryGenerator.from_env_path(
                _string(payload.get("modelEnvPath")) or _string(payload.get("vcpEnvPath")) or None
            )
            report = generator.generate(
                text=source_text,
                recent_context=recent_context,
                project=project,
                max_items=_bounded_int(payload.get("maxItems"), default=3, minimum=1, maximum=8),
            )
        except MemoryGenerationError as exc:
            return {
                "schemaVersion": "rag-ime.generated-memory.v1",
                "ok": False,
                "error": str(exc),
            }
        recorded: list[dict[str, object]] = []
        duplicate_skipped = 0
        provider_tag = report.provider
        for item in report.items:
            dedupe_tag = generated_memory_dedupe_tag(item.text)
            if not allow_duplicates and self.core.has_event_tag(dedupe_tag):
                duplicate_skipped += 1
                continue
            tags = tuple(
                dict.fromkeys(
                    (
                        "generated-memory",
                        provider_tag,
                        "aimemo",
                        dedupe_tag,
                        *item.tags,
                    )
                )
            )
            event_id = ""
            if not dry_run:
                event_id = self.adapter.commit_text(
                    item.text,
                    recent_context=generated_memory_context(source_text, recent_context, item.reason),
                    project=project,
                    app=_string(payload.get("app")) or "debug-memory-console",
                    privacy_disposition="allowed",
                    source="api_memory_generator",
                    provider_name=f"{report.provider}:{report.model}",
                    tags=tags,
                )
            recorded.append(
                {
                    "eventId": event_id,
                    "text": item.text,
                    "tags": list(tags),
                    "importance": item.importance,
                    "reason": item.reason,
                }
            )
        if not dry_run and recorded:
            self._clear_rime_cache()
        return {
            "schemaVersion": "rag-ime.generated-memory.v1",
            "ok": True,
            "dryRun": dry_run,
            "provider": report.provider,
            "model": report.model,
            "elapsedMs": report.elapsed_ms,
            "generated": len(report.items),
            "recorded": 0 if dry_run else len(recorded),
            "duplicateSkipped": duplicate_skipped,
            "items": recorded,
            "metadata": report.metadata,
        }

    def _memory_generation_source_text(self, payload: dict[str, Any], *, project: str) -> str:
        text = _string(payload.get("text"))
        if text:
            return text
        event_ids = [_optional_int(item) for item in payload.get("eventIds", [])] if isinstance(payload.get("eventIds"), list) else []
        event_ids = [item for item in event_ids if item]
        if not event_ids or not isinstance(self.core, LocalSqliteCoreClient):
            return ""
        report = self.core.list_memory_events(project=project, include_deleted=False, limit=500)
        by_id = {int(item["eventId"]): item for item in report.get("items", []) if isinstance(item, dict) and item.get("eventId")}
        chunks: list[str] = []
        for event_id in event_ids[:20]:
            item = by_id.get(event_id)
            if not item:
                continue
            chunks.append(f"- {item.get('text')} | context: {item.get('recentContext')}")
        return "\n".join(chunks)

    def input_source_status(self) -> dict[str, object]:
        script = self._input_source_check_script()
        payload: dict[str, object] = {
            "schemaVersion": "rag-ime.debug-input-source.v1",
            "inputSourceId": self.config.input_source_id,
            "script": str(script),
            "available": False,
            "ok": False,
            "typingReady": False,
            "readinessState": "unavailable",
            "readinessMessage": "input source check script is not available",
        }
        if script is None or not script.exists():
            return {**payload, "error": "input source check script is not available"}
        args = [str(script)]
        if self.config.input_source_require_hitoolbox:
            args.append("--require-hitoolbox-enabled")
        args.append(self.config.input_source_id)
        try:
            completed = subprocess.run(
                args,
                check=False,
                text=True,
                capture_output=True,
                timeout=5,
            )
        except Exception as exc:
            return {
                **payload,
                "available": True,
                "readinessState": "error",
                "readinessMessage": "input source check failed",
                "error": str(exc),
            }
        output = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part)
        parsed = _parse_input_source_check_output(output)
        ok = completed.returncode == 0
        typing_ready = bool(parsed.get("selected"))
        readiness = _input_source_readiness(parsed, ok=ok, typing_ready=typing_ready)
        return {
            **payload,
            "available": True,
            "ok": ok,
            "typingReady": ok and typing_ready,
            **readiness,
            "exitCode": completed.returncode,
            "rawOutput": output,
            **parsed,
        }

    def predictor_ttfc(self, payload: dict[str, Any]) -> dict[str, object]:
        cases = self._predictor_ttfc_cases(payload)
        repeat = _bounded_int(payload.get("repeat"), default=3, minimum=1, maximum=50)
        max_candidates = _bounded_int(payload.get("maxCandidates"), default=3, minimum=1, maximum=10)
        latency_budget_ms = _bounded_int(payload.get("latencyBudgetMs"), default=200, minimum=1, maximum=20_000)
        report = benchmark_streaming_ttft_provider(
            self.predictor,
            cases,
            max_candidates=max_candidates,
            repeat=repeat,
            latency_budget_ms=latency_budget_ms,
        )
        return {
            "schemaVersion": "rag-ime.debug-predictor-ttfc.v1",
            "project": self.config.project,
            "latencyBudgetMs": latency_budget_ms,
            "repeat": {
                "requested": repeat,
                "baseCaseCount": len(cases),
                "effectiveCaseCount": len(cases) * repeat,
            },
            "predictor": prediction_provider_status(self.predictor),
            "benchmark": report,
        }

    def cache_probe(self, payload: dict[str, Any]) -> dict[str, object]:
        current_input = _string(payload.get("currentInput")).strip() or "RAG 输入法"
        recent_context = _string(payload.get("recentContext"))
        project = _string(payload.get("project")) or self.config.project
        repeat = _bounded_int(payload.get("repeat"), default=3, minimum=2, maximum=20)
        top_k = _bounded_int(payload.get("topK"), default=5, minimum=1, maximum=10)
        before = self.health()
        suggest_samples: list[dict[str, object]] = []
        for index in range(repeat):
            response = self.suggest(
                {
                    "currentInput": current_input,
                    "recentContext": recent_context,
                    "project": project,
                    "topK": top_k,
                }
            )
            if index in (0, repeat - 1):
                suggest_samples.append(
                    {
                        "iteration": index + 1,
                        "suggestionCount": len(response.get("suggestions", [])),
                        "modelPredictionCount": len(response.get("modelPredictions", [])),
                        "topSuggestions": [
                            str(item.get("surfaceText") or "")
                            for item in response.get("suggestions", [])[:3]
                            if isinstance(item, dict)
                        ],
                    }
                )

        rime_payload = self._cache_probe_rime_payload(
            payload,
            current_input=current_input,
            recent_context=recent_context,
            project=project,
        )
        rime_samples: list[dict[str, object]] = []
        for index in range(repeat):
            request_payload = copy.deepcopy(rime_payload)
            request_payload["sessionId"] = f"cache-probe-{index + 1}"
            request_payload["requestSeq"] = index + 1
            response = self.rime_suggest(request_payload)
            cache = response.get("cache") if isinstance(response.get("cache"), dict) else {}
            diagnostics = response.get("rankingDiagnostics") if isinstance(response.get("rankingDiagnostics"), dict) else {}
            rime_samples.append(
                {
                    "iteration": index + 1,
                    "hit": bool(cache.get("hit")) if isinstance(cache, dict) else False,
                    "inFlightHit": bool(cache.get("inFlightHit")) if isinstance(cache, dict) else False,
                    "cacheKey": str(cache.get("key") or "") if isinstance(cache, dict) else "",
                    "displayCandidateCount": len(response.get("displayCandidates", [])),
                    "queryBasis": str(response.get("queryBasis") or ""),
                    "sourceCounts": dict(diagnostics.get("sourceCounts") or {}),
                    "hasRagScoreBreakdown": bool(diagnostics.get("hasRagScoreBreakdown")),
                    "topCandidate": _ranking_top_candidate_summary(diagnostics),
                }
            )
        after = self.health()
        suggestion_delta = _cache_stats_delta(before.get("suggestionCache"), after.get("suggestionCache"))
        rime_delta = _cache_stats_delta(before.get("rimeSuggestCache"), after.get("rimeSuggestCache"))
        expected_warm_hits = repeat - 1
        suggestion_hit_delta = int(suggestion_delta.get("hitsDelta") or 0)
        rime_hit_delta = int(rime_delta.get("hitsDelta") or 0)
        return {
            "schemaVersion": "rag-ime.debug-cache-probe.v1",
            "project": project,
            "currentInput": current_input,
            "recentContextLength": len(recent_context),
            "repeat": repeat,
            "expectedWarmHits": expected_warm_hits,
            "suggestionCache": suggestion_delta,
            "rimeSuggestCache": rime_delta,
            "summary": {
                "suggestionCacheHitDelta": suggestion_hit_delta,
                "rimeCacheHitDelta": rime_hit_delta,
                "suggestionCachePassed": _cache_delta_passed(suggestion_delta, expected_warm_hits),
                "rimeCachePassed": _cache_delta_passed(rime_delta, expected_warm_hits),
            },
            "samples": {
                "suggest": suggest_samples,
                "rimeSuggest": rime_samples,
            },
        }

    def prediction_live_trace(self, payload: dict[str, Any]) -> dict[str, object]:
        limit = _bounded_int(payload.get("limit"), default=100, minimum=1, maximum=500)
        session_id = _string(payload.get("sessionId"))
        with self._rime_cache_lock:
            frames = list(self._prediction_live_trace)
        if session_id:
            frames = [item for item in frames if _string(item.get("sessionId")) == session_id]
        frames = frames[-limit:]
        return {
            "schemaVersion": "rag-ime.prediction-live-trace.v1",
            "ok": True,
            "limit": limit,
            "count": len(frames),
            "rawTextVisible": self._include_raw_text(),
            "frames": frames,
            "dropStats": _prediction_drop_stats(frames),
        }

    def _resolve_external_common_trace(self, trace_id: str) -> TraceEnvelope | None:
        """Resolve source-owned traces without copying their persistence."""

        browser_prefix = "trace:browser:command:"
        if trace_id.startswith(browser_prefix):
            command_id = trace_id.removeprefix(browser_prefix)
            trace_reader = getattr(self.browser_control, "trace", None)
            if callable(trace_reader):
                try:
                    return envelope_from_browser_trace(trace_reader(command_id))
                except (BrowserControlError, KeyError, TraceContractError):
                    pass

            # The release Browser boundary exposes the bounded list endpoint
            # but may not have the exact-record reader yet. Keep that boundary
            # read-only and correlate only the requested command id; never
            # project the first/most-recent browser command as this trace.
            traces_reader = getattr(self.browser_control, "traces", None)
            if not callable(traces_reader):
                return None
            try:
                traces_payload = traces_reader(limit=_BROWSER_TRACE_RESOLUTION_LIMIT)
            except (BrowserControlError, TypeError, ValueError):
                return None
            if not isinstance(traces_payload, Mapping):
                return None
            raw_items = traces_payload.get("items")
            if not isinstance(raw_items, list):
                return None
            matching_trace = next(
                (
                    item
                    for item in raw_items
                    if isinstance(item, Mapping)
                    and _string(item.get("commandId")) == command_id
                ),
                None,
            )
            if matching_trace is None:
                return None
            try:
                return envelope_from_browser_trace(matching_trace)
            except (KeyError, TraceContractError):
                return None

        prediction_prefix = "trace:prediction:"
        if not trace_id.startswith(prediction_prefix):
            return None
        identity = trace_id.removeprefix(prediction_prefix)
        if ":request-" not in identity:
            return None
        session_id, request_text = identity.rsplit(":request-", 1)
        if not session_id or not request_text.isdigit():
            return None
        request_seq = int(request_text)
        with self._rime_cache_lock:
            frame = next(
                (
                    item
                    for item in reversed(self._prediction_live_trace)
                    if _string(item.get("sessionId")) == session_id
                    and _bounded_int(
                        item.get("requestSeq"),
                        default=0,
                        minimum=0,
                        maximum=2**63 - 1,
                    )
                    == request_seq
                ),
                None,
            )
        if frame is None:
            return None
        try:
            envelope = envelope_from_prediction_frame(frame)
        except TraceContractError:
            return None
        return envelope if envelope.trace_id == trace_id else None

    def prediction_drop_stats(self, payload: dict[str, Any]) -> dict[str, object]:
        limit = _bounded_int(payload.get("limit"), default=500, minimum=1, maximum=1000)
        with self._rime_cache_lock:
            frames = list(self._prediction_live_trace)[-limit:]
        return {
            "schemaVersion": "rag-ime.prediction-drop-stats.v1",
            "ok": True,
            "limit": limit,
            "frameCount": len(frames),
            **_prediction_drop_stats(frames),
        }

    def seed(self) -> dict[str, object]:
        event_ids = seed_demo_memories(self.adapter, default_fixture_memories())
        self._clear_rime_cache()
        return {
            "ok": True,
            "seeded": len(event_ids),
            "eventCount": self._event_count(),
        }

    def suggest(self, payload: dict[str, Any]) -> dict[str, object]:
        current_input = _string(payload.get("currentInput"))
        recent_context = _string(payload.get("recentContext"))
        project = _string(payload.get("project")) or self.config.project
        app = _string(payload.get("app") or payload.get("frontmostApp"))
        top_k = _bounded_int(payload.get("topK"), default=5, minimum=1, maximum=10)
        prediction_context = build_prediction_context(
            self.core,
            explicit_recent_context=recent_context,
            project=project,
        )
        model_predictions = self.predictor.predict(
            current_input=current_input,
            recent_context=prediction_context,
            max_candidates=5,
        )
        suggestions = self.adapter.suggest(
            SuggestionRequest(
                current_input=current_input,
                recent_context=prediction_context,
                project=project,
                app=app,
                top_k=top_k,
            )
        )
        return suggestions_response_payload(
            current_input=current_input,
            recent_context=recent_context,
            project=project,
            history_context=prediction_context,
            model_predictions=model_predictions,
            suggestions=suggestions,
        )

    def rime_suggest(self, payload: dict[str, Any]) -> dict[str, object]:
        privacy_assessment = assess_foreground_write(payload)
        if privacy_assessment["storeAllowed"] is not True or sensitive_input_requested(payload):
            return build_rime_sidecar_response(
                payload=payload,
                adapter=self.adapter,
                core=self.core,
                predictor=self.predictor,
                default_project=self.config.project,
            )
        persisted_settings = self.settings_store.get_settings(include_sensitive=True)
        runtime_config = self.runtime_config_snapshot(settings=persisted_settings)
        effective_settings = runtime_config.effective_settings(persisted_settings)
        _apply_pinyin_settings_to_process_env(effective_settings)
        configure_auto_prediction_trigger(
            min_delta_chars=runtime_config.post_commit.min_delta_chars,
            max_calls_per_10s=runtime_config.post_commit.max_calls_per_10s,
            ignore_cooldown_ms=runtime_config.post_commit.cooldown_ms,
        )
        cache_key = self._rime_suggest_cache_key(payload, runtime_config=runtime_config)
        bypass_cache = self._rime_suggest_cache_bypass(payload)
        cached = None if bypass_cache else self._get_cached_rime_response(cache_key, payload)
        if cached is not None:
            self._record_prediction_live_trace(cached, request_payload=payload)
            return cached
        owner, inflight = self._begin_rime_inflight(cache_key)
        if not owner:
            response = self._wait_for_rime_inflight(cache_key, inflight, payload)
            self._record_prediction_live_trace(response, request_payload=payload)
            return response
        try:
            response = build_rime_sidecar_response(
                payload=payload,
                adapter=self.adapter,
                core=self.core,
                predictor=self.predictor,
                default_project=self.config.project,
                runtime_config=runtime_config,
            )
        except BaseException as exc:
            self._finish_rime_inflight(cache_key, error=exc)
            raise
        _attach_rime_ranking_diagnostics(response)
        self._apply_management_settings_to_rime_response(
            response,
            request_payload=payload,
            settings=effective_settings,
            runtime_config=runtime_config,
        )
        if self._rime_response_cacheable(response, request_payload=payload):
            self._store_rime_response(cache_key, response)
        self._finish_rime_inflight(cache_key, response=response)
        response = copy.deepcopy(response)
        response["cache"] = self._cache_payload(hit=False, cache_key=cache_key)
        self._record_prediction_live_trace(response, request_payload=payload)
        return response

    def _apply_management_settings_to_rime_response(
        self,
        response: dict[str, object],
        *,
        request_payload: dict[str, Any],
        settings: dict[str, object],
        runtime_config: RuntimeConfigSnapshot,
    ) -> None:
        display = settings.get("display") if isinstance(settings.get("display"), dict) else {}
        colors = display.get("colors") if isinstance(display.get("colors"), dict) else {}
        active_composition = bool(compact_whitespace(_string(request_payload.get("rawInput")) or _string(request_payload.get("preedit"))))
        debug_force_side_candidates = _bool(
            request_payload.get("forceSideCandidates") or request_payload.get("force_side_candidates"),
            default=False,
        )
        items = [dict(item) for item in response.get("displayCandidates", []) if isinstance(item, dict)]
        if active_composition and not runtime_config.composition_ai and not debug_force_side_candidates:
            items = [item for item in items if _string(item.get("sourceType")) in {"rime", "raw_english", "status"}]
        if not active_composition and not runtime_config.post_commit.enabled:
            items = []
        if (not runtime_config.hybrid_rag.enabled or not runtime_config.memory.enabled) and not debug_force_side_candidates:
            items = [item for item in items if _string(item.get("sourceType")) not in {"rag", "memory"}]
        if not runtime_config.post_commit.show_pending_status:
            items = [item for item in items if _string(item.get("sourceType")) != "status" and _string(item.get("displayLayout")) != "status_row"]
        max_post_commit = runtime_config.post_commit.max_candidates
        if not active_composition:
            kept: list[dict[str, object]] = []
            selectable_count = 0
            for item in items:
                if _string(item.get("sourceType")) == "status" or _string(item.get("selectionAction")) == "none":
                    kept.append(item)
                    continue
                selectable_count += 1
                if selectable_count <= max_post_commit:
                    kept.append(item)
            items = kept
        show_badges = runtime_config.source_badges.enabled
        for item in items:
            source_type = _string(item.get("sourceType"))
            custom_badge = runtime_config.source_badges.badge_for(source_type)
            custom_color = _string(colors.get(source_type))
            if not show_badges:
                item["badge"] = ""
                item["sourceBadge"] = ""
            elif custom_badge:
                item["badge"] = custom_badge
                item["sourceBadge"] = custom_badge
            if custom_color:
                item["colorToken"] = custom_color
        response["displayCandidates"] = items
        response["runtimeConfig"] = runtime_config.payload()
        response["runtimeRevision"] = runtime_config.runtime_revision
        response["settingsRevision"] = runtime_config.settings_revision
        response["runtimeProfile"] = runtime_config.profile
        response["managementSettings"] = {
            "settingsHash": runtime_config.settings_revision,
            "settingsRevision": runtime_config.settings_revision,
            "runtimeRevision": runtime_config.runtime_revision,
            "snapshotHash": runtime_config.snapshot_hash,
            "profile": runtime_config.profile,
            "debugForceSideCandidates": debug_force_side_candidates,
            "interactionApplied": True,
            "displayApplied": True,
        }
        if active_composition:
            effective_key_policy = {
                "numberKeys": runtime_config.key_policy.composition_number_keys,
                "tab": runtime_config.key_policy.composition_tab,
                "optionNumber": runtime_config.key_policy.composition_option_number,
                "escape": runtime_config.key_policy.composition_escape,
            }
        else:
            effective_key_policy = {
                "numberKeys": runtime_config.key_policy.post_commit_number_keys,
                "tab": runtime_config.key_policy.tab_action,
                "optionNumber": runtime_config.key_policy.option_number,
                "escape": runtime_config.key_policy.escape,
            }
        if isinstance(response.get("keyPolicy"), dict):
            response["keyPolicy"].update(effective_key_policy)  # type: ignore[union-attr]
            applied_key_policy = dict(response["keyPolicy"])  # type: ignore[arg-type]
        else:
            applied_key_policy = dict(effective_key_policy)
            response["keyPolicy"] = applied_key_policy
        prediction_session = response.get("predictionSession")
        if isinstance(prediction_session, dict):
            policy = prediction_session.get("keyPolicy")
            if isinstance(policy, dict):
                policy.update(effective_key_policy)
            if not bool(prediction_session.get("shouldClearPredictionPanel")) and not active_composition:
                prediction_session["expiresAfterMs"] = runtime_config.post_commit.panel_ttl_ms
        input_mode = _string(response.get("inputMode"))
        existing_overlay = response.get("assistantOverlay")
        if not input_mode and isinstance(existing_overlay, dict):
            input_mode = _string(existing_overlay.get("inputMode"))
        response["candidatePanel"] = build_candidate_panel_payload(
            input_mode=input_mode,
            display_candidates=items,
        )
        rag_candidates = (
            response.get("ragCandidates")
            if runtime_config.hybrid_rag.enabled and runtime_config.memory.enabled
            else []
        )
        response["assistantOverlay"] = build_assistant_overlay_payload(
            ui_mode=_string(response.get("uiMode")),
            input_mode=input_mode,
            display_candidates=items,
            rag_candidates=rag_candidates if isinstance(rag_candidates, list) else [],
            prediction_session=prediction_session if isinstance(prediction_session, dict) else None,
            key_policy=applied_key_policy,
            progressive=response.get("progressive") if isinstance(response.get("progressive"), dict) else None,
            frontend_transaction=(
                response.get("frontendTransaction")
                if isinstance(response.get("frontendTransaction"), dict)
                else None
            ),
        )
        overlay_config = {
            **runtime_config.overlay.payload(
                expires_after_ms=(
                    runtime_config.post_commit.panel_ttl_ms
                    if not active_composition
                    and not (
                        isinstance(prediction_session, dict)
                        and bool(prediction_session.get("shouldClearPredictionPanel"))
                    )
                    else 0
                )
            ),
            "maxCandidates": runtime_config.post_commit.max_candidates,
            "showSourceBadge": runtime_config.source_badges.enabled,
            "badges": dict(runtime_config.source_badges.items),
            "colors": dict(runtime_config.source_colors),
            "keyPolicy": dict(applied_key_policy),
            "activeRag": runtime_config.active_rag.payload(),
        }
        response["overlayConfig"] = overlay_config
        response["assistantOverlay"]["overlayConfig"] = overlay_config  # type: ignore[index]
        trace_events = response.get("predictionTraceEvents")
        if isinstance(trace_events, list):
            trace_events.append(
                {
                    "event": "effective_runtime_config_applied",
                    "fields": {
                        "runtimeRevision": runtime_config.runtime_revision,
                        "postCommitEnabled": runtime_config.post_commit.enabled,
                        "memoryEnabled": runtime_config.memory.enabled,
                        "hybridRagEnabled": runtime_config.hybrid_rag.enabled,
                        "ragLanes": dict(runtime_config.hybrid_rag.query_lanes()),
                        "ragWeights": dict(runtime_config.hybrid_rag.query_weights()),
                        "activeRagEnabled": runtime_config.active_rag.enabled,
                        "activeRagShortcut": runtime_config.active_rag.shortcut,
                    },
                }
            )

    def rime_select(self, payload: dict[str, Any]) -> dict[str, object]:
        response = record_rime_side_candidate_selection(
            payload=payload,
            adapter=self.adapter,
            core=self.core,
            default_project=self.config.project,
        )
        if response.get("stored") is True:
            self._clear_rime_cache()
        return response

    def rime_rank_feedback(self, payload: dict[str, Any]) -> dict[str, object]:
        return record_native_rime_selection(
            payload,
            db_path=self.config.db_path,
            default_project=self.config.project,
        )

    def candidate_edit_feedback(self, payload: dict[str, Any]) -> dict[str, object]:
        """Record a delete immediately following an accepted candidate.

        Source isolation is intentional: every source can affect local memory
        ranking, but only native Rime candidates can influence the Pinyin
        lexicon review queue.
        """

        privacy_assessment = assess_foreground_write(payload)
        if privacy_assessment["storeAllowed"] is not True:
            return {
                "schemaVersion": "rag-ime.candidate-edit-feedback.v1",
                "ok": True,
                "recorded": False,
                "rimeRecorded": False,
                "noStore": True,
                "privacyAssessment": privacy_assessment,
                "storageReceipt": storage_receipt(privacy_assessment, stored=False),
            }
        source_type = compact_whitespace(_string(payload.get("sourceType"))).lower()
        candidate_text = compact_whitespace(_string(payload.get("candidateText")))
        candidate_id = compact_whitespace(_string(payload.get("candidateId")))
        if not source_type:
            raise ValueError("sourceType is required")
        if not candidate_text:
            raise ValueError("candidateText is required")
        event_name = (
            "backspace_after_active_rag_accept"
            if source_type in {"rag", "memory", "active_rag", "deepseek"}
            else "backspace_after_accept"
        )
        project = compact_whitespace(_string(payload.get("project"))) or self.config.project
        app = compact_whitespace(_string(payload.get("app"))) or "squirrel"
        context_hash = compact_whitespace(_string(payload.get("contextHash")))
        self.core.record_memory_feedback(
            {
                "event": event_name,
                "candidateId": candidate_id or f"accepted:{stable_text_hash(candidate_text)}",
                "candidateText": candidate_text,
                "sourceType": source_type,
                "contextHash": context_hash,
                "frontAppBundleId": app,
                "project": project,
                "metadata": {
                    "source": "patched_squirrel_post_accept_delete",
                    "deleteCount": max(1, min(32, _optional_int(payload.get("deleteCount")) or 1)),
                    "acceptedAtMs": _optional_int(payload.get("acceptedAtMs")) or 0,
                },
            }
        )
        rime_event_id = 0
        preedit = compact_whitespace(_string(payload.get("preedit")))
        if source_type == "rime" and preedit:
            rime_event_id = record_rime_rank_feedback(
                self.config.db_path,
                preedit=preedit,
                accepted_text=candidate_text,
                rejected_text=candidate_text,
                action="backspace_downrank",
                app=app,
                project=project,
                context_hash=context_hash,
                metadata={
                    "candidateSource": "rime",
                    "selectionSource": "patched_squirrel_post_accept_delete",
                },
            )
        self._clear_rime_cache()
        return {
            "schemaVersion": "rag-ime.candidate-edit-feedback.v1",
            "ok": True,
            "recorded": True,
            "rimeRecorded": rime_event_id > 0,
            "rimeEventId": rime_event_id,
            "event": event_name,
            "sourceType": source_type,
            "noStore": False,
            "privacyAssessment": privacy_assessment,
            "storageReceipt": storage_receipt(privacy_assessment, stored=True),
        }

    def rime_lexicon_review(self, payload: dict[str, Any]) -> dict[str, object]:
        project = _string(payload.get("project")) or self.config.project
        review = review_rime_lexicon(
            self.config.db_path,
            project=project,
            limit=max(1, min(500, _optional_int(payload.get("limit")) or 200)),
            rime_user_dir=self.config.rime_user_dir,
        )
        review["organization"] = lexicon_organization_status(
            self.config.db_path,
            project=project,
        )
        return review

    def rime_lexicon_apply(self, payload: dict[str, Any]) -> dict[str, object]:
        selected_keys = payload.get("selectedKeys")
        if not isinstance(selected_keys, list):
            selected_keys = []
        return apply_reviewed_rime_lexicon(
            self.config.db_path,
            rime_user_dir=self.config.rime_user_dir,
            backup_root=self.config.rime_lexicon_backup_root,
            project=_string(payload.get("project")) or self.config.project,
            limit=max(1, min(500, _optional_int(payload.get("limit")) or 200)),
            review_token=_string(payload.get("reviewToken")),
            selected_keys=[_string(value) for value in selected_keys],
            confirm_text=_string(payload.get("confirmText")),
        )

    def rime_lexicon_rollback(self, payload: dict[str, Any]) -> dict[str, object]:
        return rollback_reviewed_rime_lexicon(
            rollback_id=_string(payload.get("rollbackId")),
            backup_root=self.config.rime_lexicon_backup_root,
        )

    def commit(self, payload: dict[str, Any]) -> dict[str, object]:
        text = _string(payload.get("text")).strip()
        if not text:
            raise ValueError("text must not be empty")
        source = _string(payload.get("source")) or "debug_page_commit"
        app = _string(
            payload.get("app")
            or payload.get("frontAppBundleId")
            or payload.get("frontmostApp")
            or payload.get("bundleId")
        ) or "squirrel"
        capture_metadata = _input_capture_metadata(
            payload.get("captureMetadata"),
            text=text,
            source=source,
            app=app,
        )
        capture_contract = capture_contract_from_metadata(
            capture_metadata,
            text=text,
            source=source,
            app=app,
        )
        privacy_assessment = assess_foreground_write(payload)
        if privacy_assessment["storeAllowed"] is not True:
            if capture_contract is not None:
                capture_receipt = self._record_capture_outcome(
                    text=text,
                    source=source,
                    app=app,
                    capture_metadata=capture_metadata,
                    outcome="no_store",
                    reason_code=str(privacy_assessment["reason"]),
                )
                return _capture_commit_response(
                    privacy_assessment,
                    capture_receipt=capture_receipt,
                )
            return {
                "schemaVersion": "rag-ime.foreground-commit.v1",
                "ok": True,
                "stored": False,
                "noStore": True,
                "eventId": "",
                "privacyAssessment": privacy_assessment,
                "storageReceipt": storage_receipt(privacy_assessment, stored=False),
            }
        if capture_contract is not None and not capture_contract.is_strong_final:
            capture_receipt = self._record_capture_outcome(
                text=text,
                source=source,
                app=app,
                capture_metadata=capture_metadata,
                outcome="quarantined",
                reason_code="weak_boundary_not_final",
            )
            return _capture_commit_response(
                privacy_assessment,
                capture_receipt=capture_receipt,
            )
        capture_receipt: dict[str, object] | None = None
        if capture_contract is not None:
            recorder = getattr(self.core, "record_event_with_capture_receipt", None)
            if not callable(recorder):
                raise RuntimeError("v2 input capture requires an atomic receipt store")
            event_id, capture_receipt = recorder(
                InputEvent(
                    event_id=None,
                    created_at_ms=now_ms(),
                    source=source,
                    committed_text=text,
                    privacy_disposition=str(privacy_assessment["disposition"]),
                    recent_context=_string(payload.get("recentContext")),
                    preedit=_string(payload.get("preedit")),
                    schema_id="luna_pinyin",
                    app=app,
                    project=_string(payload.get("project")) or self.config.project,
                    candidate_rank=_optional_int(payload.get("candidateRank")),
                    provider_name=_string(payload.get("providerName")) or "debug-page",
                    tags=tuple(_string_list(payload.get("tags"))),
                    context_group_id=_string(payload.get("contextGroupId")),
                    context_group_level=_string(payload.get("contextGroupLevel")) or "app",
                    capture_metadata=capture_metadata,
                )
            )
        else:
            event_id = self.adapter.commit_text(
                text,
                recent_context=_string(payload.get("recentContext")),
                preedit=_string(payload.get("preedit")),
                project=_string(payload.get("project")) or self.config.project,
                app=app,
                privacy_disposition=str(privacy_assessment["disposition"]),
                candidate_rank=_optional_int(payload.get("candidateRank")),
                provider_name=_string(payload.get("providerName")) or "debug-page",
                tags=tuple(_string_list(payload.get("tags"))),
                source=source,
                context_group_id=_string(payload.get("contextGroupId")),
                context_group_level=_string(payload.get("contextGroupLevel")) or "app",
                capture_metadata=capture_metadata,
            )
        self._clear_rime_cache()
        stored = bool(event_id) and not event_id.startswith("skipped:")
        response: dict[str, object] = {
            "schemaVersion": "rag-ime.foreground-commit.v1",
            "ok": True,
            "stored": stored,
            "noStore": False,
            "eventId": event_id,
            "eventCount": self._event_count(),
            "privacyAssessment": privacy_assessment,
            "storageReceipt": storage_receipt(
                privacy_assessment,
                stored=stored,
                event_id=event_id,
            ),
        }
        if capture_contract is not None:
            if not isinstance(capture_receipt, dict):
                raise RuntimeError("v2 input capture stored without a durable receipt")
            return _capture_commit_response(
                privacy_assessment,
                capture_receipt=capture_receipt,
                event_count=int(response["eventCount"]),
            )
        return response

    def _record_capture_outcome(
        self,
        *,
        text: str,
        source: str,
        app: str,
        capture_metadata: dict[str, object],
        outcome: str,
        reason_code: str,
    ) -> dict[str, object]:
        recorder = getattr(self.core, "record_capture_outcome", None)
        if not callable(recorder):
            raise RuntimeError("v2 input capture requires a durable receipt store")
        receipt = recorder(
            text=text,
            source=source,
            app=app,
            capture_metadata=capture_metadata,
            outcome=outcome,
            reason_code=reason_code,
        )
        if not isinstance(receipt, dict):
            raise RuntimeError("v2 input capture receipt store returned an invalid receipt")
        return receipt

    def action(self, payload: dict[str, Any]) -> dict[str, object]:
        action_type = _canonical_action(_string(payload.get("actionType")))
        memory_id = _string(payload.get("memoryId"))
        if not memory_id:
            raise ValueError("memoryId must not be empty")
        privacy_assessment = assess_foreground_write(payload)
        if privacy_assessment["storeAllowed"] is not True:
            return {
                "schemaVersion": "rag-ime.action.v1",
                "ok": True,
                "actionId": None,
                "createdAtMs": now_ms(),
                "memoryId": memory_id,
                "actionType": action_type,
                "query": _string(payload.get("query")),
                "suggestionId": _string(payload.get("suggestionId")),
                "sourceEventId": _optional_int(payload.get("sourceEventId")),
                "metadata": {},
                "stored": False,
                "noStore": True,
                "privacyAssessment": privacy_assessment,
                "storageReceipt": storage_receipt(privacy_assessment, stored=False),
            }
        action = self.core.apply_action(
            MemoryAction(
                action_id=None,
                created_at_ms=now_ms(),
                memory_id=memory_id,
                action_type=action_type,
                query=_string(payload.get("query")),
                suggestion_id=_string(payload.get("suggestionId")),
                source_event_id=_optional_int(payload.get("sourceEventId")),
                metadata={"surface_text": _string(payload.get("surfaceText"))},
            )
        )
        self._clear_rime_cache()
        return {
            **action_response_payload(action),
            "ok": True,
            "stored": True,
            "noStore": False,
            "privacyAssessment": privacy_assessment,
            "storageReceipt": storage_receipt(privacy_assessment, stored=True),
        }

    def assistant_candidate_action(self, payload: dict[str, Any]) -> dict[str, object]:
        action = _string(payload.get("action")).strip().lower()
        if action not in {"remember", "suppress"}:
            raise ValueError("assistant candidate action must be remember or suppress")
        privacy_assessment = assess_foreground_write(payload)
        if privacy_assessment["storeAllowed"] is not True:
            return {
                "schemaVersion": "rag-ime.assistant-candidate-action.v1",
                "ok": True,
                "action": action,
                "stored": False,
                "noStore": True,
                "memoryId": None,
                "tombstoneId": None,
                "privacyAssessment": privacy_assessment,
                "storageReceipt": storage_receipt(privacy_assessment, stored=False),
            }
        candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
        assert isinstance(candidate, dict)
        text = compact_whitespace(
            _string(candidate.get("insertText")) or _string(candidate.get("text"))
        )
        if not text:
            raise ValueError("assistant candidate text must not be empty")

        source_type = _string(candidate.get("sourceType")) or "model"
        memory_id = _string(candidate.get("memoryId"))
        source_event_id = _optional_int(candidate.get("sourceEventId"))
        query = _string(payload.get("query"))
        project = _string(payload.get("project")) or self.config.project
        app = _string(payload.get("app"))
        tombstone_id: int | None = None
        if action == "remember":
            has_actionable_memory = (
                source_type in {"rag", "memory"}
                and bool(memory_id)
                and source_event_id is not None
            )
            if not has_actionable_memory:
                memory_id = self.adapter.commit_text(
                    text,
                    recent_context=query,
                    project=project,
                    app=app or "squirrel",
                    privacy_disposition=str(privacy_assessment["disposition"]),
                    source="squirrel_assistant_remember",
                    provider_name=f"assistant-overlay:{source_type}",
                    tags=("assistant-overlay", "remembered"),
                )
            pinned = self.core.apply_action(
                MemoryAction(
                    action_id=None,
                    created_at_ms=now_ms(),
                    memory_id=memory_id,
                    action_type="pin",
                    query=query,
                    suggestion_id=_string(candidate.get("suggestionId")) or f"assistant:{stable_text_hash(text)}",
                    source_event_id=source_event_id,
                    metadata={"surface_text": text, "source_type": source_type, "app": app, "project": project},
                )
            )
            result: dict[str, object] = {"pinned": action_response_payload(pinned)}
        else:
            if not isinstance(self.core, LocalSqliteCoreClient):
                raise ValueError("assistant suppression requires the local SQLite core")
            tombstone = self.core.add_memory_tombstone(
                target_type="normalized_text",
                target_value=text,
                reason="assistant_overlay_suppress",
                metadata={"sourceType": source_type, "app": app, "project": project},
            )
            tombstone_id = int(tombstone.get("id") or 0) or None
            self.core.record_memory_feedback(
                {
                    "event": "hide",
                    "candidateId": memory_id or _string(candidate.get("candidateStableId")),
                    "candidateText": text,
                    "sourceType": source_type,
                    "contextHash": query,
                    "frontAppBundleId": app,
                    "project": project,
                    "metadata": {"source": "assistant_overlay_suppress"},
                }
            )
            result = {"tombstone": tombstone}
        self._clear_rime_cache()
        return {
            "schemaVersion": "rag-ime.assistant-candidate-action.v1",
            "ok": True,
            "action": action,
            "stored": True,
            "noStore": False,
            "memoryId": memory_id or None,
            "tombstoneId": tombstone_id,
            "privacyAssessment": privacy_assessment,
            "storageReceipt": storage_receipt(
                privacy_assessment,
                stored=True,
                event_id=memory_id or tombstone_id,
            ),
            **result,
        }

    def _predictor_ttfc_cases(self, payload: dict[str, Any]) -> list[PredictionBenchmarkCase]:
        raw_cases = payload.get("cases")
        case_items: list[object]
        if isinstance(raw_cases, list) and raw_cases:
            case_items = raw_cases
        else:
            case_items = [
                {
                    "id": "debug-current",
                    "currentInput": _string(payload.get("currentInput")) or "RAG 输入法",
                    "recentContext": _string(payload.get("recentContext")),
                }
            ]
        cases: list[PredictionBenchmarkCase] = []
        for index, item in enumerate(case_items, start=1):
            if isinstance(item, str):
                current_input = item
                recent_context = _string(payload.get("recentContext"))
                case_id = f"case-{index}"
            elif isinstance(item, dict):
                current_input = (
                    _string(item.get("currentInput"))
                    or _string(item.get("current_input"))
                    or _string(item.get("query"))
                    or _string(item.get("input"))
                )
                recent_context = _string(item.get("recentContext")) or _string(item.get("recent_context"))
                case_id = _string(item.get("id")) or _string(item.get("caseId")) or f"case-{index}"
            else:
                continue
            current_input = current_input.strip()
            if not current_input:
                continue
            prediction_context = build_prediction_context(
                self.core,
                explicit_recent_context=recent_context,
                project=self.config.project,
            )
            cases.append(
                PredictionBenchmarkCase(
                    current_input=current_input,
                    recent_context=prediction_context,
                    case_id=case_id,
                )
            )
        if not cases:
            raise ValueError("predictor TTFC probe needs at least one non-empty input case")
        return cases

    def _cache_probe_rime_payload(
        self,
        payload: dict[str, Any],
        *,
        current_input: str,
        recent_context: str,
        project: str,
    ) -> dict[str, object]:
        raw_candidates = payload.get("rimeCandidates")
        candidates: list[dict[str, object]] = []
        if isinstance(raw_candidates, list):
            for index, item in enumerate(raw_candidates[:6], start=1):
                if isinstance(item, str):
                    text = item
                    comment = "cache-probe"
                elif isinstance(item, dict):
                    text = _string(item.get("text"))
                    comment = _string(item.get("comment")) or "cache-probe"
                else:
                    continue
                if text.strip():
                    candidates.append({"label": str(index), "text": text.strip(), "comment": comment, "index": index - 1})
        if not candidates:
            candidates = [{"label": "1", "text": current_input, "comment": "cache-probe", "index": 0}]
        return {
            "sessionId": "cache-probe",
            "requestSeq": 0,
            # Cache probes operate on fixed synthetic text, never foreground input.
            "privacyDisposition": "allowed",
            "rawInput": _string(payload.get("rawInput")) or current_input,
            "preedit": _string(payload.get("preedit")) or current_input,
            "committedContext": recent_context,
            "project": project,
            "idleMs": _bounded_int(payload.get("idleMs"), default=200, minimum=0, maximum=60_000),
            "maxVisibleCandidates": _bounded_int(payload.get("maxVisibleCandidates"), default=6, minimum=1, maximum=10),
            "maxSideCandidates": _bounded_int(payload.get("maxSideCandidates"), default=2, minimum=0, maximum=5),
            "forceSideCandidates": bool(payload.get("forceSideCandidates", False)),
            "rimeContext": {
                "candidates": candidates,
                "highlightedIndex": 0,
                "page": 0,
                "isLastPage": True,
            },
        }

    def _event_count(self) -> int | None:
        count = getattr(self.core, "event_count", None)
        return int(count()) if callable(count) else None

    def _action_count(self) -> int | None:
        count = getattr(self.core, "action_count", None)
        return int(count()) if callable(count) else None

    def _suggestion_cache_stats(self) -> dict[str, object] | None:
        stats = getattr(self.core, "suggestion_cache_stats", None)
        return stats() if callable(stats) else None

    def _vector_index_stats(self) -> dict[str, object] | None:
        stats = getattr(self.core, "vector_index_stats", None)
        return stats() if callable(stats) else None

    def _vector_index_cache_signature(self) -> dict[str, object] | None:
        """Return only content revisions, never wall-clock freshness ages."""

        stats = self._vector_index_stats()
        if not isinstance(stats, dict):
            return stats
        signature = dict(stats)
        projection = signature.get("memoryProjection")
        if isinstance(projection, dict):
            signature["memoryProjection"] = {
                key: projection.get(key)
                for key in (
                    "states",
                    "sourceDocuments",
                    "sourceUpdatedAtMs",
                    "retrievalDocsUpdatedAtMs",
                    "retrievalDocuments",
                    "providerFingerprint",
                    "vectorDocuments",
                    "missingVectors",
                    "staleVectors",
                    "checkpoints",
                    "latestOutboxIds",
                )
            }
        return signature

    def _create_memory_projection_worker(self) -> MemoryProjectionWorker | None:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return None
        if not _memory_projection_worker_enabled(self.config):
            return None
        configured_interval = self.config.memory_projection_poll_interval_s
        poll_interval_s = (
            _positive_float(configured_interval, default=1.0)
            if configured_interval is not None
            else _positive_float(
                os.environ.get("RAG_IME_MEMORY_PROJECTION_POLL_INTERVAL_S"),
                default=1.0,
            )
        )
        return MemoryProjectionWorker(
            self.core._connect,  # type: ignore[arg-type]
            embedding_provider=self.core.embedding_provider,
            poll_interval_s=poll_interval_s,
        )

    def _maybe_auto_rebuild_vector_index(self) -> dict[str, object] | None:
        if not isinstance(self.core, LocalSqliteCoreClient):
            return None
        limit = max(0, int(self.config.vector_auto_rebuild_limit))
        if limit <= 0:
            return None
        stats = self.core.vector_index_stats()
        if not bool(stats.get("enabled")):
            return {
                "trigger": "startup",
                "skippedReason": "embedding provider disabled",
                "limit": limit,
                **stats,
            }
        event_count = self._event_count()
        active_vectors = int(stats.get("activeProviderVectors") or 0)
        active_retrieval_vectors = int(stats.get("activeProviderRetrievalDocVectors") or 0)
        if event_count is None or event_count <= 0:
            return {
                "trigger": "startup",
                "skippedReason": "no input events",
                "limit": limit,
                **stats,
            }
        if active_vectors > 0 and active_retrieval_vectors > 0:
            return {
                "trigger": "startup",
                "skippedReason": "active provider event and retrieval vectors already present",
                "limit": limit,
                **stats,
            }
        report = self.core.rebuild_vector_index(project=self.config.project, limit=limit)
        return {
            "trigger": "startup",
            "project": self.config.project,
            "limit": limit,
            **report,
        }

    def _vector_auto_rebuild_status(self) -> dict[str, object]:
        limit = max(0, int(self.config.vector_auto_rebuild_limit))
        return {
            "enabled": limit > 0,
            "limit": limit,
            "lastRun": self._vector_auto_rebuild_report,
        }

    def _cache_ttl_ms(self) -> int:
        return max(0, int(self.config.rime_cache_ttl_ms))

    def _rime_cache_size(self) -> int:
        with self._rime_cache_lock:
            self._prune_rime_cache()
            return len(self._rime_cache)

    def _rime_inflight_size(self) -> int:
        with self._rime_cache_lock:
            return len(self._rime_inflight)

    def _rime_suggest_cache_key(
        self,
        payload: dict[str, Any],
        *,
        runtime_config: RuntimeConfigSnapshot | None = None,
    ) -> str:
        runtime_config = runtime_config or self.runtime_config_snapshot()
        snapshot = parse_rime_context_payload(payload, default_project=self.config.project)
        semantic_query, query_basis = choose_semantic_query(snapshot)
        trigger_decision = decide_side_candidate_refresh(
            snapshot=snapshot,
            semantic_query=semantic_query,
            query_basis=query_basis,
        )
        raw_sensitive_input = {}
        if query_basis in ("preedit", "rawInputFallback"):
            raw_sensitive_input = {
                "rawInput": snapshot.raw_input,
                "preedit": snapshot.preedit,
            }
        normalized_snapshot = {
            "project": snapshot.project or self.config.project,
            "semanticQuery": semantic_query,
            "queryBasis": query_basis,
            "predictionFirstMerge": prediction_first_merge_enabled(payload),
            "triggerDecision": {
                "shouldRefresh": trigger_decision.should_refresh,
                "reason": trigger_decision.reason,
                "forceSideCandidates": snapshot.force_side_candidates,
            },
            "committedContext": snapshot.committed_context,
            "commitTextPreview": snapshot.commit_text_preview,
            "rimeContext": rime_context_to_payload(snapshot),
            "latencyBudgetMs": snapshot.latency_budget_ms,
            "maxVisibleCandidates": snapshot.max_visible_candidates,
            "maxSideCandidates": snapshot.max_side_candidates,
            **raw_sensitive_input,
        }
        material = {
            "snapshot": normalized_snapshot,
            "project": self.config.project,
            "runtimeConfigHash": runtime_config.snapshot_hash,
            "runtimeRevision": runtime_config.runtime_revision,
            "eventCount": self._event_count(),
            "actionCount": self._action_count(),
            "vectorStats": self._vector_index_cache_signature(),
            "predictor": self._predictor_fingerprint(),
        }
        raw = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _rime_suggest_cache_bypass(self, payload: dict[str, Any]) -> bool:
        return _bool(payload.get("progressiveFollowUp") or payload.get("progressive_follow_up"), default=False)

    def _rime_response_cacheable(self, response: dict[str, object], *, request_payload: dict[str, Any]) -> bool:
        if self._rime_suggest_cache_bypass(request_payload):
            return False
        progressive = response.get("progressive") if isinstance(response.get("progressive"), dict) else {}
        assert isinstance(progressive, dict)
        if bool(progressive.get("shouldFollowUp")):
            return False
        for lane_key in ("modelLane", "ragLane"):
            lane = response.get(lane_key) if isinstance(response.get(lane_key), dict) else {}
            if isinstance(lane, dict) and (bool(lane.get("pending")) or bool(lane.get("inFlight"))):
                return False
        return True

    def _predictor_fingerprint(self) -> str:
        config = getattr(self.predictor, "config", None)
        if config is None:
            return self.predictor.__class__.__name__
        return f"{self.predictor.__class__.__name__}:{config!r}"

    def _predictor_status(
        self,
        *,
        probe_capabilities: bool = False,
        force_refresh: bool = False,
        ttl_ms: int = 5_000,
    ) -> dict[str, object]:
        if not probe_capabilities:
            return prediction_provider_status(self.predictor, probe_capabilities=False)
        ttl_ms = max(0, int(ttl_ms))
        fingerprint = self._predictor_fingerprint()
        now = time.monotonic()
        with self._predictor_status_lock:
            entry = self._predictor_status_cache.get(True)
            if not force_refresh and entry is not None and entry.fingerprint == fingerprint and entry.expires_at > now:
                status = copy.deepcopy(entry.status)
                status["statusCache"] = {"hit": True, "ttlMs": ttl_ms}
                return status

        status = prediction_provider_status(self.predictor, probe_capabilities=True)
        with self._predictor_status_lock:
            self._predictor_status_cache[True] = _PredictorStatusCacheEntry(
                fingerprint=fingerprint,
                expires_at=time.monotonic() + ttl_ms / 1000,
                status=copy.deepcopy(status),
            )
        status = copy.deepcopy(status)
        status["statusCache"] = {"hit": False, "ttlMs": ttl_ms}
        return status

    def _get_cached_rime_response(self, cache_key: str, payload: dict[str, Any]) -> dict[str, object] | None:
        ttl_ms = self._cache_ttl_ms()
        if ttl_ms <= 0:
            return None
        now = time.monotonic()
        with self._rime_cache_lock:
            self._prune_rime_cache(now=now)
            entry = self._rime_cache.get(cache_key)
            if entry is None or entry.expires_at <= now:
                if entry is not None:
                    self._rime_cache.pop(cache_key, None)
                return None
            self._rime_cache_hits += 1
            response = copy.deepcopy(entry.response)
        self._refresh_cached_rime_response(response, payload)
        response["sessionId"] = _string(payload.get("sessionId")) or str(response.get("sessionId") or "default")
        response["requestSeq"] = _bounded_int(payload.get("requestSeq"), default=0, minimum=0, maximum=2**63 - 1)
        response["cache"] = self._cache_payload(hit=True, cache_key=cache_key)
        return response

    def _refresh_cached_rime_response(self, response: dict[str, object], payload: dict[str, Any]) -> None:
        snapshot = parse_rime_context_payload(payload, default_project=self.config.project)
        semantic_query, query_basis = choose_semantic_query(snapshot)
        trigger_decision = decide_side_candidate_refresh(
            snapshot=snapshot,
            semantic_query=semantic_query,
            query_basis=query_basis,
        )
        response.update(
            {
                **frontend_transaction_to_payload(snapshot.frontend_transaction),
                "frontendTransaction": frontend_transaction_to_payload(snapshot.frontend_transaction),
                "project": snapshot.project or self.config.project,
                "rawInput": snapshot.raw_input,
                "preedit": snapshot.preedit,
                "commitTextPreview": snapshot.commit_text_preview,
                "committedContext": snapshot.committed_context,
                "semanticQuery": semantic_query,
                "queryBasis": query_basis,
                "latencyBudgetMs": snapshot.latency_budget_ms,
                "rimeContext": rime_context_to_payload(snapshot),
                "triggerDecision": {
                    "shouldRefresh": trigger_decision.should_refresh,
                    "reason": trigger_decision.reason,
                    "idleMs": snapshot.idle_ms,
                    "semanticSignalLength": semantic_signal_length(semantic_query),
                    "forceSideCandidates": snapshot.force_side_candidates,
                },
            }
        )
        prediction_first = response.get("predictionFirst")
        if isinstance(prediction_first, dict):
            prediction_first["enabled"] = prediction_first_merge_enabled(payload)
            prediction_first["pinyinPrefix"] = snapshot.preedit or snapshot.raw_input
        _rebind_cached_rime_prediction_payload(response, snapshot=snapshot, semantic_query=semantic_query, query_basis=query_basis)
        _attach_rime_ranking_diagnostics(response)

    def _store_rime_response(self, cache_key: str, response: dict[str, object]) -> None:
        ttl_ms = self._cache_ttl_ms()
        if ttl_ms <= 0:
            return
        with self._rime_cache_lock:
            self._rime_cache_misses += 1
            self._rime_cache[cache_key] = _RimeSuggestCacheEntry(
                expires_at=time.monotonic() + ttl_ms / 1000,
                response=copy.deepcopy(response),
            )
            self._prune_rime_cache()

    def _clear_rime_cache(self) -> None:
        with self._rime_cache_lock:
            self._rime_cache.clear()

    def _input_source_check_script(self) -> Path | None:
        if self.config.input_source_check_script is not None:
            return self.config.input_source_check_script
        source_root = os.environ.get("RAG_IME_SOURCE_ROOT") or os.environ.get("RAG_IME_REPO_ROOT")
        candidates = []
        if source_root:
            candidates.append(Path(source_root) / "scripts" / "check_macos_input_source.sh")
        candidates.append(Path.cwd() / "scripts" / "check_macos_input_source.sh")
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0] if candidates else None

    def _prune_rime_cache(self, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        expired = [key for key, entry in self._rime_cache.items() if entry.expires_at <= current]
        for key in expired:
            self._rime_cache.pop(key, None)

    def _cache_payload(self, *, hit: bool, cache_key: str) -> dict[str, object]:
        return {
            "hit": hit,
            "inFlightHit": False,
            "key": cache_key[:16],
            "ttlMs": self._cache_ttl_ms(),
            "size": self._rime_cache_size(),
            "hits": self._rime_cache_hits,
            "misses": self._rime_cache_misses,
            "inFlight": self._rime_inflight_size(),
            "inFlightHits": self._rime_inflight_hits,
        }

    def _begin_rime_inflight(self, cache_key: str) -> tuple[bool, _RimeSuggestInflightEntry]:
        with self._rime_cache_lock:
            entry = self._rime_inflight.get(cache_key)
            if entry is not None:
                entry.waiters += 1
                self._rime_inflight_hits += 1
                return False, entry
            entry = _RimeSuggestInflightEntry(event=Event())
            self._rime_inflight[cache_key] = entry
            return True, entry

    def _wait_for_rime_inflight(
        self,
        cache_key: str,
        entry: _RimeSuggestInflightEntry,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        entry.event.wait()
        if entry.error is not None:
            raise entry.error
        response = copy.deepcopy(entry.response or {})
        self._refresh_cached_rime_response(response, payload)
        response["sessionId"] = _string(payload.get("sessionId")) or str(response.get("sessionId") or "default")
        response["requestSeq"] = _bounded_int(payload.get("requestSeq"), default=0, minimum=0, maximum=2**63 - 1)
        response["cache"] = {
            **self._cache_payload(hit=False, cache_key=cache_key),
            "inFlightHit": True,
        }
        return response

    def _finish_rime_inflight(
        self,
        cache_key: str,
        *,
        response: dict[str, object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._rime_cache_lock:
            entry = self._rime_inflight.pop(cache_key, None)
            if entry is None:
                return
            if error is not None:
                self._rime_inflight_errors += 1
            entry.response = copy.deepcopy(response) if response is not None else None
            entry.error = error
            entry.event.set()

    def _record_prediction_live_trace(self, response: dict[str, object], *, request_payload: dict[str, Any]) -> None:
        frame = _prediction_live_trace_frame(
            response=response,
            request_payload=request_payload,
            include_raw_text=self._include_raw_text(),
        )
        with self._rime_cache_lock:
            self._prediction_live_trace.append(frame)
            if len(self._prediction_live_trace) > 500:
                del self._prediction_live_trace[: len(self._prediction_live_trace) - 500]


_MEMORY_ENTITY_PATH_PREFIX = "/api/memory/entities/"
_MEMORY_REFERENCE_PATH_PREFIX = "/api/memory/references/"
_KNOWLEDGE_BASES_PATH = "/api/knowledge-bases"
_MAX_KNOWLEDGE_IMPORT_BYTES = 200 * 1024 * 1024
_ISOLATED_HTML_PREVIEW_PATH = "/__paw_html_preview"
_ISOLATED_HTML_PREVIEW_DOCUMENT = b"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PAW HTML Preview</title>
</head>
<body>
  <noscript>This preview requires JavaScript.</noscript>
  <script>
  window.addEventListener('DOMContentLoaded', () => {
    try {
      const encoded = window.location.hash.slice(1).replace(/-/g, '+').replace(/_/g, '/');
      if (!encoded) throw new Error('preview source is missing');
      const padded = encoded + '='.repeat((4 - encoded.length % 4) % 4);
      const binary = window.atob(padded);
      const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
      const source = new TextDecoder().decode(bytes);
      document.open();
      document.write(source);
      document.close();
    } catch (error) {
      document.body.textContent = `HTML preview failed: ${String(error)}`;
    }
  }, { once: true });
  </script>
</body>
</html>
"""
_MEMORY_GRAPH_QUERY_FIELDS = frozenset(
    {
        "plane",
        "project",
        "status",
        "query",
        "focusId",
        "depth",
        "nodeLimit",
        "edgeLimit",
        "minWeight",
    }
)
_MEMORY_ENTITY_QUERY_FIELDS = frozenset(
    {
        "project",
        "connectionsLimit",
        "connectionsCursor",
        "membersLimit",
        "membersCursor",
    }
)


def _knowledge_route_parts(path: str) -> tuple[str, ...] | None:
    normalized = path.rstrip("/") or "/"
    if normalized == _KNOWLEDGE_BASES_PATH:
        return ()
    prefix = f"{_KNOWLEDGE_BASES_PATH}/"
    if not normalized.startswith(prefix):
        return None
    return tuple(unquote(part) for part in normalized[len(prefix) :].split("/") if part)


def _agent_session_runtime_error_payload(
    error: AgentRuntimeError,
) -> dict[str, object]:
    cause_code = (
        error.host_error_code if isinstance(error, PiRuntimeCommandRejected) else ""
    )
    message = " ".join(str(error).split()).casefold()
    workspace_missing = (
        "workspace does not exist" in message
        or "workspace root does not exist" in message
        or "workspace no longer exists" in message
    )
    if workspace_missing:
        return {
            "schemaVersion": "rag-ime.local-api-error.v1",
            "ok": False,
            "errorCode": "session_workspace_missing",
            "retryable": False,
            "error": (
                "session workspace is no longer available; "
                "select a different workspace"
            ),
            "recovery": {"action": "select_workspace"},
        }
    if cause_code in {
        "INVALID_PARAMS", "INVALID_REQUEST", "INVALID_PI_PACKAGE",
        "INVALID_PLUGIN_DRAFT", "INVALID_PLUGIN_MANIFEST", "INVALID_PLUGIN_SOURCE",
        "INVALID_PLUGIN_STATE", "PLUGIN_NOT_FOUND", "PLUGIN_DRAFT_EXISTS",
        "PLUGIN_LIMIT_EXCEEDED", "PLUGIN_PATH_BOUNDARY", "PLUGIN_SOURCE_NOT_FOUND",
        "PLUGIN_DIGEST_MISMATCH", "PLUGIN_STATE_CHANGED", "PLUGIN_PREPARE_REQUIRED",
        "PLUGIN_PREVIEW_REQUIRED", "PLUGIN_APPROVAL_REQUIRED", "PLUGIN_CONFIRMATION_REQUIRED",
        "MODEL_NOT_FOUND", "THINKING_LEVEL_UNSUPPORTED", "TOOL_NOT_FOUND",
        "METHOD_NOT_FOUND", "COMMAND_NOT_FOUND", "WORKSPACE_DENIED",
    }:
        # A terminal Host rejection proves the request was received. Preserve
        # its structured cause without exposing private paths from the message
        # or suggesting that reconnecting will repair an invalid Package.
        rejection_messages = {
            "INVALID_PLUGIN_MANIFEST": (
                "插件清单校验未通过；原生 Pi Package 请使用 packageSource 指定来源。"
            ),
            "INVALID_PI_PACKAGE": "Pi Package 的元数据或资源不符合要求，请检查包内容。",
            "INVALID_PARAMS": "请求参数不符合要求，请检查后再提交。",
            "PLUGIN_NOT_FOUND": "所选插件不存在，请选择当前可用的插件。",
        }
        return {
            "schemaVersion": "rag-ime.local-api-error.v1",
            "ok": False,
            "errorCode": "runtime_command_rejected",
            "causeCode": cause_code,
            "retryable": False,
            "error": rejection_messages.get(
                cause_code,
                "这次操作未被接受，请检查请求参数和所选资源。",
            ),
            "recovery": {"action": "review_request"},
        }
    if cause_code and cause_code not in {
        "RUNTIME_NOT_RUNNING", "SESSION_NOT_FOUND", "SESSION_BUSY",
        "ROOM_SESSION_BUSY", "REQUEST_ALREADY_ACTIVE", "SETTLED_TIMEOUT",
        "SETTLEMENT_WAITER_LIMIT",
    }:
        # INTERNAL_ERROR also covers npm/Git/IO failures. The terminal error
        # alone cannot prove which effects occurred, so do not blame inputs
        # or suggest automatically replaying a potentially partial operation.
        return {
            "schemaVersion": "rag-ime.local-api-error.v1",
            "ok": False,
            "errorCode": "runtime_operation_failed",
            "causeCode": cause_code,
            "retryable": False,
            "error": (
                "操作过程中发生异常，结果暂未确认。"
                "请先检查当前状态，再决定是否重试。"
            ),
            "recovery": {"action": "inspect_result"},
        }
    return {
        "schemaVersion": "rag-ime.local-api-error.v1",
        "ok": False,
        "errorCode": "session_runtime_unavailable",
        **({"causeCode": cause_code} if cause_code else {}),
        "retryable": True,
        "error": "session runtime is temporarily unavailable",
        "recovery": {"action": "retry"},
    }


class DebugRequestHandler(BaseHTTPRequestHandler):
    service: DebugImeService
    static_dir: Path

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        parsed = urlparse(self.path)
        if not self._authorize_gateway_request("GET", parsed):
            return
        if self._serve_isolated_html_preview(parsed.path):
            return
        if self._serve_gateway_static(parsed.path):
            return
        descriptor_route = find_route("GET", parsed.path)
        if descriptor_route is not None:
            if descriptor_route.error_response is not None:
                try:
                    self._dispatch_descriptor_route(descriptor_route, query=parse_qs(parsed.query or ""))
                except Exception as exc:
                    self._write_json(*descriptor_route.error_response(exc))
            elif descriptor_route.handler in {"agent.eval_lab_apps", "agent.eval_lab_app_download"}:
                try:
                    self._dispatch_descriptor_route(descriptor_route, query=parse_qs(parsed.query or ""))
                except Exception as exc:
                    self._write_json(*lab_project_error_response(exc))
            else:
                self._dispatch_descriptor_route(
                    descriptor_route, query=parse_qs(parsed.query or "")
                )
            return
        if parsed.path == "/api/events/stream":
            self._stream_management_events()
            return
        if parsed.path == "/api/agent/events":
            query = parse_qs(parsed.query or "")
            self._stream_agent_control_events(
                after_event_id=(
                    self.headers.get("Last-Event-ID", "")
                    or _query_first(query, "afterEventId")
                    or _query_first(query, "resumeToken")
                )
            )
            return
        if parsed.path in (
            "/api/observability/events",
            "/control/v1/observability/events",
        ):
            query = parse_qs(parsed.query or "")
            self._stream_observation_events(
                after_event_id=(
                    self.headers.get("Last-Event-ID", "")
                    or _query_first(query, "lastEventId")
                    or _query_first(query, "afterEventId")
                    or _query_first(query, "resumeToken")
                ),
                filters={
                    key: _query_first(query, key)
                    for key in ("sessionId", "roomId", "traceId", "runId", "category", "status")
                    if _query_first(query, key)
                },
            )
            return
        agent_session_id, agent_action = agent_session_route(parsed.path)
        background_job_session_id, background_job_id, background_job_action = (
            agent_background_job_route(parsed.path)
        )
        if agent_session_id and agent_action == "events":
            query = parse_qs(parsed.query or "")
            self._stream_agent_events(
                agent_session_id,
                after_event_id=(
                    self.headers.get("Last-Event-ID", "")
                    or _query_first(query, "afterEventId")
                    or _query_first(query, "resumeToken")
                ),
            )
            return
        context_session_id, context_item_id, context_item_action = agent_context_item_route(
            parsed.path
        )
        context_trace_session_id, context_trace_id = agent_context_trace_route(parsed.path)
        agent_room_id, room_action = agent_room_route(parsed.path)
        (
            room_work_room_id,
            room_work_item_id,
            room_work_action,
        ) = agent_room_work_route(parsed.path)
        wake_schedule_id, wake_schedule_action = agent_wake_schedule_route(parsed.path)
        subagent_run_id, subagent_action = agent_subagent_route(parsed.path)
        artifact_id = agent_artifact_route(parsed.path)
        collaboration_profile_id = agent_collaboration_profile_route(parsed.path)
        work_document_id, work_document_action = agent_work_document_route(parsed.path)
        if agent_room_id and room_action == "events":
            query = parse_qs(parsed.query or "")
            self._stream_agent_room_events(
                agent_room_id,
                after_event_id=(
                    self.headers.get("Last-Event-ID", "")
                    or _query_first(query, "afterEventId")
                    or _query_first(query, "resumeToken")
                ),
            )
            return
        if parsed.path in ("/api/control/v1/bootstrap", "/api/agent/control/bootstrap"):
            self._write_json(
                HTTPStatus.OK,
                self.service.control_bootstrap(self._request_access_context()),
            )
            return
        if parsed.path == "/api/agent/control/capabilities":
            self._write_json(
                HTTPStatus.OK,
                self.service.control_capabilities(self._request_access_context()),
            )
            return
        query = parse_qs(parsed.query or "")
        sandbox_run_id, sandbox_run_action = observability_sandbox_run_route(parsed.path)
        if sandbox_run_action == "list":
            try:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.observability_sandbox_runs({
                        "limit": _query_first(query, "limit"),
                    }),
                )
            except (TypeError, ValueError):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "schemaVersion": "rag-ime.observability-sandbox-run-error.v1",
                        "ok": False,
                        "errorCode": "invalid_sandbox_run_id",
                        "error": "Invalid SandboxRun list request",
                    },
                )
            except Exception:
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "schemaVersion": "rag-ime.observability-sandbox-run-error.v1",
                        "ok": False,
                        "errorCode": "sandbox_run_unavailable",
                        "error": "SandboxRun ledger is unavailable",
                    },
                )
            return
        if sandbox_run_action == "get":
            try:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.observability_sandbox_run({
                        "sandboxRunId": sandbox_run_id,
                    }),
                )
            except KeyError:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "schemaVersion": "rag-ime.observability-sandbox-run-error.v1",
                        "ok": False,
                        "errorCode": "sandbox_run_not_found",
                        "error": "SandboxRun not found",
                        "sandboxRunId": sandbox_run_id,
                    },
                )
            except ValueError:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "schemaVersion": "rag-ime.observability-sandbox-run-error.v1",
                        "ok": False,
                        "errorCode": "invalid_sandbox_run_id",
                        "error": "Invalid SandboxRun ID",
                        "sandboxRunId": sandbox_run_id,
                    },
                )
            except TraceContractError:
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "schemaVersion": "rag-ime.observability-sandbox-run-error.v1",
                        "ok": False,
                        "errorCode": "sandbox_run_invalid",
                        "error": "SandboxRun is invalid",
                        "sandboxRunId": sandbox_run_id,
                    },
                )
            except Exception:
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "schemaVersion": "rag-ime.observability-sandbox-run-error.v1",
                        "ok": False,
                        "errorCode": "sandbox_run_unavailable",
                        "error": "SandboxRun ledger is unavailable",
                        "sandboxRunId": sandbox_run_id,
                    },
                )
            return
        if parsed.path in (
            "/api/observability/eval-suites",
            "/control/v1/observability/eval-suites",
        ):
            try:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.list_eval_suites({
                        "limit": _query_first(query, "limit"),
                    }),
                )
            except (TypeError, ValueError):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "errorCode": "invalid_request",
                        "error": "Invalid Eval suite catalog request",
                    },
                )
            except Exception:
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "errorCode": "suite_catalog_unavailable",
                        "error": "Eval suite catalog is unavailable",
                    },
                )
            return
        if parsed.path == "/api/observability/eval-schedules":
            try:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.list_eval_schedules({
                        "limit": _query_first(query, "limit"),
                    }),
                )
            except (TypeError, ValueError):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "schemaVersion": "rag-ime.eval-schedule-error.v1",
                        "ok": False,
                        "errorCode": "invalid_request",
                        "error": "Invalid Eval schedule request",
                    },
                )
            except Exception:
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "schemaVersion": "rag-ime.eval-schedule-error.v1",
                        "ok": False,
                        "errorCode": "schedule_unavailable",
                        "error": "Eval schedule is unavailable",
                    },
                )
            return
        eval_schedule_id, eval_schedule_action = observability_eval_schedule_route(
            parsed.path
        )
        if eval_schedule_id and eval_schedule_action == "runs":
            try:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.eval_schedule_runs(
                        eval_schedule_id,
                        {"limit": _query_first(query, "limit")},
                    ),
                )
            except KeyError:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "schemaVersion": "rag-ime.eval-schedule-error.v1",
                        "ok": False,
                        "errorCode": "schedule_not_found",
                        "error": "Eval schedule not found",
                    },
                )
            except (TypeError, ValueError):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "schemaVersion": "rag-ime.eval-schedule-error.v1",
                        "ok": False,
                        "errorCode": "invalid_request",
                        "error": "Invalid Eval schedule request",
                    },
                )
            except Exception:
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "schemaVersion": "rag-ime.eval-schedule-error.v1",
                        "ok": False,
                        "errorCode": "schedule_unavailable",
                        "error": "Eval schedule is unavailable",
                    },
                )
            return
        if work_document_id and work_document_action == "detail":
            try:
                response = self.service.agent.work_documents.detail(work_document_id)
            except KeyError:
                self._write_json(
                    HTTPStatus.NOT_FOUND, {"ok": False, "error": "work document not found"}
                )
                return
            except Exception as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST, {"ok": False, "error": _safe_debug_error(exc)}
                )
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if parsed.path == "/api/browser/snapshots/latest":
            tab_value = _query_first(query, "tabId")
            try:
                response = self.service.browser_control.latest_snapshot(
                    device_id=_query_first(query, "deviceId"),
                    tab_id=int(tab_value) if tab_value else None,
                    include_markdown=_query_first(query, "includeMarkdown") != "false",
                )
            except (BrowserControlError, ValueError) as exc:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": str(exc), "code": "browser_snapshot_unavailable"},
                )
                return
            self._write_json(
                HTTPStatus.OK,
                response,
            )
            return
        if parsed.path.startswith("/api/browser/snapshots/") and parsed.path.endswith("/image"):
            snapshot_id = parsed.path.removeprefix("/api/browser/snapshots/").removesuffix("/image").strip("/")
            try:
                mime_type, data = self.service.browser_control.snapshot_image(snapshot_id)
            except BrowserControlError as exc:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": str(exc), "code": "browser_snapshot_image_unavailable"},
                )
                return
            self._write_binary(
                HTTPStatus.OK,
                data,
                mime_type=mime_type,
                etag=hashlib.sha256(data).hexdigest(),
            )
            return
        if parsed.path == "/api/browser/traces":
            self._write_json(
                HTTPStatus.OK,
                self.service.browser_control.traces(
                    limit=int(_query_first(query, "limit") or 50),
                ),
            )
            return
        if parsed.path in (
            "/api/observability/snapshot",
            "/control/v1/observability/snapshot",
        ):
            try:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.observation_snapshot(
                        {
                            key: _query_first(query, key)
                            for key in (
                                "limit",
                                "beforeSequence",
                                "sessionId",
                                "roomId",
                                "traceId",
                                "runId",
                                "category",
                                "status",
                            )
                            if _query_first(query, key)
                        }
                    ),
                )
            except Exception as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": str(exc)},
                )
            return
        if parsed.path in (
            "/api/observability/evals",
            "/control/v1/observability/evals",
        ):
            try:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.observation_evals({
                        "traceId": _query_first(query, "traceId"),
                        "limit": _query_first(query, "limit"),
                    }),
                )
            except (TraceContractError, ValueError):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "Invalid Eval request"},
                )
            return
        if parsed.path in {"/api/observability/trace-optimization", "/api/observability/trace-optimization/capabilities"}:
            if not self._trace_repair_loopback_allowed():
                self._write_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Trace optimization is local-only"})
                return
            try:
                response = (self.service.agent.trace_optimization_capabilities()
                    if parsed.path.endswith("/capabilities") else self.service.agent.trace_optimization_library(
                        {key: _query_first(query, key) for key in ("projectId", "query", "patternId", "revision", "offset") if key in query}))
                self._write_json(HTTPStatus.OK, response)
            except KeyError:
                self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Trace optimization record not found"})
            except (TypeError, ValueError):
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid Trace optimization request"})
            return
        diagnostic_report_id, diagnostic_report_action = (
            observability_trace_diagnostic_report_route(parsed.path)
        )
        if diagnostic_report_action in {"collection", "get"}:
            try:
                response = (
                    self.service.agent.list_trace_diagnostic_reports(
                        {
                            "limit": _query_first(query, "limit"),
                            "cursor": _query_first(query, "cursor"),
                        }
                    )
                    if diagnostic_report_action == "collection"
                    else self.service.agent.trace_diagnostic_report(
                        diagnostic_report_id
                    )
                )
                self._write_json(HTTPStatus.OK, response)
            except KeyError:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "Trace diagnostic report not found"},
                )
            except (TypeError, ValueError):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "Invalid Trace diagnostic report request"},
                )
            return
        trace_repair_receipt_id, trace_repair_action = observability_trace_repair_route(
            parsed.path
        )
        if trace_repair_action == "get":
            if not self._trace_repair_loopback_allowed():
                self._write_json(
                    HTTPStatus.FORBIDDEN,
                    {
                        "schemaVersion": "rag-ime.trace-repair-error.v1",
                        "ok": False,
                        "errorCode": "trace_repair_loopback_only",
                        "error": "Trace repair is available only from the local machine",
                    },
                )
                return
            try:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.get_trace_repair_receipt(
                        trace_repair_receipt_id
                    ),
                )
            except KeyError:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "schemaVersion": "rag-ime.trace-repair-error.v1",
                        "ok": False,
                        "errorCode": "repair_receipt_not_found",
                        "error": "Trace repair receipt not found",
                        "repairReceiptId": trace_repair_receipt_id,
                    },
                )
            except (TraceRepairValidationError, ValueError):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "schemaVersion": "rag-ime.trace-repair-error.v1",
                        "ok": False,
                        "errorCode": "invalid_repair_receipt_id",
                        "error": "Invalid Trace repair receipt ID",
                    },
                )
            return
        trace_replay_id, trace_replay_action = observability_trace_replay_route(
            parsed.path
        )
        if trace_replay_action in {"case-get", "verification-get"}:
            if not self._trace_repair_loopback_allowed():
                self._write_json(
                    HTTPStatus.FORBIDDEN,
                    {
                        "schemaVersion": "rag-ime.trace-verification-error.v1",
                        "ok": False,
                        "errorCode": "trace_replay_loopback_only",
                        "error": "Trace replay is available only from the local machine",
                    },
                )
                return
            try:
                response = (
                    self.service.agent.get_trace_replay_case(trace_replay_id)
                    if trace_replay_action == "case-get"
                    else self.service.agent.get_trace_verification_receipt(
                        trace_replay_id
                    )
                )
                self._write_json(HTTPStatus.OK, response)
            except KeyError:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "schemaVersion": "rag-ime.trace-verification-error.v1",
                        "ok": False,
                        "errorCode": "trace_replay_record_not_found",
                        "error": "Trace replay record not found",
                    },
                )
            except TraceVerificationValidationError:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "schemaVersion": "rag-ime.trace-verification-error.v1",
                        "ok": False,
                        "errorCode": "invalid_trace_replay_id",
                        "error": "Invalid Trace replay record ID",
                    },
                )
            return
        trace_id = observability_trace_route(parsed.path)
        if trace_id is not None:
            try:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.observation_trace(
                        {
                            "traceId": trace_id,
                            "limit": _query_first(query, "limit"),
                            "beforeSequence": _query_first(query, "beforeSequence"),
                        }
                    ),
                )
            except KeyError:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "schemaVersion": "rag-ime.observability-trace-error.v1",
                        "ok": False,
                        "errorCode": "trace_not_found",
                        "error": "Trace not found",
                        "traceId": trace_id,
                    },
                )
            except TraceContractError:
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "schemaVersion": "rag-ime.observability-trace-error.v1",
                        "ok": False,
                        "errorCode": "trace_invalid",
                        "error": "Trace is invalid",
                        "traceId": trace_id,
                    },
                )
            except ValueError as exc:
                code = (
                    "invalid_trace_id"
                    if str(exc) == "invalid_trace_id"
                    else "trace_invalid"
                )
                self._write_json(
                    (
                        HTTPStatus.BAD_REQUEST
                        if code == "invalid_trace_id"
                        else HTTPStatus.INTERNAL_SERVER_ERROR
                    ),
                    {
                        "schemaVersion": "rag-ime.observability-trace-error.v1",
                        "ok": False,
                        "errorCode": code,
                        "error": (
                            "Invalid Trace ID"
                            if code == "invalid_trace_id"
                            else "Trace is invalid"
                        ),
                        "traceId": trace_id,
                    },
                )
            return
        knowledge_parts = _knowledge_route_parts(parsed.path)
        if knowledge_parts is not None:
            try:
                control = self._knowledge_control()
                if knowledge_parts == ():
                    response = control.list_bases()
                elif knowledge_parts == ("health",):
                    response = control.health()
                elif knowledge_parts == ("parsers",):
                    response = control.parsers()
                elif knowledge_parts == ("embedding-profile",):
                    response = control.embedding_profile()
                elif len(knowledge_parts) == 1:
                    response = control.get_base(knowledge_parts[0])
                elif len(knowledge_parts) == 2 and knowledge_parts[1] == "documents":
                    response = control.list_documents(knowledge_parts[0])
                elif len(knowledge_parts) == 2 and knowledge_parts[1] == "jobs":
                    response = control.jobs(knowledge_parts[0])
                elif len(knowledge_parts) == 2 and knowledge_parts[1] == "graph":
                    response = control.graph(
                        knowledge_parts[0],
                        {
                            "documentId": _query_first(query, "documentId"),
                            "query": _query_first(query, "query"),
                            "kinds": _query_first(query, "kinds"),
                            "limit": _query_first(query, "limit"),
                            "depth": _query_first(query, "depth"),
                            "excludeChunks": _query_first(query, "excludeChunks"),
                            "focusId": _query_first(query, "focusId"),
                        },
                    )
                    validate_contract(response, "knowledge-graph.v1.json")
                elif len(knowledge_parts) == 2 and knowledge_parts[1] == "reindex-preview":
                    response = control.reindex_preview(knowledge_parts[0])
                elif len(knowledge_parts) == 3 and knowledge_parts[1] == "documents":
                    response = control.document_detail(
                        knowledge_parts[0],
                        knowledge_parts[2],
                        {
                            "offset": _query_first(query, "offset"),
                            "limit": _query_first(query, "limit"),
                            "lineOffset": _query_first(query, "lineOffset"),
                            "lineLimit": _query_first(query, "lineLimit"),
                        },
                    )
                elif len(knowledge_parts) == 4 and knowledge_parts[1] == "documents" and knowledge_parts[3] == "source":
                    self._write_knowledge_binary(control.document_source(knowledge_parts[0], knowledge_parts[2]))
                    return
                elif len(knowledge_parts) == 5 and knowledge_parts[1] == "documents" and knowledge_parts[3] == "assets":
                    self._write_knowledge_binary(
                        control.document_asset(knowledge_parts[0], knowledge_parts[2], knowledge_parts[4])
                    )
                    return
                elif len(knowledge_parts) == 4 and knowledge_parts[1] == "documents" and knowledge_parts[3] == "content":
                    response = control.open(
                        knowledge_parts[0],
                        knowledge_parts[2],
                        {
                            "chunkId": _query_first(query, "chunkId"),
                            "startLine": _query_first(query, "startLine"),
                            "lines": _query_first(query, "lines"),
                        },
                    )
                else:
                    self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown endpoint"})
                    return
                self._write_json(HTTPStatus.OK, response)
            except Exception as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, self._knowledge_error(exc))
            return
        if parsed.path == "/api/agent/providers/oauth/status":
            try:
                response = self.service.pi_provider_auth.oauth_status(
                    _query_first(query, "loginId")
                )
            except PiProviderAuthError as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "schemaVersion": "rag-ime.pi-provider-oauth-status.v1",
                        "ok": False,
                        "error": str(exc),
                    },
                )
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if background_job_session_id:
            try:
                if background_job_action == "collection":
                    response = self.service.agent.background_jobs.list(
                        background_job_session_id,
                        limit=_query_first(query, "limit") or 50,
                        status=_query_first(query, "status"),
                    )
                elif background_job_action == "status":
                    response = self.service.agent.background_jobs.status(
                        background_job_session_id,
                        background_job_id,
                    )
                elif background_job_action == "logs":
                    response = self.service.agent.background_jobs.logs(
                        background_job_session_id,
                        background_job_id,
                        cursor=_query_first(query, "cursor") or 0,
                        limit_bytes=_query_first(query, "limitBytes") or 65_536,
                    )
                else:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "unknown endpoint"},
                    )
                    return
            except Exception as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": _safe_debug_error(exc)},
                )
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if agent_session_id and agent_action == "workflow":
            try:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.workflow_state(agent_session_id),
                )
            except Exception as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        if agent_session_id and agent_action == "workspace":
            try:
                response = self.service.agent_tools.workspace_list(
                    agent_session_id,
                    {
                        "path": _query_first(query, "path"),
                        "depth": _query_first(query, "depth"),
                        "limit": _query_first(query, "limit"),
                    },
                )
            except Exception as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": _safe_debug_error(exc)},
                )
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if agent_session_id and agent_action == "workspace-file":
            try:
                response = self.service.agent_tools.workspace_read(
                    agent_session_id,
                    {
                        "path": _query_first(query, "path"),
                        "offset": _query_first(query, "offset"),
                        "limit": _query_first(query, "limit"),
                    },
                )
            except Exception as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": _safe_debug_error(exc)},
                )
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if agent_session_id and agent_action == "debug-context":
            if not self.service._include_raw_text():
                self._write_json(
                    HTTPStatus.FORBIDDEN,
                    {
                        "schemaVersion": "rag-ime.pi-debug-context-response.v1",
                        "ok": False,
                        "available": False,
                        "transient": True,
                        "error": "本机原始上下文调试尚未启用",
                    },
                )
                return
            try:
                response = self.service.agent.debug_context(
                    agent_session_id,
                    _query_first(query, "turnId"),
                )
            except Exception as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "schemaVersion": "rag-ime.pi-debug-context-response.v1",
                        "ok": False,
                        "available": False,
                        "transient": True,
                        "error": _safe_debug_error(exc),
                    },
                )
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if context_session_id and context_item_action == "list":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.list_context_items(
                    context_session_id,
                    {
                        "status": _query_first(query, "status"),
                        "limit": _query_first(query, "limit"),
                    },
                ),
            )
            return
        if context_trace_session_id and not context_trace_id:
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.list_context_traces(
                    context_trace_session_id,
                    {"limit": _query_first(query, "limit")},
                ),
            )
            return
        if context_trace_session_id and context_trace_id:
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.context_trace(
                    context_trace_session_id,
                    context_trace_id,
                ),
            )
            return
        if wake_schedule_id and wake_schedule_action == "runs":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.wake_schedule_runs(
                    wake_schedule_id,
                    {"limit": _query_first(query, "limit")},
                ),
            )
            return
        if wake_schedule_id and not wake_schedule_action:
            self._write_json(
                HTTPStatus.OK,
                {
                    "schemaVersion": "rag-ime.agent-wake-schedule-get.v1",
                    "ok": True,
                    "schedule": self.service.agent.get_wake_schedule(wake_schedule_id),
                },
            )
            return
        if parsed.path == "/api/agent/governance":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.governance_read_model(
                    scope_key=_query_first(query, "scopeKey") or None
                ),
            )
            return
        if parsed.path == "/api/agent/knowledge-governance":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.knowledge_governance_read_model(),
            )
            return
        if collaboration_profile_id:
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.collaboration_profile_projection(collaboration_profile_id),
            )
            return
        if agent_room_id and room_action == "snapshot":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.room_snapshot(agent_room_id),
            )
            return
        if agent_room_id and room_action == "conversation":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.room_conversation_snapshot(agent_room_id),
            )
            return
        if agent_room_id and room_action == "start-gate":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.room_start_gate(agent_room_id),
            )
            return
        if agent_room_id and room_action == "history":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.room_history(
                    agent_room_id,
                    {
                        "beforeSequence": _query_first(query, "beforeSequence"),
                        "limit": _query_first(query, "limit"),
                    },
                ),
            )
            return
        if agent_room_id and room_action == "topics":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.room_topics(
                    agent_room_id,
                    {"includeArchived": _query_first(query, "includeArchived")},
                ),
            )
            return
        if agent_room_id and room_action == "artifacts":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.room_artifacts(
                    agent_room_id,
                    {
                        "includeArchived": _query_first(query, "includeArchived"),
                        "topicId": _query_first(query, "topicId"),
                        "limit": _query_first(query, "limit"),
                    },
                ),
            )
            return
        if room_work_room_id and room_work_action == "collection":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.room_work_items(
                    room_work_room_id,
                    {
                        "state": _query_first(query, "state"),
                        "ownerParticipantId": _query_first(
                            query,
                            "ownerParticipantId",
                        ),
                        "limit": _query_first(query, "limit"),
                    },
                ),
            )
            return
        if (
            room_work_room_id
            and room_work_item_id
            and room_work_action == "get"
        ):
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.room_work_item(
                    room_work_room_id,
                    room_work_item_id,
                ),
            )
            return
        if agent_room_id and not room_action:
            self._write_json(HTTPStatus.OK, self.service.agent.room(agent_room_id))
            return
        if parsed.path == "/api/agent/tools":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent_tools.manifests(
                    session_id=_query_first(query, "sessionId"),
                ),
            )
            return
        if parsed.path == "/api/agent/lifecycle-hooks":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent_lifecycle_hooks.snapshot(
                    limit=_bounded_int(
                        _query_first(query, "limit"),
                        default=30,
                        minimum=1,
                        maximum=100,
                    )
                ),
            )
            return
        if parsed.path == "/api/agent/role-book":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent_role_book_control.catalog(
                    role_id=_query_first(query, "roleId"),
                    role_version=_query_first(query, "roleVersion"),
                    limit=_bounded_int(
                        _query_first(query, "limit"),
                        default=30,
                        minimum=1,
                        maximum=100,
                    ),
                ),
            )
            return
        if parsed.path == "/api/agent/personal-context/observability":
            self._write_json(
                HTTPStatus.OK,
                self.service.personal_context_observability.snapshot(
                    session_id=_query_first(query, "sessionId"),
                    role_id=_query_first(query, "roleId"),
                    limit=_bounded_int(
                        _query_first(query, "limit"),
                        default=20,
                        minimum=1,
                        maximum=100,
                    ),
                ),
            )
            return
        if parsed.path == "/api/agent/subagents/runs":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.delegation_status(
                    _query_first(query, "sessionId"),
                    {"limit": _query_first(query, "limit")},
                ),
            )
            return
        if subagent_run_id and not subagent_action:
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.delegation_status(
                    _query_first(query, "sessionId"),
                    {"runId": subagent_run_id},
                ),
            )
            return
        if subagent_run_id and subagent_action == "console":
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.delegation_console(
                    _query_first(query, "sessionId"),
                    subagent_run_id,
                ),
            )
            return
        if artifact_id:
            self._write_json(
                HTTPStatus.OK,
                self.service.agent.delegation_artifact(
                    _query_first(query, "sessionId"),
                    artifact_id,
                    {"limit": _query_first(query, "limit")},
                ),
            )
            return
        if parsed.path == "/api/agent/memory-maintenance":
            run_id = _query_first(query, "runId")
            job_id = _query_first(query, "jobId")
            self._write_json(
                HTTPStatus.OK,
                self.service.agent_memory_maintenance_trigger_status(job_id)
                if job_id
                else self.service.agent_memory_maintenance_run(
                    {
                        "runId": run_id,
                        "project": _query_first(query, "project"),
                    }
                )
                if run_id
                else self.service.agent_memory_maintenance_status(
                    {
                        "project": _query_first(query, "project"),
                        "limit": _query_first(query, "limit"),
                        "projectionOnly": _query_first(query, "projectionOnly"),
                    }
                ),
            )
            return
        if parsed.path == "/api/memory/lifecycle/status":
            self._write_json(HTTPStatus.OK, self.service.memory_lifecycle_status({"project": _query_first(query, "project")}))
            return
        if parsed.path == "/api/memory/lifecycle/report":
            self._write_json(HTTPStatus.OK, self.service.memory_lifecycle_report({
                "project": _query_first(query, "project"),
                "date": _query_first(query, "date"),
                "timezone": _query_first(query, "timezone"),
                "noTimeline": _query_first(query, "noTimeline") == "true",
            }))
            return
        media_id, media_action = agent_media_route(parsed.path)
        if media_id:
            try:
                session_id = _query_first(query, "sessionId")
                room_id = _query_first(query, "roomId")
                if media_action == "content":
                    receipt, content = self.service.agent.media_content(
                        media_id,
                        session_id=session_id,
                        room_id=room_id,
                    )
                    self._write_binary(
                        HTTPStatus.OK,
                        content,
                        mime_type=str(receipt["mimeType"]),
                        etag=str(receipt["sha256"]),
                    )
                elif media_action == "preview":
                    self._write_json(
                        HTTPStatus.OK,
                        self.service.agent.file_preview(
                            media_id,
                            session_id=session_id,
                            expected_sha256=_query_first(query, "sha256"),
                        ),
                    )
                else:
                    self._write_json(
                        HTTPStatus.OK,
                        self.service.agent.media_receipt(
                            media_id,
                            session_id=session_id,
                            room_id=room_id,
                        ),
                    )
            except Exception as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        approval_id, approval_action = agent_approval_route(parsed.path)
        if approval_id and not approval_action:
            self._write_json(
                HTTPStatus.OK,
                {
                    "schemaVersion": "rag-ime.agent-approval-get.v1",
                    "ok": True,
                    "approval": self.service.agent.sessions.get_approval(approval_id),
                },
            )
            return
        if agent_session_id and agent_action == "messages":
            query = parse_qs(parsed.query or "", keep_blank_values=True)
            view_values = query.get("view", [])
            if view_values and view_values != ["recent"]:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "unsupported Session snapshot view"},
                )
                return
            try:
                response = (
                    self.service.agent.message_snapshot.messages(
                        agent_session_id,
                        view="recent",
                    )
                    if view_values
                    else self.service.agent.messages(agent_session_id)
                )
            except AgentRuntimeError as exc:
                self._write_json(
                    HTTPStatus.CONFLICT,
                    _agent_session_runtime_error_payload(exc),
                )
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if agent_session_id and agent_action == "forks":
            try:
                self._write_json(HTTPStatus.OK, self.service.agent.fork_candidates(agent_session_id))
            except Exception as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        if agent_session_id and agent_action == "commands":
            self._write_json(HTTPStatus.OK, self.service.agent.command_catalog(agent_session_id))
            return
        if agent_session_id and agent_action == "models":
            try:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.model_catalog(agent_session_id),
                )
            except AgentRuntimeError as exc:
                # Model discovery restores the Session runtime, which can
                # legitimately fail after an ephemeral workspace disappears.
                # Keep the transcript readable and project a stable public
                # failure instead of dropping the HTTP connection with an
                # uncaught runtime exception (and leaking the local path).
                self._write_json(
                    HTTPStatus.CONFLICT,
                    _agent_session_runtime_error_payload(exc),
                )
            return
        if agent_session_id and agent_action == "intercom":
            try:
                response = self.service.agent.list_room_intercom(
                    agent_session_id,
                    {
                        "status": _query_first(query, "status"),
                        "limit": _query_first(query, "limit"),
                    },
                )
            except ValueError as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "schemaVersion": "rag-ime.local-api-error.v1",
                        "ok": False,
                        "errorCode": "invalid_request",
                        "error": " ".join(str(exc).split())[:256] or "invalid request",
                    },
                )
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if parsed.path.startswith("/api/runtime/job/"):
            job_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self._write_json(HTTPStatus.OK, self.service.management.runtime_job(job_id))
            return
        if parsed.path == "/api/memory/graph":
            try:
                query = parse_qs(parsed.query or "", keep_blank_values=True)
                payload = _strict_read_query(query, _MEMORY_GRAPH_QUERY_FIELDS)
                response = self.service.management.memory_graph(payload)
                validate_contract(response, "memory-graph.v1.json")
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, _memory_read_error(str(exc)))
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if parsed.path.startswith(_MEMORY_REFERENCE_PATH_PREFIX):
            try:
                kind, reference_id = _memory_reference_path(parsed.path)
                response = self.service.management.memory_reference(
                    kind,
                    reference_id,
                )
                validate_contract(response, "memory-reference.v1.json")
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, _memory_read_error(str(exc)))
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if parsed.path.startswith(_MEMORY_ENTITY_PATH_PREFIX):
            try:
                kind, entity_id = _memory_entity_path(parsed.path)
                query = parse_qs(parsed.query or "", keep_blank_values=True)
                payload = _strict_read_query(query, _MEMORY_ENTITY_QUERY_FIELDS)
                response = self.service.management.memory_entity(kind, entity_id, payload)
                validate_contract(response, "memory-entity.v1.json")
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, _memory_read_error(str(exc)))
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if parsed.path == "/api/memory/activity-timeline/calendar":
            try:
                response = self.service.activity_timeline_calendar(
                    {"month": _query_first(query, "month")}
                )
            except ValueError as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    _memory_read_error(str(exc)),
                )
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if parsed.path == "/api/memory/activity-timeline":
            try:
                response = self.service.activity_timeline_review(
                    {
                        "timelineId": _query_first(query, "timelineId"),
                        "date": _query_first(query, "date"),
                        "status": _query_first(query, "status"),
                    }
                )
            except ValueError as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    _memory_read_error(str(exc)),
                )
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if parsed.path == "/api/planning/dashboard":
            self._write_json(
                HTTPStatus.OK,
                self.service.management.planning_dashboard(
                    plan_date=_query_first(query, "date"),
                    project=_query_first(query, "project"),
                ),
            )
            return
        if parsed.path in {
            "/api/memory/apps",
            "/api/memory/books",
            "/api/memory/atoms",
            "/api/memory/timelines",
            "/api/memory/tags",
            "/api/memory/phrases",
            "/api/memory/evidence",
            "/api/memory/groups",
            "/api/memory/negative",
        }:
            kind = parsed.path.rsplit("/", 1)[-1]
            self._write_json(
                HTTPStatus.OK,
                self.service.management.memory_page(
                    kind,
                    page_request(
                        {
                            "limit": _query_first(query, "limit"),
                            "cursor": _query_first(query, "cursor"),
                            "query": _query_first(query, "query"),
                            "status": _query_first(query, "status"),
                            "ownerKind": _query_first(query, "ownerKind"),
                            "ownerId": _query_first(query, "ownerId"),
                        }
                    ),
                ),
            )
            return
        if parsed.path == "/api/history/page":
            self._write_json(
                HTTPStatus.OK,
                self.service.management.history_page(
                    page_request(
                        {
                            "limit": _query_first(query, "limit"),
                            "cursor": _query_first(query, "cursor"),
                            "query": _query_first(query, "query"),
                            "status": _query_first(query, "filter"),
                        }
                    )
                ),
            )
            return
        if parsed.path == "/api/history/detail":
            try:
                response = self.service.management.history_detail(
                    _query_first(query, "eventId")
                )
            except ManagementWorkError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, exc.payload())
                return
            self._write_json(
                HTTPStatus.OK if response.get("ok") is True else HTTPStatus.NOT_FOUND,
                response,
            )
            return
        if parsed.path in ("/api/active-rag/route-status",):
            local_only_raw = _query_first(query, "localOnly")
            self._write_json(
                HTTPStatus.OK,
                self.service.active_rag_route_status(
                    local_only=_bool(local_only_raw, default=True) if local_only_raw else None
                ),
            )
            return
        if parsed.path.startswith("/api/active-rag/session/"):
            session_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self._write_json(HTTPStatus.OK, self.service.active_rag_status({"sessionId": session_id}))
            return
        if parsed.path.startswith("/api/memory/optimizer/trace/"):
            trace_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self._write_json(HTTPStatus.OK, self.service.memory_optimizer_trace({"traceId": trace_id}))
            return
        if parsed.path.startswith("/api/memory/candidate/") and parsed.path.endswith("/explain"):
            candidate_id = unquote(parsed.path[len("/api/memory/candidate/") : -len("/explain")].strip("/"))
            self._write_json(
                HTTPStatus.OK,
                self.service.memory_candidate_explain(
                    {
                        "candidateId": candidate_id,
                        "contextHash": _query_first(query, "contextHash"),
                    }
                ),
            )
            return
        if parsed.path in ("", "/"):
            self._write_json(
                HTTPStatus.OK,
                {
                    "schemaVersion": "rag-ime.local-api-root.v1",
                    "ok": True,
                    "service": self._server_name(),
                    "controlCenter": "RagImeControl.app",
                    "browserUI": False,
                },
            )
            return
        self._write_json(
            HTTPStatus.NOT_FOUND,
            {"schemaVersion": "rag-ime.local-api-error.v1", "ok": False, "error": "route_not_found"},
        )

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        parsed = urlparse(self.path)
        if not self._authorize_gateway_request("POST", parsed):
            return
        try:
            path = parsed.path
            knowledge_parts = _knowledge_route_parts(path)
            if (
                knowledge_parts is not None
                and len(knowledge_parts) == 3
                and knowledge_parts[1:] == ("documents", "import")
            ):
                security_error = self._management_post_security_error(path, require_json=False)
                if security_error is not None:
                    self._write_json(HTTPStatus.FORBIDDEN, security_error)
                    return
                if self.headers.get("Content-Encoding", "").strip():
                    raise ValueError("compressed knowledge uploads are not accepted")
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0:
                    raise ValueError("knowledge import requires a non-empty Content-Length")
                if length > _MAX_KNOWLEDGE_IMPORT_BYTES:
                    raise ValueError("knowledge document exceeds the 200 MiB limit")
                data = self._read_knowledge_upload(length)
                if len(data) != length:
                    raise ValueError("knowledge upload ended before Content-Length")
                query = parse_qs(parsed.query or "")
                file_name = Path(unquote(_query_first(query, "fileName"))).name
                mime_type = (
                    _query_first(query, "mimeType")
                    or self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    or "application/octet-stream"
                )
                response = self._knowledge_control().import_document(
                    knowledge_parts[0],
                    data=data,
                    file_name=file_name,
                    mime_type=mime_type,
                    parser_provider=_query_first(query, "parserProvider") or "auto",
                )
                self._write_json(HTTPStatus.CREATED, response)
                return
            if path == "/api/agent/media/import":
                security_error = self._management_post_security_error(path, require_json=False)
                if security_error is not None:
                    self._write_json(HTTPStatus.FORBIDDEN, security_error)
                    return
                query = parse_qs(parsed.query or "")
                if self.headers.get("Content-Encoding", "").strip():
                    raise ValueError("compressed agent media uploads are not accepted")
                mime_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                maximum = self.service.agent.media.max_bytes_for_mime(mime_type)
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0:
                    raise ValueError("agent media import requires a non-empty Content-Length")
                if length > maximum:
                    raise ValueError(f"agent media exceeds {maximum} byte limit")
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError("agent media upload ended before Content-Length")
                self._write_json(
                    HTTPStatus.CREATED,
                    self.service.agent.import_media(
                        session_id=_query_first(query, "sessionId"),
                        room_id=_query_first(query, "roomId"),
                        data=data,
                        mime_type=mime_type,
                        file_name=_query_first(query, "fileName"),
                    ),
                )
                return
            if path == "/api/agent/tool/execute":
                provided = self.headers.get("X-RAG-IME-Agent-Token", "")
                expected = self.service.agent.tool_token
                if not provided or not hmac.compare_digest(provided, expected):
                    self._write_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "schemaVersion": "rag-ime.agent-tool-error.v1",
                            "ok": False,
                            "error": "agent capability token required",
                        },
                    )
                    return
                try:
                    result = self.service.agent_tools.execute(self._read_json())
                except WorkspaceSnapshotError as exc:
                    self._write_json(
                        HTTPStatus.CONFLICT,
                        {
                            "schemaVersion": "rag-ime.agent-tool-error.v1",
                            "ok": False,
                            "error": str(exc),
                            "errorCode": exc.code,
                            "retryable": exc.retryable,
                        },
                    )
                    return
                except WorkspaceHarnessError as exc:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "schemaVersion": "rag-ime.agent-tool-error.v1",
                            "ok": False,
                            "error": str(exc),
                            "errorCode": "invalid_workspace_request",
                            "retryable": False,
                        },
                    )
                    return
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    error = str(exc)
                    workflow_gate_closed = error.startswith(
                        "Act Gate blocked workspace mutation ("
                    )
                    self._write_json(
                        HTTPStatus.CONFLICT if workflow_gate_closed else HTTPStatus.BAD_REQUEST,
                        {
                            "schemaVersion": "rag-ime.agent-tool-error.v1",
                            "ok": False,
                            "error": error,
                            "errorCode": (
                                "workflow_gate_closed"
                                if workflow_gate_closed
                                else "invalid_request"
                            ),
                            "retryable": False,
                        },
                    )
                    return
                self._write_json(HTTPStatus.OK, result)
                return
            if path == "/api/agent/tool/lifecycle-event":
                provided = self.headers.get("X-RAG-IME-Agent-Token", "")
                expected = self.service.agent.tool_token
                if not provided or not hmac.compare_digest(provided, expected):
                    self._write_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "schemaVersion": "rag-ime.agent-lifecycle-event-result.v1",
                            "ok": False,
                            "error": "agent capability token required",
                        },
                    )
                    return
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent_lifecycle_hooks.record_event(self._read_json()),
                )
                return
            if path == "/api/agent/tool/context-refresh":
                provided = self.headers.get("X-RAG-IME-Agent-Token", "")
                expected = self.service.agent.tool_token
                if not provided or not hmac.compare_digest(provided, expected):
                    self._write_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "schemaVersion": "rag-ime.agent-tool-error.v1",
                            "ok": False,
                            "error": "agent capability token required",
                        },
                    )
                    return
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.refresh_session_context(self._read_json()),
                )
                return
            if path in {
                "/api/agent/tool/workflow-state",
                "/api/agent/tool/goal-usage",
                "/api/agent/tool/goal-settle",
            }:
                provided = self.headers.get("X-RAG-IME-Agent-Token", "")
                expected = self.service.agent.tool_token
                if not provided or not hmac.compare_digest(provided, expected):
                    self._write_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "schemaVersion": "rag-ime.agent-tool-error.v1",
                            "ok": False,
                            "error": "agent capability token required",
                        },
                    )
                    return
                internal_payload = self._read_json()
                response = (
                    self.service.agent.internal_workflow_state(internal_payload)
                    if path.endswith("/workflow-state")
                    else (
                        self.service.agent.record_goal_usage(internal_payload)
                        if path.endswith("/goal-usage")
                        else self.service.agent.settle_goal_runtime(
                            internal_payload
                        )
                    )
                )
                self._write_json(HTTPStatus.OK, response)
                return
            if path == "/api/agent/tool/approval-result":
                provided = self.headers.get("X-RAG-IME-Agent-Token", "")
                expected = self.service.agent.tool_token
                if not provided or not hmac.compare_digest(provided, expected):
                    self._write_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "schemaVersion": "rag-ime.agent-tool-error.v1",
                            "ok": False,
                            "error": "agent capability token required",
                        },
                    )
                    return
                self._write_json(HTTPStatus.OK, self.service.agent.approval_result(self._read_json()))
                return
            security_error = self._management_post_security_error(path)
            if security_error is not None:
                self._write_json(HTTPStatus.FORBIDDEN, security_error)
                return
            payload = self._read_json()
            diagnostic_report_id, diagnostic_report_action = (
                observability_trace_diagnostic_report_route(path)
            )
            if diagnostic_report_action in {
                "collection",
                "finalize",
                "repair-authorize",
                "repair-verify",
                "optimization",
            }:
                if (diagnostic_report_action.startswith("repair-") or diagnostic_report_action == "optimization") and not self._trace_repair_loopback_allowed():
                    self._write_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "ok": False,
                            "errorCode": "trace_repair_loopback_only",
                            "error": "Trace repair is available only from the local machine",
                        },
                    )
                    return
                try:
                    if diagnostic_report_action == "collection":
                        response = self.service.agent.create_trace_diagnostic_report(payload)
                    elif diagnostic_report_action == "finalize":
                        response = self.service.agent.finalize_trace_diagnostic_report(
                            diagnostic_report_id,
                            payload,
                        )
                    elif diagnostic_report_action == "repair-authorize":
                        response = self.service.agent.authorize_trace_diagnostic_repair(
                            diagnostic_report_id,
                            payload,
                        )
                    elif diagnostic_report_action == "optimization":
                        response = self.service.agent.trace_optimization_command(diagnostic_report_id, payload)
                    else:
                        response = self.service.agent.verify_trace_diagnostic_repair(
                            diagnostic_report_id,
                            payload,
                        )
                    self._write_json(
                        HTTPStatus.CREATED if diagnostic_report_action == "collection" else HTTPStatus.OK,
                        response,
                    )
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "Trace diagnostic source not found"},
                    )
                except (TypeError, ValueError):
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "Invalid Trace diagnostic report request"},
                    )
                return
            if path == "/api/observability/eval-schedules":
                try:
                    self._write_json(
                        HTTPStatus.CREATED,
                        self.service.agent.create_eval_schedule(payload),
                    )
                except (TypeError, ValueError):
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "schemaVersion": "rag-ime.eval-schedule-error.v1",
                            "ok": False,
                            "errorCode": "invalid_request",
                            "error": "Invalid Eval schedule request",
                        },
                    )
                except Exception:
                    self._write_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {
                            "schemaVersion": "rag-ime.eval-schedule-error.v1",
                            "ok": False,
                            "errorCode": "schedule_unavailable",
                            "error": "Eval schedule is unavailable",
                        },
                    )
                return
            if path == "/api/observability/evals/evidence-ground-truth":
                try:
                    self._write_json(
                        HTTPStatus.OK,
                        self.service.agent.evaluate_observation_evidence(payload),
                    )
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "Trace not found"},
                    )
                except (TraceContractError, ValueError):
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "Invalid Eval request"},
                    )
                return
            if path == "/api/observability/evals/ai-judge":
                try:
                    self._write_json(
                        HTTPStatus.OK,
                        self.service.agent.evaluate_observation_ai_judge(payload),
                    )
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "Trace not found"},
                    )
                except (TraceContractError, ValueError):
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "Invalid AI Judge request"},
                    )
                return
            trace_repair_receipt_id, trace_repair_action = observability_trace_repair_route(
                path
            )
            if trace_repair_action in {"change", "test", "create", "recheck"}:
                if not self._trace_repair_loopback_allowed():
                    self._write_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "schemaVersion": "rag-ime.trace-repair-error.v1",
                            "ok": False,
                            "errorCode": "trace_repair_loopback_only",
                            "error": "Trace repair is available only from the local machine",
                        },
                    )
                    return
                try:
                    if trace_repair_action == "change":
                        response = self.service.agent.record_trace_repair_change_evidence(
                            payload
                        )
                        status = HTTPStatus.CREATED
                    elif trace_repair_action == "test":
                        response = self.service.agent.record_trace_repair_test_evidence(
                            payload
                        )
                        status = HTTPStatus.CREATED
                    elif trace_repair_action == "create":
                        response = self.service.agent.create_trace_repair_receipt(payload)
                        status = HTTPStatus.CREATED
                    else:
                        response = self.service.agent.recheck_trace_repair(payload)
                        status = HTTPStatus.OK
                    self._write_json(status, response)
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {
                            "schemaVersion": "rag-ime.trace-repair-error.v1",
                            "ok": False,
                            "errorCode": "repair_receipt_not_found",
                            "error": "Trace repair receipt not found",
                            **(
                                {"repairReceiptId": trace_repair_receipt_id}
                                if trace_repair_action == "recheck"
                                else {}
                            ),
                        },
                    )
                except TraceRepairConflict:
                    self._write_json(
                        HTTPStatus.CONFLICT,
                        {
                            "schemaVersion": "rag-ime.trace-repair-error.v1",
                            "ok": False,
                            "errorCode": "repair_identity_conflict",
                            "error": "Trace repair identity is already bound to different content",
                        },
                    )
                except TraceRepairValidationError:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "schemaVersion": "rag-ime.trace-repair-error.v1",
                            "ok": False,
                            "errorCode": "invalid_trace_repair_request",
                            "error": "Invalid Trace repair request",
                        },
                    )
                except (TypeError, ValueError):
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "schemaVersion": "rag-ime.trace-repair-error.v1",
                            "ok": False,
                            "errorCode": "invalid_trace_repair_request",
                            "error": "Invalid Trace repair request",
                        },
                    )
                except Exception:
                    # Do not leak provider exceptions or private workspace
                    # paths through this write endpoint.  The persisted
                    # receipt/evidence remains the only public authority.
                    self._write_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {
                            "schemaVersion": "rag-ime.trace-repair-error.v1",
                            "ok": False,
                            "errorCode": "trace_repair_unavailable",
                            "error": "Trace repair service is unavailable",
                        },
                    )
                return
            trace_replay_id, trace_replay_action = observability_trace_replay_route(
                path
            )
            if trace_replay_action in {"case-create", "verify"}:
                if not self._trace_repair_loopback_allowed():
                    self._write_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "schemaVersion": "rag-ime.trace-verification-error.v1",
                            "ok": False,
                            "errorCode": "trace_replay_loopback_only",
                            "error": "Trace replay is available only from the local machine",
                        },
                    )
                    return
                try:
                    response = (
                        self.service.agent.create_trace_replay_case(payload)
                        if trace_replay_action == "case-create"
                        else self.service.agent.verify_trace_replay_case(payload)
                    )
                    self._write_json(HTTPStatus.CREATED, response)
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {
                            "schemaVersion": "rag-ime.trace-verification-error.v1",
                            "ok": False,
                            "errorCode": "trace_replay_record_not_found",
                            "error": "Trace replay authority record not found",
                        },
                    )
                except TraceVerificationConflict:
                    self._write_json(
                        HTTPStatus.CONFLICT,
                        {
                            "schemaVersion": "rag-ime.trace-verification-error.v1",
                            "ok": False,
                            "errorCode": "trace_replay_identity_conflict",
                            "error": "Trace replay identity is already bound to different content",
                        },
                    )
                except TraceVerificationValidationError:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "schemaVersion": "rag-ime.trace-verification-error.v1",
                            "ok": False,
                            "errorCode": "invalid_trace_replay_request",
                            "error": "Invalid Trace replay request",
                        },
                    )
                return
            # Migrated families are served from the route table. This sits
            # after the security gate and payload read so those semantics are
            # identical to the chain it replaces.
            descriptor_route = find_route("POST", path)
            if descriptor_route is not None:
                if descriptor_route.handler in {"agent.eval_lab_trial_start", "agent.eval_lab_trial_cancel"}:
                    try:
                        self._dispatch_descriptor_route(descriptor_route, payload=payload)
                    except Exception as exc:
                        self._write_json(*lab_trial_error_response(exc))
                else:
                    self._dispatch_descriptor_route(descriptor_route, payload=payload)
                return
            if knowledge_parts is not None:
                control = self._knowledge_control()
                if knowledge_parts == ():
                    response = control.create_base(payload)
                    status = HTTPStatus.CREATED
                elif knowledge_parts == ("embedding-probe",):
                    response = control.embedding_probe(payload)
                    status = HTTPStatus.OK
                elif knowledge_parts == ("embedding-impact",):
                    response = control.embedding_impact(payload)
                    status = HTTPStatus.OK
                elif len(knowledge_parts) == 3 and knowledge_parts[1:] == ("delete", "preview"):
                    response = control.delete_preview(knowledge_parts[0], payload)
                    status = HTTPStatus.OK
                elif len(knowledge_parts) == 3 and knowledge_parts[1:] == ("delete", "apply"):
                    response = control.delete_apply(knowledge_parts[0], payload)
                    status = HTTPStatus.OK
                elif len(knowledge_parts) == 4 and knowledge_parts[1] == "documents" and knowledge_parts[3] == "retry":
                    response = control.retry_document(knowledge_parts[0], knowledge_parts[2], payload)
                    status = HTTPStatus.OK
                elif len(knowledge_parts) == 4 and knowledge_parts[1] == "jobs" and knowledge_parts[3] == "cancel":
                    response = control.cancel_job(knowledge_parts[0], knowledge_parts[2])
                    status = HTTPStatus.OK
                elif len(knowledge_parts) == 4 and knowledge_parts[1] == "documents" and knowledge_parts[3] == "chunk-preview":
                    response = control.preview_chunking(knowledge_parts[0], knowledge_parts[2], payload)
                    status = HTTPStatus.OK
                elif len(knowledge_parts) == 2 and knowledge_parts[1] == "search":
                    response = control.search(knowledge_parts[0], payload)
                    status = HTTPStatus.OK
                elif len(knowledge_parts) == 2 and knowledge_parts[1] == "rebuild":
                    response = control.rebuild(knowledge_parts[0], payload)
                    status = HTTPStatus.OK
                elif len(knowledge_parts) == 3 and knowledge_parts[1:] == ("graph", "rebuild"):
                    response = control.rebuild_graph(knowledge_parts[0], payload)
                    status = HTTPStatus.OK
                elif len(knowledge_parts) == 4 and knowledge_parts[1] == "documents" and knowledge_parts[3] == "find":
                    response = control.find(knowledge_parts[0], knowledge_parts[2], payload)
                    status = HTTPStatus.OK
                else:
                    self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown endpoint"})
                    return
                self._write_json(status, response)
                return
            if path == "/api/browser/command":
                action = str(payload.pop("action", ""))
                self._write_json(
                    HTTPStatus.OK,
                    self.service.browser_control.submit_command(
                        action,
                        payload,
                        session_id="control-center",
                        timeout_seconds=float(payload.pop("timeoutSeconds", 20.0)),
                    ),
                )
                return
            agent_session_id, agent_action = agent_session_route(path)
            context_session_id, context_item_id, context_item_action = agent_context_item_route(path)
            agent_room_id, room_action = agent_room_route(path)
            background_job_session_id, background_job_id, background_job_action = (
                agent_background_job_route(path)
            )
            (
                room_work_room_id,
                room_work_item_id,
                room_work_action,
            ) = agent_room_work_route(path)
            subagent_run_id, subagent_action = agent_subagent_route(path)
            wake_schedule_id, wake_schedule_action = agent_wake_schedule_route(path)
            approval_id, approval_action = agent_approval_route(path)
            work_document_id, work_document_action = agent_work_document_route(path)
            if path == "/api/agent/runtime/ensure":
                self._write_json(HTTPStatus.OK, self.service.agent.ensure_runtime(payload))
            elif agent_session_id and agent_action in {"knowledge-search", "knowledge-read"}:
                response = (
                    self.service.agent.room_knowledge_search(
                        payload, authenticated_session_id=agent_session_id
                    )
                    if agent_action == "knowledge-search"
                    else self.service.agent.room_knowledge_read(
                        payload, authenticated_session_id=agent_session_id
                    )
                )
                self._write_json(HTTPStatus.OK, response)
            elif path == "/api/agent/collaboration-profiles/commands":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.apply_collaboration_profile_command(
                        payload, caller_authorized=True
                    ),
                )
            elif path == "/api/agent/providers/auth/preview":
                self._write_json(HTTPStatus.OK, self.service.pi_provider_auth.preview(payload))
            elif path == "/api/agent/providers/auth/apply":
                self._write_json(HTTPStatus.OK, self.service.pi_provider_auth.apply(payload))
            elif path == "/api/agent/providers/oauth/cancel":
                self._write_json(HTTPStatus.OK, self.service.pi_provider_auth.oauth_cancel(payload))
            elif path == "/api/agent/configuration":
                self._write_json(HTTPStatus.OK, self.service.agent.update_configuration(payload))
            elif path == "/api/agent/eval-lab/golden/command":
                try:
                    response = self.service.agent.eval_lab_golden_command(payload)
                except Exception as exc:
                    self._write_json(*lab_golden_error_response(exc))
                else:
                    self._write_json(HTTPStatus.OK, response)
            elif path == "/api/agent/eval-lab/projects/command":
                try:
                    response = self.service.agent.eval_lab_project_command(payload)
                except Exception as exc:
                    self._write_json(*lab_project_error_response(exc))
                else:
                    self._write_json(HTTPStatus.OK, response)
            elif path == "/api/agent/eval-lab/apps/command":
                try:
                    response = self.service.agent.eval_lab_app_command(payload)
                except Exception as exc:
                    self._write_json(*lab_project_error_response(exc))
                else:
                    self._write_json(HTTPStatus.OK, response)
            elif path in ("/api/agent/eval-lab/scene-recipes/apply", "/api/agent/eval-lab/scene-recipes/rollback"):
                try:
                    response = (
                        self.service.agent.eval_lab_scene_recipe_apply(payload)
                        if path.endswith("/apply")
                        else self.service.agent.eval_lab_scene_recipe_rollback(payload)
                    )
                except Exception as exc:
                    self._write_json(*lab_scene_recipe_error_response(exc))
                else:
                    self._write_json(HTTPStatus.OK, response)
            elif path == "/api/agent/extensions/drafts":
                self._write_json(
                    HTTPStatus.CREATED,
                    self.service.agent_extensions.create_package_draft(payload),
                )
            elif path == "/api/agent/extensions/validate":
                self._write_json(HTTPStatus.OK, self.service.agent_extensions.validate(payload))
            elif path == "/api/agent/extensions/preview":
                self._write_json(HTTPStatus.OK, self.service.agent_extensions.preview(payload))
            elif path == "/api/agent/extensions/apply":
                self._write_json(HTTPStatus.OK, self.service.agent_extensions.apply(payload))
            elif path == "/api/agent/deep-search":
                self._write_json(HTTPStatus.ACCEPTED, self.service.agent.deep_search(payload))
            elif path == "/api/agent/surface/complete":
                self._write_json(HTTPStatus.OK, self.service.agent_surface_complete(payload))
            elif path == "/api/agent/surface/refine-voice":
                self._write_json(HTTPStatus.OK, self.service.agent_surface_refine_voice(payload))
            elif path == "/api/agent/surface/cancel":
                self._write_json(HTTPStatus.OK, self.service.agent_surface_cancel(payload))
            elif path == "/api/agent/memory-maintenance":
                self._write_json(
                    HTTPStatus.ACCEPTED,
                    self.service.agent_memory_maintenance_trigger(payload),
                )
            elif path == "/api/memory/lifecycle/refresh":
                self._write_json(HTTPStatus.ACCEPTED, self.service.memory_lifecycle_refresh(payload))
            elif path == "/api/memory/lifecycle/forget/preview":
                self._write_json(HTTPStatus.OK, self.service.memory_lifecycle_forget_preview(payload))
            elif path == "/api/memory/lifecycle/forget/apply":
                self._write_json(HTTPStatus.OK, self.service.memory_lifecycle_forget_apply(payload))
            elif work_document_id:
                handlers = {
                    "archive": self.service.agent.work_documents.request_archive,
                    "repair": lambda identifier, _payload: self.service.agent.work_documents.repair(
                        identifier
                    ),
                    "reopen": self.service.agent.work_documents.reopen,
                    "erase-preview": self.service.agent.work_documents.erase_preview,
                    "erase": self.service.agent.work_documents.erase,
                }
                handler = handlers.get(work_document_action)
                if handler is None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown endpoint"}
                    )
                else:
                    self._write_json(
                        HTTPStatus.OK, handler(work_document_id, payload)
                    )
            elif path == "/api/agent/sessions/surface/ensure":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.ensure_surface_session(payload),
                )
            elif path == "/api/agent/sessions":
                self._write_json(HTTPStatus.CREATED, self.service.agent.create_session(payload))
            elif context_session_id and context_item_id and context_item_action == "ack":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.acknowledge_context_item(
                        context_session_id,
                        context_item_id,
                    ),
                )
            elif (
                background_job_session_id
                and background_job_id
                and background_job_action == "cancel"
            ):
                room_turn_id = str(payload.get("roomTurnId") or "").strip()
                self._write_json(
                    HTTPStatus.OK,
                    (
                        self.service.agent.background_jobs.cancel_room_owned(
                            background_job_session_id,
                            background_job_id,
                            room_turn_id=room_turn_id,
                            reason=payload.get("reason") or "control_center_requested",
                        )
                        if room_turn_id
                        else self.service.agent.background_jobs.cancel(
                            background_job_session_id,
                            background_job_id,
                            reason=payload.get("reason") or "control_center_requested",
                        )
                    ),
                )
            elif path == "/api/agent/wake-schedules":
                self._write_json(
                    HTTPStatus.CREATED,
                    self.service.agent.create_wake_schedule(payload),
                )
            elif wake_schedule_id and wake_schedule_action == "action":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.wake_schedule_action(wake_schedule_id, payload),
                )
            elif path == "/api/agent/roles":
                self._write_json(HTTPStatus.CREATED, self.service.agent.create_role(payload))
            elif path == "/api/agent/roles/runtime-defaults":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.update_role_runtime_defaults(payload),
                )
            elif path == "/api/agent/role-book/activation/preview":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent_role_book_control.activation_preview(payload),
                )
            elif path == "/api/agent/role-book/activation/apply":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent_role_book_control.activation_apply(payload),
                )
            elif path == "/api/agent/role-book/activation/rollback":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent_role_book_control.activation_rollback(payload),
                )
            elif path == "/api/agent/role-book/drafts/decision":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent_role_book_control.decide_daily_draft(payload),
                )
            elif path == "/api/memory/activity-timeline/build":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.activity_timeline_build(payload),
                )
            elif path == "/api/memory/activity-timeline/approve":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.activity_timeline_approve(payload),
                )
            elif path == "/api/memory/activity-timeline/reject":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.activity_timeline_reject(payload),
                )
            elif room_work_room_id and room_work_action == "collection":
                self._write_json(
                    HTTPStatus.CREATED,
                    self.service.agent.create_room_work_item(
                        room_work_room_id,
                        payload,
                    ),
                )
            elif (
                room_work_room_id
                and room_work_item_id
                and room_work_action == "reassign"
            ):
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.reassign_room_work_item(
                        room_work_room_id,
                        room_work_item_id,
                        payload,
                    ),
                )
            elif path == "/api/agent/rooms":
                self._write_json(HTTPStatus.CREATED, self.service.agent.create_room(payload))
            elif path == "/api/agent/subagents/runs":
                session_id = str(payload.pop("sessionId", ""))
                self._write_json(
                    HTTPStatus.ACCEPTED,
                    self.service.agent.delegate_tasks(session_id, payload),
                )
            elif subagent_run_id and subagent_action == "abort":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.abort_delegation(
                        str(payload.get("sessionId") or ""),
                        {"runId": subagent_run_id},
                    ),
                )
            elif subagent_run_id and subagent_action == "control":
                session_id = str(payload.pop("sessionId", ""))
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.control_delegation(
                        session_id,
                        subagent_run_id,
                        payload,
                    ),
                )
            elif agent_room_id and room_action == "messages":
                self.service.require_agent_runtime_execution_owner()
                self._write_json(
                    HTTPStatus.ACCEPTED,
                    self.service.agent.post_room_message(agent_room_id, payload),
                )
            elif agent_room_id and room_action == "start-gate":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.confirm_room_start(agent_room_id, payload),
                )
            elif agent_room_id and room_action == "steer":
                self.service.require_agent_runtime_execution_owner()
                self._write_json(
                    HTTPStatus.ACCEPTED,
                    self.service.agent.steer_room_participant(agent_room_id, payload),
                )
            elif agent_room_id and room_action == "abort":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.abort_room_turn(agent_room_id, payload),
                )
            elif agent_room_id and room_action == "participants":
                self._write_json(
                    HTTPStatus.CREATED,
                    self.service.agent.add_room_participant(agent_room_id, payload),
                )
            elif agent_room_id and room_action == "topics":
                self._write_json(
                    HTTPStatus.CREATED,
                    self.service.agent.create_room_topic(agent_room_id, payload),
                )
            elif agent_room_id and room_action == "artifacts":
                self._write_json(
                    HTTPStatus.CREATED,
                    self.service.agent.add_room_artifact(agent_room_id, payload),
                )
            elif agent_session_id and agent_action == "workspace-file":
                self._write_json(HTTPStatus.OK, self.service.agent_tools.workspace_save(agent_session_id, payload))
            elif agent_session_id and agent_action == "prompt":
                self._write_json(HTTPStatus.ACCEPTED, self.service.agent.prompt(agent_session_id, payload))
            elif agent_session_id and agent_action == "rewrite":
                self._write_json(
                    HTTPStatus.ACCEPTED,
                    self.service.agent.rewrite_session(agent_session_id, payload),
                )
            elif agent_session_id and agent_action == "forks":
                self._write_json(
                    HTTPStatus.CREATED,
                    self.service.agent.fork_session(agent_session_id, payload),
                )
            elif agent_session_id and agent_action == "abort":
                self._write_json(HTTPStatus.OK, self.service.agent.abort(agent_session_id))
            elif agent_session_id and agent_action == "review":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.resolve_review(agent_session_id, payload),
                )
            elif agent_session_id and agent_action == "ui-response":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.resolve_ui_request(agent_session_id, payload),
                )
            elif agent_session_id and agent_action == "compact":
                self._write_json(HTTPStatus.OK, self.service.agent.compact(agent_session_id, payload))
            elif agent_session_id and agent_action == "commands":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.invoke_command(agent_session_id, payload),
                )
            elif agent_session_id and agent_action == "goal":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.mutate_goal(agent_session_id, payload),
                )
            elif agent_session_id and agent_action == "model":
                self._write_json(HTTPStatus.OK, self.service.agent.select_model(agent_session_id, payload))
            elif agent_session_id and agent_action == "thinking":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.select_thinking_level(agent_session_id, payload),
                )
            elif agent_session_id and agent_action == "intercom":
                self._write_json(
                    HTTPStatus.ACCEPTED,
                    self.service.agent.send_room_intercom(agent_session_id, payload),
                )
            elif approval_id and approval_action == "decision":
                self._write_json(HTTPStatus.OK, self.service.agent.decide_approval(approval_id, payload))
            elif approval_id and approval_action == "external-result":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.finalize_external_approval(approval_id, payload),
                )
            elif path in ("/api/rime-suggest", "/rime-suggest"):
                validate_contract(payload, "rime-suggest-request.v1.json")
                response = self.service.rime_suggest(payload)
                validate_contract(response, "rime-suggest-response.v1.json")
                validate_contract(response.get("assistantOverlay"), "assistant-overlay.v1.json")
                if isinstance(response.get("overlayConfig"), dict):
                    validate_contract(response.get("overlayConfig"), "overlay-config.v1.json")
                self._write_json(HTTPStatus.OK, response)
            elif path.startswith("/api/memory/cleanup-diff/") and path.endswith("/apply"):
                diff_id = _cleanup_diff_path_id(path, suffix="/apply")
                self._write_json(HTTPStatus.OK, self.service.memory_cleanup_diff_apply(diff_id))
            elif path.startswith("/api/memory/cleanup-diff/") and path.endswith("/rollback"):
                diff_id = _cleanup_diff_path_id(path, suffix="/rollback")
                self._write_json(HTTPStatus.OK, self.service.memory_cleanup_diff_rollback(diff_id))
            else:
                self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown endpoint"})
        except Exception as exc:  # pragma: no cover - exercised through browser/manual debugging
            if isinstance(exc, AgentRuntimeError):
                self._write_json(
                    HTTPStatus.CONFLICT,
                    _agent_session_runtime_error_payload(exc),
                )
                return
            status = getattr(exc, "http_status", HTTPStatus.BAD_REQUEST)
            error_payload: dict[str, object] = {
                "ok": False,
                "error": str(exc),
            }
            projection = getattr(exc, "response_payload", None)
            if callable(projection):
                projected = projection()
                if isinstance(projected, dict):
                    error_payload.update(projected)
            self._write_json(
                HTTPStatus(int(status)),
                error_payload,
            )

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib API
        parsed = urlparse(self.path)
        if not self._authorize_gateway_request("PATCH", parsed):
            return
        try:
            path = parsed.path
            security_error = self._management_post_security_error(path)
            if security_error is not None:
                self._write_json(HTTPStatus.FORBIDDEN, security_error)
                return
            knowledge_parts = _knowledge_route_parts(path)
            if path == "/api/agent/lifecycle-hooks":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent_lifecycle_hooks.update_policy(self._read_json()),
                )
                return
            if path == "/api/agent/roles":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.update_role(self._read_json()),
                )
                return
            if knowledge_parts is not None:
                if len(knowledge_parts) != 1:
                    self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown endpoint"})
                    return
                self._write_json(
                    HTTPStatus.OK,
                    self._knowledge_control().update_base(knowledge_parts[0], self._read_json()),
                )
                return
            session_id, action = agent_session_route(path)
            room_id, room_action = agent_room_route(path)
            if room_id and not room_action:
                self._write_json(HTTPStatus.OK, self.service.agent.update_room(room_id, self._read_json()))
                return
            if room_id and room_action == "participants":
                payload = self._read_json()
                if "collaborationRole" in payload:
                    result = (
                        self.service.agent.update_room_participant_role(
                            room_id,
                            payload,
                        )
                    )
                else:
                    result = self.service.agent.remove_room_participant(
                        room_id,
                        payload,
                    )
                self._write_json(
                    HTTPStatus.OK,
                    result,
                )
                return
            if room_id and room_action == "topics":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.update_room_topic(room_id, self._read_json()),
                )
                return
            if room_id and room_action == "artifacts":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.update_room_artifact(room_id, self._read_json()),
                )
                return
            if not session_id or action:
                self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown endpoint"})
                return
            self._write_json(HTTPStatus.OK, self.service.agent.update_session(session_id, self._read_json()))
        except Exception as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib API
        parsed = urlparse(self.path)
        if not self._authorize_gateway_request("DELETE", parsed):
            return
        try:
            path = parsed.path
            room_id, room_action = agent_room_route(path)
            security_error = self._management_post_security_error(
                path,
                require_json=bool(room_id and not room_action),
            )
            if security_error is not None:
                self._write_json(HTTPStatus.FORBIDDEN, security_error)
                return
            knowledge_parts = _knowledge_route_parts(path)
            if knowledge_parts is not None:
                if len(knowledge_parts) != 3 or knowledge_parts[1] != "documents":
                    self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown endpoint"})
                    return
                self._write_json(
                    HTTPStatus.OK,
                    self._knowledge_control().delete_document(knowledge_parts[0], knowledge_parts[2]),
                )
                return
            if room_id and not room_action:
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.delete_room(room_id, self._read_json()),
                )
                return
            if path == "/api/agent/roles":
                self._write_json(
                    HTTPStatus.OK,
                    self.service.agent.archive_role(self._read_json()),
                )
                return
            session_id, action = agent_session_route(path)
            if not session_id or action:
                self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown endpoint"})
                return
            self._write_json(HTTPStatus.OK, self.service.agent.delete_session(session_id))
        except Exception as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib API
        parsed = urlparse(self.path)
        if not self._authorize_gateway_request("GET", parsed):
            return
        if self._serve_isolated_html_preview(parsed.path, include_body=False):
            return
        if self._serve_gateway_static(parsed.path, include_body=False):
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: object) -> None:
        if self.path.startswith(("/api/active-rag/status", "/api/active-rag/session", "/api/knowledge/status", "/api/knowledge/session")):
            return
        print(f"[rag-ime-debug] {self.address_string()} - {fmt % args}")

    def _authorize_gateway_request(self, method: str, parsed: Any) -> bool:
        try:
            decision = resolve_gateway_access(
                server_name=self._server_name(),
                host_header=self.headers.get("Host", ""),
                headers=self.headers,
                allowed_logins=os.environ.get("RAG_IME_REMOTE_ALLOWED_LOGINS", ""),
            )
            self._gateway_access = decision
            if not decision.is_remote:
                return True
            if not parsed.path.startswith("/api/"):
                if method == "GET" and self._gateway_static_file(parsed.path) is not None:
                    return True
                raise ControlApiError(
                    code=ControlErrorCode.ROUTE_NOT_FOUND,
                    message="remote Agent Gateway path is not allowlisted",
                    status=404,
                )

            body: Mapping[str, object] = {}
            if method in {"POST", "PATCH"}:
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise ControlApiError(
                        code=ControlErrorCode.INVALID_REQUEST,
                        message="remote control writes require application/json",
                    )
                body = self._read_json()
            default_route_policy().authorize_http(
                method=method,
                path=parsed.path,
                query=self._control_query(parsed.query),
                body=body,
                context=decision.context,
                request_id=self.headers.get("X-Request-ID", "http-request")[:128],
            )
            return True
        except ControlApiError as exc:
            self._write_json(
                HTTPStatus(exc.status),
                {
                    "schemaVersion": "rag-ime.gateway-access.v1",
                    "ok": False,
                    "error": exc.message,
                    "errorCode": exc.code.value,
                    "retryable": exc.retryable,
                },
            )
            return False
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "schemaVersion": "rag-ime.gateway-access.v1",
                    "ok": False,
                    "error": str(exc),
                    "errorCode": "invalid_request",
                    "retryable": False,
                },
            )
            return False

    def _trace_repair_loopback_allowed(self) -> bool:
        """Keep repair evidence and receipts local even on a debug bind-all."""

        address = getattr(self, "client_address", None)
        # Unit handlers and direct in-process callers have no socket peer; the
        # normal server always has one. Treating the former as local preserves
        # the service-level API without weakening a real network request.
        if not isinstance(address, tuple) or not address:
            return True
        host = str(address[0] or "").strip().strip("[]").lower()
        if not host:
            return True
        if host in {"localhost", "localhost.localdomain"}:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _request_access_context(self) -> ControlAccessContext:
        decision = getattr(self, "_gateway_access", None)
        if isinstance(decision, GatewayAccessDecision):
            return decision.context
        return ControlAccessContext.loopback_web()

    def _server_name(self) -> str:
        config = getattr(self.service, "config", None)
        return str(getattr(config, "server_name", "debug server"))

    @staticmethod
    def _control_query(raw_query: str) -> dict[str, object]:
        parsed = parse_qs(raw_query or "", keep_blank_values=True)
        duplicates = sorted(key for key, values in parsed.items() if len(values) != 1)
        if duplicates:
            raise ValueError(f"control query fields must not repeat: {', '.join(duplicates)}")
        return {key: values[0] for key, values in parsed.items()}

    def _gateway_static_file(self, request_path: str) -> Path | None:
        if self._server_name() != "agent gateway":
            return None
        root = getattr(self, "static_dir", Path(".")).expanduser().resolve(strict=False)
        relative = "index.html" if request_path in {"", "/"} else unquote(request_path).lstrip("/")
        if not relative or "\x00" in relative or "\\" in relative:
            return None
        candidate = (root / relative).resolve(strict=False)
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        return candidate

    def _serve_gateway_static(self, request_path: str, *, include_body: bool = True) -> bool:
        candidate = self._gateway_static_file(request_path)
        if candidate is None:
            return False
        body = candidate.read_bytes()
        mime_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        cache_control = (
            "public, max-age=31536000, immutable"
            if candidate.parent.name == "assets"
            else "no-store"
            if candidate.name == "index.html"
            else "public, max-age=3600"
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime_type}; charset=utf-8" if mime_type.startswith("text/") else mime_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self'; media-src 'self' blob:; "
            "worker-src 'self' blob:; connect-src 'self'; object-src 'none'; "
            # Portable Apps may declare a separate HTTPS or loopback workspace.
            # The App host validates that destination and keeps its message
            # bridge bound to the separate, opaque-origin conversation frame.
            "frame-src 'self' blob: https: http://127.0.0.1:* http://localhost:* http://[::1]:*; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if include_body:
            self.wfile.write(body)
        return True

    def _serve_isolated_html_preview(
        self,
        request_path: str,
        *,
        include_body: bool = True,
    ) -> bool:
        if request_path != _ISOLATED_HTML_PREVIEW_PATH:
            return False
        body = _ISOLATED_HTML_PREVIEW_DOCUMENT
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline' https: http: blob: data:; "
            "style-src 'unsafe-inline' https: http:; img-src data: blob: https: http:; "
            "font-src data: blob: https: http:; media-src data: blob: https: http:; "
            "connect-src https: http: ws: wss:; worker-src blob: data:; "
            "child-src blob: data: https: http:; object-src 'none'; base-uri 'none'; "
            "form-action https: http:; "
            "sandbox allow-downloads allow-forms allow-modals allow-pointer-lock "
            "allow-popups allow-scripts",
        )
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if include_body:
            self.wfile.write(body)
        return True

    def _stream_management_events(self) -> None:
        self._stream_sse(
            lambda: self.service.management.events.subscribe(),
            cache_control="no-cache",
        )

    def _stream_agent_events(self, session_id: str, *, after_event_id: str = "") -> None:
        self._stream_sse(
            lambda: self.service.agent.subscribe_events(
                session_id,
                after_event_id=after_event_id,
            ),
            cache_control="no-cache",
        )

    def _stream_agent_control_events(self, *, after_event_id: str = "") -> None:
        self._stream_sse(
            lambda: self.service.agent.subscribe_control_events(
                after_event_id=after_event_id
            ),
            cache_control="no-cache",
        )

    def _stream_agent_room_events(self, room_id: str, *, after_event_id: str = "") -> None:
        self._stream_sse(
            lambda: self.service.agent.subscribe_room_events(
                room_id,
                after_event_id=after_event_id,
            ),
            cache_control="no-cache",
        )

    def _stream_observation_events(
        self,
        *,
        after_event_id: str = "",
        filters: Mapping[str, object] | None = None,
    ) -> None:
        self._stream_sse(
            lambda: self.service.agent.subscribe_observations(
                after_event_id=after_event_id,
                filters=filters,
            ),
            cache_control="no-cache, no-store",
            nosniff=True,
        )

    def _stream_sse(
        self,
        stream_factory: Callable[[], object],
        *,
        cache_control: str,
        nosniff: bool = False,
    ) -> None:
        """Commit HTTP 200 only after the event source yields a frame.

        Subscription generators intentionally perform their SQLite cursor and
        replay validation on first iteration. Sending headers before that
        boundary turns a database failure into a misleading 200 + early EOF,
        which clients interpret as a stable connection and rapidly retry.
        """

        iterator: object | None = None
        close: object = None
        acquire_slot = getattr(self.server, "acquire_event_stream", None)
        release_slot = getattr(self.server, "release_event_stream", None)
        slot_acquired = True
        try:
            if callable(acquire_slot):
                slot_acquired = bool(acquire_slot())
            if not slot_acquired:
                self._write_event_stream_unavailable(
                    error_code="event_stream_capacity",
                    retry_after="2",
                )
                return
            try:
                iterator = iter(stream_factory())  # type: ignore[arg-type]
                close = getattr(iterator, "close", None)
                first_chunk = next(iterator)  # type: ignore[arg-type]
            except StopIteration:
                self._write_event_stream_unavailable()
                return
            except Exception:
                self._write_event_stream_unavailable()
                return
            if not isinstance(first_chunk, bytes):
                self._write_event_stream_unavailable()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", cache_control)
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            if nosniff:
                self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(first_chunk)
            self.wfile.flush()
            for chunk in iterator:  # type: ignore[union-attr]
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, sqlite3.Error):
            return
        finally:
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            if slot_acquired and callable(release_slot):
                release_slot()

    def _write_event_stream_unavailable(
        self,
        *,
        error_code: str = "event_stream_unavailable",
        retry_after: str = "1",
    ) -> None:
        self._write_json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {
                "schemaVersion": "rag-ime.event-stream-error.v1",
                "ok": False,
                "errorCode": error_code,
                "error": "Realtime event stream is temporarily unavailable",
                "recovery": {"action": "retry"},
            },
            headers={"Retry-After": retry_after},
        )

    def _management_post_security_error(
        self,
        path: str,
        *,
        require_json: bool = True,
    ) -> dict[str, object] | None:
        if not path.startswith("/api/"):
            return None
        settings = self.service.management_security_settings()
        if require_json and settings.get("postRequiresJson") is True:
            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type.lower():
                return {
                    "schemaVersion": "rag-ime.management-security.v3",
                    "ok": False,
                    "error": f"{self.command} requires application/json",
                }
        if settings.get("sameOriginOnly") is True:
            origin = self.headers.get("Origin", "")
            if origin and not _origin_matches_host(origin, self.headers.get("Host", "")):
                return {
                    "schemaVersion": "rag-ime.management-security.v3",
                    "ok": False,
                    "error": f"cross-origin {self.command} rejected",
                }
        if settings.get("requireToken") is True:
            expected = os.environ.get("RAG_IME_MANAGEMENT_TOKEN", "") or _string(settings.get("token"))
            provided = self.headers.get("X-RAG-IME-Admin-Token", "")
            if not expected or provided != expected:
                return {"schemaVersion": "rag-ime.management-security.v3", "ok": False, "error": "management token required"}
        return None

    def _knowledge_control(self) -> Any:
        control = self.service.knowledge_control
        if control is None:
            raise RuntimeError("document knowledge management is unavailable")
        return control

    def _read_knowledge_upload(self, length: int) -> bytes:
        """Read one bounded document body; deployment adapters can add deadlines."""
        return self.rfile.read(length)

    @staticmethod
    def _knowledge_error(exc: Exception) -> dict[str, object]:
        code = str(getattr(exc, "code", "invalid_request") or "invalid_request")
        return {
            "schemaVersion": "rag-ime.knowledge-library.v1",
            "ok": False,
            "error": str(exc),
            "errorCode": code,
        }

    def _read_json(self) -> dict[str, Any]:
        raw = getattr(self, "_request_body_bytes", None)
        if raw is None:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > 2_000_000:
                raise ValueError("JSON payload exceeds the 2,000,000 byte limit")
            raw = self.rfile.read(length) if length else b"{}"
            self._request_body_bytes = raw
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON payload must be an object")
        return data

    def _dispatch_descriptor_route(
        self,
        route,
        *,
        payload: dict[str, object] | None = None,
        query: dict[str, list[str]] | None = None,
    ) -> None:
        """Serve one migrated route from its descriptor.

        The descriptor owns method, path, parsing and the application handler,
        so this is the only place those are combined. Routes still living in
        the if/elif chains are untouched; `find_route` returning None means the
        chain remains the single owner for that path.
        """

        result = DescriptorRouteDispatcher(
            self.service,
            query_first=_query_first,
        ).dispatch(route, payload=payload, query=query)
        self._write_json(result.status, result.payload)

    def _write_json(
        self,
        status: HTTPStatus,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(str(name), str(value))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _write_knowledge_binary(self, blob: AssetBlob) -> None:
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", blob.media_type)
            self.send_header("Content-Length", str(blob.byte_size))
            self.send_header("ETag", f'"{blob.asset_id}"')
            self.send_header("Cache-Control", "private, max-age=31536000, immutable")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(blob.file_name, safe='')}")
            self.end_headers()
            self.wfile.write(blob.data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _write_binary(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        mime_type: str,
        etag: str,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Content-Disposition", "inline")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("ETag", f'"{etag}"')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Ignore normal client disconnects without dumping multi-line tracebacks."""

    # socketserver defaults to a backlog of 5. A single Provider response can
    # legitimately fan out several governed Tool calls at once; keep the
    # accept queue bounded but large enough that the Runtime Host's per-Session
    # concurrency limiter (currently 8) does not race the server backlog.
    request_queue_size = 64
    max_event_streams = 64

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._event_stream_slots = BoundedSemaphore(self.max_event_streams)
        self._event_stream_lock = RLock()
        self._active_event_streams = 0

    def acquire_event_stream(self) -> bool:
        acquired = self._event_stream_slots.acquire(blocking=False)
        if acquired:
            with self._event_stream_lock:
                self._active_event_streams += 1
        return acquired

    def release_event_stream(self) -> None:
        with self._event_stream_lock:
            if self._active_event_streams <= 0:
                return
            self._active_event_streams -= 1
        self._event_stream_slots.release()

    @property
    def active_event_streams(self) -> int:
        with self._event_stream_lock:
            return self._active_event_streams

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def run_debug_server(config: DebugServerConfig) -> None:
    if config.server_name == "agent gateway" and not _host_is_loopback(config.host):
        raise ValueError(
            "Agent Gateway must bind to loopback; use authenticated Tailscale Serve for remote access"
        )
    if (
        config.server_name == "agent gateway"
        and not (config.static_dir / "index.html").is_file()
    ):
        raise FileNotFoundError(
            f"Agent Gateway web build is missing: {config.static_dir / 'index.html'}"
        )
    service = DebugImeService(config)

    class Handler(DebugRequestHandler):
        pass

    Handler.service = service
    Handler.static_dir = config.static_dir
    try:
        server = QuietThreadingHTTPServer((config.host, config.port), Handler)
    except BaseException:
        service.close()
        raise
    url = f"http://{config.host}:{config.port}/api/health"
    print(f"RAG IME {config.server_name} API: {url}")
    if config.server_name == "agent gateway":
        print(f"RAG IME Agent Gateway UI: http://{config.host}:{config.port}/")
    print(f"DB: {config.db_path}")
    previous_sigterm = signal.getsignal(signal.SIGTERM) if current_thread() is main_thread() else None

    def stop_server(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    if previous_sigterm is not None:
        signal.signal(signal.SIGTERM, stop_server)
    try:
        service.start_background_services()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        server.server_close()
        service.close()


def _memory_projection_worker_enabled(config: DebugServerConfig) -> bool:
    if config.memory_projection_worker_enabled is not None:
        return bool(config.memory_projection_worker_enabled)
    server_name = compact_whitespace(config.server_name).lower()
    if server_name == "sidecar server":
        configured = compact_whitespace(
            os.environ.get("RAG_IME_MEMORY_PROJECTION_WORKER")
        ).lower()
        return (
            configured not in {"0", "false", "no", "off"}
            if configured
            else True
        )
    if server_name == "debug server":
        # A debug/management process often shares the production database with
        # Sidecar. It must use a debug-specific opt-in so a global Sidecar flag
        # cannot accidentally create a second projection owner.
        configured = compact_whitespace(
            os.environ.get("RAG_IME_DEBUG_MEMORY_PROJECTION_WORKER")
        ).lower()
        return bool(configured) and configured not in {"0", "false", "no", "off"}
    # Agent Gateway and every unknown process name are consumers, not default
    # projection owners. Tests or one-off deployments can still use the
    # explicit config override above.
    return False


def _positive_float(value: object, *, default: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _stable_debug_hash(text: str) -> str:
    compact = compact_whitespace(text)
    if not compact:
        return ""
    return "sha256:" + hashlib.sha256(compact.encode("utf-8")).hexdigest()[:16]


def _safe_debug_error(error: BaseException) -> str:
    value = compact_whitespace(str(error))[:240]
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{6,}\b", "[REDACTED_SECRET]", value)
    value = re.sub(r"(?:/Users/|/Volumes/|/var/folders/)[^\s，。；;]+", "[REDACTED_PATH]", value)
    return value or type(error).__name__


_PINYIN_PAIR_ENV_NAMES = {
    "zZh": "RAG_IME_PINYIN_FUZZY_Z_ZH",
    "cCh": "RAG_IME_PINYIN_FUZZY_C_CH",
    "sSh": "RAG_IME_PINYIN_FUZZY_S_SH",
    "enEng": "RAG_IME_PINYIN_FUZZY_EN_ENG",
    "inIng": "RAG_IME_PINYIN_FUZZY_IN_ING",
    "ongOn": "RAG_IME_PINYIN_FUZZY_ONG_ON",
    "nL": "RAG_IME_PINYIN_FUZZY_N_L",
    "fH": "RAG_IME_PINYIN_FUZZY_F_H",
}
_PINYIN_PAIR_DEFAULTS = {
    "zZh": True,
    "cCh": True,
    "sSh": True,
    "enEng": True,
    "inIng": True,
    "ongOn": True,
    "nL": False,
    "fH": False,
}


def _apply_pinyin_settings_to_process_env(settings: dict[str, object]) -> None:
    pinyin = settings.get("pinyin") if isinstance(settings.get("pinyin"), dict) else {}
    assert isinstance(pinyin, dict)
    profile = compact_whitespace(str(pinyin.get("fuzzyProfile") or "sichuan-mild")).lower() or "sichuan-mild"
    rerank_uses_fuzzy = pinyin.get("rerankUsesFuzzy") is not False
    if profile in {"none", "off", "disabled"} or not rerank_uses_fuzzy:
        os.environ["RAG_IME_PINYIN_FUZZY_ENABLED"] = "0"
        os.environ["RAG_IME_PINYIN_FUZZY_PROFILE"] = "none"
    else:
        os.environ["RAG_IME_PINYIN_FUZZY_ENABLED"] = "1"
        os.environ["RAG_IME_PINYIN_FUZZY_PROFILE"] = profile
    pairs = pinyin.get("pairs") if isinstance(pinyin.get("pairs"), dict) else {}
    assert isinstance(pairs, dict)
    for key, env_name in _PINYIN_PAIR_ENV_NAMES.items():
        if key in pairs:
            os.environ[env_name] = "1" if pairs.get(key) is not False else "0"


def _pinyin_runtime_status(settings: dict[str, object]) -> dict[str, object]:
    pinyin = settings.get("pinyin") if isinstance(settings.get("pinyin"), dict) else {}
    assert isinstance(pinyin, dict)
    profile = compact_whitespace(str(pinyin.get("fuzzyProfile") or "sichuan-mild")).lower() or "sichuan-mild"
    rerank_uses_fuzzy = pinyin.get("rerankUsesFuzzy") is not False
    fuzzy_enabled = profile not in {"none", "off", "disabled"} and rerank_uses_fuzzy
    pairs = pinyin.get("pairs") if isinstance(pinyin.get("pairs"), dict) else {}
    assert isinstance(pairs, dict)
    return {
        "schemaVersion": "rag-ime.pinyin-runtime.v1",
        "fuzzyEnabled": fuzzy_enabled,
        "profile": profile if fuzzy_enabled else "none",
        "rerankUsesFuzzy": rerank_uses_fuzzy,
        "pairs": {
            key: bool(pairs.get(key, _PINYIN_PAIR_DEFAULTS.get(key, False)))
            for key in _PINYIN_PAIR_ENV_NAMES
        },
    }


_ACTIVE_RAG_RUNTIME_SYNC_KEYS = {
    "activeRag.enabled",
    "activeRag.shortcut",
    "activeRag.capture.accessibility",
    "activeRag.capture.clipboardFallback",
}
_ACTIVE_RAG_DEFAULTS_KEYS = {
    "RagImeActiveRagEnabled": "-bool",
    "RagImeActiveRagShortcut": "-string",
    "RagImeActiveRagCaptureAccessibility": "-bool",
    "RagImeActiveRagCaptureClipboardFallback": "-bool",
}


def _active_rag_runtime_sync_payload(*, active_settings: object, changed_keys: tuple[str, ...]) -> dict[str, object]:
    settings = dict(active_settings) if isinstance(active_settings, dict) else {}
    capture = settings.get("capture") if isinstance(settings.get("capture"), dict) else {}
    shortcut = compact_whitespace(str(settings.get("shortcut") or "ctrl+.")).lower().replace(" ", "")
    domains = ["im.rime.inputmethod.Squirrel"]
    defaults = {
        "RagImeActiveRagEnabled": {"type": "bool", "value": bool(settings.get("enabled", True))},
        "RagImeActiveRagShortcut": {"type": "string", "value": shortcut},
        "RagImeActiveRagCaptureAccessibility": {"type": "bool", "value": bool(capture.get("accessibility", True))},
        "RagImeActiveRagCaptureClipboardFallback": {"type": "bool", "value": bool(capture.get("clipboardFallback", True))},
    }
    commands: list[list[str]] = []
    for domain in domains:
        for key, spec in defaults.items():
            value = spec["value"]
            if spec["type"] == "bool":
                commands.append(["defaults", "write", domain, key, "-bool", "true" if value else "false"])
            else:
                commands.append(["defaults", "write", domain, key, "-string", str(value)])
    return {
        "schemaVersion": "rag-ime.active-rag-runtime-sync.v1",
        "changed": bool(changed_keys),
        "changedKeys": list(changed_keys),
        "shortcut": shortcut,
        "userDefaultsDomains": domains,
        "userDefaults": defaults,
        "commands": commands,
        "restartHint": "Restart or reload Squirrel/RAG-IME if the running input method keeps an old UserDefaults cache.",
    }


def _active_rag_defaults_command_allowed(command: list[str]) -> bool:
    if len(command) != 6:
        return False
    executable, action, domain, key, value_type, value = command
    if executable != "defaults" or action != "write" or domain != "im.rime.inputmethod.Squirrel":
        return False
    if _ACTIVE_RAG_DEFAULTS_KEYS.get(key) != value_type:
        return False
    if value_type == "-bool" and value not in {"true", "false"}:
        return False
    if value_type == "-string" and not (1 <= len(value) <= 80):
        return False
    return True


def _active_rag_secure_flags(payload: dict[str, Any]) -> tuple[bool, bool]:
    foreground = payload.get("foregroundText") if isinstance(payload.get("foregroundText"), dict) else {}
    sensitive_field = _bool(
        payload.get("sensitiveField")
        or payload.get("isSensitiveField")
        or foreground.get("sensitiveField")
        or foreground.get("isSensitiveField"),
        default=False,
    )
    secure_input = _bool(
        payload.get("secureInput")
        or payload.get("isSecureInput")
        or foreground.get("secureInput")
        or foreground.get("isSecureInput"),
        default=False,
    )
    return sensitive_field, secure_input


def _sensitive_deepseek_preview_payload() -> dict[str, object]:
    empty_text = {"present": False, "chars": 0, "utf8Bytes": 0, "hash": ""}
    diagnostics = {
        "schemaVersion": "rag-ime.context-injection-trace.v1",
        "privacy": {
            "rawTextIncluded": False,
            "hashAlgorithm": "none_for_sensitive_fields",
            "sensitiveFieldBlocked": True,
        },
        "capturedContext": {
            "currentContext": dict(empty_text),
            "selectedText": dict(empty_text),
            "surroundingBefore": dict(empty_text),
            "surroundingAfter": dict(empty_text),
        },
        "evidence": {"count": 0, "sourceCounts": {}, "sourceLaneCounts": {}, "items": []},
        "contextPacket": {"present": False, "packetIdHash": "", "sectionCounts": {}},
        "prompt": {"messageCount": 0, "messages": []},
        "injection": {
            "promptBuilt": False,
            "currentContextIncluded": False,
            "selectedTextIncluded": False,
            "contextPacketIncluded": False,
            "evidenceIncluded": False,
            "success": False,
            "missing": [SENSITIVE_FIELD_BLOCK_REASON],
        },
    }
    return {
        "schemaVersion": "rag-ime.deepseek-completion-preview.v1",
        "ok": False,
        "dryRun": True,
        "error": SENSITIVE_FIELD_BLOCK_REASON,
        "routeStatus": {
            "schemaVersion": "rag-ime.active-rag-route-status.v1",
            "route": "explicit_active_rag_deepseek",
            "remoteReady": False,
            "skipReason": SENSITIVE_FIELD_BLOCK_REASON,
            "gates": {"sensitiveFieldClear": False},
            "passivePostCommitRemoteAllowed": False,
        },
        "requestDiagnostics": diagnostics,
        "retrieval": {"called": False, "evidenceCount": 0, "lanes": {}, "elapsedMs": 0.0},
        "remoteModel": {
            "requested": False,
            "allowed": False,
            "provider": "",
            "model": "",
            "skipReason": SENSITIVE_FIELD_BLOCK_REASON,
            "elapsedMs": 0.0,
        },
        "messages": [],
        "evidencePack": [],
        "streamEvents": [],
        "candidates": [],
    }
def _debug_lane_breakdown(raw_lanes: object) -> dict[str, object]:
    if not isinstance(raw_lanes, dict):
        return {}
    result: dict[str, object] = {}
    for name, payload in raw_lanes.items():
        if not isinstance(payload, dict):
            continue
        result[_camel_lane_name(str(name))] = {
            "enabled": bool(payload.get("enabled", True)),
            "available": bool(payload.get("available", True)),
            "implementation": _string(payload.get("implementation")),
            "lexicalFallback": bool(payload.get("lexicalFallback")),
            "fts5Bm25": bool(payload.get("fts5Bm25")),
            "skippedReason": _string(payload.get("skippedReason")),
            "weight": float(payload.get("weight") or 0.0),
            "count": int(payload.get("count") or 0),
            "docIds": list(payload.get("docIds") or []),
        }
    return result


def _debug_query_preview(query: object, *, include_text: bool) -> dict[str, object]:
    if not isinstance(query, dict):
        return {}
    if include_text:
        return dict(query)
    return {
        "primaryHash": _stable_debug_hash(_string(query.get("primary"))),
        "lexicalTermCount": len(query.get("lexicalTerms") or []),
        "matchedAliasCount": len(query.get("matchedAliases") or []),
        "activatedTagCount": len(query.get("activatedTags") or []),
        "negativeTagCount": len(query.get("negativeTags") or []),
        "expansionTermCount": len(query.get("expansionTerms") or []),
    }


def _debug_redact_rag_candidate(item: dict[str, object], *, include_text: bool) -> dict[str, object]:
    if include_text:
        return _redact_mapping(dict(item), include_text=True)
    text = _string(item.get("text") or item.get("insert_text") or item.get("insertText"))
    evidence_preview = _string(item.get("evidence_preview") or item.get("evidencePreview"))
    metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
    return {
        "candidateIdHash": _stable_debug_hash(_string(item.get("candidate_id") or item.get("candidateId"))),
        "textHash": _stable_debug_hash(text),
        "sourceType": item.get("source_type") or item.get("sourceType") or "",
        "sourceLane": item.get("source_lane") or item.get("sourceLane") or "",
        "score": float(item.get("score") or 0.0),
        "confidence": float(item.get("confidence") or 0.0),
        "tagCount": len(item.get("tags") or []),
        "memoryIdCount": len(item.get("memory_ids") or item.get("memoryIds") or []),
        "atomIdCount": len(item.get("atom_ids") or item.get("atomIds") or []),
        "bookIdCount": len(item.get("book_ids") or item.get("bookIds") or []),
        "evidenceEventIds": list(item.get("evidence_event_ids") or item.get("evidenceEventIds") or []),
        "evidencePreviewHash": _stable_debug_hash(evidence_preview),
        "debugFeatures": dict(item.get("debug_features") or item.get("debugFeatures") or {})
        if isinstance(item.get("debug_features") or item.get("debugFeatures"), dict)
        else {},
        "metadata": _redact_mapping(metadata, include_text=False),
    }


def _camel_lane_name(name: str) -> str:
    parts = [part for part in name.split("_") if part]
    if not parts:
        return name
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _debug_deepseek_evidence_pack(candidates: list[object], *, include_text: bool) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for item in candidates[:8]:
        if not isinstance(item, dict):
            continue
        text = compact_whitespace(str(item.get("text") or ""))
        evidence_preview = compact_whitespace(str(item.get("evidence_preview") or item.get("evidencePreview") or ""))
        payload: dict[str, object] = {
            "sourceType": item.get("source_type") or item.get("sourceType"),
            "sourceLane": item.get("source_lane") or item.get("sourceLane"),
            "textHash": _stable_debug_hash(text),
            "evidencePreviewHash": _stable_debug_hash(evidence_preview),
            "tagCount": len(item.get("tags") or []),
        }
        if include_text:
            payload.update({"text": text, "evidencePreview": evidence_preview, "tags": item.get("tags") or []})
        evidence.append(payload)
    return evidence


def _debug_memory_book_source_bundle(bundle: dict[str, object], *, include_text: bool) -> dict[str, object]:
    events: list[dict[str, object]] = []
    for item in bundle.get("recentEvents") or []:
        if not isinstance(item, dict):
            continue
        text = _string(item.get("text"))
        recent_context = _string(item.get("recentContext"))
        event: dict[str, object] = {
            "eventId": int(item.get("eventId") or 0),
            "createdAtMs": int(item.get("createdAtMs") or 0),
            "source": _string(item.get("source")),
            "app": _string(item.get("app")),
            "project": _string(item.get("project")),
            "tagCount": len(item.get("tags") or []),
            "textHash": _stable_debug_hash(text),
            "recentContextHash": _stable_debug_hash(recent_context),
        }
        if include_text:
            event.update({"text": text, "recentContext": recent_context, "tags": item.get("tags") or []})
        events.append(event)
    return {
        "schemaVersion": bundle.get("schemaVersion") or "rag-ime.memory-book-source-bundle.v1",
        "project": _string(bundle.get("project")),
        "sinceDays": int(bundle.get("sinceDays") or 0),
        "exportedAtMs": int(bundle.get("exportedAtMs") or 0),
        "redactionStats": dict(bundle.get("redactionStats") or {}) if isinstance(bundle.get("redactionStats"), dict) else {},
        "recentEventCount": len(events),
        "recentEvents": events,
    }


def _privacy_preview(text: str, *, max_chars: int = 12) -> str:
    compact = compact_whitespace(text)
    if not compact:
        return ""
    if len(compact) <= max_chars:
        return compact
    return f"{compact[:max_chars]}..."


def _redact_history_item(item: dict[str, object], *, include_text: bool) -> dict[str, object]:
    text = _string(item.get("text"))
    recent_context = _string(item.get("recentContext"))
    preedit = _string(item.get("preedit"))
    payload = {key: value for key, value in item.items() if key not in {"text", "recentContext", "preedit"}}
    payload.update(
        {
            "textHash": _stable_debug_hash(text),
            "textPreview": _privacy_preview(text),
            "recentContextHash": _stable_debug_hash(recent_context),
            "recentContextPreview": _privacy_preview(recent_context),
            "preeditHash": _stable_debug_hash(preedit),
            "preeditPreview": _privacy_preview(preedit),
        }
    )
    if include_text:
        payload.update({"text": text, "recentContext": recent_context, "preedit": preedit})
    return payload


def _rebind_cached_rime_prediction_payload(
    response: dict[str, object],
    *,
    snapshot: object,
    semantic_query: str,
    query_basis: str,
) -> None:
    prediction_session = response.get("predictionSession")
    if not isinstance(prediction_session, dict):
        return
    input_mode = _string(prediction_session.get("inputMode")) or _string(
        response.get("predictionFirst", {}).get("mode") if isinstance(response.get("predictionFirst"), dict) else ""
    )
    transaction_payload = frontend_transaction_to_payload(snapshot.frontend_transaction)
    anchors = build_prediction_anchors_from_snapshot(
        snapshot=snapshot,
        mode=input_mode,
        semantic_query=semantic_query,
        query_basis=query_basis,
        stable_short_pinyin_prefix=snapshot.preedit or snapshot.raw_input,
    )
    anchor_payload = {
        "hardContextAnchor": anchors.hard_context_anchor,
        "queryAnchor": anchors.query_anchor,
        "displayAnchor": anchors.display_anchor,
    }
    prediction_session.update(
        {
            "requestSeq": snapshot.request_seq,
            **transaction_payload,
            **anchor_payload,
            "cacheRebound": True,
        }
    )
    stable_panel = prediction_session.get("stablePanel")
    if isinstance(stable_panel, dict):
        stable_panel.update(anchor_payload)
        stable_panel["cacheRebound"] = True
    for candidate in response.get("displayCandidates") or []:
        if not isinstance(candidate, dict):
            continue
        metadata = candidate.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            candidate["metadata"] = metadata
        rebound = {
            "requestSeq": snapshot.request_seq,
            "sessionId": snapshot.session_id,
            **transaction_payload,
            **anchor_payload,
            "cacheRebound": True,
        }
        metadata.update(rebound)
        candidate.update(
            {
                "hardContextAnchor": anchors.hard_context_anchor,
                "queryAnchor": anchors.query_anchor,
                "displayAnchor": anchors.display_anchor,
            }
        )
    for trace_event in response.get("predictionTraceEvents") or []:
        if not isinstance(trace_event, dict):
            continue
        fields = trace_event.get("fields")
        if isinstance(fields, dict):
            fields.update({**anchor_payload, "cacheRebound": True})


def _redact_memory_item(item: dict[str, object], *, include_text: bool, lexicon: bool) -> dict[str, object]:
    text = _string(item.get("text"))
    normalized_text = _string(item.get("normalizedText"))
    metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
    payload = {key: value for key, value in item.items() if key not in {"text", "normalizedText", "metadata"}}
    payload.update(
        {
            "textHash": _stable_debug_hash(text),
            "textPreview": _privacy_preview(text, max_chars=16 if lexicon else 12),
            "normalizedTextHash": _stable_debug_hash(normalized_text),
            "metadata": _redact_mapping(metadata, include_text=include_text),
        }
    )
    if include_text:
        payload.update({"text": text, "normalizedText": normalized_text})
    return payload


def _rime_lexicon_export_entry(item: dict[str, object]) -> dict[str, object]:
    phrase = compact_whitespace(_string(item.get("text"))).replace("\t", " ")
    quality_score = _float_or_default(item.get("qualityScore"), 0.5)
    confidence = _float_or_default(item.get("confidence"), 0.5)
    weight = max(1, min(100, int(round(((quality_score * 0.7) + (confidence * 0.3)) * 100))))
    return {
        "phrase": phrase,
        "weight": weight,
        "memoryId": _string(item.get("memoryId")),
        "status": _string(item.get("status")),
        "qualityScore": quality_score,
        "confidence": confidence,
    }


def _rime_lexicon_export_text(*, project: str, status: str, entries: list[dict[str, object]]) -> str:
    lines = [
        "# RAG-IME lexicon export preview",
        "# Dry-run only: review before importing or writing to Rime user files.",
        f"# project: {project}",
        f"# status: {status}",
        "# format: phrase<TAB>weight<TAB>memory_id",
    ]
    for entry in entries:
        phrase = _string(entry.get("phrase")).replace("\n", " ").replace("\r", " ").replace("\t", " ")
        memory_id = _string(entry.get("memoryId")).replace("\t", " ")
        lines.append(f"{phrase}\t{int(entry.get('weight') or 1)}\t{memory_id}")
    return "\n".join(lines) + "\n"


def _redact_mapping(value: dict[str, object], *, include_text: bool) -> dict[str, object]:
    redacted: dict[str, object] = {}
    sensitive_keys = {
        "text",
        "rawText",
        "raw_text",
        "recentContext",
        "preedit",
        "committedContext",
        "evidencePreview",
        "evidence_preview",
        "payload",
        "result",
    }
    for key, item in value.items():
        if isinstance(item, dict):
            redacted[key] = _redact_mapping(item, include_text=include_text)
        elif isinstance(item, list):
            redacted[key] = [
                _redact_mapping(part, include_text=include_text) if isinstance(part, dict) else part
                for part in item
            ]
        elif include_text or key not in sensitive_keys:
            redacted[key] = item
        else:
            text = _string(item)
            redacted[f"{key}Hash"] = _stable_debug_hash(text)
            redacted[f"{key}Chars"] = len(text)
    return redacted


def _score_penalty(score_breakdown: object) -> float:
    if not isinstance(score_breakdown, dict):
        return 0.0
    penalty = 0.0
    for key, value in score_breakdown.items():
        if "penalty" not in str(key).lower():
            continue
        try:
            penalty += abs(float(value))
        except (TypeError, ValueError):
            continue
    return penalty


def _management_action(raw: str) -> str:
    normalized = raw.strip().lower().replace("_", "-")
    aliases = {
        "approved": "approve",
        "accept": "approve",
        "accepted": "approve",
        "rejected": "reject",
        "delete": "tombstone",
        "deleted": "tombstone",
        "downranked": "downrank",
        "pinned": "pin",
    }
    return aliases.get(normalized, normalized)


def _ensure_management_audit_schema(conn) -> None:
    ensure_management_tables(conn)


def _json_loads_dict(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _cleanup_diff_payload_for_debug(conn, *, diff_id: int) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT id, run_id, op, target_memory_id, payload_json, status, created_at_ms, applied_at_ms, rollback_json
        FROM memory_cleanup_diffs
        WHERE id = ?
        LIMIT 1
        """,
        (int(diff_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"cleanup diff not found: {diff_id}")
    payload = _json_loads_dict(row["payload_json"])
    rollback = _json_loads_dict(row["rollback_json"])
    return {
        "diffId": int(row["id"]),
        "runId": str(row["run_id"]),
        "op": str(row["op"]),
        "targetMemoryId": str(row["target_memory_id"]),
        "payload": _redact_mapping(payload, include_text=False),
        "status": str(row["status"]),
        "createdAtMs": int(row["created_at_ms"] or 0),
        "appliedAtMs": int(row["applied_at_ms"] or 0),
        "rollback": _redact_mapping(rollback, include_text=False),
    }


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _memory_maintenance_result_count(value: object, key: str) -> int:
    if isinstance(value, Mapping):
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return max(0, candidate)
        for child in value.values():
            found = _memory_maintenance_result_count(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value[:128]:
            found = _memory_maintenance_result_count(child, key)
            if found:
                return found
    return 0


def _memory_visible_owners_from_payload(
    payload: Mapping[str, object],
    *,
    project: str,
) -> tuple[tuple[str, str], ...] | None:
    if "visibleOwners" not in payload:
        return None
    raw = payload.get("visibleOwners")
    values: list[tuple[str, str]] = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(item, Mapping):
            values.append(
                (
                    _string(item.get("ownerKind")),
                    _string(item.get("ownerId")),
                )
            )
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            values.append((_string(item[0]), _string(item[1])))
    if not values:
        return ()
    return resolve_visible_memory_owners(values, project=project)


def _require_expected_memory_run_owner(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    payload: Mapping[str, object],
) -> None:
    expected_kind = _string(payload.get("expectedOwnerKind"))
    expected_id = _string(payload.get("expectedOwnerId"))
    if not expected_kind and not expected_id:
        return
    expected = normalize_memory_owner(expected_kind, expected_id)
    run = memory_book_run_payload(conn, run_id=run_id)
    if not run.get("provider"):
        raise ValueError(f"memory book run not found: {run_id}")
    actual = (_string(run.get("ownerKind")), _string(run.get("ownerId")))
    if actual != expected:
        raise ValueError("memory book run owner does not match the approved role")


def _nested_agent_configuration_value(
    configuration: object,
    dotted_key: str,
) -> object:
    if not isinstance(configuration, dict):
        return None
    section, leaf = dotted_key.split(".", 1)
    branch = configuration.get(section)
    return branch.get(leaf) if isinstance(branch, dict) else None


def _strict_read_query(
    query: Mapping[str, list[str]],
    allowed: frozenset[str],
) -> dict[str, object]:
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query field: {unknown[0]}")
    result: dict[str, object] = {}
    for key, values in query.items():
        if len(values) != 1:
            raise ValueError(f"query field must appear once: {key}")
        result[key] = values[0]
    return result


def _memory_entity_path(path: str) -> tuple[str, str]:
    suffix = path.removeprefix(_MEMORY_ENTITY_PATH_PREFIX)
    parts = suffix.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("memory entity path must contain kind and id")
    return unquote(parts[0]), unquote(parts[1])


def _memory_reference_path(path: str) -> tuple[str, str]:
    suffix = path.removeprefix(_MEMORY_REFERENCE_PATH_PREFIX)
    parts = suffix.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("memory reference path must contain kind and id")
    kind, reference_id = unquote(parts[0]), unquote(parts[1])
    if kind not in {
        "event",
        "evidence",
        "atom",
        "book",
        "timeline",
        "role_book_revision",
    }:
        raise ValueError("unsupported memory reference kind")
    return kind, reference_id


def _memory_read_error(message: str) -> dict[str, object]:
    payload = {
        "schemaVersion": "rag-ime.memory-read-error.v1",
        "ok": False,
        "errorCode": "invalid_request",
        "error": message[:256],
    }
    validate_contract(payload, "memory-read-error.v1.json")
    return payload


def _query_first(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0] if values else ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _input_capture_metadata(
    value: object,
    *,
    text: str,
    source: str,
    app: str,
) -> dict[str, object]:
    """Keep only bounded, text-free provenance for a finalized input."""

    return sanitize_input_capture_metadata(
        value,
        text=text,
        source=source,
        app=app,
    )


def _capture_commit_response(
    privacy_assessment: Mapping[str, object],
    *,
    capture_receipt: Mapping[str, object],
    event_count: int | None = None,
) -> dict[str, object]:
    outcome = compact_whitespace(str(capture_receipt.get("outcome") or "no_store"))
    event_id = compact_whitespace(str(capture_receipt.get("eventId") or ""))
    stored = outcome == "stored" and bool(event_id)
    response: dict[str, object] = {
        "schemaVersion": "rag-ime.foreground-commit.v1",
        "ok": True,
        "stored": stored,
        "noStore": not stored,
        "eventId": event_id,
        "privacyAssessment": dict(privacy_assessment),
        "storageReceipt": storage_receipt(
            privacy_assessment,
            stored=stored,
            event_id=event_id,
            outcome=outcome,
            reason=str(capture_receipt.get("reason") or "capture_outcome"),
        ),
        "captureReceipt": dict(capture_receipt),
    }
    if event_count is not None:
        response["eventCount"] = max(0, int(event_count))
    return response


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    parsed: list[int] = []
    for item in value:
        candidate = _optional_int(item)
        if candidate is not None:
            parsed.append(candidate)
    return parsed


def _bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        return default
    return max(minimum, min(maximum, parsed))


def _float_or_default(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _embedding_warmup_delay_seconds() -> float:
    configured = _float_or_default(
        os.environ.get("RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS", "0"),
        0.0,
    )
    if not math.isfinite(configured):
        return 0.0
    return max(0.0, min(_MAX_EMBEDDING_WARMUP_DELAY_SECONDS, configured))


def _cleanup_review_status(payload: dict[str, object]) -> str:
    review_status = _string(payload.get("reviewStatus")).strip().lower()
    action = _string(payload.get("action")).strip().lower()
    if not review_status and action in {"approve", "approved"}:
        review_status = "approved"
    if not review_status and action in {"reject", "rejected"}:
        review_status = "rejected"
    if not review_status and action in {"reset", "pending"}:
        review_status = "pending"
    return review_status


def _cleanup_diff_path_id(path: str, *, suffix: str) -> int:
    prefix = "/api/memory/cleanup-diff/"
    if not path.startswith(prefix) or not path.endswith(suffix):
        raise ValueError("invalid cleanup diff path")
    raw = path[len(prefix) : -len(suffix)].strip("/")
    diff_id = _optional_int(raw)
    if diff_id is None:
        raise ValueError("cleanup diff path requires numeric diff id")
    return diff_id


def _attach_rime_ranking_diagnostics(response: dict[str, object]) -> None:
    response["rankingDiagnostics"] = _rime_ranking_diagnostics(response)


def _rime_ranking_diagnostics(response: dict[str, object]) -> dict[str, object]:
    display_candidates = response.get("displayCandidates")
    if not isinstance(display_candidates, list):
        display_candidates = []
    rag_candidates = response.get("ragCandidates")
    if not isinstance(rag_candidates, list):
        rag_candidates = []
    items: list[dict[str, object]] = []
    evidence_items: list[dict[str, object]] = []
    source_counts: dict[str, int] = {}
    evidence_source_counts: dict[str, int] = {}
    has_rag_breakdown = False
    has_model_candidate_scores = False
    for index, candidate in enumerate(display_candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        source_type = _string(candidate.get("sourceType")) or _string(candidate.get("displayLane")) or "unknown"
        source_counts[source_type] = source_counts.get(source_type, 0) + 1
        diagnostics = _display_candidate_diagnostics(candidate, default_rank=index)
        breakdown = diagnostics.get("scoreBreakdown")
        if isinstance(breakdown, dict):
            has_rag_breakdown = True
        if int(diagnostics.get("candidateScoreCount") or 0) > 0:
            has_model_candidate_scores = True
        items.append(diagnostics)
    for index, candidate in enumerate(rag_candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
        source_type = _string(metadata.get("source_type") or candidate.get("sourceType") or "rag")
        evidence_source_counts[source_type] = evidence_source_counts.get(source_type, 0) + 1
        diagnostics = _rag_evidence_candidate_diagnostics(candidate, default_rank=index)
        if isinstance(diagnostics.get("scoreBreakdown"), dict):
            has_rag_breakdown = True
        evidence_items.append(diagnostics)
    side_count = sum(count for source, count in source_counts.items() if source != "rime")
    rime_count = source_counts.get("rime", 0)
    return {
        "schemaVersion": "rag-ime.ranking-diagnostics.v1",
        "candidateCount": len(items),
        "ragEvidenceCount": len(evidence_items),
        "sideCandidateCount": side_count,
        "rimeCandidateCount": rime_count,
        "sourceCounts": source_counts,
        "evidenceSourceCounts": evidence_source_counts,
        "hasRagScoreBreakdown": has_rag_breakdown,
        "hasModelCandidateScores": has_model_candidate_scores,
        "topCandidate": items[0] if items else None,
        "items": items,
        "evidenceItems": evidence_items,
    }


def _rag_evidence_candidate_diagnostics(candidate: dict[str, object], *, default_rank: int) -> dict[str, object]:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    normalized = dict(candidate)
    normalized.setdefault("text", candidate.get("surfaceText") or candidate.get("insertText"))
    normalized.setdefault("sourceType", metadata.get("source_type") or "rag")
    normalized.setdefault("displayLane", metadata.get("source_type") or "rag_evidence")
    return _display_candidate_diagnostics(normalized, default_rank=default_rank)


def _display_candidate_diagnostics(candidate: dict[str, object], *, default_rank: int) -> dict[str, object]:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    source_type = _string(candidate.get("sourceType")) or _string(candidate.get("displayLane")) or "unknown"
    item: dict[str, object] = {
        "selectionKey": _string(candidate.get("selectionKey")) or _string(candidate.get("label")),
        "selectionRank": _optional_int(candidate.get("selectionRank")) or default_rank,
        "text": _string(candidate.get("text")),
        "insertTextLength": len(_string(candidate.get("insertText"))),
        "sourceType": source_type,
        "displayLane": _string(candidate.get("displayLane")) or source_type,
        "displayLayout": _string(candidate.get("displayLayout")),
        "selectionAction": _string(candidate.get("selectionAction")),
        "sourceIndex": _optional_int(candidate.get("sourceIndex")),
        "memoryId": _string(candidate.get("memoryId")),
        "sourceEventId": _optional_int(candidate.get("sourceEventId")),
        "suggestionId": _string(candidate.get("suggestionId")),
        "reason": _string(metadata.get("reason")),
    }
    breakdown = metadata.get("score_breakdown")
    if breakdown is None:
        breakdown = metadata.get("scoreBreakdown")
    if isinstance(breakdown, dict):
        item["scoreBreakdown"] = breakdown
        item["scoreBreakdownTotal"] = _number_or_none(breakdown.get("total"))
        components = breakdown.get("components") if isinstance(breakdown.get("components"), dict) else {}
        item["topScoreComponents"] = _top_score_components(components)
        raw_signals = breakdown.get("rawSignals") if isinstance(breakdown.get("rawSignals"), dict) else {}
        item["rawSignalsSummary"] = _raw_signals_summary(raw_signals)
    candidate_scores = metadata.get("candidate_scores")
    if candidate_scores is None:
        candidate_scores = metadata.get("candidateScores")
    if isinstance(candidate_scores, list):
        item["candidateScoreCount"] = len(candidate_scores)
        item["candidateScoresPreview"] = candidate_scores[:5]
    else:
        item["candidateScoreCount"] = 0
    provider_name = _string(metadata.get("provider_name")) or _string(metadata.get("providerName"))
    if provider_name:
        item["providerName"] = provider_name
    candidate_mode = _string(metadata.get("candidate_mode")) or _string(metadata.get("candidateMode"))
    if not candidate_mode and isinstance(metadata.get("server_timing"), dict):
        candidate_mode = _string(metadata["server_timing"].get("candidateMode"))  # type: ignore[index]
    if candidate_mode:
        item["candidateMode"] = candidate_mode
    return item


def _ranking_top_candidate_summary(diagnostics: dict[str, object]) -> dict[str, object] | None:
    top = diagnostics.get("topCandidate")
    if not isinstance(top, dict):
        return None
    payload = {
        key: value
        for key, value in top.items()
        if key
        in {
            "selectionKey",
            "selectionRank",
            "text",
            "sourceType",
            "displayLane",
            "displayLayout",
            "scoreBreakdownTotal",
            "candidateScoreCount",
            "candidateMode",
            "reason",
        }
        and value not in ("", None)
    }
    top_components = top.get("topScoreComponents")
    if isinstance(top_components, list) and top_components:
        payload["topScoreComponents"] = top_components[:3]
    return payload


def _prediction_live_trace_frame(
    *,
    response: dict[str, object],
    request_payload: dict[str, Any],
    include_raw_text: bool,
) -> dict[str, object]:
    prediction_session = response.get("predictionSession") if isinstance(response.get("predictionSession"), dict) else {}
    sensitive_response = _string(prediction_session.get("clearReason")) == "sensitive_field"
    rag_lane = response.get("ragLane") if isinstance(response.get("ragLane"), dict) else {}
    model_lane = response.get("modelLane") if isinstance(response.get("modelLane"), dict) else {}
    display_candidates = response.get("displayCandidates") if isinstance(response.get("displayCandidates"), list) else []
    trace_events = response.get("predictionTraceEvents") if isinstance(response.get("predictionTraceEvents"), list) else []
    raw_input = "" if sensitive_response else _string(response.get("rawInput") or request_payload.get("rawInput"))
    preedit = "" if sensitive_response else _string(response.get("preedit") or request_payload.get("preedit"))
    committed_context = "" if sensitive_response else _string(
        response.get("committedContext") or request_payload.get("committedContext")
    )
    raw_foreground = (
        model_lane.get("foregroundContext")
        if isinstance(model_lane.get("foregroundContext"), dict)
        else rag_lane.get("foregroundContext")
    )
    foreground_context = _foreground_context_trace_payload(raw_foreground)
    frame: dict[str, object] = {
        "schemaVersion": "rag-ime.prediction-frame.v1",
        "recordedAtMs": now_ms(),
        "sessionId": _string(response.get("sessionId")),
        "requestSeq": _bounded_int(response.get("requestSeq"), default=0, minimum=0, maximum=2**63 - 1),
        "frontendRevision": _bounded_int(response.get("frontendRevision"), default=0, minimum=0, maximum=2**63 - 1),
        "selectionEpoch": _bounded_int(response.get("selectionEpoch"), default=0, minimum=0, maximum=2**63 - 1),
        "panelSessionId": _string(response.get("panelSessionId")),
        "input": _redacted_text_snapshot(raw_input, include_raw_text=include_raw_text),
        "preedit": _redacted_text_snapshot(preedit, include_raw_text=include_raw_text),
        "committedContext": _redacted_text_snapshot(committed_context, include_raw_text=include_raw_text),
        "foregroundContext": foreground_context,
        "predictionSession": {
            "phase": _string(prediction_session.get("phase")),
            "inputMode": _string(prediction_session.get("inputMode")),
            "hardContextAnchor": _string(prediction_session.get("hardContextAnchor")),
            "applyAnchor": _string(prediction_session.get("applyAnchor")),
            "queryAnchor": _string(prediction_session.get("queryAnchor")),
            "displayAnchor": _string(prediction_session.get("displayAnchor")),
            "stablePanelAction": _string(prediction_session.get("stablePanelAction")),
            "stablePanelReason": _string(prediction_session.get("stablePanelReason")),
            "snapshotId": _string(prediction_session.get("snapshotId") or prediction_session.get("stableSnapshotId")),
            "reusedLastGood": _bool(prediction_session.get("reusedLastGood"), default=False),
            "shouldClearPredictionPanel": _bool(prediction_session.get("shouldClearPredictionPanel"), default=False),
        },
        "ragLane": _prediction_lane_summary(rag_lane),
        "modelLane": _prediction_lane_summary(model_lane),
        "display": {
            "visibleCandidateCount": len(display_candidates),
            "sourceCounts": _display_payload_source_counts(display_candidates),
            "statusRowCount": sum(
                1
                for item in display_candidates
                if isinstance(item, dict) and _string(item.get("sourceType")) == "status"
            ),
            "candidates": [
                _prediction_candidate_summary(item, include_raw_text=include_raw_text)
                for item in display_candidates[:10]
                if isinstance(item, dict)
            ],
        },
        "cache": response.get("cache") if isinstance(response.get("cache"), dict) else {},
        "dropReasons": _prediction_drop_reasons(rag_lane=rag_lane, model_lane=model_lane, trace_events=trace_events),
        "traceEvents": _prediction_trace_event_summaries(trace_events),
    }
    return frame


def _foreground_context_trace_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    keep = (
        "applied",
        "source",
        "confidence",
        "freshnessMs",
        "capturedAtMs",
        "captureEpoch",
        "captureFailureReason",
        "reason",
        "commitTextMatched",
        "commitTextMatchDeclared",
        "contextGroupLevel",
        "contextGroupConfidence",
        "selectedTextChars",
        "surroundingBeforeChars",
        "surroundingAfterChars",
    )
    return {key: value.get(key) for key in keep if value.get(key) not in (None, "")}


def _redacted_text_snapshot(text: str, *, include_raw_text: bool) -> dict[str, object]:
    normalized = compact_whitespace(text)
    payload: dict[str, object] = {
        "length": len(normalized),
        "hash": _sha16_text(normalized) if normalized else "",
    }
    if include_raw_text:
        payload["text"] = normalized
    return payload


def _sha16_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _prediction_lane_summary(lane: dict[str, object]) -> dict[str, object]:
    keep = (
        "called",
        "timedOut",
        "staleDropped",
        "staleDropReason",
        "waitingForLatest",
        "skippedReason",
        "predictionCount",
        "suggestionCount",
        "elapsedMs",
        "latencyBudgetMs",
        "activeGeneration",
        "providerName",
        "candidateMode",
    )
    return {key: lane.get(key) for key in keep if key in lane and lane.get(key) not in ("", None)}


def _registered_model_option(deployment: ModelDeployment) -> dict[str, object]:
    profile_id = canonical_runtime_profile_id(deployment.profile)
    profile = profile_by_id(profile_id)
    return {
        "id": deployment.model_id,
        "path": deployment.path,
        "profileId": profile_id,
        "promptMode": deployment.prompt_mode or profile.prompt_mode,
        "maxTokens": deployment.max_tokens or profile.max_tokens,
        "temperature": (
            profile.temperature
            if deployment.temperature is None
            else deployment.temperature
        ),
        "topP": profile.top_p if deployment.top_p is None else deployment.top_p,
        "active": deployment.active,
    }


def _configured_model_registry_path() -> Path:
    configured = _string(os.environ.get("RAG_IME_MODEL_REGISTRY")).strip()
    return Path(configured).expanduser() if configured else default_model_registry_path()


def _predictor_status_matches_configuration(
    status: Mapping[str, object],
    configuration: PredictorConfiguration,
) -> bool:
    return (
        bool(status.get("configured"))
        and _model_reference_matches(status.get("model"), configuration)
        and canonical_runtime_profile_id(_string(status.get("providerProfile")))
        == configuration.profile_id
        and _string(status.get("promptMode")) == configuration.prompt_mode
        and _bounded_int(status.get("maxTokens"), default=0, minimum=0, maximum=64)
        == configuration.max_tokens
        and _float_matches(status.get("temperature"), configuration.temperature)
        and _float_matches(status.get("topP"), configuration.top_p)
    )


def _predictor_probe_matches_configuration(
    probe: Mapping[str, object],
    configuration: PredictorConfiguration,
) -> bool:
    runtime_config = (
        probe.get("runtimeConfig")
        if isinstance(probe.get("runtimeConfig"), Mapping)
        else {}
    )
    return (
        bool(probe.get("ok"))
        and bool(probe.get("modelLoaded"))
        and _model_reference_matches(probe.get("model"), configuration)
        and canonical_runtime_profile_id(_string(runtime_config.get("profileId")))
        == configuration.profile_id
        and _string(runtime_config.get("promptMode")) == configuration.prompt_mode
        and _bounded_int(runtime_config.get("maxTokens"), default=0, minimum=0, maximum=64)
        == configuration.max_tokens
        and _float_matches(runtime_config.get("temperature"), configuration.temperature)
        and _float_matches(runtime_config.get("topP"), configuration.top_p)
    )


def _model_reference_matches(
    value: object,
    configuration: PredictorConfiguration,
) -> bool:
    reference = _string(value).strip()
    if reference == configuration.model_id:
        return True
    if not reference:
        return False
    try:
        return Path(reference).expanduser().resolve(strict=False) == Path(
            configuration.model_path
        ).expanduser().resolve(strict=False)
    except OSError:
        return False


def _float_matches(value: object, expected: float) -> bool:
    try:
        return abs(float(value) - float(expected)) <= 1e-9
    except (TypeError, ValueError):
        return False


def _prediction_candidate_summary(candidate: dict[str, object], *, include_raw_text: bool) -> dict[str, object]:
    summary: dict[str, object] = {
        "sourceType": _string(candidate.get("sourceType")),
        "displayLane": _string(candidate.get("displayLane")),
        "displayLayout": _string(candidate.get("displayLayout")),
        "selectionKey": _string(candidate.get("selectionKey")),
        "candidateOrdinal": _bounded_int(candidate.get("candidateOrdinal"), default=0, minimum=0, maximum=100),
        "selectionAction": _string(candidate.get("selectionAction")),
        "badge": _string(candidate.get("badge")),
        "colorToken": _string(candidate.get("colorToken")),
    }
    if include_raw_text:
        summary["text"] = _string(candidate.get("text"))
        summary["insertText"] = _string(candidate.get("insertText"))
    else:
        summary["text"] = _redacted_text_snapshot(_string(candidate.get("text")), include_raw_text=False)
    return {key: value for key, value in summary.items() if value not in ("", None)}


def _display_payload_source_counts(candidates: list[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        source = _string(item.get("sourceType")) or "unknown"
        counts[source] = counts.get(source, 0) + 1
    return counts


def _prediction_drop_reasons(
    *,
    rag_lane: dict[str, object],
    model_lane: dict[str, object],
    trace_events: list[object],
) -> list[str]:
    reasons: list[str] = []
    for lane in (rag_lane, model_lane):
        reason = _string(lane.get("staleDropReason") or lane.get("dropReason") or lane.get("skippedReason"))
        if reason and reason not in reasons:
            reasons.append(reason)
    for event in trace_events:
        if not isinstance(event, dict):
            continue
        fields = event.get("fields") if isinstance(event.get("fields"), dict) else {}
        reason = _string(fields.get("reason") or fields.get("staleDropReason") or fields.get("dropReason"))
        if reason and reason not in reasons:
            reasons.append(reason)
    return reasons


def _prediction_trace_event_summaries(trace_events: list[object]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for event in trace_events[-16:]:
        if not isinstance(event, dict):
            continue
        fields = event.get("fields") if isinstance(event.get("fields"), dict) else {}
        summaries.append(
            {
                key: value
                for key, value in {
                    "event": _string(event.get("event")),
                    "action": _string(fields.get("action")),
                    "reason": _string(fields.get("reason")),
                    "snapshotId": _string(fields.get("snapshotId")),
                    "visibleCandidateCount": fields.get("visibleCandidateCount"),
                    "sourceSummary": fields.get("sourceSummary"),
                    "staleDropReason": _string(fields.get("staleDropReason")),
                }.items()
                if value not in ("", None)
            }
        )
    return summaries


def _prediction_drop_stats(frames: list[dict[str, object]]) -> dict[str, object]:
    by_reason: dict[str, int] = {}
    lane_stale_drops = {"rag": 0, "model": 0}
    for frame in frames:
        for reason in frame.get("dropReasons", []) if isinstance(frame.get("dropReasons"), list) else []:
            text = _string(reason)
            if text:
                by_reason[text] = by_reason.get(text, 0) + 1
        rag_lane = frame.get("ragLane") if isinstance(frame.get("ragLane"), dict) else {}
        model_lane = frame.get("modelLane") if isinstance(frame.get("modelLane"), dict) else {}
        if bool(rag_lane.get("staleDropped")):
            lane_stale_drops["rag"] += 1
        if bool(model_lane.get("staleDropped")):
            lane_stale_drops["model"] += 1
    return {
        "byReason": by_reason,
        "laneStaleDrops": lane_stale_drops,
        "totalDropReasons": sum(by_reason.values()),
    }


def _top_score_components(components: dict[object, object], *, limit: int = 5) -> list[dict[str, object]]:
    numeric_components: list[tuple[str, float]] = []
    for key, value in components.items():
        score = _number_or_none(value)
        if score is None or abs(score) <= 0.0005:
            continue
        numeric_components.append((str(key), score))
    numeric_components.sort(key=lambda item: abs(item[1]), reverse=True)
    return [{"name": name, "score": score} for name, score in numeric_components[:limit]]


def _raw_signals_summary(raw_signals: dict[object, object]) -> dict[str, object]:
    keep = (
        "effectiveFrequency",
        "effectiveFrequencyScope",
        "acceptedCount",
        "skippedCount",
        "downrankedCount",
        "pinned",
        "vectorScore",
    )
    return {key: raw_signals[key] for key in keep if key in raw_signals}


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _parse_input_source_check_output(output: str) -> dict[str, object]:
    parsed: dict[str, object] = {}
    id_match = re.search(r"\bid=([^\s]+)", output)
    if id_match:
        parsed["id"] = id_match.group(1)
    name_match = re.search(r"\bname=(.*?)\s+enabled=", output)
    if name_match:
        parsed["name"] = name_match.group(1)
    for key, value in re.findall(r"\b(enabled|selectable|selected|hitoolboxEnabled|thirdPartyEnabled)=([^\s]+)", output):
        parsed[key] = value.lower() == "true"
    current_match = re.search(r"\bcurrent=([^\s]+)", output)
    if current_match:
        parsed["current"] = current_match.group(1)
    return parsed


def _input_source_readiness(parsed: dict[str, object], *, ok: bool, typing_ready: bool) -> dict[str, object]:
    enabled = parsed.get("enabled") is True
    selectable = parsed.get("selectable") is True
    # Third-party input methods are canonically registered in
    # com.apple.inputsources. Mirroring them into HIToolbox creates duplicate
    # TIS rows on current macOS releases, so HIToolbox is diagnostic only.
    third_party_enabled = parsed.get("thirdPartyEnabled") is not False
    current = _string(parsed.get("current"))
    target = _string(parsed.get("id")) or "Squirrel"
    target_name = _string(parsed.get("name")) or _input_source_display_name(target)
    product_name = _input_source_product_name(target_name)
    readiness_checks = [
        {
            "name": "tis-visible",
            "passed": enabled and selectable,
            "enabled": parsed.get("enabled"),
            "selectable": parsed.get("selectable"),
        },
        {
            "name": "third-party-list",
            "passed": third_party_enabled,
            "hitoolboxEnabled": parsed.get("hitoolboxEnabled"),
            "thirdPartyEnabled": parsed.get("thirdPartyEnabled"),
        },
        {
            "name": "selected",
            "passed": ok and typing_ready,
            "selected": parsed.get("selected"),
            "current": current,
        },
    ]
    if ok and typing_ready:
        return {
            "readinessState": "ready",
            "readinessMessage": f"{product_name} is the active input source",
            "nextAction": f"start typing with {product_name}",
            "manualAction": "type in a foreground macOS text field",
            "verificationCommand": "scripts/wait_squirrel_typing_ready.sh",
            "readinessChecks": readiness_checks,
        }
    if enabled and selectable and third_party_enabled:
        return {
            "readinessState": "switch",
            "readinessMessage": f"{product_name} is installed; switch the menu bar input source",
            "nextAction": f"select {product_name} from the macOS input menu",
            "manualAction": f"macOS input menu -> {target_name}",
            "verificationCommand": "scripts/wait_squirrel_typing_ready.sh",
            "readinessChecks": readiness_checks,
            "expectedInputSourceId": target,
            "currentInputSourceId": current,
        }
    if not enabled or not selectable or not third_party_enabled:
        return {
            "readinessState": "install",
            "readinessMessage": f"{product_name} is not enabled in every macOS input-source list",
            "nextAction": f"add {product_name} in System Settings, then wait for the add gate",
            "manualAction": f"System Settings -> Keyboard -> Input Sources -> Add -> Chinese, Simplified -> {target_name}",
            "helperCommand": "scripts/open_squirrel_input_source_settings.sh --wait",
            "verificationCommand": "scripts/wait_squirrel_input_source_added.sh",
            "readinessChecks": readiness_checks,
            "expectedInputSourceId": target,
            "currentInputSourceId": current,
        }
    return {
        "readinessState": "error" if not ok else "waiting",
        "readinessMessage": "input source state is incomplete",
        "nextAction": "run doctor_squirrel_integration.sh",
        "manualAction": "inspect the input source checker output",
        "verificationCommand": f"scripts/check_macos_input_source.sh {target}",
        "readinessChecks": readiness_checks,
        "expectedInputSourceId": target,
        "currentInputSourceId": current,
    }


def _input_source_display_name(input_source_id: str) -> str:
    if input_source_id.endswith(".Hant"):
        suffix = "Traditional"
    else:
        suffix = "Simplified"
    if "RagIme" in input_source_id or "rag-ime" in input_source_id:
        return f"RAG-IME - {suffix}"
    return f"Squirrel - {suffix}"


def _input_source_product_name(display_name: str) -> str:
    for suffix in (" - Simplified", " - Traditional"):
        if display_name.endswith(suffix):
            return display_name[: -len(suffix)]
    return display_name


def _deepseek_evidence_from_suggestion(suggestion: InputSuggestion) -> dict[str, object]:
    metadata = dict(suggestion.metadata)
    tags = metadata.get("tags")
    return {
        "sourceType": _string(metadata.get("source_type") or "rag"),
        "title": _string(metadata.get("title") or metadata.get("bookTitle")),
        "surfaceHints": [compact_whitespace(suggestion.surface_text)],
        "evidencePreview": compact_whitespace(suggestion.evidence_preview or suggestion.expanded_evidence),
        "confidence": suggestion.confidence,
        "memoryId": _string(metadata.get("memory_id") or suggestion.suggestion_id),
        "sourceEventId": suggestion.source_event_id,
        "tags": [str(tag) for tag in tags[:8]] if isinstance(tags, (list, tuple)) else [],
    }


def _cache_stats_delta(before: object, after: object) -> dict[str, object]:
    if not isinstance(after, dict):
        return {"available": False, "enabled": False}
    before_stats = before if isinstance(before, dict) else {}
    numeric_keys = ("hits", "misses", "inFlightHits", "evictions", "invalidations")
    payload: dict[str, object] = {
        "available": True,
        "enabled": bool(after.get("enabled", True)),
        "before": {
            key: before_stats.get(key)
            for key in ("hits", "misses", "hitRate", "size", "inFlightHits", "evictions", "invalidations")
            if key in before_stats
        },
        "after": {
            key: after.get(key)
            for key in ("hits", "misses", "hitRate", "size", "inFlightHits", "evictions", "invalidations")
            if key in after
        },
    }
    for key in numeric_keys:
        before_value = before_stats.get(key, 0)
        after_value = after.get(key, 0)
        if isinstance(before_value, int) and isinstance(after_value, int):
            payload[f"{key}Delta"] = after_value - before_value
    return payload


def _cache_delta_passed(delta: dict[str, object], expected_warm_hits: int) -> bool | None:
    if not delta.get("available"):
        return None
    if delta.get("enabled") is False:
        return None
    hits_delta = delta.get("hitsDelta")
    if not isinstance(hits_delta, int):
        return None
    return hits_delta >= expected_warm_hits


def _canonical_action(value: str) -> str:
    aliases = {"accepted": "accepted", "accept": "accepted", "skipped": "skipped", "skip": "skipped"}
    action = aliases.get(value, value)
    allowed = {"accepted", "skipped", "pin", "unpin", "downrank", "delete", "hide", "restore"}
    if action not in allowed:
        raise ValueError(f"unsupported actionType: {value}")
    return action
