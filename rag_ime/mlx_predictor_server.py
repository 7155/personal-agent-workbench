from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import time
from copy import deepcopy
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

from .predictor import (
    PREDICTION_REQUEST_ACTIVE_RAG,
    PREDICTION_REQUEST_GENERIC,
    PREDICTION_REQUEST_IME_HOT,
    PREDICTION_REQUEST_IME_POST_COMMIT,
    PREDICTION_REQUEST_IME_QUALITY,
    PREDICTION_REQUEST_NO_INPUT,
    PREDICTION_REQUEST_PINYIN_CONSTRAINED,
    PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
    PREDICTION_REQUEST_RIME_REORDER,
    normalize_prediction_request_type,
    normalized_rime_candidate_texts,
    parse_ime_prediction_candidates,
    parse_prediction_candidates,
)
from .anti_echo import candidate_has_self_repetition, collapse_repeated_tail
from .ime_candidate_stream import ImeCandidateStreamParser
from .model_lane_scheduler import LatestWinsModelScheduler, model_request_token_from_metadata
from .model_profiles import profile_by_id
from .model_registry import fingerprint_model_artifact
from .mlx_prefix_cache import MlxPrefixCache, PrefixCacheEntry
from .predictor_latency import (
    append_latency_trace,
    trace_from_prediction_payload,
)
from .sequence_fork import BranchSpec, run_sequence_fork
from .text_utils import compact_whitespace


SYSTEM_PROMPT = (
    "你是本地中文输入法的续写候选模型。根据已上屏文本预测光标后最可能继续输入的中文短语。"
    "只返回 JSON 字符串数组, 不要解释, 不要编号, 不要输出拼音, 不要输出思考过程。"
    "每个候选必须是后文增量, 不能复述已上屏文本、提示词、格式说明或固定示例。"
    "上下文太短或不确定时可以返回空数组。"
    "不要输出泛词或模板词: 根据、基于、和、测试、分析、验证、假设、或者、现在、目前、当前、然后、模型相关表达、调试流程。"
)

STREAM_FIRST_SYSTEM_PROMPT = (
    "补全用户正在写的中文句子。"
    "只输出光标后的具体中文内容，5到16个字，不要解释。"
    "不要只输出“下一步”或“接下来”这种过渡词。"
)

SPACE_LIST_SYSTEM_PROMPT = (
    "你是一个智能中文输入法，请根据上下文预测接下来最可能出现的短候选。"
    "只返回候选词或短语，不要解释，不要编号，不要 JSON。"
    "候选之间用单个空格分隔，按可能性从高到低排列。"
    "每个候选必须是能直接接在光标后的后文增量，不要复述上下文。"
    "不要只输出下一步、接下来这种过渡词；英文专名只可作为上下文对象，不要单独当候选。"
)

LOGITS_SYSTEM_PROMPT = (
    "你是本地中文输入法的续写候选模型。根据已上屏文本直接给出最可能接在光标后的中文短语。"
    "不要解释, 不要编号, 不要 JSON, 不要拼音, 不要思考过程。"
    "不要输出泛词或固定示例: 根据、基于、和、测试、分析、验证、假设、或者、现在、目前、当前、然后、模型相关表达、调试流程。"
)

QWEN_NON_THINKING_ASSISTANT_PREFIX = "<|im_start|>assistant\n<think>\n\n</think>\n\n"
GENERATION_STOP_MARKERS = (
    "<|im_end|>",
    "<|endoftext|>",
    "<|im_start|>",
    "\nHuman:",
    "\nAssistant:",
    "Human:",
    "Assistant:",
)

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LOW_VALUE_LOGITS_CANDIDATES = {
    "测试",
    "测试流程",
    "分析",
    "分析问题",
    "验证",
    "并且",
    "但是",
    "同时",
    "或者",
    "基于",
    "根据",
    "生成",
    "假设",
    "然后",
    "现在",
    "目前",
    "当前",
    "当前问题",
    "当前流程",
    "当前拼音或参考候选",
    "模型相关表达",
    "调试流程",
    "方案",
    "上屏",
    "上屏文字",
    "已上屏",
    "已上屏文本",
    "已上屏上下文",
    "下一步",
    "接下来",
    "下一句",
    "下一句：",
    "接龙",
    "把流程跑通",
    "接入本地",
    "接入本地记忆",
    "验证 LLM 候选",
    "验证LLM候选",
    "预测流程完成",
    "部署 RAG 组件",
    "部署RAG组件",
    "继续预测",
    "预测流程",
    "的",
    "了",
    "和",
    "是",
}
_COMPLETION_DOMAIN_TERMS = (
    "rag",
    "llm",
    "ui",
    "ds",
    "模型",
    "上下文",
    "预测",
    "候选",
    "输入法",
    "前端",
    "后端",
    "接口",
    "缓存",
    "数据",
    "样本",
    "延迟",
    "推理",
    "过滤",
    "生成",
    "配置",
    "日志",
    "脚本",
    "代码",
    "错误",
    "问题",
    "效果",
    "测试",
    "结果",
    "流程",
    "状态",
    "内存",
    "质量",
    "诊断",
    "来源",
    "管理",
    "优化",
    "调整",
    "训练",
    "速度",
    "方式",
)
_COMPLETION_HIGH_SIGNAL_TERMS = {
    "rag",
    "llm",
    "ui",
    "ds",
    "模型",
    "上下文",
    "预测",
    "候选",
    "输入法",
    "前端",
    "后端",
    "接口",
    "缓存",
}
_LOW_INFORMATION_COMPLETION_SEEDS = {
    "就",
    "的",
    "了",
    "是",
    "和",
    "也",
    "还",
    "再",
    "能",
    "要",
    "会",
    "把",
}


@dataclass(frozen=True)
class MlxPredictorServerConfig:
    host: str = "127.0.0.1"
    port: int = 8767
    model: str = ""
    max_tokens: int = 8
    temperature: float = 0.15
    top_p: float = 0.85
    prompt_cache: bool = False
    prompt_cache_max_kv_size: int = 0
    profile_id: str = "qwen3_06b_ime_hot"


@dataclass(frozen=True)
class _ContinuationBranchSpec:
    label: str
    temperature: float
    max_tokens: int
    max_candidate_chars: int


