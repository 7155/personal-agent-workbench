#!/usr/bin/env python3
"""Build a privacy-safe PAW backend review ZIP for a web model.

The package keeps PAW, Pi, Tutti prompt-reference, Tutti DuoAgent-reference,
and PAW Skill provenance separate. Source worktrees are read-only; redactions
apply only to package copies.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Iterable


PAW_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TUTTI_ROOT = PAW_ROOT.parent / "tutti"
DEFAULT_PI_ROOT = PAW_ROOT.parent / "pi"
DEFAULT_SKILLS_ROOT = PAW_ROOT.parent / "pi-skills"
DEFAULT_OUTPUT = PAW_ROOT / "output" / "model-review" / "paw-backend-web-model-review.zip"

GUIDE_FILES = (
    "docs/handoffs/model-bundles/README_FOR_BACKEND_WEB_MODEL.md",
    "docs/handoffs/model-bundles/TUTTI_SYSTEM_PROMPT_INVENTORY.md",
    "docs/handoffs/model-bundles/TUTTI_DUOAGENT_REFERENCE.md",
    "docs/handoffs/model-bundles/PAW_SKILL_INVENTORY.md",
)
AUTHORITY_FILES = (
    "AGENTS.md",
    "control-center-web/docs/README.md",
    "docs/project/PROJECT.md",
    "README.md",
    "docs/project/OUTCOMES.md",
    "docs/project/CONTEXT.md",
    "docs/project/DECISIONS.md",
    "docs/project/ARCHITECTURE.md",
    "control-center-web/docs/pawos/PAWOS_REQUIREMENTS.md",
    "control-center-web/docs/pawos/PAWOS_FRONTEND_HISTORY.md",
)
AUTHORITY_GLOBS = (
    "control-center-web/docs/pawos/requirements/PAWOS_REQUIREMENTS_*.md",
)
INTERFACE_FILES = (
    "control-center-web/docs/handoffs/PAWOS_FUNCTION_INTERFACE_GUIDE.md",
    "control-center-web/docs/handoffs/PAWOS_WEB_MODEL_APP_FUNCTIONS.md",
    "control-center-web/docs/handoffs/PAWOS_WEB_MODEL_REAL_DATA_FIXTURES.md",
)

# Keep evaluation evidence curated.  The rest of `eval/` contains host-private
# prompts, qrels, command lines, and generated run material that is not needed
# by a web-model reviewer.
TRACE_EVIDENCE_FILES = (
    "control-center-web/docs/pawos/PAWOS_TRACE_EVAL.md",
    "eval/interview-metrics/README.md",
    "eval/interview-metrics/TRACE_EVAL_COMPATIBILITY.md",
    "eval/interview-metrics/trace-defect-ledger.v1.json",
    "eval/trace-agent/closed-loop-v1/public-manifest.json",
    "eval/trace-agent/closed-loop-v1/public-manifest.schema.json",
    "eval/interview-metrics/runs/trace-agent-provider-bootstrap-20260901.v1.json",
    "eval/interview-metrics/runs/trace-agent-skill-envelope-validation-20260901.v1.json",
    "eval/interview-metrics/runs/cloudops-agent-validation-20260901.v1.json",
)

# Agent Lab receipts are kept separate from Trace evidence so a reviewer can
# distinguish a diagnostic/repair claim from a model, Prompt, Tool, or cost
# experiment.  These are public projections only: raw sessions, host-private
# Gold/qrels, databases, and Provider credentials remain excluded.
AGENT_LAB_EVIDENCE_FILES = (
    "eval/interview-metrics/agent-experiments.v1.json",
    "eval/interview-metrics/evidence-ledger.v1.json",
    "eval/interview-metrics/openai-codex-runtime-pricing-20260904.v1.json",
    "eval/interview-metrics/enterprise-rag-answer-evidence-standard.v2.json",
    "eval/interview-metrics/enterprise-rag-answer-evidence-standard.validation-candidate-aware-attention-r6.json",
    "eval/interview-metrics/ENTERPRISEOPS_CSM_TRACE_REPAIR_20260901.md",
    "eval/interview-metrics/MEMORY_AND_RAG_EVAL.md",
    "eval/interview-metrics/runs/enterpriseops-csm-sol-max-preloaded-cost-optimization-20260904.v1.json",
    "eval/interview-metrics/runs/cloudops-sol-max-alert-first-cost-optimization-20260904.r7.json",
    "eval/interview-metrics/runs/memory-maintenance-sol-max-concise-contract-cost-optimization-20260904.r3.json",
    "eval/interview-metrics/runs/agent-lab-optimal-path-enterpriseops-luna-prompt-20260904.v1.json",
    "eval/interview-metrics/runs/agent-lab-optimal-path-cloudops-luna-prompt-20260904.v1.json",
    "eval/interview-metrics/runs/agent-lab-cost-enterpriseops-sol-max-preloaded-current-runtime-20260904.r8.v1.json",
    "eval/interview-metrics/runs/agent-lab-cost-enterpriseops-luna-max-preloaded-model-only-20260904.r5.v1.json",
    "eval/interview-metrics/runs/agent-lab-cost-enterpriseops-luna-max-explicit-enum-prompt-20260904.r7.v1.json",
    "eval/interview-metrics/runs/agent-lab-cost-cloudops-sol-max-alert-first-20260904.r7.v1.json",
    "eval/interview-metrics/runs/agent-lab-cost-cloudops-luna-max-alert-first-model-only-20260904.r1.v1.json",
    "eval/interview-metrics/runs/agent-lab-cost-cloudops-luna-max-owner-mechanism-prompt-20260904.r5.v1.json",
    "eval/interview-metrics/runs/agent-lab-cost-memory-sol-max-model-baseline-20260904.r4.v1.json",
    "eval/interview-metrics/runs/agent-lab-cost-memory-luna-max-model-only-20260904.r1.v1.json",
    "eval/interview-metrics/runs/memory-maintenance-sol-to-luna-model-only-optimization-20260904.r1.json",
    "eval/interview-metrics/runs/enterprise-rag-answer-evidence-standard-v2-validation-calibration-20260904.v1.json",
    "eval/interview-metrics/runs/enterprise-rag-answer-evidence-luna-max-standard-v2-offline-rescore-20260904.v1.json",
    "eval/interview-metrics/runs/enterprise-rag-answer-evidence-three-stage-preflight-20260904.v2.json",
    "eval/interview-metrics/runs/enterprise-rag-answer-evidence-three-stage-validation-20260904.v1.json",
    "eval/interview-metrics/runs/enterprise-rag-answer-evidence-standard-candidate-aware-attention-r6-calibration-20260905.v1.json",
    "eval/interview-metrics/runs/enterprise-rag-answer-evidence-sol-max-frozen-v19-r4-attention-r6-exact-offline-rescore-20260905.v1.json",
    "eval/interview-metrics/runs/enterprise-rag-answer-evidence-luna-max-model-only-v19-r4-attention-r6-exact-offline-rescore-20260905.v1.json",
    "eval/interview-metrics/runs/enterprise-rag-answer-evidence-luna-max-coverage-balanced-v4-r4-attention-r6-exact-offline-rescore-20260905.v1.json",
    "eval/interview-metrics/runs/agent-lab-cost-enterprise-rag-sol-max-frozen-v19-20260904.r4.v1.json",
    "eval/interview-metrics/runs/agent-lab-cost-enterprise-rag-luna-max-model-only-v19-20260904.r4.v1.json",
    "eval/interview-metrics/runs/agent-lab-cost-enterprise-rag-luna-max-coverage-balanced-v4-20260904.r4.v1.json",
)

# The backend/Pi bundles already carry Trace owners, but this small UI slice
# lets a reviewer follow the user-visible report, repair, replay, and contract
# projection without shipping the whole frontend or its build output.
TRACE_UI_FILES = (
    "control-center-web/src/features/trace-agent/trace-agent-model.ts",
    "control-center-web/src/features/trace-agent/trace-agent-model.test.ts",
    "control-center-web/src/features/trace-agent/report-model.ts",
    "control-center-web/src/features/trace-agent/report-document.tsx",
    "control-center-web/src/features/trace-agent/trace-repair.ts",
    "control-center-web/src/features/trace-agent/trace-replay.ts",
    "control-center-web/src/features/trace-agent/html-export.ts",
    "control-center-web/src/features/trace-agent/index.tsx",
    "control-center-web/src/features/trace-agent/failure-reasons.tsx",
    "control-center-web/src/features/trace-agent/failure-reasons.test.tsx",
    "control-center-web/src/features/trace-agent/trace-agent-feature.test.tsx",
    "control-center-web/src/contracts/generated/trace-envelope.v1.ts",
    "control-center-web/src/contracts/generated/trace-diagnostic-report.v1.ts",
    "control-center-web/src/contracts/generated/trace-diagnostic-result.v1.ts",
    "control-center-web/src/contracts/generated/trace-repair-receipt.v1.ts",
    "control-center-web/src/contracts/generated/trace-replay-case.v1.ts",
    "control-center-web/src/contracts/generated/trace-verification-receipt.v1.ts",
)

# Keep the current Agent Lab evidence browser and typed optimization workbench
# directly navigable.  The large backend source bundle remains authoritative;
# this slice exists so an external reviewer does not have to excavate it.
AGENT_LAB_UI_FILES = (
    "control-center-web/src/features/eval-lab/api.ts",
    "control-center-web/src/features/eval-lab/index.tsx",
    "control-center-web/src/features/eval-lab/index.test.tsx",
    "control-center-web/src/features/eval-lab/experiment-audit-html.ts",
    "control-center-web/src/features/eval-lab/experiment-audit-html.test.ts",
    "control-center-web/src/features/eval-lab/optimization/OptimizationWorkbench.tsx",
    "control-center-web/src/features/eval-lab/optimization/OptimizationWorkbench.test.tsx",
    "control-center-web/src/features/eval-lab/optimization/optimization-view-model.ts",
    "control-center-web/src/features/eval-lab/optimization/optimization-workbench.css",
    "control-center-web/src/features/eval-lab/optimization/use-linked-trace-report.ts",
    "control-center-web/src/contracts/generated/agent-lab-cost-request.v1.ts",
    "control-center-web/src/contracts/generated/agent-lab-cost-receipt.v1.ts",
)

TRACE_TEST_RESULTS_MARKDOWN = """# Trace Test Result Receipt

This is source-checkout evidence for the Trace/Trace-Agent paths, captured on
2026-09-05 at the PAW source snapshot recorded in the package manifest. The
package builder records the explicitly rerun commands below; it does not itself
turn them into installed-runtime or foreground acceptance.

## Current focused runs

Eight source-level Trace modules were rerun after the current fixes and
produced **78/78 passed**:

| Module | Result |
| --- | --- |
| `tests.test_trace_repair` | 6 passed |
| `tests.test_trace_replay_verification` | 4 passed |
| `tests.test_trace_repair_http` | 9 passed |
| `tests.test_trace_repair_authority` | 11 passed |
| `tests.test_trace_contracts` | 2 passed |
| `tests.test_trace_diagnostics` | 27 passed |
| `tests.test_trace_store` | 7 passed |
| `tests.test_trace_diagnostic_http` | 12 passed |

## Frontend Trace slice

The focused Trace/failure-reasons Vitest slice recorded **2 files and 45/45
tests passed**. The current full frontend run recorded 258 files and 2875/2875
tests passed.

## Real loopback HTTP boundary

`tests.test_trace_diagnostic_http` exercised real loopback HTTP and passed all
12 cases, including create, finalize, list, exact get, authorization rejection,
and the deprecated-route 404 boundary. This is an ephemeral source test server;
the installed Gateway canary remains a separate acceptance layer.

## Interpretation

