from __future__ import annotations

import os
from io import BytesIO
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from rag_ime.paw_browser_runtime import PawBrowserRuntime


class PawBrowserRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.calls: list[tuple[str, str, object]] = []
        self.targets = [
            {
                "id": "A1B2C3D4E5F6",
                "type": "page",
                "title": "PAW docs",
                "url": "https://example.com/docs",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/A1B2C3D4E5F6",
            }
        ]
        self.runtime = PawBrowserRuntime(
            Path(self.temp.name) / "runtime-profile",
            json_request=self._json_request,
            cdp_request=self._cdp_request,
            host_request=self._host_request,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_tabs_are_direct_devtools_targets_with_stable_numeric_ids(self) -> None:
        tabs = self.runtime.tabs(9222)

        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["targetId"], "A1B2C3D4E5F6")
        self.assertEqual(tabs[0]["tabId"], self.runtime.tab_id("A1B2C3D4E5F6"))
        self.assertEqual(tabs[0]["title"], "PAW docs")

    def test_live_electron_host_uses_its_own_listener_instead_of_a_stale_shared_port_file(self) -> None:
        profile = Path(self.temp.name) / "shared-runtime-profile"
        profile.mkdir(parents=True)
        (profile / "PAWBrowserHost.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        (profile / "DevToolsActivePort").write_text("60028\n/devtools/browser/stale\n", encoding="utf-8")

        def request(_method: str, url: str) -> object:
            if url == "http://127.0.0.1:59957/json/version":
                return {"Browser": "Chrome/150"}
            raise OSError("not a DevTools listener")

        runtime = PawBrowserRuntime(
            profile,
            json_request=request,
            listening_ports=lambda pid: [59957, 8770] if pid == os.getpid() else [],
        )

        self.assertEqual(runtime.port(), 59957)

    def test_live_electron_host_fails_closed_when_the_stale_port_belongs_elsewhere(self) -> None:
        profile = Path(self.temp.name) / "ambiguous-runtime-profile"
        profile.mkdir(parents=True)
        (profile / "PAWBrowserHost.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        (profile / "DevToolsActivePort").write_text("60028\n/devtools/browser/stale\n", encoding="utf-8")
        runtime = PawBrowserRuntime(
            profile,
            json_request=lambda _method, _url: (_ for _ in ()).throw(OSError("not DevTools")),
            listening_ports=lambda _pid: [8770],
        )

        self.assertEqual(runtime.port(), 0)

    def test_standalone_browser_still_uses_the_profile_port_file(self) -> None:
        self.runtime.profile_path.mkdir(parents=True)
        self.runtime.port_file.write_text("9222\n/devtools/browser/root\n", encoding="utf-8")

        self.assertEqual(self.runtime.port(), 9222)

    def test_electron_host_page_is_excluded_and_selected_webview_is_first(self) -> None:
        self.targets = [
            {
                "id": "HOST",
                "type": "page",
                "title": "Personal Agent Workbench",
                "url": "http://127.0.0.1:54321/?frontend=paw-os&pawHost=electron#/browser",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/HOST",
            },
            {
                "id": "GUEST-ONE",
                "type": "webview",
                "title": "First",
                "url": "https://first.example/",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/GUEST-ONE",
            },
            {
                "id": "GUEST-TWO",
                "type": "webview",
                "title": "Second",
                "url": "https://second.example/",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/GUEST-TWO",
            },
        ]
        self.runtime.active_target_file.parent.mkdir(parents=True, exist_ok=True)
        self.runtime.active_target_file.write_text(
            '{"targetId":"GUEST-TWO","title":"Second","url":"https://second.example/"}\n',
            encoding="utf-8",
        )

        tabs = self.runtime.tabs(9222)

        self.assertEqual([tab["title"] for tab in tabs], ["Second", "First"])
        self.assertTrue(tabs[0]["active"])
        self.assertFalse(tabs[1]["active"])

    def test_navigate_click_and_type_use_cdp_without_a_command_queue(self) -> None:
        tab_id = self.runtime.tab_id("A1B2C3D4E5F6")

        navigated = self.runtime.execute(9222, "navigate", {"tabId": tab_id, "url": "https://example.com/next"})
        clicked = self.runtime.execute(9222, "click", {"tabId": tab_id, "refId": "0:e1"})
        typed = self.runtime.execute(9222, "type", {"tabId": tab_id, "refId": "0:e2", "text": "hello", "clear": True})

        methods = [method for kind, method, _params in self.calls if kind == "cdp"]
        self.assertIn("Page.navigate", methods)
        self.assertGreaterEqual(methods.count("Runtime.evaluate"), 4)
        self.assertTrue(navigated["ok"])
        self.assertTrue(clicked["ok"])
        self.assertTrue(typed["ok"])
        self.assertEqual(typed["targetId"], "A1B2C3D4E5F6")
        self.assertEqual(typed["title"], "PAW docs")

    def test_navigate_creates_the_first_visible_electron_guest_when_no_tab_exists(self) -> None:
        self.targets = []
        self.runtime.profile_path.mkdir(parents=True)
        self.runtime.host_pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.runtime.host_origin_file.write_text("http://127.0.0.1:54321\n", encoding="utf-8")
        self.runtime.host_pid_file.with_suffix(".token").write_text("host-token\n", encoding="utf-8")

        result = self.runtime.execute(
            9222,
            "navigate",
            {"url": "https://example.com/first"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["targetId"], "VISIBLE-GUEST")
        self.assertEqual(result["tabId"], self.runtime.tab_id("VISIBLE-GUEST"))
        self.assertIn(
            (
                "host",
                "POST",
                {
                    "url": "http://127.0.0.1:54321/__paw_browser/tabs",
                    "payload": {"url": "https://example.com/first"},
                    "token": "host-token",
                },
            ),
            self.calls,
        )
        self.assertFalse(
            any(kind == "json" and "/json/new" in str(params) for kind, _method, params in self.calls)
        )

    def test_navigation_preserves_chromium_network_failure(self) -> None:
        with patch.object(self.runtime, "_cdp_request", return_value={
            "frameId": "FRAME", "errorText": "net::ERR_CONNECTION_REFUSED",
        }), patch.object(self.runtime, "_snapshot") as snapshot:
            result = self.runtime.execute(9222, "navigate", {"url": "http://127.0.0.1:4173/"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["failureReason"], "browser_navigation_failed")
        self.assertIn("ERR_CONNECTION_REFUSED", result["summary"])
        self.assertIn("workspace_job", result["recoveryHint"])
        snapshot.assert_not_called()

    def test_reading_chromium_error_page_reports_page_failure_with_diagnostics(self) -> None:
        with patch.object(self.runtime, "_snapshot", return_value={
            "url": "chrome-error://chromewebdata/",
            "title": "", "markdown": "# This site cannot be reached",
        }):
            result = self.runtime.execute(9222, "read_page", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["failureReason"], "browser_page_load_failed")
        self.assertIn("This site cannot be reached", result["markdown"])

    def test_screenshot_returns_a_bounded_data_url_and_structured_page(self) -> None:
        result = self.runtime.execute(
            9222,
            "screenshot",
            {"tabId": self.runtime.tab_id("A1B2C3D4E5F6")},
        )

        self.assertEqual(result["screenshotDataUrl"], "data:image/png;base64,iVBORw0KGgo=")
        self.assertIn("# PAW docs", result["markdown"])
        self.assertEqual(result["interactiveCount"], 2)

    def test_electron_webview_screenshot_uses_visible_host_capture(self) -> None:
        self.targets[0]["type"] = "webview"
        self.runtime.profile_path.mkdir(parents=True, exist_ok=True)
        self.runtime.host_pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.runtime.host_origin_file.write_text("http://127.0.0.1:54321\n", encoding="utf-8")
        self.runtime.host_pid_file.with_suffix(".token").write_text("host-token\n", encoding="utf-8")

        result = self.runtime.execute(
            9222,
            "screenshot",
            {"tabId": self.runtime.tab_id("A1B2C3D4E5F6")},
        )

        self.assertEqual(result["screenshotDataUrl"], "data:image/png;base64,aG9zdC1waXhlbHM=")
        self.assertTrue(any(
            kind == "host" and params["url"].endswith("/__paw_browser/screenshot")
            for kind, _method, params in self.calls
        ))
        self.assertFalse(any(
            kind == "cdp" and method == "Page.captureScreenshot"
            for kind, method, _params in self.calls
        ))

    def test_closing_the_last_tab_keeps_a_blank_page_available(self) -> None:
        result = self.runtime.execute(
            9222,
            "close_tab",
            {"tabId": self.runtime.tab_id("A1B2C3D4E5F6")},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "about:blank")
        self.assertIn("Page.navigate", [method for kind, method, _params in self.calls if kind == "cdp"])
        self.assertFalse(any(kind == "json" and "/json/close/" in str(params) for kind, _method, params in self.calls))

    def test_devtools_close_accepts_its_plain_text_success_receipt(self) -> None:
        with patch("rag_ime.paw_browser_runtime.urlopen", return_value=BytesIO(b"Target is closing")):
            self.assertEqual(self.runtime._default_json_request(
                "GET", "http://127.0.0.1:9222/json/close/TEST",
            ), {})

    def test_host_request_preserves_large_screenshot_json(self) -> None:
        encoded = "x" * 100_001
        payload = BytesIO((f'{{"ok":true,"data":"{encoded}"}}').encode("utf-8"))
        with patch("rag_ime.paw_browser_runtime.urlopen", return_value=payload):
            result = self.runtime._default_host_request(
                "POST",
                "http://127.0.0.1:54321/__paw_browser/screenshot",
                {"targetId": "guest"},
                "host-token",
            )
        self.assertEqual(result["data"], encoded)

    def test_host_request_surfaces_bounded_http_error_body(self) -> None:
        error = HTTPError(
            "http://127.0.0.1:54321/__paw_browser/screenshot",
            500,
            "Internal Server Error",
            None,
            BytesIO('{"ok":false,"error":"当前网页不可用"}'.encode("utf-8")),
        )
        with patch("rag_ime.paw_browser_runtime.urlopen", side_effect=error):
            with self.assertRaisesRegex(
                RuntimeError,
                r"PAW Browser host returned HTTP 500: 当前网页不可用",
            ):
                self.runtime._default_host_request(
                    "POST",
                    "http://127.0.0.1:54321/__paw_browser/screenshot",
                    {"targetId": "stale-target"},
                    "host-token",
                )

    def _json_request(self, method: str, url: str) -> object:
        self.calls.append(("json", method, url))
        if url.endswith("/json/version"):
            return {"Browser": "Chrome/140", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/root"}
        if "/json/list" in url:
            return self.targets
        return {}

    def _cdp_request(self, websocket_url: str, method: str, params: dict[str, object]) -> object:
        self.calls.append(("cdp", method, params))
        if method == "Runtime.evaluate" and "document.title" in str(params.get("expression") or ""):
            return {
                "result": {
                    "value": {
                        "title": "PAW docs",
                        "url": "https://example.com/docs",
                        "markdown": "# PAW docs\n- [0:e1] button \"Continue\"",
                        "interactiveCount": 2,
                        "viewport": {"width": 1280, "height": 720},
                    }
                }
            }
        if method == "Page.captureScreenshot":
            return {"data": "iVBORw0KGgo="}
        return {"result": {"value": True}}

    def _host_request(
        self,
        method: str,
        url: str,
        payload: dict[str, object],
        token: str,
    ) -> object:
        self.calls.append(("host", method, {"url": url, "payload": payload, "token": token}))
        if url.endswith("/__paw_browser/screenshot"):
            return {"ok": True, "mimeType": "image/png", "data": "aG9zdC1waXhlbHM="}
        self.targets.append(
            {
                "id": "VISIBLE-GUEST",
                "type": "webview",
                "title": "First",
                "url": str(payload.get("url") or "about:blank"),
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/VISIBLE-GUEST",
            }
        )
        return {"ok": True, "targetId": "VISIBLE-GUEST", "webContentsId": 42}


if __name__ == "__main__":
    unittest.main()
