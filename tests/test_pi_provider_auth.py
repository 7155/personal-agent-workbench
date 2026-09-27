from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from rag_ime.pi.provider_auth import (
    PiProviderAuthError,
    PiProviderAuthService,
    PiProviderBridgeConfig,
    _OAuthJob,
    _openai_codex_login_uri,
)
from rag_ime.pi.config import PiRuntimeConfig


ROOT = Path(__file__).resolve().parents[1]
PI_PACKAGE_ENTRY = ROOT.parent / "pi" / "packages" / "coding-agent" / "dist" / "index.js"


class _FakePiProviderAuthService(PiProviderAuthService):
    def __init__(self, config: PiProviderBridgeConfig) -> None:
        super().__init__(config)
        self.calls: list[dict[str, object]] = []

    def catalog(self) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.pi-provider-catalog.v1",
            "ok": True,
            "available": True,
            "providers": [
                {
                    "id": "openai-codex",
                    "name": "ChatGPT Plus/Pro",
                    "auth": {
                        "configured": False,
                        "oauthBrowserSupported": True,
                        "oauthDeviceCodeSupported": True,
                    },
                },
                {
                    "id": "test-provider",
                    "name": "Test Provider",
                    "auth": {"configured": True, "type": "api_key"},
                },
            ],
        }

    def _call(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(dict(request))
        return {
            "ok": True,
            "provider": request.get("provider"),
            "beforeType": "api_key",
        }

    def _start_oauth(
        self,
        provider: str,
        provider_name: str,
        *,
        method: str = "browser",
    ) -> dict[str, object]:
        self.calls.append(
            {
                "action": "oauth",
                "provider": provider,
                "method": method,
            }
        )
        return {
            "schemaVersion": "rag-ime.pi-provider-oauth-status.v1",
            "ok": True,
            "loginId": "pi-login-test",
            "provider": provider,
            "providerName": provider_name,
            "loginMethod": method,
            "state": "starting",
        }


class PiProviderAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-ime-pi-provider-")
        root = Path(self.temporary.name)
        package_entry = root / "index.js"
        bridge_script = root / "bridge.mjs"
        package_entry.write_text("export {};\n", encoding="utf-8")
        bridge_script.write_text("\n", encoding="utf-8")
        self.service = _FakePiProviderAuthService(
            PiProviderBridgeConfig(
                node_executable="node",
                package_entry=package_entry,
                agent_dir=root / "agent",
                bridge_script=bridge_script,
            )
        )

    def test_jev_key_uses_keychain_without_pi_or_secret_receipts(self) -> None:
        service = PiProviderAuthService(self.service.config)
        with patch.object(service, "_call", return_value={"providers": []}) as bridge, patch(
            "rag_ime.pi.provider_auth.read_keychain_secret", return_value=""
        ), patch("rag_ime.pi.provider_auth.write_keychain_secret") as write, patch.dict(
            "os.environ", {"TYPESAFE_API_KEY": ""}
        ):
            catalog = service.catalog()
            self.assertEqual(catalog["providers"][0]["id"], "typesafe")
            preview = service.preview({"provider": "typesafe", "action": "set_api_key"})
            receipt = service.apply({"previewToken": preview["previewToken"],
                                     "confirmText": "replace", "apiKey": "test-jev-secret"})
            write.assert_called_once_with("com.rag-ime.model-provider", "typesafe-api-key", "test-jev-secret")
            self.assertFalse(receipt["requiresAgentRestart"])
            self.assertNotIn("test-jev-secret", json.dumps([catalog, preview, receipt]))
            self.assertTrue(all(call.args[0]["action"] == "catalog" for call in bridge.call_args_list))

    def test_jev_environment_override_rejects_keychain_changes(self) -> None:
        service = PiProviderAuthService(self.service.config)
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": "environment-secret"}):
            with self.assertRaisesRegex(PiProviderAuthError, "环境变量"):
                service.preview({"provider": "typesafe", "action": "set_api_key"})

    def test_jev_logout_and_storage_failure_are_secret_free(self) -> None:
        service = PiProviderAuthService(self.service.config)
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": ""}), patch(
            "rag_ime.pi.provider_auth.read_keychain_secret", return_value="existing-key"
        ), patch("rag_ime.pi.provider_auth.delete_keychain_secret") as delete:
            preview = service.preview({"provider": "typesafe", "action": "logout"})
            receipt = service.apply({"previewToken": preview["previewToken"], "confirmText": "logout"})
            delete.assert_called_once_with("com.rag-ime.model-provider", "typesafe-api-key")
            self.assertFalse(receipt["requiresAgentRestart"])
            with patch("rag_ime.pi.provider_auth.write_keychain_secret", side_effect=RuntimeError("secret-value")):
                preview = service.preview({"provider": "typesafe", "action": "set_api_key"})
                with self.assertRaises(PiProviderAuthError) as error:
                    service.apply({"previewToken": preview["previewToken"], "confirmText": "replace", "apiKey": "secret-value"})
                self.assertNotIn("secret-value", str(error.exception))

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def test_api_key_replace_uses_one_time_confirmation_and_secret_free_receipt(self) -> None:
        preview = self.service.preview(
            {"provider": "test-provider", "action": "set_api_key"}
        )
        receipt = self.service.apply(
            {
                "previewToken": preview["previewToken"],
                "confirmText": "replace",
                "apiKey": "secret-sentinel-value",
            }
        )

        self.assertEqual(self.service.calls[-1]["action"], "set_api_key")
        self.assertEqual(self.service.calls[-1]["apiKey"], "secret-sentinel-value")
        self.assertNotIn("secret-sentinel-value", json.dumps(receipt))
        self.assertTrue(receipt["requiresAgentRestart"])
        with self.assertRaisesRegex(PiProviderAuthError, "失效"):
            self.service.apply(
                {
                    "previewToken": preview["previewToken"],
                    "confirmText": "replace",
                    "apiKey": "another-key",
                }
            )

    def test_logout_and_oauth_methods_are_explicit_receipted_actions(self) -> None:
        logout = self.service.preview(
            {"provider": "test-provider", "action": "logout"}
        )
        logout_receipt = self.service.apply(
            {"previewToken": logout["previewToken"], "confirmText": "logout"}
        )
        self.assertEqual(logout_receipt["action"], "logout")

        browser = self.service.preview(
            {"provider": "openai-codex", "action": "oauth_browser"}
        )
        browser_receipt = self.service.apply(
            {"previewToken": browser["previewToken"], "confirmText": "connect"}
        )
        self.assertEqual(browser_receipt["login"]["loginMethod"], "browser")
        self.assertEqual(self.service.calls[-1]["method"], "browser")

        oauth = self.service.preview(
            {"provider": "openai-codex", "action": "oauth_device_code"}
        )
        oauth_receipt = self.service.apply(
            {"previewToken": oauth["previewToken"], "confirmText": "connect"}
        )
        self.assertEqual(oauth_receipt["login"]["state"], "starting")
        self.assertEqual(oauth_receipt["login"]["loginMethod"], "device_code")
        self.assertEqual(self.service.calls[-1]["method"], "device_code")
        self.assertEqual(oauth_receipt["receiptState"], "login_started")
        self.assertFalse(oauth_receipt["requiresAgentRestart"])
        self.assertNotIn("token", json.dumps(oauth_receipt).lower())

    def test_provider_and_action_are_allowlisted(self) -> None:
        with self.assertRaises(PiProviderAuthError):
            self.service.preview({"provider": "../../auth.json", "action": "logout"})
        with self.assertRaises(PiProviderAuthError):
            self.service.preview({"provider": "test-provider", "action": "read_secret"})

    def test_browser_login_url_is_restricted_to_openai_oauth_routes(self) -> None:
        authorize = (
            "https://auth.openai.com/oauth/authorize"
            "?client_id=test&state=test&code_challenge=test"
        )
        self.assertEqual(_openai_codex_login_uri(authorize), authorize)
        self.assertEqual(
            _openai_codex_login_uri("https://auth.openai.com/codex/device"),
            "https://auth.openai.com/codex/device",
        )
        for unsafe in (
            "http://auth.openai.com/oauth/authorize",
            "https://auth.openai.com.evil.example/oauth/authorize",
            "https://auth.openai.com:444/oauth/authorize",
            "https://auth.openai.com/other",
        ):
            self.assertEqual(_openai_codex_login_uri(unsafe), "")

    def test_managed_runtime_bridge_is_discovered_without_a_pi_package_entry(self) -> None:
        root = Path(self.temporary.name)
        runtime_dir = root / "runtime-host"
        runtime_dir.mkdir()
        executable = runtime_dir / "cli.mjs"
        managed_bridge = runtime_dir / "provider-bridge.mjs"
        executable.write_text("\n", encoding="utf-8")
        managed_bridge.write_text("\n", encoding="utf-8")
        runtime = PiRuntimeConfig(
            enabled=True,
            executable=executable,
            agent_dir=root / "agent",
            session_dir=root / "sessions",
            logs_dir=root / "logs",
            node_executable=sys.executable,
        )

        config = PiProviderBridgeConfig.from_runtime(runtime)
        service = PiProviderAuthService(config)

        self.assertTrue(config.available)
        self.assertEqual(config.bridge_entry, managed_bridge)
        self.assertIsNone(config.package_entry)
        self.assertEqual(
            service._bridge_payload({"action": "catalog"}),
            {"action": "catalog", "agentDir": str(config.agent_dir)},
        )

    @unittest.skipUnless(PI_PACKAGE_ENTRY.is_file(), "local Pi build is not available")
    def test_real_pi_auth_storage_bridge_replaces_and_logs_out_without_echoing_secret(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-real-pi-auth-") as temporary:
            agent_dir = Path(temporary)
            (agent_dir / "models.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            "test-provider": {
                                "baseUrl": "https://example.invalid/v1",
                                "api": "openai-completions",
                                "models": [{"id": "test-model", "name": "Test Model"}],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            service = PiProviderAuthService(
                PiProviderBridgeConfig(
                    node_executable="node",
                    package_entry=PI_PACKAGE_ENTRY,
                    agent_dir=agent_dir,
                    bridge_script=ROOT / "rag_ime" / "node" / "pi_provider_bridge.mjs",
                    timeout_seconds=10,
                )
            )
            preview = service.preview(
                {"provider": "test-provider", "action": "set_api_key"}
            )
            receipt = service.apply(
                {
                    "previewToken": preview["previewToken"],
                    "confirmText": "replace",
                    "apiKey": "secret-sentinel-real-pi",
                }
            )
            provider = next(
                item
                for item in service.catalog()["providers"]
                if item["id"] == "test-provider"
            )
            self.assertTrue(provider["auth"]["configured"])
            self.assertEqual(provider["auth"]["type"], "api_key")
            self.assertEqual(provider["availableModelCount"], 1)
            self.assertNotIn("secret-sentinel-real-pi", json.dumps(receipt))
            self.assertEqual(
                stat.S_IMODE((agent_dir / "auth.json").stat().st_mode),
                0o600,
            )

            logout = service.preview(
                {"provider": "test-provider", "action": "logout"}
            )
            service.apply(
                {"previewToken": logout["previewToken"], "confirmText": "logout"}
            )
            self.assertEqual(
                json.loads((agent_dir / "auth.json").read_text(encoding="utf-8")),
                {},
            )

    def test_oauth_is_single_flight_and_has_a_bounded_timeout(self) -> None:
        root = Path(self.temporary.name)
        bridge = root / "oauth_bridge.py"
        bridge.write_text(
            "import json, sys, time\n"
            "json.load(sys.stdin)\n"
            "print(json.dumps({'event': 'device_code', 'userCode': 'TEST-CODE', "
            "'verificationUri': 'https://auth.openai.com/codex/device', "
            "'expiresInSeconds': 900}), flush=True)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        service = PiProviderAuthService(
            PiProviderBridgeConfig(
                node_executable=sys.executable,
                package_entry=root / "index.js",
                agent_dir=root / "agent",
                bridge_script=bridge,
                oauth_timeout_seconds=1,
            )
        )
        try:
            started = service._start_oauth(
                "openai-codex",
                "ChatGPT Plus/Pro",
                method="device_code",
            )
            with self.assertRaisesRegex(PiProviderAuthError, "正在进行"):
                service._start_oauth(
                    "openai-codex",
                    "ChatGPT Plus/Pro",
                    method="device_code",
                )

            status = started
            deadline = time.monotonic() + 4
            while status["state"] != "failed" and time.monotonic() < deadline:
                time.sleep(0.05)
                status = service.oauth_status(started["loginId"])
            self.assertEqual(status["state"], "failed")
            self.assertIn("超时", status["error"])
            self.assertFalse(status["requiresAgentRestart"])
        finally:
            service.close()

    def test_browser_oauth_opens_only_the_allowlisted_openai_url(self) -> None:
        root = Path(self.temporary.name)
        bridge = root / "browser_oauth_bridge.py"
        bridge.write_text(
            "import json, sys, time\n"
            "json.load(sys.stdin)\n"
            "print(json.dumps({'event': 'auth_url', "
            "'url': 'https://auth.openai.com/oauth/authorize?client_id=test&state=test'}), "
            "flush=True)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        opened: list[str] = []
        service = PiProviderAuthService(
            PiProviderBridgeConfig(
                node_executable=sys.executable,
                package_entry=root / "index.js",
                agent_dir=root / "agent",
                bridge_script=bridge,
                oauth_timeout_seconds=2,
            ),
            oauth_url_opener=lambda url: opened.append(url),
        )
        try:
            started = service._start_oauth(
                "openai-codex",
                "ChatGPT Plus/Pro",
                method="browser",
            )
            status = started
            deadline = time.monotonic() + 2
            while status["state"] != "waiting_for_user" and time.monotonic() < deadline:
                time.sleep(0.05)
                status = service.oauth_status(started["loginId"])
            self.assertEqual(status["loginMethod"], "browser")
            self.assertEqual(
                status["verificationUri"],
                "https://auth.openai.com/oauth/authorize?client_id=test&state=test",
            )
            self.assertEqual(opened, [status["verificationUri"]])
        finally:
            service.close()

    def test_oauth_terminal_events_reap_a_bridge_that_does_not_exit(self) -> None:
        root = Path(self.temporary.name)
        for terminal_state in ("failed", "completed"):
            with self.subTest(terminal_state=terminal_state):
                bridge = root / f"terminal_{terminal_state}.py"
                bridge.write_text(
                    "import json, sys, time\n"
                    "json.load(sys.stdin)\n"
                    f"print(json.dumps({{'event': '{terminal_state}', "
                    "'error': 'fixture OAuth failure'}), flush=True)\n"
                    "time.sleep(30)\n",
                    encoding="utf-8",
                )
                service = PiProviderAuthService(
                    PiProviderBridgeConfig(
                        node_executable=sys.executable,
                        package_entry=root / "index.js",
                        agent_dir=root / "agent",
                        bridge_script=bridge,
                        oauth_timeout_seconds=30,
                    )
                )
                try:
                    started = service._start_oauth(
                        "openai-codex", "ChatGPT Plus/Pro", method="browser"
                    )
                    job = service._oauth_jobs[str(started["loginId"])]
                    deadline = time.monotonic() + 4
                    while job.process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertIsNotNone(job.process.poll(), "terminal OAuth bridge was left running")
                    self.assertEqual(service.oauth_status(started["loginId"])["state"], terminal_state)
                finally:
                    service.close()
                    for job in service._oauth_jobs.values():
                        if job.process.poll() is None:
                            job.process.kill()
                            job.process.wait(timeout=2)

    def test_cancel_and_close_reap_terminal_bridge_processes_idempotently(self) -> None:
        for action in ("cancel", "close"):
            for terminal_state in ("failed", "completed", "cancelled"):
                with self.subTest(action=action, terminal_state=terminal_state):
                    service = PiProviderAuthService(self.service.config)
                    process = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    job = _OAuthJob(
                        login_id="terminal-fixture",
                        provider="openai-codex",
                        provider_name="ChatGPT Plus/Pro",
                        login_method="browser",
                        process=process,
                        state=terminal_state,
                    )
                    service._oauth_jobs[job.login_id] = job
                    try:
                        for _ in range(2):
                            if action == "cancel":
                                result = service.oauth_cancel({"loginId": job.login_id})
                                self.assertEqual(result["state"], terminal_state)
                            else:
                                service.close()
                            self.assertIsNotNone(process.poll(), "terminal OAuth process survived cleanup")
                            self.assertEqual(job.state, terminal_state)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
