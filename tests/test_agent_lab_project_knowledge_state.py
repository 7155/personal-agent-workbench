from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from rag_ime.agent_lab.knowledge import AgentLabKnowledgeResource
from rag_ime.agent_lab.project_application import AgentLabProjectApplication
from rag_ime.agent_lab.projects import _project_work_summary


class ProjectKnowledgeStateTests(unittest.TestCase):
    def test_completed_knowledge_materials_are_recognized_in_detail_catalog_and_command_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            jobs = []
            knowledge = AgentLabKnowledgeResource.__new__(AgentLabKnowledgeResource)
            knowledge.read_trials = Mock(side_effect=lambda _identifier: {"jobs": jobs})
            knowledge.settings = lambda: {}
            sessions = Mock()
            app = AgentLabProjectApplication(Path(temporary) / "paw.sqlite", session_application=sessions,
                current_model=lambda: {}, knowledge=knowledge, start_knowledge=Mock())
            app.store._create_guide = None
            app.directory = Mock()
            app.directory.sync.return_value = {}
            created = app.command({"action": "create", "expectedRevision": 0, "clientRequestId": "create",
                "input": {"description": "比较两篇极地论文"}})["project"]
            project_id = created["projectId"]
            self.assertEqual(created["workState"]["status"], "draft")
            for index, (kind, data) in enumerate([
                ("corpus", {"documentCount": 209, "corpusHash": "full-corpus"}),
                ("index", {"documentCount": 209, "chunkCount": 20017, "corpusHash": "full-corpus"}),
                ("dataset", {"dataset": {"caseCount": 48}}),
            ]):
                jobs.append({"jobId": f"resource-{index}", "sceneId": "knowledge-resource", "state": "completed",
                    "updatedAtMs": index + 1, "publicSpec": {"projectId": project_id, "operation": kind},
                    "result": {"schemaVersion": "paw.lab-knowledge-resource.v1", "kind": kind, **data}})
            selected = app.read({"projectId": project_id})["project"]
            summary = app.read()["items"][0]
            for value in (selected, summary):
                self.assertEqual(value["materialCount"], 0)  # The separate text material store remains truthful.
                self.assertEqual(value["knowledgeResources"]["documentCount"], 209)
                self.assertEqual(value["knowledgeResources"]["chunkCount"], 20017)
                self.assertEqual(value["knowledgeResources"]["datasetCount"], 1)
                self.assertEqual(value["workState"]["status"], "needs_binding")
                self.assertEqual(value["nextAction"]["kind"], "bind_execution")
                self.assertNotIn("至少一份当前材料快照", value["rerunReadiness"]["missing"])
            updated = app.command({"action": "update_brief", "projectId": project_id, "expectedRevision": created["revision"],
                "clientRequestId": "update", "input": {"description": "继续原来的论文研究"}})["project"]
            self.assertEqual(updated["workState"], selected["workState"])
            app.start_knowledge.assert_not_called()
            self.assertEqual(sessions.mock_calls, [])
            ready = _project_work_summary({**selected, "bindings": [{"bindingId": "original-owner"}]}, [], selected["knowledgeResources"])
            self.assertEqual(ready["workState"]["status"], "ready")
            self.assertEqual(ready["rerunReadiness"]["status"], "ready")

    def test_failed_and_foreign_resource_receipts_do_not_turn_an_empty_project_ready(self):
        knowledge = AgentLabKnowledgeResource.__new__(AgentLabKnowledgeResource)
        knowledge.read_trials = Mock(return_value={"jobs": [
            {"jobId": "failed", "sceneId": "knowledge-resource", "state": "failed", "publicSpec": {"projectId": "target"},
             "result": {"schemaVersion": "paw.lab-knowledge-resource.v1", "kind": "corpus", "documentCount": 209}},
            {"jobId": "foreign", "sceneId": "knowledge-resource", "state": "completed", "publicSpec": {"projectId": "other"},
             "result": {"schemaVersion": "paw.lab-knowledge-resource.v1", "kind": "corpus", "documentCount": 209}},
        ]})
        resources = knowledge.project_resources()
        self.assertNotIn("target", resources)
        summary = _project_work_summary({"materialCount": 0, "bindings": []}, knowledge=resources.get("target"))
        self.assertEqual(summary["workState"]["status"], "draft")
        self.assertEqual(summary["nextAction"]["kind"], "add_materials")


if __name__ == "__main__":
    unittest.main()
