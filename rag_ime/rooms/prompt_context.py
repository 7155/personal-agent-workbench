from __future__ import annotations

from collections.abc import Mapping, Sequence

from rag_ime.agent_definitions import collaboration_role
from rag_ime.rooms.prompt_support import (
    ROOM_CONTEXT_HISTORY_CHAR_BUDGET,
    ROOM_CONTEXT_PROMPT_CHAR_BUDGET,
    ROOM_CONTEXT_UNREAD_MESSAGE_LIMIT,
    _acceptance,
    _bounded_transcript,
    _budgeted_prompt,
    _document_rows,
    _integer,
    _operation_hint,
    _related_to,
    _relation,
    _text,
    _work_action,
    _work_state,
    agent_message_text,
)


_OPEN_WORK_STATES = {"queued", "active", "review", "blocked"}


def room_participant_prompt(
    room: Mapping[str, object],
    target: Mapping[str, object],
    message: str,
    *,
    recent_messages: Sequence[Mapping[str, object]] = (),
    omitted_message_count: int = 0,
    request_heading: str = "用户在 Room 中的请求",
    work_item: Mapping[str, object] | None = None,
    work_documents: Sequence[Mapping[str, object]] = (),
    work_document_authorities: Mapping[str, Mapping[str, object]] | None = None,
) -> str:
    """Build a bounded Room state delta, not a second workflow manual."""

    participants = [
        item
        for item in room.get("participants", [])
        if isinstance(item, Mapping) and item.get("status") == "active"
    ]
    names = {
        _text(item.get("id"), 240): _text(item.get("displayName"), 40)
        for item in participants
    }
    sessions = {
        _text(item.get("id"), 240): _text(item.get("sessionId"), 320)
        for item in participants
    }
    role = _text(target.get("collaborationRole"), 40) or "implementer"
    # Set only on the prepared-dispatch copy, never persisted Room metadata.
    purpose = room.get("_jevExecutionPurpose")
    if purpose not in {"plan", "execute", "verify", "synthesize"}:
        purpose = None
    try:
        role_label = collaboration_role(role).display_name
    except ValueError:
        role_label = "协作伙伴"

    active_topic_id = str(room.get("activeTopicId") or "")
    active_topic = next(
        (
            item
            for item in room.get("topics", [])
            if isinstance(item, Mapping)
            and str(item.get("id") or "") == active_topic_id
        ),
        {},
    )
    topic_title = _text(active_topic.get("title"), 120) or "主话题"
    target_id = str(target.get("id") or "")
    open_work = [
        item
        for item in room.get("workItems", [])
        if isinstance(item, Mapping)
        and str(item.get("state") or "") in _OPEN_WORK_STATES
    ]
    related = [item for item in open_work if _related_to(item, target_id)]

    opening = [
        "<room-context>",
        f"Room：{_text(room.get('title'), 120)}；话题：{topic_title}；当前角色：{role_label}",
    ]
    for item in related[:4]:
        feedback = item.get("blocker")
        feedback = feedback if isinstance(feedback, Mapping) else {}
        if not purpose and role == "coordinator" and str(item.get("state") or "") == "active" and _text(
            feedback.get("reviewFeedback"), 500
        ):
            opening.append(
                "返修待重新派发（按当前 Runtime 建议执行，不要新建替代任务）："
                f"op=retry、workItemId={_text(item.get('id'), 240)}、"
                f"expectedRevision={_integer(item.get('revision'))}；"
                "Partner 重新提交到 review 后再验收。"
            )

    context: list[str] = []
    summary = _text(active_topic.get("summary"), 600)
    scenario = _text(room.get("scenarioPrompt"), 800)
    if summary:
        context.append(f"话题摘要：{summary}")
    if scenario:
        context.append(f"Room 补充设定（不改变权限）：{scenario}")

    transcript, omitted = _bounded_transcript(recent_messages, names)
    omitted += max(0, omitted_message_count)
    if transcript or omitted:
        rows = ["", "本次新增的公开对话：", *(transcript or ["- 无"])]
        if omitted:
            rows.append(f"- 另有 {omitted} 条较早消息未注入；按需读取公开摘要、产物或状态。")
        context.append("\n".join(rows))

    # Preserve the current assignment before the broader ledger under pressure.
    if work_item is not None:
        work_id = _text(work_item.get("id"), 240)
        rows = [
            "",
            "当前工作卡片：",
            f"WorkItem：{work_id}",
            (
                f"状态：{_work_state(work_item.get('state'))}；"
                f"revision={_integer(work_item.get('revision'))}"
            ),
            f"目标：{_text(work_item.get('objective'), 1_000)}",
            f"预期产物：{_text(work_item.get('expectedOutput'), 1_000)}",
            f"验收条件：{_acceptance(work_item.get('acceptanceCriteria'))}",
        ]
        hint = "" if purpose else _operation_hint(work_item)
        if hint:
            rows.append(f"Runtime 操作提示：{hint}")
        authority = (work_document_authorities or {}).get(f"room_work_item:{work_id}", {})
        if authority:
            rows.append(
                "活动文档 authority（使用此回执，不要猜 WorkItem revision）："
                "authorityKind=room_work_item；"
                f"authorityId={work_id}；"
                f"authorityRevision={_integer(authority.get('authorityRevision'))}；"
                f"transitionReceiptId={_text(authority.get('transitionReceiptId'), 240)}"
            )
        context.append("\n".join(rows))

    if related:
        rows = ["", "与你有关的未完成工作："]
        for item in related[:6]:
            hint = "" if purpose else _operation_hint(item)
            rows.append(
                f"- WorkItem {_text(item.get('id'), 240)} · {_relation(item, target_id)} · "
                f"{_work_state(item.get('state'))} · revision={_integer(item.get('revision'))}："
                f"{_text(item.get('objective'), 320)}"
                + (f"；{hint}" if hint else "")
            )
        context.append("\n".join(rows))

    documents = _document_rows(
        open_work,
        related,
        work_documents,
        work_document_authorities or {},
        names,
        sessions,
    )
    if documents:
        context.append(
            "\n".join(
                [
                    "",
                    "相关 WorkDocument 指针：",
                    *documents,
                    "WorkDocument 是可选语义证据。按需核对当前绑定并读取明确引用；同步失败不阻断非文档交付。",
                ]
            )
        )

    work_item_id = _text(work_item.get("id"), 240) if work_item is not None else ""
    tail: list[str] = []
    if (_text(room.get("roomKind"), 40) or "collaboration") == "collaboration":
        if purpose:
            responsibility, submission_op = {
                "plan": ("与用户澄清目标、范围和验收条件，形成待审阅方案；不要执行方案中的工作", "plan_submit"),
                "execute": ("推进本次已分派的责任，读取实际材料并提供成果与检查证据", "result_submit"),
                "verify": ("独立核验 ExecutionPack 中绑定的固定成果，检查真实产物；不改写成果、不接管原负责人", "verification_submit"),
                "synthesize": ("综合已有验收成果与未解决项，形成一次用户交付；不扩展执行范围", "final_submit"),
            }[purpose]
            tail.append("\n".join([
                "", f"当前 Jev 执行目的：{purpose}。{responsibility}。",
                "所有伙伴执行地位相同，历史协作角色只提供背景；主要对话和规划者也可承担已分派任务。",
                "以当前 ExecutionPack 的目的、任务版本、材料和已披露 Tool schema 为准；图状态与调度由宿主负责。",
                f"本回合只用 room_partner op={submission_op} 提交对应结构化结果，成功后结束。",
                "不要调用旧 delegate/retry/accept/return，不自行启动其他伙伴、增加批准或发布其他 Root 终态。",
                "提交回执不等于正式验收，也不等于 Runtime 已停止。所有成功结论必须有真实工具/产物证据。",
            ]))
        elif role == "coordinator":
            tail.append(
                "\n".join(
                    [
                        "",
                        (
                            "当前职责：Room Facilitator。普通对话或一个连贯动作直接处理。"
                            "只有当前请求确实需要多个可见且独立负责的结果、依赖阶段或"
                            "有收益的并行时，才用 skill_load 加载 facilitate-room。"
                            "私有辅助结果使用 agents；用户可见责任才使用 room_partner。"
                        ),
                        (
                            "Room Context 只提供当前状态。优先使用已有 WorkItem、"
                            "allowedOperations、recommendedOperation 和 live revision；"
                            "具体参数以当前 Tool schema 和 Tool 回执为准，不要从提示词"
                            "重建 Room 状态机，也不要为了展示多 Agent 机械委派。"
                        ),
                        (
                            "Partner 交付只是 submission，不是 acceptance。Facilitator "
                            "检查实际产物与证据，分别判断 operability 和 requirement "
                            "satisfaction，再按当前合法操作验收、返修、改派或记录阻塞。"
                            "Partner 之间直接 peer 通信，Facilitator 不做消息中转。"
                        ),
                        (
                            "所有责任对账后只发布一个 Root 结果。模型表达集成结论、证据、"
                            "风险和下一步；唯一终态、幂等、Goal 完成、wake 抑制和 turn "
                            "settlement 由 Runtime 负责。"
                        ),
                    ]
                )
            )
        else:
            submission = (
                (
                    f"当前 WorkItem 为 {work_item_id}。完成后按当前 room_partner schema "
                    "提交 typed work_result，携带真实 evidence/artifact refs、"
                    "proposedOperabilityVerdict 与 proposedRequirementVerdict。"
                )
                if work_item_id
                else "没有结构化 WorkItem 时，不要自行制造或接管 Room Root。"
            )
            tail.append(
                "\n".join(
                    [
                        "",
                        (
                            "当前职责：Room Partner。只推进自己明确拥有的 WorkItem，并使用"
                            "适合该任务的普通 Session Skill；不要加载 facilitate-room，也不要"
                            "代替 Facilitator 接受其他工作或发布 Root 结果。"
                        ),
                        (
                            "需要伙伴信息时直接使用 peer 操作。关键事实仍需可核对来源或"
                            " Tool 回执；消息、文档和 Agent 结论本身不等于验收。"
                        ),
                        submission,
                        (
                            "typed work_result 只会把 WorkItem 提交到 review，不会自动验收；"
                            "后续状态和恢复以 Runtime 投影与当前 Tool schema 为准。"
                        ),
                    ]
                )
            )
        tail.append(
            "\n".join(
                [
                    "",
                    (
                        "当前工作卡片已在上文给出；不要扩大其范围。"
                        if work_item is not None or purpose
                        else (
                            "当前没有结构化 WorkItem；先判断这是普通对话、Facilitator "
                            "直做，还是确有协调需要。"
                        )
                    ),
                    (
                        "只有可核对的 Runtime/Tool 回执、产物和证据支持成功声明。"
                        "能从源码、配置或运行状态查明的事实自行核对；只有会改变结果的"
                        "用户选择才使用 alignment-and-decision。"
                    ),
                ]
            )
        )

    return _budgeted_prompt(opening, context, tail, request_heading, message)


