"""Scoped context uses actual owner reads; provider/Pi calls are test doubles."""
from __future__ import annotations

import unittest
import tempfile
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from rag_ime.agent_context_runtime import AgentContextRuntime
from rag_ime.agent_media import AgentMediaStore
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_workspace import WorkspaceHarness
from rag_ime.rooms.store import AgentRoomStore
from rag_ime.rooms.work import AgentRoomWorkStore
from rag_ime.work_documents import WorkDocumentService
from rag_ime.jev_tasks.context import Material, build_manifest
from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.ledger import GraphLedger
from rag_ime.jev_tasks.materials import JevMaterialService
from rag_ime.jev_tasks.types import Candidate, Edge, GraphError, canonical


class ManifestTests(unittest.TestCase):
    def test_optional_receipt_metadata_cannot_displace_complete_revision_feedback(self):
        # Mirrors the installed revision-2 failure without private task content:
        # three controlling bodies fit, but ten duplicated read bindings do not.
        requirements = [Material(f"required:{index}", "2", "Requirements", f"work:{index}", "",
            original=character * size, readable=True, controlling=True)
            for index, (character, size) in enumerate((("r", 3833), ("t", 3437), ("f", 5763)))]
        background = [Material("material:" + str(index).zfill(32), "sha256:" + "a" * 64,
            "Background", "workspace:/workspace/project/document-" + str(index) + ".md", "",
            original="optional body " * 400, readable=True) for index in range(10)]
        receipts = [{"materialId": item.id, "sourceRef": item.source_ref, "revision": item.revision,
            "contentSha256": "b" * 64, "reader": "WorkDocumentService/WorkspaceHarness", "complete": True,
            "selector": "", "displayedRanges": [{"startLine": 1, "endLine": 120}],
            "taskId": "room-work:" + "t" * 36, "taskRevision": 2,
            "ownerId": "room-participant:" + "o" * 36, "assignmentKey": "assignment:" + "a" * 64,
            "sessionId": "agent:" + "s" * 36} for item in background]
        inputs = requirements + background
        selections = {item.id: "read_exact" for item in inputs}
        kwargs = {"byte_budget": 24000, "read_receipts": receipts,
                  "execution_scope": {"workspaceRoots": ["/workspace/project"], "writeRoots": ["/workspace/project"]}}
        result = build_manifest("room-work:" + "t" * 36, 2, inputs, selections, **kwargs)
        self.assertFalse(result.missing)
        for original in requirements:
            self.assertEqual(next(item.content for item in result.items if item.id == original.id), original.original)
        self.assertEqual({item.id for item in result.items}, set(selections))
        self.assertTrue(all(item.mode == "reference" for item in result.items if not item.required))
        payload = result.for_executor(attempt_id="jev-dispatch:" + "d" * 40)
        self.assertLess(len(payload["readReceipts"]), len(receipts))
        self.assertTrue(any("optional read receipt metadata omitted" in notice for notice in result.notices))
        self.assertLessEqual(len(canonical(payload).encode()), 24000)
        rebuilt = build_manifest(result.task_id, result.task_revision, inputs,
            {item["id"]: item["selection"] for item in payload["materials"]}, **kwargs)
        self.assertEqual(rebuilt.for_executor(attempt_id="jev-dispatch:" + "d" * 40), payload)
        candidate = Candidate.make("claim_dispatch", result.task_id, "Check complete feedback",
                                   {"dispatchId": "jev-dispatch:" + "d" * 40, "contextManifest": payload,
                                    "execution": {"status": "idle"}})
        self.assertLessEqual(len(candidate.arguments_json.encode()), 65536)

    def test_optional_current_summary_is_reduced_before_required_original(self):
        required = Material("required", "1", "Exact input", "work:required", "", original="exact " * 100,
                            readable=True, required=True)
        optional = Material("optional", "1", "Background", "work:optional", "", original="large " * 500,
                            summary="summary " * 130, summary_revision="1", readable=True)
        result = build_manifest("task", 1, [required, optional], {"required": "read_exact", "optional": "inline"},
                                byte_budget=1600)
        self.assertEqual(result.items[0].content, required.original)
        self.assertEqual(result.items[1].mode, "reference")
        self.assertFalse(result.missing)

    def test_required_read_receipt_is_preserved_and_explicit_budget_still_fails_closed(self):
        required = Material("required", "1", "Exact input", "work:required", "", original="Exact original",
                            readable=True, required=True)
        optional = Material("optional", "1", "Background", "work:optional", "", original="Background",
                            readable=True)
        receipts = [{"materialId": item.id, "contentSha256": "a" * 64, "binding": "x" * 800}
                    for item in (required, optional)]
        result = build_manifest("task", 1, [required, optional],
            {"required": "read_exact", "optional": "read_exact"}, byte_budget=1800, read_receipts=receipts)
        self.assertEqual(result.for_executor()["readReceipts"], receipts[:1])
        self.assertEqual(result.items[0].content, "Exact original")
        self.assertFalse(result.missing)
        controlling = Material("contract", "1", "Contract", "work:contract", "", original="重要要求" * 400,
                               readable=True, controlling=True)
        with self.assertRaisesRegex(GraphError, "no mandatory content was silently dropped"):
            build_manifest("task", 1, [controlling, optional], {"optional": "reference"},
                           byte_budget=1800, read_receipts=receipts[1:])

    def test_stale_summary_uses_already_read_original(self):
        material = Material("doc", "new", "Title", "workspace:doc.md", "",
                            original="Current original: limit 12", summary="Old limit 7",
                            summary_revision="old", readable=True)
        manifest = build_manifest("task", 1, [material], {"doc": "summary"})
        self.assertEqual(manifest.items[0].content, "Current original: limit 12")
        self.assertEqual(manifest.items[0].mode, "read_exact")
        self.assertEqual(manifest.missing, ())

    def test_optional_unavailable_original_is_not_a_dispatch_blocker(self):
        material = Material("optional", "unavailable", "Title", "workspace:optional.md", "",
                            readable=True)
        manifest = build_manifest("task", 1, [material], {"optional": "inline"})
        self.assertEqual(manifest.missing, ())
        self.assertTrue(manifest.notices)

    def test_budget_preserves_requirements_and_uses_only_current_summary(self):
        required = Material("requirements", "1", "Requirements", "work:1", "",
                            original="Do not delete sources. Limit is 12.",
                            readable=True, controlling=True)
        background = Material("background", "2", "Background", "workspace:b", "",
                              original="large " * 3000, summary="Current background",
                              summary_revision="2", readable=True)
        result = build_manifest("task", 1, [required, background],
                                {"requirements": "omit", "background": "inline"}, byte_budget=1300)
        self.assertEqual(result.items[0].content, required.original)
        self.assertEqual(result.items[1].content, "Current background")
        self.assertEqual(result.items[1].mode, "summary")
        with self.assertRaises(GraphError):
            build_manifest("task", 1, [required], {}, byte_budget=512,
                           execution_scope={"tooLarge": "x" * 600})

    def test_required_missing_and_controlling_missing_have_distinct_outcomes(self):
        required = Material("input", "1", "Input", "workspace:input", "",
                            required=True, readable=True)
        result = build_manifest("task", 1, [required], {"input": "omit"})
        self.assertTrue(result.missing)
        self.assertTrue(result.for_executor()["materials"][0]["needsOriginalRead"])
        controlling = Material("requirements", "1", "Required", "work:1", "",
                               controlling=True, readable=True)
        with self.assertRaisesRegex(GraphError, "controlling original"):
            build_manifest("task", 1, [controlling], {})

    def test_large_required_input_returns_a_scoped_read_need_without_dropping_requirements(self):
        required = Material("input", "1", "Input", "workspace:input", "",
                            original="exact source " * 2000, required=True, readable=True)
        contract = Material("contract", "1", "Contract", "work:1", "",
                            original="Never change 12 to 7", controlling=True, readable=True)
        result = build_manifest("task", 1, [contract, required],
                                {"contract": "inline", "input": "read_exact"}, byte_budget=1300)
        self.assertEqual(result.items[0].content, "Never change 12 to 7")
        self.assertTrue(result.missing)
        self.assertIsNone(result.items[1].content)
        self.assertTrue(result.for_executor()["materials"][1]["needsOriginalRead"])


class MaterialPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-jev-materials-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.sqlite"
        self.sessions = AgentSessionStore(self.db)
        self.sessions.initialize()
        self.session = self.sessions.create(title="Context executor", mode="coordinator",
            execution_mode="read_only", workspace_roots=[str(self.root)])
        other = self.sessions.create(title="Other participant")
        self.rooms = AgentRoomStore(self.db, room_dir=self.root / "rooms")
        self.rooms.initialize()
        self.room = self.rooms.create(title="Context test", routing_policy="moderator", participants=[
            {"sessionId": self.session["id"], "roleId": "coordinator", "roleVersion": "1",
             "displayName": "Context executor", "collaborationRole": "coordinator"},
            {"sessionId": other["id"], "roleId": "implementer", "roleVersion": "1",
             "displayName": "Other", "collaborationRole": "implementer"}])
        self.actor = self.room["participants"][0]
        self.work = AgentRoomWorkStore(self.db)
        self.work.initialize()
        self.root_work = self.create_work("root")
        self.first = self.create_work("first", self.root_work["id"])
        self.second = self.create_work("second", self.root_work["id"])
        self.ledger = GraphLedger(self.db)
        self.ledger.register_created_root(graph_id="graph:test", room_id=self.room["id"],
            root_id="root:test", root_work_id=self.root_work["id"], controller_id="controller:test",
            participant_id=self.actor["id"], session_id=self.session["id"])
        context = AgentContextRuntime(self.db)
        context.initialize()
        self.documents = WorkDocumentService(self.db, sessions=self.sessions, context_runtime=context)
        self.documents.initialize()
        self.harness = WorkspaceHarness()
        self.addCleanup(self.harness.close_lsp)
        self.service = SimpleNamespace(rooms=self.rooms, sessions=self.sessions, room_work=self.work,
            work_documents=self.documents, background_jobs=SimpleNamespace(workspace_harness=self.harness),
            _runtime_tool_manifest=lambda session: [{"name": "workspace_read"}],
            _work_document_for_authority=self.document_for)
        self.materials = JevMaterialService(self.service)

    def create_work(self, name, parent=""):
        return self.work.create(room_id=self.room["id"], objective="Task " + name,
            expected_output="Verifiable output", acceptance_criteria=["Keep exact limit 12"],
            current_owner_participant_id=self.actor["id"], created_by_participant_id=self.actor["id"],
            accountable_participant_id=self.actor["id"], client_message_id="create:" + name,
            root_turn_id="root:test", parent_work_id=parent, depth=2 if parent else 1)

    def document_for(self, kind, identifier):
        return next((item for item in self.documents.list()["items"]
                     if item["authorityKey"] == kind + ":" + identifier and item["state"] == "active"), None)

    def snapshot(self):
        return self.ledger.snapshot("graph:test", "controller:test")

    def manifest(self, work=None, **kwargs):
        snapshot = self.snapshot()
        task = snapshot.task((work or self.first)["id"])
        return self.materials.manifest(snapshot, task, **kwargs)

    def source(self, name="source.md", content="first chapter: limit 12\nsecond chapter: preserve files\n"):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return "workspace:" + str(path)

    def body(self, manifest):
        return "\n".join(item.content or "" for item in manifest.items)

    def test_real_scoped_reads_keep_two_tasks_on_different_chapters(self):
        source = self.source()
        one = self.manifest(context_refs=[{"sourceRef": source, "selector": "1", "required": True}])
        two = self.manifest(self.second, context_refs=[{"sourceRef": source, "selector": "2", "required": True}])
        self.assertIn("first chapter", self.body(one))
        self.assertNotIn("second chapter", self.body(one))
        self.assertIn("second chapter", self.body(two))
        self.assertNotIn("first chapter", self.body(two))
        self.assertIn("Task root", self.body(one))
        self.assertIn("Task first", self.body(one))
        payload = one.for_executor(attempt_id="dispatch:one")
        self.assertFalse(payload["materials"][-1]["needsOriginalRead"])
        self.assertEqual(payload["readReceipts"][0]["attemptId"], "dispatch:one")
        self.assertEqual(payload["readReceipts"][0]["displayedRanges"], [{"startLine": 1, "endLine": 1}])
        self.assertEqual(payload["executionScope"]["writeRoots"], [])
        self.assertEqual(payload["executionScope"]["tools"], ["workspace_read"])

    def test_execution_scope_distinguishes_callable_names_from_host_capabilities(self):
        self.service._runtime_tool_manifest = lambda session: [
            {"name": "workspace_read", "modelVisible": False,
             "runtimeProjections": [{"name": "read", "operation": "read"}]},
            {"name": "workspace_shell", "modelVisible": False,
             "runtimeProjections": [{"name": "bash", "operation": "run"}]},
            {"name": "hidden", "modelVisible": False},
            {"name": "unavailable", "available": False},
            {"name": "room_partner"},
        ]
        scope = self.manifest().for_executor()["executionScope"]
        self.assertEqual(scope["tools"], ["bash", "read", "room_partner"])
        self.assertEqual(scope["toolBindings"], [
            {"capabilityId": "workspace_read", "name": "read", "operation": "read"},
            {"capabilityId": "workspace_shell", "name": "bash", "operation": "run"},
        ])
        self.assertNotIn("unavailable", scope["capabilityIds"])

    def test_registered_work_document_reads_body_and_honors_explicit_section(self):
        path = self.root / "docs" / "draft.md"
        path.parent.mkdir()
        path.write_text("chosen section\nprivate unrelated section\n", encoding="utf-8")
        authority = self.documents.authority_context("room_work_item", self.first["id"])
        document = self.documents.register({"authorityKind": "room_work_item",
            "authorityId": self.first["id"], "authorityRevision": authority["authorityRevision"],
            "workspaceRoot": str(self.root), "sourcePath": "docs/draft.md", "title": "Task document"})["document"]
        source = "workdoc:" + document["documentId"]
        result = self.manifest(context_refs=[{"sourceRef": source, "selector": "1"}])
        self.assertIn("chosen section", self.body(result))
        self.assertNotIn("private unrelated", self.body(result))
        self.assertEqual(len(result.for_executor()["readReceipts"]), 1)
        self.assertEqual(result.for_executor()["readReceipts"][0]["reader"], "WorkDocumentService/WorkspaceHarness")

    def test_current_summary_is_used_and_source_edit_rebuilds_exact_original(self):
        source = self.source(content="Exact source revision one\n")
        original = self.manifest(context_refs=[source])
        summary = {"sourceRef": source, "sourceRevision": original.items[-1].revision,
                   "selector": "", "content": "Existing Pi summary"}
        reference = {"sourceRef": source, "selection": "summary"}
        summarized = self.manifest(context_refs=[reference], summaries=[summary])
        self.assertEqual(summarized.items[-1].content, "Existing Pi summary")
        Path(source[len("workspace:"):]).write_text("Exact source revision two\n", encoding="utf-8")
        current = self.manifest(context_refs=[reference], summaries=[summary])
        self.assertEqual(current.items[-1].content, "Exact source revision two\n")
        self.assertEqual(current.items[-1].mode, "read_exact")
        self.assertFalse(current.missing)
        rebuilt = self.manifest(context_refs=[reference], summaries=[summary],
            selections={item.id: item.selection for item in current.items})
        self.assertEqual(current.for_executor(), rebuilt.for_executor())

    def test_reading_decision_respects_global_and_each_material_external_policy(self):
        sent = []
        def choose(state, questions):
            sent.append(state)
            return {"model": "test", "answers": {"reading_depth": {
                "type": "choice", "choice": "read_exact", "confidence": 1.0,
                "probabilities": {key: 1.0 if key == "read_exact" else 0.0
                                  for key in questions["reading_depth"]["criteria"]}}}}
        self.materials.decider = JevChoices(choose)
        allowed = self.source("allowed.md", "Allowed material\n")
        forbidden = self.source("forbidden.md", "Private material must stay local\n")
        refs = [{"sourceRef": allowed, "externalAllowed": True}, {"sourceRef": forbidden}]
        local = self.manifest(context_refs=refs)
        self.assertFalse(sent)
        self.assertIn("Private material", self.body(local))
        remote = self.manifest(context_refs=refs, decision_external_allowed=True)
        self.assertEqual(len(sent), 1)
        self.assertIn("Allowed material", sent[0])
        self.assertNotIn("Private material", sent[0])
        self.manifest(context_refs=refs, decision_external_allowed=True,
                      selections={item.id: item.selection for item in remote.items})
        self.assertEqual(len(sent), 1)

    def test_revoked_workspace_body_and_ref_disappear_from_rebuilt_context(self):
        source = self.source("confidential.md", "Secret original\n")
        old = self.manifest(context_refs=[source])
        self.assertIn("Secret original", self.body(old))
        elsewhere = self.root / "other"
        elsewhere.mkdir()
        self.sessions.set_runtime_policy(self.session["id"], mode="coordinator",
            tool_profile_version="control-center-v1", execution_mode="read_only",
            allowed_tools=None, workspace_roots=[str(elsewhere)])
        current = self.manifest(context_refs=[source])
        self.assertNotIn("Secret original", canonical(current.for_executor()))
        self.assertNotIn("confidential.md", canonical(current.for_executor()))
        self.assertNotEqual(old.digest, current.digest)
        self.assertFalse(current.missing)
        self.assertTrue(current.notices)

    def test_optional_unknown_ref_is_notice_required_ref_is_missing(self):
        optional = self.manifest(context_refs=["unavailable:opaque-ref"])
        required = self.manifest(context_refs=[{"sourceRef": "unavailable:opaque-ref", "required": True}])
        self.assertFalse(optional.missing)
        self.assertTrue(optional.notices)
        self.assertTrue(required.missing)
        self.assertNotIn("opaque-ref", canonical(required.for_executor()))

    def test_knowledge_read_uses_existing_receipt_owner_and_denial_does_not_leak(self):
        reader = Mock(return_value={"text": "Current scoped knowledge", "claimHash": "current-hash"})
        self.service.room_knowledge_read = reader
        ref = {"sourceRef": "knowledge:claim1", "required": True,
               "retrievalReceiptId": "receipt1", "expectedHash": "current-hash"}
        result = self.manifest(context_refs=[ref])
        self.assertIn("Current scoped knowledge", self.body(result))
        reader.assert_called_once_with({"claimRef": "claim1", "retrievalReceiptId": "receipt1",
            "expectedHash": "current-hash"}, authenticated_session_id=self.session["id"])
        reader.side_effect = PermissionError("private revoked scope")
        denied = self.manifest(context_refs=[ref])
        self.assertTrue(denied.missing)
        self.assertNotIn("private revoked scope", canonical(denied.for_executor()))

    def test_unresolved_review_feedback_rebuilds_with_current_task_revision(self):
        self.work.submit(self.session["id"], {"workId": self.first["id"], "resultSummary": "Attempt one",
            "artifactRefs": ["result:1"], "evidenceRefs": ["proof:1"]})
        self.work.return_for_revision(self.session["id"], {"workId": self.first["id"], "expectedRevision": 0,
            "reason": "Preserve the limit of 12; prior output used 7", "evidenceRefs": ["review:1"],
            "operabilityVerdict": "passed", "requirementVerdict": "not_satisfied"})
        rebuilt = self.manifest()
        self.assertEqual(rebuilt.task_revision, 1)
        self.assertIn("prior output used 7", self.body(rebuilt))
        self.assertIn("Keep exact limit 12", self.body(rebuilt))

    def test_material_service_revalidates_receipt_reduction_with_full_feedback(self):
        self.work.submit(self.session["id"], {"workId": self.first["id"], "resultSummary": "Attempt one",
            "artifactRefs": ["result:1"], "evidenceRefs": ["proof:1"]})
        reason = "保持限制十二，勿删除已有成果。" * 110
        self.work.return_for_revision(self.session["id"], {"workId": self.first["id"], "expectedRevision": 0,
            "reason": reason, "evidenceRefs": ["review:1"],
            "operabilityVerdict": "passed", "requirementVerdict": "not_satisfied"})
        refs = [self.source(f"context-{index}.md", "Optional original\n" * 200) for index in range(12)]
        result = self.manifest(context_refs=refs)
        required = self.materials._requirements(self.snapshot(), self.snapshot().task(self.first["id"]))
        for material in required:
            self.assertEqual(next(item.content for item in result.items if item.id == material.id), material.original)
        prepared = result.for_executor(attempt_id="jev-dispatch:" + "f" * 40)
        self.assertLess(len(prepared["readReceipts"]), len(refs))
        self.assertFalse(result.missing)
        self.assertLessEqual(len(canonical(prepared).encode()), 24000)
        fresh = self.manifest(context_refs=refs,
            selections={item["id"]: item["selection"] for item in prepared["materials"]})
        self.assertEqual(fresh.for_executor(attempt_id="jev-dispatch:" + "f" * 40), prepared)

    def test_dependency_context_uses_only_current_accepted_artifact_version(self):
        self.ledger.change_edges(self.snapshot(), command_id="edge:1",
            add=[Edge(self.second["id"], self.first["id"])], remove=[])
        self.work.submit(self.session["id"], {"workId": self.second["id"], "resultSummary": "Accepted artifact v1",
            "artifactRefs": ["artifact:revision-1"], "evidenceRefs": ["proof:1"]})
        self.assertNotIn("Accepted artifact v1", self.body(self.manifest()))
        self.work.return_for_revision(self.session["id"], {"workId": self.second["id"], "expectedRevision": 0,
            "reason": "Artifact v1 missed the exact limit", "evidenceRefs": ["review:1"],
            "operabilityVerdict": "passed", "requirementVerdict": "not_satisfied"})
        self.work.submit(self.session["id"], {"workId": self.second["id"], "resultSummary": "Accepted artifact v2",
            "artifactRefs": ["artifact:revision-2"], "evidenceRefs": ["proof:2"]})
        self.work.accept(self.session["id"], {"workId": self.second["id"], "expectedRevision": 1,
            "reason": "Both axes checked", "evidenceRefs": ["review:1"],
            "operabilityVerdict": "passed", "requirementVerdict": "satisfied"})
        effect = {"effectId": "accepted-dependency"}
        self.service.jev_application = SimpleNamespace(lifecycle=SimpleNamespace(
            effect_for_dispatch=Mock(return_value=effect)))
        self.materials.worker_tool_evidence = Mock(return_value={
            "status": "available", "readRef": "media://dependency-tools", "tools": []})
        accepted = self.manifest()
        evidence_call = self.materials.worker_tool_evidence.call_args
        self.assertEqual(evidence_call.args[1].id, self.second["id"])
        self.assertEqual(evidence_call.args[1].revision, 1)
        self.assertIs(evidence_call.args[2], effect)
        self.assertEqual(evidence_call.kwargs["inline_byte_budget"], 2000)
        self.assertIn('"state":"done"', self.body(accepted))
        self.assertIn('"stateSource":"current canonical WorkItem"', self.body(accepted))
        accepted_at = self.work.get(self.second["id"])["completedAtMs"]
        self.assertGreater(accepted_at, 0)
        self.assertEqual(evidence_call.args[1].completed_at_ms, accepted_at)
        self.assertIn(f'"acceptedAtMs":{accepted_at}', self.body(accepted))
        self.assertIn("media://dependency-tools", self.body(accepted))
        self.assertIn("Accepted artifact v2", self.body(accepted))
        self.assertIn("artifact:revision-2", self.body(accepted))
        self.assertNotIn("artifact:revision-1", self.body(accepted))
        self.assertEqual(next(item.revision for item in accepted.items
                              if item.id == "dependency:" + self.second["id"]), "1")

    def test_three_accepted_handoffs_fit_and_keep_complete_room_owned_results_readable(self):
        self.service.media = AgentMediaStore(self.db, root=self.root / 'media')
        dependencies = [self.second, self.create_work('third', self.root_work['id']),
                        self.create_work('fourth', self.root_work['id'])]
        self.ledger.change_edges(self.snapshot(), command_id='three-handoffs',
            add=[Edge(work['id'], self.first['id']) for work in dependencies], remove=[])
        for work in dependencies:
            self.work.submit(self.session['id'], {'workId': work['id'],
                'resultSummary': 'Complete handoff ' * 220,
                'artifactRefs': ['docs/' + work['id'] + '.md'],
                'evidenceRefs': ['Exact evidence ' * 50 + str(index) for index in range(12)]})
            self.work.accept(self.session['id'], {'workId': work['id'], 'expectedRevision': 0,
                'reason': 'Checked handoff', 'evidenceRefs': ['verification:' + work['id']],
                'operabilityVerdict': 'passed', 'requirementVerdict': 'satisfied'})
        first = self.manifest()
        self.assertFalse(first.missing)
        self.assertLessEqual(len(canonical(first.for_executor()).encode()), 24000)
        for item in first.items:
            if not item.id.startswith('dependency:'):
                continue
            inline = json.loads(item.content)
            self.assertEqual(inline['state'], 'done')
            self.assertTrue(inline['inlineTruncated'])
            self.assertLessEqual(len(item.content.encode()), 2200)
            media_id = inline['readRef'].removeprefix('media://')
            receipt, raw = self.service.media.read(media_id, room_id=self.room['id'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), inline['archiveSha256'])
            full = json.loads(raw)
            work = self.snapshot().task(full['taskId'])
            self.assertEqual(full['result'], work.result)
            self.assertEqual(full['evidence'], list(work.evidence))
            self.assertEqual(full['artifacts'], list(work.artifacts))
            self.assertEqual(receipt['roomId'], self.room['id'])
            with self.assertRaises(KeyError):
                self.service.media.read(media_id, room_id='another-room')
        self.assertEqual(self.manifest().for_executor(), first.for_executor())

    def test_archive_failure_retains_full_handoff_and_reports_the_budget_gap(self):
        self.service.media = SimpleNamespace(list_for_room=Mock(side_effect=OSError('archive unavailable')))
        self.ledger.change_edges(self.snapshot(), command_id='handoff-fallback',
            add=[Edge(self.second['id'], self.first['id'])], remove=[])
        result = 'Keep exact handoff ' * 200
        self.work.submit(self.session['id'], {'workId': self.second['id'], 'resultSummary': result,
            'evidenceRefs': ['actual verification']})
        self.work.accept(self.session['id'], {'workId': self.second['id'], 'expectedRevision': 0,
            'reason': 'Checked', 'evidenceRefs': ['verification'],
            'operabilityVerdict': 'passed', 'requirementVerdict': 'satisfied'})
        required = self.materials._requirements(self.snapshot(), self.snapshot().task(self.first['id']))
        full = next(item for item in required if item.id.startswith('dependency:'))
        self.assertEqual(json.loads(full.original)['result'], result.strip())
        manifest = self.manifest(byte_budget=3000)
        self.assertTrue(manifest.missing)


if __name__ == "__main__":
    unittest.main()
