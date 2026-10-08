from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from typing import Any

from .agent_command_receipts import AgentCommandReceiptFailed
from .agent_protocol import AgentEventEnvelope
from .agent_wake_scheduler import AgentWakeDispatchUncertain


class AgentWakeApplicationService:
    """Own durable wake schedule validation and dispatch."""

    def __init__(
        self,
        *,
        schedules: Any,
        sessions: Any,
        personas: Any,
        rooms: Any,
        context_runtime: Any,
        events: Any,
        create_session: Callable[[Mapping[str, object]], Mapping[str, object]],
        prompt: Callable[[str, Mapping[str, object]], Mapping[str, object]],
        guard_room_session_route: Callable[[str, str], None],
        context_source_token: object,
        command_receipts: Any = None,
    ) -> None:
        self.schedules = schedules
        self.sessions = sessions
        self.personas = personas
        self.rooms = rooms
        self.context_runtime = context_runtime
        self.events = events
        self.create_session = create_session
        self.prompt = prompt
        self.guard_room_session_route = guard_room_session_route
        self.context_source_token = context_source_token
        self.command_receipts = command_receipts
        self.scheduler: Any = None

    def bind_scheduler(self, scheduler: Any) -> None:
        self.scheduler = scheduler

    def preview(
        self,
        payload: Mapping[str, object],
        *,
        requested_by_session_id: str = "",
    ) -> dict[str, object]:
        candidate = dict(payload)
        if (
            str(
                candidate.get("targetType") or "session"
            ).strip().lower()
            == "session"
            and not str(
                candidate.get("targetSessionId") or ""
            ).strip()
            and requested_by_session_id
        ):
            candidate["targetType"] = "session"
            candidate["targetSessionId"] = (
                requested_by_session_id
            )
        normalized = self.validate(candidate)
        return {
            "schemaVersion": (
                "rag-ime.agent-wake-schedule-preview.v1"
            ),
            "ok": True,
            "requestedBySessionId": str(
                requested_by_session_id or ""
            ),
            "schedule": normalized,
        }

    def list(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        value = dict(payload or {})
        items = self.schedules.list(
            status=str(value.get("status") or ""),
            target_type=str(value.get("targetType") or ""),
            target_id=str(value.get("targetId") or ""),
            created_by_session_id=str(
                value.get("createdBySessionId") or ""
            ),
            limit=_integer(
                value.get("limit"),
                default=100,
                minimum=1,
                maximum=500,
            ),
        )
        return {
            "schemaVersion": (
                "rag-ime.agent-wake-schedule-list.v1"
            ),
            "ok": True,
            "schedulerActive": bool(
                self.scheduler and self.scheduler.active
            ),
            "items": items,
        }

    def get(self, schedule_id: str) -> dict[str, object]:
        return self.schedules.get(schedule_id)

    def create(
        self,
        payload: Mapping[str, object],
        *,
        created_by_session_id: str = "",
        require_confirmation: bool = True,
    ) -> dict[str, object]:
        if (
            require_confirmation
            and str(payload.get("confirmText") or "").strip()
            != "schedule"
        ):
            raise ValueError(
                "wake schedule creation requires confirmText=schedule"
            )
        schedule = self.schedules.create(
            self.validate(payload),
            created_by_session_id=created_by_session_id,
        )
        self._wake_scheduler()
        return {
            "schemaVersion": (
                "rag-ime.agent-wake-schedule-create.v1"
            ),
            "ok": True,
            "schedule": schedule,
        }

    def runs(
        self,
        schedule_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        value = dict(payload or {})
        return {
            "schemaVersion": "rag-ime.agent-wake-run-list.v1",
            "ok": True,
            "schedule": self.schedules.get(schedule_id),
            "items": self.schedules.runs(
                schedule_id,
                limit=_integer(
                    value.get("limit"),
                    default=100,
                    minimum=1,
                    maximum=500,
                ),
            ),
        }

    def action(
        self,
        schedule_id: str,
        payload: Mapping[str, object],
        *,
        require_confirmation: bool = True,
    ) -> dict[str, object]:
        if (
            require_confirmation
            and str(payload.get("confirmText") or "").strip()
            != "apply"
        ):
            raise ValueError(
                "wake schedule changes require confirmText=apply"
            )
        if str(payload.get("action") or "") == "edit":
            if self.schedules.get(schedule_id).get("metadata", {}).get("kind") == "room_partner_completion":
                raise ValueError("Room partner wakes are managed by the Room workflow")
            definition = payload.get("schedule")
            if not isinstance(definition, Mapping):
                raise ValueError("editing a wake requires its schedule definition")
            schedule = self.schedules.update(schedule_id, self.validate(definition))
        else:
            schedule = self.schedules.action(
                schedule_id,
                str(payload.get("action") or ""),
            )
        self._wake_scheduler()
        return {
            "schemaVersion": (
                "rag-ime.agent-wake-schedule-action.v1"
            ),
            "ok": True,
            "schedule": schedule,
        }

    def validate(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        normalized = self.schedules.validate_create(payload)
        if normalized["targetType"] == "session":
            session = self.sessions.get(
                str(normalized["targetSessionId"])
            )
            if (
                str(
                    session.get("sessionKind")
                    or "conversation"
                )
                != "conversation"
            ):
                raise ValueError(
                    "only a conversation thread can be scheduled"
                )
            if str(session.get("status") or "") == "archived":
                raise ValueError(
                    "archived Agent threads cannot be scheduled"
                )
            if self.rooms.participant_for_session(
                str(session["id"]),
                active_only=False,
            ) is not None:
                raise ValueError(
                    "Room participant threads must be woken through the Room workflow"
                )
            normalized["targetDisplayName"] = str(
                session.get("title") or "Agent thread"
            )
        else:
            role = self.personas.resolve_active(
                normalized["targetRoleId"],
                normalized["targetRoleVersion"],
            )
            if "assistant" not in role.selectable_modes:
                raise ValueError(
                    "scheduled role wakes require an assistant-capable Persona"
                )
            normalized["targetDisplayName"] = role.display_name
        return normalized

    def dispatch(self, claim: Mapping[str, object]) -> None:
        run_id = str(claim.get("runId") or "")
        if self.schedules.dispatch_binding(run_id)["session_id"]:
            return  # Never repeat a bound original admission or create another Role target.
        target_type = str(claim.get("targetType") or "")
        session = self._dispatch_target(
            target_type=target_type,
            claim=claim,
            run_id=run_id,
        )
        if session is None:
            return
        session_id = str(session["id"])
        self.guard_room_session_route(
            "wake.dispatch",
            session_id,
        )
        title = str(claim.get("title") or "未命名任务")
        self._enqueue_context(
            session_id=session_id,
            run_id=run_id,
            title=title,
            claim=claim,
        )
        self.schedules.bind_target(run_id, session_id=session_id)
        try:
            accepted = self.prompt(
                session_id,
                {
                    "message": f"预约任务已到期：{title}",
                    "clientMessageId": run_id,
                    "_contextSource": "schedule",
                    "_contextSourceToken": (
                        self.context_source_token
                    ),
                },
            )
        except Exception as exc:
            if (
                target_type == "session"
                and isinstance(exc, AgentCommandReceiptFailed)
                and exc.cause_code in {"SESSION_BUSY", "AGENT_TURN_CONFLICT"}
                and self.sessions.get(session_id).get("status")
                == "busy"
            ):
                self.schedules.defer(
                    run_id,
                    reason=(
                        "目标线程刚刚开始其他回合，已顺延一分钟"
                    ),
                    delay_ms=60_000,
                )
                return
            if isinstance(exc, AgentCommandReceiptFailed) and exc.cause_code:
                raise
            # Prompt was invoked: local/untyped failure is not rejection proof.
            raise AgentWakeDispatchUncertain(str(exc)) from exc
        try:
            if accepted.get("admissionCancelled") is True:
                self.schedules.fail_dispatch(run_id, error="Original prompt admission was cancelled")
                return
            turn_id = str(accepted.get("turnId") or "")
            self.schedules.accept(
                run_id,
                session_id=session_id,
                turn_id=turn_id,
            )
            replayed, _gap = self.events.replay(session_id)
            for event in replayed:
                if (
                    event.turn_id == turn_id
                    and event.event_type
                    in {"turn_completed", "turn_failed"}
                ):
                    if self.scheduler is not None:
                        self.scheduler.observe_event(event)
                    break
        except Exception as exc:
            raise AgentWakeDispatchUncertain(str(exc)) from exc

    def reconcile_once(self, *, limit: int = 100) -> int:
        """Project exact durable bindings without prompting or opening Pi."""
        count = 0
        for run in self.schedules.active_bound_runs(limit=limit):
            metadata = json.loads(str(run["metadata_json"] or "{}"))
            if metadata.get("kind") == "room_partner_completion":
                continue  # Room retains its own admission identity.
            run_id = str(run["run_id"])
            session_id = str(run["session_id"])
            turn_id = str(run["turn_id"] or "")
            changed = False
            try:
                if run["state"] == "claimed":
                    acceptance = None
                    if self.command_receipts is not None:
                        acceptance = self.command_receipts.acceptance_evidence_for_exact_command(
                            command_scope="session_prompt", scope_id=session_id,
                            client_message_id=run_id,
                        )
                    acceptance = acceptance or self.sessions.prompt_acceptance_evidence(session_id, run_id)
                    if acceptance and str(acceptance.get("clientMessageId") or "") == run_id:
                        turn_id = str(acceptance.get("turnId") or "")
                        if not turn_id:
                            continue
                        self.schedules.accept(run_id, session_id=session_id, turn_id=turn_id)
                        count += 1
                        changed = True
                    else:
                        failure = self.command_receipts.failure_evidence_for_exact_command(
                            command_scope="session_prompt", scope_id=session_id,
                            client_message_id=run_id,
                        ) if self.command_receipts is not None else None
                        if failure and failure.get("causeCode"):
                            self.schedules.fail_dispatch(run_id, error=str(failure.get("message") or "Original prompt rejected"))
                            count += 1
                        continue
                terminal = self.sessions.runtime_turn_terminal_event(session_id, turn_id)
                if not terminal or terminal.get("sessionId") != session_id or terminal.get("turnId") != turn_id:
                    continue
                event_type = str(terminal.get("eventType") or "")
                if event_type not in {"turn_completed", "turn_failed"}:
                    continue
                event = AgentEventEnvelope(
                    event_id=str(terminal["eventId"]), session_id=session_id, turn_id=turn_id,
                    sequence=int(terminal["sequence"]), created_at_ms=int(terminal["createdAtMs"]),
                    event_type=event_type, payload={"error": str(terminal.get("status") or "")},
                    resume_token=str(terminal["sequence"]),
                )
                if self.scheduler is not None:
                    finished = self.scheduler.observe_event(event)
                else:
                    finished = self.schedules.finish_event(event)
                if finished and not changed:
                    count += 1
            except ValueError:
                # Transactions fence concurrent cancellation or incompatible binding.
                continue
        return count

    def _dispatch_target(
        self,
        *,
        target_type: str,
        claim: Mapping[str, object],
        run_id: str,
    ) -> Mapping[str, object] | None:
        if target_type == "session":
            session = self.sessions.get(
                str(claim.get("targetSessionId") or "")
            )
            if str(session.get("status") or "") == "busy":
                self.schedules.defer(
                    run_id,
                    reason=(
                        "目标线程仍在执行上一回合，已顺延一分钟"
                    ),
                    delay_ms=60_000,
                )
                return None
            if str(session.get("status") or "") == "archived":
                raise ValueError(
                    "scheduled Agent thread is archived"
                )
            if session.get("evaluationSnapshot") is True:
                raise ValueError("evaluation snapshot is read-only")
            return session
        if target_type == "role":
            created = self.create_session(
                {
                    "title": (
                        "预约 · "
                        f"{str(claim.get('title') or 'Agent 任务')}"
                    ),
                    "mode": "assistant",
                    "roleId": str(
                        claim.get("targetRoleId") or ""
                    ),
                    "roleVersion": str(
                        claim.get("targetRoleVersion") or "1"
                    ),
                }
            )
            return dict(created["session"])
        raise ValueError("scheduled wake target is invalid")

    def _enqueue_context(
        self,
        *,
        session_id: str,
        run_id: str,
        title: str,
        claim: Mapping[str, object],
    ) -> None:
        instruction = str(claim.get("instruction") or "")
        planning_task_id = str(
            claim.get("planningTaskId") or ""
        )
        planning_context = (
            f"关联规划任务 ID：{planning_task_id}。"
            "如果任务已经完成，可以通过 planning "
            "提出状态更新，但仍需用户批准。"
            if planning_task_id
            else ""
        )
        self.context_runtime.enqueue(
            session_id=session_id,
            source_kind="wake_schedule",
            source_id=run_id,
            lane="schedule",
            lifecycle="turn",
            dedupe_key=f"wake:{run_id}",
            title=f"预约到期：{title}",
            summary="受管日程已唤醒当前 Agent 线程",
            payload={
                "instruction": instruction,
                "planningTaskId": planning_task_id,
                "planningContext": planning_context,
                "policy": (
                    "开始执行并说明完成结果、未完成原因或"
                    "需要批准的下一步；任何写入和外部操作"
                    "仍遵守当前 Session 的工具与审批边界。"
                ),
            },
        )

    def enqueue_room_completion(
        self,
        *,
        claim: Mapping[str, object],
        dispatch: Mapping[str, object],
    ) -> None:
        """Put an internal Partner completion in the existing context inbox."""

        run_id = str(claim.get("runId") or "")
        child_dispatch_id = str(dispatch.get("childDispatchId") or "")
        work_item_id = str(dispatch.get("workItemId") or "")
        self.context_runtime.enqueue(
            session_id=str(dispatch.get("sourceSessionId") or ""),
            source_kind="room_partner_completion",
            source_id=run_id,
            lane="room",
            lifecycle="turn",
            dedupe_key=f"room-partner-wake:{run_id}",
            title="伙伴交付待验收",
            summary="伙伴执行已结束，等待 Facilitator 双轴验收",
            payload={
                "roomId": str(dispatch.get("roomId") or ""),
                "rootId": str(dispatch.get("rootId") or ""),
                "childDispatchId": child_dispatch_id,
                "workItemId": work_item_id,
                "dispatchStatus": str(dispatch.get("status") or ""),
                "instruction": (
                    "先调用 room_partner collect 读取交付、WorkItem、"
                    "WorkDocument 与证据。分别判断运行可操作性和需求满足度；"
                    "两轴均通过时用非空 reason 显式 accept，否则显式 return 并写明原因。"
                    "审查报告 unverified、changes_required、failed 或未解决 HIGH/MEDIUM "
                    "时必须 return，不得写成 passed/satisfied。"
                ),
            },
        )

    def _wake_scheduler(self) -> None:
        if self.scheduler is not None:
            self.scheduler.wake()


def _integer(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
