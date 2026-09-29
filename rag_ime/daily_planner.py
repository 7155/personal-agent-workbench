from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date, datetime
from typing import Mapping, Sequence

from .memory_ingest import normalize_text
from .text_utils import compact_whitespace, now_ms, stable_text_hash, token_terms, truncate_text


PLANNING_SCHEMA_VERSION = "rag-ime.planning.v1"
PLANNING_CONTEXT_SCHEMA_VERSION = "rag-ime.planning-context.v1"

_TASK_STATUSES = {"todo", "in_progress", "done", "cancelled"}
_GOAL_STATUSES = {"active", "completed", "archived"}
_COMPLETION_MARKERS = (
    "完成了",
    "已经完成",
    "已完成",
    "做完了",
    "已经做完",
    "搞定了",
    "已搞定",
    "finished",
    "completed",
    "done with",
)
_NEGATED_COMPLETION_MARKERS = (
    "还没完成",
    "没有完成",
    "没完成",
    "未完成",
    "尚未完成",
    "待完成",
    "需要完成",
    "准备完成",
    "not finished",
    "not completed",
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def local_date_string(value: datetime | date | None = None) -> str:
    if value is None:
        return datetime.now().astimezone().date().isoformat()
    if isinstance(value, datetime):
        return value.astimezone().date().isoformat() if value.tzinfo else value.date().isoformat()
    return value.isoformat()


def planning_dashboard(
    conn: sqlite3.Connection,
    *,
    plan_date: str = "",
    project: str = "",
) -> dict[str, object]:
    day = _validated_date(plan_date or local_date_string())
    daily = conn.execute(
        """
        SELECT plan_id, plan_date, project, intention, notes, reflection,
               assistant_summary, created_at_ms, updated_at_ms, metadata_json
        FROM planning_daily
        WHERE plan_date = ? AND project = ?
        LIMIT 1
        """,
        (day, compact_whitespace(project)),
    ).fetchone()
    task_rows = conn.execute(
        """
        SELECT task_id, plan_date, title, detail, status, priority, due_at_ms,
               project, goal_id, source, confidence, created_at_ms,
               updated_at_ms, completed_at_ms, metadata_json
        FROM planning_tasks
        WHERE plan_date = ? AND (? = '' OR project = ? OR project = '')
        ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'todo' THEN 1
                             WHEN 'done' THEN 2 ELSE 3 END,
                 priority DESC, COALESCE(due_at_ms, 9223372036854775807), updated_at_ms DESC
        """,
        (day, project, project),
    ).fetchall()
    goal_rows = conn.execute(
        """
        SELECT goal_id, title, detail, horizon, status, priority, target_date,
               project, created_at_ms, updated_at_ms, completed_at_ms, metadata_json
        FROM planning_goals
        WHERE status = 'active' AND (? = '' OR project = ? OR project = '')
        ORDER BY priority DESC, CASE WHEN target_date = '' THEN 1 ELSE 0 END,
                 target_date ASC, updated_at_ms DESC
        """,
        (project, project),
    ).fetchall()
    suggestion_rows = conn.execute(
        """
        SELECT suggestion_id, source_event_id, candidate_task_ids_json,
               created_at_ms, metadata_json
        FROM planning_completion_suggestions
        WHERE status = 'pending'
        ORDER BY created_at_ms DESC
        LIMIT 20
        """
    ).fetchall()
    message_rows = conn.execute(
        """
        SELECT message_id, role, content, created_at_ms, metadata_json
        FROM planning_assistant_messages
        WHERE plan_date = ? AND project = ?
        ORDER BY created_at_ms DESC
        LIMIT 20
        """,
        (day, compact_whitespace(project)),
    ).fetchall()
    tasks = [_task_payload(row) for row in task_rows]
    goals = [_goal_payload(row) for row in goal_rows]
    pending = [_suggestion_payload(row, tasks=tasks) for row in suggestion_rows]
    recent_detected_completion = _recent_detected_completion(
        conn,
        day=day,
        project=compact_whitespace(project),
    )
    completed = sum(1 for item in tasks if item["status"] == "done")
    open_count = sum(1 for item in tasks if item["status"] in {"todo", "in_progress"})
    plan = _daily_payload(daily, day=day, project=project)
    assistant = compact_whitespace(str(plan.get("assistantSummary") or "")) or _assistant_summary(
        day=day,
        tasks=tasks,
        goals=goals,
    )
    return {
        "schemaVersion": PLANNING_SCHEMA_VERSION,
        "ok": True,
        "date": day,
        "plan": plan,
        "tasks": tasks,
        "goals": goals,
        "pendingCompletionSuggestions": pending,
        "recentDetectedCompletion": recent_detected_completion,
        "conversation": [
            {
                "id": str(row["message_id"]),
                "role": str(row["role"] or "assistant"),
                "content": str(row["content"] or ""),
                "createdAtMs": int(row["created_at_ms"] or 0),
                "metadata": _json_object(row["metadata_json"]),
            }
            for row in reversed(message_rows)
        ],
        "summary": {
            "taskCount": len(tasks),
            "openTaskCount": open_count,
            "completedTaskCount": completed,
            "goalCount": len(goals),
            "progress": completed / max(1, len(tasks)),
        },
        "assistant": {
            "message": assistant,
            "tone": "celebrate" if tasks and completed == len(tasks) else ("encourage" if completed else "focus"),
        },
    }


def save_daily_plan(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
    *,
    project: str = "",
) -> dict[str, object]:
    day = _validated_date(str(payload.get("date") or payload.get("planDate") or local_date_string()))
    resolved_project = compact_whitespace(str(payload.get("project") or project))
    timestamp = now_ms()
    plan_id = compact_whitespace(str(payload.get("id") or payload.get("planId") or ""))
    if not plan_id:
        plan_id = f"plan:{day}:{stable_text_hash(resolved_project or 'global').split(':')[-1][:10]}"
    intention = compact_whitespace(str(payload.get("intention") or ""))
    notes = compact_whitespace(str(payload.get("notes") or ""))
    reflection = compact_whitespace(str(payload.get("reflection") or ""))
    assistant_summary = compact_whitespace(str(payload.get("assistantSummary") or ""))
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    conn.execute(
        """
        INSERT INTO planning_daily(
            plan_id, plan_date, project, intention, notes, reflection,
            assistant_summary, created_at_ms, updated_at_ms, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(plan_date, project) DO UPDATE SET
            intention = excluded.intention,
            notes = excluded.notes,
            reflection = excluded.reflection,
            assistant_summary = excluded.assistant_summary,
            updated_at_ms = excluded.updated_at_ms,
            metadata_json = excluded.metadata_json
        """,
        (
            plan_id,
            day,
            resolved_project,
            intention,
            notes,
            reflection,
            assistant_summary,
            timestamp,
            timestamp,
            json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True),
        ),
    )
    return planning_dashboard(conn, plan_date=day, project=resolved_project)


