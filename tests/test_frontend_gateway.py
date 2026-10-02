from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from rag_ime.contracts.json_schema import validate_contract
from rag_ime.core_client import FixtureCoreClient
from rag_ime.debug_server import DebugImeService, DebugServerConfig
from rag_ime.frontend_gateway import FrontendGateway
from rag_ime.predictor import NullPredictionProvider


class FrontendGatewayTests(unittest.TestCase):
    def test_generic_suggest_delegates_to_existing_backend_once(self) -> None:
        captured: list[dict[str, object]] = []

        def suggest_handler(payload):
            captured.append(payload)
            return {
                "schemaVersion": "rag-ime.rime-sidecar.v1",
                "sessionId": payload["sessionId"],
                "requestSeq": payload["requestSeq"],
                "rawInput": payload["rawInput"],
                "preedit": payload["preedit"],
                "committedContext": payload["committedContext"],
                "semanticQuery": "继续完成",
                "queryBasis": "committedContext",
                "uiMode": "post_commit_prediction",
                "displayCandidates": [
                    {
                        "candidateStableId": "model:1",
                        "label": "1",
                        "text": "继续完成",
                        "insertText": "继续完成",
                        "sourceType": "model",
                        "selectionAction": "commit_side_candidate",
                        "selectionRank": 1,
                        "displayLane": "local_model",
                        "metadata": {},
                    },
                    {
                        "candidateStableId": "native:0",
                        "label": "2",
                        "text": "继续",
                        "insertText": "继续",
                        "sourceType": "rime",
                        "selectionAction": "select_rime_candidate",
                        "selectionRank": 2,
                        "rimeIndex": 0,
                        "metadata": {},
                    },
                ],
                "predictionSession": {},
                "keyPolicy": {},
                "assistantOverlay": {"visible": True},
                "selectionActions": {},
                "mergePolicy": {},
                "progressive": {},
                "privacyAssessment": {"disposition": "allowed"},
                "storageReceipt": {},
            }

        gateway = FrontendGateway(suggest_handler=suggest_handler, selection_handler=lambda _: {})
        request = self._suggest_request()
        validate_contract(request, "frontend-suggest-request.v1.json")

        response = gateway.suggest(request)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["privacyDisposition"], "allowed")
        self.assertEqual(captured[0]["inputEngine"], "librime")
        self.assertEqual(captured[0]["rimeContext"]["candidates"][0]["text"], "继续")
        self.assertEqual([item["origin"] for item in response["candidates"]], ["model", "native"])
        self.assertEqual(response["candidates"][1]["nativeIndex"], 0)
        self.assertEqual(response["session"]["inputGeneration"], 8)
        self.assertTrue(response["candidates"][0]["snapshotId"])
        self.assertEqual(response["candidates"][0]["inputGeneration"], 8)
        self.assertEqual(response["candidates"][0]["snapshotId"], response["candidates"][1]["snapshotId"])
        validate_contract(response, "frontend-suggest-response.v1.json")

    def test_generic_selection_round_trips_without_duplicating_feedback_logic(self) -> None:
        captured: list[dict[str, object]] = []

        def selection_handler(payload):
            captured.append(payload)
            return {
                "schemaVersion": "rag-ime.rime-selection.v1",
                "ok": True,
                "stored": True,
                "noStore": False,
                "eventId": "event:7",
                "sourceType": payload["candidate"]["sourceType"],
                "insertText": payload["candidate"]["insertText"],
                "recordedActionCount": 1,
                "privacyAssessment": {"disposition": "allowed"},
                "storageReceipt": {"outcome": "stored"},
            }

        gateway = FrontendGateway(suggest_handler=lambda _: {}, selection_handler=selection_handler)
        request = {
            "schemaVersion": "rag-ime.frontend-selection.v1",
            "frontend": {"id": "squirrel", "platform": "macos", "inputEngine": "librime"},
            "session": {"id": "generic-1", "requestSeq": 4, "inputGeneration": 9},
            "privacy": {"disposition": "allowed"},
            "candidate": {
                "id": "rag:1",
                "snapshotId": "snapshot:4",
                "snapshotGeneration": 2,
                "inputGeneration": 9,
                "label": "1",
                "text": "知识候选",
                "insertText": "知识候选",
                "origin": "retrieval",
                "rank": 1,
                "selectionAction": "commit_side_candidate",
                "memoryId": "event:3",
                "sourceEventId": 3,
            },
            "visibleCandidates": [],
            "context": {"query": "知识", "recentText": "个人知识库", "project": "wisdom-weasel-rag-ime"},
        }
        validate_contract(request, "frontend-selection.v1.json")

        response = gateway.select(request)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["candidate"]["sourceType"], "rag")
        self.assertEqual(captured[0]["privacyDisposition"], "allowed")
        self.assertEqual(captured[0]["inputGeneration"], 9)
        self.assertEqual(captured[0]["snapshotId"], "snapshot:4")
        self.assertEqual(captured[0]["snapshotGeneration"], 2)
        self.assertEqual(captured[0]["candidate"]["snapshotGeneration"], 2)
        self.assertEqual(captured[0]["candidate"]["metadata"]["inputGeneration"], 9)
        self.assertEqual(response["origin"], "retrieval")
        self.assertEqual(response["eventId"], "event:7")
        self.assertEqual(
            response["selectionReceipt"],
            {
                "sessionId": "generic-1",
                "requestSeq": 4,
                "inputGeneration": 9,
                "snapshotId": "snapshot:4",
                "snapshotGeneration": 2,
                "candidateId": "rag:1",
                "requestConsistencyValidated": True,
                "runtimeFreshnessValidated": False,
            },
        )
        validate_contract(response, "frontend-selection-response.v1.json")

    def test_selection_rejects_missing_session_generation_before_backend(self) -> None:
        gateway = FrontendGateway(suggest_handler=lambda _: {}, selection_handler=lambda _: self.fail("backend called"))
        request = self._selection_request()
        del request["session"]["inputGeneration"]

        with self.assertRaisesRegex(ValueError, "session.inputGeneration"):
            gateway.select(request)

    def test_selection_rejects_empty_session_before_backend(self) -> None:
        gateway = FrontendGateway(suggest_handler=lambda _: {}, selection_handler=lambda _: self.fail("backend called"))
        request = self._selection_request()
        request["session"]["id"] = ""

        with self.assertRaisesRegex(ValueError, "session.id must not be empty"):
            gateway.select(request)

    def test_suggest_rejects_a_response_for_another_request_sequence(self) -> None:
        def stale_handler(payload):
            return {
                "sessionId": payload["sessionId"],
                "requestSeq": payload["requestSeq"] - 1,
                "displayCandidates": [],
                "predictionSession": {},
            }

        gateway = FrontendGateway(suggest_handler=stale_handler, selection_handler=lambda _: {})

        with self.assertRaisesRegex(ValueError, "does not match the frontend session request"):
            gateway.suggest(self._suggest_request())

    def test_selection_rejects_candidate_from_another_input_generation(self) -> None:
        gateway = FrontendGateway(suggest_handler=lambda _: {}, selection_handler=lambda _: self.fail("backend called"))
        request = self._selection_request()
        request["candidate"]["inputGeneration"] = 10

        with self.assertRaisesRegex(ValueError, "does not match session"):
            gateway.select(request)

    def test_selection_rejects_mixed_visible_candidate_snapshots(self) -> None:
        gateway = FrontendGateway(suggest_handler=lambda _: {}, selection_handler=lambda _: self.fail("backend called"))
        request = self._selection_request()
        request["visibleCandidates"] = [
            {
                **request["candidate"],
                "id": "model:2",
                "snapshotId": "snapshot:stale",
            }
        ]

        with self.assertRaisesRegex(ValueError, "must match the selected candidate snapshot"):
            gateway.select(request)

    def test_capabilities_define_one_shared_backend_boundary(self) -> None:
        gateway = FrontendGateway(suggest_handler=lambda _: {}, selection_handler=lambda _: {})

        capabilities = gateway.capabilities()

        self.assertEqual(capabilities["adapterBoundary"]["nativeCandidateOwner"], "frontend_adapter")
        self.assertEqual(capabilities["adapterBoundary"]["modelRagMemoryOwner"], "shared_backend")
        self.assertTrue(capabilities["features"]["privacyAttestationRequired"])
        validate_contract(capabilities, "frontend-capabilities.v1.json")

    def test_native_candidate_selection_stays_owned_by_the_adapter(self) -> None:
        gateway = FrontendGateway(suggest_handler=lambda _: {}, selection_handler=lambda _: self.fail("backend called"))
        request = {
            "schemaVersion": "rag-ime.frontend-selection.v1",
            "frontend": {"id": "squirrel", "platform": "macos", "inputEngine": "librime"},
            "session": {"id": "generic-1", "requestSeq": 5, "inputGeneration": 9},
            "privacy": {"disposition": "allowed"},
            "candidate": {
                "id": "native:0",
                "text": "继续",
                "insertText": "继续",
                "origin": "native",
            },
        }

        with self.assertRaisesRegex(ValueError, "owned by the frontend adapter"):
            gateway.select(request)

    def test_debug_service_generic_entry_uses_the_existing_suggest_pipeline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-frontend-gateway-") as tmp:
            service = DebugImeService(
                DebugServerConfig(
                    db_path=Path(tmp) / "gateway.sqlite",
                    core=FixtureCoreClient(),
                    predictor=NullPredictionProvider(),
                    seed_if_empty=False,
                )
            )

            try:
                response = service.frontend_suggest(self._suggest_request())
            finally:
                service.close()

        self.assertEqual(response["schemaVersion"], "rag-ime.frontend-suggest-response.v1")
        self.assertTrue(any(item["origin"] == "native" for item in response["candidates"]))
        self.assertEqual(response["diagnostics"]["backendContract"], "rag-ime.rime-sidecar.v1")
        validate_contract(response, "frontend-suggest-response.v1.json")

    @staticmethod
    def _suggest_request() -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.frontend-suggest-request.v1",
            "frontend": {
                "id": "squirrel",
                "build": "test",
                "platform": "macos",
                "inputFramework": "InputMethodKit",
                "inputEngine": "librime",
            },
            "session": {"id": "generic-1", "requestSeq": 3, "inputGeneration": 8},
            "privacy": {"disposition": "allowed"},
            "input": {
                "raw": "jixu",
                "preedit": "jixu",
                "commitPreview": "",
                "committedContext": "现在继续",
                "idleMs": 80,
            },
            "context": {"project": "wisdom-weasel-rag-ime", "appId": "com.apple.TextEdit"},
            "nativeCandidates": [
                {"id": "native:0", "label": "1", "text": "继续", "annotation": "native", "rank": 1, "nativeIndex": 0}
            ],
            "nativeState": {"highlightedIndex": 0, "page": 0, "isLastPage": True},
            "limits": {"latencyBudgetMs": 900, "maxVisibleCandidates": 8, "maxAssistantCandidates": 5},
            "flags": {"predictionFirst": True},
        }

    @staticmethod
    def _selection_request() -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.frontend-selection.v1",
            "frontend": {"id": "squirrel", "platform": "macos", "inputEngine": "librime"},
            "session": {"id": "generic-1", "requestSeq": 4, "inputGeneration": 9},
            "privacy": {"disposition": "allowed"},
            "candidate": {
                "id": "model:1",
                "snapshotId": "snapshot:4",
                "snapshotGeneration": 2,
                "inputGeneration": 9,
                "text": "继续完成",
                "insertText": "继续完成",
                "origin": "model",
                "rank": 1,
            },
            "visibleCandidates": [],
        }


if __name__ == "__main__":
    unittest.main()
