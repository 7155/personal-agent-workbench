"""One descriptor per route, replacing hand-written dispatch branches.

`debug_server` grew a 1,205-line `do_GET` and an 821-line `do_POST`, each an
if/elif chain in which every route restates method matching, parsing, the
application call and the response shape. Nothing links a branch to the policy
that declares the same route, which is how a branch came to be tested twice
with the second copy unreachable.

A `RouteDescriptor` states one route once: method, path, whether it is exposed
remotely, how the request becomes handler arguments, and which application
method serves it. The dispatcher below is the single owner for the families
that have been migrated; families still in the chains are untouched, and a
route is never served by both, because `debug_server` consults the table first
and returns immediately on a hit.

Migration is per-family on purpose. Moving all 237 routes at once could not be
reviewed, and the route-ownership gate ratchets the remaining count down as
families arrive.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from ..desktop_files import file_error_response

from .lab_errors import (
    lab_golden_error_response,
    lab_project_error_response,
    lab_scene_recipe_error_response,
    lab_trial_error_response,
)


ErrorResponseAdapter = Callable[[Exception], tuple[HTTPStatus, dict[str, object]]]


def _value_error_response(exc: Exception) -> tuple[HTTPStatus, dict[str, object]]:
    """Preserve a legacy route's bounded invalid-request projection."""

    if not isinstance(exc, ValueError):
        raise exc
    return HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}


@dataclass(frozen=True)
class RouteDescriptor:
    """Everything one route needs, in one place."""

    method: str
    path: str
    handler: str
    """Attribute path on the application service that serves this route.

    Dotted for handlers reached through a sub-service, e.g.
    `management.configuration_import_preview`, so a descriptor can name any
    real call site rather than only top-level methods.
    """
    remote_safe: bool = False
    """Whether the Agent Gateway may reach it; local surfaces stay local."""
    status: int = 200
    query_args: tuple[str, ...] = ()
    """Query parameters lifted into the handler payload, for GET routes."""
    payload_args: Mapping[str, Any] = field(default_factory=dict)
    """Fixed keyword arguments, e.g. an action discriminator."""
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    """Optional payload adjustment applied before the handler runs."""
    aliases: tuple[str, ...] = ()
    """Legacy paths serving the same route, e.g. the un-prefixed `/prediction/*`.

    They are registered as ordinary table entries so an alias can never drift
    from the route it mirrors.
    """
    contract: str = ""
    """JSON-schema contract validated against the payload before dispatch.

    Several chain branches called `validate_contract` before their handler;
    naming the schema keeps that a property of the route rather than a step a
    future migration could silently drop.
    """
    response_contract: str = ""
    """JSON-schema contract validated against the response before it is sent.

    Distinct from `contract`, which validates the request: the frontend
    capabilities route checked its own output, and collapsing the two would
    silently move where validation happens.
    """
    takes_arguments: bool = True
    """False for handlers that take no request data at all, e.g. status reads.

    Passing an empty dict to those would be a TypeError, so the descriptor has
    to say which shape the handler expects rather than the dispatcher guessing.
    """
    error_response: ErrorResponseAdapter | None = None
    """Opt-in HTTP error projection for migrated reads.

    Covers parsing through response serialization, as the original GET branch
    did. An absent adapter keeps the route's existing exception propagation.
    Authentication and special transports remain outside this boundary.
    """


def _as_model_profile(payload: dict[str, Any]) -> dict[str, Any]:
    """`/api/models/profile/save` is the profile saver pinned to one kind."""

    return {**payload, "kind": "model_profile"}


def _tag_phonetic_correction(payload: dict[str, Any]) -> dict[str, Any]:
    """Preserve the one non-uniform vocabulary branch exactly.

    The original chain appended the `phonetic_correction` tag to whatever tags
    the caller sent before delegating to the shared save handler. Keeping it as
    a declared transform means the descriptor still describes the route fully,
    instead of pushing a special case back into the dispatcher.
    """

    tags = payload.get("tags")
    existing = [str(item) for item in tags if str(item)] if isinstance(tags, list) else []
    return {**payload, "tags": [*existing, "phonetic_correction"]}


# Vocabulary is the first migrated family: seven routes, entirely local, each a
# straight payload-to-service call, so the descriptor shape can be proven
# against a family whose behaviour is unambiguous.
FILES_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(method="GET", path="/api/files/list", handler="desktop_files.list",
                    query_args=("path", "offset", "limit"), error_response=file_error_response),
    RouteDescriptor(method="GET", path="/api/files/read", handler="desktop_files.read",
                    query_args=("path", "offset", "limit", "sessionId"), error_response=file_error_response),
)

