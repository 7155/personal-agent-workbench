from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, replace
from threading import BoundedSemaphore, Event, RLock, Thread
from typing import Any, Mapping

from .adapter import InputMethodAdapter, SuggestionRequest
from .anti_echo import candidate_echoes_text, candidate_has_self_repetition
from .assistant_overlay import build_assistant_overlay_payload, build_candidate_panel_payload
from .context_frame import build_current_input_frame, context_frame_trace_payload, foreground_text_from_payload
from .context_group import GroupShortBuffer
from .context_views import (
    build_display_view,
    build_model_prompt_view,
    build_rag_retrieval_view,
    build_rime_view,
    context_views_trace_payload,
)
from .contracts.key_policy import key_policy_for_prediction_session as contract_key_policy_for_prediction_session
from .contracts.source import source_badge_for, source_color_token_for
from .core_client import CoreClient
from .deepseek_completion import DeepSeekCompletionRequest
from .embeddings import NullEmbeddingProvider
from .foreground_privacy import assess_foreground_write, storage_receipt
from .generation_memory import generation_memory_evidence_pack, retrieve_generation_memory_hits
from .history_context import build_prediction_context, model_prediction_context_limits, prediction_context_metadata
from .local_sqlite_core import LocalSqliteCoreClient
from .memory_optimizer import MemoryOptimizerConfig, optimize_suggestions_if_enabled
from .models import (
    FrontendTransaction,
    InputSuggestion,
    MemoryAction,
    ModelPrediction,
    RimeCandidate,
    RimeContextSnapshot,
    SideCandidateDisplayItem,
)
from .memory.curated_store import (
    RealtimeMemoryContext,
    annotate_realtime_memory_candidate,
    decide_realtime_memory_candidate,
)
from .payloads import action_response_payload, model_prediction_to_payload, suggestion_to_payload
from .pinyin_index import build_pinyin_metadata, text_initials
from .prediction_anchors import build_prediction_anchors_from_snapshot, prediction_mode_family
from .prediction_first import (
    InputMode,
    infer_input_mode,
    prediction_session_to_payload,
)
from .prediction.quality import reject_context_echo as quality_reject_context_echo
from .prediction.quality import reject_prompt_leak as quality_reject_prompt_leak
from .prediction_manager import PredictionManager
from .prediction_trigger import PredictionTrigger, PredictionTriggerConfig, TriggerDecision
from .prediction_status import prediction_status_row
from .predictor import (
    PREDICTION_REQUEST_NO_INPUT,
    PREDICTION_REQUEST_PINYIN_CONSTRAINED,
    PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
    PREDICTION_REQUEST_RIME_REORDER,
    PredictionProvider,
    predict_with_optional_request_context,
)
from .runtime_flags import (
    assert_deepseek_not_called,
    assert_deepseek_scene_allowed,
    deepseek_scene_enabled,
)
from .runtime_config import RuntimeConfigSnapshot
from .rag_core_v3 import memory_candidates_v2_to_input_suggestions
from .side_lane_scheduler import LaneRequestToken, LatestWinsLaneScheduler
from .smart_rag_context_packet import build_post_commit_context_packet
from .text_utils import compact_whitespace, now_ms, stable_text_hash, token_terms
from .timeline_context import timeline_evidence_pack_from_core


RIME_SIDECAR_SCHEMA_VERSION = "rag-ime.rime-sidecar.v1"
_SEMANTIC_ASCII_TERMS = {
    "agent",
    "bm25",
    "brew",
    "cargo",
    "codex",
    "curl",
    "fts5",
    "git",
    "github",
    "kv",
    "llm",
    "mlx",
    "npm",
    "pi",
    "pip",
    "python",
    "qwen",
    "rag",
    "rime",
    "rust",
    "sqlite",
    "squirrel",
    "swift",
    "uv",
    "vcp",
    "xcode",
    "xcodebuild",
}
_RAW_COMMIT_ASCII_TERMS = {
    "brew",
    "cargo",
    "codex",
    "curl",
    "git",
    "github",
    "npm",
    "pip",
    "python",
    "pytest",
    "rust",
    "swift",
    "uv",
    "xcodebuild",
}
_SHELL_COMMAND_PREFIXES = _RAW_COMMIT_ASCII_TERMS | {
    "cat",
    "cd",
    "cp",
    "grep",
    "head",
    "less",
    "ls",
    "mkdir",
    "mv",
    "open",
    "rg",
    "sed",
    "tail",
}
_RAG_LANE_SEMAPHORE = BoundedSemaphore(1)
_REALTIME_MODEL_CONTEXT_BUDGET_MS = 500
_MODEL_HOLDOVER_TTL_MS = 5000
_MODEL_LANE_LEASE_TTL_MS = 3000
_PROGRESSIVE_FIRST_RESPONSE_MS = 700
_PROGRESSIVE_FOLLOW_UP_RETRY_MS = 280
_POST_COMMIT_FIRST_RESPONSE_MS = 180
_POST_COMMIT_COMPLETION_TTL_MS = 12000
_POST_COMMIT_MODEL_HARD_TIMEOUT_MS = 12000
_POST_COMMIT_PANEL_TTL_MS = 8000
_PREFIX_CONSTRAINED_PANEL_TTL_MS = 2600
_REFRESH_DEBOUNCE_MS = 100
_MODEL_HOLDOVER_MAX_ENTRIES = 32
_PREDICTION_MANAGER_MAX_ENTRIES = 16
_REFRESH_DEBOUNCE_MAX_ENTRIES = 128
_POST_COMMIT_COMPLETION_CACHE_MAX_JOBS = 8
_POST_COMMIT_PRESENTATION_STREAM_MAX_ENTRIES = 32
_FOREGROUND_CONTEXT_MAX_FRESHNESS_MS = 700
_MODEL_GROUP_CONTEXT_TTL_MS = 120_000
_MODEL_GROUP_CONTEXT_MAX_CHARS = 2048
_MODEL_COMPLETION_CONTEXT_MAX_CHARS = 256
_MODEL_LANE_LOCK = RLock()
_MODEL_LANE_ACTIVE_TOKEN: str | None = None
_MODEL_LANE_ACTIVE_STARTED_AT = 0.0
_MODEL_HOLDOVER_LOCK = RLock()
_MODEL_HOLDOVERS: dict[tuple[str, str], "_ModelPredictionHoldover"] = {}
_PREDICTION_MANAGER_LOCK = RLock()
_PREDICTION_MANAGERS: dict[tuple[str, str, str], PredictionManager] = {}
_REFRESH_DEBOUNCE_LOCK = RLock()
_REFRESH_DEBOUNCE: dict[tuple[str, str, str, str, str], "_RefreshDebounceState"] = {}
def _early_env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


_SIDE_LANE_SCHEDULER = LatestWinsLaneScheduler()
_AUTO_PREDICTION_TRIGGER = PredictionTrigger(
    PredictionTriggerConfig(
        idle_ms=0,
        min_delta_chars=max(1, _early_env_int("RAG_IME_AUTO_PREDICT_MIN_DELTA_CHARS", 1)),
        max_calls_per_10s=max(1, _early_env_int("RAG_IME_AUTO_PREDICT_MAX_CALLS_PER_10S", 10)),
        ignore_cooldown_ms=max(0, _early_env_int("RAG_IME_AUTO_PREDICT_IGNORE_COOLDOWN_MS", 0)),
    )
)
_AUTO_PREDICTION_TRIGGER_LOCK = RLock()
_AUTO_PREDICTION_JOB_DECISIONS: dict[str, TriggerDecision] = {}
_AUTO_PREDICTION_TRIGGER_CONFIG = (
    _AUTO_PREDICTION_TRIGGER.config.min_delta_chars,
    _AUTO_PREDICTION_TRIGGER.config.max_calls_per_10s,
    _AUTO_PREDICTION_TRIGGER.config.ignore_cooldown_ms,
)
_GROUP_SHORT_BUFFER = GroupShortBuffer()
_DISPLAY_FEEDBACK_SEEN_LOCK = RLock()
_DISPLAY_FEEDBACK_SEEN: dict[tuple[str, str, str], None] = {}
_DISPLAY_FEEDBACK_SEEN_MAX = 512
_POST_COMMIT_COMPLETION_CACHE: "PostCommitCompletionCache"
_POST_COMMIT_PRESENTATION_STREAM_LOCK = RLock()
_POST_COMMIT_PRESENTATION_STREAM_STAGES: dict[str, tuple[int, float]] = {}
_FALSEY_ENV_VALUES = {"0", "false", "no", "off"}
_CANDIDATE_SOURCE_SUFFIXES = (
    "_model",
    "_rag",
    "_memory",
    " [LLM]",
    " [RAG]",
    " LLM",
    " RAG",
    " 模",
    " 查",
    " 忆",
)

_MODEL_QUALITY_TOPIC_TERMS = (
    "rag",
    "rime",
    "llm",
    "ds",
    "输入法",
    "候选",
    "联想",
    "模型",
    "训练",
    "语料",
    "上下文",
    "重排",
    "排序",
    "优化",
    "推理",
    "延迟",
    "数据",
    "反馈",
    "测试",
    "终端",
    "命令行",
    "缓存",
    "接口",
)
_MODEL_QUALITY_OPEN_ENDINGS = (
    "是",
    "有",
    "要",
    "需要",
    "应该",
    "可以",
    "把",
    "先",
    "再",
    "然后",
    "因为",
    "所以",
    "但是",
    "如果",
    "以后",
    "之后",
    "为了",
    "想",
    "希望",
    "准备",
)


def configure_auto_prediction_trigger(
    *,
    min_delta_chars: int,
    max_calls_per_10s: int,
    ignore_cooldown_ms: int,
) -> None:
    """Apply the current runtime snapshot to the commit-burst trigger."""

    global _AUTO_PREDICTION_TRIGGER, _AUTO_PREDICTION_TRIGGER_CONFIG
    config_key = (
        max(1, int(min_delta_chars)),
        max(0, int(max_calls_per_10s)),
        max(0, int(ignore_cooldown_ms)),
    )
    with _AUTO_PREDICTION_TRIGGER_LOCK:
        if config_key == _AUTO_PREDICTION_TRIGGER_CONFIG:
            return
        _AUTO_PREDICTION_TRIGGER = PredictionTrigger(
            PredictionTriggerConfig(
                idle_ms=0,
                min_delta_chars=config_key[0],
                max_calls_per_10s=config_key[1],
                ignore_cooldown_ms=config_key[2],
            )
        )
        _AUTO_PREDICTION_TRIGGER_CONFIG = config_key
        _AUTO_PREDICTION_JOB_DECISIONS.clear()
_RECENT_MEMORY_KEYWORDS = (
    "输入法",
    "RAG",
    "rag",
    "LLM",
    "llm",
    "记忆",
    "模型",
    "候选",
    "预测",
    "调试",
    "流程",
    "Codex",
    "Agent",
    "agent",
)
_RECENT_MEMORY_LOW_VALUE = {
    "根据",
    "基于",
    "测试",
    "分析",
    "验证",
    "假设",
    "或者",
    "现在",
    "目前",
    "下一步",
    "接下来",
}
_LOW_INFORMATION_CJK_TOKENS = {
    "的",
    "得",
    "地",
    "了",
    "是",
    "和",
    "与",
    "及",
    "或",
    "在",
    "就",
    "都",
    "而",
    "把",
    "被",
    "个",
    "这",
    "那",
    "吗",
    "呢",
    "啊",
    "呀",
    "吧",
    "嗯",
    "呃",
    "额",
    "哦",
}
_PROMPT_EXAMPLE_LEAK_SURFACES = {
    "把流程跑通",
    "接入本地",
    "接入本地记忆",
    "验证 LLM 候选",
    "验证LLM候选",
    "预测流程完成",
    "部署 RAG 组件",
    "部署RAG组件",
    "模型相关表达",
    "调试流程",
}
_RECENT_MEMORY_TRAILING_NOISE = _RECENT_MEMORY_LOW_VALUE | {
    "啊",
    "阿",
    "呃",
    "嗯",
    "额",
    "哦",
    "噢",
    "唔",
    "下一步",
    "接下来",
    "法",
    "撒旦",
    "深度",
}
_POST_COMMIT_GENERIC_QUERY_TERMS = {
    "一下",
    "一个",
    "一些",
    "以及",
    "包括",
    "今天",
    "他们",
    "你们",
    "使用",
    "候选词",
    "刚刚",
    "可以",
    "已经",
    "我们",
    "文档",
    "项目",
    "正在",
    "输入",
    "输入法",
    "这个",
    "那个",
    "继续",
    "现在",
    "目前",
    "用户",
    "需要",
    "问题",
}


@dataclass(frozen=True)
class RimeSideCandidateTriggerDecision:
    should_refresh: bool
    reason: str


@dataclass(frozen=True)
class _ModelPredictionHoldover:
    project: str
    input_state_fingerprint: str
    explicit_recent_context: str
    current_input: str
    predictions: tuple[ModelPrediction, ...]
    created_at: float


@dataclass(frozen=True)
class _RefreshDebounceState:
    created_at_ms: int
    request_seq: int
    semantic_query_hash: str


@dataclass(frozen=True)
class PostCommitCompletionKey:
    project: str
    app: str
    hard_context_anchor: str
    context_fingerprint: str
    commit_text_hash: str
    commit_text_length: int
    input_source_id: str
    selection_epoch: int
    runtime_config_hash: str = ""
    max_candidates: int = 0
    request_type: str = PREDICTION_REQUEST_POST_COMMIT_COMPLETION


@dataclass
class PostCommitCompletionJob:
    key: PostCommitCompletionKey
    job_id: str
    created_at: float
    started_at_ms: int
    hard_timeout_ms: int
    state: str = "pending"
    predictions: tuple[ModelPrediction, ...] = ()
    error: str = ""
    completed_at: float = 0.0
    provider_call_count: int = 0
    presentation_stage: int = 0


class PostCommitCompletionCache:
    def __init__(self, *, ttl_ms: int = _POST_COMMIT_COMPLETION_TTL_MS) -> None:
        self._ttl_ms = max(1000, int(ttl_ms))
        self._lock = RLock()
        self._jobs: dict[PostCommitCompletionKey, PostCommitCompletionJob] = {}

    def poll_or_start(
        self,
        *,
        key: PostCommitCompletionKey,
        snapshot: RimeContextSnapshot,
        core: CoreClient,
        predictor: PredictionProvider,
        project: str,
        explicit_recent_context: str,
        max_candidates: int,
        ttl_ms: int,
        hard_timeout_ms: int,
        wait_ms: int = 0,
        memory_enabled: bool = True,
    ) -> tuple[list[ModelPrediction], dict[str, object]]:
        now = time.time()
        ttl_ms = max(1000, int(ttl_ms))
        hard_timeout_ms = max(300, int(hard_timeout_ms))
        wait_ms = max(0, min(int(wait_ms), hard_timeout_ms))
        start_kwargs: dict[str, object] | None = None
        started = False
        with self._lock:
            self._ttl_ms = ttl_ms
            self._drop_expired_locked(now)
            job = self._jobs.get(key)
            if job is None:
                job_id = _short_stable_id(
                    "post-commit-completion",
                    key.project,
                    key.app,
                    key.context_fingerprint,
                    key.commit_text_hash,
                    str(key.selection_epoch),
                    key.input_source_id,
                    key.runtime_config_hash,
                    str(key.max_candidates),
                )
                job = PostCommitCompletionJob(
                    key=key,
                    job_id=job_id,
                    created_at=now,
                    started_at_ms=now_ms(),
                    hard_timeout_ms=hard_timeout_ms,
                    state="pending",
                )
                self._jobs[key] = job
                self._drop_over_limit_locked()
                start_kwargs = {
                    "job": job,
                    "snapshot": snapshot,
                    "core": core,
                    "predictor": predictor,
                    "project": project,
                    "explicit_recent_context": explicit_recent_context,
                    "max_candidates": max_candidates,
                    "memory_enabled": memory_enabled,
                }
                started = True
        if start_kwargs is not None:
            Thread(
                target=self._run_job,
                name=f"rag-ime-post-commit-completion-{job.job_id}",
                kwargs=start_kwargs,
                daemon=True,
            ).start()

        # The resident MiniMind seed probe is fast and the shared-prefill batch
        # normally completes full phrases within the foreground window. Wait
        # briefly so the first response can carry real candidates.
        deadline = time.monotonic() + wait_ms / 1000
        while wait_ms > 0 and time.monotonic() < deadline:
            with self._lock:
                current = self._jobs.get(key)
                if current is not job or job.state != "pending":
                    break
            time.sleep(min(0.004, max(0.001, deadline - time.monotonic())))

        with self._lock:
            current = self._jobs.get(key)
            if current is not job:
                return [], _model_lane_status(
                    called=False,
                    timed_out=False,
                    skipped_reason="post-commit completion superseded",
                    budget_ms=hard_timeout_ms,
                    request_type=PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
                    requested_max_candidates=max_candidates,
                )
            return self._payload_for_job_locked(
                job,
                now=time.time(),
                cache_hit=not started and job.state == "completed",
                started=started and job.state == "pending",
            )

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()

    def _run_job(
        self,
        *,
        job: PostCommitCompletionJob,
        snapshot: RimeContextSnapshot,
        core: CoreClient,
        predictor: PredictionProvider,
        project: str,
        explicit_recent_context: str,
        max_candidates: int,
        memory_enabled: bool,
    ) -> None:
        started = time.perf_counter()
        predictions: list[ModelPrediction] = []
        state = "completed"
        error = ""
        try:
            recent_context = compact_whitespace(explicit_recent_context)[-420:]
            if not recent_context and memory_enabled:
                context_event_limit, context_char_limit = model_prediction_context_limits()
                recent_context = build_prediction_context(
                    core,
                    explicit_recent_context=explicit_recent_context,
                    project=project,
                    limit=context_event_limit,
                    max_chars=context_char_limit,
                )
            raw_predictions = predict_with_optional_request_context(
                predictor,
                current_input="",
                recent_context=recent_context,
                max_candidates=max_candidates,
                request_type=PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
                rime_candidates=(),
            )
            predictions = filter_post_commit_model_completions(
                raw_predictions,
                snapshot=snapshot,
                existing_texts=(),
                max_candidates=max_candidates,
            )
            if int((time.perf_counter() - started) * 1000) > job.hard_timeout_ms:
                state = "timeout"
                predictions = []
        except Exception as exc:  # pragma: no cover - defensive fail-closed guard
            state = "error"
            error = exc.__class__.__name__
        with self._lock:
            current = self._jobs.get(job.key)
            if current is not job:
                return
            current.provider_call_count += 1
            current.completed_at = time.time()
            current.error = error
            current.state = state
            current.predictions = tuple(predictions)

    def _payload_for_job_locked(
        self,
        job: PostCommitCompletionJob,
        *,
        now: float,
        cache_hit: bool,
        started: bool = False,
    ) -> tuple[list[ModelPrediction], dict[str, object]]:
        elapsed_ms = int((now - job.created_at) * 1000)
        if job.state == "pending" and elapsed_ms > job.hard_timeout_ms:
            job.state = "timeout"
            job.completed_at = now
            job.predictions = ()
        predictions, presentation_pending, presentation_stage, presentation_stage_count = (
            _post_commit_presentation_stream_predictions(job)
        )
        state = "hit" if cache_hit and predictions and not presentation_pending else ("started" if started else job.state)
        if presentation_pending:
            state = "streaming"
        lane = _model_lane_status(
            called=True,
            timed_out=job.state == "timeout",
            skipped_reason="" if predictions else f"post-commit completion {state}",
            budget_ms=job.hard_timeout_ms,
            elapsed_ms=elapsed_ms,
            prediction_count=len(predictions),
            request_type=PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
            rime_candidate_count=0,
            requested_max_candidates=len(predictions),
        )
        lane.update(
            {
                "asyncMode": True,
                "completionJobId": job.job_id,
                "completionKeyHash": _short_stable_id(
                    job.key.project,
                    job.key.app,
                    job.key.hard_context_anchor,
                    job.key.context_fingerprint,
                    job.key.commit_text_hash,
                    str(job.key.commit_text_length),
                    job.key.input_source_id,
                    str(job.key.selection_epoch),
                    job.key.request_type,
                    job.key.runtime_config_hash,
                    str(job.key.max_candidates),
                ),
                "completionJobState": state,
                "cacheHit": bool(cache_hit and predictions),
                "inFlight": job.state == "pending" or presentation_pending,
                "pending": job.state == "pending" or presentation_pending,
                "jobElapsedMs": elapsed_ms,
                "firstResponseMs": post_commit_first_response_budget_ms(),
                "noPinyinFilter": True,
                "requestType": PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
                "providerCallCount": job.provider_call_count,
                "completionCacheSize": len(self._jobs),
                "completionCacheMaxSize": post_commit_completion_cache_max_jobs(),
                "presentationStreaming": bool(presentation_stage_count > 1),
                "presentationStage": presentation_stage,
                "presentationStageCount": presentation_stage_count,
            }
        )
        if job.error:
            lane["error"] = job.error
        return predictions, lane

    def _drop_expired_locked(self, now: float) -> None:
        expired = [
            key
            for key, job in self._jobs.items()
            if int((now - job.created_at) * 1000) > self._ttl_ms
        ]
        for key in expired:
            self._jobs.pop(key, None)
        self._drop_over_limit_locked()

    def _drop_over_limit_locked(self) -> None:
        limit = post_commit_completion_cache_max_jobs()
        while len(self._jobs) > limit:
            oldest_key = min(
                self._jobs,
                key=lambda item: (
                    self._jobs[item].state == "pending",
                    self._jobs[item].created_at,
                ),
            )
            self._jobs.pop(oldest_key, None)


def _post_commit_presentation_stream_predictions(
    job: PostCommitCompletionJob,
) -> tuple[list[ModelPrediction], bool, int, int]:
    if job.state != "completed" or not job.predictions:
        return [], False, 0, 0
    first = job.predictions[0]
    steps = _post_commit_presentation_stream_steps(first.text)
    if not post_commit_presentation_stream_enabled() or len(steps) <= 1:
        return list(job.predictions), False, len(steps), len(steps)
    job.presentation_stage = min(job.presentation_stage + 1, len(steps))
    visible_text = steps[job.presentation_stage - 1]
    presentation_pending = job.presentation_stage < len(steps)
    metadata = dict(first.metadata)
    metadata.update(
        {
            "presentationStreaming": True,
            "presentationStage": job.presentation_stage,
            "presentationStageCount": len(steps),
            "presentationPartial": presentation_pending,
            "presentationFinalTextHash": stable_text_hash(first.text),
            "presentationStreamSlotKey": _post_commit_completion_stream_slot_key(job.job_id),
        }
    )
    staged_first = replace(
        first,
        text=visible_text,
        metadata=metadata,
    )
    if presentation_pending:
        return [staged_first], True, job.presentation_stage, len(steps)
    return [staged_first, *list(job.predictions[1:])], False, job.presentation_stage, len(steps)


