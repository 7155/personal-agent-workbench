from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from rag_ime.agent_routes import observability_trace_repair_route
from rag_ime.agent_service import AgentService
from rag_ime.control_api import ControlPathId, default_route_policy
from rag_ime.contracts.json_schema import validate_contract
from rag_ime.debug_server import DebugRequestHandler
from rag_ime.eval_run_store import EvalRunStore
from rag_ime.trace_repair import TraceRepairStore, TraceRepairValidationError
from rag_ime.trace_runtime import build_trace_envelope, make_span
from rag_ime.trace_store import TraceStore


SOURCE_TRACE_ID = "trace:repair-http:source"
REPAIR_TRACE_ID = "trace:repair-http:repair"
REPAIR_SESSION_ID = "agent:trace-repair-http"


def _repair_spans(*, include_change: bool = True) -> list[object]:
    spans: list[object] = []
    if include_change:
        spans.append(
            make_span(
                span_id="span:repair-change",
                name="tool.call",
                started_at_ms=21,
                ended_at_ms=22,
                attributes={"toolName": "workspace_patch"},
            )
        )
    spans.append(
        make_span(
            span_id="span:repair-test",
            name="tool.call",
            started_at_ms=23,
            ended_at_ms=24,
            attributes={"toolName": "workspace_shell"},
        )
    )
    return spans


def _persist_traces(
    db: Path,
    *,
    repair_status: str = "completed",
    repair_session_id: str = REPAIR_SESSION_ID,
    include_change: bool = True,
) -> TraceStore:
    store = TraceStore(db)
    store.persist(
        build_trace_envelope(
            trace_id=SOURCE_TRACE_ID,
            source_kind="agent",
            input_text="private input is represented by a fingerprint",
            status="completed",
            now_ms=20,
        ).to_dict()
    )
    store.persist(
        build_trace_envelope(
            trace_id=REPAIR_TRACE_ID,
            source_kind="agent",
            input_text="private repair input is represented by a fingerprint",
            binding={"sessionId": repair_session_id},
            spans=_repair_spans(include_change=include_change),
            status=repair_status,
            now_ms=20,
        ).to_dict()
    )
    return store


def _repair_snapshot(
    *,
    status: str = "idle",
    test_exit_code: int = 0,
    include_test: bool = True,
    malicious_nested_change: bool = False,
) -> dict[str, object]:
    events: list[dict[str, object]] = []
    if include_test:
        result: dict[str, object] = {
            "schemaVersion": "rag-ime.workspace-command-receipt.v1",
            "commandSha256": "b" * 64,
            "exitCode": test_exit_code,
            "networkAllowed": False,
            "timedOut": False,
            "outputLimited": False,
            "sourceReadOnly": False,
            "temporaryWritesDiscarded": False,
        }
        if malicious_nested_change:
            result["debug"] = {
                "eventType": "tool_finished",
                "toolName": "workspace_patch",
                "status": "completed",
            }
        event: dict[str, object] = {
            "eventId": "event:repair-test",
            "eventType": "tool_finished",
            "payload": {
                "toolCallId": "span:repair-test",
                "toolName": "workspace_shell",
                "args": {"command": "python3 -m unittest tests.test_trace_repair"},
                "result": result,
                "isError": test_exit_code != 0,
            },
        }
        events.append(event)
    return {
        "sessionId": REPAIR_SESSION_ID,
        "snapshotScope": "full",
        "status": status,
        "liveEvents": events,
        "items": [],
    }