VOCABULARY_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(
        method="GET",
        path="/api/vocabulary/items",
        handler="vocabulary_items",
        # Exactly the two the original branch forwarded; adding others would
        # change the payload the service receives.
        query_args=("status", "query"),
    ),
    RouteDescriptor(
        method="POST",
        path="/api/vocabulary/item/add",
        handler="vocabulary_item_save",
        payload_args={"action": "add"},
    ),
    RouteDescriptor(
        method="POST",
        path="/api/vocabulary/item/edit",
        handler="vocabulary_item_save",
        payload_args={"action": "edit"},
    ),
    RouteDescriptor(
        method="POST",
        path="/api/vocabulary/item/delete",
        handler="vocabulary_item_delete",
    ),
    RouteDescriptor(
        method="POST",
        path="/api/vocabulary/phonetic-correction/add",
        handler="vocabulary_item_save",
        payload_args={"action": "phonetic_correction_add"},
        transform=_tag_phonetic_correction,
    ),
    RouteDescriptor(
        method="POST",
        path="/api/vocabulary/rime-export-preview",
        handler="vocabulary_rime_export_preview",
    ),
    RouteDescriptor(
        method="POST",
        path="/api/vocabulary/rime-export-apply",
        handler="vocabulary_rime_export_apply",
    ),
)

# RAG core v3: two reads that forward named query parameters, three writes that
# hand the payload straight to the service. Same shape as vocabulary, so it
# migrates without extending the descriptor.
RAG_CORE_V3_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(
        method="GET",
        path="/api/rag-core-v3/doc",
        handler="rag_core_v3_doc",
        query_args=("id",),
    ),
    RouteDescriptor(
        method="GET",
        path="/api/rag-core-v3/tag-graph",
        handler="rag_core_v3_tag_graph",
        query_args=("tag", "limit"),
    ),
    RouteDescriptor(
        method="POST",
        path="/api/rag-core-v3/query-preview",
        handler="rag_core_v3_query_preview",
    ),
    RouteDescriptor(
        method="POST",
        path="/api/rag-core-v3/rebuild-retrieval-docs",
        handler="rag_core_v3_rebuild_retrieval_docs",
    ),
    RouteDescriptor(
        method="POST",
        path="/api/rag-core-v3/memory-book-preview",
        handler="rag_core_v3_memory_book_preview",
    ),
)

# Lexicon and cleanup-diff: small write families with one paired read.
LEXICON_ROUTES: tuple[RouteDescriptor, ...] = (
    # GET and POST on this path share one service method; the descriptors are
    # distinct because the request becomes handler arguments differently
    # (named query parameters versus the JSON payload).
    RouteDescriptor(
        method="GET",
        path="/api/lexicon/export-rime",
        handler="management_lexicon_export_rime",
        query_args=("limit", "project", "status", "kind", "dryRun"),
    ),
    RouteDescriptor(
        method="POST",
        path="/api/lexicon/action",
        handler="management_lexicon_action",
    ),
    RouteDescriptor(
        method="POST",
        path="/api/lexicon/export-rime",
        handler="management_lexicon_export_rime",
    ),
)

CLEANUP_DIFF_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(
        method="POST",
        path="/api/cleanup-diff/apply",
        handler="management_cleanup_diff_apply",
    ),
    RouteDescriptor(
        method="POST",
        path="/api/cleanup-diff/rollback",
        handler="management_cleanup_diff_rollback",
    ),
)

# Models and profiles: two argument-free status reads, plus writes that are
# straight payload calls except the model profile save, which is the shared
# profile saver pinned to one kind.
MODEL_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(
        method="GET", path="/api/models/status",
        handler="models_status", takes_arguments=False,
    ),
    RouteDescriptor(
        method="GET", path="/api/models/profiles",
        handler="model_profiles", takes_arguments=False,
    ),
    RouteDescriptor(method="POST", path="/api/models/probe", handler="model_probe"),
    RouteDescriptor(method="POST", path="/api/models/benchmark", handler="model_benchmark_job"),
    RouteDescriptor(method="POST", path="/api/models/matrix-eval", handler="model_benchmark_job"),
    RouteDescriptor(
        method="POST", path="/api/models/profile/save",
        handler="profile_save", transform=_as_model_profile,
    ),
    RouteDescriptor(
        method="POST", path="/api/models/profile/activate-dry-run",
        handler="model_activate_dry_run",
    ),
)