def _post_commit_presentation_stream_steps(text: str) -> list[str]:
    surface = compact_whitespace(text)
    if not surface:
        return []
    if len(surface) <= 3:
        return [surface]
    if re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9 _-]+", surface):
        if len(surface) <= 5:
            raw_steps = [surface[:2], surface]
        else:
            raw_steps = [surface[:2], surface[:4], surface]
    else:
        raw_steps = [surface[: max(2, min(4, len(surface) // 2))], surface]
    steps: list[str] = []
    for step in raw_steps:
        step = compact_whitespace(step)
        if step and step not in steps:
            steps.append(step)
    return steps or [surface]


_POST_COMMIT_COMPLETION_CACHE = PostCommitCompletionCache()


def build_rime_sidecar_response(
    *,
    payload: dict[str, Any],
    adapter: InputMethodAdapter,
    core: CoreClient,
    predictor: PredictionProvider,
    deepseek_completion_provider: Any | None = None,
    default_project: str = "wisdom-weasel-rag-ime",
    runtime_config: RuntimeConfigSnapshot | None = None,
) -> dict[str, object]:
    privacy_assessment = assess_foreground_write(payload)
    if sensitive_input_requested(payload) and privacy_assessment["disposition"] != "sensitive":
        privacy_assessment = {
            "schemaVersion": "rag-ime.foreground-privacy-assessment.v1",
            "disposition": "sensitive",
            "storeAllowed": False,
            "reason": "sensitive_foreground_flag",
            "source": "foreground_guard",
        }
    if privacy_assessment["storeAllowed"] is not True:
        return _sensitive_field_response(
            payload=payload,
            default_project=default_project,
            privacy_assessment=privacy_assessment,
        )
    if runtime_config is None:
        configure_auto_prediction_trigger(
            min_delta_chars=_bounded_int(
                os.environ.get("RAG_IME_AUTO_PREDICT_MIN_DELTA_CHARS"),
                default=2,
                minimum=1,
                maximum=32,
            ),
            max_calls_per_10s=_bounded_int(
                os.environ.get("RAG_IME_AUTO_PREDICT_MAX_CALLS_PER_10S"),
                default=2,
                minimum=0,
                maximum=10,
            ),
            ignore_cooldown_ms=_bounded_int(
                os.environ.get("RAG_IME_AUTO_PREDICT_IGNORE_COOLDOWN_MS"),
                default=1500,
                minimum=0,
                maximum=10000,
            ),
        )
    snapshot = parse_rime_context_payload(payload, default_project=default_project)
    active_composition = bool(compact_whitespace(snapshot.raw_input or snapshot.preedit))
    debug_allow_composition_lanes = _bool(
        payload.get("debugAllowCompositionLanes"),
        default=False,
    )
    debug_use_legacy_rag_fallback = _bool(
        payload.get("debugUseLegacyRagFallback"),
        default=False,
    )
    if runtime_config is not None and not active_composition and not runtime_config.post_commit.enabled:
        return _runtime_hard_clear_response(
            payload=payload,
            default_project=default_project,
            reason="post_commit_disabled",
        )
    if runtime_config is not None:
        snapshot = replace(
            snapshot,
            max_side_candidates=min(
                snapshot.max_side_candidates,
                runtime_config.post_commit.max_candidates,
            ),
        )
    foreground_context, foreground_context_meta = foreground_context_for_side_lanes(payload, snapshot=snapshot)
    effective_snapshot = (
        replace(snapshot, committed_context=foreground_context)
        if foreground_context
        else snapshot
    )
    semantic_query, query_basis = choose_semantic_query(
        effective_snapshot,
        foreground_context=foreground_context,
    )
    trigger_decision = decide_side_candidate_refresh(
        snapshot=effective_snapshot,
        semantic_query=semantic_query,
        query_basis=query_basis,
    )
    if (
        runtime_config is not None
        and active_composition
        and not runtime_config.composition_ai
        and not snapshot.force_side_candidates
        and not debug_allow_composition_lanes
    ):
        trigger_decision = RimeSideCandidateTriggerDecision(
            False,
            "composition_ai_disabled_by_effective_runtime_config",
        )
    foreground_context_gate = post_commit_foreground_context_gate(
        snapshot=effective_snapshot,
        foreground_context_meta=foreground_context_meta,
    )
    if trigger_decision.should_refresh and not bool(foreground_context_gate.get("allowed")):
        trigger_decision = RimeSideCandidateTriggerDecision(
            False,
            _string(foreground_context_gate.get("reason")) or "skip: reliable foreground context required",
        )
    trigger_decision, commit_burst_trigger = apply_commit_burst_prediction_gate(
        payload=payload,
        snapshot=effective_snapshot,
        adapter=adapter,
        predictor=predictor,
        trigger_decision=trigger_decision,
        foreground_context_gate=foreground_context_gate,
        privacy_disposition=str(privacy_assessment["disposition"]),
        runtime_config=runtime_config,
    )
    trigger_decision, refresh_debounce = apply_refresh_debounce(
        snapshot=effective_snapshot,
        trigger_decision=trigger_decision,
        semantic_query=semantic_query,
        query_basis=query_basis,
        default_project=default_project,
    )
    post_commit_action_only_fast_path = post_commit_action_only_fast_path_enabled(effective_snapshot)
    if trigger_decision.should_refresh and post_commit_action_only_fast_path:
        trigger_decision = RimeSideCandidateTriggerDecision(
            False,
            "post-commit action-only fast path; Active RAG runs after explicit selection",
        )
    if trigger_decision.should_refresh:
        lane_started = time.perf_counter()
        suggestions, rag_lane, model_predictions, model_lane, progressive_state = run_side_lanes_with_latency_budget(
            adapter=adapter,
            core=core,
            predictor=predictor,
            deepseek_completion_provider=deepseek_completion_provider,
            snapshot=effective_snapshot,
            current_input=semantic_query,
            query_basis=query_basis,
            recent_context=effective_snapshot.committed_context,
            explicit_recent_context=effective_snapshot.committed_context,
            project=snapshot.project or default_project,
            app=snapshot.app,
            context_group_id=_string(foreground_context_meta.get("contextGroupId")),
            top_k=snapshot.max_side_candidates,
            max_candidates=snapshot.max_side_candidates,
            latency_budget_ms=snapshot.latency_budget_ms,
            runtime_config=(
                None
                if snapshot.force_side_candidates or debug_use_legacy_rag_fallback
                else runtime_config
            ),
        )
        commit_burst_latest = reconcile_commit_burst_prediction_outcome(
            trigger_meta=commit_burst_trigger,
            model_lane=model_lane,
            prediction_count=len(model_predictions),
        )
        if commit_burst_latest is False:
            model_predictions = []
            model_lane["predictionCount"] = 0
            model_lane["skippedReason"] = "stale commit-burst generation; latest wins"
            model_lane["staleGenerationDropped"] = True
        raw_model_prediction_count = len(model_predictions)
        pending_preview_predictions, pending_preview_lane_update = _post_commit_pending_preview_predictions(
            snapshot=snapshot,
            semantic_query=semantic_query,
            model_predictions=model_predictions,
            model_lane=model_lane,
        )
        if pending_preview_predictions:
            model_predictions = pending_preview_predictions
            model_lane.update(pending_preview_lane_update)
            raw_model_prediction_count = len(model_predictions)
        model_predictions = _filter_model_predictions_for_snapshot(
            model_predictions,
            snapshot=effective_snapshot,
            query_basis=query_basis,
        )
        model_lane["filteredPredictionCount"] = raw_model_prediction_count - len(model_predictions)
        model_lane["predictionCount"] = len(model_predictions)
        prediction_context = _string(model_lane.get("historyContext"))
        raw_suggestion_count = len(suggestions)
        suggestions = _filter_rag_suggestions_for_query(
            suggestions,
            snapshot=effective_snapshot,
            semantic_query=semantic_query,
            query_basis=query_basis,
            prediction_context=prediction_context,
        )
        post_commit_filtered_suggestions = len(suggestions)
        suggestions = _filter_post_commit_rag_suggestions_for_query(
            suggestions,
            snapshot=effective_snapshot,
            semantic_query=semantic_query,
            query_basis=query_basis,
        )
        rag_lane["postCommitQualityFilteredCount"] = post_commit_filtered_suggestions - len(suggestions)
        rag_lane["suggestionCount"] = len(suggestions)
        rag_lane["filteredSuggestionCount"] = raw_suggestion_count - len(suggestions)
        if _should_suppress_post_commit_rag_only(snapshot, model_predictions, suggestions):
            rag_lane["postCommitRagOnlySuppressed"] = True
            rag_lane["suppressedSuggestionCount"] = len(suggestions)
            rag_lane["suggestionCount"] = 0
            suggestions = []
        suggestions, optimizer_trace = optimize_suggestions_if_enabled(
            core=core,
            snapshot=effective_snapshot,
            semantic_query=semantic_query,
            query_basis=query_basis,
            input_mode=infer_input_mode(snapshot),
            suggestions=suggestions,
            top_k=snapshot.max_side_candidates,
            latency_budget_ms=snapshot.latency_budget_ms,
        )
        rag_lane["suggestionCount"] = len(suggestions)
        if foreground_context_meta.get("applied"):
            rag_lane["foregroundContext"] = foreground_context_meta
            model_lane["foregroundContext"] = foreground_context_meta
        retrieved_suggestions = list(suggestions)
        display_suggestions = list(suggestions)
        direct_rag_display = (
            snapshot.force_side_candidates
            or debug_use_legacy_rag_fallback
            or runtime_config.hybrid_rag.direct_display
            if runtime_config is not None
            else rag_direct_display_enabled()
        )
        if direct_rag_display:
            rag_lane["directDisplaySuppressed"] = False
            rag_lane["suppressedDisplaySuggestionCount"] = 0
            rag_lane["displaySuggestionCount"] = len(display_suggestions)
        else:
            rag_lane["directDisplaySuppressed"] = bool(display_suggestions)
            rag_lane["suppressedDisplaySuggestionCount"] = len(display_suggestions)
            rag_lane["displaySuggestionCount"] = 0
            display_suggestions = []
        fallback_predictions = _post_commit_empty_result_fallback_predictions(
            snapshot=snapshot,
            semantic_query=semantic_query,
            model_predictions=model_predictions,
            suggestions=display_suggestions,
            rag_lane=rag_lane,
            model_lane=model_lane,
            progressive_state=progressive_state,
        )
        if fallback_predictions:
            fallback_predictions, fallback_lane_update = _stage_post_commit_fallback_presentation_stream(
                snapshot=snapshot,
                predictions=fallback_predictions,
                slot_key=_post_commit_model_lane_stream_slot_key(model_lane),
            )
            model_predictions = fallback_predictions
            model_lane["predictionCount"] = len(model_predictions)
            model_lane["fallbackCandidateCount"] = len(model_predictions)
            model_lane["fallbackReason"] = "demo_safe_empty_post_commit_fallback"
            model_lane["skippedReason"] = ""
            model_lane.update(fallback_lane_update)
            if bool(model_lane.get("pending")):
                progressive_state = _progressive_state(
                    enabled=progressive_sidecar_updates_enabled(),
                    partial=False,
                    should_follow_up=True,
                    pending_lanes=_progressive_pending_lanes(rag_lane=rag_lane, model_lane=model_lane),
                    first_response_budget_ms=post_commit_first_response_budget_ms(),
                    retry_after_ms=progressive_follow_up_retry_ms(),
                )
        model_lane.pop("historyContext", None)
        total_elapsed_ms = int((time.perf_counter() - lane_started) * 1000)
        rag_lane.update(
            {
                "totalLatencyBudgetMs": snapshot.latency_budget_ms,
                "sideLaneMode": "parallel",
                "sideLaneElapsedMs": total_elapsed_ms,
                "memoryOptimizer": optimizer_trace,
                "foregroundContext": foreground_context_meta,
            }
        )
        model_lane.update(
            {
                "totalLatencyBudgetMs": snapshot.latency_budget_ms,
                "elapsedBeforeModelMs": 0,
                "sideLaneMode": "parallel",
                "sideLaneElapsedMs": total_elapsed_ms,
                "foregroundContext": foreground_context_meta,
            }
        )
    else:
        skipped_by_trigger_reason = trigger_decision.reason or "side candidates disabled by trigger"
        prediction_context = ""
        model_predictions = []
        suggestions = []
        retrieved_suggestions = []
        display_suggestions = []
        optimizer_trace = {"enabled": False, "traceEnabled": False, "maxMs": 15}
        progressive_state = _progressive_state(
            enabled=progressive_sidecar_updates_enabled(),
            partial=False,
            should_follow_up=False,
        )
        rag_lane = {
            "called": False,
            "timedOut": False,
            "skippedReason": skipped_by_trigger_reason,
            "suggestionCount": 0,
            "filteredSuggestionCount": 0,
            "postCommitQualityFilteredCount": 0,
            "directDisplaySuppressed": False,
            "suppressedDisplaySuggestionCount": 0,
            "displaySuggestionCount": 0,
            "latencyBudgetMs": 0,
            "elapsedMs": 0,
            "totalLatencyBudgetMs": snapshot.latency_budget_ms,
            "memoryOptimizer": optimizer_trace,
            "foregroundContext": foreground_context_meta,
        }
        model_lane = {
            "called": False,
            "timedOut": False,
            "skippedReason": skipped_by_trigger_reason,
            "predictionCount": 0,
            "filteredPredictionCount": 0,
            "latencyBudgetMs": 0,
            "elapsedMs": 0,
            "totalLatencyBudgetMs": snapshot.latency_budget_ms,
            "elapsedBeforeModelMs": 0,
            "foregroundContext": foreground_context_meta,
        }
        if post_commit_action_only_fast_path:
            skipped_reason = "post-commit action-only fast path; Active RAG runs after explicit selection"
            rag_lane["skippedReason"] = skipped_reason
            model_lane["skippedReason"] = skipped_reason
    prediction_first_enabled = prediction_first_merge_enabled(payload) and not post_commit_action_only_fast_path
    if prediction_first_enabled:
        prediction_manager = _prediction_manager_for_snapshot(snapshot, default_project=default_project)
        raw_commit_text = raw_english_candidate_text(snapshot)
        if _should_clear_prediction_pool(
            snapshot=snapshot,
            trigger_decision=trigger_decision,
            raw_commit_text=raw_commit_text,
        ):
            prediction_manager.clear()
        source_update = trigger_decision.should_refresh or bool(model_predictions) or bool(display_suggestions)
        manager_result = prediction_manager.render(
            snapshot=snapshot,
            model_predictions=model_predictions if source_update else None,
            suggestions=display_suggestions if source_update else None,
            raw_commit_text=raw_commit_text,
            now_ms=now_ms(),
        )
        prediction_first_result = manager_result.merge_result
        display_candidates = list(prediction_first_result.display_candidates)
        prediction_session_payload = prediction_session_to_payload(manager_result.session)
        prediction_session_payload.update(
            {
                "sessionFingerprint": manager_result.session_fingerprint,
                "contextFingerprint": manager_result.context_fingerprint,
                "hardContextAnchor": manager_result.anchors.hard_context_anchor,
                "applyAnchor": manager_result.anchors.apply_anchor,
                "queryAnchor": manager_result.anchors.query_anchor,
                "displayAnchor": manager_result.anchors.display_anchor,
                "snapshotId": (
                    manager_result.stable_snapshot.snapshot_id if manager_result.stable_snapshot else ""
                ),
                "stableSnapshotId": (
                    manager_result.stable_snapshot.snapshot_id if manager_result.stable_snapshot else ""
                ),
                "snapshotGeneration": (
                    manager_result.stable_snapshot.generation if manager_result.stable_snapshot else 0
                ),
                "reusedLastGood": (
                    bool(manager_result.stable_snapshot.reused_last_good) if manager_result.stable_snapshot else False
                ),
                "stablePanelAction": _string(manager_result.stability.get("action")),
                "stablePanelReason": _string(manager_result.stability.get("reason")),
                "stablePanel": manager_result.stability,
                "requestSeq": snapshot.request_seq,
                "expiresAfterMs": _prediction_session_expiry_ms(prediction_session_payload),
                **frontend_transaction_to_payload(snapshot.frontend_transaction),
            }
        )
        prediction_first_payload: dict[str, object] = {
            "enabled": True,
            "mode": prediction_first_result.mode.value,
            "pinyinPrefix": prediction_first_result.pinyin_prefix,
            "policy": {
                **prediction_first_result.policy,
                "candidatePoolActive": manager_result.candidate_pool_active,
                "candidatePoolReused": manager_result.reused_candidate_pool,
                "candidatePoolStale": manager_result.candidate_pool_stale,
                "candidatePoolContextFingerprint": manager_result.context_fingerprint,
                "candidatePoolSessionFingerprint": manager_result.session_fingerprint,
                "stability": manager_result.stability,
            },
        }
    else:
        display_candidates = merge_display_candidates(
            snapshot=snapshot,
            model_predictions=model_predictions,
            suggestions=display_suggestions,
        )
        input_mode = infer_input_mode(snapshot)
        prediction_session_payload = {
            "phase": "legacy",
            "inputMode": input_mode.value,
            "pinyinPrefix": snapshot.preedit or snapshot.raw_input,
            "candidatePanelVisible": bool(display_candidates),
            "predictionPanelVisible": False,
            "shouldClearPredictionPanel": False,
            "clearReason": "",
            "selectionScope": "legacy",
            "rimeCompositionOwnedByRime": input_mode.value.endswith("composing"),
            "sessionFingerprint": _response_session_fingerprint(
                snapshot=snapshot,
                mode=input_mode.value,
                display_candidates=tuple(display_candidates),
            ),
            "contextFingerprint": _context_fingerprint(snapshot.committed_context),
            "requestSeq": snapshot.request_seq,
            "expiresAfterMs": 0,
            **frontend_transaction_to_payload(snapshot.frontend_transaction),
        }
        prediction_first_payload = {
            "enabled": False,
            "mode": input_mode.value,
            "pinyinPrefix": snapshot.preedit or snapshot.raw_input,
            "policy": {
                "engine": "legacy-sidecar-merge",
                "reason": "prediction-first merge is behind explicit flag",
            },
        }
    response_input_mode = infer_input_mode(snapshot)
    if response_input_mode in {InputMode.ANCHOR_COMPOSING, InputMode.PREFIX_CONSTRAINED_COMPOSING} and not snapshot.force_side_candidates:
        rime_display_candidates = rime_only_display_candidates(snapshot)
        if rime_display_candidates:
            display_candidates = rime_display_candidates
            prediction_session_payload.update(
                {
                    "phase": "anchor_composing",
                    "inputMode": response_input_mode.value,
                    "candidatePanelVisible": True,
                    "predictionPanelVisible": False,
                    "shouldClearPredictionPanel": False,
                    "clearReason": "",
                    "selectionScope": "rime",
                    "rimeCompositionOwnedByRime": True,
                    "sideCandidateCount": 0,
                    "rimeCandidateCount": len(display_candidates),
                }
            )
    key_policy = key_policy_for_prediction_session(prediction_session_payload)
    status_row = _prediction_status_row_for_response(
        progressive_state=progressive_state,
        rag_lane=rag_lane,
        model_lane=model_lane,
        generation=snapshot.request_seq,
        input_mode=response_input_mode.value,
    )
    if status_row is not None:
        display_candidates.append(status_row)
        if response_input_mode == InputMode.POST_COMMIT_PREDICTING:
            prediction_session_payload.update(
                {
                    "phase": "post_commit",
                    "inputMode": response_input_mode.value,
                    "candidatePanelVisible": True,
                    "predictionPanelVisible": True,
                    "shouldClearPredictionPanel": False,
                    "clearReason": "",
                    "selectionScope": "prediction",
                    "rimeCompositionOwnedByRime": False,
                    "expiresAfterMs": _POST_COMMIT_PANEL_TTL_MS,
                }
            )
            key_policy = key_policy_for_prediction_session(prediction_session_payload)
    action_added = append_post_commit_active_rag_action(
        display_candidates=display_candidates,
        snapshot=snapshot,
        input_mode=response_input_mode,
    )
    if action_added and response_input_mode == InputMode.POST_COMMIT_PREDICTING:
        prediction_session_payload.update(
            {
                "phase": "post_commit",
                "inputMode": response_input_mode.value,
                "candidatePanelVisible": True,
                "predictionPanelVisible": True,
                "shouldClearPredictionPanel": False,
                "clearReason": "",
                "selectionScope": "prediction",
                "rimeCompositionOwnedByRime": False,
                "expiresAfterMs": _POST_COMMIT_PANEL_TTL_MS,
            }
        )
        key_policy = key_policy_for_prediction_session(prediction_session_payload)
    ui_mode = ui_mode_for_response(
        input_mode=response_input_mode.value,
        display_candidates=display_candidates,
        status_row_visible=status_row is not None,
    )
    display_candidates = _bind_display_candidates_to_session(
        display_candidates=display_candidates,
        snapshot=snapshot,
        prediction_session_payload=prediction_session_payload,
        key_policy=key_policy,
    )
    _record_display_memory_feedback(
        core=core,
        snapshot=snapshot,
        display_candidates=display_candidates,
        trace_id=_string(
            (
                rag_lane.get("memoryOptimizer", {}).get("traceId")
                if isinstance(rag_lane.get("memoryOptimizer"), dict)
                else ""
            )
        ),
        project=snapshot.project or default_project,
    )
    refresh_decision = refresh_decision_payload(
        snapshot=snapshot,
        trigger_decision=trigger_decision,
        semantic_query=semantic_query,
        debounce=refresh_debounce,
    )
    show_decision = show_decision_payload(
        prediction_session=prediction_session_payload,
        display_candidates=display_candidates,
    )
    prediction_trace_events = prediction_trace_events_payload(
        prediction_session=prediction_session_payload,
        refresh_decision=refresh_decision,
        show_decision=show_decision,
        rag_lane=rag_lane,
        model_lane=model_lane,
        display_candidates=display_candidates,
    )
    if bool(commit_burst_trigger.get("enforced")):
        prediction_trace_events.append(
            {
                "event": str(commit_burst_trigger.get("traceEvent") or "prediction_trigger_skipped"),
                "fields": dict(commit_burst_trigger),
            }
        )
    context_frame = build_current_input_frame(
        snapshot,
        ui_mode=ui_mode,
        semantic_query=semantic_query,
        query_basis=query_basis,
        foreground_text_payload=payload.get("foregroundText") if isinstance(payload.get("foregroundText"), Mapping) else None,
    )
    rime_view = build_rime_view(context_frame)
    model_view = build_model_prompt_view(context_frame)
    rag_view = build_rag_retrieval_view(context_frame, semantic_query=semantic_query, query_basis=query_basis)
    display_view = build_display_view(
        context_frame,
        candidates=tuple(display_candidates),
        key_policy={str(key): str(value) for key, value in key_policy.items()},
    )
    include_context_text = os.environ.get("RAG_IME_TRACE_INCLUDE_TEXT") == "1"
    display_candidate_payloads = [display_item_to_payload(item) for item in display_candidates]
    rag_candidate_payloads = [suggestion_to_payload(item) for item in retrieved_suggestions]
    frontend_transaction_payload = frontend_transaction_to_payload(snapshot.frontend_transaction)
    candidate_panel_payload = build_candidate_panel_payload(
        input_mode=response_input_mode.value,
        display_candidates=display_candidate_payloads,
    )
    assistant_overlay_payload = build_assistant_overlay_payload(
        ui_mode=ui_mode,
        input_mode=response_input_mode.value,
        display_candidates=display_candidate_payloads,
        rag_candidates=rag_candidate_payloads,
        prediction_session=prediction_session_payload,
        key_policy=key_policy,
        progressive=progressive_state,
        frontend_transaction=frontend_transaction_payload,
    )
    recorded_event_ids = [
        str(item)
        for item in commit_burst_trigger.get("recordedEventIds", [])
        if str(item) and not str(item).startswith("skipped:")
    ] if isinstance(commit_burst_trigger.get("recordedEventIds"), list) else []
    suggest_storage_receipt = storage_receipt(
        privacy_assessment,
        stored=bool(recorded_event_ids),
        event_id=recorded_event_ids[0] if recorded_event_ids else None,
        outcome=None if recorded_event_ids else "no_write",
        reason=None if recorded_event_ids else "no_commit_burst_write",
    )
    if recorded_event_ids:
        suggest_storage_receipt["eventIds"] = recorded_event_ids
    return {
        "schemaVersion": RIME_SIDECAR_SCHEMA_VERSION,
        "sessionId": snapshot.session_id,
        "requestSeq": snapshot.request_seq,
        **frontend_transaction_payload,
        "frontendTransaction": frontend_transaction_payload,
        "project": snapshot.project or default_project,
        "stored": bool(recorded_event_ids),
        "noStore": False,
        "privacyAssessment": privacy_assessment,
        "storageReceipt": suggest_storage_receipt,
        "rawInput": snapshot.raw_input,
        "preedit": snapshot.preedit,
        "commitTextPreview": snapshot.commit_text_preview,
        "committedContext": snapshot.committed_context,
        "semanticQuery": semantic_query,
        "queryBasis": query_basis,
        "triggerDecision": {
            "shouldRefresh": trigger_decision.should_refresh,
            "reason": trigger_decision.reason,
            "refreshReason": trigger_decision.reason,
            "shouldShow": show_decision["shouldShow"],
            "showReason": show_decision["showReason"],
            "hardClear": show_decision["hardClear"],
            "hardClearReason": show_decision["hardClearReason"],
            "softHold": show_decision["softHold"],
            "softHoldReason": show_decision["softHoldReason"],
            "idleMs": snapshot.idle_ms,
            "semanticSignalLength": semantic_signal_length(semantic_query),
            "forceSideCandidates": snapshot.force_side_candidates,
            "foregroundContextGate": foreground_context_gate,
            "commitBurst": commit_burst_trigger,
        },
        "refreshDecision": refresh_decision,
        "showDecision": show_decision,
        "predictionTraceEvents": prediction_trace_events,
        "predictionTrigger": commit_burst_trigger,
        "historyContext": prediction_context,
        "historyContextMeta": prediction_context_metadata(prediction_context),
        "latencyBudgetMs": snapshot.latency_budget_ms,
        "uiMode": ui_mode,
        "contextFrame": context_frame_trace_payload(context_frame, include_text=include_context_text),
        "contextViews": context_views_trace_payload(
            rime_view=rime_view,
            model_view=model_view,
            rag_view=rag_view,
            display_view=display_view,
        ),
        "laneStatus": lane_status_payload(
            rag_lane=rag_lane,
            model_lane=model_lane,
            rime_candidate_count=len(snapshot.candidates),
        ),
        "ragLane": rag_lane,
        "modelLane": model_lane,
        "rimeContext": rime_context_to_payload(snapshot),
        "modelPredictions": [model_prediction_to_payload(item) for item in model_predictions],
        "ragCandidates": rag_candidate_payloads,
        "displayCandidates": display_candidate_payloads,
        "candidatePanel": candidate_panel_payload,
        "assistantOverlay": assistant_overlay_payload,
        "predictionFirst": prediction_first_payload,
        "predictionSession": prediction_session_payload,
        "keyPolicy": key_policy,
        "selectionActions": {
            "rime": "select_rime_candidate",
            "side": "commit_side_candidate",
        },
        "progressive": progressive_state,
        "mergePolicy": {
            "rimeFirst": False,
            "sideFirst": True,
            "maxVisibleCandidates": snapshot.max_visible_candidates,
            "maxSideCandidates": snapshot.max_side_candidates,
            "reservedSideSlots": min(snapshot.max_side_candidates, snapshot.max_visible_candidates),
            "maxRimeVisibleCandidates": max(0, snapshot.max_visible_candidates - min(snapshot.max_side_candidates, snapshot.max_visible_candidates)),
            "maxModelSideCandidates": max_model_side_candidates(snapshot.max_side_candidates),
            "ragBlockReserve": rag_block_reserve(snapshot.max_side_candidates),
            "ragKeepsRemainingSideSlots": False,
            "ragDirectDisplayEnabled": (
                snapshot.force_side_candidates
                or debug_use_legacy_rag_fallback
                or runtime_config.hybrid_rag.direct_display
                if runtime_config is not None
                else rag_direct_display_enabled()
            ),
            "rawPinyinFallback": query_basis == "rawInputFallback",
            "sideCandidatesEnabled": trigger_decision.should_refresh,
            "fallbackOrder": ["model", "rag", "rime"],
        },
    }


def sensitive_input_requested(payload: Mapping[str, object]) -> bool:
    rime_context = payload.get("rimeContext")
    rime_mapping = rime_context if isinstance(rime_context, Mapping) else {}
    foreground = payload.get("foregroundText")
    foreground_mapping = foreground if isinstance(foreground, Mapping) else {}
    explicit_sensitive_flag = any(
        _bool(value, default=False)
        for value in (
            payload.get("sensitiveField"),
            payload.get("secureInput"),
            payload.get("isPasswordField"),
            payload.get("credentialField"),
            rime_mapping.get("sensitiveField"),
            rime_mapping.get("secureInput"),
            rime_mapping.get("isPasswordField"),
            rime_mapping.get("credentialField"),
            foreground_mapping.get("sensitiveField"),
            foreground_mapping.get("secureInput"),
            foreground_mapping.get("isPasswordField"),
            foreground_mapping.get("credentialField"),
        )
    )
    if explicit_sensitive_flag:
        return True
    # Only inspect structured field metadata. Never classify the text itself,
    # so ordinary discussion about password policy or API keys is not blocked.
    credential_kinds = {
        "password",
        "current-password",
        "new-password",
        "passcode",
    }
    for mapping in (payload, rime_mapping, foreground_mapping):
        for key in ("fieldType", "inputPurpose", "autocomplete", "textContentType", "fieldRole"):
            value = compact_whitespace(str(mapping.get(key) or "")).lower().replace("_", "-")
            if value in credential_kinds:
                return True
    return False


def _sensitive_field_response(
    *,
    payload: Mapping[str, object],
    default_project: str,
    privacy_assessment: Mapping[str, object],
) -> dict[str, object]:
    reason = (
        "sensitive_field"
        if str(privacy_assessment.get("disposition") or "") == "sensitive"
        else "privacy_unknown"
    )
    response = _hard_clear_prediction_response(
        payload=payload,
        default_project=default_project,
        reason=reason,
        redact_transaction=True,
    )
    response.update(
        {
            "stored": False,
            "noStore": True,
            "privacyAssessment": dict(privacy_assessment),
            "storageReceipt": storage_receipt(privacy_assessment, stored=False),
        }
    )
    return response


def _runtime_hard_clear_response(
    *,
    payload: Mapping[str, object],
    default_project: str,
    reason: str,
) -> dict[str, object]:
    return _hard_clear_prediction_response(
        payload=payload,
        default_project=default_project,
        reason=reason,
        redact_transaction=False,
    )


def _hard_clear_prediction_response(
    *,
    payload: Mapping[str, object],
    default_project: str,
    reason: str,
    redact_transaction: bool,
) -> dict[str, object]:
    """Return a hard-clear response before predictor, retrieval, hashes, or trace text."""

    rime_context = payload.get("rimeContext")
    rime_mapping = rime_context if isinstance(rime_context, Mapping) else {}
    session_id = _string(payload.get("sessionId")) or "default"
    request_seq = _bounded_int(payload.get("requestSeq"), default=0, minimum=0, maximum=2**63 - 1)
    frontend_revision = _bounded_int(
        _first_present(dict(payload), dict(rime_mapping), "frontendRevision"),
        default=_bounded_int(payload.get("inputGeneration"), default=0, minimum=0, maximum=2**63 - 1),
        minimum=0,
        maximum=2**63 - 1,
    )
    selection_epoch = _bounded_int(
        _first_present(dict(payload), dict(rime_mapping), "selectionEpoch"),
        default=frontend_revision,
        minimum=0,
        maximum=2**63 - 1,
    )
    front_app_bundle_id = "" if redact_transaction else _string(
        payload.get("frontAppBundleId")
        or payload.get("frontmostAppBundleId")
        or payload.get("app")
        or rime_mapping.get("frontAppBundleId")
    )
    input_source_id = "" if redact_transaction else _string(
        payload.get("inputSourceId") or rime_mapping.get("inputSourceId")
    )
    composition_hash = "" if redact_transaction else _hash_from_payload(
        _first_present(dict(payload), dict(rime_mapping), "compositionHash"),
        fallback_text="",
    )
    committed_context_hash = "" if redact_transaction else _hash_from_payload(
        _first_present(dict(payload), dict(rime_mapping), "committedContextHash"),
        fallback_text="",
    )
    supplied_panel_session_id = "" if redact_transaction else _string(
        payload.get("panelSessionId") or rime_mapping.get("panelSessionId")
    )
    panel_session_id = supplied_panel_session_id or _short_stable_id(
        session_id,
        str(request_seq),
        str(frontend_revision),
        str(selection_epoch),
        reason,
    )
    transaction = FrontendTransaction(
        frontend_revision=frontend_revision,
        selection_epoch=selection_epoch,
        input_generation=_bounded_int(
            _first_present(dict(payload), dict(rime_mapping), "inputGeneration"),
            default=frontend_revision,
            minimum=0,
            maximum=2**63 - 1,
        ),
        front_app_bundle_id=front_app_bundle_id,
        input_source_id=input_source_id,
        composition_hash=composition_hash,
        committed_context_hash=committed_context_hash,
        panel_session_id=panel_session_id,
        created_at_ms=(
            0
            if redact_transaction
            else _bounded_int(
                _first_present(dict(payload), dict(rime_mapping), "createdAtMs"),
                default=0,
                minimum=0,
                maximum=2**63 - 1,
            )
        ),
    )
    snapshot = RimeContextSnapshot(
        session_id=session_id,
        request_seq=request_seq,
        project=("" if redact_transaction else (_string(payload.get("project")) or default_project)),
        app=front_app_bundle_id,
        highlighted_index=0,
        page=(0 if redact_transaction else _bounded_int(rime_mapping.get("page", payload.get("page")), default=0, minimum=0, maximum=999)),
        is_last_page=(True if redact_transaction else _bool(rime_mapping.get("isLastPage", payload.get("isLastPage")), default=True)),
        latency_budget_ms=(0 if redact_transaction else _bounded_int(payload.get("latencyBudgetMs"), default=0, minimum=0, maximum=20000)),
        max_visible_candidates=(8 if redact_transaction else _bounded_int(payload.get("maxVisibleCandidates"), default=8, minimum=1, maximum=10)),
        max_side_candidates=(0 if redact_transaction else _bounded_int(payload.get("maxSideCandidates"), default=0, minimum=0, maximum=10)),
        frontend_transaction=transaction,
    )
    frontend_transaction_payload = frontend_transaction_to_payload(transaction)
    prediction_session_payload: dict[str, object] = {
        "phase": reason,
        "inputMode": reason,
        "pinyinPrefix": "",
        "candidatePanelVisible": False,
        "predictionPanelVisible": False,
        "shouldClearPredictionPanel": True,
        "clearReason": reason,
        "selectionScope": "none",
        "rimeCompositionOwnedByRime": True,
        "sessionFingerprint": _short_stable_id(session_id, str(request_seq), reason),
        "contextFingerprint": "",
        "snapshotId": "",
        "stableSnapshotId": "",
        "requestSeq": request_seq,
        "expiresAfterMs": 0,
        **frontend_transaction_payload,
    }
    key_policy = contract_key_policy_for_prediction_session(prediction_session_payload)
    progressive = _progressive_state(enabled=False, partial=False, should_follow_up=False)
    candidate_panel = build_candidate_panel_payload(input_mode=reason, display_candidates=[])
    assistant_overlay = build_assistant_overlay_payload(
        ui_mode=reason,
        input_mode=reason,
        display_candidates=[],
        rag_candidates=[],
        prediction_session=prediction_session_payload,
        key_policy=key_policy,
        progressive=progressive,
        frontend_transaction=frontend_transaction_payload,
    )
    assistant_overlay["dismissReason"] = reason
    rag_lane = _rag_lane_status(
        called=False,
        timed_out=False,
        skipped_reason=reason,
        budget_ms=0,
    )
    model_lane = _model_lane_status(
        called=False,
        timed_out=False,
        skipped_reason=reason,
        budget_ms=0,
        request_type=PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
        requested_max_candidates=0,
    )
    trace_events = [
        {
            "event": f"prediction_{reason}_blocked",
            "fields": {"reason": reason},
        }
    ]
    return {
        "schemaVersion": RIME_SIDECAR_SCHEMA_VERSION,
        "sessionId": session_id,
        "requestSeq": request_seq,
        **frontend_transaction_payload,
        "frontendTransaction": frontend_transaction_payload,
        "project": snapshot.project,
        "rawInput": "",
        "preedit": "",
        "commitTextPreview": "",
        "committedContext": "",
        "semanticQuery": "",
        "queryBasis": reason,
        "triggerDecision": {
            "shouldRefresh": False,
            "reason": reason,
            "refreshReason": reason,
            "shouldShow": False,
            "showReason": reason,
            "hardClear": True,
            "hardClearReason": reason,
            "softHold": False,
            "softHoldReason": "",
            "idleMs": 0,
            "semanticSignalLength": 0,
            "forceSideCandidates": False,
            "foregroundContextGate": {"allowed": False, "reason": reason},
        },
        "refreshDecision": {
            "shouldRefresh": False,
            "refreshReason": reason,
            "reason": reason,
            "idleMs": 0,
            "semanticSignalLength": 0,
            "forceSideCandidates": False,
        },
        "showDecision": {
            "shouldShow": False,
            "showReason": reason,
            "action": "hard_clear",
            "hardClear": True,
            "hardClearReason": reason,
            "softHold": False,
            "softHoldReason": "",
            "visibleCandidateCount": 0,
            "snapshotId": "",
        },
        "predictionTraceEvents": trace_events,
        "predictionTrigger": {
            "enforced": False,
            "reason": reason,
            "traceEvent": "prediction_trigger_skipped",
        },
        "historyContext": "",
        "latencyBudgetMs": 0,
        "uiMode": reason,
        "laneStatus": lane_status_payload(rag_lane=rag_lane, model_lane=model_lane, rime_candidate_count=0),
        "ragLane": rag_lane,
        "modelLane": model_lane,
        "rimeContext": rime_context_to_payload(snapshot),
        "modelPredictions": [],
        "ragCandidates": [],
        "displayCandidates": [],
        "candidatePanel": candidate_panel,
        "assistantOverlay": assistant_overlay,
        "predictionFirst": {
            "enabled": False,
            "mode": reason,
            "pinyinPrefix": "",
            "policy": {"engine": "fail_closed", "reason": reason},
        },
        "predictionSession": prediction_session_payload,
        "keyPolicy": key_policy,
        "selectionActions": {
            "rime": "select_rime_candidate",
            "side": "commit_side_candidate",
        },
        "progressive": progressive,
        "mergePolicy": {
            "rimeFirst": True,
            "sideFirst": False,
            "maxVisibleCandidates": snapshot.max_visible_candidates,
            "maxSideCandidates": 0,
            "reservedSideSlots": 0,
            "maxRimeVisibleCandidates": snapshot.max_visible_candidates,
            "maxModelSideCandidates": 0,
            "ragBlockReserve": 0,
            "ragKeepsRemainingSideSlots": False,
            "ragDirectDisplayEnabled": False,
            "rawPinyinFallback": False,
            "sideCandidatesEnabled": False,
            "fallbackOrder": [],
        },
    }


def suggest_rag_with_latency_budget(
    *,
    adapter: InputMethodAdapter,
    core: CoreClient,
    current_input: str,
    recent_context: str,
    project: str,
    app: str,
    top_k: int,
    latency_budget_ms: int,
    context_group_id: str = "",
    runtime_config: RuntimeConfigSnapshot | None = None,
) -> tuple[list[InputSuggestion], dict[str, object]]:
    budget_ms = max(0, int(latency_budget_ms))
    if budget_ms <= 0:
        return [], _rag_lane_status(
            called=False,
            timed_out=False,
            skipped_reason="no latency budget remaining",
            budget_ms=budget_ms,
        )
    if top_k <= 0:
        return [], _rag_lane_status(
            called=False,
            timed_out=False,
            skipped_reason="no side candidate slot",
            budget_ms=budget_ms,
        )
    retrieval_top_k = max(int(top_k), min(20, int(top_k) * 2 + 4))
    if not _RAG_LANE_SEMAPHORE.acquire(blocking=False):
        return [], _rag_lane_status(
            called=False,
            timed_out=False,
            skipped_reason="RAG lane already running",
            budget_ms=budget_ms,
        )

    done = Event()
    result: dict[str, object] = {"suggestions": []}

    def run_suggest() -> None:
        started = time.perf_counter()
        try:
            if runtime_config is not None and isinstance(core, LocalSqliteCoreClient):
                candidates = core.retrieve_candidates_v3(
                    current_input=current_input,
                    recent_context=recent_context,
                    committed_context=recent_context,
                    project=project,
                    app=app,
                    top_k=retrieval_top_k,
                    source_budget_ms=min(budget_ms, runtime_config.hybrid_rag.budget_ms),
                    context_group_id=context_group_id,
                    context_group_level="document" if context_group_id.startswith("doc:") else "app",
                    enabled_lanes=runtime_config.hybrid_rag.query_lanes(),
                    lane_weights=runtime_config.hybrid_rag.query_weights(),
                )
                result["suggestions"] = memory_candidates_v2_to_input_suggestions(candidates)
                result["retrievalConfig"] = {
                    "memoryEnabled": runtime_config.memory.enabled,
                    "hybridEnabled": runtime_config.hybrid_rag.enabled,
                    "lanes": dict(runtime_config.hybrid_rag.query_lanes()),
                    "weights": dict(runtime_config.hybrid_rag.query_weights()),
                }
            else:
                realtime_adapter = _realtime_rag_adapter(adapter=adapter, core=core, budget_ms=budget_ms)
                result["suggestions"] = realtime_adapter.suggest(
                    SuggestionRequest(
                        current_input=current_input,
                        recent_context=recent_context,
                        project=project,
                        app=app,
                        top_k=retrieval_top_k,
                        context_group_id=context_group_id,
                        context_group_level="document" if context_group_id.startswith("doc:") else "app",
                    )
                )
        except Exception as exc:  # pragma: no cover - defensive fail-open guard
            result["error"] = exc.__class__.__name__
            result["failClosed"] = True
            result["warnings"] = [f"rag_exception:{exc.__class__.__name__}"]
        finally:
            result["elapsedMs"] = int((time.perf_counter() - started) * 1000)
            done.set()
            _RAG_LANE_SEMAPHORE.release()

    Thread(target=run_suggest, name="rag-ime-rag-lane", daemon=True).start()
    if not done.wait(timeout=budget_ms / 1000):
        return [], _rag_lane_status(
            called=True,
            timed_out=True,
            skipped_reason="RAG lane exceeded latency budget",
            budget_ms=budget_ms,
        )

    suggestions = result.get("suggestions")
    if not isinstance(suggestions, list):
        suggestions = []
    error = _string(result.get("error"))
    lane = _rag_lane_status(
        called=True,
        timed_out=False,
        skipped_reason=f"error: {error}" if error else "",
        budget_ms=budget_ms,
        elapsed_ms=_optional_int(result.get("elapsedMs")) or 0,
        suggestion_count=len(suggestions),
        fail_closed=bool(result.get("failClosed")),
        warnings=tuple(str(item) for item in result.get("warnings", []) if str(item)),
    )
    if isinstance(result.get("retrievalConfig"), dict):
        lane["retrievalConfig"] = dict(result["retrievalConfig"])  # type: ignore[arg-type]
    return suggestions, lane


def _realtime_rag_adapter(*, adapter: InputMethodAdapter, core: CoreClient, budget_ms: int) -> InputMethodAdapter:
    if budget_ms > _REALTIME_MODEL_CONTEXT_BUDGET_MS or not isinstance(core, LocalSqliteCoreClient):
        return adapter
    optimizer_enabled = MemoryOptimizerConfig.from_env().enabled
    return InputMethodAdapter(
        LocalSqliteCoreClient(
            core.db_path,
            suggestion_cache_size=core.suggestion_cache_size,
            embedding_provider=NullEmbeddingProvider(),
            vector_candidate_limit=0,
            vector_weight=0.0,
            legacy_governance_filter_enabled=not optimizer_enabled,
            v2_governance_filter_enabled=not optimizer_enabled,
        ),
        project=str(getattr(adapter, "project", "wisdom-weasel-rag-ime")),
    )


def _predictor_provider_name(predictor: PredictionProvider) -> str:
    config = getattr(predictor, "config", None)
    value = getattr(config, "provider_name", "") if config is not None else ""
    return compact_whitespace(str(value or predictor.__class__.__name__))


def _predictor_model_name(predictor: PredictionProvider) -> str:
    config = getattr(predictor, "config", None)
    value = getattr(config, "model", "") if config is not None else ""
    return compact_whitespace(str(value or ""))


def _looks_like_deepseek_provider(predictor: PredictionProvider) -> bool:
    identity = f"{_predictor_provider_name(predictor)} {_predictor_model_name(predictor)}".lower()
    return "deepseek" in identity




def _post_commit_online_context_packet(
    *,
    current_input: str,
    context_group_id: str,
    app: str,
    project: str,
    suggestions: list[InputSuggestion],
) -> dict[str, object]:
    current = now_ms()
    group_events = [
        {
            "text": event.text,
            "ageMs": max(0, current - event.created_at_ms),
            "accepted": event.accepted,
            "deleted": event.deleted,
            "contextGroupId": context_group_id,
        }
        for event in _GROUP_SHORT_BUFFER.recent(context_group_id, limit=2, include_deleted=True)
    ]
    surface_hints = [item.surface_text for item in suggestions if 2 <= len(compact_whitespace(item.surface_text)) <= 18]
    tag_hints = [
        str(tag)
        for suggestion in suggestions
        for tag in dict(suggestion.metadata).get("tags", [])
    ]
    memory_books = [
        {
            "bookId": str(item.metadata.get("memory_id") or item.suggestion_id),
            "title": "",
            "summary": item.evidence_preview,
            "surfaceHints": [item.surface_text],
            "tags": list(item.metadata.get("tags") or []),
            "sourceEventIds": [item.source_event_id] if item.source_event_id > 0 else [],
        }
        for item in suggestions[:2]
    ]
    return build_post_commit_context_packet(
        current_input=current_input,
        context_group_id=context_group_id,
        app=app,
        project=project,
        group_events=group_events,
        memory_books=memory_books,
        tag_hints=tag_hints,
        surface_hints=surface_hints,
        negative_signals=("根据上述", "接下来我们", "可以继续", "候选如下"),
    )


def _group_aware_model_context(
    *,
    context_group_id: str,
    explicit_recent_context: str,
    current_ms: int | None = None,
) -> tuple[str, dict[str, object]]:
    """Join the current editor value with only the recent same-group typing stream."""

    current = compact_whitespace(explicit_recent_context)
    now = now_ms() if current_ms is None else max(0, int(current_ms))
    recent_events = [
        event
        for event in _GROUP_SHORT_BUFFER.recent(context_group_id, limit=20)
        if now - event.created_at_ms <= _MODEL_GROUP_CONTEXT_TTL_MS
    ]
    stream = compact_whitespace("".join(event.text for event in recent_events))
    history = stream
    if current and history.endswith(current):
        history = history[: -len(current)]
    elif current and current.endswith(history):
        history = ""
    else:
        max_overlap = min(len(history), len(current))
        for width in range(max_overlap, 0, -1):
            if history[-width:] == current[:width]:
                history = history[:-width]
                break
    history = compact_whitespace(history)
    if history and current:
        cjk_boundary = re.search(r"[\u3400-\u9fff]", current[:1]) and re.search(
            r"[\u3400-\u9fff，。！？；：、]", history[-1:]
        )
        separator = "" if cjk_boundary else " "
        combined = f"{history}{separator}{current}"
    else:
        combined = history or current
    assembled = compact_whitespace(combined)[-_MODEL_GROUP_CONTEXT_MAX_CHARS:]
    history_chars = max(0, len(assembled) - len(current)) if current and assembled.endswith(current) else len(history)
    focused = _focus_model_completion_context(assembled=assembled, foreground=current)
    context_mode = "group-short-buffer+foreground" if history_chars else "foreground-only"
    if focused != assembled:
        context_mode = f"{context_mode}-focused"
    return focused, {
        "contextMode": context_mode,
        "foregroundContextChars": len(current),
        "groupContextChars": history_chars,
        "groupContextEventCount": len(recent_events),
        "assembledContextChars": len(assembled),
        "modelContextChars": len(focused),
        "contextWindowChars": _MODEL_COMPLETION_CONTEXT_MAX_CHARS,
        "contextWindowTrimmed": focused != assembled,
        "modelContextHash": stable_text_hash(focused),
    }


def _focus_model_completion_context(*, assembled: str, foreground: str) -> str:
    """Keep the current field plus enough same-group history to disambiguate it."""

    combined = compact_whitespace(assembled)
    current = compact_whitespace(foreground)
    if not combined:
        return ""

    # A meaningful current field is still the strongest signal. Very short
    # fragments borrow the same-group buffer, while a complete current field
    # keeps its whole tail instead of being collapsed to the last few words.
    source = current if len(current) >= 8 else combined
    return compact_whitespace(source)[-_MODEL_COMPLETION_CONTEXT_MAX_CHARS:]


def _annotate_model_prediction_context(
    predictions: list[ModelPrediction],
    context_meta: Mapping[str, object],
) -> list[ModelPrediction]:
    if not predictions:
        return []
    metadata_patch = {
        key: value
        for key, value in context_meta.items()
        if key
        in {
            "contextMode",
            "foregroundContextChars",
            "groupContextChars",
            "groupContextEventCount",
            "assembledContextChars",
            "modelContextChars",
            "contextWindowChars",
            "contextWindowTrimmed",
            "modelContextHash",
        }
    }
    return [
        ModelPrediction(
            text=item.text,
            rank=item.rank,
            provider_name=item.provider_name,
            latency_ms=item.latency_ms,
            confidence=item.confidence,
            metadata={**dict(item.metadata), **metadata_patch},
        )
        for item in predictions
    ]


def _model_prediction_decode_metadata(predictions: list[ModelPrediction]) -> dict[str, object]:
    if not predictions:
        return {}
    metadata = dict(predictions[0].metadata)
    timing = metadata.get("server_timing")
    if not isinstance(timing, Mapping):
        return {}
    allowed = {
        "candidateMode",
        "decodeMode",
        "sharedPrefill",
        "prefillMs",
        "batchMs",
        "branchCount",
        "plannedSeedIndexes",
        "qualityReranked",
        "contextDomainTermCount",
    }
    return {
        key: value
        for key, value in timing.items()
        if key in allowed
    }


def _bounded_float_env(name: str, *, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get(name, str(default)))))
    except ValueError:
        return default


def run_side_lanes_with_latency_budget(
    *,
    adapter: InputMethodAdapter,
    core: CoreClient,
    predictor: PredictionProvider,
    deepseek_completion_provider: Any | None = None,
    snapshot: RimeContextSnapshot,
    current_input: str,
    query_basis: str,
    recent_context: str,
    explicit_recent_context: str,
    project: str,
    app: str,
    top_k: int,
    max_candidates: int,
    latency_budget_ms: int,
    context_group_id: str = "",
    runtime_config: RuntimeConfigSnapshot | None = None,
) -> tuple[list[InputSuggestion], dict[str, object], list[ModelPrediction], dict[str, object], dict[str, object]]:
    started = time.perf_counter()
    rag_result: dict[str, object] = {}
    model_result: dict[str, object] = {}
    rag_budget_ms = _rag_lane_budget_for_request(
        latency_budget_ms,
        runtime_config=runtime_config,
    )
    model_budget_ms = _model_lane_budget_for_request(
        latency_budget_ms,
        snapshot=snapshot,
        runtime_config=runtime_config,
    )
    request_type = model_request_type_for_snapshot(snapshot)
    post_commit_prediction = _is_post_commit_prediction_snapshot(snapshot)
    post_commit_auto_model = (not post_commit_prediction) or post_commit_auto_model_enabled()
    post_commit_async = post_commit_prediction and post_commit_auto_model and post_commit_async_completion_enabled()
    rag_retrieval_enabled = bool(
        runtime_config is None
        or (runtime_config.hybrid_rag.enabled and runtime_config.memory.enabled)
    )
    if post_commit_async:
        request_type = PREDICTION_REQUEST_POST_COMMIT_COMPLETION
    model_candidate_limit = realtime_model_candidate_limit(max_candidates)
    local_model_candidate_limit = (
        max(0, model_candidate_limit - 1)
        if post_commit_prediction
        and deepseek_completion_provider is not None
        and deepseek_scene_enabled("post_commit")
        else model_candidate_limit
    )
    # The progressive DeepSeek lane below needs the candidate texts, not just
    # how many there are. It previously referenced `rime_candidate_texts`,
    # which is only bound in `_predict_deepseek_post_commit_candidates`'s
    # caller further down this module, so that lane raised NameError whenever
    # it ran. Binding the texts here uses the identical expression that
    # sibling already uses, and the count stays exactly what it was: the same
    # slice and the same filter, now measured off the tuple.
    rime_candidate_texts = tuple(
        compact_whitespace(item.text)
        for item in snapshot.candidates[:10]
        if compact_whitespace(item.text)
    )
    rime_candidate_count = len(rime_candidate_texts)
    rag_current_input = current_input
    model_current_input = current_input
    if request_type == PREDICTION_REQUEST_PINYIN_CONSTRAINED:
        prefix = stable_short_pinyin_prefix(snapshot)
        if prefix:
            model_current_input = prefix
    elif post_commit_prediction:
        model_current_input = ""
    model_recent_context = explicit_recent_context
    model_context_meta: dict[str, object] = {
        "contextMode": "foreground-only",
        "foregroundContextChars": len(compact_whitespace(explicit_recent_context)),
        "groupContextChars": 0,
        "groupContextEventCount": 0,
        "modelContextChars": len(compact_whitespace(explicit_recent_context)),
        "modelContextHash": stable_text_hash(explicit_recent_context),
    }
    if post_commit_prediction:
        model_recent_context, model_context_meta = _group_aware_model_context(
            context_group_id=context_group_id,
            explicit_recent_context=explicit_recent_context,
        )
    if post_commit_async and not snapshot.progressive_follow_up:
        # Start the resident local model before retrieval. The previous T0 RAG
        # probe ran synchronously here, so retrieval could hold the sub-100 ms
        # shared-prefill model path behind SQLite/vector work for much longer.
        # RAG remains a progressive lane and is appended by the follow-up.
        online_context_packet = _post_commit_online_context_packet(
            current_input=explicit_recent_context,
            context_group_id=context_group_id,
            app=app,
            project=project,
            suggestions=[],
        )
        predictions, model_lane = run_post_commit_completion_async(
            core=core,
            predictor=predictor,
            snapshot=snapshot,
            explicit_recent_context=model_recent_context,
            project=project,
            max_candidates=local_model_candidate_limit,
            model_budget_ms=model_budget_ms,
            runtime_config=runtime_config,
        )
        model_lane["contextPacket"] = online_context_packet
        model_lane.update(model_context_meta)
        predictions = _annotate_model_prediction_context(predictions, model_context_meta)
        model_lane.update(_model_prediction_decode_metadata(predictions))
        rag_lane = _rag_lane_status(
            called=False,
            timed_out=False,
            skipped_reason=(
                "RAG lane deferred behind model-first response"
                if rag_retrieval_enabled
                else "RAG/memory disabled by effective runtime config"
            ),
            budget_ms=rag_budget_ms,
        )
        if rag_retrieval_enabled:
            rag_lane["pending"] = True
            rag_lane["modelFirstDeferred"] = True
        else:
            rag_lane["runtimeDisabled"] = True
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        rag_lane.update(
            {
                "totalLatencyBudgetMs": latency_budget_ms,
                "sideLaneMode": "post_commit_async_first_response",
                "sideLaneElapsedMs": elapsed_ms,
                "memoryOptimizer": {"enabled": False, "traceEnabled": False, "maxMs": 0},
            }
        )
        model_lane.update(
            {
                "totalLatencyBudgetMs": latency_budget_ms,
                "elapsedBeforeModelMs": 0,
                "sideLaneMode": "post_commit_async_first_response",
                "sideLaneElapsedMs": elapsed_ms,
            }
        )
        pending_lanes = _progressive_pending_lanes(rag_lane=rag_lane, model_lane=model_lane)
        return [], rag_lane, predictions, model_lane, _progressive_state(
            enabled=progressive_sidecar_updates_enabled(),
            partial=bool(pending_lanes),
            should_follow_up=bool(pending_lanes),
            pending_lanes=pending_lanes,
            first_response_budget_ms=post_commit_first_response_budget_ms(),
            retry_after_ms=post_commit_completion_poll_wait_ms(),
        )
    lane_token = _SIDE_LANE_SCHEDULER.begin(_side_lane_request_token(snapshot, current_input, query_basis))

    def run_rag() -> None:
        if not rag_retrieval_enabled:
            rag_result["suggestions"] = []
            rag_result["lane"] = _rag_lane_status(
                called=False,
                timed_out=False,
                skipped_reason="RAG/memory disabled by effective runtime config",
                budget_ms=rag_budget_ms,
            )
            rag_result["lane"]["runtimeDisabled"] = True  # type: ignore[index]
            return
        if not _SIDE_LANE_SCHEDULER.is_latest(lane_token):
            rag_result["suggestions"] = []
            rag_result["lane"] = _stale_lane_status(
                lane="rag",
                reason="superseded_before_rag",
                budget_ms=rag_budget_ms,
                token=lane_token,
            )
            return
        suggestions, lane = suggest_rag_with_latency_budget(
            adapter=adapter,
            core=core,
            current_input=rag_current_input,
            recent_context=recent_context,
            project=project,
            app=app,
            top_k=top_k,
            latency_budget_ms=rag_budget_ms,
            context_group_id=context_group_id,
            runtime_config=runtime_config,
        )
        if "RAG lane already running" in _string(lane.get("skippedReason")):
            lane["pending"] = True
        if not _SIDE_LANE_SCHEDULER.is_latest(lane_token):
            suggestions = []
            lane.update(_stale_lane_fields("superseded_after_rag", lane_token))
        lane["queryInput"] = rag_current_input
        rag_result["suggestions"] = suggestions
        rag_result["lane"] = lane

    def run_model() -> None:
        if not _SIDE_LANE_SCHEDULER.is_latest(lane_token):
            model_result["predictions"] = []
            model_result["lane"] = _stale_lane_status(
                lane="model",
                reason="superseded_before_model",
                budget_ms=model_budget_ms,
                token=lane_token,
                request_type=request_type,
                rime_candidate_count=rime_candidate_count,
                requested_max_candidates=model_candidate_limit,
            )
            return
        if post_commit_async:
            predictions, lane = run_post_commit_completion_async(
                core=core,
                predictor=predictor,
                snapshot=snapshot,
                explicit_recent_context=model_recent_context,
                project=project,
                max_candidates=local_model_candidate_limit,
                runtime_config=runtime_config,
            )
            # Keep T0 local-model latency unchanged. On the progressive
            # follow-up, Flash can spend the larger budget on governed Atom
            # and Book evidence before producing its candidate.
            if snapshot.progressive_follow_up and deepseek_completion_provider is not None:
                deepseek_predictions, deepseek_lane = _predict_deepseek_post_commit_candidates(
                    provider=deepseek_completion_provider,
                    core=core,
                    snapshot=snapshot,
                    current_context=model_recent_context,
                    existing_predictions=predictions,
                    project=project,
                    max_candidates=model_candidate_limit,
                    started=started,
                    budget_ms=model_budget_ms,
                    rime_candidates=rime_candidate_texts,
                    context_group_id=context_group_id,
                )
                lane["deepseekProgressive"] = True
                lane.update(deepseek_lane)
                predictions = [*predictions, *deepseek_predictions]
            model_result["predictions"] = predictions
            model_result["lane"] = lane
            return
        if post_commit_prediction and not post_commit_auto_model:
            model_result["predictions"] = []
            model_result["lane"] = _model_lane_status(
                called=False,
                timed_out=False,
                skipped_reason="post-commit auto model disabled; use Active RAG action candidate",
                budget_ms=model_budget_ms,
                request_type=PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
                rime_candidate_count=rime_candidate_count,
                requested_max_candidates=model_candidate_limit,
            )
            return
        if not post_commit_prediction and not composing_model_enabled():
            model_result["predictions"] = []
            model_result["lane"] = _model_lane_status(
                called=False,
                timed_out=False,
                skipped_reason="model lane disabled before post-commit",
                budget_ms=model_budget_ms,
                request_type=request_type,
                rime_candidate_count=rime_candidate_count,
                requested_max_candidates=model_candidate_limit,
            )
            return
        if request_type == PREDICTION_REQUEST_PINYIN_CONSTRAINED and not pinyin_constrained_model_enabled():
            model_result["predictions"] = []
            model_result["lane"] = _model_lane_status(
                called=False,
                timed_out=False,
                skipped_reason="pinyin constrained model disabled",
                budget_ms=model_budget_ms,
                request_type=request_type,
                rime_candidate_count=rime_candidate_count,
                requested_max_candidates=model_candidate_limit,
            )
            return
        if post_commit_prediction:
            if _looks_like_deepseek_provider(predictor) or deepseek_completion_provider is not None:
                try:
                    assert_deepseek_scene_allowed("post_commit")
                except RuntimeError as exc:
                    model_result["predictions"] = []
                    model_result["lane"] = _model_lane_status(
                        called=False,
                        timed_out=False,
                        skipped_reason=str(exc),
                        budget_ms=model_budget_ms,
                        request_type=request_type,
                        rime_candidate_count=rime_candidate_count,
                        requested_max_candidates=model_candidate_limit,
                    )
                    return
        else:
            try:
                assert_deepseek_not_called(
                    "passive_per_key",
                    provider_name=_predictor_provider_name(predictor),
                    model=_predictor_model_name(predictor),
                )
            except RuntimeError as exc:
                model_result["predictions"] = []
                model_result["lane"] = _model_lane_status(
                    called=False,
                    timed_out=False,
                    skipped_reason=str(exc),
                    budget_ms=model_budget_ms,
                    request_type=request_type,
                    rime_candidate_count=rime_candidate_count,
                    requested_max_candidates=model_candidate_limit,
                )
                return
        predictions, lane = predict_model_with_latency_budget(
            core=core,
            predictor=predictor,
            deepseek_completion_provider=deepseek_completion_provider,
            snapshot=snapshot,
            current_input=model_current_input,
            explicit_recent_context=model_recent_context,
            project=project,
            max_candidates=model_candidate_limit,
            latency_budget_ms=model_budget_ms,
            memory_enabled=runtime_config.memory.enabled if runtime_config is not None else True,
            context_group_id=context_group_id,
        )
        if not _SIDE_LANE_SCHEDULER.is_latest(lane_token):
            predictions = []
            lane.update(_stale_lane_fields("superseded_after_model", lane_token))
        model_result["predictions"] = predictions
        model_result["lane"] = lane

    rag_thread = Thread(target=run_rag, name="rag-ime-sidecar-rag-dispatch", daemon=True)
    model_thread = Thread(target=run_model, name="rag-ime-sidecar-model-dispatch", daemon=True)
    threads = [rag_thread, model_thread]
    for thread in threads:
        thread.start()
    response_budget_ms = max(0, int(latency_budget_ms))
    if post_commit_prediction and not snapshot.progressive_follow_up:
        response_budget_ms = min(response_budget_ms, post_commit_side_lane_response_budget_ms())
    deadline = started + response_budget_ms / 1000
    progressive_enabled = progressive_sidecar_updates_enabled()
    allow_progressive_first_response = progressive_enabled and not snapshot.progressive_follow_up
    first_response_budget_ms = (
        post_commit_first_response_budget_ms()
        if post_commit_async
        else progressive_first_response_budget_ms(latency_budget_ms)
    )
    progressive_deadline = started + first_response_budget_ms / 1000
    progressive_partial = False
    while True:
        now = time.perf_counter()
        if not rag_thread.is_alive() and not model_thread.is_alive():
            break
        if now >= deadline:
            break
        if (
            allow_progressive_first_response
            and now >= progressive_deadline
            and _has_progressive_visible_lane_result(
                rag_result=rag_result,
                model_result=model_result,
                snapshot=snapshot,
                semantic_query=current_input,
                query_basis=query_basis,
            )
        ):
            progressive_partial = True
            break
        time.sleep(min(0.01, max(0.001, deadline - now)))

    suggestions = rag_result.get("suggestions")
    if not isinstance(suggestions, list):
        suggestions = []
    rag_lane = rag_result.get("lane")
    rag_pending = rag_thread.is_alive()
    if not isinstance(rag_lane, dict):
        pending_after_response = bool((progressive_partial or post_commit_prediction) and rag_pending)
        rag_lane = _rag_lane_status(
            called=True,
            timed_out=not pending_after_response,
            skipped_reason="RAG lane pending after progressive response window"
            if pending_after_response
            else "RAG dispatch exceeded latency budget",
            budget_ms=rag_budget_ms,
        )
        if pending_after_response:
            rag_lane["pending"] = True
    if recent_context_candidate_fallback_enabled() and rag_retrieval_enabled:
        recent_fallback = recent_context_memory_suggestions(
            recent_context=explicit_recent_context or recent_context,
            current_input=current_input,
            top_k=max(0, int(top_k) - len(suggestions)),
        )
        if recent_fallback:
            seen_surfaces = {compact_whitespace(item.surface_text) for item in suggestions}
            for item in recent_fallback:
                if compact_whitespace(item.surface_text) not in seen_surfaces:
                    suggestions.append(item)
                    seen_surfaces.add(compact_whitespace(item.surface_text))
            rag_lane["recentContextFallbackCount"] = len(recent_fallback)
            rag_lane["suggestionCount"] = len(suggestions)

    predictions = model_result.get("predictions")
    if not isinstance(predictions, list):
        predictions = []
    model_lane = model_result.get("lane")
    model_pending = model_thread.is_alive()
    if not isinstance(model_lane, dict):
        predictions = []
        model_lane = _model_lane_status(
            called=True,
            timed_out=not ((progressive_partial or post_commit_prediction) and model_pending),
            skipped_reason="model lane pending after progressive first response"
            if progressive_partial and model_pending
            else "model dispatch exceeded latency budget",
            budget_ms=model_budget_ms,
            request_type=request_type,
            rime_candidate_count=rime_candidate_count,
            requested_max_candidates=model_candidate_limit,
        )
        if (progressive_partial or post_commit_prediction) and model_pending:
            model_lane["pending"] = True

    if isinstance(model_lane, dict):
        model_lane["requestedMaxCandidates"] = model_candidate_limit
        model_lane.update(model_context_meta)
    predictions = _annotate_model_prediction_context(predictions, model_context_meta)
    model_lane.update(_model_prediction_decode_metadata(predictions))
    pending_lanes = _progressive_pending_lanes(rag_lane=rag_lane, model_lane=model_lane)
    should_follow_up = bool(pending_lanes)
    progressive_state = _progressive_state(
        enabled=progressive_enabled,
        partial=progressive_partial,
        should_follow_up=should_follow_up,
        pending_lanes=pending_lanes,
        first_response_budget_ms=first_response_budget_ms,
        retry_after_ms=(
            post_commit_completion_poll_wait_ms()
            if post_commit_prediction
            else progressive_follow_up_retry_ms()
        ),
    )
    return suggestions, rag_lane, predictions, model_lane, progressive_state


def run_post_commit_completion_async(
    *,
    core: CoreClient,
    predictor: PredictionProvider,
    snapshot: RimeContextSnapshot,
    explicit_recent_context: str,
    project: str,
    max_candidates: int,
    model_budget_ms: int | None = None,
    runtime_config: RuntimeConfigSnapshot | None = None,
) -> tuple[list[ModelPrediction], dict[str, object]]:
    key = build_post_commit_completion_key(
        snapshot=snapshot,
        project=project,
        runtime_config=runtime_config,
        max_candidates=max_candidates,
    )
    wait_ms = (
        post_commit_completion_poll_wait_ms()
        if snapshot.progressive_follow_up
        else post_commit_first_response_budget_ms()
    )
    predictions, lane = _POST_COMMIT_COMPLETION_CACHE.poll_or_start(
        key=key,
        snapshot=snapshot,
        core=core,
        predictor=predictor,
        project=project,
        explicit_recent_context=explicit_recent_context,
        max_candidates=max_candidates,
        ttl_ms=(
            runtime_config.post_commit.completion_ttl_ms
            if runtime_config is not None
            else post_commit_completion_ttl_ms()
        ),
        hard_timeout_ms=min(
            (
                runtime_config.post_commit.model_hard_timeout_ms
                if runtime_config is not None
                else post_commit_model_hard_timeout_ms()
            ),
            (
                max(300, int(model_budget_ms))
                if model_budget_ms is not None
                else (
                    runtime_config.post_commit.model_budget_ms
                    if runtime_config is not None
                    else _bounded_int(
                        os.environ.get("RAG_IME_POST_COMMIT_MODEL_BUDGET_MS"),
                        default=900,
                        minimum=300,
                        maximum=12000,
                    )
                )
            ),
        ),
        wait_ms=wait_ms,
        memory_enabled=runtime_config.memory.enabled if runtime_config is not None else True,
    )
    return predictions, lane


def build_post_commit_completion_key(
    *,
    snapshot: RimeContextSnapshot,
    project: str,
    runtime_config: RuntimeConfigSnapshot | None = None,
    max_candidates: int = 0,
) -> PostCommitCompletionKey:
    transaction = snapshot.frontend_transaction
    context_text = compact_whitespace(snapshot.committed_context)
    commit_preview = compact_whitespace(snapshot.commit_text_preview)
    app = transaction.front_app_bundle_id or snapshot.app
    committed_hash = transaction.committed_context_hash or stable_text_hash(context_text)
    return PostCommitCompletionKey(
        project=project,
        app=app,
        hard_context_anchor=committed_hash,
        context_fingerprint=_context_fingerprint(context_text),
        commit_text_hash=stable_text_hash(commit_preview),
        commit_text_length=len(commit_preview),
        input_source_id=transaction.input_source_id,
        selection_epoch=transaction.selection_epoch,
        runtime_config_hash=runtime_config.snapshot_hash if runtime_config is not None else "",
        max_candidates=max(0, int(max_candidates)),
        request_type=PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
    )


def _rag_lane_budget_for_request(
    latency_budget_ms: int,
    *,
    runtime_config: RuntimeConfigSnapshot | None = None,
) -> int:
    request_budget_ms = max(0, int(latency_budget_ms))
    if runtime_config is None or request_budget_ms <= 0:
        return request_budget_ms
    return min(request_budget_ms, max(1, int(runtime_config.hybrid_rag.budget_ms)))


def _model_lane_budget_for_request(
    latency_budget_ms: int,
    *,
    snapshot: RimeContextSnapshot,
    runtime_config: RuntimeConfigSnapshot | None = None,
) -> int:
    budget = max(0, int(latency_budget_ms))
    if not _is_post_commit_prediction_snapshot(snapshot):
        return budget
    post_commit_config = getattr(runtime_config, "post_commit", None)
    configured_budget = getattr(post_commit_config, "model_budget_ms", None)
    if configured_budget is not None:
        return max(300, min(12000, int(configured_budget)))
    configured = _bounded_int(
        os.environ.get("RAG_IME_POST_COMMIT_MODEL_BUDGET_MS"),
        default=900,
        minimum=300,
        maximum=12000,
    )
    return max(budget, configured)


def prediction_status_rows_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_PREDICTION_STATUS_ROW", "1")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def _prediction_status_row_for_response(
    *,
    progressive_state: Mapping[str, object],
    rag_lane: Mapping[str, object],
    model_lane: Mapping[str, object],
    generation: int,
    input_mode: str = "",
) -> SideCandidateDisplayItem | None:
    if not prediction_status_rows_enabled():
        return None
    if input_mode != InputMode.POST_COMMIT_PREDICTING.value:
        return None
    pending_lanes = progressive_state.get("pendingLanes")
    pending = {compact_whitespace(str(item)).lower() for item in pending_lanes} if isinstance(pending_lanes, list) else set()
    rag_pending = "rag" in pending or bool(rag_lane.get("pending"))
    model_pending = "model" in pending or bool(model_lane.get("pending"))
    rag_state = lane_status_state(rag_lane, count_key="suggestionCount", pending=rag_pending)
    model_state = lane_status_state(model_lane, count_key="predictionCount", pending=model_pending)
    waiting_ms = max(
        _optional_int(rag_lane.get("sideLaneElapsedMs")) or _optional_int(rag_lane.get("elapsedMs")) or 0,
        _optional_int(model_lane.get("sideLaneElapsedMs")) or _optional_int(model_lane.get("elapsedMs")) or 0,
        _bounded_int(progressive_state.get("firstResponseBudgetMs"), default=0, minimum=0, maximum=10000),
    )
    return prediction_status_row(
        rag_pending=rag_pending,
        model_pending=model_pending,
        waiting_ms=waiting_ms,
        latest_generation=generation,
        rag_state=rag_state,
        model_state=model_state,
        trigger="rime_candidate_commit",
    )


def lane_status_payload(
    *,
    rag_lane: Mapping[str, object],
    model_lane: Mapping[str, object],
    rime_candidate_count: int,
) -> dict[str, object]:
    return {
        "rag": {
            "state": lane_status_state(rag_lane, count_key="suggestionCount"),
            "elapsedMs": lane_elapsed_ms(rag_lane),
            "candidateCount": _bounded_int(rag_lane.get("suggestionCount"), default=0, minimum=0, maximum=999),
            "displayCandidateCount": _bounded_int(
                rag_lane.get("displaySuggestionCount"),
                default=_bounded_int(rag_lane.get("suggestionCount"), default=0, minimum=0, maximum=999),
                minimum=0,
                maximum=999,
            ),
            "directDisplaySuppressed": bool(rag_lane.get("directDisplaySuppressed")),
            "dropReason": _string(rag_lane.get("dropReason") or rag_lane.get("staleReason") or rag_lane.get("skippedReason")),
        },
        "model": {
            "state": lane_status_state(model_lane, count_key="predictionCount"),
            "elapsedMs": lane_elapsed_ms(model_lane),
            "candidateCount": _bounded_int(model_lane.get("predictionCount"), default=0, minimum=0, maximum=999),
            "dropReason": _string(
                model_lane.get("dropReason") or model_lane.get("staleReason") or model_lane.get("skippedReason")
            ),
        },
        "rime": {
            "state": "ready" if rime_candidate_count > 0 else "empty",
            "candidateCount": max(0, int(rime_candidate_count)),
        },
    }


def lane_status_state(lane: Mapping[str, object], *, count_key: str, pending: bool | None = None) -> str:
    if pending is True or bool(lane.get("pending")):
        return "pending"
    if bool(lane.get("stale")) or bool(lane.get("droppedStale")) or _string(lane.get("dropReason")):
        return "stale_dropped"
    if bool(lane.get("timedOut")):
        return "timeout"
    if bool(lane.get("error")):
        return "error"
    count = _bounded_int(lane.get(count_key), default=0, minimum=0, maximum=999)
    if count > 0:
        return "ready"
    if bool(lane.get("called")):
        return "empty"
    return "idle"


def lane_elapsed_ms(lane: Mapping[str, object]) -> int:
    return max(
        0,
        _optional_int(lane.get("sideLaneElapsedMs"))
        or _optional_int(lane.get("elapsedMs"))
        or _optional_int(lane.get("latencyMs"))
        or 0,
    )


def _side_lane_request_token(snapshot: RimeContextSnapshot, semantic_query: str, query_basis: str) -> LaneRequestToken:
    anchors = build_prediction_anchors_from_snapshot(
        snapshot=snapshot,
        mode=infer_input_mode(snapshot).value,
        semantic_query=semantic_query,
        query_basis=query_basis,
        stable_short_pinyin_prefix=stable_short_pinyin_prefix(snapshot),
    )
    transaction = snapshot.frontend_transaction
    return LaneRequestToken(
        session_id=snapshot.session_id,
        panel_session_id=transaction.panel_session_id,
        frontend_revision=transaction.frontend_revision,
        input_generation=snapshot.request_seq,
        apply_anchor=anchors.apply_anchor,
        query_anchor=anchors.query_anchor,
        created_at_ms=now_ms(),
    )


def _stale_lane_fields(reason: str, token: LaneRequestToken) -> dict[str, object]:
    return {
        "staleDropped": True,
        "staleDropReason": reason,
        "waitingForLatest": True,
        "activeGeneration": token.input_generation,
        "applyAnchor": token.apply_anchor,
        "queryAnchor": token.query_anchor,
    }


def _stale_lane_status(
    *,
    lane: str,
    reason: str,
    budget_ms: int,
    token: LaneRequestToken,
    request_type: str = "",
    rime_candidate_count: int = 0,
    requested_max_candidates: int = 0,
) -> dict[str, object]:
    if lane == "model":
        status = _model_lane_status(
            called=False,
            timed_out=False,
            skipped_reason=reason,
            budget_ms=budget_ms,
            request_type=request_type,
            rime_candidate_count=rime_candidate_count,
            requested_max_candidates=requested_max_candidates,
        )
    else:
        status = _rag_lane_status(
            called=False,
            timed_out=False,
            skipped_reason=reason,
            budget_ms=budget_ms,
        )
    status.update(_stale_lane_fields(reason, token))
    return status


def progressive_sidecar_updates_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_PROGRESSIVE_DISPLAY", "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def progressive_first_response_budget_ms(latency_budget_ms: int, env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    configured = _bounded_int(
        source.get("RAG_IME_PROGRESSIVE_FIRST_RESPONSE_MS"),
        default=_PROGRESSIVE_FIRST_RESPONSE_MS,
        minimum=120,
        maximum=2000,
    )
    budget = max(0, int(latency_budget_ms))
    if budget <= 0:
        return configured
    if budget <= configured + 80:
        return budget
    return min(configured, max(80, budget - 80))


def post_commit_first_response_budget_ms(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    configured = _bounded_int(
        source.get("RAG_IME_POST_COMMIT_FIRST_RESPONSE_MS"),
        default=_POST_COMMIT_FIRST_RESPONSE_MS,
        minimum=80,
        maximum=1000,
    )
    return configured


def post_commit_completion_poll_wait_ms(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_POST_COMMIT_COMPLETION_POLL_WAIT_MS"),
        default=60,
        minimum=20,
        maximum=250,
    )


def post_commit_side_lane_response_budget_ms(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_POST_COMMIT_SIDE_LANE_RESPONSE_MS"),
        default=80,
        minimum=40,
        maximum=300,
    )


def post_commit_completion_ttl_ms(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_POST_COMMIT_COMPLETION_TTL_MS"),
        default=_POST_COMMIT_COMPLETION_TTL_MS,
        minimum=1000,
        maximum=60000,
    )


def post_commit_completion_cache_max_jobs(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_POST_COMMIT_COMPLETION_CACHE_MAX_JOBS"),
        default=_POST_COMMIT_COMPLETION_CACHE_MAX_JOBS,
        minimum=1,
        maximum=128,
    )


def post_commit_model_hard_timeout_ms(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_POST_COMMIT_MODEL_HARD_TIMEOUT_MS"),
        default=_POST_COMMIT_MODEL_HARD_TIMEOUT_MS,
        minimum=500,
        maximum=60000,
    )


def post_commit_async_completion_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_ENABLE_POST_COMMIT_ASYNC_COMPLETION", "1")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def post_commit_auto_model_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(
        source.get(
            "RAG_IME_ENABLE_POST_COMMIT_AUTO_MODEL",
            source.get("RAG_IME_POST_COMMIT_AUTO_LLM", "1"),
        )
    ).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def local_model_quality_gate_mode(env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_LOCAL_MODEL_QUALITY_GATE_MODE", "strict")).strip().lower()
    return "observe" if value in {"observe", "report", "audit"} else "strict"


def ai_after_commit_only_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_AI_AFTER_COMMIT_ONLY", "1")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def post_commit_presentation_stream_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_POST_COMMIT_PRESENTATION_STREAM", "0")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def post_commit_pending_preview_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_POST_COMMIT_PENDING_PREVIEW", "0")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def demo_safe_fallback_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_ENABLE_DEMO_SAFE_FALLBACK", "0")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def post_commit_requires_foreground_context(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_REQUIRE_FOREGROUND_CONTEXT_FOR_POST_COMMIT", "1")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def model_holdover_max_entries(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_MODEL_HOLDOVER_MAX_ENTRIES"),
        default=_MODEL_HOLDOVER_MAX_ENTRIES,
        minimum=0,
        maximum=512,
    )


