from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit

from .agent_capability_catalog import build_capability_catalog, capability_disclosure_enabled
from .agent_scenario_policy import (
    scenario_policy_applies,
    scenario_policy_for_session,
    scenario_tool_operations,
)
from .agent_governed_memory_tools import (
    AgentRoleBookToolAdapter,
    MemoryGovernanceProposalStore,
)
from .agent_execution_policy import (
    APPROVAL_AUTO,
    APPROVAL_MODEL,
    APPROVAL_DENY,
    ROOM_UNRESTRICTED_EXECUTION_MODE,
    approval_strategy,
    read_only_blocks_effect,
    read_only_policy_active,
    unrestricted_workspace_policy_active,
)
from .agent_background_jobs import AgentBackgroundJobService
from .agent_memory_sources import AgentMemorySourceStore
from .agent_role_book import AgentRoleBookStore
from .agent_tool_ids import (
    CONTROL_CENTER_TOOL_PROFILE,
    CONTROL_TOOL_IDS,
    DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
    FULL_ACCESS_TOOL_PROFILE,
    PI_PACKAGE_OWNED_CONTROL_TOOL_IDS,
    READONLY_TOOL_PROFILE,
)
from .agent_sessions import AgentSessionStore
from .agent_tool_artifacts import AgentToolArtifactProjector
from .agent_workspace import (
    PreparedWorkspaceCommand,
    WorkspaceHarness,
    WorkspaceHarnessError,
)
from .browser_control import BrowserControlService
from .contracts.json_schema import validate_contract
from .desktop_bridge import DesktopBridgeClient
from .management_service import ManagementService, page_request
from .memory_ownership import agent_visible_memory_owners
from .memory_maintenance_settings import memory_enabled_from_settings
from .settings_schema import default_settings, flatten_settings, settings_schema


_TOOL_SPECS: tuple[dict[str, object], ...] = (
    {
        "id": "overview",
        "domain": "overview",
        "displayName": "控制中心概览",
        "description": "查看 Agent、模型、记忆、输入和最近活动的整体状态",
        "when": ("用户询问当前 Agent 整体状态、能力或最近活动",),
        "notFor": ("已明确要检查某一个具体子系统",),
        "input": "可选查询与返回条数",
        "output": "Agent、模型、记忆、输入和最近活动概览",
        "does": "汇总控制中心整体状态。",
        "operations": ("status", "capabilities", "recent_activity"),
        "resultPresentation": "status",
    },
    {
        "id": "input",
        "domain": "input",
        "displayName": "输入法",
        "description": "查看输入设置、方案、候选解释，并在原生批准后调整设置或词表",
        "when": ("用户询问或要求调整输入法设置、候选或词表",),
        "notFor": ("语音 Provider、模型或普通文本生成",),
        "input": "操作名及设置变更、候选查询或审批引用",
        "output": "输入设置、候选解释、预览或带回执的变更结果",
        "does": "读取并受控调整输入法能力。",
        "operations": (
            "get_settings",
            "preview_settings",
            "apply_settings",
            "rollback_settings",
            "profile",
            "candidate_explain",
            "lexicon_review",
            "lexicon_apply",
            "lexicon_rollback",
        ),
        "operationRisks": {
            "apply_settings": "R1",
            "rollback_settings": "R1",
            "lexicon_apply": "R1",
            "lexicon_rollback": "R1",
        },
        "resultPresentation": "table",
    },
    {
        "id": "voice",
        "domain": "voice",
        "displayName": "语音输入",
        "description": "查看语音状态，并在原生批准后切换已配置的语音 Provider",
        "when": ("用户询问语音输入状态、隐私或要求切换语音 Provider",),
        "notFor": ("让 Agent 或 Room 朗读、配音或持续输出音频",),
        "input": "操作名、Provider 与可选审批引用",
        "output": "语音输入状态、隐私策略、预览或变更回执",
        "does": "读取并受控配置语音输入。",
        "operations": (
            "status",
            "privacy_policy",
            "provider_status",
            "provider_preview",
            "provider_apply",
            "provider_rollback",
        ),
        "operationRisks": {"provider_apply": "R1", "provider_rollback": "R1"},
        "resultPresentation": "status",
    },
    {
        "id": "planning",
        "domain": "planning",
        "displayName": "规划与任务",
        "description": "查看每日计划，并在原生确认后更新任务状态",
        "when": ("用户要查看每日计划或更新真实任务状态",),
        "notFor": ("维护 Agent 自己的执行清单",),
        "input": "操作名、真实 taskId、日期与动作",
        "output": "计划面板、审批预览或任务变更回执",
        "does": "读取并受控更新用户每日规划。",
        "operations": ("dashboard", "task_action", "undo_task_event"),
        "operationRisks": {"dashboard": "R0", "task_action": "R1", "undo_task_event": "R1"},
        "resultPresentation": "tool_result",
    },
    {
        "id": "agent_schedule",
        "domain": "planning",
        "displayName": "Agent 预约唤醒",
        "description": "查看预约，并在原生批准后安排自己、其他线程或角色于指定时间执行任务",
        "when": ("用户要求定时、延期或重复唤醒某个 Agent 任务",),
        "notFor": ("当前回合立即执行或仅口头提醒",),
        "input": "目标、指令、唤醒时间、时区和重复规则",
        "output": "预约、运行记录或带回执的状态变更",
        "does": "管理可取消、可审计的 Agent 预约。",
        "operations": ("list", "runs", "schedule", "pause", "resume", "cancel", "retry"),
        "operationRisks": {
            "schedule": "R2",
            "pause": "R1",
            "resume": "R2",
            "cancel": "R1",
            "retry": "R2",
        },
        "resultPresentation": "tool_result",
    },
    {
        "id": "memory",
        "domain": "memory",
        "displayName": "个人上下文记忆",
        "description": (
            "查询用户 Evidence、Atom、Book 与已批准 Timeline；"
            "memory_capture 只提交绑定当前用户原话的长期候选，Role Book 请用 agent_role_book。"
        ),
        "when": ("任务需要查找历史用户事实，或用户表达了跨 Session 有用的稳定偏好、事实、决定、纠正或原则",),
        "notFor": (
            "原始会话、助手回答、工具回执、任务进度、文件改动、测试结果、报错、临时指令或当前对话已足够",
        ),
        "input": "检索问题、范围、用户证据引用或用户长期记忆候选",
        "output": "带证据的记忆结果、候选、草案、审批或回滚状态",
        "does": "检索并治理用户长期上下文记忆。",
        "operations": (
            "catalog",
            "read",
            "recent",
            "trace",
            "capture",
            "maintenance_status",
            "curation_prepare",
            "maintenance_preview",
            "maintenance_review",
            "maintenance_apply",
            "maintenance_rollback",
            "list",
            "search",
            "get",
            "explain",
            "review",
            "remember_preview",
            "correct_preview",
            "forget_preview",
            "remember_apply",
            "correct_apply",
            "forget_apply",
            "governance_rollback",
        ),
        "operationRisks": {
            "maintenance_apply": "R1",
            "maintenance_rollback": "R1",
            "remember_apply": "R1",
            "correct_apply": "R1",
            "forget_apply": "R1",
            "governance_rollback": "R1",
        },
        "resultPresentation": "citation",
    },
    {
        "id": "agent_role_book",
        "domain": "agents",
        "displayName": "Agent 角色书",
        "description": (
            "读取角色书；仅在任务完成且有可复用变化时生成待审草案；"
            "原始聊天只审计，不直接写入；"
            "不能激活草案或修改身份、权限、安全策略与工具白名单"
        ),
        "when": ("任务需要读取固定角色版本或提议可复用的角色变化",),
        "notFor": ("用户长期记忆、权限扩张或把普通聊天写入角色书",),
        "input": "固定 revision、证据与受限角色字段更新",
        "output": "角色书、历史或待审草案",
        "does": "读取随 Session 固定版本注入系统提示词的角色书，并受控提议 revision。",
        "operations": ("get", "history", "propose_revision", "review"),
        "resultPresentation": "tool_result",
    },
    {
        "id": "knowledge",
        "domain": "knowledge",
        "displayName": "文档知识库",
        "description": "检索并受控管理授权文档",
        "when": ("检索或管理授权文档",),
        "notFor": ("Memory、未授权数据或免审批写入",),
        "input": "op 与参数",
        "output": "证据、状态或回执",
        "does": "检索文档；写入须预览审批。",
        "operations": (
            "list_bases",
            "get_base",
            "list_documents",
            "search",
            "find",
            "open",
            "status",
            "create_base",
            "configure_base",
            "import_text",
            "rebuild_preview",
            "rebuild",
        ),
        "operationRisks": {
            "create_base": "R1",
            "configure_base": "R1",
            "import_text": "R1",
            "rebuild": "R1",
        },
        "resultPresentation": "citation",
    },
    {
        "id": "models",
        "domain": "models",
        "displayName": "模型",
        "description": "查看模型与 Provider，并在原生批准后调整不含密钥的 Provider 配置",
        "when": ("用户询问模型、Provider、缓存或要求调整模型配置",),
        "notFor": ("执行普通模型对话或处理 API 密钥",),
        "input": "操作名、模型槽位、Provider、端点或审批引用",
        "output": "模型状态、探测、缓存统计、预览或变更回执",
        "does": "读取并受控配置模型与 Provider。",
        "operations": (
            "status",
            "profiles",
            "probe",
            "cache_stats",
            "profile_preview",
            "profile_apply",
            "profile_rollback",
        ),
        "operationRisks": {"profile_apply": "R1", "profile_rollback": "R1"},
        "resultPresentation": "status",
    },
    {
        "id": "runtime",
        "domain": "runtime",
        "displayName": "诊断与运行时",
        "description": "查看运行组件，并在原生批准后暂停 AI、重启 Sidecar 或预测器、重新部署 Rime",
        "when": ("用户要诊断运行组件或执行受控恢复操作",),
        "notFor": ("一般代码调试或无证据地重启服务",),
        "input": "诊断或恢复操作及可选组件参数",
        "output": "健康状态、诊断证据或带回执的恢复结果",
        "does": "诊断并受控恢复本机 Agent 运行组件。",
        "operations": (
            "health",
            "components",
            "diagnose",
            "pause_ai",
            "resume_ai",
            "restart_sidecar",
            "restart_predictor",
            "redeploy_rime",
        ),
        "operationRisks": {
            "pause_ai": "R1",
            "resume_ai": "R1",
            "restart_sidecar": "R2",
            "restart_predictor": "R2",
            "redeploy_rime": "R2",
        },
        "resultPresentation": "status",
    },
    {
        "id": "configuration",
        "domain": "configuration",
        "displayName": "历史与配置",
        "description": "查看隐私化历史与审计，并通过原生审批导出或恢复不含密钥的便携备份",
        "when": ("用户询问审计历史、导出或恢复便携配置",),
        "notFor": ("查看个人语义记忆或导出密钥",),
        "input": "查询、条数、导出或恢复动作与审批引用",
        "output": "隐私化历史、审计、备份预览或恢复回执",
        "does": "读取审计并受控迁移无密钥配置。",
        "operations": (
            "history",
            "audit",
            "export_preview",
            "export",
            "restore_preview",
            "restore_apply",
        ),
        "operationRisks": {"export": "R1", "restore_apply": "R3"},
        "resultPresentation": "table",
    },
    {
        "id": "agents",
        "domain": "agents",
        "displayName": "多 Agent 协作",
        "description": "管理有界子 Agent 委派及 Todo 关联",
        "when": ("任务需要并行研究、实现或复核",),
        "notFor": ("单 Agent 可直接完成的任务",),
        "input": "Agent、任务、当前 Todo 任务、上下文或运行 ID",
        "output": "关联 Todo 的状态、产物或取消回执",
        "does": "执行可审计、可取消的有界委派。",
        "operations": (
            "catalog",
            "delegate",
            "status",
            "call",
            "artifact",
            "abort",
        ),
        # Session subagents are a core harness primitive, not an optional
        # product capability. Their schema must be present on a fresh Session
        # so the model never pays a tool_search/tool_load round trip first.
        "alwaysAvailable": True,
        "resultPresentation": "tool_result",
    },
    {
        "id": "session_search",
        "domain": "agents",
        "displayName": "Session 历史锚点",
        "description": "搜索持久化 Session 的受控摘要并返回可导航锚点，不读取或复制原始 transcript",
        "when": (
            "任务需要定位以前在哪个 Session 或 turn 讨论过某个主题",
            "需要恢复历史上下文，但不应把整段旧对话灌入当前上下文",
        ),
        "notFor": (
            "用户偏好和长期事实应使用 memory",
            "项目文档和来源证据应使用 knowledge 或 work_documents",
            "读取原始工具输出、私密思维或机器本地 transcript 路径",
        ),
        "input": "可选查询、返回条数，以及是否包含已归档 Session",
        "output": "Session/turn 的受控摘要、证据引用与控制中心跳转锚点",
        "does": "在持久化 Session 索引中做只读检索，不创建 Todo 或过程文档。",
        "operations": ("search",),
        "alwaysAvailable": True,
        "resultPresentation": "citation",
    },
    {
        "id": "trace_diagnostics",
        "domain": "agents",
        "displayName": "Trace 诊断与改进",
        "description": "按用户冻结的目标取证，检索持久经验，登记真实候选版本，并通过 Lab 运行验证；最终采用由 Trace App 用户操作",
        "when": (
            "任务需要诊断一个或多个 Agent 运行，比较失败、上下文、协作、Memory/RAG 或效率证据",
            "Trace Agent Skill 需要读取原对话，而不是只依赖 Session 搜索摘要",
        ),
        "notFor": (
            "直接安装候选、替换当前版本或由模型声明验证通过",
            "读取私密思维、原始 Provider context、密钥、完整 Tool 参数或机器 transcript 路径",
        ),
        "input": "inspect: 1 到 12 个诊断对象；read/command: 当前诊断 reportId，以及有界检索参数或 command、clientRequestId、input",
        "output": "冻结证据、持久经验、实际源码候选、Lab 工作与主机计算的比较结果；缺失验证明确标为待验证",
        "does": "共用 Runtime 证据、Lab 执行和版本资源 owner；模型不提供评分回执，最终安装在 App 触发。",
        "operations": ("inspect", "read", "command"),
        "alwaysAvailable": True,
        "resultPresentation": "citation",
    },
    {
        "id": "room_partner",
        "domain": "agents",
        "displayName": "Room 伙伴协作",
        "description": (
            "在 Room 中查看伙伴、异步委派有界工作、显式收集或等待结果、验收或退回 WorkItem、"
            "直接通信，发布公开回执，或在当前任务中按需增删 Room participant。新增 participant 会创建"
            "独立 Session；移除 participant 会保留历史，并把其未完成 WorkItem 交回 Room 待重新分配。"
            "恢复规则：Facilitator 看到 active 且带 reviewFeedback 的退回项时，"
            "list 会在 recoverableWorkItems 给出原 workItemId、expectedRevision 和 retry 动作；"
            "先按该动作调用 retry，不要 delegate 新复核项，也不要直接 accept。"
        ),
        "when": (
            "当前 Session 是 Room 的任一正式伙伴，需要直接与另一位伙伴通信或独立处理有界子任务",
            "同一阶段存在多个无依赖、不重叠的工作轨道，需要按任务规模动态选择并真实并行启动；"
            "一次最多 7 个可见 Partner，Room 总参与者最多 8 个",
        ),
        "notFor": (
            "普通 Session 的临时微型子 Agent，或主伙伴自己即可完成的单步工作",
            "存在前后依赖、写入范围重叠，或需要上一阶段交付才能开始的任务",
        ),
        "input": "委派合同；participant roleId/participantId；childDispatchId 或 workItemId；Facilitator 的两轴判定与证据；或直接通信/公开回执参数",
        "output": "立即委派回执、显式收集/等待结果、WorkItem 审核回执、直接 Intercom 回执或类型明确的公开回执",
        "does": "委派立即返回持久回执；Partner 完成由持久 wake 通知，collect/wait 只读取停止点且 wait 超时不取消；Facilitator 依据证据显式 accept 或 return。",
        "operations": (
            "list",
            "add_participant",
            "remove_participant",
            "delegate",
            "delegate_batch",
            "retry",
            "accept",
            "return",
            "collect",
            "wait",
            "post",
            "peer_list",
            "peer_send",
            "peer_ask",
            "peer_reply",
        ),
        # Availability is still Room-bound below. Once available, this is the
        # only formal Partner primitive and must be callable directly.
        "alwaysAvailable": True,
        "resultPresentation": "tool_result",
    },
    {
        "id": "browser",
        "domain": "browser",
        "displayName": "PAW Browser",
        "description": "读取 PAW 内置 Chromium，并通过开放 ego-browser 控制内核执行可追踪的网页任务",
        "when": ("任务需要读取或操作 PAW Browser 的真实页面",),
        "notFor": (
            "已有 API 或连接器，或只需一般网页知识",
            "操作用户日常 Chrome/Edge，或通过 desktop_semantic 打开第二个浏览器",
        ),
        "input": "ego-browser JavaScript、标签页、快照 ref、URL、文本或滚动参数",
        "output": "页面快照、截图、轨迹或带回执的操作结果",
        "does": "在同一 PAW Chromium 和 Task Space 上观察并受控操作网页。",
        "operations": (
            "status",
            "tabs",
            "snapshot",
            "screenshot",
            "trace",
            "run",
            "navigate",
            "back",
            "forward",
            "click",
            "type",
            "scroll",
            "wait",
            "stop",
        ),
        # This profile belongs to the Agent rather than the user's daily
        # browser, so browser operations execute directly without approvals.
        "operationRisks": {},
        "resultPresentation": "tool_result",
    },
    {
        "id": "todo",
        "modelVisible": False,
        "domain": "planning",
        "displayName": "Todo",
        "description": "维护当前 Session 的分阶段执行清单；状态变更立即同步到任务中心，不需要用户批准",
        "when": (
            "任务包含至少三个清晰动作、用户给出多项要求，或工作需要跨回合、跨阶段验证",
        ),
        "notFor": ("简单问答、单步操作、修改用户每日计划或替代长期 Goal",),
        "input": "init、start、done、drop、block、unblock、append、view 或 rm",
        "output": "当前 Todo 的阶段、任务状态、计数与本次完成项",
        "does": (
            "按 OMP Todo 状态机维护 Session 清单；每次调用原子更新并发布最新投影，"
            "Agent 可自行创建、推进、完成、阻塞、解除阻塞、放弃或清空。"
        ),
        "operations": (
            "init",
            "start",
            "done",
            "drop",
            "block",
            "unblock",
            "append",
            "view",
            "rm",
        ),
        "resultPresentation": "tool_result",
    },
    {
        "id": "agent_goal",
        "modelVisible": False,
        "domain": "planning",
        "displayName": "长期目标",
        "description": "在用户明确确认后配置并维护当前 Session 的长期 Goal、验收标准、预算与完成证据",
        "when": ("用户明确要求建立、更新、暂停、恢复、完成或取消当前 Session 的长期 Goal",),
        "notFor": (
            "用 Goal 代替普通执行清单",
            "用户尚未确认目标内容时擅自配置，或删除既有 Goal 审计记录",
            "把仍在进行的 Room Goal 暂停来等待用户、界面或后续消息",
        ),
        "input": "Goal 生命周期动作、目标、验收标准、证据预期、可选预算及完成证据",
        "output": "权威 Goal 状态、预算、完成或取消审计与工作流投影",
        "does": "调用 Session Goal 的唯一状态所有者；配置前要求显式确认，完成时要求可核验的证据。",
        "operations": (
            "list",
            "confirm_setup",
            "update",
            "pause",
            "resume",
            "complete",
            "cancel",
        ),
        "resultPresentation": "tool_result",
    },
    {
        "id": "plugins",
        "domain": "agents",
        "displayName": "能力市场与制作",
        "description": "在 OS 内查找、制作、安装与卸载原生 Pi Package；已授权的更改通过当前 Session 的执行权限直接应用同一份预览",
        "when": ("当前任务缺少可复用的 Skill、扩展、提示词或主题，需要先查找再获取或制作",),
        "notFor": ("已有工具或 Skill 可以完成的普通任务、写入密钥、未获授权的装卸或绕过来源与版本校验",),
        "input": "市场查询、Pi Package 来源或不可变 Package 草稿",
        "output": "市场结果、Package 草稿、校验结果或待确认安装提议",
        "does": "复用 Pi 的 Package 解析和加载能力；PAW 只负责发现、草稿、回执、版本和产品确认。",
        "operations": (
            "catalog",
            "list",
            "create_package",
            "validate",
            "propose_install",
            "propose_enable",
            "propose_disable",
            "propose_update",
            "propose_rollback",
            "propose_uninstall",
            "apply",
        ),
        "operationRisks": {"apply": "R2"},
        "resultPresentation": "tool_result",
    },
    {
        "id": "sandbox",
        "modelVisible": False,
        "runtimeProjected": False,
        "domain": "agents",
        "displayName": "垂直 Agent 沙箱",
        "description": "通过已安装的 Connector 在 PAW Host 中运行受控垂直 Agent，并返回 SandboxRun、Trace 与 EvalRun 链路",
        "when": ("用户要求运行已注册的垂直 Agent 自测，或检查它的沙箱、Trace 与 Eval 结果",),
        "notFor": (
            "运行任意主机命令或未注册代码",
            "让插件直接写 TraceStore、EvalRunStore 或生产 Memory/Knowledge",
        ),
        "input": "操作名，以及已注册的垂直 Agent suiteId 与可选精确版本",
        "output": "Host 管理的沙箱状态，或 SandboxRun、Trace 与 EvalRun 身份及指标",
        "does": "把插件入口路由到 PAW Host 的受控执行与测评链；插件本身不是 OS 安全边界。",
        "operations": ("status", "run"),
        "resultPresentation": "tool_result",
    },
    {
        "id": "work_documents",
        "modelVisible": False,
        "domain": "planning",
        "displayName": "工作文档",
        "description": "固定读取与管理当前权威 Todo、Goal 或 Room WorkItem 绑定的活动及归档工作文档",
        "when": ("本地控制面需要列出、检查、修复、重开或擦除权威工作文档",),
        "notFor": ("扫描工作区猜测文档身份，或绕过终态回执与擦除审批",),
        "input": "规范 work_documents 操作及其固定参数",
        "output": "经 JSON 契约验证的列表、详情或命令回执",
        "does": "调用 WorkDocumentService 固定适配器；不创建第二个 Todo、Goal 或 Room 状态所有者。",
        "operations": (
            "list",
            "history.search",
            "get",
            "register",
            "archive",
            "repair",
            "reopen",
            "erase.preview",
            "erase",
        ),
        "operationRisks": {
            "register": "R2",
            "archive": "R2",
            "repair": "R2",
            "reopen": "R2",
            "erase": "R3",
        },
        "resultPresentation": "tool_result",
    },
    {
        "id": "lab_project",
        "domain": "agents",
        "displayName": "Lab 项目成果",
        "description": "读取当前 Lab 项目并创建带版本的项目成果、动态视图及可选执行绑定",
        "when": ("当前 Session 是 Lab 项目 Agent，需要基于 Skill 模板组织项目特有成果与交互",),
        "notFor": ("访问其他项目、直接修改业务环境或把成果正文当作执行成功",),
        "input": "读取成果身份，或带 expectedRevision 和 clientRequestId 的项目命令",
        "output": "真实项目、成果版本、材料快照、应用版本与实际调用回执",
        "does": "与前端共用项目命令；执行操作只调用当前项目的真实绑定与原有执行所有者。",
        "operations": ("read", "command", "execution_read", "execution_command", "knowledge_read", "knowledge_command", "app_command"),
        "resultPresentation": "tool_result",
    },
    {
        "id": "lab_research", "domain": "knowledge", "displayName": "研究原文",
        "description": "在当前应用冻结的知识库中查找文档、文内定位和分页读取，保留引用及实际预算",
        "when": ("当前 App 的研究任务需要检查原文、方法、结果或限制",),
        "notFor": ("访问任意其他知识库、重新索引或修改外部数据",),
        "input": "discover/find/open/search；只使用返回的文档和引用标识",
        "output": "带来源和页码的原文窗口、后续游标与本次真实工具预算",
        "does": "读取本次 App 版本冻结的来源，重复工具回执不重新消耗预算。",
        "operations": ("discover", "find", "open", "search"), "resultPresentation": "tool_result",
    },
    {
        "id": "desktop_semantic",
        "domain": "desktop",
        "displayName": "桌面语义操作",
        "description": "通过 macOS Accessibility 读取目标窗口语义树和差分，并在原生批准后按语义节点操作；不截屏、不做 OCR",
        "when": ("任务必须读取或操作本机 Mac 应用的可访问性语义树",),
        "notFor": (
            "浏览器有专用工具、需要截图 OCR 或存在直接 API",
            "PAW Browser / ego-browser 任务，或打开独立 Chrome/Edge 作为网页验收",
        ),
        "input": "应用或窗口目标、语义节点与动作",
        "output": "可访问性树、差分、状态或操作回执",
        "does": "通过可访问性语义读取并受控操作桌面应用。",
        "operations": ("status", "list", "inspect", "find", "act"),
        "operationRisks": {"act": "R2"},
        "resultPresentation": "tool_result",
    },
    {
        "id": "workspace_list",
        "modelVisible": False,
        "domain": "workspace",
        "displayName": "工作区浏览",
        "description": "浏览当前运行协调 Session 明确授权的工作区",
        "when": ("协调 Session 需要查看授权工作区目录结构",),
        "notFor": ("读取文件内容、搜索文本或访问未授权路径",),
        "input": "相对路径、深度与条数上限",
        "output": "有界目录和文件条目",
        "does": "列出授权工作区结构。",
        "operations": ("list",),
        "sessionModes": ("coordinator",),
        "resultPresentation": "table",
    },
    {
        "id": "workspace_lsp",
        "domain": "workspace",
        "displayName": "工作区语言服务",
        "description": "在授权工作区内以受限语言服务器读取语义信息，并通过哈希绑定批准应用重命名或纯编辑代码操作",
        "when": ("协调 Session 需要符号、悬停、定义、引用、诊断或语言服务生成的受控编辑",),
        "notFor": (
            "未授权、敏感或符号链接路径",
            "执行语言服务器命令或创建、重命名、删除资源",
            "绕过原生批准直接写入文件",
        ),
        "input": "操作名、授权根或源文件、位置、服务器选择与有界超时",
        "output": "有界语义结果、服务状态，或含引用证据的哈希绑定多文件修改预览和回执",
        "does": "在 WorkspaceHarness 安全边界内读取语言服务结果并受控应用纯文本编辑。",
        "operations": (
            "status",
            "symbols",
            "hover",
            "definition",
            "references",
            "diagnostics",
            "rename",
            "code_action_apply",
        ),
        "operationRisks": {"rename": "R2", "code_action_apply": "R2"},
        "sessionModes": ("coordinator",),
        "resultPresentation": "tool_result",
    },
    {
        "id": "workspace_read",
        "modelVisible": False,
        "domain": "workspace",
        "displayName": "工作区读取",
        "description": "读取授权 UTF-8 文件或受管资源",
        "when": ("协调 Session 需要读取已知文件或内部资源",),
        "notFor": ("二进制内容、敏感文件、搜索或未授权资源",),
        "input": "路径或资源 URI、选择器与续读偏移",
        "output": "有界内容、选择片段、来源修订与续读偏移",
        "does": "读取授权文件或受管资源。",
        "operations": ("read",),
        "sessionModes": ("coordinator",),
        "resultPresentation": "tool_result",
    },
    {
        "id": "workspace_search",
        "modelVisible": False,
        "domain": "workspace",
        "displayName": "工作区搜索",
        "description": "在授权工作区内有界搜索非敏感文件名与 UTF-8 文本内容",
        "when": ("协调 Session 需要定位文件、符号或文本位置",),
        "notFor": ("已知文件直接读取、互联网搜索或未授权路径",),
        "input": "查询、相对路径、模式、大小写与条数上限",
        "output": "带路径和位置的有界匹配结果",
        "does": "搜索授权工作区文件与文本。",
        "operations": ("search",),
        "sessionModes": ("coordinator",),
        "resultPresentation": "table",
    },
    {
        "id": "workspace_patch",
        "modelVisible": False,
        "domain": "workspace",
        "displayName": "精确文件修改",
        "description": "预览精确文本替换，并在原生批准和文件哈希复验后原子写入",
        "when": ("协调 Session 需要对授权文本做精确可复验修改",),
        "notFor": ("模糊重写、二进制编辑、未授权路径或无需修改",),
        "input": "路径、旧文本、新文本与预期匹配次数",
        "output": "修改预览、审批状态和原子写入回执",
        "does": "受控执行精确文本替换。",
        "operations": ("apply",),
        "operationRisks": {"apply": "R2"},
        "sessionModes": ("coordinator",),
        "resultPresentation": "tool_result",
    },
    {
        "id": "workspace_edit",
        "modelVisible": False,
        "domain": "workspace",
        "displayName": "文件编辑",
        "description": "按最新读取快照执行精确替换；过期快照不创建审批",
        "when": ("已读取授权文本且需局部修改",),
        "notFor": ("未读取、新建、重写、二进制或未授权路径",),
        "input": "路径、resourceRevision 与 oldText/newText 数组",
        "output": "差异、审批、原子写入回执与诊断",
        "does": "快照绑定地修改文件。",
        "operations": ("apply",),
        "operationRisks": {"apply": "R2"},
        "sessionModes": ("coordinator",),
        "resultPresentation": "tool_result",
    },
    {
        "id": "workspace_write",
        "modelVisible": False,
        "domain": "workspace",
        "displayName": "文件写入",
        "description": "快照绑定地新建或覆盖 UTF-8 文件",
        "when": ("新建文件，或已读取现有文件并需重写",),
        "notFor": ("局部修改、未读取覆盖、二进制、敏感或未授权路径",),
        "input": "路径、内容及 resourceRevision 或 missing",
        "output": "差异、审批、原子写入回执与诊断",
        "does": "快照绑定地写文件。",
        "operations": ("apply",),
        "operationRisks": {"apply": "R2"},
        "sessionModes": ("coordinator",),
        "resultPresentation": "tool_result",
    },
    {
        "id": "workspace_job",
        "domain": "workspace",
        "displayName": "后台任务",
        "description": "在授权工作区的 macOS 沙箱中启动、查看日志并停止受管后台任务",
        "when": ("协调 Session 需要运行开发服务器、长构建或其他长生命周期命令",),
        "notFor": (
            "普通短命令；短命令继续使用 workspace_shell",
            "交互式 REPL 或任意 stdin 写入",
            "跨 Session 或跨授权工作区管理进程",
        ),
        "input": "操作名，以及启动参数或后台任务 ID",
        "output": "持久任务快照、增量日志或停止回执",
        "does": "管理会话级、可审计、可取消的后台工作区任务。",
        "operations": ("start", "list", "status", "logs", "cancel"),
        "operationRisks": {"start": "R2", "cancel": "R1"},
        "sessionModes": ("coordinator",),
        "resultPresentation": "terminal",
    },
    {
        "id": "workspace_shell",
        "modelVisible": False,
        "domain": "workspace",
        "displayName": "受控命令",
        "description": (
            "在授权工作区的 macOS 沙箱中运行有界命令；只读复核命令不授予源码写权限"
        ),
        "when": ("协调 Session 必须运行构建、测试或诊断命令",),
        "notFor": (
            "已知文本可由 workspace_read 或 workspace_patch 完成",
            "用 apply_patch、heredoc 等 Shell 包装修改文件",
            "用 sleep 或轮询等待其他 Room 成员",
            "命令越过授权边界",
        ),
        "input": "命令、工作目录、超时和网络开关",
        "output": "退出状态、标准输出、错误输出和执行回执",
        "does": (
            "在授权工作区受控运行命令；只读复核时源码强制只读，"
            "仅本次临时缓存可写。"
        ),
        "operations": ("run",),
        "operationRisks": {"run": "R2"},
        "sessionModes": ("coordinator",),
        "resultPresentation": "terminal",
    },
)
_TOOL_SPEC_BY_ID = {str(item["id"]): item for item in _TOOL_SPECS}
_WORKSPACE_TOOLS = frozenset(
    {
        "workspace_list",
        "workspace_read",
        "workspace_search",
        "workspace_lsp",
        "workspace_edit",
        "workspace_patch",
        "workspace_write",
        "workspace_shell",
        "workspace_job",
    }
)
_READ_ONLY_SUBAGENT_WORKSPACE_TOOLS = frozenset(
    {
        "workspace_list",
        "workspace_read",
        "workspace_search",
        "workspace_lsp",
    }
)


def _tool_mode_compatible(
    session: Mapping[str, object],
    spec: Mapping[str, object],
) -> bool:
    mode = str(session.get("mode") or "assistant")
    session_modes = tuple(spec.get("sessionModes") or ("assistant", "coordinator"))
    if mode in session_modes:
        return True
    # Delegated read-only Sessions intentionally stay in assistant mode. Their
    # internal Session kind, profile and explicit allowlist remain separate
    # gates for these native read projections.
    return (
        str(session.get("sessionKind") or "") == "subagent_runtime"
        and mode == "assistant"
        and str(session.get("toolProfileVersion") or "")
        == "subagent-readonly-v1"
        and str(spec.get("id") or "") in _READ_ONLY_SUBAGENT_WORKSPACE_TOOLS
    )


if (
    len(_TOOL_SPEC_BY_ID) != len(_TOOL_SPECS)
    or tuple(_TOOL_SPEC_BY_ID) != CONTROL_TOOL_IDS
):
    raise RuntimeError(
        "Agent Tool inventory or order differs between agent_tools and agent_tool_ids"
    )

_KNOWLEDGE_CHUNKING_PARAMETER_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "strategy": {
            "type": "string",
            "enum": ["general", "markdown", "book", "qa", "laws", "separator", "fixed"],
        },
        "size": {"type": "integer", "minimum": 200, "maximum": 8_000},
        "overlap": {"type": "integer", "minimum": 0, "maximum": 2_000},
        "separator": {"type": "string", "maxLength": 100},
        "respectHeadings": {"type": "boolean"},
        "respectPageBoundaries": {"type": "boolean"},
    },
}
_KNOWLEDGE_RETRIEVAL_PARAMETER_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "mode": {"type": "string", "enum": ["hybrid", "lexical", "dense"]},
        "topK": {"type": "integer", "minimum": 1, "maximum": 100},
        "threshold": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "lexicalWeight": {"type": "number", "minimum": 0.0, "maximum": 10.0},
        "denseWeight": {"type": "number", "minimum": 0.0, "maximum": 10.0},
        "graphEnabled": {"type": "boolean"},
        "graphWeight": {"type": "number", "minimum": 0.0, "maximum": 10.0},
        "rrfK": {"type": "integer", "minimum": 1, "maximum": 1_000},
        "candidateMultiplier": {"type": "integer", "minimum": 1, "maximum": 20},
        "rerankEnabled": {"type": "boolean"},
        "rerankCandidateDepth": {"type": "integer", "minimum": 1, "maximum": 100},
    },
}

_RUNTIME_TOOL_PARAMETER_SCHEMAS: dict[str, dict[str, object]] = {
    "lab_research": {
        "type": "object", "additionalProperties": False, "required": ["op"],
        "properties": {
            "op": {"type": "string", "enum": ["discover", "find", "open", "search"]},
            "query": {"type": "string", "maxLength": 2000, "description": "discover/search 必填。discover 使用单篇文献的短标题关键词；search 使用一个具体证据问题。"},
            "sourceId": {"type": "string"}, "documentId": {"type": "string"}, "chunkId": {"type": "string"},
            "patterns": {"type": "array", "minItems": 1, "maxItems": 10, "description": "仅 find：1–10 个原文中的字面短语，不是正则表达式。", "items": {"type": "string", "minLength": 1, "maxLength": 240}},
            "page": {"type": "integer", "minimum": 1}, "offset": {"type": "integer", "minimum": 0},
            "charOffset": {"type": "integer", "minimum": 0}, "before": {"type": "integer", "minimum": 0},
            "after": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "description": "仅 discover/find/open；search 不接受此字段。discover/find 最大20，open 最大24。"},
            "maxChars": {"type": "integer", "minimum": 1, "description": "仅 open；不得超过本次 App 返回的 maxReadChars。"},
        },
    },
    "trace_diagnostics": {
        "type": "object", "additionalProperties": False, "required": ["op"],
        "properties": {
            "op": {"type": "string", "enum": ["inspect", "read", "command"]},
            "targets": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
                "type": "object", "additionalProperties": False, "required": ["kind", "id", "title"],
                "properties": {"kind": {"type": "string", "enum": ["session", "room", "run"]},
                    "id": {"type": "string"}, "title": {"type": "string"},
                    "traceIds": {"type": "array", "items": {"type": "string"}}}}},
            "reportId": {"type": "string", "minLength": 1},
            "query": {"type": "string", "maxLength": 1000},
            "patternId": {"type": "string"}, "revision": {"type": "integer", "minimum": 1},
            "offset": {"type": "integer", "minimum": 0},
            "command": {"type": "string", "enum": ["register_version", "register_plan", "prepare_candidate", "propose", "save_pattern", "prepare_distillation", "save_distillation", "run_candidate", "cancel_candidate", "compare"]},
            "clientRequestId": {"type": "string", "minLength": 1, "maxLength": 240},
            "input": {"type": "object"}, "candidateId": {"type": "string"},
        },
        "oneOf": [
            {"properties": {"op": {"const": "inspect"}}, "required": ["op", "targets"]},
            {"properties": {"op": {"const": "read"}}, "required": ["op", "reportId"]},
            {"properties": {"op": {"const": "command"}}, "required": ["op", "reportId", "command", "clientRequestId"]},
        ],
    },
    "lab_project": {
        "type": "object",
        "oneOf": [
            {"type": "object", "additionalProperties": False,
             "required": ["op", "appId", "action", "expectedRevision", "clientRequestId", "input"],
             "properties": {"op": {"const": "app_command"}, "appId": {"type": "string", "minLength": 1},
                 "action": {"type": "string", "enum": ["invoke", "cancel", "resume"]},
                 "expectedRevision": {"type": "integer", "minimum": 1},
                 "clientRequestId": {"type": "string", "minLength": 1, "maxLength": 240}, "input": {"type": "object"}}},
            {"type": "object", "additionalProperties": False, "required": ["op"],
             "properties": {"op": {"const": "read"}, "artifactId": {"type": "string"},
                            "artifactRevision": {"type": "integer", "minimum": 1}, "materialSetId": {"type": "string"},
                            "appId": {"type": "string", "description": "读取本项目应用版本与调用摘要；与 artifactId/materialSetId 分开读取。"},
                            "appVersion": {"type": "integer", "minimum": 1},
                            "appCallId": {"type": "string", "description": "按需读取这次实际调用的完整输入、输出、用量及回执。"}}},
            {"type": "object", "additionalProperties": False,
             "required": ["op", "action", "expectedRevision", "clientRequestId", "input"],
             "properties": {"op": {"const": "command"},
                            "action": {"type": "string", "enum": ["update_brief", "import_materials", "remove_materials", "publish_artifact", "set_workspace", "bind_execution", "prepare_app"]},
                            "expectedRevision": {"type": "integer", "minimum": 1},
                            "clientRequestId": {"type": "string", "minLength": 1, "maxLength": 240},
                            "input": {"type": "object", "description": "update_brief: title/description; import_materials: path or materials; remove_materials: sourceIds; publish_artifact: title,kind,view,content, optional artifactId+expectedArtifactRevision,summary,actions; set_workspace: artifactOrder,primaryArtifactId,layout; bind_execution: adapterId,input, optional artifactId+artifactRevision; prepare_app: directory relative to executionWorkspace.path, optional appId to create a new immutable version. Read the current project's commandGuide before a new operation."}}},
            {"type": "object", "additionalProperties": False, "required": ["op"],
             "properties": {"op": {"const": "knowledge_read"}, "jobId": {"type": "string", "minLength": 1}}},
            {"type": "object", "additionalProperties": False, "required": ["op", "expectedRevision", "clientRequestId", "input"],
             "properties": {"op": {"const": "knowledge_command"},
                            "expectedRevision": {"type": "integer", "minimum": 1},
                            "clientRequestId": {"type": "string", "minLength": 1, "maxLength": 240},
                            "input": {"type": "object", "description": "Read commandGuide.knowledge; use existing project corpus/index/dataset IDs. File paths must be inside the Session workspace."}}},
            {"type": "object", "additionalProperties": False, "required": ["op", "bindingId"],
             "properties": {"op": {"const": "execution_read"}, "bindingId": {"type": "string", "minLength": 1}}},
            {"type": "object", "additionalProperties": False, "required": ["op", "bindingId", "action", "expectedRevision", "clientRequestId", "input"],
             "properties": {"op": {"const": "execution_command"}, "bindingId": {"type": "string", "minLength": 1},
                            "action": {"type": "string", "enum": ["draft", "review", "review_case", "label_sample", "judge_config", "calibrate", "freeze", "experiment", "cancel", "resume"]},
                            "expectedRevision": {"type": "integer", "minimum": 1}, "clientRequestId": {"type": "string", "minLength": 1, "maxLength": 240}, "input": {"type": "object"}}},
        ],
    },
    "room_partner": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "op": {
                "type": "string",
                "description": (
                    "list 返回 recoverableWorkItems，其中包含退回后仍为 active 且带 "
                    "reviewFeedback 的 WorkItem 及其 expectedRevision；按其建议使用 retry；"
                    "retry 沿同一修订链重新派发当前负责人。"
                ),
                "enum": [
                    "list",
                    "add_participant",
                    "remove_participant",
                    "delegate",
                    "delegate_batch",
                    "retry",
                    "accept",
                    "return",
                    "collect",
                    "wait",
                    "post",
                    "peer_list",
                    "peer_send",
                    "peer_ask",
                    "peer_reply",
                ],
            },
            "targetParticipantId": {
                "type": "string",
                "minLength": 1,
                "maxLength": 240,
                "description": (
                    "delegate 时必须原样使用 list 返回的 participantId；retry 可省略，"
                    "省略时继续使用 WorkItem 当前负责人。"
                ),
            },
            "roleId": {
                "type": "string",
                "minLength": 1,
                "maxLength": 320,
                "description": "add_participant 使用的活动 Persona roleId；每个 Room participant role 不能重复。",
            },
            "roleVersion": {
                "type": "string",
                "minLength": 1,
                "maxLength": 40,
                "description": "add_participant 可选的 Persona 版本；省略时使用 1。",
            },
            "collaborationRole": {
                "type": "string",
                "enum": ["coordinator", "researcher", "implementer", "reviewer"],
                "description": "add_participant 的 Room 职责；新增成员不能担任 Facilitator。",
            },
            "participantId": {
                "type": "string",
                "minLength": 1,
                "maxLength": 320,
                "description": "remove_participant 必须原样使用 list 返回的 participantId。",
            },
            "task": {
                "type": "string",
                "minLength": 1,
                "maxLength": 8_000,
            },
            "expectedOutput": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1_200,
            },
            "acceptanceCriteria": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 320,
                },
            },
            "timeoutSeconds": {
                "type": "integer",
                "minimum": 5,
                "maximum": 300,
                "description": "wait 的有界等待时长；超时只结束本次等待，不取消 Partner。",
            },
            "childDispatchId": {
                "type": "string",
                "minLength": 1,
                "maxLength": 240,
                "description": "delegate 回执中的持久 childDispatchId。",
            },
            "workItemId": {
                "type": "string",
                "minLength": 1,
                "maxLength": 240,
                "description": (
                    "delegate 回执或 collect/wait 结果中的 WorkItem ID；"
                    "return、blocked 或 failed 后 retry 时携带原 ID。"
                ),
            },
            "expectedRevision": {
                "type": "integer",
                "minimum": 0,
                "maximum": 2,
                "description": "Facilitator 已检查的 WorkItem revision；用于拒绝陈旧审核或重试。",
            },
            "operabilityVerdict": {
                "type": "string",
                "enum": ["passed", "failed", "unverified"],
                "description": "真实路径是否运行的独立判定。",
            },
            "requirementVerdict": {
                "type": "string",
                "enum": ["satisfied", "not_satisfied", "unverified"],
                "description": "观察结果是否满足精确需求的独立判定。",
            },
            "evidenceRefs": {
                "type": "array",
                "minItems": 1,
                "maxItems": 24,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1_000,
                },
                "description": "支撑两个判定的持久证据引用。",
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 2_000,
                "description": (
                    "accept 时写明已核对的证据；return 或 retry 时给负责 Partner 的具体原因。"
                ),
            },
            "supersededByWorkId": {
                "type": "string",
                "minLength": 1,
                "maxLength": 240,
                "description": (
                    "当 Partner 已提出 failed/unverified/not_satisfied 时，"
                    "accept 必须指向该 WorkItem 的直接子复核项；复核项还必须在之后"
                    "提交并提出 passed/satisfied。"
                ),
            },
            "proposedOperabilityVerdict": {
                "type": "string",
                "enum": ["passed", "failed", "unverified"],
                "description": (
                    "post kind=work_result 必填：Partner 对真实路径是否运行的诚实提交判定。"
                ),
            },
            "proposedRequirementVerdict": {
                "type": "string",
                "enum": ["satisfied", "not_satisfied", "unverified"],
                "description": (
                    "post kind=work_result 必填：Partner 对需求是否满足的诚实提交判定。"
                ),
            },
            "phase": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "description": "同一并行波次的可读阶段名称。",
            },
            "tasks": {
                "type": "array",
                "minItems": 2,
                "maxItems": 7,
                "description": (
                    "必须互不依赖且目标伙伴不重复的同阶段任务；只按实际可并行工作"
                    "数量派发，不为凑满 Room 人数创建任务。"
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "targetParticipantId",
                        "task",
                        "expectedOutput",
                        "acceptanceCriteria",
                    ],
                    "properties": {
                        "targetParticipantId": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 240,
                            "description": "必须原样使用 list 返回的 participantId。",
                        },
                        "task": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 8_000,
                        },
                        "expectedOutput": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1_200,
                        },
                        "acceptanceCriteria": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 8,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 320,
                            },
                        },
                    },
                },
            },
            "content": {
                "type": "string",
                "minLength": 1,
                "maxLength": 8_000,
            },
            "kind": {
                "type": "string",
                "enum": [
                    "progress",
                    "result",
                    "work_result",
                    "review_result",
                    "handoff",
                    "wait",
                    "blocked",
                ],
                "description": "公开内容类型；省略时为 progress，最终答复使用 result。",
            },
            "clientMessageId": {
                "type": "string",
                "maxLength": 200,
                "description": "可选幂等键；省略时使用当前 Tool 调用 ID。",
            },
            "replyTo": {
                "type": "string",
                "minLength": 1,
                "maxLength": 240,
                "description": "peer_reply 必须使用 peer_ask 回执中的 message.id。",
            },
        },
        "oneOf": [
            {
                "required": ["op"],
                "properties": {"op": {"const": "list"}},
            },
            {
                "required": ["op", "roleId"],
                "properties": {"op": {"const": "add_participant"}},
            },
            {
                "required": ["op", "participantId"],
                "properties": {"op": {"const": "remove_participant"}},
            },
            {
                "required": [
                    "op",
                    "targetParticipantId",
                    "task",
                    "expectedOutput",
                    "acceptanceCriteria",
                ],
                "properties": {"op": {"const": "delegate"}},
            },
            {
                "required": ["op", "phase", "tasks"],
                "properties": {"op": {"const": "delegate_batch"}},
            },
            {
                "required": [
                    "op",
                    "workItemId",
                    "expectedRevision",
                    "reason",
                ],
                "properties": {"op": {"const": "retry"}},
            },
            {
                "required": [
                    "op",
                    "workItemId",
                    "expectedRevision",
                    "operabilityVerdict",
                    "requirementVerdict",
                    "evidenceRefs",
                    "reason",
                ],
                "properties": {
                    "op": {"const": "accept"},
                    "operabilityVerdict": {"const": "passed"},
                    "requirementVerdict": {"const": "satisfied"},
                },
            },
            {
                "required": [
                    "op",
                    "workItemId",
                    "expectedRevision",
                    "operabilityVerdict",
                    "requirementVerdict",
                    "evidenceRefs",
                    "reason",
                ],
                "properties": {"op": {"const": "return"}},
                "not": {
                    "properties": {
                        "operabilityVerdict": {"const": "passed"},
                        "requirementVerdict": {"const": "satisfied"},
                    },
                    "required": ["operabilityVerdict", "requirementVerdict"],
                },
            },
            {
                "required": ["op"],
                "properties": {"op": {"const": "collect"}},
                "oneOf": [
                    {"required": ["childDispatchId"]},
                    {"required": ["workItemId"]},
                ],
            },
            {
                "required": ["op", "timeoutSeconds"],
                "properties": {"op": {"const": "wait"}},
                "oneOf": [
                    {"required": ["childDispatchId"]},
                    {"required": ["workItemId"]},
                ],
            },
            {
                "required": ["op", "content"],
                "properties": {"op": {"const": "post"}},
                "if": {
                    "required": ["kind"],
                    "properties": {"kind": {"const": "work_result"}},
                },
                "then": {
                    "required": [
                        "proposedOperabilityVerdict",
                        "proposedRequirementVerdict",
                    ],
                },
            },
            {
                "required": ["op"],
                "properties": {"op": {"const": "peer_list"}},
            },
            {
                "required": ["op", "targetParticipantId", "content"],
                "properties": {"op": {"const": "peer_send"}},
            },
            {
                "required": ["op", "targetParticipantId", "content"],
                "properties": {"op": {"const": "peer_ask"}},
            },
            {
                "required": ["op", "replyTo", "content"],
                "properties": {"op": {"const": "peer_reply"}},
            },
        ],
    },
    "planning": {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op"],
                "properties": {
                    "op": {"const": "dashboard"},
                    "date": {
                        "type": "string",
                        "maxLength": 24,
                        "description": "可选计划日期；省略时读取本地今天。",
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "taskId", "date", "action"],
                "properties": {
                    "op": {"const": "task_action"},
                    "taskId": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 240,
                        "description": "必须原样使用同一日期 dashboard 返回的 tasks[].id，不能根据标题猜测。",
                    },
                    "date": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 24,
                        "description": "必须原样使用 dashboard 返回的 date。",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["complete", "start", "reopen", "cancel"],
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "eventId"],
                "properties": {
                    "op": {"const": "undo_task_event"},
                    "eventId": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 240,
                        "description": "必须使用已应用 task_action 回执中的 taskEventId。",
                    },
                },
            },
        ],
    },
    "knowledge": {
        "type": "object",
        "additionalProperties": False,
        # Keep the Provider-facing registry compact: the shared property table
        # carries validation bounds once, while oneOf retains operation-specific
        # required fields. The Python adapter still dispatches strictly by op.
        "properties": {
            "op": {"type": "string"},
            "kbId": {"type": "string", "maxLength": 240},
            "query": {"type": "string", "maxLength": 500},
            "topK": {"type": "integer", "minimum": 1, "maximum": 12},
            "searchMode": {
                "type": "string",
                "enum": ["hybrid", "lexical", "dense"],
            },
            "threshold": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "rerank": {"type": "boolean"},
            "rerankCandidateDepth": {"type": "integer", "minimum": 1, "maximum": 100},
            "fileName": {
                "type": "string",
                "maxLength": 240,
                "pattern": r"^[^/\\\x00]+$",
            },
            "fileId": {"type": "string", "maxLength": 240},
            "chunkId": {"type": "string", "maxLength": 240},
            "patterns": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {"type": "string", "maxLength": 240},
            },
            "useRegex": {"type": "boolean"},
            "caseSensitive": {"type": "boolean"},
            "maxWindows": {"type": "integer", "minimum": 1, "maximum": 20},
            "windowSize": {"type": "integer", "minimum": 1, "maximum": 300},
            "before": {"type": "integer", "minimum": 0, "maximum": 10},
            "after": {"type": "integer", "minimum": 0, "maximum": 10},
            "line": {"type": "integer", "minimum": 1, "maximum": 50_000_000},
            "offset": {"type": "integer", "minimum": 0, "maximum": 50_000_000},
            "name": {"type": "string", "maxLength": 300},
            "description": {"type": "string", "maxLength": 4_000},
            "agentEnabled": {"type": "boolean"},
            "parserProvider": {
                "type": "string",
                "enum": ["auto", "builtin", "mineru"],
            },
            "chunkingConfig": copy.deepcopy(_KNOWLEDGE_CHUNKING_PARAMETER_SCHEMA),
            "retrievalConfig": copy.deepcopy(_KNOWLEDGE_RETRIEVAL_PARAMETER_SCHEMA),
            "expectedRevision": {"type": "integer", "minimum": 1},
            "text": {"type": "string", "maxLength": 262_144},
        },
        "oneOf": [
            *[
                {
                    "required": ["op"],
                    "properties": {"op": {"const": operation}},
                }
                for operation in ("list_bases", "status")
            ],
            *[
                {
                    "required": ["op", "kbId"],
                    "properties": {"op": {"const": operation}},
                }
                for operation in ("get_base", "list_documents", "rebuild_preview")
            ],
            {
                "required": ["op", "kbId", "query"],
                "properties": {"op": {"const": "search"}},
            },
            {
                "required": ["op", "kbId", "fileId", "patterns"],
                "properties": {"op": {"const": "find"}},
            },
            {
                "required": ["op", "kbId"],
                "anyOf": [
                    {"required": ["fileId"]},
                    {"required": ["chunkId"]},
                ],
                "properties": {"op": {"const": "open"}},
            },
            {
                "required": ["op", "name"],
                "properties": {"op": {"const": "create_base"}},
            },
            {
                "required": ["op", "kbId", "expectedRevision"],
                "properties": {"op": {"const": "configure_base"}},
            },
            {
                "required": ["op", "kbId", "expectedRevision", "fileName", "text"],
                "properties": {"op": {"const": "import_text"}},
            },
            {
                "required": ["op", "kbId", "expectedRevision"],
                "properties": {"op": {"const": "rebuild"}},
            },
        ],
    },
    "todo": {
        "type": "object",
        "additionalProperties": False,
        "required": ["op"],
        "properties": {
            "op": {"type": "string"},
            "list": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["phase", "items"],
                    "properties": {
                        "phase": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 80,
                        },
                        "items": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 100,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 240,
                            },
                        },
                    },
                },
            },
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 240,
                },
            },
            "phase": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
            },
            "task": {
                "type": "string",
                "minLength": 1,
                "maxLength": 240,
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
            },
        },
        "oneOf": [
            {
                "required": ["op"],
                "oneOf": [
                    {"required": ["list"]},
                    {"required": ["items"]},
                ],
                "properties": {"op": {"const": "init"}},
            },
            {
                "required": ["op", "task"],
                "properties": {"op": {"const": "start"}},
            },
            *[
                {
                    "required": ["op"],
                    "oneOf": [
                        {"required": ["task"]},
                        {"required": ["phase"]},
                    ],
                    "properties": {"op": {"const": operation}},
                }
                for operation in ("done", "drop", "block", "unblock")
            ],
            {
                "additionalProperties": False,
                "required": ["op", "phase", "items"],
                "properties": {
                    "op": {"const": "append"},
                    "phase": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 80,
                    },
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 240,
                        },
                    },
                },
            },
            {
                "required": ["op"],
                "properties": {"op": {"const": "view"}},
            },
            {
                "required": ["op"],
                "not": {"required": ["task", "phase"]},
                "properties": {"op": {"const": "rm"}},
            },
        ],
    },
    "agent_goal": {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op"],
                "properties": {"op": {"const": "list"}},
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "confirmed", "objective"],
                "properties": {
                    "op": {"const": "confirm_setup"},
                    "confirmed": {"const": True},
                    "objective": {"type": "string", "minLength": 1, "maxLength": 4_000},
                    "successCriteria": {"type": "string", "maxLength": 2_000},
                    "evidenceExpectations": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {"type": "string", "minLength": 1, "maxLength": 600},
                    },
                    "tokenBudget": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 100_000_000,
                    },
                    "timeBudgetMs": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 31_536_000_000,
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op"],
                "anyOf": [
                    {"required": ["objective"]},
                    {"required": ["successCriteria"]},
                    {"required": ["evidenceExpectations"]},
                    {"required": ["tokenBudget"]},
                    {"required": ["timeBudgetMs"]},
                ],
                "properties": {
                    "op": {"const": "update"},
                    "objective": {"type": "string", "minLength": 1, "maxLength": 4_000},
                    "successCriteria": {"type": "string", "maxLength": 2_000},
                    "evidenceExpectations": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {"type": "string", "minLength": 1, "maxLength": 600},
                    },
                    "tokenBudget": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 100_000_000,
                    },
                    "timeBudgetMs": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 31_536_000_000,
                    },
                },
            },
            *[
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op"],
                    "properties": {"op": {"const": operation}},
                }
                for operation in ("pause", "resume")
            ],
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "summary", "evidence"],
                "properties": {
                    "op": {"const": "complete"},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 2_000},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kind", "summary", "reference"],
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": ["test", "artifact", "commit", "receipt", "note"],
                                },
                                "summary": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 600,
                                },
                                "reference": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 1_000,
                                },
                            },
                        },
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "reason"],
                "properties": {
                    "op": {"const": "cancel"},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
                },
            },
        ],
    },
    "work_documents": {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op"],
                "properties": {
                    "op": {"const": "list"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op"],
                "properties": {
                    "op": {"const": "history.search"},
                    "query": {"type": "string", "maxLength": 240},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
            *[
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "documentId"],
                    "properties": {
                        "op": {"const": operation},
                        "documentId": {"type": "string", "pattern": "^workdoc_[a-f0-9]{32}$"},
                    },
                }
                for operation in ("get", "repair", "erase.preview")
            ],
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "op", "authorityKind", "authorityId", "authorityRevision",
                    "workspaceRoot", "sourcePath",
                ],
                "properties": {
                    "op": {"const": "register"},
                    "authorityKind": {
                        "type": "string",
                        "enum": ["session_goal", "room_work_item"],
                    },
                    "authorityId": {"type": "string", "minLength": 1, "maxLength": 240},
                    "authorityRevision": {"type": "integer", "minimum": 0},
                    "workspaceRoot": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "sourcePath": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "title": {"type": "string", "maxLength": 240},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "documentId", "terminalReceiptId"],
                "properties": {
                    "op": {"const": "archive"},
                    "documentId": {"type": "string", "pattern": "^workdoc_[a-f0-9]{32}$"},
                    "terminalReceiptId": {"type": "string", "minLength": 1, "maxLength": 240},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "documentId", "authorityRevision", "transitionReceiptId"],
                "properties": {
                    "op": {"const": "reopen"},
                    "documentId": {"type": "string", "pattern": "^workdoc_[a-f0-9]{32}$"},
                    "authorityRevision": {"type": "integer", "minimum": 0},
                    "transitionReceiptId": {"type": "string", "minLength": 1, "maxLength": 240},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "documentId", "approvalId", "payloadSha256"],
                "properties": {
                    "op": {"const": "erase"},
                    "documentId": {"type": "string", "pattern": "^workdoc_[a-f0-9]{32}$"},
                    "approvalId": {"type": "string", "minLength": 1, "maxLength": 240},
                    "payloadSha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                },
            },
        ],
    },
    "desktop_semantic": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "op": {
                "type": "string",
                "enum": ["status", "list", "inspect", "find", "act"],
            },
            "includeBackground": {"type": "boolean"},
            "bundleId": {"type": "string", "maxLength": 300},
            "pid": {"type": "integer", "minimum": 1, "maximum": 2147483647},
            "match": {
                "type": "object",
                "additionalProperties": False,
                "minProperties": 1,
                "properties": {
                    "role": {"type": "string", "minLength": 1, "maxLength": 80},
                    "subrole": {"type": "string", "minLength": 1, "maxLength": 80},
                    "identifier": {"type": "string", "minLength": 1, "maxLength": 240},
                    "label": {"type": "string", "minLength": 1, "maxLength": 240},
                    "query": {"type": "string", "minLength": 1, "maxLength": 300},
                },
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "maxNodes": {"type": "integer", "minimum": 1, "maximum": 500},
            "maxDepth": {"type": "integer", "minimum": 1, "maximum": 12},
            "sinceSnapshotId": {"type": "string", "maxLength": 200},
            "incremental": {"type": "boolean"},
            "snapshotId": {"type": "string", "minLength": 1, "maxLength": 200},
            "revision": {"type": "integer", "minimum": 1},
            "nodeRef": {"type": "string", "minLength": 1, "maxLength": 200},
            "action": {
                "type": "string",
                "enum": [
                    "press", "click", "double_click", "right_click", "long_press",
                    "focus", "set_text", "type_text", "key", "increment", "decrement",
                    "show_menu", "scroll"
                ],
            },
            "text": {"type": "string", "maxLength": 8000},
            "key": {
                "type": "string",
                "enum": [
                    "return", "enter", "tab", "escape", "space", "delete",
                    "forward_delete", "left", "right", "up", "down", "home",
                    "end", "page_up", "page_down"
                ],
            },
            "modifiers": {
                "type": "array",
                "maxItems": 5,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": ["command", "option", "control", "shift", "fn"],
                },
            },
            "durationMs": {"type": "integer", "minimum": 100, "maximum": 3000},
            "scrollDelta": {
                "type": "integer",
                "minimum": -20,
                "maximum": 20,
                "not": {"const": 0},
            },
        },
        "oneOf": [
            {
                "required": ["op"],
                "properties": {"op": {"const": "status"}},
            },
            {
                "required": ["op"],
                "properties": {"op": {"const": "list"}},
            },
            {
                "required": ["op"],
                "properties": {"op": {"const": "inspect"}},
            },
            {
                "required": ["op", "match"],
                "properties": {"op": {"const": "find"}},
            },
            {
                "required": ["op", "action"],
                "oneOf": [
                    {"required": ["snapshotId", "revision", "nodeRef"]},
                    {"required": ["match"]},
                ],
                "properties": {"op": {"const": "act"}},
            },
        ],
    },
}

_DELEGATION_BUDGET_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "maxTotalTokens": {
            "type": "integer",
            "minimum": 256,
            "maximum": 262_144,
        },
        "maxDurationMs": {
            "type": "integer",
            "minimum": 1_000,
            "maximum": 900_000,
        },
        "maxOutputChars": {
            "type": "integer",
            "minimum": 256,
            "maximum": 100_000,
        },
    },
}


_RUNTIME_TOOL_ARGUMENT_SCHEMAS: dict[str, dict[str, object]] = {
    "query": {"type": "string", "maxLength": 500},
    "suiteId": {"type": "string", "minLength": 1, "maxLength": 120},
    "suiteRevision": {"type": "string", "minLength": 1, "maxLength": 120},
    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    "includeArchived": {"type": "boolean"},
    "targets": {
        "type": "array",
        "minItems": 1,
        "maxItems": 12,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "id", "title"],
            "properties": {
                "kind": {"type": "string", "enum": ["session", "room", "run"]},
                "id": {"type": "string", "minLength": 1, "maxLength": 240},
                "title": {"type": "string", "maxLength": 240},
                "traceIds": {
                    "type": "array",
                    "maxItems": 32,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 240},
                },
            },
        },
    },
    "workDocument": {
        "type": "object",
        "additionalProperties": False,
        "required": ["authorityKind", "authorityId", "authorityRevision"],
        "properties": {
            "authorityKind": {
                "type": "string",
                "enum": ["session_goal", "room_work_item"],
            },
            "authorityId": {"type": "string", "minLength": 1, "maxLength": 240},
            "authorityRevision": {"type": "integer", "minimum": 0},
            "title": {"type": "string", "maxLength": 240},
        },
        "description": (
            "可选的显式权威绑定。workspace_write 成功后最多尝试一次注册；"
            "注册失败只生成 documentSync pending/failed 回执和 Trace 引用，不阻断真实写入；"
            "禁止根据目录或文件名推断。"
        ),
    },
    "changes": {
        "type": "array",
        "minItems": 1,
        "maxItems": 12,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["key", "value"],
            "properties": {
                "key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "description": "只能使用 input.get_settings/preview_settings 暴露的 Agent 可管理键。",
                },
                "value": {
                    "oneOf": [
                        {"type": "boolean"},
                        {"type": "integer"},
                        {"type": "string"},
                    ]
                },
            },
        },
    },
    "selectedKeys": {
        "type": "array",
        "minItems": 1,
        "maxItems": 100,
        "items": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    "sourceApprovalId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240,
        "description": "使用对应已应用回执中的 approvalId。",
    },
    "currentInput": {"type": "string", "maxLength": 240},
    "recentContext": {"type": "string", "maxLength": 800},
    "topK": {"type": "integer", "minimum": 1, "maximum": 12},
    "provider": {"type": "string", "minLength": 1, "maxLength": 80},
    "slot": {"type": "string", "enum": ["instant", "knowledge"]},
    "endpoint": {"type": "string", "minLength": 1, "maxLength": 320},
    "model": {"type": "string", "maxLength": 200},
    "bookId": {"type": "string", "minLength": 1, "maxLength": 240},
    "traceId": {"type": "string", "minLength": 1, "maxLength": 240},
    "runId": {"type": "string", "minLength": 1, "maxLength": 240},
    "targetRunId": {"type": "string", "minLength": 1, "maxLength": 240},
    "message": {"type": "string", "minLength": 1, "maxLength": 4_000},
    "proposalId": {"type": "string", "minLength": 1, "maxLength": 240},
    "idempotencyKey": {"type": "string", "minLength": 1, "maxLength": 240},
    "claimKey": {"type": "string", "minLength": 1, "maxLength": 240},
    "revisionId": {"type": "string", "minLength": 1, "maxLength": 240},
    "instruction": {"type": "string", "minLength": 1, "maxLength": 8_000},
    # `text` and `reason` were declared twice in this literal. Python keeps the
    # last binding, so these earlier entries never took effect: the schemas
    # actually in force are the ones defined further down. They are removed so
    # the table states what the runtime really enforces. This is not a schema
    # change -- the emitted argument schemas are byte-identical before and
    # after. The narrower limits these lines appeared to impose (text
    # minLength 1 / maxLength 1200, reason maxLength 400) have never been
    # applied, which is recorded as a separate finding rather than silently
    # "restored" here, because tightening them would change Tool validation.
    "memoryKind": {
        "type": "string",
        "enum": ["fact", "preference", "decision", "commitment", "project_state"],
    },
    "evidenceIds": {
        "type": "array",
        "minItems": 1,
        "maxItems": 16,
        "items": {"type": "string", "minLength": 1, "maxLength": 240},
    },
    "changeSummary": {"type": "string", "maxLength": 400},
    "updates": {
        "type": "object",
        "additionalProperties": False,
        "$defs": {
            "item": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "provenance", "evidenceIds"],
                "properties": {
                    "itemId": {"type": "string", "maxLength": 160},
                    "text": {"type": "string", "minLength": 1, "maxLength": 280},
                    "provenance": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["sourceType", "sourceId"],
                        "properties": {
                            "sourceType": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 80,
                            },
                            "sourceId": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 240,
                            },
                            "observedAtMs": {
                                "type": ["integer", "null"],
                                "minimum": 0,
                            },
                        },
                    },
                    "evidenceIds": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 240,
                        },
                    },
                },
            }
        },
        "properties": {
            section: {
                "type": "array",
                "maxItems": limit,
                "items": {"$ref": "#/properties/updates/$defs/item"},
            }
            for section, limit in {
                "personality": 6,
                "capabilities": 12,
                "recentWork": 8,
                "lessonsAndLimits": 8,
                "activeCommitments": 8,
            }.items()
        },
    },
    "kind": {
        "type": "string",
        "enum": [
            "apps",
            "books",
            "atoms",
            "timelines",
            "evidence",
            "tags",
            "phrases",
            "groups",
        ],
    },
    "scheduleId": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240,
        "description": "使用 agent_schedule.list 返回的 scheduleId。",
    },
    "title": {"type": "string", "minLength": 1, "maxLength": 240},
    "targetType": {"type": "string", "enum": ["session", "role"]},
    "targetId": {"type": "string", "maxLength": 240},
    "targetSessionId": {"type": "string", "maxLength": 240},
    "targetRoleId": {"type": "string", "maxLength": 120},
    "targetRoleVersion": {"type": "string", "maxLength": 40},
    "planningTaskId": {"type": "string", "maxLength": 240},
    "todoTask": {
        "type": "string",
        "minLength": 1,
        "maxLength": 240,
        "description": (
            "可选 Todo 导航链接；未传入时子 Agent 仍必须独立启动，"
            "不得因 Session 存在 Todo 而拒绝或自动绑定。"
        ),
    },
    "wakeAtMs": {"type": "integer", "minimum": 1},
    "timezone": {"type": "string", "maxLength": 80},
    "recurrenceKind": {"type": "string", "enum": ["once", "daily", "weekly"]},
    "recurrenceInterval": {"type": "integer", "minimum": 1, "maximum": 30},
    "maxRuns": {"type": "integer", "minimum": 1, "maximum": 100},
    "status": {"type": "string", "maxLength": 40},
    "agent": {
        "type": "string",
        "enum": ["researcher", "planner", "worker", "reviewer", "delegate"],
    },
    "version": {"type": "string", "enum": ["1"]},
    "task": {"type": "string", "minLength": 1, "maxLength": 8_000},
    "expectedOutput": {"type": "string", "minLength": 1, "maxLength": 2_000},
    "acceptanceCriteria": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 1_000},
    },
    "outputSchema": {"type": "object"},
    "modelProfile": {
        "type": "string",
        "minLength": 3,
        "maxLength": 240,
        "pattern": r"^[^\s/]+/[^\s/]+$",
        "description": (
            "默认省略并继承父 Session；只有用户明确选择，或已从 Pi 确认的模型目录"
            "取得完全一致的 provider/model 时才传入，禁止猜测 Provider 或模型名。"
        ),
    },
    "thinkingLevel": {
        "type": "string",
        "enum": ["off", "minimal", "low", "medium", "high", "xhigh", "max"],
    },
    "budget": dict(_DELEGATION_BUDGET_SCHEMA),
    "access": {
        "type": "string",
        "enum": ["inherit", "read_only", "write"],
    },
    "allowedTools": {
        "type": "array",
        "minItems": 1,
        "maxItems": 64,
        "uniqueItems": True,
        "items": {"type": "string", "enum": list(CONTROL_TOOL_IDS)},
        "description": (
            "只接受 PAW 产品工具 ID；Pi 原生 tool_search/read/grep/find/bash "
            "由子 Session 运行时按能力投影提供，不写入这里。"
        ),
    },
    "piSkillsEnabled": {"type": "boolean"},
    "codexSkillsEnabled": {"type": "boolean"},
    "workspaceRoots": {
        "type": "array",
        "minItems": 1,
        "maxItems": 4,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 1_024},
    },
    "tasks": {
        "type": "array",
        "minItems": 1,
        "maxItems": 2,
        "description": (
            "批量委派；也可以改用 agent、version、task 提交单项任务。"
            " contextMode 仅在请求根级设置，不属于 tasks[] 子项。"
        ),
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["agent", "task", "expectedOutput", "acceptanceCriteria"],
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": ["researcher", "planner", "worker", "reviewer", "delegate"],
                },
                "version": {"type": "string", "enum": ["1"]},
                "task": {"type": "string", "minLength": 1, "maxLength": 8_000},
                "expectedOutput": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2_000,
                },
                "acceptanceCriteria": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1_000,
                    },
                },
                "outputSchema": {"type": "object"},
                "modelProfile": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 240,
                    "pattern": r"^[^\s/]+/[^\s/]+$",
                    "description": (
                        "默认省略并继承父 Session；仅接受用户明确选择或 Pi 目录确认的精确值。"
                    ),
                },
                "thinkingLevel": {
                    "type": "string",
                    "enum": ["off", "minimal", "low", "medium", "high", "xhigh", "max"],
                },
                "budget": dict(_DELEGATION_BUDGET_SCHEMA),
                "access": {
                    "type": "string",
                    "enum": ["inherit", "read_only", "write"],
                },
                "allowedTools": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "uniqueItems": True,
                    "items": {"type": "string", "enum": list(CONTROL_TOOL_IDS)},
                    "description": (
                        "只接受 PAW 产品工具 ID；Pi 原生工具由运行时能力投影提供。"
                    ),
                },
                "piSkillsEnabled": {"type": "boolean"},
                "codexSkillsEnabled": {"type": "boolean"},
                "workspaceRoots": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 1_024},
                },
            },
        },
    },
    "contextMode": {
        "type": "string",
        "enum": ["fresh", "fork"],
        "description": "委派请求级上下文模式；不要在 tasks[] 子项中重复设置。",
    },
    "forkEntryId": {"type": "string", "minLength": 1, "maxLength": 240},
    "wait": {
        "type": "boolean",
        "description": "默认不等待；仅显式 true 才等待终态并返回完整结果。",
    },
    "batchId": {"type": "string", "minLength": 1, "maxLength": 240},
    "artifactId": {"type": "string", "minLength": 1, "maxLength": 240},
    "reason": {"type": "string", "minLength": 1, "maxLength": 2_000},
    "draftId": {"type": "string", "minLength": 1, "maxLength": 160},
    "pluginId": {"type": "string", "minLength": 1, "maxLength": 240},
    "manifest": {"type": "object"},
    "packageJson": {
        "type": "object",
        "description": "原生 Pi Package 的完整 package.json，包含名称、版本与 pi 资源声明。",
    },
    "files": {"type": "object"},
    "sourcePath": {"type": "string", "minLength": 1, "maxLength": 1_024},
    "packageSource": {
        "type": "string",
        "minLength": 1,
        "description": "原生 Pi Package 来源：npm: 包标识、Git 来源或本地 Package 目录。",
    },
    "catalogId": {"type": "string", "minLength": 1},
    "catalogVersion": {
        "type": "string",
        "description": "catalogId 对应的版本；省略时使用目录中的默认版本。",
    },
    "validationToken": {"type": "string", "minLength": 1, "maxLength": 240},
    "previewToken": {"type": "string", "minLength": 1, "maxLength": 240},
    "payloadSha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    "enable": {"type": "boolean"},
    "path": {"type": "string", "minLength": 1, "maxLength": 1_024},
    "resourceRef": {"type": "string", "maxLength": 1_024},
    "resourceRevision": {
        "type": "string",
        "pattern": r"^(?:sha256:[0-9a-fA-F]{64}|missing)$",
    },
    "selector": {"type": "string"},
    "selectorCursor": {"type": "integer", "minimum": 0},
    "root": {"type": "string", "maxLength": 1_024},
    "server": {"type": "string", "minLength": 1, "maxLength": 120},
    "line": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
    "column": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
    "includeDeclaration": {"type": "boolean"},
    "newName": {"type": "string", "minLength": 1, "maxLength": 240},
    "depth": {"type": "integer", "minimum": 1, "maximum": 3},
    "offset": {"type": "integer", "minimum": 0, "maximum": 50_000_000},
    "lineOffset": {"type": "integer", "minimum": 1, "maximum": 50_000_000},
    "lineLimit": {"type": "integer", "minimum": 1, "maximum": 2_000},
    "mode": {"type": "string", "enum": ["content", "name", "both"]},
    "patternKind": {"type": "string", "enum": ["literal", "regex", "glob"]},
    "glob": {"type": "string", "minLength": 1, "maxLength": 500},
    "context": {"type": "integer", "minimum": 0, "maximum": 20},
    "oldText": {"type": "string", "minLength": 1, "maxLength": 65_536},
    "newText": {"type": "string", "maxLength": 131_072},
    "edits": {
        "type": "array",
        "minItems": 1,
        "maxItems": 64,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "oldText": {"type": "string", "minLength": 1, "maxLength": 65_536},
                "newText": {"type": "string", "maxLength": 131_072},
            },
            "required": ["oldText", "newText"],
        },
    },
    "content": {"type": "string", "maxLength": 2 * 1024 * 1024},
    "expectedOccurrences": {"type": "integer", "minimum": 1, "maximum": 100},
    "command": {"type": "string", "minLength": 1, "maxLength": 2_000},
    "cwd": {"type": "string", "maxLength": 1_024},
    "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 120},
    "allowNetwork": {"type": "boolean"},
    "asArtifact": {"type": "boolean"},
    "label": {"type": "string", "minLength": 1, "maxLength": 120},
    "jobId": {"type": "string", "pattern": r"^bg_[a-f0-9]{32}$"},
    "cursor": {"type": "integer", "minimum": 0},
    "limitBytes": {"type": "integer", "minimum": 1, "maximum": 131_072},
    "action": {"type": "string", "minLength": 1, "maxLength": 120},
    "caseSensitive": {"type": "boolean"},
    "deviceId": {"type": "string", "minLength": 1, "maxLength": 160},
    "tabId": {"type": "integer", "minimum": 1},
    "refId": {"type": "string", "minLength": 1, "maxLength": 160},
    "url": {"type": "string", "minLength": 1, "maxLength": 4_000},
    "text": {"type": "string", "maxLength": 8_000},
    "script": {
        "type": "string",
        "minLength": 1,
        "maxLength": 24_000,
        "description": "在 PAW 内置 Chromium 中运行的一段 ego-browser JavaScript；不要包含 shell 或 imports。",
    },
    "clear": {"type": "boolean"},
    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
    "amount": {"type": "integer", "minimum": 80, "maximum": 2_400},
    "timeoutMs": {"type": "integer", "minimum": 100, "maximum": 20_000},
    "maxChars": {"type": "integer", "minimum": 1_000, "maximum": 80_000},
}

_RUNTIME_TOOL_ARGUMENT_SCHEMA_OVERRIDES: dict[tuple[str, str], dict[str, object]] = {
    ("plugins", "sourcePath"): {
        "type": "string",
        "minLength": 1,
        "maxLength": 1_024,
        "description": (
            "仅用于含 rag-ime-plugin.json 的旧插件目录；"
            "原生 Pi Package（包括本地目录）使用 packageSource。"
        ),
    },
    ("workspace_list", "path"): {
        "type": "string",
        "maxLength": 1_024,
        "description": "可省略或传空字符串以列出当前授权工作区根目录。",
    },
    ("workspace_list", "limit"): {"type": "integer", "minimum": 1, "maximum": 300},
    ("workspace_read", "limit"): {
        "type": "integer",
        "minimum": 1,
        "maximum": 65_536,
    },
    ("workspace_lsp", "query"): {
        "type": "string",
        "maxLength": 240,
        "pattern": r"^[^\u0000]*$",
    },
    ("workspace_lsp", "title"): {
        "type": "string",
        "minLength": 1,
        "maxLength": 500,
        "pattern": r"^[^\u0000]+$",
    },
    ("memory", "mode"): {
        "type": "string",
        "enum": ["current", "historical", "change"],
        "description": "默认 current；只有显式选择 historical/change 才读取历史或变更。",
    },
    ("session_search", "query"): {
        "type": "string",
        "maxLength": 240,
        "description": "可省略；空查询返回最近的持久化 Session 锚点。",
    },
    ("session_search", "limit"): {
        "type": "integer",
        "minimum": 1,
        "maximum": 50,
    },
    ("workspace_search", "query"): {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "pattern": r"^[^\r\n\u0000]+$",
    },
    ("workspace_search", "path"): {
        "type": "string",
        "maxLength": 1_024,
        "description": "可省略或传空字符串以搜索全部授权工作区。",
    },
    ("workspace_job", "timeoutSeconds"): {
        "type": "integer",
        "minimum": 1,
        "maximum": 86_400,
        "description": "后台任务最长运行秒数；默认 3600，最大 86400。",
    },
    ("workspace_job", "idempotencyKey"): {
        "type": "string",
        "minLength": 1,
        "maxLength": 240,
        "description": (
            "start 的稳定请求标识。启动回复丢失时携带相同标识和参数读取原任务，"
            "不会再次执行命令；新的独立任务使用新标识。"
        ),
    },
    ("browser", "timeoutMs"): {
        "type": "integer",
        "minimum": 1_000,
        "maximum": 120_000,
        "description": "ego-browser run 最长 120 秒；普通等待操作仍由浏览器运行时收窄。",
    },
}

_RUNTIME_TOOL_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "overview": ("query", "limit"),
    "input": (
        "changes", "selectedKeys", "sourceApprovalId", "query", "currentInput",
        "recentContext", "topK", "limit",
    ),
    "voice": ("provider", "sourceApprovalId"),
    "agent_schedule": (
        "scheduleId", "title", "instruction", "targetType", "targetId",
        "targetSessionId", "targetRoleId", "targetRoleVersion", "planningTaskId",
        "wakeAtMs", "timezone", "recurrenceKind", "recurrenceInterval", "maxRuns",
        "status", "limit",
    ),
    "memory": (
        "query", "limit", "kind", "bookId", "traceId", "runId", "instruction",
        "targetId", "text", "reason", "memoryKind", "evidenceIds", "claimKey",
        "idempotencyKey", "proposalId", "draftId", "mode", "trigger",
        "claim", "sourceId", "captureScope", "basis", "futureUse", "supersedes",
    ),
    "agent_role_book": (
        "revisionId", "draftId", "limit", "updates", "changeSummary",
    ),
    "models": ("slot", "provider", "endpoint", "model", "sourceApprovalId"),
    "configuration": ("query", "limit", "action", "sourceApprovalId"),
    "agents": (
        "agent", "version", "task", "tasks", "expectedOutput",
        "acceptanceCriteria", "outputSchema", "modelProfile", "thinkingLevel",
        "budget", "access", "allowedTools", "piSkillsEnabled", "codexSkillsEnabled",
        "workspaceRoots", "todoTask", "contextMode", "forkEntryId", "wait",
        "runId", "batchId", "targetRunId", "message", "artifactId", "limit",
    ),
    "session_search": ("query", "limit", "includeArchived"),
    "trace_diagnostics": ("targets", "reportId", "query", "patternId", "revision", "offset", "command", "clientRequestId", "input", "candidateId"),
    "plugins": (
        "draftId", "manifest", "packageJson", "files", "sourcePath",
        "packageSource", "catalogId", "catalogVersion",
        "validationToken", "pluginId", "enable",
        "previewToken", "payloadSha256",
    ),
    "sandbox": ("suiteId", "suiteRevision"),
    "browser": (
        "deviceId", "tabId", "refId", "url", "text", "script", "clear", "direction",
        "amount", "timeoutMs", "maxChars", "limit",
    ),
    "workspace_list": ("path", "depth", "limit"),
    "workspace_lsp": (
        "root", "path", "server", "query", "line", "column", "timeoutMs",
        "includeDeclaration", "newName", "title",
    ),
    "workspace_read": (
        "path", "resourceRef", "selector", "selectorCursor",
        "offset", "limit", "lineOffset", "lineLimit", "asArtifact",
    ),
    "workspace_search": (
        "query", "path", "mode", "caseSensitive", "limit",
        "patternKind", "glob", "context",
    ),
    "workspace_patch": ("path", "oldText", "newText", "expectedOccurrences"),
    "workspace_edit": ("path", "resourceRevision", "edits"),
    "workspace_write": ("path", "resourceRevision", "content", "workDocument"),
    "workspace_shell": ("command", "cwd", "timeoutSeconds", "allowNetwork"),
    "workspace_job": (
        "command", "label", "jobId", "cwd", "timeoutSeconds", "allowNetwork",
        "cursor", "limitBytes", "limit", "status", "reason", "idempotencyKey",
    ),
}

_RUNTIME_TOOL_REQUIRED_ARGUMENTS: dict[tuple[str, str], tuple[str, ...]] = {
    ("input", "preview_settings"): ("changes",),
    ("input", "apply_settings"): ("changes",),
    ("input", "rollback_settings"): ("sourceApprovalId",),
    ("input", "candidate_explain"): ("query",),
    ("input", "lexicon_apply"): ("selectedKeys",),
    ("input", "lexicon_rollback"): ("sourceApprovalId",),
    ("voice", "provider_preview"): ("provider",),
    ("voice", "provider_apply"): ("provider",),
    ("voice", "provider_rollback"): ("sourceApprovalId",),
    ("agent_schedule", "runs"): ("scheduleId",),
    ("agent_schedule", "schedule"): ("instruction", "targetType", "wakeAtMs"),
    ("agent_schedule", "pause"): ("scheduleId",),
    ("agent_schedule", "resume"): ("scheduleId",),
    ("agent_schedule", "cancel"): ("scheduleId",),
    ("agent_schedule", "retry"): ("scheduleId",),
    ("memory", "read"): ("bookId",),
    ("memory", "trace"): ("traceId",),
    ("memory", "capture"): (
        "kind",
        "claim",
        "captureScope",
        "basis",
        "futureUse",
    ),
    ("memory", "curation_prepare"): ("trigger",),
    ("memory", "maintenance_preview"): ("trigger",),
    ("memory", "maintenance_review"): ("runId",),
    ("memory", "maintenance_apply"): ("runId",),
    ("memory", "maintenance_rollback"): ("runId",),
    ("memory", "remember_preview"): ("text",),
    ("memory", "correct_preview"): ("targetId", "text"),
    ("memory", "forget_preview"): ("targetId", "reason"),
    ("memory", "remember_apply"): ("proposalId",),
    ("memory", "correct_apply"): ("proposalId",),
    ("memory", "forget_apply"): ("proposalId",),
    ("memory", "governance_rollback"): ("proposalId",),
    ("memory", "explain"): ("targetId",),
    ("memory", "review"): ("draftId",),
    ("agent_role_book", "propose_revision"): ("updates",),
    ("trace_diagnostics", "inspect"): ("targets",),
    ("trace_diagnostics", "read"): ("reportId",),
    ("trace_diagnostics", "command"): ("reportId", "command", "clientRequestId"),
    ("models", "profile_preview"): ("slot",),
    ("models", "profile_apply"): ("slot",),
    ("models", "profile_rollback"): ("sourceApprovalId",),
    ("configuration", "restore_preview"): ("sourceApprovalId",),
    ("configuration", "restore_apply"): ("sourceApprovalId",),
    ("agents", "artifact"): ("artifactId",),
    ("agents", "call"): ("targetRunId", "message"),
    ("plugins", "create_package"): ("draftId", "packageJson", "files"),
    ("plugins", "propose_install"): ("validationToken",),
    ("plugins", "propose_enable"): ("pluginId",),
    ("plugins", "propose_disable"): ("pluginId",),
    ("plugins", "propose_update"): ("validationToken",),
    ("plugins", "propose_rollback"): ("pluginId",),
    ("plugins", "propose_uninstall"): ("pluginId",),
    ("plugins", "apply"): ("previewToken", "payloadSha256"),
    ("browser", "run"): ("script",),
    ("browser", "navigate"): ("url",),
    ("browser", "click"): ("refId",),
    ("browser", "type"): ("refId", "text"),
    ("workspace_search", "search"): ("query",),
    ("workspace_lsp", "hover"): ("path",),
    ("workspace_lsp", "definition"): ("path",),
    ("workspace_lsp", "references"): ("path",),
    ("workspace_lsp", "diagnostics"): ("path",),
    ("workspace_lsp", "rename"): ("path", "newName"),
    ("workspace_lsp", "code_action_apply"): ("path", "title"),
    ("workspace_patch", "apply"): ("path", "oldText", "newText"),
    ("workspace_edit", "apply"): ("path", "resourceRevision", "edits"),
    ("workspace_write", "apply"): ("path", "resourceRevision", "content"),
    ("workspace_shell", "run"): ("command",),
    ("workspace_job", "start"): ("command",),
    ("workspace_job", "status"): ("jobId",),
    ("workspace_job", "logs"): ("jobId",),
    ("workspace_job", "cancel"): ("jobId",),
}

_RUNTIME_TOOL_REQUIRED_ALTERNATIVES: dict[
    tuple[str, str], tuple[tuple[str, ...], ...]
] = {
    ("models", "profile_preview"): (("provider",), ("endpoint",), ("model",)),
    ("models", "profile_apply"): (("provider",), ("endpoint",), ("model",)),
    ("agents", "delegate"): (
        ("tasks",),
        ("agent", "task", "expectedOutput", "acceptanceCriteria"),
    ),
    ("agents", "abort"): (("runId",), ("batchId",)),
    ("agent_role_book", "review"): (("revisionId",), ("draftId",)),
    ("memory", "get"): (("targetId",), ("draftId",)),
    ("workspace_read", "read"): (("path",), ("resourceRef",)),
}


_RUNTIME_TOOL_PROJECTIONS: dict[str, tuple[dict[str, object], ...]] = {
    "workspace_list": (
        {
            "name": "ls",
            "operation": "list",
            "description": "List files and directories inside the authorized workspace.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "maxLength": 1_024},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 3},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 300},
                },
            },
        },
    ),
    "workspace_read": (
        {
            "name": "read",
            "operation": "read",
            "description": (
                "Read authorized UTF-8 files or managed resources with bounded "
                "continuation. HTML files automatically include a sandboxed "
                "preview; use asArtifact to preview another generated text file."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "oneOf": [{"required": ["path"]}, {"required": ["resourceRef"]}],
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 1_024},
                    "resourceRef": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1_024,
                    },
                    "selector": {"type": "string", "minLength": 1, "maxLength": 500},
                    "selectorCursor": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 50_000_000,
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50_000_000,
                    },
                    "byteOffset": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 50_000_000,
                    },
                    "byteLimit": {"type": "integer", "minimum": 1, "maximum": 65_536},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2_000},
                    "asArtifact": {"type": "boolean"},
                },
            },
        },
    ),
    "workspace_search": (
        {
            "name": "grep",
            "operation": "search",
            "description": (
                "Search authorized UTF-8 files for a literal string or regular "
                "expression and return matching lines."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["pattern"],
                "properties": {
                    "pattern": {"type": "string", "minLength": 1, "maxLength": 500},
                    "path": {"type": "string", "maxLength": 1_024},
                    "glob": {"type": "string", "maxLength": 300},
                    "ignoreCase": {"type": "boolean"},
                    "literal": {"type": "boolean"},
                    "context": {"type": "integer", "minimum": 0, "maximum": 20},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
        {
            "name": "find",
            "operation": "search",
            "description": (
                "Find files and directories by glob pattern inside the authorized "
                "workspace."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["pattern"],
                "properties": {
                    "pattern": {"type": "string", "minLength": 1, "maxLength": 500},
                    "path": {"type": "string", "maxLength": 1_024},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
    ),
    "workspace_edit": (
        {
            "name": "edit",
            "operation": "apply",
            "description": (
                "Apply exact replacements to the file revision returned by the "
                "latest read."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "resourceRevision", "edits"],
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 1_024},
                    "resourceRevision": {
                        "type": "string",
                        "pattern": r"^sha256:[0-9a-fA-F]{64}$",
                    },
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["oldText", "newText"],
                            "properties": {
                                "oldText": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 65_536,
                                },
                                "newText": {"type": "string", "maxLength": 131_072},
                            },
                        },
                    },
                },
            },
        },
    ),
    "workspace_write": (
        {
            "name": "write",
            "operation": "apply",
            "description": (
                "Create or overwrite an authorized UTF-8 file with snapshot "
                "preflight."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "resourceRevision", "content"],
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 1_024},
                    "resourceRevision": {
                        "type": "string",
                        "pattern": r"^(?:sha256:[0-9a-fA-F]{64}|missing)$",
                    },
                    "content": {"type": "string", "maxLength": 2_097_152},
                    "workDocument": copy.deepcopy(
                        _RUNTIME_TOOL_ARGUMENT_SCHEMAS["workDocument"]
                    ),
                },
            },
        },
    ),
    "workspace_shell": (
        {
            "name": "bash",
            "operation": "run",
            "description": (
                "Execute a bounded shell command inside the authorized workspace "
                "and return output and exit status."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "minLength": 1, "maxLength": 2_000},
                    "cwd": {"type": "string", "maxLength": 1_024},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
                    "allowNetwork": {"type": "boolean"},
                },
            },
        },
    ),
    "memory": (
        {
            "name": "memory_capture",
            "operation": "capture",
        },
    ),
}

_PLANNING_TARGET_STATUS = {
    "complete": "done",
    "start": "in_progress",
    "reopen": "todo",
    "cancel": "cancelled",
}
_PLANNING_ACTION_LABELS = {
    "complete": "标记为已完成",
    "start": "标记为进行中",
    "reopen": "重新打开",
    "cancel": "取消任务",
}
_PLANNING_STATUS_LABELS = {
    "todo": "待办",
    "in_progress": "进行中",
    "done": "已完成",
    "completed": "已完成",
    "cancelled": "已取消",
}

# Agent writes intentionally cover only ordinary, non-secret input settings.
# Provider, privacy, management-token and Pi-runtime changes remain outside this
# tool until their own preview/restart contracts exist.
_INPUT_SETTING_FIELDS: dict[str, dict[str, object]] = {
    "interaction.postCommit.enabled": {"label": "提交后预测"},
    "interaction.postCommit.showPendingStatus": {"label": "立即显示处理状态"},
    "interaction.postCommit.idleTriggerMs": {"label": "停顿触发时间", "min": 100, "max": 5_000},
    "interaction.postCommit.minDeltaChars": {"label": "最少新增字数", "min": 1, "max": 100},
    "interaction.postCommit.maxCallsPer10s": {"label": "10 秒最大调用数", "min": 1, "max": 20},
    "interaction.postCommit.cooldownMs": {"label": "空结果冷却时间", "min": 0, "max": 30_000},
    "interaction.postCommit.pendingStatusDelayMs": {"label": "状态显示延迟", "min": 0, "max": 5_000},
    "interaction.postCommit.panelTtlMs": {"label": "生成结果停留时间", "min": 500, "max": 30_000},
    "interaction.postCommit.tabAction": {"label": "Tab 行为"},
    "display.showSourceBadge": {"label": "显示来源标记"},
    "display.showDiagnosticsInline": {"label": "候选行内诊断"},
    "display.maxPostCommitCandidates": {"label": "预测候选数量", "min": 1, "max": 10},
    "display.panelStyle": {"label": "候选面板样式"},
    "display.candidateFontSize": {"label": "候选字号", "min": 10, "max": 28},
    "display.fadeAnimation": {"label": "候选动画"},
    "display.maxWidth": {"label": "候选面板宽度", "min": 280, "max": 1_200},
    "activeRag.enabled": {"label": "知识生成入口"},
    "activeRag.localOnlyDefault": {"label": "预览默认仅本地检索"},
    "pinyin.fuzzyProfile": {"label": "模糊音方案"},
    "pinyin.rimeManagedPatch": {"label": "Rime 模糊音补丁"},
    "pinyin.rerankUsesFuzzy": {"label": "重排使用模糊音"},
    "pinyin.pairs.zZh": {"label": "z / zh 模糊音"},
    "pinyin.pairs.cCh": {"label": "c / ch 模糊音"},
    "pinyin.pairs.sSh": {"label": "s / sh 模糊音"},
    "pinyin.pairs.enEng": {"label": "en / eng 模糊音"},
    "pinyin.pairs.inIng": {"label": "in / ing 模糊音"},
    "pinyin.pairs.ongOn": {"label": "on / ong 模糊音"},
    "pinyin.pairs.nL": {"label": "n / l 模糊音"},
    "pinyin.pairs.fH": {"label": "f / h 模糊音"},
}
_SETTING_DEFAULTS = flatten_settings(default_settings())
_SETTING_SCHEMA_FIELDS = {
    str(field.get("key") or ""): dict(field)
    for section in settings_schema().get("sections", [])
    if isinstance(section, Mapping)
    for field in section.get("fields", [])
    if isinstance(field, Mapping) and str(field.get("key") or "")
}


def _normalize_runtime_tool_args(
    tool: str,
    args: Mapping[str, object],
) -> dict[str, object]:
    normalized = dict(args)
    if tool != "memory" or str(normalized.get("op") or "").strip():
        return normalized

    query = str(normalized.get("query") or "").strip()
    scope = str(normalized.get("scope") or "").strip().lower()
    if not query:
        return normalized
    if scope == "recent":
        normalized["op"] = "recent"
        normalized.pop("scope", None)
        return normalized
    if scope in {"", "current", "historical", "change"}:
        normalized["op"] = "search"
        normalized.pop("scope", None)
        if scope:
            normalized.setdefault("mode", scope)
    return normalized


def _normalize_runtime_tool_call(
    tool: str,
    args: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    aliases = {
        str(projection["name"]): (target, str(projection["operation"]))
        for target, projections in _RUNTIME_TOOL_PROJECTIONS.items()
        if target.startswith("workspace_")
        for projection in projections
    }
    target = aliases.get(tool)
    if target is None:
        return tool, _normalize_runtime_tool_args(tool, args)

    gateway_tool, operation = target
    params = dict(args)
    if tool == "read":
        path = str(params.get("path") or "")
        resource_ref = params.get("resourceRef")
        if not resource_ref and re.match(r"^(?:artifact|media|room|skill)://", path):
            resource_ref = path
        if resource_ref:
            normalized = {
                "op": operation,
                "resourceRef": resource_ref,
                "offset": params.get("byteOffset"),
                "limit": params.get("byteLimit"),
            }
        elif params.get("selector"):
            normalized = {
                "op": operation,
                "path": params.get("path"),
                "selector": params.get("selector"),
                "selectorCursor": params.get("selectorCursor"),
                "lineLimit": params.get("limit"),
            }
        else:
            as_artifact = params.get("asArtifact")
            if as_artifact is None and Path(path).suffix.lower() in {".htm", ".html"}:
                as_artifact = True
            normalized = {
                "op": operation,
                "path": params.get("path"),
                "lineOffset": params.get("offset"),
                "lineLimit": params.get("limit"),
                "asArtifact": as_artifact,
            }
    elif tool == "grep":
        normalized = {
            "op": operation,
            "query": params.get("pattern"),
            "path": params.get("path"),
            "mode": "content",
            "patternKind": "literal" if params.get("literal") is True else "regex",
            "caseSensitive": params.get("ignoreCase") is not True,
            "glob": params.get("glob"),
            "context": params.get("context"),
            "limit": params.get("limit"),
        }
    elif tool == "find":
        normalized = {
            "op": operation,
            "query": params.get("pattern"),
            "path": params.get("path"),
            "mode": "name",
            "patternKind": "glob",
            "limit": params.get("limit"),
        }
    elif tool == "bash":
        normalized = {
            "op": operation,
            "command": params.get("command"),
            "cwd": params.get("cwd"),
            "timeoutSeconds": params.get("timeout"),
            "allowNetwork": params.get("allowNetwork"),
        }
    else:
        normalized = {"op": operation, **params}
    return gateway_tool, {
        key: value for key, value in normalized.items() if value is not None
    }


class TraceDiagnosticsService(Protocol):
    def trace_diagnostic_inspection(
        self,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def trace_optimization_read(self, report_id: str, payload: Mapping[str, object], *, session_id: str) -> Mapping[str, object]: ...

    def trace_optimization_command(self, report_id: str, payload: Mapping[str, object], *, session_id: str) -> Mapping[str, object]: ...


class ControlToolGateway:
    """Capability-scoped gateway over the existing control-plane services.

    Pi never receives a database handle. Each operation is an explicit adapter
    over the same management/core services used by the native control center.
    R1+ operations always create a preview with a payload hash. Legacy
    Sessions enforce preview hash/state fences; explicit unrestricted profiles
    retain approval, schemas, native applicability/atomic checks, receipts, and
    rollback.
    """

    def __init__(
        self,
        *,
        sessions: AgentSessionStore,
        management: ManagementService,
        core: object,
        project: str,
        facade: object | None = None,
        knowledge_client: object | None = None,
        knowledge_control: object | None = None,
        workspace_harness: WorkspaceHarness | None = None,
        background_jobs: AgentBackgroundJobService | None = None,
        delegation: object | None = None,
        collaboration: object | None = None,
        extensions: object | None = None,
        scheduling: object | None = None,
        configuration_store: object | None = None,
        governed_skills: object | None = None,
        browser_control: BrowserControlService | None = None,
        desktop_client: object | None = None,
        role_books: object | None = None,
        artifact_projector: AgentToolArtifactProjector | None = None,
        work_documents: object | None = None,
        lab_projects: object | None = None,
        sandbox_connector: object | None = None,
        trace_diagnostics: TraceDiagnosticsService | None = None,
        workflow_publisher: Callable[[str, str], object] | None = None,
        memory_enabled_provider: Callable[[], bool] | None = None,
    ) -> None:
        self.sessions = sessions
        self.management = management
        self.core = core
        self.project = project
        self.facade = facade
        self.knowledge_client = knowledge_client
        self.knowledge_control = knowledge_control
        self.workspace_harness = workspace_harness or WorkspaceHarness()
        self.background_jobs = background_jobs
        self.delegation = delegation
        self.collaboration = collaboration
        self.extensions = extensions
        self.configuration_store = configuration_store
        self.governed_skills = governed_skills
        self.scheduling = scheduling
        self.browser_control = browser_control
        self.desktop_client = desktop_client or DesktopBridgeClient()
        self.role_books = role_books
        self.artifact_projector = artifact_projector
        self.work_documents = work_documents
        self.lab_projects = lab_projects
        self.sandbox_connector = sandbox_connector
        self.trace_diagnostics = trace_diagnostics
        self.workflow_publisher = workflow_publisher
        sessions_db_path = getattr(sessions, "db_path", "")
        self._memory_enabled_provider = memory_enabled_provider or (
            lambda: memory_enabled_from_settings(sessions_db_path)
        )
        self._role_book_tool_adapter: AgentRoleBookToolAdapter | None = None
        self._memory_governance_store: MemoryGovernanceProposalStore | None = None
        self._desktop_cursor_lock = threading.Lock()
        self._desktop_snapshot_cursors: dict[tuple[str, str, int, int], str] = {}
        self._auto_approval_executor: (
            Callable[[Mapping[str, object]], Mapping[str, object]] | None
        ) = None

    def bind_auto_approval_executor(
        self,
        executor: Callable[[Mapping[str, object]], Mapping[str, object]],
    ) -> None:
        self._auto_approval_executor = executor

    def manifests(self, *, session_id: str = "") -> dict[str, object]:
        session = self.sessions.get(session_id) if session_id else None
        if session is not None:
            session = self._session_with_scenario_context(session)
        public_manifests = [
            manifest
            for manifest in self._manifest_items(
                session,
                include_runtime_projection=session is not None,
            )
            if _TOOL_SPEC_BY_ID[str(manifest["id"])].get("modelVisible") is not False
        ]
        return build_capability_catalog(
            tool_manifests=public_manifests,
            session=session,
            configuration_store=self.configuration_store,
            governed_skills=self.governed_skills,
            extensions=self.extensions,
        )

    def workspace_list(
        self,
        session_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Project the existing bounded WorkspaceHarness for the native file tree."""
        session = self.sessions.get(session_id)
        result = self.workspace_harness.list(session, dict(payload or {}))
        return {
            "schemaVersion": "rag-ime.agent-workspace-list.v1",
            "ok": True,
            "sessionId": session_id,
            **result,
        }

    def workspace_read(
        self,
        session_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Read a bounded UTF-8 workspace file for the native preview surface."""
        session = self.sessions.get(session_id)
        result = self.workspace_harness.read(session, dict(payload or {}))
        return {
            "schemaVersion": "rag-ime.agent-workspace-read.v1",
            "ok": True,
            "sessionId": session_id,
            **result,
            "requestedPath": str((payload or {}).get("path") or ""),
            "editability": self.workspace_harness.file_editability(session, str(result["path"])),
        }

    def workspace_save(
        self,
        session_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Explicit local Files edits share workspace authority without starting a model turn."""
        session = self.sessions.get(session_id)
        return {
            "schemaVersion": "rag-ime.agent-workspace-save.v1",
            "ok": True,
            "sessionId": session_id,
            **self.workspace_harness.save_file(session, dict(payload or {})),
        }

    def trace_optimization_capability_catalog(self) -> list[dict]:
        """Installed built-in capabilities; this catalog grants no Session access."""
        return [{"id": str(spec["id"]), "name": str(spec.get("displayName") or spec["id"]),
                 "kind": "tool", "status": "installed", "version": "control-center-v1",
                 "summary": str(spec.get("description") or ""),
                 "capabilityKeys": [str(spec["id"]), *[str(op) for op in spec.get("operations", ())]]}
                for spec in _TOOL_SPECS]

    def runtime_manifests(self, session: Mapping[str, object]) -> list[Mapping[str, object]]:
        # The runtime callback may retain a Prompt-time Session mapping. Reload
        # the durable policy immediately before disclosure so a Room workspace
        # lease cannot be widened by a stale workspace-managed grant.
        session_id = str(session.get("id") or "").strip()
        if session_id:
            session = self.sessions.get(session_id)
        session = self._session_with_scenario_context(session)
        manifest_items = self._manifest_items(session)
        capability_catalog = build_capability_catalog(
            tool_manifests=manifest_items,
            session=session,
            configuration_store=self.configuration_store,
            governed_skills=None,
            extensions=None,
        )
        disclosed_tools = {
            str(item["id"])
            for item in capability_catalog["items"]
            if item.get("kind") == "tool"
            and isinstance(item.get("disclosure"), Mapping)
            and item["disclosure"].get("effective") == "enabled"
        }
        active_room_facilitator = self._is_active_room_facilitator(session)
        manifests: list[Mapping[str, object]] = []
        for manifest in manifest_items:
            tool_id = str(manifest["id"])
            spec = _TOOL_SPEC_BY_ID[str(manifest["id"])]
            package_owned_connector = (
                tool_id == "sandbox"
                and self._managed_extension_enabled("vertical-agent-sandbox")
            )
            audited_goal_for_facilitator = (
                tool_id == "agent_goal" and active_room_facilitator
            )
            if (
                tool_id in PI_PACKAGE_OWNED_CONTROL_TOOL_IDS
                and not audited_goal_for_facilitator
            ):
                # Migration storage remains readable through the legacy
                # management APIs.  The native Package owns transcript-local
                # workflow state; only a real Room Facilitator also receives
                # the distinct evidence-audited Product Goal capability.
                continue
            if (
                spec.get("runtimeProjected") is False
                and not package_owned_connector
            ):
                # This target is registered and described by its Pi Package.
                # Keep it out of the resident extension before installation.
                # Once the Connector package is enabled, PAW projects the
                # Host-owned target so the long-lived multi-Session Runtime
                # never needs a process-global Session id or gateway token in
                # third-party extension code.
                continue
            if manifest.get("enabled") is not True or manifest["id"] not in disclosed_tools:
                continue
            operations = list(manifest.get("effectiveOperations") or [])
            projections = [
                copy.deepcopy(projection)
                for projection in _RUNTIME_TOOL_PROJECTIONS.get(
                    str(manifest["id"]),
                    (),
                )
                if projection["operation"] in operations
            ]
            # Pi owns the resident model-facing workspace names (ls/read/grep/
            # find/edit/write/bash). PAW registers only its hidden governed
            # workspace_* execution targets and declares which native name each
            # target projects. Passing a native name as a backend manifest would
            # collide with Pi's reserved tool namespace and prevent the Session
            # from opening before the first model request.
            parameter_schema = _runtime_tool_parameter_schema(
                str(manifest["id"]),
                operations,
            )
            item: dict[str, object] = {
                "name": manifest["id"],
                # The public card below is the routing authority. Keep the
                # Provider-required description compact instead of projecting
                # the same guidance a second time for every tool.
                "description": spec["displayName"],
                "parameters": parameter_schema,
                "when": list(spec["when"]),
                "notFor": list(spec["notFor"]),
                "input": spec["input"],
                "output": spec["output"],
                "does": spec["does"],
                "risk": manifest.get("riskLevel") or "R0",
                **(
                    {"alwaysAvailable": True}
                    if manifest.get("alwaysAvailable") is True
                    else {}
                ),
            }
            if (
                spec.get("modelVisible") is False
                and not audited_goal_for_facilitator
                and not package_owned_connector
            ):
                item["modelVisible"] = False
            if projections:
                item["runtimeProjections"] = [
                    {
                        "name": projection["name"],
                        "operation": projection["operation"],
                    }
                    for projection in projections
                ]
            manifests.append(item)
        structured_manifest_provider = getattr(
            self.delegation,
            "structured_output_manifest",
            None,
        )
        if callable(structured_manifest_provider) and session_id:
            structured_manifest = structured_manifest_provider(session_id)
            if isinstance(structured_manifest, Mapping):
                manifests.append(dict(structured_manifest))
        return manifests

    def _memory_enabled(self, session: Mapping[str, object] | None = None) -> bool:
        try:
            return bool(self._memory_enabled_provider()) and (
                session is None
                or capability_disclosure_enabled(
                    "tool:memory", session=session, configuration_store=self.configuration_store,
                )
            )
        except Exception:
            return False

    def _managed_extension_enabled(self, extension_id: str) -> bool:
        catalog_provider = getattr(self.extensions, "catalog", None)
        if not callable(catalog_provider):
            return False
        try:
            catalog = catalog_provider()
        except (RuntimeError, ValueError):
            return False
        items = catalog.get("items") if isinstance(catalog, Mapping) else None
        if not isinstance(items, list):
            return False
        return any(
            isinstance(item, Mapping)
            and str(item.get("id") or "") == extension_id
            and item.get("installed") is True
            and item.get("enabled") is True
            for item in items
        )

    def _is_active_room_facilitator(self, session: Mapping[str, object]) -> bool:
        if (
            str(session.get("sessionKind") or "") == "subagent_runtime"
            or str(session.get("mode") or "") != "coordinator"
        ):
            return False
        session_id = str(session.get("id") or "").strip()
        rooms = getattr(self.collaboration, "rooms", None)
        participant_for_session = getattr(rooms, "participant_for_session", None)
        if not session_id or not callable(participant_for_session):
            return False
        participant = participant_for_session(session_id, active_only=True)
        return (
            isinstance(participant, Mapping)
            and str(participant.get("collaborationRole") or "") == "coordinator"
        )

    def _room_participant_for_session(
        self,
        session: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        projected = session.get("roomParticipant")
        if isinstance(projected, Mapping):
            return projected
        rooms = getattr(self.collaboration, "rooms", None)
        participant_for_session = getattr(rooms, "participant_for_session", None)
        session_id = str(session.get("id") or "").strip()
        if not session_id or not callable(participant_for_session):
            return None
        try:
            participant = participant_for_session(session_id, active_only=False)
        except TypeError:
            participant = participant_for_session(session_id)
        return participant if isinstance(participant, Mapping) else None

    def _session_with_scenario_context(
        self,
        session: Mapping[str, object],
    ) -> dict[str, object]:
        participant = self._room_participant_for_session(session)
        if participant is None:
            return dict(session)
        return {
            **dict(session),
            "roomParticipant": dict(participant),
        }

    def _configured_scenario_policy(
        self,
        session: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        if not scenario_policy_applies(session):
            return None
        if self.configuration_store is None:
            return None
        snapshot = self.configuration_store.snapshot()
        configuration = snapshot.get("configuration") if isinstance(snapshot, Mapping) else None
        policies = configuration.get("scenarioPolicies") if isinstance(configuration, Mapping) else None
        if not isinstance(policies, Mapping):
            return None
        participant = isinstance(session.get("roomParticipant"), Mapping)
        scenario = scenario_policy_for_session(session, room_participant=participant)
        selected = policies.get(scenario.scenario_id)
        return selected if isinstance(selected, Mapping) else None

    def _lab_project(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        execute = getattr(self.lab_projects, "eval_lab_project_tool", None)
        if not callable(execute):
            raise ValueError("Lab project service is unavailable")
        return dict(execute(str(args.get("_sessionId") or ""), operation, args))

    def _lab_research(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        execute = getattr(self.lab_projects, 'eval_lab_app_research_tool', None)
        if not callable(execute):
            raise ValueError('Lab research service is unavailable')
        return dict(execute(str(args.get('_sessionId') or ''), operation, args))

    def _require_facilitator_root_work_document(
        self,
        session: Mapping[str, object],
        operation: str,
    ) -> None:
        if operation not in {"delegate", "delegate_batch"} or not self._is_active_room_facilitator(session):
            return
        goal = self.sessions.agent_goal(str(session.get("id") or ""))
        goal_id = str(goal.get("goalId") or "").strip()
        if goal.get("configured") is not True or str(goal.get("status") or "") != "active" or not goal_id:
            raise ValueError(
                "Act Gate blocked workspace mutation (Room delegation requires an active session Goal)"
            )
        # A Goal is still required before a Facilitator can dispatch work, but
        # the WorkDocument is an advisory audit trail. Its registration is a
        # best-effort, one-shot side effect of the actual workspace write; a
        # stale/archived document must never turn delegation into a retry loop.

    def _manifest_items(
        self,
        session: Mapping[str, object] | None,
        *,
        include_runtime_projection: bool = False,
    ) -> list[dict[str, object]]:
        manifests = []
        session = self._session_with_scenario_context(session) if session is not None else None
        configured_policy = (
            self._configured_scenario_policy(session) if session is not None else None
        )
        scenario_policy = (
            scenario_policy_for_session(session, configured_policy=configured_policy)
            if session is not None and scenario_policy_applies(session)
            else None
        )
        room_participant = (
            isinstance(session.get("roomParticipant"), Mapping)
            if session is not None
            else False
        )
        for spec in _TOOL_SPECS:
            operations = list(spec["operations"])
            operation_risks = {
                operation: str(dict(spec.get("operationRisks") or {}).get(operation) or "R0")
                for operation in operations
            }
            available = (
                (
                    str(spec["id"]) != "workspace_job"
                    or (
                        self.background_jobs is not None
                        and self.background_jobs.execution_owner
                    )
                )
                and (
                    str(spec["id"]) != "sandbox"
                    or self.sandbox_connector is not None
                )
                and (
                    str(spec["id"]) not in {"lab_project", "lab_research"}
                    or self.lab_projects is not None
                )
            )
            if str(spec["id"]) == "room_partner":
                room_store = getattr(self.collaboration, "rooms", None)
                participant_for_session = getattr(
                    room_store,
                    "participant_for_session",
                    None,
                )
                participant = (
                    participant_for_session(
                        str(session.get("id") or ""),
                        active_only=True,
                    )
                    if callable(participant_for_session)
                    and isinstance(session, Mapping)
                    and str(session.get("id") or "")
                    else None
                )
                available = participant is not None
            manifest = {
                "schemaVersion": "rag-ime.control-tool-manifest.v1",
                "id": spec["id"],
                "domain": spec["domain"],
                "displayName": spec["displayName"],
                "description": spec["description"],
                "category": spec["domain"],
                "riskLevel": _highest_risk(operation_risks.values()),
                "sessionModes": list(spec.get("sessionModes") or ("assistant", "coordinator")),
                "operations": operations,
                "operationRisks": operation_risks,
                "resultPresentation": spec["resultPresentation"],
                "availability": "online" if available else "offline",
                "version": "1",
                **(
                    {"alwaysAvailable": True}
                    if spec.get("alwaysAvailable") is True
                    else {}
                ),
            }
            validate_contract(manifest, "control-tool-manifest.v1.json")
            if session is not None:
                mode_compatible = _tool_mode_compatible(session, spec)
                manifest["profileOperations"] = {
                    profile: [
                        operation
                        for operation in operations
                        if _tool_profile_allows(
                            {
                                **session,
                                "toolProfileVersion": profile,
                                "toolAllowlistMode": "profile",
                                "allowedTools": [],
                            },
                            tool=str(spec["id"]),
                            operation=operation,
                            spec=spec,
                        )
                        and (
                            scenario_policy is None
                            or operation
                            in scenario_tool_operations(
                                session,
                                str(spec["id"]),
                                operations,
                                room_participant=room_participant,
                                configured_policy=configured_policy,
                            )
                        )
                    ]
                    for profile in (
                        "control-center-v1",
                        "subagent-readonly-v1",
                        DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
                        FULL_ACCESS_TOOL_PROFILE,
                    )
                }
                effective_operations = [
                    operation
                    for operation in operations
                    if mode_compatible
                    and _tool_profile_allows(
                        session,
                        tool=str(spec["id"]),
                        operation=operation,
                        spec=spec,
                    )
                ]
                if scenario_policy is not None:
                    effective_operations = scenario_tool_operations(
                        session,
                        str(spec["id"]),
                        effective_operations,
                        room_participant=room_participant,
                        configured_policy=configured_policy,
                    )
                if str(spec["id"]) == "memory" and not self._memory_enabled(session):
                    # Keep the public capability card available so the UI can
                    # explain that Memory is off, but expose no executable
                    # operation to a model/runtime manifest.
                    effective_operations = []
                if str(spec["id"]) == "lab_project" and not (
                    session.get("surfaceKind") == "extension_app"
                    and session.get("ownerAppId") == "extension:agent-lab"
                    and str(session.get("surfaceKey") or "").startswith("project.")
                    and str(session.get("surfaceKey") or "").endswith(".guide")
                ):
                    effective_operations = []
                if str(spec['id']) == 'lab_research' and not (
                    session.get('surfaceKind') == 'extension_app'
                    and str(session.get('ownerAppId') or '').startswith('extension:lab-')
                    and str(session.get('surfaceKey') or '').startswith('application.')
                ):
                    effective_operations = []
                # A coordinator without a selected directory must not expose
                # filesystem tools to Pi. The control center still lists the
                # capability and can prompt for a directory, but the runtime
                # cannot turn a missing scope into a failing Tool call.
                if str(spec["id"]) in _WORKSPACE_TOOLS and not session.get(
                    "workspaceRoots"
                ):
                    effective_operations = []
                if not available:
                    effective_operations = []
                manifest["enabled"] = bool(effective_operations)
                manifest["effectiveOperations"] = effective_operations
                manifest["scenario"] = scenario_policy.scenario_id if scenario_policy else ""
                manifest["scenarioVariant"] = scenario_policy.variant if scenario_policy else ""
                manifest["scenarioAllowed"] = bool(
                    scenario_policy is None
                    or str(spec["id"]) in scenario_policy.tool_ids
                )
                if scenario_policy is not None and not manifest["scenarioAllowed"]:
                    manifest["scenarioReason"] = "scenario_policy"
                manifest["explicitlyAllowed"] = (
                    str(session.get("toolAllowlistMode") or "profile") != "explicit"
                    or str(spec["id"])
                    in {str(value) for value in session.get("allowedTools") or []}
                )
                if (
                    include_runtime_projection
                    and str(spec["id"]) == "workspace_lsp"
                    and str(session.get("mode") or "") == "coordinator"
                ):
                    manifest["runtimeProjection"] = self.workspace_harness.lsp_status(
                        session,
                        {},
                    )
            manifests.append(manifest)
        return manifests

    def execute(self, payload: Mapping[str, object]) -> dict[str, object]:
        request = dict(payload)
        validate_contract(request, "agent-tool-call.v1.json")
        session_id = str(request["sessionId"])
        session = self.sessions.get(session_id)
        if session.get("status") == "archived":
            raise ValueError("archived sessions cannot execute tools")
        session = self._session_with_scenario_context(session)
        tool = str(request["tool"])
        raw_args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
        tool, args = _normalize_runtime_tool_call(tool, raw_args)
        if tool == "memory" and not self._memory_enabled(session):
            raise ValueError("memory tool is disabled for this session or by settings.memory.enabled")
        if tool == "structured_output":
            submit = getattr(self.delegation, "submit_structured_output", None)
            if not callable(submit):
                raise ValueError("structured output is unavailable")
            result = submit(
                session_id,
                args,
                tool_call_id=str(request["toolCallId"]),
            )
            return {"ok": True, "result": dict(result)}
        if tool == "room_partner":
            room_spec = _TOOL_SPEC_BY_ID[tool]
            effective_session = self._session_with_scenario_context(session)
            if scenario_policy_applies(effective_session) and str(raw_args.get("op") or "") not in scenario_tool_operations(
                effective_session,
                tool,
                room_spec["operations"],
                configured_policy=self._configured_scenario_policy(effective_session),
            ):
                raise ValueError("tool is not disclosed for this Agent scenario")
            if self.collaboration is None:
                raise ValueError("managed room collaboration is unavailable")
            self._require_facilitator_root_work_document(
                session,
                str(args.get("op") or ""),
            )
            result = self.collaboration.execute_room_partner_tool(  # type: ignore[attr-defined]
                session_id,
                args,
                tool_call_id=str(request["toolCallId"]),
                source_loop_id=str(request.get("sourceLoopId") or ""),
            )
            return {"ok": True, "result": dict(result)}
        spec = _TOOL_SPEC_BY_ID.get(tool)
        if spec is None:
            raise ValueError("tool is not enabled for this session")
        if not _tool_mode_compatible(session, spec):
            raise ValueError("tool is not enabled for this session mode")
        operation = str(args.get("op") or "")
        if operation not in spec["operations"]:
            raise ValueError(f"unsupported {tool} operation")
        effective_session = self._session_with_scenario_context(session)
        if scenario_policy_applies(effective_session) and operation not in scenario_tool_operations(
            effective_session,
            tool,
            spec["operations"],
            configured_policy=self._configured_scenario_policy(effective_session),
        ):
            raise ValueError("tool is not disclosed for this Agent scenario")
        if read_only_policy_active(session) and read_only_blocks_effect(
            tool,
            operation,
        ):
            raise ValueError(
                "workspace mutation is blocked by the active read-only policy"
            )

        response = self._execute_product_tool(
            request=request,
            session=session,
            tool=tool,
            args=args,
            spec=spec,
            operation=operation,
        )
        validate_contract(response, "agent-tool-result.v1.json")
        return response

    def _execute_product_tool(
        self,
        *,
        request: Mapping[str, object],
        session: Mapping[str, object],
        tool: str,
        args: Mapping[str, object],
        spec: Mapping[str, object],
        operation: str,
    ) -> dict[str, object]:
        session_id = str(session["id"])
        # Re-read immediately before authorization/approval so a waiting Room
        # Dispatch cannot apply a mutation after its workspace lease becomes
        # read-only.
        fresh_session = self.sessions.get(session_id)
        if isinstance(session.get("roomParticipant"), Mapping):
            fresh_session = {
                **fresh_session,
                "roomParticipant": dict(session["roomParticipant"]),
            }
        session = self._session_with_scenario_context(fresh_session)
        if tool == "memory" and not self._memory_enabled(session):
            raise ValueError("memory tool is disabled for this session or by settings.memory.enabled")
        # A live Room dispatch is the runtime confirmation boundary for the
        # Room-only unrestricted overlay. Keep it in-memory for this Tool
        # call as well, so a legacy/old row created before migration 0164 does
        # not silently fall back to per-action approval during a live dispatch.
        # The overlay never changes the persisted Session mode or workspace
        # lease; ``approval_strategy`` still enforces both and hard fences.
        room_dispatch_context = self._room_dispatch_context(session_id)
        room_dispatch_authorized = (
            not read_only_policy_active(session)
            and room_dispatch_context is not None
        )
        session = {
            **session,
            # This capability is deliberately ephemeral. A persisted Room
            # overlay may describe membership/defaults, but only this exact
            # live dispatch can authorize the current Tool call.
            "roomDispatchAuthorized": room_dispatch_authorized,
        }
        if (
            room_dispatch_authorized
            and str(session.get("roomExecutionMode") or "").strip().lower()
            != ROOM_UNRESTRICTED_EXECUTION_MODE
        ):
            session = {
                **session,
                "roomExecutionMode": ROOM_UNRESTRICTED_EXECUTION_MODE,
            }
        if read_only_policy_active(session) and read_only_blocks_effect(
            tool,
            operation,
        ):
            raise ValueError(
                "workspace mutation is blocked by the active read-only policy"
            )
        spec = _TOOL_SPEC_BY_ID.get(tool)
        if spec is None or (
            scenario_policy_applies(session)
            and operation not in scenario_tool_operations(
                session,
                tool,
                spec["operations"],
                configured_policy=self._configured_scenario_policy(session),
            )
        ):
            raise ValueError("tool is not disclosed for this Agent scenario")
        if not _tool_profile_allows(session, tool=tool, operation=operation, spec=spec):
            raise ValueError("tool operation is not enabled for this session tool profile")
        read_only_validation_command = (
            read_only_policy_active(session)
            and (tool, operation) == ("workspace_shell", "run")
        )
        if (tool, operation) in {
            ("workspace_edit", "apply"),
            ("workspace_patch", "apply"),
            ("workspace_shell", "run"),
            ("workspace_job", "start"),
            ("workspace_write", "apply"),
            ("workspace_lsp", "rename"),
            ("workspace_lsp", "code_action_apply"),
        } and not read_only_validation_command:
            # A preview request is still planning. Prove Act is open here, but
            # transition to executing only when an approved write is applied.
            self.sessions.require_workspace_act(
                session_id,
                room_dispatch_authorized=self._room_dispatch_authorized(
                    session_id
                ),
            )
        handlers = {
            "overview": self._overview,
            "input": self._input,
            "voice": self._voice,
            "planning": self._planning,
            "agent_schedule": self._agent_schedule,
            "memory": self._memory,
            "agent_role_book": self._role_book,
            "knowledge": self._knowledge,
            "models": self._models,
            "runtime": self._runtime,
            "configuration": self._configuration,
            "agents": self._agents,
            "session_search": self._session_search,
            "trace_diagnostics": self._trace_diagnostics,
            "browser": self._browser,
            "todo": self._todo,
            "agent_goal": self._agent_goal,
            "plugins": self._plugins,
            "sandbox": self._sandbox,
            "work_documents": self._work_documents,
            "lab_project": self._lab_project,
            "lab_research": self._lab_research,
        }
        risk_level = str(dict(spec.get("operationRisks") or {}).get(operation) or "R0")
        if read_only_validation_command:
            prepared = self.workspace_harness.prepare_command(
                session,
                args,
            )
            result = self.workspace_harness.execute(prepared)
        elif tool == "work_documents":
            handler_args = dict(args)
            handler_args["_sessionId"] = session_id
            result = handlers[tool](operation, handler_args)
        elif risk_level == "R0":
            if tool == "workspace_list":
                result = self.workspace_harness.list(session, args)
            elif tool == "workspace_read":
                if str(args.get("resourceRef") or "").strip():
                    result = self._read_internal_resource(session_id, args)
                else:
                    result = self.workspace_harness.read(session, args)
                    if args.get("asArtifact") is True and self.artifact_projector is not None:
                        projection = self.artifact_projector.project_workspace_read(
                            session=session,
                            receipt=result,
                        )
                        result.update(projection.receipt_fields())
            elif tool == "workspace_search":
                result = self.workspace_harness.search(session, args)
            elif tool == "workspace_lsp":
                result = (
                    self.workspace_harness.lsp_status(session, args)
                    if operation == "status"
                    else self.workspace_harness.lsp_read(session, operation, args)
                )
            elif tool == "workspace_job":
                result = self._background_job(
                    session_id,
                    operation,
                    args,
                )
            else:
                handler_args = dict(args)
                handler_args["_sessionId"] = session_id
                runtime_context = request.get("runtimeContext")
                if tool in {"agents", "lab_research"}:
                    handler_args["_toolCallId"] = str(request["toolCallId"])
                if tool == "agents":
                    handler_args["_loadReceiptId"] = str(request.get("loadReceiptId") or "")
                if tool == "agents" and operation == "delegate":
                    if isinstance(runtime_context, Mapping):
                        handler_args["_runtimeContext"] = dict(runtime_context)
                if tool == "desktop_semantic":
                    result = self._desktop(operation, handler_args)
                else:
                    result = handlers[tool](operation, handler_args)
        else:
            strategy = approval_strategy(
                session,
                tool=tool,
                operation=operation,
                preview=args,
                risk_level=risk_level,
            )
            if strategy == APPROVAL_DENY:
                # A denied operation must not leave a pending approval behind.
                # Read-only and missing workspace scope are hard runtime fences.
                if str(session.get("executionMode") or "") == "read_only":
                    raise ValueError(
                        "write and Shell operations are blocked in read-only mode"
                    )
                raise ValueError(
                    "workspace operations require a previously authorized scope"
                )
            with self.sessions.approval_creation_scope(
                session_id=session_id,
                tool_call_id=str(request["toolCallId"]),
                room_context=(
                    room_dispatch_context
                    if room_dispatch_authorized
                    else None
                ),
            ):
                result = self._prepare_approval(
                    session_id=session_id,
                    tool=tool,
                    operation=operation,
                    args=args,
                    risk_level=risk_level,
                )
            approval = (
                result.get("approval")
                if isinstance(result.get("approval"), Mapping)
                else None
            )
            if approval is None:
                raise ValueError("approval preparation returned no approval")
            result = dict(result)
            result["approval"] = self.sessions.bind_approval_tool_call(
                str(approval.get("approvalId") or ""),
                tool_call_id=str(request["toolCallId"]),
            )
            if room_dispatch_authorized:
                result["approval"] = self.sessions.bind_approval_room_dispatch(
                    str(approval.get("approvalId") or ""),
                    room_context=room_dispatch_context,
                )
            approval = result["approval"]
            if strategy in {APPROVAL_AUTO, APPROVAL_MODEL}:
                if self._auto_approval_executor is None:
                    raise ValueError("unattended approval bridge is unavailable")
                result = dict(self._auto_approval_executor(approval))
        response = {
            "schemaVersion": "rag-ime.agent-tool-result.v1",
            "ok": True,
            "tool": tool,
            "operation": operation,
            "result": result,
        }
        return response

    def _room_dispatch_authorized(self, session_id: str) -> bool:
        """Let an active Room Session use the ordinary Session Act gate.

        Room no longer issues a second per-Tool capability receipt. The Room
        adapter only proves that this Session currently owns a live dispatch;
        the Session execution mode, workspace scope and approval strategy stay
        authoritative for every actual Tool call.
        """

        return self._room_dispatch_context(session_id) is not None

    def _room_dispatch_context(
        self,
        session_id: str,
    ) -> Mapping[str, object] | None:
        inspect = getattr(
            self.collaboration,
            "_active_room_dispatch_context",
            None,
        )
        if not callable(inspect):
            return None
        context = inspect(session_id)
        if not isinstance(context, Mapping):
            return None
        room_id = str(context.get("roomId") or "").strip()
        root_id = str(context.get("rootId") or "").strip()
        dispatch_id = str(context.get("dispatchId") or "").strip()
        try:
            generation = int(context.get("generation") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Room dispatch context is incomplete") from exc
        if not room_id or not root_id or not dispatch_id or generation <= 0:
            # A malformed Mapping is not an ordinary Session. Failing before
            # approval preparation prevents an orphan pending approval from
            # surfacing as a false human/model decision in the Room.
            raise ValueError("Room dispatch context is incomplete")
        return dict(context)

    def _read_internal_resource(
        self,
        session_id: str,
        args: Mapping[str, object],
    ) -> dict[str, object]:
        resource_ref = _bounded_text(args.get("resourceRef"), maximum=1_024)
        parsed = urlsplit(resource_ref)
        if (
            parsed.scheme not in {"artifact", "media", "room", "skill"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "resourceRef must use artifact://, media://, room://, or skill://"
            )
        resource_id = unquote(f"{parsed.netloc}{parsed.path}").strip("/")
        if not resource_id or "/" in resource_id or "\x00" in resource_id:
            raise ValueError("resourceRef identifier is invalid")
        metadata: dict[str, object]
        if parsed.scheme == "artifact":
            inspect = getattr(self.delegation, "inspect_artifact", None)
            if not callable(inspect):
                raise ValueError("delegated artifact reader is unavailable")
            payload = inspect(session_id, resource_id, limit=100)
            metadata = {"owner": "AgentDelegationCoordinator"}
            content = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        elif parsed.scheme == "media":
            reader = getattr(self.collaboration, "read_media_resource", None)
            if not callable(reader):
                raise ValueError("managed media reader is unavailable")
            receipt, raw = reader(resource_id, session_id=session_id)
            metadata = {"owner": "AgentMediaStore", "media": receipt}
            if str(receipt.get("mimeType") or "").startswith("text/"):
                content = raw.decode("utf-8")
            else:
                content = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2)
        elif parsed.scheme == "skill":
            load_exact = getattr(self.governed_skills, "load_exact", None)
            if not callable(load_exact):
                raise ValueError("governed Skill reader is unavailable")
            payload = dict(load_exact(resource_id))
            content = str(payload.pop("body", ""))
            metadata = {"owner": "SessionSkillRegistry", **payload}
        else:
            rooms = getattr(self.collaboration, "rooms", None)
            participant_for_session = getattr(rooms, "participant_for_session", None)
            participant = (
                participant_for_session(session_id)
                if callable(participant_for_session)
                else None
            )
            if (
                not isinstance(participant, Mapping)
                or str(participant.get("roomId") or "") != resource_id
            ):
                raise ValueError("Room resource does not belong to this Session")
            snapshot = getattr(self.collaboration, "room_snapshot", None)
            if not callable(snapshot):
                raise ValueError("Room snapshot reader is unavailable")
            payload = snapshot(resource_id)
            metadata = {"owner": "AgentRoomService"}
            content = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        offset = _bounded_int(
            args.get("offset"),
            default=0,
            minimum=0,
            maximum=50_000_000,
        )
        requested_limit = _bounded_int(
            args.get("limit"),
            default=32_768,
            minimum=1,
            maximum=65_536,
        )
        encoded = content.encode("utf-8")
        if offset > len(encoded):
            raise ValueError(
                f"resource offset {offset} is beyond end of content ({len(encoded)} bytes)"
            )
        if offset and offset < len(encoded) and 0x80 <= encoded[offset] <= 0xBF:
            raise ValueError("resource offset must be a UTF-8 character boundary")
        end = min(len(encoded), offset + min(requested_limit, 40 * 1024))
        while end > offset:
            try:
                visible = encoded[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            visible = ""
        next_offset = end if end < len(encoded) else None
        return {
            "summary": f"已读取受管资源 {resource_ref}",
            "resourceRef": resource_ref,
            "resourceKind": parsed.scheme,
            "resourceId": resource_id,
            "metadata": metadata,
            "content": visible,
            "offset": offset,
            "nextOffset": next_offset,
            "truncated": next_offset is not None,
            "contentBytes": len(visible.encode("utf-8")),
            "size": len(encoded),
            "resourceRevision": hashlib.sha256(encoded).hexdigest(),
        }

    def _plugins(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        if self.extensions is None:
            raise ValueError("managed plugin lifecycle is unavailable")
        if operation == "catalog":
            return dict(self.extensions.catalog())  # type: ignore[attr-defined]
        if operation == "list":
            return dict(self.extensions.list())  # type: ignore[attr-defined]
        if operation == "create_package":
            return dict(self.extensions.create_package_draft(args))  # type: ignore[attr-defined]
        if operation == "validate":
            return dict(self.extensions.validate(args))  # type: ignore[attr-defined]
        proposal_actions = {
            "propose_install": "install",
            "propose_enable": "enable",
            "propose_disable": "disable",
            "propose_update": "update",
            "propose_rollback": "rollback",
            "propose_uninstall": "uninstall",
        }
        action = proposal_actions.get(operation)
        if action is not None:
            preview: dict[str, object] = {"action": action}
            if action in {"install", "update"}:
                preview.update(
                    {
                        "validationToken": args.get("validationToken"),
                        "enable": args.get("enable") is True,
                    }
                )
            else:
                preview["pluginId"] = args.get("pluginId")
            return dict(self.extensions.preview(preview))  # type: ignore[attr-defined]
        raise ValueError("unsupported plugins operation")

    def _sandbox(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        if self.sandbox_connector is None:
            raise ValueError("vertical Agent sandbox connector is unavailable")
        execute = getattr(self.sandbox_connector, "execute", None)
        if not callable(execute):
            raise ValueError("vertical Agent sandbox connector is invalid")
        session_id = str(args.get("_sessionId") or "").strip()
        result = execute(session_id, operation, args)
        if not isinstance(result, Mapping):
            raise ValueError("vertical Agent sandbox connector returned an invalid result")
        return dict(result)

    def _browser(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        service = self.browser_control
        if service is None:
            raise ValueError("PAW Browser is unavailable")
        if operation == "status":
            return service.status(agent_safe=True)
        if operation == "tabs":
            return service.tabs()
        if operation == "snapshot":
            result = service.latest_snapshot(
                device_id=_bounded_text(args.get("deviceId"), maximum=160),
                tab_id=(
                    _bounded_int(args.get("tabId"), default=0, minimum=0, maximum=2_147_483_647)
                    or None
                ),
                include_markdown=True,
            )
            maximum = _bounded_int(
                args.get("maxChars"),
                default=24_000,
                minimum=1_000,
                maximum=80_000,
            )
            markdown = str(result.get("markdown") or "")
            result["markdown"] = markdown[:maximum]
            result["truncated"] = len(markdown) > maximum
            result["untrustedData"] = True
            return result
        if operation == "trace":
            return service.traces(
                limit=_bounded_int(args.get("limit"), default=20, minimum=1, maximum=100)
            )
        if operation == "screenshot":
            return service.submit_command(
                "screenshot",
                args,
                session_id=_bounded_text(args.get("_sessionId"), maximum=240),
                timeout_seconds=20.0,
            )
        if operation == "stop":
            return service.stop()
        if operation == "run":
            return service.submit_command(
                operation,
                args,
                session_id=_bounded_text(args.get("_sessionId"), maximum=240),
                timeout_seconds=125.0,
            )
        if operation in {
            "navigate",
            "back",
            "forward",
            "click",
            "type",
            "scroll",
            "wait",
        }:
            return service.submit_command(
                operation,
                args,
                session_id=_bounded_text(args.get("_sessionId"), maximum=240),
                timeout_seconds=25.0 if operation in {"navigate", "wait"} else 15.0,
            )
        raise ValueError(f"unsupported browser operation: {operation}")

    def _desktop(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        if operation == "status":
            return dict(self.desktop_client.status())  # type: ignore[attr-defined]
        if operation == "list":
            return dict(
                self.desktop_client.list_applications(  # type: ignore[attr-defined]
                    include_background=args.get("includeBackground") is True,
                )
            )
        if operation == "inspect":
            session_id = _bounded_text(args.get("_sessionId"), maximum=240)
            bundle_id = _bounded_text(args.get("bundleId"), maximum=300)
            pid = _bounded_int(args.get("pid"), default=0, minimum=0, maximum=2_147_483_647)
            query = _bounded_text(args.get("query"), maximum=300)
            max_nodes = _bounded_int(args.get("maxNodes"), default=160, minimum=1, maximum=500)
            max_depth = _bounded_int(args.get("maxDepth"), default=8, minimum=1, maximum=12)
            target = bundle_id or (f"pid:{pid}" if pid > 0 else "frontmost")
            cursor_key = (session_id, target, max_nodes, max_depth)
            since_snapshot_id = _bounded_text(args.get("sinceSnapshotId"), maximum=200)
            if not since_snapshot_id and not query and args.get("incremental") is not False:
                with self._desktop_cursor_lock:
                    since_snapshot_id = self._desktop_snapshot_cursors.get(cursor_key, "")
            result = dict(
                self.desktop_client.inspect(  # type: ignore[attr-defined]
                    bundle_id=bundle_id,
                    pid=pid,
                    query=query,
                    max_nodes=max_nodes,
                    max_depth=max_depth,
                    since_snapshot_id=since_snapshot_id,
                )
            )
            snapshot_id = _bounded_text(result.get("snapshotId"), maximum=200)
            if snapshot_id and not query:
                with self._desktop_cursor_lock:
                    self._desktop_snapshot_cursors[cursor_key] = snapshot_id
                    while len(self._desktop_snapshot_cursors) > 256:
                        self._desktop_snapshot_cursors.pop(
                            next(iter(self._desktop_snapshot_cursors))
                        )
            return result
        if operation == "find":
            return dict(self._find_desktop_target(args))
        raise ValueError("unsupported desktop_semantic operation")

    def _find_desktop_target(self, args: Mapping[str, object]) -> dict[str, object]:
        match = args.get("match") if isinstance(args.get("match"), Mapping) else {}
        result = self.desktop_client.find(  # type: ignore[attr-defined]
            bundle_id=_bounded_text(args.get("bundleId"), maximum=300),
            pid=_bounded_int(args.get("pid"), default=0, minimum=0, maximum=2_147_483_647),
            role=_bounded_text(match.get("role"), maximum=80),
            subrole=_bounded_text(match.get("subrole"), maximum=80),
            identifier=_bounded_text(match.get("identifier"), maximum=240),
            label=_bounded_text(match.get("label"), maximum=240),
            query=_bounded_text(match.get("query"), maximum=300),
            limit=_bounded_int(args.get("limit"), default=5, minimum=1, maximum=20),
            max_nodes=_bounded_int(args.get("maxNodes"), default=500, minimum=1, maximum=500),
            max_depth=_bounded_int(args.get("maxDepth"), default=12, minimum=1, maximum=12),
        )
        return dict(result) if isinstance(result, Mapping) else {}

    def _agents(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        if self.delegation is None:
            raise ValueError("managed delegation is unavailable")
        session_id = _bounded_text(args.get("_sessionId"), maximum=240)
        if not session_id:
            raise ValueError("agent collaboration session is missing")
        if operation == "catalog":
            return dict(self.delegation.catalog())  # type: ignore[attr-defined]
        if operation == "delegate":
            return dict(self.delegation.delegate(session_id, args))  # type: ignore[attr-defined]
        if operation == "status":
            return dict(self.delegation.status(session_id, args))  # type: ignore[attr-defined]
        if operation == "call":
            return dict(self.delegation.call(session_id, args))  # type: ignore[attr-defined]
        if operation == "artifact":
            return dict(
                self.delegation.inspect_artifact(  # type: ignore[attr-defined]
                    session_id,
                    _bounded_text(args.get("artifactId"), maximum=240),
                    limit=_bounded_int(args.get("limit"), default=50, minimum=1, maximum=500),
                )
            )
        if operation == "abort":
            return dict(self.delegation.abort(session_id, args))  # type: ignore[attr-defined]
        raise ValueError("unsupported agents operation")

    def _session_search(
        self,
        operation: str,
        args: Mapping[str, object],
    ) -> dict[str, object]:
        if operation != "search":
            raise ValueError("unsupported session_search operation")
        requester_session_id = _bounded_text(args.get("_sessionId"), maximum=240)
        anchors = self.sessions.search_history(
            query=_bounded_text(args.get("query"), maximum=240),
            requester_session_id=requester_session_id,
            include_archived=args.get("includeArchived") is True,
            limit=_bounded_int(args.get("limit"), default=12, minimum=1, maximum=50),
        )
        query = _bounded_text(args.get("query"), maximum=240)
        return {
            "summary": (
                f"找到 {len(anchors)} 个 Session 历史锚点"
                if query
                else f"返回最近 {len(anchors)} 个 Session 历史锚点"
            ),
            "presentationKind": "session_history",
            "query": query,
            "anchors": anchors,
            "privacyBoundary": (
                "仅包含标题、已脱敏摘要与导航引用；不返回原始 transcript、"
                "工具输出、绝对路径或私密思维。"
            ),
        }

    def _trace_diagnostics(
        self,
        operation: str,
        args: Mapping[str, object],
    ) -> dict[str, object]:
        service = self.trace_diagnostics
        if service is None:
            raise ValueError("Trace diagnostics service is unavailable")
        session_id = _bounded_text(args.get("_sessionId"), maximum=240)
        report_id = _bounded_text(args.get("reportId"), maximum=240)
        if operation in {"read", "command"} and not report_id:
            raise ValueError("Trace optimization requires reportId")
        if operation == "inspect":
            targets = _strict_trace_diagnostic_targets(args.get("targets"))
            payload = service.trace_diagnostic_inspection({"targets": targets, "_sessionId": session_id})
        elif operation == "read":
            payload = service.trace_optimization_read(
                report_id,
                {key: args[key] for key in ("query", "patternId", "revision", "offset") if key in args},
                session_id=session_id,
            )
        elif operation == "command":
            payload = service.trace_optimization_command(
                report_id,
                {key: args[key] for key in ("command", "clientRequestId", "input", "candidateId") if key in args},
                session_id=session_id,
            )
        else:
            raise ValueError("unsupported trace_diagnostics operation")
        if not isinstance(payload, Mapping):
            raise ValueError("Trace diagnostics inspector returned an invalid payload")
        return dict(payload)

    def _todo(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        session_id = _bounded_text(args.get("_sessionId"), maximum=240)
        if not session_id:
            raise ValueError("Todo session is missing")
        mutation: dict[str, object] = {"op": operation}
        for key in ("list", "items", "phase", "task", "reason"):
            if key in args:
                mutation[key] = args[key]
        result = self.sessions.mutate_agent_todo(
            session_id,
            mutation,
            actor="agent-runtime",
        )
        todo = (
            dict(result["todo"])
            if isinstance(result.get("todo"), Mapping)
            else self.sessions.agent_todo(session_id)
        )
        if operation != "view":
            self._publish_workflow(session_id, f"todo:{operation}")
            if self.work_documents is not None:
                try:
                    self.work_documents.observe_authority(  # type: ignore[attr-defined,union-attr]
                        "session_todo",
                        session_id,
                    )
                except Exception:
                    # The observer persists its own retry record. The Todo
                    # mutation is already durable and must not be replayed.
                    pass
        counts = todo.get("counts") if isinstance(todo.get("counts"), Mapping) else {}
        total = _safe_int(counts.get("total"))
        if total == 0:
            summary = (
                "Todo list is empty."
                if operation == "view"
                else "Todo list cleared."
            )
        else:
            summary = (
                f"Todo：{_safe_int(counts.get('completed'))}/{total} 已完成，"
                f"{_safe_int(counts.get('inProgress'))} 进行中，"
                f"{_safe_int(counts.get('pending'))} 待处理，"
                f"{_safe_int(counts.get('blocked'))} 已阻塞，"
                f"{_safe_int(counts.get('abandoned'))} 已放弃"
            )
        completed_tasks = [
            dict(item)
            for item in result.get("completedTasks", [])
            if isinstance(item, Mapping)
        ]
        return {
            "summary": summary,
            "presentationKind": "todo",
            "todo": todo,
            "phases": list(todo.get("phases") or []),
            "storage": str(result.get("storage") or "session"),
            "completedTasks": completed_tasks,
            **(
                {"event": dict(result["event"])}
                if isinstance(result.get("event"), Mapping)
                else {}
            ),
        }

    def _agent_goal(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        session_id = _bounded_text(args.get("_sessionId"), maximum=240)
        if not session_id:
            raise ValueError("agent goal session is missing")
        current = self.sessions.agent_goal(session_id)
        if operation == "list":
            configured = current.get("configured") is True
            return {
                "summary": (
                    f"当前长期目标状态为 {current.get('status', '')}"
                    if configured
                    else "当前 Session 尚未配置长期目标"
                ),
                "presentationKind": "goal",
                "goal": current,
            }
        if operation not in {
            "confirm_setup",
            "update",
            "pause",
            "resume",
            "complete",
            "cancel",
        }:
            raise ValueError("unsupported agent_goal operation")
        if operation == "complete":
            self._require_room_terminal_result(session_id)
        payload: dict[str, object] = {
            "action": operation,
            "expectedRevision": _safe_int(current.get("revision")),
        }
        for field in (
            "confirmed",
            "objective",
            "successCriteria",
            "evidenceExpectations",
            "tokenBudget",
            "timeBudgetMs",
            "summary",
            "evidence",
            "reason",
        ):
            if field in args:
                payload[field] = args[field]
        result = self.sessions.mutate_agent_goal(
            session_id,
            payload,
            actor="agent-runtime",
        )
        workflow = (
            result["workflow"]
            if isinstance(result.get("workflow"), Mapping)
            else {}
        )
        goal = workflow["goal"] if isinstance(workflow.get("goal"), Mapping) else {}
        self._publish_workflow(session_id, f"goal:{operation}")
        goal_id = str(goal.get("goalId") or "")
        if self.work_documents is not None and goal_id:
            try:
                self.work_documents.observe_authority(  # type: ignore[attr-defined,union-attr]
                    "session_goal",
                    goal_id,
                )
            except Exception:
                # The observer persists its own retry record. The Goal
                # transition is already durable and must not be replayed.
                pass
        summary = {
            "confirm_setup": "长期目标已确认并开始执行",
            "update": "长期目标已更新",
            "pause": "长期目标已暂停",
            "resume": "长期目标已恢复",
            "complete": "长期目标已完成并记录证据",
            "cancel": "长期目标已取消并保留审计记录",
        }[operation]
        return {
            "summary": summary,
            "presentationKind": "goal",
            "event": result.get("event"),
            "goal": goal,
            "workflow": workflow,
        }

    def _require_room_terminal_result(self, session_id: str) -> None:
        """Keep a Room's typed public result ahead of Goal completion."""

        session = self.sessions.get(session_id)
        if not self._is_active_room_facilitator(session):
            return
        rooms = getattr(self.collaboration, "rooms", None)
        room_turns = getattr(self.collaboration, "room_turns", None)
        room_events = getattr(self.collaboration, "room_events", None)
        participant_for_session = getattr(rooms, "participant_for_session", None)
        active_turn = getattr(room_turns, "active_turn", None)
        has_projection = getattr(room_events, "has_projection", None)
        if not (
            callable(participant_for_session)
            and callable(active_turn)
            and callable(has_projection)
        ):
            raise ValueError(
                "Room Goal completion cannot verify the typed terminal result"
            )
        participant = participant_for_session(session_id, active_only=True)
        participant = participant if isinstance(participant, Mapping) else {}
        room_id = str(participant.get("roomId") or "").strip()
        root_id, _dispatch_id = active_turn(session_id)
        root_id = str(root_id or "").strip()
        if not room_id or not root_id:
            raise ValueError("Room Goal completion requires an active Room root")
        if not has_projection(f"room-terminal-result:{room_id}:{root_id}"):
            raise ValueError(
                "Room Goal completion requires room_partner post(kind=result) "
                "for the active Root first"
            )

    def _work_documents(
        self,
        operation: str,
        args: Mapping[str, object],
    ) -> dict[str, object]:
        service = self.work_documents
        if service is None:
            raise ValueError("work document lifecycle is unavailable")
        document_id = _bounded_text(args.get("documentId"), maximum=80)
        if operation == "list":
            return dict(service.list({"limit": args.get("limit", 100)}))  # type: ignore[attr-defined]
        if operation == "history.search":
            return dict(
                service.history_search(  # type: ignore[attr-defined]
                    {
                        "query": _bounded_text(args.get("query"), maximum=240),
                        "limit": args.get("limit", 100),
                    }
                )
            )
        if operation == "get":
            return dict(service.detail(document_id))  # type: ignore[attr-defined]
        if operation == "register":
            return dict(
                service.register(  # type: ignore[attr-defined]
                    {
                        key: args[key]
                        for key in (
                            "authorityKind",
                            "authorityId",
                            "authorityRevision",
                            "workspaceRoot",
                            "sourcePath",
                            "title",
                        )
                        if key in args
                    }
                )
            )
        if operation == "archive":
            return dict(
                service.request_archive(  # type: ignore[attr-defined]
                    document_id,
                    {"terminalReceiptId": args.get("terminalReceiptId")},
                )
            )
        if operation == "repair":
            return dict(service.repair(document_id))  # type: ignore[attr-defined]
        if operation == "reopen":
            return dict(
                service.reopen(  # type: ignore[attr-defined]
                    document_id,
                    {
                        "authorityRevision": args.get("authorityRevision"),
                        "transitionReceiptId": args.get("transitionReceiptId"),
                    },
                )
            )
        session_id = _bounded_text(args.get("_sessionId"), maximum=240)
        if operation == "erase.preview":
            return dict(
                service.erase_preview(  # type: ignore[attr-defined]
                    document_id,
                    {"sessionId": session_id},
                )
            )
        if operation == "erase":
            return dict(
                service.erase(  # type: ignore[attr-defined]
                    document_id,
                    {
                        "sessionId": session_id,
                        "approvalId": args.get("approvalId"),
                        "payloadSha256": args.get("payloadSha256"),
                    },
                )
            )
        raise ValueError("unsupported work_documents operation")

    def _publish_workflow(self, session_id: str, reason: str) -> None:
        if self.workflow_publisher is None:
            return
        self.workflow_publisher(session_id, reason)

    def _background_job(
        self,
        session_id: str,
        operation: str,
        args: Mapping[str, object],
    ) -> dict[str, object]:
        service = self._background_job_service()
        if operation == "list":
            return service.list(
                session_id,
                limit=args.get("limit", 50),
                status=args.get("status", ""),
            )
        if operation == "status":
            return service.status(
                session_id,
                str(args.get("jobId") or ""),
            )
        if operation == "logs":
            return service.logs(
                session_id,
                str(args.get("jobId") or ""),
                cursor=args.get("cursor", 0),
                limit_bytes=args.get("limitBytes", 65_536),
            )
        raise ValueError("unsupported read-only workspace_job operation")

    def _background_job_service(self) -> AgentBackgroundJobService:
        if self.background_jobs is None:
            raise ValueError("background job service is unavailable")
        return self.background_jobs
    def apply_approval(self, approval: Mapping[str, object]) -> dict[str, object]:
        """Execute one already-approved operation after revalidating its preview."""

        if str(approval.get("state") or "") != "approved":
            raise ValueError("approval must be in approved state before execution")
        tool = str(approval.get("toolId") or "")
        operation = str(approval.get("operation") or "")
        session_id = str(approval.get("sessionId") or "")
        live_session = self.sessions.get(session_id)
        if tool == "memory" and not self._memory_enabled(live_session):
            raise ValueError("memory tool is disabled for this session or by settings.memory.enabled")
        if read_only_policy_active(live_session) and read_only_blocks_effect(
            tool,
            operation,
        ):
            raise ValueError(
                "workspace mutation is blocked by the active read-only policy"
            )
        if _legacy_room_invocation_receipt_id(approval):
            raise ValueError(
                "legacy Room-bound approvals are no longer executable; "
                "retry the Tool call in the active Session"
            )
        if (tool, operation) in {
            ("workspace_edit", "apply"),
            ("workspace_patch", "apply"),
            ("workspace_shell", "run"),
            ("workspace_job", "start"),
            ("workspace_write", "apply"),
            ("workspace_lsp", "rename"),
            ("workspace_lsp", "code_action_apply"),
        }:
            self.sessions.require_workspace_act(
                session_id,
                room_dispatch_authorized=self._room_dispatch_authorized(
                    session_id
                ),
            )
        return self._apply_approved_operation(approval)

    def _approval_preview_state_fence_applies(self, session_id: str) -> bool:
        """Use the live Session when deciding whether legacy state fences apply."""

        session = self.sessions.get(str(session_id or ""))
        return not unrestricted_workspace_policy_active(session)

    def _approval_payload_matches(
        self,
        approval: Mapping[str, object],
        expected_digest: str,
    ) -> bool:
        return not self._approval_preview_state_fence_applies(
            str(approval.get("sessionId") or "")
        ) or expected_digest == str(approval.get("payloadSha256") or "")

    def _apply_approved_operation(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        tool = str(approval.get("toolId") or "")
        operation = str(approval.get("operation") or "")
        if (tool, operation) == ("plugins", "apply"):
            preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
            payload = preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
            digest = _approval_payload_digest(
                session_id=str(approval.get("sessionId") or ""), tool=tool, operation=operation,
                action_payload=payload, base_state={},
            )
            if digest != approval.get("payloadSha256"):
                raise ValueError("plugin approval payload no longer matches its preview")
            if self.extensions is None:
                raise ValueError("managed plugin lifecycle is unavailable")
            return dict(self.extensions.apply({**payload, "confirmText": "apply"}))
        if (tool, operation) == ("workspace_job", "start"):
            return self._apply_background_job_start(approval)
        if (tool, operation) == ("workspace_job", "cancel"):
            return self._apply_background_job_cancel(approval)
        if (tool, operation) == ("workspace_shell", "run"):
            return self._apply_workspace_command(approval)
        if (tool, operation) == ("workspace_patch", "apply"):
            return self._apply_workspace_patch(approval)
        if (tool, operation) == ("workspace_edit", "apply"):
            return self._apply_workspace_edit(approval)
        if (tool, operation) == ("workspace_write", "apply"):
            return self._apply_workspace_write(approval)
        if tool == "workspace_lsp" and operation in {"rename", "code_action_apply"}:
            return self._apply_workspace_lsp(approval)
        if (tool, operation) == ("desktop_semantic", "act"):
            return self._apply_desktop_action(approval)
        if (tool, operation) == ("planning", "undo_task_event"):
            return self._apply_planning_undo(approval)
        if tool == "agent_schedule":
            return self._apply_agent_schedule(approval)
        if tool == "memory" and operation in {"maintenance_apply", "maintenance_rollback"}:
            return self._apply_memory_mutation(approval)
        if tool == "memory" and operation in {
            "remember_apply",
            "correct_apply",
            "forget_apply",
            "governance_rollback",
        }:
            return self._apply_governed_memory_mutation(approval)
        if tool == "knowledge" and operation in {
            "create_base",
            "configure_base",
            "import_text",
            "rebuild",
        }:
            return self._apply_knowledge_mutation(approval)
        if tool == "input" and operation in {"apply_settings", "rollback_settings"}:
            return self._apply_input_settings(approval)
        if tool == "input" and operation in {"lexicon_apply", "lexicon_rollback"}:
            return self._apply_lexicon_mutation(approval)
        if tool == "runtime" and operation in {
            "pause_ai",
            "resume_ai",
            "restart_sidecar",
            "restart_predictor",
            "redeploy_rime",
        }:
            return self._apply_runtime_mutation(approval)
        if tool == "models" and operation in {"profile_apply", "profile_rollback"}:
            return self._apply_model_profile_mutation(approval)
        if tool == "voice" and operation in {"provider_apply", "provider_rollback"}:
            return self._apply_voice_provider_mutation(approval)
        if (tool, operation) == ("configuration", "export"):
            return self._apply_configuration_export(approval)
        if (tool, operation) == ("configuration", "restore_apply"):
            return self._apply_configuration_restore(approval)
        if (tool, operation) != ("planning", "task_action"):
            raise ValueError("approved operation is not enabled")
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = (
            preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        )
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool=tool,
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")

        task_id = _bounded_text(action_payload.get("taskId"), maximum=240)
        action = _bounded_text(action_payload.get("action"), maximum=40)
        plan_date = _bounded_text(action_payload.get("date"), maximum=24)
        if not task_id or action not in _PLANNING_TARGET_STATUS or not plan_date:
            raise ValueError("approved task action payload is invalid")
        task = self._planning_task(task_id=task_id, plan_date=plan_date)
        if self._approval_preview_state_fence_applies(
            str(approval.get("sessionId") or "")
        ) and (
            str(task.get("status") or "") != str(base_state.get("status") or "")
            or _safe_int(task.get("updatedAtMs")) != _safe_int(base_state.get("updatedAtMs"))
        ):
            raise ValueError("task changed after the approval preview was created")

        target_status = _PLANNING_TARGET_STATUS[action]
        try:
            result = self.management.planning_task_action({"taskId": task_id, "action": action})
        except Exception as exc:
            # The task mutation and audit use separate service calls. If an audit
            # write fails after the task commit, report the observed mutation so
            # Pi cannot retry and duplicate the action.
            observed = self._planning_task(task_id=task_id, plan_date=plan_date)
            if (
                str(observed.get("status") or "") == target_status
                and (
                    not self._approval_preview_state_fence_applies(
                        str(approval.get("sessionId") or "")
                    )
                    or _safe_int(observed.get("updatedAtMs"))
                    != _safe_int(base_state.get("updatedAtMs"))
                )
            ):
                return self._planning_receipt(
                    approval=approval,
                    action=action,
                    result={"task": observed, "undoAvailable": False},
                    audit_persisted=False,
                    warning=_bounded_text(exc, maximum=240),
                )
            raise
        return self._planning_receipt(
            approval=approval,
            action=action,
            result=result,
            audit_persisted=True,
        )
    def _prepare_approval(
        self,
        *,
        session_id: str,
        tool: str,
        operation: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        return self._prepare_approval_operation(
            session_id=session_id,
            tool=tool,
            operation=operation,
            args=args,
            risk_level=risk_level,
        )

    def _prepare_approval_operation(
        self,
        *,
        session_id: str,
        tool: str,
        operation: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        if (tool, operation) == ("plugins", "apply"):
            if self.extensions is None:
                raise ValueError("managed plugin lifecycle is unavailable")
            payload = {key: str(args.get(key) or "") for key in ("previewToken", "payloadSha256")}
            summary = self.extensions.inspect_preview(payload)
            label = {"install": "安装", "update": "更新", "enable": "启用", "disable": "停用", "uninstall": "卸载", "rollback": "回滚"}.get(str(summary.get("action")), "更改")
            description = f"{label} {summary.get('displayName') or summary.get('pluginId')}"
            digest = _approval_payload_digest(
                session_id=session_id, tool=tool, operation=operation, action_payload=payload, base_state={},
            )
            approval = self.sessions.create_approval(
                session_id=session_id, tool_name=tool, operation=operation, payload_sha256=digest,
                preview={"title": description, "summary": description, "actionPayload": payload, "baseState": {}, "package": summary},
                risk_level=risk_level, ttl_ms=60_000,
            )
            return {"summary": description, "approvalRequired": True, "approvalId": approval["approvalId"], "approval": approval}
        if tool == "memory" and not self._memory_enabled(self.sessions.get(session_id)):
            raise ValueError("memory tool is disabled for this session or by settings.memory.enabled")
        if (tool, operation) == ("workspace_job", "start"):
            return self._prepare_background_job_start(
                session_id=session_id,
                args=args,
                risk_level=risk_level,
            )
        if (tool, operation) == ("workspace_job", "cancel"):
            return self._prepare_background_job_cancel(
                session_id=session_id,
                args=args,
                risk_level=risk_level,
            )
        if (tool, operation) == ("workspace_shell", "run"):
            return self._prepare_workspace_command(
                session_id=session_id,
                args=args,
                risk_level=risk_level,
            )
        if (tool, operation) == ("workspace_patch", "apply"):
            return self._prepare_workspace_patch(
                session_id=session_id,
                args=args,
                risk_level=risk_level,
            )
        if (tool, operation) == ("workspace_edit", "apply"):
            return self._prepare_workspace_edit(
                session_id=session_id,
                args=args,
                risk_level=risk_level,
            )
        if (tool, operation) == ("workspace_write", "apply"):
            return self._prepare_workspace_write(
                session_id=session_id,
                args=args,
                risk_level=risk_level,
            )
        if tool == "workspace_lsp" and operation in {"rename", "code_action_apply"}:
            return self._prepare_workspace_lsp(
                session_id=session_id,
                operation=operation,
                args=args,
                risk_level=risk_level,
            )
        if (tool, operation) == ("desktop_semantic", "act"):
            return self._prepare_desktop_action(
                session_id=session_id,
                args=args,
                risk_level=risk_level,
            )
        if (tool, operation) == ("planning", "undo_task_event"):
            return self._prepare_planning_undo(
                session_id=session_id,
                args=args,
                risk_level=risk_level,
            )
        if tool == "agent_schedule":
            return self._prepare_agent_schedule(
                session_id=session_id,
                operation=operation,
                args=args,
                risk_level=risk_level,
            )
        if tool == "memory" and operation in {"maintenance_apply", "maintenance_rollback"}:
            return self._prepare_memory_mutation(
                session_id=session_id,
                operation=operation,
                args=args,
                risk_level=risk_level,
            )
        if tool == "memory" and operation in {
            "remember_apply",
            "correct_apply",
            "forget_apply",
            "governance_rollback",
        }:
            return self._prepare_governed_memory_mutation(
                session_id=session_id,
                operation=operation,
                args=args,
                risk_level=risk_level,
            )
        if tool == "knowledge" and operation in {
            "create_base",
            "configure_base",
            "import_text",
            "rebuild",
        }:
            return self._prepare_knowledge_mutation(
                session_id=session_id,
                operation=operation,
                args=args,
                risk_level=risk_level,
            )
        if tool == "input" and operation in {"apply_settings", "rollback_settings"}:
            return self._prepare_input_settings(
                session_id=session_id,
                operation=operation,
                args=args,
                risk_level=risk_level,
            )
        if tool == "input" and operation in {"lexicon_apply", "lexicon_rollback"}:
            return self._prepare_lexicon_mutation(
                session_id=session_id,
                operation=operation,
                args=args,
                risk_level=risk_level,
            )
        if tool == "runtime" and operation in {
            "pause_ai",
            "resume_ai",
            "restart_sidecar",
            "restart_predictor",
            "redeploy_rime",
        }:
            return self._prepare_runtime_mutation(
                session_id=session_id,
                operation=operation,
                risk_level=risk_level,
            )
        if tool == "models" and operation in {"profile_apply", "profile_rollback"}:
            return self._prepare_model_profile_mutation(
                session_id=session_id,
                operation=operation,
                args=args,
                risk_level=risk_level,
            )
        if tool == "voice" and operation in {"provider_apply", "provider_rollback"}:
            return self._prepare_voice_provider_mutation(
                session_id=session_id,
                operation=operation,
                args=args,
                risk_level=risk_level,
            )
        if (tool, operation) == ("configuration", "export"):
            return self._prepare_configuration_export(
                session_id=session_id,
                risk_level=risk_level,
            )
        if (tool, operation) == ("configuration", "restore_apply"):
            return self._prepare_configuration_restore(
                session_id=session_id,
                args=args,
                risk_level=risk_level,
            )
        if (tool, operation) != ("planning", "task_action"):
            raise ValueError("write operation is not enabled")
        task_id = _bounded_text(args.get("taskId"), maximum=240)
        action = _bounded_text(args.get("action"), maximum=40).lower()
        plan_date = _bounded_text(args.get("date"), maximum=24)
        if not task_id:
            raise ValueError("taskId is required for planning.task_action")
        if action not in _PLANNING_TARGET_STATUS:
            raise ValueError("action must be complete, start, reopen, or cancel")
        task = self._planning_task(task_id=task_id, plan_date=plan_date)
        previous_status = str(task.get("status") or "todo")
        target_status = _PLANNING_TARGET_STATUS[action]
        if previous_status == target_status:
            raise ValueError("task is already in the requested state")
        action_payload = {
            "taskId": task_id,
            "action": action,
            "date": str(task.get("date") or plan_date),
            "project": str(task.get("project") or ""),
        }
        base_state = {
            "status": previous_status,
            "updatedAtMs": _safe_int(task.get("updatedAtMs")),
        }
        digest = _approval_payload_digest(
            session_id=session_id,
            tool=tool,
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        title = _bounded_text(task.get("title"), maximum=160) or "未命名任务"
        action_label = _PLANNING_ACTION_LABELS[action]
        preview = {
            "title": "确认更新任务",
            "summary": f"将《{title}》{action_label}",
            "operationLabel": action_label,
            "changes": [
                {
                    "label": "任务状态",
                    "before": _planning_status_label(previous_status),
                    "after": _planning_status_label(target_status),
                }
            ],
            "actionPayload": action_payload,
            "baseState": base_state,
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name=tool,
            operation=operation,
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{preview['summary']}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _prepare_desktop_action(
        self,
        *,
        session_id: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        snapshot_id = _bounded_text(args.get("snapshotId"), maximum=200)
        revision = _bounded_int(args.get("revision"), default=0, minimum=0, maximum=2_147_483_647)
        node_ref = _bounded_text(args.get("nodeRef"), maximum=200)
        selector_used = not (snapshot_id and revision > 0 and node_ref)
        if selector_used:
            found = self._find_desktop_target({**dict(args), "limit": 2})
            matches = found.get("matches") if isinstance(found.get("matches"), list) else []
            match_count = _safe_int(found.get("matchCount"))
            if match_count == 0 or not matches:
                raise ValueError("desktop semantic selector matched no nodes")
            match = args.get("match") if isinstance(args.get("match"), Mapping) else {}
            exact_identifier = _bounded_text(match.get("identifier"), maximum=240)
            if found.get("truncated") is True and not exact_identifier:
                raise ValueError(
                    "desktop semantic selector uniqueness is unknown because the tree was truncated; "
                    "use an exact identifier"
                )
            if match_count != 1:
                raise ValueError("desktop semantic selector must match exactly one node")
            match = matches[0] if isinstance(matches[0], Mapping) else {}
            snapshot_id = _bounded_text(found.get("snapshotId"), maximum=200)
            revision = _bounded_int(found.get("revision"), default=0, minimum=0, maximum=2_147_483_647)
            node_ref = _bounded_text(match.get("nodeRef"), maximum=200)
        prepared = self.desktop_client.prepare_action(  # type: ignore[attr-defined]
            snapshot_id=snapshot_id,
            revision=revision,
            node_ref=node_ref,
            action=_bounded_text(args.get("action"), maximum=80),
            text=str(args.get("text"))[:8_000] if isinstance(args.get("text"), str) else None,
            key=_bounded_text(args.get("key"), maximum=40),
            modifiers=tuple(
                str(value)
                for value in args.get("modifiers", [])
                if isinstance(value, str)
            )
            if isinstance(args.get("modifiers"), list)
            else (),
            duration_ms=(
                _bounded_int(args.get("durationMs"), default=650, minimum=100, maximum=3_000)
                if args.get("durationMs") is not None
                else None
            ),
            scroll_delta=(
                _bounded_int(args.get("scrollDelta"), default=3, minimum=-20, maximum=20)
                if args.get("scrollDelta") is not None
                else None
            ),
        )
        if not isinstance(prepared, Mapping):
            raise ValueError("desktop action preview is invalid")
        action_payload = (
            dict(prepared.get("actionPayload"))
            if isinstance(prepared.get("actionPayload"), Mapping)
            else {}
        )
        base_state = (
            dict(prepared.get("baseState"))
            if isinstance(prepared.get("baseState"), Mapping)
            else {}
        )
        if not action_payload or not base_state:
            raise ValueError("desktop action preview is incomplete")
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="desktop_semantic",
            operation="act",
            action_payload=action_payload,
            base_state=base_state,
        )
        summary = _bounded_text(prepared.get("summary"), maximum=240) or "执行桌面语义动作"
        action = _bounded_text(action_payload.get("action"), maximum=80)
        text_chars = _safe_int(action_payload.get("textChars"))
        changes = [
            {
                "label": "桌面动作",
                "before": "未执行",
                "after": action,
            },
            {
                "label": "目标快照",
                "before": _bounded_text(action_payload.get("snapshotId"), maximum=80),
                "after": f"revision {_safe_int(action_payload.get('revision'))}",
            },
        ]
        if text_chars > 0:
            changes.append(
                {
                    "label": "输入文本",
                    "before": "未输入",
                    "after": f"{text_chars} 个字符（内容不在审批摘要显示）",
                }
            )
        preview = {
            "title": "确认桌面操作",
            "summary": summary,
            "operationLabel": "执行 Accessibility 语义动作",
            "changes": changes,
            "actionPayload": action_payload,
            "baseState": base_state,
            "selectorResolved": selector_used,
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="desktop_semantic",
            operation="act",
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=30_000,
        )
        return {
            "summary": f"等待确认：{summary}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_desktop_action(self, approval: Mapping[str, object]) -> dict[str, object]:
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = (
            preview.get("actionPayload")
            if isinstance(preview.get("actionPayload"), Mapping)
            else {}
        )
        base_state = (
            preview.get("baseState")
            if isinstance(preview.get("baseState"), Mapping)
            else {}
        )
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="desktop_semantic",
            operation="act",
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("desktop approval no longer matches its preview")
        receipt = self.desktop_client.act(  # type: ignore[attr-defined]
            action_payload=action_payload,
            base_state=base_state,
        )
        if not isinstance(receipt, Mapping):
            raise ValueError("desktop action receipt is invalid")
        native_receipt = dict(receipt)
        post_snapshot = (
            native_receipt.get("postSnapshot")
            if isinstance(native_receipt.get("postSnapshot"), Mapping)
            else {}
        )
        compact_receipt = {
            key: native_receipt[key]
            for key in ("schemaVersion", "applied", "action", "nodeRef", "method")
            if key in native_receipt
        }
        if post_snapshot:
            diff = post_snapshot.get("diff") if isinstance(post_snapshot.get("diff"), Mapping) else {}
            compact_receipt["verification"] = {
                "snapshotId": post_snapshot.get("snapshotId") or "",
                "revision": post_snapshot.get("revision") or 0,
                "changedNodeCount": sum(
                    len(diff.get(key) or [])
                    for key in ("added", "updated", "removed")
                    if isinstance(diff.get(key), list)
                ),
                "focusChanged": diff.get("focusChanged") is True,
            }
        return {
            "summary": (
                f"桌面动作 {_bounded_text(action_payload.get('action'), maximum=80)} 已执行，"
                "并已重新读取窗口语义状态"
            ),
            "presentationKind": "desktop_action",
            "approvalId": str(approval.get("approvalId") or ""),
            "mutationApplied": native_receipt.get("applied") is True,
            "receipt": compact_receipt,
            "auditReceipt": native_receipt,
        }

    def _prepare_memory_mutation(
        self,
        *,
        session_id: str,
        operation: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        run_id = _bounded_text(args.get("runId"), maximum=240)
        if not run_id:
            raise ValueError(f"runId is required for memory.{operation}")
        visible_owners, mutable_owner = self._memory_owner_context(session_id)
        review = self._facade_call(
            "agent_memory_maintenance_run",
            {
                "runId": run_id,
                "project": self.project,
                "visibleOwners": _owner_payloads(visible_owners),
            },
        )
        run = review.get("run") if isinstance(review.get("run"), Mapping) else {}
        run_owner = _memory_run_owner(run)
        if run_owner not in _memory_mutation_owners(
            visible_owners,
            mutable_owner=mutable_owner,
        ):
            raise ValueError("memory run is outside the current writable owner scope")
        applying = operation == "maintenance_apply"
        if applying and review.get("canApply") is not True:
            raise ValueError("memory draft is not currently applicable")
        if not applying and review.get("canRollback") is not True:
            raise ValueError("memory run is not currently rollbackable")
        revision_hash = _bounded_text(review.get("revisionHash"), maximum=96)
        if not revision_hash:
            raise ValueError("memory run revision is unavailable")
        action_payload = {
            "runId": run_id,
            "project": self.project,
            "expectedOwnerKind": run_owner[0],
            "expectedOwnerId": run_owner[1],
        }
        base_state = {
            "revisionHash": revision_hash,
            "status": _bounded_text(run.get("status"), maximum=40),
            "bundleHash": _bounded_text(run.get("bundleHash"), maximum=96),
        }
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="memory",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        pending_count = _safe_int(run.get("pendingDiffCount"))
        applied_count = _safe_int(run.get("appliedDiffCount"))
        diff_count = _safe_int(run.get("diffCount"))
        before_status = _memory_run_status_label(str(run.get("status") or ""))
        after_status = "已应用" if applying else "已回滚"
        operation_label = "应用记忆草案" if applying else "回滚记忆整理"
        summary = (
            f"将应用草案 {run_id} 的 {pending_count} 项已选差异"
            if applying
            else f"将回滚整理 {run_id} 的 {applied_count} 项已应用差异"
        )
        source_cursor = run.get("sourceCursor") if isinstance(run.get("sourceCursor"), Mapping) else {}
        from_event = _safe_int(source_cursor.get("fromEventId"))
        to_event = _safe_int(source_cursor.get("toEventId"))
        changes = [
            {"label": "整理状态", "before": before_status, "after": after_status},
            {
                "label": "记忆差异",
                "before": f"{diff_count} 项草案",
                "after": f"{pending_count if applying else applied_count} 项{after_status}",
            },
        ]
        if to_event > 0:
            changes.append(
                {
                    "label": "证据范围",
                    "before": f"事件 {from_event or 1}",
                    "after": f"事件 {to_event}",
                }
            )
        review_changes = run.get("changes") if isinstance(run.get("changes"), list) else []
        for item in review_changes[:3]:
            if not isinstance(item, Mapping) or item.get("selected") is False:
                continue
            title = _bounded_text(item.get("title"), maximum=80) or "记忆差异"
            label = _bounded_text(item.get("operationLabel"), maximum=40) or "整理记忆"
            changes.append(
                {
                    "label": title,
                    "before": "待审阅" if applying else "已应用",
                    "after": label if applying else "恢复应用前状态",
                }
            )
        preview = {
            "title": f"确认{operation_label}",
            "summary": summary,
            "operationLabel": operation_label,
            "changes": changes,
            "actionPayload": action_payload,
            "baseState": base_state,
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="memory",
            operation=operation,
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{summary}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_memory_mutation(self, approval: Mapping[str, object]) -> dict[str, object]:
        tool = str(approval.get("toolId") or "")
        operation = str(approval.get("operation") or "")
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool=tool,
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        run_id = _bounded_text(action_payload.get("runId"), maximum=240)
        project = _bounded_text(action_payload.get("project"), maximum=160)
        expected_owner = (
            _bounded_text(action_payload.get("expectedOwnerKind"), maximum=40),
            _bounded_text(action_payload.get("expectedOwnerId"), maximum=160),
        )
        if not run_id or project != self.project:
            raise ValueError("approved memory action payload is invalid")
        if not all(expected_owner):
            raise ValueError("approved memory owner is invalid")
        session_id = _bounded_text(approval.get("sessionId"), maximum=240)
        visible_owners, mutable_owner = self._memory_owner_context(session_id)
        if expected_owner not in _memory_mutation_owners(
            visible_owners,
            mutable_owner=mutable_owner,
        ):
            raise ValueError("approved memory owner is no longer writable by this session")
        current = self._facade_call(
            "agent_memory_maintenance_run",
            {
                "runId": run_id,
                "project": self.project,
                "visibleOwners": _owner_payloads(visible_owners),
            },
        )
        current_run = current.get("run") if isinstance(current.get("run"), Mapping) else {}
        _require_memory_run_owner(current_run, expected_owner)
        if self._approval_preview_state_fence_applies(session_id) and (
            _bounded_text(current.get("revisionHash"), maximum=96)
            != _bounded_text(base_state.get("revisionHash"), maximum=96)
            or _bounded_text(_mapping_value(current, "run", "status"), maximum=40)
            != _bounded_text(base_state.get("status"), maximum=40)
        ):
            raise ValueError("memory draft changed after the approval preview was created")
        applying = operation == "maintenance_apply"
        if applying and current.get("canApply") is not True:
            raise ValueError("memory draft is no longer applicable")
        if not applying and current.get("canRollback") is not True:
            raise ValueError("memory run is no longer rollbackable")
        action_result = self._facade_call(
            "knowledge_workbench_database_apply" if applying else "knowledge_workbench_database_rollback",
            {
                "runId": run_id,
                "confirm": "apply" if applying else "rollback",
                "expectedOwnerKind": expected_owner[0],
                "expectedOwnerId": expected_owner[1],
            },
        )
        if action_result.get("ok") is not True:
            raise ValueError(_bounded_text(action_result.get("error"), maximum=240) or "memory action failed")
        after = self._facade_call(
            "agent_memory_maintenance_run",
            {
                "runId": run_id,
                "project": self.project,
                "visibleOwners": _owner_payloads(visible_owners),
            },
        )
        after_run = after.get("run") if isinstance(after.get("run"), Mapping) else {}
        _require_memory_run_owner(after_run, expected_owner)
        after_status = _bounded_text(after_run.get("status"), maximum=40)
        mutation_applied = (
            after_status in {"applied", "partial"}
            if applying
            else after_status == "rolled_back"
        )
        if not mutation_applied:
            raise ValueError(f"memory action ended in unexpected status: {after_status or 'unknown'}")
        diff_count = (
            _safe_int(after_run.get("appliedDiffCount"))
            if applying
            else _safe_int(_mapping_value(current, "run", "appliedDiffCount"))
        )
        receipt = {
            "schemaVersion": "rag-ime.agent-operation-receipt.v1",
            "mutationApplied": True,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": tool,
            "operation": operation,
            "auditId": str(approval.get("approvalId") or ""),
            "runId": run_id,
            "status": after_status,
            "diffCount": diff_count,
            "summary": (
                f"已应用记忆草案，共写入 {diff_count} 项差异"
                if applying
                else f"已回滚记忆整理，共恢复 {diff_count} 项差异"
            ),
            "undoAvailable": applying,
        }
        if applying:
            receipt["rollback"] = {
                "tool": "memory",
                "operation": "maintenance_rollback",
                "args": {"runId": run_id},
            }
        else:
            receipt["revertedRunId"] = run_id
        return receipt

    def _prepare_governed_memory_mutation(
        self,
        *,
        session_id: str,
        operation: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        proposal_id = _bounded_text(args.get("proposalId"), maximum=240)
        if not proposal_id:
            raise ValueError(f"proposalId is required for memory.{operation}")
        store = self._governed_memory_store()
        if operation == "governance_rollback":
            prepared = store.prepare_rollback(
                proposal_id=proposal_id,
                session_id=session_id,
            )
        else:
            prepared = store.prepare_apply(
                operation,
                proposal_id=proposal_id,
                session_id=session_id,
            )
        action_payload = (
            prepared.get("actionPayload")
            if isinstance(prepared.get("actionPayload"), Mapping)
            else {}
        )
        base_state = (
            prepared.get("baseState")
            if isinstance(prepared.get("baseState"), Mapping)
            else {}
        )
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="memory",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        preview = {
            "title": _bounded_text(prepared.get("title"), maximum=160),
            "summary": _bounded_text(prepared.get("summary"), maximum=400),
            "operationLabel": _bounded_text(
                prepared.get("operationLabel"),
                maximum=80,
            ),
            "changes": _safe_payload(prepared.get("changes")),
            "actionPayload": dict(action_payload),
            "baseState": dict(base_state),
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="memory",
            operation=operation,
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{preview['summary']}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_governed_memory_mutation(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        operation = str(approval.get("operation") or "")
        preview = (
            approval.get("preview")
            if isinstance(approval.get("preview"), Mapping)
            else {}
        )
        action_payload = (
            preview.get("actionPayload")
            if isinstance(preview.get("actionPayload"), Mapping)
            else {}
        )
        base_state = (
            preview.get("baseState")
            if isinstance(preview.get("baseState"), Mapping)
            else {}
        )
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="memory",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        proposal_id = _bounded_text(action_payload.get("proposalId"), maximum=240)
        payload_sha256 = _bounded_text(
            action_payload.get("payloadSha256"),
            maximum=64,
        )
        if not proposal_id or len(payload_sha256) != 64:
            raise ValueError("approved memory proposal payload is invalid")
        store = self._governed_memory_store()
        if operation == "governance_rollback":
            rollback_state_sha256 = _bounded_text(
                base_state.get("rollbackStateSha256"),
                maximum=64,
            )
            if len(rollback_state_sha256) != 64:
                raise ValueError("approved memory rollback state is invalid")
            return store.rollback(
                proposal_id=proposal_id,
                session_id=str(approval.get("sessionId") or ""),
                approval_id=str(approval.get("approvalId") or ""),
                expected_state_sha256=rollback_state_sha256,
            )
        return store.apply(
            operation,
            proposal_id=proposal_id,
            session_id=str(approval.get("sessionId") or ""),
            approval_id=str(approval.get("approvalId") or ""),
        )

    def _prepare_input_settings(
        self,
        *,
        session_id: str,
        operation: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        snapshot = self._input_settings_snapshot()
        current_flat = flatten_settings(snapshot["settings"])
        source_approval_id = ""
        if operation == "rollback_settings":
            source = self._settings_rollback_source(
                session_id=session_id,
                source_approval_id=_bounded_text(args.get("sourceApprovalId"), maximum=240),
            )
            receipt = source.get("receipt") if isinstance(source.get("receipt"), Mapping) else {}
            previous_values = (
                receipt.get("beforeValues") if isinstance(receipt.get("beforeValues"), Mapping) else {}
            )
            applied_values = (
                receipt.get("afterValues") if isinstance(receipt.get("afterValues"), Mapping) else {}
            )
            if not previous_values or set(previous_values) != set(applied_values):
                raise ValueError("settings rollback receipt is incomplete")
            for key, value in applied_values.items():
                if current_flat.get(str(key)) != value:
                    raise ValueError("input settings changed after the source operation")
            normalized = _normalize_input_setting_changes(
                [{"key": key, "value": value} for key, value in previous_values.items()]
            )
            source_approval_id = str(source.get("approvalId") or "")
        else:
            normalized = _normalize_input_setting_changes(args.get("changes"))

        actual = [
            item
            for item in normalized
            if current_flat.get(str(item["key"])) != item["value"]
        ]
        if not actual:
            raise ValueError("input settings already match the requested values")
        before_values = {str(item["key"]): current_flat.get(str(item["key"])) for item in actual}
        after_values = {str(item["key"]): item["value"] for item in actual}
        action_payload: dict[str, object] = {"changes": actual}
        if source_approval_id:
            action_payload["sourceApprovalId"] = source_approval_id
        base_state = {
            "settingsHash": snapshot["settingsHash"],
            "values": before_values,
        }
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="input",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        rolling_back = operation == "rollback_settings"
        operation_label = "撤销输入设置变更" if rolling_back else "应用输入设置"
        preview = {
            "title": f"确认{operation_label}",
            "summary": (
                f"将恢复 {len(actual)} 项输入设置"
                if rolling_back
                else f"将更新 {len(actual)} 项输入设置"
            ),
            "operationLabel": operation_label,
            "changes": [
                {
                    "label": _input_setting_label(str(item["key"])),
                    "path": str(item["key"]),
                    "before": before_values[str(item["key"])],
                    "after": item["value"],
                }
                for item in actual
            ],
            "actionPayload": action_payload,
            "baseState": base_state,
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="input",
            operation=operation,
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{preview['summary']}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_input_settings(self, approval: Mapping[str, object]) -> dict[str, object]:
        tool = str(approval.get("toolId") or "")
        operation = str(approval.get("operation") or "")
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool=tool,
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        snapshot = self._input_settings_snapshot()
        current_flat = flatten_settings(snapshot["settings"])
        before_values = (
            base_state.get("values")
            if isinstance(base_state.get("values"), Mapping)
            else {}
        )
        normalized = _normalize_input_setting_changes(action_payload.get("changes"))
        if not before_values:
            raise ValueError("input settings changed after the approval preview was created")
        if self._approval_preview_state_fence_applies(
            str(approval.get("sessionId") or "")
        ) and (
            snapshot["settingsHash"] != str(base_state.get("settingsHash") or "")
            or any(
                current_flat.get(str(key)) != value
                for key, value in before_values.items()
            )
        ):
            raise ValueError("input settings changed after the approval preview was created")
        source_approval_id = _bounded_text(action_payload.get("sourceApprovalId"), maximum=240)
        if operation == "rollback_settings":
            self._settings_rollback_source(
                session_id=str(approval.get("sessionId") or ""),
                source_approval_id=source_approval_id,
            )

        updates = {str(item["key"]): item["value"] for item in normalized}
        result = self._facade_call(
            "settings_update",
            {**updates, "updatedBy": "pi-control-agent"},
        )
        if result.get("ok") is False:
            raise ValueError(_bounded_text(result.get("error"), maximum=240) or "settings update failed")
        changed_keys = sorted(str(value) for value in result.get("changedKeys", []) if str(value))
        expected_keys = sorted(updates)
        if changed_keys != expected_keys:
            raise ValueError("settings update did not apply the exact approved keys")
        after_snapshot = self._input_settings_snapshot()
        after_flat = flatten_settings(after_snapshot["settings"])
        if any(after_flat.get(key) != value for key, value in updates.items()):
            raise ValueError("settings update could not be verified")

        rolling_back = operation == "rollback_settings"
        receipt: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-operation-receipt.v1",
            "mutationApplied": True,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "input",
            "operation": operation,
            "auditId": _safe_int(result.get("auditId")),
            "summary": (
                f"已恢复 {len(expected_keys)} 项输入设置"
                if rolling_back
                else f"已更新 {len(expected_keys)} 项输入设置"
            ),
            "settingKeys": expected_keys,
            "changeCount": len(expected_keys),
            "beforeValues": {key: before_values[key] for key in expected_keys},
            "afterValues": {key: updates[key] for key in expected_keys},
            "settingsHash": after_snapshot["settingsHash"],
            "settingsRevision": _bounded_text(result.get("settingsRevision"), maximum=96),
            "runtimeRevision": _safe_int(result.get("runtimeRevision")),
            "undoAvailable": not rolling_back,
        }
        if rolling_back:
            receipt["revertedSettingsApprovalId"] = source_approval_id
        else:
            receipt["rollback"] = {
                "toolId": "input",
                "operation": "rollback_settings",
                "args": {"sourceApprovalId": str(approval.get("approvalId") or "")},
                "requiresApproval": True,
            }
        return receipt

    def _input_settings_snapshot(self) -> dict[str, object]:
        payload = self._facade_call("settings")
        settings = payload.get("settings") if isinstance(payload.get("settings"), Mapping) else {}
        settings_hash = _bounded_text(payload.get("settingsHash"), maximum=96)
        if not settings_hash:
            settings_hash = hashlib.sha256(
                json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        return {"settings": dict(settings), "settingsHash": settings_hash}

    def _settings_rollback_source(
        self,
        *,
        session_id: str,
        source_approval_id: str,
    ) -> dict[str, object]:
        if not source_approval_id:
            raise ValueError("sourceApprovalId is required for input.rollback_settings")
        approvals = self.sessions.list_approvals(session_id=session_id, state="applied", limit=500)
        for item in approvals:
            receipt = item.get("receipt") if isinstance(item.get("receipt"), Mapping) else {}
            if str(receipt.get("revertedSettingsApprovalId") or "") == source_approval_id:
                raise ValueError("settings change has already been rolled back")
        for item in approvals:
            receipt = item.get("receipt") if isinstance(item.get("receipt"), Mapping) else {}
            if (
                str(item.get("approvalId") or "") == source_approval_id
                and str(item.get("toolId") or "") == "input"
                and str(item.get("operation") or "") == "apply_settings"
                and receipt.get("undoAvailable") is True
            ):
                return item
        raise ValueError("settings change is not backed by an applied approval receipt")

    def _prepare_lexicon_mutation(
        self,
        *,
        session_id: str,
        operation: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        if operation == "lexicon_rollback":
            source = self._lexicon_rollback_source(
                session_id=session_id,
                source_approval_id=_bounded_text(args.get("sourceApprovalId"), maximum=240),
            )
            receipt = source.get("receipt") if isinstance(source.get("receipt"), Mapping) else {}
            rollback_id = _bounded_text(receipt.get("rollbackId"), maximum=240)
            entry_count = _safe_int(receipt.get("entryCount"))
            if not rollback_id:
                raise ValueError("lexicon rollback receipt is incomplete")
            action_payload = {
                "sourceApprovalId": str(source.get("approvalId") or ""),
                "rollbackId": rollback_id,
            }
            base_state = {
                "rollbackIdSha256": _sha256_text(rollback_id),
                "entryCount": entry_count,
            }
            summary = f"将恢复应用前的词表，并撤销 {entry_count} 条个人词条"
            changes = [
                {
                    "label": "个人词表",
                    "before": f"已应用 {entry_count} 条建议",
                    "after": "恢复应用前版本",
                }
            ]
            operation_label = "回滚个人词表"
        else:
            limit = _bounded_int(args.get("limit"), default=100, minimum=1, maximum=100)
            selected_keys = _review_key_list(args.get("selectedKeys"), limit=100)
            if not selected_keys:
                raise ValueError("selectedKeys is required for input.lexicon_apply")
            review = self._facade_call(
                "rime_lexicon_review",
                {"project": self.project, "limit": limit},
            )
            entries = review.get("entries") if isinstance(review.get("entries"), list) else []
            entry_by_key = {
                str(item.get("reviewKey") or f"{item.get('text', '')}\t{item.get('pinyin', '')}"): item
                for item in entries
                if isinstance(item, Mapping)
            }
            unknown = [key for key in selected_keys if key not in entry_by_key]
            if unknown:
                raise ValueError("selected lexicon entries changed; review them again")
            token = str(review.get("reviewToken") or "")
            if not token:
                raise ValueError("lexicon review token is unavailable")
            selected_entries = [entry_by_key[key] for key in selected_keys]
            action_payload = {
                "project": self.project,
                "limit": limit,
                "selectedKeys": selected_keys,
            }
            base_state = {
                "reviewTokenSha256": _sha256_text(token),
                "selectedEntriesSha256": _sha256_json(selected_entries),
            }
            summary = f"将把 {len(selected_entries)} 条已审阅建议增量加入个人词表"
            changes = [
                {
                    "label": _bounded_text(item.get("text"), maximum=80) or "个人词条",
                    "before": "未加入",
                    "after": _bounded_text(item.get("pinyin"), maximum=120) or "加入个人词表",
                }
                for item in selected_entries[:8]
            ]
            operation_label = "应用已审词条并重新部署"

        digest = _approval_payload_digest(
            session_id=session_id,
            tool="input",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        preview = {
            "title": f"确认{operation_label}",
            "summary": summary,
            "operationLabel": operation_label,
            "changes": changes,
            "actionPayload": action_payload,
            "baseState": base_state,
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="input",
            operation=operation,
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{summary}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_lexicon_mutation(self, approval: Mapping[str, object]) -> dict[str, object]:
        operation = str(approval.get("operation") or "")
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="input",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")

        rolling_back = operation == "lexicon_rollback"
        source_approval_id = _bounded_text(action_payload.get("sourceApprovalId"), maximum=240)
        if rolling_back:
            source = self._lexicon_rollback_source(
                session_id=str(approval.get("sessionId") or ""),
                source_approval_id=source_approval_id,
            )
            source_receipt = source.get("receipt") if isinstance(source.get("receipt"), Mapping) else {}
            rollback_id = _bounded_text(action_payload.get("rollbackId"), maximum=240)
            if (
                not rollback_id
                or rollback_id != _bounded_text(source_receipt.get("rollbackId"), maximum=240)
                or _sha256_text(rollback_id) != str(base_state.get("rollbackIdSha256") or "")
            ):
                raise ValueError("lexicon rollback no longer matches its source receipt")
            result = self._facade_call("rime_lexicon_rollback", {"rollbackId": rollback_id})
            if result.get("ok") is not True or result.get("rolledBack") is not True:
                raise ValueError(
                    _bounded_text(result.get("reason") or result.get("error"), maximum=240)
                    or "lexicon rollback failed"
                )
            entry_count = _safe_int(source_receipt.get("entryCount"))
        else:
            limit = _bounded_int(action_payload.get("limit"), default=100, minimum=1, maximum=100)
            selected_keys = _review_key_list(action_payload.get("selectedKeys"), limit=100)
            review = self._facade_call(
                "rime_lexicon_review",
                {"project": self.project, "limit": limit},
            )
            token = str(review.get("reviewToken") or "")
            entries = review.get("entries") if isinstance(review.get("entries"), list) else []
            entry_by_key = {
                str(item.get("reviewKey") or f"{item.get('text', '')}\t{item.get('pinyin', '')}"): item
                for item in entries
                if isinstance(item, Mapping)
            }
            selected_entries = [
                entry_by_key[key] for key in selected_keys if key in entry_by_key
            ]
            if not token or len(selected_entries) != len(selected_keys):
                raise ValueError("lexicon review changed after the approval preview was created")
            if self._approval_preview_state_fence_applies(
                str(approval.get("sessionId") or "")
            ) and (
                _sha256_text(token) != str(base_state.get("reviewTokenSha256") or "")
                or _sha256_json(selected_entries)
                != str(base_state.get("selectedEntriesSha256") or "")
            ):
                raise ValueError("lexicon review changed after the approval preview was created")
            result = self._facade_call(
                "rime_lexicon_apply",
                {
                    "project": self.project,
                    "limit": limit,
                    "reviewToken": token,
                    "selectedKeys": selected_keys,
                    "confirmText": str(review.get("confirmText") or ""),
                },
            )
            if result.get("ok") is not True or result.get("applied") is not True:
                raise ValueError(
                    _bounded_text(result.get("reason") or result.get("error"), maximum=240)
                    or "lexicon apply failed"
                )
            rollback_id = _bounded_text(result.get("rollbackId"), maximum=240)
            entry_count = _safe_int(result.get("entryCount"))
            if not rollback_id:
                raise ValueError("lexicon apply returned no rollback receipt")

        deployment = self._redeploy_rime_and_wait()
        deployment_status = _bounded_text(deployment.get("status"), maximum=80)
        deployed = deployment_status == "succeeded"
        receipt: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-operation-receipt.v1",
            "mutationApplied": True,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "input",
            "operation": operation,
            "auditId": str(approval.get("approvalId") or ""),
            "summary": (
                f"已回滚个人词表并重新部署，共恢复 {entry_count} 条词条"
                if rolling_back and deployed
                else f"已应用并重新部署 {entry_count} 条个人词条"
                if deployed
                else f"词表文件已更新，但重新部署未完成；共 {entry_count} 条词条"
            ),
            "entryCount": entry_count,
            "rollbackId": rollback_id,
            "deploymentJobId": _bounded_text(deployment.get("jobId"), maximum=240),
            "deploymentStatus": deployment_status or "unknown",
            "status": "applied" if deployed else "partial",
            "undoAvailable": not rolling_back,
        }
        if not deployed:
            receipt["warning"] = _bounded_text(
                deployment.get("error") or "Rime 重新部署未成功，请在诊断页处理后再验证候选",
                maximum=240,
            )
        if rolling_back:
            receipt["revertedLexiconApprovalId"] = source_approval_id
        else:
            receipt["rollback"] = {
                "toolId": "input",
                "operation": "lexicon_rollback",
                "args": {"sourceApprovalId": str(approval.get("approvalId") or "")},
                "requiresApproval": True,
            }
        return receipt

    def _lexicon_rollback_source(
        self,
        *,
        session_id: str,
        source_approval_id: str,
    ) -> dict[str, object]:
        if not source_approval_id:
            raise ValueError("sourceApprovalId is required for input.lexicon_rollback")
        approvals = self.sessions.list_approvals(session_id=session_id, state="applied", limit=500)
        for item in approvals:
            receipt = item.get("receipt") if isinstance(item.get("receipt"), Mapping) else {}
            if str(receipt.get("revertedLexiconApprovalId") or "") == source_approval_id:
                raise ValueError("lexicon change has already been rolled back")
        for item in approvals:
            receipt = item.get("receipt") if isinstance(item.get("receipt"), Mapping) else {}
            if (
                str(item.get("approvalId") or "") == source_approval_id
                and str(item.get("toolId") or "") == "input"
                and str(item.get("operation") or "") == "lexicon_apply"
                and receipt.get("undoAvailable") is True
                and _bounded_text(receipt.get("rollbackId"), maximum=1)
            ):
                return item
        raise ValueError("lexicon change is not backed by an applied approval receipt")

    def _voice_provider_snapshot(self) -> dict[str, object]:
        payload = self.management.provider_configuration()
        providers = payload.get("providers") if isinstance(payload.get("providers"), Mapping) else {}
        voice = providers.get("voice") if isinstance(providers.get("voice"), Mapping) else {}
        provider = _bounded_text(voice.get("provider"), maximum=80) or "native_streaming"
        return {
            "configurationHash": _bounded_text(payload.get("configurationHash"), maximum=96),
            "provider": provider,
            "settingsRevision": _bounded_text(payload.get("settingsRevision"), maximum=96),
            "runtimeRevision": _safe_int(payload.get("runtimeRevision")),
        }

    @staticmethod
    def _normalize_voice_provider(value: object) -> str:
        provider = _bounded_text(value, maximum=80)
        if provider not in {"native_streaming", "realtime_websocket", "http_transcription"}:
            raise ValueError("voice provider is unsupported")
        return provider

    def _prepare_voice_provider_mutation(
        self,
        *,
        session_id: str,
        operation: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        snapshot = self._voice_provider_snapshot()
        current = str(snapshot["provider"])
        source_approval_id = ""
        if operation == "provider_rollback":
            source = self._voice_provider_rollback_source(
                session_id=session_id,
                source_approval_id=_bounded_text(args.get("sourceApprovalId"), maximum=240),
            )
            receipt = source.get("receipt") if isinstance(source.get("receipt"), Mapping) else {}
            if current != str(receipt.get("afterProvider") or ""):
                raise ValueError("voice provider changed after the source operation")
            desired = self._normalize_voice_provider(receipt.get("beforeProvider"))
            source_approval_id = str(source.get("approvalId") or "")
        else:
            allowed = {"op", "provider"}
            unknown = sorted(str(key) for key in set(args) - allowed)
            if unknown:
                raise ValueError(f"unsupported voice provider field: {unknown[0]}")
            desired = self._normalize_voice_provider(args.get("provider"))
        if desired == current:
            raise ValueError("voice provider already matches the requested value")
        action_payload: dict[str, object] = {"provider": desired}
        if source_approval_id:
            action_payload["sourceApprovalId"] = source_approval_id
        base_state = {
            "configurationHash": snapshot["configurationHash"],
            "provider": current,
        }
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="voice",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        rolling_back = operation == "provider_rollback"
        preview = {
            "title": "确认恢复语音 Provider" if rolling_back else "确认切换语音 Provider",
            "summary": "只切换已配置的语音服务；不会读取、写入或显示任何凭据",
            "operationLabel": "恢复语音 Provider" if rolling_back else "切换语音 Provider",
            "changes": [
                {
                    "label": "语音服务",
                    "path": "voice.provider",
                    "before": current,
                    "after": desired,
                }
            ],
            "actionPayload": action_payload,
            "baseState": base_state,
            "secretsPreserved": True,
            "restartComponent": "voice",
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="voice",
            operation=operation,
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{preview['summary']}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_voice_provider_mutation(self, approval: Mapping[str, object]) -> dict[str, object]:
        operation = str(approval.get("operation") or "")
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="voice",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        snapshot = self._voice_provider_snapshot()
        if self._approval_preview_state_fence_applies(
            str(approval.get("sessionId") or "")
        ) and (
            snapshot["configurationHash"] != str(base_state.get("configurationHash") or "")
            or snapshot["provider"] != str(base_state.get("provider") or "")
        ):
            raise ValueError("voice provider changed after the approval preview was created")
        desired = self._normalize_voice_provider(action_payload.get("provider"))
        source_approval_id = _bounded_text(action_payload.get("sourceApprovalId"), maximum=240)
        if operation == "provider_rollback":
            self._voice_provider_rollback_source(
                session_id=str(approval.get("sessionId") or ""),
                source_approval_id=source_approval_id,
            )
        result = self.management.provider_configuration_apply(
            {
                "slot": "voice",
                "provider": desired,
                "expectedConfigurationHash": snapshot["configurationHash"],
            }
        )
        after = result.get("after") if isinstance(result.get("after"), Mapping) else {}
        if str(after.get("provider") or "") != desired:
            raise ValueError("voice provider update could not be verified")
        rolling_back = operation == "provider_rollback"
        receipt: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-operation-receipt.v1",
            "mutationApplied": True,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "voice",
            "operation": operation,
            "auditId": result.get("auditId") or str(approval.get("approvalId") or ""),
            "summary": (
                "语音 Provider 已恢复；重新启动语音代理后激活"
                if rolling_back
                else "语音 Provider 已保存；重新启动语音代理后激活"
            ),
            "beforeProvider": snapshot["provider"],
            "afterProvider": desired,
            "configurationHash": _bounded_text(result.get("configurationHash"), maximum=96),
            "restartComponent": "voice",
            "activationStatus": "pending_external_restart",
            "secretsPreserved": result.get("existingSecretPreserved") is True,
            "undoAvailable": not rolling_back,
        }
        if rolling_back:
            receipt["revertedVoiceProviderApprovalId"] = source_approval_id
        else:
            receipt["rollback"] = {
                "toolId": "voice",
                "operation": "provider_rollback",
                "args": {"sourceApprovalId": str(approval.get("approvalId") or "")},
                "requiresApproval": True,
            }
        return receipt

    def _voice_provider_rollback_source(
        self,
        *,
        session_id: str,
        source_approval_id: str,
    ) -> dict[str, object]:
        if not source_approval_id:
            raise ValueError("sourceApprovalId is required for voice.provider_rollback")
        approvals = self.sessions.list_approvals(session_id=session_id, state="applied", limit=500)
        for item in approvals:
            receipt = item.get("receipt") if isinstance(item.get("receipt"), Mapping) else {}
            if str(receipt.get("revertedVoiceProviderApprovalId") or "") == source_approval_id:
                raise ValueError("voice provider change has already been rolled back")
        for item in approvals:
            receipt = item.get("receipt") if isinstance(item.get("receipt"), Mapping) else {}
            if (
                str(item.get("approvalId") or "") == source_approval_id
                and str(item.get("toolId") or "") == "voice"
                and str(item.get("operation") or "") == "provider_apply"
                and receipt.get("undoAvailable") is True
            ):
                return item
        raise ValueError("voice provider change is not backed by an applied approval receipt")

    def _model_profile_snapshot(self) -> dict[str, object]:
        payload = self.management.provider_configuration()
        configuration_hash = _bounded_text(payload.get("configurationHash"), maximum=96)
        providers = payload.get("providers") if isinstance(payload.get("providers"), Mapping) else {}
        profiles: dict[str, dict[str, str]] = {}
        for slot in ("instant", "knowledge"):
            raw = providers.get(slot) if isinstance(providers.get(slot), Mapping) else {}
            profiles[slot] = {
                "provider": _bounded_text(raw.get("provider"), maximum=80),
                "endpoint": _bounded_text(raw.get("endpoint"), maximum=320),
                "model": _bounded_text(raw.get("model"), maximum=200),
            }
        if not configuration_hash:
            configuration_hash = "sha256:" + _sha256_text(
                json.dumps(profiles, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
        return {
            "configurationHash": configuration_hash,
            "profiles": profiles,
            "settingsRevision": _bounded_text(payload.get("settingsRevision"), maximum=96),
            "runtimeRevision": _safe_int(payload.get("runtimeRevision")),
        }

    def _normalize_model_profile(
        self,
        *,
        slot: str,
        requested: Mapping[str, object],
        current: Mapping[str, object],
    ) -> dict[str, str]:
        if slot not in {"instant", "knowledge"}:
            raise ValueError("slot must be instant or knowledge")
        forbidden = {"apiKey", "accessToken", "headers", "headersJSON", "token", "secret"}
        if forbidden.intersection(requested):
            raise ValueError("model profile changes cannot include secrets or custom headers")
        profile = {
            key: _bounded_text(
                requested.get(key) if key in requested else current.get(key),
                maximum=320 if key == "endpoint" else 200,
            )
            for key in ("provider", "endpoint", "model")
        }
        allowed_providers = {
            "instant": {"mlx", "ollama", "openai-compatible"},
            "knowledge": {"deepseek", "openai-compatible"},
        }[slot]
        if profile["provider"] not in allowed_providers:
            raise ValueError(f"unsupported {slot} provider")
        try:
            parsed = urlsplit(profile["endpoint"])
            port = parsed.port
        except ValueError as exc:
            raise ValueError("provider endpoint is invalid") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or port is not None and not (1 <= port <= 65_535)
        ):
            raise ValueError("provider endpoint must be a plain HTTP(S) URL without credentials or query")
        if profile["provider"] == "mlx" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("the managed MLX provider must use a loopback endpoint")
        if profile["provider"] != "mlx" and not profile["model"]:
            raise ValueError("model is required for the selected provider")
        return profile

    def _prepare_model_profile_mutation(
        self,
        *,
        session_id: str,
        operation: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        snapshot = self._model_profile_snapshot()
        profiles = snapshot["profiles"] if isinstance(snapshot.get("profiles"), Mapping) else {}
        source_approval_id = ""
        if operation == "profile_rollback":
            source = self._model_profile_rollback_source(
                session_id=session_id,
                source_approval_id=_bounded_text(args.get("sourceApprovalId"), maximum=240),
            )
            receipt = source.get("receipt") if isinstance(source.get("receipt"), Mapping) else {}
            slot = _bounded_text(receipt.get("slot"), maximum=40)
            before = receipt.get("beforeProfile") if isinstance(receipt.get("beforeProfile"), Mapping) else {}
            after = receipt.get("afterProfile") if isinstance(receipt.get("afterProfile"), Mapping) else {}
            current = profiles.get(slot) if isinstance(profiles.get(slot), Mapping) else {}
            if not before or not after or dict(current) != dict(after):
                raise ValueError("model provider profile changed after the source operation")
            desired = self._normalize_model_profile(slot=slot, requested=before, current=current)
            source_approval_id = str(source.get("approvalId") or "")
        else:
            allowed = {"op", "slot", "provider", "endpoint", "model"}
            unknown = sorted(str(key) for key in set(args) - allowed)
            if unknown:
                raise ValueError(f"unsupported model profile field: {unknown[0]}")
            slot = _bounded_text(args.get("slot"), maximum=40)
            current = profiles.get(slot) if isinstance(profiles.get(slot), Mapping) else {}
            desired = self._normalize_model_profile(slot=slot, requested=args, current=current)

        changes = [
            {
                "label": {"provider": "服务", "endpoint": "服务地址", "model": "模型"}[key],
                "path": f"{slot}.{key}",
                "before": current.get(key, ""),
                "after": desired[key],
            }
            for key in ("provider", "endpoint", "model")
            if current.get(key, "") != desired[key]
        ]
        if not changes:
            raise ValueError("model provider profile already matches the requested values")
        action_payload: dict[str, object] = {"slot": slot, "profile": desired}
        if source_approval_id:
            action_payload["sourceApprovalId"] = source_approval_id
        base_state = {
            "configurationHash": snapshot["configurationHash"],
            "profile": dict(current),
        }
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="models",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        rolling_back = operation == "profile_rollback"
        slot_label = "即时补全" if slot == "instant" else "知识模型"
        preview = {
            "title": "确认恢复模型 Provider" if rolling_back else "确认更新模型 Provider",
            "summary": f"将{'恢复' if rolling_back else '更新'}{slot_label}的非密钥配置",
            "operationLabel": "恢复 Provider 配置" if rolling_back else "更新 Provider 配置",
            "changes": changes,
            "actionPayload": action_payload,
            "baseState": base_state,
            "secretsPreserved": True,
            "restartComponent": "predictor" if slot == "instant" else "sidecar",
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="models",
            operation=operation,
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{preview['summary']}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_model_profile_mutation(self, approval: Mapping[str, object]) -> dict[str, object]:
        operation = str(approval.get("operation") or "")
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="models",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        snapshot = self._model_profile_snapshot()
        profiles = snapshot["profiles"] if isinstance(snapshot.get("profiles"), Mapping) else {}
        slot = _bounded_text(action_payload.get("slot"), maximum=40)
        current = profiles.get(slot) if isinstance(profiles.get(slot), Mapping) else {}
        desired_raw = action_payload.get("profile") if isinstance(action_payload.get("profile"), Mapping) else {}
        if self._approval_preview_state_fence_applies(
            str(approval.get("sessionId") or "")
        ) and (
            snapshot["configurationHash"] != str(base_state.get("configurationHash") or "")
            or dict(current) != dict(base_state.get("profile") or {})
        ):
            raise ValueError("model provider profile changed after the approval preview was created")
        desired = self._normalize_model_profile(slot=slot, requested=desired_raw, current=current)
        source_approval_id = _bounded_text(action_payload.get("sourceApprovalId"), maximum=240)
        if operation == "profile_rollback":
            self._model_profile_rollback_source(
                session_id=str(approval.get("sessionId") or ""),
                source_approval_id=source_approval_id,
            )
        result = self.management.provider_configuration_apply(
            {
                "slot": slot,
                **desired,
                "expectedConfigurationHash": snapshot["configurationHash"],
            }
        )
        after = result.get("after") if isinstance(result.get("after"), Mapping) else {}
        if any(str(after.get(key) or "") != desired[key] for key in desired):
            raise ValueError("model provider update could not be verified")

        restart_component = _bounded_text(result.get("restartComponent"), maximum=40)
        activation_status = "pending_external_restart"
        activation_error = ""
        job_id = ""
        if restart_component == "predictor":
            job = self._run_runtime_action_and_wait("restart_predictor")
            activation_status = _bounded_text(job.get("status"), maximum=80) or "unknown"
            activation_error = _bounded_text(job.get("error"), maximum=240)
            job_id = _bounded_text(job.get("jobId"), maximum=240)

        rolling_back = operation == "profile_rollback"
        slot_label = "即时补全" if slot == "instant" else "知识模型"
        summary = f"已{'恢复' if rolling_back else '保存'}{slot_label} Provider 配置"
        if activation_status == "succeeded":
            summary += "并重启本地预测器"
        elif activation_status == "pending_external_restart":
            summary += "；需由外部 Supervisor 重启 Sidecar 后激活"
        else:
            summary += "；预测器重启未完成"
        receipt: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-operation-receipt.v1",
            "mutationApplied": True,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "models",
            "operation": operation,
            "auditId": result.get("auditId") or str(approval.get("approvalId") or ""),
            "summary": summary,
            "slot": slot,
            "beforeProfile": dict(current),
            "afterProfile": dict(after),
            "configurationHash": _bounded_text(result.get("configurationHash"), maximum=96),
            "restartComponent": restart_component,
            "activationStatus": activation_status,
            "jobId": job_id,
            "secretsPreserved": result.get("existingSecretPreserved") is True,
            "undoAvailable": not rolling_back,
        }
        if activation_error:
            receipt["warning"] = activation_error
        if rolling_back:
            receipt["revertedModelProfileApprovalId"] = source_approval_id
        else:
            receipt["rollback"] = {
                "toolId": "models",
                "operation": "profile_rollback",
                "args": {"sourceApprovalId": str(approval.get("approvalId") or "")},
                "requiresApproval": True,
            }
        return receipt

    def _model_profile_rollback_source(
        self,
        *,
        session_id: str,
        source_approval_id: str,
    ) -> dict[str, object]:
        if not source_approval_id:
            raise ValueError("sourceApprovalId is required for models.profile_rollback")
        approvals = self.sessions.list_approvals(session_id=session_id, state="applied", limit=500)
        for item in approvals:
            receipt = item.get("receipt") if isinstance(item.get("receipt"), Mapping) else {}
            if str(receipt.get("revertedModelProfileApprovalId") or "") == source_approval_id:
                raise ValueError("model provider change has already been rolled back")
        for item in approvals:
            receipt = item.get("receipt") if isinstance(item.get("receipt"), Mapping) else {}
            if (
                str(item.get("approvalId") or "") == source_approval_id
                and str(item.get("toolId") or "") == "models"
                and str(item.get("operation") or "") == "profile_apply"
                and receipt.get("undoAvailable") is True
            ):
                return item
        raise ValueError("model provider change is not backed by an applied approval receipt")

    def _prepare_runtime_mutation(
        self,
        *,
        session_id: str,
        operation: str,
        risk_level: str,
    ) -> dict[str, object]:
        status = self.management.overview()
        ai_paused = status.get("aiPaused") is True
        if operation == "pause_ai" and ai_paused:
            raise ValueError("AI assistance is already paused")
        if operation == "resume_ai" and not ai_paused:
            raise ValueError("AI assistance is already running")
        management_action = {
            "pause_ai": "stop_ai",
            "resume_ai": "resume_ai",
            "restart_sidecar": "restart_sidecar",
            "restart_predictor": "restart_predictor",
            "redeploy_rime": "redeploy_rime",
        }[operation]
        labels = {
            "pause_ai": ("暂停 AI 辅助", "运行中", "已暂停"),
            "resume_ai": ("恢复 AI 辅助", "已暂停", "运行中"),
            "restart_sidecar": ("重启 Sidecar", "当前服务进程", "外部监督器启动的新进程"),
            "restart_predictor": ("重启本地预测器", "当前进程", "新进程"),
            "redeploy_rime": ("重新部署 Rime 配置", "当前部署", "重新编译并加载"),
        }
        operation_label, before_label, after_label = labels[operation]
        action_payload = {"action": management_action}
        base_state = {
            "settingsRevision": _bounded_text(status.get("settingsRevision"), maximum=96),
            "runtimeRevision": _safe_int(status.get("runtimeRevision")),
            "aiPaused": ai_paused,
        }
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="runtime",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        preview = {
            "title": f"确认{operation_label}",
            "summary": (
                "暂停期间普通拼音仍可使用，AI 候选与生成入口暂不工作"
                if operation == "pause_ai"
                else "恢复提交后预测和 AI 生成能力"
                if operation == "resume_ai"
                else "先让当前 Pi 回合完成，再由控制中心的外部监督器重启 Sidecar；普通 Rime 拼音继续可用"
                if operation == "restart_sidecar"
                else "预测器会短暂不可用，普通 Rime 拼音不受影响"
                if operation == "restart_predictor"
                else "将重新编译并加载当前 Rime 配置，输入法可能短暂刷新"
            ),
            "operationLabel": operation_label,
            "changes": [
                {
                    "label": "AI 辅助" if operation in {"pause_ai", "resume_ai"} else "运行组件",
                    "before": before_label,
                    "after": after_label,
                }
            ],
            "actionPayload": action_payload,
            "baseState": base_state,
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation=operation,
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{operation_label}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_runtime_mutation(self, approval: Mapping[str, object]) -> dict[str, object]:
        operation = str(approval.get("operation") or "")
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="runtime",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        current = self.management.overview()
        if self._approval_preview_state_fence_applies(
            str(approval.get("sessionId") or "")
        ) and (
            _bounded_text(current.get("settingsRevision"), maximum=96)
            != _bounded_text(base_state.get("settingsRevision"), maximum=96)
            or _safe_int(current.get("runtimeRevision"))
            != _safe_int(base_state.get("runtimeRevision"))
            or (current.get("aiPaused") is True) != bool(base_state.get("aiPaused"))
        ):
            raise ValueError("runtime state changed after the approval preview was created")
        if operation == "pause_ai" and current.get("aiPaused") is True:
            raise ValueError("AI assistance is already paused")
        if operation == "resume_ai" and current.get("aiPaused") is not True:
            raise ValueError("AI assistance is already running")
        action = _bounded_text(action_payload.get("action"), maximum=80)
        expected_action = {
            "pause_ai": "stop_ai",
            "resume_ai": "resume_ai",
            "restart_sidecar": "restart_sidecar",
            "restart_predictor": "restart_predictor",
            "redeploy_rime": "redeploy_rime",
        }.get(operation)
        if not expected_action or action != expected_action:
            raise ValueError("approved runtime action is invalid")
        job = self._run_runtime_action_and_wait(action)
        status = _bounded_text(job.get("status"), maximum=80)
        succeeded = status == "succeeded"
        after = self.management.overview()
        summaries = {
            "pause_ai": "AI 辅助已暂停，普通 Rime 拼音仍可使用",
            "resume_ai": "AI 辅助已恢复",
            "restart_sidecar": "Sidecar 已重启",
            "restart_predictor": "本地预测器已重启",
            "redeploy_rime": "Rime 配置已重新部署",
        }
        if operation == "restart_sidecar" and status == "external-supervisor-required":
            external_command = job.get("externalCommand")
            if not isinstance(external_command, list) or not external_command:
                raise ValueError("Sidecar restart returned no external supervisor command")
            command = [_bounded_text(value, maximum=300) for value in external_command]
            if any(not value for value in command):
                raise ValueError("Sidecar restart returned an invalid external supervisor command")
            expected_command = [
                "launchctl",
                "kickstart",
                "-k",
                f"gui/{os.getuid()}/com.rag-ime.sidecar",
            ]
            if command != expected_command:
                raise ValueError("Sidecar restart command does not match the fixed supervisor policy")
            return {
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "mutationApplied": False,
                "externalActionPending": True,
                "approvalId": str(approval.get("approvalId") or ""),
                "toolId": "runtime",
                "operation": operation,
                "auditId": job.get("auditId") or str(approval.get("approvalId") or ""),
                "summary": "Sidecar 重启已批准；Pi 完成当前回答后，由控制中心外部监督器执行",
                "jobId": _bounded_text(job.get("jobId"), maximum=240),
                "status": status,
                "externalAction": "restart_sidecar",
                "externalCommand": command,
                "externalCommandSha256": _sha256_json(command),
                "runtimeRevision": _safe_int(after.get("runtimeRevision")),
                "settingsRevision": _bounded_text(after.get("settingsRevision"), maximum=96),
                "aiPaused": after.get("aiPaused") is True,
                "undoAvailable": False,
            }
        receipt: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-operation-receipt.v1",
            "mutationApplied": succeeded,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "runtime",
            "operation": operation,
            "auditId": job.get("auditId") or str(approval.get("approvalId") or ""),
            "summary": summaries[operation] if succeeded else f"{summaries[operation]}失败",
            "jobId": _bounded_text(job.get("jobId"), maximum=240),
            "status": status or "unknown",
            "runtimeRevision": _safe_int(after.get("runtimeRevision")),
            "settingsRevision": _bounded_text(after.get("settingsRevision"), maximum=96),
            "aiPaused": after.get("aiPaused") is True,
            "undoAvailable": False,
        }
        if not succeeded:
            receipt["reason"] = "runtime_action_failed"
            receipt["error"] = _bounded_text(job.get("error"), maximum=240) or "运行组件没有成功完成操作"
        return receipt

    def _redeploy_rime_and_wait(self) -> dict[str, object]:
        return self._run_runtime_action_and_wait("redeploy_rime")

    def _run_runtime_action_and_wait(self, action: str) -> dict[str, object]:
        starter = getattr(self.management, "start_runtime_action", None)
        reader = getattr(self.management, "runtime_job", None)
        if not callable(starter) or not callable(reader):
            raise ValueError("runtime action service is unavailable")
        queued = starter({"action": action, "requestedBy": "pi-control-agent"})
        job_id = _bounded_text(queued.get("jobId"), maximum=240)
        if not job_id:
            raise ValueError("runtime action returned no jobId")
        deadline = time.monotonic() + 95.0
        while time.monotonic() < deadline:
            report = reader(job_id)
            job = report.get("job") if isinstance(report.get("job"), Mapping) else {}
            status = _bounded_text(job.get("status"), maximum=80)
            if status in {"succeeded", "failed", "timed_out", "external-supervisor-required"}:
                result = job.get("result") if isinstance(job.get("result"), Mapping) else {}
                external_command = result.get("externalCommand")
                return {
                    "jobId": job_id,
                    "status": status,
                    "error": _bounded_text(job.get("error"), maximum=240),
                    "auditId": queued.get("auditId"),
                    "externalCommand": list(external_command)
                    if isinstance(external_command, list)
                    else [],
                }
            time.sleep(0.1)
        return {
            "jobId": job_id,
            "status": "timed_out",
            "error": "runtime action timed out",
            "auditId": queued.get("auditId"),
        }

    def _prepare_configuration_export(
        self,
        *,
        session_id: str,
        risk_level: str,
    ) -> dict[str, object]:
        overview = self.management.overview()
        export_id = f"agent-{int(time.time() * 1000)}-{_sha256_text(session_id)[:8]}"
        relative_path = f"Backups/{export_id}.ragime-backup"
        action_payload = {"exportId": export_id, "managedRelativePath": relative_path}
        base_state = {
            "scopeVersion": "portable-backup-v1",
            "secretsIncluded": False,
        }
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="configuration",
            operation="export",
            action_payload=action_payload,
            base_state=base_state,
        )
        memory = overview.get("memory") if isinstance(overview.get("memory"), Mapping) else {}
        preview = {
            "title": "确认导出便携备份",
            "summary": "导出当前数据库、非敏感设置和安全的 Rime 配置；不会包含 API Key 或模型文件",
            "operationLabel": "导出无密钥备份",
            "changes": [
                {
                    "label": "备份内容",
                    "before": "仅保存在当前设备",
                    "after": "数据库 + 设置 + Rime 配置",
                },
                {
                    "label": "敏感信息",
                    "before": "Keychain / API Key",
                    "after": "不导出",
                },
                {
                    "label": "记忆工具书",
                    "before": "当前数据库",
                    "after": f"备份 {_safe_int(memory.get('memoryBookCount'))} 本工具书",
                },
            ],
            "actionPayload": action_payload,
            "baseState": base_state,
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="configuration",
            operation="export",
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": "等待确认：导出不含密钥的便携备份",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_configuration_export(self, approval: Mapping[str, object]) -> dict[str, object]:
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="configuration",
            operation="export",
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        if base_state.get("secretsIncluded") is not False or base_state.get("scopeVersion") != "portable-backup-v1":
            raise ValueError("approved backup scope is invalid")
        relative_path = _bounded_text(action_payload.get("managedRelativePath"), maximum=300)
        target = self._managed_backup_target(relative_path)
        if target.exists() or target.is_symlink():
            raise ValueError("managed backup target already exists")
        result = self.management.portable_backup_export({"destination": str(target)})
        if result.get("ok") is False or result.get("secretsIncluded") is not False:
            raise ValueError(_bounded_text(result.get("error"), maximum=240) or "portable backup export failed")
        if Path(str(result.get("path") or "")).expanduser().resolve() != target.resolve():
            raise ValueError("portable backup was written outside the approved managed path")
        return {
            "schemaVersion": "rag-ime.agent-operation-receipt.v1",
            "mutationApplied": True,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "configuration",
            "operation": "export",
            "auditId": result.get("auditId") or str(approval.get("approvalId") or ""),
            "summary": f"已导出无密钥备份 {target.name}",
            "exportId": _bounded_text(action_payload.get("exportId"), maximum=120),
            "fileName": target.name,
            "managedRelativePath": relative_path,
            "sizeBytes": _safe_int(result.get("sizeBytes")),
            "databaseCounts": _safe_payload(result.get("databaseCounts")),
            "rimeFileCount": _safe_int(result.get("rimeFileCount")),
            "secretsIncluded": False,
            "undoAvailable": False,
        }

    def _prepare_configuration_restore(
        self,
        *,
        session_id: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        source_approval_id = _bounded_text(args.get("sourceApprovalId"), maximum=240)
        _source, target = self._configuration_export_source(
            session_id=session_id,
            source_approval_id=source_approval_id,
        )
        restored = self.management.portable_restore_preview({"path": str(target)})
        if restored.get("valid") is not True or restored.get("requiresRestart") is not True:
            raise ValueError("managed backup is not eligible for an external restore")
        archive_revision = _bounded_text(restored.get("restoreToken"), maximum=96)
        if len(archive_revision) != 64:
            raise ValueError("managed backup preview returned an invalid archive revision")
        action_payload = {"sourceApprovalId": source_approval_id}
        base_state = {
            "archiveRevision": archive_revision,
            "createdAtMs": _safe_int(restored.get("createdAtMs")),
            "databaseMigrationVersion": _safe_int(restored.get("databaseMigrationVersion")),
            "databaseCounts": _safe_payload(restored.get("databaseCounts")),
            "rimeFileCount": _safe_int(restored.get("rimeFileCount")),
        }
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="configuration",
            operation="restore_apply",
            action_payload=action_payload,
            base_state=base_state,
        )
        counts = restored.get("databaseCounts") if isinstance(restored.get("databaseCounts"), Mapping) else {}
        preview = {
            "title": "强确认：恢复便携备份",
            "summary": (
                "Pi 当前回合结束后，原生监督器会停止 Sidecar、恢复数据库与安全配置，"
                "再启动新 Sidecar；恢复前会自动生成回滚包"
            ),
            "operationLabel": "停止 Sidecar 并恢复备份",
            "changes": [
                {
                    "label": "本地数据库",
                    "before": "当前数据",
                    "after": f"备份中的 {_safe_int(counts.get('input_events'))} 条输入事件",
                },
                {
                    "label": "Memory Book",
                    "before": "当前工具书",
                    "after": f"备份中的 {_safe_int(counts.get('memory_books'))} 本工具书",
                },
                {
                    "label": "Rime 配置",
                    "before": "当前安全配置",
                    "after": f"备份中的 {_safe_int(restored.get('rimeFileCount'))} 个文件",
                },
            ],
            "warnings": [
                "恢复会替换当前数据库和安全配置；API Key、Keychain 与模型文件不在备份内",
                "普通 Rime 拼音在 Sidecar 停止期间仍可使用，AI 能力会短暂离线",
            ],
            "actionPayload": action_payload,
            "baseState": base_state,
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="configuration",
            operation="restore_apply",
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=120_000,
        )
        return {
            "summary": "等待强确认：停止 Sidecar 并恢复受管备份",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_configuration_restore(self, approval: Mapping[str, object]) -> dict[str, object]:
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="configuration",
            operation="restore_apply",
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        source_approval_id = _bounded_text(action_payload.get("sourceApprovalId"), maximum=240)
        _source, target = self._configuration_export_source(
            session_id=str(approval.get("sessionId") or ""),
            source_approval_id=source_approval_id,
        )
        current = self.management.portable_restore_preview({"path": str(target)})
        if (
            current.get("valid") is not True
            or (
                self._approval_preview_state_fence_applies(
                    str(approval.get("sessionId") or "")
                )
                and (
                    _bounded_text(current.get("restoreToken"), maximum=96)
                    != _bounded_text(base_state.get("archiveRevision"), maximum=96)
                    or _safe_int(current.get("databaseMigrationVersion"))
                    != _safe_int(base_state.get("databaseMigrationVersion"))
                )
            )
        ):
            raise ValueError("managed backup changed after the restore approval preview")

        support = self._managed_support_directory()
        database = Path(getattr(self.management, "db_path", support / "rag-ime.sqlite")).expanduser().resolve()
        rime_directory = Path(
            os.environ.get("RAG_IME_RIME_USER_DIR") or Path.home() / "Library" / "Rime"
        ).expanduser().resolve()
        return {
            "schemaVersion": "rag-ime.agent-operation-receipt.v1",
            "mutationApplied": False,
            "externalActionPending": True,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "configuration",
            "operation": "restore_apply",
            "auditId": str(approval.get("approvalId") or ""),
            "summary": (
                "数据库恢复已批准；Pi 完成当前回答后，由原生监督器停止 Sidecar、恢复并重新启动"
            ),
            "status": "external-supervisor-required",
            "externalAction": "restore_backup",
            "undoAvailable": False,
            "_externalPlanPayload": {
                "archivePath": str(target),
                "databasePath": str(database),
                "restoreToken": str(current.get("restoreToken") or ""),
                "rimeUserDirectory": str(rime_directory),
                "supportDirectory": str(support),
                "launchAgentPlist": str(
                    Path.home() / "Library" / "LaunchAgents" / "com.rag-ime.sidecar.plist"
                ),
                "launchAgentTarget": f"gui/{os.getuid()}/com.rag-ime.sidecar",
            },
        }

    @staticmethod
    def _managed_support_directory() -> Path:
        raw = Path(
            os.environ.get("RAG_IME_APP_SUPPORT_DIR")
            or Path.home() / "Library" / "Application Support" / "RagIme"
        ).expanduser()
        if raw.is_symlink():
            raise ValueError("managed application support directory must not be a symlink")
        return raw.resolve()

    def _managed_backup_target(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise ValueError("managed backup path is invalid")
        support = self._managed_support_directory()
        raw_backup_root = support / "Backups"
        if raw_backup_root.is_symlink():
            raise ValueError("managed backup directory must not be a symlink")
        backup_root = raw_backup_root.resolve()
        target = (support / relative_path).resolve()
        if target.parent != backup_root or target.suffix != ".ragime-backup":
            raise ValueError("managed backup path escaped its approved directory")
        return target

    def _configuration_export_source(
        self,
        *,
        session_id: str,
        source_approval_id: str,
    ) -> tuple[dict[str, object], Path]:
        if not source_approval_id:
            raise ValueError("sourceApprovalId is required for configuration.restore_preview")
        approvals = self.sessions.list_approvals(session_id=session_id, state="applied", limit=500)
        for item in approvals:
            receipt = item.get("receipt") if isinstance(item.get("receipt"), Mapping) else {}
            if (
                str(item.get("approvalId") or "") == source_approval_id
                and str(item.get("toolId") or "") == "configuration"
                and str(item.get("operation") or "") == "export"
                and receipt.get("secretsIncluded") is False
            ):
                relative_path = _bounded_text(receipt.get("managedRelativePath"), maximum=300)
                target = self._managed_backup_target(relative_path)
                if not target.is_file() or target.is_symlink():
                    raise ValueError("managed backup file is no longer available")
                return item, target
        raise ValueError("backup is not backed by an applied export receipt")

    def _prepare_workspace_command(
        self,
        *,
        session_id: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        prepared = self.workspace_harness.prepare_command(session, args)
        preview = self.workspace_harness.preview(prepared)
        action_payload = preview.get("actionPayload")
        base_state = preview.get("baseState")
        assert isinstance(action_payload, Mapping)
        assert isinstance(base_state, Mapping)
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_shell",
            operation="run",
            action_payload=action_payload,
            base_state=base_state,
        )
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_shell",
            operation="run",
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": "等待确认：运行受沙箱保护的工作区命令",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_workspace_command(self, approval: Mapping[str, object]) -> dict[str, object]:
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = (
            preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        )
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="workspace_shell",
            operation="run",
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        session = self.sessions.get(str(approval.get("sessionId") or ""))
        prepared: PreparedWorkspaceCommand = self.workspace_harness.prepare_command(
            session,
            action_payload,
        )
        if self._approval_preview_state_fence_applies(
            str(approval.get("sessionId") or "")
        ) and prepared.roots_digest != str(base_state.get("workspaceRootsSha256") or ""):
            raise ValueError("authorized workspace changed after approval preview")
        receipt = self.workspace_harness.execute(prepared)
        return {
            **receipt,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "workspace_shell",
            "operation": "run",
            "auditId": str(approval.get("approvalId") or ""),
        }

    def _prepare_background_job_start(
        self,
        *,
        session_id: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        prepared = self.workspace_harness.prepare_background_command(session, args)
        preview = dict(self.workspace_harness.preview(prepared))
        label = _bounded_text(args.get("label"), maximum=120)
        action_payload = dict(preview.get("actionPayload") or {})
        action_payload["label"] = label
        if "idempotencyKey" in args:
            action_payload["idempotencyKey"] = args["idempotencyKey"]
        base_state = dict(preview.get("baseState") or {})
        preview.update(
            {
                "title": "确认启动后台任务",
                "summary": (
                    f"在 {prepared.cwd.name or prepared.cwd} 中启动"
                    f"《{label or _bounded_text(prepared.command, maximum=80)}》"
                ),
                "operationLabel": "启动受管后台任务",
                "actionPayload": action_payload,
                "baseState": base_state,
                "changes": [
                    *list(preview.get("changes") or []),
                    {
                        "label": "运行方式",
                        "before": "前台等待",
                        "after": "后台受管，可查看日志并停止",
                    },
                ],
            }
        )
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_job",
            operation="start",
            action_payload=action_payload,
            base_state=base_state,
        )
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_job",
            operation="start",
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": "等待确认：启动受沙箱保护的后台任务",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_background_job_start(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = (
            preview.get("actionPayload")
            if isinstance(preview.get("actionPayload"), Mapping)
            else {}
        )
        base_state = (
            preview.get("baseState")
            if isinstance(preview.get("baseState"), Mapping)
            else {}
        )
        session_id = str(approval.get("sessionId") or "")
        expected_digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_job",
            operation="start",
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        session = self.sessions.get(session_id)
        prepared = self.workspace_harness.prepare_background_command(
            session,
            action_payload,
        )
        if self._approval_preview_state_fence_applies(session_id) and (
            prepared.roots_digest != str(base_state.get("workspaceRootsSha256") or "")
        ):
            raise ValueError("authorized workspace changed after approval preview")
        causal_metadata = (
            dict(approval.get("causalMetadata"))
            if isinstance(approval.get("causalMetadata"), Mapping)
            else {}
        )
        lineage = base_state.get("roomLineage")
        if isinstance(lineage, Mapping):
            causal_metadata.update(
                {
                    "roomId": lineage.get("roomId"),
                    "rootId": lineage.get("rootId"),
                    "generation": lineage.get("generation"),
                    "taskId": lineage.get("taskId"),
                    "dispatchId": lineage.get("dispatchId"),
                }
            )
        receipt = self._background_job_service().start(
            session_id,
            prepared,
            label=action_payload.get("label"),
            approval_id=str(approval.get("approvalId") or ""),
            causal_metadata=causal_metadata,
            **(
                {"idempotency_key": action_payload["idempotencyKey"]}
                if "idempotencyKey" in action_payload
                else {}
            ),
        )
        return {
            **receipt,
            "mutationApplied": not bool(receipt.get("replayed")),
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "workspace_job",
            "operation": "start",
            "auditId": str(approval.get("approvalId") or ""),
        }

    def _prepare_background_job_cancel(
        self,
        *,
        session_id: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        job_id = str(args.get("jobId") or "")
        job = self._background_job_service().status(session_id, job_id)["job"]
        action_payload = {
            "jobId": job_id,
            "reason": _bounded_text(args.get("reason"), maximum=240) or "agent_requested",
        }
        base_state = {
            "jobStatus": job["status"],
            "commandSha256": job["commandSha256"],
        }
        preview = {
            "title": "确认停止后台任务",
            "summary": f"停止后台任务《{job['label']}》及其进程组",
            "operationLabel": "停止后台任务",
            "changes": [
                {
                    "label": "任务状态",
                    "before": str(job["status"]),
                    "after": "停止",
                },
                {
                    "label": "命令",
                    "before": str(job["command"]),
                    "after": "终止整个进程组",
                },
            ],
            "actionPayload": action_payload,
            "baseState": base_state,
        }
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_job",
            operation="cancel",
            action_payload=action_payload,
            base_state=base_state,
        )
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_job",
            operation="cancel",
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": "等待确认：停止后台任务",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_background_job_cancel(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = (
            preview.get("actionPayload")
            if isinstance(preview.get("actionPayload"), Mapping)
            else {}
        )
        base_state = (
            preview.get("baseState")
            if isinstance(preview.get("baseState"), Mapping)
            else {}
        )
        session_id = str(approval.get("sessionId") or "")
        expected_digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_job",
            operation="cancel",
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        receipt = self._background_job_service().cancel(
            session_id,
            str(action_payload.get("jobId") or ""),
            reason=action_payload.get("reason"),
        )
        return {
            **receipt,
            "mutationApplied": True,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "workspace_job",
            "operation": "cancel",
            "auditId": str(approval.get("approvalId") or ""),
        }

    def _prepare_workspace_lsp(
        self,
        *,
        session_id: str,
        operation: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        prepared = self.workspace_harness.prepare_lsp_mutation(
            session,
            operation,
            args,
        )
        preview = self.workspace_harness.lsp_mutation_preview(prepared)
        action_payload = preview.get("actionPayload")
        base_state = preview.get("baseState")
        assert isinstance(action_payload, Mapping)
        assert isinstance(base_state, Mapping)
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_lsp",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_lsp",
            operation=operation,
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{preview['summary']}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_workspace_lsp(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        operation = str(approval.get("operation") or "")
        preview = (
            approval.get("preview")
            if isinstance(approval.get("preview"), Mapping)
            else {}
        )
        action_payload = (
            preview.get("actionPayload")
            if isinstance(preview.get("actionPayload"), Mapping)
            else {}
        )
        base_state = (
            preview.get("baseState")
            if isinstance(preview.get("baseState"), Mapping)
            else {}
        )
        session_id = str(approval.get("sessionId") or "")
        expected_digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_lsp",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        session = self.sessions.get(session_id)
        receipt = self.workspace_harness.apply_lsp_mutation(
            session,
            operation,
            action_payload,
            base_state,
        )
        return {
            **receipt,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "workspace_lsp",
            "operation": operation,
            "auditId": str(approval.get("approvalId") or ""),
        }


    def _prepare_workspace_patch(
        self,
        *,
        session_id: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        prepared = self.workspace_harness.prepare_patch(session, args)
        preview = self.workspace_harness.patch_preview(prepared)
        action_payload = preview.get("actionPayload")
        base_state = preview.get("baseState")
        assert isinstance(action_payload, Mapping)
        assert isinstance(base_state, Mapping)
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_patch",
            operation="apply",
            action_payload=action_payload,
            base_state=base_state,
        )
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_patch",
            operation="apply",
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{preview['summary']}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_workspace_patch(self, approval: Mapping[str, object]) -> dict[str, object]:
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = (
            preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        )
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        session_id = str(approval.get("sessionId") or "")
        expected_digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_patch",
            operation="apply",
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        session = self.sessions.get(session_id)
        receipt = self.workspace_harness.apply_patch(session, action_payload, base_state)
        result = {
            **receipt,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "workspace_patch",
            "operation": "apply",
            "auditId": str(approval.get("approvalId") or ""),
        }
        if self.artifact_projector is not None:
            projection = self.artifact_projector.project_workspace_patch(
                session=session,
                approval_id=str(approval.get("approvalId") or ""),
                receipt=result,
                preview=preview,
            )
            result.update(projection.receipt_fields())
        return result

    def _prepare_workspace_edit(
        self,
        *,
        session_id: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        prepared = self.workspace_harness.prepare_edit(session, args)
        preview = self.workspace_harness.edit_preview(prepared)
        action_payload = preview.get("actionPayload")
        base_state = preview.get("baseState")
        assert isinstance(action_payload, Mapping)
        assert isinstance(base_state, Mapping)
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_edit",
            operation="apply",
            action_payload=action_payload,
            base_state=base_state,
        )
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_edit",
            operation="apply",
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{preview['summary']}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_workspace_edit(self, approval: Mapping[str, object]) -> dict[str, object]:
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = (
            preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        )
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        session_id = str(approval.get("sessionId") or "")
        expected_digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_edit",
            operation="apply",
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        session = self.sessions.get(session_id)
        receipt = self.workspace_harness.apply_edit(session, action_payload, base_state)
        result = {
            **receipt,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "workspace_edit",
            "operation": "apply",
            "auditId": str(approval.get("approvalId") or ""),
        }
        if self.artifact_projector is not None:
            projection = self.artifact_projector.project_workspace_mutation(
                session=session,
                approval_id=str(approval.get("approvalId") or ""),
                receipt=result,
                preview=preview,
                origin_tool="workspace_edit",
            )
            result.update(projection.receipt_fields())
        return result

    def _prepare_workspace_write(
        self,
        *,
        session_id: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        prepared = self.workspace_harness.prepare_write(session, args)
        preview = self.workspace_harness.write_preview(prepared)
        action_payload = preview.get("actionPayload")
        base_state = preview.get("baseState")
        assert isinstance(action_payload, Mapping)
        assert isinstance(base_state, Mapping)
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_write",
            operation="apply",
            action_payload=action_payload,
            base_state=base_state,
        )
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_write",
            operation="apply",
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{preview['summary']}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_workspace_write(self, approval: Mapping[str, object]) -> dict[str, object]:
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = (
            preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        )
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        session_id = str(approval.get("sessionId") or "")
        expected_digest = _approval_payload_digest(
            session_id=session_id,
            tool="workspace_write",
            operation="apply",
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        session = self.sessions.get(session_id)
        work_document = (
            action_payload.get("workDocument")
            if isinstance(action_payload.get("workDocument"), Mapping)
            else None
        )
        registration_payload: dict[str, object] | None = None
        document_sync: dict[str, object] | None = None
        if work_document is not None:
            authority_kind = str(work_document.get("authorityKind") or "")
            authority_id = str(work_document.get("authorityId") or "")
            if (
                authority_kind == "session_goal"
                and authority_id
                != str(self.sessions.agent_goal(session_id).get("goalId") or "")
            ):
                raise ValueError(
                    "session work document authority must match the active Session Goal"
                )
            target = Path(str(action_payload.get("path") or "")).resolve(strict=False)
            workspace_root: Path | None = None
            relative_path: Path | None = None
            roots = session.get("workspaceRoots")
            for value in roots if isinstance(roots, list) else []:
                root = Path(str(value)).expanduser().resolve(strict=True)
                try:
                    relative = target.relative_to(root)
                except ValueError:
                    continue
                workspace_root = root
                relative_path = relative
                break
            if workspace_root is None or relative_path is None:
                raise ValueError(
                    "applied work document is outside the approved workspace roots"
                )
            registration_payload = {
                "authorityKind": authority_kind,
                "authorityId": authority_id,
                "authorityRevision": work_document.get("authorityRevision"),
                "workspaceRoot": str(workspace_root),
                "sourcePath": str(relative_path),
                "title": work_document.get("title"),
            }
            if self.work_documents is None:
                document_sync = _work_document_sync_receipt(
                    registration_payload,
                    approval_id=str(approval.get("approvalId") or ""),
                    state="failed",
                    reason="work document lifecycle is unavailable",
                )
            else:
                try:
                    self.work_documents.preflight_register(  # type: ignore[attr-defined,union-attr]
                        registration_payload
                    )
                except Exception as registration_error:
                    # The workspace mutation remains the source of truth. A
                    # stale/archived authority is recorded once and surfaced
                    # to the Room, but must not roll back real product work.
                    document_sync = _work_document_sync_receipt(
                        registration_payload,
                        approval_id=str(approval.get("approvalId") or ""),
                        state="failed",
                        reason=_bounded_text(registration_error, maximum=240),
                    )
        receipt = self.workspace_harness.apply_write(session, action_payload, base_state)
        result = {
            **receipt,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "workspace_write",
            "operation": "apply",
            "auditId": str(approval.get("approvalId") or ""),
        }
        if registration_payload is not None:
            if document_sync is None:
                try:
                    registration = dict(
                        self.work_documents.register(registration_payload)  # type: ignore[attr-defined,union-attr]
                    )
                    registration_receipt = (
                        registration.get("receipt")
                        if isinstance(registration.get("receipt"), Mapping)
                        else {}
                    )
                    registration_document = (
                        registration.get("document")
                        if isinstance(registration.get("document"), Mapping)
                        else {}
                    )
                    receipt_status = str(registration_receipt.get("status") or "")
                    document_state = str(registration_document.get("state") or "")
                    if receipt_status == "applied" and document_state == "active":
                        result["workDocumentRegistration"] = registration
                        document_sync = _work_document_sync_receipt(
                            registration_payload,
                            approval_id=str(approval.get("approvalId") or ""),
                            state="applied",
                            document_id=str(registration_document.get("documentId") or ""),
                            document_revision=registration_document.get("documentRevision"),
                        )
                    else:
                        document_sync = _work_document_sync_receipt(
                            registration_payload,
                            approval_id=str(approval.get("approvalId") or ""),
                            state=(
                                "failed"
                                if receipt_status == "failed" or document_state == "error"
                                else "pending"
                            ),
                            reason="work document registration did not become active",
                            receipt_status=receipt_status,
                            document_state=document_state,
                        )
                except Exception as registration_error:
                    document_sync = _work_document_sync_receipt(
                        registration_payload,
                        approval_id=str(approval.get("approvalId") or ""),
                        state="failed",
                        reason=_bounded_text(registration_error, maximum=240),
                    )
            result["documentSync"] = dict(document_sync or {})
        if self.artifact_projector is not None:
            projection = self.artifact_projector.project_workspace_mutation(
                session=session,
                approval_id=str(approval.get("approvalId") or ""),
                receipt=result,
                preview=preview,
                origin_tool="workspace_write",
            )
            result.update(projection.receipt_fields())
        return result

    def _prepare_planning_undo(
        self,
        *,
        session_id: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        event_id = _bounded_text(args.get("eventId"), maximum=240)
        if not event_id:
            raise ValueError("eventId is required for planning.undo_task_event")
        source_approval = self._rollback_source_approval(session_id=session_id, event_id=event_id)
        source_receipt = (
            source_approval.get("receipt") if isinstance(source_approval.get("receipt"), Mapping) else {}
        )
        source_preview = (
            source_approval.get("preview") if isinstance(source_approval.get("preview"), Mapping) else {}
        )
        source_base = (
            source_preview.get("baseState") if isinstance(source_preview.get("baseState"), Mapping) else {}
        )
        task = source_receipt.get("task") if isinstance(source_receipt.get("task"), Mapping) else {}
        task_id = _bounded_text(task.get("id"), maximum=240)
        plan_date = _bounded_text(task.get("date"), maximum=24)
        target_status = _bounded_text(source_base.get("status"), maximum=40)
        if not task_id or not plan_date or not target_status:
            raise ValueError("rollback receipt does not contain a complete task snapshot")
        current = self._planning_task(task_id=task_id, plan_date=plan_date)
        expected_current_status = _bounded_text(task.get("status"), maximum=40)
        if str(current.get("status") or "") != expected_current_status:
            raise ValueError("task no longer matches the rollback receipt")
        action_payload = {
            "eventId": event_id,
            "taskId": task_id,
            "date": plan_date,
            "targetStatus": target_status,
            "sourceApprovalId": str(source_approval.get("approvalId") or ""),
        }
        base_state = {
            "status": str(current.get("status") or ""),
            "updatedAtMs": _safe_int(current.get("updatedAtMs")),
        }
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="planning",
            operation="undo_task_event",
            action_payload=action_payload,
            base_state=base_state,
        )
        title = _bounded_text(current.get("title"), maximum=160) or "未命名任务"
        preview = {
            "title": "确认撤销任务变更",
            "summary": f"把《{title}》恢复为{_planning_status_label(target_status)}",
            "operationLabel": "撤销上一次任务变更",
            "changes": [
                {
                    "label": "任务状态",
                    "before": _planning_status_label(str(current.get("status") or "")),
                    "after": _planning_status_label(target_status),
                }
            ],
            "actionPayload": action_payload,
            "baseState": base_state,
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="planning",
            operation="undo_task_event",
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{preview['summary']}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_planning_undo(self, approval: Mapping[str, object]) -> dict[str, object]:
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = (
            preview.get("actionPayload") if isinstance(preview.get("actionPayload"), Mapping) else {}
        )
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="planning",
            operation="undo_task_event",
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("approval payload no longer matches its preview")
        event_id = _bounded_text(action_payload.get("eventId"), maximum=240)
        task_id = _bounded_text(action_payload.get("taskId"), maximum=240)
        plan_date = _bounded_text(action_payload.get("date"), maximum=24)
        target_status = _bounded_text(action_payload.get("targetStatus"), maximum=40)
        source_approval_id = _bounded_text(action_payload.get("sourceApprovalId"), maximum=240)
        if not event_id or not task_id or not plan_date or not target_status or not source_approval_id:
            raise ValueError("approved rollback payload is invalid")
        self._rollback_source_approval(
            session_id=str(approval.get("sessionId") or ""),
            event_id=event_id,
            expected_approval_id=source_approval_id,
        )
        task = self._planning_task(task_id=task_id, plan_date=plan_date)
        if self._approval_preview_state_fence_applies(
            str(approval.get("sessionId") or "")
        ) and (
            str(task.get("status") or "") != str(base_state.get("status") or "")
            or _safe_int(task.get("updatedAtMs")) != _safe_int(base_state.get("updatedAtMs"))
        ):
            raise ValueError("task changed after the rollback preview was created")
        try:
            result = self.management.planning_undo_task_event({"eventId": event_id})
        except Exception as exc:
            observed = self._planning_task(task_id=task_id, plan_date=plan_date)
            if (
                str(observed.get("status") or "") == target_status
                and (
                    not self._approval_preview_state_fence_applies(
                        str(approval.get("sessionId") or "")
                    )
                    or _safe_int(observed.get("updatedAtMs"))
                    != _safe_int(base_state.get("updatedAtMs"))
                )
            ):
                return self._planning_undo_receipt(
                    approval=approval,
                    result={"task": observed},
                    event_id=event_id,
                    source_approval_id=source_approval_id,
                    audit_persisted=False,
                    warning=_bounded_text(exc, maximum=240),
                )
            raise
        return self._planning_undo_receipt(
            approval=approval,
            result=result,
            event_id=event_id,
            source_approval_id=source_approval_id,
            audit_persisted=True,
        )

    def _rollback_source_approval(
        self,
        *,
        session_id: str,
        event_id: str,
        expected_approval_id: str = "",
    ) -> dict[str, object]:
        applied = self.sessions.list_approvals(session_id=session_id, state="applied", limit=200)
        for item in applied:
            receipt = item.get("receipt") if isinstance(item.get("receipt"), Mapping) else {}
            if str(receipt.get("revertedTaskEventId") or "") == event_id:
                raise ValueError("task event has already been rolled back")
        for item in applied:
            if expected_approval_id and str(item.get("approvalId") or "") != expected_approval_id:
                continue
            receipt = item.get("receipt") if isinstance(item.get("receipt"), Mapping) else {}
            rollback = receipt.get("rollback") if isinstance(receipt.get("rollback"), Mapping) else {}
            if (
                str(receipt.get("taskEventId") or "") == event_id
                and str(rollback.get("operation") or "") == "undo_task_event"
                and receipt.get("undoAvailable") is True
            ):
                return item
        raise ValueError("task event is not backed by an applied approval receipt")

    def _planning_undo_receipt(
        self,
        *,
        approval: Mapping[str, object],
        result: Mapping[str, object],
        event_id: str,
        source_approval_id: str,
        audit_persisted: bool,
        warning: str = "",
    ) -> dict[str, object]:
        task = result.get("task") if isinstance(result.get("task"), Mapping) else {}
        receipt: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-operation-receipt.v1",
            "mutationApplied": True,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "planning",
            "operation": "undo_task_event",
            "summary": f"已撤销《{_bounded_text(task.get('title'), maximum=160) or '任务'}》的上一次状态变更",
            "auditId": _safe_int(result.get("auditId")),
            "auditPersisted": audit_persisted,
            "task": _safe_payload(task),
            "revertedTaskEventId": event_id,
            "sourceApprovalId": source_approval_id,
            "undoAvailable": False,
        }
        if warning:
            receipt["warning"] = warning
        return receipt

    def _planning_task(self, *, task_id: str, plan_date: str) -> dict[str, object]:
        dashboard = self.management.planning_dashboard(
            plan_date=plan_date,
            project=self.project,
        )
        tasks = dashboard.get("tasks") if isinstance(dashboard.get("tasks"), list) else []
        for item in tasks:
            if isinstance(item, Mapping) and str(item.get("id") or "") == task_id:
                return dict(item)
        raise ValueError("task is not visible in the current planning scope")

    def _planning_receipt(
        self,
        *,
        approval: Mapping[str, object],
        action: str,
        result: Mapping[str, object],
        audit_persisted: bool,
        warning: str = "",
    ) -> dict[str, object]:
        task = result.get("task") if isinstance(result.get("task"), Mapping) else {}
        event_id = _bounded_text(result.get("eventId"), maximum=240)
        undo_available = bool(result.get("undoAvailable")) and bool(event_id)
        receipt: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-operation-receipt.v1",
            "mutationApplied": True,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "planning",
            "operation": "task_action",
            "summary": f"已将《{_bounded_text(task.get('title'), maximum=160) or '任务'}》{_PLANNING_ACTION_LABELS[action]}",
            "auditId": _safe_int(result.get("auditId")),
            "auditPersisted": audit_persisted,
            "taskEventId": event_id,
            "task": _safe_payload(task),
            "undoAvailable": undo_available,
        }
        if undo_available:
            receipt["rollback"] = {
                "toolId": "planning",
                "operation": "undo_task_event",
                "args": {"eventId": event_id},
                "requiresApproval": True,
            }
        if warning:
            receipt["warning"] = warning
        return receipt

    def _overview(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        if operation == "status":
            payload = self.management.overview()
            components = payload.get("components") if isinstance(payload.get("components"), Mapping) else {}
            unhealthy = [
                str(key)
                for key, value in components.items()
                if isinstance(value, Mapping) and value.get("ok") is not True
            ]
            return {
                "summary": "控制中心运行正常" if not unhealthy else f"发现 {len(unhealthy)} 个未就绪组件",
                "components": _safe_payload(components),
                "memory": _safe_payload(payload.get("memory")),
                "lastPrediction": _safe_payload(payload.get("lastPrediction")),
                "unhealthyComponents": unhealthy,
            }
        if operation == "capabilities":
            return {
                "summary": "已连接 9 个控制中心领域；运行协调会话另有 3 个受 Harness 保护的工作区工具",
                "readOnly": False,
                "toolCount": len(_TOOL_SPECS),
                "tools": [
                    {
                        "id": spec["id"],
                        "displayName": spec["displayName"],
                        "operations": list(spec["operations"]),
                    }
                    for spec in _TOOL_SPECS
                ],
                "approvalGatedOperations": [
                    "input.apply_settings",
                    "input.rollback_settings",
                    "input.lexicon_apply",
                    "input.lexicon_rollback",
                    "planning.task_action",
                    "memory.maintenance_apply",
                    "memory.maintenance_rollback",
                    "runtime.pause_ai",
                    "runtime.resume_ai",
                    "runtime.restart_sidecar",
                    "runtime.restart_predictor",
                    "runtime.redeploy_rime",
                    "configuration.export",
                ],
                "writePolicy": "R1 以上操作必须生成差异、校验快照、原生确认并保存 receipt",
            }
        limit = _bounded_int(args.get("limit"), default=8, minimum=1, maximum=20)
        query = _bounded_text(args.get("query"), maximum=240)
        history = self.management.history_page(page_request({"query": query, "limit": limit}))
        items = history.get("items", []) if isinstance(history, Mapping) else []
        return {
            "summary": f"读取 {len(items)} 条近期活动摘要",
            "items": _safe_payload(items),
            "nextCursor": _bounded_text(history.get("nextCursor"), maximum=80),
        }

    def _input(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        if operation == "get_settings":
            payload = self._facade_call("settings")
            settings = payload.get("settings") if isinstance(payload.get("settings"), Mapping) else {}
            visible = {
                key: settings[key]
                for key in ("interaction", "pinyin", "privacy", "memory", "activeRag", "agent")
                if key in settings
            }
            return {
                "summary": "已读取输入法的非敏感设置",
                "settings": _safe_payload(visible),
                "runtimeConfig": _safe_payload(payload.get("runtimeConfig")),
            }
        if operation == "preview_settings":
            snapshot = self._input_settings_snapshot()
            current_flat = flatten_settings(snapshot["settings"])
            normalized = _normalize_input_setting_changes(args.get("changes"))
            actual = [
                item
                for item in normalized
                if current_flat.get(str(item["key"])) != item["value"]
            ]
            return {
                "summary": (
                    f"有 {len(actual)} 项输入设置会发生变化"
                    if actual
                    else "输入设置已经符合请求，无需修改"
                ),
                "changeCount": len(actual),
                "changes": [
                    {
                        "label": _input_setting_label(str(item["key"])),
                        "path": str(item["key"]),
                        "before": current_flat.get(str(item["key"])),
                        "after": item["value"],
                    }
                    for item in actual
                ],
                "approvalRequiredForApply": True,
            }
        if operation == "profile":
            capabilities = self._facade_call("frontend_capabilities")
            source = self._facade_call("input_source_status")
            return {
                "summary": "已读取当前输入方案和前端能力",
                "inputSource": _safe_payload(source),
                "frontend": _safe_payload(capabilities),
            }
        if operation == "candidate_explain":
            query = _bounded_text(args.get("query") or args.get("currentInput"), maximum=240)
            if not query:
                raise ValueError("query is required for input.candidate_explain")
            payload = self._facade_call(
                "candidate_explain",
                {
                    "query": query,
                    "recentContext": _bounded_text(args.get("recentContext"), maximum=800),
                    "project": self.project,
                    "topK": _bounded_int(args.get("topK"), default=5, minimum=1, maximum=10),
                },
            )
            return {"summary": "已解释当前候选的来源与排序", **_safe_mapping_payload(payload)}
        review = self._facade_call(
            "rime_lexicon_review",
            {"project": self.project, "limit": _bounded_int(args.get("limit"), default=50, minimum=1, maximum=100)},
        )
        entries = review.get("entries") if isinstance(review.get("entries"), list) else []
        return {
            "summary": f"有 {len(entries)} 条词表建议等待用户审阅",
            "entryCount": len(entries),
            "reviewRequired": True,
            "entries": [
                _safe_lexicon_review_entry(item)
                for item in entries[:100]
                if isinstance(item, Mapping)
            ],
            "safety": "应用前必须经过词表质量 harness、原生批准和可回滚快照",
        }

    def _voice(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        if operation == "privacy_policy":
            return {
                "summary": "语音只把用户确认的最终文本送入对话",
                "partialStored": False,
                "rawAudioStoredByRagIme": False,
                "automaticSend": False,
                "memorySource": "用户发送后的 final/canonical 文本",
                "sensitiveFields": "安全输入框不启动语音写入",
            }
        if operation == "provider_preview":
            allowed = {"op", "provider", "_sessionId"}
            unknown = sorted(str(key) for key in set(args) - allowed)
            if unknown:
                raise ValueError(f"unsupported voice provider field: {unknown[0]}")
            snapshot = self._voice_provider_snapshot()
            desired = self._normalize_voice_provider(args.get("provider"))
            changed = desired != snapshot["provider"]
            return {
                "summary": "语音 Provider 已匹配，无需修改" if not changed else "将切换语音 Provider",
                "changes": (
                    [
                        {
                            "label": "语音服务",
                            "path": "voice.provider",
                            "before": snapshot["provider"],
                            "after": desired,
                        }
                    ]
                    if changed
                    else []
                ),
                "configurationHash": snapshot["configurationHash"],
                "approvalRequiredForApply": changed,
                "requiresVoiceRestart": changed,
                "secretsPreserved": True,
            }
        status = _read_voice_agent_status()
        if operation == "provider_status":
            snapshot = self._voice_provider_snapshot()
            return {
                "summary": "语音 Provider 已就绪" if status.get("credentialsConfigured") else "语音 Provider 尚未就绪",
                "provider": snapshot["provider"],
                "running": bool(status.get("running")),
                "credentialsConfigured": bool(status.get("credentialsConfigured")),
                "networkState": _bounded_text(_mapping_value(status, "telemetry", "networkState"), maximum=80),
                "updatedAtMs": _safe_int(status.get("updatedAtMs")),
            }
        return {
            "summary": "语音输入正在运行" if status.get("running") else "语音输入当前未运行",
            "status": _safe_payload(status),
        }

    def _planning(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        if operation != "dashboard":
            raise ValueError("planning write operations require native approval")
        dashboard = self.management.planning_dashboard(
            plan_date=_bounded_text(args.get("date"), maximum=24),
            project=self.project,
        )
        tasks = dashboard.get("tasks") if isinstance(dashboard.get("tasks"), list) else []
        open_count = sum(
            1
            for item in tasks
            if isinstance(item, Mapping) and str(item.get("status") or "") not in {"done", "completed", "cancelled"}
        )
        return {
            "summary": f"当前有 {open_count} 个未完成任务",
            "dashboard": _safe_payload(dashboard),
        }

    def _agent_schedule(
        self,
        operation: str,
        args: Mapping[str, object],
    ) -> dict[str, object]:
        if self.scheduling is None:
            raise ValueError("Agent wake scheduling is unavailable")
        if operation == "list":
            result = self.scheduling.list_wake_schedules(  # type: ignore[attr-defined]
                {
                    "status": args.get("status"),
                    "targetType": args.get("targetType"),
                    "targetId": args.get("targetId"),
                    "limit": args.get("limit"),
                }
            )
            items = result.get("items") if isinstance(result, Mapping) else []
            return {
                "summary": f"共有 {len(items) if isinstance(items, list) else 0} 个可见预约",
                "schedulerActive": bool(result.get("schedulerActive")),
                "items": _safe_payload(items),
            }
        if operation == "runs":
            schedule_id = _bounded_text(args.get("scheduleId"), maximum=240)
            if not schedule_id:
                raise ValueError("scheduleId is required for agent_schedule.runs")
            result = self.scheduling.wake_schedule_runs(  # type: ignore[attr-defined]
                schedule_id,
                {"limit": args.get("limit")},
            )
            return {
                "summary": "已读取预约执行记录",
                "schedule": _safe_payload(result.get("schedule")),
                "items": _safe_payload(result.get("items")),
            }
        raise ValueError("Agent schedule changes require native approval")

    def _prepare_agent_schedule(
        self,
        *,
        session_id: str,
        operation: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        if self.scheduling is None:
            raise ValueError("Agent wake scheduling is unavailable")
        if operation == "schedule":
            preview_result = self.scheduling.preview_wake_schedule(  # type: ignore[attr-defined]
                args,
                requested_by_session_id=session_id,
            )
            schedule = (
                preview_result.get("schedule")
                if isinstance(preview_result.get("schedule"), Mapping)
                else {}
            )
            action_payload = {
                key: schedule.get(key)
                for key in (
                    "title",
                    "instruction",
                    "targetType",
                    "targetSessionId",
                    "targetRoleId",
                    "targetRoleVersion",
                    "planningTaskId",
                    "timezone",
                    "recurrenceKind",
                    "recurrenceInterval",
                    "maxRuns",
                    "wakeAtMs",
                )
            }
            base_state: dict[str, object] = {}
            target_name = _bounded_text(schedule.get("targetDisplayName"), maximum=120)
            summary = f"安排{target_name or 'Agent'}在指定时间执行《{schedule.get('title', '')}》"
            changes = [
                {"label": "唤醒对象", "before": "未安排", "after": target_name},
                {
                    "label": "执行次数",
                    "before": "0 次",
                    "after": f"最多 {_safe_int(schedule.get('maxRuns'))} 次",
                },
            ]
        else:
            schedule_id = _bounded_text(args.get("scheduleId"), maximum=240)
            if not schedule_id:
                raise ValueError(f"scheduleId is required for agent_schedule.{operation}")
            schedule = self.scheduling.get_wake_schedule(schedule_id)  # type: ignore[attr-defined]
            action_payload = {"scheduleId": schedule_id, "action": operation}
            base_state = {
                "status": str(schedule.get("status") or ""),
                "updatedAtMs": _safe_int(schedule.get("updatedAtMs")),
            }
            labels = {
                "pause": "暂停预约",
                "resume": "恢复预约",
                "cancel": "取消预约",
                "retry": "重新执行预约",
            }
            summary = f"{labels[operation]}《{schedule.get('title', '')}》"
            changes = [
                {
                    "label": "预约状态",
                    "before": str(schedule.get("status") or ""),
                    "after": operation,
                }
            ]
        digest = _approval_payload_digest(
            session_id=session_id,
            tool="agent_schedule",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        preview = {
            "title": "确认 Agent 预约",
            "summary": summary,
            "operationLabel": "安排未来 Agent 执行" if operation == "schedule" else summary,
            "changes": changes,
            "actionPayload": action_payload,
            "baseState": base_state,
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="agent_schedule",
            operation=operation,
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=60_000,
        )
        return {
            "summary": f"等待确认：{summary}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_agent_schedule(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        if self.scheduling is None:
            raise ValueError("Agent wake scheduling is unavailable")
        operation = str(approval.get("operation") or "")
        preview = approval.get("preview") if isinstance(approval.get("preview"), Mapping) else {}
        action_payload = (
            preview.get("actionPayload")
            if isinstance(preview.get("actionPayload"), Mapping)
            else {}
        )
        base_state = preview.get("baseState") if isinstance(preview.get("baseState"), Mapping) else {}
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="agent_schedule",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("Agent schedule approval no longer matches its preview")
        if operation == "schedule":
            result = self.scheduling.create_wake_schedule(  # type: ignore[attr-defined]
                action_payload,
                created_by_session_id=str(approval.get("sessionId") or ""),
                require_confirmation=False,
            )
        else:
            schedule_id = _bounded_text(action_payload.get("scheduleId"), maximum=240)
            current = self.scheduling.get_wake_schedule(schedule_id)  # type: ignore[attr-defined]
            if self._approval_preview_state_fence_applies(
                str(approval.get("sessionId") or "")
            ) and (
                str(current.get("status") or "") != str(base_state.get("status") or "")
                or _safe_int(current.get("updatedAtMs"))
                != _safe_int(base_state.get("updatedAtMs"))
            ):
                raise ValueError("Agent schedule changed after the approval preview was created")
            result = self.scheduling.wake_schedule_action(  # type: ignore[attr-defined]
                schedule_id,
                {"action": operation},
                require_confirmation=False,
            )
        schedule = result.get("schedule") if isinstance(result, Mapping) else {}
        return {
            "schemaVersion": "rag-ime.agent-operation-receipt.v1",
            "mutationApplied": True,
            "approvalId": str(approval.get("approvalId") or ""),
            "toolId": "agent_schedule",
            "operation": operation,
            "summary": f"预约《{schedule.get('title', '')}》已更新",
            "schedule": _safe_payload(schedule),
            "undoAvailable": False,
        }

    def _memory(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        if not self._memory_enabled():
            raise ValueError("memory tool is disabled by settings.memory.enabled")
        session_id = _bounded_text(args.get("_sessionId"), maximum=240)
        if operation == "capture":
            try:
                capture = AgentMemorySourceStore(
                    self.sessions.db_path,
                    project=self.project,
                ).capture_hint(
                    session_id=session_id,
                    kind=_bounded_text(args.get("kind"), maximum=40),
                    claim=_bounded_text(args.get("claim"), maximum=800),
                    scope=_bounded_text(args.get("captureScope"), maximum=24)
                    or "project",
                    basis=_bounded_text(args.get("basis"), maximum=40),
                    future_use=_bounded_text(args.get("futureUse"), maximum=300),
                    supersedes=_bounded_text(args.get("supersedes"), maximum=800),
                    source_id=_bounded_text(args.get("sourceId"), maximum=240),
                    evidence_ids=[
                        _bounded_text(value, maximum=240)
                        for value in args.get("evidenceIds") or []
                        if _bounded_text(value, maximum=240)
                    ]
                    if isinstance(args.get("evidenceIds"), (list, tuple))
                    else [],
                )
            except ValueError as exc:
                message = str(exc)
                reason_code = next(
                    (
                        code
                        for marker, code in (
                            ("workflow noise or transient input", "not_durable"),
                            ("sensitive content", "sensitive"),
                            ("no active user evidence", "no_user_evidence"),
                            ("missing or outside this session", "needs_source"),
                            ("source is not eligible", "conflict_needs_review"),
                            ("unsupported memory capture basis", "invalid_basis"),
                            ("only a correction", "invalid_correction"),
                        )
                        if marker in message
                    ),
                    "invalid_candidate",
                )
                return {
                    "summary": "这条内容没有进入记忆候选层",
                    "candidate": "rejected",
                    "createsDurableMemory": False,
                    "reasonCode": reason_code,
                    "retryable": reason_code == "needs_source",
                }
            return {
                "summary": "已标记一条待后台整理的记忆候选，不创建正式 Atom",
                **capture,
            }
        visible_owners, mutable_owner = self._memory_owner_context(session_id)
        curation_owner = _personal_memory_curation_owner(
            visible_owners,
            fallback=mutable_owner,
        )
        visible_owner_payload = _owner_payloads(visible_owners)
        if operation in {"remember_preview", "correct_preview", "forget_preview"}:
            return self._governed_memory_store().preview(
                operation,
                args,
                session_id=session_id,
            )
        if operation in {"get", "review"} and _bounded_text(
            args.get("draftId"),
            maximum=240,
        ):
            return self._governed_memory_store().daily_user_memory_draft(
                draft_id=_bounded_text(args.get("draftId"), maximum=240),
                session_id=_bounded_text(args.get("_sessionId"), maximum=240),
            )
        if operation == "review":
            raise ValueError("draftId is required for memory.review")
        if operation in {"search", "get", "explain"}:
            kind = _bounded_text(args.get("kind"), maximum=40) or "atoms"
            if kind in {"atoms", "timelines"}:
                return self._governed_memory_store().read(operation, args)
            if operation != "search":
                raise ValueError(
                    f"memory.{operation} only supports governed Atom records"
                )
            if _bounded_text(args.get("mode"), maximum=24) not in {"", "current"}:
                raise ValueError(
                    "historical/change modes are only available for governed Atom records"
                )
        if operation == "catalog":
            return self._catalog(args, visible_owners=visible_owners)
        if operation == "read":
            return self._read(args, visible_owners=visible_owners)
        if operation == "recent":
            return self._recent(args, visible_owners=visible_owners)
        if operation == "trace":
            trace_id = _bounded_text(args.get("traceId"), maximum=240)
            if not trace_id:
                raise ValueError("traceId is required for memory.trace")
            payload = self._facade_call("memory_optimizer_trace", {"traceId": trace_id})
            return {"summary": "已读取记忆整理追溯信息", "trace": _safe_payload(payload)}
        if operation == "maintenance_status":
            payload = self._facade_call(
                "agent_memory_maintenance_status",
                {
                    "project": self.project,
                    "limit": _bounded_int(args.get("limit"), default=10, minimum=1, maximum=30),
                    "ownerKind": curation_owner[0],
                    "ownerId": curation_owner[1],
                    "scope": _bounded_text(args.get("scope"), maximum=24) or "incremental",
                    "policy": _bounded_text(args.get("policy"), maximum=24) or "conservative",
                },
            )
            draft_count = _safe_int(payload.get("pendingDraftCount"))
            pending_count = _safe_int(
                _mapping_value(payload, "compileState", "pendingEventCount")
            )
            auto_apply = payload.get("autoApply") is True
            draft_label = "份草案待审阅" if not auto_apply else "份草案未自动应用"
            return {
                "summary": f"有 {pending_count} 条来源待整理、{draft_count} {draft_label}",
                "maintenance": _safe_payload(payload),
            }
        if operation in {"curation_prepare", "maintenance_preview"}:
            trigger = _bounded_text(args.get("trigger"), maximum=40)
            if trigger not in {"task_completion", "explicit_request", "idle_batch"}:
                raise ValueError(
                    "memory.curation_prepare requires trigger="
                    "task_completion, explicit_request, or idle_batch"
                )
            payload = self._facade_call(
                "agent_memory_maintenance_prepare",
                {
                    "project": self.project,
                    "instruction": _bounded_text(args.get("instruction"), maximum=800),
                    "trigger": trigger,
                    "ownerKind": curation_owner[0],
                    "ownerId": curation_owner[1],
                },
            )
            if payload.get("ok") is not True:
                raise ValueError(_memory_curation_error(payload))
            source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
            pending_count = _safe_int(source.get("pendingSourceCount"))
            needs_review_count = _safe_int(source.get("needsReviewSourceCount"))
            batch_count = _safe_int(source.get("batchCount"))
            drain_limited = source.get("drainLimited") is True
            stored = payload.get("storedRun") if isinstance(payload.get("storedRun"), Mapping) else {}
            run_id = _bounded_text(stored.get("runId"), maximum=240)
            if not run_id:
                return {
                    "summary": (
                        "当前没有新增个人记忆证据需要整理"
                        if pending_count <= 0
                        else (
                            f"仍有 {needs_review_count} 条个人记忆证据需要人工确认"
                            if needs_review_count > 0
                            else f"仍有 {pending_count} 条个人记忆证据等待后续整理"
                        )
                    ),
                    "runId": "",
                    "counts": {},
                    "diffCount": 0,
                    "needsReview": False,
                    "reviewRequired": False,
                    "storedDraft": False,
                    "reusedDraft": payload.get("reusedDraft") is True,
                    "skipped": True,
                    "reason": _memory_curation_skip_reason(payload),
                    "pendingSourceCount": pending_count,
                    "needsReviewSourceCount": needs_review_count,
                    "batchCount": batch_count,
                    "drainLimited": drain_limited,
                }
            review = self._facade_call(
                "agent_memory_maintenance_run",
                {
                    "runId": run_id,
                    "project": self.project,
                    "visibleOwners": visible_owner_payload,
                },
            )
            run = review.get("run") if isinstance(review.get("run"), Mapping) else {}
            _require_memory_run_owner(run, curation_owner)
            diff_count = _safe_int(run.get("diffCount"))
            applied_diff_count = _safe_int(run.get("appliedDiffCount")) or _safe_int(
                payload.get("appliedDiffCount")
            )
            reused = payload.get("reusedDraft") is True
            receipt = _compact_memory_run_for_agent(run)
            auto_applied = payload.get("autoApplied") is True or (
                receipt["status"] in {"applied", "partial"}
                and applied_diff_count > 0
            )
            needs_review = receipt["status"] == "draft" and diff_count > 0
            pending_suffix = (
                f"；另有 {pending_count} 条证据留待下一轮整理"
                if pending_count > 0
                else ""
            )
            if auto_applied:
                summary = (
                    f"已自动应用 {applied_diff_count or diff_count} 项记忆整理"
                    f"{pending_suffix}"
                )
            elif reused:
                summary = f"已复用现有记忆草案，共 {diff_count} 项差异{pending_suffix}"
            elif needs_review:
                summary = f"已生成记忆草案，共 {diff_count} 项差异{pending_suffix}"
            elif needs_review_count > 0:
                summary = (
                    f"已整理 {max(batch_count, 1)} 批证据，有 "
                    f"{needs_review_count} 条需要人工确认"
                )
            elif pending_count > 0:
                summary = (
                    f"已整理 {max(batch_count, 1)} 批证据，仍有 "
                    f"{pending_count} 条等待后续整理"
                )
            else:
                summary = "全部新增证据已完成整理，没有需要写入的变更"
            return {
                "summary": summary,
                "runId": receipt["runId"],
                "counts": receipt["operationCounts"],
                "diffCount": receipt["diffCount"],
                "needsReview": needs_review,
                # Pi's native review bridge still consumes this compatibility
                # field; the semantic result field is needsReview.
                "reviewRequired": needs_review,
                "storedDraft": needs_review,
                "autoApplied": auto_applied,
                "appliedDiffCount": applied_diff_count,
                "reusedDraft": reused,
                "pendingSourceCount": pending_count,
                "needsReviewSourceCount": needs_review_count,
                "batchCount": batch_count,
                "drainLimited": drain_limited,
                # The Memory page fetches full diffs out of band. Returning the
                # whole draft here would feed dozens of database operations
                # back into the main Agent transcript for no useful reason.
            }
        if operation == "maintenance_review":
            run_id = _bounded_text(args.get("runId"), maximum=240)
            if not run_id:
                raise ValueError("runId is required for memory.maintenance_review")
            review = self._facade_call(
                "agent_memory_maintenance_run",
                {
                    "runId": run_id,
                    "project": self.project,
                    "visibleOwners": visible_owner_payload,
                },
            )
            run = review.get("run") if isinstance(review.get("run"), Mapping) else {}
            _require_memory_run_visible(run, visible_owners)
            receipt = _compact_memory_run_for_agent(run)
            needs_review = (
                receipt["status"] == "draft"
                and int(receipt["diffCount"]) > 0
            )
            return {
                "summary": f"已读取记忆草案 {run_id} 的 {_safe_int(run.get('diffCount'))} 项差异",
                "runId": receipt["runId"],
                "counts": receipt["operationCounts"],
                "diffCount": receipt["diffCount"],
                "needsReview": needs_review,
                "reviewRequired": needs_review,
                "canApply": review.get("canApply") is True,
                "canRollback": review.get("canRollback") is True,
                "stale": review.get("stale") is True,
            }
        kind = _bounded_text(args.get("kind"), maximum=40) or "atoms"
        if kind not in {
            "apps",
            "books",
            "atoms",
            "timelines",
            "evidence",
            "tags",
            "phrases",
            "groups",
        }:
            raise ValueError("unsupported memory list kind")
        query = _bounded_text(args.get("query"), maximum=240)
        limit = _bounded_int(args.get("limit"), default=8, minimum=1, maximum=20)
        page = self.management.memory_page(
            kind,
            page_request(
                {
                    "query": query,
                    "limit": limit,
                    "visibleOwners": visible_owner_payload,
                    "project": self.project,
                }
            ),
        )
        items = page.get("items", []) if isinstance(page, Mapping) else []
        return {
            "summary": f"检索到 {len(items)} 条 {kind} 记忆记录",
            "kind": kind,
            "query": query,
            "items": _safe_payload(items),
            "nextCursor": _bounded_text(page.get("nextCursor"), maximum=80),
        }

    def _governed_memory_store(self) -> MemoryGovernanceProposalStore:
        if self._memory_governance_store is None:
            db_path = getattr(self.sessions, "db_path", None)
            if db_path is None:
                raise ValueError("memory governance storage is unavailable")
            store = MemoryGovernanceProposalStore(db_path, project=self.project)
            store.initialize()
            self._memory_governance_store = store
        return self._memory_governance_store

    def _role_book(
        self,
        operation: str,
        args: Mapping[str, object],
    ) -> dict[str, object]:
        session_id = _bounded_text(args.get("_sessionId"), maximum=240)
        if not session_id:
            raise ValueError("agent_role_book session is missing")
        if self._role_book_tool_adapter is None:
            store = self.role_books
            if store is None:
                db_path = getattr(self.sessions, "db_path", None)
                if db_path is None:
                    raise ValueError("Agent Role Book storage is unavailable")
                store = AgentRoleBookStore(db_path)
                store.initialize()
                self.role_books = store
            db_path = getattr(store, "db_path", None) or getattr(
                self.sessions, "db_path", None
            )
            if db_path is None:
                raise ValueError("Agent Role Book history storage is unavailable")
            self._role_book_tool_adapter = AgentRoleBookToolAdapter(
                store,
                db_path=db_path,
                project=self.project,
            )
        return self._role_book_tool_adapter.execute(
            operation,
            args,
            session=self.sessions.get(session_id),
        )

    def _knowledge(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        if operation in {"get_base", "list_documents", "rebuild_preview"}:
            control = self._knowledge_management_service()
            base_id = _knowledge_identifier(args.get("kbId"), "kbId")
            if operation == "get_base":
                result = control.get_base(base_id)  # type: ignore[attr-defined]
                summary = "已读取知识库配置、版本与索引统计"
            elif operation == "list_documents":
                result = control.list_documents(base_id)  # type: ignore[attr-defined]
                summary = "已读取知识库文档清单"
            else:
                result = control.reindex_preview(base_id)  # type: ignore[attr-defined]
                summary = "已生成当前知识库索引重建预览；执行重建仍需批准"
            if not isinstance(result, Mapping):
                raise ValueError("knowledge management returned an invalid result")
            safe_result = _safe_knowledge_payload(result)
            safe_result.setdefault("summary", summary)
            safe_result.setdefault("untrustedData", True)
            return safe_result
        if operation == "status" and self.knowledge_client is None:
            return {
                "summary": "文档知识库当前不可用",
                "available": False,
                "state": "unavailable",
                "reason": "knowledge_client_not_configured",
            }
        client = self.knowledge_client
        if client is None:
            raise ValueError("document knowledge library is unavailable")

        payload: dict[str, object] = {}
        if operation in {"search", "find", "open"}:
            base_id = _bounded_text(args.get("kbId") or args.get("baseId"), maximum=240)
            if not base_id:
                raise ValueError(f"kbId is required for knowledge.{operation}")
            payload["kbId"] = base_id
        if operation == "search":
            query = _bounded_text(args.get("query"), maximum=500)
            if not query:
                raise ValueError("query is required for knowledge.search")
            payload["query"] = query
            if args.get("topK") is not None:
                payload["topK"] = _knowledge_strict_int(
                    args.get("topK"), field="topK", minimum=1, maximum=12
                )
            if args.get("searchMode") is not None:
                search_mode = _bounded_text(args.get("searchMode"), maximum=24)
                if search_mode not in {"hybrid", "lexical", "dense"}:
                    raise ValueError("searchMode must be hybrid, lexical, or dense")
                payload["mode"] = search_mode
            if args.get("threshold") is not None:
                payload["threshold"] = _knowledge_strict_float(
                    args.get("threshold"), field="threshold", minimum=0.0, maximum=1.0
                )
            if args.get("rerank") is not None:
                if not isinstance(args.get("rerank"), bool):
                    raise ValueError("rerank must be boolean")
                payload["rerank"] = args["rerank"]
            if args.get("rerankCandidateDepth") is not None:
                payload["rerankCandidateDepth"] = _knowledge_strict_int(
                    args.get("rerankCandidateDepth"),
                    field="rerankCandidateDepth",
                    minimum=1,
                    maximum=100,
                )
            file_name = _bounded_text(args.get("fileName"), maximum=240)
            if file_name:
                payload["fileName"] = file_name
        elif operation == "find":
            file_id = _bounded_text(args.get("fileId"), maximum=240)
            raw_patterns = args.get("patterns")
            if isinstance(raw_patterns, str):
                patterns = [_bounded_text(raw_patterns, maximum=240)]
            elif isinstance(raw_patterns, (list, tuple)):
                patterns = [_bounded_text(item, maximum=240) for item in raw_patterns[:10]]
            else:
                patterns = []
            patterns = [item for item in patterns if item]
            if not file_id:
                raise ValueError("fileId is required for knowledge.find")
            if not patterns:
                raise ValueError("patterns are required for knowledge.find")
            payload.update(
                {
                    "fileId": file_id,
                    "patterns": patterns,
                    "useRegex": args.get("useRegex") is True,
                    "caseSensitive": args.get("caseSensitive") is True,
                    "maxWindows": _bounded_int(
                        args.get("maxWindows"), default=8, minimum=1, maximum=20
                    ),
                    "windowSize": _bounded_int(
                        args.get("windowSize"), default=24, minimum=4, maximum=120
                    ),
                    "offset": _bounded_int(args.get("offset"), default=0, minimum=0, maximum=1_000_000),
                }
            )
        elif operation == "open":
            file_id = _bounded_text(args.get("fileId"), maximum=240)
            chunk_id = _bounded_text(args.get("chunkId"), maximum=240)
            if not file_id and not chunk_id:
                raise ValueError("fileId or chunkId is required for knowledge.open")
            if chunk_id:
                payload.update(
                    {
                        "chunkId": chunk_id,
                        "before": _bounded_int(args.get("before"), default=1, minimum=0, maximum=10),
                        "after": _bounded_int(args.get("after"), default=1, minimum=0, maximum=10),
                    }
                )
            else:
                payload.update({
                    "fileId": file_id,
                    "line": _bounded_int(args.get("line"), default=1, minimum=1, maximum=50_000_000),
                    "offset": _bounded_int(
                        args.get("offset"), default=0, minimum=0, maximum=50_000_000
                    ),
                    "windowSize": _bounded_int(
                        args.get("windowSize"), default=180, minimum=1, maximum=300
                    ),
                })

        handler = getattr(client, operation, None)
        if not callable(handler):
            raise ValueError(f"document knowledge operation {operation} is unavailable")
        result = handler(payload)
        if not isinstance(result, Mapping):
            raise ValueError("document knowledge client returned an invalid result")
        safe_result = _safe_knowledge_payload(result)
        if "summary" not in safe_result:
            counts = safe_result.get("items")
            count = len(counts) if isinstance(counts, list) else 0
            summaries = {
                "list_bases": f"已列出 {count} 个可供 Agent 使用的文档知识库",
                "search": f"文档知识库返回 {count} 条引用证据",
                "find": f"在文档内找到 {count} 个匹配窗口",
                "open": "已读取文档引用窗口",
                "status": "已读取文档知识库状态",
            }
            safe_result["summary"] = summaries[operation]
        safe_result.setdefault("untrustedData", True)
        return safe_result

    def _knowledge_management_service(self) -> object:
        control = self.knowledge_control
        if control is None:
            raise ValueError("document knowledge management is unavailable")
        return control

    def _prepare_knowledge_mutation(
        self,
        *,
        session_id: str,
        operation: str,
        args: Mapping[str, object],
        risk_level: str,
    ) -> dict[str, object]:
        control = self._knowledge_management_service()
        changes: list[dict[str, object]] = []
        if operation == "create_base":
            action_payload = _normalize_knowledge_create_args(args)
            base_state: dict[str, object] = {
                "scope": "document_knowledge",
                "mutation": "create_base",
            }
            title = "确认创建文档知识库"
            summary = f"创建知识库《{action_payload['name']}》"
            changes = [
                {
                    "label": "Agent 可检索",
                    "before": "不存在",
                    "after": "开启" if action_payload["agentEnabled"] else "关闭",
                }
            ]
        elif operation == "configure_base":
            base_id = _knowledge_identifier(args.get("kbId"), "kbId")
            current = _knowledge_base_record(
                control.get_base(base_id)  # type: ignore[attr-defined]
            )
            expected_revision = _knowledge_expected_revision(args.get("expectedRevision"))
            _knowledge_require_base_revision(current, expected_revision)
            patch = _normalize_knowledge_configure_args(args, current=current)
            action_payload = {
                "kbId": base_id,
                "expectedRevision": expected_revision,
                "patch": patch,
            }
            base_state = {
                "baseRevision": expected_revision,
                "baseName": str(current.get("name") or base_id),
            }
            title = "确认调整知识库参数"
            summary = f"更新知识库《{base_state['baseName']}》的受控配置"
            changes = _knowledge_configuration_changes(current, patch)
        elif operation == "import_text":
            base_id = _knowledge_identifier(args.get("kbId"), "kbId")
            current = _knowledge_base_record(
                control.get_base(base_id)  # type: ignore[attr-defined]
            )
            expected_revision = _knowledge_expected_revision(args.get("expectedRevision"))
            _knowledge_require_base_revision(current, expected_revision)
            imported = _normalize_knowledge_import_args(args)
            action_payload = {
                "kbId": base_id,
                "expectedRevision": expected_revision,
                **imported,
            }
            base_state = {
                "baseRevision": expected_revision,
                "baseName": str(current.get("name") or base_id),
            }
            title = "确认向文档知识库导入文本"
            summary = (
                f"向《{base_state['baseName']}》导入 {imported['fileName']} "
                f"（{imported['byteSize']} 字节）"
            )
            changes = [
                {
                    "label": "导入内容",
                    "before": "无",
                    "after": f"{imported['fileName']} · sha256:{str(imported['textSha256'])[:12]}",
                }
            ]
        elif operation == "rebuild":
            base_id = _knowledge_identifier(args.get("kbId"), "kbId")
            expected_revision = _knowledge_expected_revision(args.get("expectedRevision"))
            current = _knowledge_base_record(
                control.get_base(base_id)  # type: ignore[attr-defined]
            )
            _knowledge_require_base_revision(current, expected_revision)
            native_preview = control.reindex_preview(base_id)  # type: ignore[attr-defined]
            if not isinstance(native_preview, Mapping):
                raise ValueError("knowledge rebuild preview is invalid")
            preview_revision = _knowledge_expected_revision(
                native_preview.get("configRevision")
            )
            if preview_revision != expected_revision:
                raise ValueError("knowledge rebuild preview is stale")
            preview_token = _knowledge_required_text(
                native_preview.get("previewToken"),
                field="previewToken",
                maximum=240,
            )
            native_payload_sha256 = _knowledge_sha256(
                native_preview.get("payloadSha256"),
                field="payloadSha256",
            )
            action_payload = {
                "kbId": base_id,
                "expectedRevision": expected_revision,
                "previewToken": preview_token,
                "payloadSha256": native_payload_sha256,
            }
            base_state = {
                "baseRevision": expected_revision,
                "baseName": str(current.get("name") or base_id),
                "documentCount": _safe_int(native_preview.get("documentCount")),
                "chunkCount": _safe_int(native_preview.get("chunkCount")),
            }
            title = "确认重建知识库索引"
            summary = (
                f"重建《{base_state['baseName']}》的索引，"
                f"涉及 {base_state['documentCount']} 个文档"
            )
            changes = [
                {
                    "label": "索引版本",
                    "before": expected_revision,
                    "after": "按当前配置重新构建",
                }
            ]
        else:
            raise ValueError("unsupported knowledge mutation")

        digest = _approval_payload_digest(
            session_id=session_id,
            tool="knowledge",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        preview = {
            "title": title,
            "summary": summary,
            "operationLabel": operation,
            "changes": changes,
            "actionPayload": action_payload,
            "baseState": base_state,
        }
        approval = self.sessions.create_approval(
            session_id=session_id,
            tool_name="knowledge",
            operation=operation,
            payload_sha256=digest,
            preview=preview,
            risk_level=risk_level,
            ttl_ms=300_000,
        )
        return {
            "summary": f"等待确认：{summary}",
            "approvalRequired": True,
            "approvalId": approval["approvalId"],
            "approval": approval,
        }

    def _apply_knowledge_mutation(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        operation = str(approval.get("operation") or "")
        preview = (
            approval.get("preview")
            if isinstance(approval.get("preview"), Mapping)
            else {}
        )
        action_payload = (
            preview.get("actionPayload")
            if isinstance(preview.get("actionPayload"), Mapping)
            else {}
        )
        base_state = (
            preview.get("baseState")
            if isinstance(preview.get("baseState"), Mapping)
            else {}
        )
        expected_digest = _approval_payload_digest(
            session_id=str(approval.get("sessionId") or ""),
            tool="knowledge",
            operation=operation,
            action_payload=action_payload,
            base_state=base_state,
        )
        if not self._approval_payload_matches(approval, expected_digest):
            raise ValueError("knowledge approval no longer matches its preview")

        control = self._knowledge_management_service()
        if operation == "create_base":
            result = control.create_base(dict(action_payload))  # type: ignore[attr-defined]
        else:
            base_id = _knowledge_identifier(action_payload.get("kbId"), "kbId")
            expected_revision = _knowledge_expected_revision(
                action_payload.get("expectedRevision")
            )
            session_id = str(approval.get("sessionId") or "")
            state_fence_applies = self._approval_preview_state_fence_applies(session_id)
            current = _knowledge_base_record(
                control.get_base(base_id)  # type: ignore[attr-defined]
            )
            current_revision = _knowledge_base_revision(current)
            if state_fence_applies and current_revision != expected_revision:
                raise ValueError(
                    "knowledge base changed after the approval preview was created"
                )
            if operation == "configure_base":
                patch = (
                    dict(action_payload["patch"])
                    if isinstance(action_payload.get("patch"), Mapping)
                    else {}
                )
                result = control.update_base(  # type: ignore[attr-defined]
                    base_id,
                    {
                        **patch,
                        "expectedRevision": (
                            expected_revision if state_fence_applies else current_revision
                        ),
                    },
                )
            elif operation == "import_text":
                text_value = action_payload.get("text")
                if not isinstance(text_value, str):
                    raise ValueError("knowledge import text is missing from its preview")
                raw = text_value.encode("utf-8")
                if len(raw) != _safe_int(action_payload.get("byteSize")):
                    raise ValueError("knowledge import byte size no longer matches its preview")
                if hashlib.sha256(raw).hexdigest() != str(
                    action_payload.get("textSha256") or ""
                ):
                    raise ValueError("knowledge import digest no longer matches its preview")
                result = control.import_document(  # type: ignore[attr-defined]
                    base_id,
                    data=raw,
                    file_name=_knowledge_required_text(
                        action_payload.get("fileName"),
                        field="fileName",
                        maximum=240,
                    ),
                    mime_type=_knowledge_required_text(
                        action_payload.get("mimeType"),
                        field="mimeType",
                        maximum=200,
                    ),
                    parser_provider=_knowledge_parser_provider(
                        action_payload.get("parserProvider")
                    ),
                )
            elif operation == "rebuild":
                rebuild_payload: dict[str, object] = {
                    "expectedRevision": expected_revision,
                    "previewToken": _knowledge_required_text(
                        action_payload.get("previewToken"),
                        field="previewToken",
                        maximum=240,
                    ),
                    "payloadSha256": _knowledge_sha256(
                        action_payload.get("payloadSha256"),
                        field="payloadSha256",
                    ),
                    "confirmText": "REBUILD",
                }
                if not state_fence_applies:
                    fresh_preview = control.reindex_preview(base_id)  # type: ignore[attr-defined]
                    if not isinstance(fresh_preview, Mapping):
                        raise ValueError("knowledge rebuild preview is invalid")
                    rebuild_payload.update(
                        {
                            "expectedRevision": _knowledge_expected_revision(
                                fresh_preview.get("configRevision")
                            ),
                            "previewToken": _knowledge_required_text(
                                fresh_preview.get("previewToken"),
                                field="previewToken",
                                maximum=240,
                            ),
                            "payloadSha256": _knowledge_sha256(
                                fresh_preview.get("payloadSha256"),
                                field="payloadSha256",
                            ),
                        }
                    )
                result = control.rebuild(  # type: ignore[attr-defined]
                    base_id,
                    rebuild_payload,
                )
            else:
                raise ValueError("unsupported approved knowledge mutation")
        if not isinstance(result, Mapping):
            raise ValueError("knowledge management returned an invalid mutation receipt")
        preview_summary = _bounded_text(preview.get("summary"), maximum=1_000)
        return {
            **_safe_knowledge_payload(result),
            "summary": (
                f"已完成：{preview_summary}"
                if preview_summary
                else "知识库操作已完成"
            ),
            "mutationApplied": True,
            "operation": operation,
            "approvalId": str(approval.get("approvalId") or ""),
            "auditId": str(approval.get("approvalId") or ""),
            # Document Knowledge is an independent evidence domain.  A worker
            # response must never opt its mutation receipt into personal
            # Memory, even if an accidental or untrusted field says otherwise.
            "memoryDomain": "document_knowledge",
            "personalMemoryEligible": False,
        }

    def _models(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        if operation == "status":
            payload = self._facade_call("models_status")
            return {"summary": _model_status_summary(payload), "status": _safe_payload(payload)}
        if operation == "profiles":
            snapshot = self._model_profile_snapshot()
            return {
                "summary": "已读取即时补全与知识模型的非密钥 Provider 配置",
                "profiles": _safe_payload(snapshot["profiles"]),
                "configurationHash": snapshot["configurationHash"],
                "secretsVisible": False,
            }
        if operation == "profile_preview":
            allowed = {"op", "slot", "provider", "endpoint", "model", "_sessionId"}
            unknown = sorted(str(key) for key in set(args) - allowed)
            if unknown:
                raise ValueError(f"unsupported model profile field: {unknown[0]}")
            snapshot = self._model_profile_snapshot()
            profiles = snapshot["profiles"] if isinstance(snapshot.get("profiles"), Mapping) else {}
            slot = _bounded_text(args.get("slot"), maximum=40)
            current = profiles.get(slot) if isinstance(profiles.get(slot), Mapping) else {}
            desired = self._normalize_model_profile(slot=slot, requested=args, current=current)
            changes = [
                {
                    "label": {"provider": "服务", "endpoint": "服务地址", "model": "模型"}[key],
                    "path": f"{slot}.{key}",
                    "before": current.get(key, ""),
                    "after": desired[key],
                }
                for key in ("provider", "endpoint", "model")
                if current.get(key, "") != desired[key]
            ]
            return {
                "summary": "配置已匹配，无需修改" if not changes else f"将更新 {len(changes)} 项 Provider 配置",
                "slot": slot,
                "changes": changes,
                "configurationHash": snapshot["configurationHash"],
                "restartComponent": "predictor" if slot == "instant" else "sidecar",
                "approvalRequiredForApply": bool(changes),
                "secretsPreserved": True,
            }
        if operation == "probe":
            payload = self._facade_call("model_probe", {})
            return {"summary": "已完成只读模型能力探测", "probe": _safe_payload(payload)}
        stats = getattr(self.core, "suggestion_cache_stats", None)
        payload = stats() if callable(stats) else {"available": False}
        return {"summary": "已读取本地候选缓存统计", "cache": _safe_payload(payload)}

    def _runtime(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        del args
        if operation == "components":
            payload = self.management.runtime_components()
            return {"summary": "已读取运行组件列表", "runtime": _safe_payload(payload)}
        status = self.management.runtime_status()
        components = status.get("components") if isinstance(status.get("components"), Mapping) else {}
        unhealthy = [
            str(key)
            for key, value in components.items()
            if isinstance(value, Mapping) and value.get("ok") is not True
        ]
        if operation == "diagnose":
            return {
                "summary": "未发现异常" if not unhealthy else f"发现 {len(unhealthy)} 个未就绪组件",
                "unhealthyComponents": unhealthy,
                "components": _safe_payload(components),
                "mutationsAvailable": False,
            }
        return {"summary": "运行时状态已读取", "runtime": _safe_payload(status)}

    def _configuration(self, operation: str, args: Mapping[str, object]) -> dict[str, object]:
        limit = _bounded_int(args.get("limit"), default=20, minimum=1, maximum=50)
        query = _bounded_text(args.get("query"), maximum=240)
        if operation == "history":
            history = self.management.history_page(page_request({"query": query, "limit": limit}))
            items = history.get("items", []) if isinstance(history, Mapping) else []
            return {
                "summary": f"读取 {len(items)} 条隐私化历史摘要",
                "items": _safe_payload(items),
                "rawTextVisible": False,
                "nextCursor": _bounded_text(history.get("nextCursor"), maximum=80),
            }
        if operation == "export_preview":
            overview = self.management.overview()
            memory = overview.get("memory") if isinstance(overview.get("memory"), Mapping) else {}
            return {
                "summary": "可导出数据库、非敏感设置和安全的 Rime 配置",
                "scope": ["database", "redacted_settings", "safe_rime_configuration"],
                "memoryBookCount": _safe_int(memory.get("memoryBookCount")),
                "secretsIncluded": False,
                "modelFilesIncluded": False,
                "approvalRequiredForExport": True,
            }
        if operation == "restore_preview":
            source_approval_id = _bounded_text(args.get("sourceApprovalId"), maximum=240)
            _source, target = self._configuration_export_source(
                session_id=_bounded_text(args.get("sessionId"), maximum=240)
                or _bounded_text(args.get("_sessionId"), maximum=240),
                source_approval_id=source_approval_id,
            )
            payload = self.management.portable_restore_preview({"path": str(target)})
            return {
                "summary": f"已验证备份 {target.name}，恢复会替换数据库、设置和 Rime 配置",
                "sourceApprovalId": source_approval_id,
                "fileName": target.name,
                "valid": payload.get("valid") is True,
                "createdAtMs": _safe_int(payload.get("createdAtMs")),
                "databaseCounts": _safe_payload(payload.get("databaseCounts")),
                "databaseMigrationVersion": _safe_int(payload.get("databaseMigrationVersion")),
                "rimeFileCount": _safe_int(payload.get("rimeFileCount")),
                "requiresRestart": payload.get("requiresRestart") is True,
                "restoreApplyAvailable": True,
                "restoreApplyRisk": "R3",
                "externalSupervisorRequired": True,
            }
        payload = self._facade_call(
            "management_audit",
            {"limit": limit, "action": _bounded_text(args.get("action"), maximum=120)},
        )
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        return {"summary": f"读取 {len(items)} 条管理审计记录", "items": _safe_payload(items)}

    def _facade_call(self, name: str, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        target = getattr(self.facade, name, None)
        if not callable(target):
            raise ValueError(f"control capability is unavailable: {name}")
        result = target(dict(payload)) if payload is not None else target()
        if not isinstance(result, Mapping):
            raise ValueError(f"control capability returned an invalid payload: {name}")
        return dict(result)

    def _catalog(
        self,
        args: Mapping[str, object],
        *,
        visible_owners: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        query = _bounded_text(args.get("query"), maximum=240)
        limit = _bounded_int(args.get("limit"), default=5, minimum=1, maximum=8)
        request = page_request(
            {
                "query": query,
                "limit": limit,
                "visibleOwners": _owner_payloads(visible_owners),
            }
        )
        books = self.management.memory_page("books", request).get("items", [])
        groups = self.management.memory_page("groups", request).get("items", [])
        tags = self.management.memory_page("tags", request).get("items", [])
        safe_books = [_catalog_book(item) for item in books if isinstance(item, Mapping)]
        safe_groups = [_catalog_group(item) for item in groups if isinstance(item, Mapping)]
        safe_tags = [_catalog_tag(item) for item in tags if isinstance(item, Mapping)]
        items = [*safe_books, *safe_groups, *safe_tags]
        return {
            "summary": (
                f"查询到 {len(safe_books)} 本工具书、"
                f"{len(safe_groups)} 个 Group、{len(safe_tags)} 个 Tag"
            ),
            "query": query,
            "counts": {
                "books": len(safe_books),
                "groups": len(safe_groups),
                "tags": len(safe_tags),
            },
            "items": items,
        }

    def _read(
        self,
        args: Mapping[str, object],
        *,
        visible_owners: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        book_id = _bounded_text(args.get("bookId"), maximum=240)
        if not book_id:
            raise ValueError("bookId is required for memory.read")
        report = self.management.memory_page(
            "books",
            page_request(
                {
                    "limit": 500,
                    "visibleOwners": _owner_payloads(visible_owners),
                }
            ),
        )
        match = next(
            (
                item
                for item in report.get("items", [])
                if isinstance(item, Mapping) and str(item.get("id") or "") == book_id
            ),
            None,
        )
        if match is None:
            raise ValueError("memory book not found")
        memories = [
            {
                "memoryId": _bounded_text(item.get("id"), maximum=240),
                "type": _bounded_text(item.get("type"), maximum=80),
                "text": _bounded_text(item.get("text"), maximum=1200),
                "updatedAtMs": _safe_int(item.get("updatedAtMs")),
                **(
                    {
                        "ref": _memory_reference(
                            "atom",
                            _bounded_text(item.get("id"), maximum=240),
                            legacy_type="memory_atom",
                        )
                    }
                    if _bounded_text(item.get("id"), maximum=240)
                    else {}
                ),
            }
            for item in match.get("memories", [])
            if isinstance(item, Mapping)
        ][:20]
        title = _bounded_text(match.get("title"), maximum=180)
        book_ref = _coerce_memory_reference(
            match.get("ref"),
            fallback_kind="book",
            fallback_id=book_id,
            legacy_type="book",
        )
        evidence_refs = _safe_memory_references(match.get("evidenceRefs"), limit=80)
        return {
            "summary": f"已读取工具书《{title or '未命名'}》，包含 {len(memories)} 条相关记忆",
            "book": {
                "bookId": book_id,
                "title": title,
                "summary": _bounded_text(match.get("summary"), maximum=1600),
                "type": _bounded_text(match.get("type"), maximum=80),
                "tags": _string_list(match.get("tags"), limit=20),
                "sourceStartMs": _safe_int(match.get("sourceStartMs")),
                "sourceEndMs": _safe_int(match.get("sourceEndMs")),
                "memories": memories,
                "ref": book_ref,
                "evidenceRefs": evidence_refs,
            },
            "items": [{"title": title, "kind": "book", "ref": book_ref}],
        }

    def _recent(
        self,
        args: Mapping[str, object],
        *,
        visible_owners: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        query = _bounded_text(args.get("query"), maximum=240)
        limit = _bounded_int(args.get("limit"), default=8, minimum=1, maximum=12)
        raw_items: list[Mapping[str, object]] = []
        for disposition in ("remember", "consolidated"):
            report = self.management.memory_page(
                "evidence",
                page_request(
                    {
                        "query": query,
                        "limit": limit,
                        "status": disposition,
                        "visibleOwners": _owner_payloads(visible_owners),
                    }
                ),
            )
            raw_items.extend(
                item
                for item in report.get("items", [])
                if isinstance(item, Mapping)
            )

        seen: set[str] = set()
        items: list[dict[str, object]] = []
        for item in sorted(
            raw_items,
            key=lambda value: (
                _safe_int(value.get("createdAtMs")),
                _bounded_text(value.get("id") or value.get("itemId"), maximum=240),
            ),
            reverse=True,
        ):
            source_id = _bounded_text(
                item.get("id") or item.get("itemId"),
                maximum=240,
            )
            disposition = _bounded_text(
                item.get("disposition") or item.get("status"),
                maximum=40,
            )
            text = _bounded_text(item.get("text"), maximum=1200)
            if (
                not source_id
                or source_id in seen
                or disposition not in {"remember", "consolidated"}
                or item.get("sensitive") is True
                or not text
            ):
                continue
            seen.add(source_id)
            raw_source = item.get("source")
            source_ref = (
                _safe_memory_source(raw_source)
                if isinstance(raw_source, Mapping)
                else {}
            )
            legacy_source = (
                _bounded_text(item.get("transportSource"), maximum=80)
                if source_ref
                else _bounded_text(raw_source, maximum=80)
            )
            ref = _coerce_memory_reference(
                item.get("ref"),
                fallback_kind="evidence",
                fallback_id=source_id,
                legacy_type="evidence",
            )
            items.append(
                {
                    "sourceId": source_id,
                    "createdAtMs": _safe_int(item.get("createdAtMs")),
                    # Keep the historical string field while exposing the new
                    # structured source and actionable stable reference.
                    "source": legacy_source,
                    "sourceRef": source_ref,
                    "ref": ref,
                    "evidenceRefs": _safe_memory_references(
                        item.get("evidenceRefs"),
                        limit=40,
                    ),
                    "sourceKind": _bounded_text(item.get("type"), maximum=80),
                    "text": text,
                    "app": _bounded_text(item.get("app"), maximum=160),
                    "project": _bounded_text(item.get("project"), maximum=160),
                    "ownerKind": _bounded_text(item.get("ownerKind"), maximum=40),
                    "ownerId": _bounded_text(item.get("ownerId"), maximum=160),
                    "disposition": disposition,
                }
            )
            if len(items) >= limit:
                break
        return {
            "summary": f"召回 {len(items)} 段已治理记忆证据",
            "query": query,
            "count": len(items),
            "items": items,
        }

    def _memory_owner_context(
        self,
        session_id: str,
    ) -> tuple[tuple[tuple[str, str], ...], tuple[str, str]]:
        if not session_id:
            raise ValueError("memory tool session is missing")
        session = self.sessions.get(session_id)
        role_id = _bounded_text(session.get("roleId"), maximum=160)
        mutable_owner = ("agent", role_id) if role_id else ("session", session_id)
        room_ids: tuple[str, ...] = ()
        rooms = getattr(self.collaboration, "rooms", None)
        participant_for_session = getattr(rooms, "participant_for_session", None)
        if callable(participant_for_session):
            participant = participant_for_session(session_id)
            if isinstance(participant, Mapping):
                room_id = _bounded_text(participant.get("roomId"), maximum=240)
                if room_id:
                    room_ids = (room_id,)
        visible = agent_visible_memory_owners(
            project=self.project,
            role_id=role_id,
            session_id=session_id,
            room_ids=room_ids,
        )
        return visible, mutable_owner


def _catalog_book(item: Mapping[str, object]) -> dict[str, object]:
    book_id = _bounded_text(item.get("id"), maximum=240)
    return {
        "kind": "book",
        "bookId": book_id,
        "title": _bounded_text(item.get("title"), maximum=180),
        "summary": _bounded_text(item.get("summary"), maximum=800),
        "tags": _string_list(item.get("tags"), limit=20),
        "atomCount": _safe_int(item.get("atomCount")),
        "updatedAtMs": _safe_int(item.get("updated_at_ms") or item.get("updatedAtMs")),
        "ownerKind": _bounded_text(item.get("ownerKind"), maximum=40),
        "ownerId": _bounded_text(item.get("ownerId"), maximum=160),
        "ref": _coerce_memory_reference(
            item.get("ref"),
            fallback_kind="book",
            fallback_id=book_id,
            legacy_type="book",
        ),
        "evidenceRefs": _safe_memory_references(
            item.get("evidenceRefs"),
            limit=80,
        ),
    }


_MEMORY_REFERENCE_KINDS = frozenset(
    {"event", "evidence", "atom", "book", "timeline", "role_book_revision"}
)


def _memory_reference(
    kind: str,
    reference_id: str,
    *,
    legacy_type: str = "",
    label: str = "",
) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": kind,
        "id": reference_id,
        "referenceKind": kind,
        "referenceId": reference_id,
    }
    if legacy_type:
        value["type"] = legacy_type
    if label:
        value["label"] = label
    return value


def _coerce_memory_reference(
    value: object,
    *,
    fallback_kind: str,
    fallback_id: str,
    legacy_type: str,
) -> dict[str, object]:
    source = value if isinstance(value, Mapping) else {}
    kind = _bounded_text(
        source.get("referenceKind") or source.get("kind") or fallback_kind,
        maximum=40,
    )
    if kind not in _MEMORY_REFERENCE_KINDS:
        kind = fallback_kind
    reference_id = _bounded_text(
        source.get("referenceId") or source.get("id") or fallback_id,
        maximum=240,
    )
    if not reference_id:
        return {}
    return _memory_reference(
        kind,
        reference_id,
        legacy_type=_bounded_text(source.get("type"), maximum=80) or legacy_type,
        label=_bounded_text(source.get("label"), maximum=180),
    )


def _safe_memory_references(value: object, *, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for raw in value[: max(0, limit)]:
        if not isinstance(raw, Mapping):
            continue
        kind = _bounded_text(
            raw.get("referenceKind") or raw.get("kind"),
            maximum=40,
        )
        reference_id = _bounded_text(
            raw.get("referenceId") or raw.get("id"),
            maximum=240,
        )
        identity = (kind, reference_id)
        if kind not in _MEMORY_REFERENCE_KINDS or not reference_id or identity in seen:
            continue
        seen.add(identity)
        result.append(
            _memory_reference(
                kind,
                reference_id,
                legacy_type=_bounded_text(raw.get("type"), maximum=80) or kind,
                label=_bounded_text(raw.get("label"), maximum=180),
            )
        )
    return result


def _safe_memory_source(value: Mapping[str, object]) -> dict[str, object]:
    kind = _bounded_text(value.get("kind") or value.get("type"), maximum=80)
    source_id = _bounded_text(value.get("id"), maximum=320)
    if not kind or not source_id:
        return {}
    result: dict[str, object] = {"kind": kind, "id": source_id}
    source_kind = _bounded_text(value.get("sourceKind"), maximum=120)
    if source_kind:
        result["sourceKind"] = source_kind
    legacy_type = _bounded_text(value.get("type"), maximum=80)
    if legacy_type:
        result["type"] = legacy_type
    return result


def _compact_memory_run_for_agent(run: Mapping[str, object]) -> dict[str, object]:
    return {
        "runId": _bounded_text(run.get("runId"), maximum=240),
        "createdAtMs": _safe_int(run.get("createdAtMs")),
        "status": _bounded_text(run.get("status"), maximum=40),
        "summary": _bounded_text(run.get("summary"), maximum=240),
        "diffCount": _safe_int(run.get("diffCount")),
        "pendingDiffCount": _safe_int(run.get("pendingDiffCount")),
        "appliedDiffCount": _safe_int(run.get("appliedDiffCount")),
        "operationCounts": _safe_payload(run.get("operationCounts")),
        "sourceCursor": _safe_payload(run.get("sourceCursor")),
    }


def _catalog_group(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "kind": "group",
        "title": _bounded_text(item.get("title"), maximum=180),
        "summary": _bounded_text(item.get("note"), maximum=600),
        "tags": _string_list(item.get("tags"), limit=20),
        "itemCount": _safe_int(item.get("event_count")),
        "updatedAtMs": _safe_int(item.get("updated_at_ms") or item.get("latestAtMs")),
    }


def _catalog_tag(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "kind": "tag",
        "title": _bounded_text(item.get("tag"), maximum=180),
        "summary": _bounded_text(item.get("description"), maximum=600),
        "itemCount": _safe_int(item.get("item_count")),
        "updatedAtMs": _safe_int(item.get("updated_at_ms") or item.get("updatedAtMs")),
    }


def _owner_payloads(
    owners: tuple[tuple[str, str], ...],
) -> list[dict[str, str]]:
    return [
        {"ownerKind": owner_kind, "ownerId": owner_id}
        for owner_kind, owner_id in owners
    ]


def _personal_memory_curation_owner(
    visible_owners: tuple[tuple[str, str], ...],
    *,
    fallback: tuple[str, str],
) -> tuple[str, str]:
    personal_owner = ("user", "default")
    return personal_owner if personal_owner in visible_owners else fallback


def _memory_mutation_owners(
    visible_owners: tuple[tuple[str, str], ...],
    *,
    mutable_owner: tuple[str, str],
) -> frozenset[tuple[str, str]]:
    owners = {mutable_owner}
    personal_owner = ("user", "default")
    if personal_owner in visible_owners:
        owners.add(personal_owner)
    return frozenset(owners)


def _memory_run_owner(run: Mapping[str, object]) -> tuple[str, str]:
    owner = (
        _bounded_text(run.get("ownerKind"), maximum=40),
        _bounded_text(run.get("ownerId"), maximum=160),
    )
    if not all(owner):
        raise ValueError("memory run owner is unavailable")
    return owner


def _require_memory_run_owner(
    run: Mapping[str, object],
    expected_owner: tuple[str, str],
) -> None:
    actual = _memory_run_owner(run)
    if actual != expected_owner:
        raise ValueError("memory run is outside the current role")


def _require_memory_run_visible(
    run: Mapping[str, object],
    visible_owners: tuple[tuple[str, str], ...],
) -> None:
    if _memory_run_owner(run) not in visible_owners:
        raise ValueError("memory run is outside the current visible owner scope")


def _memory_curation_error(payload: Mapping[str, object]) -> str:
    validation = payload.get("validation")
    errors = validation.get("errors") if isinstance(validation, Mapping) else None
    if isinstance(errors, (list, tuple)):
        messages = [
            _bounded_text(item, maximum=160)
            for item in errors
            if _bounded_text(item, maximum=160)
        ]
        if messages:
            return "; ".join(messages)[:240]
    message = _bounded_text(errors, maximum=240)
    return message or _bounded_text(payload.get("error"), maximum=240) or "memory draft generation failed validation"


def _memory_curation_skip_reason(payload: Mapping[str, object]) -> str:
    curation = payload.get("curation")
    results = curation.get("results") if isinstance(curation, Mapping) else None
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, Mapping):
                continue
            reason = _bounded_text(item.get("reason"), maximum=80)
            if reason:
                return reason
    return "no_durable_changes"


def _safe_lexicon_review_entry(item: Mapping[str, object]) -> dict[str, object]:
    text = _bounded_text(item.get("text"), maximum=120)
    pinyin = _bounded_text(item.get("pinyin"), maximum=240)
    review_key = str(item.get("reviewKey") or f"{text}\t{pinyin}").strip()[:300]
    return {
        "text": text,
        "pinyin": pinyin,
        "reviewKey": review_key,
        "selected": item.get("selected") is not False,
        "weight": _safe_int(item.get("weight")),
        "positiveCount": _safe_int(item.get("positiveCount")),
        "reviewSource": _bounded_text(item.get("reviewSource"), maximum=80),
        "reviewReason": _bounded_text(item.get("reviewReason"), maximum=240),
    }


_KNOWLEDGE_CHUNKING_DEFAULTS: dict[str, object] = {
    "strategy": "markdown",
    "size": 1_200,
    "overlap": 160,
    "separator": "\n\n",
    "respectHeadings": True,
    "respectPageBoundaries": True,
}
_KNOWLEDGE_RETRIEVAL_DEFAULTS: dict[str, object] = {
    "mode": "hybrid",
    "topK": 10,
    "threshold": 0.0,
    "lexicalWeight": 1.0,
    "denseWeight": 1.0,
    "graphEnabled": True,
    "graphWeight": 0.7,
    "rrfK": 60,
    "candidateMultiplier": 4,
    "rerankEnabled": False,
    "rerankCandidateDepth": 40,
}


def _normalize_knowledge_create_args(
    args: Mapping[str, object],
) -> dict[str, object]:
    allowed = {
        "op", "name", "description", "agentEnabled", "parserProvider",
        "chunkingConfig", "retrievalConfig",
    }
    _knowledge_reject_unknown(args, allowed, "create_base")
    payload: dict[str, object] = {
        "name": _knowledge_required_text(args.get("name"), field="name", maximum=300),
        "description": _knowledge_optional_text(
            args.get("description"), field="description", maximum=4_000
        ),
        "agentEnabled": _knowledge_optional_bool(args, "agentEnabled", default=False),
        "parserProvider": _knowledge_parser_provider(args.get("parserProvider")),
    }
    if args.get("chunkingConfig") is not None:
        payload["chunkingConfig"] = _normalize_knowledge_chunking_config(
            args.get("chunkingConfig")
        )
    if args.get("retrievalConfig") is not None:
        payload["retrievalConfig"] = _normalize_knowledge_retrieval_config(
            args.get("retrievalConfig")
        )
    return payload


def _normalize_knowledge_configure_args(
    args: Mapping[str, object],
    *,
    current: Mapping[str, object],
) -> dict[str, object]:
    allowed = {
        "op", "kbId", "expectedRevision", "name", "description", "agentEnabled",
        "parserProvider", "chunkingConfig", "retrievalConfig",
    }
    _knowledge_reject_unknown(args, allowed, "configure_base")
    patch: dict[str, object] = {}
    if "name" in args:
        patch["name"] = _knowledge_required_text(
            args.get("name"), field="name", maximum=300
        )
    if "description" in args:
        patch["description"] = _knowledge_optional_text(
            args.get("description"), field="description", maximum=4_000
        )
    if "agentEnabled" in args:
        patch["agentEnabled"] = _knowledge_optional_bool(
            args, "agentEnabled", default=False
        )
    if "parserProvider" in args:
        patch["parserProvider"] = _knowledge_parser_provider(args.get("parserProvider"))
    if "chunkingConfig" in args:
        defaults = (
            current.get("chunkingConfig")
            if isinstance(current.get("chunkingConfig"), Mapping)
            else _KNOWLEDGE_CHUNKING_DEFAULTS
        )
        patch["chunkingConfig"] = _normalize_knowledge_chunking_config(
            args.get("chunkingConfig"), defaults=defaults
        )
    if "retrievalConfig" in args:
        defaults = (
            current.get("retrievalConfig")
            if isinstance(current.get("retrievalConfig"), Mapping)
            else _KNOWLEDGE_RETRIEVAL_DEFAULTS
        )
        patch["retrievalConfig"] = _normalize_knowledge_retrieval_config(
            args.get("retrievalConfig"), defaults=defaults
        )
    if not patch:
        raise ValueError("knowledge.configure_base requires at least one change")
    return patch


def _normalize_knowledge_import_args(
    args: Mapping[str, object],
) -> dict[str, object]:
    allowed = {"op", "kbId", "expectedRevision", "fileName", "text", "parserProvider"}
    _knowledge_reject_unknown(args, allowed, "import_text")
    file_name = _knowledge_required_text(
        args.get("fileName"), field="fileName", maximum=240
    )
    if file_name in {".", ".."} or "/" in file_name or "\\" in file_name:
        raise ValueError("fileName must not contain a path")
    text_value = args.get("text")
    if not isinstance(text_value, str) or not text_value.strip():
        raise ValueError("text is required for knowledge.import_text")
    raw = text_value.encode("utf-8")
    if len(text_value) > 262_144 or len(raw) > 262_144:
        raise ValueError("knowledge.import_text is limited to 262144 UTF-8 bytes")
    suffix = Path(file_name).suffix.lower()
    mime_type = {
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".csv": "text/csv",
        ".json": "application/json",
    }.get(suffix, "text/plain")
    return {
        "fileName": file_name,
        "text": text_value,
        "byteSize": len(raw),
        "textSha256": hashlib.sha256(raw).hexdigest(),
        "mimeType": mime_type,
        "parserProvider": _knowledge_parser_provider(args.get("parserProvider")),
    }


def _normalize_knowledge_chunking_config(
    value: object,
    *,
    defaults: Mapping[str, object] = _KNOWLEDGE_CHUNKING_DEFAULTS,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("chunkingConfig must be an object")
    allowed = set(_KNOWLEDGE_CHUNKING_DEFAULTS)
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise ValueError(f"unsupported chunkingConfig field: {unknown[0]}")
    if not value:
        raise ValueError("chunkingConfig must contain at least one field")
    merged = {**_KNOWLEDGE_CHUNKING_DEFAULTS, **dict(defaults), **dict(value)}
    strategy = str(merged.get("strategy") or "").strip().lower()
    if strategy not in {"general", "markdown", "book", "qa", "laws", "separator", "fixed"}:
        raise ValueError("unsupported knowledge chunking strategy")
    size = _knowledge_strict_int(
        merged.get("size"), field="chunkingConfig.size", minimum=200, maximum=8_000
    )
    overlap = _knowledge_strict_int(
        merged.get("overlap"), field="chunkingConfig.overlap", minimum=0, maximum=2_000
    )
    if overlap >= size:
        raise ValueError("chunkingConfig.overlap must be smaller than size")
    separator = merged.get("separator")
    if not isinstance(separator, str) or len(separator) > 100:
        raise ValueError("chunkingConfig.separator must be a string of at most 100 characters")
    if strategy == "separator" and not separator:
        raise ValueError("separator chunking requires a non-empty separator")
    result: dict[str, object] = {
        "strategy": strategy, "size": size, "overlap": overlap, "separator": separator,
    }
    for field in ("respectHeadings", "respectPageBoundaries"):
        if not isinstance(merged.get(field), bool):
            raise ValueError(f"chunkingConfig.{field} must be boolean")
        result[field] = merged[field]
    return result


def _normalize_knowledge_retrieval_config(
    value: object,
    *,
    defaults: Mapping[str, object] = _KNOWLEDGE_RETRIEVAL_DEFAULTS,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("retrievalConfig must be an object")
    allowed = set(_KNOWLEDGE_RETRIEVAL_DEFAULTS)
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise ValueError(f"unsupported retrievalConfig field: {unknown[0]}")
    if not value:
        raise ValueError("retrievalConfig must contain at least one field")
    merged = {**_KNOWLEDGE_RETRIEVAL_DEFAULTS, **dict(defaults), **dict(value)}
    mode = str(merged.get("mode") or "").strip().lower()
    if mode not in {"hybrid", "lexical", "dense"}:
        raise ValueError("retrievalConfig.mode must be hybrid, lexical, or dense")
    lexical_weight = _knowledge_strict_float(
        merged.get("lexicalWeight"), field="retrievalConfig.lexicalWeight",
        minimum=0.0, maximum=10.0,
    )
    dense_weight = _knowledge_strict_float(
        merged.get("denseWeight"), field="retrievalConfig.denseWeight",
        minimum=0.0, maximum=10.0,
    )
    if lexical_weight + dense_weight <= 0.0:
        raise ValueError("retrievalConfig lexical and dense weights cannot both be zero")
    graph_enabled = merged.get("graphEnabled")
    if not isinstance(graph_enabled, bool):
        raise ValueError("retrievalConfig.graphEnabled must be boolean")
    rerank_enabled = merged.get("rerankEnabled")
    if not isinstance(rerank_enabled, bool):
        raise ValueError("retrievalConfig.rerankEnabled must be boolean")
    top_k = _knowledge_strict_int(
        merged.get("topK"), field="retrievalConfig.topK", minimum=1, maximum=100
    )
    rerank_candidate_depth = _knowledge_strict_int(
        merged.get("rerankCandidateDepth"),
        field="retrievalConfig.rerankCandidateDepth",
        minimum=1,
        maximum=100,
    )
    if rerank_enabled and top_k > 20:
        raise ValueError("retrievalConfig.topK must not exceed 20 when rerank is enabled")
    if rerank_enabled and rerank_candidate_depth < top_k:
        raise ValueError("retrievalConfig.rerankCandidateDepth must be at least topK")
    return {
        "mode": mode,
        "topK": top_k,
        "threshold": _knowledge_strict_float(
            merged.get("threshold"), field="retrievalConfig.threshold", minimum=0.0, maximum=1.0
        ),
        "lexicalWeight": lexical_weight,
        "denseWeight": dense_weight,
        "graphEnabled": graph_enabled,
        "graphWeight": _knowledge_strict_float(
            merged.get("graphWeight"), field="retrievalConfig.graphWeight", minimum=0.0, maximum=10.0
        ),
        "rrfK": _knowledge_strict_int(
            merged.get("rrfK"), field="retrievalConfig.rrfK", minimum=1, maximum=1_000
        ),
        "candidateMultiplier": _knowledge_strict_int(
            merged.get("candidateMultiplier"), field="retrievalConfig.candidateMultiplier",
            minimum=1, maximum=20,
        ),
        "rerankEnabled": rerank_enabled,
        "rerankCandidateDepth": rerank_candidate_depth,
    }


def _knowledge_configuration_changes(
    current: Mapping[str, object],
    patch: Mapping[str, object],
) -> list[dict[str, object]]:
    labels = {
        "name": "名称",
        "description": "描述",
        "agentEnabled": "Agent 可检索",
        "parserProvider": "解析器",
        "chunkingConfig": "Chunk 参数",
        "retrievalConfig": "检索参数",
    }
    return [
        {
            "label": labels.get(key, key),
            "before": current.get("parser") if key == "parserProvider" else current.get(key),
            "after": value,
        }
        for key, value in patch.items()
    ]


def _knowledge_reject_unknown(
    args: Mapping[str, object],
    allowed: set[str],
    operation: str,
) -> None:
    unknown = sorted(str(key) for key in set(args) - allowed)
    if unknown:
        raise ValueError(f"unsupported knowledge.{operation} field: {unknown[0]}")


def _knowledge_base_record(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("knowledge base snapshot is unavailable")
    base = value.get("base") if isinstance(value.get("base"), Mapping) else value
    if not isinstance(base, Mapping):
        raise ValueError("knowledge base snapshot is unavailable")
    result = dict(base)
    if not str(result.get("id") or result.get("kbId") or "").strip():
        raise ValueError("knowledge base snapshot has no identifier")
    return result


def _knowledge_base_revision(base: Mapping[str, object]) -> int:
    return _knowledge_expected_revision(
        base.get("revision")
        if base.get("revision") is not None
        else base.get("configRevision")
    )


def _knowledge_require_base_revision(
    base: Mapping[str, object], expected_revision: int
) -> None:
    if _knowledge_base_revision(base) != expected_revision:
        raise ValueError("knowledge base revision does not match the requested snapshot")


def _knowledge_expected_revision(value: object) -> int:
    return _knowledge_strict_int(
        value, field="expectedRevision", minimum=1, maximum=2_147_483_647
    )


def _knowledge_identifier(value: object, field: str) -> str:
    identifier = _knowledge_required_text(value, field=field, maximum=160)
    if not all(character.isalnum() or character in "._:-" for character in identifier):
        raise ValueError(f"invalid {field}")
    return identifier


def _knowledge_required_text(value: object, *, field: str, maximum: int) -> str:
    result = _knowledge_optional_text(value, field=field, maximum=maximum)
    if not result:
        raise ValueError(f"{field} is required")
    return result


def _knowledge_optional_text(value: object, *, field: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL")
    result = " ".join(value.split()).strip()
    if len(result) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return result


def _knowledge_optional_bool(
    args: Mapping[str, object], field: str, *, default: bool
) -> bool:
    if field not in args:
        return default
    value = args.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


def _knowledge_parser_provider(value: object) -> str:
    provider = str(value or "auto").strip().lower()
    if provider not in {"auto", "builtin", "mineru"}:
        raise ValueError("parserProvider must be auto, builtin, or mineru")
    return provider


def _knowledge_sha256(value: object, *, field: str) -> str:
    digest = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{field} must be a sha256 digest")
    return digest


def _knowledge_strict_int(
    value: object, *, field: str, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _knowledge_strict_float(
    value: object, *, field: str, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    parsed = float(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def _bounded_text(value: object, *, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"\[L:[^\]]+\]", "", text)
    return " ".join(text.split())[:maximum]


_TRACE_DIAGNOSTIC_TARGET_FIELDS = frozenset(
    {"kind", "id", "title", "traceIds"}
)


def _strict_trace_diagnostic_targets(value: object) -> list[Mapping[str, object]]:
    """Validate nested diagnostic targets before dispatching to the service."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Trace diagnostic targets must be an array")
    if not 1 <= len(value) <= 12:
        raise ValueError("Trace diagnostic targets must contain between 1 and 12 objects")
    targets: list[Mapping[str, object]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"Trace diagnostic targets must contain only objects (index {index})"
            )
        unknown = sorted(
            str(key) for key in raw if key not in _TRACE_DIAGNOSTIC_TARGET_FIELDS
        )
        if unknown:
            raise ValueError(
                "Trace diagnostic target contains unsupported fields: "
                + ", ".join(str(item) for item in unknown)
            )
        for field in ("kind", "id", "title"):
            if field not in raw or not isinstance(raw[field], str):
                raise ValueError(
                    f"Trace diagnostic target {field} must be a string (index {index})"
                )
        if raw["kind"] not in {"session", "room", "run"}:
            raise ValueError(
                f"Trace diagnostic target kind is invalid (index {index})"
            )
        if not raw["id"].strip() or len(raw["id"]) > 240:
            raise ValueError(
                f"Trace diagnostic target id is invalid (index {index})"
            )
        if len(raw["title"]) > 240:
            raise ValueError(
                f"Trace diagnostic target title is too long (index {index})"
            )
        trace_ids = raw.get("traceIds")
        if trace_ids is not None:
            if not isinstance(trace_ids, Sequence) or isinstance(
                trace_ids, (str, bytes, bytearray)
            ):
                raise ValueError(
                    f"Trace diagnostic target traceIds must be an array (index {index})"
                )
            if len(trace_ids) > 32:
                raise ValueError(
                    f"Trace diagnostic target traceIds has too many items (index {index})"
                )
            if any(
                not isinstance(trace_id, str)
                or not trace_id.strip()
                or len(trace_id) > 240
                for trace_id in trace_ids
            ):
                raise ValueError(
                    "Trace diagnostic target traceIds must contain only non-empty strings "
                    f"(index {index})"
                )
            if len(set(trace_ids)) != len(trace_ids):
                raise ValueError(
                    f"Trace diagnostic target traceIds must be unique (index {index})"
                )
        targets.append(raw)
    return targets


def _normalize_input_setting_changes(value: object) -> list[dict[str, object]]:
    if isinstance(value, Mapping):
        raw_items = [{"key": key, "value": child} for key, child in value.items()]
    elif isinstance(value, list):
        raw_items = value
    else:
        raise ValueError("changes must be an array of setting key/value pairs")
    if not raw_items:
        raise ValueError("at least one input setting change is required")
    if len(raw_items) > 12:
        raise ValueError("at most 12 input settings may change in one approval")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise ValueError("each input setting change must be an object")
        key = str(raw.get("key") or "").strip()
        if key not in _INPUT_SETTING_FIELDS:
            raise ValueError(f"input setting is not agent-manageable: {key or 'missing key'}")
        if key in seen:
            raise ValueError(f"duplicate input setting: {key}")
        if "value" not in raw:
            raise ValueError(f"input setting value is required: {key}")
        result.append({"key": key, "value": _normalize_input_setting_value(key, raw["value"])})
        seen.add(key)
    return sorted(result, key=lambda item: str(item["key"]))


def _normalize_input_setting_value(key: str, value: object) -> object:
    default = _SETTING_DEFAULTS[key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"setting {key} must be a boolean")
        normalized: object = value
    elif isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"setting {key} must be an integer")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"setting {key} must be an integer")
        normalized = int(value)
    elif isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"setting {key} must be a string")
        normalized = value.strip()
    else:
        raise ValueError(f"setting {key} has an unsupported value type")
    schema_field = _SETTING_SCHEMA_FIELDS.get(key, {})
    options = schema_field.get("options")
    if isinstance(options, list) and normalized not in options:
        raise ValueError(f"setting {key} must be one of: {', '.join(str(item) for item in options)}")
    bounds = _INPUT_SETTING_FIELDS[key]
    if isinstance(normalized, int) and not isinstance(normalized, bool):
        minimum = bounds.get("min")
        maximum = bounds.get("max")
        if isinstance(minimum, int) and normalized < minimum:
            raise ValueError(f"setting {key} must be >= {minimum}")
        if isinstance(maximum, int) and normalized > maximum:
            raise ValueError(f"setting {key} must be <= {maximum}")
    return normalized


def _input_setting_label(key: str) -> str:
    return str(_INPUT_SETTING_FIELDS.get(key, {}).get("label") or key)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _work_document_sync_receipt(
    registration: Mapping[str, object],
    *,
    approval_id: str,
    state: str,
    reason: str = "",
    document_id: str = "",
    document_revision: object = None,
    receipt_status: str = "",
    document_state: str = "",
) -> dict[str, object]:
    """Describe one best-effort WorkDocument registration attempt.

    This is deliberately a Tool receipt rather than a second lifecycle write:
    the workspace mutation has already been applied, and callers must not
    replay it merely to repair an auxiliary document binding.
    """

    authority_kind = _bounded_text(registration.get("authorityKind"), maximum=40)
    authority_id = _bounded_text(registration.get("authorityId"), maximum=240)
    trace_material = "|".join(
        (
            approval_id,
            authority_kind,
            authority_id,
            _bounded_text(registration.get("authorityRevision"), maximum=40),
        )
    )
    trace_id = f"trace:work-document-sync:{_sha256_text(trace_material)[:32]}"
    normalized_state = state if state in {"applied", "pending", "failed"} else "failed"
    receipt: dict[str, object] = {
        "state": normalized_state,
        "attemptCount": 1,
        "retryable": False,
        "authorityKind": authority_kind,
        "authorityId": authority_id,
        "authorityRevision": _safe_int(registration.get("authorityRevision")),
        "traceId": trace_id,
        "publicNotice": (
            "WorkDocument 同步已记录一次；不阻断真实工作结果，也不要仅因文档同步失败自动 retry/return。"
            if normalized_state != "applied"
            else "WorkDocument 已同步。"
        ),
    }
    if document_id:
        receipt["documentId"] = _bounded_text(document_id, maximum=240)
    if document_revision is not None:
        receipt["documentRevision"] = _safe_int(document_revision)
    if receipt_status:
        receipt["receiptStatus"] = _bounded_text(receipt_status, maximum=40)
    if document_state:
        receipt["documentState"] = _bounded_text(document_state, maximum=40)
    normalized_reason = _bounded_text(reason, maximum=240)
    if normalized_reason:
        receipt["reason"] = normalized_reason
    return receipt


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _legacy_room_invocation_receipt_id(
    approval: Mapping[str, object],
) -> str:
    preview = approval.get("preview")
    if not isinstance(preview, Mapping):
        return ""
    base_state = preview.get("baseState")
    if not isinstance(base_state, Mapping):
        return ""
    return _bounded_text(
        base_state.get("roomInvocationReceiptId"),
        maximum=240,
    )


def _approval_payload_digest(
    *,
    session_id: str,
    tool: str,
    operation: str,
    action_payload: Mapping[str, object],
    base_state: Mapping[str, object],
) -> str:
    material = {
        "schemaVersion": "rag-ime.agent-approved-operation.v1",
        "sessionId": session_id,
        "tool": tool,
        "operation": operation,
        "actionPayload": dict(action_payload),
        "baseState": dict(base_state),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _highest_risk(values: object) -> str:
    priority = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}
    normalized = [str(value) for value in values] if values is not None else ["R0"]
    return max(normalized or ["R0"], key=lambda value: priority.get(value, 3))


def _planning_status_label(value: str) -> str:
    return _PLANNING_STATUS_LABELS.get(value, value or "未知")


def _memory_run_status_label(value: str) -> str:
    return {
        "draft": "待审草案",
        "applied": "已应用",
        "partial": "部分应用",
        "rolled_back": "已回滚",
        "superseded": "已被新草案替代",
    }.get(value, value or "未知")


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _string_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_bounded_text(item, maximum=120) for item in value[:limit] if _bounded_text(item, maximum=1)]


def _review_key_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value[:limit]:
        key = str(item or "").strip()[:300]
        if key and key not in result:
            result.append(key)
    return result


def _safe_mapping_payload(value: Mapping[str, object]) -> dict[str, object]:
    safe = _safe_payload(value)
    return safe if isinstance(safe, dict) else {}


def _safe_payload(value: object, *, depth: int = 0) -> object:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, child in list(value.items())[:100]:
            key = _bounded_text(raw_key, maximum=120)
            if not key or _secret_key(key):
                continue
            result[key] = _safe_payload(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_payload(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return _bounded_text(value, maximum=2000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded_text(value, maximum=500)


def _safe_knowledge_payload(value: Mapping[str, object]) -> dict[str, object]:
    """Bound document evidence without exposing worker or filesystem details."""

    blocked_keys = {
        "absolutepath",
        "localpath",
        "sourcepath",
        "storedpath",
        "workertoken",
        "workerurl",
        "endpoint",
    }

    def visit(item: object, *, depth: int = 0) -> object:
        if depth > 6:
            return "[truncated]"
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for raw_key, child in list(item.items())[:100]:
                key = _bounded_text(raw_key, maximum=120)
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if not key or normalized in blocked_keys or _secret_key(key):
                    continue
                result[key] = visit(child, depth=depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            return [visit(child, depth=depth + 1) for child in item[:100]]
        if isinstance(item, str):
            return _bounded_text(item, maximum=2000)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return _bounded_text(item, maximum=500)

    safe = visit(value)
    return safe if isinstance(safe, dict) else {}


def _secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(
        token in normalized
        for token in ("apikey", "token", "secret", "password", "authorization", "cookie", "credential")
    )


def _tool_profile_allows(
    session: Mapping[str, object],
    *,
    tool: str,
    operation: str,
    spec: Mapping[str, object],
) -> bool:
    profile = str(session.get("toolProfileVersion") or "control-center-v1")
    if profile in {
        DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
        FULL_ACCESS_TOOL_PROFILE,
    }:
        return True
    if (
        str(session.get("toolAllowlistMode") or "profile") == "explicit"
        and tool not in {str(value) for value in session.get("allowedTools") or []}
    ):
        return False
    if read_only_policy_active(session) and read_only_blocks_effect(tool, operation):
        return False
    if (
        read_only_policy_active(session)
        and (tool, operation) == ("workspace_shell", "run")
    ):
        return True
    if profile in {"control-center-v1", "subagent-worker-v1"}:
        return True
    if profile != "subagent-readonly-v1":
        return not profile.startswith("subagent-")
    allowed: dict[str, frozenset[str]] = {
        "overview": frozenset({"status", "capabilities", "recent_activity"}),
        "memory": frozenset(
            {
                "catalog",
                "read",
                "recent",
                "trace",
                "maintenance_status",
                "list",
                "search",
                "get",
                "explain",
                "review",
                "remember_preview",
                "correct_preview",
                "forget_preview",
            }
        ),
        "agent_role_book": frozenset({"get", "history", "review"}),
        "knowledge": frozenset(
            {
                "list_bases",
                "get_base",
                "list_documents",
                "search",
                "find",
                "open",
                "status",
                "rebuild_preview",
            }
        ),
        "models": frozenset({"status", "profiles", "probe", "cache_stats"}),
        "runtime": frozenset({"health", "components", "diagnose"}),
        "browser": frozenset({"status", "tabs", "snapshot", "screenshot", "trace"}),
        # The Connector is a read-only profile capability: PAW Host keeps the
        # process, workspace, network, and persistence fences authoritative.
        "sandbox": frozenset({"status", "run"}),
        "agents": frozenset(
            {
                "catalog",
                "status",
                "artifact",
                "delegate",
                "call",
                "abort",
            }
        ),
        "session_search": frozenset({"search"}),
        "trace_diagnostics": frozenset({"inspect", "read"}),
        # Formal Room operations do not widen the workspace policy. Delegation,
        # stopping-point reads, evidence-backed review transitions, public
        # receipts, and direct intercom mutate Room collaboration state rather
        # than the read-only Session's source workspace.
        "room_partner": frozenset(
            {
                "list",
                "add_participant",
                "remove_participant",
                "delegate",
                "delegate_batch",
                "retry",
                "accept",
                "return",
                "collect",
                "wait",
                "post",
                "peer_list",
                "peer_send",
                "peer_ask",
                "peer_reply",
            }
        ),
        "agent_schedule": frozenset({"list", "runs"}),
        "todo": frozenset(
            {"init", "start", "done", "drop", "append", "view", "rm"}
        ),
        "agent_goal": frozenset({"list"}),
        "work_documents": frozenset({"list", "history.search", "get"}),
        # These commands maintain the current App's artifacts and bindings,
        # like conversational Todo state; no external task runs here.
        "lab_project": frozenset({"read", "command", "execution_read", "knowledge_read"}),
        "lab_research": frozenset({"discover", "find", "open", "search"}),
        "workspace_list": frozenset({"list"}),
        "workspace_read": frozenset({"read"}),
        "workspace_search": frozenset({"search"}),
        "workspace_lsp": frozenset(
            {
                "status",
                "symbols",
                "hover",
                "definition",
                "references",
                "diagnostics",
            }
        ),
        "workspace_job": frozenset({"list", "status", "logs"}),
        "workspace_shell": frozenset({"run"}),
        "desktop_semantic": frozenset({"status", "list", "inspect", "find"}),
    }
    operation_risk = str(dict(spec.get("operationRisks") or {}).get(operation) or "R0")
    return operation_risk == "R0" and operation in allowed.get(tool, frozenset())


def _runtime_memory_tool_parameter_schema(
    operations: list[str],
) -> dict[str, object]:
    argument_names = (*_RUNTIME_TOOL_ARGUMENTS["memory"], "scope", "policy")
    properties = {
        "op": {"type": "string", "enum": operations},
        **{
        name: dict(
            _RUNTIME_TOOL_ARGUMENT_SCHEMA_OVERRIDES.get(
                ("memory", name),
                _RUNTIME_TOOL_ARGUMENT_SCHEMAS[name],
            )
        )
        for name in argument_names
        if name in _RUNTIME_TOOL_ARGUMENT_SCHEMAS
        or ("memory", name) in _RUNTIME_TOOL_ARGUMENT_SCHEMA_OVERRIDES
        },
    }
    properties.update(
        {
            "query": {"type": "string", "maxLength": 240},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            "instruction": {"type": "string", "maxLength": 800},
            "trigger": {
                "type": "string",
                "enum": ["task_completion", "explicit_request", "idle_batch"],
            },
            "scope": {
                "type": "string",
                "enum": [
                    "incremental",
                    "global",
                    "recent",
                    "current",
                    "historical",
                    "change",
                ],
            },
            "policy": {"type": "string", "enum": ["conservative"]},
            "claim": {
                "type": "string",
                "minLength": 1,
                "maxLength": 800,
                "description": (
                    "只陈述一条由当前用户原话支持、脱离当前对话仍可独立理解且会改变未来协作的"
                    "稳定偏好、个人事实、长期决定、纠正或原则；不得记录任务执行与会话状态。"
                ),
            },
            "sourceId": {
                "type": "string",
                "maxLength": 240,
                "description": (
                    "只有当前上下文明确给出精确 Evidence ID 时才填写；"
                    "不得猜测内部 ID。"
                ),
            },
            "captureScope": {
                "type": "string",
                "enum": ["user", "project"],
                "description": (
                    "跨项目适用的用户偏好、个人事实或原则选 user；"
                    "用户明确声明的长期项目约束或决定选 project。"
                ),
            },
            "basis": {
                "type": "string",
                "enum": [
                    "explicit_user_request",
                    "explicit_user_statement",
                    "user_correction",
                    "repeated_user_signal",
                    "verified_outcome",
                ],
                "description": (
                    "explicit_user_request=用户明确要求记住；"
                    "explicit_user_statement=用户明确陈述；"
                    "user_correction=用户纠正已有记忆；"
                    "repeated_user_signal=当前可见上下文至少两条独立用户表达；"
                    "verified_outcome=已应用工具回执或运行结果提供可核验依据。"
                ),
            },
            "futureUse": {
                "type": "string",
                "minLength": 1,
                "maxLength": 300,
                "description": (
                    "说明未来 Session 在什么情形下应怎样使用该候选；"
                    "若只影响当前任务或当前会话，就不应调用 capture。"
                ),
            },
            "supersedes": {
                "type": "string",
                "maxLength": 800,
                "description": "仅 correction 可填写；写被纠正的旧说法，不写内部 ID。",
            },
        }
    )
    branches: list[dict[str, object]] = []
    for operation in operations:
        branch: dict[str, object] = {
            "required": [
                "op",
                *_RUNTIME_TOOL_REQUIRED_ARGUMENTS.get(
                    ("memory", operation),
                    (),
                ),
            ],
            "properties": {"op": {"const": operation}},
        }
        alternatives = _RUNTIME_TOOL_REQUIRED_ALTERNATIVES.get(
            ("memory", operation),
            (),
        )
        if alternatives:
            branch["anyOf"] = [
                {"required": list(alternative)} for alternative in alternatives
            ]
        if operation == "capture":
            branch["properties"] = {
                "op": {"const": operation},
                "kind": {
                    "type": "string",
                    "enum": [
                        "preference",
                        "fact",
                        "decision",
                        "correction",
                        "pitfall",
                    ],
                    "description": (
                        "preference=稳定偏好；fact=用户明确陈述的个人事实；"
                        "decision=跨 Session 持续的决定或约束；correction=用户纠正；"
                        "pitfall=用户明确表达的长期原则或边界。"
                    ),
                },
                "captureScope": {
                    "type": "string",
                    "enum": ["user", "project"],
                },
                "basis": {
                    "type": "string",
                    "enum": [
                        "explicit_user_request",
                        "explicit_user_statement",
                        "user_correction",
                        "repeated_user_signal",
                        "verified_outcome",
                    ],
                },
            }
        elif operation == "maintenance_status":
            branch["properties"] = {
                "op": {"const": operation},
                "scope": {
                    "type": "string",
                    "enum": ["incremental", "global"],
                },
            }
        branches.append(branch)
    # Older and weaker OpenAI-compatible models sometimes infer the obvious
    # read-only call from the compact catalog before emitting `op`. Accept only
    # this bounded compatibility shape; every mutation still requires an exact
    # operation branch and its operation-specific fields.
    branches.append(
        {
            "description": (
                "只读兼容：query 必填；scope=recent 读取近期证据，"
                "current/historical/change 转为对应检索视图。新调用应显式传 op。"
            ),
            "required": ["query"],
            "not": {"required": ["op"]},
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["recent", "current", "historical", "change"],
                },
            },
        }
    )
    return {
        "type": "object",
        "description": (
            "Evidence->Candidate->治理->Atom。capture 只形成候选，不等于正式记忆；"
            "同一事实每轮最多一次，一轮最多三条。不要捕获一次性请求、临时进度、"
            "工具日志、短暂故障、猜测、敏感信息、普通寒暄、泛泛表扬、大段原文、"
            "Room 私有过程或模型未确认建议；Room 仅允许已公开且有证据的交付、决定"
            "或项目结论。失败不循环重试。Role Book 用 agent_role_book。"
        ),
        "additionalProperties": False,
        "properties": properties,
        "oneOf": branches,
    }


def _runtime_tool_parameter_schema(
    tool_id: str,
    operations: list[object],
) -> dict[str, object]:
    normalized_operations = [str(operation) for operation in operations]
    if tool_id == "memory":
        return _runtime_memory_tool_parameter_schema(normalized_operations)
    configured = _RUNTIME_TOOL_PARAMETER_SCHEMAS.get(tool_id)
    if configured is not None:
        # Runtime manifests are public projections.  Never expose the module-level
        # schema objects themselves: callers and tests may normalize the returned
        # payload in place, and one mutation must not corrupt later Sessions.
        configured = copy.deepcopy(configured)
        allowed = {str(operation) for operation in operations}
        branches = configured.get("oneOf")
        if isinstance(branches, list):
            filtered = [
                branch
                for branch in branches
                if isinstance(branch, Mapping)
                and str(
                    (
                        branch.get("properties", {}).get("op", {})
                        if isinstance(branch.get("properties"), Mapping)
                        else {}
                    ).get("const")
                    or ""
                )
                in allowed
            ]
            return {**configured, "oneOf": filtered}
        return dict(configured)
    argument_names = _RUNTIME_TOOL_ARGUMENTS.get(tool_id, ())
    properties = {
        "op": {"type": "string", "enum": normalized_operations},
        **{
            name: dict(
                _RUNTIME_TOOL_ARGUMENT_SCHEMA_OVERRIDES.get(
                    (tool_id, name),
                    _RUNTIME_TOOL_ARGUMENT_SCHEMAS[name],
                )
            )
            for name in argument_names
        },
    }
    branches: list[dict[str, object]] = []
    for operation in normalized_operations:
        required = [
            "op",
            *_RUNTIME_TOOL_REQUIRED_ARGUMENTS.get((tool_id, operation), ()),
        ]
        branch: dict[str, object] = {
            "required": required,
            "properties": {"op": {"const": operation}},
        }
        alternatives = _RUNTIME_TOOL_REQUIRED_ALTERNATIVES.get((tool_id, operation), ())
        if tool_id == "agents" and operation == "delegate":
            single_task_fields = (
                "agent", "version", "task", "expectedOutput",
                "acceptanceCriteria", "outputSchema", "modelProfile",
                "thinkingLevel", "budget", "access", "allowedTools", "piSkillsEnabled",
                "codexSkillsEnabled", "workspaceRoots",
            )
            branch["oneOf"] = [
                {
                    "required": [
                        "agent", "task", "expectedOutput", "acceptanceCriteria",
                    ],
                    "not": {"required": ["tasks"]},
                },
                {
                    "required": ["tasks"],
                    "not": {
                        "anyOf": [
                            {"required": [field]} for field in single_task_fields
                        ]
                    },
                },
            ]
        elif tool_id == "plugins" and operation == "validate":
            branch["oneOf"] = [
                {"required": [source]}
                for source in ("sourcePath", "packageSource", "catalogId")
            ]
        elif alternatives:
            branch["anyOf"] = [
                {"required": list(alternative)} for alternative in alternatives
            ]
        branches.append(branch)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["op"],
        "properties": properties,
        "oneOf": branches,
    }


def _read_voice_agent_status() -> dict[str, object]:
    app_support = Path(
        os.environ.get("RAG_IME_APP_SUPPORT_DIR")
        or Path.home() / "Library" / "Application Support" / "RagIme"
    ).expanduser()
    path = app_support / "voice-agent-status.json"
    if not path.is_file() or path.is_symlink():
        return {
            "available": False,
            "running": False,
            "state": "unavailable",
            "statusText": "语音代理没有可用状态",
        }
    try:
        if path.stat().st_size > 256 * 1024:
            raise ValueError("voice status is too large")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {
            "available": False,
            "running": False,
            "state": "invalid",
            "statusText": "语音代理状态不可解析",
        }
    if not isinstance(raw, Mapping):
        return {"available": False, "running": False, "state": "invalid"}
    allowed = (
        "schemaVersion",
        "running",
        "accessibilityTrusted",
        "microphoneAuthorization",
        "credentialsConfigured",
        "hotkeyMode",
        "hotwordsEnabled",
        "hotwordCount",
        "hotkeyInstalled",
        "state",
        "statusText",
        "updatedAtMs",
    )
    result = {key: raw.get(key) for key in allowed if key in raw}
    telemetry = raw.get("telemetry")
    if isinstance(telemetry, Mapping):
        result["telemetry"] = {
            key: telemetry.get(key)
            for key in (
                "networkState",
                "sessionActive",
                "sessionStartedAtMs",
                "firstPartialLatencyMs",
                "finalLatencyMs",
                "pcmFrameCount",
                "droppedPCMFrameCount",
                "partialRevisionCount",
                "finalReceived",
            )
            if key in telemetry
        }
    result["available"] = True
    return result


def _mapping_value(value: Mapping[str, object], parent: str, child: str) -> object:
    nested = value.get(parent)
    return nested.get(child) if isinstance(nested, Mapping) else None


def _memory_evidence(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        getter = value.get
    else:
        getter = lambda key, default=None: getattr(value, key, default)
    source_event_id = getter("source_event_id", "")
    return {
        "kind": "source",
        "sourceId": f"event:{source_event_id}" if source_event_id else "",
        "text": _bounded_text(getter("text", ""), maximum=1600),
        "evidence": _bounded_text(getter("evidence_preview", ""), maximum=1200),
        "score": max(0.0, float(getter("score", 0.0) or 0.0)),
        "reason": _bounded_text(getter("reason", ""), maximum=300),
        "project": _bounded_text(getter("project", ""), maximum=160),
        "tags": _string_list(getter("tags", ()), limit=20),
        "createdAtMs": _safe_int(getter("created_at_ms", 0)),
    }


def _model_status_summary(payload: Mapping[str, object]) -> str:
    predictor = payload.get("predictor") if isinstance(payload.get("predictor"), Mapping) else {}
    active = payload.get("activeRagRoute") if isinstance(payload.get("activeRagRoute"), Mapping) else {}
    predictor_ready = predictor.get("ok") is True or predictor.get("available") is True
    remote_ready = active.get("remoteReady") is True
    if predictor_ready and remote_ready:
        return "本地预测与深度知识模型均已就绪"
    if predictor_ready:
        return "本地预测已就绪，深度知识模型当前不可用"
    return "模型运行链路当前未就绪"
