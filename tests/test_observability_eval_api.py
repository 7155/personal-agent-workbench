from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rag_ime.agent_service import AgentService
from rag_ime.contracts.json_schema import validate_contract
from rag_ime.control_api import ControlPathId, ControlScope, default_route_policy
from rag_ime.debug_server import DebugRequestHandler
from rag_ime.eval_run_store import EvalRunStore
from rag_ime.observability import ObservationHub
from rag_ime.trace_runtime import EvidenceRef, TraceContractError, build_trace_envelope, make_span
from rag_ime.trace_runtime import build_eval_run


def _trace_detail(*, truncated: bool = False, status: str = "completed") -> dict[str, object]:
    trace = build_trace_envelope(
        trace_id="trace:eval:1",
        source_kind="active_rag",
        input_text="private question",
        spans=(
            make_span(
                span_id="span:eval:retrieve",
                name="rag.retrieve",
                started_at_ms=10,
                ended_at_ms=12,
            ),
        ),
        evidence=(
            EvidenceRef(
                evidence_id="knowledge:answer",
                source_kind="knowledge",
                source_ref="knowledge://answer",
                evidence_stage="retrieval_output",
                disposition="included",
            ),
        ),
        status=status,
        now_ms=20,
    ).to_dict()
    return {
        "schemaVersion": "rag-ime.observability-trace-get.v1",
        "traceId": "trace:eval:1",
        "trace": trace,
        "truncated": truncated,
        "projectionSource": "observation_journal",
        "observationWindow": {
            "firstSequence": 1,
            "lastSequence": 1,
            "resumeToken": "observation:1",
            "nextBeforeSequence": 1 if truncated else None,
        },
    }


def _request() -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.observability-evidence-eval-request.v1",
        "traceId": "trace:eval:1",
        "requiredEvidenceIds": ["knowledge:answer"],
        "datasetId": "manual:trace-eval-1",
        "labelRevision": "review:1",
        "truthKind": "human",
    }


def _invoke_get(service: object, path: str) -> tuple[object, dict[str, object]]:
    handler = DebugRequestHandler.__new__(DebugRequestHandler)
    handler.service = service  # type: ignore[assignment]
    handler.path = path
    handler._authorize_gateway_request = lambda method, parsed: True  # type: ignore[method-assign]
    handler._serve_gateway_static = lambda request_path: False  # type: ignore[method-assign]
    responses: list[tuple[object, dict[str, object]]] = []
    handler._write_json = lambda status, payload, **kwargs: responses.append((status, payload))  # type: ignore[method-assign]
    handler.do_GET()
    if len(responses) != 1:
        raise AssertionError(f"expected one response, received {len(responses)}")
    return responses[0]


def _invoke_post(
    service: object,
    path: str,
    body: dict[str, object],
) -> tuple[object, dict[str, object]]:
    handler = DebugRequestHandler.__new__(DebugRequestHandler)
    handler.service = service  # type: ignore[assignment]
    handler.path = path
    handler._authorize_gateway_request = lambda method, parsed: True  # type: ignore[method-assign]
    handler._management_post_security_error = lambda request_path, **kwargs: None  # type: ignore[method-assign]
    handler._read_json = lambda: dict(body)  # type: ignore[method-assign]
    responses: list[tuple[object, dict[str, object]]] = []
    handler._write_json = lambda status, payload, **kwargs: responses.append((status, payload))  # type: ignore[method-assign]
    handler.do_POST()
    if len(responses) != 1:
        raise AssertionError(f"expected one response, received {len(responses)}")
    return responses[0]