PROFILE_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(method="POST", path="/api/profiles/save", handler="profile_save"),
    RouteDescriptor(
        method="POST", path="/api/profiles/activate-dry-run",
        handler="profile_activate_dry_run",
    ),
)

# Settings: one argument-free schema read plus five payload writes.
SETTINGS_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(
        method="GET", path="/api/settings/schema",
        handler="settings_schema", takes_arguments=False,
    ),
    RouteDescriptor(method="POST", path="/api/settings/preview", handler="configuration_settings_preview"),
    RouteDescriptor(method="POST", path="/api/settings/apply", handler="configuration_settings_apply"),
    RouteDescriptor(method="POST", path="/api/settings/rollback", handler="configuration_settings_rollback"),
    RouteDescriptor(method="POST", path="/api/settings/update", handler="settings_update"),
    RouteDescriptor(method="POST", path="/api/settings/reset-section", handler="settings_reset_section"),
)

# Configuration: reached through the management sub-service. These are already
# declared in route_policy, so migrating them consolidates ownership without
# moving the ratchet.
CONFIGURATION_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(method="POST", path="/api/configuration/import-preview", handler="management.configuration_import_preview"),
    RouteDescriptor(method="POST", path="/api/configuration/import-apply", handler="management.configuration_import_apply"),
    RouteDescriptor(method="POST", path="/api/configuration/backup-export", handler="management.portable_backup_export"),
    RouteDescriptor(method="POST", path="/api/configuration/restore-preview", handler="management.portable_restore_preview"),
    RouteDescriptor(method="POST", path="/api/configuration/restore-apply", handler="management.portable_restore_apply"),
)

# Prediction, memories and rime-lexicon. Prediction keeps its un-prefixed
# aliases, which the chain served from the same branch.
PREDICTION_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(
        method="GET", path="/api/prediction/live-trace",
        aliases=("/prediction/live-trace",),
        handler="prediction_live_trace", query_args=("limit", "sessionId"),
    ),
    RouteDescriptor(
        method="GET", path="/api/prediction/drop-stats",
        aliases=("/prediction/drop-stats",),
        handler="prediction_drop_stats", query_args=("limit",),
    ),
)

MEMORIES_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(
        method="GET", path="/api/memories", handler="management_memories",
        query_args=("limit", "project", "status", "kind"),
    ),
    RouteDescriptor(
        method="POST", path="/api/memories/action", handler="management_memory_action",
    ),
)

RIME_LEXICON_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(
        method="GET", path="/api/rime-lexicon/review",
        handler="rime_lexicon_review", query_args=("limit", "project"),
    ),
    RouteDescriptor(method="POST", path="/api/rime-lexicon/apply", handler="rime_lexicon_apply"),
    RouteDescriptor(method="POST", path="/api/rime-lexicon/rollback", handler="rime_lexicon_rollback"),
)

def _post(
    path: str,
    handler: str,
    *,
    aliases: tuple[str, ...] = (),
    contract: str = "",
    takes_arguments: bool = True,
) -> RouteDescriptor:
    return RouteDescriptor(
        method="POST", path=path, handler=handler, aliases=aliases,
        contract=contract, takes_arguments=takes_arguments,
    )


# Foreground and memory single-path writes. Each kept its un-prefixed alias,
# and the three contract-validated routes keep validation as a declared field.
FOREGROUND_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/action", "action", aliases=("/action",)),
    _post("/api/cache-probe", "cache_probe", aliases=("/cache-probe",)),
    _post("/api/candidate-edit-feedback", "candidate_edit_feedback", aliases=("/candidate-edit-feedback",)),
    _post("/api/commit", "commit", aliases=("/commit",), contract="foreground-commit.v1.json"),
    _post("/api/predictor-ttfc", "predictor_ttfc", aliases=("/predictor-ttfc",)),
    _post("/api/rime-rank-feedback", "rime_rank_feedback", aliases=("/rime-rank-feedback",), contract="rime-rank-selection.v1.json"),
    _post("/api/assistant-candidate-action", "assistant_candidate_action", aliases=("/assistant-candidate-action",), contract="assistant-candidate-action.v1.json"),
    _post("/api/seed", "seed", aliases=("/seed",), takes_arguments=False),
)