The green results prove source-level logic and real loopback HTTP contract
behavior. A `source_local` or `not_installed` candidate still does not prove
that the active managed Runtime changed, that Trace Agent applied a repair, or
that the PAWOS foreground path accepted it. Installation, an installed Gateway
canary, and a foreground Trace/Eval check remain separate requirements.
"""

TUTTI_EXACT_PROMPT_FILES = (
    "packages/agent/daemon/runtime/prompt_content.go",
    "packages/agent/daemon/runtime/prompt_content_test.go",
    "packages/agent/daemon/runtime/tutti_mode_host_context.go",
    "packages/agent/daemon/runtime/tutti_mode_host_context_test.go",
    "packages/agent/daemon/runtime/codex_appserver_event_params.go",
    "packages/agent/daemon/runtime/codex_appserver_side.go",
    "packages/agent/daemon/runtime/codex_appserver_turn.go",
    "packages/agent/daemon/runtime/codex_appserver_adapter.go",
    "packages/agent/daemon/runtime/acp_auto_continue.go",
    "packages/agent/daemon/runtime/tutti_mention_routing.go",
    "packages/agent/daemon/runtime/claude_provider_meta.go",
    "packages/agent/daemon/runtime/claude_sdk_execution.go",
    "packages/agent/daemon/runtime/claude_sdk_compaction_notice.go",
    "packages/agent/daemon/runtime/standard_acp_turn.go",
    "packages/agent/daemon/runtime/acp_provider_cursor.go",
    "packages/agent/host/runtime_operation_plan.go",
    "packages/agent/host/runtime_operation_plan_test.go",
    "packages/agent/claude-sdk-sidecar/src/options.ts",
    "packages/agent/claude-sdk-sidecar/src/options.test.ts",
    "packages/agent/claude-sdk-sidecar/src/compaction.ts",
    "packages/agent/claude-sdk-sidecar/src/compaction.test.ts",
    "packages/agent/claude-sdk-sidecar/src/queryGeneration.ts",
    "packages/agent/claude-sdk-sidecar/src/sessionRuntime.ts",
    "packages/agent/claude-sdk-sidecar/src/sdkMessages.ts",
    "services/tuttid/service/collabrun/service.go",
    "services/tuttid/service/collabrun/service_test.go",
    "services/tuttid/service/workspace/app_factory_prompts.go",
    "services/tuttid/service/workspace/app_factory_prompts_test.go",
    "services/tuttid/service/workspace/issue_sequential_dispatch.go",
    "services/tuttid/service/agent/native_provider_probe.go",
    "services/tuttid/service/agent/external_import_parse.go",
    "services/tuttid/data/workspace/migrations_automation_rules.go",
    "services/tuttid/tutti_mode_plan_feedback_dispatcher.go",
    "services/tuttid/tutti_mode_goal_review_agent_adapter.go",
    "packages/workspace/issue-manager/src/core/runPrompt.ts",
    "apps/desktop/scripts/generate-release-summary.mjs",
)
TUTTI_PROMPT_GLOBS = (
    "packages/agent/runtimeprep/**/*",
    "services/tuttid/service/tuttimodeexecution/**/*",
    "services/tuttid/service/workspace/app_factory_reference/**/*",
)
TUTTI_SCAN_ROOTS = (
    "packages/agent/runtimeprep",
    "packages/agent/daemon/runtime",
    "packages/agent/host",
    "packages/agent/claude-sdk-sidecar/src",
    "services/tuttid",
    "packages/workspace",
    "apps/desktop/scripts",
)

# "DuoAgent" is the handoff vocabulary. Tutti's current source calls the two
# relevant vertical slices Tutti Mode execution and CollaborationRun. Keep the
# selection explicit at its architectural seams, then scan only bounded owner
# roots for the current symbol closure so renamed integration files are not
# silently omitted.
TUTTI_DUOAGENT_EXACT_FILES = (
    "docs/architecture/workspace-workflows.md",
    "docs/architecture/issue-execution.md",
    "docs/architecture/model-access-plans.md",
    "services/tuttid/api/openapi/tuttid.v1.yaml",
    "services/tuttid/api/routes.go",
    "services/tuttid/api/routes_tutti_mode_execution.go",
    "services/tuttid/api/daemon_collab_runs.go",
    "services/tuttid/api/daemon_tutti_mode_activation.go",
    "services/tuttid/api/daemon_tutti_mode_activation_test.go",
    "services/tuttid/api/daemon_tutti_mode_execution.go",
    "services/tuttid/api/daemon_tutti_mode_execution_test.go",
    "services/tuttid/api/daemon_tutti_mode_goal_review.go",
    "services/tuttid/api/daemon_tutti_mode_goal_review_contract_test.go",
    "services/tuttid/data/workspace/migrations.go",
    "services/tuttid/data/workspace/store.go",
    "services/tuttid/service/agent/collab_timeline.go",
    "services/tuttid/service/agent/collab_timeline_test.go",
    "services/tuttid/service/workspace/issue_budget_gate.go",
    "services/tuttid/service/workspace/issue_budget_history.go",
    "services/tuttid/service/workspace/issue_execution_coordinator.go",
    "services/tuttid/service/workspace/issue_execution_ports.go",
    "services/tuttid/service/workspace/issue_run_observer.go",
    "services/tuttid/service/workspace/issue_sequential_dispatch.go",
    "services/tuttid/service/workspace/issue_sequential_dispatch_test.go",
    "services/tuttid/service/workspace/issue_tutti_mode_mutation.go",
    "services/tuttid/agent_collaboration_canceller.go",
    "services/tuttid/tutti_mode_execution_agent_adapter.go",
    "services/tuttid/tutti_mode_execution_agent_adapter_test.go",
    "services/tuttid/tutti_mode_execution_watchdog_wiring.go",
    "services/tuttid/tutti_mode_execution_watchdog_wiring_test.go",
    "services/tuttid/tutti_mode_goal_review_agent_adapter.go",
    "services/tuttid/tutti_mode_goal_review_agent_adapter_test.go",
    "services/tuttid/tutti_mode_plan_feedback_dispatcher.go",
    "services/tuttid/tutti_mode_plan_feedback_dispatcher_test.go",
    "services/tuttid/wiring_daemon_api.go",
    "services/tuttid/wiring_issue_execution_startup.go",
    "packages/agent/activity-core/src/collaboration.types.ts",
    "packages/agent/gui/agentActivityRuntime.tsx",
    "packages/agent/gui/shared/agentConversation/components/AgentCollaborationRow.tsx",
    "packages/agent/gui/shared/agentConversation/components/AgentMessageBlock.tsx",
    "packages/agent/gui/shared/agentConversation/contracts/agentCollaborationVM.ts",
    "packages/agent/gui/shared/agentConversation/projection/agentCollaborationProjection.ts",
    "packages/agent/gui/shared/agentConversation/projection/agentCollaborationProjection.spec.ts",
    "packages/clients/tuttid-ts/src/collaborationRunsClient.ts",
    "packages/clients/tuttid-ts/src/workspaceIssueOrchestrationClient.ts",
    "packages/events/protocol/definitions/agent/collaboration.updated.event.json",
    "apps/desktop/src/renderer/src/features/workspace-agent/services/internal/workspaceAgentActivityService.ts",
    "apps/desktop/src/renderer/src/features/workspace-agent/services/internal/workspaceAgentActivityService.test.ts",
)
TUTTI_DUOAGENT_GLOBS = (
    "services/tuttid/biz/collabrun/**/*",
    "services/tuttid/service/collabrun/**/*",
    "services/tuttid/biz/tuttimodeactivation/**/*",
    "services/tuttid/service/tuttimodeactivation/**/*",
    "services/tuttid/biz/tuttimodeexecution/**/*",
    "services/tuttid/service/tuttimodeexecution/**/*",
    "services/tuttid/service/tuttimodeplan/**/*",
    "services/tuttid/service/cli/providers/tuttimodeplan/**/*",
    "services/tuttid/data/workspace/*collab_run*",
    "services/tuttid/data/workspace/*tutti_mode*",
    "services/tuttid/service/eventstream/*collaboration*",
    "services/tuttid/service/eventstream/*tutti_mode*",
    "packages/agent/daemon/runtime/*tutti_mode*",
    "packages/agent/runtimeprep/*tutti_mode*",
    "packages/agent/runtimeprep/skill_templates/tutti-model-allocation*",
    "packages/agent/gui/workspaceWorkflow/tuttiModePlan/**/*",
    "packages/agent/gui/app/renderer/i18n/locales/*agentGuiCollaboration*",
)
TUTTI_DUOAGENT_SCAN_ROOTS = (
    "services/tuttid/api",
    "services/tuttid/data/workspace",
    "services/tuttid/service/agent",
    "services/tuttid/service/eventstream",
    "services/tuttid/service/workspace",
    "services/tuttid",
    "packages/agent",
    "packages/clients/tuttid-ts/src",
    "packages/events/protocol/definitions/agent",
    "apps/desktop/src/renderer/src/features/workspace-agent",
    "apps/desktop/src/renderer/src/features/workspace-workbench",
)
TUTTI_DUOAGENT_SCAN_PATTERNS = (
    re.compile(r"(?i)\bcollabrun|\bCollaborationRun"),
    re.compile(r"(?i)\btutti[_ -]?mode[_ -]?execution|\bTuttiModeExecution"),
    re.compile(r"(?i)\btutti(?:[_ -]?mode)?[_ -]?plan|\bTutti Mode\b"),
    re.compile(r"(?i)\bgoal[_ -]?review|\bGoalReview"),
)

TEXT_SUFFIXES = {
    ".cjs", ".css", ".go", ".html", ".js", ".json", ".md", ".mjs",
    ".mod", ".py", ".sh", ".sql", ".sum", ".toml", ".ts", ".tsx",
    ".txt", ".yaml", ".yml",
}
TEXT_NAMES = {"LICENSE", "NOTICE"}
EXCLUDED_PARTS = {
    ".cache", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "__pycache__", "build", "cache", "coverage", "credentials",
    "dist", "history", "logs", "model-cache", "node_modules", "output",
    "playwright-report", "private", "test-results",
}
EXCLUDED_NAMES = {
    ".DS_Store", ".env", ".env.local", "auth.json", "credentials.json",
    # Transcript-import fixtures are outside this review package's scope.
    "test_codex_history.py",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock",
}
MAX_SOURCE_BYTES = 1_500_000

SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{24,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{32,}={0,2}")),
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"password|authorization)\b[\"']?\s*[:=]\s*)([\"'])([^\"'\n]{4,})([\"'])"
)

# Test fixtures often use paths such as `/Users/alice` or `/Volumes/private`
# even though they are not live data.  Keep those examples useful while
# preventing them from looking like a real machine path in an external review
# package.  Source-root substitutions above run first, so provenance remains
# visible as `[SOURCE_ROOT:...]`.
MACHINE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])/(?:Users|Volumes|private|var/folders|tmp)/"
    r"[^\s\"'`\)\]}>,;]+"
)

PROMPT_SCAN_PATTERNS = (
    ("system-prompt", re.compile(r"(?i)\bsystem[_ -]?prompt\b")),
    ("developer-instructions", re.compile(r"(?i)\bdeveloper[_ -]?instructions\b")),
    ("system-reminder", re.compile(r"<system-reminder>")),
    ("prompt-constant", re.compile(r"\b(?:const|var)\s+[A-Za-z0-9_]*(?:Prompt|Instructions|Reminder)[A-Za-z0-9_]*")),
    ("prompt-field", re.compile(r"\b(?:Prompt|System|Instruction):\s*[\"'`]")),
    ("prompt-builder", re.compile(r"\b(?:func|function)\s+[A-Za-z0-9_]*(?:Prompt|Instructions)[A-Za-z0-9_]*")),
    ("model-role", re.compile(r"(?:\bYou are\b|\bYou have been\b)")),
)

LANGUAGE_BY_SUFFIX = {
    ".cjs": "javascript", ".css": "css", ".go": "go", ".html": "html",
    ".js": "javascript", ".json": "json", ".md": "markdown",
    ".mjs": "javascript", ".mod": "text", ".py": "python", ".sh": "bash",
    ".sql": "sql", ".sum": "text", ".toml": "toml", ".ts": "typescript",
    ".tsx": "tsx", ".txt": "text", ".yaml": "yaml", ".yml": "yaml",
}


@dataclass(frozen=True)
class RepoSnapshot:
    label: str
    head: str
    branch: str
    dirty_count: int


@dataclass
class SourceReceipt:
    repository: str
    category: str
    source_relative: str
    target: str
    git_status: str
    original_bytes: int
    original_sha256: str
    package_bytes: int
    package_sha256: str
    redactions: dict[str, int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tutti-root", type=Path, default=DEFAULT_TUTTI_ROOT)
    parser.add_argument("--pi-root", type=Path, default=DEFAULT_PI_ROOT)
    parser.add_argument("--skills-root", type=Path, default=DEFAULT_SKILLS_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--scope", choices=("backend", "room-jev", "room-jev-fullstack"), default="backend")
    parser.add_argument("--room-pi-source", type=Path, help="Include selected Pi source needed by the Room/Jev integration")
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        command, cwd=cwd, check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    return completed.stdout.strip()


def snapshot(root: Path, label: str) -> RepoSnapshot:
    status = run(["git", "status", "--porcelain=v1", "--untracked-files=all"], root)
    return RepoSnapshot(
        label=label,
        head=run(["git", "rev-parse", "HEAD"], root),
        branch=run(["git", "branch", "--show-current"], root) or "detached",
        dirty_count=len(status.splitlines()) if status else 0,
    )


def git_file_status(root: Path) -> tuple[set[str], dict[str, str]]:
    tracked = set(run(["git", "ls-files"], root).splitlines())
    raw = run(["git", "status", "--porcelain=v1", "--untracked-files=all"], root)
    statuses: dict[str, str] = {}
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        relative = line[3:]
        if " -> " in relative:
            relative = relative.split(" -> ", 1)[1]
        statuses[relative.strip('"')] = f"git-{line[:2].strip() or line[:2].replace(' ', '_')}"
    return tracked, statuses


def status_for(relative: str, tracked: set[str], statuses: dict[str, str]) -> str:
    if relative in statuses:
        return statuses[relative]
    return "tracked-clean" if relative in tracked else "outside-git-index"


def eligible(path: Path, *, root: Path) -> bool:
    if not path.is_file() or path.is_symlink() or path.name in EXCLUDED_NAMES:
        return False
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    # Filter package contents, not host ancestors such as macOS /private/tmp.
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if path.name not in TEXT_NAMES and path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    return path.stat().st_size <= MAX_SOURCE_BYTES


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sanitize(text: str, roots: Iterable[Path]) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    sanitized = text
    for label, pattern in SECRET_PATTERNS:
        sanitized, count = pattern.subn(f"[REDACTED_PACKAGE_COPY:{label}]", sanitized)
        if count:
            counts[label] = counts.get(label, 0) + count

    def replace_assignment(match: re.Match[str]) -> str:
        counts["secret-assignment"] = counts.get("secret-assignment", 0) + 1
        return f"{match.group(1)}{match.group(2)}[REDACTED_PACKAGE_COPY:secret-assignment]{match.group(4)}"

    sanitized = SECRET_ASSIGNMENT.sub(replace_assignment, sanitized)
    replacements = {
        str(Path.home()): "/Users/[REDACTED_USER]",
        str(PAW_ROOT.parent.parent): "/Volumes/[REDACTED_VOLUME]",
    }
    for root in roots:
        replacements[str(root)] = f"[SOURCE_ROOT:{root.name}]"
    for source, replacement in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        count = sanitized.count(source)
        if count:
            sanitized = sanitized.replace(source, replacement)
            counts["machine-path"] = counts.get("machine-path", 0) + count
    sanitized, count = MACHINE_PATH.subn("[REDACTED_PACKAGE_COPY:machine-path]", sanitized)
    if count:
        counts["machine-path"] = counts.get("machine-path", 0) + count
    return sanitized, counts


def add_text_source(
    staging: Path,
    receipts: list[SourceReceipt],
    *,
    repository: str,
    category: str,
    root: Path,
    path: Path,
    target: str,
    git_status: str,
    roots: tuple[Path, ...],
) -> None:
    if not eligible(path, root=root):
        raise ValueError(f"ineligible required source: {path}")
    raw = path.read_bytes()
    if b"\0" in raw:
        raise ValueError(f"NUL byte in selected source: {path}")
    text = raw.decode("utf-8")
    copied, redactions = sanitize(text, roots)
    encoded = copied.encode("utf-8")
    destination = staging / target
    if destination.exists():
        if destination.read_bytes() != encoded:
            raise ValueError(f"package target collision: {target}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoded)
    receipts.append(SourceReceipt(
        repository=repository,
        category=category,
        source_relative=path.relative_to(root).as_posix(),
        target=target,
        git_status=git_status,
        original_bytes=len(raw),
        original_sha256=sha256(raw),
        package_bytes=len(encoded),
        package_sha256=sha256(encoded),
        redactions=redactions,
    ))


def walk_eligible(root: Path) -> list[Path]:
    return sorted((path for path in root.rglob("*") if eligible(path, root=root)), key=lambda path: path.relative_to(root).as_posix())


def collect_tutti_prompt_paths(tutti_root: Path) -> list[Path]:
    selected: set[Path] = set()
    for relative in TUTTI_EXACT_PROMPT_FILES:
        path = tutti_root / relative
        if path.is_file() and eligible(path, root=tutti_root):
            selected.add(path)
    for pattern in TUTTI_PROMPT_GLOBS:
        selected.update(path for path in tutti_root.glob(pattern) if eligible(path, root=tutti_root))
    for relative_root in TUTTI_SCAN_ROOTS:
        scan_root = tutti_root / relative_root
        if not scan_root.is_dir():
            continue
        for path in walk_eligible(scan_root):
            relative = path.relative_to(tutti_root).as_posix()
            if "/generated/" in f"/{relative}/" or "/i18n/" in f"/{relative}/" or "/testdata/" in f"/{relative}/":
                continue
            if path.name.endswith(("_test.go", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
                continue
            text = path.read_text(encoding="utf-8")
            if any(pattern.search(text) for _, pattern in PROMPT_SCAN_PATTERNS):
                selected.add(path)
    return sorted(selected, key=lambda path: path.relative_to(tutti_root).as_posix())


def collect_tutti_duoagent_paths(tutti_root: Path) -> list[Path]:
    selected: set[Path] = set()
    for relative in TUTTI_DUOAGENT_EXACT_FILES:
        path = tutti_root / relative
        if path.is_file() and eligible(path, root=tutti_root):
            selected.add(path)
    for pattern in TUTTI_DUOAGENT_GLOBS:
        selected.update(path for path in tutti_root.glob(pattern) if eligible(path, root=tutti_root))
    for relative_root in TUTTI_DUOAGENT_SCAN_ROOTS:
        scan_root = tutti_root / relative_root
        if not scan_root.is_dir():
            continue
        for path in walk_eligible(scan_root):
            relative = path.relative_to(tutti_root).as_posix()
            if "/generated/" in f"/{relative}/" or "/testdata/" in f"/{relative}/":
                continue
            text = path.read_text(encoding="utf-8")
            if any(pattern.search(text) for pattern in TUTTI_DUOAGENT_SCAN_PATTERNS):
                selected.add(path)
    return sorted(selected, key=lambda path: path.relative_to(tutti_root).as_posix())


def render_prompt_candidates(tutti_root: Path, roots: tuple[Path, ...]) -> str:
    rows = ["path\tline\tclassifiers\tpreview"]
    for relative_root in TUTTI_SCAN_ROOTS:
        scan_root = tutti_root / relative_root
        if not scan_root.is_dir():
            continue
        for path in walk_eligible(scan_root):
            relative = path.relative_to(tutti_root).as_posix()
            if "/generated/" in f"/{relative}/" or "/i18n/" in f"/{relative}/" or "/testdata/" in f"/{relative}/":
                continue
            if path.name.endswith(("_test.go", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                labels = [label for label, pattern in PROMPT_SCAN_PATTERNS if pattern.search(line)]
                if not labels:
                    continue
                preview, _ = sanitize(line.strip().replace("\t", " "), roots)
                rows.append(f"{relative}\t{number}\t{','.join(labels)}\t{preview[:600]}")
    return "\n".join(rows) + "\n"


def fence_for(text: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_tutti_extraction(staging: Path, receipts: list[SourceReceipt]) -> str:
    prompt_receipts = sorted(
        (receipt for receipt in receipts if receipt.category == "tutti-prompt-source"),
        key=lambda receipt: receipt.source_relative,
    )
    sections = [
        "# Tutti System Prompts — Embedded Source Extraction",
        "",
        "This is the source-authoritative extraction for the Tutti prompt snapshot. "
        "Dynamic final prompts depend on current host facts, capability packs and provider transport; "
        "review the builders together with their templates rather than treating one literal as the final prompt.",
        "",
        f"Selected prompt-related source files: `{len(prompt_receipts)}`.",
        "",
    ]
    for index, receipt in enumerate(prompt_receipts, 1):
        path = staging / receipt.target
        content = path.read_text(encoding="utf-8")
        fence = fence_for(content)
        language = LANGUAGE_BY_SUFFIX.get(Path(receipt.source_relative).suffix.lower(), "text")
        sections.extend((
            f"## PROMPT-SOURCE-{index:04d} — `{receipt.source_relative}`",
            "",
            f"- Git status: `{receipt.git_status}`",
            f"- Original bytes: `{receipt.original_bytes}`",
            f"- Original SHA-256: `{receipt.original_sha256}`",
            f"- Package-copy redactions: `{sum(receipt.redactions.values())}`",
            "",
            f"{fence}{language}",
            content.rstrip("\n"),
            fence,
            "",
        ))
    return "\n".join(sections)


def duoagent_layer(relative: str) -> str:
    if relative.startswith("docs/"):
        return "architecture"
    if "/biz/" in relative:
        return "domain"
    if "/data/" in relative:
        return "persistence"
    if "/api/" in relative or relative.startswith("packages/clients/"):
        return "transport"
    if "/eventstream/" in relative or "collab_timeline" in relative:
        return "events-and-timeline"
    if relative.startswith("packages/agent/gui/") or relative.startswith("apps/desktop/"):
        return "frontend-projection"
    if "/runtimeprep/" in relative or "/daemon/runtime/" in relative:
        return "runtime-context"
    if "/service/" in relative or relative.startswith("services/tuttid/"):
        return "orchestration-and-adapters"
    return "supporting-contract"


def render_duoagent_index(receipts: list[SourceReceipt]) -> str:
    selected = sorted(
        (receipt for receipt in receipts if receipt.category == "tutti-duoagent-reference"),
        key=lambda receipt: receipt.source_relative,
    )
    layer_counts: dict[str, int] = {}
    rows: list[str] = []
    for receipt in selected:
        layer = duoagent_layer(receipt.source_relative)
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
        test_kind = "test" if receipt.source_relative.endswith(
            ("_test.go", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
        ) else "source"
        rows.append(
            f"| `{layer}` | `{test_kind}` | [`{receipt.source_relative}`](source/{receipt.source_relative}) "
            f"| `{receipt.git_status}` | {receipt.original_bytes} | `{receipt.original_sha256}` |"
        )
    count_rows = "\n".join(
        f"| `{layer}` | {count} |" for layer, count in sorted(layer_counts.items())
    )
    return f"""# Tutti DuoAgent Reference Source Index