class ObservabilityEvalServiceTests(unittest.TestCase):
    def test_ai_judge_defaults_to_current_model_and_records_estimate_authority(self) -> None:
        class JudgeRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def complete_once(self, **kwargs: object) -> dict[str, object]:
                self.calls.append(dict(kwargs))
                return {
                    "text": json.dumps({
                        "relevance": 0.9,
                        "coverage": 0.8,
                        "groundedness": 1.0,
                        "contradiction": 0.0,
                        "confidence": 0.7,
                    }),
                    "elapsedMs": 123,
                    "usage": {"totalTokens": 42},
                }

        with tempfile.TemporaryDirectory(prefix="observability-ai-judge-") as tmp:
            service = AgentService.__new__(AgentService)
            service.eval_runs = EvalRunStore(Path(tmp) / "eval.sqlite")
            service.observation_trace = lambda payload: _trace_detail()  # type: ignore[method-assign]
            runtime = JudgeRuntime()
            service.runtime = runtime

            result = service.evaluate_observation_ai_judge({"traceId": "trace:eval:1"})

            self.assertEqual(runtime.calls[0]["provider"], "openai-codex")
            self.assertEqual(runtime.calls[0]["model_id"], "gpt-6.1-sol")
            self.assertEqual(runtime.calls[0]["thinking_level"], "max")
            self.assertEqual(result["mode"], "ai_judge")
            self.assertEqual(result["metricAuthority"], "ai_judge_estimate")
            self.assertEqual(result["truth"]["status"], "none")
            self.assertEqual(result["evaluator"], {
                "provider": "openai-codex",
                "model": "gpt-6.1-sol",
                "thinking": "max",
                "displayName": "GPT-6.1 Sol Max",
            })
            self.assertEqual(result["requestedEvaluator"], result["evaluator"])
            self.assertEqual(result["promptVersion"], "trace-eval-ai-judge-v1")
            self.assertEqual(result["rubricVersion"], "trace-eval-ai-judge-v1")
            self.assertEqual(
                result["inputTraceFingerprint"],
                _trace_detail()["trace"]["input"]["fingerprint"],
            )
            self.assertIsInstance(result["startedAtMs"], int)
            self.assertIsInstance(result["completedAtMs"], int)
            self.assertGreaterEqual(result["completedAtMs"], result["startedAtMs"])
            self.assertIsInstance(result["elapsedMs"], int)
            self.assertGreaterEqual(result["elapsedMs"], 0)
            self.assertEqual(result["latencyMs"], 123)
            self.assertEqual(result["usage"], {"totalTokens": 42})
            self.assertFalse(result["fallbackUsed"])
            self.assertEqual(result["metrics"]["relevance"], 0.9)
            self.assertNotIn("private question", str(result))

    def test_ai_judge_failure_is_explicit_without_private_error_payload(self) -> None:
        class FailingJudgeRuntime:
            def complete_once(self, **kwargs: object) -> dict[str, object]:
                raise RuntimeError("PRIVATE provider token and local path")

        with tempfile.TemporaryDirectory(prefix="observability-ai-judge-failure-") as tmp:
            service = AgentService.__new__(AgentService)
            service.eval_runs = EvalRunStore(Path(tmp) / "eval.sqlite")
            service.observation_trace = lambda payload: _trace_detail()  # type: ignore[method-assign]
            service.runtime = FailingJudgeRuntime()

            result = service.evaluate_observation_ai_judge({"traceId": "trace:eval:1"})

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failureCode"], "ai_judge_request_failed")
            self.assertEqual(result["promptVersion"], "trace-eval-ai-judge-v1")
            self.assertEqual(result["rubricVersion"], "trace-eval-ai-judge-v1")
            self.assertEqual(
                result["inputTraceFingerprint"],
                _trace_detail()["trace"]["input"]["fingerprint"],
            )
            self.assertEqual(result["metrics"], {})
            self.assertNotIn("PRIVATE provider token", str(result))
            self.assertNotIn("local path", str(result))

    def test_eval_suite_catalog_is_a_safe_registry_projection(self) -> None:
        service = AgentService.__new__(AgentService)

        result = service.list_eval_suites({"limit": "10"})

        self.assertEqual(result["schemaVersion"], "rag-ime.eval-suite-list.v1")
        self.assertEqual([item["suiteId"] for item in result["items"]], ["earth-research", "sgg", "zhanggui-wenshu"])
        by_id = {item["suiteId"]: item for item in result["items"]}
        self.assertEqual(by_id["sgg"]["suiteRevision"], "fixture-v2")
        self.assertEqual(by_id["sgg"]["fixtureCount"], 1)
        self.assertEqual(by_id["earth-research"]["suiteRevision"], "source-grounding-v1")
        for item in result["items"]:
            self.assertEqual(set(item), {"suiteId", "suiteRevision", "displayName", "fixtureCount", "capabilities"})
        self.assertNotIn("fixturePath", repr(result))
        self.assertNotIn("requiredEvidenceIds", repr(result))

    def test_eval_suite_catalog_has_a_local_and_gateway_read_route(self) -> None:
        service = SimpleNamespace(agent=AgentService.__new__(AgentService))
        service.agent.list_eval_suites = lambda payload=None: {
            "schemaVersion": "rag-ime.eval-suite-list.v1",
            "ok": True,
            "items": [],
        }

        for path in (
            "/api/observability/eval-suites",
            "/control/v1/observability/eval-suites",
        ):
            status, payload = _invoke_get(service, path)
            self.assertEqual(status, 200)
            self.assertEqual(payload["schemaVersion"], "rag-ime.eval-suite-list.v1")
            validate_contract(payload, "eval-suite-list.v1.json")

    def test_memory_recall_journal_trace_and_eval_share_turn_binding(self) -> None:
        with tempfile.TemporaryDirectory(prefix="observability-memory-eval-") as tmp:
            observations = ObservationHub(Path(tmp) / "observations.sqlite")
            try:
                observations.enqueue_memory_recall_record({
                    "recallId": "session-memory-recall:integration",
                    "sessionId": "session-integration",
                    "turnId": "turn:integration",
                    "trigger": "turn_start",
                    "generatedAtMs": 100,
                    "metrics": {"selectedCount": 1},
                    "attributes": {"embeddingFallback": False},
                    "traceEvidence": [{
                        "evidenceId": "memory:integration",
                        "sourceKind": "memory",
                        "sourceRef": "memory:integration",
                        "sourceLane": "bm25_raw",
                        "disposition": "included",
                        "scores": {"score": 0.75},
                        "rankBefore": None,
                        "rankAfter": 1,
                        "omissionReason": "",
                    }],
                })
                self.assertTrue(observations.flush(timeout_seconds=2.0))

                service = AgentService.__new__(AgentService)
                service.observations = observations
                service.eval_runs = EvalRunStore(Path(tmp) / "eval.sqlite")

                trace_id = "trace:memory-recall:session-memory-recall:integration"
                detail = service.observation_trace({"traceId": trace_id})
                trace = detail["trace"]
                self.assertEqual(
                    trace["binding"],
                    {
                        "sessionId": "session-integration",
                        "turnId": "turn:integration",
                        "runId": "session-memory-recall:integration",
                    },
                )
                self.assertIn(
                    "memory:integration",
                    {item["evidenceId"] for item in trace["evidence"]},
                )

                evaluated = service.evaluate_observation_evidence({
                    "schemaVersion": "rag-ime.observability-evidence-eval-request.v1",
                    "traceId": trace_id,
                    "requiredEvidenceIds": ["memory:integration"],
                    "datasetId": "manual:memory-integration",
                    "labelRevision": "review:memory-integration-1",
                    "truthKind": "human",
                })
                listing = service.observation_evals({"traceId": trace_id})

                self.assertEqual(evaluated["traceIds"], [trace_id])
                self.assertEqual(listing["total"], 1)
                self.assertEqual(
                    listing["items"][0]["evalRunId"],
                    evaluated["evalRunId"],
                )
            finally:
                observations.close()

    def test_runs_and_persists_human_ground_truth_without_copying_trace_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="observability-eval-api-") as tmp:
            service = AgentService.__new__(AgentService)
            service.eval_runs = EvalRunStore(Path(tmp) / "eval.sqlite")
            service.observation_trace = lambda payload: _trace_detail()  # type: ignore[method-assign]

            first = service.evaluate_observation_evidence(_request())
            second = service.evaluate_observation_evidence(_request())
            listing = service.observation_evals({"traceId": "trace:eval:1"})

            self.assertEqual(first, second)
            self.assertEqual(first["metricAuthority"], "ground_truth")
            self.assertEqual(first["truth"]["status"], "human")
            self.assertEqual(first["evaluator"]["displayName"], "Human labels")
            self.assertEqual(first["metrics"], {"precision": 1.0, "recall": 1.0, "f1": 1.0})
            self.assertEqual(len(listing["items"]), 1)
            self.assertEqual(listing["total"], 1)
            self.assertFalse(listing["truncated"])
            self.assertEqual(listing["items"][0]["evalRunId"], first["evalRunId"])
            self.assertNotIn("private question", str(first))
            self.assertNotIn("private question", str(listing))
            validate_contract(listing, "observability-eval-list.v1.json")

    def test_eval_listing_keeps_the_exact_vertical_suite_revision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="observability-suite-list-") as tmp:
            service = AgentService.__new__(AgentService)
            service.eval_runs = EvalRunStore(Path(tmp) / "eval.sqlite")
            service.eval_runs.persist(build_eval_run(
                eval_run_id="eval:sgg:fixture-v2",
                trace_ids=["trace:eval:1"],
                mode="ground_truth",
                truth_kind="frozen",
                dataset_id="sgg-fixtures",
                label_revision="fixture-v2",
                metrics={"f1": 1.0},
                suite_binding={"suiteId": "sgg", "suiteRevision": "fixture-v2"},
                now_ms=20,
            ))

            listing = service.observation_evals({"traceId": "trace:eval:1"})

            self.assertEqual(
                listing["items"][0]["suiteBinding"],
                {"suiteId": "sgg", "suiteRevision": "fixture-v2"},
            )
            validate_contract(listing, "observability-eval-list.v1.json")

    def test_rejects_truncated_running_or_unversioned_eval_requests(self) -> None:
        with tempfile.TemporaryDirectory(prefix="observability-eval-api-") as tmp:
            service = AgentService.__new__(AgentService)
            service.eval_runs = EvalRunStore(Path(tmp) / "eval.sqlite")
            service.observation_trace = lambda payload: _trace_detail(truncated=True)  # type: ignore[method-assign]
            with self.assertRaisesRegex(TraceContractError, "complete observation window"):
                service.evaluate_observation_evidence(_request())

            service.observation_trace = lambda payload: _trace_detail(status="building")  # type: ignore[method-assign]
            with self.assertRaisesRegex(TraceContractError, "completed traces"):
                service.evaluate_observation_evidence(_request())

            invalid = _request()
            invalid.pop("schemaVersion")
            with self.assertRaises(ValueError):
                service.evaluate_observation_evidence(invalid)

            false_frozen_claim = _request()
            false_frozen_claim["truthKind"] = "frozen"
            with self.assertRaises(ValueError):
                service.evaluate_observation_evidence(false_frozen_claim)


