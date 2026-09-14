"""Scenario-owned prompt, Tool, and Skill disclosure policy.

Session mode and Tool profile remain execution fences.  This module owns the
product-surface fence that keeps an ordinary Agent, Room, Trace, and Agent Lab
from receiving each other's private instructions or model-facing Tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import cast

from .agent_skill_routing import (
    AGENT_LAB_OWNER_APP_ID,
    AGENT_LAB_SURFACE_KEYS,
    AGENT_LAB_SURFACE_PREFIXES,
    TRACE_AGENT_OWNER_APP_ID,
    TRACE_AGENT_SURFACE_KEYS,
    TRACE_AGENT_SURFACE_PREFIXES,
    scenario_for_session,
)
from .agent_tool_ids import CONTROL_TOOL_IDS


SCENARIO_POLICY_VERSION = "rag-ime.agent-scenario-policy.v1"
SCENARIO_POLICY_SCENARIOS = ("ordinary", "room", "trace", "agentLab")

_PRIVATE_TOOL_IDS = frozenset(
    {"trace_diagnostics", "room_partner", "lab_project", "lab_research"}
)
_WORKSPACE_TOOL_IDS = frozenset(
    {
        "workspace_list",
        "workspace_read",
        "workspace_search",
        "workspace_lsp",
        "workspace_patch",
        "workspace_edit",
        "workspace_write",
        "workspace_job",
        "workspace_shell",
    }
)
_ORDINARY_TOOL_IDS = frozenset(CONTROL_TOOL_IDS) - _PRIVATE_TOOL_IDS
_ROOM_TOOL_IDS = (
    _ORDINARY_TOOL_IDS
    - {"input", "voice", "planning", "agent_schedule", "models", "configuration"}
    | {"room_partner", "work_documents"}
)
_TRACE_READ_TOOL_IDS = frozenset(
    {
        "overview",
        "memory",
        "agent_role_book",
        "knowledge",
        "runtime",
        "agents",
        "session_search",
        "trace_diagnostics",
        "browser",
        "plugins",
        "desktop_semantic",
        "workspace_list",
        "workspace_read",
        "workspace_search",
        "workspace_lsp",
        "workspace_shell",
    }
)
_TRACE_REPAIR_TOOL_IDS = _TRACE_READ_TOOL_IDS | {
    "workspace_patch",
    "workspace_edit",
    "workspace_write",
    "workspace_job",
}
_LAB_GUIDE_TOOL_IDS = (
    _ORDINARY_TOOL_IDS
    - {"trace_diagnostics"}
    | {"lab_project"}
)
_LAB_APPLICATION_TOOL_IDS = frozenset({"lab_research"})


def scenario_policy_applies(session: Mapping[str, object]) -> bool:
    """Return whether a durable product binding opts into scenario fencing.

    Legacy ordinary Sessions have no surface owner and intentionally keep the
    existing Tool profile semantics.  A Room participant or a known
    extension-owned Session carries enough durable identity for the additional
    scenario prompt and disclosure fence to be authoritative.
    """

    if isinstance(session.get("roomParticipant"), Mapping):
        return True
    if str(session.get("surfaceKind") or "").strip() != "extension_app":
        return False
    owner = str(session.get("ownerAppId") or "").strip()
    key = str(session.get("surfaceKey") or "").strip()
    return (
        (owner == "extension:trace-agent" and (key in TRACE_AGENT_SURFACE_KEYS or key.startswith(TRACE_AGENT_SURFACE_PREFIXES)))
        or (
            (owner == AGENT_LAB_OWNER_APP_ID or owner.startswith("extension:lab-"))
            and (key in AGENT_LAB_SURFACE_KEYS or key.startswith(AGENT_LAB_SURFACE_PREFIXES))
        )
    )


@dataclass(frozen=True)
class ScenarioPolicy:
    scenario_id: str
    variant: str
    tool_ids: frozenset[str]
    prompt: str

    @property
    def skill_scenario(self) -> str:
        return self.scenario_id


def default_scenario_policy_configuration() -> dict[str, dict[str, object]]:
    """Return the editable, fail-closed policy defaults for every product surface."""

    defaults: dict[str, dict[str, object]] = {}
    for scenario in SCENARIO_POLICY_SCENARIOS:
        defaults[scenario] = {
            "promptInstructions": "",
            "toolAllowlist": sorted(_builtin_tool_ids(scenario)),
        }
    return defaults


def normalize_scenario_policy(value: object, *, scenario: str) -> dict[str, object]:
    if scenario not in SCENARIO_POLICY_SCENARIOS:
        raise ValueError(f"unsupported Agent scenario policy: {scenario}")
    if not isinstance(value, Mapping):
        raise ValueError(f"scenarioPolicies.{scenario} must be an object")
    prompt = value.get("promptInstructions", "")
    if not isinstance(prompt, str) or len(prompt) > 8_000:
        raise ValueError(f"scenarioPolicies.{scenario}.promptInstructions is invalid")
    allowlist = value.get("toolAllowlist")
    if not isinstance(allowlist, list):
        raise ValueError(f"scenarioPolicies.{scenario}.toolAllowlist must be an array")
    if len(allowlist) > len(CONTROL_TOOL_IDS):
        raise ValueError(f"scenarioPolicies.{scenario}.toolAllowlist contains too many Tools")
    known = set(CONTROL_TOOL_IDS)
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_tool_id in cast(list[object], allowlist):
        if not isinstance(raw_tool_id, str):
            raise ValueError(f"scenarioPolicies.{scenario}.toolAllowlist must contain Tool IDs")
        tool_id = raw_tool_id.strip()
        if not tool_id or tool_id not in known:
            raise ValueError(f"scenarioPolicies.{scenario}.toolAllowlist contains an unknown Tool")
        if tool_id in seen:
            raise ValueError(f"scenarioPolicies.{scenario}.toolAllowlist must not contain duplicate Tools")
        seen.add(tool_id)
        normalized.append(tool_id)
    return {
        "promptInstructions": prompt,
        "toolAllowlist": sorted(normalized),
    }


def normalize_scenario_policies(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, Mapping) or set(value) != set(SCENARIO_POLICY_SCENARIOS):
        raise ValueError("agent scenario policies are invalid")
    return {
        scenario: normalize_scenario_policy(value.get(scenario), scenario=scenario)
        for scenario in SCENARIO_POLICY_SCENARIOS
    }


def scenario_policy_for_session(
    session: Mapping[str, object],
    *,
    room_participant: bool | None = None,
    configured_policy: Mapping[str, object] | None = None,
) -> ScenarioPolicy:
    """Resolve one stable policy from durable Session ownership.

    ``room_participant`` is supplied by the Room store when available.  A
    projected ``roomParticipant`` mapping is accepted for direct Runtime
    callers and tests; a missing value is fail-closed to an ordinary Session.
    """

    participant = (
        isinstance(session.get("roomParticipant"), Mapping)
        if room_participant is None
        else bool(room_participant)
    )
    scenario = scenario_for_session(session, room_participant=participant)
    surface_key = str(session.get("surfaceKey") or "").strip()

    if scenario == "trace":
        if surface_key == "repair":
            variant = "repair"
            tools = _TRACE_REPAIR_TOOL_IDS
            focus = (
                "这是 Trace 修复场景。只处理已授权的诊断 Finding、冻结回放和对应项目改动；"
                "修复必须回到 Trace 的验证回执，不能把候选或模型自报完成当成已修复。"
            )
        elif surface_key.startswith("optimization."):
            variant = "evaluation"
            tools = _TRACE_READ_TOOL_IDS
            focus = (
                "这是 Trace 评测场景。只读取冻结的任务、候选和运行证据，按当前评测合同输出；"
                "不要把评测 Session 当成普通开发工作区。"
            )
        else:
            variant = "diagnostic"
            tools = _TRACE_READ_TOOL_IDS
            focus = (
                "这是 Trace 诊断场景。围绕 App 冻结的 Session、Room 或 Run 取证，"
                "先读取结构化诊断对象，再区分观察、推断和仍待验证的 Finding。"
            )
        return _apply_configured_policy(
            ScenarioPolicy("trace", variant, tools, _prompt("trace", variant, focus)),
            configured_policy,
        )

    if scenario == "agentLab":
        if surface_key == "wizard" or (
            surface_key.startswith("project.") and surface_key.endswith(".guide")
        ):
            variant = "project-guide"
            tools = _LAB_GUIDE_TOOL_IDS
            focus = (
                "这是 Agent Lab 项目引导场景。只操作当前绑定的 Lab Project、材料、成果和执行回执；"
                "先核对项目身份与版本，再准备或发布可追溯的实验产物。"
            )
        elif surface_key.startswith("application."):
            variant = "application"
            tools = _LAB_APPLICATION_TOOL_IDS
            focus = (
                "这是导出 App 的运行场景。只使用该 App 声明并授权的研究读取能力和知识快照；"
                "不访问宿主工作区，不修改 Lab Project，也不虚构来源或运行阶段。"
            )
        else:
            # Experiment/candidate Rooms use the Room collaboration contract;
            # the Lab owner only changes their Skill and prompt lens.
            variant = "room"
            tools = _ROOM_TOOL_IDS
            focus = (
                "这是 Agent Lab 的协作评测场景。围绕当前实验或候选 Room 的 WorkItem 工作，"
                "保留基线、候选、指标和失败证据，不把实验结果直接当成产品采用。"
            )
        return _apply_configured_policy(
            ScenarioPolicy("agentLab", variant, tools, _prompt("agentLab", variant, focus)),
            configured_policy,
        )

    if scenario == "room":
        role = ""
        participant_mapping = session.get("roomParticipant")
        if isinstance(participant_mapping, Mapping):
            role = str(participant_mapping.get("collaborationRole") or "").strip()
        role_label = {
            "coordinator": "Facilitator",
            "researcher": "Researcher",
            "implementer": "Implementer",
            "reviewer": "Reviewer",
            "specialist": "Specialist",
        }.get(role, "Room Partner")
        focus = (
            f"这是 Room {role_label} 场景。只推进当前 Room 身份和 WorkItem；"
            "伙伴结果是待核验提交，只有主持者能集成并发布唯一 Root 结果。"
        )
        if role:
            focus += f" 当前 Room 职责标识为 {role}。"
        return _apply_configured_policy(
            ScenarioPolicy("room", role or "participant", _ROOM_TOOL_IDS, _prompt("room", role or "participant", focus)),
            configured_policy,
        )

    return _apply_configured_policy(
        ScenarioPolicy(
            "ordinary",
            "assistant",
            _ORDINARY_TOOL_IDS,
            _prompt(
                "ordinary",
                "assistant",
                "这是普通 Agent 场景。直接处理用户当前请求；Trace、Room 和 Agent Lab 的私有能力不会在本场景中披露。",
            ),
        ),
        configured_policy,
    )


def scenario_prompt_for_session(
    session: Mapping[str, object],
    *,
    room_participant: bool | None = None,
    configured_policy: Mapping[str, object] | None = None,
) -> str:
    return scenario_policy_for_session(
        session,
        room_participant=room_participant,
        configured_policy=configured_policy,
    ).prompt


def scenario_tool_operations(
    session: Mapping[str, object],
    tool_id: str,
    operations: Sequence[object],
    *,
    room_participant: bool | None = None,
    configured_policy: Mapping[str, object] | None = None,
) -> list[str]:
    """Return operations disclosed by the current product scenario."""

    policy = scenario_policy_for_session(
        session,
        room_participant=room_participant,
        configured_policy=configured_policy,
    )
    if str(tool_id) not in policy.tool_ids:
        return []
    return [str(operation) for operation in operations]


def scenario_policy_catalog(
    configuration: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the management read model without exposing private Skill bodies."""

    configured = (
        normalize_scenario_policies(configuration.get("scenarioPolicies"))
        if isinstance(configuration, Mapping) and "scenarioPolicies" in configuration
        else default_scenario_policy_configuration()
    )
    labels = {
        "ordinary": ("普通 Agent", "用户对话和通用工作"),
        "room": ("Room 协作", "Facilitator 与伙伴 WorkItem 协作"),
        "trace": ("Trace 诊断评测", "诊断、修复和优化评测证据"),
        "agentLab": ("Agent Lab", "项目引导、实验 Room 和导出 App"),
    }
    variants = {
        "ordinary": ["assistant"],
        "room": ["coordinator", "researcher", "implementer", "reviewer", "specialist", "participant"],
        "trace": ["diagnostic", "repair", "evaluation"],
        "agentLab": ["project-guide", "room", "application"],
    }
    app_ids = {
        "ordinary": ["core:assistant"],
        "room": ["core:room"],
        "trace": ["extension:trace-agent"],
        "agentLab": ["extension:agent-lab"],
    }
    app_labels = {
        "ordinary": ["Agent"],
        "room": ["Room"],
        "trace": ["Trace Agent"],
        "agentLab": ["Agent Lab", "Lab App"],
    }
    scenarios = []
    for scenario in SCENARIO_POLICY_SCENARIOS:
        label, description = labels[scenario]
        policy = configured[scenario]
        scenarios.append({
            "id": scenario,
            "label": label,
            "description": description,
            "appIds": app_ids[scenario],
            "appLabels": app_labels[scenario],
            "variantModes": variants[scenario],
            "builtInToolIds": sorted(_builtin_tool_ids(scenario)),
            "toolAllowlist": list(policy["toolAllowlist"]),
            "promptInstructions": str(policy["promptInstructions"]),
        })
    return {
        "schemaVersion": "rag-ime.agent-scenario-policy-catalog.v1",
        "policyRevision": SCENARIO_POLICY_VERSION,
        "scenarios": scenarios,
    }