MEMORY_TOOL_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/generate-memory", "generate_memory", aliases=("/generate-memory",)),
    _post("/api/memory-candidate-explain", "memory_candidate_explain", aliases=("/memory-candidate-explain",)),
    _post("/api/memory-cleanup-runs", "memory_cleanup_runs", aliases=("/memory-cleanup-runs",)),
    _post("/api/memory-governance", "memory_governance", aliases=("/memory-governance",)),
    _post("/api/memory-history", "memory_history", aliases=("/memory-history",)),
    _post("/api/memory-optimizer-trace", "memory_optimizer_trace", aliases=("/memory-optimizer-trace",)),
    _post("/api/memory-tombstone", "memory_tombstone", aliases=("/memory-tombstone", "/api/memory/tombstone")),
    _post("/api/organize-rag-db", "organize_rag_database", aliases=("/organize-rag-db",)),
    _post("/api/rebuild-vector-index", "rebuild_vector_index", aliases=("/rebuild-vector-index",)),
    _post("/api/deepseek/completion-preview", "deepseek_completion_preview"),
)

def _get(
    path: str,
    handler: str,
    *,
    aliases: tuple[str, ...] = (),
    query_args: tuple[str, ...] = (),
    takes_arguments: bool = False,
    response_contract: str = "",
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    error_response: ErrorResponseAdapter | None = None,
) -> RouteDescriptor:
    """Most migrated reads take no request data, so that is the default here."""

    return RouteDescriptor(
        method="GET", path=path, handler=handler, aliases=aliases,
        query_args=query_args, takes_arguments=takes_arguments,
        response_contract=response_contract, transform=transform,
        error_response=error_response,
    )


AGENT_LAB_TRIAL_ROUTES: tuple[RouteDescriptor, ...] = (
    _get(
        "/api/agent/eval-lab/trials", "agent.eval_lab_trials",
        query_args=("jobId",), takes_arguments=True,
        error_response=lab_trial_error_response,
    ),
    RouteDescriptor(
        method="POST", path="/api/agent/eval-lab/trials/start",
        handler="agent.eval_lab_trial_start", status=202,
    ),
    _post("/api/agent/eval-lab/trials/cancel", "agent.eval_lab_trial_cancel"),
)