def prediction_manager_max_entries(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_PREDICTION_MANAGER_MAX_ENTRIES"),
        default=_PREDICTION_MANAGER_MAX_ENTRIES,
        minimum=0,
        maximum=512,
    )


def refresh_debounce_max_entries(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_REFRESH_DEBOUNCE_MAX_ENTRIES"),
        default=_REFRESH_DEBOUNCE_MAX_ENTRIES,
        minimum=1,
        maximum=4096,
    )


def post_commit_presentation_stream_max_entries(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_POST_COMMIT_PRESENTATION_STREAM_MAX_ENTRIES"),
        default=_POST_COMMIT_PRESENTATION_STREAM_MAX_ENTRIES,
        minimum=1,
        maximum=512,
    )


def composing_model_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_ENABLE_COMPOSING_MODEL", "0")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def pinyin_constrained_model_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL", "0")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def progressive_follow_up_retry_ms(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_PROGRESSIVE_FOLLOW_UP_RETRY_MS"),
        default=_PROGRESSIVE_FOLLOW_UP_RETRY_MS,
        minimum=80,
        maximum=1500,
    )


def candidate_source_badges_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_CANDIDATE_SOURCE_BADGES", "1")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def candidate_source_colors_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_CANDIDATE_SOURCE_COLORS", "1")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def candidate_diagnostics_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_CANDIDATE_DIAGNOSTICS", "0")).strip().lower()
    return value not in _FALSEY_ENV_VALUES