def save_goal(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
    *,
    project: str = "",
) -> dict[str, object]:
    title = compact_whitespace(str(payload.get("title") or ""))
    if not title:
        raise ValueError("goal title is required")
    goal_id = compact_whitespace(str(payload.get("id") or payload.get("goalId") or "")) or f"goal:{uuid.uuid4().hex}"
    status = compact_whitespace(str(payload.get("status") or "active")).lower()
    if status not in _GOAL_STATUSES:
        raise ValueError(f"unsupported goal status: {status}")
    timestamp = now_ms()
    previous = conn.execute("SELECT created_at_ms FROM planning_goals WHERE goal_id = ?", (goal_id,)).fetchone()
    completed_at = timestamp if status == "completed" else None
    conn.execute(
        """
        INSERT OR REPLACE INTO planning_goals(
            goal_id, title, detail, horizon, status, priority, target_date,
            project, created_at_ms, updated_at_ms, completed_at_ms, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            goal_id,
            title,
            compact_whitespace(str(payload.get("detail") or "")),
            compact_whitespace(str(payload.get("horizon") or "long_term")) or "long_term",
            status,
            _bounded_int(payload.get("priority"), default=1, minimum=0, maximum=3),
            _optional_date(str(payload.get("targetDate") or payload.get("target_date") or "")),
            compact_whitespace(str(payload.get("project") or project)),
            int(previous[0]) if previous is not None else timestamp,
            timestamp,
            completed_at,
            json.dumps(dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), Mapping) else {}, ensure_ascii=False, sort_keys=True),
        ),
    )
    row = conn.execute("SELECT * FROM planning_goals WHERE goal_id = ?", (goal_id,)).fetchone()
    return {"schemaVersion": PLANNING_SCHEMA_VERSION, "ok": True, "goal": _goal_payload(row)}


def save_task(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
    *,
    project: str = "",
) -> dict[str, object]:
    title = compact_whitespace(str(payload.get("title") or ""))
    if not title:
        raise ValueError("task title is required")
    task_id = compact_whitespace(str(payload.get("id") or payload.get("taskId") or "")) or f"task:{uuid.uuid4().hex}"
    status = compact_whitespace(str(payload.get("status") or "todo")).lower()
    if status not in _TASK_STATUSES:
        raise ValueError(f"unsupported task status: {status}")
    day = _validated_date(str(payload.get("date") or payload.get("planDate") or local_date_string()))
    timestamp = now_ms()
    previous = conn.execute(
        "SELECT created_at_ms, completed_at_ms FROM planning_tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    completed_at = timestamp if status == "done" else None
    if status == "done" and previous is not None and previous[1] is not None:
        completed_at = int(previous[1])
    due_value = payload.get("dueAtMs") if payload.get("dueAtMs") is not None else payload.get("due_at_ms")
    due_at_ms = _optional_int(due_value)
    conn.execute(
        """
        INSERT OR REPLACE INTO planning_tasks(
            task_id, plan_date, title, detail, status, priority, due_at_ms,
            project, goal_id, source, confidence, created_at_ms, updated_at_ms,
            completed_at_ms, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            day,
            title,
            compact_whitespace(str(payload.get("detail") or "")),
            status,
            _bounded_int(payload.get("priority"), default=1, minimum=0, maximum=3),
            due_at_ms,
            compact_whitespace(str(payload.get("project") or project)),
            compact_whitespace(str(payload.get("goalId") or "")),
            compact_whitespace(str(payload.get("source") or "manual")) or "manual",
            _bounded_float(payload.get("confidence"), default=1.0),
            int(previous[0]) if previous is not None else timestamp,
            timestamp,
            completed_at,
            json.dumps(dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), Mapping) else {}, ensure_ascii=False, sort_keys=True),
        ),
    )
    row = conn.execute("SELECT * FROM planning_tasks WHERE task_id = ?", (task_id,)).fetchone()
    return {"schemaVersion": PLANNING_SCHEMA_VERSION, "ok": True, "task": _task_payload(row)}


