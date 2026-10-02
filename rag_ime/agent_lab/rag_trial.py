"""App scene trials over the existing RAG runner and its ordinary Pi owner.

Admission is a read-only freeze of explicitly supplied host assets. Execution
uses the original runner once, retains its Runtime/report evidence privately,
and projects only aggregate fields to the App. This adapter never installs a
Runtime, supplies Gold from the frontend, resumes execution, or retries a run.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ..agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from .trial_execution import AgentLabTrialExecutionInterrupted, TrialObserver


@dataclass(frozen=True)
class AgentLabRagTrialAssets:
    prepared_path: Path
    answer_cases_path: Path
    answer_evidence_qrels_path: Path
    retrieval_report_path: Path
    source_agent_config: Path
    pi_runtime_payload: Path
    reranker_model: Path | None = None
    reranker_revision: str = ""
    reranker_cache: Path | None = None
    rerank_instruction: str | None = None
    development_report_paths: tuple[Path, ...] = ()
    slice_seed: str = "paw-retrieval-experiment-v1"
    agent_seed: str = "paw-agent-ablation-v1"
    slice_cases_per_split: int = 60
    agent_case_limit: int = 4
    distractor_limit: int = 1000
    timeout_seconds: float = 420.0
    lane_attempts: int = 1
    judge_model: str = DEFAULT_AGENT_MODEL_ID
    pricing_config: Path | None = None
    pricing_published_date: str = ""
    pricing_source_url: str = "https://platform.openai.com/docs/pricing"


_INPUT_SCHEMA = "rag-ime.agent-lab-rag-trial-input.v1"
_METRICS = (
    "highLevelAnswerCorrectnessRate", "highLevelFactCoverage", "citationFactCoverage",
    "answerableCitationSupportRate", "infoNotFoundAbstentionRecall", "answerJudgeCorrectnessRate",
    "answerSuccessRate", "citationSuccessRate", "agentSuccessRate", "toolSuccessRate", "abstentionAccuracy",
)
_DENOMINATORS = ("highLevelCases", "highLevelFacts", "citationFacts", "infoNotFoundCases", "protocolCases", "answerableCitationCases")


def _runner():
    # AgentService may lazily register this adapter. Avoid importing its CLI
    # consumer while AgentService itself is still being imported.
    from scripts import run_rag_agent_ablation
    return run_rag_agent_ablation


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_identity(path: Path) -> dict:
    source = Path(path).expanduser().resolve(strict=True)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return {"path": str(source), "sha256": digest.hexdigest()}


def _directory_identity(path: Path) -> dict:
    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("RAG host model directory is unavailable")
    files = {str(item.relative_to(root)): _file_identity(item)["sha256"]
        for item in sorted(root.rglob("*")) if item.is_file()}
    if not files:
        raise ValueError("RAG host model directory is empty")
    return {"path": str(root), "sha256": _digest(files), "fileCount": len(files)}


def _write_private(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(_canonical(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _snapshot_file(identity: Mapping, destination: Path) -> None:
    digest = hashlib.sha256()
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output, Path(identity["path"]).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != identity["sha256"]:
        raise ValueError("RAG host assets changed while freezing execution input")


class AgentLabRagTrialAdapter:
    scene_id = "enterprise-rag"

    def __init__(self, artifact_root: str | Path, *, assets: AgentLabRagTrialAssets | None = None):
        self.artifact_root = Path(artifact_root).expanduser().resolve(strict=False)
        self.assets = assets

    def _controls(self, spec: Mapping) -> dict:
        allowed = {"provider", "model", "thinking", "evaluationSplit", "candidatePromptText", "promptProfile", "agenticSupplementalLimit"}
        if not isinstance(spec, Mapping) or set(spec) - allowed:
            raise ValueError("Unsupported RAG trial controls; host paths and Gold are not accepted")
        runner = _runner()
        controls = {"provider": "openai-codex", "model": runner._EVALUATION_MODEL, "thinking": "max",
            "evaluationSplit": "validation", "candidatePromptText": "", "promptProfile": runner._INCUMBENT_PROMPT_PROFILE,
            "agenticSupplementalLimit": 6, **dict(spec)}
        if (controls["provider"] != "openai-codex" or controls["thinking"] != "max"
                or controls["evaluationSplit"] != "validation" or controls["model"] not in runner._EVALUATION_MODELS):
            raise ValueError("RAG trials require a supported OpenAI Codex model, max thinking and development Validation")
        if type(controls["agenticSupplementalLimit"]) is not int or controls["agenticSupplementalLimit"] not in {3, 6}:
            raise ValueError("Unsupported RAG supplemental search budget")
        if not isinstance(controls["promptProfile"], str):
            raise ValueError("Unsupported RAG Prompt profile")
        runner._validate_prompt_profile(controls["promptProfile"], answer_only=True, evaluation_split="validation", development_only=True)
        candidate = controls["candidatePromptText"]
        if not isinstance(candidate, str):
            raise ValueError("candidatePromptText must be text")
        if candidate:
            runner.CandidatePrompt(candidate)
        return controls

    def _freeze_assets(self) -> dict:
        if self.assets is None:
            raise ValueError("RAG host assets are not configured")
        assets, runner = self.assets, _runner()
        if assets.judge_model not in runner._EVALUATION_MODELS:
            raise ValueError("RAG host Judge model is unsupported")
        for value, lower, upper in ((assets.slice_cases_per_split, 1, 10000), (assets.agent_case_limit, 1, 10000),
                (assets.distractor_limit, 0, 1000000), (assets.lane_attempts, 1, 3)):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("RAG host execution budget is invalid")
        if type(assets.timeout_seconds) not in {int, float} or not math.isfinite(assets.timeout_seconds) or not 1 <= assets.timeout_seconds <= 86400:
            raise ValueError("RAG host timeout is invalid")
        if assets.pricing_config is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", assets.pricing_published_date):
            raise ValueError("RAG host pricing requires an explicit publication date")
        from ..managed_pi_runtime import snapshot_managed_pi_runtime_payload
        config = Path(assets.source_agent_config).expanduser()
        files = {"prepared": assets.prepared_path, "answers": assets.answer_cases_path, "qrels": assets.answer_evidence_qrels_path,
            "retrieval": assets.retrieval_report_path, "source_auth": config / "auth.json"}
        if (config / "settings.json").is_file():
            files["source_settings"] = config / "settings.json"
        if assets.pricing_config is not None:
            files["pricing"] = assets.pricing_config
        files.update({f"development_{index}": path for index, path in enumerate(assets.development_report_paths)})
        try:
            identities = {key: _file_identity(path) for key, path in files.items()}
            auth = json.loads((config / "auth.json").read_text(encoding="utf-8"))
            if not isinstance(auth, Mapping) or not isinstance(auth.get("openai-codex"), Mapping):
                raise ValueError("OpenAI Codex configuration is unavailable")
            installation = snapshot_managed_pi_runtime_payload(assets.pi_runtime_payload)
            # The managed payload verifies every manifest file. Model assets
            # use a content fingerprint; the mutable pair-score cache is not
            # an input authority and is kept outside that fingerprint.
            models = {"reranker": _directory_identity(assets.reranker_model)} if assets.reranker_model is not None else {}
            retrieval = json.loads(Path(assets.retrieval_report_path).read_text(encoding="utf-8"))
            embedding = retrieval.get("embedding") if isinstance(retrieval, Mapping) else None
            model = embedding.get("model") if isinstance(embedding, Mapping) else None
            if isinstance(model, str) and Path(model).is_absolute():
                models["embedding"] = _directory_identity(Path(model))
            contracts = {str(path.relative_to(runner.ROOT)): _file_identity(path)["sha256"]
                for path in (*runner._RUNTIME_CONTRACT_PATHS, runner._RAG_OPTIMIZATION_SKILL_PATH)}
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise ValueError("RAG host assets are unavailable or invalid") from exc
        return {"files": identities, "models": models,
            "runtime": {"path": str(Path(assets.pi_runtime_payload).expanduser().resolve()), "manifestSha256": installation.manifest_sha256},
            "contractsSha256": _digest(contracts), "policy": {
                "sliceSeed": assets.slice_seed, "agentSeed": assets.agent_seed, "sliceCasesPerSplit": assets.slice_cases_per_split,
                "agentCaseLimit": assets.agent_case_limit, "distractorLimit": assets.distractor_limit,
                "timeoutSeconds": assets.timeout_seconds, "laneAttempts": assets.lane_attempts, "judgeModel": assets.judge_model,
                "rerankerRevision": assets.reranker_revision, "rerankInstruction": assets.rerank_instruction or runner.QWEN3_RERANKER_DEFAULT_INSTRUCTION,
                "rerankerCache": str(Path(assets.reranker_cache).expanduser().resolve()) if assets.reranker_cache is not None else None,
                "pricingPublishedDate": assets.pricing_published_date, "pricingSourceUrl": assets.pricing_source_url}}

    def prepare(self, spec: Mapping, job_id: str) -> Mapping:
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 256:
            raise ValueError("RAG trial requires a bounded job identity")
        controls, assets = self._controls(spec), self._freeze_assets()
        runner = _runner()
        candidate = runner.CandidatePrompt(controls["candidatePromptText"]) if controls["candidatePromptText"] else None
        public = {key: value for key, value in controls.items() if key != "candidatePromptText"}
        public.update({"candidatePrompt": runner.candidate_prompt_identity(candidate), "judgeModel": assets["policy"]["judgeModel"],
            "caseLimit": assets["policy"]["agentCaseLimit"], "laneAttempts": assets["policy"]["laneAttempts"],
            "candidateAware": True, "formalAcceptanceEligible": False, "assetsSha256": _digest(assets)})
        return {"publicSpec": public, "privateInput": {"schemaVersion": _INPUT_SCHEMA, "jobId": job_id,
            "runRoot": str(self.artifact_root / ("trial-" + _digest(job_id))), "controls": controls, "assets": assets}}

    def execute(self, private_input: Mapping, observer: TrialObserver, cancelled: Callable[[], bool]) -> Mapping:
        runner = _runner()
        if cancelled():
            raise runner.RagEvaluationCancelled("RAG trial cancelled before execution")
        if private_input.get("schemaVersion") != _INPUT_SCHEMA:
            raise ValueError("Invalid prepared RAG trial input")
        job_id = private_input["jobId"]
        run_root = self.artifact_root / ("trial-" + _digest(job_id))
        if str(run_root) != private_input.get("runRoot"):
            raise ValueError("Prepared RAG trial belongs to a different artifact owner")
        if run_root.exists():
            raise AgentLabTrialExecutionInterrupted("RAG execution evidence already exists; inspect the original run")
        controls = self._controls(private_input["controls"])
        try:
            assets = self._freeze_assets()
        except ValueError as exc:
            raise ValueError("RAG host assets changed or became unavailable after admission") from exc
        if assets != private_input["assets"]:
            raise ValueError("RAG host assets changed after admission")
        if cancelled():
            raise runner.RagEvaluationCancelled("RAG trial cancelled before execution")
        run_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            run_root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise AgentLabTrialExecutionInterrupted("RAG trial was already started") from exc
        _write_private(run_root / "admission.json", private_input)
        policy = assets["policy"]
        try:
            observer.progress("Freezing RAG execution inputs")
            inputs = run_root / "inputs"
            inputs.mkdir(mode=0o700)
            config = inputs / "agent-config"
            config.mkdir(mode=0o700)
            paths = {}
            for name, identity in assets["files"].items():
                if cancelled():
                    raise runner.RagEvaluationCancelled("RAG trial cancelled before Pi admission")
                destination = config / ("auth.json" if name == "source_auth" else "settings.json") if name.startswith("source_") else inputs / (name + ".json")
                _snapshot_file(identity, destination)
                paths[name] = destination

            def bind(session_id: str, turn_id: str = ""):
                # Persist exact observed identities before projecting them. No
                # duplicate abort hook is registered for blank and real turns;
                # the runner's guarded Pi service owns cooperative abort.
                descriptor = os.open(run_root / "bindings.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    handle.write(_canonical({"sessionId": session_id, "turnId": turn_id}) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                observer.bind_session(session_id, turn_id)
                observer.progress("RAG Pi turn admitted" if turn_id else "RAG Pi Session created")

            observer.progress("Running the frozen RAG evaluation")
            report = runner._run(run_root, prepared_path=paths["prepared"], answer_cases_path=paths["answers"],
                answer_evidence_qrels_path=paths["qrels"], retrieval_report_path=paths["retrieval"], source_agent_config=config,
                slice_seed=policy["sliceSeed"], agent_seed=policy["agentSeed"], slice_cases_per_split=policy["sliceCasesPerSplit"],
                agent_case_limit=policy["agentCaseLimit"], distractor_limit=policy["distractorLimit"], timeout_seconds=policy["timeoutSeconds"],
                lane_attempts=policy["laneAttempts"], reranker_model=Path(assets["models"]["reranker"]["path"]) if "reranker" in assets["models"] else None,
                reranker_revision=policy["rerankerRevision"], reranker_cache=Path(policy["rerankerCache"]) if policy["rerankerCache"] else None,
                rerank_instruction=policy["rerankInstruction"], development_report_paths=[paths[key] for key in paths if key.startswith("development_")],
                calibration_no_metal=False, development_only=True, evaluation_split="validation", answer_only=True,
                pi_runtime_payload=Path(assets["runtime"]["path"]), checkpoint_path=run_root / "lane-checkpoint.json", resume_checkpoint=False,
                agentic_supplemental_limit=controls["agenticSupplementalLimit"], prompt_profile=controls["promptProfile"],
                evaluation_model=controls["model"], judge_model=policy["judgeModel"],
                candidate_prompt=runner.CandidatePrompt(controls["candidatePromptText"]) if controls["candidatePromptText"] else None,
                cancelled=cancelled, on_session=bind, on_turn=bind)
            if not isinstance(report, Mapping) or report.get("schemaVersion") != runner.SCHEMA_VERSION:
                raise ValueError("RAG runner returned an invalid report")
            _write_private(run_root / "report.json", report)
        except BaseException as exc:
            _write_private(run_root / "failure.json", {"type": type(exc).__name__, "error": str(exc), "executionMayHaveStarted": True})
            raise
        if report.get("executionSettled") is False or report.get("status") == "interrupted":
            raise AgentLabTrialExecutionInterrupted("RAG execution cleanup is uncertain; retained original evidence requires inspection")
        cost = self._cost(run_root, paths.get("pricing"), policy, job_id)
        observer.progress("RAG report and execution evidence retained")
        return self._public_report(report, job_id=job_id, assets=assets, cost=cost)

    @staticmethod
    def _cost(run_root: Path, pricing: Path | None, policy: Mapping, job_id: str) -> dict:
        unavailable = {"available": False, "providerBillAvailable": False}
        if pricing is None:
            return {**unavailable, "reason": "pricing_not_configured"}
        if not (run_root / "agent.sqlite").is_file():
            return {**unavailable, "reason": "runtime_evidence_unavailable"}
        from scripts.build_agent_lab_cost_receipt_from_runtime_db import build_multi_model_cost_receipt
        try:
            receipt = build_multi_model_cost_receipt(run_root / "agent.sqlite", pricing, run_id=job_id,
                published_date=policy["pricingPublishedDate"], source_url=policy["pricingSourceUrl"])
            _write_private(run_root / "cost-receipt.json", receipt)
            aggregate = receipt["aggregate"]
            total = Decimal(aggregate["totalCostUsd"])
            if not total.is_finite() or total < 0:
                raise ValueError("Invalid cost estimate")
        except (Exception, SystemExit) as exc:
            _write_private(run_root / "cost-failure.json", {"type": type(exc).__name__, "error": str(exc)})
            return {**unavailable, "reason": "receipt_unavailable"}
        return {"available": True, "authority": "runtime_cost_reconciled_estimate", "providerBillAvailable": False,
            "currency": "USD", "totalCostUsd": str(total), "requestCount": aggregate["requestCount"],
            "failedRequestCount": aggregate["failedRequestCount"], "modelCount": aggregate["modelCount"],
            "usageReconciliation": "matched" if all(item["usageStatus"] == "matched" for item in receipt["reconciliation"]) else "different"}

    @staticmethod
    def _public_report(report: Mapping, *, job_id: str, assets: Mapping, cost: Mapping) -> dict:
        def mapping(value):
            return value if isinstance(value, Mapping) else {}
        evaluation = mapping(report.get("evaluation"))
        judge = mapping(report.get("answerJudge"))
        decision = mapping(report.get("candidateDecision"))
        status = report.get("status")
        if status not in {"failed", "cancelled", "interrupted"}:
            status = "failed" if mapping(report.get("preflight")).get("accepted") is False else "completed"
        metrics, denominators = {}, {}
        for lane in report.get("lanes") or []:
            name = mapping(lane).get("lane")
            if name not in _runner().LANES:
                continue
            score = mapping(lane.get("score"))
            values = mapping(score.get("agentMetrics"))
            metrics[name] = {key: values[key] for key in _METRICS
                if type(values.get(key)) in {int, float} and math.isfinite(values[key]) and 0 <= values[key] <= 1}
            counts = mapping(score.get("metricDenominators"))
            denominators[name] = {key: counts[key] for key in _DENOMINATORS if type(counts.get(key)) is int and counts[key] >= 0}
        scored = (status == "completed" and report.get("scoreEligible") is not False
            and report.get("cleanupPassed") is True and judge.get("accepted") is True
            and set(metrics) == set(_runner().LANES)
            and all({"highLevelAnswerCorrectnessRate", "citationFactCoverage"} <= set(values) for values in metrics.values()))
        quality = ("keep" if decision.get("accepted") is True and decision.get("decision") == "keep" else "reject") if scored else "unavailable"
        evidence = {"reportRef": "lab-rag-report:" + _digest(job_id), "reportSha256": _digest(report), "assetsSha256": _digest(assets)}
        for field in ("caseSetSha256", "answerCaseManifestSha256", "promptConfigSha256"):
            value = evaluation.get(field)
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                evidence[field] = value
        return {"schemaVersion": "rag-ime.agent-lab-trial-result.v1", "sceneId": AgentLabRagTrialAdapter.scene_id,
            "trialId": job_id, "status": status, "caseCount": evaluation.get("caseCount") if type(evaluation.get("caseCount")) is int else None,
            "metrics": metrics, "metricDenominators": denominators,
            "signals": {"qualityVerdict": quality, "scoreEligible": scored, "judgeAccepted": judge.get("accepted") is True,
                "cleanupPassed": report.get("cleanupPassed") is True, "candidateAware": True, "formalAcceptanceEligible": False},
            "cost": dict(cost), "evidence": evidence}
