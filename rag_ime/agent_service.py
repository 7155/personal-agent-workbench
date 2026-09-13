from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import RLock

from .db import sqlite_connection
from .agent_capability_catalog import capability_disclosure_enabled, session_resource_disclosure_policy
from .agent_composition import build_room_stores, build_session_applications
from .agent_configuration import (
    AgentConfigurationStore,
    AgentControlEventHub,
    default_agent_configuration,
    runtime_policy_from_configuration,
)
from .agent_skill_routing import (
    TRACE_AGENT_OWNER_APP_ID,
    TRACE_AGENT_SURFACE_KEYS,
    skill_allowlist_for_session,
)
from .agent_prompt_settings import prompt_settings_catalog
from .agent_approval_application import AgentApprovalApplicationService
from .agent_approval_model import ApprovalModelArbiter
from .agent_background_jobs import AgentBackgroundJobService
from .agent_context_runtime import AgentContextRuntime
from .agent_execution_policy import (
    FULL_TRUST_EXECUTION_MODE,
    READ_ONLY_EXECUTION_MODE,
    ROOM_UNRESTRICTED_EXECUTION_MODE,
    auto_approve_policy_active,
    execution_policy_prompt,
    read_only_policy_active,
    workspace_scope_sha256,
)
from .room_permission_policy import (
    normalize_room_permission_policy,
    resolve_room_permission_policy,
)
from .work_documents import WorkDocumentService
from .agent_command_receipts import (
    AgentCommandReceiptStore,
    AgentTurnConflictError,
)
from .agent_events import AgentEventHub
from .agent_event_projection import AgentEventProjectionService
from .agent_block_store import AgentBlockStore
from .agent_delegation import AgentDelegationCoordinator
from .agent_file_preview import AgentFilePreviewReader
from .agent_media import AgentMediaStore, IMAGE_MIME_TYPES, TEXT_MEDIA_MIME_TYPES
from .agent_memory_context import AgentMemoryContextService
from .agent_memory_context_support import (
    compaction_summary as _compaction_summary,
)
from .agent_memory_evidence import AgentMemoryEvidenceService
from .agent_message_snapshot import AgentMessageSnapshotService
from .agent_memory_sources import AgentMemorySourceStore
from .agent_personas import AgentPersonaStore
from .agent_prompt_application import AgentPromptApplicationService
from .agent_prompt_delivery import AgentPromptDeliveryService
from .agent_prompt_support import (
    optional_client_message_id as _optional_client_message_id,
)
from .agent_protocol import AgentEventEnvelope
from .agent_role_book import AgentRoleBookStore
from .agent_role_application import AgentRoleApplicationService
from .agent_session_mode_gate import AgentSessionModeGate
from rag_ime.rooms.intercom import (
    AgentRoomIntercomRouter,
    AgentRoomIntercomStore,
)
from rag_ime.rooms.intercom_application import (
    RoomIntercomApplicationService,
)
from rag_ime.rooms.session_dispatch import RoomSessionDispatchService
from rag_ime.rooms.session_cancellation import RoomSessionCancellationService
from rag_ime.rooms.management import RoomManagementService
from rag_ime.rooms.partner_application import (
    RoomPartnerApplicationService,
)
from rag_ime.rooms.prompt_context import (
    agent_message_text as _agent_message_text,
    room_intercom_prompt as _room_intercom_prompt,
    room_participant_prompt as _room_participant_prompt,
)
from .collaboration_profile_control import CollaborationProfileControl
from rag_ime.rooms.work_application import RoomWorkApplicationService
from rag_ime.rooms.turn_registry import (
    RoomSessionBusyError,
    RoomTurnRegistry,
)
from .agent_governance_projection import GovernanceProjectionStore
from .agent_knowledge_promotion import KNOWLEDGE_ROUTE_HASH, KnowledgePromotionStore
from .knowledge_scope import bound_session_knowledge_caller
from .agent_runtime_driver import (
    AgentRuntimePolicy,
    AgentRuntimeDriver,
    RuntimeDriverContext,
    RuntimeDriverFactory,
    ToolManifestProvider,
)
from rag_ime.rooms.store import AgentRoomEventHub, AgentRoomNotFound
from .agent_roles import PersonaManifest
from .agent_sessions import AgentSessionStore
from .agent_wake_scheduler import AgentWakeScheduleStore, AgentWakeScheduler
from .agent_wake_application import AgentWakeApplicationService
from .contracts.json_schema import validate_contract
from .embeddings import EmbeddingProvider
from .eval_run_store import EvalRunStore
from .ai_judge_eval import (
    AI_JUDGE_RUBRIC_VERSION,
    AiJudgeOutputError,
    build_ai_judge_prompt,
    effective_ai_judge_evaluator,
    parse_ai_judge_metrics,
)
from .eval_schedule_store import (
    EvalScheduleExecutionError,
    EvalScheduleRunner,
    EvalScheduleStore,
)
from .evidence_eval import evaluate_evidence_ground_truth
from .observability import ObservationHub
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.factory import PiRuntimeDriverFactory
from .personal_context import (
    AgentMemoryEvidenceStore,
    PersonalContextConsolidator,
)
from .memory_maintenance_settings import memory_enabled_from_settings
from .session_memory_recall import SessionMemoryRecallBuilder
from .text_utils import compact_whitespace
from .trace_adapters import envelope_from_observations
from .trace_runtime import (
    TraceContractError,
    TraceEnvelope,
    build_eval_run,
    build_trace_envelope,
    validate_sandbox_run,
    validate_trace_envelope,
)
from .trace_store import TraceStore
from .trace_optimization import TraceOptimizationStore
from .trace_optimization_versions import TraceOptimizationVersionStore
from .trace_diagnostics import (
    TraceDiagnosticReportStore,
    extract_trace_diagnostic_result,
    inspect_trace_targets,
)
from .trace_repair import (
    TraceRepairStore,
    TraceRepairValidationError,
    derive_repair_evidence,
    run_ai_judge_recheck,
)
from .trace_replay_verification import (
    TraceReplayVerificationStore,
    TraceVerificationValidationError,
)
from .sandbox_run_store import SandboxRunStore
from .agent_lab.scene_recipes import AgentLabSceneRecipeStore
from .agent_lab.trial_execution import AgentLabTrialApplication, TrialAdapter
from .agent_lab.trials import AgentLabTrialServiceUnavailable, AgentLabTrialStore
from .eval_lab import EvalLabProjection
from .eval_lab_evidence import EvalLabEvidenceProjection
from .vertical_agent_suite import (
    BuiltinVerticalSuiteError,
    run_builtin_vertical_agent_eval,
)
from .vertical_agent_harness import list_builtin_eval_suites

ROOM_CONTEXT_UNREAD_MESSAGE_LIMIT = 12
ROOM_CONTEXT_HISTORY_CHAR_BUDGET = 3_600
ROOM_CONTEXT_PROMPT_CHAR_BUDGET = 24_000
ROOM_MESSAGE_CHAR_LIMIT = 8_000
# Ordinary Goal recovery allows four native follow-up opportunities, followed
# by one final settle decision that must stop the cancel scope.
GOAL_SETTLE_ATTEMPT_LIMIT = 5
GOAL_CONTINUATION_LIMIT = 4
_SCHEDULE_RUN_TRACE_ID_LIMIT = 64