# Status and catalog reads. Handlers are dotted where the chain reached through
# a sub-service; none of them take request data except `/api/profiles`.
READ_ROUTES: tuple[RouteDescriptor, ...] = (
    _get("/api/health", "health", aliases=("/health",)),
    _get("/api/input-source", "input_source_status", aliases=("/input-source",)),
    _get("/api/overview", "management.overview"),
    _get("/api/settings", "settings"),
    _get("/api/predictor/status", "predictor_status"),
    _get("/api/predictor/cache/stats", "predictor_cache_stats"),
    _get("/api/active-rag/settings", "active_rag_settings"),
    _get("/api/knowledge/route-status", "knowledge_workbench_route_status"),
    _get("/api/memory/summary", "management.memory_summary"),
    _get("/api/runtime/status", "management.runtime_status"),
    _get("/api/runtime/config", "runtime_config"),
    _get("/api/runtime/components", "management.runtime_components"),
    _get("/api/browser/status", "browser_control.status"),
    _get("/api/browser/tabs", "browser_control.tabs"),
    _get("/api/agent/runtime", "agent.runtime_status"),
    _get(
        "/api/agent/sessions",
        "agent.list_sessions",
        query_args=(
            "includeArchived", "includeInternal", "limit", "beforeUpdatedAtMs",
            "beforeId", "surfaceKind", "ownerAppId", "surfaceKey", "projectionOnly",
        ),
        takes_arguments=True,
        error_response=_value_error_response,
    ),
    _get(
        "/api/agent/wake-schedules",
        "agent.list_wake_schedules",
        query_args=("status", "targetType", "targetId", "createdBySessionId", "limit"),
        takes_arguments=True,
    ),
    _get(
        "/api/agent/approvals",
        "agent.list_approvals",
        query_args=("sessionId", "state", "limit"),
        takes_arguments=True,
    ),
    _get(
        "/api/agent/rooms",
        "agent.list_rooms",
        query_args=(
            "includeArchived", "limit", "beforeUpdatedAtMs", "beforeId",
            "projectionOnly", "ownerAppId", "surfaceKey",
        ),
        takes_arguments=True,
    ),
    _get(
        "/api/agent/memory-sources",
        "agent.list_memory_sources",
        query_args=("sessionId", "limit"),
        takes_arguments=True,
    ),
    _get(
        "/api/agent/media",
        "agent.list_media",
        query_args=("sessionId", "roomId", "limit"),
        takes_arguments=True,
    ),
    _get(
        "/api/agent/eval-lab/runs",
        "agent.eval_lab_runs",
        response_contract="eval-lab-run-list.v1.json",
    ),
    _get(
        "/api/agent/eval-lab/evidence",
        "agent.eval_lab_evidence_read",
        query_args=("runId", "taskIndex"),
        takes_arguments=True,
    ),
    _get(
        "/api/agent/eval-lab/scene-recipes",
        "agent.eval_lab_scene_recipes",
        query_args=("sceneId", "experimentId"),
        takes_arguments=True,
        error_response=lab_scene_recipe_error_response,
    ),
    _get(
        "/api/agent/eval-lab/golden",
        "agent.eval_lab_golden",
        query_args=("suiteId",),
        takes_arguments=True,
        error_response=lab_golden_error_response,
    ),
    _get(
        "/api/agent/eval-lab/projects",
        "agent.eval_lab_projects",
        query_args=("projectId", "materialSetId", "artifactId", "artifactRevision"),
        takes_arguments=True,
        error_response=lab_project_error_response,
    ),
    _get('/api/agent/eval-lab/apps','agent.eval_lab_apps',query_args=('appId','projectId','version','callId'),takes_arguments=True),
    _get('/api/agent/eval-lab/apps/download','agent.eval_lab_app_download',query_args=('appId','version','target'),takes_arguments=True),
    _get("/api/agent/providers", "pi_provider_auth.catalog"),
    _get("/api/agent/extensions", "agent_extensions.list"),
    _get("/api/agent/extensions/catalog", "agent_extensions.catalog"),
    _get("/api/agent/extensions/skills", "agent_extensions.skills_list"),
    _get(
        "/api/agent/extensions/skills/detail",
        "agent_extensions.skill_detail",
        query_args=("skillId",),
        takes_arguments=True,
    ),
    _get("/api/agent/extensions/proposals", "agent_extensions.proposals"),
    _get(
        "/api/observability/sandbox-runs",
        "agent.observability_sandbox_runs",
        aliases=("/control/v1/observability/sandbox-runs",),
        query_args=("limit",),
        takes_arguments=True,
        response_contract="observability-sandbox-run-list.v1.json",
    ),
    RouteDescriptor(
        method="GET",
        path="/api/observability/sandbox-runs/{sandboxRunId}",
        handler="agent.observability_sandbox_run",
        aliases=("/control/v1/observability/sandbox-runs/{sandboxRunId}",),
        takes_arguments=True,
        response_contract="sandbox-run.v1.json",
    ),
    _get("/api/agent/roles", "agent.list_roles"),
    _get("/api/agent/roles/models", "agent.role_model_catalog"),
    _get("/api/agent/subagents/templates", "agent.list_agent_templates"),
    _get("/api/agent/configuration", "agent.configuration"),
    _get("/api/profiles", "profiles", query_args=("kind",), takes_arguments=True),
    _get(
        "/api/frontend/v1/capabilities", "frontend_capabilities",
        aliases=("/frontend/v1/capabilities",),
        response_contract="frontend-capabilities.v1.json",
    ),
)

EXTENSION_SANDBOX_EXPERIMENT_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(
        method="POST",
        path="/api/extensions/sandbox/experiments",
        handler="extension_sandbox_experiments.execute",
    ),
)

def _session_id_from_either_key(payload: dict[str, Any]) -> dict[str, Any]:
    """`sessionId` wins, `id` is the fallback, and only `sessionId` is sent on.

    The active-RAG reads accepted either spelling: the chain wrote
    `_query_first(query, "sessionId") or _query_first(query, "id")`. Both names
    have to be declared as query arguments to be read at all, so the transform
    collapses them again -- passing `id` through as well would hand the service
    a key it never received before.
    """

    return {"sessionId": payload.get("sessionId") or payload.get("id") or ""}


def _session_traces_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    """The trace reads take the same session-id fallback plus a limit."""

    return {**_session_id_from_either_key(payload), "limit": payload.get("limit") or ""}


