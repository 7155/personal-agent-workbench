from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from .agent_tool_ids import (
    CONTROL_CENTER_TOOL_PROFILE,
    DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
    FULL_ACCESS_TOOL_PROFILE,
    READONLY_TOOL_PROFILE,
)


READ_ONLY_EXECUTION_MODE = "read_only"
PER_ACTION_EXECUTION_MODE = "per_action"
WORKSPACE_MANAGED_EXECUTION_MODE = "workspace_managed"
FULL_TRUST_EXECUTION_MODE = "full_trust"
# Room-only execution mode. It is deliberately separate from the ordinary
# Session executionMode enum: a Room can opt into this only after its explicit
# start confirmation, while the Session's normal mode and workspace lease stay
# authoritative hard fences.
ROOM_UNRESTRICTED_EXECUTION_MODE = "room_unrestricted"

SUPPORTED_EXECUTION_MODES = frozenset(
    {
        READ_ONLY_EXECUTION_MODE,
        PER_ACTION_EXECUTION_MODE,
        WORKSPACE_MANAGED_EXECUTION_MODE,
        FULL_TRUST_EXECUTION_MODE,
    }
)

WORKSPACE_SCOPE_CONFIRMATION = "APPROVE_WORKSPACE_SCOPE"

APPROVAL_DENY = "deny"
APPROVAL_ASK = "ask"
APPROVAL_AUTO = "auto"
APPROVAL_MODEL = "model"

_WORKSPACE_EFFECTS = frozenset(
    {
        ("workspace_edit", "apply"),
        ("workspace_patch", "apply"),
        ("workspace_lsp", "rename"),
        ("workspace_lsp", "code_action_apply"),
        ("workspace_shell", "run"),
        ("workspace_job", "start"),
        ("workspace_job", "cancel"),
        ("workspace_write", "apply"),
        ("knowledge", "create_base"),
        ("knowledge", "configure_base"),
        ("knowledge", "import_text"),
        ("knowledge", "rebuild"),
        # These are the native Pi names projected by the coordinator adapter.
        # They remain hard-fenced if a stale or alternate adapter reaches the
        # policy helper before translating to a product Tool.
        ("edit", "apply"),
        ("write", "apply"),
        ("apply_patch", "apply"),
        ("bash", "run"),
    }
)

# Room read-only work may inspect an authorized workspace and run a foreground
# validation command in the source-read-only workspace sandbox. It must never
# mutate source, start or cancel a background job, or apply an LSP edit. The
# harness, rather than command text, is the write boundary for workspace_shell.
READ_ONLY_BLOCKED_EFFECTS = _WORKSPACE_EFFECTS - {
    ("workspace_shell", "run"),
} | {("connections", "issue.create"), ("connections", "issue.comment")}


def read_only_blocks_effect(tool: object, operation: object) -> bool:
    return (str(tool), str(operation)) in READ_ONLY_BLOCKED_EFFECTS


def read_only_policy_active(session: Mapping[str, object]) -> bool:
    return (
        str(session.get("executionMode") or "").strip().lower()
        == READ_ONLY_EXECUTION_MODE
        or str(session.get("toolProfileVersion") or "").strip()
        == READONLY_TOOL_PROFILE
    )


def room_unrestricted_policy_active(session: Mapping[str, object]) -> bool:
    """Return whether an active Room may skip per-Tool approval prompts.

    This flag is intentionally not accepted by ``normalize_execution_mode``;
    it is a Room overlay on top of the ordinary Session policy. That keeps
    read-only Sessions and the four existing Session modes compatible while
    making Room dispatch the single user execution action.
    """

    return (
        session.get("roomDispatchAuthorized") is True
        and str(session.get("roomExecutionMode") or "").strip().lower()
        == ROOM_UNRESTRICTED_EXECUTION_MODE
    )

