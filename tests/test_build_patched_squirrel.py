from __future__ import annotations

import json
import os
import shlex
import shutil
import struct
import zipfile
import zlib
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class BuildPatchedSquirrelScriptTests(unittest.TestCase):
    def test_local_adhoc_signing_uses_a_stable_designated_requirement(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "build_patched_squirrel.sh").read_text(encoding="utf-8")

        self.assertIn('designated => identifier \\"$BUNDLE_ID\\"', script)
        self.assertIn("Accessibility authorization is not tied to a changing cdhash", script)

    def test_squirrel_patch_contains_side_first_mixed_layout_hooks(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")

        self.assertIn("fallback: 5, range: 0...10", patch_text)
        self.assertIn("fallback: 900, range: 30...3000", patch_text)
        self.assertIn("fallback: 1200, range: 30...3000", patch_text)
        self.assertIn("sidecar_url missing; passive input path does not run CLI fallback", patch_text)
        self.assertIn("passive CLI fallback disabled", patch_text)
        self.assertIn("case circuitOpen(Int)", patch_text)
        self.assertNotIn("RAG IME sidecar HTTP request failed, falling back to CLI", patch_text)
        self.assertIn("let displayLayout: String?", patch_text)
        self.assertIn("let displayLane: String?", patch_text)
        self.assertIn("let badge: String?", patch_text)
        self.assertIn("let sourceBadge: String?", patch_text)
        self.assertIn("let colorToken: String?", patch_text)
        self.assertIn("let sourceStability: String?", patch_text)
        self.assertIn("let candidateOrdinal: Int?", patch_text)
        self.assertIn("let candidateStableId: String?", patch_text)
        self.assertIn("let snapshotId: String?", patch_text)
        self.assertIn("let hardContextAnchor: String?", patch_text)
        self.assertIn("let queryAnchor: String?", patch_text)
        self.assertIn("let displayAnchor: String?", patch_text)
        self.assertIn("let keyPolicy: RagImeKeyPolicyPayload?", patch_text)
        self.assertIn("let predictionTraceEvents: [RagImePredictionTraceEvent]?", patch_text)
        self.assertIn("struct RagImeKeyPolicyPayload: Codable", patch_text)
        self.assertIn("struct RagImePredictionTraceEvent: Codable", patch_text)
        self.assertIn("struct RagImeRefreshDecision: Codable", patch_text)
        self.assertIn("struct RagImeShowDecision: Codable", patch_text)
        self.assertIn("func candidateSeparator(before index: Int) -> String", patch_text)
        self.assertIn('currentLayout == "inline", previousLayout == "inline"', patch_text)
        self.assertIn("private var ragImePanelUsesDisplayCandidates: Bool = false", patch_text)
        self.assertIn("private var ragImeDisplaySessionFingerprint: String = \"\"", patch_text)
        self.assertIn("private var ragImeDisplaySnapshotId: String = \"\"", patch_text)
        self.assertIn("private var ragImeDisplayNumberKeyPolicy: String = \"\"", patch_text)
        self.assertIn("private var ragImeDisplayTabPolicy: String = \"\"", patch_text)
        self.assertIn("private var ragImeDisplayOptionNumberPolicy: String = \"\"", patch_text)
        self.assertIn("private var ragImeDisplayExpiresAt: Date?", patch_text)
        self.assertIn("func ragImePanelForcesHorizontalLayout() -> Bool", patch_text)
        self.assertIn("func ragImePanelUsesSideDisplay() -> Bool", patch_text)
        self.assertIn("func ragImeDisplaySourceType(at index: Int) -> String?", patch_text)
        self.assertIn("private var ragImeInputGeneration: Int = 0", patch_text)
        self.assertIn("private let ragImeActiveResponseApplyWindowMs: Int = 9000", patch_text)
        self.assertIn("private let ragImePostCommitResponseApplyWindowMs: Int = 12000", patch_text)
        self.assertIn("var ragImePanelLinear: Bool", patch_text)
        self.assertIn("view.textView.setLayoutOrientation(ragImePanelVertical ? .vertical : .horizontal)", patch_text)
        self.assertIn("var candidateSeparators = [String]()", patch_text)
        self.assertIn('traceRagImeFrontendEvent("panel_text_layout"', patch_text)
        self.assertIn("traceRagImePanelTextLayout(", patch_text)
        self.assertIn('case "model":', patch_text)
        self.assertIn('return "模"', patch_text)
        self.assertIn('return "查"', patch_text)
        self.assertIn('return "忆"', patch_text)
        self.assertIn('return "词"', patch_text)
        self.assertIn('return "input"', patch_text)
        self.assertIn('case "action":', patch_text)
        self.assertIn('return "生成"', patch_text)
        self.assertIn('case "model", "rag", "memory", "status", "action":', patch_text)
        self.assertIn("return .systemBlue", patch_text)
        self.assertIn("let maxTextHeight = ragImePanelVertical", patch_text)
        self.assertIn("let maxWidth = if ragImePanelVertical", patch_text)
        self.assertIn("var ragImePanelUsesSideDisplay: Bool", patch_text)
        self.assertIn("let readableCap: CGFloat = min(screenRect.width - 48, 560)", patch_text)
        self.assertIn("if theme.translucency && !ragImePanelUsesSideDisplay", patch_text)
        self.assertIn("alphaValue = ragImePanelUsesSideDisplay ? 1 : theme.alpha", patch_text)
        self.assertIn("return NSView()", patch_text)
        self.assertIn("RAG_IME_ASSISTANT_OVERLAY_AUTO_PENDING", patch_text)
        self.assertIn("func refreshRagImeInputClient(_ sender: Any!, reason: String) -> String", patch_text)
        self.assertIn('identitySource = "frontmost_application"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("input_client_identity_resolved"', patch_text)
        self.assertIn('refreshRagImeInputClient(sender, reason: "activate_server")', patch_text)
        self.assertIn('refreshRagImeInputClient(sender, reason: "key_event")', patch_text)
        handle_start = patch_text.index("override func handle(_ event: NSEvent!, client sender: Any!) -> Bool")
        handle_end = patch_text.index("func selectCandidate(_ index: Int) -> Bool", handle_start)
        handle_text = patch_text[handle_start:handle_end]
        self.assertNotIn("enforceRagImeSensitiveFieldGuard()", handle_text)
        self.assertIn("ragImeNativeCompositionActive()", handle_text)
        self.assertIn('"assistant_overlay_tab_passed_through"', handle_text)
        self.assertIn("let nativeCompositionBeforeKey = ragImeNativeCompositionActive()", handle_text)
        self.assertIn("let modifiedReturn = !modifiers.intersection", handle_text)
        self.assertIn("finalizeRagImeActiveInputBuffer(", handle_text)
        self.assertIn('reason: "return_key"', handle_text)
        self.assertIn('kind: "host_return"', handle_text)
        self.assertIn('confidence: "strong"', handle_text)
        self.assertIn("hostForwarded: true", handle_text)
        self.assertIn("finalCommitted: true", handle_text)
        self.assertIn("&& !nativeCompositionBeforeKey", handle_text)
        self.assertIn("&& !handled", handle_text)
        self.assertIn("private var ragImeActiveInputBuffer: String = \"\"", patch_text)
        self.assertIn("rememberRagImeActiveInputFragment(string)", patch_text)
        self.assertIn("removeLastRagImeActiveInputCharacter()", patch_text)
        self.assertIn('source: "squirrel_input_segment"', patch_text)
        self.assertIn('"input-segment",', patch_text)
        self.assertIn('"finalized",', patch_text)
        self.assertIn('captureTags.append(contentsOf: ["finalized", "complete-input"])', patch_text)
        self.assertIn('"input_segment_capture_failed"', patch_text)
        self.assertIn("RagImeInputCaptureOutbox.shared.contains(", patch_text)
        self.assertIn('"capture:\\(captureMetadata.captureSource)"', patch_text)
        self.assertIn('"ime_input_transaction_empty"', patch_text)
        self.assertIn('"field_context_not_bound_to_ime_transaction"', patch_text)
        self.assertIn('NSWorkspace.shared.frontmostApplication?.bundleIdentifier == sourceAppBundleId', patch_text)
        self.assertIn('"front_app_changed_during_capture"', patch_text)
        self.assertIn('"com.mitchellh.ghostty",', patch_text)
        self.assertIn("if terminalBundleIds.contains(sourceAppBundleId) { return false }", patch_text)
        self.assertIn('contextSource: activeInput.isEmpty', patch_text)
        self.assertIn('"active_input_buffer"', patch_text)
        refresh_start = patch_text.index("func refreshRagImeSidecar(")
        refresh_end = patch_text.index("func scheduleRagImeCommitBurstPrediction(", refresh_start)
        refresh_text = patch_text[refresh_start:refresh_end]
        composition_branch = refresh_text.index("if !rawInput.isEmpty || !preedit.isEmpty")
        post_commit_gate = refresh_text.index("guard commitBurstReady || acceptedCandidateContinuation")
        privacy_probe = refresh_text.index("let fastPrivacyStatus = ragImeSelectedTextProvider.fastSensitiveFieldStatus(")
        self.assertLess(composition_branch, post_commit_gate)
        self.assertLess(post_commit_gate, privacy_probe)
        schedule_start = patch_text.index("func scheduleRagImeCommitBurstPrediction(")
        schedule_end = patch_text.index("func cancelRagImeCommitBurstAfterDelete()", schedule_start)
        schedule_text = patch_text[schedule_start:schedule_end]
        self.assertNotIn("enforceRagImeSensitiveFieldGuard()", schedule_text)
        self.assertNotIn("sensitiveFieldStatus(", schedule_text)
        self.assertIn("showRagImeImmediatePostCommitPending(committedText: committedText, generation: generation)", schedule_text)
        self.assertIn('traceEvent: "assistant_overlay_post_commit_immediate"', patch_text)
        self.assertIn("func dismissRagImePostCommitAssistForSubmission(keyCode: UInt16)", patch_text)
        self.assertIn("[36, 76].contains(keyCode) && !nativeCompositionBeforeKey", handle_text)
        self.assertIn('dismissRagImeAssistantOverlay(reason: "user_submit")', patch_text)
        self.assertIn('traceRagImeFrontendEvent("assistant_overlay_submit_dismissed"', patch_text)
        self.assertNotIn("\n+    self.client ?= sender as? IMKTextInput", patch_text)
        self.assertIn("private var ragImePanelUpdateGeneration: UInt = 0", patch_text)
        self.assertIn("ragImePanelUpdateGeneration &+= 1", patch_text)
        self.assertIn("DispatchQueue.main.async { [weak self] in", patch_text)
        self.assertIn("generation == ragImePanelUpdateGeneration", patch_text)
        self.assertIn("private var ragImeDeferredDeactivateWorkItem: DispatchWorkItem?", patch_text)
        self.assertIn('traceRagImeFrontendEvent("deactivate_server_deferred"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("deactivate_server_recovered"', patch_text)
        self.assertIn("sameAppClientRotation", patch_text)
        deactivate_start = patch_text.index("override func deactivateServer(")
        deactivate_end = patch_text.index("override func hidePalettes()", deactivate_start)
        deactivate_text = patch_text[deactivate_start:deactivate_end]
        self.assertIn("DispatchQueue.main.asyncAfter", deactivate_text)
        self.assertIn(".milliseconds(450)", deactivate_text)
        self.assertLess(deactivate_text.index("let workItem = DispatchWorkItem"), deactivate_text.index("self.hidePalettes()"))
        self.assertIn('currentApp == "com.google.Chrome"', patch_text)
        self.assertIn('currentApp == "com.microsoft.edgemac"', patch_text)
        self.assertIn('currentApp == "com.openai.codex"', patch_text)
        self.assertIn('currentApp == "com.microsoft.VSCode"', patch_text)
        self.assertIn("ragImeReuseChromiumPanelPosition", patch_text)
        self.assertIn("ragImeChromiumAssistantAnchor", patch_text)
        self.assertIn("ragImeChromiumAssistantAnchorCapturedAt", patch_text)
        self.assertIn("Date().timeIntervalSince(ragImeChromiumAssistantAnchorCapturedAt) <= 2.0", patch_text)
        empty_panel_cleanup = patch_text[
            patch_text.index("if preedit.isEmpty && candidates.isEmpty {") :
        ]
        self.assertNotIn("ragImeChromiumAssistantAnchor = nil", empty_panel_cleanup[:600])
        self.assertIn('ragImeBoolEnvEnabled("RAG_IME_ASSISTANT_OVERLAY_AUTO_PENDING", default: true)', patch_text)
        self.assertIn("let maximumTTL = containsResult ? 30000 : (isPendingFeedback ? 12000 : 2000)", patch_text)
        self.assertIn("ragImeOverlayExpiresAt = isExplicitOverlay", patch_text)
        self.assertIn("let activeOverlayTraceAnchor", patch_text)
        self.assertIn("let cancelledSelectedTextHash", patch_text)
        self.assertIn("post_commit_pending_overlay_disabled", patch_text)
        self.assertIn("assistant_overlay_local_placeholder_suppressed", patch_text)
        self.assertIn("打开澄控制中心...", patch_text)
        self.assertIn("配置由控制中心管理", patch_text)
        self.assertIn("重新启动后台服务", patch_text)
        self.assertIn("诊断与修复...", patch_text)
        self.assertNotIn("打开 RAG-IME 控制中心...", patch_text)
        self.assertNotIn("当前配置：由 Sidecar 同步", patch_text)
        self.assertNotIn("重新启动 RAG-IME Sidecar", patch_text)
        self.assertNotIn("RAG-IME 诊断...", patch_text)
        self.assertNotIn('urlForApplication(withBundleIdentifier: "com.rag-ime.control")', patch_text)
        self.assertIn('Applications/RagImeControl.app', patch_text)
        self.assertIn('rag-ime-control-web-build-marker.json', patch_text)
        self.assertIn('marker?["ui"] as? String == "control-center-web"', patch_text)
        self.assertIn('performRagImeControlAction("stop_ai")', patch_text)
        self.assertIn('performRagImeControlAction("restart_sidecar")', patch_text)
        overlay_sources = root / "squirrel-patches" / "sources"
        controller_text = (overlay_sources / "RagImeAssistantPanelController.swift").read_text(encoding="utf-8")
        card_text = (overlay_sources / "RagImeSuggestionCardView.swift").read_text(encoding="utf-8")
        row_text = (overlay_sources / "RagImeSuggestionRowView.swift").read_text(encoding="utf-8")
        state_text = (overlay_sources / "RagImeAssistantSurfaceState.swift").read_text(encoding="utf-8")
        panel_text = (overlay_sources / "RagImeNonActivatingPanel.swift").read_text(encoding="utf-8")
        outbox_text = (overlay_sources / "RagImeInputCaptureOutbox.swift").read_text(encoding="utf-8")
        prepare_script = (root / "scripts" / "prepare_squirrel_workspace.sh").read_text(encoding="utf-8")
        build_script = (root / "scripts" / "build_patched_squirrel.sh").read_text(encoding="utf-8")
        shared_motion_text = (root / "macos" / "Shared" / "RagImeMotion.swift").read_text(encoding="utf-8")
        combined_overlay_text = "\n".join([controller_text, card_text, row_text, state_text, panel_text])
        self.assertIn("RagImeInputCaptureOutbox.swift", prepare_script)
        self.assertIn("RagImeInputCaptureOutbox.swift", build_script)
        self.assertIn("maximumEntries = 64", outbox_text)
        self.assertIn("maximumBytes = 2 * 1024 * 1024", outbox_text)
        self.assertIn("guard entries.count <= maximumEntries", outbox_text)
        self.assertNotIn("entries.suffix(maximumEntries)", outbox_text)
        self.assertNotIn("nowMs - $0.queuedAtMs", outbox_text)
        self.assertIn("func contains(captureId: String) -> Bool", outbox_text)
        self.assertIn("func retryPending(using client: RagImeSidecarClient) throws -> Int", outbox_text)
        self.assertIn("observingCaptureId: nil", outbox_text)
        self.assertIn('"input_capture_outbox_replayed"', patch_text)
        self.assertIn('"input_capture_outbox_retry_deferred"', patch_text)
        self.assertIn("let captureDeliveryEvents: Set<String>", patch_text)
        self.assertIn("ragImeSidecarClient?.frontendTrace == true || isCaptureDeliveryEvent", patch_text)
        self.assertIn("let includeText = !isCaptureDeliveryEvent", patch_text)
        self.assertLess(outbox_text.index("try persist(entries)"), outbox_text.index("client.sidecarURL"))
        self.assertIn(
            "if currentState.isExplicit && currentState != .explicitGenerating {",
            controller_text,
        )
        self.assertNotIn("currentState != .explicitError", controller_text)
        self.assertIn("assistant_explicit_surface_pinned", controller_text)
        self.assertIn('NSMenuItem(title: "查看输入与依据"', controller_text)
        self.assertIn("showContextInspector", controller_text)
        self.assertIn("RagImeAssistantContextInspectorViewController", controller_text)
        self.assertIn("final class RagImeAssistantPanelController: NSObject", controller_text)
        self.assertIn("contextInspectorWindowController", controller_text)
        self.assertIn("menu.autoenablesItems = false", controller_text)
        self.assertIn("context.target = self", controller_text)
        self.assertIn("context.isEnabled = contextMenuDocument != nil", controller_text)
        self.assertIn('NSMenuItem(title: "内容状态：\\(unavailableReason)"', controller_text)
        self.assertIn("context.keyEquivalentModifierMask = [.command, .option]", controller_text)
        self.assertIn('context.setAccessibilityLabel("查看本轮输入与依据")', controller_text)
        self.assertIn("guard let document else", controller_text)
        self.assertNotIn("guard let document, document.isAvailable else", controller_text)
        self.assertIn('"contentState": document.state.rawValue', controller_text)
        self.assertIn("func authorizedContextView()", patch_text)
        self.assertIn("response.authorizedContextView()", patch_text)
        self.assertIn('schemaVersion == "rag-ime.active-rag-context-view.v1"', patch_text)
        self.assertIn('source == "frontend_request" || source == "provider_request"', patch_text)
        self.assertIn(
            "styleMask: [.borderless, .nonactivatingPanel]",
            controller_text,
        )
        self.assertIn("inspectorPanel.orderFront(nil)", controller_text)
        self.assertNotIn("inspectorPanel.makeKeyAndOrderFront(nil)", controller_text)
        self.assertNotIn("inspectorPanel.makeFirstResponder(viewController)", controller_text)
        self.assertNotIn("let popover = NSPopover()", controller_text)
        self.assertIn('accessibilityDescription: "关闭输入与依据"', controller_text)
        self.assertIn("closeContextInspector", controller_text)
        self.assertIn("captureResultViewport()", controller_text)
        self.assertIn("restoreResultViewport", controller_text)
        self.assertIn("let panelFrame: NSRect", controller_text)
        self.assertIn("case loading", controller_text)
        self.assertIn("case empty", controller_text)
        self.assertIn("case error", controller_text)
        self.assertIn("copyButton.isEnabled = document.isAvailable", controller_text)
        self.assertIn("RagImeAssistantMetrics.minimumHitTarget", combined_overlay_text)
        self.assertIn("button.focusRingType = .exterior", card_text)
        self.assertIn("func captureResultViewport()", card_text)
        self.assertIn("func restoreResultViewport", card_text)
        dismiss_start = controller_text.index("func dismiss(reason: String)")
        dismiss_end = controller_text.index("private func apply(", dismiss_start)
        dismiss = controller_text[dismiss_start:dismiss_end]
        self.assertNotIn("contextInspectorWindowController", dismiss)
        self.assertNotIn("closeContextInspector", dismiss)
        self.assertNotIn("copyEvidence", controller_text)
        self.assertIn("let explicitGeneration = currentState == .explicitGenerating", controller_text)
        self.assertIn('? 30_000\n      : (explicitGeneration ? 30_000', controller_text)
        self.assertIn('"active_rag_budget"', controller_text)
        self.assertIn('"no_result_guard"', controller_text)
        self.assertNotIn("panel.appearance = NSAppearance(named: .aqua)", combined_overlay_text)
        self.assertNotIn("calibratedWhite", combined_overlay_text)
        self.assertNotIn("NSStackView", combined_overlay_text)
        self.assertNotIn("fittingSize", combined_overlay_text)
        self.assertNotIn("arrangedSubviews", combined_overlay_text)
        self.assertIn("case compactPrediction", state_text)
        self.assertIn("case pendingPrediction", state_text)
        self.assertIn("case explicitGenerating", state_text)
        self.assertIn("case explicitError", state_text)
        self.assertIn("enum RagImeAssistantMotion", state_text)
        self.assertIn("macos/Shared/RagImeMotion.swift", state_text)
        for declaration in (
            "static let micro: TimeInterval = 0.10",
            "static let entrance: TimeInterval = 0.14",
            "static let transition: TimeInterval = 0.18",
            "static let ambientPulse: TimeInterval = 0.72",
        ):
            self.assertIn(declaration, shared_motion_text)
            self.assertIn(declaration, state_text)
        self.assertIn("RagImeAssistantMotion.Duration.transition", controller_text)
        self.assertIn("RagImeAssistantMotion.Duration.entrance", row_text)
        self.assertIn("RagImeAssistantMotion.Duration.ambientPulse", card_text)
        self.assertNotIn("let breathe = CABasicAnimation", card_text)
        self.assertNotIn('statusHalo.layer?.add(breathe', card_text)
        self.assertIn("override var canBecomeKey: Bool { false }", panel_text)
        self.assertIn("override var canBecomeMain: Bool { false }", panel_text)
        self.assertIn("panel.ignoresMouseEvents = false", controller_text)
        self.assertNotIn("dismiss(reason: \"passive_anchor_missing\")", controller_text)
        self.assertIn('resolved = (lastPositionedAnchor, "last_panel_anchor")', controller_text)
        self.assertIn('resolved = (mouse, "passive_mouse_fallback")', controller_text)
        self.assertIn('"passive_screen_center"', controller_text)
        self.assertIn("same_snapshot_stable_ids", controller_text)
        self.assertIn("assistant_panel_created", controller_text)
        self.assertIn("assistant_panel_update_coalesced", controller_text)
        self.assertIn("assistant_passive_update_suppressed_while_explicit", controller_text)
        self.assertIn("assistant_explicit_dismiss_suppressed", controller_text)
        self.assertIn("shouldKeepExplicitSurface", controller_text)
        self.assertNotIn("assistant_result_collapsed_to_recall", controller_text)
        self.assertNotIn("RagImeAssistantResultRecallView", controller_text)
        self.assertIn("onRestore: { [weak self] in", patch_text)
        self.assertIn("RagImeSuggestionCardView.minimumPredictionWidth", controller_text)
        self.assertIn("RagImeSuggestionCardView.maximumPredictionWidth", controller_text)
        self.assertIn(
            "width: min(configuredMaximumWidth, RagImeSuggestionCardView.pendingPredictionWidth)",
            controller_text,
        )
        self.assertIn("assistant_panel_frame_transition_started", controller_text)
        self.assertIn("panel.animator().setFrame(positionedFrame", controller_text)
        self.assertIn("cardView.layoutSubtreeIfNeeded()", controller_text)
        self.assertIn("let isPassivePrediction = state == .pendingPrediction", controller_text)
        self.assertIn("&& !isPassivePrediction", controller_text)
        self.assertIn("func replacingCandidates(_ candidates: [RagImeDisplayCandidate])", patch_text)
        self.assertIn("func ragImeAssistantOverlayPreservingSessionActions(", patch_text)
        self.assertIn("assistant_overlay_session_actions_preserved", patch_text)
        self.assertIn('metadata["frontendPreservedAction"] = .bool(true)', patch_text)
        self.assertIn("snapshotId: payload.snapshotId", patch_text)
        self.assertIn("static let minimumPredictionWidth: CGFloat = 196", card_text)
        self.assertIn("static let pendingPredictionWidth: CGFloat = 248", card_text)
        self.assertIn("static let actionPredictionWidth: CGFloat = 264", card_text)
        self.assertIn("static let maximumPredictionWidth: CGFloat = 460", card_text)
        self.assertIn("func compactPredictionWidth(", controller_text)
        self.assertIn("(text as NSString).size(withAttributes: attributes).width", controller_text)
        self.assertIn("static let rowHeight: CGFloat = 40", card_text)
        self.assertIn("static let actionHeight: CGFloat = 40", card_text)
        self.assertIn("static let maximumPredictionCandidates = 4", card_text)
        self.assertIn("let visibleCount = min(Self.maximumPredictionCandidates, candidates.count)", card_text)
        self.assertIn('let shortcut = index == 0 ? "Tab" : "⌥\\(index + 1)"', card_text)
        self.assertIn('NSButton(title: "生成"', card_text)
        self.assertNotIn('NSButton(title: "看图"', card_text)
        self.assertIn('NSButton(title: "深度"', card_text)
        self.assertIn("predictionHeight(candidateCount: realCandidates.count, hasAction: hasAction)", controller_text)
        self.assertIn("min(maximumPredictionCandidates, candidateCount)", card_text)
        self.assertIn("same_snapshot_stable_ids", controller_text)
        self.assertNotIn("state == .compactPrediction ? min(1, candidates.count)", card_text)
        self.assertNotIn("min(520, max(280, measuredWidth))", controller_text)
        self.assertIn("case .insert:", controller_text)
        self.assertIn("case .replace:", controller_text)
        self.assertIn("canReplaceSelection(in: payload)", controller_text)
        self.assertIn('#selector(rememberResult)', controller_text)
        self.assertIn('#selector(suppressResult)', controller_text)
        self.assertNotIn('for title in ["记住", "不再推荐"]', controller_text)
        self.assertIn("replaceButton.isHidden = !canReplaceSelection", card_text)
        self.assertIn("DispatchQueue.main.async", controller_text)
        self.assertIn("final class RagImeSuggestionCardView: NSVisualEffectView", card_text)
        self.assertIn("material = .contentBackground", card_text)
        self.assertIn("blendingMode = .withinWindow", card_text)
        self.assertIn("state = .inactive", card_text)
        self.assertNotIn("blendingMode = .behindWindow", combined_overlay_text)
        self.assertIn("layer?.backgroundColor = NSColor.clear.cgColor", card_text)
        self.assertIn("NSColor.separatorColor", card_text)
        self.assertIn("accessibilityDisplayShouldReduceMotion", controller_text)
        self.assertIn("accessibilityDisplayShouldIncreaseContrast", card_text)
        self.assertIn("Timer(timeInterval: 1, repeats: true)", controller_text)
        self.assertIn("RunLoop.main.add(timer, forMode: .common)", controller_text)
        self.assertIn("assistant_generating_animation_started", controller_text)
        self.assertIn("assistant_generating_animation_stopped", controller_text)
        self.assertIn("func updateGeneratingFrame", card_text)
        self.assertIn('companionImage(named: "RagImeCompanionThinking")', card_text)
        self.assertIn('companionImage(named: "RagImeCompanionWarning")', card_text)
        self.assertNotIn("statusHalo.layer?.cornerRadius", card_text)
        self.assertIn("RagImeSuggestionRowView", row_text)
        self.assertIn('return "生成"', row_text)
        self.assertIn('case "rag": return .systemTeal', row_text)
        self.assertIn('case "memory": return .systemOrange', row_text)
        self.assertIn("sourceIcon.isHidden = true", row_text)
        self.assertNotIn("sourceIcon.image = sourceImage", row_text)
        self.assertIn("private let sourceBar = NSView()", row_text)
        self.assertIn("private let shortcutPlate = NSView()", row_text)
        self.assertIn("enum RagImeAssistantTypography", card_text)
        self.assertIn("sourceLabel.font = RagImeAssistantTypography.source", row_text)
        self.assertIn("sourceLabel.isHidden = true", row_text)
        self.assertIn("candidateLabel.font = RagImeAssistantTypography.candidate", row_text)
        self.assertIn("resultText.textStorage?.setAttributedString", card_text)
        self.assertIn("RagImeAssistantTypography.resultAttributes()", card_text)
        self.assertIn("enum RagImeAssistantMarkdownRenderer", card_text)
        self.assertIn("AttributedString(", card_text)
        self.assertIn("interpretedSyntax: .full", card_text)
        self.assertIn("RagImeAssistantMarkdownRenderer.render(text)", card_text)
        self.assertIn("visibleResultMarkdown", card_text)
        self.assertIn("RagImeSuggestionCardView.explicitResultHeight", controller_text)
        self.assertIn("[actionSeparator, actionContainer].forEach", card_text)
        self.assertNotIn("deepSeekShortcutPlate", card_text)
        self.assertIn("ragImePrivacyDisposition == \"allowed\"", patch_text)
        self.assertIn("let privacyDisposition: String", patch_text)
        self.assertIn("request.privacyDisposition == \"allowed\"", patch_text)
        self.assertIn('"--privacy-disposition"', patch_text)
        self.assertNotIn("badgeView", row_text)
        self.assertIn("func animateContentIn", row_text)
        self.assertIn("func animateAccepted", row_text)
        self.assertIn("setGeneratingPulse", card_text)
        self.assertIn("[progressTitleLabel, progressContainer, stopButton].forEach", card_text)
        self.assertIn("case .explicitGenerating:", card_text)
        self.assertIn("progressContainer.frame = NSRect", card_text)
        self.assertIn(
            "width: min(configuredMaximumWidth, RagImeSuggestionCardView.thinkingWidth)",
            controller_text,
        )
        self.assertIn("private let stateTint = NSView()", card_text)
        self.assertIn("private let accentRail = NSView()", card_text)
        self.assertIn("updateThemeChrome(for: state)", card_text)
        self.assertIn('resultHeader.stringValue = streaming ? "正在接收内容" : "生成结果"', card_text)
        self.assertIn("layoutActionBar(frame: NSRect(x: 12, y: 5", card_text)
        self.assertIn("let segment = max(0, floor((frame.width - gap) / 2))", card_text)
        self.assertIn("quickGenerateButton.frame = NSRect(x: 0, y: 0, width: segment", card_text)
        self.assertNotIn("visualGenerateButton", card_text)
        self.assertIn("deepSearchButton.frame = NSRect(", card_text)
        self.assertIn("private func updateActionGroupChrome()", card_text)
        self.assertIn("actionContainer.layer?.borderWidth = 0", card_text)
        self.assertIn("button.layer?.backgroundColor = fill", card_text)
        self.assertIn("actionDividerLeading.layer?.backgroundColor = NSColor.clear.cgColor", card_text)
        self.assertNotIn("actionDividerTrailing", card_text)
        self.assertIn("let gap: CGFloat = compact", card_text)
        self.assertIn("x: segment + gap", card_text)
        self.assertNotIn("deepSeekButton.layer?.backgroundColor = NSColor.systemBlue", card_text)
        self.assertNotIn("deepSearchButton.layer?.backgroundColor = NSColor.systemIndigo", card_text)
        self.assertIn('keys: ["timelineRecentInputChars"]', card_text)
        self.assertIn('"历史补充 \\(recentInputChars) 字"', card_text)
        self.assertIn('"历史已记录，本次未引用"', card_text)
        self.assertIn("let remoteUnavailable = status.contains", card_text)
        self.assertNotIn('return "远程模型未启用"', card_text)
        self.assertIn("assistant_panel_present_animation_started", controller_text)
        self.assertIn("assistant_candidate_content_transition_started", controller_text)
        self.assertIn("assistant_candidate_group_exit_animation_started", controller_text)
        self.assertIn("assistant_context_diagnostics_rendered", controller_text)
        self.assertIn("private static weak var activeOwner", controller_text)
        self.assertIn("claimSingletonOwnership", controller_text)
        self.assertIn("scheduleTTL(for: payload)", controller_text)
        self.assertIn("assistant_overlay_singleton_replaced", controller_text)
        self.assertIn("assistant_overlay_ttl_expired", controller_text)
        self.assertIn('dismiss(reason: "ttl_expired")', controller_text)
        self.assertIn('"ttlSource": hasRealCandidate', controller_text)
        self.assertIn("let fallbackMs = 4_000", controller_text)
        self.assertIn(
            "let expiresAfterMs = pendingWithoutResult && !isNoResultFeedback",
            controller_text,
        )
        self.assertIn("? max(12_000, boundedMs)", controller_text)
        self.assertNotIn("hasRealCandidate ? max(12_000, boundedMs)", controller_text)
        self.assertIn("renderedContentSignature", controller_text)
        self.assertIn("scheduleTTL(for: payload)", controller_text)
        self.assertIn("onDismiss: ((String) -> Void)?", controller_text)
        self.assertIn("layoutResultText()", card_text)
        self.assertIn("applyConfiguration(candidateFontSize:", card_text)
        self.assertIn('payload.overlayConfigBool("fadeAnimation")', controller_text)
        self.assertIn('payload.overlayConfigNumber("maxWidth")', controller_text)
        self.assertIn('payload.overlayConfigNumber("expiresAfterMs")', controller_text)
        self.assertIn("let overlayConfig: [String: RagImeJSONValue]?", patch_text)
        self.assertIn('payload.overlayConfigKeyPolicy("tab")', patch_text)
        self.assertIn("private var ragImeActiveRagGeneration: Int = 0", patch_text)
        self.assertIn("private var ragImeActiveRagPollWorkItem: DispatchWorkItem?", patch_text)
        self.assertIn("func beginRagImeActiveRagSession", patch_text)
        self.assertIn("func cancelRagImeActiveRagSession", patch_text)
        self.assertIn("func ragImeActiveRagSessionIsLive", patch_text)
        self.assertIn('cancelRagImeActiveRagSession(reason: "sensitive_field")', patch_text)
        self.assertIn("cancelRagImeActiveRagSession(reason: reason)", patch_text)
        self.assertIn('"start_callback_generation_changed"', patch_text)
        self.assertIn('"status_callback_generation_changed"', patch_text)
        self.assertIn("request.frontAppBundleId == currentApp", patch_text)
        self.assertIn("request.frontendRevision == ragImeFrontendRevision", patch_text)
        self.assertIn("liveInputSourceId == ragImeActiveRagInputSourceId", patch_text)
        self.assertIn("ragImeActiveRagPollWorkItem?.cancel()", patch_text)
        self.assertIn("generation: generation", patch_text)
        self.assertIn("func requireRagImeActiveRagEnabled", patch_text)
        self.assertIn('userDefaultsKey: "RagImeActiveRagEnabled"', patch_text)
        self.assertIn('"reason": "active_rag_disabled"', patch_text)
        self.assertIn('guard requireRagImeActiveRagEnabled(trigger: "shortcut") else { return false }', patch_text)
        self.assertIn('guard requireRagImeActiveRagEnabled(trigger: "selection") else { return false }', patch_text)
        self.assertIn('guard requireRagImeActiveRagEnabled(trigger: "context") else { return false }', patch_text)
        self.assertIn("reduceMotion: Bool", row_text)
        self.assertIn('"contextChars": .number(Double(request.contextChars))', patch_text)
        self.assertIn('"evidenceCount": .number(Double(response.evidenceCount))', patch_text)
        self.assertIn('"traceIncludesText": false', patch_text)
        self.assertNotIn('"selectedTextPreview": request.selectedTextPreview', patch_text)
        self.assertIn('let postCommitIdleMs: Int', patch_text)
        self.assertIn('"rag_ime/post_commit_idle_ms", fallback: 420, range: 40...1000', patch_text)
        self.assertIn('snapshot.source == "ime_commit_ledger"', patch_text)
        self.assertIn("freshSnapshot.commitTextMatched", patch_text)
        self.assertIn('["text_input_client", "accessibility", "ime_commit_ledger"]', patch_text)
        self.assertIn('if reason == "front_app_changed"', patch_text)
        self.assertIn("let idleMs = acceptedCandidate ? 0 : sidecarClient.postCommitIdleMs", patch_text)
        self.assertIn("IsSecureEventInputEnabled()", patch_text)
        self.assertIn('reason: "ax_secure_text_field"', patch_text)
        self.assertIn('reason: "sensitive_field_metadata"', patch_text)
        self.assertIn('reason: "privacy_unknown_app_bundle_missing"', patch_text)
        self.assertIn('reason: "privacy_unknown_ax_not_trusted"', patch_text)
        self.assertIn('reason: "privacy_unknown_focused_element_missing"', patch_text)
        self.assertIn('reason: "privacy_unknown_metadata_read_failed"', patch_text)
        self.assertIn('reason: "privacy_unknown_text_field_metadata_missing"', patch_text)
        self.assertNotIn("metadataReads.contains(where: { $0.failed })", patch_text)
        self.assertIn('secureRole.contains("text")', patch_text)
        self.assertIn('reason: "sensitive_application_bundle"', patch_text)
        self.assertIn('"com.apple.passwords"', patch_text)
        self.assertIn('"org.torproject.torbrowser"', patch_text)
        self.assertIn('"RAG_IME_SENSITIVE_APP_BUNDLE_IDS"', patch_text)
        self.assertIn('"RAG_IME_SENSITIVE_APP_BUNDLE_TOKENS"', patch_text)
        self.assertIn('"RagImeSensitiveAppBundleIds"', patch_text)
        self.assertIn('"RagImeSensitiveAppBundleTokens"', patch_text)
        self.assertIn('"password", "passcode", "密码", "口令"', patch_text)
        self.assertNotIn('"username", "user name"', patch_text)
        self.assertNotIn('"one-time", "one time", "otp"', patch_text)
        self.assertIn("func enforceRagImeFastPrivacyGuard() -> Bool", patch_text)
        self.assertIn("func ragImeFullTextSHA256(_ text: String) -> String", patch_text)
        self.assertIn("contentSha256: ragImeFullTextSHA256(selectedText)", patch_text)
        self.assertIn('schemaVersion: "rag-ime.input-capture.v2"', patch_text)
        self.assertIn('boundaryKind: boundary.kind', patch_text)
        self.assertIn('boundaryConfidence: boundary.confidence', patch_text)
        self.assertIn("finalCommitted: boundary.finalCommitted", patch_text)
        self.assertIn("let documentLength = client.length()", patch_text)
        self.assertIn(
            "let length = min(documentLength, ragImeFinalizedFieldContextLimit)",
            patch_text,
        )
        self.assertIn(
            "afterLimit: self.ragImeFinalizedFieldContextLimit",
            patch_text,
        )
        self.assertIn(
            "else if !compactImeText.isEmpty && compactFieldText.count < compactImeText.count",
            patch_text,
        )
        self.assertIn('fallbackReason = "field_context_shorter_than_ime"', patch_text)
        self.assertIn("selectedText = compactFieldText", patch_text)
        self.assertIn(
            "ragImeCommittedContext = boundedRagImeCommittedContext(text)",
            patch_text,
        )
        self.assertIn("func boundedRagImeFinalizedAccessibilityContext(", patch_text)
        self.assertIn("String(before.suffix(beforeBudget))", patch_text)
        self.assertIn("String(after.prefix(afterBudget))", patch_text)
        self.assertIn(
            'RagImeSensitiveFieldStatus(isSensitive: false, reason: "privacy_unknown_ax_not_trusted")',
            patch_text,
        )
        self.assertIn('"privacy_unknown_focused_element_missing"', patch_text)
        self.assertIn(
            "AXUIElementCreateApplication(frontmostApp.processIdentifier)",
            patch_text,
        )
        self.assertIn('"AXManualAccessibility" as CFString', patch_text)
        self.assertIn("application_after_manual_accessibility", patch_text)
        self.assertIn("AXUIElementGetPid(focusedElement", patch_text)
        self.assertIn('reason: "privacy_unknown_front_app_mismatch"', patch_text)
        self.assertIn("private let ragImeForegroundCaptureTimeoutMs: Int = 480", patch_text)
        self.assertIn(
            'RagImeSensitiveFieldStatus(isSensitive: true, reason: "secure_event_input")',
            patch_text,
        )
        self.assertIn(
            'RagImeSensitiveFieldStatus(isSensitive: true, reason: "sensitive_application_bundle")',
            patch_text,
        )
        self.assertIn("func clearRagImeSensitiveRuntimeState()", patch_text)
        self.assertIn("ragImeCommittedContext = \"\"", patch_text)
        self.assertIn("ragImeCommitBurstTexts = []", patch_text)
        self.assertIn("ragImeLastForegroundTextSnapshot = nil", patch_text)
        self.assertIn('invalidateRagImeForegroundTransaction(reason: "deactivate_server")', patch_text)
        self.assertIn('dismiss(reason: "input_controller_deinit")', patch_text)
        self.assertIn(
            "guard !ragImeSensitiveFieldActive || sensitiveSafeEvents.contains(event) else { return }",
            patch_text,
        )
        self.assertNotIn("let includeText = !sensitiveField", patch_text)
        self.assertIn("private let ragImeDisplayHoldoverDuration: TimeInterval = 2.6", patch_text)
        self.assertIn("private let ragImePostCommitDisplayHoldoverDuration: TimeInterval = 8.0", patch_text)
        self.assertIn("func canUseRagImeDisplayHoldover(", patch_text)
        self.assertIn("func canUseRagImePostCommitDisplayHoldover(", patch_text)
        self.assertIn("func canSelectCurrentRagImeDisplayCandidates(", patch_text)
        self.assertIn("func canSelectRagImeDisplayCandidate(_ candidate: RagImeDisplayCandidate) -> Bool", patch_text)
        self.assertIn("func ragImeDisplayCandidateSessionFingerprint(_ candidate: RagImeDisplayCandidate) -> String", patch_text)
        self.assertIn("func ragImeJSONValueString(_ value: RagImeJSONValue?) -> String?", patch_text)
        self.assertIn("func ragImeDisplayTraceTransactionFields() -> [String: Any]", patch_text)
        self.assertIn('if event == "panel_display_candidates"', patch_text)
        self.assertIn("postCommitHoldover", patch_text)
        self.assertIn("ragImePostCommitDisplayHoldoverDuration", patch_text)
        self.assertIn("let predictionSession: RagImePredictionSessionPayload?", patch_text)
        self.assertIn("let progressive: RagImeProgressivePayload?", patch_text)
        self.assertIn("let progressiveFollowUp: Bool", patch_text)
        self.assertIn("struct RagImePredictionSessionPayload: Codable", patch_text)
        self.assertIn("init(from decoder: Decoder) throws", patch_text)
        self.assertIn("decodeIfPresent(String.self, forKey: .pinyinPrefix) ?? \"\"", patch_text)
        self.assertIn("decodeIfPresent(Bool.self, forKey: .shouldClearPredictionPanel) ?? false", patch_text)
        self.assertIn("struct RagImeProgressivePayload: Codable", patch_text)
        self.assertIn("let sessionFingerprint: String?", patch_text)
        self.assertIn("let expiresAfterMs: Int?", patch_text)
        self.assertIn("let stablePanelAction: String?", patch_text)
        self.assertIn("let stablePanelReason: String?", patch_text)
        self.assertIn("guard !ragImeDisplayCandidates.isEmpty else { return false }", patch_text)
        self.assertNotIn('ragImeDisplayQueryBasis == "committedContext"', patch_text)
        self.assertIn("let forceSideCandidates = rawInput.isEmpty && preedit.isEmpty", patch_text)
        self.assertIn("let hasPostCommitCaptureSeed = commitBurstReady", patch_text)
        self.assertIn("guard commitBurstReady || acceptedCandidateContinuation", patch_text)
        self.assertIn('"reason": "empty_commit"', patch_text)
        self.assertIn("forceSideCandidates: forceSideCandidates", patch_text)
        self.assertIn("let frontendBuild: String", patch_text)
        self.assertIn("let schemaVersion: String", patch_text)
        self.assertIn("let predictionFirstMerge: Bool", patch_text)
        self.assertIn('frontendBuild: "rag-ime.foreground-trace.v2"', patch_text)
        self.assertIn('schemaVersion: "rag-ime.squirrel-frontend-trace.v1"', patch_text)
        self.assertIn("predictionFirstMerge: true", patch_text)
        self.assertIn('traceRagImeFrontendEvent("sidecar_request_scheduled"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("sidecar_empty_response_cleared"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("sidecar_clear_response_applied"', patch_text)
        self.assertIn("response.predictionSession?.shouldClearPredictionPanel == true", patch_text)
        self.assertIn("let shouldClearEmptyResponse = response.predictionSession?.shouldClearPredictionPanel == true", patch_text)
        self.assertNotIn("if overlayVisible {", patch_text)
        self.assertIn("scheduleRagImeProgressiveFollowUpIfNeeded(\n+        response: response", patch_text)
        self.assertIn("let shouldKeepFingerprintForFollowUp = response.progressive?.enabled == true", patch_text)
        self.assertIn("keepLastFingerprint: shouldKeepFingerprintForFollowUp", patch_text)
        self.assertIn('"keptExistingPanel": !shouldClearEmptyResponse', patch_text)
        self.assertIn("displayCandidatesToApply.allSatisfy({ ragImeDisplayCandidateSessionFingerprint($0) == responseSessionFingerprint })", patch_text)
        self.assertIn("clearRagImeDisplayCandidates()", patch_text)
        self.assertIn("rag-ime.foreground-trace.v2", patch_text)
        self.assertIn("struct RagImeForegroundTextSnapshot: Codable", patch_text)
        self.assertIn("captureForegroundTextForSidecar", patch_text)
        self.assertIn("RagImeSelectedTextProvider.swift in Sources", patch_text)
        self.assertIn("private let ragImeSelectedTextProvider = RagImeSelectedTextProvider()", patch_text)
        self.assertIn("kAXSelectedTextRangeAttribute", patch_text)
        self.assertIn("kAXStringForRangeParameterizedAttribute", patch_text)
        self.assertIn("kAXValueAttribute", patch_text)
        self.assertIn('source: "accessibility"', patch_text)
        self.assertIn("let wholeValueHash: String", patch_text)
        self.assertIn("let wholeValueChars: Int", patch_text)
        self.assertIn("let warnings: [String]", patch_text)
        self.assertIn("foregroundText: foregroundText", patch_text)
        self.assertIn("foregroundText: request.foregroundText", patch_text)
        self.assertIn('let fallbackForegroundSource = (!rawInput.isEmpty || !preedit.isEmpty)', patch_text)
        self.assertIn('"text_input_client"', patch_text)
        self.assertIn('"ime_commit_ledger"', patch_text)
        self.assertIn('"ime_commit_ledger_same_app"', patch_text)
        self.assertNotIn("allowAccessibility: false", patch_text)
        self.assertIn("RagImeForegroundContextResolver", patch_text)
        self.assertIn("foreground_context_capture_scheduled", patch_text)
        self.assertIn("foreground_context_capture_resolved", patch_text)
        self.assertIn("foreground_context_capture_failed", patch_text)
        self.assertIn("capturedAtMs", patch_text)
        self.assertIn("captureFailureReason", patch_text)
        self.assertIn("assistant_overlay_candidate_visible", patch_text)
        self.assertIn("side_candidate_feedback_recorded", patch_text)
        self.assertIn('"foregroundTextSource": foregroundText.source', patch_text)
        self.assertIn('"foregroundTextSource": foregroundText.source', patch_text)
        self.assertIn("selectedTextHash: \"\"", patch_text)
        self.assertIn("selectedTextPreview: \"\"", patch_text)
        self.assertIn("canReplaceSelection: false", patch_text)
        self.assertIn("guard !displayCandidatesToApply.isEmpty else {", patch_text)
        self.assertIn("committedContext: ragImeCommittedContext", patch_text)
        self.assertIn("removeLastRagImeCommittedContextCharacterIfNoComposition()", patch_text)
        self.assertIn(
            "cancelRagImeCommitBurstAfterDelete()\n+        removeLastRagImeCommittedContextCharacterIfNoComposition()",
            patch_text,
        )
        self.assertIn("func removeLastRagImeCommittedContextCharacter()", patch_text)
        self.assertIn("func boundedRagImeCommittedContext(_ text: String) -> String", patch_text)
        self.assertIn("func syncRagImeCommittedContextFromClient(excludingMarkedText markedText: String = \"\") -> String", patch_text)
        self.assertIn("client.attributedSubstring(from: range)", patch_text)
        self.assertIn("func invalidateRagImeDisplayForInputChange(reason: String, keyCode: UInt16)", patch_text)
        self.assertIn("boundedFallback.hasSuffix(capturedContext)", patch_text)
        self.assertIn('traceRagImeFrontendEvent("text_input_client_short_suffix_preserved"', patch_text)
        self.assertIn("committed_context_trimmed_after_delete", patch_text)
        self.assertNotIn("committed_context_invalidated_after_delete", patch_text)
        self.assertIn('traceRagImeFrontendEvent("sidecar_response_dropped_stale"', patch_text)
        self.assertIn('"input_generation_changed"', patch_text)
        self.assertIn('"response_too_late_for_foreground"', patch_text)
        self.assertIn("func ragImeResponseContainsRimeFallback(_ response: RagImeSidecarResponse) -> Bool", patch_text)
        self.assertIn("func ragImeDisplayCandidatesContainComposableSideCandidate() -> Bool", patch_text)
        self.assertIn('selectionScope == "mixed_prediction_first"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("sidecar_rime_candidate_change_allowed"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("sidecar_rime_fallback_stripped"', patch_text)
        self.assertIn('candidate.sourceType != "rime" && candidate.selectionAction != "select_rime_candidate"', patch_text)
        self.assertIn("displayCandidatesToApply = sideOnlyCandidates", patch_text)
        self.assertIn('traceRagImeFrontendEvent("sidecar_stale_fingerprint_allowed"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("sidecar_input_generation_change_allowed"', patch_text)
        self.assertIn("let canApplySideOnlyAcrossGeneration = !responseContainsRimeFallback", patch_text)
        self.assertIn("&& currentRawInput == request.rawInput", patch_text)
        self.assertIn("&& ragImeCommittedContext == request.committedContext", patch_text)
        self.assertIn('traceRagImeFrontendEvent("sidecar_progressive_generation_change_allowed"', patch_text)
        self.assertIn('dropRagImeSidecarResponse("rime_candidates_changed"', patch_text)
        self.assertIn('dropRagImeSidecarResponse("raw_input_changed"', patch_text)
        self.assertIn("currentRagImeContextMatches(request, requireRimeFallback: responseContainsRimeFallback)", patch_text)
        self.assertIn("private var ragImePendingRequestFingerprint: String = \"\"", patch_text)
        self.assertIn("private var ragImeDisplayExpiryWorkItem: DispatchWorkItem?", patch_text)
        self.assertIn("private var ragImePendingContinuationCandidate: RagImeDisplayCandidate?", patch_text)
        self.assertIn("private var ragImePendingContinuationCommittedText: String = \"\"", patch_text)
        self.assertIn("private var ragImePendingContinuationPreviousContextHash: String = \"\"", patch_text)
        self.assertIn("fingerprint == ragImeLastRequestFingerprint || fingerprint == ragImePendingRequestFingerprint", patch_text)
        self.assertIn("guard response.requestSeq == request.requestSeq else {", patch_text)
        self.assertIn("guard response.committedContext == request.committedContext else {", patch_text)
        self.assertIn("guard ragImeCommittedContext == request.committedContext else {", patch_text)
        self.assertIn("func completeRagImeSidecarRequest(fingerprint: String, keepLastFingerprint: Bool)", patch_text)
        self.assertIn("func scheduleRagImeDisplayExpiry()", patch_text)
        self.assertIn("func scheduleRagImeProgressiveFollowUpIfNeeded(", patch_text)
        self.assertIn("func ragImeTraceProgressive(_ progressive: RagImeProgressivePayload?) -> [String: Any]", patch_text)
        self.assertIn('traceRagImeFrontendEvent("sidecar_progressive_followup_scheduled"', patch_text)
        self.assertIn("ragImeLastRequestFingerprint = fingerprint\n+    ragImePendingRequestFingerprint = fingerprint", patch_text)
        self.assertNotIn("text_input_client_synchronous_fallback_after_", patch_text)
        self.assertIn('traceRagImeFrontendEvent("foreground_context_capture_retry"', patch_text)
        self.assertIn('reason: "privacy_probe_timeout"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("sidecar_progressive_followup_sent"', patch_text)
        self.assertIn("progressiveFollowUp: true", patch_text)
        self.assertIn('"progressive": ragImeTraceProgressive(response.progressive)', patch_text)
        self.assertIn('"panelUsesDisplayCandidates": ragImePanelUsesDisplayCandidates', patch_text)
        self.assertIn("ragImePanelUsesDisplayCandidates = !displayCandidatesToApply.isEmpty", patch_text)
        self.assertIn("func ragImeDisplayStateFingerprint() -> String", patch_text)
        self.assertIn('traceRagImeFrontendEvent("panel_expired_cleared"', patch_text)
        self.assertIn("ragImeDisplayExpiryWorkItem?.cancel()", patch_text)
        self.assertIn("rimeUpdate(clearReservedComments: false, requestSidecar: false)", patch_text)
        self.assertIn("func scheduleRagImePostCommitContinuation(committedText: String, sourceCandidate: RagImeDisplayCandidate)", patch_text)
        self.assertIn("func observeRagImePostCommitContinuationIfReady()", patch_text)
        self.assertIn('traceRagImeFrontendEvent("side_candidate_commit_pending"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("side_candidate_continuation_scheduled"', patch_text)
        self.assertIn("showRagImeAssistantOverlayPending(request: request)", patch_text)
        self.assertIn('uiMode: "post_commit_pending"', patch_text)
        self.assertIn('traceEvent: "assistant_overlay_post_commit_pending"', patch_text)
        self.assertIn('("快速生成", "start_active_rag_from_context"', patch_text)
        self.assertIn('("框选对话", "start_visual_rag_from_context"', patch_text)
        self.assertIn('("深度查找", "start_agent_deep_search_from_context"', patch_text)
        self.assertIn("ragImePendingContinuationCandidate = sourceCandidate", patch_text)
        self.assertIn("guard committedContextHash != ragImePendingContinuationPreviousContextHash || committedContext.contains(committedText) else {", patch_text)
        self.assertIn("scheduleRagImePostCommitContinuation(committedText: insertText, sourceCandidate: candidate)", patch_text)
        self.assertIn('commitTextPreview: committedText', patch_text)
        self.assertIn("immediate: Bool = false", patch_text)
        self.assertIn("let delay = TimeInterval(immediate ? 16 : sidecarClient.debounceMs) / 1000", patch_text)
        self.assertIn("immediate: true", patch_text)
        self.assertIn("let frontendTrace: Bool", patch_text)
        self.assertIn("rag_ime/frontend_trace", patch_text)
        self.assertIn("func traceRagImeFrontendEvent(_ event: String, fields: [String: Any])", patch_text)
        self.assertIn(
            "guard ragImeSidecarClient?.frontendTrace == true || isCaptureDeliveryEvent else { return }",
            patch_text,
        )
        self.assertIn("ragImePrepareFrontendTraceLog", patch_text)
        self.assertIn(".posixPermissions: 0o600", patch_text)
        self.assertIn('appendingPathExtension("1")', patch_text)
        self.assertIn("let contextAnchor = ragImeStableTextHash(context)", patch_text)
        self.assertIn("queryAnchor: contextAnchor", patch_text)
        self.assertIn("displayAnchor: contextAnchor", patch_text)
        self.assertIn('traceRagImeFrontendEvent("panel_display_candidates"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("side_candidate_commit"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("candidate_snapshot_selection_accepted"', patch_text)
        self.assertIn('traceRagImeFrontendEvent("candidate_snapshot_selection_rejected_stale"', patch_text)
        self.assertIn("shownCandidates: ragImeDisplayCandidates", patch_text)
        self.assertIn("let shownCandidates: [RagImeDisplayCandidate]", patch_text)
        self.assertIn("traceRagImePredictionEvents(response.predictionTraceEvents)", patch_text)
        self.assertIn("func traceRagImePredictionEvents(_ events: [RagImePredictionTraceEvent]?)", patch_text)
        self.assertIn("func ragImeJSONValueDictionary(_ dictionary: [String: RagImeJSONValue]) -> [String: Any]", patch_text)
        self.assertIn("func ragImeJSONValueToAny(_ value: RagImeJSONValue) -> Any", patch_text)
        self.assertNotIn('ragImeDisplayNumberKeyPolicy == "select_visible_candidate"', patch_text)
        self.assertIn('ragImeDisplayNumberKeyPolicy = response.keyPolicy?.numberKeys ?? ""', patch_text)
        self.assertIn('ragImeDisplayTabPolicy = response.keyPolicy?.tab ?? ""', patch_text)
        self.assertIn('ragImeDisplayOptionNumberPolicy = response.keyPolicy?.optionNumber ?? ""', patch_text)
        self.assertIn('ragImeOverlayTabPolicy == "accept_top_prediction"', patch_text)
        self.assertIn('ragImeOverlayTabPolicy == "accept_top_when_ready"', patch_text)
        self.assertIn('selectRagImeOverlayCandidate(atOrdinal: 1, route: "tab", key: "tab")', patch_text)
        self.assertIn('"assistant_overlay_tab_waiting_for_result"', patch_text)
        self.assertIn('"assistant_overlay_unchanged_skipped"', patch_text)
        self.assertIn("min(max(serverDelayMs, 80), 180)", patch_text)
        self.assertIn("serverDelayMs > 0", patch_text)
        self.assertIn('candidate.metadata["streamingPartial"]', patch_text)
        self.assertIn('ragImeOverlayOptionNumberPolicy == "select_prediction_by_ordinal"', patch_text)
        self.assertIn('selectRagImeOverlayCandidate(forKey: String(char), route: "option_number")', patch_text)
        self.assertIn("RagImeAssistantPanelController.swift in Sources", patch_text)
        self.assertIn("func applyRagImeAssistantOverlay(", patch_text)
        self.assertIn("func commitRagImeAssistantCandidate(", patch_text)
        self.assertIn("replaceSelection: Bool = false", patch_text)
        self.assertIn("func replaceRagImeAssistantSelection(with text: String", patch_text)
        self.assertIn('client.insertText(text, replacementRange: selectedRange)', patch_text)
        self.assertIn('endpoint("assistant-candidate-action", base: sidecarURL)', patch_text)
        self.assertIn('recordRagImeAssistantCandidateAction("remember")', patch_text)
        self.assertIn('recordRagImeAssistantCandidateAction("suppress")', patch_text)
        self.assertIn('replacingOccurrences(of: "sha256:", with: "")', patch_text)
        self.assertIn("assistant_overlay_candidate_accepted", patch_text)
        self.assertIn("response.candidatePanel?.candidates", patch_text)
        self.assertIn("func selectRagImePredictionCandidate(forKey key: String, route: String) -> Bool", patch_text)
        self.assertIn("func selectRagImePredictionCandidate(atOrdinal ordinal: Int, route: String, key: String) -> Bool", patch_text)
        self.assertIn('traceRagImeFrontendEvent(eventName, fields: [', patch_text)
        self.assertIn('let eventName = route == "tab" ? "tab_key_route" : "option_number_route"', patch_text)
        self.assertIn('candidate.sourceType != "raw_english" && candidate.sourceType != "rime"', patch_text)
        self.assertIn('"candidateOrdinal": candidate.candidateOrdinal ?? 0', patch_text)
        self.assertIn('"candidateStableId": candidate.candidateStableId ?? ""', patch_text)
        self.assertIn('"snapshotId": candidate.snapshotId ?? ""', patch_text)
        self.assertIn('"sourceStability": candidate.sourceStability ?? ""', patch_text)
        self.assertIn("func ragImeCandidateSourceBadgesEnabled() -> Bool", patch_text)
        self.assertIn("func ragImeCandidateSourceColorsEnabled() -> Bool", patch_text)
        self.assertIn("func ragImeCandidateDiagnosticsEnabled() -> Bool", patch_text)
        self.assertIn("func ragImeDisplayBadge(for candidate: RagImeDisplayCandidate) -> String", patch_text)
        self.assertIn("func ragImeDisplayText(for candidate: RagImeDisplayCandidate) -> String", patch_text)
        self.assertIn("func ragImeStripSourceSuffix(_ text: String) -> String", patch_text)
        self.assertIn("let visibleCandidates = ragImeDisplayCandidates.filter", patch_text)
        self.assertIn("visibleCandidates.map { ragImeDisplayText(for: $0) }", patch_text)
        self.assertIn("let text = candidate.text.trimmingCharacters(in: .whitespacesAndNewlines)", patch_text)
        self.assertIn("let base = ragImeStripSourceSuffix(text.isEmpty ? candidate.insertText : text)", patch_text)
        self.assertIn('case "model", "rag", "memory", "status", "action":', patch_text)
        self.assertIn('return "[\\(badge)] \\(base)"', patch_text)
        self.assertIn("let insertText = ragImeStripSourceSuffix(candidate.insertText.isEmpty ? candidate.text : candidate.insertText)", patch_text)
        self.assertIn("if !stripped.isEmpty", patch_text)
        self.assertIn("func ragImeDisplayComment(for candidate: RagImeDisplayCandidate) -> String", patch_text)
        self.assertIn(
            '+    return ""\n+  }',
            patch_text,
        )
        self.assertNotIn('return "\\(badge)·\\(candidate.comment)"', patch_text)
        self.assertNotIn("ragImeDisplayCandidates.map { $0.text }", patch_text)
        self.assertIn("RAG_IME_CANDIDATE_SOURCE_BADGES", patch_text)
        self.assertIn("RAG_IME_CANDIDATE_SOURCE_COLORS", patch_text)
        self.assertIn("RAG_IME_CANDIDATE_DIAGNOSTICS", patch_text)
        self.assertIn("func ragImeSourceColor(_ sourceType: String) -> NSColor?", patch_text)
        self.assertIn("return .systemBlue", patch_text)
        self.assertIn("return .systemTeal", patch_text)
        self.assertIn("return .systemPurple", patch_text)
        self.assertIn("return .systemOrange", patch_text)
        self.assertIn("return .systemGray", patch_text)
        build_script = (root / "scripts" / "build_patched_squirrel.sh").read_text(encoding="utf-8")
        self.assertIn('"displayTextPrefersCleanText": True', build_script)
        self.assertNotIn('"displayTextUsesInsertText": True', build_script)
        self.assertIn('cp "$source_dir/$file" "$SQUIRREL_WORKDIR/sources/$file"', build_script)
        self.assertNotIn('if [[ ! -f "$SQUIRREL_WORKDIR/sources/$file"', build_script)
        self.assertIn('"schemaVersion": "rag-ime.squirrel-build-marker.v2"', build_script)
        self.assertIn('"overlaySha256": "$overlay_sha256"', build_script)
        self.assertIn(
            "+    committedContext: String,\n"
            "+    foregroundText: RagImeForegroundTextSnapshot,\n"
            "+    page: Int,\n"
            "+    highlighted: Int,\n"
            "+    candidates: [RagImeRimeCandidatePayload]\n"
            "+  ) -> String {\n"
            "+    return [\n"
            "+      rawInput,\n"
            "+      preedit,\n"
            "+      commitTextPreview,\n"
            "+      committedContext,",
            patch_text,
        )
        self.assertIn("+      ragImeStableTextHash(foregroundText.surroundingBefore),", patch_text)
        self.assertIn("+      ragImeStableTextHash(foregroundText.surroundingAfter),", patch_text)
        self.assertIn("+      foregroundText.wholeValueHash,", patch_text)
        self.assertIn(
            "+  func selectRagImeSideCandidate(forKey key: String) -> Bool {\n"
            "+    guard ragImePanelUsesDisplayCandidates else {\n"
            "+      return false\n"
            "+    }\n"
            "+    guard canSelectCurrentRagImeDisplayCandidates() else {\n"
            "+      traceRagImeFrontendEvent(\"stale_candidate_selection_rejected\", fields: [",
            patch_text,
        )
        self.assertIn("+    guard let index = ragImeDisplayCandidates.firstIndex(where: { ragImeSelectionKey(for: $0) == key }) else {", patch_text)
        self.assertIn('traceRagImeFrontendEvent("number_key_route"', patch_text)
        self.assertIn('guard ragImeDisplayCandidates[index].sourceType == "rime" else {', patch_text)
        self.assertEqual(patch_text.count("selectRagImeSideCandidate("), 1)
        self.assertIn("func ragImeDisplayCandidateMatchesTransaction(_ candidate: RagImeDisplayCandidate, response: RagImeSidecarResponse) -> Bool", patch_text)
        self.assertIn("let responseSnapshotId = response.predictionSession?.snapshotId ?? response.predictionSession?.stableSnapshotId ?? \"\"", patch_text)
        self.assertIn("candidate.snapshotId != responseSnapshotId", patch_text)
        self.assertIn("candidate.snapshotId == ragImeDisplaySnapshotId", patch_text)
        self.assertIn("ragImeDisplayCandidates[ordinal - 1].candidateStableId == candidate.candidateStableId", patch_text)
        self.assertIn("let nowMs = Int(Date().timeIntervalSince1970 * 1000)", patch_text)
        self.assertIn("guard nowMs <= expiresAtMs else { return false }", patch_text)
        self.assertIn("func ragImeSanitizedTraceValue(_ value: Any, key: String, includeText: Bool) -> Any", patch_text)
        self.assertIn('ProcessInfo.processInfo.environment["RAG_IME_TRACE_INCLUDE_TEXT"] == "1"', patch_text)
        self.assertIn("guard canSelectRagImeDisplayCandidate(ragImeDisplayCandidates[index]) else {", patch_text)
        self.assertIn("guard canSelectRagImeDisplayCandidate(candidate) else {", patch_text)
        self.assertIn("+    return selectCandidate(index)", patch_text)
        self.assertIn("ragImeDisplayTabPolicy = \"\"", patch_text)
        self.assertIn("ragImeDisplayOptionNumberPolicy = \"\"", patch_text)
        self.assertIn("observeRagImePostCommitContinuationIfReady()", patch_text)
        self.assertIn("showRagImePostCommitActionPlaceholder(request: request)", patch_text)
        self.assertIn("rime_composition_started", patch_text)
        self.assertIn("rime_composition_candidates_visible", patch_text)
        self.assertIn("let evidencePreview: String\n+  let confidence: Double?\n+  let suggestionId: String\n+  let memoryId: String", patch_text)
        self.assertIn("composition_ai_suppressed", patch_text)
        self.assertIn("reason\": \"rime_composition_phase", patch_text)
        self.assertIn("if forceSideCandidates {", patch_text)
        self.assertIn("assistant_overlay_post_commit_pending", patch_text)
        self.assertIn('numberKeys: "pass_through"', patch_text)
        self.assertIn("require_trace_event_field()", build_script)
        self.assertIn("assistant_overlay_active_rag_thinking", build_script)
        self.assertIn("active_rag_status_poll_scheduled", build_script)
        self.assertIn('"frontAppBundleId": currentApp', build_script)
        self.assertIn('"selectedTextChars": request.selectedTextChars', build_script)
        self.assertIn('"traceIncludesText": false', build_script)

    def test_context_inspector_describes_generation_text_projection(self) -> None:
        root = Path(__file__).resolve().parents[1]
        controller_text = (
            root / "squirrel-patches" / "sources" / "RagImeAssistantPanelController.swift"
        ).read_text(encoding="utf-8")
        row_text = (
            root / "squirrel-patches" / "sources" / "RagImeSuggestionRowView.swift"
        ).read_text(encoding="utf-8")

        self.assertIn('appendSection("本轮输入"', controller_text)
        self.assertIn('appendSection("窗口语义"', controller_text)
        self.assertIn("zed_workspace_state", controller_text)
        self.assertIn("控件、动作与内部节点未发送给模型", controller_text)
        self.assertNotIn('node["actions"]', controller_text)
        self.assertNotIn('node["nodeRef"]', controller_text)
        self.assertIn('appendSection("当前规划与任务"', controller_text)
        self.assertIn('appendSection("最近批准时间线"', controller_text)
        self.assertIn('appendSection("知识库事实 · Atom"', controller_text)
        self.assertIn('appendSection("知识主题 · Book"', controller_text)
        self.assertIn('appendSection("RAG 依据"', controller_text)
        self.assertIn('appendSection("依据状态"', controller_text)
        self.assertIn("本轮没有召回可显示的知识依据", controller_text)
        self.assertIn('keys: ["evidencePreview", "preview", "textPreview", "text", "summary", "detail"]', controller_text)
        self.assertIn('keys: ["sourceLane", "sourceType"]', controller_text)
        self.assertIn('contextSchemaVersion != "rag-ime.active-rag-context-view.v1"', controller_text)
        self.assertIn("RagImeAssistantContextInspectorState", controller_text)
        status_schema = (
            root / "rag_ime" / "contracts" / "json" / "active-rag-status.v1.json"
        ).read_text(encoding="utf-8")
        self.assertIn('"contextView": {"$ref": "#/$defs/contextView"}', status_schema)
        self.assertIn('"groundingEvidence": {"type": "array", "maxItems": 12}', status_schema)
        self.assertIn('"redactedEvidence"', status_schema)
        grounding_start = controller_text.index('let groundingEvidence = array(contextView["groundingEvidence"])')
        grounding_body = controller_text[grounding_start : grounding_start + 3200]
        self.assertIn("groundingEvidence.prefix(8)", grounding_body)
        self.assertIn("if groundingEvidence.isEmpty {", grounding_body)
        self.assertLess(
            grounding_body.index("if groundingEvidence.isEmpty {"),
            grounding_body.index('array(contextView["evidenceHints"])'),
        )
        self.assertIn('array(contextView["recentCompleteInputs"]).prefix(3)', controller_text)
        self.assertIn('appendSection("最近完整输入 · 仅用于承接"', controller_text)
        self.assertIn("boundedParagraph", controller_text)
        self.assertIn("private static func evidenceDisplayText(_ value: RagImeJSONValue)", controller_text)
        self.assertIn("evidenceDisplayText(value)", controller_text)
        self.assertIn("scrollView.hasHorizontalScroller = false", controller_text)
        self.assertIn("textView.textContainer?.lineBreakMode = .byCharWrapping", controller_text)
        self.assertIn('NSButton(title: "重新生成"', controller_text)
        self.assertIn("retryButton.isHidden = document.state != .error", controller_text)
        self.assertIn("guard document.state == .error, let onRetry else { return }", controller_text)
        self.assertIn("self.onAction?(.retry)", controller_text)
        self.assertIn("readableEvidenceHint", row_text)

    def test_native_rime_selection_feedback_is_local_source_only_and_sensitive_guarded(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")
        prepare_script = (root / "scripts" / "prepare_squirrel_workspace.sh").read_text(encoding="utf-8")
        build_script = (root / "scripts" / "build_patched_squirrel.sh").read_text(encoding="utf-8")

        self.assertIn('endpoint("rime-rank-feedback", base: sidecarURL)', patch_text)
        self.assertIn('sourceType: "rime"', patch_text)
        self.assertIn('selectionSource: "patched_squirrel"', patch_text)
        self.assertIn("guard !enforceRagImeFastPrivacyGuard() else { return }", patch_text)

        consume_start = patch_text.index("func rimeConsumeCommittedText()")
        consume_body = patch_text[consume_start : consume_start + 1800]
        self.assertLess(consume_body.index("rimeAPI.get_commit"), consume_body.index("privacyRestricted"))
        self.assertLess(
            consume_body.index("discardRagImeSensitiveNativeLearningTransaction()"),
            consume_body.index("commit(string: String(cString: text), privacyRestricted: privacyRestricted)"),
        )
        self.assertLess(
            consume_body.index("commit(string: String(cString: text), privacyRestricted: privacyRestricted)"),
            consume_body.index("rimeAPI.free_commit"),
        )
        self.assertIn("if privacyRestricted {", consume_body)
        self.assertIn("guard rimeAPI.get_status(session, &status) else { return false }", patch_text)
        self.assertIn("guard !isComposing else { return false }", patch_text)
        self.assertIn("rimeAPI.process_key(session, Int32(XK_BackSpace), 0)", patch_text)
        self.assertIn("return !backspaceHandled", patch_text)
        self.assertIn('"forwardedToClient": false', patch_text)
        self.assertIn('"traceIncludesText": false', patch_text)
        self.assertIn("func commit(string: String, privacyRestricted: Bool? = nil) -> Bool", patch_text)
        self.assertIn("let isPrivacyRestricted = privacyRestricted ?? enforceRagImeFastPrivacyGuard()", patch_text)
        self.assertIn("func rimeConsumeCommittedText()", patch_text)
        ordinary_key_route_start = patch_text.index("handled = processKey(rimeKeycode, modifiers: rimeModifiers)")
        self.assertIn("rimeUpdate()", patch_text[ordinary_key_route_start : ordinary_key_route_start + 200])
        self.assertIn("ragImeNativeSelectionSnapshot(at: index)", patch_text)
        self.assertIn('traceRagImeFrontendEvent("native_rime_rank_feedback_recorded"', patch_text)
        for script_text in (prepare_script, build_script):
            self.assertIn('"rime-rank-feedback"', script_text)
            self.assertIn('"ragImeNativeSelectionSnapshot"', script_text)
            self.assertIn('"native_rime_rank_feedback_recorded"', script_text)
            self.assertIn('"guard !enforceRagImeFastPrivacyGuard()"', script_text)
        normal_start = patch_text.index("let nativeSelection = ragImeNativeSelectionSnapshot(at: index)")
        normal_selection = patch_text[normal_start : normal_start + 900]
        self.assertLess(
            normal_selection.index("let success = rimeAPI.select_candidate_on_current_page(session, index)"),
            normal_selection.index("recordRagImeNativeSelection(nativeSelection)"),
        )

    def test_physical_key_and_post_commit_paths_never_run_full_accessibility_privacy_inline(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")

        def section(start: str, end: str) -> str:
            start_index = patch_text.index(start)
            return patch_text[start_index : patch_text.index(end, start_index)]

        # These functions execute on the InputMethodKit/main-queue path. The fast
        # guard may inspect Secure Event Input and the local app denylist, but a
        # full field probe would synchronously cross into the foreground process.
        main_queue_sections = {
            "text_input_client_capture": section("func captureFromTextInputClient(", "func captureFromAccessibility("),
            "native_selection_feedback": section("func recordRagImeNativeSelection(", "func rememberRagImeAcceptedEditFeedback("),
            "accepted_edit_feedback": section("func recordRagImeAcceptedEditFeedbackIfNeeded()", "func ragImeDisplayLayout("),
            "assistant_overlay_apply": section("func applyRagImeAssistantOverlay(", "func dismissRagImeAssistantOverlay("),
            "post_commit_refresh": section("func refreshRagImeSidecar(", "func scheduleRagImeCommitBurstPrediction("),
            "post_commit_scheduler": section("func scheduleRagImeCommitBurstPrediction(", "func cancelRagImeCommitBurstAfterDelete("),
            "foreground_resolve": section(
                "func resolveRagImeForegroundContextAndSend(",
                "func probeRagImeForegroundPrivacyAndContext(",
            ),
            "sidecar_send": section("func sendRagImeSidecarRequest(", "func applyRagImeSidecarResponse("),
            "sidecar_apply": section("func applyRagImeSidecarResponse(", "func dropRagImeSidecarResponse("),
            "native_commit_consume": section("func rimeConsumeCommittedText()", "func discardRagImeSensitiveNativeLearningTransaction("),
            "native_commit_insert": section(
                "func commit(string: String, privacyRestricted: Bool? = nil) -> Bool",
                "func show(preedit: String, selRange: NSRange, caretPos: Int)",
            ),
        }
        forbidden_inline_full_probe_tokens = (
            "enforceRagImeSensitiveFieldGuard()",
            ".sensitiveFieldStatus(",
            "AXUIElementCopyAttributeValue(",
            "AXUIElementCopyParameterizedAttributeValue(",
            "captureFromAccessibility(",
        )
        for label, source in main_queue_sections.items():
            for token in forbidden_inline_full_probe_tokens:
                self.assertNotIn(token, source, f"{label} synchronously invokes full Accessibility privacy via {token}")

        self.assertIn("func fastSensitiveFieldStatus(sourceAppBundleId: String = \"\")", patch_text)
        fast_status = section(
            "func fastSensitiveFieldStatus(sourceAppBundleId: String = \"\")",
            "func sensitiveFieldStatus(sourceAppBundleId: String = \"\")",
        )
        for ax_token in (
            "AXIsProcessTrusted",
            "focusedTextElement(",
            "AXUIElementCopyAttributeValue(",
            "privacyStringAttribute(",
            "privacyWindowTitle(",
        ):
            self.assertNotIn(ax_token, fast_status)
        self.assertIn("IsSecureEventInputEnabled()", fast_status)
        self.assertIn("sensitiveApplicationReason", fast_status)
        deterministic_guard = section(
            "func enforceRagImeDeterministicPrivacyGuard(",
            "func enforceRagImeFastPrivacyGuard() -> Bool",
        )
        self.assertIn("fastSensitiveFieldStatus(", deterministic_guard)
        self.assertNotIn("ragImePrivacyDisposition !=", deterministic_guard)
        self.assertIn("func enforceRagImeFastPrivacyGuard() -> Bool", patch_text)
        cached_guard = section(
            "func enforceRagImeFastPrivacyGuard() -> Bool",
            "func applyRagImePrivacyStatus(",
        )
        self.assertIn("enforceRagImeDeterministicPrivacyGuard()", cached_guard)
        self.assertIn('ragImePrivacyDisposition != "allowed"', cached_guard)
        status_model = section("struct RagImeSensitiveFieldStatus", "final class RagImeForegroundContextResolver")
        self.assertLess(
            status_model.index('reason.hasPrefix("privacy_unknown_")'),
            status_model.index('return isSensitive ? "sensitive" : "allowed"'),
        )

        # Every request still performs the full field probe. When an Electron
        # client exposes no AX focused element, a same-transaction IME ledger
        # may authorize the bounded fallback after deterministic guards pass.
        probe = section(
            "func probeRagImeForegroundPrivacyAndContext(",
            "func applyRagImeForegroundPrivacyProbe",
        )
        self.assertIn("ragImeForegroundContextQueue.async", probe)
        self.assertIn("ragImeSelectedTextProvider.sensitiveFieldStatus(", probe)
        self.assertIn("DispatchQueue.main.async", probe)
        self.assertLess(
            probe.index("ragImeForegroundContextQueue.async"),
            probe.index("ragImeSelectedTextProvider.sensitiveFieldStatus("),
        )
        probe_after_full_status = probe[probe.index("ragImeSelectedTextProvider.sensitiveFieldStatus(") :]
        self.assertIn("DispatchQueue.main.async", probe_after_full_status)
        self.assertIn("allowedByImeCommitLedger", patch_text)
        self.assertIn('resolverPath: "ime_commit_ledger_fallback"', patch_text)
        active_context = section(
            "func startRagImeActiveRagAssistFromContext(",
            "func finishRagImeActiveRagAssistFromContext(",
        )
        self.assertIn("ragImeForegroundContextQueue.async", active_context)
        self.assertIn("ragImeForegroundContextResolver.captureFromAccessibility(", active_context)
        self.assertIn("ragImeForegroundContextResolver.captureWindowContext(", active_context)
        self.assertIn("DispatchQueue.main.async", active_context)
        self.assertLess(
            active_context.index("ragImeForegroundContextQueue.async"),
            active_context.index("ragImeForegroundContextResolver.captureWindowContext("),
        )
        self.assertLess(
            active_context.index("ragImeForegroundContextResolver.captureWindowContext("),
            active_context.index("DispatchQueue.main.async"),
        )
        finalized_input = section(
            "func finalizeRagImeActiveInputBuffer(",
            "func removeLastRagImeCommittedContextCharacterIfNoComposition(",
        )
        self.assertIn("ragImeForegroundContextQueue.async", finalized_input)
        self.assertIn(
            "enforceRagImeDeterministicPrivacyGuard(sourceAppBundleId: app)",
            finalized_input,
        )
        finalizer_before_queue = finalized_input[: finalized_input.index("ragImeForegroundContextQueue.async")]
        self.assertNotIn("enforceRagImeFastPrivacyGuard()", finalizer_before_queue)
        self.assertNotIn('ragImePrivacyDisposition == "allowed"', finalizer_before_queue)
        self.assertLess(
            finalized_input.index("ragImeFinalizedFieldContextFromInputClient()"),
            finalized_input.index("ragImeForegroundContextQueue.async"),
        )
        self.assertIn("ragImeSelectedTextProvider.sensitiveFieldStatus(", finalized_input)
        self.assertIn("ragImeForegroundContextResolver.captureFromAccessibility(", finalized_input)
        self.assertIn("DispatchQueue.main.async", finalized_input)
        self.assertIn('source: "input_segment_finalize"', finalized_input)
        self.assertIn('privacyStatus.privacyDisposition == "allowed"', finalized_input)
        self.assertIn("allowedByTextInputClient", finalized_input)
        self.assertIn("fastPrivacyStatusAtFinalize", finalized_input)
        self.assertLess(
            finalized_input.index("ragImeForegroundContextQueue.async"),
            finalized_input.index("ragImeSelectedTextProvider.sensitiveFieldStatus("),
        )
        self.assertLess(
            finalized_input.index("ragImeForegroundContextResolver.captureFromAccessibility("),
            finalized_input.index("DispatchQueue.main.async"),
        )

        prewarm = section(
            "func scheduleRagImePrivacyPrewarm(",
            "func resolveRagImeForegroundContextAndSend(",
        )
        self.assertIn("ragImeForegroundContextQueue.async", prewarm)
        self.assertIn("let provider = ragImeSelectedTextProvider", prewarm)
        self.assertIn("provider.sensitiveFieldStatus(", prewarm)

        controller_start = patch_text.index("diff --git a/sources/SquirrelInputController.swift")
        controller_text = patch_text[controller_start:]
        full_status_calls = [
            line
            for line in controller_text.splitlines()
            if ".sensitiveFieldStatus(" in line
        ]
        self.assertCountEqual(
            full_status_calls,
            [
                next(line for line in prewarm.splitlines() if "provider.sensitiveFieldStatus(" in line),
                next(line for line in probe.splitlines() if "ragImeSelectedTextProvider.sensitiveFieldStatus(" in line),
                next(line for line in finalized_input.splitlines() if "ragImeSelectedTextProvider.sensitiveFieldStatus(" in line),
            ],
        )
        for full_ax_entrypoint in (
            "ragImeSelectedTextProvider.captureForegroundTextForSidecar(",
            "ragImeSelectedTextProvider.captureSelectedTextForActiveRag(",
            "ragImeForegroundContextResolver.captureFromAccessibility(",
        ):
            controller_calls = [
                line for line in controller_text.splitlines() if full_ax_entrypoint in line
            ]
            probe_calls = [line for line in probe.splitlines() if full_ax_entrypoint in line]
            active_context_calls = [
                line for line in active_context.splitlines() if full_ax_entrypoint in line
            ]
            finalized_input_calls = [
                line for line in finalized_input.splitlines() if full_ax_entrypoint in line
            ]
            self.assertCountEqual(
                controller_calls,
                [*probe_calls, *active_context_calls, *finalized_input_calls],
                f"{full_ax_entrypoint} must only be invoked by a dedicated background AX queue",
            )

        apply_probe_start = patch_text.index("func applyRagImeForegroundPrivacyProbe")
        apply_probe_end = patch_text.index("func finishRagImeForegroundContextCapture(", apply_probe_start)
        apply_probe = patch_text[apply_probe_start:apply_probe_end]
        self.assertIn("effectivePrivacyStatus.reason.isEmpty", apply_probe)
        self.assertIn("effectivePrivacyStatus.isSensitive", apply_probe)
        self.assertIn("failRagImeForegroundContextCapture(", apply_probe)
        self.assertIn("privacy_probe_unknown", apply_probe)

        timeout = section(
            "func probeRagImeForegroundPrivacyAndContext(",
            "func finishRagImeForegroundContextCapture(",
        )
        self.assertIn("ragImeForegroundCaptureTimeoutMs", timeout)
        self.assertIn("privacy_probe_timeout", timeout)
        self.assertIn("scheduleRagImeForegroundPrivacyRetry(", timeout)
        self.assertIn("failRagImeForegroundContextCapture(", timeout)
        self.assertNotIn("sendRagImeSidecarRequest(", timeout)

    def test_stale_accessibility_snapshot_falls_back_to_fresh_commit_ledger(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")
        panel_text = (
            root / "squirrel-patches" / "sources" / "RagImeAssistantPanelController.swift"
        ).read_text(encoding="utf-8")

        finish_start = patch_text.index("func finishRagImeForegroundContextCapture(")
        finish_end = patch_text.index("func liveRagImeCommitLedgerFallback(", finish_start)
        finish = patch_text[finish_start:finish_end]
        self.assertIn('reason: "commit_text_not_observed"', finish)
        self.assertIn('snapshot.source != "ime_commit_ledger"', finish)
        self.assertIn("let fallback = liveRagImeCommitLedgerFallback(for: request)", finish)
        self.assertIn('traceRagImeFrontendEvent("foreground_context_commit_ledger_fallback"', finish)
        self.assertIn('captureReason: "same_app_commit_ledger_after_commit_text_not_observed"', finish)
        self.assertLess(
            finish.index("captureAttempt == 0"),
            finish.index('snapshot.source != "ime_commit_ledger"'),
        )

        fallback_start = patch_text.index("func liveRagImeCommitLedgerFallback(")
        fallback_end = patch_text.index("func failRagImeForegroundContextCapture(", fallback_start)
        fallback = patch_text[fallback_start:fallback_end]
        for guard in (
            'snapshot.source == "ime_commit_ledger"',
            "commitTextStillMatches",
            "snapshot.captureEpoch == request.inputGeneration",
            "snapshot.sourceAppBundleId == request.frontAppBundleId",
            "snapshot.sourceAppBundleId == currentApp",
            "snapshot.inputSourceId == request.inputSourceId",
            'snapshot.inputSourceId == (SquirrelInstaller.currentInputSourceID() ?? "")',
            "ageMs <= maximumAgeMs",
        ):
            self.assertIn(guard, fallback)
        self.assertIn("maximumAgeMs: Int = 2_000", fallback)

        resolve_start = patch_text.index("func resolveRagImeForegroundContextAndSend(")
        resolve_end = patch_text.index("func probeRagImeForegroundPrivacyAndContext(", resolve_start)
        resolve = patch_text[resolve_start:resolve_end]
        self.assertIn("fastSensitiveFieldStatus", resolve)
        self.assertIn("ragImePrivacyDisposition != \"sensitive\"", resolve)
        self.assertIn("probeRagImeForegroundPrivacyAndContext(", resolve)
        self.assertNotIn("liveRagImeCommitLedgerFallback", resolve)
        self.assertNotIn("foreground_context_commit_ledger_direct", resolve)
        self.assertNotIn('privacyDisposition: "allowed"', resolve)
        self.assertLess(resolve.index("fastSensitiveFieldStatus"), resolve.index("probeRagImeForegroundPrivacyAndContext("))

        probe_start = patch_text.index("func probeRagImeForegroundPrivacyAndContext(")
        probe_end = patch_text.index("func finishRagImeForegroundContextCapture(", probe_start)
        probe = patch_text[probe_start:probe_end]
        self.assertLess(
            probe.index("ragImeSelectedTextProvider.sensitiveFieldStatus("),
            probe.index("liveRagImeCommitLedgerFallback(for: allowedRequest)"),
        )

        fail_start = patch_text.index("func failRagImeForegroundContextCapture(")
        fail_end = patch_text.index("func sendRagImeSidecarRequest(", fail_start)
        fail = patch_text[fail_start:fail_end]
        self.assertIn("ragImeAssistantOverlayController.dismissPendingPrediction(", fail)
        self.assertIn("snapshotId: request.panelSessionId", fail)
        self.assertIn("func dismissPendingPrediction(snapshotId: String, reason: String)", panel_text)
        self.assertIn('private var pendingSnapshotId = ""', panel_text)
        self.assertIn("if pendingSnapshotId == snapshotId", panel_text)
        self.assertIn("pendingUpdate?.cancel()", panel_text)
        self.assertIn("guard currentState == .pendingPrediction", panel_text)
        self.assertIn("currentPayload?.snapshotId == snapshotId", panel_text)

    def test_privacy_probe_uses_lease_and_retries_transient_ax_focus_gaps(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")
        schema_text = (root / "rag_ime" / "contracts" / "json" / "rime-suggest-request.v1.json").read_text(
            encoding="utf-8"
        )

        provider_start = patch_text.index("struct RagImeSensitiveFieldStatus")
        provider_end = patch_text.index("diff --git a/sources/RagImeSidecarClient.swift", provider_start)
        provider = patch_text[provider_start:provider_end]
        self.assertIn("var isTransientUnknown: Bool", provider)
        self.assertIn('"privacy_unknown_focused_element_missing"', provider)
        self.assertIn("AXUIElementCreateApplication(frontmostApp.processIdentifier)", provider)
        self.assertIn('path: "application"', provider)
        self.assertIn('path: "system_wide_fallback"', provider)
        self.assertLess(
            provider.index("AXUIElementCreateApplication(frontmostApp.processIdentifier)"),
            provider.index("AXUIElementCreateSystemWide()"),
        )

        controller_start = patch_text.index("diff --git a/sources/SquirrelInputController.swift")
        controller = patch_text[controller_start:]
        self.assertIn("ragImePrivacyProbeMaximumAttempts: Int = 5", controller)
        self.assertIn("ragImePrivacyProbeRetryDelaysMs: [Int] = [30, 90, 240, 600]", controller)
        self.assertIn("scheduleRagImeForegroundPrivacyRetry(", controller)
        self.assertIn("privacyStatus.isTransientUnknown", controller)
        self.assertIn('traceRagImePrivacyDiagnostic("privacy_probe_retry_scheduled"', controller)

        prewarm_start = controller.index("func scheduleRagImePrivacyPrewarm(")
        prewarm_end = controller.index("func resolveRagImeForegroundContextAndSend(", prewarm_start)
        prewarm = controller[prewarm_start:prewarm_end]
        self.assertIn("ragImeForegroundContextQueue.async", prewarm)
        self.assertIn("sensitiveFieldStatus(", prewarm)
        self.assertIn('traceRagImePrivacyDiagnostic("privacy_prewarm_completed"', prewarm)
        self.assertNotIn("issueRagImePrivacyLease", prewarm)
        self.assertNotIn("sendRagImeSidecarRequest", prewarm)
        self.assertNotIn("applyRagImePrivacyStatus", prewarm)
        self.assertGreaterEqual(controller.count('scheduleRagImePrivacyPrewarm(reason:'), 2)

        apply_status_start = controller.index("func applyRagImePrivacyStatus(")
        apply_status_end = controller.index("func clearRagImeSensitiveRuntimeState()", apply_status_start)
        apply_status = controller[apply_status_start:apply_status_end]
        unknown_start = apply_status.index('if disposition == "unknown"')
        unknown_body = apply_status[unknown_start:]
        self.assertIn('ragImePrivacyDisposition = "unknown"', unknown_body)
        self.assertNotIn("clearRagImeSensitiveRuntimeState()", unknown_body)

        pending_start = controller.index("func showRagImeImmediatePostCommitPending(")
        pending_end = controller.index("func showRagImeAssistantOverlayFeedback(", pending_start)
        pending = controller[pending_start:pending_end]
        self.assertIn("fastSensitiveFieldStatus", pending)
        self.assertIn('ragImePrivacyDisposition != "sensitive"', pending)
        self.assertNotIn('ragImePrivacyDisposition == "allowed"', pending)

        for field in ("privacyLeaseId", "privacyLeaseEpoch", "privacyFocusEpoch"):
            self.assertIn(field, controller)
            self.assertIn(f'"{field}"', schema_text)
        self.assertIn("issueRagImePrivacyLease(for: request", controller)
        self.assertGreaterEqual(controller.count("hasValidRagImePrivacyLease(for: request)"), 3)
        self.assertIn('statusText: "当前输入框暂时无法安全读取"', controller)

    def test_privacy_diagnostics_are_always_text_free(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")
        start = patch_text.index("func traceRagImePrivacyDiagnostic(")
        end = patch_text.index("func invalidateRagImePrivacyLease(", start)
        body = patch_text[start:end]
        self.assertIn('safeFields["traceIncludesText"] = false', body)
        self.assertIn('NSLog("RAG-IME privacy %@", json)', body)
        for forbidden in ("rawInput", "preedit", "committedContext", "selectedText", "semanticQuery"):
            self.assertNotIn(f'"{forbidden}"', body)

    def test_transaction_invalidation_cancels_commit_burst_for_every_reason(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")

        invalidate_start = patch_text.index("func invalidateRagImeForegroundTransaction(")
        invalidate_end = patch_text.index("func ragImeDisplaySessionStillLive", invalidate_start)
        invalidate = patch_text[invalidate_start:invalidate_end]
        front_app_branch = invalidate.index('if reason == "front_app_changed"')
        for token in (
            "ragImeCommitBurstWorkItem?.cancel()",
            "ragImeCommitBurstWorkItem = nil",
            "ragImeCommitBurstGeneration += 1",
            "ragImeCommitBurstDeltaChars = 0",
            "ragImeCommitBurstTexts = []",
        ):
            self.assertIn(token, invalidate)
            self.assertLess(invalidate.index(token), front_app_branch)
        self.assertGreater(invalidate.index('ragImeCommittedContext = ""'), front_app_branch)

        deactivate_start = patch_text.index("override func deactivateServer(_ sender: Any!)")
        deactivate_end = patch_text.index("override func hidePalettes()", deactivate_start)
        deactivate = patch_text[deactivate_start:deactivate_end]
        self.assertLess(
            deactivate.index("commitComposition(sender)"),
            deactivate.index('invalidateRagImeForegroundTransaction(reason: "deactivate_server")'),
        )

    def test_active_rag_terminal_lifecycle_fails_closed_across_focus_and_mouse_routes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")
        controller_text = (
            root / "squirrel-patches" / "sources" / "RagImeAssistantPanelController.swift"
        ).read_text(encoding="utf-8")

        invalidate_start = patch_text.index("func invalidateRagImeForegroundTransaction(")
        invalidate_end = patch_text.index("func ragImeDisplaySessionStillLive", invalidate_start)
        invalidate = patch_text[invalidate_start:invalidate_end]
        self.assertIn("clearRagImeDisplayCandidates(dismissReason: reason)", invalidate)

        clear_start = patch_text.index("func clearRagImeDisplayCandidates(")
        clear_end = patch_text.index("func recordRagImeSideCandidateCommit(", clear_start)
        clear = patch_text[clear_start:clear_end]
        self.assertIn(
            'func clearRagImeDisplayCandidates(dismissReason: String = "clear_display_candidates")',
            clear,
        )
        self.assertIn("dismissRagImeAssistantOverlay(reason: dismissReason)", clear)

        keep_start = controller_text.index("private func shouldKeepExplicitSurface(")
        keep_end = controller_text.index("private func showMoreMenu(", keep_start)
        keep = controller_text[keep_start:keep_end]
        self.assertIn('"clear_display_candidates"', keep)
        self.assertNotIn('"deactivate_server"', keep)
        self.assertNotIn('"front_app_changed"', keep)

        commit_start = patch_text.index("func commitRagImeAssistantCandidate(")
        commit_end = patch_text.index("func replaceRagImeAssistantSelection(", commit_start)
        commit = patch_text[commit_start:commit_end]
        liveness_guard = commit.index("guard canSelectRagImeOverlayCandidate(candidate) else {")
        first_action = commit.index('if candidate.selectionAction == "start_agent_deep_search_from_context"')
        self.assertLess(liveness_guard, first_action)
        self.assertIn('"reason": "commit_candidate_transaction_mismatch"', commit)
        self.assertIn('dismissRagImeAssistantOverlay(reason: "stale_overlay_candidate")', commit)

        apply_start = patch_text.index("func applyRagImeActiveRagResponse(")
        apply_end = patch_text.index("func scheduleRagImeActiveRagStatusPoll(", apply_start)
        apply_response = patch_text[apply_start:apply_end]
        blocked_branch = apply_response.index('if response.status == "blocked" {')
        normal_anchor_guard = apply_response.index(
            "guard response.selectedTextHash == request.selectedTextHash"
        )
        self.assertLess(blocked_branch, normal_anchor_guard)
        self.assertIn(
            "guard ragImeActiveRagSessionIsLive(request, generation: generation) else { return }",
            apply_response,
        )
        self.assertIn('"active_rag_blocked_terminal_handled"', apply_response)
        self.assertIn('cancelRagImeActiveRagSession(reason: "active_rag_blocked")', apply_response)
        self.assertIn("showRagImeActiveRagBlockedFeedback(request: request)", apply_response)
        self.assertNotIn('dismissRagImeAssistantOverlay(reason: "active_rag_blocked")', apply_response)
        blocked_feedback_start = patch_text.index("func showRagImeActiveRagBlockedFeedback(")
        blocked_feedback_end = patch_text.index(
            "func scheduleRagImeActiveRagStatusPoll(",
            blocked_feedback_start,
        )
        blocked_feedback = patch_text[blocked_feedback_start:blocked_feedback_end]
        self.assertIn('uiMode: "active_rag_blocked"', blocked_feedback)
        self.assertIn('statusText: "当前内容可能包含敏感信息，未发送"', blocked_feedback)
        self.assertIn('traceEvent: "assistant_overlay_active_rag_blocked"', blocked_feedback)

    def test_terminal_sidecar_paths_target_pending_overlay_by_panel_session(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")

        send_start = patch_text.index("func sendRagImeSidecarRequest(")
        send_end = patch_text.index("func applyRagImeSidecarResponse(", send_start)
        send = patch_text[send_start:send_end]
        for reason in (
            "sidecar_send_guard_rejected",
            "sidecar_stale_before_send",
            "sidecar_transport_failed",
        ):
            self.assertIn(f'reason: "{reason}"', send)
        self.assertGreaterEqual(send.count("snapshotId: request.panelSessionId"), 3)
        self.assertGreaterEqual(send.count("keepLastFingerprint: false"), 3)

        apply_start = patch_text.index("func applyRagImeSidecarResponse(")
        apply_end = patch_text.index("func dropRagImeSidecarResponse(", apply_start)
        apply_response = patch_text[apply_start:apply_end]
        self.assertIn('reason: "sidecar_response_privacy_rejected"', apply_response)
        self.assertIn("snapshotId: request.panelSessionId", apply_response)
        self.assertIn('dropRagImeSidecarResponse("session_missing"', apply_response)

        drop_start = patch_text.index("func dropRagImeSidecarResponse(")
        drop_end = patch_text.index("func completeRagImeSidecarRequest(", drop_start)
        drop = patch_text[drop_start:drop_end]
        self.assertIn("dismissPendingPrediction(", drop)
        self.assertIn("snapshotId: request.panelSessionId", drop)
        self.assertIn('reason: "sidecar_response_\\(reason)"', drop)

    def test_post_accept_backspace_feedback_is_suffix_guarded_and_source_isolated(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")

        self.assertIn('endpoint("candidate-edit-feedback", base: sidecarURL)', patch_text)
        self.assertIn("recordRagImeAcceptedEditFeedbackIfNeeded()", patch_text)
        self.assertIn("guard !ragImeNativeCompositionIsActive() else { return }", patch_text)
        self.assertIn("Date().timeIntervalSince(ragImeAcceptedEditAt) <= 10", patch_text)
        self.assertIn("guard currentContext.hasSuffix(ragImeAcceptedEditText)", patch_text)
        self.assertIn('sourceType: "rime"', patch_text)
        self.assertIn('"accepted_candidate_backspace_feedback_recorded"', patch_text)
        self.assertIn('"traceIncludesText": false', patch_text)

    def test_sensitive_app_policy_guards_capture_send_and_trace(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")
        prepare_script = (root / "scripts" / "prepare_squirrel_workspace.sh").read_text(encoding="utf-8")
        build_script = (root / "scripts" / "build_patched_squirrel.sh").read_text(encoding="utf-8")

        fast_status_start = patch_text.index("func fastSensitiveFieldStatus(sourceAppBundleId: String = \"\")")
        status_start = patch_text.index("func sensitiveFieldStatus(sourceAppBundleId: String = \"\")")
        fast_status_body = patch_text[fast_status_start:status_start]
        status_body = patch_text[status_start : status_start + 5000]
        self.assertIn("sensitiveApplicationReason", fast_status_body)
        self.assertNotIn("AXIsProcessTrusted", fast_status_body)
        self.assertIn("AXIsProcessTrusted", status_body)
        self.assertIn("fastSensitiveFieldStatus(sourceAppBundleId: sourceAppBundleId)", patch_text)
        self.assertIn("ragImeSelectedTextProvider.sensitiveFieldStatus(", patch_text)
        self.assertIn("sourceAppBundleId: currentApp", patch_text)
        self.assertIn('guard !enforceRagImeFastPrivacyGuard(), ragImePrivacyDisposition == "allowed" else { return false }', patch_text)
        self.assertIn("guard !enforceRagImeFastPrivacyGuard() else { return }", patch_text)

        trace_start = patch_text.index("func traceRagImeFrontendEvent(_ event: String, fields: [String: Any])")
        trace_body = patch_text[trace_start : trace_start + 1400]
        self.assertLess(
            trace_body.index("guard !ragImeSensitiveFieldActive || sensitiveSafeEvents.contains(event) else { return }"),
            trace_body.index("ragImeSanitizedTraceFields"),
        )
        for event in (
            "sensitive_field_suppressed",
            "sensitive_field_exited",
            "sensitive_native_learning_transaction_discarded",
        ):
            self.assertIn(f'"{event}"', trace_body)
        for script_text in (prepare_script, build_script):
            self.assertIn("privacy_unknown_ax_not_trusted", script_text)
            self.assertIn('"sensitive_application_bundle"', script_text)
            self.assertIn(
                '"guard !ragImeSensitiveFieldActive || sensitiveSafeEvents.contains(event) else { return }"',
                script_text,
            )
            self.assertIn('"RAG_IME_SENSITIVE_APP_BUNDLE_IDS"', script_text)
            self.assertIn('"discardRagImeSensitiveNativeLearningTransaction"', script_text)
            self.assertIn('"rimeAPI.process_key(session, Int32(XK_BackSpace), 0)"', script_text)
            self.assertIn('"guard !isComposing else { return false }"', script_text)

    def test_assistant_overlay_single_three_and_four_candidate_geometry_is_stable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")
        overlay_sources = root / "squirrel-patches" / "sources"
        card_text = (overlay_sources / "RagImeSuggestionCardView.swift").read_text(encoding="utf-8")
        controller_text = (overlay_sources / "RagImeAssistantPanelController.swift").read_text(encoding="utf-8")
        row_height_match = re.search(r"static let rowHeight: CGFloat = ([0-9.]+)", card_text)
        action_height_match = re.search(r"static let actionHeight: CGFloat = ([0-9.]+)", card_text)

        self.assertIsNotNone(row_height_match)
        self.assertIsNotNone(action_height_match)
        row_height = float(row_height_match.group(1))
        action_height = float(action_height_match.group(1))
        self.assertEqual(row_height * min(3, 1), 40)
        self.assertEqual(row_height * min(3, 3), 120)
        self.assertEqual(row_height * min(4, 4), 160)
        self.assertEqual(row_height * min(3, 1) + action_height, 80)
        self.assertEqual(row_height * min(3, 3) + action_height, 160)
        self.assertEqual(row_height * min(4, 4) + action_height, 200)
        self.assertIn("visibleCount - index - 1", card_text)
        self.assertNotIn("candidates.count - index - 1", card_text)
        self.assertIn("payload.snapshotId == renderedSnapshotId", controller_text)
        self.assertIn("stableIds == renderedStableIds", controller_text)
        self.assertIn("same_snapshot_stable_ids", controller_text)
        self.assertIn("RagImeSuggestionCardView.actionPredictionWidth", controller_text)
        self.assertNotIn("RagImeSuggestionCardView.preferredPredictionWidth", controller_text)
        self.assertIn('resultHeader.stringValue = streaming ? "正在接收内容" : "生成结果"', card_text)
        self.assertIn('NSTextField(labelWithString: "Tab 插入")', card_text)
        self.assertIn('resultShortcutLabel.toolTip = "结果就绪后按 Tab 插入"', card_text)
        self.assertIn("isStreamingResult = streaming", card_text)
        self.assertIn("if streaming {\n        stopButton.isHidden = false", card_text)
        self.assertIn("[resultShortcutPlate, resultShortcutLabel, closeButton, insertButton, retryButton, moreButton]", card_text)
        self.assertIn('configureActionButton(closeButton, action: #selector(close), symbol: "xmark"', card_text)
        self.assertIn("static let minimumExplicitResultHeight: CGFloat = 176", card_text)
        self.assertIn("static let maximumExplicitResultHeight: CGFloat = 300", card_text)
        self.assertIn("static let explicitResultChromeHeight: CGFloat = 112", card_text)
        self.assertIn("static let explicitResultBodyBottom: CGFloat = 48", card_text)
        self.assertIn("static let explicitResultDiagnosticBottomInset: CGFloat = 54", card_text)
        self.assertIn("static func explicitResultHeight", card_text)
        minimum_result_height = 176
        result_body_top = 48 + (minimum_result_height - 112)
        diagnostic_bottom = minimum_result_height - 54
        self.assertGreaterEqual(diagnostic_bottom - result_body_top, 10)
        self.assertNotIn('"DS ·', card_text)
        self.assertNotIn('"DeepSeek ', card_text)
        self.assertNotIn('"MiniMind ', card_text)
        build_script = (root / "scripts" / "build_patched_squirrel.sh").read_text(encoding="utf-8")
        self.assertIn("CompanionStates", build_script)
        self.assertIn("sourceLabel.isHidden = true", (overlay_sources / "RagImeSuggestionRowView.swift").read_text(encoding="utf-8"))
        self.assertIn('tab: "accept_top_when_ready"', patch_text)
        self.assertIn('statusText: "继续联想"', patch_text)
        self.assertIn('statusText: "这次没有合适建议"', patch_text)
        self.assertIn('"暂未完成，可以重试"', patch_text)
        self.assertIn('"暂未完成，可以重试"', card_text)
        self.assertNotIn('"生成失败，请重试"', card_text)
        self.assertIn('"assistant_overlay_no_result_feedback"', patch_text)
        self.assertIn("let assistantOverlayHasCandidates", patch_text)
        self.assertIn("!assistantOverlayHasCandidates", patch_text)
        self.assertIn("A ready completion must win over a still-pending secondary lane", patch_text)
        self.assertIn(
            "let expiresAfterMs = pendingWithoutResult && !isNoResultFeedback",
            patch_text,
        )
        self.assertIn("? max(12_000, boundedMs)", patch_text)
        self.assertNotIn("hasRealCandidate ? max(12_000, boundedMs)", patch_text)
        self.assertIn("scheduleRagImePostCommitContinuation(committedText: insertText, sourceCandidate: candidate)", patch_text)
        self.assertNotIn(
            ") { [weak self] _ in\n+      self?.scheduleRagImePostCommitContinuation",
            patch_text,
        )

    def test_squirrel_patch_contains_active_rag_selected_text_provider(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")

        self.assertIn("RagImeSelectedTextProvider.swift", patch_text)
        self.assertIn("RagImeSelectedTextProvider.swift in Sources", patch_text)
        self.assertIn("struct RagImeActiveRagRequest: Codable", patch_text)
        self.assertIn("struct RagImeActiveRagResponse: Codable", patch_text)
        self.assertIn("func captureSelectedTextForActiveRag", patch_text)
        self.assertIn("allowAccessibility: Bool = true", patch_text)
        self.assertIn("allowClipboardFallback: Bool = true", patch_text)
        self.assertIn("captureSelectedTextViaAccessibility", patch_text)
        self.assertIn("kAXFocusedUIElementAttribute", patch_text)
        self.assertIn("kAXSelectedTextAttribute", patch_text)
        self.assertIn("captureSelectedTextViaClipboardFallbackPreservingPasteboard", patch_text)
        self.assertIn("NSPasteboard.general", patch_text)
        self.assertIn("restorePasteboardItems", patch_text)
        self.assertIn("clipboard_fallback_restores_clipboard_contract", patch_text)
        self.assertIn("ragImeActiveRagAccessibilityCaptureEnabled()", patch_text)
        self.assertIn("ragImeActiveRagClipboardFallbackEnabled()", patch_text)
        self.assertIn('RAG_IME_ACTIVE_RAG_CAPTURE_ACCESSIBILITY', patch_text)
        self.assertIn('RAG_IME_ACTIVE_RAG_CAPTURE_CLIPBOARD_FALLBACK', patch_text)

    def test_squirrel_patch_declares_active_rag_shortcut_and_endpoint(self) -> None:
        root = Path(__file__).resolve().parents[1]
        patch_text = (root / "squirrel-patches" / "0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")
        card_text = (root / "squirrel-patches" / "sources" / "RagImeSuggestionCardView.swift").read_text(
            encoding="utf-8"
        )

        self.assertIn("matchesRagImeActiveRagShortcut(keyCode, modifiers: modifiers)", patch_text)
        self.assertIn('UserDefaults.standard.string(forKey: "RagImeActiveRagShortcut")', patch_text)
        self.assertIn('ProcessInfo.processInfo.environment["RAG_IME_ACTIVE_RAG_SHORTCUT"]', patch_text)
        self.assertIn("} else if modifiers.contains(.shift)", patch_text)
        self.assertIn('return raw.isEmpty ? "ctrl+."', patch_text)
        self.assertIn("ragImeShortcutKeyMatches", patch_text)
        self.assertIn('case ".", "period", "dot": return [47]', patch_text)
        self.assertIn('case "enter": return [36, 76]', patch_text)
        self.assertIn('case "r": return [15]', patch_text)
        self.assertIn("startRagImeActiveRagAssistFromShortcut", patch_text)
        self.assertIn("startRagImeActiveRagAssistFromSelection", patch_text)
        self.assertIn("startRagImeActiveRagAssistFromContext", patch_text)
        self.assertIn("active_rag_shortcut_context_fallback", patch_text)
        self.assertIn("func startRagImeActiveRagAssistFromContext(", patch_text)
        self.assertIn("visualContext: RagImeVisualContext? = nil", patch_text)
        self.assertIn('candidate.selectionAction == "start_active_rag_from_context"', patch_text)
        self.assertIn('candidate.selectionAction == "start_visual_rag_from_context"', patch_text)
        self.assertIn("ragImeDisplayCandidateIsShortcutOnlyAction", patch_text)
        self.assertIn('numberKeys: "pass_through"', patch_text)
        self.assertIn('"requiresExplicitSelection": .bool(true)', patch_text)
        self.assertIn('selectionKey: nil', patch_text)
        self.assertIn('candidateOrdinal: 0', patch_text)
        self.assertIn('"active_rag_shortcut_action_button_route"', patch_text)
        self.assertIn("active_rag_context_button_triggered", patch_text)
        self.assertIn("let imkForeground = ragImeForegroundContextResolver.captureFromTextInputClient", patch_text)
        self.assertIn("struct RagImeWindowContextSnapshot: Codable", patch_text)
        self.assertIn("func captureWindowContext(", patch_text)
        self.assertIn("AXUIElementCopyMultipleAttributeValues(", patch_text)
        self.assertIn("AXUIElementCopyAttributeValue(element, attribute, &value)", patch_text)
        self.assertIn("kAXVisibleChildrenAttribute", patch_text)
        self.assertIn('"AXChildrenInNavigationOrder"', patch_text)
        self.assertIn("kAXContentsAttribute", patch_text)
        self.assertIn("func childElements(_ values: [String: Any])", patch_text)
        self.assertIn("kAXVisibleCharacterRangeAttribute", patch_text)
        self.assertIn('captureMode: isTerminalSurface', patch_text)
        self.assertIn('"terminal_visible_range"', patch_text)
        self.assertIn("if !isTerminalSurface", patch_text)
        self.assertIn('isFocused || role == "AXTextArea" || role == "AXTextField"', patch_text)
        self.assertIn("String(rawValue.suffix(4_000))", patch_text)
        self.assertIn("[REDACTED]", patch_text)
        self.assertIn("DispatchQueue.main.async(execute: closeUI)", patch_text)
        self.assertIn("let provider = ragImeSelectedTextProvider", patch_text)
        self.assertIn("let status = provider.sensitiveFieldStatus(", patch_text)
        self.assertIn('captureMode: isTerminalSurface', patch_text)
        self.assertIn(': "accessibility_semantics"', patch_text)
        self.assertIn('reason: "private_browsing_window"', patch_text)
        self.assertIn(
            "NSWorkspace.shared.frontmostApplication?.bundleIdentifier == sourceAppBundleId",
            patch_text,
        )
        self.assertIn("windowContext: RagImeWindowContextSnapshot?", patch_text)
        self.assertIn("windowContext: windowContext", patch_text)
        self.assertIn('"usesScreenCapture": false', patch_text)
        self.assertIn('applicationSemantics["source"]', patch_text)
        self.assertIn("zed_workspace_state", patch_text)
        self.assertIn('appendSection("窗口语义"', patch_text)
        self.assertIn("let useClientContext = clientContext.count > capturedBefore.count", patch_text)
        self.assertIn("context.isEmpty ? semanticQuery : context", patch_text)
        self.assertIn("postActiveRagStatus", patch_text)
        self.assertIn("TimeInterval(request.latencyBudgetMs) / 1_000 + 0.8", patch_text)
        self.assertIn("timeoutOverride: requestTimeout", patch_text)
        self.assertIn("active_rag_status_poll_scheduled", patch_text)
        self.assertIn("response.pollAfterMs", patch_text)
        self.assertIn('candidate.metadata["activeRagNoSuggestion"]', patch_text)
        self.assertIn("} else if activeStatusNoSuggestion {", patch_text)
        self.assertNotIn(
            'activeStatusText?.contains("没有合适") == true',
            patch_text,
        )
        self.assertIn("latencyBudgetMs: 30000", patch_text)
        self.assertIn("let expiresAfterMs: Int?", patch_text)
        self.assertIn("expiresAfterMs: response.expiresAfterMs", patch_text)
        self.assertIn(
            "expiresAfterMs: min(max(request.latencyBudgetMs, 2_000), 8_000)",
            patch_text,
        )
        self.assertIn("guard attempt <= 1200 else { return }", patch_text)
        self.assertIn("postActiveRagStart", patch_text)
        self.assertIn('endpoint("active-rag/start"', patch_text)
        self.assertIn('endpoint("agent/deep-search"', patch_text)
        self.assertIn('candidate.selectionAction == "start_agent_deep_search_from_context"', patch_text)
        self.assertIn("startRagImeAgentDeepSearchFromContext", patch_text)
        self.assertIn("RagImeAgentDeepSearchRequest", patch_text)
        self.assertIn('"--paw-session=" + sessionId', patch_text)
        self.assertIn('"--paw-capture", "--paw-source-app=" + currentApp', patch_text)
        self.assertNotIn("com.rag-ime.control.open-agent", patch_text)
        self.assertIn('"reason": "transport_or_runtime_error"', patch_text)
        self.assertIn("deepSearchButton", card_text)
        self.assertNotIn("visualGenerateButton", card_text)
        self.assertIn("layoutActionBar", card_text)
        self.assertIn('title: "生成"', card_text)
        self.assertNotIn('title: "看图"', card_text)
        self.assertIn('title: "深度"', card_text)
        self.assertNotIn("startVisualActiveRag", card_text)
        self.assertIn("startAgentDeepSearch", card_text)
        self.assertIn("static func failure(sessionId: String", patch_text)
        self.assertIn('status: "error"', patch_text)
        self.assertIn('candidateCount: 0', patch_text)
        self.assertIn('candidates: []', patch_text)
        self.assertNotIn("fallbackCandidateText", patch_text)
        self.assertNotIn("frontend_fallback", patch_text)
        self.assertNotIn("重试DeepSeek生成", patch_text)
        self.assertIn("active_rag_thinking_displayed", patch_text)
        self.assertIn("showRagImeActiveRagThinkingPlaceholder(request: request)", patch_text)
        self.assertIn("assistant_overlay_active_rag_thinking", patch_text)
        self.assertIn('"selectedTextChars": request.selectedTextChars', patch_text)
        self.assertIn('"frontAppBundleId": request.frontAppBundleId', patch_text)
        self.assertIn('"traceIncludesText": false', patch_text)
        self.assertIn("active_rag_ready_displayed", patch_text)
        self.assertIn("active_rag_response_dropped_stale", patch_text)
        self.assertIn("active_rag_candidate_committed", patch_text)

        quick_start = patch_text.index("func startRagImeActiveRagAssistFromContext(")
        quick_end = patch_text.index("func startRagImeVisualAssistFromContext(", quick_start)
        quick_path = patch_text[quick_start:quick_end]
        self.assertIn("ragImeForegroundContextQueue.async", quick_path)
        self.assertIn("captureWindowContext(", quick_path)
        self.assertIn("let accessibilityForeground = imkForeground == nil", quick_path)
        self.assertIn("imkPrivacyFallbackAllowed", quick_path)
        self.assertIn("enforceRagImeDeterministicPrivacyGuard()", quick_path)
        self.assertIn('"usesScreenCapture": visualContext != nil', quick_path)
        self.assertNotIn("captureRagImeCurrentAppVisualContext(", quick_path)
        self.assertNotIn("CGWindowListCreateImage(", quick_path)
        self.assertNotIn("CGRequestScreenCaptureAccess(", quick_path)

    def test_build_script_dry_run_reports_resolved_commands(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-squirrel-build-") as tmp:
            workdir = Path(tmp) / "squirrel"
            result = subprocess.run(
                ["bash", str(root / "scripts" / "build_patched_squirrel.sh"), "build"],
                cwd=root,
                env={
                    **os.environ,
                    "RAG_IME_SQUIRREL_BUILD_DRY_RUN": "1",
                    "RAG_IME_SQUIRREL_WORKDIR": str(workdir),
                    "RAG_IME_SQUIRREL_SCHEME": "SquirrelTest",
                    "RAG_IME_SQUIRREL_CONFIGURATION": "Debug",
                },
                check=True,
                text=True,
                capture_output=True,
            )
        self.assertIn(f"workdir={workdir}", result.stdout)
        self.assertIn("scheme=SquirrelTest", result.stdout)
        self.assertIn("configuration=Debug", result.stdout)
        self.assertIn("list_command=", result.stdout)
        self.assertIn("build_command=", result.stdout)
        self.assertIn("CODE_SIGNING_ALLOWED=NO", result.stdout)
        self.assertIn("INFOPLIST_KEY_LSRegisterProhibited=YES", result.stdout)
        self.assertIn("enable_pref_repair=0", result.stdout)
        self.assertIn("auto_select=0", result.stdout)
        self.assertIn(
            "connection_name=im.rime.inputmethod.Squirrel_Connection",
            result.stdout,
        )

    def test_build_script_rejects_stale_generated_repo_root_before_xcode(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-squirrel-stale-root-") as tmp:
            tmp_path = Path(tmp)
            workdir = _fake_patched_squirrel_workdir(tmp_path)
            config_path = workdir / "rag-ime.squirrel.custom.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    f"repo_root: {root}",
                    f"repo_root: {tmp_path / 'old-product-worktree'}",
                ),
                encoding="utf-8",
            )
            xcode_log = tmp_path / "xcodebuild-called"
            fake_xcodebuild = tmp_path / "xcodebuild"
            fake_xcodebuild.write_text(
                "#!/usr/bin/env bash\n"
                f"touch {xcode_log!s}\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_xcodebuild.chmod(0o755)

            result = subprocess.run(
                ["bash", str(root / "scripts" / "build_patched_squirrel.sh"), "build"],
                cwd=root,
                env={
                    **os.environ,
                    "RAG_IME_XCODEBUILD": str(fake_xcodebuild),
                    "RAG_IME_SQUIRREL_WORKDIR": str(workdir),
                    "RAG_IME_SQUIRREL_DERIVED_DATA": str(tmp_path / "derived-data"),
                },
                text=True,
                capture_output=True,
            )
            xcode_was_called = xcode_log.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("patched Squirrel config repo_root is stale", result.stderr)
        self.assertIn(str(root), result.stderr)
        self.assertFalse(xcode_was_called)

    def test_branded_install_pref_repair_and_auto_select_are_explicit(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "build_patched_squirrel.sh").read_text(encoding="utf-8")

        self.assertIn("RAG_IME_SQUIRREL_ENABLE_PREF_REPAIR", source)
        self.assertIn("RAG_IME_SQUIRREL_AUTO_SELECT", source)
        self.assertIn('if bool_true "$ENABLE_PREF_REPAIR"', source)
        self.assertIn('bool_true "$AUTO_SELECT"', source)
        self.assertIn("preference repair is disabled", source)
        self.assertIn("input-source selection is deferred", source)
        self.assertIn("audit_canonical_squirrel_bundles.py", source)
        self.assertIn("--preinstall", source)
        self.assertIn('pkill -f "$TARGET_APP/Contents/MacOS/Squirrel"', source)
        self.assertNotIn("killall imklaunchagent TextInputMenuAgent TextInputSwitcher", source)
        self.assertIn("handoff_active_squirrel_input_source", source)
        self.assertIn("atomically installed final signed Squirrel.app", source)
        self.assertIn("INSTALL_LOCK_DIR", source)
        self.assertIn("RAG_IME_SQUIRREL_ALLOW_SOURCE_ROOT_CHANGE", source)
        self.assertIn('open -a "$TARGET_APP"', source)
        self.assertIn("keeping the registered replacement installed", source)
        self.assertLess(
            source.index("INSTALL_COMPLETE=1", source.index("install_squirrel_app()")),
            source.index("if ! restore_input_source_after_install"),
        )
        self.assertNotIn(
            'if ! restore_input_source_after_install && [[ -d "$INSTALL_PREVIOUS_APP" ]]',
            source,
        )
        self.assertIn("traceRagImeProcessEvent", source)
        self.assertIn('elif [[ "$PREINSTALL" == "auto" ]]; then', source)
        self.assertIn("if ! deps_ready; then", source)
        self.assertNotIn('[[ "$PREINSTALL" == "auto" && ! deps_ready ]]', source)

    def test_build_script_uses_xcodebuild_list_and_build(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-squirrel-build-") as tmp:
            tmp_path = Path(tmp)
            workdir = _fake_patched_squirrel_workdir(tmp_path)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            sdk = tmp_path / "CLT-SDK"
            headers = _fake_squirrel_sdk(sdk)
            fake_log = tmp_path / "xcodebuild.log"
            fake_xcodebuild = fake_bin / "xcodebuild"
            fake_xcodebuild.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "printf '%s\\n' \"$*\" >> \"$FAKE_XCODEBUILD_LOG\"",
                        "if [[ \"$1\" == \"-version\" ]]; then",
                        "  echo 'Xcode 16.0'",
                        "  echo 'Build version 16A000'",
                        "  exit 0",
                        "fi",
                        "if [[ \"$1\" == \"-project\" && \"$3\" == \"-list\" ]]; then",
                        "  echo 'Targets:'",
                        "  echo '  Squirrel'",
                        "  exit 0",
                        "fi",
                        "if [[ \"$1\" == \"-project\" && \"$*\" == *'CODE_SIGNING_ALLOWED=NO'* && \"$*\" == *' build'* ]]; then",
                        "  echo 'Build succeeded'",
                        "  exit 0",
                        "fi",
                        "exit 1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_xcodebuild.chmod(0o755)

            result = subprocess.run(
                ["bash", str(root / "scripts" / "build_patched_squirrel.sh")],
                cwd=root,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "FAKE_XCODEBUILD_LOG": str(fake_log),
                    "RAG_IME_XCODEBUILD": str(fake_xcodebuild),
                    "RAG_IME_CLT_SDK_ROOT": str(sdk),
                    "DEVELOPER_DIR": "",
                    "RAG_IME_SQUIRREL_WORKDIR": str(workdir),
                    "RAG_IME_SQUIRREL_DERIVED_DATA": str(tmp_path / "derived-data"),
                },
                check=True,
                text=True,
                capture_output=True,
            )

            xcodebuild_log = fake_log.read_text(encoding="utf-8")
        self.assertIn("[OK] xcodebuild can inspect patched Squirrel project", result.stdout)
        self.assertIn("[OK] xcodebuild build succeeded", result.stdout)
        self.assertIn("-list", xcodebuild_log)
        self.assertIn("-scheme Squirrel", xcodebuild_log)
        self.assertIn("CODE_SIGNING_ALLOWED=NO", xcodebuild_log)
        self.assertIn("INFOPLIST_KEY_LSRegisterProhibited=YES", xcodebuild_log)
        self.assertIn("SYSTEM_HEADER_SEARCH_PATHS=", xcodebuild_log)
        self.assertIn("Tk.framework/Headers", xcodebuild_log)
        self.assertIn(f"SYSTEM_HEADER_SEARCH_PATHS={headers}", xcodebuild_log)

    def test_dependency_preinstall_overrides_install_name_tool_for_spaceful_developer_dir(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-squirrel-xcode-space-") as tmp:
            tmp_path = Path(tmp)
            workdir = _fake_patched_squirrel_workdir(tmp_path)
            developer_dir = tmp_path / "Xcode Beta" / "Contents" / "Developer"
            (developer_dir / "usr" / "bin").mkdir(parents=True)
            make = shutil.which("make")
            if sys.platform == "darwin":
                # /usr/bin/make can be an Xcode shim. Resolve the real tool
                # before applying the intentionally synthetic Developer path.
                make = subprocess.run(
                    ["xcrun", "--find", "make"],
                    env={key: value for key, value in os.environ.items() if key != "DEVELOPER_DIR"},
                    check=True, text=True, capture_output=True,
                ).stdout.strip()
            self.assertIsNotNone(make, "this contract requires real make")
            self.assertTrue(Path(make).is_file())
            (developer_dir / "usr" / "bin" / "make").symlink_to(make)
            (developer_dir / "usr" / "bin" / "gnumake").symlink_to(make)
            install_name_tool = tmp_path / "install_name_tool"
            install_name_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            install_name_tool.chmod(0o755)
            effective_install_name_tool = tmp_path / "effective-install-name-tool.txt"
            (workdir / "action-install-test.mk").write_text(
                "INSTALL_NAME_TOOL = /tmp/Xcode Beta/Contents/Developer/usr/bin/install_name_tool\n"
                "verify:\n"
                f"\t@printf '%s' \"$(INSTALL_NAME_TOOL)\" > \"{effective_install_name_tool}\"\n",
                encoding="utf-8",
            )
            action_install = workdir / "action-install.sh"
            action_install.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "make -s -f action-install-test.mk verify\n"
                "mkdir -p lib Frameworks/Sparkle.framework bin\n"
                "touch lib/librime.1.dylib bin/rime-install\n",
                encoding="utf-8",
            )
            action_install.chmod(0o755)
            fake_xcodebuild = tmp_path / "xcodebuild"
            fake_xcodebuild.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == '-version' ]]; then echo 'Xcode 27.0'; exit 0; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_xcodebuild.chmod(0o755)

            result = subprocess.run(
                ["bash", str(root / "scripts" / "build_patched_squirrel.sh"), "build"],
                cwd=root,
                env={
                    **os.environ,
                    "DEVELOPER_DIR": str(developer_dir),
                    "MAKEFLAGS": "",
                    "RAG_IME_INSTALL_NAME_TOOL": str(install_name_tool),
                    "PATH": f"{developer_dir / 'usr' / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
                    "RAG_IME_XCODEBUILD": str(fake_xcodebuild),
                    "RAG_IME_SQUIRREL_PREINSTALL": "1",
                    "RAG_IME_SQUIRREL_WORKDIR": str(workdir),
                    "RAG_IME_SQUIRREL_DERIVED_DATA": str(tmp_path / "derived-data"),
                },
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(
                effective_install_name_tool.exists(),
                f"action-install verification log missing\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            observed = effective_install_name_tool.read_text(encoding="utf-8")
            self.assertEqual(observed, str(install_name_tool))

    def test_dependency_preinstall_rejects_whitespace_install_name_tool(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for whitespace in (" ", "\t"):
            with self.subTest(whitespace=repr(whitespace)), tempfile.TemporaryDirectory(prefix="rag-ime-tool-path-") as tmp:
                tmp_path = Path(tmp)
                workdir = _fake_patched_squirrel_workdir(tmp_path)
                tool = tmp_path / f"tool{whitespace}directory" / "install_name_tool"
                tool.parent.mkdir()
                tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                tool.chmod(0o755)
                called = tmp_path / "action-install-called"
                action = workdir / "action-install.sh"
                action.write_text(
                    "#!/bin/sh\nprintf called > " + shlex.quote(str(called)) + "\nexit 17\n",
                    encoding="utf-8",
                )
                action.chmod(0o755)
                xcodebuild = tmp_path / "xcodebuild"
                xcodebuild.write_text(
                    '#!/bin/sh\nif [ "$1" = "-version" ]; then echo "Xcode 16.0"; fi\nexit 0\n',
                    encoding="utf-8",
                )
                xcodebuild.chmod(0o755)
                result = subprocess.run(
                    ["bash", str(root / "scripts/build_patched_squirrel.sh"), "build"],
                    cwd=root,
                    env={
                        **{key: value for key, value in os.environ.items() if not key.startswith("RAG_IME_")},
                        "DEVELOPER_DIR": str(tmp_path / "Xcode Beta"),
                        "RAG_IME_XCODEBUILD": str(xcodebuild),
                        "RAG_IME_INSTALL_NAME_TOOL": str(tool),
                        "RAG_IME_SQUIRREL_WORKDIR": str(workdir),
                        "RAG_IME_SQUIRREL_DERIVED_DATA": str(tmp_path / "derived-data"),
                        "RAG_IME_SQUIRREL_PREINSTALL": "1",
                    },
                    text=True, capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("install_name_tool path without whitespace", result.stderr)
                self.assertFalse(called.exists(), "invalid MAKEFLAGS path reached dependency installation")

    def test_spaceful_developer_dir_prefers_no_space_x11_header_root(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-squirrel-sdk-") as tmp:
            tmp_path = Path(tmp)
            developer_dir = tmp_path / "Xcode Beta" / "Contents" / "Developer"
            selected_sdk = developer_dir / "Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
            selected_headers = _fake_squirrel_sdk(selected_sdk)
            clt_sdk = tmp_path / "CLT-SDK"
            clt_headers = _fake_squirrel_sdk(clt_sdk)
            result = subprocess.run(
                ["bash", str(root / "scripts" / "build_patched_squirrel.sh"), "build"],
                cwd=root,
                env={
                    **os.environ,
                    "DEVELOPER_DIR": str(developer_dir),
                    "RAG_IME_CLT_SDK_ROOT": str(clt_sdk),
                    "RAG_IME_SQUIRREL_BUILD_DRY_RUN": "1",
                },
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertTrue((selected_headers / "X11/keysym.h").is_file())
            self.assertIn(f"SYSTEM_HEADER_SEARCH_PATHS={clt_headers}", result.stdout)
            self.assertNotIn(f"SYSTEM_HEADER_SEARCH_PATHS={developer_dir}", result.stdout)

    def test_install_action_copies_app_and_installs_rag_config(self) -> None:
        self._check_install_action(native_tools=False)

    @unittest.skipUnless(sys.platform == "darwin", "requires native macOS archive, plist and image tools")
    def test_install_action_with_native_macos_tools(self) -> None:
        # Retain the native tool integration previously implicit in the install
        # fixture, while all four portable orchestration cases run on Linux too.
        self._check_install_action(native_tools=True)

    def _check_install_action(self, *, native_tools: bool) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-squirrel-install-") as tmp:
            tmp_path = Path(tmp)
            workdir = _fake_patched_squirrel_workdir(tmp_path)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            tool_env = _fake_squirrel_system_tools(tmp_path, fake_bin, native_tools=native_tools)
            fake_xcodebuild = fake_bin / "xcodebuild"
            fake_xcodebuild.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "if [[ \"$1\" == \"-version\" ]]; then",
                        "  echo 'Xcode 16.0'",
                        "  echo 'Build version 16A000'",
                        "  exit 0",
                        "fi",
                        "if [[ \"$1\" == \"-project\" && \"$3\" == \"-list\" ]]; then",
                        "  echo 'Targets:'",
                        "  echo '  Squirrel'",
                        "  exit 0",
                        "fi",
                        "derived=''",
                        "previous=''",
                        "for arg in \"$@\"; do",
                        "  if [[ \"$previous\" == \"-derivedDataPath\" ]]; then derived=\"$arg\"; fi",
                        "  previous=\"$arg\"",
                        "done",
                        "if [[ \"$*\" == *'CODE_SIGNING_ALLOWED=NO'* && \"$*\" == *' build'* ]]; then",
                        "  mkdir -p \"$derived/Build/Products/Release/Squirrel.app/Contents/MacOS\"",
                        "  mkdir -p \"$derived/Build/Products/Release/Squirrel.app/Contents/SharedSupport\"",
                        '  cp "$FAKE_SQUIRREL_INFO" "$derived/Build/Products/Release/Squirrel.app/Contents/Info.plist"',
                        "  printf 'schema_list:\\n  - schema: luna_pinyin\\n' > \"$derived/Build/Products/Release/Squirrel.app/Contents/SharedSupport/default.yaml\"",
                        "  printf 'schema:\\n  schema_id: luna_pinyin\\n' > \"$derived/Build/Products/Release/Squirrel.app/Contents/SharedSupport/luna_pinyin.schema.yaml\"",
                        "  printf -- '---\\nname: luna_pinyin\\n...\\n' > \"$derived/Build/Products/Release/Squirrel.app/Contents/SharedSupport/luna_pinyin.dict.yaml\"",
                        "  cat > \"$derived/Build/Products/Release/Squirrel.app/Contents/MacOS/Squirrel\" <<'SH'",
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        "if [[ \"${1:-}\" == \"--build\" ]]; then",
                        "  mkdir -p build",
                        "  printf 'default\\n' > build/default.yaml",
                        "  printf 'schema\\n' > build/luna_pinyin.schema.yaml",
                        "  printf 'table\\n' > build/luna_pinyin.table.bin",
                        "  exit 0",
                        "fi",
                        "if [[ \"${1:-}\" == \"--reload\" ]]; then exit 0; fi",
                        "exit 0",
                        "SH",
                        "  chmod +x \"$derived/Build/Products/Release/Squirrel.app/Contents/MacOS/Squirrel\"",
                        "  exit 0",
                        "fi",
                        "exit 1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_xcodebuild.chmod(0o755)
            install_dir = tmp_path / "Input Methods"
            system_install_dir = tmp_path / "System Input Methods"
            system_install_dir.mkdir()
            quarantine_root = tmp_path / "disabled-input-method-backups"
            (install_dir / "RAG-IME.app" / "Contents" / "MacOS").mkdir(parents=True)
            (install_dir / "RagIme.app" / "Contents" / "MacOS").mkdir(parents=True)
            for alias in ("RAG-IME.app", "RagIme.app"):
                (install_dir / alias / "Contents/sentinel.txt").write_text(alias, encoding="utf-8")
            rime_dir = tmp_path / "Rime"
            install_env = {
                **{key: value for key, value in os.environ.items() if not key.startswith("RAG_IME_")},
                **tool_env,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "RAG_IME_SQUIRREL_WORKDIR": str(workdir),
                "RAG_IME_SQUIRREL_DERIVED_DATA": str(tmp_path / "derived-data"),
                "RAG_IME_SQUIRREL_INSTALL_DIR": str(install_dir),
                "RAG_IME_SQUIRREL_SYSTEM_INPUT_METHOD_DIR": str(system_install_dir),
                "RAG_IME_SQUIRREL_CANONICAL_QUARANTINE_DIR": str(quarantine_root),
                "RAG_IME_RIME_USER_DIR": str(rime_dir),
                "RAG_IME_SQUIRREL_SKIP_CODESIGN": "1",
                "RAG_IME_SQUIRREL_SKIP_POSTINSTALL": "1",
                "RAG_IME_SQUIRREL_DISPLAY_NAME": "Squirrel",
            }
            result = subprocess.run(
                ["bash", str(root / "scripts" / "build_patched_squirrel.sh"), "install"],
                cwd=root,
                env=install_env,
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            self.assertIn("[OK] atomically installed final signed Squirrel.app", result.stdout)
            self.assertIn("archived noncanonical input method app", result.stdout)
            self.assertIn("canonical input method bundle enforced", result.stdout)
            self.assertTrue((install_dir / "Squirrel.app" / "Contents" / "MacOS" / "Squirrel").is_file())
            menu_icon = install_dir / "Squirrel.app" / "Contents" / "Resources" / "RagImeInputMenuIcon.png"
            self.assertTrue(menu_icon.is_file())
            resources = menu_icon.parent
            icns = (resources / "RagImeIcon.icns").read_bytes()
            if native_tools:
                menu_icon_info = subprocess.run(
                    ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(menu_icon)],
                    env=install_env, check=True, text=True, capture_output=True,
                ).stdout
                self.assertIn("pixelWidth: 18", menu_icon_info)
                self.assertIn("pixelHeight: 18", menu_icon_info)
                self.assertEqual(icns[:4], b"icns")
                self.assertEqual(int.from_bytes(icns[4:8], "big"), len(icns))
            else:
                self.assertEqual(_validate_fixture_png(menu_icon.read_bytes()), (18, 18))
                self.assertEqual(_validate_fixture_icns(icns), {
                    b"icp4": (16, 16), b"icp5": (32, 32), b"icp6": (64, 64),
                    b"ic07": (128, 128), b"ic08": (256, 256), b"ic09": (512, 512),
                    b"ic10": (1024, 1024),
                })
            info = plistlib.loads((resources.parent / "Info.plist").read_bytes())
            self.assertEqual(info["CFBundleIdentifier"], "im.rime.inputmethod.Squirrel")
            self.assertEqual(info["CFBundleExecutable"], "Squirrel")
            self.assertEqual(info["FixturePreserved"], {"value": "keep-me"})
            self.assertEqual(info["CFBundleIconFile"], "RagImeIcon")
            self.assertNotIn("LSRegisterProhibited", info)
            product_info = tmp_path / "derived-data/Build/Products/Release/Squirrel.app/Contents/Info.plist"
            self.assertIs(plistlib.loads(product_info.read_bytes())["LSRegisterProhibited"], True)
            marker = json.loads((resources / "rag-ime-build-marker.json").read_text())
            self.assertEqual(marker["repoRoot"], str(root))
            archives = list(quarantine_root.rglob("*.zip"))
            self.assertEqual(len(archives), 2)
            preserved = {}
            for archive_path in archives:
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertIsNone(archive.testzip())
                    sentinels = [name for name in archive.namelist() if name.endswith("/Contents/sentinel.txt")]
                    self.assertEqual(len(sentinels), 1)
                    preserved[sentinels[0]] = archive.read(sentinels[0]).decode()
            self.assertEqual(preserved, {f"{alias}/Contents/sentinel.txt": alias for alias in ("RAG-IME.app", "RagIme.app")})
            if not native_tools:
                tool_calls = [json.loads(line) for line in Path(tool_env["FAKE_SYSTEM_TOOL_LOG"]).read_text().splitlines()]
                self.assertEqual(sum(call["tool"] == "ditto" for call in tool_calls), 2)
                self.assertEqual(sum(call["tool"] == "sips" for call in tool_calls), 12)
                self.assertEqual(sum(call["tool"] == "iconutil" for call in tool_calls), 1)
            self.assertFalse((install_dir / "RAG-IME.app").exists())
            self.assertFalse((install_dir / "RagIme.app").exists())
            self.assertIn(str(quarantine_root), result.stdout)
            self.assertTrue((workdir / "librime" / "include" / "rime_api_stdbool.h").is_file())
            config = (rime_dir / "squirrel.custom.yaml").read_text(encoding="utf-8")
            self.assertIn('"rag_ime/enabled": true', config)
            self.assertIn('"rag_ime/sidecar_url": "http://127.0.0.1:19866/api"', config)
            self.assertIn('"rag_ime/frontend_trace": true', config)
            self.assertIn("skipped Squirrel user-data bootstrap and postinstall", result.stderr)
            self.assertFalse((rime_dir / "build" / "luna_pinyin.table.bin").exists())

            target_executable = install_dir / "Squirrel.app" / "Contents" / "MacOS" / "Squirrel"
            target_executable.write_text("preflight-must-not-replace\n", encoding="utf-8")
            _write_same_bundle_app(system_install_dir / "LegacySquirrel.app")
            conflict = subprocess.run(
                ["bash", str(root / "scripts" / "build_patched_squirrel.sh"), "install"],
                cwd=root,
                env=install_env,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("[ERROR] preinstall same-bundle conflict", conflict.stderr)
            self.assertEqual(target_executable.read_text(encoding="utf-8"), "preflight-must-not-replace\n")

    def test_build_script_fails_clearly_when_workdir_is_missing(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-squirrel-build-") as tmp:
            result = subprocess.run(
                ["bash", str(root / "scripts" / "build_patched_squirrel.sh")],
                cwd=root,
                env={
                    **os.environ,
                    "RAG_IME_XCODEBUILD": sys.executable,
                    "RAG_IME_SQUIRREL_WORKDIR": str(Path(tmp) / "missing-squirrel"),
                },
                text=True,
                capture_output=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Squirrel workdir not prepared", result.stderr)
        self.assertIn("Run scripts/prepare_squirrel_workspace.sh first", result.stderr)


def _fake_patched_squirrel_workdir(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    workdir = tmp_path / "squirrel"
    workdir.mkdir()
    subprocess.run(["git", "init"], cwd=workdir, check=True, capture_output=True, text=True)
    (workdir / "sources").mkdir()
    (workdir / "sources" / "RagImeSidecarModels.swift").write_text(
        "struct RagImeWindowContextSnapshot {}\n"
        "struct Request { let privacyDisposition: String }\n",
        encoding="utf-8",
    )
    (workdir / "sources" / "RagImeSidecarClient.swift").write_text(
        '// client endpoint("rime-rank-feedback", base: sidecarURL)\n'
        '// request.privacyDisposition == "allowed"\n',
        encoding="utf-8",
    )
    (workdir / "sources" / "RagImeSelectedTextProvider.swift").write_text(
        (
            "import ApplicationServices\n"
            "final class RagImeForegroundContextResolver {}\n"
            "final class RagImeSelectedTextProvider { "
            "func captureWindowContext() { _ = AXUIElementCopyMultipleAttributeValues }; "
            "func captureForegroundTextForSidecar() { "
            "_ = kAXSelectedTextRangeAttribute; "
            "_ = kAXStringForRangeParameterizedAttribute; "
            "_ = kAXValueAttribute } }\n"
            '// privacy_unknown_app_bundle_missing; isSensitive: false, reason: "privacy_unknown_ax_not_trusted"\n'
            "// privacy_unknown_focused_element_missing privacy_unknown_metadata_read_failed\n"
            "// privacy_unknown_text_field_metadata_missing sensitive_application_bundle\n"
            "// RAG_IME_SENSITIVE_APP_BUNDLE_IDS RagImeSensitiveAppBundleTokens\n"
            '// AXManualAccessibility AXUIElementGetPid focused element app identity\n'
        ),
        encoding="utf-8",
    )
    (workdir / "sources" / "SquirrelInputController.swift").write_text(
        (
            'final class SquirrelInputController { func ragImePanelForcesHorizontalLayout() -> Bool { false }; '
            'func ragImePanelUsesSideDisplay() -> Bool { false }; '
            '// ragImePrivacyDisposition == "allowed"; privacyDisposition: ragImePrivacyDisposition; '
            'func traceRagImeFrontendEvent() {}; '
            '// guard ragImeSidecarClient?.frontendTrace == true || isCaptureDeliveryEvent else { return }; '
            '// guard !ragImeSensitiveFieldActive || sensitiveSafeEvents.contains(event) else { return }; '
            '// ragImePrepareFrontendTraceLog .posixPermissions: 0o600 appendingPathExtension("1"); '
            '// let contextAnchor = ragImeStableTextHash(context); '
            '// queryAnchor: contextAnchor displayAnchor: contextAnchor; '
            'func traceRagImePanelTextLayout() { _ = "panel_text_layout" }; '
            'func traceSidecarRequestScheduled() { _ = "sidecar_request_scheduled" }; '
            'func traceSidecarEmptyResponseCleared() { _ = "sidecar_empty_response_cleared" }; '
            'func traceV2() { _ = "rag-ime.foreground-trace.v2" }; '
            'func forceSideCandidates() { let forceSideCandidates = rawInput.isEmpty && preedit.isEmpty; _ = "forceSideCandidates: forceSideCandidates" }; '
            'func compositionAISuppressed() { _ = "composition_ai_suppressed" }; '
            'func traceRimeComposition() { _ = "rime_composition_started"; _ = "rime_composition_candidates_visible" }; '
            'func controlCenterMenu() { _ = "打开澄控制中心..."; _ = "配置由控制中心管理"; '
            '_ = "重新启动后台服务"; _ = "诊断与修复..."; _ = "Applications/RagImeControl.app"; _ = "--paw-session="; _ = "--paw-capture" }; '
            'func suppressPostCommitOverlay() { _ = "RAG_IME_ASSISTANT_OVERLAY_AUTO_PENDING"; _ = "assistant_overlay_local_placeholder_suppressed" }; '
            'func foregroundSnapshot() { _ = "ragImeSelectedTextProvider.captureForegroundTextForSidecar" }; '
            'func queuedForegroundSnapshot() { _ = "ragImeForegroundContextResolver.captureFromAccessibility(" }; '
            'func probeRagImeForegroundPrivacyAndContext() { _ = "privacy_probe_timeout" }; '
            '// active_rag_window_context_capture_scheduled "usesScreenCapture": false; '
            'func foregroundCaptureResolved() { _ = "foreground_context_capture_resolved" }; '
            'func foregroundCaptureFailed() { _ = "foreground_context_capture_failed" }; '
            'func sideCandidateFeedbackRecorded() { _ = "side_candidate_feedback_recorded" }; '
            'func nativeRimeFeedback() { _ = "ragImeNativeSelectionSnapshot"; '
            '_ = "native_rime_rank_feedback_recorded"; '
            'guard !enforceRagImeFastPrivacyGuard() else { return } }; '
            '// discardRagImeSensitiveNativeLearningTransaction; '
            '// guard rimeAPI.get_status(session, &status) else { return false }; '
            '// guard !isComposing else { return false }; return !backspaceHandled; '
            '// rimeAPI.process_key(session, Int32(XK_BackSpace), 0); '
            '// "forwardedToClient": false; '
            'func assistantOverlayCandidateVisible() { _ = "assistant_overlay_candidate_visible" }; '
            'func activeRagLocalThinkingTrace(request: Request) { _ = "assistant_overlay_active_rag_thinking"; traceRagImeFrontendEvent("active_rag_thinking_displayed", fields: ["selectedTextHash": request.selectedTextHash, "selectedTextChars": request.selectedTextChars, "frontAppBundleId": currentApp, "traceIncludesText": false]) }; '
            'func activeRagPollTrace(request: Request) { traceRagImeFrontendEvent("active_rag_status_poll_scheduled", fields: ["selectedTextHash": request.selectedTextHash, "selectedTextChars": request.selectedTextChars, "frontAppBundleId": request.frontAppBundleId, "traceIncludesText": false]) }; '
            'func ragImeDisplayComment() { _ = "candidate.sourceType == \\"model\\"" } }\n'
        ),
        encoding="utf-8",
    )
    (workdir / "sources" / "SquirrelPanel.swift").write_text(
        "final class SquirrelPanel { var ragImePanelLinear: Bool { true }; var ragImePanelUsesSideDisplay: Bool { false }; func nonGlassBackground() { _ = \"return NSView()\" }; func candidateSeparator(before index: Int) -> String { \" \" }; func traceRagImePanelTextLayout() {} }\n",
        encoding="utf-8",
    )
    (workdir / "sources" / "Main.swift").write_text(
        "struct SquirrelApp { static func traceRagImeProcessEvent() {} }\n",
        encoding="utf-8",
    )
    (workdir / "Squirrel.xcodeproj").mkdir()
    (workdir / "Squirrel.xcodeproj" / "project.pbxproj").write_text("// pbxproj\n", encoding="utf-8")
    (workdir / "librime" / "dist" / "include").mkdir(parents=True)
    (workdir / "librime" / "dist" / "include" / "rime_api_stdbool.h").write_text("// rime api\n", encoding="utf-8")
    (workdir / "rag-ime.squirrel.custom.yaml").write_text(
        "\n".join(
            [
                "rag_ime:",
                "  enabled: true",
                "  sidecar_url: http://127.0.0.1:19866/api",
                "  python: /usr/bin/python3",
                f"  repo_root: {root}",
                f"  db_path: {tmp_path / 'rag-ime.sqlite'}",
                "  project: offline-test",
                "  max_visible_candidates: 8",
                "  max_side_candidates: 5",
                "  latency_budget_ms: 300",
                "  debounce_ms: 80",
                "  timeout_ms: 250",
                "  frontend_trace: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return workdir


def _write_same_bundle_app(path: Path) -> None:
    executable = path / "Contents" / "MacOS" / "Squirrel"
    executable.parent.mkdir(parents=True)
    (path / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "im.rime.inputmethod.Squirrel"})
    )
    executable.write_text("legacy\n", encoding="utf-8")


def _fake_squirrel_sdk(sdk: Path) -> Path:
    headers = sdk / "System/Library/Frameworks/Tk.framework/Headers"
    (headers / "X11").mkdir(parents=True)
    (headers / "X11/keysym.h").write_text("/* fixture X11 header */\n", encoding="utf-8")
    return headers


def _fake_squirrel_system_tools(tmp_path: Path, fake_bin: Path, *, native_tools: bool = False) -> dict[str, str]:
    # These are command/artifact contracts, not native rendering or signing.
    from tests.squirrel_system_tools import write_png

    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    adapter = Path(__file__).with_name("squirrel_system_tools.py")
    for name in (() if native_tools else ("ditto", "PlistBuddy", "sips", "iconutil")):
        wrapper = fake_bin / name
        wrapper.write_text(
            "#!/usr/bin/env bash\nexec " + shlex.quote(sys.executable) + " "
            + shlex.quote(str(adapter)) + " " + name + ' "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    # Host input sources and processes are outside this isolated install test.
    controls = {
        "check-input-source": 'echo "selected=false current=com.apple.keylayout.ABC"\n',
        "select-input-source": 'echo "unexpected input-source selection" >&2\nexit 97\n',
        "lsregister": '[[ "$1" == "-u" && "$2" == "$FAKE_SQUIRREL_ROOT/"* ]]\n',
        "pkill": '[[ "$1" == "-f" && "$2" == "$FAKE_SQUIRREL_ROOT/"* ]] || exit 97\nexit 1\n',
        "pgrep": '[[ "$1" == "-f" && "$2" == "$FAKE_SQUIRREL_ROOT/"* ]] || exit 97\nexit 1\n',
    }
    for name, body in controls.items():
        wrapper = fake_bin / name
        wrapper.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
        wrapper.chmod(0o755)
    source = tmp_path / "fixture-source.png"
    write_png(source, 2, 2)
    info = tmp_path / "fixture-Info.plist"
    info.write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "im.rime.inputmethod.Squirrel",
        "CFBundleExecutable": "Squirrel",
        "LSRegisterProhibited": False,
        "FixturePreserved": {"value": "keep-me"},
    }))
    environment = {
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "PYTHON_BIN": sys.executable,
        "RAG_IME_XCODEBUILD": str(fake_bin / "xcodebuild"),
        "RAG_IME_CHECK_INPUT_SOURCE_SCRIPT": str(fake_bin / "check-input-source"),
        "RAG_IME_SELECT_INPUT_SOURCE_SCRIPT": str(fake_bin / "select-input-source"),
        "RAG_IME_LSREGISTER": str(fake_bin / "lsregister"),
        "RAG_IME_SQUIRREL_INSTALL_LOCK_DIR": str(tmp_path / "install.lock"),
        "RAG_IME_SQUIRREL_CUSTOM_CONFIG": str(tmp_path / "Rime/squirrel.custom.yaml"),
        "RAG_IME_RIME_DEFAULT_CUSTOM_CONFIG": str(tmp_path / "Rime/default.custom.yaml"),
        "RAG_IME_SQUIRREL_AUTO_SELECT": "0",
        "FAKE_SYSTEM_TOOL_LOG": str(tmp_path / "system-tools.jsonl"),
        "FAKE_SQUIRREL_ROOT": str(tmp_path),
        "FAKE_SQUIRREL_INFO": str(info),
    }
    if not native_tools:
        environment.update({
            "RAG_IME_DITTO": str(fake_bin / "ditto"),
            "RAG_IME_PLISTBUDDY": str(fake_bin / "PlistBuddy"),
            "RAG_IME_INPUT_MENU_ICON_SOURCE": str(source),
            "RAG_IME_ICON_SOURCE": str(source),
        })
    return environment


def _validate_fixture_png(data: bytes) -> tuple[int, int]:
    # Independent output inspection: do not use the fake encoder's decoder.
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("invalid PNG signature")
    chunks = []
    offset = 8
    while offset < len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + size]
        crc = data[offset + 8 + size:offset + 12 + size]
        if len(crc) != 4 or int.from_bytes(crc, "big") != zlib.crc32(kind + payload):
            raise AssertionError("invalid PNG CRC/framing")
        chunks.append((kind, payload))
        offset += size + 12
    if [kind for kind, _ in chunks] != [b"IHDR", b"IDAT", b"IEND"]:
        raise AssertionError("unexpected fixture PNG chunks")
    width, height, *format_fields = struct.unpack(">IIBBBBB", chunks[0][1])
    if format_fields != [8, 6, 0, 0, 0] or chunks[-1][1]:
        raise AssertionError("unexpected fixture PNG format")
    raw = zlib.decompress(chunks[1][1])
    expected = b"".join(
        b"\0" + bytes(
            component for x in range(width)
            for component in (255 * (x * 2 // width), 255 * (y * 2 // height), 127, 255)
        ) for y in range(height)
    )
    if raw != expected:
        raise AssertionError("resized PNG lost the four fixture color quadrants")
    return width, height


def _validate_fixture_icns(data: bytes) -> dict[bytes, tuple[int, int]]:
    if data[:4] != b"icns" or int.from_bytes(data[4:8], "big") != len(data):
        raise AssertionError("invalid ICNS framing")
    chunks = {}
    offset = 8
    while offset < len(data):
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "big")
        if size < 8 or offset + size > len(data) or kind in chunks:
            raise AssertionError("invalid ICNS chunk")
        chunks[kind] = _validate_fixture_png(data[offset + 8:offset + size])
        offset += size
    return chunks


if __name__ == "__main__":
    unittest.main()
