from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any, Mapping, cast

from .agent_sessions import AgentSessionStore
from .agent_lab.experiments import AgentLabExperimentStore
from .agent_lab.candidate_evidence import project_candidate_evidence
from .contracts.json_schema import load_contract, validate_contract


class EvalLabProjection:
    """Read-only, privacy-bounded projection of imported evaluation Sessions."""

    def __init__(self, db_path: str | Path, *, source_ledger_path: str | Path | None = None):
        self.sessions = AgentSessionStore(db_path)
        self.sessions.initialize()
        self.experiments = AgentLabExperimentStore(db_path)
        self.experiments.initialize()
        self.source_ledger_path = Path(source_ledger_path) if source_ledger_path else None

    def list_runs(self) -> dict[str, object]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        before_updated_at_ms: int | None = None
        before_id: str | None = None
        while True:
            page = self.sessions.list_evaluation_page(
                include_archived=True, limit=500,
                before_updated_at_ms=before_updated_at_ms, before_id=before_id,
            )
            for record in cast(list[dict[str, Any]], page["items"]):
                snapshot = record["snapshot"]
                if not isinstance(snapshot, Mapping):
                    continue
                run_id = str(snapshot.get("runId") or "").strip()
                if run_id:
                    grouped[run_id].append(record)
            if not page["hasMore"]:
                break
            before_updated_at_ms = cast(int, page["nextBeforeUpdatedAtMs"])
            before_id = cast(str, page["nextBeforeId"])

        items = [self._run_payload(run_id, records) for run_id, records in grouped.items()]
        items.sort(key=lambda item: (-int(item["updatedAtMs"]), str(item["runId"])))
        total = len(items)
        items = items[:500]
        omitted_tasks = 0
        for item in items:
            tasks = cast(list[dict[str, object]], item["tasks"])
            omitted_tasks += max(0, len(tasks) - 500)
            item["tasks"] = tasks[:500]
        experiments = self.list_experiments()
        payload: dict[str, object] = {
            "schemaVersion": "rag-ime.eval-lab-run-list.v1",
            "ok": True,
            "items": items,
            "total": total,
            "experiments": experiments,
            "experimentTotal": len(experiments),
        }
        if total > len(items) or omitted_tasks:
            payload["truncation"] = {
                "runLimit": 500, "taskLimit": 500,
                "omittedRunCount": total - len(items),
                "omittedTaskCount": omitted_tasks,
            }
        path_searches = self._path_searches_with_source_ledger()
        payload["pathSearches"] = path_searches
        payload["pathSearchTotal"] = len(path_searches)
        validate_contract(payload, "eval-lab-run-list.v1.json")
        return payload

    def _path_searches_with_source_ledger(self) -> list[dict[str, object]]:
        """Expose only the public summary of the latest path-search receipts."""

        if self.source_ledger_path is None:
            return []
        directory = self.source_ledger_path.parent / "runs"
        if not directory.is_dir():
            return []
        projections: list[dict[str, object]] = []
        path_search_schema = load_contract("eval-lab-run-list.v1.json")["$defs"]["pathSearch"]
        for path in sorted(directory.glob("agent-lab-optimal-path-*.json"), reverse=True):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(raw, Mapping) or raw.get("schemaVersion") != "rag-ime.agent-lab-path-search.v1":
                continue
            selected_path = raw.get("selectedPath")
            claim = raw.get("claim")
            baseline = raw.get("baseline")
            candidates = raw.get("candidates")
            objective = raw.get("objective")
            controls = raw.get("frozenControls")
            if not isinstance(selected_path, list) or not isinstance(claim, Mapping) or not isinstance(baseline, Mapping) or not isinstance(candidates, list) or not isinstance(objective, Mapping) or not isinstance(controls, list):
                continue
            selected_node_id = str(selected_path[-1].get("nodeId") or "") if isinstance(selected_path[-1], Mapping) else ""
            baseline_metrics = baseline.get("metrics")
            baseline_node_id = str(baseline.get("nodeId") or "")
            if not selected_node_id or not baseline_node_id or not isinstance(baseline_metrics, Mapping):
                continue
            candidate_projection: list[dict[str, object]] = [{
                "nodeId": baseline_node_id,
                "changedFactor": str(baseline.get("changedFactor") or "baseline"),
                "status": str(baseline.get("status") or "unknown"),
                "metrics": {
                    str(key): float(value)
                    for key, value in baseline_metrics.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                },
                "reason": "用户指定的比较基线；是否采用候选仍以本路径的门禁与质量结论为准。",
            }]
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                metrics = candidate.get("metrics")
                candidate_node_id = str(candidate.get("nodeId") or "")
                if not candidate_node_id or candidate_node_id == baseline_node_id or not isinstance(metrics, Mapping):
                    continue
                candidate_projection.append({
                    "nodeId": candidate_node_id,
                    "changedFactor": str(candidate.get("changedFactor") or ""),
                    "status": str(candidate.get("status") or "unknown"),
                    "metrics": {
                        str(key): float(value)
                        for key, value in metrics.items()
                        if isinstance(value, (int, float)) and not isinstance(value, bool)
                    },
                    "reason": str(candidate.get("reason") or "未提供候选判定原因。"),
                })
            metric_values = objective.get("metrics")
            metric_summary = "、".join(
                f"{str(metric.get('name') or '')} {'↑' if metric.get('direction') == 'max' else '↓'} {float(metric.get('weight') or 0):g}"
                for metric in metric_values
                if isinstance(metric, Mapping) and metric.get("name")
            ) if isinstance(metric_values, list) else "未提供指标权重。"
            projection = {
                "schemaVersion": "rag-ime.agent-lab-path-search.v1",
                "searchId": str(raw.get("searchId") or path.stem),
                "title": str(raw.get("title") or "Agent Lab 最优路径搜索"),
                "objectiveSummary": str(objective.get("userNeed") or "未提供用户目标。"),
                "metricSummary": metric_summary or "未提供指标权重。",
                "frozenControlCount": len(controls),
                "selectedNodeId": selected_node_id,
                "selectedPath": [
                    {
                        "nodeId": str(step.get("nodeId") or ""),
                        "decision": str(step.get("decision") or "unknown"),
                        "reason": str(step.get("reason") or "未提供路径判定原因。"),
                    }
                    for step in selected_path
                    if isinstance(step, Mapping)
                ],
                "claimStatus": str(claim.get("status") or "insufficient_evidence"),
                "claimSummary": str(claim.get("summary") or "尚无路径判定摘要。"),
                "candidates": candidate_projection,
                "generatedAtMs": max(0, int(raw.get("generatedAtMs") or 0)),
            }
            try:
                validate_contract(projection, path_search_schema)
            except Exception:
                continue
            projections.append(projection)
        projections.sort(key=lambda item: (-int(item["generatedAtMs"]), str(item["searchId"])))
        return projections[:32]

    def list_experiments(self) -> list[dict[str, object]]:
        """Prefer the checked-in public ledger while retaining DB revisions.

        The ledger is read-only here; explicit import remains the persistence
        path.  This lets a managed Runtime serve a freshly rebuilt frontend
        even when its external demo DB predates the latest projection fields.
        """

        stored = self.experiments.list_latest()
        if self.source_ledger_path is None or not self.source_ledger_path.is_file():
            return stored
        try:
            from scripts.import_agent_lab_experiments import read_public_experiments

            _, _, projected = read_public_experiments(ledger_path=self.source_ledger_path)
        except (OSError, ValueError, TypeError, ImportError):
            return stored
        merged = {str(item["experimentId"]): item for item in stored}
        merged.update({str(item["experimentId"]): item for item in projected})
        experiments = sorted(merged.values(), key=lambda item: str(item["experimentId"]))
        # Optional public detail is an installed receipt projection, never a
        # live private rescore or rewrite of an imported experiment revision.
        root = self.source_ledger_path.resolve().parents[2]
        for experiment in experiments:
            detail = project_candidate_evidence(experiment, root=root)
            if detail is not None:
                experiment["optimizationEvidence"] = detail
                validate_contract(experiment, "agent-lab-experiment.v1.json")
        return experiments

    @staticmethod
    def _run_payload(
        run_id: str,
        records: list[dict[str, Any]],
    ) -> dict[str, object]:
        records.sort(
            key=lambda record: (
                int(record["snapshot"].get("taskIndex") or 0),
                str(record["session"].get("id") or ""),
            )
        )
        first = records[0]["snapshot"]
        tasks: list[dict[str, object]] = []
        for record in records:
            session = record["session"]
            snapshot = record["snapshot"]
            verifier = snapshot.get("verifier")
            verifier = verifier if isinstance(verifier, Mapping) else {}
            task_payload: dict[str, object] = {
                "sessionId": str(session.get("id") or ""),
                "title": str(session.get("title") or ""),
                "taskAlias": str(snapshot.get("taskAlias") or "Task"),
                "taskIndex": max(1, int(snapshot.get("taskIndex") or 1)),
                "taskSucceeded": bool(snapshot.get("taskSucceeded")),
                "terminalEvent": str(snapshot.get("terminalEvent") or "unknown"),
                "verifierPassed": max(0, int(verifier.get("passed") or 0)),
                "verifierTotal": max(0, int(verifier.get("total") or 0)),
                "toolCalls": max(0, int(snapshot.get("toolCalls") or 0)),
                "failedToolCalls": max(
                    0, int(snapshot.get("failedToolCalls") or 0)
                ),
                "latencyMs": max(0.0, float(snapshot.get("latencyMs") or 0.0)),
            }
            explanation = snapshot.get("explanation")
            if isinstance(explanation, Mapping):
                task_payload["explanation"] = dict(explanation)
            tasks.append(task_payload)
        task_count = len(tasks)
        task_success_count = sum(bool(task["taskSucceeded"]) for task in tasks)
        verifier_pass_count = sum(int(task["verifierPassed"]) for task in tasks)
        verifier_count = sum(int(task["verifierTotal"]) for task in tasks)
        created_at_ms = min(int(record["session"].get("createdAtMs") or 0) for record in records)
        updated_at_ms = max(int(record["session"].get("updatedAtMs") or 0) for record in records)
        split = str(first.get("split") or "unknown")
        suite_id = str(first.get("suiteId") or "evaluation")
        title = (
            "EnterpriseOps CSM"
            if suite_id == "enterpriseops-csm"
            else suite_id.replace("-", " ").strip().title()
        )
        return {
            "schemaVersion": "rag-ime.eval-lab-run.v1",
            "runId": run_id,
            "title": title,
            "suiteId": suite_id,
            "split": split,
            "workflowProfile": str(first.get("workflowProfile") or "unknown"),
            "status": "completed",
            "taskCount": task_count,
            "taskSuccessCount": task_success_count,
            "taskSuccessRate": task_success_count / task_count if task_count else 0.0,
            "verifierPassCount": verifier_pass_count,
            "verifierCount": verifier_count,
            "verifierPassRate": (
                verifier_pass_count / verifier_count if verifier_count else 0.0
            ),
            "toolCalls": sum(int(task["toolCalls"]) for task in tasks),
            "failedToolCalls": sum(int(task["failedToolCalls"]) for task in tasks),
            "latencyMs": sum(float(task["latencyMs"]) for task in tasks),
            "sourceDatabaseSha256": str(first.get("sourceDatabaseSha256") or ""),
            "sourceReportSha256": str(first.get("sourceReportSha256") or ""),
            "createdAtMs": created_at_ms,
            "updatedAtMs": updated_at_ms,
            "tasks": tasks,
        }
