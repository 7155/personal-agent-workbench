from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .paw_browser_runtime import (
    BROWSER_LOAD_RECOVERY_HINT,
    CHROMIUM_ERROR_PAGE_URL,
    PawBrowserRuntime,
    PawBrowserRuntimeError,
)


SCHEMA_VERSION = "rag-ime.browser-control.v1"
READ_ACTIONS = frozenset({"tabs", "snapshot", "read_page", "screenshot"})
WRITE_ACTIONS = frozenset(
    {
        "run",
        "navigate",
        "new_tab",
        "close_tab",
        "reload",
        "back",
        "forward",
        "click",
        "type",
        "scroll",
        "wait",
    }
)
ALLOWED_ACTIONS = READ_ACTIONS | WRITE_ACTIONS
MAX_SNAPSHOT_MARKDOWN = 160_000
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
MAX_RESULT_JSON = 256_000
MAX_EGO_SCRIPT_CHARS = 24_000
MAX_EGO_OUTPUT_CHARS = 120_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 15.0
CLIENT_CONNECTED_TTL_MS = 30_000


class BrowserControlError(ValueError):
    pass


class BrowserControlService:
    """Own PAW's isolated Chromium and its durable direct-control trace."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        app_support_root: str | Path | None = None,
        command_runner: Any | None = None,
        browser_runtime: PawBrowserRuntime | None = None,
        ego_runtime_root: str | Path | None = None,
        ego_check_runner: Any | None = None,
        ego_process_runner: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)
        self.app_support_root = Path(
            app_support_root
            or os.environ.get("RAG_IME_APP_SUPPORT_DIR")
            or Path.home() / "Library" / "Application Support" / "RagIme"
        ).expanduser()
        self.command_runner = command_runner or subprocess.Popen
        self.browser_runtime = browser_runtime or PawBrowserRuntime(
            self.app_support_root / "Browser" / "runtime-profile"
        )
        self.ego_runtime_root = Path(
            ego_runtime_root
            or os.environ.get("RAG_IME_EGO_BROWSER_ROOT")
            or Path(__file__).resolve().parents[1]
            / "integrations"
            / "ego-browser"
            / "upstream"
        ).expanduser().resolve(strict=False)
        self.ego_check_runner = ego_check_runner or subprocess.run
        self.ego_process_runner = ego_process_runner or subprocess.Popen
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS browser_control_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_control_clients (
                    device_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    client_kind TEXT NOT NULL,
                    extension_version TEXT NOT NULL,
                    browser_name TEXT NOT NULL,
                    active_tab_id INTEGER,
                    last_seen_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_control_tabs (
                    device_id TEXT NOT NULL,
                    tab_id INTEGER NOT NULL,
                    window_id INTEGER,
                    url TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 0,
                    last_seen_ms INTEGER NOT NULL,
                    PRIMARY KEY(device_id, tab_id)
                );
                CREATE INDEX IF NOT EXISTS idx_browser_tabs_seen
                    ON browser_control_tabs(last_seen_ms DESC);
                CREATE TABLE IF NOT EXISTS browser_control_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    tab_id INTEGER NOT NULL,
                    frame_id INTEGER NOT NULL DEFAULT 0,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    markdown TEXT NOT NULL,
                    interactive_count INTEGER NOT NULL DEFAULT 0,
                    viewport_json TEXT NOT NULL DEFAULT '{}',
                    screenshot_mime TEXT,
                    screenshot_bytes BLOB,
                    created_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_browser_snapshots_device_tab
                    ON browser_control_snapshots(device_id, tab_id, created_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_browser_snapshots_created
                    ON browser_control_snapshots(created_at_ms DESC);
                CREATE TABLE IF NOT EXISTS browser_control_commands (
                    command_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    failure_reason TEXT,
                    created_at_ms INTEGER NOT NULL,
                    claimed_at_ms INTEGER,
                    completed_at_ms INTEGER,
                    claimed_by TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_browser_commands_queue
                    ON browser_control_commands(device_id, status, created_at_ms);
                CREATE TABLE IF NOT EXISTS browser_control_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    command_id TEXT,
                    detail_json TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_browser_events_created
                    ON browser_control_events(created_at_ms DESC);
                """
            )
            connection.execute(
                """
                UPDATE browser_control_commands
                SET status='failed',
                    result_json=?,
                    failure_reason='direct_browser_interrupted',
                    completed_at_ms=?
                WHERE status='claimed'
                  AND claimed_by='paw-cdp-direct'
                """,
                (json.dumps({"ok": False}, separators=(",", ":")), self._now_ms()),
            )
            connection.commit()

    def push_snapshot(self, payload: Mapping[str, object]) -> dict[str, object]:
        device_id = self._identifier(payload.get("deviceId"), field="deviceId")
        tab_id = self._positive_int(payload.get("tabId"))
        snapshot_id = self._identifier(
            payload.get("snapshotId") or f"snap_{uuid.uuid4().hex}",
            field="snapshotId",
        )
        raw_url = str(payload.get("url") or "").strip()
        page_url = self._page_url(raw_url, allow_blank=True)
        markdown = str(payload.get("markdown") or "")[:MAX_SNAPSHOT_MARKDOWN]
        if raw_url and raw_url != page_url:
            markdown = markdown.replace(raw_url, page_url)
        screenshot_mime, screenshot_bytes = self._decode_screenshot(payload.get("screenshotDataUrl"))
        now = self._now_ms()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO browser_control_snapshots(
                    snapshot_id, device_id, tab_id, frame_id, url, title, summary,
                    markdown, interactive_count, viewport_json, screenshot_mime,
                    screenshot_bytes, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    device_id,
                    tab_id,
                    self._positive_int(payload.get("frameId"), allow_zero=True, allow_none=True) or 0,
                    page_url,
                    self._text(payload.get("title"), maximum=500),
                    self._text(payload.get("summary"), maximum=1_200),
                    markdown,
                    self._positive_int(payload.get("interactiveCount"), allow_zero=True, allow_none=True) or 0,
                    json.dumps(payload.get("viewport") if isinstance(payload.get("viewport"), Mapping) else {}),
                    screenshot_mime,
                    screenshot_bytes,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE browser_control_clients
                SET active_tab_id=?, last_seen_ms=?
                WHERE device_id=?
                """,
                (tab_id, now, device_id),
            )
            connection.execute(
                """
                INSERT INTO browser_control_tabs(
                    device_id, tab_id, window_id, url, title, active, last_seen_ms
                ) VALUES (?, ?, NULL, ?, ?, 1, ?)
                ON CONFLICT(device_id, tab_id) DO UPDATE SET
                    url=excluded.url,
                    title=excluded.title,
                    last_seen_ms=excluded.last_seen_ms
                """,
                (
                    device_id,
                    tab_id,
                    page_url,
                    self._text(payload.get("title"), maximum=500),
                    now,
                ),
            )
            self._event(
                connection,
                "snapshot",
                device_id,
                {
                    "snapshotId": snapshot_id,
                    "tabId": tab_id,
                    "title": self._text(payload.get("title"), maximum=180),
                    "url": page_url,
                    "interactiveCount": self._positive_int(
                        payload.get("interactiveCount"),
                        allow_zero=True,
                        allow_none=True,
                    )
                    or 0,
                },
            )
            connection.commit()
        return {"schemaVersion": SCHEMA_VERSION, "ok": True, "snapshotId": snapshot_id}

    def status(self, *, agent_safe: bool = False) -> dict[str, object]:
        self._sync_direct_browser()
        now = self._now_ms()
        with self._connection() as connection:
            clients = [
                self._public_client(row, now=now)
                for row in connection.execute(
                    "SELECT * FROM browser_control_clients ORDER BY last_seen_ms DESC"
                ).fetchall()
            ]
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM browser_control_commands GROUP BY status"
                ).fetchall()
            }
            snapshot = self._latest_snapshot_row(connection)
        response: dict[str, object] = {
            "schemaVersion": SCHEMA_VERSION,
            "ok": True,
            "mode": "managed",
            "connected": any(client["connected"] for client in clients),
            "clients": clients,
            "commandCounts": counts,
            "managedBrowser": self.managed_status(),
            "latestSnapshot": self._public_snapshot(snapshot, include_markdown=False) if snapshot else None,
            "summary": (
                "PAW Browser 已就绪，Agent 可直接操作"
                if any(client["connected"] for client in clients)
                else "PAW Browser 尚未启动"
            ),
        }
        return response

    def tabs(self) -> dict[str, object]:
        direct_tabs = self._sync_direct_browser()
        target_ids = {
            int(item["tabId"]): str(item.get("targetId") or "")
            for item in (direct_tabs or [])
            if item.get("tabId") is not None
        }
        now = self._now_ms()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT tabs.*, clients.display_name, clients.client_kind
                FROM browser_control_tabs tabs
                INNER JOIN browser_control_clients clients
                    ON clients.device_id=tabs.device_id
                WHERE clients.last_seen_ms>=?
                    AND clients.client_kind='managed'
                ORDER BY tabs.active DESC, tabs.last_seen_ms DESC, tabs.tab_id ASC
                LIMIT 100
                """,
                (now - CLIENT_CONNECTED_TTL_MS,),
            ).fetchall()
            items = []
            for row in rows:
                device_id = str(row["device_id"])
                tab_id = int(row["tab_id"])
                snapshot = self._latest_snapshot_row(
                    connection,
                    device_id=device_id,
                    tab_id=tab_id,
                )
                item = self._public_snapshot(snapshot, include_markdown=False) if snapshot else {
                    "snapshotId": "",
                    "deviceId": device_id,
                    "tabId": tab_id,
                    "frameId": 0,
                    "pageSummary": "",
                    "interactiveCount": 0,
                    "viewport": {},
                    "hasScreenshot": False,
                    "imagePath": "",
                    "createdAtMs": int(row["last_seen_ms"]),
                }
                item.update(
                    {
                        "targetId": target_ids.get(tab_id, ""),
                        "url": str(row["url"]),
                        "title": str(row["title"]),
                        "active": bool(row["active"]),
                        "deviceName": str(row["display_name"]),
                        "clientKind": str(row["client_kind"]),
                        "connected": True,
                    }
                )
                items.append(item)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "ok": True,
            "items": items,
            "summary": f"已读取 {len(items)} 个浏览器标签页",
            "liveSnapshot": direct_tabs is not None,
        }

    def latest_snapshot(
        self,
        *,
        device_id: str = "",
        tab_id: int | None = None,
        include_markdown: bool = True,
    ) -> dict[str, object]:
        with self._connection() as connection:
            row = self._latest_snapshot_row(connection, device_id=device_id, tab_id=tab_id)
        if row is None:
            current = self.managed_status()
            port = int(current.get("debugPort") or 0)
            if port:
                direct = self.browser_runtime.execute(
                    port,
                    "snapshot",
                    {"tabId": tab_id} if tab_id else {},
                )
                self._record_direct_snapshot(direct)
                with self._connection() as connection:
                    row = self._latest_snapshot_row(
                        connection,
                        device_id=PawBrowserRuntime.DEVICE_ID,
                        tab_id=tab_id,
                    )
        if row is None:
            raise BrowserControlError("browser snapshot is unavailable")
        page_failed = row["url"] == CHROMIUM_ERROR_PAGE_URL
        return {
            "schemaVersion": SCHEMA_VERSION,
            "ok": not page_failed,
            **self._public_snapshot(row, include_markdown=include_markdown),
            "summary": (
                "页面加载失败，当前显示 Chromium 错误页"
                if page_failed
                else f"已读取《{str(row['title']) or '未命名页面'}》的页面快照"
            ),
            **({
                "failureReason": "browser_page_load_failed",
                "recoveryHint": BROWSER_LOAD_RECOVERY_HINT,
            } if page_failed else {}),
        }

    def snapshot_image(self, snapshot_id: str) -> tuple[str, bytes]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT screenshot_mime, screenshot_bytes FROM browser_control_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        if row is None or not row["screenshot_bytes"]:
            raise BrowserControlError("snapshot image is unavailable")
        return str(row["screenshot_mime"] or "image/png"), bytes(row["screenshot_bytes"])

    def submit_command(
        self,
        action: str,
        payload: Mapping[str, object],
        *,
        session_id: str = "",
        timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        normalized_action = str(action or "").strip()
        if normalized_action not in ALLOWED_ACTIONS:
            raise BrowserControlError(f"unsupported browser action: {normalized_action}")
        if not bool(self.managed_status().get("running")):
            self.start_managed()
        self._sync_direct_browser()
        device_id = self._select_device(requested=str(payload.get("deviceId") or ""))
        command_id = f"bcmd_{uuid.uuid4().hex}"
        command_payload = self._normalize_command(normalized_action, payload)
        now = self._now_ms()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO browser_control_commands(
                    command_id, device_id, session_id, action, payload_json,
                    status, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    command_id,
                    device_id,
                    self._text(session_id, maximum=240),
                    normalized_action,
                    json.dumps(command_payload, ensure_ascii=False),
                    now,
                ),
            )
            self._event(
                connection,
                "command_queued",
                device_id,
                {"action": normalized_action},
                command_id=command_id,
            )
            connection.commit()

        return self._execute_direct_command(
            command_id=command_id,
            action=normalized_action,
            payload=command_payload,
        )

    def complete_command(self, payload: Mapping[str, object]) -> dict[str, object]:
        command_id = self._identifier(payload.get("commandId"), field="commandId")
        result = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
        result = dict(result)
        if result.get("url"):
            result["url"] = self._page_url(result.get("url"))
        screenshot_data_url = result.pop("screenshotDataUrl", None)
        page_markdown = result.pop("markdown", None)
        failure_reason = self._text(result.get("failureReason") or result.get("error"), maximum=240)
        status = "completed" if result.get("ok", True) is not False else "failed"
        with self._connection() as connection:
            record = connection.execute(
                "SELECT * FROM browser_control_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if record is None:
                raise BrowserControlError("browser command not found")
            if record["status"] not in {"queued", "claimed"}:
                raise BrowserControlError(f"browser command is already {record['status']}")
            if screenshot_data_url or page_markdown:
                snapshot_id = self._store_command_snapshot(
                    connection,
                    record=record,
                    result=result,
                    screenshot_data_url=screenshot_data_url,
                    markdown=page_markdown,
                )
                result["snapshotId"] = snapshot_id
                result["imagePath"] = f"/api/browser/snapshots/{snapshot_id}/image"
            encoded = json.dumps(result, ensure_ascii=False)
            if len(encoded.encode("utf-8")) > MAX_RESULT_JSON:
                result = {
                    "ok": result.get("ok", True) is not False,
                    "summary": self._text(result.get("summary"), maximum=1_200),
                    "url": self._page_url(result.get("url"), allow_blank=True),
                    "title": self._text(result.get("title"), maximum=500),
                    "truncated": True,
                }
                encoded = json.dumps(result, ensure_ascii=False)
            completed_at = self._now_ms()
            connection.execute(
                """
                UPDATE browser_control_commands
                SET status=?, result_json=?, failure_reason=?, completed_at_ms=?
                WHERE command_id=?
                """,
                (status, encoded, failure_reason, completed_at, command_id),
            )
            self._touch_client(connection, str(record["device_id"]))
            self._event(
                connection,
                "command_completed" if status == "completed" else "command_failed",
                str(record["device_id"]),
                {"action": record["action"], "failureReason": failure_reason},
                command_id=command_id,
            )
            connection.commit()
        return {"schemaVersion": SCHEMA_VERSION, "ok": True, "commandId": command_id}

    def traces(self, *, limit: int = 50) -> dict[str, object]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM browser_control_commands
                ORDER BY created_at_ms DESC
                LIMIT ?
                """,
                (min(max(int(limit), 1), 200),),
            ).fetchall()
            items = [
                self._public_trace(
                    row,
                    steps=[
                        self._json_object(step["detail_json"])
                        for step in connection.execute(
                            """
                            SELECT detail_json FROM browser_control_events
                            WHERE command_id=? AND kind='ego_trace_step'
                            ORDER BY created_at_ms ASC, event_id ASC
                            """,
                            (str(row["command_id"]),),
                        ).fetchall()
                    ],
                )
                for row in rows
            ]
        return {
            "schemaVersion": SCHEMA_VERSION,
            "ok": True,
            "items": items,
            "summary": f"已读取 {len(items)} 条浏览器执行轨迹",
        }

    def stop(self) -> dict[str, object]:
        now = self._now_ms()
        stopped_runner = self._stop_ego_runner()
        with self._connection() as connection:
            cancelled = connection.execute(
                """
                UPDATE browser_control_commands
                SET status='cancelled', failure_reason='cancelled_by_user', completed_at_ms=?
                WHERE status IN ('queued', 'claimed')
                """,
                (now,),
            ).rowcount
            self._event(
                connection,
                "stop_requested",
                "",
                {"cancelled": cancelled, "egoRunnerStopped": stopped_runner},
            )
            connection.commit()
        return {
            "schemaVersion": SCHEMA_VERSION,
            "ok": True,
            "cancelled": cancelled,
            "egoRunnerStopped": stopped_runner,
            "summary": (
                f"已停止 {cancelled} 个待执行浏览器操作"
                + ("，并终止当前 ego-browser 脚本" if stopped_runner else "")
            ),
        }

    def managed_status(self) -> dict[str, object]:
        with self._connection() as connection:
            raw_pid = self._setting(connection, "managed_pid") or ""
        stored_pid = int(raw_pid) if raw_pid.isdigit() else 0
        host_pid_file = getattr(self.browser_runtime, "host_pid_file", None)
        electron_host_pid = self._read_pid(host_pid_file) if isinstance(host_pid_file, Path) else 0
        pid = electron_host_pid if self._pid_running(electron_host_pid) else stored_pid
        process_running = self._pid_running(pid)
        port = self.browser_runtime.port() if process_running else 0
        connected = False
        browser_version = ""
        if port:
            try:
                version = self.browser_runtime.version(port)
                connected = True
                browser_version = str(version.get("Browser") or "")
            except (OSError, ValueError, PawBrowserRuntimeError):
                connected = False
        ego_paths = self._ego_paths(port=port)
        ego_host_pid = self._read_pid(ego_paths["pid"])
        return {
            "running": process_running,
            "connected": connected,
            "pid": pid if process_running else None,
            "hostKind": "electron-webview" if pid and pid == electron_host_pid else "chromium-window",
            "debugPort": port if connected else None,
            "browserVersion": browser_version,
            "profilePath": str(self.browser_runtime.profile_path),
            "controlProtocol": "ego-browser",
            "browserTransport": "cdp",
            "egoBrowser": {
                "available": self._ego_runtime_available(),
                "hostRunning": self._pid_running(ego_host_pid),
                "hostPid": ego_host_pid if self._pid_running(ego_host_pid) else None,
                "taskSpacesPath": str(ego_paths["data"] / "spaces.json"),
                "secondBrowserProcess": False,
            },
        }

    def start_managed(
        self,
        *,
        current_commit: str = "",
        expected_commit: str = "",
    ) -> dict[str, object]:
        commit_pair = self._caller_commit_pair(
            current_commit=current_commit,
            expected_commit=expected_commit,
        )
        if commit_pair is None:
            raise BrowserControlError(
                "Browser host source commit does not match the caller current/expected commit"
            )
        current_source_commit, expected_source_commit = commit_pair
        current = self.managed_status()
        if current["running"]:
            self._sync_direct_browser()
            return {
                "schemaVersion": SCHEMA_VERSION,
                "ok": True,
                **self.managed_status(),
                "summary": "PAW Browser 已在运行",
            }
        host_executable = self._paw_browser_host_executable(
            current_commit=current_source_commit,
            expected_commit=expected_source_commit,
        )
        if host_executable is None:
            raise BrowserControlError(
                "PAW 同窗 Browser 宿主未安装；不会启动外部 Chrome"
            )
        self._stop_ego_host()
        self.browser_runtime.prepare_launch()
        with self._connection() as connection:
            self._set_setting(connection, "managed_pid", "")
            connection.commit()
        environment = dict(os.environ)
        environment["PAW_INITIAL_ROUTE"] = "/browser"
        try:
            process = self.command_runner(
                [str(host_executable)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=environment,
            )
        except BaseException:
            raise
        pid = int(getattr(process, "pid", 0) or 0)
        if pid <= 0:
            raise BrowserControlError("托管浏览器未返回有效进程号")
        with self._connection() as connection:
            self._set_setting(connection, "managed_pid", str(pid))
            self._event(connection, "managed_started", "", {"pid": pid})
            connection.commit()
        try:
            self.browser_runtime.wait_for_port()
            self._sync_direct_browser()
        except BaseException:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            with self._connection() as connection:
                self._set_setting(connection, "managed_pid", "")
                connection.commit()
            raise
        return {
            "schemaVersion": SCHEMA_VERSION,
            "ok": True,
            **self.managed_status(),
            "summary": "PAW 同窗 Browser 已启动，Agent 正通过 Ego/CDP 连接",
        }

    def stop_managed(self) -> dict[str, object]:
        current = self.managed_status()
        self._stop_ego_runner()
        self._stop_ego_host()
        pid = int(current.get("pid") or 0)
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        with self._connection() as connection:
            self._set_setting(connection, "managed_pid", "")
            self._event(connection, "managed_stopped", "", {"pid": pid})
            connection.execute(
                "DELETE FROM browser_control_clients WHERE device_id=?",
                (PawBrowserRuntime.DEVICE_ID,),
            )
            connection.execute(
                "DELETE FROM browser_control_tabs WHERE device_id=?",
                (PawBrowserRuntime.DEVICE_ID,),
            )
            connection.commit()
        return {
            "schemaVersion": SCHEMA_VERSION,
            "ok": True,
            "running": False,
            "summary": "PAW Browser 已停止",
        }

    def _sync_direct_browser(self) -> list[dict[str, object]] | None:
        current = self.managed_status()
        port = int(current.get("debugPort") or 0)
        if not port:
            return None if current.get("running") else []
        try:
            tabs = self.browser_runtime.tabs(port)
        except (OSError, ValueError, PawBrowserRuntimeError):
            return None
        now = self._now_ms()
        active_tab_id = int(tabs[0]["tabId"]) if tabs else None
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO browser_control_clients(
                    device_id, display_name, client_kind, extension_version,
                    browser_name, active_tab_id, last_seen_ms
                ) VALUES (?, ?, 'managed', 'direct-cdp', 'Chromium', ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    client_kind='managed',
                    extension_version='direct-cdp',
                    browser_name='Chromium',
                    active_tab_id=excluded.active_tab_id,
                    last_seen_ms=excluded.last_seen_ms
                """,
                (
                    PawBrowserRuntime.DEVICE_ID,
                    PawBrowserRuntime.DISPLAY_NAME,
                    active_tab_id,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM browser_control_tabs WHERE device_id=?",
                (PawBrowserRuntime.DEVICE_ID,),
            )
            for index, tab in enumerate(tabs):
                raw_url = str(tab.get("url") or "")
                try:
                    public_url = self._page_url(raw_url, allow_blank=True)
                except BrowserControlError:
                    public_url = ""
                connection.execute(
                    """
                    INSERT INTO browser_control_tabs(
                        device_id, tab_id, window_id, url, title, active, last_seen_ms
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        PawBrowserRuntime.DEVICE_ID,
                        int(tab["tabId"]),
                        public_url,
                        self._text(tab.get("title"), maximum=500),
                        1 if index == 0 else 0,
                        now,
                    ),
                )
            connection.commit()
        return tabs

    def _execute_direct_command(
        self,
        *,
        command_id: str,
        action: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        current = self.managed_status()
        port = int(current.get("debugPort") or 0)
        if not port:
            raise BrowserControlError("PAW Browser is not connected")
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE browser_control_commands
                SET status='claimed', claimed_at_ms=?, claimed_by='paw-cdp-direct'
                WHERE command_id=? AND status='queued'
                """,
                (self._now_ms(), command_id),
            )
            connection.commit()
        try:
            if action == "run":
                direct_result = self._run_ego_script(
                    command_id=command_id,
                    port=port,
                    payload=payload,
                )
            else:
                direct_result = self.browser_runtime.execute(port, action, payload)
        except (OSError, ValueError, PawBrowserRuntimeError) as exc:
            direct_result = {
                "ok": False,
                "failureReason": "direct_browser_error",
                "error": self._text(exc, maximum=240),
            }
        with self._connection() as connection:
            status_row = connection.execute(
                "SELECT status FROM browser_control_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
        if status_row is not None and status_row["status"] in {"queued", "claimed"}:
            self.complete_command({"commandId": command_id, "result": direct_result})
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM browser_control_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
        assert row is not None
        result = self._json_object(row["result_json"])
        return {
            "schemaVersion": SCHEMA_VERSION,
            "ok": row["status"] == "completed" and result.get("ok", True) is not False,
            "commandId": command_id,
            "action": action,
            "status": str(row["status"]),
            "durationMs": max(0, int(row["completed_at_ms"] or self._now_ms()) - int(row["created_at_ms"])),
            "failureReason": str(row["failure_reason"] or ""),
            "result": result,
            "summary": self._command_summary(action, row["status"], result),
        }

    def _run_ego_script(
        self,
        *,
        command_id: str,
        port: int,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        script = str(payload.get("script") or "")
        timeout_ms = min(max(int(payload.get("timeoutMs") or 60_000), 1_000), 120_000)
        node = self._ego_node()
        paths = self._ego_paths(port=port)
        environment = self._ego_environment(port=port, node=node)
        trace_path = paths["data"] / "traces" / f"{command_id}.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        trace_path.unlink(missing_ok=True)
        environment["EGO_PAW_TRACE_PATH"] = str(trace_path)
        self._ensure_ego_host(node=node, environment=environment)

        with self._connection() as connection:
            active_pid = int(self._setting(connection, "ego_runner_pid") or "0")
            if self._pid_running(active_pid):
                raise BrowserControlError("another ego-browser script is already running")
            self._set_setting(connection, "ego_runner_pid", "")
            self._set_setting(connection, "ego_runner_command_id", "")
            connection.commit()

        command = [
            str(node),
            "--permission",
            f"--allow-fs-read={self.ego_runtime_root}",
            f"--allow-fs-read={paths['data']}",
            f"--allow-fs-write={trace_path}",
            str(paths["cli"]),
        ]
        try:
            process = self.ego_process_runner(
                command,
                cwd=str(self.ego_runtime_root),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise BrowserControlError(f"could not start ego-browser: {exc}") from exc
        pid = int(getattr(process, "pid", 0) or 0)
        if pid <= 0:
            raise BrowserControlError("ego-browser did not return a valid process id")
        with self._connection() as connection:
            self._set_setting(connection, "ego_runner_pid", str(pid))
            self._set_setting(connection, "ego_runner_command_id", command_id)
            connection.commit()

        timed_out = False
        trace_stop = threading.Event()
        trace_collector = threading.Thread(
            target=self._collect_ego_trace,
            args=(command_id, trace_path, trace_stop),
            daemon=True,
        )
        trace_collector.start()
        try:
            stdout, stderr = process.communicate(
                input=script,
                timeout=timeout_ms / 1000.0,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_group(pid)
            stdout, stderr = process.communicate()
        finally:
            trace_stop.set()
            trace_collector.join(timeout=1.0)
            with self._connection() as connection:
                if self._setting(connection, "ego_runner_pid") == str(pid):
                    self._set_setting(connection, "ego_runner_pid", "")
                    self._set_setting(connection, "ego_runner_command_id", "")
                connection.commit()

        output = str(stdout or "")[:MAX_EGO_OUTPUT_CHARS]
        error_output = str(stderr or "")[:MAX_EGO_OUTPUT_CHARS]
        return_code = int(getattr(process, "returncode", 1) or 0)
        ok = not timed_out and return_code == 0
        return {
            "ok": ok,
            "summary": (
                "ego-browser 脚本已完成"
                if ok
                else "ego-browser 脚本超时"
                if timed_out
                else "ego-browser 脚本失败"
            ),
            "stdout": output,
            "stderr": error_output,
            "exitCode": return_code,
            "timedOut": timed_out,
            "failureReason": (
                "ego_browser_timeout"
                if timed_out
                else "ego_browser_script_failed"
                if return_code != 0
                else ""
            ),
            "controlProtocol": "ego-browser",
            "secondBrowserProcess": False,
        }

    def _ego_runtime_available(self) -> bool:
        paths = self._ego_paths(port=0)
        return all(
            path.is_file()
            for path in (paths["cli"], paths["host"], paths["harness"], paths["cliModule"], paths["hostModule"])
        )

    def _ego_paths(self, *, port: int) -> dict[str, Path]:
        data = self.app_support_root / "Browser" / "ego-browser"
        runtime = data / "runtime"
        host_package = self.ego_runtime_root / "package" / "ego-linux-host"
        return {
            "data": data,
            "runtime": runtime,
            "socket": runtime / "host.sock",
            "pid": runtime / "host.pid",
            "cli": host_package / "bin" / "ego-browser.mjs",
            "host": host_package / "bin" / "ego-linux-hostd.mjs",
            "cliModule": host_package / "dist" / "cli.js",
            "hostModule": host_package / "dist" / "host-control.js",
            "harness": self.ego_runtime_root
            / "package"
            / "ego-browser"
            / "dist"
            / "src"
            / "run.js",
            "profile": self.browser_runtime.profile_path,
        }

    def _ego_environment(self, *, port: int, node: Path) -> dict[str, str]:
        paths = self._ego_paths(port=port)
        paths["data"].mkdir(parents=True, exist_ok=True, mode=0o700)
        paths["runtime"].mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", str(node.parent)),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "EGO_CONFIG_DIR": str(paths["data"] / "config"),
            "EGO_DATA_DIR": str(paths["data"]),
            "EGO_RUNTIME_DIR": str(paths["runtime"]),
            "EGO_HOST_SOCK": str(paths["socket"]),
            "EGO_USER_DATA_DIR": str(paths["profile"]),
            "EGO_CDP_PORT": str(port),
            "EGO_BROWSER_AGENT_WORKSPACE": str(
                self.ego_runtime_root / "skills" / "ego-browser"
            ),
            "EGO_HEADLESS": "0",
            "EGO_PAW_REUSE_SELECTED_TARGET": "1",
            "EGO_PAW_ACTIVE_TARGET_FILE": str(
                getattr(
                    self.browser_runtime,
                    "active_target_file",
                    self.browser_runtime.profile_path
                    / "PAWBrowserHost.active-target.json",
                )
            ),
        }
        try:
            environment["EGO_PAW_HOST_ORIGIN"] = (
                self.browser_runtime.host_origin_file.read_text(encoding="utf-8").strip()
            )
            environment["EGO_PAW_HOST_TOKEN"] = (
                self.browser_runtime.host_pid_file.with_suffix(".token")
                .read_text(encoding="utf-8")
                .strip()
            )
        except (AttributeError, OSError):
            pass
        temporary = os.environ.get("TMPDIR")
        if temporary:
            environment["TMPDIR"] = temporary
        return environment

    def _ensure_ego_host(self, *, node: Path, environment: Mapping[str, str]) -> None:
        if not self._ego_runtime_available():
            raise BrowserControlError(
                "PAW Browser runtime is missing; build with scripts/build_ego_browser_runtime.py "
                "and install its output under app/integrations/ego-browser/upstream"
            )

        def doctor() -> Mapping[str, object]:
            result = self.ego_check_runner(
                [str(node), str(self._ego_paths(port=0)["cli"]), "--doctor"],
                cwd=str(self.ego_runtime_root),
                env=dict(environment),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20.0,
                check=False,
            )
            if int(getattr(result, "returncode", 1) or 0) != 0:
                detail = self._text(getattr(result, "stderr", ""), maximum=500)
                raise BrowserControlError(
                    "ego-browser host did not become ready"
                    + (f": {detail}" if detail else "")
                )
            try:
                payload = json.loads(str(getattr(result, "stdout", "") or ""))
            except json.JSONDecodeError as exc:
                raise BrowserControlError(
                    "ego-browser doctor returned invalid diagnostics"
                ) from exc
            if not isinstance(payload, Mapping):
                raise BrowserControlError("ego-browser doctor returned invalid diagnostics")
            return payload

        expected_port = int(environment.get("EGO_CDP_PORT") or 0)
        expected_profile = Path(
            str(environment.get("EGO_USER_DATA_DIR") or "")
        ).expanduser().resolve(strict=False)

        def owns_current_browser(payload: Mapping[str, object]) -> bool:
            try:
                actual_port = int(payload.get("cdpPort") or 0)
                actual_profile = Path(str(payload.get("profileDir") or "")).expanduser().resolve(
                    strict=False
                )
            except (TypeError, ValueError, OSError):
                return False
            return (
                payload.get("ok") is True
                and payload.get("cdpUp") is True
                and actual_port == expected_port
                and actual_profile == expected_profile
            )

        diagnostics = doctor()
        if owns_current_browser(diagnostics):
            return

        # The Unix socket can outlive the random Electron CDP listener it was
        # created for. Restart only the Ego Host so it adopts the current PAW
        # Browser authority; never fall through to launching another Chrome.
        self._stop_ego_host()
        diagnostics = doctor()
        if not owns_current_browser(diagnostics):
            actual_port = self._text(diagnostics.get("cdpPort"), maximum=12) or "unknown"
            state = "up" if diagnostics.get("cdpUp") is True else "down"
            raise BrowserControlError(
                "ego-browser host is not attached to the current PAW Browser "
                f"(expected CDP {expected_port}, got {actual_port} {state})"
            )

    def _stop_ego_runner(self) -> bool:
        with self._connection() as connection:
            raw_pid = self._setting(connection, "ego_runner_pid") or ""
            pid = int(raw_pid) if raw_pid.isdigit() else 0
            self._set_setting(connection, "ego_runner_pid", "")
            self._set_setting(connection, "ego_runner_command_id", "")
            connection.commit()
        if not self._pid_running(pid):
            return False
        self._terminate_process_group(pid)
        return True

    def _stop_ego_host(self) -> bool:
        paths = self._ego_paths(port=0)
        pid = self._read_pid(paths["pid"])
        if not self._pid_running(pid):
            return False
        try:
            node = self._ego_node()
            environment = self._ego_environment(port=0, node=node)
            result = self.ego_check_runner(
                [str(node), str(paths["host"]), "stop"],
                cwd=str(self.ego_runtime_root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=8.0,
                check=False,
            )
            if int(getattr(result, "returncode", 1) or 0) == 0:
                return True
        except (OSError, subprocess.SubprocessError, BrowserControlError):
            pass
        self._terminate_process_group(pid)
        return True

    def _ego_node(self) -> Path:
        candidates = [
            os.environ.get("RAG_IME_EGO_NODE"),
            os.environ.get("RAG_IME_PI_NODE"),
            os.environ.get("RAG_IME_MANAGED_NODE"),
        ]
        try:
            from .managed_pi_runtime import discover_managed_pi_runtime

            candidates.append(
                discover_managed_pi_runtime(self.app_support_root).node_executable
            )
        except (OSError, ValueError, RuntimeError):
            pass
        candidates.append(shutil.which("node"))
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).expanduser().resolve(strict=False)
            if path.is_file() and os.access(path, os.X_OK):
                return path
        raise BrowserControlError("Node.js 22 or newer is unavailable for ego-browser")

    @staticmethod
    def _read_pid(path: Path) -> int:
        try:
            raw = path.read_text(encoding="utf-8").strip()
            return int(raw) if raw.isdigit() else 0
        except OSError:
            return 0

    @staticmethod
    def _terminate_process_group(pid: int) -> None:
        if pid <= 0:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def _record_direct_snapshot(self, result: Mapping[str, object]) -> None:
        self.push_snapshot(
            {
                "deviceId": PawBrowserRuntime.DEVICE_ID,
                "tabId": result.get("tabId") or 1,
                "url": result.get("url") or "",
                "title": result.get("title") or "",
                "summary": result.get("summary") or "",
                "markdown": result.get("markdown") or "",
                "interactiveCount": result.get("interactiveCount") or 0,
                "viewport": result.get("viewport") or {},
                "screenshotDataUrl": result.get("screenshotDataUrl") or "",
            }
        )

    def _select_device(self, *, requested: str) -> str:
        if requested and requested != PawBrowserRuntime.DEVICE_ID:
            raise BrowserControlError("browser commands can only target PAW Browser")
        now = self._now_ms()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM browser_control_clients WHERE device_id=? AND client_kind='managed'",
                (PawBrowserRuntime.DEVICE_ID,),
            ).fetchone()
        if row is None or now - int(row["last_seen_ms"]) > CLIENT_CONNECTED_TTL_MS:
            raise BrowserControlError("PAW Browser is not connected")
        return PawBrowserRuntime.DEVICE_ID

    def _normalize_command(self, action: str, payload: Mapping[str, object]) -> dict[str, object]:
        result: dict[str, object] = {}
        if action == "run":
            script = str(payload.get("script") or "")
            if not script.strip() or len(script) > MAX_EGO_SCRIPT_CHARS or "\x00" in script:
                raise BrowserControlError(
                    f"ego-browser script must contain 1-{MAX_EGO_SCRIPT_CHARS} characters"
                )
            result["script"] = script
            result["timeoutMs"] = min(
                max(int(payload.get("timeoutMs") or 60_000), 1_000),
                120_000,
            )
            return result
        if payload.get("tabId") is not None:
            result["tabId"] = self._positive_int(payload.get("tabId"))
        if payload.get("refId") is not None:
            result["refId"] = self._identifier(payload.get("refId"), field="refId")
        if action in {"navigate", "new_tab"}:
            result["url"] = self._url(payload.get("url"))
        if action == "type":
            result["text"] = str(payload.get("text") or "")[:8_000]
            result["clear"] = payload.get("clear") is not False
            result["submit"] = payload.get("submit") is True
        if action == "scroll":
            direction = str(payload.get("direction") or "down")
            if direction not in {"up", "down", "left", "right"}:
                raise BrowserControlError("scroll direction is invalid")
            result["direction"] = direction
            result["amount"] = min(max(int(payload.get("amount") or 650), 80), 2_400)
        if action == "wait":
            result["text"] = str(payload.get("text") or "")[:500]
            result["timeoutMs"] = min(max(int(payload.get("timeoutMs") or 5_000), 100), 20_000)
        return result

    def _store_command_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        record: sqlite3.Row,
        result: dict[str, object],
        screenshot_data_url: object,
        markdown: object,
    ) -> str:
        mime, data = self._decode_screenshot(screenshot_data_url)
        latest = self._latest_snapshot_row(
            connection,
            device_id=str(record["device_id"]),
            tab_id=self._positive_int(result.get("tabId"), allow_none=True),
        )
        snapshot_id = f"snap_{uuid.uuid4().hex}"
        connection.execute(
            """
            INSERT INTO browser_control_snapshots(
                snapshot_id, device_id, tab_id, frame_id, url, title, summary,
                markdown, interactive_count, viewport_json, screenshot_mime,
                screenshot_bytes, created_at_ms
            ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                str(record["device_id"]),
                self._positive_int(result.get("tabId"), allow_none=True)
                or (int(latest["tab_id"]) if latest else 1),
                self._page_url(result.get("url"), allow_blank=True) or (str(latest["url"]) if latest else ""),
                self._text(result.get("title"), maximum=500) or (str(latest["title"]) if latest else ""),
                self._text(result.get("summary"), maximum=1_200)
                or ("可视页面截图" if data else "页面操作后的结构化快照"),
                str(markdown or "")[:MAX_SNAPSHOT_MARKDOWN]
                or (str(latest["markdown"]) if latest else ""),
                self._positive_int(
                    result.get("interactiveCount"),
                    allow_zero=True,
                    allow_none=True,
                )
                or (int(latest["interactive_count"]) if latest else 0),
                json.dumps(result.get("viewport") if isinstance(result.get("viewport"), Mapping) else {})
                if isinstance(result.get("viewport"), Mapping)
                else (str(latest["viewport_json"]) if latest else "{}"),
                mime,
                data,
                self._now_ms(),
            ),
        )
        return snapshot_id

    def _latest_snapshot_row(
        self,
        connection: sqlite3.Connection,
        *,
        device_id: str = "",
        tab_id: int | None = None,
    ) -> sqlite3.Row | None:
        conditions: list[str] = []
        values: list[object] = []
        if device_id:
            conditions.append("device_id=?")
            values.append(device_id)
        if tab_id:
            conditions.append("tab_id=?")
            values.append(tab_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return connection.execute(
            f"SELECT * FROM browser_control_snapshots {where} ORDER BY created_at_ms DESC LIMIT 1",
            values,
        ).fetchone()

    def _public_client(self, row: sqlite3.Row, *, now: int) -> dict[str, object]:
        last_seen = int(row["last_seen_ms"])
        return {
            "deviceId": str(row["device_id"]),
            "displayName": str(row["display_name"]),
            "clientKind": str(row["client_kind"]),
            "browserName": str(row["browser_name"]),
            "activeTabId": row["active_tab_id"],
            "lastSeenMs": last_seen,
            "connected": now - last_seen <= CLIENT_CONNECTED_TTL_MS,
        }

    def _public_snapshot(self, row: sqlite3.Row, *, include_markdown: bool) -> dict[str, object]:
        snapshot_id = str(row["snapshot_id"])
        result: dict[str, object] = {
            "snapshotId": snapshot_id,
            "deviceId": str(row["device_id"]),
            "tabId": int(row["tab_id"]),
            "frameId": int(row["frame_id"]),
            "url": str(row["url"]),
            "title": str(row["title"]),
            "pageSummary": str(row["summary"]),
            "interactiveCount": int(row["interactive_count"]),
            "viewport": self._json_object(row["viewport_json"]),
            "hasScreenshot": bool(row["screenshot_bytes"]),
            "imagePath": f"/api/browser/snapshots/{snapshot_id}/image" if row["screenshot_bytes"] else "",
            "createdAtMs": int(row["created_at_ms"]),
        }
        if include_markdown:
            result["markdown"] = str(row["markdown"])
        return result

    def _public_trace(
        self,
        row: sqlite3.Row,
        *,
        steps: list[Mapping[str, object]] | None = None,
    ) -> dict[str, object]:
        created = int(row["created_at_ms"])
        completed = int(row["completed_at_ms"]) if row["completed_at_ms"] else None
        payload = self._json_object(row["payload_json"])
        session_id = str(row["session_id"])
        target = (
            self._text(payload.get("url"), maximum=320)
            or self._text(payload.get("refId"), maximum=160)
            or ("Task Space" if str(row["action"]) == "run" else "")
        )
        return {
            "commandId": str(row["command_id"]),
            "deviceId": str(row["device_id"]),
            "sessionId": session_id,
            "sourceKind": "human" if session_id == "control-center" else "agent",
            "action": str(row["action"]),
            "target": target,
            "targetRefId": self._text(payload.get("refId"), maximum=160),
            "tabId": payload.get("tabId"),
            "status": str(row["status"]),
            "createdAtMs": created,
            "claimedAtMs": int(row["claimed_at_ms"]) if row["claimed_at_ms"] else None,
            "completedAtMs": completed,
            "durationMs": max(0, completed - created) if completed else None,
            "failureReason": str(row["failure_reason"] or ""),
            "steps": [self._public_ego_step(step) for step in (steps or [])],
            "result": self._trace_result(self._json_object(row["result_json"])),
        }

    def _collect_ego_trace(
        self,
        command_id: str,
        trace_path: Path,
        stop: threading.Event,
    ) -> None:
        position = 0
        pending = ""
        while True:
            try:
                with trace_path.open("r", encoding="utf-8") as stream:
                    stream.seek(position)
                    chunk = stream.read()
                    position = stream.tell()
            except FileNotFoundError:
                chunk = ""
            pending += chunk
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, Mapping):
                    self._record_ego_step(command_id, value)
            if stop.is_set():
                break
            stop.wait(0.06)

    def _record_ego_step(
        self,
        command_id: str,
        value: Mapping[str, object],
    ) -> None:
        if value.get("schemaVersion") != "paw.ego-browser-step.v1":
            return
        event = self._text(value.get("event"), maximum=24)
        action = self._text(value.get("action"), maximum=48)
        if event not in {"started", "completed", "failed"} or not action:
            return
        detail = {
            "event": event,
            "action": action,
            "target": self._text(value.get("target"), maximum=320),
            "atMs": self._positive_int(value.get("atMs"), allow_none=True)
            or self._now_ms(),
            "error": self._text(value.get("error"), maximum=240),
        }
        with self._connection() as connection:
            self._event(
                connection,
                "ego_trace_step",
                PawBrowserRuntime.DEVICE_ID,
                detail,
                command_id=command_id,
            )
            connection.commit()
        if event == "completed" and action in {
            "navigate",
            "reload",
            "click",
            "hover",
            "drag",
            "type",
            "press",
            "select",
            "check",
            "uncheck",
            "upload",
            "new_tab",
            "switch_tab",
            "close_tab",
        }:
            current = self.managed_status()
            port = int(current.get("debugPort") or 0)
            if port:
                try:
                    self._record_direct_snapshot(
                        self.browser_runtime.execute(port, "screenshot", {})
                    )
                except (OSError, ValueError, PawBrowserRuntimeError):
                    pass

    def _public_ego_step(self, value: Mapping[str, object]) -> dict[str, object]:
        return {
            "event": self._text(value.get("event"), maximum=24),
            "action": self._text(value.get("action"), maximum=48),
            "target": self._text(value.get("target"), maximum=320),
            "atMs": self._positive_int(value.get("atMs"), allow_none=True),
            "error": self._text(value.get("error"), maximum=240),
        }

    def _trace_result(self, value: Mapping[str, object]) -> dict[str, object]:
        return {
            key: child
            for key, child in value.items()
            if key in {"ok", "summary", "url", "title", "tabId", "snapshotId", "imagePath"}
        }

    def _touch_client(self, connection: sqlite3.Connection, device_id: str) -> None:
        connection.execute(
            "UPDATE browser_control_clients SET last_seen_ms=? WHERE device_id=?",
            (self._now_ms(), device_id),
        )

    def _event(
        self,
        connection: sqlite3.Connection,
        kind: str,
        device_id: str,
        detail: Mapping[str, object],
        *,
        command_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO browser_control_events(kind, device_id, command_id, detail_json, created_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (kind, device_id, command_id, json.dumps(dict(detail), ensure_ascii=False), self._now_ms()),
        )
        connection.execute(
            """
            DELETE FROM browser_control_events
            WHERE event_id NOT IN (
                SELECT event_id FROM browser_control_events ORDER BY created_at_ms DESC LIMIT 1000
            )
            """
        )

    def _setting(self, connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute(
            "SELECT value FROM browser_control_settings WHERE key=?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row else None

    def _set_setting(self, connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            """
            INSERT INTO browser_control_settings(key, value, updated_at_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at_ms=excluded.updated_at_ms
            """,
            (key, value, self._now_ms()),
        )

    def _decode_screenshot(self, value: object) -> tuple[str | None, bytes | None]:
        text = str(value or "")
        if not text:
            return None, None
        if not text.startswith("data:image/") or ";base64," not in text:
            raise BrowserControlError("screenshotDataUrl must be a base64 image data URL")
        header, encoded = text.split(",", 1)
        mime = header[5:].split(";", 1)[0].lower()
        if mime not in {"image/png", "image/jpeg", "image/webp"}:
            raise BrowserControlError("browser screenshot mime type is not supported")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise BrowserControlError("browser screenshot base64 is invalid") from exc
        if not data or len(data) > MAX_SCREENSHOT_BYTES:
            raise BrowserControlError("browser screenshot exceeds the 8 MiB limit")
        return mime, data

    def _paw_browser_host_executable(
        self,
        *,
        current_commit: str = "",
        expected_commit: str = "",
    ) -> Path | None:
        commit_pair = self._caller_commit_pair(
            current_commit=current_commit,
            expected_commit=expected_commit,
        )
        if commit_pair is None:
            return None
        _current_source_commit, expected_source_commit = commit_pair

        # The production selector intentionally ignores RAG_IME_PAW_BROWSER_HOST_APP.
        # A caller-controlled app path can otherwise make an old WebKit/native
        # bundle win over the canonical installed Electron host.
        candidates = (
            Path.home() / "Applications" / "Personal Agent Workbench.app",
            Path("/Applications/Personal Agent Workbench.app"),
            Path.home() / "Applications" / "RagImeControl.app",
            Path("/Applications/RagImeControl.app"),
        )
        for application in candidates:
            marker_path = (
                application
                / "Contents"
                / "Resources"
                / "rag-ime-control-web-build-marker.json"
            )
            info_path = application / "Contents" / "Info.plist"
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                with info_path.open("rb") as handle:
                    info = plistlib.load(handle)
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            if not isinstance(marker, Mapping) or not isinstance(info, Mapping):
                continue
            if not self._canonical_browser_marker(
                application,
                marker,
                info,
                expected_commit=expected_source_commit,
            ):
                continue
            executable_name = str(info.get("CFBundleExecutable") or "").strip()
            executable = application / "Contents" / "MacOS" / executable_name
            if executable_name and executable.is_file():
                return executable
        return None

    def _canonical_browser_marker(
        self,
        application: Path,
        marker: Mapping[str, object],
        info: Mapping[str, object],
        *,
        expected_commit: str,
    ) -> bool:
        required_marker = {
            "schemaVersion": "rag-ime.control-build-marker.v1",
            "bundleId": "com.rag-ime.control",
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
        }
        if any(marker.get(key) != value for key, value in required_marker.items()):
            return False
        if info.get("CFBundleIdentifier") != "com.rag-ime.control":
            return False
        if "swiftFallback" in marker:
            return False
        source_dirty = marker.get("sourceDirty")
        if not isinstance(source_dirty, bool) or marker.get("gitDirty") is not source_dirty:
            return False
        if source_dirty and marker.get("developmentInstall") is not True:
            return False

        if any(
            str(marker.get(key) or "").strip().lower() != expected_commit
            for key in ("gitCommit", "sourceCommit")
        ):
            return False

        provenance = marker.get("provenance")
        if not isinstance(provenance, Mapping):
            return False
        required_provenance = {
            "sourceCommit": expected_commit,
            "sourceDirty": source_dirty,
            "frontendProduct": "paw-os",
            "bundleId": "com.rag-ime.control",
            "frontendTransport": "http",
            "browserHost": "electron-webview",
            "browserControl": "ego-browser",
            "browserTransport": "cdp",
            "browserPartition": "persist:paw-browser",
            "sameOriginControlProxy": True,
        }
        if any(provenance.get(key) != value for key, value in required_provenance.items()):
            return False

        dist = application / "Contents" / "Resources" / "app" / "dist"
        if not self._valid_browser_dist(dist, expected_commit=expected_commit):
            return False
        computed_digest = self._dist_tree_sha256(dist)
        declared_digest = self._normalize_sha256(marker.get("distTreeDigest"))
        provenance_digest = self._normalize_sha256(provenance.get("distTreeDigest"))
        return bool(declared_digest) and declared_digest == computed_digest == provenance_digest

    def _valid_browser_dist(self, dist: Path, *, expected_commit: str) -> bool:
        if not dist.is_dir() or dist.is_symlink():
            return False
        if not (dist / "index.html").is_file():
            return False
        if not (dist / "manifest.webmanifest").is_file():
            return False
        if not (dist / "assets").is_dir():
            return False
        marker_path = dist / "rag-ime-control-web-build.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(marker, Mapping):
            return False
        if marker.get("schemaVersion") != "rag-ime.control-web-build.v1":
            return False
        if marker.get("buildChannel") != "production":
            return False
        if marker.get("transport") != "http":
            return False
        if marker.get("frontendProduct") != "paw-os":
            return False
        if marker.get("nativeOnly") is not False:
            return False
        if marker.get("httpOnly") is not True:
            return False
        if marker.get("forbiddenTransportModulesExcluded") is not True:
            return False
        if marker.get("previewFixturesExcluded") is not True:
            return False
        inner_commit = str(marker.get("sourceCommit") or "").strip().lower()
        inner_digest = self._normalize_sha256(marker.get("distTreeDigest"))
        return (
            bool(inner_commit)
            and inner_commit == expected_commit
            and bool(inner_digest)
            and inner_digest == self._dist_tree_sha256(dist)
        )

    @staticmethod
    def _normalize_sha256(value: object) -> str:
        digest = str(value or "").strip().lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            return ""
        return digest

    @classmethod
    def _dist_tree_sha256(cls, dist: Path) -> str:
        digest = hashlib.sha256()
        try:
            paths = sorted(
                dist.rglob("*"),
                key=lambda path: path.relative_to(dist).as_posix(),
            )
            for path in paths:
                if path.is_symlink():
                    return ""
                if path.is_file() and path.name != "rag-ime-control-web-build.json":
                    digest.update(path.relative_to(dist).as_posix().encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
                    digest.update(b"\0")
        except OSError:
            return ""
        return digest.hexdigest()

    @classmethod
    def _caller_commit_pair(
        cls,
        *,
        current_commit: str,
        expected_commit: str,
    ) -> tuple[str, str] | None:
        current = str(current_commit or "").strip().lower() or cls._repository_commit()
        expected = str(expected_commit or "").strip().lower() or current
        if not cls._is_commit(current) or not cls._is_commit(expected) or current != expected:
            return None
        return current, expected

    @staticmethod
    def _is_commit(value: str) -> bool:
        return len(value) == 40 and all(character in "0123456789abcdef" for character in value)

    @staticmethod
    def _repository_commit() -> str:
        source_root = Path(
            os.environ.get("RAG_IME_SOURCE_ROOT")
            or Path(__file__).resolve().parents[1]
        ).expanduser()
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source_root,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        commit = str(result.stdout or "").strip().lower()
        return commit if result.returncode == 0 and BrowserControlService._is_commit(commit) else ""

    @staticmethod
    def _pid_running(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            waited_pid, _status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                return False
            if waited_pid == 0:
                return True
        except (ChildProcessError, OSError):
            pass
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _text(value: object, *, maximum: int) -> str:
        return " ".join(str(value or "").split())[:maximum]

    @staticmethod
    def _identifier(value: object, *, field: str) -> str:
        text = str(value or "").strip()
        if not text or len(text) > 160 or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for character in text):
            raise BrowserControlError(f"{field} is invalid")
        return text

    @staticmethod
    def _positive_int(
        value: object,
        *,
        allow_zero: bool = False,
        allow_none: bool = False,
    ) -> int | None:
        if value is None and allow_none:
            return None
        if isinstance(value, bool):
            raise BrowserControlError("numeric browser field is invalid")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise BrowserControlError("numeric browser field is invalid") from exc
        minimum = 0 if allow_zero else 1
        if number < minimum:
            raise BrowserControlError("numeric browser field is invalid")
        return number

    @staticmethod
    def _url(value: object, *, allow_blank: bool = False) -> str:
        text = str(value or "").strip()
        if allow_blank and not text:
            return ""
        if text in {"about:blank", "chrome://history", "chrome://history/"}:
            return text
        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise BrowserControlError("browser URL must use http or https")
        try:
            parsed.port
        except ValueError as exc:
            raise BrowserControlError("browser URL port is invalid") from exc
        return text[:4_000]

    @staticmethod
    def _page_url(value: object, *, allow_blank: bool = False) -> str:
        # Chromium's observed error page is evidence, not a navigation request.
        # Preserve it so failed commands settle and snapshots remain readable;
        # _url still rejects internal error schemes as navigation input.
        if str(value or "").strip() == CHROMIUM_ERROR_PAGE_URL:
            return CHROMIUM_ERROR_PAGE_URL
        text = BrowserControlService._url(value, allow_blank=allow_blank)
        if not text:
            return ""
        if text in {"about:blank", "chrome://history", "chrome://history/"}:
            return text
        parsed = urlsplit(text)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        query = urlencode(
            [
                (
                    key,
                    "[redacted]" if BrowserControlService._sensitive_query_key(key) else child,
                )
                for key, child in parse_qsl(parsed.query, keep_blank_values=True)
            ],
            doseq=True,
        )
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))[:4_000]

    @staticmethod
    def _sensitive_query_key(value: str) -> bool:
        normalized = value.lower().replace("-", "_")
        return any(
            marker in normalized
            for marker in (
                "token",
                "secret",
                "password",
                "passwd",
                "auth",
                "session",
                "jwt",
                "signature",
                "api_key",
                "code",
                "state",
            )
        )

    @staticmethod
    def _json_object(value: object) -> dict[str, object]:
        if isinstance(value, Mapping):
            return dict(value)
        if not value:
            return {}
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    @staticmethod
    def _command_summary(action: str, status: object, result: Mapping[str, object]) -> str:
        provided = " ".join(str(result.get("summary") or result.get("error") or "").split())
        if status != "completed":
            return (f"浏览器操作 {action} 未完成：{provided}" if provided else f"浏览器操作 {action} 未完成")[:1_200]
        return provided[:1_200] or f"浏览器操作 {action} 已完成"
