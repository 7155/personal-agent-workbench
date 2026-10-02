from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from rag_ime.memory_maintenance_settings import MemoryMaintenanceSettings
from rag_ime.owner_memory_maintenance import main as maintenance_main
from rag_ime.settings_store import ManagementSettingsStore


class MemoryMaintenanceSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-ime-maintenance-settings-"
        )
        self.db_path = Path(self.temporary.name) / "rag-ime.sqlite"
        self.store = ManagementSettingsStore(self.db_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_defaults_enable_both_lanes_twice_daily(self) -> None:
        settings = MemoryMaintenanceSettings.load(self.db_path)

        self.assertTrue(settings.automatic_organization_enabled)
        self.assertTrue(settings.include_agent_dialogue)
        self.assertTrue(settings.dreaming_enabled)
        self.assertEqual(
            settings.automatic_organization_model,
            "openai-codex/gpt-6.1-sol",
        )
        self.assertEqual(settings.automatic_organization_thinking_level, "max")
        self.assertEqual(settings.dreaming_model, "openai-codex/gpt-6.1-sol")
        self.assertEqual(settings.dreaming_thinking_level, "max")
        self.assertEqual(settings.automatic_organization_interval_seconds, 43_200)
        self.assertEqual(settings.dreaming_interval_seconds, 43_200)
        self.assertEqual(settings.recall_detail_level, "compact")
        self.assertEqual(settings.timeline_max_items, 2)
        self.assertTrue(settings.catalog_consolidation_enabled)
        self.assertEqual(settings.catalog_consolidation_cadence_days, 7)
        self.assertEqual(
            settings.catalog_consolidation_interval_seconds,
            7 * 24 * 60 * 60,
        )
        self.assertTrue(
            settings.as_dict()["automaticOrganization"]["includeAgentDialogue"]
        )
        self.assertTrue(settings.as_dict()["catalogConsolidation"]["enabled"])

    def test_management_updates_drive_next_maintenance_run(self) -> None:
        self.store.update_settings(
            {
                "memory.automaticOrganization.enabled": False,
                "memory.automaticOrganization.model": "deepseek/deepseek-v4-pro",
                "memory.automaticOrganization.thinkingLevel": "high",
                "memory.automaticOrganization.runsPerDay": 4,
                "memory.automaticOrganization.includeAgentDialogue": False,
                "memory.dreaming.model": "gpt/gpt-5.6-sol",
                "memory.dreaming.thinkingLevel": "low",
                "memory.dreaming.runsPerDay": 1,
                "memory.catalogConsolidation.enabled": False,
                "memory.catalogConsolidation.cadenceDays": 14,
                "memory.recall.detailLevel": "balanced",
                "memory.recall.timelineEnabled": False,
            }
        )

        settings = MemoryMaintenanceSettings.load(self.db_path)

        self.assertFalse(settings.automatic_organization_enabled)
        self.assertEqual(settings.automatic_organization_model, "deepseek/deepseek-v4-pro")
        self.assertEqual(settings.automatic_organization_thinking_level, "high")
        self.assertFalse(settings.include_agent_dialogue)
        self.assertEqual(settings.dreaming_model, "gpt/gpt-5.6-sol")
        self.assertEqual(settings.dreaming_thinking_level, "low")
        self.assertEqual(settings.automatic_organization_interval_seconds, 21_600)
        self.assertEqual(settings.dreaming_interval_seconds, 86_400)
        self.assertFalse(settings.catalog_consolidation_enabled)
        self.assertEqual(settings.catalog_consolidation_cadence_days, 14)
        self.assertEqual(
            settings.catalog_consolidation_interval_seconds,
            14 * 24 * 60 * 60,
        )
        self.assertEqual(settings.recall_detail_level, "balanced")
        self.assertFalse(settings.timeline_recall_enabled)

    def test_memory_master_disables_maintenance_lanes_without_deleting_memory(self) -> None:
        self.store.update_settings({"memory.enabled": False})

        disabled = MemoryMaintenanceSettings.load(self.db_path)

        self.assertFalse(disabled.memory_enabled)
        self.assertFalse(disabled.automatic_organization_enabled)
        self.assertFalse(disabled.dreaming_enabled)
        self.assertFalse(disabled.as_dict()["enabled"])
        self.assertFalse(disabled.as_dict()["automaticOrganization"]["enabled"])
        self.assertFalse(disabled.as_dict()["dreaming"]["enabled"])
        self.assertFalse(disabled.as_dict()["catalogConsolidation"]["enabled"])

        # Re-enabling restores the independent lane defaults; no memory data
        # table is touched by resolving the switch.
        self.store.update_settings({"memory.enabled": True})
        enabled = MemoryMaintenanceSettings.load(self.db_path)
        self.assertTrue(enabled.memory_enabled)
        self.assertTrue(enabled.automatic_organization_enabled)
        self.assertTrue(enabled.dreaming_enabled)

    def test_preverified_snapshot_reads_settings_without_replaying_migrations(self) -> None:
        self.store.update_settings(
            {"memory.recall.detailLevel": "balanced"}
        )

        with (
            patch(
                "rag_ime.settings_store.apply_database_migrations",
                side_effect=AssertionError("migration replay is forbidden"),
            ) as migrate,
            patch("rag_ime.settings_store._purge_transport_metadata") as purge,
        ):
            settings = MemoryMaintenanceSettings.load(
                self.db_path,
                preverified_schema=True,
            )

        self.assertEqual(settings.recall_detail_level, "balanced")
        migrate.assert_not_called()
        purge.assert_not_called()

    def test_legacy_explicit_deepseek_choice_is_preserved_with_provider_prefix(self) -> None:
        self.store.update_settings(
            {"memory.dreaming.model": "deepseek-v4-flash"}
        )

        settings = MemoryMaintenanceSettings.load(self.db_path)

        self.assertEqual(
            settings.dreaming_model,
            "deepseek/deepseek-v4-flash",
        )


    def test_non_provider_memory_model_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider/model reference"):
            self.store.update_settings(
                {"memory.dreaming.model": "deepseek-v3"}
            )
    def test_legacy_cli_flags_delegate_to_gateway_without_reading_database(self) -> None:
        output = io.StringIO()
        with (
            patch(
                "rag_ime.owner_memory_maintenance.run_gateway_memory_maintenance",
                return_value={
                    "schemaVersion": "rag-ime.gateway-memory-maintenance-job.v1",
                    "ok": True,
                    "jobId": "memory-maintenance:test",
                    "state": "completed",
                    "result": {"ok": True, "results": []},
                },
            ) as gateway_run,
            redirect_stdout(output),
        ):
            exit_code = maintenance_main(
                [
                    "--db-path",
                    str(self.db_path.with_name("must-not-be-opened.sqlite")),
                    "--gateway-url",
                    "http://127.0.0.1:18768",
                    "--project",
                    "sample-project",
                    "--manual",
                    "--auto-apply",
                ]
            )

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["state"], "completed")
        gateway_run.assert_called_once()
        call = gateway_run.call_args
        self.assertEqual(call.args[0], "http://127.0.0.1:18768")
        self.assertEqual(call.args[1]["project"], "sample-project")
        self.assertTrue(call.args[1]["manual"])
        self.assertNotIn("dbPath", call.args[1])
        self.assertNotIn("model", call.args[1])


if __name__ == "__main__":
    unittest.main()
