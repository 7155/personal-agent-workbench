from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import RLock
from typing import Any

from .knowledge_library import AssetBlob, HttpKnowledgeClient, KnowledgeLibraryError
from .knowledge_library.identity import knowledge_worker_fingerprint, normalized_knowledge_root
from .knowledge_library.rerank import knowledge_reranker_profile_sha256
from .knowledge_embedding_profile import (
    embedding_environment_from_settings,
    normalize_knowledge_embedding_profile,
)
from .knowledge_library.models import DEFAULT_MINERU_TIMEOUT_SECONDS


class KnowledgeWorkerSupervisor:
    """Lazily starts the isolated document worker and exposes its HTTP client."""

    def __init__(
        self,
        *,
        settings_provider: Callable[[], Mapping[str, object]],
        root_dir: Path | None = None,
        base_url: str = "http://127.0.0.1:8769",
        idle_seconds: int = 900,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        allow_shared_owner: bool | None = None,
        intake_db_path: Path | None = None,
    ) -> None:
        self.settings_provider = settings_provider
        self.root_dir = Path(root_dir or default_knowledge_root()).expanduser()
        self.base_url = base_url.rstrip("/")
        parsed_url = urllib.parse.urlparse(self.base_url)
        self._worker_host = str(parsed_url.hostname or "127.0.0.1")
        self._worker_port = int(parsed_url.port or 80)
        self.idle_seconds = max(60, min(86_400, int(idle_seconds)))
        self._popen = popen
        self._clock = clock
        self.allow_shared_owner = (
            str(os.environ.get("RAG_IME_KNOWLEDGE_SHARED_WORKER", "0")).strip().lower()
            in {"1", "true", "yes", "on"}
            if allow_shared_owner is None
            else bool(allow_shared_owner)
        )
        self.python_executable, self.python_version = _knowledge_python_runtime()
        self._client = HttpKnowledgeClient(self.base_url)
        self._loopback_urlopen = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
        ).open
        self._process: subprocess.Popen[bytes] | None = None
        self._started_fingerprint = ""
        self._owner = f"sidecar:{secrets.token_hex(16)}"
        self.intake_db_path = Path(intake_db_path).expanduser().resolve(strict=False) if intake_db_path else None
        self._adopted_owner = ""
        self._lock = RLock()

    def list_bases(self, payload: Mapping[str, object]) -> dict[str, Any]:
        return self._call("list_bases", payload)

    def search(self, payload: Mapping[str, object]) -> dict[str, Any]:
        return self._call("search", payload)

    def find(self, payload: Mapping[str, object]) -> dict[str, Any]:
        return self._call("find", payload)

    def open(self, payload: Mapping[str, object]) -> dict[str, Any]:
        return self._call("open", payload)

    def status(self, payload: Mapping[str, object]) -> dict[str, Any]:
        _ = payload
        try:
            return self._call("status", {})
        except KnowledgeLibraryError as exc:
            return {
                "schemaVersion": "rag-ime.knowledge-library.v1",
                "available": False,
                "state": "unavailable",
                "reason": exc.code,
            }

    def management_call(self, operation: str, *args: object, **kwargs: object) -> dict[str, Any] | AssetBlob:
        return self._call(operation, *args, **kwargs)

    def probe_embedding_profile(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, Any]:
        allowed = {
            "provider",
            "model",
            "baseUrl",
            "dimensions",
            "secretReference",
            "queryPrefix",
            "documentPrefix",
            "denseBackend",
        }
        unknown = sorted(str(key) for key in set(payload) - allowed)
        if unknown:
            raise KnowledgeLibraryError(
                f"unsupported embedding profile field: {unknown[0]}",
                code="invalid_argument",
            )
        current = json.loads(json.dumps(self.settings_provider()))
        if not isinstance(current, dict):
            current = {}
        knowledge = current.get("knowledgeLibrary")
        if not isinstance(knowledge, dict):
            knowledge = {}
            current["knowledgeLibrary"] = knowledge
        embedding = knowledge.get("embedding")
        if not isinstance(embedding, dict):
            embedding = {}
            knowledge["embedding"] = embedding
        embedding.update(dict(payload))
        profile = normalize_knowledge_embedding_profile(current, environ=os.environ)
        environment = self._worker_environment(current)
        try:
            completed = subprocess.run(
                [self.python_executable, "-m", "rag_ime.knowledge_embedding_probe"],
                check=False,
                cwd=str(Path(__file__).resolve().parents[1]),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=180.0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise KnowledgeLibraryError(
                "Knowledge embedding probe process failed",
                code="embedding_probe_failed",
            ) from exc
        try:
            result = json.loads(str(completed.stdout or "")[:32_768])
        except json.JSONDecodeError as exc:
            raise KnowledgeLibraryError(
                "Knowledge embedding probe returned an invalid receipt",
                code="embedding_probe_failed",
            ) from exc
        if (
            completed.returncode != 0
            or not isinstance(result, Mapping)
            or result.get("ready") is not True
            or result.get("secretsVisible") is not False
        ):
            error_type = (
                str(result.get("errorType") or "")
                if isinstance(result, Mapping)
                else ""
            )
            raise KnowledgeLibraryError(
                "Knowledge embedding probe failed"
                + (f" ({error_type})" if error_type else ""),
                code="embedding_probe_failed",
            )
        if str(result.get("provider") or "") != str(profile["provider"]):
            raise KnowledgeLibraryError(
                "Knowledge embedding probe provider does not match the candidate profile",
                code="embedding_probe_mismatch",
            )
        return {
            **dict(result),
            "profileSha256": str(profile["profileSha256"]),
            "secretsVisible": False,
        }

    def ensure_running(self) -> None:
        with self._lock:
            settings = self.settings_provider()
            fingerprint, mineru_enabled, mineru_port = self._worker_settings(settings)
            mineru_timeout_seconds = self._mineru_timeout_seconds(settings)
            health = self._worker_health()
            if health is not None and self._health_matches(health, fingerprint):
                if self._process is None:
                    parent_pid = _health_parent_pid(health)
                    if parent_pid in {0, os.getpid()}:
                        self._adopted_owner = str(health["owner"])
                        return
                    if _pid_exists(parent_pid):
                        if self.allow_shared_owner:
                            self._adopted_owner = str(health["owner"])
                            return
                        raise KnowledgeLibraryError(
                            "the matching knowledge worker belongs to another live Sidecar",
                            code="worker_parent_mismatch",
                        )
                    health = self._wait_for_stale_parent_worker()
                    if health is not None:
                        raise KnowledgeLibraryError(
                            "the knowledge worker did not exit after its parent stopped",
                            code="worker_stale_parent",
                        )
                elif str(health["owner"]) == self._owner and fingerprint == self._started_fingerprint:
                    return
            if health is not None:
                if self._process is None or str(health.get("owner") or "") != self._owner:
                    raise KnowledgeLibraryError(
                        "a knowledge worker with a different root or configuration already owns the port",
                        code="worker_identity_mismatch",
                    )
                if self._process is not None:
                    self._stop_owned_worker()
            elif self._process is not None and self._process.poll() is not None:
                self._process = None
                self._started_fingerprint = ""
            elif self._process is not None:
                self._stop_owned_worker()

            self.root_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root_dir, 0o700)
            command = [
                self.python_executable,
                "-m",
                "rag_ime.knowledge_library.worker",
                "--host",
                self._worker_host,
                "--port",
                str(self._worker_port),
                "--root",
                str(self.root_dir),
                "--idle-seconds",
                str(self.idle_seconds),
                "--mineru-port",
                str(mineru_port),
                "--mineru-timeout-seconds",
                str(mineru_timeout_seconds),
                "--owner",
                self._owner,
                "--parent-pid",
                str(os.getpid()),
            ]
            if self.intake_db_path is not None:
                command.extend(["--intake-db", str(self.intake_db_path)])
            if mineru_enabled:
                command.append("--mineru-enabled")
            worker_env = self._worker_environment(settings)
            self._process = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(Path(__file__).resolve().parents[1]),
                env=worker_env,
                start_new_session=True,
            )
            self._started_fingerprint = fingerprint
            self._adopted_owner = ""
            deadline = self._clock() + 5.0
            while self._clock() < deadline:
                if self._process.poll() is not None:
                    break
                health = self._worker_health()
                if (
                    health is not None
                    and self._health_matches(health, fingerprint)
                    and str(health["owner"]) == self._owner
                ):
                    return
                time.sleep(0.05)
            code = self._process.poll()
            raise KnowledgeLibraryError(
                f"knowledge worker failed to start{f' (exit {code})' if code is not None else ''}",
                code="worker_start_failed",
            )

    def close(self) -> None:
        with self._lock:
            self._stop_owned_worker()

    def _call(self, operation: str, *args: object, **kwargs: object) -> Any:
        self.ensure_running()
        handler = getattr(self._client, operation, None)
        if not callable(handler):
            raise KnowledgeLibraryError(
                f"knowledge worker operation is unavailable: {operation}",
                code="unsupported_operation",
            )
        try:
            result = handler(*args, **kwargs)
        except KnowledgeLibraryError as exc:
            if exc.code != "worker_unavailable":
                raise
            with self._lock:
                if self._process is not None and self._process.poll() is not None:
                    self._process = None
            self.ensure_running()
            result = handler(*args, **kwargs)
        if not isinstance(result, (dict, AssetBlob)):
            raise KnowledgeLibraryError(
                "knowledge worker returned an invalid response",
                code="worker_bad_response",
            )
        return result

    def _worker_health(self) -> dict[str, object] | None:
        request = urllib.request.Request(
            f"{self.base_url}/v1/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with self._loopback_urlopen(request, timeout=0.35) as response:
                if int(getattr(response, "status", 0)) != 200:
                    return None
                raw = response.read(16_384)
            payload = json.loads(raw.decode("utf-8"))
            return dict(payload) if isinstance(payload, Mapping) else None
        except (OSError, ValueError, UnicodeDecodeError, urllib.error.URLError):
            return None

    def _health_matches(self, health: Mapping[str, object], fingerprint: str) -> bool:
        owner = str(health.get("owner") or "")
        return (
            str(health.get("status") or "") == "ok"
            and str(health.get("root") or "") == normalized_knowledge_root(self.root_dir)
            and str(health.get("configFingerprint") or "") == fingerprint
            and bool(owner)
        )

    def _wait_for_stale_parent_worker(self) -> dict[str, object] | None:
        deadline = self._clock() + 2.0
        health = self._worker_health()
        while health is not None and self._clock() < deadline:
            time.sleep(0.05)
            health = self._worker_health()
        return health

    def _worker_settings(
        self,
        settings: Mapping[str, object] | None = None,
    ) -> tuple[str, bool, int]:
        settings = settings if settings is not None else self.settings_provider()
        knowledge = settings.get("knowledgeLibrary") if isinstance(settings, Mapping) else None
        parser = knowledge.get("parser") if isinstance(knowledge, Mapping) else None
        mineru = parser.get("mineru") if isinstance(parser, Mapping) else None
        enabled = bool(mineru.get("enabled", False)) if isinstance(mineru, Mapping) else False
        raw_port = mineru.get("port", 30_001) if isinstance(mineru, Mapping) else 30_001
        port = int(raw_port) if isinstance(raw_port, (int, float)) and not isinstance(raw_port, bool) else 30_001
        port = max(1_024, min(65_535, port))
        timeout_seconds = self._mineru_timeout_seconds(settings)
        embedding = normalize_knowledge_embedding_profile(settings, environ=os.environ)
        fingerprint = knowledge_worker_fingerprint(
            self.root_dir,
            mineru_enabled=enabled,
            mineru_port=port,
            mineru_timeout_seconds=timeout_seconds,
            idle_seconds=self.idle_seconds,
            python_executable=self.python_executable,
            python_version=self.python_version,
            embedding_provider=str(embedding["provider"]),
            embedding_model=str(embedding["model"]),
            dense_backend=str(embedding["denseBackend"]),
            embedding_profile_sha256=(
                str(embedding["profileSha256"])
                if embedding["source"] == "settings"
                else ""
            ),
            reranker_provider=os.environ.get(
                "RAG_IME_KNOWLEDGE_RERANK_PROVIDER",
                "none",
            ),
            reranker_model_path=os.environ.get(
                "RAG_IME_KNOWLEDGE_RERANK_MODEL_PATH",
                "",
            ),
            reranker_model_revision=os.environ.get(
                "RAG_IME_KNOWLEDGE_RERANK_MODEL_REVISION",
                "",
            ),
            reranker_profile_sha256=knowledge_reranker_profile_sha256(),
        )
        return fingerprint, enabled, port

    @staticmethod
    def _mineru_timeout_seconds(settings: Mapping[str, object] | None = None) -> float:
        settings = settings if settings is not None else {}
        knowledge = settings.get("knowledgeLibrary") if isinstance(settings, Mapping) else None
        parser = knowledge.get("parser") if isinstance(knowledge, Mapping) else None
        mineru = parser.get("mineru") if isinstance(parser, Mapping) else None
        raw = mineru.get("timeoutSeconds", DEFAULT_MINERU_TIMEOUT_SECONDS) if isinstance(mineru, Mapping) else DEFAULT_MINERU_TIMEOUT_SECONDS
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = DEFAULT_MINERU_TIMEOUT_SECONDS
        return max(1.0, min(86_400.0, value))

    def _worker_environment(
        self,
        settings: Mapping[str, object],
    ) -> dict[str, str]:
        environment = _knowledge_worker_env(os.environ)
        for key in tuple(environment):
            if key.startswith("RAG_IME_EMBEDDING_") or key == "RAG_IME_KNOWLEDGE_DENSE_BACKEND":
                environment.pop(key, None)
        environment.pop("RAG_IME_KNOWLEDGE_EMBEDDING_PROFILE_SHA256", None)
        environment.update(
            embedding_environment_from_settings(settings, environ=os.environ)
        )
        profile = normalize_knowledge_embedding_profile(settings, environ=os.environ)
        if profile["source"] == "settings":
            environment["RAG_IME_KNOWLEDGE_EMBEDDING_PROFILE_SHA256"] = str(
                profile["profileSha256"]
            )
        return environment

    def _stop_owned_worker(self) -> None:
        process = self._process
        self._process = None
        self._started_fingerprint = ""
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)