def _mlx_warmup_enabled() -> bool:
    return os.environ.get("RAG_IME_MLX_WARMUP", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _mlx_cache_limit_bytes() -> int:
    raw_limit = os.environ.get("RAG_IME_MLX_CACHE_LIMIT_MB", "256").strip()
    try:
        limit_mb = int(raw_limit)
    except ValueError:
        limit_mb = 256
    return min(max(0, limit_mb), 4096) * 1024 * 1024


def _configure_mlx_allocator_cache() -> tuple[dict[str, Any], Any | None]:
    limit_bytes = _mlx_cache_limit_bytes()
    status: dict[str, Any] = {
        "enabled": limit_bytes > 0,
        "configured": False,
        "limitBytes": limit_bytes,
    }
    if limit_bytes <= 0:
        return status, None
    try:
        import mlx.core as mx  # type: ignore

        set_cache_limit = getattr(mx, "set_cache_limit", None)
        if not callable(set_cache_limit):
            status["unsupported"] = True
            return status, mx
        previous_limit = set_cache_limit(limit_bytes)
        status["configured"] = True
        if previous_limit is not None:
            status["previousLimitBytes"] = int(previous_limit)
    except Exception as exc:  # pragma: no cover - depends on the installed MLX runtime
        status["error"] = type(exc).__name__
        return status, None
    return status, mx


def _mlx_memory_status(cache_status: dict[str, Any], mx: Any | None) -> dict[str, Any]:
    status = dict(cache_status)
    if mx is None:
        return status
    try:
        for field, getter_name in (
            ("activeBytes", "get_active_memory"),
            ("cacheBytes", "get_cache_memory"),
            ("peakBytes", "get_peak_memory"),
        ):
            getter = getattr(mx, getter_name, None)
            if callable(getter):
                status[field] = int(getter())
    except Exception as exc:  # pragma: no cover - depends on the installed MLX runtime
        status.setdefault("error", type(exc).__name__)
    return status


class _LocalTokenizersBackendWrapper:
    def __init__(self, tokenizer: Any, *, eos_token_id: int | None = None) -> None:
        self._tokenizer = tokenizer
        self.eos_token_id = eos_token_id
        self.eos_token_ids = {int(eos_token_id)} if eos_token_id is not None else set()

    def encode(self, text: str) -> list[int]:
        encoded = self._tokenizer.encode(str(text))
        ids = getattr(encoded, "ids", encoded)
        return [int(item) for item in ids]

    def decode(self, tokens: Iterable[int]) -> str:
        return str(self._tokenizer.decode([int(item) for item in tokens]))


def _load_mlx_model_and_tokenizer(model_id: str) -> tuple[Any, Any]:
    try:
        from mlx_lm import load  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on local Mac setup
        raise RuntimeError("Install mlx-lm before running mlx-predictor-server") from exc

    try:
        return load(model_id)
    except ValueError as exc:
        if not _should_use_local_tokenizers_backend_fallback(model_id, exc):
            raise
        from mlx_lm.utils import load_model  # type: ignore
        from tokenizers import Tokenizer  # type: ignore

        model_path = Path(model_id).expanduser()
        model, _config = load_model(model_path, lazy=False)
        tokenizer = Tokenizer.from_file(str(model_path / "tokenizer.json"))
        eos_token_id = _local_tokenizers_backend_eos_id(model_path, tokenizer)
        return model, _LocalTokenizersBackendWrapper(tokenizer, eos_token_id=eos_token_id)


def _should_use_local_tokenizers_backend_fallback(model_id: str, exc: ValueError) -> bool:
    model_path = Path(model_id).expanduser()
    if not model_path.exists() or not model_path.joinpath("tokenizer.json").exists():
        return False
    message = str(exc)
    return "TokenizersBackend" in message or "qwen3_5" in message


def _local_tokenizers_backend_eos_id(model_path: Path, tokenizer: Any) -> int | None:
    config_path = model_path / "tokenizer_config.json"
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    eos_token = config.get("eos_token")
    if not eos_token:
        return None
    token_to_id = getattr(tokenizer, "token_to_id", None)
    if not callable(token_to_id):
        return None
    token_id = token_to_id(str(eos_token))
    return int(token_id) if token_id is not None else None


class MlxLmEngine:
    """Resident MLX-LM engine for the IME model lane.

    This module intentionally imports MLX-LM lazily so the rest of RAG-IME can
    run on machines where MLX is not installed. The loaded process owns model
    memory; the IME talks to it over a tiny local HTTP protocol.
    """

    def __init__(
        self,
        model_id: str,
        *,
        enable_prompt_cache: bool = False,
        prompt_cache_max_kv_size: int = 0,
        profile_id: str = "qwen3_06b_ime_hot",
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ):
        if not model_id:
            raise RuntimeError("MLX predictor requires --model or RAG_IME_MLX_MODEL")
        self.model_id = model_id
        self.profile = profile_by_id(profile_id)
        self.configured_max_tokens = max(
            1,
            min(64, int(self.profile.max_tokens if max_tokens is None else max_tokens)),
        )
        self.configured_temperature = max(
            0.0,
            min(2.0, float(self.profile.temperature if temperature is None else temperature)),
        )
        self.configured_top_p = max(
            0.05,
            min(1.0, float(self.profile.top_p if top_p is None else top_p)),
        )
        self.model_info = _inspect_local_mlx_model(model_id)
        # MLX otherwise defaults to a multi-gigabyte Metal allocator cache. Cap
        # it before model loading so varied IME requests cannot grow indefinitely.
        self._mlx_allocator_cache, self._mlx_core = _configure_mlx_allocator_cache()
        self.model, self.tokenizer = _load_mlx_model_and_tokenizer(model_id)
        self.model_fingerprint = _local_model_fingerprint(model_id)
        self._base_completion_mode = _is_base_completion_model(model_id, self.model_info)
        self._prompt_cache = _PromptCacheState(
            enabled=bool(enable_prompt_cache) and not self._base_completion_mode,
            stable_prefix=self._stable_prompt_prefix(),
            max_kv_size=max(0, int(prompt_cache_max_kv_size)),
        )
        self._scheduler = LatestWinsModelScheduler()
        self._prefix_cache_enabled = os.environ.get("RAG_IME_MLX_PREFIX_CACHE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._prefix_cache_boundary_tokens = max(
            1,
            int(os.environ.get("RAG_IME_MLX_PREFIX_CACHE_BOUNDARY_TOKENS", "32")),
        )
        self._prefix_cache = MlxPrefixCache(
            max_entries=int(os.environ.get("RAG_IME_MLX_PREFIX_CACHE_MAX_ENTRIES", "64")),
            max_bytes=int(os.environ.get("RAG_IME_MLX_PREFIX_CACHE_MAX_MB", "256")) * 1024 * 1024,
        )
        self._last_prefix_cache_status: dict[str, Any] = {
            "enabled": self._prefix_cache_enabled,
            "cacheHit": False,
            "cacheHitTokens": 0,
            "cacheMissTokens": 0,
        }
        self._warmup_status: dict[str, Any] = {
            "enabled": _mlx_warmup_enabled(),
            "completed": False,
            "ok": False,
            "elapsedMs": 0,
            "candidateCount": 0,
        }
        if self._prompt_cache.enabled:
            self._prepare_prompt_cache()

    def warmup(self, *, max_tokens: int, temperature: float, top_p: float) -> dict[str, Any]:
        """Compile the resident MLX graph before the first foreground keystroke."""

        if not _mlx_warmup_enabled():
            self._warmup_status = {
                "enabled": False,
                "completed": True,
                "ok": True,
                "elapsedMs": 0,
                "candidateCount": 0,
                "skippedReason": "disabled",
            }
            return dict(self._warmup_status)
        started = time.perf_counter()
        try:
            payload = self.predict(
                current_input="",
                recent_context="输入法本地三候选已经准备，接下来",
                max_candidates=3,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                request_type=PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
                request_metadata={"requestId": "mlx-startup-warmup", "profileId": self.profile.id},
            )
            self._warmup_status = {
                "enabled": True,
                "completed": True,
                "ok": bool(payload.get("ok")),
                "elapsedMs": int((time.perf_counter() - started) * 1000),
                "candidateCount": len(payload.get("candidates") or []),
            }
        except Exception as exc:
            self._warmup_status = {
                "enabled": True,
                "completed": True,
                "ok": False,
                "elapsedMs": int((time.perf_counter() - started) * 1000),
                "candidateCount": 0,
                "error": type(exc).__name__,
            }
        return dict(self._warmup_status)

    def health(self) -> dict[str, Any]:
        prompt_cache = self.prompt_cache_status()
        model_profile = {
            **self.profile.to_payload(),
            "maxTokens": self.configured_max_tokens,
            "temperature": self.configured_temperature,
            "topP": self.configured_top_p,
        }
        prompt_mode = "base-completion" if self._base_completion_mode else "chat-json"
        return {
            "ok": True,
            "provider": "mlx-lm",
            "model": self.model_id,
            "modelFingerprint": self.model_fingerprint,
            "modelProfile": model_profile,
            "modelLoaded": True,
            "modelInfo": self.model_info,
            "warmup": dict(self._warmup_status),
            "promptCache": prompt_cache,
            "prefixCache": self.prefix_cache_status(),
            "mlxMemory": _mlx_memory_status(self._mlx_allocator_cache, self._mlx_core),
            "capabilities": {
                "streaming": True,
                "residentModel": True,
                "boundedAllocatorCache": bool(self._mlx_allocator_cache.get("configured")),
                "promptCache": _prompt_cache_used_for_generation(prompt_cache),
                "prefixCache": bool(self._prefix_cache_enabled),
                "textOnlyModel": bool(self.model_info.get("textOnly")),
                "seededPromptReplay": True,
                "kvFork": bool(self.profile.sequence_fork),
                "sequenceFork": bool(self.profile.sequence_fork),
                "batchCandidates": True,
                "logitsTopK": True,
                "continuationBranches": True,
                "serverTiming": True,
                "baseCompletion": self._base_completion_mode,
            },
            "promptMode": prompt_mode,
            "runtimeConfig": {
                "profileId": self.profile.id,
                "promptMode": prompt_mode,
                "maxTokens": self.configured_max_tokens,
                "temperature": self.configured_temperature,
                "topP": self.configured_top_p,
            },
        }

    def prompt_cache_status(self) -> dict[str, Any]:
        return self._prompt_cache.to_payload()

    def prefix_cache_status(self) -> dict[str, Any]:
        return {
            **self._prefix_cache.stats(),
            **self._last_prefix_cache_status,
            "boundaryTokens": self._prefix_cache_boundary_tokens,
        }

    def canonical_request_metadata(
        self,
        request_metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        metadata = dict(request_metadata or {})
        # Deployment aliases are accepted only when the process loads a model.
        # Once loaded, the engine profile owns scheduler, cache and trace keys.
        metadata.pop("profile", None)
        metadata.pop("profile_id", None)
        metadata["profileId"] = self.profile.id
        return metadata

    def predict(
        self,
        *,
        current_input: str,
        recent_context: str,
        max_candidates: int,
        max_tokens: int,
        temperature: float,
        top_p: float,
        request_type: str = PREDICTION_REQUEST_GENERIC,
        rime_candidates: tuple[str, ...] = (),
        stream_first_candidate: bool = False,
        request_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        resolved_request_type = normalize_prediction_request_type(request_type)
        rime_candidate_tuple = normalized_rime_candidate_texts(rime_candidates)
        metadata = self.canonical_request_metadata(request_metadata)
        profile_id = self.profile.id
        token = model_request_token_from_metadata(metadata, profile_id=profile_id)
        metadata["requestId"] = token.request_id
        metadata["profileId"] = profile_id
        self._scheduler.begin(token)

        def finalize(payload: dict[str, Any]) -> dict[str, Any]:
            if self._scheduler.is_cancelled(token.request_id):
                payload["candidates"] = []
                payload["candidateScores"] = []
                payload["cancelled"] = True
                payload["cancelReason"] = self._scheduler.cancel_reason(token.request_id) or "superseded_by_newer_generation"
                timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
                payload["timing"] = {**timing, "cancelled": True, "cancelReason": payload["cancelReason"]}
            payload["requestMeta"] = dict(metadata)
            payload["prefixCache"] = self.prefix_cache_status()
            trace = trace_from_prediction_payload(
                payload,
                request_id=token.request_id,
                request_type=resolved_request_type,
                profile_id=profile_id,
                model_id=self.model_id,
                prompt_tokens=_estimate_prompt_tokens(
                    current_input=current_input,
                    recent_context=recent_context,
                    rime_candidates=rime_candidate_tuple,
                ),
                output_tokens=_estimate_output_tokens(payload.get("rawText"), payload.get("candidates")),
            )
            payload["latencyTrace"] = trace.to_payload()
            try:
                append_latency_trace(trace)
            except OSError:
                pass
            self._scheduler.finish(token)
            return payload

        if self._base_completion_mode:
            display_candidate_limit = max(1, min(3, int(max_candidates)))
            prompt = _build_base_completion_prompt(
                current_input=current_input,
                recent_context=recent_context,
            )
            try:
                prompt_tokens = self.tokenizer.encode(prompt) if prompt else []
            except Exception:
                prompt_tokens = []
            if not prompt_tokens:
                return finalize({
                    "ok": True,
                    "model": self.model_id,
                    "rawText": "",
                    "candidates": [],
                    "candidateScores": [],
                    "candidateMode": "base-completion-empty-prompt",
                    "requestType": resolved_request_type,
                    "totalMs": int((time.perf_counter() - started) * 1000),
                    "promptCache": self.prompt_cache_status(),
                    "timing": {
                        "candidateMode": "base-completion-empty-prompt",
                        "logitsMs": 0,
                        "fallbackJson": False,
                        "requestType": resolved_request_type,
                        "skippedReason": "empty_prompt",
                    },
                    "requestMeta": dict(metadata),
                })

            # This checkpoint is a bare next-token completion model. Top-k
            # logits are branch seeds, not complete user-facing candidates.
            # Decode each seed into its own short continuation so the three
            # rows are genuine alternatives rather than three isolated tokens.
            probe_candidate_limit = max(16, min(32, display_candidate_limit * 8))
            logits_payload = self.prefill_base_completion_logits(
                prompt_tokens=prompt_tokens,
                max_candidates=probe_candidate_limit,
            )
            if not logits_payload.get("candidateScores"):
                logits_payload = self.predict_next_token_logits(
                    current_input=current_input,
                    recent_context=recent_context,
                    max_candidates=probe_candidate_limit,
                    request_type=resolved_request_type,
                    rime_candidates=rime_candidate_tuple,
                    request_metadata=metadata,
                )
            branch_payload = self.predict_base_completion_branches(
                current_input=current_input,
                recent_context=recent_context,
                prompt=prompt,
                max_candidates=display_candidate_limit,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                started=started,
                request_type=resolved_request_type,
                logits_candidates=logits_payload,
                request_metadata=metadata,
            )
            if branch_payload is not None:
                return finalize(branch_payload)

            raw_text = "".join(
                self.stream_text(
                    current_input=current_input,
                    recent_context=recent_context,
                    max_candidates=display_candidate_limit,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    request_type=resolved_request_type,
                    rime_candidates=rime_candidate_tuple,
                    stream_first_candidate=stream_first_candidate,
                    request_metadata=metadata,
                )
            )
            total_ms = int((time.perf_counter() - started) * 1000)
            candidates = self.parse_candidates_from_raw(
                raw_text,
                current_input=current_input,
                recent_context=recent_context,
                max_candidates=display_candidate_limit,
            )
            return finalize({
                "ok": True,
                "model": self.model_id,
                "rawText": raw_text,
                "candidates": candidates,
                "candidateMode": "base-completion",
                "requestType": resolved_request_type,
                "totalMs": total_ms,
                "promptCache": self.prompt_cache_status(),
                "timing": {
                    "candidateMode": "base-completion",
                    "logitsMs": int(logits_payload.get("elapsedMs") or 0),
                    "fallbackJson": False,
                    "fallbackReason": "base_completion_branches_empty",
                    "requestType": resolved_request_type,
                },
                "requestMeta": dict(metadata),
            })

        display_candidate_limit = max(1, int(max_candidates))
        if _should_use_realtime_post_commit_fast_path(
            resolved_request_type,
            current_input=current_input,
            stream_first_candidate=stream_first_candidate,
        ):
            return finalize(
                self.predict_realtime_post_commit_fast_path(
                    current_input=current_input,
                    recent_context=recent_context,
                    rime_candidates=rime_candidate_tuple,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    started=started,
                    request_type=resolved_request_type,
                    request_metadata=metadata,
                )
            )

        logits_probe_candidate_limit = display_candidate_limit
        if resolved_request_type == PREDICTION_REQUEST_NO_INPUT and not stream_first_candidate:
            logits_probe_candidate_limit = max(3, display_candidate_limit)

        logits_candidates = self.predict_next_token_logits(
            current_input=current_input,
            recent_context=recent_context,
            max_candidates=logits_probe_candidate_limit,
            request_type=resolved_request_type,
            rime_candidates=rime_candidate_tuple,
            request_metadata=metadata,
        )
        if _logits_candidates_are_ime_quality(logits_candidates["candidates"], max_candidates=max_candidates):
            visible_logits_candidates = logits_candidates["candidates"][:display_candidate_limit]
            visible_logits_scores = [
                dict(item)
                for item in (logits_candidates.get("candidateScores") or [])[:display_candidate_limit]
                if isinstance(item, dict)
            ]
            total_ms = int((time.perf_counter() - started) * 1000)
            return finalize({
                "ok": True,
                "model": self.model_id,
                "rawText": " ".join(visible_logits_candidates),
                "candidates": visible_logits_candidates,
                "candidateScores": visible_logits_scores,
                "candidateMode": "next-token-logits",
                "requestType": resolved_request_type,
                "totalMs": total_ms,
                "promptCache": self.prompt_cache_status(),
                "timing": {
                    "candidateMode": "next-token-logits",
                    "logitsMs": logits_candidates["elapsedMs"],
                    "logitsProbeCandidateLimit": logits_probe_candidate_limit,
                    "displayCandidateLimit": display_candidate_limit,
                    "fallbackJson": False,
                    "requestType": resolved_request_type,
                },
                "requestMeta": dict(metadata),
            })

        if resolved_request_type == PREDICTION_REQUEST_NO_INPUT and not stream_first_candidate:
            seeded_replay_payload = self.predict_no_input_seeded_prompt_replay(
                current_input=current_input,
                recent_context=recent_context,
                max_candidates=max_candidates,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                started=started,
                logits_candidates=logits_candidates,
                request_metadata=metadata,
            )
            if seeded_replay_payload is not None and seeded_replay_payload.get("candidates"):
                requested_candidates = max(1, int(max_candidates))
                seeded_candidate_count = len(seeded_replay_payload.get("candidates") or [])
                if seeded_candidate_count >= requested_candidates:
                    return finalize(seeded_replay_payload)
                branch_payload = self.predict_no_input_continuation_branches(
                    current_input=current_input,
                    recent_context=recent_context,
                    max_candidates=max(1, requested_candidates - seeded_candidate_count),
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    started=started,
                    logits_elapsed_ms=logits_candidates.get("elapsedMs", 0),
                    logits_quality_reason="seeded_replay_underfilled",
                    request_metadata=metadata,
                )
                return finalize(_merge_seeded_replay_with_branch_payload(
                    seeded_replay_payload,
                    branch_payload,
                    max_candidates=requested_candidates,
                    started=started,
                ))
            branch_payload = self.predict_no_input_continuation_branches(
                current_input=current_input,
                recent_context=recent_context,
                max_candidates=max_candidates,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                started=started,
                logits_elapsed_ms=logits_candidates.get("elapsedMs", 0),
                logits_quality_reason=logits_candidates.get("qualityReason") or "logits_candidates_not_phrase_quality",
                request_metadata=metadata,
            )
            return finalize(branch_payload)

        raw_text = "".join(
            self.stream_text(
                current_input=current_input,
                recent_context=recent_context,
                max_candidates=max_candidates,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                request_type=resolved_request_type,
                rime_candidates=rime_candidate_tuple,
                stream_first_candidate=stream_first_candidate,
                request_metadata=metadata,
            )
        )
        total_ms = int((time.perf_counter() - started) * 1000)
        candidates = parse_ime_prediction_candidates(
            raw_text,
            max_candidates=max_candidates,
            current_input=current_input,
            recent_context=recent_context,
            request_type=resolved_request_type,
            rime_candidates=rime_candidate_tuple,
        )
        if not candidates and not current_input.strip() and resolved_request_type in {
            PREDICTION_REQUEST_IME_HOT,
            PREDICTION_REQUEST_IME_POST_COMMIT,
            PREDICTION_REQUEST_ACTIVE_RAG,
        }:
            branch_payload = self.predict_no_input_continuation_branches(
                current_input=current_input,
                recent_context=recent_context,
                max_candidates=max_candidates,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                started=started,
                logits_elapsed_ms=logits_candidates.get("elapsedMs", 0),
                logits_quality_reason="json_generation_empty_for_empty_input",
                request_metadata=metadata,
            )
            branch_payload["requestType"] = resolved_request_type
            timing = branch_payload.get("timing")
            if isinstance(timing, dict):
                timing["requestType"] = resolved_request_type
            if rime_candidate_tuple:
                _backfill_payload_candidates_with_rime(
                    branch_payload,
                    rime_candidates=rime_candidate_tuple,
                    max_candidates=max_candidates,
                    empty_mode="rime-candidate-fallback",
                )
            return finalize(branch_payload)
        json_payload = {
            "ok": True,
            "model": self.model_id,
            "rawText": raw_text,
            "candidates": candidates,
            "candidateMode": "json-generation",
            "requestType": resolved_request_type,
            "totalMs": total_ms,
            "promptCache": self.prompt_cache_status(),
            "timing": {
                "candidateMode": "json-generation",
                "logitsMs": logits_candidates.get("elapsedMs", 0),
                "fallbackJson": True,
                "fallbackReason": logits_candidates.get("qualityReason") or "logits_candidates_not_phrase_quality",
                "requestType": resolved_request_type,
            },
            "requestMeta": dict(metadata),
        }
        if rime_candidate_tuple and resolved_request_type in {
            PREDICTION_REQUEST_IME_HOT,
            PREDICTION_REQUEST_IME_POST_COMMIT,
            PREDICTION_REQUEST_ACTIVE_RAG,
        }:
            _backfill_payload_candidates_with_rime(
                json_payload,
                rime_candidates=rime_candidate_tuple,
                max_candidates=max_candidates,
                empty_mode="rime-candidate-fallback",
            )
        return finalize(json_payload)

    def predict_realtime_post_commit_fast_path(
        self,
        *,
        current_input: str,
        recent_context: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        started: float,
        request_type: str,
        rime_candidates: tuple[str, ...] = (),
        request_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = dict(request_metadata or {})
        token_budget = _realtime_post_commit_token_budget(max_tokens)
        raw_text = "".join(
            self.stream_text(
                current_input=current_input,
                recent_context=recent_context,
                max_candidates=1,
                max_tokens=token_budget,
                temperature=max(0.05, min(float(temperature), 0.16)),
                top_p=top_p,
                request_type=request_type,
                rime_candidates=(),
                stream_first_candidate=False,
                request_metadata=metadata,
            )
        )
        candidate = _realtime_post_commit_candidate(
            raw_text,
            current_input=current_input,
            recent_context=recent_context,
        )
        candidates = [candidate] if candidate else []
        if not candidates and rime_candidates:
            candidates = list(rime_candidates[:1])
        total_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "model": self.model_id,
            "rawText": raw_text,
            "candidates": candidates,
            "candidateScores": [
                {
                    "text": candidate,
                    "source": "realtime-post-commit",
                    "mode": "realtime-post-commit-fast-path",
                    "rank": 1,
                }
                for candidate in candidates
            ],
            "candidateMode": "realtime-post-commit",
            "requestType": request_type,
            "totalMs": total_ms,
            "promptCache": self.prompt_cache_status(),
            "timing": {
                "candidateMode": "realtime-post-commit",
                "fallbackJson": False,
                "logitsMs": 0,
                "requestType": request_type,
                "fastPath": True,
                "branchCount": 0,
                "maxTokens": token_budget,
            },
            "requestMeta": dict(metadata),
        }

    def predict_no_input_continuation_branches(
        self,
        *,
        current_input: str,
        recent_context: str,
        max_candidates: int,
        max_tokens: int,
        temperature: float,
        top_p: float,
        started: float,
        logits_elapsed_ms: int,
        logits_quality_reason: str,
        request_metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
        max_items = max(1, int(max_candidates))
        metadata = dict(request_metadata or {})
        cancel_request_id = str(metadata.get("requestId") or "")
        branch_specs = _continuation_branch_specs(temperature=temperature, max_tokens=max_tokens)
        raw_texts: list[str] = []
        candidates: list[str] = []
        seen: set[str] = set()
        branch_timings: list[dict[str, Any]] = []
        list_started = time.perf_counter()
        list_max_tokens = max(16, min(64, max(int(max_tokens), max_items * 8)))
        # The space-list prompt is ChatML; a base completion model skips it
        # and relies on the base-mode-aware stream_text branches below.
        list_raw_text = (
            ""
            if self._base_completion_mode
            else "".join(
                self._stream_text_with_generate_step(
                    prompt=_build_no_input_space_list_prompt(
                        recent_context=recent_context,
                        max_candidates=max_items,
                    ),
                    max_tokens=list_max_tokens,
                    temperature=max(0.05, min(float(temperature), 0.18)),
                    top_p=top_p,
                    cancel_request_id=cancel_request_id,
                )
            )
        )
        raw_texts.append(list_raw_text)
        list_candidates = _space_list_continuation_candidates(
            list_raw_text,
            current_input=current_input,
            recent_context=recent_context,
            max_candidates=max_items,
        )
        branch_timings.append(
            {
                "label": "space-list",
                "temperature": max(0.05, min(float(temperature), 0.18)),
                "maxTokens": list_max_tokens,
                "elapsedMs": int((time.perf_counter() - list_started) * 1000),
                "candidates": list_candidates,
            }
        )
        for candidate in list_candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
            if len(candidates) >= max_items:
                break

        for branch in branch_specs:
            if len(candidates) >= max_items:
                break
            branch_started = time.perf_counter()
            raw_text = "".join(
                self.stream_text(
                    current_input=current_input,
                    recent_context=recent_context,
                    max_candidates=max_candidates,
                    max_tokens=branch.max_tokens,
                    temperature=branch.temperature,
                    top_p=top_p,
                    request_type=PREDICTION_REQUEST_NO_INPUT,
                    rime_candidates=(),
                    stream_first_candidate=True,
                    request_metadata=request_metadata,
                )
            )
            raw_texts.append(raw_text)
            branch_candidates = _branch_continuation_candidates(
                raw_text,
                current_input=current_input,
                recent_context=recent_context,
                max_candidate_chars=branch.max_candidate_chars,
                max_candidates=max_items - len(candidates),
            )
            branch_timings.append(
                {
                    "label": branch.label,
                    "temperature": branch.temperature,
                    "maxTokens": branch.max_tokens,
                    "elapsedMs": int((time.perf_counter() - branch_started) * 1000),
                    "candidates": branch_candidates,
                    "candidate": branch_candidates[0] if branch_candidates else "",
                }
            )
            for candidate in branch_candidates:
                if candidate and not _looks_like_meta_completion_candidate(candidate) and candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)
                if len(candidates) >= max_items:
                    break
        if max_items >= 5 and len(candidates) == 1:
            expanded_candidates = _expand_continuation_candidates_from_model_output(
                candidates,
                current_input=current_input,
                recent_context=recent_context,
                max_candidates=max_items,
            )
            for candidate in expanded_candidates:
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)
                if len(candidates) >= max_items:
                    break
            if expanded_candidates:
                branch_timings.append(
                    {
                        "label": "model-output-splits",
                        "temperature": temperature,
                        "maxTokens": 0,
                        "elapsedMs": 0,
                        "candidates": expanded_candidates,
                    }
                )
        candidate_scores = _continuation_branch_candidate_scores(
            candidates[:max_items],
            branch_timings=branch_timings,
        )
        total_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "model": self.model_id,
            "rawText": "\n".join(raw_texts),
            "candidates": candidates[:max_items],
            "candidateScores": candidate_scores,
            "candidateMode": "continuation-branches",
            "requestType": PREDICTION_REQUEST_NO_INPUT,
            "totalMs": total_ms,
            "promptCache": self.prompt_cache_status(),
            "timing": {
                "candidateMode": "continuation-branches",
                "logitsMs": int(logits_elapsed_ms or 0),
                "fallbackJson": False,
                "fallbackReason": logits_quality_reason,
                "requestType": PREDICTION_REQUEST_NO_INPUT,
                "branches": branch_timings,
            },
            "requestMeta": dict(request_metadata or {}),
        }

    def predict_base_completion_branches(
        self,
        *,
        current_input: str,
        recent_context: str,
        prompt: str,
        max_candidates: int,
        max_tokens: int,
        temperature: float,
        top_p: float,
        started: float,
        request_type: str,
        logits_candidates: dict[str, Any],
        request_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Expand top-logit seeds into distinct short base-model completions."""

        display_limit = max(1, min(3, int(max_candidates)))
        seeds = _seed_replay_specs_from_logits(
            logits_candidates.get("candidateScores"),
            # Filtering malformed, echoed, or one-character branches can
            # consume more than half of MiniMind's top logits. Keep a wider
            # seed reserve so a request for three rows is not routinely
            # returned as only one or two candidates.
            max_seeds=max(6, display_limit * 4),
            allow_single_cjk=True,
        )
        if not seeds:
            return None
        # Four tokens regularly cut MiniMind in the middle of a phrase (for
        # example, `短候`). Six keeps the branch compact while giving the model
        # enough room to finish the thought in the same batched decode.
        per_seed_max_tokens = max(4, min(6, int(max_tokens)))
        branch_temperature = max(0.0, min(float(temperature), 0.10))
        metadata = dict(request_metadata or {})
        cancel_request_id = str(metadata.get("requestId") or "")
        candidates: list[str] = []
        raw_texts: list[str] = []
        branch_timings: list[dict[str, Any]] = []
        seen: set[str] = set()
        processed_seed_indexes: set[int] = set()
        failed_seed_indexes: set[int] = set()
        batch_elapsed_ms = 0
        batch_calls = 0
        batch_error = ""
        batch_stats: dict[str, Any] = {}
        shared_prompt_cache = logits_candidates.get("promptCache")

        def record_branch(
            *,
            seed_index: int,
            raw_text: str,
            decode_mode: str,
            elapsed_ms: int = 0,
        ) -> bool:
            seed = seeds[seed_index]
            seed_text = compact_whitespace(str(seed.get("text") or ""))
            if not seed_text:
                return False
            candidate = _seeded_replay_candidate(
                seed_text=seed_text,
                raw_text=raw_text,
                current_input=current_input,
                recent_context=recent_context,
                max_candidate_chars=max(8, min(18, per_seed_max_tokens * 2 + len(seed_text))),
            )
            branch_candidates = [candidate] if candidate else []
            raw_texts.append(f"{seed_text}{raw_text}")
            branch_timings.append(
                {
                    "label": f"seed:{seed_text}",
                    "branchRank": seed_index + 1,
                    "branchCount": 0,
                    "seedPoolCount": len(seeds),
                    "seedText": seed_text,
                    "seedTokenId": seed.get("tokenId"),
                    "logprob": seed.get("logprob"),
                    "probability": seed.get("probability"),
                    "maxTokens": per_seed_max_tokens,
                    "elapsedMs": max(0, int(elapsed_ms)),
                    "decodeMode": decode_mode,
                    "candidates": branch_candidates,
                }
            )
            if candidate and candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
            return bool(candidate)

        # The common path computes the context once, retains its KV cache, and
        # decodes the seed tokens together. MiniMind exposes no native
        # multi-candidate API; this is a decoding-layer fork over one prefill.
        # Plan four branches before decoding instead of blindly expanding the
        # first six logits. A seed that begins a domain term in the current
        # context (for example `候` -> `候选`) outranks a high-probability
        # function word such as `就`. Unselected seeds remain available to the
        # sequential underfill fallback below.
        pending_indexes = _initial_base_completion_seed_indexes(
            seeds,
            recent_context=recent_context,
            display_limit=display_limit,
        )
        planned_seed_indexes = list(pending_indexes)
        while pending_indexes and len(candidates) < display_limit:
            if cancel_request_id and self._scheduler.is_cancelled(cancel_request_id):
                break
            batch_result = self._batch_base_completion_seed_continuations(
                prompt=prompt,
                seeds=[seeds[index] for index in pending_indexes],
                prompt_cache=shared_prompt_cache,
                max_tokens=per_seed_max_tokens,
                temperature=branch_temperature,
                top_p=top_p,
            )
            if batch_result is None:
                batch_error = "batch_generate_unavailable"
                break
            batch_calls += 1
            batch_elapsed_ms += int(batch_result.get("elapsedMs") or 0)
            if isinstance(batch_result.get("stats"), dict):
                batch_stats = dict(batch_result["stats"])
            batch_error = str(batch_result.get("error") or "")
            texts = batch_result.get("texts") if isinstance(batch_result.get("texts"), list) else []
            for offset, seed_index in enumerate(pending_indexes):
                processed_seed_indexes.add(seed_index)
                candidate_ok = record_branch(
                    seed_index=seed_index,
                    raw_text=str(texts[offset]) if offset < len(texts) else "",
                    decode_mode=str(batch_result.get("decodeMode") or "batch"),
                )
                if not candidate_ok:
                    failed_seed_indexes.add(seed_index)
            if len(candidates) >= display_limit:
                break
            pending_indexes = [
                index
                for index in range(len(seeds))
                if index not in processed_seed_indexes
            ][: max(1, display_limit - len(candidates))]

        # Older mlx-lm builds and unsupported cache types retain the proven
        # sequential replay path. It is also a last-resort backfill if a batch
        # produces an unusable branch after filtering.
        for seed_index, seed in enumerate(seeds):
            if len(candidates) >= display_limit:
                break
            if seed_index in processed_seed_indexes and seed_index not in failed_seed_indexes:
                continue
            if cancel_request_id and self._scheduler.is_cancelled(cancel_request_id):
                break
            seed_text = compact_whitespace(str(seed.get("text") or ""))
            if not seed_text:
                continue
            branch_started = time.perf_counter()
            raw_text = "".join(
                self._stream_text_with_generate_step(
                    prompt=f"{prompt}{seed_text}",
                    max_tokens=per_seed_max_tokens,
                    temperature=branch_temperature,
                    top_p=top_p,
                    cancel_request_id=cancel_request_id,
                )
            )
            record_branch(
                seed_index=seed_index,
                raw_text=raw_text,
                decode_mode="sequential-fallback",
                elapsed_ms=int((time.perf_counter() - branch_started) * 1000),
            )

        if not candidates:
            return None
        executed_branch_count = len(branch_timings)
        for item in branch_timings:
            item["branchCount"] = executed_branch_count
        ranked_candidates, rerank_meta = _rank_base_completion_candidates(
            candidates,
            recent_context=recent_context,
        )
        displayed = ranked_candidates[:display_limit]
        return {
            "ok": True,
            "model": self.model_id,
            "rawText": "\n".join(raw_texts),
            "candidates": displayed,
            "candidateScores": _seeded_prompt_replay_candidate_scores(
                displayed,
                branch_timings=branch_timings,
                mode="base-completion-branches",
            ),
            "candidateMode": "base-completion-branches",
            "requestType": request_type,
            "totalMs": int((time.perf_counter() - started) * 1000),
            "promptCache": self.prompt_cache_status(),
            "timing": {
                "candidateMode": "base-completion-branches",
                "logitsMs": int(logits_candidates.get("elapsedMs") or 0),
                "fallbackJson": False,
                "requestType": request_type,
                "seedReplayReason": "top_logits_seed_continuation",
                "branchCount": len(branch_timings),
                "displayCandidateLimit": display_limit,
                "underfilled": len(displayed) < display_limit,
                **rerank_meta,
                "decodeMode": (
                    "shared-prefill-batch"
                    if bool(logits_candidates.get("sharedPrefill")) and batch_calls
                    else "batch-full-prompt"
                    if batch_calls
                    else "sequential-fallback"
                ),
                "sharedPrefill": bool(logits_candidates.get("sharedPrefill")),
                "prefillMs": int(logits_candidates.get("elapsedMs") or 0),
                "batchCalls": batch_calls,
                "batchMs": batch_elapsed_ms,
                "batchError": batch_error,
                "batchStats": batch_stats,
                "plannedSeedIndexes": planned_seed_indexes,
                "branches": branch_timings,
            },
            "requestMeta": metadata,
        }

    def prefill_base_completion_logits(
        self,
        *,
        prompt_tokens: list[int],
        max_candidates: int,
    ) -> dict[str, Any]:
        """Prefill once and retain the KV cache used by seeded batch decode."""

        started = time.perf_counter()
        try:
            from mlx_lm.models.cache import make_prompt_cache  # type: ignore
            import mlx.core as mx  # type: ignore

            cache = make_prompt_cache(self.model)
            model_output = self.model(mx.array(prompt_tokens)[None], cache=cache)
            logits = getattr(model_output, "logits", model_output)[:, -1, :]
            logprobs = (logits - mx.logsumexp(logits, keepdims=True)).squeeze(0)
            mx.eval(logprobs, [item.state for item in cache])
            candidate_scores = self._candidate_scores_from_logprobs(
                logprobs,
                max_candidates=max_candidates,
                scan_limit=max(64, int(max_candidates) * 24),
            )
            return {
                "candidates": [item["text"] for item in candidate_scores],
                "candidateScores": candidate_scores,
                "elapsedMs": int((time.perf_counter() - started) * 1000),
                "promptCache": cache,
                "promptTokens": len(prompt_tokens),
                "sharedPrefill": True,
            }
        except Exception as exc:  # pragma: no cover - depends on local MLX-LM internals
            return {
                "candidates": [],
                "candidateScores": [],
                "elapsedMs": int((time.perf_counter() - started) * 1000),
                "error": exc.__class__.__name__,
                "sharedPrefill": False,
            }

    def _batch_base_completion_seed_continuations(
        self,
        *,
        prompt: str,
        seeds: list[dict[str, Any]],
        prompt_cache: Any | None,
        max_tokens: int,
        temperature: float,
        top_p: float,
    ) -> dict[str, Any] | None:
        try:
            from mlx_lm.generate import batch_generate  # type: ignore
            from mlx_lm.sample_utils import make_sampler  # type: ignore
        except (ImportError, AttributeError):
            return None

        started = time.perf_counter()
        try:
            prompts: list[list[int]] = []
            for seed in seeds:
                seed_text = compact_whitespace(str(seed.get("text") or ""))
                token_id = seed.get("tokenId")
                if prompt_cache is not None and token_id is not None:
                    prompts.append([int(token_id)])
                else:
                    prompts.append([int(item) for item in self.tokenizer.encode(f"{prompt}{seed_text}")])
            if not prompts or any(not item for item in prompts):
                return None

            eos_token_ids = _tokenizer_eos_token_ids(self.tokenizer)

            def keep_early_continuation_visible(tokens: Any, logits: Any) -> Any:
                # A base checkpoint often ranks EOS immediately after a good
                # seed. Suppress EOS for two continuation steps so every row is
                # a phrase rather than the isolated top-logit token itself.
                shape = getattr(tokens, "shape", ())
                token_count = int(shape[-1]) if shape else len(tokens)
                if token_count <= 2:
                    for token_id in eos_token_ids:
                        logits[:, int(token_id)] = -1e9
                return logits

            kwargs: dict[str, Any] = {
                "max_tokens": max(1, min(64, int(max_tokens))),
                "sampler": make_sampler(
                    temp=max(0.0, float(temperature)),
                    top_p=max(0.0, float(top_p)),
                ),
            }
            if prompt_cache is not None:
                # batch_generate merges these read-only snapshots into one
                # batched cache and does not mutate the retained base cache.
                kwargs["prompt_caches"] = [prompt_cache] * len(prompts)
            if eos_token_ids:
                kwargs["logits_processors"] = [keep_early_continuation_visible]
            response = batch_generate(self.model, self.tokenizer, prompts, **kwargs)
            texts = getattr(response, "texts", None)
            if not isinstance(texts, list):
                return None
            stats = getattr(response, "stats", None)
            stats_payload = {
                key: getattr(stats, key)
                for key in (
                    "prompt_tokens",
                    "prompt_time",
                    "generation_tokens",
                    "generation_time",
                    "peak_memory",
                )
                if stats is not None and isinstance(getattr(stats, key, None), (int, float))
            }
            return {
                "texts": [str(item) for item in texts],
                "elapsedMs": int((time.perf_counter() - started) * 1000),
                "decodeMode": "shared-prefill-batch" if prompt_cache is not None else "batch-full-prompt",
                "stats": stats_payload,
            }
        except Exception as exc:  # pragma: no cover - optional mlx-lm batch path
            return {
                "texts": [],
                "elapsedMs": int((time.perf_counter() - started) * 1000),
                "decodeMode": "batch-error",
                "error": exc.__class__.__name__,
            }

    def predict_no_input_seeded_prompt_replay(
        self,
        *,
        current_input: str,
        recent_context: str,
        max_candidates: int,
        max_tokens: int,
        temperature: float,
        top_p: float,
        started: float,
        logits_candidates: dict[str, Any],
        request_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        # Seeded replay and sequence-fork prompts are ChatML; a base
        # completion model must stay on the raw-continuation contract.
        if self._base_completion_mode:
            return None
        display_limit = max(1, int(max_candidates))
        seeds = _seed_replay_specs_from_logits(
            logits_candidates.get("candidateScores"),
            max_seeds=3,
        )
        if not seeds:
            return None
        sequence_fork_payload = self._predict_no_input_seeded_sequence_fork(
            current_input=current_input,
            recent_context=recent_context,
            max_candidates=max_candidates,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            started=started,
            logits_candidates=logits_candidates,
            seeds=seeds,
            request_metadata=request_metadata,
        )
        if sequence_fork_payload is not None:
            return sequence_fork_payload
        kv_fork_available = False
        sequence_fork_available = False
        replay_fallback_reason = "cache_clone_unsupported"
        raw_texts: list[str] = []
        candidates: list[str] = []
        seen: set[str] = set()
        branch_timings: list[dict[str, Any]] = []
        per_seed_max_tokens = max(8, min(24, int(max_tokens)))
        replay_temperature = max(0.05, min(float(temperature), 0.18))
        branch_count = len(seeds)
        metadata = dict(request_metadata or {})
        cancel_request_id = str(metadata.get("requestId") or "")
        for branch_rank, seed in enumerate(seeds, start=1):
            if cancel_request_id and self._scheduler.is_cancelled(cancel_request_id):
                break
            seed_text = str(seed.get("text") or "")
            if not seed_text:
                continue
            branch_started = time.perf_counter()
            raw_text = "".join(
                self._stream_text_with_generate_step(
                    prompt=_build_seeded_replay_prompt(recent_context=recent_context, seed_text=seed_text),
                    max_tokens=per_seed_max_tokens,
                    temperature=replay_temperature,
                    top_p=top_p,
                    cancel_request_id=cancel_request_id,
                )
            )
            raw_texts.append(f"{seed_text}{raw_text}")
            candidate = _seeded_replay_candidate(
                seed_text=seed_text,
                raw_text=raw_text,
                current_input=current_input,
                recent_context=recent_context,
                max_candidate_chars=max(12, min(24, per_seed_max_tokens * 2)),
            )
            branch_candidates = [candidate] if candidate else []
            branch_timings.append(
                {
                    "label": f"seed:{seed_text}",
                    "branchRank": branch_rank,
                    "branchCount": branch_count,
                    "seedText": seed_text,
                    "seedTokenId": seed.get("tokenId"),
                    "logprob": seed.get("logprob"),
                    "probability": seed.get("probability"),
                    "maxTokens": per_seed_max_tokens,
                    "kvFork": kv_fork_available,
                    "sequenceFork": sequence_fork_available,
                    "fallbackReason": replay_fallback_reason,
                    "elapsedMs": int((time.perf_counter() - branch_started) * 1000),
                    "candidates": branch_candidates,
                }
            )
            if candidate and candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
        if not candidates:
            return None
        displayed_candidates = candidates[:display_limit]
        total_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "model": self.model_id,
            "rawText": "\n".join(raw_texts),
            "candidates": displayed_candidates,
            "candidateScores": _seeded_prompt_replay_candidate_scores(
                displayed_candidates,
                branch_timings=branch_timings,
            ),
            "candidateMode": "seeded-prompt-replay",
            "requestType": PREDICTION_REQUEST_NO_INPUT,
            "totalMs": total_ms,
            "promptCache": self.prompt_cache_status(),
            "timing": {
                "candidateMode": "seeded-prompt-replay",
                "logitsMs": int(logits_candidates.get("elapsedMs") or 0),
                "fallbackJson": False,
                "fallbackReason": replay_fallback_reason,
                "kvFork": kv_fork_available,
                "sequenceFork": sequence_fork_available,
                "seedReplayReason": "top_logits_seed_replay",
                "requestType": PREDICTION_REQUEST_NO_INPUT,
                "seedReplayBranchCount": len(branch_timings),
                "seedReplayDisplayedCount": len(displayed_candidates),
                "displayCandidateLimit": display_limit,
                "branches": branch_timings,
            },
            "requestMeta": dict(request_metadata or {}),
        }

    def _predict_no_input_seeded_sequence_fork(
        self,
        *,
        current_input: str,
        recent_context: str,
        max_candidates: int,
        max_tokens: int,
        temperature: float,
        top_p: float,
        started: float,
        logits_candidates: dict[str, Any],
        seeds: list[dict[str, Any]],
        request_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        metadata = dict(request_metadata or {})
        cancel_request_id = str(metadata.get("requestId") or "")
        display_limit = max(1, int(max_candidates))
        per_seed_max_tokens = max(8, min(24, int(max_tokens)))
        replay_temperature = max(0.05, min(float(temperature), 0.18))
        base_prompt = _build_seeded_sequence_fork_base_prompt(recent_context=recent_context)
        try:
            base_tokens = tuple(int(item) for item in self.tokenizer.encode(base_prompt))
        except Exception:
            return None
        if not base_tokens:
            return None

        branch_specs = tuple(
            BranchSpec(
                branch_id=f"seed-{rank}",
                seed_text=str(seed.get("text") or ""),
                temperature=replay_temperature,
                max_tokens=per_seed_max_tokens,
                max_candidate_chars=max(12, min(24, per_seed_max_tokens * 2)),
            )
            for rank, seed in enumerate(seeds, start=1)
            if str(seed.get("text") or "")
        )
        if not branch_specs:
            return None

        seed_by_text = {str(seed.get("text") or ""): seed for seed in seeds}
        try:
            from mlx_lm.generate import generate_step  # type: ignore
            from mlx_lm.models.cache import make_prompt_cache  # type: ignore
            from mlx_lm.sample_utils import make_sampler  # type: ignore
            import mlx.core as mx  # type: ignore
        except Exception:
            return None

        def prefill(prompt_tokens: tuple[int, ...]) -> object:
            kwargs: dict[str, Any] = {}
            if self._prompt_cache.max_kv_size > 0:
                kwargs["max_kv_size"] = self._prompt_cache.max_kv_size
            cache = make_prompt_cache(self.model, **kwargs)
            for _token, _logprobs in generate_step(
                mx.array(prompt_tokens),
                self.model,
                max_tokens=0,
                prompt_cache=cache,
            ):
                pass
            try:
                mx.eval([item.state for item in cache])
            except Exception:
                pass
            return cache

        def clone_cache(cache_obj: object) -> object:
            return deepcopy(cache_obj)

        def decode_branch(cache_obj: object, branch: BranchSpec) -> tuple[str, int]:
            seed_tokens = tuple(int(item) for item in self.tokenizer.encode(branch.seed_text))
            if not seed_tokens:
                return "", 0
            sampler = make_sampler(temp=max(0.0, float(branch.temperature)), top_p=max(0.0, float(top_p)))
            emitted = ""
            generated_tokens: list[int] = []
            for token, _logprobs in generate_step(
                mx.array(seed_tokens),
                self.model,
                max_tokens=max(1, min(64, int(branch.max_tokens))),
                prompt_cache=cache_obj,
                sampler=sampler,
            ):
                if cancel_request_id and self._scheduler.is_cancelled(cancel_request_id):
                    break
                token_id = _token_to_int(token)
                if _token_is_eos(self.tokenizer, token_id):
                    break
                generated_tokens.append(token_id)
                decoded = self.tokenizer.decode(generated_tokens)
                if isinstance(decoded, bytes):
                    decoded = decoded.decode("utf-8", errors="ignore")
                if not isinstance(decoded, str):
                    decoded = str(decoded)
                decoded, reached_stop = _visible_generation_text(decoded)
                if "\ufffd" in decoded:
                    continue
                if candidate_has_self_repetition(decoded):
                    break
                emitted = decoded
                if reached_stop:
                    break
            candidate = _seeded_replay_candidate(
                seed_text=branch.seed_text,
                raw_text=emitted,
                current_input=current_input,
                recent_context=recent_context,
                max_candidate_chars=branch.max_candidate_chars,
            )
            return candidate, len(generated_tokens)

        try:
            fork_results = run_sequence_fork(
                prompt_tokens=base_tokens,
                branches=branch_specs,
                prefill=prefill,
                clone_cache=clone_cache,
                decode_branch=decode_branch,
                is_cancelled=lambda: bool(cancel_request_id and self._scheduler.is_cancelled(cancel_request_id)),
            )
        except Exception:
            return None
        if not fork_results or not all(item.cache_fork_supported for item in fork_results):
            return None

        branch_count = len(branch_specs)
        raw_texts: list[str] = []
        candidates: list[str] = []
        seen: set[str] = set()
        branch_timings: list[dict[str, Any]] = []
        for rank, result in enumerate(fork_results, start=1):
            branch = branch_specs[rank - 1]
            seed = seed_by_text.get(branch.seed_text, {})
            branch_candidates = [result.candidate] if result.candidate else []
            if result.candidate:
                raw_texts.append(result.candidate)
            branch_timings.append(
                {
                    "label": f"seed:{branch.seed_text}",
                    "branchRank": rank,
                    "branchCount": branch_count,
                    "seedText": branch.seed_text,
                    "seedTokenId": seed.get("tokenId"),
                    "logprob": seed.get("logprob"),
                    "probability": seed.get("probability"),
                    "maxTokens": branch.max_tokens,
                    "tokensGenerated": result.tokens_generated,
                    "elapsedMs": int(result.elapsed_ms),
                    "kvFork": True,
                    "sequenceFork": True,
                    "cacheForkSupported": True,
                    "candidates": branch_candidates,
                }
            )
            if result.candidate and result.candidate not in seen:
                seen.add(result.candidate)
                candidates.append(result.candidate)
        if not candidates:
            return None
        displayed_candidates = candidates[:display_limit]
        total_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "model": self.model_id,
            "rawText": "\n".join(raw_texts),
            "candidates": displayed_candidates,
            "candidateScores": _seeded_prompt_replay_candidate_scores(
                displayed_candidates,
                branch_timings=branch_timings,
                mode="seeded-sequence-fork",
            ),
            "candidateMode": "seeded-sequence-fork",
            "requestType": PREDICTION_REQUEST_NO_INPUT,
            "totalMs": total_ms,
            "promptCache": self.prompt_cache_status(),
            "timing": {
                "candidateMode": "seeded-sequence-fork",
                "logitsMs": int(logits_candidates.get("elapsedMs") or 0),
                "fallbackJson": False,
                "fallbackReason": "",
                "kvFork": True,
                "sequenceFork": True,
                "seedReplayReason": "top_logits_seed_sequence_fork",
                "requestType": PREDICTION_REQUEST_NO_INPUT,
                "seedReplayBranchCount": len(branch_timings),
                "seedReplayDisplayedCount": len(displayed_candidates),
                "displayCandidateLimit": display_limit,
                "prefillTokenCount": len(base_tokens),
                "branches": branch_timings,
            },
            "requestMeta": dict(request_metadata or {}),
        }

    def predict_next_token_logits(
        self,
        *,
        current_input: str,
        recent_context: str,
        max_candidates: int,
        request_type: str = PREDICTION_REQUEST_GENERIC,
        rime_candidates: tuple[str, ...] = (),
        request_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = request_metadata
        started = time.perf_counter()
        try:
            from mlx_lm.generate import generate_step  # type: ignore
            import mlx.core as mx  # type: ignore

            prompt = self._build_logits_prompt(
                current_input=current_input,
                recent_context=recent_context,
                max_candidates=max_candidates,
                request_type=request_type,
                rime_candidates=rime_candidates,
            )
            tokens = self.tokenizer.encode(prompt)
            token, logprobs = next(generate_step(mx.array(tokens), self.model, max_tokens=1))
            candidate_scores = self._candidate_scores_from_logprobs(
                logprobs,
                max_candidates=max_candidates,
                scan_limit=max(64, max_candidates * 24),
            )
            return {
                "candidates": [item["text"] for item in candidate_scores],
                "candidateScores": candidate_scores,
                "sampledTokenId": _token_to_int(token),
                "elapsedMs": int((time.perf_counter() - started) * 1000),
            }
        except Exception as exc:  # pragma: no cover - depends on local MLX-LM internals
            return {
                "candidates": [],
                "candidateScores": [],
                "error": exc.__class__.__name__,
                "elapsedMs": int((time.perf_counter() - started) * 1000),
            }

    def _candidate_scores_from_logprobs(
        self,
        logprobs: Any,
        *,
        max_candidates: int,
        scan_limit: int,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for token_id in _top_logprob_indices(logprobs, limit=max(1, int(scan_limit))):
            text = _normalize_logits_candidate_text(self.tokenizer.decode([int(token_id)]))
            if not text or text in seen:
                continue
            if text in _LOW_VALUE_LOGITS_CANDIDATES:
                continue
            if len(text) > 8 or not _CJK_RE.search(text):
                continue
            seen.add(text)
            logprob = _logprob_at(logprobs, int(token_id))
            item: dict[str, Any] = {
                "text": text,
                "tokenId": int(token_id),
            }
            if logprob is not None:
                item["logprob"] = logprob
                item["probability"] = max(0.0, min(1.0, math.exp(max(-60.0, min(0.0, logprob)))))
            result.append(item)
            if len(result) >= max(1, min(64, int(max_candidates))):
                break
        return result

    def stream_text(
        self,
        *,
        current_input: str,
        recent_context: str,
        max_candidates: int,
        max_tokens: int,
        temperature: float,
        top_p: float,
        request_type: str = PREDICTION_REQUEST_GENERIC,
        rime_candidates: tuple[str, ...] = (),
        stream_first_candidate: bool = False,
        request_metadata: dict[str, Any] | None = None,
    ) -> Iterable[str]:
        metadata = self.canonical_request_metadata(request_metadata)
        cancel_request_id = str(metadata.get("requestId") or "")
        prompt = self._build_prompt(
            current_input=current_input,
            recent_context=recent_context,
            max_candidates=max_candidates,
            request_type=request_type,
            rime_candidates=rime_candidates,
            stream_first_candidate=stream_first_candidate,
        )
        if not compact_whitespace(prompt):
            return
        self._record_prefix_cache(prompt=prompt, request_metadata=metadata)
        if self._prompt_cache.ready_for_generation() and not stream_first_candidate and not self._base_completion_mode:
            try:
                for text in self._stream_text_with_prompt_cache(
                    current_input=current_input,
                    recent_context=recent_context,
                    max_candidates=max_candidates,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    request_type=request_type,
                    rime_candidates=rime_candidates,
                    stream_first_candidate=stream_first_candidate,
                    cancel_request_id=cancel_request_id,
                ):
                    yield text
                return
            except Exception as exc:  # pragma: no cover - depends on local MLX-LM internals
                self._prompt_cache.miss_count += 1
                self._prompt_cache.error = f"cached_generation:{exc.__class__.__name__}"

        if self._prompt_cache.enabled:
            self._prompt_cache.miss_count += 1
        for text in self._stream_text_with_generate_step(
            prompt=prompt,
            max_tokens=max(1, min(64, int(max_tokens))),
            temperature=float(temperature),
            top_p=float(top_p),
            cancel_request_id=cancel_request_id,
        ):
            yield text

    def _record_prefix_cache(self, *, prompt: str, request_metadata: dict[str, Any]) -> None:
        if not self._prefix_cache_enabled:
            self._last_prefix_cache_status = {
                "enabled": False,
                "cacheHit": False,
                "cacheHitTokens": 0,
                "cacheMissTokens": 0,
            }
            return
        try:
            token_ids = tuple(int(item) for item in self.tokenizer.encode(prompt))
        except Exception:
            self._last_prefix_cache_status = {
                "enabled": True,
                "cacheHit": False,
                "cacheHitTokens": 0,
                "cacheMissTokens": 0,
                "error": "tokenize_failed",
            }
            return
        profile_id = self.profile.id
        hit = self._prefix_cache.lookup_longest_prefix(
            profile_id=profile_id,
            prompt_format="IMEV1",
            token_ids=token_ids,
        )
        cache_hit_tokens = hit.token_count if hit else 0
        boundary = min(len(token_ids), max(1, self._prefix_cache_boundary_tokens))
        if boundary > 0 and (hit is None or hit.token_count < boundary):
            prefix_tokens = token_ids[:boundary]
            self._prefix_cache.put(
                PrefixCacheEntry(
                    cache_id=f"{profile_id}:IMEV1:{hash(prefix_tokens)}",
                    profile_id=profile_id,
                    prompt_format="IMEV1",
                    token_ids=prefix_tokens,
                    token_count=len(prefix_tokens),
                    cache_obj=None,
                    created_at_ms=int(time.time() * 1000),
                    last_used_at_ms=int(time.time() * 1000),
                    bytes_estimate=len(prefix_tokens) * 8,
                )
            )
        self._last_prefix_cache_status = {
            "enabled": True,
            "cacheHit": hit is not None,
            "cacheHitTokens": cache_hit_tokens,
            "cacheMissTokens": max(0, len(token_ids) - cache_hit_tokens),
            "tokenizedPrefixCache": True,
            "kvObjectCache": False,
            "fallbackReason": "kv_cache_clone_unsupported" if hit is not None else "",
            "stats": self._prefix_cache.stats(),
        }

    def _stream_text_with_prompt_cache(
        self,
        *,
        current_input: str,
        recent_context: str,
        max_candidates: int,
        max_tokens: int,
        temperature: float,
        top_p: float,
        request_type: str = PREDICTION_REQUEST_GENERIC,
        rime_candidates: tuple[str, ...] = (),
        stream_first_candidate: bool = False,
        cancel_request_id: str = "",
    ) -> Iterable[str]:
        from mlx_lm.generate import generate_step  # type: ignore
        from mlx_lm.models.cache import load_prompt_cache  # type: ignore
        from mlx_lm.sample_utils import make_sampler  # type: ignore
        import mlx.core as mx  # type: ignore

        cache = load_prompt_cache(str(self._prompt_cache.cache_file))
        dynamic_prompt = self._dynamic_prompt_suffix(
            current_input=current_input,
            recent_context=recent_context,
            max_candidates=max_candidates,
            request_type=request_type,
            rime_candidates=rime_candidates,
            stream_first_candidate=stream_first_candidate,
        )
        self._prompt_cache.used_for_generation = True
        self._prompt_cache.hit_count += 1
        self._prompt_cache.error = ""
        for text in self._stream_text_with_generate_step(
            prompt=dynamic_prompt,
            max_tokens=max(1, min(64, int(max_tokens))),
            temperature=float(temperature),
            top_p=float(top_p),
            prompt_cache=cache,
            cancel_request_id=cancel_request_id,
        ):
            yield text

    def _stream_text_with_generate_step(
        self,
        *,
        prompt: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        prompt_cache: Any | None = None,
        cancel_request_id: str = "",
    ) -> Iterable[str]:
        from mlx_lm.generate import generate_step  # type: ignore
        from mlx_lm.sample_utils import make_sampler  # type: ignore
        import mlx.core as mx  # type: ignore

        tokens = self.tokenizer.encode(prompt)
        if not tokens:
            return
        sampler = make_sampler(temp=max(0.0, float(temperature)), top_p=max(0.0, float(top_p)))
        emitted = ""
        generated_tokens: list[int] = []
        for token, logprobs in generate_step(
            mx.array(tokens),
            self.model,
            max_tokens=max(1, min(64, int(max_tokens))),
            prompt_cache=prompt_cache,
            sampler=sampler,
        ):
            if cancel_request_id and self._scheduler.is_cancelled(cancel_request_id):
                break
            token_id = _token_to_int(token)
            first_token_visible = _token_decodes_visible_text(self.tokenizer, token_id)
            if _token_is_eos(self.tokenizer, token_id) or (not generated_tokens and not first_token_visible):
                if self._base_completion_mode and len(emitted.strip()) < 2:
                    forced_token_id = _best_non_eos_token_id(logprobs, self.tokenizer)
                    if forced_token_id is None:
                        break
                    token_id = forced_token_id
                else:
                    break
            generated_tokens.append(token_id)
            decoded = self.tokenizer.decode(generated_tokens)
            if isinstance(decoded, bytes):
                decoded = decoded.decode("utf-8", errors="ignore")
            if not isinstance(decoded, str):
                decoded = str(decoded)
            decoded, reached_stop = _visible_generation_text(decoded)
            if "\ufffd" in decoded:
                continue
            if candidate_has_self_repetition(decoded):
                break
            delta = decoded[len(emitted) :] if decoded.startswith(emitted) else decoded
            emitted = decoded
            if delta:
                yield delta
            if reached_stop:
                break

    def _stable_prompt_prefix(self, *, stream_first_candidate: bool = False) -> str:
        if self._base_completion_mode:
            return ""
        prompt = STREAM_FIRST_SYSTEM_PROMPT if stream_first_candidate else SYSTEM_PROMPT
        return f"<|im_start|>system\n{prompt}<|im_end|>\n<|im_start|>user\n"

    def _dynamic_prompt_suffix(
        self,
        *,
        current_input: str,
        recent_context: str,
        max_candidates: int,
        request_type: str = PREDICTION_REQUEST_GENERIC,
        rime_candidates: tuple[str, ...] = (),
        stream_first_candidate: bool = False,
    ) -> str:
        if self._base_completion_mode:
            return _build_base_completion_prompt(current_input=current_input, recent_context=recent_context)
        return (
            f"{_build_mlx_dynamic_prompt(current_input=current_input, recent_context=recent_context, max_candidates=max_candidates, request_type=request_type, rime_candidates=rime_candidates, stream_first_candidate=stream_first_candidate)}"
            "<|im_end|>\n"
            f"{QWEN_NON_THINKING_ASSISTANT_PREFIX}"
        )

    def _build_prompt(
        self,
        *,
        current_input: str,
        recent_context: str,
        max_candidates: int,
        request_type: str = PREDICTION_REQUEST_GENERIC,
        rime_candidates: tuple[str, ...] = (),
        stream_first_candidate: bool = False,
    ) -> str:
        if self._base_completion_mode:
            return _build_base_completion_prompt(current_input=current_input, recent_context=recent_context)
        return (
            f"{self._stable_prompt_prefix(stream_first_candidate=stream_first_candidate)}"
            f"{self._dynamic_prompt_suffix(current_input=current_input, recent_context=recent_context, max_candidates=max_candidates, request_type=request_type, rime_candidates=rime_candidates, stream_first_candidate=stream_first_candidate)}"
        )

    def _build_logits_prompt(
        self,
        *,
        current_input: str,
        recent_context: str,
        max_candidates: int,
        request_type: str = PREDICTION_REQUEST_GENERIC,
        rime_candidates: tuple[str, ...] = (),
    ) -> str:
        if self._base_completion_mode:
            return _build_base_completion_prompt(current_input=current_input, recent_context=recent_context)
        resolved_request_type = normalize_prediction_request_type(request_type)
        rime_line = _rime_candidates_prompt_line(rime_candidates)
        return (
            f"<|im_start|>system\n{LOGITS_SYSTEM_PROMPT}<|im_end|>\n"
            "<|im_start|>user\n"
            f"请求类型: {resolved_request_type}\n"
            f"上下文: {recent_context}\n"
            f"当前输入: {current_input}\n"
            f"{rime_line}"
            f"模式说明: {_request_type_prompt_instruction(resolved_request_type)}\n"
            f"给出 {max_candidates} 个候选中最可能的第一个候选, 直接从候选文本开始。"
            "<|im_end|>\n"
            f"{QWEN_NON_THINKING_ASSISTANT_PREFIX}"
        )

    def _prepare_prompt_cache(self) -> None:
        started = time.perf_counter()
        try:
            from mlx_lm.generate import generate_step  # type: ignore
            from mlx_lm.models.cache import make_prompt_cache, save_prompt_cache  # type: ignore
            import mlx.core as mx  # type: ignore

            tokens = self.tokenizer.encode(self._prompt_cache.stable_prefix)
            self._prompt_cache.stable_prefix_tokens = len(tokens)
            kwargs: dict[str, Any] = {}
            if self._prompt_cache.max_kv_size > 0:
                kwargs["max_kv_size"] = self._prompt_cache.max_kv_size
            cache = make_prompt_cache(self.model, **kwargs)
            for _ in generate_step(mx.array(tokens), self.model, max_tokens=1, prompt_cache=cache):
                break
            handle = tempfile.NamedTemporaryFile(prefix="rag-ime-mlx-prefix-", suffix=".safetensors", delete=False)
            handle.close()
            save_prompt_cache(
                handle.name,
                cache,
                metadata={
                    "model": self.model_id,
                    "stablePrefixHash": self._prompt_cache.stable_prefix_hash(),
                },
            )
            self._prompt_cache.cache_file = handle.name
            self._prompt_cache.prepared = True
            self._prompt_cache.error = ""
        except Exception as exc:  # pragma: no cover - depends on local MLX-LM internals
            self._prompt_cache.prepared = False
            self._prompt_cache.error = exc.__class__.__name__
        finally:
            self._prompt_cache.prepare_ms = int((time.perf_counter() - started) * 1000)

    def parse_candidates_from_raw(
        self,
        raw_text: str,
        *,
        current_input: str,
        recent_context: str,
        max_candidates: int,
        request_type: str = PREDICTION_REQUEST_GENERIC,
        rime_candidates: tuple[str, ...] = (),
    ) -> list[str]:
        if self._base_completion_mode:
            return _parse_base_completion_candidates(
                raw_text,
                current_input=current_input,
                recent_context=recent_context,
                max_candidates=max_candidates,
            )
        return parse_ime_prediction_candidates(
            raw_text,
            max_candidates=max_candidates,
            current_input=current_input,
            recent_context=recent_context,
            request_type=request_type,
            rime_candidates=rime_candidates,
        )


def make_mlx_predictor_handler(engine: MlxLmEngine):
    class MlxPredictorHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            if self.path == "/health":
                self._send_json(engine.health())
                return
            if self.path == "/v1/models":
                self._send_json({"data": [{"id": engine.model_id, "owned_by": "local-mlx"}]})
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802 - stdlib API
            if self.path not in {"/predict", "/predict-stream"}:
                self.send_error(404)
                return
            try:
                payload = self._read_json()
            except json.JSONDecodeError:
                self.send_error(400, "invalid JSON")
                return
            request = _normalize_prediction_request(
                payload,
                default_model=engine.model_id,
                max_tokens_limit=int(getattr(engine, "configured_max_tokens", 64)),
            )
            request["request_metadata"] = engine.canonical_request_metadata(
                request.get("request_metadata")
            )
            if self.path == "/predict-stream":
                self._send_stream(engine, request)
                return
            started = time.perf_counter()
            try:
                result = engine.predict(**request)
            except Exception as exc:  # pragma: no cover - exercised by handler-level tests
                result = _predict_error_payload(
                    engine=engine,
                    request=request,
                    exc=exc,
                    started=started,
                )
            self._send_json(result)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            return payload if isinstance(payload, dict) else {}

        def _send_stream(self, engine: MlxLmEngine, request: dict[str, Any]) -> None:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            started = time.perf_counter()
            raw_text = ""
            parser = ImeCandidateStreamParser(
                max_candidates=int(request["max_candidates"]),
                current_input=str(request.get("current_input") or ""),
                recent_context=str(request.get("recent_context") or ""),
            )
            for text in engine.stream_text(**request):
                raw_text += text
                if not self._write_json_line({"delta": text, "elapsedMs": int((time.perf_counter() - started) * 1000)}):
                    return
                for candidate in parser.feed(text):
                    if not self._write_json_line(
                        {
                            "event": "candidate_delta",
                            "candidate": candidate,
                            "index": len(parser.candidates) - 1,
                            "elapsedMs": int((time.perf_counter() - started) * 1000),
                            "partial": True,
                        }
                    ):
                        return
                if parser.done():
                    break
            parse_candidates = getattr(engine, "parse_candidates_from_raw", None)
            if callable(parse_candidates):
                candidates = parse_candidates(
                    raw_text,
                    current_input=str(request.get("current_input") or ""),
                    recent_context=str(request.get("recent_context") or ""),
                    max_candidates=int(request["max_candidates"]),
                    request_type=normalize_prediction_request_type(request.get("request_type")),
                    rime_candidates=normalized_rime_candidate_texts(request.get("rime_candidates")),
                )
            else:
                candidates = parse_prediction_candidates(raw_text, max_candidates=int(request["max_candidates"]))
            self._write_json_line(
                {
                    "done": True,
                    "rawText": raw_text,
                    "candidates": candidates,
                    "requestType": normalize_prediction_request_type(request.get("request_type")),
                    "totalMs": int((time.perf_counter() - started) * 1000),
                    "promptCache": engine.prompt_cache_status(),
                    "latencyTrace": trace_from_prediction_payload(
                        {
                            "rawText": raw_text,
                            "candidates": candidates,
                            "totalMs": int((time.perf_counter() - started) * 1000),
                            "promptCache": engine.prompt_cache_status(),
                        },
                        request_id=str(dict(request.get("request_metadata") or {}).get("requestId") or ""),
                        request_type=normalize_prediction_request_type(request.get("request_type")),
                        profile_id=str(dict(request.get("request_metadata") or {}).get("profileId") or "default"),
                        model_id=engine.model_id,
                    ).to_payload(),
                    "requestMeta": dict(request.get("request_metadata") or {}),
                }
            )

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _write_json_line(self, payload: dict[str, Any]) -> bool:
            try:
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False
            return True

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return MlxPredictorHandler


def serve_mlx_predictor(config: MlxPredictorServerConfig) -> None:
    engine = MlxLmEngine(
        config.model,
        enable_prompt_cache=config.prompt_cache,
        prompt_cache_max_kv_size=config.prompt_cache_max_kv_size,
        profile_id=config.profile_id,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        top_p=config.top_p,
    )
    warmup = engine.warmup(
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        top_p=config.top_p,
    )
    server = ThreadingHTTPServer((config.host, config.port), make_mlx_predictor_handler(engine))
    print(
        json.dumps(
            {
                "schemaVersion": "rag-ime.mlx-predictor-server.v1",
                "listening": f"http://{config.host}:{config.port}",
                "model": config.model,
                "profile": config.profile_id,
                "maxTokens": config.max_tokens,
                "warmup": warmup,
                "promptCache": engine.prompt_cache_status(),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _normalize_prediction_request(
    payload: dict[str, Any],
    *,
    default_model: str,
    max_tokens_limit: int = 64,
) -> dict[str, Any]:
    _ = str(payload.get("model") or default_model)
    current_input = collapse_repeated_tail(
        compact_whitespace(str(_payload_value(payload, "currentInput", "current_input", default="") or ""))
    )
    recent_context = collapse_repeated_tail(
        compact_whitespace(str(_payload_value(payload, "recentContext", "recent_context", default="") or ""))[-420:]
    )
    return {
        "current_input": current_input,
        "recent_context": recent_context,
        "max_candidates": max(1, min(10, _int_payload(_payload_value(payload, "maxCandidates", "max_candidates"), 3))),
        "max_tokens": max(
            1,
            min(
                max(1, min(64, int(max_tokens_limit))),
                _int_payload(_payload_value(payload, "maxTokens", "max_tokens"), 8),
            ),
        ),
        "temperature": _float_payload(payload.get("temperature"), 0.15),
        "top_p": _float_payload(_payload_value(payload, "topP", "top_p"), 0.85),
        "request_type": normalize_prediction_request_type(_payload_value(payload, "requestType", "request_type")),
        "rime_candidates": normalized_rime_candidate_texts(_payload_value(payload, "rimeCandidates", "rime_candidates")),
        "stream_first_candidate": bool(_payload_value(payload, "streamFirstCandidate", "stream_first_candidate")),
        "request_metadata": _request_metadata_from_payload(payload),
    }


def _payload_value(payload: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload:
            return payload.get(name)
    return default


def _predict_error_payload(
    *,
    engine: MlxLmEngine,
    request: dict[str, Any],
    exc: Exception,
    started: float,
) -> dict[str, Any]:
    metadata = engine.canonical_request_metadata(request.get("request_metadata"))
    request_type = normalize_prediction_request_type(request.get("request_type"))
    payload = {
        "ok": False,
        "model": engine.model_id,
        "rawText": "",
        "candidates": [],
        "candidateScores": [],
        "candidateMode": "error",
        "requestType": request_type,
        "totalMs": int((time.perf_counter() - started) * 1000),
        "promptCache": engine.prompt_cache_status(),
        "timing": {
            "candidateMode": "error",
            "fallbackJson": False,
            "requestType": request_type,
            "errorType": exc.__class__.__name__,
        },
        "error": exc.__class__.__name__,
        "requestMeta": metadata,
    }
    payload["latencyTrace"] = trace_from_prediction_payload(
        payload,
        request_id=str(metadata.get("requestId") or ""),
        request_type=request_type,
        profile_id=str(metadata.get("profileId") or metadata.get("profile") or "default"),
        model_id=engine.model_id,
    ).to_payload()
    return payload


def _local_model_fingerprint(model_id: str) -> str:
    path = Path(model_id).expanduser()
    if not path.exists():
        return "runtime:mlx:unverified"
    return fingerprint_model_artifact(path)


def _inspect_local_mlx_model(model_id: str) -> dict[str, Any]:
    path = Path(model_id).expanduser()
    info: dict[str, Any] = {
        "modelId": model_id,
        "localPath": path.exists(),
        "textOnly": False,
        "hasVisionConfig": False,
        "configPresent": False,
        "modelFileCount": 0,
        "diskBytes": 0,
    }
    if not path.exists():
        info["reason"] = "non_local_model_id"
        return info

    model_dir = path if path.is_dir() else path.parent
    info["modelDir"] = str(model_dir)
    model_files = sorted(model_dir.glob("*.safetensors"))
    info["modelFileCount"] = len(model_files)
    info["diskBytes"] = sum(file.stat().st_size for file in model_files if file.is_file())

    config_path = model_dir / "config.json"
    if not config_path.exists():
        info["reason"] = "missing_config_json"
        return info

    info["configPresent"] = True
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        info["reason"] = f"invalid_config_json:{exc.__class__.__name__}"
        return info

    text_config = config.get("text_config") if isinstance(config.get("text_config"), dict) else {}
    source = text_config if text_config else config
    has_vision_config = isinstance(config.get("vision_config"), dict)
    info.update(
        {
            "architecture": _first_string(config.get("architectures")),
            "modelType": str(config.get("model_type") or source.get("model_type") or ""),
            "textModelType": str(text_config.get("model_type") or ""),
            "hasVisionConfig": has_vision_config,
            "textOnly": not has_vision_config,
            "vocabSize": _optional_int(source.get("vocab_size")),
            "hiddenSize": _optional_int(source.get("hidden_size")),
            "numHiddenLayers": _optional_int(source.get("num_hidden_layers")),
            "intermediateSize": _optional_int(source.get("intermediate_size")),
            "numAttentionHeads": _optional_int(source.get("num_attention_heads")),
            "numKeyValueHeads": _optional_int(source.get("num_key_value_heads")),
            "quantization": _quantization_summary(config),
        }
    )
    tokenizer_config_path = model_dir / "tokenizer_config.json"
    if tokenizer_config_path.exists():
        try:
            tokenizer_config = json.loads(tokenizer_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            tokenizer_config = {}
        chat_template = str(tokenizer_config.get("chat_template") or "")
        info["hasChatTemplate"] = bool(chat_template)
        info["chatTemplateSupportsThinking"] = "enable_thinking" in chat_template or "thinking" in chat_template.lower()
    else:
        info["hasChatTemplate"] = False
        info["chatTemplateSupportsThinking"] = False
    return info


def _first_string(value: Any) -> str:
    if isinstance(value, list) and value:
        return str(value[0])
    return str(value or "")


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quantization_summary(config: dict[str, Any]) -> dict[str, Any]:
    quantization = config.get("quantization")
    if not isinstance(quantization, dict):
        quantization = config.get("quantization_config")
    if not isinstance(quantization, dict):
        return {}
    return {
        key: value
        for key, value in quantization.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }


def _is_base_completion_model(model_id: str, model_info: dict[str, Any]) -> bool:
    prompt_mode = os.environ.get("RAG_IME_MLX_PROMPT_MODE", "").strip().lower().replace("_", "-")
    if prompt_mode in {"base", "base-completion", "completion", "none", "prompt-free"}:
        return True
    if prompt_mode in {"chat", "chat-json", "imev1", "instruction", "instruct"}:
        return False
    normalized_id = str(model_id).lower()
    path_parts = {part.lower() for part in Path(model_id).parts}
    if "base" in normalized_id or any(part.endswith("-base") or part == "base" for part in path_parts):
        return True
    if model_info.get("hasChatTemplate"):
        return False
    architecture = str(model_info.get("architecture") or "").lower()
    model_type = str(model_info.get("modelType") or "").lower()
    if "instruct" in normalized_id or "chat" in normalized_id:
        return False
    return architecture == "qwen3forcausallm" and model_type == "qwen3"




def _stable_prompt_prefix() -> str:
    return f"{SYSTEM_PROMPT}\n"


def _build_mlx_dynamic_prompt(
    *,
    current_input: str,
    recent_context: str,
    max_candidates: int,
    request_type: str = PREDICTION_REQUEST_GENERIC,
    rime_candidates: tuple[str, ...] = (),
    stream_first_candidate: bool = False,
) -> str:
    resolved_request_type = normalize_prediction_request_type(request_type)
    if (
        stream_first_candidate
        and resolved_request_type in {PREDICTION_REQUEST_NO_INPUT, PREDICTION_REQUEST_POST_COMMIT_COMPLETION, PREDICTION_REQUEST_IME_POST_COMMIT}
    ):
        context_tail = _tail_chars(compact_whitespace(recent_context), 180)
        return (
            f"已上屏文本: {context_tail}\n"
            "请只输出一个可直接插入光标后的短补全短语。"
            "不要 JSON, 不要编号, 不要解释, 不要复述已上屏文本, 不要输出拼音。\n"
            "补全:"
        )
    if os.environ.get("RAG_IME_MLX_LEGACY_PROMPT", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return _build_imev1_dynamic_prompt(
            current_input=current_input,
            recent_context=recent_context,
            max_candidates=max_candidates,
            request_type=request_type,
            rime_candidates=rime_candidates,
            stream_first_candidate=stream_first_candidate,
        )
    rime_candidate_tuple = normalized_rime_candidate_texts(rime_candidates)
    rime_line = _rime_candidates_prompt_line(rime_candidate_tuple)
    mode_instruction = _request_type_prompt_instruction(resolved_request_type)
    constraint_line = _request_type_candidate_constraint(resolved_request_type, rime_candidate_tuple)
    if stream_first_candidate:
        if resolved_request_type == PREDICTION_REQUEST_NO_INPUT:
            return (
                f"已上屏文本: {recent_context}\n"
                "光标后内容:"
            )
        return (
            f"请求类型: {resolved_request_type}\n"
            f"已上屏上下文: {recent_context}\n"
            f"当前拼音或参考候选: {current_input}\n"
            f"{rime_line}"
            f"模式说明: {mode_instruction}\n"
            f"{constraint_line}"
            "答案写用户下一步最可能输入的具体短语正文。"
        )
    return (
        f"请求类型: {resolved_request_type}\n"
        f"已上屏上下文: {recent_context}\n"
        f"当前拼音或参考候选: {current_input}\n"
        f"{rime_line}"
        f"模式说明: {mode_instruction}\n"
        f"{constraint_line}"
        "要求:\n"
        "- 输出能直接接在已上屏上下文后面的候选, 每个 2 到 16 个汉字为主。\n"
        "- 不要复述、改写、重排已上屏上下文本身; 候选必须是后文增量。\n"
        "- 当前输入如果是拼音、英文串或 Rime 候选列表, 只把它当作约束, 不要复述这些词。\n"
        "- 拼音约束模式下, 候选语义要符合上下文, 同时尽量满足当前拼音或首字母。\n"
        "- Rime 重排模式下, 优先从 Rime 候选里挑更符合上下文的词, 必要时只补充极短预测。\n"
        "- 候选要像用户下一步真的会输入的内容; 不要默认假设用户在写项目、输入法、RAG 或模型调试, 除非上下文明确出现这些主题。\n"
        "- 不要输出单字、语气词、连接词、泛词、重复词。\n"
        f"输出 {max_candidates} 个候选 JSON 数组。"
    )


def _build_imev1_dynamic_prompt(
    *,
    current_input: str,
    recent_context: str,
    max_candidates: int,
    request_type: str = PREDICTION_REQUEST_GENERIC,
    rime_candidates: tuple[str, ...] = (),
    stream_first_candidate: bool = False,
) -> str:
    resolved_request_type = normalize_prediction_request_type(request_type)
    rime_candidate_tuple = normalized_rime_candidate_texts(rime_candidates)
    context_tail = _tail_chars(collapse_repeated_tail(compact_whitespace(recent_context)), 96)
    query = collapse_repeated_tail(compact_whitespace(current_input))
    mode = _imev1_mode(resolved_request_type)
    rime_line = _rime_candidates_prompt_line(rime_candidate_tuple)
    mode_instruction = _request_type_prompt_instruction(resolved_request_type)
    constraint_line = _request_type_candidate_constraint(resolved_request_type, rime_candidate_tuple)
    negatives = "下一步|接下来|根据上述|可以进行|当前|目前|然后|测试|分析|验证|候选如下|输入法候选"
    if stream_first_candidate:
        target = "1"
    else:
        target = str(max(1, min(3, int(max_candidates))))
    return (
        "<IMEV1>\n"
        f"M={mode}\n"
        f"请求类型: {resolved_request_type}\n"
        f"CTX={context_tail}\n"
        f"IN={query}\n"
        f"RIME={'|'.join(rime_candidate_tuple[:5])}\n"
        f"{rime_line}"
        "RAG=\n"
        "MEM=\n"
        f"NEG={negatives}\n"
        f"模式说明: {mode_instruction}\n"
        f"{constraint_line}"
        f"OUT={target} candidates, tab-separated, append-only text, no JSON, no explanation.\n"
        "光标后内容:\n"
        "<CAND>\n"
    )


def _imev1_mode(request_type: str) -> str:
    resolved = normalize_prediction_request_type(request_type)
    if resolved in {PREDICTION_REQUEST_NO_INPUT, PREDICTION_REQUEST_POST_COMMIT_COMPLETION, PREDICTION_REQUEST_IME_POST_COMMIT}:
        return "POST"
    if resolved in {PREDICTION_REQUEST_IME_HOT, PREDICTION_REQUEST_PINYIN_CONSTRAINED}:
        return "HOT"
    if resolved == PREDICTION_REQUEST_IME_QUALITY:
        return "QUALITY_APPEND"
    if resolved == PREDICTION_REQUEST_ACTIVE_RAG:
        return "ACTIVE_RAG"
    if resolved == PREDICTION_REQUEST_RIME_REORDER:
        return "RIME"
    return "GEN"


def _request_type_prompt_instruction(request_type: str) -> str:
    resolved = normalize_prediction_request_type(request_type)
    if resolved in {PREDICTION_REQUEST_NO_INPUT, PREDICTION_REQUEST_POST_COMMIT_COMPLETION}:
        return "用户刚上屏了一段文字, 现在需要预测后文接龙, 不要做拼音转汉字。"
    if resolved == PREDICTION_REQUEST_PINYIN_CONSTRAINED:
        return "用户正在用拼音约束预测方向, 候选必须尽量匹配当前拼音或首字母约束。"
    if resolved == PREDICTION_REQUEST_RIME_REORDER:
        return "当前已有 Rime/Wanxiang 候选, 只在候选之间选择或补充极短候选, 不要自由发挥长句。"
    if resolved == PREDICTION_REQUEST_ACTIVE_RAG:
        return "当前是用户显式触发的选区 RAG Assist, 只生成可上屏短候选, 不解释, 不输出 Markdown, 不复读选区原文。"
    return "根据上下文给出输入法候选。"


def _request_type_candidate_constraint(request_type: str, rime_candidates: tuple[str, ...]) -> str:
    resolved = normalize_prediction_request_type(request_type)
    if resolved == PREDICTION_REQUEST_PINYIN_CONSTRAINED and rime_candidates:
        return f"硬约束: 每个候选必须以这些 Rime 候选之一开头: {' / '.join(rime_candidates[:8])}。\n"
    if resolved == PREDICTION_REQUEST_RIME_REORDER and rime_candidates:
        return "硬约束: 只能输出 Rime候选 原文或它们的序号, 不要创造新词。\n"
    return ""


def _rime_candidates_prompt_line(rime_candidates: tuple[str, ...] | list[str] | object) -> str:
    candidates = normalized_rime_candidate_texts(rime_candidates)
    if not candidates:
        return ""
    return f"Rime候选: {' / '.join(candidates[:8])}\n"


def _build_base_completion_prompt(*, current_input: str, recent_context: str) -> str:
    sections = _split_prediction_context(recent_context)
    current = compact_whitespace(sections["current"] or _strip_prediction_context_labels(recent_context))
    query = compact_whitespace(current_input)
    if current:
        query = _remove_context_overlap(query, current)
    if query and (_looks_like_candidate_constraint_query(query) or _looks_like_prompt_instruction(query)):
        query = ""
    if current and query:
        prompt = f"{current}{query}" if _CJK_RE.search(current[-1:]) and _CJK_RE.search(query[:1]) else f"{current} {query}"
    else:
        prompt = current or query
    return _tail_chars(compact_whitespace(prompt), 360)


def _parse_base_completion_candidates(
    raw_text: str,
    *,
    current_input: str,
    recent_context: str,
    max_candidates: int,
) -> list[str]:
    cleaned = _clean_base_completion_text(raw_text)
    if not cleaned:
        return []
    context_sections = _split_prediction_context(recent_context)
    context_text = compact_whitespace(" ".join(item for item in context_sections.values() if item))
    candidates = _base_candidate_parts(cleaned)
    if not candidates:
        candidates = parse_prediction_candidates(cleaned, max_candidates=max_candidates)
    if not candidates:
        candidates = [cleaned]
    return _filter_base_completion_candidates(
        candidates,
        current_input=current_input,
        recent_context=context_text or recent_context,
        max_candidates=max_candidates,
    )


def _clean_base_completion_text(text: str) -> str:
    text = re.split(r"<\|(?:im_end|endoftext|im_start)\|>", text, maxsplit=1)[0]
    cleaned = re.sub(r"(?is)<think>.*?</think>", " ", text)
    cleaned = re.sub(r"(?is)<think>.*", " ", cleaned)
    cleaned = re.sub(r"(?is)<analysis>.*?</analysis>", " ", cleaned)
    cleaned = re.sub(r"(?is)<reasoning>.*?</reasoning>", " ", cleaned)
    cleaned = re.sub(r"<\|[^|]{1,64}\|>", " ", cleaned)
    cleaned = cleaned.replace("Assistant:", " ").replace("assistant:", " ")
    cleaned = cleaned.replace("```json", " ").replace("```", " ")
    return compact_whitespace(cleaned)


def _base_candidate_parts(text: str) -> list[str]:
    json_candidates = _jsonish_base_candidates(text)
    if json_candidates:
        return json_candidates
    first_clause = re.split(r"[。！？!?；;\n\r]", text, maxsplit=1)[0]
    first_clause = re.sub(r"^[,，、\s]+", "", first_clause)
    if not first_clause:
        return []
    chunks = [part.strip(" ,，、。！？!?；;:：\"'“”‘’[]()（）") for part in re.split(r"[,，、]|\\s{2,}", first_clause)]
    chunks = [part for part in chunks if part]
    if chunks:
        return chunks
    return [first_clause]


def _jsonish_base_candidates(text: str) -> list[str]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [compact_whitespace(str(item)) for item in parsed if compact_whitespace(str(item))]
    if "[" in text:
        quoted = [match.strip() for match in re.findall(r'"([^"\n\r]{1,48})"', text)]
        if quoted:
            return quoted
    return []


def _filter_base_completion_candidates(
    candidates: list[str],
    *,
    current_input: str,
    recent_context: str,
    max_candidates: int,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    input_norm = _repeat_norm(current_input)
    context_norm = _repeat_norm(recent_context)
    for candidate in candidates:
        surface = _normalize_base_candidate(candidate)
        if not surface:
            continue
        norm = _repeat_norm(surface)
        if not norm or norm in seen:
            continue
        if input_norm and (norm == input_norm or norm in input_norm):
            continue
        if context_norm and (norm == context_norm or norm in context_norm):
            continue
        if _looks_like_prompt_instruction(surface) or _is_low_value_base_candidate(surface):
            continue
        seen.add(norm)
        result.append(surface)
        if len(result) >= max(1, int(max_candidates)):
            break
    return result


def _normalize_base_candidate(text: str) -> str:
    surface = compact_whitespace(text)
    surface = re.sub(r"^[0-9]+[.)、．]\s*", "", surface)
    surface = surface.strip(" \t\r\n\"'`[]{}(),，。！？:：;；、|")
    if not surface or "�" in surface or "<|" in surface:
        return ""
    if len(surface) > 24:
        surface = surface[:24]
    return surface


def _is_low_value_base_candidate(text: str) -> bool:
    normalized = compact_whitespace(text)
    if normalized in _LOW_VALUE_LOGITS_CANDIDATES:
        return True
    if re.fullmatch(r"(?:候选|预测|建议)[:：]\s*[A-Za-z0-9_-]{0,8}", normalized):
        return True
    if len(normalized) <= 1:
        return True
    cjk_count = len(_CJK_RE.findall(normalized))
    if re.fullmatch(r"[A-Za-z0-9_./:\-\s]{1,24}", normalized):
        return True
    if cjk_count < 2:
        return True
    if normalized.startswith(("我", "你", "您")) and len(normalized) <= 3:
        return True
    if re.fullmatch(r"(?:我|你|您)?(?:想|打算|准备|准备要|要|想要|计划)(?:设|写|做|改|看|试|用|把|让|给)?", normalized):
        return True
    if re.fullmatch(r"[嗯啊呃额哦噢唔]{1,4}", normalized):
        return True
    if normalized.endswith(("：", ":")) and len(normalized) <= 6:
        return True
    if normalized.endswith("候选") and len(normalized) <= 6:
        return True
    if normalized in {"短候", "长候"}:
        return True
    if re.search(r"[太先再又还更很最不没就也都才只要会能可得的地和或与并但而为从向在对被将把给让]$", normalized):
        return True
    if re.search(r"(?:可以|应该|需要|继续|先|再)(?:先|再)?(?:拿|把)$", normalized):
        return True
    if re.search(r"(?:太|很|更|最)(?:快|慢|远|近|长|短)路(?:上|线|径)?$", normalized):
        return True
    if re.search(r"(?:加|建|写|做)新事$", normalized):
        return True
    if re.search(r"(?:同步|导|拿|取|抽|提|找|列|移|搬|拷|复制|发|传)出$", normalized):
        return True
    if re.search(r"^(?:先|再)?把.{1,16}(?:放|写|改|存|移|传|发|拿|给)$", normalized):
        return True
    if re.search(r"^(?:过来)?等(?:会儿|一下)(?:再)?(?:跑|看|做|改)$", normalized):
        return True
    if re.search(r"^[去来回到]再(?:说|改|看|做)$", normalized):
        return True
    if re.search(r"([能再先在给把要可很就让还都也并])\1", normalized):
        return True
    if candidate_has_self_repetition(normalized):
        return True
    if _looks_like_meta_completion_candidate(normalized):
        return True
    return False


def _split_prediction_context(text: str) -> dict[str, str]:
    normalized = compact_whitespace(text)
    result = {"history": "", "current": ""}
    if not normalized:
        return result
    current_match = re.search(r"当前上下文:\s*(.+)$", normalized)
    if current_match:
        current = current_match.group(1)
        current = re.split(r"\s*历史(?:输入|参考)[^:：]*[:：]", current, maxsplit=1)[0]
        result["current"] = compact_whitespace(current)
    history_match = re.search(r"历史(?:输入|参考)[^:：]*[:：]\s*(.+?)(?:\s*当前上下文:|$)", normalized)
    if history_match:
        result["history"] = compact_whitespace(history_match.group(1))
    if not result["current"] and not result["history"]:
        result["current"] = normalized
    return result


def _strip_prediction_context_labels(text: str) -> str:
    stripped = re.sub(r"历史(?:输入|参考)[^:：]*[:：]", " ", text)
    stripped = stripped.replace("当前上下文:", " ")
    return compact_whitespace(stripped)


def _remove_context_overlap(query: str, current: str) -> str:
    query = compact_whitespace(query)
    current = compact_whitespace(current)
    if not query or not current:
        return query
    if _repeat_norm(query) == _repeat_norm(current[-len(query) :]):
        return ""
    if query.startswith(current[-min(len(current), len(query)) :]):
        return compact_whitespace(query.removeprefix(current[-min(len(current), len(query)) :]))
    return query


def _looks_like_candidate_constraint_query(text: str) -> bool:
    normalized = compact_whitespace(text)
    if not normalized:
        return False
    parts = [part for part in re.split(r"[\s,，、;；|/]+", normalized) if part]
    cjk_parts = sum(1 for part in parts if _CJK_RE.search(part))
    ascii_parts = sum(1 for part in parts if part.isascii())
    return len(parts) >= 2 and (cjk_parts >= 2 or ascii_parts >= 1)


def _looks_like_prompt_instruction(text: str) -> bool:
    lowered = text.lower()
    prompt_markers = (
        "已上屏上下文",
        "已上屏文本",
        "上屏文字",
        "请求类型",
        "模式说明",
        "当前拼音",
        "当前拼音或参考候选",
        "当前输入",
        "候选词",
        "输出",
        "json",
        "assistant",
        "system",
        "user",
        "/no_think",
    )
    return any(marker in lowered for marker in prompt_markers)


def _repeat_norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", compact_whitespace(text), flags=re.UNICODE).lower()


def _tail_chars(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _estimate_prompt_tokens(
    *,
    current_input: str,
    recent_context: str,
    rime_candidates: tuple[str, ...],
) -> int:
    text = " ".join([compact_whitespace(current_input), compact_whitespace(recent_context), " ".join(rime_candidates)])
    # Chinese IME prompts are short; chars/2 is a stable redacted estimate for gates.
    return max(1, int(len(text) / 2) + 24)


def _estimate_output_tokens(raw_text: Any, candidates: Any) -> int:
    if isinstance(raw_text, str) and raw_text:
        return max(1, int(len(compact_whitespace(raw_text)) / 2))
    if isinstance(candidates, list):
        text = " ".join(str(item) for item in candidates)
        return max(0, int(len(compact_whitespace(text)) / 2))
    return 0


def _request_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "requestId",
        "sessionId",
        "panelSessionId",
        "inputGeneration",
        "requestSeq",
        "profileId",
        "createdAtMs",
        "currentInputFingerprint",
        "contextFingerprint",
        "contextChars",
        "stablePrefixHash",
        "requestType",
        "rimeCandidateCount",
        "rimeCandidatesFingerprint",
    ):
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)):
            result[key] = value
    return result


@dataclass
class _PromptCacheState:
    enabled: bool
    stable_prefix: str
    max_kv_size: int = 0
    cache_file: str = ""
    prepared: bool = False
    stable_prefix_tokens: int = 0
    prepare_ms: int = 0
    used_for_generation: bool = False
    hit_count: int = 0
    miss_count: int = 0
    error: str = ""

    def stable_prefix_hash(self) -> str:
        return hashlib.sha256(self.stable_prefix.encode("utf-8")).hexdigest()[:16]

    def ready_for_generation(self) -> bool:
        return self.enabled and self.prepared and bool(self.cache_file) and os.path.exists(self.cache_file)

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "enabled": self.enabled,
            "prepared": self.prepared,
            "usedForGeneration": self.used_for_generation,
            "stablePrefixHash": self.stable_prefix_hash(),
            "stablePrefixTokens": self.stable_prefix_tokens,
            "prepareMs": self.prepare_ms,
            "maxKvSize": self.max_kv_size,
            "cacheFileReady": bool(self.cache_file) and os.path.exists(self.cache_file),
            "hits": self.hit_count,
            "misses": self.miss_count,
        }
        if self.error:
            payload["error"] = self.error
        if self.enabled and self.prepared and not self.used_for_generation:
            payload["reason"] = "prepared_only_streaming_generation_not_cached_yet"
        elif not self.enabled:
            payload["reason"] = "disabled"
        return payload


def _token_to_int(token: object) -> int:
    item = getattr(token, "item", None)
    if callable(item):
        return int(item())
    return int(token)  # type: ignore[arg-type]


def _tokenizer_eos_token_ids(tokenizer: Any) -> set[int]:
    values = getattr(tokenizer, "eos_token_ids", None)
    if values is None:
        values = getattr(tokenizer, "eos_token_id", None)
    if values is None:
        return set()
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    result: set[int] = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _token_is_eos(tokenizer: Any, token_id: int) -> bool:
    try:
        return int(token_id) in _tokenizer_eos_token_ids(tokenizer)
    except (TypeError, ValueError):
        return False


def _best_non_eos_token_id(logprobs: Any, tokenizer: Any) -> int | None:
    """Enforce one visible token for bare completion when EOS ranks first."""
    try:
        values = logprobs.tolist() if hasattr(logprobs, "tolist") else list(logprobs)
    except (TypeError, ValueError):
        return None
    if values and isinstance(values[0], list):
        values = values[0]
    ranked = sorted(range(len(values)), key=lambda token_id: float(values[token_id]), reverse=True)
    for token_id in ranked[:64]:
        if _token_is_eos(tokenizer, token_id):
            continue
        try:
            decoded = tokenizer.decode([token_id])
        except Exception:
            continue
        if isinstance(decoded, bytes):
            decoded = decoded.decode("utf-8", errors="ignore")
        if isinstance(decoded, str) and decoded.strip() and "\ufffd" not in decoded:
            return int(token_id)
    return None


def _token_decodes_visible_text(tokenizer: Any, token_id: int) -> bool:
    try:
        decoded = tokenizer.decode([token_id])
    except Exception:
        return False
    if isinstance(decoded, bytes):
        decoded = decoded.decode("utf-8", errors="ignore")
    return isinstance(decoded, str) and bool(decoded.strip()) and "\ufffd" not in decoded


def _truncate_at_generation_stop(text: str) -> tuple[str, bool]:
    stop_indexes = [text.find(marker) for marker in GENERATION_STOP_MARKERS if marker and marker in text]
    stop_indexes = [index for index in stop_indexes if index >= 0]
    if not stop_indexes:
        return text, False
    first_stop = min(stop_indexes)
    return text[:first_stop], True


def _visible_generation_text(text: str) -> tuple[str, bool]:
    visible, reached_stop = _truncate_at_generation_stop(text)
    if reached_stop:
        return visible, True
    for marker in GENERATION_STOP_MARKERS:
        max_prefix = min(len(marker) - 1, len(visible))
        for prefix_len in range(max_prefix, 0, -1):
            if visible.endswith(marker[:prefix_len]):
                return visible[:-prefix_len], False
    return visible, False


def _top_logprob_indices(logprobs: Any, *, limit: int) -> list[int]:
    if isinstance(logprobs, (list, tuple)):
        return sorted(range(len(logprobs)), key=lambda index: float(logprobs[index]), reverse=True)[:limit]
    try:
        import mlx.core as mx  # type: ignore

        order = mx.argsort(-logprobs)[:limit]
        return [int(item) for item in _array_to_list(order)]
    except Exception:
        return []


def _logprob_at(logprobs: Any, token_id: int) -> float | None:
    try:
        if isinstance(logprobs, (list, tuple)):
            return float(logprobs[token_id])
        value = logprobs[token_id]
        item = getattr(value, "item", None)
        return float(item() if callable(item) else value)
    except Exception:
        return None




def _array_to_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        result = tolist()
        return result if isinstance(result, list) else [result]
    try:
        return list(value)
    except TypeError:
        return [value]


def _normalize_logits_candidate_text(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    text = compact_whitespace(str(value))
    text = text.strip(" \t\r\n\"'`[]{}(),，。！？:：;；、|")
    text = text.replace("<0x0A>", "").replace("<|endoftext|>", "")
    if "<|" in text or "�" in text:
        return ""
    return text


def _logits_candidates_are_ime_quality(candidates: Any, *, max_candidates: int) -> bool:
    if not isinstance(candidates, list):
        return False
    texts = [compact_whitespace(str(item)) for item in candidates if compact_whitespace(str(item))]
    if not texts:
        return False
    if len(texts) < max(3, min(5, int(max_candidates))):
        return False
    phrase_like = [
        text
        for text in texts
        if len(text) >= 3
        and not _is_low_value_logits_candidate(text)
        and not re.fullmatch(r"[嗯啊呃额哦噢唔]{1,4}", text)
    ]
    if len(phrase_like) < max(3, min(5, int(max_candidates))):
        return False
    single_char_count = sum(1 for text in phrase_like if len(text) == 1)
    if single_char_count > len(phrase_like) // 2:
        return False
    return True


def _is_low_value_logits_candidate(text: str) -> bool:
    normalized = compact_whitespace(text)
    if normalized in _LOW_VALUE_LOGITS_CANDIDATES:
        return True
    if re.fullmatch(r"(?:候选|预测|建议)[:：]\s*[A-Za-z0-9_-]{0,8}", normalized):
        return True
    cjk_count = len(_CJK_RE.findall(normalized))
    if re.fullmatch(r"[A-Za-z0-9_./:\-\s]{1,24}", normalized):
        return True
    if cjk_count < 2:
        return True
    if any(normalized.startswith(prefix) for prefix in ("测试", "分析")) and len(normalized) <= 4:
        return True
    if any(normalized.startswith(prefix) for prefix in ("当前", "目前", "现在")) and len(normalized) <= 5:
        return True
    if _looks_like_meta_completion_candidate(normalized):
        return True
    return False


def _build_no_input_space_list_prompt(*, recent_context: str, max_candidates: int) -> str:
    max_items = max(1, min(10, int(max_candidates)))
    return (
        f"<|im_start|>system\n{SPACE_LIST_SYSTEM_PROMPT}<|im_end|>\n"
        "<|im_start|>user\n"
        f"上下文：\"{recent_context}\"\n"
        "当前输入：\"\"\n"
        f"候选词数量：{max_items}\n"
        "候选词："
        "<|im_end|>\n"
        f"{QWEN_NON_THINKING_ASSISTANT_PREFIX}"
    )


def _build_seeded_replay_prompt(*, recent_context: str, seed_text: str) -> str:
    return (
        f"<|im_start|>system\n{STREAM_FIRST_SYSTEM_PROMPT}<|im_end|>\n"
        "<|im_start|>user\n"
        f"上下文：\"{recent_context}\"\n"
        f"当前输入：\"\"\n"
        f"种子候选：\"{seed_text}\"\n"
        "请把这个种子候选续写成一个可直接上屏的短语。"
        "不要解释，不要换行，不要输出多个候选。"
        "<|im_end|>\n"
        f"{QWEN_NON_THINKING_ASSISTANT_PREFIX}{seed_text}"
    )


def _build_seeded_sequence_fork_base_prompt(*, recent_context: str) -> str:
    return (
        f"<|im_start|>system\n{STREAM_FIRST_SYSTEM_PROMPT}<|im_end|>\n"
        "<|im_start|>user\n"
        f"上下文：\"{recent_context}\"\n"
        "当前输入：\"\"\n"
        "任务：assistant 会先给出一个种子候选。"
        "请只把这个种子候选续写成一个可直接上屏的短语。"
        "不要解释，不要换行，不要输出多个候选。"
        "<|im_end|>\n"
        f"{QWEN_NON_THINKING_ASSISTANT_PREFIX}"
    )


def _space_list_continuation_candidates(
    raw_text: str,
    *,
    current_input: str,
    recent_context: str,
    max_candidates: int,
) -> list[str]:
    cleaned = _clean_base_completion_text(raw_text)
    if not cleaned:
        return []
    parts = [
        part
        for part in re.split(r"[\s,，、;；|/\n\r]+", cleaned)
        if compact_whitespace(part)
    ]
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        candidate = _branch_continuation_candidate(
            part,
            current_input=current_input,
            recent_context=recent_context,
            max_candidate_chars=12,
        )
        normalized = compact_whitespace(candidate)
        if (
            not normalized
            or normalized in seen
            or _is_low_value_base_candidate(normalized)
            or _looks_like_meta_completion_candidate(normalized)
        ):
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= max(1, int(max_candidates)):
            break
    return result


def _should_use_realtime_post_commit_fast_path(
    request_type: str,
    *,
    current_input: str,
    stream_first_candidate: bool,
) -> bool:
    if compact_whitespace(current_input):
        return False
    resolved = normalize_prediction_request_type(request_type)
    if stream_first_candidate and resolved in {
        PREDICTION_REQUEST_NO_INPUT,
        PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
        PREDICTION_REQUEST_IME_POST_COMMIT,
    }:
        return True
    return resolved in {
        PREDICTION_REQUEST_POST_COMMIT_COMPLETION,
        PREDICTION_REQUEST_IME_POST_COMMIT,
    }


def _realtime_post_commit_token_budget(max_tokens: int) -> int:
    return max(4, min(12, int(max_tokens)))


def _realtime_post_commit_candidate(
    raw_text: str,
    *,
    current_input: str,
    recent_context: str,
) -> str:
    candidate = _branch_continuation_candidate(
        raw_text,
        current_input=current_input,
        recent_context=recent_context,
        max_candidate_chars=14,
    )
    if (
        not candidate
        or _is_low_value_base_candidate(candidate)
        or _looks_like_meta_completion_candidate(candidate)
    ):
        return ""
    return candidate


def _continuation_branch_specs(*, temperature: float, max_tokens: int) -> list[_ContinuationBranchSpec]:
    low_temperature = max(0.05, min(float(temperature), 0.18))
    token_budget = max(12, min(64, int(max_tokens)))
    return [
        _ContinuationBranchSpec(
            label="lead",
            temperature=low_temperature,
            max_tokens=min(24, token_budget),
            max_candidate_chars=24,
        )
    ]


def _seed_replay_specs_from_logits(
    candidate_scores: Any,
    *,
    max_seeds: int,
    allow_single_cjk: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(candidate_scores, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidate_scores:
        if not isinstance(item, dict):
            continue
        text = compact_whitespace(str(item.get("text") or ""))
        low_value_seed = (
            text in _LOW_VALUE_LOGITS_CANDIDATES
            if allow_single_cjk
            else _is_low_value_base_candidate(text)
        )
        if not text or text in seen or low_value_seed or _looks_like_meta_completion_candidate(text):
            continue
        if allow_single_cjk and not _CJK_RE.search(text):
            continue
        if len(text) > 6:
            continue
        seen.add(text)
        result.append(
            {
                "text": text,
                "tokenId": item.get("tokenId"),
                "logprob": item.get("logprob"),
                "probability": item.get("probability"),
            }
        )
        if len(result) >= max(1, int(max_seeds)):
            break
    return result


def _initial_base_completion_seed_indexes(
    seeds: list[dict[str, Any]],
    *,
    recent_context: str,
    display_limit: int,
) -> list[int]:
    if not seeds:
        return []
    batch_limit = min(len(seeds), max(1, int(display_limit)) + 1)
    context = compact_whitespace(recent_context).lower()
    context_terms = {term for term in _COMPLETION_DOMAIN_TERMS if term in context}
    domain_context = bool(context_terms.intersection(_COMPLETION_HIGH_SIGNAL_TERMS)) or len(context_terms) >= 2
    if not domain_context:
        return list(range(batch_limit))

    def seed_score(index: int) -> tuple[int, int]:
        seed_text = compact_whitespace(str(seeds[index].get("text") or "")).lower()
        affinity = sum(
            1
            for term in context_terms
            if seed_text and (term.startswith(seed_text) or seed_text.startswith(term))
        )
        low_information_penalty = 1 if seed_text in _LOW_INFORMATION_COMPLETION_SEEDS else 0
        return affinity * 100 - low_information_penalty * 50, -index

    chosen = sorted(range(len(seeds)), key=seed_score, reverse=True)[:batch_limit]
    return sorted(chosen)


def _rank_base_completion_candidates(
    candidates: list[str],
    *,
    recent_context: str,
) -> tuple[list[str], dict[str, Any]]:
    context = compact_whitespace(recent_context).lower()
    context_terms = {term for term in _COMPLETION_DOMAIN_TERMS if term in context}
    domain_context = bool(context_terms.intersection(_COMPLETION_HIGH_SIGNAL_TERMS)) or len(context_terms) >= 2
    if not domain_context or len(candidates) <= 1:
        return list(candidates), {
            "qualityReranked": False,
            "contextDomainTermCount": len(context_terms),
        }

    indexed = list(enumerate(candidates))

    def candidate_score(item: tuple[int, str]) -> tuple[int, int, int]:
        index, candidate = item
        normalized = compact_whitespace(candidate).lower()
        candidate_terms = {term for term in _COMPLETION_DOMAIN_TERMS if term in normalized}
        shared_terms = candidate_terms.intersection(context_terms)
        generic_personal_lead = normalized.startswith(("我", "先", "晚点", "明天", "等晚上"))
        score = len(shared_terms) * 6 + len(candidate_terms) * 2
        if generic_personal_lead and not shared_terms:
            score -= 2
        return score, len(shared_terms), -index

    ranked = [candidate for _, candidate in sorted(indexed, key=candidate_score, reverse=True)]
    return ranked, {
        "qualityReranked": ranked != candidates,
        "contextDomainTermCount": len(context_terms),
    }


def _branch_continuation_candidate(
    raw_text: str,
    *,
    current_input: str,
    recent_context: str,
    max_candidate_chars: int,
) -> str:
    parsed = parse_ime_prediction_candidates(
        raw_text,
        max_candidates=8,
        current_input=current_input,
        recent_context=recent_context,
        request_type=PREDICTION_REQUEST_NO_INPUT,
    )
    if not parsed:
        return ""
    limit = max(2, int(max_candidate_chars))
    eligible = [candidate for candidate in parsed if len(candidate) <= limit]
    if eligible:
        candidate = max(eligible, key=len)
    else:
        candidate = parsed[0][:limit]
    if _is_low_value_base_candidate(candidate) or _looks_like_meta_completion_candidate(candidate):
        return ""
    return candidate


def _branch_continuation_candidates(
    raw_text: str,
    *,
    current_input: str,
    recent_context: str,
    max_candidate_chars: int,
    max_candidates: int,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    parts = [
        compact_whitespace(part)
        for part in re.split(r"[\s,，、;；。.!！?？\n\r]+", _clean_base_completion_text(raw_text))
        if compact_whitespace(part)
    ]
    if len(parts) <= 1:
        parts = [raw_text]
    for part in parts:
        candidate = _branch_continuation_candidate(
            part,
            current_input=current_input,
            recent_context=recent_context,
            max_candidate_chars=max_candidate_chars,
        )
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
        if len(result) >= max(1, int(max_candidates)):
            break
    return result


def _seeded_replay_candidate(
    *,
    seed_text: str,
    raw_text: str,
    current_input: str,
    recent_context: str,
    max_candidate_chars: int,
) -> str:
    seed = compact_whitespace(seed_text)
    continuation = _clean_base_completion_text(raw_text)
    if continuation.startswith(seed):
        combined = continuation
    else:
        combined = compact_whitespace(f"{seed}{continuation}")
    normalized_direct = _normalize_base_candidate(combined)
    direct = _repair_base_completion_candidate(normalized_direct)
    repaired = bool(direct and direct != normalized_direct)
    if direct and (
        _is_low_value_base_candidate(direct)
        or _looks_like_meta_completion_candidate(direct)
    ):
        # Do not let the generic parser rescue a malformed full branch by
        # slicing off its bad ending. That previously turned
        # `调整不要一开始就改太` into the deceptively clean `调整不要一开`.
        return ""
    if (
        direct
        and (
            (repaired and len(direct) > 1)
            or (direct.startswith(seed) and len(direct) > len(seed))
        )
        and len(direct) <= max(2, int(max_candidate_chars))
        and not _is_low_value_base_candidate(direct)
        and not _looks_like_meta_completion_candidate(direct)
    ):
        return direct
    parsed = parse_ime_prediction_candidates(
        combined,
        max_candidates=4,
        current_input=current_input,
        recent_context=recent_context,
        request_type=PREDICTION_REQUEST_NO_INPUT,
    )
    limit = max(2, int(max_candidate_chars))
    for candidate in parsed:
        normalized = compact_whitespace(candidate)
        if (
            normalized
            and normalized.startswith(seed)
            and len(normalized) > len(seed)
            and len(normalized) <= limit
            and not _is_low_value_base_candidate(normalized)
            and not _looks_like_meta_completion_candidate(normalized)
        ):
            return normalized
    return ""


def _repair_base_completion_candidate(text: str) -> str:
    """Repair a few deterministic truncation shapes from the tiny checkpoint."""

    surface = compact_whitespace(text)
    if not surface:
        return ""
    surface = re.sub(r"加新事$", "加新内容", surface)
    surface = re.sub(r"定反$", "定方案", surface)
    adjective_first = re.fullmatch(r"(?:太|很|更|最)?(?:复杂|快|慢|难|多|长|远)先(.{2,12})", surface)
    if adjective_first:
        surface = compact_whitespace(f"先{adjective_first.group(1)}")
    surface = re.sub(r"最重要的([两三几])$", r"最重要的\1项", surface)
    if surface == "一版再说":
        surface = "先做一版再说"
    simple_object = re.fullmatch(r"(.{2,10}?)(?:最好)?加个简单", surface)
    if simple_object:
        return compact_whitespace(f"先补一个简单的{simple_object.group(1)}")
    return surface


def _expand_continuation_candidates_from_model_output(
    candidates: list[str],
    *,
    current_input: str,
    recent_context: str,
    max_candidates: int,
) -> list[str]:
    """Split a real model continuation into several selectable IME candidates.

    This keeps post-commit prediction local-model based even when the small MLX
    model emits one fluent continuation instead of a candidate list.
    """

    max_items = max(1, int(max_candidates))
    result: list[str] = []
    seen = {compact_whitespace(item) for item in candidates if compact_whitespace(item)}
    for candidate in candidates:
        normalized = compact_whitespace(candidate)
        if not normalized or _looks_like_meta_completion_candidate(normalized):
            continue
        splits = _continuation_candidate_splits(normalized)
        if not splits:
            splits = parse_ime_prediction_candidates(
                normalized,
                max_candidates=max_items,
                current_input=current_input,
                recent_context=recent_context,
                request_type=PREDICTION_REQUEST_NO_INPUT,
            )
        for item in splits:
            item = compact_whitespace(item)
            if (
                not item
                or item in seen
                or _is_low_value_base_candidate(item)
                or _looks_like_meta_completion_candidate(item)
            ):
                continue
            seen.add(item)
            result.append(item)
            if len(seen) >= max_items:
                return result
    return result


def _continuation_candidate_splits(text: str) -> list[str]:
    normalized = compact_whitespace(text)
    compacted = re.sub(r"\s+", "", normalized)
    if _CJK_RE.search(compacted) is None or len(compacted) < 5:
        return []
    base = _strip_leading_continuation_connector(compacted)
    variants = [base]
    for match in re.finditer(r"(更直观|看到|优化|完成|继续|实现|提升|减少|帮助|方便|用于|接入|整理|修复|验证)", base):
        if 0 < match.start() <= len(base) - 4:
            variants.append(base[match.start() :])
        if 0 < match.end() <= len(base) - 4:
            variants.append(base[match.end() :])
    result: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        if len(variant) < 4:
            continue
        full_variant = _normalize_continuation_split_candidate(variant[:12])
        if (
            full_variant
            and full_variant not in seen
            and len(full_variant) >= 4
            and not _is_low_value_base_candidate(full_variant)
            and not _looks_like_meta_completion_candidate(full_variant)
        ):
            seen.add(full_variant)
            result.append(full_variant)
        if len(variant) >= 4:
            tail = _normalize_continuation_split_candidate(variant[-min(6, len(variant)) :])
            if (
                tail
                and tail not in seen
                and len(tail) >= 3
                and not _is_low_value_base_candidate(tail)
                and not _looks_like_meta_completion_candidate(tail)
            ):
                seen.add(tail)
                result.append(tail)
    return result


def _strip_leading_continuation_connector(text: str) -> str:
    result = compact_whitespace(text)
    for prefix in ("从而", "用于", "以便", "为了", "并且", "然后", "接着", "以", "并", "来", "将"):
        if result.startswith(prefix) and len(result) - len(prefix) >= 4:
            return result[len(prefix) :]
    return result


def _normalize_continuation_split_candidate(text: str) -> str:
    item = compact_whitespace(str(text))
    item = item.strip(" \t\r\n。.!！?？:\"'“”‘’[]()（）{}<>《》")
    return compact_whitespace(item)


def _looks_like_meta_completion_candidate(text: str) -> bool:
    normalized = compact_whitespace(text)
    if not normalized:
        return False
    meta_prefixes = (
        "你正在",
        "您正在",
        "用户正在",
        "当前正在",
        "正在输入",
        "正在阅读",
    )
    attempt_prefixes = (
        "你尝试",
        "您尝试",
        "用户尝试",
    )
    if any(normalized.startswith(prefix) for prefix in attempt_prefixes):
        return True
    meta_markers = (
        "已经上屏",
        "上屏文本",
        "说明性文本",
        "关于“",
        "关于\"",
        "作为候选",
        "输入法候选",
        "LLM",
        "RAG",
        "功能",
        "方法",
        "成功",
        "请检查",
        "重新输入",
        "这个项目",
        "号项目",
        "项目吗",
    )
    if not any(normalized.startswith(prefix) for prefix in meta_prefixes):
        return False
    if len(normalized) <= 8:
        return True
    return any(marker in normalized for marker in meta_markers)


def _continuation_branch_candidate_scores(
    candidates: list[str],
    *,
    branch_timings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    branch_by_candidate: dict[str, str] = {}
    for branch in branch_timings:
        label = str(branch.get("label") or "branch")
        branch_candidates = branch.get("candidates")
        if not isinstance(branch_candidates, list):
            continue
        for item in branch_candidates:
            text = compact_whitespace(str(item))
            if text and text not in branch_by_candidate:
                branch_by_candidate[text] = label
    total = max(1, len(candidates))
    result: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        text = compact_whitespace(candidate)
        if not text:
            continue
        result.append(
            {
                "text": text,
                "rank": index,
                "source": branch_by_candidate.get(text, "branch"),
                "mode": "continuation-branches",
                "confidence": max(0.0, min(1.0, 1.0 - ((index - 1) / max(3, total + 1)) * 0.35)),
            }
        )
    return result


def _seeded_prompt_replay_candidate_scores(
    candidates: list[str],
    *,
    branch_timings: list[dict[str, Any]],
    mode: str = "seeded-prompt-replay",
) -> list[dict[str, Any]]:
    branch_by_candidate: dict[str, dict[str, Any]] = {}
    for branch in branch_timings:
        branch_candidates = branch.get("candidates")
        if not isinstance(branch_candidates, list):
            continue
        for item in branch_candidates:
            text = compact_whitespace(str(item))
            if text and text not in branch_by_candidate:
                branch_by_candidate[text] = branch
    total = max(1, len(candidates))
    result: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        text = compact_whitespace(candidate)
        if not text:
            continue
        branch = branch_by_candidate.get(text, {})
        result.append(
            {
                "text": text,
                "rank": index,
                "source": str(branch.get("label") or "seed-replay"),
                "mode": mode,
                "branchRank": branch.get("branchRank"),
                "branchCount": branch.get("branchCount"),
                "seedText": branch.get("seedText"),
                "seedTokenId": branch.get("seedTokenId"),
                "probability": branch.get("probability"),
                "logprob": branch.get("logprob"),
                "confidence": max(0.0, min(1.0, 1.0 - ((index - 1) / max(3, total + 1)) * 0.28)),
            }
        )
    return result


def _backfill_payload_candidates_with_rime(
    payload: dict[str, Any],
    *,
    rime_candidates: tuple[str, ...],
    max_candidates: int,
    empty_mode: str,
) -> None:
    target = max(1, int(max_candidates))
    existing = [
        compact_whitespace(str(item))
        for item in payload.get("candidates", [])
        if compact_whitespace(str(item))
    ]
    candidates = list(existing)
    for item in rime_candidates:
        text = compact_whitespace(str(item))
        if text and text not in candidates:
            candidates.append(text)
        if len(candidates) >= target:
            break
    if candidates == existing:
        return
    was_empty = not existing
    payload["candidates"] = candidates[:target]
    scores = [
        dict(item)
        for item in payload.get("candidateScores", [])
        if isinstance(item, dict) and compact_whitespace(str(item.get("text") or ""))
    ]
    scored_texts = {compact_whitespace(str(item.get("text") or "")) for item in scores}
    for index, text in enumerate(payload["candidates"], start=1):
        if text in scored_texts:
            continue
        scores.append(
            {
                "text": text,
                "rank": index,
                "source": "rime-fallback" if was_empty else "rime-backfill",
                "mode": "rime-candidate-fallback" if was_empty else "rime-candidate-backfill",
                "confidence": max(0.0, min(1.0, 0.82 - (index - 1) * 0.08)),
            }
        )
    for index, score in enumerate(scores, start=1):
        score["rank"] = index
    payload["candidateScores"] = scores[:target]
    if was_empty:
        payload["candidateMode"] = empty_mode
        payload["rawText"] = " ".join(payload["candidates"])
    timing = payload.get("timing")
    if isinstance(timing, dict):
        if was_empty:
            timing["candidateMode"] = empty_mode
            timing["rimeFallback"] = True
            timing["rimeFallbackReason"] = "model_empty_after_empty_input_fallback"
        else:
            timing["rimeBackfill"] = True
            timing["rimeBackfillCount"] = max(0, len(payload["candidates"]) - len(existing))


def _merge_seeded_replay_with_branch_payload(
    seeded_payload: dict[str, Any],
    branch_payload: dict[str, Any],
    *,
    max_candidates: int,
    started: float,
) -> dict[str, Any]:
    target = max(1, int(max_candidates))
    merged_candidates: list[str] = []
    seeded_candidates = seeded_payload.get("candidates") if isinstance(seeded_payload.get("candidates"), list) else []
    branch_candidates = branch_payload.get("candidates") if isinstance(branch_payload.get("candidates"), list) else []
    for payload_candidates in (seeded_candidates, branch_candidates):
        for item in payload_candidates:
            text = compact_whitespace(str(item))
            if not text or text in merged_candidates:
                continue
            merged_candidates.append(text)
            if len(merged_candidates) >= target:
                break
        if len(merged_candidates) >= target:
            break

    score_by_text: dict[str, dict[str, Any]] = {}
    for payload in (seeded_payload, branch_payload):
        scores = payload.get("candidateScores") if isinstance(payload.get("candidateScores"), list) else []
        for score in scores:
            if not isinstance(score, dict):
                continue
            text = compact_whitespace(str(score.get("text") or ""))
            if text and text not in score_by_text:
                score_by_text[text] = dict(score)

    total = max(1, len(merged_candidates))
    merged_scores: list[dict[str, Any]] = []
    for rank, text in enumerate(merged_candidates, start=1):
        score = dict(score_by_text.get(text) or {"text": text, "source": "merged-fallback"})
        score["text"] = text
        score["rank"] = rank
        score["confidence"] = max(0.0, min(1.0, 1.0 - ((rank - 1) / max(3, total + 1)) * 0.28))
        merged_scores.append(score)

    timing = dict(seeded_payload.get("timing") if isinstance(seeded_payload.get("timing"), dict) else {})
    branch_timing = branch_payload.get("timing") if isinstance(branch_payload.get("timing"), dict) else {}
    seeded_mode = str(seeded_payload.get("candidateMode") or "seeded-prompt-replay")
    timing.update(
        {
            "candidateMode": seeded_mode,
            "underfilled": True,
            "seededCandidateCount": len(seeded_candidates),
            "fallbackCandidateMode": branch_payload.get("candidateMode"),
            "fallbackCandidateCount": len(branch_candidates),
            "filledByFallbackCount": max(0, len(merged_candidates) - len(seeded_candidates)),
            "fallbackBranches": branch_timing.get("branches") if isinstance(branch_timing, dict) else [],
        }
    )

    raw_parts = [
        compact_whitespace(str(seeded_payload.get("rawText") or "")),
        compact_whitespace(str(branch_payload.get("rawText") or "")),
    ]
    merged = dict(seeded_payload)
    merged.update(
        {
            "rawText": "\n".join(part for part in raw_parts if part),
            "candidates": merged_candidates,
            "candidateScores": merged_scores,
            "candidateMode": seeded_mode,
            "totalMs": int((time.perf_counter() - started) * 1000),
            "timing": timing,
        }
    )
    return merged


def _prompt_cache_used_for_generation(prompt_cache: dict[str, Any]) -> bool:
    return (
        bool(prompt_cache.get("enabled"))
        and bool(prompt_cache.get("prepared"))
        and bool(prompt_cache.get("cacheFileReady"))
        and bool(prompt_cache.get("usedForGeneration"))
    )


def _int_payload(value: object, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _float_payload(value: object, fallback: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag-ime-mlx-predictor-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-tokens", type=int, default=8)
    parser.add_argument("--temperature", type=float, default=0.15)
    parser.add_argument("--top-p", type=float, default=0.85)
    parser.add_argument("--prompt-cache", action="store_true", help="Prepare the stable system-prompt cache at startup")
    parser.add_argument("--prompt-cache-max-kv-size", type=int, default=0)
    args = parser.parse_args(argv)
    serve_mlx_predictor(
        MlxPredictorServerConfig(
            host=args.host,
            port=args.port,
            model=args.model,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
            prompt_cache=args.prompt_cache,
            prompt_cache_max_kv_size=args.prompt_cache_max_kv_size,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