def task_action(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    action: str,
    source_event_id: int | None = None,
    source_text: str = "",
    metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    row = conn.execute("SELECT * FROM planning_tasks WHERE task_id = ?", (compact_whitespace(task_id),)).fetchone()
    if row is None:
        raise ValueError(f"task not found: {task_id}")
    normalized_action = compact_whitespace(action).lower()
    status_by_action = {
        "complete": "done",
        "done": "done",
        "reopen": "todo",
        "undo": "todo",
        "start": "in_progress",
        "cancel": "cancelled",
    }
    next_status = status_by_action.get(normalized_action)
    if next_status is None:
        raise ValueError(f"unsupported task action: {action}")
    previous_status = str(row["status"] or "todo")
    timestamp = now_ms()
    completed_at = timestamp if next_status == "done" else None
    conn.execute(
        "UPDATE planning_tasks SET status = ?, completed_at_ms = ?, updated_at_ms = ? WHERE task_id = ?",
        (next_status, completed_at, timestamp, task_id),
    )
    event_id = f"task-event:{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO planning_task_events(
            event_id, task_id, action, previous_status, next_status,
            source_event_id, source_text_hash, created_at_ms, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            task_id,
            normalized_action,
            previous_status,
            next_status,
            source_event_id,
            stable_text_hash(source_text) if source_text else "",
            timestamp,
            json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True),
        ),
    )
    updated = conn.execute("SELECT * FROM planning_tasks WHERE task_id = ?", (task_id,)).fetchone()
    return {
        "schemaVersion": PLANNING_SCHEMA_VERSION,
        "ok": True,
        "eventId": event_id,
        "task": _task_payload(updated),
        "undoAvailable": previous_status != next_status,
    }


