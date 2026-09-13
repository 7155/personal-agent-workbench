from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from rag_ime.agent_service import AgentService
from rag_ime.contracts.json_schema import validate_contract
from rag_ime.control_api import (
    ControlAccessContext,
    ControlApiError,
    ControlErrorCode,
    ControlPathId,
    ControlRequest,
    default_route_policy,
)
from rag_ime.debug_server import DebugRequestHandler
from rag_ime.eval_run_store import EvalRunStore
from rag_ime.eval_schedule_store import EvalScheduleStore
from rag_ime.trace_runtime import build_eval_run


def _definition(schedule_id: str, *, due_at_ms: int) -> dict[str, object]:
    return {
        "scheduleId": schedule_id,
        "suiteId": "sgg",
        "suiteRevision": "fixture-v2",
        "recurrenceKind": "daily",
        "recurrenceInterval": 1,
        "maxRuns": 2,
        "nextDueAtMs": due_at_ms,
    }


def _invoke_get(service: object, path: str) -> tuple[int, dict[str, object]]:
    handler = DebugRequestHandler.__new__(DebugRequestHandler)
    handler.service = service  # type: ignore[assignment]
    handler.path = path
    handler._authorize_gateway_request = lambda method, parsed: True  # type: ignore[method-assign]
    handler._serve_gateway_static = lambda request_path: False  # type: ignore[method-assign]
    responses: list[tuple[int, dict[str, object]]] = []
    handler._write_json = lambda status, payload, **kwargs: responses.append(  # type: ignore[method-assign]
        (int(status), payload)
    )
    handler.do_GET()
    if len(responses) != 1:
        raise AssertionError(f"expected one response, received {len(responses)}")
    return responses[0]


def _invoke_post(
    service: object,
    path: str,
    body: dict[str, object],
) -> tuple[int, dict[str, object]]:
    handler = DebugRequestHandler.__new__(DebugRequestHandler)
    handler.service = service  # type: ignore[assignment]
    handler.path = path
    handler._authorize_gateway_request = lambda method, parsed: True  # type: ignore[method-assign]
    handler._management_post_security_error = lambda request_path, **kwargs: None  # type: ignore[method-assign]
    handler._read_json = lambda: dict(body)  # type: ignore[method-assign]
    responses: list[tuple[int, dict[str, object]]] = []
    handler._write_json = lambda status, payload, **kwargs: responses.append(  # type: ignore[method-assign]
        (int(status), payload)
    )
    handler.do_POST()
    if len(responses) != 1:
        raise AssertionError(f"expected one response, received {len(responses)}")
    return responses[0]


