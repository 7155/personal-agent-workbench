from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from rag_ime.agent_lab.scene_recipes import (
    AgentLabSceneRecipeConflict,
    AgentLabSceneRecipeStore,
    AgentLabSceneRecipeUnavailable,
    ENTERPRISE_RAG_VALIDATION_SCENE_ID,
    validate_scene_recipe_binding,
)
from rag_ime.db import apply_database_migrations, latest_migration_version, sqlite_connection
from rag_ime.agent_lab import scene_recipes as recipes


SCENE = ENTERPRISE_RAG_VALIDATION_SCENE_ID
EXPERIMENT = "enterprise-rag.luna-prompt-v4-standard-r6.v1"
CANDIDATE_RUN = "enterprise-rag-luna-max-coverage-balanced-v4-20260904-r4"


def candidate_experiment() -> dict[str, object]:
    return {
        "experimentId": EXPERIMENT,
        "projectionState": "current",
        "status": "kept",
        "comparison": {"decision": "keep"},
        "candidate": {"runId": CANDIDATE_RUN},
        "dataset": {"split": "validation"},
    }


class AgentLabSceneRecipeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-lab-scene-recipe-")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.experiments = [candidate_experiment()]
        self.store = AgentLabSceneRecipeStore(
            self.db_path, experiment_provider=lambda: copy.deepcopy(self.experiments),
        )
        self.store.initialize()

    def apply(self, *, revision: int = 0, request: str = "apply-1") -> dict[str, object]:
        return self.store.apply_candidate(
            SCENE, experiment_id=EXPERIMENT,
            expected_revision=revision, client_request_id=request,
        )

    def event_count(self) -> int:
        with sqlite_connection(self.db_path) as conn:
            return conn.execute("SELECT count(*) FROM agent_lab_scene_recipe_events").fetchone()[0]

    def test_upgrade_preserves_immutable_v1_binding_receipt_and_revision_zero_rollback(self) -> None:
        old_path = Path(self.tmp.name) / "old.sqlite"
        legacy = copy.deepcopy(recipes._BUILTIN_VERSION)
        legacy["recipe"]["model"] = "gpt-5.6-sol"
        payloads = [legacy, copy.deepcopy(recipes._CANDIDATE_VERSION)]
        def encode(value):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with sqlite_connection(old_path) as conn:
            migration = apply_database_migrations(conn).current_version
            for payload in payloads:
                conn.execute("INSERT INTO agent_lab_scene_recipe_versions (scene_id, version_id, payload_json) VALUES (?, ?, ?)",
                             (SCENE, payload["versionId"], encode(payload)))
        old_store = AgentLabSceneRecipeStore(old_path, experiment_provider=lambda: [candidate_experiment()])
        # Reconstruct the old owner against its already-initialized immutable
        # rows, without asking today's initializer to seed a future default.
        with patch.object(old_store, "initialize", return_value=migration), patch.object(
            old_store, "_active_version", side_effect=lambda conn, scene, row: old_store._version(
                conn, scene, str(row["version_id"] if row else legacy["versionId"])),
        ):
            binding = old_store.resolve_for_run(SCENE)
            first = old_store.apply_candidate(SCENE, experiment_id=EXPERIMENT,
                                             expected_revision=0, client_request_id="old-apply")
        upgraded = AgentLabSceneRecipeStore(old_path, experiment_provider=lambda: [])
        upgraded.initialize()
        self.assertEqual(validate_scene_recipe_binding(binding), binding)
        self.assertEqual(binding["recipe"]["model"], "gpt-5.6-sol")
        replay = upgraded.apply_candidate(SCENE, experiment_id=EXPERIMENT,
                                          expected_revision=0, client_request_id="old-apply")
        self.assertEqual(replay, {**first, "replayed": True})
        rollback = upgraded.rollback(SCENE, expected_revision=1, client_request_id="old-rollback")
        self.assertEqual(rollback["activeVersion"], legacy)
        self.assertEqual(rollback["event"]["versionId"], first["event"]["fromVersionId"])
        with sqlite_connection(old_path) as conn:
            for payload in payloads:
                actual = conn.execute("SELECT payload_json FROM agent_lab_scene_recipe_versions WHERE scene_id=? AND version_id=?",
                                      (SCENE, payload["versionId"])).fetchone()[0]
                self.assertEqual(actual, encode(payload))
        reopened = AgentLabSceneRecipeStore(old_path, experiment_provider=lambda: [])
        self.assertEqual(reopened.resolve_for_run(SCENE)["recipe"]["model"], "gpt-5.6-sol")

    def test_initial_state_is_scene_incumbent_without_an_application_claim(self) -> None:
        state = self.store.get_state(SCENE)
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["effectScope"], "future_validation_runs")
        self.assertIsNone(state["lastEvent"])
        self.assertIsNone(state["previousVersion"])
        self.assertFalse(state["rollbackAvailable"])
        version = state["activeVersion"]
        self.assertEqual(version["origin"], "runner_builtin")
        self.assertEqual(version["sourceExperimentId"], "")
        self.assertEqual(version["sourceCandidateRunId"], "")
        self.assertEqual(version["recipe"], {
            "provider": "openai-codex", "model": "gpt-6.1-sol", "thinkingLevel": "max",
            "promptProfile": "incumbent",
            "promptContractVersion": "rag-agent-evidence-state-budget-routing-v19",
            "agenticSupplementalLimit": 6, "answerOnly": True, "developmentOnly": True,
            "split": "validation", "candidateAware": True,
            "unbiasedPromotionClaimAllowed": False,
        })
        self.assertTrue(state["candidate"]["available"])
        self.assertEqual(self.event_count(), 0)

    def test_apply_pins_registered_recipe_and_preserves_previous_run_snapshot(self) -> None:
        before = self.store.resolve_for_run(SCENE)
        result = self.apply()
        self.assertEqual(result["revision"], 1)
        self.assertFalse(result["replayed"])
        self.assertEqual(result["event"]["operation"], "apply")
        self.assertEqual(result["previousVersion"]["versionId"], before["versionId"])
        self.assertTrue(result["rollbackAvailable"])
        self.assertEqual(result["activeVersion"]["sourceCandidateRunId"], CANDIDATE_RUN)
        recipe = result["activeVersion"]["recipe"]
        self.assertEqual(recipe["model"], "gpt-5.6-luna")
        self.assertEqual(recipe["promptProfile"], "coverage-balanced-evidence-gate-v4")
        self.assertEqual(recipe["split"], "validation")
        self.assertTrue(recipe["candidateAware"])
        self.assertFalse(recipe["unbiasedPromotionClaimAllowed"])
        self.assertEqual(before["recipe"]["model"], "gpt-6.1-sol")
        self.assertEqual(self.store.resolve_for_run(SCENE)["recipe"], recipe)
        recipe["model"] = "caller-mutation"
        self.assertEqual(self.store.resolve_for_run(SCENE)["recipe"]["model"], "gpt-5.6-luna")

    def test_same_request_returns_original_receipt_even_after_rollback_or_catalog_change(self) -> None:
        first = self.apply()
        self.store.rollback(SCENE, expected_revision=1, client_request_id="rollback-1")
        self.experiments.clear()
        replay = self.apply()
        self.assertEqual(replay, {**first, "replayed": True})
        self.assertEqual(self.event_count(), 2)
        self.assertEqual(self.store.get_state(SCENE)["revision"], 2)

    def assert_service_unavailable(self, exception: Exception, reason: str) -> None:
        self.assertEqual(type(exception).__name__, "AgentLabSceneRecipeServiceUnavailable")
        self.assertEqual(exception.http_status, 503)
        self.assertEqual(exception.response_payload(), {
            "ok": False, "code": "AGENT_LAB_SCENE_RECIPE_SERVICE_UNAVAILABLE",
            "reasonCode": reason, "error": "场景配置服务暂不可用，请稍后重试。",
        })

    def test_public_projection_failure_is_a_safe_service_error_not_candidate_rejection(self) -> None:
        with patch.object(self.store, "_experiment_provider", side_effect=sqlite3.OperationalError(
            "database is locked /private/fixture.sqlite secret-token",
        )):
            for operation in (lambda: self.store.get_state(SCENE), self.apply):
                with self.subTest(operation=operation):
                    with self.assertRaises(Exception) as caught:
                        operation()
                    self.assert_service_unavailable(caught.exception, "experiment_source_unavailable")
        self.assertEqual(self.event_count(), 0)

    def test_persisted_request_replays_before_reading_an_unavailable_projection(self) -> None:
        first = self.apply()
        broken_provider = Mock(side_effect=sqlite3.OperationalError("database is locked"))
        reopened = AgentLabSceneRecipeStore(self.db_path, experiment_provider=broken_provider)
        replay = reopened.apply_candidate(
            SCENE, experiment_id=EXPERIMENT, expected_revision=0, client_request_id="apply-1",
        )
        self.assertEqual(replay, {**first, "replayed": True})
        broken_provider.assert_not_called()
        self.assertEqual(self.event_count(), 1)

    def test_rollback_uses_persisted_history_without_reading_the_public_projection(self) -> None:
        first = self.apply()
        broken_provider = Mock(side_effect=sqlite3.OperationalError("database is locked"))
        reopened = AgentLabSceneRecipeStore(self.db_path, experiment_provider=broken_provider)
        rolled_back = reopened.rollback(SCENE, expected_revision=1, client_request_id="rollback-outage")
        self.assertEqual(rolled_back["activeVersion"], first["previousVersion"])
        self.assertEqual(rolled_back["revision"], 2)
        self.assertFalse(rolled_back["candidate"]["available"])
        self.assertEqual(rolled_back["candidate"]["reasonCode"], "not_checked")
        broken_provider.assert_not_called()
        self.assertEqual(reopened.rollback(SCENE, expected_revision=1, client_request_id="rollback-outage"), {
            **rolled_back, "replayed": True,
        })
        self.assertEqual(self.event_count(), 2)

    def test_sqlite_initialization_and_later_access_fail_as_safe_service_errors(self) -> None:
        reopened = AgentLabSceneRecipeStore(self.db_path, experiment_provider=lambda: [])
        operations = (
            reopened.initialize,
            lambda: self.store.get_state(SCENE),
            lambda: self.store.resolve_for_run(SCENE),
            self.apply,
            lambda: self.store.rollback(SCENE, expected_revision=0, client_request_id="rollback-fault"),
        )
        with patch("rag_ime.agent_lab.scene_recipes.sqlite_connection", side_effect=sqlite3.OperationalError(
            "unable to open /private/fixture.sqlite secret-token",
        )):
            for operation in operations:
                with self.subTest(operation=operation):
                    with self.assertRaises(Exception) as caught:
                        operation()
                    self.assert_service_unavailable(caught.exception, "storage_unavailable")
        self.assertEqual(reopened.initialize(), latest_migration_version())
        self.assertEqual(self.event_count(), 0)

    def test_uncertain_post_commit_failure_recovers_the_existing_receipt(self) -> None:
        @contextmanager
        def fail_after_commit(path: Path, **options: object):
            with sqlite_connection(path, **options) as conn:
                yield conn
            if options.get("foreign_keys"):
                raise sqlite3.OperationalError("connection failed after commit /private/fixture.sqlite")

        with patch("rag_ime.agent_lab.scene_recipes.sqlite_connection", fail_after_commit):
            with self.assertRaises(Exception) as caught:
                self.apply()
            self.assert_service_unavailable(caught.exception, "storage_unavailable")
        self.assertEqual(self.event_count(), 1)
        with patch.object(self.store, "_experiment_provider", side_effect=AssertionError("must not replay apply")) as provider:
            recovered = self.apply()
        provider.assert_not_called()
        self.assertTrue(recovered["replayed"])
        self.assertEqual(recovered["revision"], 1)
        self.assertEqual(recovered["event"]["clientRequestId"], "apply-1")
        self.assertEqual(self.event_count(), 1)

    def test_reusing_request_id_for_another_payload_is_a_distinct_conflict(self) -> None:
        self.apply()
        with self.assertRaises(AgentLabSceneRecipeConflict) as caught:
            self.store.rollback(SCENE, expected_revision=1, client_request_id="apply-1")
        self.assertEqual(caught.exception.http_status, 409)
        self.assertEqual(caught.exception.response_payload()["reasonCode"], "request_id_reused")
        self.assertEqual(self.event_count(), 1)

    def test_stale_revision_conflicts_without_appending_a_second_event(self) -> None:
        self.apply()
        with self.assertRaises(AgentLabSceneRecipeConflict) as caught:
            self.apply(request="stale-apply")
        payload = caught.exception.response_payload()
        self.assertEqual(payload["reasonCode"], "stale_revision")
        self.assertEqual(payload["currentRevision"], 1)
        self.assertEqual(caught.exception.http_status, 409)
        self.assertEqual(self.event_count(), 1)

    def test_concurrent_compare_and_swap_has_one_winner(self) -> None:
        barrier = threading.Barrier(2)

        def apply_concurrently(index: int) -> object:
            barrier.wait()
            try:
                return self.apply(request=f"concurrent-{index}")
            except AgentLabSceneRecipeConflict as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply_concurrently, range(2)))
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertEqual(sum(isinstance(value, AgentLabSceneRecipeConflict) for value in results), 1)
        self.assertEqual(self.event_count(), 1)

    def test_concurrent_duplicate_requests_append_only_once(self) -> None:
        barrier = threading.Barrier(2)

        def apply_concurrently(_: int) -> dict[str, object]:
            barrier.wait()
            return self.apply()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply_concurrently, range(2)))
        self.assertEqual(sorted(value["replayed"] for value in results), [False, True])
        self.assertEqual(self.event_count(), 1)

    def test_rollback_restores_existing_default_and_survives_restart(self) -> None:
        first = self.apply()
        reopened = AgentLabSceneRecipeStore(self.db_path, experiment_provider=lambda: [])
        self.assertEqual(reopened.resolve_for_run(SCENE)["versionId"], first["activeVersion"]["versionId"])
        result = reopened.rollback(SCENE, expected_revision=1, client_request_id="rollback-1")
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["event"]["operation"], "rollback")
        self.assertEqual(result["activeVersion"], first["previousVersion"])
        self.assertIsNone(result["previousVersion"])
        self.assertFalse(result["rollbackAvailable"])
        self.assertEqual(reopened.rollback(SCENE, expected_revision=1, client_request_id="rollback-1"), {
            **result, "replayed": True,
        })
        with self.assertRaises(AgentLabSceneRecipeUnavailable) as caught:
            reopened.rollback(SCENE, expected_revision=2, client_request_id="rollback-again")
        self.assertEqual(caught.exception.response_payload()["reasonCode"], "no_previous_version")
        self.assertEqual(self.event_count(), 2)

    def test_only_current_exact_kept_candidate_is_available(self) -> None:
        cases = (
            ({"projectionState": "history"}, "experiment_not_current"),
            ({"status": "rejected"}, "experiment_not_kept"),
            ({"comparison": {"decision": "reject"}}, "experiment_not_kept"),
            ({"candidate": {"runId": "another-run"}}, "candidate_identity_mismatch"),
            ({"dataset": {"split": "held_out"}}, "validation_required"),
        )
        for change, reason in cases:
            with self.subTest(reason=reason):
                self.experiments[:] = [{**candidate_experiment(), **change}]
                candidate = self.store.get_state(SCENE)["candidate"]
                self.assertFalse(candidate["available"])
                self.assertEqual(candidate["reasonCode"], reason)
                with self.assertRaises(AgentLabSceneRecipeUnavailable) as caught:
                    self.apply()
                self.assertEqual(caught.exception.http_status, 422)
                self.assertEqual(caught.exception.response_payload()["reasonCode"], reason)
        self.assertEqual(self.event_count(), 0)

    def test_missing_optional_hash_trace_and_patch_do_not_block_registered_recipe(self) -> None:
        self.experiments[0]["optimizationEvidence"] = {
            "status": "partial", "patch": {"unifiedDiff": "untrusted arbitrary code"},
        }
        result = self.apply()
        self.assertEqual(result["event"]["sourceExperimentRevision"], "")
        self.assertEqual(result["activeVersion"]["recipe"]["model"], "gpt-5.6-luna")
        self.assertNotIn("untrusted", str(result))

    def test_missing_or_unsupported_experiments_are_readably_unavailable(self) -> None:
        self.experiments.clear()
        self.assertEqual(self.store.get_state(SCENE)["candidate"]["reasonCode"], "experiment_unavailable")
        candidate = self.store.get_state(SCENE, experiment_id="different-experiment")["candidate"]
        self.assertEqual(candidate["reasonCode"], "unsupported_experiment")
        self.assertTrue(candidate["reason"])
        self.assertEqual(self.store.get_state(SCENE)["activeVersion"]["origin"], "runner_builtin")

    def test_unsupported_scene_cannot_mutate_the_validation_scene(self) -> None:
        before = self.store.get_state(SCENE)
        with self.assertRaises(AgentLabSceneRecipeUnavailable) as caught:
            self.store.apply_candidate(
                "agent-lab.enterpriseops", experiment_id=EXPERIMENT,
                expected_revision=0, client_request_id="other-scene",
            )
        self.assertEqual(caught.exception.response_payload()["reasonCode"], "unsupported_scene")
        self.assertEqual(self.store.get_state(SCENE), before)
        self.assertEqual(self.event_count(), 0)

    def test_recipe_versions_and_events_are_immutable_at_storage_boundary(self) -> None:
        self.apply()
        for statement in (
            "UPDATE agent_lab_scene_recipe_versions SET payload_json = '{}'",
            "DELETE FROM agent_lab_scene_recipe_versions",
            "UPDATE agent_lab_scene_recipe_events SET operation = 'rollback'",
            "DELETE FROM agent_lab_scene_recipe_events",
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                with sqlite_connection(self.db_path) as conn:
                    conn.execute(statement)
        self.assertEqual(self.store.get_state(SCENE)["revision"], 1)

    def test_other_scene_history_does_not_affect_validation_revision_or_rollback(self) -> None:
        with sqlite_connection(self.db_path, foreign_keys=True) as conn:
            conn.execute(
                "INSERT INTO agent_lab_scene_recipe_versions VALUES (?, ?, ?)",
                ("another-scene", "other-version", '{"unrelated":true}'),
            )
            conn.execute(
                "INSERT INTO agent_lab_scene_recipe_events "
                "(scene_id, revision, event_id, client_request_id, operation, request_json, "
                "from_version_id, version_id, rollback_revision, event_json, response_json, created_at_ms) "
                "VALUES ('another-scene', 99, 'other-event', 'other-request', 'apply', '{}', "
                "'other-version', 'other-version', 0, '{}', '{}', 1)"
            )
            before = conn.execute(
                "SELECT * FROM agent_lab_scene_recipe_events WHERE scene_id = 'another-scene'"
            ).fetchall()
        self.assertEqual(self.store.get_state(SCENE)["revision"], 0)
        self.apply()
        self.store.rollback(SCENE, expected_revision=1, client_request_id="rollback-1")
        with sqlite_connection(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT * FROM agent_lab_scene_recipe_events WHERE scene_id = 'another-scene'"
            ).fetchall(), before)

    def test_reapplying_after_rollback_preserves_the_existing_version_and_undo_chain(self) -> None:
        first = self.apply()
        self.store.rollback(SCENE, expected_revision=1, client_request_id="rollback-1")
        second = self.apply(revision=2, request="apply-2")
        self.assertEqual(second["activeVersion"], first["activeVersion"])
        result = self.store.rollback(SCENE, expected_revision=3, client_request_id="rollback-2")
        self.assertEqual(result["activeVersion"]["origin"], "runner_builtin")
        self.assertFalse(result["rollbackAvailable"])
        self.assertEqual(self.event_count(), 4)
        with sqlite_connection(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM agent_lab_scene_recipe_versions").fetchone()[0], 3)

    def test_default_provider_uses_current_public_ledger_without_an_experiment_import(self) -> None:
        store = AgentLabSceneRecipeStore(self.db_path)
        candidate = store.get_state(SCENE)["candidate"]
        self.assertTrue(candidate["available"], candidate)
        self.assertEqual(candidate["experimentId"], EXPERIMENT)


if __name__ == "__main__":
    unittest.main()
