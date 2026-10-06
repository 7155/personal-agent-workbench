"""Execute the shipped Swift presentation model, rather than matching its copy."""
from __future__ import annotations

import shutil
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AssistantGenerationBehaviorTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "the complete Squirrel model requires macOS CryptoKit")
    @unittest.skipUnless(shutil.which("swift"), "Swift is required for the native presentation model")
    def test_native_response_tracks_worker_settlement_after_visible_timeout(self) -> None:
        patch = (ROOT / "squirrel-patches/0001-add-rag-ime-sidecar.patch").read_text()
        block = patch.split("diff --git a/sources/RagImeSidecarModels.swift b/sources/RagImeSidecarModels.swift\n", 1)[1].split("diff --git ", 1)[0]
        models = "\n".join(line[1:] for line in block.splitlines() if line.startswith("+") and not line.startswith("+++"))
        response = dict(schemaVersion="rag-ime.active-rag.v1", sessionId="fixture", status="ready",
                        thinkingRow=False, uiMode="active_rag", selectedTextHash="fixture",
                        frontendRevision=1, selectionEpoch=1, panelSessionId="fixture", frontAppBundleId="test.editor",
                        placement="insert", intent="continue", evidenceCount=0, candidateCount=0, candidates=[],
                        diagnostics={"progress": {"workerPending": True}})
        cases = []
        for status, pending, expected in [("ready", True, True), ("ready", False, False),
                                          ("cancelled", True, False), ("error", True, False)]:
            value = {**response, "status": status, "diagnostics": {"progress": {"workerPending": pending}}}
            cases.append((json.dumps(value), expected))
        cases.append((json.dumps({**response, "status": "pending", "diagnostics": {}}), True))
        checks = []
        for index, (payload, expected) in enumerate(cases):
            checks.append(f'let decoded{index} = try JSONDecoder().decode(RagImeActiveRagResponse.self, from: Data(#"'
                          + payload + '"#.utf8))\n' + f'assert(decoded{index}.generationPending == ' + str(expected).lower() + ')')
        with tempfile.TemporaryDirectory(prefix="paw-worker-state-test-") as directory:
            path = Path(directory) / "main.swift"
            card = (ROOT / "squirrel-patches/sources/RagImeSuggestionCardView.swift").read_text()
            signature = card[card.index("extension RagImeDisplayCandidate {"):card.index("enum RagImeAssistantAction")]
            candidate = dict(label="1", candidateStableId="same-id", text="第一段", insertText="第一段",
                             sourceType="model", selectionAction="none", sourceIndex=0, comment="", evidencePreview="",
                             suggestionId="candidate", memoryId="", isSelectable=False, metadata={"streamingPartial": True})
            variants = [candidate, {**candidate, "text": "第一段继续生成", "insertText": "第一段继续生成"},
                        {**candidate, "metadata": {"streamingPartial": False}, "isSelectable": True, "selectionAction": "insert"},
                        {**candidate, "metadata": {"partialRecovered": True, "streamInterrupted": True}}]
            for index, value in enumerate(variants):
                checks.append(f'let candidate{index} = try JSONDecoder().decode(RagImeDisplayCandidate.self, from: Data(#"'
                              + json.dumps(value) + '"#.utf8))')
            for index in range(1, len(variants)):
                checks.append(f'assert(candidate0.assistantPresentationSignature != candidate{index}.assistantPresentationSignature)')
            path.write_text(models + "\n" + signature + "\n" + "\n".join(checks))
            result = subprocess.run([shutil.which("swift"), str(path)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("swift"), "Swift is required for the Foundation progress planner")
    def test_progress_never_claims_unobserved_context_or_retrieval(self) -> None:
        source = (ROOT / "squirrel-patches/sources/RagImeSuggestionCardView.swift").read_text()
        planner = source[source.index("enum RagImeGenerationStageState"):source.index("final class RagImeSuggestionCardView")]
        checks = r'''
func check(_ condition: Bool, _ message: String) {
  if !condition { print(message); exit(1) }
}
var input = RagImeGenerationStagePlanner.Input()
input.stage = "capturing_context"
let initial = RagImeGenerationStagePlanner.plan(input)
check(!initial.summary.contains("已准备"), "empty progress invented ready context")
input.foregroundChars = 42
let retrieving = RagImeGenerationStagePlanner.plan(input)
check(retrieving.title == "正在查找相关资料", "title does not name the current operation")
input.stage = "retrieval_complete"
input.retrievalAttempted = true
let empty = RagImeGenerationStagePlanner.plan(input)
check(!empty.title.contains("已找到"), "empty retrieval claimed a match")
check(empty.summary.contains("未找到额外资料"), "empty retrieval needs an honest next step")
input.recentCount = 3
input.recentUsed = false
let unused = RagImeGenerationStagePlanner.plan(input)
check(!unused.summary.contains("近期输入 3 条"), "unused history presented as used context")
input.foregroundChars = 0
input.stage = "generating"
input.diagnosticStatus = "context_missing"
let noContext = RagImeGenerationStagePlanner.plan(input)
check(noContext.rows[0].state != .active && noContext.rows[0].state != .pending,
      "model is running but the context row is still waiting")
input.stage = "cancelled"
let stopped = RagImeGenerationStagePlanner.plan(input)
check(!stopped.rows.contains { $0.state == .active }, "cancelled generation remains active")
check(stopped.rows[3].state == .failed, "interrupted model incorrectly attributed to missing optional context")
print("native generation behavior passed")
'''
        with tempfile.TemporaryDirectory(prefix="paw-generation-test-") as directory:
            path = Path(directory) / "main.swift"
            path.write_text("import Foundation\n" + planner + "\n" + checks)
            result = subprocess.run([shutil.which("swift"), str(path)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_bridge_does_not_invent_initial_or_missing_progress(self) -> None:
        patch = (ROOT / "squirrel-patches/0001-add-rag-ime-sidecar.patch").read_text()
        placeholder = patch[patch.index("+  func showRagImeActiveRagThinkingPlaceholder"):patch.index("+  func sendRagImeActiveRagStartRequest")]
        self.assertIn('"progressStage": .string("capturing_context")', placeholder)
        retrieval = patch[patch.index("+    let retrievalAttempted: Bool = {"):]
        retrieval = retrieval[:retrieval.index("+    }()")]
        self.assertIn("else { return false }", retrieval)
        self.assertIn(r"activeOverlayCandidates.map(\.assistantPresentationSignature)", patch)
