#!/usr/bin/env python3
"""Live-model evaluation for owner-memory updates on an isolated SQLite DB.

The script intentionally never accepts the production database path. It seeds
synthetic current facts, sends paraphrased updates through the real organizer,
and checks claim lineage, source dispositions, and Topic Book references.  The
configured Provider remains available for historical comparison; the interview
lane uses the governed private Luna/max evaluation boundary.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_ID, DEFAULT_AGENT_MODEL_PROFILE
from rag_ime.agent_memory_sources import AgentMemorySourceStore
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.deepseek_config import load_deepseek_config
from rag_ime.deepseek_memory_organizer import (
    DeepSeekMemoryOrganizer,
    ManagedPiMemoryOrganizer,
)
from rag_ime.owner_memory_curation import OwnerMemoryCurator
from rag_ime.personal_memory_luna_evaluation import PrivateCodexLunaMemoryExecutor


PROJECT = "wisdom-weasel-rag-ime"
OWNER_KIND = "user"
OWNER_ID = "default"
DEFAULT_MODEL_ENV = Path.home() / "Library/Application Support/RagIme/deepseek.env"
FORGET_CLAIM_KEY = "user:collaboration:completion-sound"
FORGET_BASELINE = "用户偏好在重要工作完成后播放提示音。"
FORGET_INPUT = "请忘掉我在重要工作完成后偏好播放提示音这条记忆，不要再记住它。"
CONFLICT_CLAIM_KEY = "user:communication:answer-length"
CONFLICT_BASELINE = "用户长期偏好简短回答。"
CONFLICT_INPUT = "这一次临时请给长回答，但不改变我长期偏好简短回答。"


@dataclass(frozen=True)
class Scenario:
    claim_key: str
    kind: str
    topic: str
    baseline: str
    v2_input: str
    v2_terms: tuple[str, ...]
    v3_input: str
    v3_terms: tuple[str, ...]


SCENARIOS = (
    Scenario(
        "ime:runtime:primary-model",
        "project_fact",
        "输入法推理与性能",
        "输入法候选热路径当前使用 Qwen 0.8B 模型。",
        "输入法线上候选的主推理模型已经改成 100M 自训练模型，Qwen 0.8B 不再负责主路径。",
        ("100M", "主"),
        "输入法线上候选主模型再次更新为量化后的 120M 自训练模型，100M 版本退出主路径。",
        ("120M", "主"),
    ),
    Scenario(
        "ime:runtime:p95-latency-target",
        "project_requirement",
        "输入法推理与性能",
        "输入法候选可见延迟目标是 p95 80 毫秒以内。",
        "候选可见延迟的正式目标已经收紧到 p95 50 毫秒以内。",
        ("p95", "50"),
        "候选可见延迟目标再次收紧为 p95 40 毫秒以内。",
        ("p95", "40"),
    ),
    Scenario(
        "memory:maintenance:runs-per-day",
        "project_requirement",
        "记忆整理与治理",
        "个人记忆每天夜间整理一次。",
        "个人记忆的正式整理频率已经调整为每天两次。",
        ("每天", "两次"),
        "个人记忆改为每日一次夜间整理；白天空闲时只在出现新增事实时做增量整理。",
        ("每日一次", "增量"),
    ),
    Scenario(
        "memory:role-book:write-policy",
        "project_decision",
        "记忆整理与治理",
        "角色书由每日整理任务自动扫描全部聊天记录生成。",
        "角色书不再主动扫描全部聊天；只在 Agent 完成功能或项目里程碑时调用工具写入。",
        ("角色书", "里程碑", "工具"),
        "角色书继续采用里程碑触发；空闲整理只处理高密度摘要，不读取逐轮聊天。",
        ("里程碑", "摘要", "不读取"),
    ),
    Scenario(
        "agent:session:memory-injection",
        "project_decision",
        "Agent 上下文",
        "个人记忆会在 Agent 每一轮请求前重新召回。",
        "个人记忆只在 Session 首轮自动召回一次，后续由 Agent 按需调用工具。",
        ("Session", "首轮", "工具"),
        "Session 首轮只注入有界 Book 和 Atom 包，后续只能通过记忆工具按需追加。",
        ("首轮", "Book", "Atom", "按需"),
    ),
    Scenario(
        "room:context:new-member-budget",
        "project_constraint",
        "Room 协作",
        "新角色加入 Room 时会注入 Room 的全部历史消息。",
        "新角色进入 Room 时最多注入最近 12 条消息和一份压缩摘要，禁止灌入完整历史。",
        ("12", "压缩摘要"),
        "Room 新角色上下文预算改为最近 8 条消息，加一份不超过 2000 字的摘要。",
        ("8", "2000", "摘要"),
    ),
    Scenario(
        "desktop:context:source-priority",
        "project_decision",
        "桌面与语音输入",
        "输入原文只取输入法自身记录的 committed text。",
        "按回车时优先读取输入框最终全文；AX 不可用或内容更短时回退到输入法提交记录。",
        ("输入框", "AX", "回退"),
        (
            "最终输入来源策略已从“AX 不可用时回退到输入法提交记录”更新为"
            "“AX 优先，但密码框和 Secure Input 必须拒绝采集且不允许回退录入”。"
        ),
        ("AX", "密码框", "拒绝"),
    ),
    Scenario(
        "voice:focus-change:behavior",
        "project_fact",
        "桌面与语音输入",
        "语音输入遇到焦点变化会停止识别。",
        "语音输入遇到焦点变化时继续识别，无法写入的最终稿保留到剪贴板。",
        ("继续", "剪贴板"),
        "焦点切换时继续听写；目标应用拒绝流式写入时，最终稿进入剪贴板并明确提示。",
        ("继续", "剪贴板", "提示"),
    ),
    Scenario(
        "browser:observation:protocol",
        "project_decision",
        "浏览器与桌面 Agent",
        "浏览器 Agent 每一步都发送完整 DOM 和截图。",
        "浏览器插件改为批量命令和增量状态返回，只有必要时才请求截图。",
        ("批量", "增量", "截图"),
        "浏览器观察协议已从增量 DOM 状态改为紧凑快照桥接；批量执行动作，失败后才回退截图。",
        ("紧凑快照", "批量", "回退截图"),
    ),
    Scenario(
        "memory:timeline:granularity",
        "project_decision",
        "记忆整理与治理",
        "时间线按每条输入生成一个事件块。",
        "时间线改为按持续任务聚合，一上午或一天通常只保留两到三个活动块。",
        ("持续任务", "两到三个"),
        "每日时间线固定聚合为少量粗粒度持续任务，每个任务保留来源证据而不是逐句事件。",
        ("粗粒度", "持续任务", "来源证据"),
    ),
    Scenario(
        "memory:retrieval:lanes",
        "project_fact",
        "记忆检索架构",
        "个人记忆检索目前只使用 Dense 向量召回。",
        "个人记忆召回已经采用 BM25、Dense、标签别名和 Book 图扩展的混合检索。",
        ("BM25", "Dense", "图扩展"),
        "混合召回保留 BM25、Dense 与图扩展，并先保证每条事实探针覆盖旧事实，再做全局重排。",
        ("BM25", "探针", "重排"),
    ),
    Scenario(
        "memory:storage:atom-first",
        "project_constraint",
        "记忆检索架构",
        "历史输入片段可以直接进入 Agent 长期上下文。",
        "长期记忆已经改为 Atom-first；原始片段只作为可追溯证据，不能直接进入 Agent 上下文。",
        ("Atom-first", "证据", "不直接|不能直接"),
        "Atom 是唯一能进入长期召回的事实层，Book 只组织主题，输入片段仅保留审计来源。",
        ("Atom", "Book", "审计"),
    ),
    Scenario(
        "agent:capability:catalog-policy",
        "project_decision",
        "Agent 上下文",
        "Agent 启动时加载所有 Skill 正文和全部工具 Schema。",
        "Agent 常驻上下文只保留 Skill 路由卡和工具 name/does，需要时再加载正文或 Schema。",
        ("路由卡", "name", "does", "加载"),
        "Pi Skills 和 Codex Skills 默认关闭，输入法 Skills 常驻；能力正文仍然按需加载。",
        ("默认关闭", "输入法", "按需加载"),
    ),
    Scenario(
        "ui:plan:placement",
        "project_decision",
        "控制面板交互",
        "Plan 和 Todo 浮层默认显示在对话中央。",
        "Plan 和 Todo 清单默认固定到右侧信息栏，不能遮挡对话正文。",
        ("右侧", "不能遮挡"),
        "Plan 和 Todo 继续停靠右栏，移动端改为可折叠抽屉。",
        ("右栏", "移动端", "折叠"),
    ),
    Scenario(
        "room:routing:assignment-policy",
        "project_decision",
        "Room 协作",
        "Room 默认让所有 Agent 按顺序轮流发言。",
        "Room 改为通过 @ 指派和责任账本分配干活权，未被点名的 Agent 不自动抢答。",
        ("@", "责任账本", "不自动"),
        "Room 路由以 @ 指派为优先，并把主持协调责任与实际执行责任分开。",
        ("@", "协调", "执行"),
    ),
    Scenario(
        "security:desktop:screenshot-policy",
        "security_constraint",
        "浏览器与桌面 Agent",
        "桌面 Agent 默认每一步都截图。",
        "桌面 Agent 默认不截图，优先读取 Accessibility Tree，必要时才回退截图。",
        ("默认不截图", "Accessibility Tree", "回退"),
        "桌面上下文继续 AX 优先，只有用户授权且 AX 失败时才允许截图。",
        ("AX", "用户授权", "截图"),
    ),
    Scenario(
        "memory:external-agent:ingestion-policy",
        "security_constraint",
        "外部 Agent 记忆",
        "Codex 的全部历史会话原文会直接导入长期记忆。",
        "不再直接导入 Codex 原始会话，只接收近几个月由 Agent 汇总的高密度 Session 摘要。",
        ("不再", "原始会话", "摘要"),
        "Codex 接入只允许顶层索引和近三个月 rollout 摘要，不得读取或恢复原始对话。",
        ("顶层索引", "rollout", "不得"),
    ),
    Scenario(
        "memory:book:topic-scope",
        "project_constraint",
        "记忆检索架构",
        "所有个人记忆都会写入同一本个人长期记忆 Book。",
        "Book 已经按稳定语义主题拆分，禁止把全部事实塞进通用的个人长期记忆 Book。",
        ("语义主题", "禁止", "全部事实"),
        "Topic Book 只负责组织主题脉络，同一事实仍由带版本的 Atom 保存。",
        ("Topic Book", "Atom", "版本"),
    ),
)


SEMANTIC_NOISE = (
    "这一轮只是临时比较两个按钮的间距，不形成长期设计决定。",
    "今天随手看了几眼日志，暂时没有形成可复用结论。",
    "刚才把窗口拖到左边只是为了当前演示，不代表长期布局偏好。",
    "这轮临时把输出压成一句话，下一轮仍按正常方式回答。",
    "今天试用了一个配色草稿，但没有决定采用它。",
    "刚才折叠面板只是当前操作，不是产品约束。",
    "这次临时按创建时间排序，后续没有确定继续使用。",
    "当前只为排查问题打开详细日志，排查结束后不保留这个选择。",
    "今天暂时把提示音关掉，尚未形成长期偏好。",
    "这轮演示先隐藏侧栏，产品默认布局没有变化。",
    "刚才测试了一个候选词，但没有决定加入正式词库。",
    "本轮只是检查接口返回格式，没有产生新的项目事实。",
)


class CapturingOrganizer:
    def __init__(self, delegate: DeepSeekMemoryOrganizer) -> None:
        self.delegate = delegate
        self.calls: list[dict[str, object]] = []

    @property
    def provider_name(self) -> str:
        return self.delegate.provider_name

    @property
    def curation_protocol_version(self) -> str:
        """Preserve the governed organizer protocol through the audit wrapper."""

        return str(getattr(self.delegate, "curation_protocol_version", ""))

    def begin_run(
        self,
        run_id: str,
        *,
        frozen_input_sha256: str = "",
    ) -> dict[str, object]:
        begin = getattr(self.delegate, "begin_run", None)
        if not callable(begin):
            return {}
        return dict(
            begin(
                run_id,
                frozen_input_sha256=frozen_input_sha256,
            )
        )

    def finish_run(self) -> dict[str, object]:
        finish = getattr(self.delegate, "finish_run", None)
        return dict(finish()) if callable(finish) else {}

    def fail_run(self, error: BaseException) -> dict[str, object]:
        fail = getattr(self.delegate, "fail_run", None)
        return dict(fail(error)) if callable(fail) else {}

    def close(self) -> None:
        close = getattr(self.delegate, "close", None)
        if callable(close):
            close()

    def curate_owner_memory(self, **kwargs: Any) -> dict[str, object]:
        bundle = dict(kwargs.get("bundle") or {})
        result = self.delegate.curate_owner_memory(**kwargs)
        self.calls.append(
            {
                "inputs": [
                    {
                        "sourceRef": item.get("sourceRef"),
                        "text": item.get("text"),
                        "sourceEventIds": item.get("sourceEventIds"),
                    }
                    for item in bundle.get("inputs") or []
                    if isinstance(item, dict)
                ],
                "recalledAtoms": [
                    {
                        "atomId": item.get("atomId"),
                        "claimKey": item.get("claimKey"),
                        "canonicalText": item.get("canonicalText"),
                    }
                    for item in bundle.get("existingMemoryAtoms") or []
                    if isinstance(item, dict)
                ],
                "recall": dict(bundle.get("existingMemoryRecall") or {}),
                "sourceDecisions": list(result.get("sourceDecisions") or []),
                "memoryAtoms": list(result.get("memoryAtoms") or []),
                "topicBooks": list(result.get("topicBooks") or []),
                "warnings": list(result.get("warnings") or []),
                "diagnostics": dict(result.get("modelDiagnostics") or {}),
                "elapsedMs": int(result.get("elapsedMs") or 0),
            }
        )
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate real-model owner-memory updates on temporary SQLite.",
    )
    parser.add_argument(
        "--profile",
        choices=("calibration", "stress"),
        default="calibration",
    )
    parser.add_argument(
        "--model-env-path",
        default=str(DEFAULT_MODEL_ENV),
    )
    parser.add_argument(
        "--organizer",
        choices=("configured", "luna"),
        default="configured",
        help=(
            "Use the historical configured Provider or the fixed "
            f"{DEFAULT_AGENT_MODEL_PROFILE} thinking=max evaluation boundary."
        ),
    )
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--timeout-seconds", type=float, default=1_200.0)
    parser.add_argument(
        "--artifact-root",
        default="",
        help=(
            "Optional private mode-0700 directory for resumable Luna prompts, "
            "outputs and failure logs. Omit it for disposable temporary artifacts."
        ),
    )
    parser.add_argument("--report", default="")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print only metrics/failures while preserving the full report file.",
    )
    return parser


def _seed_current_memory(db_path: Path, scenarios: tuple[Scenario, ...]) -> None:
    members_by_topic: dict[str, list[str]] = {}
    with sqlite3.connect(db_path) as conn:
        for index, scenario in enumerate(scenarios):
            atom_id = f"atom:live-eval:{index:02d}:v1"
            members_by_topic.setdefault(scenario.topic, []).append(atom_id)
            conn.execute(
                """
                INSERT INTO memory_atoms(
                    id, kind, text, canonical_text, scope_project,
                    confidence, quality_score, status, created_at_ms,
                    updated_at_ms, owner_kind, owner_id, claim_key,
                    lineage_id, claim_state, valid_from_ms
                ) VALUES (?, ?, ?, ?, ?, 0.99, 0.99, 'active', 10, 10,
                          ?, ?, ?, ?, 'current', 10)
                """,
                (
                    atom_id,
                    scenario.kind,
                    scenario.baseline,
                    scenario.baseline,
                    PROJECT,
                    OWNER_KIND,
                    OWNER_ID,
                    scenario.claim_key,
                    f"lineage:{scenario.claim_key}",
                ),
            )
        forget_atom_id = "atom:live-eval:forget:v1"
        members_by_topic.setdefault("个人协作偏好", []).append(forget_atom_id)
        conn.execute(
            """
            INSERT INTO memory_atoms(
                id, kind, text, canonical_text, scope_project,
                confidence, quality_score, status, created_at_ms,
                updated_at_ms, owner_kind, owner_id, claim_key,
                lineage_id, claim_state, valid_from_ms
            ) VALUES (?, 'durable_preference', ?, ?, ?, 0.99, 0.99,
                      'active', 10, 10, ?, ?, ?, ?, 'current', 10)
            """,
            (
                forget_atom_id,
                FORGET_BASELINE,
                FORGET_BASELINE,
                PROJECT,
                OWNER_KIND,
                OWNER_ID,
                FORGET_CLAIM_KEY,
                f"lineage:{FORGET_CLAIM_KEY}",
            ),
        )
        conflict_atom_id = "atom:live-eval:conflict:v1"
        members_by_topic.setdefault("个人协作偏好", []).append(conflict_atom_id)
        conn.execute(
            """
            INSERT INTO memory_atoms(
                id, kind, text, canonical_text, scope_project,
                confidence, quality_score, status, created_at_ms,
                updated_at_ms, owner_kind, owner_id, claim_key,
                lineage_id, claim_state, valid_from_ms
            ) VALUES (?, 'durable_preference', ?, ?, ?, 0.99, 0.99,
                      'active', 10, 10, ?, ?, ?, ?, 'current', 10)
            """,
            (
                conflict_atom_id,
                CONFLICT_BASELINE,
                CONFLICT_BASELINE,
                PROJECT,
                OWNER_KIND,
                OWNER_ID,
                CONFLICT_CLAIM_KEY,
                f"lineage:{CONFLICT_CLAIM_KEY}",
            ),
        )
        for topic_index, (topic, atom_ids) in enumerate(members_by_topic.items()):
            conn.execute(
                """
                INSERT INTO memory_books(
                    book_id, book_type, book_key, title, summary,
                    normalized_text, project, tags_json,
                    query_expansions_json, memory_atom_ids_json, status,
                    confidence, quality_score, created_at_ms, updated_at_ms,
                    owner_kind, owner_id
                ) VALUES (?, 'topic', ?, ?, ?, ?, ?, ?, ?, ?, 'active',
                          0.99, 0.99, 10, 10, ?, ?)
                """,
                (
                    f"book:live-eval:{topic_index:02d}",
                    f"live-eval-topic-{topic_index:02d}",
                    topic,
                    f"{topic}的现行决策、约束和部署事实。",
                    topic,
                    PROJECT,
                    json.dumps([topic, "当前方案", "项目事实"], ensure_ascii=False),
                    json.dumps([topic, "最新方案", "当前配置"], ensure_ascii=False),
                    json.dumps(atom_ids, ensure_ascii=False),
                    OWNER_KIND,
                    OWNER_ID,
                ),
            )
        conn.commit()


def _checkpoint(
    store: AgentMemorySourceStore,
    *,
    session_id: str,
    label: str,
    text: str,
    created_at_ms: int,
    capture_candidate: bool,
) -> str:
    result = store.checkpoint_user_message(
        session_id=session_id,
        pi_entry_id=f"entry:live-eval:{label}",
        turn_id=f"turn:live-eval:{label}",
        text=text,
        created_at_ms=created_at_ms,
    )
    source = dict(result.get("source") or {})
    source_id = str(source.get("sourceId") or "")
    if capture_candidate and source_id:
        store.capture_hint(
            session_id=session_id,
            source_id=source_id,
            kind="decision",
            claim=text[:800],
            # These are synthetic user-grounded facts/preferences evaluated in
            # the personal Memory domain. ``project`` scope is intentionally
            # quarantined from canonical personal Evidence and would make the
            # governed Luna curator correctly ignore the entire fixture.
            scope="user",
            basis="explicit_user_statement",
            future_use="评估该用户声明是否构成跨 Session 持续的项目约束。",
            created_at_ms=created_at_ms + 1,
        )
    return source_id


def _deterministic_noise_text(index: int) -> str:
    lane = index % 3
    if lane == 0:
        return f"请调用 memory 的 curation_prepare 并返回 runId live-eval-{index:03d}。"
    if lane == 1:
        return f"为什么这个临时测试现在没有输出？编号 {index:03d}。"
    return f"测试一下 123-{index:03d}"


def _add_wave(
    store: AgentMemorySourceStore,
    *,
    session_id: str,
    updates: list[tuple[str, str]],
    semantic_noise_count: int,
    deterministic_noise_count: int,
    created_at_ms: int,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], int]:
    update_sources: dict[str, str] = {}
    semantic_noise_sources: dict[str, str] = {}
    deterministic_noise_sources: dict[str, str] = {}
    entries: list[tuple[str, str, str]] = []
    for index, (label, text) in enumerate(updates):
        entries.append(("update", label, text))
        if index < semantic_noise_count:
            entries.append(
                (
                    "semantic-noise",
                    f"semantic-noise-{index:02d}",
                    SEMANTIC_NOISE[index % len(SEMANTIC_NOISE)],
                )
            )
    while sum(kind == "semantic-noise" for kind, _, _ in entries) < semantic_noise_count:
        index = sum(kind == "semantic-noise" for kind, _, _ in entries)
        entries.append(
            (
                "semantic-noise",
                f"semantic-noise-{index:02d}",
                SEMANTIC_NOISE[index % len(SEMANTIC_NOISE)],
            )
        )
    for index in range(deterministic_noise_count):
        insert_at = min(len(entries), (index * 5) % (len(entries) + 1))
        entries.insert(
            insert_at,
            (
                "deterministic-noise",
                f"deterministic-noise-{index:03d}",
                _deterministic_noise_text(index),
            ),
        )
    for kind, label, text in entries:
        source_id = _checkpoint(
            store,
            session_id=session_id,
            label=f"{created_at_ms}:{label}",
            text=text,
            created_at_ms=created_at_ms,
            # Let the real capture/admission gates reject workflow noise. Any
            # deterministic item that survives those gates must still be
            # presented as canonical Evidence so the Curator's own rules are
            # exercised rather than leaving an unobservable legacy source.
            capture_candidate=True,
        )
        created_at_ms += 1
        if kind == "update":
            update_sources[label] = source_id
        elif kind == "semantic-noise":
            semantic_noise_sources[label] = source_id
        else:
            deterministic_noise_sources[label] = source_id
    return (
        update_sources,
        semantic_noise_sources,
        deterministic_noise_sources,
        created_at_ms,
    )


def _pending_count(curator: OwnerMemoryCurator, current_ms: int) -> int:
    status = curator.status(
        owner_kind=OWNER_KIND,
        owner_id=OWNER_ID,
        current_ms=current_ms,
    )
    scopes = [item for item in status.get("scopes") or [] if isinstance(item, dict)]
    return sum(int(item.get("pendingSourceCount") or 0) for item in scopes)


def _drain(
    curator: OwnerMemoryCurator,
    *,
    current_ms: int,
    max_runs: int = 32,
) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for offset in range(max_runs):
        if _pending_count(curator, current_ms + offset) == 0:
            break
        report = curator.run_due(
            manual=True,
            owner_kind=OWNER_KIND,
            owner_id=OWNER_ID,
            instruction=(
                "这是隔离数据库中的长期记忆语义评测。稳定项目事实必须 remember；"
                "明确说明仅本轮临时发生且不形成决定的内容必须 not_for_memory；"
                "新证据更新现有事实时必须复用 existingMemoryAtoms.claimKey。"
            ),
            current_ms=current_ms + offset,
        )
        reports.append(report)
        if not report.get("ok"):
            break
    return reports


def _source_rows(db_path: Path) -> dict[str, dict[str, object]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return {
            str(row["source_id"]): dict(row)
            for row in conn.execute(
                """
                SELECT source_id, disposition, disposition_reason,
                       curation_run_id
                FROM agent_memory_sources
                """
            ).fetchall()
        }


def _database_snapshot(db_path: Path) -> dict[str, object]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        atoms = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, kind, canonical_text, status, claim_key, lineage_id,
                       claim_state, valid_from_ms, valid_to_ms, supersedes_id,
                       source_event_ids_json
                FROM memory_atoms
                ORDER BY claim_key, valid_from_ms, id
                """
            ).fetchall()
        ]
        books = [
            dict(row)
            for row in conn.execute(
                """
                SELECT book_id, title, status, memory_atom_ids_json
                FROM memory_books
                ORDER BY book_id
                """
            ).fetchall()
        ]
        supersessions = [
            dict(row)
            for row in conn.execute(
                """
                SELECT old_memory_id, new_memory_id, status
                FROM memory_supersessions
                ORDER BY created_at_ms, supersession_id
                """
            ).fetchall()
        ]
        tombstones = [
            dict(row)
            for row in conn.execute(
                """
                SELECT target_type, target_value, reason, active
                FROM memory_tombstones
                ORDER BY id
                """
            ).fetchall()
        ]
    return {
        "atoms": atoms,
        "books": books,
        "supersessions": supersessions,
        "tombstones": tombstones,
    }


