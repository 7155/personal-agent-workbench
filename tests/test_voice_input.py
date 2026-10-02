from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VoiceInputTests(unittest.TestCase):
    def test_voice_agent_bundle_and_build_contract(self) -> None:
        with (ROOT / "macos/RagImeVoice/Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        build = (ROOT / "scripts/build_voice_input.sh").read_text(encoding="utf-8")
        launch = (ROOT / "scripts/install_voice_input_launch_agent.sh").read_text(encoding="utf-8")

        self.assertEqual(info["CFBundleIdentifier"], "com.rag-ime.voice")
        self.assertTrue(info["LSUIElement"])
        self.assertIn("NSMicrophoneUsageDescription", info)
        for framework in ("AVFoundation", "ApplicationServices", "Carbon", "LocalAuthentication", "Security"):
            self.assertIn(f"-framework {framework}", build)
        self.assertIn("scripts/support/build_app_icon.sh", build)
        self.assertIn("CompanionStates", build)
        self.assertIn("resolve_voice_developer_dir", build)
        self.assertIn("libSwiftMacros.dylib", build)
        self.assertIn("kMDItemCFBundleIdentifier", build)
        self.assertIn('DEVELOPER_DIR="$VOICE_DEVELOPER_DIR" xcrun swiftc', build)
        self.assertIn('designated => identifier "com.rag-ime.voice"', build)
        self.assertIn("forget Accessibility and Microphone grants", build)
        self.assertNotIn("RagImeMac.app", build)
        self.assertIn("RunAtLoad bool true", launch)
        self.assertIn("KeepAlive:SuccessfulExit bool false", launch)
        self.assertLess(launch.index("launchctl enable"), launch.index("launchctl bootstrap"))

    def test_credentials_avoid_repeat_prompts_and_sensitive_fields_are_blocked(self) -> None:
        keychain = (ROOT / "macos/Shared/VoiceKeychainStore.swift").read_text(encoding="utf-8")
        insertion = (ROOT / "macos/RagImeVoice/VoiceTextInsertion.swift").read_text(encoding="utf-8")
        coordinator = (ROOT / "macos/RagImeVoice/VoiceInputCoordinator.swift").read_text(encoding="utf-8")
        configure = (ROOT / "scripts/configure_volcengine_asr.sh").read_text(encoding="utf-8")

        self.assertIn("SecItemCopyMatching", keychain)
        self.assertIn("SecItemUpdate", keychain)
        self.assertIn("VoiceCredentialFileStore.loadCredentials(provider: provider)", keychain)
        self.assertIn('case credentialBundle = "credentials-v1"', keychain)
        self.assertIn("VoiceCredentialMetadataStore.isConfigured(provider)", keychain)
        self.assertIn("voice-credential-status.json", keychain)
        self.assertIn("allowLegacyFallback: Bool = false", keychain)
        self.assertIn("readLegacyCredentials(provider: provider)", keychain)
        self.assertIn("try? writeCredentialBundle(credentials)", keychain)
        self.assertIn("readCredentialBundle(provider: provider)?.isComplete == true", keychain)
        self.assertIn("loadCredentials(allowLegacyFallback: true)", coordinator)
        self.assertIn('"voice-credentials-\\(provider.rawValue).json"', keychain)
        self.assertIn("voice-credentials.json", keychain)
        self.assertIn(".posixPermissions: 0o600", keychain)
        self.assertIn("IsSecureEventInputEnabled", insertion)
        for marker in ("securetextfield", "password", "密码", "口令"):
            self.assertIn(marker, insertion)
        self.assertNotIn("HistoryStore", coordinator)
        self.assertIn("VoiceCommitRecorder.record", coordinator)
        self.assertIn("security add-generic-password", configure)
        self.assertIn('-T "$VOICE_BIN"', configure)
        self.assertIn('-T "$CONTROL_BIN"', configure)

        delegate = (ROOT / "macos/RagImeVoice/VoiceApplicationDelegate.swift").read_text(encoding="utf-8")
        self.assertIn("AXIsProcessTrustedWithOptions", delegate)
        self.assertIn("com.rag-ime.voice.request-accessibility-permission", delegate)
        self.assertLess(delegate.index("startHotkeyWhenTrusted()"), delegate.index("requestAccessibilityPermission()"))
        self.assertIn("Never trigger a TCC prompt", delegate)
        self.assertNotIn("NSStatusItem", delegate)
        self.assertNotIn("NSStatusBar", delegate)

        page = (ROOT / "control-center-web/src/features/voice/index.tsx").read_text(encoding="utf-8")
        self.assertIn("queries.runtime", page)
        self.assertIn("麦克风", page)
        self.assertIn("辅助功能", page)
        self.assertIn("用于将文字写回当前应用", page)
        self.assertNotIn("AXIsProcessTrusted", page)
        self.assertNotIn("AVCaptureDevice.authorizationStatus", page)
        self.assertIn("com.rag-ime.voice.request-microphone-permission", delegate)
        self.assertIn("VoiceAudioRecorder.requestPermission", delegate)

        status = (ROOT / "macos/Shared/VoiceAgentStatus.swift").read_text(encoding="utf-8")
        self.assertIn("rag-ime.voice-agent-status.v4", status)
        self.assertIn("LegacyVoiceAgentStatusV3", status)
        self.assertIn("let interactionSource: String", status)
        self.assertIn("let recognition: VoiceRecognitionContract?", status)
        self.assertIn("semanticSmoothing: true", status)
        self.assertIn("fullResultReplacement: true", status)
        self.assertIn("providerResponseMetadata: true", status)
        self.assertIn("thirdPassRefinement: true", status)
        self.assertIn('UserDefaults.standard.bool(forKey: "thirdPassRefinementEnabled")', status)
        self.assertIn("VoiceThirdPassRefiner.isEnabled", coordinator)
        self.assertIn("voice-agent-status.json", status)
        self.assertIn(".posixPermissions: 0o600", status)
        for marker in (
            "networkState",
            "firstPartialLatencyMs",
            "finalLatencyMs",
            "providerFinalRevisedPartial",
            "pcmFrameCount",
            "droppedPCMFrameCount",
            "partialRevisionCount",
        ):
            self.assertIn(marker, status)
        self.assertIn("页面不会显示已保存的密钥或请求头", page)
        self.assertIn("VoiceAgentStatusStore.write", delegate)

    def test_final_voice_commit_enters_recent_context_without_recording_partials(self) -> None:
        coordinator = (ROOT / "macos/RagImeVoice/VoiceInputCoordinator.swift").read_text(encoding="utf-8")
        recorder = (ROOT / "macos/RagImeVoice/VoiceCommitRecorder.swift").read_text(encoding="utf-8")
        insertion = (ROOT / "macos/RagImeVoice/VoiceTextInsertion.swift").read_text(encoding="utf-8")
        build = (ROOT / "scripts/build_voice_input.sh").read_text(encoding="utf-8")

        partial_start = coordinator.index("case .partial(let text):")
        final_start = coordinator.index("case .final(let text):", partial_start)
        failure_start = coordinator.index("case .failure(let message):", final_start)
        complete_start = coordinator.index("private func completeFinal(")
        complete_end = coordinator.index("@discardableResult", complete_start)
        self.assertNotIn("recordCommittedVoiceTextIfNeeded", coordinator[partial_start:final_start])
        self.assertIn("completeFinal(", coordinator[final_start:failure_start])
        self.assertIn("recordCommittedVoiceTextIfNeeded(finalText)", coordinator[complete_start:complete_end])
        self.assertIn("if interactionSource == .hotkey", coordinator[complete_start:complete_end])
        self.assertIn("partialText == providerFinalText", coordinator)
        self.assertIn("VoiceThirdPassRefiner.refine(", coordinator)
        self.assertIn('case agentComposer = "agent_composer"', coordinator)
        self.assertIn('insertion.appBundleIdentifier == "com.rag-ime.control"', coordinator)
        self.assertIn("appBundleIdentifier", insertion)
        self.assertIn("let referenceContext: String", insertion)
        self.assertIn("contextBeforeCaret(focused, range:", insertion)
        self.assertIn("maximumUTF16Length: 1_600", insertion)
        self.assertIn("http://127.0.0.1:8766/api/commit", recorder)
        self.assertIn("recentContext: insertion.referenceContext", coordinator)
        self.assertIn("let recentContext: String?", recorder)
        self.assertIn("inserted_voice_final_with_ax_context", recorder)
        self.assertIn("ax_context_unavailable", recorder)
        self.assertIn('"voice-input"', recorder)
        self.assertIn('"recent-input"', recorder)
        self.assertIn('source: "voice_final"', recorder)
        self.assertIn('schemaVersion: "rag-ime.input-capture.v2"', recorder)
        self.assertIn('boundaryKind: "voice_final"', recorder)
        self.assertIn('boundaryConfidence: "strong"', recorder)
        self.assertIn("finalCommitted: true", recorder)
        self.assertIn('captureSource: "voice_insertion"', recorder)
        self.assertIn("pending-commits-v2.json", recorder)
        self.assertIn("static func retryPending()", recorder)
        self.assertIn("VoiceCommitRecorder.retryPending()", coordinator)
        self.assertIn("deliverPending(&entries, observingCaptureID: nil)", recorder)
        self.assertIn("maximumEntries = 64", recorder)
        self.assertIn("maximumBytes = 2 * 1024 * 1024", recorder)
        self.assertNotIn("entries.suffix(maximumEntries)", recorder)
        self.assertNotIn("nowMs - $0.queuedAtMs", recorder)
        self.assertIn("guard entries.count <= maximumEntries", recorder)
        self.assertIn("permissions: 0o700", recorder)
        self.assertIn("permissions: 0o600", recorder)
        self.assertIn('"rag-ime.input-capture-receipt.v2"', recorder)
        self.assertLess(recorder.index("try persist(entries)"), recorder.index("let response = try post"))
        self.assertIn('find "$SHARED" "$SRC"', build)

        record_start = coordinator.index("private func recordCommittedVoiceTextIfNeeded")
        record_end = coordinator.index("private func scheduleFinalTimeout", record_start)
        record_body = coordinator[record_start:record_end]
        self.assertIn('voiceCommitCaptureID = "voice:\\(UUID().uuidString.lowercased())"', coordinator)
        self.assertIn("committedVoiceTextRecordPending = true", record_body)
        self.assertIn("case .acknowledged, .queued:", record_body)
        self.assertIn("case .failed:", record_body)
        self.assertIn("语音已输入，但输入历史暂未保存", record_body)
        self.assertLess(
            record_body.index("VoiceCommitRecorder.record("),
            record_body.index("self.committedVoiceTextRecorded = true"),
        )
        failure_start = coordinator.index("case .failure(let message):")
        failure_end = coordinator.index("case .transport(let networkState):", failure_start)
        timeout_start = coordinator.index("private func scheduleFinalTimeout()")
        self.assertNotIn("recordCommittedVoiceTextIfNeeded", coordinator[failure_start:failure_end])
        self.assertNotIn("recordCommittedVoiceTextIfNeeded", coordinator[timeout_start:])
        self.assertIn("临时稿已保留", coordinator)
        self.assertIn("copyToClipboardForRecovery", coordinator)

        delegate = (ROOT / "macos/RagImeVoice/VoiceApplicationDelegate.swift").read_text(encoding="utf-8")
        page = (ROOT / "control-center-web/src/paw-os/apps/PawSessionWorkspace.tsx").read_text(encoding="utf-8")
        for command in ("begin", "finish", "cancel"):
            notification = f"com.rag-ime.voice.agent-composer-{command}"
            self.assertIn(notification, delegate)
            self.assertNotIn(notification, page)

    def test_voice_provider_credentials_and_adapters_are_isolated(self) -> None:
        keychain = (ROOT / "macos/Shared/VoiceKeychainStore.swift").read_text(encoding="utf-8")
        adapters = (ROOT / "macos/Shared/VoiceStreamingASR.swift").read_text(encoding="utf-8")
        coordinator = (ROOT / "macos/RagImeVoice/VoiceInputCoordinator.swift").read_text(encoding="utf-8")
        page = (ROOT / "control-center-web/src/features/voice/index.tsx").read_text(encoding="utf-8")

        self.assertIn("enum VoiceASRProvider", keychain)
        self.assertIn('case nativeStreaming = "native_streaming"', keychain)
        self.assertIn('case realtimeWebSocket = "realtime_websocket"', keychain)
        self.assertIn('"com.rag-ime.voice.\\(provider.rawValue)"', keychain)
        self.assertIn("voice-provider.json", keychain)
        self.assertIn("voice-credentials-\\(provider.rawValue).json", keychain)
        self.assertIn("VoiceStreamingASRFactory.make", coordinator)
        self.assertIn("VolcengineStreamingASRClient", adapters)
        self.assertIn("RealtimeWebSocketASRClient", adapters)
        self.assertIn('"input_audio_buffer.append"', adapters)
        self.assertIn('"input_audio_buffer.commit"', adapters)
        self.assertIn("native_streaming", page)
        self.assertIn("realtime_websocket", page)
        self.assertIn("http_transcription", page)
        self.assertIn("saveVoiceCredentials", page)
        self.assertIn("useVoiceCredentialStatus", page)
        self.assertIn("边说边显示，说完后补充完整文字", page)
        self.assertIn("实时服务", page)
        self.assertIn("上传后转写", page)
        self.assertNotIn("内置流式识别", page)
        self.assertIn('case .nativeStreaming: return "流式 ASR"', keychain)
        self.assertNotIn("providerHandoffAvailable", page)

        metadata_start = keychain.index("static func hasConfiguredCredentialMetadata")
        metadata_end = keychain.index("private static func service", metadata_start)
        metadata_query = keychain[metadata_start:metadata_end]
        self.assertIn("VoiceCredentialMetadataStore.isConfigured", metadata_query)
        self.assertNotIn("SecItemCopyMatching", metadata_query)
        self.assertNotIn("LocalAuthentication", metadata_query)
        self.assertNotIn("kSecReturnData", metadata_query)
        self.assertNotIn("read(", metadata_query)

        legacy_start = keychain.index("private static func readLegacyCredentials")
        legacy_end = keychain.index("static func save(appID", legacy_start)
        default_load_start = keychain.index("static func loadCredentials(")
        default_load_end = keychain.index("private static func readLegacyCredentials", default_load_start)
        self.assertIn("guard allowLegacyFallback", keychain[default_load_start:default_load_end])
        self.assertIn("read(.accessToken", keychain[legacy_start:legacy_end])

        save_start = keychain.index("static func save(\n        provider:")
        save_end = keychain.index("static func saveLocal(appID", save_start)
        save_body = keychain[save_start:save_end]
        self.assertEqual(save_body.count("writeCredentialBundle(credentials)"), 1)
        self.assertNotIn("allowLegacyFallback: true", save_body)
        self.assertNotIn("readLegacyCredentials", save_body)

        token_status_start = keychain.index("static func hasAccessToken(provider:")
        token_status_end = keychain.index("static func hasConfiguredCredentialMetadata", token_status_start)
        token_status_body = keychain[token_status_start:token_status_end]
        self.assertEqual(token_status_body.count("readCredentialBundle(provider: provider)"), 2)
        self.assertNotIn("read(.accessToken", token_status_body)

        coordinator = (ROOT / "macos/RagImeVoice/VoiceInputCoordinator.swift").read_text(encoding="utf-8")
        probe = (ROOT / "macos/RagImeVoice/VoiceASRProbe.swift").read_text(encoding="utf-8")
        self.assertIn("loadCredentials(allowLegacyFallback: true)", coordinator)
        self.assertIn("loadCredentials(allowLegacyFallback: true)", probe)

    def test_middle_mouse_is_default_push_to_talk_and_keyboard_fallbacks_remain_configurable(self) -> None:
        config = (ROOT / "macos/Shared/VoiceHotkeyConfig.swift").read_text(encoding="utf-8")
        hotkey = (ROOT / "macos/RagImeVoice/GlobalVoiceHotkey.swift").read_text(encoding="utf-8")
        coordinator = (ROOT / "macos/RagImeVoice/VoiceInputCoordinator.swift").read_text(encoding="utf-8")
        page = (ROOT / "control-center-web/src/features/voice/index.tsx").read_text(encoding="utf-8")
        configure = (ROOT / "scripts/configure_voice_hotkey.sh").read_text(encoding="utf-8")

        self.assertIn('case middleMouse = "middle_mouse"', config)
        self.assertIn('case rightOption = "right_option"', config)
        self.assertIn('case optionSpace = "option_space"', config)
        self.assertIn("choice: .middleMouse", config)
        self.assertIn("voice-hotkey.json", config)
        self.assertIn(".posixPermissions: 0o600", config)
        self.assertIn("matching: [.otherMouseDown, .otherMouseUp]", hotkey)
        self.assertIn("mouseEventButtonNumber", hotkey)
        self.assertIn("middleMouseButton: Int64 = 2", hotkey)
        self.assertIn("CGEventType.flagsChanged", hotkey)
        self.assertIn("keyCode == 61", hotkey)
        self.assertIn("suppressedReleaseChoice", hotkey)
        self.assertIn("NSEvent.addGlobalMonitorForEvents", hotkey)
        self.assertIn("passiveMiddleMouse", hotkey)
        self.assertIn("if configuration.choice == .middleMouse", hotkey)
        self.assertIn("startPassiveMiddleMouseMonitor()", hotkey)
        self.assertIn("options: .listenOnly", hotkey)
        self.assertNotIn("options: .defaultTap", hotkey)
        self.assertNotIn("return nil", hotkey)
        self.assertIn("hotkey.reloadConfiguration()", coordinator)
        self.assertIn("restartHotkeyMonitor", coordinator)
        self.assertIn("middle_mouse", page)
        self.assertIn("option_space", page)
        self.assertIn("按住说话", page)
        self.assertIn("middle_mouse|right_option|option_space", configure)

        delegate = (ROOT / "macos/RagImeVoice/VoiceApplicationDelegate.swift").read_text(encoding="utf-8")
        self.assertLess(delegate.index("coordinator?.startHotkeyMonitor()"), delegate.index("if AXIsProcessTrusted()"))

        overlay = (ROOT / "macos/RagImeVoice/VoiceOverlay.swift").read_text(encoding="utf-8")
        recorder = (ROOT / "macos/RagImeVoice/VoiceAudioRecorder.swift").read_text(encoding="utf-8")
        companion = (ROOT / "macos/Shared/RagImeCompanionMark.swift").read_text(encoding="utf-8")
        state_assets = ROOT / "macos/Shared/Assets/CompanionStates"
        self.assertIn("RagImeAnimeCompanion", overlay)
        self.assertIn("An original book-shaped companion", companion)
        self.assertIn("Original anime companion artwork", companion)
        anime_companion = companion.split("struct RagImeAnimeCompanion", 1)[1].split(
            "struct RagImeFullBodyCompanion",
            1,
        )[0]
        self.assertIn("bundledImage(named: requestedArtworkName)", anime_companion)
        self.assertIn("artworkName == requestedArtworkName", anime_companion)
        self.assertNotIn("artwork = nil", anime_companion)
        for state in ("Idle", "Listening", "Thinking", "Done", "Warning"):
            self.assertIn(f"RagImeCompanion{state}", companion)
            asset = state_assets / f"RagImeCompanion{state}.png"
            self.assertTrue(asset.is_file())
            self.assertEqual(asset.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertNotIn("Circle()\n                .fill(state.accent.opacity(0.09))", companion)
        self.assertIn("RagImeCompanionState", companion)
        self.assertIn('model.message = "正在听写"', overlay)
        self.assertIn("请关闭后重新开启澄语音", overlay)
        self.assertIn("VoiceLevelWaveform", overlay)
        self.assertIn("levelHistory", overlay)
        self.assertIn("guard now - lastLevelUpdateAt >= 0.08", overlay)
        self.assertIn("let response = level > model.level ? 0.78 : 0.28", overlay)
        self.assertIn("linearGradient", overlay)
        self.assertIn("layer.addFilter(.blur(radius: 2.5))", overlay)
        self.assertIn("let decibels = 20 * log10(rms)", recorder)
        self.assertIn("pow(normalized, 0.68)", recorder)
        self.assertIn("VoiceOverlayMetrics.width", overlay)
        self.assertIn("NSWindow.Level.screenSaver.rawValue + 1", overlay)
        self.assertIn("panel.hidesOnDeactivate = false", overlay)
        self.assertNotIn(".fullScreenAuxiliary, .transient", overlay)
        self.assertIn(".stationary, .ignoresCycle", overlay)
        self.assertIn("panel.isReleasedWhenClosed = false", overlay)
        self.assertGreaterEqual(overlay.count("ensureVisible()"), 4)
        self.assertIn("Color(nsColor: .windowBackgroundColor).opacity(0.98)", overlay)
        self.assertIn("scheduleDismiss(after: 1.4)", overlay)
        self.assertIn("overlay.showRecording()", coordinator)
        self.assertIn("连接信息", page)
        self.assertIn("页面不会显示已保存的密钥或请求头", page)

        with tempfile.TemporaryDirectory(prefix="rag-ime-voice-hotkey-") as directory:
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/configure_voice_hotkey.sh"), "middle_mouse"],
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "HOME": directory},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            path = Path(directory) / "Library/Application Support/RagIme/voice-hotkey.json"
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"schemaVersion": "rag-ime.voice-hotkey.v1", "choice": "middle_mouse"},
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_hotwords_are_explicit_bounded_and_not_derived_from_rime(self) -> None:
        config = (ROOT / "macos/Shared/VoiceHotwordConfig.swift").read_text(encoding="utf-8")
        asr = (ROOT / "macos/Shared/VolcengineStreamingASR.swift").read_text(encoding="utf-8")
        page = (ROOT / "control-center-web/src/features/voice/index.tsx").read_text(encoding="utf-8")
        coordinator = (ROOT / "macos/RagImeVoice/VoiceInputCoordinator.swift").read_text(encoding="utf-8")

        self.assertIn("maxWordCount = 32", config)
        self.assertIn("minCharactersPerWord = 2", config)
        self.assertIn("maxCharactersPerWord = 9", config)
        self.assertIn("voice-hotwords.json", config)
        self.assertIn(".posixPermissions: 0o600", config)
        self.assertIn('request["context"] = context', asr)
        self.assertIn('"enable_ddc": true', asr)
        self.assertIn('"hotwords": effectiveWords.map', config)
        self.assertIn("VoiceHotwordConfigStore.read()", coordinator)
        self.assertIn("allowedTechnicalSeparators", config)
        self.assertIn('".-_+#/&"', config)
        self.assertIn("支持本次专有词的当前实时听写服务会在听写时使用它", page)
        self.assertIn("每行一个词", page)
        self.assertNotIn("rime", config.lower())

        status = (ROOT / "macos/Shared/VoiceAgentStatus.swift").read_text(encoding="utf-8")
        self.assertIn("let hotwordsEnabled: Bool", status)
        self.assertIn("let hotwordCount: Int", status)
        self.assertNotIn("let hotwords:", status)

    def test_pcm_privacy_gate_revalidates_and_cancellation_drops_queued_audio(self) -> None:
        coordinator = (ROOT / "macos/RagImeVoice/VoiceInputCoordinator.swift").read_text(encoding="utf-8")
        insertion = (ROOT / "macos/RagImeVoice/VoiceTextInsertion.swift").read_text(encoding="utf-8")
        policy = (ROOT / "macos/RagImeVoice/VoicePrivacyPolicy.swift").read_text(encoding="utf-8")
        target_policy = (ROOT / "macos/Shared/VoiceInsertionTargetPolicy.swift").read_text(encoding="utf-8")
        asr = (ROOT / "macos/Shared/VolcengineStreamingASR.swift").read_text(encoding="utf-8")

        forward_start = coordinator.index("private func forwardPCMIfSafe")
        forward_end = coordinator.index("private func requireSafeInsertionTarget", forward_start)
        forward = coordinator[forward_start:forward_end]
        self.assertLess(forward.index("try requireSafeInsertionTarget()"), forward.index("client.appendPCM(data)"))
        self.assertGreaterEqual(coordinator.count("try requireSafeInsertionTarget()"), 4)
        self.assertIn("abortForUnsafeTarget(error)", coordinator)

        self.assertIn("func validateForAudioTransmission() throws", insertion)
        self.assertIn("case privacyStateUnknown", insertion)
        self.assertIn("value as? URL", insertion)
        self.assertIn("url.absoluteString", insertion)
        self.assertIn("unreadable metadata is", insertion)
        self.assertNotIn("kAXFocusedAttribute", insertion)
        self.assertNotIn("无法确认当前输入框是否安全", insertion)
        validation_start = insertion.index("func validateForAudioTransmission()")
        validation_end = insertion.index("func apply(_ revision", validation_start)
        self.assertNotIn("validatePrivacy", insertion[validation_start:validation_end])
        self.assertNotIn("privacyUnknownSinceMs", coordinator)
        self.assertIn('"securetextfield", "secure text", "password", "passcode", "密码", "口令"', insertion)
        self.assertIn("AXUIElementGetPid", insertion)
        self.assertIn("VoicePrivacyPolicy.denies", insertion)
        self.assertIn("case .attributeUnsupported, .noValue", insertion)
        self.assertIn("case finalPaste", target_policy)
        self.assertIn('"com.openai.codex"', target_policy)
        self.assertIn("prefersFinalPaste", target_policy)
        self.assertIn("applicationIdentity(for: focused)", insertion)
        self.assertIn("resolveApplication", insertion)
        self.assertIn("guard let focused = focusedElement() else", insertion)
        self.assertIn("AXUIElementCreateApplication(frontmostApplication.processIdentifier)", insertion)
        self.assertIn("guard revision.isFinal, !revision.text.isEmpty else", insertion)
        self.assertIn("pasteFinalText", insertion)
        self.assertIn("copyToClipboardForRecovery", insertion)
        self.assertIn("keyDown.post(tap: .cghidEventTap)", insertion)
        self.assertIn("pasteboard.changeCount == writtenChangeCount", insertion)
        self.assertIn("var supportsClipboardRecovery: Bool", insertion)
        self.assertNotIn("本次语音已停止", insertion)
        self.assertNotIn("无法读取当前输入框", insertion)
        self.assertIn("guard !bundle.isEmpty else { return false }", policy)

        self.assertIn("clipboardFallbackReason", coordinator)
        self.assertIn("insertionError.supportsClipboardRecovery", coordinator)
        self.assertIn("deliverFinalToClipboard", coordinator)
        self.assertIn("preserveTranscriptToClipboard", coordinator)
        self.assertIn("overlay.showClipboardPending(text)", coordinator)
        self.assertIn("!finalDeliveryUsedClipboard", coordinator)

        for marker in ("com.1password", "com.icbc", "org.torproject.torbrowser", "incognito", "无痕"):
            self.assertIn(marker, policy.lower())
        self.assertIn("RAG_IME_VOICE_DENIED_BUNDLE_IDS", policy)

        cancel_start = asr.index("func cancel()")
        cancel_end = asr.index("private func startOnQueue", cancel_start)
        cancel_body = asr[cancel_start:cancel_end]
        self.assertLess(cancel_body.index("requestCancellation()"), cancel_body.index("queue.async"))
        self.assertIn("!wasCancellationRequested()", asr)
        self.assertIn("outboundFrames.removeAll()", asr)
        self.assertIn("pendingAudio.removeAll()", asr)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS Security framework")
    def test_protocol_and_revision_state_machine_in_swift(self) -> None:
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            self.skipTest("swiftc is not available")
        harness = r'''
import Foundation

@main
enum Harness {
    static func main() {
        let payload = Data("hello".utf8)
        precondition(VoiceHotkeyConfig.default.choice == .middleMouse)
        precondition(VoiceHotkeyChoice.allCases.first == .middleMouse)
        precondition(VoiceHotkeyChoice.allCases.contains(.rightOption))
        precondition(VoiceHotkeyChoice.allCases.contains(.optionSpace))
        let frame = VoiceASRFrame.build(
            messageType: .fullClientRequest,
            flags: .positiveSequence,
            serializationJSON: true,
            payload: payload,
            sequence: 1
        )
        precondition(Array(frame.prefix(4)) == [0x11, 0x11, 0x10, 0x00])
        let parsed = VoiceASRFrame.parse(frame)!
        precondition(parsed.sequence == 1)
        precondition(parsed.payload == payload)
        precondition(!parsed.isFinal)

        let final = VoiceASRFrame.build(
            messageType: .audioOnlyRequest,
            flags: .negativeSequence,
            serializationJSON: false,
            payload: Data(),
            sequence: -4
        )
        precondition(VoiceASRFrame.parse(final)!.isFinal)

        var reconciler = VoiceTranscriptReconciler()
        let first = reconciler.revise(to: "你好", isFinal: false)!
        precondition(first.replacementUTF16Length == 0)
        let second = reconciler.revise(to: "你好世界", isFinal: false)!
        precondition(second.replacementUTF16Length == 2)
        let done = reconciler.revise(to: "你好，世界。", isFinal: true)!
        precondition(done.replacementUTF16Length == 4)
        precondition(done.isFinal)

        var secondPass = VoiceTranscriptReconciler()
        _ = secondPass.revise(to: "豆包", isFinal: false)!
        let corrected = secondPass.revise(to: "豆包 API 已修正。", isFinal: true)!
        precondition(corrected.replacementUTF16Length == 2)
        precondition(corrected.text == "豆包 API 已修正。")
        precondition(
            VoiceFinalTextNormalizer.normalize("还有就是我怀疑它依然依然都在。")
                == "还有就是我怀疑它依然都在。"
        )
        precondition(
            VoiceFinalTextNormalizer.normalize("当前用的补全模型还是 还是 60M？")
                == "当前用的补全模型还是 60M？"
        )
        precondition(
            VoiceFinalTextNormalizer.normalize("这个功能非常非常重要。")
                == "这个功能非常非常重要。"
        )

        let json: [String: Any] = ["result": ["utterances": [["text": "边说"], ["text": "边写"]]]]
        precondition(VolcengineStreamingASRClient.transcript(from: json) == "边说边写")
        let secondPassJSON: [String: Any] = [
            "result": ["text": "豆包 API 已修正。", "utterances": [["text": "豆包"]]]
        ]
        precondition(VolcengineStreamingASRClient.transcript(from: secondPassJSON) == "豆包 API 已修正。")
        let metadataJSON: [String: Any] = [
            "result": [[
                "text": "不会进入诊断元数据",
                "utterances": [[
                    "text": "也不能进入诊断元数据",
                    "start_time": 0,
                    "end_time": 133,
                    "definite": true,
                ]],
                "additions": "{\"duration\":133,\"stage\":\"nonstream\"}",
            ]],
        ]
        let metadataFrame = VoiceASRParsedFrame(
            messageType: .fullServerResponse,
            flags: VoiceASRFlags.negativeSequence.rawValue,
            sequence: -17,
            errorCode: nil,
            payload: Data()
        )
        let metadata = VolcengineStreamingASRClient.responseMetadata(
            from: metadataJSON,
            frame: metadataFrame
        )
        precondition(metadata.stage == "nonstream")
        precondition(metadata.sequence == -17)
        precondition(metadata.isFinalFrame)
        precondition(metadata.utteranceMetadata.count == 1)
        precondition(metadata.utteranceMetadata[0]["end_time"] == "133")
        precondition(metadata.utteranceMetadata[0]["text"] == nil)
        precondition(metadata.additionFields["duration"] == "133")

        let hotwords = try! VoiceASRHotwordConfig.validated(
            enabled: true,
            rawLines: "MiniMind\nDeepSeek\nminimind\n火山语音"
        )
        precondition(hotwords.words == ["MiniMind", "DeepSeek", "火山语音"])
        let requestPayload = VolcengineStreamingASRClient.initialRequestPayload(
            connectID: "test-connect-id",
            hotwordConfig: hotwords
        )
        let request = requestPayload["request"] as! [String: Any]
        precondition(request["enable_ddc"] as? Bool == true)
        precondition(request["enable_nonstream"] as? Bool == true)
        precondition(request["result_type"] as? String == "full")
        let context = request["context"] as! String
        let contextData = context.data(using: .utf8)!
        let contextJSON = try! JSONSerialization.jsonObject(with: contextData) as! [String: Any]
        let requestHotwords = contextJSON["hotwords"] as! [[String: String]]
        precondition(requestHotwords.map { $0["word"]! } == hotwords.words)

        let disabledPayload = VolcengineStreamingASRClient.initialRequestPayload(
            connectID: "test-connect-id",
            hotwordConfig: .disabled
        )
        let disabledRequest = disabledPayload["request"] as! [String: Any]
        precondition(disabledRequest["context"] == nil)
        let technicalHotwords = try! VoiceASRHotwordConfig.validated(
            enabled: true,
            rawLines: "GPT-5.6\nAPI Key\nSK\nC++"
        )
        precondition(technicalHotwords.words == ["GPT-5.6", "API Key", "SK", "C++"])
        precondition((try? VoiceASRHotwordConfig.validated(enabled: true, rawLines: "bad|word")) == nil)
        precondition((try? VoiceASRHotwordConfig.validated(enabled: true, rawLines: "bad{json")) == nil)
        precondition((try? VoiceASRHotwordConfig.validated(enabled: true, rawLines: "用")) == nil)

        let codex = VoiceInsertionApplicationIdentity(
            bundleIdentifier: "com.openai.codex",
            name: "Codex"
        )
        let transientFrontmost = VoiceInsertionApplicationIdentity(
            bundleIdentifier: "com.rag-ime.voice",
            name: "RagImeVoice"
        )
        let resolved = VoiceInsertionTargetPolicy.resolveApplication(
            focused: codex,
            frontmost: transientFrontmost
        )
        precondition(resolved == codex)
        let ghostty = VoiceInsertionApplicationIdentity(
            bundleIdentifier: "com.mitchellh.ghostty",
            name: "Ghostty"
        )
        precondition(VoiceInsertionTargetPolicy.resolveApplication(
            focused: VoiceInsertionApplicationIdentity(bundleIdentifier: "com.apple.TextEdit", name: "TextEdit"),
            frontmost: ghostty
        ) == ghostty)
        precondition(VoiceInsertionTargetPolicy.mode(for: resolved, accessibilityWritable: true) == .finalPaste)
        precondition(VoiceInsertionTargetPolicy.mode(
            for: VoiceInsertionApplicationIdentity(bundleIdentifier: "com.mitchellh.ghostty", name: "Ghostty"),
            accessibilityWritable: true
        ) == .finalPaste)
        precondition(VoiceInsertionTargetPolicy.mode(
            for: VoiceInsertionApplicationIdentity(bundleIdentifier: "com.apple.Terminal", name: "Terminal"),
            accessibilityWritable: true
        ) == .finalPaste)
        precondition(VoiceInsertionTargetPolicy.mode(
            for: VoiceInsertionApplicationIdentity(bundleIdentifier: "dev.warp.Warp-Stable", name: "Warp"),
            accessibilityWritable: true
        ) == .finalPaste)
        precondition(VoiceInsertionTargetPolicy.isSameApplication(
            captured: codex,
            focused: codex,
            frontmost: transientFrontmost
        ))
        precondition(!VoiceInsertionTargetPolicy.isSameApplication(
            captured: codex,
            focused: VoiceInsertionApplicationIdentity(bundleIdentifier: "com.apple.TextEdit", name: "TextEdit"),
            frontmost: transientFrontmost
        ))
        precondition(!VoiceInsertionTargetPolicy.isSameApplication(
            captured: codex,
            focused: codex,
            frontmost: VoiceInsertionApplicationIdentity(bundleIdentifier: "com.apple.TextEdit", name: "TextEdit")
        ))
        precondition(VoiceInsertionTargetPolicy.isSameApplication(
            captured: VoiceInsertionApplicationIdentity(bundleIdentifier: "com.openai.codex.helper", name: "Codex Helper"),
            focused: VoiceInsertionApplicationIdentity(bundleIdentifier: "com.openai.codex.helper", name: "Codex Helper"),
            frontmost: codex
        ))
        precondition(VoiceInsertionTargetPolicy.selectionMatchesOwnRevision(
            origin: 4,
            insertedUTF16Length: 5,
            currentLocation: 9,
            currentLength: 0
        ))
        precondition(!VoiceInsertionTargetPolicy.selectionMatchesOwnRevision(
            origin: 4,
            insertedUTF16Length: 5,
            currentLocation: 8,
            currentLength: 0
        ))
        precondition(VoiceInsertionTargetPolicy.selectionMatchesOwnRevision(
            origin: 4,
            insertedUTF16Length: 5,
            currentLocation: 4,
            currentLength: 5
        ))
        precondition(VoiceInsertionTargetPolicy.selectionMatchesOwnRevision(
            origin: 4,
            insertedUTF16Length: 5,
            currentLocation: 4,
            currentLength: 0,
            observedAfterWrite: VoiceInsertionSelection(location: 4, length: 0)
        ))
        precondition(!VoiceInsertionTargetPolicy.selectionMatchesOwnRevision(
            origin: 4,
            insertedUTF16Length: 5,
            currentLocation: 10,
            currentLength: 0,
            observedAfterWrite: VoiceInsertionSelection(location: 4, length: 0)
        ))

        precondition(VoicePrivacyPolicy.denies(
            bundleIdentifier: "com.1Password.1Password",
            applicationName: "1Password",
            metadata: "",
            environment: [:]
        ))
        precondition(VoicePrivacyPolicy.denies(
            bundleIdentifier: "com.example.browser",
            applicationName: "Browser",
            metadata: "Incognito Window",
            environment: [:]
        ))
        precondition(VoicePrivacyPolicy.denies(
            bundleIdentifier: "com.example.private-notes",
            applicationName: "Notes",
            metadata: "",
            environment: [VoicePrivacyPolicy.additionalBundleIDsEnvironmentKey: "com.example.private-notes"]
        ))
        precondition(!VoicePrivacyPolicy.denies(
            bundleIdentifier: "com.example.editor",
            applicationName: "Editor",
            metadata: "document",
            environment: [:]
        ))
        precondition(!VoicePrivacyPolicy.denies(
            bundleIdentifier: "",
            applicationName: "Unknown Web Editor",
            metadata: "",
            environment: [:]
        ))

        let telemetry = VoiceSessionTelemetry(
            networkState: "streaming",
            sessionActive: true,
            sessionStartedAtMs: 100,
            firstPartialLatencyMs: 42,
            finalLatencyMs: nil,
            pcmFrameCount: 7,
            droppedPCMFrameCount: 0,
            partialRevisionCount: 2,
            finalReceived: false,
            finalRevisedPartial: nil,
            localSmoothingApplied: nil
        )
        let encoded = try! JSONEncoder().encode(telemetry)
        precondition(try! JSONDecoder().decode(VoiceSessionTelemetry.self, from: encoded) == telemetry)
        precondition(String(data: encoded, encoding: .utf8)?.contains("边说") == false)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="rag-ime-voice-test-") as directory:
            temp = Path(directory)
            harness_path = temp / "Harness.swift"
            binary = temp / "voice-protocol-test"
            harness_path.write_text(harness, encoding="utf-8")
            compiled = subprocess.run(
                [
                    swiftc,
                    "-parse-as-library",
                    str(ROOT / "macos/Shared/VoiceHotkeyConfig.swift"),
                    str(ROOT / "macos/Shared/VoiceKeychainStore.swift"),
                    str(ROOT / "macos/Shared/VoiceAgentStatus.swift"),
                    str(ROOT / "macos/Shared/VoiceHotwordConfig.swift"),
                    str(ROOT / "macos/Shared/VoiceInsertionTargetPolicy.swift"),
                    str(ROOT / "macos/Shared/VoiceStreamingASR.swift"),
                    str(ROOT / "macos/Shared/VolcengineStreamingASR.swift"),
                    str(ROOT / "macos/RagImeVoice/VoicePrivacyPolicy.swift"),
                    str(harness_path),
                    "-framework",
                    "Security",
                    "-o",
                    str(binary),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            executed = subprocess.run([str(binary)], text=True, capture_output=True, check=False)
            self.assertEqual(executed.returncode, 0, executed.stderr)


if __name__ == "__main__":
    unittest.main()
