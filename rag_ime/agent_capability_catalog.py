from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence

from .agent_execution_policy import unrestricted_workspace_policy_active
from .agent_scenario_policy import scenario_policy_applies, scenario_policy_for_session
from .agent_skill_routing import skill_allowlist_for_session

_DISCLOSURE_VALUES = frozenset({"inherit", "enabled", "disabled"})
_CAPABILITY_KINDS = frozenset({"tool", "skill", "extension"})


def capability_disclosure_enabled(
    canonical_id: str,
    *,
    session: Mapping[str, object],
    configuration_store: object | None,
) -> bool:
    """Resolve the same use preference as the conversation capability card."""
    global_preferences, _project_id, project_preferences = _configuration_preferences(
        configuration_store, session=session,
    )
    return _capability_disclosure(
        canonical_id,
        session=session,
        global_preferences=global_preferences,
        project_preferences=project_preferences,
        session_preferences=_preferences(session.get("capabilityDisclosurePreferences")),
    )["effective"] == "enabled"


def session_resource_disclosure_policy(
    session: Mapping[str, object], *, configuration_store: object | None,
) -> dict[str, list[str]]:
    """Resolve resource exclusions through the same preferences as the UI."""
    global_preferences, _project_id, project_preferences = _configuration_preferences(
        configuration_store, session=session,
    )
    session_preferences = _preferences(session.get("capabilityDisclosurePreferences"))
    ids = set(global_preferences) | set(project_preferences) | set(session_preferences)
    hidden = [canonical_id for canonical_id in sorted(ids) if _capability_disclosure(
        canonical_id, session=session, global_preferences=global_preferences,
        project_preferences=project_preferences, session_preferences=session_preferences,
    )["effective"] == "disabled"]
    return {
        "disabledSkillNames": [value.removeprefix("skill:") for value in hidden if value.startswith("skill:")],
        "disabledPluginIds": [value.removeprefix("extension:") for value in hidden if value.startswith("extension:")],
    }


def build_capability_catalog(
    *,
    tool_manifests: Sequence[Mapping[str, object]],
    session: Mapping[str, object] | None,
    configuration_store: object | None,
    governed_skills: object | None,
    extensions: object | None,
) -> dict[str, object]:
    effective_at_ms = int(time.time() * 1000)
    global_preferences, project_id, project_preferences = _configuration_preferences(
        configuration_store,
        session=session,
    )
    session_preferences = _preferences(
        session.get("capabilityDisclosurePreferences")
        if session is not None
        else None
    )
    scenario = None
    scenario_skills: set[str] | None = None
    if session is not None and scenario_policy_applies(session):
        scenario = scenario_policy_for_session(session)
        configuration = _configuration_value(configuration_store)
        if configuration is not None:
            participant = isinstance(session.get("roomParticipant"), Mapping)
            scenario_skills = set(
                skill_allowlist_for_session(
                    configuration,
                    session,
                    room_participant=participant,
                )
            )
    items = [
        _tool_item(
            manifest,
            session=session,
            global_preferences=global_preferences,
            project_preferences=project_preferences,
            session_preferences=session_preferences,
            effective_at_ms=effective_at_ms,
        )
        for manifest in tool_manifests
    ]
    if not any(str(manifest.get("id") or "") == "ask" for manifest in tool_manifests):
        items.append(
            _tool_item(
                _native_ask_manifest(session),
                session=session,
                global_preferences=global_preferences,
                project_preferences=project_preferences,
                session_preferences=session_preferences,
                effective_at_ms=effective_at_ms,
            )
        )
    items.extend(
        sorted(
            _skill_items(
                governed_skills,
                session=session,
                global_preferences=global_preferences,
                session_preferences=session_preferences,
                project_preferences=project_preferences,
                effective_at_ms=effective_at_ms,
                scenario_skills=scenario_skills,
            ),
            key=lambda item: str(item.get("canonicalId") or ""),
        )
    )
    items.extend(
        sorted(
            _extension_items(
                extensions,
                session=session,
                global_preferences=global_preferences,
                session_preferences=session_preferences,
                project_preferences=project_preferences,
                effective_at_ms=effective_at_ms,
            ),
            key=lambda item: str(item.get("canonicalId") or ""),
        )
    )
    known_ids = {str(item["canonicalId"]) for item in items}
    for canonical_id in sorted(
        (
            set(global_preferences)
            | set(project_preferences)
            | set(session_preferences)
        )
        - known_ids
    ):
        items.append(
            _removed_item(
                canonical_id,
                global_preferences=global_preferences,
                project_preferences=project_preferences,
                session_preferences=session_preferences,
                effective_at_ms=effective_at_ms,
            )
        )
    revision_material = [
        {
            key: item.get(key)
            for key in (
                "canonicalId",
                "status",
                "revision",
                "authorization",
                "disclosure",
                "effectiveScope",
            )
        }
        for item in items
    ]
    response: dict[str, object] = {
        "schemaVersion": "rag-ime.capability-catalog.v1",
        "ok": True,
        "revision": f"sha256:{_sha256_json(revision_material)}",
        "effectiveAtMs": effective_at_ms,
        "projectScope": (
            {
                "supported": True,
                "identityKind": "workspace_scope_sha256",
                "projectId": project_id,
                "reason": "session_workspace_scope",
            }
            if project_id
            else {
                "supported": False,
                "identityKind": "none",
                "reason": "stable_project_identity_unavailable",
            }
        ),
        "items": items,
    }
    if session is not None:
        response["sessionPolicy"] = {
            "sessionId": session["id"],
            "mode": session["mode"],
            "executionMode": session.get("executionMode", "per_action"),
            "workspaceScopeGranted": session.get(
                "workspaceScopeGranted", False
            ),
            "toolProfileVersion": session["toolProfileVersion"],
            "toolAllowlistMode": session.get("toolAllowlistMode", "profile"),
            "allowedTools": list(session.get("allowedTools") or []),
            "policyRevision": int(session.get("policyRevision") or 1),
            "disclosurePreferences": {
                "globalDefault": global_preferences,
                "projectDefault": project_preferences,
                "session": session_preferences,
                "effective": {
                    str(item["canonicalId"]): str(
                        _mapping(item["disclosure"])["effective"]
                    )
                    for item in items
                },
            },
            "effectiveAtMs": effective_at_ms,
            "scenario": scenario.scenario_id if scenario is not None else "",
            "scenarioVariant": scenario.variant if scenario is not None else "",
        }
    return response