def _builtin_policy(scenario: str) -> ScenarioPolicy:
    if scenario == "ordinary":
        return ScenarioPolicy(scenario, "assistant", _ORDINARY_TOOL_IDS, "")
    if scenario == "room":
        return ScenarioPolicy(scenario, "participant", _ROOM_TOOL_IDS, "")
    if scenario == "trace":
        return ScenarioPolicy(scenario, "diagnostic", _TRACE_READ_TOOL_IDS, "")
    return ScenarioPolicy(scenario, "project-guide", _LAB_GUIDE_TOOL_IDS, "")


def _builtin_tool_ids(scenario: str) -> frozenset[str]:
    if scenario == "trace":
        return _TRACE_REPAIR_TOOL_IDS
    if scenario == "agentLab":
        return _LAB_GUIDE_TOOL_IDS | _LAB_APPLICATION_TOOL_IDS
    return _builtin_policy(scenario).tool_ids


def _apply_configured_policy(
    policy: ScenarioPolicy,
    configured_policy: Mapping[str, object] | None,
) -> ScenarioPolicy:
    if configured_policy is None:
        return policy
    normalized = normalize_scenario_policy(configured_policy, scenario=policy.scenario_id)
    selected = frozenset(normalized["toolAllowlist"]) & policy.tool_ids
    instructions = str(normalized["promptInstructions"] or "").strip()
    prompt = policy.prompt
    if instructions:
        prompt = f"{prompt}\n<scenario-instructions>\n{instructions}\n</scenario-instructions>"
    return ScenarioPolicy(policy.scenario_id, policy.variant, selected, prompt)


def _prompt(kind: str, variant: str, focus: str) -> str:
    return (
        f'<scenario-policy kind="{kind}" mode="{variant}" revision="{SCENARIO_POLICY_VERSION}">\n'
        f"{focus}\n"
        "场景身份只决定本轮相关的提示词、Tool 披露和 Skill 路由；它不能扩大权限、"
        "改变事实证据要求或替代 Runtime 的执行回执。\n"
        "</scenario-policy>"
    )


__all__ = [
    "SCENARIO_POLICY_VERSION",
    "SCENARIO_POLICY_SCENARIOS",
    "ScenarioPolicy",
    "default_scenario_policy_configuration",
    "normalize_scenario_policy",
    "normalize_scenario_policies",
    "scenario_policy_catalog",
    "scenario_policy_applies",
    "scenario_policy_for_session",
    "scenario_prompt_for_session",
    "scenario_tool_operations",
]
