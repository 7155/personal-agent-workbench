from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rag_ime.pi.provider_config import load_pi_provider_config


class PiProviderConfigTest(unittest.TestCase):
    def test_imported_openai_gateway_disables_unverified_native_tool_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "providers.json"
            path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "openai": {
                                "options": {
                                    "baseURL": "https://gateway.example/v1",
                                    "apiKey": "fixture-secret",
                                },
                                "models": {"gpt-test": {"name": "GPT Test"}},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            bundle = load_pi_provider_config(path)

        provider = bundle.providers["gpt"]
        self.assertNotIn("modelCatalogProvider", provider)
        self.assertEqual(provider["models"], [{"id": "gpt-test", "name": "GPT Test"}])
        self.assertEqual(provider["compat"], {"supportsToolSearch": False})

    def test_imported_openai_compatible_gateway_declares_pi_completions_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "providers.json"
            path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "openai": {
                                "options": {
                                    "baseURL": "https://gateway.example/v1",
                                    "apiKey": "fixture-secret",
                                },
                                "models": {"gpt-5.6-luna": {"name": "GPT-5.6 Luna"}},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            bundle = load_pi_provider_config(path)

        self.assertEqual(bundle.providers["gpt"]["api"], "openai-completions")

    def _import_model(self, model: dict) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "providers.json"
            path.write_text(json.dumps({"provider": {"openai": {
                "options": {"baseURL": "https://gateway.example/v1", "apiKey": "fixture-secret"},
                "models": {"vision-model": model},
            }}}), encoding="utf-8")
            return load_pi_provider_config(path).providers["gpt"]["models"][0]

    def test_pi_image_and_prompt_cache_metadata_survive_import(self) -> None:
        limits = {"maxRequestBytes": 8_000_000, "images": {
            "maxPerMessage": 5, "maxPerRequest": 20,
            "resize": {"maxWidth": 1568, "maxHeight": 1568,
                       "maxBytes": 524288, "jpegQuality": 75},
        }}
        model = self._import_model({"modalities": {"input": ["text", "image", "text"]},
                                    "inputLimits": limits,
                                    "promptCache": {"short": 300, "long": 3600}})
        self.assertEqual(model["input"], ["text", "image"])
        self.assertEqual(model["inputLimits"], limits)
        self.assertEqual(model["promptCache"], {"short": 300, "long": 3600})

    def test_pi_metadata_rejects_invalid_values_and_unknown_payload(self) -> None:
        model = self._import_model({
            "modalities": {"input": ["text", "audio"]},
            "inputLimits": {"maxRequestBytes": True, "apiKey": "do-not-forward", "images": {
                "maxPerMessage": -1, "maxPerRequest": "10",
                "resize": {"maxWidth": 2**53, "maxHeight": 0,
                           "maxBytes": 10.5, "jpegQuality": 101, "headers": {"key": "secret"}},
            }},
            "promptCache": {"short": False, "long": "3600", "command": "!echo secret"},
        })
        self.assertEqual(model, {"id": "vision-model"})
        self.assertEqual(self._import_model({"inputLimits": {"maxRequestBytes": 123}})["inputLimits"],
                         {"maxRequestBytes": 123})

    def test_imported_aed_models_translate_whitelisted_runtime_metadata(self) -> None:
        source_models = {
            model_id: {
                "name": name,
                "limit": {"context": 1_050_000, "output": 128_000},
                "variants": {
                    "low": {},
                    "medium": {},
                    "high": {},
                    "xhigh": {},
                    "max": {},
                },
            }
            for model_id, name in (
                ("gpt-5.6-luna", "GPT-5.6 Luna"),
                ("gpt-5.6-sol", "GPT-5.6 Sol"),
                ("gpt-5.6-terra", "GPT-5.6 Terra"),
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "providers.json"
            path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "openai": {
                                "options": {
                                    "baseURL": "https://gateway.example/v1",
                                    "apiKey": "fixture-secret",
                                },
                                "models": source_models,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            bundle = load_pi_provider_config(path)

        expected_thinking_levels = {
            "off": None,
            "minimal": None,
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "max": "max",
        }
        for model, expected_name in zip(
            bundle.providers["gpt"]["models"],
            ("GPT-5.6 Luna", "GPT-5.6 Sol", "GPT-5.6 Terra"),
            strict=True,
        ):
            self.assertEqual(model["name"], expected_name)
            self.assertIs(model["reasoning"], True)
            self.assertEqual(model["thinkingLevelMap"], expected_thinking_levels)
            self.assertEqual(model["contextWindow"], 1_050_000)
            self.assertEqual(model["maxTokens"], 128_000)


if __name__ == "__main__":
    unittest.main()
