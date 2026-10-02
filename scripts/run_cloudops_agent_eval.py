#!/usr/bin/env python3
"""Run a frozen CloudOps blind suite through real PAW AgentService Sessions.

The orchestration entry point is dependency-injected so tests never launch a
model.  A Host caller supplies its already configured AgentService and the
append-only Trace/Eval/Sandbox/Artifact stores.  Gold is passed only to the
post-terminal scorer callable and never to a Session or Tool gateway.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from rag_ime.agent_artifacts import AgentArtifactStore
from rag_ime.agent_service import AgentService
from rag_ime.cloudops_benchmark_agent import (
    CloudOpsBenchmarkGateway,
    CloudOpsBenchmarkGatewayServer,
    CloudOpsBlindSuite,
)
from rag_ime.eval_run_store import EvalRunStore
from rag_ime.managed_pi_runtime import snapshot_managed_pi_runtime_payload
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.sandbox_run_store import SandboxRunStore
from rag_ime.trace_runtime import (
    ArtifactRef,
    EvidenceRef,
    TraceLink,
    build_eval_run,
    build_sandbox_run,
    build_trace_envelope,
    make_span,
)
from rag_ime.trace_store import TraceStore
from scripts.agent_eval_candidate_prompt import (
    append_candidate_prompt, candidate_prompt_identity, load_candidate_prompt,
)

HostScorer = Callable[[Path, list[dict[str, object]]], Mapping[str, object]]
_TRIAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_SCORE_METRICS = ("AnswerCoverage", "CA", "FA", "JRA", "Top3JRA")
_USAGE_KEYS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
_CONTEXT_PROJECTIONS = frozenset({"standard-v1", "observation-id-v1"})
_LUNA_OWNER_MECHANISM_PROFILE = "luna-owner-mechanism-gate-v8"


class EvaluationCancelled(RuntimeError):
    """The owning caller stopped this evaluation; no further batch is admitted."""


def _check_cancelled(cancelled: Callable[[], bool] | None, service: Any = None, session_id: str = "") -> None:
    if cancelled is not None and cancelled():
        if service is not None and session_id:
            service.abort(session_id)
        raise EvaluationCancelled("evaluation cancelled")


class _CloudOpsContextProjectionGateway:
    """Change only the public Tool addressing projected into model context.

    The wrapped gateway remains the sole owner of suite validation, budgets,
    answers, and the append-only ledger.  ``observation-id-v1`` replaces the
    long cache key in public list/read traffic with the already-derived short
    observation id.  It does not change the prompt, observation body, cases,
    Gold, scorer, or Host-side evidence hashes.
    """

    def __init__(
        self,
        gateway: CloudOpsBenchmarkGateway,
        *,
        context_projection: str = "standard-v1",
    ) -> None:
        if context_projection not in _CONTEXT_PROJECTIONS:
            raise ValueError("CloudOps context projection is unsupported")
        self._gateway = gateway
        self.context_projection = context_projection
        self.suite = gateway.suite
        self.max_reads_per_case = gateway.max_reads_per_case
        self._source_chars = 0
        self._projected_chars = 0
        self._projected_calls = 0

    def bind_session(self, *args: object, **kwargs: object) -> dict[str, object]:
        forwarded = dict(kwargs)
        if forwarded.get("workflow_profile") in {
            "quality-bounded-v3",
            "quality-staged-v4",
            "alert-first-v5",
            "luna-evidence-calibrated-v6",
            "luna-bounded-evidence-v7",
            _LUNA_OWNER_MECHANISM_PROFILE,
        }:
            # This candidate changes only the Agent-visible diagnostic prompt.
            # The owning gateway remains on the exact baseline Tool contract.
            forwarded["workflow_profile"] = "baseline-v1"
        return self._gateway.bind_session(*args, **forwarded)

    def unbind_session(self, *args: object, **kwargs: object) -> bool:
        return self._gateway.unbind_session(*args, **kwargs)

    def answers(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
        return self._gateway.answers(*args, **kwargs)

    def ledger(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._gateway.ledger(*args, **kwargs)

    def runtime_manifests(self, session: Mapping[str, object]) -> list[dict[str, object]]:
        manifests = deepcopy(self._gateway.runtime_manifests(session))
        if self.context_projection == "standard-v1" or not manifests:
            return manifests
        schemas = manifests[0].get("parameters", {}).get("oneOf", [])
        for schema in schemas:
            if not isinstance(schema, dict):
                continue
            properties = schema.get("properties")
            if not isinstance(properties, dict):
                continue
            operation = properties.get("op")
            if not isinstance(operation, dict) or operation.get("const") != "read":
                continue
            properties.pop("cacheKey", None)
            properties["observationId"] = {
                "type": "string",
                "pattern": r"^obs_[0-9a-f]{24}$",
            }
            schema["required"] = ["op", "caseId", "observationId"]
        return manifests

    def execute(self, payload: Mapping[str, object]) -> dict[str, object]:
        forwarded = deepcopy(dict(payload))
        args = forwarded.get("args")
        if (
            self.context_projection == "observation-id-v1"
            and isinstance(args, dict)
            and str(args.get("op") or "") == "read"
        ):
            case_id = str(args.get("caseId") or "")
            observation_id = str(args.pop("observationId", "") or "")
            args["cacheKey"] = self._cache_key_for_observation_id(
                case_id,
                observation_id,
            )
        source = self._gateway.execute(forwarded)
        projected = deepcopy(source)
        if self.context_projection == "observation-id-v1":
            result = projected.get("result")
            if isinstance(result, dict):
                result.pop("cacheKey", None)
                items = result.get("items")
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            item.pop("cacheKey", None)
        self._source_chars += len(_canonical(source))
        self._projected_chars += len(_canonical(projected))
        self._projected_calls += 1
        return projected

    def projection_summary(self) -> dict[str, object]:
        return {
            "schemaVersion": "paw.cloudops-context-projection.v1",
            "profile": self.context_projection,
            "toolResultCount": self._projected_calls,
            "sourceChars": self._source_chars,
            "projectedChars": self._projected_chars,
            "savedChars": self._source_chars - self._projected_chars,
            "semanticObservationBodiesChanged": False,
            "promptChanged": False,
        }

    def _cache_key_for_observation_id(self, case_id: str, observation_id: str) -> str:
        cursor = 0
        while True:
            page = self.suite.list_observations(case_id, cursor=cursor, limit=50)
            for item in page.get("items") or []:
                if (
                    isinstance(item, Mapping)
                    and str(item.get("observationId") or "") == observation_id
                ):
                    return str(item.get("cacheKey") or "")
            next_cursor = str(page.get("nextCursor") or "")
            if not next_cursor:
                break
            cursor = int(next_cursor)
        raise ValueError("CloudOps observation id is not present in the assigned case")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_trial_id(value: object) -> str:
    normalized = str(value or "").strip()
    if _TRIAL_ID.fullmatch(normalized) is None:
        raise ValueError("CloudOps trial id must be a bounded basename")
    return normalized


def invoke_host_scorer(
    scorer_script: str | Path,
    gold_path: str | Path,
    answers: list[dict[str, object]],
) -> dict[str, object]:
    """Invoke the existing host scorer without putting labels in Agent state."""

    script = Path(scorer_script).expanduser().resolve(strict=True)
    gold = Path(gold_path).expanduser().resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="cloudops-host-score-") as temporary:
        root = Path(temporary)
        answer_path = root / "answers.jsonl"
        output_path = root / "score.json"
        answer_path.write_text(
            "".join(_canonical(answer) + "\n" for answer in answers),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                "python3",
                str(script),
                "--gold",
                str(gold),
                "--answers",
                str(answer_path),
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not output_path.is_file():
            raise RuntimeError("CloudOps host scorer failed")
        value = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("CloudOps host scorer returned an invalid report")
        return value


def _candidate_runtime_config(
    candidate_root: str | Path,
    *,
    run_root: str | Path,
    source_agent_config: str | Path,
    provider: str,
    model: str,
    tool_gateway_url: str,
    tool_gateway_token: str,
    runtime_wrapper: str | Path | None = None,
    spool_dir: str | Path | None = None,
) -> tuple[PiRuntimeConfig, dict[str, object]]:
    """Verify one explicit payload and create an isolated, non-installed config."""

    candidate = Path(candidate_root).expanduser().resolve(strict=True)
    installation = snapshot_managed_pi_runtime_payload(candidate)
    private_root = Path(run_root).expanduser().resolve(strict=False)
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_root.chmod(0o700)
    agent_config = private_root / "agent" / "config"
    agent_config.mkdir(parents=True, exist_ok=True, mode=0o700)
    agent_config.chmod(0o700)
    source = Path(source_agent_config).expanduser().resolve(strict=True)
    copied: list[str] = []
    for name in ("auth.json", "models.json", "models-store.json", "settings.json"):
        source_path = source / name
        if not source_path.is_file() or source_path.is_symlink():
            continue
        target = agent_config / name
        shutil.copy2(source_path, target)
        target.chmod(0o600)
        copied.append(name)
    if "auth.json" not in copied:
        raise RuntimeError("CloudOps candidate Runtime requires an existing auth.json")

    base = PiRuntimeConfig.from_environment(enabled_default=True)
    provider_environment = dict(base.provider_environment)
    executable = Path(installation.executable)
    if runtime_wrapper is not None:
        wrapper = Path(runtime_wrapper).expanduser().resolve(strict=True)
        if spool_dir is None:
            raise ValueError("CloudOps spool Runtime requires a spool directory")
        spool = Path(spool_dir).expanduser().resolve(strict=True)
        provider_environment.update(
            {
                "RAG_IME_BENCHMARK_GATEWAY_SPOOL": str(spool),
                "RAG_IME_BENCHMARK_RUNTIME_ENTRYPOINT": str(executable),
            }
        )
        executable = wrapper
    config = replace(
        base,
        enabled=True,
        executable=executable,
        extension_path=Path(installation.extension_path),
        node_executable=str(installation.node_executable),
        agent_dir=agent_config,
        session_dir=private_root / "agent" / "sessions",
        logs_dir=private_root / "agent" / "logs",
        debug_context_dir=private_root / "agent" / "debug-context",
        provider=str(provider),
        model=str(model),
        tools=tuple(installation.tools),
        tool_gateway_url=str(tool_gateway_url),
        tool_gateway_token=str(tool_gateway_token),
        provider_environment=provider_environment,
        model_configured=True,
        model_configuration_error="",
        pi_version=str(installation.pi_version),
        protocol_version=str(installation.protocol_version),
        installation_error="",
        idle_timeout_seconds=0,
        command_timeout_seconds=60.0,
        max_sessions=1,
    )
    identity = {
        "schemaVersion": "paw.cloudops-runtime-candidate.v1",
        "runtimeVersion": str(installation.runtime_version),
        "piVersion": str(installation.pi_version),
        "protocolVersion": str(installation.protocol_version),
        "manifestSha256": str(installation.manifest_sha256),
        "entrypointSha256": _file_sha256(Path(installation.executable)),
        "nodeSha256": _file_sha256(Path(installation.node_executable)),
        "extensionSha256": _file_sha256(Path(installation.extension_path)),
        "provider": str(provider),
        "model": str(model),
        "toolProfileSha256": _sha256(list(installation.tools)),
        "sourceConfigFiles": sorted(copied),
        "installActionPerformed": False,
    }
    identity["identitySha256"] = _sha256(identity)
    return config, identity


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _public_host_invocation(args: Any, *, transport: str) -> dict[str, object]:
    """Project the reproducibility contract without publishing host locators.

    The private command contains the auth-config, gold, scorer, candidate and
    output paths.  Their identities belong in the bounded runtime/suite/score
    receipts, not in a public argv echo.
    """

    payload: dict[str, object] = {
        "schemaVersion": "paw.cloudops-host-invocation.v1",
        "trialId": _normalize_trial_id(args.trial_id),
        "provider": str(args.provider),
        "model": str(args.model),
        "thinking": str(args.thinking),
        "transport": str(transport),
        "timeoutSeconds": float(args.timeout_seconds),
        "maxReadsPerCase": int(args.max_reads_per_case),
        "workflowProfile": str(getattr(args, "workflow_profile", "baseline-v1")),
        "contextProjection": str(getattr(args, "context_projection", "standard-v1")),
        "hostPathsPublished": False,
    }
    payload["commandSha256"] = _sha256(payload)
    return payload


def _assert_expected_runtime(
    receipt: Mapping[str, object],
    *,
    provider: str,
    model: str,
    thinking_level: str | None,
) -> None:
    state = receipt.get("state")
    selected = state.get("model") if isinstance(state, Mapping) else None
    actual_provider = str(selected.get("provider") or "") if isinstance(selected, Mapping) else ""
    actual_model = str(selected.get("id") or "") if isinstance(selected, Mapping) else ""
    actual_thinking = str(state.get("thinkingLevel") or "") if isinstance(state, Mapping) else ""
    if (actual_provider, actual_model) != (str(provider), str(model)):
        raise RuntimeError("CloudOps evaluation model route drifted from the frozen Runtime identity")
    if thinking_level is not None and actual_thinking != str(thinking_level):
        raise RuntimeError("CloudOps evaluation thinking level drifted from the frozen Runtime identity")


def run_cloudops_agent_eval(
    *,
    suite: CloudOpsBlindSuite,
    gateway: CloudOpsBenchmarkGateway,
    service: Any,
    trial_id: str,
    gold_path: str | Path,
    score_host_only: HostScorer,
    trace_store: TraceStore,
    eval_store: EvalRunStore,
    sandbox_store: SandboxRunStore,
    artifact_store: AgentArtifactStore,
    now_ms: Callable[[], int] | None = None,
    timeout_seconds: float = 600.0,
    runtime_identity: Mapping[str, object] | None = None,
    thinking_level: str = "max",
    workflow_profile: str = "baseline-v1",
    context_projection: str = "standard-v1",
    candidate_prompt_file: str | Path | None = None,
    evaluation_split: str = "validation",
    scorer_identity: str = "",
    cancelled: Callable[[], bool] | None = None,
    on_session: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Execute the frozen batches sequentially and persist one truthful chain."""

    candidate_prompt = load_candidate_prompt(candidate_prompt_file, evaluation_split=evaluation_split)
    prompt_identity = candidate_prompt_identity(candidate_prompt)
    _check_cancelled(cancelled)
    clock = now_ms or (lambda: int(time.time() * 1_000))
    normalized_trial = _normalize_trial_id(trial_id)
    gold = Path(gold_path).expanduser().resolve(strict=True)
    if not gold.is_file():
        raise ValueError("CloudOps host scorer input is unavailable")
    if candidate_prompt_file is not None and Path(candidate_prompt_file).expanduser().resolve() == gold:
        raise ValueError("Host Gold cannot be used as a candidate Prompt file")
    gold_sha256 = _file_sha256(gold)
    if tuple(suite.batch_ids) != ("batch-1", "batch-2", "batch-3"):
        raise ValueError("CloudOps P0 runner requires the frozen 3x4 batch plan")
    if any(len(suite.assigned_case_ids(batch_id)) != 4 for batch_id in suite.batch_ids):
        raise ValueError("CloudOps P0 runner requires exactly four cases per batch")
    if workflow_profile not in {
        "baseline-v1",
        "evidence-search-v1",
        "evidence-search-v2",
        "observation-id-v1",
        "quality-bounded-v3",
        "quality-staged-v4",
        "alert-first-v5",
        "luna-evidence-calibrated-v6",
        "luna-bounded-evidence-v7",
        _LUNA_OWNER_MECHANISM_PROFILE,
    }:
        raise ValueError("CloudOps workflow profile is unsupported")
    if context_projection not in _CONTEXT_PROJECTIONS:
        raise ValueError("CloudOps context projection is unsupported")
    artifact_store.initialize()
    batch_plan = {
        batch_id: list(suite.assigned_case_ids(batch_id)) for batch_id in suite.batch_ids
    }
    input_fingerprint = "sha256:" + _sha256(
        {"suite": suite.suite_sha256, "contract": suite.contract_sha256, "batches": batch_plan}
    )
    public_runtime_identity = dict(runtime_identity or {})
    expected_provider = str(public_runtime_identity.get("provider") or "")
    expected_model = str(public_runtime_identity.get("model") or "")
    expected_model_profile = (
        f"{expected_provider}/{expected_model}"
        if expected_provider and expected_model
        else ""
    )

    artifact_kind = "cloudops_eval"
    record_sequence = 0

    def append(event_type: str, payload: Mapping[str, object]) -> dict[str, object]:
        nonlocal record_sequence
        record_sequence += 1
        return artifact_store.append_records(
            owner_kind="connector_run",
            owner_id=normalized_trial,
            artifact_kind=artifact_kind,
            records=[
                {
                    "recordId": f"{normalized_trial}:{record_sequence:04d}",
                    "eventType": event_type,
                    "sequence": record_sequence,
                    "createdAtMs": clock(),
                    "payload": dict(payload),
                }
            ],
        )

    started_at = clock()
    append(
        "trial_started",
        {
            "trialId": normalized_trial,
            "suiteSha256": suite.suite_sha256,
            "contractSha256": suite.contract_sha256,
            "sourceRevision": suite.source_revision,
            "batchPlanSha256": _sha256(batch_plan),
            "goldVisibleToAgent": False,
            "runtimeIdentity": public_runtime_identity,
            "thinkingLevel": thinking_level,
            "workflowProfile": workflow_profile,
            "contextProjection": context_projection,
            "candidatePrompt": prompt_identity,
            "evaluationSplit": evaluation_split,
        },
    )
    service.bind_tool_manifest_provider(gateway.runtime_manifests)
    batch_results: list[dict[str, object]] = []
    merged_answers: list[dict[str, object]] = []

    def check_admission() -> None:
        if cancelled is not None and cancelled():
            append("trial_cancelled", {"completedBatchCount": len(batch_results), "candidatePrompt": prompt_identity})
            _persist_failed_sandbox(sandbox_store, trial_id=normalized_trial, suite=suite, now_ms=clock(), status="cancelled")
            raise EvaluationCancelled("evaluation cancelled")

    for batch_index, batch_id in enumerate(suite.batch_ids, start=1):
        check_admission()
        append(
            "batch_started",
            {
                "batchId": batch_id,
                "assignedCaseIdsSha256": _sha256(list(suite.assigned_case_ids(batch_id))),
            },
        )
        session_payload: dict[str, object] = {
            "title": f"CloudOps blind evaluation {batch_id}",
            "mode": "assistant",
            "roleId": "companion-firstlight-v1",
            "roleVersion": "1",
            "toolProfileVersion": "subagent-readonly-v1",
            "toolAllowlistMode": "explicit",
            "allowedTools": [],
            "_modelRoute": "primary",
        }
        if expected_model_profile:
            session_payload["modelProfile"] = expected_model_profile
        session = service.create_session(session_payload)["session"]
        session_id = str(session["id"])
        service.update_session(
            session_id,
            {
                "mode": "assistant",
                "executionMode": "read_only",
                "toolProfileVersion": "subagent-readonly-v1",
                "toolAllowlistMode": "explicit",
                "allowedTools": [],
                "projectContextEnabled": False,
                "piSkillsEnabled": False,
                "codexSkillsEnabled": False,
                "workspaceRoots": [],
                "thinkingLevel": thinking_level,
            },
        )
        binding = gateway.bind_session(
            session_id,
            batch_id=batch_id,
            workflow_profile=workflow_profile,
        )
        tool_manifest_sha256 = _sha256(gateway.runtime_manifests({"id": session_id})[0])
        base_prompt = _batch_prompt(
            batch_id,
            suite.assigned_case_ids(batch_id),
            workflow_profile=workflow_profile,
        )
        prompt = append_candidate_prompt(base_prompt, candidate_prompt)
        accepted_at = clock()
        terminal = ""
        turn_id = ""
        events: list[dict[str, object]] = []
        try:
            if on_session is not None:
                on_session(session_id)
            _check_cancelled(cancelled, service, session_id)
            ensured = service.ensure_runtime({"sessionId": session_id})
            if expected_model_profile:
                _assert_expected_runtime(
                    ensured,
                    provider=expected_provider,
                    model=expected_model,
                    thinking_level=None,
                )
            thinking_receipt = service.select_thinking_level(
                session_id,
                {"level": thinking_level},
            )
            if str(thinking_receipt.get("thinkingLevel") or "") != thinking_level:
                raise RuntimeError(
                    "CloudOps evaluation thinking level drifted from the frozen Runtime identity"
                )
            ensured = {
                "runtime": ensured,
                "thinkingSelection": thinking_receipt,
            }
            _check_cancelled(cancelled, service, session_id)
            receipt = service.prompt(
                session_id,
                {
                    "message": prompt,
                    "clientMessageId": f"cloudops:{normalized_trial}:{batch_id}",
                },
            )
            turn_id = str(receipt.get("turnId") or "")
            events, terminal = _wait_for_terminal(
                service,
                session_id=session_id,
                turn_id=turn_id,
                timeout_seconds=timeout_seconds,
                cancelled=cancelled,
            )
            answers = gateway.answers(session_id)
            if terminal != "turn_completed" or len(answers) != 4:
                raise RuntimeError("CloudOps batch did not complete one canonical submission")
            ledger = gateway.ledger(session_id=session_id)
            merged_answers.extend(answers)
            batch = {
                "batchId": batch_id,
                "sessionId": session_id,
                "turnId": turn_id,
                "promptSha256": _sha256(prompt),
                "basePromptSha256": _sha256(base_prompt),
                "acceptedAtMs": accepted_at,
                "terminalEvent": terminal,
                "answersSha256": _sha256(answers),
                "answerCount": len(answers),
                "bindingReceiptId": str(binding["bindingReceiptId"]),
                "ledger": ledger,
                "usage": _token_usage(events),
                "eventCount": len(events),
                "batchIndex": batch_index,
                "modelIdentitySha256": _sha256(ensured),
                "toolManifestSha256": tool_manifest_sha256,
                "workflowProfile": workflow_profile,
                "contextProjection": context_projection,
            }
            batch_results.append(batch)
            append(
                "batch_completed",
                {
                    "batchId": batch_id,
                    "sessionSha256": _sha256(session_id),
                    "turnSha256": _sha256(turn_id),
                    "answerCount": len(answers),
                    "answersSha256": _sha256(answers),
                    "ledgerSha256": str(ledger["ledgerSha256"]),
                    "terminalEvent": terminal,
                },
            )
        except Exception as exc:
            append(
                "batch_cancelled" if isinstance(exc, EvaluationCancelled) else "batch_failed",
                {
                    "batchId": batch_id,
                    "sessionSha256": _sha256(session_id),
                    "turnSha256": _sha256(turn_id) if turn_id else "",
                    "errorType": type(exc).__name__,
                    "errorFingerprint": "sha256:" + _sha256(f"{type(exc).__name__}:{exc}"),
                },
            )
            _persist_failed_sandbox(
                sandbox_store,
                trial_id=normalized_trial,
                suite=suite,
                now_ms=clock(),
                status="cancelled" if isinstance(exc, EvaluationCancelled) else "failed",
            )
            raise
        finally:
            removed = gateway.unbind_session(session_id)
            append(
                "batch_cleanup",
                {
                    "batchId": batch_id,
                    "sessionSha256": _sha256(session_id),
                    "gatewayBindingRemoved": removed,
                },
            )

    if len(merged_answers) != 12 or {item["case_id"] for item in merged_answers} != set(suite.case_ids):
        raise RuntimeError("CloudOps merged answers do not cover the frozen suite")
    check_admission()
    if _file_sha256(gold) != gold_sha256:
        raise ValueError("CloudOps Host Gold changed during evaluation")
    score = dict(score_host_only(gold, merged_answers))
    metrics, per_case = _validated_score(score, case_ids=suite.case_ids)
    process_signals = _process_signals(score.get("process"))

    batch_trace_ids = [f"trace:cloudops:{normalized_trial}:{item['batchId']}" for item in batch_results]
    aggregate_trace_id = f"trace:cloudops:{normalized_trial}:aggregate"
    batch_eval_ids = [f"eval:cloudops:{normalized_trial}:{item['batchId']}" for item in batch_results]
    aggregate_eval_id = f"eval:cloudops:{normalized_trial}:aggregate"
    sandbox_run_id = f"sandbox:cloudops:{normalized_trial}"
    completed_at = clock()
    total_usage = _sum_usage(batch_results)
    signals = _execution_signals(batch_results, started_at_ms=started_at, completed_at_ms=completed_at)
    append(
        "trial_completed",
        {
            "trialId": normalized_trial,
            "answerSetSha256": _sha256(merged_answers),
            "scoreSha256": _sha256(score),
            "answers": merged_answers,
            "metrics": metrics,
            "perCase": [dict(per_case[case_id]) for case_id in suite.case_ids],
            "processSignals": process_signals,
            "candidatePrompt": prompt_identity,
            "usage": total_usage,
            "signals": signals,
            "traceIds": [*batch_trace_ids, aggregate_trace_id],
            "evalRunIds": [*batch_eval_ids, aggregate_eval_id],
            "sandboxRunId": sandbox_run_id,
            "cleanup": {"gatewayBindingsRemoved": True, "temporaryOutputsRetained": False},
        },
    )
    artifact_store.checkpoint(
        owner_kind="connector_run",
        owner_id=normalized_trial,
        artifact_kind=artifact_kind,
        projection={
            "state": "completed",
            "completedBatchIds": list(suite.batch_ids),
            "metrics": metrics,
        },
        runtime_checkpoint={"terminal": True},
        updated_at_ms=completed_at,
    )
    artifact_reference = artifact_store.reference(
        owner_kind="connector_run",
        owner_id=normalized_trial,
        artifact_kind=artifact_kind,
    )
    trace_artifact = ArtifactRef(
        artifact_id=str(artifact_reference["artifactId"]),
        kind="cloudops_eval_receipt",
        media_type=str(artifact_reference["mediaType"]),
        sha256=str(artifact_reference["sha256"]),
        byte_size=int(artifact_reference["byteSize"]),
        record_count=int(artifact_reference["recordCount"]),
    )

    persisted_batch_traces: list[dict[str, object]] = []
    persisted_batch_evals: list[dict[str, object]] = []
    for result, trace_id, eval_id in zip(
        batch_results, batch_trace_ids, batch_eval_ids, strict=True
    ):
        trace = _build_batch_trace(
            result,
            trace_id=trace_id,
            trial_id=normalized_trial,
            artifact=trace_artifact,
            now_ms=completed_at,
        )
        persisted_batch_traces.append(trace_store.persist(trace))
        case_ids = suite.assigned_case_ids(str(result["batchId"]))
        values = [float(per_case.get(case_id, {}).get("JRA") or 0.0) for case_id in case_ids]
        batch_accuracy = sum(values) / len(values)
        evaluated = build_eval_run(
            eval_run_id=eval_id,
            trace_ids=[trace_id],
            mode="ground_truth",
            truth_kind="frozen",
            metrics={"accuracy": batch_accuracy},
            dataset_id=f"cloudops-{suite.suite_sha256[:16]}",
            label_revision=f"labels-{suite.contract_sha256[:16]}",
            suite_binding={
                "suiteId": "cloudops-smoke-v1",
                "suiteRevision": suite.suite_sha256[:16],
            },
            latency_ms=_batch_latency_ms(result),
            usage=_numeric_usage(result.get("usage")),
            now_ms=completed_at,
        )
        persisted_batch_evals.append(eval_store.persist(evaluated))

    aggregate_trace = build_trace_envelope(
        trace_id=aggregate_trace_id,
        source_kind="vertical_agent",
        input_text="",
        input_fingerprint=input_fingerprint,
        input_normalization="cloudops-frozen-suite-v1",
        binding={"runId": normalized_trial, "caseId": "cloudops-smoke-v1-3x4"},
        spans=tuple(
            make_span(
                span_id=f"span:batch:{index}",
                name="cloudops.batch",
                started_at_ms=completed_at + index,
                ended_at_ms=completed_at + index + 1,
                attributes={"action": str(item["batchId"])},
                metrics={"count": int(item["answerCount"])},
            )
            for index, item in enumerate(batch_results, start=1)
        ),
        artifacts=(trace_artifact,),
        links=tuple(TraceLink(target_trace_id=value, relation="related") for value in batch_trace_ids),
        status="completed",
        created_at_ms=started_at,
        updated_at_ms=completed_at + 4,
    )
    trace_store.persist(aggregate_trace)
    aggregate_eval = eval_store.persist(
        build_eval_run(
            eval_run_id=aggregate_eval_id,
            trace_ids=[aggregate_trace_id],
            mode="ground_truth",
            truth_kind="frozen",
            metrics={"accuracy": metrics["JRA"]},
            dataset_id=f"cloudops-{suite.suite_sha256[:16]}",
            label_revision=f"labels-{suite.contract_sha256[:16]}",
            suite_binding={
                "suiteId": "cloudops-smoke-v1",
                "suiteRevision": suite.suite_sha256[:16],
            },
            latency_ms=max(0, completed_at - started_at),
            usage=_numeric_usage(total_usage),
            now_ms=completed_at + 5,
        )
    )
    cohort = {
        "suiteId": "cloudops-smoke-v1",
        "suiteRevision": suite.suite_sha256[:16],
        "caseId": "cloudops-smoke-v1-3x4",
        "inputFingerprint": input_fingerprint,
        "environmentFingerprint": "sha256:" + _sha256(
            {"python": sys.version_info[:3], "platform": platform.platform(), "runner": "cloudops-p0-v1"}
        ),
        "configFingerprint": "sha256:" + _sha256(
            {
                "batchPlan": batch_plan,
                "sequential": True,
                "maxReadsPerCase": gateway.max_reads_per_case,
                "workflowProfile": workflow_profile,
                "contextProjection": context_projection,
                "candidatePrompt": prompt_identity,
            }
        ),
        "modelProfileFingerprint": "sha256:" + _sha256(
            public_runtime_identity.get("identitySha256")
            or [item.get("modelIdentitySha256", "") for item in batch_results]
        ),
        "toolProfileFingerprint": "sha256:" + _sha256(
            [item.get("toolManifestSha256", "") for item in batch_results]
        ),
        "skillProfileFingerprint": "sha256:" + _sha256("none"),
    }
    sandbox_store.persist(
        build_sandbox_run(
            sandbox_run_id=sandbox_run_id,
            app_id="changeops",
            workspace_root=f"cloudops-blind:{suite.suite_sha256}",
            workspace_binding_id=f"workspace-binding:cloudops:{suite.suite_sha256[:24]}",
            mutation_mode="read_only",
            network="allowlisted",
            trace_ids=[*batch_trace_ids, aggregate_trace_id],
            eval_run_ids=[*batch_eval_ids, aggregate_eval_id],
            replay_cohort=cohort,
            status="completed",
            now_ms=completed_at + 6,
        )
    )
    public_batches = []
    for item in batch_results:
        ledger = item.get("ledger")
        ledger_items = ledger.get("items") if isinstance(ledger, Mapping) else []
        if not isinstance(ledger_items, list):
            ledger_items = []
        batch_id = str(item["batchId"])
        public_batches.append(
            {
                "batchId": batch_id,
                "batchIndex": int(item["batchIndex"]),
                "assignedCaseIdsSha256": _sha256(list(suite.assigned_case_ids(batch_id))),
                "terminalEvent": str(item["terminalEvent"]),
                "answerCount": int(item["answerCount"]),
                "answersSha256": str(item["answersSha256"]),
                "promptSha256": str(item["promptSha256"]),
                "basePromptSha256": str(item["basePromptSha256"]),
                "ledgerSha256": str(ledger.get("ledgerSha256") or "")
                if isinstance(ledger, Mapping)
                else "",
                "usage": dict(item.get("usage") or {}),
                "toolCalls": len(ledger_items),
                "successfulToolCalls": sum(
                    entry.get("ok") is True for entry in ledger_items if isinstance(entry, Mapping)
                ),
                "latencyMs": _batch_latency_ms(item),
            }
        )
    frozen_controls = {
        "suiteSha256": suite.suite_sha256, "businessContractSha256": suite.contract_sha256,
        "goldSha256": gold_sha256, "scorerIdentity": scorer_identity,
        "batchPlanSha256": _sha256(batch_plan), "evaluationSplit": evaluation_split,
        "runtimeIdentity": public_runtime_identity, "thinkingLevel": thinking_level,
        "workflowProfile": workflow_profile, "contextProjection": context_projection,
        "maxReadsPerCase": gateway.max_reads_per_case, "skills": [],
        "timeoutSeconds": timeout_seconds,
        "runnerSha256": _file_sha256(Path(__file__).resolve()),
        "basePrompts": [item["basePromptSha256"] for item in batch_results],
        "toolManifests": [item["toolManifestSha256"] for item in batch_results],
    }
    frozen_controls["contractSha256"] = _sha256(frozen_controls)
    return {
        "schemaVersion": "paw.cloudops-agent-eval-run.v1",
        "trialId": normalized_trial,
        "status": "completed",
        "suiteSha256": suite.suite_sha256,
        "contractSha256": suite.contract_sha256,
        "batchPlan": "3x4-sequential",
        "batchPlanSha256": _sha256(batch_plan),
        "batchCount": 3,
        "caseCount": 12,
        "batches": public_batches,
        "metrics": metrics,
        "processSignals": process_signals,
        "usage": total_usage,
        "signals": signals,
        "traceIds": [*batch_trace_ids, aggregate_trace_id],
        "evalRunIds": [*batch_eval_ids, aggregate_eval_id],
        "aggregateTraceId": aggregate_trace_id,
        "aggregateEvalRunId": str(aggregate_eval["evalRunId"]),
        "sandboxRunId": sandbox_run_id,
        "artifactId": str(artifact_reference["artifactId"]),
        "cleanup": {"gatewayBindingsRemoved": True, "temporaryOutputsRetained": False},
        "goldVisibleToAgent": False,
        "runtimeIdentity": public_runtime_identity,
        "thinkingLevel": thinking_level,
        "workflowProfile": workflow_profile,
        "contextProjection": context_projection,
        "candidatePrompt": prompt_identity,
        "frozenControls": frozen_controls,
        "evaluationSplit": evaluation_split,
        "contextProjectionSummary": (
            gateway.projection_summary()
            if callable(getattr(gateway, "projection_summary", None))
            else {
                "schemaVersion": "paw.cloudops-context-projection.v1",
                "profile": context_projection,
                "semanticObservationBodiesChanged": False,
                "promptChanged": False,
            }
        ),
        "replayCohort": cohort,
    }


