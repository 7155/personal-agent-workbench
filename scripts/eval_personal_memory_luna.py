#!/usr/bin/env python3
"""Run the unified Atom-first pipeline on a private shadow with real Luna/max."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from rag_ime.deepseek_memory_organizer import ManagedPiMemoryOrganizer
from rag_ime.embeddings import (
    HashingEmbeddingProvider,
    NullEmbeddingProvider,
    embedding_provider_from_env,
)
from rag_ime.activity_timeline_evaluation import load_frozen_activity_timeline
from rag_ime.memory_book_compiler import rollback_memory_book_run
from rag_ime.memory_evidence_admission import rollback_evidence_admissions_for_run
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.owner_memory_curation import OwnerMemoryCurator
from rag_ime.personal_memory_luna_evaluation import (
    PERSONAL_MEMORY_LUNA_EVALUATION_SCHEMA_VERSION,
    PrivateCodexLunaMemoryExecutor,
    atom_first_memory_state_summary,
    build_personal_memory_semantic_evaluation_bundle,
    evaluate_synthetic_personal_memory_rag,
    prepare_private_shadow_schema_view,
    prepare_verified_memory_shadow,
    private_shadow_core,
    redacted_luna_request_summary,
    redacted_owner_run_summary,
    redacted_personal_memory_rag_summary,
    redacted_semantic_curation_summary,
    redacted_synthetic_seed_summary,
    seed_synthetic_personal_memory_rag_cases,
    verify_recovered_memory_shadow,
)


DEFAULT_PRODUCTION_DB = (
    Path.home() / "Library" / "Application Support" / "RagIme" / "rag-ime.sqlite"
)
ATOM_FIRST_EVALUATION_INSTRUCTION = (
    "Use the single Evidence -> Atom -> Book pipeline. Retain durable personal facts, "
    "habits, preferences and principles, plus durable project requirements, decisions, "
    "constraints and facts that remain useful across sessions. Organize every accepted "
    "Atom into one or more stable topic Books. Reject temporary activity, one-off commands, "
    "unresolved questions and implementation status unless the evidence directly states a "
    "durable requirement or decision. Never infer facts from app, time or repetition."
)
_MEMORY_MODELS = (DEFAULT_AGENT_MODEL_ID, "gpt-5.6-luna", "gpt-5.6-sol")
_MEMORY_CONTEXT_PROFILES = ("full-json-v1", "compact-json-v1")
_MEMORY_PROMPT_CONTRACTS = ("standard-v1", "concise-json-v1")
_MEMORY_USAGE_KEYS = (
    "uncachedInputTokens",
    "cachedInputTokens",
    "outputTokens",
)


@dataclass(frozen=True)
class _CodexStructuredRun:
    phase: str
    model: str
    thinking: str
    command: tuple[str, ...]
    elapsed_seconds: float
    exit_code: int
    prompt_sha256: str
    schema_sha256: str
    output_sha256: str
    stdout_sha256: str
    stderr_sha256: str
    output: dict[str, object]
    usage: dict[str, object] = field(default_factory=dict)

    def redacted_receipt(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "model": self.model,
            "thinking": self.thinking,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
            "exitCode": self.exit_code,
            "promptSha256": self.prompt_sha256,
            "schemaSha256": self.schema_sha256,
            "outputSha256": self.output_sha256,
            "stdoutSha256": self.stdout_sha256,
            "stderrSha256": self.stderr_sha256,
            "usage": dict(self.usage),
        }


def _codex_jsonl_usage(stdout: str) -> dict[str, object]:
    completed: list[Mapping[str, object]] = []
    for line in str(stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            completed.append(usage)
    if len(completed) != 1:
        return {"available": False}
    usage = completed[0]
    names = {
        "inputTokens": "input_tokens",
        "cachedInputTokens": "cached_input_tokens",
        "cacheWriteInputTokens": "cache_write_input_tokens",
        "outputTokens": "output_tokens",
    }
    values: dict[str, int] = {}
    for public, source in names.items():
        raw = usage.get(source, 0 if source == "cache_write_input_tokens" else None)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return {"available": False}
        values[public] = raw
    if values["cachedInputTokens"] > values["inputTokens"]:
        return {"available": False}
    return {
        "available": True,
        **values,
        "uncachedInputTokens": values["inputTokens"] - values["cachedInputTokens"],
    }


def _run_codex_structured(
    *,
    prompt: str,
    schema: Mapping[str, object],
    artifact_dir: str | Path,
    phase: str,
    model: str,
    thinking: str,
    timeout_seconds: float = 1_200.0,
    codex_bin: str = "codex",
    command_runner=subprocess.run,
) -> _CodexStructuredRun:
    normalized_phase = " ".join(str(phase).strip().split()).casefold().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", normalized_phase):
        raise ValueError("phase must be a short filesystem-safe identifier")
    if model not in _MEMORY_MODELS or thinking != "max":
        raise ValueError("Memory model identity is unsupported")
    directory = Path(artifact_dir).expanduser()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    directory.chmod(0o700)
    schema_text = json.dumps(
        dict(schema), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    prompt_path = directory / f"{normalized_phase}-prompt.txt"
    schema_path = directory / f"{normalized_phase}-schema.json"
    output_path = directory / f"{normalized_phase}-output.json"
    stdout_path = directory / f"{normalized_phase}-stdout.log"
    stderr_path = directory / f"{normalized_phase}-stderr.log"
    receipt_path = directory / f"{normalized_phase}-receipt.json"
    _write_private_exclusive(prompt_path, prompt)
    _write_private_exclusive(schema_path, schema_text)
    command = (
        codex_bin,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{thinking}"',
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--json",
        "--cd",
        str(directory),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "-",
    )
    started = time.monotonic()
    completed = command_runner(
        list(command),
        input=prompt,
        text=True,
        capture_output=True,
        timeout=max(1.0, float(timeout_seconds)),
        check=False,
    )
    elapsed = time.monotonic() - started
    stdout = str(completed.stdout or "")
    stderr = str(completed.stderr or "")
    _write_private_exclusive(stdout_path, stdout)
    _write_private_exclusive(stderr_path, stderr)
    if int(completed.returncode) != 0:
        raise RuntimeError(
            f"Codex {normalized_phase} exited {completed.returncode}; private logs retained"
        )
    if not output_path.is_file():
        raise RuntimeError(f"Codex {normalized_phase} produced no structured output file")
    output_path.chmod(0o600)
    output_text = output_path.read_text(encoding="utf-8")
    try:
        output = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Codex {normalized_phase} output is not JSON") from exc
    if not isinstance(output, dict):
        raise RuntimeError(f"Codex {normalized_phase} output must be an object")
    run = _CodexStructuredRun(
        phase=normalized_phase,
        model=model,
        thinking=thinking,
        command=command,
        elapsed_seconds=elapsed,
        exit_code=int(completed.returncode),
        prompt_sha256=_text_sha256(prompt),
        schema_sha256=_text_sha256(schema_text),
        output_sha256=_text_sha256(output_text),
        stdout_sha256=_text_sha256(stdout),
        stderr_sha256=_text_sha256(stderr),
        output=dict(output),
        usage=_codex_jsonl_usage(stdout),
    )
    _write_private_exclusive(
        receipt_path,
        json.dumps(run.redacted_receipt(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
    )
    return run


def _load_codex_structured_run(
    artifact_dir: str | Path,
    *,
    phase: str,
    model: str,
    thinking: str,
) -> _CodexStructuredRun:
    normalized_phase = " ".join(str(phase).strip().split()).casefold().replace("_", "-")
    directory = Path(artifact_dir).expanduser().resolve(strict=True)
    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise ValueError("private Codex artifact directory has unsafe permissions")
    output_path = directory / f"{normalized_phase}-output.json"
    receipt_path = directory / f"{normalized_phase}-receipt.json"
    for path in (output_path, receipt_path):
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("private Codex artifact is missing or unsafe")
    output_text = output_path.read_text(encoding="utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    output = json.loads(output_text)
    if not isinstance(receipt, dict) or not isinstance(output, dict):
        raise ValueError("private Codex artifact is invalid")
    if (
        receipt.get("phase") != normalized_phase
        or receipt.get("model") != model
        or receipt.get("thinking") != thinking
        or receipt.get("exitCode") != 0
        or receipt.get("outputSha256") != _text_sha256(output_text)
    ):
        raise ValueError("private Codex artifact identity drifted")
    usage = receipt.get("usage")
    return _CodexStructuredRun(
        phase=normalized_phase,
        model=model,
        thinking=thinking,
        command=(),
        elapsed_seconds=float(receipt.get("elapsedSeconds") or 0.0),
        exit_code=0,
        prompt_sha256=_receipt_sha256(receipt, "promptSha256"),
        schema_sha256=_receipt_sha256(receipt, "schemaSha256"),
        output_sha256=_text_sha256(output_text),
        stdout_sha256=_receipt_sha256(receipt, "stdoutSha256"),
        stderr_sha256=_receipt_sha256(receipt, "stderrSha256"),
        output=dict(output),
        usage=dict(usage) if isinstance(usage, Mapping) else {"available": False},
    )


def _write_private_exclusive(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    path.chmod(0o600)


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _receipt_sha256(receipt: Mapping[str, object], key: str) -> str:
    value = str(receipt.get(key) or "")
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"private Codex receipt has invalid {key}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate the real unified Evidence -> Atom -> Book path on a "
            "verified private shadow. Production SQLite is never opened."
        )
    )
    parser.add_argument("--shadow-db", type=Path,
        help="Verified host recovery shadow; optional only for a clean synthetic fixture evaluation.")
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--public-report", type=Path)
    parser.add_argument("--project", default="wisdom-weasel-rag-ime")
    parser.add_argument("--max-sources", type=int, default=1_000)
    parser.add_argument("--timeout-seconds", type=float, default=1_200.0)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--model", choices=_MEMORY_MODELS, default=DEFAULT_AGENT_MODEL_ID)
    parser.add_argument(
        "--context-profile",
        choices=_MEMORY_CONTEXT_PROFILES,
        default="full-json-v1",
    )
    parser.add_argument(
        "--prompt-contract",
        choices=_MEMORY_PROMPT_CONTRACTS,
        default="standard-v1",
    )
    parser.add_argument(
        "--run-id",
        default="memory-maintenance-luna-max-validation-20260902",
    )
    parser.add_argument(
        "--require-usage",
        action="store_true",
        help="Fail the run when Codex does not emit complete token usage categories.",
    )
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--optimization-output", type=Path)
    parser.add_argument(
        "--model-only-baseline-report",
        type=Path,
        help=(
            "Freeze a passed Sol/max Memory report as the Prompt/context baseline, "
            "then require Luna/max to be the only changed experimental factor."
        ),
    )
    parser.add_argument(
        "--embedding-from-env",
        action="store_true",
        help=(
            "Require the configured semantic embedding provider for projection "
            "and RAG instead of the degraded deterministic hash provider."
        ),
    )
    parser.add_argument("--production-db", type=Path, default=DEFAULT_PRODUCTION_DB)
    parser.add_argument(
        "--semantic-timeline-id",
        default="",
        help=(
            "Run a read-only semantic quality evaluation over one approved "
            "historical Timeline. This never admits legacy rows as Evidence."
        ),
    )
    parser.add_argument(
        "--seed-synthetic-rag-fixture",
        action="store_true",
        help=(
            "Seed public-safe capture-v2 cases into the private working copy, "
            "then require Atom/Book projection, hybrid RAG recall, rollback "
            "removal, and replay recall."
        ),
    )
    parser.add_argument(
        "--clean-synthetic-shadow",
        action="store_true",
        help=(
            "Use a fresh private SQLite for the public-safe synthetic fixture "
            "while still verifying that --shadow-db remains unchanged."
        ),
    )
    return parser


def _create_evaluation_executor(args: argparse.Namespace, artifact_root: Path, *,
                                audit_db_path: Path | None = None,
                                executor_factory: Callable[..., object] | None = None,
                                control: _EvaluationControl | None = None):
    options = dict(audit_db_path=audit_db_path, timeout_seconds=float(args.timeout_seconds),
                   model_id=str(args.model), context_profile=str(args.context_profile),
                   prompt_contract=str(args.prompt_contract))
    if executor_factory is not None:
        if control is not None:
            options.update(cancelled=control.is_cancelled, receipt_observer=control.receipt)
        executor = executor_factory(artifact_root, **options)
        if control is not None:
            control.executor = executor
        if (executor.model_id != str(args.model) or executor.thinking_level != "max"
                or executor.context_profile != str(args.context_profile)
                or executor.prompt_contract != str(args.prompt_contract)):
            raise ValueError("injected Memory executor does not match frozen run controls")
        return executor
    executor = PrivateCodexLunaMemoryExecutor(
        artifact_root, **options, codex_bin=str(args.codex_bin),
        structured_runner=lambda **kwargs: _run_codex_structured(**kwargs, model=str(args.model), thinking="max"),
        structured_loader=lambda directory, *, phase: _load_codex_structured_run(directory, phase=phase, model=str(args.model), thinking="max"),
    )
    if control is not None:
        control.executor = executor
    return executor


class _EvaluationCancelled(Exception):
    pass


class _EvaluationControl:
    def __init__(self, progress, cancelled):
        self.progress = progress
        self.cancelled = cancelled or (lambda: False)
        self.cancel_seen = False
        self.current_stage = "preparation"
        self.executor = None
        self.private_root: Path | None = None
        self.details: dict[str, object] = {}

    def is_cancelled(self) -> bool:
        self.cancel_seen = self.cancel_seen or bool(self.cancelled())
        return self.cancel_seen

    def emit(self, event: Mapping[str, object]) -> None:
        if self.progress is not None:
            self.progress(dict(event))

    def receipt(self, receipt: Mapping[str, object]) -> None:
        self.emit({"type": "model_receipt", "receipt": dict(receipt)})

    def stage(self, name: str, *, cleanup: bool = False) -> None:
        self.current_stage = name
        self.emit({"type": "stage", "stage": name, "necessaryCleanup": cleanup})
        if self.is_cancelled() and not cleanup:
            raise _EvaluationCancelled()


def run_evaluation(args: argparse.Namespace, *, executor_factory: Callable[..., object] | None = None,
                   progress: Callable[[Mapping[str, object]], None] | None = None,
                   cancelled: Callable[[], bool] | None = None) -> dict[str, object]:
    """Run one private evaluation, without changing process umask or stdout.

    Progress is private to the embedding owner: model_receipt events contain
    real Pi request/Session/turn bindings. Pi can recover a model request; this
    function does not resume an interrupted shadow/rollback/replay pipeline.
    """
    control = _EvaluationControl(progress, cancelled)
    try:
        summary = _run_evaluation_body(args, executor_factory=executor_factory, control=control)
    except Exception as exc:
        if isinstance(exc, OSError) or getattr(exc, "interrupted", False):
            # A stop request cannot prove a failed transport or cleanup settled.
            raise
        if not isinstance(exc, _EvaluationCancelled) and not control.is_cancelled():
            raise
        summary = {"schemaVersion": PERSONAL_MEMORY_LUNA_EVALUATION_SCHEMA_VERSION,
            "status": "cancelled", "passed": False, "runId": str(args.run_id),
            "model": str(args.model), "thinking": "max", "cancelledAt": control.current_stage,
            "productionMutationPerformed": False, "pipelineResumeSupported": False, **control.details}
        if not isinstance(exc, _EvaluationCancelled):
            summary["interruptedErrorClass"] = type(exc).__name__
        if control.executor is not None:
            summary["transport"] = control.executor.transport
            summary["modelRequests"] = redacted_luna_request_summary(control.executor.receipts)
        if control.private_root is not None:
            _write_private_summary(control.private_root / "evaluation-summary.json", summary)
    finally:
        try:
            if control.executor is not None:
                control.executor.close()
        finally:
            # Dependencies create SQLite/temp artifacts under this mode-0700
            # root. Explicitly secure final files without process-global umask.
            if control.private_root is not None:
                for path in control.private_root.rglob("*"):
                    if not path.is_symlink():
                        path.chmod(0o700 if path.is_dir() else 0o600)
    control.emit({"type": "stage", "stage": "cancelled" if summary["status"] == "cancelled" else "completed"})
    return summary


def main(argv: list[str] | None = None, *, executor_factory: Callable[..., object] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    try:
        summary = run_evaluation(args, executor_factory=executor_factory)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    common = ("passed", "status", "model", "thinking", "modelRequests", "sourceShadowUnchanged", "productionMutationPerformed")
    fields = (("recovery", "curation", "legalEvidenceAdmissionPerformed")
        if summary.get("evaluationKind") == "historical_semantic_quality_only" else
        ("firstRun", "appliedState", "appliedRag", "rollback", "rollbackRag", "replay", "replayRag", "modelOnlyComparison"))
    print(json.dumps({key: summary.get(key) for key in (*common, *fields)}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary.get("passed") else 1


def _run_evaluation_body(args: argparse.Namespace, *, executor_factory, control: _EvaluationControl) -> dict[str, object]:
    evaluation_started = time.monotonic()
    synthetic_only = args.shadow_db is None and bool(args.clean_synthetic_shadow) and bool(args.seed_synthetic_rag_fixture)
    if args.shadow_db is None and (not synthetic_only or str(args.semantic_timeline_id).strip()):
        raise ValueError("--shadow-db is required outside a clean synthetic fixture evaluation")
    source_path = args.shadow_db.expanduser().resolve(strict=True) if args.shadow_db is not None else None
    production_path = args.production_db.expanduser().resolve(strict=False)
    if source_path is not None and (source_path == production_path or (
            production_path.exists() and source_path.samefile(production_path))):
        raise ValueError("the production database cannot be a Memory evaluation source")
    if executor_factory is not None and any((args.baseline_report, args.model_only_baseline_report, args.optimization_output)):
        raise ValueError("injected Memory transport requires a separate evaluation; legacy comparison expects codex CLI receipts")
    if args.optimization_output is not None and args.baseline_report is None:
        raise ValueError("--optimization-output requires --baseline-report")
    if args.model_only_baseline_report is not None and args.baseline_report is not None:
        raise ValueError(
            "--model-only-baseline-report cannot be combined with --baseline-report"
        )
    model_only_baseline: dict[str, object] | None = None
    model_only_baseline_sha256 = ""
    if args.model_only_baseline_report is not None:
        baseline_path = args.model_only_baseline_report.expanduser().resolve(strict=True)
        value = json.loads(baseline_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Memory model-only baseline report must be a JSON object")
        model_only_baseline = dict(value)
        preflight = _memory_model_only_preflight(
            model_only_baseline,
            model=str(args.model),
            context_profile=str(args.context_profile),
            prompt_contract=str(args.prompt_contract),
        )
        if not bool(preflight.get("passed")):
            failed = ", ".join(
                str(name)
                for name, passed in dict(preflight.get("checks") or {}).items()
                if passed is not True
            )
            raise ValueError(
                "invalid Memory model-only baseline or candidate controls: " + failed
            )
        model_only_baseline_sha256 = _file_sha256(baseline_path)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", str(args.run_id)) is None:
        raise ValueError("--run-id must be a bounded public identifier")
    private_root = args.private_dir.expanduser().resolve(strict=False)
    if private_root.is_relative_to(ROOT):
        raise ValueError("--private-dir must be outside the Git worktree")
    if private_root.exists():
        if not private_root.is_dir() or private_root.stat().st_mode & 0o077:
            raise ValueError("existing --private-dir must be a mode-0700 directory")
    else:
        private_root.mkdir(parents=True, mode=0o700)
        private_root.chmod(0o700)
    control.private_root = private_root
    control.stage("preparation")

    if str(args.semantic_timeline_id).strip():
        return _run_semantic_quality(args, private_root, executor_factory=executor_factory, control=control)

    if bool(args.clean_synthetic_shadow) and not bool(args.seed_synthetic_rag_fixture):
        raise ValueError("--clean-synthetic-shadow requires --seed-synthetic-rag-fixture")
    embedding_provider = (
        embedding_provider_from_env()
        if bool(args.embedding_from_env)
        else None
    )
    if isinstance(embedding_provider, NullEmbeddingProvider):
        raise ValueError("--embedding-from-env resolved to a disabled provider")
    rag_core = None
    if bool(args.clean_synthetic_shadow):
        source_identity = _file_identity(source_path) if source_path is not None else None
        verification = verify_recovered_memory_shadow(source_path) if source_path is not None else {"syntheticOnly": True}
        working_db = private_root / "clean-synthetic-evaluation.sqlite"
        if working_db.exists():
            raise ValueError("clean synthetic working database already exists")
        rag_core = LocalSqliteCoreClient(
            working_db,
            # Keep the default synthetic fixture fully local and deterministic
            # while still exercising the dense lane. A configured provider is
            # opt-in via --embedding-from-env and is never silently substituted.
            embedding_provider=embedding_provider or HashingEmbeddingProvider(),
        )
        rag_core.initialize()
        prepared = {
            "workingDb": working_db,
            "resumed": False,
            "sourceFileSha256": _file_sha256(source_path) if source_path is not None else None,
            "sourceIdentity": source_identity,
            "verification": verification,
        }
    else:
        prepared = prepare_verified_memory_shadow(
            args.shadow_db,
            private_root=private_root,
            production_db=args.production_db,
        )
        working_db = Path(prepared["workingDb"])
    schema_view = prepare_private_shadow_schema_view(private_root)
    synthetic_cases: tuple[dict[str, object], ...] = ()
    synthetic_seed: dict[str, object] = {}
    if bool(args.seed_synthetic_rag_fixture):
        if rag_core is None:
            rag_core = private_shadow_core(
                working_db,
                embedding_provider=embedding_provider,
            )
        synthetic_cases = seed_synthetic_personal_memory_rag_cases(
            rag_core,
            project=str(args.project),
        )
        synthetic_seed = redacted_synthetic_seed_summary(synthetic_cases)
    baseline = atom_first_memory_state_summary(working_db)
    replay_baseline_snapshot: Path | None = None
    replay_baseline_sha256 = ""
    if bool(args.clean_synthetic_shadow):
        # A rollback is a semantic operation, but the projection layer may
        # retain derived topic rows that are intentionally not part of the
        # logical Atom summary.  Capture the exact pre-run SQLite state so a
        # replay can prove the same frozen input rather than accidentally
        # measuring a second run against post-rollback projection residue.
        replay_baseline_snapshot = private_root / "pre-run-baseline.sqlite"
        _snapshot_sqlite(working_db, replay_baseline_snapshot)
        replay_baseline_sha256 = _file_sha256(replay_baseline_snapshot)
    control.stage("curation")
    executor = _create_evaluation_executor(args, private_root / "codex", audit_db_path=working_db, executor_factory=executor_factory, control=control)
    organizer = ManagedPiMemoryOrganizer(executor)
    curator = OwnerMemoryCurator(
        working_db,
        organizer=organizer,
        project=str(args.project),
        initial_settle_ms=0,
        auto_apply=True,
        include_agent_dialogue=False,
        embedding_provider=(rag_core.embedding_provider if rag_core is not None else None),
        max_sources=max(1, min(1_500, int(args.max_sources))),
    )

    # The recovery builder already applied and verified every required
    # migration in an isolated migration view. Do not initialize the same
    # shadow again here: the prepared-shadow receipt and required-table check
    # above are the explicit replacement gate for this private evaluation.
    first = curator.run_due(
        manual=True,
        owner_kind="user",
        owner_id="default",
        instruction=ATOM_FIRST_EVALUATION_INSTRUCTION,
    )
    # Applied shadow changes still need local rollback after cancellation.
    control.stage("curation_completed", cleanup=True)
    first_state = atom_first_memory_state_summary(working_db)
    first_result = _first_result(first)
    first_request_count = len(executor.receipts)
    run_id = str(first_result.get("runId") or "")
    first_rag_full: dict[str, object] = {}
    first_rag: dict[str, object] = {}
    seeded_atom_ids: list[str] = []
    if (
        rag_core is not None
        and bool(first.get("ok"))
        and str(first_result.get("runStatus") or "") == "applied"
        and not control.is_cancelled()
    ):
        first_rag_full = evaluate_synthetic_personal_memory_rag(
            rag_core,
            synthetic_cases,
            project=str(args.project),
            migrations_dir=schema_view,
        )
        seeded_atom_ids = [
            str(value)
            for value in first_rag_full.get("atomIds") or []
            if str(value)
        ]
        first_rag = redacted_personal_memory_rag_summary(first_rag_full)
    rollback: dict[str, object] = {
        "attempted": False,
        "ok": False,
        "kind": "none",
        "restoredBaseline": False,
    }
    replay: dict[str, object] = {
        "attempted": False,
        "ok": True,
        "reusedModelRequests": False,
        "restoredAppliedState": False,
    }
    second: dict[str, object] = {}
    second_state: dict[str, object] = {}
    rollback_rag: dict[str, object] = {}
    replay_rag: dict[str, object] = {}
    control.details.update(rollback=rollback, replay=replay)

    if bool(first.get("ok")) and run_id:
        control.stage("rollback", cleanup=True)
        rollback["attempted"] = True
        with closing(sqlite3.connect(working_db)) as conn:
            conn.row_factory = sqlite3.Row
            if str(first_result.get("runStatus") or "") == "applied":
                rolled = rollback_memory_book_run(conn, run_id=run_id)
                rollback["kind"] = "memory_book_run"
                rollback["runStatus"] = str(rolled.get("status") or "")
            else:
                rolled = rollback_evidence_admissions_for_run(
                    conn,
                    run_id,
                    created_at_ms=int(time.time() * 1_000),
                )
                rollback["kind"] = "evidence_admission_run"
                rollback["guardedCount"] = len(rolled.get("guardedEvidenceIds") or [])
                rollback["restoredEvidenceCount"] = len(
                    rolled.get("restoredEvidenceIds") or []
                )
            conn.commit()
        rolled_back_state = atom_first_memory_state_summary(working_db)
        rollback["restoredBaseline"] = (
            rolled_back_state["logicalStateSha256"]
            == baseline["logicalStateSha256"]
        )
        if rag_core is not None and seeded_atom_ids and not control.is_cancelled():
            rollback_rag_full = evaluate_synthetic_personal_memory_rag(
                rag_core,
                synthetic_cases,
                project=str(args.project),
                expected_atom_ids=seeded_atom_ids,
                expect_present=False,
                migrations_dir=schema_view,
            )
            rollback_rag = redacted_personal_memory_rag_summary(rollback_rag_full)
            rollback["ragCleared"] = bool(rollback_rag_full.get("passed"))
        rollback["ok"] = bool(rollback["restoredBaseline"]) and (
            rag_core is None or bool(rollback.get("ragCleared"))
        )

        if rollback["ok"] and rollback["kind"] == "memory_book_run" and not control.is_cancelled():
            control.stage("replay")
            replay["attempted"] = True
            if replay_baseline_snapshot is not None:
                replay["baselineRestoredForReplay"] = _restore_sqlite_snapshot(
                    replay_baseline_snapshot,
                    working_db,
                )
                if not replay["baselineRestoredForReplay"]:
                    replay["ok"] = False
                    replay["error"] = "private replay baseline restore failed"
            else:
                replay["baselineRestoredForReplay"] = True
            receipt_count_before = len(executor.receipts)
            if replay.get("baselineRestoredForReplay"):
                control.stage("replay_curation")
                second = curator.run_due(
                    manual=True,
                    owner_kind="user",
                    owner_id="default",
                    instruction=ATOM_FIRST_EVALUATION_INSTRUCTION,
                    current_ms=int(time.time() * 1_000) + 120_000,
                )
                control.stage("replay_completed", cleanup=True)
                second_state = atom_first_memory_state_summary(working_db)
                second_result = _first_result(second)
                if control.is_cancelled() and second.get("ok") and second_result.get("runStatus") == "applied" and second_result.get("runId"):
                    control.stage("replay_rollback", cleanup=True)
                    with closing(sqlite3.connect(working_db)) as conn:
                        conn.row_factory = sqlite3.Row
                        rolled = rollback_memory_book_run(conn, run_id=str(second_result["runId"]))
                        conn.commit()
                    replay["cancelledCleanup"] = {
                        "attempted": True, "runStatus": str(rolled.get("status") or ""),
                        "restoredBaseline": atom_first_memory_state_summary(working_db)["logicalStateSha256"] == baseline["logicalStateSha256"],
                    }
                if rag_core is not None and not control.is_cancelled():
                    replay_rag_full = evaluate_synthetic_personal_memory_rag(
                        rag_core,
                        synthetic_cases,
                        project=str(args.project),
                        migrations_dir=schema_view,
                    )
                    replay_rag = redacted_personal_memory_rag_summary(replay_rag_full)
                    replay["ragRestored"] = bool(replay_rag_full.get("passed"))
                    replay["ragAtomSetStable"] = (
                        str(replay_rag_full.get("discoveredAtomIdsSha256") or "")
                        == str(first_rag_full.get("discoveredAtomIdsSha256") or "")
                    )
                replay_receipts = executor.receipts[receipt_count_before:]
                replay["reusedModelRequests"] = bool(replay_receipts) and all(
                    bool(item.get("resumed")) for item in replay_receipts
                )
                replay["restoredAppliedState"] = (
                    second_state["logicalStateSha256"]
                    == first_state["logicalStateSha256"]
                )
            replay["ok"] = bool(replay.get("ok", True)) and (
                bool(second.get("ok"))
                and bool(replay["reusedModelRequests"])
                and bool(replay["restoredAppliedState"])
                and (
                    rag_core is None
                    or (
                        bool(replay.get("ragRestored"))
                        and bool(replay.get("ragAtomSetStable"))
                    )
                )
            )

    source_unchanged = _file_identity(source_path) == prepared["sourceIdentity"] if source_path is not None else None
    requests = redacted_luna_request_summary(executor.receipts)
    phases = [str(item.get("phase") or "") for item in requests]
    first_ok = bool(first.get("ok"))
    lineage_ok = bool(
        first_state.get("allGovernedCurrentAtomsHaveLegalLineage")
    )
    book_ok = bool(dict(first_state.get("bookProjection") or {}).get("inSync"))
    first_phases = phases[:first_request_count]
    first_requests = requests[:first_request_count]
    model_ok = (
        bool(first_requests)
        and first_phases[0] == "atom-first-curation"
        and first_phases[-1] == "atom-first-verifier"
        and all(
            phase in {
                "atom-first-curation",
                "atom-first-repair",
                "atom-first-verifier",
            }
            for phase in first_phases
        )
        and bool(first_requests[-1].get("isolated"))
        and all(item.get("model") == str(args.model) for item in first_requests)
        and all(item.get("thinking") == "max" for item in first_requests)
    )
    aggregate_usage = _aggregate_model_usage(requests)
    usage_ok = aggregate_usage.get("available") is True
    passed = (
        not control.is_cancelled()
        and first_ok
        and lineage_ok
        and book_ok
        and model_ok
        and (usage_ok or not bool(args.require_usage))
        and bool(rollback.get("ok"))
        and (not replay["attempted"] or bool(replay["ok"]))
        and (synthetic_only or source_unchanged)
        and (
            not bool(args.seed_synthetic_rag_fixture)
            or (
                bool(synthetic_seed.get("allStored"))
                and bool(synthetic_seed.get("allCandidateEvidence"))
                and bool(first_rag.get("passed"))
            )
        )
    )
    summary: dict[str, object] = {
        "schemaVersion": PERSONAL_MEMORY_LUNA_EVALUATION_SCHEMA_VERSION,
        "status": "cancelled" if control.is_cancelled() else ("pass" if passed else "iterate"),
        "pipelineResumeSupported": False,
        "cancelledAt": control.current_stage if control.is_cancelled() else None,
        "passed": passed,
        "runId": str(args.run_id),
        "model": str(args.model),
        "provider": "openai-codex",
        "thinking": "max",
        "transport": executor.transport,
        "contextProfile": str(args.context_profile),
        "promptContract": str(args.prompt_contract),
        "gatewayInstalledAcceptance": False,
        "productionDatabaseOpened": False,
        "productionMutationPerformed": False,
        "sourceShadowEvaluated": source_path is not None,
        "sourceShadowUnchanged": source_unchanged,
        "syntheticRagFixtureEnabled": bool(args.seed_synthetic_rag_fixture),
        "syntheticSeed": synthetic_seed,
        "shadow": {
            "resumed": bool(prepared.get("resumed")),
            "sourceFileSha256": prepared["sourceFileSha256"],
            "verification": prepared["verification"],
        },
        "firstRun": redacted_owner_run_summary(first),
        "baselineState": baseline,
        "appliedState": first_state,
        "appliedRag": first_rag,
        "rollback": rollback,
        "rollbackRag": rollback_rag,
        "replay": replay,
        "replayBaseline": {
            "captured": replay_baseline_snapshot is not None,
            "sha256": replay_baseline_sha256,
        },
        "replayRun": redacted_owner_run_summary(second) if second else {},
        "replayState": second_state,
        "replayRag": replay_rag,
        "modelRequests": requests,
        "usage": aggregate_usage,
        "timing": {
            "evaluationElapsedMs": round((time.monotonic() - evaluation_started) * 1_000),
            "modelElapsedMs": round(
                sum(
                    float(item.get("elapsedSeconds") or 0.0)
                    for item in requests
                    if item.get("resumed") is not True
                )
                * 1_000
            ),
            "keepGate": False,
        },
        "privateArtifactDirectorySha256": _sha256(str(private_root)),
    }
    if model_only_baseline is not None:
        model_only_comparison = _memory_model_only_comparison(
            model_only_baseline,
            _public_report_payload(summary),
            baseline_report_sha256=model_only_baseline_sha256,
        )
        summary["modelOnlyComparison"] = model_only_comparison
        passed = bool(passed and model_only_comparison.get("singleFactorGatePassed"))
        summary["passed"] = passed
        summary["status"] = "cancelled" if control.is_cancelled() else ("pass" if passed else "iterate")
    baseline_report: dict[str, object] | None = None
    if args.baseline_report is not None:
        value = json.loads(
            args.baseline_report.expanduser().resolve(strict=True).read_text(encoding="utf-8")
        )
        if not isinstance(value, dict):
            raise ValueError("Memory baseline report must be a JSON object")
        baseline_report = dict(value)
        summary["optimizationComparison"] = _memory_cost_optimization_comparison(
            baseline_report,
            _public_report_payload(summary),
        )
    _write_private_summary(private_root / "evaluation-summary.json", summary)
    if args.public_report is not None:
        _write_public_report(args.public_report.expanduser(), summary)
        if args.optimization_output is not None and baseline_report is not None:
            candidate_report = _public_report_payload(summary)
            optimization_path = args.optimization_output.expanduser()
            if optimization_path.exists():
                raise ValueError("Memory optimization output already exists")
            optimization_path.parent.mkdir(parents=True, exist_ok=True)
            _write_private_summary(
                optimization_path,
                _memory_optimization_receipt(
                    baseline=baseline_report,
                    candidate=candidate_report,
                    baseline_report_sha256=_file_sha256(
                        args.baseline_report.expanduser().resolve(strict=True)
                    ),
                    candidate_report_sha256=_file_sha256(
                        args.public_report.expanduser().resolve(strict=True)
                    ),
                ),
            )
    return summary


def _run_semantic_quality(args: argparse.Namespace, private_root: Path, *, executor_factory: Callable[..., object] | None = None, control: _EvaluationControl) -> dict[str, object]:
    """Use real Luna to judge recovered history without changing its legal state."""

    source_db = args.shadow_db.expanduser().resolve(strict=True)
    source_before = _file_identity(source_db)
    verification = verify_recovered_memory_shadow(source_db)
    snapshot = load_frozen_activity_timeline(
        source_db,
        timeline_id=str(args.semantic_timeline_id),
        require_approved=True,
    )
    bundle, recovery = build_personal_memory_semantic_evaluation_bundle(snapshot)
    frozen_sha256 = str(recovery["semanticBundleSha256"])
    control.stage("semantic_curation")
    executor = _create_evaluation_executor(args, private_root / "luna", executor_factory=executor_factory, control=control)
    organizer = ManagedPiMemoryOrganizer(executor)
    run_id = f"semantic-evaluation:{frozen_sha256[:32]}"
    try:
        executor.begin_run(run_id, frozen_input_sha256=frozen_sha256)
        result = organizer.curate_owner_memory(
            bundle=bundle,
            project=snapshot.project,
            owner_kind="user",
            owner_id="default",
            instruction=(
                "This is historical semantic quality evaluation only. Classify "
                "durable cross-project personal information conservatively. Project "
                "requirements, implementation details, tasks, documents, questions, "
                "temporary activity, and ambiguous reconstructed text are not personal "
                "Memory. Never treat evaluation-only boundaries as legal admission."
            ),
        )
        executor.finish_run()
    except BaseException as exc:
        executor.fail_run(exc)
        raise
    # The embedding body closes the executor on every exit.
    control.stage("semantic_validation")

    expected_refs = [
        str(item.get("sourceRef") or "")
        for item in bundle.get("inputs") or []
        if isinstance(item, Mapping)
    ]
    curation = redacted_semantic_curation_summary(
        result,
        expected_source_refs=expected_refs,
    )
    requests = redacted_luna_request_summary(executor.receipts)
    phases = [str(item.get("phase") or "") for item in requests]
    source_unchanged = _file_identity(source_db) == source_before
    atom_phase_optional = int(curation.get("memoryAtomCount") or 0) == 0
    valid_phase_sequence = phases == [
        "evidence-adjudication",
        "atom-adjudication",
        "independent-verifier",
    ] or (
        atom_phase_optional
        and phases == ["evidence-adjudication", "independent-verifier"]
    )
    model_ok = (
        valid_phase_sequence
        and all(item.get("model") == executor.model_id for item in requests)
        and all(item.get("thinking") == "max" for item in requests)
        and bool(requests[-1].get("isolated"))
    )
    passed = (
        source_unchanged
        and model_ok
        and bool(curation.get("allSourceRefsCoveredExactlyOnce"))
        and bool(curation.get("independentlyVerified"))
        and recovery.get("legalEvidenceAdmissionAllowed") is False
    )
    summary: dict[str, object] = {
        "schemaVersion": PERSONAL_MEMORY_LUNA_EVALUATION_SCHEMA_VERSION,
        "evaluationKind": "historical_semantic_quality_only",
        "pipelineResumeSupported": False,
        "status": "pass" if passed else "iterate",
        "passed": passed,
        "model": executor.model_id,
        "thinking": "max",
        "transport": executor.transport,
        "semanticEvaluationOnly": True,
        "legalEvidenceAdmissionPerformed": False,
        "productionDatabaseOpened": False,
        "productionMutationPerformed": False,
        "sourceShadowUnchanged": source_unchanged,
        "shadowVerification": verification,
        "recovery": recovery,
        "curation": curation,
        "atomPhaseSkippedBecauseNoEligiblePersonalEvidence": (
            "atom-adjudication" not in phases
        ),
        "modelRequests": requests,
        "privateArtifactDirectorySha256": _sha256(str(private_root)),
    }
    _write_private_summary(private_root / "semantic-evaluation-summary.json", summary)
    if args.public_report is not None:
        _write_semantic_public_report(args.public_report.expanduser(), summary)
    return summary


def _first_result(report: Mapping[str, object]) -> dict[str, object]:
    values = report.get("results")
    if not isinstance(values, list) or not values or not isinstance(values[0], Mapping):
        return {}
    return dict(values[0])


def _file_identity(path: Path) -> dict[str, int]:
    value = path.stat()
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "mtimeNs": int(value.st_mtime_ns),
    }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    """Create a consistent private SQLite snapshot without copying a live WAL."""

    if destination.exists():
        raise ValueError(f"SQLite snapshot already exists: {destination.name}")
    temporary = destination.with_name("." + destination.name + ".tmp")
    for sidecar in (
        temporary.with_name(temporary.name + "-wal"),
        temporary.with_name(temporary.name + "-shm"),
    ):
        if sidecar.exists():
            sidecar.unlink()
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as source_conn:
        with closing(sqlite3.connect(temporary)) as destination_conn:
            source_conn.backup(destination_conn)
            destination_conn.commit()
    temporary.chmod(0o600)
    os.replace(temporary, destination)
    destination.chmod(0o600)


def _restore_sqlite_snapshot(snapshot: Path, target: Path) -> bool:
    """Restore a private SQLite snapshot atomically and verify its bytes."""

    temporary = target.with_name("." + target.name + ".replay.tmp")
    for sidecar in (
        temporary.with_name(temporary.name + "-wal"),
        temporary.with_name(temporary.name + "-shm"),
    ):
        if sidecar.exists():
            sidecar.unlink()
    with closing(sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)) as source_conn:
        with closing(sqlite3.connect(temporary)) as destination_conn:
            source_conn.backup(destination_conn)
            destination_conn.commit()
    temporary.chmod(0o600)
    for sidecar in (
        target.with_name(target.name + "-wal"),
        target.with_name(target.name + "-shm"),
    ):
        if sidecar.exists():
            sidecar.unlink()
    os.replace(temporary, target)
    target.chmod(0o600)
    return _file_sha256(snapshot) == _file_sha256(target)


def _aggregate_model_usage(
    requests: list[dict[str, object]],
) -> dict[str, object]:
    fresh = [item for item in requests if item.get("resumed") is not True]
    if not fresh:
        return {"available": False}
    totals = {
        "inputTokens": 0,
        "uncachedInputTokens": 0,
        "cachedInputTokens": 0,
        "cacheWriteInputTokens": 0,
        "outputTokens": 0,
    }
    for item in fresh:
        usage = item.get("usage")
        if not isinstance(usage, Mapping) or usage.get("available") is not True:
            return {"available": False}
        for key in totals:
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return {"available": False}
            totals[key] += value
    return {"available": True, "providerCallCount": len(fresh), **totals}


def _memory_quality_gate(report: Mapping[str, object]) -> dict[str, object]:
    metrics = report.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    fixture = metrics.get("fixture")
    fixture = fixture if isinstance(fixture, Mapping) else {}
    curation = metrics.get("curation")
    curation = curation if isinstance(curation, Mapping) else {}
    retrieval = metrics.get("retrieval")
    retrieval = retrieval if isinstance(retrieval, Mapping) else {}
    recovery = metrics.get("recovery")
    recovery = recovery if isinstance(recovery, Mapping) else {}
    hard = report.get("hardGates")
    hard = hard if isinstance(hard, Mapping) else {}
    current_atoms = curation.get("currentAtomCount")
    checks = {
        "status": report.get("status") == "passed",
        "caseCount": fixture.get("caseCount") == 5,
        "durableCaseCount": fixture.get("durableCaseCount") == 4,
        "nonMemoryCaseCount": fixture.get("nonMemoryCaseCount") == 1,
        "allStored": fixture.get("allStored") is True,
        "allCandidateEvidence": fixture.get("allCandidateEvidence") is True,
        "curation": curation.get("ok") is True,
        "sourceCount": curation.get("sourceCount") == 5,
        "modelDecisionCount": curation.get("modelDecisionCount") == 5,
        "currentAtoms": isinstance(current_atoms, int)
        and not isinstance(current_atoms, bool)
        and current_atoms > 0,
        "governedAtoms": curation.get("governedCurrentAtomCount") == current_atoms,
        "legalLineage": curation.get("legalLineageCurrentAtomCount") == current_atoms,
        "bookProjection": curation.get("bookProjectionInSync") is True,
        "retrievalCases": retrieval.get("caseCount") == 5,
        "durableRetrievalCases": retrieval.get("durableCaseCount") == 4,
        "allRetrievalCases": retrieval.get("passed") is True
        and retrieval.get("allCasesPassed") is True,
        "vectorCoverage": retrieval.get("vectorCoverage") == 1.0,
        "rollback": recovery.get("rollbackPassed") is True,
        "replay": recovery.get("replayPassed") is True,
        "replayRag": recovery.get("replayRagPassed") is True,
        "replayAtomSet": recovery.get("replayAtomSetStable") is True,
        "privateShadow": hard.get("privateShadow") is True,
        "sourceShadowUnchanged": hard.get("sourceShadowUnchanged") is True,
        "rollbackVerified": hard.get("rollbackVerified") is True,
        "replayVerified": hard.get("replayVerified") is True,
        "productionDatabaseClosed": hard.get("productionDatabaseOpened") is False,
        "productionUnchanged": hard.get("productionMutationPerformed") is False,
    }
    return {"passed": all(checks.values()), "checks": checks}


def _fresh_memory_model_requests(
    report: Mapping[str, object],
) -> list[dict[str, object]]:
    return [
        dict(item)
        for item in report.get("modelRequests") or []
        if isinstance(item, Mapping) and item.get("resumed") is not True
    ]


def _memory_model_contract_snapshot(report: Mapping[str, object]) -> dict[str, object]:
    requests = _fresh_memory_model_requests(report)
    curation = next(
        (item for item in requests if item.get("phase") == "atom-first-curation"),
        {},
    )
    verifier = next(
        (
            item
            for item in reversed(requests)
            if item.get("phase") == "atom-first-verifier"
        ),
        {},
    )
    evidence = report.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    frozen_input = str(curation.get("frozenInputSha256") or "")
    snapshot: dict[str, object] = {
        "provider": str(report.get("provider") or ""),
        "thinking": str(report.get("thinking") or ""),
        "transport": str(report.get("transport") or ""),
        "evaluationScope": str(report.get("evaluationScope") or ""),
        "contextProfile": str(report.get("contextProfile") or ""),
        "promptContract": str(report.get("promptContract") or ""),
        "syntheticFixtureSha256": str(evidence.get("syntheticFixtureSha256") or ""),
        "frozenInputSha256": frozen_input,
        # The verifier packet contains the model's curation output, so it is an
        # observed consequence rather than a frozen experimental input.
        "curationSemanticPacketSha256": str(
            curation.get("semanticPacketSha256") or ""
        ),
        "curationSourcePacketChars": curation.get("sourcePacketChars"),
        "curationProjectedPacketChars": curation.get("projectedPacketChars"),
        "curationSchemaSha256": str(curation.get("schemaSha256") or ""),
        "verifierSchemaSha256": str(verifier.get("schemaSha256") or ""),
        "allRequestsUseFrozenInput": bool(requests)
        and all(item.get("frozenInputSha256") == frozen_input for item in requests),
        "allRequestsUseContextProfile": bool(requests)
        and all(
            item.get("contextProfile") == str(report.get("contextProfile") or "")
            for item in requests
        ),
    }
    hash_values = (
        snapshot["syntheticFixtureSha256"],
        snapshot["frozenInputSha256"],
        snapshot["curationSemanticPacketSha256"],
        snapshot["curationSchemaSha256"],
        snapshot["verifierSchemaSha256"],
    )
    packet_sizes = (
        snapshot["curationSourcePacketChars"],
        snapshot["curationProjectedPacketChars"],
    )
    snapshot["complete"] = bool(
        all(re.fullmatch(r"[0-9a-f]{64}", str(value or "")) for value in hash_values)
        and all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in packet_sizes
        )
        and snapshot["allRequestsUseFrozenInput"]
        and snapshot["allRequestsUseContextProfile"]
        and curation
        and verifier
    )
    fingerprint_payload = {
        key: value
        for key, value in snapshot.items()
        if key not in {"complete"}
    }
    snapshot["contractFingerprintSha256"] = _sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return snapshot


def _memory_semantic_quality_gate(report: Mapping[str, object]) -> dict[str, object]:
    quality = _memory_quality_gate(report)
    checks = dict(quality["checks"])
    semantic_names = (
        "caseCount",
        "durableCaseCount",
        "nonMemoryCaseCount",
        "curation",
        "sourceCount",
        "modelDecisionCount",
        "currentAtoms",
        "governedAtoms",
        "legalLineage",
        "bookProjection",
        "retrievalCases",
        "durableRetrievalCases",
        "allRetrievalCases",
    )
    selected = {name: checks[name] for name in semantic_names}
    return {"passed": all(selected.values()), "checks": selected}


def _memory_recovery_gate(report: Mapping[str, object]) -> dict[str, object]:
    quality = _memory_quality_gate(report)
    checks = dict(quality["checks"])
    recovery_names = (
        "allStored",
        "allCandidateEvidence",
        "vectorCoverage",
        "rollback",
        "replay",
        "replayRag",
        "replayAtomSet",
        "privateShadow",
        "sourceShadowUnchanged",
        "rollbackVerified",
        "replayVerified",
        "productionDatabaseClosed",
        "productionUnchanged",
    )
    selected = {name: checks[name] for name in recovery_names}
    return {"passed": all(selected.values()), "checks": selected}


def _memory_model_operability_gate(report: Mapping[str, object]) -> dict[str, object]:
    requests = _fresh_memory_model_requests(report)
    phases = [str(item.get("phase") or "") for item in requests]
    model = str(report.get("model") or "")
    hard = report.get("hardGates")
    hard = hard if isinstance(hard, Mapping) else {}
    checks = {
        "requestSequence": bool(phases)
        and phases[0] == "atom-first-curation"
        and phases[-1] == "atom-first-verifier"
        and all(
            phase
            in {
                "atom-first-curation",
                "atom-first-repair",
                "atom-first-verifier",
            }
            for phase in phases
        ),
        "requestExit": bool(requests)
        and all(item.get("exitCode") == 0 for item in requests),
        "requestModel": bool(requests)
        and all(item.get("model") == model for item in requests),
        "requestThinking": bool(requests)
        and all(item.get("thinking") == "max" for item in requests),
        "verifierIsolation": bool(requests)
        and requests[-1].get("isolated") is True,
        "sourceShadowUnchanged": hard.get("sourceShadowUnchanged") is True,
        "productionDatabaseClosed": hard.get("productionDatabaseOpened") is False,
        "productionUnchanged": hard.get("productionMutationPerformed") is False,
    }
    return {"passed": all(checks.values()), "checks": checks}


def _memory_model_only_preflight(
    baseline: Mapping[str, object],
    *,
    model: str,
    context_profile: str,
    prompt_contract: str,
) -> dict[str, object]:
    baseline_contract = _memory_model_contract_snapshot(baseline)
    baseline_quality = _memory_quality_gate(baseline)
    baseline_operability = _memory_model_operability_gate(baseline)
    checks = {
        "baselineModel": baseline.get("model") == "gpt-5.6-sol",
        "candidateModel": model == "gpt-5.6-luna",
        "provider": baseline.get("provider") == "openai-codex",
        "thinking": baseline.get("thinking") == "max",
        "transport": baseline.get("transport") == "codex_cli_ephemeral",
        "evaluationScope": baseline.get("evaluationScope") == "validation-only",
        "contextProfile": baseline_contract.get("contextProfile") == context_profile,
        "promptContract": baseline_contract.get("promptContract") == prompt_contract,
        "baselineQuality": baseline_quality["passed"] is True,
        "baselineOperability": baseline_operability["passed"] is True,
        "contractHashes": baseline_contract.get("complete") is True,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "baselineContractFingerprintSha256": baseline_contract.get(
            "contractFingerprintSha256"
        ),
    }


def _memory_model_only_comparison(
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
    *,
    baseline_report_sha256: str = "",
) -> dict[str, object]:
    baseline_contract = _memory_model_contract_snapshot(baseline)
    candidate_contract = _memory_model_contract_snapshot(candidate)

    def factor(
        name: str,
        mode: str,
        before: object,
        after: object,
        passed: bool,
    ) -> dict[str, object]:
        return {
            "name": name,
            "mode": mode,
            "baseline": before,
            "candidate": after,
            "passed": bool(passed),
        }

    factor_table = [
        factor(
            "model",
            "changed",
            baseline.get("model"),
            candidate.get("model"),
            baseline.get("model") == "gpt-5.6-sol"
            and candidate.get("model") == "gpt-5.6-luna",
        )
    ]
    expected_values: dict[str, object] = {
        "provider": "openai-codex",
        "thinking": "max",
        "transport": "codex_cli_ephemeral",
        "evaluationScope": "validation-only",
        "contextProfile": _MEMORY_CONTEXT_PROFILES,
        "promptContract": _MEMORY_PROMPT_CONTRACTS,
    }
    for name in (
        "provider",
        "thinking",
        "transport",
        "evaluationScope",
        "contextProfile",
        "promptContract",
    ):
        before = baseline_contract.get(name)
        after = candidate_contract.get(name)
        expected = expected_values[name]
        valid = before in expected if isinstance(expected, tuple) else before == expected
        factor_table.append(
            factor(
                name,
                "frozen",
                before,
                after,
                bool(valid and before == after),
            )
        )

    def hash_row(name: str, key: str) -> dict[str, object]:
        before = str(baseline_contract.get(key) or "")
        after = str(candidate_contract.get(key) or "")
        valid = bool(re.fullmatch(r"[0-9a-f]{64}", before)) and bool(
            re.fullmatch(r"[0-9a-f]{64}", after)
        )
        return {
            "name": name,
            "baselineSha256": before,
            "candidateSha256": after,
            "matched": bool(valid and before == after),
        }

    hash_table = [
        hash_row("syntheticFixture", "syntheticFixtureSha256"),
        hash_row("frozenInput", "frozenInputSha256"),
        hash_row("curationSemanticPacket", "curationSemanticPacketSha256"),
        hash_row("curationSchema", "curationSchemaSha256"),
        hash_row("verifierSchema", "verifierSchemaSha256"),
        hash_row("contractFingerprint", "contractFingerprintSha256"),
    ]
    packet_shape_matches = (
        baseline_contract.get("curationSourcePacketChars")
        == candidate_contract.get("curationSourcePacketChars")
        and baseline_contract.get("curationProjectedPacketChars")
        == candidate_contract.get("curationProjectedPacketChars")
    )
    factor_table.append(
        factor(
            "curationPacketShape",
            "frozen",
            {
                "sourceChars": baseline_contract.get("curationSourcePacketChars"),
                "projectedChars": baseline_contract.get(
                    "curationProjectedPacketChars"
                ),
            },
            {
                "sourceChars": candidate_contract.get("curationSourcePacketChars"),
                "projectedChars": candidate_contract.get(
                    "curationProjectedPacketChars"
                ),
            },
            packet_shape_matches,
        )
    )
    baseline_quality = _memory_quality_gate(baseline)
    candidate_quality = _memory_quality_gate(candidate)
    semantic_quality = _memory_semantic_quality_gate(candidate)
    recovery = _memory_recovery_gate(candidate)
    operability = _memory_model_operability_gate(candidate)
    single_factor = bool(
        baseline_quality["passed"]
        and baseline_contract.get("complete") is True
        and candidate_contract.get("complete") is True
        and all(item["passed"] is True for item in factor_table)
        and all(item["matched"] is True for item in hash_table)
    )
    if not single_factor:
        decision = "invalid_model_only_lane"
        failure_owner = "experiment_contract"
        allowed_next_stage = "correct_factor_freeze"
    elif not operability["passed"]:
        decision = "diagnose_runtime_or_provider"
        failure_owner = "runtime_or_provider"
        allowed_next_stage = "runtime_provider_diagnosis"
    elif not semantic_quality["passed"]:
        decision = "adapt_prompt"
        failure_owner = "model_prompt_interaction"
        allowed_next_stage = "one_general_prompt_contract"
    elif not recovery["passed"] or not candidate_quality["passed"]:
        decision = "repair_evaluation_harness"
        failure_owner = "evaluation_harness"
        allowed_next_stage = "evaluation_harness_repair"
    else:
        decision = "no_prompt_adaptation"
        failure_owner = "none"
        allowed_next_stage = "none"
    prompt_adaptation_needed = decision == "adapt_prompt"
    return {
        "schemaVersion": "paw.memory-maintenance-model-only-comparison.v1",
        "decision": decision,
        "singleVariable": "model" if single_factor else "invalid",
        "singleFactorGatePassed": single_factor,
        "operabilityGatePassed": operability["passed"],
        "semanticQualityGatePassed": semantic_quality["passed"],
        "recoveryGatePassed": recovery["passed"],
        "qualityGatePassed": candidate_quality["passed"],
        "promptAdaptationNeeded": prompt_adaptation_needed,
        "allowedNextStage": allowed_next_stage,
        "failureOwner": failure_owner,
        "baselineRunId": baseline.get("runId"),
        "candidateRunId": candidate.get("runId"),
        "factorTable": factor_table,
        "hashTable": hash_table,
        "baselineQuality": baseline_quality,
        "candidateQuality": candidate_quality,
        "semanticQuality": semantic_quality,
        "recovery": recovery,
        "operability": operability,
        "evidence": {
            "baselineReportSha256": baseline_report_sha256,
            "verifierSemanticPacketFrozen": False,
            "verifierSemanticPacketReason": (
                "the verifier packet contains the candidate model output"
            ),
        },
        "claimBoundary": [
            "one private-shadow validation run only",
            "quality and recovery determine prompt adaptation; usage and elapsed time do not",
            "no Provider bill, installed Gateway, foreground, or held-out claim",
        ],
    }


def _memory_cost_optimization_comparison(
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
) -> dict[str, object]:
    baseline_context = str(baseline.get("contextProfile") or "full-json-v1")
    candidate_context = str(candidate.get("contextProfile") or "full-json-v1")
    baseline_contract = str(baseline.get("promptContract") or "standard-v1")
    candidate_contract = str(candidate.get("promptContract") or "standard-v1")
    context_changed = baseline_context != candidate_context
    contract_changed = baseline_contract != candidate_contract
    one_factor = context_changed != contract_changed
    single_variable = (
        "canonical_json_context_projection"
        if context_changed and not contract_changed
        else "concise_json_prompt_contract"
        if contract_changed and not context_changed
        else "invalid_multiple_or_missing_factors"
    )
    baseline_evidence = baseline.get("evidence")
    candidate_evidence = candidate.get("evidence")
    identity_checks = {
        "provider": (
            str(baseline.get("provider") or "openai-codex")
            == str(candidate.get("provider") or "openai-codex")
            == "openai-codex"
        ),
        "model": baseline.get("model") == candidate.get("model") == "gpt-5.6-sol",
        "thinking": baseline.get("thinking") == candidate.get("thinking") == "max",
        "evaluationScope": baseline.get("evaluationScope")
        == candidate.get("evaluationScope")
        == "validation-only",
        "fixture": isinstance(baseline_evidence, Mapping)
        and isinstance(candidate_evidence, Mapping)
        and baseline_evidence.get("syntheticFixtureSha256")
        == candidate_evidence.get("syntheticFixtureSha256")
        and bool(baseline_evidence.get("syntheticFixtureSha256")),
        "singleVariable": one_factor,
    }
    baseline_quality = _memory_quality_gate(baseline)
    candidate_quality = _memory_quality_gate(candidate)
    quality_ok = bool(baseline_quality["passed"] and candidate_quality["passed"])
    baseline_usage = baseline.get("usage")
    candidate_usage = candidate.get("usage")
    usage_available = (
        isinstance(baseline_usage, Mapping)
        and baseline_usage.get("available") is True
        and isinstance(candidate_usage, Mapping)
        and candidate_usage.get("available") is True
    )
    usage_comparison: dict[str, dict[str, object]] = {}
    strict_decrease = False
    usage_ok = usage_available
    for key in _MEMORY_USAGE_KEYS:
        before = baseline_usage.get(key) if isinstance(baseline_usage, Mapping) else None
        after = candidate_usage.get(key) if isinstance(candidate_usage, Mapping) else None
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
        usage_comparison[key] = {
            "before": before,
            "after": after,
            "delta": (after - before) if valid else None,
            "nonIncreasing": non_increasing,
            "strictlyDecreased": decreased,
        }
    cost_ok = bool(usage_ok and strict_decrease)
    passed = bool(all(identity_checks.values()) and quality_ok and cost_ok)
    return {
        "schemaVersion": "paw.memory-maintenance-cost-optimization-comparison.v1",
        "decision": "keep" if passed else "reject",
        "singleVariable": single_variable,
        "baselineContextProfile": baseline_context,
        "candidateContextProfile": candidate_context,
        "baselinePromptContract": baseline_contract,
        "candidatePromptContract": candidate_contract,
        "identityGatePassed": all(identity_checks.values()),
        "identityChecks": identity_checks,
        "qualityGatePassed": quality_ok,
        "baselineQuality": baseline_quality,
        "candidateQuality": candidate_quality,
        "costGatePassed": cost_ok,
        "usage": usage_comparison,
        "costAuthority": "same-provider-model-usage-categories",
        "providerBillAvailable": False,
        "elapsedIsKeepGate": False,
    }


def _memory_optimization_receipt(
    *,
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
    baseline_report_sha256: str,
    candidate_report_sha256: str,
) -> dict[str, object]:
    comparison = _memory_cost_optimization_comparison(baseline, candidate)
    variable = str(comparison["singleVariable"])
    if variable == "canonical_json_context_projection":
        factor = {
            "layer": "prompt_context",
            "name": variable,
            "before": comparison["baselineContextProfile"],
            "after": comparison["candidateContextProfile"],
            "why": "remove JSON formatting whitespace without changing the decoded packet",
        }
    else:
        factor = {
            "layer": "prompt_output_contract",
            "name": variable,
            "before": comparison["baselinePromptContract"],
            "after": comparison["candidatePromptContract"],
            "why": "remove redundant wrapper instructions and bound JSON rationale fields while preserving the full packet",
        }
    return {
        "schemaVersion": "paw.memory-maintenance-cost-optimization-receipt.v1",
        "runId": f"memory-maintenance-cost-optimization:{candidate.get('runId') or 'unknown'}",
        "status": "completed",
        "decision": comparison["decision"],
        "baselineRunId": baseline.get("runId"),
        "candidateRunId": candidate.get("runId"),
        "factor": factor,
        "comparison": comparison,
        "timing": {
            "baseline": baseline.get("timing"),
            "candidate": candidate.get("timing"),
            "keepGate": False,
        },
        "evidence": {
            "baselineReportSha256": baseline_report_sha256,
            "candidateReportSha256": candidate_report_sha256,
            "priorCandidateReceiptsConsumed": False,
            "selectionPolicy": "compare only the named fresh candidate with the named full-json baseline",
        },
        "claimBoundary": [
            "private-shadow validation only",
            "cost verdict uses comparable Codex usage categories, not a Provider bill",
            "elapsed time is recorded but is not a Keep gate",
            "installed Gateway and foreground acceptance remain separate",
        ],
    }


def _write_private_summary(path: Path, summary: Mapping[str, object]) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(dict(summary), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)
    path.chmod(0o600)


def _write_public_report(path: Path, summary: Mapping[str, object]) -> None:
    if path.exists():
        raise ValueError("public report path already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(
            json.dumps(_public_report_payload(summary), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return
    first = dict(summary.get("firstRun") or {})
    applied = dict(summary.get("appliedState") or {})
    rollback = dict(summary.get("rollback") or {})
    replay = dict(summary.get("replay") or {})
    replay_baseline = dict(summary.get("replayBaseline") or {})
    seed = dict(summary.get("syntheticSeed") or {})
    applied_rag = dict(summary.get("appliedRag") or {})
    rollback_rag = dict(summary.get("rollbackRag") or {})
    replay_rag = dict(summary.get("replayRag") or {})
    requests = [
        dict(item)
        for item in summary.get("modelRequests") or []
        if isinstance(item, Mapping)
    ]
    lines = [
        "# Personal Memory Luna Shadow Evaluation",
        "",
        f"- Result: `{summary.get('status')}`.",
        "- Scope: verified private recovery shadow; production SQLite was not opened or modified.",
        f"- Model: `{summary.get('model')}`, thinking `{summary.get('thinking')}`.",
        f"- Transport: `{summary.get('transport')}`; installed Gateway acceptance remains separate.",
        f"- Source shadow unchanged: `{summary.get('sourceShadowUnchanged')}`.",
        "",
        "## Evidence -> Atom -> Book",
        "",
        f"- First run: `{first.get('ok')}`; scopes `{first.get('ranScopeCount')}`.",
        f"- Current Atoms: `{applied.get('currentAtomCount')}`; governed `{applied.get('governedCurrentAtomCount')}`.",
        f"- Unsupported governed Atoms: `{applied.get('unsupportedGovernedAtomCount')}`.",
        f"- All governed current Atoms have legal lineage: `{applied.get('allGovernedCurrentAtomsHaveLegalLineage')}`.",
        f"- Book projection in sync: `{dict(applied.get('bookProjection') or {}).get('inSync')}`.",
        f"- Logical applied-state SHA-256: `{applied.get('logicalStateSha256')}`.",
        "",
        "## Recovery",
        "",
        f"- Rollback kind: `{rollback.get('kind')}`; pass `{rollback.get('ok')}`.",
        f"- Rollback restored baseline: `{rollback.get('restoredBaseline')}`.",
        f"- Replay attempted: `{replay.get('attempted')}`; pass `{replay.get('ok')}`.",
        f"- Replay used an exact private pre-run snapshot: `{replay_baseline.get('captured')}`; restored before replay: `{replay.get('baselineRestoredForReplay')}`.",
        f"- Replay reused content-addressed model outputs: `{replay.get('reusedModelRequests')}`.",
        f"- Replay restored identical logical state: `{replay.get('restoredAppliedState')}`.",
        "",
    ]
    if bool(summary.get("syntheticRagFixtureEnabled")):
        lines.extend(
            [
                "## Hybrid RAG acceptance",
                "",
                f"- Synthetic capture-v2 cases: `{seed.get('caseCount')}`; durable `{seed.get('durableCaseCount')}`; non-memory controls `{seed.get('nonMemoryCaseCount')}`.",
                f"- Every fixture stored with candidate Evidence: `{seed.get('allStored') and seed.get('allCandidateEvidence')}`.",
                f"- Applied projection fresh: `{applied_rag.get('projectionFresh')}`; vector coverage `{applied_rag.get('vectorCoverage')}`.",
                f"- Applied hybrid recall passed: `{applied_rag.get('passed')}`; curated Atom count `{applied_rag.get('discoveredAtomCount')}`.",
                f"- Rollback removed the curated Atom set from retrieval: `{rollback_rag.get('passed')}`.",
                f"- Content-addressed replay restored the same Atom set and recall: `{replay_rag.get('passed') and replay.get('ragAtomSetStable')}`.",
                "",
                "| Case | Expected Memory | Linked Atoms | Retrieved | Best rank | Pass |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for item in applied_rag.get("cases") or []:
            if not isinstance(item, Mapping):
                continue
            lines.append(
                "| `{case}` | `{expected}` | `{linked}` | `{retrieved}` | `{rank}` | `{passed}` |".format(
                    case=item.get("caseId"),
                    expected=item.get("expectMemory"),
                    linked=item.get("linkedAtomCount"),
                    retrieved=item.get("retrieved"),
                    rank=item.get("bestRank"),
                    passed=item.get("passed"),
                )
            )
        lines.append("")
    lines.extend(["## Real-model receipts", ""])
    for item in requests:
        lines.append(
            "- `{phase}`: model `{model}`, thinking `{thinking}`, isolated `{isolated}`, "
            "resumed `{resumed}`, elapsed `{elapsed}` s, output `{output}`.".format(
                phase=item.get("phase"),
                model=item.get("model"),
                thinking=item.get("thinking"),
                isolated=item.get("isolated"),
                resumed=item.get("resumed"),
                elapsed=item.get("elapsedSeconds"),
                output=item.get("outputSha256"),
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "This closes the private-shadow real-Luna quality and recovery slice only. "
            "It does not claim installed Gateway, Squirrel, Voice, or Control Center acceptance.",
            "No raw source text, model output, database path, or private identifier is included here.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _public_report_payload(summary: Mapping[str, object]) -> dict[str, object]:
    """Build the machine-readable, raw-text-free public receipt."""

    first = dict(summary.get("firstRun") or {})
    applied = dict(summary.get("appliedState") or {})
    applied_rag = dict(summary.get("appliedRag") or {})
    rollback = dict(summary.get("rollback") or {})
    rollback_rag = dict(summary.get("rollbackRag") or {})
    replay = dict(summary.get("replay") or {})
    replay_rag = dict(summary.get("replayRag") or {})
    seed = dict(summary.get("syntheticSeed") or {})
    model_requests = [
        dict(item)
        for item in summary.get("modelRequests") or []
        if isinstance(item, Mapping)
    ]
    return {
        "schemaVersion": "paw.memory-maintenance-validation-receipt.v1",
        "runId": summary.get("runId"),
        "status": "passed" if bool(summary.get("passed")) else "iterate",
        "evaluationScope": "validation-only",
        "heldOutEvaluated": False,
        "localOnly": True,
        "provider": summary.get("provider", "openai-codex"),
        "model": summary.get("model"),
        "thinking": summary.get("thinking"),
        "transport": summary.get("transport"),
        "contextProfile": summary.get("contextProfile"),
        "promptContract": summary.get("promptContract", "standard-v1"),
        "metrics": {
            "fixture": {
                "caseCount": seed.get("caseCount"),
                "durableCaseCount": seed.get("durableCaseCount"),
                "nonMemoryCaseCount": seed.get("nonMemoryCaseCount"),
                "allStored": seed.get("allStored"),
                "allCandidateEvidence": seed.get("allCandidateEvidence"),
            },
            "curation": {
                "ok": first.get("ok"),
                "sourceCount": first.get("results", [{}])[0].get("sourceCount")
                if isinstance(first.get("results"), list) and first.get("results")
                and isinstance(first.get("results")[0], Mapping)
                else None,
                "modelDecisionCount": first.get("results", [{}])[0].get("modelDecisionCount")
                if isinstance(first.get("results"), list) and first.get("results")
                and isinstance(first.get("results")[0], Mapping)
                else None,
                "currentAtomCount": applied.get("currentAtomCount"),
                "governedCurrentAtomCount": applied.get("governedCurrentAtomCount"),
                "legalLineageCurrentAtomCount": applied.get("legalLineageCurrentAtomCount"),
                "bookProjectionInSync": dict(applied.get("bookProjection") or {}).get("inSync"),
            },
            "retrieval": {
                "caseCount": applied_rag.get("caseCount"),
                "durableCaseCount": applied_rag.get("durableCaseCount"),
                "passed": applied_rag.get("passed"),
                "allCasesPassed": all(
                    bool(item.get("passed"))
                    for item in applied_rag.get("cases") or []
                    if isinstance(item, Mapping)
                ),
                "bestRanks": {
                    str(item.get("caseId")): item.get("bestRank")
                    for item in applied_rag.get("cases") or []
                    if isinstance(item, Mapping)
                },
                "projectionFresh": applied_rag.get("projectionFresh"),
                "projectionBacklog": applied_rag.get("projectionBacklog"),
                "vectorCoverage": applied_rag.get("vectorCoverage"),
            },
            "recovery": {
                "rollbackPassed": rollback.get("ok"),
                "rollbackRestoredBaseline": rollback.get("restoredBaseline"),
                "rollbackRagCleared": rollback_rag.get("passed"),
                "replayPassed": replay.get("ok"),
                "replayReusedModelRequests": replay.get("reusedModelRequests"),
                "replayRestoredAppliedState": replay.get("restoredAppliedState"),
                "replayRagPassed": replay_rag.get("passed"),
                "replayAtomSetStable": replay.get("ragAtomSetStable"),
            },
        },
        "modelRequests": model_requests,
        "usage": summary.get("usage"),
        "timing": summary.get("timing"),
        "optimizationComparison": summary.get("optimizationComparison"),
        "modelOnlyComparison": summary.get("modelOnlyComparison"),
        "hardGates": {
            "productionDatabaseOpened": False,
            "productionMutationPerformed": False,
            "sourceShadowUnchanged": summary.get("sourceShadowUnchanged"),
            "heldOutEvaluated": False,
            "privateShadow": True,
            "rollbackVerified": rollback.get("ok"),
            "replayVerified": replay.get("ok"),
        },
        "evidence": {
            "syntheticFixtureSha256": seed.get("fixtureSha256"),
            "appliedStateSha256": applied.get("logicalStateSha256"),
            "appliedAtomSetSha256": applied_rag.get("discoveredAtomIdsSha256"),
            "replayAtomSetSha256": replay_rag.get("discoveredAtomIdsSha256"),
            "replayBaselineSha256": dict(summary.get("replayBaseline") or {}).get("sha256"),
        },
        "claimBoundary": [
            "private-shadow validation only",
            "real Codex model requests and independent verifier retained in private artifacts",
            "installed Gateway and foreground acceptance are not evaluated",
        ],
    }


def _write_semantic_public_report(
    path: Path,
    summary: Mapping[str, object],
) -> None:
    if path.exists():
        raise ValueError("public report path already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    recovery = dict(summary.get("recovery") or {})
    curation = dict(summary.get("curation") or {})
    requests = [
        dict(item)
        for item in summary.get("modelRequests") or []
        if isinstance(item, Mapping)
    ]
    lines = [
        "# Personal Memory Luna Historical Semantic Evaluation",
        "",
        f"- Result: `{summary.get('status')}`.",
        "- Scope: approved historical Timeline inside a verified private recovery shadow.",
        "- This is classification quality evidence only; legacy rows were not admitted, persisted, or relabeled as canonical Evidence.",
        f"- Model: `{summary.get('model')}`, thinking `{summary.get('thinking')}`.",
        f"- Source shadow unchanged: `{summary.get('sourceShadowUnchanged')}`.",
        "",
        "## Historical expression recovery",
        "",
        f"- Raw Timeline events: `{recovery.get('rawEventCount')}`.",
        f"- Raw input-method events: `{recovery.get('rawInputMethodEventCount')}`.",
        f"- Strong-final candidates: `{recovery.get('strongFinalCandidateCount')}`.",
        f"- Bounded-context recovered candidates: `{recovery.get('contextRecoveredCandidateCount')}`.",
        f"- Ambiguous fragments excluded: `{recovery.get('ambiguousExcludedCount')}`.",
        f"- Sensitive rows excluded: `{recovery.get('sensitiveExcludedCount')}`.",
        f"- Exact duplicate snapshots removed: `{recovery.get('deduplicatedCandidateCount')}`.",
        f"- Luna inputs: `{recovery.get('modelInputCount')}` expressions representing `{recovery.get('representedSourceEventCount')}` source events.",
        f"- Frozen semantic bundle SHA-256: `{recovery.get('semanticBundleSha256')}`.",
        "",
        "## Luna classification",
        "",
        f"- Exact source-ref coverage: `{curation.get('allSourceRefsCoveredExactlyOnce')}`.",
        f"- Dispositions: `{json.dumps(curation.get('dispositionCounts') or {}, sort_keys=True)}`.",
        f"- Proposed Atom count: `{curation.get('memoryAtomCount')}`.",
        f"- Proposed Atom kinds: `{json.dumps(curation.get('memoryAtomKindCounts') or {}, sort_keys=True)}`.",
        f"- Independent verifier passed: `{curation.get('independentlyVerified')}`.",
        f"- Redacted result SHA-256: `{curation.get('resultSha256')}`.",
        "",
        "## Real-model receipts",
        "",
    ]
    for item in requests:
        lines.append(
            "- `{phase}`: model `{model}`, thinking `{thinking}`, isolated `{isolated}`, "
            "resumed `{resumed}`, elapsed `{elapsed}` s, output `{output}`.".format(
                phase=item.get("phase"),
                model=item.get("model"),
                thinking=item.get("thinking"),
                isolated=item.get("isolated"),
                resumed=item.get("resumed"),
                elapsed=item.get("elapsedSeconds"),
                output=item.get("outputSha256"),
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "This validates the compact personal-v2 prompts against real private history while preserving the fail-closed Evidence boundary. It does not replace legal capture-v2 lineage, RAG retrieval acceptance, or installed foreground acceptance.",
            "No raw input, reconstructed expression, Atom text, model output, database path, or private identifier is included in this report.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
