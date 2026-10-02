from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tools import ControlToolGateway
from rag_ime.browser_control import BrowserControlError, BrowserControlService
from rag_ime.paw_browser_runtime import PawBrowserRuntime


class BrowserControlServiceTests(unittest.TestCase):
    def test_failed_browser_poll_does_not_claim_targets_were_closed(self) -> None:
        with mock.patch.object(self.service, 'managed_status', return_value={'running': True, 'debugPort': 9222}), \
             mock.patch.object(self.runtime, 'tabs', side_effect=OSError('CDP read interrupted')):
            self.assertFalse(self.service.tabs()['liveSnapshot'])
        with mock.patch.object(self.service, 'managed_status', return_value={'running': False, 'debugPort': 0}):
            self.assertTrue(self.service.tabs()['liveSnapshot'])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.runtime = FakePawBrowserRuntime(root / "direct-profile")
        self.service = BrowserControlService(
            root / "rag-ime.sqlite",
            app_support_root=root / "support",
            browser_runtime=self.runtime,
        )
        with self.service._connection() as connection:
            self.service._set_setting(connection, "managed_pid", str(os.getpid()))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_snapshot_is_durable_across_service_instances(self) -> None:
        self.service.push_snapshot(
            {
                "deviceId": PawBrowserRuntime.DEVICE_ID,
                "snapshotId": "snap-test",
                "tabId": self.runtime.tab_id,
                "url": "https://example.com/docs",
                "title": "Example Docs",
                "summary": "2 个可交互元素",
                "markdown": '# Example\n- [0:e1] link "Read"',
                "interactiveCount": 2,
                "viewport": {"width": 1280, "height": 720},
            }
        )
        other = BrowserControlService(
            self.service.db_path,
            app_support_root=Path(self.temp.name) / "support",
            browser_runtime=self.runtime,
        )

        snapshot = other.latest_snapshot(
            device_id=PawBrowserRuntime.DEVICE_ID,
            tab_id=self.runtime.tab_id,
        )

        self.assertEqual(snapshot["snapshotId"], "snap-test")
        self.assertIn("[0:e1]", snapshot["markdown"])

    def test_restart_fails_interrupted_direct_browser_claim(self) -> None:
        command_id = "bcmd_interrupted_direct"
        with self.service._connection() as connection:
            connection.execute(
                """
                INSERT INTO browser_control_commands(
                    command_id, device_id, session_id, action, payload_json,
                    status, created_at_ms, claimed_at_ms, claimed_by
                ) VALUES (?, ?, 'session-restart', 'navigate', '{}',
                          'claimed', 1, 2, 'paw-cdp-direct')
                """,
                (command_id, PawBrowserRuntime.DEVICE_ID),
            )

        BrowserControlService(
            self.service.db_path,
            app_support_root=Path(self.temp.name) / "support",
            browser_runtime=self.runtime,
        )

        trace = next(
            item
            for item in self.service.traces()["items"]
            if item["commandId"] == command_id
        )
        self.assertEqual(trace["status"], "failed")
        self.assertEqual(trace["failureReason"], "direct_browser_interrupted")
        self.assertIsNotNone(trace["completedAtMs"])

    def test_status_uses_created_at_index_for_latest_snapshot(self) -> None:
        with self.service._connection() as connection:
            indexes = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA index_list(browser_control_snapshots)"
                ).fetchall()
            }
            plan = " ".join(
                str(row[3])
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT * FROM browser_control_snapshots "
                    "ORDER BY created_at_ms DESC LIMIT 1"
                ).fetchall()
            )

        self.assertIn("idx_browser_snapshots_created", indexes)
        self.assertIn("idx_browser_snapshots_created", plan)

    def test_snapshot_urls_redact_credentials_fragments_and_sensitive_queries(self) -> None:
        unsafe_url = (
            "https://reader:password@example.com/docs"
            "?query=browser&access_token=private&session_id=session-1#account"
        )
        self.service.push_snapshot(
            {
                "deviceId": PawBrowserRuntime.DEVICE_ID,
                "snapshotId": "snap-private-url",
                "tabId": self.runtime.tab_id,
                "url": unsafe_url,
                "title": "Private URL",
                "markdown": f"# Private URL\nURL: {unsafe_url}",
            }
        )

        snapshot = self.service.latest_snapshot(
            device_id=PawBrowserRuntime.DEVICE_ID,
            tab_id=self.runtime.tab_id,
        )

        self.assertEqual(
            snapshot["url"],
            "https://example.com/docs?query=browser&access_token=%5Bredacted%5D"
            "&session_id=%5Bredacted%5D",
        )
        self.assertNotIn("password", snapshot["markdown"])
        self.assertNotIn("access_token=private", snapshot["markdown"])
        self.assertNotIn("#account", snapshot["markdown"])

    def test_inventory_contains_only_the_direct_paw_browser(self) -> None:
        items = self.service.tabs()["items"]

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["deviceId"], PawBrowserRuntime.DEVICE_ID)
        self.assertEqual(items[0]["clientKind"], "managed")
        self.assertEqual(items[0]["targetId"], "DIRECTTARGET")
        status = self.service.status()
        self.assertEqual(status["mode"], "managed")
        self.assertNotIn("permissions", status)
        self.assertNotIn("extensionPath", status)

    def test_browser_rejects_any_device_outside_the_isolated_profile(self) -> None:
        with self.assertRaisesRegex(BrowserControlError, "only target PAW Browser"):
            self.service.submit_command(
                "navigate",
                {
                    "deviceId": "daily-chrome",
                    "url": "https://example.com/not-allowed",
                },
            )

    def test_direct_commands_and_trace_share_the_selected_browser(self) -> None:
        for action in ("back", "forward", "reload"):
            result = self.service.submit_command(
                action,
                {"deviceId": PawBrowserRuntime.DEVICE_ID, "tabId": self.runtime.tab_id},
                session_id="session-test",
            )
            self.assertTrue(result["ok"])

        self.assertEqual(self.runtime.actions, ["back", "forward", "reload"])
        traces = self.service.traces()["items"]
        self.assertEqual([item["action"] for item in traces[:3]], ["reload", "forward", "back"])
        self.assertTrue(all(item["sourceKind"] == "agent" for item in traces[:3]))

    def test_traces_batch_steps_without_changing_command_or_step_order(self) -> None:
        with self.service._connection() as connection:
            connection.executemany(
                """
                INSERT INTO browser_control_commands(
                    command_id, device_id, session_id, action, payload_json,
                    status, result_json, created_at_ms, completed_at_ms
                ) VALUES (?, 'paw-browser', 'session-test', 'run', '{}',
                          'completed', '{"ok":true,"summary":"done"}', ?, ?)
                """,
                [(f"command-{index}", index + 1, index + 2) for index in range(3)],
            )
            connection.executemany(
                """
                INSERT INTO browser_control_events(
                    kind, device_id, command_id, detail_json, created_at_ms
                ) VALUES (?, 'paw-browser', ?, ?, ?)
                """,
                [
                    (kind, command_id, json.dumps({"event": "completed", "action": action, "atMs": timestamp}), timestamp)
                    for kind, command_id, action, timestamp in [
                        ("ego_trace_step", "command-2", "last", 3),
                        ("ego_trace_step", "command-1", "other", 1),
                        ("ego_trace_step", "command-2", "first", 1),
                        ("ego_trace_step", "command-2", "second", 1),
                        ("command_completed", "command-2", "ignored-kind", 1),
                        ("ego_trace_step", "command-0", "outside-page", 1),
                    ]
                ],
            )
        statements: list[str] = []
        original_connect = self.service._connect

        def traced_connect():
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch.object(self.service, "_connect", side_effect=traced_connect):
            result = self.service.traces(limit=2)

        self.assertEqual(result["schemaVersion"], "rag-ime.browser-control.v1")
        self.assertEqual(result["summary"], "已读取 2 条浏览器执行轨迹")
        self.assertEqual([item["commandId"] for item in result["items"]], ["command-2", "command-1"])
        self.assertEqual([step["action"] for step in result["items"][0]["steps"]], ["first", "second", "last"])
        self.assertEqual([step["action"] for step in result["items"][1]["steps"]], ["other"])
        self.assertEqual(result["items"][0]["result"], {"ok": True, "summary": "done"})
        selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 2)

    def test_empty_traces_do_not_fetch_steps(self) -> None:
        statements: list[str] = []
        original_connect = self.service._connect

        def traced_connect():
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch.object(self.service, "_connect", side_effect=traced_connect):
            self.assertEqual(self.service.traces()["items"], [])
        selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1)

    def test_trace_collector_drains_a_final_append_when_stop_arrives_after_read(self) -> None:
        stop = threading.Event()
        final_step = {
            "schemaVersion": "paw.ego-browser-step.v1",
            "event": "completed",
            "action": "click",
            "atMs": 1,
        }
        final_line = json.dumps(final_step) + "\n"
        stream = mock.MagicMock()

        def read_before_process_exit():
            # The current read already observed EOF. The process then appends
            # its final line and exits before the collector checks the flag.
            stop.set()
            return ""

        reads = iter([read_before_process_exit, lambda: final_line])
        stream.read.side_effect = lambda: next(reads)()
        stream.tell.side_effect = [0, len(final_line)]
        trace_path = mock.MagicMock(spec=Path)
        trace_path.open.return_value.__enter__.return_value = stream

        with mock.patch.object(self.service, "_record_ego_step") as record:
            self.service._collect_ego_trace("command-final", trace_path, stop)

        self.assertEqual(stream.read.call_count, 2)
        record.assert_called_once_with("command-final", final_step)

    def test_trace_collector_joins_partial_final_line_and_skips_malformed_json(self) -> None:
        stop = threading.Event()
        final_step = {"event": "completed", "action": "click"}
        final_line = json.dumps(final_step) + "\n"
        boundary = len(final_line) // 2
        stream = mock.MagicMock()

        def read_before_process_exit():
            stop.set()
            return final_line[:boundary]

        reads = iter([
            read_before_process_exit,
            lambda: final_line[boundary:] + "malformed trace\n",
        ])
        stream.read.side_effect = lambda: next(reads)()
        stream.tell.side_effect = [boundary, len(final_line) + len("malformed trace\n")]
        trace_path = mock.MagicMock(spec=Path)
        trace_path.open.return_value.__enter__.return_value = stream

        with mock.patch.object(self.service, "_record_ego_step") as record:
            self.service._collect_ego_trace("command-final", trace_path, stop)

        self.assertEqual(stream.read.call_count, 2)
        record.assert_called_once_with("command-final", final_step)

    def test_trace_collector_reads_once_when_process_already_stopped(self) -> None:
        stop = threading.Event()
        stop.set()
        final_step = {"event": "completed", "action": "click"}
        path = Path(self.temp.name) / "completed-trace.jsonl"
        path.write_text(json.dumps(final_step) + "\n", encoding="utf-8")
        with mock.patch.object(self.service, "_record_ego_step") as record:
            self.service._collect_ego_trace("command-final", path, stop)
        record.assert_called_once_with("command-final", final_step)

    def test_first_browser_command_starts_the_managed_browser_when_stopped(self) -> None:
        stopped = {"running": False, "connected": False, "debugPort": None}
        connected = {"running": True, "connected": True, "debugPort": 9222}

        with (
            mock.patch.object(
                self.service,
                "managed_status",
                side_effect=[stopped, connected, connected],
            ),
            mock.patch.object(
                self.service,
                "start_managed",
                return_value={"ok": True, **connected},
            ) as start_managed,
        ):
            result = self.service.submit_command(
                "navigate",
                {"url": "https://example.com/managed"},
                session_id="session-auto-start",
            )

        self.assertTrue(result["ok"])
        start_managed.assert_called_once_with()

    def test_error_page_result_is_terminal_and_the_same_tab_can_retry(self) -> None:
        error_page = {
            "ok": False,
            "url": "chrome-error://chromewebdata/",
            "tabId": self.runtime.tab_id,
            "markdown": "# This site cannot be reached",
            "summary": "页面加载失败：net::ERR_CONNECTION_REFUSED",
            "failureReason": "browser_navigation_failed",
        }
        with mock.patch.object(self.runtime, "execute", return_value=error_page):
            result = self.service.submit_command(
                "navigate", {"url": "http://127.0.0.1:4173/"},
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("ERR_CONNECTION_REFUSED", result["summary"])
        trace = self.service.traces()["items"][0]
        self.assertEqual(trace["status"], "failed")
        self.assertIsNotNone(trace["completedAtMs"])
        self.assertEqual(trace["result"]["url"], error_page["url"])
        self.assertFalse(self.service.latest_snapshot(tab_id=self.runtime.tab_id)["ok"])

        retried = self.service.submit_command(
            "navigate", {"tabId": self.runtime.tab_id, "url": "http://127.0.0.1:4173/"},
        )
        self.assertTrue(retried["ok"])

    def test_live_error_snapshot_does_not_raise_a_navigation_parameter_error(self) -> None:
        with mock.patch.object(self.runtime, "execute", return_value={
            "ok": False,
            "url": "chrome-error://chromewebdata/",
            "tabId": self.runtime.tab_id,
            "markdown": "# This site cannot be reached",
            "summary": "页面加载失败，请检查目标服务后重试",
        }):
            result = self.service.latest_snapshot(tab_id=self.runtime.tab_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["url"], "chrome-error://chromewebdata/")
        self.assertIn("页面加载失败", result["summary"])

    def test_observed_error_page_does_not_expand_navigation_schemes(self) -> None:
        for url in ("chrome-error://chromewebdata/", "file:///tmp/private", "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaises(BrowserControlError):
                self.service.submit_command("navigate", {"url": url})

    def test_screenshot_is_stored_behind_bounded_binary_route(self) -> None:
        result = self.service.submit_command(
            "screenshot",
            {"tabId": self.runtime.tab_id},
            session_id="control-center",
        )
        snapshot_id = str(result["result"]["snapshotId"])

        mime_type, data = self.service.snapshot_image(snapshot_id)

        self.assertEqual(mime_type, "image/png")
        self.assertEqual(data, FakePawBrowserRuntime.PNG_BYTES)
        trace = self.service.traces()["items"][0]
        self.assertEqual(trace["sourceKind"], "human")
        self.assertEqual(trace["result"]["snapshotId"], snapshot_id)

    def test_agent_browser_action_runs_without_per_action_approval(self) -> None:
        sessions = AgentSessionStore(Path(self.temp.name) / "agent.sqlite")
        sessions.initialize()
        session = sessions.create(title="managed browser", created_at_ms=1)
        gateway = ControlToolGateway(
            sessions=sessions,
            management=object(),
            core=object(),
            project="browser-test",
            browser_control=self.service,
        )

        response = gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session["id"],
                "tool": "browser",
                "toolCallId": "tool:browser:managed-direct",
                "args": {
                    "op": "navigate",
                    "url": "https://example.com/managed",
                },
            }
        )["result"]

        self.assertNotIn("approvalRequired", response)
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["action"], "navigate")
        self.assertEqual(self.runtime.actions, ["navigate"])
        trace = self.service.traces()["items"][0]
        self.assertEqual(trace["sessionId"], session["id"])
        self.assertEqual(trace["sourceKind"], "agent")
        self.assertEqual(trace["target"], "https://example.com/managed")

    def test_legacy_pairing_and_permission_apis_are_absent(self) -> None:
        for name in (
            "pairing",
            "authenticate",
            "hello",
            "set_mode",
            "next_command",
            "request_permission",
            "decide_permission",
        ):
            self.assertFalse(hasattr(self.service, name), name)

    def test_ego_script_attaches_to_the_existing_paw_browser(self) -> None:
        captured: dict[str, object] = {}

        def start_ego(command: list[str], **kwargs: object) -> FakeEgoProcess:
            captured["command"] = command
            captured["env"] = kwargs.get("env")
            environment = kwargs.get("env")
            assert isinstance(environment, dict)
            return FakeEgoProcess(Path(str(environment["EGO_PAW_TRACE_PATH"])))

        self.service.ego_process_runner = start_ego
        with (
            mock.patch.object(self.service, "_ego_node", return_value=Path("/usr/bin/node")),
            mock.patch.object(self.service, "_ensure_ego_host") as ensure_host,
        ):
            result = self.service.submit_command(
                "run",
                {
                    "script": "const task = await taskSpaces.useOrCreate('test'); console.log(task.id)",
                    "timeoutMs": 3_000,
                },
                session_id="session-ego-test",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["stdout"], '{"taskSpaceId":2}\n')
        self.assertFalse(result["result"]["secondBrowserProcess"])
        self.assertIn("--permission", captured["command"])
        self.assertFalse(any("allow-child-process" in value for value in captured["command"]))
        self.assertTrue(any("allow-fs-write" in value for value in captured["command"]))
        environment = captured["env"]
        self.assertEqual(environment["EGO_CDP_PORT"], "9222")
        self.assertEqual(environment["EGO_USER_DATA_DIR"], str(self.runtime.profile_path))
        ensure_host.assert_called_once()
        steps = self.service.traces()["items"][0]["steps"]
        self.assertEqual(
            [(step["event"], step["action"]) for step in steps],
            [("started", "click"), ("completed", "click")],
        )

    def test_ego_host_restarts_when_doctor_owns_a_stale_cdp_port(self) -> None:
        doctor_calls: list[list[str]] = []
        responses = iter(
            [
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "ok": True,
                            "cdpPort": 9111,
                            "cdpUp": False,
                            "profileDir": str(self.runtime.profile_path),
                        }
                    ),
                    stderr="",
                ),
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "ok": True,
                            "cdpPort": 9222,
                            "cdpUp": True,
                            "profileDir": str(self.runtime.profile_path),
                        }
                    ),
                    stderr="",
                ),
            ]
        )

        def check(command: list[str], **_kwargs: object) -> SimpleNamespace:
            doctor_calls.append(command)
            return next(responses)

        self.service.ego_check_runner = check
        environment = {
            "EGO_CDP_PORT": "9222",
            "EGO_USER_DATA_DIR": str(self.runtime.profile_path),
        }
        with (
            mock.patch.object(self.service, "_ego_runtime_available", return_value=True),
            mock.patch.object(self.service, "_stop_ego_host", return_value=True) as stop_host,
        ):
            self.service._ensure_ego_host(
                node=Path("/usr/bin/node"),
                environment=environment,
            )

        stop_host.assert_called_once_with()
        self.assertEqual(len(doctor_calls), 2)

    def test_stop_terminates_the_active_ego_script_and_cancels_visible_trace(self) -> None:
        with self.service._connection() as connection:
            self.service._set_setting(connection, "ego_runner_pid", "424242")
            self.service._set_setting(connection, "ego_runner_command_id", "bcmd-active")
            connection.execute(
                """
                INSERT INTO browser_control_commands(
                    command_id, device_id, session_id, action, payload_json,
                    status, created_at_ms
                ) VALUES ('bcmd-active', ?, 'session-test', 'run', '{}', 'claimed', 1)
                """,
                (PawBrowserRuntime.DEVICE_ID,),
            )
        with (
            mock.patch.object(self.service, "_pid_running", return_value=True),
            mock.patch.object(self.service, "_terminate_process_group") as terminate,
        ):
            result = self.service.stop()

        self.assertTrue(result["egoRunnerStopped"])
        self.assertEqual(result["cancelled"], 1)
        terminate.assert_called_once_with(424242)
        trace = self.service.traces()["items"][0]
        self.assertEqual(trace["status"], "cancelled")
        with self.service._connection() as connection:
            self.assertEqual(self.service._setting(connection, "ego_runner_pid"), "")

    def test_managed_browser_launches_the_embedded_paw_host_without_external_chrome(self) -> None:
        root = Path(self.temp.name)
        runtime = FakePawBrowserRuntime(root / "launch-profile")
        service = BrowserControlService(
            root / "launch.sqlite",
            app_support_root=root / "launch-support",
            browser_runtime=runtime,
        )
        captured: list[str] = []
        captured_environment: dict[str, str] = {}

        def launch(command: list[str], **kwargs: object) -> SimpleNamespace:
            captured.extend(command)
            captured_environment.update(dict(kwargs.get("env") or {}))
            runtime.host_pid_file.parent.mkdir(parents=True, exist_ok=True)
            runtime.host_pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
            return SimpleNamespace(pid=os.getpid())

        service.command_runner = launch
        commit = "a" * 40
        host_app = root / "Applications" / "RagImeControl.app"
        host_executable = self._write_canonical_electron_host(host_app, commit=commit)
        env_override = root / "untrusted" / "RagImeControl.app"
        self._write_canonical_electron_host(
            env_override,
            commit=commit,
            marker_overrides={"frontendTransport": "native"},
        )

        with (
            mock.patch.object(Path, "home", return_value=root),
            mock.patch.dict(os.environ, {"RAG_IME_PAW_BROWSER_HOST_APP": str(env_override)}),
        ):
            response = service.start_managed(current_commit=commit, expected_commit=commit)

        self.assertTrue(response["running"])
        self.assertEqual(captured, [str(host_executable)])
        self.assertEqual(captured_environment["PAW_INITIAL_ROUTE"], "/browser")
        self.assertFalse(any("Google Chrome" in item for item in captured))
        self.assertEqual(response["controlProtocol"], "ego-browser")
        self.assertEqual(response["browserTransport"], "cdp")
        self.assertEqual(response["hostKind"], "electron-webview")
        self.assertFalse(response["egoBrowser"]["secondBrowserProcess"])

    def test_managed_browser_rejects_legacy_native_or_webkit_marker(self) -> None:
        root = Path(self.temp.name)
        runtime = FakePawBrowserRuntime(root / "legacy-profile")
        service = BrowserControlService(
            root / "legacy.sqlite",
            app_support_root=root / "legacy-support",
            browser_runtime=runtime,
        )
        service.command_runner = mock.Mock()
        legacy_app = root / "Applications" / "RagImeControl.app"
        self._write_canonical_electron_host(
            legacy_app,
            commit="a" * 40,
            marker_overrides={
                "frontendTransport": "native",
                "browserHost": "webkit",
            },
        )

        with (
            mock.patch.object(Path, "home", return_value=root),
            mock.patch.dict(os.environ, {"RAG_IME_PAW_BROWSER_HOST_APP": str(legacy_app)}),
            self.assertRaisesRegex(BrowserControlError, "同窗 Browser"),
        ):
            service.start_managed(current_commit="a" * 40, expected_commit="a" * 40)

        service.command_runner.assert_not_called()

    def test_browser_host_selector_uses_current_product_name_and_explicit_development_marker(self) -> None:
        root = Path(self.temp.name)
        app = root / "Applications" / "Personal Agent Workbench.app"
        executable = self._write_canonical_electron_host(app, commit="a" * 40)
        marker_path = app / "Contents/Resources/rag-ime-control-web-build-marker.json"
        with mock.patch.object(Path, "home", return_value=root):
            self.assertEqual(self.service._paw_browser_host_executable(current_commit="a" * 40), executable)
            marker = json.loads(marker_path.read_text())
            marker.update(gitDirty=True, sourceDirty=True)
            marker["provenance"]["sourceDirty"] = True
            marker_path.write_text(json.dumps(marker))
            self.assertIsNone(self.service._paw_browser_host_executable(current_commit="a" * 40))
            marker["developmentInstall"] = True
            marker_path.write_text(json.dumps(marker))
            self.assertEqual(self.service._paw_browser_host_executable(current_commit="a" * 40), executable)
            marker["provenance"]["sourceDirty"] = False
            marker_path.write_text(json.dumps(marker))
            self.assertIsNone(self.service._paw_browser_host_executable(current_commit="a" * 40))

    def test_browser_runtime_requires_compiled_host_modules_not_just_cli_wrappers(self) -> None:
        self.service.ego_runtime_root = Path(self.temp.name) / "ego"
        paths = self.service._ego_paths(port=0)
        for key in ("cli", "host", "harness"):
            paths[key].parent.mkdir(parents=True, exist_ok=True)
            paths[key].write_text("// wrapper only")
        self.assertFalse(self.service._ego_runtime_available())
        for name in ("cli.js", "host-control.js"):
            path = self.service.ego_runtime_root / "package/ego-linux-host/dist" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("export {};")
        self.assertTrue(self.service._ego_runtime_available())

    def test_managed_browser_requires_matching_current_and_expected_source_commits(self) -> None:
        root = Path(self.temp.name)
        runtime = FakePawBrowserRuntime(root / "commit-profile")
        service = BrowserControlService(
            root / "commit.sqlite",
            app_support_root=root / "commit-support",
            browser_runtime=runtime,
        )
        service.command_runner = mock.Mock()
        self._write_canonical_electron_host(
            root / "Applications" / "RagImeControl.app",
            commit="a" * 40,
        )

        with (
            mock.patch.object(Path, "home", return_value=root),
            self.assertRaisesRegex(BrowserControlError, "commit"),
        ):
            service.start_managed(current_commit="a" * 40, expected_commit="b" * 40)

        service.command_runner.assert_not_called()

    def test_managed_browser_rejects_stale_or_missing_dist_digest(self) -> None:
        root = Path(self.temp.name)
        runtime = FakePawBrowserRuntime(root / "digest-profile")
        service = BrowserControlService(
            root / "digest.sqlite",
            app_support_root=root / "digest-support",
            browser_runtime=runtime,
        )
        service.command_runner = mock.Mock()
        app = root / "Applications" / "RagImeControl.app"
        self._write_canonical_electron_host(app, commit="a" * 40)
        (app / "Contents" / "Resources" / "app" / "dist" / "assets" / "main.js").write_text(
            "tampered\n",
            encoding="utf-8",
        )

        with (
            mock.patch.object(Path, "home", return_value=root),
            self.assertRaisesRegex(BrowserControlError, "同窗 Browser"),
        ):
            service.start_managed(current_commit="a" * 40, expected_commit="a" * 40)

        service.command_runner.assert_not_called()

        missing_digest_service = BrowserControlService(
            root / "missing-digest.sqlite",
            app_support_root=root / "missing-digest-support",
            browser_runtime=FakePawBrowserRuntime(root / "missing-digest-profile"),
        )
        missing_digest_service.command_runner = mock.Mock()
        missing_digest_app = root / "Applications" / "RagImeControl.app"
        self._write_canonical_electron_host(
            missing_digest_app,
            commit="a" * 40,
            marker_overrides={"distTreeDigest": None},
        )
        with (
            mock.patch.object(Path, "home", return_value=root),
            self.assertRaisesRegex(BrowserControlError, "同窗 Browser"),
        ):
            missing_digest_service.start_managed(
                current_commit="a" * 40,
                expected_commit="a" * 40,
            )
        missing_digest_service.command_runner.assert_not_called()

    def _write_canonical_electron_host(
        self,
        application: Path,
        *,
        commit: str,
        marker_overrides: dict[str, object] | None = None,
    ) -> Path:
        contents = application / "Contents"
        executable = contents / "MacOS" / "RagImeControl"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("", encoding="utf-8")
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleExecutable": "RagImeControl",
                    "CFBundleIdentifier": "com.rag-ime.control",
                },
                handle,
            )

        dist = contents / "Resources" / "app" / "dist"
        (dist / "assets").mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text(
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'self'\"><main>paw-os</main>\n",
            encoding="utf-8",
        )
        (dist / "manifest.webmanifest").write_text("{}\n", encoding="utf-8")
        (dist / "assets" / "main.js").write_text("console.log('paw-os');\n", encoding="utf-8")
        (dist / "rag-ime-control-web-build.json").write_text(
            json.dumps(
                {
                    "schemaVersion": "rag-ime.control-web-build.v1",
                    "buildChannel": "production",
                    "transport": "http",
                    "httpOnly": True,
                    "nativeOnly": False,
                    "frontendProduct": "paw-os",
                    "sourceCommit": commit,
                }
            ),
            encoding="utf-8",
        )
        dist_digest_hash = hashlib.sha256()
        for path in sorted(dist.rglob("*"), key=lambda item: item.relative_to(dist).as_posix()):
            if path.is_file() and path.name != "rag-ime-control-web-build.json":
                dist_digest_hash.update(path.relative_to(dist).as_posix().encode("utf-8"))
                dist_digest_hash.update(b"\0")
                dist_digest_hash.update(path.read_bytes())
                dist_digest_hash.update(b"\0")
        dist_digest = dist_digest_hash.hexdigest()
        inner_marker_path = dist / "rag-ime-control-web-build.json"
        inner_marker = json.loads(inner_marker_path.read_text(encoding="utf-8"))
        inner_marker.update(
            {
                "forbiddenTransportModulesExcluded": True,
                "previewFixturesExcluded": True,
                "distTreeDigest": dist_digest,
            }
        )
        inner_marker_path.write_text(json.dumps(inner_marker), encoding="utf-8")
        marker = {
            "schemaVersion": "rag-ime.control-build-marker.v1",
            "bundleId": "com.rag-ime.control",
            "gitCommit": commit,
            "sourceCommit": commit,
            "gitDirty": False,
            "sourceDirty": False,
            "ui": "control-center-web",
            "channel": "release",
            "frontendTransport": "http",
            "frontendBuildChannel": "production",
            "frontendProduct": "paw-os",
            "forbiddenTransportModulesExcluded": True,
            "browserHost": "electron-webview",
            "browserControl": "ego-browser",
            "browserTransport": "cdp",
            "browserPartition": "persist:paw-browser",
            "sameOriginControlProxy": True,
            "distTreeDigest": dist_digest,
            "provenance": {
                "sourceCommit": commit,
                "sourceDirty": False,
                "frontendProduct": "paw-os",
                "bundleId": "com.rag-ime.control",
                "frontendTransport": "http",
                "browserHost": "electron-webview",
                "browserControl": "ego-browser",
                "browserTransport": "cdp",
                "browserPartition": "persist:paw-browser",
                "sameOriginControlProxy": True,
                "distTreeDigest": dist_digest,
            },
        }
        marker.update(marker_overrides or {})
        marker_path = contents / "Resources" / "rag-ime-control-web-build-marker.json"
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        return executable

    def test_managed_browser_never_falls_back_to_external_chrome(self) -> None:
        root = Path(self.temp.name)
        runtime = FakePawBrowserRuntime(root / "missing-host-profile")
        service = BrowserControlService(
            root / "missing-host.sqlite",
            app_support_root=root / "missing-host-support",
            browser_runtime=runtime,
        )
        service.command_runner = mock.Mock()

        with mock.patch.object(
            service,
            "_paw_browser_host_executable",
            return_value=None,
        ):
            with self.assertRaisesRegex(BrowserControlError, "同窗 Browser"):
                service.start_managed()

        service.command_runner.assert_not_called()

    def test_managed_browser_adopts_the_live_electron_host_without_launching_chrome(self) -> None:
        root = Path(self.temp.name)
        runtime = FakePawBrowserRuntime(root / "electron-profile")
        runtime.host_pid_file.parent.mkdir(parents=True, exist_ok=True)
        runtime.host_pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        service = BrowserControlService(
            root / "electron.sqlite",
            app_support_root=root / "electron-support",
            browser_runtime=runtime,
        )

        with mock.patch.object(service, "_paw_browser_host_executable") as launcher:
            response = service.start_managed()

        launcher.assert_not_called()
        self.assertTrue(response["running"])
        self.assertTrue(response["connected"])
        self.assertEqual(response["hostKind"], "electron-webview")

    def test_zombie_managed_process_is_not_reported_as_running(self) -> None:
        with (
            mock.patch("rag_ime.browser_control.os.waitpid", return_value=(424242, 0)),
            mock.patch("rag_ime.browser_control.os.kill") as kill,
        ):
            running = self.service._pid_running(424242)

        self.assertFalse(running)
        kill.assert_not_called()


