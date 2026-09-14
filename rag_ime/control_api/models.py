from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .errors import ControlApiError, ControlErrorCode


class ControlMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    PATCH = "PATCH"
    DELETE = "DELETE"


class ControlClientKind(str, Enum):
    NATIVE = "native"
    LOOPBACK_WEB = "loopback-web"
    REMOTE_WEB = "remote-web"
    TEAM_WEB = "team-web"


class ControlScope(str, Enum):
    CONTROL_READ = "control.read"
    OVERVIEW_READ = "overview.read"
    INPUT_READ = "input.read"
    AGENT_READ = "agent.read"
    AGENT_WRITE = "agent.write"
    AGENT_APPROVE = "agent.approve"
    AGENT_DELEGATE = "agent.delegate"
    PLANNING_READ = "planning.read"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    KNOWLEDGE_READ = "knowledge.read"
    HISTORY_READ = "history.read"
    DIAGNOSTICS_READ = "diagnostics.read"
    CONFIGURATION_READ = "configuration.read"


@dataclass(frozen=True)
class ControlAccessContext:
    client_kind: ControlClientKind
    granted_scopes: frozenset[str] = frozenset()
    device_id: str = ""
    user_id: str = ""
    space_id: str = ""
    allowed_path_ids: frozenset[str] = frozenset()

    @classmethod
    def team(
        cls, *, user_id: str, space_id: str, allowed_paths: frozenset[str] | set[str],
    ) -> ControlAccessContext:
        """Carry authority resolved by the team gateway, never by request JSON."""
        if not user_id.strip() or not space_id.strip():
            raise ValueError("team access requires an authenticated user and space")
        return cls(
            client_kind=ControlClientKind.TEAM_WEB,
            user_id=user_id.strip(), space_id=space_id.strip(),
            allowed_path_ids=frozenset(allowed_paths),
        )

    @property
    def is_team(self) -> bool:
        return self.client_kind is ControlClientKind.TEAM_WEB

    @classmethod
    def native(cls) -> ControlAccessContext:
        return cls(client_kind=ControlClientKind.NATIVE)

    @classmethod
    def loopback_web(cls) -> ControlAccessContext:
        return cls(client_kind=ControlClientKind.LOOPBACK_WEB)

    @classmethod
    def remote(
        cls,
        *,
        device_id: str = "",
        scopes: frozenset[str] | set[str] = frozenset(),
    ) -> ControlAccessContext:
        return cls(
            client_kind=ControlClientKind.REMOTE_WEB,
            granted_scopes=frozenset(str(value) for value in scopes),
            device_id=str(device_id).strip(),
        )

    @property
    def is_remote(self) -> bool:
        return self.client_kind in {ControlClientKind.REMOTE_WEB, ControlClientKind.TEAM_WEB}

    @property
    def remote_authenticated(self) -> bool:
        if self.is_team:
            return bool(self.user_id and self.space_id)
        return self.is_remote and bool(self.device_id) and bool(self.granted_scopes)


_REQUEST_FIELDS = frozenset({"id", "pathId", "params", "query", "body"})


@dataclass(frozen=True)
class ControlRequest:
    request_id: str
    path_id: str
    params: Mapping[str, object] = field(default_factory=dict)
    query: Mapping[str, object] = field(default_factory=dict)
    body: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ControlRequest:
        unknown = sorted(str(key) for key in set(payload) - _REQUEST_FIELDS)
        if unknown:
            raise ControlApiError(
                ControlErrorCode.INVALID_REQUEST,
                "control request contains unsupported fields",
                details={"fields": unknown},
            )
        request_id = _required_text(payload.get("id"), field_name="id", maximum=128)
        path_id = _required_text(payload.get("pathId"), field_name="pathId", maximum=128)
        return cls(
            request_id=request_id,
            path_id=path_id,
            params=_object(payload.get("params"), field_name="params"),
            query=_object(payload.get("query"), field_name="query"),
            body=_object(payload.get("body"), field_name="body"),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _required_text(self.request_id, field_name="id", maximum=128))
        object.__setattr__(self, "path_id", _required_text(self.path_id, field_name="pathId", maximum=128))
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        object.__setattr__(self, "query", MappingProxyType(dict(self.query)))
        object.__setattr__(self, "body", _json_object(self.body))


def _object(value: object, *, field_name: str) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ControlApiError(
            ControlErrorCode.INVALID_REQUEST,
            f"control request {field_name} must be an object",
            details={"field": field_name},
        )
    if any(not isinstance(key, str) for key in value):
        raise ControlApiError(
            ControlErrorCode.INVALID_REQUEST,
            f"control request {field_name} keys must be strings",
            details={"field": field_name},
        )
    return MappingProxyType(dict(value))


def _required_text(value: object, *, field_name: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ControlApiError(
            ControlErrorCode.INVALID_REQUEST,
            f"control request {field_name} is invalid",
            details={"field": field_name},
        )
    return text


def _json_object(value: Mapping[str, object]) -> Mapping[str, object]:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ControlApiError(
            ControlErrorCode.INVALID_REQUEST,
            "control request body must contain JSON values",
            details={"field": "body"},
        ) from exc
    if len(encoded.encode("utf-8")) > 2_000_000:
        raise ControlApiError(
            ControlErrorCode.INVALID_REQUEST,
            "control request body is too large",
            details={"field": "body", "maximumBytes": 2_000_000},
        )
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - encoded from a dict
        raise ControlApiError(
            ControlErrorCode.INVALID_REQUEST,
            "control request body must be an object",
            details={"field": "body"},
        )
    return MappingProxyType(decoded)
