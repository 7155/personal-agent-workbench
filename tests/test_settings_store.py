from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from rag_ime.settings_models import UserProfile, UserVocabularyItem
from rag_ime.settings_store import ManagementSettingsStore


class SettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-settings-store-")
        self.db_path = Path(self.tmp.name) / "settings.sqlite"
        self.store = ManagementSettingsStore(self.db_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_settings_update_writes_audit_and_reset_restores_defaults(self) -> None:
        result = self.store.update_settings({"interaction.postCommit.optionNumber": "disabled"})

        self.assertGreater(result.audit_id, 0)
        self.assertEqual(result.settings["interaction"]["postCommit"]["optionNumber"], "disabled")
        self.assertIn("interaction.postCommit.optionNumber", result.changed_keys)
        self.assertEqual(self._audit_count("settings_update"), 1)

        reset = self.store.reset_section("interaction")

        self.assertGreater(reset.audit_id, result.audit_id)
        self.assertEqual(
            reset.settings["interaction"]["postCommit"]["optionNumber"],
            "select_prediction_by_ordinal",
        )
        self.assertEqual(reset.settings["interaction"]["postCommit"]["numberKeys"], "pass_through")
        self.assertEqual(self._audit_count("settings_reset_section"), 1)

    def test_identity_names_are_editable_bounded_and_trimmed(self) -> None:
        result = self.store.update_settings(
            {
                "identity.productName": "  记川  ",
                "identity.assistantName": "阿川",
                "identity.tagline": "陪你记住，也陪你完成",
            }
        )

        self.assertEqual(result.settings["identity"]["productName"], "记川")
        self.assertEqual(result.settings["identity"]["assistantName"], "阿川")
        self.assertEqual(result.settings["identity"]["tagline"], "陪你记住，也陪你完成")
        with self.assertRaisesRegex(ValueError, "at least 1 character"):
            self.store.update_settings({"identity.productName": "   "})
        with self.assertRaisesRegex(ValueError, "at most 24 character"):
            self.store.update_settings({"identity.assistantName": "长" * 25})

    def test_remote_model_enable_requires_confirm_text(self) -> None:
        with self.assertRaises(ValueError):
            self.store.update_settings({"activeRag.allowRemoteModel": True})

        result = self.store.update_settings(
            {"activeRag.allowRemoteModel": True},
            confirm_text="ALLOW REMOTE MODEL",
        )

        self.assertTrue(result.settings["activeRag"]["allowRemoteModel"])

    def test_mineru_settings_only_accept_a_bounded_loopback_port(self) -> None:
        result = self.store.update_settings(
            {
                "knowledgeLibrary.parser.mineru.enabled": True,
                "knowledgeLibrary.parser.mineru.port": 30001,
                "knowledgeLibrary.parser.mineru.timeoutSeconds": 7200,
            }
        )

        mineru = result.settings["knowledgeLibrary"]["parser"]["mineru"]
        self.assertTrue(mineru["enabled"])
        self.assertEqual(mineru["port"], 30001)
        self.assertEqual(mineru["timeoutSeconds"], 7200)
        with self.assertRaisesRegex(ValueError, "must be >= 1024"):
            self.store.update_settings({"knowledgeLibrary.parser.mineru.port": 80})
        with self.assertRaisesRegex(ValueError, "must be <= 65535"):
            self.store.update_settings({"knowledgeLibrary.parser.mineru.port": 65536})
        with self.assertRaisesRegex(ValueError, "must be >= 60"):
            self.store.update_settings({"knowledgeLibrary.parser.mineru.timeoutSeconds": 30})

    def test_knowledge_embedding_profile_is_persisted_without_a_secret_value(self) -> None:
        result = self.store.update_settings(
            {
                "knowledgeLibrary.embedding.provider": "openai-compatible",
                "knowledgeLibrary.embedding.model": "bge-small-zh",
                "knowledgeLibrary.embedding.baseUrl": "https://embedding.example.test",
                "knowledgeLibrary.embedding.dimensions": 1024,
                "knowledgeLibrary.embedding.secretReference": "PAW_EMBEDDING_API_KEY",
                "knowledgeLibrary.embedding.queryPrefix": "query: ",
                "knowledgeLibrary.embedding.documentPrefix": "passage: ",
                "knowledgeLibrary.embedding.denseBackend": "usearch",
            }
        )

        embedding = result.settings["knowledgeLibrary"]["embedding"]
        self.assertEqual("openai-compatible", embedding["provider"])
        self.assertEqual("bge-small-zh", embedding["model"])
        self.assertEqual("PAW_EMBEDDING_API_KEY", embedding["secretReference"])
        self.assertNotIn("apiKey", embedding)
        with self.assertRaisesRegex(ValueError, "must be one of"):
            self.store.update_settings(
                {"knowledgeLibrary.embedding.provider": "unknown-provider"}
            )

    def test_debug_context_archive_requires_a_bounded_absolute_directory(self) -> None:
        result = self.store.update_settings(
            {
                "privacy.debugIncludeText": True,
                "privacy.debugContextDirectory": "/Volumes/External/Cheng/context-snapshots",
                "privacy.debugContextMaxGiB": 5,
                "privacy.debugContextMaxCallsPerTurn": 128,
            }
        )

        privacy = result.settings["privacy"]
        self.assertEqual(
            privacy["debugContextDirectory"],
            "/Volumes/External/Cheng/context-snapshots",
        )
        self.assertEqual(privacy["debugContextMaxGiB"], 5)
        with self.assertRaisesRegex(ValueError, "absolute or ~/ path"):
            self.store.update_settings(
                {"privacy.debugContextDirectory": "relative/context-snapshots"}
            )
        with self.assertRaisesRegex(ValueError, "bounded subdirectory"):
            self.store.update_settings({"privacy.debugContextDirectory": "/"})
        with self.assertRaisesRegex(ValueError, "must be <= 64"):
            self.store.update_settings({"privacy.debugContextMaxGiB": 65})

    def test_one_shot_lightning_model_reference_and_thinking_are_bounded(self) -> None:
        result = self.store.update_settings(
            {
                "activeRag.quickModel": "deepseek/deepseek-v4-flash",
                "activeRag.quickThinkingLevel": "high",
            }
        )

        self.assertEqual(result.settings["activeRag"]["quickThinkingLevel"], "high")
        off_result = self.store.update_settings({"activeRag.quickThinkingLevel": "off"})
        self.assertEqual(off_result.settings["activeRag"]["quickThinkingLevel"], "off")
        with self.assertRaisesRegex(ValueError, "provider/model reference"):
            self.store.update_settings({"activeRag.quickModel": "deepseek-v4-flash"})

    def test_voice_refinement_model_can_inherit_or_select_a_pi_model(self) -> None:
        inherited = self.store.update_settings(
            {
                "voice.refinementModel": "inherit",
                "voice.refinementThinkingLevel": "off",
            }
        )
        selected = self.store.update_settings(
            {
                "voice.refinementModel": "gpt/gpt-5.6-luna",
                "voice.refinementThinkingLevel": "high",
            }
        )

        self.assertEqual(inherited.settings["voice"]["refinementModel"], "inherit")
        self.assertEqual(selected.settings["voice"]["refinementModel"], "gpt/gpt-5.6-luna")
        self.assertEqual(selected.settings["voice"]["refinementThinkingLevel"], "high")
        with self.assertRaisesRegex(ValueError, "provider/model reference"):
            self.store.update_settings({"voice.refinementModel": "gpt-5.6-luna"})
        with self.assertRaisesRegex(ValueError, "must be one of"):
            self.store.update_settings({"voice.refinementThinkingLevel": "unbounded"})

    def test_legacy_unconsumed_model_fields_are_ignored_on_read(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO management_settings(key, value_json, updated_at_ms, updated_by)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "models",
                    json.dumps(
                        {
                            "hot": "minimind_ime_v2",
                            "activeRag": "deepseek-v4",
                            "offlineCleanup": "deepseek-v4",
                            "main": "unused",
                            "quality": "unused",
                            "embedding": "unused",
                        }
                    ),
                    1,
                    "legacy-test",
                ),
            )

        settings = self.store.get_settings(include_sensitive=True)

        self.assertEqual(settings["models"]["hot"], "minimind_ime_v2")
        self.assertEqual(settings["models"]["modelId"], "")
        self.assertEqual(settings["models"]["maxTokens"], 8)
        self.assertNotIn("activeRag", settings["models"])
        self.assertNotIn("offlineCleanup", settings["models"])

    def test_local_predictor_settings_are_bounded_and_paths_are_absolute(self) -> None:
        model_path = Path(self.tmp.name) / "MiniMind"
        result = self.store.update_settings(
            {
                "models.modelId": "minimind-ime-v2",
                "models.hot": "minimind_ime_v2",
                "models.path": str(model_path),
                "models.promptMode": "base-completion",
                "models.maxTokens": 12,
                "models.temperature": 0.2,
                "models.topP": 0.9,
                "interaction.postCommit.modelBudgetMs": 1200,
                "interaction.postCommit.optionNumber": "disabled",
            }
        )

        self.assertEqual(result.settings["models"]["modelId"], "minimind-ime-v2")
        self.assertEqual(result.settings["models"]["path"], str(model_path))
        self.assertEqual(result.settings["models"]["maxTokens"], 12)
        self.assertEqual(result.settings["models"]["temperature"], 0.2)
        self.assertEqual(result.settings["models"]["topP"], 0.9)
        self.assertEqual(result.settings["interaction"]["postCommit"]["modelBudgetMs"], 1200)
        self.assertEqual(result.settings["interaction"]["postCommit"]["optionNumber"], "disabled")
        with self.assertRaisesRegex(ValueError, "absolute or ~/ path"):
            self.store.update_settings({"models.path": "relative/MiniMind"})
        with self.assertRaisesRegex(ValueError, "bounded model directory"):
            self.store.update_settings({"models.path": str(Path.home())})
        with self.assertRaisesRegex(ValueError, "must be <= 64"):
            self.store.update_settings({"models.maxTokens": 65})
        with self.assertRaisesRegex(ValueError, "must be finite"):
            self.store.update_settings({"models.temperature": float("nan")})

    def test_lightning_off_setting_is_preserved(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO management_settings(key, value_json, updated_at_ms, updated_by)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "activeRag",
                    json.dumps(
                        {
                            "quickModel": "deepseek/deepseek-v4-flash",
                            "quickThinkingLevel": "off",
                        }
                    ),
                    1,
                    "legacy-test",
                ),
            )

        settings = self.store.get_settings(include_sensitive=True)

        self.assertEqual(settings["activeRag"]["quickModel"], "deepseek/deepseek-v4-flash")
        self.assertEqual(settings["activeRag"]["quickThinkingLevel"], "off")

    def test_dotted_leaf_update_preserves_persisted_sibling_overrides(self) -> None:
        self.store.update_settings(
            {
                "interaction.postCommit.idleTriggerMs": 180,
                "interaction.postCommit.maxCallsPer10s": 6,
                "interaction.postCommit.cooldownMs": 600,
                "interaction.postCommit.panelTtlMs": 5000,
            }
        )

        self.store.update_settings({"interaction.postCommit.minDeltaChars": 2})
        persisted = self.store.get_settings(include_sensitive=True)
        post_commit = persisted["interaction"]["postCommit"]

        self.assertEqual(post_commit["idleTriggerMs"], 180)
        self.assertEqual(post_commit["minDeltaChars"], 2)
        self.assertEqual(post_commit["maxCallsPer10s"], 6)
        self.assertEqual(post_commit["cooldownMs"], 600)
        self.assertEqual(post_commit["panelTtlMs"], 5000)

    def test_voice_hotwords_accept_technical_terms_and_keep_the_enabled_state_bound(self) -> None:
        result = self.store.update_settings(
            {
                "voice.hotwords": ["GPT-5.6", "API Key", "SK", "C++"],
                "voice.hotwordsEnabled": True,
            }
        )

        self.assertTrue(result.settings["voice"]["hotwordsEnabled"])
        self.assertEqual(
            result.settings["voice"]["hotwords"],
            ["GPT-5.6", "API Key", "SK", "C++"],
        )

    def test_voice_hotwords_cannot_be_enabled_with_an_empty_or_invalid_list(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one word"):
            self.store.update_settings({"voice.hotwordsEnabled": True})
        with self.assertRaisesRegex(ValueError, "unsupported characters"):
            self.store.update_settings(
                {
                    "voice.hotwords": ["bad|word"],
                    "voice.hotwordsEnabled": True,
                }
            )

    def test_profile_save_and_activate_dry_run(self) -> None:
        saved = self.store.save_profile(
            UserProfile(
                profile_id="focused-writing",
                profile_kind="interaction_profile",
                label="Focused Writing",
                description="longer post-commit TTL",
                settings={"interaction.postCommit.panelTtlMs": 6000},
            )
        )
        listed = self.store.list_profiles(kind="interaction_profile")
        dry_run = self.store.activate_profile_dry_run("focused-writing")

        self.assertTrue(saved["ok"])
        self.assertEqual(len(listed["items"]), 1)
        self.assertTrue(dry_run["ok"])
        self.assertTrue(dry_run["dryRun"])
        self.assertIn("scripts/restart_rag_ime_runtime.sh", dry_run["commands"])

    def test_vocabulary_add_edit_delete_and_rime_export(self) -> None:
        added = self.store.save_vocabulary_item(
            UserVocabularyItem(
                vocab_id="",
                surface="StableCandidateSnapshot",
                aliases=("候选快照",),
                pinyin="hou xuan kuai zhao",
                tags=("输入法",),
                priority=100,
                scope="project:wisdom-weasel-rag-ime",
            )
        )
        preview = self.store.rime_export_preview()
        denied = self.store.rime_export_apply(target_file=str(Path(self.tmp.name) / "rag_ime.user.dict.yaml"), confirm_text="wrong")
        applied = self.store.rime_export_apply(
            target_file=str(Path(self.tmp.name) / "rag_ime.user.dict.yaml"),
            confirm_text="EXPORT RIME",
        )
        deleted = self.store.delete_vocabulary_item(str(added["vocabId"]))

        self.assertTrue(added["ok"])
        self.assertEqual(preview["entryCount"], 1)
        self.assertIn("StableCandidateSnapshot", preview["text"])
        self.assertFalse(denied["ok"])
        self.assertTrue(applied["ok"])
        self.assertTrue(Path(applied["targetFile"]).exists())
        self.assertTrue(deleted["ok"])

    def _audit_count(self, action: str) -> int:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            row = conn.execute("SELECT COUNT(*) FROM management_audit_log WHERE action = ?", (action,)).fetchone()
        return int(row[0])


if __name__ == "__main__":
    unittest.main()