def _tool_item(
    manifest: Mapping[str, object],
    *,
    session: Mapping[str, object] | None,
    global_preferences: Mapping[str, str],
    project_preferences: Mapping[str, str],
    session_preferences: Mapping[str, str],
    effective_at_ms: int,
) -> dict[str, object]:
    tool_id = str(manifest["id"])
    canonical_id = f"tool:{tool_id}"
    authorized = session is not None and manifest.get("enabled") is True
    risk = str(manifest.get("riskLevel") or "R0")
    disclosure = _capability_disclosure(
        canonical_id,
        session=session,
        global_preferences=global_preferences,
        project_preferences=project_preferences,
        session_preferences=session_preferences,
    )
    if manifest.get("scenarioAllowed") is False:
        disclosure = {
            **disclosure,
            "effective": "disabled",
            "state": "hidden",
            "reason": "scenario_policy",
            "scope": "scenario",
        }
    if manifest.get("alwaysAvailable") is True:
        if manifest.get("scenarioAllowed") is False:
            pass
        elif tool_id == "room_partner" and manifest.get("availability") != "online":
            # `alwaysAvailable` describes the formal Room primitive once a
            # real participant identity is present. It must not turn an
            # offline, ordinary Session catalog entry into an enabled
            # capability merely because the tool is registered globally.
            disclosure = {
                **disclosure,
                "effective": "disabled",
                "state": "hidden",
                "reason": "room_context_required",
            }
        else:
            disclosure = {
                "preference": "inherit",
                "effective": "enabled",
                "state": "disclosed",
                "reason": "required_session_tool",
                "scope": "built_in_default",
            }
    return {
        **dict(manifest),
        "canonicalId": canonical_id,
        "kind": "tool",
        "source": {
            "kind": "product",
            "label": "Personal Agent Workbench",
        },
        "status": str(manifest.get("availability") or "offline"),
        "risk": risk,
        "requiredPermissions": _tool_permissions(tool_id, risk),
        "authorization": {
            "state": (
                "authorized"
                if authorized
                else "not_applicable"
                if session is None
                else "denied"
            ),
            "reason": (
                "existing_session_policy_authorizes_tool"
                if authorized
                else "session_context_required"
                if session is None
                else "existing_session_policy_does_not_authorize_tool"
            ),
        },
        "disclosure": disclosure,
        "effectiveScope": str(disclosure["scope"]),
        "reasons": [
            str(disclosure["reason"]),
            (
                "tool_authorized_by_existing_session_policy"
                if authorized
                else "tool_not_authorized_by_existing_session_policy"
            ),
        ],
        "revision": f"tool-spec:{manifest.get('version') or '1'}",
        "effectiveAtMs": effective_at_ms,
    }