def rag_direct_display_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_RAG_DIRECT_DISPLAY", "0")).strip().lower()
    return bool(value) and value not in _FALSEY_ENV_VALUES


def post_commit_active_rag_button_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    # Explicit generation is the stable fallback when the hot model has no
    # suitable completion. Keep the affordance present by default; users can
    # still disable it explicitly for a minimal passive-only surface.
    value = str(source.get("RAG_IME_POST_COMMIT_ACTIVE_RAG_BUTTON", "1")).strip().lower()
    return bool(value) and value not in _FALSEY_ENV_VALUES


def post_commit_action_only_fast_path_enabled(
    snapshot: RimeContextSnapshot,
    env: Mapping[str, str] | None = None,
) -> bool:
    if not _is_post_commit_prediction_snapshot(snapshot):
        return False
    if snapshot.progressive_follow_up:
        return False
    if not post_commit_active_rag_button_enabled(env):
        return False
    if post_commit_auto_model_enabled(env):
        return False
    return not rag_direct_display_enabled(env)


def candidate_source_badge(source_type: str, env: Mapping[str, str] | None = None) -> str:
    if not candidate_source_badges_enabled(env):
        return ""
    return source_badge_for(source_type)


def candidate_color_token(source_type: str, env: Mapping[str, str] | None = None) -> str:
    if not candidate_source_colors_enabled(env):
        return ""
    return source_color_token_for(source_type)


def _strip_candidate_source_suffix(text: str) -> str:
    value = _string(text).strip()
    for suffix in _CANDIDATE_SOURCE_SUFFIXES:
        if not value.endswith(suffix):
            continue
        stripped = value[: -len(suffix)].strip()
        if stripped:
            return stripped
    return value


def _has_progressive_visible_lane_result(
    *,
    rag_result: Mapping[str, object],
    model_result: Mapping[str, object],
    snapshot: RimeContextSnapshot,
    semantic_query: str = "",
    query_basis: str = "",
) -> bool:
    if _is_post_commit_prediction_snapshot(snapshot):
        return True
    suggestions = rag_result.get("suggestions")
    if isinstance(suggestions, list) and suggestions:
        visible_suggestions = _filter_rag_suggestions_for_query(
            suggestions,
            snapshot=snapshot,
            semantic_query=semantic_query,
            query_basis=query_basis,
            prediction_context="",
        )
        if visible_suggestions:
            return True
    predictions = model_result.get("predictions")
    if isinstance(predictions, list) and predictions:
        return True
    if snapshot.force_side_candidates:
        return False
    return bool(snapshot.candidates)


def _progressive_pending_lanes(*, rag_lane: Mapping[str, object], model_lane: Mapping[str, object]) -> list[str]:
    pending: list[str] = []
    if bool(rag_lane.get("pending")):
        pending.append("rag")
    model_reason = _string(model_lane.get("skippedReason"))
    if bool(model_lane.get("pending")) or (
        not bool(model_lane.get("holdoverHit"))
        and model_reason
        and (
            "model lane already running" in model_reason
            or "model lane pending" in model_reason
            or "model dispatch exceeded latency budget" in model_reason
            or "model lane exceeded latency budget" in model_reason
        )
    ):
        pending.append("model")
    return pending


def _progressive_state(
    *,
    enabled: bool,
    partial: bool,
    should_follow_up: bool,
    pending_lanes: list[str] | None = None,
    first_response_budget_ms: int = 0,
    retry_after_ms: int = 0,
) -> dict[str, object]:
    return {
        "enabled": bool(enabled),
        "partial": bool(partial),
        "shouldFollowUp": bool(should_follow_up),
        "pendingLanes": list(pending_lanes or []),
        "firstResponseBudgetMs": max(0, int(first_response_budget_ms)),
        "retryAfterMs": max(0, int(retry_after_ms)),
    }


def realtime_model_candidate_limit(max_candidates: int) -> int:
    configured = _bounded_int(
        os.environ.get("RAG_IME_MODEL_LANE_MAX_CANDIDATES"),
        default=3,
        minimum=1,
        maximum=10,
    )
    return min(max(0, int(max_candidates)), configured)


def _model_lane_lease_ttl_ms() -> int:
    return _bounded_int(
        os.environ.get("RAG_IME_MODEL_LANE_LEASE_TTL_MS"),
        default=_MODEL_LANE_LEASE_TTL_MS,
        minimum=250,
        maximum=30000,
    )


def _try_acquire_model_lane() -> str | None:
    global _MODEL_LANE_ACTIVE_STARTED_AT, _MODEL_LANE_ACTIVE_TOKEN
    now = time.monotonic()
    with _MODEL_LANE_LOCK:
        active_token = _MODEL_LANE_ACTIVE_TOKEN
        if active_token is not None:
            age_ms = int((now - _MODEL_LANE_ACTIVE_STARTED_AT) * 1000)
            if age_ms < _model_lane_lease_ttl_ms():
                return None
        token = f"{now:.9f}:{id(object())}"
        _MODEL_LANE_ACTIVE_TOKEN = token
        _MODEL_LANE_ACTIVE_STARTED_AT = now
        return token


def _release_model_lane(token: str) -> None:
    global _MODEL_LANE_ACTIVE_STARTED_AT, _MODEL_LANE_ACTIVE_TOKEN
    with _MODEL_LANE_LOCK:
        if _MODEL_LANE_ACTIVE_TOKEN != token:
            return
        _MODEL_LANE_ACTIVE_TOKEN = None
        _MODEL_LANE_ACTIVE_STARTED_AT = 0.0


def _model_lane_is_idle() -> bool:
    global _MODEL_LANE_ACTIVE_STARTED_AT, _MODEL_LANE_ACTIVE_TOKEN
    with _MODEL_LANE_LOCK:
        if _MODEL_LANE_ACTIVE_TOKEN is None:
            return True
        age_ms = int((time.monotonic() - _MODEL_LANE_ACTIVE_STARTED_AT) * 1000)
        if age_ms < _model_lane_lease_ttl_ms():
            return False
        _MODEL_LANE_ACTIVE_TOKEN = None
        _MODEL_LANE_ACTIVE_STARTED_AT = 0.0
        return True


def recent_context_memory_suggestions(
    *,
    recent_context: str,
    current_input: str,
    top_k: int,
) -> list[InputSuggestion]:
    if top_k <= 0:
        return []
    context = compact_whitespace(recent_context)
    if len(context) < 12:
        return []
    if not any(keyword in context for keyword in _RECENT_MEMORY_KEYWORDS):
        return []
    current = compact_whitespace(current_input)
    candidates: list[str] = []
    for segment in re.split(r"[，。！？；;,.!?、\n\r]+", context):
        text = compact_whitespace(segment)
        if 4 <= len(text) <= 28:
            candidates.append(text)
        for keyword in _RECENT_MEMORY_KEYWORDS:
            pos = text.find(keyword)
            if pos < 0:
                continue
            start = max(0, pos - 6)
            end = min(len(text), pos + len(keyword) + 12)
            phrase = compact_whitespace(text[start:end])
            if 4 <= len(phrase) <= 24:
                candidates.append(phrase)
    result: list[InputSuggestion] = []
    seen: set[str] = set()
    for text in candidates:
        text = _normalize_recent_memory_surface(text)
        if not text or text in seen or text == current:
            continue
        if _looks_like_broken_recent_memory_surface(text):
            continue
        if text in _RECENT_MEMORY_LOW_VALUE:
            continue
        seen.add(text)
        index = len(result)
        result.append(
            InputSuggestion(
                suggestion_id=f"recent-context-memory:{index}:{_short_stable_id(context[-80:], text)}",
                surface_text=text,
                suggestion_type="memory",
                source_event_id=0,
                evidence_preview=context[-160:],
                confidence=0.72 - (index * 0.02),
                expanded_evidence=context,
                metadata={
                    "source_type": "memory",
                    "memory_id": f"recent-context:{index}",
                    "insert_text": text,
                    "fallback": "recent_context",
                    "debugOnly": True,
                    "governanceLayer": "recent_context",
                    **build_pinyin_metadata(text),
                },
            )
        )
        if len(result) >= top_k:
            break
    return result