def undo_task_event(conn: sqlite3.Connection, *, event_id: str) -> dict[str, object]:
    row = conn.execute(
        "SELECT * FROM planning_task_events WHERE event_id = ?",
        (compact_whitespace(event_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"task event not found: {event_id}")
    metadata = _json_object(row["metadata_json"])
    if metadata.get("undoneAtMs"):
        raise ValueError("task event already undone")
    timestamp = now_ms()
    conn.execute(
        "UPDATE planning_tasks SET status = ?, completed_at_ms = NULL, updated_at_ms = ? WHERE task_id = ?",
        (str(row["previous_status"] or "todo"), timestamp, str(row["task_id"])),
    )
    metadata["undoneAtMs"] = timestamp
    conn.execute(
        "UPDATE planning_task_events SET metadata_json = ? WHERE event_id = ?",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), event_id),
    )
    task = conn.execute("SELECT * FROM planning_tasks WHERE task_id = ?", (str(row["task_id"]),)).fetchone()
    return {"schemaVersion": PLANNING_SCHEMA_VERSION, "ok": True, "task": _task_payload(task)}


def detect_task_completion(
    conn: sqlite3.Connection,
    *,
    text: str,
    source_event_id: int | None = None,
    project: str = "",
    current_date: str = "",
) -> dict[str, object]:
    value = compact_whitespace(text)
    lowered = value.lower()
    if not value or any(marker in lowered for marker in _NEGATED_COMPLETION_MARKERS):
        return _completion_result("ignored", reason="not_an_explicit_completion")
    if not any(marker in lowered for marker in _COMPLETION_MARKERS):
        return _completion_result("ignored", reason="no_completion_marker")
    if not _completion_detection_enabled(conn):
        return _completion_result("ignored", reason="completion_detection_disabled")
    day = _validated_date(current_date or local_date_string())
    rows = conn.execute(
        """
        SELECT * FROM planning_tasks
        WHERE status IN ('todo', 'in_progress')
          AND plan_date <= ?
          AND (? = '' OR project = ? OR project = '')
        ORDER BY CASE WHEN plan_date = ? THEN 0 ELSE 1 END,
                 priority DESC, updated_at_ms DESC
        LIMIT 80
        """,
        (day, project, project, day),
    ).fetchall()
    scored = sorted(
        ((_task_match_score(value, str(row["title"] or ""), str(row["detail"] or "")), row) for row in rows),
        key=lambda item: item[0],
        reverse=True,
    )
    matches = [(score, row) for score, row in scored if score >= 0.42]
    if not matches:
        return _completion_result("ignored", reason="no_matching_open_task")
    best_score, best = matches[0]
    second_score = matches[1][0] if len(matches) > 1 else 0.0
    if best_score >= 0.78 and best_score - second_score >= 0.16:
        mutation = task_action(
            conn,
            task_id=str(best["task_id"]),
            action="complete",
            source_event_id=source_event_id,
            source_text=value,
            metadata={"detector": "explicit_completion_v1", "matchScore": round(best_score, 4)},
        )
        return {
            **_completion_result("completed", reason="unambiguous_explicit_completion"),
            "task": mutation["task"],
            "taskEventId": mutation["eventId"],
            "matchScore": round(best_score, 4),
            "undoAvailable": True,
        }
    candidate_ids = [str(row["task_id"]) for _, row in matches[:4]]
    suggestion_id = f"task-suggestion:{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO planning_completion_suggestions(
            suggestion_id, source_event_id, source_text_hash,
            candidate_task_ids_json, status, created_at_ms, metadata_json
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            suggestion_id,
            source_event_id,
            stable_text_hash(value),
            json.dumps(candidate_ids, ensure_ascii=False),
            now_ms(),
            json.dumps(
                {"detector": "explicit_completion_v1", "scores": [round(score, 4) for score, _ in matches[:4]]},
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )
    return {
        **_completion_result("confirmation_required", reason="ambiguous_task_match"),
        "suggestionId": suggestion_id,
        "candidateTaskIds": candidate_ids,
    }