def _batch_prompt(
    batch_id: str,
    case_ids: tuple[str, ...],
    *,
    workflow_profile: str = "baseline-v1",
) -> str:
    base = (
        "This is a frozen, read-only CloudOps blind evaluation. "
        "Use only cloudops_benchmark. Call index, selectively list and read exact observations, "
        "then call submit once with exactly the four assigned cases. Do not use workspace, shell, "
        f"network, or external context. Batch={batch_id}; cases={','.join(case_ids)}."
    )
    if workflow_profile == "baseline-v1":
        return base
    if workflow_profile == "quality-bounded-v3":
        return (
            base
            + " For each case, first localize the affected object and name the two most plausible "
            "root causes. Use at most three list pages, then perform at least five and at most eight exact "
            "observation reads. Do not submit until the top diagnosis has direct "
            "root-cause evidence and counterevidence against the closest alternative; healthy adjacent "
            "components are counterevidence, not proof that the affected component is healthy. Preserve "
            "residual uncertainty in Top-2 and Top-3. Avoid narration between calls and emit the smallest "
            "valid submit JSON once all four cases meet this evidence rule."
        )
    if workflow_profile == "quality-staged-v4":
        return (
            base
            + " Investigate all four cases in two stages. Stage 1: localize the affected object and name "
            "the closest competing cause for every case. Stage 2: read only observations that complete a "
            "causal chain or falsify that competitor. For code defects, follow caller/callee values across "
            "both sides of the failure. For service failures, inspect gateway route configuration plus the "
            "target Service and endpoints before choosing DNS, selector, policy, or routing causes. For "
            "performance failures, compare alert onset through the caller graph to find the earliest affected "
            "leaf, then distinguish application delay from transport or resource faults using connectivity, "
            "errors, and image identity. Across the whole batch, target no more than eight list pages and "
            "twenty-four exact reads, but do not submit any case without direct support and evidence against "
            "its closest alternative. Healthy adjacent components are counterevidence, not proof. Preserve "
            "residual uncertainty in Top-2 and Top-3, avoid narration between calls, and emit the smallest "
            "valid submit JSON once all four causal chains are complete."
        )
    if workflow_profile in {
        "alert-first-v5",
        "luna-evidence-calibrated-v6",
        "luna-bounded-evidence-v7",
        _LUNA_OWNER_MECHANISM_PROFILE,
    }:
        prompt = (
            base
            + " Investigate every case independently in two stages: localize the affected object, then "
            "falsify its closest competing cause. For code defects, follow caller/callee values across both "
            "sides of the failure. For service failures, inspect gateway route configuration plus the target "
            "Service and endpoints before choosing DNS, selector, policy, or routing causes. For each "
            "performance case, after one inventory page first read the GetAlerts observation; use its anomaly "
            "magnitudes and the propagation order with dependency observations to localize the earliest "
            "affected leaf. Only then use connectivity, errors, resource signals, or image evidence to "
            "distinguish code delay from transport or capacity faults. Do not use pod age, image pull events, "
            "or image identity to localize, and never compare image identity across independent cases. Across "
            "the whole batch, target no more than eight list pages and twenty-four exact reads, but quality "
            "wins: every Top-1 needs direct support and evidence against its closest alternative. Keep a "
            "compact four-row evidence ledger without recapping completed cases or narrating between calls. "
            "Preserve uncertainty in Top-2 and Top-3; keep each key_evidence_summary within 55 words and emit "
            "the smallest valid submit JSON once all four causal chains are complete."
        )
        if workflow_profile == "alert-first-v5":
            return prompt
        shared_luna_corrections = (
            " This Luna adaptation changes only the Agent prompt. For every read, copy its cacheKey "
            "byte-for-byte from a list result for that same case. Never synthesize a cacheKey from a Tool "
            "name or arguments, and never reuse one across cases. Treat fault_object as the affected application whose task is failing when a "
            "shared scheduling policy blocks it; use a node fault_object only when that specific node itself "
            "is faulty. For performance diagnoses, successful connectivity does not rule out latency or "
            "packet delay, and empty application error logs do not support code_artificial_delay. Rank "
            "code_artificial_delay first only with direct code or runtime evidence of an inserted delay; "
            "otherwise distinguish pod_network_delay from CPU or node capacity using latency propagation, "
            "traffic anomalies, throttling, and resource signals."
        )
        if workflow_profile == "luna-evidence-calibrated-v6":
            return (
                prompt
                + shared_luna_corrections
                + " Make one Tool call at a time; do not issue parallel reads."
            )
        bounded_luna_contract = (
            prompt
            + shared_luna_corrections
            + " Treat the retrieval budget as a strict ceiling, not a target: use no more than one broad inventory "
            "page and six exact reads per case. As soon as one case has direct Top-1 support and evidence "
            "against its closest alternative, stop reading that case immediately and lock its ledger row. "
            "Do not revisit a completed case. Independent exact reads may share one response only after every "
            "cacheKey has already appeared in a list result for its own case. When the ceiling is reached, "
            "preserve remaining uncertainty in Top-2 and Top-3 and submit the best supported four-row result "
            "instead of exploring the observation catalog exhaustively."
        )
        if workflow_profile == "luna-bounded-evidence-v7":
            return bounded_luna_contract
        return (
            bounded_luna_contract
            + " Before locking any Top-1, satisfy an owner-and-mechanism gate through an exact family lookup. "
            "For a runtime crash, call list(toolName=\"GetErrorLogs\") and read logs from an affected workload. "
            "For partial reachability or routing, call list(toolName=\"GetAlerts\") followed by "
            "list(toolName=\"GetAppYAML\"), and attribute a bad route to the component whose configuration "
            "contains it. For performance, retain the earliest alerting application as owner unless "
            "list(toolName=\"CheckNodeServiceStatus\") or another direct node observation proves a node-wide "
            "fault. These exact-family selectors are evidence lookups and do not count as an additional broad "
            "inventory page. Never invent a toolName. Inventory status, labels, co-location, and healthy "
            "neighbors may localize but cannot satisfy this gate. If the evidence summary says the discriminator "
            "remains unresolved, do not lock that row; spend the remaining bounded reads on the exact family, "
            "and never replace missing evidence with a guessed owner or mechanism."
        )
    if workflow_profile in {"evidence-search-v2", "observation-id-v1"}:
        prompt = (
            base
            + " For each case, localize the fault object first, then discriminate the root cause. "
            "Do not promote an app or pod fault to a node without direct node-fault evidence; a healthy "
            "Ready node with Flannel up is counterevidence for a node fault, not proof that no pod network "
            "delay exists. Use at most two distinct search calls per case, at most one list fallback, and at "
            "most six exact observation reads. Before any additional retrieval, name the two diagnoses that "
            "the next observation will distinguish; if it will not add a new evidenceId, stop exploring. "
            "Top-1 must have direct support plus counterevidence against the closest competing diagnosis; "
            "use Top-2 and Top-3 to preserve residual uncertainty."
        )
        if workflow_profile == "observation-id-v1":
            prompt += " Use observationId returned by list or search for every read; never construct or infer one."
        return prompt
    if workflow_profile != "evidence-search-v1":
        raise ValueError("CloudOps workflow profile is unsupported")
    return (
        base
        + " Use search before paging list when a symptom, service, Tool or competing root cause is known; "
        "read only the discriminating observations returned by search. For performance cases, first localize "
        "the affected component, then inspect connectivity and network evidence before choosing between a "
        "network fault and an application-code delay. Empty application logs do not distinguish those causes; "
        "do not choose a code delay without direct code or runtime evidence."
    )


