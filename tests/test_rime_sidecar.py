from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rag_ime.adapter import InputMethodAdapter
from rag_ime import rime_sidecar as rime_sidecar_module
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_optimizer import optimize_suggestions_if_enabled
from rag_ime.models import InputSuggestion, ModelPrediction
from rag_ime.rime_sidecar import (
    build_rime_sidecar_response,
    clear_model_prediction_holdover_cache,
    clear_prediction_manager_cache,
    clear_refresh_debounce_cache,
    filter_post_commit_model_completions,
    key_policy_for_prediction_session,
    record_rime_side_candidate_selection,
    wait_for_model_prediction_lane_idle,
)


class RecordingPredictionProvider:
    def __init__(self, predictions: list[str] | None = None, *, sleep_s: float = 0.0) -> None:
        self.predictions = predictions or ["真实输入链路跑通", "v1 范围收缩", "候选质量验收"]
        self.sleep_s = sleep_s
        self.calls = 0
        self.requests: list[dict[str, object]] = []

    def predict(self, **kwargs: object) -> list[ModelPrediction]:
        self.calls += 1
        self.requests.append(dict(kwargs))
        if self.sleep_s > 0:
            time.sleep(self.sleep_s)
        max_candidates = int(kwargs.get("max_candidates") or 5)
        return [
            ModelPrediction(
                text=text,
                rank=index,
                provider_name="v1-contract-model",
                latency_ms=int(self.sleep_s * 1000),
                confidence=0.9,
            )
            for index, text in enumerate(self.predictions, start=1)
        ][:max_candidates]


class InlinePredictionThread:
    def __init__(self, *, target: Callable[..., object], kwargs: dict[str, object] | None = None, **_: object) -> None:
        self._target = target
        self._kwargs = kwargs or {}

    def start(self) -> None:
        self._target(**self._kwargs)

    def is_alive(self) -> bool:
        return False


class CuratedMemoryCore:
    def __init__(self, suggestions: list[InputSuggestion] | None = None) -> None:
        self.suggestions = suggestions or []
        self.calls = 0
        self.last_current_input = ""
        self.last_recent_context = ""

    def suggest_for_input(
        self,
        *,
        current_input: str,
        recent_context: str = "",
        project: str = "",
        app: str = "",
        top_k: int = 5,
    ) -> list[InputSuggestion]:
        self.calls += 1
        self.last_current_input = current_input
        self.last_recent_context = recent_context
        return self.suggestions[:top_k]

    def recent_input_context(self, *, project: str = "", limit: int = 6, max_chars: int = 420) -> str:
        return "历史输入只能给模型上下文，不能直接污染 RAG 候选"


def _memory(
    suggestion_id: str,
    text: str,
    *,
    source_type: str = "memory",
    accepted_count: int = 0,
    confidence: float = 0.92,
) -> InputSuggestion:
    return InputSuggestion(
        suggestion_id=suggestion_id,
        surface_text=text,
        suggestion_type="phrase",
        source_event_id=abs(hash(suggestion_id)) % 100000,
        evidence_preview="v1 contract fixture",
        confidence=confidence,
        metadata={
            "source_type": source_type,
            "state": {"accepted_count": accepted_count},
        },
    )


