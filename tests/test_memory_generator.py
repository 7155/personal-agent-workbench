from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from rag_ime.memory_compiler import compiler_generator_from_env
from rag_ime.memory_generator import (
    MemoryGenerationError,
    DeepSeekMemoryGenerator,
    _build_openai_url,
    _core_optimization_max_tokens,
    default_memory_generator_env_path,
    generated_memory_context,
    generated_memory_dedupe_tags,
    _extract_json_object,
    _parse_generated_lexicon_phrases,
    _parse_generated_memory_items,
    _parse_suggested_hidden_events,
)


class MemoryGeneratorTests(unittest.TestCase):
    def test_cli_keeps_retired_option_at_the_parse_boundary_only(self) -> None:
        from rag_ime import cli

        captured = []

        def capture_arguments(args):
            captured.append(args)
            raise RuntimeError("arguments captured before any runtime execution")

        with patch.dict("os.environ", {
            "RAG_IME_MEMORY_GENERATOR_ENV": "", "RAG_IME_DEEPSEEK_ENV": "",
            "RAG_IME_MODEL_ENV": "", "RAG_IME_VCP_REBUILD_ENV": "",
        }), patch.object(cli, "_build_core", side_effect=capture_arguments):
            with self.assertRaisesRegex(RuntimeError, "arguments captured"):
                cli.main(["generate-memory", "待整理内容", "--vcp-env-path", "/legacy/model.env"])
        self.assertEqual(captured[0].legacy_model_env_path, "/legacy/model.env")
        self.assertFalse(hasattr(captured[0], "vcp_env_path"))
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as result:
            cli.main(["generate-memory", "--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--model-env-path", output.getvalue())
        self.assertNotIn("--vcp-env-path", output.getvalue())

    def test_generator_environment_prefers_current_keys_and_accepts_legacy_configuration(self) -> None:
        values = {
            "RAG_IME_MEMORY_GENERATOR_ENV": "",
            "RAG_IME_DEEPSEEK_ENV": "",
            "RAG_IME_MODEL_ENV": "",
            "RAG_IME_VCP_REBUILD_ENV": "/legacy/model.env",
        }
        with patch.dict("os.environ", values):
            self.assertEqual(default_memory_generator_env_path(), Path("/legacy/model.env"))
            with patch.dict("os.environ", {"RAG_IME_MEMORY_GENERATOR_ENV": "/current/model.env"}):
                self.assertEqual(default_memory_generator_env_path(), Path("/current/model.env"))

    def test_memory_writes_use_neutral_names_and_keep_historical_dedupe_identity(self) -> None:
        canonical, historical = generated_memory_dedupe_tags("  记忆需要证据  ")
        self.assertTrue(canonical.startswith("generated-memory:"))
        self.assertTrue(historical.startswith("vcp-memory:"))
        self.assertEqual(canonical.partition(":")[2], historical.partition(":")[2])
        self.assertEqual(generated_memory_dedupe_tags("记忆需要证据"), (canonical, historical))
        items = _parse_generated_memory_items('{"memories":[{"text":"记忆需要保留可靠证据和来源"}]}')
        self.assertEqual(items[0].source, "memory-distillation")
        self.assertNotIn("VCP", generated_memory_context("来源", "上下文", "原因"))

    def test_default_generator_env_requires_an_explicit_provider_path(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "RAG_IME_MEMORY_GENERATOR_ENV": "",
                "RAG_IME_DEEPSEEK_ENV": "",
                "RAG_IME_MODEL_ENV": "",
                "RAG_IME_VCP_REBUILD_ENV": "",
            },
            clear=False,
        ):
            self.assertIsNone(default_memory_generator_env_path())

    def test_parse_generated_memory_items_filters_noise_and_secrets(self) -> None:
        raw = """
        ```json
        {
          "memories": [
            {"text": "用户希望 RAG 输入法参考 Wisdom-Weasel 的连续预测交互", "tags": ["preference", "ime"], "importance": 0.9, "reason": "稳定项目要求"},
            {"text": "现在用不了，需要真实生效", "tags": ["complaint"], "importance": 0.5},
            {"text": "API_KEY 是 sk-xxx", "tags": ["secret"], "importance": 1.0}
          ]
        }
        ```
        """

        items = _parse_generated_memory_items(raw, max_items=5)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "用户希望 RAG 输入法参考 Wisdom-Weasel 的连续预测交互")
        self.assertIn("preference", items[0].tags)

    def test_build_openai_url_accepts_base_with_or_without_v1(self) -> None:
        self.assertEqual(
            _build_openai_url("https://example.com", "/chat/completions"),
            "https://example.com/v1/chat/completions",
        )
        self.assertEqual(
            _build_openai_url("https://example.com/v1", "/chat/completions"),
            "https://example.com/v1/chat/completions",
        )

    def test_from_env_path_reads_model_settings_without_printing_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "API_BASE_URL=https://example.com/v1",
                        "API_KEY=secret-value",
                        "MODEL=deepseek-v4-flash",
                        "UPSTREAM_WIRE_API=responses",
                        "REQUEST_TIMEOUT_SECONDS=12",
                    ]
                ),
                encoding="utf-8",
            )

            generator = DeepSeekMemoryGenerator.from_env_path(env_path)

        self.assertEqual(generator.config.model, "deepseek-v4-flash")
        self.assertEqual(generator.config.upstream_wire_api, "responses")
        self.assertEqual(generator.config.request_timeout_seconds, 12)

    def test_from_env_path_accepts_deepseek_v4_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "RAG_IME_DEEPSEEK_BASE_URL=https://api.example.com/v1",
                        "DEEPSEEK_API_KEY=secret-value",
                        "RAG_IME_DEEPSEEK_MODEL=deepseek-v4-flash",
                        "RAG_IME_AI_WIRE_API=chat_completions",
                        "RAG_IME_AI_THINKING=disabled",
                        "RAG_IME_AI_RESPONSE_FORMAT=json_object",
                    ]
                ),
                encoding="utf-8",
            )

            generator = DeepSeekMemoryGenerator.from_env_path(env_path)

        self.assertEqual(generator.config.api_base_url, "https://api.example.com/v1")
        self.assertEqual(generator.config.model, "deepseek-v4-flash")
        self.assertEqual(generator.config.api_key, "secret-value")
        self.assertEqual(generator.config.chat_thinking_type, "disabled")
        self.assertEqual(generator.config.response_format, "json_object")
        self.assertEqual(generator.provider_name, "deepseek-v4")

    def test_model_request_uses_gateway_friendly_user_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "RAG_IME_DEEPSEEK_BASE_URL=https://api.example.com/v1",
                        "DEEPSEEK_API_KEY=secret-value",
                        "RAG_IME_DEEPSEEK_MODEL=deepseek-v4-flash",
                        "RAG_IME_AI_WIRE_API=chat_completions",
                    ]
                ),
                encoding="utf-8",
            )
            generator = DeepSeekMemoryGenerator.from_env_path(env_path)
            captured = {}

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return b'{"choices":[{"message":{"content":"{\\"memories\\":[]}"}}]}'

            def fake_urlopen(request, timeout):
                captured["user_agent"] = request.get_header("User-agent")
                captured["payload"] = json.loads(request.data.decode("utf-8"))
                return FakeResponse()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                report = generator.generate(text="测试离线整理模型", max_items=1)

        self.assertEqual(report.provider, "deepseek-v4")
        self.assertEqual(captured["user_agent"], "rag-ime/1.0 curl-compatible")

    def test_chat_completion_payload_can_disable_thinking_and_force_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "RAG_IME_DEEPSEEK_BASE_URL=https://api.example.com/v1",
                        "DEEPSEEK_API_KEY=secret-value",
                        "RAG_IME_DEEPSEEK_MODEL=deepseek-v4-flash",
                        "RAG_IME_AI_WIRE_API=chat_completions",
                        "RAG_IME_AI_THINKING=disabled",
                        "RAG_IME_AI_RESPONSE_FORMAT=json_object",
                    ]
                ),
                encoding="utf-8",
            )
            generator = DeepSeekMemoryGenerator.from_env_path(env_path)
            captured = {}

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return b'{"choices":[{"message":{"content":"{\\"memories\\":[]}"}}]}'

            def fake_urlopen(request, timeout):
                captured["payload"] = json.loads(request.data.decode("utf-8"))
                return FakeResponse()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                generator.generate(text="测试离线整理模型", max_items=1)

        self.assertEqual(captured["payload"]["thinking"], {"type": "disabled"})
        self.assertEqual(captured["payload"]["response_format"], {"type": "json_object"})

    def test_core_optimization_uses_larger_budget_for_large_diffs(self) -> None:
        self.assertGreaterEqual(
            _core_optimization_max_tokens(max_memories=8, max_lexicon_phrases=24, max_hide_suggestions=40),
            7000,
        )

    def test_optimize_core_metadata_reports_finish_and_cache_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "RAG_IME_DEEPSEEK_BASE_URL=https://api.example.com/v1",
                        "DEEPSEEK_API_KEY=secret-value",
                        "RAG_IME_DEEPSEEK_MODEL=deepseek-v4-flash",
                        "RAG_IME_AI_WIRE_API=chat_completions",
                        "RAG_IME_AI_RESPONSE_FORMAT=json_object",
                    ]
                ),
                encoding="utf-8",
            )
            generator = DeepSeekMemoryGenerator.from_env_path(env_path)

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return (
                        b'{"choices":[{"finish_reason":"stop","message":{"content":"{\\"memories\\":[]}"}}],'
                        b'"usage":{"prompt_cache_hit_tokens":12,"prompt_cache_miss_tokens":34,'
                        b'"prompt_tokens":46,"completion_tokens":5,"total_tokens":51}}'
                    )

            with patch("urllib.request.urlopen", return_value=FakeResponse()):
                report = generator.optimize_core(snapshot={"recentEvents": []})

        self.assertEqual(report.metadata["finishReason"], "stop")
        self.assertEqual(report.metadata["prompt_cache_hit_tokens"], 12)
        self.assertEqual(report.metadata["prompt_cache_miss_tokens"], 34)

    def test_from_env_path_rejects_non_deepseek_v4_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "API_BASE_URL=https://api.example.com/v1",
                        "API_KEY=secret-value",
                        "MODEL=gpt-5.5",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(MemoryGenerationError):
                DeepSeekMemoryGenerator.from_env_path(env_path)

    def test_compiler_generator_accepts_dsv4_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "RAG_IME_DEEPSEEK_BASE_URL=https://api.example.com/v1",
                        "DEEPSEEK_API_KEY=secret-value",
                        "RAG_IME_DEEPSEEK_MODEL=deepseek-v4-flash",
                    ]
                ),
                encoding="utf-8",
            )

            generator = compiler_generator_from_env(
                env_path=env_path,
                provider="dsv4",
            )

        self.assertEqual(generator.provider_name, "deepseek-v4")
        self.assertEqual(generator.config.model, "deepseek-v4-flash")

    def test_parse_core_optimization_payload(self) -> None:
        raw = """
        {
          "memories": [
            {"text":"用户是四川人，输入法默认开启四川常见模糊音。","tags":["dialect"],"importance":0.9}
          ],
          "lexiconPhrases": [
            {"text":"高频词自动优化","tags":["ime"],"weight":0.8,"reason":"项目常用词"},
            {"text":"API_KEY 是 sk-xxx","tags":["secret"],"weight":1.0}
          ],
          "hideEventIds": [
            {"eventId": 12, "reason": "旧候选回灌"}
          ]
        }
        """
        payload = _extract_json_object(raw)

        memories = _parse_generated_memory_items(raw, max_items=3)
        phrases = _parse_generated_lexicon_phrases(payload, max_items=3)
        hidden = _parse_suggested_hidden_events(payload, max_items=3)

        self.assertEqual([item.text for item in memories], ["用户是四川人，输入法默认开启四川常见模糊音。"])
        self.assertEqual([item.text for item in phrases], ["高频词自动优化"])
        self.assertEqual(hidden[0].event_id, 12)