def _persist_failed_sandbox(
    store: SandboxRunStore,
    *,
    trial_id: str,
    suite: CloudOpsBlindSuite,
    now_ms: int,
    status: str = "failed",
) -> dict[str, object]:
    return store.persist(
        build_sandbox_run(
            sandbox_run_id=f"sandbox:cloudops:{trial_id}",
            app_id="changeops",
            workspace_root=f"cloudops-blind:{suite.suite_sha256}",
            workspace_binding_id=f"workspace-binding:cloudops:{suite.suite_sha256[:24]}",
            mutation_mode="read_only",
            network="allowlisted",
            trace_ids=[],
            eval_run_ids=[],
            status=status,
            now_ms=now_ms,
        )
    )


def _wait_for_terminal(
    service: Any,
    *,
    session_id: str,
    turn_id: str,
    timeout_seconds: float,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, object]], str]:
    deadline = time.monotonic() + max(0.01, float(timeout_seconds))
    while time.monotonic() < deadline:
        _check_cancelled(cancelled, service, session_id)
        events, gap = service.events.replay(session_id)
        if gap:
            raise RuntimeError("CloudOps Agent event replay developed a gap")
        selected = [event.to_payload() for event in events if not turn_id or event.turn_id in {"", turn_id}]
        terminal = next(
            (
                event.event_type
                for event in reversed(events)
                if event.turn_id == turn_id and event.event_type in {"turn_completed", "turn_failed"}
            ),
            "",
        )
        if terminal:
            return selected, terminal
        time.sleep(0.05)
    service.abort(session_id)
    raise TimeoutError("CloudOps Agent turn did not settle before its timeout")