def _evaluate(
    *,
    db_path: Path,
    scenarios: tuple[Scenario, ...],
    v3_count: int,
    update_sources: dict[str, str],
    semantic_noise_sources: dict[str, str],
    deterministic_noise_sources: dict[str, str],
    forget_source_id: str,
    conflict_source_id: str,
    organizer: CapturingOrganizer,
    reports: list[dict[str, object]],
) -> dict[str, object]:
    snapshot = _database_snapshot(db_path)
    source_rows = _source_rows(db_path)
    atoms = [dict(item) for item in snapshot["atoms"]]
    current_by_claim: dict[str, list[dict[str, object]]] = {}
    history_by_claim: dict[str, list[dict[str, object]]] = {}
    for atom in atoms:
        claim_key = str(atom.get("claim_key") or "")
        history_by_claim.setdefault(claim_key, []).append(atom)
        if atom.get("status") == "active" and atom.get("claim_state") == "current":
            current_by_claim.setdefault(claim_key, []).append(atom)

    failures: list[dict[str, object]] = []
    correct_current = 0
    correct_history_depth = 0
    for index, scenario in enumerate(scenarios):
        rows = current_by_claim.get(scenario.claim_key, [])
        expected_terms = scenario.v3_terms if index < v3_count else scenario.v2_terms
        if len(rows) != 1:
            failures.append(
                {
                    "kind": "current_claim_cardinality",
                    "claimKey": scenario.claim_key,
                    "expected": 1,
                    "actual": len(rows),
                    "currentTexts": [row.get("canonical_text") for row in rows],
                }
            )
        else:
            text = str(rows[0].get("canonical_text") or "")
            missing = [
                term
                for term in expected_terms
                if not any(
                    alternative.casefold() in text.casefold()
                    for alternative in term.split("|")
                    if alternative
                )
            ]
            if missing:
                failures.append(
                    {
                        "kind": "current_value_mismatch",
                        "claimKey": scenario.claim_key,
                        "text": text,
                        "missingTerms": missing,
                    }
                )
            else:
                correct_current += 1
        expected_depth = 3 if index < v3_count else 2
        claim_history = history_by_claim.get(scenario.claim_key, [])
        superseded = [row for row in claim_history if row.get("claim_state") == "superseded"]
        if len(claim_history) == expected_depth and len(superseded) == expected_depth - 1:
            correct_history_depth += 1
        else:
            failures.append(
                {
                    "kind": "claim_history_mismatch",
                    "claimKey": scenario.claim_key,
                    "expectedDepth": expected_depth,
                    "actualDepth": len(claim_history),
                    "supersededCount": len(superseded),
                }
            )

    forget_history = history_by_claim.get(FORGET_CLAIM_KEY, [])
    forget_current = current_by_claim.get(FORGET_CLAIM_KEY, [])
    forget_retracted = [
        row
        for row in forget_history
        if row.get("status") == "tombstoned"
        and row.get("claim_state") == "retracted"
    ]
    active_forget_tombstones = [
        row
        for row in snapshot["tombstones"]
        if row.get("target_type") == "memory_id"
        and row.get("target_value") == "atom:live-eval:forget:v1"
        and int(row.get("active") or 0) == 1
    ]
    if (
        len(forget_history) != 1
        or forget_current
        or len(forget_retracted) != 1
        or len(active_forget_tombstones) != 1
    ):
        failures.append(
            {
                "kind": "explicit_forget_not_retracted",
                "historyCount": len(forget_history),
                "currentCount": len(forget_current),
                "retractedCount": len(forget_retracted),
                "activeTombstoneCount": len(active_forget_tombstones),
            }
        )

    conflict_history = history_by_claim.get(CONFLICT_CLAIM_KEY, [])
    conflict_current = current_by_claim.get(CONFLICT_CLAIM_KEY, [])
    conflict_preserved = (
        len(conflict_history) == 1
        and len(conflict_current) == 1
        and str(conflict_current[0].get("canonical_text") or "")
        == CONFLICT_BASELINE
        and conflict_current[0].get("status") == "active"
        and conflict_current[0].get("claim_state") == "current"
    )
    if not conflict_preserved:
        failures.append(
            {
                "kind": "temporary_conflict_overrode_current_claim",
                "historyCount": len(conflict_history),
                "currentCount": len(conflict_current),
                "currentTexts": [row.get("canonical_text") for row in conflict_current],
            }
        )

    update_dispositions = {
        label: str(source_rows.get(source_id, {}).get("disposition") or "missing")
        for label, source_id in update_sources.items()
    }
    forget_disposition = str(
        source_rows.get(forget_source_id, {}).get("disposition") or "missing"
    )
    conflict_disposition = str(
        source_rows.get(conflict_source_id, {}).get("disposition") or "missing"
    )
    semantic_noise_dispositions = {
        label: str(source_rows.get(source_id, {}).get("disposition") or "missing")
        for label, source_id in semantic_noise_sources.items()
    }
    deterministic_noise_dispositions = {
        label: (
            str(source_rows.get(source_id, {}).get("disposition") or "missing")
            if source_id
            else "precheck_rejected"
        )
        for label, source_id in deterministic_noise_sources.items()
    }
    for label, disposition in update_dispositions.items():
        if disposition != "consolidated":
            failures.append(
                {"kind": "update_source_not_consolidated", "source": label, "disposition": disposition}
            )
    if forget_disposition != "consolidated":
        failures.append(
            {
                "kind": "forget_source_not_consolidated",
                "disposition": forget_disposition,
            }
        )
    if conflict_disposition != "not_for_memory":
        failures.append(
            {
                "kind": "temporary_conflict_not_rejected",
                "disposition": conflict_disposition,
            }
        )
    for label, disposition in semantic_noise_dispositions.items():
        if disposition != "not_for_memory":
            failures.append(
                {"kind": "semantic_noise_not_rejected", "source": label, "disposition": disposition}
            )
    for label, disposition in deterministic_noise_dispositions.items():
        if disposition not in {"not_for_memory", "precheck_rejected"}:
            failures.append(
                {"kind": "deterministic_noise_not_rejected", "source": label, "disposition": disposition}
            )

    active_current_ids = {
        str(atom.get("id") or "")
        for atom in atoms
        if atom.get("status") == "active" and atom.get("claim_state") == "current"
    }
    invalid_book_members: list[dict[str, str]] = []
    for book in snapshot["books"]:
        if book.get("status") != "active":
            continue
        for atom_id in json.loads(str(book.get("memory_atom_ids_json") or "[]")):
            if atom_id not in active_current_ids:
                invalid_book_members.append(
                    {"bookId": str(book.get("book_id") or ""), "atomId": str(atom_id)}
                )
    if invalid_book_members:
        failures.append(
            {"kind": "book_references_non_current_atom", "items": invalid_book_members[:20]}
        )

    prompt_tokens = sum(
        int(dict(call.get("diagnostics") or {}).get("promptTokens") or 0)
        + int(dict(dict(call.get("diagnostics") or {}).get("retry") or {}).get("promptTokens") or 0)
        for call in organizer.calls
    )
    completion_tokens = sum(
        int(dict(call.get("diagnostics") or {}).get("completionTokens") or 0)
        + int(dict(dict(call.get("diagnostics") or {}).get("retry") or {}).get("completionTokens") or 0)
        for call in organizer.calls
    )
    all_source_dispositions = Counter(
        str(row.get("disposition") or "") for row in source_rows.values()
    )
    return {
        "passed": not failures,
        "metrics": {
            "scenarioCount": len(scenarios),
            "expectedUpdateCount": len(scenarios) + v3_count + 1,
            "forgetCaseCount": 1,
            "forgetRetractedCount": int(
                len(forget_retracted) == 1
                and not forget_current
                and len(active_forget_tombstones) == 1
            ),
            "temporaryConflictCaseCount": 1,
            "temporaryConflictPreservedCount": int(
                conflict_preserved and conflict_disposition == "not_for_memory"
            ),
            "correctCurrentValueCount": correct_current,
            "correctClaimHistoryCount": correct_history_depth,
            "updateSourceCount": len(update_sources),
            "updateSourceConsolidatedCount": sum(
                disposition == "consolidated" for disposition in update_dispositions.values()
            ),
            "semanticNoiseCount": len(semantic_noise_sources),
            "semanticNoiseRejectedCount": sum(
                disposition == "not_for_memory"
                for disposition in semantic_noise_dispositions.values()
            ),
            "deterministicNoiseCount": len(deterministic_noise_sources),
            "deterministicNoiseRejectedCount": sum(
                disposition in {"not_for_memory", "precheck_rejected"}
                for disposition in deterministic_noise_dispositions.values()
            ),
            "deterministicNoisePrecheckRejectedCount": sum(
                disposition == "precheck_rejected"
                for disposition in deterministic_noise_dispositions.values()
            ),
            "modelCallCount": len(organizer.calls),
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
            "supersessionEdgeCount": len(snapshot["supersessions"]),
            "invalidBookMemberCount": len(invalid_book_members),
            "sourceDispositionCounts": dict(sorted(all_source_dispositions.items())),
            "runCount": len(reports),
        },
        "failures": failures,
        "calls": organizer.calls,
        "runReports": reports,
        "currentFacts": {
            claim_key: [str(row.get("canonical_text") or "") for row in rows]
            for claim_key, rows in sorted(current_by_claim.items())
            if claim_key in {scenario.claim_key for scenario in scenarios}
        },
    }


