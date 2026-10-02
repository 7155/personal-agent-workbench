from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class PawBrowserRuntimeError(RuntimeError):
    pass


JsonRequest = Callable[[str, str], object]
CdpRequest = Callable[[str, str, dict[str, object]], object]
ListeningPorts = Callable[[int], list[int]]
HostRequest = Callable[[str, str, dict[str, object], str], object]

CHROMIUM_ERROR_PAGE_URL = "chrome-error://chromewebdata/"
MAX_HOST_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_HOST_ERROR_BYTES = 64 * 1024
MAX_HOST_ERROR_TEXT = 240
BROWSER_LOAD_RECOVERY_HINT = (
    "检查目标地址和服务是否可用；本地预览服务请使用 workspace_job 启动并保持运行，"
    "再在当前标签页重试。"
)


class PawBrowserRuntime:
    """Direct owner of PAW's isolated Chromium DevTools session.

    The runtime talks to Chromium itself. There is no extension, pairing
    protocol, polling worker, or per-site permission broker between the Agent
    and the browser profile created for it.
    """

    DEVICE_ID = "paw-browser"
    DISPLAY_NAME = "PAW Browser"

    def __init__(
        self,
        profile_path: str | Path,
        *,
        json_request: JsonRequest | None = None,
        cdp_request: CdpRequest | None = None,
        listening_ports: ListeningPorts | None = None,
        host_request: HostRequest | None = None,
    ) -> None:
        self.profile_path = Path(profile_path).expanduser().resolve(strict=False)
        self.port_file = self.profile_path / "DevToolsActivePort"
        self.host_pid_file = self.profile_path / "PAWBrowserHost.pid"
        self.host_origin_file = self.profile_path / "PAWBrowserHost.origin"
        self.active_target_file = self.profile_path / "PAWBrowserHost.active-target.json"
        self._json_request = json_request or self._default_json_request
        self._cdp_request = cdp_request or self._default_cdp_request
        self._listening_ports = listening_ports or self._default_listening_ports
        self._host_request = host_request or self._default_host_request

    def launch_command(self, executable: str | Path) -> list[str]:
        return [
            str(executable),
            f"--user-data-dir={self.profile_path}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "about:blank",
        ]

    def prepare_launch(self) -> None:
        self.profile_path.mkdir(parents=True, exist_ok=True)
        try:
            self.port_file.unlink()
        except FileNotFoundError:
            pass

    def wait_for_port(self, *, timeout_seconds: float = 8.0) -> int:
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while time.monotonic() < deadline:
            port = self.port()
            if port:
                try:
                    self.version(port)
                    return port
                except (OSError, ValueError, PawBrowserRuntimeError):
                    pass
            time.sleep(0.08)
        raise PawBrowserRuntimeError("PAW Browser did not expose its local DevTools endpoint")

    def port(self) -> int:
        host_pid = self._live_host_pid()
        if host_pid:
            for candidate in self._listening_ports(host_pid):
                if not 1 <= candidate <= 65535:
                    continue
                try:
                    version = self.version(candidate)
                except (OSError, ValueError, PawBrowserRuntimeError):
                    continue
                if str(version.get("Browser") or ""):
                    return candidate
            # Preview and Release may intentionally share the persistent
            # Browser profile. In that case DevToolsActivePort can belong to a
            # different or already-exited host, so a live Electron handoff must
            # fail closed instead of taking over that ambiguous endpoint.
            return 0
        try:
            first_line = self.port_file.read_text(encoding="utf-8").splitlines()[0]
            port = int(first_line)
        except (FileNotFoundError, IndexError, OSError, ValueError):
            return 0
        return port if 1 <= port <= 65535 else 0

    def _live_host_pid(self) -> int:
        try:
            pid = int(self.host_pid_file.read_text(encoding="utf-8").strip())
            if pid <= 0:
                return 0
            os.kill(pid, 0)
        except (FileNotFoundError, OSError, ValueError):
            return 0
        return pid

    @staticmethod
    def _default_listening_ports(pid: int) -> list[int]:
        try:
            result = subprocess.run(
                ["lsof", "-Pan", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        ports: list[int] = []
        for line in result.stdout.splitlines():
            if not line.startswith("n") or ":" not in line:
                continue
            raw_port = line.rsplit(":", 1)[-1]
            if not raw_port.isdigit():
                continue
            port = int(raw_port)
            if 1 <= port <= 65535 and port not in ports:
                ports.append(port)
        return ports

    def version(self, port: int) -> dict[str, object]:
        value = self._json_request("GET", self._endpoint(port, "/json/version"))
        if not isinstance(value, Mapping):
            raise PawBrowserRuntimeError("Chromium DevTools version response is invalid")
        return dict(value)

    def tabs(self, port: int) -> list[dict[str, object]]:
        value = self._json_request("GET", self._endpoint(port, "/json/list"))
        if not isinstance(value, list):
            raise PawBrowserRuntimeError("Chromium DevTools target list is invalid")
        items: list[dict[str, object]] = []
        for raw in value:
            if not isinstance(raw, Mapping):
                continue
            target_type = str(raw.get("type") or "")
            raw_url = str(raw.get("url") or "")
            if (
                target_type not in {"page", "webview"}
                or raw_url.startswith("file:")
                or "pawHost=electron" in raw_url
            ):
                continue
            target_id = str(raw.get("id") or "")
            websocket_url = str(raw.get("webSocketDebuggerUrl") or "")
            if not target_id or not websocket_url:
                continue
            items.append(
                {
                    "deviceId": self.DEVICE_ID,
                    "deviceName": self.DISPLAY_NAME,
                    "clientKind": "managed",
                    "targetType": target_type,
                    "targetId": target_id,
                    "tabId": self.tab_id(target_id),
                    "title": str(raw.get("title") or ""),
                    "url": str(raw.get("url") or ""),
                    "webSocketDebuggerUrl": websocket_url,
                    "active": len(items) == 0,
                    "connected": True,
                }
            )
        active_target = self._active_target()
        if active_target:
            items.sort(
                key=lambda item: 0
                if (
                    (
                        active_target.get("targetId")
                        and str(item.get("targetId") or "")
                        == active_target.get("targetId")
                    )
                    or (
                        not active_target.get("targetId")
                        and str(item.get("url") or "") == active_target.get("url")
                        and (
                            not active_target.get("title")
                            or str(item.get("title") or "") == active_target.get("title")
                        )
                    )
                )
                else 1
            )
        for index, item in enumerate(items):
            item["active"] = index == 0
        return items

    def _active_target(self) -> dict[str, str]:
        try:
            value = json.loads(self.active_target_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(value, Mapping):
            return {}
        return {
            "targetId": str(value.get("targetId") or ""),
            "title": str(value.get("title") or ""),
            "url": str(value.get("url") or ""),
        }

    def execute(self, port: int, action: str, payload: Mapping[str, object]) -> dict[str, object]:
        if action == "new_tab":
            url = str(payload.get("url") or "about:blank")
            created = self._json_request("PUT", self._endpoint(port, f"/json/new?{quote(url, safe=':/?&=%')}"))
            if not isinstance(created, Mapping):
                raise PawBrowserRuntimeError("Chromium did not create a new page")
            target_id = str(created.get("id") or "")
            return {"ok": True, "summary": "已新建标签页", "targetId": target_id, "tabId": self.tab_id(target_id)}

        target = self._target(
            port,
            payload.get("tabId"),
            create_url=(str(payload.get("url") or "") if action == "navigate" else ""),
        )
        websocket_url = str(target["webSocketDebuggerUrl"])
        if action == "close_tab":
            # A browser keeps one usable tab after the last tab is closed.  The
            # direct CDP surface must preserve that invariant too; otherwise
            # the next Browser refresh has no target to select or render.
            if len(self.tabs(port)) == 1:
                self._cdp_request(websocket_url, "Page.navigate", {"url": "about:blank"})
                time.sleep(0.12)
                return {
                    "ok": True,
                    "summary": "已关闭标签页，已保留空白页",
                    "targetId": target["targetId"],
                    "tabId": target["tabId"],
                    "url": "about:blank",
                }
            self._json_request("GET", self._endpoint(port, f"/json/close/{target['targetId']}"))
            return {"ok": True, "summary": "已关闭标签页", "targetId": target["targetId"], "tabId": target["tabId"]}
        if action == "navigate":
            url = str(payload.get("url") or "")
            navigation = self._cdp_request(websocket_url, "Page.navigate", {"url": url})
            if isinstance(navigation, Mapping) and navigation.get("errorText"):
                error = str(navigation["errorText"])[:240]
                return {
                    "ok": False,
                    "targetId": target["targetId"],
                    "tabId": target["tabId"],
                    "url": url,
                    "error": error,
                    "failureReason": "browser_navigation_failed",
                    "summary": f"页面加载失败：{error}",
                    "recoveryHint": BROWSER_LOAD_RECOVERY_HINT,
                }
            time.sleep(0.18)
        elif action == "reload":
            self._cdp_request(websocket_url, "Page.reload", {"ignoreCache": False})
            time.sleep(0.12)
        elif action in {"back", "forward"}:
            self._evaluate(websocket_url, f"history.{action}()")
            time.sleep(0.12)
        elif action == "click":
            reference = json.dumps(str(payload.get("refId") or ""))
            self._highlight_reference(websocket_url, reference, "点击")
            expression = (
                "(()=>{const e=document.querySelector('[data-paw-ref='+JSON.stringify(" + reference + ")+']');"
                "if(!e)return false;e.scrollIntoView({block:'center',inline:'center'});e.click();return true})()"
            )
            if self._evaluate(websocket_url, expression) is not True:
                raise PawBrowserRuntimeError("The referenced page element is no longer available")
        elif action == "type":
            reference = json.dumps(str(payload.get("refId") or ""))
            self._highlight_reference(websocket_url, reference, "输入")
            typed = json.dumps(str(payload.get("text") or "")[:8_000])
            clear = "true" if payload.get("clear") is not False else "false"
            submit = "true" if payload.get("submit") is True else "false"
            expression = (
                "(()=>{const e=document.querySelector('[data-paw-ref='+JSON.stringify(" + reference + ")+']');"
                "if(!e)return false;e.focus();const t=" + typed + ";"
                "if('value'in e)e.value=" + "t" + " + (" + clear + "?'':String(e.value||''));"
                "else e.textContent=t;e.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:t}));"
                "e.dispatchEvent(new Event('change',{bubbles:true}));"
                "if(" + submit + "){e.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}));"
                "if(e.form)e.form.requestSubmit();e.dispatchEvent(new KeyboardEvent('keyup',{key:'Enter',code:'Enter',bubbles:true}));}return true})()"
            )
            if self._evaluate(websocket_url, expression) is not True:
                raise PawBrowserRuntimeError("The referenced input is no longer available")
        elif action == "scroll":
            direction = str(payload.get("direction") or "down")
            amount = int(payload.get("amount") or 650)
            x = -amount if direction == "left" else amount if direction == "right" else 0
            y = -amount if direction == "up" else amount if direction == "down" else 0
            self._evaluate(websocket_url, f"window.scrollBy({x},{y});true")
        elif action == "wait":
            self._wait_for_text(
                websocket_url,
                str(payload.get("text") or ""),
                int(payload.get("timeoutMs") or 5_000),
            )
        elif action not in {"tabs", "snapshot", "read_page", "screenshot"}:
            raise PawBrowserRuntimeError(f"Unsupported direct browser action: {action}")

        snapshot = self._snapshot(websocket_url)
        result: dict[str, object] = {
            "ok": True,
            "targetId": target["targetId"],
            "tabId": target["tabId"],
            **snapshot,
            "summary": self._summary(action, snapshot),
        }
        if snapshot.get("url") == CHROMIUM_ERROR_PAGE_URL:
            result.update({
                "ok": False,
                "failureReason": "browser_page_load_failed",
                "summary": "页面加载失败，当前显示 Chromium 错误页",
                "recoveryHint": BROWSER_LOAD_RECOVERY_HINT,
            })
        if action == "screenshot":
            result["screenshotDataUrl"] = self._capture_screenshot(
                target=target,
                websocket_url=websocket_url,
            )
        return result

    def _capture_screenshot(
        self,
        *,
        target: Mapping[str, object],
        websocket_url: str,
    ) -> str:
        # Electron exposes PAW Browser pages as `webview` DevTools targets. Its
        # Page.captureScreenshot endpoint can accept the command and then hang
        # until the WebSocket read timeout, while the owning WebContents has a
        # supported capturePage() implementation. Keep CDP for standalone
        # Chromium page targets and route the visible guest through its host.
        if str(target.get("targetType") or "") == "webview" and self._live_host_pid():
            try:
                origin = self.host_origin_file.read_text(encoding="utf-8").strip().rstrip("/")
                token = self.host_pid_file.with_suffix(".token").read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise PawBrowserRuntimeError(
                    "PAW Browser visible guest bridge is unavailable"
                ) from exc
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or not token:
                raise PawBrowserRuntimeError("PAW Browser visible guest bridge is unavailable")
            captured = self._host_request(
                "POST",
                f"{origin}/__paw_browser/screenshot",
                {"targetId": str(target.get("targetId") or "")},
                token,
            )
            if not isinstance(captured, Mapping) or captured.get("ok") is not True:
                raise PawBrowserRuntimeError("PAW Browser host did not return a screenshot")
            encoded = str(captured.get("data") or "")
            mime = str(captured.get("mimeType") or "image/png").lower()
            if not encoded or mime not in {"image/png", "image/jpeg", "image/webp"}:
                raise PawBrowserRuntimeError("PAW Browser host returned an invalid screenshot")
            if len(encoded) > MAX_HOST_RESPONSE_BYTES:
                raise PawBrowserRuntimeError("PAW Browser host screenshot exceeds the response limit")
            return f"data:{mime};base64,{encoded}"

        captured = self._cdp_request(
            websocket_url,
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
        )
        encoded = str(captured.get("data") or "") if isinstance(captured, Mapping) else ""
        if not encoded:
            raise PawBrowserRuntimeError("Chromium did not return a screenshot")
        return f"data:image/png;base64,{encoded}"

    @staticmethod
    def tab_id(target_id: str) -> int:
        if not target_id:
            return 1
        value = int(hashlib.sha256(target_id.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF
        return value or 1

    def _target(
        self,
        port: int,
        raw_tab_id: object,
        *,
        create_url: str = "",
    ) -> dict[str, object]:
        tabs = self.tabs(port)
        if not tabs and create_url:
            return self._create_visible_target(port, create_url)
        if not tabs:
            raise PawBrowserRuntimeError("PAW Browser has no open page")
        if raw_tab_id is None:
            return tabs[0]
        try:
            tab_id = int(raw_tab_id)
        except (TypeError, ValueError) as exc:
            raise PawBrowserRuntimeError("Browser tab id is invalid") from exc
        for tab in tabs:
            if tab["tabId"] == tab_id:
                return tab
        raise PawBrowserRuntimeError("The selected PAW Browser tab is no longer open")

    def _create_visible_target(self, port: int, url: str) -> dict[str, object]:
        if not self._live_host_pid():
            raise PawBrowserRuntimeError("PAW Browser has no open page")
        try:
            origin = self.host_origin_file.read_text(encoding="utf-8").strip().rstrip("/")
            token = self.host_pid_file.with_suffix(".token").read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PawBrowserRuntimeError("PAW Browser visible guest bridge is unavailable") from exc
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or not token:
            raise PawBrowserRuntimeError("PAW Browser visible guest bridge is unavailable")
        created = self._host_request(
            "POST",
            f"{origin}/__paw_browser/tabs",
            {"url": url},
            token,
        )
        if not isinstance(created, Mapping) or created.get("ok") is not True:
            raise PawBrowserRuntimeError("PAW Browser did not create a visible guest")
        target_id = str(created.get("targetId") or "")
        for tab in self.tabs(port):
            if str(tab.get("targetId") or "") == target_id:
                return tab
        raise PawBrowserRuntimeError("PAW Browser visible guest is not available to Agent control")

    def _snapshot(self, websocket_url: str) -> dict[str, object]:
        value = self._evaluate(websocket_url, _SNAPSHOT_EXPRESSION)
        if not isinstance(value, Mapping):
            raise PawBrowserRuntimeError("Chromium returned an invalid page snapshot")
        return dict(value)

    def _evaluate(self, websocket_url: str, expression: str) -> object:
        response = self._cdp_request(
            websocket_url,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if not isinstance(response, Mapping):
            raise PawBrowserRuntimeError("Chromium Runtime.evaluate response is invalid")
        exception = response.get("exceptionDetails")
        if exception:
            raise PawBrowserRuntimeError("Page evaluation failed")
        result = response.get("result")
        return result.get("value") if isinstance(result, Mapping) else None

    def _wait_for_text(self, websocket_url: str, text: str, timeout_ms: int) -> None:
        if not text:
            time.sleep(min(max(timeout_ms, 100), 20_000) / 1000)
            return
        deadline = time.monotonic() + min(max(timeout_ms, 100), 20_000) / 1000
        encoded = json.dumps(text[:500])
        while time.monotonic() < deadline:
            if self._evaluate(websocket_url, f"(document.body?.innerText||'').includes({encoded})") is True:
                return
            time.sleep(0.1)
        raise PawBrowserRuntimeError("Timed out waiting for page text")

    def _highlight_reference(self, websocket_url: str, reference: str, label: str) -> None:
        encoded_label = json.dumps(label)
        self._evaluate(
            websocket_url,
            "(()=>{"
            "const e=document.querySelector('[data-paw-ref='+JSON.stringify(" + reference + ")+']');"
            "if(!e)return false;const b=e.getBoundingClientRect();"
            "let p=document.getElementById('__paw_ego_pointer__');"
            "if(!p){p=document.createElement('div');p.id='__paw_ego_pointer__';"
            "Object.assign(p.style,{position:'fixed',width:'22px',height:'22px',border:'2px solid #0d9488',"
            "borderRadius:'999px',boxShadow:'0 0 0 5px rgba(13,148,136,.18)',background:'rgba(255,255,255,.9)',"
            "pointerEvents:'none',zIndex:'2147483647',transform:'translate(-50%,-50%)'});document.documentElement.appendChild(p);}"
            "p.style.left=(b.left+b.width/2)+'px';p.style.top=(b.top+b.height/2)+'px';"
            "p.animate([{opacity:0,scale:.62},{opacity:1,scale:1},{opacity:.92,scale:.86}],"
            "{duration:520,easing:'cubic-bezier(.2,.8,.2,1)',fill:'forwards'});"
            "clearTimeout(window.__pawEgoPointerTimer);window.__pawEgoPointerTimer=setTimeout(()=>p?.remove(),1100);"
            "let s=document.getElementById('__paw_ego_state__');if(!s){s=document.createElement('div');s.id='__paw_ego_state__';"
            "Object.assign(s.style,{position:'fixed',right:'18px',top:'18px',padding:'8px 11px',color:'#0f172a',"
            "background:'rgba(255,255,255,.92)',border:'1px solid rgba(13,148,136,.42)',borderRadius:'8px',"
            "boxShadow:'0 10px 30px rgba(15,23,42,.16)',font:'600 12px/1.35 system-ui,sans-serif',"
            "pointerEvents:'none',zIndex:'2147483646'});document.documentElement.appendChild(s);}"
            "s.textContent='Agent · '+" + encoded_label + ";clearTimeout(window.__pawEgoStateTimer);"
            "window.__pawEgoStateTimer=setTimeout(()=>s?.remove(),1800);return true})()",
        )

    @staticmethod
    def _summary(action: str, snapshot: Mapping[str, object]) -> str:
        title = str(snapshot.get("title") or "未命名页面")
        labels = {
            "navigate": "已打开",
            "back": "已后退到",
            "forward": "已前进到",
            "reload": "已刷新",
            "click": "已点击页面元素：",
            "type": "已输入并更新：",
            "scroll": "已滚动：",
            "wait": "页面等待完成：",
            "snapshot": "已读取",
            "read_page": "已读取",
            "screenshot": "已截取",
            "tabs": "已读取",
        }
        return f"{labels.get(action, '已操作')}《{title}》"

    @staticmethod
    def _endpoint(port: int, path: str) -> str:
        if not 1 <= int(port) <= 65535:
            raise PawBrowserRuntimeError("Chromium DevTools port is invalid")
        return f"http://127.0.0.1:{int(port)}{path}"

    @staticmethod
    def _default_json_request(method: str, url: str) -> object:
        request = Request(url, method=method, headers={"Accept": "application/json"})
        with urlopen(request, timeout=2.0) as response:
            payload = response.read(2_000_000)
        # DevTools discovery uses JSON except for the successful close receipt.
        if urlsplit(url).path.startswith("/json/close/") and payload.strip() == b"Target is closing":
            return {}
        return json.loads(payload.decode("utf-8")) if payload else {}

    @staticmethod
    def _default_host_request(
        method: str,
        url: str,
        payload: dict[str, object],
        token: str,
    ) -> object:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=encoded,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "X-PAW-Browser-Token": token,
            },
        )
        try:
            with urlopen(request, timeout=10.0) as response:
                body = response.read(MAX_HOST_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            try:
                error_body = exc.read(MAX_HOST_ERROR_BYTES + 1)
            finally:
                exc.close()
            if len(error_body) > MAX_HOST_ERROR_BYTES:
                raise PawBrowserRuntimeError(
                    f"PAW Browser host returned HTTP {exc.code}; error body exceeds the error limit"
                ) from exc
            detail = ""
            if error_body:
                try:
                    parsed = json.loads(error_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    detail = error_body.decode("utf-8", "replace")
                else:
                    if isinstance(parsed, Mapping):
                        detail = str(parsed.get("error") or parsed.get("message") or "")
                    else:
                        detail = str(parsed)
            detail = " ".join(detail.split())[:MAX_HOST_ERROR_TEXT]
            suffix = f": {detail}" if detail else ""
            raise PawBrowserRuntimeError(
                f"PAW Browser host returned HTTP {exc.code}{suffix}"
            ) from exc
        if len(body) > MAX_HOST_RESPONSE_BYTES:
            raise PawBrowserRuntimeError("PAW Browser host response exceeds the response limit")
        return json.loads(body.decode("utf-8")) if body else {}

    @staticmethod
    def _default_cdp_request(websocket_url: str, method: str, params: dict[str, object]) -> object:
        return _WebSocketClient(websocket_url).request(method, params)


class _WebSocketClient:
    def __init__(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise PawBrowserRuntimeError("PAW Browser DevTools websocket must be local")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path + (f"?{parsed.query}" if parsed.query else "")

    def request(self, method: str, params: dict[str, object]) -> object:
        connection = socket.create_connection((self.host, self.port), timeout=4.0)
        try:
            self._handshake(connection)
            request_id = secrets.randbelow(2_000_000_000) + 1
            self._send_frame(connection, json.dumps({"id": request_id, "method": method, "params": params}).encode("utf-8"))
            while True:
                opcode, payload = self._receive_frame(connection)
                if opcode == 0x9:
                    self._send_frame(connection, payload, opcode=0xA)
                    continue
                if opcode == 0x8:
                    raise PawBrowserRuntimeError("Chromium closed the DevTools websocket")
                if opcode != 0x1:
                    continue
                message = json.loads(payload.decode("utf-8"))
                if isinstance(message, Mapping) and message.get("id") == request_id:
                    if message.get("error"):
                        raise PawBrowserRuntimeError(str(message["error"]))
                    result = message.get("result")
                    return dict(result) if isinstance(result, Mapping) else {}
        finally:
            connection.close()

    def _handshake(self, connection: socket.socket) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        connection.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response and len(response) < 16_384:
            chunk = connection.recv(2_048)
            if not chunk:
                break
            response.extend(chunk)
        if not bytes(response).startswith(b"HTTP/1.1 101"):
            raise PawBrowserRuntimeError("Chromium rejected the DevTools websocket handshake")

    @staticmethod
    def _send_frame(connection: socket.socket, payload: bytes, *, opcode: int = 0x1) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65_536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        connection.sendall(bytes(header) + mask + masked)

    @staticmethod
    def _receive_frame(connection: socket.socket) -> tuple[int, bytes]:
        first = _read_exact(connection, 2)
        opcode = first[0] & 0x0F
        length = first[1] & 0x7F
        masked = bool(first[1] & 0x80)
        if length == 126:
            length = struct.unpack("!H", _read_exact(connection, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _read_exact(connection, 8))[0]
        if length > 16 * 1024 * 1024:
            raise PawBrowserRuntimeError("Chromium DevTools frame exceeds the 16 MiB limit")
        mask = _read_exact(connection, 4) if masked else b""
        payload = _read_exact(connection, length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return opcode, payload


def _read_exact(connection: socket.socket, length: int) -> bytes:
    output = bytearray()
    while len(output) < length:
        chunk = connection.recv(length - len(output))
        if not chunk:
            raise PawBrowserRuntimeError("Chromium closed the DevTools connection")
        output.extend(chunk)
    return bytes(output)


_SNAPSHOT_EXPRESSION = r"""
(()=>{
  const clean=(value,limit=500)=>String(value||'').replace(/\s+/g,' ').trim().slice(0,limit);
  const interactive=[...document.querySelectorAll('a[href],button,input,textarea,select,[role=button],[contenteditable=true]')]
    .filter((element)=>{const box=element.getBoundingClientRect();const style=getComputedStyle(element);return box.width>0&&box.height>0&&style.visibility!=='hidden'&&style.display!=='none';})
    .slice(0,500);
  const lines=[];
  const elements=[];
  interactive.forEach((element,index)=>{
    const ref=`0:e${index+1}`;element.setAttribute('data-paw-ref',ref);
    const box=element.getBoundingClientRect();
    const role=element.getAttribute('role')||element.tagName.toLowerCase();
    const label=clean(element.getAttribute('aria-label')||element.innerText||element.getAttribute('placeholder')||element.getAttribute('title')||element.value,180);
    elements.push({refId:ref,tag:element.tagName.toLowerCase(),role,label,inputType:clean(element.getAttribute('type'),40),x:box.left,y:box.top,width:box.width,height:box.height});
    lines.push(`- [${ref}] ${role} "${label.replace(/"/g,"'")}"`);
  });
  const blocks=[...document.querySelectorAll('h1,h2,h3,p,li,article')].slice(0,700).map((element)=>clean(element.innerText,600)).filter(Boolean);
  const title=clean(document.title||location.hostname,500);
  const markdown=[`# ${title}`,`URL: ${location.href}`,...blocks,...lines].join('\n').slice(0,160000);
  return {title,url:location.href,markdown,interactiveCount:interactive.length,viewport:{width:innerWidth,height:innerHeight,scrollX,scrollY,devicePixelRatio,elements}};
})()
"""
