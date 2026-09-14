from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from rag_ime.agent_events import AgentEventHub
from rag_ime.agent_personas import AgentPersonaStore
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.deepseek_config import DeepSeekConfig
from rag_ime.pi.provider_config import PiProviderConfigError, load_pi_provider_config
from rag_ime.pi.config import PiRuntimeConfig, _deepseek_pi_provider
from rag_ime.pi.public import pi_message_payload, public_pi_model
from rag_ime.pi.values import PiRuntimeError
from rag_ime.pi.runtime import PiRuntimeHostManager


class PiRuntimeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="paw-pi-config-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fake_pi = self.root / "fake-host"
        self.fake_pi.write_text("#!/bin/sh\nexit 0\n")
        self.fake_pi.chmod(0o755)
        self.store = AgentSessionStore(self.root / "rag-ime.sqlite")
        self.store.initialize()
        self.session = self.store.create(
            title="输入助手",
            model_profile="deepseek/deepseek-v4",
            thinking_level="off",
            created_at_ms=100,
        )
        self.events = AgentEventHub()
        self.config = PiRuntimeConfig(
            enabled=True,
            executable=self.fake_pi,
            agent_dir=self.root / "agent-config",
            session_dir=self.root / "sessions",
            logs_dir=self.root / "logs",
            idle_timeout_seconds=0,
        )

    def test_launch_is_isolated_and_uses_no_tools_before_gateway_exists(self) -> None:
        self.assertEqual(
            self.config.launch_host_command(), [str(self.fake_pi.resolve())]
        )
        prompt = self.config.system_prompt_for_session(self.session)
        self.assertNotIn("<persona", prompt)
        self.assertNotIn("澄·远", prompt)
        self.assertIn("你在当前 Session 中协助用户", prompt)
        self.assertNotIn("Agent 伙伴", prompt)
        self.assertNotIn("<execution-mode", prompt)
        self.assertNotIn("<todo-policy>", prompt)
        self.assertNotIn("<managed-work>", prompt)
        self.assertLess(
            prompt.index("</work-policy>"),
            prompt.index("<durable-memory-policy>"),
        )

        environment = self.config.child_environment()
        self.assertEqual(
            environment["PI_CODING_AGENT_DIR"], str(self.root / "agent-config")
        )
        self.assertNotIn("RAG_IME_DEEPSEEK_API_KEY", environment)

        session_environment = self.config.child_environment(session=self.session)
        self.assertEqual(
            session_environment["RAG_IME_AGENT_EXECUTION_MODE"],
            "per_action",
        )
        self.assertNotIn("RAG_IME_AGENT_ROOM_BOUND", session_environment)
        room_environment = self.config.child_environment(
            session={
                **self.session,
                "roomParticipant": {
                    "roomId": "room:1",
                    "participantId": "participant:1",
                },
            }
        )
        self.assertEqual(room_environment["RAG_IME_AGENT_ROOM_BOUND"], "1")

    def test_managed_pi_config_extends_transient_provider_retry_window(self) -> None:
        self.config.prepare_agent_config()

        settings_path = self.config.agent_dir / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(
            settings["retry"],
            {
                "enabled": True,
                "maxRetries": 7,
                "baseDelayMs": 2_500,
            },
        )
        self.assertEqual(
            sum(
                settings["retry"]["baseDelayMs"] * (2**attempt)
                for attempt in range(settings["retry"]["maxRetries"])
            ),
            317_500,
        )
        self.assertEqual(settings_path.stat().st_mode & 0o777, 0o600)

    def test_managed_pi_retry_defaults_preserve_explicit_user_settings(self) -> None:
        self.config.agent_dir.mkdir(parents=True)
        settings_path = self.config.agent_dir / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "theme": "paper",
                    "retry": {
                        "enabled": False,
                        "maxRetries": 4,
                        "baseDelayMs": 5_000,
                    },
                }
            ),
            encoding="utf-8",
        )

        self.config.prepare_agent_config()

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["theme"], "paper")
        self.assertEqual(
            settings["retry"],
            {
                "enabled": False,
                "maxRetries": 4,
                "baseDelayMs": 5_000,
            },
        )
        self.assertEqual(settings_path.stat().st_mode & 0o777, 0o600)

    def test_managed_config_rejects_agent_parent_and_target_symlinks(self) -> None:
        outside = self.root / "outside-config"
        outside.mkdir()
        outside_settings = outside / "settings.json"
        outside_models = outside / "models.json"
        outside_settings.write_text('{"outside": true}\n', encoding="utf-8")
        outside_models.write_text('{"outside": true}\n', encoding="utf-8")
        provider = {
            "team": {
                "baseUrl": "http://127.0.0.1:8766/v1",
                "apiKey": "$TEAM_ATTEMPT_TOKEN",
                "models": [{"id": "team-model"}],
            }
        }

        linked_agent = self.root / "linked-agent"
        linked_agent.symlink_to(outside, target_is_directory=True)
        linked_config = replace(
            self.config,
            agent_dir=linked_agent,
            provider="team",
            model="team-model",
            model_configured=True,
            model_providers=provider,
        )
        with self.assertRaisesRegex(PiRuntimeError, "agent directory"):
            linked_config.prepare_agent_config()

        for filename, outside_target in (
            ("settings.json", outside_settings),
            ("models.json", outside_models),
        ):
            with self.subTest(filename=filename):
                agent = self.root / f"agent-{filename[:-5]}"
                agent.mkdir()
                target = agent / filename
                target.symlink_to(outside_target)
                config = replace(
                    self.config,
                    agent_dir=agent,
                    provider="team",
                    model="team-model",
                    model_configured=True,
                    model_providers=provider,
                )
                with self.assertRaisesRegex(PiRuntimeError, "must not be a symlink"):
                    config.prepare_agent_config()
        self.assertEqual(outside_settings.read_text(encoding="utf-8"), '{"outside": true}\n')
        self.assertEqual(outside_models.read_text(encoding="utf-8"), '{"outside": true}\n')

    def test_launch_does_not_inject_persistent_persona_without_package(self) -> None:
        personas = AgentPersonaStore(self.root / "rag-ime.sqlite")
        personas.initialize()
        role = personas.create(
            {
                "displayName": "澄·雨天",
                "tagline": "陪你安静整理",
                "summary": "偏向温和复盘与清楚的下一步。",
                "traits": ["温和", "复盘"],
                "timelineModel": "terra",
                "selectableModes": ["assistant"],
                "suitableTasks": ["温和复盘"],
                "unsuitableTasks": ["高风险独立决定"],
            }
        )
        session = self.store.create(
            title="雨天整理",
            role_id=role.role_id,
            role_version=role.version,
        )

        config = replace(self.config, role_resolver=personas.resolve)
        prompt = config.system_prompt_for_session(session)

        self.assertNotIn("澄·雨天", prompt)
        self.assertNotIn("<persona", prompt)
        self.assertIn("未启用的 Persona 或 Workflow 不得被固定注入", prompt)
        self.assertIn("能力可见不等于获得许可", prompt)

    def test_role_book_is_not_injected_without_persona_package(self) -> None:
        session = {
            **self.session,
            "roleBookRevisionId": "role-book:companion-present-v1:1:2",
        }
        config = replace(
            self.config,
            role_book_resolver=lambda value: (
                "这位伙伴已经形成的稳定工作画像：\n- 已验证能力：能够维护个人记忆投影"
            ),
        )

        prompt = config.system_prompt_for_session(session)

        self.assertNotIn("<agent-profile>", prompt)
        self.assertNotIn("能够维护个人记忆投影", prompt)
        self.assertNotIn("<persona", prompt)
        self.assertEqual(prompt.count("<durable-memory-policy>"), 1)
        self.assertIn("memory_capture", prompt)
        self.assertNotIn('name="persona"', prompt)

    def test_missing_role_book_adds_no_placeholder_or_negative_status_block(
        self,
    ) -> None:
        config = replace(
            self.config,
            role_book_resolver=lambda _value: "",
        )

        prompt = config.system_prompt_for_session(
            {**self.session, "roleBookRevisionId": ""}
        )

        self.assertNotIn("<agent-profile>", prompt)
        self.assertNotIn("revision_not_pinned", prompt)
        self.assertNotIn("尚未安全固定", prompt)
        self.assertEqual(prompt.count("<durable-memory-policy>"), 1)

    def test_missing_project_guide_routes_the_session_to_the_bootstrap_skill(
        self,
    ) -> None:
        project = self.root / "new-project"
        project.mkdir()

        prompt = self.config.system_prompt_for_session(
            {
                **self.session,
                "workspaceRoots": [str(project)],
                "projectContextEnabled": True,
            }
        )

        self.assertIn('name="project_context_bootstrap"', prompt)
        self.assertIn("skill_load", prompt)
        self.assertIn("bootstrap-project-context", prompt)
        self.assertIn("resourceRevision=missing", prompt)

    def test_system_root_is_authority_not_project_bootstrap_context(self) -> None:
        project = self.root / "project-after-system-root"
        project.mkdir()

        system_only = self.config.system_prompt_for_session(
            {
                **self.session,
                "workspaceRoots": ["/"],
                "projectContextEnabled": True,
            }
        )
        with_project = self.config.system_prompt_for_session(
            {
                **self.session,
                "workspaceRoots": ["/", str(project)],
                "projectContextEnabled": True,
            }
        )

        self.assertNotIn('name="project_context_bootstrap"', system_only)
        self.assertIn('name="project_context_bootstrap"', with_project)

    def test_existing_project_guide_suppresses_the_bootstrap_layer(self) -> None:
        project = self.root / "existing-project"
        project.mkdir()
        (project / "AGENTS.md").write_text("# Project guide\n", encoding="utf-8")

        prompt = self.config.system_prompt_for_session(
            {
                **self.session,
                "workspaceRoots": [str(project)],
                "projectContextEnabled": True,
            }
        )

        self.assertNotIn('name="project_context_bootstrap"', prompt)
        self.assertNotIn("bootstrap-project-context", prompt)

    def test_memory_curation_profile_uses_a_dedicated_data_only_prompt(self) -> None:
        prompt = self.config.system_prompt_for_session(
            {
                **self.session,
                "toolProfileVersion": "memory-curation-v1",
                "roleId": "companion-present-v1",
            }
        )

        self.assertIn("governed personal-memory curation engine", prompt)
        self.assertIn("Return exactly one JSON object", prompt)
        self.assertIn("Never call tools", prompt)
        self.assertNotIn("<agent-profile>", prompt)
        self.assertNotIn("<durable-memory-policy>", prompt)
        self.assertNotIn("<persona", prompt)

    def test_ordinary_agent_template_never_injects_room_lifecycle_contract(
        self,
    ) -> None:
        prompt = replace(
            self.config,
            protocol_version="2",
        ).system_prompt_for_session(
            {
                **self.session,
                "agentTemplateId": "worker",
                "agentTemplateVersion": "1",
            }
        )

        self.assertIn('<responsibility-profile id="worker">', prompt)
        self.assertIn("在一个明确 TaskBrief 内产出并验证真实改动", prompt)
        self.assertIn("<capability-policy>", prompt)
        self.assertNotIn("<room-work>", prompt)
        self.assertNotIn("room_commit", prompt)
        self.assertNotIn('name="collaboration_role"', prompt)

    def test_ordinary_coordinator_has_a_session_policy_not_a_room_role(self) -> None:
        prompt = replace(
            self.config,
            protocol_version="2",
        ).system_prompt_for_session(
            {
                **self.session,
                "mode": "coordinator",
                "agentTemplateId": "",
            }
        )

        self.assertIn('<session-mode kind="coordinator">', prompt)
        self.assertIn("若本轮同时提供 <room-context>", prompt)
        self.assertIn("以其中的 Room 身份", prompt)
        self.assertNotIn("不是 Room，也没有 Room Dispatch", prompt)
        self.assertNotIn("<room-work>", prompt)
        self.assertNotIn("room_commit", prompt)
        self.assertIn('name="session_mode_policy"', prompt)

    def test_environment_model_slot_is_scoped_to_the_pi_child(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    "RAG_IME_APP_SUPPORT_DIR": str(self.root / "support"),
                    "RAG_IME_PI_EXECUTABLE": str(self.fake_pi),
                    "RAG_IME_PI_ENABLED": "1",
                },
                clear=True,
            ),
            mock.patch(
                "rag_ime.pi.config.load_deepseek_config",
                return_value=DeepSeekConfig(
                    api_base_url="https://gateway.example/v1",
                    api_key="test-secret",
                    model="deepseek-v4-flash",
                ),
            ),
        ):
            config = PiRuntimeConfig.from_environment()

        self.assertTrue(config.model_configured)
        self.assertEqual(config.provider, "deepseek")
        self.assertEqual(config.model, "deepseek-v4-flash")
        self.assertNotIn("test-secret", repr(config))
        child = config.child_environment()
        self.assertEqual(child["DEEPSEEK_API_KEY"], "test-secret")
        self.assertNotIn("RAG_IME_PI_DEBUG_CONTEXT_DIR", child)
        self.assertNotIn("RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES", child)
        self.assertNotIn("RAG_IME_PI_DEBUG_CONTEXT_MAX_CALLS", child)
        self.assertNotIn("RAG_IME_DEEPSEEK_API_KEY", child)
        config.prepare_agent_config()
        models_path = config.agent_dir / "models.json"
        models_text = models_path.read_text(encoding="utf-8")
        self.assertIn("https://gateway.example/v1", models_text)
        self.assertIn("$DEEPSEEK_API_KEY", models_text)
        self.assertNotIn("test-secret", models_text)
        self.assertEqual(models_path.stat().st_mode & 0o777, 0o600)
        provider = json.loads(models_text)["providers"]["deepseek"]
        self.assertTrue(provider["compat"]["supportsReasoningEffort"])
        self.assertTrue(provider["compat"]["supportsUsageInStreaming"])
        self.assertEqual(provider["compat"]["thinkingFormat"], "deepseek")
        self.assertNotIn("modelOverrides", provider)

    def test_pi_remote_provider_uses_valid_macos_system_proxy_and_bypasses_localhost(
        self,
    ) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    "RAG_IME_APP_SUPPORT_DIR": str(self.root / "support"),
                    "RAG_IME_PI_EXECUTABLE": str(self.fake_pi),
                    "RAG_IME_PI_ENABLED": "1",
                },
                clear=True,
            ),
            mock.patch(
                "rag_ime.pi.config.getproxies",
                return_value={
                    "http": "http://127.0.0.1:7897",
                    "https": "http://127.0.0.1:7897",
                    "no": "internal.example",
                    "socks": "socks5://127.0.0.1:7897",
                },
            ),
            mock.patch(
                "rag_ime.pi.config.load_deepseek_config",
                return_value=DeepSeekConfig(
                    api_base_url="https://gateway.example/v1",
                    api_key="test-secret",
                    model="deepseek-v4-flash",
                ),
            ),
        ):
            config = PiRuntimeConfig.from_environment()

        child = config.child_environment()
        self.assertEqual(child["NODE_USE_ENV_PROXY"], "1")
        self.assertEqual(child["HTTP_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(child["HTTPS_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(child["http_proxy"], "http://127.0.0.1:7897")
        self.assertEqual(child["https_proxy"], "http://127.0.0.1:7897")
        self.assertEqual(
            child["NO_PROXY"],
            "internal.example,127.0.0.1,localhost,::1",
        )
        self.assertEqual(child["no_proxy"], child["NO_PROXY"])
        self.assertNotIn("ALL_PROXY", child)
        self.assertNotIn("all_proxy", child)
        config.prepare_agent_config()
        models_text = (config.agent_dir / "models.json").read_text(encoding="utf-8")
        self.assertIn('"baseUrl": "https://gateway.example/v1"', models_text)
        self.assertNotIn("test-secret", models_text)

    def test_models_file_rejects_literal_provider_credentials_only(self) -> None:
        config = replace(
            self.config,
            provider="custom",
            model="custom-model",
            provider_environment={
                "NODE_USE_ENV_PROXY": "1",
                "CUSTOM_API_KEY": "literal-provider-secret",
            },
            model_providers={
                "custom": {
                    "baseUrl": "https://gateway.example/v1",
                    "apiKey": "literal-provider-secret",
                    "models": [{"id": "custom-model"}],
                }
            },
        )

        with self.assertRaisesRegex(
            PiRuntimeError,
            "models.json must not contain provider credentials",
        ):
            config.prepare_agent_config()

    def test_pi_system_proxy_can_be_disabled_and_rejects_socks_only_proxy(
        self,
    ) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"RAG_IME_PI_SYSTEM_PROXY": "off"},
                clear=True,
            ),
            mock.patch(
                "rag_ime.pi.config.getproxies",
                return_value={"https": "socks5://127.0.0.1:7897"},
            ),
            mock.patch(
                "rag_ime.pi.config.load_deepseek_config",
                return_value=None,
            ),
        ):
            config = PiRuntimeConfig.from_environment()

        child = config.child_environment()
        self.assertNotIn("HTTP_PROXY", child)
        self.assertNotIn("HTTPS_PROXY", child)
        self.assertNotIn("NODE_USE_ENV_PROXY", child)
        self.assertNotIn("NO_PROXY", child)

    def test_debug_context_persistence_requires_explicit_opt_in(self) -> None:
        debug_directory = self.root / "private-debug-context"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "RAG_IME_APP_SUPPORT_DIR": str(self.root / "support"),
                    "RAG_IME_PI_EXECUTABLE": str(self.fake_pi),
                    "RAG_IME_PI_ENABLED": "1",
                    "RAG_IME_PI_DEBUG_CONTEXT_DIR": str(debug_directory),
                    "RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES": str(5 * 1024 * 1024 * 1024),
                    "RAG_IME_PI_DEBUG_CONTEXT_MAX_CALLS": "128",
                },
                clear=True,
            ),
            mock.patch(
                "rag_ime.pi.config.load_deepseek_config",
                return_value=DeepSeekConfig(
                    api_base_url="https://gateway.example/v1",
                    api_key="test-secret",
                    model="deepseek-v4-flash",
                ),
            ),
        ):
            config = PiRuntimeConfig.from_environment()

        child = config.child_environment()
        self.assertEqual(child["RAG_IME_PI_DEBUG_CONTEXT_DIR"], str(debug_directory))
        self.assertEqual(
            child["RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES"],
            str(5 * 1024 * 1024 * 1024),
        )
        self.assertEqual(child["RAG_IME_PI_DEBUG_CONTEXT_MAX_CALLS"], "128")

    def test_native_deepseek_endpoint_keeps_native_thinking_contract(self) -> None:
        provider = _deepseek_pi_provider(
            "https://api.deepseek.com/v1",
            model="deepseek-v4-flash",
        )

        self.assertTrue(provider["compat"]["supportsReasoningEffort"])
        self.assertTrue(provider["compat"]["supportsUsageInStreaming"])
        self.assertTrue(
            provider["compat"]["requiresReasoningContentOnAssistantMessages"]
        )
        self.assertEqual(provider["compat"]["thinkingFormat"], "deepseek")
        self.assertNotIn("modelOverrides", provider)

    def test_deepseek_reasoning_can_be_explicitly_disabled_without_hostname_inference(
        self,
    ) -> None:
        provider = _deepseek_pi_provider(
            "https://gateway.example/v1",
            model="deepseek-v4-flash",
            supports_reasoning=False,
            supports_reasoning_effort=False,
            supports_usage_in_streaming=False,
            requires_reasoning_content=False,
            thinking_format="openai",
        )

        self.assertFalse(provider["compat"]["supportsReasoningEffort"])
        self.assertFalse(provider["compat"]["supportsUsageInStreaming"])
        self.assertEqual(provider["compat"]["thinkingFormat"], "openai")
        self.assertFalse(provider["modelOverrides"]["deepseek-v4-flash"]["reasoning"])

    def test_opencode_provider_file_is_translated_without_persisting_secrets(
        self,
    ) -> None:
        provider_path = self.root / "pikey.md"
        provider_path.write_text(
            json.dumps(
                {
                    "provider": {
                        "openai": {
                            "options": {
                                "baseURL": "https://gpt.example/v1",
                                "apiKey": "gpt-test-secret",
                            },
                            "models": {
                                "gpt-5.6-luna": {
                                    "name": "GPT-5.6 Luna",
                                    "limit": {"context": 1_050_000, "output": 128_000},
                                    "variants": {
                                        "low": {},
                                        "medium": {},
                                        "high": {},
                                        "xhigh": {},
                                        "max": {},
                                    },
                                }
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        with (
            mock.patch.dict(
                os.environ,
                {
                    "RAG_IME_APP_SUPPORT_DIR": str(self.root / "support"),
                    "RAG_IME_PI_EXECUTABLE": str(self.fake_pi),
                    "RAG_IME_PI_ENABLED": "1",
                    "RAG_IME_PI_PROVIDER_CONFIG": str(provider_path),
                    "RAG_IME_PI_PROVIDER": "gpt",
                    "RAG_IME_PI_MODEL": "gpt-5.6-luna",
                },
                clear=True,
            ),
            mock.patch(
                "rag_ime.pi.config.load_deepseek_config",
                return_value=DeepSeekConfig(
                    api_base_url="https://deepseek.example/v1",
                    api_key="deepseek-test-secret",
                    model="deepseek-v4-flash",
                ),
            ),
        ):
            config = PiRuntimeConfig.from_environment()

        self.assertTrue(config.model_configured)
        self.assertEqual(config.provider, "gpt")
        self.assertEqual(config.model, "gpt-5.6-luna")
        self.assertEqual(
            config.child_environment()["RAG_IME_PI_GPT_API_KEY"], "gpt-test-secret"
        )
        self.assertNotIn("gpt-test-secret", repr(config))
        config.prepare_agent_config()
        models_text = (config.agent_dir / "models.json").read_text(encoding="utf-8")
        self.assertIn('"gpt"', models_text)
        self.assertIn('"deepseek"', models_text)
        self.assertIn("$RAG_IME_PI_GPT_API_KEY", models_text)
        self.assertIn('"supportsDeveloperRole": false', models_text)
        self.assertNotIn("gpt-test-secret", models_text)
        self.assertNotIn("deepseek-test-secret", models_text)
        managed_models = json.loads(models_text)
        imported_provider = managed_models["providers"]["gpt"]
        self.assertNotIn("modelCatalogProvider", imported_provider)
        self.assertEqual(
            imported_provider["models"],
            [
                {
                    "id": "gpt-5.6-luna",
                    "name": "GPT-5.6 Luna",
                    "reasoning": True,
                    "thinkingLevelMap": {
                        "off": None,
                        "minimal": None,
                        "low": "low",
                        "medium": "medium",
                        "high": "high",
                        "xhigh": "xhigh",
                        "max": "max",
                    },
                    "contextWindow": 1_050_000,
                    "maxTokens": 128_000,
                }
            ],
        )
        self.assertEqual(imported_provider["api"], "openai-completions")
        self.assertEqual(
            imported_provider["compat"],
            {"supportsToolSearch": False},
        )
        self.assertNotIn("input", imported_provider["models"][0])
        self.assertEqual(
            config.resolved_model_reference({**self.session, "modelProfile": ""}),
            ("gpt", "gpt-5.6-luna"),
        )

    def test_provider_models_remain_exact_pi_api_models_without_persona_aliasing(
        self,
    ) -> None:
        provider_path = self.root / "timeline-provider.json"
        provider_path.write_text(
            json.dumps(
                {
                    "provider": {
                        "openai": {
                            "options": {
                                "baseURL": "https://gpt.example/v1",
                                "apiKey": "gpt-test-secret",
                            },
                            "models": {
                                "gpt-5.6-luna": {"name": "GPT-5.6 Luna"},
                                "gpt-5.6-terra": {"name": "GPT-5.6 Terra"},
                                "gpt-5.6-sol": {"name": "GPT-5.6 Sol"},
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        bundle = load_pi_provider_config(provider_path)
        models = bundle.providers["gpt"]["models"]
        self.assertEqual(
            [model["id"] for model in models],
            ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"],
        )
        self.assertEqual(
            models,
            [
                {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna"},
                {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra"},
                {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol"},
            ],
        )
        self.assertNotIn("modelCatalogProvider", bundle.providers["gpt"])

        with (
            mock.patch.dict(
                os.environ,
                {
                    "RAG_IME_APP_SUPPORT_DIR": str(self.root / "support"),
                    "RAG_IME_PI_EXECUTABLE": str(self.fake_pi),
                    "RAG_IME_PI_ENABLED": "1",
                    "RAG_IME_PI_PROVIDER_CONFIG": str(provider_path),
                    "RAG_IME_PI_PROVIDER": "gpt",
                    "RAG_IME_PI_MODEL": "gpt-5.6-luna",
                },
                clear=True,
            ),
            mock.patch(
                "rag_ime.pi.config.load_deepseek_config", return_value=None
            ),
        ):
            config = PiRuntimeConfig.from_environment()
        self.assertEqual(config.model, "gpt-5.6-luna")

        legacy_session = self.store.create(
            title="legacy family alias",
            model_profile="gpt/gpt-5.6",
        )
        self.assertEqual(
            config.resolved_model_reference(legacy_session), ("gpt", "gpt-5.6-luna")
        )

    def test_imported_provider_only_copies_whitelisted_valid_model_metadata(
        self,
    ) -> None:
        provider_path = self.root / "provider-input-capabilities.json"
        provider_path.write_text(
            json.dumps(
                {
                    "provider": {
                        "openai": {
                            "options": {
                                "baseURL": "https://gpt.example/v1",
                                "apiKey": "gpt-test-secret",
                            },
                            "models": {
                                "gpt-5.6-luna": {
                                    "name": ["not", "a", "string"],
                                    "api": "anthropic-messages",
                                    "baseUrl": "https://untrusted.example/v1",
                                    "headers": {"Authorization": "untrusted-secret"},
                                    "input": ["image"],
                                    "reasoning": False,
                                    "thinkingLevelMap": {"max": "untrusted-max"},
                                    "contextWindow": 1,
                                    "maxTokens": 2,
                                    "limit": {"context": True, "output": -1},
                                    "variants": {
                                        "off": {},
                                        "low": {},
                                        "minimal": "not-an-object",
                                        "max": [],
                                        "untrusted-level": {},
                                    },
                                },
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        bundle = load_pi_provider_config(provider_path)

        self.assertEqual(
            bundle.providers["gpt"]["models"],
            [
                {
                    "id": "gpt-5.6-luna",
                    "reasoning": True,
                    "thinkingLevelMap": {
                        "off": "none",
                        "minimal": None,
                        "low": "low",
                        "medium": None,
                        "high": None,
                        "xhigh": None,
                        "max": None,
                    },
                }
            ],
        )
        self.assertNotIn("untrusted-secret", json.dumps(bundle.providers))

    def test_pi_max_mapping_exposes_the_distinct_max_reasoning_level(self) -> None:
        model = public_pi_model(
            {
                "provider": "gpt",
                "id": "gpt-5.6-luna",
                "name": "GPT-5.6 Luna",
                "reasoning": True,
                "thinkingLevelMap": {"max": "max"},
                "input": ["text", "image"],
            }
        )

        self.assertEqual(
            model["thinkingLevels"],
            ["off", "minimal", "low", "medium", "high", "max"],
        )

    def test_imported_provider_rejects_credentials_and_query_parameters_in_url(
        self,
    ) -> None:
        for endpoint in (
            "https://user:secret@gpt.example/v1",
            "https://gpt.example/v1?token=secret",
            "https://gpt.example/v1#secret",
        ):
            with self.subTest(endpoint=endpoint):
                provider_path = self.root / f"provider-{len(endpoint)}.json"
                provider_path.write_text(
                    json.dumps(
                        {
                            "provider": {
                                "openai": {
                                    "options": {
                                        "baseURL": endpoint,
                                        "apiKey": "test-only",
                                    },
                                    "models": {"gpt-test": {"name": "GPT Test"}},
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(PiProviderConfigError, "不能包含凭据"):
                    load_pi_provider_config(provider_path)

    def test_missing_model_credential_is_a_distinct_runtime_state(self) -> None:
        config = PiRuntimeConfig(
            enabled=True,
            executable=self.fake_pi,
            agent_dir=self.root / "missing-model-config",
            session_dir=self.root / "missing-model-sessions",
            logs_dir=self.root / "missing-model-logs",
            model_configured=False,
            model_configuration_error="尚未配置对话模型",
        )
        runtime = PiRuntimeHostManager(
            config=config, sessions=self.store, events=self.events
        )

        status = runtime.runtime_status()
        self.assertEqual(status["status"], "needs_configuration")
        self.assertFalse(status["capabilities"]["modelConfigured"])
        with self.assertRaisesRegex(PiRuntimeError, "尚未配置对话模型"):
            runtime.ensure(str(self.session["id"]))

    def test_audited_extension_receives_only_scoped_gateway_capability(self) -> None:
        extension = self.root / "rag-ime-control.ts"
        extension.write_text("export default function () {}\n", encoding="utf-8")
        config = PiRuntimeConfig(
            enabled=True,
            executable=self.fake_pi,
            agent_dir=self.root / "tool-config",
            session_dir=self.root / "tool-sessions",
            logs_dir=self.root / "tool-logs",
            extension_path=extension,
            tools=("memory",),
            tool_gateway_token="scoped-test-token",
            plugin_approval_token="plugin-only-test-token",
        )
        environment = config.child_environment(session=self.session)

        self.assertEqual(environment["RAG_IME_AGENT_TOOL_TOKEN"], "scoped-test-token")
        self.assertEqual(environment["RAG_IME_TOOL_GATEWAY_TOKEN"], "scoped-test-token")
        self.assertEqual(
            environment["RAG_IME_PLUGIN_APPROVAL_TOKEN"],
            "plugin-only-test-token",
        )
        self.assertEqual(environment["RAG_IME_AGENT_SESSION_ID"], self.session["id"])
        self.assertEqual(environment["RAG_IME_AGENT_SESSION_MODE"], "assistant")
        self.assertNotIn("RAG_IME_MANAGEMENT_TOKEN", environment)

    def test_provider_error_without_text_has_readable_terminal_message(self) -> None:
        message = pi_message_payload(
            {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": "fetch failed",
                "timestamp": 106,
            },
            session_id=str(self.session["id"]),
            turn_id="turn:provider-error",
        ).to_payload()

        self.assertEqual(message["status"], "failed")
        self.assertEqual(
            [block["type"] for block in message["blocks"]],
            ["text", "error"],
        )
        self.assertIn(
            "模型服务未能生成最终回复",
            message["blocks"][0]["data"]["text"],
        )
        self.assertEqual(message["blocks"][1]["data"]["message"], "fetch failed")

    def test_invalidated_oauth_explains_account_recovery(self) -> None:
        from rag_ime.agent_runtime_failure import classify_runtime_failure

        error = "Encountered invalidated oauth token for user, failing request"
        message = pi_message_payload(
            {"role": "assistant", "content": [], "stopReason": "error",
             "errorMessage": error, "timestamp": 106},
            session_id=str(self.session["id"]), turn_id="turn:oauth-error",
        ).to_payload()
        self.assertEqual(message["status"], "failed")
        self.assertIn("模型账号", message["blocks"][0]["data"]["text"])
        self.assertIn("重新登录", message["blocks"][0]["data"]["text"])
        self.assertEqual(message["blocks"][1]["data"]["message"], error)
        classification = classify_runtime_failure(error, had_tool_activity=False)
        self.assertEqual(classification.reason_code, "provider_auth_failure")
        self.assertFalse(classification.retryable)

    def test_deep_search_transport_prompt_is_not_exposed_as_user_message(self) -> None:
        message = pi_message_payload(
            {
                "role": "user",
                "timestamp": 104,
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<rag-ime-deep-search-context>internal</rag-ime-deep-search-context>\n"
                            "<rag-ime-user-query>最近做了什么？</rag-ime-user-query>\n"
                            "本地时间：2026-07-14"
                        ),
                    }
                ],
            },
            session_id=str(self.session["id"]),
            turn_id="turn:deep",
        ).to_payload()

        self.assertEqual(message["blocks"][0]["data"]["text"], "最近做了什么？")

    def test_transient_context_envelope_projects_only_the_user_message(self) -> None:
        internal_context = "private-workspace-context-must-not-render"
        message = pi_message_payload(
            {
                "role": "user",
                "timestamp": 105,
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "RAG_IME_TRANSIENT_CONTEXT_V1\n"
                            + json.dumps(
                                {
                                    "schemaVersion": "rag-ime.runtime-prompt.v1",
                                    "message": "立即干预：只回复 STEER-OK。",
                                    "sessionContext": internal_context,
                                    "transientContext": "another-private-context",
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        ),
                    }
                ],
            },
            session_id=str(self.session["id"]),
            turn_id="turn:steer",
        ).to_payload()

        self.assertEqual(
            message["blocks"][0]["data"]["text"],
            "立即干预：只回复 STEER-OK。",
        )
        serialized = json.dumps(message, ensure_ascii=False)
        self.assertNotIn("RAG_IME_TRANSIENT_CONTEXT_V1", serialized)
        self.assertNotIn(internal_context, serialized)

    def test_malformed_transient_context_envelope_fails_closed(self) -> None:
        message = pi_message_payload(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "RAG_IME_TRANSIENT_CONTEXT_V1\n{not-json",
                    }
                ],
            },
            session_id=str(self.session["id"]),
            turn_id="turn:malformed",
        ).to_payload()

        serialized = json.dumps(message, ensure_ascii=False)
        self.assertIn("本轮没有可展示正文", serialized)
        self.assertNotIn("RAG_IME_TRANSIENT_CONTEXT_V1", serialized)
        self.assertNotIn("not-json", serialized)

    def test_aborted_pi_message_projects_a_stopped_terminal_turn(self) -> None:
        message = pi_message_payload(
            {
                "role": "assistant",
                "content": [],
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "stopReason": "aborted",
                "errorMessage": "Request was aborted",
                "timestamp": 106,
            },
            session_id=str(self.session["id"]),
            turn_id="turn:aborted",
        ).to_payload()

        self.assertEqual(message["status"], "aborted")
        self.assertEqual(message["blocks"][0]["status"], "aborted")
        self.assertEqual(message["blocks"][0]["data"]["text"], "已停止。")
        self.assertNotIn("Request was aborted", json.dumps(message))

    def test_disabled_and_uninstalled_states_fail_closed(self) -> None:
        disabled = PiRuntimeHostManager(
            config=PiRuntimeConfig(
                enabled=False,
                executable=self.fake_pi,
                agent_dir=self.root / "disabled-config",
                session_dir=self.root / "disabled-sessions",
                logs_dir=self.root / "disabled-logs",
            ),
            sessions=self.store,
            events=self.events,
        )
        with self.assertRaisesRegex(PiRuntimeError, "disabled"):
            disabled.ensure(str(self.session["id"]))

        missing = PiRuntimeConfig(
            enabled=True,
            executable=self.root / "missing-pi",
            agent_dir=self.root / "missing-config",
            session_dir=self.root / "missing-sessions",
            logs_dir=self.root / "missing-logs",
        )
        self.assertEqual(
            PiRuntimeHostManager(
                config=missing, sessions=self.store, events=self.events
            ).runtime_status()["status"],
            "not_installed",
        )


if __name__ == "__main__":
    unittest.main()