def default_knowledge_root() -> Path:
    explicit = os.environ.get("RAG_IME_KNOWLEDGE_ROOT", "").strip()
    if explicit:
        return Path(explicit)
    support = os.environ.get("RAG_IME_APP_SUPPORT_DIR", "").strip()
    if support:
        return Path(support) / "Knowledge"
    return Path.home() / "Library" / "Application Support" / "RagIme" / "Knowledge"


def _knowledge_python_runtime() -> tuple[str, str]:
    configured = os.environ.get("RAG_IME_KNOWLEDGE_PYTHON", "").strip() or sys.executable
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise KnowledgeLibraryError(
            "RAG_IME_KNOWLEDGE_PYTHON must be an absolute executable file",
            code="invalid_worker_python",
        )
    # Keep the configured path itself: resolving a virtualenv's ``bin/python``
    # symlink selects the base interpreter and silently drops that environment's
    # site-packages (notably MLX and USearch in the dedicated knowledge runtime).
    executable = str(candidate)
    try:
        probe = subprocess.run(
            [
                executable,
                "-c",
                "import platform,sys; print(platform.python_version()); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KnowledgeLibraryError(
            "RAG_IME_KNOWLEDGE_PYTHON must run Python 3.11 or newer",
            code="invalid_worker_python",
        ) from exc
    version = probe.stdout.strip()
    if not version:
        raise KnowledgeLibraryError(
            "RAG_IME_KNOWLEDGE_PYTHON did not report a Python version",
            code="invalid_worker_python",
        )
    return executable, version


def _knowledge_worker_env(source: Mapping[str, str]) -> dict[str, str]:
    """Build the worker's minimum environment without inheriting Sidecar secrets."""

    exact_keys = {
        "HOME",
        "PATH",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        # The worker may read the owner-only provider file for explicit,
        # offline graph rebuilds. Keep literal provider credentials out of the
        # subprocess environment.
        "RAG_IME_DEEPSEEK_ENV",
        "HF_HOME",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "TOKENIZERS_PARALLELISM",
        "PYTORCH_ENABLE_MPS_FALLBACK",
    }
    prefixes = ("RAG_IME_EMBEDDING_", "RAG_IME_KNOWLEDGE_")
    result = {
        str(key): str(value)
        for key, value in source.items()
        if value is not None and (key in exact_keys or key.startswith(prefixes))
    }
    result.pop("PYTHONHOME", None)
    result.pop("PYTHONPATH", None)
    result.pop("__PYVENV_LAUNCHER__", None)
    result["PYTHONUNBUFFERED"] = "1"
    return result


def _health_parent_pid(health: Mapping[str, object]) -> int:
    raw = health.get("parentPid")
    return int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0 else 0


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