class AgentService:
    def __init__(
        self,
        *,
        db_path: str | Path,
        runtime_config: PiRuntimeConfig | None = None,
        runtime_factory: RuntimeDriverFactory | None = None,
        configuration_defaults: Mapping[str, object] | None = None,
        project: str = "",
        process_id_provider: Callable[[], int] = os.getpid,
        tool_gateway_url: str = "http://127.0.0.1:8766/api/agent/tool/execute",
        tool_gateway_token: str = "",
        memory_embedding_provider: EmbeddingProvider | None = None,
        wake_scheduler_enabled: bool = False,
        wake_scheduler_poll_seconds: float = 1.0,
        background_job_execution_owner: bool = True,
        eval_lab_trial_adapters: Mapping[str, TrialAdapter] | None = None,
        eval_schedule_executor: (
            Callable[[Mapping[str, object]], Mapping[str, object]] | None
        ) = None,
        trace_store: TraceStore | None = None,
        sandbox_run_store: SandboxRunStore | None = None,
        collaboration_profile_signers: Mapping[str, bytes] | None = None,
        startup_recovery_enabled: bool = True,
        defer_startup_recovery: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.project = str(project or "")
        self._startup_recovery_enabled = bool(startup_recovery_enabled)
        self._startup_recovery_run_lock = RLock()
        self._startup_recovery_status_lock = RLock()
        self._startup_recovery_report: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-startup-recovery.v1",
            "enabled": self._startup_recovery_enabled,
            "status": "pending" if self._startup_recovery_enabled else "complete",
            "ok": not self._startup_recovery_enabled,
            "error": "",
            **(
                {}
                if self._startup_recovery_enabled
                else {"skippedReason": "not_execution_owner"}
            ),
        }
        self.personas = AgentPersonaStore(db_path)
        self.personas.initialize()
        self.role_books = AgentRoleBookStore(db_path)
        self.role_books.initialize()
        self.tool_token = str(tool_gateway_token or secrets.token_urlsafe(32))
        self.plugin_approval_token = secrets.token_urlsafe(32)
        self.tool_gateway_url = str(tool_gateway_url).strip()
        if not self.tool_gateway_url:
            raise ValueError("tool gateway URL must not be empty")
        self._tool_manifest_provider: ToolManifestProvider | None = None
        self._external_trace_resolvers: list[
            Callable[[str], TraceEnvelope | None]
        ] = []
        # Eval schedules use the existing Gateway wake poller.  The executor
        # is intentionally injected: creating a schedule never grants access
        # to Provider, Memory, or Knowledge state by itself.
        self._eval_schedule_executor = eval_schedule_executor
        configured = replace(
            runtime_config or PiRuntimeConfig.from_environment(),
            tool_gateway_token=self.tool_token,
            plugin_approval_token=self.plugin_approval_token,
            tool_gateway_url=self.tool_gateway_url,
            role_resolver=self.personas.resolve,
            role_book_resolver=self.role_books.prompt_block,
        )
        if runtime_factory is None:
            self.runtime_factory = PiRuntimeDriverFactory(configured)
        else:
            # Product-owned per-process capabilities (tool gateway and plugin
            # approval tokens) are minted above.  A caller-supplied factory
            # must receive that completed configuration before it creates the
            # long-lived Runtime Host; otherwise validation can work while the
            # guarded install step is permanently disabled.
            runtime_factory.reconfigure(configured)
            self.runtime_factory = runtime_factory
        self.sessions = AgentSessionStore(db_path, persistent_reads=True)
        self.sessions.initialize()
        self._eval_lab_golden_lock = RLock()
        self._eval_lab_golden_store = None
        self._eval_lab_golden_application = None
        self._eval_lab_golden_execution_owner = background_job_execution_owner
        self._eval_lab_project_lock = RLock()
        self._eval_lab_project_application = None
        self._eval_lab_app_lock = RLock()
        self._eval_lab_app_application = None
        self._eval_lab_app_research_application = None
        self._eval_lab_trial_lock = RLock()
        self._eval_lab_trial_store: AgentLabTrialStore | None = None
        self._eval_lab_trial_application: AgentLabTrialApplication | None = None
        self._eval_lab_trial_execution_owner = background_job_execution_owner
        self._eval_lab_trial_closed = False
        self._eval_lab_knowledge_resource = None
        self._lab_knowledge_client = None
        self._lab_knowledge_settings = lambda: {}
        self._eval_lab_trial_adapters = (
            self._default_trial_adapters()
            if eval_lab_trial_adapters is None
            else dict(eval_lab_trial_adapters)
        )
        # The checked-in public ledger is a read-only projection source for the
        # Agent Lab page.  Explicit import remains append-only persistence;
        # this avoids hiding newly recorded cards when an external demo DB is
        # older than the current source checkout.
        source_ledger = Path(__file__).resolve().parents[1] / "eval/interview-metrics/agent-experiments.v1.json"
        self.eval_lab = EvalLabProjection(db_path, source_ledger_path=source_ledger)
        self.eval_lab_scene_recipe_store = AgentLabSceneRecipeStore(
            db_path,
            experiment_provider=lambda: self.eval_lab.list_runs()["experiments"],
        )
        # The evidence catalog is a read-only view over the optional
        # source-local evaluation archive.  It never joins the archive into
        # ordinary Agent Sessions and never starts a Provider; the App asks for
        # one bounded transcript only when the user opens it.
        self.eval_lab_evidence = EvalLabEvidenceProjection()
        self.context_runtime = AgentContextRuntime(db_path)
        self.context_runtime.initialize()
        self.command_receipts = AgentCommandReceiptStore(db_path)
        self.command_receipts.initialize()
        seed_configuration = dict(
            configuration_defaults
            or default_agent_configuration(
                enabled=configured.enabled,
                idle_timeout_seconds=configured.idle_timeout_seconds,
            )
        )
        self.configuration_store = AgentConfigurationStore(
            db_path,
            persistent_reads=True,
        )
        self.configuration_store.initialize(seed_configuration)
        self.control_events = AgentControlEventHub(self.configuration_store)
        self._configuration_lock = RLock()
        self._context_source_token = object()
        self.session_mode_gate = AgentSessionModeGate()
        self.room_turns = RoomTurnRegistry()
        self.runtime_factory.apply_policy(
            runtime_policy_from_configuration(
                self.configuration_store.snapshot()["configuration"]
            )
        )
        self.media = AgentMediaStore(db_path)
        self.media.initialize()
        self.file_previews = AgentFilePreviewReader(self.media)
        self.agent_blocks = AgentBlockStore(db_path)
        self.agent_blocks.initialize()
        self.memory_sources = AgentMemorySourceStore(db_path, project=project)
        self.memory_sources.initialize()
        self.memory_evidence = AgentMemoryEvidenceStore(db_path, project=self.project)
        self.memory_evidence.initialize()
        self.memory_bootstrap = SessionMemoryRecallBuilder(
            db_path,
            project=self.project,
            embedding_provider=memory_embedding_provider,
        )
        self.memory_bootstrap.initialize()
        self.personal_context = PersonalContextConsolidator(
            db_path,
            project=self.project,
            role_book_applier=self.role_books,
        )
        self.personal_context.initialize()
        room_stores = build_room_stores(db_path, session_root=self.runtime_factory.session_root)
        self.rooms = room_stores.rooms
        self.room_start_gates = room_stores.start_gates
        self.room_work = room_stores.work
        self.room_partner_dispatches = room_stores.partner_dispatches
        self.governance_projection = GovernanceProjectionStore(db_path)
        self.governance_projection.initialize()
        self.knowledge_promotion = KnowledgePromotionStore(
            db_path,
            observation_callback=(
                lambda record: self.observations.enqueue_knowledge_retrieval_record(record)
            ),
        )
        self.knowledge_promotion.initialize()
        self._collaboration_profile_signers = {
            str(signer_id): bytes(key)
            for signer_id, key in (collaboration_profile_signers or {}).items()
        }
        self.observations = ObservationHub(db_path)
        self.trace_store = trace_store or TraceStore(db_path)
        initialize_trace_store = getattr(self.trace_store, "initialize", None)
        if callable(initialize_trace_store):
            initialize_trace_store()
        self.eval_runs = EvalRunStore(db_path)
        self.eval_runs.initialize()
        self.trace_optimization_versions = TraceOptimizationVersionStore(db_path)
        self.trace_optimizations = TraceOptimizationStore(
            db_path,
            version_reader=self.trace_optimization_versions.resolve,
            application_reader=self.trace_optimization_versions.application_receipt,
        )
        self._trace_optimization_app = None
        self._trace_optimization_lock = RLock()
        self._trace_optimization_extensions = None
        self._trace_optimization_capability_reader = None
        self.trace_diagnostic_reports = TraceDiagnosticReportStore(
            db_path, optimization_store=self.trace_optimizations,
        )
        self.trace_diagnostic_reports.initialize()
        # Trace repair evidence and receipts are a Runtime-owned authority,
        # separate from the bounded Observation journal.  The HTTP surface
        # below only accepts opaque IDs returned by this store and derives
        # every recheck binding from the persisted receipt.
        self.trace_repairs = TraceRepairStore(db_path)
        self.trace_repairs.initialize()
        self._trace_repair_recheck_lock = RLock()
        self.sandbox_runs = sandbox_run_store or SandboxRunStore(db_path)
        self.sandbox_runs.initialize()
        self.trace_replay_verifications = TraceReplayVerificationStore(db_path)
        self.trace_replay_verifications.initialize()
        if self._eval_schedule_executor is None:
            self._eval_schedule_executor = self._run_builtin_eval_schedule
        self.room_events = AgentRoomEventHub(self.rooms)
        self._remove_observation_room_observer = self.room_events.add_observer(
            self.observations.enqueue_room_event
        )
        self.events = AgentEventHub(
            sequence_loader=self.sessions.max_event_sequence,
            event_recorder=self._record_event,
            event_observer=self._mirror_event_to_room,
            background_projection=True,
        )
        self.background_jobs = AgentBackgroundJobService(
            db_path,
            events=self.events.publish,
            execution_owner=background_job_execution_owner,
        )
        if background_job_execution_owner and startup_recovery_enabled:
            self._golden_store().recover_interrupted_jobs()
            self._trial_store().recover_interrupted()
        self.work_documents = WorkDocumentService(
            db_path,
            sessions=self.sessions,
            context_runtime=self.context_runtime,
        )
        self.work_documents.initialize(reconcile=False)
        self.room_work.set_terminal_observer(
            self.work_documents.observe_authority
        )
        self.runtime: AgentRuntimeDriver = self.runtime_factory.create(
            RuntimeDriverContext(
                sessions=self.sessions,
                events=self.events,
                media_resolver=self.media.resolve_pi_image,
                tool_gateway_token=self.tool_token,
                tool_gateway_url=self.tool_gateway_url,
                tool_manifest_provider=self._runtime_tool_manifest,
                skill_allowlist_provider=self._runtime_skill_allowlist,
                candidate_skill_paths_provider=self._runtime_candidate_skill_paths,
                compaction_observer=self._checkpoint_runtime_compaction,
                prompt_settings_provider=self._runtime_prompt_settings,
            ),
            purpose="interactive",
            session_context_provider=self._runtime_session_context,
        )
        self.approval_model = ApprovalModelArbiter(
            db_path,
            runtime_provider=lambda: self.runtime,
            context_provider=self._approval_model_context,
        )
        self.role_application = AgentRoleApplicationService(
            personas=self.personas,
            runtime_provider=lambda: self.runtime,
            runtime_factory=self.runtime_factory,
            default_model_profile_provider=lambda: str(
                self.configuration_store.snapshot()["configuration"]["sessionDefaults"]["modelProfile"]
            ),
        )
        initial_configuration = self.configuration_store.snapshot()
        if (
            initial_configuration["sync"]["state"] != "synchronized"
            or initial_configuration["sync"]["appliedRevision"]
            != initial_configuration["revision"]
        ):
            _, reconciled_event = self.configuration_store.mark_applied(
                int(initial_configuration["revision"]),
                runtime_status=self.runtime.runtime_status(),
            )
            self.control_events.fan_out(reconciled_event)
        self.delegation = AgentDelegationCoordinator(
            db_path=db_path,
            runtime_config=configured,
            sessions=self.sessions,
            context_runtime=self.context_runtime,
            events=self.events,
            media_resolver=self.media.resolve_pi_image,
            runtime_driver_factory=self.runtime_factory,
            runtime_provider=lambda: self.runtime,
            tool_gateway_token=self.tool_token,
            tool_gateway_url=self.tool_gateway_url,
            tool_manifest_provider=self._runtime_tool_manifest,
            compaction_observer=self._checkpoint_runtime_compaction,
            prompt_settings_provider=self._runtime_prompt_settings,
            room_context_provider=self._room_delegation_context,
            model_route_provider=self._configured_model_route,
            startup_recovery=False,
        )
        session_applications = build_session_applications(
            sessions=self.sessions, runtime_provider=lambda: self.runtime,
            runtime_factory=self.runtime_factory, configuration_store=self.configuration_store,
            rooms=self.rooms, delegation=self.delegation, media=self.media, events=self.events,
            command_receipts=self.command_receipts, runtime_status=lambda: self.runtime_status(),
            pending_memory_bootstrap=self._pending_memory_bootstrap,
            probe_memory_maintenance=lambda session_id, **kwargs: self._probe_memory_maintenance(session_id, **kwargs),
            prompt_with_checkpoint=lambda **kwargs: self.prompt_application.prompt_rewritten_session(**kwargs),
            cancel_pending_approvals=lambda session_id, **kwargs: self.approval_application.cancel_pending_for_session(session_id, **kwargs),
            recent_recall_messages=self._recent_recall_messages,
            checkpoint_runtime_compaction=self._checkpoint_runtime_compaction,
            refresh_session_context=self.refresh_session_context,
            public_error=_public_error,
        )
        self.session_application = session_applications.application
        self.session_policy = session_applications.policy
        self.session_branching = session_applications.branching
        self.task_context = session_applications.task_context
        self.memory_context_application = (
            AgentMemoryContextService(
                sessions=self.sessions,
                memory_bootstrap=self.memory_bootstrap,
                context_runtime=self.context_runtime,
                task_context=self.task_context,
                runtime_provider=lambda: self.runtime,
                observation_callback=self.observations.enqueue_memory_recall_record,
                memory_enabled_provider=self.memory_enabled,
                session_memory_enabled_provider=self._session_memory_disclosed,
            )
        )
        self.memory_evidence_application = (
            AgentMemoryEvidenceService(
                sessions=self.sessions,
                memory_evidence=self.memory_evidence,
                message_text=_agent_message_text,
                memory_enabled_provider=self.memory_enabled,
            )
        )
        self.prompt_delivery_application = (
            AgentPromptDeliveryService(
                sessions=self.sessions,
                context_runtime=self.context_runtime,
                runtime_provider=lambda: self.runtime,
                runtime_tool_manifest=lambda session: (
                    self._runtime_tool_manifest(session)
                ),
                room_public_recovery_context=(
                    self._room_public_recovery_context_for_session
                ),
                execution_policy_context=(
                    self._execution_policy_prompt_for_session
                ),
                memory_enabled_provider=self.memory_enabled,
                session_memory_enabled_provider=self._session_memory_disclosed,
            )
        )
        self.prompt_application = AgentPromptApplicationService(
            sessions=self.sessions,
            command_receipts=self.command_receipts,
            configuration_store=self.configuration_store,
            media=self.media,
            events=self.events,
            memory_sources=self.memory_sources,
            memory_context=self.memory_context_application,
            memory_evidence=self.memory_evidence_application,
            prompt_delivery_service=(
                self.prompt_delivery_application
            ),
            runtime_provider=lambda: self.runtime,
            runtime_status=lambda: self.runtime_status(),
            create_session=lambda payload: (
                self.create_session(payload)
            ),
            dispatch_checkpoint=lambda **kwargs: (
                self._prompt_with_checkpoint(**kwargs)
            ),
            context_source_token=self._context_source_token,
            transient_context_char_budget=(
                ROOM_CONTEXT_PROMPT_CHAR_BUDGET
            ),
        )
        self.event_projection_application = (
            AgentEventProjectionService(
                sessions=self.sessions,
                rooms=self.rooms,
                agent_blocks=self.agent_blocks,
                observations=self.observations,
                room_events=self.room_events,
                room_turns=self.room_turns,
                append_recent_message=(
                    lambda session_id, message: (
                        self._append_recent_recall_message(
                            session_id,
                            message,
                        )
                    )
                ),
                record_assistant_evidence=(
                    lambda event: (
                        self._record_assistant_evidence_safely(
                            event
                        )
                    )
                ),
                notify_intercom=lambda: (
                    getattr(self, "room_intercom", None)
                    and self.room_intercom.notify()
                ),
            )
        )
        # Recovered jobs can finish and publish synchronously from initialize().
        # Their durable event must never race the projection owner into existence.
        self.background_jobs.initialize()
        self.room_intercom_application = (
            RoomIntercomApplicationService(
                sessions=self.sessions,
                rooms=self.rooms,
                room_work=self.room_work,
                context_runtime=self.context_runtime,
                room_events=self.room_events,
                room_turns=self.room_turns,
                runtime_provider=lambda: self.runtime,
                # Read-only membership checks; the registry stays the only
                # mutation owner.
                user_priority_sessions=(
                    self.room_turns.user_priority_sessions
                ),
                turn_lock=self.room_turns.lock,
                guard_room_session_route=(
                    lambda route_id, session_id: (
                        self._guard_room_session_route(
                            route_id,
                            session_id,
                        )
                    )
                ),
                runtime_prompt_with_context=(
                    lambda session_id, message, **kwargs: (
                        self._runtime_prompt_with_context(
                            session_id,
                            message,
                            **kwargs,
                        )
                    )
                ),
                room_intercom_prompt=_room_intercom_prompt,
                room_participant_prompt=(
                    self._room_participant_prompt_with_documents
                ),
                publish_room_work_activity=(
                    lambda work, **kwargs: (
                        self._publish_room_work_activity(
                            work,
                            **kwargs,
                        )
                    )
                ),
            )
        )
        self.room_intercom = AgentRoomIntercomRouter(
            AgentRoomIntercomStore(db_path),
            generation_provider=self._room_runtime_generation,
            idle_probe=self._room_target_idle,
            delivery_handler=self._deliver_room_intercom,
            audit_publisher=self._publish_room_intercom_audit,
        )
        self._approval_executor: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None
        self._memory_maintenance_probe: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None
        self._process_id_provider = process_id_provider
        self.eval_schedules = EvalScheduleStore(db_path)
        self.eval_schedules.initialize()
        self.eval_schedule_runner = EvalScheduleRunner(
            store=self.eval_schedules,
            execute=self._execute_eval_schedule,
            eval_run_exists=self._eval_run_exists,
            eval_run_loader=self.eval_runs.get,
            max_parallel=1,
        )
        self.wake_schedules = AgentWakeScheduleStore(db_path)
        self.wake_schedules.initialize()
        self.wake_application = AgentWakeApplicationService(
            schedules=self.wake_schedules,
            sessions=self.sessions,
            personas=self.personas,
            rooms=self.rooms,
            context_runtime=self.context_runtime,
            events=self.events,
            create_session=lambda payload: self.create_session(payload),
            prompt=lambda session_id, payload: self.prompt(
                session_id,
                payload,
            ),
                guard_room_session_route=lambda route_id, session_id: (
                self._guard_room_session_route(route_id, session_id)
            ),
            context_source_token=self._context_source_token,
        )
        self.wake_scheduler = AgentWakeScheduler(
            store=self.wake_schedules,
            dispatch=self._dispatch_wake_claim,
            enabled=wake_scheduler_enabled,
            poll_seconds=wake_scheduler_poll_seconds,
            max_parallel=2,
            on_tick=self._run_eval_schedules_once,
        )
        self.wake_application.bind_scheduler(self.wake_scheduler)
        self._remove_wake_observer = self.events.add_observer(
            self.wake_scheduler.observe_event
        )
        self.room_dispatch = RoomSessionDispatchService(
            rooms=self.rooms, room_work=self.room_work, room_events=self.room_events,
            room_turns=self.room_turns, room_partner_dispatches=self.room_partner_dispatches,
            context_source_token=self._context_source_token,
            restore_participant_sessions=self._restore_room_participant_sessions,
            guard_session_route=self._guard_room_session_route,
            recover_faulted_session=self._recover_faulted_room_session,
            resume_goal_if_paused=self._resume_room_goal_if_paused,
            target_idle=lambda session_id, **kwargs: self._room_target_idle(session_id, **kwargs),
            record_room_evidence=self._record_room_evidence_safely,
            accept_turn=self._accept_room_turn,
            prompt=lambda session_id, payload: self.prompt(session_id, payload),
            build_participant_prompt=self._room_participant_prompt_with_documents,
            resolve_attachments=self._resolve_room_attachments,
        )
        self.room_cancellation = RoomSessionCancellationService(
            rooms=self.rooms, room_events=self.room_events,
            delegation=self.delegation, background_jobs=self.background_jobs,
            room_intercom=self.room_intercom, wake_schedules=self.wake_schedules,
            room_turns=self.room_turns,
            cancel_partner_root=lambda **kwargs: self.room_partner_application.cancel_root(**kwargs),
            abort_session=lambda session_id: self.abort(session_id),
        )
        self.room_partner_application = RoomPartnerApplicationService(
            rooms=self.rooms,
            room_turns=self.room_turns,
            runtime_status=self.runtime.runtime_status,
            sessions=self.sessions,
            room_events=self.room_events,
            room_target_idle=self._room_target_idle,
            begin_room_turn=self._begin_room_turn,
            room_dispatch=self.room_dispatch,
            cancel_room_turn=self._cancel_room_turn,
            abort_session=self.abort,
            room_topic_for_turn=self._room_topic_for_turn,
            send_room_intercom=lambda session_id, payload: self.room_work_application.send_room_intercom(
                session_id,
                payload,
            )["message"],
            list_room_intercom=lambda session_id: self.room_work_application.list_room_intercom(
                session_id,
                {"limit": 100},
            )["items"],
            room_work=self.room_work,
            publish_room_work_activity=lambda work, **kwargs: self._publish_room_work_activity(
                work,
                **kwargs,
            ),
            work_document_for_authority=self._work_document_for_authority,
            dispatch_store=self.room_partner_dispatches,
            wake_schedules=self.wake_schedules,
            notify_wake_scheduler=self.wake_scheduler.wake,
            dispatch_facilitator_wake=self._dispatch_room_partner_wake,
            command_acceptance_evidence=lambda session_id, client_message_id: (
                self.command_receipts.acceptance_evidence_for_exact_command(
                    command_scope="session_prompt",
                    scope_id=session_id,
                    client_message_id=client_message_id,
                )
            ),
            command_failure_evidence=lambda session_id, client_message_id: (
                self.command_receipts.failure_evidence_for_exact_command(
                    command_scope="session_prompt",
                    scope_id=session_id,
                    client_message_id=client_message_id,
                )
            ),
            accept_room_work=lambda session_id, payload: self.room_work_application.accept_room_work(
                session_id,
                payload,
            ),
            return_room_work=lambda session_id, payload: self.room_work_application.return_room_work(
                session_id,
                payload,
            ),
            add_room_participant=self.add_room_participant,
            remove_room_participant=self.remove_room_participant,
            recover_faulted_session=self._recover_faulted_room_session,
        )
        self.room_work_application = RoomWorkApplicationService(
            rooms=self.rooms,
            room_work=self.room_work,
            room_intercom=self.room_intercom,
            room_events=self.room_events,
            guard_session_route=self._guard_room_session_route,
        )
        self._remove_room_partner_observer = self.room_events.add_observer(
            self.room_partner_application.observe_room_event
        )
        self.wake_scheduler.bind_terminal_observer(
            self.room_partner_application.observe_wake_terminal_event
        )
        self.approval_application = AgentApprovalApplicationService(
            sessions=self.sessions, events=self.events, rooms=self.rooms,
            room_events=self.room_events, room_turns=self.room_turns,
            memory_sources=self.memory_sources, approval_model=self.approval_model,
            runtime_provider=lambda: self.runtime,
            executor_provider=lambda: self._approval_executor,
            process_id_provider=lambda: self._process_id_provider(),
            record_tool_receipt_evidence=lambda approval: self._record_tool_receipt_evidence_safely(approval),
            active_room_dispatch_context=lambda session_id: self._active_room_dispatch_context(session_id),
        )
        self.message_snapshot = AgentMessageSnapshotService(
            sessions=self.sessions,
            runtime_provider=lambda: self.runtime,
            workflow_projector=self.workflow_state,
            agent_blocks=self.agent_blocks,
            media=self.media,
            observations=self.observations,
            events=self.events,
            background_jobs=self.background_jobs,
            room_public_messages=(
                self._room_public_messages_for_session
            ),
            room_recent_public_messages=(
                self._recent_room_public_messages_for_session
            ),
        )
        # Report persistence is a backend lifecycle responsibility. The Trace
        # page may be unmounted while the full-trust diagnostic Session
        # finishes, so a terminal Runtime event must reconcile the bound
        # report without relying on a browser poller.
        self._remove_trace_diagnostic_observer = self.events.add_observer(
            self._observe_trace_diagnostic_terminal_event
        )
        self.room_management = RoomManagementService(
            rooms=self.rooms,
            sessions=self.sessions,
            personas=self.personas,
            events=self.room_events,
            create_session=lambda payload: (
                self.create_session(payload)
            ),
            delete_session=lambda session_id: (
                self.delete_session(session_id)
            ),
            close_session=self._close_runtime_session,
            runtime_status=lambda: self.runtime_status(),
            release_room_work=lambda room_id, participant_id, replacement_id, reason: (
                self.room_work.release_for_participant(
                    room_id,
                    participant_id,
                    replacement_participant_id=replacement_id,
                    reason=reason,
                )
            ),
            # Read-only busy checks under the shared lock; mutation stays
            # inside the registry.
            turn_lock=self.room_turns.lock,
            pending_turns=self.room_turns.pending_turn_by_session,
            user_priority_sessions=(
                self.room_turns.user_priority_sessions
            ),
        )
        if self._startup_recovery_enabled and not defer_startup_recovery:
            self.run_startup_recovery()

    def startup_recovery_status(self) -> dict[str, object]:
        with self._startup_recovery_status_lock:
            return dict(self._startup_recovery_report)

    def run_startup_recovery(self) -> None:
        """Replay noncritical durable recovery after schema owners exist."""

        if not self._startup_recovery_enabled:
            return
        with self._startup_recovery_run_lock:
            with self._startup_recovery_status_lock:
                if self._startup_recovery_report.get("status") == "complete":
                    return
                self._startup_recovery_report = {
                    **self._startup_recovery_report,
                    "status": "running",
                    "ok": False,
                    "error": "",
                }
            try:
                approval_execution_recovery = (
                    self.approval_application
                    .reconcile_abandoned_execution_claims()
                )
                retired_room_start_gate_count = (
                    self.room_start_gates.retire_pending()
                )
                room_cursor: Mapping[str, object] | None = None
                while True:
                    page = self.rooms.list_page(
                        include_archived=False,
                        limit=200,
                        before_updated_at_ms=(
                            int(room_cursor["beforeUpdatedAtMs"])
                            if room_cursor is not None
                            else None
                        ),
                        before_id=(
                            str(room_cursor["beforeId"])
                            if room_cursor is not None
                            else None
                        ),
                    )
                    for room in page["items"]:
                        self._activate_room_unrestricted_execution(
                            str(room.get("id") or ""),
                            room=room,
                        )
                    next_cursor = page.get("nextCursor")
                    if not isinstance(next_cursor, Mapping):
                        break
                    room_cursor = next_cursor
                self.work_documents.reconcile(retry_failed_observers=True)
                self.delegation.reconcile_startup()
                self.room_work.reconcile_intercom_outcomes()
                self.room_partner_application.reconcile()
            except Exception as exc:
                with self._startup_recovery_status_lock:
                    self._startup_recovery_report = {
                        **self._startup_recovery_report,
                        "status": "failed",
                        "ok": False,
                        "error": exc.__class__.__name__,
                    }
                raise
            with self._startup_recovery_status_lock:
                self._startup_recovery_report = {
                    **self._startup_recovery_report,
                    "status": "complete",
                    "ok": True,
                    "error": "",
                    "approvalExecutionRecovery": (
                        approval_execution_recovery
                    ),
                    "retiredRoomStartGateCount": (
                        retired_room_start_gate_count
                    ),
                }

    def _close_runtime_session(self, session_id: str) -> None:
        close_session = getattr(self.runtime, "close_session", None)
        if callable(close_session):
            close_session(session_id)

    def bind_approval_executor(
        self,
        executor: Callable[[Mapping[str, object]], Mapping[str, object]],
    ) -> None:
        self._approval_executor = executor

    def bind_memory_maintenance_probe(
        self,
        probe: Callable[[Mapping[str, object]], Mapping[str, object]],
    ) -> None:
        self._memory_maintenance_probe = probe

    def bind_eval_schedule_executor(
        self,
        executor: Callable[[Mapping[str, object]], Mapping[str, object]],
    ) -> None:
        """Bind the Runtime-owned evaluator used by the existing wake loop.

        The callback must persist its immutable EvalRun through ``eval_runs``
        and return only ``{"evalRunId": ...}`` (additional values are not
        persisted by the schedule ledger).  It must not use this seam to write
        production Memory/Knowledge or invoke a Provider implicitly.
        """

        if not callable(executor):
            raise TypeError("eval schedule executor must be callable")
        self._eval_schedule_executor = executor
        self.wake_scheduler.wake()

    def _execute_eval_schedule(
        self,
        claim: Mapping[str, object],
    ) -> Mapping[str, object]:
        executor = self._eval_schedule_executor
        if executor is None:
            raise EvalScheduleExecutionError("executor_unavailable")
        result = executor(claim)
        if not isinstance(result, Mapping):
            raise EvalScheduleExecutionError("executor_invalid_result")
        return result

    def _run_builtin_eval_schedule(
        self,
        claim: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Run one checked-in fixture suite in a managed temporary workspace."""

        try:
            with tempfile.TemporaryDirectory(prefix="paw-vertical-eval-") as workspace:
                return run_builtin_vertical_agent_eval(
                    claim.get("suiteId"),
                    claim.get("suiteRevision"),
                    workspace,
                    eval_store=self.eval_runs,
                    trace_store=getattr(self, "trace_store", None),
                    schedule_run_id=claim.get("runId"),
                    schedule_due_at_ms=claim.get("dueAtMs"),
                )
        except BuiltinVerticalSuiteError as exc:
            raise EvalScheduleExecutionError(exc.code) from exc

    def _eval_run_exists(self, eval_run_id: str) -> bool:
        return self.eval_runs.get(eval_run_id) is not None

    def _run_eval_schedules_once(self, now_ms: int | None = None) -> int:
        """Advance due Eval schedules from the existing wake scheduler tick."""

        if self._eval_schedule_executor is None:
            return 0
        return self.eval_schedule_runner.run_due_once(now_ms=now_ms)

    def bind_tool_manifest_provider(self, provider: ToolManifestProvider) -> None:
        """Bind the backend-owned tool catalog without exposing gateway credentials."""

        self._tool_manifest_provider = provider

    def bind_extension_app_skill_owners(
        self,
        provider: Callable[[], Mapping[str, str]],
    ) -> None:
        """Scope packaged App Skills to their owning Session surface."""

        self.session_policy.bind_extension_app_skill_owners(provider)

    def bind_external_trace_resolver(
        self,
        resolver: Callable[[str], TraceEnvelope | None],
    ) -> None:
        """Bind an authority-preserving adapter for traces outside the journal."""

        self._external_trace_resolvers.append(resolver)

    def list_context_items(
        self,
        session_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self.sessions.get(session_id)
        value = dict(payload or {})
        items = self.context_runtime.list_items(
            session_id,
            status=str(value.get("status") or ""),
            limit=_integer(value.get("limit"), default=100, minimum=1, maximum=500),
        )
        return {
            "schemaVersion": "rag-ime.agent-context-inbox.v1",
            "ok": True,
            "sessionId": session_id,
            "items": items,
        }

    def acknowledge_context_item(
        self,
        session_id: str,
        item_id: str,
    ) -> dict[str, object]:
        self.sessions.get(session_id)
        return {
            "schemaVersion": "rag-ime.agent-context-item-ack.v1",
            "ok": True,
            "sessionId": session_id,
            "item": self.context_runtime.acknowledge(session_id, item_id),
        }

    def list_context_traces(
        self,
        session_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self.sessions.get(session_id)
        value = dict(payload or {})
        return {
            "schemaVersion": "rag-ime.agent-context-trace-list.v1",
            "ok": True,
            "sessionId": session_id,
            "items": self.context_runtime.list_traces(
                session_id,
                limit=_integer(value.get("limit"), default=30, minimum=1, maximum=100),
            ),
        }

    def context_trace(self, session_id: str, trace_id: str) -> dict[str, object]:
        self.sessions.get(session_id)
        trace = self.context_runtime.trace(trace_id)
        if trace["sessionId"] != session_id:
            raise KeyError(trace_id)
        return trace

    def debug_context(self, session_id: str, turn_id: str = "") -> dict[str, object]:
        self.sessions.get(session_id)
        provider = getattr(self.runtime, "debug_context", None)
        if not callable(provider):
            return {
                "schemaVersion": "rag-ime.pi-debug-context-response.v1",
                "sessionId": session_id,
                "turnId": str(turn_id or ""),
                "available": False,
                "transient": True,
                "context": None,
                "telemetry": None,
            }
        return dict(provider(session_id, str(turn_id or "")))

    def _runtime_tool_manifest(
        self,
        session: Mapping[str, object],
    ) -> list[Mapping[str, object]]:
        session_id = str(session.get("id") or "").strip()
        if session_id:
            # Prompt/runtime callbacks may retain an earlier Session mapping.
            # The durable lease policy is authoritative for this disclosure.
            session = self.sessions.get(session_id)
        provider = self._tool_manifest_provider
        if provider is None:
            return []
        return [dict(item) for item in provider(session)]

    def _runtime_skill_allowlist(
        self,
        session: Mapping[str, object],
    ) -> list[str]:
        session_id = str(session.get("id") or "").strip()
        if session_id:
            session = self.sessions.get(session_id)
        optimization_policy = self._optimization_application().session_policy(session)
        if optimization_policy is not None:
            return list(optimization_policy["skillIds"])
        if (
            session_id
            and not str(session.get("ownerAppId") or "").strip()
            and self.trace_diagnostic_reports.owns_session(session_id)
        ):
            # Reports created before Trace Agent Sessions carried durable
            # surface ownership remain isolated after this clean cutover.
            session = {
                **session,
                "surfaceKind": "extension_app",
                "ownerAppId": TRACE_AGENT_OWNER_APP_ID,
                "surfaceKey": "diagnostic",
            }
        participant = (
            self.rooms.participant_for_session(
                session_id,
                active_only=False,
            )
            if session_id
            else None
        )
        configuration = self.configuration_store.snapshot()["configuration"]
        return skill_allowlist_for_session(
            configuration,
            session,
            room_participant=isinstance(participant, Mapping),
        )



    def _approval_model_context(
        self,
        approval: Mapping[str, object],
        session: Mapping[str, object],
    ) -> dict[str, object]:
        """Project user/task evidence without any primary-Agent output."""

        session_id = str(
            approval.get("sessionId") or session.get("id") or ""
        )
        participant = self.rooms.participant_for_session(
            session_id,
            active_only=False,
        )
        room_id = (
            str(participant.get("roomId") or "")
            if isinstance(participant, Mapping)
            else ""
        )
        if room_id:
            requests: list[dict[str, object]] = []
            for event in self.rooms.list_events(room_id, limit=500):
                if str(event.get("eventType") or "") != "user_message":
                    continue
                payload = (
                    event.get("payload")
                    if isinstance(event.get("payload"), Mapping)
                    else {}
                )
                text = str(payload.get("text") or "").strip()
                if not text:
                    continue
                requests.append(
                    {
                        "role": "user",
                        "text": text,
                        "turnId": str(event.get("turnId") or ""),
                        "createdAtMs": int(event.get("createdAtMs") or 0),
                    }
                )
            task = self.task_context.resolve(session_id)
            root_id, dispatch_id = self.room_turns.active_turn(session_id)
            task_context = {
                "kind": str(task.get("kind") or "room_task"),
                "roomId": room_id,
                "rootId": root_id,
                "dispatchId": dispatch_id,
                "taskId": str(task.get("workItemId") or ""),
                "state": str(task.get("state") or ""),
                "objective": str(task.get("objective") or ""),
                "expectedOutput": str(task.get("expectedOutput") or ""),
            }
            return {
                "contextKind": "room",
                "contextAvailable": bool(requests),
                "contextId": room_id,
                "userRequests": requests[-8:],
                "currentTask": task_context,
                "actor": {
                    "sessionId": session_id,
                    "roomId": room_id,
                    "participantId": (
                        str(participant.get("id") or "")
                        if isinstance(participant, Mapping)
                        else ""
                    ),
                    "dispatchId": (
                        dispatch_id
                    ),
                },
            }

        message_snapshot = self.message_snapshot.messages(session_id)
        snapshot_messages = message_snapshot.get("items")
        if not isinstance(snapshot_messages, list):
            # Older in-process projections used ``messages``; the canonical
            # snapshot contract is ``items``. Keep context assembly tolerant
            # while reading the authoritative snapshot, not Provider output.
            snapshot_messages = message_snapshot.get("messages")
        requests: list[dict[str, object]] = []
        for message in list(snapshot_messages or []):
            if not isinstance(message, Mapping):
                continue
            if str(message.get("role") or "") != "user":
                continue
            text = _approval_user_request_text(message)
            if not text:
                continue
            requests.append(
                {
                    "role": "user",
                    "text": text,
                    "turnId": str(
                        message.get("turnId") or message.get("id") or ""
                    ),
                    "createdAtMs": int(message.get("createdAtMs") or 0),
                }
            )
        if not requests:
            # Pi compaction owns the Provider history and may leave the current
            # UI snapshot without an original user message. Do not ask Luna to
            # judge from an assistant-authored summary; recover only bounded,
            # immutable ``user_final`` checkpoints from this same Session.
            requests.extend(
                self.memory_sources.recent_user_requests(session_id, limit=8)
            )
        workflow = self.sessions.workflow_state(session_id)
        goal = (
            workflow.get("goal")
            if isinstance(workflow.get("goal"), Mapping)
            else {}
        )
        todo = (
            workflow.get("todo")
            if isinstance(workflow.get("todo"), Mapping)
            else {}
        )
        current_task = {
            "kind": "agent_workflow",
            "activeUserRequest": (
                str(requests[-1]["text"]) if requests else ""
            ),
            "goal": (
                {
                    "goalId": str(goal.get("goalId") or ""),
                    "status": str(goal.get("status") or ""),
                    "objective": str(goal.get("objective") or ""),
                    "successCriteria": str(
                        goal.get("successCriteria") or ""
                    ),
                    "evidenceExpectations": list(
                        goal.get("evidenceExpectations") or []
                    ),
                }
                if goal.get("configured") is True
                else {}
            ),
            "todo": {
                "phases": [
                    {
                        "name": str(phase.get("name") or ""),
                        "tasks": [
                            {
                                "content": str(item.get("content") or ""),
                                "status": str(item.get("status") or ""),
                            }
                            for item in list(phase.get("tasks") or [])
                            if isinstance(item, Mapping)
                        ],
                    }
                    for phase in list(todo.get("phases") or [])
                    if isinstance(phase, Mapping)
                ],
            },
        }
        return {
            "contextKind": "session",
            "contextAvailable": bool(requests),
            "contextId": session_id,
            "userRequests": requests[-8:],
            "currentTask": current_task,
            "actor": {"sessionId": session_id},
        }



    def _room_public_recovery_context_for_session(
        self,
        session_id: str,
    ) -> str:
        # Room partners are ordinary Pi Sessions. Pi's own compaction summary
        # is the recovery source; there is no second Kernel recovery packet.
        return ""

    def _room_participant_prompt_with_documents(
        self,
        room: Mapping[str, object],
        target: Mapping[str, object],
        message: str,
        **kwargs: object,
    ) -> str:
        """Add a soft, recoverable WorkItem-to-document index to Room context.

        WorkDocument remains the registry owner and Room WorkItem remains the
        responsibility owner.  The prompt only tells a participant where the
        current documents live; it does not copy document bodies or create a
        second context store.
        """
        try:
            documents = self.work_documents.context_discovery(limit=200)["items"]
        except Exception:
            # Document discovery is advisory. A registry/read failure must not
            # turn an otherwise valid Pi Session dispatch into a false failure.
            documents = []
        document_authorities: dict[str, Mapping[str, object]] = {}
        work_candidates = [
            value
            for value in room.get("workItems", [])
            if isinstance(value, Mapping)
        ]
        explicit_work_item = kwargs.get("work_item")
        if isinstance(explicit_work_item, Mapping):
            explicit_id = str(explicit_work_item.get("id") or "")
            if explicit_id and all(
                str(value.get("id") or "") != explicit_id
                for value in work_candidates
            ):
                # A delegate is created after the caller captured its Room
                # snapshot. The explicit WorkItem is authoritative for this
                # dispatch and must still receive its current document receipt.
                work_candidates.append(explicit_work_item)
        for value in work_candidates:
            work_id = str(value.get("id") or "").strip()
            if not work_id:
                continue
            try:
                authority = self.work_documents.authority_context(
                    "room_work_item",
                    work_id,
                )
            except Exception:
                # Like document discovery, the hint is recoverable and must
                # never become a second dispatch gate.
                continue
            document_authorities[str(authority["authorityKey"])] = authority
        return _room_participant_prompt(
            room,
            target,
            message,
            work_documents=documents,
            work_document_authorities=document_authorities,
            **kwargs,
        )

    def _work_document_for_authority(
        self,
        authority_kind: str,
        authority_id: str,
    ) -> Mapping[str, object] | None:
        authority_key = f"{authority_kind}:{authority_id}"
        try:
            documents = self.work_documents.list(limit=500)["items"]
        except Exception:
            return None
        return next(
            (
                document
                for document in documents
                if document.get("state") == "active"
                and document.get("authorityKey") == authority_key
            ),
            None,
        )


    def _execution_policy_prompt_for_session(
        self,
        session: Mapping[str, object],
    ) -> str:
        """Compile execution guidance from the live dispatch, never stale DB state."""

        session_id = str(session.get("id") or "")
        room_dispatch_context = self._exact_room_dispatch_context(
            self._active_room_dispatch_context(session_id)
        )
        effective_session = {
            **session,
            "roomDispatchAuthorized": room_dispatch_context is not None,
        }
        if room_dispatch_context is not None and not read_only_policy_active(session):
            effective_session["roomExecutionMode"] = (
                ROOM_UNRESTRICTED_EXECUTION_MODE
            )
        return execution_policy_prompt(effective_session)

    def _runtime_session_context(self, session: Mapping[str, object]) -> Mapping[str, object]:
        resource_policy = session_resource_disclosure_policy(session, configuration_store=self.configuration_store)
        delegation = getattr(self, "delegation", None)
        if delegation is not None:
            delegated = delegation.runtime_session_context(session)
            if delegated:
                return {**delegated, "resourceDisclosurePolicy": resource_policy}
        session_id = str(session.get("id") or "")
        session_context = "\n\n".join(
            value
            for value in (
                self._execution_policy_prompt_for_session(session),
                self.memory_context_application.provider_context(session_id)
                if self.memory_enabled() and self._session_memory_disclosed(session_id)
                else "",
            )
            if value
        )
        return {"resourceDisclosurePolicy": resource_policy, **({"sessionContext": session_context} if session_context else {})}

    def memory_enabled(self) -> bool:
        """Resolve the live memory master switch for the next Runtime call."""

        return memory_enabled_from_settings(self.db_path)

    def _session_memory_disclosed(self, session_id: str) -> bool:
        return capability_disclosure_enabled(
            "tool:memory",
            session=self.sessions.get(session_id),
            configuration_store=self.configuration_store,
        )

    def runtime_status(self) -> dict[str, object]:
        payload = self.runtime.runtime_status()
        payload.setdefault("driverId", self.runtime_factory.driver_id)
        payload.setdefault("runtimeKind", self.runtime_factory.runtime_kind)
        payload.setdefault("runtimeVersion", str(payload.get("piVersion") or ""))
        capabilities = (
            dict(payload.get("capabilities"))
            if isinstance(payload.get("capabilities"), Mapping)
            else {}
        )
        capabilities["delegation"] = True
        payload["capabilities"] = capabilities
        configuration = self.configuration_store.snapshot()
        payload["configurationRevision"] = configuration["revision"]
        payload["configurationSyncState"] = configuration["sync"]["state"]
        validate_contract(payload, "agent-runtime.v1.json")
        return payload

    def configuration(self) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.agent-configuration-get.v1",
            "ok": True,
            "configuration": self.configuration_store.snapshot(),
            "promptPolicy": prompt_settings_catalog(),
        }

    def _runtime_prompt_settings(self, _session: Mapping[str, object]) -> Mapping[str, object]:
        optimization_policy = self._optimization_application().session_policy(_session)
        if optimization_policy is not None:
            return dict(optimization_policy["promptSettings"])
        return self.configuration_store.snapshot()["configuration"]["prompts"]

    def _runtime_candidate_skill_paths(self, session: Mapping[str, object]) -> list[str]:
        optimization_policy = self._optimization_application().session_policy(session)
        return list(optimization_policy["candidateSkillPaths"]) if optimization_policy is not None else []

    def update_configuration(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._configuration_lock:
            return self._update_configuration_locked(payload)

    def _update_configuration_locked(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        expected_revision = payload.get("expectedRevision")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValueError("agent configuration update requires expectedRevision")
        changes = payload.get("changes")
        if not isinstance(changes, Mapping):
            raise ValueError("agent configuration update requires a changes object")
        runtime_keys = {
            "runtime.enabled",
            "runtime.startup",
            "runtime.idleTimeoutSeconds",
        }
        # Resource selection is read when a new Pi Session is bound. Existing
        # Sessions keep their resourceSnapshot, including across reconnects.
        runtime_change = bool(runtime_keys.intersection(str(key) for key in changes))
        if runtime_change:
            status = self.runtime.runtime_status()
            if status.get("status") in {"starting", "busy"}:
                raise ValueError("agent runtime settings cannot change during an active turn")

        update = self.configuration_store.update(
            changes,
            expected_revision=expected_revision,
            updated_by=str(payload.get("updatedBy") or "api-client"),
        )
        self.control_events.fan_out(update.event)
        snapshot = update.snapshot
        sync_event = None
        if update.runtime_sync_required:
            revision = int(snapshot["revision"])
            try:
                status = self._apply_runtime_policy(
                    runtime_policy_from_configuration(snapshot["configuration"])
                )
            except Exception as exc:
                fallback_status = self.runtime.runtime_status()
                snapshot, sync_event = self.configuration_store.mark_failed(
                    revision,
                    error=str(exc),
                    runtime_status=fallback_status,
                )
            else:
                snapshot, sync_event = self.configuration_store.mark_applied(
                    revision,
                    runtime_status=status,
                )
            self.control_events.fan_out(sync_event)
        return {
            "schemaVersion": "rag-ime.agent-configuration-update.v1",
            "ok": snapshot["sync"]["state"] != "failed",
            "changedKeys": list(update.changed_keys),
            "configuration": snapshot,
            "event": sync_event or update.event,
        }

    def ensure_runtime(self, payload: Mapping[str, object]) -> dict[str, object]:
        # Opening a Room prewarms its Session. This is not a user-message
        # admission: keeping a priority claim through the optional memory
        # probe makes an idle participant reject the user's actual send.
        # Pi's ensure() already owns Host/Session lifecycle serialization.
        return self.session_application.ensure_runtime(payload)

    def list_sessions(self, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        return self.session_application.list_sessions(payload)

    def eval_lab_runs(self) -> dict[str, object]:
        return self.eval_lab.list_runs()

    def _default_trial_adapters(self) -> dict[str, TrialAdapter]:
        # The resident Pi can evaluate the fixed Memory fixture. RAG and
        # CloudOps need explicit host assets and stay absent until registered.
        # An explicit registry, including {}, replaces this default.
        if not self._eval_lab_trial_execution_owner:
            return {}
        artifact_root = self.sessions.db_path.expanduser().resolve().parent / "agent-lab-trials" / "memory"
        if artifact_root.is_relative_to(Path(__file__).resolve().parents[1]):
            # A source-local database is not a private evaluation artifact
            # owner. Its host can inject an adapter with external storage.
            return {}
        from .agent_lab.memory_trial import AgentLabMemoryTrialAdapter

        def executor():
            from .agent_lab.golden_pi import AgentLabGoldenPiExecutor
            return AgentLabGoldenPiExecutor(
                self.sessions.db_path, sessions=self.sessions, runtime=lambda: self.runtime,
            )

        return {"knowledge-resource": self._knowledge_resource(), "memory": AgentLabMemoryTrialAdapter(
            artifact_root, pi_executor_factory=executor,
            abort_session=lambda session_id: self.runtime.abort(session_id),
            production_db=self.sessions.db_path, project=self.project or "personal-agent-workbench",
        )}

    def configure_lab_knowledge(self, *, client, settings_provider) -> None:
        self._lab_knowledge_client = client
        self._lab_knowledge_settings = settings_provider

    def _knowledge_resource(self):
        from .agent_lab.knowledge import AgentLabKnowledgeResource
        if self._eval_lab_knowledge_resource is None:
            self._eval_lab_knowledge_resource = AgentLabKnowledgeResource(
                self.sessions.db_path.expanduser().resolve().parent / "agent-lab-trials" / "knowledge",
                read_trials=lambda job_id: self._trial_store().read(job_id),
                settings=lambda: self._lab_knowledge_settings(),
                knowledge_client=lambda: self._lab_knowledge_client,
            )
        return self._eval_lab_knowledge_resource

    def _trial_store(self) -> AgentLabTrialStore:
        with self._eval_lab_trial_lock:
            if self._eval_lab_trial_store is None:
                self._eval_lab_trial_store = AgentLabTrialStore(self.sessions.db_path)
            return self._eval_lab_trial_store

    def _trial_application(self) -> AgentLabTrialApplication:
        with self._eval_lab_trial_lock:
            if not self._eval_lab_trial_execution_owner or self._eval_lab_trial_closed:
                raise AgentLabTrialServiceUnavailable()
            if self._eval_lab_trial_application is None:
                self._eval_lab_trial_application = AgentLabTrialApplication(
                    self._trial_store(), self._eval_lab_trial_adapters,
                )
            return self._eval_lab_trial_application

    @staticmethod
    def _trial_command_payload(payload: Mapping[str, object], fields: set[str]) -> None:
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValueError("Trial command fields are invalid")
        for key in fields - {"spec"}:
            value = payload[key]
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError("Trial identifiers must be nonempty strings of at most 256 characters")
        if "spec" in fields and not isinstance(payload["spec"], Mapping):
            raise ValueError("Trial spec must be a JSON mapping")

    def eval_lab_trials(self, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        # Reads never construct the execution Application or recover jobs.
        if payload is not None and (not isinstance(payload, Mapping) or set(payload) - {"jobId"}):
            raise ValueError("Trial read fields are invalid")
        job_id = (payload or {}).get("jobId", "")
        if not isinstance(job_id, str) or len(job_id) > 256 or (job_id and not job_id.strip()):
            raise ValueError("Trial jobId is invalid")
        response = self._trial_store().read(job_id)
        if not job_id:
            # Registry membership is configuration, not asset validation or
            # permission to execute a scene. Reading it starts no workers.
            response["registeredSceneIds"] = sorted(self._eval_lab_trial_adapters)
        return response

    def eval_lab_trial_start(self, payload: Mapping[str, object]) -> dict[str, object]:
        self._trial_command_payload(payload, {"clientRequestId", "sceneId", "spec"})
        if not self._eval_lab_trial_execution_owner or self._eval_lab_trial_closed:
            raise AgentLabTrialServiceUnavailable()
        return self._trial_application().start(payload["clientRequestId"], payload["sceneId"], payload["spec"])

    def eval_lab_trial_cancel(self, payload: Mapping[str, object]) -> dict[str, object]:
        self._trial_command_payload(payload, {"jobId"})
        return self._trial_application().cancel(payload["jobId"])

    def _lab_project_application(self):
        from .agent_lab.project_application import AgentLabProjectApplication
        with self._eval_lab_project_lock:
            if self._eval_lab_project_application is None:
                self._eval_lab_project_application = AgentLabProjectApplication(
                    self.sessions.db_path, session_application=self.session_application,
                    current_model=self._golden_current_model,
                    read_golden=self.eval_lab_golden, command_golden=self.eval_lab_golden_command,
                    knowledge=self._knowledge_resource(),
                    start_knowledge=lambda request_id, spec: self.eval_lab_trial_start({"clientRequestId": request_id, "sceneId": "knowledge-resource", "spec": spec}),
                    cancel_knowledge=lambda job_id: self.eval_lab_trial_cancel({"jobId": job_id}),
                    read_experiments=self.eval_lab._experiments_with_source_ledger,
                    read_trials=self.eval_lab_trials,
                    command_app=self.eval_lab_app_command,
                )
            return self._eval_lab_project_application

    def eval_lab_projects(self, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        return self._lab_project_application().read(payload)

    def eval_lab_project_command(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._lab_project_application().command(payload)

    def eval_lab_project_tool(self, session_id: str, operation: str, payload: Mapping[str, object]) -> dict[str, object]:
        return self._lab_project_application().tool(self.sessions.get(session_id), operation, payload)

    def eval_lab_apps(self, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        return self._lab_project_application().apps.read(payload)

    def eval_lab_app_download(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._lab_project_application().apps.download(payload)

    def eval_lab_app_research_tool(self, session_id: str, operation: str, payload: Mapping[str, object]) -> dict[str, object]:
        from .agent_lab.apps import AgentLabAppApplication
        # Tool dispatch may arrive in a gateway process other than the model
        # admission process. The same persisted App call/version owns scope.
        with self._eval_lab_app_lock:
            app = self._eval_lab_app_application
            if app is None:
                if self._eval_lab_app_research_application is None:
                    self._eval_lab_app_research_application = AgentLabAppApplication(
                        self._lab_project_application().apps,
                        complete=lambda **_: (_ for _ in ()).throw(RuntimeError('Research tools do not admit models')),
                        abort=lambda _: None, start_workers=False, recover=False)
                app = self._eval_lab_app_research_application
        return app.research_tool(self.sessions.get(session_id), operation, payload)

    def eval_lab_app_command(self, payload: Mapping[str, object]) -> dict[str, object]:
        from .agent_lab.apps import AgentLabAppApplication
        from .agent_lab.golden_pi import AgentLabGoldenPiExecutor
        from .agent_lab.projects import AgentLabProjectUnavailable
        if payload.get('action') in {'activate','deactivate'}:
            return self._lab_project_application().apps.command(payload)
        if not self._eval_lab_golden_execution_owner: raise AgentLabProjectUnavailable()
        with self._eval_lab_app_lock:
            if self._eval_lab_app_application is None:
                executor = AgentLabGoldenPiExecutor(self.sessions.db_path,sessions=self.sessions,
                    runtime=lambda:self.runtime,session_identity=lambda request_id:self._eval_lab_app_application.session_identity(request_id))
                self._eval_lab_app_application = AgentLabAppApplication(self._lab_project_application().apps,
                    complete=executor.complete,abort=lambda session_id:self.runtime.abort(session_id))
            application = self._eval_lab_app_application
        return application.command(payload)

    def _golden_store(self):
        from .agent_lab.golden import AgentLabGoldenStore
        with self._eval_lab_golden_lock:
            if self._eval_lab_golden_store is None:
                self._eval_lab_golden_store = AgentLabGoldenStore(self.sessions.db_path)
            return self._eval_lab_golden_store

    def _golden_current_model(self) -> dict[str, str]:
        configuration = self.configuration_store.snapshot()["configuration"]
        primary = configuration.get("modelRouting", {}).get("primary", {})
        profile = str(primary.get("modelProfile") or "inherit")
        if profile == "inherit":
            profile = str(configuration["sessionDefaults"]["modelProfile"])
        provider, separator, model_id = profile.partition("/")
        if not separator:
            raise ValueError("configured Session model must contain provider/model")
        thinking = str(primary.get("thinkingLevel") or "high")
        return {"provider": provider, "model": model_id,
                "thinkingLevel": "high" if thinking == "inherit" else thinking, "prompt": ""}

    def eval_lab_golden(self, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        # Read replicas do not recover jobs or create a Runtime worker.
        return self._golden_store().read(str((payload or {}).get("suiteId") or ""))

    def eval_lab_golden_command(self, payload: Mapping[str, object]) -> dict[str, object]:
        from .agent_lab.golden import AgentLabGoldenServiceUnavailable, AgentLabGoldenStore
        from .agent_lab.golden_execution import AgentLabGoldenApplication
        from .agent_lab.golden_pi import AgentLabGoldenPiExecutor
        if payload.get("action") == "create":
            # Capture current settings for this suite only. Existing standards
            # and replayed command receipts keep their original model binding.
            return AgentLabGoldenStore(
                self.sessions.db_path, default_model=self._golden_current_model(),
            ).command(payload)
        if payload.get("action") not in {"draft", "review", "calibrate", "experiment", "cancel", "resume"}:
            return self._golden_store().command(payload)
        if not self._eval_lab_golden_execution_owner:
            raise AgentLabGoldenServiceUnavailable()
        with self._eval_lab_golden_lock:
            if self._eval_lab_golden_application is None:
                executor = AgentLabGoldenPiExecutor(
                    self.sessions.db_path, sessions=self.sessions, runtime=lambda: self.runtime,
                )
                self._eval_lab_golden_application = AgentLabGoldenApplication(
                    store=self._golden_store(), complete=executor.complete,
                    completed_result=executor.completed_result,
                    abort=lambda session_id: self.runtime.abort(session_id),
                    retrieve_knowledge=self._knowledge_resource().answer_sources,
                )
        return self._eval_lab_golden_application.command(payload)

    def eval_lab_evidence_read(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self.eval_lab_evidence.read(payload)

    def eval_lab_scene_recipes(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        request = payload or {}
        return self.eval_lab_scene_recipe_store.get_state(
            str(request.get("sceneId") or ""),
            experiment_id=str(request.get("experimentId") or ""),
        )

    def eval_lab_scene_recipe_apply(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        revision = payload.get("expectedRevision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("expectedRevision must be a non-negative integer")
        return self.eval_lab_scene_recipe_store.apply_candidate(
            _required_text(payload, "sceneId"),
            experiment_id=_required_text(payload, "experimentId"),
            expected_revision=revision,
            client_request_id=_required_text(payload, "clientRequestId"),
        )

    def eval_lab_scene_recipe_rollback(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        revision = payload.get("expectedRevision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("expectedRevision must be a non-negative integer")
        return self.eval_lab_scene_recipe_store.rollback(
            _required_text(payload, "sceneId"),
            expected_revision=revision,
            client_request_id=_required_text(payload, "clientRequestId"),
        )

    def ensure_surface_session(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.session_application.ensure_surface_session(payload)

    def workflow_state(self, session_id: str) -> dict[str, object]:
        state = self.sessions.workflow_state(
            session_id,
            room_dispatch_authorized=(
                self._active_room_dispatch_authorizes_work(session_id)
            ),
        )
        validate_contract(state, "agent-workflow-state.v1.json")
        return state

    def _active_room_dispatch_context(
        self,
        session_id: str,
    ) -> dict[str, object] | None:
        participant = self.rooms.participant_for_session(
            session_id,
            active_only=True,
        )
        root_id, dispatch_id = self.room_turns.active_turn(session_id)
        if participant is None or not root_id:
            return None
        if self.room_turns.is_cancelled(session_id, root_id):
            return None
        task = self.task_context.resolve(session_id)
        try:
            generation = self._room_runtime_generation(session_id)
        except (KeyError, ValueError):
            return None
        return self._exact_room_dispatch_context(
            {
                "roomId": str(participant.get("roomId") or ""),
                "rootId": root_id,
                "taskId": str(task.get("workItemId") or ""),
                "dispatchId": dispatch_id,
                "generation": generation,
            }
        )

    @staticmethod
    def _exact_room_dispatch_context(
        context: Mapping[str, object] | None,
    ) -> dict[str, object] | None:
        """Accept only a complete dispatch capability, never a partial overlay."""

        if not isinstance(context, Mapping):
            return None
        room_id = str(context.get("roomId") or "").strip()
        root_id = str(context.get("rootId") or "").strip()
        dispatch_id = str(context.get("dispatchId") or "").strip()
        try:
            generation = int(context.get("generation") or 0)
        except (TypeError, ValueError):
            return None
        if not room_id or not root_id or not dispatch_id or generation <= 0:
            return None
        return {
            **dict(context),
            "roomId": room_id,
            "rootId": root_id,
            "dispatchId": dispatch_id,
            "generation": generation,
        }

    def _active_room_dispatch_authorizes_work(
        self,
        session_id: str,
    ) -> bool:
        context = self._active_room_dispatch_context(session_id)
        # Posting work to a Room is the user's execution action.  A live,
        # session-bound dispatch therefore authorizes the Room's default
        # no-per-Tool policy even for legacy Rooms that predate the persisted
        # roomExecutionMode overlay.  Ordinary Session turns never enter this
        # path, and read-only/workspace fences are still checked by the Tool
        # gateway immediately before application.
        return context is not None

    def _room_delegation_context(
        self,
        session_id: str,
    ) -> dict[str, object]:
        participant = self.rooms.participant_for_session(
            session_id,
            active_only=True,
        )
        permission_policy: object = None
        room_id = ""
        if participant is not None:
            room_id = str(participant.get("roomId") or "")
            try:
                room = self.rooms.get(room_id)
            except AgentRoomNotFound:
                room = None
            if room is not None:
                permission_policy = room.get("permissionPolicy")
        context: dict[str, object] = {
            "roomBound": False,
            "roomId": "",
            "rootId": "",
            "taskId": "",
            "dispatchId": "",
            "generation": 0,
        }
        if permission_policy is not None:
            context["permissionPolicy"] = permission_policy
            context["permissionPolicySource"] = "room"
        live = self._active_room_dispatch_context(session_id)
        if live is None or not room_id:
            return context
        live_room_id = str(live.get("roomId") or "")
        if live_room_id != room_id:
            return context
        context.update(
            {
                "roomBound": True,
                "roomId": live_room_id,
                "rootId": str(live.get("rootId") or ""),
                "taskId": str(live.get("taskId") or ""),
                "dispatchId": str(live.get("dispatchId") or ""),
                "generation": int(live["generation"]),
            }
        )
        return context


    def mutate_goal(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        request = dict(payload)
        validate_contract(request, "agent-goal-mutation.v1.json")
        if request["action"] in {"pause", "cancel"}:
            self._transition_goal_cancellation(session_id, request)
        else:
            self.sessions.mutate_agent_goal(session_id, request)
        state = self.publish_workflow_state(session_id, reason=f"goal:{request['action']}")
        goal = state.get("goal")
        if isinstance(goal, Mapping) and goal.get("goalId"):
            try:
                self.work_documents.observe_authority(
                    "session_goal", str(goal["goalId"])
                )
            except Exception:
                # The reconciler owns filesystem recovery; Goal state remains canonical.
                pass
        return state

    def _transition_goal_cancellation(
        self,
        session_id: str,
        request: Mapping[str, object],
    ) -> dict[str, object]:
        action = str(request["action"])
        scope_kind = "goal"
        source = self.sessions.agent_goal(session_id)
        scope_id = str(source["goalId"])
        expected_terminal = "paused" if action == "pause" else "cancelled"
        source_revision = int(source["revision"])
        expected_revision = request.get("expectedRevision")
        existing = None
        if str(source["status"]) == expected_terminal:
            candidate = self.sessions.lifecycle_cancellation_for_transition(
                session_id,
                scope_kind=scope_kind,
                scope_id=scope_id,
                transition_revision=source_revision,
                action=action,
            )
            if (
                candidate is not None
                and (
                    expected_revision is None
                    or int(candidate["sourceRevision"]) == int(expected_revision)
                )
            ):
                existing = candidate
        if existing is None:
            lifecycle_request = {
                "requestId": _lifecycle_cancellation_request_id(
                    session_id=session_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    source_revision=source_revision,
                    action=action,
                ),
                "scopeKind": scope_kind,
                "scopeId": scope_id,
                "sourceRevision": source_revision,
                "sourceTurnId": self.sessions.latest_runtime_turn_id(session_id),
                "action": action,
                "reason": str(request.get("reason") or request.get("note") or ""),
            }
            mutation = self.sessions.mutate_agent_goal(
                session_id,
                request,
                lifecycle_request=lifecycle_request,
            )
            existing = dict(mutation["lifecycleAudit"])
        audit = self._cancel_lifecycle_owners(existing)
        self.events.publish(
            session_id,
            "lifecycle_cancellation_changed",
            {"audit": audit},
            turn_id=str(audit.get("sourceTurnId") or ""),
        )
        return audit

    def _cancel_lifecycle_owners(
        self,
        audit: Mapping[str, object],
    ) -> dict[str, object]:
        request_id = str(audit["requestId"])
        session_id = str(audit["sessionId"])
        scope_kind = str(audit["scopeKind"])
        scope_id = str(audit["scopeId"])
        source_revision = int(audit["sourceRevision"])
        reason = str(audit.get("reason") or f"{scope_kind}_{audit['action']}")
        current = dict(audit)

        runtime_owner = _lifecycle_owner(current, "runtime")
        if runtime_owner["status"] not in {"succeeded", "excluded"}:
            try:
                raw_runtime_receipt = self.runtime.abort(session_id)
                runtime_receipt = _lifecycle_public_receipt(
                    raw_runtime_receipt
                )
                runtime_status = _runtime_lifecycle_status(
                    runtime_receipt
                )
            except Exception as exc:
                runtime_status = "unknown"
                runtime_receipt = {
                    "reason": "runtime_owner_request_failed",
                    "errorType": type(exc).__name__,
                }
            current = self.sessions.record_lifecycle_owner_receipt(
                request_id,
                owner="runtime",
                status=runtime_status,
                receipt=runtime_receipt,
            )

        approval_owner = _lifecycle_owner(current, "approval")
        if approval_owner["status"] not in {"succeeded", "excluded"}:
            try:
                approval_receipt = self.sessions.cancel_causal_approvals(
                    session_id,
                    request_id=request_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    source_revision=source_revision,
                    reason=reason,
                )
                approval_status = "succeeded"
            except Exception as exc:
                approval_status = "unknown"
                approval_receipt = {
                    "reason": "approval_owner_request_failed",
                    "errorType": type(exc).__name__,
                }
            current = self.sessions.record_lifecycle_owner_receipt(
                request_id,
                owner="approval",
                status=approval_status,
                receipt=_lifecycle_public_receipt(approval_receipt),
            )

        job_owner = _lifecycle_owner(current, "job")
        if job_owner["status"] not in {"succeeded", "excluded"}:
            try:
                job_receipt = self.background_jobs.cancel_causal(
                    session_id,
                    request_id=request_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    source_revision=source_revision,
                    reason=reason,
                )
                job_status = _job_lifecycle_status(job_receipt)
            except Exception as exc:
                job_status = "unknown"
                job_receipt = {
                    "reason": "background_job_owner_request_failed",
                    "errorType": type(exc).__name__,
                }
            current = self.sessions.record_lifecycle_owner_receipt(
                request_id,
                owner="job",
                status=job_status,
                receipt=_lifecycle_public_receipt(job_receipt),
            )
        delegation_owner = _lifecycle_owner(current, "delegation")
        if delegation_owner["status"] not in {"succeeded", "excluded"}:
            try:
                delegation_receipt = self.delegation.cancel_causal(
                    session_id,
                    request_id=request_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    source_revision=source_revision,
                    reason=reason,
                )
                delegation_status = _delegation_lifecycle_status(
                    delegation_receipt
                )
            except Exception as exc:
                delegation_status = "unknown"
                delegation_receipt = {
                    "reason": "delegation_owner_request_failed",
                    "errorType": type(exc).__name__,
                }
            current = self.sessions.record_lifecycle_owner_receipt(
                request_id,
                owner="delegation",
                status=delegation_status,
                receipt=_lifecycle_public_receipt(
                    delegation_receipt
                ),
            )
        return current

    def internal_workflow_state(self, payload: Mapping[str, object]) -> dict[str, object]:
        state = self.workflow_state(_required_text(payload, "sessionId"))
        return {
            "ok": True,
            "result": {
                "todo": state["todo"],
                "goal": state["goal"],
                "actGate": state["actGate"],
            },
        }

    def settle_goal_runtime(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Decide whether one ordinary Goal turn needs one native continuation.

        Pi owns turn scheduling; this method owns the product decision.  A
        continuation claim is persisted atomically against the Goal lifecycle
        epoch, so replay is idempotent and a new Pi cancel scope or process
        cannot replenish the global allowance. Paused, completed, exhausted,
        or stalled Goals and closed Act Gates terminate without asking another
        model to judge its own completion.
        """

        request = dict(payload)
        validate_contract(request, "agent-goal-settle-request.v1.json")
        session_id = str(request["sessionId"])
        settle_scope_id = str(request["settleScopeId"])
        settle_attempt = int(request["settleAttempt"])
        fresh_tool_evidence_count = int(
            request["freshToolEvidenceCount"]
        )
        fresh_tool_evidence_sha256 = str(
            request["freshToolEvidenceSha256"]
        )
        state = self.workflow_state(session_id)
        goal = (
            state["goal"]
            if isinstance(state.get("goal"), Mapping)
            else {}
        )
        gate = (
            state["actGate"]
            if isinstance(state.get("actGate"), Mapping)
            else {}
        )
        goal_id = str(goal.get("goalId") or "")
        goal_status = str(goal.get("status") or "cleared")
        gate_reason = str(gate.get("reason") or "governance_blocked")
        continuation = self.sessions.agent_goal_continuation_budget(
            session_id,
            goal_id=goal_id,
            limit=GOAL_CONTINUATION_LIMIT,
        )

        decision = "inactive"
        reason = "goal_not_active"
        if goal.get("configured") is True:
            if goal_status == "paused":
                decision = "paused"
                reason = "goal_paused"
            elif goal_status == "completed":
                decision = "completed"
                reason = "goal_completed"
            elif goal_status == "cancelled":
                decision = "cancelled"
                reason = "goal_cancelled"
            elif goal_status != "active":
                decision = "inactive"
                reason = f"goal_{goal_status or 'not_active'}"
            elif goal.get("budgetExceeded") is True:
                decision = "budget_exhausted"
                reason = "goal_budget_exhausted"
            elif gate.get("allowed") is not True:
                decision = "blocked"
                reason = gate_reason
            elif settle_attempt >= GOAL_SETTLE_ATTEMPT_LIMIT:
                decision = "stalled"
                reason = "settle_attempt_limit"
            elif (
                settle_attempt > 1
                and fresh_tool_evidence_count == 0
            ):
                decision = "stalled"
                reason = "no_progress"
            else:
                decision = "continue"
                reason = "goal_active"

        material = "\x1f".join(
            (
                session_id,
                goal_id,
                str(int(goal.get("revision") or 0)),
                settle_scope_id,
                str(settle_attempt),
                str(fresh_tool_evidence_count),
                fresh_tool_evidence_sha256,
            )
        )
        request_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        if decision == "continue":
            claim = self.sessions.claim_agent_goal_continuation(
                session_id,
                goal_id=goal_id,
                request_key=request_key,
                limit=GOAL_CONTINUATION_LIMIT,
            )
            continuation = {
                key: int(claim[key])
                for key in ("epoch", "issuedCount", "limit", "remaining")
            }
            current_goal = (
                claim["goal"]
                if isinstance(claim.get("goal"), Mapping)
                else goal
            )
            current_gate = (
                claim["actGate"]
                if isinstance(claim.get("actGate"), Mapping)
                else gate
            )
            goal = current_goal
            goal_id = str(goal.get("goalId") or "")
            if claim.get("claimed") is not True:
                claim_reason = str(claim.get("reason") or "goal_not_active")
                if claim_reason == "goal_continuation_limit":
                    decision = "stalled"
                elif str(goal.get("status") or "") == "paused":
                    decision = "paused"
                    claim_reason = "goal_paused"
                elif str(goal.get("status") or "") == "completed":
                    decision = "completed"
                    claim_reason = "goal_completed"
                elif str(goal.get("status") or "") == "cancelled":
                    decision = "cancelled"
                    claim_reason = "goal_cancelled"
                elif goal.get("budgetExceeded") is True:
                    decision = "budget_exhausted"
                    claim_reason = "goal_budget_exhausted"
                else:
                    decision = "blocked"
                reason = claim_reason

        result: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-goal-settle-result.v1",
            "sessionId": session_id,
            "goalId": goal_id,
            "goalRevision": int(goal.get("revision") or 0),
            "settleScopeId": settle_scope_id,
            "settleAttempt": settle_attempt,
            "freshToolEvidenceCount": fresh_tool_evidence_count,
            "freshToolEvidenceSha256": fresh_tool_evidence_sha256,
            "continuationEpoch": int(continuation["epoch"]),
            "continuationCount": int(continuation["issuedCount"]),
            "continuationLimit": int(continuation["limit"]),
            "continuationRemaining": int(continuation["remaining"]),
            "state": decision,
            "reason": reason,
            "followUpKey": "",
            "message": "",
        }
        if decision == "continue":
            result["followUpKey"] = (
                "goal-settle:"
                + request_key[:32]
            )
            result["message"] = (
                '<managed-goal-follow-up origin="goal-supervisor">'
                "当前 Goal 仍处于 active，且预算允许继续。"
                "一次回答结束不代表 Goal 完成；先同步更新 Todo 状态，再推进"
                "能够产生新验收证据的下一步。不要只汇报进度或复述 Todo。"
                "没有可继续的下一步时：已有完整验收证据则完成 Goal；"
                "Room 受阻则发出 blocked/partial 终态，保持 Goal active。"
                "不要把仍在进行的 Room Goal 暂停来等待用户、界面或后续消息；"
                "暂停只用于用户明确要求停止，不是等待继续的手段。"
                "</managed-goal-follow-up>"
            )
        validate_contract(result, "agent-goal-settle-result.v1.json")
        return {"ok": True, "result": result}

    def record_goal_usage(self, payload: Mapping[str, object]) -> dict[str, object]:
        request = dict(payload)
        validate_contract(request, "agent-goal-usage.v1.json")
        state = self.sessions.record_agent_goal_usage(
            str(request["sessionId"]),
            idempotency_key=str(request["idempotencyKey"]),
            turn_id=str(request.get("turnId") or ""),
            event_id=str(request.get("eventId") or ""),
            token_delta=int(request.get("tokenDelta") or 0),
            elapsed_delta_ms=int(request.get("elapsedDeltaMs") or 0),
        )
        validate_contract(state, "agent-workflow-state.v1.json")
        self.events.publish(
            str(request["sessionId"]),
            "workflow_changed",
            {"reason": "goal:usage", **state},
        )
        return {
            "ok": True,
            "result": {
                "todo": state["todo"],
                "goal": state["goal"],
                "actGate": state["actGate"],
            },
        }

    def publish_workflow_state(
        self,
        session_id: str,
        *,
        reason: str,
    ) -> dict[str, object]:
        state = self.workflow_state(session_id)
        self.events.publish(
            session_id,
            "workflow_changed",
            {"reason": reason, **state},
        )
        return state

    def create_session(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self.session_application.create_session(payload)

    def list_roles(self) -> dict[str, object]:
        return self.role_application.list_roles()

    def create_role(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self.role_application.create_role(payload)

    def update_role(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self.role_application.update_role(payload)

    def archive_role(self, payload: Mapping[str, object]) -> dict[str, object]:
        role_id = str(payload.get("roleId") or "").strip()
        role_version = str(payload.get("roleVersion") or "").strip()
        defaults = self.configuration_store.snapshot()["configuration"]["sessionDefaults"]
        if role_id == defaults["roleId"] and role_version == defaults["roleVersion"]:
            raise ValueError("default companion cannot be removed; choose another default first")
        return self.role_application.archive_role(payload)

    def role_model_catalog(self) -> dict[str, object]:
        return self.role_application.model_catalog()

    def update_role_runtime_defaults(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self.role_application.update_runtime_defaults(payload)

    def _role_payload(
        self,
        value: Mapping[str, object],
        *,
        available_models: set[tuple[str, str]] | None = None,
    ) -> dict[str, object]:
        return self.role_application.role_payload(
            value,
            available_models=available_models,
        )

    def _initial_role_runtime_defaults(
        self,
        role: PersonaManifest,
        *,
        default_model_profile: str | None = None,
        available_models: set[tuple[str, str]] | None = None,
    ) -> dict[str, str]:
        return self.role_application.initial_runtime_defaults(
            role,
            default_model_profile=default_model_profile,
            available_models=available_models,
        )

    def _available_role_models(self) -> set[tuple[str, str]] | None:
        return self.role_application.available_models()

    def preview_wake_schedule(
        self,
        payload: Mapping[str, object],
        *,
        requested_by_session_id: str = "",
    ) -> dict[str, object]:
        return self.wake_application.preview(
            payload,
            requested_by_session_id=requested_by_session_id,
        )

    def list_wake_schedules(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self.wake_application.list(payload)

    def get_wake_schedule(self, schedule_id: str) -> dict[str, object]:
        return self.wake_application.get(schedule_id)

    def create_wake_schedule(
        self,
        payload: Mapping[str, object],
        *,
        created_by_session_id: str = "",
        require_confirmation: bool = True,
    ) -> dict[str, object]:
        return self.wake_application.create(
            payload,
            created_by_session_id=created_by_session_id,
            require_confirmation=require_confirmation,
        )

    def wake_schedule_runs(
        self,
        schedule_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self.wake_application.runs(schedule_id, payload)

    def wake_schedule_action(
        self,
        schedule_id: str,
        payload: Mapping[str, object],
        *,
        require_confirmation: bool = True,
    ) -> dict[str, object]:
        return self.wake_application.action(
            schedule_id,
            payload,
            require_confirmation=require_confirmation,
        )

    def _validated_wake_schedule(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.wake_application.validate(payload)

    def _dispatch_scheduled_wake(self, claim: Mapping[str, object]) -> None:
        self.wake_application.dispatch(claim)

    def list_agent_templates(self) -> dict[str, object]:
        return self.delegation.catalog()

    def delegate_tasks(self, session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        return self.delegation.delegate(session_id, payload)

    def _configured_model_route(self, route_id: str) -> Mapping[str, object] | None:
        routing = self.configuration_store.snapshot()["configuration"].get(
            "modelRouting"
        )
        if not isinstance(routing, Mapping):
            return None
        route = routing.get(route_id)
        return dict(route) if isinstance(route, Mapping) else None

    def delegation_status(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.delegation.status(session_id, payload)

    def delegation_console(
        self,
        session_id: str,
        run_id: str,
    ) -> dict[str, object]:
        return self.delegation.console(session_id, run_id)

    def control_delegation(
        self,
        session_id: str,
        run_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.delegation.control(session_id, run_id, payload)

    def abort_delegation(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.delegation.abort(session_id, payload)

    def delegation_artifact(
        self,
        session_id: str,
        artifact_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        value = dict(payload or {})
        return self.delegation.inspect_artifact(
            session_id,
            artifact_id,
            limit=_integer(value.get("limit"), default=100, minimum=1, maximum=500),
        )

    def list_rooms(self, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        return self.room_management.list_rooms(payload)

    def _room_participant_sessions(
        self,
        room: Mapping[str, object],
    ) -> list[dict[str, object]]:
        return self.room_management.participant_sessions(room)

    def _repair_room_participant_session(
        self,
        room: Mapping[str, object],
        participant: Mapping[str, object],
    ) -> dict[str, object]:
        repaired = self.room_management._repair_participant_session(
            room,
            participant,
        )
        self._activate_room_unrestricted_execution(
            str(room.get("id") or ""),
        )
        return repaired

    def _restore_room_participant_sessions(
        self,
        room: Mapping[str, object],
    ) -> None:
        self.room_management.restore_participant_sessions(room)
        self._activate_room_unrestricted_execution(
            str(room.get("id") or ""),
        )

    def room(self, room_id: str) -> dict[str, object]:
        return self.room_management.get_room(room_id)

    def room_snapshot(self, room_id: str) -> dict[str, object]:
        return self.room_management.snapshot(room_id)

    def room_conversation_snapshot(self, room_id: str) -> dict[str, object]:
        return self.room_management.conversation_snapshot(room_id)

    def room_history(
        self,
        room_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self.room_management.history(room_id, payload)


    def collaboration_profile_projection(self, profile_id: str) -> dict[str, object]:
        with sqlite_connection(self.db_path) as conn:
            return self._collaboration_profile_control(conn).projection(profile_id)

    def apply_collaboration_profile_command(
        self,
        payload: Mapping[str, object],
        *,
        caller_authorized: bool = False,
    ) -> dict[str, object]:
        if not caller_authorized:
            raise PermissionError("CollaborationProfile control requires an authorized control caller")
        with sqlite_connection(self.db_path) as conn:
            result = self._collaboration_profile_control(conn).execute(payload)
        return result



    def room_knowledge_search(
        self,
        payload: Mapping[str, object],
        *,
        authenticated_session_id: str,
    ) -> dict[str, object]:
        self._consume_room_knowledge_cache_tombstones()
        forbidden = {"owner", "ownerId", "ownerKind", "scope", "scopeId", "scopeKind", "allowedScopes", "allowedDomains", "sessionId"}
        if forbidden.intersection(payload):
            raise PermissionError("knowledge owner/scope/session are server-derived")
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ValueError("knowledge search query is required")
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            caller = bound_session_knowledge_caller(conn, authenticated_session_id)
        receipt_id = str(payload.get("retrievalReceiptId") or f"knowledge-retrieval:{uuid.uuid4().hex}")
        result = self.knowledge_promotion.search(
            retrieval_receipt_id=receipt_id,
            query=query,
            caller=caller,
            limit=int(payload.get("limit") or 10),
            created_at_ms=int(payload.get("createdAtMs") or int(time.time() * 1000)),
        )
        return {
            "schemaVersion": "wisdom-weasel.room-knowledge-search-result.v1",
            "routeHash": KNOWLEDGE_ROUTE_HASH,
            "requiredScopes": ["agent.read"],
            **result,
        }

    def room_knowledge_read(
        self,
        payload: Mapping[str, object],
        *,
        authenticated_session_id: str,
    ) -> dict[str, object]:
        self._consume_room_knowledge_cache_tombstones()
        forbidden = {"owner", "ownerId", "ownerKind", "scope", "scopeId", "scopeKind", "allowedScopes", "allowedDomains", "sessionId", "query"}
        if forbidden.intersection(payload):
            raise PermissionError("knowledge read uses only its prior retrieval receipt")
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            caller = bound_session_knowledge_caller(conn, authenticated_session_id)
        if caller is None:
            raise PermissionError("knowledge read requires an active Room ParticipantBinding")
        result = self.knowledge_promotion.read(
            claim_ref=_required_text(payload, "claimRef"),
            retrieval_receipt_id=_required_text(payload, "retrievalReceiptId"),
            expected_hash=_required_text(payload, "expectedHash"),
            caller=caller,
        )
        return {
            "schemaVersion": "wisdom-weasel.room-knowledge-read-result.v1",
            "routeHash": KNOWLEDGE_ROUTE_HASH,
            "requiredScopes": ["agent.read"],
            **result,
        }

    def _collaboration_profile_control(
        self,
        conn: sqlite3.Connection,
    ) -> CollaborationProfileControl:
        return CollaborationProfileControl(
            conn,
            trusted_signers=self._collaboration_profile_signers,
            baseline_capabilities=(
                "rag", "memory", "planning", "review", "control", "delegation"
            ),
            binding_revision="room-v2-agent-definition-compiler-v1",
        )

















    def governance_read_model(self, *, scope_key: str | None = None) -> dict[str, object]:
        return {
            "schemaVersion": "wisdom-weasel.governance-read-model.v1",
            "governance": self.governance_projection.snapshot(scope_key=scope_key),
        }

    def knowledge_governance_read_model(self) -> dict[str, object]:
        return {
            "schemaVersion": "wisdom-weasel.knowledge-governance-read-model.v1",
            "knowledge": self.knowledge_promotion.governance_snapshot(),
        }


    def room_work_items(
        self,
        room_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self.room_work_application.room_work_items(
            room_id,
            payload,
        )

    def room_work_item(
        self,
        room_id: str,
        work_item_id: str,
    ) -> dict[str, object]:
        return self.room_work_application.room_work_item(
            room_id,
            work_item_id,
        )

    def create_room_work_item(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_work_application.create_room_work_item(
            room_id,
            payload,
        )

    def reassign_room_work_item(
        self,
        room_id: str,
        work_item_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_work_application.reassign_room_work_item(
            room_id,
            work_item_id,
            payload,
        )

    def resume_room_work_item(
        self,
        room_id: str,
        work_item_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        actor_participant_id = str(payload.get("actorParticipantId") or "").strip()
        if not actor_participant_id:
            raise ValueError("actorParticipantId must not be empty")
        client_action_id = _optional_client_message_id(payload.get("clientActionId"))
        if not client_action_id:
            client_action_id = f"room-work-resume:{uuid.uuid4()}"
        phase = str(payload.get("phase") or "recovery").strip()
        if not phase or len(phase) > 120:
            raise ValueError("phase must contain between 1 and 120 characters")
        timeout_seconds = int(payload.get("timeoutSeconds") or 300)
        if not 5 <= timeout_seconds <= 300:
            raise ValueError("timeoutSeconds must be between 5 and 300")
        response = self.room_partner_application.resume_work_item_for_control(
            room_id,
            work_item_id,
            actor_participant_id,
            phase=phase,
            timeout_seconds=timeout_seconds,
            tool_call_id=client_action_id,
        )
        if not isinstance(response.get("workItem"), Mapping) or not str(
            response.get("childDispatchId") or ""
        ).strip():
            raise RuntimeError("Room WorkItem resume did not return a verifiable dispatch receipt")
        response = {
            "schemaVersion": "rag-ime.agent-room-work-item-resume.v1",
            "ok": True,
            "operation": "resume",
            "roomId": room_id,
            "workItem": response.get("workItem"),
            "rootId": response.get("rootId"),
            "dispatchReceipt": {
                "childDispatchId": response.get("childDispatchId", ""),
                "participantId": response.get("participantId", ""),
                "status": response.get("status", "failed"),
                "workItemId": work_item_id,
                "accepted": True,
            },
            "contractStatus": response.get("contractStatus", "pending"),
        }
        response["controlReceipt"] = {
            "state": "accepted",
            "scope": "room_work_resume",
            "clientActionId": client_action_id,
        }
        return response

    def room_topics(
        self,
        room_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self.room_management.topics(room_id, payload)

    def create_room_topic(self, room_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        return self.room_management.create_topic(room_id, payload)

    def update_room_topic(self, room_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        return self.room_management.update_topic(room_id, payload)

    def room_artifacts(
        self,
        room_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self.room_management.artifacts(room_id, payload)

    def add_room_artifact(self, room_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        return self.room_management.add_artifact(room_id, payload)

    def update_room_artifact(self, room_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        return self.room_management.update_artifact(room_id, payload)

    def update_room(self, room_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        response = self.room_management.update_room(room_id, payload)
        room = response.get("room") if isinstance(response, Mapping) else None
        if isinstance(room, Mapping):
            self._activate_room_unrestricted_execution(
                room_id,
                room=room,
            )
            # `update_room` may restore an archived Room whose response was
            # projected before the legacy pending start row was retired.  Give
            # the caller the post-policy snapshot, never a confirmation card
            # that no longer exists in durable state.
            return {**dict(response), "room": self.rooms.get(room_id)}
        return response

    def add_room_participant(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        response = self.room_management.add_participant(room_id, payload)
        self._activate_room_unrestricted_execution(room_id)
        return response

    def remove_room_participant(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_management.remove_participant(room_id, payload)

    def update_room_participant_role(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_management.update_participant_role(
            room_id,
            payload,
        )

    def delete_room(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_management.delete_room(room_id, payload)

    def create_room(self, payload: Mapping[str, object]) -> dict[str, object]:
        response = self.room_management.create_room(payload)
        room = response.get("room") if isinstance(response, Mapping) else None
        if isinstance(room, Mapping):
            self._activate_room_unrestricted_execution(
                str(room.get("id") or ""),
                room=room,
            )
        return response

    def post_room_message(self, room_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        message = str(payload.get("message") or "")
        if not message.strip():
            raise ValueError("room message must not be empty")
        if len(message) > ROOM_MESSAGE_CHAR_LIMIT:
            raise ValueError(
                f"Room message must not exceed {ROOM_MESSAGE_CHAR_LIMIT} characters"
            )
        client_message_id = _optional_client_message_id(payload.get("clientMessageId"))
        retry_of_root_id = str(payload.get("retryOfRootId") or "").strip()
        if len(retry_of_root_id) > 320:
            raise ValueError("Room retry root identity is too long")
        work_item_id = _optional_work_item_id(payload.get("workItemId"))
        answer_to_post_id = str(payload.get("answerToPostId") or "").strip()
        answer_to_root_id = str(payload.get("answerToRootId") or "").strip()
        if bool(answer_to_post_id) != bool(answer_to_root_id):
            raise ValueError(
                "Room clarification answers require answerToPostId and answerToRootId"
            )
        if len(answer_to_post_id) > 320 or len(answer_to_root_id) > 320:
            raise ValueError("Room clarification answer identity is too long")
        attachment_ids = _room_attachment_ids(payload.get("attachmentIds"))
        requested_ids = payload.get("participantIds")
        if requested_ids is None:
            requested_participant_ids: list[str] = []
        elif isinstance(requested_ids, list):
            requested_participant_ids = [
                str(value or "").strip()
                for value in requested_ids
                if str(value or "").strip()
            ]
        else:
            raise ValueError("participantIds must be an array")
        # Room is an explicitly created collaboration surface. Its participant
        # Sessions receive the workspace-scoped unrestricted overlay at Room
        # creation, so normal work never pauses for a second start approval or
        # for per-Tool approvals. Activation also retires any legacy start gate.
        if work_item_id:
            room = self.rooms.get(room_id)
            if str(room.get("roomKind") or "collaboration") == "collaboration":
                self._activate_room_unrestricted_execution(room_id, room=room)
        if not client_message_id:
            return self._post_room_message_once(
                room_id,
                message=message,
                client_message_id="",
                retry_of_root_id=retry_of_root_id,
                requested_participant_ids=requested_participant_ids,
                work_item_id=work_item_id,
                attachment_ids=attachment_ids,
                answer_to_post_id=answer_to_post_id,
                answer_to_root_id=answer_to_root_id,
            )
        command_payload = {
            "message": message,
            "retryOfRootId": retry_of_root_id,
            "participantIds": requested_participant_ids,
            "workItemId": work_item_id,
            "attachmentIds": attachment_ids,
            "answerToPostId": answer_to_post_id,
            "answerToRootId": answer_to_root_id,
        }
        claim = self.command_receipts.begin(
            command_scope="room_message",
            scope_id=room_id,
            client_message_id=client_message_id,
            payload=command_payload,
        )
        if claim.replay_response is not None:
            start_confirmation = claim.replay_response.get(
                "startConfirmation"
            )
            if not (
                claim.replay_response.get("accepted") is False
                and claim.replay_response.get("status")
                == "awaiting_confirmation"
                and isinstance(start_confirmation, Mapping)
                and start_confirmation.get("status") == "pending"
            ):
                return {**claim.replay_response, "idempotentReplay": True}
            # Older Hosts durably accepted the UI pause itself.  Room dispatch
            # is now the authorization boundary, so atomically reopen only
            # that exact obsolete response and execute the original command.
            # Concurrent callers either receive the repaired response or a
            # pending receipt; they can never dispatch the Tool turn twice.
            claim = (
                self.command_receipts
                .reopen_accepted_response_for_compatibility(
                    command_scope="room_message",
                    scope_id=room_id,
                    client_message_id=client_message_id,
                    payload=command_payload,
                    expected_response=claim.replay_response,
                )
            )
            if claim.replay_response is not None:
                return {**claim.replay_response, "idempotentReplay": True}
        try:
            response = self._post_room_message_once(
                room_id,
                message=message,
                client_message_id=client_message_id,
                retry_of_root_id=retry_of_root_id,
                requested_participant_ids=requested_participant_ids,
                work_item_id=work_item_id,
                attachment_ids=attachment_ids,
                answer_to_post_id=answer_to_post_id,
                answer_to_root_id=answer_to_root_id,
            )
        except Exception as exc:
            self.command_receipts.fail(
                claim,
                command_scope="room_message",
                scope_id=room_id,
                client_message_id=client_message_id,
                error=exc,
                cause_code=str(
                    getattr(exc, "cause_code", "")
                    or getattr(exc, "error_code", "")
                    or ""
                ),
            )
            raise
        return self.command_receipts.complete(
            claim,
            command_scope="room_message",
            scope_id=room_id,
            client_message_id=client_message_id,
            response=response,
        )

    def _claim_room_start_gate(
        self,
        room_id: str,
        *,
        message: str,
        client_message_id: str,
        requested_participant_ids: Sequence[str],
        work_item_id: str,
        attachment_ids: Sequence[str],
        retry_of_root_id: str,
    ) -> dict[str, object] | None:
        # Client IDs are the replay boundary for Room commands. Requests from
        # older direct callers without one retain the pre-gate compatibility
        # path; the Control Center always supplies one.
        if not client_message_id:
            return None
        existing_gate = self.room_start_gates.get(room_id)
        if (
            existing_gate is not None
            and existing_gate.get("status") == "confirmed"
            and str(existing_gate.get("clientMessageId") or "") != client_message_id
        ):
            # The Room start boundary is crossed once. A later WorkItem owns a
            # new command receipt, not a new alignment gate. The original
            # client id still reaches `claim()` below so an exact retry can
            # replay the stored first response and a mutated retry is rejected.
            return None
        target_ids = list(requested_participant_ids)
        if not target_ids:
            try:
                _work, owner_id = self.room_work.authoritative_owner(
                    work_item_id,
                    room_id=room_id,
                )
                target_ids = [str(owner_id)]
            except Exception:
                target_ids = []
        gate = self.room_start_gates.claim(
            room_id=room_id,
            objective_text=message,
            client_message_id=client_message_id,
            target_participant_ids=target_ids,
            work_item_id=work_item_id,
            attachment_ids=attachment_ids,
            retry_of_root_id=retry_of_root_id,
        )
        if gate.get("status") == "pending" and not gate.get("idempotentReplay"):
            event = self.room_events.publish(
                room_id=room_id,
                event_type="room_start_confirmation_required",
                payload={
                    "gateId": gate["gateId"],
                    "objective": gate["objective"],
                    "workItemId": gate["workItemId"],
                    "targetParticipantIds": gate["targetParticipantIds"],
                    "requiresConfirmation": True,
                },
                turn_id=str(gate["gateId"]),
                topic_id=str(self.rooms.get(room_id).get("activeTopicId") or ""),
            )
            gate = {**gate, "event": event}
        return gate

    @staticmethod
    def _room_start_confirmation_response(gate: Mapping[str, object]) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.agent-room-message.v1",
            "ok": True,
            "accepted": False,
            "status": "awaiting_confirmation",
            "phase": "alignment",
            "executionOwner": "session",
            "roomId": gate["roomId"],
            "clientMessageId": gate["clientMessageId"],
            "workItemId": gate["workItemId"],
            "startConfirmation": {
                "gateId": gate["gateId"],
                "status": gate["status"],
                "objective": gate["objective"],
                "workItemId": gate["workItemId"],
                "targetParticipantIds": gate["targetParticipantIds"],
                "requiresConfirmation": True,
                "afterConfirmExecutionMode": "room_unrestricted",
            },
            "timelineEvents": ([gate["event"]] if isinstance(gate.get("event"), Mapping) else []),
        }

    def _post_room_message_command(
        self,
        room_id: str,
        *,
        message: str,
        client_message_id: str,
        retry_of_root_id: str,
        requested_participant_ids: Sequence[str],
        work_item_id: str,
        attachment_ids: Sequence[str],
        bypass_start_gate: bool = False,
    ) -> dict[str, object]:
        del bypass_start_gate
        if not client_message_id:
            return self._post_room_message_once(
                room_id,
                message=message,
                client_message_id="",
                retry_of_root_id=retry_of_root_id,
                requested_participant_ids=requested_participant_ids,
                work_item_id=work_item_id,
                attachment_ids=attachment_ids,
                answer_to_post_id="",
                answer_to_root_id="",
            )
        claim = self.command_receipts.begin(
            command_scope="room_message",
            scope_id=room_id,
            client_message_id=client_message_id,
            payload={
                "message": message,
                "retryOfRootId": retry_of_root_id,
                "participantIds": list(requested_participant_ids),
                "workItemId": work_item_id,
                "attachmentIds": list(attachment_ids),
                "answerToPostId": "",
                "answerToRootId": "",
            },
        )
        if claim.replay_response is not None:
            return {**claim.replay_response, "idempotentReplay": True}
        try:
            response = self._post_room_message_once(
                room_id,
                message=message,
                client_message_id=client_message_id,
                retry_of_root_id=retry_of_root_id,
                requested_participant_ids=requested_participant_ids,
                work_item_id=work_item_id,
                attachment_ids=attachment_ids,
                answer_to_post_id="",
                answer_to_root_id="",
            )
        except Exception as exc:
            self.command_receipts.fail(
                claim,
                command_scope="room_message",
                scope_id=room_id,
                client_message_id=client_message_id,
                error=exc,
            )
            raise
        response = self.command_receipts.complete(
            claim,
            command_scope="room_message",
            scope_id=room_id,
            client_message_id=client_message_id,
            response=response,
        )
        self.room_start_gates.complete(
            room_id,
            root_id=str(response.get("roomTurnId") or ""),
            response=response,
        )
        return response

    def execute_room_partner_tool(
        self,
        session_id: str,
        args: Mapping[str, object],
        *,
        tool_call_id: str,
        source_loop_id: str = "",
    ) -> dict[str, object]:
        return self.room_partner_application.execute(
            session_id,
            args,
            tool_call_id=tool_call_id,
            source_loop_id=source_loop_id,
        )

    def room_start_gate(self, room_id: str) -> dict[str, object]:
        gate = self.room_start_gates.get(room_id)
        if gate is None:
            raise KeyError(room_id)
        return gate

    def confirm_room_start(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        gate = self.room_start_gates.get(room_id)
        if gate is None:
            raise KeyError(room_id)
        gate_id = str(payload.get("gateId") or "").strip()
        if gate_id and gate_id != str(gate["gateId"]):
            raise ValueError("Room start gate identity does not match")
        decision = str(payload.get("decision") or payload.get("action") or "confirm").strip().lower()
        if decision in {"reject", "rejected", "cancel"}:
            if gate["status"] == "pending":
                rejected = self.room_start_gates.reject(room_id)
                self.room_events.publish(
                    room_id=room_id,
                    event_type="room_start_confirmation_rejected",
                    payload={"gateId": gate["gateId"], "objective": gate["objective"]},
                    turn_id=str(gate["gateId"]),
                )
            else:
                rejected = gate
            return {
                "schemaVersion": "rag-ime.agent-room-start-gate.v1",
                "ok": True,
                "status": "rejected",
                "gateId": rejected["gateId"],
                "roomId": room_id,
                "idempotentReplay": gate["status"] != "pending",
            }
        confirmed = self.room_start_gates.confirm(room_id)
        # The confirmation is the sole user-facing authorization boundary for
        # ordinary Room work. Persist the overlay before dispatch, and also on
        # idempotent replay so an older or restarted Host repairs participant
        # Sessions instead of falling back to per-Tool approvals.
        self._activate_room_unrestricted_execution(room_id)
        stored = confirmed.get("response")
        if isinstance(stored, Mapping):
            return {**dict(stored), "idempotentReplay": True}
        self.room_events.publish(
            room_id=room_id,
            event_type="room_start_confirmation_confirmed",
            payload={
                "gateId": confirmed["gateId"],
                "objective": confirmed["objective"],
                "workItemId": confirmed["workItemId"],
                "executionMode": ROOM_UNRESTRICTED_EXECUTION_MODE,
            },
            turn_id=str(confirmed["gateId"]),
        )
        response = self._post_room_message_command(
            room_id,
            message=str(confirmed["objective"]),
            client_message_id=str(confirmed["clientMessageId"]),
            retry_of_root_id=str(confirmed.get("retryOfRootId") or ""),
            requested_participant_ids=[
                str(value) for value in confirmed["targetParticipantIds"]
            ],
            work_item_id=str(confirmed["workItemId"]),
            attachment_ids=[
                str(value) for value in confirmed.get("attachmentIds", [])
            ],
            bypass_start_gate=True,
        )
        self.room_start_gates.complete(
            room_id,
            root_id=str(response.get("roomTurnId") or ""),
            response=response,
        )
        return response

    def _activate_room_unrestricted_execution(
        self,
        room_id: str,
        *,
        room: Mapping[str, object] | None = None,
    ) -> int:
        """Align the Room approval overlay with effective partner authority."""

        current_room = room
        if current_room is None:
            try:
                current_room = self.rooms.get(room_id)
            except AgentRoomNotFound:
                return 0
        if str(current_room.get("status") or "") != "active":
            return 0
        room_kind = str(current_room.get("roomKind") or "collaboration")
        if room_kind == "collaboration":
            legacy_gate = self.room_start_gates.get(room_id)
            if (
                legacy_gate is not None
                and legacy_gate.get("status") == "pending"
            ):
                # A Room dispatch is already the user's authority boundary.
                # Pending start-gate rows only exist from older Hosts and must
                # disappear before snapshots can revive an obsolete prompt.
                self.room_start_gates.reject(room_id)
        try:
            permission_policy = normalize_room_permission_policy(
                current_room.get("permissionPolicy"),
                room_kind=room_kind,
                current=current_room,
                legacy_execution_mode=current_room.get("executionMode"),
            )
        except ValueError:
            return 0
        effective_partner_mode = resolve_room_permission_policy(
            permission_policy
        )["partner"]["executionMode"]
        # A Room dispatch is the user's confirmation boundary. Every active
        # non-read-only participant skips the separate human/Luna per-Tool
        # layer, independent of the Room's legacy permission preset.
        target_overlay = (
            ""
            if effective_partner_mode == READ_ONLY_EXECUTION_MODE
            else ROOM_UNRESTRICTED_EXECUTION_MODE
        )
        updated = 0
        for participant in current_room.get("participants", []):
            if not isinstance(participant, Mapping):
                continue
            if str(participant.get("status") or "") != "active":
                continue
            session_id = str(participant.get("sessionId") or "").strip()
            if not session_id:
                continue
            try:
                session = self.sessions.get(session_id)
            except KeyError:
                continue
            if str(session.get("roomExecutionMode") or "") == target_overlay:
                continue
            self.sessions.set_room_execution_mode(
                session_id,
                target_overlay,
            )
            updated += 1
        return updated

    def steer_room_participant(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Durably apply one typed supplement to a running Room participant."""

        root_id = str(payload.get("rootId") or "").strip()
        client_action_id = str(payload.get("clientActionId") or "").strip()
        if not root_id or not client_action_id:
            raise ValueError(
                "participant steer requires rootId and clientActionId"
            )
        receipt_payload = {
            "action": payload.get("action"),
            "rootId": root_id,
            "expectedGeneration": payload.get("expectedGeneration"),
            "participantId": payload.get("participantId"),
            "message": payload.get("message"),
        }
        scope_id = f"{room_id}:{root_id}"
        claim = self.command_receipts.begin(
            command_scope="room_participant_steer",
            scope_id=scope_id,
            client_message_id=client_action_id,
            payload=receipt_payload,
        )
        if claim.replay_response is not None:
            return {**claim.replay_response, "idempotentReplay": True}
        try:
            response = self._steer_room_participant_once(room_id, payload)
        except Exception as exc:
            self.command_receipts.fail(
                claim,
                command_scope="room_participant_steer",
                scope_id=scope_id,
                client_message_id=client_action_id,
                error=exc,
            )
            raise
        response["controlReceipt"] = {
            "state": "accepted",
            "scope": "room_participant_steer",
            "clientActionId": client_action_id,
        }
        return self.command_receipts.complete(
            claim,
            command_scope="room_participant_steer",
            scope_id=scope_id,
            client_message_id=client_action_id,
            response=response,
        )

    def _steer_room_participant_once(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if str(payload.get("action") or "").strip() != "steer_participant":
            raise ValueError(
                "Room participant control action must be steer_participant"
            )
        root_id = str(payload.get("rootId") or "").strip()
        message = str(payload.get("message") or "").strip()
        client_action_id = str(payload.get("clientActionId") or "").strip()
        if not message:
            raise ValueError("participant steer message must not be empty")
        if len(message) > ROOM_MESSAGE_CHAR_LIMIT:
            raise ValueError(
                f"Room message must not exceed {ROOM_MESSAGE_CHAR_LIMIT} characters"
            )
        room = self.rooms.get(room_id)
        events = [
            event
            for event in self.rooms.list_events(room_id, after_sequence=0, limit=2000)
            if str(event.get("turnId") or "") == root_id
        ]
        if not any(str(event.get("eventType") or "") == "user_message" for event in events):
            raise ValueError("Room turn does not belong to this Room")
        routed_participant_ids = {
            str(
                event.get("participantId")
                or (
                    event.get("payload", {}).get("targetParticipantId")
                    if isinstance(event.get("payload"), Mapping)
                    else ""
                )
                or ""
            )
            for event in events
            if str(event.get("eventType") or "") == "route_decision"
        }
        routed_participant_ids.discard("")
        requested_participant_id = str(payload.get("participantId") or "").strip()
        participant_id = requested_participant_id or next(iter(routed_participant_ids), "")
        if not participant_id or participant_id not in routed_participant_ids:
            raise ValueError("participant steer target is not part of this Room turn")
        participant = self.rooms.participant(participant_id)
        if (
            str(participant.get("roomId") or "") != room_id
            or str(participant.get("status") or "") != "active"
        ):
            raise ValueError("participant steer target is no longer active")
        session_id = str(participant.get("sessionId") or "")
        topic_id = str(room.get("activeTopicId") or "")
        room_event = self.room_events.publish(
            room_id=room_id,
            event_type="user_message",
            payload={
                "text": message,
                "delivery": "steer",
                "rootId": root_id,
                "targetParticipantIds": [participant_id],
                "clientActionId": client_action_id,
            },
            turn_id=root_id,
            participant_id=participant_id,
            source_session_id=session_id,
            topic_id=topic_id,
        )
        accepted = self.prompt(
            session_id,
            {
                "message": message,
                "clientMessageId": client_action_id,
                "delivery": "steer",
                "_contextSourceToken": self._context_source_token,
                "_contextSource": "room",
                "_checkpointText": message,
            },
        )
        return {
            "schemaVersion": "rag-ime.agent-room-steer.v1",
            "ok": True,
            "accepted": True,
            "roomId": room_id,
            "rootId": root_id,
            "participantId": participant_id,
            "sessionId": session_id,
            "delivery": "steer",
            "turnId": str(accepted.get("turnId") or ""),
            "event": room_event,
            "sessionReceipt": accepted,
        }

    def _resolve_room_attachments(
        self,
        room_id: str,
        target_session_ids: Sequence[str],
        attachment_ids: Sequence[str],
    ) -> list[dict[str, object]]:
        if not attachment_ids:
            return []
        room = self.rooms.get(room_id)
        if room.get("status") != "active":
            raise ValueError("Room attachments require an active Room")
        active_by_session = {
            str(participant.get("sessionId") or ""): participant
            for participant in room.get("participants", [])
            if (
                isinstance(participant, Mapping)
                and participant.get("status") == "active"
            )
        }
        receipts = [self.media.receipt(media_id, room_id=room_id) for media_id in dict.fromkeys(attachment_ids)]
        if any(receipt.get("mimeType") not in IMAGE_MIME_TYPES | TEXT_MEDIA_MIME_TYPES for receipt in receipts):
            raise ValueError("Agent 当前支持图片和文本附件，请将此文件转换为文本后发送。")
        has_images = any(receipt.get("mimeType") in IMAGE_MIME_TYPES for receipt in receipts)
        for session_id in target_session_ids:
            participant = active_by_session.get(str(session_id))
            if participant is None:
                raise ValueError("Room attachment target is no longer an active participant")
            if not has_images:
                continue
            selected = self.runtime.model_catalog(str(session_id)).get("selected")
            if (
                not isinstance(selected, Mapping)
                or selected.get("supportsImages") is not True
            ):
                name = str(participant.get("displayName") or "所选伙伴")
                raise ValueError(
                    f"{name} 的当前模型不支持图片，请切换模型或移除图片后重试"
                )
        return receipts


    def abort_room_turn(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        room_turn_id = _required_text(payload, "roomTurnId")
        if len(room_turn_id) > 320:
            raise ValueError("roomTurnId must not exceed 320 characters")
        client_request_id = _optional_client_message_id(payload.get("clientRequestId"))
        if not client_request_id:
            raise ValueError("clientRequestId must not be empty")
        claim = self.command_receipts.begin(
            command_scope="room_turn_abort",
            scope_id=room_id,
            client_message_id=client_request_id,
            payload={"roomTurnId": room_turn_id},
        )
        if claim.replay_response is not None:
            return {**claim.replay_response, "idempotentReplay": True}
        try:
            response = self._abort_room_turn_once(
                room_id,
                room_turn_id=room_turn_id,
            )
        except Exception as exc:
            self.command_receipts.fail(
                claim,
                command_scope="room_turn_abort",
                scope_id=room_id,
                client_message_id=client_request_id,
                error=exc,
            )
            raise
        return self.command_receipts.complete(
            claim,
            command_scope="room_turn_abort",
            scope_id=room_id,
            client_message_id=client_request_id,
            response=response,
        )

    def _abort_room_turn_once(
        self,
        room_id: str,
        *,
        room_turn_id: str,
    ) -> dict[str, object]:
        return self.room_cancellation.abort_turn(
            room_id,
            room_turn_id=room_turn_id,
        )

    def _post_room_message_once(
        self,
        room_id: str,
        *,
        message: str,
        client_message_id: str,
        retry_of_root_id: str,
        requested_participant_ids: Sequence[str],
        work_item_id: str,
        attachment_ids: Sequence[str],
        answer_to_post_id: str,
        answer_to_root_id: str,
    ) -> dict[str, object]:
        if answer_to_post_id or answer_to_root_id:
            raise ValueError("Room messages no longer accept clarification reply ids")
        return self.room_dispatch.post_message(
            room_id,
            message=message,
            client_message_id=client_message_id,
            retry_of_root_id=retry_of_root_id,
            requested_participant_ids=requested_participant_ids,
            work_item_id=work_item_id,
            attachment_ids=attachment_ids,
        )

    def send_room_intercom(
        self,
        source_session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_work_application.send_room_intercom(
            source_session_id,
            payload,
        )

    def list_room_intercom(
        self,
        session_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self.room_work_application.list_room_intercom(
            session_id,
            payload,
        )

    def assign_room_work(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_work_application.assign_room_work(
            session_id,
            payload,
        )

    def submit_room_work(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_work_application.submit_room_work(
            session_id,
            payload,
        )

    def accept_room_work(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_work_application.accept_room_work(
            session_id,
            payload,
        )

    def return_room_work(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_work_application.return_room_work(
            session_id,
            payload,
        )

    def block_room_work(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_work_application.block_room_work(
            session_id,
            payload,
        )

    def escalate_room_work(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.room_work_application.escalate_room_work(
            session_id,
            payload,
        )

    def list_room_work(
        self,
        session_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self.room_work_application.list_room_work(
            session_id,
            payload,
        )

    def subscribe_room_events(
        self,
        room_id: str,
        *,
        after_event_id: str = "",
        heartbeat_seconds: float = 10.0,
    ) -> Iterator[bytes]:
        self.rooms.get(room_id)
        return self.room_events.subscribe(
            room_id,
            after_event_id=after_event_id,
            heartbeat_seconds=heartbeat_seconds,
        )


    def model_catalog(self, session_id: str) -> dict[str, object]:
        return self.session_policy.model_catalog(session_id)

    def command_catalog(self, session_id: str) -> dict[str, object]:
        return self.session_policy.command_catalog(session_id)

    def _require_mutable_session(self, session_id: str) -> dict[str, object]:
        session = self.sessions.get(session_id)
        if session.get("evaluationSnapshot") is True:
            raise ValueError("evaluation snapshot is read-only")
        return session

    def invoke_command(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        self._require_mutable_session(session_id)
        return self.session_policy.invoke_command(session_id, payload)

    def select_model(self, session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._require_mutable_session(session_id)
        return self.session_policy.select_model(session_id, payload)

    def select_thinking_level(self, session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._require_mutable_session(session_id)
        return self.session_policy.select_thinking_level(
            session_id,
            payload,
        )

    def update_session(self, session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._require_mutable_session(session_id)
        return self.session_policy.update_session(session_id, payload)

    def delete_session(self, session_id: str) -> dict[str, object]:
        self._require_mutable_session(session_id)
        self.background_jobs.cancel_session(
            session_id,
            reason="agent_session_deleted",
        )
        return self.session_application.delete_session(session_id)

    def fork_candidates(self, session_id: str) -> dict[str, object]:
        return self.session_branching.fork_candidates(session_id)

    def fork_session(self, session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._require_mutable_session(session_id)
        return self.session_branching.fork_session(
            session_id,
            payload,
        )

    def rewrite_session(self, session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._require_mutable_session(session_id)
        return self.session_branching.rewrite_session(
            session_id,
            payload,
        )

    def _rewrite_session_once(
        self,
        *,
        session_id: str,
        entry_id: str,
        message: str,
        attachment_ids: list[str],
        client_message_id: str,
    ) -> dict[str, object]:
        return self.session_branching.rewrite_session_once(
            session_id=session_id,
            entry_id=entry_id,
            message=message,
            attachment_ids=attachment_ids,
            client_message_id=client_message_id,
        )

    def _rewritable_session(self, session_id: str) -> dict[str, object]:
        return self.session_branching.rewritable_session(session_id)

    def _forkable_session(self, session_id: str) -> dict[str, object]:
        return self.session_branching.forkable_session(session_id)

    def _room_public_messages_for_session(
        self,
        session_id: str,
    ) -> dict[str, object] | None:
        participant = self.rooms.participant_for_session(
            session_id,
            active_only=False,
        )
        if participant is None:
            return None
        room_id = str(participant.get("roomId") or "")
        participant_id = str(participant.get("id") or "")
        if not room_id or not participant_id:
            return None
        events = self.rooms.list_events(
            room_id,
            limit=2_000,
        )
        return {
            "participantId": participant_id,
            "events": [
                event
                for event in events
                if event.get("eventType") == "user_message"
                or (
                    event.get("eventType") == "room_post"
                    and str(event.get("participantId") or "")
                    == participant_id
                )
            ],
        }

    def _recent_room_public_messages_for_session(
        self,
        session_id: str,
    ) -> dict[str, object] | None:
        participant = self.rooms.participant_for_session(
            session_id,
            active_only=False,
        )
        if participant is None:
            return None
        room_id = str(participant.get("roomId") or "")
        participant_id = str(participant.get("id") or "")
        if not room_id or not participant_id:
            return None
        events = self.rooms.recent_public_messages(
            room_id,
            limit=100,
        )
        return {
            "participantId": participant_id,
            "events": [
                event
                for event in events
                if event.get("eventType") == "user_message"
                or (
                    event.get("eventType") == "room_post"
                    and str(event.get("participantId") or "")
                    == participant_id
                )
            ],
        }


    def messages(self, session_id: str) -> dict[str, object]:
        return self.message_snapshot.messages(session_id)

    def import_media(
        self,
        *,
        session_id: str = "",
        room_id: str = "",
        data: bytes,
        mime_type: str,
        file_name: str = "",
    ) -> dict[str, object]:
        owner_type, owner_id = _media_owner_input(
            session_id=session_id,
            room_id=room_id,
        )
        if owner_type == "session":
            self.sessions.get(owner_id)
        else:
            room = self.rooms.get(owner_id)
            if room.get("status") != "active":
                raise ValueError("Room attachments require an active Room")
        return {
            "schemaVersion": "rag-ime.agent-media-import.v1",
            "ok": True,
            "media": self.media.import_bytes(
                session_id=owner_id if owner_type == "session" else "",
                room_id=owner_id if owner_type == "room" else "",
                data=data,
                mime_type=mime_type,
                file_name=file_name,
            ),
        }

    def list_media(self, payload: Mapping[str, object]) -> dict[str, object]:
        owner_type, owner_id = _media_owner_input(
            session_id=payload.get("sessionId"),
            room_id=payload.get("roomId"),
        )
        if owner_type == "session":
            self.sessions.get(owner_id)
        else:
            self.rooms.get(owner_id)
        return {
            "schemaVersion": "rag-ime.agent-media-list.v1",
            "ok": True,
            "ownerType": owner_type,
            "ownerId": owner_id,
            "items": self.media.list_for_owner(
                session_id=owner_id if owner_type == "session" else "",
                room_id=owner_id if owner_type == "room" else "",
                limit=_integer(payload.get("limit"), default=100, minimum=1, maximum=500),
            ),
        }

    def media_receipt(
        self,
        media_id: str,
        *,
        session_id: str = "",
        room_id: str = "",
    ) -> dict[str, object]:
        owner_type, owner_id = _media_owner_input(
            session_id=session_id,
            room_id=room_id,
        )
        if owner_type == "session":
            self.sessions.get(owner_id)
        else:
            self.rooms.get(owner_id)
        return {
            "schemaVersion": "rag-ime.agent-media-get.v1",
            "ok": True,
            "media": self.media.receipt(
                media_id,
                session_id=owner_id if owner_type == "session" else "",
                room_id=owner_id if owner_type == "room" else "",
            ),
        }

    def read_media_resource(
        self,
        media_id: str,
        *,
        session_id: str,
    ) -> tuple[dict[str, object], bytes]:
        """Session Tools can read their own files and their active Room's files."""
        self.sessions.get(session_id)
        try:
            return self.media.read(media_id, session_id=session_id)
        except KeyError:
            participant = self.rooms.participant_for_session(session_id)
            if not participant or participant.get("status") != "active":
                raise
            return self.media.read(media_id, room_id=str(participant["roomId"]))

    def media_content(
        self,
        media_id: str,
        *,
        session_id: str = "",
        room_id: str = "",
    ) -> tuple[dict[str, object], bytes]:
        owner_type, owner_id = _media_owner_input(
            session_id=session_id,
            room_id=room_id,
        )
        if owner_type == "session":
            return self.read_media_resource(media_id, session_id=owner_id)
        else:
            self.rooms.get(owner_id)
        return self.media.read(
            media_id,
            session_id=owner_id if owner_type == "session" else "",
            room_id=owner_id if owner_type == "room" else "",
        )

    def file_preview(
        self,
        media_id: str,
        *,
        session_id: str,
        expected_sha256: str = "",
    ) -> dict[str, object]:
        self.sessions.get(session_id)
        receipt, _ = self.read_media_resource(media_id, session_id=session_id)
        return self.file_previews.read(
            media_id,
            session_id=session_id,
            expected_sha256=expected_sha256,
            room_id=str(receipt.get("roomId") or ""),
        )

    def prompt(self, session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._require_mutable_session(session_id)
        return self.prompt_application.prompt(
            session_id,
            payload,
        )

    def deep_search(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self.prompt_application.deep_search(payload)

    def _deep_search_session(
        self,
        runtime: Mapping[str, object],
    ) -> tuple[dict[str, object], bool]:
        return self.prompt_application.deep_search_session(
            runtime
        )

    def _prompt_with_checkpoint(
        self,
        *,
        session_id: str,
        message: str,
        checkpoint_text: str,
        attachment_ids: list[str],
        client_message_id: str = "",
        retry_of_client_message_id: str = "",
        context_source: str = "user",
        delivery: str = "prompt",
        transient_context: str = "",
        media_owner_room_id: str = "",
        on_accepted: (
            Callable[[Mapping[str, object]], None] | None
        ) = None,
    ) -> dict[str, object]:
        if media_owner_room_id:
            if context_source != "room":
                raise ValueError(
                    "Room-owned media requires a Room prompt context"
                )
            self._resolve_room_attachments(
                media_owner_room_id,
                [session_id],
                attachment_ids,
            )
        elif context_source == "room" and attachment_ids:
            raise ValueError(
                "Room prompt attachments require a Room media owner"
            )
        if context_source == "room":
            return self.prompt_application.prompt_with_checkpoint(
                session_id=session_id,
                message=message,
                checkpoint_text=checkpoint_text,
                attachment_ids=attachment_ids,
                client_message_id=client_message_id,
                retry_of_client_message_id=(
                    retry_of_client_message_id
                ),
                context_source=context_source,
                delivery=delivery,
                transient_context=transient_context,
                media_owner_room_id=media_owner_room_id,
                on_accepted=on_accepted,
            )
        with self._direct_agent_entry(
            session_id,
            continuation=delivery != "prompt",
        ):
            return self.prompt_application.prompt_with_checkpoint(
                session_id=session_id,
                message=message,
                checkpoint_text=checkpoint_text,
                attachment_ids=attachment_ids,
                client_message_id=client_message_id,
                retry_of_client_message_id=(
                    retry_of_client_message_id
                ),
                context_source=context_source,
                delivery=delivery,
                transient_context=transient_context,
                media_owner_room_id=media_owner_room_id,
                on_accepted=on_accepted,
            )

    @contextmanager
    def _direct_agent_entry(
        self,
        session_id: str,
        *,
        continuation: bool = False,
    ) -> Iterator[None]:
        claim = (
            self.session_mode_gate.claim_agent_continuation(
                session_id
            )
            if continuation
            else self.session_mode_gate.claim_agent(session_id)
        )
        with claim:
            self._assert_direct_agent_prompt_available(session_id)
            if not continuation:
                self.room_turns.hold_priority((session_id,))
            try:
                yield
            finally:
                if not continuation:
                    self.room_turns.release_priority_session(
                        session_id
                    )

    def _assert_direct_agent_prompt_available(
        self,
        session_id: str,
    ) -> None:
        room_busy = self.room_turns.session_turn_active(
            session_id
        )
        if not room_busy:
            return
        raise AgentTurnConflictError(
            "Session 正在执行 Room 任务，不能同时从 Agent 发送；"
            "请等待 Room 结束或先停止该 Room 任务"
        )

    def _runtime_prompt_with_context(
        self,
        session_id: str,
        message: str,
        *,
        images: list[Mapping[str, str]] | None = None,
        client_message_id: str = "",
        source_kind: str,
        delivery: str = "prompt",
        transient_context: str = "",
    ) -> tuple[dict[str, object], str, int]:
        return self.prompt_delivery_application.deliver(
            session_id,
            message,
            images=images,
            client_message_id=client_message_id,
            source_kind=source_kind,
            delivery=delivery,
            transient_context=transient_context,
        )

    def _pending_memory_bootstrap(
        self,
        session: Mapping[str, object],
    ) -> dict[str, object]:
        return self.memory_context_application.pending_bootstrap(
            session
        )

    def _ensure_memory_bootstrap(
        self,
        session: Mapping[str, object],
        *,
        query_text: str,
    ) -> dict[str, object]:
        return self.memory_context_application.ensure_bootstrap(
            session,
            query_text=query_text,
        )

    def refresh_session_context(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.memory_context_application.refresh(payload)

    def _replace_recent_recall_messages(
        self,
        session_id: str,
        messages: Sequence[Mapping[str, object]],
    ) -> None:
        self.memory_context_application.replace_recent_messages(
            session_id,
            messages,
        )

    def _append_recent_recall_message(
        self,
        session_id: str,
        message: Mapping[str, object],
    ) -> None:
        self.memory_context_application.append_recent_message(
            session_id,
            message,
        )

    def _recent_recall_messages(self, session_id: str) -> list[dict[str, object]]:
        return self.memory_context_application.recent_messages(
            session_id
        )

    def _record_user_evidence_safely(
        self,
        *,
        session_id: str,
        pi_entry_id: str,
        turn_id: str,
        text: str,
    ) -> dict[str, object]:
        return self.memory_evidence_application.record_user(
            session_id=session_id,
            pi_entry_id=pi_entry_id,
            turn_id=turn_id,
            text=text,
        )

    def _record_assistant_evidence_safely(
        self,
        event: AgentEventEnvelope,
    ) -> dict[str, object]:
        return self.memory_evidence_application.record_assistant(
            event
        )

    def _record_tool_receipt_evidence_safely(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        return self.memory_evidence_application.record_tool_receipt(
            approval
        )

    def _record_room_evidence_safely(
        self,
        *,
        room_id: str,
        room_event: Mapping[str, object],
        text: str,
        role_id: str,
        session_id: str,
        event_type: str,
        accepted: bool,
    ) -> dict[str, object]:
        return self.memory_evidence_application.record_room(
            room_id=room_id,
            room_event=room_event,
            text=text,
            role_id=role_id,
            session_id=session_id,
            event_type=event_type,
            accepted=accepted,
        )

    def abort(self, session_id: str) -> dict[str, object]:
        self._require_mutable_session(session_id)
        return self.session_application.abort(session_id)

    def compact(self, session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._require_mutable_session(session_id)
        return self.session_application.compact(session_id, payload)

    def _checkpoint_runtime_compaction(
        self,
        session_id: str,
        result: Mapping[str, object],
        trigger: str,
    ) -> Mapping[str, object]:
        summary = _compaction_summary(result)
        material = json.dumps(
            {
                "sessionId": session_id,
                "summary": summary,
                "firstKeptEntryId": str(result.get("firstKeptEntryId") or ""),
                "trigger": compact_whitespace(trigger) or "automatic",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        # Pi's Session JSONL is the authoritative compaction checkpoint. A
        # lossy summary may guide the immediate post-compaction recall, but it
        # is not evidence and must never silently enter long-term memory.
        return {
            "schemaVersion": "rag-ime.agent-memory-checkpoint.v1",
            "ok": True,
            "stored": False,
            "status": "session_context_only",
            "summarySha256": hashlib.sha256(summary.encode("utf-8")).hexdigest()
            if summary
            else "",
            "checkpointKey": hashlib.sha256(material.encode("utf-8")).hexdigest()[:32],
            "longTermMemoryEligible": False,
        }

    def _probe_memory_maintenance(self, session_id: str, *, trigger: str) -> dict[str, object]:
        if self._memory_maintenance_probe is None:
            return {
                "schemaVersion": "rag-ime.agent-memory-maintenance-probe.v1",
                "ok": False,
                "available": False,
                "trigger": trigger,
            }
        try:
            status = dict(
                self._memory_maintenance_probe(
                    {"project": self.project, "limit": 5, "trigger": trigger}
                )
            )
        except Exception as exc:
            return {
                "schemaVersion": "rag-ime.agent-memory-maintenance-probe.v1",
                "ok": False,
                "available": True,
                "trigger": trigger,
                "error": _public_error(exc),
            }
        compile_state = status.get("compileState") if isinstance(status.get("compileState"), Mapping) else {}
        legacy_pending = _integer(
            compile_state.get("pendingEventCount"),
            default=0,
            minimum=0,
            maximum=2_147_483_647,
        )
        owner_curation = (
            status.get("ownerCuration")
            if isinstance(status.get("ownerCuration"), Mapping)
            else {}
        )
        pending = _integer(
            owner_curation.get("pendingSourceCount"),
            default=legacy_pending,
            minimum=0,
            maximum=2_147_483_647,
        )
        needs_review = _integer(
            owner_curation.get("needsReviewSourceCount"),
            default=0,
            minimum=0,
            maximum=2_147_483_647,
        )
        drafts = _integer(
            status.get("pendingDraftCount"),
            default=0,
            minimum=0,
            maximum=2_147_483_647,
        )
        due = status.get("due") is True
        summary = (
            f"记忆整理已达到触发条件，当前有 {pending} 条受治理证据、{needs_review} 条待人工判定"
            if due
            else f"已检查记忆整理状态：{pending} 条受治理证据、{needs_review} 条待人工判定、{drafts} 份待审草案"
        )
        self.events.publish(
            session_id,
            "memory_maintenance_updated",
            {
                "trigger": trigger,
                "due": due,
                "dueReason": str(status.get("dueReason") or ""),
                "pendingEventCount": pending,
                "pendingSourceCount": pending,
                "needsReviewSourceCount": needs_review,
                "legacyPendingEventCount": legacy_pending,
                "pendingDraftCount": drafts,
                "summary": summary,
            },
        )
        return {**status, "trigger": trigger}

    def list_approvals(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self.approval_application.list_approvals(payload)

    def resolve_review(self, session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._require_mutable_session(session_id)
        return self.approval_application.resolve_review(session_id, payload)

    def resolve_ui_request(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        self._require_mutable_session(session_id)
        request_id = str(payload.get("requestId") or "").strip()
        if not request_id:
            raise ValueError("UI requestId must not be empty")
        response = {
            key: payload[key]
            for key in (
                "value",
                "confirmed",
                "cancelled",
                "resolutionSource",
            )
            if key in payload
        }
        if response.get("cancelled") is not True and not (
            "value" in response or isinstance(response.get("confirmed"), bool)
        ):
            raise ValueError("UI response must include a value, confirmation, or cancellation")
        return self.runtime.resolve_ui_request(
            session_id,
            request_id,
            response=response,
        )

    def decide_approval(self, approval_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        return self.approval_application.decide_approval(approval_id, payload)

    def _finish_approval_decision(
        self,
        approval: Mapping[str, object],
        *,
        pending_in_pi: bool,
    ) -> dict[str, object]:
        return self.approval_application.finish_decision(
            approval,
            pending_in_pi=pending_in_pi,
        )

    def auto_approve_pending(self, approval: Mapping[str, object]) -> dict[str, object]:
        return self.approval_application.auto_approve_pending(approval)

    def _execute_approved_operation(
        self,
        decided: Mapping[str, object],
    ) -> dict[str, object]:
        return self.approval_application.execute_approved(decided)

    def _checkpoint_applied_approval(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        return self.approval_application.checkpoint_applied(approval)

    def _finish_terminal_approval(
        self,
        approval: Mapping[str, object],
        *,
        pending_in_pi: bool,
    ) -> dict[str, object]:
        return self.approval_application.finish_terminal(
            approval,
            pending_in_pi=pending_in_pi,
        )

    def finalize_external_approval(
        self,
        approval_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self.approval_application.finalize_external(
            approval_id,
            payload,
        )

    def approval_result(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self.approval_application.approval_result(payload)

    def list_memory_sources(self, payload: Mapping[str, object]) -> dict[str, object]:
        session_id = _required_text(payload, "sessionId")
        self.sessions.get(session_id)
        return {
            "schemaVersion": "rag-ime.agent-memory-source-list.v1",
            "ok": True,
            "sessionId": session_id,
            "items": self.memory_sources.list_for_session(
                session_id,
                limit=_integer(payload.get("limit"), default=100, minimum=1, maximum=500),
            ),
        }

    def subscribe_events(
        self,
        session_id: str,
        *,
        after_event_id: str = "",
        heartbeat_seconds: float = 10.0,
    ) -> Iterator[bytes]:
        self.sessions.get(session_id)
        return self.events.subscribe(
            session_id,
            after_event_id=after_event_id,
            heartbeat_seconds=heartbeat_seconds,
        )

    def subscribe_control_events(
        self,
        *,
        after_event_id: str = "",
        heartbeat_seconds: float = 10.0,
    ) -> Iterator[bytes]:
        return self.control_events.subscribe(
            after_event_id=after_event_id,
            heartbeat_seconds=heartbeat_seconds,
        )

    def observation_snapshot(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self.observations.snapshot(payload)

    def _trace_diagnostic_source_environment(
        self,
        kind: str,
        identifier: str,
    ) -> Mapping[str, object] | None:
        """Resolve project policy from the selected source, never UI hints."""

        source: Mapping[str, object] | None = None
        try:
            if kind == "session":
                source = self.sessions.get(identifier)
            elif kind == "room":
                room_snapshot = self.room_snapshot(identifier)
                room = room_snapshot.get("room")
                source = room if isinstance(room, Mapping) else None
            elif kind == "run":
                observation = self.observation_snapshot(
                    {"runId": identifier, "limit": 100}
                )
                items = observation.get("items")
                session_ids = (
                    {
                        str(item.get("sessionId") or "").strip()
                        for item in items
                        if isinstance(item, Mapping)
                        and str(item.get("sessionId") or "").strip()
                    }
                    if isinstance(items, Sequence)
                    and not isinstance(items, (str, bytes, bytearray))
                    else set()
                )
                if len(session_ids) == 1:
                    source = self.sessions.get(next(iter(session_ids)))
        except (KeyError, ValueError):
            return None
        if not isinstance(source, Mapping):
            return None
        projected = dict(source)
        roots = projected.get("workspaceRoots")
        if isinstance(roots, Sequence) and not isinstance(
            roots, (str, bytes, bytearray)
        ):
            projected["workspaceScopeSha256"] = workspace_scope_sha256(roots)
        return projected

    def _trace_diagnostic_project_roots(
        self,
        targets: Sequence[Mapping[str, object]],
    ) -> list[str]:
        """Return the ordered union of authoritative non-system source roots."""

        roots: list[str] = []
        for target in targets:
            environment = self._trace_diagnostic_source_environment(
                str(target.get("kind") or ""),
                str(target.get("id") or ""),
            )
            if not isinstance(environment, Mapping):
                continue
            source_roots = environment.get("workspaceRoots")
            if not isinstance(source_roots, Sequence) or isinstance(
                source_roots, (str, bytes, bytearray)
            ):
                continue
            for value in source_roots:
                root = str(value or "").strip()
                if root and root != "/" and root not in roots:
                    roots.append(root)
        return roots

    def _assert_trace_diagnostic_project_binding(
        self,
        session: Mapping[str, object],
        targets: Sequence[Mapping[str, object]],
        *,
        frozen_environment: Mapping[str, object] | None = None,
    ) -> None:
        """Require exact source-project identity beside the `/` capability."""

        for target in targets:
            environment = self._trace_diagnostic_source_environment(
                str(target.get("kind") or ""),
                str(target.get("id") or ""),
            )
            source_roots = environment.get("workspaceRoots") if environment else None
            if (
                not isinstance(source_roots, Sequence)
                or isinstance(source_roots, (str, bytes, bytearray))
                or not any(str(root or "").strip() not in {"", "/"} for root in source_roots)
            ):
                raise ValueError(
                    "binding_required: select a project for every Trace source before diagnosis"
                )
        expected_roots = self._trace_diagnostic_project_roots(targets)
        if not expected_roots:
            raise ValueError(
                "binding_required: select a project for every Trace source before diagnosis"
            )
        actual_roots = [
            str(root or "").strip()
            for root in session.get("workspaceRoots", [])
            if str(root or "").strip() and str(root or "").strip() != "/"
        ]
        if actual_roots != expected_roots:
            raise ValueError(
                "Trace diagnostic Session project workspace binding does not match frozen source targets"
            )
        if not isinstance(frozen_environment, Mapping):
            return
        frozen_rows = frozen_environment.get("targets")
        if not isinstance(frozen_rows, Sequence) or isinstance(
            frozen_rows, (str, bytes, bytearray)
        ):
            raise ValueError("Trace diagnostic frozen project workspace binding is unavailable")
        frozen_by_key = {
            str(row.get("targetKey") or ""): str(
                row.get("workspaceScopeSha256") or ""
            )
            for row in frozen_rows
            if isinstance(row, Mapping)
        }
        for target in targets:
            target_key = str(target.get("targetKey") or "")
            if target_key not in frozen_by_key:
                raise ValueError(
                    "Trace diagnostic frozen project workspace binding is incomplete"
                )
            environment = self._trace_diagnostic_source_environment(
                str(target.get("kind") or ""),
                str(target.get("id") or ""),
            )
            roots = environment.get("workspaceRoots") if environment else []
            current_hash = (
                workspace_scope_sha256(roots)
                if isinstance(roots, Sequence)
                and not isinstance(roots, (str, bytes, bytearray))
                else ""
            )
            if current_hash != frozen_by_key[target_key]:
                raise ValueError(
                    "Trace diagnostic source project workspace binding changed after inspection"
                )

    def trace_diagnostic_inspection(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        raw_targets = payload.get("targets")
        if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, (str, bytes, bytearray)):
            raise ValueError("Trace diagnostic targets must be an array")
        targets = _strict_trace_diagnostic_targets(raw_targets)
        session_id = str(payload.get("_sessionId") or "")
        if session_id:
            owned = self.trace_diagnostic_reports.for_diagnostic_session(session_id)
            if owned is not None:
                identity = lambda rows: [(row["kind"], row["id"], tuple(row.get("traceIds") or [])) for row in rows]
                if identity(targets) != identity(owned["targets"]):
                    raise ValueError("inspection targets differ from the user-selected report")
                return dict(owned["inspection"])

        def trace_reader(trace_id: str) -> Mapping[str, object] | None:
            try:
                return self.observation_trace({"traceId": trace_id})
            except KeyError:
                return None

        def eval_reader(trace_id: str) -> Sequence[Mapping[str, object]]:
            result = self.observation_evals({"traceId": trace_id, "limit": 100})
            items = result.get("items")
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
                raise ValueError("Trace diagnostic Eval projection must contain an items array")
            if any(not isinstance(item, Mapping) for item in items):
                raise ValueError("Trace diagnostic Eval items must be objects")
            return list(items)

        def environment_reader(kind: str, identifier: str) -> Mapping[str, object] | None:
            return self._trace_diagnostic_source_environment(kind, identifier)

        return inspect_trace_targets(
            targets=targets,
            session_reader=self.messages,
            room_reader=self.room_snapshot,
            observation_reader=self.observation_snapshot,
            trace_reader=trace_reader,
            eval_reader=eval_reader,
            environment_reader=environment_reader,
            intent=payload.get("intent"),
        )

    def create_trace_diagnostic_report(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        diagnostic_session_id = _required_text(payload, "diagnosticSessionId")
        session = self.sessions.get(diagnostic_session_id)
        # A diagnostic report is owned by the dedicated Trace Agent policy:
        # full-auto coordinator authority is what permits transcript and
        # external-file reads through the root workspace without per-read
        # approval. Do not accept an ordinary Session merely because it is
        # writable.
        if not _trace_diagnostic_session_policy_active(
            session,
            expected_surface_key="diagnostic",
        ):
            raise ValueError(
                "Trace diagnostic report requires the explicit full-trust diagnostic Session policy"
            )
        inspection = self.trace_diagnostic_inspection(payload)
        self._assert_trace_diagnostic_project_binding(
            session,
            [
                target
                for target in inspection.get("targets", [])
                if isinstance(target, Mapping)
            ],
        )
        title = str(payload.get("title") or "Trace 诊断报告")
        project_id = self._optimization_application().ensure_project(
            self._trace_diagnostic_project_roots(inspection["targets"])
        )
        return self.trace_diagnostic_reports.create(
            diagnostic_session_id=diagnostic_session_id,
            title=title,
            targets=inspection["targets"],
            inspection=inspection,
            intent=inspection.get("intent"),
            optimization_project_id=project_id,
        )

    def finalize_trace_diagnostic_report(
        self,
        report_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        report = self.trace_diagnostic_reports.get(report_id)
        if report is None:
            raise KeyError(report_id)
        session_id = str(report["diagnosticSessionId"])
        expected_revision = payload.get("expectedRevision")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or not 1 <= expected_revision <= 1_000_000
        ):
            raise ValueError("Trace diagnostic finalize requires expectedRevision")
        # A Session's public lifecycle is idle/active/busy/faulted/archived;
        # ``completed`` is a Trace/message status, not a Session status. A
        # faulted diagnostic must become a durable failed report even when the
        # Runtime can no longer produce a readable transcript snapshot.
        session = self.sessions.get(session_id)
        failure_statuses = {"faulted", "failed", "error", "cancelled", "canceled"}
        session_status = str(session.get("status") or "idle")
        if session_status in failure_statuses:
            return self.trace_diagnostic_reports.fail(
                report_id,
                expected_revision=expected_revision,
                reason=_diagnostic_failure_reason(session, status=session_status),
            )
        snapshot = self.messages(session_id)
        snapshot_status = str(snapshot.get("status") or session_status)
        if snapshot_status in failure_statuses:
            return self.trace_diagnostic_reports.fail(
                report_id,
                expected_revision=expected_revision,
                reason=_diagnostic_failure_reason(
                    snapshot,
                    session=session,
                    status=snapshot_status,
                ),
            )
        if snapshot_status not in {"idle", "archived"}:
            raise ValueError("diagnostic Session is not complete")
        try:
            result = extract_trace_diagnostic_result(snapshot)
        except ValueError:
            return self.trace_diagnostic_reports.fail(
                report_id,
                expected_revision=expected_revision,
                reason="诊断 Session 未生成可校验的结构化报告。",
            )
        try:
            completed = self.trace_diagnostic_reports.complete(
                report_id,
                expected_revision=expected_revision,
                result=result,
            )
            self._optimization_application().record_report(completed)
            return completed
        except ValueError as exc:
            if not str(exc).startswith("unknown evidenceId:"):
                raise
            return self.trace_diagnostic_reports.fail(
                report_id,
                expected_revision=expected_revision,
                reason="结构化诊断报告引用了不属于冻结范围的证据。",
            )

    def _observe_trace_diagnostic_terminal_event(
        self,
        event: AgentEventEnvelope,
    ) -> None:
        if event.event_type not in {"turn_completed", "turn_failed"}:
            return
        report = self.trace_diagnostic_reports.for_diagnostic_session(
            event.session_id
        )
        if report is None or report.get("status") != "generating":
            return
        self.finalize_trace_diagnostic_report(
            str(report["reportId"]),
            {"expectedRevision": int(report["revision"])},
        )

    def _reconcile_trace_diagnostic_report(
        self,
        report: Mapping[str, object],
    ) -> dict[str, object]:
        """Recover a terminal result left generating by an older frontend.

        A plain idle Session is not enough: report creation briefly precedes
        the first prompt. Lazy reconciliation therefore requires either a
        terminal failure status or a complete structured envelope.
        """

        if report.get("status") != "generating":
            return dict(report)
        session_id = str(report.get("diagnosticSessionId") or "")
        session = self.sessions.get(session_id)
        failure_statuses = {
            "faulted",
            "failed",
            "error",
            "cancelled",
            "canceled",
        }
        session_status = str(session.get("status") or "")
        if session_status in failure_statuses:
            return self.finalize_trace_diagnostic_report(
                str(report["reportId"]),
                {"expectedRevision": int(report["revision"])},
            )
        if session_status not in {"idle", "archived"}:
            return dict(report)
        snapshot = self.messages(session_id)
        try:
            extract_trace_diagnostic_result(snapshot)
        except ValueError:
            # Report creation intentionally precedes the first prompt, so an
            # empty idle snapshot is still a valid pre-admission race. Once a
            # durable public user message exists, however, idle proves that a
            # diagnostic turn was admitted and has already settled. Leaving
            # that report in ``generating`` would make a missing/invalid result
            # permanent (for example after a long tool-only turn whose final
            # answer was lost).
            items = snapshot.get("items")
            report_created_at_ms = report.get("createdAtMs")
            admitted = (
                isinstance(items, Sequence)
                and not isinstance(items, (str, bytes, bytearray))
                and isinstance(report_created_at_ms, int)
                and not isinstance(report_created_at_ms, bool)
                and any(
                    isinstance(item, Mapping)
                    and str(item.get("role") or "") == "user"
                    and isinstance(item.get("createdAtMs"), int)
                    and not isinstance(item.get("createdAtMs"), bool)
                    and int(item["createdAtMs"]) >= report_created_at_ms
                    for item in items
                )
            )
            if admitted:
                return self.trace_diagnostic_reports.fail(
                    str(report["reportId"]),
                    expected_revision=int(report["revision"]),
                    reason="诊断 Session 未生成可校验的结构化报告。",
                )
            return dict(report)
        return self.finalize_trace_diagnostic_report(
            str(report["reportId"]),
            {"expectedRevision": int(report["revision"])},
        )

    def authorize_trace_diagnostic_repair(
        self,
        report_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Persist a user-approved repair handoff, never write authority."""

        expected_revision = _trace_diagnostic_expected_revision(payload)
        repair_session_id = _required_text(payload, "repairSessionId")
        repair_session = self.sessions.get(repair_session_id)
        if not _trace_diagnostic_session_policy_active(
            repair_session,
            expected_surface_key="repair",
        ):
            raise ValueError(
                "Trace diagnostic repair handoff requires the unrestricted auto-approve profile"
            )
        source_scope = _required_text(payload, "sourceScope")
        source_trace_id = _required_text(payload, "sourceTraceId")
        report = self.trace_diagnostic_reports.get(report_id)
        if report is not None:
            targets = report.get("targets")
            report_targets = (
                [target for target in targets if isinstance(target, Mapping)]
                if isinstance(targets, Sequence)
                and not isinstance(targets, (str, bytes, bytearray))
                else []
            )
            inspection = report.get("inspection")
            environment = (
                inspection.get("environment")
                if isinstance(inspection, Mapping)
                else None
            )
            diagnostic_session_id = str(
                report.get("diagnosticSessionId") or ""
            ).strip()
            if diagnostic_session_id:
                diagnostic_session = self.sessions.get(diagnostic_session_id)
                self._assert_trace_diagnostic_project_binding(
                    diagnostic_session,
                    report_targets,
                    frozen_environment=(
                        environment if isinstance(environment, Mapping) else None
                    ),
                )
            self._assert_trace_diagnostic_project_binding(
                repair_session,
                report_targets,
                frozen_environment=(
                    environment if isinstance(environment, Mapping) else None
                ),
            )
            source_target = next(
                (
                    target
                    for target in report_targets
                    if isinstance(target, Mapping)
                    and str(target.get("targetKey") or "") == source_scope
                ),
                None,
            )
            if isinstance(source_target, Mapping):
                source_kind = str(source_target.get("kind") or "")
                source_id = str(source_target.get("id") or "")
                frozen_trace_ids = source_target.get("traceIds")
                if (
                    not isinstance(frozen_trace_ids, Sequence)
                    or isinstance(frozen_trace_ids, (str, bytes, bytearray))
                    or source_trace_id
                    not in {str(trace_id) for trace_id in frozen_trace_ids}
                ):
                    raise ValueError(
                        "Trace diagnostic source Trace is outside the frozen "
                        f"{source_kind} target"
                    )

                if source_kind == "session":
                    # The target row is the frozen authority. A canonical
                    # Trace binding, when one is available, must agree with
                    # that row rather than silently rebinding the handoff.
                    self.sessions.get(source_id)
                    try:
                        trace_projection = self.observation_trace(
                            {"traceId": source_trace_id}
                        )
                    except KeyError:
                        trace_projection = None
                    trace = (
                        trace_projection.get("trace")
                        if isinstance(trace_projection, Mapping)
                        else None
                    )
                    binding = trace.get("binding") if isinstance(trace, Mapping) else None
                    if isinstance(binding, Mapping):
                        bound_session_id = str(binding.get("sessionId") or "").strip()
                        if bound_session_id and bound_session_id != source_id:
                            raise ValueError(
                                "Trace diagnostic session binding does not match "
                                "the source session"
                            )
                elif source_kind == "room":
                    self.rooms.get(source_id)
                    try:
                        trace_projection = self.observation_trace(
                            {"traceId": source_trace_id}
                        )
                    except KeyError:
                        trace_projection = None
                    trace = (
                        trace_projection.get("trace")
                        if isinstance(trace_projection, Mapping)
                        else None
                    )
                    binding = trace.get("binding") if isinstance(trace, Mapping) else None
                    if isinstance(binding, Mapping):
                        bound_room_id = str(binding.get("roomId") or "").strip()
                        if bound_room_id and bound_room_id != source_id:
                            raise ValueError(
                                "Trace diagnostic room binding does not match "
                                "the source room"
                            )
                elif source_kind == "run":
                    try:
                        trace_projection = self.observation_trace(
                            {"traceId": source_trace_id}
                        )
                    except KeyError as exc:
                        raise ValueError(
                            "Trace diagnostic run has no canonical Session binding"
                        ) from exc
                    trace = (
                        trace_projection.get("trace")
                        if isinstance(trace_projection, Mapping)
                        else None
                    )
                    binding = trace.get("binding") if isinstance(trace, Mapping) else None
                    if not isinstance(binding, Mapping):
                        raise ValueError(
                            "Trace diagnostic run has no canonical Session binding"
                        )
                    if str(binding.get("runId") or "") != source_id:
                        raise ValueError(
                            "Trace diagnostic run binding does not match the source run"
                        )
                    source_session_id = str(binding.get("sessionId") or "").strip()
                    if not source_session_id:
                        raise ValueError(
                            "Trace diagnostic run has no canonical Session binding"
                        )
                    try:
                        self.sessions.get(source_session_id)
                    except KeyError as exc:
                        raise ValueError(
                            "Trace diagnostic run canonical Session is unavailable"
                        ) from exc
        return self.trace_diagnostic_reports.authorize_repair(
            report_id,
            expected_revision=expected_revision,
            finding_id=_required_text(payload, "findingId"),
            source_scope=source_scope,
            source_trace_id=source_trace_id,
            failure_ref=_required_text(payload, "failureRef"),
            repair_session_id=repair_session_id,
        )

    def verify_trace_diagnostic_repair(
        self,
        report_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Link an immutable repair receipt and Runtime-owned Eval to a report."""

        expected_revision = _trace_diagnostic_expected_revision(payload)
        receipt_id = _required_text(payload, "repairReceiptId")
        verification_receipt_id = str(
            payload.get("verificationReceiptId") or ""
        ).strip()
        receipt_result = self.get_trace_repair_receipt(receipt_id)
        receipt = receipt_result.get("receipt")
        if not isinstance(receipt, Mapping):
            raise ValueError("Trace repair receipt projection is invalid")
        recheck = self.recheck_trace_repair(
            {
                "schemaVersion": "rag-ime.trace-repair-recheck-request.v1",
                "repairReceiptId": receipt_id,
            }
        )
        eval_run = recheck.get("evalRun")
        if not isinstance(eval_run, Mapping):
            raise ValueError("Trace repair recheck did not return an EvalRun")
        report = self.trace_diagnostic_reports.get(report_id)
        if report is None:
            raise KeyError(report_id)
        source_trace = self.trace_store.get(str(receipt.get("sourceTraceId") or ""))
        repair_trace = self.trace_store.get(str(receipt.get("repairTraceId") or ""))
        if source_trace is None or repair_trace is None:
            raise ValueError("Trace diagnostic comparison requires persisted source and repair Traces")
        comparison = _trace_diagnostic_repair_comparison(
            report=report,
            source_trace=source_trace,
            repair_trace=repair_trace,
            eval_run=eval_run,
        )
        verification_receipt: Mapping[str, object] | None = None
        if verification_receipt_id:
            verification_result = self.get_trace_verification_receipt(
                verification_receipt_id
            )
            candidate = verification_result.get("verificationReceipt")
            if not isinstance(candidate, Mapping):
                raise ValueError("Trace verification receipt projection is invalid")
            verification_receipt = candidate
        return self.trace_diagnostic_reports.verify_repair(
            report_id,
            expected_revision=expected_revision,
            receipt=receipt,
            eval_run=eval_run,
            comparison=comparison,
            verification_receipt=verification_receipt,
        )

    def trace_diagnostic_report(self, report_id: str) -> dict[str, object]:
        report = self.trace_diagnostic_reports.get(report_id)
        if report is None:
            raise KeyError(report_id)
        report = self._reconcile_trace_diagnostic_report(report)
        if report.get("optimizationProjectId"):
            app = self._optimization_application()
            app.reconcile(report_id)
            app.record_report(report)
            return self.trace_diagnostic_reports.get(report_id)
        return report

    def bind_trace_optimization_services(self, extensions, capability_reader) -> None:
        self._trace_optimization_extensions = extensions
        self._trace_optimization_capability_reader = capability_reader

    def _optimization_application(self):
        from .trace_optimization_application import TraceOptimizationApplication
        with self._trace_optimization_lock:
            if self._trace_optimization_app is None:
                self._trace_optimization_app = TraceOptimizationApplication(self)
            return self._trace_optimization_app

    def trace_optimization_library(self, payload: Mapping[str, object]) -> dict:
        return self._optimization_application().library(payload)

    def trace_optimization_capabilities(self) -> dict:
        return self._optimization_application().capabilities()

    def _assert_trace_optimization_session(self, report_id: str, session_id: str) -> dict:
        report = self.trace_diagnostic_reports.get(report_id)
        if report is None:
            raise KeyError(report_id)
        if session_id != report["diagnosticSessionId"]:
            raise ValueError("Trace optimization requires this report's diagnostic Session")
        session = self.sessions.get(session_id)
        if not _trace_diagnostic_session_policy_active(session, expected_surface_key="diagnostic"):
            raise ValueError("Trace optimization requires the diagnostic Session policy")
        self._assert_trace_diagnostic_project_binding(
            session, report["targets"],
            frozen_environment=report["inspection"].get("environment"),
        )
        return report

    def trace_optimization_read(self, report_id: str, payload: Mapping[str, object], *, session_id: str) -> dict:
        self._assert_trace_optimization_session(report_id, session_id)
        return self._optimization_application().read(report_id, payload)

    def trace_optimization_command(self, report_id: str, payload: Mapping[str, object], *, session_id: str = "") -> dict:
        operation = str(payload.get("operation") or payload.get("command") or "")
        if session_id:
            self._assert_trace_optimization_session(report_id, session_id)
            if operation == "candidate_action":
                raise ValueError("installation and adoption are owned by the user's Trace App action")
        elif operation not in {"candidate_action", "run_candidate", "cancel_candidate"}:
            raise ValueError("this operation requires the bound diagnostic Session")
        return self._optimization_application().command(report_id, payload)

    def list_trace_diagnostic_reports(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        values = dict(payload or {})
        return self.trace_diagnostic_reports.list(
            limit=_integer(values.get("limit"), default=100, minimum=1, maximum=100),
            cursor=str(values.get("cursor") or "").strip() or None,
        )

    def list_eval_suites(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return the validated, privacy-safe registry projection for Eval UI."""

        values = dict(payload or {})
        limit_value = values.get("limit")
        if limit_value in (None, ""):
            limit = 100
        else:
            if isinstance(limit_value, bool):
                raise ValueError("eval suite list limit must be an integer")
            try:
                limit = int(limit_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("eval suite list limit must be an integer") from exc
        if not 1 <= limit <= 100:
            raise ValueError("eval suite list limit must be between 1 and 100")
        result = {
            "schemaVersion": "rag-ime.eval-suite-list.v1",
            "ok": True,
            "items": list_builtin_eval_suites()[:limit],
        }
        validate_contract(result, "eval-suite-list.v1.json")
        return result

    def list_eval_schedules(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return bounded local EvalSchedule projections for a caller.

        The schedule store owns lease and settlement state.  This facade only
        returns its public projection, so a Control API caller can inspect
        schedule identity and run state without receiving a lease token.
        """

        values = dict(payload or {})
        limit = values.get("limit")
        if limit in (None, ""):
            limit = 100
        result = {
            "schemaVersion": "rag-ime.eval-schedule-list.v1",
            "ok": True,
            "items": self.eval_schedules.list(limit=limit),
        }
        validate_contract(result, "eval-schedule-list.v1.json")
        return result

    def create_eval_schedule(
        self,
        payload: Mapping[str, object],
        *,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Create one local EvalSchedule without starting a scheduler daemon."""

        schedule = self.eval_schedules.create(payload, now_ms=now_ms)
        result = {
            "schemaVersion": "rag-ime.eval-schedule-create.v1",
            "ok": True,
            "schedule": schedule,
        }
        validate_contract(result, "eval-schedule-create.v1.json")
        scheduler = getattr(self, "wake_scheduler", None)
        if scheduler is not None:
            # ``wake`` only nudges the already-owned wake loop.  It does not
            # create or start a second thread for Eval schedules.
            scheduler.wake()
        return result

    def eval_schedule_action(self, schedule_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        schedule = self.eval_schedules.action(schedule_id, str(payload.get("action") or ""))
        scheduler = getattr(self, "wake_scheduler", None)
        if scheduler is not None:
            scheduler.wake()
        return {"ok": True, "schedule": schedule}

    def eval_schedule_runs(
        self,
        schedule_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return one schedule and a bounded newest-first run projection."""

        values = dict(payload or {})
        limit = values.get("limit")
        if limit in (None, ""):
            limit = 100
        schedule = self.eval_schedules.get(schedule_id)
        runs = self.eval_schedules.runs(schedule_id, limit=limit)
        eval_store = getattr(self, "eval_runs", None)
        eval_get = getattr(eval_store, "get", None)
        projected_runs: list[dict[str, object]] = []
        for run in runs:
            projected = dict(run)
            trace_ids: list[str] = []
            trace_ids_truncated = False
            eval_run_id = str(run.get("evalRunId") or "")
            if eval_run_id and callable(eval_get):
                eval_run = eval_get(eval_run_id)
                if eval_run is not None:
                    if not isinstance(eval_run, Mapping):
                        raise TraceContractError("persisted EvalRun projection is invalid")
                    raw_trace_ids = eval_run.get("traceIds")
                    if (
                        not isinstance(raw_trace_ids, Sequence)
                        or isinstance(raw_trace_ids, (str, bytes, bytearray))
                    ):
                        raise TraceContractError("persisted EvalRun traceIds are invalid")
                    trace_ids_truncated = len(raw_trace_ids) > _SCHEDULE_RUN_TRACE_ID_LIMIT
                    trace_ids = [
                        str(trace_id)
                        for trace_id in raw_trace_ids[:_SCHEDULE_RUN_TRACE_ID_LIMIT]
                    ]
            projected["traceIds"] = trace_ids
            projected["traceIdsTruncated"] = trace_ids_truncated
            projected_runs.append(projected)
        result = {
            "schemaVersion": "rag-ime.eval-schedule-run-list.v1",
            "ok": True,
            "schedule": schedule,
            "items": projected_runs,
        }
        validate_contract(result, "eval-schedule-run-list.v1.json")
        return result

    def observation_trace(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Project one bounded observation trace without creating persistence."""

        values = dict(payload or {})
        trace_id = _observation_trace_id(values)

        external_envelope = self._resolve_external_trace(trace_id)
        if external_envelope is not None:
            trace = external_envelope.to_dict()
            if trace.get("traceId") != trace_id:
                raise TraceContractError("external trace envelope id does not match requested trace")
            result = {
                "schemaVersion": "rag-ime.observability-trace-get.v1",
                "traceId": trace_id,
                "trace": trace,
                "truncated": False,
                "projectionSource": "source_adapter",
                "observationWindow": {
                    "firstSequence": 0,
                    "lastSequence": 0,
                    "resumeToken": f"source:{trace_id}",
                    "nextBeforeSequence": None,
                },
            }
            validate_contract(result, "observability-trace-get.v1.json")
            return result

        # A durable canonical trace outranks the observation journal, which
        # is only a bounded discovery/progress projection.  ``getattr`` keeps
        # lightweight AgentService.__new__ test doubles compatible with the
        # pre-TraceStore facade.
        durable_store = getattr(self, "trace_store", None)
        durable_get = getattr(durable_store, "get", None)
        if callable(durable_get):
            durable_trace = durable_get(trace_id)
            if durable_trace is not None:
                if not isinstance(durable_trace, Mapping):
                    raise TraceContractError("durable TraceStore returned an invalid envelope")
                trace = dict(durable_trace)
                validate_trace_envelope(trace)
                if trace.get("traceId") != trace_id:
                    raise TraceContractError(
                        "durable trace envelope id does not match requested trace"
                    )
                result = {
                    "schemaVersion": "rag-ime.observability-trace-get.v1",
                    "traceId": trace_id,
                    "trace": trace,
                    "truncated": False,
                    "projectionSource": "trace_store",
                    "observationWindow": {
                        "firstSequence": 0,
                        "lastSequence": 0,
                        "resumeToken": f"trace-store:{trace_id}",
                        "nextBeforeSequence": None,
                    },
                }
                validate_contract(result, "observability-trace-get.v1.json")
                return result

        snapshot_payload = {"traceId": trace_id}
        for key in ("limit", "beforeSequence"):
            if values.get(key) not in (None, ""):
                snapshot_payload[key] = values[key]
        snapshot = self.observations.snapshot(snapshot_payload)
        raw_items = snapshot.get("items")
        if (
            not isinstance(raw_items, Sequence)
            or isinstance(raw_items, (str, bytes, bytearray))
            or not raw_items
        ):
            raise KeyError(trace_id)
        truncated_value = snapshot.get("truncated")
        if not isinstance(truncated_value, bool):
            raise TraceContractError("observation snapshot has invalid truncated flag")

        try:
            envelope = envelope_from_observations(raw_items)  # type: ignore[arg-type]
            trace = envelope.to_dict()
            if trace.get("traceId") != trace_id:
                raise TraceContractError("trace envelope id does not match requested trace")
            if truncated_value:
                trace["status"] = "building"
                validate_trace_envelope(trace)
            sequences = [
                int(item["sequence"])
                for item in raw_items
                if isinstance(item, Mapping)
                and isinstance(item.get("sequence"), (int, float))
            ]
            result = {
                "schemaVersion": "rag-ime.observability-trace-get.v1",
                "traceId": trace_id,
                "trace": trace,
                "truncated": truncated_value,
                "projectionSource": "observation_journal",
                "observationWindow": {
                    "firstSequence": min(sequences),
                    "lastSequence": max(sequences),
                    "resumeToken": f"observation:{max(sequences)}",
                    "nextBeforeSequence": (
                        min(sequences)
                        if truncated_value and sequences
                        else None
                    ),
                },
            }
            validate_contract(result, "observability-trace-get.v1.json")
            return result
        except TraceContractError:
            raise
        except Exception as exc:
            raise TraceContractError(str(exc)) from exc

    def _resolve_external_trace(self, trace_id: str) -> TraceEnvelope | None:
        for resolver in tuple(getattr(self, "_external_trace_resolvers", ())):
            envelope = resolver(trace_id)
            if envelope is not None:
                return envelope
        return None

    def observation_evals(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return privacy-safe EvalRun summaries for one authoritative Trace."""

        values = dict(payload or {})
        trace_id = _observation_trace_id(values)
        limit = _integer(values.get("limit"), default=100, minimum=1, maximum=500)
        runs, total = self.eval_runs.recent_for_trace(trace_id, limit=limit)
        items: list[dict[str, object]] = []
        for run in runs:
            truth = run.get("truth")
            evaluator = run.get("evaluator")
            metrics = run.get("metrics")
            suite_binding = run.get("suiteBinding")
            if not isinstance(truth, Mapping) or not isinstance(evaluator, Mapping):
                raise TraceContractError("persisted EvalRun projection is invalid")
            if not isinstance(metrics, Mapping):
                raise TraceContractError("persisted EvalRun metrics are invalid")
            item: dict[str, object] = {
                "evalRunId": str(run["evalRunId"]),
                "mode": str(run["mode"]),
                "metricAuthority": str(run["metricAuthority"]),
                "truthStatus": str(truth.get("status") or "none"),
                "datasetId": str(truth.get("datasetId") or ""),
                "labelRevision": str(truth.get("labelRevision") or ""),
                "evaluatorDisplayName": str(evaluator.get("displayName") or ""),
                "metrics": {
                    str(key): float(value)
                    for key, value in metrics.items()
                },
                "status": str(run["status"]),
                "createdAtMs": int(run["createdAtMs"]),
                "updatedAtMs": int(run["updatedAtMs"]),
            }
            if isinstance(suite_binding, Mapping):
                item["suiteBinding"] = {
                    "suiteId": str(suite_binding["suiteId"]),
                    "suiteRevision": str(suite_binding["suiteRevision"]),
                }
            items.append(item)
        result = {
            "schemaVersion": "rag-ime.observability-eval-list.v1",
            "traceId": trace_id,
            "total": total,
            "truncated": total > len(items),
            "items": items,
        }
        validate_contract(result, "observability-eval-list.v1.json")
        return result

    def observability_sandbox_runs(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return the Host-owned SandboxRun ledger as a bounded read projection."""

        values = dict(payload or {})
        limit = _integer(values.get("limit"), default=20, minimum=1, maximum=500)
        store = getattr(self, "sandbox_runs", None)
        if store is None:
            raise TraceContractError("SandboxRun store is unavailable")
        runs = store.list(limit=limit)
        total = store.count()
        result = {
            "schemaVersion": "rag-ime.observability-sandbox-run-list.v1",
            "ok": True,
            "items": runs,
            "total": total,
        }
        validate_contract(result, "observability-sandbox-run-list.v1.json")
        return result

    def observability_sandbox_run(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return one immutable SandboxRun payload without granting mutation."""

        values = dict(payload or {})
        sandbox_run_id = values.get("sandboxRunId")
        if not isinstance(sandbox_run_id, str) or not sandbox_run_id.strip():
            raise ValueError("invalid_sandbox_run_id")
        store = getattr(self, "sandbox_runs", None)
        if store is None:
            raise TraceContractError("SandboxRun store is unavailable")
        run = store.get(sandbox_run_id)
        if run is None:
            raise KeyError(sandbox_run_id)
        if not isinstance(run, Mapping):
            raise TraceContractError("SandboxRun store returned an invalid payload")
        result = dict(run)
        validate_sandbox_run(result)
        validate_contract(result, "sandbox-run.v1.json")
        return result

    def evaluate_observation_evidence(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Run one deterministic evidence-set Eval against a completed Trace."""

        values = dict(payload)
        validate_contract(
            values,
            "observability-evidence-eval-request.v1.json",
        )
        trace_id = _observation_trace_id(values)
        detail = self.observation_trace({"traceId": trace_id, "limit": 500})
        if detail.get("truncated") is True:
            raise TraceContractError(
                "evidence evaluation requires a complete observation window"
            )
        trace = detail.get("trace")
        if not isinstance(trace, Mapping):
            raise TraceContractError("observation Trace projection is invalid")
        required_ids = values.get("requiredEvidenceIds")
        if not isinstance(required_ids, Sequence) or isinstance(required_ids, (str, bytes)):
            raise TraceContractError("requiredEvidenceIds must be a sequence")
        return evaluate_evidence_ground_truth(
            (trace,),
            {trace_id: [str(value) for value in required_ids]},
            dataset_id=str(values["datasetId"]),
            label_revision=str(values["labelRevision"]),
            truth_kind=str(values["truthKind"]),
            store=self.eval_runs,
        )

    def evaluate_observation_ai_judge(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Run one redacted Trace through the Luna Max AI-Judge seam.

        AI Judge output is always persisted as an estimate.  Runtime or model
        output failures still produce a failed EvalRun with the effective
        evaluator recorded, so the UI never turns an unavailable judge into a
        silently missing or deterministic result.
        """

        values = dict(payload)
        trace_id = _observation_trace_id(values)
        detail = self.observation_trace({"traceId": trace_id, "limit": 500})
        if detail.get("truncated") is True:
            raise TraceContractError(
                "AI Judge evaluation requires a complete observation window"
            )
        trace = detail.get("trace")
        if not isinstance(trace, Mapping):
            raise TraceContractError("observation Trace projection is invalid")
        if str(trace.get("status") or "") != "completed":
            raise TraceContractError("AI Judge evaluation requires completed traces")

        requested = values.get("evaluator")
        if not isinstance(requested, Mapping):
            requested_fields = {
                key: values[key]
                for key in ("provider", "model", "thinking", "displayName")
                if key in values
            }
            requested = requested_fields or None
        evaluator = effective_ai_judge_evaluator(requested)
        # Store only the normalized four-field identity.  This makes an
        # explicit requested evaluator auditable while dropping arbitrary
        # request fields that could contain private provider diagnostics.
        requested_evaluator = dict(evaluator)
        started_at_ms = int(time.time() * 1000)
        started_clock = time.perf_counter()
        request_id = "ai-judge:" + hashlib.sha256(
            f"{trace_id}:{started_at_ms}".encode("utf-8")
        ).hexdigest()[:32]
        metrics: dict[str, float] = {}
        status = "completed"
        failure_code: str | None = None
        response: Mapping[str, object] | None = None
        runtime = getattr(self, "runtime", None)
        try:
            complete_once = getattr(runtime, "complete_once", None)
            if not callable(complete_once):
                failure_code = "ai_judge_runtime_unavailable"
                raise RuntimeError("AI Judge runtime is unavailable")
            response_value = complete_once(
                request_id=request_id,
                provider=evaluator["provider"],
                model_id=evaluator["model"],
                thinking_level=evaluator["thinking"],
                message=build_ai_judge_prompt(trace),
                timeout_seconds=120.0,
            )
            if isinstance(response_value, Mapping):
                response = response_value
            response_text = response.get("text") if response is not None else response_value
            metrics = parse_ai_judge_metrics(response_text)
        except AiJudgeOutputError:
            # Keep model output details out of the receipt; the closed code is
            # enough for the Trace/diagnostic UI to distinguish this failure.
            status = "failed"
            failure_code = "ai_judge_invalid_response"
        except TimeoutError:
            status = "failed"
            failure_code = "ai_judge_timeout"
        except RuntimeError:
            status = "failed"
            if failure_code is None:
                failure_code = "ai_judge_request_failed"
        except Exception:
            # The failed receipt is intentional: it keeps evaluator identity,
            # timing, and estimate authority visible without inventing scores
            # or persisting a raw provider exception.
            status = "failed"
            failure_code = "ai_judge_request_failed"

        completed_at_ms = max(started_at_ms, int(time.time() * 1000))
        elapsed_ms = max(0, int(round((time.perf_counter() - started_clock) * 1000)))
        latency_ms = _public_ai_judge_latency(response) if response is not None else None
        if latency_ms is None:
            latency_ms = elapsed_ms
        usage = _public_ai_judge_usage(response.get("usage")) if response is not None else None
        cost = _public_ai_judge_cost(response) if response is not None else None
        input_trace_fingerprint = None
        trace_input = trace.get("input")
        if isinstance(trace_input, Mapping):
            candidate_fingerprint = trace_input.get("fingerprint")
            if isinstance(candidate_fingerprint, str):
                input_trace_fingerprint = candidate_fingerprint

        eval_run_id = "eval:ai-judge:" + hashlib.sha256(
            f"{trace_id}:{request_id}".encode("utf-8")
        ).hexdigest()[:32]
        run = build_eval_run(
            eval_run_id=eval_run_id,
            trace_ids=[trace_id],
            mode="ai_judge",
            truth_kind="none",
            dataset_id="trace-eval-ai-judge",
            label_revision="trace-eval-ai-judge-v1",
            metrics=metrics,
            evaluator=evaluator,
            requested_evaluator=requested_evaluator,
            prompt_version=AI_JUDGE_RUBRIC_VERSION,
            rubric_version=AI_JUDGE_RUBRIC_VERSION,
            input_trace_fingerprint=input_trace_fingerprint,
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            elapsed_ms=elapsed_ms,
            latency_ms=latency_ms,
            usage=usage,
            cost=cost,
            fallback_used=False,
            failure_code=failure_code,
            status=status,
            now_ms=started_at_ms,
            updated_at_ms=completed_at_ms,
        )
        return self.eval_runs.persist(run)

    def record_trace_repair_change_evidence(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Persist change evidence derived from a server-owned repair run.

        ``repairSessionId`` and ``repairTraceId`` are references, not proof.
        The evidence body, status and identity are rebuilt from the durable
        Session message snapshot and the canonical TraceStore envelope.  This
        intentionally rejects the old client-provided ``evidence`` shape;
        accepting it would let a caller turn arbitrary prose into a repair
        receipt.
        """

        values = _trace_repair_candidate_payload(
            payload,
            schema_version="rag-ime.trace-repair-change-evidence.v1",
        )
        repair_session_id = _required_trace_repair_text(values, "repairSessionId")
        repair_trace_id = _required_trace_repair_text(values, "repairTraceId")
        canonical = self._derive_trace_repair_evidence(
            repair_session_id=repair_session_id,
            repair_trace_id=repair_trace_id,
        )["change"]
        if int(canonical.get("changeCount") or 0) < 1:
            raise TraceRepairValidationError(
                "repair run has no completed mutating Tool evidence"
            )
        stored = self.trace_repairs.record_change_evidence(
            # Evidence rows are keyed by the server-owned repair candidate;
            # the original failure identity belongs only to the receipt.
            source_scope="trace-repair",
            source_trace_id=repair_trace_id,
            evidence=canonical,
        )
        return {
            "schemaVersion": "rag-ime.trace-repair-evidence-write.v1",
            "ok": True,
            "evidence": stored,
        }

    def record_trace_repair_test_evidence(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Persist test evidence and derive status from completed Tool facts."""

        values = _trace_repair_candidate_payload(
            payload,
            schema_version="rag-ime.trace-repair-test-evidence.v1",
        )
        repair_session_id = _required_trace_repair_text(values, "repairSessionId")
        repair_trace_id = _required_trace_repair_text(values, "repairTraceId")
        canonical = self._derive_trace_repair_evidence(
            repair_session_id=repair_session_id,
            repair_trace_id=repair_trace_id,
        )["test"]
        test_status = _required_trace_repair_text(canonical, "status")
        stored = self.trace_repairs.record_test_evidence(
            source_scope="trace-repair",
            source_trace_id=repair_trace_id,
            evidence=canonical,
            status=test_status,
        )
        return {
            "schemaVersion": "rag-ime.trace-repair-evidence-write.v1",
            "ok": True,
            "evidence": stored,
        }

    def create_trace_repair_receipt(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Bind persisted change/test evidence to one repair Trace.

        Receipt and test-status identifiers are deliberately not accepted from
        the request.  The status is read from the persisted test evidence and
        the receipt ID is derived by ``TraceRepairStore``.  This prevents a
        copied assistant message from manufacturing a successful receipt.
        """

        values = _trace_repair_receipt_payload(payload)
        source_scope = _required_trace_repair_text(values, "sourceScope")
        source_trace_id = _required_trace_repair_text(values, "sourceTraceId")
        failure_ref = _required_trace_repair_text(values, "failureRef")
        change_id = _required_trace_repair_text(values, "changeReceiptId")
        test_id = _required_trace_repair_text(values, "testEvidenceId")
        repair_trace_id = _required_trace_repair_text(values, "repairTraceId")
        repair_session_id = _required_trace_repair_text(values, "repairSessionId")
        store = self.trace_repairs
        test = store.get_evidence(test_id)
        if test is None or test.get("evidenceKind") != "test":
            raise TraceRepairValidationError(
                "testEvidenceId does not reference persisted test evidence"
            )
        canonical_test = test.get("evidence")
        canonical_change = store.get_evidence(change_id)
        if not isinstance(canonical_test, Mapping) or not isinstance(canonical_change, Mapping):
            raise TraceRepairValidationError("repair evidence payload is invalid")
        for evidence_payload, label in (
            (canonical_change.get("evidence"), "changeReceiptId"),
            (canonical_test, "testEvidenceId"),
        ):
            if not isinstance(evidence_payload, Mapping):
                raise TraceRepairValidationError(f"{label} has no canonical repair binding")
            if str(evidence_payload.get("repairTraceId") or "") != repair_trace_id:
                raise TraceRepairValidationError(f"{label} is bound to a different repair Trace")
            bound_session_id = str(evidence_payload.get("repairSessionId") or "")
            if not bound_session_id:
                raise TraceRepairValidationError(f"{label} has no repair Session binding")
            if bound_session_id != repair_session_id:
                raise TraceRepairValidationError(f"{label} is bound to a different repair Session")
        # Receipt creation is a second authority check, not merely a lookup
        # of the earlier candidate snapshot.  This catches failed/building or
        # rebound Trace references between evidence and receipt creation.
        repair_trace = self._require_trace_repair_trace(repair_trace_id)
        if str(repair_trace.get("status") or "") != "completed":
            raise TraceRepairValidationError("repair trace must be completed")
        binding = repair_trace.get("binding")
        if not isinstance(binding, Mapping) or str(binding.get("sessionId") or "") != repair_session_id:
            raise TraceRepairValidationError("repair trace is bound to a different repair Session")
        if int(canonical_change.get("evidence", {}).get("changeCount") or 0) < 1:
            raise TraceRepairValidationError("change evidence has no completed mutating Tool")
        if str(canonical_test.get("status") or "") != "passed":
            raise TraceRepairValidationError("test evidence is not passed")
        has_host_sandbox = (
            canonical_test.get("sandboxRequired") is True
            and int(canonical_test.get("sandboxedCount") or 0) >= 1
        )
        has_server_terminal = (
            canonical_test.get("sessionTerminalRequired") is True
            and int(canonical_test.get("sessionTerminalCount") or 0) >= 1
        )
        if not has_host_sandbox and not has_server_terminal:
            raise TraceRepairValidationError(
                "test evidence has no authoritative completed execution"
            )
        self._freeze_trace_repair_source_trace(
            source_trace_id=source_trace_id,
            source_scope=source_scope,
        )
        receipt = store.persist_receipt(
            source_scope=source_scope,
            source_trace_id=source_trace_id,
            failure_ref=failure_ref,
            change_receipt_id=change_id,
            test_evidence_id=test_id,
            repair_trace_id=repair_trace_id,
            repair_session_id=repair_session_id,
        )
        return {
            "schemaVersion": "rag-ime.trace-repair-receipt-create.v1",
            "ok": True,
            "receipt": receipt,
        }

    def get_trace_repair_receipt(
        self,
        repair_receipt_id: str,
    ) -> dict[str, object]:
        """Read one immutable repair receipt by its server-issued ID."""

        if not isinstance(repair_receipt_id, str) or not repair_receipt_id.strip():
            raise TraceRepairValidationError("repairReceiptId must be a non-empty string")
        receipt = self.trace_repairs.get_receipt(repair_receipt_id.strip())
        if receipt is None:
            raise KeyError(repair_receipt_id)
        return {
            "schemaVersion": "rag-ime.trace-repair-receipt-get.v1",
            "ok": True,
            "receipt": receipt,
        }

    def recheck_trace_repair(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Run the Runtime-owned AI Judge against exactly one stored receipt.

        The request contains only the opaque receipt ID.  Source scope,
        source/repair Trace IDs, failure reference, and evidence IDs all come
        from the immutable receipt and are passed to the recheck helper as
        canonical values.
        """

        values = _trace_repair_recheck_payload(payload)
        receipt_id = _required_trace_repair_text(values, "repairReceiptId")
        store = self.trace_repairs
        receipt = store.get_receipt(receipt_id)
        if receipt is None:
            raise KeyError(receipt_id)
        eval_store = self.eval_runs

        # Rechecking one immutable receipt is idempotent at the public API.
        # Hold the per-service lock across lookup, model call, and persistence:
        # two concurrent HTTP requests must not both invoke Luna before either
        # one has written the deterministic EvalRun.
        with self._trace_repair_recheck_guard():
            existing = next(
                (
                    run
                    for run in eval_store.list()
                    if run.get("repairReceiptId") == receipt_id
                ),
                None,
            )
            if existing is not None:
                return {
                    "schemaVersion": "rag-ime.trace-repair-recheck.v1",
                    "ok": True,
                    "receipt": receipt,
                    "evalRun": dict(existing),
                    "idempotent": True,
                }

            def judge(trace: Mapping[str, object]) -> Mapping[str, object] | str:
                request_id = "trace-repair-recheck:" + hashlib.sha256(
                    f"{receipt_id}:{receipt['repairTraceId']}".encode("utf-8")
                ).hexdigest()[:32]
                response = self.runtime.complete_once(
                    request_id=request_id,
                    provider="openai-codex",
                    model_id="gpt-5.6-luna",
                    thinking_level="max",
                    message=build_ai_judge_prompt(trace),
                    timeout_seconds=120.0,
                )
                if isinstance(response, Mapping):
                    return response.get("text", response)
                return response

            eval_run = run_ai_judge_recheck(
                trace_store=self.trace_store,
                repair_store=store,
                eval_store=eval_store,
                source_trace_id=str(receipt["sourceTraceId"]),
                repair_trace_id=str(receipt["repairTraceId"]),
                source_scope=str(receipt["sourceScope"]),
                failure_ref=str(receipt["failureRef"]),
                judge=judge,
            )
            return {
                "schemaVersion": "rag-ime.trace-repair-recheck.v1",
                "ok": True,
                "receipt": receipt,
                "evalRun": eval_run,
                "idempotent": False,
            }

    def _require_trace_repair_trace(
        self,
        trace_id: str,
        *,
        repair_session_id: str = "",
        session_snapshot: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        trace = self.trace_store.get(trace_id)
        if (
            trace is None
            and repair_session_id
            and isinstance(session_snapshot, Mapping)
        ):
            trace = self._materialize_terminal_repair_trace(
                trace_id=trace_id,
                repair_session_id=repair_session_id,
                session_snapshot=session_snapshot,
            )
        if trace is None:
            raise TraceRepairValidationError("source trace is not persisted")
        if not isinstance(trace, Mapping):
            raise TraceRepairValidationError("persisted source trace is invalid")
        return trace

    def _materialize_terminal_repair_trace(
        self,
        *,
        trace_id: str,
        repair_session_id: str,
        session_snapshot: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Persist a metadata-only Trace for one terminal Agent repair turn.

        Pi's durable runtime-event journal is the authority.  The caller-provided
        Trace ID is accepted only when it names an exact non-history terminal
        turn in a complete, idle snapshot of the same Session.  No message or
        Tool content is copied into the Trace.
        """

        match = re.fullmatch(
            r"trace:turn:([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})",
            trace_id,
        )
        if match is None:
            return None
        turn_id = match.group(1)
        if turn_id.startswith("history:"):
            return None
        if (
            session_snapshot.get("status") != "idle"
            or session_snapshot.get("partial") is True
            or session_snapshot.get("truncated") is True
            or str(session_snapshot.get("snapshotScope") or "").strip().lower()
            == "recent"
            or str(session_snapshot.get("sessionId") or "").strip()
            != repair_session_id
        ):
            return None
        terminal_event_lookup = getattr(
            self.sessions,
            "runtime_turn_terminal_event",
            None,
        )
        if not callable(terminal_event_lookup):
            return None
        terminal_event = terminal_event_lookup(repair_session_id, turn_id)
        if (
            not isinstance(terminal_event, Mapping)
            or str(terminal_event.get("sessionId") or "") != repair_session_id
            or str(terminal_event.get("turnId") or "") != turn_id
            or terminal_event.get("eventType")
            not in {"turn_completed", "turn_failed"}
        ):
            return None
        created_at_ms = terminal_event.get("createdAtMs")
        if (
            not isinstance(created_at_ms, int)
            or isinstance(created_at_ms, bool)
            or created_at_ms < 0
        ):
            return None
        updated_at_ms = created_at_ms
        return self.trace_store.persist(
            build_trace_envelope(
                trace_id=trace_id,
                source_kind="agent_repair",
                input_text=f"{repair_session_id}\n{turn_id}",
                binding={
                    "sessionId": repair_session_id,
                    "turnId": turn_id,
                },
                status="completed",
                spans=(),
                evidence=(),
                artifacts=(),
                created_at_ms=created_at_ms,
                updated_at_ms=max(created_at_ms, updated_at_ms),
                input_content_policy="hash_only",
                input_normalization="none",
            )
        )

    def _freeze_trace_repair_source_trace(
        self,
        *,
        source_trace_id: str,
        source_scope: str,
    ) -> Mapping[str, object]:
        """Freeze one observation-backed source Trace before receipt creation."""

        trace = self.trace_store.get(source_trace_id)
        if trace is None:
            try:
                projection = self.observation_trace(
                    {"traceId": source_trace_id}
                )
            except KeyError as exc:
                raise TraceRepairValidationError(
                    "source trace is not available"
                ) from exc
            candidate = (
                projection.get("trace")
                if isinstance(projection, Mapping)
                else None
            )
            if (
                not isinstance(candidate, Mapping)
                or str(candidate.get("traceId") or "") != source_trace_id
                or candidate.get("partial") is True
                or candidate.get("truncated") is True
            ):
                raise TraceRepairValidationError(
                    "source trace projection is invalid"
                )
            trace = candidate

        scope_kind, separator, scope_id = source_scope.partition(":")
        binding_key = {
            "session": "sessionId",
            "room": "roomId",
            "run": "runId",
        }.get(scope_kind if separator else "")
        if binding_key is not None:
            binding = trace.get("binding")
            if (
                not isinstance(binding, Mapping)
                or str(binding.get(binding_key) or "") != scope_id
            ):
                raise TraceRepairValidationError(
                    "source trace binding does not match sourceScope"
                )

        if self.trace_store.get(source_trace_id) is None:
            trace = self.trace_store.persist(trace)
        return trace

    def create_trace_replay_case(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Freeze one failing ground-truth case before a repair is evaluated."""

        values = _trace_replay_request(
            payload,
            schema_version="rag-ime.trace-replay-case-create.v1",
            required={
                "sourceScope",
                "failureRef",
                "sourceTraceId",
                "baselineEvalRunId",
                "baselineSandboxRunId",
                "successMetric",
                "successThreshold",
                "rollbackTarget",
            },
        )
        replay_case = self.trace_replay_verifications.freeze_case(
            source_scope=_required_trace_verification_text(values, "sourceScope"),
            failure_ref=_required_trace_verification_text(values, "failureRef"),
            source_trace_id=_required_trace_verification_text(values, "sourceTraceId"),
            baseline_eval_run_id=_required_trace_verification_text(
                values, "baselineEvalRunId"
            ),
            baseline_sandbox_run_id=_required_trace_verification_text(
                values, "baselineSandboxRunId"
            ),
            success_metric=_required_trace_verification_text(values, "successMetric"),
            success_threshold=values["successThreshold"],
            rollback_target=_required_trace_verification_text(values, "rollbackTarget"),
            created_at_ms=int(time.time() * 1000),
        )
        return {
            "schemaVersion": "rag-ime.trace-replay-case-create.v1",
            "ok": True,
            "replayCase": replay_case,
        }

    def get_trace_replay_case(self, replay_case_id: str) -> dict[str, object]:
        replay_case = self.trace_replay_verifications.get_case(replay_case_id)
        if replay_case is None:
            raise KeyError(replay_case_id)
        return {
            "schemaVersion": "rag-ime.trace-replay-case-get.v1",
            "ok": True,
            "replayCase": replay_case,
        }

    def verify_trace_replay_case(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Persist a comparison only after same-cohort GT replay and regressions."""

        values = _trace_replay_request(
            payload,
            schema_version="rag-ime.trace-verification-request.v1",
            required={
                "replayCaseId",
                "repairReceiptId",
                "repairEvalRunId",
                "repairSandboxRunId",
                "regressionEvalRunIds",
            },
        )
        regression_ids = values.get("regressionEvalRunIds")
        if not isinstance(regression_ids, list):
            raise TraceVerificationValidationError(
                "regressionEvalRunIds must be an array"
            )
        receipt = self.trace_replay_verifications.verify_repair(
            replay_case_id=_required_trace_verification_text(values, "replayCaseId"),
            repair_receipt_id=_required_trace_verification_text(
                values, "repairReceiptId"
            ),
            repair_eval_run_id=_required_trace_verification_text(
                values, "repairEvalRunId"
            ),
            repair_sandbox_run_id=_required_trace_verification_text(
                values, "repairSandboxRunId"
            ),
            regression_eval_run_ids=regression_ids,
            verified_at_ms=int(time.time() * 1000),
        )
        return {
            "schemaVersion": "rag-ime.trace-verification-receipt-create.v1",
            "ok": True,
            "verificationReceipt": receipt,
        }

    def get_trace_verification_receipt(
        self, verification_receipt_id: str
    ) -> dict[str, object]:
        receipt = self.trace_replay_verifications.get_verification(
            verification_receipt_id
        )
        if receipt is None:
            raise KeyError(verification_receipt_id)
        return {
            "schemaVersion": "rag-ime.trace-verification-receipt-get.v1",
            "ok": True,
            "verificationReceipt": receipt,
        }

    def _derive_trace_repair_evidence(
        self,
        *,
        repair_session_id: str,
        repair_trace_id: str,
    ) -> dict[str, dict[str, object]]:
        """Build canonical evidence from the server's Session and Trace views."""

        snapshot = self.message_snapshot.messages(repair_session_id)
        if not isinstance(snapshot, Mapping):
            raise TraceRepairValidationError(
                "repair Session message snapshot is unavailable"
            )
        repair_trace = self._require_trace_repair_trace(
            repair_trace_id,
            repair_session_id=repair_session_id,
            session_snapshot=snapshot,
        )
        return derive_repair_evidence(
            repair_session_id=repair_session_id,
            repair_trace_id=repair_trace_id,
            session_snapshot=snapshot,
            repair_trace=repair_trace,
        )

    def _trace_repair_recheck_guard(self):
        return self._trace_repair_recheck_lock

    def subscribe_observations(
        self,
        *,
        after_event_id: str = "",
        filters: Mapping[str, object] | None = None,
        heartbeat_seconds: float = 10.0,
    ) -> Iterator[bytes]:
        return self.observations.subscribe(
            after_event_id=after_event_id,
            filters=filters,
            heartbeat_seconds=heartbeat_seconds,
        )










    def _consume_room_knowledge_cache_tombstones(self) -> int:
        """Clear process-local recall state after durable knowledge invalidation.

        Retiring the durable tombstones belongs to the store that writes them;
        this service only owns the in-process recall state that has to follow.
        """
        retired = self.knowledge_promotion.consume_cache_tombstones(
            consumed_at_ms=int(time.time() * 1000)
        )
        self.memory_context_application.clear_recall_state(retired.session_ids)
        return retired.consumed

    def _dispatch_wake_claim(self, claim: Mapping[str, object]) -> None:
        metadata = claim.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        if str(metadata.get("kind") or "") != "room_partner_completion":
            self.wake_application.dispatch(claim)
            return
        if not hasattr(self, "room_partner_application"):
            self.wake_schedules.defer(
                str(claim.get("runId") or ""),
                reason="Room Partner wake adapter is still starting",
                delay_ms=5_000,
            )
            return
        try:
            self.room_partner_application.dispatch_wake(claim)
        except Exception as exc:
            self.room_partner_application.record_wake_failure(claim, exc)
            raise

    def _dispatch_room_partner_wake(
        self,
        claim: Mapping[str, object],
        dispatch: Mapping[str, object],
    ) -> bool:
        run_id = str(claim.get("runId") or "")
        session_id = str(dispatch.get("sourceSessionId") or "")
        root_id = str(dispatch.get("rootId") or "")
        child_dispatch_id = str(dispatch.get("childDispatchId") or "")
        self._recover_faulted_room_session(session_id)
        try:
            self.room_turns.hold_priority_if_idle((session_id,))
        except RoomSessionBusyError:
            self.wake_schedules.defer(
                run_id,
                reason="Facilitator 正在执行上一回合，伙伴交付稍后重试",
                cause_code="AGENT_TURN_CONFLICT",
                delay_ms=5_000,
            )
            return False
        try:
            session = self.sessions.get(session_id)
            session_status = str(session.get("status") or "")
            if session_status == "busy":
                self.wake_schedules.defer(
                    run_id,
                    reason="Facilitator 正在执行上一回合，伙伴交付稍后重试",
                    cause_code="AGENT_TURN_CONFLICT",
                    delay_ms=5_000,
                )
                return False
            if session_status not in {"idle", "active"}:
                raise ValueError("Facilitator Session is unavailable for Room wake")
            if not self._room_target_idle(
                session_id,
                allow_user_priority=True,
            ):
                self.wake_schedules.defer(
                    run_id,
                    reason="Facilitator 正在执行上一回合，伙伴交付稍后重试",
                    cause_code="AGENT_TURN_CONFLICT",
                    delay_ms=5_000,
                )
                return False
            participant = self.rooms.participant_for_session(
                session_id,
                active_only=True,
            )
            if participant is None or str(participant.get("id") or "") != str(
                dispatch.get("sourceParticipantId") or ""
            ):
                raise ValueError("Facilitator is no longer active in this Room")
            room = self.rooms.get(str(dispatch.get("roomId") or ""))
            topic_id = self._room_topic_for_turn(root_id) or str(
                room.get("activeTopicId") or ""
            )
            self.wake_application.enqueue_room_completion(
                claim=claim,
                dispatch=dispatch,
            )
            wake_dispatch_id = (
                f"room-wake-dispatch:{child_dispatch_id}:"
                f"{int((dispatch.get('wake') or {}).get('generation') or 0)}"
                if isinstance(dispatch.get("wake"), Mapping)
                else f"room-wake-dispatch:{child_dispatch_id}"
            )
            self._begin_room_turn(
                session_id,
                root_id,
                topic_id,
                dispatch_id=wake_dispatch_id,
                child=False,
            )
            try:
                accepted = self.prompt(
                    session_id,
                    {
                        "message": "伙伴交付已到达，请检查并完成双轴验收。",
                        "clientMessageId": run_id,
                        "_contextSourceToken": self._context_source_token,
                        "_contextSource": "room",
                        "_checkpointText": "伙伴交付已到达，请检查并完成双轴验收。",
                    },
                )
            except Exception as exc:
                self._cancel_room_turn(session_id, root_id)
                cause_code = _room_wake_failure_cause_code(exc)
                if cause_code in {
                    "SESSION_BUSY",
                    "AGENT_TURN_CONFLICT",
                    # Paused Goal must not auto-resume on wake. Defer until an
                    # explicit user Room message resumes the Goal; do not burn
                    # the completion wake as a terminal failure.
                    "GOAL_PAUSED",
                }:
                    self.wake_schedules.defer(
                        run_id,
                        reason=(
                            "Facilitator Goal 已暂停，等待用户在 Room 中继续后再验收"
                            if cause_code == "GOAL_PAUSED"
                            else "Facilitator 刚刚开始其他回合，伙伴交付稍后重试"
                        ),
                        cause_code=cause_code,
                        # Busy conflicts retry quickly; a paused Goal waits for
                        # an explicit user Room message, so avoid a 5s storm.
                        delay_ms=60_000 if cause_code == "GOAL_PAUSED" else 5_000,
                    )
                    return False
                if str(self.sessions.get(session_id).get("status") or "") == "busy":
                    self.wake_schedules.defer(
                        run_id,
                        reason="Facilitator 刚刚开始其他回合，伙伴交付稍后重试",
                        cause_code="AGENT_TURN_CONFLICT",
                        delay_ms=5_000,
                    )
                    return False
                raise
            turn_id = str(accepted.get("turnId") or "")
            self._accept_room_turn(session_id, turn_id, root_id)
            self.wake_schedules.accept(
                run_id,
                session_id=session_id,
                turn_id=turn_id,
            )
            replayed, _gap = self.events.replay(session_id)
            for event in replayed:
                if (
                    event.turn_id == turn_id
                    and event.event_type in {"turn_completed", "turn_failed"}
                ):
                    self.wake_scheduler.observe_event(event)
                    break
            return True
        finally:
            self.room_turns.release_priority_session(session_id)

    def _resume_room_goal_if_paused(self, session_id: str) -> None:
        """Resume a paused participant Goal for an explicit user Room message.

        The Room conversation entry is the only continue control a returning
        user has, so the user's message carries the resume intent. Wake,
        partner and Tool Agent paths never call this.
        """

        goal = self.sessions.agent_goal(session_id)
        if str(goal.get("status") or "") != "paused":
            return
        self.sessions.mutate_agent_goal(
            session_id,
            {"action": "resume", "expectedRevision": int(goal["revision"])},
            actor="room-user-message",
        )
        self.publish_workflow_state(session_id, reason="goal:resume")

    def _recover_faulted_room_session(self, session_id: str) -> None:
        """Re-open one recoverable Pi Session without replacing Room identity."""

        session = self.sessions.get(session_id)
        session_status = str(session.get("status") or "")
        if session_status not in {"faulted", "idle", "active"}:
            return
        if (
            session_status != "faulted"
            and self.sessions.runtime_binding(session_id) is None
        ):
            return
        # PAW can already project the Session as idle while a restarted Pi Host
        # restores the interrupted durable turn from JSONL. Re-open the same Pi
        # Session and retire only that exact turn when Pi proves it is idle; a
        # genuinely running turn reports isIdle=false and remains untouched.
        ensured = self.runtime.ensure(session_id)
        state = ensured.get("state")
        state = state if isinstance(state, Mapping) else {}
        active_turn = state.get("activeTurn")
        active_turn = active_turn if isinstance(active_turn, Mapping) else {}
        recovered_turn_id = str(active_turn.get("turnId") or "").strip()
        if state.get("isIdle") is True and recovered_turn_id:
            retire_recovered_turn = getattr(
                self.runtime,
                "retire_recovered_turn",
                None,
            )
            if not callable(retire_recovered_turn):
                raise RuntimeError(
                    "Pi Runtime cannot retire the recovered Room Session turn"
                )
            retire_recovered_turn(session_id, recovered_turn_id)
        if (
            session_status == "faulted"
            and str(self.sessions.get(session_id).get("status") or "")
            == "faulted"
        ):
            raise RuntimeError(
                "Room participant Session remained faulted after Pi recovery"
            )






    def close(self) -> None:
        with self._eval_lab_trial_lock:
            self._eval_lab_trial_closed = True
            trial_application = self._eval_lab_trial_application
        if trial_application is not None:
            # Abort observed calls and wait for adapter cleanup while its
            # Runtime, Session store and event owners are still available.
            trial_application.close()
        if self._eval_lab_golden_application is not None:
            self._eval_lab_golden_application.close()
        if self._eval_lab_app_application is not None:
            self._eval_lab_app_application.close()
        if self._eval_lab_app_research_application is not None:
            self._eval_lab_app_research_application.close()
        self.delegation.close()
        self.runtime.stop()
        self.background_jobs.close()
        self._remove_trace_diagnostic_observer()
        self.events.close()
        self._remove_observation_room_observer()
        self._remove_room_partner_observer()
        self.observations.close()
        self._remove_wake_observer()
        self.wake_scheduler.bind_terminal_observer(None)
        self.wake_scheduler.close()
        self.room_intercom.close()
        self.rooms.close()
        self.sessions.close()
        self.configuration_store.close()

    def reconfigure_runtime(self, config: PiRuntimeConfig) -> dict[str, object]:
        self.runtime.stop()
        config = replace(
            config,
            tool_gateway_token=self.tool_token,
            plugin_approval_token=self.plugin_approval_token,
            role_resolver=self.personas.resolve,
            role_book_resolver=self.role_books.prompt_block,
        )
        self.runtime_factory.reconfigure(config)
        self.runtime = self.runtime_factory.create(
            RuntimeDriverContext(
                sessions=self.sessions,
                events=self.events,
                media_resolver=self.media.resolve_pi_image,
                tool_gateway_token=self.tool_token,
                tool_gateway_url=self.tool_gateway_url,
                tool_manifest_provider=self._runtime_tool_manifest,
                skill_allowlist_provider=self._runtime_skill_allowlist,
                candidate_skill_paths_provider=self._runtime_candidate_skill_paths,
                compaction_observer=self._checkpoint_runtime_compaction,
                prompt_settings_provider=self._runtime_prompt_settings,
            ),
            purpose="interactive",
            session_context_provider=self._runtime_session_context,
        )
        self.delegation.reconfigure(config)
        self.room_intercom.notify()
        return self.runtime_status()

    def _apply_runtime_policy(self, policy: AgentRuntimePolicy) -> dict[str, object]:
        self.runtime.stop()
        self.runtime_factory.apply_policy(policy)
        self.runtime = self.runtime_factory.create(
            RuntimeDriverContext(
                sessions=self.sessions,
                events=self.events,
                media_resolver=self.media.resolve_pi_image,
                tool_gateway_token=self.tool_token,
                tool_gateway_url=self.tool_gateway_url,
                tool_manifest_provider=self._runtime_tool_manifest,
                skill_allowlist_provider=self._runtime_skill_allowlist,
                candidate_skill_paths_provider=self._runtime_candidate_skill_paths,
                compaction_observer=self._checkpoint_runtime_compaction,
                prompt_settings_provider=self._runtime_prompt_settings,
            ),
            purpose="interactive",
            session_context_provider=self._runtime_session_context,
        )
        self.delegation.refresh_runtime_factory()
        self.room_intercom.notify()
        return self.runtime.runtime_status()

    def _record_event(self, event: AgentEventEnvelope) -> None:
        self.event_projection_application.record(event)


    def _mirror_event_to_room(self, event: AgentEventEnvelope) -> None:
        cancelled_terminal = (
            self.room_turns.claim_cancelled_terminal(event)
        )
        if cancelled_terminal is not None:
            self.event_projection_application.mirror_to_room(
                event,
                cancelled_terminal=cancelled_terminal,
            )
            return
        if not self.room_turns.allows_room_event(event):
            return
        self.event_projection_application.mirror_to_room(
            event
        )

    def _begin_room_turn(
        self,
        session_id: str,
        room_turn_id: str,
        topic_id: str = "",
        *,
        dispatch_id: str = "",
        child: bool = False,
        work_item_id: str = "",
        work_item_revision: int = 0,
        attempt_id: str = "",
    ) -> None:
        self.room_turns.begin(
            session_id,
            room_turn_id,
            topic_id,
            dispatch_id=dispatch_id,
            child=child,
            work_item_id=work_item_id,
            work_item_revision=work_item_revision,
            attempt_id=attempt_id,
        )

    def _accept_room_turn(
        self,
        session_id: str,
        session_turn_id: str,
        room_turn_id: str,
    ) -> None:
        with self.room_turns.lock:
            buffered = self.room_turns.accept(
                session_id,
                session_turn_id,
                room_turn_id,
            )
            for event in buffered:
                self._mirror_event_to_room(event)
                if event.event_type in {
                    "turn_completed",
                    "turn_failed",
                }:
                    break

    def _cancel_room_turn(self, session_id: str, room_turn_id: str) -> None:
        self.room_turns.cancel(session_id, room_turn_id)

    def _room_turn_for_event(self, event: AgentEventEnvelope) -> str:
        return self.room_turns.turn_for_event(event)

    def _room_dispatch_for_event(self, event: AgentEventEnvelope) -> str:
        return self.room_turns.dispatch_for_event(event)

    def _finish_room_turn(
        self,
        session_id: str,
        session_turn_id: str,
        room_turn_id: str,
    ) -> None:
        self.room_turns.finish(
            session_id,
            session_turn_id,
            room_turn_id,
        )

    def _drop_room_topic_if_idle_locked(self, room_turn_id: str) -> None:
        self.room_turns.drop_topic_if_idle(room_turn_id)

    def _room_topic_for_turn(self, room_turn_id: str) -> str:
        return self.room_turns.topic_for_turn(room_turn_id)

    def _room_runtime_generation(self, session_id: str) -> int:
        return self.room_intercom_application.runtime_generation(
            session_id
        )

    def _room_target_idle(
        self,
        session_id: str,
        *,
        allow_user_priority: bool = False,
    ) -> bool:
        return self.room_intercom_application.target_idle(
            session_id,
            allow_user_priority=allow_user_priority,
        )

    def _deliver_room_intercom(
        self,
        item: Mapping[str, object],
    ) -> Mapping[str, object]:
        return self.room_intercom_application.deliver(item)

    def _publish_room_intercom_audit(
        self,
        item: Mapping[str, object],
        phase: str,
    ) -> None:
        self.room_intercom_application.publish_audit(
            item,
            phase,
        )

    def _guard_room_session_route(self, route_id: str, session_id: str) -> None:
        # Pi Session is the sole execution owner for direct and Room prompts.
        self.sessions.get(str(session_id or ""))

    def _publish_room_work_activity(
        self,
        work: Mapping[str, object],
        *,
        phase: str,
        actor: Mapping[str, object],
        document_sync: Mapping[str, object] | None = None,
    ) -> None:
        return self.room_work_application._publish_room_work_activity(
            work,
            phase=phase,
            actor=actor,
            document_sync=document_sync,
        )


def _room_wake_failure_cause_code(error: BaseException) -> str:
    return " ".join(
        str(
            getattr(error, "cause_code", "")
            or getattr(error, "host_error_code", "")
            or getattr(error, "error_code", "")
            or ""
        ).split()
    ).upper()[:80]


def agent_service_from_environment(
    db_path: str | Path,
    *,
    project: str = "",
    memory_embedding_provider: EmbeddingProvider | None = None,
    wake_scheduler_enabled: bool = True,
    eval_lab_trial_adapters: Mapping[str, TrialAdapter] | None = None,
) -> AgentService:
    return AgentService(
        db_path=db_path,
        runtime_config=PiRuntimeConfig.from_environment(),
        project=project,
        tool_gateway_url=os.environ.get(
            "RAG_IME_AGENT_TOOL_URL",
            "http://127.0.0.1:8766/api/agent/tool/execute",
        ),
        memory_embedding_provider=memory_embedding_provider,
        wake_scheduler_enabled=wake_scheduler_enabled,
        eval_lab_trial_adapters=eval_lab_trial_adapters,
    )


def pi_runtime_config_from_settings(settings: Mapping[str, object]) -> PiRuntimeConfig:
    agent = settings.get("agent") if isinstance(settings.get("agent"), Mapping) else {}
    pi = agent.get("pi") if isinstance(agent.get("pi"), Mapping) else {}
    privacy = (
        settings.get("privacy")
        if isinstance(settings.get("privacy"), Mapping)
        else {}
    )
    runtime_config = PiRuntimeConfig.from_environment(
        enabled_default=_bool(pi.get("enabled")),
        idle_timeout_default=_integer(
            pi.get("idleTimeoutSeconds"),
            default=900,
            minimum=0,
            maximum=86400,
        ),
        system_proxy_default=_bool(pi.get("systemProxy", True)),
    )
    if (
        runtime_config.debug_context_dir is None
        and _bool(privacy.get("debugIncludeText"))
    ):
        configured_directory = str(
            privacy.get("debugContextDirectory") or ""
        ).strip()
        storage_gib = _integer(
            privacy.get("debugContextMaxGiB"),
            default=5,
            minimum=1,
            maximum=64,
        )
        max_calls_per_turn = _integer(
            privacy.get("debugContextMaxCallsPerTurn"),
            default=128,
            minimum=1,
            maximum=256,
        )
        runtime_config = replace(
            runtime_config,
            debug_context_dir=(
                Path(configured_directory).expanduser()
                if configured_directory
                else runtime_config.agent_dir.parent / "debug-context"
            ),
            debug_context_max_bytes=storage_gib * 1024 * 1024 * 1024,
            debug_context_max_calls=max_calls_per_turn,
        )
    return runtime_config


def agent_service_from_settings(
    db_path: str | Path,
    settings: Mapping[str, object],
    *,
    project: str = "",
    memory_embedding_provider: EmbeddingProvider | None = None,
    wake_scheduler_enabled: bool = True,
    runtime_execution_owner: bool = True,
    defer_startup_recovery: bool = False,
    eval_lab_trial_adapters: Mapping[str, TrialAdapter] | None = None,
) -> AgentService:
    runtime_config = pi_runtime_config_from_settings(settings)
    agent = settings.get("agent") if isinstance(settings.get("agent"), Mapping) else {}
    pi = agent.get("pi") if isinstance(agent.get("pi"), Mapping) else {}
    return AgentService(
        db_path=db_path,
        runtime_config=runtime_config,
        runtime_factory=PiRuntimeDriverFactory(
            runtime_config,
            execution_owner=runtime_execution_owner,
        ),
        configuration_defaults=default_agent_configuration(
            enabled=runtime_config.enabled,
            idle_timeout_seconds=runtime_config.idle_timeout_seconds,
            role_id=str(pi.get("defaultRoleId") or "companion-future-v1"),
            role_version="1",
            tool_profile_version=str(pi.get("toolProfile") or "control-center-v1"),
            resume_last_session=_bool(pi.get("resumeLastSession")),
            coordinator_enabled=_bool(pi.get("coordinatorEnabled")),
        ),
        project=project,
        tool_gateway_url=os.environ.get(
            "RAG_IME_AGENT_TOOL_URL",
            "http://127.0.0.1:8766/api/agent/tool/execute",
        ),
        memory_embedding_provider=memory_embedding_provider,
        wake_scheduler_enabled=wake_scheduler_enabled,
        background_job_execution_owner=runtime_execution_owner,
        startup_recovery_enabled=runtime_execution_owner,
        defer_startup_recovery=defer_startup_recovery,
        eval_lab_trial_adapters=eval_lab_trial_adapters,
    )














def _approval_user_request_text(
    message: Mapping[str, object],
) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    text = message.get("text")
    if isinstance(text, str):
        return text.strip()
    blocks = message.get("blocks")
    if not isinstance(blocks, Sequence) or isinstance(
        blocks,
        (str, bytes, bytearray),
    ):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        block_type = str(block.get("type") or "")
        if block_type not in {"text", "input_text"}:
            continue
        value = block.get("text") or block.get("content")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)






def _lifecycle_cancellation_request_id(
    *,
    session_id: str,
    scope_kind: str,
    scope_id: str,
    source_revision: int,
    action: str,
) -> str:
    canonical = json.dumps(
        {
            "sessionId": session_id,
            "scopeKind": scope_kind,
            "scopeId": scope_id,
            "sourceRevision": int(source_revision),
            "action": action,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "lifecycle:"
        + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )


def _lifecycle_owner(
    audit: Mapping[str, object],
    owner: str,
) -> Mapping[str, object]:
    owners = audit.get("owners")
    if not isinstance(owners, Mapping):
        return {"status": "pending", "receipt": {}}
    value = owners.get(owner)
    return value if isinstance(value, Mapping) else {
        "status": "pending",
        "receipt": {},
    }


def _lifecycle_public_receipt(value: object) -> dict[str, object]:
    forbidden = {
        "pid",
        "processid",
        "processhandle",
        "processgroupid",
    }

    def project(item: object) -> object:
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                normalized = "".join(
                    character
                    for character in key.casefold()
                    if character.isalnum()
                )
                if normalized in forbidden:
                    continue
                result[key] = project(child)
            return result
        if isinstance(item, (list, tuple)):
            return [project(child) for child in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        return str(item)

    projected = project(value)
    return projected if isinstance(projected, dict) else {}


def _runtime_lifecycle_status(receipt: Mapping[str, object]) -> str:
    if not receipt:
        return "unknown"
    lifecycle = receipt.get("lifecycle")
    if isinstance(lifecycle, Mapping):
        if (
            lifecycle.get("drained") is False
            or bool(lifecycle.get("failedOperationIds"))
            or bool(lifecycle.get("pendingOperations"))
        ):
            return "partial"
    return "succeeded"


def _job_lifecycle_status(receipt: Mapping[str, object]) -> str:
    jobs = receipt.get("jobs")
    if not isinstance(jobs, list):
        return "unknown"
    statuses = {
        str(item.get("status") or "")
        for item in jobs
        if isinstance(item, Mapping)
    }
    if statuses & {"queued", "running", "cancelling", "failed", "orphaned"}:
        return "partial"
    return "succeeded"


def _delegation_lifecycle_status(
    receipt: Mapping[str, object],
) -> str:
    if not receipt:
        return "unknown"
    if (
        str(receipt.get("state") or "") == "requested"
        or bool(receipt.get("pendingRunIds"))
    ):
        return "partial"
    return "succeeded"


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value


def _trace_diagnostic_session_policy_active(
    session: Mapping[str, object],
    *,
    expected_surface_key: str,
) -> bool:
    workspace_roots = session.get("workspaceRoots")
    return (
        str(session.get("surfaceKind") or "") == "extension_app"
        and str(session.get("ownerAppId") or "") == TRACE_AGENT_OWNER_APP_ID
        and expected_surface_key in TRACE_AGENT_SURFACE_KEYS
        and str(session.get("surfaceKey") or "") == expected_surface_key
        and str(session.get("mode") or "") == "coordinator"
        and str(session.get("executionMode") or "") == FULL_TRUST_EXECUTION_MODE
        and auto_approve_policy_active(session)
        and isinstance(workspace_roots, list)
        and bool(workspace_roots)
        and all(str(root).strip() and str(root).strip() != "/" for root in workspace_roots)
        and str(session.get("toolAllowlistMode") or "") == "profile"
        and all(
            session.get(key) is True
            for key in (
                "projectContextEnabled",
                "piSkillsEnabled",
                "codexSkillsEnabled",
            )
        )
    )


def _trace_diagnostic_expected_revision(payload: Mapping[str, object]) -> int:
    value = payload.get("expectedRevision")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000:
        raise ValueError("Trace diagnostic repair lifecycle requires expectedRevision")
    return value


def _trace_diagnostic_repair_comparison(
    *,
    report: Mapping[str, object],
    source_trace: Mapping[str, object],
    repair_trace: Mapping[str, object],
    eval_run: Mapping[str, object],
) -> dict[str, object]:
    def fingerprint(trace: Mapping[str, object]) -> str:
        raw_input = trace.get("input")
        value = str(raw_input.get("fingerprint") or "") if isinstance(raw_input, Mapping) else ""
        return value if re.fullmatch(r"sha256:[a-f0-9]{64}", value) else ""

    def numeric_metrics(raw: object) -> dict[str, float]:
        if not isinstance(raw, Mapping):
            return {}
        result: dict[str, float] = {}
        for key, value in list(raw.items())[:64]:
            if not isinstance(key, str) or not key or len(key) > 160:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            number = float(value)
            if math.isfinite(number):
                result[key] = number
        return result

    before: dict[str, float] = {}
    inspection = report.get("inspection")
    scorecard = inspection.get("scorecard") if isinstance(inspection, Mapping) else None
    dimensions = scorecard.get("dimensions") if isinstance(scorecard, Mapping) else None
    if isinstance(dimensions, list):
        for item in dimensions:
            if not isinstance(item, Mapping):
                continue
            identifier = str(item.get("dimensionId") or "")
            score = item.get("score")
            if identifier and isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(float(score)):
                before[identifier] = float(score)
    after = numeric_metrics(eval_run.get("metrics"))
    source_fingerprint = fingerprint(source_trace)
    repair_fingerprint = fingerprint(repair_trace)
    if not source_fingerprint or not repair_fingerprint:
        status = "unknown"
        reason = "至少一条 Trace 缺少 input fingerprint，不能判断修复前后是否可比。"
    elif source_fingerprint != repair_fingerprint:
        status = "incomparable"
        reason = "输入 fingerprint 不同，只能并列展示，不能声称效果提升。"
    else:
        status = "incomparable"
        reason = "输入 fingerprint 一致，但缺少同一 Eval suite/rubric 与环境快照的修复前基线，不能计算差值。"
    return {
        "status": status,
        "reason": reason,
        "sourceStatus": str(source_trace.get("status") or "")[:80],
        "repairStatus": str(repair_trace.get("status") or "")[:80],
        "sourceFingerprint": source_fingerprint,
        "repairFingerprint": repair_fingerprint,
        "beforeMetrics": before,
        "afterMetrics": after,
        "deltas": {},
    }


_TRACE_DIAGNOSTIC_TARGET_FIELDS = frozenset(
    {"kind", "id", "title", "traceIds"}
)


def _strict_trace_diagnostic_targets(
    value: Sequence[object],
) -> list[Mapping[str, object]]:
    """Validate the nested target array before the extractor sees it.

    The tool argument schema is a disclosure contract, not an execution-time
    validator. Keep the service boundary strict too: dropping a malformed
    target would make a requested multi-target diagnosis silently incomplete.
    """

    if not 1 <= len(value) <= 12:
        raise ValueError("Trace diagnostic targets must contain between 1 and 12 objects")
    targets: list[Mapping[str, object]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"Trace diagnostic targets must contain only objects (index {index})"
            )
        unknown = sorted(
            str(key) for key in raw if key not in _TRACE_DIAGNOSTIC_TARGET_FIELDS
        )
        if unknown:
            raise ValueError(
                "Trace diagnostic target contains unsupported fields: "
                + ", ".join(str(item) for item in unknown)
            )
        for field in ("kind", "id", "title"):
            if field not in raw or not isinstance(raw[field], str):
                raise ValueError(
                    f"Trace diagnostic target {field} must be a string (index {index})"
                )
        if raw["kind"] not in {"session", "room", "run"}:
            raise ValueError(
                f"Trace diagnostic target kind is invalid (index {index})"
            )
        if not raw["id"].strip() or len(raw["id"]) > 240:
            raise ValueError(
                f"Trace diagnostic target id is invalid (index {index})"
            )
        if len(raw["title"]) > 240:
            raise ValueError(
                f"Trace diagnostic target title is too long (index {index})"
            )
        trace_ids = raw.get("traceIds")
        if trace_ids is not None:
            if not isinstance(trace_ids, Sequence) or isinstance(
                trace_ids, (str, bytes, bytearray)
            ):
                raise ValueError(
                    f"Trace diagnostic target traceIds must be an array (index {index})"
                )
            if len(trace_ids) > 32:
                raise ValueError(
                    f"Trace diagnostic target traceIds has too many items (index {index})"
                )
            if any(
                not isinstance(trace_id, str)
                or not trace_id.strip()
                or len(trace_id) > 240
                for trace_id in trace_ids
            ):
                raise ValueError(
                    "Trace diagnostic target traceIds must contain only non-empty strings "
                    f"(index {index})"
                )
            if len(set(trace_ids)) != len(trace_ids):
                raise ValueError(
                    f"Trace diagnostic target traceIds must be unique (index {index})"
                )
        targets.append(raw)
    return targets


def _diagnostic_failure_reason(
    snapshot: Mapping[str, object],
    *,
    session: Mapping[str, object] | None = None,
    status: str = "faulted",
) -> str:
    """Choose a public terminal-failure reason; the report store redacts it."""

    candidates: list[object] = []
    for source in (snapshot, session or {}):
        for key in (
            "failureReason",
            "error",
            "lastError",
            "reason",
            "lastMessagePreview",
        ):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value)
    raw_live_events = snapshot.get("liveEvents")
    live_events = (
        [item for item in raw_live_events if isinstance(item, Mapping)]
        if isinstance(raw_live_events, Sequence)
        and not isinstance(raw_live_events, (str, bytes, bytearray))
        else []
    )
    for event in reversed(live_events):
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        for key in ("error", "failureReason", "reason", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value)
    detail = " ".join(str(candidates[0]).split())[:800] if candidates else ""
    public_status = status if status in {"faulted", "failed", "error", "cancelled", "canceled"} else "failed"
    return (
        f"诊断 Session 以 {public_status} 终态结束：{detail}"
        if detail
        else f"诊断 Session 以 {public_status} 终态结束，未生成诊断报告。"
    )


def _trace_repair_payload(
    payload: Mapping[str, object],
    *,
    schema_version: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, object]:
    """Validate a small Trace repair HTTP request before storage access."""

    if not isinstance(payload, Mapping):
        raise TraceRepairValidationError("Trace repair request must be an object")
    values = dict(payload)
    if values.get("schemaVersion") != schema_version:
        raise TraceRepairValidationError(
            f"schemaVersion must be {schema_version}"
        )
    allowed = required | set(optional or ()) | {"schemaVersion"}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise TraceRepairValidationError(
            f"Trace repair request contains unsupported fields: {', '.join(unknown)}"
        )
    missing = sorted(required - set(values))
    if missing:
        raise TraceRepairValidationError(
            f"Trace repair request is missing fields: {', '.join(missing)}"
        )
    return values


def _required_trace_repair_text(
    payload: Mapping[str, object],
    key: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TraceRepairValidationError(f"{key} must be a non-empty string")
    return value.strip()


def _trace_repair_candidate_payload(
    payload: Mapping[str, object],
    *,
    schema_version: str,
) -> dict[str, object]:
    return _trace_repair_payload(
        payload,
        schema_version=schema_version,
        required={"repairSessionId", "repairTraceId"},
    )


def _trace_repair_receipt_payload(
    payload: Mapping[str, object],
) -> dict[str, object]:
    return _trace_repair_payload(
        payload,
        schema_version="rag-ime.trace-repair-receipt-create.v1",
        required={
            "sourceScope",
            "sourceTraceId",
            "failureRef",
            "changeReceiptId",
            "testEvidenceId",
            "repairTraceId",
            "repairSessionId",
        },
    )


def _trace_repair_recheck_payload(
    payload: Mapping[str, object],
) -> dict[str, object]:
    return _trace_repair_payload(
        payload,
        schema_version="rag-ime.trace-repair-recheck-request.v1",
        required={"repairReceiptId"},
    )


def _trace_replay_request(
    payload: Mapping[str, object],
    *,
    schema_version: str,
    required: set[str],
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise TraceVerificationValidationError(
            "Trace replay request must be an object"
        )
    values = dict(payload)
    if values.get("schemaVersion") != schema_version:
        raise TraceVerificationValidationError(
            f"schemaVersion must be {schema_version}"
        )
    allowed = required | {"schemaVersion"}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise TraceVerificationValidationError(
            "Trace replay request contains unsupported fields: "
            + ", ".join(unknown)
        )
    missing = sorted(required - set(values))
    if missing:
        raise TraceVerificationValidationError(
            "Trace replay request is missing fields: " + ", ".join(missing)
        )
    return values


def _required_trace_verification_text(
    payload: Mapping[str, object], key: str
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TraceVerificationValidationError(
            f"{key} must be a non-empty string"
        )
    return value.strip()


def _observation_trace_id(payload: Mapping[str, object]) -> str:
    try:
        trace_id = _required_text(payload, "traceId")
    except ValueError as exc:
        raise ValueError("invalid_trace_id") from exc
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", trace_id) is None:
        raise ValueError("invalid_trace_id")
    return trace_id


def _public_ai_judge_usage(value: object) -> dict[str, int] | None:
    """Project provider usage into the bounded EvalRun public shape."""

    if not isinstance(value, Mapping):
        return None
    usage: dict[str, int] = {}
    for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        number = float(raw)
        if not math.isfinite(number) or number < 0:
            continue
        usage[key] = int(number)
    return usage or None


def _public_ai_judge_cost(response: Mapping[str, object]) -> dict[str, float | int] | None:
    """Project provider cost metadata without retaining arbitrary diagnostics."""

    raw_cost = response.get("cost")
    if isinstance(raw_cost, Mapping):
        cost: dict[str, float | int] = {}
        for key in ("input", "output", "cacheRead", "cacheWrite", "total"):
            raw = raw_cost.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            amount = float(raw)
            if math.isfinite(amount) and amount >= 0:
                cost[key] = raw
        return cost or None
    for key in ("costUsd", "totalCost", "total"):
        raw = response.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        amount = float(raw)
        if math.isfinite(amount) and amount >= 0:
            return {"total": raw}
    return None


def _public_ai_judge_latency(response: Mapping[str, object]) -> int | None:
    for key in ("latencyMs", "elapsedMs"):
        raw = response.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        number = float(raw)
        if math.isfinite(number) and number >= 0:
            return int(number)
    return None


def _media_owner_input(
    *,
    session_id: object = "",
    room_id: object = "",
) -> tuple[str, str]:
    session = str(session_id or "").strip()
    room = str(room_id or "").strip()
    if bool(session) == bool(room):
        raise ValueError("exactly one sessionId or roomId is required")
    owner_id = session or room
    if (
        len(owner_id) > 160
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]*", owner_id)
    ):
        raise ValueError("media owner id is invalid")
    return ("session", session) if session else ("room", room)


def _room_attachment_ids(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("attachmentIds must be an array")
    if len(value) > 8:
        raise ValueError("a Room message supports at most 8 images")
    result: list[str] = []
    for item in value:
        media_id = str(item or "").strip()
        if not re.fullmatch(r"media_[A-Za-z0-9_-]{12,80}", media_id):
            raise ValueError("attachmentIds must contain managed mediaId values")
        if media_id not in result:
            result.append(media_id)
    return result














def _optional_work_item_id(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("workItemId must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > 320 or any(
        ord(char) < 32 for char in normalized
    ):
        raise ValueError("workItemId must contain between 1 and 320 safe characters")
    return normalized


def _public_error(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return text[:240] or error.__class__.__name__


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _integer(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()








def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