def recent_context_candidate_fallback_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Debug-only escape hatch for turning recent screen text into candidates.

    The production IME must not treat the just-typed context as a memory
    candidate. Recent context is still fed to model/RAG retrieval, but direct
    slicing makes the UI behave like a clipboard and crowds out real memories.
    """

    source = env if env is not None else os.environ
    value = str(source.get("RAG_IME_RECENT_CONTEXT_CANDIDATES", "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _normalize_recent_memory_surface(text: str) -> str:
    text = compact_whitespace(text)
    text = re.sub(r"^[,，。！？；;、\s]+|[,，。！？；;、\s]+$", "", text)
    text = re.sub(r"^(的|和|与|及|或|把|再|先|然后)\s*(?=[A-Za-z0-9\u3400-\u9fff])", "", text)
    text = compact_whitespace(text)
    text = _collapse_repeated_recent_memory_surface(text)
    return text


def _looks_like_broken_recent_memory_surface(text: str) -> bool:
    if _is_low_value_recent_memory_surface(text):
        return True
    if re.match(r"^[A-Za-z]{1,2}\s+[A-Za-z0-9\u3400-\u9fff]", text):
        return True
    if re.search(r"[A-Za-z]-$", text):
        return True
    if re.search(r"(^|[\s，,。])[-_][A-Za-z0-9]", text):
        return True
    parts = [part for part in re.split(r"[\s,，、;；。.!?！？/]+", compact_whitespace(text)) if part]
    if len(parts) <= 4 and all(part in _RECENT_MEMORY_TRAILING_NOISE for part in parts):
        return True
    return False


def _collapse_repeated_recent_memory_surface(text: str) -> str:
    surface = compact_whitespace(text)
    parts = [part for part in surface.split(" ") if part]
    if len(parts) < 3:
        return surface
    for width in range(1, min(5, len(parts) // 2 + 1)):
        first = parts[:width]
        second = parts[width : width * 2]
        if first != second:
            continue
        tail = parts[width * 2 :]
        if not tail or all(part in _RECENT_MEMORY_TRAILING_NOISE or len(part) <= 1 for part in tail):
            candidate = compact_whitespace(" ".join(first))
            if candidate and not _is_low_value_recent_memory_surface(candidate):
                return candidate
    return surface


def _is_low_value_recent_memory_surface(text: str) -> bool:
    surface = compact_whitespace(text)
    if not surface:
        return True
    if surface in _RECENT_MEMORY_LOW_VALUE:
        return True
    parts = [part for part in re.split(r"[\s,，、;；。.!?！？/]+", surface) if part]
    if not parts:
        return True
    if len(parts) <= 4 and all(part in _RECENT_MEMORY_TRAILING_NOISE for part in parts):
        return True
    return False


def _filter_rag_suggestions_for_query(
    suggestions: list[InputSuggestion],
    *,
    snapshot: RimeContextSnapshot,
    semantic_query: str,
    query_basis: str,
    prediction_context: str = "",
) -> list[InputSuggestion]:
    result: list[InputSuggestion] = []
    seen: set[str] = set()
    for suggestion in suggestions:
        surface = compact_whitespace(suggestion.surface_text)
        if not surface:
            continue
        realtime_decision = decide_realtime_memory_candidate(
            suggestion,
            context=RealtimeMemoryContext(
                committed_context=snapshot.committed_context,
                commit_preview=snapshot.commit_text_preview,
                semantic_query=semantic_query,
                query_basis=query_basis,
            ),
        )
        if not realtime_decision.allowed:
            continue
        suggestion = annotate_realtime_memory_candidate(suggestion, decision=realtime_decision)
        metadata = dict(suggestion.metadata)
        durable_memory = _suggestion_has_durable_memory_signal(metadata)
        if _rag_suggestion_repeats_context(
            suggestion,
            snapshot=snapshot,
            semantic_query=semantic_query,
            query_basis=query_basis,
            prediction_context=prediction_context,
        ):
            continue
        if _looks_like_assistant_history_candidate(suggestion):
            continue
        if not durable_memory and _looks_like_low_quality_memory_candidate(surface):
            continue
        if _looks_like_uncompiled_raw_history_candidate(suggestion):
            continue
        key = _display_text_norm(surface)
        if key in seen:
            continue
        seen.add(key)
        result.append(suggestion)
    return result


def _rag_suggestion_repeats_context(
    suggestion: InputSuggestion,
    *,
    snapshot: RimeContextSnapshot,
    semantic_query: str,
    query_basis: str,
    prediction_context: str,
) -> bool:
    surface = compact_whitespace(suggestion.surface_text)
    if not surface:
        return True
    if _surface_repeats_current_context(
        surface,
        snapshot=snapshot,
        semantic_query=semantic_query,
        query_basis=query_basis,
    ):
        return True
    if _suggestion_has_durable_memory_signal(dict(suggestion.metadata)):
        return False
    return _surface_repeats_history_reference(surface, prediction_context)


def _surface_repeats_current_context(
    surface: str,
    *,
    snapshot: RimeContextSnapshot,
    semantic_query: str,
    query_basis: str,
) -> bool:
    surface_norm = _repeat_norm(surface)
    if len(surface_norm) < 3:
        return False
    anchors = [
        compact_whitespace(snapshot.committed_context),
        compact_whitespace(snapshot.commit_text_preview),
    ]
    if query_basis in {"committedContext", "commitTextPreview"}:
        anchors.append(compact_whitespace(semantic_query))
    for anchor in anchors:
        anchor_norm = _repeat_norm(anchor)
        if len(anchor_norm) < 3:
            continue
        if surface_norm == anchor_norm:
            return True
        if surface_norm in anchor_norm:
            coverage = len(surface_norm) / max(1, len(anchor_norm))
            if coverage >= 0.9:
                return True
        if len(anchor_norm) >= 4 and anchor_norm in surface_norm:
            coverage = len(anchor_norm) / max(1, len(surface_norm))
            if coverage >= 0.75:
                return True
    return False


def _surface_repeats_history_reference(surface: str, prediction_context: str) -> bool:
    if "历史参考" not in prediction_context and "历史输入:" not in prediction_context:
        return False
    surface_norm = _repeat_norm(surface)
    context_norm = _repeat_norm(prediction_context)
    if len(surface_norm) < 4 or not context_norm:
        return False
    if surface_norm in context_norm:
        return True
    if len(surface_norm) >= 8 and _cjk_context_overlap_ratio(surface, prediction_context) >= 0.8:
        return True
    return False


def _suggestion_has_durable_memory_signal(metadata: Mapping[str, object]) -> bool:
    if _metadata_memory_kind(metadata) in {"stable_memory", "phrase", "memory_alias"}:
        return True
    tags = {
        compact_whitespace(str(tag)).lower()
        for tag in metadata.get("tags", [])
        if compact_whitespace(str(tag))
    } if isinstance(metadata.get("tags"), list) else set()
    if tags.intersection({"generated-memory", "api-core-optimized", "api-lexicon", "lexicon-phrase", "phrase-memory", "curated", "structure"}):
        return True
    state = metadata.get("state") if isinstance(metadata.get("state"), dict) else {}
    assert isinstance(state, dict)
    if bool(state.get("pinned")):
        return True
    durable_count = max(
        _safe_int(state.get("accepted_count")),
        _safe_int(state.get("event_accepted_count")),
        _safe_int(state.get("effective_frequency")),
        _safe_int(state.get("project_input_frequency")),
        _safe_int(state.get("app_input_frequency")),
    )
    return durable_count >= 3


def _looks_like_uncompiled_raw_history_candidate(suggestion: InputSuggestion) -> bool:
    surface = compact_whitespace(suggestion.surface_text)
    if not surface:
        return True
    metadata = dict(suggestion.metadata)
    if _metadata_memory_kind(metadata) in {"stable_memory", "phrase", "memory_alias"}:
        return False
    if _suggestion_has_curated_or_repeated_accept_signal(metadata):
        return False

    source_ref = compact_whitespace(str(metadata.get("source_ref") or metadata.get("memory_id") or "")).lower()
    suggestion_id = compact_whitespace(suggestion.suggestion_id).lower()
    raw_event_like = (
        suggestion.source_event_id is not None
        or source_ref.startswith(("input_event:", "event:", "committed:"))
        or suggestion_id.startswith(("sug-event:", "event:", "input_event:", "committed:"))
    )
    if not raw_event_like:
        return False

    cjk_count = len(re.findall(r"[\u3400-\u9fff]", surface))
    punctuation_count = len(re.findall(r"[，。！？；：,.!?;:]", surface))
    if punctuation_count > 0:
        return True
    if len(surface) > 42:
        return True
    if cjk_count >= 18 and punctuation_count > 0:
        return True
    if compact_whitespace(suggestion.suggestion_type).lower() in {"paragraph", "history", "input_event"} and cjk_count >= 14:
        return True
    return False


def _suggestion_has_curated_or_repeated_accept_signal(metadata: Mapping[str, object]) -> bool:
    if _metadata_memory_kind(metadata) in {"stable_memory", "phrase", "memory_alias"}:
        return True
    tags = {
        compact_whitespace(str(tag)).lower()
        for tag in metadata.get("tags", [])
        if compact_whitespace(str(tag))
    } if isinstance(metadata.get("tags"), list) else set()
    if tags.intersection({"generated-memory", "api-core-optimized", "api-lexicon", "lexicon-phrase", "phrase-memory", "curated", "structure"}):
        return True
    state = metadata.get("state") if isinstance(metadata.get("state"), dict) else {}
    assert isinstance(state, dict)
    raw_signals = state.get("rawSignals") if isinstance(state.get("rawSignals"), dict) else {}
    assert isinstance(raw_signals, dict)
    if bool(state.get("pinned") or raw_signals.get("pinned")):
        return True
    accepted_count = max(
        _safe_int(state.get("accepted_count")),
        _safe_int(state.get("event_accepted_count")),
        _safe_int(state.get("acceptedCount")),
        _safe_int(raw_signals.get("acceptedCount")),
    )
    return accepted_count >= 3


def _metadata_memory_kind(metadata: Mapping[str, object]) -> str:
    state = metadata.get("state") if isinstance(metadata.get("state"), dict) else {}
    assert isinstance(state, dict)
    return compact_whitespace(str(metadata.get("memory_kind") or state.get("memory_kind") or "")).lower()


def _cjk_context_overlap_ratio(surface: str, context: str) -> float:
    candidate_chars = re.findall(r"[\u3400-\u9fff]", surface)
    context_chars = set(re.findall(r"[\u3400-\u9fff]", context))
    if len(candidate_chars) < 4 or not context_chars:
        return 0.0
    covered = sum(1 for char in candidate_chars if char in context_chars)
    return covered / max(1, len(candidate_chars))


def _filter_post_commit_rag_suggestions_for_query(
    suggestions: list[InputSuggestion],
    *,
    snapshot: RimeContextSnapshot,
    semantic_query: str,
    query_basis: str,
) -> list[InputSuggestion]:
    if not _is_post_commit_prediction_snapshot(snapshot):
        return suggestions
    result: list[InputSuggestion] = []
    for suggestion in suggestions:
        if _post_commit_fallback_text_echoes_snapshot(suggestion.surface_text, snapshot):
            continue
        if _post_commit_model_prediction_echoes_context(
            suggestion.surface_text,
            snapshot.committed_context,
        ) or _post_commit_model_prediction_echoes_context(
            suggestion.surface_text,
            snapshot.commit_text_preview,
        ):
            continue
        if _post_commit_rag_candidate_has_strong_signal(
            suggestion,
            semantic_query=semantic_query,
            query_basis=query_basis,
        ):
            result.append(suggestion)
    return result


def _filter_model_predictions_for_snapshot(
    predictions: list[ModelPrediction],
    *,
    snapshot: RimeContextSnapshot,
    query_basis: str,
) -> list[ModelPrediction]:
    if not predictions or not _is_post_commit_prediction_snapshot(snapshot):
        return predictions
    if query_basis not in {"committedContext", "commitTextPreview"}:
        return predictions
    return filter_post_commit_model_completions(
        predictions,
        snapshot=snapshot,
        existing_texts=(),
        max_candidates=len(predictions),
    )


def _post_commit_empty_result_fallback_predictions(
    *,
    snapshot: RimeContextSnapshot,
    semantic_query: str,
    model_predictions: list[ModelPrediction],
    suggestions: list[InputSuggestion],
    rag_lane: Mapping[str, object],
    model_lane: Mapping[str, object],
    progressive_state: Mapping[str, object],
) -> list[ModelPrediction]:
    if model_predictions or suggestions:
        return []
    if not _is_post_commit_prediction_snapshot(snapshot):
        return []
    if not snapshot.progressive_follow_up:
        return []
    if not demo_safe_fallback_enabled():
        return []
    pending_lanes = progressive_state.get("pendingLanes")
    if isinstance(pending_lanes, list) and pending_lanes:
        return []
    if bool(rag_lane.get("pending")) or bool(model_lane.get("pending")) or bool(model_lane.get("inFlight")):
        return []
    text = _post_commit_empty_result_fallback_text(snapshot=snapshot, semantic_query=semantic_query)
    if not text or _post_commit_fallback_text_echoes_snapshot(text, snapshot):
        return []
    return [
        ModelPrediction(
            text=text,
            rank=1,
            provider_name="demo-safe-fallback",
            latency_ms=int(model_lane.get("elapsedMs") or 0),
            confidence=0.35,
            metadata={
                "requestType": PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
                "noPinyinFilter": True,
                "fallbackReason": "demo_safe_empty_post_commit_fallback",
                "fallbackSource": "post_commit_empty_result",
                "queryHash": stable_text_hash(semantic_query),
            },
        )
    ]


def _post_commit_pending_preview_predictions(
    *,
    snapshot: RimeContextSnapshot,
    semantic_query: str,
    model_predictions: list[ModelPrediction],
    model_lane: Mapping[str, object],
) -> tuple[list[ModelPrediction], dict[str, object]]:
    if model_predictions:
        return [], {}
    if not post_commit_pending_preview_enabled() or not post_commit_presentation_stream_enabled():
        return [], {}
    if not _is_post_commit_prediction_snapshot(snapshot):
        return [], {}
    if not (bool(model_lane.get("pending")) or bool(model_lane.get("inFlight"))):
        return [], {}
    final_text = _post_commit_empty_result_fallback_text(snapshot=snapshot, semantic_query=semantic_query)
    if _post_commit_fallback_text_echoes_snapshot(final_text, snapshot):
        return [], {}
    steps = _post_commit_presentation_stream_steps(final_text)
    if not steps:
        return [], {}
    slot_key = _post_commit_model_lane_stream_slot_key(model_lane)
    if not slot_key:
        slot_key = _post_commit_fallback_presentation_stream_key(snapshot=snapshot, final_text=final_text)
    metadata = {
        "requestType": PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
        "noPinyinFilter": True,
        "pendingPreview": True,
        "fallbackSource": "post_commit_pending_preview",
        "presentationStreaming": True,
        "presentationStage": 1,
        "presentationStageCount": len(steps),
        "presentationPartial": True,
        "presentationFinalTextHash": stable_text_hash(final_text),
        "presentationStreamSlotKey": slot_key,
        "previewFinalTextHash": stable_text_hash(final_text),
        "queryHash": stable_text_hash(semantic_query),
    }
    return [
        ModelPrediction(
            text=steps[0],
            rank=1,
            provider_name="post-commit-preview",
            latency_ms=int(model_lane.get("elapsedMs") or 0),
            confidence=0.25,
            metadata=metadata,
        )
    ], {
        "pendingPreview": True,
        "predictionCount": 1,
        "presentationStreaming": True,
        "presentationStage": 1,
        "presentationStageCount": len(steps),
        "skippedReason": "",
    }


def _post_commit_empty_result_fallback_text(*, snapshot: RimeContextSnapshot, semantic_query: str) -> str:
    context = compact_whitespace(
        " ".join(
            item
            for item in (snapshot.committed_context, semantic_query, snapshot.commit_text_preview)
            if compact_whitespace(item)
        )
    )
    if not context:
        return ""
    if "需求" in context and ("完成" in context or "没" in context or "没有" in context):
        return "继续补齐需求"
    if "为什么" in context:
        return "为什么会这样"
    if "测试" in context:
        return "继续测试一下"
    if "输入法" in context:
        return "继续调试输入法"
    if "RAG" in context or "rag" in context:
        return "继续完善 RAG"
    if "LLM" in context or "llm" in context or "模型" in context:
        return "继续检查模型输出"
    return "继续完善一下"


def _post_commit_fallback_text_echoes_snapshot(text: str, snapshot: RimeContextSnapshot) -> bool:
    surface = compact_whitespace(text)
    if not surface:
        return True
    if candidate_has_self_repetition(surface):
        return True
    for context in (
        snapshot.committed_context,
        snapshot.commit_text_preview,
        snapshot.raw_input,
        snapshot.preedit,
    ):
        if candidate_echoes_text(surface, context, reject_single_occurrence=True):
            return True
    return False


def _stage_post_commit_fallback_presentation_stream(
    *,
    snapshot: RimeContextSnapshot,
    predictions: list[ModelPrediction],
    slot_key: str = "",
) -> tuple[list[ModelPrediction], dict[str, object]]:
    if not predictions or not post_commit_presentation_stream_enabled():
        return predictions, {}
    first = predictions[0]
    steps = _post_commit_presentation_stream_steps(first.text)
    if len(steps) <= 1:
        return predictions, {
            "presentationStreaming": False,
            "presentationStage": len(steps),
            "presentationStageCount": len(steps),
        }
    key = slot_key or _post_commit_fallback_presentation_stream_key(snapshot=snapshot, final_text=first.text)
    now = time.time()
    with _POST_COMMIT_PRESENTATION_STREAM_LOCK:
        _drop_expired_post_commit_presentation_stream_keys(now)
        previous_stage = _POST_COMMIT_PRESENTATION_STREAM_STAGES.get(key, (0, now))[0]
        stage = min(previous_stage + 1, len(steps))
        _POST_COMMIT_PRESENTATION_STREAM_STAGES[key] = (stage, now)
    pending = stage < len(steps)
    metadata = dict(first.metadata)
    metadata.update(
        {
            "presentationStreaming": True,
            "presentationStage": stage,
            "presentationStageCount": len(steps),
            "presentationPartial": pending,
            "presentationFinalTextHash": stable_text_hash(first.text),
            "presentationStreamSlotKey": key,
        }
    )
    staged_first = replace(first, text=steps[stage - 1], metadata=metadata)
    staged_predictions = [staged_first] if pending else [staged_first, *predictions[1:]]
    return staged_predictions, {
        "completionJobState": "streaming" if pending else "completed",
        "pending": pending,
        "inFlight": pending,
        "presentationStreaming": True,
        "presentationStage": stage,
        "presentationStageCount": len(steps),
    }


def _post_commit_completion_stream_slot_key(job_id: object) -> str:
    job_id_text = compact_whitespace(str(job_id or ""))
    return f"post-commit-completion:{job_id_text}" if job_id_text else ""


def _post_commit_model_lane_stream_slot_key(model_lane: Mapping[str, object]) -> str:
    configured = compact_whitespace(str(model_lane.get("presentationStreamSlotKey") or ""))
    if configured:
        return configured
    return _post_commit_completion_stream_slot_key(model_lane.get("completionJobId"))


def _post_commit_fallback_presentation_stream_key(*, snapshot: RimeContextSnapshot, final_text: str) -> str:
    transaction = snapshot.frontend_transaction
    material = "\x1f".join(
        (
            snapshot.session_id,
            compact_whitespace(snapshot.committed_context),
            compact_whitespace(snapshot.commit_text_preview),
            transaction.front_app_bundle_id or snapshot.app,
            transaction.input_source_id,
            str(transaction.selection_epoch),
            stable_text_hash(final_text),
        )
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _drop_expired_post_commit_presentation_stream_keys(now: float) -> None:
    expired = [
        key
        for key, (_, updated_at) in _POST_COMMIT_PRESENTATION_STREAM_STAGES.items()
        if now - updated_at > 60
    ]
    for key in expired:
        _POST_COMMIT_PRESENTATION_STREAM_STAGES.pop(key, None)
    limit = post_commit_presentation_stream_max_entries()
    while len(_POST_COMMIT_PRESENTATION_STREAM_STAGES) > limit:
        oldest_key = min(
            _POST_COMMIT_PRESENTATION_STREAM_STAGES,
            key=lambda item: _POST_COMMIT_PRESENTATION_STREAM_STAGES[item][1],
        )
        _POST_COMMIT_PRESENTATION_STREAM_STAGES.pop(oldest_key, None)


def filter_post_commit_model_completions(
    predictions: list[ModelPrediction],
    *,
    snapshot: RimeContextSnapshot,
    existing_texts: tuple[str, ...] = (),
    max_candidates: int = 5,
) -> list[ModelPrediction]:
    committed = compact_whitespace(snapshot.committed_context)
    commit_preview = compact_whitespace(snapshot.commit_text_preview)
    duplicate_surfaces = {
        compact_whitespace(item).lower()
        for item in (committed, commit_preview, *existing_texts)
        if compact_whitespace(item)
    }
    result: list[ModelPrediction] = []
    seen: set[str] = set()
    for prediction in predictions:
        quality_allowed, quality_meta = _post_commit_base_completion_quality(
            prediction,
            context=committed or commit_preview,
        )
        quality_mode = local_model_quality_gate_mode()
        if not quality_allowed and (
            quality_mode == "strict" or quality_meta.get("reason") == "negates_context_hypothesis"
        ):
            continue
        text = _clean_post_commit_completion_text(prediction.text)
        if _is_bare_completion_prediction(prediction):
            text = _bridge_bare_completion_clause(text, committed)
        normalized = compact_whitespace(text).lower()
        if not normalized or normalized in seen or normalized in duplicate_surfaces:
            continue
        if _post_commit_fallback_text_echoes_snapshot(text, snapshot):
            continue
        if _post_commit_model_prediction_echoes_context(text, committed) or _post_commit_model_prediction_echoes_context(
            text, commit_preview
        ):
            continue
        if (
            quality_reject_prompt_leak(text)
            or _looks_like_model_prompt_echo(text)
            or _looks_like_complaint_or_debug_fragment(text)
            or _looks_like_truncated_post_commit_completion(text)
        ):
            continue
        if _looks_like_meta_candidate_surface(text):
            continue
        seen.add(normalized)
        metadata = dict(prediction.metadata)
        metadata["noPinyinFilter"] = True
        metadata["requestType"] = PREDICTION_REQUEST_POST_COMMIT_COMPLETION
        if quality_meta:
            quality_meta["mode"] = quality_mode
            quality_meta["observedOnly"] = not quality_allowed and quality_mode == "observe"
            metadata["qualityGate"] = quality_meta
        quality_confidence = quality_meta.get("seedProbability") if quality_meta else None
        result.append(
            ModelPrediction(
                text=text,
                rank=len(result) + 1,
                provider_name=prediction.provider_name,
                latency_ms=prediction.latency_ms,
                confidence=(
                    max(0.0, min(1.0, float(quality_confidence)))
                    if isinstance(quality_confidence, (int, float))
                    else prediction.confidence
                ),
                metadata=metadata,
            )
        )
        if len(result) >= max(0, int(max_candidates)):
            break
    return result


def _post_commit_base_completion_quality(
    prediction: ModelPrediction,
    *,
    context: str,
) -> tuple[bool, dict[str, object]]:
    """Fail closed on uncertain MiniMind branches without affecting other providers."""

    metadata = dict(prediction.metadata or {})
    if str(metadata.get("candidate_mode") or "") != "base-completion-branches":
        return True, {}
    score_rows = metadata.get("candidate_scores")
    if not isinstance(score_rows, list):
        return True, {}
    surface = compact_whitespace(prediction.text)
    score = next(
        (
            row
            for row in score_rows
            if isinstance(row, Mapping) and compact_whitespace(str(row.get("text") or "")) == surface
        ),
        None,
    )
    if not isinstance(score, Mapping):
        return True, {}
    try:
        probability = float(score.get("probability"))
    except (TypeError, ValueError):
        return True, {}

    context_tail = compact_whitespace(re.split(r"[。！？!?\n\r]+", context)[-1]).lower()
    candidate_text = surface.lower()
    context_terms = {term for term in _MODEL_QUALITY_TOPIC_TERMS if term in context_tail}
    candidate_terms = {term for term in _MODEL_QUALITY_TOPIC_TERMS if term in candidate_text}
    shared_terms = sorted(context_terms.intersection(candidate_terms))
    technical_context = len(context_terms) >= 2 or bool(
        context_terms.intersection({"rag", "rime", "llm", "ds", "输入法", "候选", "模型", "训练", "语料"})
    )
    open_context = context_tail.endswith(_MODEL_QUALITY_OPEN_ENDINGS)
    negates_context_hypothesis = bool(
        technical_context
        and candidate_text.startswith(("不是", "并不是", "不一定是"))
        and re.search(r"(?:可能|也许|或许)(?:是|与).{0,16}(?:问题|原因)", context_tail)
    )
    if negates_context_hypothesis:
        allowed = False
        reason = "negates_context_hypothesis"
    elif technical_context:
        allowed = bool(shared_terms and probability >= 0.015) or bool(open_context and probability >= 0.18)
        reason = "topic_match" if shared_terms else "open_technical_context" if allowed else "technical_topic_miss"
    else:
        allowed = bool(open_context and probability >= 0.12)
        reason = "open_context" if allowed else "closed_or_uncertain_context"
    return allowed, {
        "schemaVersion": "rag-ime.model-quality-gate.v1",
        "allowed": allowed,
        "reason": reason,
        "seedProbability": probability,
        "technicalContext": technical_context,
        "openContext": open_context,
        "negatesContextHypothesis": negates_context_hypothesis,
        "sharedTerms": shared_terms,
    }


def _clean_post_commit_completion_text(text: str) -> str:
    surface = compact_whitespace(text)
    surface = re.sub(r"^\s*(?:[-*]|\d+[.)、]|[一二三四五六七八九十]+[.)、])\s*", "", surface)
    surface = re.sub(r"^[\"'“”‘’\[\]【】]+|[\"'“”‘’\[\]【】]+$", "", surface)
    surface = re.sub(r"^[,，。！？；;、\s]+|[,，。！？；;、\s]+$", "", surface)
    if len(surface) > 18:
        surface = surface[:18].rstrip("，。！？；;、 ")
    return compact_whitespace(surface)


def _looks_like_truncated_post_commit_completion(text: str) -> bool:
    surface = compact_whitespace(text)
    if not surface:
        return True
    if surface in {"您", "你", "我", "已", "您已", "你已", "我已", "您已经", "你已经", "我已经"}:
        return True
    return bool(re.fullmatch(r"(?:您已|你已|我已|已){2,}", surface))


def _is_post_commit_prediction_snapshot(snapshot: RimeContextSnapshot) -> bool:
    if compact_whitespace(snapshot.raw_input) or compact_whitespace(snapshot.preedit):
        return False
    if snapshot.candidates:
        return False
    return bool(compact_whitespace(snapshot.committed_context) or compact_whitespace(snapshot.commit_text_preview))


def _post_commit_model_prediction_echoes_context(candidate: str, committed_context: str) -> bool:
    return quality_reject_context_echo(candidate, committed_context, "")


def _post_commit_rag_candidate_has_strong_signal(
    suggestion: InputSuggestion,
    *,
    semantic_query: str,
    query_basis: str,
) -> bool:
    metadata = dict(suggestion.metadata)
    if _suggestion_has_positive_user_signal(metadata):
        return True

    query = compact_whitespace(semantic_query)
    surface = compact_whitespace(suggestion.surface_text)
    if not query or not surface:
        return False

    if query_basis == "commitTextPreview" and semantic_signal_length(query) < 4:
        return False

    hits = _important_surface_overlap_terms(query=query, surface=surface)
    if query_basis == "commitTextPreview" and hits:
        return True
    if len(hits) >= 2:
        return True
    if any(len(term) >= 4 for term in hits):
        return True
    if any(term in {"agent", "codex", "llm", "mlx", "rag", "rime", "squirrel"} for term in hits):
        return True
    return False


def _suggestion_has_positive_user_signal(metadata: Mapping[str, object]) -> bool:
    state = metadata.get("state") if isinstance(metadata.get("state"), dict) else {}
    assert isinstance(state, dict)
    raw_signals = state.get("rawSignals") if isinstance(state.get("rawSignals"), dict) else {}
    assert isinstance(raw_signals, dict)
    generated_side_candidate = _suggestion_is_generated_side_candidate(metadata)
    if bool(state.get("pinned") or raw_signals.get("pinned")):
        return True
    if generated_side_candidate:
        generated_accepts = max(
            _safe_int(state.get("accepted_count")),
            _safe_int(state.get("event_accepted_count")),
            _safe_int(state.get("acceptedCount")),
            _safe_int(raw_signals.get("acceptedCount")),
        )
        generated_frequency = max(
            _safe_int(state.get("input_frequency")),
            _safe_int(state.get("project_input_frequency")),
            _safe_int(state.get("app_input_frequency")),
            _safe_int(state.get("effective_frequency")),
            _safe_int(raw_signals.get("inputFrequency")),
            _safe_int(raw_signals.get("projectInputFrequency")),
            _safe_int(raw_signals.get("appInputFrequency")),
            _safe_int(raw_signals.get("effectiveFrequency")),
        )
        return generated_accepts >= 3 or generated_frequency >= 3
    for key in (
        "accepted_count",
        "event_accepted_count",
        "acceptedCount",
    ):
        if _safe_int(state.get(key) if key in state else raw_signals.get(key)) > 0:
            return True
    for key in (
        "input_frequency",
        "project_input_frequency",
        "effective_frequency",
        "inputFrequency",
        "projectInputFrequency",
        "effectiveFrequency",
    ):
        if _safe_int(state.get(key) if key in state else raw_signals.get(key)) >= 2:
            return True
    return False


def _suggestion_is_generated_side_candidate(metadata: Mapping[str, object]) -> bool:
    tags_raw = metadata.get("tags")
    tags = {compact_whitespace(str(tag)).lower() for tag in tags_raw if str(tag).strip()} if isinstance(tags_raw, list) else set()
    if tags.intersection({"source:model", "source:rag"}):
        return True
    source_ref = compact_whitespace(str(metadata.get("source_ref") or "")).lower()
    reason = compact_whitespace(str(metadata.get("reason") or "")).lower()
    provider = compact_whitespace(str(metadata.get("provider_name") or "")).lower()
    return "rime-sidecar:model" in reason or "rime-sidecar:model" in provider or "source:model" in source_ref


def _important_surface_overlap_terms(*, query: str, surface: str) -> list[str]:
    surface_lower = compact_whitespace(surface).lower()
    result: list[str] = []
    seen: set[str] = set()
    for term in token_terms(query, max_terms=48):
        normalized = compact_whitespace(term).lower()
        if len(normalized) < 2:
            continue
        if normalized in _POST_COMMIT_GENERIC_QUERY_TERMS:
            continue
        if normalized in seen:
            continue
        if normalized in surface_lower:
            seen.add(normalized)
            result.append(normalized)
    return result


def _safe_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _looks_like_low_quality_memory_candidate(surface: str) -> bool:
    text = compact_whitespace(surface)
    if not text:
        return True
    if _looks_like_prompt_example_leak(text):
        return True
    if _looks_like_session_identifier_surface(text):
        return True
    if _looks_like_complaint_or_debug_fragment(text):
        return True
    if _looks_like_meta_candidate_surface(text):
        return True
    if _is_low_value_recent_memory_surface(text):
        return True
    if not _text_has_meaningful_ime_signal(text):
        return True
    return False


def _looks_like_prompt_example_leak(text: str) -> bool:
    return compact_whitespace(text) in _PROMPT_EXAMPLE_LEAK_SURFACES


def _looks_like_session_identifier_surface(text: str) -> bool:
    surface = compact_whitespace(text)
    if re.search(
        r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])",
        surface,
    ):
        return True
    if re.fullmatch(r"(?i)[0-9a-f]{16,}", surface):
        return True
    return False


def _looks_like_complaint_or_debug_fragment(text: str) -> bool:
    surface = compact_whitespace(text)
    lowered = surface.lower()
    if "…" in surface or "..." in surface:
        return True
    complaint_markers = (
        "不能用",
        "用不了",
        "没办法",
        "没法",
        "没区别",
        "没意义",
        "没记录",
        "没有记录",
        "不然这个",
        "感觉随机",
        "真实生效",
        "输入不了",
        "崩溃",
        "报错",
        "错误",
        "失败",
        "切成豆包",
    )
    if any(marker in surface for marker in complaint_markers):
        return True
    debug_markers = (
        "exception",
        "traceback",
        "brokenpipe",
        "timeout",
        "failure",
        "error",
    )
    if any(marker in lowered for marker in debug_markers):
        return True
    if surface.startswith(("现在的话", "然后可以再查", "不然")):
        return True
    return False


def _looks_like_meta_candidate_surface(text: str) -> bool:
    surface = compact_whitespace(text)
    if not surface:
        return True
    meta_prefixes = (
        "你正在输入",
        "你正在尝试",
        "你正在使用",
        "你正在看",
        "你正在查看",
        "你当前正在",
        "您正在输入",
        "您正在尝试",
        "您正在使用",
        "您正在看",
        "您正在查看",
        "您当前正在",
        "用户正在输入",
        "用户正在尝试",
        "用户正在使用",
        "用户正在看",
        "用户正在查看",
        "正在输入一个",
        "正在查看",
        "正在看",
    )
    if surface.startswith(meta_prefixes):
        return True
    meta_markers = (
        "已经上屏的文本",
        "上屏的文本",
        "作为输入法候选",
        "这是一个候选",
        "这个项目",
        "号项目",
    )
    return any(marker in surface for marker in meta_markers)


def _looks_like_assistant_history_candidate(suggestion: InputSuggestion) -> bool:
    metadata = dict(suggestion.metadata)
    material = "\n".join(
        compact_whitespace(str(value or ""))
        for value in (
            suggestion.evidence_preview,
            suggestion.expanded_evidence,
            metadata.get("preview_text"),
        )
    )
    return bool(re.search(r"(?m)^\[\d+\]\s*assistant:", material))


def _short_stable_id(*parts: str) -> str:
    material = "\x1f".join(parts)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:10]


def model_request_type_for_snapshot(snapshot: RimeContextSnapshot) -> str:
    active_prefix = compact_whitespace(snapshot.preedit or snapshot.raw_input)
    committed_context = compact_whitespace(snapshot.committed_context)
    if active_prefix and committed_context:
        if stable_short_pinyin_prefix(snapshot):
            return PREDICTION_REQUEST_PINYIN_CONSTRAINED
        return PREDICTION_REQUEST_NO_INPUT
    if active_prefix and snapshot.candidates:
        return PREDICTION_REQUEST_RIME_REORDER
    return PREDICTION_REQUEST_NO_INPUT


def predict_model_with_latency_budget(
    *,
    core: CoreClient,
    predictor: PredictionProvider,
    deepseek_completion_provider: Any | None = None,
    snapshot: RimeContextSnapshot,
    current_input: str,
    explicit_recent_context: str,
    project: str,
    max_candidates: int,
    latency_budget_ms: int,
    memory_enabled: bool = True,
    context_group_id: str = "",
) -> tuple[list[ModelPrediction], dict[str, object]]:
    budget_ms = max(0, int(latency_budget_ms))
    request_type = model_request_type_for_snapshot(snapshot)
    rime_candidate_texts = tuple(
        compact_whitespace(item.text)
        for item in snapshot.candidates[:10]
        if compact_whitespace(item.text)
    )
    if _is_post_commit_prediction_snapshot(snapshot) and not post_commit_auto_model_enabled():
        return [], _model_lane_status(
            called=False,
            timed_out=False,
            skipped_reason="post-commit auto model disabled; use Active RAG action candidate",
            budget_ms=budget_ms,
            request_type=PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
            rime_candidate_count=len(rime_candidate_texts),
        )
    if not _is_post_commit_prediction_snapshot(snapshot) and not composing_model_enabled():
        return [], _model_lane_status(
            called=False,
            timed_out=False,
            skipped_reason="model lane disabled before post-commit",
            budget_ms=budget_ms,
            request_type=request_type,
            rime_candidate_count=len(rime_candidate_texts),
        )
    if budget_ms <= 0:
        return [], _model_lane_status(
            called=False,
            timed_out=False,
            skipped_reason="no latency budget remaining",
            budget_ms=budget_ms,
            request_type=request_type,
            rime_candidate_count=len(rime_candidate_texts),
        )
    if max_candidates <= 0:
        return [], _model_lane_status(
            called=False,
            timed_out=False,
            skipped_reason="no side candidate slot",
            budget_ms=budget_ms,
            request_type=request_type,
            rime_candidate_count=len(rime_candidate_texts),
        )
    if _is_post_commit_prediction_snapshot(snapshot):
        holdover_predictions = _get_model_holdover_predictions(
            project=project,
            current_input=current_input,
            explicit_recent_context=explicit_recent_context,
            max_candidates=max_candidates,
        )
        if holdover_predictions:
            return holdover_predictions, _model_lane_status(
                called=False,
                timed_out=False,
                skipped_reason="holdover prediction cache hit",
                budget_ms=budget_ms,
                elapsed_ms=0,
                prediction_count=len(holdover_predictions),
                holdover_hit=True,
                request_type=request_type,
                rime_candidate_count=len(rime_candidate_texts),
            )
    lane_token = _try_acquire_model_lane()
    if lane_token is None:
        return [], _model_lane_status(
            called=False,
            timed_out=False,
            skipped_reason="model lane already running",
            budget_ms=budget_ms,
            request_type=request_type,
            rime_candidate_count=len(rime_candidate_texts),
        )

    done = Event()
    result: dict[str, object] = {"predictions": []}

    def run_prediction() -> None:
        started = time.perf_counter()
        try:
            local_max_candidates = (
                max(0, max_candidates - 1)
                if _is_post_commit_prediction_snapshot(snapshot)
                and deepseek_completion_provider is not None
                and deepseek_scene_enabled("post_commit")
                else max_candidates
            )
            if request_type == PREDICTION_REQUEST_PINYIN_CONSTRAINED:
                # Small local IME models follow short prefix-constrained prompts
                # more reliably when the context is the clean on-screen text,
                # not the expanded "history/reference" wrapper used for RAG.
                recent_context = compact_whitespace(explicit_recent_context)[-420:]
                result["contextMode"] = "explicit-pinyin-constrained"
            elif budget_ms <= _REALTIME_MODEL_CONTEXT_BUDGET_MS:
                recent_context = compact_whitespace(explicit_recent_context)[-420:]
                result["contextMode"] = "explicit-realtime"
            elif request_type == PREDICTION_REQUEST_NO_INPUT:
                # Post-commit prediction is a continuation task. The RAG lane
                # can use expanded history, but small local models are more
                # reliable when they see only clean on-screen context instead
                # of "history/reference/current context" wrappers.
                recent_context = compact_whitespace(explicit_recent_context)[-420:]
                result["contextMode"] = "explicit-post-commit"
            elif memory_enabled:
                context_event_limit, context_char_limit = model_prediction_context_limits()
                recent_context = build_prediction_context(
                    core,
                    explicit_recent_context=explicit_recent_context,
                    project=project,
                    limit=context_event_limit,
                    max_chars=context_char_limit,
                )
                result["contextMode"] = "history-expanded"
            else:
                recent_context = compact_whitespace(explicit_recent_context)[-420:]
                result["contextMode"] = "explicit-memory-disabled"
            result["historyContext"] = recent_context
            if int((time.perf_counter() - started) * 1000) >= budget_ms:
                result["skippedReason"] = "history context exceeded latency budget"
                return
            result["requestType"] = request_type
            result["rimeCandidateCount"] = len(rime_candidate_texts)
            raw_predictions = predict_with_optional_request_context(
                predictor,
                current_input=current_input,
                recent_context=recent_context,
                max_candidates=local_max_candidates,
                request_type=request_type,
                rime_candidates=rime_candidate_texts,
            )
            result["rawPredictionCount"] = len(raw_predictions)
            predictions = _filter_model_predictions(
                raw_predictions,
                current_input=current_input,
                explicit_recent_context=explicit_recent_context,
                prediction_context=recent_context,
                pinyin_prefix=stable_short_pinyin_prefix(snapshot) if request_type == PREDICTION_REQUEST_PINYIN_CONSTRAINED else "",
            )
            if deepseek_completion_provider is not None:
                deepseek_predictions, deepseek_lane = _predict_deepseek_post_commit_candidates(
                    provider=deepseek_completion_provider,
                    core=core,
                    snapshot=snapshot,
                    current_context=recent_context,
                    existing_predictions=predictions,
                    project=project,
                    max_candidates=max_candidates,
                    started=started,
                    budget_ms=budget_ms,
                    rime_candidates=rime_candidate_texts,
                    context_group_id=context_group_id,
                )
                result["deepseekLane"] = deepseek_lane
                predictions = [*predictions, *deepseek_predictions]
            result["predictions"] = predictions
            if not predictions:
                predictor_error = _predictor_last_error(predictor)
                if predictor_error:
                    result["error"] = predictor_error
                elif request_type == PREDICTION_REQUEST_PINYIN_CONSTRAINED and raw_predictions:
                    result["skippedReason"] = "model predictions did not match pinyin prefix"
            if isinstance(result["predictions"], list) and result["predictions"]:
                _store_model_holdover_predictions(
                    project=project,
                    current_input=current_input,
                    explicit_recent_context=explicit_recent_context,
                    predictions=result["predictions"],
                )
        except Exception as exc:  # pragma: no cover - defensive fail-open guard
            result["error"] = exc.__class__.__name__
        finally:
            result["elapsedMs"] = int((time.perf_counter() - started) * 1000)
            done.set()
            _release_model_lane(lane_token)

    Thread(target=run_prediction, name="rag-ime-model-lane", daemon=True).start()
    if not done.wait(timeout=budget_ms / 1000):
        return [], _model_lane_status(
            called=True,
            timed_out=True,
            skipped_reason="model lane exceeded latency budget",
            budget_ms=budget_ms,
            request_type=request_type,
            rime_candidate_count=len(rime_candidate_texts),
        )

    predictions = result.get("predictions")
    if not isinstance(predictions, list):
        predictions = []
    error = _string(result.get("error"))
    skipped_reason = _string(result.get("skippedReason"))
    lane = _model_lane_status(
        called=True,
        timed_out=False,
        skipped_reason=f"error: {error}" if error else skipped_reason,
        budget_ms=budget_ms,
        elapsed_ms=_optional_int(result.get("elapsedMs")) or 0,
        prediction_count=len(predictions),
        history_context=_string(result.get("historyContext")),
        context_mode=_string(result.get("contextMode")),
        request_type=_string(result.get("requestType")) or request_type,
        rime_candidate_count=_optional_int(result.get("rimeCandidateCount")) or len(rime_candidate_texts),
    )
    deepseek_lane = result.get("deepseekLane")
    if isinstance(deepseek_lane, dict):
        lane.update(deepseek_lane)
    return predictions, lane


def _predict_deepseek_post_commit_candidates(
    *,
    provider: Any,
    core: CoreClient,
    snapshot: RimeContextSnapshot,
    current_context: str,
    existing_predictions: list[ModelPrediction],
    project: str,
    max_candidates: int,
    started: float,
    budget_ms: int,
    rime_candidates: tuple[str, ...],
    context_group_id: str = "",
) -> tuple[list[ModelPrediction], dict[str, object]]:
    if not _is_post_commit_prediction_snapshot(snapshot):
        return [], {
            "deepseekCalled": False,
            "deepseekPredictionCount": 0,
            "deepseekSkippedReason": "not post_commit",
        }
    try:
        assert_deepseek_scene_allowed("post_commit")
    except RuntimeError as exc:
        return [], {
            "deepseekCalled": False,
            "deepseekPredictionCount": 0,
            "deepseekSkippedReason": str(exc),
        }
    remaining = max(0, int(max_candidates) - len(existing_predictions))
    if remaining <= 0:
        return [], {
            "deepseekCalled": False,
            "deepseekPredictionCount": 0,
            "deepseekSkippedReason": "no side candidate slot",
        }
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    remaining_budget_ms = max(1, int(budget_ms) - elapsed_ms)
    app = snapshot.app or snapshot.frontend_transaction.front_app_bundle_id
    memory_hits = retrieve_generation_memory_hits(
        core,
        current_context=current_context,
        project=project,
        app=app,
        context_group_id=context_group_id,
        top_k=6,
    )
    memory_evidence = generation_memory_evidence_pack(memory_hits, max_items=6)
    context_packet = build_post_commit_context_packet(
        current_input=current_context,
        context_group_id=context_group_id,
        app=app,
        project=project,
        memory_books=[
            {
                "bookId": hit.book_ids[0] if hit.book_ids else hit.source_id,
                "title": str(hit.metadata.get("title") or ""),
                "summary": hit.text,
                "surfaceHints": list(hit.surface_hints),
                "tags": list(hit.tags),
                "sourceEventIds": list(hit.evidence_event_ids),
            }
            for hit in memory_hits
            if hit.doc_type == "book"
        ],
        tag_hints=[tag for hit in memory_hits for tag in hit.tags],
        surface_hints=[hint for hit in memory_hits for hint in hit.surface_hints],
        negative_signals=("根据上述", "接下来我们", "可以继续", "候选如下"),
    )
    request = DeepSeekCompletionRequest(
        scene="post_commit",
        current_context=current_context,
        evidence_pack=(
            *_deepseek_post_commit_evidence_pack(
                snapshot=snapshot,
                current_context=current_context,
                rime_candidates=rime_candidates,
            ),
            *memory_evidence,
            *timeline_evidence_pack_from_core(
                core,
                project=project,
                app=app,
                current_context=current_context,
                max_items=4,
            ),
        ),
        context_packet=context_packet,
        max_candidates=remaining,
        latency_budget_ms=remaining_budget_ms,
    )
    predictions: list[ModelPrediction] = []
    seen = {compact_whitespace(item.text).lower() for item in existing_predictions}
    try:
        for delta in provider.stream_candidates(request):
            text = compact_whitespace(str(getattr(delta, "text", "")))
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            predictions.append(
                ModelPrediction(
                    text=text,
                    rank=len(existing_predictions) + len(predictions) + 1,
                    provider_name="deepseek-v4-flash",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    confidence=0.72,
                    metadata={
                        "source_lane": "deepseek_v4_flash",
                        "scene": "post_commit",
                        "append_only": True,
                        **dict(getattr(delta, "metadata", {}) or {}),
                    },
                )
            )
            if len(predictions) >= remaining:
                break
            if int((time.perf_counter() - started) * 1000) >= budget_ms:
                break
    except Exception as exc:  # pragma: no cover - defensive fail-open guard
        return [], {
            "deepseekCalled": True,
            "deepseekPredictionCount": 0,
            "deepseekSkippedReason": f"error: {exc.__class__.__name__}",
        }
    return predictions, {
        "deepseekCalled": True,
        "deepseekPredictionCount": len(predictions),
        "deepseekSkippedReason": "" if predictions else "empty",
        "deepseekAppendOnly": True,
        "deepseekLatencyBudgetMs": remaining_budget_ms,
        "generationMemoryHitCount": len(memory_hits),
        "generationMemoryEvidenceCount": len(memory_evidence),
    }


def _deepseek_post_commit_evidence_pack(
    *,
    snapshot: RimeContextSnapshot,
    current_context: str,
    rime_candidates: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    hints = [item for item in rime_candidates[:5] if compact_whitespace(item)]
    return (
        {
            "sourceType": "post_commit_context",
            "evidencePreview": compact_whitespace(current_context)[-240:],
            "surfaceHints": hints,
            "tags": ["post_commit", "input_method"],
            "metadata": {
                "requestSeq": snapshot.request_seq,
                "frontendRevision": snapshot.frontend_transaction.frontend_revision,
                "selectionEpoch": snapshot.frontend_transaction.selection_epoch,
            },
        },
    )


def _filter_model_predictions(
    predictions: list[ModelPrediction],
    *,
    current_input: str,
    explicit_recent_context: str,
    prediction_context: str = "",
    pinyin_prefix: str = "",
) -> list[ModelPrediction]:
    cleaned: list[ModelPrediction] = []
    seen: set[str] = set()
    for prediction in predictions:
        text = _clean_model_prediction_text(
            prediction.text,
            current_input=current_input,
            explicit_recent_context=explicit_recent_context,
            prediction_context=prediction_context,
        )
        if _is_bare_completion_prediction(prediction):
            text = _bridge_bare_completion_clause(text, explicit_recent_context)
        normalized = compact_whitespace(text).lower()
        if not normalized or normalized in seen:
            continue
        if pinyin_prefix and not _model_prediction_matches_pinyin_prefix(prediction, text, pinyin_prefix):
            continue
        seen.add(normalized)
        if text == compact_whitespace(prediction.text):
            cleaned.append(prediction)
        else:
            cleaned.append(
                ModelPrediction(
                    text=text,
                    rank=prediction.rank,
                    provider_name=prediction.provider_name,
                    latency_ms=prediction.latency_ms,
                    confidence=prediction.confidence,
                    metadata=dict(prediction.metadata),
                )
            )
    return cleaned


def _is_bare_completion_prediction(prediction: ModelPrediction) -> bool:
    metadata = dict(prediction.metadata or {})
    return str(metadata.get("candidate_mode") or "") in {
        "base-completion",
        "base-completion-branches",
    }


def _bridge_bare_completion_clause(candidate: str, context: str) -> str:
    surface = compact_whitespace(candidate)
    prefix = compact_whitespace(context)
    if not surface or not prefix or not re.search(r"[\u3400-\u9fff]$", prefix):
        return surface
    if len(re.split(r"[。！？!?\n\r]+", prefix)[-1]) < 8:
        return surface
    incomplete_endings = (
        "的",
        "地",
        "得",
        "是",
        "有",
        "把",
        "被",
        "给",
        "让",
        "向",
        "从",
        "在",
        "对",
        "为",
        "和",
        "或",
        "与",
        "及",
        "但",
        "而",
        "如果",
        "因为",
        "所以",
        "虽然",
        "不过",
        "需要",
        "应该",
        "可以",
        "能够",
        "不能",
        "不要",
        "准备",
        "计划",
        "继续",
        "正在",
        "已经",
        "随后",
        "然后",
        "包括",
        "例如",
        "比如",
        "缺少",
    )
    if prefix.endswith(incomplete_endings):
        return surface
    return f"，{surface}"


def _model_prediction_matches_pinyin_prefix(prediction: ModelPrediction, text: str, prefix: str) -> bool:
    prefix_norm = _pinyin_constraint_norm(prefix)
    if not prefix_norm:
        return True
    keys: list[str] = []
    metadata = dict(prediction.metadata or {})
    for key in ("initials", "pinyin_initials"):
        value = metadata.get(key)
        if isinstance(value, str):
            keys.append(_pinyin_constraint_norm(value))
    for key in ("pinyin", "full_pinyin", "pinyin_prefixes"):
        value = metadata.get(key)
        if isinstance(value, str):
            keys.extend(_pinyin_constraint_norm(part) for part in value.replace("'", " ").split())
            keys.append(_pinyin_constraint_norm(value))
        elif isinstance(value, (list, tuple)):
            keys.extend(_pinyin_constraint_norm(str(part)) for part in value)
    keys.append(_pinyin_constraint_norm(text_initials(text)))
    return any(key.startswith(prefix_norm) for key in dict.fromkeys(keys) if key)


def _pinyin_constraint_norm(value: str) -> str:
    return "".join(char.lower() for char in compact_whitespace(value) if char.isascii() and char.isalnum())


def _clean_model_prediction_text(
    text: str,
    *,
    current_input: str,
    explicit_recent_context: str,
    prediction_context: str = "",
) -> str:
    surface = compact_whitespace(text)
    if not surface:
        return ""
    surface = re.sub(r"(?is)<think>.*?</think>", " ", surface)
    surface = re.sub(r"(?is)<think>.*", " ", surface)
    surface = compact_whitespace(surface)
    for prefix in (current_input, explicit_recent_context):
        prefix = compact_whitespace(prefix)
        if prefix and surface.startswith(prefix) and _has_repeated_recent_memory_prefix(prefix):
            surface = compact_whitespace(surface.removeprefix(prefix))
    surface = compact_whitespace(surface)
    surface = re.sub(r"^[,，。！？；;、\s]+|[,，。！？；;、\s]+$", "", surface)
    surface = _collapse_repeated_recent_memory_surface(surface)
    if not surface:
        return ""
    if _looks_like_broken_recent_memory_surface(surface):
        return ""
    if _looks_like_model_prompt_echo(surface):
        return ""
    if _looks_like_meta_candidate_surface(surface):
        return ""
    if _model_prediction_repeats_context(
        surface,
        current_input=current_input,
        explicit_recent_context=explicit_recent_context,
        prediction_context=prediction_context,
    ):
        return ""
    if surface in {"续写", "继续", "候选", "预测"}:
        return ""
    return surface


def _looks_like_model_prompt_echo(surface: str) -> bool:
    normalized = compact_whitespace(surface)
    lowered = normalized.lower()
    if _looks_like_prompt_example_leak(normalized):
        return True
    markers = (
        "已上屏上下文",
        "当前拼音",
        "当前输入",
        "候选词",
        "候选 JSON",
        "assistant",
        "system",
        "user",
        "/no_think",
    )
    if any(marker.lower() in lowered for marker in markers):
        return True
    if normalized.endswith("候选") and len(normalized) <= 6:
        return True
    return False


def _model_prediction_repeats_context(
    surface: str,
    *,
    current_input: str,
    explicit_recent_context: str,
    prediction_context: str,
) -> bool:
    if not _repeat_norm(surface):
        return True
    if candidate_has_self_repetition(surface):
        return True
    for context in (current_input, explicit_recent_context, prediction_context):
        if candidate_echoes_text(surface, context, reject_single_occurrence=True):
            return True
    return False


def _repeat_norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", compact_whitespace(text), flags=re.UNICODE).lower()


def _has_repeated_recent_memory_prefix(text: str) -> bool:
    parts = [part for part in compact_whitespace(text).split(" ") if part]
    if len(parts) < 3:
        return False
    for width in range(1, min(5, len(parts) // 2 + 1)):
        if parts[:width] == parts[width : width * 2]:
            return True
    return False


def _rag_lane_status(
    *,
    called: bool,
    timed_out: bool,
    skipped_reason: str,
    budget_ms: int,
    elapsed_ms: int = 0,
    suggestion_count: int = 0,
    fail_closed: bool = False,
    warnings: tuple[str, ...] = (),
) -> dict[str, object]:
    payload: dict[str, object] = {
        "called": called,
        "timedOut": timed_out,
        "skippedReason": skipped_reason,
        "suggestionCount": suggestion_count,
        "latencyBudgetMs": budget_ms,
        "elapsedMs": elapsed_ms,
    }
    if fail_closed:
        payload["failClosed"] = True
    if warnings:
        payload["warnings"] = list(warnings)
    return payload


def _model_lane_status(
    *,
    called: bool,
    timed_out: bool,
    skipped_reason: str,
    budget_ms: int,
    elapsed_ms: int = 0,
    prediction_count: int = 0,
    history_context: str = "",
    holdover_hit: bool = False,
    context_mode: str = "",
    request_type: str = "",
    rime_candidate_count: int = 0,
    requested_max_candidates: int = 0,
) -> dict[str, object]:
    return {
        "called": called,
        "timedOut": timed_out,
        "skippedReason": skipped_reason,
        "predictionCount": prediction_count,
        "latencyBudgetMs": budget_ms,
        "elapsedMs": elapsed_ms,
        "historyContext": history_context,
        "holdoverHit": holdover_hit,
        "contextMode": context_mode,
        "requestType": request_type,
        "rimeCandidateCount": max(0, int(rime_candidate_count)),
        "requestedMaxCandidates": max(0, int(requested_max_candidates)),
    }


def clear_model_prediction_holdover_cache() -> None:
    global _MODEL_LANE_ACTIVE_STARTED_AT, _MODEL_LANE_ACTIVE_TOKEN
    with _MODEL_HOLDOVER_LOCK:
        _MODEL_HOLDOVERS.clear()
    _POST_COMMIT_COMPLETION_CACHE.clear()
    with _POST_COMMIT_PRESENTATION_STREAM_LOCK:
        _POST_COMMIT_PRESENTATION_STREAM_STAGES.clear()
    with _MODEL_LANE_LOCK:
        _MODEL_LANE_ACTIVE_TOKEN = None
        _MODEL_LANE_ACTIVE_STARTED_AT = 0.0


def clear_display_memory_feedback_cache() -> None:
    with _DISPLAY_FEEDBACK_SEEN_LOCK:
        _DISPLAY_FEEDBACK_SEEN.clear()


def clear_prediction_manager_cache() -> None:
    with _PREDICTION_MANAGER_LOCK:
        _PREDICTION_MANAGERS.clear()
    clear_refresh_debounce_cache()


def clear_refresh_debounce_cache() -> None:
    with _REFRESH_DEBOUNCE_LOCK:
        _REFRESH_DEBOUNCE.clear()
    with _AUTO_PREDICTION_TRIGGER_LOCK:
        _AUTO_PREDICTION_TRIGGER.clear()
        _AUTO_PREDICTION_JOB_DECISIONS.clear()
        _GROUP_SHORT_BUFFER.clear()


def wait_for_model_prediction_lane_idle(timeout_s: float = 1.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() <= deadline:
        if _model_lane_is_idle():
            return True
        time.sleep(0.005)
    return False


def _prediction_manager_for_snapshot(
    snapshot: RimeContextSnapshot,
    *,
    default_project: str,
) -> PredictionManager:
    project = compact_whitespace(snapshot.project or default_project)
    app = compact_whitespace(snapshot.app)
    session_id = compact_whitespace(snapshot.session_id)
    key = (project, app, session_id)
    with _PREDICTION_MANAGER_LOCK:
        manager = _PREDICTION_MANAGERS.pop(key, None)
        if manager is not None:
            _PREDICTION_MANAGERS[key] = manager
            return manager
        manager = PredictionManager(candidate_pool_ttl_ms=_POST_COMMIT_PANEL_TTL_MS)
        limit = prediction_manager_max_entries()
        if limit > 0:
            _PREDICTION_MANAGERS[key] = manager
            while len(_PREDICTION_MANAGERS) > limit:
                _PREDICTION_MANAGERS.pop(next(iter(_PREDICTION_MANAGERS)), None)
        return manager


def _should_clear_prediction_pool(
    *,
    snapshot: RimeContextSnapshot,
    trigger_decision: RimeSideCandidateTriggerDecision,
    raw_commit_text: str,
) -> bool:
    if compact_whitespace(raw_commit_text):
        return True
    if not compact_whitespace(snapshot.committed_context):
        return True
    reason = trigger_decision.reason
    if reason in {
        "skip: stale post-commit continuation",
        "skip: raw pinyin fallback",
        "skip: empty semantic signal",
        "skip: low-information Rime candidates",
        "skip: composition owned by rime; ai after commit only",
    }:
        return True
    return False


def _store_model_holdover_predictions(
    *,
    project: str,
    current_input: str,
    explicit_recent_context: str,
    predictions: list[ModelPrediction],
) -> None:
    fingerprint = _holdover_input_state_fingerprint(
        explicit_recent_context=explicit_recent_context,
        current_input=current_input,
    )
    context = _holdover_context_text(explicit_recent_context)
    query = _holdover_query_text(current_input)
    if not fingerprint:
        return
    visible = tuple(predictions[:10])
    if not visible:
        return
    with _MODEL_HOLDOVER_LOCK:
        if model_holdover_max_entries() <= 0:
            _MODEL_HOLDOVERS.clear()
            return
        _MODEL_HOLDOVERS[(project, fingerprint)] = _ModelPredictionHoldover(
            project=project,
            input_state_fingerprint=fingerprint,
            explicit_recent_context=context,
            current_input=query,
            predictions=visible,
            created_at=time.monotonic(),
        )
        _prune_model_holdovers_locked(time.monotonic())


def _get_model_holdover_predictions(
    *,
    project: str,
    current_input: str,
    explicit_recent_context: str,
    max_candidates: int,
) -> list[ModelPrediction]:
    fingerprint = _holdover_input_state_fingerprint(
        explicit_recent_context=explicit_recent_context,
        current_input=current_input,
    )
    if not fingerprint:
        return []
    now = time.monotonic()
    limit = max(1, min(10, int(max_candidates)))
    with _MODEL_HOLDOVER_LOCK:
        _prune_model_holdovers_locked(now)
        cached = _MODEL_HOLDOVERS.get((project, fingerprint))
        if cached is not None and now - cached.created_at > _MODEL_HOLDOVER_TTL_MS / 1000:
            _MODEL_HOLDOVERS.pop((project, fingerprint), None)
            return []
        if cached is not None:
            return list(cached.predictions[:limit])
        return []


def _prune_model_holdovers_locked(now: float) -> None:
    max_entries = model_holdover_max_entries()
    if max_entries <= 0:
        _MODEL_HOLDOVERS.clear()
        return
    expired = [
        key
        for key, candidate in _MODEL_HOLDOVERS.items()
        if now - candidate.created_at > _MODEL_HOLDOVER_TTL_MS / 1000
    ]
    for key in expired:
        _MODEL_HOLDOVERS.pop(key, None)
    while len(_MODEL_HOLDOVERS) > max_entries:
        oldest_key = min(_MODEL_HOLDOVERS, key=lambda item: _MODEL_HOLDOVERS[item].created_at)
        _MODEL_HOLDOVERS.pop(oldest_key, None)


def _holdover_input_state_fingerprint(*, explicit_recent_context: str, current_input: str) -> str:
    context = _holdover_context_text(explicit_recent_context)
    query = _holdover_query_text(current_input)
    if not context and not query:
        return ""
    return _short_stable_id(context, query)


def _holdover_context_text(explicit_recent_context: str) -> str:
    return compact_whitespace(explicit_recent_context)[-420:]


def _holdover_query_text(current_input: str) -> str:
    return compact_whitespace(current_input)[:240]


def _predictor_last_error(predictor: PredictionProvider) -> str:
    direct_error = _string(getattr(predictor, "last_error", ""))
    if direct_error:
        return direct_error
    cooldown_status = getattr(predictor, "cooldown_status", None)
    if callable(cooldown_status):
        try:
            status = cooldown_status()
        except Exception:  # pragma: no cover - defensive debug-only guard
            return ""
        if isinstance(status, dict):
            return _string(status.get("lastError"))
    return ""


def record_rime_side_candidate_selection(
    *,
    payload: dict[str, Any],
    adapter: InputMethodAdapter,
    core: CoreClient,
    default_project: str = "wisdom-weasel-rag-ime",
) -> dict[str, object]:
    privacy_assessment = assess_foreground_write(payload)
    if privacy_assessment["storeAllowed"] is not True:
        return {
            "schemaVersion": "rag-ime.rime-selection.v1",
            "ok": True,
            "dryRun": bool(payload.get("dryRun", False)),
            "stored": False,
            "noStore": True,
            "eventId": "",
            "project": "",
            "sourceType": "",
            "insertText": "",
            "recordedAction": False,
            "recordedCommitAction": False,
            "recordedActionCount": 0,
            "action": None,
            "commitAction": None,
            "skippedActionCount": 0,
            "skippedActions": [],
            "privacyAssessment": privacy_assessment,
            "storageReceipt": storage_receipt(privacy_assessment, stored=False),
        }

    candidate = _candidate_payload(payload)
    insert_text = (
        _string(candidate.get("insertText"))
        or _string(candidate.get("insert_text"))
        or _string(candidate.get("text"))
        or _string(payload.get("insertText"))
    ).strip()
    if not insert_text:
        raise ValueError("candidate insertText/text must not be empty")

    dry_run = _bool(
        payload.get("dryRun", payload.get("dry_run", payload.get("recordingDisabled"))),
        default=False,
    )
    source_type = _string(candidate.get("sourceType") or candidate.get("source_type")) or "side"
    project = _string(payload.get("project")) or default_project
    query = _string(payload.get("query") or payload.get("semanticQuery"))
    recent_context = _string(payload.get("recentContext") or payload.get("committedContext"))
    memory_context = _selection_memory_context(recent_context=recent_context, query=query)
    preedit = _string(payload.get("preedit"))
    label = _string(candidate.get("selectionKey") or candidate.get("label"))
    candidate_rank = _optional_int(candidate.get("selectionRank")) or _candidate_rank(label)
    tags = tuple(
        item
        for item in (
            "squirrel",
            "rime-sidecar",
            "sidecar-selected",
            f"source:{source_type}" if source_type else "",
        )
        if item
    )
    event_id = ""
    if not dry_run:
        event_id = adapter.commit_text(
            insert_text,
            recent_context=memory_context,
            preedit=preedit,
            schema_id="rime_sidecar",
            app=_string(payload.get("app")) or "squirrel",
            project=project,
            privacy_disposition=str(privacy_assessment["disposition"]),
            source=_string(payload.get("source")) or "squirrel_rime_sidecar",
            candidate_rank=candidate_rank,
            provider_name=_string(payload.get("providerName")) or f"rime-sidecar:{source_type}",
            tags=tags,
            context_group_id=_string(
                payload.get("contextGroupId")
                or _mapping(payload.get("foregroundText")).get("contextGroupId")
            ),
            context_group_level=_string(
                payload.get("contextGroupLevel")
                or _mapping(payload.get("foregroundText")).get("contextGroupLevel")
            )
            or "app",
        )

    commit_action_payload: dict[str, object] | None = None
    action_payload: dict[str, object] | None = None
    skipped_actions: list[dict[str, object]] = []
    if not dry_run:
        shown_candidates = _shown_candidate_payloads(payload)
        shown_candidate_ids = [
            _string(item.get("memoryId") or item.get("memory_id") or item.get("suggestionId") or item.get("suggestion_id"))
            for item in shown_candidates
        ]
        feedback_base = _selection_feedback_base_event(
            payload=payload,
            candidate=candidate,
            project=project,
            query=query,
            recent_context=recent_context,
            shown_candidate_ids=shown_candidate_ids,
        )
        action_payload = _apply_candidate_memory_action(
            core=core,
            candidate=candidate,
            action_type="accepted",
            query=query,
            metadata={
                "surface_text": _string(candidate.get("text")) or insert_text,
                "insert_text": insert_text,
                "source": "rime-sidecar-select",
                },
            )
        _record_memory_feedback_event(
            core=core,
            event={
                **feedback_base,
                "event": "accepted",
                "candidateId": _string(candidate.get("memoryId") or candidate.get("memory_id")) or event_id,
                "candidateText": _string(candidate.get("text")) or insert_text,
                "sourceType": source_type,
                "rank": candidate_rank or 0,
                "selectedCandidateId": _string(candidate.get("memoryId") or candidate.get("memory_id")) or event_id,
                "selectedText": _string(candidate.get("text")) or insert_text,
                "selectedRank": candidate_rank or 0,
                "sourceEventId": _optional_int(candidate.get("sourceEventId") or candidate.get("source_event_id")) or _event_id_from_memory_id(event_id),
                "suggestionId": _string(candidate.get("suggestionId") or candidate.get("suggestion_id")),
            },
        )
        if action_payload is None:
            commit_action_payload = _apply_committed_event_feedback(
                core=core,
                event_id=event_id,
                action_type="accepted",
                query=query,
                suggestion_id=_string(candidate.get("suggestionId") or candidate.get("suggestion_id"))
                or f"committed:{source_type}:{candidate_rank or 0}",
                metadata={
                    "surface_text": _string(candidate.get("text")) or insert_text,
                    "insert_text": insert_text,
                    "source": "rime-sidecar-select-committed-event",
                    "source_type": source_type,
                    "selection_rank": candidate_rank or 0,
                    },
                )
        if candidate_rank is not None:
            for shown_candidate in shown_candidates:
                shown_rank = _candidate_display_rank(shown_candidate)
                if shown_rank is None or shown_rank >= candidate_rank:
                    continue
                if _same_candidate_identity(shown_candidate, candidate):
                    continue
                skipped_payload = _apply_candidate_memory_action(
                    core=core,
                    candidate=shown_candidate,
                    action_type="skipped",
                    query=query,
                    metadata={
                        "surface_text": _string(shown_candidate.get("text")),
                        "insert_text": _string(
                            shown_candidate.get("insertText")
                            or shown_candidate.get("insert_text")
                            or shown_candidate.get("text")
                        ),
                        "source": "rime-sidecar-select-skipped-higher",
                        "selected_text": _string(candidate.get("text")) or insert_text,
                        "selected_source_type": source_type,
                        "selected_rank": candidate_rank,
                        "skipped_rank": shown_rank,
                        },
                    )
                _record_memory_feedback_event(
                    core=core,
                    event={
                        **feedback_base,
                        "event": "skipped",
                        "candidateId": _string(shown_candidate.get("memoryId") or shown_candidate.get("memory_id")),
                        "candidateText": _string(shown_candidate.get("text")),
                        "sourceType": _string(shown_candidate.get("sourceType") or shown_candidate.get("source_type")),
                        "rank": shown_rank,
                        "selectedCandidateId": _string(candidate.get("memoryId") or candidate.get("memory_id")) or event_id,
                        "selectedText": _string(candidate.get("text")) or insert_text,
                        "selectedRank": candidate_rank,
                        "sourceEventId": _optional_int(shown_candidate.get("sourceEventId") or shown_candidate.get("source_event_id")),
                        "suggestionId": _string(shown_candidate.get("suggestionId") or shown_candidate.get("suggestion_id")),
                    },
                )
                if skipped_payload is not None:
                    skipped_actions.append(skipped_payload)

    stored = not dry_run and bool(event_id) and not event_id.startswith("skipped:")
    return {
        "schemaVersion": "rag-ime.rime-selection.v1",
        "ok": True,
        "dryRun": dry_run,
        "stored": stored,
        "noStore": False,
        "eventId": event_id,
        "project": project,
        "sourceType": source_type,
        "insertText": insert_text,
        "recordedAction": action_payload is not None,
        "recordedCommitAction": commit_action_payload is not None,
        "recordedActionCount": (1 if action_payload is not None else 0)
        + (1 if commit_action_payload is not None else 0)
        + len(skipped_actions),
        "action": action_payload,
        "commitAction": commit_action_payload,
        "skippedActionCount": len(skipped_actions),
        "skippedActions": skipped_actions,
        "privacyAssessment": privacy_assessment,
        "storageReceipt": storage_receipt(
            privacy_assessment,
            stored=stored,
            event_id=event_id,
            outcome=None if stored else "dry_run",
            reason=None if stored else "dry_run",
        ),
    }


def parse_rime_context_payload(payload: dict[str, Any], *, default_project: str) -> RimeContextSnapshot:
    rime_context = payload.get("rimeContext") if isinstance(payload.get("rimeContext"), dict) else {}
    assert isinstance(rime_context, dict)
    candidates_source = rime_context.get("candidates", payload.get("rimeCandidates"))
    candidates = tuple(_parse_rime_candidates(candidates_source))
    select_keys = _string(rime_context.get("selectKeys") or payload.get("selectKeys"))
    if select_keys:
        candidates = tuple(
            RimeCandidate(
                text=item.text,
                label=item.label or (select_keys[index] if index < len(select_keys) else ""),
                comment=item.comment,
                index=item.index,
            )
            for index, item in enumerate(candidates)
        )
    raw_input = _string(payload.get("rawInput") or payload.get("currentInput"))
    preedit = _string(payload.get("preedit"))
    committed_context = _string(payload.get("committedContext") or payload.get("recentContext"))
    return RimeContextSnapshot(
        session_id=_string(payload.get("sessionId")) or "default",
        request_seq=_bounded_int(payload.get("requestSeq"), default=0, minimum=0, maximum=2**63 - 1),
        raw_input=raw_input,
        preedit=preedit,
        commit_text_preview=_string(payload.get("commitTextPreview") or rime_context.get("commitTextPreview")),
        committed_context=committed_context,
        project=_string(payload.get("project")) or default_project,
        app=_string(payload.get("app") or payload.get("frontmostApp") or rime_context.get("app") or rime_context.get("frontmostApp")),
        candidates=candidates,
        highlighted_index=_bounded_int(
            rime_context.get("highlightedIndex", payload.get("highlightedIndex")),
            default=0,
            minimum=0,
            maximum=999,
        ),
        page=_bounded_int(rime_context.get("page", payload.get("page")), default=0, minimum=0, maximum=999),
        is_last_page=_bool(rime_context.get("isLastPage", payload.get("isLastPage")), default=True),
        latency_budget_ms=_bounded_int(payload.get("latencyBudgetMs"), default=300, minimum=30, maximum=20000),
        max_visible_candidates=_bounded_int(payload.get("maxVisibleCandidates"), default=8, minimum=1, maximum=10),
        max_side_candidates=_bounded_int(payload.get("maxSideCandidates"), default=8, minimum=0, maximum=10),
        idle_ms=_bounded_int(_first_present(payload, rime_context, "idleMs"), default=0, minimum=0, maximum=10000),
        force_side_candidates=_bool(
            _first_present(payload, rime_context, "forceSideCandidates"),
            default=False,
        ),
        progressive_follow_up=_bool(
            _first_present(payload, rime_context, "progressiveFollowUp"),
            default=False,
        ),
        frontend_transaction=_frontend_transaction_from_payload(
            payload=payload,
            rime_context=rime_context,
            raw_input=raw_input,
            preedit=preedit,
            committed_context=committed_context,
        ),
    )


def _frontend_transaction_from_payload(
    *,
    payload: Mapping[str, object],
    rime_context: Mapping[str, object],
    raw_input: str,
    preedit: str,
    committed_context: str,
) -> FrontendTransaction:
    frontend_revision = _bounded_int(
        _first_present(dict(payload), dict(rime_context), "frontendRevision"),
        default=_bounded_int(payload.get("inputGeneration"), default=0, minimum=0, maximum=2**63 - 1),
        minimum=0,
        maximum=2**63 - 1,
    )
    selection_epoch = _bounded_int(
        _first_present(dict(payload), dict(rime_context), "selectionEpoch"),
        default=frontend_revision,
        minimum=0,
        maximum=2**63 - 1,
    )
    composition_text = preedit or raw_input
    composition_hash = _hash_from_payload(
        _first_present(dict(payload), dict(rime_context), "compositionHash"),
        fallback_text=composition_text,
    )
    committed_context_hash = _hash_from_payload(
        _first_present(dict(payload), dict(rime_context), "committedContextHash"),
        fallback_text=committed_context,
    )
    session_id = _string(payload.get("sessionId")) or "default"
    request_seq = _bounded_int(payload.get("requestSeq"), default=0, minimum=0, maximum=2**63 - 1)
    front_app_bundle_id = _string(
        payload.get("frontAppBundleId")
        or payload.get("frontmostAppBundleId")
        or payload.get("frontmostApp")
        or payload.get("app")
        or rime_context.get("frontAppBundleId")
        or rime_context.get("frontmostAppBundleId")
    )
    input_source_id = _string(payload.get("inputSourceId") or rime_context.get("inputSourceId"))
    panel_session_id = _string(payload.get("panelSessionId") or rime_context.get("panelSessionId"))
    if not panel_session_id:
        panel_session_id = _short_stable_id(
            session_id,
            str(request_seq),
            str(frontend_revision),
            str(selection_epoch),
            composition_hash,
            committed_context_hash,
        )
    return FrontendTransaction(
        frontend_revision=frontend_revision,
        selection_epoch=selection_epoch,
        input_generation=_bounded_int(
            _first_present(dict(payload), dict(rime_context), "inputGeneration"),
            default=frontend_revision,
            minimum=0,
            maximum=2**63 - 1,
        ),
        front_app_bundle_id=front_app_bundle_id,
        input_source_id=input_source_id,
        composition_hash=composition_hash,
        committed_context_hash=committed_context_hash,
        panel_session_id=panel_session_id,
        created_at_ms=_bounded_int(
            _first_present(dict(payload), dict(rime_context), "createdAtMs"),
            default=0,
            minimum=0,
            maximum=2**63 - 1,
        ),
    )


def _hash_from_payload(value: object, *, fallback_text: str) -> str:
    configured = _string(value)
    if configured.startswith("sha256:") and len(configured) >= len("sha256:") + 8:
        return configured
    return stable_text_hash(fallback_text)


def choose_semantic_query(snapshot: RimeContextSnapshot, *, foreground_context: str = "") -> tuple[str, str]:
    commit_preview = compact_whitespace(snapshot.commit_text_preview)
    foreground = compact_whitespace(foreground_context)
    active_composition = bool(compact_whitespace(snapshot.raw_input or snapshot.preedit) or snapshot.candidates)
    if foreground and not active_composition:
        # Rime commonly commits Chinese a word at a time. A two- or three-character
        # commit is not the completion context when IMK has already supplied the
        # fresh text field contents; using it alone starves both model and RAG.
        return foreground[-240:], "foregroundText"
    if commit_preview and _commit_preview_has_prediction_signal(commit_preview):
        return commit_preview, "commitTextPreview"
    raw_semantic_input = semantic_ascii_input_text(snapshot)
    if raw_semantic_input:
        return raw_semantic_input, "rawSemanticInput"
    candidate_text = compact_whitespace(" ".join(item.text for item in snapshot.candidates[:3] if item.text))
    if candidate_text:
        prefix = stable_short_pinyin_prefix(snapshot)
        context = compact_whitespace(foreground or snapshot.committed_context)
        query_parts = tuple(part for part in (context[-160:] if context else "", prefix, candidate_text) if part)
        if context:
            return compact_whitespace(" ".join(query_parts)), "rimeCandidates"
        if prefix:
            return compact_whitespace(f"{prefix} {candidate_text}"), "rimeCandidates"
        return candidate_text, "rimeCandidates"
    preedit = compact_whitespace(snapshot.preedit)
    raw_input = compact_whitespace(snapshot.raw_input)
    if preedit and preedit != raw_input:
        return preedit, "preedit"
    context = compact_whitespace(foreground or snapshot.committed_context)
    if context:
        return context[-240:], "foregroundText" if foreground_context else "committedContext"
    return raw_input, "rawInputFallback"


def prediction_first_merge_enabled(payload: Mapping[str, object]) -> bool:
    if "predictionFirstMerge" in payload:
        return _bool(payload.get("predictionFirstMerge"), default=False)
    frontend_build = compact_whitespace(str(payload.get("frontendBuild") or ""))
    schema_version = compact_whitespace(str(payload.get("schemaVersion") or ""))
    return frontend_build.startswith("rag-ime.") or schema_version.startswith("rag-ime.squirrel")


def apply_refresh_debounce(
    *,
    snapshot: RimeContextSnapshot,
    trigger_decision: RimeSideCandidateTriggerDecision,
    semantic_query: str,
    query_basis: str,
    default_project: str,
) -> tuple[RimeSideCandidateTriggerDecision, dict[str, object]]:
    debounce_ms = _refresh_debounce_ms()
    if not trigger_decision.should_refresh or debounce_ms <= 0:
        return trigger_decision, {"debounced": False, "debounceMs": debounce_ms}
    if snapshot.progressive_follow_up or compact_whitespace(snapshot.commit_text_preview):
        return trigger_decision, {"debounced": False, "debounceMs": debounce_ms, "reason": "post_commit_or_followup"}
    if not compact_whitespace(snapshot.raw_input or snapshot.preedit):
        return trigger_decision, {"debounced": False, "debounceMs": debounce_ms, "reason": "no_active_composition"}

    mode = infer_input_mode(snapshot)
    anchors = build_prediction_anchors_from_snapshot(
        snapshot=snapshot,
        mode=mode.value,
        semantic_query=semantic_query,
        query_basis=query_basis,
        stable_short_pinyin_prefix=stable_short_pinyin_prefix(snapshot),
    )
    project = compact_whitespace(snapshot.project or default_project)
    app = compact_whitespace(snapshot.app)
    session_id = compact_whitespace(snapshot.session_id)
    query_family = f"{prediction_mode_family(mode.value)}:{query_basis}"
    key = (project, app, session_id, anchors.hard_context_anchor, query_family)
    monotonic_ms = int(time.monotonic() * 1000)
    semantic_hash = stable_text_hash(semantic_query)
    with _REFRESH_DEBOUNCE_LOCK:
        previous = _REFRESH_DEBOUNCE.get(key)
        if previous is not None:
            age_ms = max(0, monotonic_ms - previous.created_at_ms)
            if age_ms <= debounce_ms:
                return (
                    RimeSideCandidateTriggerDecision(False, "skip: refresh debounce coalesced"),
                    {
                        "debounced": True,
                        "debounceMs": debounce_ms,
                        "coalescedWithAgeMs": age_ms,
                        "debounceKey": _short_stable_id(*key),
                        "previousRequestSeq": previous.request_seq,
                        "previousSemanticQueryHash": previous.semantic_query_hash,
                    },
                )
        _REFRESH_DEBOUNCE[key] = _RefreshDebounceState(
            created_at_ms=monotonic_ms,
            request_seq=snapshot.request_seq,
            semantic_query_hash=semantic_hash,
        )
        max_entries = refresh_debounce_max_entries()
        while len(_REFRESH_DEBOUNCE) > max_entries:
            oldest_key = min(_REFRESH_DEBOUNCE, key=lambda item: _REFRESH_DEBOUNCE[item].created_at_ms)
            _REFRESH_DEBOUNCE.pop(oldest_key, None)
    return (
        trigger_decision,
        {
            "debounced": False,
            "debounceMs": debounce_ms,
            "debounceKey": _short_stable_id(*key),
        },
    )


def _refresh_debounce_ms() -> int:
    raw = os.environ.get("RAG_IME_REFRESH_DEBOUNCE_MS", str(_REFRESH_DEBOUNCE_MS))
    try:
        return max(0, min(1000, int(raw)))
    except ValueError:
        return _REFRESH_DEBOUNCE_MS


def foreground_context_for_side_lanes(
    payload: Mapping[str, object],
    *,
    snapshot: RimeContextSnapshot,
) -> tuple[str, dict[str, object]]:
    raw_payload = payload.get("foregroundText")
    if not isinstance(raw_payload, Mapping):
        return "", {"applied": False, "source": "missing", "reason": "foregroundText payload missing"}
    foreground = foreground_text_from_payload(
        raw_payload,
        snapshot=snapshot,
        created_at_ms=now_ms(),
    )
    meta: dict[str, object] = {
        "applied": False,
        "source": foreground.source,
        "confidence": foreground.confidence,
        "freshnessMs": foreground.freshness_ms,
        "selectedTextChars": foreground.selected_text_chars,
        "surroundingBeforeChars": len(compact_whitespace(foreground.surrounding_before)),
        "surroundingAfterChars": len(compact_whitespace(foreground.surrounding_after)),
        "wholeValueHash": foreground.whole_value_hash,
        "wholeValueChars": foreground.whole_value_chars,
        "captureEpoch": foreground.capture_epoch,
        "capturedAtMs": foreground.captured_at_ms,
        "sourceAppBundleId": foreground.source_app_bundle_id,
        "inputSourceId": foreground.input_source_id,
        "captureFailureReason": foreground.capture_failure_reason,
        "warnings": list(foreground.warnings),
        "snapshotId": foreground.snapshot_id,
        "contextGroupId": foreground.context_group_id,
        "contextGroupLevel": foreground.context_group_level,
        "contextGroupConfidence": foreground.context_group_confidence,
        "commitTextMatched": foreground.commit_text_matched,
        "commitTextMatchDeclared": "commitTextMatched" in raw_payload,
    }
    transaction = snapshot.frontend_transaction
    expected_capture_epoch = transaction.input_generation or transaction.frontend_revision
    if foreground.capture_failure_reason:
        meta["reason"] = foreground.capture_failure_reason
        return "", meta
    if foreground.capture_epoch and expected_capture_epoch and foreground.capture_epoch != expected_capture_epoch:
        meta["reason"] = "capture epoch mismatch"
        meta["expectedCaptureEpoch"] = expected_capture_epoch
        return "", meta
    if (
        foreground.source_app_bundle_id
        and transaction.front_app_bundle_id
        and foreground.source_app_bundle_id != transaction.front_app_bundle_id
    ):
        meta["reason"] = "capture app mismatch"
        return "", meta
    if foreground.input_source_id and transaction.input_source_id and foreground.input_source_id != transaction.input_source_id:
        meta["reason"] = "capture input source mismatch"
        return "", meta
    if foreground.source not in {"accessibility", "text_input_client", "ime_commit_ledger"}:
        meta["reason"] = "foreground source is not an external text context"
        return "", meta
    if foreground.source == "ime_commit_ledger" and (
        "commitTextMatched" not in raw_payload or not foreground.commit_text_matched
    ):
        meta["reason"] = "commit ledger does not prove the current commit"
        return "", meta
    if not foreground.available:
        meta["reason"] = "foreground context unavailable"
        return "", meta
    max_freshness = (
        progressive_follow_up_foreground_max_freshness_ms()
        if snapshot.progressive_follow_up
        else foreground_context_max_freshness_ms()
    )
    if foreground.freshness_ms > max_freshness:
        meta["reason"] = "foreground context stale"
        meta["maxFreshnessMs"] = max_freshness
        return "", meta
    # Post-commit completion continues at the caret. Text after the caret is
    # useful provenance for explicit rewrite/Active RAG, but feeding it into the
    # hot completion query makes the model and retriever continue the wrong
    # paragraph (for example, TextEdit instructions below the insertion point).
    context = compact_whitespace(
        " ".join(
            part
            for part in (
                foreground.surrounding_before,
                foreground.selected_text_preview,
            )
            if compact_whitespace(part)
        )
    )
    if not context:
        meta["reason"] = "foreground context empty"
        return "", meta
    meta["applied"] = True
    meta["reason"] = ""
    meta["status"] = "foreground_context_applied"
    meta["semanticContextMode"] = "before_caret_and_selection"
    meta["excludedAfterChars"] = len(compact_whitespace(foreground.surrounding_after))
    meta["contextHash"] = stable_text_hash(context)
    meta["contextChars"] = len(context)
    return context[-600:], meta


def foreground_context_max_freshness_ms(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_FOREGROUND_CONTEXT_MAX_FRESHNESS_MS"),
        default=_FOREGROUND_CONTEXT_MAX_FRESHNESS_MS,
        minimum=0,
        maximum=10000,
    )


def progressive_follow_up_foreground_max_freshness_ms(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_PROGRESSIVE_FOREGROUND_CONTEXT_MAX_FRESHNESS_MS"),
        default=2500,
        minimum=700,
        maximum=10000,
    )


def apply_commit_burst_prediction_gate(
    *,
    payload: Mapping[str, object],
    snapshot: RimeContextSnapshot,
    adapter: InputMethodAdapter,
    predictor: PredictionProvider,
    trigger_decision: RimeSideCandidateTriggerDecision,
    foreground_context_gate: Mapping[str, object],
    privacy_disposition: str,
    runtime_config: RuntimeConfigSnapshot | None = None,
) -> tuple[RimeSideCandidateTriggerDecision, dict[str, object]]:
    if "commitBurstReady" not in payload or snapshot.progressive_follow_up:
        return trigger_decision, {"enforced": False, "reason": "legacy_or_followup_request"}
    if not _is_post_commit_prediction_snapshot(snapshot):
        return trigger_decision, {"enforced": False, "reason": "not_post_commit"}
    ready = _bool(payload.get("commitBurstReady"), default=False)
    fallback_group_material = snapshot.app or snapshot.frontend_transaction.front_app_bundle_id
    fallback_group_digest = stable_text_hash(fallback_group_material).removeprefix("sha256:")
    group_id = _string(_mapping(payload.get("foregroundText")).get("contextGroupId")) or (
        f"app:{fallback_group_digest[:16]}"
    )
    effective_idle_ms = (
        runtime_config.post_commit.idle_trigger_ms
        if runtime_config is not None
        else auto_predict_idle_ms()
    )
    memory_recording_enabled = runtime_config is None or runtime_config.memory.enabled
    base = {
        "enforced": True,
        "groupId": group_id,
        "idleMs": effective_idle_ms,
        "deltaChars": _optional_int(payload.get("commitBurstDeltaChars")) or 0,
        "acceptedCandidate": _bool(payload.get("acceptedCandidateContinuation"), default=False),
        "remoteDeepSeekAutoCallCount": 0,
        "memoryRecordingEnabled": memory_recording_enabled,
        "rawFragmentPersistence": "disabled",
    }
    if not trigger_decision.should_refresh:
        return trigger_decision, {**base, "reason": trigger_decision.reason, "traceEvent": "prediction_trigger_skipped"}
    if not ready:
        decision = RimeSideCandidateTriggerDecision(False, "skip: waiting for commit burst idle")
        return decision, {**base, "reason": "waiting_idle", "traceEvent": "prediction_trigger_coalesced"}
    if not bool(foreground_context_gate.get("allowed")):
        decision = RimeSideCandidateTriggerDecision(False, "skip: unreliable commit burst context")
        return decision, {**base, "reason": "unreliable_context", "traceEvent": "prediction_trigger_skipped"}
    delta_chars = max(0, int(base["deltaChars"]))
    preview = compact_whitespace(snapshot.commit_text_preview)
    trigger_text = preview + ("文" * max(0, delta_chars - len(preview)))
    current = now_ms()
    with _AUTO_PREDICTION_TRIGGER_LOCK:
        burst_texts = [
            compact_whitespace(str(item))
            for item in payload.get("commitBurstTexts", [])
            if compact_whitespace(str(item))
        ] if isinstance(payload.get("commitBurstTexts"), list) else []
        # Per-commit pieces are prediction-only process state. Persisting them
        # created one-word atoms and transport tags. Squirrel now writes one
        # finalized input segment through /api/commit on Enter/App boundary.
        recorded_event_ids: list[str] = []
        if memory_recording_enabled:
            buffered_texts = burst_texts or ([preview] if preview else [])
            for buffered_text in buffered_texts:
                _GROUP_SHORT_BUFFER.append(
                    group_id,
                    buffered_text,
                    accepted=bool(base["acceptedCandidate"]),
                    source="accepted_candidate" if bool(base["acceptedCandidate"]) else "commit",
                    created_at_ms=current,
                )
        dirty = _AUTO_PREDICTION_TRIGGER.record_commit(
            group_id=group_id,
            text=trigger_text,
            context_hash=_string(foreground_context_gate.get("contextHash"))
            or stable_text_hash(snapshot.committed_context),
            reliable=True,
            accepted_candidate=bool(base["acceptedCandidate"]),
            now=max(0, current - effective_idle_ms),
        )
        provider_identity = f"{_predictor_provider_name(predictor)} {_predictor_model_name(predictor)}".lower()
        provider_kind = "deepseek" if "deepseek" in provider_identity else "local"
        fired = _AUTO_PREDICTION_TRIGGER.poll(group_id, now=current, provider_kind=provider_kind)
    base["recordedEventIds"] = recorded_event_ids
    if not fired.should_call_predictor:
        decision = RimeSideCandidateTriggerDecision(False, f"skip: {fired.reason}")
        return decision, {
            **base,
            "reason": fired.reason,
            "generation": fired.generation,
            "contextHash": fired.context_hash,
            "traceEvent": fired.trace_event,
            "dirtyTraceEvent": dirty.trace_event,
        }
    return trigger_decision, {
        **base,
        "reason": fired.reason,
        "generation": fired.generation,
        "contextHash": fired.context_hash,
        "traceEvent": fired.trace_event,
        "dirtyTraceEvent": dirty.trace_event,
        "providerCallAllowed": True,
    }


def auto_predict_idle_ms(env: Mapping[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    return _bounded_int(
        source.get("RAG_IME_AUTO_PREDICT_IDLE_MS"),
        default=500,
        minimum=0,
        maximum=5000,
    )


def reconcile_commit_burst_prediction_outcome(
    *,
    trigger_meta: dict[str, object],
    model_lane: Mapping[str, object],
    prediction_count: int,
) -> bool | None:
    """Settle trigger budgets when T0 wins or an async local job completes."""

    job_id = _string(model_lane.get("completionJobId"))
    state = _string(model_lane.get("completionJobState"))
    with _AUTO_PREDICTION_TRIGGER_LOCK:
        decision: TriggerDecision | None = None
        if bool(trigger_meta.get("providerCallAllowed")):
            decision = TriggerDecision(
                action="fire",
                reason=_string(trigger_meta.get("reason")) or "commit_burst_idle",
                group_id=_string(trigger_meta.get("groupId")) or "app:unknown",
                generation=_optional_int(trigger_meta.get("generation")) or 0,
                context_hash=_string(trigger_meta.get("contextHash")),
                should_call_predictor=True,
                trace_event="prediction_trigger_fired",
            )
        if bool(model_lane.get("directMemoryHit")) and decision is not None:
            latest = _AUTO_PREDICTION_TRIGGER.complete(
                decision,
                result_count=max(1, int(prediction_count)),
                provider_called=False,
            )
            trigger_meta.update(
                {
                    "providerCallAllowed": False,
                    "directMemoryHit": True,
                    "traceEvent": "prediction_trigger_direct_memory_hit",
                }
            )
            return latest
        if bool(model_lane.get("asyncMode")):
            if decision is not None and job_id and state in {"started", "pending", "streaming"}:
                _AUTO_PREDICTION_JOB_DECISIONS[job_id] = decision
                return None
            completed_decision = _AUTO_PREDICTION_JOB_DECISIONS.pop(job_id, None) if job_id else None
            if completed_decision is not None and state in {"completed", "hit", "error", "timeout"}:
                return _AUTO_PREDICTION_TRIGGER.complete(
                    completed_decision,
                    result_count=max(0, int(prediction_count)),
                    ignored=state in {"error", "timeout"},
                )
            return None
        if decision is not None and bool(model_lane.get("called")):
            return _AUTO_PREDICTION_TRIGGER.complete(
                decision,
                result_count=max(0, int(prediction_count)),
                ignored=bool(model_lane.get("timedOut")),
            )
    return None


def post_commit_foreground_context_gate(
    *,
    snapshot: RimeContextSnapshot,
    foreground_context_meta: Mapping[str, object],
) -> dict[str, object]:
    if not _is_post_commit_prediction_snapshot(snapshot):
        return {"allowed": True, "reason": "not post-commit", "required": False}
    if not post_commit_requires_foreground_context():
        return {"allowed": True, "reason": "foreground context gate disabled", "required": False}
    if bool(foreground_context_meta.get("applied")):
        if bool(foreground_context_meta.get("commitTextMatchDeclared")) and not bool(
            foreground_context_meta.get("commitTextMatched")
        ):
            return {
                "allowed": False,
                "reason": "skip: committed text not observed in foreground context",
                "required": True,
                "source": _string(foreground_context_meta.get("source")),
            }
        return {
            "allowed": True,
            "reason": "reliable foreground context applied",
            "required": True,
            "source": _string(foreground_context_meta.get("source")),
            "contextHash": _string(foreground_context_meta.get("contextHash")),
        }
    return {
        "allowed": False,
        "reason": "skip: reliable foreground context required",
        "required": True,
        "source": _string(foreground_context_meta.get("source")),
        "foregroundReason": _string(foreground_context_meta.get("reason")),
    }


def decide_side_candidate_refresh(
    *,
    snapshot: RimeContextSnapshot,
    semantic_query: str,
    query_basis: str,
) -> RimeSideCandidateTriggerDecision:
    """Decide whether this Rime refresh should call model/RAG side lanes.

    Squirrel can refresh the candidate panel on nearly every composing event.
    Rime candidates should always render, but local model and RAG work should
    wait until there is a stable semantic signal instead of raw pinyin noise.
    """

    if ai_after_commit_only_enabled() and (
        compact_whitespace(snapshot.raw_input) or compact_whitespace(snapshot.preedit)
    ):
        return RimeSideCandidateTriggerDecision(
            False,
            "skip: composition owned by rime; ai after commit only",
        )

    if snapshot.max_side_candidates <= 0:
        return RimeSideCandidateTriggerDecision(False, "skip: side candidates disabled")

    signal_len = semantic_signal_length(semantic_query)
    if signal_len <= 0:
        return RimeSideCandidateTriggerDecision(False, "skip: empty semantic signal")

    if snapshot.force_side_candidates and _forced_refresh_has_side_signal(snapshot, query_basis, semantic_query):
        return RimeSideCandidateTriggerDecision(True, "force: explicit side candidate refresh")

    if raw_english_candidate_text(snapshot):
        return RimeSideCandidateTriggerDecision(False, "skip: raw ascii passthrough")

    if query_basis == "rimeCandidates" and _active_rime_candidates_are_low_information(snapshot):
        if compact_whitespace(snapshot.raw_input or snapshot.preedit):
            return RimeSideCandidateTriggerDecision(True, "refresh: active composition warmup")
        return RimeSideCandidateTriggerDecision(False, "skip: low-information Rime candidates")

    if query_basis == "committedContext" and _committed_context_is_low_information(snapshot, semantic_query):
        if snapshot.idle_ms <= _POST_COMMIT_PANEL_TTL_MS:
            return RimeSideCandidateTriggerDecision(True, "refresh: short post-commit continuation")
        return RimeSideCandidateTriggerDecision(False, "skip: low-information committed context")

    if query_basis == "rawInputFallback":
        if compact_whitespace(snapshot.raw_input or snapshot.preedit):
            return RimeSideCandidateTriggerDecision(True, "refresh: raw pinyin warmup")
        return RimeSideCandidateTriggerDecision(False, "skip: raw pinyin fallback")

    if query_basis == "rawSemanticInput":
        return RimeSideCandidateTriggerDecision(True, "refresh: semantic raw input")

    if query_basis == "commitTextPreview" and _commit_preview_has_prediction_signal(semantic_query):
        return RimeSideCandidateTriggerDecision(True, "refresh: commit preview")

    if query_basis == "rimeCandidates":
        if signal_len >= 3:
            return RimeSideCandidateTriggerDecision(True, "refresh: stable Rime candidates")
        if snapshot.idle_ms >= 300 and signal_len >= 2:
            return RimeSideCandidateTriggerDecision(True, "refresh: idle short Rime candidate")
        if compact_whitespace(snapshot.raw_input or snapshot.preedit) and signal_len >= 1:
            return RimeSideCandidateTriggerDecision(True, "refresh: active composition warmup")
        return RimeSideCandidateTriggerDecision(False, "skip: Rime candidate signal too short")

    if query_basis == "preedit":
        if snapshot.idle_ms >= 300 and signal_len >= 4:
            return RimeSideCandidateTriggerDecision(True, "refresh: idle semantic preedit")
        return RimeSideCandidateTriggerDecision(False, "skip: preedit not stable enough")

    if query_basis == "foregroundText":
        if signal_len >= 2 or _commit_preview_has_prediction_signal(semantic_query):
            return RimeSideCandidateTriggerDecision(True, "refresh: foreground accessibility context")
        return RimeSideCandidateTriggerDecision(False, "skip: foreground context too short")

    if query_basis == "committedContext":
        no_active_composition = not compact_whitespace(snapshot.raw_input) and not compact_whitespace(snapshot.preedit)
        if signal_len >= 2 or _commit_preview_has_prediction_signal(semantic_query):
            if snapshot.idle_ms > _POST_COMMIT_PANEL_TTL_MS:
                return RimeSideCandidateTriggerDecision(False, "skip: stale post-commit continuation")
            return RimeSideCandidateTriggerDecision(
                True,
                "refresh: post-commit continuation" if no_active_composition else "refresh: committed context fallback",
            )
        if snapshot.idle_ms >= 300 and signal_len >= 4:
            return RimeSideCandidateTriggerDecision(True, "refresh: idle committed context")
        return RimeSideCandidateTriggerDecision(False, "skip: waiting for active composition signal")

    return RimeSideCandidateTriggerDecision(False, "skip: unsupported query basis")


def semantic_signal_length(text: str) -> int:
    return sum(1 for char in compact_whitespace(text) if not char.isspace())


def _commit_preview_has_prediction_signal(text: str) -> bool:
    """Allow one meaningful CJK commit without treating punctuation as a query."""

    surface = compact_whitespace(text)
    if not surface:
        return False
    cjk = re.findall(r"[\u3400-\u9fff]", surface)
    if cjk:
        return any(char not in _LOW_INFORMATION_CJK_TOKENS for char in cjk)
    lowered = surface.lower()
    return lowered in _SEMANTIC_ASCII_TERMS or any(
        term.lower() in _SEMANTIC_ASCII_TERMS
        for term in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}", surface)
    )


def _committed_context_is_low_information(snapshot: RimeContextSnapshot, semantic_query: str) -> bool:
    if compact_whitespace(snapshot.raw_input) or compact_whitespace(snapshot.preedit):
        return False
    if compact_whitespace(snapshot.commit_text_preview) or snapshot.candidates:
        return False
    return semantic_signal_length(semantic_query) < 8


def _should_suppress_post_commit_rag_only(
    snapshot: RimeContextSnapshot,
    model_predictions: list[ModelPrediction],
    suggestions: list[InputSuggestion],
) -> bool:
    _ = snapshot, model_predictions, suggestions
    # Weak post-commit retrieval is filtered before this point. If a RAG/memory
    # item survives, it is either semantically aligned with the current text or
    # backed by explicit user feedback, so it should remain selectable even when
    # the local model has no live candidate.
    return False


def _active_rime_candidates_are_low_information(snapshot: RimeContextSnapshot) -> bool:
    active_input = compact_whitespace(snapshot.preedit or snapshot.raw_input)
    if not active_input or not snapshot.candidates:
        return False
    return not _rime_candidates_have_meaningful_signal(snapshot)


def _forced_refresh_has_side_signal(snapshot: RimeContextSnapshot, query_basis: str, semantic_query: str) -> bool:
    if query_basis == "commitTextPreview":
        return _commit_preview_has_prediction_signal(semantic_query)
    active_input = compact_whitespace(snapshot.preedit or snapshot.raw_input)
    if not active_input:
        return False
    if query_basis == "rawInputFallback":
        return True
    if query_basis not in {"rimeCandidates", "rawSemanticInput", "preedit", "foregroundText"}:
        return False
    pinyin_signal = re.sub(r"[^A-Za-z0-9]+", "", active_input)
    return len(pinyin_signal) >= 4


def _rime_candidates_have_meaningful_signal(snapshot: RimeContextSnapshot) -> bool:
    for candidate in snapshot.candidates[:6]:
        if _text_has_meaningful_ime_signal(candidate.text):
            return True
    return False


def _text_has_meaningful_ime_signal(text: str) -> bool:
    surface = compact_whitespace(text)
    if not surface:
        return False
    lowered = surface.lower()
    if lowered in _SEMANTIC_ASCII_TERMS:
        return True
    ascii_terms = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}", surface)
    if any(term.lower() in _SEMANTIC_ASCII_TERMS for term in ascii_terms):
        return True
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", surface)
    for run in cjk_runs:
        if len(run) >= 2 and any(char not in _LOW_INFORMATION_CJK_TOKENS for char in run):
            return True
    return False


def stable_short_pinyin_prefix(snapshot: RimeContextSnapshot) -> str:
    """Return a stable user pinyin prefix that can constrain model/RAG lookup.

    Long raw strings are often typo-heavy pinyin fragments; those should not be
    used as semantic RAG queries. A short prefix such as "sj" and a complete
    single syllable such as "xiang" are different: they are the user's active
    constraint and should match the phrase-memory pinyin index.
    """

    prefix = compact_whitespace(snapshot.preedit or snapshot.raw_input).lower()
    if not prefix or not prefix.isascii() or not prefix.isalnum():
        return ""
    if 1 <= len(prefix) <= 4:
        return prefix
    return prefix if _looks_like_single_pinyin_syllable(prefix) else ""


_PINYIN_SYLLABLE_FINALS = (
    "iang",
    "iong",
    "uang",
    "ang",
    "eng",
    "ing",
    "ong",
    "iao",
    "ian",
    "uan",
    "uai",
    "uei",
    "ui",
    "uo",
    "ua",
    "ue",
    "ve",
    "ai",
    "ei",
    "ao",
    "ou",
    "an",
    "en",
    "in",
    "un",
    "er",
    "a",
    "o",
    "e",
    "i",
    "u",
    "v",
)


_PINYIN_SYLLABLE_INITIALS = (
    "zh",
    "ch",
    "sh",
    "b",
    "p",
    "m",
    "f",
    "d",
    "t",
    "n",
    "l",
    "g",
    "k",
    "h",
    "j",
    "q",
    "x",
    "r",
    "z",
    "c",
    "s",
    "y",
    "w",
    "",
)


def _looks_like_single_pinyin_syllable(prefix: str) -> bool:
    if not 5 <= len(prefix) <= 6:
        return False
    for initial in _PINYIN_SYLLABLE_INITIALS:
        if not prefix.startswith(initial):
            continue
        final = prefix[len(initial) :]
        if final in _PINYIN_SYLLABLE_FINALS:
            return True
    return False


def merge_display_candidates(
    *,
    snapshot: RimeContextSnapshot,
    model_predictions: list[ModelPrediction],
    suggestions: list[InputSuggestion],
) -> list[SideCandidateDisplayItem]:
    max_visible = snapshot.max_visible_candidates
    display: list[SideCandidateDisplayItem] = []
    display_texts: set[str] = set()
    raw_english = raw_english_candidate_text(snapshot)
    if raw_english:
        display_texts.add(_display_text_norm(raw_english))
        display.append(
            SideCandidateDisplayItem(
                label=_display_label("", len(display)),
                text=raw_english,
                insert_text=raw_english,
                source_type="raw_english",
                selection_action="commit_side_candidate",
                source_index=0,
                comment="english",
                display_layout="inline",
                display_lane="input",
                metadata={"candidate_mode": "raw-english"},
            )
        )
        return display
    input_mode = infer_input_mode(snapshot)
    rime_reserve = _display_rime_reserve(snapshot=snapshot, mode=input_mode, max_visible=max_visible)
    side_budget = min(snapshot.max_side_candidates, max(0, max_visible - rime_reserve))
    model_items = list(model_predictions)
    rag_items: list[tuple[int, InputSuggestion]] = []
    memory_items: list[tuple[int, InputSuggestion]] = []
    for index, suggestion in enumerate(suggestions):
        metadata = dict(suggestion.metadata)
        source_type = str(metadata.get("source_type") or "rag")
        if source_type == "memory":
            memory_items.append((index, suggestion))
        else:
            rag_items.append((index, suggestion))

    side_inserted = 0
    model_inserted = 0
    suggestion_inserted = 0
    has_suggestion_items = bool(rag_items or memory_items)
    suggestion_count = len(rag_items) + len(memory_items)
    model_slot_cap = _display_max_model_side_candidates(side_budget, has_suggestions=has_suggestion_items)
    if side_budget <= 1:
        suggestion_reserve = 0 if model_items else min(suggestion_count, 1)
    elif side_budget == 2:
        suggestion_reserve = min(suggestion_count, 1)
    else:
        minimum_model_slots = min(2, len(model_items))
        suggestion_reserve = min(
            suggestion_count,
            rag_block_reserve(side_budget),
            max(0, side_budget - minimum_model_slots),
        )
    model_before_suggestions_limit = min(model_slot_cap, max(0, side_budget - suggestion_reserve))

    def append_model() -> bool:
        nonlocal side_inserted, model_inserted
        while model_items:
            if (
                side_inserted >= side_budget
                or model_inserted >= model_slot_cap
                or len(display) >= max_visible - rime_reserve
            ):
                return False
            prediction = model_items.pop(0)
            candidate_insert_text = _strip_candidate_source_suffix(
                str(prediction.metadata.get("insert_text") or prediction.text)
            )
            # A comma can be necessary when the completion is committed after
            # a complete clause, but it is visual noise at the start of a row.
            # Keep it in insertText and render the semantic phrase itself.
            candidate_text = re.sub(
                r"^[,，。！？；;、\s]+",
                "",
                _strip_candidate_source_suffix(prediction.text),
            )
            candidate_insert_text = _strip_candidate_source_suffix(
                candidate_insert_text
            )
            normalized_text = _display_text_norm(candidate_text)
            if not normalized_text or normalized_text in display_texts:
                continue
            display_texts.add(normalized_text)
            display.append(
                SideCandidateDisplayItem(
                    label=_display_label("", len(display)),
                    text=candidate_text,
                    insert_text=candidate_insert_text,
                    source_type="model",
                    selection_action="commit_side_candidate",
                    source_index=prediction.rank - 1,
                    comment=prediction.provider_name,
                    display_layout="block",
                    display_lane="model",
                    metadata={
                        "providerName": prediction.provider_name,
                        "latencyMs": prediction.latency_ms,
                        "confidence": prediction.confidence,
                        **dict(prediction.metadata),
                    },
                )
            )
            side_inserted += 1
            model_inserted += 1
            return True
        return False

    def append_suggestion(group: list[tuple[int, InputSuggestion]]) -> bool:
        nonlocal side_inserted, suggestion_inserted
        while group:
            if (
                side_inserted >= side_budget
                or suggestion_inserted >= suggestion_reserve
                or len(display) >= max_visible - rime_reserve
            ):
                return False
            source_index, suggestion = group.pop(0)
            candidate_text = _strip_candidate_source_suffix(suggestion.surface_text)
            candidate_insert_text = _strip_candidate_source_suffix(
                str(suggestion.metadata.get("insert_text") or candidate_text)
            )
            normalized_text = _display_text_norm(candidate_text)
            if not normalized_text or normalized_text in display_texts:
                continue
            display_texts.add(normalized_text)
            metadata = dict(suggestion.metadata)
            source_type = str(metadata.get("source_type") or "rag")
            if source_type not in {"rag", "memory"}:
                source_type = "rag"
            display.append(
                SideCandidateDisplayItem(
                    label=_display_label("", len(display)),
                    text=candidate_text,
                    insert_text=candidate_insert_text,
                    source_type=source_type,
                    selection_action="commit_side_candidate",
                    source_index=source_index,
                    comment=suggestion.suggestion_type,
                    evidence_preview=suggestion.evidence_preview,
                    expanded_evidence=suggestion.expanded_evidence,
                    suggestion_id=suggestion.suggestion_id,
                    memory_id=str(metadata.get("memory_id") or suggestion.suggestion_id),
                    source_event_id=suggestion.source_event_id,
                    display_layout="block",
                    display_lane="memory",
                    metadata=metadata,
                )
            )
            side_inserted += 1
            suggestion_inserted += 1
            return True
        return False

    while side_inserted < model_before_suggestions_limit and len(display) < max_visible - rime_reserve:
        if not append_model():
            break
    append_suggestion(rag_items)
    append_suggestion(memory_items)
    while side_inserted < side_budget and len(display) < max_visible - rime_reserve:
        progressed = append_model()
        if not progressed:
            break
    for candidate in snapshot.candidates:
        if len(display) >= max_visible:
            break
        normalized_text = _display_text_norm(candidate.text)
        if normalized_text in display_texts:
            continue
        display_texts.add(normalized_text)
        display.append(
            SideCandidateDisplayItem(
                label=_display_label(candidate.label if not display else "", len(display)),
                text=candidate.text,
                insert_text=candidate.text,
                source_type="rime",
                selection_action="select_rime_candidate",
                source_index=candidate.index,
                comment=candidate.comment,
                rime_index=candidate.index,
                display_layout="fallback",
                display_lane=candidate.comment or "wanxiang",
                metadata={"candidate_mode": "composition-rime"},
            )
        )
    return display


def rime_only_display_candidates(snapshot: RimeContextSnapshot) -> list[SideCandidateDisplayItem]:
    display: list[SideCandidateDisplayItem] = []
    max_visible = max(0, int(snapshot.max_visible_candidates))
    seen: set[str] = set()
    for candidate in snapshot.candidates:
        if len(display) >= max_visible:
            break
        text = compact_whitespace(candidate.text)
        if not text:
            continue
        normalized_text = _display_text_norm(text)
        if normalized_text in seen:
            continue
        seen.add(normalized_text)
        display.append(
            SideCandidateDisplayItem(
                label=_display_label(candidate.label if not display else "", len(display)),
                text=text,
                insert_text=text,
                source_type="rime",
                selection_action="select_rime_candidate",
                source_index=candidate.index,
                comment=candidate.comment,
                rime_index=candidate.index,
                display_layout="fallback",
                display_lane="rime",
            )
        )
    return display


def append_post_commit_active_rag_action(
    *,
    display_candidates: list[SideCandidateDisplayItem],
    snapshot: RimeContextSnapshot,
    input_mode: InputMode,
) -> bool:
    if input_mode != InputMode.POST_COMMIT_PREDICTING:
        return False
    if not post_commit_active_rag_button_enabled():
        return False
    existing = {item.selection_action for item in display_candidates if item.source_type == "action"}
    action_specs = (
        (
            "快速生成",
            "start_active_rag_from_context",
            "active_rag_generate",
            "ctrl+.",
            "active_rag",
        ),
        (
            "深度查找",
            "start_agent_deep_search_from_context",
            "agent_deep_search",
            "",
            "agent_deep_search",
        ),
    )
    for text, selection_action, button_role, shortcut, lane in action_specs:
        if selection_action in existing:
            continue
        display_candidates.append(
            SideCandidateDisplayItem(
                label="",
                text=text,
                insert_text="",
                source_type="action",
                selection_action=selection_action,
                source_index=len(display_candidates),
                comment=lane,
                display_layout="action_bar",
                display_lane=lane,
                metadata={
                    "candidate_mode": "post-commit-semantic-action-button",
                    "activeRagTrigger": selection_action == "start_active_rag_from_context",
                    "agentDeepSearchTrigger": selection_action == "start_agent_deep_search_from_context",
                    "buttonRole": button_role,
                    "buttonLabel": text,
                    "shortcutHint": shortcut,
                    "numericSelectionDisabled": True,
                    "triggerPolicy": "manual_only",
                    "requiresExplicitSelection": True,
                    "intent": "deep_search" if button_role == "agent_deep_search" else "complete",
                    "placement": "control_center" if button_role == "agent_deep_search" else "insert_after_selection",
                    "maxCandidates": 1,
                    "requiresPi": True,
                    "requiresScreenshot": False,
                },
            )
        )
    return True


def ui_mode_for_response(
    *,
    input_mode: str,
    display_candidates: list[SideCandidateDisplayItem],
    status_row_visible: bool = False,
) -> str:
    if input_mode in {InputMode.ANCHOR_COMPOSING.value, InputMode.PREFIX_CONSTRAINED_COMPOSING.value}:
        if display_candidates and not all(item.source_type == "rime" for item in display_candidates):
            return "active_rag_assist"
        return "composition_rime"
    if input_mode == InputMode.POST_COMMIT_PREDICTING.value:
        has_prediction = any(
            item.source_type in {"model", "rag", "memory"} and _display_item_is_selectable(item)
            for item in display_candidates
        )
        if has_prediction:
            return "post_commit_prediction"
        if status_row_visible:
            return "post_commit_pending"
        return "post_commit_pending"
    if not display_candidates:
        return "empty"
    return "active_rag_assist"


def _display_rime_reserve(*, snapshot: RimeContextSnapshot, mode: object, max_visible: int) -> int:
    mode_value = getattr(mode, "value", str(mode))
    if mode_value not in {"anchor_composing", "prefix_constrained_composing"}:
        return 0
    rime_count = sum(1 for candidate in snapshot.candidates if compact_whitespace(candidate.text))
    if rime_count <= 0:
        return 0
    if max_visible >= 8:
        return min(3, rime_count)
    if max_visible >= 5:
        return min(2, rime_count)
    return min(1, rime_count)


def _display_text_norm(text: str) -> str:
    return compact_whitespace(text).lower()


def raw_english_candidate_text(snapshot: RimeContextSnapshot) -> str:
    raw = compact_whitespace(snapshot.raw_input)
    if not raw:
        raw = compact_whitespace(snapshot.preedit)
    return raw if looks_like_raw_commit_ascii_input(raw) else ""


def semantic_ascii_input_text(snapshot: RimeContextSnapshot) -> str:
    raw = compact_whitespace(snapshot.raw_input)
    if not raw:
        raw = compact_whitespace(snapshot.preedit)
    return raw if looks_like_semantic_ascii_input(raw) else ""


def looks_like_semantic_ascii_input(raw: str) -> bool:
    if not _is_ascii_text_input(raw):
        return False
    if raw.lower() in _SEMANTIC_ASCII_TERMS:
        return True
    return looks_like_raw_commit_ascii_input(raw)


def looks_like_raw_commit_ascii_input(raw: str) -> bool:
    if not _is_ascii_text_input(raw):
        return False
    parts = raw.split()
    if parts and parts[0].lower() in _SHELL_COMMAND_PREFIXES:
        return True
    if raw.lower() in _RAW_COMMIT_ASCII_TERMS:
        return True
    code_delimiters = set("_./:-+=<>[]{}()$@#\\|")
    has_code_delimiter = any(char in code_delimiters for char in raw)
    has_upper = any(char.isupper() for char in raw)
    has_lower = any(char.islower() for char in raw)
    has_digit = any(char.isdigit() for char in raw)
    has_space = any(char.isspace() for char in raw)
    return (
        has_code_delimiter
        or (has_space and len(parts) >= 2)
        or (has_upper and has_lower)
        or (has_upper and len(raw) >= 2)
        or (has_digit and len(raw) >= 2)
    )


def _is_ascii_text_input(raw: str) -> bool:
    if not raw or len(raw) > 80:
        return False
    if raw != compact_whitespace(raw):
        return False
    if not all(32 <= ord(char) <= 126 for char in raw):
        return False
    if not any(char.isalpha() for char in raw):
        return False
    return True


def max_model_side_candidates(side_budget: int) -> int:
    if side_budget <= 0:
        return 0
    return min(3, side_budget)


def _display_max_model_side_candidates(side_budget: int, *, has_suggestions: bool) -> int:
    if side_budget <= 0:
        return 0
    if has_suggestions:
        return max_model_side_candidates(side_budget)
    return min(3, side_budget)


def rag_block_reserve(side_budget: int) -> int:
    if side_budget <= 1:
        return 0
    return 1


def rime_context_to_payload(snapshot: RimeContextSnapshot) -> dict[str, object]:
    return {
        "candidates": [
            {
                "index": item.index,
                "label": item.label or _display_label("", index),
                "text": item.text,
                "comment": item.comment,
            }
            for index, item in enumerate(snapshot.candidates)
        ],
        "highlightedIndex": snapshot.highlighted_index,
        "page": snapshot.page,
        "isLastPage": snapshot.is_last_page,
        "app": snapshot.app,
    }


def frontend_transaction_to_payload(transaction: FrontendTransaction) -> dict[str, object]:
    return {
        "frontendRevision": transaction.frontend_revision,
        "selectionEpoch": transaction.selection_epoch,
        "inputGeneration": transaction.input_generation,
        "frontAppBundleId": transaction.front_app_bundle_id,
        "inputSourceId": transaction.input_source_id,
        "compositionHash": transaction.composition_hash,
        "committedContextHash": transaction.committed_context_hash,
        "panelSessionId": transaction.panel_session_id,
        "createdAtMs": transaction.created_at_ms,
    }


def _record_display_memory_feedback(
    *,
    core: CoreClient,
    snapshot: RimeContextSnapshot,
    display_candidates: list[SideCandidateDisplayItem],
    trace_id: str,
    project: str,
) -> None:
    shown_candidate_ids = [
        item.memory_id or item.suggestion_id
        for item in display_candidates
        if item.source_type in {"rag", "memory"} and (item.memory_id or item.suggestion_id)
    ]
    transaction = snapshot.frontend_transaction
    for item in display_candidates:
        if item.source_type not in {"rag", "memory"}:
            continue
        # Progressive follow-ups rebuild the response for the same panel, so
        # the same visible candidate must not produce a second `shown` row.
        # Without a panel identity there is no follow-up to dedup against.
        if transaction.panel_session_id:
            seen_key = (
                transaction.panel_session_id,
                item.memory_id or item.suggestion_id,
                transaction.committed_context_hash,
            )
            with _DISPLAY_FEEDBACK_SEEN_LOCK:
                if seen_key in _DISPLAY_FEEDBACK_SEEN:
                    continue
                _DISPLAY_FEEDBACK_SEEN[seen_key] = None
                while len(_DISPLAY_FEEDBACK_SEEN) > _DISPLAY_FEEDBACK_SEEN_MAX:
                    _DISPLAY_FEEDBACK_SEEN.pop(next(iter(_DISPLAY_FEEDBACK_SEEN)))
        _record_memory_feedback_event(
            core=core,
            event={
                "event": "shown",
                "candidateId": item.memory_id or item.suggestion_id,
                "candidateText": item.text,
                "sourceType": item.source_type,
                "lane": item.display_lane or item.source_type,
                "rank": _candidate_rank(item.label),
                "contextHash": snapshot.frontend_transaction.committed_context_hash,
                "frontAppBundleId": snapshot.frontend_transaction.front_app_bundle_id or snapshot.app,
                "rawInput": snapshot.raw_input,
                "preedit": snapshot.preedit,
                "committedTail": snapshot.committed_context,
                "timestampMs": now_ms(),
                "project": project,
                "traceId": trace_id,
                "requestSeq": snapshot.request_seq,
                "sessionId": snapshot.session_id,
                "suggestionId": item.suggestion_id,
                "sourceEventId": item.source_event_id,
                "shownCandidateIds": shown_candidate_ids,
                "shownCandidateCount": len(shown_candidate_ids),
            },
        )


def _record_memory_feedback_event(*, core: CoreClient, event: dict[str, object]) -> None:
    recorder = getattr(core, "record_memory_feedback", None)
    if not callable(recorder):
        return
    try:
        recorder(dict(event))
    except Exception:
        # Feedback is a governance signal; typing and selection must stay live if
        # the memory store or future shared core is temporarily unavailable.
        return


def display_item_to_payload(item: SideCandidateDisplayItem) -> dict[str, object]:
    metadata = dict(item.metadata)
    numeric_selection_disabled = bool(metadata.get("numericSelectionDisabled"))
    selectable = _display_item_is_selectable(item)
    selection_key = None if numeric_selection_disabled else (item.label if selectable else None)
    source_badge = candidate_source_badge(item.source_type)
    color_token = candidate_color_token(item.source_type)
    text = _strip_candidate_source_suffix(item.text)
    insert_text = _strip_candidate_source_suffix(item.insert_text or text)
    group = _display_candidate_group(item)
    return {
        "label": item.label,
        "visibleLabel": metadata.get("visibleLabel") or item.label,
        "selectionKey": selection_key,
        "selectionRank": 0 if numeric_selection_disabled else _candidate_rank(selection_key or ""),
        "candidateOrdinal": _bounded_int(
            metadata.get("candidateOrdinal"),
            default=0 if numeric_selection_disabled else (_candidate_rank(selection_key or "") or 0),
            minimum=0,
            maximum=99,
        ),
        "candidateStableId": _string(metadata.get("candidateStableId")),
        "snapshotId": _string(metadata.get("snapshotId") or metadata.get("stableSnapshotId")),
        "snapshotGeneration": _bounded_int(
            metadata.get("snapshotGeneration"),
            default=0,
            minimum=0,
            maximum=2**63 - 1,
        ),
        "text": text,
        "insertText": insert_text,
        "sourceType": item.source_type,
        "selectionAction": item.selection_action,
        "sourceIndex": item.source_index,
        "comment": "",
        "badge": source_badge,
        "sourceBadge": metadata.get("sourceBadge") or source_badge,
        "colorToken": color_token,
        "sourceStability": metadata.get("sourceStability") or "fresh",
        "hardContextAnchor": _string(metadata.get("hardContextAnchor")),
        "queryAnchor": _string(metadata.get("queryAnchor")),
        "displayAnchor": _string(metadata.get("displayAnchor")),
        "expiresAtMs": _bounded_int(metadata.get("expiresAtMs"), default=0, minimum=0, maximum=2**63 - 1),
        "minVisibleUntilMs": _bounded_int(
            metadata.get("minVisibleUntilMs"),
            default=0,
            minimum=0,
            maximum=2**63 - 1,
        ),
        "evidencePreview": item.evidence_preview,
        "expandedEvidence": item.expanded_evidence,
        "suggestionId": item.suggestion_id,
        "memoryId": item.memory_id,
        "sourceEventId": item.source_event_id,
        "rimeIndex": item.rime_index,
        "displayLayout": item.display_layout,
        "displayLane": item.display_lane or item.source_type,
        "group": metadata.get("group") or group,
        "groupLabel": metadata.get("groupLabel") or _display_candidate_group_label(group),
        "isSelectable": selectable,
        "isStatus": item.source_type == "status" or item.display_layout == "status_row",
        "metadata": metadata,
    }


def _display_candidate_group(item: SideCandidateDisplayItem) -> str:
    if item.source_type == "status" or item.display_layout == "status_row":
        return "status"
    if item.source_type == "action":
        return "action"
    if item.source_type == "rime":
        return "rime"
    return "prediction"


def _display_candidate_group_label(group: str) -> str:
    return {
        "rime": "词库",
        "prediction": "预测",
        "status": "状态",
        "action": "操作",
    }.get(group, group)


def key_policy_for_prediction_session(prediction_session_payload: Mapping[str, object]) -> dict[str, object]:
    return contract_key_policy_for_prediction_session(prediction_session_payload)


def refresh_decision_payload(
    *,
    snapshot: RimeContextSnapshot,
    trigger_decision: RimeSideCandidateTriggerDecision,
    semantic_query: str,
    debounce: Mapping[str, object] | None = None,
) -> dict[str, object]:
    debounce_payload = dict(debounce or {})
    return {
        "shouldRefresh": trigger_decision.should_refresh,
        "refreshReason": trigger_decision.reason,
        "reason": trigger_decision.reason,
        "idleMs": snapshot.idle_ms,
        "semanticSignalLength": semantic_signal_length(semantic_query),
        "forceSideCandidates": snapshot.force_side_candidates,
        "debounced": bool(debounce_payload.get("debounced")),
        "debounceMs": int(debounce_payload.get("debounceMs") or _refresh_debounce_ms()),
        "coalescedWithAgeMs": int(debounce_payload.get("coalescedWithAgeMs") or 0),
        "debounceKey": _string(debounce_payload.get("debounceKey")),
    }


def show_decision_payload(
    *,
    prediction_session: Mapping[str, object],
    display_candidates: list[SideCandidateDisplayItem],
) -> dict[str, object]:
    stable_panel = prediction_session.get("stablePanel")
    stable_panel = stable_panel if isinstance(stable_panel, Mapping) else {}
    action = _string(prediction_session.get("stablePanelAction") or stable_panel.get("action"))
    reason = _string(prediction_session.get("stablePanelReason") or stable_panel.get("reason"))
    should_show = bool(display_candidates) and not _bool(
        prediction_session.get("shouldClearPredictionPanel"),
        default=False,
    )
    hard_clear = action == "hard_clear"
    soft_hold = action in {"soft_hold", "reuse_last_good", "prefix_filter"}
    if should_show:
        show_reason = reason or "visible_candidates"
    elif hard_clear:
        show_reason = reason or "hard_clear"
    else:
        show_reason = _string(prediction_session.get("clearReason")) or reason or "no_visible_candidates"
    return {
        "shouldShow": should_show,
        "showReason": show_reason,
        "action": action or ("show" if should_show else "hide"),
        "hardClear": hard_clear,
        "hardClearReason": reason if hard_clear else "",
        "softHold": soft_hold,
        "softHoldReason": reason if soft_hold else "",
        "visibleCandidateCount": len(display_candidates),
        "snapshotId": _string(prediction_session.get("snapshotId") or prediction_session.get("stableSnapshotId")),
        "sourceSummary": stable_panel.get("snapshot", {}).get("sourceSummary", {})
        if isinstance(stable_panel.get("snapshot"), Mapping)
        else {},
    }


def prediction_trace_events_payload(
    *,
    prediction_session: Mapping[str, object],
    refresh_decision: Mapping[str, object],
    show_decision: Mapping[str, object],
    rag_lane: Mapping[str, object],
    model_lane: Mapping[str, object],
    display_candidates: list[SideCandidateDisplayItem],
) -> list[dict[str, object]]:
    stable_panel = prediction_session.get("stablePanel")
    stable_panel = stable_panel if isinstance(stable_panel, Mapping) else {}
    action = _string(prediction_session.get("stablePanelAction") or stable_panel.get("action"))
    reason = _string(prediction_session.get("stablePanelReason") or stable_panel.get("reason"))
    snapshot_id = _string(prediction_session.get("snapshotId") or prediction_session.get("stableSnapshotId"))
    common = {
        "mode": _string(prediction_session.get("inputMode")),
        "phase": _string(prediction_session.get("phase")),
        "hardContextAnchor": _string(prediction_session.get("hardContextAnchor")),
        "applyAnchor": _string(prediction_session.get("applyAnchor")),
        "queryAnchor": _string(prediction_session.get("queryAnchor")),
        "displayAnchor": _string(prediction_session.get("displayAnchor")),
        "snapshotId": snapshot_id,
        "visibleCandidateCount": len(display_candidates),
        "sourceSummary": _display_source_summary(display_candidates),
        "action": action,
        "reason": reason,
        "ragTimedOut": bool(rag_lane.get("timedOut")),
        "modelTimedOut": bool(model_lane.get("timedOut")),
        "asyncMode": bool(model_lane.get("asyncMode")),
        "completionJobId": _string(model_lane.get("completionJobId")),
        "completionJobState": _string(model_lane.get("completionJobState")),
        "cacheHit": bool(model_lane.get("cacheHit")),
        "inFlight": bool(model_lane.get("inFlight")),
        "jobElapsedMs": _optional_int(model_lane.get("jobElapsedMs")) or 0,
        "firstResponseMs": _optional_int(model_lane.get("firstResponseMs")) or 0,
        "noPinyinFilter": bool(model_lane.get("noPinyinFilter")),
        "holdoverHit": bool(model_lane.get("holdoverHit")) or bool(rag_lane.get("holdoverHit")),
        "hardClearReason": _string(show_decision.get("hardClearReason")),
        "previousSnapshotId": _string(stable_panel.get("previousSnapshotId")),
        "preservedOrdinalCount": _bounded_int(
            stable_panel.get("preservedOrdinalCount"),
            default=0,
            minimum=0,
            maximum=99,
        ),
        "appendedCandidateCount": _bounded_int(
            stable_panel.get("appendedCandidateCount"),
            default=0,
            minimum=0,
            maximum=99,
        ),
        "reorderedCandidateCount": _bounded_int(
            stable_panel.get("reorderedCandidateCount"),
            default=0,
            minimum=0,
            maximum=99,
        ),
        "progressiveReorderRejected": bool(stable_panel.get("progressiveReorderRejected")),
        "prefixFiltered": bool(stable_panel.get("prefixFiltered")),
        "prefixRemovedCandidateCount": _bounded_int(
            stable_panel.get("prefixRemovedCandidateCount"),
            default=0,
            minimum=0,
            maximum=99,
        ),
        "prefixKeptSideCandidateCount": _bounded_int(
            stable_panel.get("prefixKeptSideCandidateCount"),
            default=0,
            minimum=0,
            maximum=99,
        ),
    }

    events: list[dict[str, object]] = [
        {"event": "prediction_anchor_computed", "fields": _non_empty_trace_fields(common)},
        {
            "event": "prediction_refresh_decision",
            "fields": _non_empty_trace_fields({**common, **dict(refresh_decision)}),
        },
        {
            "event": "prediction_show_decision",
            "fields": _non_empty_trace_fields({**common, **dict(show_decision)}),
        },
    ]

    if bool(model_lane.get("directMemoryHit")):
        events.append(
            {
                "event": "prediction_trigger_direct_memory_hit",
                "fields": _non_empty_trace_fields(
                    {
                        **common,
                        "sourceEventIds": list(rag_lane.get("directMemorySourceEventIds") or []),
                        "remoteDeepSeekAutoCallCount": 0,
                    }
                ),
            }
        )
    elif bool(model_lane.get("called")) and (
        not bool(model_lane.get("asyncMode"))
        or _string(model_lane.get("completionJobState")) == "started"
    ):
        events.append(
            {
                "event": "prediction_provider_called",
                "fields": _non_empty_trace_fields(
                    {
                        **common,
                        "providerCallCount": 1,
                        "remoteDeepSeekAutoCallCount": 0,
                    }
                ),
            }
        )

    if action == "fresh" and snapshot_id:
        events.append({"event": "prediction_snapshot_created", "fields": _non_empty_trace_fields(common)})
    elif action == "progressive_append" and snapshot_id:
        events.append({"event": "candidate_snapshot_progressive_append", "fields": _non_empty_trace_fields(common)})
    elif action == "progressive_replace" and snapshot_id:
        events.append({"event": "candidate_snapshot_progressive_replace", "fields": _non_empty_trace_fields(common)})
        events.append({"event": "prediction_snapshot_created", "fields": _non_empty_trace_fields(common)})
    elif action in {"soft_hold", "reuse_last_good", "prefix_filter"} and snapshot_id:
        events.append({"event": "prediction_snapshot_reused", "fields": _non_empty_trace_fields(common)})
    if action == "prefix_filter":
        events.append({"event": "prediction_prefix_filter_applied", "fields": _non_empty_trace_fields(common)})
    if action == "soft_hold":
        events.append({"event": "prediction_panel_soft_hold", "fields": _non_empty_trace_fields(common)})
    elif action == "soft_hide":
        events.append({"event": "prediction_panel_soft_hide", "fields": _non_empty_trace_fields(common)})
    elif action == "hard_clear":
        events.append({"event": "prediction_panel_hard_clear", "fields": _non_empty_trace_fields(common)})

    if bool(model_lane.get("asyncMode")):
        state = _string(model_lane.get("completionJobState"))
        if state == "started":
            events.append({"event": "post_commit_completion_job_started", "fields": _non_empty_trace_fields(common)})
        if state == "hit":
            events.append({"event": "post_commit_completion_job_cache_hit", "fields": _non_empty_trace_fields(common)})
        if bool(model_lane.get("inFlight")):
            events.append({"event": "post_commit_completion_job_pending", "fields": _non_empty_trace_fields(common)})
        if state == "hit" and _bounded_int(model_lane.get("predictionCount"), default=0, minimum=0, maximum=999) > 0:
            events.append({"event": "post_commit_completion_job_completed", "fields": _non_empty_trace_fields(common)})
        if state == "timeout" or bool(model_lane.get("timedOut")):
            events.append({"event": "post_commit_completion_job_timeout", "fields": _non_empty_trace_fields(common)})
        if state == "stale":
            events.append(
                {"event": "post_commit_completion_job_stale_dropped", "fields": _non_empty_trace_fields(common)}
            )
        if state == "error" or bool(model_lane.get("error")):
            events.append({"event": "post_commit_completion_job_error", "fields": _non_empty_trace_fields(common)})
        events.append(
            {"event": "post_commit_completion_first_response_returned", "fields": _non_empty_trace_fields(common)}
        )

    if bool(show_decision.get("shouldShow")) and _lane_empty_or_timed_out_for_trace(rag_lane, model_lane):
        events.append(
            {
                "event": "prediction_empty_lane_did_not_clear_panel",
                "fields": _non_empty_trace_fields(common),
            }
        )
    if (bool(rag_lane.get("timedOut")) or bool(model_lane.get("timedOut"))) and bool(show_decision.get("shouldShow")):
        events.append(
            {
                "event": "prediction_lane_timeout_with_holdover",
                "fields": _non_empty_trace_fields(common),
            }
        )
    elif bool(rag_lane.get("timedOut")) or bool(model_lane.get("timedOut")):
        events.append(
            {
                "event": "prediction_lane_timeout_without_holdover",
                "fields": _non_empty_trace_fields(common),
            }
        )
    return events


def _display_source_summary(display_candidates: list[SideCandidateDisplayItem]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in display_candidates:
        source = compact_whitespace(item.source_type)
        if not source:
            continue
        summary[source] = summary.get(source, 0) + 1
    return summary


def _lane_empty_or_timed_out_for_trace(
    rag_lane: Mapping[str, object],
    model_lane: Mapping[str, object],
) -> bool:
    return (
        bool(rag_lane.get("timedOut"))
        or bool(model_lane.get("timedOut"))
        or (bool(rag_lane.get("called")) and int(rag_lane.get("suggestionCount") or 0) <= 0)
        or (bool(model_lane.get("called")) and int(model_lane.get("predictionCount") or 0) <= 0)
    )


def _non_empty_trace_fields(fields: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in fields.items():
        if value in ("", None):
            continue
        result[key] = value
    return result


def _bind_display_candidates_to_session(
    *,
    display_candidates: list[SideCandidateDisplayItem],
    snapshot: RimeContextSnapshot,
    prediction_session_payload: Mapping[str, object],
    key_policy: Mapping[str, object] | None = None,
) -> list[SideCandidateDisplayItem]:
    session_fingerprint = _string(prediction_session_payload.get("sessionFingerprint"))
    context_fingerprint = _string(prediction_session_payload.get("contextFingerprint")) or _context_fingerprint(
        snapshot.committed_context
    )
    hard_context_anchor = _string(prediction_session_payload.get("hardContextAnchor"))
    apply_anchor = _string(prediction_session_payload.get("applyAnchor"))
    query_anchor = _string(prediction_session_payload.get("queryAnchor"))
    display_anchor = _string(prediction_session_payload.get("displayAnchor"))
    stable_snapshot_id = _string(prediction_session_payload.get("snapshotId")) or _string(
        prediction_session_payload.get("stableSnapshotId")
    )
    snapshot_generation = _bounded_int(
        prediction_session_payload.get("snapshotGeneration"),
        default=0,
        minimum=0,
        maximum=2**63 - 1,
    )
    min_visible_until_ms = _bounded_int(
        _nested_prediction_session_value(prediction_session_payload, "minVisibleUntilMs"),
        default=0,
        minimum=0,
        maximum=2**63 - 1,
    )
    expires_at_ms = _bounded_int(
        _nested_prediction_session_value(prediction_session_payload, "expiresAtMs"),
        default=0,
        minimum=0,
        maximum=2**63 - 1,
    )
    source_stability = _source_stability_from_session(prediction_session_payload)
    phase = _string(prediction_session_payload.get("phase"))
    scope = _string(prediction_session_payload.get("selectionScope"))
    transaction = snapshot.frontend_transaction
    bound: list[SideCandidateDisplayItem] = []
    ordinal = 0
    for item in display_candidates:
        metadata = dict(item.metadata)
        selectable = _display_item_is_selectable(item)
        numeric_selection_disabled = bool(metadata.get("numericSelectionDisabled"))
        numbered_selectable = selectable and not numeric_selection_disabled
        if numbered_selectable:
            ordinal += 1
        candidate_ordinal = ordinal if numbered_selectable else 0
        display_label = _display_label("", candidate_ordinal - 1) if numbered_selectable else ""
        metadata.update(
            {
                "sessionFingerprint": session_fingerprint,
                "contextFingerprint": context_fingerprint,
                "hardContextAnchor": hard_context_anchor,
                "applyAnchor": apply_anchor,
                "queryAnchor": query_anchor,
                "displayAnchor": display_anchor,
                "snapshotId": stable_snapshot_id,
                "stableSnapshotId": stable_snapshot_id,
                "snapshotGeneration": snapshot_generation,
                "candidateStableId": _candidate_stable_id(item, snapshot_id=stable_snapshot_id),
                "candidateOrdinal": candidate_ordinal,
                "visibleLabel": display_label,
                "sourceBadge": candidate_source_badge(item.source_type),
                "sourceStability": source_stability,
                "expiresAtMs": expires_at_ms,
                "minVisibleUntilMs": min_visible_until_ms,
                "keyPolicy": dict(key_policy or {}),
                "requestSeq": snapshot.request_seq,
                "sessionId": snapshot.session_id,
                "predictionSessionPhase": phase,
                "selectionScope": scope,
                "frontendRevision": transaction.frontend_revision,
                "selectionEpoch": transaction.selection_epoch,
                "frontAppBundleId": transaction.front_app_bundle_id,
                "inputSourceId": transaction.input_source_id,
                "compositionHash": transaction.composition_hash,
                "committedContextHash": transaction.committed_context_hash,
                "panelSessionId": transaction.panel_session_id,
            }
        )
        bound.append(replace(item, label=display_label, metadata=metadata))
    return bound


def _display_item_is_selectable(item: SideCandidateDisplayItem) -> bool:
    if item.selection_action in {"", "none"} or item.source_type == "status":
        return False
    if item.source_type == "action":
        return True
    return bool(compact_whitespace(item.label))


def _nested_prediction_session_value(prediction_session_payload: Mapping[str, object], key: str) -> object:
    if key in prediction_session_payload:
        return prediction_session_payload.get(key)
    stable_panel = prediction_session_payload.get("stablePanel")
    if not isinstance(stable_panel, Mapping):
        return None
    snapshot = stable_panel.get("snapshot")
    if isinstance(snapshot, Mapping):
        return snapshot.get(key)
    return None


def _source_stability_from_session(prediction_session_payload: Mapping[str, object]) -> str:
    if _bool(prediction_session_payload.get("reusedLastGood"), default=False):
        return "reused_last_good"
    stable_panel = prediction_session_payload.get("stablePanel")
    if isinstance(stable_panel, Mapping):
        snapshot = stable_panel.get("snapshot")
        if isinstance(snapshot, Mapping):
            stale_level = _string(snapshot.get("staleLevel"))
            if stale_level:
                return stale_level
    return "fresh"


def _candidate_stable_id(item: SideCandidateDisplayItem, *, snapshot_id: str) -> str:
    # Progressive model output mutates text in place. Identity belongs to the
    # snapshot lane/slot, otherwise every token delta looks like a new row and
    # invalidates the user's current keyboard selection.
    slot_identity = item.selection_action if item.source_type == "action" else str(item.source_index)
    material = "\x1f".join(
        (
            snapshot_id,
            item.source_type,
            slot_identity,
        )
    )
    return f"{item.source_type}:{hashlib.sha1(material.encode('utf-8')).hexdigest()[:16]}"


def _prediction_session_expiry_ms(prediction_session_payload: Mapping[str, object]) -> int:
    phase = _string(prediction_session_payload.get("phase"))
    if phase == "post_commit":
        return _POST_COMMIT_PANEL_TTL_MS
    if phase == "prefix_constrained":
        return _PREFIX_CONSTRAINED_PANEL_TTL_MS
    return 0


def _response_session_fingerprint(
    *,
    snapshot: RimeContextSnapshot,
    mode: str,
    display_candidates: tuple[SideCandidateDisplayItem, ...],
) -> str:
    visible_material = "\x1e".join(
        f"{item.label}:{item.selection_action}:{item.source_type}:{item.source_index}:{item.text}"
        for item in display_candidates[: snapshot.max_visible_candidates]
    )
    material = "\x1f".join(
        (
            snapshot.session_id,
            str(snapshot.request_seq),
            _context_fingerprint(snapshot.committed_context),
            compact_whitespace(snapshot.raw_input),
            compact_whitespace(snapshot.preedit),
            mode,
            visible_material,
        )
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _context_fingerprint(committed_context: str) -> str:
    return compact_whitespace(committed_context)[-420:]


def _parse_rime_candidates(value: object) -> list[RimeCandidate]:
    if not isinstance(value, list):
        return []
    candidates: list[RimeCandidate] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            text = item
            label = ""
            comment = ""
            rime_index = index
        elif isinstance(item, dict):
            text = _string(item.get("text") or item.get("candidate"))
            label = _string(item.get("label"))
            comment = _string(item.get("comment"))
            rime_index = _bounded_int(item.get("index"), default=index, minimum=0, maximum=999)
        else:
            continue
        text = compact_whitespace(text)
        if text:
            candidates.append(RimeCandidate(text=text, label=label, comment=comment, index=rime_index))
    return candidates


def _display_label(label: str, zero_based_index: int) -> str:
    if label:
        return label
    number = (zero_based_index + 1) % 10
    return "0" if number == 0 else str(number)


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        return default
    return max(minimum, min(maximum, parsed))


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return default


def _first_present(primary: dict[str, Any], fallback: dict[str, Any], key: str) -> object:
    if key in primary:
        return primary.get(key)
    return fallback.get(key)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _candidate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("candidate") or payload.get("displayCandidate")
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be a JSON object")
    return candidate


def _shown_candidate_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = (
        payload.get("shownCandidates")
        or payload.get("displayCandidates")
        or payload.get("shown_candidates")
        or payload.get("display_candidates")
    )
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _candidate_display_rank(candidate: dict[str, Any]) -> int | None:
    return (
        _optional_int(candidate.get("selectionRank") or candidate.get("selection_rank"))
        or _candidate_rank(
            _string(
                candidate.get("selectionKey")
                or candidate.get("selection_key")
                or candidate.get("label")
            )
        )
    )


def _same_candidate_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_id = (
        _string(left.get("suggestionId") or left.get("suggestion_id")),
        _optional_int(left.get("sourceEventId") or left.get("source_event_id")),
        _string(left.get("memoryId") or left.get("memory_id")),
    )
    right_id = (
        _string(right.get("suggestionId") or right.get("suggestion_id")),
        _optional_int(right.get("sourceEventId") or right.get("source_event_id")),
        _string(right.get("memoryId") or right.get("memory_id")),
    )
    if any(left_id) and left_id == right_id:
        return True
    return (
        _string(left.get("sourceType") or left.get("source_type"))
        == _string(right.get("sourceType") or right.get("source_type"))
        and _string(left.get("text")) == _string(right.get("text"))
        and _string(left.get("insertText") or left.get("insert_text"))
        == _string(right.get("insertText") or right.get("insert_text"))
    )


def _apply_candidate_memory_action(
    *,
    core: CoreClient,
    candidate: dict[str, Any],
    action_type: str,
    query: str,
    metadata: dict[str, object],
) -> dict[str, object] | None:
    source_type = _string(candidate.get("sourceType") or candidate.get("source_type"))
    memory_id = _string(candidate.get("memoryId") or candidate.get("memory_id"))
    suggestion_id = _string(candidate.get("suggestionId") or candidate.get("suggestion_id"))
    source_event_id = _optional_int(candidate.get("sourceEventId") or candidate.get("source_event_id"))
    if (
        source_type not in {"rag", "memory"}
        or not memory_id
        or not suggestion_id
        or source_event_id is None
        or source_event_id <= 0
    ):
        return None
    try:
        action = core.apply_action(
            MemoryAction(
                action_id=None,
                created_at_ms=now_ms(),
                memory_id=memory_id,
                action_type=action_type,
                query=query,
                suggestion_id=suggestion_id,
                source_event_id=source_event_id,
                metadata=metadata,
            )
        )
    except Exception:
        return None
    return action_response_payload(action)


def _apply_committed_event_feedback(
    *,
    core: CoreClient,
    event_id: str,
    action_type: str,
    query: str,
    suggestion_id: str,
    metadata: dict[str, object],
) -> dict[str, object] | None:
    source_event_id = _event_id_from_memory_id(event_id)
    if source_event_id is None:
        return None
    try:
        action = core.apply_action(
            MemoryAction(
                action_id=None,
                created_at_ms=now_ms(),
                memory_id=event_id,
                action_type=action_type,
                query=query,
                suggestion_id=suggestion_id,
                source_event_id=source_event_id,
                metadata=metadata,
            )
        )
    except Exception:
        return None
    return action_response_payload(action)


def _selection_feedback_base_event(
    *,
    payload: dict[str, Any],
    candidate: dict[str, Any],
    project: str,
    query: str,
    recent_context: str,
    shown_candidate_ids: list[str],
) -> dict[str, object]:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    assert isinstance(metadata, dict)
    frontend_transaction = payload.get("frontendTransaction") if isinstance(payload.get("frontendTransaction"), dict) else {}
    assert isinstance(frontend_transaction, dict)
    normalized_shown_candidate_ids = [item for item in shown_candidate_ids if item]
    context_hash = _string(
        payload.get("committedContextHash")
        or metadata.get("committedContextHash")
        or payload.get("contextHash")
        or frontend_transaction.get("committedContextHash")
    )
    front_app_bundle_id = _string(
        payload.get("frontAppBundleId")
        or metadata.get("frontAppBundleId")
        or payload.get("app")
    )
    return {
        "contextHash": context_hash,
        "frontAppBundleId": front_app_bundle_id,
        "rawInput": _string(payload.get("rawInput")),
        "preedit": _string(payload.get("preedit")),
        "committedTail": recent_context,
        "recentContext": recent_context,
        "timestampMs": now_ms(),
        "project": project,
        "query": query,
        "traceId": _string(payload.get("traceId") or metadata.get("traceId")),
        "requestSeq": _optional_int(payload.get("requestSeq")) or 0,
        "sessionId": _string(payload.get("sessionId") or metadata.get("sessionId")),
        "shownCandidateIds": normalized_shown_candidate_ids,
        "shownCandidateCount": len(normalized_shown_candidate_ids),
    }


def _selection_memory_context(*, recent_context: str, query: str) -> str:
    context = compact_whitespace(recent_context)
    query_text = compact_whitespace(query)
    if not query_text:
        return context
    if query_text in context:
        return context
    if not context:
        return query_text
    return compact_whitespace(f"{context} {query_text}")


def _event_id_from_memory_id(memory_id: str) -> int | None:
    value = _string(memory_id)
    if not value.startswith("event:"):
        return None
    try:
        event_id = int(value.split(":", 1)[1])
    except (TypeError, ValueError):
        return None
    return event_id if event_id > 0 else None


def _candidate_rank(label: str) -> int | None:
    if not label:
        return None
    if label == "0":
        return 10
    if label.isdigit():
        return int(label)
    return None