def resolve_completion_suggestion(
    conn: sqlite3.Connection,
    *,
    suggestion_id: str,
    task_id: str = "",
    dismiss: bool = False,
) -> dict[str, object]:
    row = conn.execute(
        "SELECT * FROM planning_completion_suggestions WHERE suggestion_id = ? AND status = 'pending'",
        (compact_whitespace(suggestion_id),),
    ).fetchone()
    if row is None:
        raise ValueError("completion suggestion not found or already resolved")
    candidates = _json_list(row["candidate_task_ids_json"])
    timestamp = now_ms()
    if dismiss:
        conn.execute(
            "UPDATE planning_completion_suggestions SET status = 'dismissed', resolved_at_ms = ? WHERE suggestion_id = ?",
            (timestamp, suggestion_id),
        )
        return _completion_result("dismissed", reason="user_dismissed")
    if task_id not in candidates:
        raise ValueError("task is not a candidate for this completion suggestion")
    mutation = task_action(
        conn,
        task_id=task_id,
        action="complete",
        source_event_id=_optional_int(row["source_event_id"]),
        metadata={"detector": "user_confirmed_completion_v1", "suggestionId": suggestion_id},
    )
    conn.execute(
        "UPDATE planning_completion_suggestions SET status = 'resolved', resolved_at_ms = ? WHERE suggestion_id = ?",
        (timestamp, suggestion_id),
    )
    return {
        **_completion_result("completed", reason="user_confirmed"),
        "task": mutation["task"],
        "taskEventId": mutation["eventId"],
        "undoAvailable": True,
    }


def planning_context(
    conn: sqlite3.Connection,
    *,
    project: str = "",
    plan_date: str = "",
    task_limit: int = 20,
    goal_limit: int = 8,
) -> dict[str, object]:
    dashboard = planning_dashboard(conn, plan_date=plan_date, project=project)
    tasks = [
        item
        for item in dashboard["tasks"]
        if isinstance(item, dict) and item.get("status") in {"todo", "in_progress"}
    ][: max(1, int(task_limit))]
    goals = [item for item in dashboard["goals"] if isinstance(item, dict)][: max(1, int(goal_limit))]
    plan = dashboard["plan"] if isinstance(dashboard.get("plan"), dict) else {}
    rendered_parts = [
        compact_whitespace(str(plan.get("intention") or "")),
        *(compact_whitespace(str(item.get("title") or "")) for item in tasks),
        *(compact_whitespace(str(item.get("title") or "")) for item in goals),
    ]
    rendered = "\n".join(item for item in rendered_parts if item)
    return {
        "schemaVersion": PLANNING_CONTEXT_SCHEMA_VERSION,
        "date": dashboard["date"],
        "intention": compact_whitespace(str(plan.get("intention") or "")),
        "notes": truncate_text(compact_whitespace(str(plan.get("notes") or "")), 600),
        "openTasks": tasks,
        "longTermGoals": goals,
        "counts": {
            "taskCount": len(tasks),
            "goalCount": len(goals),
            "completedToday": int(dict(dashboard["summary"]).get("completedTaskCount") or 0),
        },
        "estimatedTokens": estimate_tokens(rendered),
    }


