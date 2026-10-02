"""CloudOps Lab admission over the existing Pi benchmark runner.

Host assets are registered by the application owner, never supplied as paths
by a browser. Each admitted trial retains its original private execution
directory. Reading or replaying its job does not run this adapter again.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from ..agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from .trial_execution import TrialObserver


@dataclass(frozen=True)
class CloudOpsTrialAssets:
    blind_root: Path
    gold: Path
    scorer: Path
    runtime_candidate: Path
    source_agent_config: Path
    private_root: Path
    pricing_config: Path | None = None
    pricing_date: str = ""
    provider: str = "openai-codex"
    default_model: str = DEFAULT_AGENT_MODEL_ID


@contextmanager
def _runtime_environment(assets, run_root, spec, suite, gateway, *, service_factory):
    # This is the same isolated benchmark service used by the CLI. Its Tool
    # manifest provider must not replace the ordinary Session service's one.
    from ..rag_benchmark_agent import RagBenchmarkAgentSpoolGateway
    from scripts.run_cloudops_agent_eval import ROOT, _candidate_runtime_config

    spool = run_root / "agent" / "tool-spool"
    transport = RagBenchmarkAgentSpoolGateway(gateway, spool_dir=spool)
    service = None
    try:
        transport.start()
        config, identity = _candidate_runtime_config(
            assets.runtime_candidate, run_root=run_root,
            source_agent_config=assets.source_agent_config,
            provider=spec["provider"], model=spec["model"],
            tool_gateway_url=transport.tool_gateway_url, tool_gateway_token=transport.token,
            runtime_wrapper=ROOT / "scripts" / "rag_agent_spool_runtime_wrapper.mjs",
            spool_dir=spool,
        )
        service = service_factory(
            db_path=run_root / "observability.sqlite", runtime_config=config,
            project="cloudops-benchmark-agent", tool_gateway_url=transport.tool_gateway_url,
            tool_gateway_token=transport.token, wake_scheduler_enabled=False,
            background_job_execution_owner=False,
        )
        yield service, identity
    finally:
        try:
            if service is not None:
                service.close()
        finally:
            transport.close()


class _ObservedService:
    def __init__(self, service, observer):
        self._service, self._observer = service, observer

    def __getattr__(self, name):
        return getattr(self._service, name)

    def prompt(self, session_id, payload):
        receipt = self._service.prompt(session_id, payload)
        turn_id = str(receipt.get("turnId") or "")
        if turn_id:
            # The Session binding already owns the one actual abort hook.
            self._observer.bind_session(session_id, turn_id)
        return receipt


class CloudOpsTrialAdapter:
    def __init__(self, assets: CloudOpsTrialAssets | None, *, environment_factory=None, service_factory=None):
        self.assets = assets
        if environment_factory is None and service_factory is None:
            raise ValueError("CloudOps runtime environment must be supplied by the application owner")
        self._environment = environment_factory or partial(
            _runtime_environment, service_factory=service_factory,
        )

    def _settings(self, spec: Mapping) -> dict:
        if self.assets is None:
            raise ValueError("CloudOps execution assets are not configured")
        allowed = {"model", "provider", "thinking", "evaluationSplit", "workflowProfile", "contextProjection", "maxReadsPerCase", "timeoutSeconds", "candidatePromptText"}
        if not isinstance(spec, Mapping) or set(spec) - allowed:
            raise ValueError("CloudOps trial contains unsupported settings; host paths are not accepted")
        settings = {
            "model": self.assets.default_model, "provider": self.assets.provider,
            "thinking": "max", "evaluationSplit": "validation",
            "workflowProfile": "baseline-v1", "contextProjection": "standard-v1",
            "maxReadsPerCase": 40, "timeoutSeconds": 900., "candidatePromptText": "",
            **spec,
        }
        for key in ("model", "provider", "thinking", "evaluationSplit", "workflowProfile", "contextProjection", "candidatePromptText"):
            if not isinstance(settings[key], str):
                raise ValueError(f"CloudOps {key} must be text")
        if settings["provider"] != self.assets.provider or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", settings["model"]):
            raise ValueError("CloudOps model must belong to the configured Provider")
        if settings["thinking"] not in {"off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("CloudOps thinking level is unsupported")
        if settings["evaluationSplit"] not in {"validation", "development"}:
            raise ValueError("CloudOps trial supports validation/development only")
        if settings["contextProjection"] not in {"standard-v1", "observation-id-v1"}:
            raise ValueError("CloudOps context projection is unsupported")
        reads, timeout = settings["maxReadsPerCase"], settings["timeoutSeconds"]
        if type(reads) is not int or not 1 <= reads <= 200:
            raise ValueError("CloudOps maxReadsPerCase must be between 1 and 200")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 30 <= timeout <= 3600:
            raise ValueError("CloudOps timeoutSeconds must be between 30 and 3600")
        from scripts.agent_eval_candidate_prompt import CandidatePrompt
        from scripts.run_cloudops_agent_eval import _batch_prompt
        if settings["candidatePromptText"]:
            CandidatePrompt(settings["candidatePromptText"])
        # The runner remains the authority for registered workflow profiles.
        _batch_prompt("batch-1", ("validation/runtime/1",), workflow_profile=settings["workflowProfile"])
        return settings

    def _asset_identity(self, suite) -> dict:
        from scripts.run_cloudops_agent_eval import _file_sha256
        assets = self.assets
        assert assets is not None
        return {
            "suite": suite.suite_sha256, "contract": suite.contract_sha256,
            "gold": _file_sha256(Path(assets.gold)), "scorer": _file_sha256(Path(assets.scorer)),
            "blindFiles": {str(path.relative_to(assets.blind_root)): _file_sha256(path)
                           for path in sorted(Path(assets.blind_root).rglob("*")) if path.is_file()},
        }

    def prepare(self, spec: Mapping, job_id: str) -> dict:
        from ..cloudops_benchmark_agent import CloudOpsBlindSuite
        from scripts.agent_eval_candidate_prompt import CandidatePrompt, candidate_prompt_identity
        settings = self._settings(spec)
        suite = CloudOpsBlindSuite(self.assets.blind_root)
        identity = self._asset_identity(suite)
        candidate = CandidatePrompt(settings["candidatePromptText"]) if settings["candidatePromptText"] else None
        public = {key: value for key, value in settings.items() if key != "candidatePromptText"}
        public.update({"caseCount": len(suite.case_ids), "batchCount": len(suite.batch_ids),
                       "candidatePrompt": candidate_prompt_identity(candidate),
                       "suiteRevision": suite.suite_sha256})
        return {"publicSpec": public, "privateInput": {
            "jobId": job_id, "trialId": "lab-" + hashlib.sha256(job_id.encode()).hexdigest()[:24],
            "settings": settings, "assetIdentity": identity,
        }}

    def execute(self, private_input: Mapping, observer: TrialObserver, cancelled: Callable[[], bool]) -> dict:
        from ..agent_artifacts import AgentArtifactStore
        from ..cloudops_benchmark_agent import CloudOpsBenchmarkGateway, CloudOpsBlindSuite
        from scripts import run_cloudops_agent_eval as runner

        assets = self.assets
        if assets is None:
            raise ValueError("CloudOps execution assets are not configured")
        settings = self._settings(private_input["settings"])
        if cancelled():
            raise runner.EvaluationCancelled("evaluation cancelled")
        suite = CloudOpsBlindSuite(assets.blind_root)
        if self._asset_identity(suite) != private_input["assetIdentity"]:
            raise ValueError("CloudOps assets changed after trial admission")
        trial_id = runner._normalize_trial_id(private_input["trialId"])
        private_root = Path(assets.private_root).expanduser().resolve()
        private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_root = private_root / trial_id
        # Existing execution evidence is never overwritten or silently resumed.
        run_root.mkdir(mode=0o700)
        runner._write_json_atomic(run_root / "input.json", private_input)
        candidate_path = None
        if settings["candidatePromptText"]:
            candidate_path = run_root / "candidate.txt"
            with candidate_path.open("x", encoding="utf-8") as handle:
                handle.write(settings["candidatePromptText"])
            candidate_path.chmod(0o600)
        gateway = runner._CloudOpsContextProjectionGateway(
            CloudOpsBenchmarkGateway(suite, max_reads_per_case=settings["maxReadsPerCase"]),
            context_projection=settings["contextProjection"],
        )
        report = None
        try:
            observer.progress("正在准备 CloudOps 的 12 个验证案例")
            with self._environment(assets, run_root, settings, suite, gateway) as (service, runtime_identity):
                batch_count = 0

                def on_session(session_id):
                    nonlocal batch_count
                    batch_count += 1
                    observer.bind_session(session_id, cancel=lambda: service.abort(session_id))
                    observer.progress(f"正在执行第 {batch_count}/3 批案例")

                report = runner.run_cloudops_agent_eval(
                    suite=suite, gateway=gateway, service=_ObservedService(service, observer),
                    trial_id=trial_id, gold_path=assets.gold,
                    score_host_only=lambda gold, answers: runner.invoke_host_scorer(assets.scorer, gold, answers),
                    trace_store=service.trace_store, eval_store=service.eval_runs,
                    sandbox_store=service.sandbox_runs,
                    artifact_store=AgentArtifactStore(run_root / "observability.sqlite", root=run_root / "agent-artifacts"),
                    timeout_seconds=settings["timeoutSeconds"], runtime_identity=runtime_identity,
                    thinking_level=settings["thinking"], workflow_profile=settings["workflowProfile"],
                    context_projection=settings["contextProjection"], candidate_prompt_file=candidate_path,
                    evaluation_split=settings["evaluationSplit"], scorer_identity=private_input["assetIdentity"]["scorer"],
                    cancelled=cancelled, on_session=on_session,
                )
                runner._write_json_atomic(run_root / "report.json", report)
        except BaseException as exc:
            runner._write_json_atomic(run_root / "failure.json", {
                "trialId": trial_id, "status": "cancelled" if cancelled() else "failed",
                "errorType": type(exc).__name__,
                "errorFingerprint": hashlib.sha256(f"{type(exc).__name__}:{exc}".encode()).hexdigest(),
            })
            raise
        finally:
            # Export only after the service and transport have settled. Even
            # failed trials retain their costs when source receipts exist.
            cost = self._cost(run_root, trial_id)
        observer.progress("评测已完成，结果与执行回执已保存")
        return {
            "schemaVersion": "rag-ime.agent-lab-trial-result.v1", "sceneId": "cloudops",
            "trialId": trial_id, "status": report["status"], "caseCount": report["caseCount"],
            "metrics": report["metrics"], "signals": report["signals"], "usage": report["usage"],
            "evaluationSplit": report["evaluationSplit"], "cost": cost,
            "candidatePrompt": report["candidatePrompt"], "frozenControls": report["frozenControls"],
            "evidence": {"reportRef": f"lab-trial:{trial_id}:report", "traceIds": report["traceIds"],
                         "evalRunIds": report["evalRunIds"], "artifactId": report["artifactId"]},
        }

    def _cost(self, run_root: Path, trial_id: str) -> dict:
        from scripts.run_cloudops_agent_eval import _write_json_atomic
        from scripts.build_agent_lab_cost_receipt_from_runtime_db import build_multi_model_cost_receipt
        assets = self.assets
        if assets.pricing_config is None or not assets.pricing_date:
            return {"available": False, "reason": "pricing_not_configured"}
        try:
            receipt = build_multi_model_cost_receipt(
                run_root / "observability.sqlite", Path(assets.pricing_config),
                run_id=trial_id, published_date=assets.pricing_date,
            )
            _write_json_atomic(run_root / "cost.json", receipt)
            aggregate = receipt["aggregate"]
            return {"available": True, **aggregate, "authority": receipt["authority"],
                    "providerBillAvailable": False, "receiptRef": f"lab-trial:{trial_id}:cost"}
        except (Exception, SystemExit):
            _write_json_atomic(run_root / "cost-unavailable.json", {"trialId": trial_id, "reason": "runtime_receipts_incomplete"})
            return {"available": False, "reason": "runtime_receipts_incomplete"}