`DuoAgent` is the handoff label. The current Tutti source uses Tutti Mode
execution and CollaborationRun names. This is a bounded reference-source
closure, not a complete Tutti checkout and not proof of PAW migration.

Selected files: `{len(selected)}`.

## Coverage by layer

| Layer | Files |
| --- | ---: |
{count_rows}

## Source receipts

| Layer | Kind | Tutti source path | Git status | Original bytes | Original SHA-256 |
| --- | --- | --- | --- | ---: | --- |
{chr(10).join(rows)}
"""


def copy_generated_text(staging: Path, source: Path, target: str, roots: tuple[Path, ...]) -> None:
    raw = source.read_text(encoding="utf-8")
    copied, _ = sanitize(raw, roots)
    destination = staging / target
    if destination.exists():
        if destination.read_text(encoding="utf-8") != copied:
            raise ValueError(f"generated package target collision: {target}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(copied, encoding="utf-8")


def missing_exact_paths(root: Path, relatives: Iterable[str]) -> list[str]:
    """Return intentionally skipped exact entries instead of hiding drift."""
    return [relative for relative in relatives if not (root / relative).is_file()]


def _trace_payload_summary(path: Path) -> tuple[str, str, str]:
    """Extract a bounded, metadata-only result summary for the review index."""
    if path.suffix.lower() != ".json":
        return "document", "", "contract/status map"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unparseable", "", "JSON source retained; inspect the receipt"
    claim = payload.get("claim")
    claim_status = claim.get("status") if isinstance(claim, dict) else None
    status = str(
        payload.get("status")
        or payload.get("decision")
        or payload.get("purpose")
        or claim_status
        or "metadata"
    )
    run_id = str(
        payload.get("runId")
        or payload.get("suiteId")
        or payload.get("searchId")
        or payload.get("schemaVersion")
        or ""
    )
    metrics = payload.get("result") or payload.get("metrics")
    if isinstance(metrics, dict):
        preferred = (
            "taskSuccessRate", "taskSuccessCount", "taskCount", "AnswerCoverage",
            "CA", "FA", "JRA", "Top3JRA", "focusedTestsPassed",
            "focusedTestsFailed", "persistedValidReports", "contractRejectedReports",
        )
        pairs = [f"{key}={metrics[key]}" for key in preferred if key in metrics]
        summary = ", ".join(pairs) or ", ".join(
            f"{key}={value}" for key, value in list(metrics.items())[:4]
        )
    elif isinstance(claim, dict) and claim.get("summary"):
        summary = str(claim["summary"])
    elif isinstance(payload.get("agenticResult"), dict):
        agentic = payload["agenticResult"]
        rescore = payload.get("rescoreDecision")
        rescored_decision = (
            rescore.get("rescoredCandidateDecision")
            if isinstance(rescore, dict)
            else agentic.get("decision")
        )
        summary = (
            f"rescoredDecision={rescored_decision}, "
            f"Judge={agentic.get('answerJudgeCorrectnessRate')}, "
            f"exactCitationFacts={agentic.get('exactCitationFactsCovered')}/"
            f"{agentic.get('citationFactCount')}, "
            f"abstention={agentic.get('infoNotFoundAbstentionRecall')}, "
            f"protocol={agentic.get('outputProtocolRate')}, "
            f"Tool={agentic.get('toolSuccessRate')}"
        )
    elif isinstance(payload.get("estimate"), dict):
        estimate = payload["estimate"]
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        summary = (
            f"estimatedTotalUsd={estimate.get('totalCostUsd')}, "
            f"uncachedInputTokens={usage.get('uncachedInputTokens')}, "
            f"cachedInputTokens={usage.get('cachedInputTokens')}, "
            f"outputTokens={usage.get('outputTokens')}, providerBill=false"
        )
    elif isinstance(payload.get("candidate"), dict):
        candidate = payload["candidate"]
        before = candidate.get("focusedPiTestsBefore")
        after = candidate.get("focusedPiTestsAfter")
        summary = f"focused-before={before!r}, focused-after={after!r}"
    elif isinstance(payload.get("cases"), list):
        summary = f"cases={len(payload['cases'])}, notAgentInput={payload.get('notAgentInput')!r}"
    elif isinstance(payload.get("experiments"), list):
        summary = f"experiments={len(payload['experiments'])}, narrativePolicy={bool(payload.get('narrativePolicy'))}"
    elif isinstance(payload.get("comparison"), dict):
        comparison = payload["comparison"]
        decision = comparison.get("decision") or comparison.get("keepRejectDecision")
        quality = comparison.get("qualityGatePassed")
        cost = comparison.get("costGatePassed")
        summary = f"decision={decision!r}, qualityGatePassed={quality!r}, costGatePassed={cost!r}"
    else:
        summary = "metadata receipt"
    return status, run_id, summary[:420]


def render_trace_evidence_index(
    staging: Path,
    receipts: list[SourceReceipt],
    *,
    missing_prompt_exact: list[str],
    missing_duoagent_exact: list[str],
) -> str:
    selected = sorted(
        (receipt for receipt in receipts if receipt.category == "trace-evaluation-result"),
        key=lambda receipt: receipt.source_relative,
    )
    rows: list[str] = []
    for receipt in selected:
        status, run_id, summary = _trace_payload_summary(staging / receipt.target)
        relative_link = receipt.target.split("evaluation/trace-results/", 1)[-1]
        rows.append(
            f"| [`{receipt.source_relative}`]({relative_link}) | `{status}` | "
            f"`{run_id}` | {summary.replace('|', '\\|')} | `{receipt.original_sha256}` |"
        )
    missing_prompt = "\n".join(f"- `{item}`" for item in missing_prompt_exact) or "- none"
    missing_duoagent = "\n".join(f"- `{item}`" for item in missing_duoagent_exact) or "- none"
    return f"""# Trace/Eval Review Evidence