def planning_assistant_reply(
    conn: sqlite3.Connection,
    *,
    message: str,
    project: str = "",
    plan_date: str = "",
) -> dict[str, object]:
    content = compact_whitespace(message)
    if not content:
        raise ValueError("assistant message is required")
    day = _validated_date(plan_date or local_date_string())
    dashboard = planning_dashboard(conn, plan_date=day, project=project)
    tasks = [item for item in dashboard.get("tasks", []) if isinstance(item, dict)]
    goals = [item for item in dashboard.get("goals", []) if isinstance(item, dict)]
    open_tasks = [item for item in tasks if item.get("status") in {"todo", "in_progress"}]
    completed = [item for item in tasks if item.get("status") == "done"]
    lowered = content.lower()
    if any(marker in lowered for marker in ("还有什么", "剩下", "待办", "任务", "今天做什么")):
        if open_tasks:
            titles = "、".join(f"“{item.get('title', '')}”" for item in open_tasks[:5])
            reply = f"今天还剩 {len(open_tasks)} 项：{titles}。先推进优先级最高的一项。"
        else:
            reply = "今天没有未完成任务。可以写一句复盘，或者从长期目标拆一项明天的任务。"
    elif any(marker in lowered for marker in ("进度", "完成", "做得怎么样", "总结", "复盘")):
        reply = (
            f"今天已完成 {len(completed)} 项，仍有 {len(open_tasks)} 项。"
            + (f" 下一项建议是“{open_tasks[0].get('title', '')}”。" if open_tasks else " 今天的计划已经收束。")
        )
    elif any(marker in lowered for marker in ("目标", "长期", "方向")):
        reply = (
            "当前长期目标是：" + "、".join(f"“{item.get('title', '')}”" for item in goals[:5]) + "。"
            if goals
            else "还没有长期目标。先写一个能持续数周、并能拆成每日任务的方向。"
        )
    else:
        reply = _assistant_summary(day=day, tasks=tasks, goals=goals)
    timestamp = now_ms()
    for role, text in (("user", content), ("assistant", reply)):
        conn.execute(
            """
            INSERT INTO planning_assistant_messages(
                message_id, plan_date, project, role, content, created_at_ms, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"planning-message:{uuid.uuid4().hex}",
                day,
                compact_whitespace(project),
                role,
                text,
                timestamp if role == "user" else timestamp + 1,
                json.dumps({"mode": "local_planning_assistant_v1"}, ensure_ascii=False, sort_keys=True),
            ),
        )
    return {
        "schemaVersion": "rag-ime.planning-assistant.v1",
        "ok": True,
        "date": day,
        "reply": reply,
        "dashboard": planning_dashboard(conn, plan_date=day, project=project),
        "modelUsed": False,
    }


def planning_evidence_pack(
    conn: sqlite3.Connection,
    *,
    project: str = "",
    plan_date: str = "",
    max_items: int = 8,
) -> tuple[dict[str, object], ...]:
    context = planning_context(conn, project=project, plan_date=plan_date)
    items: list[dict[str, object]] = []
    intention = compact_whitespace(str(context.get("intention") or ""))
    if intention:
        items.append(
            {
                "sourceType": "daily_plan",
                "sourceLane": "planning_today",
                "title": "今日计划",
                "summary": intention,
                "evidencePreview": intention,
                "surfaceHints": [],
                "tags": ["planning", "today"],
                "metadata": {"contextOnly": True, "planDate": context["date"]},
            }
        )
    for task in context.get("openTasks", []):
        if not isinstance(task, dict):
            continue
        title = compact_whitespace(str(task.get("title") or ""))
        if not title:
            continue
        items.append(
            {
                "sourceType": "todo",
                "sourceLane": "planning_open_task",
                "title": title,
                "summary": compact_whitespace(str(task.get("detail") or title)),
                "evidencePreview": title,
                "surfaceHints": [],
                "tags": ["todo", "planning"],
                "metadata": {
                    "contextOnly": True,
                    "taskId": task.get("id"),
                    "priority": task.get("priority"),
                    "status": task.get("status"),
                },
            }
        )
        if len(items) >= max(1, int(max_items)):
            break
    return tuple(items[: max(1, int(max_items))])


def estimate_tokens(text: str) -> int:
    value = compact_whitespace(text)
    if not value:
        return 0
    cjk = len(_CJK_RE.findall(value))
    ascii_words = len(re.findall(r"[A-Za-z0-9_+#.-]+", value))
    punctuation = len(re.findall(r"[^\w\s\u3400-\u9fff]", value))
    return max(1, cjk + ascii_words + (punctuation + 1) // 2)


def _assistant_summary(*, day: str, tasks: Sequence[dict[str, object]], goals: Sequence[dict[str, object]]) -> str:
    completed = [item for item in tasks if item.get("status") == "done"]
    open_tasks = [item for item in tasks if item.get("status") in {"todo", "in_progress"}]
    if tasks and not open_tasks:
        return f"今天的 {len(tasks)} 项任务都完成了。可以写一句复盘，给明天留下清楚的起点。"
    if completed and open_tasks:
        next_title = str(open_tasks[0].get("title") or "下一项任务")
        return f"今天已完成 {len(completed)} 项，还剩 {len(open_tasks)} 项。接下来先推进“{next_title}”。"
    if open_tasks:
        next_title = str(open_tasks[0].get("title") or "第一项任务")
        return f"今天有 {len(open_tasks)} 项待办。先从“{next_title}”开始，完成后我会帮你更新进度。"
    if goals:
        return f"今天还没有任务。可以从长期目标“{goals[0].get('title', '')}”拆一项今天能完成的小任务。"
    return f"{day} 还没有规划。先写下今天最重要的一件事。"


def _task_match_score(source: str, title: str, detail: str) -> float:
    source_norm = normalize_text(source)
    title_norm = normalize_text(title)
    detail_norm = normalize_text(detail)
    if not source_norm or not title_norm:
        return 0.0
    if title_norm in source_norm:
        return 1.0
    source_terms = set(token_terms(source, max_terms=64)) | _cjk_ngrams(source_norm)
    title_terms = set(token_terms(title, max_terms=32)) | _cjk_ngrams(title_norm)
    if not title_terms:
        return 0.0
    overlap = len(source_terms & title_terms) / max(1, len(title_terms))
    detail_bonus = 0.0
    if detail_norm:
        detail_terms = set(token_terms(detail, max_terms=32)) | _cjk_ngrams(detail_norm)
        detail_bonus = min(0.15, len(source_terms & detail_terms) / max(1, len(detail_terms)) * 0.15)
    return min(1.0, overlap * 0.9 + detail_bonus)


def _cjk_ngrams(text: str) -> set[str]:
    chars = "".join(_CJK_RE.findall(text))
    if not chars:
        return set()
    if len(chars) <= 2:
        return {chars}
    return {chars[index : index + 2] for index in range(len(chars) - 1)}


def _daily_payload(row: sqlite3.Row | None, *, day: str, project: str) -> dict[str, object]:
    if row is None:
        return {
            "id": "",
            "date": day,
            "project": project,
            "intention": "",
            "notes": "",
            "reflection": "",
            "assistantSummary": "",
            "createdAtMs": 0,
            "updatedAtMs": 0,
            "metadata": {},
        }
    return {
        "id": str(row["plan_id"]),
        "date": str(row["plan_date"]),
        "project": str(row["project"] or ""),
        "intention": str(row["intention"] or ""),
        "notes": str(row["notes"] or ""),
        "reflection": str(row["reflection"] or ""),
        "assistantSummary": str(row["assistant_summary"] or ""),
        "createdAtMs": int(row["created_at_ms"] or 0),
        "updatedAtMs": int(row["updated_at_ms"] or 0),
        "metadata": _json_object(row["metadata_json"]),
    }


def _task_payload(row: sqlite3.Row | None) -> dict[str, object]:
    if row is None:
        return {}
    return {
        "id": str(row["task_id"]),
        "date": str(row["plan_date"]),
        "title": str(row["title"]),
        "detail": str(row["detail"] or ""),
        "status": str(row["status"]),
        "priority": int(row["priority"] or 0),
        "dueAtMs": int(row["due_at_ms"]) if row["due_at_ms"] is not None else None,
        "project": str(row["project"] or ""),
        "goalId": str(row["goal_id"] or ""),
        "source": str(row["source"] or "manual"),
        "confidence": float(row["confidence"] or 0.0),
        "createdAtMs": int(row["created_at_ms"] or 0),
        "updatedAtMs": int(row["updated_at_ms"] or 0),
        "completedAtMs": int(row["completed_at_ms"]) if row["completed_at_ms"] is not None else None,
        "metadata": _json_object(row["metadata_json"]),
    }


def _goal_payload(row: sqlite3.Row | None) -> dict[str, object]:
    if row is None:
        return {}
    return {
        "id": str(row["goal_id"]),
        "title": str(row["title"]),
        "detail": str(row["detail"] or ""),
        "horizon": str(row["horizon"] or "long_term"),
        "status": str(row["status"]),
        "priority": int(row["priority"] or 0),
        "targetDate": str(row["target_date"] or ""),
        "project": str(row["project"] or ""),
        "createdAtMs": int(row["created_at_ms"] or 0),
        "updatedAtMs": int(row["updated_at_ms"] or 0),
        "completedAtMs": int(row["completed_at_ms"]) if row["completed_at_ms"] is not None else None,
        "metadata": _json_object(row["metadata_json"]),
    }


def _suggestion_payload(row: sqlite3.Row, *, tasks: Sequence[dict[str, object]]) -> dict[str, object]:
    candidates = set(_json_list(row["candidate_task_ids_json"]))
    return {
        "id": str(row["suggestion_id"]),
        "sourceEventId": int(row["source_event_id"]) if row["source_event_id"] is not None else None,
        "candidateTasks": [item for item in tasks if str(item.get("id") or "") in candidates],
        "createdAtMs": int(row["created_at_ms"] or 0),
        "metadata": _json_object(row["metadata_json"]),
    }


def _recent_detected_completion(
    conn: sqlite3.Connection,
    *,
    day: str,
    project: str,
) -> dict[str, object] | None:
    rows = conn.execute(
        """
        SELECT e.event_id, e.task_id, e.source_event_id, e.created_at_ms,
               e.metadata_json
        FROM planning_task_events e
        JOIN planning_tasks t ON t.task_id = e.task_id
        WHERE e.action = 'complete'
          AND e.source_event_id IS NOT NULL
          AND t.status = 'done'
          AND t.plan_date = ?
          AND (? = '' OR t.project = ? OR t.project = '')
        ORDER BY e.created_at_ms DESC
        LIMIT 20
        """,
        (day, project, project),
    ).fetchall()
    for row in rows:
        metadata = _json_object(row["metadata_json"])
        if metadata.get("undoneAtMs"):
            continue
        task_row = conn.execute(
            "SELECT * FROM planning_tasks WHERE task_id = ?",
            (str(row["task_id"]),),
        ).fetchone()
        task = _task_payload(task_row)
        if not task:
            continue
        return {
            "eventId": str(row["event_id"]),
            "sourceEventId": int(row["source_event_id"]),
            "createdAtMs": int(row["created_at_ms"] or 0),
            "task": task,
            "message": f"已根据输入将“{task['title']}”标为完成",
            "undoAvailable": True,
        }
    return None


def _completion_result(status: str, *, reason: str) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.planning-completion-detection.v1",
        "status": status,
        "reason": reason,
    }


def _validated_date(value: str) -> str:
    compact = compact_whitespace(value)
    try:
        return date.fromisoformat(compact).isoformat()
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD") from exc


def _optional_date(value: str) -> str:
    compact = compact_whitespace(value)
    return _validated_date(compact) if compact else ""


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _bounded_float(value: object, *, default: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _json_object(raw: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_list(raw: object) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [compact_whitespace(str(item)) for item in parsed if compact_whitespace(str(item))]


def _completion_detection_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT value_json FROM management_settings WHERE key = 'planning' LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return True
    if row is None:
        return True
    settings = _json_object(row[0])
    return bool(settings.get("enabled", True)) and bool(settings.get("detectExplicitCompletion", True))