class ObservabilityEvalRouteTests(unittest.TestCase):
    def test_route_policy_keeps_read_remote_safe_and_eval_mutation_local(self) -> None:
        policy = default_route_policy()
        listing = policy.resolve(ControlPathId.OBSERVABILITY_EVALS_LIST)
        mutation = policy.resolve(ControlPathId.OBSERVABILITY_EVIDENCE_EVAL_RUN)

        self.assertEqual(listing.local_8766_path, "/api/observability/evals")
        self.assertEqual(listing.gateway_8768_path, "/control/v1/observability/evals")
        self.assertTrue(listing.remote_safe)
        self.assertEqual(listing.remote_scopes, frozenset({ControlScope.AGENT_READ.value}))
        self.assertEqual(mutation.local_8766_path, "/api/observability/evals/evidence-ground-truth")
        self.assertIsNone(mutation.gateway_8768_path)
        self.assertFalse(mutation.remote_safe)

    def test_http_handlers_expose_eval_list_and_manual_run(self) -> None:
        agent = SimpleNamespace(
            observation_evals=lambda payload: {
                "schemaVersion": "rag-ime.observability-eval-list.v1",
                "traceId": payload["traceId"],
                "total": 0,
                "truncated": False,
                "items": [],
            },
            evaluate_observation_evidence=lambda payload: {
                "schemaVersion": "rag-ime.eval-run.v1",
                "evalRunId": "eval:manual:1",
            },
        )
        service = SimpleNamespace(agent=agent, config=SimpleNamespace(server_name="test"))

        get_status, listing = _invoke_get(
            service,
            "/api/observability/evals?traceId=trace%3Aeval%3A1&limit=20",
        )
        post_status, run = _invoke_post(
            service,
            "/api/observability/evals/evidence-ground-truth",
            _request(),
        )

        self.assertEqual(int(get_status), 200)
        self.assertEqual(listing["traceId"], "trace:eval:1")
        self.assertEqual(int(post_status), 200)
        self.assertEqual(run["evalRunId"], "eval:manual:1")

    def test_eval_list_never_reflects_contract_error_content(self) -> None:
        sentinel = "PRIVATE_EVAL_CONTENT /Volumes/private/eval.json"
        agent = SimpleNamespace(
            observation_evals=lambda payload: (_ for _ in ()).throw(TraceContractError(sentinel))
        )
        service = SimpleNamespace(agent=agent, config=SimpleNamespace(server_name="test"))

        status, payload = _invoke_get(
            service,
            "/api/observability/evals?traceId=trace%3Aeval%3A1",
        )

        self.assertEqual(int(status), 400)
        self.assertEqual(payload, {"ok": False, "error": "Invalid Eval request"})
        self.assertNotIn("PRIVATE_EVAL_CONTENT", str(payload))
        self.assertNotIn("/Volumes/private", str(payload))


if __name__ == "__main__":
    unittest.main()
