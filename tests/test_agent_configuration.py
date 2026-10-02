from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from rag_ime.agent_configuration import (
    AgentConfigurationConflict,
    AgentConfigurationStore,
    AgentControlEventHub,
    default_agent_configuration,
    runtime_policy_from_configuration,
)
from rag_ime.db import apply_database_migrations


class AgentConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-agent-configuration-")
        self.store = AgentConfigurationStore(Path(self.tmp.name) / "agent.sqlite")
        self.store.initialize(
            default_agent_configuration(
                enabled=False,
                model_profile="deepseek/deepseek-chat",
            )
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_product_default_routes_visible_and_private_collaboration_models(self) -> None:
        configuration = default_agent_configuration()
        self.assertEqual(configuration["sessionDefaults"]["roleId"], "companion-future-v1")
        self.assertEqual(
            configuration["sessionDefaults"]["modelProfile"],
            "openai-codex/gpt-6.1-sol",
        )
        self.assertEqual(
            default_agent_configuration(role_id="vcp-v1")["sessionDefaults"]["roleId"],
            "companion-future-v1",
        )
        self.assertEqual(
            configuration["modelRouting"],
            {
                "primary": {
                    "modelProfile": "openai-codex/gpt-6.1-sol",
                    "thinkingLevel": "max",
                },
                "traceDiagnostic": {
                    "modelProfile": "openai-codex/gpt-6.1-sol",
                    "thinkingLevel": "high",
                },
                "toolAgent": {
                    "modelProfile": "openai-codex/gpt-6.1-sol",
                    "thinkingLevel": "max",
                },
                "subagent": {
                    "modelProfile": "openai-codex/gpt-6.1-sol",
                    "thinkingLevel": "max",
                },
                "roomCoordinator": {
                    "modelProfile": "openai-codex/gpt-6.1-sol",
                    "thinkingLevel": "high",
                },
            },
        )

        skill_routing = configuration["skillRouting"]
        self.assertIn("systematic-debugging", skill_routing["ordinary"])
        self.assertIn("systematic-debugging", skill_routing["room"])
        self.assertIn("systematic-debugging", skill_routing["trace"])
        self.assertIn("systematic-debugging", skill_routing["agentLab"])
        self.assertIn("facilitate-room", skill_routing["room"])
        self.assertNotIn("facilitate-room", skill_routing["ordinary"])
        self.assertIn("trace-agent-diagnostics", skill_routing["trace"])
        self.assertNotIn("trace-agent-diagnostics", skill_routing["ordinary"])
        self.assertIn("agent-eval-room-optimizer", skill_routing["agentLab"])
        self.assertNotIn("agent-eval-room-optimizer", skill_routing["room"])
        self.assertTrue(
            all(
                "memory-curation" not in skill_ids
                for skill_ids in skill_routing.values()
            )
        )

    def test_model_routes_are_revisioned_without_changing_legacy_session_defaults(self) -> None:
        update = self.store.update(
            {
                "modelRouting.toolAgent": {
                    "modelProfile": "openai-codex/gpt-5.6-luna",
                    "thinkingLevel": "low",
                },
            },
            expected_revision=1,
            updated_by="models-ui",
        )

        configuration = update.snapshot["configuration"]
        self.assertEqual(
            configuration["modelRouting"]["toolAgent"],
            {
                "modelProfile": "openai-codex/gpt-5.6-luna",
                "thinkingLevel": "low",
            },
        )
        self.assertEqual(
            configuration["sessionDefaults"]["modelProfile"],
            "deepseek/deepseek-chat",
        )
        self.assertFalse(update.runtime_sync_required)

    def test_trace_diagnostic_route_is_revisioned_as_its_own_model_responsibility(self) -> None:
        update = self.store.update(
            {
                "modelRouting.traceDiagnostic": {
                    "modelProfile": "openai-codex/gpt-5.6-sol",
                    "thinkingLevel": "high",
                },
            },
            expected_revision=1,
            updated_by="models-ui",
        )

        self.assertEqual(
            update.snapshot["configuration"]["modelRouting"]["traceDiagnostic"],
            {
                "modelProfile": "openai-codex/gpt-5.6-sol",
                "thinkingLevel": "high",
            },
        )
        self.assertNotIn("roomCoordinator", update.changed_keys)

    def test_skill_routes_are_revisioned_without_restarting_existing_sessions(self) -> None:
        update = self.store.update(
            {
                "skillRouting.ordinary": [
                    "systematic-debugging",
                    "test-driven-implementation",
                ],
            },
            expected_revision=1,
            updated_by="settings-ui",
        )

        self.assertEqual(
            update.snapshot["configuration"]["skillRouting"]["ordinary"],
            ["systematic-debugging", "test-driven-implementation"],
        )
        self.assertEqual(update.changed_keys, ("skillRouting.ordinary",))
        self.assertFalse(update.runtime_sync_required)
        self.assertEqual(update.snapshot["sync"]["state"], "synchronized")

    def test_owning_private_skill_cannot_be_removed_from_its_scenario(self) -> None:
        update = self.store.update(
            {"skillRouting.room": []},
            expected_revision=1,
            updated_by="settings-ui",
        )

        self.assertEqual(
            update.snapshot["configuration"]["skillRouting"]["room"],
            ["facilitate-room"],
        )

    def test_skill_routes_reject_cross_scenario_private_skills(self) -> None:
        for key, skill_id in (
            ("skillRouting.ordinary", "facilitate-room"),
            ("skillRouting.room", "trace-agent-diagnostics"),
            ("skillRouting.trace", "agent-eval-room-optimizer"),
            ("skillRouting.agentLab", "trace-agent-diagnostics"),
        ):
            with self.subTest(key=key, skill_id=skill_id), self.assertRaisesRegex(
                ValueError,
                "belongs to",
            ):
                self.store.update(
                    {key: [skill_id]},
                    expected_revision=1,
                    updated_by="settings-ui",
                )

    def test_startup_rewrites_a_persisted_legacy_role_id_once(self) -> None:
        path = Path(self.tmp.name) / "legacy-agent.sqlite"
        legacy = default_agent_configuration()
        legacy["sessionDefaults"]["roleId"] = "zhiyou-v1"
        with closing(sqlite3.connect(path)) as conn, conn:
            apply_database_migrations(conn)
            conn.execute(
                """
                INSERT INTO agent_configuration_state(
                    singleton_id, revision, configuration_json, applied_revision,
                    sync_state, sync_error, updated_at_ms, updated_by
                ) VALUES (1, 7, ?, 7, 'synchronized', '', 1, 'legacy-test')
                """,
                (json.dumps(legacy, ensure_ascii=False, sort_keys=True),),
            )

        store = AgentConfigurationStore(path)
        store.initialize(default_agent_configuration())
        snapshot = store.snapshot()

        self.assertEqual(snapshot["configuration"]["sessionDefaults"]["roleId"], "companion-present-v1")
        self.assertEqual(snapshot["revision"], 8)
        self.assertEqual(snapshot["lastEventId"], "agent-control:1")
        with closing(sqlite3.connect(path)) as conn:
            stored = conn.execute(
                "SELECT configuration_json FROM agent_configuration_state WHERE singleton_id = 1"
            ).fetchone()[0]
        self.assertNotIn("zhiyou-v1", stored)

    def test_startup_migrates_flattened_model_routes_without_losing_choices(self) -> None:
        path = Path(self.tmp.name) / "legacy-model-routing.sqlite"
        legacy = default_agent_configuration()
        legacy["sessionDefaults"].pop("modelProfile")
        legacy["modelRouting"] = {
            "sessionModelProfile": "gpt/gpt-5.6-luna",
            "sessionThinkingLevel": "max",
            "toolAgentModelProfile": "openai-codex/gpt-5.4",
            "toolAgentThinkingLevel": "low",
            "roomPartnerModelProfile": "openai-codex/gpt-5.4",
            "roomPartnerThinkingLevel": "medium",
        }
        with closing(sqlite3.connect(path)) as conn, conn:
            apply_database_migrations(conn)
            conn.execute(
                """
                INSERT INTO agent_configuration_state(
                    singleton_id, revision, configuration_json, applied_revision,
                    sync_state, sync_error, updated_at_ms, updated_by
                ) VALUES (1, 4, ?, 4, 'synchronized', '', 1, 'legacy-model-ui')
                """,
                (json.dumps(legacy, ensure_ascii=False, sort_keys=True),),
            )

        store = AgentConfigurationStore(path)
        store.initialize(default_agent_configuration())
        snapshot = store.snapshot()
        configuration = snapshot["configuration"]

        self.assertEqual(snapshot["revision"], 5)
        self.assertEqual(
            configuration["sessionDefaults"]["modelProfile"],
            "gpt/gpt-5.6-luna",
        )
        self.assertEqual(
            configuration["modelRouting"],
            {
                "primary": {
                    "modelProfile": "gpt/gpt-5.6-luna",
                    "thinkingLevel": "max",
                },
                "traceDiagnostic": {
                    "modelProfile": "openai-codex/gpt-6.1-sol",
                    "thinkingLevel": "high",
                },
                "toolAgent": {
                    "modelProfile": "openai-codex/gpt-5.4",
                    "thinkingLevel": "low",
                },
                "subagent": {
                    "modelProfile": "openai-codex/gpt-5.4",
                    "thinkingLevel": "medium",
                },
                "roomCoordinator": {
                    "modelProfile": "openai-codex/gpt-5.4",
                    "thinkingLevel": "medium",
                },
            },
        )
        self.assertEqual(snapshot["lastEventId"], "agent-control:1")

        store_again = AgentConfigurationStore(path)
        store_again.initialize(default_agent_configuration())
        self.assertEqual(store_again.snapshot()["revision"], 5)

    def test_startup_persists_the_new_trace_route_for_an_older_nested_configuration(self) -> None:
        path = Path(self.tmp.name) / "legacy-nested-model-routing.sqlite"
        legacy = default_agent_configuration()
        legacy["modelRouting"].pop("traceDiagnostic")
        with closing(sqlite3.connect(path)) as conn, conn:
            apply_database_migrations(conn)
            conn.execute(
                """
                INSERT INTO agent_configuration_state(
                    singleton_id, revision, configuration_json, applied_revision,
                    sync_state, sync_error, updated_at_ms, updated_by
                ) VALUES (1, 9, ?, 9, 'synchronized', '', 1, 'legacy-model-ui')
                """,
                (json.dumps(legacy, ensure_ascii=False, sort_keys=True),),
            )

        store = AgentConfigurationStore(path)
        store.initialize(default_agent_configuration())
        snapshot = store.snapshot()

        self.assertEqual(snapshot["revision"], 10)
        self.assertEqual(
            snapshot["configuration"]["modelRouting"]["traceDiagnostic"],
            {
                "modelProfile": "openai-codex/gpt-6.1-sol",
                "thinkingLevel": "high",
            },
        )
        with closing(sqlite3.connect(path)) as conn:
            persisted = json.loads(
                conn.execute(
                    "SELECT configuration_json FROM agent_configuration_state WHERE singleton_id = 1"
                ).fetchone()[0]
            )
        self.assertIn("traceDiagnostic", persisted["modelRouting"])

    def test_startup_adds_skill_routes_to_legacy_configuration_once(self) -> None:
        path = Path(self.tmp.name) / "legacy-skill-routing.sqlite"
        legacy = default_agent_configuration()
        legacy.pop("skillRouting")
        with closing(sqlite3.connect(path)) as conn, conn:
            apply_database_migrations(conn)
            conn.execute(
                """
                INSERT INTO agent_configuration_state(
                    singleton_id, revision, configuration_json, applied_revision,
                    sync_state, sync_error, updated_at_ms, updated_by
                ) VALUES (1, 5, ?, 5, 'synchronized', '', 1, 'legacy-settings')
                """,
                (json.dumps(legacy, ensure_ascii=False, sort_keys=True),),
            )

        store = AgentConfigurationStore(path)
        store.initialize(default_agent_configuration())
        snapshot = store.snapshot()

        self.assertEqual(snapshot["revision"], 6)
        self.assertEqual(
            snapshot["configuration"]["skillRouting"],
            default_agent_configuration()["skillRouting"],
        )
        store_again = AgentConfigurationStore(path)
        store_again.initialize(default_agent_configuration())
        self.assertEqual(store_again.snapshot()["revision"], 6)

    def test_configuration_is_revisioned_and_rejects_stale_writers(self) -> None:
        initial = self.store.snapshot()
        update = self.store.update(
            {
                "runtime.enabled": True,
                "sessionDefaults.roleId": "companion-firstlight-v1",
            },
            expected_revision=initial["revision"],
            updated_by="mac-control",
        )

        self.assertEqual(update.snapshot["revision"], 2)
        self.assertEqual(update.snapshot["sync"]["state"], "pending")
        self.assertEqual(update.changed_keys, ("runtime.enabled", "sessionDefaults.roleId"))
        self.assertTrue(update.runtime_sync_required)
        self.assertEqual(update.event["eventId"], "agent-control:1")
        with self.assertRaisesRegex(AgentConfigurationConflict, "current 2"):
            self.store.update(
                {"coordination.enabled": True},
                expected_revision=1,
                updated_by="stale-phone",
            )

        policy = runtime_policy_from_configuration(update.snapshot["configuration"])
        self.assertTrue(policy.enabled)
        synchronized, event = self.store.mark_applied(
            2,
            runtime_status={
                "driverId": "managed-pi",
                "runtimeKind": "pi_rpc",
                "enabled": True,
                "status": "stopped",
            },
        )
        self.assertEqual(synchronized["sync"], {"state": "synchronized", "appliedRevision": 2, "error": ""})
        self.assertEqual(event["eventType"], "configuration_applied")

    def test_non_runtime_default_change_is_immediately_synchronized(self) -> None:
        update = self.store.update(
            {"sessionDefaults.toolProfileVersion": "control-center-v2"},
            expected_revision=1,
            updated_by="gateway",
        )
        self.assertFalse(update.runtime_sync_required)
        self.assertEqual(update.snapshot["sync"]["appliedRevision"], 2)
        self.assertEqual(update.snapshot["sync"]["state"], "synchronized")

    def test_non_runtime_change_does_not_hide_a_pending_runtime_apply(self) -> None:
        runtime_update = self.store.update(
            {"runtime.enabled": True},
            expected_revision=1,
            updated_by="mac-control",
        )
        defaults_update = self.store.update(
            {"sessionDefaults.toolProfileVersion": "control-center-v2"},
            expected_revision=runtime_update.snapshot["revision"],
            updated_by="phone-control",
        )

        self.assertEqual(defaults_update.snapshot["revision"], 3)
        self.assertEqual(defaults_update.snapshot["sync"]["state"], "pending")
        self.assertEqual(defaults_update.snapshot["sync"]["appliedRevision"], 1)

    def test_control_stream_replays_and_requires_snapshot_after_bad_cursor(self) -> None:
        hub = AgentControlEventHub(self.store)
        update = self.store.update(
            {"coordination.enabled": True},
            expected_revision=1,
            updated_by="mac-control",
        )
        hub.fan_out(update.event)

        stream = hub.subscribe(after_event_id="")
        self.assertEqual(next(stream), b": connected\n\n")
        replay = _sse_payload(next(stream))
        self.assertEqual(replay["eventId"], "agent-control:1")
        stream.close()

        gap_stream = hub.subscribe(after_event_id="another-stream:4")
        self.assertEqual(next(gap_stream), b": connected\n\n")
        gap = _sse_payload(next(gap_stream))
        self.assertEqual(gap["eventType"], "snapshot_required")
        self.assertEqual(gap["payload"]["snapshotEndpoint"], "/api/agent/configuration")

        live_update = self.store.update(
            {"coordination.enabled": False},
            expected_revision=update.snapshot["revision"],
            updated_by="phone-control",
        )
        hub.fan_out(live_update.event)
        live = _sse_payload(next(gap_stream))
        self.assertEqual(live["eventId"], "agent-control:2")
        gap_stream.close()

    def test_unknown_or_secret_shaped_settings_are_not_part_of_agent_contract(self) -> None:
        for changes in (
            {"runtime.apiKey": "secret"},
            {"sessionDefaults.roleId": "../../bad"},
            {"runtime.startup": "always-on"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.update(changes, expected_revision=1, updated_by="test")


def _sse_payload(chunk: bytes) -> dict[str, object]:
    data = next(
        line.removeprefix("data: ")
        for line in chunk.decode("utf-8").splitlines()
        if line.startswith("data: ")
    )
    payload = json.loads(data)
    assert isinstance(payload, dict)
    return payload


if __name__ == "__main__":
    unittest.main()