def _native_ask_manifest(
    session: Mapping[str, object] | None,
) -> dict[str, object]:
    ordinary_session = (
        session is not None
        and str(session.get("sessionKind") or "conversation") == "conversation"
        and str(session.get("mode") or "assistant")
        in {"assistant", "coordinator"}
    )
    return {
        "schemaVersion": "rag-ime.control-tool-manifest.v1",
        "id": "ask",
        "name": "ask",
        "displayName": "Ask",
        "runtimeOwner": "pi_host",
        "domain": "planning",
        "category": "planning",
        "description": "向用户提出仍需其决定的结构化选择",
        "availability": "online",
        "riskLevel": "R0",
        "operationRisks": {"ask": "R0"},
        "effectiveOperations": ["ask"],
        "enabled": ordinary_session,
        "alwaysAvailable": True,
        "operations": ["ask"],
        "sessionModes": ["assistant", "coordinator"],
        "resultPresentation": "approval",
        "does": "调查后收敛一组实质取舍，并等待用户回答。",
        "when": ["仍有用户必须决定的实质取舍", "用户明确要求 Grill"],
        "notFor": ["可检查的事实", "授权内的可逆默认", "普通进度确认"],
        "output": "结构化问题组和用户选择回执",
        "version": "1",
    }



def _skill_items(
    governed_skills: object | None,
    *,
    session: Mapping[str, object] | None,
    global_preferences: Mapping[str, str],
    project_preferences: Mapping[str, str],
    session_preferences: Mapping[str, str],
    effective_at_ms: int,
    scenario_skills: set[str] | None = None,
) -> list[dict[str, object]]:
    if governed_skills is None:
        return []
    values = governed_skills.governance_catalog()  # type: ignore[attr-defined]
    items: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        skill_id = str(value.get("skillId") or value.get("name") or "")
        if not skill_id:
            continue
        canonical_id = f"skill:{skill_id}"
        disclosure = _capability_disclosure(
            canonical_id,
            session=session,
            global_preferences=global_preferences,
            project_preferences=project_preferences,
            session_preferences=session_preferences,
        )
        scenario_allowed = session is None or scenario_skills is None or skill_id in scenario_skills
        if not scenario_allowed:
            disclosure = {
                **disclosure,
                "effective": "disabled",
                "state": "hidden",
                "reason": "scenario_policy",
                "scope": "scenario",
            }
        authorization_state = (
            "not_applicable"
            if session is None
            else "authorized"
            if scenario_allowed
            else "denied"
        )
        authorization_reason = (
            "session_context_required"
            if session is None
            else "scenario_policy_authorizes_skill"
            if scenario_allowed
            else "scenario_policy"
        )
        risk = {"low": "R0", "medium": "R1", "high": "R2"}.get(
            str(value.get("risk") or ""), "R2"
        )
        items.append(
            {
                "id": skill_id,
                "canonicalId": canonical_id,
                "kind": "skill",
                "source": {
                    "kind": "governed_native",
                    "label": "Governed native Pi Skill",
                },
                "displayName": str(value.get("name") or skill_id),
                "description": str(value.get("does") or ""),
                "status": "governed",
                "risk": risk,
                "requiredPermissions": ["room_skill_load_receipt"],
                "authorization": {
                    "state": authorization_state,
                    "reason": authorization_reason,
                },
                "scenarioAllowed": scenario_allowed,
                "disclosure": disclosure,
                "effectiveScope": str(disclosure["scope"]),
                "reasons": [
                    "native_skill_body_not_disclosed_by_catalog",
                    "scenario_policy" if not scenario_allowed else "room_skill_load_receipt_required",
                    str(disclosure["reason"]),
                ],
                "revision": (
                    f"skill:{value.get('policyId')}@"
                    f"{value.get('policyVersion')}:"
                    f"{value.get('contentRevision')}"
                ),
                "effectiveAtMs": effective_at_ms,
                "when": list(value.get("when") or []),
                "notFor": list(value.get("notFor") or []),
                "input": str(value.get("input") or ""),
                "output": str(value.get("output") or ""),
                "does": str(value.get("does") or ""),
                "stages": list(value.get("stages") or []),
            }
        )
    return items