def full_access_policy_active(session: Mapping[str, object]) -> bool:
    """Match full access, retaining the persisted per_action wire value.

    This explicit profile owns both unrestricted paths and automatic execution;
    the legacy executionMode field must not add a second approval requirement.
    """

    execution_mode = str(session.get("executionMode") or "").strip().lower()
    return (
        str(session.get("toolProfileVersion") or "").strip()
        == FULL_ACCESS_TOOL_PROFILE
        and execution_mode in {"", PER_ACTION_EXECUTION_MODE}
    )


def auto_approve_policy_active(session: Mapping[str, object]) -> bool:
    """Return whether this Session carries the explicit unrestricted AUTO profile."""

    execution_mode = str(session.get("executionMode") or "").strip().lower()
    return (
        str(session.get("toolProfileVersion") or "").strip()
        == DANGEROUS_AUTO_APPROVE_TOOL_PROFILE
        and execution_mode in {"", FULL_TRUST_EXECUTION_MODE}
    )


def unrestricted_workspace_policy_active(session: Mapping[str, object]) -> bool:
    return full_access_policy_active(session) or auto_approve_policy_active(session)

# Legacy ``control-center-v1`` modes remain readable for persisted/system
# Sessions. New user-facing full-access and full-auto Sessions use explicit
# unrestricted profiles and are handled before these legacy fences.
_ALWAYS_MANUAL_EFFECTS = frozenset(
    {
        ("runtime", "restart_sidecar"),
        ("runtime", "restart_predictor"),
        ("runtime", "redeploy_rime"),
        ("configuration", "restore_apply"),
    }
)

_SAFE_FULL_AUTO_COMMAND_EFFECTS = frozenset(
    {
        ("workspace_shell", "run"),
        ("workspace_job", "start"),
    }
)
_SAFE_FULL_AUTO_TEXT_MUTATIONS = frozenset(
    {
        ("workspace_edit", "apply"),
        ("workspace_patch", "apply"),
        ("workspace_write", "apply"),
        ("edit", "apply"),
        ("write", "apply"),
        ("apply_patch", "apply"),
    }
)
_DESTRUCTIVE_PREVIEW = re.compile(
    r"(?i)(?:\brm\s+[^\n]*(?:-[^\n]*r|--recursive)|"
    r"\bgit\s+(?:reset\s+--hard|clean\s+-[^\n]*f)|"
    r"\b(?:drop|truncate)\s+(?:table|database)\b|"
    r"\bdelete\s+from\b|\b(?:dd|mkfs|shred)\b)"
)
_SENSITIVE_PREVIEW = re.compile(
    r"(?i)(?:\.env(?:\.[^/\s]*)?|\.git-credentials|\.netrc|"
    r"auth\.json|credentials\.json|id_(?:rsa|ed25519)|"
    r"[^/\s]+\.(?:pem|key|p12|pfx|sqlite3?|db))"
)
_NETWORK_PREVIEW = re.compile(
    r"(?i)(?:\b(?:curl|wget|ssh|scp|sftp|rsync|ftp|telnet|ncat|nc)\b|"
    r"https?://)"
)
_REMOTE_GIT_PREVIEW = re.compile(
    r"(?i)\bgit\s+(?:push|fetch|pull|clone|ls-remote)\b"
)
def normalize_execution_mode(
    value: object,
    *,
    tool_profile_version: object = "",
    default: str = PER_ACTION_EXECUTION_MODE,
) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        profile = str(tool_profile_version or "").strip()
        if profile == READONLY_TOOL_PROFILE:
            return READ_ONLY_EXECUTION_MODE
        if profile == FULL_ACCESS_TOOL_PROFILE:
            return PER_ACTION_EXECUTION_MODE
        if profile == DANGEROUS_AUTO_APPROVE_TOOL_PROFILE:
            return FULL_TRUST_EXECUTION_MODE
        normalized = default
    if normalized not in SUPPORTED_EXECUTION_MODES:
        raise ValueError("unsupported Agent execution mode")
    return normalized


