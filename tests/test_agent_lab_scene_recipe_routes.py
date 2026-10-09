from __future__ import annotations

import tempfile
import unittest
import sqlite3
from copy import deepcopy
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rag_ime.agent_service import AgentService
from rag_ime.control_api import (
    ControlAccessContext,
    ControlApiError,
    ControlRequest,
    default_route_policy,
)
from rag_ime.control_api.route_table import find_route
from rag_ime.debug_server import DebugRequestHandler
from rag_ime.pi.config import PiRuntimeConfig


SCENE_ID = "agent-lab.enterprise-rag.validation"
EXPERIMENT_ID = "enterprise-rag.luna-prompt-v4-standard-r6.v1"
CANDIDATE_RUN_ID = "enterprise-rag-luna-max-coverage-balanced-v4-20260904-r4"
RECIPE_PATH = "/api/agent/eval-lab/scene-recipes"
RECIPE_PATH_ID = "agent.eval-lab.scene-recipes"


def _experiment() -> dict[str, object]:
    return {
        "experimentId": EXPERIMENT_ID,
        "projectionState": "current",
        "status": "kept",
        "comparison": {"decision": "keep"},
        "candidate": {"runId": CANDIDATE_RUN_ID},
        "dataset": {"split": "validation"},
    }


def _handler(agent: object) -> tuple[DebugRequestHandler, list[tuple[int, dict[str, object]]]]:
    handler = DebugRequestHandler.__new__(DebugRequestHandler)
    handler.service = SimpleNamespace(agent=agent)
    handler._authorize_gateway_request = lambda _method, _parsed: True
    handler._serve_isolated_html_preview = lambda _path: False
    handler._serve_gateway_static = lambda _path: False
    handler._management_post_security_error = lambda _path, require_json=True: None
    written: list[tuple[int, dict[str, object]]] = []
    handler._write_json = lambda status, body: written.append((int(status), body))
    return handler, written


class AgentLabSceneRecipeRoutePolicyTests(unittest.TestCase):
    def test_scene_recipe_routes_have_exact_local_only_contracts(self) -> None:
        policy = default_route_policy()
        bodies = {
            "apply": {"sceneId", "experimentId", "expectedRevision", "clientRequestId"},
            "rollback": {"sceneId", "expectedRevision", "clientRequestId"},
        }
        for action in ("get", "apply", "rollback"):
            with self.subTest(action=action):
                route = policy.resolve(f"{RECIPE_PATH_ID}.{action}")
                self.assertEqual(route.method.value, "GET" if action == "get" else "POST")
                self.assertEqual(route.local_8766_path, RECIPE_PATH if action == "get" else f"{RECIPE_PATH}/{action}")
                self.assertIsNone(route.gateway_8768_path)
                self.assertFalse(route.remote_safe)
                if action == "get":
                    self.assertEqual(route.query, frozenset({"sceneId", "experimentId"}))
                    self.assertEqual(route.required_query, frozenset({"sceneId"}))
                else:
                    self.assertEqual(route.body, frozenset(bodies[action]))
                    self.assertEqual(route.required_body, frozenset(bodies[action]))

    def test_mutations_need_identity_and_revision_but_no_second_confirmation(self) -> None:
        policy = default_route_policy()
        for action in ("apply", "rollback"):
            body: dict[str, object] = {
                "sceneId": SCENE_ID, "expectedRevision": 0, "clientRequestId": f"{action}-request",
            }
            if action == "apply":
                body["experimentId"] = EXPERIMENT_ID
            request = ControlRequest(request_id="control-request", path_id=f"{RECIPE_PATH_ID}.{action}", body=body)
            policy.authorize(request, ControlAccessContext.native())
            with self.assertRaises(ControlApiError):
                policy.authorize(request, ControlAccessContext.remote(device_id="phone", scopes={"*"}))
            for invalid in ({key: value for key, value in body.items() if key != "clientRequestId"}, {**body, "confirmText": "apply"}):
                with self.subTest(action=action, invalid=invalid), self.assertRaises(ControlApiError):
                    policy.authorize(
                        ControlRequest(request_id="invalid", path_id=f"{RECIPE_PATH_ID}.{action}", body=invalid),
                        ControlAccessContext.native(),
                    )

    def test_get_descriptor_forwards_only_scene_and_optional_experiment(self) -> None:
        read = Mock(return_value={"ok": True, "sceneId": SCENE_ID})
        handler, written = _handler(SimpleNamespace(eval_lab_scene_recipes=read))
        descriptor = find_route("GET", RECIPE_PATH)
        self.assertIsNotNone(descriptor)
        handler.path = f"{RECIPE_PATH}?sceneId={SCENE_ID}&experimentId={EXPERIMENT_ID}&ignored=value"
        handler.do_GET()
        read.assert_called_once_with({"sceneId": SCENE_ID, "experimentId": EXPERIMENT_ID})
        self.assertEqual(written, [(HTTPStatus.OK, {"ok": True, "sceneId": SCENE_ID})])

    def test_scene_failure_boundary_does_not_capture_other_read_owners(self) -> None:
        handler, written = _handler(SimpleNamespace(configuration=Mock(side_effect=RuntimeError("other owner"))))
        handler.path = "/api/agent/configuration"
        with self.assertRaisesRegex(RuntimeError, "other owner"):
            handler.do_GET()
        self.assertEqual(written, [])


class AgentLabSceneRecipeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-scene-recipe-api-")
        self.root = Path(self.tmp.name)
        self.service = AgentService(
            db_path=self.root / "paw.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config",
                session_dir=self.root / "sessions",
                logs_dir=self.root / "logs",
            ),
            background_job_execution_owner=False,
            startup_recovery_enabled=False,
        )
        projection_patch = patch.object(self.service.eval_lab, "list_experiments", return_value=[_experiment()])
        self.public_projection = projection_patch.start()
        self.addCleanup(projection_patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)

    def _post(self, action: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        handler, written = _handler(self.service)
        handler.path = f"{RECIPE_PATH}/{action}"
        handler._read_json = lambda: payload
        handler.do_POST()
        self.assertEqual(len(written), 1)
        return written[0]

    def _apply_body(self, **changes: object) -> dict[str, object]:
        return {
            "sceneId": SCENE_ID,
            "experimentId": EXPERIMENT_ID,
            "expectedRevision": 0,
            "clientRequestId": "apply-scene-request",
            **changes,
        }

    def _get(self) -> tuple[int, dict[str, object]]:
        handler, written = _handler(self.service)
        handler.path = f"{RECIPE_PATH}?sceneId={SCENE_ID}&experimentId={EXPERIMENT_ID}"
        handler.do_GET()
        self.assertEqual(len(written), 1)
        return written[0]

    def test_source_failure_is_a_safe_service_error_instead_of_candidate_rejection(self) -> None:
        self.public_projection.side_effect = sqlite3.OperationalError("database is locked /private/source.sqlite")
        for operation in (lambda: self._get(), lambda: self._post("apply", self._apply_body())):
            with self.subTest(operation=operation):
                status, payload = operation()
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(payload["code"], "AGENT_LAB_SCENE_RECIPE_SERVICE_UNAVAILABLE")
                self.assertEqual(payload["reasonCode"], "experiment_source_unavailable")
                self.assertNotIn("/private", str(payload))
                self.assertNotIn("database is locked", str(payload))

    def test_untyped_storage_failure_is_safe_for_scene_get_apply_and_rollback(self) -> None:
        for failure in (sqlite3.OperationalError("database is locked /private/paw.sqlite"), OSError("disk failure /private/paw.sqlite")):
            with patch.object(self.service.eval_lab_scene_recipe_store, "initialize", side_effect=failure):
                for operation in (
                    lambda: self._get(),
                    lambda: self._post("apply", self._apply_body()),
                    lambda: self._post("rollback", {"sceneId": SCENE_ID, "expectedRevision": 0, "clientRequestId": "rollback-unavailable"}),
                ):
                    with self.subTest(failure=type(failure).__name__, operation=operation):
                        status, payload = operation()
                        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                        self.assertEqual(payload["code"], "AGENT_LAB_SCENE_RECIPE_SERVICE_UNAVAILABLE")
                        self.assertEqual(payload["reasonCode"], "storage_unavailable")
                        self.assertNotIn("/private", str(payload))
                        self.assertNotIn("database is locked", str(payload))

    def test_unknown_scene_internal_errors_return_safe_500_without_raw_exception(self) -> None:
        for method, operation in (
            ("eval_lab_scene_recipes", lambda: self._get()),
            ("eval_lab_scene_recipe_apply", lambda: self._post("apply", self._apply_body())),
            ("eval_lab_scene_recipe_rollback", lambda: self._post("rollback", {"sceneId": SCENE_ID, "expectedRevision": 0, "clientRequestId": "rollback-error"})),
        ):
            with self.subTest(method=method), patch.object(self.service, method, side_effect=RuntimeError("secret internal failure /private/source")):
                status, payload = operation()
                self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
                self.assertEqual(payload["code"], "AGENT_LAB_SCENE_RECIPE_INTERNAL_ERROR")
                self.assertFalse(payload["ok"])
                self.assertNotIn("secret", str(payload))
                self.assertNotIn("/private", str(payload))

    def test_service_reads_same_public_projection_and_applies_only_future_scene_runs(self) -> None:
        session = self.service.sessions.create(title="Existing conversation")
        session_before = deepcopy(self.service.sessions.get(str(session["id"])))
        configuration_before = deepcopy(self.service.configuration())

        self.public_projection.return_value = []
        initial = self.service.eval_lab_scene_recipes({"sceneId": SCENE_ID, "experimentId": EXPERIMENT_ID})
        self.assertEqual(initial["revision"], 0)
        self.assertEqual(initial["activeVersion"]["origin"], "runner_builtin")
        self.assertFalse(initial["candidate"]["available"])

        self.public_projection.return_value = [_experiment()]
        available = self.service.eval_lab_scene_recipes({"sceneId": SCENE_ID, "experimentId": EXPERIMENT_ID})
        self.assertTrue(available["candidate"]["available"])
        applied = self.service.eval_lab_scene_recipe_apply(self._apply_body())
        self.assertEqual(applied["revision"], 1)
        self.assertEqual(applied["effectScope"], "future_validation_runs")
        self.assertEqual(applied["activeVersion"]["sourceExperimentId"], EXPERIMENT_ID)
        self.assertEqual(applied["activeVersion"]["sourceCandidateRunId"], CANDIDATE_RUN_ID)
        self.assertEqual(applied["previousVersion"], initial["activeVersion"])

        reread = self.service.eval_lab_scene_recipes({"sceneId": SCENE_ID})
        self.assertEqual(reread["activeVersion"], applied["activeVersion"])
        self.assertTrue(reread["rollbackAvailable"])
        rolled_back = self.service.eval_lab_scene_recipe_rollback({
            "sceneId": SCENE_ID, "expectedRevision": 1, "clientRequestId": "rollback-scene-request",
        })
        self.assertEqual(rolled_back["revision"], 2)
        self.assertEqual(rolled_back["activeVersion"], initial["activeVersion"])
        self.assertFalse(rolled_back["rollbackAvailable"])
        self.assertEqual(self.service.configuration(), configuration_before)
        self.assertEqual(self.service.sessions.get(str(session["id"])), session_before)
        self.assertGreaterEqual(self.public_projection.call_count, 2)

    def test_post_preserves_idempotency_conflict_revision_and_unavailable_reason(self) -> None:
        status, applied = self._post("apply", self._apply_body())
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(applied["revision"], 1)
        self.assertFalse(applied["replayed"])
        status, replayed = self._post("apply", self._apply_body())
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["revision"], 1)

        status, conflict = self._post("rollback", {
            "sceneId": SCENE_ID, "expectedRevision": 0, "clientRequestId": "stale-rollback",
        })
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(conflict["code"], "AGENT_LAB_SCENE_RECIPE_CONFLICT")
        self.assertEqual(conflict["reasonCode"], "stale_revision")
        self.assertEqual(conflict["currentRevision"], 1)

        status, unavailable = self._post("apply", self._apply_body(
            experimentId="unsupported-experiment", expectedRevision=1, clientRequestId="unsupported-apply",
        ))
        self.assertEqual(status, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertEqual(unavailable["code"], "AGENT_LAB_SCENE_RECIPE_UNAVAILABLE")
        self.assertEqual(unavailable["reasonCode"], "unsupported_experiment")
        status, rollback = self._post("rollback", {
            "sceneId": SCENE_ID, "expectedRevision": 1, "clientRequestId": "rollback-current",
        })
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(rollback["revision"], 2)
        self.assertEqual(rollback["activeVersion"]["origin"], "runner_builtin")

    def test_get_returns_structured_error_for_unknown_scene(self) -> None:
        handler, written = _handler(self.service)
        handler.path = f"{RECIPE_PATH}?sceneId=unknown-scene"
        handler.do_GET()
        self.assertEqual(len(written), 1)
        status, payload = written[0]
        self.assertEqual(status, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertEqual(payload["code"], "AGENT_LAB_SCENE_RECIPE_UNAVAILABLE")
        self.assertFalse(payload["ok"])

    def test_invalid_revision_never_becomes_a_successful_apply(self) -> None:
        for revision in (None, True, -1, 1.5, "0"):
            with self.subTest(revision=revision):
                status, payload = self._post("apply", self._apply_body(expectedRevision=revision))
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertFalse(payload["ok"])
        state = self.service.eval_lab_scene_recipes({"sceneId": SCENE_ID})
        self.assertEqual(state["revision"], 0)


if __name__ == "__main__":
    unittest.main()