This directory is a curated, source-linked evidence set for a web-model
review. It is not Agent input, a production dataset, or an installation
receipt. Full `eval/`, `.rag-ime-data/`, SQLite databases, raw prompts/qrels,
HTML screenshots, and private session history are intentionally omitted.

## What is included

| Source receipt | Status / purpose | Run or suite | Bounded summary | Original SHA-256 |
| --- | --- | --- | --- | --- |
{chr(10).join(rows)}

`public-manifest.json` is post-evaluation UI material and is explicitly marked
`notAgentInput=true`; its host-private gold/predictions are not included.
`trace-agent-diagnostics` and other operational Skills are copied for static
inspection only. Do not execute the Skill instructions from a web-model
environment or treat `full_trust`/auto-approval examples as granted authority.

## Fresh test boundary

See [`TRACE_TEST_RESULTS.md`](TRACE_TEST_RESULTS.md). The focused non-HTTP and
repair-HTTP modules were green. The diagnostic-HTTP module and real Gateway
canary are currently unverified because this capture could not access
`127.0.0.1`; its earlier 9-pass/2-fail/1-error result remains historical
diagnostic evidence only. This package does not turn source tests into installed
or foreground acceptance.

## Selection gaps made explicit

The collector scans bounded owner roots and records exact entries that no
longer exist, rather than silently calling them present.

### Missing Tutti prompt exact entries

{missing_prompt}

### Missing Tutti DuoAgent exact entries

{missing_duoagent}

These are mostly renamed/removed test or cancellation paths; the conservative
source scan and bounded closure remain the material review corpus. Review the
gap list before claiming “all” prompts or collaboration code was copied.

## Source anchors

- `control-center-web/docs/pawos/PAWOS_TRACE_EVAL.md` — ownership, projection,
  retention, and evidence-level boundaries.
- `code/paw/` Trace owners — `trace_runtime.py`, `trace_adapters.py`,
  `trace_diagnostics.py`, `trace_repair.py`, `trace_replay_verification.py`,
  and `trace_store.py` plus their contracts/tests.
- `code/pawos-trace/` — selected report, repair, replay, and UI projection
  source; source code is not a foreground proof.
"""


def _packaged_agent_lab_payload(
    staging: Path,
    receipts: list[SourceReceipt],
    source_relative: str,
) -> tuple[dict[str, object], str]:
    receipt = next(
        (
            item
            for item in receipts
            if item.category == "agent-lab-evaluation-result"
            and item.source_relative == source_relative
        ),
        None,
    )
    if receipt is None:
        raise ValueError(f"Agent Lab evidence receipt missing: {source_relative}")
    payload = json.loads((staging / receipt.target).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Agent Lab evidence must be a JSON object: {source_relative}")
    return payload, receipt.target.split("evaluation/agent-lab-results/", 1)[-1]


def render_agent_lab_evidence_index(
    staging: Path,
    receipts: list[SourceReceipt],
) -> str:
    """Render a source-derived result matrix without upgrading claim scope."""
    enterpriseops, enterpriseops_link = _packaged_agent_lab_payload(
        staging,
        receipts,
        "eval/interview-metrics/runs/agent-lab-optimal-path-enterpriseops-luna-prompt-20260904.v1.json",
    )
    cloudops, cloudops_link = _packaged_agent_lab_payload(
        staging,
        receipts,
        "eval/interview-metrics/runs/agent-lab-optimal-path-cloudops-luna-prompt-20260904.v1.json",
    )
    memory, memory_link = _packaged_agent_lab_payload(
        staging,
        receipts,
        "eval/interview-metrics/runs/memory-maintenance-sol-to-luna-model-only-optimization-20260904.r1.json",
    )
    rag_sol, _rag_sol_link = _packaged_agent_lab_payload(
        staging,
        receipts,
        "eval/interview-metrics/runs/enterprise-rag-answer-evidence-sol-max-frozen-v19-r4-attention-r6-exact-offline-rescore-20260905.v1.json",
    )
    rag_luna, _rag_luna_link = _packaged_agent_lab_payload(
        staging,
        receipts,
        "eval/interview-metrics/runs/enterprise-rag-answer-evidence-luna-max-model-only-v19-r4-attention-r6-exact-offline-rescore-20260905.v1.json",
    )
    rag_prompt, rag_link = _packaged_agent_lab_payload(
        staging,
        receipts,
        "eval/interview-metrics/runs/enterprise-rag-answer-evidence-luna-max-coverage-balanced-v4-r4-attention-r6-exact-offline-rescore-20260905.v1.json",
    )
    rag_sol_cost, _rag_sol_cost_link = _packaged_agent_lab_payload(
        staging,
        receipts,
        "eval/interview-metrics/runs/agent-lab-cost-enterprise-rag-sol-max-frozen-v19-20260904.r4.v1.json",
    )
    rag_luna_cost, _rag_luna_cost_link = _packaged_agent_lab_payload(
        staging,
        receipts,
        "eval/interview-metrics/runs/agent-lab-cost-enterprise-rag-luna-max-model-only-v19-20260904.r4.v1.json",
    )
    rag_prompt_cost, _rag_prompt_cost_link = _packaged_agent_lab_payload(
        staging,
        receipts,
        "eval/interview-metrics/runs/agent-lab-cost-enterprise-rag-luna-max-coverage-balanced-v4-20260904.r4.v1.json",
    )

    def selected_candidate(payload: dict[str, object]) -> dict[str, object]:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("optimal-path receipt has no candidates")
        selected = [
            item
            for item in candidates
            if isinstance(item, dict) and item.get("status") == "eligible"
        ]
        if not selected:
            raise ValueError("optimal-path receipt has no eligible candidate")
        return selected[-1]

    def metric(payload: dict[str, object], key: str) -> float:
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict) or not isinstance(metrics.get(key), (int, float)):
            raise ValueError(f"missing numeric Agent Lab metric: {key}")
        return float(metrics[key])

    def cost_text(before: float, after: float) -> str:
        decrease = (1.0 - after / before) * 100.0
        multiple = before / after
        return f"${before:.8g} -> ${after:.8g}; -{decrease:.4f}% ({multiple:.4f}x)"

    enterpriseops_baseline = enterpriseops.get("baseline")
    cloudops_baseline = cloudops.get("baseline")
    if not isinstance(enterpriseops_baseline, dict) or not isinstance(cloudops_baseline, dict):
        raise ValueError("optimal-path receipt has no baseline")
    enterpriseops_candidate = selected_candidate(enterpriseops)
    cloudops_candidate = selected_candidate(cloudops)

    memory_comparison = memory.get("comparison")
    if not isinstance(memory_comparison, dict):
        raise ValueError("Memory model comparison missing")
    memory_cost = memory_comparison.get("cost")
    if not isinstance(memory_cost, dict):
        raise ValueError("Memory cost comparison missing")
    memory_before = float(memory_cost["baselineTotalUsd"])
    memory_after = float(memory_cost["candidateTotalUsd"])

    def rag_agentic(payload: dict[str, object]) -> dict[str, object]:
        result = payload.get("agenticResult")
        if not isinstance(result, dict):
            raise ValueError("RAG exact rescore has no agenticResult")
        return result

    def estimated_cost(payload: dict[str, object]) -> float:
        estimate = payload.get("estimate")
        if not isinstance(estimate, dict):
            raise ValueError("Agent Lab cost receipt has no estimate")
        return float(estimate["totalCostUsd"])

    rag_sol_result = rag_agentic(rag_sol)
    rag_luna_result = rag_agentic(rag_luna)
    rag_prompt_result = rag_agentic(rag_prompt)
    rag_quality = (
        "Judge "
        f"{float(rag_sol_result['answerJudgeCorrectnessRate']):g} -> "
        f"{float(rag_luna_result['answerJudgeCorrectnessRate']):g} -> "
        f"{float(rag_prompt_result['answerJudgeCorrectnessRate']):g}; "
        "exact citation facts "
        f"{int(rag_sol_result['exactCitationFactsCovered'])}/{int(rag_sol_result['citationFactCount'])} -> "
        f"{int(rag_luna_result['exactCitationFactsCovered'])}/{int(rag_luna_result['citationFactCount'])} -> "
        f"{int(rag_prompt_result['exactCitationFactsCovered'])}/{int(rag_prompt_result['citationFactCount'])}"
    )

    selected = sorted(
        (
            receipt
            for receipt in receipts
            if receipt.category == "agent-lab-evaluation-result"
        ),
        key=lambda receipt: receipt.source_relative,
    )
    receipt_rows: list[str] = []
    for receipt in selected:
        packaged = staging / receipt.target
        status, run_id, summary = _trace_payload_summary(packaged)
        relative_link = receipt.target.split("evaluation/agent-lab-results/", 1)[-1]
        receipt_rows.append(
            f"| [`{receipt.source_relative}`]({relative_link}) | `{status}` | "
            f"`{run_id}` | {summary.replace('|', '\\|')} | `{receipt.original_sha256}` |"
        )

    return f"""# Agent Lab Evaluation and Optimization Evidence

