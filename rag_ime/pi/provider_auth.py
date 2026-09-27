from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from rag_ime.keychain_secrets import (
    MODEL_KEYCHAIN_SERVICE, TYPESAFE_ACCOUNT, read_keychain_secret, write_keychain_secret,
    delete_keychain_secret,
)
from rag_ime.pi.config import PiRuntimeConfig


_PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ALLOWED_ACTIONS = frozenset(
    {"set_api_key", "logout", "oauth_browser", "oauth_device_code"}
)
_OAUTH_TIMEOUT_SECONDS = 15 * 60
_CONFIRM_TEXT = {
    "set_api_key": "replace",
    "logout": "logout",
    "oauth_browser": "connect",
    "oauth_device_code": "connect",
}


class PiProviderAuthError(ValueError):
    pass


@dataclass(frozen=True)
class PiProviderBridgeConfig:
    node_executable: str
    package_entry: Path | None
    agent_dir: Path
    bridge_script: Path
    managed_bridge_entry: Path | None = None
    timeout_seconds: float = 15.0
    oauth_timeout_seconds: float = _OAUTH_TIMEOUT_SECONDS
    provider_environment: Mapping[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_runtime(
        cls,
        runtime: PiRuntimeConfig,
        *,
        repo_root: str | Path | None = None,
    ) -> PiProviderBridgeConfig:
        root = Path(repo_root).expanduser().resolve(strict=False) if repo_root else None
        package_entry = _discover_pi_package_entry(runtime, repo_root=root)
        managed_bridge_entry = (
            runtime.executable.parent / "provider-bridge.mjs"
            if runtime.executable is not None
            else None
        )
        return cls(
            node_executable=runtime.node_executable or "node",
            package_entry=package_entry,
            agent_dir=runtime.agent_dir.expanduser().resolve(strict=False),
            bridge_script=Path(__file__).resolve().parents[1] / "node" / "pi_provider_bridge.mjs",
            managed_bridge_entry=managed_bridge_entry,
            timeout_seconds=max(5.0, min(float(runtime.command_timeout_seconds), 60.0)),
            oauth_timeout_seconds=_OAUTH_TIMEOUT_SECONDS,
            provider_environment={
                str(key): str(value) for key, value in runtime.provider_environment.items()
            },
        )

    @property
    def available(self) -> bool:
        node_path = Path(self.node_executable)
        node = (
            shutil.which(self.node_executable)
            if not node_path.is_absolute()
            else node_path.is_file()
        )
        return bool(node and self.bridge_entry is not None)

    @property
    def bridge_entry(self) -> Path | None:
        if (
            self.managed_bridge_entry is not None
            and self.managed_bridge_entry.is_file()
            and not self.managed_bridge_entry.is_symlink()
        ):
            return self.managed_bridge_entry
        if (
            self.package_entry is not None
            and self.package_entry.is_file()
            and not self.package_entry.is_symlink()
            and self.bridge_script.is_file()
            and not self.bridge_script.is_symlink()
        ):
            return self.bridge_script
        return None


@dataclass
class _Preview:
    token: str
    provider: str
    provider_name: str
    action: str
    expires_at_ms: int
    consumed: bool = False


@dataclass
class _OAuthJob:
    login_id: str
    provider: str
    provider_name: str
    login_method: str
    process: subprocess.Popen[str]
    state: str = "starting"
    created_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    verification_uri: str = ""
    user_code: str = ""
    expires_at_ms: int = 0
    timeout_at_ms: int = 0
    error: str = ""


class PiProviderAuthService:
    """A secret-free control surface backed by Pi's AuthStorage and ModelRegistry."""

    def __init__(
        self,
        config: PiProviderBridgeConfig,
        *,
        oauth_url_opener: Callable[[str], object] | None = None,
    ) -> None:
        self.config = config
        self._oauth_url_opener = oauth_url_opener or webbrowser.open
        self._lock = threading.RLock()
        self._bridge_lock = threading.Lock()
        self._oauth_start_lock = threading.Lock()
        self._previews: dict[str, _Preview] = {}
        self._oauth_jobs: dict[str, _OAuthJob] = {}

    @classmethod
    def from_runtime(
        cls,
        runtime: PiRuntimeConfig,
        *,
        repo_root: str | Path | None = None,
    ) -> PiProviderAuthService:
        return cls(PiProviderBridgeConfig.from_runtime(runtime, repo_root=repo_root))

    def catalog(self) -> dict[str, object]:
        if not self.config.available:
            return {
                "schemaVersion": "rag-ime.pi-provider-catalog.v1",
                "ok": True,
                "available": False,
                "providers": [],
                "unavailableReason": "当前 Agent 运行时未安装凭据管理组件，请重新安装或更新运行时。",
            }
        try:
            result = self._call({"action": "catalog"})
        except PiProviderAuthError as exc:
            return {
                "schemaVersion": "rag-ime.pi-provider-catalog.v1",
                "ok": False,
                "available": True,
                "providers": [],
                "error": _public_error(exc) or "无法读取 Pi Provider 状态，请确认 Agent 运行时可用后重试。",
            }
        providers = result.get("providers")
        return {
            "schemaVersion": "rag-ime.pi-provider-catalog.v1",
            "ok": True,
            "available": True,
            "providers": [
                *[item for item in (providers if isinstance(providers, list) else [])
                  if isinstance(item, Mapping) and item.get("id") != "typesafe"],
                self._jev_provider(),
            ],
            "catalogWarning": (
                "自定义模型目录未能完整载入，请检查模型配置。"
                if result.get("catalogError")
                else ""
            ),
            "refreshBehavior": "每次刷新都会重新读取 Pi 的模型目录与登录状态。",
            "sessionBoundary": "凭据变更不会打断正在回复的会话；下次重启 Agent 运行时后统一生效。",
        }

    def _jev_provider(self) -> dict[str, object]:
        configured = bool(str(os.environ.get("TYPESAFE_API_KEY") or read_keychain_secret(
            MODEL_KEYCHAIN_SERVICE, TYPESAFE_ACCOUNT
        ) or "").strip())
        return {
            "id": "typesafe", "name": "TypeSafe / Jev",
            "scenarios": [
                {"id": "approval", "name": "工具审批", "status": "configured" if configured else "needs_key",
                 "description": "按用户授权与操作范围判断；服务失败回退 Luna Max。"},
                {"id": "rerank", "name": "知识库重排", "status": "available",
                 "description": "按查询相关性给候选片段排序；需选择 Jev 重排服务并开启知识库重排。"},
                {"id": "compression", "name": "压缩前内容筛选", "status": "candidate",
                 "description": "判断哪些内容应保留；压缩摘要仍由 Pi 的生成模型完成。"},
                {"id": "memory", "name": "记忆去重与冲突判断", "status": "candidate",
                 "description": "识别重复或冲突，保留来源；不直接覆盖已接受记忆。"},
                {"id": "routing", "name": "检索路由", "status": "candidate",
                 "description": "判断是否需要检索、选择检索方式；由现有检索流程执行。"},
                {"id": "evidence", "name": "证据充分性检查", "status": "candidate",
                 "description": "检查召回证据能否支持回答，决定是否继续检索。"},
            ],
            "auth": {"configured": configured, "type": "api_key" if configured else "",
                     "oauthBrowserSupported": False, "oauthDeviceCodeSupported": False},
            "models": [{"id": "jev-latest", "name": "Jev · 工具审批专用"}],
        }

    def preview(self, payload: Mapping[str, object]) -> dict[str, object]:
        action = str(payload.get("action") or "").strip()
        provider = _provider_id(payload.get("provider"))
        if action not in _ALLOWED_ACTIONS:
            raise PiProviderAuthError("不支持这项凭据操作。")
        if provider == "typesafe" and str(os.environ.get("TYPESAFE_API_KEY") or "").strip():
            raise PiProviderAuthError("Jev 当前使用 TYPESAFE_API_KEY 环境变量；请先移除环境变量并重启服务，再在这里管理密钥。")
        provider_item = self._provider(provider)
        auth = provider_item.get("auth") if isinstance(provider_item.get("auth"), Mapping) else {}
        if action == "oauth_browser" and not bool(auth.get("oauthBrowserSupported")):
            raise PiProviderAuthError("这个 Provider 暂不支持浏览器登录。")
        if (
            action == "oauth_device_code"
            and not bool(auth.get("oauthDeviceCodeSupported"))
        ):
            raise PiProviderAuthError("这个 Provider 暂不支持设备码登录。")
        token = secrets.token_urlsafe(32)
        expires_at_ms = int(time.time() * 1000) + 120_000
        preview = _Preview(
            token=token,
            provider=provider,
            provider_name=str(provider_item.get("name") or provider),
            action=action,
            expires_at_ms=expires_at_ms,
        )
        with self._lock:
            self._prune_locked()
            self._previews[token] = preview
        return {
            "schemaVersion": "rag-ime.pi-provider-auth-preview.v1",
            "ok": True,
            "previewToken": token,
            "action": action,
            "provider": provider,
            "providerName": preview.provider_name,
            "requiredConfirm": _CONFIRM_TEXT[action],
            "expiresAtMs": expires_at_ms,
            "summary": _preview_summary(action, preview.provider_name),
            "secretPolicy": "密钥仅交给本机安全存储，不进入预览、收据或日志。",
            "sessionBoundary": ("保存后下一次工具审批生效，无需重启。" if provider == "typesafe"
                                else "正在回复的会话不被中断；凭据在 Agent 运行时下次启动时生效。"),
        }

    def apply(self, payload: Mapping[str, object]) -> dict[str, object]:
        token = str(payload.get("previewToken") or "").strip()
        if not token:
            raise PiProviderAuthError("确认已失效，请重新预览。")
        with self._lock:
            self._prune_locked()
            preview = self._previews.get(token)
            if preview is None or preview.consumed:
                raise PiProviderAuthError("确认已失效，请重新预览。")
            if int(time.time() * 1000) >= preview.expires_at_ms:
                self._previews.pop(token, None)
                raise PiProviderAuthError("确认已过期，请重新预览。")
            if str(payload.get("confirmText") or "") != _CONFIRM_TEXT[preview.action]:
                raise PiProviderAuthError("确认内容不匹配。")
            preview.consumed = True

        if preview.action == "set_api_key":
            api_key = str(payload.get("apiKey") or "").strip()
            if not api_key or len(api_key) > 16 * 1024:
                raise PiProviderAuthError("请输入有效的 API Key。")
            if preview.provider == "typesafe":
                self._write_jev_key(api_key)
                return self._receipt(preview, before_type="")
            result = self._call(
                {"action": "set_api_key", "provider": preview.provider, "apiKey": api_key}
            )
            return self._receipt(preview, before_type=str(result.get("beforeType") or ""))
        if preview.action == "logout" and preview.provider == "typesafe":
            self._write_jev_key(None)
            return self._receipt(preview, before_type="api_key")
        if preview.action == "logout":
            result = self._call({"action": "logout", "provider": preview.provider})
            return self._receipt(preview, before_type=str(result.get("beforeType") or ""))
        if preview.action in {"oauth_browser", "oauth_device_code"}:
            method = (
                "browser"
                if preview.action == "oauth_browser"
                else "device_code"
            )
            login = self._start_oauth(
                preview.provider,
                preview.provider_name,
                method=method,
            )
            return self._receipt(preview, before_type="", login=login)
        raise PiProviderAuthError("不支持这项凭据操作。")

    def _write_jev_key(self, value: str | None) -> None:
        if str(os.environ.get("TYPESAFE_API_KEY") or "").strip():
            raise PiProviderAuthError("Jev 当前由环境变量配置，请先移除环境变量并重启服务。")
        try:
            if value is None:
                delete_keychain_secret(MODEL_KEYCHAIN_SERVICE, TYPESAFE_ACCOUNT)
            else:
                write_keychain_secret(MODEL_KEYCHAIN_SERVICE, TYPESAFE_ACCOUNT, value)
        except Exception:
            raise PiProviderAuthError("Jev 密钥保存失败，请确认 macOS 钥匙串可用后重试。") from None

    def oauth_status(self, login_id: object) -> dict[str, object]:
        normalized = str(login_id or "").strip()
        with self._lock:
            self._prune_locked()
            job = self._oauth_jobs.get(normalized)
            if job is None:
                raise PiProviderAuthError("登录会话不存在或服务已重启，请重新连接。")
            return _oauth_payload(job)

    def oauth_cancel(self, payload: Mapping[str, object]) -> dict[str, object]:
        login_id = str(payload.get("loginId") or "").strip()
        with self._lock:
            job = self._oauth_jobs.get(login_id)
            if job is None:
                raise PiProviderAuthError("登录会话不存在或已经结束。")
            if job.state not in {"completed", "failed", "cancelled"}:
                job.state = "cancelled"
                job.updated_at_ms = int(time.time() * 1000)
            process = job.process
        _terminate_process(process)
        return {**_oauth_payload(job), "cancelled": job.state == "cancelled"}

    def close(self) -> None:
        with self._lock:
            processes = [
                job.process
                for job in self._oauth_jobs.values()
                if job.process.poll() is None
            ]
        for process in processes:
            _terminate_process(process)

    def _provider(self, provider: str) -> Mapping[str, object]:
        if provider == "typesafe":
            return self._jev_provider()
        catalog = self.catalog()
        if not catalog.get("available"):
            raise PiProviderAuthError(str(catalog.get("unavailableReason") or "凭据管理暂不可用。"))
        if not catalog.get("ok"):
            raise PiProviderAuthError(str(catalog.get("error") or "无法读取 Provider。"))
        for item in catalog.get("providers", []):
            if isinstance(item, Mapping) and item.get("id") == provider:
                return item
        raise PiProviderAuthError("Provider 不在 Pi 模型目录中。")

    def _receipt(
        self,
        preview: _Preview,
        *,
        before_type: str,
        login: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        login_started = login is not None
        now = int(time.time() * 1000)
        return {
            "schemaVersion": "rag-ime.pi-provider-auth-receipt.v1",
            "ok": True,
            "receiptId": f"pi-auth-{uuid.uuid4().hex}",
            "action": preview.action,
            "provider": preview.provider,
            "providerName": preview.provider_name,
            "previousAuthType": before_type,
            "receiptState": "login_started" if login_started else "applied",
            "issuedAtMs": now,
            "completedAtMs": 0 if login_started else now,
            "requiresAgentRestart": not login_started and preview.provider != "typesafe",
            "sessionBoundary": (
                "下一次工具审批将使用新配置，无需重启；未配置 Jev 时回退 Luna Max。"
                if preview.provider == "typesafe" else
                "登录完成后再重启 Agent 运行时；当前回复不会被中断。"
                if login_started
                else "不会中断正在回复的会话；结束当前回复后重启 Agent 运行时即可使用新凭据。"
            ),
            **({"login": dict(login)} if login is not None else {}),
        }

    def _call(self, request: Mapping[str, object]) -> dict[str, object]:
        bridge_entry = self.config.bridge_entry
        if not self.config.available or bridge_entry is None:
            raise PiProviderAuthError("当前 Agent 运行时没有可用的 Pi 凭据组件。")
        payload = self._bridge_payload(request)
        try:
            with self._bridge_lock:
                completed = subprocess.run(
                    [self.config.node_executable, str(bridge_entry)],
                    input=json.dumps(payload, ensure_ascii=False),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=self.config.timeout_seconds,
                    check=False,
                    env=self._bridge_environment(),
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PiProviderAuthError("Pi 凭据操作超时或运行时不可用。") from exc
        result = _last_bridge_event(completed.stdout)
        if completed.returncode != 0 or not result.get("ok"):
            raise PiProviderAuthError(_public_error(result.get("error")) or "Pi 凭据操作失败。")
        return result

    def _start_oauth(
        self,
        provider: str,
        provider_name: str,
        *,
        method: str = "browser",
    ) -> dict[str, object]:
        if method not in {"browser", "device_code"}:
            raise PiProviderAuthError("不支持这项登录方式。")
        bridge_entry = self.config.bridge_entry
        if not self.config.available or bridge_entry is None:
            raise PiProviderAuthError("当前 Agent 运行时没有可用的 Pi 登录组件。")
        with self._oauth_start_lock:
            with self._lock:
                self._prune_locked()
                for existing in self._oauth_jobs.values():
                    if existing.provider == provider and existing.state not in {"completed", "failed", "cancelled"}:
                        raise PiProviderAuthError("这个 Provider 已有一个登录流程正在进行。")
            request = self._bridge_payload(
                {
                    "action": (
                        "oauth_browser"
                        if method == "browser"
                        else "oauth_device_code"
                    ),
                    "provider": provider,
                }
            )
            process: subprocess.Popen[str] | None = None
            try:
                process = subprocess.Popen(
                    [self.config.node_executable, str(bridge_entry)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    env=self._bridge_environment(),
                    start_new_session=True,
                )
                if process.stdin is None:
                    raise OSError("Pi login stdin is unavailable")
                process.stdin.write(json.dumps(request, ensure_ascii=False))
                process.stdin.close()
            except OSError as exc:
                if process is not None:
                    _terminate_process(process)
                raise PiProviderAuthError("无法启动 Pi 登录流程。") from exc
            now = int(time.time() * 1000)
            login_id = f"pi-login-{uuid.uuid4().hex}"
            job = _OAuthJob(
                login_id=login_id,
                provider=provider,
                provider_name=provider_name,
                login_method=method,
                process=process,
                created_at_ms=now,
                updated_at_ms=now,
                timeout_at_ms=now + int(max(1.0, self.config.oauth_timeout_seconds) * 1000),
            )
            with self._lock:
                self._oauth_jobs[login_id] = job
        threading.Thread(
            target=self._watch_oauth_job,
            args=(job,),
            name=f"pi-oauth-{login_id[-8:]}",
            daemon=True,
        ).start()
        timeout = threading.Timer(
            max(1.0, self.config.oauth_timeout_seconds),
            self._expire_oauth_job,
            args=(job,),
        )
        timeout.daemon = True
        timeout.start()
        return _oauth_payload(job)

    def _bridge_payload(self, request: Mapping[str, object]) -> dict[str, object]:
        payload = {**dict(request), "agentDir": str(self.config.agent_dir)}
        if (
            self.config.bridge_entry == self.config.bridge_script
            and self.config.package_entry is not None
        ):
            payload["packageEntry"] = str(self.config.package_entry)
        return payload

    def _expire_oauth_job(self, job: _OAuthJob) -> None:
        with self._lock:
            if job.state in {"completed", "failed", "cancelled"}:
                return
            job.state = "failed"
            job.error = "登录等待超时，请重新连接。"
            job.user_code = ""
            job.updated_at_ms = int(time.time() * 1000)
            process = job.process
        _terminate_process(process)

    def _watch_oauth_job(self, job: _OAuthJob) -> None:
        stdout = job.process.stdout
        try:
            if stdout is not None:
                for line in stdout:
                    browser_url = ""
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, Mapping):
                        continue
                    with self._lock:
                        if job.state == "cancelled":
                            break
                        now = int(time.time() * 1000)
                        job.updated_at_ms = now
                        kind = str(event.get("event") or "")
                        if kind == "auth_url":
                            job.state = "waiting_for_user"
                            job.user_code = ""
                            job.verification_uri = _openai_codex_login_uri(
                                event.get("url")
                            )
                            browser_url = job.verification_uri
                        elif kind == "device_code":
                            job.state = "waiting_for_user"
                            job.user_code = str(event.get("userCode") or "")[:64]
                            job.verification_uri = _openai_codex_login_uri(
                                event.get("verificationUri")
                            )
                            expires_in = _bounded_int(event.get("expiresInSeconds"), 0, 3600)
                            job.expires_at_ms = now + expires_in * 1000 if expires_in else 0
                        elif kind == "completed":
                            job.state = "completed"
                            job.user_code = ""
                            job.verification_uri = ""
                        elif kind == "failed":
                            job.state = "failed"
                            job.error = _public_error(event.get("error"))
                            job.user_code = ""
                            job.verification_uri = ""
                        terminal = job.state in {"completed", "failed", "cancelled"}
                    if browser_url:
                        try:
                            self._oauth_url_opener(browser_url)
                        except (OSError, RuntimeError):
                            pass
                    if terminal:
                        break
            return_code = job.process.wait(timeout=2)
            with self._lock:
                if job.state not in {"completed", "failed", "cancelled"}:
                    job.state = "failed"
                    job.error = "登录流程提前结束，请重新连接。" if return_code else "登录未完成。"
                    job.updated_at_ms = int(time.time() * 1000)
        except Exception:
            with self._lock:
                if job.state not in {"completed", "failed", "cancelled"}:
                    job.state = "failed"
                    job.error = "登录流程异常结束，请重新连接。"
                    job.updated_at_ms = int(time.time() * 1000)
        finally:
            _terminate_process(job.process)
            if stdout is not None:
                stdout.close()

    def _bridge_environment(self) -> dict[str, str]:
        allowed = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
        environment = {key: os.environ[key] for key in allowed if os.environ.get(key)}
        environment.update(self.config.provider_environment)
        environment["PI_CODING_AGENT_DIR"] = str(self.config.agent_dir)
        return environment

    def _prune_locked(self) -> None:
        now = int(time.time() * 1000)
        self._previews = {
            token: preview
            for token, preview in self._previews.items()
            if not preview.consumed and preview.expires_at_ms > now
        }
        stale_before = now - 30 * 60 * 1000
        self._oauth_jobs = {
            login_id: job
            for login_id, job in self._oauth_jobs.items()
            if job.updated_at_ms >= stale_before
            or job.state not in {"completed", "failed", "cancelled"}
        }


def _discover_pi_package_entry(
    runtime: PiRuntimeConfig,
    *,
    repo_root: Path | None,
) -> Path | None:
    explicit = os.environ.get("RAG_IME_PI_PACKAGE_ENTRY", "").strip()
    candidates: list[Path] = [Path(explicit).expanduser()] if explicit else []
    if runtime.executable is not None:
        executable = runtime.executable.expanduser().resolve(strict=False)
        candidates.extend(
            [
                executable.parent / "index.js",
                executable.parent.parent / "dist" / "index.js",
            ]
        )
    if repo_root is not None:
        candidates.append(repo_root.parent / "pi" / "packages" / "coding-agent" / "dist" / "index.js")
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved.is_file() and not resolved.is_symlink():
            return resolved
    return None


def _last_bridge_event(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {"ok": False, "error": "Pi 凭据组件没有返回结果。"}


def _provider_id(value: object) -> str:
    provider = str(value or "").strip()
    if not _PROVIDER_ID_PATTERN.fullmatch(provider):
        raise PiProviderAuthError("Provider 无效。")
    return provider


def _preview_summary(action: str, provider_name: str) -> list[str]:
    if action == "set_api_key":
        return [f"替换 {provider_name} 的 API Key。", "现有密钥不会读取或显示。", "确认后由 Pi 安全写入。"]
    if action == "logout":
        return [f"退出 {provider_name}。", "本机保存的登录信息会移除。", "环境变量与外部配置不会改变。"]
    if action == "oauth_browser":
        return [
            f"连接 {provider_name}。",
            "默认打开浏览器，并通过本机 localhost 回调完成登录。",
            "令牌只由 Pi 保存。",
        ]
    return [
        f"使用设备码连接 {provider_name}。",
        "设备码仅用于本机回调不可用的备用流程。",
        "令牌只由 Pi 保存。",
    ]


def _openai_codex_login_uri(value: object) -> str:
    uri = str(value or "").strip()
    if not uri or len(uri) > 4096:
        return ""
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname != "auth.openai.com"
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.path not in {"/oauth/authorize", "/codex/device"}
    ):
        return ""
    return uri


def _oauth_payload(job: _OAuthJob) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.pi-provider-oauth-status.v1",
        "ok": job.state != "failed",
        "loginId": job.login_id,
        "provider": job.provider,
        "providerName": job.provider_name,
        "loginMethod": job.login_method,
        "state": job.state,
        "verificationUri": job.verification_uri,
        "userCode": job.user_code,
        "expiresAtMs": job.expires_at_ms,
        "timeoutAtMs": job.timeout_at_ms,
        "updatedAtMs": job.updated_at_ms,
        "error": job.error,
        "requiresAgentRestart": job.state == "completed",
    }


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


def _public_error(value: object) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(
        r"(?i)(?:sk|key|token|bearer)[-_A-Za-z0-9.]{8,}",
        "[redacted]",
        text,
    )
    text = re.sub(r"(?:/[^\s:;,]+){2,}", "[internal path]", text)
    text = re.sub(r"[A-Za-z]:\\(?:[^\s:;,]+\\)+[^\s:;,]+", "[internal path]", text)
    return text[:300]


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(parsed, maximum))