def _validated_score(
    score: Mapping[str, object],
    *,
    case_ids: tuple[str, ...],
) -> tuple[dict[str, float], dict[str, Mapping[str, object]]]:
    aggregate = score.get("aggregate")
    if not isinstance(aggregate, Mapping):
        raise RuntimeError("CloudOps host scorer returned no aggregate")
    metrics: dict[str, float] = {}
    for key in _SCORE_METRICS:
        raw = aggregate.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise RuntimeError(f"CloudOps host scorer omitted required metric {key}")
        value = float(raw)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise RuntimeError(f"CloudOps host scorer returned invalid metric {key}")
        metrics[key] = value

    raw_per_case = score.get("perCase")
    if not isinstance(raw_per_case, list):
        raise RuntimeError("CloudOps host scorer returned no per-case metrics")
    per_case: dict[str, Mapping[str, object]] = {}
    expected = set(case_ids)
    for raw in raw_per_case:
        if not isinstance(raw, Mapping):
            raise RuntimeError("CloudOps host scorer returned an invalid per-case metric")
        case_id = str(raw.get("case_id") or "")
        jra = raw.get("JRA")
        if case_id not in expected or case_id in per_case:
            raise RuntimeError("CloudOps host scorer returned duplicate or unknown cases")
        if isinstance(jra, bool) or not isinstance(jra, (int, float)):
            raise RuntimeError("CloudOps host scorer omitted a per-case JRA metric")
        numeric = float(jra)
        if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
            raise RuntimeError("CloudOps host scorer returned an invalid per-case JRA metric")
        per_case[case_id] = dict(raw)
    if set(per_case) != expected:
        raise RuntimeError("CloudOps host scorer did not cover the frozen case set")
    return metrics, per_case