class RimeSidecarV1ContractTests(unittest.TestCase):
    def test_progressive_candidate_text_keeps_the_same_snapshot_slot_identity(self) -> None:
        partial = SimpleNamespace(
            source_type="model",
            source_index=0,
            text="继续",
            insert_text="继续",
        )
        final = SimpleNamespace(
            source_type="model",
            source_index=0,
            text="继续完成整个前台验收",
            insert_text="继续完成整个前台验收",
        )

        partial_id = rime_sidecar_module._candidate_stable_id(partial, snapshot_id="snap:stream")
        final_id = rime_sidecar_module._candidate_stable_id(final, snapshot_id="snap:stream")

        self.assertEqual(partial_id, final_id)
        self.assertNotEqual(
            final_id,
            rime_sidecar_module._candidate_stable_id(final, snapshot_id="snap:next"),
        )

        action_before_expansion = SimpleNamespace(
            source_type="action",
            source_index=1,
            selection_action="start_active_rag_from_context",
            text="知识生成",
            insert_text="",
        )
        action_after_expansion = SimpleNamespace(
            source_type="action",
            source_index=3,
            selection_action="start_active_rag_from_context",
            text="知识生成",
            insert_text="",
        )
        self.assertEqual(
            rime_sidecar_module._candidate_stable_id(action_before_expansion, snapshot_id="snap:stream"),
            rime_sidecar_module._candidate_stable_id(action_after_expansion, snapshot_id="snap:stream"),
        )

    def setUp(self) -> None:
        self.assertTrue(wait_for_model_prediction_lane_idle(timeout_s=1.0))
        clear_model_prediction_holdover_cache()
        clear_prediction_manager_cache()
        clear_refresh_debounce_cache()
        self.env = patch.dict(
            os.environ,
            {
                "RAG_IME_ENABLE_POST_COMMIT_ASYNC_COMPLETION": "1",
                "RAG_IME_ENABLE_POST_COMMIT_AUTO_MODEL": "1",
                "RAG_IME_ENABLE_COMPOSING_MODEL": "0",
                "RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL": "0",
                "RAG_IME_POST_COMMIT_FIRST_RESPONSE_MS": "150",
                "RAG_IME_POST_COMMIT_MODEL_BUDGET_MS": "900",
                "RAG_IME_POST_COMMIT_MODEL_HARD_TIMEOUT_MS": "12000",
                "RAG_IME_PROGRESSIVE_FOLLOW_UP_RETRY_MS": "250",
                "RAG_IME_POST_COMMIT_PRESENTATION_STREAM": "0",
                "RAG_IME_REFRESH_DEBOUNCE_MS": "0",
                "RAG_IME_PREDICTION_STATUS_ROW": "1",
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.assertTrue(wait_for_model_prediction_lane_idle(timeout_s=2.0))
        clear_model_prediction_holdover_cache()
        clear_prediction_manager_cache()
        clear_refresh_debounce_cache()
        self.env.stop()

    def _adapter(self, core: CuratedMemoryCore) -> InputMethodAdapter:
        return InputMethodAdapter(core)

    def test_post_commit_model_budget_prefers_live_runtime_settings(self) -> None:
        snapshot = rime_sidecar_module.parse_rime_context_payload(
            self._post_commit_payload(),
            default_project="wisdom-weasel-rag-ime",
        )
        runtime_config = SimpleNamespace(
            post_commit=SimpleNamespace(model_budget_ms=1300)
        )

        budget = rime_sidecar_module._model_lane_budget_for_request(
            900,
            snapshot=snapshot,
            runtime_config=runtime_config,
        )

        self.assertEqual(budget, 1300)

    def test_post_commit_async_job_uses_model_budget_as_late_result_deadline(self) -> None:
        snapshot = rime_sidecar_module.parse_rime_context_payload(
            self._post_commit_payload(),
            default_project="wisdom-weasel-rag-ime",
        )
        runtime_config = SimpleNamespace(
            snapshot_hash="sha256:runtime",
            post_commit=SimpleNamespace(
                completion_ttl_ms=4000,
                model_budget_ms=1300,
                model_hard_timeout_ms=12000,
            ),
            memory=SimpleNamespace(enabled=True),
        )
        with patch.object(
            rime_sidecar_module._POST_COMMIT_COMPLETION_CACHE,
            "poll_or_start",
            return_value=([], {}),
        ) as poll:
            rime_sidecar_module.run_post_commit_completion_async(
                core=object(),  # type: ignore[arg-type]
                predictor=object(),  # type: ignore[arg-type]
                snapshot=snapshot,
                explicit_recent_context="继续整理输入法",
                project="wisdom-weasel-rag-ime",
                max_candidates=3,
                model_budget_ms=1300,
                runtime_config=runtime_config,  # type: ignore[arg-type]
            )

        self.assertEqual(poll.call_args.kwargs["hard_timeout_ms"], 1300)

    def test_model_lane_exposes_context_and_decode_observability(self) -> None:
        prediction = ModelPrediction(
            text="候选不要重复",
            rank=1,
            provider_name="local-mlx",
            latency_ms=88,
            metadata={
                "server_timing": {
                    "decodeMode": "shared-prefill-batch",
                    "sharedPrefill": True,
                    "branchCount": 4,
                    "plannedSeedIndexes": [0, 2, 3, 4],
                    "qualityReranked": True,
                    "contextDomainTermCount": 4,
                }
            },
        )

        metadata = rime_sidecar_module._model_prediction_decode_metadata([prediction])

        self.assertEqual(metadata["decodeMode"], "shared-prefill-batch")
        self.assertEqual(metadata["plannedSeedIndexes"], [0, 2, 3, 4])
        self.assertTrue(metadata["qualityReranked"])
        self.assertEqual(metadata["contextDomainTermCount"], 4)

    def test_empty_deferred_rag_lane_skips_memory_optimizer_work(self) -> None:
        class FailingOptimizerCore:
            def optimize_memory_candidates(self, *_args, **_kwargs):
                raise AssertionError("empty model-first lane must not run the memory optimizer")

        snapshot = rime_sidecar_module.parse_rime_context_payload(
            self._post_commit_payload(),
            default_project="wisdom-weasel-rag-ime",
        )
        suggestions, trace = optimize_suggestions_if_enabled(
            core=FailingOptimizerCore(),
            snapshot=snapshot,
            semantic_query="模型上下文",
            query_basis="committedContext",
            input_mode="post_commit_continuation",
            suggestions=[],
            top_k=3,
            latency_budget_ms=900,
            env={"RAG_IME_MEMORY_OPTIMIZER": "1"},
        )

        self.assertEqual(suggestions, [])
        self.assertEqual(trace["skippedReason"], "no_suggestions")
        self.assertEqual(trace["latencyMs"], 0.0)

    def _composition_payload(self, *, request_seq: int = 1) -> dict[str, object]:
        return {
            "sessionId": "v1-composition",
            "requestSeq": request_seq,
            "privacyDisposition": "allowed",
            "rawInput": "nihao",
            "preedit": "nihao",
            "rimeCandidates": [
                {"text": "你好", "label": "1", "comment": "Rime"},
                {"text": "拟好", "label": "2", "comment": "Rime"},
            ],
            "committedContext": "我刚才正在整理项目",
            "latencyBudgetMs": 900,
            "maxVisibleCandidates": 8,
            "maxSideCandidates": 5,
            "predictionFirstMerge": True,
            "frontendRevision": request_seq,
            "selectionEpoch": request_seq,
            "inputGeneration": request_seq,
            "frontAppBundleId": "com.apple.TextEdit",
            "inputSourceId": "im.rime.inputmethod.Squirrel.Hans",
        }

    def _post_commit_payload(
        self,
        *,
        request_seq: int = 1,
        context: str = "我想彻底整理项目，先把",
        commit_preview: str = "先把",
        progressive_follow_up: bool = False,
        foreground: bool = True,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "sessionId": "v1-post-commit",
            "requestSeq": request_seq,
            "privacyDisposition": "allowed",
            "rawInput": "",
            "preedit": "",
            "committedContext": context,
            "commitTextPreview": commit_preview,
            "latencyBudgetMs": 900,
            "maxVisibleCandidates": 8,
            "maxSideCandidates": 5,
            "predictionFirstMerge": True,
            "progressiveFollowUp": progressive_follow_up,
            "frontendRevision": 7,
            "selectionEpoch": 7,
            "inputGeneration": request_seq,
            "frontAppBundleId": "com.apple.TextEdit",
            "inputSourceId": "im.rime.inputmethod.Squirrel.Hans",
            "panelSessionId": "panel-v1",
        }
        if foreground:
            payload["foregroundText"] = {
                "available": True,
                "source": "text_input_client",
                "confidence": 0.78,
                "freshnessMs": 0,
                "selectedTextHash": "",
                "selectedTextChars": 0,
                "selectedTextPreview": "",
                "surroundingBefore": context,
                "surroundingAfter": "",
                "wholeValueHash": "",
                "wholeValueChars": 0,
                "canReplaceSelection": False,
                "captureEpoch": request_seq,
                "warnings": ["text_input_client_context"],
            }
        return payload

    def _response(
        self,
        payload: dict[str, object],
        *,
        core: CuratedMemoryCore | None = None,
        predictor: RecordingPredictionProvider | None = None,
    ) -> dict[str, object]:
        actual_core = core or CuratedMemoryCore()
        return build_rime_sidecar_response(
            payload=payload,
            adapter=self._adapter(actual_core),
            core=actual_core,
            predictor=predictor or RecordingPredictionProvider(),
        )

    def _prime_post_commit(
        self,
        *,
        core: CuratedMemoryCore | None = None,
        predictor: RecordingPredictionProvider | None = None,
        context: str = "我想彻底整理项目，先把",
        commit_preview: str = "先把",
    ) -> tuple[dict[str, object], dict[str, object], RecordingPredictionProvider, CuratedMemoryCore]:
        actual_core = core or CuratedMemoryCore()
        actual_predictor = predictor or RecordingPredictionProvider()
        first = self._response(
            self._post_commit_payload(context=context, commit_preview=commit_preview),
            core=actual_core,
            predictor=actual_predictor,
        )
        self.assertTrue(wait_for_model_prediction_lane_idle(timeout_s=2.0))
        follow_up = self._response(
            self._post_commit_payload(
                request_seq=2,
                context=context,
                commit_preview=commit_preview,
                progressive_follow_up=True,
            ),
            core=actual_core,
            predictor=actual_predictor,
        )
        return first, follow_up, actual_predictor, actual_core

    def test_v1_composition_model_disabled_by_default(self) -> None:
        predictor = RecordingPredictionProvider()
        response = self._response(self._composition_payload(), predictor=predictor)

        self.assertEqual(predictor.calls, 0)
        self.assertFalse(response["modelLane"]["called"])
        self.assertFalse(response["ragLane"]["called"])
        self.assertEqual(response["modelLane"]["skippedReason"], "skip: composition owned by rime; ai after commit only")
        self.assertEqual(response["ragLane"]["skippedReason"], "skip: composition owned by rime; ai after commit only")
        self.assertTrue(response["predictionSession"]["rimeCompositionOwnedByRime"])
        self.assertEqual(response["predictionSession"]["selectionScope"], "rime")
        self.assertEqual([item["sourceType"] for item in response["displayCandidates"]], ["rime", "rime"])
        self.assertEqual([item["sourceType"] for item in response["candidatePanel"]["candidates"]], ["rime", "rime"])
        self.assertFalse(response["assistantOverlay"]["visible"])
        self.assertEqual(response["assistantOverlay"]["dismissReason"], "composition_owned_by_rime")
        self.assertEqual(response["assistantOverlay"]["candidates"], [])
        self.assertEqual(response["keyPolicy"]["numberKeys"], "select_rime_candidate")

    def test_v1_accessibility_foreground_context_does_not_feed_side_lanes_during_composition(self) -> None:
        core = CuratedMemoryCore([_memory("fg:1", "真实前台文本相关候选", accepted_count=2)])
        payload = self._composition_payload(request_seq=11)
        payload["committedContext"] = "旧的输入法 ledger 不应该优先进入 side lane"
        payload["foregroundText"] = {
            "available": True,
            "source": "accessibility",
            "confidence": 0.86,
            "freshnessMs": 0,
            "selectedTextHash": "",
            "selectedTextChars": 0,
            "selectedTextPreview": "",
            "surroundingBefore": "真实前台文本 正在写输入法上下文",
            "surroundingAfter": "后面还有编辑器正文",
            "wholeValueHash": "sha256:foreground-whole-value",
            "wholeValueChars": 38,
            "canReplaceSelection": False,
            "captureEpoch": 11,
            "warnings": ["string_for_range"],
        }

        response = self._response(payload, core=core, predictor=RecordingPredictionProvider([]))
        foreground_context = response["ragLane"]["foregroundContext"]

        self.assertEqual(response["queryBasis"], "rimeCandidates")
        self.assertTrue(foreground_context["applied"])
        self.assertEqual(foreground_context["source"], "accessibility")
        self.assertEqual(foreground_context["wholeValueHash"], "sha256:foreground-whole-value")
        self.assertEqual(core.calls, 0)
        self.assertEqual(core.last_current_input, "")
        self.assertEqual(core.last_recent_context, "")
        self.assertFalse(response["ragLane"]["called"])
        self.assertFalse(response["modelLane"]["called"])
        self.assertEqual(response["triggerDecision"]["reason"], "skip: composition owned by rime; ai after commit only")
        self.assertEqual([item["sourceType"] for item in response["displayCandidates"]], ["rime", "rime"])
        self.assertEqual([item["sourceType"] for item in response["candidatePanel"]["candidates"]], ["rime", "rime"])
        self.assertFalse(response["assistantOverlay"]["visible"])
        self.assertEqual(response["assistantOverlay"]["dismissReason"], "composition_owned_by_rime")

    def test_v1_accessibility_foreground_context_can_drive_query_without_rime(self) -> None:
        core = CuratedMemoryCore([_memory("fg:2", "前台正文续写候选", accepted_count=2)])
        payload = self._post_commit_payload(request_seq=12, context="旧 ledger", commit_preview="")
        payload["progressiveFollowUp"] = True
        payload["forceSideCandidates"] = True
        payload["foregroundText"] = {
            "available": True,
            "source": "accessibility",
            "confidence": 0.86,
            "freshnessMs": 0,
            "surroundingBefore": "前台正文正在讨论 DeepSeek 生成输入法候选",
            "surroundingAfter": "",
            "wholeValueHash": "sha256:foreground-only",
            "wholeValueChars": 25,
            "canReplaceSelection": False,
            "captureEpoch": 12,
            "warnings": ["ax_value_fallback"],
        }

        response = self._response(payload, core=core, predictor=RecordingPredictionProvider([]))

        self.assertEqual(response["queryBasis"], "foregroundText")
        self.assertTrue(response["ragLane"]["foregroundContext"]["applied"])
        self.assertEqual(core.last_current_input, "前台正文正在讨论 DeepSeek 生成输入法候选")
        self.assertEqual(core.last_recent_context, "前台正文正在讨论 DeepSeek 生成输入法候选")

    def test_v1_text_input_client_context_drives_post_commit_query(self) -> None:
        core = CuratedMemoryCore([_memory("fg:3", "前台输入框续写候选", accepted_count=2)])
        payload = self._post_commit_payload(
            request_seq=13,
            context="旧 ledger 不应该进入模型",
            commit_preview="",
            foreground=False,
        )
        payload["progressiveFollowUp"] = True
        payload["forceSideCandidates"] = True
        payload["foregroundText"] = {
            "available": True,
            "source": "text_input_client",
            "confidence": 0.78,
            "freshnessMs": 0,
            "surroundingBefore": "真实输入框上下文来自 IMKTextInput",
            "surroundingAfter": "光标后的说明文字不能进入补全查询",
            "wholeValueHash": "",
            "wholeValueChars": 0,
            "canReplaceSelection": False,
            "captureEpoch": 13,
            "warnings": ["text_input_client_context"],
        }

        response = self._response(payload, core=core, predictor=RecordingPredictionProvider([]))

        self.assertEqual(response["queryBasis"], "foregroundText")
        self.assertTrue(response["triggerDecision"]["foregroundContextGate"]["allowed"])
        self.assertEqual(response["ragLane"]["foregroundContext"]["source"], "text_input_client")
        self.assertEqual(
            response["ragLane"]["foregroundContext"]["semanticContextMode"],
            "before_caret_and_selection",
        )
        self.assertEqual(
            response["ragLane"]["foregroundContext"]["excludedAfterChars"],
            len("光标后的说明文字不能进入补全查询"),
        )
        self.assertEqual(core.last_current_input, "真实输入框上下文来自 IMKTextInput")
        self.assertEqual(core.last_recent_context, "真实输入框上下文来自 IMKTextInput")

    def test_v1_word_commit_preview_uses_complete_foreground_context(self) -> None:
        predictor = RecordingPredictionProvider(["继续把候选栏稳定下来"])
        core = CuratedMemoryCore()
        first = self._response(
            self._post_commit_payload(
                request_seq=15,
                context="这个输入法目前最影响体验的是弹窗一直不出现，而且",
                commit_preview="而且",
            ),
            core=core,
            predictor=predictor,
        )

        self.assertEqual(first["queryBasis"], "foregroundText")
        self.assertEqual(first["semanticQuery"], "这个输入法目前最影响体验的是弹窗一直不出现，而且")
        self.assertTrue(first["triggerDecision"]["shouldRefresh"])
        self.assertTrue(first["modelLane"]["called"])
        self.assertTrue(first["progressive"]["shouldFollowUp"])
        self.assertTrue(wait_for_model_prediction_lane_idle(timeout_s=2.0))

        follow_up = self._response(
            self._post_commit_payload(
                request_seq=16,
                context="这个输入法目前最影响体验的是弹窗一直不出现，而且",
                commit_preview="而且",
                progressive_follow_up=True,
            ),
            core=core,
            predictor=predictor,
        )

        self.assertEqual(follow_up["queryBasis"], "foregroundText")
        self.assertEqual([item["text"] for item in follow_up["modelPredictions"]], ["继续把候选栏稳定下来"])
        self.assertIn("继续把候选栏稳定下来", [item["text"] for item in follow_up["displayCandidates"]])

    def test_v1_post_commit_without_reliable_foreground_context_skips_side_lanes(self) -> None:
        predictor = RecordingPredictionProvider()
        core = CuratedMemoryCore([_memory("stale:1", "旧上下文候选", accepted_count=3)])
        payload = self._post_commit_payload(
            request_seq=14,
            context="旧 ledger 不可信",
            commit_preview="",
            foreground=False,
        )
        payload["forceSideCandidates"] = True
        payload["foregroundText"] = {
            "available": True,
            "source": "ime_commit_ledger",
            "confidence": 0.65,
            "freshnessMs": 0,
            "surroundingBefore": "旧 ledger 不可信",
            "surroundingAfter": "",
            "wholeValueHash": "",
            "wholeValueChars": 0,
            "canReplaceSelection": False,
            "captureEpoch": 14,
            "warnings": [],
        }

        response = self._response(payload, core=core, predictor=predictor)

        self.assertEqual(predictor.calls, 0)
        self.assertEqual(core.calls, 0)
        self.assertFalse(response["triggerDecision"]["shouldRefresh"])
        self.assertFalse(response["triggerDecision"]["foregroundContextGate"]["allowed"])
        self.assertIn("reliable foreground context required", response["triggerDecision"]["reason"])

    def test_v1_progressive_follow_up_keeps_same_transaction_context_long_enough_to_poll(self) -> None:
        payload = self._post_commit_payload(request_seq=18, progressive_follow_up=True)
        foreground = payload["foregroundText"]
        self.assertIsInstance(foreground, dict)
        foreground["freshnessMs"] = 1200

        follow_up = self._response(payload, predictor=RecordingPredictionProvider([]))
        self.assertTrue(follow_up["triggerDecision"]["foregroundContextGate"]["allowed"])

        initial = self._post_commit_payload(request_seq=19)
        initial_foreground = initial["foregroundText"]
        self.assertIsInstance(initial_foreground, dict)
        initial_foreground["freshnessMs"] = 1200
        rejected = self._response(initial, predictor=RecordingPredictionProvider([]))
        self.assertFalse(rejected["triggerDecision"]["foregroundContextGate"]["allowed"])
        self.assertEqual(rejected["triggerDecision"]["foregroundContextGate"]["foregroundReason"], "foreground context stale")

    def test_v1_foreground_capture_epoch_mismatch_fails_closed(self) -> None:
        predictor = RecordingPredictionProvider()
        payload = self._post_commit_payload(request_seq=21)
        foreground = payload["foregroundText"]
        self.assertIsInstance(foreground, dict)
        foreground["captureEpoch"] = 20

        response = self._response(payload, predictor=predictor)

        self.assertEqual(predictor.calls, 0)
        gate = response["triggerDecision"]["foregroundContextGate"]
        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["foregroundReason"], "capture epoch mismatch")
        self.assertFalse(response["modelLane"]["called"])
        self.assertFalse(response["ragLane"]["called"])

    def test_v1_sensitive_field_fails_closed_before_storage_rag_or_model(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-sensitive-sidecar-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "rag-ime.sqlite")
            core.initialize()
            predictor = RecordingPredictionProvider(["绝不能调用模型"])
            adapter = InputMethodAdapter(core, project="wisdom-weasel-rag-ime")
            secret = "hunter2@example.com"
            payload = self._post_commit_payload(
                request_seq=29,
                context=f"账号 {secret}",
                commit_preview=secret,
            )
            payload.update(
                {
                    "secureInput": True,
                    "sensitiveField": False,
                    "rawInput": secret,
                    "preedit": secret,
                    "commitBurstReady": True,
                    "commitBurstDeltaChars": len(secret),
                    "commitBurstTexts": [secret],
                    "compositionHash": "sha256:secret-composition",
                    "committedContextHash": "sha256:secret-context",
                }
            )
            payload["foregroundText"].update(
                {
                    "surroundingBefore": f"账号 {secret}",
                    "contextGroupId": "doc:secure-field",
                    "commitTextMatched": True,
                }
            )
            rag_calls = 0
            original_suggest = core.suggest_for_input

            def counted_suggest(**kwargs):
                nonlocal rag_calls
                rag_calls += 1
                return original_suggest(**kwargs)

            core.suggest_for_input = counted_suggest  # type: ignore[method-assign]
            events_before = core.event_count()
            actions_before = core.action_count()
            buffer_before = list(rime_sidecar_module._GROUP_SHORT_BUFFER.recent("doc:secure-field", limit=10))

            response = build_rime_sidecar_response(
                payload=payload,
                adapter=adapter,
                core=core,
                predictor=predictor,
            )

            self.assertEqual(core.event_count(), events_before)
            self.assertEqual(core.action_count(), actions_before)
            self.assertEqual(rag_calls, 0)
            self.assertEqual(predictor.calls, 0)
            self.assertEqual(
                list(rime_sidecar_module._GROUP_SHORT_BUFFER.recent("doc:secure-field", limit=10)),
                buffer_before,
            )
            self.assertEqual(response["displayCandidates"], [])
            self.assertEqual(response["modelPredictions"], [])
            self.assertEqual(response["ragCandidates"], [])
            self.assertTrue(response["predictionSession"]["shouldClearPredictionPanel"])
            self.assertEqual(response["predictionSession"]["clearReason"], "sensitive_field")
            self.assertEqual(response["predictionSession"]["pinyinPrefix"], "")
            self.assertTrue(response["triggerDecision"]["hardClear"])
            self.assertFalse(response["assistantOverlay"]["visible"])
            self.assertEqual(response["assistantOverlay"]["dismissReason"], "sensitive_field")
            self.assertFalse(response["ragLane"]["called"])
            self.assertFalse(response["modelLane"]["called"])
            self.assertEqual(
                response["predictionTraceEvents"],
                [{"event": "prediction_sensitive_field_blocked", "fields": {"reason": "sensitive_field"}}],
            )
            trace_json = json.dumps(response["predictionTraceEvents"], ensure_ascii=False)
            self.assertNotIn(secret, trace_json)
            self.assertNotIn("sha256", trace_json)
            self.assertEqual(response["rawInput"], "")
            self.assertEqual(response["preedit"], "")
            self.assertEqual(response["committedContext"], "")
            self.assertEqual(response["frontendTransaction"]["compositionHash"], "")
            self.assertEqual(response["frontendTransaction"]["committedContextHash"], "")
            self.assertEqual(response["frontendTransaction"]["frontAppBundleId"], "")
            self.assertEqual(response["frontendTransaction"]["inputSourceId"], "")

    def test_missing_privacy_disposition_fails_closed_before_hash_buffer_storage_or_model(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-unknown-privacy-sidecar-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "rag-ime.sqlite")
            core.initialize()
            predictor = RecordingPredictionProvider(["绝不能调用模型"])
            adapter = InputMethodAdapter(core, project="wisdom-weasel-rag-ime")
            payload = self._post_commit_payload(
                request_seq=30,
                context="旧客户端提交上下文",
                commit_preview="提交上下文",
            )
            payload.pop("privacyDisposition")
            payload.update(
                {
                    "commitBurstReady": True,
                    "commitBurstDeltaChars": 6,
                    "commitBurstTexts": ["旧客户端提交上下文"],
                }
            )
            payload["foregroundText"].update(
                {
                    "contextGroupId": "doc:unknown-privacy",
                    "contextGroupLevel": "document",
                    "commitTextMatched": True,
                }
            )
            buffer_before = list(
                rime_sidecar_module._GROUP_SHORT_BUFFER.recent("doc:unknown-privacy", limit=10)
            )

            response = build_rime_sidecar_response(
                payload=payload,
                adapter=adapter,
                core=core,
                predictor=predictor,
            )

            self.assertEqual(core.event_count(), 0)
            self.assertEqual(predictor.calls, 0)
            self.assertEqual(
                list(rime_sidecar_module._GROUP_SHORT_BUFFER.recent("doc:unknown-privacy", limit=10)),
                buffer_before,
            )
            self.assertTrue(response["noStore"])
            self.assertFalse(response["stored"])
            self.assertEqual(response["privacyAssessment"]["disposition"], "unknown")
            self.assertEqual(response["storageReceipt"]["outcome"], "no_store")
            self.assertEqual(response["predictionSession"]["clearReason"], "privacy_unknown")
            self.assertEqual(response["frontendTransaction"]["compositionHash"], "")
            self.assertEqual(response["frontendTransaction"]["committedContextHash"], "")

    def test_structured_password_field_metadata_blocks_before_text_inspection(self) -> None:
        secret = "ordinary-looking-value-123"
        predictor = RecordingPredictionProvider(["不应调用"])
        payload = self._post_commit_payload(context=secret, commit_preview=secret)
        payload.update(
            {
                "fieldType": "password",
                "secureInput": False,
                "sensitiveField": False,
                "rawInput": secret,
                "preedit": secret,
            }
        )

        response = self._response(payload, predictor=predictor)

        self.assertEqual(predictor.calls, 0)
        self.assertEqual(response["predictionSession"]["clearReason"], "sensitive_field")
        self.assertEqual(response["frontendTransaction"]["compositionHash"], "")
        self.assertEqual(response["frontendTransaction"]["committedContextHash"], "")
        self.assertNotIn(secret, json.dumps(response, ensure_ascii=False))

    def test_password_discussion_text_is_not_misclassified_without_field_metadata(self) -> None:
        payload = self._post_commit_payload(
            context="这里讨论密码策略和 API key 轮换",
            commit_preview="轮换",
        )

        response = self._response(payload, predictor=RecordingPredictionProvider(["方案"]))

        self.assertNotEqual(response["predictionSession"]["clearReason"], "sensitive_field")

    def test_non_password_browser_field_metadata_does_not_disable_ai(self) -> None:
        for field_type in ("username", "email", "login", "one-time-code", "otp"):
            with self.subTest(field_type=field_type):
                payload = self._post_commit_payload(context="普通输入", commit_preview="输入")
                payload["fieldType"] = field_type
                response = self._response(payload, predictor=RecordingPredictionProvider(["继续"]))

                self.assertNotEqual(response["predictionSession"]["clearReason"], "sensitive_field")

    def test_v1_post_commit_async_first_response_under_250ms(self) -> None:
        predictor = RecordingPredictionProvider(sleep_s=0.35)
        started = time.perf_counter()
        response = self._response(self._post_commit_payload(), predictor=predictor)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        self.assertLess(elapsed_ms, 250)
        self.assertEqual(predictor.calls, 1)
        self.assertEqual(response["modelLane"]["requestType"], "post_commit_completion")
        self.assertTrue(response["modelLane"]["asyncMode"])
        self.assertTrue(response["modelLane"]["pending"])
        self.assertEqual(response["modelLane"]["firstResponseMs"], 150)
        self.assertTrue(response["progressive"]["shouldFollowUp"])
        self.assertFalse(response["predictionSession"]["shouldClearPredictionPanel"])
        self.assertEqual(response["candidatePanel"]["candidates"], [])
        self.assertTrue(response["assistantOverlay"]["visible"])
        self.assertEqual(response["assistantOverlay"]["phase"], "post_commit")
        self.assertEqual(response["assistantOverlay"]["animation"]["kind"], "none")
        self.assertEqual(response["assistantOverlay"]["dismissReason"], "")
        self.assertTrue(
            any(
                item["selectionAction"] == "start_active_rag_from_context"
                for item in response["assistantOverlay"]["candidates"]
            )
        )

    def test_v1_fast_local_model_returns_three_candidates_in_first_response(self) -> None:
        predictor = RecordingPredictionProvider(["结果", "速度", "方式"], sleep_s=0.03)
        started = time.perf_counter()
        # Keep response-shaping deterministic; the slow-provider test above
        # retains the real asynchronous thread and deadline path.
        with patch.object(rime_sidecar_module, "Thread", InlinePredictionThread):
            response = self._response(self._post_commit_payload(), predictor=predictor)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        self.assertLess(elapsed_ms, 220)
        self.assertEqual(predictor.calls, 1)
        self.assertFalse(response["modelLane"]["pending"])
        self.assertEqual(response["modelLane"]["predictionCount"], 3)
        self.assertEqual([item["text"] for item in response["modelPredictions"]], ["结果", "速度", "方式"])
        model_cards = [item for item in response["displayCandidates"] if item["sourceType"] == "model"]
        self.assertEqual([item["text"] for item in model_cards], ["结果", "速度", "方式"])
        self.assertEqual(response["keyPolicy"]["tab"], "accept_top_prediction")
        self.assertEqual(response["progressive"]["retryAfterMs"], 60)

    def test_v1_model_first_response_defers_strong_memory_to_progressive_rag(self) -> None:
        predictor = RecordingPredictionProvider(["先显示本地模型"])
        core = CuratedMemoryCore([_memory("phrase:front", "完成前台闭环", accepted_count=3)])
        payload = self._post_commit_payload(
            context="这个方案正在验证真实前台上下文",
            commit_preview="前台上下文",
        )
        payload.update({"commitBurstReady": True, "commitBurstDeltaChars": 6, "commitBurstTexts": []})
        payload["foregroundText"].update(
            {
                "contextGroupId": "doc:t0",
                "contextGroupLevel": "document",
                "contextGroupConfidence": 1.0,
                "commitTextMatched": True,
            }
        )

        response = self._response(payload, core=core, predictor=predictor)

        self.assertEqual(predictor.calls, 1)
        self.assertTrue(response["ragLane"]["pending"])
        self.assertTrue(response["ragLane"]["modelFirstDeferred"])
        self.assertEqual(response["modelPredictions"][0]["providerName"], "v1-contract-model")
        self.assertEqual(response["modelPredictions"][0]["text"], "先显示本地模型")
        self.assertTrue(response["progressive"]["shouldFollowUp"])
        self.assertTrue(response["predictionTrigger"]["providerCallAllowed"])
        trace_names = [item["event"] for item in response["predictionTraceEvents"]]
        self.assertIn("post_commit_completion_first_response_returned", trace_names)

    def test_v1_progressive_follow_up_waits_for_configured_rag_lane(self) -> None:
        class SlowMemoryCore(CuratedMemoryCore):
            def suggest_for_input(self, **kwargs: object) -> list[InputSuggestion]:
                time.sleep(0.18)
                return super().suggest_for_input(**kwargs)

        core = SlowMemoryCore([_memory("phrase:progressive", "渐进检索结果")])
        payload = self._post_commit_payload(request_seq=2, progressive_follow_up=True)
        snapshot = rime_sidecar_module.parse_rime_context_payload(
            payload,
            default_project="wisdom-weasel-rag-ime",
        )
        runtime_config = SimpleNamespace(
            hybrid_rag=SimpleNamespace(enabled=True, budget_ms=400),
            memory=SimpleNamespace(enabled=True),
        )

        started = time.perf_counter()
        with patch.dict(os.environ, {"RAG_IME_ENABLE_POST_COMMIT_AUTO_MODEL": "0"}):
            suggestions, rag_lane, _, _, _ = rime_sidecar_module.run_side_lanes_with_latency_budget(
                adapter=InputMethodAdapter(core),
                core=core,
                predictor=RecordingPredictionProvider([]),
                snapshot=snapshot,
                current_input="先把",
                query_basis="commit_text_preview",
                recent_context=snapshot.committed_context,
                explicit_recent_context=snapshot.committed_context,
                project=snapshot.project,
                app=snapshot.app,
                top_k=3,
                max_candidates=3,
                latency_budget_ms=snapshot.latency_budget_ms,
                runtime_config=runtime_config,
            )
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        self.assertGreaterEqual(elapsed_ms, 150)
        self.assertLess(elapsed_ms, 500)
        self.assertEqual([item.surface_text for item in suggestions], ["渐进检索结果"])
        self.assertFalse(rag_lane["timedOut"])
        self.assertFalse(rag_lane.get("pending", False))
        self.assertEqual(rag_lane["latencyBudgetMs"], 400)

    def test_v1_rag_lane_budget_is_capped_by_effective_runtime_config(self) -> None:
        runtime_config = SimpleNamespace(hybrid_rag=SimpleNamespace(budget_ms=400))

        self.assertEqual(
            rime_sidecar_module._rag_lane_budget_for_request(900, runtime_config=runtime_config),
            400,
        )
        self.assertEqual(
            rime_sidecar_module._rag_lane_budget_for_request(300, runtime_config=runtime_config),
            300,
        )

    def test_v1_commit_burst_gate_keeps_fragments_out_of_persistent_memory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-burst-sidecar-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "rag-ime.sqlite")
            core.initialize()
            adapter = InputMethodAdapter(core, project="wisdom-weasel-rag-ime")
            predictor = RecordingPredictionProvider()
            payload = self._post_commit_payload(
                context="这个方案主要解决前台上下文问题",
                commit_preview="上下文问题",
            )
            payload.update(
                {
                    "commitBurstReady": False,
                    "commitBurstDeltaChars": 10,
                    "commitBurstTexts": ["这个方案主要", "解决前台上下文问题"],
                }
            )
            payload["foregroundText"].update(
                {
                    "contextGroupId": "doc:test",
                    "contextGroupLevel": "document",
                    "contextGroupConfidence": 1.0,
                    "commitTextMatched": True,
                }
            )

            waiting = build_rime_sidecar_response(payload=payload, adapter=adapter, core=core, predictor=predictor)
            self.assertFalse(waiting["triggerDecision"]["shouldRefresh"])
            self.assertEqual(waiting["predictionTrigger"]["reason"], "waiting_idle")
            self.assertEqual(core.event_count(), 0)

            payload["commitBurstReady"] = True
            fired = build_rime_sidecar_response(payload=payload, adapter=adapter, core=core, predictor=predictor)
            self.assertTrue(fired["predictionTrigger"]["providerCallAllowed"])
            self.assertEqual(core.event_count(), 0)
            self.assertEqual(fired["predictionTrigger"]["recordedEventIds"], [])
            self.assertEqual(fired["predictionTrigger"]["rawFragmentPersistence"], "disabled")

            duplicate = build_rime_sidecar_response(payload=payload, adapter=adapter, core=core, predictor=predictor)
            self.assertEqual(duplicate["predictionTrigger"]["reason"], "duplicate_context_hash")
            self.assertEqual(core.event_count(), 0)

    def test_v1_commit_burst_fallback_group_uses_full_short_digest(self) -> None:
        payload = self._post_commit_payload(request_seq=20)
        payload.update({"commitBurstReady": False, "commitBurstDeltaChars": 6})

        response = self._response(payload, predictor=RecordingPredictionProvider([]))

        digest = hashlib.sha256("com.apple.TextEdit".encode()).hexdigest()[:16]
        self.assertEqual(response["predictionTrigger"]["groupId"], f"app:{digest}")
        self.assertNotIn("sha256:", response["predictionTrigger"]["groupId"])

    def test_v1_tab_continuation_bypasses_normal_commit_rate_limit(self) -> None:
        predictor = RecordingPredictionProvider(["结果", "速度", "方式"])
        core = CuratedMemoryCore([])
        for index in range(2):
            payload = self._post_commit_payload(
                request_seq=30 + index,
                context=f"普通提交上下文第{index}次已经完成",
                commit_preview=f"第{index}次",
            )
            payload.update(
                {
                    "commitBurstReady": True,
                    "commitBurstDeltaChars": 6,
                    "commitBurstTexts": [],
                }
            )
            payload["foregroundText"].update({"contextGroupId": "doc:tab-chain", "commitTextMatched": True})
            response = self._response(payload, core=core, predictor=predictor)
            self.assertTrue(response["predictionTrigger"]["providerCallAllowed"])

        accepted = self._post_commit_payload(
            request_seq=32,
            context="普通提交上下文之后按下Tab接受首候选",
            commit_preview="首候选",
        )
        accepted.update(
            {
                "commitBurstReady": True,
                "acceptedCandidateContinuation": True,
                "commitBurstDeltaChars": 3,
                "commitBurstTexts": [],
            }
        )
        accepted["foregroundText"].update({"contextGroupId": "doc:tab-chain", "commitTextMatched": True})
        continued = self._response(accepted, core=core, predictor=predictor)

        self.assertTrue(continued["predictionTrigger"]["acceptedCandidate"])
        self.assertTrue(continued["predictionTrigger"]["providerCallAllowed"])
        self.assertNotEqual(continued["predictionTrigger"]["reason"], "max_calls_per_10s")
        self.assertEqual([item["text"] for item in continued["modelPredictions"]], ["结果", "速度", "方式"])

    def test_v1_post_commit_followup_does_not_restart_provider(self) -> None:
        predictor = RecordingPredictionProvider(sleep_s=0.4)
        core = CuratedMemoryCore([])
        first = self._response(self._post_commit_payload(), core=core, predictor=predictor)
        follow_up = self._response(
            self._post_commit_payload(request_seq=2, progressive_follow_up=True),
            core=core,
            predictor=predictor,
        )

        self.assertEqual(predictor.calls, 1)
        self.assertTrue(first["modelLane"]["pending"])
        self.assertIn(follow_up["modelLane"]["completionJobState"], {"pending", "hit"})
        self.assertEqual(follow_up["modelLane"]["requestType"], "post_commit_completion")
        self.assertEqual(follow_up["progressive"]["retryAfterMs"], 60)

    def test_v1_post_commit_completion_cache_respects_memory_bound(self) -> None:
        predictor = RecordingPredictionProvider(sleep_s=0.01)
        last_response: dict[str, object] = {}
        with patch.dict(os.environ, {"RAG_IME_POST_COMMIT_COMPLETION_CACHE_MAX_JOBS": "2"}):
            for index in range(3):
                last_response = self._response(
                    self._post_commit_payload(
                        request_seq=20 + index,
                        context=f"我想整理第 {index} 段真实前台上下文",
                        commit_preview=f"第{index}段",
                    ),
                    predictor=predictor,
                )

        model_lane = last_response["modelLane"]
        self.assertEqual(model_lane["completionCacheMaxSize"], 2)
        self.assertLessEqual(model_lane["completionCacheSize"], 2)

    def test_v1_post_commit_model_candidates_not_pinyin_filtered(self) -> None:
        _, follow_up, predictor, _ = self._prime_post_commit(
            predictor=RecordingPredictionProvider(["真实输入链路跑通", "v1 范围收缩"])
        )

        self.assertEqual(predictor.requests[0]["current_input"], "")
        self.assertEqual(predictor.requests[0]["request_type"], "post_commit_completion")
        self.assertEqual(follow_up["modelLane"]["completionJobState"], "hit")
        self.assertTrue(follow_up["modelLane"]["noPinyinFilter"])
        self.assertEqual([item["text"] for item in follow_up["modelPredictions"]], ["真实输入链路跑通", "v1 范围收缩"])
        self.assertEqual(follow_up["modelLane"]["filteredPredictionCount"], 0)

    def test_v1_rag_does_not_echo_current_context(self) -> None:
        core = CuratedMemoryCore(
            [
                _memory("echo:1", "我想彻底整理项目，先把", source_type="rag"),
                _memory("good:1", "真实输入链路跑通", accepted_count=2),
            ]
        )
        _, follow_up, _, actual_core = self._prime_post_commit(core=core)
        surfaces = [item["text"] for item in follow_up["displayCandidates"]]
        rag_surfaces = [item["surfaceText"] for item in follow_up["ragCandidates"]]

        self.assertEqual(actual_core.last_current_input, "我想彻底整理项目，先把")
        self.assertNotIn("我想彻底整理项目，先把", surfaces)
        self.assertNotIn("我想彻底整理项目，先把", rag_surfaces)
        self.assertIn("真实输入链路跑通", rag_surfaces)
        self.assertTrue(follow_up["ragLane"]["directDisplaySuppressed"])
        self.assertEqual(follow_up["ragLane"]["displaySuggestionCount"], 0)
        self.assertGreaterEqual(follow_up["ragLane"]["filteredSuggestionCount"], 1)
        source_cards = follow_up["assistantOverlay"]["sourceCards"]
        self.assertTrue(any(card["title"] == "真实输入链路跑通" for card in source_cards))

    def test_v1_rag_prompt_leaks_filtered(self) -> None:
        core = CuratedMemoryCore(
            [
                _memory("leak:1", "019f1228-34de-74b3-a627-c546f091e87e 这个会话继续调试", source_type="rag"),
                _memory("leak:2", '{"sessionId":"debug","requestSeq":42}', source_type="rag"),
                _memory("good:1", "候选质量验收", accepted_count=2),
            ]
        )
        _, follow_up, _, _ = self._prime_post_commit(core=core)
        surfaces = [item["text"] for item in follow_up["displayCandidates"]]
        rag_surfaces = [item["surfaceText"] for item in follow_up["ragCandidates"]]

        self.assertFalse(any("019f1228" in item for item in surfaces))
        self.assertFalse(any("sessionId" in item for item in surfaces))
        self.assertFalse(any("019f1228" in item for item in rag_surfaces))
        self.assertFalse(any("sessionId" in item for item in rag_surfaces))
        self.assertIn("候选质量验收", rag_surfaces)
        self.assertTrue(follow_up["ragLane"]["directDisplaySuppressed"])
        self.assertGreaterEqual(follow_up["ragLane"]["filteredSuggestionCount"], 2)

    def test_v1_timeout_does_not_clear_legal_panel(self) -> None:
        response = self._response(
            self._post_commit_payload(),
            predictor=RecordingPredictionProvider(sleep_s=0.4),
        )

        self.assertEqual(response["uiMode"], "post_commit_pending")
        self.assertEqual(response["displayCandidates"][0]["sourceType"], "status")
        self.assertFalse(response["predictionSession"]["shouldClearPredictionPanel"])
        self.assertFalse(response["showDecision"]["hardClear"])
        self.assertTrue(response["progressive"]["shouldFollowUp"])

    def test_v1_post_commit_panel_can_disable_active_rag_action(self) -> None:
        with patch.dict(os.environ, {"RAG_IME_POST_COMMIT_ACTIVE_RAG_BUTTON": "0"}):
            response = self._response(
                self._post_commit_payload(),
                predictor=RecordingPredictionProvider(sleep_s=0.4),
            )
        action_candidates = [item for item in response["displayCandidates"] if item["sourceType"] == "action"]

        self.assertEqual(response["uiMode"], "post_commit_pending")
        self.assertEqual(action_candidates, [])

    def test_v1_post_commit_panel_exposes_active_rag_action_button_when_opted_in(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RAG_IME_POST_COMMIT_ACTIVE_RAG_BUTTON": "1",
                "RAG_IME_ASSISTANT_OVERLAY_AUTO_PENDING": "1",
            },
        ):
            response = self._response(
                self._post_commit_payload(),
                predictor=RecordingPredictionProvider(sleep_s=0.4),
            )
        action_candidates = [item for item in response["displayCandidates"] if item["sourceType"] == "action"]

        self.assertEqual(response["uiMode"], "post_commit_pending")
        self.assertEqual([item["text"] for item in action_candidates], ["快速生成", "深度查找"])
        self.assertEqual(
            [item["selectionAction"] for item in action_candidates],
            [
                "start_active_rag_from_context",
                "start_agent_deep_search_from_context",
            ],
        )
        for item in action_candidates:
            self.assertEqual(item["label"], "")
            self.assertEqual(item["visibleLabel"], "")
            self.assertIsNone(item["selectionKey"])
            self.assertEqual(item["selectionRank"], 0)
            self.assertEqual(item["candidateOrdinal"], 0)
            self.assertTrue(item["isSelectable"])
            self.assertEqual(item["colorToken"], "modelBlue")
            self.assertEqual(item["metadata"]["maxCandidates"], 1)
            self.assertTrue(item["metadata"]["numericSelectionDisabled"])
            self.assertEqual(item["metadata"]["triggerPolicy"], "manual_only")
            self.assertTrue(item["metadata"]["requiresExplicitSelection"])
        self.assertEqual(action_candidates[0]["metadata"]["buttonRole"], "active_rag_generate")
        self.assertEqual(action_candidates[0]["metadata"]["shortcutHint"], "ctrl+.")
        self.assertFalse(action_candidates[0]["metadata"]["requiresScreenshot"])
        self.assertEqual(action_candidates[1]["metadata"]["buttonRole"], "agent_deep_search")
        self.assertTrue(action_candidates[1]["metadata"]["requiresPi"])
        self.assertFalse(action_candidates[1]["metadata"]["requiresScreenshot"])
        overlay_candidates = response["assistantOverlay"]["candidates"]
        self.assertEqual(response["candidatePanel"]["candidates"], [])
        self.assertTrue(any(item["sourceType"] == "action" for item in overlay_candidates))
        self.assertTrue(all(item["sourceType"] != "status" for item in overlay_candidates))

    def test_v1_post_commit_auto_model_can_be_action_only(self) -> None:
        predictor = RecordingPredictionProvider(["不应该自动调用"])
        with patch.dict(
            os.environ,
            {
                "RAG_IME_ENABLE_POST_COMMIT_AUTO_MODEL": "0",
                "RAG_IME_POST_COMMIT_ACTIVE_RAG_BUTTON": "1",
            },
        ):
            response = self._response(self._post_commit_payload(), predictor=predictor)
        action_candidates = [item for item in response["displayCandidates"] if item["sourceType"] == "action"]

        self.assertEqual(predictor.calls, 0)
        self.assertTrue(action_candidates)
        self.assertFalse([item for item in response["displayCandidates"] if item["sourceType"] == "model"])
        self.assertEqual(response["predictionSession"]["phase"], "post_commit")
        self.assertFalse(response["predictionSession"]["shouldClearPredictionPanel"])
        self.assertIn("action-only fast path", response["modelLane"]["skippedReason"])

    def test_v1_post_commit_action_button_fast_path_skips_rag_and_model(self) -> None:
        predictor = RecordingPredictionProvider(["不应该自动调用"], sleep_s=0.2)
        core = CuratedMemoryCore([_memory("m-fast", "不应该进入普通候选")])
        with patch.dict(
            os.environ,
            {
                "RAG_IME_ENABLE_POST_COMMIT_AUTO_MODEL": "0",
                "RAG_IME_RAG_DIRECT_DISPLAY": "0",
                "RAG_IME_POST_COMMIT_ACTIVE_RAG_BUTTON": "1",
            },
        ):
            response = self._response(self._post_commit_payload(), core=core, predictor=predictor)
        action_candidates = [item for item in response["displayCandidates"] if item["sourceType"] == "action"]

        self.assertEqual(core.calls, 0)
        self.assertEqual(predictor.calls, 0)
        self.assertEqual([item["text"] for item in action_candidates], ["快速生成", "深度查找"])
        for item in action_candidates:
            self.assertEqual(item["label"], "")
            self.assertIsNone(item["selectionKey"])
            self.assertEqual(item["candidateOrdinal"], 0)
            self.assertTrue(item["isSelectable"])
        self.assertIn("action-only fast path", response["ragLane"]["skippedReason"])
        self.assertIn("action-only fast path", response["modelLane"]["skippedReason"])
        self.assertFalse(response["predictionFirst"]["enabled"])

    def test_v1_post_commit_auto_model_defaults_to_visible_llm_lane(self) -> None:
        predictor = RecordingPredictionProvider(["自动LLM候选"])
        with patch.dict(os.environ, {}, clear=True):
            first = self._response(self._post_commit_payload(), predictor=predictor)
            self.assertTrue(wait_for_model_prediction_lane_idle(timeout_s=2.0))
            follow_up = self._response(
                self._post_commit_payload(request_seq=2, progressive_follow_up=True),
                predictor=predictor,
            )
        model_candidates = [item for item in follow_up["displayCandidates"] if item["sourceType"] == "model"]

        self.assertTrue(first["progressive"]["shouldFollowUp"])
        self.assertTrue(model_candidates)
        self.assertTrue("自动LLM候选".startswith(model_candidates[0]["text"]))
        self.assertEqual(follow_up["uiMode"], "post_commit_prediction")

    def test_v1_post_commit_actions_do_not_consume_numbered_candidate_slots(self) -> None:
        payload = self._post_commit_payload()
        payload["maxVisibleCandidates"] = 1
        with (
            patch.dict(os.environ, {"RAG_IME_POST_COMMIT_ACTIVE_RAG_BUTTON": "1"}),
            patch.object(rime_sidecar_module, "Thread", InlinePredictionThread),
        ):
            response = self._response(payload, predictor=RecordingPredictionProvider(["继续完善"]))

        numbered = [item for item in response["displayCandidates"] if item["candidateOrdinal"] > 0]
        actions = [item for item in response["displayCandidates"] if item["sourceType"] == "action"]
        self.assertEqual(len(numbered), 1)
        self.assertEqual(numbered[0]["selectionAction"], "commit_side_candidate")
        self.assertEqual([item["candidateOrdinal"] for item in actions], [0, 0])

    def test_v1_empty_post_commit_followup_does_not_emit_demo_fallback_by_default(self) -> None:
        first, follow_up, _, _ = self._prime_post_commit(
            core=CuratedMemoryCore(),
            predictor=RecordingPredictionProvider(["我的需求你没有完成"]),
            context="我的需求你没有完成",
            commit_preview="完成",
        )
        fallback_candidates = [item for item in follow_up["displayCandidates"] if item["sourceType"] == "model"]

        self.assertEqual(first["uiMode"], "post_commit_pending")
        self.assertIn(follow_up["uiMode"], {"post_commit_pending", "post_commit_prediction"})
        self.assertFalse(fallback_candidates)
        self.assertNotEqual(follow_up["modelLane"].get("fallbackReason"), "demo_safe_empty_post_commit_fallback")

    def test_v1_empty_post_commit_followup_gets_demo_safe_fallback_candidate_when_opted_in(self) -> None:
        with patch.dict(os.environ, {"RAG_IME_ENABLE_DEMO_SAFE_FALLBACK": "1"}):
            first, follow_up, _, _ = self._prime_post_commit(
                core=CuratedMemoryCore(),
                predictor=RecordingPredictionProvider(["我的需求你没有完成"]),
                context="我的需求你没有完成",
                commit_preview="完成",
            )
        fallback_candidates = [item for item in follow_up["displayCandidates"] if item["sourceType"] == "model"]

        self.assertEqual(first["uiMode"], "post_commit_pending")
        self.assertEqual(follow_up["uiMode"], "post_commit_prediction")
        self.assertTrue(fallback_candidates)
        self.assertEqual(fallback_candidates[0]["text"], "继续补齐需求")
        self.assertEqual(fallback_candidates[0]["selectionAction"], "commit_side_candidate")
        self.assertEqual(follow_up["modelLane"]["fallbackReason"], "demo_safe_empty_post_commit_fallback")
        self.assertEqual(follow_up["modelLane"]["fallbackCandidateCount"], 1)
        self.assertEqual(follow_up["modelPredictions"][0]["metadata"]["fallbackSource"], "post_commit_empty_result")

    def test_v1_truncated_post_commit_model_fragment_uses_demo_safe_fallback(self) -> None:
        with patch.dict(os.environ, {"RAG_IME_ENABLE_DEMO_SAFE_FALLBACK": "1"}):
            _, follow_up, _, _ = self._prime_post_commit(
                core=CuratedMemoryCore(),
                predictor=RecordingPredictionProvider(["您已"]),
                context="我的需求你没有完成",
                commit_preview="完成",
            )
        model_candidates = [item for item in follow_up["displayCandidates"] if item["sourceType"] == "model"]

        self.assertEqual([item["text"] for item in model_candidates], ["继续补齐需求"])
        self.assertEqual(follow_up["modelLane"]["fallbackReason"], "demo_safe_empty_post_commit_fallback")

    def test_v1_repeated_context_does_not_show_demo_fallback_echo(self) -> None:
        _, follow_up, _, _ = self._prime_post_commit(
            core=CuratedMemoryCore(),
            predictor=RecordingPredictionProvider(["您已"]),
            context="你好明天明天见继续完善一下继续完善一下继续完善一下",
            commit_preview="",
        )
        model_candidates = [item for item in follow_up["displayCandidates"] if item["sourceType"] == "model"]

        self.assertFalse(any(item["text"].startswith("继续完善") or item["text"] == "继续" for item in model_candidates))
        self.assertNotEqual(follow_up["modelLane"].get("fallbackReason"), "demo_safe_empty_post_commit_fallback")

    def test_v1_post_commit_rag_rejects_short_context_echo_even_when_accepted(self) -> None:
        _, follow_up, _, _ = self._prime_post_commit(
            core=CuratedMemoryCore([_memory("echo:welcome", "欢迎", accepted_count=9)]),
            predictor=RecordingPredictionProvider([""]),
            context="你好欢迎欢迎使用继续继续完善一下欢迎继续欢迎欢迎",
            commit_preview="",
        )
        surfaces = [item["text"] for item in follow_up["displayCandidates"]]

        self.assertNotIn("欢迎", surfaces)

    def test_v1_post_commit_presentation_streams_candidate_prefixes(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RAG_IME_POST_COMMIT_PRESENTATION_STREAM": "1",
                "RAG_IME_POST_COMMIT_PENDING_PREVIEW": "1",
            },
        ):
            core = CuratedMemoryCore()
            predictor = RecordingPredictionProvider(["继续补齐需求"], sleep_s=0.35)
            first = self._response(
                self._post_commit_payload(context="我的需求你没有完成", commit_preview="完成"),
                core=core,
                predictor=predictor,
            )
            self.assertEqual(first["uiMode"], "post_commit_prediction")
            first_model_candidates = [item for item in first["displayCandidates"] if item["sourceType"] == "model"]
            self.assertEqual(first_model_candidates[0]["text"], "继续")
            self.assertTrue(first_model_candidates[0]["metadata"]["pendingPreview"])
            self.assertTrue(wait_for_model_prediction_lane_idle(timeout_s=2.0))
            time.sleep(0.25)

            streamed_surfaces: list[str] = []
            lane_states: list[str] = []
            should_follow_up: list[bool] = []
            for request_seq in (2, 3, 4):
                response = self._response(
                    self._post_commit_payload(
                        request_seq=request_seq,
                        context="我的需求你没有完成",
                        commit_preview="完成",
                        progressive_follow_up=True,
                    ),
                    core=core,
                    predictor=predictor,
                )
                model_candidates = [item for item in response["displayCandidates"] if item["sourceType"] == "model"]
                streamed_surfaces.append(model_candidates[0]["text"])
                lane_states.append(response["modelLane"]["completionJobState"])
                should_follow_up.append(response["progressive"]["shouldFollowUp"])

            self.assertEqual(streamed_surfaces, ["继续", "继续补齐", "继续补齐需求"])
            self.assertEqual(lane_states, ["streaming", "streaming", "hit"])
            self.assertEqual(should_follow_up, [True, True, False])

    def test_v1_post_commit_fallback_presentation_streams_candidate_prefixes(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RAG_IME_POST_COMMIT_PRESENTATION_STREAM": "1",
                "RAG_IME_POST_COMMIT_PENDING_PREVIEW": "1",
                "RAG_IME_ENABLE_DEMO_SAFE_FALLBACK": "1",
            },
        ):
            core = CuratedMemoryCore()
            predictor = RecordingPredictionProvider(["您已"], sleep_s=0.35)
            first = self._response(
                self._post_commit_payload(context="我的需求你没有完成", commit_preview="完成"),
                core=core,
                predictor=predictor,
            )
            self.assertEqual(first["uiMode"], "post_commit_prediction")
            first_model_candidates = [item for item in first["displayCandidates"] if item["sourceType"] == "model"]
            self.assertEqual(first_model_candidates[0]["text"], "继续")
            self.assertTrue(first_model_candidates[0]["metadata"]["pendingPreview"])
            self.assertTrue(wait_for_model_prediction_lane_idle(timeout_s=2.0))
            time.sleep(0.25)

            streamed_surfaces: list[str] = []
            lane_states: list[str] = []
            should_follow_up: list[bool] = []
            for request_seq in (2, 3, 4):
                response = self._response(
                    self._post_commit_payload(
                        request_seq=request_seq,
                        context="我的需求你没有完成",
                        commit_preview="完成",
                        progressive_follow_up=True,
                    ),
                    core=core,
                    predictor=predictor,
                )
                model_candidates = [item for item in response["displayCandidates"] if item["sourceType"] == "model"]
                streamed_surfaces.append(model_candidates[0]["text"])
                lane_states.append(response["modelLane"]["completionJobState"])
                should_follow_up.append(response["progressive"]["shouldFollowUp"])

            self.assertEqual(streamed_surfaces, ["继续", "继续补齐", "继续补齐需求"])
            self.assertEqual(lane_states, ["streaming", "streaming", "completed"])
            self.assertEqual(should_follow_up, [True, True, False])

    def test_v1_filter_post_commit_completion_rejects_truncated_fragment(self) -> None:
        from rag_ime.rime_sidecar import parse_rime_context_payload

        rime_snapshot = parse_rime_context_payload(
            self._post_commit_payload(context="我的需求你没有完成", commit_preview="完成"),
            default_project="wisdom-weasel-rag-ime",
        )
        kept = filter_post_commit_model_completions(
            [
                ModelPrediction("您已", 1, "model", 1, 0.9),
                ModelPrediction("继续补齐需求", 2, "model", 1, 0.9),
            ],
            snapshot=rime_snapshot,
            max_candidates=3,
        )

        self.assertEqual([item.text for item in kept], ["继续补齐需求"])

    def test_v1_minimind_quality_gate_abstains_on_off_topic_closed_context(self) -> None:
        from rag_ime.rime_sidecar import parse_rime_context_payload

        snapshot = parse_rime_context_payload(
            self._post_commit_payload(
                context="RAG要好好测试，还有rime的重排和优化",
                commit_preview="重排和优化",
            ),
            default_project="wisdom-weasel-rag-ime",
        )
        metadata = {
            "candidate_mode": "base-completion-branches",
            "candidate_scores": [
                {"text": "不要再临时加新内容", "probability": 0.22},
                {"text": "等跑完再看结果", "probability": 0.04},
            ],
        }
        kept = filter_post_commit_model_completions(
            [
                ModelPrediction("不要再临时加新内容", 1, "local-mlx", 80, 0.9, metadata),
                ModelPrediction("等跑完再看结果", 2, "local-mlx", 80, 0.8, metadata),
            ],
            snapshot=snapshot,
            max_candidates=3,
        )

        self.assertEqual(kept, [])

    def test_v1_minimind_quality_gate_keeps_topic_match_and_open_context(self) -> None:
        from rag_ime.rime_sidecar import parse_rime_context_payload

        technical = parse_rime_context_payload(
            self._post_commit_payload(
                context="如果是模型的话，可能是训练数据和语料问题",
                commit_preview="语料问题",
            ),
            default_project="wisdom-weasel-rag-ime",
        )
        technical_metadata = {
            "candidate_mode": "base-completion-branches",
            "candidate_scores": [{"text": "需要重新训练模型", "probability": 0.13}],
        }
        technical_kept = filter_post_commit_model_completions(
            [ModelPrediction("需要重新训练模型", 1, "local-mlx", 80, 0.9, technical_metadata)],
            snapshot=technical,
            max_candidates=3,
        )

        natural = parse_rime_context_payload(
            self._post_commit_payload(context="明天上午开会以后", commit_preview="以后"),
            default_project="wisdom-weasel-rag-ime",
        )
        natural_metadata = {
            "candidate_mode": "base-completion-branches",
            "candidate_scores": [{"text": "我再重新检查一遍", "probability": 0.34}],
        }
        natural_kept = filter_post_commit_model_completions(
            [ModelPrediction("我再重新检查一遍", 1, "local-mlx", 80, 0.9, natural_metadata)],
            snapshot=natural,
            max_candidates=3,
        )

        self.assertEqual([item.text for item in technical_kept], ["，需要重新训练模型"])
        self.assertEqual(technical_kept[0].metadata["qualityGate"]["reason"], "topic_match")
        self.assertAlmostEqual(technical_kept[0].confidence, 0.13)
        self.assertEqual([item.text for item in natural_kept], ["，我再重新检查一遍"])
        self.assertEqual(natural_kept[0].metadata["qualityGate"]["reason"], "open_context")

        display = rime_sidecar_module.merge_display_candidates(
            snapshot=technical,
            model_predictions=technical_kept,
            suggestions=[],
        )
        self.assertEqual(display[0].text, "需要重新训练模型")
        self.assertEqual(display[0].insert_text, "，需要重新训练模型")

    def test_v1_minimind_quality_gate_observe_mode_keeps_visible_candidates(self) -> None:
        from rag_ime.rime_sidecar import parse_rime_context_payload

        snapshot = parse_rime_context_payload(
            self._post_commit_payload(
                context="RAG要好好测试，还有rime的重排和优化",
                commit_preview="重排和优化",
            ),
            default_project="wisdom-weasel-rag-ime",
        )
        metadata = {
            "candidate_mode": "base-completion-branches",
            "candidate_scores": [
                {"text": "不要再临时加新内容", "probability": 0.22},
                {"text": "等跑完再看结果", "probability": 0.04},
            ],
        }
        with patch.dict(os.environ, {"RAG_IME_LOCAL_MODEL_QUALITY_GATE_MODE": "observe"}):
            kept = filter_post_commit_model_completions(
                [
                    ModelPrediction("不要再临时加新内容", 1, "local-mlx", 80, 0.9, metadata),
                    ModelPrediction("等跑完再看结果", 2, "local-mlx", 80, 0.8, metadata),
                ],
                snapshot=snapshot,
                max_candidates=3,
            )

        self.assertEqual([item.text for item in kept], ["，不要再临时加新内容", "，等跑完再看结果"])
        self.assertTrue(all(item.metadata["qualityGate"]["observedOnly"] for item in kept))
        self.assertTrue(all(item.metadata["qualityGate"]["mode"] == "observe" for item in kept))

    def test_v1_minimind_quality_gate_rejects_hypothesis_negation(self) -> None:
        from rag_ime.rime_sidecar import parse_rime_context_payload

        snapshot = parse_rime_context_payload(
            self._post_commit_payload(
                context="如果是模型的话，可能是训练数据和语料问题",
                commit_preview="语料问题",
            ),
            default_project="wisdom-weasel-rag-ime",
        )
        metadata = {
            "candidate_mode": "base-completion-branches",
            "candidate_scores": [{"text": "不是训练数据问题", "probability": 0.13}],
        }

        kept = filter_post_commit_model_completions(
            [ModelPrediction("不是训练数据问题", 1, "local-mlx", 80, 0.9, metadata)],
            snapshot=snapshot,
            max_candidates=3,
        )

        self.assertEqual(kept, [])

    def test_v1_same_app_commit_ledger_allows_terminal_without_accessibility(self) -> None:
        payload = self._post_commit_payload(
            context="终端里继续执行命令",
            commit_preview="执行命令",
        )
        payload["frontAppBundleId"] = "com.mitchellh.ghostty"
        payload["foregroundText"] = {
            "available": True,
            "source": "ime_commit_ledger",
            "confidence": 0.65,
            "freshnessMs": 0,
            "surroundingBefore": "终端里继续执行命令",
            "surroundingAfter": "",
            "captureEpoch": 1,
            "sourceAppBundleId": "com.mitchellh.ghostty",
            "inputSourceId": "im.rime.inputmethod.Squirrel.Hans",
            "commitTextMatched": True,
            "warnings": ["ime_commit_ledger_same_app"],
        }

        response = self._response(payload, predictor=RecordingPredictionProvider(["然后检查输出"]))

        self.assertTrue(response["triggerDecision"]["foregroundContextGate"]["allowed"])
        self.assertEqual(response["triggerDecision"]["foregroundContextGate"]["source"], "ime_commit_ledger")

    def test_v1_unmatched_commit_ledger_still_fails_closed(self) -> None:
        payload = self._post_commit_payload(context="终端旧上下文", commit_preview="新提交")
        payload["frontAppBundleId"] = "com.mitchellh.ghostty"
        payload["foregroundText"] = {
            "available": True,
            "source": "ime_commit_ledger",
            "confidence": 0.65,
            "freshnessMs": 0,
            "surroundingBefore": "终端旧上下文",
            "captureEpoch": 1,
            "sourceAppBundleId": "com.mitchellh.ghostty",
            "inputSourceId": "im.rime.inputmethod.Squirrel.Hans",
            "commitTextMatched": False,
        }

        response = self._response(payload, predictor=RecordingPredictionProvider(["不应出现"]))

        self.assertFalse(response["triggerDecision"]["foregroundContextGate"]["allowed"])
        self.assertEqual(
            response["triggerDecision"]["foregroundContextGate"]["foregroundReason"],
            "commit ledger does not prove the current commit",
        )

    def test_v1_stale_context_late_result_dropped(self) -> None:
        _, first_follow_up, _, _ = self._prime_post_commit(
            predictor=RecordingPredictionProvider(["第一段建议"]),
            context="旧上下文先写",
            commit_preview="先写",
        )
        _, second_follow_up, _, _ = self._prime_post_commit(
            predictor=RecordingPredictionProvider(["第二段建议"]),
            context="新上下文继续",
            commit_preview="继续",
        )

        self.assertNotEqual(
            first_follow_up["predictionSession"]["contextFingerprint"],
            second_follow_up["predictionSession"]["contextFingerprint"],
        )
        self.assertIn("第一段建议", [item["text"] for item in first_follow_up["modelPredictions"]])
        self.assertIn("第二段建议", [item["text"] for item in second_follow_up["modelPredictions"]])
        self.assertNotIn("第一段建议", [item["text"] for item in second_follow_up["displayCandidates"]])

    def test_v1_accept_prediction_schedules_next_prediction(self) -> None:
        _, follow_up, _, _ = self._prime_post_commit(predictor=RecordingPredictionProvider(["真实输入链路跑通"]))
        model_candidates = [item for item in follow_up["displayCandidates"] if item["sourceType"] == "model"]
        self.assertTrue(model_candidates)
        top = model_candidates[0]

        with tempfile.TemporaryDirectory(prefix="rag-ime-v1-select-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "select.sqlite")
            core.initialize()
            result = record_rime_side_candidate_selection(
                payload={
                    "privacyDisposition": "allowed",
                    "candidate": top,
                    "shownCandidates": follow_up["displayCandidates"],
                    "recentContext": "我想彻底整理项目，先把",
                    "project": "wisdom-weasel-rag-ime",
                    "frontAppBundleId": "com.apple.TextEdit",
                },
                adapter=InputMethodAdapter(core),
                core=core,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["recordedAction"] or result["recordedCommitAction"])
        self.assertEqual(follow_up["predictionSession"]["phase"], "post_commit")
        self.assertEqual(follow_up["keyPolicy"]["tab"], "accept_top_prediction")
        self.assertEqual(follow_up["keyPolicy"]["optionNumber"], "select_prediction_by_ordinal")

    def test_v1_diagnostic_filter_counts_are_stable_zero_when_nothing_filtered(self) -> None:
        _, follow_up, _, _ = self._prime_post_commit(
            core=CuratedMemoryCore([_memory("good:1", "真实输入链路跑通", accepted_count=2)]),
            predictor=RecordingPredictionProvider(["候选质量验收"]),
        )

        self.assertIn("filteredPredictionCount", follow_up["modelLane"])
        self.assertIn("filteredSuggestionCount", follow_up["ragLane"])
        self.assertIn("postCommitQualityFilteredCount", follow_up["ragLane"])
        self.assertEqual(follow_up["modelLane"]["filteredPredictionCount"], 0)
        self.assertEqual(follow_up["ragLane"]["filteredSuggestionCount"], 0)
        self.assertEqual(follow_up["ragLane"]["postCommitQualityFilteredCount"], 0)

    def test_v1_memory_evidence_candidate_exposes_source_reason_without_direct_display(self) -> None:
        _, follow_up, _, _ = self._prime_post_commit(
            core=CuratedMemoryCore([_memory("good:1", "真实输入链路跑通", accepted_count=2)]),
            predictor=RecordingPredictionProvider(["候选质量验收"]),
        )
        memory_candidates = [item for item in follow_up["displayCandidates"] if item["sourceType"] == "memory"]
        memory_evidence = [
            item for item in follow_up["ragCandidates"] if item["metadata"].get("source_type") == "memory"
        ]

        self.assertFalse(memory_candidates)
        self.assertTrue(memory_evidence)
        self.assertEqual(memory_evidence[0]["metadata"]["sourceReason"], "accepted_memory")
        self.assertTrue(follow_up["ragLane"]["directDisplaySuppressed"])

    def test_v1_filter_post_commit_completion_rejects_context_echo(self) -> None:
        from rag_ime.rime_sidecar import parse_rime_context_payload

        rime_snapshot = parse_rime_context_payload(
            self._post_commit_payload(context="现在模型不对拼音预测，只是", commit_preview="只是"),
            default_project="wisdom-weasel-rag-ime",
        )
        kept = filter_post_commit_model_completions(
            [
                ModelPrediction("现在模型不对拼音预测，只是", 1, "model", 1, 0.9),
                ModelPrediction("上屏后短补全", 2, "model", 1, 0.9),
            ],
            snapshot=rime_snapshot,
            max_candidates=3,
        )

        self.assertEqual([item.text for item in kept], ["上屏后短补全"])
        self.assertEqual(kept[0].metadata["requestType"], "post_commit_completion")
        self.assertTrue(kept[0].metadata["noPinyinFilter"])

    def test_v1_filter_post_commit_completion_rejects_repeated_tail_echo(self) -> None:
        from rag_ime.rime_sidecar import parse_rime_context_payload

        rime_snapshot = parse_rime_context_payload(
            self._post_commit_payload(
                context="你好明天明天见继续完善一下继续完善一下继续完善一下",
                commit_preview="",
            ),
            default_project="wisdom-weasel-rag-ime",
        )
        kept = filter_post_commit_model_completions(
            [
                ModelPrediction("继续完善", 1, "model", 1, 0.9),
                ModelPrediction("换一个方向", 2, "model", 1, 0.9),
            ],
            snapshot=rime_snapshot,
            max_candidates=3,
        )

        self.assertEqual([item.text for item in kept], ["换一个方向"])

    def test_model_filter_rejects_adjacent_function_character_repetition(self) -> None:
        kept = rime_sidecar_module._filter_model_predictions(
            [
                ModelPrediction("能能接", 1, "model", 1, 0.9),
                ModelPrediction("继续优化交互", 2, "model", 1, 0.9),
            ],
            current_input="",
            explicit_recent_context="持续优化",
        )

        self.assertEqual([item.text for item in kept], ["继续优化交互"])

    def test_bare_completion_adds_clause_bridge_only_after_complete_context(self) -> None:
        predictions = [
            ModelPrediction(
                "候选也要重新生成",
                1,
                "local-mlx",
                30,
                0.9,
                metadata={"candidate_mode": "base-completion-branches"},
            )
        ]

        complete = rime_sidecar_module._filter_model_predictions(
            predictions,
            current_input="",
            explicit_recent_context="模型好像上下文有问题，预测的不合理",
        )
        incomplete = rime_sidecar_module._filter_model_predictions(
            predictions,
            current_input="",
            explicit_recent_context="这个输入法目前最影响体验的是",
        )

        self.assertEqual([item.text for item in complete], ["，候选也要重新生成"])
        self.assertEqual([item.text for item in incomplete], ["候选也要重新生成"])

    def test_group_aware_model_context_keeps_recent_same_group_typing(self) -> None:
        group_id = "app:context-quality-test"
        current_ms = rime_sidecar_module.now_ms()
        rime_sidecar_module._GROUP_SHORT_BUFFER.clear()
        try:
            rime_sidecar_module._GROUP_SHORT_BUFFER.append(
                group_id,
                "LLM 要立即弹出，",
                created_at_ms=current_ms - 10,
            )
            rime_sidecar_module._GROUP_SHORT_BUFFER.append(
                group_id,
                "持续优化",
                created_at_ms=current_ms - 5,
            )

            context, meta = rime_sidecar_module._group_aware_model_context(
                context_group_id=group_id,
                explicit_recent_context="持续优化",
                current_ms=current_ms,
            )

            self.assertEqual(context, "LLM 要立即弹出，持续优化")
            self.assertEqual(meta["contextMode"], "group-short-buffer+foreground")
            self.assertEqual(meta["foregroundContextChars"], 4)
            self.assertGreater(meta["groupContextChars"], 0)
        finally:
            rime_sidecar_module._GROUP_SHORT_BUFFER.clear()

    def test_group_aware_model_context_keeps_long_foreground_up_to_model_budget(self) -> None:
        foreground = (
            "LLM 要立即弹出并稳定显示三个连续候选。"
            "RAG 和 DeepSeek 上下文需要可验证注入。"
            "模型好像上下文有问题，预测的不合理"
        )
        context, meta = rime_sidecar_module._group_aware_model_context(
            context_group_id="app:context-focus-test",
            explicit_recent_context=foreground,
        )

        self.assertEqual(context, foreground)
        self.assertFalse(meta["contextWindowTrimmed"])
        self.assertEqual(meta["assembledContextChars"], meta["modelContextChars"])
        self.assertEqual(meta["contextWindowChars"], 256)

    def test_group_aware_model_context_keeps_group_tail_for_short_foreground(self) -> None:
        group_id = "app:context-short-fragment-test"
        rime_sidecar_module._GROUP_SHORT_BUFFER.clear()
        try:
            rime_sidecar_module._GROUP_SHORT_BUFFER.append(
                group_id,
                "候选选择目前不方便，缺少 DS 入口，需要",
                created_at_ms=rime_sidecar_module.now_ms() - 5,
            )
            context, meta = rime_sidecar_module._group_aware_model_context(
                context_group_id=group_id,
                explicit_recent_context="持续优化",
            )

            self.assertTrue(context.endswith("持续优化"))
            self.assertIn("DS 入口", context)
            self.assertGreater(meta["groupContextChars"], 0)
        finally:
            rime_sidecar_module._GROUP_SHORT_BUFFER.clear()

    def test_v1_key_policy_keeps_composition_numbers_for_rime(self) -> None:
        composition_policy = key_policy_for_prediction_session(
            {"phase": "anchor_composing", "inputMode": "anchor_composing", "selectionScope": "rime"}
        )
        post_commit_policy = key_policy_for_prediction_session(
            {"phase": "post_commit", "inputMode": "post_commit_predicting", "selectionScope": "prediction"}
        )

        self.assertEqual(composition_policy["numberKeys"], "select_rime_candidate")
        self.assertEqual(post_commit_policy["numberKeys"], "pass_through")
        self.assertEqual(post_commit_policy["tab"], "accept_top_prediction")
        self.assertEqual(post_commit_policy["optionNumber"], "select_prediction_by_ordinal")


class DisplayMemoryFeedbackDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        rime_sidecar_module.clear_display_memory_feedback_cache()
        self.addCleanup(rime_sidecar_module.clear_display_memory_feedback_cache)

    class _RecordingFeedbackCore:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def record_memory_feedback(self, event: dict[str, object]) -> None:
            self.events.append(dict(event))

    def _snapshot(self, *, panel_session_id: str = "panel-a") -> object:
        return rime_sidecar_module.parse_rime_context_payload(
            {
                "sessionId": "session-1",
                "requestSeq": 7,
                "committedContext": "今天继续完善输入法上下文",
                "panelSessionId": panel_session_id,
                "committedContextHash": "sha256:feedbackcontext1",
            },
            default_project="test-project",
        )

    def _display_candidates(self) -> list[object]:
        from rag_ime.models import SideCandidateDisplayItem

        return [
            SideCandidateDisplayItem(
                label="1",
                text="记忆候选",
                insert_text="记忆候选",
                source_type="memory",
                selection_action="commit_side_candidate",
                source_index=0,
                suggestion_id="sugg-1",
                memory_id="mem-1",
            )
        ]

    def test_same_panel_records_shown_only_once(self) -> None:
        core = self._RecordingFeedbackCore()
        snapshot = self._snapshot()
        for _ in range(3):
            rime_sidecar_module._record_display_memory_feedback(
                core=core,
                snapshot=snapshot,
                display_candidates=self._display_candidates(),
                trace_id="trace-1",
                project="test-project",
            )

        shown_events = [event for event in core.events if event.get("event") == "shown"]
        self.assertEqual(len(shown_events), 1)
        self.assertEqual(shown_events[0]["candidateId"], "mem-1")

    def test_new_panel_session_records_shown_again(self) -> None:
        core = self._RecordingFeedbackCore()
        rime_sidecar_module._record_display_memory_feedback(
            core=core,
            snapshot=self._snapshot(panel_session_id="panel-a"),
            display_candidates=self._display_candidates(),
            trace_id="trace-1",
            project="test-project",
        )
        rime_sidecar_module._record_display_memory_feedback(
            core=core,
            snapshot=self._snapshot(panel_session_id="panel-b"),
            display_candidates=self._display_candidates(),
            trace_id="trace-2",
            project="test-project",
        )

        shown_events = [event for event in core.events if event.get("event") == "shown"]
        self.assertEqual(len(shown_events), 2)


if __name__ == "__main__":
    unittest.main()
