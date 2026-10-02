from __future__ import annotations

import tempfile
import unittest
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from rag_ime.debug_server import DebugImeService, DebugServerConfig
from rag_ime.runtime_config import RuntimeConfigResolver
from rag_ime.settings_store import ManagementSettingsStore
from rag_ime.text_utils import now_ms


class RuntimeConfigResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-runtime-config-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.store = ManagementSettingsStore(self.db_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_current_profile_clamps_legacy_composition_settings_to_rime_only(self) -> None:
        settings = self.store.get_settings(include_sensitive=True)
        settings["interaction"]["composition"].update(  # type: ignore[index]
            {"showPrediction": True, "showOnlyRime": False}
        )
        resolver = RuntimeConfigResolver(
            self.store,
            environ={
                "RAG_IME_RUNTIME_PROFILE": "production",
                "RAG_IME_RUNTIME_OVERRIDE_COMPOSITION_AI": "1",
            },
        )

        snapshot = resolver.resolve(settings=settings)

        self.assertFalse(snapshot.composition_ai)
        self.assertTrue(snapshot.show_only_rime)
        self.assertIn("composition_ai_disabled_by_profile", snapshot.safety_clamps)
        self.assertIn("composition_rime_only", snapshot.safety_clamps)
        self.assertEqual(snapshot.key_policy.composition_number_keys, "select_rime_candidate")
        effective = snapshot.effective_settings(settings)
        self.assertFalse(effective["interaction"]["composition"]["showPrediction"])  # type: ignore[index]
        self.assertTrue(effective["interaction"]["composition"]["showOnlyRime"])  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            snapshot.profile = "safe-dev"  # type: ignore[misc]

        safe_dev = RuntimeConfigResolver(
            self.store,
            environ={"RAG_IME_RUNTIME_PROFILE": "safe-dev"},
        ).resolve(settings=settings)
        self.assertFalse(safe_dev.hybrid_rag.enabled)

    def test_allowlisted_experiment_overrides_are_bounded_and_visible(self) -> None:
        resolver = RuntimeConfigResolver(
            self.store,
            environ={
                "RAG_IME_RUNTIME_PROFILE": "v1-proof",
                "RAG_IME_RUNTIME_OVERRIDE_POST_COMMIT_ENABLED": "0",
                "RAG_IME_RUNTIME_OVERRIDE_HYBRID_RAG_ENABLED": "0",
                "RAG_IME_RUNTIME_OVERRIDE_MAX_POST_COMMIT_CANDIDATES": "99",
                "RAG_IME_RUNTIME_OVERRIDE_MODEL_PROFILE": "minimind_ime_v2",
                "RAG_IME_RUNTIME_OVERRIDE_MODEL_PATH": "/tmp/minimind-ime-v2",
                "RAG_IME_RUNTIME_OVERRIDE_SHOW_SOURCE_BADGES": "0",
                "RAG_IME_RUNTIME_OVERRIDE_POST_COMMIT_NUMBER_KEYS": "select_prediction",
                "RAG_IME_UNRELATED_OVERRIDE": "ignored",
            },
        )

        snapshot = resolver.resolve()

        self.assertFalse(snapshot.post_commit.enabled)
        self.assertFalse(snapshot.hybrid_rag.enabled)
        self.assertEqual(snapshot.post_commit.max_candidates, 10)
        self.assertEqual(snapshot.model.profile_id, "minimind_ime_v2")
        self.assertEqual(snapshot.model.model_path, "/tmp/minimind-ime-v2")
        self.assertFalse(snapshot.source_badges.enabled)
        self.assertEqual(snapshot.key_policy.post_commit_number_keys, "pass_through")
        self.assertIn("ordinary_number_keys_reserved_for_rime", snapshot.safety_clamps)
        self.assertNotIn("RAG_IME_UNRELATED_OVERRIDE", snapshot.experiment_overrides)

    def test_default_post_commit_budget_and_panel_ttl_match_persisted_targets(self) -> None:
        snapshot = RuntimeConfigResolver(
            self.store,
            environ={"RAG_IME_RUNTIME_PROFILE": "v1-proof"},
        ).resolve()

        self.assertEqual(snapshot.post_commit.max_calls_per_10s, 6)
        self.assertEqual(snapshot.post_commit.panel_ttl_ms, 4000)
        self.assertEqual(snapshot.post_commit.model_budget_ms, 900)
        self.assertEqual(snapshot.hybrid_rag.lane_weight("bm25_raw"), 1.0)
        self.assertEqual(snapshot.hybrid_rag.lane_weight("vector_raw"), 1.05)

    def test_local_predictor_and_interaction_settings_flow_into_runtime_snapshot(self) -> None:
        self.store.update_settings(
            {
                "interaction.postCommit.modelBudgetMs": 1300,
                "interaction.postCommit.optionNumber": "disabled",
                "models.modelId": "minimind-ime-v2",
                "models.path": "/tmp/minimind-ime-v2",
                "models.hot": "minimind_ime_v2",
                "models.promptMode": "base-completion",
                "models.maxTokens": 12,
                "models.temperature": 0.2,
                "models.topP": 0.9,
            }
        )

        snapshot = RuntimeConfigResolver(
            self.store,
            environ={"RAG_IME_RUNTIME_PROFILE": "v1-proof"},
        ).resolve()

        self.assertEqual(snapshot.post_commit.model_budget_ms, 1300)
        self.assertEqual(snapshot.key_policy.option_number, "disabled")
        self.assertEqual(snapshot.model.model_id, "minimind-ime-v2")
        self.assertEqual(snapshot.model.profile_id, "minimind_ime_v2")
        self.assertEqual(snapshot.model.model_path, "/tmp/minimind-ime-v2")
        self.assertEqual(snapshot.model.prompt_mode, "base-completion")
        self.assertEqual(snapshot.model.max_tokens, 12)
        self.assertEqual(snapshot.model.temperature, 0.2)
        self.assertEqual(snapshot.model.top_p, 0.9)

    def test_snapshot_contains_effective_memory_rag_overlay_and_active_rag_config(self) -> None:
        self.store.update_settings(
            {
                "memory.enabled": False,
                "rag.lanes.bm25Raw": False,
                "rag.weights.bm25Tags": 3.25,
                "display.candidateFontSize": 17,
                "display.maxWidth": 440,
                "display.fadeAnimation": False,
                "activeRag.enabled": False,
                "activeRag.shortcut": "ctrl+r",
                "activeRag.latencyBudgetMs": 6500,
            }
        )

        snapshot = RuntimeConfigResolver(
            self.store,
            environ={"RAG_IME_RUNTIME_PROFILE": "foreground-rag-proof"},
        ).resolve()
        payload = snapshot.payload()

        self.assertFalse(snapshot.memory.enabled)
        self.assertFalse(snapshot.hybrid_rag.lane_enabled("bm25_raw"))
        self.assertEqual(snapshot.hybrid_rag.lane_weight("bm25_tags"), 3.25)
        self.assertFalse(snapshot.hybrid_rag.lane_enabled("vector_raw"))
        self.assertIn("vector_raw_unavailable", snapshot.safety_clamps)
        self.assertEqual(payload["overlayConfig"]["candidateFontSize"], 17)
        self.assertEqual(payload["overlayConfig"]["maxWidth"], 440)
        self.assertFalse(payload["overlayConfig"]["fadeAnimation"])
        self.assertEqual(
            payload["activeRag"],
            {
                "enabled": False,
                "shortcut": "ctrl+r",
                "latencyBudgetMs": 6500,
            },
        )

    def test_configured_remote_embedding_keeps_vector_lanes_enabled(self) -> None:
        snapshot = RuntimeConfigResolver(
            self.store,
            environ={
                "RAG_IME_RUNTIME_PROFILE": "foreground-rag-proof",
                "RAG_IME_EMBEDDING_PROVIDER": "openai-compatible",
                "RAG_IME_EMBEDDING_BASE_URL": "http://192.168.1.121:8000",
                "RAG_IME_EMBEDDING_MODEL": "Qwen3-Embedding-0.6B",
            },
        ).resolve()

        self.assertTrue(snapshot.hybrid_rag.lane_enabled("vector_raw"))
        self.assertTrue(snapshot.hybrid_rag.lane_enabled("vector_tag_boost"))
        self.assertNotIn("vector_raw_unavailable", snapshot.safety_clamps)

    def test_foreground_rag_proof_exposes_direct_rag_without_composition_ai(self) -> None:
        snapshot = RuntimeConfigResolver(
            self.store,
            environ={
                "RAG_IME_RUNTIME_PROFILE": "foreground-rag-proof",
                "RAG_IME_RAG_DIRECT_DISPLAY": "1",
            },
        ).resolve()

        self.assertTrue(snapshot.hybrid_rag.enabled)
        self.assertTrue(snapshot.hybrid_rag.direct_display)
        self.assertFalse(snapshot.composition_ai)

    def test_revision_is_stable_monotonic_and_persists_across_resolvers(self) -> None:
        environ = {"RAG_IME_RUNTIME_PROFILE": "v1-proof"}
        first_resolver = RuntimeConfigResolver(self.store, environ=environ)
        first = first_resolver.resolve()
        same = first_resolver.resolve()
        self.assertEqual(same.runtime_revision, first.runtime_revision)
        self.assertEqual(same.snapshot_hash, first.snapshot_hash)

        self.store.update_settings({"interaction.postCommit.maxCallsPer10s": 1})
        changed = first_resolver.resolve()
        self.assertGreater(changed.runtime_revision, first.runtime_revision)

        reopened_store = ManagementSettingsStore(self.db_path)
        try:
            reopened = RuntimeConfigResolver(reopened_store, environ=environ).resolve()
        finally:
            reopened_store.close()
        self.assertEqual(reopened.runtime_revision, changed.runtime_revision)
        self.assertEqual(reopened.snapshot_hash, changed.snapshot_hash)

        environ["RAG_IME_RUNTIME_OVERRIDE_POST_COMMIT_ENABLED"] = "0"
        overridden = first_resolver.resolve()
        self.assertGreater(overridden.runtime_revision, reopened.runtime_revision)
        self.assertFalse(overridden.post_commit.enabled)

    def test_concurrent_resolution_converges_on_one_persisted_revision(self) -> None:
        resolver = RuntimeConfigResolver(
            self.store,
            environ={"RAG_IME_RUNTIME_PROFILE": "v1-proof"},
        )

        with ThreadPoolExecutor(max_workers=8) as executor:
            snapshots = list(executor.map(lambda _index: resolver.resolve(), range(24)))

        self.assertEqual(len({item.snapshot_hash for item in snapshots}), 1)
        self.assertEqual(len({item.runtime_revision for item in snapshots}), 1)


class RuntimeConfigIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-runtime-config-integration-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.service = DebugImeService(
            DebugServerConfig(
                db_path=self.db_path,
                seed_if_empty=False,
                runtime_command_runner=lambda command, **_kwargs: subprocess.CompletedProcess(
                    command,
                    0,
                    "",
                    "",
                ),
            )
        )

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_management_and_rime_response_share_one_runtime_snapshot(self) -> None:
        self.service.settings_update(
            {
                "interaction.composition.showPrediction": True,
                "interaction.composition.showOnlyRime": False,
            }
        )
        expected = self.service.runtime_config()["runtimeConfig"]
        with patch.object(
            self.service.runtime_config_resolver,
            "resolve",
            wraps=self.service.runtime_config_resolver.resolve,
        ) as resolve:
            response = self.service.rime_suggest(
                {
                    "sessionId": "runtime-config-composition",
                    "requestSeq": 1,
                    "privacyDisposition": "allowed",
                    "rawInput": "houxuan",
                    "preedit": "houxuan",
                    "committedContext": "输入法",
                    "rimeContext": {"candidates": [{"label": "1", "text": "候选"}]},
                }
            )

        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(response["runtimeConfig"], expected)
        self.assertEqual(response["runtimeRevision"], expected["runtimeRevision"])
        self.assertEqual(response["settingsRevision"], expected["settingsRevision"])
        self.assertEqual(response["runtimeProfile"], expected["profile"])
        self.assertEqual(response["managementSettings"]["runtimeRevision"], expected["runtimeRevision"])
        self.assertEqual(response["managementSettings"]["settingsRevision"], expected["settingsRevision"])
        self.assertTrue(all(item["sourceType"] in {"rime", "raw_english"} for item in response["displayCandidates"]))
        self.assertEqual(response["assistantOverlay"]["candidates"], [])
        self.assertFalse(response["assistantOverlay"]["visible"])
        self.assertEqual(response["keyPolicy"]["numberKeys"], "select_rime_candidate")
        self.assertEqual(response["keyPolicy"]["tab"], "rime_default")

        overview = self.service.management.overview()
        self.assertEqual(overview["runtimeConfig"]["snapshotHash"], expected["snapshotHash"])
        self.assertEqual(overview["profile"], "v1-proof")

    def test_runtime_revision_survives_service_restart(self) -> None:
        before = self.service.runtime_config()["runtimeConfig"]
        update = self.service.settings_update({"interaction.postCommit.maxCallsPer10s": 1})
        changed_revision = int(update["runtimeRevision"])
        self.assertGreater(changed_revision, int(before["runtimeRevision"]))
        self.service.close()

        reopened = DebugImeService(DebugServerConfig(db_path=self.db_path, seed_if_empty=False))
        try:
            payload = reopened.runtime_config()["runtimeConfig"]
            self.assertEqual(payload["runtimeRevision"], changed_revision)
            self.assertEqual(payload["settingsRevision"], update["settingsRevision"])
        finally:
            reopened.close()

    def test_rime_suggest_applies_post_commit_trigger_settings(self) -> None:
        self.service.settings_update(
            {
                "interaction.postCommit.minDeltaChars": 2,
                "interaction.postCommit.maxCallsPer10s": 3,
                "interaction.postCommit.cooldownMs": 400,
            }
        )
        with patch("rag_ime.debug_server.configure_auto_prediction_trigger") as configure:
            self.service.rime_suggest(
                {
                    "sessionId": "runtime-trigger-settings",
                    "requestSeq": 1,
                    "privacyDisposition": "allowed",
                    "committedContext": "今天继续",
                }
            )

        configure.assert_called_once_with(
            min_delta_chars=2,
            max_calls_per_10s=3,
            ignore_cooldown_ms=400,
        )

    def test_post_commit_disabled_hard_clears_before_commit_rag_or_predictor(self) -> None:
        self.service.settings_update({"interaction.postCommit.enabled": False})
        payload = self._post_commit_payload("runtime-disabled")
        with (
            patch.object(self.service.adapter, "commit_text", side_effect=AssertionError("commit burst called")) as commit,
            patch.object(self.service.core, "retrieve_candidates_v3", side_effect=AssertionError("RAG called")) as rag,
            patch.object(self.service.predictor, "predict", side_effect=AssertionError("predictor called")) as predictor,
        ):
            response = self.service.rime_suggest(payload)

        commit.assert_not_called()
        rag.assert_not_called()
        predictor.assert_not_called()
        self.assertTrue(response["predictionSession"]["shouldClearPredictionPanel"])
        self.assertEqual(response["predictionSession"]["clearReason"], "post_commit_disabled")
        self.assertEqual(response["assistantOverlay"]["candidates"], [])
        self.assertFalse(response["assistantOverlay"]["visible"])

    def test_memory_disabled_skips_recording_and_retrieval_but_keeps_local_model_lane(self) -> None:
        self.service.settings_update(
            {
                "interaction.postCommit.enabled": True,
                "memory.enabled": False,
                "rag.hybrid.enabled": True,
            }
        )
        payload = self._post_commit_payload("runtime-memory-off")
        with (
            patch.object(self.service.adapter, "commit_text", side_effect=AssertionError("memory recording called")) as commit,
            patch.object(self.service.core, "retrieve_candidates_v3", side_effect=AssertionError("RAG called")) as rag,
        ):
            response = self.service.rime_suggest(payload)

        commit.assert_not_called()
        rag.assert_not_called()
        self.assertFalse(response["ragLane"]["called"])
        self.assertTrue(response["ragLane"]["runtimeDisabled"])
        self.assertFalse(response["runtimeConfig"]["memory"]["enabled"])
        last_prediction = self.service._last_management_prediction()
        self.assertEqual(last_prediction["foregroundContext"]["source"], "accessibility")
        self.assertTrue(last_prediction["foregroundContext"]["applied"])
        self.assertTrue(last_prediction["foregroundContext"]["commitTextMatched"])
        self.assertGreater(int(last_prediction["foregroundContext"]["capturedAtMs"]), 0)
        self.assertTrue(last_prediction["contextInjection"]["success"])

    def test_management_foreground_status_ignores_native_doctor_probes(self) -> None:
        self.service._prediction_live_trace.clear()
        self.service._prediction_live_trace.extend(
            [
                {
                    "sessionId": "squirrel-live",
                    "foregroundContext": {
                        "source": "text_input_client",
                        "applied": True,
                        "commitTextMatched": True,
                    },
                },
                {
                    "sessionId": "native-doctor",
                    "foregroundContext": {
                        "source": "missing",
                        "applied": False,
                        "reason": "foregroundText payload missing",
                    },
                },
            ]
        )

        last_prediction = self.service._last_management_prediction()

        self.assertEqual(last_prediction["foregroundContext"]["source"], "text_input_client")
        self.assertTrue(last_prediction["contextInjection"]["success"])

    def test_hybrid_rag_disabled_skips_retrieval_before_dispatch(self) -> None:
        self.service.settings_update(
            {
                "interaction.postCommit.enabled": True,
                "memory.enabled": True,
                "rag.hybrid.enabled": False,
            }
        )
        with patch.object(
            self.service.core,
            "retrieve_candidates_v3",
            side_effect=AssertionError("RAG called"),
        ) as rag:
            response = self.service.rime_suggest(self._post_commit_payload("runtime-rag-off"))

        rag.assert_not_called()
        self.assertFalse(response["ragLane"]["called"])
        self.assertTrue(response["ragLane"]["runtimeDisabled"])
        self.assertFalse(response["runtimeConfig"]["hybridRag"]["enabled"])

    def test_zero_call_budget_and_idle_setting_change_commit_burst_hot_path(self) -> None:
        self.service.settings_update(
            {
                "interaction.postCommit.enabled": True,
                "interaction.postCommit.idleTriggerMs": 775,
                "interaction.postCommit.minDeltaChars": 2,
                "interaction.postCommit.maxCallsPer10s": 0,
            }
        )
        with (
            patch.object(self.service.core, "retrieve_candidates_v3", side_effect=AssertionError("RAG called")) as rag,
            patch.object(self.service.predictor, "predict", side_effect=AssertionError("predictor called")) as predictor,
        ):
            response = self.service.rime_suggest(self._post_commit_payload("runtime-zero-budget"))

        rag.assert_not_called()
        predictor.assert_not_called()
        self.assertEqual(response["predictionTrigger"]["idleMs"], 775)
        self.assertEqual(response["predictionTrigger"]["reason"], "max_calls_per_10s")
        self.assertFalse(response["triggerDecision"]["shouldRefresh"])

    def test_settings_update_changes_overlay_contract_and_trace(self) -> None:
        self.service.settings_update(
            {
                "interaction.postCommit.panelTtlMs": 900,
                "interaction.postCommit.tabAction": "disabled",
                "display.maxPostCommitCandidates": 3,
                "display.showSourceBadge": False,
                "display.candidateFontSize": 18,
                "display.maxWidth": 460,
                "display.fadeAnimation": False,
                "activeRag.enabled": False,
                "activeRag.shortcut": "ctrl+r",
                "activeRag.latencyBudgetMs": 6500,
            }
        )
        response = self.service.rime_suggest(self._post_commit_payload("runtime-overlay"))

        overlay = response["assistantOverlay"]
        config = overlay["overlayConfig"]
        self.assertEqual(response["keyPolicy"]["tab"], "disabled")
        self.assertEqual(overlay["keyPolicy"]["tab"], "disabled")
        self.assertEqual(overlay["expiresAfterMs"], 900)
        self.assertEqual(config["expiresAfterMs"], 900)
        self.assertEqual(config["maxCandidates"], 3)
        self.assertEqual(config["candidateFontSize"], 18)
        self.assertEqual(config["maxWidth"], 460)
        self.assertFalse(config["fadeAnimation"])
        self.assertFalse(config["showSourceBadge"])
        self.assertEqual(
            config["activeRag"],
            {
                "enabled": False,
                "shortcut": "ctrl+r",
                "latencyBudgetMs": 6500,
            },
        )
        applied = [
            item
            for item in response["predictionTraceEvents"]
            if item.get("event") == "effective_runtime_config_applied"
        ]
        self.assertEqual(len(applied), 1)
        self.assertFalse(applied[0]["fields"]["activeRagEnabled"])
        self.assertEqual(applied[0]["fields"]["activeRagShortcut"], "ctrl+r")

    def test_rag_settings_change_real_query_plan_diagnostics(self) -> None:
        self.service.settings_update(
            {
                "rag.lanes.bm25Raw": False,
                "rag.weights.bm25Tags": 3.5,
            }
        )

        preview = self.service.rag_core_v3_query_preview({"query": "输入法"})

        self.assertTrue(preview["retrieval"]["called"])
        self.assertFalse(preview["lanes"]["bm25Raw"]["enabled"])
        self.assertEqual(preview["lanes"]["bm25Raw"]["count"], 0)
        self.assertEqual(preview["lanes"]["bm25Tags"]["weight"], 3.5)
        self.assertFalse(preview["lanes"]["vectorRaw"]["available"])
        self.assertEqual(
            preview["lanes"]["vectorRaw"]["skippedReason"],
            "embedding_provider_not_wired",
        )

        self.service.settings_update({"memory.enabled": False})
        disabled = self.service.rag_core_v3_query_preview({"query": "输入法"})
        self.assertFalse(disabled["retrieval"]["called"])
        self.assertEqual(disabled["fusedCandidates"], [])

        active_preview = self.service.active_rag_management_preview(
            {
                "selectedText": "解释输入法候选",
                "localOnly": True,
                "privacyDisposition": "allowed",
            }
        )
        self.assertTrue(active_preview["ok"])
        self.assertFalse(active_preview["diagnostics"]["retrieval"]["called"])
        self.assertEqual(active_preview["diagnostics"]["retrieval"]["skipReason"], "memory_disabled")

    def test_generic_settings_update_applies_active_rag_shortcut_to_user_defaults(self) -> None:
        commands: list[list[str]] = []

        def runner(command, **_kwargs):
            commands.append(list(command))
            return subprocess.CompletedProcess(command, 0, "", "")

        self.service._runtime_command_runner = runner
        result = self.service.settings_update({"activeRag.shortcut": "ctrl+r"})

        self.assertTrue(result["runtimeSync"]["attempted"])
        self.assertTrue(result["runtimeSync"]["applied"])
        self.assertIn(
            [
                "defaults",
                "write",
                "im.rime.inputmethod.Squirrel",
                "RagImeActiveRagShortcut",
                "-string",
                "ctrl+r",
            ],
            commands,
        )

        commands.clear()
        reset = self.service.settings_reset_section({"section": "activeRag"})
        self.assertTrue(reset["runtimeSync"]["attempted"])
        self.assertTrue(reset["runtimeSync"]["applied"])
        self.assertIn(
            [
                "defaults",
                "write",
                "im.rime.inputmethod.Squirrel",
                "RagImeActiveRagShortcut",
                "-string",
                "ctrl+.",
            ],
            commands,
        )

    def test_sensitive_service_request_bypasses_cache_key_and_keeps_no_text_hash(self) -> None:
        secret = "sk-never-hash-this-password"  # public-audit-secret-fixture
        payload = {
            **self._post_commit_payload("runtime-sensitive"),
            "secureInput": True,
            "rawInput": secret,
            "preedit": secret,
            "committedContext": secret,
            "compositionHash": "sha256:malicious-composition",
            "committedContextHash": "sha256:malicious-context",
            "panelSessionId": "derived-from-secret",
        }
        with patch.object(
            self.service,
            "_rime_suggest_cache_key",
            side_effect=AssertionError("sensitive payload was hashed"),
        ) as cache_key:
            response = self.service.rime_suggest(payload)

        cache_key.assert_not_called()
        transaction = response["frontendTransaction"]
        self.assertEqual(transaction["compositionHash"], "")
        self.assertEqual(transaction["committedContextHash"], "")
        self.assertNotEqual(transaction["panelSessionId"], "derived-from-secret")
        trace_blob = str(self.service.prediction_live_trace({"limit": 1}))
        self.assertNotIn(secret, trace_blob)
        self.assertNotIn("malicious-composition", trace_blob)
        self.assertNotIn("malicious-context", trace_blob)
        self.assertNotIn("sha256:", str(response))

    def test_unknown_privacy_service_request_bypasses_cache_and_predictor(self) -> None:
        payload = self._post_commit_payload("runtime-privacy-unknown")
        payload.pop("privacyDisposition")
        with patch.object(
            self.service,
            "_rime_suggest_cache_key",
            side_effect=AssertionError("unknown privacy payload was hashed"),
        ) as cache_key, patch.object(
            self.service.predictor,
            "predict",
            side_effect=AssertionError("unknown privacy payload reached predictor"),
        ) as predictor:
            response = self.service.rime_suggest(payload)

        cache_key.assert_not_called()
        predictor.assert_not_called()
        self.assertTrue(response["noStore"])
        self.assertEqual(response["privacyAssessment"]["disposition"], "unknown")
        self.assertEqual(response["displayCandidates"], [])
        self.assertEqual(response["frontendTransaction"]["committedContextHash"], "")

    @staticmethod
    def _post_commit_payload(session_id: str) -> dict[str, object]:
        context = "今天继续完成输入法预测"
        return {
            "sessionId": session_id,
            "requestSeq": 1,
            "privacyDisposition": "allowed",
            "committedContext": context,
            "commitTextPreview": "预测",
            "maxSideCandidates": 3,
            "latencyBudgetMs": 500,
            "commitBurstReady": True,
            "commitBurstDeltaChars": 8,
            "commitBurstTexts": ["预测"],
            "foregroundText": {
                "source": "accessibility",
                "surroundingBefore": context,
                "surroundingAfter": "",
                "captureAgeMs": 0,
                "capturedAtMs": now_ms(),
                "commitTextMatched": True,
                "contextGroupId": f"doc:{session_id}",
                "contextGroupLevel": "document",
            },
            "rimeContext": {"candidates": []},
        }


if __name__ == "__main__":
    unittest.main()