def room_intercom_prompt(
    room: Mapping[str, object],
    target: Mapping[str, object],
    item: Mapping[str, object],
    *,
    source: Mapping[str, object],
    work: Mapping[str, object] | None,
) -> str:
    del room, target
    kind = str(item.get("kind") or "send")
    action = str(item.get("workAction") or "")
    prefix = f"你刚收到伙伴 {source.get('displayName')} 发来的协作消息。"
    if work is not None:
        prefix += (
            f"这条消息与当前工作“{_text(work.get('objective'), 320)}”有关，"
            f"对方希望你{_work_action(action)}。"
        )
    private_notice = (
        kind == "send"
        and work is None
        and not action
        and not str(item.get("replyTo") or "").strip()
    )
    if private_notice:
        return (
            prefix
            + "这是一条只供你参考的内部消息。按需继续工作，不要公开重复或回复"
            "礼貌回声；无需行动时直接结束本轮。"
        )
    if kind == "ask":
        return (
            prefix
            + "对方需要你的答复。判断后在 Room 中直接回复结论，不要只在私下说"
            "已经回复。"
        )
    if kind == "reply":
        return (
            prefix
            + "这是对先前问题的答复。将其作为输入继续，但关键事实仍需可核对来源"
            "或 Tool 回执。"
        )
    return prefix + "只在当前工作需要时使用，不必机械复述。"
