"""Per-session Pi runtime multiplexing for authenticated team execution.

This module intentionally contains no model or tool loop.  Each entry in the
mapping is an ordinary :class:`PiRuntimeHostManager`; Pi remains the owner of
the Session/Agent/Tool lifecycle while the team boundary chooses its isolated
host and capability for the session.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
import threading

from .shared_packages import StagedTeamPackage

from rag_ime.agent_runtime_driver import (
    AgentRuntimeDriver,
    AgentRuntimeError,
    RuntimeDriverContext,
    SessionContextProvider,
)
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.runtime import DurableHistoryUnavailable, PiRuntimeHostManager
from rag_ime.room_runtime_host_kill_gate import RuntimeHostKillGate

from .execution import ExecutionLauncher, ExecutionSpec


__all__ = ["TeamRuntimeBinding", "TeamRuntimeDriver"]


_BROKER_PROVIDER_CREDENTIAL_KEYS = frozenset(
    {
        "apikey",
        "api_key",
        "password",
        "passwd",
        "secret",
        "credential",
        "privatekey",
        "accesstoken",
        "refreshtoken",
        "token",
    }
)


def _validate_broker_provider_values(
    value: object,
    gateway_token: str,
    *,
    path: str = "provider",
) -> None:
    """Reject real provider credentials from a team Host config.

    Pi still needs an ``apiKey`` field so its ordinary HTTP client sends a
    bearer header to the per-attempt broker.  The only permitted value is the
    short-lived attempt token; operator model-service credentials stay in the
    server-side broker.
    """

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.casefold() in _BROKER_PROVIDER_CREDENTIAL_KEYS:
                if child is not None and str(child).strip() not in {"", gateway_token}:
                    raise ValueError(
                        f"team provider credential at {path}.{key} cannot enter the Pi Host"
                    )
                continue
            _validate_broker_provider_values(
                child,
                gateway_token,
                path=f"{path}.{key}",
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_broker_provider_values(
                child,
                gateway_token,
                path=f"{path}[{index}]",
            )


@dataclass(frozen=True)
class TeamRuntimeBinding:
    """Server-derived execution scope and short-lived gateway capability."""

    spec: ExecutionSpec
    gateway_url: str
    gateway_token: str
    grant_id: str = ""
    generation: int = 1
    provider: str = ""
    model: str = ""
    model_providers: Mapping[str, Mapping[str, object]] | None = None
    launcher: ExecutionLauncher | None = None
    packages: tuple[StagedTeamPackage, ...] = field(default=(), repr=False)
    package_activation_token: str = field(default='', repr=False)
    skill_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.spec.session_id.strip() == "":
            raise ValueError("team execution binding requires a Session")
        gateway_token = self.gateway_token.strip()
        if not gateway_token or len(gateway_token) > 4096 or any(
            ord(char) < 32 for char in gateway_token
        ):
            raise ValueError("team execution binding requires a short-lived gateway token")
        gateway_url = self.gateway_url.strip()
        if not gateway_url.startswith(("http://127.0.0.1", "http://localhost")):
            raise ValueError("team execution gateway must point to the local broker")
        grant_id = self.grant_id.strip()
        if not grant_id or len(grant_id) > 256 or any(ord(char) < 32 for char in grant_id):
            raise ValueError("team execution binding requires a grant id")
        if int(self.generation) < 1:
            raise ValueError("team execution binding generation must be positive")
        if self.launcher is None:
            raise ValueError("team execution binding requires a trusted launcher")
        if not isinstance(self.launcher, ExecutionLauncher):
            raise ValueError("team execution binding launcher does not implement the trusted contract")
        object.__setattr__(self, "gateway_url", gateway_url)
        object.__setattr__(self, "gateway_token", gateway_token)
        object.__setattr__(self, "grant_id", grant_id)
        object.__setattr__(self, "generation", int(self.generation))
        if tuple(package.path for package in self.packages) != self.spec.resource_roots:
            raise ValueError('task Package sources must exactly match the fixed resource mounts')
        if self.packages:
            token = self.package_activation_token
            if not token or len(token) > 1024 or any(ord(char) < 32 for char in token):
                raise ValueError('task Package activation requires an attempt token')
        elif self.package_activation_token:
            raise ValueError('Package activation token requires fixed resources')

    def scoped_config(self, base: PiRuntimeConfig) -> PiRuntimeConfig:
        """Bind Pi's filesystem and gateway settings to this attempt."""

        environment = self.spec.container_environment_for(
            gateway_url=self.gateway_url,
            gateway_token=self.gateway_token,
        )
        environment.update(
            {
                "RAG_IME_AGENT_SESSION_ID": self.spec.session_id,
                "RAG_IME_AGENT_ATTEMPT_ID": self.spec.attempt_id,
                "RAG_IME_AGENT_GRANT_ID": self.grant_id,
                "RAG_IME_AGENT_GENERATION": str(self.generation),
            }
        )
        if self.packages:
            environment['RAG_IME_PLUGIN_APPROVAL_TOKEN'] = self.package_activation_token
        # Provider credentials are broker-owned.  The worker can receive
        # broker-backed model definitions, but never inherited API keys.
        providers = (
            dict(self.model_providers)
            if self.model_providers is not None
            else dict(base.model_providers)
        )
        _validate_broker_provider_values(providers, self.gateway_token)
        provider = self.provider or base.provider
        model = self.model or base.model
        return replace(
            base,
            enabled=base.enabled,
            agent_dir=self.spec.agent_dir,
            session_dir=self.spec.session_dir,
            logs_dir=self.spec.logs_dir,
            debug_context_dir=None,
            provider=provider,
            model=model,
            model_providers=providers,
            provider_environment={},
            tool_gateway_url=self.gateway_url,
            tool_gateway_token=self.gateway_token,
            plugin_approval_token=self.package_activation_token,
            runtime_environment=environment,
            execution_spec=self.spec,
        )


