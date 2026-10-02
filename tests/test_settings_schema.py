from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from rag_ime.agent_service import agent_service_from_settings
from rag_ime.settings_schema import default_settings, flatten_settings, settings_schema, unflatten_settings


class SettingsSchemaTests(unittest.TestCase):
    def test_schema_contains_management_console_v3_sections(self) -> None:
        schema = settings_schema()
        section_ids = {str(item["id"]) for item in schema["sections"]}

        self.assertIn("identity", section_ids)
        self.assertIn("interaction", section_ids)
        self.assertIn("display", section_ids)
        self.assertIn("rag", section_ids)
        self.assertIn("models", section_ids)
        self.assertIn("activeRag", section_ids)
        self.assertIn("lexiconOrganization", section_ids)
        self.assertNotIn("externalMemorySources", section_ids)
        self.assertIn("agent", section_ids)
        self.assertIn("pinyin", section_ids)
        self.assertIn("privacy", section_ids)

    def test_defaults_include_user_customization_controls(self) -> None:
        defaults = default_settings()

        self.assertEqual(defaults["identity"]["productName"], "PAW")
        self.assertEqual(defaults["identity"]["assistantName"], "Agent")
        self.assertEqual(defaults["identity"]["tagline"], "记得你，也陪你做事")
        self.assertFalse(defaults["interaction"]["composition"]["showPrediction"])
        self.assertTrue(defaults["interaction"]["composition"]["showOnlyRime"])
        self.assertEqual(defaults["models"]["hot"], "minimind_ime_v2")
        self.assertFalse(defaults["interaction"]["postCommit"]["showPendingStatus"])
        self.assertEqual(defaults["interaction"]["postCommit"]["idleTriggerMs"], 220)
        self.assertEqual(defaults["interaction"]["postCommit"]["minDeltaChars"], 2)
        self.assertEqual(defaults["interaction"]["postCommit"]["maxCallsPer10s"], 6)
        self.assertEqual(defaults["interaction"]["postCommit"]["cooldownMs"], 1500)
        self.assertEqual(defaults["interaction"]["postCommit"]["pendingStatusDelayMs"], 600)
        self.assertEqual(defaults["interaction"]["postCommit"]["panelTtlMs"], 4000)
        self.assertEqual(defaults["display"]["badges"]["model"], "模")
        self.assertEqual(defaults["display"]["badges"]["action"], "生成")
        self.assertEqual(defaults["display"]["colors"]["action"], "blue")
        self.assertTrue(defaults["rag"]["lanes"]["bm25Raw"])
        self.assertEqual(defaults["rag"]["weights"]["bm25Raw"], 1.0)
        self.assertEqual(defaults["rag"]["weights"]["vectorRaw"], 1.05)
        self.assertEqual(defaults["rag"]["hybrid"]["budgetMs"], 400)
        self.assertEqual(defaults["activeRag"]["shortcut"], "ctrl+.")
        self.assertTrue(defaults["activeRag"]["capture"]["accessibility"])
        self.assertTrue(defaults["activeRag"]["capture"]["clipboardFallback"])
        self.assertEqual(defaults["activeRag"]["defaultPlacement"], "replace_selection")
        self.assertEqual(defaults["activeRag"]["maxCandidates"], 1)
        self.assertEqual(defaults["activeRag"]["latencyBudgetMs"], 8000)
        self.assertEqual(defaults["activeRag"]["quickModel"], "deepseek/deepseek-v4-flash")
        self.assertEqual(defaults["activeRag"]["quickThinkingLevel"], "high")
        self.assertNotIn("visualModel", defaults["activeRag"])
        self.assertTrue(defaults["activeRag"]["allowRemoteModel"])
        self.assertTrue(defaults["privacy"]["allowRemoteModelForActiveRag"])
        self.assertEqual(
            set(defaults["models"]),
            {"modelId", "hot", "path", "promptMode", "maxTokens", "temperature", "topP"},
        )
        self.assertEqual(defaults["voice"]["refinementModel"], "inherit")
        self.assertEqual(defaults["voice"]["refinementThinkingLevel"], "off")
        self.assertEqual(defaults["pinyin"]["fuzzyProfile"], "sichuan-mild")
        self.assertTrue(defaults["pinyin"]["rerankUsesFuzzy"])
        self.assertTrue(defaults["pinyin"]["pairs"]["sSh"])
        self.assertTrue(defaults["pinyin"]["pairs"]["ongOn"])
        self.assertFalse(defaults["pinyin"]["pairs"]["nL"])
        self.assertFalse(defaults["privacy"]["debugIncludeText"])
        self.assertEqual(defaults["privacy"]["debugContextDirectory"], "")
        self.assertEqual(defaults["privacy"]["debugContextMaxGiB"], 5)
        self.assertEqual(defaults["privacy"]["debugContextMaxCallsPerTurn"], 128)
        self.assertEqual(defaults["context"]["tokenBudget"], 4096)
        self.assertEqual(defaults["context"]["reservedOutputTokens"], 1024)
        self.assertEqual(defaults["context"]["recentInputCharMaximum"], 12000)
        self.assertEqual(defaults["context"]["axNodeMaximum"], 160)
        self.assertEqual(defaults["context"]["axCharMaximum"], 12000)
        self.assertFalse(defaults["agent"]["pi"]["enabled"])
        self.assertEqual(defaults["agent"]["pi"]["idleTimeoutSeconds"], 900)
        self.assertTrue(defaults["agent"]["pi"]["systemProxy"])
        self.assertEqual(defaults["agent"]["pi"]["defaultRoleId"], "companion-future-v1")
        self.assertEqual(defaults["agent"]["defaults"]["modelReference"], "openai-codex/gpt-6.1-sol")
        self.assertEqual(defaults["agent"]["defaults"]["thinkingLevel"], "max")
        self.assertEqual(defaults["agent"]["defaults"]["executionMode"], "per_action")
        self.assertTrue(defaults["lexiconOrganization"]["enabled"])
        self.assertEqual(defaults["lexiconOrganization"]["runsPerDay"], 2)
        self.assertTrue(defaults["memory"]["automaticOrganization"]["enabled"])
        self.assertEqual(
            defaults["memory"]["automaticOrganization"]["model"],
            "openai-codex/gpt-6.1-sol",
        )
        self.assertEqual(
            defaults["memory"]["automaticOrganization"]["thinkingLevel"],
            "max",
        )
        self.assertEqual(
            defaults["memory"]["automaticOrganization"]["runsPerDay"],
            2,
        )
        self.assertTrue(
            defaults["memory"]["automaticOrganization"]["includeAgentDialogue"]
        )
        self.assertTrue(defaults["memory"]["dreaming"]["enabled"])
        self.assertEqual(
            defaults["memory"]["dreaming"]["model"],
            "openai-codex/gpt-6.1-sol",
        )
        self.assertEqual(defaults["memory"]["dreaming"]["thinkingLevel"], "max")
        self.assertEqual(defaults["memory"]["dreaming"]["runsPerDay"], 2)
        self.assertNotIn("externalSources", defaults["memory"])
        self.assertEqual(defaults["memory"]["recall"]["detailLevel"], "compact")
        self.assertEqual(
            defaults["knowledgeLibrary"]["embedding"]["provider"],
            "environment",
        )
        self.assertEqual(
            defaults["knowledgeLibrary"]["embedding"]["denseBackend"],
            "sqlite-exact",
        )

        fields = {field["key"]: field for section in settings_schema()["sections"] for field in section["fields"]}
        self.assertEqual(fields["identity.productName"]["maxLength"], 24)
        self.assertEqual(fields["identity.assistantName"]["maxLength"], 24)
        self.assertEqual(fields["identity.tagline"]["maxLength"], 48)
        self.assertFalse(fields["interaction.composition.showPrediction"]["default"])
        self.assertTrue(fields["interaction.composition.showOnlyRime"]["default"])
        self.assertEqual(fields["interaction.postCommit.maxCallsPer10s"]["default"], 6)
        self.assertEqual(fields["interaction.postCommit.panelTtlMs"]["default"], 4000)
        self.assertEqual(fields["interaction.postCommit.panelTtlMs"]["max"], 30000)
        self.assertEqual(fields["models.hot"]["default"], "minimind_ime_v2")
        self.assertEqual(fields["models.hot"]["label"], "推理 Profile")
        self.assertIn("不属于 Pi Provider 模型", fields["models.hot"]["description"])
        self.assertEqual(fields["models.modelId"]["maxLength"], 128)
        self.assertEqual(fields["models.path"]["maxLength"], 1024)
        self.assertEqual(fields["models.maxTokens"]["max"], 64)
        self.assertEqual(fields["lexiconOrganization.runsPerDay"]["min"], 1)
        self.assertEqual(fields["lexiconOrganization.runsPerDay"]["max"], 6)
        self.assertEqual(fields["models.temperature"]["max"], 2.0)
        self.assertEqual(fields["models.topP"]["max"], 1.0)
        self.assertEqual(fields["privacy.debugContextMaxGiB"]["default"], 5)
        self.assertEqual(fields["privacy.debugContextMaxGiB"]["max"], 64)
        self.assertEqual(
            fields["privacy.debugContextMaxCallsPerTurn"]["default"],
            128,
        )
        self.assertEqual(
            fields["privacy.debugContextDirectory"]["maxLength"],
            1024,
        )
        self.assertEqual(fields["context.tokenBudget"]["default"], 4096)
        self.assertEqual(fields["context.recentInputMaximum"]["label"], "最近完整输入条数上限")
        self.assertEqual(fields["context.recentInputCharMaximum"]["label"], "最近输入字符预算")
        self.assertEqual(fields["context.axNodeMaximum"]["label"], "AX 节点数预算")
        self.assertEqual(fields["context.axCharMaximum"]["label"], "AX 字符预算")
        # Persona selection belongs to the Agent partner flow, not the generic
        # runtime settings form. Keep the internal default without exposing a
        # raw role id as a second, conflicting UI owner.
        self.assertNotIn("agent.pi.defaultRoleId", fields)
        self.assertNotIn("agent.pi.toolProfile", fields)
        self.assertEqual(
            fields["knowledgeLibrary.embedding.provider"]["options"],
            [
                "environment",
                "none",
                "local-hash",
                "sentence-transformers",
                "mlx-bert",
                "openai-compatible",
            ],
        )
        self.assertEqual(
            fields["knowledgeLibrary.embedding.provider"]["applyMode"],
            "restart_knowledge_worker",
        )
        self.assertEqual(
            fields["knowledgeLibrary.embedding.dimensions"]["max"],
            65536,
        )
        self.assertNotIn("agent.pi.startup", fields)
        self.assertEqual(fields["agent.defaults.modelReference"]["type"], "pi-model-or-inherit")
        self.assertEqual(fields["agent.defaults.thinkingLevel"]["type"], "pi-thinking")
        self.assertEqual(fields["agent.defaults.thinkingLevel"]["modelKey"], "agent.defaults.modelReference")
        self.assertEqual(
            fields["agent.defaults.executionMode"]["options"],
            ["read_only", "per_action", "workspace_managed", "full_trust"],
        )
        self.assertEqual(fields["agent.defaults.executionMode"]["applyMode"], "next_session")
        self.assertEqual(fields["activeRag.quickModel"]["type"], "pi-model")
        self.assertEqual(fields["activeRag.latencyBudgetMs"]["label"], "生成框最长等待")
        self.assertEqual(fields["activeRag.latencyBudgetMs"]["default"], 8000)
        self.assertEqual(fields["activeRag.latencyBudgetMs"]["min"], 2000)
        self.assertEqual(fields["activeRag.latencyBudgetMs"]["max"], 30000)
        self.assertEqual(
            fields["activeRag.quickThinkingLevel"]["options"],
            ["off", "minimal", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(fields["activeRag.quickThinkingLevel"]["default"], "high")
        self.assertIn(
            "默认使用高思考",
            fields["activeRag.quickThinkingLevel"]["description"],
        )
        self.assertNotIn("activeRag.visualModel", fields)
        self.assertNotIn("models.activeRag", fields)
        self.assertNotIn("models.offlineCleanup", fields)
        self.assertEqual(
            fields["voice.refinementModel"]["type"],
            "pi-model-or-inherit",
        )
        self.assertEqual(
            fields["memory.automaticOrganization.model"]["type"],
            "pi-model",
        )
        self.assertEqual(
            fields["memory.automaticOrganization.model"]["label"],
            "自动整理模型",
        )
        self.assertIn(
            "Pi Provider",
            fields["memory.automaticOrganization.model"]["description"],
        )
        self.assertEqual(
            fields["memory.automaticOrganization.thinkingLevel"]["type"],
            "pi-thinking",
        )
        self.assertEqual(
            fields["memory.automaticOrganization.thinkingLevel"]["modelKey"],
            "memory.automaticOrganization.model",
        )
        self.assertEqual(
            fields["memory.automaticOrganization.includeAgentDialogue"]["label"],
            "整理 Agent 对话摘要",
        )
        self.assertIn(
            "不可独立支持事实",
            fields["memory.automaticOrganization.includeAgentDialogue"]["description"],
        )
        self.assertEqual(
            fields["memory.dreaming.model"]["type"],
            "pi-model",
        )
        self.assertEqual(
            fields["memory.dreaming.thinkingLevel"]["type"],
            "pi-thinking",
        )
        self.assertEqual(
            fields["voice.refinementThinkingLevel"]["modelKey"],
            "voice.refinementModel",
        )
        self.assertEqual(
            fields["memory.automaticOrganization.enabled"]["label"],
            "自动整理",
        )
        self.assertEqual(
            fields["memory.dreaming.enabled"]["label"],
            "记忆做梦",
        )
        self.assertFalse(
            any(key.startswith("memory.externalSources.codexMemory") for key in fields)
        )

    def test_flatten_roundtrip(self) -> None:
        original = {"interaction": {"postCommit": {"panelTtlMs": 4200}}, "display": {"badges": {"model": "模"}}}

        self.assertEqual(unflatten_settings(flatten_settings(original)), original)

    def test_agent_default_is_future_but_an_explicit_legacy_role_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-role-default-") as temporary, ExitStack() as services:
            default_service = agent_service_from_settings(
                Path(temporary) / "default.sqlite",
                {"agent": {"pi": {}}},
                wake_scheduler_enabled=False,
            )
            services.callback(default_service.close)
            legacy_service = agent_service_from_settings(
                Path(temporary) / "legacy.sqlite",
                {"agent": {"pi": {"defaultRoleId": "companion-present-v1"}}},
                wake_scheduler_enabled=False,
            )
            services.callback(legacy_service.close)

            self.assertEqual(
                default_service.configuration()["configuration"]["configuration"]["sessionDefaults"]["roleId"],
                "companion-future-v1",
            )
            self.assertEqual(
                default_service.configuration()["configuration"]["configuration"]["sessionDefaults"]["modelProfile"],
                "openai-codex/gpt-6.1-sol",
            )
            self.assertEqual(
                default_service.configuration()["configuration"]["configuration"]["modelRouting"]["primary"],
                {"modelProfile": "openai-codex/gpt-6.1-sol", "thinkingLevel": "max"},
            )
            self.assertEqual(
                legacy_service.configuration()["configuration"]["configuration"]["sessionDefaults"]["roleId"],
                "companion-present-v1",
            )

    def test_every_field_declares_runtime_application_metadata(self) -> None:
        schema = settings_schema()
        required = {"description", "applyMode", "risk", "expert", "min", "max", "step", "unit", "validation", "restartComponent"}
        fields = [field for section in schema["sections"] for field in section["fields"]]

        self.assertTrue(fields)
        self.assertTrue(all(required.issubset(field) for field in fields))
        shortcut = next(field for field in fields if field["key"] == "activeRag.shortcut")
        self.assertEqual(shortcut["type"], "shortcut")
        self.assertEqual(shortcut["applyMode"], "restart_input_method")


if __name__ == "__main__":
    unittest.main()