class _JudgeRuntime:
    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.calls: list[dict[str, object]] = []
        self.delay_seconds = delay_seconds

    def complete_once(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return {
            "text": json.dumps(
                {
                    "relevance": 0.9,
                    "coverage": 0.8,
                    "groundedness": 0.7,
                    "contradiction": 0.1,
                    "confidence": 0.9,
                }
            )
        }


def _service(
    tmp: str,
    *,
    repair_status: str = "completed",
    bound_session_id: str = REPAIR_SESSION_ID,
    include_change: bool = True,
    session_status: str = "idle",
    test_exit_code: int = 0,
    include_test: bool = True,
    malicious_nested_change: bool = False,
    judge_delay_seconds: float = 0.0,
) -> tuple[AgentService, _JudgeRuntime]:
    db = Path(tmp) / "trace-repair.sqlite"
    trace_store = _persist_traces(
        db,
        repair_status=repair_status,
        repair_session_id=bound_session_id,
        include_change=include_change,
    )
    repair_store = TraceRepairStore(db)
    repair_store.initialize()
    eval_runs = EvalRunStore(db)
    eval_runs.initialize()
    runtime = _JudgeRuntime(delay_seconds=judge_delay_seconds)
    service = AgentService.__new__(AgentService)
    service.db_path = db
    service.trace_store = trace_store
    service.trace_repairs = repair_store
    service.eval_runs = eval_runs
    service.runtime = runtime
    service._trace_repair_recheck_lock = threading.RLock()
    snapshot = _repair_snapshot(
        status=session_status,
        test_exit_code=test_exit_code,
        include_test=include_test,
        malicious_nested_change=malicious_nested_change,
    )
    service.message_snapshot = SimpleNamespace(
        messages=lambda session_id: dict(snapshot)
        if session_id == REPAIR_SESSION_ID
        else None
    )
    return service, runtime


def _record_authoritative_evidence(
    service: AgentService,
) -> tuple[dict[str, object], dict[str, object]]:
    candidate = {
        "repairSessionId": REPAIR_SESSION_ID,
        "repairTraceId": REPAIR_TRACE_ID,
    }
    change = service.record_trace_repair_change_evidence(
        {
            "schemaVersion": "rag-ime.trace-repair-change-evidence.v1",
            **candidate,
        }
    )["evidence"]
    test = service.record_trace_repair_test_evidence(
        {
            "schemaVersion": "rag-ime.trace-repair-test-evidence.v1",
            **candidate,
        }
    )["evidence"]
    return change, test


def _create_authoritative_receipt(
    service: AgentService,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    change, test = _record_authoritative_evidence(service)
    receipt = service.create_trace_repair_receipt(
        {
            "schemaVersion": "rag-ime.trace-repair-receipt-create.v1",
            "sourceScope": "trace-runtime",
            "sourceTraceId": SOURCE_TRACE_ID,
            "failureRef": "failure:trace-repair-http",
            "changeReceiptId": change["evidenceId"],
            "testEvidenceId": test["evidenceId"],
            "repairTraceId": REPAIR_TRACE_ID,
            "repairSessionId": REPAIR_SESSION_ID,
        }
    )["receipt"]
    return change, test, receipt


def _invoke_get(service: object, path: str) -> tuple[HTTPStatus, dict[str, object]]:
    handler = DebugRequestHandler.__new__(DebugRequestHandler)
    handler.service = service  # type: ignore[assignment]
    handler.path = path
    handler._authorize_gateway_request = lambda method, parsed: True  # type: ignore[method-assign]
    handler._serve_gateway_static = lambda request_path: False  # type: ignore[method-assign]
    responses: list[tuple[HTTPStatus, dict[str, object]]] = []
    handler._write_json = lambda status, payload, **kwargs: responses.append((status, payload))  # type: ignore[method-assign]
    handler.do_GET()
    if len(responses) != 1:
        raise AssertionError(f"expected one response, received {len(responses)}")
    return responses[0]


def _invoke_post(
    service: object,
    path: str,
    body: dict[str, object],
) -> tuple[HTTPStatus, dict[str, object]]:
    handler = DebugRequestHandler.__new__(DebugRequestHandler)
    handler.service = service  # type: ignore[assignment]
    handler.path = path
    handler._authorize_gateway_request = lambda method, parsed: True  # type: ignore[method-assign]
    handler._management_post_security_error = lambda request_path, **kwargs: None  # type: ignore[method-assign]
    handler._read_json = lambda: dict(body)  # type: ignore[method-assign]
    responses: list[tuple[HTTPStatus, dict[str, object]]] = []
    handler._write_json = lambda status, payload, **kwargs: responses.append((status, payload))  # type: ignore[method-assign]
    handler.do_POST()
    if len(responses) != 1:
        raise AssertionError(f"expected one response, received {len(responses)}")
    return responses[0]


class TraceRepairRouteTests(unittest.TestCase):
    def test_routes_are_strict_and_decode_only_the_receipt_id(self) -> None:
        self.assertEqual(
            observability_trace_repair_route(
                "/api/observability/trace-repair/evidence/change"
            ),
            ("", "change"),
        )
        self.assertEqual(
            observability_trace_repair_route(
                "/api/observability/trace-repair/evidence/test"
            ),
            ("", "test"),
        )
        self.assertEqual(
            observability_trace_repair_route(
                "/api/observability/trace-repair/receipts"
            ),
            ("", "create"),
        )
        receipt_id = "repair-receipt:trace:repair-http:repair"
        self.assertEqual(
            observability_trace_repair_route(
                "/api/observability/trace-repair/receipts/" + quote(receipt_id, safe="")
            ),
            (receipt_id, "get"),
        )
        self.assertEqual(
            observability_trace_repair_route(
                "/api/observability/trace-repair/recheck"
            ),
            ("", "recheck"),
        )
        for path in (
            "/api/observability/trace-repair",
            "/api/observability/trace-repair/receipts/",
            "/api/observability/trace-repair/receipts/a/b",
            "/control/v1/observability/trace-repair/recheck",
        ):
            with self.subTest(path=path):
                self.assertEqual(observability_trace_repair_route(path), ("", ""))


class TraceRepairRoutePolicyTests(unittest.TestCase):
    def test_repair_mutations_are_local_only_and_body_allowlisted(self) -> None:
        policy = default_route_policy()
        expected = {
            ControlPathId.OBSERVABILITY_TRACE_REPAIR_CHANGE_EVIDENCE: (
                "/api/observability/trace-repair/evidence/change",
                {"schemaVersion", "repairSessionId", "repairTraceId"},
            ),
            ControlPathId.OBSERVABILITY_TRACE_REPAIR_TEST_EVIDENCE: (
                "/api/observability/trace-repair/evidence/test",
                {"schemaVersion", "repairSessionId", "repairTraceId"},
            ),
            ControlPathId.OBSERVABILITY_TRACE_REPAIR_RECEIPT_CREATE: (
                "/api/observability/trace-repair/receipts",
                {"schemaVersion", "sourceScope", "sourceTraceId", "failureRef", "changeReceiptId", "testEvidenceId", "repairTraceId", "repairSessionId"},
            ),
            ControlPathId.OBSERVABILITY_TRACE_REPAIR_RECEIPT_GET: (
                "/api/observability/trace-repair/receipts/{repairReceiptId}",
                set(),
            ),
            ControlPathId.OBSERVABILITY_TRACE_REPAIR_RECHECK: (
                "/api/observability/trace-repair/recheck",
                {"schemaVersion", "repairReceiptId"},
            ),
        }
        for path_id, (path, body) in expected.items():
            with self.subTest(path_id=path_id):
                route = policy.resolve(path_id)
                self.assertEqual(route.local_8766_path, path)
                self.assertIsNone(route.gateway_8768_path)
                self.assertFalse(route.remote_safe)
                self.assertEqual(route.body, frozenset(body))
                required = set(body)
                self.assertEqual(route.required_body, frozenset(required))
                if path.endswith("{repairReceiptId}"):
                    self.assertEqual(route.params, frozenset({"repairReceiptId"}))


class TraceRepairServiceTests(unittest.TestCase):
    def test_service_derives_ids_and_recheck_binding_from_persisted_receipts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trace-repair-http-") as tmp:
            service, runtime = _service(tmp)
            change, test, receipt = _create_authoritative_receipt(service)
            validate_contract(receipt, "trace-repair-receipt.v1.json")
            self.assertEqual(change["sourceScope"], "trace-repair")
            self.assertEqual(change["sourceTraceId"], REPAIR_TRACE_ID)
            self.assertEqual(test["testStatus"], "passed")
            self.assertEqual(change["evidence"]["repairSessionId"], REPAIR_SESSION_ID)
            self.assertEqual(change["evidence"]["repairTraceId"], REPAIR_TRACE_ID)
            self.assertEqual(receipt["repairSessionId"], REPAIR_SESSION_ID)
            self.assertEqual(receipt["repairTraceId"], REPAIR_TRACE_ID)
            self.assertEqual(change["evidence"]["changeCount"], 1)
            self.assertEqual(test["evidence"]["passedCount"], 1)
            # Canonical evidence stores bounded labels/IDs/counts, never the
            # client prose, raw command, file path, or Tool output.
            serialized_evidence = json.dumps(
                {"change": change["evidence"], "test": test["evidence"]},
                ensure_ascii=False,
            )
            self.assertNotIn("python3 -m unittest", serialized_evidence)
            self.assertNotIn("rag_ime/agent_service.py", serialized_evidence)

            result = service.recheck_trace_repair(
                {
                    "schemaVersion": "rag-ime.trace-repair-recheck-request.v1",
                    "repairReceiptId": receipt["repairReceiptId"],
                }
            )
            self.assertEqual(result["receipt"], receipt)
            run = result["evalRun"]
            validate_contract(run, "eval-run.v1.json")
            self.assertEqual(run["sourceTraceId"], SOURCE_TRACE_ID)
            self.assertEqual(run["repairTraceId"], REPAIR_TRACE_ID)
            self.assertEqual(run["sourceScope"], "trace-runtime")
            self.assertEqual(run["failureRef"], "failure:trace-repair-http")
            self.assertEqual(run["repairReceiptId"], receipt["repairReceiptId"])
            self.assertEqual(run["changeReceiptId"], change["evidenceId"])
            self.assertEqual(run["testEvidenceId"], test["evidenceId"])
            self.assertEqual(run["testStatus"], "passed")
            self.assertEqual(runtime.calls[0]["model_id"], "gpt-6.1-sol")
            self.assertEqual(runtime.calls[0]["thinking_level"], "max")

            repeated = service.recheck_trace_repair(
                {
                    "schemaVersion": "rag-ime.trace-repair-recheck-request.v1",
                    "repairReceiptId": receipt["repairReceiptId"],
                }
            )
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(len(runtime.calls), 1)

    def test_service_rejects_client_evidence_status_and_ids(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trace-repair-http-") as tmp:
            service, _runtime = _service(tmp)
            invalid = {
                "schemaVersion": "rag-ime.trace-repair-change-evidence.v1",
                "repairSessionId": REPAIR_SESSION_ID,
                "repairTraceId": REPAIR_TRACE_ID,
                "evidence": {"ok": True},
                "evidenceId": "client-chosen",
            }
            with self.assertRaises(TraceRepairValidationError):
                service.record_trace_repair_change_evidence(invalid)
            with self.assertRaises(TraceRepairValidationError):
                service.record_trace_repair_test_evidence(
                    {
                        "schemaVersion": "rag-ime.trace-repair-test-evidence.v1",
                        "repairSessionId": REPAIR_SESSION_ID,
                        "repairTraceId": REPAIR_TRACE_ID,
                        "testStatus": "passed",
                    }
                )

    def test_service_rejects_read_only_nested_forgery_and_unfinished_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trace-repair-http-read-") as tmp:
            service, _runtime = _service(
                tmp,
                include_change=False,
                malicious_nested_change=True,
            )
            with self.assertRaisesRegex(TraceRepairValidationError, "mutating"):
                _record_authoritative_evidence(service)

        # TraceStore intentionally persists terminal canonical traces only.
        # Simulate an in-flight projection at the service seam to prove the
        # repair authority still fails closed before recording evidence.
        with tempfile.TemporaryDirectory(prefix="trace-repair-http-building-") as tmp:
            service, _runtime = _service(tmp)
            building_trace = dict(service.trace_store.get(REPAIR_TRACE_ID) or {})
            building_trace["status"] = "building"
            service.trace_store = SimpleNamespace(
                get=lambda trace_id: building_trace
                if trace_id == REPAIR_TRACE_ID
                else None
            )
            with self.assertRaisesRegex(TraceRepairValidationError, "completed"):
                _record_authoritative_evidence(service)

        for label, kwargs in (
            ("failed trace", {"repair_status": "failed"}),
            ("running Session", {"session_status": "running"}),
            ("cross Session", {"bound_session_id": "agent:other-repair"}),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix="trace-repair-http-terminal-"
            ) as tmp:
                service, _runtime = _service(tmp, **kwargs)
                with self.assertRaises(TraceRepairValidationError):
                    _record_authoritative_evidence(service)

    def test_nonzero_test_exit_cannot_be_upgraded_to_passed_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trace-repair-http-failed-test-") as tmp:
            service, _runtime = _service(tmp, test_exit_code=1)
            change, test = _record_authoritative_evidence(service)
            self.assertEqual(test["testStatus"], "failed")
            with self.assertRaisesRegex(TraceRepairValidationError, "not passed"):
                service.create_trace_repair_receipt(
                    {
                        "schemaVersion": "rag-ime.trace-repair-receipt-create.v1",
                        "sourceScope": "trace-runtime",
                        "sourceTraceId": SOURCE_TRACE_ID,
                        "failureRef": "failure:trace-repair-http",
                        "changeReceiptId": change["evidenceId"],
                        "testEvidenceId": test["evidenceId"],
                        "repairTraceId": REPAIR_TRACE_ID,
                        "repairSessionId": REPAIR_SESSION_ID,
                    }
                )

    def test_concurrent_recheck_invokes_luna_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trace-repair-http-concurrent-") as tmp:
            service, runtime = _service(tmp, judge_delay_seconds=0.05)
            _change, _test, receipt = _create_authoritative_receipt(service)
            request = {
                "schemaVersion": "rag-ime.trace-repair-recheck-request.v1",
                "repairReceiptId": receipt["repairReceiptId"],
            }
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: service.recheck_trace_repair(request), range(2)))
            self.assertEqual(len(runtime.calls), 1)
            self.assertEqual(sorted(bool(result["idempotent"]) for result in results), [False, True])


class TraceRepairHttpTests(unittest.TestCase):
    def test_http_dispatches_write_get_and_recheck_routes(self) -> None:
        calls: list[tuple[str, object]] = []
        receipt_id = "repair-receipt:trace:repair-http:repair"

        class Agent:
            def record_trace_repair_change_evidence(self, payload: object) -> dict[str, object]:
                calls.append(("change", payload))
                return {"schemaVersion": "rag-ime.trace-repair-evidence-write.v1", "ok": True}

            def record_trace_repair_test_evidence(self, payload: object) -> dict[str, object]:
                calls.append(("test", payload))
                return {"schemaVersion": "rag-ime.trace-repair-evidence-write.v1", "ok": True}

            def create_trace_repair_receipt(self, payload: object) -> dict[str, object]:
                calls.append(("create", payload))
                return {"schemaVersion": "rag-ime.trace-repair-receipt-create.v1", "ok": True}

            def get_trace_repair_receipt(self, value: str) -> dict[str, object]:
                calls.append(("get", value))
                return {"schemaVersion": "rag-ime.trace-repair-receipt-get.v1", "ok": True}

            def recheck_trace_repair(self, payload: object) -> dict[str, object]:
                calls.append(("recheck", payload))
                return {"schemaVersion": "rag-ime.trace-repair-recheck.v1", "ok": True}

        service = SimpleNamespace(agent=Agent())
        body = {"schemaVersion": "opaque", "sourceTraceId": SOURCE_TRACE_ID}
        for path, expected_action, expected_status in (
            ("/api/observability/trace-repair/evidence/change", "change", HTTPStatus.CREATED),
            ("/api/observability/trace-repair/evidence/test", "test", HTTPStatus.CREATED),
            ("/api/observability/trace-repair/receipts", "create", HTTPStatus.CREATED),
            ("/api/observability/trace-repair/recheck", "recheck", HTTPStatus.OK),
        ):
            with self.subTest(path=path):
                status, payload = _invoke_post(service, path, body)
                self.assertEqual(status, expected_status)
                self.assertTrue(payload["ok"])
                self.assertEqual(calls[-1][0], expected_action)

        status, payload = _invoke_get(
            service,
            "/api/observability/trace-repair/receipts/" + quote(receipt_id, safe=""),
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(payload["ok"])
        self.assertEqual(calls[-1], ("get", receipt_id))

    def test_http_returns_safe_validation_error(self) -> None:
        class Agent:
            def record_trace_repair_change_evidence(self, payload: object) -> dict[str, object]:
                raise TraceRepairValidationError("PRIVATE workspace path must not escape")

        status, payload = _invoke_post(
            SimpleNamespace(agent=Agent()),
            "/api/observability/trace-repair/evidence/change",
            {},
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(payload["errorCode"], "invalid_trace_repair_request")
        self.assertNotIn("PRIVATE", str(payload))


if __name__ == "__main__":
    unittest.main()
