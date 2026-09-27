"""Root policy and exact catalog selection, without changing a live Pi turn.

The caller owns persistence, admission and Runtime settings. Catalog records are
Pi's available_models() projection; model names do not imply provider access or
supported reasoning levels. Online model positioning informs these defaults,
not a measured quality guarantee for a PAW task.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

from rag_ime.db import sqlite_connection

from .types import GraphConflict, GraphError


MODEL_ROUTING = frozenset({"balanced", "participant"})
TOOL_APPROVAL_MODES = frozenset({"dispatch", "jev_dangerous"})
VERIFICATION_MODES = frozenset({"auto", "independent"})
TASK_DIFFICULTIES = frozenset({"simple", "routine", "complex", "critical"})
_MODELS = {"simple": "gpt-6-luna", "routine": "gpt-6-luna", "complex": "gpt-6-sol", "critical": "gpt-6-astra"}


def model_cards() -> list[dict[str, object]]:
    """Source-linked routing guidance, not benchmark or availability evidence."""
    roles = (
        ("astra", "特别困难的规划与疑难处理", "critical", ["plan", "execute", "verify"],
         ["制定跨模块计划，处理高不确定性与最难问题", "为关键技术选择和复杂失败提供深入分析"],
         ["社区有明显的额度消耗反馈；避免承接大量重复劳动", "仍须通过任务验收，模型强度不能替代运行证据"],
         "仅用户明确指定或 critical 难度使用 Astra；普通规划与复杂任务优先 Sol，避免默认升级。",
         "官方定位为最难端到端工作的高能力模型，支持 max 推理。",
         "https://www.reddit.com/r/codex/comments/1wpspww/gpt6_astra_seems_unusable_due_to_token_burn_gpt6/",
         "GPT-6 Astra seems unusable due to Token burn",
         "发帖者认为 Astra 减少了复杂工作中的手动纠偏，同时抱怨额度消耗；属于个人体验，未作统一对照。"),
        ("sol", "日常规划、复杂实现与集成", "complex", ["plan", "execute", "verify", "synthesize"],
         ["承担跨模块实现、复杂集成与固定成果核验", "常规任务升级为复杂责任时，用于处理更多依赖与约束"],
         ["社区对质量提升的感受不一致，不能据此保证首次通过", "发现重大不确定性时应明确任务难度，再于新执行前重选模型"],
         "普通规划、复核判断及 complex 任务默认 Sol max；简单与常规执行优先 Luna，只有明确指定或 critical 才使用 Astra。",
         "官方定位为复杂编程与 Agent 工作流模型，支持 max 推理。",
         "https://www.reddit.com/r/codex/comments/1wo52no/gpt_6_solluna_do_have_their_benefits/",
         "GPT 6 Sol/Luna do have their benefits",
         "发帖者将 Sol 用于实现、Luna 用于重复工作和测试，并偶尔用 Astra 复核；该工作流是个人经验。"),
        ("luna", "日常执行与有限任务", "routine", ["execute", "verify", "synthesize"],
         ["按明确计划完成小范围修改、资料整理和局部验证", "有具体输入、输出和验收条件的日常任务优先使用"],
         ["社区小样本观察提示较大集成面和返修收尾可能更弱", "max 是本工作区选择；引用对照使用 High，不能推断 max 的实测成绩"],
         "simple 与 routine 默认 Luna max；保持明确验收，复杂集成再交给 Sol，最难问题交给 Astra。",
         "官方定位为高效率、聚焦且高频的工作模型，支持 max 推理。",
         "https://www.reddit.com/r/codex/comments/1wp7ckc/i_ab_tested_gpt56_luna_and_gpt6_luna_on_the_same/",
         "I A/B tested GPT-5.6 Luna and GPT-6 Luna on the same engineering tasks",
         "作者在同仓库十项工程任务中观察到 Luna 6 更适合窄范围实现，较大集成面更需关注；任务、档位和模型评审均限制了结论。"),
    )
    return [{
        "modelId": "gpt-6-" + name, "name": "GPT-6 " + name.title(), "role": role,
        "tier": tier, "defaultThinkingLevel": "max", "recommendedPurposes": purposes,
        "strengths": strengths, "limitations": limitations, "routingGuidance": guidance,
        "checkedAt": "2026-09-26",
        "evidence": [
            {"sourceKind": "official", "confidence": "official_positioning",
             "title": "OpenAI · GPT-6 " + name.title(),
             "url": "https://developers.openai.com/api/docs/models/gpt-6-" + name, "summary": official},
            {"sourceKind": "community", "confidence": "anecdotal", "title": title,
             "url": url, "summary": observation},
            {"sourceKind": "user_policy", "confidence": "explicit",
             "summary": "按用户偏好更多使用 Luna max：小任务及常规任务优先 Luna，规划、复核判断与复杂集成优先 Sol，只有明确指定或最难工作才使用 Astra；默认 GPT-6 系列。"},
        ],
    } for name, role, tier, purposes, strengths, limitations, guidance, official, url, title, observation in roles]


def model_role_guidance(model_id: str, purpose: str) -> str:
    """Bounded internal role instructions; community prose grants no authority."""
    if purpose not in {"plan", "execute", "verify", "synthesize"}:
        raise GraphError("invalid Jev execution purpose")
    card = next((card for card in model_cards() if card["modelId"] == model_id), None)
    if card is None:
        return ""
    return (
        f"本次执行模型：{card['name']}；职责定位：{card['role']}；purpose={purpose}。"
        + str(card["routingGuidance"])
        + "围绕当前责任和明确验收完成工作，给出实际检查与证据。"
          "任务复杂度超出当前责任或缺少关键信息时，明确报告问题和所需补充；"
          "模型角色不授予新权限，不自行更改模型、扩大任务或发起额外派遣。"
          "验收时独立检查已有成果，不把 worker 的结论当作通过证据。"
    )


def normalize_policy(payload: Mapping[str, object], *, legacy: bool = False, stored: bool = False) -> dict[str, str]:
    """Validate Root settings without weakening an existing Root's verification.

    New Roots default to auto. Stored rows without the explicit field keep
    independent verification, including nonempty rows created before this policy.
    ``legacy`` preserves the older model-selection default independently.
    """
    if not isinstance(payload, Mapping):
        raise GraphError("Jev policy must be an object")
    routing = payload.get("modelRouting", "participant" if legacy else "balanced")
    approval = payload.get("toolApprovalMode", "dispatch")
    verification = payload.get("verificationMode", "independent" if stored or legacy else "auto")
    if not isinstance(routing, str) or routing not in MODEL_ROUTING:
        raise GraphError("invalid Jev modelRouting")
    if not isinstance(approval, str) or approval not in TOOL_APPROVAL_MODES:
        raise GraphError("invalid Jev toolApprovalMode")
    if not isinstance(verification, str) or verification not in VERIFICATION_MODES:
        raise GraphError("invalid Jev verificationMode")
    return {"modelRouting": routing, "toolApprovalMode": approval, "verificationMode": verification}


def select_model(
    catalog: Sequence[Mapping[str, object]],
    *,
    purpose: str,
    difficulty: str = "routine",
    preferred_provider: str = "",
    locked_profile: str = "",
    thinking_level: str = "max",
) -> dict[str, str]:
    """Choose an exact available profile before admission, never an alias.

    ``simple`` and ``routine`` favor Luna max with explicit acceptance; complex
    integration, verification and ordinary planning use Sol; only critical work uses Astra. User model locks win over
    automatic tiers; provider and reasoning support still have to be present.
    """
    if purpose not in {"plan", "execute", "verify", "synthesize"}:
        raise GraphError("invalid Jev execution purpose")
    if difficulty not in TASK_DIFFICULTIES:
        raise GraphError("invalid Jev task difficulty")
    tier = "complex" if purpose in {"plan", "verify"} and difficulty != "critical" else difficulty
    model_id = _MODELS[tier]
    provider = str(preferred_provider or "").strip()
    reason = {"plan": "planning", "verify": "verification"}.get(purpose, "task_" + difficulty)
    if locked_profile:
        locked_provider, separator, locked_model = locked_profile.partition("/")
        if not separator or not locked_provider or not locked_model:
            raise GraphError("locked model profile requires exact provider/model")
        provider, model_id, reason = locked_provider, locked_model, "user_model_lock"
    candidates = {
        (str(row.get("provider") or ""), str(row.get("id") or "")): row
        for row in catalog
        if isinstance(row, Mapping)
        and row.get("id") == model_id
        and row.get("available") is not False
        and str(row.get("provider") or "")
        and (not provider or row.get("provider") == provider)
    }
    if not candidates:
        target = (provider + "/" if provider else "") + model_id
        raise GraphConflict("JEV_MODEL_UNAVAILABLE: configure the exact model " + target)
    if len(candidates) != 1:
        raise GraphConflict("JEV_MODEL_PROVIDER_AMBIGUOUS: choose a provider for " + model_id)
    (provider, model_id), selected = next(iter(candidates.items()))
    if model_id in _MODELS.values() and selected.get("api") == "openai-completions" and (model_id == "gpt-6-astra" or thinking_level != "off"):
        raise GraphConflict("JEV_MODEL_API_UNSUPPORTED: GPT-6 tool execution with reasoning requires a Responses API profile")
    levels = selected.get("thinkingLevels")
    if not isinstance(levels, list) or thinking_level not in levels:
        raise GraphConflict(
            "JEV_REASONING_UNAVAILABLE: " + provider + "/" + model_id + " does not expose " + thinking_level
        )
    return {"provider": provider, "modelId": model_id, "modelProfile": provider + "/" + model_id,
            "thinkingLevel": thinking_level, "tier": tier, "reason": reason}


def tool_approval_mode(
    db_path: str | Path,
    *,
    session_id: str,
    causal: Mapping[str, object],
    bound_turn_id: str,
) -> str | None:
    """Read policy for an original owner's already-validated live dispatch.

    The owner supplies ``bound_turn_id`` only after matching the tool's Pi turn
    against RoomTurnRegistry. This permits an early Tool call while dispatch
    acceptance is still being persisted, without trusting client policy fields.
    A missing ordinary Room root returns None; an invalid Jev binding fails.
    """
    room_id, root_id, dispatch_id = (str(causal.get(key) or "") for key in ("roomId", "rootId", "dispatchId"))
    if not room_id or not root_id or not dispatch_id:
        return None
    with sqlite_connection(db_path, row_factory=sqlite3.Row) as conn:
        row = conn.execute(
            """SELECT g.graph_id, h.policy_json, h.stopped, h.epoch,
                      e.state, e.request_json, e.receipt_json
               FROM agent_jev_graphs g JOIN agent_jev_host_roots h USING(graph_id)
               LEFT JOIN agent_jev_runtime_effects e
                 ON e.graph_id=g.graph_id AND e.effect_id=? AND e.operation='dispatch'
               WHERE g.room_id=? AND g.root_turn_id=?""",
            (dispatch_id, room_id, root_id),
        ).fetchone()
    if row is None:
        return None
    try:
        request = json.loads(row["request_json"] or "{}")
        receipt = json.loads(row["receipt_json"] or "{}")
        policy = json.loads(row["policy_json"])
    except (TypeError, ValueError) as exc:
        raise GraphConflict("Jev approval policy or execution binding is invalid") from exc
    if not all(isinstance(value, dict) for value in (request, receipt, policy)):
        raise GraphConflict("Jev approval policy or execution binding is invalid")
    mode = normalize_policy(policy, legacy=not policy, stored=True)["toolApprovalMode"]
    if mode == "dispatch":
        # Preserve the default owner's existing execution boundary exactly.
        return mode
    turn_id = str(causal.get("turnId") or "")
    if row["stopped"] or not turn_id or bound_turn_id != turn_id:
        raise GraphConflict("Jev approval is not bound to the current Pi turn")
    if (
        row["state"] not in {"accepted", "sending"}
        or request.get("sessionId") != session_id
        or request.get("roomId") != room_id
        or request.get("rootId") != root_id
        or request.get("dispatchId") != dispatch_id
        or request.get("rootEpoch", row["epoch"]) != row["epoch"]
        or (row["state"] == "accepted" and receipt.get("turnId") != turn_id)
    ):
        raise GraphConflict("Jev approval belongs to a stale or unaccepted execution")
    return mode