def _build_organizer(
    *,
    organizer_kind: str,
    db_path: Path,
    artifact_root: Path,
    model_env_path: Path,
    codex_bin: str,
    timeout_seconds: float,
) -> tuple[CapturingOrganizer, dict[str, str]]:
    if organizer_kind == "luna":
        executor = PrivateCodexLunaMemoryExecutor(
            artifact_root,
            audit_db_path=db_path,
            timeout_seconds=timeout_seconds,
            codex_bin=codex_bin,
        )
        return CapturingOrganizer(ManagedPiMemoryOrganizer(executor)), {
            "provider": executor.provider,
            "model": executor.model_id,
            "thinking": executor.thinking_level,
            "transport": executor.transport,
        }
    if organizer_kind != "configured":
        raise ValueError(f"unsupported organizer: {organizer_kind}")
    config = load_deepseek_config(model_env_path)
    if not config.api_key:
        raise RuntimeError(f"no model credential found in {model_env_path}")
    return CapturingOrganizer(DeepSeekMemoryOrganizer(config)), {
        "provider": config.provider_name,
        "model": config.model,
        "thinking": "configured",
        "transport": "configured_http",
    }


def _model_execution_summary(
    organizer: CapturingOrganizer,
    reports: list[dict[str, object]],
) -> dict[str, object]:
    failed_results = [
        dict(result)
        for report in reports
        if isinstance(report, dict)
        for result in report.get("results") or []
        if isinstance(result, dict) and result.get("ok") is not True
    ]
    model_call_count = len(organizer.calls)
    if failed_results:
        status = "failed_before_completion" if model_call_count == 0 else "failed_after_partial_completion"
    elif model_call_count > 0:
        status = "completed"
    else:
        status = "not_started"
    return {
        "status": status,
        "modelCallCount": model_call_count,
        "curationRunCount": len(reports),
        "failedResultCount": len(failed_results),
        "completed": status == "completed",
    }