class FakePawBrowserRuntime:
    PNG_BYTES = b"\x89PNG\r\n\x1a\nfixture"

    def __init__(self, profile_path: Path) -> None:
        self.profile_path = profile_path
        self.host_pid_file = profile_path / "PAWBrowserHost.pid"
        self.tab_id = PawBrowserRuntime.tab_id("DIRECTTARGET")
        self.actions: list[str] = []

    def port(self) -> int:
        return 9222

    def version(self, _port: int) -> dict[str, object]:
        return {"Browser": "Chrome/Test"}

    def tabs(self, _port: int) -> list[dict[str, object]]:
        return [{
            "deviceId": PawBrowserRuntime.DEVICE_ID,
            "deviceName": PawBrowserRuntime.DISPLAY_NAME,
            "clientKind": "managed",
            "targetId": "DIRECTTARGET",
            "tabId": self.tab_id,
            "title": "Direct",
            "url": "https://example.com",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/DIRECTTARGET",
            "active": True,
            "connected": True,
        }]

    def execute(self, _port: int, action: str, payload: object) -> dict[str, object]:
        self.actions.append(action)
        result: dict[str, object] = {
            "ok": True,
            "summary": f"history:{action}" if action in {"back", "forward"} else "已打开页面",
            "tabId": self.tab_id,
            "url": "https://example.com/managed",
            "title": "Managed",
            "markdown": "# Managed",
            "interactiveCount": 0,
            "viewport": {},
        }
        if action == "screenshot":
            encoded = base64.b64encode(self.PNG_BYTES).decode("ascii")
            result["screenshotDataUrl"] = f"data:image/png;base64,{encoded}"
        return result

    def prepare_launch(self) -> None:
        self.profile_path.mkdir(parents=True, exist_ok=True)

    def launch_command(self, executable: Path) -> list[str]:
        return [
            str(executable),
            f"--user-data-dir={self.profile_path}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
        ]

    def wait_for_port(self) -> int:
        return 9222


