"""Memory scene trials over the existing shadow evaluator and resident Pi.

Only the host can register recovered source databases or construct/abort Pi
execution. Admission freezes identifiers and controls without creating files.
The default five synthetic fixtures exercise the actual Memory pipeline; they
do not measure quality on the user's personal history.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ..agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from .golden_pi import AgentLabGoldenPiExecutor
from .memory_pi import AgentLabMemoryPiExecutor
from .trial_execution import AgentLabTrialExecutionInterrupted, TrialObserver
from ..personal_memory_luna_evaluation import SYNTHETIC_PERSONAL_MEMORY_RAG_CASES, verify_recovered_memory_shadow

_ROOT = Path(__file__).resolve().parents[2]
_PREPARED_SCHEMA = "rag-ime.agent-lab-memory-trial-input.v1"
_MODELS = {DEFAULT_AGENT_MODEL_ID, "gpt-5.6-luna", "gpt-5.6-sol"}
_CONTEXTS = {"full-json-v1", "compact-json-v1"}
_PROMPTS = {"standard-v1", "concise-json-v1"}
_STAGES = {
    "preparation": "Preparing the isolated Memory evaluation.",
    "curation": "Running Memory curation through Pi.",
    "curation_completed": "Memory curation returned; inspecting the shadow result.",
    "rollback": "Rolling back the isolated Memory changes.",
    "replay": "Preparing Memory rollback replay.",
    "replay_curation": "Replaying the original frozen Memory requests.",
    "replay_completed": "Memory replay returned; inspecting recovery.",
    "replay_rollback": "Cleaning up cancelled Memory replay changes.",
    "completed": "Memory evaluation report produced; cleaning up the shadow.",
    "cancelled": "Memory execution stopped; cleaning up the shadow.",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _file_fingerprint(path: Path) -> dict:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    def identity(value):
        return {"device": value.st_dev, "inode": value.st_ino, "size": value.st_size, "mtimeNs": value.st_mtime_ns}
    if identity(before) != identity(after):
        raise ValueError("Memory source changed while it was frozen")
    return {"sha256": digest.hexdigest(), "identity": identity(after)}


def _private_json(path: Path, value: Mapping) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(_json(dict(value)) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _public_cost(receipts: Sequence[Mapping]) -> dict:
    """Deduplicate shadow replays, keeping absent Runtime cost unavailable."""
    unique = {}
    for receipt in receipts:
        request_id = receipt.get("requestId")
        if request_id and receipt.get("resumed") is not True:
            unique.setdefault(str(request_id), receipt)
    result = {"available": False, "requestCount": len(unique), "providerBill": False}
    amounts, bases = [], set()
    for receipt in unique.values():
        usage = dict(receipt.get("usage") or {}).get("runtimeUsage")
        if not isinstance(usage, Mapping):
            return result
        amount = usage.get("estimatedCostUsd", usage.get("costUsd"))
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
            return result
        basis = usage.get("costBasis")
        if basis not in {"model_catalog_estimate", "runtime_reported", "runtime_reported_estimate"}:
            return result
        amounts.append(float(amount))
        bases.add(str(basis))
    if amounts:
        result.update(available=True, estimatedCostUsd=math.fsum(amounts), currency="USD",
            basis=next(iter(bases)) if len(bases) == 1 else "mixed_runtime_estimates")
    return result


def _public_report(summary: Mapping, *, job_id: str, mode: str, cost: Mapping) -> dict:
    """Project only typed aggregate fields, never model text or source metadata."""
    seed = dict(summary.get("syntheticSeed") or {})
    first = dict(summary.get("firstRun") or {})
    rows = first.get("results")
    curation = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], Mapping) else {}
    applied = dict(summary.get("appliedState") or {})
    retrieval = dict(summary.get("appliedRag") or {})
    rollback, replay = dict(summary.get("rollback") or {}), dict(summary.get("replay") or {})
    def count(value):
        return value if type(value) is int and value >= 0 else None
    def flag(value):
        return value if type(value) is bool else None
    case_count = count(seed.get("caseCount")) if mode == "synthetic-fixture" else count(curation.get("sourceCount"))
    status = summary.get("status")
    if status not in {"failed", "cancelled", "interrupted"}:
        status = "completed"
    quality = ("pass" if summary.get("passed") is True else "reject") if status == "completed" else "unavailable"
    return {
        "schemaVersion": "rag-ime.agent-lab-trial-result.v1", "sceneId": "memory", "trialId": job_id,
        "status": status, "qualityVerdict": quality,
        "caseCount": case_count,
        "metrics": {
            "curation": {"sourceCount": count(curation.get("sourceCount")),
                "modelDecisionCount": count(curation.get("modelDecisionCount")), "ok": flag(first.get("ok")),
                "currentAtomCount": count(applied.get("currentAtomCount")),
                "governedCurrentAtomCount": count(applied.get("governedCurrentAtomCount")),
                "legalLineageCurrentAtomCount": count(applied.get("legalLineageCurrentAtomCount"))},
            "retrieval": {"caseCount": count(retrieval.get("caseCount")),
                "durableCaseCount": count(retrieval.get("durableCaseCount")), "passed": flag(retrieval.get("passed")),
                "projectionFresh": flag(retrieval.get("projectionFresh"))},
            "recovery": {"rollbackPassed": flag(rollback.get("ok")) if rollback.get("attempted") is True else None,
                "rollbackRestoredBaseline": flag(rollback.get("restoredBaseline")) if rollback.get("attempted") is True else None,
                "replayAttempted": flag(replay.get("attempted")),
                "replayPassed": flag(replay.get("ok")) if replay.get("attempted") is True else None,
                "replayReusedModelRequests": flag(replay.get("reusedModelRequests"))},
        },
        "signals": {"evaluationMode": mode, "syntheticFixture": mode == "synthetic-fixture",
            "personalMemoryQualityMeasured": False, "heldOutEvaluated": False,
            "productionMutationPerformed": False, "installedAcceptanceEvaluated": False,
            "pipelineResumeSupported": False, "embeddingMode": "local-hashing",
            "sourceShadowUnchanged": flag(summary.get("sourceShadowUnchanged"))},
        "cost": dict(cost), "evidence": {"reportRef": f"memory-trial:{_sha(job_id)}:report"},
    }


class AgentLabMemoryTrialAdapter:
    def __init__(self, artifact_root: str | Path, *,
                 pi_executor_factory: Callable[[], AgentLabGoldenPiExecutor],
                 abort_session: Callable[[str], object],
                 source_assets: Mapping[str, str | Path] | None = None,
                 production_db: str | Path | None = None,
                 project: str = "personal-agent-workbench", max_sources: int = 1_000) -> None:
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        if self.artifact_root.is_relative_to(_ROOT):
            raise ValueError("Memory trial artifacts must be outside the Git worktree")
        if not callable(pi_executor_factory) or not callable(abort_session):
            raise ValueError("Memory trials require a resident Pi factory and abort callback")
        if type(max_sources) is not int or not 1 <= max_sources <= 1_500:
            raise ValueError("Memory source bound must be between 1 and 1500")
        self._pi_factory, self._abort_session = pi_executor_factory, abort_session
        self._sources = {str(key): Path(value).expanduser().resolve() for key, value in (source_assets or {}).items()}
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", key) for key in self._sources):
            raise ValueError("Memory source asset IDs must be bounded public identifiers")
        self._production = Path(production_db or Path.home()/"Library"/"Application Support"/"RagIme"/"rag-ime.sqlite").expanduser().resolve()
        self._project, self._max_sources = str(project), max_sources

    def _source(self, asset_id: str) -> Path:
        source = self._sources.get(asset_id)
        if source is None:
            raise ValueError("Memory source asset is not registered by the host")
        source = source.resolve(strict=True)
        if source == self._production or (self._production.exists() and source.samefile(self._production)):
            raise ValueError("the production database cannot be a Memory trial source")
        if not source.is_file():
            raise ValueError("Memory source asset is not a file")
        return source

    def prepare(self, spec: Mapping, job_id: str) -> dict:
        if not isinstance(spec, Mapping) or set(spec) - {"model", "contextProfile", "promptContract", "sourceAssetId"}:
            raise ValueError("unsupported Memory trial controls; host paths cannot be submitted")
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", job_id):
            raise ValueError("Memory trial requires a bounded job identifier")
        controls = {"model": spec.get("model", DEFAULT_AGENT_MODEL_ID),
            "contextProfile": spec.get("contextProfile", "full-json-v1"),
            "promptContract": spec.get("promptContract", "standard-v1")}
        for key, allowed in (("model", _MODELS), ("contextProfile", _CONTEXTS), ("promptContract", _PROMPTS)):
            if not isinstance(controls[key], str) or controls[key] not in allowed:
                raise ValueError("unsupported Memory trial model or prompt configuration")
        asset_id = spec.get("sourceAssetId", "")
        if not isinstance(asset_id, str):
            raise ValueError("Memory source asset ID must be a string")
        source = None
        if asset_id:
            path = self._source(asset_id)
            frozen = _file_fingerprint(path)
            verify_recovered_memory_shadow(path)
            if frozen != _file_fingerprint(path):
                raise ValueError("Memory source changed during verification")
            source = {"assetId": asset_id, "path": str(path), **frozen}
        mode = "host-shadow" if source else "synthetic-fixture"
        public = {**controls, "provider": "openai-codex", "thinkingLevel": "max", "evaluationMode": mode,
            "sourceAssetId": asset_id, "fixtureCaseCount": None if source else len(SYNTHETIC_PERSONAL_MEMORY_RAG_CASES),
            "personalMemoryQualityMeasured": False, "embeddingMode": "local-hashing"}
        return {"publicSpec": public, "privateInput": {"schemaVersion": _PREPARED_SCHEMA, "jobId": job_id,
            "controls": controls, "source": source, "evaluationMode": mode,
            "project": self._project, "maxSources": self._max_sources,
            "fixtureSha256": _sha(_json(SYNTHETIC_PERSONAL_MEMORY_RAG_CASES)) if source is None else None}}

    def execute(self, private_input: Mapping, observer: TrialObserver, cancelled: Callable[[], bool]) -> dict:
        from scripts import eval_personal_memory_luna as runner
        if private_input.get("schemaVersion") != _PREPARED_SCHEMA:
            raise ValueError("unsupported prepared Memory trial")
        job_id, mode = str(private_input["jobId"]), str(private_input["evaluationMode"])
        controls = dict(private_input["controls"])
        if cancelled():
            return _public_report({"status": "cancelled"}, job_id=job_id, mode=mode, cost={"available": False})
        source = private_input.get("source")
        source_path = None
        if source is not None:
            source_path = self._source(str(source["assetId"]))
            if str(source_path) != source["path"] or _file_fingerprint(source_path) != {key: source[key] for key in ("sha256", "identity")}:
                raise ValueError("Memory source changed after trial admission")
        elif private_input.get("fixtureSha256") != _sha(_json(SYNTHETIC_PERSONAL_MEMORY_RAG_CASES)):
            raise ValueError("Memory fixture changed after trial admission")
        self.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.artifact_root.is_dir() or self.artifact_root.stat().st_mode & 0o077:
            raise ValueError("Memory trial artifact root must be a private directory")
        trial_root = self.artifact_root / _sha(job_id)
        try:
            trial_root.mkdir(mode=0o700)
        except FileExistsError:
            raise AgentLabTrialExecutionInterrupted("Memory trial already has execution artifacts; automatic rerun is unavailable") from None
        work = trial_root / "work"
        executor = None
        binding_lock = threading.RLock()
        active_sessions: set[str] = set()
        registered_sessions: set[str] = set()
        aborted_sessions: set[str] = set()

        def abort(session_id):
            with binding_lock:
                if session_id not in active_sessions or session_id in aborted_sessions:
                    return None
                aborted_sessions.add(session_id)
            return self._abort_session(session_id)

        def progress(event):
            if event.get("type") == "stage":
                message = _STAGES.get(str(event.get("stage")))
                if message:
                    observer.progress(message)
                return
            if event.get("type") != "model_receipt" or not isinstance(event.get("receipt"), Mapping):
                return
            receipt = event["receipt"]
            session_id, turn_id = str(receipt.get("sessionId") or ""), str(receipt.get("turnId") or "")
            if not session_id:
                return
            with binding_lock:
                bound = receipt.get("status") == "session_bound"
                if bound:
                    active_sessions.add(session_id)
                else:
                    active_sessions.discard(session_id)
                register_abort = bound and session_id not in registered_sessions
                if register_abort:
                    registered_sessions.add(session_id)
            # A later observed turn refines the same binding; it must not
            # register a second abort for the same Session.
            observer.bind_session(session_id, turn_id, cancel=(lambda: abort(session_id)) if register_abort else None)

        def factory(_artifact_root, **kwargs):
            nonlocal executor
            executor = AgentLabMemoryPiExecutor(trial_root / "pi", pi_executor=self._pi_factory(),
                model_id=kwargs["model_id"], context_profile=kwargs["context_profile"],
                prompt_contract=kwargs["prompt_contract"], cancelled=kwargs["cancelled"],
                receipt_observer=kwargs["receipt_observer"], request_namespace=job_id,
                audit_db_path=kwargs["audit_db_path"])
            return executor

        try:
            _private_json(trial_root / "prepared-input.json", private_input)
            argv = ["--private-dir", str(work), "--production-db", str(self._production),
                "--run-id", job_id, "--model", str(controls["model"]),
                "--context-profile", str(controls["contextProfile"]), "--prompt-contract", str(controls["promptContract"]),
                "--project", str(private_input["project"]), "--max-sources", str(private_input["maxSources"])]
            if source_path is None:
                argv.extend(["--seed-synthetic-rag-fixture", "--clean-synthetic-shadow"])
            else:
                argv.extend(["--shadow-db", str(source_path)])
            summary = runner.run_evaluation(runner.build_parser().parse_args(argv), executor_factory=factory,
                progress=progress, cancelled=cancelled)
            receipts = executor.receipts if executor is not None else ()
            _private_json(trial_root / "evaluation-report.json", {"report": summary, "piRequests": list(receipts)})
            if any(item.get("status") == "interrupted" for item in receipts):
                raise AgentLabTrialExecutionInterrupted("Memory Pi settlement remains uncertain; inspect the original request")
            # The evaluator also returns a quality report when its curator
            # could not execute. Preserve the actual Pi disposition instead
            # of interpreting that report as a completed quality rejection.
            execution_summary = summary
            if summary.get("status") != "cancelled":
                statuses = {item.get("status") for item in receipts}
                if "failed" in statuses or "cancelled" in statuses:
                    execution_summary = {**summary, "status": "failed" if "failed" in statuses else "cancelled"}
            result = _public_report(execution_summary, job_id=job_id, mode=mode, cost=_public_cost(receipts))
            _private_json(trial_root / "public-report.json", result)
            return result
        except BaseException as exc:
            _private_json(trial_root / "execution-error.json", {"errorClass": type(exc).__name__,
                "piRequests": list(executor.receipts) if executor is not None else []})
            raise
        finally:
            if executor is not None:
                executor.close()
            if work.exists():
                shutil.rmtree(work)
