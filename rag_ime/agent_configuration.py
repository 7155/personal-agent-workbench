from __future__ import annotations

import copy
import json
import queue
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .agent_role_identity import canonical_agent_role_id
from .agent_prompt_settings import default_prompt_settings, normalize_prompt_settings, prompt_text
from .agent_skill_routing import (
    SKILL_SCENARIOS,
    default_skill_routing,
    normalize_skill_route,
    normalize_skill_routing,
)
from .agent_scenario_policy import (
    default_scenario_policy_configuration,
    normalize_scenario_policy,
    normalize_scenario_policies,
)
from .agent_runtime_driver import AgentRuntimePolicy
from .contracts.json_schema import validate_contract
from .db import apply_database_migrations


_CONTROL_STREAM_ID = "agent-control"
_EVENT_RETENTION = 2_048
_RUNTIME_KEYS = frozenset(
    {
        "runtime.enabled",
        "runtime.startup",
        "runtime.idleTimeoutSeconds",
    }
)
_MODEL_ROUTE_IDS = (
    "primary",
    "traceDiagnostic",
    "toolAgent",
    "subagent",
    "roomCoordinator",
)
_MODEL_ROUTE_THINKING_LEVELS = frozenset(
    {"inherit", "off", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_STRING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AgentConfigurationConflict(ValueError):
    """Raised when a client writes from a stale multi-device snapshot."""


@dataclass(frozen=True)
class AgentConfigurationUpdate:
    snapshot: dict[str, object]
    event: dict[str, object] | None
    changed_keys: tuple[str, ...]
    runtime_sync_required: bool


def default_agent_configuration(
    *,
    enabled: bool = False,
    idle_timeout_seconds: int = 900,
    role_id: str = "companion-future-v1",
    role_version: str = "1",
    model_profile: str = "openai-codex/gpt-5.6-luna",
    tool_profile_version: str = "control-center-v1",
    resume_last_session: bool = True,
    coordinator_enabled: bool = False,
) -> dict[str, object]:
    configuration: dict[str, object] = {
        "runtime": {
            "enabled": bool(enabled),
            "startup": "lazy",
            "idleTimeoutSeconds": _bounded_integer(
                idle_timeout_seconds,
                field="runtime.idleTimeoutSeconds",
                minimum=0,
                maximum=86_400,
            ),
        },
        "sessionDefaults": {
            "resumeLastSession": bool(resume_last_session),
            "roleId": _identifier(
                canonical_agent_role_id(role_id),
                field="sessionDefaults.roleId",
                maximum=80,
            ),
            "roleVersion": _identifier(
                role_version,
                field="sessionDefaults.roleVersion",
                maximum=32,
            ),
            "modelProfile": _model_profile(model_profile),
            "toolProfileVersion": _identifier(
                tool_profile_version,
                field="sessionDefaults.toolProfileVersion",
                maximum=80,
            ),
            "capabilityDisclosurePreferences": {},
        },
        "coordination": {"enabled": bool(coordinator_enabled)},
        "modelRouting": _default_model_routing(),
        "skillRouting": default_skill_routing(),
        "scenarioPolicies": default_scenario_policy_configuration(),
        "prompts": default_prompt_settings(),
        "capabilityDisclosure": {"projectPreferences": {}},
    }
    _validate_configuration(configuration)
    return configuration


def runtime_policy_from_configuration(
    configuration: Mapping[str, object],
) -> AgentRuntimePolicy:
    runtime = _mapping(configuration.get("runtime"), field="runtime")
    return AgentRuntimePolicy(
        enabled=_boolean(runtime.get("enabled"), field="runtime.enabled"),
        startup=_string(runtime.get("startup"), field="runtime.startup"),
        idle_timeout_seconds=_bounded_integer(
            runtime.get("idleTimeoutSeconds"),
            field="runtime.idleTimeoutSeconds",
            minimum=0,
            maximum=86_400,
        ),
    )


class AgentConfigurationStore:
    """Durable Agent-Kernel desired state with optimistic concurrency."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        persistent_reads: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self._persistent_reads = bool(persistent_reads)
        self._initialize_lock = threading.RLock()
        self._read_lock = threading.RLock()
        self._read_connection: sqlite3.Connection | None = None
        self._initialized = False

    def initialize(self, seed: Mapping[str, object]) -> None:
        if self._initialized:
            return
        configuration = copy.deepcopy(dict(seed))
        configuration.setdefault("prompts", default_prompt_settings())
        configuration.setdefault("scenarioPolicies", default_scenario_policy_configuration())
        defaults = configuration.get("sessionDefaults")
        if isinstance(defaults, dict):
            defaults.setdefault("capabilityDisclosurePreferences", {})
        _ensure_model_routing(configuration)
        configuration.setdefault(
            "capabilityDisclosure",
            {"projectPreferences": {}},
        )
        _validate_configuration(configuration)
        with self._initialize_lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            manager = (
                self._persistent_connection()
                if self._persistent_reads
                else self._connect()
            )
            with manager as conn:
                apply_database_migrations(conn)
                row = conn.execute(
                    "SELECT * FROM agent_configuration_state WHERE singleton_id = 1"
                ).fetchone()
                if row is None:
                    now = _now_ms()
                    conn.execute(
                        """
                        INSERT INTO agent_configuration_state(
                            singleton_id, revision, configuration_json,
                            applied_revision, sync_state, sync_error,
                            updated_at_ms, updated_by
                        ) VALUES (1, 1, ?, 1, 'synchronized', '', ?, 'bootstrap')
                        """,
                        (_json(configuration), now),
                    )
                else:
                    self._canonicalize_legacy_configuration(
                        conn,
                        row,
                        fallback=configuration,
                    )
            self._initialized = True

    def _canonicalize_legacy_configuration(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        fallback: Mapping[str, object],
    ) -> None:
        raw = json.loads(str(row["configuration_json"]))
        if not isinstance(raw, dict):
            raise RuntimeError("agent configuration row is invalid")
        configuration = copy.deepcopy(raw)
        changed_keys: list[str] = []
        if "prompts" not in configuration:
            configuration["prompts"] = default_prompt_settings()
            changed_keys.append("prompts")
        if "scenarioPolicies" not in configuration:
            fallback_policies = fallback.get("scenarioPolicies")
            configuration["scenarioPolicies"] = copy.deepcopy(
                fallback_policies
                if isinstance(fallback_policies, Mapping)
                else default_scenario_policy_configuration()
            )
            changed_keys.append("scenarioPolicies")
        else:
            normalized_scenario_policies = normalize_scenario_policies(
                configuration["scenarioPolicies"]
            )
            if configuration["scenarioPolicies"] != normalized_scenario_policies:
                configuration["scenarioPolicies"] = normalized_scenario_policies
                changed_keys.append("scenarioPolicies")

        defaults = configuration.get("sessionDefaults")
        if not isinstance(defaults, dict):
            raise RuntimeError("agent session defaults are invalid")
        defaults.setdefault("capabilityDisclosurePreferences", {})

        # A short-lived model-settings build persisted the model profile only
        # in a flattened modelRouting object.  Read that shape before replacing
        # it so an upgrade never makes an otherwise healthy local database
        # unbootable.  The nested routes below are the sole canonical shape.
        routing = configuration.get("modelRouting")
        if isinstance(routing, Mapping) and any(
            key in routing
            for key in (
                "sessionModelProfile",
                "sessionThinkingLevel",
                "toolAgentModelProfile",
                "toolAgentThinkingLevel",
                "roomPartnerModelProfile",
                "roomPartnerThinkingLevel",
            )
        ):
            def legacy_route(profile_key: str, thinking_key: str) -> dict[str, str]:
                profile = str(routing.get(profile_key) or "inherit").strip()
                thinking = str(routing.get(thinking_key) or "inherit").strip()
                candidate = {
                    "modelProfile": profile,
                    "thinkingLevel": thinking,
                }
                return _model_route(candidate, field=f"legacyModelRouting.{profile_key}")

            primary = legacy_route("sessionModelProfile", "sessionThinkingLevel")
            tool_agent = legacy_route(
                "toolAgentModelProfile",
                "toolAgentThinkingLevel",
            )
            room_partner = legacy_route(
                "roomPartnerModelProfile",
                "roomPartnerThinkingLevel",
            )
            configuration["modelRouting"] = {
                "primary": primary,
                "toolAgent": tool_agent,
                "subagent": room_partner,
                "roomCoordinator": room_partner,
            }
            changed_keys.append("modelRouting")
            if "modelProfile" not in defaults:
                defaults["modelProfile"] = primary["modelProfile"]
                changed_keys.append("sessionDefaults.modelProfile")
        elif "modelProfile" not in defaults:
            fallback_defaults = fallback.get("sessionDefaults")
            if not isinstance(fallback_defaults, Mapping):
                raise RuntimeError("agent configuration fallback is invalid")
            defaults["modelProfile"] = str(
                fallback_defaults.get("modelProfile") or ""
            )
            changed_keys.append("sessionDefaults.modelProfile")

        configuration.setdefault(
            "capabilityDisclosure",
            {"projectPreferences": {}},
        )
        if "skillRouting" not in configuration:
            fallback_skill_routing = fallback.get("skillRouting")
            configuration["skillRouting"] = copy.deepcopy(
                fallback_skill_routing
                if isinstance(fallback_skill_routing, Mapping)
                else default_skill_routing()
            )
            changed_keys.append("skillRouting")
        else:
            normalized_skill_routing = normalize_skill_routing(
                configuration["skillRouting"]
            )
            if configuration["skillRouting"] != normalized_skill_routing:
                configuration["skillRouting"] = normalized_skill_routing
                changed_keys.append("skillRouting")
        had_trace_diagnostic_route = isinstance(
            configuration.get("modelRouting"), Mapping
        ) and "traceDiagnostic" in configuration["modelRouting"]
        _ensure_model_routing(configuration)
        if not had_trace_diagnostic_route:
            changed_keys.append("modelRouting.traceDiagnostic")
        previous = str(defaults.get("roleId") or "")
        canonical = canonical_agent_role_id(previous)
        if canonical != previous:
            defaults["roleId"] = canonical
            changed_keys.append("sessionDefaults.roleId")
        _validate_configuration(configuration)
        if not changed_keys:
            return
        revision = int(row["revision"]) + 1
        synchronized = str(row["sync_state"]) == "synchronized"
        applied_revision = revision if synchronized else int(row["applied_revision"])
        now = _now_ms()
        conn.execute(
            """
            UPDATE agent_configuration_state
            SET revision = ?, configuration_json = ?, applied_revision = ?,
                updated_at_ms = ?, updated_by = 'legacy-configuration-migrator'
            WHERE singleton_id = 1
            """,
            (revision, _json(configuration), applied_revision, now),
        )
        self._append_event(
            conn,
            "configuration_changed",
            {
                "revision": revision,
                "revisionToken": _revision_token(revision),
                "changedKeys": sorted(set(changed_keys)),
                "updatedBy": "legacy-configuration-migrator",
                "syncState": str(row["sync_state"]),
            },
            created_at_ms=now,
        )

    def snapshot(self) -> dict[str, object]:
        self._require_initialized()
        with self._read_connect() as conn:
            row = self._state_row(conn)
            last_sequence = self._last_sequence(conn)
        return _snapshot_payload(row, last_sequence=last_sequence)

    def update(
        self,
        changes: Mapping[str, object],
        *,
        expected_revision: int,
        updated_by: str,
    ) -> AgentConfigurationUpdate:
        self._require_initialized()
        normalized = _normalize_changes(changes)
        actor = _bounded_actor(updated_by)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._state_row(conn)
            current_revision = int(row["revision"])
            if int(expected_revision) != current_revision:
                raise AgentConfigurationConflict(
                    f"agent configuration revision changed: expected {expected_revision}, "
                    f"current {current_revision}"
                )
            before = _configuration_from_row(row)
            after = copy.deepcopy(before)
            for key, value in normalized.items():
                _set_dotted(after, key, value)
            _validate_configuration(after)
            changed_keys = tuple(
                sorted(key for key in normalized if _get_dotted(before, key) != _get_dotted(after, key))
            )
            if not changed_keys:
                return AgentConfigurationUpdate(
                    snapshot=_snapshot_payload(row, last_sequence=self._last_sequence(conn)),
                    event=None,
                    changed_keys=(),
                    runtime_sync_required=False,
                )

            revision = current_revision + 1
            runtime_sync_required = bool(_RUNTIME_KEYS.intersection(changed_keys))
            if runtime_sync_required:
                sync_state = "pending"
                applied_revision = int(row["applied_revision"])
            elif str(row["sync_state"]) == "synchronized":
                sync_state = "synchronized"
                applied_revision = revision
            else:
                sync_state = str(row["sync_state"])
                applied_revision = int(row["applied_revision"])
            now = _now_ms()
            conn.execute(
                """
                UPDATE agent_configuration_state
                SET revision = ?, configuration_json = ?, applied_revision = ?,
                    sync_state = ?, sync_error = '', updated_at_ms = ?, updated_by = ?
                WHERE singleton_id = 1
                """,
                (revision, _json(after), applied_revision, sync_state, now, actor),
            )
            event = self._append_event(
                conn,
                "configuration_changed",
                {
                    "revision": revision,
                    "revisionToken": _revision_token(revision),
                    "changedKeys": list(changed_keys),
                    "updatedBy": actor,
                    "syncState": sync_state,
                },
                created_at_ms=now,
            )
            updated_row = self._state_row(conn)
            snapshot = _snapshot_payload(updated_row, last_sequence=int(event["sequence"]))
        return AgentConfigurationUpdate(
            snapshot=snapshot,
            event=event,
            changed_keys=changed_keys,
            runtime_sync_required=runtime_sync_required,
        )

    def mark_applied(
        self,
        revision: int,
        *,
        runtime_status: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        return self._mark_sync(
            revision,
            state="synchronized",
            error="",
            runtime_status=runtime_status,
        )

    def mark_failed(
        self,
        revision: int,
        *,
        error: str,
        runtime_status: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        return self._mark_sync(
            revision,
            state="failed",
            error=" ".join(str(error).split())[:500],
            runtime_status=runtime_status,
        )

    def list_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = _EVENT_RETENTION,
    ) -> list[dict[str, object]]:
        self._require_initialized()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_control_events
                WHERE sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (max(0, int(after_sequence)), max(1, min(int(limit), _EVENT_RETENTION))),
            ).fetchall()
        return [_event_payload(row) for row in rows]

    def event_bounds(self) -> tuple[int, int]:
        self._require_initialized()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(sequence), MAX(sequence) FROM agent_control_events"
            ).fetchone()
        if row is None or row[0] is None:
            return 0, 0
        return int(row[0]), int(row[1])

    def _mark_sync(
        self,
        revision: int,
        *,
        state: str,
        error: str,
        runtime_status: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        self._require_initialized()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._state_row(conn)
            if int(row["revision"]) != int(revision):
                return _snapshot_payload(row, last_sequence=self._last_sequence(conn)), None
            applied_revision = int(revision) if state == "synchronized" else int(row["applied_revision"])
            conn.execute(
                """
                UPDATE agent_configuration_state
                SET applied_revision = ?, sync_state = ?, sync_error = ?
                WHERE singleton_id = 1
                """,
                (applied_revision, state, error),
            )
            event_type = "configuration_applied" if state == "synchronized" else "configuration_failed"
            event = self._append_event(
                conn,
                event_type,
                {
                    "revision": int(revision),
                    "revisionToken": _revision_token(int(revision)),
                    "syncState": state,
                    "error": error,
                    "runtime": _safe_runtime_status(runtime_status),
                },
            )
            updated = self._state_row(conn)
            snapshot = _snapshot_payload(updated, last_sequence=int(event["sequence"]))
        return snapshot, event

    def _append_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        payload: Mapping[str, object],
        *,
        created_at_ms: int | None = None,
    ) -> dict[str, object]:
        sequence = self._last_sequence(conn) + 1
        event_id = f"{_CONTROL_STREAM_ID}:{sequence}"
        created = int(created_at_ms if created_at_ms is not None else _now_ms())
        conn.execute(
            """
            INSERT INTO agent_control_events(
                sequence, event_id, event_type, payload_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (sequence, event_id, event_type, _json(dict(payload)), created),
        )
        conn.execute(
            "DELETE FROM agent_control_events WHERE sequence <= ?",
            (max(0, sequence - _EVENT_RETENTION),),
        )
        event = {
            "schemaVersion": "rag-ime.agent-control-event.v1",
            "eventId": event_id,
            "sequence": sequence,
            "eventType": event_type,
            "createdAtMs": created,
            "payload": dict(payload),
            "resumeToken": event_id,
        }
        validate_contract(event, "agent-control-event.v1.json")
        return event

    @staticmethod
    def _state_row(conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM agent_configuration_state WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("agent configuration store is not initialized")
        return row

    @staticmethod
    def _last_sequence(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM agent_control_events").fetchone()
        return int(row[0]) if row is not None else 0

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("agent configuration store is not initialized")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _persistent_connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            conn.close()
            raise
        else:
            with self._read_lock:
                previous = self._read_connection
                self._read_connection = conn
            if previous is not None:
                previous.close()

    @contextmanager
    def _read_connect(self) -> Iterator[sqlite3.Connection]:
        with self._read_lock:
            conn = self._read_connection
            if conn is not None:
                yield conn
                return
        with self._connect() as fallback:
            yield fallback

    def close(self) -> None:
        with self._read_lock:
            conn = self._read_connection
            self._read_connection = None
        if conn is not None:
            conn.close()


class AgentControlEventHub:
    """Persistent global Agent control stream for every UI or gateway client."""

    def __init__(self, store: AgentConfigurationStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._subscribers: set[queue.Queue[dict[str, object]]] = set()

    def fan_out(self, event: Mapping[str, object] | None) -> None:
        if event is None:
            return
        item = dict(event)
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(item)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(item)
                except (queue.Empty, queue.Full):
                    pass

    def subscribe(
        self,
        *,
        after_event_id: str = "",
        heartbeat_seconds: float = 10.0,
    ) -> Iterator[bytes]:
        after_sequence = _control_event_sequence(after_event_id)
        if after_event_id and after_sequence is None:
            gap = True
            after_sequence = 0
        else:
            first, last = self.store.event_bounds()
            gap = bool(
                after_sequence
                and (
                    after_sequence > last
                    or (first > 0 and after_sequence < first - 1)
                )
            )
        subscriber: queue.Queue[dict[str, object]] = queue.Queue(maxsize=128)
        with self._lock:
            if gap:
                snapshot = self.store.snapshot()
                replay = [
                    _snapshot_required_event(
                        after_event_id=after_event_id,
                        last_event_id=str(snapshot["lastEventId"]),
                    )
                ]
            else:
                replay = self.store.list_events(after_sequence=after_sequence or 0)
            self._subscribers.add(subscriber)
        delivered = 0 if gap else (after_sequence or 0)
        try:
            yield b": connected\n\n"
            for event in replay:
                delivered = max(delivered, int(event["sequence"]))
                yield _event_sse(event)
            while True:
                try:
                    event = subscriber.get(timeout=heartbeat_seconds)
                    if int(event["sequence"]) <= delivered:
                        continue
                    delivered = int(event["sequence"])
                    yield _event_sse(event)
                except queue.Empty:
                    yield b": heartbeat\n\n"
        finally:
            with self._lock:
                self._subscribers.discard(subscriber)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


def _snapshot_payload(row: sqlite3.Row, *, last_sequence: int) -> dict[str, object]:
    revision = int(row["revision"])
    configuration = _configuration_from_row(row)
    payload = {
        "schemaVersion": "rag-ime.agent-configuration.v1",
        "revision": revision,
        "revisionToken": _revision_token(revision),
        "configuration": configuration,
        "sync": {
            "state": str(row["sync_state"]),
            "appliedRevision": int(row["applied_revision"]),
            "error": str(row["sync_error"] or ""),
        },
        "updatedAtMs": int(row["updated_at_ms"]),
        "updatedBy": str(row["updated_by"]),
        "lastEventId": f"{_CONTROL_STREAM_ID}:{last_sequence}" if last_sequence else "",
    }
    validate_contract(payload, "agent-configuration.v1.json")
    return payload


def _event_payload(row: sqlite3.Row) -> dict[str, object]:
    raw = json.loads(str(row["payload_json"]))
    payload = {
        "schemaVersion": "rag-ime.agent-control-event.v1",
        "eventId": str(row["event_id"]),
        "sequence": int(row["sequence"]),
        "eventType": str(row["event_type"]),
        "createdAtMs": int(row["created_at_ms"]),
        "payload": raw if isinstance(raw, dict) else {},
        "resumeToken": str(row["event_id"]),
    }
    validate_contract(payload, "agent-control-event.v1.json")
    return payload


def _snapshot_required_event(*, after_event_id: str, last_event_id: str) -> dict[str, object]:
    last_sequence = _control_event_sequence(last_event_id) or 0
    event = {
        "schemaVersion": "rag-ime.agent-control-event.v1",
        "eventId": f"{_CONTROL_STREAM_ID}:snapshot",
        "sequence": last_sequence,
        "eventType": "snapshot_required",
        "createdAtMs": _now_ms(),
        "payload": {
            "reason": "agent_control_event_replay_gap",
            "afterEventId": after_event_id,
            "snapshotEndpoint": "/api/agent/configuration",
        },
        "resumeToken": last_event_id,
    }
    validate_contract(event, "agent-control-event.v1.json")
    return event


def _event_sse(event: Mapping[str, object]) -> bytes:
    event_id = str(event.get("eventId") or "")
    event_type = str(event.get("eventType") or "message")
    body = json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: {event_type}\ndata: {body}\n\n".encode("utf-8")


def _configuration_from_row(
    row: sqlite3.Row,
    *,
    canonicalize_role_id: bool = True,
) -> dict[str, object]:
    raw = json.loads(str(row["configuration_json"]))
    if not isinstance(raw, dict):
        raise RuntimeError("agent configuration row is invalid")
    defaults = raw.get("sessionDefaults")
    if isinstance(defaults, dict):
        defaults["roleId"] = (
            canonical_agent_role_id(defaults.get("roleId"))
            if canonicalize_role_id
            else str(defaults.get("roleId") or "")
        )
        defaults.setdefault("capabilityDisclosurePreferences", {})
    raw.setdefault(
        "capabilityDisclosure",
        {"projectPreferences": {}},
    )
    _ensure_model_routing(raw)
    raw.setdefault("prompts", default_prompt_settings())
    raw.setdefault("scenarioPolicies", default_scenario_policy_configuration())
    _validate_configuration(raw)
    return raw


def _normalize_changes(changes: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(changes, Mapping):
        raise ValueError("agent configuration changes must be an object")
    if not 1 <= len(changes) <= 16:
        raise ValueError("agent configuration update requires between 1 and 16 changes")
    normalized: dict[str, object] = {}
    for raw_key, value in changes.items():
        key = str(raw_key)
        if key == "runtime.enabled":
            normalized[key] = _boolean(value, field=key)
        elif key == "runtime.startup":
            startup = _string(value, field=key)
            if startup != "lazy":
                raise ValueError("runtime.startup only supports lazy")
            normalized[key] = startup
        elif key == "runtime.idleTimeoutSeconds":
            normalized[key] = _bounded_integer(value, field=key, minimum=0, maximum=86_400)
        elif key in {"sessionDefaults.resumeLastSession", "coordination.enabled"}:
            normalized[key] = _boolean(value, field=key)
        elif key in {
            "sessionDefaults.roleId",
            "sessionDefaults.roleVersion",
            "sessionDefaults.toolProfileVersion",
        }:
            maximum = 32 if key.endswith("roleVersion") else 80
            normalized[key] = _identifier(
                canonical_agent_role_id(value) if key == "sessionDefaults.roleId" else value,
                field=key,
                maximum=maximum,
            )
        elif key == "sessionDefaults.modelProfile":
            normalized[key] = _model_profile(value)
        elif key == "sessionDefaults.capabilityDisclosurePreferences":
            normalized[key] = _capability_disclosure_preferences(value)
        elif key.startswith("modelRouting."):
            route_id = key.removeprefix("modelRouting.")
            if route_id not in _MODEL_ROUTE_IDS:
                raise ValueError(f"unsupported Agent model route: {route_id}")
            normalized[key] = _model_route(value, field=key)
        elif key.startswith("skillRouting."):
            scenario = key.removeprefix("skillRouting.")
            normalized[key] = normalize_skill_route(value, scenario=scenario)
        elif key.startswith("scenarioPolicies."):
            scenario = key.removeprefix("scenarioPolicies.")
            normalized[key] = normalize_scenario_policy(value, scenario=scenario)
        elif key in {"prompts.systemInstructions", "prompts.compactionInstructions"}:
            normalized[key] = prompt_text(value, field=key)
        elif key == "capabilityDisclosure.projectPreferences":
            normalized[key] = _project_disclosure_preferences(value)
        else:
            raise ValueError(f"unsupported agent configuration key: {key}")
    return normalized


def _validate_configuration(configuration: Mapping[str, object]) -> None:
    if set(configuration) != {
        "runtime",
        "sessionDefaults",
        "coordination",
        "modelRouting",
        "capabilityDisclosure",
        "skillRouting",
        "scenarioPolicies",
        "prompts",
    }:
        raise ValueError("agent configuration sections are invalid")
    runtime = _mapping(configuration.get("runtime"), field="runtime")
    defaults = _mapping(configuration.get("sessionDefaults"), field="sessionDefaults")
    coordination = _mapping(configuration.get("coordination"), field="coordination")
    model_routing = _mapping(configuration.get("modelRouting"), field="modelRouting")
    skill_routing = _mapping(
        configuration.get("skillRouting"),
        field="skillRouting",
    )
    scenario_policies = _mapping(
        configuration.get("scenarioPolicies"),
        field="scenarioPolicies",
    )
    disclosure = _mapping(
        configuration.get("capabilityDisclosure"),
        field="capabilityDisclosure",
    )
    if set(runtime) != {"enabled", "startup", "idleTimeoutSeconds"}:
        raise ValueError("agent runtime configuration fields are invalid")
    if set(defaults) != {
        "resumeLastSession",
        "roleId",
        "roleVersion",
        "modelProfile",
        "toolProfileVersion",
        "capabilityDisclosurePreferences",
    }:
        raise ValueError("agent session default fields are invalid")
    if set(coordination) != {"enabled"}:
        raise ValueError("agent coordination configuration fields are invalid")
    if set(model_routing) != set(_MODEL_ROUTE_IDS):
        raise ValueError("agent model routing fields are invalid")
    if set(disclosure) != {"projectPreferences"}:
        raise ValueError("agent capability disclosure fields are invalid")
    normalize_skill_routing(skill_routing)
    normalize_scenario_policies(scenario_policies)
    normalize_prompt_settings(configuration.get("prompts"))
    runtime_policy_from_configuration(configuration)
    _boolean(defaults.get("resumeLastSession"), field="sessionDefaults.resumeLastSession")
    _identifier(defaults.get("roleId"), field="sessionDefaults.roleId", maximum=80)
    _identifier(defaults.get("roleVersion"), field="sessionDefaults.roleVersion", maximum=32)
    _model_profile(defaults.get("modelProfile"))
    _identifier(
        defaults.get("toolProfileVersion"),
        field="sessionDefaults.toolProfileVersion",
        maximum=80,
    )
    _capability_disclosure_preferences(
        defaults.get("capabilityDisclosurePreferences")
    )
    _project_disclosure_preferences(disclosure.get("projectPreferences"))
    _boolean(coordination.get("enabled"), field="coordination.enabled")
    for route_id in _MODEL_ROUTE_IDS:
        _model_route(
            model_routing.get(route_id),
            field=f"modelRouting.{route_id}",
        )


def _default_model_routing() -> dict[str, dict[str, str]]:
    return {
        "primary": {
            "modelProfile": "openai-codex/gpt-5.6-luna",
            "thinkingLevel": "max",
        },
        "traceDiagnostic": {
            "modelProfile": "openai-codex/gpt-5.6-sol",
            "thinkingLevel": "high",
        },
        "toolAgent": {
            "modelProfile": "openai-codex/gpt-5.6-luna",
            "thinkingLevel": "max",
        },
        "subagent": {
            "modelProfile": "openai-codex/gpt-5.6-luna",
            "thinkingLevel": "max",
        },
        "roomCoordinator": {
            "modelProfile": "openai-codex/gpt-5.6-sol",
            "thinkingLevel": "high",
        },
    }


def _ensure_model_routing(configuration: dict[str, object]) -> None:
    existing = configuration.get("modelRouting")
    routing = dict(existing) if isinstance(existing, Mapping) else {}
    defaults = _default_model_routing()
    for route_id in _MODEL_ROUTE_IDS:
        routing.setdefault(route_id, defaults[route_id])
    configuration["modelRouting"] = routing


def _model_route(value: object, *, field: str) -> dict[str, str]:
    route = _mapping(value, field=field)
    if set(route) != {"modelProfile", "thinkingLevel"}:
        raise ValueError(f"{field} fields are invalid")
    model_profile = _string(route.get("modelProfile"), field=f"{field}.modelProfile")
    if model_profile != "inherit":
        if (
            len(model_profile) > 200
            or any(character.isspace() for character in model_profile)
            or "/" not in model_profile
        ):
            raise ValueError(f"{field}.modelProfile must be inherit or provider/model")
    thinking_level = _string(
        route.get("thinkingLevel"),
        field=f"{field}.thinkingLevel",
    )
    if thinking_level not in _MODEL_ROUTE_THINKING_LEVELS:
        raise ValueError(f"{field}.thinkingLevel is invalid")
    return {
        "modelProfile": model_profile,
        "thinkingLevel": thinking_level,
    }


def _project_disclosure_preferences(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        raise ValueError(
            "capabilityDisclosure.projectPreferences must be an object"
        )
    if len(value) > 64:
        raise ValueError(
            "capabilityDisclosure.projectPreferences contains too many projects"
        )
    normalized: dict[str, dict[str, str]] = {}
    for raw_project_id, raw_preferences in value.items():
        project_id = _identifier(
            raw_project_id,
            field="capabilityDisclosure.projectPreferences project id",
            maximum=120,
        )
        normalized[project_id] = _capability_disclosure_preferences(
            raw_preferences
        )
    return dict(sorted(normalized.items()))


def _set_dotted(target: dict[str, object], key: str, value: object) -> None:
    section, leaf = key.split(".", 1)
    branch = target.get(section)
    if not isinstance(branch, dict):
        raise ValueError(f"agent configuration section is invalid: {section}")
    branch[leaf] = value


def _get_dotted(target: Mapping[str, object], key: str) -> object:
    section, leaf = key.split(".", 1)
    branch = target.get(section)
    return branch.get(leaf) if isinstance(branch, Mapping) else None


def _capability_disclosure_preferences(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(
            "sessionDefaults.capabilityDisclosurePreferences must be an object"
        )
    if len(value) > 512:
        raise ValueError(
            "sessionDefaults.capabilityDisclosurePreferences contains too many items"
        )
    normalized: dict[str, str] = {}
    for raw_id, raw_preference in value.items():
        capability_id = str(raw_id or "").strip()
        if (
            not capability_id
            or len(capability_id) > 240
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._-"
                for character in capability_id
            )
        ):
            raise ValueError(
                "sessionDefaults.capabilityDisclosurePreferences contains an invalid capability id"
            )
        preference = str(raw_preference or "").strip().lower()
        if preference not in {"inherit", "enabled", "disabled"}:
            raise ValueError(
                "capability disclosure preference must be inherit, enabled, or disabled"
            )
        normalized[capability_id] = preference
    return dict(sorted(normalized.items()))


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _boolean(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _bounded_integer(value: object, *, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _string(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value.strip()


def _identifier(value: object, *, field: str, maximum: int) -> str:
    text = _string(value, field=field)
    if not text or len(text) > maximum or not _STRING_ID.fullmatch(text):
        raise ValueError(f"{field} must be a stable identifier")
    return text


def _model_profile(value: object) -> str:
    text = _string(value, field="sessionDefaults.modelProfile")
    if not text or len(text) > 200 or any(character.isspace() for character in text):
        raise ValueError("sessionDefaults.modelProfile must be a stable model reference")
    return text


def _bounded_actor(value: object) -> str:
    text = " ".join(str(value or "unknown-client").split())[:120]
    return text or "unknown-client"


def _safe_runtime_status(value: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value.get(key)
        for key in (
            "driverId",
            "runtimeKind",
            "runtimeVersion",
            "enabled",
            "status",
            "idleTimeoutSeconds",
        )
        if key in value
    }


def _control_event_sequence(event_id: str) -> int | None:
    if not event_id:
        return 0
    prefix = f"{_CONTROL_STREAM_ID}:"
    if not event_id.startswith(prefix):
        return None
    try:
        return int(event_id[len(prefix) :])
    except ValueError:
        return None


def _revision_token(revision: int) -> str:
    return f"agent-config:{revision}"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now_ms() -> int:
    return int(time.time() * 1000)