# Filtered management reads. Each chain branch built one dict of named query
# parameters and passed it as the handler's single positional payload, so the
# descriptor states the same thing with the parameter list as data. The names
# are copied from the branch they replace: a read that forwards a parameter the
# service no longer receives, or stops forwarding one, changes the result, so
# these tuples are exact rather than tidied.
MANAGEMENT_READ_ROUTES: tuple[RouteDescriptor, ...] = (
    _get("/api/audit", "management_audit", query_args=("limit", "action"), takes_arguments=True),
    _get(
        "/api/history", "management_history", takes_arguments=True,
        query_args=("limit", "project", "query", "source", "includeDeleted", "generatedOnly"),
    ),
    _get(
        "/api/lexicon", "management_lexicon", takes_arguments=True,
        query_args=("limit", "project", "status", "kind"),
    ),
    _get(
        "/api/cleanup-diff", "management_cleanup_diff", takes_arguments=True,
        query_args=("id", "diffId", "runId", "status", "limit"),
    ),
    _get(
        "/api/candidates/explain", "candidate_explain", takes_arguments=True,
        query_args=("query", "currentInput", "recentContext", "project", "app", "topK"),
    ),
    _get(
        "/api/predictor/latency", "predictor_latency", takes_arguments=True,
        query_args=("log", "last"),
    ),
)

# Suggestion and runtime-action writes. Three carry a status other than 200 and
# two validate both request and response, so the descriptors state those rather
# than letting the dispatcher assume: `202 Accepted` on the runtime actions is
# what tells a caller the work was queued, not completed.
SUGGESTION_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/suggest", "suggest", aliases=("/suggest",)),
    RouteDescriptor(
        method="POST", path="/api/frontend/v1/suggest", handler="frontend_suggest",
        aliases=("/frontend/v1/suggest",),
        contract="frontend-suggest-request.v1.json",
        response_contract="frontend-suggest-response.v1.json",
    ),
    RouteDescriptor(
        method="POST", path="/api/frontend/v1/select", handler="frontend_select",
        aliases=("/frontend/v1/select",),
        contract="frontend-selection.v1.json",
        response_contract="frontend-selection-response.v1.json",
    ),
)

RUNTIME_ACTION_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(
        method="POST", path="/api/runtime/action",
        handler="management.start_runtime_action", status=202,
    ),
    RouteDescriptor(
        method="POST", path="/api/runtime/action/preview",
        handler="management.runtime_action_preview",
    ),
    RouteDescriptor(
        method="POST", path="/api/runtime/action/start",
        handler="management.runtime_action_start", status=202,
    ),
)

# Memory writes reached through the management sub-service. `edit` and
# `source/disposition` are already declared in route_policy, so they
# consolidate ownership without moving the ratchet.
MEMORY_WRITE_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/memory/action", "management.memory_action"),
    _post("/api/memory/edit", "management.memory_edit"),
    _post("/api/memory/source/disposition", "management.memory_source_disposition"),
)

# Planning, history tombstones and memory-book archiving. Twenty writes that
# were each one line in the chain: take the payload, call one management
# method, answer 200. Nothing distinguishes them but the handler name, which is
# precisely what a descriptor holds.
PLANNING_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/planning/mutation/preview", "management.planning_mutation_preview"),
    _post("/api/planning/mutation/rollback", "management.planning_mutation_rollback"),
    _post("/api/planning/plan/save", "management.planning_save_plan"),
    _post("/api/planning/goal/save", "management.planning_apply_goal_save"),
    _post("/api/planning/task/save", "management.planning_apply_task_save"),
    _post("/api/planning/task/action", "management.planning_apply_task_action"),
    _post("/api/planning/task-event/undo", "management.planning_undo_task_event_contract"),
    _post("/api/planning/completion/resolve", "management.planning_resolve_completion"),
    _post("/api/planning/assistant", "management.planning_assistant"),
)

# `/api/history/tombstone` is served by a top-level handler while its
# preview/apply/rollback siblings go through the management sub-service. The
# descriptors keep that difference rather than assuming the family is uniform.
HISTORY_TOMBSTONE_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/history/tombstone", "management_history_tombstone"),
    _post("/api/history/tombstone/preview", "management.history_tombstone_preview"),
    _post("/api/history/tombstone/apply", "management.history_tombstone_apply"),
    _post("/api/history/tombstone/rollback", "management.history_tombstone_rollback"),
)

MEMORY_BOOK_ARCHIVE_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/memory/book/archive-status", "management.memory_book_archive_status"),
    _post("/api/memory/book/archive-maintenance", "management.memory_book_archive_maintenance"),
    _post("/api/memory/book/archive/preview", "management.memory_book_archive_preview"),
    _post("/api/memory/book/archive/apply", "management.memory_book_archive_apply"),
    _post("/api/memory/book/archive/rollback", "management.memory_book_archive_rollback"),
)

PREDICTOR_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/predictor/benchmark", "predictor_benchmark"),
    _post("/api/predictor/cache/clear", "predictor_cache_clear"),
)