BindingResolver = Callable[[Mapping[str, object]], TeamRuntimeBinding | None]
ManagerBuilder = Callable[..., PiRuntimeHostManager]
ExecutionStop = Callable[[str], None]
ExecutionHistoryRoot = Callable[[str], Path]


class TeamRuntimeDriver:
    """Multiplex ordinary Pi managers, one isolated manager per attempt."""

    _RUNTIME_KIND = "pi_rpc"
    _DRIVER_ID = "managed-pi"

    def __init__(
        self,
        *,
        base_config: PiRuntimeConfig,
        context: RuntimeDriverContext,
        purpose: str,
        binding_resolver: Callable[[Mapping[str, object]], object],
        manager_builder: ManagerBuilder,
        session_context_provider: SessionContextProvider | None = None,
        execution_stop: ExecutionStop | None = None,
        execution_history_root: ExecutionHistoryRoot | None = None,
    ) -> None:
        if purpose not in {"interactive", "delegated"}:
            raise ValueError("runtime driver purpose must be interactive or delegated")
        self.config = base_config
        self.context = context
        self.purpose = purpose
        self.binding_resolver = binding_resolver
        self.manager_builder = manager_builder
        self.session_context_provider = session_context_provider
        self.execution_stop = execution_stop
        self.execution_history_root = execution_history_root
        self._lock = threading.RLock()
        self._managers: dict[str, PiRuntimeHostManager] = {}
        self._manager_keys: dict[str, tuple[str, int, str]] = {}
        self._preparation_locks: dict[str, threading.RLock] = {}
        self._starting: dict[str, PiRuntimeHostManager] = {}
        self._lifecycle_generation = 0
        self._stopping = 0
        self._history_manager: PiRuntimeHostManager | None = None

    @property
    def managers(self) -> Mapping[str, PiRuntimeHostManager]:
        """Expose a read-only snapshot useful to runtime diagnostics/tests."""

        with self._lock:
            return dict(self._managers)

    @property
    def runtime_kind(self) -> str:
        return self.runtime_kind_value

    @property
    def runtime_kind_value(self) -> str:
        return self._RUNTIME_KIND

    @property
    def driver_id(self) -> str:
        return self.driver_id_value

    @property
    def driver_id_value(self) -> str:
        return self._DRIVER_ID

    @property
    def session_root(self) -> Path:
        # This property is a factory-level hint only. Actual transcripts are
        # under each binding's scoped session_dir and are never opened here.
        return self.config.session_dir

    @property
    def default_model_profile(self) -> str:
        provider = str(self.config.provider or "").strip()
        model = str(self.config.model or "").strip()
        return f"{provider}/{model}" if provider and model else "pi/default"

    def _binding_for(self, session_id: str) -> TeamRuntimeBinding:
        try:
            session = dict(self.context.sessions.get(session_id))
        except KeyError as exc:
            raise AgentRuntimeError("team Session is unavailable") from exc
        raw = self.binding_resolver(session)
        if not isinstance(raw, TeamRuntimeBinding):
            raise AgentRuntimeError(
                "team execution binding is unavailable; refusing shared-host fallback"
            )
        if raw.spec.session_id != session_id:
            raise AgentRuntimeError("team execution binding Session does not match request")
        return raw

    def _manager_for(self, session_id: str) -> PiRuntimeHostManager:
        normalized = str(session_id).strip()
        if not normalized:
            raise AgentRuntimeError("team runtime requires a Session id")
        with self._lock:
            if self._stopping:
                raise AgentRuntimeError('Task Runtime is stopping; retry after retirement completes')
            preparation = self._preparation_locks.setdefault(normalized, threading.RLock())
            lifecycle_generation = self._lifecycle_generation
        # Install a fixed bundle once before opening this Session. Other users'
        # Hosts prepare concurrently; only calls for this Session serialize.
        with preparation:
            return self._prepare_manager(normalized, lifecycle_generation)

    def _prepare_manager(self, normalized: str, lifecycle_generation: int) -> PiRuntimeHostManager:
        with self._lock:
            if lifecycle_generation != self._lifecycle_generation:
                raise AgentRuntimeError('Task Runtime stopped before resource preparation')
        binding = self._binding_for(normalized)
        key = (binding.spec.attempt_id, binding.generation, binding.grant_id)
        old_manager: PiRuntimeHostManager | None = None
        with self._lock:
            if lifecycle_generation != self._lifecycle_generation:
                raise AgentRuntimeError('Task Runtime stopped during binding resolution')
            existing = self._managers.get(normalized)
            if existing is not None and self._manager_keys.get(normalized) == key:
                return existing
            if existing is not None:
                old_manager = existing
                self._managers.pop(normalized, None)
                self._manager_keys.pop(normalized, None)
        if old_manager is not None:
            old_manager.retire()
        scoped = binding.scoped_config(self.config)
        # RuntimeHostKillGate historically enforces one personal Host per
        # SQLite database. Team attempts intentionally have independent Host
        # lifecycles, so each attempt gets a private registry under its scope.
        kill_gate = RuntimeHostKillGate(binding.spec.scope_root / ".runtime-host-kill.sqlite")
        manager = self.manager_builder(
            config=scoped,
            context=replace(self.context, skill_allowlist_provider=lambda _session: list(binding.skill_refs),
                            candidate_skill_paths_provider=None),
            session_context_provider=self.session_context_provider,
            execution_launcher=binding.launcher,
            kill_gate=kill_gate,
        )
        with self._lock:
            if lifecycle_generation != self._lifecycle_generation:
                manager.retire()
                raise AgentRuntimeError('Task Runtime stopped during resource preparation')
            self._starting[normalized] = manager
        try:
            if binding.packages:
                from .resource_runtime import install_task_packages
                install_task_packages(manager, tuple(
                    replace(package, path=Path(binding.spec.container_resource_path(index)))
                    for index, package in enumerate(binding.packages)
                ))
            # A stop/revocation while installation was in flight cannot make
            # the prepared manager available for a later prompt.
            current_binding = self._binding_for(normalized)
            if (current_binding.spec.attempt_id, current_binding.generation, current_binding.grant_id) != key:
                raise AgentRuntimeError('Task authorization changed during resource preparation')
        except BaseException:
            manager.retire()
            raise
        finally:
            with self._lock:
                self._starting.pop(normalized, None)
        with self._lock:
            if lifecycle_generation != self._lifecycle_generation:
                manager.retire()
                raise AgentRuntimeError('Task Runtime stopped during resource preparation')
            current = self._managers.get(normalized)
            if current is not None:
                manager.retire()
                return current
            self._managers[normalized] = manager
            self._manager_keys[normalized] = key
        return manager

    def _existing_manager(self) -> PiRuntimeHostManager:
        with self._lock:
            values = tuple(self._managers.values())
        if len(values) != 1:
            raise AgentRuntimeError(
                "team runtime operation requires an explicit Session-scoped manager"
            )
        return values[0]

    def _resident_manager(self, session_id: str) -> PiRuntimeHostManager | None:
        """Read a resident manager without resolving a binding or starting a Host."""

        with self._lock:
            return self._managers.get(str(session_id).strip())

    def _history_root_for(self, session_id: str) -> Path | None:
        """Resolve the server-owned stable transcript root for cold reads."""

        if self.execution_history_root is None:
            return None
        normalized = str(session_id).strip()
        if not normalized:
            return None
        try:
            root = Path(self.execution_history_root(normalized)).expanduser()
            if not root.is_absolute() or root == Path("/"):
                raise DurableHistoryUnavailable(
                    "durable Session history root is invalid"
                )
            if root.is_symlink():
                raise DurableHistoryUnavailable(
                    "durable Session history root must not be a symlink"
                )
            resolved = root.resolve(strict=True)
        except FileNotFoundError:
            # The coordinator may not have materialized a stable transcript
            # directory for a brand-new Session yet. That is an ordinary
            # missing-history state; permission and symlink failures below are
            # deliberately kept observable.
            return None
        except DurableHistoryUnavailable:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise DurableHistoryUnavailable(
                "durable Session history root could not be read safely"
            ) from exc
        if resolved == Path("/") or resolved.is_symlink() or not resolved.is_dir():
            raise DurableHistoryUnavailable(
                "durable Session history root is not a safe directory"
            )
        # Keep the callback's spelling so _scoped_transcript_path can map a
        # macOS /var or /tmp alias to its canonical form while preserving
        # descendant components for O_NOFOLLOW validation.
        return root

    def _history_manager_for(self) -> PiRuntimeHostManager:
        """Build one ordinary Pi projection manager without starting a Host."""

        with self._lock:
            if self._history_manager is not None:
                return self._history_manager
        manager = self.manager_builder(
            config=self.config,
            context=self.context,
            session_context_provider=self.session_context_provider,
        )
        with self._lock:
            if self._history_manager is None:
                self._history_manager = manager
                return manager
        manager.retire()
        with self._lock:
            assert self._history_manager is not None
            return self._history_manager

    def _cold_history_snapshot(self, session_id: str) -> dict[str, object] | None:
        """Project an existing transcript through Pi's normal durable owner."""

        root = self._history_root_for(session_id)
        if root is None:
            return None
        try:
            snapshot = self._history_manager_for().session_snapshot(
                session_id,
                durable_only=True,
                transcript_root=root,
            )
        except DurableHistoryUnavailable as exc:
            # A missing transcript is the normal first-run state. A path that
            # exists but is outside the root, symlinked, malformed, or
            # unreadable must remain visible as unavailable rather than being
            # mistaken for a newly-created empty Session.
            if exc.missing:
                return None
            raise
        except AgentRuntimeError:
            raise
        except Exception as exc:
            # Unexpected projection failures are still an explicit read
            # failure; never turn them into a successful empty conversation.
            raise DurableHistoryUnavailable(
                "durable Session transcript could not be projected safely"
            ) from exc
        result = dict(snapshot)
        result.setdefault("sessionId", session_id)
        result.setdefault("isIdle", True)
        result.setdefault("toolHistoryEvents", [])
        result.setdefault("telemetry", None)
        result.setdefault(
            "messageQueue",
            {
                "steering": [],
                "followUp": [],
                "steeringMode": "",
                "followUpMode": "",
            },
        )
        result.setdefault("snapshotScope", "persisted")
        result.setdefault("runtimeResident", False)
        result.setdefault("historyState", "available")
        return result

    def _cold_recent_history_snapshot(
        self,
        session_id: str,
    ) -> dict[str, object] | None:
        """Read a bounded existing transcript window without Host lifecycle."""

        root = self._history_root_for(session_id)
        if root is None:
            return None
        try:
            snapshot = self._history_manager_for().recent_session_snapshot(
                session_id,
                durable_only=True,
                transcript_root=root,
            )
        except DurableHistoryUnavailable as exc:
            if exc.missing:
                return None
            raise
        except AgentRuntimeError:
            raise
        except Exception as exc:
            raise DurableHistoryUnavailable(
                "durable Session transcript could not be projected safely"
            ) from exc
        result = dict(snapshot)
        result.setdefault("sessionId", session_id)
        result.setdefault("isIdle", True)
        result.setdefault("toolHistoryEvents", [])
        result.setdefault("telemetry", None)
        result.setdefault("snapshotScope", "persisted")
        result.setdefault("runtimeResident", False)
        result.setdefault("historyState", "available")
        return result

    def _persisted_snapshot(self, session_id: str) -> dict[str, object]:
        """Return an honest cold snapshot for read-only HTTP projections.

        Listing a newly-created team Session must not mint a worker token or
        start Docker merely to discover that Pi has no transcript yet. Once a
        Session is resident, the ordinary Pi manager remains the richer
        snapshot authority; this shape mirrors the manager's empty-history
        result for a cold Session.
        """

        historical = self._cold_history_snapshot(session_id)
        if historical is not None:
            return historical
        session = dict(self.context.sessions.get(session_id))
        return {
            "sessionId": str(session.get("id") or session_id),
            # Persisted active/busy is not proof that a worker survived a
            # service restart.  A cold snapshot is always a non-resident
            # projection and must not claim that execution is currently live.
            "isIdle": True,
            "messages": [],
            "toolHistoryEvents": [],
            "telemetry": None,
            "messageQueue": {
                "steering": [],
                "followUp": [],
                "steeringMode": "",
                "followUpMode": "",
            },
            "snapshotScope": "persisted",
            "runtimeResident": False,
            "historyState": "missing",
        }

    def runtime_status(self) -> dict[str, object]:
        with self._lock:
            managers = tuple(self._managers.items())
        statuses = [dict(manager.runtime_status()) for _, manager in managers]
        active = [
            session_id
            for session_id, status in statuses_for_sessions(managers, statuses)
            if status.get("status") == "busy"
        ]
        status_values = {str(item.get("status") or "") for item in statuses}
        status = (
            "busy"
            if active
            else "faulted"
            if "faulted" in status_values
            else "ready"
            if any(value in {"ready", "busy"} for value in status_values)
            else "stopped"
        )
        return {
            "schemaVersion": "rag-ime.agent-runtime.v1",
            "enabled": bool(self.config.enabled),
            "managed": True,
            "isolated": True,
            "status": status,
            "driverId": self.driver_id,
            "runtimeKind": self.runtime_kind,
            "runtimeVersion": self.config.pi_version,
            "piVersion": self.config.pi_version,
            "protocolVersion": self.config.protocol_version,
            "idleTimeoutSeconds": self.config.idle_timeout_seconds,
            "activeSessionId": active[0] if len(active) == 1 else None,
            "activeSessionIds": active,
            "activeCompletionIds": [],
            "openSessionIds": [session_id for session_id, _ in managers],
            "lastError": next(
                (str(item.get("lastError") or "") for item in statuses if item.get("lastError")),
                "",
            ),
            "capabilities": {
                # Team commands live in the trusted runtime image, so the
                # server-side config intentionally has no host executable.
                "rpc": bool(self.config.executable)
                or bool(self.config.enabled and self.binding_resolver),
                "sessions": True,
                "multiSession": True,
                "tools": True,
                "dynamicTools": True,
                "isolatedExecution": True,
                "modelConfigured": self.config.model_configured,
            },
            "teamExecution": {
                "managerCount": len(managers),
                "bindingRequired": True,
                "sharedHostFallback": False,
            },
        }

    def ensure(self, session_id: str) -> dict[str, object]:
        return self._manager_for(session_id).ensure(session_id)

    def command_catalog(self, session_id: str) -> list[dict[str, object]]:
        manager = self._resident_manager(session_id)
        return manager.command_catalog(session_id) if manager is not None else []

    def invoke_command(self, session_id: str, command: str) -> dict[str, object]:
        return self._manager_for(session_id).invoke_command(session_id, command)

    def prompt(
        self,
        session_id: str,
        message: str,
        *,
        images: list[Mapping[str, str]] | None = None,
        client_message_id: str = "",
        delivery: str = "prompt",
    ) -> dict[str, object]:
        return self._manager_for(session_id).prompt(
            session_id,
            message,
            images=images,
            client_message_id=client_message_id,
            delivery=delivery,
        )

    def messages(self, session_id: str) -> list[dict[str, object]]:
        manager = self._resident_manager(session_id)
        if manager is None:
            cold = self._persisted_snapshot(session_id)
            values = cold.get("messages")
            if not isinstance(values, list):
                return []
            return [dict(value) for value in values if isinstance(value, Mapping)]
        return manager.messages(session_id)

    def model_catalog(self, session_id: str) -> dict[str, object]:
        manager = self._resident_manager(session_id)
        if manager is not None:
            return manager.model_catalog(session_id)
        session = dict(self.context.sessions.get(session_id))
        provider, model = self.config.resolved_model_reference(session)
        selected = (
            {"provider": provider, "id": model, "name": model}
            if provider and model
            else None
        )
        return {
            "selected": selected,
            "models": [selected] if selected is not None else [],
            "thinkingLevel": str(session.get("thinkingLevel") or "off"),
            "snapshotScope": "persisted",
            "runtimeResident": False,
        }

    def set_model(
        self,
        session_id: str,
        *,
        provider: str,
        model_id: str,
        max_tokens: int | None = None,
    ) -> dict[str, object]:
        return self._manager_for(session_id).set_model(
            session_id,
            provider=provider,
            model_id=model_id,
            max_tokens=max_tokens,
        )

    def set_thinking_level(self, session_id: str, *, level: str) -> dict[str, object]:
        return self._manager_for(session_id).set_thinking_level(session_id, level=level)

    def fork_candidates(self, session_id: str) -> list[dict[str, object]]:
        manager = self._resident_manager(session_id)
        return manager.fork_candidates(session_id) if manager is not None else []

    def fork_session(self, source_session_id: str, target_session_id: str, *, entry_id: str) -> dict[str, object]:
        return self._manager_for(source_session_id).fork_session(
            source_session_id,
            target_session_id,
            entry_id=entry_id,
        )

    def rewind_session(self, session_id: str, *, entry_id: str) -> dict[str, object]:
        return self._manager_for(session_id).rewind_session(session_id, entry_id=entry_id)

    def complete_once(
        self,
        *,
        request_id: str,
        provider: str,
        model_id: str,
        thinking_level: str,
        message: str,
        on_text_delta: Callable[[str], None] | None = None,
        timeout_seconds: float = 120.0,
    ) -> dict[str, object]:
        return self._existing_manager().complete_once(
            request_id=request_id,
            provider=provider,
            model_id=model_id,
            thinking_level=thinking_level,
            message=message,
            on_text_delta=on_text_delta,
            timeout_seconds=timeout_seconds,
        )

    def cancel_completion(self, request_id: str) -> bool:
        with self._lock:
            managers = tuple(self._managers.values())
        return any(manager.cancel_completion(request_id) for manager in managers)

    def available_models(self) -> list[dict[str, object]]:
        with self._lock:
            managers = tuple(self._managers.values())
        if len(managers) == 1:
            return managers[0].available_models()
        provider = str(self.config.provider or "").strip()
        model = str(self.config.model or "").strip()
        return [{"provider": provider, "id": model, "name": model}] if provider and model else []

    def tool_catalog(self, session_id: str) -> list[dict[str, object]]:
        manager = self._resident_manager(session_id)
        return manager.tool_catalog(session_id) if manager is not None else []

    def abort(self, session_id: str) -> Mapping[str, object] | None:
        normalized = str(session_id).strip()
        manager = self._resident_manager(normalized)
        pi_result: Mapping[str, object] | None = None
        pi_error: Exception | None = None
        if manager is not None:
            try:
                # Keep Pi's native abort first so its turn/UI state is
                # settled before the enclosing execution scope is torn down.
                pi_result = manager.abort(normalized)
            except Exception as exc:
                pi_error = exc
        # Revocation and user Stop also need to terminate workspace/command
        # workers that are outside Pi's own Host process.  This callback is
        # deliberately injected by the team coordinator; the personal
        # factory leaves it unset.  A cold Session may still have an active
        # one-shot worker, so this path must not depend on a resident manager
        # or resolve a new binding.
        stop_error: Exception | None = None
        if self.execution_stop is not None and normalized:
            try:
                self.execution_stop(normalized)
            except Exception as exc:
                stop_error = exc
        if stop_error is not None:
            if pi_error is not None:
                stop_error.add_note(f"Pi abort also failed: {pi_error}")
            raise stop_error
        if pi_error is not None:
            raise pi_error
        return pi_result

    def close_session(self, session_id: str) -> bool:
        manager = self._resident_manager(session_id)
        if manager is None:
            return False
        return manager.close_session(session_id)

    def compact(self, session_id: str, instructions: str = "") -> dict[str, object]:
        return self._manager_for(session_id).compact(session_id, instructions)

    def has_pending_approval(self, session_id: str, approval_id: str) -> bool:
        manager = self._resident_manager(session_id)
        return (
            manager.has_pending_approval(session_id, approval_id)
            if manager is not None
            else False
        )

    def has_pending_review(self, session_id: str, run_id: str) -> bool:
        manager = self._resident_manager(session_id)
        return (
            manager.has_pending_review(session_id, run_id)
            if manager is not None
            else False
        )

    def pending_ui_requests(self, session_id: str) -> list[dict[str, object]]:
        manager = self._resident_manager(session_id)
        if manager is None:
            return []
        return manager.pending_ui_requests(session_id)

    def resolve_review(self, session_id: str, run_id: str, *, reviewed: bool) -> None:
        self._manager_for(session_id).resolve_review(session_id, run_id, reviewed=reviewed)

    def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        *,
        approved: bool,
        resolution_state: str = "",
    ) -> None:
        self._manager_for(session_id).resolve_approval(
            session_id,
            approval_id,
            approved=approved,
            resolution_state=resolution_state,
        )

    def resolve_ui_request(
        self,
        session_id: str,
        request_id: str,
        *,
        response: Mapping[str, object],
    ) -> dict[str, object]:
        return self._manager_for(session_id).resolve_ui_request(
            session_id,
            request_id,
            response=response,
        )

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        manager = self._resident_manager(session_id)
        if manager is None:
            return self._persisted_snapshot(session_id)
        return manager.session_snapshot(session_id)

    def recent_session_snapshot(self, session_id: str) -> dict[str, object]:
        manager = self._resident_manager(session_id)
        if manager is None:
            historical = self._cold_recent_history_snapshot(session_id)
            if historical is not None:
                return historical
            session = dict(self.context.sessions.get(session_id))
            return {
                "sessionId": str(session.get("id") or session_id),
                "isIdle": True,
                "messages": [],
                "toolHistoryEvents": [],
                "snapshotScope": "persisted",
                "runtimeResident": False,
                "historyState": "missing",
            }
        return manager.recent_session_snapshot(session_id)

    def retire_recovered_turn(self, session_id: str, expected_turn_id: str) -> dict[str, object]:
        return self._manager_for(session_id).retire_recovered_turn(session_id, expected_turn_id)

    def plugin_list(self) -> list[dict[str, object]]:
        with self._lock:
            managers = tuple(self._managers.values())
        return managers[0].plugin_list() if len(managers) == 1 else []

    def plugin_catalog(self) -> list[dict[str, object]]:
        with self._lock:
            managers = tuple(self._managers.values())
        return managers[0].plugin_catalog() if len(managers) == 1 else []

    def plugin_create_package(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._existing_manager().plugin_create_package(payload)

    def plugin_validate(self, source_path: str) -> dict[str, object]:
        return self._existing_manager().plugin_validate(source_path)

    def plugin_prepare_package(self, source: str) -> dict[str, object]:
        return self._existing_manager().plugin_prepare_package(source)

    def plugin_preview_install(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._existing_manager().plugin_preview_install(payload)

    def plugin_install(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._existing_manager().plugin_install(payload)

    def plugin_enable(
        self,
        plugin_id: str,
        *,
        enabled: bool,
        expected_active_digest: str,
        expected_enabled: bool,
    ) -> dict[str, object]:
        return self._existing_manager().plugin_enable(
            plugin_id,
            enabled=enabled,
            expected_active_digest=expected_active_digest,
            expected_enabled=expected_enabled,
        )

    def plugin_uninstall(
        self,
        plugin_id: str,
        *,
        expected_active_digest: str,
        expected_enabled: bool,
    ) -> dict[str, object]:
        return self._existing_manager().plugin_uninstall(
            plugin_id,
            expected_active_digest=expected_active_digest,
            expected_enabled=expected_enabled,
        )

    def plugin_rollback(
        self,
        plugin_id: str,
        *,
        expected_active_digest: str,
        target_digest: str,
    ) -> dict[str, object]:
        return self._existing_manager().plugin_rollback(
            plugin_id,
            expected_active_digest=expected_active_digest,
            target_digest=target_digest,
        )

    def stop(self) -> None:
        with self._lock:
            self._stopping += 1
            self._lifecycle_generation += 1
            managers = (*self._managers.values(), *self._starting.values())
            self._managers.clear()
            self._manager_keys.clear()
            history_manager = self._history_manager
            self._history_manager = None
        if history_manager is not None:
            managers = (*managers, history_manager)
        first_error: BaseException | None = None
        try:
            for manager in managers:
                try:
                    manager.retire()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        finally:
            with self._lock:
                self._stopping -= 1
        if first_error is not None:
            raise first_error


def statuses_for_sessions(
    managers: tuple[tuple[str, PiRuntimeHostManager], ...],
    statuses: list[dict[str, object]],
) -> list[tuple[str, dict[str, object]]]:
    """Pair status snapshots with their Session key without starting Hosts."""

    return [
        (session_id, status)
        for (session_id, _), status in zip(managers, statuses, strict=True)
    ]