def _extension_items(
    extensions: object | None,
    *,
    session: Mapping[str, object] | None,
    project_preferences: Mapping[str, str],
    global_preferences: Mapping[str, str],
    session_preferences: Mapping[str, str],
    effective_at_ms: int,
) -> list[dict[str, object]]:
    if extensions is None:
        return []
    catalog = extensions.catalog()  # type: ignore[attr-defined]
    values = catalog.get("items")
    if not isinstance(values, list):
        return []
    items: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        extension_id = str(value.get("id") or "")
        if not extension_id:
            continue
        canonical_id = f"extension:{extension_id}"
        disclosure = _capability_disclosure(
            canonical_id,
            session=session,
            global_preferences=global_preferences,
            project_preferences=project_preferences,
            session_preferences=session_preferences,
        )
        permissions = [
            str(permission) for permission in value.get("permissions") or []
        ]
        enabled = session is not None and value.get("enabled") is True
        source = value.get("source")
        source_label = (
            str(source.get("label") or "")
            if isinstance(source, Mapping)
            else ""
        )
        items.append(
            {
                "id": extension_id,
                "canonicalId": canonical_id,
                "kind": "extension",
                "source": {
                    "kind": "managed_extension",
                    "label": source_label or "Managed Pi extension",
                },
                "displayName": str(value.get("displayName") or extension_id),
                "description": str(value.get("description") or ""),
                "status": str(value.get("installState") or "available"),
                "risk": _extension_risk(permissions),
                "requiredPermissions": permissions,
                "authorization": {
                    "state": (
                        "authorized"
                        if enabled
                        else "not_applicable"
                        if session is None
                        else "denied"
                    ),
                    "reason": (
                        "installed_extension_enabled"
                        if enabled
                        else "session_context_required"
                        if session is None
                        else "extension_not_installed_or_enabled"
                    ),
                },
                "disclosure": disclosure,
                "effectiveScope": str(disclosure["scope"]),
                "reasons": [
                    (
                        "installed_extension_enabled"
                        if enabled
                        else "extension_not_installed_or_enabled"
                    ),
                    str(disclosure["reason"]),
                ],
                "revision": (
                    f"extension:{value.get('installedVersion') or value.get('latestVersion') or 'unversioned'}:"
                    f"{catalog.get('catalogVersion') or 'runtime'}"
                ),
                "effectiveAtMs": effective_at_ms,
            }
        )
    return items


def _removed_item(
    canonical_id: str,
    *,
    project_preferences: Mapping[str, str],
    global_preferences: Mapping[str, str],
    session_preferences: Mapping[str, str],
    effective_at_ms: int,
) -> dict[str, object]:
    raw_kind, separator, native_id = canonical_id.partition(":")
    kind = raw_kind if separator and raw_kind in _CAPABILITY_KINDS else "extension"
    disclosure = _disclosure(
        canonical_id,
        global_preferences=global_preferences,
        project_preferences=project_preferences,
        session_preferences=session_preferences,
    )
    return {
        "id": native_id or canonical_id,
        "canonicalId": canonical_id,
        "kind": kind,
        "source": {
            "kind": "persisted_preference",
            "label": "Persisted disclosure preference",
        },
        "displayName": native_id or canonical_id,
        "description": "",
        "status": "removed",
        "risk": "unknown",
        "requiredPermissions": [],
        "authorization": {
            "state": "denied",
            "reason": "capability_removed_or_unavailable",
        },
        "disclosure": disclosure,
        "effectiveScope": str(disclosure["scope"]),
        "reasons": [
            "capability_removed_or_unavailable",
            str(disclosure["reason"]),
        ],
        "revision": "removed",
        "effectiveAtMs": effective_at_ms,
    }