# Active RAG writes. `/status` and `/session` are two paths for one handler,
# which the chain served from a single branch; they stay two descriptors so
# neither can drift. Only the GET side of those paths remains in the chain,
# because a GET reads its session id from the query string or the trailing
# path segment rather than from a payload.
ACTIVE_RAG_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/active-rag/start", "active_rag_start", contract="active-rag-start.v1.json"),
    _post("/api/active-rag/settings/update", "active_rag_settings_update"),
    _post("/api/active-rag/preview", "active_rag_preview"),
    _post("/api/active-rag/status", "active_rag_status"),
    _post("/api/active-rag/session", "active_rag_status"),
    _post("/api/active-rag/diagnostics", "active_rag_diagnostics"),
    _post("/api/active-rag/cancel", "active_rag_cancel"),
    _post("/api/active-rag/accept", "active_rag_accept"),
)

# Knowledge workbench writes. The templated `/api/knowledge/{base}/...` routes
# stay in the chain: they parse path segments into several handler arguments,
# which a descriptor cannot state.
KNOWLEDGE_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/knowledge/vault", "knowledge_control.vault"),
    _post("/api/knowledge/start", "knowledge_workbench_start"),
    _post("/api/knowledge/status", "knowledge_workbench_status"),
    _post("/api/knowledge/session", "knowledge_workbench_status"),
    _post("/api/knowledge/cancel", "knowledge_workbench_cancel"),
    _post("/api/knowledge/database/apply-preview", "knowledge_workbench_database_apply_preview"),
    _post("/api/knowledge/database/apply", "knowledge_workbench_database_apply_contract"),
    _post("/api/knowledge/database/draft-edit", "knowledge_workbench_database_draft_edit"),
    _post("/api/knowledge/database/rollback", "knowledge_workbench_database_rollback_contract"),
)

# `/api/rime-select` validates its request only. Its sibling `/api/rime-suggest`
# deliberately stays in the chain: that branch validates the response against
# three schemas, one of them conditionally, and a single `response_contract`
# field would describe less than the branch does.
RIME_SELECT_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/rime-select", "rime_select", aliases=("/rime-select",),
          contract="rime-select.v1.json"),
)

# Active-RAG reads. `/route-status` and the templated `/session/{id}` stay in
# the chain: the first passes a keyword argument that is None unless the query
# supplied it, and the second reads its id from the path.
ACTIVE_RAG_READ_ROUTES: tuple[RouteDescriptor, ...] = (
    _get("/api/active-rag/status", "active_rag_status", takes_arguments=True,
         query_args=("sessionId", "id"), transform=_session_id_from_either_key),
    _get("/api/active-rag/session", "active_rag_status", takes_arguments=True,
         query_args=("sessionId", "id"), transform=_session_id_from_either_key),
    _get("/api/active-rag/diagnostics", "active_rag_diagnostics", takes_arguments=True,
         query_args=("sessionId", "id"), transform=_session_id_from_either_key),
    _get("/api/active-rag/traces", "active_rag_traces", takes_arguments=True,
         query_args=("sessionId", "id", "limit"), transform=_session_traces_arguments),
    _get("/api/active-rag/chain-trace", "active_rag_traces", takes_arguments=True,
         query_args=("sessionId", "id", "limit"), transform=_session_traces_arguments),
)

# The knowledge reads take the same session-id fallback as the active-RAG ones.
# `/api/control/v1/bootstrap` is not here on purpose: it passes an access
# context the handler derives from the request itself, which is neither a query
# parameter nor a payload, so no descriptor field describes it.
KNOWLEDGE_READ_ROUTES: tuple[RouteDescriptor, ...] = (
    _get("/api/knowledge/status", "knowledge_workbench_status", takes_arguments=True,
         query_args=("sessionId", "id"), transform=_session_id_from_either_key),
    _get("/api/knowledge/session", "knowledge_workbench_status", takes_arguments=True,
         query_args=("sessionId", "id"), transform=_session_id_from_either_key),
)

# Two more filtered management reads, same shape as MANAGEMENT_READ_ROUTES.
# `/suppressions` and `/governance` are two paths for one handler.
MEMORY_READ_ROUTES: tuple[RouteDescriptor, ...] = (
    _get("/api/memory/suppressions", "memory_governance", takes_arguments=True,
         query_args=("limit", "includeInactive")),
    _get("/api/memory/governance", "memory_governance", takes_arguments=True,
         query_args=("limit", "includeInactive")),
    _get("/api/memory/cleanup-runs", "memory_cleanup_runs", takes_arguments=True,
         query_args=("limit", "runId", "status")),
)