This directory is the public, privacy-safe evidence set for reviewing what was
tested, what changed, why it changed, and what the result can honestly prove.
It is not a Provider bill, Held-out result, installed-app receipt, or production
claim. Quality gates are evaluated before cost; elapsed time is diagnostic and
is not a Keep gate in the current experiments.

## Current result matrix

| Project | Causal path | Quality result | Estimated cost evidence | Decision |
| --- | --- | --- | --- | --- |
| EnterpriseOps | Sol baseline -> Luna model-only Reject -> Luna + general Prompt | 3/3 tasks, 31/31 host verifiers, 0 Tool failures | {cost_text(metric(enterpriseops_baseline, 'apiCostUsd'), metric(enterpriseops_candidate, 'apiCostUsd'))} | [Keep]({enterpriseops_link}) |
| CloudOps | Sol baseline -> Luna model-only Reject -> Luna + owner/mechanism Prompt | CA 12/12, Top3JRA 12/12, FA/JRA 11/12, 0 Tool failures | {cost_text(metric(cloudops_baseline, 'apiCostUsd'), metric(cloudops_candidate, 'apiCostUsd'))} | [Keep]({cloudops_link}) |
| Memory | Sol baseline -> Luna model-only | 5/5 curation, 4/4 durable recall, 1/1 abstention, rollback/replay pass | {cost_text(memory_before, memory_after)} | [Keep]({memory_link}) |
| Enterprise RAG | Sol baseline -> Luna model-only Reject -> Luna + Prompt | {rag_quality}; abstention/protocol/Tool 100% | {cost_text(estimated_cost(rag_sol_cost), estimated_cost(rag_prompt_cost))} | [Validation-quality Keep]({rag_link}) |

All four rows are source-local Validation evidence and do not carry repeated-run
confidence intervals. EnterpriseOps, CloudOps, and Enterprise RAG prove why
"just change to a cheaper model" was insufficient: their model-only Luna runs
failed quality gates, while a second run changed only a general Prompt contract
and recovered quality. Memory did not need Prompt adaptation.

Enterprise RAG's current Standard is post-Validation and candidate-aware. The
exact rescores reuse frozen outputs: no candidate, Provider, or Judge was rerun,
and the Standard revision is not a model improvement. Held-out remains unopened,
so the current Keep is a Validation-quality result and cannot support an
unbiased promotion or production-generalization claim. Within the same Luna
model route, the general Prompt adaptation changed the estimate from
{cost_text(estimated_cost(rag_luna_cost), estimated_cost(rag_prompt_cost))}.

All cost numbers are deterministic estimates or Runtime-reconciled estimates
from reported usage and a versioned pricing source. `providerBillAvailable` is
false, so use "estimated API cost" in a resume or interview, not "actual bill".
Memory uses a bound report-usage estimate; the other three projects use
Runtime-reconciled estimates. These are different evidence levels.

## Remaining product work

The current RAG optimization view has aggregate metrics and before/after factor
descriptions, but lacks a recorded patch Diff, a linked baseline Trace diagnosis,
and public per-Case before/after bodies. Factor names are not a patch Diff. The
typed workbench is a read-only evidence projection, not an implemented generic
counterfactual-probe or repair-operator engine. Review these gaps explicitly;
do not infer their completion from the Keep decision or the included UI source.

## How to inspect the evidence

- `agent-experiments.v1.json` records task, dataset, Standard, frozen controls,
  before/after factors, reasons, effects, limitations, and STAR-ready claims.
- `agent-lab-optimal-path-*.json` shows the full Baseline -> model-only ->
  Prompt-adapted path and preserves rejected candidates.
- `agent-lab-cost-*.json` exposes tokens, rates, source identity, formula result,
  and the explicit no-Provider-bill boundary.
- `enterprise-rag-answer-evidence-standard.validation-candidate-aware-attention-r6.json`
  plus calibration and exact offline rescore receipts demonstrate Judge/Gold
  evolution without relabeling it as a model improvement.
- The top-level Trace index and source bundles expose Tool, Prompt, Skill,
  Workflow, Runtime, repair, replay, and frontend projection owners.

## Included source receipts

| Source receipt | Status / purpose | Run or suite | Bounded summary | Original SHA-256 |
| --- | --- | --- | --- | --- |
{chr(10).join(receipt_rows)}
"""


def render_prompt_skill_inventory(
    private_skills: list[str],
    current_skills: list[str],
    prompt_count: int,
    duoagent_count: int,
    missing_prompt_exact: list[str],
    missing_duoagent_exact: list[str],
    high_privilege_paths: list[str],
) -> str:
    def bullets(items: Iterable[str]) -> str:
        values = list(items)
        return "\n".join(f"- `{item}`" for item in values) or "- none"

    return f"""# Prompt, Tool, and Skill Inventory (current package snapshot)

This index is the package-local authority for counts. The older handoff
inventory is retained as historical context and may contain a previous count.

## Counts

- Private Pi Skill package: **{len(private_skills)}** directories.
- Current PAW Skill checkout: **{len(current_skills)}** directories.
- Tutti prompt-related source selection: **{prompt_count}** files.
- Tutti DuoAgent/Tutti Mode reference closure: **{duoagent_count}** files.

## Current PAW Skills

{bullets(current_skills)}

## Private Pi Skills

{bullets(private_skills)}

## High-privilege static-review warning

The following selected Skill files contain full-trust, root-workspace, or
automatic-approval language. They are source material for critique only:

{bullets(high_privilege_paths)}

## Exact-selection gaps

Prompt entries no longer present:

{bullets(missing_prompt_exact)}

DuoAgent entries no longer present:

{bullets(missing_duoagent_exact)}

The collector's conservative scan still captures current owner files matching
the prompt/collaboration patterns, but these gaps must remain visible in any
review conclusion.
"""


def skill_names(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith("."))


def write_entrypoint(
    staging: Path,
    snapshots: list[RepoSnapshot],
    receipts: list[SourceReceipt],
    private_skills: list[str],
    current_skills: list[str],
) -> None:
    category_counts: dict[str, int] = {}
    for receipt in receipts:
        category_counts[receipt.category] = category_counts.get(receipt.category, 0) + 1
    snapshot_rows = "\n".join(
        f"| {item.label} | `{item.head}` | `{item.branch}` | {item.dirty_count} |"
        for item in snapshots
    )
    category_rows = "\n".join(
        f"| `{category}` | {count} |" for category, count in sorted(category_counts.items())
    )
    dirty_sources = [item.label for item in snapshots if item.dirty_count]
    clean_sources = [item.label for item in snapshots if not item.dirty_count]
    if dirty_sources:
        source_state = (
            "Dirty source snapshots are intentional: `" + "`, `".join(dirty_sources)
            + "` had uncommitted entries when this package was built."
        )
    else:
        source_state = "All source snapshots were clean when this package was built."
    if clean_sources:
        source_state += " Clean snapshots: `" + "`, `".join(clean_sources) + "`."
    content = f"""# PAW Backend Web-Model Review Workspace

This ZIP contains the current PAW backend, the selected authoritative Pi Runtime
source, the Tutti system/developer prompt reference corpus, the bounded Tutti
DuoAgent/Tutti Mode implementation reference, PAW-owned Skills, interfaces,
production-shaped privacy-safe fixtures, licenses and a per-file manifest.

Start with `guides/PAW_BACKEND_WEB_MODEL_GUIDE.md`.

## Responsibility warning

- Pi is the sole Agent Runtime.
- PAW owns product backend capabilities and adapters.
- Tutti prompts and DuoAgent/Tutti Mode code are reference source only and must
  not become a production dependency or proof that the behavior was migrated.
- Skills are conditional methods, not Runtime state or authority.

## Source snapshots

| Repository | HEAD | Branch | Dirty entries |
| --- | --- | --- | ---: |
{snapshot_rows}

{source_state} The package captures current source without resetting, stashing,
committing or pushing any source worktree.

## Coverage

| Category | Source files copied |
| --- | ---: |
{category_rows}

- Private Pi Skill package: `{len(private_skills)}` Skills.
- Current PAW Skill checkout: `{len(current_skills)}` Skills.
- Tutti extraction includes dynamic builders, templates, provider transports,
  recovery/compaction paths and specialized synthetic prompts.
- Tutti DuoAgent reference includes the current Tutti Mode execution and Goal
  Review path plus the adjacent CollaborationRun vertical slice. Tutti has no
  literal `DuoAgent` source type in this snapshot.
- Room/Session examples are production-shaped fixtures, not private user transcripts.

## Navigation

- `guides/` — vision, review brief, Tutti prompt/DuoAgent maps and PAW Skill map.
- `authorities/` — controlling PAW project and architecture documents.
- `code/paw/` — navigable current PAW backend source/contracts/tests.
- `code/pi/` — navigable selected Pi core/Runtime Host source and tests.
- `single-file-bundles/` — searchable embedded-source backend and Pi bundles.
- `tutti-prompts/` — extracted prompt corpus, conservative scan and source tree.
- `tutti-duoagent/` — bounded Tutti Mode/CollaborationRun reference source and index.
- `skills/` — private {len(private_skills)}-Skill package and current
  {len(current_skills)}-Skill PAW checkout.
- `interfaces-and-fixtures/` — route/function guide and safe real-shape data.
- `evaluation/trace-results/` — curated Trace/Eval contracts, result receipts,
  closed-loop manifest, and the fresh test-result boundary (not Agent input).
- `evaluation/agent-lab-results/` — public Task/Dataset/Standard, causal
  optimization paths, before/after receipts, cost calculations, and the current
  four-project Validation-quality result boundary.
- `code/pawos-trace/` — selected Trace report/repair/replay UI and generated
  contract source.
- `code/pawos-agent-lab/` — current evidence browser, typed Optimization
  Workbench, exact Trace drill-down, and executable frontend tests.
