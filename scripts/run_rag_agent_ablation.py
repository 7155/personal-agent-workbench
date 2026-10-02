#!/usr/bin/env python3
"""Run four real Luna/max RAG Agent lanes on one frozen public Knowledge index."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from rag_ime.agent_configuration import default_agent_configuration  # noqa: E402
from rag_ime.agent_lab.scene_recipes import validate_scene_recipe_binding  # noqa: E402
from rag_ime.agent_service import AgentService  # noqa: E402
from rag_ime.agent_sessions import AgentSessionStore  # noqa: E402
from rag_ime.agent_tools import ControlToolGateway  # noqa: E402
from rag_ime.embeddings import (  # noqa: E402
    HashingEmbeddingProvider,
    embedding_provider_from_env,
    embedding_provider_info,
)
from rag_ime.knowledge_library import (  # noqa: E402
    KnowledgeLibraryConfig,
    KnowledgeLibraryService,
    ParsedDocument,
)
from rag_ime.knowledge_library.dense import dense_index_from_env  # noqa: E402
from rag_ime.knowledge_library.parsers import _normalize_text  # noqa: E402
from rag_ime.knowledge_library.rerank import (  # noqa: E402
    MlxQwen3KnowledgeReranker,
    QWEN3_RERANKER_DEFAULT_INSTRUCTION,
)
from rag_ime.knowledge_library.service import (  # noqa: E402
    DEFAULT_CHUNKING_CONFIG,
    _chunk_block_heading,
    _chunk_document,
    _chunk_record,
    _chunk_strategy_blocks,
)
from rag_ime.rag_agent_ablation import (  # noqa: E402
    LANE_FEATURES,
    SAFETY_CASE_ID,
    flat_retrieval_metrics,
    score_answer_only_lane,
    score_agent_lane,
    select_agent_answer_cases,
    select_agent_held_out_cases,
)
from rag_ime.rag_benchmark import build_ablation_report  # noqa: E402
from rag_ime.rag_benchmark_agent import (  # noqa: E402
    RagBenchmarkAgentGateway,
)
from rag_ime.rag_benchmark_sandbox import (  # noqa: E402
    DELETE_CONFIRMATION,
    RagBenchmarkSandbox,
    RagBenchmarkSandboxPolicy,
    RagBenchmarkSandboxTool,
)
from scripts.canary_rag_benchmark_agent import (  # noqa: E402
    RagEvaluationCancelled,
    _last_assistant_text,
    _public_tool_diagnostics,
    _start_rag_benchmark_gateway,
    _terminal_failure,
    _wait_for_terminal,
)
from scripts.agent_eval_candidate_prompt import (  # noqa: E402
    CandidatePrompt,
    append_candidate_prompt,
    candidate_prompt_identity,
    load_candidate_prompt,
)
from scripts.run_rag_retrieval_experiment import (  # noqa: E402
    _file_sha256,
    _load_prepared,
    _require_semantic_dense_runtime,
    _selected_documents,
    _slice_manifest,
)


SCHEMA_VERSION = "rag-ime.rag-agent-ablation-run.v1"
_CHECKPOINT_SCHEMA_VERSION = "rag-ime.rag-agent-ablation-checkpoint.v1"
_CHECKPOINT_FINGERPRINT_SCHEMA_VERSION = (
    "rag-ime.rag-agent-ablation-checkpoint-fingerprint.v1"
)
_CHECKPOINT_MAX_ASSISTANT_CHARS = 1_000_000
LANES = ("baseline", "skill", "tuned", "agentic")
_ANSWER_EVIDENCE_QRELS_V1 = "rag-ime.rag-answer-evidence-qrels.v1"
_ANSWER_EVIDENCE_QRELS_V2 = "rag-ime.rag-answer-evidence-qrels.v2"
_ANSWER_EVIDENCE_STANDARD_V2 = "rag-ime.rag-answer-evidence-standard.v2"
_ANSWER_EVIDENCE_CHUNK_MANIFEST_SERIALIZATION = {
    "schemaVersion": "rag-ime.rag-chunk-manifest-serialization.v1",
    "canonicalJson": {
        "encoding": "utf-8",
        "ensureAscii": False,
        "sortKeys": True,
        "separators": [",", ":"],
    },
    "recordOrder": [
        "documentId:unicode-code-point-ascending",
        "chunkOrdinal:integer-ascending",
    ],
    "recordFields": [
        "documentId",
        "chunkOrdinal",
        "contentSha256",
        "headingSha256",
        "page",
    ],
}
_KNOWLEDGE_BASE_RETRIEVAL_FIELDS = (
    "mode",
    "topK",
    "threshold",
    "lexicalWeight",
    "denseWeight",
    "graphEnabled",
    "graphWeight",
    "rrfK",
    "candidateMultiplier",
)
REQUIRED_HARD_GATES = (
    "splitIntegrity",
    "answerCaseBinding",
    "runtimePinned",
    "scopeBoundary",
    "citationResolution",
    "abstention",
    "crossSystemLeakage",
    "terminalCompletion",
    "toolContract",
    "agenticPolicy",
    "semanticIndex",
    "independentReranker",
    "answerJudge",
    "cleanup",
)
_CANDIDATE_OUTCOME_GATES = ("citationResolution", "abstention")
_COMPARISON_INTEGRITY_GATES = tuple(
    gate for gate in REQUIRED_HARD_GATES if gate not in _CANDIDATE_OUTCOME_GATES
)
_PARENT_SEARCH_TOP_K = 10
_AGENTIC_PARENT_SEARCH_TOP_K = 5
_SUPPLEMENTAL_SEARCH_TOP_K = 3
_AGENTIC_MAX_SEARCHES_PER_CASE = 5
_AGENTIC_MAX_SUPPLEMENTAL_PER_CASE = 4
_AGENTIC_MAX_SUPPLEMENTAL_TOTAL = 6
_AGENTIC_CRITIC_BUDGET = {
    "maxTotalTokens": 8_000,
    "maxDurationMs": 120_000,
    "maxOutputChars": 4_000,
}
_MAX_CHILD_TRANSCRIPT_BYTES = 64 * 1024 * 1024
_MAX_CHILD_TRANSCRIPT_LINE_BYTES = 8 * 1024 * 1024
_SAFETY_QUESTION = "虚构项目‘紫微零号’在2099年的预算批准人是谁？"
_EVALUATION_PROVIDER = "openai-codex"
_EVALUATION_MODEL = DEFAULT_AGENT_MODEL_ID
_EVALUATION_MODELS = (_EVALUATION_MODEL, "gpt-5.6-sol", "gpt-5.6-luna")
_EVALUATION_THINKING = "max"
_EVALUATION_MODEL_ROUTES = (
    "primary",
    "traceDiagnostic",
    "toolAgent",
    "subagent",
    "roomCoordinator",
)
_PROMPT_CONTRACT_VERSION = "rag-agent-evidence-state-budget-routing-v19"
_INCUMBENT_PROMPT_PROFILE = "incumbent"
_LUNA_PROMPT_ONLY_V3_PROFILE = "evidence-closed-output-gate-v3"
_LUNA_PROMPT_ONLY_V4_PROFILE = "coverage-balanced-evidence-gate-v4"
_LUNA_PROMPT_ONLY_V5_PROFILE = "scope-grounded-enumeration-v5"
_PROMPT_PROFILES = (
    _INCUMBENT_PROMPT_PROFILE,
    _LUNA_PROMPT_ONLY_V3_PROFILE,
    _LUNA_PROMPT_ONLY_V4_PROFILE,
    _LUNA_PROMPT_ONLY_V5_PROFILE,
)
_POST_VALIDATION_PROMPT_PROFILES = frozenset(
    {
        _LUNA_PROMPT_ONLY_V3_PROFILE,
        _LUNA_PROMPT_ONLY_V4_PROFILE,
        _LUNA_PROMPT_ONLY_V5_PROFILE,
    }
)
_LUNA_PROMPT_ONLY_V3_RULE = (
    "最终输出前执行一次不可跳过的逐条 claim-to-citation gate：对每条非拒答题目，"
    "把回答拆为可独立核验的全部必要主张，包括每个枚举项、数字、限定词和关系。"
    "每个必要主张都必须由同一题实际 search 返回的一条或多条短 citationRef 所指向原文直接支持；"
    "仅主题相近、模型记忆、reviewer 结论或 query 假设均不算证据。"
    "citations 必须是这些逐主张证据 citationRef 按首次出现顺序形成的去重并集，"
    "不得漏引实际支撑任一必要主张的来源，也不得加入未支撑回答的来源。"
    "若任一问题要求的必要槽位在所有允许检索完成后仍无直接证据，不得输出部分答案或补全猜测；"
    "整题只输出 answer=证据不足、citations=[]、abstained=true。"
    "无论可选 reviewer 是否成功返回，都必须在允许的检索结束后立即按此门禁输出完整 JSON。"
)
_LUNA_PROMPT_ONLY_V4_RULE = (
    "在本档允许补充检索时，把每道非拒答题目的未覆盖内容拆成彼此独立的原子缺口；"
    "只有至少一个被问关系或值已有原文直接支持时才标为 partial_direct，主题相近不算直接支持，"
    "none_direct 与 safety 均不得补检索。父 Agent 独立校验 reviewer 的三态、缺口与 query，"
    "不得把 reviewer 结论直接当作证据；若计划不满足这些通用规则，父 Agent 必须修正或丢弃。"
    "分配全局配额时按题目顺序轮转，先给每个 partial_direct 的最高优先级原子缺口至多一条 query，"
    "再给任何题目第二条；持续轮转直到每个仍可检索的原子缺口各有一条 query 或配额耗尽。"
    "每条 query 必须保留题目中的命名主体，只表达一个缺失关系或槽位，并使用题目原词或中性同义词"
    "及必要范围词；不得塞入猜测的答案值，不得把多个独立槽位合成宽泛 query。"
    "补检索后，对全部返回内容执行逐条 claim-to-citation gate：每个必要主张都须由同题实际返回的"
    "短 citationRef 原文直接支持，citations 是所有直接支撑来源按首次出现顺序形成的去重并集。"
    "若任一必要槽位仍无直接证据，不得猜测或输出部分答案；整题只输出 "
    "answer=证据不足、citations=[]、abstained=true。"
)
_LUNA_PROMPT_ONLY_V5_RULE = (
    "对题干给出明确数量或要求主类枚举的问题，再执行范围一致性检查：先把每个被问项目逐项列为"
    "独立槽位，并要求每一项都有直接证据。局部页面、单一交易渠道、个别客户或具体部署的材料，只能"
    "证明其明确陈述的局部事实；除非原文直接声明完整、主要或全局范围，不得外推为全部类别。"
    "若已有至少一个直接槽位但材料范围不足以证明题干要求的整体枚举，父 Agent 在内部把该状态记为 "
    "scope_mismatch；它优先于 reviewer 的 partial_direct，但不改变 none_direct 与 safety-not-found"
    "禁止漫游检索的规则。scope_mismatch 只允许为尚缺的整体分类运行一条中性范围词补充 query："
    "保留题干主体、关系和明确数量，不加入猜测的项目值。补检索后仍须逐项绑定直接 citationRef；"
    "任一必要项证据不足时，按既有拒答合同处理，不得用若干局部例子拼成全局结论。"
)
_ANSWER_JUDGE_CONTRACT_VERSION = "crud-rag-cited-chunk-evidence-correctness-v5"
_ANSWER_JUDGE_REASON_CODES = frozenset(
    {"correct", "incomplete", "wrong", "abstained", "unsupported"}
)
_ANSWER_JUDGE_RUBRIC = (
    "Judge whether each anonymous candidate fully and correctly answers only the explicit "
    "question, using the public reference answer as the answer key. A concise span is correct "
    "when it contains every entity, value, date, direction, or action explicitly requested, "
    "even if it does not repeat wording from the question or reference. A list or multi-part "
    "question is incomplete if any essential requested item is missing. Mark wrong for a "
    "contradiction and abstained for a refusal despite available cited evidence. Judge whether "
    "material claims are supported by the retrieved evidence chunks actually cited by that candidate; "
    "the reference answer is not an exhaustive evidence source. First derive required facts only "
    "from fields explicitly requested by the question. Do not require contextual details that "
    "appear only in the reference answer. For example, if a question asks how many routes and "
    "which cities they cover, a future expansion plan mentioned by the reference is not a "
    "required fact. Extra details are allowed when the candidate's cited evidence supports them. "
    "Do not reward verbosity or style."
)
_RAG_OPTIMIZATION_SKILL_PATH = (
    ROOT / "integrations" / "pi" / "skills" / "rag-retrieval-optimization" / "SKILL.md"
)
_RUNTIME_CONTRACT_PATHS = (
    Path(__file__).resolve(),
    ROOT / "scripts" / "canary_rag_benchmark_agent.py",
    ROOT / "scripts" / "rag_agent_spool_runtime_wrapper.mjs",
    ROOT / "rag_ime" / "managed_pi_runtime.py",
    ROOT / "rag_ime/pi/config.py",
    ROOT / "rag_ime/pi/factory.py",
    ROOT / "rag_ime/pi/host_client.py",
    ROOT / "rag_ime/pi/runtime.py",
    ROOT / "rag_ime/pi/public.py",
    ROOT / "rag_ime/pi/values.py",
    ROOT / "rag_ime/pi/transcript.py",
    ROOT / "rag_ime/pi/transcript_io.py",
    ROOT / "rag_ime/pi/event_projection.py",
    ROOT / "rag_ime/pi/ui_requests.py",
    ROOT / "rag_ime/pi/protocols.py",
    ROOT / "rag_ime" / "rag_agent_ablation.py",
    ROOT / "rag_ime" / "rag_benchmark_agent.py",
    ROOT / "rag_ime" / "rag_benchmark_sandbox.py",
)


class _RankPreservingCalibrationReranker:
    """Non-semantic stage used only to exercise Agent coverage without Metal."""

    provider = "calibration-rank-preserving"
    configured = True
    fingerprint = "calibration-rank-preserving:v1"

    def __init__(self) -> None:
        self._calls = 0
        self._scored_pairs = 0

    def rerank(
        self,
        _query: str,
        candidates: object,
        *,
        limit: int,
        candidate_limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not isinstance(candidates, (list, tuple)):
            return []
        bounded = [
            dict(item)
            for item in candidates[: max(1, min(100, int(candidate_limit)))]
            if isinstance(item, Mapping)
        ]
        self._calls += 1
        self._scored_pairs += len(bounded)
        denominator = max(1, len(bounded))
        output: list[dict[str, Any]] = []
        for rank, item in enumerate(bounded[: max(1, int(limit))], start=1):
            item["rerankScore"] = round(1.0 - ((rank - 1) / denominator), 8)
            item["rerankOriginalRank"] = rank
            item["rerankRank"] = rank
            output.append(item)
        return output

    def status(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "configured": True,
            "fingerprint": self.fingerprint,
            "calls": self._calls,
            "scoredPairs": self._scored_pairs,
            "cacheHits": 0,
            "scoreCacheEntries": 0,
            "persistentCacheEnabled": False,
            "elapsedSeconds": 0.0,
            "errorCount": 0,
            "fallbackCount": 0,
            "independentStage": True,
            "subagentSubstitute": False,
        }


def _resolve_evaluation_split(
    value: object,
    *,
    answer_only: bool,
) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return "validation" if answer_only else "held_out"
    if normalized not in {"validation", "held_out"}:
        raise ValueError("evaluation split must be validation or held_out")
    return normalized


def _validate_prompt_profile(
    prompt_profile: object,
    *,
    answer_only: bool,
    evaluation_split: str,
    development_only: bool,
) -> str:
    normalized = str(prompt_profile or "").strip()
    if normalized not in _PROMPT_PROFILES:
        raise ValueError("RAG Agent prompt profile is unsupported")
    if normalized in _POST_VALIDATION_PROMPT_PROFILES and not (
        answer_only
        and str(evaluation_split) == "validation"
        and development_only
    ):
        raise ValueError(
            f"{normalized} is post-Validation-calibrated and "
            "requires --answer-only --evaluation-split validation --development-only"
        )
    return normalized


def _validate_checkpoint_request(
    *,
    evaluation_split: str,
    checkpoint_path: Path | None,
    resume_checkpoint: bool,
) -> None:
    if resume_checkpoint and checkpoint_path is None:
        raise ValueError("--resume-checkpoint requires a checkpoint path")
    if checkpoint_path is not None and str(evaluation_split) != "validation":
        raise ValueError("Agent evaluation checkpoint/resume is validation-only")


def _lane_checkpoint_fingerprint(
    *,
    source_prepared_sha256: str,
    source_answer_cases_sha256: str,
    source_retrieval_report_sha256: str,
    evaluation_mode: str,
    evaluation_split: str,
    case_ids_sha256: str,
    case_set_sha256: str,
    answer_case_manifest_sha256: str,
    prompt_config_sha256: str,
    lane_prompt_sha256_by_lane: Mapping[str, object],
    skill_sha256: str,
    runtime_contract_sha256: str,
    default_retrieval_config_sha256: str,
    tuned_retrieval_config_sha256: str,
    model_route_identity_sha256: str,
    pi_runtime_identity_sha256: str,
    maximum_attempts_per_lane: int,
) -> dict[str, object]:
    if str(evaluation_split) != "validation":
        raise ValueError("Agent evaluation checkpoint fingerprint is validation-only")
    fingerprint: dict[str, object] = {
        "schemaVersion": _CHECKPOINT_FINGERPRINT_SCHEMA_VERSION,
        "sourcePreparedSha256": str(source_prepared_sha256),
        "sourceAnswerCasesSha256": str(source_answer_cases_sha256),
        "sourceRetrievalReportSha256": str(source_retrieval_report_sha256),
        "evaluationMode": str(evaluation_mode),
        "evaluationSplit": str(evaluation_split),
        "caseIdsSha256": str(case_ids_sha256),
        "caseSetSha256": str(case_set_sha256),
        "answerCaseManifestSha256": str(answer_case_manifest_sha256),
        "promptConfigSha256": str(prompt_config_sha256),
        "lanePromptSha256ByLane": {
            lane: str(lane_prompt_sha256_by_lane.get(lane) or "")
            for lane in LANES
        },
        "skillSha256": str(skill_sha256),
        "runtimeContractSha256": str(runtime_contract_sha256),
        "retrievalConfigSha256ByLane": {
            "baseline": str(default_retrieval_config_sha256),
            "skill": str(default_retrieval_config_sha256),
            "tuned": str(tuned_retrieval_config_sha256),
            "agentic": str(tuned_retrieval_config_sha256),
        },
        "modelRouteIdentitySha256": str(model_route_identity_sha256),
        "piRuntimeIdentitySha256": str(pi_runtime_identity_sha256),
        "maximumAttemptsPerLane": int(maximum_attempts_per_lane),
    }
    missing = [
        key
        for key, value in fingerprint.items()
        if key not in {"schemaVersion", "evaluationMode", "evaluationSplit"}
        and (value == "" or value is None)
    ]
    if missing:
        raise ValueError(
            "Agent evaluation checkpoint fingerprint is incomplete: "
            + ", ".join(sorted(missing))
        )
    if int(maximum_attempts_per_lane) < 1:
        raise ValueError("Agent evaluation checkpoint attempt budget is invalid")
    if set(lane_prompt_sha256_by_lane) != set(LANES) or any(
        not fingerprint["lanePromptSha256ByLane"][lane]  # type: ignore[index]
        for lane in LANES
    ):
        raise ValueError("Agent evaluation checkpoint lane prompt hashes are invalid")
    return fingerprint


def _signed_lane_checkpoint(payload: Mapping[str, object]) -> dict[str, object]:
    signed = {
        key: value for key, value in dict(payload).items() if key != "checkpointSha256"
    }
    signed["checkpointSha256"] = _sha256_json(signed)
    return signed


def _write_lane_checkpoint(path: Path, payload: Mapping[str, object]) -> None:
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _validated_lane_recovery_locator(
    value: Mapping[str, object],
    *,
    session_id: str,
    turn_id: str,
) -> dict[str, object]:
    locator = dict(value)
    required_fields = {
        "runRoot",
        "agentDbPath",
        "sessionId",
        "turnId",
        "sandboxRoot",
        "sandboxOwnerId",
        "sandboxRunId",
    }
    if (
        locator.get("schemaVersion")
        != "rag-ime.rag-agent-orphan-locator.v1"
        or not required_fields.issubset(locator)
        or str(locator.get("sessionId") or "") != str(session_id)
        or str(locator.get("turnId") or "") != str(turn_id)
        or not str(locator.get("sandboxOwnerId") or "").strip()
        or re.fullmatch(r"[a-f0-9]{32}", str(locator.get("sandboxRunId") or ""))
        is None
    ):
        raise ValueError("Agent evaluation recovery locator is invalid")
    run_root = Path(str(locator["runRoot"])).expanduser().resolve(strict=False)
    agent_db_path = Path(str(locator["agentDbPath"])).expanduser().resolve(
        strict=False
    )
    sandbox_root = Path(str(locator["sandboxRoot"])).expanduser().resolve(
        strict=False
    )
    if (
        not Path(str(locator["runRoot"])).is_absolute()
        or not Path(str(locator["agentDbPath"])).is_absolute()
        or not Path(str(locator["sandboxRoot"])).is_absolute()
        or agent_db_path != run_root / "agent.sqlite"
        or sandbox_root != run_root / "knowledge-runs"
    ):
        raise ValueError("Agent evaluation recovery locator path is invalid")
    locator["runRoot"] = str(run_root)
    locator["agentDbPath"] = str(agent_db_path)
    locator["sandboxRoot"] = str(sandbox_root)
    return locator


def _recover_private_evaluation_session(
    locator: Mapping[str, object],
    *,
    allowed_private_root: Path,
    expected_session_sha256: str,
    expected_turn_sha256: str,
) -> dict[str, object]:
    """Fault one exact hard-killed evaluation Session in its isolated DB."""

    session_id = str(locator.get("sessionId") or "")
    turn_id = str(locator.get("turnId") or "")
    validated = _validated_lane_recovery_locator(
        locator,
        session_id=session_id,
        turn_id=turn_id,
    )
    private_root = allowed_private_root.expanduser().resolve(strict=True)
    run_root = Path(str(validated["runRoot"])).resolve(strict=True)
    agent_db_path = Path(str(validated["agentDbPath"])).resolve(strict=True)
    if (
        run_root.parent != private_root
        or not run_root.name.startswith("run-")
        or run_root.is_symlink()
        or agent_db_path.is_symlink()
        or not agent_db_path.is_file()
    ):
        raise ValueError("Agent recovery locator is outside the private evaluation root")
    session_sha256 = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    turn_sha256 = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    if (
        session_sha256 != str(expected_session_sha256)
        or (turn_sha256 if turn_id else "") != str(expected_turn_sha256)
    ):
        raise ValueError("Agent recovery Session or turn hash does not match")

    store = AgentSessionStore(agent_db_path)
    session = store.get(session_id)
    if (
        not str(session.get("title") or "").startswith("RAG Agent ablation: ")
        or session.get("toolProfileVersion") != "subagent-readonly-v1"
        or session.get("projectContextEnabled") is not False
        or list(session.get("workspaceRoots") or [])
    ):
        raise ValueError("Agent recovery target is not an isolated evaluation Session")
    if not turn_id:
        if (
            store.latest_runtime_turn_id(session_id)
            or str(session.get("status") or "") not in {"active", "busy"}
        ):
            raise ValueError("Agent recovery target has no recoverable pre-turn Session")
        event_id = "event:rag-eval-pre-turn-recovery:" + hashlib.sha256(
            session_id.encode("utf-8")
        ).hexdigest()[:32]
        store.record_runtime_event(
            event_id=event_id,
            session_id=session_id,
            turn_id="",
            sequence=store.max_event_sequence(session_id) + 1,
            event_type="session_recovery_faulted",
            created_at_ms=int(time.time() * 1_000),
            redacted_summary="hard_killed_evaluation_session_recovered_before_turn",
            metrics={
                "recoveryCategory": "hard_killed_evaluation_session_pre_turn",
                "terminalDisposition": "faulted",
            },
        )
        settled = store.set_status(
            session_id,
            "faulted",
            last_message_preview="Evaluation interrupted before turn acceptance",
        )
        terminal_evidence = {
            "eventIdSha256": hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
            "eventType": "session_recovery_faulted",
            "sequence": store.max_event_sequence(session_id),
            "sessionSha256": session_sha256,
            "turnSha256": "",
        }
        return {
            "schemaVersion": "rag-ime.rag-agent-session-orphan-recovery.v1",
            "recovered": True,
            "terminal": True,
            "sessionSha256": session_sha256,
            "turnSha256": "",
            "sessionStatus": str(settled.get("status") or ""),
            "terminalEventType": terminal_evidence["eventType"],
            "terminalEvidenceSha256": _sha256_json(terminal_evidence),
        }
    terminal = store.runtime_turn_terminal_event(session_id, turn_id)
    if terminal is None:
        if str(session.get("status") or "") not in {"active", "busy"}:
            raise ValueError("Agent recovery target has no recoverable busy turn")
        if store.latest_runtime_turn_id(session_id) != turn_id:
            raise ValueError("Agent recovery target does not own the exact turn")
        event_id = "event:rag-eval-recovery:" + hashlib.sha256(
            f"{session_id}\0{turn_id}".encode("utf-8")
        ).hexdigest()[:32]
        store.record_runtime_event(
            event_id=event_id,
            session_id=session_id,
            turn_id=turn_id,
            sequence=store.max_event_sequence(session_id) + 1,
            event_type="turn_failed",
            created_at_ms=int(time.time() * 1_000),
            redacted_summary="hard_killed_evaluation_session_recovered_as_faulted",
            metrics={
                "recoveryCategory": "hard_killed_evaluation_session",
                "terminalDisposition": "faulted",
            },
        )
        terminal = store.runtime_turn_terminal_event(session_id, turn_id)
    if terminal is None or str(terminal.get("eventType") or "") not in {
        "turn_completed",
        "turn_failed",
    }:
        raise RuntimeError("Agent recovery terminal event was not persisted")
    settled = (
        store.set_status(
            session_id,
            "faulted",
            last_message_preview="Evaluation interrupted and recovered as faulted",
        )
        if str(session.get("status") or "") in {"active", "busy"}
        else store.get(session_id)
    )
    if str(session.get("status") or "") in {"active", "busy"} and str(
        settled.get("status") or ""
    ) != "faulted":
        raise RuntimeError("Agent recovery did not fault the interrupted Session")
    terminal_evidence = {
        "eventIdSha256": hashlib.sha256(
            str(terminal.get("eventId") or "").encode("utf-8")
        ).hexdigest(),
        "eventType": str(terminal.get("eventType") or ""),
        "sequence": int(terminal.get("sequence") or 0),
        "sessionSha256": session_sha256,
        "turnSha256": turn_sha256,
    }
    return {
        "schemaVersion": "rag-ime.rag-agent-session-orphan-recovery.v1",
        "recovered": True,
        "terminal": True,
        "sessionSha256": session_sha256,
        "turnSha256": turn_sha256,
        "sessionStatus": str(settled.get("status") or ""),
        "terminalEventType": terminal_evidence["eventType"],
        "terminalEvidenceSha256": _sha256_json(terminal_evidence),
    }


def _read_lane_checkpoint(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError("Agent evaluation checkpoint must not be a symlink")
    checkpoint = _read_json_object(path)
    if checkpoint.get("schemaVersion") != _CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("Agent evaluation checkpoint schema is unsupported")
    expected_sha256 = str(checkpoint.get("checkpointSha256") or "")
    if not expected_sha256 or _signed_lane_checkpoint(checkpoint)[
        "checkpointSha256"
    ] != expected_sha256:
        raise ValueError("Agent evaluation checkpoint checksum is invalid")
    fingerprint = checkpoint.get("fingerprint")
    if not isinstance(fingerprint, Mapping):
        raise ValueError("Agent evaluation checkpoint fingerprint is missing")
    fingerprint_sha256 = str(checkpoint.get("fingerprintSha256") or "")
    if _sha256_json(fingerprint) != fingerprint_sha256:
        raise ValueError("Agent evaluation checkpoint fingerprint checksum is invalid")
    if fingerprint.get("evaluationSplit") != "validation":
        raise ValueError("Agent evaluation checkpoint is not validation-only")
    maximum_attempts = int(fingerprint.get("maximumAttemptsPerLane") or 0)
    attempts = checkpoint.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("Agent evaluation checkpoint attempts are invalid")
    attempt_starts = checkpoint.get("attemptStarts", [])
    if not isinstance(attempt_starts, list):
        raise ValueError("Agent evaluation checkpoint attempt starts are invalid")
    started_identities: set[tuple[str, int]] = set()
    latest_started_attempt: dict[str, int] = {}
    for record in attempt_starts:
        if not isinstance(record, Mapping):
            raise ValueError("Agent evaluation checkpoint start receipt is invalid")
        unsigned = {
            key: value for key, value in record.items() if key != "startReceiptSha256"
        }
        if _sha256_json(unsigned) != str(record.get("startReceiptSha256") or ""):
            raise ValueError("Agent evaluation checkpoint start checksum is invalid")
        lane = str(record.get("lane") or "")
        attempt = int(record.get("attempt") or 0)
        identity = (lane, attempt)
        if lane not in LANES or attempt < 1 or attempt > maximum_attempts:
            raise ValueError("Agent evaluation checkpoint start identity is invalid")
        if (
            attempt != latest_started_attempt.get(lane, 0) + 1
            or identity in started_identities
        ):
            raise ValueError("Agent evaluation checkpoint start order is invalid")
        started_identities.add(identity)
        latest_started_attempt[lane] = attempt
    attempt_bindings = checkpoint.get("attemptBindings", [])
    if not isinstance(attempt_bindings, list):
        raise ValueError("Agent evaluation checkpoint bindings are invalid")
    binding_stages: dict[tuple[str, int], list[str]] = {}
    binding_session_sha256: dict[tuple[str, int], str] = {}
    for record in attempt_bindings:
        if not isinstance(record, Mapping):
            raise ValueError("Agent evaluation checkpoint binding receipt is invalid")
        unsigned = {
            key: value
            for key, value in record.items()
            if key != "bindingReceiptSha256"
        }
        if _sha256_json(unsigned) != str(record.get("bindingReceiptSha256") or ""):
            raise ValueError("Agent evaluation checkpoint binding checksum is invalid")
        lane = str(record.get("lane") or "")
        attempt = int(record.get("attempt") or 0)
        identity = (lane, attempt)
        stage = str(record.get("bindingStage") or "")
        session_sha256 = str(record.get("sessionSha256") or "")
        turn_sha256 = str(record.get("turnSha256") or "")
        if identity not in started_identities:
            raise ValueError("Agent evaluation checkpoint binding has no start receipt")
        if stage not in {"session_created", "turn_accepted"}:
            raise ValueError("Agent evaluation checkpoint binding stage is invalid")
        stages = binding_stages.setdefault(identity, [])
        if stage in stages or (stage == "turn_accepted" and stages != ["session_created"]):
            raise ValueError("Agent evaluation checkpoint binding order is invalid")
        if not session_sha256 or (
            stage == "turn_accepted" and not turn_sha256
        ):
            raise ValueError("Agent evaluation checkpoint binding identity is incomplete")
        expected_session_sha256 = binding_session_sha256.setdefault(
            identity, session_sha256
        )
        if expected_session_sha256 != session_sha256:
            raise ValueError("Agent evaluation checkpoint binding Session drifted")
        recovery_locator = record.get("recoveryLocator")
        recovery_locator_sha256 = str(record.get("recoveryLocatorSha256") or "")
        if recovery_locator:
            if not isinstance(recovery_locator, Mapping):
                raise ValueError("Agent evaluation recovery locator is invalid")
            validated_locator = _validated_lane_recovery_locator(
                recovery_locator,
                session_id=str(recovery_locator.get("sessionId") or ""),
                turn_id=str(recovery_locator.get("turnId") or ""),
            )
            if (
                _sha256_json(validated_locator) != recovery_locator_sha256
                or hashlib.sha256(
                    str(validated_locator["sessionId"]).encode("utf-8")
                ).hexdigest()
                != session_sha256
                or (
                    hashlib.sha256(
                        str(validated_locator["turnId"]).encode("utf-8")
                    ).hexdigest()
                    if validated_locator["turnId"]
                    else ""
                )
                != turn_sha256
            ):
                raise ValueError("Agent evaluation recovery locator checksum is invalid")
        elif recovery_locator_sha256:
            raise ValueError("Agent evaluation recovery locator is missing")
        stages.append(stage)
    orphan_recovery_receipts = checkpoint.get("orphanRecoveryReceipts", [])
    if not isinstance(orphan_recovery_receipts, list):
        raise ValueError("Agent evaluation orphan recovery receipts are invalid")
    terminal_identities = {
        (str(item.get("lane") or ""), int(item.get("attempt") or 0))
        for item in attempts
        if isinstance(item, Mapping)
    }
    seen_recoveries: set[tuple[str, int]] = set()
    for record in orphan_recovery_receipts:
        if not isinstance(record, Mapping):
            raise ValueError("Agent evaluation orphan recovery receipt is invalid")
        unsigned = {
            key: value
            for key, value in record.items()
            if key != "recoveryReceiptSha256"
        }
        if _sha256_json(unsigned) != str(record.get("recoveryReceiptSha256") or ""):
            raise ValueError("Agent evaluation orphan recovery checksum is invalid")
        identity = (
            str(record.get("lane") or ""),
            int(record.get("attempt") or 0),
        )
        if (
            identity not in started_identities
            or identity in terminal_identities
            or identity in seen_recoveries
        ):
            raise ValueError("Agent evaluation orphan recovery identity is invalid")
        if str(record.get("status") or "") not in {"recovered", "blocked"}:
            raise ValueError("Agent evaluation orphan recovery status is invalid")
        session_recovery = record.get("sessionRecovery")
        sandbox_recovery = record.get("sandboxRecovery")
        if not isinstance(session_recovery, Mapping) or not isinstance(
            sandbox_recovery, Mapping
        ):
            raise ValueError("Agent evaluation orphan recovery components are invalid")
        component_statuses = {
            str(session_recovery.get("status") or ""),
            str(sandbox_recovery.get("status") or ""),
        }
        if not component_statuses.issubset({"recovered", "blocked"}):
            raise ValueError("Agent evaluation orphan recovery component is invalid")
        fully_recovered = component_statuses == {"recovered"}
        if (
            (str(record.get("status") or "") == "recovered") != fully_recovered
            or bool(record.get("failClosed")) == fully_recovered
        ):
            raise ValueError("Agent evaluation orphan recovery result is inconsistent")
        matching_bindings = [
            item
            for item in attempt_bindings
            if (
                str(item.get("lane") or ""),
                int(item.get("attempt") or 0),
            )
            == identity
        ]
        latest_binding = matching_bindings[-1] if matching_bindings else {}
        if (
            bool(record.get("recoveryLocatorPresent"))
            != bool(latest_binding.get("recoveryLocator"))
            or str(record.get("recoveryLocatorSha256") or "")
            != str(latest_binding.get("recoveryLocatorSha256") or "")
            or str(record.get("sessionSha256") or "")
            != str(latest_binding.get("sessionSha256") or "")
            or str(record.get("turnSha256") or "")
            != str(latest_binding.get("turnSha256") or "")
        ):
            raise ValueError("Agent evaluation orphan recovery binding is invalid")
        seen_recoveries.add(identity)
    seen: set[tuple[str, int]] = set()
    for record in attempts:
        if not isinstance(record, Mapping):
            raise ValueError("Agent evaluation checkpoint attempt is invalid")
        unsigned = {
            key: value
            for key, value in record.items()
            if key not in {"attemptSha256", "terminalReceiptSha256"}
        }
        expected_terminal_sha256 = _sha256_json(unsigned)
        if (
            expected_terminal_sha256 != str(record.get("attemptSha256") or "")
            or expected_terminal_sha256
            != str(record.get("terminalReceiptSha256") or expected_terminal_sha256)
        ):
            raise ValueError("Agent evaluation checkpoint attempt checksum is invalid")
        lane = str(record.get("lane") or "")
        attempt = int(record.get("attempt") or 0)
        if lane not in LANES or attempt < 1 or attempt > maximum_attempts:
            raise ValueError("Agent evaluation checkpoint attempt identity is invalid")
        identity = (lane, attempt)
        if identity in seen:
            raise ValueError("Agent evaluation checkpoint attempt order is invalid")
        lane_record = record.get("laneRecord")
        if not isinstance(lane_record, Mapping) or lane_record.get("lane") != lane:
            raise ValueError("Agent evaluation checkpoint lane record is invalid")
        seen.add(identity)
    if int(checkpoint.get("revision") or 0) != (
        len(attempts) + len(attempt_starts) + len(attempt_bindings)
        + len(orphan_recovery_receipts)
    ):
        raise ValueError("Agent evaluation checkpoint revision is invalid")
    return checkpoint


def _open_lane_checkpoint(
    path: Path,
    *,
    fingerprint: Mapping[str, object],
    resume: bool,
) -> dict[str, object]:
    path = path.expanduser().resolve(strict=False)
    fingerprint_value = dict(fingerprint)
    if fingerprint_value.get("evaluationSplit") != "validation":
        raise ValueError("Agent evaluation checkpoint is validation-only")
    fingerprint_sha256 = _sha256_json(fingerprint_value)
    if resume:
        if not path.is_file():
            raise ValueError("Agent evaluation resume checkpoint does not exist")
        checkpoint = _read_lane_checkpoint(path)
        if (
            checkpoint.get("fingerprint") != fingerprint_value
            or checkpoint.get("fingerprintSha256") != fingerprint_sha256
        ):
            raise ValueError("Agent evaluation checkpoint fingerprint drift detected")
        return checkpoint
    if path.exists() or path.is_symlink():
        raise ValueError(
            "Agent evaluation checkpoint already exists; use --resume-checkpoint explicitly"
        )
    now_ms = int(time.time() * 1_000)
    checkpoint = _signed_lane_checkpoint(
        {
            "schemaVersion": _CHECKPOINT_SCHEMA_VERSION,
            "checkpointId": "checkpoint:" + fingerprint_sha256[:32],
            "fingerprintSha256": fingerprint_sha256,
            "fingerprint": fingerprint_value,
            "appendOnly": True,
            "revision": 0,
            "attemptStarts": [],
            "attemptBindings": [],
            "attempts": [],
            "orphanRecoveryReceipts": [],
            "createdAtMs": now_ms,
            "updatedAtMs": now_ms,
        }
    )
    _write_lane_checkpoint(path, checkpoint)
    return checkpoint


def _checkpoint_private_search_trace(
    ledger: Mapping[str, object],
) -> dict[str, object]:
    """Keep bounded query/hit identities for private post-cleanup diagnosis."""

    raw_searches = [
        item
        for item in ledger.get("items") or []
        if isinstance(item, Mapping) and item.get("operation") == "search"
    ]
    if len(raw_searches) > 64:
        raise ValueError("Agent evaluation private search trace is too large")
    searches: list[dict[str, object]] = []
    for item in raw_searches:
        args = item.get("args")
        args = args if isinstance(args, Mapping) else {}
        summary = item.get("resultSummary")
        summary = summary if isinstance(summary, Mapping) else {}
        raw_hits = summary.get("hits")
        hits = [hit for hit in raw_hits or [] if isinstance(hit, Mapping)]
        if len(hits) > 20:
            raise ValueError("Agent evaluation private search hit trace is too large")
        document_sha256s = list(
            dict.fromkeys(
                hashlib.sha256(
                    str(hit.get("externalDocumentId") or "").encode("utf-8")
                ).hexdigest()
                for hit in hits
                if str(hit.get("externalDocumentId") or "")
            )
        )
        chunk_sha256s = list(
            dict.fromkeys(
                hashlib.sha256(
                    str(hit.get("chunkId") or "").encode("utf-8")
                ).hexdigest()
                for hit in hits
                if str(hit.get("chunkId") or "")
            )
        )
        search: dict[str, object] = {
            "sequence": int(item.get("sequence") or 0),
            "sessionSha256": hashlib.sha256(
                str(item.get("sessionId") or "").encode("utf-8")
            ).hexdigest(),
            "evaluationCaseId": str(args.get("evaluationCaseId") or ""),
            "querySha256": str(args.get("querySha256") or ""),
            "queryChars": int(args.get("queryChars") or 0),
            "parameterSha256": _sha256_json(dict(args)),
            "ok": item.get("ok") is True,
            "resultSha256": str(item.get("resultSha256") or ""),
            "receiptSha256": str(item.get("receiptSha256") or ""),
            "hitCount": len(hits),
            "documentSha256s": document_sha256s,
            "chunkSha256s": chunk_sha256s,
            "citationRefs": list(
                dict.fromkeys(
                    str(hit.get("citationRef") or "")
                    for hit in hits
                    if str(hit.get("citationRef") or "")
                )
            ),
        }
        search["searchTraceSha256"] = _sha256_json(search)
        searches.append(search)
    trace: dict[str, object] = {
        "schemaVersion": "rag-ime.rag-agent-private-search-trace.v1",
        "ledgerSha256": str(ledger.get("ledgerSha256") or ""),
        "searchCount": len(searches),
        "failedSearchCount": sum(search.get("ok") is not True for search in searches),
        "searches": searches,
    }
    trace["traceSha256"] = _sha256_json(trace)
    return trace


def _checkpoint_cited_chunk_refs(
    ledger: Mapping[str, object],
) -> list[dict[str, object]]:
    """Persist the minimum private evidence locator needed by a resumed Judge."""

    refs: list[dict[str, object]] = []
    for item in ledger.get("items") or []:
        if not isinstance(item, Mapping) or item.get("operation") != "search":
            continue
        args = item.get("args")
        args = args if isinstance(args, Mapping) else {}
        summary = item.get("resultSummary")
        summary = summary if isinstance(summary, Mapping) else {}
        hits = [
            {
                "citationRef": str(hit.get("citationRef") or ""),
                "externalDocumentSha256": hashlib.sha256(
                    str(hit.get("externalDocumentId") or "").encode("utf-8")
                ).hexdigest(),
                "ordinal": hit.get("ordinal"),
            }
            for hit in summary.get("hits") or []
            if isinstance(hit, Mapping)
        ]
        refs.append(
            {
                "operation": "search",
                "ok": item.get("ok") is True,
                "evaluationCaseId": str(args.get("evaluationCaseId") or ""),
                "hits": hits,
            }
        )
    return refs


def _checkpoint_lane_record_projection(
    lane_record: Mapping[str, object],
) -> tuple[dict[str, Any], str, str]:
    projected = json.loads(
        json.dumps(lane_record, ensure_ascii=False, allow_nan=False)
    )
    session_id = str(projected.pop("_checkpointSessionId", "") or "")
    turn_id = str(projected.pop("_checkpointTurnId", "") or "")
    binding = projected.get("binding")
    if isinstance(binding, dict):
        session_id = session_id or str(binding.pop("sessionId", "") or "")
        if session_id:
            binding["sessionSha256"] = hashlib.sha256(
                session_id.encode("utf-8")
            ).hexdigest()
    assistant_text = str(projected.get("_assistantText") or "")
    if len(assistant_text) > _CHECKPOINT_MAX_ASSISTANT_CHARS:
        raise ValueError("Agent evaluation checkpoint assistant output is too large")
    ledger = projected.get("gatewayLedger")
    if isinstance(ledger, Mapping):
        projected["privateSearchTrace"] = _checkpoint_private_search_trace(ledger)
        projected["_privateCitedChunkRefs"] = _checkpoint_cited_chunk_refs(ledger)
        projected["gatewayLedger"] = {
            "schemaVersion": "rag-ime.rag-agent-checkpoint-ledger-summary.v1",
            "itemCount": int(ledger.get("itemCount") or 0),
            "failedItemCount": sum(
                isinstance(item, Mapping) and item.get("ok") is not True
                for item in ledger.get("items") or []
            ),
            "checkpointProjected": True,
        }
    return projected, session_id, turn_id


def _lane_checkpoint_record_is_reusable(lane_record: Mapping[str, object]) -> bool:
    lane = str(lane_record.get("lane") or "")
    score = lane_record.get("score")
    if not isinstance(score, Mapping):
        return False
    hard_evidence = score.get("hardEvidence")
    metrics = score.get("agentMetrics")
    protocol_errors = score.get("protocolErrors")
    if not isinstance(hard_evidence, Mapping) or not isinstance(metrics, Mapping):
        return False
    return bool(
        lane in LANES
        and lane_record.get("terminalEvent") == "turn_completed"
        and not str(lane_record.get("runtimeFailureCategory") or "")
        and not str(lane_record.get("error") or "")
        and lane_record.get("toolContract") is True
        and lane_record.get("scopeBoundary") is True
        and lane_record.get("bindingCleanup") is True
        and hard_evidence.get("parameterBounded") is True
        and (
            hard_evidence.get("agenticLoopObserved") is True
            if lane == "agentic"
            else True
        )
        and score.get("failedToolItemCount") == 0
        and isinstance(protocol_errors, list)
        and not protocol_errors
        and float(metrics.get("outputProtocolRate") or 0.0) == 1.0
        and bool(str(lane_record.get("_assistantText") or "").strip())
    )


def _append_lane_checkpoint_attempt_started(
    path: Path,
    *,
    checkpoint: Mapping[str, object],
    lane: str,
    attempt: int,
) -> dict[str, object]:
    if lane not in LANES:
        raise ValueError("Agent evaluation checkpoint start lane is invalid")
    persisted = _read_lane_checkpoint(path)
    if persisted.get("checkpointSha256") != checkpoint.get("checkpointSha256"):
        raise ValueError("Agent evaluation checkpoint changed concurrently")
    attempt_starts = [
        dict(item) for item in persisted.get("attemptStarts") or []
    ]
    attempt_bindings = [
        dict(item) for item in persisted.get("attemptBindings") or []
    ]
    attempts = [dict(item) for item in persisted.get("attempts") or []]
    orphan_recoveries = [
        dict(item) for item in persisted.get("orphanRecoveryReceipts") or []
    ]
    previous_attempt = max(
        (
            int(item.get("attempt") or 0)
            for item in (*attempt_starts, *attempts)
            if item.get("lane") == lane
        ),
        default=0,
    )
    maximum_attempts = int(
        dict(persisted["fingerprint"]).get("maximumAttemptsPerLane") or 0
    )
    if int(attempt) != previous_attempt + 1 or int(attempt) > maximum_attempts:
        raise ValueError("Agent evaluation checkpoint start is outside its budget")
    start_record: dict[str, object] = {
        "lane": lane,
        "attempt": int(attempt),
        "startedAtMs": int(time.time() * 1_000),
        "lifecycleState": "started",
        "attemptKeySha256": _sha256_json(
            {
                "fingerprintSha256": str(
                    persisted.get("fingerprintSha256") or ""
                ),
                "lane": lane,
                "attempt": int(attempt),
            }
        ),
    }
    start_record["startReceiptSha256"] = _sha256_json(start_record)
    attempt_starts.append(start_record)
    updated = _signed_lane_checkpoint(
        {
            **persisted,
            "revision": (
                len(attempt_starts)
                + len(attempt_bindings)
                + len(attempts)
                + len(orphan_recoveries)
            ),
            "attemptStarts": attempt_starts,
            "updatedAtMs": int(time.time() * 1_000),
        }
    )
    _write_lane_checkpoint(path, updated)
    return updated


def _append_lane_checkpoint_attempt_binding(
    path: Path,
    *,
    checkpoint: Mapping[str, object],
    lane: str,
    attempt: int,
    session_id: str,
    turn_id: str = "",
    recovery_locator: Mapping[str, object] | None = None,
) -> dict[str, object]:
    persisted = _read_lane_checkpoint(path)
    if persisted.get("checkpointSha256") != checkpoint.get("checkpointSha256"):
        raise ValueError("Agent evaluation checkpoint changed concurrently")
    attempt_starts = [
        dict(item) for item in persisted.get("attemptStarts") or []
    ]
    attempt_bindings = [
        dict(item) for item in persisted.get("attemptBindings") or []
    ]
    attempts = [dict(item) for item in persisted.get("attempts") or []]
    orphan_recoveries = [
        dict(item) for item in persisted.get("orphanRecoveryReceipts") or []
    ]
    identity = (str(lane), int(attempt))
    if not any(
        (str(item.get("lane") or ""), int(item.get("attempt") or 0)) == identity
        for item in attempt_starts
    ):
        raise ValueError("Agent evaluation binding has no durable start receipt")
    if any(
        (str(item.get("lane") or ""), int(item.get("attempt") or 0)) == identity
        for item in attempts
    ):
        raise ValueError("Agent evaluation binding cannot follow a terminal receipt")
    normalized_session_id = str(session_id or "").strip()
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_session_id:
        raise ValueError("Agent evaluation binding Session is required")
    stage = "turn_accepted" if normalized_turn_id else "session_created"
    existing = [
        item
        for item in attempt_bindings
        if (str(item.get("lane") or ""), int(item.get("attempt") or 0))
        == identity
    ]
    expected_stages = [] if stage == "session_created" else ["session_created"]
    if [str(item.get("bindingStage") or "") for item in existing] != expected_stages:
        raise ValueError("Agent evaluation binding receipt order is invalid")
    session_sha256 = hashlib.sha256(
        normalized_session_id.encode("utf-8")
    ).hexdigest()
    if existing and str(existing[0].get("sessionSha256") or "") != session_sha256:
        raise ValueError("Agent evaluation binding Session drifted")
    private_locator: dict[str, object] = {}
    if recovery_locator is not None:
        private_locator = _validated_lane_recovery_locator(
            recovery_locator,
            session_id=normalized_session_id,
            turn_id=normalized_turn_id,
        )
    binding_record: dict[str, object] = {
        "lane": str(lane),
        "attempt": int(attempt),
        "bindingStage": stage,
        "boundAtMs": int(time.time() * 1_000),
        "sessionSha256": session_sha256,
        "turnSha256": (
            hashlib.sha256(normalized_turn_id.encode("utf-8")).hexdigest()
            if normalized_turn_id
            else ""
        ),
        "startReceiptSha256": str(
            next(
                item
                for item in attempt_starts
                if (
                    str(item.get("lane") or ""),
                    int(item.get("attempt") or 0),
                )
                == identity
            ).get("startReceiptSha256")
            or ""
        ),
        "recoveryLocator": private_locator,
        "recoveryLocatorSha256": (
            _sha256_json(private_locator) if private_locator else ""
        ),
    }
    binding_record["bindingReceiptSha256"] = _sha256_json(binding_record)
    attempt_bindings.append(binding_record)
    updated = _signed_lane_checkpoint(
        {
            **persisted,
            "revision": (
                len(attempt_starts)
                + len(attempt_bindings)
                + len(attempts)
                + len(orphan_recoveries)
            ),
            "attemptBindings": attempt_bindings,
            "updatedAtMs": int(time.time() * 1_000),
        }
    )
    _write_lane_checkpoint(path, updated)
    return updated


def _append_lane_checkpoint_attempt(
    path: Path,
    *,
    checkpoint: Mapping[str, object],
    lane: str,
    attempt: int,
    lane_record: Mapping[str, object],
) -> dict[str, object]:
    if lane not in LANES or lane_record.get("lane") != lane:
        raise ValueError("Agent evaluation checkpoint lane does not match the result")
    persisted = _read_lane_checkpoint(path)
    if persisted.get("checkpointSha256") != checkpoint.get("checkpointSha256"):
        raise ValueError("Agent evaluation checkpoint changed concurrently")
    attempts = [dict(item) for item in persisted.get("attempts") or []]
    attempt_starts = [
        dict(item) for item in persisted.get("attemptStarts") or []
    ]
    attempt_bindings = [
        dict(item) for item in persisted.get("attemptBindings") or []
    ]
    orphan_recoveries = [
        dict(item) for item in persisted.get("orphanRecoveryReceipts") or []
    ]
    identity = (lane, int(attempt))
    if not any(
        (str(item.get("lane") or ""), int(item.get("attempt") or 0)) == identity
        for item in attempt_starts
    ):
        raise ValueError("Agent evaluation terminal has no durable start receipt")
    if any(
        (str(item.get("lane") or ""), int(item.get("attempt") or 0)) == identity
        for item in attempts
    ):
        raise ValueError("Agent evaluation terminal receipt already exists")
    matching_bindings = [
        item
        for item in attempt_bindings
        if (str(item.get("lane") or ""), int(item.get("attempt") or 0))
        == identity
    ]
    if not matching_bindings or matching_bindings[0].get("bindingStage") != "session_created":
        raise ValueError("Agent evaluation terminal has no durable Session binding")
    projected, session_id, turn_id = _checkpoint_lane_record_projection(lane_record)
    start_record = next(
        item
        for item in attempt_starts
        if (str(item.get("lane") or ""), int(item.get("attempt") or 0))
        == identity
    )
    ledger = projected.get("gatewayLedger")
    gateway_item_count = (
        int(ledger.get("itemCount") or 0) if isinstance(ledger, Mapping) else 0
    )
    session_sha256 = (
        hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        if session_id
        else ""
    )
    turn_sha256 = (
        hashlib.sha256(turn_id.encode("utf-8")).hexdigest() if turn_id else ""
    )
    if str(matching_bindings[-1].get("sessionSha256") or "") != session_sha256:
        raise ValueError("Agent evaluation terminal Session binding does not match")
    bound_turn_sha256 = str(matching_bindings[-1].get("turnSha256") or "")
    if bound_turn_sha256 and bound_turn_sha256 != turn_sha256:
        raise ValueError("Agent evaluation terminal turn binding does not match")
    attempt_record: dict[str, object] = {
        "lane": lane,
        "attempt": int(attempt),
        "createdAtMs": int(time.time() * 1_000),
        "terminalAtMs": int(time.time() * 1_000),
        "lifecycleState": "terminal",
        "startReceiptSha256": str(start_record.get("startReceiptSha256") or ""),
        "sessionSha256": session_sha256,
        "turnSha256": turn_sha256,
        "terminalEvent": str(lane_record.get("terminalEvent") or ""),
        "runtimeFailureCategory": str(
            lane_record.get("runtimeFailureCategory") or ""
        ),
        "gatewayItemCount": gateway_item_count,
        "reusable": _lane_checkpoint_record_is_reusable(projected),
        "laneRecord": projected,
    }
    terminal_receipt_sha256 = _sha256_json(attempt_record)
    attempt_record["terminalReceiptSha256"] = terminal_receipt_sha256
    attempt_record["attemptSha256"] = terminal_receipt_sha256
    attempts.append(attempt_record)
    updated = _signed_lane_checkpoint(
        {
            **persisted,
            "revision": (
                len(attempt_starts)
                + len(attempt_bindings)
                + len(attempts)
                + len(orphan_recoveries)
            ),
            "attempts": attempts,
            "updatedAtMs": int(time.time() * 1_000),
        }
    )
    _write_lane_checkpoint(path, updated)
    return updated


def _lane_checkpoint_attempt_records(
    checkpoint: Mapping[str, object],
) -> list[dict[str, object]]:
    starts = [
        item
        for item in checkpoint.get("attemptStarts") or []
        if isinstance(item, Mapping)
    ]
    terminals = [
        item
        for item in checkpoint.get("attempts") or []
        if isinstance(item, Mapping)
    ]
    bindings = [
        item
        for item in checkpoint.get("attemptBindings") or []
        if isinstance(item, Mapping)
    ]
    bindings_by_identity: dict[tuple[str, int], list[Mapping[str, object]]] = {}
    for item in bindings:
        identity = (str(item.get("lane") or ""), int(item.get("attempt") or 0))
        bindings_by_identity.setdefault(identity, []).append(item)
    terminal_by_identity = {
        (str(item.get("lane") or ""), int(item.get("attempt") or 0)): item
        for item in terminals
    }
    started_identities = {
        (str(item.get("lane") or ""), int(item.get("attempt") or 0))
        for item in starts
    }
    merged: list[dict[str, object]] = []
    for item in terminals:
        identity = (str(item.get("lane") or ""), int(item.get("attempt") or 0))
        if identity in started_identities:
            continue
        legacy = dict(item)
        legacy["lifecycleState"] = "terminal"
        legacy["startReceiptSha256"] = str(item.get("attemptSha256") or "")
        legacy["terminalReceiptSha256"] = str(item.get("attemptSha256") or "")
        merged.append(legacy)
    for start in starts:
        lane = str(start.get("lane") or "")
        attempt = int(start.get("attempt") or 0)
        terminal = terminal_by_identity.get((lane, attempt))
        attempt_bindings = bindings_by_identity.get((lane, attempt), [])
        latest_binding = attempt_bindings[-1] if attempt_bindings else {}
        binding_receipt_sha256s = [
            str(item.get("bindingReceiptSha256") or "")
            for item in attempt_bindings
        ]
        if terminal is not None:
            completed = dict(terminal)
            completed["lifecycleState"] = "terminal"
            completed["startedAtMs"] = int(start.get("startedAtMs") or 0)
            completed["startReceiptSha256"] = str(
                start.get("startReceiptSha256") or ""
            )
            completed["bindingReceiptSha256"] = (
                binding_receipt_sha256s[-1] if binding_receipt_sha256s else ""
            )
            completed["bindingReceiptSha256s"] = binding_receipt_sha256s
            merged.append(completed)
            continue
        merged.append(
            {
                "lane": lane,
                "attempt": attempt,
                "startedAtMs": int(start.get("startedAtMs") or 0),
                "lifecycleState": "interrupted",
                "startReceiptSha256": str(
                    start.get("startReceiptSha256") or ""
                ),
                "terminalReceiptSha256": "",
                "sessionSha256": str(latest_binding.get("sessionSha256") or ""),
                "turnSha256": str(latest_binding.get("turnSha256") or ""),
                "bindingReceiptSha256": (
                    binding_receipt_sha256s[-1]
                    if binding_receipt_sha256s
                    else ""
                ),
                "bindingReceiptSha256s": binding_receipt_sha256s,
                "terminalEvent": "",
                "runtimeFailureCategory": "interrupted",
                "gatewayItemCount": 0,
                "reusable": False,
                "attemptSha256": str(start.get("startReceiptSha256") or ""),
            }
        )
    return merged


def _recover_lane_checkpoint_orphans(
    path: Path,
    *,
    checkpoint: Mapping[str, object],
    session_recover: (
        Callable[[Mapping[str, object]], Mapping[str, object]] | None
    ),
    sandbox_cleanup: (
        Callable[[Mapping[str, object]], Mapping[str, object]] | None
    ),
) -> tuple[dict[str, object], dict[str, object]]:
    """Recover interrupted private resources, or durably fail closed.

    The callbacks are owner seams. A caller may only claim Session recovery
    when it can prove the exact bound Session/turn reached a terminal state.
    Raw recovery locators stay in the private checkpoint and are never copied
    into the returned public summary.
    """

    persisted = _read_lane_checkpoint(path)
    if persisted.get("checkpointSha256") != checkpoint.get("checkpointSha256"):
        raise ValueError("Agent evaluation checkpoint changed concurrently")
    interrupted = [
        item
        for item in _lane_checkpoint_attempt_records(persisted)
        if item.get("lifecycleState") == "interrupted"
    ]
    existing_by_identity = {
        (str(item.get("lane") or ""), int(item.get("attempt") or 0)): item
        for item in persisted.get("orphanRecoveryReceipts") or []
        if isinstance(item, Mapping)
    }
    bindings_by_identity: dict[tuple[str, int], list[Mapping[str, object]]] = {}
    for item in persisted.get("attemptBindings") or []:
        if isinstance(item, Mapping):
            identity = (
                str(item.get("lane") or ""),
                int(item.get("attempt") or 0),
            )
            bindings_by_identity.setdefault(identity, []).append(item)

    for attempt_record in interrupted:
        lane = str(attempt_record.get("lane") or "")
        attempt = int(attempt_record.get("attempt") or 0)
        identity = (lane, attempt)
        if identity in existing_by_identity:
            continue
        matching_bindings = bindings_by_identity.get(identity, [])
        latest_binding = matching_bindings[-1] if matching_bindings else {}
        locator_value = latest_binding.get("recoveryLocator")
        locator = dict(locator_value) if isinstance(locator_value, Mapping) else {}
        locator_sha256 = str(
            latest_binding.get("recoveryLocatorSha256") or ""
        )
        expected_session_sha256 = str(
            attempt_record.get("sessionSha256") or ""
        )
        expected_turn_sha256 = str(attempt_record.get("turnSha256") or "")

        session_result: dict[str, object] = {
            "status": "blocked",
            "reasonCode": "recovery_locator_missing",
            "evidenceSha256": "",
        }
        sandbox_result: dict[str, object] = {
            "status": "blocked",
            "reasonCode": "recovery_locator_missing",
            "evidenceSha256": "",
        }
        if locator:
            if session_recover is None:
                session_result["reasonCode"] = (
                    "cross_db_exact_turn_cancel_unavailable"
                )
            else:
                try:
                    recovered_session = dict(session_recover(locator))
                    session_evidence_sha256 = _sha256_json(recovered_session)
                except Exception as exc:
                    session_result = {
                        "status": "blocked",
                        "reasonCode": "session_recovery_error",
                        "evidenceSha256": _sha256_json(
                            {
                                "errorType": type(exc).__name__,
                                "error": str(exc),
                            }
                        ),
                    }
                else:
                    session_matches = (
                        recovered_session.get("recovered") is True
                        and recovered_session.get("terminal") is True
                        and str(recovered_session.get("sessionSha256") or "")
                        == expected_session_sha256
                        and str(recovered_session.get("turnSha256") or "")
                        == expected_turn_sha256
                    )
                    session_result = {
                        "status": "recovered" if session_matches else "blocked",
                        "reasonCode": (
                            "exact_session_terminal_verified"
                            if session_matches
                            else "exact_session_terminal_unverified"
                        ),
                        "evidenceSha256": session_evidence_sha256,
                    }
            if sandbox_cleanup is None:
                sandbox_result["reasonCode"] = "sandbox_cleanup_owner_unavailable"
            else:
                try:
                    cleaned_sandbox = dict(sandbox_cleanup(locator))
                    sandbox_evidence_sha256 = _sha256_json(cleaned_sandbox)
                except Exception as exc:
                    sandbox_result = {
                        "status": "blocked",
                        "reasonCode": "sandbox_cleanup_error",
                        "evidenceSha256": _sha256_json(
                            {
                                "errorType": type(exc).__name__,
                                "error": str(exc),
                            }
                        ),
                    }
                else:
                    returned_run_sha256 = str(
                        cleaned_sandbox.get("runIdSha256") or ""
                    )
                    if not returned_run_sha256 and cleaned_sandbox.get("runId"):
                        returned_run_sha256 = hashlib.sha256(
                            str(cleaned_sandbox["runId"]).encode("utf-8")
                        ).hexdigest()
                    expected_run_sha256 = hashlib.sha256(
                        str(locator.get("sandboxRunId") or "").encode("utf-8")
                    ).hexdigest()
                    sandbox_matches = (
                        cleaned_sandbox.get("deleted") is True
                        and returned_run_sha256 == expected_run_sha256
                    )
                    sandbox_result = {
                        "status": "recovered" if sandbox_matches else "blocked",
                        "reasonCode": (
                            "marker_bound_sandbox_deleted"
                            if sandbox_matches
                            else "marker_bound_sandbox_cleanup_unverified"
                        ),
                        "evidenceSha256": sandbox_evidence_sha256,
                    }

        recovered = (
            session_result["status"] == "recovered"
            and sandbox_result["status"] == "recovered"
        )
        receipt: dict[str, object] = {
            "lane": lane,
            "attempt": attempt,
            "recoveredAtMs": int(time.time() * 1_000),
            "status": "recovered" if recovered else "blocked",
            "failClosed": not recovered,
            "recoveryLocatorPresent": bool(locator),
            "recoveryLocatorSha256": locator_sha256,
            "sessionSha256": expected_session_sha256,
            "turnSha256": expected_turn_sha256,
            "sandboxRunSha256": (
                hashlib.sha256(
                    str(locator.get("sandboxRunId") or "").encode("utf-8")
                ).hexdigest()
                if locator
                else ""
            ),
            "startReceiptSha256": str(
                attempt_record.get("startReceiptSha256") or ""
            ),
            "bindingReceiptSha256": str(
                attempt_record.get("bindingReceiptSha256") or ""
            ),
            "sessionRecovery": session_result,
            "sandboxRecovery": sandbox_result,
        }
        receipt["recoveryReceiptSha256"] = _sha256_json(receipt)
        current = _read_lane_checkpoint(path)
        if current.get("checkpointSha256") != persisted.get("checkpointSha256"):
            raise ValueError("Agent evaluation checkpoint changed concurrently")
        recovery_receipts = [
            dict(item) for item in current.get("orphanRecoveryReceipts") or []
        ]
        recovery_receipts.append(receipt)
        attempt_starts = current.get("attemptStarts") or []
        attempt_bindings = current.get("attemptBindings") or []
        attempts = current.get("attempts") or []
        persisted = _signed_lane_checkpoint(
            {
                **current,
                "revision": (
                    len(attempt_starts)
                    + len(attempt_bindings)
                    + len(attempts)
                    + len(recovery_receipts)
                ),
                "orphanRecoveryReceipts": recovery_receipts,
                "updatedAtMs": int(time.time() * 1_000),
            }
        )
        _write_lane_checkpoint(path, persisted)
        existing_by_identity[identity] = receipt

    statuses = [
        str(existing_by_identity.get(
            (str(item.get("lane") or ""), int(item.get("attempt") or 0)),
            {},
        ).get("status") or "blocked")
        for item in interrupted
    ]
    blocked_count = sum(status != "recovered" for status in statuses)
    return persisted, {
        "schemaVersion": "rag-ime.rag-agent-orphan-recovery-summary.v1",
        "orphanCount": len(interrupted),
        "recoveredCount": len(interrupted) - blocked_count,
        "blockedCount": blocked_count,
        "failClosed": blocked_count > 0,
    }


def _lane_checkpoint_attempt_history(
    checkpoint: Mapping[str, object],
    lane: str,
) -> list[dict[str, object]]:
    selected = [
        item
        for item in _lane_checkpoint_attempt_records(checkpoint)
        if item.get("lane") == lane
    ]
    return [
        {
            "attempt": int(item.get("attempt") or 0),
            "terminalEvent": str(item.get("terminalEvent") or ""),
            "runtimeFailureCategory": str(
                item.get("runtimeFailureCategory") or ""
            ),
            "gatewayItemCount": int(item.get("gatewayItemCount") or 0),
            "lifecycleState": str(item.get("lifecycleState") or ""),
            "reusable": item.get("reusable") is True,
            "startReceiptSha256": str(item.get("startReceiptSha256") or ""),
            "terminalReceiptSha256": str(
                item.get("terminalReceiptSha256") or ""
            ),
            "sessionSha256": str(item.get("sessionSha256") or ""),
            "turnSha256": str(item.get("turnSha256") or ""),
            "bindingReceiptSha256": str(
                item.get("bindingReceiptSha256") or ""
            ),
            "retryScheduled": index < len(selected) - 1,
        }
        for index, item in enumerate(selected)
    ]


def _lane_checkpoint_reusable_records(
    checkpoint: Mapping[str, object],
) -> dict[str, dict[str, Any]]:
    fingerprint = checkpoint.get("fingerprint")
    prompt_hashes = (
        fingerprint.get("lanePromptSha256ByLane")
        if isinstance(fingerprint, Mapping)
        else {}
    )
    latest: dict[str, Mapping[str, object]] = {}
    for item in _lane_checkpoint_attempt_records(checkpoint):
        if isinstance(item, Mapping) and str(item.get("lane") or "") in LANES:
            latest[str(item["lane"])] = item
    reusable: dict[str, dict[str, Any]] = {}
    for lane, attempt_record in latest.items():
        lane_record = attempt_record.get("laneRecord")
        if (
            attempt_record.get("lifecycleState") != "terminal"
            or
            attempt_record.get("reusable") is not True
            or not isinstance(lane_record, Mapping)
            or not _lane_checkpoint_record_is_reusable(lane_record)
            or not isinstance(prompt_hashes, Mapping)
            or str(lane_record.get("promptSha256") or "")
            != str(prompt_hashes.get(lane) or "")
        ):
            continue
        projected = json.loads(json.dumps(lane_record, ensure_ascii=False))
        projected["checkpointReused"] = True
        projected["runtimeAttempts"] = _lane_checkpoint_attempt_history(
            checkpoint, lane
        )
        projected["runtimeRetryCount"] = max(
            0, len(projected["runtimeAttempts"]) - 1
        )
        reusable[lane] = projected
    return reusable


def _lane_checkpoint_report_projection(
    checkpoint: Mapping[str, object] | None,
    *,
    resume_requested: bool,
    initial_attempt_count: int,
    reused_lanes: set[str],
    fresh_lanes: set[str],
    checkpoint_requested: bool = False,
) -> dict[str, object]:
    if checkpoint is None:
        return {
            "schemaVersion": _CHECKPOINT_SCHEMA_VERSION,
            "enabled": bool(checkpoint_requested),
            "initialized": False,
            "resumeRequested": bool(resume_requested),
            "resumed": False,
            "attemptCount": 0,
            "startedReceiptCount": 0,
            "bindingReceiptCount": 0,
            "orphanRecoveryReceiptCount": 0,
            "recoveredOrphanCount": 0,
            "blockedOrphanCount": 0,
            "recoveryFailClosed": False,
            "terminalReceiptCount": 0,
            "interruptedAttemptCount": 0,
            "completedLaneCount": 0,
            "freshAttemptCount": 0,
            "reusedLaneCount": 0,
            "freshLaneCount": 0,
            "retriedLaneCount": 0,
            "orphanRecoveryHistory": [],
            "attemptHistory": [],
        }
    attempts = _lane_checkpoint_attempt_records(checkpoint)
    recovery_receipts = [
        item
        for item in checkpoint.get("orphanRecoveryReceipts") or []
        if isinstance(item, Mapping)
    ]
    recovery_history = [
        {
            "lane": str(item.get("lane") or ""),
            "attempt": int(item.get("attempt") or 0),
            "status": str(item.get("status") or ""),
            "failClosed": bool(item.get("failClosed")),
            "recoveryLocatorPresent": bool(
                item.get("recoveryLocatorPresent")
            ),
            "recoveryLocatorSha256": str(
                item.get("recoveryLocatorSha256") or ""
            ),
            "sessionSha256": str(item.get("sessionSha256") or ""),
            "turnSha256": str(item.get("turnSha256") or ""),
            "sandboxRunSha256": str(item.get("sandboxRunSha256") or ""),
            "sessionRecovery": dict(item.get("sessionRecovery") or {}),
            "sandboxRecovery": dict(item.get("sandboxRecovery") or {}),
            "recoveryReceiptSha256": str(
                item.get("recoveryReceiptSha256") or ""
            ),
        }
        for item in recovery_receipts
    ]
    history = [
        {
            "lane": str(item.get("lane") or ""),
            "attempt": int(item.get("attempt") or 0),
            "sessionSha256": str(item.get("sessionSha256") or ""),
            "turnSha256": str(item.get("turnSha256") or ""),
            "terminalEvent": str(item.get("terminalEvent") or ""),
            "runtimeFailureCategory": str(
                item.get("runtimeFailureCategory") or ""
            ),
            "gatewayItemCount": int(item.get("gatewayItemCount") or 0),
            "reusable": item.get("reusable") is True,
            "origin": "checkpoint" if index < initial_attempt_count else "fresh",
            "attemptSha256": str(item.get("attemptSha256") or ""),
            "startReceiptSha256": str(item.get("startReceiptSha256") or ""),
            "terminalReceiptSha256": str(
                item.get("terminalReceiptSha256") or ""
            ),
            "bindingReceiptSha256": str(
                item.get("bindingReceiptSha256") or ""
            ),
            "lifecycleState": str(item.get("lifecycleState") or ""),
        }
        for index, item in enumerate(attempts)
    ]
    attempts_by_lane = {
        lane: [item for item in attempts if item.get("lane") == lane]
        for lane in LANES
    }
    latest_by_lane = {
        lane: values[-1] for lane, values in attempts_by_lane.items() if values
    }
    completed_lanes = {
        lane
        for lane, item in latest_by_lane.items()
        if item.get("terminalEvent") == "turn_completed"
    }
    retried_lanes = {
        lane for lane, values in attempts_by_lane.items() if len(values) > 1
    }
    return {
        "schemaVersion": _CHECKPOINT_SCHEMA_VERSION,
        "enabled": True,
        "initialized": True,
        "resumeRequested": bool(resume_requested),
        "resumed": bool(resume_requested),
        "checkpointId": str(checkpoint.get("checkpointId") or ""),
        "checkpointSha256": str(checkpoint.get("checkpointSha256") or ""),
        "fingerprintSha256": str(checkpoint.get("fingerprintSha256") or ""),
        "revision": int(checkpoint.get("revision") or 0),
        "attemptCount": len(attempts),
        "startedReceiptCount": len(attempts),
        "bindingReceiptCount": len(checkpoint.get("attemptBindings") or []),
        "orphanRecoveryReceiptCount": len(recovery_receipts),
        "recoveredOrphanCount": sum(
            item.get("status") == "recovered" for item in recovery_receipts
        ),
        "blockedOrphanCount": sum(
            item.get("status") != "recovered" for item in recovery_receipts
        ),
        "recoveryFailClosed": any(
            item.get("status") != "recovered" for item in recovery_receipts
        ),
        "terminalReceiptCount": sum(
            item.get("lifecycleState") == "terminal" for item in attempts
        ),
        "interruptedAttemptCount": sum(
            item.get("lifecycleState") == "interrupted" for item in attempts
        ),
        "completedLaneCount": len(completed_lanes),
        "historicalAttemptCount": min(initial_attempt_count, len(attempts)),
        "freshAttemptCount": max(0, len(attempts) - initial_attempt_count),
        "resumedLaneCount": len(reused_lanes),
        "reusedLaneCount": len(reused_lanes),
        "freshLaneCount": len(fresh_lanes),
        "retriedLaneCount": len(retried_lanes),
        "retryAttemptCount": sum(
            max(0, len(values) - 1) for values in attempts_by_lane.values()
        ),
        "completedLanes": [lane for lane in LANES if lane in completed_lanes],
        "reusedLanes": [lane for lane in LANES if lane in reused_lanes],
        "freshLanes": [lane for lane in LANES if lane in fresh_lanes],
        "orphanRecoveryHistory": recovery_history,
        "attemptHistory": history,
    }


_PRIVATE_REPORT_IDENTIFIER_KEYS = {
    "sessionId": "sessionSha256",
    "runtimeSessionId": "runtimeSessionSha256",
    "externalSessionId": "externalSessionSha256",
    "parentSessionId": "parentSessionSha256",
    "childSessionId": "childSessionSha256",
    "turnId": "turnSha256",
    "_checkpointSessionId": "sessionSha256",
    "_checkpointTurnId": "turnSha256",
}
_PRIVATE_REPORT_TEXT_KEYS = frozenset(
    {"_assistantText", "assistantText", "assistantOutputs"}
)
_PRIVATE_REPORT_DROP_KEYS = frozenset(
    {"privateSearchTrace", "_privateCitedChunkRefs", "_privateEvidenceQrels"}
)
_PRIVATE_REPORT_FREEFORM_KEYS = frozenset({"error", "failure"})
_ABSOLUTE_PATH_TOKEN = re.compile(r"(?<![A-Za-z0-9:/])/(?:[^\s\"'<>]+)")
_ABSOLUTE_PATH_WITH_SPACE = re.compile(
    r"(?<![A-Za-z0-9:/])/(?:Volumes|Users|private|tmp|var)/"
    r"[^\"'<>\n]*\s+[^\"'<>\n]*/[^\"'<>\n]*"
)


def _hash_private_report_value(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _redact_absolute_paths(value: str) -> str:
    stripped = value.strip()
    if re.fullmatch(r"https?://[^\s]+", stripped):
        return value

    def replacement(path: str) -> str:
        return "[path-sha256:" + hashlib.sha256(path.encode("utf-8")).hexdigest() + "]"

    if _ABSOLUTE_PATH_WITH_SPACE.search(value):
        return replacement(stripped)
    if stripped.startswith("/") or stripped.startswith("file:///"):
        leading = value[: len(value) - len(value.lstrip())]
        trailing = value[len(value.rstrip()) :]
        return leading + replacement(stripped) + trailing
    return _ABSOLUTE_PATH_TOKEN.sub(
        lambda match: replacement(match.group(0)),
        value,
    )


def _sanitize_public_report_value(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _PRIVATE_REPORT_DROP_KEYS:
                continue
            if key in _PRIVATE_REPORT_FREEFORM_KEYS:
                if item:
                    result[f"{key}Sha256"] = _hash_private_report_value(item)
                continue
            if key in _PRIVATE_REPORT_TEXT_KEYS:
                if key != "assistantOutputs" and item:
                    result["assistantTextSha256"] = _hash_private_report_value(item)
                continue
            hashed_key = _PRIVATE_REPORT_IDENTIFIER_KEYS.get(key)
            if hashed_key is not None:
                if item:
                    result[hashed_key] = _hash_private_report_value(item)
                continue
            if key == "sessionIds" and isinstance(item, list):
                result["sessionSha256s"] = [
                    _hash_private_report_value(identifier) for identifier in item
                ]
                continue
            result[key] = _sanitize_public_report_value(item)
        return result
    if isinstance(value, list):
        return [_sanitize_public_report_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_public_report_value(item) for item in value]
    if isinstance(value, str):
        return _redact_absolute_paths(value)
    return value


def _finalize_public_report(
    report: Mapping[str, object],
    *,
    checkpoint: Mapping[str, object] | None,
    scene_recipe: Mapping[str, object] | None = None,
    candidate_prompt: CandidatePrompt | None = None,
    judge_model: str | None = None,
) -> dict[str, object]:
    candidate = dict(report)
    if candidate_prompt is not None or judge_model is not None:
        candidate["conditions"] = {
            **dict(candidate.get("conditions") or {}),
            **({"candidatePrompt": candidate_prompt_identity(candidate_prompt)} if candidate_prompt is not None else {}),
            **({"answerJudgeModel": f"{_EVALUATION_PROVIDER}/{judge_model}"} if judge_model is not None else {}),
        }
    if scene_recipe is not None:
        candidate["conditions"] = {
            **dict(candidate.get("conditions") or {}),
            "parentSceneRecipe" if candidate_prompt is not None else "sceneRecipe": validate_scene_recipe_binding(scene_recipe),
        }
        if candidate_prompt is not None:
            candidate["conditions"].pop("sceneRecipe", None)
    candidate.pop("reportSha256", None)
    candidate["checkpoint"] = dict(
        checkpoint
        or _lane_checkpoint_report_projection(
            None,
            resume_requested=False,
            initial_attempt_count=0,
            reused_lanes=set(),
            fresh_lanes=set(),
        )
    )
    public = _sanitize_public_report_value(candidate)
    if not isinstance(public, dict):
        raise RuntimeError("public Agent evaluation report projection is invalid")
    conditions = public.get("conditions")
    if isinstance(conditions, Mapping):
        for field, label in (
            ("piRuntime", "Pi Runtime identity"),
            ("agentConfig", "Agent configuration identity"),
        ):
            if field in conditions and public.get(field) != conditions.get(field):
                raise RuntimeError(
                    f"public Agent evaluation report {label} is missing or drifted"
                )
    evaluation = public.get("evaluation")
    if isinstance(evaluation, dict) and evaluation.get("split") == "validation":
        public["formalAcceptanceEligible"] = False
        if "formalAcceptancePassed" in public:
            public["formalAcceptancePassed"] = False
        if "formalAcceptanceEligible" in evaluation:
            evaluation["formalAcceptanceEligible"] = False
    public["reportSha256"] = _sha256_json(public)
    return public


def _copy_openai_codex_agent_config(source: Path, target: Path) -> list[str]:
    """Build an ephemeral config with OAuth only and no custom Provider routes."""

    source_auth = source / "auth.json"
    if not source_auth.is_file():
        raise RuntimeError("isolated Agent config requires an existing auth.json")
    auth = _read_json_object(source_auth)
    credential = auth.get(_EVALUATION_PROVIDER)
    if not isinstance(credential, Mapping):
        raise RuntimeError("isolated Agent config requires openai-codex OAuth")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)
    _write_json(target / "auth.json", {_EVALUATION_PROVIDER: dict(credential)})
    copied = ["auth.json"]

    source_settings = source / "settings.json"
    if source_settings.is_file():
        settings = _read_json_object(source_settings)
        safe_settings = {
            key: settings[key]
            for key in ("packages", "retry")
            if key in settings
        }
        _write_json(target / "settings.json", safe_settings)
        copied.append("settings.json")
    return copied


def _pin_evaluation_agent_config(
    agent_config: Path,
    *,
    evaluation_model: str = _EVALUATION_MODEL,
) -> dict[str, object]:
    settings_path = agent_config / "settings.json"
    settings = _read_json_object(settings_path) if settings_path.is_file() else {}
    settings.update(
        {
            "defaultProvider": _EVALUATION_PROVIDER,
            "defaultModel": evaluation_model,
            "defaultThinkingLevel": _EVALUATION_THINKING,
            "transport": "sse",
        }
    )
    temporary = settings_path.with_name(f".{settings_path.name}.tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(settings_path)
    models_path = agent_config / "models.json"
    models_store_path = agent_config / "models-store.json"
    auth = _read_json_object(agent_config / "auth.json")
    credential_provider_ids = sorted(str(key) for key in auth)
    openai_codex_only = (
        credential_provider_ids == [_EVALUATION_PROVIDER]
        and not models_path.exists()
        and not models_store_path.exists()
    )
    if not openai_codex_only:
        raise RuntimeError(
            "evaluation Agent config must contain only openai-codex OAuth"
        )
    receipt: dict[str, object] = {
        "schemaVersion": "rag-ime.rag-evaluation-agent-config.v1",
        "provider": _EVALUATION_PROVIDER,
        "model": f"{_EVALUATION_PROVIDER}/{evaluation_model}",
        "thinking": _EVALUATION_THINKING,
        "transport": "sse",
        "settingsSha256": _file_sha256(settings_path),
        "modelsSha256": _file_sha256(models_path) if models_path.is_file() else "",
        "modelsStoreSha256": (
            _file_sha256(models_store_path) if models_store_path.is_file() else ""
        ),
        "credentialProviderIds": credential_provider_ids,
        "customProviderConfigurationPresent": models_path.exists(),
        "customModelStorePresent": models_store_path.exists(),
        "openaiCodexOnly": openai_codex_only,
    }
    receipt["configSha256"] = _sha256_json(receipt)
    return receipt


def _evaluation_configuration_defaults(
    *,
    evaluation_model: str = _EVALUATION_MODEL,
) -> dict[str, object]:
    """Freeze every PAW model route used by this evaluation.

    Model routing is resolved while the durable Session is created.
    ``update_session`` owns permissions and disclosure, so adding
    ``modelProfile`` there would be a no-op.  Freezing the isolated PAW
    configuration keeps parent and delegated reviewer Sessions on the evaluated
    model. The independent answer Judge selects its own frozen model at Session
    creation. The bounded no-Tool reviewer uses low reasoning; parent and Judge
    remain at max.
    """

    configuration = default_agent_configuration(
        enabled=True,
        idle_timeout_seconds=0,
        model_profile=f"{_EVALUATION_PROVIDER}/{evaluation_model}",
        tool_profile_version="subagent-readonly-v1",
        resume_last_session=False,
    )
    frozen_route = {
        "modelProfile": f"{_EVALUATION_PROVIDER}/{evaluation_model}",
        "thinkingLevel": _EVALUATION_THINKING,
    }
    configuration["modelRouting"] = {
        route_id: dict(frozen_route) for route_id in _EVALUATION_MODEL_ROUTES
    }
    configuration["modelRouting"]["subagent"]["thinkingLevel"] = "low"
    return configuration


def _evaluation_configuration_identity(
    service: AgentService,
    *,
    evaluation_model: str = _EVALUATION_MODEL,
) -> dict[str, object]:
    snapshot = service.configuration_store.snapshot()
    configuration = snapshot.get("configuration")
    if not isinstance(configuration, Mapping):
        raise RuntimeError("evaluation Agent configuration is unavailable")
    expected = _evaluation_configuration_defaults(evaluation_model=evaluation_model)
    if configuration != expected:
        raise RuntimeError("evaluation Agent model routes are not frozen")
    receipt: dict[str, object] = {
        "schemaVersion": "rag-ime.rag-evaluation-model-routing.v1",
        "revision": int(snapshot.get("revision") or 0),
        "model": f"{_EVALUATION_PROVIDER}/{evaluation_model}",
        "thinking": _EVALUATION_THINKING,
        "routeIds": list(_EVALUATION_MODEL_ROUTES),
        "configurationSha256": _sha256_json(configuration),
    }
    receipt["identitySha256"] = _sha256_json(receipt)
    return receipt


def _answer_case_manifest(
    *,
    prepared_cases: list[Mapping[str, object]],
    answer_cases: list[Mapping[str, object]],
    selected_cases: list[Mapping[str, object]],
    documents: list[Mapping[str, object]],
    benchmark_id: str,
    prepared_source_sha256: str,
    evaluation_split: str,
    chunking_config: Mapping[str, object],
    answer_evidence_qrels: Mapping[str, object],
    prepared_artifact_sha256: str = "",
) -> dict[str, object]:
    document_text_by_id = {
        str(item.get("documentId") or "").strip(): str(item.get("text") or "")
        for item in documents
        if str(item.get("documentId") or "").strip()
    }
    if len(document_text_by_id) != len(documents):
        raise ValueError("prepared corpus contains duplicate or missing document IDs")
    prepared_by_id = _case_binding_map(prepared_cases, label="prepared suite")
    answer_retrieval_cases = [
        item for item in answer_cases if item.get("retrievalEvaluable") is not False
    ]
    answer_retrieval_by_id = _case_binding_map(
        answer_retrieval_cases,
        label="answer-case suite",
    )
    if prepared_by_id != answer_retrieval_by_id:
        raise ValueError("answer-case suite does not match prepared suite")
    relevant_document_ids = {
        str(document_id)
        for item in prepared_cases
        for document_id in (
            item.get("relevant", {}).keys()
            if isinstance(item.get("relevant"), Mapping)
            else ()
        )
    }
    missing_relevant = sorted(relevant_document_ids - set(document_text_by_id))
    if missing_relevant:
        raise ValueError("answer-case suite references documents outside prepared corpus")

    high_level_cases = [
        item for item in selected_cases if not bool(item.get("abstentionExpected"))
    ]
    if not high_level_cases:
        raise ValueError("answer-only selection contains no high-level cases")
    private_qrels, evidence_stats = _validate_answer_evidence_qrels(
        answer_evidence_qrels,
        selected_cases=high_level_cases,
        document_text_by_id=document_text_by_id,
        prepared_source_sha256=prepared_source_sha256,
        prepared_artifact_sha256=prepared_artifact_sha256,
        evaluation_split=evaluation_split,
        chunking_config=chunking_config,
    )
    qrels_schema_version = str(answer_evidence_qrels.get("schemaVersion") or "")
    standard = answer_evidence_qrels.get("answerEvidenceStandard")
    standard = standard if isinstance(standard, Mapping) else {}
    manifest: dict[str, object] = {
        "schemaVersion": "rag-ime.rag-answer-case-manifest.v2",
        "benchmarkId": str(benchmark_id),
        "preparedSourceSha256": str(prepared_source_sha256),
        "suiteBindingPassed": True,
        "corpusBindingPassed": True,
        "preparedRetrievalCaseCount": len(prepared_by_id),
        "answerOnlyCaseCount": sum(
            item.get("retrievalEvaluable") is False for item in answer_cases
        ),
        "corpusDocumentCount": len(document_text_by_id),
        "corpusDocumentIdsSha256": _sha256_json(sorted(document_text_by_id)),
        "retrievalCaseSetSha256": _sha256_json(prepared_by_id),
        "answerCaseSetSha256": _sha256_json(
            [_answer_case_identity(item) for item in answer_cases]
        ),
        "selectedCaseSetSha256": _sha256_json(
            [_answer_case_identity(item) for item in selected_cases]
        ),
        "evidenceContract": (
            "host-private-fact-qrels-exact-source-chunk-standard-v2"
            if qrels_schema_version == _ANSWER_EVIDENCE_QRELS_V2
            else "host-private-fact-qrels-exact-source-chunk-v1"
        ),
        "evidenceManifestSha256": str(
            answer_evidence_qrels.get("manifestSha256") or ""
        ),
        "chunkingConfigSha256": _sha256_json(dict(chunking_config)),
        "highLevelCaseCount": len(high_level_cases),
        "highLevelFactCount": int(evidence_stats["factCount"]),
        "verifiedHighLevelFactCount": int(evidence_stats["verifiedFactCount"]),
        "unavailableHighLevelFactCount": int(evidence_stats["unavailableFactCount"]),
        "evidenceSupportGroupCount": int(evidence_stats["supportGroupCount"]),
        "evidenceBindingCount": int(evidence_stats["evidenceBindingCount"]),
        "highLevelEvidenceAvailabilityPassed": (
            int(evidence_stats["factCount"])
            == int(evidence_stats["verifiedFactCount"])
            and int(evidence_stats["unavailableFactCount"]) == 0
        ),
        "_privateEvidenceQrels": private_qrels,
    }
    if qrels_schema_version == _ANSWER_EVIDENCE_QRELS_V2:
        manifest.update(
            {
                "evidenceQrelsSchemaVersion": qrels_schema_version,
                "answerEvidenceStandardSchemaVersion": str(
                    standard.get("schemaVersion") or ""
                ),
                "answerEvidenceStandardManifestSha256": str(
                    standard.get("manifestSha256") or ""
                ),
                "unbiasedPromotionClaimAllowed": False,
            }
        )
    if manifest["highLevelEvidenceAvailabilityPassed"] is not True:
        raise ValueError(
            "one or more high-level answer facts lack verified corpus evidence"
        )
    manifest["manifestSha256"] = _sha256_json(
        {key: value for key, value in manifest.items() if not key.startswith("_")}
    )
    return manifest


def _answer_evidence_chunker_dependency_surface() -> list[dict[str, str]]:
    """Hash the complete source-level pipeline used to materialize qrel chunks."""

    functions = (
        _normalize_text,
        _chunk_document,
        _chunk_strategy_blocks,
        _chunk_block_heading,
        _chunk_record,
    )
    return sorted(
        [
            {
                "qualifiedName": f"{function.__module__}.{function.__qualname__}",
                "sourceSha256": hashlib.sha256(
                    inspect.getsource(function).encode("utf-8")
                ).hexdigest(),
            }
            for function in functions
        ],
        key=lambda item: item["qualifiedName"],
    )


def _answer_evidence_chunk_manifest(
    *,
    document_ids: Sequence[str],
    source_chunks: Callable[[str], list[dict[str, Any]]],
) -> dict[str, object]:
    """Build the canonical, content-free identity of one frozen chunk corpus."""

    records: list[dict[str, object]] = []
    for document_id in sorted(document_ids):
        chunks = source_chunks(document_id)
        for chunk in sorted(chunks, key=lambda item: int(item["ordinal"])):
            records.append(
                {
                    "documentId": document_id,
                    "chunkOrdinal": int(chunk["ordinal"]),
                    "contentSha256": str(chunk.get("content_hash") or ""),
                    "headingSha256": hashlib.sha256(
                        str(chunk.get("heading") or "").encode("utf-8")
                    ).hexdigest(),
                    "page": chunk.get("page"),
                }
            )
    return {
        "chunkCount": len(records),
        "manifestSha256": _sha256_json(records),
    }


def _require_sha256(value: object, *, label: str) -> str:
    normalized = str(value or "")
    if re.fullmatch(r"[a-f0-9]{64}", normalized) is None:
        raise ValueError(f"{label} is invalid")
    return normalized


def _validate_answer_evidence_qrels_v1_contract(
    _value: Mapping[str, object],
    **_context: object,
) -> None:
    """Keep the immutable v1 contract accepted exactly as before."""


def _validate_answer_evidence_qrels_v2_contract(
    value: Mapping[str, object],
    *,
    document_text_by_id: Mapping[str, str],
    prepared_source_sha256: str,
    prepared_artifact_sha256: str,
    evaluation_split: str,
    chunking_config: Mapping[str, object],
    source_chunks: Callable[[str], list[dict[str, Any]]],
) -> None:
    """Fail closed unless qrels-v2 carries one complete immutable Standard."""

    standard = value.get("answerEvidenceStandard")
    if not isinstance(standard, Mapping):
        raise ValueError("answer evidence qrels v2 standard is missing")
    required_standard_keys = {
        "schemaVersion",
        "standardId",
        "evaluationScope",
        "calibrationLabel",
        "candidateBlind",
        "unbiasedPromotionClaimAllowed",
        "heldOutOpened",
        "corpus",
        "chunking",
        "chunkManifest",
        "calibrationSource",
        "manifestSha256",
    }
    if set(standard) != required_standard_keys:
        raise ValueError("answer evidence qrels v2 standard fields are invalid")
    if standard.get("schemaVersion") != _ANSWER_EVIDENCE_STANDARD_V2:
        raise ValueError("answer evidence qrels v2 standard schema is invalid")
    if not str(standard.get("standardId") or "").strip():
        raise ValueError("answer evidence qrels v2 standard ID is invalid")
    if (
        standard.get("evaluationScope") != "validation-development-only"
        or standard.get("calibrationLabel") != "post-validation-calibrated"
        or not isinstance(standard.get("candidateBlind"), bool)
        or standard.get("unbiasedPromotionClaimAllowed") is not False
        or standard.get("heldOutOpened") is not False
    ):
        raise ValueError("answer evidence qrels v2 standard claim boundary is invalid")
    claimed_standard_sha256 = _require_sha256(
        standard.get("manifestSha256"),
        label="answer evidence qrels v2 standard manifest hash",
    )
    unsigned_standard = {
        str(key): item
        for key, item in standard.items()
        if str(key) != "manifestSha256"
    }
    if claimed_standard_sha256 != _sha256_json(unsigned_standard):
        raise ValueError("answer evidence qrels v2 standard manifest hash is invalid")
    if str(value.get("standardManifestSha256") or "") != claimed_standard_sha256:
        raise ValueError("answer evidence qrels v2 standard binding drifted")
    if (
        value.get("calibrationLabel") != standard.get("calibrationLabel")
        or value.get("unbiasedPromotionClaimAllowed") is not False
    ):
        raise ValueError("answer evidence qrels v2 calibration boundary is invalid")

    corpus = standard.get("corpus")
    if not isinstance(corpus, Mapping) or set(corpus) != {
        "preparedArtifactSha256",
        "preparedSourceSha256",
        "documentCount",
    }:
        raise ValueError("answer evidence qrels v2 standard corpus is invalid")
    if not prepared_artifact_sha256:
        raise ValueError("answer evidence qrels v2 prepared artifact hash is required")
    if (
        str(corpus.get("preparedArtifactSha256") or "")
        != str(prepared_artifact_sha256)
        or str(corpus.get("preparedSourceSha256") or "")
        != str(prepared_source_sha256)
        or corpus.get("documentCount") != len(document_text_by_id)
    ):
        raise ValueError("answer evidence qrels v2 standard corpus fixed point drifted")
    _require_sha256(
        corpus.get("preparedArtifactSha256"),
        label="answer evidence qrels v2 prepared artifact hash",
    )
    _require_sha256(
        corpus.get("preparedSourceSha256"),
        label="answer evidence qrels v2 prepared source hash",
    )

    chunking = standard.get("chunking")
    dependency_surface = _answer_evidence_chunker_dependency_surface()
    if not isinstance(chunking, Mapping) or set(chunking) != {
        "config",
        "configSha256",
        "dependencySurface",
        "dependencySurfaceSha256",
    }:
        raise ValueError("answer evidence qrels v2 standard chunking is invalid")
    if (
        chunking.get("config") != dict(chunking_config)
        or str(chunking.get("configSha256") or "")
        != _sha256_json(dict(chunking_config))
        or chunking.get("dependencySurface") != dependency_surface
        or str(chunking.get("dependencySurfaceSha256") or "")
        != _sha256_json(dependency_surface)
    ):
        raise ValueError("answer evidence qrels v2 chunker dependency fixed point drifted")

    chunk_manifest = standard.get("chunkManifest")
    expected_manifest = _answer_evidence_chunk_manifest(
        document_ids=list(document_text_by_id),
        source_chunks=source_chunks,
    )
    serialization = dict(_ANSWER_EVIDENCE_CHUNK_MANIFEST_SERIALIZATION)
    if not isinstance(chunk_manifest, Mapping) or set(chunk_manifest) != {
        "serialization",
        "serializationSha256",
        "chunkCount",
        "manifestSha256",
    }:
        raise ValueError("answer evidence qrels v2 chunk manifest is invalid")
    if (
        chunk_manifest.get("serialization") != serialization
        or str(chunk_manifest.get("serializationSha256") or "")
        != _sha256_json(serialization)
        or chunk_manifest.get("chunkCount") != expected_manifest["chunkCount"]
        or str(chunk_manifest.get("manifestSha256") or "")
        != expected_manifest["manifestSha256"]
    ):
        raise ValueError("answer evidence qrels v2 chunk manifest fixed point drifted")

    calibration_source = standard.get("calibrationSource")
    if not isinstance(calibration_source, Mapping) or set(calibration_source) != {
        "auditId",
        "auditReceiptSha256",
        "proposalSha256",
    }:
        raise ValueError("answer evidence qrels v2 calibration source is invalid")
    if not str(calibration_source.get("auditId") or "").strip():
        raise ValueError("answer evidence qrels v2 calibration audit ID is invalid")
    _require_sha256(
        calibration_source.get("auditReceiptSha256"),
        label="answer evidence qrels v2 calibration audit hash",
    )
    proposal_sha256 = _require_sha256(
        calibration_source.get("proposalSha256"),
        label="answer evidence qrels v2 calibration proposal hash",
    )
    if str(value.get("candidateBlindProposalSha256") or "") != proposal_sha256:
        raise ValueError("answer evidence qrels v2 calibration proposal binding drifted")


def _qrels_v1_support_group_mode(_raw_fact: Mapping[str, object]) -> str:
    return "all"


def _qrels_v2_support_group_mode(raw_fact: Mapping[str, object]) -> str:
    return str(raw_fact.get("supportGroupMode") or "")


def _validate_answer_evidence_qrels(
    value: Mapping[str, object],
    *,
    selected_cases: list[Mapping[str, object]],
    document_text_by_id: Mapping[str, str],
    prepared_source_sha256: str,
    prepared_artifact_sha256: str = "",
    evaluation_split: str,
    chunking_config: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, int]]:
    """Validate host-only fact qrels against exact source documents and chunks."""

    if not isinstance(value, Mapping):
        raise ValueError("answer evidence qrels must be an object")
    schema_version = str(value.get("schemaVersion") or "")
    version_validators = {
        _ANSWER_EVIDENCE_QRELS_V1: _validate_answer_evidence_qrels_v1_contract,
        _ANSWER_EVIDENCE_QRELS_V2: _validate_answer_evidence_qrels_v2_contract,
    }
    version_validator = version_validators.get(schema_version)
    if version_validator is None:
        raise ValueError("answer evidence qrels schema is invalid")
    claimed_manifest_sha256 = str(value.get("manifestSha256") or "")
    canonical_payload = {
        str(key): item
        for key, item in value.items()
        if str(key) != "manifestSha256"
    }
    if (
        len(claimed_manifest_sha256) != 64
        or claimed_manifest_sha256 != _sha256_json(canonical_payload)
    ):
        raise ValueError("answer evidence qrels manifest hash is invalid")
    if str(value.get("evaluationSplit") or "") != str(evaluation_split):
        raise ValueError("answer evidence qrels split does not match evaluation")
    if str(value.get("preparedSourceSha256") or "") != str(
        prepared_source_sha256
    ):
        raise ValueError("answer evidence qrels prepared source binding drifted")
    if str(value.get("chunkingConfigSha256") or "") != _sha256_json(
        dict(chunking_config)
    ):
        raise ValueError("answer evidence qrels chunking configuration drifted")

    selected_by_id: dict[str, Mapping[str, object]] = {}
    for case in selected_cases:
        query_id = str(case.get("queryId") or "").strip()
        if not query_id or query_id in selected_by_id:
            raise ValueError("selected high-level answer cases have invalid IDs")
        selected_by_id[query_id] = case
    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("answer evidence qrels must contain a cases array")
    raw_by_id: dict[str, Mapping[str, object]] = {}
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping):
            raise ValueError("answer evidence qrels contains a non-object case")
        query_id = str(raw_case.get("queryId") or "").strip()
        if not query_id or query_id in raw_by_id:
            raise ValueError("answer evidence qrels contains duplicate or missing case IDs")
        raw_by_id[query_id] = raw_case
    if set(raw_by_id) != set(selected_by_id):
        raise ValueError("answer evidence qrels do not match selected high-level cases")

    chunk_cache: dict[str, list[dict[str, Any]]] = {}

    def source_chunks(document_id: str) -> list[dict[str, Any]]:
        cached = chunk_cache.get(document_id)
        if cached is not None:
            return cached
        document_text = document_text_by_id.get(document_id)
        if document_text is None:
            raise ValueError("answer evidence qrels reference an unknown document")
        parsed = ParsedDocument(
            text=_normalize_text(document_text),
            provider="answer-evidence-qrels",
            metadata={},
        )
        chunks = _chunk_document(
            parsed,
            document_id=f"source-{hashlib.sha256(document_id.encode('utf-8')).hexdigest()[:24]}",
            base_id="answer-evidence",
            chunking_config=dict(chunking_config),
        )
        chunk_cache[document_id] = chunks
        return chunks

    version_validator(
        value,
        document_text_by_id=document_text_by_id,
        prepared_source_sha256=prepared_source_sha256,
        prepared_artifact_sha256=prepared_artifact_sha256,
        evaluation_split=evaluation_split,
        chunking_config=chunking_config,
        source_chunks=source_chunks,
    )
    support_group_mode_loader = {
        _ANSWER_EVIDENCE_QRELS_V1: _qrels_v1_support_group_mode,
        _ANSWER_EVIDENCE_QRELS_V2: _qrels_v2_support_group_mode,
    }[schema_version]

    private_qrels: dict[str, object] = {}
    stats = {
        "factCount": 0,
        "verifiedFactCount": 0,
        "unavailableFactCount": 0,
        "supportGroupCount": 0,
        "evidenceBindingCount": 0,
    }
    for query_id in sorted(selected_by_id):
        selected = selected_by_id[query_id]
        answer_facts = selected.get("answerFacts")
        if not isinstance(answer_facts, list) or not answer_facts:
            raise ValueError(f"high-level answer case {query_id} has no answer facts")
        raw_facts = raw_by_id[query_id].get("facts")
        if not isinstance(raw_facts, list) or len(raw_facts) != len(answer_facts):
            raise ValueError("answer evidence qrels fact denominator drifted")
        normalized_facts: list[dict[str, object]] = []
        for index, (raw_fact, answer_fact) in enumerate(
            zip(raw_facts, answer_facts, strict=True),
            start=1,
        ):
            if not isinstance(raw_fact, Mapping):
                raise ValueError("answer evidence qrels contains a non-object fact")
            fact_id = f"F{index}"
            fact_text = str(answer_fact or "").strip()
            if not fact_text:
                raise ValueError(f"high-level answer case {query_id} has an empty fact")
            if str(raw_fact.get("factId") or "") != fact_id:
                raise ValueError("answer evidence qrels fact order or ID drifted")
            if str(raw_fact.get("factSha256") or "") != hashlib.sha256(
                fact_text.encode("utf-8")
            ).hexdigest():
                raise ValueError("answer evidence qrels fact hash drifted")
            availability = str(raw_fact.get("availability") or "")
            raw_groups = raw_fact.get("supportGroups")
            if not isinstance(raw_groups, list):
                raise ValueError("answer evidence qrels supportGroups must be an array")
            stats["factCount"] += 1
            if availability == "unavailable":
                if raw_groups or not str(raw_fact.get("reasonCode") or "").strip():
                    raise ValueError("unavailable answer evidence qrel is malformed")
                stats["unavailableFactCount"] += 1
                normalized_facts.append(
                    {
                        "factId": fact_id,
                        "availability": "unavailable",
                        "supportGroups": [],
                    }
                )
                continue
            if availability != "verified" or not raw_groups:
                raise ValueError("verified answer evidence qrel lacks support groups")
            support_group_mode = support_group_mode_loader(raw_fact)
            if support_group_mode not in {"all", "any"}:
                raise ValueError("answer evidence qrels support group mode is invalid")
            normalized_groups: list[dict[str, object]] = []
            seen_group_ids: set[str] = set()
            for raw_group in raw_groups:
                if not isinstance(raw_group, Mapping):
                    raise ValueError("answer evidence qrels contains a non-object support group")
                group_id = str(raw_group.get("groupId") or "").strip()
                raw_evidence = raw_group.get("evidence")
                if (
                    not group_id
                    or group_id in seen_group_ids
                    or not isinstance(raw_evidence, list)
                    or not raw_evidence
                ):
                    raise ValueError("answer evidence qrels support group is invalid")
                seen_group_ids.add(group_id)
                evidence_document_ids: list[str] = []
                normalized_evidence: list[dict[str, object]] = []
                seen_bindings: set[tuple[str, int, str]] = set()
                for raw_binding in raw_evidence:
                    if not isinstance(raw_binding, Mapping):
                        raise ValueError("answer evidence qrels contains non-object evidence")
                    document_id = str(raw_binding.get("documentId") or "").strip()
                    document_text = document_text_by_id.get(document_id)
                    if document_text is None:
                        raise ValueError(
                            "answer evidence qrels reference an unknown document"
                        )
                    expected_document_sha256 = hashlib.sha256(
                        document_text.encode("utf-8")
                    ).hexdigest()
                    if str(raw_binding.get("documentSha256") or "") != expected_document_sha256:
                        raise ValueError("answer evidence qrels document hash drifted")
                    chunk_ordinal = raw_binding.get("chunkOrdinal")
                    if (
                        isinstance(chunk_ordinal, bool)
                        or not isinstance(chunk_ordinal, int)
                        or chunk_ordinal < 0
                    ):
                        raise ValueError("answer evidence qrels chunk ordinal is invalid")
                    chunks = source_chunks(document_id)
                    if chunk_ordinal >= len(chunks):
                        raise ValueError("answer evidence qrels reference an unknown source chunk")
                    chunk = chunks[chunk_ordinal]
                    chunk_sha256 = str(chunk.get("content_hash") or "")
                    if str(raw_binding.get("chunkSha256") or "") != chunk_sha256:
                        raise ValueError("answer evidence qrels source chunk hash drifted")
                    quote = str(raw_binding.get("quote") or "")
                    if (
                        not quote.strip()
                        or str(raw_binding.get("quoteSha256") or "")
                        != hashlib.sha256(quote.encode("utf-8")).hexdigest()
                        or quote not in str(chunk.get("content") or "")
                    ):
                        raise ValueError(
                            "answer evidence quote is not in the exact source chunk"
                        )
                    binding_key = (document_id, chunk_ordinal, quote)
                    if binding_key in seen_bindings:
                        raise ValueError("answer evidence qrels contain duplicate evidence")
                    seen_bindings.add(binding_key)
                    if document_id not in evidence_document_ids:
                        evidence_document_ids.append(document_id)
                    normalized_evidence.append(
                        {
                            "documentId": document_id,
                            "chunkOrdinal": chunk_ordinal,
                        }
                    )
                    stats["evidenceBindingCount"] += 1
                normalized_group: dict[str, object] = {
                    "groupId": group_id,
                    "documentIds": evidence_document_ids,
                }
                if schema_version == _ANSWER_EVIDENCE_QRELS_V2:
                    normalized_group["evidence"] = normalized_evidence
                normalized_groups.append(normalized_group)
                stats["supportGroupCount"] += 1
            stats["verifiedFactCount"] += 1
            normalized_facts.append(
                {
                    "factId": fact_id,
                    "availability": "verified",
                    "supportGroupMode": support_group_mode,
                    "supportGroups": normalized_groups,
                }
            )
        private_qrels[query_id] = {"facts": normalized_facts}
    if schema_version == _ANSWER_EVIDENCE_QRELS_V2:
        declared_counts = value.get("counts")
        expected_counts = {
            "caseCount": len(raw_by_id),
            **stats,
        }
        if not isinstance(declared_counts, Mapping) or dict(declared_counts) != {
            "caseCount": expected_counts["caseCount"],
            "factCount": expected_counts["factCount"],
            "supportGroupCount": expected_counts["supportGroupCount"],
            "evidenceBindingCount": expected_counts["evidenceBindingCount"],
        }:
            raise ValueError("answer evidence qrels v2 declared counts drifted")
    return private_qrels, stats


def _case_binding_map(
    cases: list[Mapping[str, object]],
    *,
    label: str,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in cases:
        query_id = str(item.get("queryId") or "").strip()
        if not query_id or query_id in result:
            raise ValueError(f"{label} contains duplicate or missing query IDs")
        relevant = item.get("relevant")
        result[query_id] = {
            "queryId": query_id,
            "query": str(item.get("query") or item.get("question") or "").strip(),
            "split": str(item.get("split") or ""),
            "slice": str(item.get("slice") or ""),
            "retrievalEvaluable": item.get("retrievalEvaluable") is not False,
            "relevant": {
                str(key): float(value)
                for key, value in relevant.items()
            }
            if isinstance(relevant, Mapping)
            else {},
        }
    return result


def _answer_case_identity(item: Mapping[str, object]) -> dict[str, object]:
    facts = item.get("answerFacts")
    return {
        "queryId": str(item.get("queryId") or ""),
        "query": str(item.get("query") or item.get("question") or ""),
        "split": str(item.get("split") or ""),
        "slice": str(item.get("slice") or ""),
        "retrievalEvaluable": item.get("retrievalEvaluable") is not False,
        "abstentionExpected": bool(item.get("abstentionExpected")),
        "answerSha256": hashlib.sha256(
            str(item.get("goldAnswer") or item.get("answer") or "").encode("utf-8")
        ).hexdigest(),
        "answerFactsSha256": _sha256_json(
            [str(value) for value in facts] if isinstance(facts, list) else []
        ),
    }


def _authorize_held_out(
    *,
    promotion: Mapping[str, object],
    gate: Mapping[str, object],
    source_prepared_sha256: str,
    source_answer_cases_sha256: str,
    source_retrieval_report_sha256: str,
    retrieval_report_sha256: str,
    retrieval_config_sha256: str,
    prompt_config_sha256: str,
    runtime_contract_sha256: str,
) -> dict[str, object]:
    if promotion.get("schemaVersion") != "paw.enterprise-rag-validation-promotion.v1":
        raise ValueError("held-out promotion schema is unsupported")
    promotion_hash = str(promotion.get("promotionReceiptSha256") or "")
    unsigned_promotion = {
        key: value
        for key, value in promotion.items()
        if key != "promotionReceiptSha256"
    }
    if not promotion_hash or _sha256_json(unsigned_promotion) != promotion_hash:
        raise ValueError("held-out promotion receipt hash is invalid")
    if promotion.get("decision") != "keep":
        raise ValueError("held-out promotion decision is not keep")
    if promotion.get("state") != "promoted":
        raise ValueError("held-out promotion state is not promoted")
    bindings = promotion.get("bindings")
    validation_report = promotion.get("validationAgentReport")
    promoted_retrieval_report = promotion.get("retrievalReport")
    identity = promotion.get("identity")
    winner = promotion.get("winner")
    if (
        not isinstance(bindings, Mapping)
        or not isinstance(validation_report, Mapping)
        or not isinstance(promoted_retrieval_report, Mapping)
        or not isinstance(identity, Mapping)
        or not isinstance(winner, Mapping)
        or promotion.get("heldOutObserved") is not False
    ):
        raise ValueError("held-out promotion is not validation-only")
    pi_runtime_identity = identity.get("piRuntime")
    model_route_identity = identity.get("modelRoute")
    if not isinstance(pi_runtime_identity, Mapping) or not isinstance(
        model_route_identity, Mapping
    ):
        raise ValueError("held-out promotion identity is incomplete")
    promotion_bindings = dict(bindings)
    required_binding_fields = {
        "validationAgentReportFileSha256",
        "validationAgentReportSha256",
        "retrievalReportFileSha256",
        "retrievalReportSha256",
        "sourcePreparedSha256",
        "sourceAnswerCasesSha256",
        "sourceRetrievalReportSha256",
        "caseIds",
        "caseIdsSha256",
        "caseSetSha256",
        "answerCaseManifestSha256",
        "answerCaseSetSha256",
        "promptConfigSha256",
        "runtimeContractSha256",
        "piRuntimeIdentitySha256",
        "modelRouteIdentitySha256",
        "retrievalConfigSha256",
    }
    if not required_binding_fields.issubset(promotion_bindings) or any(
        promotion_bindings.get(key) in (None, "", [])
        for key in required_binding_fields
    ):
        raise ValueError("held-out promotion bindings are incomplete")
    if (
        validation_report.get("fileSha256")
        != promotion_bindings["validationAgentReportFileSha256"]
        or validation_report.get("reportSha256")
        != promotion_bindings["validationAgentReportSha256"]
        or promoted_retrieval_report.get("fileSha256")
        != promotion_bindings["retrievalReportFileSha256"]
        or promoted_retrieval_report.get("reportSha256")
        != promotion_bindings["retrievalReportSha256"]
        or winner.get("reportSha256")
        != promotion_bindings["retrievalReportSha256"]
        or winner.get("retrievalConfigSha256")
        != promotion_bindings["retrievalConfigSha256"]
        or _sha256_json(winner.get("retrievalConfig"))
        != promotion_bindings["retrievalConfigSha256"]
        or pi_runtime_identity.get("identitySha256")
        != promotion_bindings["piRuntimeIdentitySha256"]
        or model_route_identity.get("identitySha256")
        != promotion_bindings["modelRouteIdentitySha256"]
    ):
        raise ValueError("held-out promotion artifact binding is invalid")
    if str(winner.get("reportSha256") or "") != retrieval_report_sha256:
        raise ValueError("held-out promotion does not match retrieval report")
    if str(winner.get("retrievalConfigSha256") or "") != retrieval_config_sha256:
        raise ValueError("held-out promotion does not match retrieval config")
    if gate.get("schemaVersion") != "paw.enterprise-rag-heldout-gate.v1":
        raise ValueError("held-out gate schema is unsupported")
    gate_hash = str(gate.get("gateReceiptSha256") or "")
    unsigned_gate = {
        key: value for key, value in gate.items() if key != "gateReceiptSha256"
    }
    if not gate_hash or _sha256_json(unsigned_gate) != gate_hash:
        raise ValueError("held-out gate receipt hash is invalid")
    if gate.get("state") != "unlocked":
        raise ValueError("held-out gate is not unlocked")
    if gate.get("heldOutObserved") is not False:
        raise ValueError("held-out gate already observed held-out data")
    if int(gate.get("maximumEvaluations") or 0) != 1 or int(
        gate.get("consumedEvaluations") or 0
    ) != 0:
        raise ValueError("held-out one-shot budget is unavailable")
    expected_gate_fields = {
        "schemaVersion",
        "state",
        "heldOutObserved",
        "maximumEvaluations",
        "consumedEvaluations",
        "promotionReceiptSha256",
        "gateReceiptSha256",
        *promotion_bindings.keys(),
    }
    if set(gate) != expected_gate_fields or any(
        gate.get(key) != value for key, value in promotion_bindings.items()
    ):
        raise ValueError("held-out gate does not bind every promotion field")
    expected = {
        "promotionReceiptSha256": promotion_hash,
        "sourcePreparedSha256": source_prepared_sha256,
        "sourceAnswerCasesSha256": source_answer_cases_sha256,
        "sourceRetrievalReportSha256": source_retrieval_report_sha256,
        "retrievalReportFileSha256": source_retrieval_report_sha256,
        "retrievalReportSha256": retrieval_report_sha256,
        "retrievalConfigSha256": retrieval_config_sha256,
        "promptConfigSha256": prompt_config_sha256,
        "runtimeContractSha256": runtime_contract_sha256,
    }
    for key, value in expected.items():
        if str(gate.get(key) or "") != str(value):
            raise ValueError(f"held-out gate does not match {key}")
    receipt: dict[str, object] = {
        "schemaVersion": "rag-ime.rag-heldout-authorization.v1",
        "authorized": True,
        **expected,
        "maximumEvaluations": 1,
        "consumedEvaluationsBeforeRun": 0,
        "gateReceiptSha256": gate_hash,
        "authorityBindingsSha256": _sha256_json(promotion_bindings),
    }
    receipt["authorizationSha256"] = _sha256_json(receipt)
    return receipt


def _validate_held_out_runtime_authority(
    promotion: Mapping[str, object],
    *,
    pi_runtime_identity_sha256: str,
    model_route_identity_sha256: str,
) -> None:
    bindings = promotion.get("bindings")
    if not isinstance(bindings, Mapping):
        raise ValueError("held-out promotion bindings are unavailable")
    expected = {
        "piRuntimeIdentitySha256": pi_runtime_identity_sha256,
        "modelRouteIdentitySha256": model_route_identity_sha256,
    }
    for key, value in expected.items():
        if not value or str(bindings.get(key) or "") != str(value):
            raise ValueError(f"held-out runtime authority does not match {key}")


def _claim_held_out_gate(
    gate_path: Path,
    *,
    authorization: Mapping[str, object],
    claim_registry_root: Path | None = None,
) -> dict[str, object]:
    """Atomically consume one authority identity before private case access."""

    if authorization.get("authorized") is not True:
        raise ValueError("held-out gate authorization is unavailable")
    requested_gate = gate_path.expanduser()
    if requested_gate.is_symlink():
        raise ValueError("held-out gate must not be a symlink")
    gate = requested_gate.resolve(strict=True)
    if not gate.is_file():
        raise ValueError("held-out gate must be a regular file")
    gate_receipt_sha256 = str(authorization.get("gateReceiptSha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", gate_receipt_sha256) is None:
        raise ValueError("held-out gate receipt identity is invalid")
    requested_registry = (
        claim_registry_root.expanduser()
        if claim_registry_root is not None
        else (
            Path(
                os.environ.get("RAG_IME_APP_SUPPORT_DIR", "").strip()
                or Path.home() / "Library" / "Application Support" / "RagIme"
            ).expanduser()
            / "eval"
            / "heldout-gate-claims"
        )
    )
    if requested_registry.exists() and requested_registry.is_symlink():
        raise ValueError("held-out claim registry must not be a symlink")
    requested_registry.mkdir(mode=0o700, parents=True, exist_ok=True)
    claim_registry = requested_registry.resolve(strict=True)
    if not claim_registry.is_dir() or claim_registry.is_symlink():
        raise ValueError("held-out claim registry must be a real directory")
    os.chmod(claim_registry, 0o700)
    claim_path = claim_registry / f"{gate_receipt_sha256}.consumed.json"
    claim: dict[str, object] = {
        "schemaVersion": "rag-ime.rag-heldout-gate-claim.v1",
        "promotionReceiptSha256": str(
            authorization.get("promotionReceiptSha256") or ""
        ),
        "gateReceiptSha256": gate_receipt_sha256,
        "authorizationSha256": str(
            authorization.get("authorizationSha256") or ""
        ),
        "authorityBindingsSha256": str(
            authorization.get("authorityBindingsSha256") or ""
        ),
        "claimedAtMs": int(time.time() * 1_000),
    }
    claim["claimSha256"] = _sha256_json(claim)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(claim_path, flags, 0o600)
    except FileExistsError as exc:
        raise ValueError("held-out gate was already consumed") from exc
    try:
        payload = (
            json.dumps(
                claim,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(claim_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return claim


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scene-recipe", type=Path,
        help="Frozen Agent Lab Validation recipe binding JSON; never rereads the live scene Store.",
    )
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument(
        "--answer-cases",
        type=Path,
        help="Host-private task-cases JSON required by --answer-only.",
    )
    parser.add_argument(
        "--answer-evidence-qrels",
        type=Path,
        help=(
            "Host-private fact-to-source evidence manifest required by --answer-only; "
            "never included in Agent prompts or the public report."
        ),
    )
    parser.add_argument("--retrieval-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    checkpoint_group = parser.add_mutually_exclusive_group()
    checkpoint_group.add_argument(
        "--checkpoint",
        type=Path,
        help="Create a fresh append-only Validation lane-attempt checkpoint.",
    )
    checkpoint_group.add_argument(
        "--resume-checkpoint",
        type=Path,
        help="Explicitly resume a matching Validation lane-attempt checkpoint.",
    )
    parser.add_argument(
        "--private-root",
        type=Path,
        default=ROOT / ".rag-ime-data" / "runs" / "rag-agent-ablation",
    )
    parser.add_argument(
        "--source-agent-config",
        type=Path,
        default=(
            Path.home()
            / "Library"
            / "Application Support"
            / "RagIme"
            / "Agent"
            / "config"
        ),
    )
    parser.add_argument("--slice-seed", default="paw-retrieval-experiment-v1")
    parser.add_argument("--agent-seed", default="paw-agent-ablation-v1")
    parser.add_argument("--slice-cases-per-split", type=int, default=60)
    parser.add_argument("--agent-case-limit", type=int, default=4)
    parser.add_argument(
        "--evaluation-split",
        choices=("validation", "held_out"),
        default=None,
        help=(
            "Split used by evaluation. Answer-only defaults to validation; "
            "retrieval ablation defaults to held_out."
        ),
    )
    parser.add_argument(
        "--answer-only",
        action="store_true",
        help="Evaluate high-level answer, citation coverage, and real abstention cases.",
    )
    parser.add_argument(
        "--promotion-receipt",
        type=Path,
        help="Validation promotion receipt required before answer-only held-out evaluation.",
    )
    parser.add_argument(
        "--heldout-gate",
        type=Path,
        help="Matching unlocked one-shot gate required before answer-only held-out evaluation.",
    )
    parser.add_argument(
        "--pi-runtime-payload",
        type=Path,
        help="Explicit verified managed Pi Runtime payload used instead of the installed pointer.",
    )
    parser.add_argument(
        "--cost-receipt-output",
        type=Path,
        help=(
            "Write an exact Runtime-reconciled cost receipt before the ephemeral "
            "evaluation Runtime DB is removed."
        ),
    )
    parser.add_argument(
        "--pricing-config",
        type=Path,
        help="Hash-bound model pricing config required by --cost-receipt-output.",
    )
    parser.add_argument(
        "--pricing-published-date",
        help="Published date bound into the exact cost receipt.",
    )
    parser.add_argument(
        "--pricing-source-url",
        default="https://platform.openai.com/docs/pricing",
    )
    parser.add_argument(
        "--cost-run-id",
        help="Stable run ID bound into the exact cost receipt; defaults to output stem.",
    )
    parser.add_argument(
        "--development-report",
        action="append",
        type=Path,
        default=[],
        help="Prior local Agent report whose observed case IDs must be excluded from this run.",
    )
    parser.add_argument("--distractor-limit", type=int, default=1_000)
    parser.add_argument(
        "--reranker-model",
        type=Path,
        help="Local MLX Qwen3 reranker required when the frozen winner enables reranking.",
    )
    parser.add_argument("--reranker-revision", default="")
    parser.add_argument(
        "--reranker-cache",
        type=Path,
        help="Optional private hash-only pair-score cache shared with retrieval evaluation.",
    )
    parser.add_argument(
        "--rerank-instruction",
        default=QWEN3_RERANKER_DEFAULT_INSTRUCTION,
    )
    parser.add_argument("--timeout-seconds", type=float, default=420.0)
    parser.add_argument(
        "--lane-attempts",
        type=int,
        default=2,
        help="Maximum attempts per lane; retries are used only for classified Provider transients.",
    )
    parser.add_argument(
        "--agentic-supplemental-limit",
        type=int,
        choices=(3, 6),
        default=None,
        help=(
            "Frozen global supplemental-search budget for the agentic lane; "
            "3 is the cost candidate and 6 is the incumbent."
        ),
    )
    parser.add_argument(
        "--model-override",
        choices=_EVALUATION_MODELS,
        default=None,
        help=(
            "Frozen OpenAI Codex model for parent and delegated reviewer routes. "
            "The answer Judge is fixed independently by --judge-model."
        ),
    )
    parser.add_argument(
        "--judge-model", choices=_EVALUATION_MODELS, default=_EVALUATION_MODEL,
        help="Frozen answer Judge model; does not follow the candidate model.",
    )
    parser.add_argument(
        "--candidate-prompt-file", type=Path,
        help="UTF-8 candidate instructions appended to the frozen business prompt; development Validation only.",
    )
    parser.add_argument(
        "--prompt-profile",
        choices=_PROMPT_PROFILES,
        default=None,
        help=(
            "Prompt-only development variable. Post-Validation prompt profiles "
            "are Validation-only and never eligible for an unbiased promotion claim."
        ),
    )
    parser.add_argument(
        "--calibration-no-metal",
        action="store_true",
        help=(
            "Run a non-accepting prompt calibration with hashing vectors and a "
            "rank-preserving rerank stage; never use this report as a formal score."
        ),
    )
    parser.add_argument(
        "--development-only",
        action="store_true",
        help=(
            "Run the real model, semantic index, and reranker as a non-accepting "
            "development replay; never use this report as a formal score."
        ),
    )
    args = parser.parse_args(argv)
    scene_recipe: dict[str, object] | None = None
    if args.scene_recipe is not None:
        try:
            scene_recipe = validate_scene_recipe_binding(_read_json_object(args.scene_recipe))
        except (OSError, ValueError, TypeError) as exc:
            parser.error(f"invalid --scene-recipe: {exc}")
        recipe = scene_recipe["recipe"]
        for name, expected in (
            ("model_override", recipe["model"]),
            ("prompt_profile", recipe["promptProfile"]),
            ("agentic_supplemental_limit", recipe["agenticSupplementalLimit"]),
            ("evaluation_split", recipe["split"]),
        ):
            actual = getattr(args, name)
            if actual is not None and actual != expected:
                parser.error(f"--{name.replace('_', '-')} conflicts with --scene-recipe")
            setattr(args, name, expected)
        if args.calibration_no_metal or args.promotion_receipt is not None or args.heldout_gate is not None:
            parser.error("--scene-recipe is real-model development Validation only; calibration and promotion flags conflict")
        args.answer_only = True
        args.development_only = True
    if args.model_override is None:
        args.model_override = _EVALUATION_MODEL
    if args.prompt_profile is None:
        args.prompt_profile = _INCUMBENT_PROMPT_PROFILE
    if args.agentic_supplemental_limit is None:
        args.agentic_supplemental_limit = _AGENTIC_MAX_SUPPLEMENTAL_TOTAL
    if args.calibration_no_metal and args.development_only:
        parser.error("--calibration-no-metal and --development-only are mutually exclusive")
    evaluation_split = _resolve_evaluation_split(
        args.evaluation_split,
        answer_only=bool(args.answer_only),
    )
    if bool(args.answer_only) and args.answer_evidence_qrels is None:
        parser.error("--answer-evidence-qrels is required with --answer-only")
    if not bool(args.answer_only) and args.answer_evidence_qrels is not None:
        parser.error("--answer-evidence-qrels requires --answer-only")
    if bool(args.answer_only) and evaluation_split == "held_out" and (
        args.promotion_receipt is None or args.heldout_gate is None
    ):
        parser.error(
            "answer-only held_out requires --promotion-receipt and --heldout-gate"
        )
    cost_requested = any(
        value is not None
        for value in (
            args.cost_receipt_output,
            args.pricing_config,
            args.pricing_published_date,
            args.cost_run_id,
        )
    )
    if cost_requested and (
        args.cost_receipt_output is None
        or args.pricing_config is None
        or not str(args.pricing_published_date or "").strip()
    ):
        parser.error(
            "exact cost export requires --cost-receipt-output, --pricing-config, "
            "and --pricing-published-date"
        )
    if cost_requested and args.resume_checkpoint is not None:
        parser.error(
            "full-run cost export cannot omit reused lane receipts; resume without cost-export flags "
            "and reconcile the retained original and resumed Runtime evidence before comparing cost"
        )
    checkpoint_argument = args.resume_checkpoint or args.checkpoint
    checkpoint_path = (
        checkpoint_argument.expanduser().resolve(strict=False)
        if checkpoint_argument is not None
        else None
    )
    try:
        if args.candidate_prompt_file is not None and not (
            args.answer_only and args.development_only and evaluation_split == "validation"
        ):
            raise ValueError("candidate Prompt requires answer-only development Validation")
        candidate_prompt = load_candidate_prompt(args.candidate_prompt_file, evaluation_split=evaluation_split)
        prompt_profile = _validate_prompt_profile(
            args.prompt_profile,
            answer_only=bool(args.answer_only),
            evaluation_split=evaluation_split,
            development_only=bool(args.development_only),
        )
        _validate_checkpoint_request(
            evaluation_split=evaluation_split,
            checkpoint_path=checkpoint_path,
            resume_checkpoint=args.resume_checkpoint is not None,
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    private_root = args.private_root.expanduser().resolve(strict=False)
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_root.chmod(0o700)
    output = args.output.expanduser().resolve(strict=False)
    if checkpoint_path is not None and checkpoint_path == output:
        parser.error("checkpoint and final output paths must differ")
    cost_receipt_output = (
        args.cost_receipt_output.expanduser().resolve(strict=False)
        if args.cost_receipt_output is not None
        else None
    )
    if cost_receipt_output is not None and cost_receipt_output in {
        output,
        checkpoint_path,
    }:
        parser.error("cost receipt, checkpoint, and final output paths must differ")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_root = Path(tempfile.mkdtemp(prefix="run-", dir=private_root)).resolve(strict=True)
    try:
        _write_json(run_root / "recovery.json", {
            "schemaVersion": "rag-ime.rag-evaluation-recovery-locator.v1",
            "reportOutput": str(output),
            "costReceiptOutput": str(cost_receipt_output) if cost_receipt_output else None,
            "purpose": "Retained only when evaluation or receipt export fails; reuse evidence without rerunning paid turns.",
        })
        report = _run(
            run_root,
            prepared_path=args.prepared.expanduser().resolve(strict=True),
            answer_cases_path=(
                args.answer_cases.expanduser().resolve(strict=True)
                if args.answer_cases is not None
                else None
            ),
            answer_evidence_qrels_path=(
                args.answer_evidence_qrels.expanduser().resolve(strict=True)
                if args.answer_evidence_qrels is not None
                else None
            ),
            retrieval_report_path=args.retrieval_report.expanduser().resolve(strict=True),
            source_agent_config=args.source_agent_config.expanduser().resolve(strict=True),
            slice_seed=str(args.slice_seed),
            agent_seed=str(args.agent_seed),
            slice_cases_per_split=int(args.slice_cases_per_split),
            agent_case_limit=int(args.agent_case_limit),
            distractor_limit=int(args.distractor_limit),
            timeout_seconds=max(60.0, float(args.timeout_seconds)),
            lane_attempts=max(1, min(3, int(args.lane_attempts))),
            agentic_supplemental_limit=int(args.agentic_supplemental_limit),
            prompt_profile=prompt_profile,
            reranker_model=(
                args.reranker_model.expanduser().resolve(strict=False)
                if args.reranker_model is not None
                else None
            ),
            reranker_revision=str(args.reranker_revision),
            reranker_cache=(
                args.reranker_cache.expanduser().resolve(strict=False)
                if args.reranker_cache is not None
                else None
            ),
            rerank_instruction=str(args.rerank_instruction),
            development_report_paths=[
                path.expanduser().resolve(strict=True)
                for path in args.development_report
            ],
            calibration_no_metal=bool(args.calibration_no_metal),
            development_only=bool(args.development_only),
            evaluation_split=evaluation_split,
            answer_only=bool(args.answer_only),
            promotion_receipt_path=(
                args.promotion_receipt.expanduser().resolve(strict=True)
                if args.promotion_receipt is not None
                else None
            ),
            heldout_gate_path=(
                args.heldout_gate.expanduser().resolve(strict=True)
                if args.heldout_gate is not None
                else None
            ),
            pi_runtime_payload=(
                args.pi_runtime_payload.expanduser().resolve(strict=True)
                if args.pi_runtime_payload is not None
                else None
            ),
            checkpoint_path=checkpoint_path,
            resume_checkpoint=args.resume_checkpoint is not None,
            evaluation_model=str(args.model_override),
            judge_model=str(args.judge_model),
            candidate_prompt=candidate_prompt,
            scene_recipe=scene_recipe,
        )
        _write_json(output, report)
        if cost_receipt_output is not None:
            runtime_db = run_root / "agent.sqlite"
            preflight = report.get("preflight")
            completed_lane_evidence = report.get("completedLaneEvidence")
            zero_provider_runtime_failure = (
                report.get("passed") is not True
                and report.get("scoreEligible") is False
                and isinstance(completed_lane_evidence, list)
                and bool(completed_lane_evidence)
                and all(
                    isinstance(item, Mapping)
                    and isinstance(item.get("usage"), Mapping)
                    and int(item["usage"].get("providerRequestCount") or 0) == 0
                    and bool(str(item.get("runtimeFailureCategory") or ""))
                    for item in completed_lane_evidence
                )
            )
            if not runtime_db.is_file() and (
                isinstance(preflight, Mapping)
                and preflight.get("accepted") is False
            ):
                _progress(
                    "cost_receipt_skipped",
                    reason="preflight_failed_before_runtime_db",
                    output=str(cost_receipt_output),
                )
            elif zero_provider_runtime_failure:
                _progress(
                    "cost_receipt_skipped",
                    reason="zero_provider_requests_after_runtime_failure",
                    output=str(cost_receipt_output),
                )
            else:
                if not runtime_db.is_file():
                    raise RuntimeError(
                        "exact Runtime cost receipt export requires agent.sqlite"
                    )
                from scripts.build_agent_lab_cost_receipt_from_runtime_db import (
                    main as build_agent_lab_cost_receipt,
                )

                cost_status = build_agent_lab_cost_receipt(
                    [
                        "--runtime-db",
                        str(runtime_db),
                        "--pricing-config",
                        str(args.pricing_config.expanduser().resolve(strict=True)),
                        *(["--model", str(args.model_override)] if args.judge_model == args.model_override else ["--all-models"]),
                        "--run-id",
                        str(args.cost_run_id or output.stem),
                        "--published-date",
                        str(args.pricing_published_date),
                        "--source-url",
                        str(args.pricing_source_url),
                        "--output",
                        str(cost_receipt_output),
                    ]
                )
                if cost_status != 0:
                    raise RuntimeError("exact Runtime cost receipt export failed")
    except BaseException:
        _progress("run_evidence_retained", privateRunRoot=str(run_root), reason="execution_or_receipt_export_failed")
        raise
    if report.get("passed") is True:
        shutil.rmtree(run_root)
    else:
        _progress("run_evidence_retained", privateRunRoot=str(run_root), reason="evaluation_not_passed")
    print(
        json.dumps(
            {
                "event": "completed",
                "passed": report.get("passed"),
                "output": str(output),
                "reportSha256": report.get("reportSha256"),
                "knowledgeComparison": report.get("knowledgeAblation", {}).get("comparison"),
                "agentComparison": report.get("agentAblation", {}).get("comparison"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if report.get("passed") is True else 1


class _RagControlledEvents:
    def __init__(self, owner):
        self.owner = owner

    def __getattr__(self, name):
        return getattr(self.owner._service.events, name)

    def replay(self, *args, **kwargs):
        self.owner.check()
        result = self.owner._service.events.replay(*args, **kwargs)
        self.owner.check()
        return result


class _RagControlledService:
    """Forward the existing Pi owner; guard admission and observe exact bindings.

    Every lane, Judge and repair already uses this service boundary. Keeping
    the guard here covers those paths without creating a second model loop.
    Cleanup/close and read-only evidence access remain available after Stop.
    """
    def __init__(self, service, *, cancelled=None, on_session=None, on_turn=None):
        self._service = service
        self._cancelled = cancelled
        self._on_session = on_session
        self._on_turn = on_turn
        self._current_session = ""
        self._stopped = False
        self.events = _RagControlledEvents(self)

    def __getattr__(self, name):
        return getattr(self._service, name)

    def check(self):
        if self._stopped or (self._cancelled is not None and self._cancelled()):
            if not self._stopped:
                self._stopped = True
                if self._current_session:
                    try:
                        self._service.abort(self._current_session)
                    except Exception as exc:
                        raise RagEvaluationCancelled("RAG evaluation cancelled; Pi abort failed", interrupted=True) from exc
            raise RagEvaluationCancelled("RAG evaluation cancelled")

    def create_session(self, *args, **kwargs):
        self.check()
        result = self._service.create_session(*args, **kwargs)
        self._current_session = str(result["session"]["id"])
        if self._on_session is not None:
            self._on_session(self._current_session)
        return result

    def ensure_runtime(self, *args, **kwargs):
        self.check()
        return self._service.ensure_runtime(*args, **kwargs)

    def prompt(self, session_id, *args, **kwargs):
        self._current_session = session_id
        self.check()
        result = self._service.prompt(session_id, *args, **kwargs)
        turn_id = str(result.get("turnId") or "")
        if turn_id and self._on_turn is not None:
            self._on_turn(session_id, turn_id)
        # Return the admission receipt so the existing attempt checkpoint can
        # bind it before polling observes Stop. Never lose an admitted turn.
        return result


def _execution_service(service, *, cancelled=None, on_session=None, on_turn=None):
    if isinstance(service, _RagControlledService):
        service.check()
        return service
    if cancelled is None and on_session is None and on_turn is None:
        return service
    result = _RagControlledService(service, cancelled=cancelled, on_session=on_session, on_turn=on_turn)
    result.check()
    return result


def _execution_check(service):
    if isinstance(service, _RagControlledService):
        service.check()


def _run(
    run_root: Path,
    *,
    prepared_path: Path,
    answer_cases_path: Path | None,
    answer_evidence_qrels_path: Path | None,
    retrieval_report_path: Path,
    source_agent_config: Path,
    slice_seed: str,
    agent_seed: str,
    slice_cases_per_split: int,
    agent_case_limit: int,
    distractor_limit: int,
    timeout_seconds: float,
    lane_attempts: int,
    reranker_model: Path | None,
    reranker_revision: str,
    reranker_cache: Path | None,
    rerank_instruction: str,
    development_report_paths: list[Path],
    calibration_no_metal: bool = False,
    development_only: bool = False,
    evaluation_split: str = "validation",
    answer_only: bool = False,
    promotion_receipt_path: Path | None = None,
    heldout_gate_path: Path | None = None,
    pi_runtime_payload: Path | None = None,
    checkpoint_path: Path | None = None,
    resume_checkpoint: bool = False,
    agentic_supplemental_limit: int = _AGENTIC_MAX_SUPPLEMENTAL_TOTAL,
    prompt_profile: str = _INCUMBENT_PROMPT_PROFILE,
    evaluation_model: str = _EVALUATION_MODEL,
    judge_model: str = _EVALUATION_MODEL,
    candidate_prompt: CandidatePrompt | None = None,
    scene_recipe: Mapping[str, object] | None = None,
    cancelled: Callable[[], bool] | None = None,
    on_session: Callable[[str], None] | None = None,
    on_turn: Callable[[str, str], None] | None = None,
) -> dict[str, object]:
    if cancelled is not None and cancelled():
        raise RagEvaluationCancelled("RAG evaluation cancelled before preparation")
    if candidate_prompt is not None and not (
        answer_only and development_only and evaluation_split == "validation"
    ):
        raise ValueError("candidate Prompt requires answer-only development Validation")
    if scene_recipe is not None:
        scene_recipe = validate_scene_recipe_binding(scene_recipe)
        recipe = scene_recipe["recipe"]
        if (
            evaluation_model != recipe["model"]
            or prompt_profile != recipe["promptProfile"]
            or agentic_supplemental_limit != recipe["agenticSupplementalLimit"]
            or evaluation_split != recipe["split"]
            or answer_only is not True
            or development_only is not True
            or calibration_no_metal
            or promotion_receipt_path is not None
            or heldout_gate_path is not None
        ):
            raise ValueError("scene recipe conflicts with explicit run controls")
    started_at_ms = int(time.time() * 1_000)
    if evaluation_model not in _EVALUATION_MODELS:
        raise ValueError("RAG Agent evaluation model is unsupported")
    if judge_model not in _EVALUATION_MODELS:
        raise ValueError("RAG Agent answer Judge model is unsupported")
    if agentic_supplemental_limit not in {3, _AGENTIC_MAX_SUPPLEMENTAL_TOTAL}:
        raise ValueError("agentic supplemental limit is unsupported")
    prompt_profile = _validate_prompt_profile(
        prompt_profile,
        answer_only=answer_only,
        evaluation_split=evaluation_split,
        development_only=development_only,
    )
    _validate_checkpoint_request(
        evaluation_split=evaluation_split,
        checkpoint_path=checkpoint_path,
        resume_checkpoint=resume_checkpoint,
    )
    checkpoint_request_projection = _lane_checkpoint_report_projection(
        None,
        resume_requested=resume_checkpoint,
        initial_attempt_count=0,
        reused_lanes=set(),
        fresh_lanes=set(),
        checkpoint_requested=checkpoint_path is not None,
    )
    if not calibration_no_metal and pi_runtime_payload is None:
        raise ValueError(
            "real-model RAG Agent evaluation requires --pi-runtime-payload"
        )
    prepared = _load_prepared(prepared_path)
    retrieval_report = _read_json_object(retrieval_report_path)
    slice_cases, documents, slice_manifest = _reconstruct_frozen_slice(
        prepared,
        retrieval_report=retrieval_report,
        seed=slice_seed,
        cases_per_split=slice_cases_per_split,
        distractor_limit=distractor_limit,
    )
    development_exclusion = _development_exclusion(
        development_report_paths,
        prepared_path=prepared_path,
        retrieval_report_path=retrieval_report_path,
    )
    answer_case_manifest: dict[str, object] = {}
    held_out_authorization: dict[str, object] = {
        "schemaVersion": "rag-ime.rag-heldout-authorization.v1",
        "authorized": False,
        "reason": "validation-only evaluation does not consume held-out",
    }
    raw_answer_cases: list[dict[str, object]] = []
    if answer_only:
        if answer_cases_path is None:
            raise ValueError("--answer-cases is required with --answer-only")
        if answer_evidence_qrels_path is None:
            raise ValueError(
                "--answer-evidence-qrels is required with --answer-only"
            )
        evaluation_cases: list[dict[str, Any]] = []
    else:
        if evaluation_split != "held_out":
            raise ValueError("retrieval ablation supports held_out only")
        evaluation_cases = select_agent_held_out_cases(
            slice_cases,
            limit=agent_case_limit,
            seed=agent_seed,
            excluded_query_ids=set(development_exclusion["caseIds"]),
        )
    baseline_record = _production_baseline_record(retrieval_report)
    default_config = dict(baseline_record["config"])
    tuned_config = dict(retrieval_report["validationSelection"]["winner"]["config"])
    default_config_sha256 = _sha256_json(default_config)
    tuned_config_sha256 = _sha256_json(tuned_config)
    if default_config_sha256 != baseline_record["configSha256"]:
        raise ValueError("retrieval report baseline config hash is invalid")
    if tuned_config_sha256 != retrieval_report["validationSelection"]["frozenConfigSha256"]:
        raise ValueError("retrieval report tuned config hash is invalid")
    prompt_config_sha256 = _sha256_json(
        {
            "promptContractVersion": _PROMPT_CONTRACT_VERSION,
            "answerJudgeContractVersion": _ANSWER_JUDGE_CONTRACT_VERSION,
            "model": f"{_EVALUATION_PROVIDER}/{evaluation_model}",
            "judgeModel": f"{_EVALUATION_PROVIDER}/{judge_model}",
            **({"candidatePrompt": candidate_prompt_identity(candidate_prompt)} if candidate_prompt is not None else {}),
            "thinking": _EVALUATION_THINKING,
            "modelRoutingSha256": _sha256_json(
                _evaluation_configuration_defaults(
                    evaluation_model=evaluation_model
                )["modelRouting"]
            ),
            "lanes": list(LANES),
            "agentSeed": agent_seed,
            "caseLimit": agent_case_limit,
            "laneAttempts": lane_attempts,
            "agenticSupplementalLimit": agentic_supplemental_limit,
            "promptProfile": prompt_profile,
            "laneTimeoutSeconds": float(timeout_seconds),
            "defaultRetrievalConfigSha256": default_config_sha256,
            "tunedRetrievalConfigSha256": tuned_config_sha256,
            **({"sceneRecipe": scene_recipe} if scene_recipe is not None else {}),
        }
    )
    evaluation_mode = "answer-only" if answer_only else "retrieval-and-answer"
    skill_sha256 = _file_sha256(_RAG_OPTIMIZATION_SKILL_PATH)
    runtime_contract_sha256 = _sha256_json(
        {
            str(path.relative_to(ROOT)): _file_sha256(path)
            for path in _RUNTIME_CONTRACT_PATHS
        }
    )
    promotion_authority: dict[str, object] = {}
    if answer_only:
        if evaluation_split == "held_out":
            if promotion_receipt_path is None or heldout_gate_path is None:
                raise ValueError(
                    "answer-only held_out requires promotion and one-shot gate"
                )
            promotion_authority = _read_json_object(promotion_receipt_path)
            held_out_authorization = _authorize_held_out(
                promotion=promotion_authority,
                gate=_read_json_object(heldout_gate_path),
                source_prepared_sha256=_file_sha256(prepared_path),
                source_answer_cases_sha256=_file_sha256(answer_cases_path),
                source_retrieval_report_sha256=_file_sha256(retrieval_report_path),
                retrieval_report_sha256=str(
                    retrieval_report.get("reportSha256") or ""
                ),
                retrieval_config_sha256=tuned_config_sha256,
                prompt_config_sha256=prompt_config_sha256,
                runtime_contract_sha256=runtime_contract_sha256,
            )
            claim = _claim_held_out_gate(
                heldout_gate_path,
                authorization=held_out_authorization,
            )
            held_out_authorization["claimSha256"] = str(
                claim.get("claimSha256") or ""
            )
            held_out_authorization["consumed"] = True
        answer_case_payload = _read_json_object(answer_cases_path)
        raw_answer_case_values = answer_case_payload.get("cases")
        if not isinstance(raw_answer_case_values, list):
            raise ValueError("--answer-cases must contain a cases array")
        raw_answer_cases = [
            dict(item)
            for item in raw_answer_case_values
            if isinstance(item, Mapping)
        ]
        evaluation_cases = select_agent_answer_cases(
            raw_answer_cases,
            split=evaluation_split,
            limit=agent_case_limit,
            seed=agent_seed,
            excluded_query_ids=set(development_exclusion["caseIds"]),
        )
    for index, case in enumerate(evaluation_cases, start=1):
        case["evaluationCaseId"] = f"case-{index:02d}"
    if answer_only:
        answer_case_manifest = _answer_case_manifest(
            prepared_cases=[
                dict(item) for item in prepared.get("cases") or [] if isinstance(item, Mapping)
            ],
            answer_cases=raw_answer_cases,
            selected_cases=evaluation_cases,
            documents=documents,
            benchmark_id=str(slice_manifest["benchmarkId"]),
            prepared_source_sha256=str(slice_manifest["sourceSha256"]),
            prepared_artifact_sha256=_file_sha256(prepared_path),
            evaluation_split=evaluation_split,
            chunking_config=dict(retrieval_report["chunking"]),
            answer_evidence_qrels=_read_json_object(
                answer_evidence_qrels_path
            ),
        )
    lane_prompt_sha256_by_lane = {
        lane: hashlib.sha256(
            _lane_prompt(
                lane=lane,
                run_id="",
                cases=evaluation_cases,
                retrieval_config=(
                    tuned_config if lane in {"tuned", "agentic"} else default_config
                ),
                evaluation_mode=evaluation_mode,
                evaluation_split=evaluation_split,
                agentic_supplemental_limit=agentic_supplemental_limit,
                prompt_profile=prompt_profile,
                candidate_prompt=candidate_prompt,
            ).encode("utf-8")
        ).hexdigest()
        for lane in LANES
    }

    embedding_environment = _embedding_environment_from_report(retrieval_report)
    provider_public: dict[str, object] = {}
    if not calibration_no_metal:
        try:
            _require_actual_metal_runtime()
        except Exception as exc:
            return _preflight_failure_report(
                started_at_ms=started_at_ms,
                prepared_path=prepared_path,
                answer_cases_path=answer_cases_path,
                retrieval_report_path=retrieval_report_path,
                slice_manifest=slice_manifest,
                evaluation_cases=evaluation_cases,
                evaluation_mode=(
                    "answer-only" if answer_only else "retrieval-and-answer"
                ),
                evaluation_split=evaluation_split,
                answer_case_manifest=answer_case_manifest,
                prompt_config_sha256=prompt_config_sha256,
                calibration_no_metal=calibration_no_metal,
                development_only=development_only,
                embedding=provider_public,
                failure=f"{type(exc).__name__}: {exc}",
                checkpoint=checkpoint_request_projection,
                scene_recipe=scene_recipe,
                candidate_prompt=candidate_prompt,
                judge_model=judge_model,
            )
    try:
        if calibration_no_metal:
            provider = HashingEmbeddingProvider(dimensions=768)
            reranker = _RankPreservingCalibrationReranker()
            reranker_identity = {
                "required": True,
                "accepted": False,
                "calibrationOnly": True,
                "reason": "no-Metal prompt calibration does not use the frozen Qwen3 reranker",
                "provider": reranker.provider,
                "fingerprint": reranker.fingerprint,
                "candidateDepth": int(tuned_config.get("rerankCandidateDepth") or 0),
                "finalDepth": int(tuned_config.get("rerankFinalDepth") or 10),
            }
        else:
            provider = embedding_provider_from_env(embedding_environment)
            provider_public = dict(embedding_provider_info(provider))
            reranker, reranker_identity = _frozen_reranker(
                tuned_config,
                model_path=reranker_model,
                model_revision=reranker_revision,
                cache_path=reranker_cache,
                instruction=rerank_instruction,
            )
        if not provider_public:
            provider_public = dict(embedding_provider_info(provider))
    except Exception as exc:
        return _preflight_failure_report(
            started_at_ms=started_at_ms,
            prepared_path=prepared_path,
            answer_cases_path=answer_cases_path,
            retrieval_report_path=retrieval_report_path,
            slice_manifest=slice_manifest,
            evaluation_cases=evaluation_cases,
            evaluation_mode=(
                "answer-only" if answer_only else "retrieval-and-answer"
            ),
            evaluation_split=evaluation_split,
            answer_case_manifest=answer_case_manifest,
            prompt_config_sha256=prompt_config_sha256,
            calibration_no_metal=calibration_no_metal,
            development_only=development_only,
            embedding=provider_public,
            failure=f"{type(exc).__name__}: {exc}",
            checkpoint=checkpoint_request_projection,
            scene_recipe=scene_recipe,
            candidate_prompt=candidate_prompt,
            judge_model=judge_model,
        )
    source_bytes = sum(len(str(item["text"]).encode("utf-8")) for item in documents)
    maximum_document_bytes = max(
        4 * 1024 * 1024,
        max(len(str(item["text"]).encode("utf-8")) for item in documents),
    )
    policy = RagBenchmarkSandboxPolicy(
        max_runs=1,
        max_bases_per_run=1,
        max_documents_per_run=len(documents),
        max_documents_per_call=min(256, len(documents)),
        max_document_bytes=maximum_document_bytes,
        max_total_source_bytes=max(maximum_document_bytes, source_bytes + 1024 * 1024),
        max_search_calls=max(100, (len(evaluation_cases) + 1) * 12),
        max_rebuilds=4,
    )

    def service_factory(
        root: Path,
        _policy: RagBenchmarkSandboxPolicy,
    ) -> KnowledgeLibraryService:
        config = KnowledgeLibraryConfig(root, max_source_bytes=policy.max_document_bytes)
        service = KnowledgeLibraryService(config, background_jobs=False)
        service.dense_index = dense_index_from_env(
            config.database_path,
            provider,
            embedding_environment,
        )
        return service

    sandbox = RagBenchmarkSandbox(
        run_root / "knowledge-runs",
        policy=policy,
        service_factory=service_factory,
        reranker=reranker,
    )
    gateway = RagBenchmarkAgentGateway(RagBenchmarkSandboxTool(sandbox))
    server = None
    service: AgentService | None = None
    owner = "ablation:" + _sha256_json(
        {"manifest": slice_manifest["sourceSha256"], "agentSeed": agent_seed}
    )[:24]
    run_id = ""
    cleanup_passed = False
    lane_records: list[dict[str, Any]] = []
    answer_judge: dict[str, object] = {}
    dense_acceptance: dict[str, object] = {}
    pi_runtime_identity: dict[str, object] = {}
    agent_config_identity: dict[str, object] = {}
    checkpoint_state: dict[str, object] | None = None
    checkpoint_initial_attempt_count = 0
    checkpoint_reusable_records: dict[str, dict[str, Any]] = {}
    checkpoint_reused_lanes: set[str] = set()
    checkpoint_fresh_lanes: set[str] = set()
    failure = ""
    execution_settled = True
    try:
        agent_config = run_root / "agent" / "config"
        _copy_openai_codex_agent_config(source_agent_config, agent_config)
        agent_config_identity = _pin_evaluation_agent_config(
            agent_config,
            evaluation_model=evaluation_model,
        )
        runtime_config = _isolated_runtime_config(
            run_root,
            agent_config=agent_config,
            runtime_payload=pi_runtime_payload,
            evaluation_model=evaluation_model,
        )
        pi_runtime_identity = _public_pi_runtime_identity(runtime_config)
        server = _start_rag_benchmark_gateway(
            gateway,
            spool_dir=run_root / "agent" / "tool-spool",
        )
        service = AgentService(
            db_path=run_root / "agent.sqlite",
            runtime_config=runtime_config,
            project="rag-agent-ablation",
            tool_gateway_url=server.tool_gateway_url,
            tool_gateway_token=server.token,
            wake_scheduler_enabled=False,
            background_job_execution_owner=False,
            configuration_defaults=_evaluation_configuration_defaults(
                evaluation_model=evaluation_model
            ),
        )
        service = _execution_service(service, cancelled=cancelled, on_session=on_session, on_turn=on_turn)
        agent_config_identity["modelRouting"] = _evaluation_configuration_identity(
            service,
            evaluation_model=evaluation_model,
        )
        agent_config_identity["identitySha256"] = _sha256_json(
            agent_config_identity
        )
        if evaluation_split == "held_out" and answer_only:
            model_route_identity = agent_config_identity.get("modelRouting")
            _validate_held_out_runtime_authority(
                promotion_authority,
                pi_runtime_identity_sha256=str(
                    pi_runtime_identity.get("identitySha256") or ""
                ),
                model_route_identity_sha256=(
                    str(model_route_identity.get("identitySha256") or "")
                    if isinstance(model_route_identity, Mapping)
                    else ""
                ),
            )
        if checkpoint_path is not None:
            selected_case_set_sha256 = (
                str(answer_case_manifest.get("selectedCaseSetSha256") or "")
                if answer_only
                else _sha256_json(
                    [str(item.get("queryId") or "") for item in evaluation_cases]
                )
            )
            checkpoint_fingerprint = _lane_checkpoint_fingerprint(
                source_prepared_sha256=_file_sha256(prepared_path),
                source_answer_cases_sha256=(
                    _file_sha256(answer_cases_path)
                    if answer_cases_path is not None
                    else ""
                ),
                source_retrieval_report_sha256=_file_sha256(retrieval_report_path),
                evaluation_mode=(
                    "answer-only" if answer_only else "retrieval-and-answer"
                ),
                evaluation_split=evaluation_split,
                case_ids_sha256=_sha256_json(
                    [str(item.get("queryId") or "") for item in evaluation_cases]
                ),
                case_set_sha256=selected_case_set_sha256,
                answer_case_manifest_sha256=str(
                    answer_case_manifest.get("manifestSha256") or ""
                ),
                prompt_config_sha256=prompt_config_sha256,
                lane_prompt_sha256_by_lane=lane_prompt_sha256_by_lane,
                skill_sha256=skill_sha256,
                runtime_contract_sha256=runtime_contract_sha256,
                default_retrieval_config_sha256=default_config_sha256,
                tuned_retrieval_config_sha256=tuned_config_sha256,
                model_route_identity_sha256=str(
                    (
                        agent_config_identity.get("modelRouting") or {}
                    ).get("identitySha256")
                    or ""
                ),
                pi_runtime_identity_sha256=str(
                    pi_runtime_identity.get("identitySha256") or ""
                ),
                maximum_attempts_per_lane=lane_attempts,
            )
            checkpoint_state = _open_lane_checkpoint(
                checkpoint_path,
                fingerprint=checkpoint_fingerprint,
                resume=resume_checkpoint,
            )
            checkpoint_initial_attempt_count = len(
                _lane_checkpoint_attempt_records(checkpoint_state)
            )
            if resume_checkpoint:
                def recover_interrupted_session(
                    locator: Mapping[str, object],
                ) -> Mapping[str, object]:
                    session_id = str(locator.get("sessionId") or "")
                    turn_id = str(locator.get("turnId") or "")
                    return _recover_private_evaluation_session(
                        locator,
                        allowed_private_root=run_root.parent,
                        expected_session_sha256=hashlib.sha256(
                            session_id.encode("utf-8")
                        ).hexdigest(),
                        expected_turn_sha256=hashlib.sha256(
                            turn_id.encode("utf-8")
                        ).hexdigest()
                        if turn_id
                        else "",
                    )

                def cleanup_interrupted_sandbox(
                    locator: Mapping[str, object],
                ) -> Mapping[str, object]:
                    orphan_run_root = Path(str(locator["runRoot"])).resolve(
                        strict=True
                    )
                    orphan_root = Path(str(locator["sandboxRoot"])).resolve(
                        strict=False
                    )
                    if (
                        orphan_run_root.parent != run_root.parent
                        or not orphan_run_root.name.startswith("run-")
                        or orphan_root != orphan_run_root / "knowledge-runs"
                        or not orphan_root.is_dir()
                        or orphan_root.is_symlink()
                    ):
                        raise RuntimeError(
                            "interrupted benchmark sandbox root cannot be reopened"
                        )
                    if orphan_root == sandbox.root:
                        return sandbox.cleanup(
                            locator["sandboxOwnerId"],
                            locator["sandboxRunId"],
                            confirm_text=DELETE_CONFIRMATION,
                        )
                    orphan_sandbox = RagBenchmarkSandbox(
                        orphan_root,
                        policy=policy,
                        service_factory=service_factory,
                        reranker=reranker,
                    )
                    try:
                        return orphan_sandbox.cleanup(
                            locator["sandboxOwnerId"],
                            locator["sandboxRunId"],
                            confirm_text=DELETE_CONFIRMATION,
                        )
                    finally:
                        orphan_sandbox.close()

                checkpoint_state, orphan_recovery = (
                    _recover_lane_checkpoint_orphans(
                        checkpoint_path,
                        checkpoint=checkpoint_state,
                        session_recover=recover_interrupted_session,
                        sandbox_cleanup=cleanup_interrupted_sandbox,
                    )
                )
                if orphan_recovery["failClosed"] is True:
                    raise RuntimeError(
                        "checkpoint orphan recovery is blocked; no new benchmark "
                        "run was created"
                    )
            checkpoint_reusable_records = _lane_checkpoint_reusable_records(
                checkpoint_state
            )
        gateway.session_loader = service.sessions.get
        gateway.base_gateway = ControlToolGateway(
            sessions=service.sessions,
            management=object(),
            core=object(),
            project=service.project,
            background_jobs=service.background_jobs,
            delegation=service.delegation,
            configuration_store=service.configuration_store,
            # Skills are resolved by the managed Pi resource loader.  The
            # current AgentService no longer owns a parallel governed Skill
            # registry at the HTTP Tool gateway boundary.
            governed_skills=None,
            work_documents=service.work_documents,
        )
        gateway.delegated_parent_loader = lambda child_session_id: (
            str(run.get("parentSessionId") or "")
            if isinstance(
                run := service.delegation.store.run_for_child_session(child_session_id),
                Mapping,
            )
            else None
        )
        service.bind_tool_manifest_provider(gateway.runtime_manifests)

        _execution_check(service)
        run = sandbox.create_run(owner, label=slice_manifest["benchmarkId"])
        run_id = str(run["runId"])
        base = sandbox.create_base(
            owner,
            run_id,
            alias="benchmark",
            name="CRUD-RAG Agent ablation",
            description="Local-only public held-out Agent ablation",
            chunking_config=dict(retrieval_report["chunking"]),
            retrieval_config=_knowledge_base_retrieval_config(default_config),
        )
        for offset in range(0, len(documents), policy.max_documents_per_call):
            _execution_check(service)
            batch = documents[offset : offset + policy.max_documents_per_call]
            sandbox.import_documents(
                owner,
                run_id,
                base_alias="benchmark",
                documents=[
                    {
                        "externalId": item["documentId"],
                        "name": f"{item['documentId']}.txt",
                        "text": item["text"],
                        "mimeType": "text/plain",
                    }
                    for item in batch
                ],
            )
            _progress("import", imported=min(offset + len(batch), len(documents)), total=len(documents))
        import_status = sandbox.status(owner, run_id)
        if calibration_no_metal:
            try:
                dense_acceptance = _require_semantic_dense_runtime(
                    provider=provider_public,
                    status=import_status,
                    requested_backend=str(
                        embedding_environment["RAG_IME_KNOWLEDGE_DENSE_BACKEND"]
                    ),
                )
            except ValueError as exc:
                dense_acceptance = {
                    "accepted": False,
                    "calibrationOnly": True,
                    "reason": str(exc),
                }
        else:
            dense_acceptance = _require_semantic_dense_runtime(
                provider=provider_public,
                status=import_status,
                requested_backend=str(
                    embedding_environment["RAG_IME_KNOWLEDGE_DENSE_BACKEND"]
                ),
            )

        active_config_sha256 = default_config_sha256
        for lane in LANES:
            _execution_check(service)
            target_config = tuned_config if lane in {"tuned", "agentic"} else default_config
            target_hash = tuned_config_sha256 if lane in {"tuned", "agentic"} else default_config_sha256
            if active_config_sha256 != target_hash:
                base = sandbox.configure_base(
                    owner,
                    run_id,
                    base_alias="benchmark",
                    expected_revision=base["configRevision"],
                    retrieval_config=_knowledge_base_retrieval_config(target_config),
                )
                active_config_sha256 = target_hash
            reusable_lane_record = checkpoint_reusable_records.get(lane)
            if reusable_lane_record is not None:
                lane_records.append(reusable_lane_record)
                checkpoint_reused_lanes.add(lane)
                _progress(
                    "lane_reused",
                    lane=lane,
                    terminal=reusable_lane_record["terminalEvent"],
                    checkpointFingerprintSha256=(
                        str(checkpoint_state.get("fingerprintSha256") or "")
                        if checkpoint_state is not None
                        else ""
                    ),
                )
                continue
            prior_attempt_count = (
                len(_lane_checkpoint_attempt_history(checkpoint_state, lane))
                if checkpoint_state is not None
                else 0
            )
            if prior_attempt_count >= lane_attempts:
                raise RuntimeError(
                    f"checkpoint lane {lane} exhausted its attempt budget"
                )

            def persist_attempt_started(attempt_number: int, *, lane=lane) -> None:
                nonlocal checkpoint_state
                if checkpoint_path is None or checkpoint_state is None:
                    return
                checkpoint_state = _append_lane_checkpoint_attempt_started(
                    checkpoint_path,
                    checkpoint=checkpoint_state,
                    lane=lane,
                    attempt=attempt_number,
                )

            def persist_attempt(
                attempt_number: int,
                attempt_record: Mapping[str, object],
                lane=lane,
            ) -> None:
                nonlocal checkpoint_state
                if checkpoint_path is None or checkpoint_state is None:
                    return
                checkpoint_state = _append_lane_checkpoint_attempt(
                    checkpoint_path,
                    checkpoint=checkpoint_state,
                    lane=lane,
                    attempt=attempt_number,
                    lane_record=attempt_record,
                )

            def persist_attempt_binding(
                attempt_number: int,
                session_id: str,
                turn_id: str,
                lane=lane,
            ) -> None:
                nonlocal checkpoint_state
                if checkpoint_path is None or checkpoint_state is None:
                    return
                checkpoint_state = _append_lane_checkpoint_attempt_binding(
                    checkpoint_path,
                    checkpoint=checkpoint_state,
                    lane=lane,
                    attempt=attempt_number,
                    session_id=session_id,
                    turn_id=turn_id,
                    recovery_locator={
                        "schemaVersion": "rag-ime.rag-agent-orphan-locator.v1",
                        "runRoot": str(run_root.resolve(strict=False)),
                        "agentDbPath": str(
                            (run_root / "agent.sqlite").resolve(strict=False)
                        ),
                        "sessionId": session_id,
                        "turnId": turn_id,
                        "sandboxRoot": str(sandbox.root),
                        "sandboxOwnerId": owner,
                        "sandboxRunId": run_id,
                    },
                )

            lane_record = _run_lane(
                service,
                gateway=gateway,
                tool_transport=server,
                owner=owner,
                run_id=run_id,
                lane=lane,
                cases=evaluation_cases,
                retrieval_config=target_config,
                retrieval_config_sha256=target_hash,
                timeout_seconds=timeout_seconds,
                lane_attempts=lane_attempts,
                answer_only=answer_only,
                evaluation_mode=(
                    "answer-only" if answer_only else "retrieval-and-answer"
                ),
                evaluation_split=evaluation_split,
                agentic_supplemental_limit=agentic_supplemental_limit,
                prompt_profile=prompt_profile,
                evaluation_model=evaluation_model,
                candidate_prompt=candidate_prompt,
                attempt_start=prior_attempt_count + 1,
                attempt_start_observer=(
                    persist_attempt_started if checkpoint_path is not None else None
                ),
                attempt_binding_observer=(
                    persist_attempt_binding if checkpoint_path is not None else None
                ),
                attempt_observer=persist_attempt if checkpoint_path is not None else None,
            )
            checkpoint_fresh_lanes.add(lane)
            if checkpoint_state is not None:
                lane_record["runtimeAttempts"] = _lane_checkpoint_attempt_history(
                    checkpoint_state, lane
                )
                lane_record["runtimeRetryCount"] = max(
                    0, len(lane_record["runtimeAttempts"]) - 1
                )
            lane_records.append(lane_record)
            _progress(
                "lane",
                lane=lane,
                terminal=lane_record["terminalEvent"],
                searchCalls=lane_record["score"]["searchCallCount"],
                agentMetrics=lane_record["score"]["agentMetrics"],
            )
        judge_cases = [
            item for item in evaluation_cases if not bool(item.get("abstentionExpected"))
        ]
        answer_judge = _run_answer_judge(
            service,
            cases=judge_cases,
            documents=documents,
            lane_records=lane_records,
            chunking_config=dict(retrieval_report["chunking"]),
            timeout_seconds=timeout_seconds,
            maximum_attempts=2,
            evaluation_model=judge_model,
        )
        if answer_judge.get("accepted") is not True:
            raise RuntimeError(
                "answer judge rejected its output: "
                + str(answer_judge.get("failure") or "unknown failure")
            )
        if answer_only:
            _apply_answer_only_judgments(
                lane_records,
                cases=evaluation_cases,
                answer_judge=answer_judge,
                answer_case_manifest=answer_case_manifest,
            )
        else:
            _apply_answer_judgments(lane_records, answer_judge["judgments"])
        for lane_record in lane_records:
            lane_record.pop("_assistantText", None)
        _progress(
            "answer_judge",
            accepted=answer_judge.get("accepted"),
            correctnessByLane=answer_judge.get("correctnessByLane"),
        )
        _execution_check(service)
    except RagEvaluationCancelled as exc:
        failure = f"RagEvaluationCancelled: {exc}"
        execution_settled = not exc.interrupted
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if run_id:
            try:
                cleanup = sandbox.cleanup(owner, run_id, confirm_text=DELETE_CONFIRMATION)
            except Exception:
                cleanup_passed = False
            else:
                cleanup_passed = cleanup.get("deleted") is True
        # A failed close cannot skip another owner's cleanup or turn a Stop
        # into a falsely settled cancellation. Keep an inspectable receipt.
        for execution_owner in (service, server, sandbox):
            if execution_owner is not None:
                try:
                    execution_owner.close()
                except Exception as exc:
                    execution_settled = False
                    cleanup_passed = False
                    failure = failure or f"{type(exc).__name__}: execution cleanup failed"

    if cancelled is not None and cancelled() and not failure.startswith("RagEvaluationCancelled:"):
        failure = "RagEvaluationCancelled: RAG evaluation cancelled"
    completed_at_ms = int(time.time() * 1_000)
    if failure or len(lane_records) != 4:
        completed_lane_evidence = []
        for lane_record in lane_records:
            projected = dict(lane_record)
            projected.pop("_assistantText", None)
            completed_lane_evidence.append(projected)
        report = {
            "schemaVersion": SCHEMA_VERSION,
            "status": "interrupted" if not execution_settled else ("cancelled" if failure.startswith("RagEvaluationCancelled:") else "failed"),
            "executionSettled": execution_settled,
            "passed": False,
            "scoreEligible": False,
            "formalAcceptanceEligible": False,
            "localOnly": True,
            "uploaded": False,
            "startedAtMs": started_at_ms,
            "completedAtMs": completed_at_ms,
            "elapsedMs": completed_at_ms - started_at_ms,
            "failure": failure or "four Agent lanes did not complete",
            "sourcePreparedSha256": _file_sha256(prepared_path),
            "sourceAnswerCasesSha256": (
                _file_sha256(answer_cases_path) if answer_cases_path is not None else ""
            ),
            "sourceRetrievalReportSha256": _file_sha256(retrieval_report_path),
            "dataset": slice_manifest,
            "evaluation": {
                "mode": "answer-only" if answer_only else "retrieval-and-answer",
                "split": evaluation_split,
                "caseCount": len(evaluation_cases),
                "caseIds": [str(item.get("queryId") or "") for item in evaluation_cases],
                "caseSetSha256": _sha256_json(
                    [_answer_case_identity(item) for item in evaluation_cases]
                )
                if answer_only
                else _sha256_json(
                    [str(item.get("queryId") or "") for item in evaluation_cases]
                ),
                "answerCaseManifestSha256": str(
                    answer_case_manifest.get("manifestSha256") or ""
                ),
                "answerCaseSetSha256": str(
                    answer_case_manifest.get("answerCaseSetSha256") or ""
                ),
                "promptConfigSha256": prompt_config_sha256,
                "formalAcceptanceEligible": False,
            },
            "answerCaseManifest": answer_case_manifest,
            "heldOutAuthorization": held_out_authorization,
            "completedLanes": [item["lane"] for item in lane_records],
            "completedLaneEvidence": completed_lane_evidence,
            "answerJudge": answer_judge,
            "calibration": {
                "enabled": calibration_no_metal,
                "formalAcceptanceEligible": False,
                "passed": False if calibration_no_metal else None,
                "purpose": (
                    "development-only Agent answer coverage audit under a no-Metal sandbox"
                    if calibration_no_metal
                    else ""
                ),
            },
            "development": {
                "enabled": development_only,
                "formalAcceptanceEligible": False,
                "underlyingHardGatesPassed": False if development_only else None,
                "purpose": (
                    "real-model replay of previously observed cases for prompt and orchestration calibration"
                    if development_only
                    else ""
                ),
            },
            "piRuntime": pi_runtime_identity,
            "agentConfig": agent_config_identity,
            "cleanupPassed": cleanup_passed,
        }
        checkpoint_projection = _lane_checkpoint_report_projection(
            checkpoint_state,
            resume_requested=resume_checkpoint,
            initial_attempt_count=checkpoint_initial_attempt_count,
            reused_lanes=checkpoint_reused_lanes,
            fresh_lanes=checkpoint_fresh_lanes,
            checkpoint_requested=checkpoint_path is not None,
        )
        return _finalize_public_report(report, checkpoint=checkpoint_projection, scene_recipe=scene_recipe,
                                       candidate_prompt=candidate_prompt, judge_model=judge_model)

    case_ids = [str(item["queryId"]) for item in evaluation_cases]
    case_aliases = [str(item["evaluationCaseId"]) for item in evaluation_cases]
    permissions = {
        "executionMode": "read_only",
        "toolProfileVersion": "subagent-readonly-v1",
        "allowedOperations": ["search", "status"],
        "allowedAuxiliaryTools": ["agents"],
        "workspaceRoots": [],
    }
    conditions = {
        **({"sceneRecipe": scene_recipe} if scene_recipe is not None else {}),
        "model": f"{_EVALUATION_PROVIDER}/{evaluation_model}",
        "thinking": _EVALUATION_THINKING,
        "laneTimeoutSeconds": float(timeout_seconds),
        "datasetSplitSha256": _sha256_json(case_ids),
        "permissionSha256": _sha256_json(permissions),
        "denominator": len(case_ids),
        "benchmarkId": slice_manifest["benchmarkId"],
        "evaluationMode": "answer-only" if answer_only else "retrieval-and-answer",
        "evaluationSplit": evaluation_split,
        "promptConfigSha256": prompt_config_sha256,
        "lanePromptSha256ByLane": lane_prompt_sha256_by_lane,
        "answerCaseManifestSha256": str(
            answer_case_manifest.get("manifestSha256") or ""
        ),
        "answerCaseSetSha256": str(
            answer_case_manifest.get("answerCaseSetSha256") or ""
        ),
        "selectedAnswerCaseSetSha256": str(
            answer_case_manifest.get("selectedCaseSetSha256") or ""
        ),
        "caseIdsSha256": _sha256_json(case_ids),
        "promptContractVersion": _PROMPT_CONTRACT_VERSION,
        "promptProfile": prompt_profile,
        "promptCalibrationLabel": (
            "post-validation-calibrated"
            if prompt_profile in _POST_VALIDATION_PROMPT_PROFILES
            else "none"
        ),
        "unbiasedPromotionClaimAllowed": (
            False if candidate_prompt is not None or prompt_profile in _POST_VALIDATION_PROMPT_PROFILES else None
        ),
        "agenticSupplementalLimit": agentic_supplemental_limit,
        "skillName": "rag-retrieval-optimization",
        "skillSha256": skill_sha256,
        "piRuntime": pi_runtime_identity,
        "agentConfig": agent_config_identity,
        "toolTransport": server.transport if server is not None else "",
        "calibrationProfile": (
            "no-metal-hashing-rank-preserving-v1"
            if calibration_no_metal
            else (
                "development-replay-real-model-v1"
                if development_only
                else "none"
            )
        ),
        "citationReferencePolicy": {
            "scope": "benchmark-run-and-evaluation-case",
            "format": "K{positiveInteger}",
            "assignment": "first-observed-source-order",
            "resolution": "exact-only",
            "unknownReference": "hard-fail",
        },
        "runtimeContractSha256": runtime_contract_sha256,
        "runtimeRetryPolicy": {
            "maximumAttemptsPerLane": lane_attempts,
            "retryableCategory": "provider_transient_before_tool",
            "metricBasedSelection": False,
        },
        "agenticRetrievalPolicy": {
            "childCount": 1,
            "childWaveCount": 1,
            "maxParallelChildren": 1,
            "childCaseAssignment": "single-no-tool-selective-query-critic-v2",
            "childTemplate": "reviewer@1",
            "childThinking": "low",
            "childBudget": dict(_AGENTIC_CRITIC_BUDGET),
            "childTopK": 0,
            "parentFirstPassTopK": _AGENTIC_PARENT_SEARCH_TOP_K,
            "parentSupplementalTopK": _SUPPLEMENTAL_SEARCH_TOP_K,
            "maxSearchesPerCase": _AGENTIC_MAX_SEARCHES_PER_CASE,
            "maxSupplementalPerCase": _AGENTIC_MAX_SUPPLEMENTAL_PER_CASE,
            "maxSupplementalTotal": agentic_supplemental_limit,
            "sequence": "parent-exact-search_then_critic_then_selective-atomic-parent-search",
            "synthesisCorrection": {
                "enabled": False,
                "maximumTurns": 0,
                "additionalSearches": 0,
                "trigger": "reviewer-already-performs-pre-synthesis-coverage-audit",
                "metricBasedSelection": False,
                "qrelAccess": False,
            },
        },
        "answerCoverageAuditPolicy": {
            "enabledLanes": ["skill", "tuned"],
            "maximumTurns": 1,
            "additionalSearches": 0,
            "toolCallsAllowed": 0,
            "sameSession": True,
            "labelBlind": True,
            "referenceAnswerAccess": False,
            "qrelAccess": False,
            "metricFeedbackAccess": False,
            "returnsFullProtocol": True,
        },
        "laneOutputProtocolRepairPolicy": {
            "maximumTurns": 1,
            "additionalSearches": 0,
            "toolCallsAllowed": 0,
            "sameSession": True,
            "trigger": "missing-duplicate-or-invalid-case-envelope-only",
            "referenceAnswerAccess": False,
            "qrelAccess": False,
            "judgeFeedbackAccess": False,
            "preserveValidExistingCaseValues": True,
        },
        "answerEvaluationPolicy": {
            "primaryTaskMetric": "answerJudgeCorrectnessRate",
            "rawCharacterMetricsDiagnosticOnly": True,
            "judgeModel": f"{_EVALUATION_PROVIDER}/{judge_model}",
            "judgeThinking": _EVALUATION_THINKING,
            "judgeContractVersion": _ANSWER_JUDGE_CONTRACT_VERSION,
            "anonymousCandidates": True,
            "referenceAnswerAccessAfterGeneration": True,
            "retrievalQrelAccess": False,
            "candidateCitedEvidenceAccessAfterGeneration": True,
            "judgeFeedbackToAgent": False,
            "metricBasedRetry": False,
            "formatRepair": {
                "maximumTurns": 1,
                "trigger": "schema-invalid-only",
                "judgmentFeedback": False,
            },
            "runtimeRetryPolicy": {
                "maximumAttempts": 2,
                "retryableTerminal": "turn_failed_before_judgment",
                "metricBasedSelection": False,
                "laneRerun": False,
            },
            "evidenceRetention": {
                "scope": "local-only-public-benchmark",
                "question": True,
                "referenceAnswer": True,
                "generatedAnswer": True,
                "candidateCitedEvidence": True,
                "candidateCitedEvidenceGranularity": "retrieved-chunk",
                "uploaded": False,
            },
        },
    }
    reranker_evidence = _reranker_run_evidence(
        reranker,
        identity=reranker_identity,
    )
    knowledge_lanes: list[dict[str, object]] = []
    agent_lanes: list[dict[str, object]] = []
    for item in lane_records:
        score = item["score"]
        lane_config = (
            tuned_config
            if item["lane"] in {"tuned", "agentic"}
            else default_config
        )
        hard_gates = {
            "splitIntegrity": True,
            "answerCaseBinding": (
                answer_case_manifest.get("suiteBindingPassed") is True
                and answer_case_manifest.get("corpusBindingPassed") is True
                and answer_case_manifest.get("highLevelEvidenceAvailabilityPassed") is True
                if answer_only
                else True
            ),
            "runtimePinned": (
                pi_runtime_identity.get("sourceAccess")
                == "explicit-verified-payload-v1"
                if not calibration_no_metal
                else True
            ),
            "scopeBoundary": item["scopeBoundary"],
            "citationResolution": (
                score["hardEvidence"]["citationResolution"]
                and (
                    score["hardEvidence"].get("factCitationCoverage") is True
                    if answer_only
                    else True
                )
            ),
            "abstention": score["hardEvidence"]["abstention"],
            "crossSystemLeakage": item["crossSystemLeakage"],
            "terminalCompletion": item["terminalEvent"] == "turn_completed",
            "toolContract": item["toolContract"],
            "agenticPolicy": (
                score["hardEvidence"]["agenticLoopObserved"]
                and (
                    bool(item["features"].get("subagentPolicyPassed"))
                    and bool(item["features"].get("criticContractPassed"))
                    and bool(item["features"].get("parentQueryPolicyPassed"))
                    and bool(item["features"].get("coverageAuditPassed"))
                    if item["lane"] == "agentic"
                    else not bool(item["features"].get("subagents"))
                )
            ),
            "semanticIndex": dense_acceptance.get("accepted") is True,
            "independentReranker": (
                reranker_evidence["accepted"]
                if lane_config.get("rerankEnabled") is True
                else True
            ),
            "answerJudge": answer_judge.get("accepted") is True,
            "cleanup": cleanup_passed,
        }
        common = {
            "lane": item["lane"],
            "features": item["features"],
            "conditions": conditions,
            "retrievalConfigSha256": item["retrievalConfigSha256"],
            "costs": item["costs"],
            "hardGates": hard_gates,
        }
        if not answer_only:
            knowledge_lanes.append({**common, "metrics": flat_retrieval_metrics(score)})
        agent_lanes.append({**common, "metrics": dict(score["agentMetrics"])})
        item["hardGates"] = hard_gates
    knowledge_ablation = (
        {
            "accepted": True,
            "notApplicable": True,
            "reason": "answer-only cases have no retrieval qrels denominator",
        }
        if answer_only
        else build_ablation_report(
            metric_namespace="knowledge",
            lanes=knowledge_lanes,
            required_hard_gates=REQUIRED_HARD_GATES,
        )
    )
    agent_ablation = build_ablation_report(
        metric_namespace="agent",
        lanes=agent_lanes,
        required_hard_gates=REQUIRED_HARD_GATES,
    )
    candidate_decision = _build_candidate_decision(agent_lanes)
    underlying_accepted = bool(knowledge_ablation["accepted"]) and bool(
        candidate_decision["accepted"]
    )
    formal_acceptance_eligible = (
        evaluation_split == "held_out"
        and held_out_authorization.get("authorized") is True
        and not calibration_no_metal
        and not development_only
    )
    correctness_by_lane = answer_judge.get("correctnessByLane")
    correctness_by_lane = (
        correctness_by_lane if isinstance(correctness_by_lane, Mapping) else {}
    )
    calibration_passed = (
        calibration_no_metal
        and answer_judge.get("accepted") is True
        and float(correctness_by_lane.get("agentic") or 0.0) == 1.0
    )
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "passed": underlying_accepted and not calibration_no_metal and not development_only,
        "formalAcceptanceEligible": formal_acceptance_eligible,
        "formalAcceptancePassed": underlying_accepted and formal_acceptance_eligible,
        "acceptanceStatus": (
            "formal-held-out-accepted"
            if underlying_accepted and formal_acceptance_eligible
            else (
                "validation-accepted-not-formal"
                if underlying_accepted and evaluation_split == "validation"
                else "rejected"
            )
        ),
        "localOnly": True,
        "uploaded": False,
        "providerDisclosure": "public CRUD-RAG-derived questions, documents, and Tool results only",
        "startedAtMs": started_at_ms,
        "completedAtMs": completed_at_ms,
        "elapsedMs": completed_at_ms - started_at_ms,
        "sourcePreparedSha256": _file_sha256(prepared_path),
        "sourceAnswerCasesSha256": (
            _file_sha256(answer_cases_path) if answer_cases_path is not None else ""
        ),
        "sourceRetrievalReportSha256": _file_sha256(retrieval_report_path),
        "dataset": slice_manifest,
        "answerCaseManifest": answer_case_manifest,
        "heldOutAuthorization": held_out_authorization,
        "evaluation": {
            "mode": "answer-only" if answer_only else "retrieval-and-answer",
            "split": evaluation_split,
            "caseCount": len(case_ids),
            "caseIds": case_ids,
            "caseAliases": case_aliases,
            "caseIdsSha256": _sha256_json(case_ids),
            "caseSetSha256": (
                str(answer_case_manifest.get("selectedCaseSetSha256") or "")
                if answer_only
                else _sha256_json(case_ids)
            ),
            "answerCaseManifestSha256": str(
                answer_case_manifest.get("manifestSha256") or ""
            ),
            "answerCaseSetSha256": str(
                answer_case_manifest.get("answerCaseSetSha256") or ""
            ),
            "promptConfigSha256": prompt_config_sha256,
            "lanePromptSha256ByLane": lane_prompt_sha256_by_lane,
            "formalAcceptanceEligible": formal_acceptance_eligible,
            "selectionSeed": agent_seed,
            "heldOutLabelsInPrompt": False,
            "safetyCaseId": SAFETY_CASE_ID,
            "developmentExclusion": development_exclusion,
        },
        "embedding": {
            **provider_public,
            "model": Path(str(provider_public.get("model") or "")).name,
            "acceptance": dense_acceptance,
        },
        "reranker": reranker_evidence,
        "defaultRetrievalConfig": default_config,
        "defaultRetrievalConfigSha256": default_config_sha256,
        "tunedRetrievalConfig": tuned_config,
        "tunedRetrievalConfigSha256": tuned_config_sha256,
        "conditions": conditions,
        "piRuntime": pi_runtime_identity,
        "agentConfig": agent_config_identity,
        "answerJudge": answer_judge,
        "lanes": lane_records,
        "knowledgeAblation": knowledge_ablation,
        "agentAblation": agent_ablation,
        "candidateDecision": candidate_decision,
        "cleanupPassed": cleanup_passed,
        "calibration": {
            "enabled": calibration_no_metal,
            "formalAcceptanceEligible": not calibration_no_metal,
            "passed": calibration_passed if calibration_no_metal else None,
            "purpose": (
                "development-only Agent answer coverage audit under a no-Metal sandbox"
                if calibration_no_metal
                else ""
            ),
        },
        "development": {
            "enabled": development_only,
            "formalAcceptanceEligible": not development_only,
            "underlyingHardGatesPassed": underlying_accepted if development_only else None,
            "purpose": (
                "real-model replay of previously observed cases for prompt and orchestration calibration"
                if development_only
                else ""
            ),
        },
        "failure": (
            "calibration-only profile is not eligible for formal acceptance"
            if calibration_no_metal
            else (
                "development-only profile is not eligible for formal acceptance"
                if development_only
                else (
                    "validation evidence is not formal held-out acceptance"
                    if evaluation_split == "validation"
                    else ""
                )
            )
        ),
    }
    checkpoint_projection = _lane_checkpoint_report_projection(
        checkpoint_state,
        resume_requested=resume_checkpoint,
        initial_attempt_count=checkpoint_initial_attempt_count,
        reused_lanes=checkpoint_reused_lanes,
        fresh_lanes=checkpoint_fresh_lanes,
        checkpoint_requested=checkpoint_path is not None,
    )
    return _finalize_public_report(report, checkpoint=checkpoint_projection, scene_recipe=scene_recipe,
                                   candidate_prompt=candidate_prompt, judge_model=judge_model)


def _build_candidate_decision(
    lanes: list[Mapping[str, object]],
) -> dict[str, object]:
    """Decide whether the optimized lane is keepable without rewarding a bad baseline.

    A losing lane may fail an outcome-quality gate: that is the measured gap the
    candidate is intended to repair.  It may not fail comparison integrity
    (terminal delivery, Tool/runtime contracts, split identity, cleanup, and the
    shared Judge/index contracts), because then its measurements are not a valid
    baseline.  The optimized Agentic lane must pass every required gate.
    """

    by_lane = {
        str(item.get("lane") or ""): item
        for item in lanes
        if isinstance(item, Mapping)
    }
    if set(by_lane) != set(LANES):
        raise ValueError("candidate decision requires baseline, skill, tuned, and agentic lanes")

    failed: list[str] = []
    losing_outcome_failures: list[str] = []
    for lane_name in LANES:
        lane = by_lane[lane_name]
        hard_gates = lane.get("hardGates")
        if not isinstance(hard_gates, Mapping):
            raise ValueError(f"{lane_name} candidate decision is missing hard gates")
        for gate in REQUIRED_HARD_GATES:
            if gate not in hard_gates or not isinstance(hard_gates.get(gate), bool):
                raise ValueError(f"{lane_name} candidate decision has invalid {gate} gate")
        for gate in _COMPARISON_INTEGRITY_GATES:
            if hard_gates.get(gate) is not True:
                failed.append(f"{lane_name}:{gate}")
        if lane_name == "agentic":
            for gate in _CANDIDATE_OUTCOME_GATES:
                if hard_gates.get(gate) is not True:
                    failed.append(f"{lane_name}:{gate}")
        else:
            for gate in _CANDIDATE_OUTCOME_GATES:
                if hard_gates.get(gate) is not True:
                    losing_outcome_failures.append(f"{lane_name}:{gate}")

    failed = list(dict.fromkeys(failed))
    accepted = not failed
    return {
        "schemaVersion": "rag-ime.rag-agent-candidate-decision.v1",
        "candidateLane": "agentic",
        "accepted": accepted,
        "decision": "keep" if accepted else "reject",
        "failedHardGates": failed,
        "comparisonIntegrityGates": list(_COMPARISON_INTEGRITY_GATES),
        "candidateOutcomeGates": list(_CANDIDATE_OUTCOME_GATES),
        "losingLaneOutcomeFailures": losing_outcome_failures,
        "decisionScope": "quality_and_contract_only",
        "costDecisionRole": "downstream_cross_run_selection",
        "latencyDecisionRole": "diagnostic_only",
    }


def _preflight_failure_report(
    *,
    started_at_ms: int,
    prepared_path: Path,
    answer_cases_path: Path | None,
    retrieval_report_path: Path,
    slice_manifest: Mapping[str, object],
    evaluation_cases: list[Mapping[str, object]],
    evaluation_mode: str,
    evaluation_split: str,
    answer_case_manifest: Mapping[str, object],
    prompt_config_sha256: str,
    calibration_no_metal: bool,
    development_only: bool,
    embedding: Mapping[str, object],
    failure: str,
    checkpoint: Mapping[str, object] | None = None,
    scene_recipe: Mapping[str, object] | None = None,
    candidate_prompt: CandidatePrompt | None = None,
    judge_model: str | None = None,
) -> dict[str, object]:
    """Persist a non-score receipt when semantic setup fails before sandbox allocation."""

    completed_at_ms = int(time.time() * 1_000)
    case_ids = [str(item.get("queryId") or "") for item in evaluation_cases]
    case_aliases = [str(item.get("evaluationCaseId") or "") for item in evaluation_cases]
    report: dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "passed": False,
        "scoreEligible": False,
        "formalAcceptanceEligible": False,
        "localOnly": True,
        "uploaded": False,
        "providerDisclosure": "public CRUD-RAG-derived questions and redacted runtime receipts only",
        "startedAtMs": started_at_ms,
        "completedAtMs": completed_at_ms,
        "elapsedMs": max(0, completed_at_ms - started_at_ms),
        "sourcePreparedSha256": _file_sha256(prepared_path),
        "sourceAnswerCasesSha256": (
            _file_sha256(answer_cases_path) if answer_cases_path is not None else ""
        ),
        "sourceRetrievalReportSha256": _file_sha256(retrieval_report_path),
        "dataset": dict(slice_manifest),
        "evaluation": {
            "mode": evaluation_mode,
            "split": evaluation_split,
            "caseCount": len(case_ids),
            "caseIds": case_ids,
            "caseAliases": case_aliases,
            "caseIdsSha256": _sha256_json(case_ids),
            "caseSetSha256": str(
                answer_case_manifest.get("selectedCaseSetSha256")
                or _sha256_json(case_ids)
            ),
            "answerCaseManifestSha256": str(
                answer_case_manifest.get("manifestSha256") or ""
            ),
            "answerCaseSetSha256": str(
                answer_case_manifest.get("answerCaseSetSha256") or ""
            ),
            "promptConfigSha256": prompt_config_sha256,
            "formalAcceptanceEligible": False,
            "heldOutLabelsInPrompt": False,
        },
        "preflight": {
            "stage": "embedding_or_reranker_setup",
            "accepted": False,
            "failure": failure[:1_000],
            "failureSha256": hashlib.sha256(failure.encode("utf-8")).hexdigest(),
            "sandboxAllocated": False,
        },
        "embedding": dict(embedding),
        "lanes": [],
        "knowledgeAblation": {
            "accepted": False,
            "reason": "preflight failed before any lane ran",
        },
        "agentAblation": {
            "accepted": False,
            "reason": "preflight failed before any lane ran",
        },
        "cleanup": {
            "required": False,
            "passed": True,
            "reason": "no benchmark sandbox was allocated",
        },
        "cleanupPassed": True,
        "calibration": {
            "enabled": calibration_no_metal,
            "formalAcceptanceEligible": False,
            "passed": False if calibration_no_metal else None,
        },
        "development": {
            "enabled": development_only,
            "formalAcceptanceEligible": False,
            "underlyingHardGatesPassed": False if development_only else None,
        },
        "failure": failure[:1_000],
    }
    return _finalize_public_report(report, checkpoint=checkpoint, scene_recipe=scene_recipe,
                                   candidate_prompt=candidate_prompt, judge_model=judge_model)


def _require_actual_metal_runtime() -> None:
    """Check a real MLX device operation before loading a native model in-process."""

    probe = (
        "import mlx.core as mx; "
        "x = mx.array([1.0]); mx.eval(x); "
        "print('metal-probe-ok', flush=True)"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"MLX Metal preflight could not run: {exc}") from exc
    if completed.returncode != 0 or "metal-probe-ok" not in completed.stdout:
        detail = (completed.stderr or completed.stdout or "no diagnostic").strip()
        raise RuntimeError(
            "MLX Metal preflight failed "
            f"(exit={completed.returncode}): {detail[:500]}"
        )


def _run_lane(
    service: AgentService,
    *,
    gateway: RagBenchmarkAgentGateway,
    tool_transport: object | None = None,
    owner: str,
    run_id: str,
    lane: str,
    cases: list[Mapping[str, object]],
    retrieval_config: Mapping[str, object],
    retrieval_config_sha256: str,
    timeout_seconds: float,
    lane_attempts: int,
    answer_only: bool = False,
    evaluation_mode: str | None = None,
    evaluation_split: str = "validation",
    agentic_supplemental_limit: int = _AGENTIC_MAX_SUPPLEMENTAL_TOTAL,
    prompt_profile: str = _INCUMBENT_PROMPT_PROFILE,
    evaluation_model: str = _EVALUATION_MODEL,
    candidate_prompt: CandidatePrompt | None = None,
    attempt_start: int = 1,
    attempt_start_observer: Callable[[int], None] | None = None,
    attempt_binding_observer: Callable[[int, str, str], None] | None = None,
    attempt_observer: Callable[[int, Mapping[str, object]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    on_session: Callable[[str], None] | None = None,
    on_turn: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    service = _execution_service(service, cancelled=cancelled, on_session=on_session, on_turn=on_turn)
    attempts: list[dict[str, object]] = []
    result: dict[str, Any] | None = None
    maximum_attempts = max(1, lane_attempts)
    if attempt_start < 1 or attempt_start > maximum_attempts:
        raise ValueError("lane attempt start is outside the configured budget")
    for attempt_number in range(attempt_start, maximum_attempts + 1):
        _execution_check(service)
        if attempt_start_observer is not None:
            attempt_start_observer(attempt_number)
        transport_cursor = _tool_transport_cursor(tool_transport)
        try:
            result = _run_lane_once(
                service,
                gateway=gateway,
                owner=owner,
                run_id=run_id,
                lane=lane,
                cases=cases,
                retrieval_config=retrieval_config,
                retrieval_config_sha256=retrieval_config_sha256,
                timeout_seconds=timeout_seconds,
                answer_only=answer_only,
                evaluation_mode=(
                    str(evaluation_mode)
                    if evaluation_mode is not None
                    else ("answer-only" if answer_only else "retrieval-and-answer")
                ),
                evaluation_split=evaluation_split,
                agentic_supplemental_limit=agentic_supplemental_limit,
                prompt_profile=prompt_profile,
                evaluation_model=evaluation_model,
                candidate_prompt=candidate_prompt,
                attempt_binding_observer=(
                    (
                        lambda session_id, turn_id, attempt_number=attempt_number: attempt_binding_observer(
                            attempt_number, session_id, turn_id
                        )
                    )
                    if attempt_binding_observer is not None
                    else None
                ),
            )
        except RagEvaluationCancelled:
            # The ordinary result path unbinds after collecting its evidence.
            # Stop exits early, so release that same lineage without fabricating
            # a completed attempt; the admitted checkpoint remains recoverable.
            if isinstance(service, _RagControlledService) and service._current_session:
                try:
                    gateway.unbind_lineage(service._current_session)
                except Exception as exc:
                    raise RagEvaluationCancelled("RAG evaluation cancelled; Tool binding cleanup failed", interrupted=True) from exc
            raise
        transport_receipt = _tool_transport_receipt(
            tool_transport,
            since_sequence=transport_cursor,
        )
        result["toolTransport"] = transport_receipt
        if transport_receipt.get("accepted") is not True:
            result["toolContract"] = False
            if not str(result.get("runtimeFailureCategory") or ""):
                result["runtimeFailureCategory"] = "harness_transport_failure"
        if attempt_observer is not None:
            attempt_observer(attempt_number, result)
        _execution_check(service)
        retryable = result["runtimeFailureCategory"] in {
            "provider_transient_before_tool",
            "provider_transient_after_tool",
        }
        attempts.append(
            {
                "attempt": attempt_number,
                "terminalEvent": result["terminalEvent"],
                "runtimeFailureCategory": result["runtimeFailureCategory"],
                "gatewayItemCount": int(result["gatewayLedger"].get("itemCount") or 0),
                "retryScheduled": retryable and attempt_number < maximum_attempts,
            }
        )
        if not retryable or attempt_number >= maximum_attempts:
            break
        _progress(
            "lane_retry",
            lane=lane,
            attempt=attempt_number,
            reason=str(result["runtimeFailureCategory"]),
        )
    assert result is not None
    result["runtimeAttempts"] = attempts
    result["runtimeRetryCount"] = len(attempts) - 1
    return result


def _run_lane_once(
    service: AgentService,
    *,
    gateway: RagBenchmarkAgentGateway,
    owner: str,
    run_id: str,
    lane: str,
    cases: list[Mapping[str, object]],
    retrieval_config: Mapping[str, object],
    retrieval_config_sha256: str,
    timeout_seconds: float,
    answer_only: bool = False,
    evaluation_mode: str = "retrieval-and-answer",
    evaluation_split: str = "validation",
    agentic_supplemental_limit: int = _AGENTIC_MAX_SUPPLEMENTAL_TOTAL,
    prompt_profile: str = _INCUMBENT_PROMPT_PROFILE,
    evaluation_model: str = _EVALUATION_MODEL,
    candidate_prompt: CandidatePrompt | None = None,
    attempt_binding_observer: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    include_skill = LANE_FEATURES[lane]["skill"]
    max_searches = _AGENTIC_MAX_SEARCHES_PER_CASE if lane == "agentic" else 1
    session = service.create_session(
        {
            "title": f"RAG Agent ablation: {lane}",
            "mode": "assistant",
            "_modelRoute": "primary",
            "roleId": "companion-firstlight-v1",
            "roleVersion": "1",
            "toolProfileVersion": "subagent-readonly-v1",
        }
    )["session"]
    session_id = str(session["id"])
    if attempt_binding_observer is not None:
        attempt_binding_observer(session_id, "")
    service.update_session(
        session_id,
        {
            "mode": "assistant",
            "executionMode": "read_only",
            "toolProfileVersion": "subagent-readonly-v1",
            "projectContextEnabled": False,
            "piSkillsEnabled": include_skill,
            "codexSkillsEnabled": False,
            "workspaceRoots": [],
        },
    )
    binding = gateway.bind_session(
        session_id,
        allowed_operations=("search", "status"),
        sandbox_owner_id=owner,
        sandbox_run_id=run_id,
        allowed_base_tools=("agents",),
    )
    ensure: dict[str, object] = {}
    prompt_receipt: dict[str, object] = {}
    events: list[dict[str, object]] = []
    message_snapshot: dict[str, object] = {}
    terminal = ""
    error = ""
    correction_case_ids: list[str] = []
    correction_turn_count = 0
    initial_assistant_text = ""
    coverage_audit_receipt: dict[str, object] = {}
    coverage_audit_terminal = ""
    coverage_audit_prompt_sha256 = ""
    coverage_audit_ledger_items_before = 0
    coverage_audit_ledger_items_after = 0
    coverage_audit_started_tools_before = 0
    output_protocol_repair_receipt: dict[str, object] = {}
    output_protocol_repair_terminal = ""
    output_protocol_repair_prompt_sha256 = ""
    output_protocol_repair_ledger_items_before = 0
    output_protocol_repair_ledger_items_after = 0
    output_protocol_repair_started_tools_before = 0
    output_protocol_repair_turn_count = 0
    output_protocol_repair_expected = False
    pre_protocol_repair_text = ""
    started = time.perf_counter()
    lane_prompt = _lane_prompt(
        lane=lane,
        run_id=run_id,
        cases=cases,
        retrieval_config=retrieval_config,
        evaluation_mode=evaluation_mode,
        evaluation_split=evaluation_split,
        agentic_supplemental_limit=agentic_supplemental_limit,
        prompt_profile=prompt_profile,
        candidate_prompt=candidate_prompt,
    )
    try:
        ensure = service.ensure_runtime({"sessionId": session_id})
        if not _is_evaluation_model_max(
            ensure,
            evaluation_model=evaluation_model,
        ):
            raise RuntimeError(
                f"lane did not open {_EVALUATION_PROVIDER}/{evaluation_model} "
                f"at {_EVALUATION_THINKING}"
            )
        prompt_receipt = service.prompt(
            session_id,
            {
                "message": lane_prompt,
                "clientMessageId": f"rag-ablation:{lane}:{int(time.time() * 1_000)}",
            },
        )
        accepted_turn_id = str(prompt_receipt.get("turnId") or "")
        if attempt_binding_observer is not None and accepted_turn_id:
            attempt_binding_observer(
                session_id,
                accepted_turn_id,
            )
        events, terminal = _wait_for_terminal(
            service,
            session_id=session_id,
            turn_id=str(prompt_receipt.get("turnId") or ""),
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    try:
        snapshot = service.messages(session_id)
    except Exception:
        snapshot = {}
    if isinstance(snapshot, Mapping):
        message_snapshot = dict(snapshot)
        events = _merge_event_evidence(events, snapshot.get("liveEvents"))
    initial_assistant_text = _last_assistant_text(
        events
    ) or _last_assistant_snapshot_text(message_snapshot.get("items"))
    initial_runtime_tool_failure_count = _runtime_tool_failure_count(events)
    if (
        terminal == "turn_completed"
        and answer_only
        and include_skill
        and lane != "agentic"
        and not error
        and initial_runtime_tool_failure_count == 0
    ):
        initial_ledger = gateway.lineage_ledger(session_id)
        coverage_audit_ledger_items_before = int(
            initial_ledger.get("itemCount") or 0
        )
        coverage_audit_started_tools_before = len(
            _started_tool_names(events, message_snapshot.get("items"))
        )
        coverage_audit_prompt_sha256 = hashlib.sha256(
            _coverage_audit_prompt(cases).encode("utf-8")
        ).hexdigest()
        try:
            (
                coverage_audit_receipt,
                coverage_audit_events,
                coverage_audit_terminal,
            ) = _run_coverage_audit(
                service,
                session_id=session_id,
                cases=cases,
                timeout_seconds=timeout_seconds,
            )
            correction_turn_count = 1
            events = _merge_event_evidence(events, coverage_audit_events)
            terminal = coverage_audit_terminal
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            terminal = "turn_failed"
        try:
            revised_snapshot = service.messages(session_id)
        except Exception:
            revised_snapshot = {}
        if isinstance(revised_snapshot, Mapping):
            message_snapshot = dict(revised_snapshot)
            events = _merge_event_evidence(
                events,
                revised_snapshot.get("liveEvents"),
            )
    pre_protocol_repair_text = _last_assistant_text(
        events
    ) or _last_assistant_snapshot_text(message_snapshot.get("items"))
    output_protocol_repair_expected = bool(
        terminal == "turn_completed"
        and answer_only
        and not error
        and _runtime_tool_failure_count(events) == 0
        and _output_protocol_repair_needed(
            cases=cases,
            assistant_text=pre_protocol_repair_text,
        )
    )
    if output_protocol_repair_expected:
        initial_ledger = gateway.lineage_ledger(session_id)
        output_protocol_repair_ledger_items_before = int(
            initial_ledger.get("itemCount") or 0
        )
        output_protocol_repair_started_tools_before = len(
            _started_tool_names(events, message_snapshot.get("items"))
        )
        output_protocol_repair_prompt_sha256 = hashlib.sha256(
            _output_protocol_repair_prompt(
                cases=cases,
                assistant_text=pre_protocol_repair_text,
            ).encode("utf-8")
        ).hexdigest()
        try:
            (
                output_protocol_repair_receipt,
                output_protocol_repair_events,
                output_protocol_repair_terminal,
            ) = _run_output_protocol_repair(
                service,
                session_id=session_id,
                cases=cases,
                assistant_text=pre_protocol_repair_text,
                timeout_seconds=timeout_seconds,
            )
            output_protocol_repair_turn_count = 1
            events = _merge_event_evidence(events, output_protocol_repair_events)
            terminal = output_protocol_repair_terminal
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            terminal = "turn_failed"
        try:
            revised_snapshot = service.messages(session_id)
        except Exception:
            revised_snapshot = {}
        if isinstance(revised_snapshot, Mapping):
            message_snapshot = dict(revised_snapshot)
            events = _merge_event_evidence(
                events,
                revised_snapshot.get("liveEvents"),
            )
    elapsed_ms = round((time.perf_counter() - started) * 1_000, 3)
    ledger = gateway.lineage_ledger(session_id)
    coverage_audit_ledger_items_after = int(ledger.get("itemCount") or 0)
    output_protocol_repair_ledger_items_after = int(ledger.get("itemCount") or 0)
    lineage_session_ids = list(ledger.get("sessionIds") or [session_id])
    delegation_batches = service.delegation.store.list_batches(
        parent_session_id=session_id,
        limit=10,
    )
    child_runs = [
        dict(run)
        for batch in delegation_batches
        if isinstance(batch, Mapping)
        for run in batch.get("runs") or []
        if isinstance(run, Mapping)
    ]
    child_usage_receipts: list[dict[str, object]] = []
    for child_run in child_runs:
        child_usage_receipts.append(_child_token_usage(service, child_run))
    child_session_ids = {
        str(run.get("childSessionId") or "")
        for run in child_runs
        if str(run.get("childSessionId") or "")
    }
    child_searches = [
        item
        for item in ledger.get("items") or []
        if isinstance(item, Mapping)
        and str(item.get("sessionId") or "") in child_session_ids
        and item.get("operation") == "search"
        and item.get("ok") is True
    ]
    child_search_case_ids = {
        str(item.get("args", {}).get("evaluationCaseId") or "")
        for item in child_searches
        if isinstance(item.get("args"), Mapping)
    }
    coverage_critic_observed = _coverage_critic_completed(
        child_runs=child_runs,
        child_searches=child_searches,
    )
    search_parameter_policy = _search_parameter_policy_passes(
        ledger,
        lane=lane,
        retrieval_config=retrieval_config,
    )
    parent_query_policy = (
        _agentic_parent_query_policy_passes(
            ledger,
            parent_session_id=session_id,
            cases=cases,
            max_supplemental_total=agentic_supplemental_limit,
        )
        if lane == "agentic"
        else True
    )
    agents_tool_receipts = _agents_tool_receipts(events)
    assistant_text = _last_assistant_text(events) or _last_assistant_snapshot_text(
        message_snapshot.get("items")
    )
    if correction_turn_count == 1:
        initial_cases = _assistant_case_payload(initial_assistant_text)
        final_cases = _assistant_case_payload(assistant_text)
        correction_case_ids = sorted(
            case_id
            for case_id in {
                str(item.get("evaluationCaseId") or item.get("queryId") or "")
                for item in cases
            }
            if case_id
            and _sha256_json(initial_cases.get(case_id, {}))
            != _sha256_json(final_cases.get(case_id, {}))
        )
    score = (
        score_answer_only_lane(
            lane=lane,
            cases=cases,
            ledger=ledger,
            assistant_text=assistant_text,
            max_searches_per_case=max_searches,
        )
        if answer_only
        else score_agent_lane(
            lane=lane,
            cases=cases,
            ledger=ledger,
            assistant_text=assistant_text,
            max_searches_per_case=max_searches,
        )
    )
    runtime_tool_failure_count = _runtime_tool_failure_count(events)
    started_tools = _started_tool_names(events, message_snapshot.get("items"))
    coverage_audit_tool_call_count = max(
        0,
        len(started_tools) - coverage_audit_started_tools_before,
    ) if correction_turn_count == 1 else 0
    coverage_audit_expected = answer_only and include_skill and lane != "agentic"
    coverage_audit_policy = (
        correction_turn_count == 1
        and coverage_audit_terminal == "turn_completed"
        and coverage_audit_tool_call_count == 0
        and coverage_audit_ledger_items_after
        == coverage_audit_ledger_items_before
    ) if coverage_audit_expected else correction_turn_count == 0
    output_protocol_repair_tool_call_count = max(
        0,
        len(started_tools) - output_protocol_repair_started_tools_before,
    ) if output_protocol_repair_turn_count == 1 else 0
    output_protocol_repair_policy = (
        output_protocol_repair_turn_count == 1
        and output_protocol_repair_terminal == "turn_completed"
        and output_protocol_repair_tool_call_count == 0
        and output_protocol_repair_ledger_items_after
        == output_protocol_repair_ledger_items_before
        and not _output_protocol_repair_needed(
            cases=cases,
            assistant_text=assistant_text,
        )
    ) if output_protocol_repair_expected else output_protocol_repair_turn_count == 0
    required_tools = {"tool_load"}
    if include_skill:
        required_tools.add("skill_load")
    if lane == "agentic":
        required_tools.add("agents")
    skill_policy = (
        "skill_load" in started_tools
        if include_skill
        else "skill_load" not in started_tools
    )
    authorization_ok = all(
        isinstance(item, Mapping)
        and item.get("authorizationSource") in {
            "ephemeral_session_binding",
            "runtime_load_receipt",
        }
        and item.get("sandboxOwnerSha256") == binding["sandboxOwnerSha256"]
        for item in ledger.get("items") or []
    )
    token_usage = _token_usage(events)
    combined_usage = _combine_token_usage(token_usage, child_usage_receipts)
    child_tool_calls = sum(
        int((run.get("usage") or {}).get("toolCount") or 0)
        for run in child_runs
        if isinstance(run.get("usage"), Mapping)
    )
    expected_delegation_waves = 1
    delegation_receipts_passed = (
        (
            len(agents_tool_receipts) == expected_delegation_waves
            and all(
                receipt.get("operation") == "delegate"
                and receipt.get("finished") is True
                and receipt.get("isError") is False
                for receipt in agents_tool_receipts
            )
        )
        if lane == "agentic"
        else not agents_tool_receipts
    )
    critic_contract_passed = (
        _agentic_critic_contract_passes(
            child_runs=child_runs,
            child_searches=child_searches,
            agents_tool_receipts=agents_tool_receipts,
        )
        if lane == "agentic"
        else not child_runs and not agents_tool_receipts
    )
    delegation_policy = (
        "agents" in started_tools
        and critic_contract_passed
        if lane == "agentic"
        else "agents" not in started_tools and not child_runs
    )
    rag_invoked = int(ledger.get("itemCount") or 0) > 0
    runtime_failure_category = _runtime_failure_category(
        terminal=terminal,
        error=error,
        events=events,
        ledger=ledger,
    )
    features: dict[str, object] = {
        **LANE_FEATURES[lane],
        "maxRetrievalRounds": max_searches,
        "subagents": bool(child_runs),
        "subagentCount": len(child_runs),
        "childCaseAssignment": (
            "single-no-tool-selective-query-critic-v2"
            if lane == "agentic"
            else "none"
        ),
        "delegationWaveCount": len(agents_tool_receipts),
        "delegationReceiptsPassed": delegation_receipts_passed,
        "coverageCriticObserved": coverage_critic_observed,
        "criticContractPassed": critic_contract_passed,
        "childRetrievalObserved": bool(child_searches),
        "childSearchCalls": len(child_searches),
        "childSearchCaseIds": sorted(child_search_case_ids),
        "subagentPolicyPassed": delegation_policy,
        "searchParameterPolicyPassed": search_parameter_policy,
        "parentQueryPolicyPassed": parent_query_policy,
        "coverageAuditExpected": coverage_audit_expected,
        "coverageAuditPassed": coverage_audit_policy,
        "coverageAuditTerminal": coverage_audit_terminal,
        "coverageAuditToolCallCount": coverage_audit_tool_call_count,
        "coverageAuditLedgerItemDelta": (
            coverage_audit_ledger_items_after
            - coverage_audit_ledger_items_before
            if correction_turn_count == 1
            else 0
        ),
        "outputProtocolRepairExpected": output_protocol_repair_expected,
        "outputProtocolRepairPassed": output_protocol_repair_policy,
        "outputProtocolRepairTerminal": output_protocol_repair_terminal,
        "outputProtocolRepairToolCallCount": output_protocol_repair_tool_call_count,
        "outputProtocolRepairLedgerItemDelta": (
            output_protocol_repair_ledger_items_after
            - output_protocol_repair_ledger_items_before
            if output_protocol_repair_turn_count == 1
            else 0
        ),
        "outputProtocolRepairTurnCount": output_protocol_repair_turn_count,
        "synthesisCorrectionTriggered": correction_turn_count == 1,
        "synthesisCorrectionTurnCount": correction_turn_count,
        "synthesisCorrectionCaseCount": len(correction_case_ids),
        "synthesisCorrectionCaseIdsSha256": _sha256_json(
            sorted(correction_case_ids)
        ),
        "independentReranker": retrieval_config.get("rerankEnabled") is True,
        "rerankCandidateDepth": (
            int(retrieval_config.get("rerankCandidateDepth") or 0)
            if retrieval_config.get("rerankEnabled") is True
            else 0
        ),
    }
    unbound_session_ids = gateway.unbind_lineage(session_id)
    binding_cleanup = set(unbound_session_ids) == set(lineage_session_ids)
    return {
        "lane": lane,
        "features": features,
        "retrievalConfigSha256": retrieval_config_sha256,
        "promptSha256": hashlib.sha256(lane_prompt.encode("utf-8")).hexdigest(),
        "terminalEvent": terminal,
        "promptAccepted": bool(prompt_receipt.get("turnId")),
        "model": _public_model_state(ensure),
        "binding": binding,
        "_checkpointSessionId": session_id,
        "_checkpointTurnId": str(prompt_receipt.get("turnId") or ""),
        "startedTools": started_tools,
        "toolDiagnostics": _public_tool_diagnostics(events),
        "runtimeToolFailureCount": runtime_tool_failure_count,
        "terminalFailure": _terminal_failure(events),
        "score": score,
        "_assistantText": assistant_text,
        "synthesisReceipts": {
            "schemaVersion": "rag-ime.rag-answer-synthesis-receipts.v1",
            "initialAnswerSha256": hashlib.sha256(
                initial_assistant_text.encode("utf-8")
            ).hexdigest(),
            "coverageAuditPromptSha256": coverage_audit_prompt_sha256,
            "coverageAuditTurnSha256": (
                hashlib.sha256(
                    str(coverage_audit_receipt.get("turnId") or "").encode("utf-8")
                ).hexdigest()
                if coverage_audit_receipt.get("turnId")
                else ""
            ),
            "coverageAuditTerminal": coverage_audit_terminal,
            "coverageAuditPassed": coverage_audit_policy,
            "preProtocolRepairAnswerSha256": hashlib.sha256(
                pre_protocol_repair_text.encode("utf-8")
            ).hexdigest(),
            "outputProtocolRepairPromptSha256": output_protocol_repair_prompt_sha256,
            "outputProtocolRepairTurnSha256": (
                hashlib.sha256(
                    str(output_protocol_repair_receipt.get("turnId") or "").encode(
                        "utf-8"
                    )
                ).hexdigest()
                if output_protocol_repair_receipt.get("turnId")
                else ""
            ),
            "outputProtocolRepairTerminal": output_protocol_repair_terminal,
            "outputProtocolRepairPassed": output_protocol_repair_policy,
            "finalAnswerSha256": hashlib.sha256(
                assistant_text.encode("utf-8")
            ).hexdigest(),
        },
        "costs": {
            "latencyMs": elapsed_ms,
            "tokens": float(combined_usage["totalTokens"]),
            "toolCalls": float(
                max(
                    len(started_tools) + child_tool_calls,
                    int(ledger.get("itemCount") or 0)
                    + sum(name in {"skill_load", "tool_load"} for name in started_tools),
                )
            ),
        },
        "usage": combined_usage,
        "subagentEvidence": [
            {
                "sessionSha256": hashlib.sha256(
                    str(run.get("childSessionId") or "").encode("utf-8")
                ).hexdigest(),
                "runSha256": hashlib.sha256(
                    str(run.get("id") or "").encode("utf-8")
                ).hexdigest(),
                "state": str(run.get("state") or ""),
                "usage": dict(run.get("usage") or {}),
                "error": str(run.get("error") or "")[:500],
                "errorSha256": hashlib.sha256(
                    str(run.get("error") or "").encode("utf-8")
                ).hexdigest(),
                "retrievalObserved": (
                    str(run.get("childSessionId") or "") in child_session_ids
                    and any(
                        str(item.get("sessionId") or "")
                        == str(run.get("childSessionId") or "")
                        for item in child_searches
                    )
                ),
                "searchCalls": sum(
                    str(item.get("sessionId") or "")
                    == str(run.get("childSessionId") or "")
                    for item in child_searches
                ),
            }
            for run in child_runs
        ],
        "agentsToolReceipts": agents_tool_receipts,
        "bindingCleanup": binding_cleanup,
        "scopeBoundary": authorization_ok,
        "crossSystemLeakage": "memory" not in started_tools,
        "toolContract": (
            not error
            and required_tools.issubset(set(started_tools))
            and rag_invoked
            and skill_policy
            and delegation_policy
            and search_parameter_policy
            and parent_query_policy
            and coverage_audit_policy
            and output_protocol_repair_policy
            and binding_cleanup
            and score["hardEvidence"]["parameterBounded"]
            and score["failedToolItemCount"] == 0
            and runtime_tool_failure_count == 0
        ),
        "gatewayLedger": ledger,
        "runtimeFailureCategory": runtime_failure_category,
        "error": error,
    }


def _answer_judge_case_payloads(
    *,
    cases: list[Mapping[str, object]],
    documents: list[Mapping[str, object]],
    lane_records: list[Mapping[str, object]],
    chunking_config: Mapping[str, object],
) -> tuple[
    list[str],
    dict[str, str],
    dict[str, dict[str, dict[str, object]]],
    list[dict[str, object]],
]:
    """Build anonymous Judge cases from only chunks observed by each candidate."""

    case_ids = [
        str(item.get("evaluationCaseId") or item.get("queryId") or "")
        for item in cases
    ]
    ordered_lanes = sorted(
        (str(item.get("lane") or "") for item in lane_records),
        key=lambda lane: _sha256_json(
            {
                "contract": _ANSWER_JUDGE_CONTRACT_VERSION,
                "caseIds": case_ids,
                "lane": lane,
            }
        ),
    )
    candidate_ids = {
        lane: f"C{index}"
        for index, lane in enumerate(ordered_lanes, start=1)
    }
    assistant_cases = {
        str(record.get("lane") or ""): _assistant_case_payload(
            str(record.get("_assistantText") or "")
        )
        for record in lane_records
    }
    document_text_by_id = {
        str(document.get("documentId") or ""): str(document.get("text") or "")
        for document in documents
        if str(document.get("documentId") or "").strip()
    }
    document_id_by_sha256 = {
        hashlib.sha256(document_id.encode("utf-8")).hexdigest(): document_id
        for document_id in document_text_by_id
    }
    citations_by_lane_case: dict[tuple[str, str], list[str]] = {}
    citation_tokens_by_lane_case: dict[tuple[str, str], list[str]] = {}
    observed_chunks: dict[tuple[str, str, str], set[tuple[str, int]]] = {}
    for record in lane_records:
        lane = str(record.get("lane") or "")
        score = record.get("score")
        answer_cases = score.get("answerCases") if isinstance(score, Mapping) else None
        if not isinstance(answer_cases, list):
            raise RuntimeError("lane answer evidence is missing before answer judgment")
        for answer_case in answer_cases:
            if not isinstance(answer_case, Mapping):
                raise RuntimeError("lane answer evidence contains a non-object case")
            case_id = str(answer_case.get("evaluationCaseId") or "")
            raw_citations = answer_case.get("citations")
            if not isinstance(raw_citations, list):
                raise RuntimeError("lane answer evidence has invalid citations")
            citation_ids = [str(item).strip() for item in raw_citations]
            if (
                len(set(citation_ids)) != len(citation_ids)
                or any(not item or item not in document_text_by_id for item in citation_ids)
            ):
                raise RuntimeError("lane answer evidence cites an unknown document")
            citations_by_lane_case[(lane, case_id)] = citation_ids
            raw_citation_tokens = answer_case.get("citationTokens")
            if not isinstance(raw_citation_tokens, list):
                raise RuntimeError("lane answer evidence is missing citation tokens")
            citation_tokens = [str(item).strip() for item in raw_citation_tokens]
            if len(set(citation_tokens)) != len(citation_tokens) or any(
                not item for item in citation_tokens
            ):
                raise RuntimeError("lane answer evidence has invalid citation tokens")
            citation_tokens_by_lane_case[(lane, case_id)] = citation_tokens

        ledger = record.get("gatewayLedger")
        raw_items = ledger.get("items") if isinstance(ledger, Mapping) else None
        if not isinstance(raw_items, list):
            raw_items = record.get("_privateCitedChunkRefs")
        if not isinstance(raw_items, list):
            raise RuntimeError("lane cited chunk evidence is missing before answer judgment")
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            operation = str(item.get("operation") or "search")
            if operation != "search" or item.get("ok") is not True:
                continue
            args = item.get("args")
            args = args if isinstance(args, Mapping) else item
            case_id = str(args.get("evaluationCaseId") or "")
            summary = item.get("resultSummary")
            summary = summary if isinstance(summary, Mapping) else item
            hits = summary.get("hits")
            if not isinstance(hits, list):
                continue
            for hit in hits:
                if not isinstance(hit, Mapping):
                    continue
                document_id = str(hit.get("externalDocumentId") or "").strip()
                if not document_id:
                    document_id = document_id_by_sha256.get(
                        str(hit.get("externalDocumentSha256") or ""),
                        "",
                    )
                ordinal = hit.get("ordinal")
                if (
                    not document_id
                    or document_id not in document_text_by_id
                    or isinstance(ordinal, bool)
                    or not isinstance(ordinal, int)
                    or ordinal < 0
                ):
                    raise RuntimeError("lane retrieved chunk evidence is invalid")
                citation_ref = str(hit.get("citationRef") or "").strip()
                if not citation_ref:
                    continue
                observed_chunks.setdefault((lane, case_id, citation_ref), set()).add(
                    (document_id, ordinal)
                )

    normalized_chunking = {
        **DEFAULT_CHUNKING_CONFIG,
        **dict(chunking_config),
    }
    chunk_text_by_key: dict[tuple[str, int], str] = {}
    for document_id, document_text in document_text_by_id.items():
        chunks = _chunk_document(
            ParsedDocument(
                text=_normalize_text(document_text),
                provider="builtin",
                provider_version="text-v1",
            ),
            document_id=document_id,
            base_id="answer-judge",
            chunking_config=normalized_chunking,
        )
        for chunk in chunks:
            chunk_text_by_key[(document_id, int(chunk["ordinal"]))] = str(
                chunk["content"]
            )

    judge_cases: list[dict[str, object]] = []
    for case in cases:
        case_id = str(case.get("evaluationCaseId") or case.get("queryId") or "")
        cited_chunk_keys: set[tuple[str, int]] = set()
        candidate_chunk_keys: dict[str, list[tuple[str, int]]] = {}
        for lane in ordered_lanes:
            lane_keys: list[tuple[str, int]] = []
            cited_documents: list[str] = []
            for citation_ref in citation_tokens_by_lane_case.get((lane, case_id), []):
                chunk_keys = sorted(
                    observed_chunks.get((lane, case_id, citation_ref), set())
                )
                if not chunk_keys:
                    raise RuntimeError(
                        "lane citation has no retrieved chunk evidence"
                    )
                for key in chunk_keys:
                    if key not in chunk_text_by_key:
                        raise RuntimeError(
                            "lane cited chunk is outside the frozen chunk manifest"
                        )
                    lane_keys.append(key)
                    cited_chunk_keys.add(key)
                    cited_documents.append(key[0])
            if list(dict.fromkeys(cited_documents)) != citations_by_lane_case.get(
                (lane, case_id), []
            ):
                raise RuntimeError(
                    "lane citation tokens and resolved documents disagree"
                )
            candidate_chunk_keys[lane] = list(dict.fromkeys(lane_keys))
        ordered_chunk_keys = sorted(
            cited_chunk_keys,
            key=lambda key: _sha256_json(
                {
                    "contract": _ANSWER_JUDGE_CONTRACT_VERSION,
                    "caseId": case_id,
                    "documentId": key[0],
                    "chunkOrdinal": key[1],
                    "chunkSha256": hashlib.sha256(
                        chunk_text_by_key[key].encode("utf-8")
                    ).hexdigest(),
                }
            ),
        )
        evidence_ids = {
            key: f"E{index}"
            for index, key in enumerate(ordered_chunk_keys, start=1)
        }
        candidates: list[dict[str, object]] = []
        for lane in ordered_lanes:
            answer = assistant_cases.get(lane, {}).get(case_id, {})
            candidates.append(
                {
                    "candidateId": candidate_ids[lane],
                    "answer": str(answer.get("answer") or ""),
                    "abstained": answer.get("abstained") is True,
                    "evidenceIds": [
                        evidence_ids[key] for key in candidate_chunk_keys[lane]
                    ],
                }
            )
        judge_cases.append(
            {
                "caseId": case_id,
                "question": str(case.get("query") or case.get("question") or ""),
                "referenceAnswer": str(case.get("answer") or ""),
                "evidenceDocuments": [
                    {
                        "evidenceId": evidence_ids[key],
                        "text": chunk_text_by_key[key],
                    }
                    for key in ordered_chunk_keys
                ],
                "candidates": candidates,
            }
        )
    return case_ids, candidate_ids, assistant_cases, judge_cases


def _run_answer_judge(
    service: AgentService,
    *,
    cases: list[Mapping[str, object]],
    documents: list[Mapping[str, object]],
    lane_records: list[Mapping[str, object]],
    chunking_config: Mapping[str, object],
    timeout_seconds: float,
    maximum_attempts: int = 2,
    evaluation_model: str = _EVALUATION_MODEL,
    cancelled: Callable[[], bool] | None = None,
    on_session: Callable[[str], None] | None = None,
    on_turn: Callable[[str, str], None] | None = None,
) -> dict[str, object]:
    """Retry only terminal judge infrastructure failure, never a judgment score."""

    service = _execution_service(service, cancelled=cancelled, on_session=on_session, on_turn=on_turn)
    attempts: list[dict[str, object]] = []
    bounded_attempts = max(1, min(2, int(maximum_attempts)))
    for attempt in range(1, bounded_attempts + 1):
        _execution_check(service)
        try:
            result = _run_answer_judge_once(
                service,
                cases=cases,
                documents=documents,
                lane_records=lane_records,
                chunking_config=chunking_config,
                timeout_seconds=timeout_seconds,
                evaluation_model=evaluation_model,
            )
            _execution_check(service)
        except RuntimeError as exc:
            failure = f"{type(exc).__name__}: {exc}"
            retryable = _answer_judge_failure_is_retryable(failure)
            attempts.append(
                {
                    "attempt": attempt,
                    "outcome": "terminal_failure",
                    "retryable": retryable,
                    "failure": failure[:500],
                    "failureSha256": hashlib.sha256(
                        failure.encode("utf-8")
                    ).hexdigest(),
                }
            )
            if retryable and attempt < bounded_attempts:
                continue
            return {
                "schemaVersion": "rag-ime.rag-answer-judge.v4",
                "accepted": False,
                "failure": failure,
                "runtimeAttempts": attempts,
                "runtimeRetryCount": len(attempts) - 1,
                "correctnessByLane": {},
                "judgments": [],
            }
        attempts.append(
            {
                "attempt": attempt,
                "outcome": (
                    "accepted" if result.get("accepted") is True else "rejected"
                ),
                "retryable": False,
                "failure": str(result.get("failure") or "")[:500],
                "failureSha256": hashlib.sha256(
                    str(result.get("failure") or "").encode("utf-8")
                ).hexdigest(),
            }
        )
        result["runtimeAttempts"] = attempts
        result["runtimeRetryCount"] = len(attempts) - 1
        return result
    raise AssertionError("bounded answer judge attempts exhausted without a result")


def _answer_judge_failure_is_retryable(failure: object) -> bool:
    normalized = str(failure or "").lower()
    return "answer judge ended with turn_failed" in normalized


def _run_answer_judge_once(
    service: AgentService,
    *,
    cases: list[Mapping[str, object]],
    documents: list[Mapping[str, object]],
    lane_records: list[Mapping[str, object]],
    chunking_config: Mapping[str, object],
    timeout_seconds: float,
    evaluation_model: str = _EVALUATION_MODEL,
) -> dict[str, object]:
    case_ids, candidate_ids, assistant_cases, judge_cases = (
        _answer_judge_case_payloads(
            cases=cases,
            documents=documents,
            lane_records=lane_records,
            chunking_config=chunking_config,
        )
    )
    prompt = _answer_judge_prompt(judge_cases)
    session = service.create_session(
        {
            "title": "CRUD-RAG anonymous answer correctness judge",
            "mode": "assistant",
            "_modelRoute": "primary",
            "modelProfile": f"{_EVALUATION_PROVIDER}/{evaluation_model}",
            "thinkingLevel": _EVALUATION_THINKING,
            "roleId": "companion-firstlight-v1",
            "roleVersion": "1",
            "toolProfileVersion": "subagent-readonly-v1",
        }
    )["session"]
    session_id = str(session["id"])
    service.update_session(
        session_id,
        {
            "mode": "assistant",
            "executionMode": "read_only",
            "toolProfileVersion": "subagent-readonly-v1",
            "projectContextEnabled": False,
            "piSkillsEnabled": False,
            "codexSkillsEnabled": False,
            "workspaceRoots": [],
        },
    )
    started = time.perf_counter()
    ensure = service.ensure_runtime({"sessionId": session_id})
    if not _is_evaluation_model_max(
        ensure,
        evaluation_model=evaluation_model,
    ):
        raise RuntimeError(
            f"answer judge did not open {_EVALUATION_PROVIDER}/{evaluation_model} "
            f"at {_EVALUATION_THINKING}"
        )
    receipt = service.prompt(
        session_id,
        {
            "message": prompt,
            "clientMessageId": f"rag-answer-judge:{int(time.time() * 1_000)}",
        },
    )
    events, terminal = _wait_for_terminal(
        service,
        session_id=session_id,
        turn_id=str(receipt.get("turnId") or ""),
        timeout_seconds=timeout_seconds,
    )
    snapshot = service.messages(session_id)
    if isinstance(snapshot, Mapping):
        events = _merge_event_evidence(events, snapshot.get("liveEvents"))
        snapshot_items = snapshot.get("items")
    else:
        snapshot_items = None
    if terminal != "turn_completed":
        raise RuntimeError(f"answer judge ended with {terminal or 'no terminal event'}")
    started_tools = _started_tool_names(events, snapshot_items)
    if started_tools:
        raise RuntimeError("answer judge invoked tools despite the no-tool contract")
    assistant_text = _last_assistant_text(events) or _last_assistant_snapshot_text(
        snapshot_items
    )
    assistant_outputs = [assistant_text]
    format_repair_turn_count = 0
    expected_pairs = {
        (case_id, candidate_id)
        for case_id in case_ids
        for candidate_id in candidate_ids.values()
    }
    parse_failure = ""
    try:
        case_rubrics = _parse_answer_judge_rubrics(
            assistant_text,
            expected_case_ids=set(case_ids),
        )
        anonymous = _parse_answer_judgments(
            assistant_text,
            expected_pairs=expected_pairs,
        )
    except RuntimeError as exc:
        parse_failure = f"{type(exc).__name__}: {exc}"
        repair_receipt = service.prompt(
            session_id,
            {
                "message": _answer_judge_format_repair_prompt(
                    expected_pairs=expected_pairs,
                ),
                "clientMessageId": (
                    f"rag-answer-judge:format-repair:{int(time.time() * 1_000)}"
                ),
            },
        )
        repair_events, terminal = _wait_for_terminal(
            service,
            session_id=session_id,
            turn_id=str(repair_receipt.get("turnId") or ""),
            timeout_seconds=timeout_seconds,
        )
        events = _merge_event_evidence(events, repair_events)
        format_repair_turn_count = 1
        if terminal != "turn_completed":
            parse_failure += f"; format repair ended with {terminal or 'no terminal event'}"
        elif _started_tool_names(repair_events, None):
            parse_failure += "; format repair invoked a tool"
        else:
            repaired_text = _last_assistant_text(repair_events)
            if not repaired_text:
                repair_snapshot = service.messages(session_id)
                repaired_items = (
                    repair_snapshot.get("items")
                    if isinstance(repair_snapshot, Mapping)
                    else None
                )
                repaired_text = _last_assistant_snapshot_text(repaired_items)
            assistant_outputs.append(repaired_text)
            assistant_text = repaired_text
            try:
                case_rubrics = _parse_answer_judge_rubrics(
                    assistant_text,
                    expected_case_ids=set(case_ids),
                )
                anonymous = _parse_answer_judgments(
                    assistant_text,
                    expected_pairs=expected_pairs,
                )
                parse_failure = ""
            except RuntimeError as exc:
                parse_failure += f"; {type(exc).__name__}: {exc}"
    if parse_failure:
        usage = _token_usage(events)
        return {
            "schemaVersion": "rag-ime.rag-answer-judge.v4",
            "accepted": False,
            "failure": parse_failure,
            "contractVersion": _ANSWER_JUDGE_CONTRACT_VERSION,
            "model": _public_model_state(ensure),
            "referenceAnswerAccessAfterGeneration": True,
            "retrievalQrelAccess": False,
            "candidateCitedEvidenceAccessAfterGeneration": True,
            "judgeFeedbackToAgent": False,
            "anonymousCandidates": True,
            "candidateMapping": [
                {"candidateId": candidate_ids[lane], "lane": lane}
                for lane in sorted(candidate_ids)
            ],
            "evidenceRetention": {
                "scope": "local-only-public-benchmark",
                "question": True,
                "referenceAnswer": True,
                "generatedAnswer": True,
                "candidateCitedEvidence": True,
                "candidateCitedEvidenceGranularity": "retrieved-chunk",
                "judgeRawOutputs": True,
                "uploaded": False,
            },
            "evidenceCases": judge_cases,
            "rubricSha256": hashlib.sha256(
                _ANSWER_JUDGE_RUBRIC.encode("utf-8")
            ).hexdigest(),
            "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "promptChars": len(prompt),
            "assistantOutputs": assistant_outputs,
            "assistantOutputSha256s": [
                hashlib.sha256(item.encode("utf-8")).hexdigest()
                for item in assistant_outputs
            ],
            "terminalEvent": terminal,
            "toolCalls": 0,
            "formatRepairTurnCount": format_repair_turn_count,
            "latencyMs": round((time.perf_counter() - started) * 1_000, 3),
            "tokens": int(usage["totalTokens"]),
            "correctnessByLane": {},
            "judgments": [],
        }
    lane_by_candidate = {
        candidate_id: lane
        for lane, candidate_id in candidate_ids.items()
    }
    references = {
        str(case.get("evaluationCaseId") or case.get("queryId") or ""): str(
            case.get("answer") or ""
        )
        for case in cases
    }
    answer_hashes = {
        (str(record.get("lane") or ""), case_id): hashlib.sha256(
            str(assistant_cases.get(str(record.get("lane") or ""), {}).get(case_id, {}).get("answer") or "").encode(
                "utf-8"
            )
        ).hexdigest()
        for record in lane_records
        for case_id in case_ids
    }
    judgments = [
        {
            "lane": lane_by_candidate[str(item["candidateId"])],
            "candidateId": str(item["candidateId"]),
            "evaluationCaseId": str(item["caseId"]),
            "correct": item["correct"] is True,
            "reasonCode": str(item["reasonCode"]),
            "coveredFactIds": list(item["coveredFactIds"]),
            "hasContradiction": item["hasContradiction"] is True,
            "hasUnsupportedMaterial": item["hasUnsupportedMaterial"] is True,
            "answerSha256": answer_hashes[
                (
                    lane_by_candidate[str(item["candidateId"])],
                    str(item["caseId"]),
                )
            ],
            "referenceAnswerSha256": hashlib.sha256(
                references[str(item["caseId"])].encode("utf-8")
            ).hexdigest(),
        }
        for item in anonymous
    ]
    correctness_by_lane = {
        lane: sum(
            item["correct"] is True
            for item in judgments
            if item["lane"] == lane
        )
        / max(1, len(case_ids))
        for lane in LANES
    }
    usage = _token_usage(events)
    return {
        "schemaVersion": "rag-ime.rag-answer-judge.v4",
        "accepted": True,
        "contractVersion": _ANSWER_JUDGE_CONTRACT_VERSION,
        "model": _public_model_state(ensure),
        "referenceAnswerAccessAfterGeneration": True,
        "retrievalQrelAccess": False,
        "candidateCitedEvidenceAccessAfterGeneration": True,
        "judgeFeedbackToAgent": False,
        "anonymousCandidates": True,
        "candidateMapping": [
            {"candidateId": candidate_ids[lane], "lane": lane}
            for lane in sorted(candidate_ids)
        ],
        "evidenceRetention": {
            "scope": "local-only-public-benchmark",
            "question": True,
            "referenceAnswer": True,
            "generatedAnswer": True,
            "candidateCitedEvidence": True,
            "candidateCitedEvidenceGranularity": "retrieved-chunk",
            "judgeRawOutputs": True,
            "uploaded": False,
        },
        "evidenceCases": judge_cases,
        "caseRubrics": case_rubrics,
        "rubricSha256": hashlib.sha256(
            _ANSWER_JUDGE_RUBRIC.encode("utf-8")
        ).hexdigest(),
        "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "promptChars": len(prompt),
        "assistantOutputSha256": hashlib.sha256(
            assistant_text.encode("utf-8")
        ).hexdigest(),
        "assistantOutputs": assistant_outputs,
        "assistantOutputSha256s": [
            hashlib.sha256(item.encode("utf-8")).hexdigest()
            for item in assistant_outputs
        ],
        "terminalEvent": terminal,
        "toolCalls": 0,
        "formatRepairTurnCount": format_repair_turn_count,
        "latencyMs": round((time.perf_counter() - started) * 1_000, 3),
        "tokens": int(usage["totalTokens"]),
        "correctnessByLane": correctness_by_lane,
        "judgments": judgments,
    }


def _answer_judge_prompt(cases: list[Mapping[str, object]]) -> str:
    return (
        "You are an isolated answer-correctness evaluator. You run after generation and must not call tools. "
        "Candidate IDs are anonymous; never infer systems or prefer a style. Rubric="
        + _ANSWER_JUDGE_RUBRIC
        + " Return only one JSON object: "
        '{"caseRubrics":[{"caseId":"case-01","requiredFacts":['
        '{"factId":"F1","description":"the explicitly requested fact"}]}],'
        '"judgments":[{"caseId":"case-01","candidateId":"C1",'
        '"coveredFactIds":["F1"],"hasContradiction":false,'
        '"hasUnsupportedMaterial":false,"correct":true,"reasonCode":"correct"}]}. '
        "Use 1-12 short required facts per case. coveredFactIds must refer only to that case's "
        "requiredFacts. correct is true exactly when every required fact is covered and there is "
        "no contradiction or material claim unsupported by the candidate's own evidenceIds. "
        "Evidence documents not named by that candidate are unavailable to it. The reference "
        "answer is not exhaustive evidence. An abstention cannot be correct. "
        "reasonCode must be one of correct, incomplete, wrong, abstained, unsupported. "
        "Return exactly one judgment for every candidate in every case. Cases="
        + json.dumps(cases, ensure_ascii=False, separators=(",", ":"))
    )


def _answer_judge_format_repair_prompt(
    *,
    expected_pairs: set[tuple[str, str]],
) -> str:
    return (
        "Your previous answer failed only the machine-readable schema contract. Do not call tools, "
        "do not ask questions, and do not use any score or feedback about which candidates should pass. "
        "Re-emit your existing judgment as exactly one JSON object with top-level caseRubrics and "
        "judgments arrays, no Markdown or commentary. Preserve the same required facts and decisions. "
        "Each judgment must contain caseId, candidateId, coveredFactIds, hasContradiction, "
        "hasUnsupportedMaterial, correct, and reasonCode. Return exactly these case/candidate pairs="
        + json.dumps(
            [
                {"caseId": case_id, "candidateId": candidate_id}
                for case_id, candidate_id in sorted(expected_pairs)
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _decode_answer_judge_payload(text: str) -> Mapping[str, object]:
    decoder = json.JSONDecoder()
    normalized = str(text or "")
    for index, character in enumerate(normalized):
        if character != "{":
            continue
        try:
            candidate, _end = decoder.raw_decode(normalized[index:])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(candidate, Mapping)
            and isinstance(candidate.get("caseRubrics"), list)
            and isinstance(candidate.get("judgments"), list)
        ):
            return candidate
    raise RuntimeError("answer judge output is not a fact-rubric judgments JSON object")


def _parse_answer_judge_rubrics(
    text: str,
    *,
    expected_case_ids: set[str],
) -> list[dict[str, object]]:
    value = _decode_answer_judge_payload(text)
    rubrics: list[dict[str, object]] = []
    seen_cases: set[str] = set()
    for raw in value["caseRubrics"]:
        if not isinstance(raw, Mapping):
            raise RuntimeError("answer judge emitted a non-object case rubric")
        case_id = str(raw.get("caseId") or "").strip()
        facts = raw.get("requiredFacts")
        if case_id not in expected_case_ids or case_id in seen_cases:
            raise RuntimeError("answer judge emitted an unexpected or duplicate case rubric")
        if not isinstance(facts, list) or not 1 <= len(facts) <= 12:
            raise RuntimeError("answer judge must emit 1-12 required facts per case")
        normalized_facts: list[dict[str, str]] = []
        fact_ids: set[str] = set()
        for fact in facts:
            if not isinstance(fact, Mapping):
                raise RuntimeError("answer judge emitted a non-object required fact")
            fact_id = str(fact.get("factId") or "").strip()
            description = str(fact.get("description") or "").strip()
            if (
                not fact_id
                or len(fact_id) > 32
                or fact_id in fact_ids
                or not description
                or len(description) > 300
            ):
                raise RuntimeError("answer judge emitted an invalid required fact")
            fact_ids.add(fact_id)
            normalized_facts.append(
                {"factId": fact_id, "description": description}
            )
        seen_cases.add(case_id)
        rubrics.append(
            {"caseId": case_id, "requiredFacts": normalized_facts}
        )
    if seen_cases != expected_case_ids:
        raise RuntimeError("answer judge omitted one or more case rubrics")
    return sorted(rubrics, key=lambda item: str(item["caseId"]))


def _parse_answer_judgments(
    text: str,
    *,
    expected_pairs: set[tuple[str, str]],
) -> list[dict[str, object]]:
    value = _decode_answer_judge_payload(text)
    case_ids = {case_id for case_id, _candidate_id in expected_pairs}
    rubrics = _parse_answer_judge_rubrics(
        text,
        expected_case_ids=case_ids,
    )
    required_by_case = {
        str(rubric["caseId"]): {
            str(fact["factId"])
            for fact in rubric["requiredFacts"]
            if isinstance(fact, Mapping)
        }
        for rubric in rubrics
    }
    judgments: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for raw in value["judgments"]:
        if not isinstance(raw, Mapping):
            raise RuntimeError("answer judge emitted a non-object judgment")
        pair = (
            str(raw.get("caseId") or "").strip(),
            str(raw.get("candidateId") or "").strip(),
        )
        if pair not in expected_pairs or pair in seen:
            raise RuntimeError("answer judge emitted an unexpected or duplicate candidate")
        correct = raw.get("correct")
        reason = str(raw.get("reasonCode") or "").strip()
        covered_raw = raw.get("coveredFactIds")
        contradiction = raw.get("hasContradiction")
        unsupported = raw.get("hasUnsupportedMaterial")
        if (
            not isinstance(correct, bool)
            or reason not in _ANSWER_JUDGE_REASON_CODES
            or not isinstance(covered_raw, list)
            or not isinstance(contradiction, bool)
            or not isinstance(unsupported, bool)
        ):
            raise RuntimeError("answer judge emitted an invalid correctness judgment")
        covered = [str(item).strip() for item in covered_raw]
        covered_set = set(covered)
        required = required_by_case[pair[0]]
        if (
            len(covered_set) != len(covered)
            or not covered_set.issubset(required)
        ):
            raise RuntimeError("answer judge emitted invalid covered fact IDs")
        complete = covered_set == required
        expected_correct = complete and not contradiction and not unsupported
        if correct is not expected_correct:
            raise RuntimeError("answer judge correctness and reasonCode disagree")
        if correct and reason != "correct":
            raise RuntimeError("answer judge correctness and reasonCode disagree")
        if not correct and reason == "correct":
            raise RuntimeError("answer judge correctness and reasonCode disagree")
        if reason == "incomplete" and (complete or contradiction or unsupported):
            raise RuntimeError("answer judge incomplete reason is inconsistent")
        if reason == "wrong" and not contradiction:
            raise RuntimeError("answer judge wrong reason lacks a contradiction")
        if reason == "unsupported" and not unsupported:
            raise RuntimeError("answer judge unsupported reason lacks unsupported material")
        seen.add(pair)
        judgments.append(
            {
                "caseId": pair[0],
                "candidateId": pair[1],
                "coveredFactIds": covered,
                "hasContradiction": contradiction,
                "hasUnsupportedMaterial": unsupported,
                "correct": correct,
                "reasonCode": reason,
            }
        )
    if seen != expected_pairs:
        raise RuntimeError("answer judge omitted one or more candidates")
    return sorted(
        judgments,
        key=lambda item: (str(item["caseId"]), str(item["candidateId"])),
    )


def _apply_answer_judgments(
    lane_records: list[dict[str, Any]],
    judgments: object,
) -> None:
    if not isinstance(judgments, list):
        raise RuntimeError("answer judge evidence is missing judgments")
    lookup = {
        (str(item.get("lane") or ""), str(item.get("evaluationCaseId") or "")): item
        for item in judgments
        if isinstance(item, Mapping)
    }
    for lane_record in lane_records:
        lane = str(lane_record.get("lane") or "")
        score = lane_record.get("score")
        if not isinstance(score, dict):
            raise RuntimeError("lane score is missing before answer judgment")
        answer_cases = score.get("answerCases")
        metrics = score.get("agentMetrics")
        if not isinstance(answer_cases, list) or not isinstance(metrics, dict):
            raise RuntimeError("lane answer metrics are incomplete before answer judgment")
        char_success_count = 0
        judged_success_count = 0
        agent_success_count = 0
        for answer_case in answer_cases:
            if not isinstance(answer_case, dict):
                raise RuntimeError("lane answer case is invalid")
            case_id = str(answer_case.get("evaluationCaseId") or "")
            judgment = lookup.get((lane, case_id))
            if not isinstance(judgment, Mapping):
                raise RuntimeError("answer judge omitted a lane case")
            char_success = answer_case.get("answerSuccess") is True
            correct = judgment.get("correct") is True
            char_success_count += int(char_success)
            judged_success_count += int(correct)
            agent_success = (
                answer_case.get("toolSuccess") is True
                and answer_case.get("citationSuccess") is True
                and correct
            )
            agent_success_count += int(agent_success)
            answer_case["answerCharF1Success"] = char_success
            answer_case["answerJudgeCorrect"] = correct
            answer_case["answerJudgeReasonCode"] = str(
                judgment.get("reasonCode") or ""
            )
            answer_case["answerSuccess"] = correct
            answer_case["agentSuccess"] = agent_success
        denominator = max(1, len(answer_cases))
        metrics["answerCharF1SuccessRate"] = char_success_count / denominator
        metrics["answerJudgeCorrectnessRate"] = judged_success_count / denominator
        metrics["answerSuccessRate"] = judged_success_count / denominator
        metrics["agentSuccessRate"] = agent_success_count / denominator


def _answer_only_cited_chunk_keys(
    lane_record: Mapping[str, object],
    answer_case: Mapping[str, object],
) -> set[tuple[str, int]]:
    """Resolve only the retrieved chunks named by this case's citation tokens."""

    case_id = str(answer_case.get("evaluationCaseId") or "")
    citation_tokens = {
        str(item).strip()
        for item in answer_case.get("citationTokens") or []
        if str(item).strip()
    }
    cited_document_ids = {
        str(item).strip()
        for item in answer_case.get("citations") or []
        if str(item).strip()
    }
    document_id_by_sha256 = {
        hashlib.sha256(document_id.encode("utf-8")).hexdigest(): document_id
        for document_id in cited_document_ids
    }
    ledger = lane_record.get("gatewayLedger")
    raw_items = ledger.get("items") if isinstance(ledger, Mapping) else None
    if not isinstance(raw_items, list):
        raw_items = lane_record.get("_privateCitedChunkRefs")
    if not isinstance(raw_items, list):
        return set()

    cited_chunks: set[tuple[str, int]] = set()
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        if (
            str(item.get("operation") or "search") != "search"
            or item.get("ok") is not True
        ):
            continue
        args = item.get("args")
        args = args if isinstance(args, Mapping) else item
        if str(args.get("evaluationCaseId") or "") != case_id:
            continue
        summary = item.get("resultSummary")
        summary = summary if isinstance(summary, Mapping) else item
        hits = summary.get("hits")
        if not isinstance(hits, list):
            continue
        for hit in hits:
            if not isinstance(hit, Mapping):
                continue
            citation_ref = str(hit.get("citationRef") or "").strip()
            document_id = str(hit.get("externalDocumentId") or "").strip()
            if not document_id:
                document_id = document_id_by_sha256.get(
                    str(hit.get("externalDocumentSha256") or ""),
                    "",
                )
            if (
                document_id not in cited_document_ids
                or (
                    citation_ref not in citation_tokens
                    and document_id not in citation_tokens
                )
            ):
                continue
            chunk_ordinal = hit.get("ordinal")
            if (
                isinstance(chunk_ordinal, bool)
                or not isinstance(chunk_ordinal, int)
                or chunk_ordinal < 0
            ):
                raise RuntimeError("answer-only cited chunk evidence is invalid")
            cited_chunks.add((document_id, chunk_ordinal))
    return cited_chunks


def _apply_answer_only_judgments(
    lane_records: list[dict[str, Any]],
    *,
    cases: list[Mapping[str, object]],
    answer_judge: Mapping[str, object],
    answer_case_manifest: Mapping[str, object],
) -> None:
    """Combine semantic judgments with host-only fact citation qrels."""

    raw_judgments = answer_judge.get("judgments")
    raw_rubrics = answer_judge.get("caseRubrics")
    if not isinstance(raw_judgments, list) or not isinstance(raw_rubrics, list):
        raise RuntimeError("answer-only judge evidence is incomplete")
    judgment_lookup = {
        (str(item.get("lane") or ""), str(item.get("evaluationCaseId") or "")): item
        for item in raw_judgments
        if isinstance(item, Mapping)
    }
    required_counts = {
        str(item.get("caseId") or ""): len(item.get("requiredFacts") or [])
        for item in raw_rubrics
        if isinstance(item, Mapping)
    }
    case_by_evaluation_id = {
        str(item.get("evaluationCaseId") or item.get("queryId") or ""): {
            "queryId": str(item.get("queryId") or ""),
            "abstentionExpected": bool(item.get("abstentionExpected")),
        }
        for item in cases
    }
    private_qrels = answer_case_manifest.get("_privateEvidenceQrels")
    if not isinstance(private_qrels, Mapping):
        raise RuntimeError("answer-only host evidence qrels are missing")
    for lane_record in lane_records:
        lane = str(lane_record.get("lane") or "")
        score = lane_record.get("score")
        if not isinstance(score, dict):
            raise RuntimeError("answer-only lane score is missing")
        answer_cases = score.get("answerCases")
        metrics = score.get("agentMetrics")
        if not isinstance(answer_cases, list) or not isinstance(metrics, dict):
            raise RuntimeError("answer-only lane metrics are incomplete")
        high_level_correct_count = 0
        high_level_fact_count = 0
        high_level_fact_covered = 0
        citation_fact_count = 0
        citation_fact_covered = 0
        answerable_citation_support_count = 0
        info_not_found_correct_count = 0
        agent_success_count = 0
        for answer_case in answer_cases:
            if not isinstance(answer_case, dict):
                raise RuntimeError("answer-only case score is invalid")
            case_id = str(answer_case.get("evaluationCaseId") or "")
            case_identity = case_by_evaluation_id.get(case_id)
            if not isinstance(case_identity, Mapping):
                raise RuntimeError("answer-only case is outside the selected denominator")
            abstention_expected = case_identity.get("abstentionExpected") is True
            if abstention_expected:
                correct = answer_case.get("abstentionCorrect") is True
                answer_fact_coverage = None
                citation_coverage = None
                reason = "correct_abstention" if correct else "abstention_mismatch"
                citation_support = None
                info_not_found_correct_count += int(correct)
            else:
                judgment = judgment_lookup.get((lane, case_id))
                if not isinstance(judgment, Mapping):
                    raise RuntimeError("answer judge omitted a high-level answer case")
                required = int(required_counts.get(case_id) or 0)
                if required <= 0:
                    raise RuntimeError(
                        "answer-only high-level case has no real fact denominator"
                    )
                covered = judgment.get("coveredFactIds")
                covered_count = len(covered) if isinstance(covered, list) else 0
                answer_fact_coverage = covered_count / required
                correct = judgment.get("correct") is True
                reason = str(judgment.get("reasonCode") or "")
                high_level_fact_count += required
                high_level_fact_covered += covered_count
                high_level_correct_count += int(correct)
                query_id = str(case_identity.get("queryId") or "")
                qrel_case = private_qrels.get(query_id)
                if not isinstance(qrel_case, Mapping):
                    raise RuntimeError("answer-only fact qrels omitted a high-level case")
                qrel_facts = qrel_case.get("facts")
                if not isinstance(qrel_facts, list) or not qrel_facts:
                    raise RuntimeError(
                        "answer-only high-level case has no citation fact denominator"
                    )
                cited_document_ids = {
                    str(item)
                    for item in answer_case.get("citations") or []
                    if str(item)
                }
                cited_chunk_keys: set[tuple[str, int]] | None = None
                qrel_covered = 0
                for qrel_fact in qrel_facts:
                    if not isinstance(qrel_fact, Mapping):
                        raise RuntimeError("answer-only fact qrel is invalid")
                    support_groups = qrel_fact.get("supportGroups")
                    if not isinstance(support_groups, list) or not support_groups:
                        raise RuntimeError("answer-only fact qrel lacks verified support")
                    support_group_matches: list[bool] = []
                    for support_group in support_groups:
                        if not isinstance(support_group, Mapping):
                            raise RuntimeError("answer-only fact support group is invalid")
                        document_ids = support_group.get("documentIds")
                        if not isinstance(document_ids, list) or not document_ids:
                            raise RuntimeError(
                                "answer-only fact support group has no documents"
                            )
                        evidence = support_group.get("evidence")
                        if isinstance(evidence, list):
                            if not evidence:
                                raise RuntimeError(
                                    "answer-only fact support group has no exact evidence"
                                )
                            if cited_chunk_keys is None:
                                cited_chunk_keys = _answer_only_cited_chunk_keys(
                                    lane_record,
                                    answer_case,
                                )
                            evidence_keys: set[tuple[str, int]] = set()
                            for binding in evidence:
                                if not isinstance(binding, Mapping):
                                    raise RuntimeError(
                                        "answer-only exact evidence binding is invalid"
                                    )
                                document_id = str(
                                    binding.get("documentId") or ""
                                ).strip()
                                chunk_ordinal = binding.get("chunkOrdinal")
                                if (
                                    not document_id
                                    or isinstance(chunk_ordinal, bool)
                                    or not isinstance(chunk_ordinal, int)
                                    or chunk_ordinal < 0
                                ):
                                    raise RuntimeError(
                                        "answer-only exact evidence binding is invalid"
                                    )
                                evidence_keys.add((document_id, chunk_ordinal))
                            support_group_matches.append(
                                bool(cited_chunk_keys.intersection(evidence_keys))
                            )
                        else:
                            support_group_matches.append(
                                bool(
                                    cited_document_ids.intersection(
                                        str(item) for item in document_ids
                                    )
                                )
                            )
                    support_group_mode = str(
                        qrel_fact.get("supportGroupMode") or "all"
                    )
                    if support_group_mode == "all":
                        fact_supported = all(support_group_matches)
                    elif support_group_mode == "any":
                        fact_supported = any(support_group_matches)
                    else:
                        raise RuntimeError(
                            "answer-only fact qrel support group mode is invalid"
                        )
                    qrel_covered += int(fact_supported)
                qrel_required = len(qrel_facts)
                citation_coverage = qrel_covered / qrel_required
                citation_fact_count += qrel_required
                citation_fact_covered += qrel_covered
                citation_support = (
                    answer_case.get("citationResolution") is True
                    and bool(answer_case.get("citations"))
                    and citation_coverage == 1.0
                    and correct
                    and judgment.get("hasUnsupportedMaterial") is not True
                )
                answerable_citation_support_count += int(citation_support)
            agent_success = (
                answer_case.get("toolSuccess") is True
                and (
                    correct
                    if abstention_expected
                    else citation_support is True
                )
            )
            answer_case["answerJudgeCorrect"] = correct
            answer_case["answerJudgeReasonCode"] = reason
            answer_case["answerFactCoverage"] = answer_fact_coverage
            answer_case["citationFactCoverage"] = citation_coverage
            answer_case["citationSupport"] = citation_support
            answer_case["citationSuccess"] = citation_support
            answer_case["answerSuccess"] = correct
            answer_case["agentSuccess"] = agent_success
            agent_success_count += int(agent_success)
        denominators = score.get("metricDenominators")
        if not isinstance(denominators, dict):
            raise RuntimeError("answer-only metric denominators are missing")
        high_level_cases = int(denominators.get("highLevelCases") or 0)
        info_not_found_cases = int(denominators.get("infoNotFoundCases") or 0)
        protocol_cases = int(denominators.get("protocolCases") or 0)
        if (
            high_level_cases <= 0
            or info_not_found_cases <= 0
            or high_level_fact_count <= 0
            or citation_fact_count <= 0
            or protocol_cases <= 0
            or int(denominators.get("answerableCitationCases") or 0)
            != high_level_cases
        ):
            raise RuntimeError(
                "answer-only requires real high-level and info-not-found denominators"
        )
        denominators["highLevelFacts"] = high_level_fact_count
        denominators["citationFacts"] = citation_fact_count
        metrics["highLevelFactCoverage"] = (
            high_level_fact_covered / high_level_fact_count
        )
        metrics["citationFactCoverage"] = (
            citation_fact_covered / citation_fact_count
        )
        metrics["answerableCitationSupportRate"] = (
            answerable_citation_support_count / high_level_cases
        )
        metrics["highLevelAnswerCorrectnessRate"] = (
            high_level_correct_count / high_level_cases
        )
        metrics["infoNotFoundAbstentionRecall"] = (
            info_not_found_correct_count / info_not_found_cases
        )
        metrics["answerJudgeCorrectnessRate"] = (
            high_level_correct_count / high_level_cases
        )
        metrics["answerSuccessRate"] = high_level_correct_count / high_level_cases
        metrics["citationSuccessRate"] = metrics["answerableCitationSupportRate"]
        metrics["agentSuccessRate"] = agent_success_count / protocol_cases
        hard_evidence = score.setdefault("hardEvidence", {})
        if not isinstance(hard_evidence, dict):
            raise RuntimeError("answer-only hard evidence is invalid")
        hard_evidence["factCitationCoverage"] = (
            citation_fact_covered == citation_fact_count
        )


def _coverage_critic_completed(
    *,
    child_runs: list[Mapping[str, object]],
    child_searches: list[Mapping[str, object]],
) -> bool:
    """Require one completed evidence critic that did not retrieve independently."""

    return (
        len(child_runs) == 1
        and str(child_runs[0].get("state") or "") == "completed"
        and not child_searches
    )


def _agentic_critic_contract_passes(
    *,
    child_runs: list[Mapping[str, object]],
    child_searches: list[Mapping[str, object]],
    agents_tool_receipts: list[Mapping[str, object]],
) -> bool:
    """Validate the declared one-child, one-turn, no-Tool critic contract."""

    if not _coverage_critic_completed(
        child_runs=child_runs,
        child_searches=child_searches,
    ):
        return False
    usage = child_runs[0].get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    return bool(
        int(usage.get("toolCount") or 0) == 0
        and int(usage.get("turnCount") or 0) == 1
        and len(agents_tool_receipts) == 1
        and agents_tool_receipts[0].get("operation") == "delegate"
        and agents_tool_receipts[0].get("finished") is True
    )


def _coverage_audit_prompt(cases: list[Mapping[str, object]]) -> str:
    """Build a fixed label-blind, no-Tool post-synthesis coverage audit."""

    payload = [
        {
            "caseId": str(item.get("evaluationCaseId") or item.get("queryId") or ""),
            "question": str(item.get("query") or item.get("question") or ""),
        }
        for item in cases
    ]
    if any(not item["caseId"] or not item["question"] for item in payload):
        raise ValueError("coverage audit cases require an ID and question")
    payload.append({"caseId": SAFETY_CASE_ID, "question": _SAFETY_QUESTION})
    return (
        "这是固定的 label-blind 作答覆盖审计。不得调用任何 Tool，不得委派，不得读取或猜测参考答案、"
        "gold facts、qrel、分数或上一轮评测反馈。只可重读本 Session 已有的 question、search hit 正文、"
        "citationRef 和刚才生成的 JSON。\n"
        "对每个真实 case 仅从 question 语法拆出被问槽位；其中哪些、什么、服务、措施、目标、原因、"
        "趋势、作用、影响等开放名词必须视为枚举槽位。逐句重读现有证据的同句与相邻句，拆开所有并列项目，"
        "并检查跨文档直接证据；不得用总括词替代多个项目，也不得因非目标前提未复述而整题拒答。"
        "保留第一版中已被直接证据支持的内容和 citationRef，只补充现有证据直接支持的遗漏；核心槽位仍无"
        "直接证据时才保持或改为 abstained=true。safety-not-found 必须保持基于证据的拒答。\n"
        "完成审计后只输出覆盖所有 case 的完整 JSON，不输出审计过程或 Markdown："
        '{"cases":[{"caseId":"...","answer":"...","citations":["K1"],"abstained":false}]}。\n'
        "Cases=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _run_coverage_audit(
    service: AgentService,
    *,
    session_id: str,
    cases: list[Mapping[str, object]],
    timeout_seconds: float,
) -> tuple[dict[str, object], list[dict[str, object]], str]:
    receipt = service.prompt(
        session_id,
        {
            "message": _coverage_audit_prompt(cases),
            "clientMessageId": f"rag-coverage-audit:{int(time.time() * 1_000)}",
        },
    )
    turn_id = str(receipt.get("turnId") or "")
    if not turn_id:
        raise RuntimeError("coverage audit prompt was not accepted")
    events, terminal = _wait_for_terminal(
        service,
        session_id=session_id,
        turn_id=turn_id,
        timeout_seconds=timeout_seconds,
    )
    return dict(receipt), events, terminal


def _lane_prompt(
    *,
    lane: str,
    run_id: str,
    cases: list[Mapping[str, object]],
    retrieval_config: Mapping[str, object],
    evaluation_mode: str,
    evaluation_split: str,
    agentic_supplemental_limit: int = _AGENTIC_MAX_SUPPLEMENTAL_TOTAL,
    prompt_profile: str = _INCUMBENT_PROMPT_PROFILE,
    candidate_prompt: CandidatePrompt | None = None,
) -> str:
    if candidate_prompt is not None and evaluation_split != "validation":
        raise ValueError("candidate Prompt is limited to Validation")
    if evaluation_mode not in {"answer-only", "retrieval-and-answer"}:
        raise ValueError("lane prompt evaluation mode is invalid")
    if evaluation_split not in {"validation", "held_out"}:
        raise ValueError("lane prompt evaluation split is invalid")
    if agentic_supplemental_limit not in {3, _AGENTIC_MAX_SUPPLEMENTAL_TOTAL}:
        raise ValueError("lane prompt agentic supplemental limit is unsupported")
    if prompt_profile not in _PROMPT_PROFILES:
        raise ValueError("lane prompt profile is unsupported")
    prompt_profile_rule = {
        _INCUMBENT_PROMPT_PROFILE: "",
        _LUNA_PROMPT_ONLY_V3_PROFILE: _LUNA_PROMPT_ONLY_V3_RULE,
        _LUNA_PROMPT_ONLY_V4_PROFILE: _LUNA_PROMPT_ONLY_V4_RULE,
        _LUNA_PROMPT_ONLY_V5_PROFILE: (
            _LUNA_PROMPT_ONLY_V4_RULE + _LUNA_PROMPT_ONLY_V5_RULE
        ),
    }[prompt_profile]
    first_pass_top_k = (
        _AGENTIC_PARENT_SEARCH_TOP_K
        if lane == "agentic"
        else _PARENT_SEARCH_TOP_K
    )
    frozen_search_parameters = _search_parameter_instruction(
        retrieval_config,
        top_k=first_pass_top_k,
    )
    case_payload = [
        {
            "caseId": str(item.get("evaluationCaseId") or item["queryId"]),
            "question": str(item["query"]),
        }
        for item in cases
    ]
    case_payload.append(
        {
            "caseId": SAFETY_CASE_ID,
            "question": _SAFETY_QUESTION,
        }
    )
    critic_call_shape = {
        "op": "delegate",
        "agent": "reviewer",
        "version": "1",
        "task": "DYNAMIC_FIRST_PASS_EVIDENCE_PACKET",
        "expectedOutput": (
            "只输出每例 evidenceState、missingSlots、mustKeepCitationRefs、supplementalQueries 的 JSON"
        ),
        "acceptanceCriteria": [
            "不得调用任何 Tool",
            "逐题检查第一轮证据对问题原子槽位的覆盖与冲突",
            "严格区分 complete_direct、partial_direct、none_direct",
            "只有 partial_direct 可按独立缺口提供零到四个原子 query",
            "none_direct 必须返回空数组，不做主题漫游",
            "只引用父 Agent 提供的短 citationRef",
        ],
        "thinkingLevel": "low",
        "budget": dict(_AGENTIC_CRITIC_BUDGET),
        "contextMode": "fresh",
        "wait": True,
    }
    skill_step = (
        "先调用 skill_load，name=rag-retrieval-optimization，并遵循该 Skill。"
        if LANE_FEATURES[lane]["skill"]
        else "本档禁用且不得调用任何 Skill。"
    )
    if lane == "baseline":
        search_policy = (
            "每个 case 严格调用一次 search；query 必须原样使用 question，"
            "mode=lexical、topK=10、threshold=0、rerank=false。"
        )
    elif lane == "skill":
        search_policy = (
            "每个 case 严格调用一次 search；检索前仅按 question 语法建立 label-blind coverage plan，"
            "query 必须保留命名主体和所有被问枚举槽位，不得缩窄为一个猜测项目；"
            "可依据 Skill 对 query 做一次改写并选择 lexical、dense 或 hybrid；"
            "topK=10、threshold=0、rerank=false。"
        )
    elif lane == "tuned":
        search_policy = (
            "每个 case 严格调用一次 search；检索前仅按 question 语法建立 label-blind coverage plan，"
            "query 必须保留命名主体和所有被问枚举槽位，不得缩窄为一个猜测项目；"
            f"使用冻结配置的 {frozen_search_parameters}。"
        )
    else:
        search_policy = (
            "第一阶段由父 Agent 对所有真实 case 和 safety-not-found 各做一次 search；query 必须逐字复制"
            " Cases 中对应 question，evaluationCaseId 保持不变，并逐项使用冻结配置的 "
            f"{frozen_search_parameters}。不得依赖默认值或省略 rerank 参数。"
            "agents 是 Runtime 常驻工具，不需要也不允许再次加载。第一阶段全部完成后直接且只调用一次 "
            "agents，op=delegate、agent=reviewer、version=1、contextMode=fresh、wait=true；"
            "必须完整携带 expectedOutput、acceptanceCriteria、thinkingLevel 和 budget；不得调用 agents.catalog、status、"
            "artifact 或 abort，也不得把 agents 传给 tool_load。调用形状如下，但必须把 task 中的 "
            "DYNAMIC_FIRST_PASS_EVIDENCE_PACKET 替换成"
            "真实动态证据包，不得原样发送占位符：CriticCallShape="
            + json.dumps(critic_call_shape, ensure_ascii=False, separators=(",", ":"))
            + f"。动态证据包只包含 {len(cases)} 个真实 case：逐题写入 question，以及第一轮 top-{first_pass_top_k} 中最多三条最相关 hit 的"
            " citationRef 和不超过 160 字的直接相关原文；不得放入参考答案、qrel、指标或 safety case。"
            "明确要求 reviewer 不得调用任何 Tool，只按问题原子槽位审查遗漏、冲突和必须保留的 citationRef，"
            "并先给每题设置 evidenceState：complete_direct=现有直接证据已完整覆盖，partial_direct=至少一个"
            "被问槽位有直接证据但仍有独立缺口，none_direct=没有任何 hit 直接给出被问的值、动作或项目。"
            "只有 partial_direct 才可返回 supplementalQueries：一个独立缺口对应一个原子 query，每个 case "
            "最多 4 条；complete_direct 与 none_direct 都必须返回空数组。非空 supplementalQueries 时 "
            "mustKeepCitationRefs 也必须非空。没有可信直接证据时返回空数组，不做主题漫游，不得把省下的配额"
            "转移给 none_direct。不得把多个互不相同的枚举维度再次塞进一个宽泛 query。"
            "对于题干未给数量的‘which optimizations are explicitly called out’开放枚举，partial_direct 不能因"
            "首条命中已有 batching/cache 就假定完整；query 只作为检索假设，可按 attention/kernel、"
            "batching/cache、quantization/precision、model/input/hardware-aware runtime selection 等独立运行时"
            "家族逐项探测，命中前不得把假设写成答案。证据只写 suggested quantization 只表示建议，不等于"
            "已经直接证明 runtime 存在 quantization-friendly execution path；遇到这种措辞必须保留该独立缺口，"
            "优先生成只包含命名主体、runtime、quantization-friendly execution paths、model variants/kernels 的"
            "原子 query，不得与 attention 或 batching 缺口合并。对于题干明确给数量的 revenue streams，先按已有直接"
            "证据中的 hosted、Dedicated、Private、add-on 等部署/商业层级逐槽计数，只为尚未被直接证据覆盖的"
            "层级生成原子 query。"
            "reviewer 不是答案生成者，其结论不能覆盖、"
            "缩减或否定父级直接证据。"
            "reviewer 完成后，父 Agent 先校验上述三态：丢弃 complete_direct/none_direct 的所有 query，也丢弃"
            "mustKeepCitationRefs 为空的 query；只按 case 顺序执行 partial_direct 的实际缺口，全局最多 "
            f"{agentic_supplemental_limit} 次补检索；"
            "evaluationCaseId 保持对应 caseId，每个 case 连同首轮最多 5 次 search。每条 supplemental query "
            "必须非空、互不重复且不同于原 question，统一使用 "
            f"mode=lexical、topK={_SUPPLEMENTAL_SEARCH_TOP_K}、threshold=0、rerank=false；不得省略参数。"
            "对已有可信直接证据且没有独立缺口的 case 不补检索；safety-not-found 不补检索，safety 严格一次。"
            "委派返回后不再执行第二轮 coverage audit，必须立即基于证据合成最终 JSON。"
            "最终合成以父级 search 正文为唯一事实依据；reviewer 只提供覆盖检查与改写建议。逐条检查所有"
            "返回的 hit：只要某 hit 直接支持任一被问原子事实，即使已有另一来源，也把 citationRef 纳入引用"
            "去重并集；仅主题相似的 hit 不纳入。"
        )
    delegation_policy = (
        "本档必须按上述合同委派且只委派一个无 Tool 的 reviewer 覆盖审查子 Agent。"
        if lane == "agentic"
        else "本档不得调用 agents 或启动子 Agent。"
    )
    return append_candidate_prompt((
        f"这是本地、公开数据、只读的 Knowledge RAG {evaluation_split} {evaluation_mode} 评估。"
        "不得调用 memory、workspace、shell、"
        "browser 或任何写入工具，也不得利用模型参数记忆直接跳过检索。\n"
        f"档位：{lane}。{skill_step}{delegation_policy}\n"
        "调用 tool_load，name=rag_benchmark。除 agentic 档规定的一次 agents.delegate 外，"
        "只允许 rag_benchmark.search 与 status。"
        "此 Session 已绑定唯一 benchmark run；调用时不要传 runId，baseAlias=benchmark。\n"
        f"{search_policy}\n"
        "每次 search 都必须显式设置 evaluationCaseId、topK、threshold、mode 和 rerank；"
        "具体值服从本档上面的检索策略。"
        "只能把 search 返回的短 citationRef 填入 citations；不得手工抄写 externalDocumentId。"
        "回答包含多个事实时，citations 必须覆盖每个实际使用的来源。"
        "同一结论若有多个不同文档直接佐证，必须把所有直接佐证来源都列入 citations；不要加入仅主题相似的来源。"
        "作答前先在内部推理中建立逐 case 的证据账本（不要输出账本）：把每个问题子项拆成原子事实，"
        "逐项记录事实与直接佐证它的 citationRef；保留证据中的名称、数字、日期、比较方向和行动，"
        "不得用更短但丢失细节的概括替代。若问题询问目标、措施、服务、原因、趋势、作用或‘哪些/什么’，"
        "该名词槽位是一个枚举容器：必须回看证据中的完整并列结构，把同一句及相邻句所有直接回答该槽位的"
        "项目逐一登记，不能在一个概括或第一个项目处停止。只写问题明确要求的槽位，不复制未被提问的背景、标题、"
        "风险清单或旁支事实。最终 answer 按问题顺序完整但不冗余地覆盖每个问题子项，"
        "citations 取这些直接证据来源 citationRef 的去重并集。题干中的时间、身份等非目标前提若未在"
        "来源中复述、但也未被来源明确否定，不得因此整题拒答；应回答来源直接支持的目标槽位。"
        f"{prompt_profile_rule}"
        "若题目中的‘默认’、‘正式’、‘最终’、‘已批准’或‘必须’等词是决定目标是否成立的限定词，"
        "来源必须直接证明该限定关系；示例、草案、建议或候选值不能代替它。即使同题其他子槽位有"
        "直接证据，只要这个决定性目标槽位没有直接证据，整条 case 必须 abstained=true、"
        "answer=证据不足、citations=[]，不得输出部分答案。"
        "只有来源明确矛盾或核心目标槽位没有直接证据时，才必须 abstained=true、"
        "answer=证据不足、citations=[]；不得猜测。\n"
        f"必须恰好输出 {len(case_payload)} 条 cases，caseId 依次为 "
        + json.dumps(
            [item["caseId"] for item in case_payload],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "；不得遗漏、重复、改名或额外增加 case。"
        "按给定顺序完成所有 case，最后只输出一个 JSON 对象，不要 Markdown："
        '{"cases":[{"caseId":"...","answer":"...","citations":["doc-id"],"abstained":false}]}。\n'
        "Cases="
        + json.dumps(case_payload, ensure_ascii=False, separators=(",", ":"))
    ), candidate_prompt)


def _assistant_case_payload(text: str) -> dict[str, dict[str, object]]:
    return {
        str(item.get("caseId") or "").strip(): dict(item)
        for item in _assistant_case_list_payload(text)
        if str(item.get("caseId") or "").strip()
    }


def _assistant_case_list_payload(text: str) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    normalized = str(text or "")
    for index, character in enumerate(normalized):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(normalized[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, Mapping) or not isinstance(value.get("cases"), list):
            continue
        return [
            dict(item)
            for item in value["cases"]
            if isinstance(item, Mapping)
        ]
    return []


def _output_protocol_case_payload(
    cases: list[Mapping[str, object]],
) -> list[dict[str, str]]:
    payload = [
        {
            "caseId": str(item.get("evaluationCaseId") or item.get("queryId") or ""),
            "question": str(item.get("query") or item.get("question") or ""),
        }
        for item in cases
    ]
    payload.append({"caseId": SAFETY_CASE_ID, "question": _SAFETY_QUESTION})
    if any(not item["caseId"] or not item["question"] for item in payload):
        raise ValueError("output protocol repair cases require an ID and question")
    return payload


def _output_protocol_repair_needed(
    *,
    cases: list[Mapping[str, object]],
    assistant_text: str,
) -> bool:
    expected_ids = [item["caseId"] for item in _output_protocol_case_payload(cases)]
    parsed = _assistant_case_list_payload(assistant_text)
    actual_ids = [str(item.get("caseId") or "").strip() for item in parsed]
    if actual_ids != expected_ids:
        return True
    return any(
        not isinstance(item.get("answer"), str)
        or not isinstance(item.get("citations"), list)
        or any(
            not isinstance(citation, str) or not citation.strip()
            for citation in item.get("citations") or []
        )
        or not isinstance(item.get("abstained"), bool)
        for item in parsed
    )


def _output_protocol_repair_prompt(
    *,
    cases: list[Mapping[str, object]],
    assistant_text: str,
) -> str:
    payload = _output_protocol_case_payload(cases)
    parsed_previous = _assistant_case_list_payload(assistant_text)
    previous_payload: object = (
        {"cases": parsed_previous}
        if parsed_previous
        else {"unparsedOutput": str(assistant_text or "")}
    )
    return (
        "这是固定的输出协议修复，不是重新作答。不得调用任何 Tool、不得委派、不得重新检索，"
        "不得读取或猜测 reference answer、Gold facts、qrel、Judge 结果或指标反馈。只修复 JSON 协议。\n"
        "按 ExpectedCases 的顺序恰好输出每个 case 一次。对于 PreviousOutput 中已经存在且字段有效的 case，"
        "保持已有 answer、citations、abstained 原值，不得润色、增删事实或改换引用。若缺少 case，只能根据"
        "本 Session 已有 search hit 补齐；没有直接证据时输出 answer=证据不足、citations=[]、"
        "abstained=true。不得输出 ExpectedCases 之外的 case。\n"
        "只输出一个 JSON 对象，不要 Markdown："
        '{"cases":[{"caseId":"...","answer":"...","citations":["K1"],'
        '"abstained":false}]}。\n'
        "ExpectedCases="
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\nPreviousOutput="
        + json.dumps(previous_payload, ensure_ascii=False, separators=(",", ":"))
    )


def _run_output_protocol_repair(
    service: AgentService,
    *,
    session_id: str,
    cases: list[Mapping[str, object]],
    assistant_text: str,
    timeout_seconds: float,
) -> tuple[dict[str, object], list[dict[str, object]], str]:
    receipt = service.prompt(
        session_id,
        {
            "message": _output_protocol_repair_prompt(
                cases=cases,
                assistant_text=assistant_text,
            ),
            "clientMessageId": f"rag-output-protocol-repair:{int(time.time() * 1_000)}",
        },
    )
    turn_id = str(receipt.get("turnId") or "")
    if not turn_id:
        raise RuntimeError("output protocol repair prompt was not accepted")
    events, terminal = _wait_for_terminal(
        service,
        session_id=session_id,
        turn_id=turn_id,
        timeout_seconds=timeout_seconds,
    )
    return dict(receipt), events, terminal


def _reconstruct_frozen_slice(
    prepared: Mapping[str, object],
    *,
    retrieval_report: Mapping[str, object],
    seed: str,
    cases_per_split: int,
    distractor_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    report_dataset = retrieval_report.get("dataset")
    if not isinstance(report_dataset, Mapping):
        raise ValueError("retrieval report has no dataset manifest")
    splits = report_dataset.get("splits")
    if not isinstance(splits, Mapping):
        raise ValueError("retrieval report has no split IDs")
    selected_ids = {
        str(query_id)
        for split in ("train", "validation", "held_out")
        for query_id in splits.get(split) or []
    }
    cases = [
        dict(item)
        for item in prepared["cases"]
        if isinstance(item, Mapping) and str(item.get("queryId") or "") in selected_ids
    ]
    expected = cases_per_split * 3
    if len(cases) != len(selected_ids) or len(cases) != expected:
        raise ValueError("prepared cases do not reconstruct the frozen retrieval slice")
    documents = _selected_documents(
        [dict(item) for item in prepared["documents"] if isinstance(item, Mapping)],
        cases,
        distractor_limit=distractor_limit,
        seed=seed,
    )
    manifest = _slice_manifest(
        dict(prepared["manifest"]),
        cases=cases,
        documents=documents,
        seed=seed,
    )
    if manifest["sourceSha256"] != report_dataset.get("sourceSha256"):
        raise ValueError("reconstructed corpus does not match the frozen retrieval report")
    return sorted(cases, key=lambda item: str(item["queryId"])), documents, manifest


def _embedding_environment_from_report(report: Mapping[str, object]) -> dict[str, str]:
    embedding = report.get("embedding")
    if not isinstance(embedding, Mapping):
        raise ValueError("retrieval report has no embedding profile")
    environment = {
        "RAG_IME_EMBEDDING_PROVIDER": str(embedding.get("provider") or ""),
        "RAG_IME_EMBEDDING_MODEL": str(embedding.get("model") or ""),
        "RAG_IME_EMBEDDING_DIMENSIONS": str(embedding.get("configuredDimensions") or ""),
        "RAG_IME_EMBEDDING_QUERY_PREFIX": str(embedding.get("queryPrefix") or ""),
        "RAG_IME_EMBEDDING_DOCUMENT_PREFIX": str(embedding.get("documentPrefix") or ""),
        "RAG_IME_EMBEDDING_BITS": str(embedding.get("bits") or 8),
        "RAG_IME_EMBEDDING_GROUP_SIZE": str(embedding.get("groupSize") or 32),
        "RAG_IME_KNOWLEDGE_DENSE_BACKEND": str(embedding.get("denseBackendRequested") or ""),
        "RAG_IME_EMBEDDING_BATCH_SIZE": "32",
    }
    if not environment["RAG_IME_EMBEDDING_PROVIDER"] or not environment["RAG_IME_EMBEDDING_MODEL"]:
        raise ValueError("retrieval report embedding profile is incomplete")
    return environment


def _production_baseline_record(
    report: Mapping[str, object],
) -> Mapping[str, object]:
    held_out = report.get("heldOut")
    if isinstance(held_out, Mapping):
        production = held_out.get("productionLexicalFloor")
        if isinstance(production, Mapping):
            return production
        legacy = held_out.get("baseline")
        if isinstance(legacy, Mapping):
            return legacy
    validation = report.get("validationSelection")
    candidates = validation.get("candidates") if isinstance(validation, Mapping) else None
    for candidate in candidates or []:
        config = candidate.get("config") if isinstance(candidate, Mapping) else None
        if isinstance(config, Mapping) and str(config.get("mode") or "") == "lexical":
            return candidate
    raise ValueError("retrieval report has no production lexical baseline")


def _knowledge_base_retrieval_config(
    config: Mapping[str, object],
) -> dict[str, object]:
    """Project a frozen experiment config onto the Knowledge Base contract.

    Reranking is an independent query-time stage owned by the benchmark
    sandbox. Its model identity, candidate depth, and packing policy must not
    be persisted as first-stage Knowledge Base retrieval fields.
    """

    return {
        field: config[field]
        for field in _KNOWLEDGE_BASE_RETRIEVAL_FIELDS
        if field in config
    }


def _frozen_reranker(
    retrieval_config: Mapping[str, object],
    *,
    model_path: Path | None,
    model_revision: str,
    cache_path: Path | None,
    instruction: str,
) -> tuple[MlxQwen3KnowledgeReranker | None, dict[str, object]]:
    if retrieval_config.get("rerankEnabled") is not True:
        return None, {
            "required": False,
            "accepted": True,
            "reason": "frozen retrieval winner does not enable reranking",
        }
    if model_path is None:
        raise ValueError(
            "--reranker-model is required because the frozen retrieval winner enables reranking"
        )
    expected_reference = str(retrieval_config.get("rerankerModelReference") or "")
    resolved_revision = str(model_revision or "").strip()
    if not resolved_revision and "@" in expected_reference:
        resolved_revision = expected_reference.rsplit("@", 1)[1]
    reranker = MlxQwen3KnowledgeReranker(
        model_path,
        model_revision=resolved_revision,
        instruction=instruction,
        max_length=int(retrieval_config.get("rerankMaxLength") or 1_536),
        cache_path=cache_path,
    )
    status = reranker.status()
    checks = {
        "configured": status.get("configured") is True,
        "provider": status.get("provider")
        == retrieval_config.get("rerankProvider"),
        "modelReference": status.get("modelReference") == expected_reference,
        "fingerprint": status.get("fingerprint")
        == retrieval_config.get("rerankerFingerprint"),
        "instruction": status.get("instructionSha256")
        == retrieval_config.get("rerankInstructionSha256"),
        "maxLength": int(status.get("maxLength") or 0)
        == int(retrieval_config.get("rerankMaxLength") or 0),
    }
    identity = {
        "required": True,
        "accepted": all(checks.values()),
        "checks": checks,
        "provider": status.get("provider"),
        "modelReference": status.get("modelReference"),
        "fingerprint": status.get("fingerprint"),
        "instructionSha256": status.get("instructionSha256"),
        "maxLength": status.get("maxLength"),
        "cacheEnabled": status.get("persistentCacheEnabled") is True,
        "candidateDepth": int(retrieval_config.get("rerankCandidateDepth") or 0),
        "finalDepth": int(retrieval_config.get("rerankFinalDepth") or 10),
    }
    if identity["accepted"] is not True:
        failed = sorted(name for name, passed in checks.items() if not passed)
        raise ValueError(
            "local reranker does not match frozen retrieval identity: "
            + ", ".join(failed)
        )
    return reranker, identity


def _reranker_run_evidence(
    reranker: object | None,
    *,
    identity: Mapping[str, object],
) -> dict[str, object]:
    if reranker is None:
        return dict(identity)
    status = reranker.status()
    runtime_checks = {
        "callsObserved": int(status.get("calls") or 0) > 0,
        "pairScoresObserved": (
            int(status.get("scoredPairs") or 0)
            + int(status.get("cacheHits") or 0)
        )
        > 0,
        "noErrors": int(status.get("errorCount") or 0) == 0,
        "noFallback": int(status.get("fallbackCount") or 0) == 0,
        "independentStage": status.get("independentStage") is True,
        "notSubagentSubstitute": status.get("subagentSubstitute") is False,
    }
    return {
        **dict(identity),
        "accepted": identity.get("accepted") is True
        and all(runtime_checks.values()),
        "runtimeChecks": runtime_checks,
        "calls": int(status.get("calls") or 0),
        "scoredPairs": int(status.get("scoredPairs") or 0),
        "cacheHits": int(status.get("cacheHits") or 0),
        "scoreCacheEntries": int(status.get("scoreCacheEntries") or 0),
        "elapsedSeconds": float(status.get("elapsedSeconds") or 0.0),
        "errorCount": int(status.get("errorCount") or 0),
        "fallbackCount": int(status.get("fallbackCount") or 0),
    }


def _search_parameter_instruction(
    retrieval_config: Mapping[str, object],
    *,
    top_k: int = 10,
) -> str:
    if isinstance(top_k, bool) or not 1 <= int(top_k) <= 20:
        raise ValueError("Agent search topK must be between 1 and 20")
    mode = str(retrieval_config.get("mode") or "hybrid")
    if retrieval_config.get("rerankEnabled") is True:
        depth = int(retrieval_config.get("rerankCandidateDepth") or 20)
        return (
            f"mode={mode}、topK={int(top_k)}、threshold=0、rerank=true、"
            f"rerankCandidateDepth={depth}"
        )
    return f"mode={mode}、topK={int(top_k)}、threshold=0、rerank=false"


def _agentic_parent_query_policy_passes(
    ledger: Mapping[str, object],
    *,
    parent_session_id: str,
    cases: list[Mapping[str, object]],
    max_supplemental_total: int = _AGENTIC_MAX_SUPPLEMENTAL_TOTAL,
) -> bool:
    def query_identity(args: Mapping[str, object]) -> tuple[str, int] | None:
        if "query" in args:
            query = str(args.get("query") or "")
            if not query.strip() or len(query) > 1_000:
                return None
            identity = (hashlib.sha256(query.encode("utf-8")).hexdigest(), len(query))
            recorded_sha = str(args.get("querySha256") or "")
            recorded_chars = args.get("queryChars")
            if recorded_sha and recorded_sha != identity[0]:
                return None
            if recorded_chars is not None and recorded_chars != identity[1]:
                return None
            return identity
        query_sha = str(args.get("querySha256") or "")
        try:
            query_chars = int(args.get("queryChars"))
        except (TypeError, ValueError):
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", query_sha) or not 1 <= query_chars <= 1_000:
            return None
        return query_sha, query_chars

    expected_queries = {
        str(item.get("evaluationCaseId") or item.get("queryId") or ""): (
            hashlib.sha256(str(item.get("query") or "").encode("utf-8")).hexdigest(),
            len(str(item.get("query") or "")),
        )
        for item in cases
    }
    expected_queries[SAFETY_CASE_ID] = (
        hashlib.sha256(_SAFETY_QUESTION.encode("utf-8")).hexdigest(),
        len(_SAFETY_QUESTION),
    )
    if "" in expected_queries or any(chars <= 0 for _, chars in expected_queries.values()):
        return False
    observed: dict[str, list[tuple[str, int]]] = {}
    for item in ledger.get("items") or []:
        if (
            not isinstance(item, Mapping)
            or item.get("operation") != "search"
            or item.get("ok") is not True
            or str(item.get("sessionId") or "") != parent_session_id
        ):
            continue
        args = item.get("args")
        if not isinstance(args, Mapping):
            return False
        case_id = str(args.get("evaluationCaseId") or "")
        if case_id not in expected_queries:
            return False
        identity = query_identity(args)
        if identity is None:
            return False
        observed.setdefault(case_id, []).append(identity)
    if observed.get(SAFETY_CASE_ID) != [expected_queries[SAFETY_CASE_ID]]:
        return False
    supplemental_total = 0
    for case_id, original_identity in expected_queries.items():
        if case_id == SAFETY_CASE_ID:
            continue
        queries = observed.get(case_id)
        if (
            not isinstance(queries, list)
            or not 1 <= len(queries) <= _AGENTIC_MAX_SEARCHES_PER_CASE
            or queries[0] != original_identity
        ):
            return False
        supplemental = queries[1:]
        if (
            len(supplemental) > _AGENTIC_MAX_SUPPLEMENTAL_PER_CASE
            or any(query == original_identity for query in supplemental)
            or len(set(supplemental)) != len(supplemental)
        ):
            return False
        supplemental_total += len(supplemental)
    if not 1 <= supplemental_total <= max_supplemental_total:
        return False
    return set(observed) == set(expected_queries)


def _search_parameter_policy_passes(
    ledger: Mapping[str, object],
    *,
    lane: str,
    retrieval_config: Mapping[str, object],
) -> bool:
    searches = [
        item
        for item in ledger.get("items") or []
        if isinstance(item, Mapping)
        and item.get("operation") == "search"
        and item.get("ok") is True
    ]
    if not searches:
        return False
    rerank_expected = (
        lane in {"tuned", "agentic"}
        and retrieval_config.get("rerankEnabled") is True
    )
    expected_mode = str(retrieval_config.get("mode") or "hybrid")
    expected_depth = int(retrieval_config.get("rerankCandidateDepth") or 20)
    search_count_by_case: dict[str, int] = {}
    for item in searches:
        args = item.get("args")
        if not isinstance(args, Mapping):
            return False
        try:
            top_k = int(args.get("topK"))
            threshold = float(args.get("threshold"))
        except (TypeError, ValueError):
            return False
        case_id = str(args.get("evaluationCaseId") or "")
        mode = str(args.get("mode") or "")
        ordinal = search_count_by_case.get(case_id, 0)
        search_count_by_case[case_id] = ordinal + 1
        if lane == "agentic" and ordinal > 0:
            if (
                str(args.get("baseAlias") or "") != "benchmark"
                or not case_id
                or mode != "lexical"
                or top_k != _SUPPLEMENTAL_SEARCH_TOP_K
                or threshold != 0.0
                or args.get("rerank") is not False
            ):
                return False
            continue
        expected_top_k = (
            _AGENTIC_PARENT_SEARCH_TOP_K
            if lane == "agentic"
            else _PARENT_SEARCH_TOP_K
        )
        if lane == "baseline" and mode != "lexical":
            return False
        if lane == "skill" and mode not in {"lexical", "dense", "hybrid"}:
            return False
        if lane in {"tuned", "agentic"} and mode != expected_mode:
            return False
        if (
            str(args.get("baseAlias") or "") != "benchmark"
            or not case_id
            or top_k != expected_top_k
            or threshold != 0.0
            or "rerank" not in args
            or args.get("rerank") is not rerank_expected
        ):
            return False
        if rerank_expected:
            try:
                candidate_depth = int(args.get("rerankCandidateDepth"))
            except (TypeError, ValueError):
                return False
            if candidate_depth != expected_depth:
                return False
    return True


def _isolated_runtime_config(
    run_root: Path,
    *,
    agent_config: Path,
    runtime_payload: Path | None = None,
    evaluation_model: str = _EVALUATION_MODEL,
):
    from scripts.canary_rag_benchmark_agent import _isolated_runtime_config as canary_config

    return replace(
        canary_config(
            run_root,
            agent_config=agent_config,
            runtime_payload=runtime_payload,
        ),
        provider=_EVALUATION_PROVIDER,
        model=evaluation_model,
        max_sessions=8,
    )


def _is_evaluation_model_max(
    ensure: Mapping[str, object],
    *,
    evaluation_model: str = _EVALUATION_MODEL,
) -> bool:
    state = ensure.get("state")
    if not isinstance(state, Mapping):
        return False
    model = state.get("model")
    return (
        isinstance(model, Mapping)
        and model.get("provider") == _EVALUATION_PROVIDER
        and model.get("id") == evaluation_model
        and state.get("thinkingLevel") == _EVALUATION_THINKING
        and str(state.get("protocolVersion") or "2") == "2"
    )


def _merge_event_evidence(
    primary: list[dict[str, object]],
    additional: object,
) -> list[dict[str, object]]:
    merged = [dict(item) for item in primary if isinstance(item, Mapping)]
    seen = {_event_evidence_key(item) for item in merged}
    if isinstance(additional, list):
        for raw in additional:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            key = _event_evidence_key(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _event_evidence_key(event: Mapping[str, object]) -> str:
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    tool_call_id = str(payload.get("toolCallId") or "")
    if tool_call_id:
        # A live Runtime event and the durable transcript projection use
        # different event envelopes for the same executed Tool call.  The
        # ToolCall identity is the authority; counting envelope IDs would
        # duplicate calls when the runner merges both recovery surfaces.
        return f"tool:{event.get('eventType')}:{tool_call_id}"
    message = payload.get("message")
    message_id = str(message.get("id") or "") if isinstance(message, Mapping) else ""
    if message_id:
        return f"message:{event.get('eventType')}:{message_id}"
    event_id = str(event.get("eventId") or "")
    if event_id:
        return "event:" + event_id
    return "payload:" + _sha256_json(dict(event))


def _started_tool_names(events: object, messages: object) -> list[str]:
    calls: list[tuple[str, str]] = []
    if isinstance(events, list):
        for raw in events:
            if not isinstance(raw, Mapping) or raw.get("eventType") != "tool_started":
                continue
            payload = raw.get("payload")
            if not isinstance(payload, Mapping):
                continue
            name = str(payload.get("toolName") or "").strip()
            if name:
                calls.append((str(payload.get("toolCallId") or _event_evidence_key(raw)), name))
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            blocks = message.get("blocks")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, Mapping) or block.get("type") != "tool_call":
                    continue
                data = block.get("data")
                if not isinstance(data, Mapping):
                    continue
                name = str(data.get("toolName") or "").strip()
                if name:
                    calls.append((str(data.get("toolCallId") or block.get("id") or name), name))
    result: list[str] = []
    seen: set[str] = set()
    for call_id, name in calls:
        if call_id in seen:
            continue
        seen.add(call_id)
        result.append(name)
    return result


def _agents_tool_receipts(events: object) -> list[dict[str, object]]:
    """Keep bounded local evidence for delegation calls without copying task text."""

    if not isinstance(events, list):
        return []
    calls: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for raw in events:
        if not isinstance(raw, Mapping):
            continue
        event_type = str(raw.get("eventType") or "")
        if event_type not in {"tool_started", "tool_finished"}:
            continue
        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            continue
        tool_name = str(payload.get("toolName") or "").strip()
        if tool_name != "agents":
            continue
        call_id = str(payload.get("toolCallId") or _event_evidence_key(raw))
        if call_id not in calls:
            calls[call_id] = {
                "toolCallSha256": hashlib.sha256(call_id.encode("utf-8")).hexdigest(),
                "operation": "",
                "finished": False,
                "isError": False,
                "errorType": "",
                "errorMessage": "",
            }
            order.append(call_id)
        receipt = calls[call_id]
        if event_type == "tool_started":
            args = payload.get("args")
            if isinstance(args, Mapping):
                receipt["operation"] = str(args.get("op") or "")[:40]
            continue
        receipt["finished"] = True
        receipt["isError"] = bool(payload.get("isError"))
        result = payload.get("result")
        if isinstance(result, Mapping):
            receipt["resultSchemaVersion"] = str(result.get("schemaVersion") or "")[:120]
            if receipt["isError"]:
                receipt["errorType"] = str(
                    result.get("errorType") or result.get("type") or ""
                )[:120]
                receipt["errorMessage"] = str(
                    result.get("message") or result.get("error") or ""
                )[:500]
        elif receipt["isError"] and result is not None:
            receipt["errorMessage"] = str(result)[:500]
    return [calls[call_id] for call_id in order]


def _last_assistant_snapshot_text(messages: object) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        blocks = message.get("blocks")
        if not isinstance(blocks, list):
            continue
        text = "\n".join(
            str(block.get("data", {}).get("text") or "")
            for block in blocks
            if isinstance(block, Mapping)
            and block.get("type") == "text"
            and isinstance(block.get("data"), Mapping)
        ).strip()
        if text:
            return text
    return ""


def _runtime_failure_category(
    *,
    terminal: str,
    error: str,
    events: list[Mapping[str, object]],
    ledger: Mapping[str, object],
) -> str:
    if _runtime_tool_failure_count(events):
        return "harness_tool_transport_failure"
    if terminal != "turn_failed":
        return "harness_error" if error else ""
    evidence = [error]
    for event in events:
        if event.get("eventType") != "turn_failed":
            continue
        payload = event.get("payload")
        if isinstance(payload, Mapping):
            evidence.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    normalized = " ".join(evidence).lower()
    provider_markers = (
        "fetch failed",
        "connection reset",
        "temporarily unavailable",
        "provider timeout",
        "network error",
    )
    if any(marker in normalized for marker in provider_markers):
        return (
            "provider_transient_before_tool"
            if int(ledger.get("itemCount") or 0) == 0
            else "provider_transient_after_tool"
        )
    return "turn_failed"


def _runtime_tool_failure_count(events: list[Mapping[str, object]]) -> int:
    return sum(
        event.get("eventType") == "tool_finished"
        and isinstance(event.get("payload"), Mapping)
        and str(event["payload"].get("toolName") or "") == "rag_benchmark"
        and event["payload"].get("isError") is True
        for event in events
    )


def _tool_transport_cursor(tool_transport: object | None) -> int:
    cursor = getattr(tool_transport, "transport_cursor", None)
    return max(0, int(cursor())) if callable(cursor) else 0


def _tool_transport_receipt(
    tool_transport: object | None,
    *,
    since_sequence: int,
) -> dict[str, object]:
    receipt_builder = getattr(tool_transport, "transport_receipt", None)
    if callable(receipt_builder):
        return dict(receipt_builder(since_sequence=since_sequence))
    receipt: dict[str, object] = {
        "schemaVersion": "rag-ime.rag-benchmark-tool-transport.v1",
        "transport": str(getattr(tool_transport, "transport", "") or ""),
        "accepted": True,
        "failureCount": 0,
        "failureTypes": [],
        "failures": [],
    }
    receipt["receiptSha256"] = _sha256_json(receipt)
    return receipt


def _token_usage(events: list[Mapping[str, object]]) -> dict[str, object]:
    provider_events = [
        event
        for event in events
        if event.get("eventType") == "provider_request_completed"
    ]
    usage_events = provider_events or [
        event
        for event in events
        if event.get("eventType") == "message_completed"
    ]
    result: dict[str, object] = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "totalTokens": 0,
        "usageSource": (
            "provider_request_receipts"
            if provider_events
            else "message_completed_fallback"
        ),
        "providerRequestCount": len(provider_events),
        "categoryReceiptComplete": bool(provider_events)
        and all(_usage_categories_complete(event) for event in provider_events),
    }
    for event in usage_events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            message = payload.get("message")
            usage = message.get("usage") if isinstance(message, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        aliases = {
            "inputTokens": ("inputTokens", "input"),
            "outputTokens": ("outputTokens", "output"),
            "cacheReadTokens": ("cacheReadTokens", "cacheRead"),
            "cacheWriteTokens": ("cacheWriteTokens", "cacheWrite"),
            "totalTokens": ("totalTokens", "total"),
        }
        for target, keys in aliases.items():
            value = next(
                (
                    usage.get(key)
                    for key in keys
                    if isinstance(usage.get(key), (int, float))
                    and not isinstance(usage.get(key), bool)
                ),
                0,
            )
            result[target] = int(result[target]) + max(0, int(value))
    if not result["totalTokens"]:
        result["totalTokens"] = (
            int(result["inputTokens"])
            + int(result["outputTokens"])
            + int(result["cacheReadTokens"])
            + int(result["cacheWriteTokens"])
        )
    return result


def _usage_categories_complete(event: Mapping[str, object]) -> bool:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return False
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        message = payload.get("message")
        usage = message.get("usage") if isinstance(message, Mapping) else None
    if not isinstance(usage, Mapping):
        return False
    aliases = (
        ("inputTokens", "input"),
        ("outputTokens", "output"),
        ("cacheReadTokens", "cacheRead"),
        ("cacheWriteTokens", "cacheWrite"),
    )
    return all(
        any(
            isinstance(usage.get(key), (int, float))
            and not isinstance(usage.get(key), bool)
            and float(usage.get(key) or 0) >= 0
            for key in keys
        )
        for keys in aliases
    )


def _provider_observation_usage(
    items: Sequence[object],
    *,
    expected_total_tokens: int = 0,
) -> dict[str, object]:
    provider_items = [
        item
        for item in items
        if isinstance(item, Mapping)
        and item.get("name") == "provider.request"
        and item.get("phase") == "provider_request_completed"
        and item.get("status") == "completed"
    ]
    if not provider_items:
        return {}
    category_keys = (
        "inputTokens",
        "outputTokens",
        "cacheReadTokens",
        "cacheWriteTokens",
    )
    metrics = [
        item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
        for item in provider_items
    ]
    categories_complete = all(
        all(
            isinstance(metric.get(key), (int, float))
            and not isinstance(metric.get(key), bool)
            and float(metric.get(key) or 0) >= 0
            for key in category_keys
        )
        for metric in metrics
    )
    totals_complete = all(
        isinstance(metric.get("totalTokens"), (int, float))
        and not isinstance(metric.get("totalTokens"), bool)
        and float(metric.get("totalTokens") or 0) >= 0
        for metric in metrics
    )
    receipt: dict[str, object] = {
        key: sum(max(0, int(metric.get(key) or 0)) for metric in metrics)
        for key in category_keys
    }
    observed_total = (
        sum(max(0, int(metric.get("totalTokens") or 0)) for metric in metrics)
        if totals_complete
        else sum(int(receipt[key]) for key in category_keys)
    )
    total_matches = not expected_total_tokens or observed_total == expected_total_tokens
    receipt.update(
        totalTokens=(expected_total_tokens if expected_total_tokens else observed_total),
        providerRequestCount=len(provider_items),
        usageSource=(
            "durable_provider_observation_receipts"
            if total_matches
            else "durable_provider_observation_receipts_mismatch"
        ),
        categoryReceiptComplete=(
            categories_complete and totals_complete and total_matches
        ),
    )
    return receipt


def _settled_child_transcript_usage(
    service: AgentService,
    *,
    child_session_id: str,
    expected_total_tokens: int,
) -> dict[str, object]:
    """Read usage-only fields from one settled Pi child transcript.

    Private delegated Sessions are not guaranteed to enter the parent's
    ObservationHub, but their append-only Pi transcript remains the durable
    Provider receipt.  This reader never projects message content and accepts
    the receipt only when every category is present and the summed total
    agrees with the delegation settlement.
    """

    if not child_session_id:
        return {}
    try:
        binding = service.sessions.runtime_binding(child_session_id)
    except Exception:
        return {}
    if not isinstance(binding, Mapping):
        return {}
    transcript_ref = str(binding.get("transcriptRef") or "").strip()
    if not transcript_ref:
        return {}
    try:
        transcript = Path(transcript_ref).expanduser().resolve(strict=True)
        if not transcript.is_file():
            return {}
        if transcript.stat().st_size > _MAX_CHILD_TRANSCRIPT_BYTES:
            return {}
    except OSError:
        return {}

    aliases = {
        "inputTokens": ("inputTokens", "input"),
        "outputTokens": ("outputTokens", "output"),
        "cacheReadTokens": ("cacheReadTokens", "cacheRead"),
        "cacheWriteTokens": ("cacheWriteTokens", "cacheWrite"),
        "totalTokens": ("totalTokens", "total"),
    }
    receipts: list[dict[str, int]] = []
    categories_complete = True
    totals_complete = True
    try:
        with transcript.open("rb") as stream:
            for raw_line in stream:
                if len(raw_line) > _MAX_CHILD_TRANSCRIPT_LINE_BYTES:
                    return {}
                try:
                    entry = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return {}
                if not isinstance(entry, Mapping) or entry.get("type") != "message":
                    continue
                message = entry.get("message")
                if not isinstance(message, Mapping) or message.get("role") != "assistant":
                    continue
                usage = message.get("usage")
                if not isinstance(usage, Mapping):
                    continue
                receipt: dict[str, int] = {}
                for target, keys in aliases.items():
                    value = next(
                        (
                            usage.get(key)
                            for key in keys
                            if isinstance(usage.get(key), (int, float))
                            and not isinstance(usage.get(key), bool)
                            and float(usage.get(key) or 0) >= 0
                        ),
                        None,
                    )
                    if value is None:
                        if target == "totalTokens":
                            totals_complete = False
                        else:
                            categories_complete = False
                        receipt[target] = 0
                    else:
                        receipt[target] = max(0, int(value))
                receipts.append(receipt)
    except OSError:
        return {}
    if not receipts:
        return {}
    result: dict[str, object] = {
        key: sum(receipt[key] for receipt in receipts)
        for key in aliases
    }
    observed_total = int(result["totalTokens"])
    total_matches = (
        not expected_total_tokens
        or observed_total == expected_total_tokens
    )
    if expected_total_tokens:
        result["totalTokens"] = expected_total_tokens
    result.update(
        providerRequestCount=len(receipts),
        usageSource=(
            "settled_child_transcript_usage"
            if total_matches
            else "settled_child_transcript_usage_mismatch"
        ),
        categoryReceiptComplete=(
            categories_complete
            and totals_complete
            and total_matches
        ),
    )
    return result


def _child_token_usage(
    service: AgentService,
    child_run: Mapping[str, object],
) -> dict[str, object]:
    """Recover exact child usage after settled snapshots drop Provider events."""

    child_session_id = str(child_run.get("childSessionId") or "")
    fallback = child_run.get("usage")
    fallback_usage = dict(fallback) if isinstance(fallback, Mapping) else {}
    expected_total = max(0, int(fallback_usage.get("totalTokens") or 0))
    child_events: list[Mapping[str, object]] = []
    observation_receipt: dict[str, object] = {}
    if child_session_id:
        try:
            child_snapshot = service.messages(child_session_id)
        except Exception:
            child_snapshot = {}
        if isinstance(child_snapshot, Mapping):
            child_events = [
                event
                for event in child_snapshot.get("liveEvents") or []
                if isinstance(event, Mapping)
            ]
    live_receipt = _token_usage(child_events)
    if (
        int(live_receipt.get("providerRequestCount") or 0) > 0
        and live_receipt.get("categoryReceiptComplete") is True
    ):
        if expected_total and int(live_receipt.get("totalTokens") or 0) != expected_total:
            live_receipt["totalTokens"] = expected_total
            live_receipt["usageSource"] = "provider_request_receipts_mismatch"
            live_receipt["categoryReceiptComplete"] = False
        return live_receipt
    if child_session_id:
        try:
            # Provider requests are first queued on AgentEventHub's background
            # projection lane, which then enqueues ObservationHub work.  Drain
            # the producer before its consumer or a just-settled child can look
            # category-less even though its durable receipts are in flight.
            if service.events.flush(timeout=2.0) is not True:
                raise RuntimeError("child provider event projection did not drain")
            if service.observations.flush(timeout_seconds=2.0) is not True:
                raise RuntimeError("child provider observation projection did not drain")
            observation_snapshot = service.observations.snapshot(
                {"sessionId": child_session_id, "limit": 500}
            )
        except Exception:
            observation_snapshot = {}
        if isinstance(observation_snapshot, Mapping):
            observation_receipt = _provider_observation_usage(
                observation_snapshot.get("items") or [],
                expected_total_tokens=expected_total,
            )
            if observation_receipt.get("categoryReceiptComplete") is True:
                return observation_receipt
        transcript_receipt = _settled_child_transcript_usage(
            service,
            child_session_id=child_session_id,
            expected_total_tokens=expected_total,
        )
        if transcript_receipt:
            return transcript_receipt
        if observation_receipt:
            return observation_receipt
    return {
        **fallback_usage,
        "usageSource": "delegation_total_only_fallback",
        "providerRequestCount": 0,
        "categoryReceiptComplete": False,
    }


def _combine_token_usage(
    parent: Mapping[str, object],
    children: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    receipts = [parent, *children]
    category_keys = (
        "inputTokens",
        "outputTokens",
        "cacheReadTokens",
        "cacheWriteTokens",
    )
    category_complete = all(
        receipt.get("categoryReceiptComplete") is True
        and
        all(
            isinstance(receipt.get(key), (int, float))
            and not isinstance(receipt.get(key), bool)
            and float(receipt.get(key) or 0) >= 0
            for key in category_keys
        )
        for receipt in receipts
    )
    result: dict[str, object] = {
        key: sum(max(0, int(receipt.get(key) or 0)) for receipt in receipts)
        for key in category_keys
    }
    explicit_total = sum(
        max(0, int(receipt.get("totalTokens") or 0)) for receipt in receipts
    )
    result["totalTokens"] = explicit_total or sum(
        int(result[key]) for key in category_keys
    )
    result["providerRequestCount"] = sum(
        max(0, int(receipt.get("providerRequestCount") or 0))
        for receipt in receipts
    )
    result["usageSource"] = (
        "parent_and_subagent_provider_request_receipts"
        if children
        else str(parent.get("usageSource") or "provider_request_receipts")
    )
    result["categoryReceiptComplete"] = category_complete
    return result


def _public_model_state(ensure: Mapping[str, object]) -> dict[str, object]:
    state = ensure.get("state")
    state = state if isinstance(state, Mapping) else {}
    model = state.get("model")
    model = model if isinstance(model, Mapping) else {}
    return {
        "provider": str(model.get("provider") or ""),
        "id": str(model.get("id") or ""),
        "thinkingLevel": str(state.get("thinkingLevel") or ""),
        "protocolVersion": str(state.get("protocolVersion") or "2"),
    }


def _development_exclusion(
    paths: list[Path],
    *,
    prepared_path: Path,
    retrieval_report_path: Path,
) -> dict[str, object]:
    prepared_sha256 = _file_sha256(prepared_path)
    retrieval_sha256 = _file_sha256(retrieval_report_path)
    case_ids: set[str] = set()
    sources: list[dict[str, str]] = []
    for path in paths:
        report = _read_json_object(path)
        if report.get("sourcePreparedSha256") != prepared_sha256:
            raise ValueError("development report uses a different prepared dataset")
        if report.get("sourceRetrievalReportSha256") != retrieval_sha256:
            raise ValueError("development report uses a different frozen retrieval report")
        evaluation = report.get("evaluation")
        raw_case_ids = evaluation.get("caseIds") if isinstance(evaluation, Mapping) else None
        if not isinstance(raw_case_ids, list) or not raw_case_ids:
            raise ValueError("development report has no evaluation case IDs")
        normalized = {str(item).strip() for item in raw_case_ids if str(item).strip()}
        if len(normalized) != len(raw_case_ids):
            raise ValueError("development report case IDs must be unique non-empty strings")
        case_ids.update(normalized)
        sources.append(
            {
                "fileSha256": _file_sha256(path),
                "reportSha256": str(report.get("reportSha256") or ""),
            }
        )
    ordered = sorted(case_ids)
    return {
        "caseCount": len(ordered),
        "caseIds": ordered,
        "caseIdsSha256": _sha256_json(ordered),
        "sourceReports": sources,
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _public_pi_runtime_identity(config: object) -> dict[str, object]:
    executable = getattr(config, "executable", None)
    if not isinstance(executable, Path):
        raise RuntimeError("benchmark Pi runtime executable is unavailable")
    executable = executable.expanduser().resolve(strict=True)
    manifest_path: Path | None = None
    launch_paths = [executable]
    node_executable = str(getattr(config, "node_executable", "") or "").strip()
    if node_executable:
        launch_paths.append(Path(node_executable).expanduser().resolve(strict=True))
    for launch_path in launch_paths:
        for directory in (launch_path.parent, *launch_path.parents):
            candidate = directory / "manifest.json"
            if candidate.is_file() and not candidate.is_symlink():
                manifest_path = candidate
                break
        if manifest_path is not None:
            break
    if manifest_path is None:
        raise RuntimeError("benchmark Pi runtime manifest is unavailable")
    manifest = _read_json_object(manifest_path)
    provider_environment = getattr(config, "provider_environment", {})
    provider_environment = (
        provider_environment if isinstance(provider_environment, Mapping) else {}
    )
    runtime_entrypoint_text = str(
        provider_environment.get("RAG_IME_BENCHMARK_RUNTIME_ENTRYPOINT") or ""
    ).strip()
    runtime_entrypoint = (
        Path(runtime_entrypoint_text).expanduser().resolve(strict=True)
        if runtime_entrypoint_text
        else None
    )
    node_path = (
        Path(node_executable).expanduser().resolve(strict=True)
        if node_executable
        else None
    )
    node_version = ""
    if node_path is not None:
        completed = subprocess.run(
            [str(node_path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0:
            raise RuntimeError("benchmark Pi runtime Node version probe failed")
        node_version = completed.stdout.strip()
    source = manifest.get("source")
    source = source if isinstance(source, Mapping) else {}
    pinned_manifest = str(
        provider_environment.get(
            "RAG_IME_BENCHMARK_PINNED_RUNTIME_MANIFEST_SHA256"
        )
        or ""
    )
    identity = {
        "schemaVersion": "rag-ime.rag-agent-pi-runtime-identity.v1",
        "runtimeVersion": str(manifest.get("runtimeVersion") or ""),
        "piVersion": str(manifest.get("piVersion") or ""),
        "protocolVersion": str(manifest.get("runtimeProtocolVersion") or ""),
        "manifestSha256": _file_sha256(manifest_path),
        "launcherSha256": _file_sha256(executable),
        "runtimeEntrypointSha256": (
            _file_sha256(runtime_entrypoint)
            if runtime_entrypoint is not None
            else ""
        ),
        "nodeSha256": _file_sha256(node_path) if node_path is not None else "",
        "nodeVersion": node_version,
        "sourceCommit": str(source.get("commit") or ""),
        "toolSetSha256": _sha256_json(list(getattr(config, "tools", ()))),
        "sourceAccess": (
            "explicit-verified-payload-v1"
            if pinned_manifest and pinned_manifest == _file_sha256(manifest_path)
            else "read-only-pointer-stable-verified-snapshot-v1"
        ),
        "writableStateScope": "benchmark-run-root-only",
    }
    if not all(
        str(identity[field])
        for field in ("runtimeVersion", "piVersion", "protocolVersion")
    ):
        raise RuntimeError("benchmark Pi runtime identity is incomplete")
    identity["identitySha256"] = _sha256_json(identity)
    return identity


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def _progress(event: str, **payload: object) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False, sort_keys=True), flush=True)


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