# Browser: the product owns one managed Chromium and exposes only its lifecycle
# here. Ego scripts and direct commands share `/api/browser/command`; the old
# extension pairing and permission routes are deliberately no longer public.
BROWSER_ROUTES: tuple[RouteDescriptor, ...] = (
    _post("/api/browser/stop", "browser_control.stop", takes_arguments=False),
    _post("/api/browser/managed/start", "browser_control.start_managed", takes_arguments=False),
    _post("/api/browser/managed/stop", "browser_control.stop_managed", takes_arguments=False),
)

SYSTEM_TERMINAL_ROUTES: tuple[RouteDescriptor, ...] = (
    _get("/api/terminal/sessions", "system_terminal.list", takes_arguments=False),
    _post("/api/terminal/sessions", "system_terminal.create"),
    _post("/api/terminal/read", "system_terminal.read"),
    _post("/api/terminal/write", "system_terminal.write"),
    _post("/api/terminal/resize", "system_terminal.resize"),
    _post("/api/terminal/close", "system_terminal.close_terminal"),
)

WORK_DOCUMENT_ROUTES: tuple[RouteDescriptor, ...] = (
    RouteDescriptor(
        method="GET",
        path="/api/agent/work-documents",
        handler="agent.work_documents.list",
        query_args=("limit",),
        takes_arguments=True,
        response_contract="work-document-list.v1.json",
    ),
    RouteDescriptor(
        method="GET",
        path="/api/agent/work-documents/history/search",
        handler="agent.work_documents.history_search",
        query_args=("query", "limit"),
        takes_arguments=True,
        response_contract="work-document-list.v1.json",
    ),
    RouteDescriptor(
        method="POST",
        path="/api/agent/work-documents",
        handler="agent.work_documents.register",
        status=201,
        response_contract="work-document-command.v1.json",
    ),
)


MIGRATED_ROUTES: tuple[RouteDescriptor, ...] = (
    *FILES_ROUTES,
    *EXTENSION_SANDBOX_EXPERIMENT_ROUTES,
    *SYSTEM_TERMINAL_ROUTES,
    *WORK_DOCUMENT_ROUTES,
    *VOCABULARY_ROUTES,
    *BROWSER_ROUTES,
    *READ_ROUTES,
    *AGENT_LAB_TRIAL_ROUTES,
    *MANAGEMENT_READ_ROUTES,
    *SUGGESTION_ROUTES,
    *RUNTIME_ACTION_ROUTES,
    *MEMORY_WRITE_ROUTES,
    *PLANNING_ROUTES,
    *HISTORY_TOMBSTONE_ROUTES,
    *MEMORY_BOOK_ARCHIVE_ROUTES,
    *PREDICTOR_ROUTES,
    *ACTIVE_RAG_ROUTES,
    *KNOWLEDGE_ROUTES,
    *RIME_SELECT_ROUTES,
    *ACTIVE_RAG_READ_ROUTES,
    *KNOWLEDGE_READ_ROUTES,
    *MEMORY_READ_ROUTES,
    *FOREGROUND_ROUTES,
    *MEMORY_TOOL_ROUTES,
    *PREDICTION_ROUTES,
    *MEMORIES_ROUTES,
    *RIME_LEXICON_ROUTES,
    *SETTINGS_ROUTES,
    *CONFIGURATION_ROUTES,
    *MODEL_ROUTES,
    *PROFILE_ROUTES,
    *RAG_CORE_V3_ROUTES,
    *LEXICON_ROUTES,
    *CLEANUP_DIFF_ROUTES,
)

ROUTE_TABLE: dict[tuple[str, str], RouteDescriptor] = {}
for _route in MIGRATED_ROUTES:
    for _path in (_route.path, *_route.aliases):
        _key = (_route.method, _path)
        if _key in ROUTE_TABLE:  # pragma: no cover - import-time guard
            raise RuntimeError(f"duplicate route {_key}")
        ROUTE_TABLE[_key] = _route




def find_route(method: str, path: str) -> RouteDescriptor | None:
    return ROUTE_TABLE.get((str(method).upper(), str(path)))


def build_arguments(
    route: RouteDescriptor,
    *,
    payload: Mapping[str, Any] | None,
    query_first: Callable[[str], str],
) -> dict[str, Any]:
    """Turn one request into the handler's payload, per the descriptor."""

    if route.method == "GET":
        arguments: dict[str, Any] = {name: query_first(name) for name in route.query_args}
    else:
        arguments = dict(payload or {})
    if route.transform is not None:
        arguments = route.transform(arguments)
    return arguments
