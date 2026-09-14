from __future__ import annotations

from collections.abc import Mapping
from typing import cast


SKILL_SCENARIOS = ("ordinary", "room", "trace", "agentLab")

# These Skills own a product surface and are never valid in another scenario,
# even when a stale or malicious settings client submits their names there.
SCENARIO_PRIVATE_SKILLS: dict[str, frozenset[str]] = {
    "room": frozenset({"facilitate-room"}),
    "trace": frozenset({"trace-agent-diagnostics"}),
    "agentLab": frozenset({"agent-eval-room-optimizer", "agent-lab-project"}),
}

TRACE_AGENT_OWNER_APP_ID = "extension:trace-agent"
TRACE_AGENT_SURFACE_KEYS = frozenset({"diagnostic", "repair"})
TRACE_AGENT_SURFACE_PREFIXES = ("optimization.",)
AGENT_LAB_OWNER_APP_ID = "extension:agent-lab"
AGENT_LAB_SURFACE_KEYS = frozenset({"wizard"})
AGENT_LAB_SURFACE_PREFIXES = (
    "experiment.",
    "candidate.",
    "project.",
    "application.",
)

_GENERAL_SKILLS = (
    "alignment-and-decision",
    "bootstrap-project-context",
    "ego-browser",
    "implementation-planning",
    "improve-codebase-architecture",
    "independent-review",
    "orchestrate-session",
    "organize-work-documents",
    "pawos-app-builder",
    "pawos-system",
    "plugin-creator",
    "project-maintainer",
    "rag-retrieval-optimization",
    "systematic-debugging",
    "test-driven-implementation",
)


def default_skill_routing() -> dict[str, list[str]]:
    return {
        scenario: sorted(
            {
                *_GENERAL_SKILLS,
                *SCENARIO_PRIVATE_SKILLS.get(scenario, ()),
            }
        )
        for scenario in SKILL_SCENARIOS
    }


def normalize_skill_route(value: object, *, scenario: str) -> list[str]:
    if scenario not in SKILL_SCENARIOS:
        raise ValueError(f"unsupported Agent Skill scenario: {scenario}")
    if not isinstance(value, list):
        raise ValueError(f"skillRouting.{scenario} must be an array")
    values = cast(list[object], value)
    if len(values) > 128:
        raise ValueError(f"skillRouting.{scenario} contains too many Skills")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_skill_id in values:
        if not isinstance(raw_skill_id, str):
            raise ValueError(f"skillRouting.{scenario} must contain only Skill IDs")
        skill_id = raw_skill_id.strip()
        if (
            not skill_id
            or len(skill_id) > 128
            or not skill_id[0].isalnum()
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in skill_id
            )
        ):
            raise ValueError(f"skillRouting.{scenario} contains an invalid Skill ID")
        if skill_id in seen:
            raise ValueError(f"skillRouting.{scenario} must not contain duplicate Skills")
        owner = private_skill_scenario(skill_id)
        if owner is not None and owner != scenario:
            raise ValueError(
                f"Skill {skill_id} belongs to the {owner} scenario, not {scenario}"
            )
        seen.add(skill_id)
        normalized.append(skill_id)
    for required_skill_id in SCENARIO_PRIVATE_SKILLS.get(scenario, ()):
        if required_skill_id not in seen:
            normalized.append(required_skill_id)
    return sorted(normalized)


def normalize_skill_routing(value: object) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        raise ValueError("agent Skill routing scenarios are invalid")
    routing = cast(Mapping[object, object], value)
    if set(routing) != set(SKILL_SCENARIOS):
        raise ValueError("agent Skill routing scenarios are invalid")
    return {
        scenario: normalize_skill_route(routing.get(scenario), scenario=scenario)
        for scenario in SKILL_SCENARIOS
    }


def private_skill_scenario(skill_id: str) -> str | None:
    for scenario, skill_ids in SCENARIO_PRIVATE_SKILLS.items():
        if skill_id in skill_ids:
            return scenario
    return None


def scenario_for_session(
    session: Mapping[str, object],
    *,
    room_participant: bool,
) -> str:
    owner_app_id = str(session.get("ownerAppId") or "").strip()
    surface_kind = str(session.get("surfaceKind") or "").strip()
    surface_key = str(session.get("surfaceKey") or "").strip()
    extension_surface = surface_kind == "extension_app"
    if (
        extension_surface
        and owner_app_id == TRACE_AGENT_OWNER_APP_ID
        and (
            surface_key in TRACE_AGENT_SURFACE_KEYS
            or surface_key.startswith(TRACE_AGENT_SURFACE_PREFIXES)
        )
    ):
        return "trace"
    if (
        extension_surface
        and (
            owner_app_id == AGENT_LAB_OWNER_APP_ID
            or owner_app_id.startswith("extension:lab-")
        )
        and (
            surface_key in AGENT_LAB_SURFACE_KEYS
            or surface_key.startswith(AGENT_LAB_SURFACE_PREFIXES)
        )
    ):
        return "agentLab"
    if room_participant:
        return "room"
    return "ordinary"


def skill_allowlist_for_session(
    configuration: Mapping[str, object],
    session: Mapping[str, object],
    *,
    room_participant: bool,
) -> list[str]:
    routing = normalize_skill_routing(configuration.get("skillRouting"))
    scenario = scenario_for_session(
        session,
        room_participant=room_participant,
    )
    selected = set(routing[scenario])
    if scenario == "agentLab":
        surface_key = str(session.get("surfaceKey") or "")
        project_guide = surface_key.startswith("project.")
        application = surface_key.startswith("application.")
        if application:
            selected.difference_update(SCENARIO_PRIVATE_SKILLS["agentLab"])
        else:
            selected.discard("agent-eval-room-optimizer" if project_guide else "agent-lab-project")
    # Agent Lab participants are still Room participants. Compose both
    # mandatory private capabilities without merging the two configurable
    # general-Skill selections.
    if room_participant:
        selected.update(SCENARIO_PRIVATE_SKILLS["room"])
    return sorted(selected)