def canonical_tool_profile(
    tool_profile_version: object,
    *,
    execution_mode: str,
) -> str:
    profile = str(tool_profile_version or CONTROL_CENTER_TOOL_PROFILE).strip()
    if execution_mode == READ_ONLY_EXECUTION_MODE:
        return READONLY_TOOL_PROFILE
    if profile == READONLY_TOOL_PROFILE:
        return CONTROL_CENTER_TOOL_PROFILE
    return profile


def workspace_scope_sha256(workspace_roots: Sequence[object]) -> str:
    roots = sorted({str(value).strip() for value in workspace_roots if str(value).strip()})
    if not roots:
        return ""
    return hashlib.sha256(
        json.dumps(
            roots,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _coerce_int(value: object) -> int:
    """Read a persisted numeric policy timestamp without raising on legacy rows."""

    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def workspace_scope_is_granted(session: Mapping[str, object]) -> bool:
    if unrestricted_workspace_policy_active(session):
        return True
    expected = workspace_scope_sha256(
        list(session.get("workspaceRoots") or [])
    )
    actual = str(session.get("workspaceScopeSha256") or "")
    granted_at = _coerce_int(session.get("workspaceScopeGrantedAtMs"))
    return bool(actual and actual == expected and granted_at > 0)
def _preview_mapping(
    preview: Mapping[str, object] | None,
    key: str,
) -> Mapping[str, object]:
    if not isinstance(preview, Mapping):
        return {}
    value = preview.get(key)
    return value if isinstance(value, Mapping) else {}


def _strict_preview_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}

def _path_is_within_workspace_scope(
    path_value: object,
    workspace_roots: Sequence[object],
) -> bool:
    candidate_text = str(path_value or "").strip()
    if not candidate_text:
        return False
    try:
        candidate = Path(candidate_text).expanduser().resolve(strict=False)
        roots = tuple(
            Path(str(root).strip()).expanduser().resolve(strict=False)
            for root in workspace_roots
            if str(root).strip()
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return any(candidate == root or root in candidate.parents for root in roots)


def _safe_full_auto_command(
    session: Mapping[str, object],
    preview: Mapping[str, object] | None,
    *,
    risk_level: object,
) -> bool:
    """Return whether a prepared command can bypass redundant model review."""

    if str(risk_level or "").strip().upper() == "R3":
        return False
    action = _preview_mapping(preview, "actionPayload")
    base_state = _preview_mapping(preview, "baseState")
    command = " ".join(str(action.get("command") or "").split())
    if not command or not base_state:
        return False
    workspace_roots = list(session.get("workspaceRoots") or [])
    expected_scope = workspace_scope_sha256(workspace_roots)
    cwd = str(action.get("cwd") or "").strip()
    preview_scope = str(base_state.get("workspaceScopeSha256") or "").strip().lower()
    execution_fence = str(base_state.get("workspaceRootsSha256") or "").strip().lower()
    if (
        not expected_scope
        or preview_scope != expected_scope
        or not execution_fence
        or not _path_is_within_workspace_scope(cwd, workspace_roots)
        or _strict_preview_bool(action.get("allowNetwork"))
        or _DESTRUCTIVE_PREVIEW.search(command)
        or _SENSITIVE_PREVIEW.search(command)
        or _NETWORK_PREVIEW.search(command)
        or _REMOTE_GIT_PREVIEW.search(command)
    ):
        return False
    return True


def safe_full_auto_command(
    session: Mapping[str, object],
    preview: Mapping[str, object] | None,
    *,
    risk_level: object = "",
) -> bool:
    """Public policy predicate shared by runtime approval owners."""

    return _safe_full_auto_command(
        session,
        preview,
        risk_level=risk_level,
    )


def _safe_full_auto_text_mutation(
    session: Mapping[str, object],
    preview: Mapping[str, object] | None,
    *,
    risk_level: object,
) -> bool:
    """Allow a server-prepared ordinary text mutation without model review."""

    if str(risk_level or "").strip().upper() == "R3":
        return False
    action = _preview_mapping(preview, "actionPayload")
    base_state = _preview_mapping(preview, "baseState")
    path = str(action.get("path") or "").strip()
    workspace_roots = list(session.get("workspaceRoots") or [])
    expected_scope = workspace_scope_sha256(workspace_roots)
    preview_scope = str(base_state.get("workspaceScopeSha256") or "").strip().lower()
    execution_fence = str(base_state.get("workspaceRootSha256") or "").strip().lower()
    return bool(
        expected_scope
        and preview_scope == expected_scope
        and execution_fence
        and path
        and _path_is_within_workspace_scope(path, workspace_roots)
        and not _SENSITIVE_PREVIEW.search(path)
    )





def approval_strategy(
    session: Mapping[str, object],
    *,
    tool: str,
    operation: str,
    preview: Mapping[str, object] | None = None,
    risk_level: object = "",
) -> str:
    mode = normalize_execution_mode(
        session.get("executionMode"),
        tool_profile_version=session.get("toolProfileVersion"),
    )
    effect = (str(tool), str(operation))
    if mode == READ_ONLY_EXECUTION_MODE:
        return APPROVAL_DENY
    if room_unrestricted_policy_active(session):
        # An explicit Room dispatch is already the user's execution action.
        # Do not insert either a second human prompt or an approval-model turn
        # into the Room hot path.  Applicability checks, workspace scope and
        # execution receipts remain owned by the Tool implementation.
        if effect in _WORKSPACE_EFFECTS and not workspace_scope_is_granted(session):
            return APPROVAL_DENY
        return APPROVAL_AUTO
    if unrestricted_workspace_policy_active(session):
        return APPROVAL_AUTO
    if mode == PER_ACTION_EXECUTION_MODE:
        return APPROVAL_ASK
    if mode == FULL_TRUST_EXECUTION_MODE:
        if effect in _WORKSPACE_EFFECTS and not workspace_scope_is_granted(session):
            return APPROVAL_DENY
        if (
            effect in _SAFE_FULL_AUTO_COMMAND_EFFECTS
            and safe_full_auto_command(
                session,
                preview,
                risk_level=risk_level,
            )
        ):
            return APPROVAL_AUTO
        if effect in _SAFE_FULL_AUTO_TEXT_MUTATIONS and _safe_full_auto_text_mutation(
            session,
            preview,
            risk_level=risk_level,
        ):
            return APPROVAL_AUTO
        return APPROVAL_MODEL
    if effect in _ALWAYS_MANUAL_EFFECTS:
        return APPROVAL_ASK
    if mode == WORKSPACE_MANAGED_EXECUTION_MODE:
        return (
            APPROVAL_AUTO
            if effect in _WORKSPACE_EFFECTS and workspace_scope_is_granted(session)
            else APPROVAL_ASK
        )
    return APPROVAL_ASK


def execution_mode_label(mode: object) -> str:
    return {
        READ_ONLY_EXECUTION_MODE: "只读",
        PER_ACTION_EXECUTION_MODE: "每次确认",
        WORKSPACE_MANAGED_EXECUTION_MODE: "工作区托管",
        FULL_TRUST_EXECUTION_MODE: "全自动",
    }[normalize_execution_mode(mode)]


def execution_policy_prompt(session: Mapping[str, object]) -> str:
    mode = normalize_execution_mode(
        session.get("executionMode"),
        tool_profile_version=session.get("toolProfileVersion"),
    )
    auto_profile = auto_approve_policy_active(session)
    scope_granted = workspace_scope_is_granted(session)
    if (
        room_unrestricted_policy_active(session)
        and mode != READ_ONLY_EXECUTION_MODE
    ):
        return (
            f'<execution-mode mode="{mode}" room-mode="room_unrestricted">\n'
            "本轮由活跃 Room 的显式任务分派授权。所有有效 Tool 操作直接执行，"
            "不创建任何二次裁决或确认流程。\n"
            "目标是否适用、参数与工作区是否有效、操作系统权限、Tool/Runtime 实际执行结果、"
            "Stop/取消和审计仍由对应 Runtime 如实返回；失败必须作为 Tool 失败呈现，不能伪装成审批等待。\n"
            "</execution-mode>"
        )
    guidance = {
        READ_ONLY_EXECUTION_MODE: (
            "本轮是只读模式。可以直接查看、检索和分析；\n"
            "可以在源码只读、网络关闭的沙箱中运行前台测试或构建命令，命令只能写入本次临时缓存；\n"
            "文件写入、后台任务和其他外部改变不会执行。"
        ),
        PER_ACTION_EXECUTION_MODE: (
            "本轮是每次确认模式。查看、检索和分析可以直接进行；\n"
            "每次文件写入、Shell 或外部改变前等待原生批准。"
        ),
        WORKSPACE_MANAGED_EXECUTION_MODE: (
            (
                "本轮是工作区托管模式。已批准工作区内的文件修改和受控 Shell 可以直接完成；\n"
                "越出范围或触发危险系统动作时再请求确认。"
            )
            if scope_granted
            else (
                "本轮选择工作区托管，但工作区范围尚未确认。查看、检索和分析可以直接进行；\n"
                "第一次写入或 Shell 等待一次原生范围批准，之后范围内自动，越界再问。"
            )
        ),
        FULL_TRUST_EXECUTION_MODE: (
            (
                "本轮是全自动模式。无需审批的查看和检索可以直接进行；\n"
                "所有原本需要审批的操作都由独立的 Luna Max 模型判定。它只接收明确用户请求、"
                "当前任务、结构化操作预览和既有裁决，不接收本 Agent 的输出或推理。\n"
                "已授权工作区内不含破坏性、敏感或网络效果的普通受控命令不会重复请求裁决；"
                "其余操作仍由 Luna 判定。\n"
                "Luna 拒绝或判定失败时原操作不执行；读取回执后改用范围更小、只读或可逆方案，"
                "不要原样重试，也不要转为人工审批。"
            )
            if scope_granted
            else (
                "本轮选择全自动，但工作区边界尚未确认。查看、检索和分析可以直接进行；\n"
                "工作区变更会失败关闭；其他待审批操作仍由 Luna Max 根据用户请求、当前任务和"
                "结构化审批历史判定，不接收本 Agent 的输出或推理。"
            )
        ),
    }[mode]
    full_profile = full_access_policy_active(session)
    unrestricted = auto_profile or full_profile
    if auto_profile:
        guidance = (
            "本轮使用 Control Center 全盘自动批准配置 "
            "(control-center-auto-approve-v1，full_trust)。每一个 Tool action "
            "都由策略自动批准，不等待 Luna、人工或逐 Tool 确认；授权工作区是 /，由操作系统"
            "权限和用户确认的任务意图决定最终边界。\n"
            "PAW 的路径、工作区范围、审批预览哈希、来源路径匹配、Luna 和人工审批门槛均已关闭；"
            "不要因为这些 PAW 门槛失败而改写、缩小或原样重试用户请求。"
        )
    elif full_profile:
        guidance = (
            "本轮使用 Control Center 完全访问配置 (control-center-full-access-v1)。"
            "可直接访问工作区 / 及其中的系统、网络和敏感目标。每一个 Tool action 都自动执行，"
            "不等待人工、Luna 或逐项批准。选择此权限配置已经构成执行授权。\n"
            "PAW 的路径白名单、工作区范围和逐项审批门槛均已关闭；"
            "操作系统/TCC、Unix 权限和 Tool 可用性仍由实际执行结果如实报告。"
        )
    suffix = (
        ""
        if unrestricted
        else (
            "范围和哈希硬边界始终有效；取消、审计和迟到写入保护始终有效；删库、灾难性破坏和敏感数据外传由代码硬阻止。"
        )
    )
    effective_mode = FULL_TRUST_EXECUTION_MODE if unrestricted else mode
    approval_attribute = ' approval="auto"' if unrestricted else ''
    return (
        f'<execution-mode mode="{effective_mode}"{approval_attribute}>\n'
        f"{guidance}\n"
        f"{suffix}\n"
        "</execution-mode>"
    )