def _token_usage(events: list[Mapping[str, object]]) -> dict[str, object]:
    totals = {key: 0 for key in _USAGE_KEYS}
    observed = False
    # Pi repeats the final request's usage on message/turn completion. Count
    # actual Provider receipts once, including failed paid requests. Older
    # traces without Provider events retain their existing usage projection.
    provider_events = [event for event in events if event.get("eventType") in {
        "provider_request_completed", "provider_request_failed",
    }]
    for event in provider_events or events:
        usage = event.get("usage")
        if not isinstance(usage, Mapping):
            payload = event.get("payload")
            usage = payload.get("usage") if isinstance(payload, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        aliases = {
            "input": ("input", "inputTokens"),
            "output": ("output", "outputTokens"),
            "cacheRead": ("cacheRead", "cacheReadTokens"),
            "cacheWrite": ("cacheWrite", "cacheWriteTokens"),
            "totalTokens": ("totalTokens",),
        }
        for key, names in aliases.items():
            value = next((usage.get(name) for name in names if usage.get(name) is not None), None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[key] += value
                observed = True
    if not observed:
        return {"available": False}
    if totals["totalTokens"] == 0:
        totals["totalTokens"] = totals["input"] + totals["output"] + totals["cacheRead"] + totals["cacheWrite"]
    if not any(totals.values()):
        return {"available": False}
    return {"available": True, **totals}


def _sum_usage(batch_results: list[Mapping[str, object]]) -> dict[str, object]:
    result = {key: 0 for key in _USAGE_KEYS}
    available = bool(batch_results)
    for batch in batch_results:
        usage = batch.get("usage")
        if not isinstance(usage, Mapping) or usage.get("available") is not True:
            available = False
            continue
        for key in result:
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                available = False
                break
            result[key] += value
    return {"available": True, **result} if available else {"available": False}


def _numeric_usage(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping) or value.get("available") is not True:
        return None
    return {
        key: int(value[key])
        for key in _USAGE_KEYS
        if isinstance(value.get(key), int) and not isinstance(value.get(key), bool)
    }


def _cost_optimization_comparison(
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
) -> dict[str, object]:
    """Fail closed unless one context-only candidate preserves quality and costs less."""

    identity_fields = (
        "suiteSha256",
        "contractSha256",
        "batchPlanSha256",
        "caseCount",
        "thinkingLevel",
    )
    identity_checks = {
        key: baseline.get(key) == candidate.get(key) and baseline.get(key) not in {None, ""}
        for key in identity_fields
    }
    baseline_workflow = str(baseline.get("workflowProfile") or "")
    candidate_workflow = str(candidate.get("workflowProfile") or "")
    baseline_projection = str(baseline.get("contextProjection") or "")
    candidate_projection = str(candidate.get("contextProjection") or "")
    def prompt_hash(report: Mapping[str, object]) -> str | None:
        value = report.get("candidatePrompt")
        if value is None:
            return candidate_prompt_identity(None)["sha256"]
        if not isinstance(value, Mapping) or type(value.get("enabled")) is not bool:
            return None
        digest = value.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return None
        if value["enabled"] is False and digest != candidate_prompt_identity(None)["sha256"]:
            return None
        return digest

    baseline_prompt_hash, candidate_prompt_hash = prompt_hash(baseline), prompt_hash(candidate)
    prompt_identity_valid = baseline_prompt_hash is not None and candidate_prompt_hash is not None
    external_prompt_changed = baseline_prompt_hash != candidate_prompt_hash
    before_controls, after_controls = baseline.get("frozenControls"), candidate.get("frozenControls")
    same_frozen_controls = (
        isinstance(before_controls, Mapping) and isinstance(after_controls, Mapping)
        and isinstance(before_controls.get("contractSha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", before_controls["contractSha256"]) is not None
        and before_controls["contractSha256"] == after_controls.get("contractSha256")
        and isinstance(before_controls.get("scorerIdentity"), str)
        and re.fullmatch(r"[0-9a-f]{64}", before_controls["scorerIdentity"]) is not None
        and before_controls["scorerIdentity"] == after_controls.get("scorerIdentity")
    )
    workflow_changed = baseline_workflow != candidate_workflow
    external_prompt_contract = (
        prompt_identity_valid and external_prompt_changed and not workflow_changed
        and bool(baseline_workflow) and baseline_projection == candidate_projection
        and bool(baseline_projection) and same_frozen_controls
    )
    observation_projection = (
        not workflow_changed
        and baseline_workflow == candidate_workflow == "baseline-v1"
        and baseline_projection == "standard-v1"
        and candidate_projection == "observation-id-v1"
    )
    prompt_contract = (
        workflow_changed
        and baseline_workflow == "baseline-v1"
        and candidate_workflow in {
            "quality-bounded-v3",
            "quality-staged-v4",
            "alert-first-v5",
        }
        and baseline_projection == candidate_projection == "standard-v1"
    )
    luna_prompt_contract = (
        workflow_changed
        and baseline_workflow == "alert-first-v5"
        and candidate_workflow in {
            "luna-evidence-calibrated-v6",
            "luna-bounded-evidence-v7",
            _LUNA_OWNER_MECHANISM_PROFILE,
        }
        and baseline_projection == candidate_projection == "standard-v1"
    )
    identity_checks["candidatePromptIdentity"] = prompt_identity_valid
    identity_checks["singleVariable"] = external_prompt_contract or (
        not external_prompt_changed and (observation_projection or prompt_contract or luna_prompt_contract)
    )
    single_variable = (
        "candidate_prompt_file"
        if external_prompt_contract
        else "invalid_multiple_or_missing_factors"
        if external_prompt_changed
        else "public_tool_addressing_projection"
        if observation_projection
        else "luna_prompt_evidence_contract"
        if luna_prompt_contract
        else "bounded_diagnostic_prompt_contract"
        if prompt_contract
        else "invalid_multiple_or_missing_factors"
    )
    baseline_runtime = baseline.get("runtimeIdentity")
    candidate_runtime = candidate.get("runtimeIdentity")
    runtime_keys = (
        "runtimeVersion",
        "piVersion",
        "protocolVersion",
        "manifestSha256",
        "entrypointSha256",
        "nodeSha256",
        "extensionSha256",
        "provider",
        "model",
    )
    if not isinstance(baseline_runtime, Mapping) or not isinstance(candidate_runtime, Mapping):
        runtime_checks = {key: False for key in runtime_keys}
    else:
        runtime_checks = {
            key: baseline_runtime.get(key) == candidate_runtime.get(key)
            and baseline_runtime.get(key) not in {None, ""}
            for key in runtime_keys
        }
    identity_ok = all(identity_checks.values()) and all(runtime_checks.values())

    baseline_metrics = baseline.get("metrics")
    candidate_metrics = candidate.get("metrics")
    metric_comparison: dict[str, dict[str, object]] = {}
    quality_ok = isinstance(baseline_metrics, Mapping) and isinstance(candidate_metrics, Mapping)
    for key in _SCORE_METRICS:
        before = baseline_metrics.get(key) if isinstance(baseline_metrics, Mapping) else None
        after = candidate_metrics.get(key) if isinstance(candidate_metrics, Mapping) else None
        valid = (
            isinstance(before, (int, float))
            and not isinstance(before, bool)
            and isinstance(after, (int, float))
            and not isinstance(after, bool)
            and math.isfinite(float(before))
            and math.isfinite(float(after))
        )
        non_regressed = bool(valid and float(after) + 1e-12 >= float(before))
        quality_ok = bool(quality_ok and non_regressed)
        metric_comparison[key] = {
            "before": before,
            "after": after,
            "delta": (float(after) - float(before)) if valid else None,
            "nonRegressed": non_regressed,
        }

    usage_categories = {
        "uncachedInputTokens": "input",
        "cachedInputTokens": "cacheRead",
        "outputTokens": "output",
    }
    baseline_usage = baseline.get("usage")
    candidate_usage = candidate.get("usage")
    usage_comparison: dict[str, dict[str, object]] = {}
    usage_ok = (
        isinstance(baseline_usage, Mapping)
        and baseline_usage.get("available") is True
        and isinstance(candidate_usage, Mapping)
        and candidate_usage.get("available") is True
    )
    strict_decrease = False
    for public_key, source_key in usage_categories.items():
        before = baseline_usage.get(source_key) if isinstance(baseline_usage, Mapping) else None
        after = candidate_usage.get(source_key) if isinstance(candidate_usage, Mapping) else None
        valid = (
            isinstance(before, int)
            and not isinstance(before, bool)
            and before >= 0
            and isinstance(after, int)
            and not isinstance(after, bool)
            and after >= 0
        )
        non_increasing = bool(valid and after <= before)
        decreased = bool(valid and after < before)
        usage_ok = bool(usage_ok and non_increasing)
        strict_decrease = strict_decrease or decreased
        usage_comparison[public_key] = {
            "before": before,
            "after": after,
            "delta": (after - before) if valid else None,
            "nonIncreasing": non_increasing,
            "strictlyDecreased": decreased,
        }
    cost_ok = bool(usage_ok and strict_decrease)
    passed = bool(identity_ok and quality_ok and cost_ok)
    return {
        "schemaVersion": "paw.cloudops-cost-optimization-comparison.v1",
        "decision": "keep" if passed else "reject",
        "singleVariable": single_variable,
        "baselineWorkflowProfile": baseline_workflow,
        "candidateWorkflowProfile": candidate_workflow,
        "baselineContextProjection": baseline_projection,
        "candidateContextProjection": candidate_projection,
        "baselineCandidatePromptSha256": baseline_prompt_hash,
        "candidateCandidatePromptSha256": candidate_prompt_hash,
        "identityGatePassed": identity_ok,
        "identityChecks": {**identity_checks, **{f"runtime.{k}": v for k, v in runtime_checks.items()}},
        "qualityGatePassed": quality_ok,
        "quality": metric_comparison,
        "costGatePassed": cost_ok,
        "usage": usage_comparison,
        "costAuthority": "same-provider-model-usage-categories",
        "providerBillAvailable": False,
        "elapsedIsKeepGate": False,
    }


def _cloudops_optimization_receipt(
    *,
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
    baseline_report_sha256: str,
    candidate_report_sha256: str,
) -> dict[str, object]:
    comparison = _cost_optimization_comparison(baseline, candidate)
    variable = str(comparison["singleVariable"])
    if variable == "candidate_prompt_file":
        factor = {
            "layer": "prompt", "name": variable,
            "before": comparison["baselineCandidatePromptSha256"],
            "after": comparison["candidateCandidatePromptSha256"],
            "why": "apply generic candidate instructions with the same frozen business, Tool and Host verifier controls",
        }
    elif variable in {
        "bounded_diagnostic_prompt_contract",
        "luna_prompt_evidence_contract",
    }:
        factor = {
            "layer": "prompt",
            "name": variable,
            "before": comparison["baselineWorkflowProfile"],
            "after": comparison["candidateWorkflowProfile"],
            "why": (
                "adapt the weaker model to exact Tool addressing, affected-object semantics, and evidence-backed latency discrimination"
                if variable == "luna_prompt_evidence_contract"
                else "bound retrieval while requiring direct root-cause and competing-diagnosis evidence"
            ),
        }
    else:
        factor = {
            "layer": "tool",
            "name": variable,
            "before": comparison["baselineContextProjection"],
            "after": comparison["candidateContextProjection"],
            "why": "replace long public cache keys with equivalent bounded observation ids",
        }
    return {
        "schemaVersion": "paw.cloudops-cost-optimization-receipt.v1",
        "runId": f"cloudops-cost-optimization:{candidate.get('trialId') or 'unknown'}",
        "status": "completed",
        "decision": comparison["decision"],
        "baselineRunId": baseline.get("trialId"),
        "candidateRunId": candidate.get("trialId"),
        "factor": factor,
        "comparison": comparison,
        "timing": {
            "baselineElapsedMs": dict(baseline.get("signals") or {}).get("elapsedMs"),
            "candidateElapsedMs": dict(candidate.get("signals") or {}).get("elapsedMs"),
            "keepGate": False,
        },
        "projection": candidate.get("contextProjectionSummary"),
        "evidence": {
            "baselineReportSha256": baseline_report_sha256,
            "candidateReportSha256": candidate_report_sha256,
        },
        "claimBoundary": [
            "validation-only source-local candidate",
            "cost verdict uses comparable Provider usage categories, not a Provider bill",
            "elapsed time is recorded but is not a Keep gate",
            "installed and foreground acceptance remain separate",
        ],
    }


def _execution_signals(
    batch_results: list[Mapping[str, object]],
    *,
    started_at_ms: int,
    completed_at_ms: int,
) -> dict[str, object]:
    items = [
        item
        for batch in batch_results
        if isinstance(batch.get("ledger"), Mapping)
        for item in batch["ledger"].get("items") or []
        if isinstance(item, Mapping)
    ]
    first_started = min(
        (int(item.get("startedAtMs") or 0) for item in items if int(item.get("startedAtMs") or 0) > 0),
        default=started_at_ms,
    )
    return {
        "batchCount": len(batch_results),
        "toolCalls": len(items),
        "successfulToolCalls": sum(item.get("ok") is True for item in items),
        "failedToolCalls": sum(item.get("ok") is not True for item in items),
        "readCalls": sum(item.get("operation") == "read" for item in items),
        "listCalls": sum(item.get("operation") == "list" for item in items),
        "searchCalls": sum(item.get("operation") == "search" for item in items),
        "firstToolLatencyMs": max(0, first_started - started_at_ms),
        "elapsedMs": max(0, completed_at_ms - started_at_ms),
    }


def _process_signals(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {"available": False, "authority": "diagnostic"}
    result: dict[str, object] = {
        "available": True,
        "authority": str(value.get("authority") or "diagnostic"),
    }
    for key in ("MC", "EOC", "EE", "processCompleteRate", "meanSteps"):
        raw = value.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            result[key] = float(raw)
    return result


def _batch_latency_ms(result: Mapping[str, object]) -> int:
    ledger = result.get("ledger")
    items = ledger.get("items") if isinstance(ledger, Mapping) else []
    if not isinstance(items, list) or not items:
        return 0
    starts = [int(item.get("startedAtMs") or 0) for item in items if isinstance(item, Mapping)]
    ends = [int(item.get("endedAtMs") or 0) for item in items if isinstance(item, Mapping)]
    return max(0, max(ends, default=0) - min(starts, default=0))


def _build_batch_trace(
    result: Mapping[str, object],
    *,
    trace_id: str,
    trial_id: str,
    artifact: ArtifactRef,
    now_ms: int,
):
    ledger = result.get("ledger")
    items = ledger.get("items") if isinstance(ledger, Mapping) else []
    if not isinstance(items, list):
        items = []
    spans = [
        make_span(
            span_id="span:agent-turn",
            name="agent.turn",
            started_at_ms=now_ms,
            ended_at_ms=now_ms + 1,
            attributes={"action": str(result["batchId"])},
            metrics={"count": int(result["answerCount"])},
        )
    ]
    evidence: list[EvidenceRef] = []
    evidence_keys: set[tuple[str, str]] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            continue
        start = int(item.get("startedAtMs") or now_ms)
        end = max(start, int(item.get("endedAtMs") or start))
        operation = str(item.get("operation") or "unknown")
        spans.append(
            make_span(
                span_id=f"span:tool:{index}",
                name=f"cloudops.{operation}",
                parent_span_id="span:agent-turn",
                started_at_ms=start,
                ended_at_ms=end,
                status="completed" if item.get("ok") is True else "failed",
                attributes={"action": operation},
            )
        )
        summary = item.get("resultSummary")
        args = item.get("args")
        if operation == "read" and item.get("ok") is True and isinstance(summary, Mapping):
            observation_sha = str(summary.get("observationSha256") or "")
            case_id = str(args.get("caseId") or "") if isinstance(args, Mapping) else ""
            if observation_sha and case_id:
                evidence_id = str(
                    summary.get("evidenceId")
                    or f"evidence:cloudops:{observation_sha[:32]}"
                )
                evidence_key = (evidence_id, "tool_observation")
                if evidence_key in evidence_keys:
                    continue
                evidence_keys.add(evidence_key)
                evidence.append(
                    EvidenceRef(
                        evidence_id=evidence_id,
                        source_kind="tool",
                        source_ref=(
                            "fixture://cloudops/"
                            + _sha256(trial_id)[:16]
                            + "/"
                            + _sha256(case_id)[:16]
                            + "/"
                            + observation_sha
                        ),
                        source_lane="cloudops_blind",
                        evidence_stage="tool_observation",
                    )
                )
    return build_trace_envelope(
        trace_id=trace_id,
        source_kind="vertical_agent",
        input_text="",
        input_fingerprint="sha256:" + str(result["promptSha256"]),
        input_normalization=f"cloudops-batch-prompt:{result.get('workflowProfile') or 'baseline-v1'}",
        binding={
            "sessionId": str(result["sessionId"]),
            "turnId": str(result["turnId"]),
            "runId": trial_id,
            "sourceLoopId": str(result["turnId"]),
            "caseId": str(result["batchId"]),
        },
        spans=tuple(spans),
        evidence=tuple(evidence),
        artifacts=(artifact,),
        status="completed",
        created_at_ms=now_ms,
        updated_at_ms=now_ms + max(2, len(spans)),
    )


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-candidate", type=Path, required=True)
    parser.add_argument("--blind-root", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--scorer", type=Path, required=True)
    parser.add_argument("--source-agent-config", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--candidate-prompt-file", type=Path, help="UTF-8 generic candidate instructions; never Host Gold.")
    parser.add_argument("--evaluation-split", choices=("validation", "development"), default="validation")
    parser.add_argument("--provider", default="openai-codex")
    parser.add_argument("--model", default=DEFAULT_AGENT_MODEL_ID)
    parser.add_argument("--thinking", default="max")
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--max-reads-per-case", type=int, default=40)
    parser.add_argument(
        "--workflow-profile",
        choices=(
            "baseline-v1",
            "evidence-search-v1",
            "evidence-search-v2",
            "observation-id-v1",
            "quality-bounded-v3",
            "quality-staged-v4",
            "alert-first-v5",
            "luna-evidence-calibrated-v6",
            "luna-bounded-evidence-v7",
            _LUNA_OWNER_MECHANISM_PROFILE,
        ),
        default="baseline-v1",
    )
    parser.add_argument(
        "--context-projection",
        choices=tuple(sorted(_CONTEXT_PROJECTIONS)),
        default="standard-v1",
        help="Change only the public Tool addressing projected into Agent context.",
    )
    parser.add_argument(
        "--baseline-report",
        type=Path,
        help="Optional matching baseline report used for a fail-closed cost comparison.",
    )
    parser.add_argument(
        "--optimization-output",
        type=Path,
        help="Write a standalone optimization receipt; requires --baseline-report.",
    )
    parser.add_argument("--transport", choices=("spool", "loopback"), default="spool")
    args = parser.parse_args(argv)
    if args.optimization_output is not None and args.baseline_report is None:
        parser.error("--optimization-output requires --baseline-report")

    args.trial_id = _normalize_trial_id(args.trial_id)
    private_root = args.private_root.expanduser().resolve(strict=False)
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_root = (private_root / args.trial_id).resolve(strict=False)
    try:
        run_root.relative_to(private_root)
    except ValueError as exc:
        raise ValueError("CloudOps trial root escapes the private root") from exc
    if run_root.exists():
        raise FileExistsError("CloudOps trial root already exists")
    run_root.mkdir(mode=0o700)
    suite = CloudOpsBlindSuite(args.blind_root)
    gateway = _CloudOpsContextProjectionGateway(
        CloudOpsBenchmarkGateway(
            suite,
            max_reads_per_case=max(1, int(args.max_reads_per_case)),
        ),
        context_projection=str(args.context_projection),
    )
    transport: object
    runtime_wrapper: Path | None = None
    spool_dir: Path | None = None
    if args.transport == "spool":
        from rag_ime.rag_benchmark_agent import RagBenchmarkAgentSpoolGateway

        spool_dir = run_root / "agent" / "tool-spool"
        transport = RagBenchmarkAgentSpoolGateway(gateway, spool_dir=spool_dir)
        runtime_wrapper = ROOT / "scripts" / "rag_agent_spool_runtime_wrapper.mjs"
    else:
        transport = CloudOpsBenchmarkGatewayServer(gateway)
    transport.start()
    service: AgentService | None = None
    try:
        config, runtime_identity = _candidate_runtime_config(
            args.runtime_candidate,
            run_root=run_root,
            source_agent_config=args.source_agent_config,
            provider=args.provider,
            model=args.model,
            tool_gateway_url=transport.tool_gateway_url,
            tool_gateway_token=transport.token,
            runtime_wrapper=runtime_wrapper,
            spool_dir=spool_dir,
        )
        database = run_root / "observability.sqlite"
        service = AgentService(
            db_path=database,
            runtime_config=config,
            project="cloudops-benchmark-agent",
            tool_gateway_url=transport.tool_gateway_url,
            tool_gateway_token=transport.token,
            wake_scheduler_enabled=False,
            background_job_execution_owner=False,
        )
        report = run_cloudops_agent_eval(
            suite=suite,
            gateway=gateway,
            service=service,
            trial_id=args.trial_id,
            gold_path=args.gold,
            score_host_only=lambda gold, answers: invoke_host_scorer(
                args.scorer,
                gold,
                answers,
            ),
            trace_store=service.trace_store,
            eval_store=service.eval_runs,
            sandbox_store=service.sandbox_runs,
            artifact_store=AgentArtifactStore(
                database,
                root=run_root / "agent-artifacts",
            ),
            timeout_seconds=max(30.0, float(args.timeout_seconds)),
            runtime_identity=runtime_identity,
            thinking_level=str(args.thinking),
            workflow_profile=str(args.workflow_profile),
            context_projection=str(args.context_projection),
            candidate_prompt_file=args.candidate_prompt_file,
            evaluation_split=args.evaluation_split,
            scorer_identity=_file_sha256(args.scorer),
        )
        report["hostInvocation"] = _public_host_invocation(
            args,
            transport=str(getattr(transport, "transport", "")),
        )
    except Exception as exc:
        failure = {
            "schemaVersion": "paw.cloudops-agent-eval-failure.v1",
            "trialId": args.trial_id,
            "status": "failed",
            "errorType": type(exc).__name__,
            "errorFingerprint": "sha256:" + _sha256(f"{type(exc).__name__}:{exc}"),
            "hostInvocation": _public_host_invocation(
                args,
                transport=str(getattr(transport, "transport", "")),
            ),
        }
        _write_json_atomic(args.output, failure)
        raise
    finally:
        if service is not None:
            service.close()
        transport.close()
    baseline_report: dict[str, object] | None = None
    if args.baseline_report is not None:
        value = json.loads(args.baseline_report.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("CloudOps baseline report must be a JSON object")
        baseline_report = dict(value)
        report["optimizationComparison"] = _cost_optimization_comparison(
            baseline_report,
            report,
        )
    _write_json_atomic(args.output, report)
    if args.optimization_output is not None and baseline_report is not None:
        _write_json_atomic(
            args.optimization_output,
            _cloudops_optimization_receipt(
                baseline=baseline_report,
                candidate=report,
                baseline_report_sha256=_file_sha256(
                    args.baseline_report.expanduser().resolve(strict=True)
                ),
                candidate_report_sha256=_file_sha256(
                    args.output.expanduser().resolve(strict=True)
                ),
            ),
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _write_json_atomic(path: str | Path, value: Mapping[str, object]) -> None:
    target = Path(path).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(target)


if __name__ == "__main__":
    raise SystemExit(main())
