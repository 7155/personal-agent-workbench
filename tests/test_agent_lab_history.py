from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rag_ime.agent_lab.history import history_collections
from rag_ime.agent_lab.project_application import AgentLabProjectApplication
from rag_ime.agent_lab.projects import AgentLabProjectConflict, AgentLabProjectValidationError
from scripts.import_agent_lab_experiments import read_public_experiments


class LabHistoryImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        _, _, self.records = read_public_experiments(ledger_path=Path(__file__).parents[1] / "eval/interview-metrics/agent-experiments.v1.json")
        self.original = copy.deepcopy(self.records)
        self.reader = Mock(side_effect=lambda: self.records)
        self.sessions = Mock()
        self.app = AgentLabProjectApplication(Path(self.temporary.name) / "lab.sqlite", session_application=self.sessions,
                                             current_model=Mock(side_effect=AssertionError("Import must not select or run a model")),
                                             read_experiments=self.reader)

    def request(self, collection, request_id="once"):
        return {"action": "import_history", "expectedRevision": 0, "clientRequestId": request_id,
                "input": {key: collection[key] for key in ("sceneId", "sourceHash")}}

    def test_product_history_composition_uses_narrow_public_catalog_without_execution(self):
        from rag_ime.agent_service import AgentService
        from rag_ime.eval_lab import EvalLabProjection

        projection = EvalLabProjection(Path(self.temporary.name) / "lab.sqlite",
                                       source_ledger_path=Path(__file__).parents[1] / "eval/interview-metrics/agent-experiments.v1.json")
        # Exercise the actual lazy composition method without booting Runtime.
        service = AgentService.__new__(AgentService)
        service.sessions = projection.sessions
        service.session_application = self.sessions
        service.eval_lab = projection
        service._eval_lab_project_lock = threading.Lock()
        service._eval_lab_project_application = None
        with patch.object(service, "_knowledge_resource", return_value=None), patch("rag_ime.agent_lab.project_application.AgentLabProjectApplication") as application, patch.object(projection, "list_runs", side_effect=AssertionError("history only reads catalog")):
            first = service._lab_project_application()
            self.assertIs(service._lab_project_application(), first)
            self.assertEqual(application.call_args.kwargs["read_experiments"](), projection.list_experiments())
        application.assert_called_once()
        self.sessions.create_in_transaction.assert_not_called()

    def test_six_existing_scenarios_preserve_exact_evidence_without_model_calls(self):
        collections = history_collections(self.records)
        self.assertEqual({row["sceneId"] for row in collections}, {"enterpriseops", "enterprise-rag", "trace-agent", "cloudops", "memory", "model-cost"})
        for source in collections:
            project = self.app.command(self.request(source, source["sceneId"]))["project"]
            origin = project["historyOrigin"]
            snapshot = self.app.read({"projectId": project["projectId"], "artifactId": origin["snapshotArtifactId"],
                                      "artifactRevision": 1})["artifact"]["content"]
            self.assertEqual(snapshot["experiments"], source["records"])
            self.assertFalse(snapshot["executionPerformed"])
            self.assertEqual(project["guideSessionId"], "")
            self.assertEqual(project["materialCount"], 0)
            self.assertEqual(project["bindings"][0]["ownerRef"], {"kind": "scene_trial", "id": source["sceneId"]})
            self.assertEqual(project["artifactCount"], 6)
            steps = self.app.read({"projectId": project["projectId"]})["project"]["artifacts"]
            self.assertIn("逐步实验卡", {item["title"] for item in steps})
            detail = next(item for item in steps if item["title"] == "逐步实验卡")
            # The detailed card keeps the causal change surface and claim boundary;
            # the raw snapshot remains the immutable source of truth.
            detail_payload = self.app.read({"projectId": project["projectId"], "artifactId": detail["artifactId"]})["artifact"]["content"]
            self.assertEqual(len(detail_payload["experiments"]), source["experimentCount"])
            self.assertTrue(all({"steps", "promptChanges", "toolChanges", "workflowChanges", "modelChanges", "effect", "allowedClaim", "forbiddenClaim"}.issubset(item) for item in detail_payload["experiments"]))
            self.assertTrue(all({"continuation", "comparedTo", "metricDeltas"}.issubset(item) for item in detail_payload["experiments"]))
            ids = {item["experimentId"] for item in source["records"]}
            if any(item.get("supersededBy") in ids for item in source["records"]):
                self.assertTrue(any(item["comparedTo"] for item in detail_payload["experiments"]),
                                "supersededBy links must produce adjacent causal comparisons")
            chain = next(item for item in steps if item["title"] == "实验链")
            chain_payload = self.app.read({"projectId": project["projectId"], "artifactId": chain["artifactId"]})["artifact"]["content"]
            self.assertEqual(chain_payload["columns"][-2]["key"], "failureEvidence")
            self.assertEqual(chain_payload["columns"][-1]["key"], "metricDeltas")
            self.assertTrue(all("failureEvidence" in row and "metricDeltas" in row for row in chain_payload["rows"]))
        self.assertEqual(len(self.app.read()["items"]), 6)
        self.assertEqual(self.records, self.original)
        self.sessions.create_in_transaction.assert_not_called()

    def test_replay_survives_source_outage_and_second_request_does_not_duplicate_project(self):
        source = history_collections(self.records)[0]
        request = self.request(source)
        first = self.app.command(request)
        again = self.app.command(self.request(source, "another-click"))
        self.assertEqual(again["project"], first["project"])
        self.reader.side_effect = OSError("source unavailable")
        self.assertEqual(self.app.command(request), {**first, "replayed": True})
        self.assertEqual(len(self.app.store.read()["items"]), 1)

    def test_changed_source_and_browser_supplied_metrics_cannot_be_imported(self):
        source = history_collections(self.records)[0]
        request = self.request(source)
        self.records[0]["title"] += " updated"
        with self.assertRaises(AgentLabProjectConflict):
            self.app.command(request)
        with self.assertRaises(AgentLabProjectValidationError):
            self.app.command({**request, "input": {**request["input"], "metrics": {"passRate": 1}}})
        self.assertEqual(self.app.store.read()["items"], [])

    def test_later_artifact_edits_do_not_replace_original_import_revision(self):
        source = history_collections(self.records)[0]
        project = self.app.command(self.request(source))["project"]
        artifact_id = project["historyOrigin"]["snapshotArtifactId"]
        self.app.command({"action": "publish_artifact", "projectId": project["projectId"], "expectedRevision": 1,
                          "clientRequestId": "edit", "input": {"artifactId": artifact_id, "expectedArtifactRevision": 1,
                          "content": {"note": "user annotation"}}})
        original = self.app.read({"projectId": project["projectId"], "artifactId": artifact_id, "artifactRevision": 1})
        self.assertEqual(original["artifact"]["content"]["experiments"], source["records"])

    def test_bound_guide_can_read_only_its_scene_execution_status(self):
        source = history_collections(self.records)[0]
        project = self.app.command(self.request(source))["project"]
        self.app.read_trials = Mock(return_value={"schemaVersion": "rag-ime.agent-lab-trial.v1", "registeredSceneIds": [],
            "jobs": [{"jobId": "same-scene", "sceneId": source["sceneId"]}, {"jobId": "different-scene", "sceneId": "memory"}]})
        result = self.app._execution(project, "execution_read", {"bindingId": project["bindings"][0]["bindingId"]})
        self.assertFalse(result["execution"]["registered"])
        self.assertEqual([job["jobId"] for job in result["execution"]["jobs"]], ["same-scene"])