class FakeEgoProcess:
    pid = 424243
    returncode = 0

    def __init__(self, trace_path: Path) -> None:
        self.trace_path = trace_path

    def communicate(
        self,
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        del input, timeout
        self.trace_path.write_text(
            "\n".join(
                [
                    '{"schemaVersion":"paw.ego-browser-step.v1","event":"started","action":"click","target":"getByRole(button): Continue","atMs":10}',
                    '{"schemaVersion":"paw.ego-browser-step.v1","event":"completed","action":"click","target":"getByRole(button): Continue","atMs":20}',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return '{"taskSpaceId":2}\n', ""


class BrowserCliOutputTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the CLI pipe check")
    def test_cli_drains_large_stdout_and_stderr_before_exiting(self) -> None:
        entrypoint = Path(__file__).resolve().parents[1] / (
            "integrations/ego-browser/upstream/package/ego-linux-host/bin/ego-browser.mjs"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            (root / "dist").mkdir()
            shutil.copy2(entrypoint, root / "bin/ego-browser.mjs")
            (root / "package.json").write_text('{"type":"module"}')
            # Exercise the real CLI exit path without opening a browser. Keep an
            # unrelated handle alive so draining cannot depend on natural exit.
            (root / "dist/cli.js").write_text(
                'export async function runCli(args) {\n'
                '  process.stdout.write(JSON.stringify({text:"植物".repeat(18000)})+"\\n");\n'
                '  process.stderr.write("diagnostic ".repeat(10000)+"\\n");\n'
                '  setInterval(() => {}, 30000);\n'
                '  return Number(args[0]);\n'
                '}\n'
            )
            for exit_code in (0, 3):
                with self.subTest(exit_code=exit_code):
                    result = subprocess.run(
                        [shutil.which("node"), str(root / "bin/ego-browser.mjs"), str(exit_code)],
                        capture_output=True, timeout=10, check=False,
                    )
                    self.assertEqual(result.returncode, exit_code)
                    self.assertEqual(
                        result.stdout.decode("utf-8"),
                        json.dumps({"text": "植物" * 18000}, ensure_ascii=False, separators=(",", ":")) + "\n",
                    )
                    self.assertEqual(result.stderr, ("diagnostic " * 10000 + "\n").encode())


if __name__ == "__main__":
    unittest.main()