- `licenses/` — PAW notices and upstream license texts.
- `manifest/` — provenance, exclusions, per-file hashes and package checksums.

## Explicit exclusions

API keys, credentials, account state, user configuration, SQLite databases,
chat histories, logs, model weights, dependency directories, caches, compiled
Python, generated binaries and build output are absent. Package-copy redactions
never modify a source worktree.

The `trace-agent-diagnostics` Skill and several browser/maintenance Skills
contain high-privilege or auto-approval examples. They are included for static
review only; a web model must not execute them or treat their permissions as
granted authority.
"""
    (staging / "00_READ_ME_FIRST_BACKEND.md").write_text(content, encoding="utf-8")


def validate_package(staging: Path, receipts: list[SourceReceipt]) -> dict[str, object]:
    if len({receipt.target for receipt in receipts}) != len(receipts):
        raise ValueError("duplicate source targets in receipt ledger")
    required_paths = (
        "guides/TUTTI_DUOAGENT_REFERENCE.md",
        "tutti-duoagent/SOURCE_INDEX.md",
        "tutti-duoagent/source/docs/architecture/workspace-workflows.md",
        "tutti-duoagent/source/services/tuttid/service/tuttimodeexecution/service.go",
        "tutti-duoagent/source/services/tuttid/service/tuttimodeexecution/worker_test.go",
        "tutti-duoagent/source/services/tuttid/service/collabrun/service.go",
        "tutti-duoagent/source/packages/agent/gui/shared/agentConversation/components/AgentCollaborationRow.tsx",
        "evaluation/trace-results/control-center-web/docs/pawos/PAWOS_TRACE_EVAL.md",
        "evaluation/trace-results/eval/trace-agent/closed-loop-v1/public-manifest.json",
        "evaluation/trace-results/eval/interview-metrics/runs/trace-agent-provider-bootstrap-20260901.v1.json",
        "evaluation/trace-results/TRACE_TEST_RESULTS.md",
        "evaluation/agent-lab-results/README.md",
        "evaluation/agent-lab-results/eval/interview-metrics/agent-experiments.v1.json",
        "evaluation/agent-lab-results/eval/interview-metrics/runs/agent-lab-optimal-path-enterpriseops-luna-prompt-20260904.v1.json",
        "evaluation/agent-lab-results/eval/interview-metrics/runs/agent-lab-optimal-path-cloudops-luna-prompt-20260904.v1.json",
        "evaluation/agent-lab-results/eval/interview-metrics/runs/memory-maintenance-sol-to-luna-model-only-optimization-20260904.r1.json",
        "evaluation/agent-lab-results/eval/interview-metrics/runs/enterprise-rag-answer-evidence-luna-max-coverage-balanced-v4-r4-attention-r6-exact-offline-rescore-20260905.v1.json",
        "code/pawos-agent-lab/control-center-web/src/features/eval-lab/optimization/OptimizationWorkbench.tsx",
    )
    missing = [relative for relative in required_paths if not (staging / relative).is_file()]
    if missing:
        raise ValueError(f"required review-package paths missing: {missing}")
    duoagent_receipts = [
        receipt for receipt in receipts if receipt.category == "tutti-duoagent-reference"
    ]
    if not duoagent_receipts:
        raise ValueError("Tutti DuoAgent reference source selection is empty")
    duoagent_test_count = sum(
        receipt.source_relative.endswith(
            ("_test.go", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
        )
        for receipt in duoagent_receipts
    )
    if duoagent_test_count == 0:
        raise ValueError("Tutti DuoAgent reference contains no executable tests")
    secret_hits: list[str] = []
    local_path_hits: list[str] = []
    for path in sorted(staging.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            raise ValueError(f"NUL byte in package text: {path.relative_to(staging)}")
        text = data.decode("utf-8")
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                secret_hits.append(f"{path.relative_to(staging)}:{label}")
        if str(Path.home()) in text or str(PAW_ROOT.parent.parent) in text:
            local_path_hits.append(path.relative_to(staging).as_posix())
        if MACHINE_PATH.search(text):
            local_path_hits.append(path.relative_to(staging).as_posix())
    if secret_hits:
        raise ValueError(f"credential-like values remain: {secret_hits[:10]}")
    if local_path_hits:
        raise ValueError(f"machine-local paths remain: {sorted(set(local_path_hits))[:10]}")
    public_manifest = staging / "evaluation/trace-results/eval/trace-agent/closed-loop-v1/public-manifest.json"
    if public_manifest.is_file():
        payload = json.loads(public_manifest.read_text(encoding="utf-8"))
        if payload.get("notAgentInput") is not True:
            raise ValueError("closed-loop public manifest must remain marked notAgentInput")
    return {
        "sourceReceiptCount": len(receipts),
        "sourceOriginalBytes": sum(receipt.original_bytes for receipt in receipts),
        "sourcePackageBytes": sum(receipt.package_bytes for receipt in receipts),
        "sourceRedactions": sum(sum(receipt.redactions.values()) for receipt in receipts),
        "tuttiDuoAgentSourceCount": len(duoagent_receipts),
        "tuttiDuoAgentTestCount": duoagent_test_count,
        "traceEvidenceFileCount": sum(
            receipt.category == "trace-evaluation-result" for receipt in receipts
        ),
        "agentLabEvidenceFileCount": sum(
            receipt.category == "agent-lab-evaluation-result" for receipt in receipts
        ),
        "traceUiSourceFileCount": sum(
            receipt.category == "pawos-trace-ui-source" for receipt in receipts
        ),
    }


def deterministic_zip(staging: Path, output: Path, package_name: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(staging.rglob("*"), key=lambda item: item.relative_to(staging).as_posix()):
            if not path.is_file():
                continue
            relative = path.relative_to(staging).as_posix()
            info = zipfile.ZipInfo(f"{package_name}/{relative}", date_time=(2026, 8, 23, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100644 & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    temporary.replace(output)


ROOM_JEV_SKILLS = (
    "alignment-and-decision", "implementation-planning", "orchestrate-session",
    "facilitate-room", "independent-review", "organize-work-documents",
    "bootstrap-project-context", "systematic-debugging", "test-driven-implementation",
    "trace-agent-diagnostics",
)
ROOM_JEV_GUIDE = "docs/project/jev-backend-20260926/HANDOFF.md"
ROOM_JEV_FULLSTACK_GUIDE = "docs/project/jev-backend-20260926/FULLSTACK_HANDOFF.md"


def collect_room_jev_paths(root: Path) -> list[Path]:
    """Bounded Room/Jev review slice; never read other repositories or live data."""
    paths: set[Path] = set()
    for directory in ("rag_ime/rooms", "rag_ime/jev_tasks", "rag_ime/pi", "rag_ime/control_api", "rag_ime/db"):
        paths.update((root / directory).rglob("*.py"))
        if directory == "rag_ime/db":
            # All append-only schemas are needed to understand canonical DB
            # evolution; no database contents are included.
            paths.update((root / directory / "migrations").glob("*.sql"))
    for pattern in ("rag_ime/agent_*.py", "rag_ime/jev*.py", "tests/test_*room*.py",
                    "tests/test_jev*.py", "tests/test_pi*runtime*.py",
                    "rag_ime/contracts/json/*room*.json", "rag_ime/contracts/json/*work-item*.json",
                    "integrations/pi/*runtime*contract*.json", "integrations/pi/*room*.json"):
        paths.update(root.glob(pattern))
    for relative in ("AGENTS.md", "README.md", "CONTRIBUTING.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
                     "pyproject.toml", "rag_ime/__init__.py", "rag_ime/debug_server.py",
                     "rag_ime/contracts/json_schema.py", "rag_ime/contracts/__init__.py",
                     "tests/test_agent_service.py", "tests/test_agent_tools.py", "tests/test_agent_routes.py",
                     "tests/test_agent_background_jobs.py",
                     "tests/test_debug_management_api.py", "tests/test_database_migrations.py",
                     "tests/test_pi_exact_turn_cancellation.py", "scripts/canary_jev_room.py",
                     "tests/test_pi_prompt_tool_sync.py", "tests/test_pi_prompt_settings_overlay.py",
                     "scripts/pi_canary_support.py", "integrations/jev/README.md",
                     "integrations/pi/rag-ime-control.ts", "integrations/pi/README.md",
                     "rag_ime/browser_control.py", "rag_ime/paw_browser_runtime.py",
                     "scripts/build_ego_browser_runtime.py", "scripts/build_macos_installer.py",
                     "scripts/install_binary_payload.py", "tests/test_binary_installer.py",
                     "scripts/install_sidecar_launch_agent.sh", "scripts/install_agent_gateway_launch_agent.sh",
                     "tests/test_browser_control.py", "tests/test_paw_browser_runtime.py",
                     "tests/test_ego_browser_runtime_build.py", "integrations/ego-browser/UPSTREAM.md",
                     "integrations/ego-browser/upstream/LICENSE",
                     "scripts/build_paw_backend_web_model_package.py",
                     "tests/test_build_paw_backend_web_model_package.py", ROOM_JEV_GUIDE):
        paths.add(root / relative)
    browser_source = root / "integrations/ego-browser/upstream"
    for package in ("ego-browser", "ego-linux-host"):
        base = browser_source / "package" / package
        for directory in ("src", "bin"):
            paths.update((base / directory).rglob("*"))
        paths.update(base.glob("*.json"))
        paths.update(base.glob("*.md"))
    paths.update((browser_source / "skills/ego-browser").rglob("*"))
    for skill in ROOM_JEV_SKILLS:
        directory = root / "integrations/pi/skills" / skill
        if not (directory / "SKILL.md").is_file():
            raise ValueError(f"missing Room Skill: {skill}")
        paths.update(directory.rglob("*"))
    return sorted((p for p in paths if eligible(p, root=root)), key=lambda p: p.relative_to(root).as_posix())


def collect_room_jev_frontend_paths(root: Path) -> list[Path]:
    """Include the Room/Jev surfaces and their local source imports, not all apps."""
    web = root / "control-center-web"
    paths: set[Path] = set()
    for directory in ("src/features/rooms", "src/features/semantic-workspace",
                      "src/features/conversation-ui", "src/features/composer",
                      "src/platform", "src/contracts", "src/paw-os/styles"):
        paths.update(p for p in (web / directory).rglob("*") if eligible(p, root=root))
    for pattern in ("src/paw-os/apps/PawRoom*", "src/paw-os/apps/room-*",
                    "src/paw-os/apps/paw-room*", "src/paw-os/apps/PawAgent*",
                    "src/paw-os/apps/entries/PawAgent*", "src/app/*control-transport*",
                    "src/app/preview-room*", "src/test/*transport*", "src/test/fixtures/room*",
                    "src/paw-os/apps/PawJev*", "e2e/fixtures/jev-execution.*", "e2e/fixtures/jev-live.*",
                    "tsconfig*.json", "vite*.ts", "vitest*.ts"):
        paths.update(p for p in web.glob(pattern) if eligible(p, root=root))
    for relative in ("package.json", "pnpm-lock.yaml", "README.md", "CLOUD_MODEL.md",
                     "src/paw-os/shell/PawRoomProjectionKeeper.tsx", "src/index.css", "src/test/setup.ts"):
        if eligible(web / relative, root=root):
            paths.add(web / relative)
    # Static imports/reexports, side-effect imports, literal dynamic imports and
    # CSS imports. External dependencies are described by package.json/lockfile.
    imports = re.compile(r"(?:\bfrom\s*|\bimport\s*(?:\(\s*)?|@import\s*)[\"']([^\"']+)[\"']")
    queue = list(paths)
    while queue:
        source = queue.pop()
        if source.suffix not in {".ts", ".tsx", ".js", ".jsx", ".css"}:
            continue
        for spec in imports.findall(source.read_text(encoding="utf-8")):
            if spec.startswith("@/"):
                base = web / "src" / spec[2:]
            elif spec.startswith("."):
                base = source.parent / spec
            else:
                continue
            base = base.resolve()
            if not base.is_relative_to(web.resolve()):
                continue
            candidates = [base] + [Path(str(base) + suffix) for suffix in (".ts", ".tsx", ".js", ".jsx", ".css", ".json")]
            candidates += [base / ("index" + suffix) for suffix in (".ts", ".tsx", ".js", ".jsx")]
            resolved = next((p for p in candidates if eligible(p, root=root)), None)
            if resolved is not None and resolved not in paths:
                paths.add(resolved)
                queue.append(resolved)
    # Include the loading boundary for review without recursively bundling all
    # unrelated apps selected by the top-level lazy router. This remains a
    # source review slice, not an independently buildable frontend checkout.
    for relative in ("index.html", "src/main.tsx", "src/app/App.tsx", "src/app/App-loading.test.tsx"):
        if eligible(web / relative, root=root):
            paths.add(web / relative)
    return sorted(paths, key=lambda p: p.relative_to(root).as_posix())


def build_room_jev(output: Path, *, include_frontend: bool = False, pi_root: Path | None = None) -> dict[str, object]:
    paths = collect_room_jev_paths(PAW_ROOT)
    scope = "room-jev-fullstack" if include_frontend else "room-jev"
    guide = ROOM_JEV_FULLSTACK_GUIDE if include_frontend else ROOM_JEV_GUIDE
    frontend_paths = collect_room_jev_frontend_paths(PAW_ROOT) if include_frontend else []
    if include_frontend:
        paths = [p for p in paths if p.relative_to(PAW_ROOT).as_posix() != ROOM_JEV_GUIDE]
        paths = sorted(set(paths + frontend_paths + [PAW_ROOT / guide]))
    tracked, statuses = git_file_status(PAW_ROOT)
    source_snapshot = snapshot(PAW_ROOT, "PAW Room/Jev working tree")
    pi_sources = []
    pi_snapshot = None
    if pi_root is not None:
        pi_root = pi_root.expanduser().resolve()
        pi_builder = load_module("room_jev_pi_sources", PAW_ROOT / "scripts/build_paw_pi_runtime_model_bundle.py")
        pi_snapshot = snapshot(pi_root, "Pi Room/Jev Runtime source")
        pi_sources = [source for source in pi_builder.collect_sources(
            pi_builder.git_snapshot(PAW_ROOT, "PAW"), pi_builder.git_snapshot(pi_root, "Pi")
        ) if source.root == pi_root and source.repository != "installed-runtime"]
    with tempfile.TemporaryDirectory(prefix="paw-room-jev-review-") as directory:
        staging = Path(directory)
        receipts: list[SourceReceipt] = []
        for path in paths:
            relative = path.relative_to(PAW_ROOT).as_posix()
            if relative == guide:
                target, category = "00_READ_ME_FIRST_ROOM_JEV.md", "handoff"
            elif relative.startswith("control-center-web/"):
                target, category = "code/pawos/" + relative.removeprefix("control-center-web/"), "room-jev-frontend"
            elif relative.startswith("integrations/pi/skills/"):
                target = "skills/" + relative.removeprefix("integrations/pi/skills/")
                category = "current-paw-skill"
            elif relative in {"LICENSE", "THIRD_PARTY_NOTICES.md"}:
                target, category = "licenses/" + relative, "license"
            else:
                target, category = "code/paw/" + relative, "room-jev-source"
            add_text_source(staging, receipts, repository="paw", category=category,
                root=PAW_ROOT, path=path, target=target,
                git_status=status_for(relative, tracked, statuses), roots=(PAW_ROOT,))
        for source in pi_sources:
            add_text_source(staging, receipts, repository="pi", category=source.group,
                root=pi_root, path=source.path, target="code/pi/" + source.relative,
                git_status=source.status, roots=(PAW_ROOT, pi_root))
        if pi_root is not None:
            tracked_pi, statuses_pi = git_file_status(pi_root)
            add_text_source(staging, receipts, repository="pi", category="license",
                root=pi_root, path=pi_root / "LICENSE", target="licenses/PI_MIT.txt",
                git_status=status_for("LICENSE", tracked_pi, statuses_pi), roots=(PAW_ROOT, pi_root))
        if include_frontend:
            # Explicit product assets, not a traversal of user media. Preserve
            # alpha and source bytes; PNGs contain no textual package redaction.
            for filename in ("planet-avatars-v1.png", "planet-bodies-v1.png"):
                relative = "control-center-web/src/features/rooms/assets/" + filename
                raw = (PAW_ROOT / relative).read_bytes()
                if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("Room planet atlas is not a PNG")
                target = "code/pawos/" + relative.removeprefix("control-center-web/")
                destination = staging / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
                receipts.append(SourceReceipt("paw", "room-jev-avatar-asset", relative, target,
                    status_for(relative, tracked, statuses), len(raw), sha256(raw), len(raw), sha256(raw), {}))
        bundle = ["# Room / Jev " + ("前后端" if include_frontend else "后端") + "及 Skills 源码", "",
                  "源码审阅快照。先阅读 00_READ_ME_FIRST_ROOM_JEV.md；非安装包，打包成功不代表运行验收。", ""]
        for receipt in sorted(receipts, key=lambda r: r.target):
            if receipt.category == "room-jev-avatar-asset":
                bundle.extend([f"## {receipt.source_relative}", "",
                    f"PNG asset: `{receipt.target}`; SHA-256: `{receipt.package_sha256}`", ""])
                continue
            content = (staging / receipt.target).read_text(encoding="utf-8")
            fence = fence_for(content)
            bundle.extend([f"## {receipt.source_relative}", "",
                           f"SHA-256 (package): `{receipt.package_sha256}`; status: `{receipt.git_status}`", "",
                           fence + LANGUAGE_BY_SUFFIX.get(Path(receipt.source_relative).suffix, "text"),
                           content.rstrip(), fence, ""])
        bundle_name = "ROOM_JEV_FULLSTACK_AND_SKILLS.md" if include_frontend else "ROOM_JEV_BACKEND_AND_SKILLS.md"
        bundle_path = staging / "single-file-bundles" / bundle_name
        bundle_path.parent.mkdir(parents=True)
        bundle_path.write_text("\n".join(bundle), encoding="utf-8")
        manifest_dir = staging / "manifest"
        manifest_dir.mkdir()
        manifest = {
            "schemaVersion": "paw-room-jev-review/1", "scope": scope, "sourceSnapshot": asdict(source_snapshot),
            "sourceReceiptCount": len(receipts), "skills": list(ROOM_JEV_SKILLS),
            "frontendSourceCount": len(frontend_paths),
            "piSourceSnapshot": asdict(pi_snapshot) if pi_snapshot else None,
            "piSourceCount": len(pi_sources),
            "sourceRedactions": sum(sum(r.redactions.values()) for r in receipts),
            "exclusions": ([] if include_frontend else ["frontend"]) + ([] if pi_root else ["external Pi repository"]) + ["unrelated product modules", "Tutti", "global Skills",
                           "credentials", "machine configuration", "databases", "production conversations", "logs",
                           "weights", "node_modules", "caches", "build output"],
            "evidenceBoundary": "source review only; see handoff for independently recorded checks; no installation or deployment",
            "sourceFiles": [asdict(r) for r in receipts],
        }
        (manifest_dir / "package-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        checksums = [f"{sha256(p.read_bytes())}  {p.relative_to(staging).as_posix()}"
                     for p in sorted(staging.rglob("*")) if p.is_file()]
        (manifest_dir / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
        for receipt in receipts:
            if sha256((staging / receipt.target).read_bytes()) != receipt.package_sha256:
                raise ValueError("source receipt mismatch")
        deterministic_zip(staging, output, output.stem)
    return {"ok": True, "scope": scope, "frontendSourceCount": len(frontend_paths), "output": str(output), "outputBytes": output.stat().st_size,
            "outputSha256": sha256(output.read_bytes()), "sourceReceiptCount": len(receipts),
            "currentPawSkillCount": len(ROOM_JEV_SKILLS), "sourceRedactions": manifest["sourceRedactions"]}


def build(args: argparse.Namespace, output: Path) -> dict[str, object]:
    if getattr(args, "scope", "backend") in {"room-jev", "room-jev-fullstack"}:
        return build_room_jev(output, include_frontend=args.scope == "room-jev-fullstack", pi_root=getattr(args, "room_pi_source", None))
    tutti_root = args.tutti_root.expanduser().resolve()
    pi_root = args.pi_root.expanduser().resolve()
    skills_root = args.skills_root.expanduser().resolve()
    roots = (PAW_ROOT, tutti_root, pi_root, skills_root)
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(root)

    backend_builder = load_module("paw_backend_bundle_builder", PAW_ROOT / "scripts/build_paw_backend_model_bundle.py")
    pi_builder = load_module("paw_pi_bundle_builder", PAW_ROOT / "scripts/build_paw_pi_runtime_model_bundle.py")
    subprocess.run([sys.executable, str(PAW_ROOT / "scripts/build_paw_backend_model_bundle.py")], cwd=PAW_ROOT, check=True)
    subprocess.run([
        sys.executable, str(PAW_ROOT / "scripts/build_paw_pi_runtime_model_bundle.py"),
        "--pi-worktree", str(pi_root),
    ], cwd=PAW_ROOT, check=True)

    snapshots = [
        snapshot(PAW_ROOT, "PAW product/backend"),
        snapshot(pi_root, "Pi Runtime"),
        snapshot(tutti_root, "Tutti prompt and DuoAgent reference"),
        snapshot(skills_root, "PAW private Pi Skills"),
    ]
    paw_tracked, paw_statuses = git_file_status(PAW_ROOT)
    tutti_tracked, tutti_statuses = git_file_status(tutti_root)
    skills_tracked, skills_statuses = git_file_status(skills_root)

    private_skill_names = skill_names(skills_root / "skills")
    current_skill_names = skill_names(PAW_ROOT / "integrations/pi/skills")
    if len(private_skill_names) != 13:
        raise ValueError(f"expected 13 private Skills, found {len(private_skill_names)}")
    if not current_skill_names:
        raise ValueError("current PAW Skill checkout is empty")
    missing_prompt_exact = missing_exact_paths(tutti_root, TUTTI_EXACT_PROMPT_FILES)
    missing_duoagent_exact = missing_exact_paths(tutti_root, TUTTI_DUOAGENT_EXACT_FILES)
    high_privilege_paths: list[str] = []
    current_skills_root = PAW_ROOT / "integrations/pi/skills"
    for path in walk_eligible(current_skills_root):
        text = path.read_text(encoding="utf-8")
        if re.search(r"(?i)(full[_ -]?trust|auto[- ]?approv|workspaceRoots?\s*:\s*\[\s*[\"']?/\s*[\"']?\s*\])", text):
            high_privilege_paths.append(path.relative_to(current_skills_root).as_posix())

    with tempfile.TemporaryDirectory(prefix="paw-backend-web-model-") as temporary_dir:
        staging = Path(temporary_dir)
        receipts: list[SourceReceipt] = []

        backend_paths, backend_categories = backend_builder.collect_files()
        for path in backend_paths:
            relative = path.relative_to(PAW_ROOT).as_posix()
            add_text_source(
                staging, receipts, repository="paw", category=f"paw-{backend_categories[relative]}",
                root=PAW_ROOT, path=path, target=f"code/paw/{relative}",
                git_status=status_for(relative, paw_tracked, paw_statuses), roots=roots,
            )

        paw_pi_snapshot = pi_builder.git_snapshot(PAW_ROOT, "PAW adapter/product")
        pi_snapshot = pi_builder.git_snapshot(pi_root, "Pi core/fork worktree")
        for source in pi_builder.collect_sources(paw_pi_snapshot, pi_snapshot):
            if source.repository == "installed-runtime":
                continue
            repository = "paw" if source.root == PAW_ROOT else "pi"
            target = f"code/{repository}/{source.relative}"
            add_text_source(
                staging, receipts, repository=repository, category=source.group,
                root=source.root, path=source.path, target=target,
                git_status=source.status, roots=roots,
            )

        tutti_prompt_paths = collect_tutti_prompt_paths(tutti_root)
        for path in tutti_prompt_paths:
            relative = path.relative_to(tutti_root).as_posix()
            add_text_source(
                staging, receipts, repository="tutti", category="tutti-prompt-source",
                root=tutti_root, path=path, target=f"tutti-prompts/source/{relative}",
                git_status=status_for(relative, tutti_tracked, tutti_statuses), roots=roots,
            )

        tutti_duoagent_paths = collect_tutti_duoagent_paths(tutti_root)
        for path in tutti_duoagent_paths:
            relative = path.relative_to(tutti_root).as_posix()
            add_text_source(
                staging, receipts, repository="tutti", category="tutti-duoagent-reference",
                root=tutti_root, path=path, target=f"tutti-duoagent/source/{relative}",
                git_status=status_for(relative, tutti_tracked, tutti_statuses), roots=roots,
            )

        for path in walk_eligible(skills_root):
            relative = path.relative_to(skills_root).as_posix()
            add_text_source(
                staging, receipts, repository="pi-skills", category="private-pi-skill-package",
                root=skills_root, path=path, target=f"skills/private-pi-skills/{relative}",
                git_status=status_for(relative, skills_tracked, skills_statuses), roots=roots,
            )

        for path in walk_eligible(current_skills_root):
            relative = path.relative_to(current_skills_root).as_posix()
            source_relative = path.relative_to(PAW_ROOT).as_posix()
            add_text_source(
                staging, receipts, repository="paw", category="current-paw-skill",
                root=current_skills_root, path=path, target=f"skills/current-paw/{relative}",
                git_status=status_for(source_relative, paw_tracked, paw_statuses), roots=roots,
            )

        for relative in TRACE_EVIDENCE_FILES:
            path = PAW_ROOT / relative
            add_text_source(
                staging, receipts, repository="paw", category="trace-evaluation-result",
                root=PAW_ROOT, path=path, target=f"evaluation/trace-results/{relative}",
                git_status=status_for(relative, paw_tracked, paw_statuses), roots=roots,
            )

        for relative in AGENT_LAB_EVIDENCE_FILES:
            path = PAW_ROOT / relative
            add_text_source(
                staging, receipts, repository="paw", category="agent-lab-evaluation-result",
                root=PAW_ROOT, path=path, target=f"evaluation/agent-lab-results/{relative}",
                git_status=status_for(relative, paw_tracked, paw_statuses), roots=roots,
            )

        for relative in TRACE_UI_FILES:
            path = PAW_ROOT / relative
            add_text_source(
                staging, receipts, repository="paw", category="pawos-trace-ui-source",
                root=PAW_ROOT, path=path, target=f"code/pawos-trace/{relative}",
                git_status=status_for(relative, paw_tracked, paw_statuses), roots=roots,
            )

        for relative in AGENT_LAB_UI_FILES:
            path = PAW_ROOT / relative
            add_text_source(
                staging, receipts, repository="paw", category="pawos-agent-lab-ui-source",
                root=PAW_ROOT, path=path, target=f"code/pawos-agent-lab/{relative}",
                git_status=status_for(relative, paw_tracked, paw_statuses), roots=roots,
            )

        for relative in GUIDE_FILES:
            copy_generated_text(staging, PAW_ROOT / relative, f"guides/{Path(relative).name}", roots)
        authority_paths = [PAW_ROOT / relative for relative in AUTHORITY_FILES if (PAW_ROOT / relative).is_file()]
        for pattern in AUTHORITY_GLOBS:
            authority_paths.extend(sorted(PAW_ROOT.glob(pattern)))
        seen_authority_paths: set[Path] = set()
        for path in authority_paths:
            if path in seen_authority_paths:
                continue
            seen_authority_paths.add(path)
            # Preserve the source-relative path so two README.md files (the
            # PAW root index and the frontend docs index) cannot overwrite one
            # another and remain independently traceable in the manifest.
            relative = path.relative_to(PAW_ROOT).as_posix()
            copy_generated_text(staging, path, f"authorities/{relative}", roots)
        for relative in INTERFACE_FILES:
            path = PAW_ROOT / relative
            if path.is_file():
                copy_generated_text(staging, path, f"interfaces-and-fixtures/{path.name}", roots)

        copy_generated_text(
            staging, PAW_ROOT / "docs/handoffs/model-bundles/PAW_BACKEND_MODEL_BUNDLE.md",
            "single-file-bundles/PAW_BACKEND_MODEL_BUNDLE.md", roots,
        )
        copy_generated_text(
            staging, PAW_ROOT / "docs/handoffs/model-bundles/PAW_PI_RUNTIME_MODEL_BUNDLE.md",
            "single-file-bundles/PAW_PI_RUNTIME_MODEL_BUNDLE.md", roots,
        )

        legal_sources = (
            (PAW_ROOT / "LICENSE", "licenses/PAW_LICENSE"),
            (PAW_ROOT / "THIRD_PARTY_NOTICES.md", "licenses/PAW_THIRD_PARTY_NOTICES.md"),
            (tutti_root / "LICENSE", "licenses/TUTTI_APACHE_2.0.txt"),
            (pi_root / "LICENSE", "licenses/PI_MIT.txt"),
            (skills_root / "LICENSES/PAW-GPL-3.0-only.txt", "licenses/PI_SKILLS_PAW_GPL_3.0_ONLY.txt"),
        )
        for source, target in legal_sources:
            if source.is_file():
                copy_generated_text(staging, source, target, roots)

        candidate_text = render_prompt_candidates(tutti_root, roots)
        (staging / "tutti-prompts/prompt-source-candidates.tsv").write_text(candidate_text, encoding="utf-8")
        extraction = render_tutti_extraction(staging, receipts)
        (staging / "tutti-prompts/TUTTI_SYSTEM_PROMPTS_EXTRACTED.md").write_text(extraction, encoding="utf-8")
        duoagent_index = render_duoagent_index(receipts)
        (staging / "tutti-duoagent/SOURCE_INDEX.md").write_text(duoagent_index, encoding="utf-8")

        trace_dir = staging / "evaluation/trace-results"
        trace_dir.mkdir(parents=True, exist_ok=True)
        (trace_dir / "TRACE_TEST_RESULTS.md").write_text(
            TRACE_TEST_RESULTS_MARKDOWN, encoding="utf-8"
        )
        (trace_dir / "README.md").write_text(
            render_trace_evidence_index(
                staging,
                receipts,
                missing_prompt_exact=missing_prompt_exact,
                missing_duoagent_exact=missing_duoagent_exact,
            ),
            encoding="utf-8",
        )
        agent_lab_dir = staging / "evaluation/agent-lab-results"
        agent_lab_dir.mkdir(parents=True, exist_ok=True)
        (agent_lab_dir / "README.md").write_text(
            render_agent_lab_evidence_index(staging, receipts),
            encoding="utf-8",
        )
        (staging / "guides/PROMPT_SKILL_INVENTORY_CURRENT.md").write_text(
            render_prompt_skill_inventory(
                private_skill_names,
                current_skill_names,
                len(tutti_prompt_paths),
                len(tutti_duoagent_paths),
                missing_prompt_exact,
                missing_duoagent_exact,
                high_privilege_paths,
            ),
            encoding="utf-8",
        )

        write_entrypoint(staging, snapshots, receipts, private_skill_names, current_skill_names)
        validation = validate_package(staging, receipts)

        manifest_dir = staging / "manifest"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schemaVersion": 1,
            "packagePurpose": "PAW backend and Pi Runtime review with Tutti prompt and DuoAgent references and PAW Skills",
            "referenceDomains": {
                "tuttiPrompts": "reference-only prompt assembly and provider transport",
                "tuttiDuoAgent": "reference-only Tutti Mode execution, Goal Review, and adjacent CollaborationRun vertical slice",
            },
            "traceEvidence": {
                "selectedSourceFiles": [
                    receipt.source_relative for receipt in receipts
                    if receipt.category == "trace-evaluation-result"
                ],
                "generatedIndex": "evaluation/trace-results/README.md",
                "generatedTestReceipt": "evaluation/trace-results/TRACE_TEST_RESULTS.md",
                "publicManifestNotAgentInput": True,
                "hostPrivateGoldIncluded": False,
            },
            "agentLabEvidence": {
                "selectedSourceFiles": [
                    receipt.source_relative for receipt in receipts
                    if receipt.category == "agent-lab-evaluation-result"
                ],
                "selectedUiSourceFiles": [
                    receipt.source_relative for receipt in receipts
                    if receipt.category == "pawos-agent-lab-ui-source"
                ],
                "generatedIndex": "evaluation/agent-lab-results/README.md",
                "qualifiedProjects": ["enterpriseops", "cloudops", "memory", "enterprise-rag"],
                "invalidOrPendingProjects": [],
                "enterpriseRagCalibration": "post_validation_candidate_aware",
                "providerBillIncluded": False,
                "hostPrivateGoldIncluded": False,
                "heldOutResultIncluded": False,
                "installedOrForegroundProofIncluded": False,
            },
            "promptAndSkillSelection": {
                "missingTuttiPromptExactEntries": missing_prompt_exact,
                "missingTuttiDuoAgentExactEntries": missing_duoagent_exact,
                "highPrivilegeSkillFiles": high_privilege_paths,
                "inventory": "guides/PROMPT_SKILL_INVENTORY_CURRENT.md",
            },
            "sourceSnapshots": [asdict(item) for item in snapshots],
            "privateSkills": private_skill_names,
            "currentPawSkills": current_skill_names,
            "exclusions": sorted(EXCLUDED_PARTS | EXCLUDED_NAMES | {
                "user configuration", "SQLite databases", "chat history", "logs",
                "model weights", "dependency trees", "build output", "global third-party Skills",
            }),
            "validation": validation,
            "sourceFiles": [asdict(receipt) for receipt in sorted(receipts, key=lambda receipt: receipt.target)],
            "generatedArtifacts": [
                "00_READ_ME_FIRST_BACKEND.md",
                "guides/PROMPT_SKILL_INVENTORY_CURRENT.md",
                "evaluation/trace-results/README.md",
                "evaluation/trace-results/TRACE_TEST_RESULTS.md",
                "evaluation/agent-lab-results/README.md",
                "tutti-prompts/TUTTI_SYSTEM_PROMPTS_EXTRACTED.md",
                "tutti-duoagent/SOURCE_INDEX.md",
            ],
        }
        manifest_path = manifest_dir / "package-manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        checksums = []
        for path in sorted(staging.rglob("*"), key=lambda item: item.relative_to(staging).as_posix()):
            if path.is_file() and path.name != "SHA256SUMS.txt":
                checksums.append(f"{sha256(path.read_bytes())}  {path.relative_to(staging).as_posix()}")
        (manifest_dir / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")

        final_validation = validate_package(staging, receipts)
        package_name = output.stem
        deterministic_zip(staging, output, package_name)

    result = {
        "ok": True,
        "output": str(output),
        "outputBytes": output.stat().st_size,
        "outputSha256": sha256(output.read_bytes()),
        "sourceReceiptCount": len(receipts),
        "tuttiPromptSourceCount": len(tutti_prompt_paths),
        "tuttiDuoAgentSourceCount": len(tutti_duoagent_paths),
        "privateSkillCount": len(private_skill_names),
        "currentPawSkillCount": len(current_skill_names),
        "validation": final_validation,
    }
    return result


def main() -> None:
    args = parse_args()
    output = args.output.expanduser().resolve()
    if args.check:
        with tempfile.TemporaryDirectory(prefix="paw-backend-package-check-") as directory:
            candidate = Path(directory) / output.name
            result = build(args, candidate)
            if not output.is_file() or output.read_bytes() != candidate.read_bytes():
                raise SystemExit(f"package is stale: {output}")
            result["output"] = str(output)
            result["mode"] = "check"
    else:
        result = build(args, output)
        result["mode"] = "write"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
