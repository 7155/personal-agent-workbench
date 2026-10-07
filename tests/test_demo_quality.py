from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.adapter import InputMethodAdapter, SuggestionRequest
from rag_ime.codex_history import load_codex_history_records
from rag_ime.input_quality import MEMORY_CONTEXT_OPT_IN_TAG
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.models import ModelPrediction
from rag_ime.rime_sidecar import (
    build_rime_sidecar_response,
    clear_model_prediction_holdover_cache,
    wait_for_model_prediction_lane_idle,
)
from tests.test_rime_sidecar import InlinePredictionThread


PROJECT = "wisdom-weasel-rag-ime"
BAD_CANDIDATE_FRAGMENTS = (
    "py 通过",
    "git diff",
    "installation yaml",
    "installation.yaml",
    "你要自己调试",
    "Working",
    "Chunk ID",
    "管理员密码",
    "未跟踪",
    "AGENTS",
    "codex_internal_context",
)


class DemoPredictionProvider:
    def predict(self, *, current_input: str, recent_context: str = "", max_candidates: int = 5):
        text = "本地小模型候选"
        if "LLM" in current_input or "模型" in current_input:
            text = "本地 MLX 小模型"
        elif "数字键" in current_input or "候选" in current_input:
            text = "候选面板选择"
        return [
            ModelPrediction(
                text=text,
                rank=1,
                provider_name="local-mlx-demo",
                latency_ms=11,
                confidence=0.88,
                metadata={"source": "local-llm", "initials": "bdmlxmx"},
            )
        ][:max_candidates]


class RagImeDemoQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._composing_model_env = os.environ.get("RAG_IME_ENABLE_COMPOSING_MODEL")
        self._pinyin_model_env = os.environ.get("RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL")
        os.environ["RAG_IME_ENABLE_COMPOSING_MODEL"] = "1"
        os.environ["RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL"] = "1"
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-demo-quality-")
        self.db_path = Path(self.tmp.name) / "quality.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()
        self.adapter = InputMethodAdapter(self.core, project=PROJECT)
        self._seed_quality_memories()
        self.assertTrue(wait_for_model_prediction_lane_idle(timeout_s=1.0))
        clear_model_prediction_holdover_cache()

    def tearDown(self) -> None:
        self.assertTrue(wait_for_model_prediction_lane_idle(timeout_s=1.0))
        clear_model_prediction_holdover_cache()
        if self._composing_model_env is None:
            os.environ.pop("RAG_IME_ENABLE_COMPOSING_MODEL", None)
        else:
            os.environ["RAG_IME_ENABLE_COMPOSING_MODEL"] = self._composing_model_env
        if self._pinyin_model_env is None:
            os.environ.pop("RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL", None)
        else:
            os.environ["RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL"] = self._pinyin_model_env
        self.tmp.cleanup()

    def _seed_quality_memories(self) -> None:
        good_rows = [
            (
                "embedding query 应该从当前输入和最近上下文构造, 再走本地向量召回。",
                "用户问 embedding query 怎么形成, 重点是当前输入 recent_context 和本地 embedding。",
                ("embedding", "retrieval", "query"),
            ),
            (
                "RAG 输入法是高频实时场景里的个人记忆系统。",
                "用户要求测试 RAG 和 LLM 能否很好输入, 产品想法是 local-first 个人输入记忆。",
                ("rag-ime", "product", "memory"),
            ),
            (
                "候选必须能用数字键选择, Rime 候选和 side candidate 要有不同 selectionAction。",
                "用户关心候选可选性、数字键路由和 Squirrel 面板。",
                ("candidate", "selection", "rime"),
            ),
            (
                "LLM 来源应优先本地小模型, 云端只做离线质量上限。",
                "用户问 LLM 来源, 要区分本地 MLX/Ollama/OpenAI-compatible 和云端默认链路。",
                ("llm", "local-model", "privacy"),
            ),
            (
                "旧候选不能覆盖新输入, requestSeq stale guard 要保护数字键面板。",
                "用户提到数字键 stale panel, 关键是 requestSeq/latest request 保护。",
                ("stale", "requestSeq", "number-key"),
            ),
            (
                "英文、代码和路径输入应优先保护原文提交, 不要被中文 RAG 候选吞掉。",
                "用户要求英文/代码/路径保护, 例如 git status、/Volumes 路径和 Python 标识符。",
                ("raw-ascii", "code", "path"),
            ),
            (
                "Codex 历史导入只保留 role=user 的真实输入, 过滤工具输出和系统注入。",
                "用户点名过滤系统注入、AGENTS、Working 状态和工具输出。",
                ("codex-history", "noise-filter"),
            ),
            (
                "用户短语频率要提升常选短语, accepted_count 和 phrase frequency 应进入排序。",
                "用户关注短语频率, 高频选择应比普通候选更靠前。",
                ("frequency", "ranking"),
            ),
            (
                "设计一个候选展示方式",
                "用户输入 sj 前缀时, 只能优先展示拼音前缀匹配的历史短语。",
                ("pinyin-prefix", "phrase-memory"),
            ),
            (
                "RAG 输入法结构化候选",
                "Prediction-first RAG 输入法需要展示结构化 RAG 候选，而不是直接展示旧输入原文。",
                ("structure", "rag-ime"),
            ),
            (
                "LLM RAG memory 都成功",
                "Prediction-first demo 要同时出现模型候选、RAG 候选和记忆短语。",
                ("phrase-memory", "memory"),
            ),
        ]
        for text, context, tags in good_rows:
            self.adapter.commit_text(
                text,
                recent_context=context,
                project=PROJECT,
                app="codex",
                source="codex_history",
                tags=tuple(dict.fromkeys((*tags, "curated", MEMORY_CONTEXT_OPT_IN_TAG))),
                privacy_disposition="allowed",
            )

        for text in BAD_CANDIDATE_FRAGMENTS:
            self.adapter.commit_text(
                text,
                recent_context="Codex runtime noise, tool output, system injection, not a user phrase.",
                project=PROJECT,
                app="codex",
                source="codex_history",
                tags=("runtime-noise",),
                privacy_disposition="allowed",
            )

        frequent_id = self.adapter.commit_text(
            "用我的 Codex 用户输入做黑盒质量测试",
            recent_context="用户短语频率 高频真实输入 质量测试",
            project=PROJECT,
            app="codex",
            source="codex_history",
            tags=("frequency", "quality", "curated", MEMORY_CONTEXT_OPT_IN_TAG),
            privacy_disposition="allowed",
        )
        suggestion = self.adapter.suggest(SuggestionRequest(current_input="Codex 用户输入 质量测试", top_k=1))[0]
        if str(suggestion.metadata.get("memory_id")) == frequent_id:
            for _ in range(3):
                self.adapter.choose(suggestion, query="Codex 用户输入 质量测试")

    def _suggest(self, current_input: str, *, recent_context: str = "", top_k: int = 5):
        suggestions = self.adapter.suggest(
            SuggestionRequest(
                current_input=current_input,
                recent_context=recent_context,
                project=PROJECT,
                app="codex",
                top_k=top_k,
            )
        )
        self._assert_no_bad_candidates_from_suggestions(suggestions)
        return suggestions

    def _candidate_text_blob(self, suggestions) -> str:
        return "\n".join(
            " ".join(
                [
                    item.surface_text,
                    str(item.metadata.get("insert_text") or ""),
                    str(item.metadata.get("source_type") or ""),
                    item.suggestion_type,
                ]
            )
            for item in suggestions
        )

    def _assert_no_bad_candidates_from_suggestions(self, suggestions) -> None:
        blob = self._candidate_text_blob(suggestions)
        for fragment in BAD_CANDIDATE_FRAGMENTS:
            self.assertNotIn(fragment, blob)

    def _assert_no_bad_display_candidates(self, response: dict[str, object]) -> None:
        display = response.get("displayCandidates")
        self.assertIsInstance(display, list)
        blob = "\n".join(
            f"{item.get('text', '')} {item.get('insertText', '')} {item.get('sourceType', '')}"
            for item in display
            if isinstance(item, dict)
        )
        for fragment in BAD_CANDIDATE_FRAGMENTS:
            self.assertNotIn(fragment, blob)

    def assertCandidateContains(self, suggestions, *needles: str) -> None:
        blob = self._candidate_text_blob(suggestions)
        self.assertTrue(any(needle in blob for needle in needles), blob)

    def test_embedding_query_real_input_recalls_retrieval_contract(self) -> None:
        suggestions = self._suggest(
            "embedding query 怎么从我的输入构造",
            recent_context="RAG 输入法要把当前输入和 recent_context 变成检索 query",
        )
        self.assertCandidateContains(suggestions, "embedding query", "本地向量召回")

    def test_rag_ime_product_idea_returns_personal_memory_candidate(self) -> None:
        suggestions = self._suggest("RAG 输入法 产品想法 个人记忆")
        self.assertCandidateContains(suggestions, "高频实时场景", "个人记忆系统")

    def test_candidate_selectability_case_mentions_number_key_contract(self) -> None:
        suggestions = self._suggest("候选可选性 数字键 Rime side candidate")
        self.assertCandidateContains(suggestions, "selectionAction", "数字键选择")

    def test_llm_source_case_prefers_local_model_language(self) -> None:
        suggestions = self._suggest("LLM 来源 本地小模型 云端默认链路")
        self.assertCandidateContains(suggestions, "本地小模型", "离线质量上限")

    def test_stale_panel_case_recalls_request_sequence_guard(self) -> None:
        suggestions = self._suggest("数字键 stale panel 旧候选覆盖新输入")
        self.assertCandidateContains(suggestions, "requestSeq", "stale guard")

    def test_english_code_and_path_input_keeps_raw_candidate_first(self) -> None:
        response = build_rime_sidecar_response(
            payload={
                "sessionId": "raw-code",
                "requestSeq": 7,
                "privacyDisposition": "allowed",
                "rawInput": "git status",
                "preedit": "git status",
                "committedContext": "用户正在输入 shell 命令和路径, 需要保护原文提交。",
                "maxVisibleCandidates": 5,
                "maxSideCandidates": 3,
                "predictionFirstMerge": True,
                "rimeContext": {"candidates": [{"label": "1", "text": "给他", "comment": "rime"}]},
            },
            adapter=self.adapter,
            core=self.core,
            predictor=DemoPredictionProvider(),
        )
        self._assert_no_bad_display_candidates(response)
        display = response["displayCandidates"]
        self.assertEqual(display[0]["sourceType"], "raw_english")
        self.assertEqual(display[0]["insertText"], "git status")
        self.assertEqual(display[0]["selectionAction"], "commit_side_candidate")

    def test_codex_history_noise_case_recalls_user_only_filtering_goal(self) -> None:
        suggestions = self._suggest("Codex 历史噪声 role user 工具输出过滤")
        self.assertCandidateContains(suggestions, "role=user", "系统注入", "工具输出")

    def test_user_phrase_frequency_boosts_frequent_short_phrase(self) -> None:
        suggestions = self._suggest("Codex 用户输入 质量测试", top_k=3)
        self.assertEqual(suggestions[0].surface_text, "用我的 Codex 用户输入做黑盒质量测试")
        reason = str(suggestions[0].metadata.get("reason", ""))
        self.assertTrue("frequency" in reason or "accepted:" in reason, reason)

    def test_pinyin_prefix_case_prioritizes_matching_phrase(self) -> None:
        suggestions = self._suggest("sj", recent_context="候选展示 输入法设计", top_k=3)
        self.assertEqual(suggestions[0].surface_text, "设计一个候选展示方式")
        self.assertIn("pinyin:", str(suggestions[0].metadata.get("reason", "")))

    # Source quality/selection requires complete real fixture retrieval. The
    # sidecar latency tests retain real threads and the bounded timeout path.
    @patch("rag_ime.rime_sidecar.Thread", InlinePredictionThread)
    def test_sidecar_distribution_keeps_model_rag_and_rime_sources_selectable(self) -> None:
        with patch.dict(os.environ, {"RAG_IME_AI_AFTER_COMMIT_ONLY": "0", "RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL": "1"}):
            response = build_rime_sidecar_response(
                payload={
                    "sessionId": "source-distribution",
                    "requestSeq": 11,
                    "privacyDisposition": "allowed",
                    "rawInput": "ragshurufa",
                    "preedit": "ragshurufa",
                    "committedContext": "RAG 输入法 候选可选性 LLM 来源 本地记忆",
                    "forceSideCandidates": True,
                    "maxVisibleCandidates": 6,
                    "maxSideCandidates": 4,
                    "rimeContext": {
                        "candidates": [
                            {"label": "1", "text": "RAG 输入法", "comment": "rime"},
                            {"label": "2", "text": "候选", "comment": "rime"},
                        ],
                        "page": 0,
                        "isLastPage": True,
                    },
                },
                adapter=self.adapter,
                core=self.core,
                predictor=DemoPredictionProvider(),
            )
        self._assert_no_bad_display_candidates(response)
        self.assertEqual(response["latencyBudgetMs"], 300)
        display = response["displayCandidates"]
        source_types = {item["sourceType"] for item in display}
        self.assertIn("model", source_types)
        self.assertFalse(source_types.intersection({"rag", "memory"}), source_types)
        self.assertIn("rime", source_types)
        self.assertGreaterEqual(len(response["ragCandidates"]), 1)
        self.assertTrue(response["ragLane"]["directDisplaySuppressed"])
        self.assertTrue(
            all(
                item["selectionAction"] == "commit_side_candidate"
                for item in display
                if item["sourceType"] in {"model", "rag", "memory"}
            )
        )
        self.assertTrue(
            all(
                item["selectionAction"] == "select_rime_candidate"
                for item in display
                if item["sourceType"] == "rime"
            )
        )
        self.assertEqual([item["selectionKey"] for item in display], ["1", "2", "3", "4", "5", "6"][: len(display)])
        self.assertEqual(response["modelLane"]["predictionCount"], 1)
        self.assertGreaterEqual(response["ragLane"]["suggestionCount"], 1)

    @patch("rag_ime.rime_sidecar.Thread", InlinePredictionThread)
    def test_prediction_first_demo_requires_llm_rag_and_memory_candidates(self) -> None:
        response = build_rime_sidecar_response(
            payload={
                "sessionId": "prediction-first-source-distribution",
                "requestSeq": 12,
                "privacyDisposition": "allowed",
                "commitTextPreview": "RAG 输入法 LLM 记忆 embedding",
                "committedContext": "我正在实现 Prediction-first RAG 输入法, 需要 LLM RAG memory 都成功。",
                "predictionFirstMerge": True,
                "forceSideCandidates": True,
                "maxVisibleCandidates": 8,
                "maxSideCandidates": 8,
                "rimeContext": {
                    "candidates": [
                        {"label": "1", "text": "根据", "comment": "wanxiang"},
                        {"label": "2", "text": "测试", "comment": "wanxiang"},
                    ],
                    "page": 0,
                    "isLastPage": True,
                },
            },
            adapter=self.adapter,
            core=self.core,
            predictor=DemoPredictionProvider(),
        )
        self._assert_no_bad_display_candidates(response)
        self.assertEqual(response["latencyBudgetMs"], 300)
        display = response["displayCandidates"]
        source_types = [item["sourceType"] for item in display]
        self.assertIn("model", source_types)
        self.assertFalse(set(source_types).intersection({"rag", "memory"}), source_types)
        self.assertGreaterEqual(len(response["ragCandidates"]), 1)
        self.assertTrue(response["ragLane"]["directDisplaySuppressed"])
        self.assertNotIn("rime", source_types)
        self.assertEqual(response["predictionFirst"]["policy"]["wanxiangFallbackCount"], 0)
        generated = [item for item in display if item["sourceType"] != "action"]
        actions = [item for item in display if item["sourceType"] == "action"]
        self.assertTrue(
            all(item["selectionAction"] == "commit_side_candidate" for item in generated),
            display,
        )
        self.assertTrue(
            {
                item["selectionAction"]
                for item in actions
            }
            == {
                "start_active_rag_from_context",
                "start_agent_deep_search_from_context",
            },
            display,
        )
        self.assertGreaterEqual(response["modelLane"]["predictionCount"], 1)
        self.assertGreaterEqual(response["ragLane"]["suggestionCount"], 1)


class CodexHistoryDemoQualityTests(unittest.TestCase):
    def test_codex_history_loader_keeps_only_real_user_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-history-quality-") as tmp:
            history = Path(tmp) / "rollout.jsonl"
            rows = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "用我的 Codex 的用户输入截取部分测试 RAG 输入法质量。",
                            }
                        ],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "git diff py 通过 未跟踪"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "# AGENTS.md instructions for /Volumes/example/git/learnA",
                            }
                        ],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"message": "Working (44m 30s) esc to interrupt"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": "Chunk ID: abc123\nOutput: installation.yaml 管理员密码",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "<codex_internal_context>tool state</codex_internal_context>",
                            }
                        ],
                    },
                },
            ]
            history.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )

            records = load_codex_history_records(history, limit=10)
            texts = [record.text for record in records]

        self.assertEqual(texts, ["用我的 Codex 的用户输入截取部分测试 RAG 输入法质量。"])
        self.assertTrue(all(record.role == "user" for record in records))
        blob = "\n".join(texts)
        for fragment in BAD_CANDIDATE_FRAGMENTS:
            self.assertNotIn(fragment, blob)
