"""Durable Golden standards and job receipts; Pi remains the execution owner.

The suite revision versions human-facing standards, not worker progress. Each
job keeps its admission inputs and may only ingest into that same revision.
Human reviews and labels have dedicated commands; model output cannot author
either. Frozen snapshots and command receipts are immutable in SQLite as well
as through this API.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..db import apply_database_migrations, sqlite_connection

__all__ = [
    "AgentLabGoldenConflict", "AgentLabGoldenServiceUnavailable",
    "AgentLabGoldenStore", "AgentLabGoldenValidationError", "GOLDEN_JUDGE_PROTOCOL_VERSION",
]

GOLDEN_JUDGE_PROTOCOL_VERSION = "paw.golden.context-qa-judge.v3"


def optimization_scope(baseline: Mapping, candidate: Mapping, optimize_prompt: bool) -> str:
    model_changed = any(baseline.get(key) != candidate.get(key) for key in ("provider", "model", "thinkingLevel"))
    prompt_changed = optimize_prompt or baseline.get("prompt", "") != candidate.get("prompt", "")
    skill_changed = application_method_identity(baseline).get("sha256") != application_method_identity(candidate).get("sha256")
    changed = [name for name, enabled in (("model", model_changed), ("prompt", prompt_changed), ("skill", skill_changed)) if enabled]
    return "_and_".join(changed) if changed else "repeat"


def application_method_identity(model: Mapping) -> dict:
    method = model.get("applicationMethod")
    return {key: copy.deepcopy(value) for key, value in method.items() if key != "body"} if isinstance(method, Mapping) else {}


def application_method_comparison(baseline: Mapping, candidate: Mapping) -> dict:
    left, right = application_method_identity(baseline), application_method_identity(candidate)
    before = (baseline.get("applicationMethod") or {}).get("body", "")
    after = (candidate.get("applicationMethod") or {}).get("body", "")
    return {"scope": "application_skill_body", "changed": left.get("sha256") != right.get("sha256"),
            "baseline": left or None, "candidate": right or None,
            "diff": "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                                fromfile="baseline/SKILL.md", tofile="candidate/SKILL.md"))}

_ACTIVE = {"queued", "running"}
_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
_ACTIONS = {"create", "draft", "review", "review_case", "label_sample", "judge_config",
            "calibrate", "freeze", "experiment", "cancel", "resume"}
_MODEL_FIELDS = ("provider", "model", "thinkingLevel", "prompt")
_SNAPSHOT_FIELDS = ("snapshotId", "suiteId", "version", "sourceRevision", "createdAtMs",
                    "developmentCount", "holdoutCount", "judgeConfig", "judgeProtocolVersion")


class AgentLabGoldenValidationError(ValueError):
    http_status = 422
    code = "AGENT_LAB_GOLDEN_VALIDATION"

    def response_payload(self) -> dict[str, object]:
        return {"ok": False, "code": self.code, "message": str(self)}


class AgentLabGoldenConflict(AgentLabGoldenValidationError):
    http_status = 409
    code = "AGENT_LAB_GOLDEN_CONFLICT"


class AgentLabGoldenServiceUnavailable(AgentLabGoldenValidationError):
    http_status = 503
    code = "AGENT_LAB_GOLDEN_SERVICE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("Golden 存储暂不可用，请保留原请求并重试。")


def _now() -> int:
    return time.time_ns() // 1_000_000


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"


def _json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AgentLabGoldenValidationError("请求必须是有效的 JSON 数据。") from exc


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentLabGoldenValidationError(f"{name}必须是对象。")
    return dict(value)


def _text(value: object, name: str, *, optional: bool = False, limit: int = 100_000) -> str:
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise AgentLabGoldenValidationError(f"{name}需要有效文本。")
    # Source quotes, answers, and prompts are preserved verbatim.
    return value


def _integer(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise AgentLabGoldenValidationError(f"{name}需要在 {low} 到 {high} 之间。")
    return value


def _choice(value: object, name: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise AgentLabGoldenValidationError(f"{name}不在允许范围内。")
    return value


def _array(value: object, name: str, *, limit: int = 300, minimum: int = 0) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= limit:
        raise AgentLabGoldenValidationError(f"{name}需要包含 {minimum} 到 {limit} 项。")
    return value


def _strings(value: object, name: str) -> list[str]:
    return [_text(item, name, limit=20_000) for item in _array(value, name, limit=100)]


def _model(value: object, fallback: Mapping[str, Any], *, required: bool = True) -> dict[str, Any]:
    provided = _object(value, "模型配置")
    result = {}
    for key in _MODEL_FIELDS:
        field = provided.get(key, "")
        field = _text(field, "模型配置", optional=True)
        if key != "prompt" and not field.strip():
            field = _text(fallback.get(key, ""), "默认模型配置", optional=True)
        result[key] = field
    if required and any(not result[key].strip() for key in ("provider", "model", "thinkingLevel")):
        raise AgentLabGoldenValidationError("请先选择明确的 Provider、模型和推理强度。")
    return result


def _freeze_application_method(conn: sqlite3.Connection, value: object, project_id: str) -> dict:
    """Freeze an explicit application method, never Pi Skill discovery or labels."""
    value = _object(value, "应用 Skill")
    if "artifactId" in value:
        if set(value) != {"artifactId", "artifactRevision"} or not project_id:
            raise AgentLabGoldenValidationError("方法成果需要本评测绑定的项目与明确成果版本。")
        artifact_id = _text(value["artifactId"], "方法成果标识", limit=240)
        revision = _integer(value["artifactRevision"], "方法成果版本", 1, 1_000_000)
        row = conn.execute("SELECT v.payload_json FROM agent_lab_project_artifact_versions v "
                           "JOIN agent_lab_project_artifacts a USING(artifact_id) "
                           "WHERE a.project_id=? AND a.artifact_id=? AND v.revision=?",
                           (project_id, artifact_id, revision)).fetchone()
        if row is None:
            raise AgentLabGoldenValidationError("此项目的方法成果版本不存在。")
        artifact = json.loads(row[0])
        body = artifact.get("content")
        if artifact.get("view") == "code" and isinstance(body, Mapping):
            body = body.get("source")
        elif artifact.get("view") != "markdown":
            raise AgentLabGoldenValidationError("应用 Skill 需要 Markdown 或代码正文成果。")
        title = artifact.get("title", "Application Skill")
        source = {"kind": "project_artifact", "projectId": project_id, "artifactId": artifact_id, "artifactRevision": revision}
    else:
        if set(value) - {"body", "title", "sha256"}:
            raise AgentLabGoldenValidationError("内联方法只接受正文、标题与可选正文哈希，不能声明未核对的成果身份。")
        body, title, source = value.get("body"), value.get("title", "Application Skill"), {"kind": "inline"}
    body = _text(body, "应用 Skill 正文", limit=64_000)
    title = _text(title, "应用 Skill 标题", limit=500)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if "sha256" in value and value["sha256"] != digest:
        raise AgentLabGoldenValidationError("应用 Skill 正文与哈希不一致。")
    return {"kind": "application_skill", "title": title, "body": body, "sha256": digest, "source": source}


def validated_application_method(value: object) -> dict:
    """Check the already admitted body when recovering an execution input."""
    method = _object(value, "冻结应用 Skill")
    if set(method) != {"kind", "title", "body", "sha256", "source"} or method["kind"] != "application_skill":
        raise AgentLabGoldenValidationError("冻结应用 Skill 结构无效。")
    body = _text(method["body"], "冻结应用 Skill 正文", limit=64_000)
    if method["sha256"] != hashlib.sha256(body.encode("utf-8")).hexdigest():
        raise AgentLabGoldenValidationError("冻结应用 Skill 正文已变化，不能恢复原调用。")
    _text(method["title"], "冻结应用 Skill 标题", limit=500)
    _object(method["source"], "冻结应用 Skill 来源")
    return copy.deepcopy(method)


def _sources(value: object) -> list[dict[str, str]]:
    result = []
    identifiers = set()
    for item in _array(value, "来源", minimum=1, limit=100):
        item = _object(item, "来源")
        source_id = _text(item.get("sourceId") or _id("source"), "来源标识", limit=240)
        if source_id in identifiers:
            raise AgentLabGoldenValidationError("来源标识不能重复。")
        identifiers.add(source_id)
        result.append({
            "sourceId": source_id, "title": _text(item.get("title"), "来源标题", limit=500),
            "kind": _choice(item.get("kind"), "来源类型", {"document", "history", "failure"}),
            "uri": _text(item.get("uri"), "来源引用", limit=4000),
            "text": _text(item.get("text"), "来源正文", limit=500_000),
        })
    if sum(len(item["text"]) for item in result) > 2_000_000:
        raise AgentLabGoldenValidationError("来源正文总量过大，请拆分套件。")
    return result


def _evidence(value: object, sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    by_id = {item["sourceId"]: item["text"] for item in sources}
    result = []
    for item in _array(value, "证据", limit=100):
        item = _object(item, "证据")
        source_id = _text(item.get("sourceId"), "证据来源", limit=240)
        quote = _text(item.get("quote"), "证据原文")
        if source_id not in by_id or quote not in by_id[source_id]:
            raise AgentLabGoldenValidationError("证据引用必须逐字存在于对应来源中。")
        result.append({"sourceId": source_id, "quote": quote})
    return result


def _case_standard(value: Mapping[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = value.get("answerable")
    if not isinstance(answerable, bool):
        raise AgentLabGoldenValidationError("是否可回答需要明确的是或否。")
    return {
        "caseId": _text(value.get("caseId"), "题目标识", limit=240),
        "question": _text(value.get("question"), "题目"),
        "taskType": _text(value.get("taskType"), "任务类型", limit=100),
        "answerable": answerable,
        "requiredFacts": _strings(value.get("requiredFacts"), "必答事实"),
        "evidence": _evidence(value.get("evidence"), sources),
        "rubric": _strings(value.get("rubric"), "评分标准"),
        "split": _choice(value.get("split"), "题目分组", {"development", "holdout"}),
    }


def _draft_cases(value: object, sources: list[dict[str, Any]], target_count: int) -> list[dict[str, Any]]:
    result = []
    identifiers = set()
    for raw in _array(value, "起草题目", minimum=1, limit=target_count):
        raw = _object(raw, "起草题目")
        item = _case_standard(raw, sources)
        if item["caseId"] in identifiers:
            raise AgentLabGoldenValidationError("题目标识不能重复。")
        identifiers.add(item["caseId"])
        samples = []
        sample_ids = set()
        for raw_sample in _array(raw.get("samples", []), "答案样本", limit=30):
            sample = _object(raw_sample, "答案样本")
            sample_id = _text(sample.get("sampleId"), "样本标识", limit=240)
            if sample_id in sample_ids:
                raise AgentLabGoldenValidationError("同一题目的样本标识不能重复。")
            sample_ids.add(sample_id)
            samples.append({
                "sampleId": sample_id, "answer": _text(sample.get("answer"), "样本答案"),
                "category": _choice(sample.get("category"), "样本类别", {"correct", "incorrect", "boundary"}),
                "humanVerdict": None, "humanNote": "",
            })
        item.update(review={"status": "pending", "note": "", "reviewedAtMs": None}, samples=samples)
        result.append(item)
    return result


def _labeled_samples(suite: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(item["caseId"], sample["sampleId"]): sample
            for item in suite["cases"]
            if item["split"] == "development" and item["review"]["status"] == "approved"
            for sample in item["samples"] if sample["humanVerdict"] is not None}


def _calibration(suite: dict[str, Any], value: object) -> dict[str, Any]:
    expected = _labeled_samples(suite)
    judgments = []
    seen = set()
    for raw in _array(value, "评审结果", limit=3000):
        raw = _object(raw, "评审结果")
        key = (_text(raw.get("caseId"), "题目标识", limit=240),
               _text(raw.get("sampleId"), "样本标识", limit=240))
        if key not in expected or key in seen:
            raise AgentLabGoldenValidationError("评审结果只能对应本次已标注的开发集样本，且不能重复。")
        seen.add(key)
        judgments.append({
            "caseId": key[0], "sampleId": key[1],
            "verdict": _choice(raw.get("verdict"), "评审判定", {"pass", "fail", "uncertain"}),
            "reason": _text(raw.get("reason"), "评审依据"),
            "evidence": _evidence(raw.get("evidence", []), suite["sources"]),
        })
    if not expected or seen != set(expected):
        raise AgentLabGoldenValidationError("评审结果不完整，不能替缺失的判定填写通过。")
    comparable = matches = false_passes = false_fails = uncertain = unknown_judge = 0
    for judgment in judgments:
        human = expected[(judgment["caseId"], judgment["sampleId"])]["humanVerdict"]
        judged = judgment["verdict"]
        if human in {"pass", "fail"}:
            comparable += 1
            matches += human == judged
        false_passes += human == "fail" and judged == "pass"
        false_fails += human == "pass" and judged == "fail"
        uncertain += human == "uncertain" or judged == "uncertain"
        unknown_judge += judged == "uncertain"
    agreement = matches / comparable if comparable else 0.0
    categories = {sample["category"] for sample in expected.values()}
    labels = {sample["humanVerdict"] for sample in expected.values()}
    label_authors = {author: sum(sample.get("labelAuthor", "unrecorded") == author for sample in expected.values())
                     for author in ("human", "agent", "unrecorded")}
    reasons = []
    if not {"correct", "incorrect", "boundary"}.issubset(categories):
        reasons.append("需覆盖正确、错误和边界三类已标注样本。")
    if not {"pass", "fail"}.issubset(labels):
        reasons.append("至少需要一条标注通过和一条标注不通过的样本。")
    if false_passes:
        reasons.append(f"有 {false_passes} 条标注不通过样本被评审误判为通过。")
    if unknown_judge:
        reasons.append(f"有 {unknown_judge} 条评审判定仍不确定。")
    if agreement < 0.8:
        reasons.append("与明确参考标签的一致率需达到 80%。")
    ready = not reasons
    reasons.append(f"当前只有 {comparable} 条可比较的标注样本；小样本一致率不能证明泛化能力。")
    if label_authors["agent"]:
        reasons.append("包含 Agent 辅助标注；此结果用于流程与模型对照，不代表独立人工金标验收。")
    return {
        "calibrationId": _id("calibration"), "suiteRevision": suite["revision"],
        "judgeProtocolVersion": GOLDEN_JUDGE_PROTOCOL_VERSION,
        "judgeConfig": copy.deepcopy(suite["judgeConfig"]), "judgments": judgments,
        "labelAuthors": label_authors,
        "referenceAuthority": "agent_assisted" if label_authors["agent"] else "human" if not label_authors["unrecorded"] else "unrecorded",
        "metrics": {"total": len(expected), "comparable": comparable, "agreement": agreement,
                    "falsePasses": false_passes, "falseFails": false_fails, "uncertain": uncertain},
        "ready": ready, "reasons": reasons, "createdAtMs": _now(),
    }


def _can_reprocess_draft(job: Mapping[str, Any]) -> bool:
    result = job.get("result")
    receipts = result.get("receipts") if isinstance(result, Mapping) else None
    if job.get("state") != "failed" or job.get("kind") != "draft" or not isinstance(receipts, list) or len(receipts) != 1:
        return False
    record = receipts[0]
    return (isinstance(record, Mapping) and record.get("stage") == "draft"
            and all(isinstance(record.get(key), str) and record[key] for key in ("requestId", "sessionId", "turnId"))
            and isinstance(record.get("receipt"), Mapping)
            and record["receipt"].get("status") == "completed")


def _can_reprocess_experiment(job: Mapping[str, Any], bound: Mapping[str, Any]) -> bool:
    """Rebuild a failed report only when every fixed-arm call already settled."""
    result, snapshot, inputs = job.get("result"), bound.get("snapshot"), bound.get("input", {})
    if (job.get("state") != "failed" or job.get("kind") != "experiment"
            or inputs.get("optimizePrompt") is not False
            or not isinstance(result, Mapping) or not isinstance(snapshot, Mapping)):
        return False
    cases = snapshot.get("cases", [])
    expected = {":".join(quote(str(part), safe="-_.") for part in
                (job["jobId"], stage, case["split"], variant, 0, case["caseId"]))
                for case in cases for variant in ("baseline", "candidate") for stage in ("answer", "judge")}
    receipts = result.get("receipts", [])
    if not expected or not isinstance(receipts, list) or len(receipts) != len(expected):
        return False
    if {r.get("requestId") for r in receipts if isinstance(r, Mapping)} != expected:
        return False
    if any(not isinstance(r, Mapping) or not all(r.get(k) for k in ("sessionId", "turnId"))
           or not isinstance(r.get("receipt"), Mapping) or r["receipt"].get("status") != "completed"
           for r in receipts):
        return False
    if snapshot.get("knowledge"):
        packets = result.get("knowledgePackets", {})
        if not isinstance(packets, Mapping) or set(packets) != {case["caseId"] for case in cases}:
            return False
    return True


def _retryable_request(job: Mapping[str, Any]) -> str:
    """An exact unsuccessful terminal Pi settlement permits explicit retry.

    Host recovery can settle its orphaned turn as cancelled/aborted. The Lab
    job then fails while retaining that receipt. Unknown/interrupted receipts
    still cannot admit another attempt; completed calls are reused.
    """
    result = job.get('result')
    if job.get('state') != 'failed' or not isinstance(result, Mapping): return ''
    pending, receipts = result.get('pendingRequestId'), result.get('receipts')
    if not isinstance(pending,str) or not pending.startswith(quote(str(job.get('jobId','')),safe='-_.')+':') or not isinstance(receipts,list): return ''
    for record in receipts:
        if (isinstance(record,Mapping) and record.get('requestId') == pending
                and all(isinstance(record.get(key),str) and record[key] for key in ('sessionId','turnId'))
                and isinstance(record.get('receipt'),Mapping) and record['receipt'].get('status') in {'failed','cancelled','aborted'}):
            return pending
    return ''


class AgentLabGoldenStore:
    def __init__(self, db_path: str | Path, default_model: Mapping[str, Any] | None = None) -> None:
        self.db_path = Path(db_path)
        self._default_model = _model(default_model or {}, {}, required=False)
        self._initialize_lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            with sqlite_connection(self.db_path, row_factory=sqlite3.Row, foreign_keys=True) as conn:
                yield conn
        except (sqlite3.Error, OSError) as exc:
            raise AgentLabGoldenServiceUnavailable() from exc

    def initialize(self) -> None:
        with self._initialize_lock:
            if self._initialized:
                return
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AgentLabGoldenServiceUnavailable() from exc
            with self._connection() as conn:
                apply_database_migrations(conn)
            self._initialized = True

    @staticmethod
    def _suite(conn: sqlite3.Connection, suite_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT payload_json FROM agent_lab_golden_suites WHERE suite_id = ?", (suite_id,)).fetchone()
        if row is None:
            raise AgentLabGoldenValidationError("Golden 套件不存在。")
        return json.loads(row[0])

    @staticmethod
    def _public_suite(conn: sqlite3.Connection, suite: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(suite)
        result['currentJudgeProtocolVersion'] = GOLDEN_JUDGE_PROTOCOL_VERSION
        calibration = result.get('calibration')
        if isinstance(calibration, dict) and calibration.get('judgeProtocolVersion') != GOLDEN_JUDGE_PROTOCOL_VERSION:
            calibration['ready'] = False
            calibration['reasons'] = ['评审协议已更新，需要重新校准；原校准记录仍保留。', *calibration.get('reasons', [])]
        result["jobs"] = []
        for row in conn.execute(
            "SELECT payload_json, input_json FROM agent_lab_golden_jobs WHERE suite_id = ? ORDER BY created_at_ms DESC, rowid DESC",
            (suite["suiteId"],),
        ):
            job = json.loads(row[0])
            bound = json.loads(row[1])
            unchanged = job['kind'] == 'experiment' or bound["suite"]["revision"] == suite["revision"]
            job["canReprocess"] = bool((_can_reprocess_draft(job) or _can_reprocess_experiment(job, bound)) and unchanged)
            job['canRetryFailedCall'] = bool(_retryable_request(job) and unchanged)
            result["jobs"].append(job)
        return result

    @staticmethod
    def _save_suite(conn: sqlite3.Connection, suite: dict[str, Any]) -> None:
        suite["updatedAtMs"] = _now()
        conn.execute("UPDATE agent_lab_golden_suites SET revision = ?, payload_json = ?, updated_at_ms = ? WHERE suite_id = ?",
                     (suite["revision"], _json(suite), suite["updatedAtMs"], suite["suiteId"]))

    @staticmethod
    def _job_row(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM agent_lab_golden_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise AgentLabGoldenValidationError("Golden 任务不存在。")
        return row

    @staticmethod
    def _save_job(conn: sqlite3.Connection, job: dict[str, Any]) -> None:
        job["updatedAtMs"] = _now()
        conn.execute("UPDATE agent_lab_golden_jobs SET state = ?, payload_json = ?, updated_at_ms = ? WHERE job_id = ?",
                     (job["state"], _json(job), job["updatedAtMs"], job["jobId"]))

    def read(self, suite_id: str = "") -> dict[str, Any]:
        _text(suite_id, "套件标识", optional=True, limit=240)
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN")
            items = [self._public_suite(conn, json.loads(row[0])) for row in conn.execute(
                "SELECT payload_json FROM agent_lab_golden_suites ORDER BY updated_at_ms DESC, rowid DESC"
            )]
        return {"ok": True, "items": items,
                "suite": next((item for item in items if item["suiteId"] == suite_id), None)}

    def command(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        payload = _object(payload, "命令")
        action = _choice(payload.get("action"), "操作", _ACTIONS)
        client_id = _text(payload.get("clientRequestId"), "请求标识", limit=240)
        suite_id = _text(payload.get("suiteId", ""), "套件标识", optional=True, limit=240)
        revision = _integer(payload.get("expectedRevision"), "标准版本", 0, 2**53 - 1)
        value = _object(payload.get("input", {}), "命令内容")
        request_json = _json({"action": action, "suiteId": suite_id, "expectedRevision": revision, "input": value})
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            receipt = conn.execute("SELECT request_json, response_json FROM agent_lab_golden_commands WHERE client_request_id = ?",
                                   (client_id,)).fetchone()
            if receipt is not None:
                if receipt[0] != request_json:
                    raise AgentLabGoldenConflict("此请求标识已用于不同内容，请刷新后重新操作。")
                return {**json.loads(receipt[1]), "replayed": True}
            job = None
            if action == "create":
                if revision != 0 or suite_id:
                    raise AgentLabGoldenValidationError("新建套件需要标准版本 0，且不能指定已有套件。")
                suite = self._create(conn, value)
            else:
                suite = self._suite(conn, suite_id)
                if revision != suite["revision"]:
                    raise AgentLabGoldenConflict("Golden 标准已经更新，请刷新后保留你的修改重新操作。")
                if action in {"draft", "review", "calibrate", "experiment"}:
                    job = self._admit_job(conn, suite, action, value)
                elif action in {"review_case", "label_sample", "judge_config"}:
                    self._edit(suite, action, value)
                    self._save_suite(conn, suite)
                elif action == "freeze":
                    self._freeze(conn, suite)
                    self._save_suite(conn, suite)
                else:
                    job = self._control_job(conn, suite, action, value)
            result = {"ok": True, "suite": self._public_suite(conn, suite), "job": job,
                      "clientRequestId": client_id, "replayed": False}
            conn.execute("INSERT INTO agent_lab_golden_commands (client_request_id, suite_id, request_json, response_json, created_at_ms) VALUES (?, ?, ?, ?, ?)",
                         (client_id, suite["suiteId"], request_json, _json(result), _now()))
            return result

    def create_in_transaction(self, conn: sqlite3.Connection, value: dict[str, Any]) -> dict[str, Any]:
        """Create a suite with an owning project's binding in one unit of work.

        The caller owns the active transaction and migration initialization.
        This creates no job, Session or model call.
        """
        if not conn.in_transaction:
            raise AgentLabGoldenValidationError("关联评测集需要由项目事务创建。")
        return self._create(conn, _object(value, "评测集"))

    def _create(self, conn: sqlite3.Connection, value: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        suite = {
            "schemaVersion": "rag-ime.agent-lab-golden-suite.v1", "suiteId": _id("golden"),
            "title": _text(value.get("title"), "套件名称", limit=500),
            "scenario": _text(value.get("scenario"), "任务场景", limit=10_000),
            "revision": 1, "targetCount": _integer(value.get("targetCount", 30), "题目数量", 2, 100),
            "sources": _sources(value.get("sources")), "cases": [],
            "judgeConfig": copy.deepcopy(self._default_model), "calibration": None, "snapshot": None,
            "createdAtMs": now, "updatedAtMs": now,
        }
        if "knowledge" in value:
            knowledge = _object(value["knowledge"], "知识库绑定")
            if set(knowledge) != {"projectId", "indexId", "corpusHash", "configHash", "profile", "documentCount", "chunkCount"}:
                raise AgentLabGoldenValidationError("知识库绑定字段无效。")
            for key in ("projectId", "indexId", "corpusHash", "configHash"):
                _text(knowledge[key], "知识库绑定标识", limit=240)
            _object(knowledge["profile"], "检索配置")
            suite["knowledge"] = copy.deepcopy(knowledge)
            suite["datasetProvenance"] = _object(value.get("datasetProvenance", {}), "评测集来源")
            if "importedCases" in value:
                suite["cases"] = _draft_cases(value["importedCases"], suite["sources"], suite["targetCount"])
        conn.execute("INSERT INTO agent_lab_golden_suites (suite_id, revision, payload_json, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?)",
                     (suite["suiteId"], suite["revision"], _json(suite), now, now))
        return suite

    @staticmethod
    def _edit(suite: dict[str, Any], action: str, value: dict[str, Any]) -> None:
        if action == "judge_config":
            suite["judgeConfig"] = _model(value.get("judgeConfig"), suite["judgeConfig"])
        else:
            case_id = _text(value.get("caseId"), "题目标识", limit=240)
            item = next((item for item in suite["cases"] if item["caseId"] == case_id), None)
            if item is None:
                raise AgentLabGoldenValidationError("题目不存在，请刷新后重试。")
            if action == "review_case":
                reviewed = _case_standard(value, suite["sources"])
                verdict = _choice(value.get("verdict"), "人工审核", {"approved", "rejected"})
                if verdict == "approved":
                    if not reviewed["rubric"]:
                        raise AgentLabGoldenValidationError("审核通过前，请填写可执行的评分标准。")
                    if reviewed["answerable"] and (not reviewed["requiredFacts"] or not reviewed["evidence"]):
                        raise AgentLabGoldenValidationError("可回答的题目需要必答事实和来源证据，才能审核通过。")
                reviewed["review"] = {
                    "status": verdict,
                    "author": _choice(value.get("reviewAuthor", "human"), "审核来源", {"human", "agent"}),
                    "note": _text(value.get("note", ""), "审核备注", optional=True), "reviewedAtMs": _now(),
                }
                item.update(reviewed)
            else:
                sample_id = _text(value.get("sampleId"), "样本标识", limit=240)
                sample = next((sample for sample in item["samples"] if sample["sampleId"] == sample_id), None)
                if sample is None:
                    raise AgentLabGoldenValidationError("答案样本不存在，请刷新后重试。")
                sample.update({
                    "answer": _text(value.get("answer"), "样本答案"),
                    "humanVerdict": _choice(value.get("humanVerdict"), "人工标签", {"pass", "fail", "uncertain"}),
                    "humanNote": _text(value.get("humanNote", ""), "标注备注", optional=True),
                    "labelAuthor": _choice(value.get("labelAuthor", "human"), "标注来源", {"human", "agent"}),
                })
        suite["revision"] += 1
        suite["calibration"] = None

    @staticmethod
    def _finish_review(suite: dict, bound: dict, result: dict, job_id: str) -> None:
        """Ingest an independent review, preserving questions and human edits."""
        expected = set(bound['input']['caseIds'])
        reviews = _array(result.get('reviews'), 'Agent 核对结果', minimum=1, limit=100)
        if (len(reviews) != len(expected) or any(not isinstance(row, Mapping) for row in reviews)
                or {row.get('caseId') for row in reviews} != expected):
            raise AgentLabGoldenValidationError('核对结果必须与本次待核对题目一一对应。')
        normalized = []
        for row in reviews:
            item = next(case for case in suite['cases'] if case['caseId'] == row['caseId'])
            verdict = _choice(row.get('verdict'), 'Agent 核对', {'approved', 'rejected'})
            if item['review']['status'] != 'pending':
                raise AgentLabGoldenConflict('已核对的题目不能被后台结果覆盖。')
            if verdict == 'approved' and (not item['rubric'] or (item['answerable'] and (not item['requiredFacts'] or not item['evidence']))):
                raise AgentLabGoldenValidationError('通过的题目仍需要评分标准、必答事实及原文证据。')
            samples = _array(row.get('samples'), 'Agent 样本标注', limit=30)
            sample_ids = {sample['sampleId'] for sample in item['samples']}
            if (len(samples) != len(sample_ids) or any(not isinstance(sample, Mapping) for sample in samples)
                    or {sample.get('sampleId') for sample in samples} != sample_ids):
                raise AgentLabGoldenValidationError('Agent 标注必须对应本次每个原始样本。')
            note = _text(row.get('note', ''), '核对说明', optional=True)
            item['review'] = {'status': verdict, 'author': 'agent', 'note': note, 'reviewedAtMs': _now(), 'jobId': job_id}
            labels = []
            for label in samples:
                sample = next(sample for sample in item['samples'] if sample['sampleId'] == label['sampleId'])
                decision = _choice(label.get('verdict'), 'Agent 标注', {'pass', 'fail', 'uncertain'})
                reason = _text(label.get('note', ''), '标注说明', optional=True)
                # Historical field names stay compatible. Explicit authorship
                # is authoritative; never copy a model's claimed human author.
                sample.update(humanVerdict=decision, humanNote=reason, labelAuthor='agent', labelJobId=job_id)
                labels.append({'sampleId': sample['sampleId'], 'verdict': decision, 'note': reason})
            normalized.append({'caseId': item['caseId'], 'verdict': verdict, 'note': note, 'samples': labels})
        result.update(reviews=normalized, reviewAuthor='agent', reviewedCount=len(normalized),
                      approvedCount=sum(row['verdict'] == 'approved' for row in normalized))
        suite['revision'] += 1
        suite['calibration'] = None

    def _admit_job(self, conn: sqlite3.Connection, suite: dict[str, Any], kind: str, value: dict[str, Any]) -> dict[str, Any]:
        active = conn.execute("SELECT 1 FROM agent_lab_golden_jobs WHERE suite_id = ? AND kind = ? AND state IN ('queued', 'running')",
                              (suite["suiteId"], kind)).fetchone()
        if active:
            raise AgentLabGoldenConflict("此类任务仍在进行中，请等待结果或停止后重试。")
        snapshot = None
        if kind == "draft":
            if suite.get("datasetProvenance", {}).get("kind") == "imported_reference":
                raise AgentLabGoldenValidationError("原始评测题已经导入，请直接核对题目；重新起草请建立独立的合成评测集。")
            value = {"model": _model(value.get("model", {}), suite["judgeConfig"])}
        elif kind == "review":
            if set(value) - {"model"}:
                raise AgentLabGoldenValidationError("Agent 核对只接受可选的审核模型配置。")
            pending = [item['caseId'] for item in suite['cases'] if item['review']['status'] == 'pending']
            if not pending:
                raise AgentLabGoldenValidationError("没有待核对题目；已完成的核对会保留。")
            value = {"model": _model(value.get("model", {}), suite["judgeConfig"]), "caseIds": pending}
        elif kind == "calibrate":
            _model(suite["judgeConfig"], {})
            if not _labeled_samples(suite):
                raise AgentLabGoldenValidationError("请先核对开发集题目，并标注答案样本；会记录人工或 Agent 作者。")
            value = {}
        else:
            experiment_input = value
            snapshot_id = _text(value.get("snapshotId"), "冻结版本标识", limit=240)
            row = conn.execute("SELECT payload_json FROM agent_lab_golden_snapshots WHERE suite_id = ? AND snapshot_id = ?",
                               (suite["suiteId"], snapshot_id)).fetchone()
            if row is None:
                raise AgentLabGoldenValidationError("请选择本套件已冻结的 Golden 版本。")
            snapshot = json.loads(row[0])
            if snapshot.get('judgeProtocolVersion') != GOLDEN_JUDGE_PROTOCOL_VERSION:
                raise AgentLabGoldenValidationError('此快照使用旧评审协议，请重新校准并冻结后开始实验。')
            optimize = value.get("optimizePrompt", True)
            if not isinstance(optimize, bool):
                raise AgentLabGoldenValidationError("是否优化 Prompt 需要明确的是或否。")
            value = {"snapshotId": snapshot_id,
                     "baseline": _model(value.get("baseline", {}), suite["judgeConfig"]),
                     "candidate": _model(value.get("candidate", {}), suite["judgeConfig"]),
                     "optimizePrompt": optimize,
                     "maxCandidates": _integer(value.get("maxCandidates", 1), "候选数量", 1, 3)}
            for variant in ("baseline", "candidate"):
                configured = experiment_input.get(variant, {})
                if "applicationMethod" in configured:
                    value[variant]["applicationMethod"] = _freeze_application_method(
                        conn, configured["applicationMethod"], snapshot.get("knowledge", {}).get("projectId", ""))
        now = _now()
        job = {"jobId": _id("golden-job"), "kind": kind, "state": "queued", "progress": "等待执行",
               "sessionId": "", "error": "", "result": None, "createdAtMs": now, "updatedAtMs": now}
        inputs = {"suite": self._public_suite(conn, suite), "snapshot": snapshot, "input": value}
        # Jobs are excluded from model inputs to avoid nesting old results and
        # exposing historical holdout evidence through an unrelated task.
        inputs["suite"]["jobs"] = []
        conn.execute("INSERT INTO agent_lab_golden_jobs (job_id, suite_id, kind, state, payload_json, input_json, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (job["jobId"], suite["suiteId"], kind, job["state"], _json(job), _json(inputs), now, now))
        return job

    @staticmethod
    def _freeze(conn: sqlite3.Connection, suite: dict[str, Any]) -> None:
        calibration = suite["calibration"]
        if (not calibration or not calibration["ready"] or calibration["suiteRevision"] != suite["revision"]
                or calibration.get("judgeProtocolVersion") != GOLDEN_JUDGE_PROTOCOL_VERSION):
            raise AgentLabGoldenValidationError("请先完成当前标准的评审校准，再冻结 Golden。")
        approved = [item for item in suite["cases"] if item["review"]["status"] == "approved"]
        counts = {split: sum(item["split"] == split for item in approved) for split in ("development", "holdout")}
        if not all(counts.values()):
            raise AgentLabGoldenValidationError("开发集和保留集都需要至少一道核对通过的题目。")
        development_questions = {" ".join(item["question"].split()).casefold()
                                 for item in approved if item["split"] == "development"}
        if any(" ".join(item["question"].split()).casefold() in development_questions
               for item in approved if item["split"] == "holdout"):
            raise AgentLabGoldenValidationError("开发集和保留集不能使用相同题目，请调整分组后重新校准。")
        existing = conn.execute("SELECT payload_json FROM agent_lab_golden_snapshots WHERE suite_id = ? AND source_revision = ?",
                                (suite["suiteId"], suite["revision"])).fetchone()
        if existing:
            snapshot = json.loads(existing[0])
        else:
            version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM agent_lab_golden_snapshots WHERE suite_id = ?", (suite["suiteId"],)).fetchone()[0]
            snapshot = {"snapshotId": _id("golden-snapshot"), "suiteId": suite["suiteId"], "version": version,
                        "sourceRevision": suite["revision"], "createdAtMs": _now(),
                        "developmentCount": counts["development"], "holdoutCount": counts["holdout"],
                        "judgeConfig": copy.deepcopy(suite["judgeConfig"]),
                        "judgeProtocolVersion": GOLDEN_JUDGE_PROTOCOL_VERSION,
                        "title": suite["title"], "scenario": suite["scenario"],
                        "sources": copy.deepcopy(suite["sources"]), "cases": copy.deepcopy(approved),
                        "calibration": copy.deepcopy(calibration)}
            for key in ("knowledge", "datasetProvenance"):
                if key in suite:
                    snapshot[key] = copy.deepcopy(suite[key])
            conn.execute("INSERT INTO agent_lab_golden_snapshots (snapshot_id, suite_id, version, source_revision, payload_json, created_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                         (snapshot["snapshotId"], suite["suiteId"], version, suite["revision"], _json(snapshot), snapshot["createdAtMs"]))
        suite["snapshot"] = {key: copy.deepcopy(snapshot[key]) for key in _SNAPSHOT_FIELDS}

    def _control_job(self, conn: sqlite3.Connection, suite: dict[str, Any], action: str, value: dict[str, Any]) -> dict[str, Any]:
        row = self._job_row(conn, _text(value.get("jobId"), "任务标识", limit=240))
        if row["suite_id"] != suite["suiteId"]:
            raise AgentLabGoldenValidationError("此任务不属于当前 Golden 套件。")
        job = json.loads(row["payload_json"])
        if action == "cancel":
            if job["state"] not in _ACTIVE:
                return job
            conn.execute("UPDATE agent_lab_golden_jobs SET cancel_requested = 1 WHERE job_id = ?", (job["jobId"],))
            if job["state"] == "queued":
                if row["execution_started"]:
                    job.update(state="interrupted", progress="已请求停止原任务，等待原 Pi 回合确认")
                else:
                    job.update(state="cancelled", progress="已停止")
            else:
                job["progress"] = "停止请求已记录"
        else:
            bound = json.loads(row["input_json"])
            reprocess = _can_reprocess_draft(job) or _can_reprocess_experiment(job, bound)
            retry = _retryable_request(job)
            if job["state"] != "interrupted" and not reprocess and not retry:
                raise AgentLabGoldenConflict("只有已中断的任务或有明确失败回执的调用可以恢复。")
            if job["kind"] != "experiment" and bound["suite"]["revision"] != suite["revision"]:
                raise AgentLabGoldenConflict("任务对应的标准已经更新，请按当前标准重新开始。")
            if conn.execute("SELECT 1 FROM agent_lab_golden_jobs WHERE suite_id = ? AND kind = ? AND state IN ('queued', 'running')",
                            (suite["suiteId"], job["kind"])).fetchone():
                raise AgentLabGoldenConflict("此类任务仍在进行中，暂不能恢复另一个任务。")
            conn.execute("UPDATE agent_lab_golden_jobs SET cancel_requested = 0 WHERE job_id = ?", (job["jobId"],))
            if reprocess:
                # Re-parse the original completed model outputs. This mode
                # never admits another model request, even if its cache is gone.
                job["reprocessOnly"] = True
            if retry:
                retries = job.setdefault('requestRetries', {})
                base = next((key for key,item in retries.items() if item.get('requestId') == retry), retry)
                attempt = int(retries.get(base, {}).get('attempt', 0)) + 1
                receipt = {'requestId':f'{base}:retry:{attempt}', 'attempt':attempt,
                           'previousRequestId':retry,'createdAtMs':_now()}
                retries[base] = receipt
                job.setdefault('retryHistory', []).append({'baseRequestId':base, **receipt})
            job.update(state="queued", progress="等待恢复", error="")
        self._save_job(conn, job)
        return job

    def job_input(self, job_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connection() as conn:
            row = self._job_row(conn, _text(job_id, "任务标识", limit=240))
            return {**json.loads(row["input_json"]), "job": json.loads(row["payload_json"]),
                    "cancelRequested": bool(row["cancel_requested"])}

    def application_configuration(self, suite_id: str, job_id: str, variant: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Read a completed tested arm without exposing Golden cases or answers.

        The project application owner still verifies its own suite binding.
        Candidate prompts come from the executed selection, not a newer draft.
        """
        variant = _choice(variant, "评测方案", {"baseline", "candidate"})
        suite_id = _text(suite_id, "评测集标识", limit=240)
        if connection is None:
            self.initialize()
        # App preparation already holds the project transaction. Reuse its
        # committed schema and read snapshot rather than start another writer.
        with nullcontext(connection) if connection is not None else self._connection() as conn:
            row = self._job_row(conn, _text(job_id, "任务标识", limit=240))
            job = json.loads(row["payload_json"])
            if row["suite_id"] != suite_id or job["kind"] != "experiment" or job["state"] != "completed":
                raise AgentLabGoldenValidationError("应用配置需要本评测集已完成的对照实验。")
            bound, result = json.loads(row["input_json"]), job.get("result") or {}
            selected = _object(result.get(variant), "已执行方案")
            configuration = _model(selected, {}, required=True)
            if selected.get("applicationMethod"):
                configuration["applicationMethod"] = validated_application_method(selected["applicationMethod"])
            if configuration.get("applicationMethod") != bound["input"][variant].get("applicationMethod"):
                raise AgentLabGoldenValidationError("已执行方法与原实验冻结正文不一致。")
            knowledge = copy.deepcopy(bound["snapshot"].get("knowledge"))
            metrics = {phase: {key: result.get(phase, {}).get(variant + "Metrics", {}).get(key)
                              for key in ("passed", "failed", "uncertain", "runtimeErrors", "total", "passRate")}
                       for phase in ("development", "holdout")}
            return {"suiteId": suite_id, "jobId": job_id, "variant": variant,
                    "snapshotId": bound["snapshot"]["snapshotId"],
                    "projectId": knowledge.get("projectId") if knowledge else None,
                    "configuration": configuration, "configurationSha256": hashlib.sha256(_json(configuration).encode()).hexdigest(),
                    "knowledge": knowledge, "summary": {"state": "completed", "optimizationScope": result.get("optimizationScope"),
                    "qualityDecision": result.get("comparison", {}).get("decision"), "metrics": metrics}}

    def begin_validation(self, job_id: str) -> dict[str, Any]:
        """Record entry into held-out validation, once per logical job.

        This records local execution history, not whether a person has seen
        the questions elsewhere. Failed validation remains a prior exposure.
        """
        self.initialize()
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._job_row(conn, _text(job_id, '任务标识', limit=240))
            job = json.loads(row['payload_json'])
            if isinstance(job.get('validationUse'), dict): return copy.deepcopy(job['validationUse'])
            if job['kind'] != 'experiment' or job['state'] != 'running' or row['cancel_requested']:
                raise AgentLabGoldenConflict('当前任务不能进入留出题验证。')
            snapshot = json.loads(row['input_json'])['snapshot']; snapshot_id = snapshot['snapshotId']
            questions = {' '.join(item['question'].split()).casefold() for item in snapshot['cases'] if item['split']=='holdout'}
            prior_started = prior_completed = 0; overlapping: set[str] = set()
            prior_rows = conn.execute('''SELECT state, json_extract(input_json,'$.snapshot.cases')
                FROM agent_lab_golden_jobs
                WHERE suite_id=? AND kind='experiment' AND job_id<>?
                AND (json_type(payload_json,'$.validationUse')='object'
                    OR json_type(payload_json,'$.result.holdout')='object'
                    OR EXISTS (SELECT 1 FROM json_each(payload_json,'$.result.receipts') AS receipt
                        WHERE json_extract(receipt.value,'$.split')='holdout'))''',
                (row['suite_id'],job_id)).fetchall()
            for prior_state, encoded_cases in prior_rows:
                previous_questions = {' '.join(item['question'].split()).casefold() for item in json.loads(encoded_cases or '[]') if item.get('split')=='holdout'}
                shared = questions & previous_questions
                if shared:
                    overlapping.update(shared); prior_started += 1; prior_completed += prior_state=='completed'
            usage = {'snapshotId':snapshot_id,'ordinal':prior_started+1,'priorStartedRuns':prior_started,
                     'priorCompletedRuns':prior_completed,'reused':bool(overlapping),'overlappingQuestionCount':len(overlapping),
                     'totalQuestions':len(questions),'startedAtMs':_now(),
                     'scope':'local_lab_validation_entry'}
            job['validationUse'] = usage; self._save_job(conn,job)
            return copy.deepcopy(usage)

    def update_job(self, job_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        patch = _object(patch, "任务进度")
        if set(patch) - {"state", "progress", "sessionId", "error", "result"}:
            raise AgentLabGoldenValidationError("任务进度包含不允许修改的字段。")
        if "state" in patch:
            _choice(patch["state"], "任务状态", {"running", "failed", "cancelled", "interrupted"})
        for key in ("progress", "sessionId", "error"):
            if key in patch:
                _text(patch[key], "任务进度", optional=True)
        if "result" in patch and patch["result"] is not None:
            _object(patch["result"], "任务结果")
        _json(patch)
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._job_row(conn, _text(job_id, "任务标识", limit=240))
            job = json.loads(row["payload_json"])
            if job["state"] in _TERMINAL:
                return job
            if row["cancel_requested"] and patch.get("state") == "running":
                return job
            if patch.get("state") == "running":
                conn.execute("UPDATE agent_lab_golden_jobs SET execution_started = 1 WHERE job_id = ?", (job_id,))
            job.update(copy.deepcopy(patch))
            self._save_job(conn, job)
            return job

    def finish_job(self, job_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(_object(result, "任务结果"))
        _json(result)
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._job_row(conn, _text(job_id, "任务标识", limit=240))
            job = json.loads(row["payload_json"])
            if job["state"] in _TERMINAL:
                return job
            if row["cancel_requested"]:
                raise AgentLabGoldenConflict("任务已请求停止，不能再录入成功结果。")
            if job["state"] != "running":
                raise AgentLabGoldenConflict("任务尚未开始执行，不能录入完成结果。")
            bound = json.loads(row["input_json"])
            suite = self._suite(conn, row["suite_id"])
            if job["kind"] in {"draft", "review", "calibrate"}:
                if suite["revision"] != bound["suite"]["revision"]:
                    raise AgentLabGoldenConflict("执行期间 Golden 标准已更新，旧结果不能覆盖新的人工审核。")
                if job["kind"] == "draft":
                    cases = _draft_cases(result.get("cases"), bound["suite"]["sources"], bound["suite"]["targetCount"])
                    # A fresh draft may replace pending proposals, never previous
                    # explicit human decisions, even at the same input revision.
                    reviewed = [item for item in suite["cases"] if item["review"]["status"] != "pending"]
                    reviewed_ids = {item["caseId"] for item in reviewed}
                    suite["cases"] = reviewed + [item for item in cases if item["caseId"] not in reviewed_ids]
                    result["cases"] = cases
                    suite["revision"] += 1
                    suite["calibration"] = None
                elif job["kind"] == "review":
                    self._finish_review(suite, bound, result, job['jobId'])
                else:
                    if result.get("judgeProtocolVersion", GOLDEN_JUDGE_PROTOCOL_VERSION) != GOLDEN_JUDGE_PROTOCOL_VERSION:
                        raise AgentLabGoldenValidationError("评审协议已变化，请按当前协议重新校准。")
                    previous_snapshot = suite.get('snapshot') or {}
                    if previous_snapshot.get('sourceRevision') == suite['revision'] and previous_snapshot.get('judgeProtocolVersion') != GOLDEN_JUDGE_PROTOCOL_VERSION:
                        # The scoring protocol is part of a standard version.
                        # Keep the old snapshot immutable and give the new
                        # calibrated protocol a distinct revision to freeze.
                        suite['revision'] += 1
                    suite["calibration"] = _calibration({**bound["suite"], 'revision':suite['revision']}, result.get("judgments"))
                    result["judgments"] = suite["calibration"]["judgments"]
                    result["calibration"] = suite["calibration"]
                self._save_suite(conn, suite)
            else:
                snapshot = bound["snapshot"]
                expected_mode = "knowledge_qa" if snapshot.get("knowledge") else "context_qa"
                if (result.get("snapshotId") != snapshot["snapshotId"] or result.get("suiteId") != suite["suiteId"]
                        or result.get("executionMode") != expected_mode or result.get("optimizationScope") != optimization_scope(result.get("baseline", {}), result.get("candidate", {}), bool(bound["input"].get("optimizePrompt", True)))
                        or (snapshot.get("knowledge") and result.get("knowledge") != snapshot["knowledge"])
                        or result.get("judgeProtocolVersion", GOLDEN_JUDGE_PROTOCOL_VERSION) != snapshot["judgeProtocolVersion"]):
                    raise AgentLabGoldenValidationError("实验结果必须绑定本次冻结版本和实际优化范围。")
                method_bound = any(bound["input"][variant].get("applicationMethod") for variant in ("baseline", "candidate"))
                method_claimed = any(result.get(variant, {}).get("applicationMethod") for variant in ("baseline", "candidate"))
                if method_bound or method_claimed or result.get("applicationMethodComparison"):
                    for variant in ("baseline", "candidate"):
                        if result.get(variant, {}).get("applicationMethod") != bound["input"][variant].get("applicationMethod"):
                            raise AgentLabGoldenValidationError("实验应用 Skill 必须与本次准入冻结正文一致。")
                    if result.get("applicationMethodComparison") != application_method_comparison(result["baseline"], result["candidate"]):
                        raise AgentLabGoldenValidationError("应用 Skill 差异必须来自实际冻结正文。")
                    answer_receipts = {item.get("requestId"): item for item in result.get("receipts", [])
                                       if isinstance(item, Mapping) and item.get("stage") == "answer"}
                    if method_bound and not answer_receipts:
                        raise AgentLabGoldenValidationError("应用 Skill 对照不能只提交方法元数据，必须有实际作答回执。")
                    for phase in ("development", "holdout"):
                        # The execution owner uses canonical case-id order;
                        # source-import order is not a coverage requirement.
                        expected_cases = sorted(case["caseId"] for case in snapshot["cases"] if case["split"] == phase)
                        if [case.get("caseId") for case in result.get(phase, {}).get("cases", [])] != expected_cases:
                            raise AgentLabGoldenValidationError("应用 Skill 成绩必须覆盖相同冻结题目，不能替换或省略案例。")
                        for case in result.get(phase, {}).get("cases", []):
                            for variant in ("baseline", "candidate"):
                                expected_method = application_method_identity(bound["input"][variant])
                                answer = case.get(variant, {})
                                receipt = answer_receipts.get(answer.get("requestId"), {})
                                if expected_method and (answer.get("applicationMethod") != expected_method
                                        or receipt.get("applicationMethod") != expected_method
                                        or receipt.get("variant") != variant or receipt.get("caseId") != case.get("caseId")
                                        or receipt.get("split") != phase
                                        or not isinstance(receipt.get("promptSha256"), str) or len(receipt["promptSha256"]) != 64):
                                    raise AgentLabGoldenValidationError("应用 Skill 成绩缺少对应作答正文身份与执行回执。")
                if isinstance(job.get('validationUse'), dict): result['validationUse'] = copy.deepcopy(job['validationUse'])
            job.update(state="completed", progress="已完成", error="", result=result)
            self._save_job(conn, job)
            return job

    def recover_interrupted_jobs(self) -> list[dict[str, Any]]:
        """Called by the application once when taking ownership after restart."""
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            affected = []
            for row in conn.execute("SELECT payload_json FROM agent_lab_golden_jobs WHERE state IN ('queued', 'running') ORDER BY created_at_ms, rowid").fetchall():
                job = json.loads(row[0])
                job.update(state="interrupted", progress="执行已中断，可恢复原任务", error="执行进程已离开，尚未收到完整结果。")
                self._save_job(conn, job)
                affected.append(job)
            return affected