def _disclosure(
    canonical_id: str,
    *,
    global_preferences: Mapping[str, str],
    project_preferences: Mapping[str, str],
    session_preferences: Mapping[str, str],
) -> dict[str, str]:
    session_preference = session_preferences.get(canonical_id, "inherit")
    project_preference = project_preferences.get(canonical_id, "inherit")
    global_preference = global_preferences.get(canonical_id, "inherit")
    if session_preference != "inherit":
        preference = session_preference
        effective = session_preference
        scope = "session"
        reason = "session_preference"
    elif project_preference != "inherit":
        preference = "inherit"
        effective = project_preference
        scope = "project_default"
        reason = "inherited_project_default"
    elif global_preference != "inherit":
        preference = "inherit"
        effective = global_preference
        scope = "global_default"
        reason = "inherited_global_default"
    else:
        preference = "inherit"
        effective = "disabled" if canonical_id == "tool:memory" else "enabled"
        scope = "built_in_default"
        reason = "inherited_built_in_default"
    return {
        "preference": preference,
        "effective": effective,
        "state": "disclosed" if effective == "enabled" else "hidden",
        "reason": reason,
        "scope": scope,
    }

def _capability_disclosure(
    canonical_id: str,
    *,
    session: Mapping[str, object] | None,
    global_preferences: Mapping[str, str],
    project_preferences: Mapping[str, str],
    session_preferences: Mapping[str, str],
) -> dict[str, str]:
    disclosure = _disclosure(
        canonical_id,
        global_preferences=global_preferences,
        project_preferences=project_preferences,
        session_preferences=session_preferences,
    )
    if (
        session is None
        or not unrestricted_workspace_policy_active(session)
        or canonical_id == "tool:memory"
        or disclosure["scope"] != "built_in_default"
    ):
        return disclosure
    # Full access removes execution restrictions, not capability choices.
    # Memory remains opt-in; tool, skill and extension preferences at every
    # scope still determine what is loaded into a conversation.
    return {
        "preference": "inherit",
        "effective": "enabled",
        "state": "disclosed",
        "reason": "unrestricted_session_profile",
        "scope": "session",
    }


def _configuration_preferences(
    configuration_store: object | None,
    *,
    session: Mapping[str, object] | None,
) -> tuple[dict[str, str], str, dict[str, str]]:
    if configuration_store is None:
        return {}, "", {}
    snapshot = configuration_store.snapshot()  # type: ignore[attr-defined]
    configuration = snapshot.get("configuration")
    defaults = (
        configuration.get("sessionDefaults")
        if isinstance(configuration, Mapping)
        else None
    )
    disclosure = (
        configuration.get("capabilityDisclosure")
        if isinstance(configuration, Mapping)
        else None
    )
    global_preferences = _preferences(
        defaults.get("capabilityDisclosurePreferences")
        if isinstance(defaults, Mapping)
        else None
    )
    project_id = _project_identity(session)
    project_preferences_by_id = (
        disclosure.get("projectPreferences")
        if isinstance(disclosure, Mapping)
        else None
    )
    project_preferences = _preferences(
        project_preferences_by_id.get(project_id)
        if project_id and isinstance(project_preferences_by_id, Mapping)
        else None
    )
    return global_preferences, project_id, project_preferences


def _configuration_value(configuration_store: object | None) -> Mapping[str, object] | None:
    if configuration_store is None:
        return None
    snapshot = configuration_store.snapshot()  # type: ignore[attr-defined]
    configuration = snapshot.get("configuration") if isinstance(snapshot, Mapping) else None
    return configuration if isinstance(configuration, Mapping) else None


def _project_identity(session: Mapping[str, object] | None) -> str:
    if session is None or session.get("workspaceScopeGranted") is not True:
        return ""
    scope_sha256 = str(session.get("workspaceScopeSha256") or "")
    if (
        len(scope_sha256) != 64
        or scope_sha256 != scope_sha256.lower()
        or any(character not in "0123456789abcdef" for character in scope_sha256)
    ):
        return ""
    return f"workspace-{scope_sha256}"


def _preferences(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(preference)
        for key, preference in sorted(value.items())
        if str(preference) in _DISCLOSURE_VALUES
    }


def _tool_permissions(tool_id: str, risk: str) -> list[str]:
    permissions: list[str] = []
    if risk != "R0":
        permissions.append("native_approval")
    if tool_id.startswith("workspace_"):
        permissions.append("workspace_scope")
    return permissions


def _extension_risk(permissions: Sequence[str]) -> str:
    normalized = " ".join(permissions).lower()
    if any(token in normalized for token in ("write", "shell", "network", "execute")):
        return "R2"
    return "R1" if permissions else "R0"


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