def run(
    profile: str,
    model_env_path: Path,
    *,
    organizer_kind: str = "configured",
    codex_bin: str = "codex",
    timeout_seconds: float = 1_200.0,
    artifact_root: Path | None = None,
) -> dict[str, object]:
    scenario_count = 6 if profile == "calibration" else len(SCENARIOS)
    v3_count = 0 if profile == "calibration" else 9
    scenarios = SCENARIOS[:scenario_count]
    semantic_noise_v2 = 3 if profile == "calibration" else 12
    deterministic_noise_v2 = 12 if profile == "calibration" else 60
    semantic_noise_v3 = 0 if profile == "calibration" else 6
    deterministic_noise_v3 = 0 if profile == "calibration" else 30

    with tempfile.TemporaryDirectory(prefix="rag-ime-live-model-eval-") as tmp:
        db_path = Path(tmp) / "rag-ime-live-eval.sqlite"
        sessions = AgentSessionStore(db_path)
        sessions.initialize()
        session = sessions.create(
            title="真实模型记忆更新评测",
            role_id="companion-present-v1",
            role_version="1",
            created_at_ms=1,
        )
        sources = AgentMemorySourceStore(db_path, project=PROJECT)
        sources.initialize()
        organizer, model_identity = _build_organizer(
            organizer_kind=organizer_kind,
            db_path=db_path,
            artifact_root=(
                artifact_root.expanduser().resolve(strict=False)
                if artifact_root is not None
                else Path(tmp) / "model-artifacts"
            ),
            model_env_path=model_env_path,
            codex_bin=codex_bin,
            timeout_seconds=timeout_seconds,
        )
        curator = OwnerMemoryCurator(
            db_path,
            organizer=organizer,
            project=PROJECT,
            initial_settle_ms=0,
            auto_apply=True,
            max_sources=64,
        )
        curator.initialize()
        _seed_current_memory(db_path, scenarios)

        update_sources: dict[str, str] = {}
        semantic_noise_sources: dict[str, str] = {}
        deterministic_noise_sources: dict[str, str] = {}
        created_at_ms = 1_000
        v2_updates = [
            (f"v2:{index:02d}", scenario.v2_input)
            for index, scenario in enumerate(scenarios)
        ]
        update, semantic, deterministic, created_at_ms = _add_wave(
            sources,
            session_id=str(session["id"]),
            updates=v2_updates,
            semantic_noise_count=semantic_noise_v2,
            deterministic_noise_count=deterministic_noise_v2,
            created_at_ms=created_at_ms,
        )
        update_sources.update(update)
        semantic_noise_sources.update({f"v2:{key}": value for key, value in semantic.items()})
        deterministic_noise_sources.update({f"v2:{key}": value for key, value in deterministic.items()})
        forget_source_id = _checkpoint(
            sources,
            session_id=str(session["id"]),
            label=f"{created_at_ms}:forget:completion-sound",
            text=FORGET_INPUT,
            created_at_ms=created_at_ms,
            capture_candidate=True,
        )
        created_at_ms += 1
        conflict_source_id = _checkpoint(
            sources,
            session_id=str(session["id"]),
            label=f"{created_at_ms}:conflict:answer-length",
            text=CONFLICT_INPUT,
            created_at_ms=created_at_ms,
            capture_candidate=True,
        )
        created_at_ms += 1
        reports = _drain(curator, current_ms=1_000_000)

        if v3_count:
            compound = "；".join(scenario.v3_input.rstrip("。") for scenario in scenarios[:3]) + "。"
            v3_updates = [("v3:compound-00-02", compound)] + [
                (f"v3:{index:02d}", scenarios[index].v3_input)
                for index in range(3, v3_count)
            ]
            update, semantic, deterministic, created_at_ms = _add_wave(
                sources,
                session_id=str(session["id"]),
                updates=v3_updates,
                semantic_noise_count=semantic_noise_v3,
                deterministic_noise_count=deterministic_noise_v3,
                created_at_ms=created_at_ms,
            )
            update_sources.update(update)
            semantic_noise_sources.update({f"v3:{key}": value for key, value in semantic.items()})
            deterministic_noise_sources.update({f"v3:{key}": value for key, value in deterministic.items()})
            reports.extend(_drain(curator, current_ms=2_000_000))

        evaluation = _evaluate(
            db_path=db_path,
            scenarios=scenarios,
            v3_count=v3_count,
            update_sources=update_sources,
            semantic_noise_sources=semantic_noise_sources,
            deterministic_noise_sources=deterministic_noise_sources,
            forget_source_id=forget_source_id,
            conflict_source_id=conflict_source_id,
            organizer=organizer,
            reports=reports,
        )
        model_execution = _model_execution_summary(organizer, reports)
        identity_gate = bool(model_identity["provider"] and model_identity["model"])
        if organizer_kind == "luna":
            identity_gate = (
                model_identity["provider"] == "openai-codex"
                and model_identity["model"] == DEFAULT_AGENT_MODEL_ID
                and model_identity["thinking"] == "max"
                and model_identity["transport"] == "codex_cli_ephemeral"
            )
        hard_gates = {
            "isolatedTemporaryDatabase": True,
            "productionDatabaseUntouched": True,
            "modelIdentity": identity_gate,
            "modelExecution": bool(model_execution["completed"]),
            "claimLifecycleAndNoiseQuality": bool(evaluation.get("passed")),
        }
        return {
            "schemaVersion": "rag-ime.owner-memory-live-model-eval.v3",
            "profile": profile,
            "database": "temporary-isolated-sqlite",
            "productionDatabaseTouched": False,
            "localOnly": True,
            "uploaded": False,
            "organizer": organizer_kind,
            **model_identity,
            **evaluation,
            "execution": model_execution,
            "scoreEligible": all(
                hard_gates[name]
                for name in (
                    "isolatedTemporaryDatabase",
                    "productionDatabaseUntouched",
                    "modelIdentity",
                    "modelExecution",
                )
            ),
            "hardGates": hard_gates,
            "passed": all(hard_gates.values()),
        }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run(
        args.profile,
        Path(args.model_env_path).expanduser(),
        organizer_kind=str(args.organizer),
        codex_bin=str(args.codex_bin),
        timeout_seconds=float(args.timeout_seconds),
        artifact_root=(
            Path(args.artifact_root)
            if str(args.artifact_root).strip()
            else None
        ),
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        report_path = Path(args.report).expanduser()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(serialized + "\n", encoding="utf-8")
    printed = (
        json.dumps(
            {
                "passed": report["passed"],
                "profile": report["profile"],
                "database": report["database"],
                "productionDatabaseTouched": report["productionDatabaseTouched"],
                "organizer": report["organizer"],
                "provider": report["provider"],
                "model": report["model"],
                "thinking": report["thinking"],
                "transport": report["transport"],
                "execution": report["execution"],
                "scoreEligible": report["scoreEligible"],
                "hardGates": report["hardGates"],
                "metrics": report["metrics"],
                "failures": report["failures"],
                "claimKeyReconciliations": [
                    item
                    for run in report.get("runReports") or []
                    if isinstance(run, dict)
                    for result in run.get("results") or []
                    if isinstance(result, dict)
                    for item in result.get("claimKeyReconciliations") or []
                    if isinstance(item, dict)
                ],
                "memoryAtomRepairs": [
                    item
                    for run in report.get("runReports") or []
                    if isinstance(run, dict)
                    for result in run.get("results") or []
                    if isinstance(result, dict)
                    for item in result.get("memoryAtomRepairs") or []
                    if isinstance(item, dict)
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        if args.summary_only
        else serialized
    )
    print(printed)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