class EvalScheduleStoreListTests(unittest.TestCase):
    def test_list_is_bounded_deterministic_and_never_returns_lease_tokens(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eval-schedule-api-") as tmp:
            store = EvalScheduleStore(Path(tmp) / "eval.sqlite")
            store.initialize()
            now_ms = int(time.time() * 1000)
            store.create(_definition("eval-schedule:z", due_at_ms=now_ms + 10_000), now_ms=now_ms)
            store.create(_definition("eval-schedule:a", due_at_ms=now_ms + 10_000), now_ms=now_ms)

            items = store.list(limit=1)
            self.assertEqual([item["id"] for item in items], ["eval-schedule:a"])
            self.assertNotIn("leaseToken", items[0])
            self.assertEqual(items[0]["latestRun"], {})

            with self.assertRaisesRegex(ValueError, "between 1 and 500"):
                store.list(limit=0)
            with self.assertRaisesRegex(ValueError, "between 1 and 500"):
                store.list(limit=501)


class EvalScheduleServiceFacadeTests(unittest.TestCase):
    def test_http_action_controls_the_existing_store_and_returns_real_errors(self) -> None:
        from urllib.parse import quote
        with tempfile.TemporaryDirectory(prefix="eval-schedule-controls-") as tmp:
            store = EvalScheduleStore(Path(tmp) / "eval.sqlite")
            store.initialize()
            agent = AgentService.__new__(AgentService)
            agent.eval_schedules = store
            service = SimpleNamespace(agent=agent)
            identifier = "eval-schedule:managed"
            store.create(_definition(identifier, due_at_ms=int(time.time() * 1000) + 60_000))
            path = f"/api/observability/eval-schedules/{quote(identifier, safe='')}/action"
            for action, expected in (("pause", "paused"), ("resume", "scheduled"), ("cancel", "cancelled")):
                code, result = _invoke_post(service, path, {"action": action})
                self.assertEqual(code, 200)
                self.assertEqual(result["schedule"]["status"], expected)
                validate_contract(result["schedule"], "eval-schedule.v1.json")
            self.assertEqual(_invoke_post(service, path, {"action": "resume"})[0], 400)
            self.assertEqual(_invoke_post(service, path.replace("managed", "missing"), {"action": "pause"})[0], 404)

    def test_list_create_and_runs_facades_have_checked_json_contracts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eval-schedule-api-") as tmp:
            store = EvalScheduleStore(Path(tmp) / "eval.sqlite")
            store.initialize()
            service = AgentService.__new__(AgentService)
            service.eval_schedules = store
            eval_store = EvalRunStore(Path(tmp) / "eval-runs.sqlite")
            eval_store.persist(
                build_eval_run(
                    eval_run_id="eval:sgg:facade",
                    trace_ids=["trace:sgg:facade"],
                    mode="ground_truth",
                    truth_kind="frozen",
                    dataset_id="dataset:sgg",
                    label_revision="fixture-v2",
                    metrics={"f1": 1.0},
                    now_ms=1,
                )
            )
            service.eval_runs = eval_store
            now_ms = int(time.time() * 1000)
            created = service.create_eval_schedule(
                _definition("eval-schedule:facade", due_at_ms=now_ms + 1_000)
            )
            self.assertEqual(created["ok"], True)
            self.assertEqual(created["schedule"]["suiteRevision"], "fixture-v2")
            self.assertNotIn("leaseToken", str(created))
            validate_contract(created, "eval-schedule-create.v1.json")

            listed = service.list_eval_schedules({"limit": 10})
            self.assertEqual(listed["items"][0]["id"], "eval-schedule:facade")
            self.assertNotIn("leaseToken", str(listed))
            validate_contract(listed, "eval-schedule-list.v1.json")

            claim = store.claim_due(now_ms=now_ms + 1_000)[0]
            store.succeed(
                str(claim["runId"]),
                lease_token=str(claim["leaseToken"]),
                eval_run_id="eval:sgg:facade",
                now_ms=now_ms + 2_000,
            )
            runs = service.eval_schedule_runs(
                "eval-schedule:facade",
                {"limit": 10},
            )
            self.assertEqual(runs["items"][0]["state"], "succeeded")
            self.assertEqual(runs["items"][0]["evalRunId"], "eval:sgg:facade")
            self.assertEqual(runs["items"][0]["traceIds"], ["trace:sgg:facade"])
            self.assertFalse(runs["items"][0]["traceIdsTruncated"])
            self.assertNotIn("leaseToken", str(runs))
            validate_contract(runs, "eval-schedule-run-list.v1.json")

    def test_run_projection_bounds_trace_ids_and_marks_truncation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eval-schedule-api-") as tmp:
            root = Path(tmp)
            schedule_store = EvalScheduleStore(root / "eval.sqlite")
            schedule_store.initialize()
            eval_store = EvalRunStore(root / "eval-runs.sqlite")
            trace_ids = [f"trace:sgg:bounded:{index}" for index in range(70)]
            eval_store.persist(
                build_eval_run(
                    eval_run_id="eval:sgg:bounded",
                    trace_ids=trace_ids,
                    mode="ground_truth",
                    truth_kind="frozen",
                    dataset_id="dataset:sgg",
                    label_revision="fixture-v1",
                    metrics={"f1": 1.0},
                    now_ms=1,
                )
            )
            service = AgentService.__new__(AgentService)
            service.eval_schedules = schedule_store
            service.eval_runs = eval_store
            now_ms = int(time.time() * 1000)
            service.create_eval_schedule(
                _definition("eval-schedule:bounded", due_at_ms=now_ms + 1_000)
            )
            claim = schedule_store.claim_due(now_ms=now_ms + 1_000)[0]
            schedule_store.succeed(
                str(claim["runId"]),
                lease_token=str(claim["leaseToken"]),
                eval_run_id="eval:sgg:bounded",
                now_ms=now_ms + 2_000,
            )

            result = service.eval_schedule_runs(
                "eval-schedule:bounded",
                {"limit": 10},
            )

            item = result["items"][0]
            self.assertEqual(item["traceIds"], trace_ids[:64])
            self.assertTrue(item["traceIdsTruncated"])
            validate_contract(result, "eval-schedule-run-list.v1.json")

    def test_create_rejects_unknown_suite_and_revision_without_persisting(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eval-schedule-api-") as tmp:
            store = EvalScheduleStore(Path(tmp) / "eval.sqlite")
            store.initialize()
            service = AgentService.__new__(AgentService)
            service.eval_schedules = store
            now_ms = int(time.time() * 1000)

            for suite_id, suite_revision in (
                ("not-allowlisted", "fixture-v2"),
                ("sgg", "fixture-v1"),
            ):
                with self.subTest(suite_id=suite_id, suite_revision=suite_revision):
                    with self.assertRaises(ValueError):
                        service.create_eval_schedule(
                            {
                                **_definition(
                                    f"eval-schedule:invalid-{suite_id}",
                                    due_at_ms=now_ms + 1_000,
                                ),
                                "suiteId": suite_id,
                                "suiteRevision": suite_revision,
                            }
                        )
                    self.assertEqual(store.list(), [])


class EvalScheduleRoutePolicyTests(unittest.TestCase):
    def test_schedule_routes_are_local_only(self) -> None:
        policy = default_route_policy()
        routes = (
            policy.resolve(ControlPathId.OBSERVABILITY_EVAL_SCHEDULES_LIST),
            policy.resolve(ControlPathId.OBSERVABILITY_EVAL_SCHEDULES_CREATE),
            policy.resolve(ControlPathId.OBSERVABILITY_EVAL_SCHEDULE_RUNS),
        )
        self.assertEqual(routes[0].local_8766_path, "/api/observability/eval-schedules")
        self.assertEqual(routes[1].local_8766_path, "/api/observability/eval-schedules")
        self.assertEqual(
            routes[2].local_8766_path,
            "/api/observability/eval-schedules/{scheduleId}/runs",
        )
        for route in routes:
            with self.subTest(path_id=route.path_id):
                self.assertIsNone(route.gateway_8768_path)
                self.assertFalse(route.remote_safe)

        request = ControlRequest(
            request_id="eval-schedule-remote",
            path_id=ControlPathId.OBSERVABILITY_EVAL_SCHEDULES_LIST.value,
        )
        with self.assertRaises(ControlApiError) as raised:
            policy.authorize(
                request,
                ControlAccessContext.remote(device_id="phone-1", scopes={"*"}),
            )
        self.assertEqual(raised.exception.code, ControlErrorCode.ROUTE_NOT_ALLOWED)


class EvalScheduleHttpRouteTests(unittest.TestCase):
    def test_invalid_suite_uses_stable_public_error_and_does_not_persist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eval-schedule-api-") as tmp:
            store = EvalScheduleStore(Path(tmp) / "eval.sqlite")
            store.initialize()
            agent = AgentService.__new__(AgentService)
            agent.eval_schedules = store
            service = SimpleNamespace(
                agent=agent,
                config=SimpleNamespace(server_name="test"),
            )

            status, payload = _invoke_post(
                service,
                "/api/observability/eval-schedules",
                {
                    **_definition("eval-schedule:invalid-http", due_at_ms=1),
                    "suiteId": "not-allowlisted",
                },
            )

            self.assertEqual(status, 400)
            self.assertEqual(payload, {
                "schemaVersion": "rag-ime.eval-schedule-error.v1",
                "ok": False,
                "errorCode": "invalid_request",
                "error": "Invalid Eval schedule request",
            })
            self.assertEqual(store.list(), [])

    def test_local_http_routes_call_facades_without_exposing_lease_or_exception_text(self) -> None:
        schedule = {
            "id": "eval-schedule:http",
            "suiteId": "sgg",
            "suiteRevision": "fixture-v2",
            "recurrenceKind": "daily",
            "recurrenceInterval": 1,
            "maxRuns": 1,
            "runCount": 0,
            "status": "scheduled",
            "initialDueAtMs": 1,
            "nextDueAtMs": 1,
            "lastErrorCode": "",
            "createdAtMs": 1,
            "updatedAtMs": 1,
            "latestRun": {},
        }
        agent = SimpleNamespace(
            list_eval_schedules=lambda payload: {
                "schemaVersion": "rag-ime.eval-schedule-list.v1",
                "ok": True,
                "items": [schedule],
            },
            create_eval_schedule=lambda payload: {
                "schemaVersion": "rag-ime.eval-schedule-create.v1",
                "ok": True,
                "schedule": schedule,
            },
            eval_schedule_runs=lambda schedule_id, payload: {
                "schemaVersion": "rag-ime.eval-schedule-run-list.v1",
                "ok": True,
                "schedule": schedule,
                "items": [
                    {
                        "id": "eval-run:claim",
                        "scheduleId": schedule_id,
                        "attempt": 1,
                        "state": "succeeded",
                        "dueAtMs": 1,
                        "claimedAtMs": 1,
                        "finishedAtMs": 2,
                        "evalRunId": "eval:sgg:http",
                        "errorCode": "",
                        "traceIds": ["trace:sgg:http"],
                        "traceIdsTruncated": False,
                    }
                ],
            },
        )
        service = SimpleNamespace(agent=agent, config=SimpleNamespace(server_name="test"))

        get_status, listing = _invoke_get(
            service,
            "/api/observability/eval-schedules?limit=10",
        )
        post_status, created = _invoke_post(
            service,
            "/api/observability/eval-schedules",
            _definition("eval-schedule:http", due_at_ms=1),
        )
        runs_status, runs = _invoke_get(
            service,
            "/api/observability/eval-schedules/eval-schedule%3Ahttp/runs?limit=10",
        )

        self.assertEqual(get_status, 200)
        self.assertEqual(post_status, 201)
        self.assertEqual(runs_status, 200)
        self.assertEqual(created["schedule"]["suiteRevision"], "fixture-v2")
        self.assertEqual(runs["items"][0]["evalRunId"], "eval:sgg:http")
        self.assertNotIn("leaseToken", str((listing, created, runs)))

    def test_http_errors_use_stable_public_copies(self) -> None:
        sentinel = "PRIVATE_EVAL_SCHEDULE /Volumes/private/eval.sqlite"
        agent = SimpleNamespace(
            list_eval_schedules=lambda payload: (_ for _ in ()).throw(ValueError(sentinel)),
            create_eval_schedule=lambda payload: (_ for _ in ()).throw(ValueError(sentinel)),
            eval_schedule_runs=lambda schedule_id, payload: (_ for _ in ()).throw(KeyError(sentinel)),
        )
        service = SimpleNamespace(agent=agent, config=SimpleNamespace(server_name="test"))

        get_status, get_error = _invoke_get(
            service,
            "/api/observability/eval-schedules?limit=bad",
        )
        post_status, post_error = _invoke_post(
            service,
            "/api/observability/eval-schedules",
            {},
        )
        runs_status, runs_error = _invoke_get(
            service,
            "/api/observability/eval-schedules/eval-schedule%3Amissing/runs",
        )

        self.assertEqual(get_status, 400)
        self.assertEqual(post_status, 400)
        self.assertEqual(runs_status, 404)
        self.assertEqual(get_error, {
            "schemaVersion": "rag-ime.eval-schedule-error.v1",
            "ok": False,
            "errorCode": "invalid_request",
            "error": "Invalid Eval schedule request",
        })
        self.assertEqual(post_error, get_error)
        self.assertEqual(runs_error, {
            "schemaVersion": "rag-ime.eval-schedule-error.v1",
            "ok": False,
            "errorCode": "schedule_not_found",
            "error": "Eval schedule not found",
        })
        self.assertNotIn("PRIVATE_EVAL_SCHEDULE", str((get_error, post_error, runs_error)))


if __name__ == "__main__":
    unittest.main()
