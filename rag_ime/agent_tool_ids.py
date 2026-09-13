from __future__ import annotations


ASSISTANT_CONTROL_TOOL_IDS = (
    "overview",
    "input",
    "voice",
    "planning",
    "agent_schedule",
    "memory",
    "agent_role_book",
    "knowledge",
    "models",
    "runtime",
    "configuration",
    "agents",
    "session_search",
    "trace_diagnostics",
    "room_partner",
    "browser",
    "todo",
    "agent_goal",
    "plugins",
    "sandbox",
    "work_documents",
    "lab_project",
    "lab_research",
    "desktop_semantic",
)

# Plan/Todo and transcript-local Goal state live in the optional native Pi
# Session Workflow Package.  Keep the legacy identifiers hidden by default.
# `ControlToolGateway.runtime_manifests()` makes the narrower exception for an
# active Room Facilitator that must close the separate audited PAW Product Goal.
PI_PACKAGE_OWNED_CONTROL_TOOL_IDS = frozenset({"todo", "agent_goal"})

COORDINATOR_TOOL_IDS = (
    "workspace_list",
    "workspace_lsp",
    "workspace_read",
    "workspace_search",
    "workspace_patch",
    "workspace_edit",
    "workspace_write",
    "workspace_job",
    "workspace_shell",
)

CONTROL_TOOL_IDS = (*ASSISTANT_CONTROL_TOOL_IDS, *COORDINATOR_TOOL_IDS)
CONTROL_CENTER_TOOL_PROFILE = "control-center-v1"
FULL_ACCESS_TOOL_PROFILE = "control-center-full-access-v1"
READONLY_TOOL_PROFILE = "subagent-readonly-v1"
WORKER_TOOL_PROFILE = "subagent-worker-v1"
SURFACE_TOOL_PROFILE = "ime-surface-v1"
VOICE_REFINEMENT_TOOL_PROFILE = "voice-refinement-v1"
MEMORY_CURATION_TOOL_PROFILE = "memory-curation-v1"
DANGEROUS_AUTO_APPROVE_TOOL_PROFILE = "control-center-auto-approve-v1"
DANGEROUS_MODE_CONFIRMATION = "ENABLE_FULL_TRUST"

SUPPORTED_AGENT_TOOL_PROFILES = frozenset(
    {
        CONTROL_CENTER_TOOL_PROFILE,
        FULL_ACCESS_TOOL_PROFILE,
        READONLY_TOOL_PROFILE,
        WORKER_TOOL_PROFILE,
        SURFACE_TOOL_PROFILE,
        VOICE_REFINEMENT_TOOL_PROFILE,
        MEMORY_CURATION_TOOL_PROFILE,
        DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
    }
)
