from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from typing import Any, Callable

from rag_ime.rooms.prompt_context import agent_message_text
from rag_ime.rooms.turn_registry import RoomSessionBusyError
from rag_ime.contracts.json_schema import validate_contract


_MAX_AUTONOMOUS_COMPLETION_WAKE_GENERATION = 2
_MAX_LEGACY_PREACCEPTANCE_WAKE_GENERATION = 3
_MAX_LEGACY_STALE_WAKE_GENERATION = 3
_MAX_MISSING_TYPED_RESULT_WAKE_GENERATION = 4
_LEGACY_STALE_WAKE_ERROR = "Stale Room Partner completion wake"
_MISSING_TYPED_RESULT_ERROR = (
    "Facilitator turn completed without room_partner post(kind=result)"
)
_RECOVERABLE_RUNTIME_HOST_FAILURE_MARKERS = (
    "invalid pi runtime host jsonl",
    "pi runtime host stdout ended",
    "pi runtime host exited",
    "runtime_host_exit",
    "partial protocol record",
)
_ROOM_CELESTIAL_NAMES = (
    "Earth",
    "Mars",
    "Venus",
    "Jupiter",
    "Saturn",
    "Mercury",
    "Neptune",
    "Uranus",
)


def _room_celestial_name(
    participant: Mapping[str, object],
    *,
    fallback_ordinal: int,
) -> str:
    raw_ordinal = participant.get("ordinal")
    ordinal = (
        raw_ordinal
        if isinstance(raw_ordinal, int) and not isinstance(raw_ordinal, bool)
        else fallback_ordinal
    )
    if 0 <= ordinal < len(_ROOM_CELESTIAL_NAMES):
        return _ROOM_CELESTIAL_NAMES[ordinal]
    return f"Planet {ordinal + 1}"


class _RootWorkPending(ValueError):
    """The original Root has work which is not explicitly accepted yet."""


class RoomPartnerApplicationService:
    """A thin Partner adapter over ordinary Pi Sessions.

    Room owns only routing, public events and the parent/child relationship.
    Pi continues to own each Session turn, Tool loop, Steer and cancellation.
    """

    def __init__(
        self,
        *,
        rooms: Any,
        room_turns: Any,
        runtime_status: Callable[[], Mapping[str, object]],
        sessions: Any,
        room_events: Any,
        room_target_idle: Callable[..., bool],
        begin_room_turn: Callable[..., None],
        room_dispatch: Any,
        cancel_room_turn: Callable[[str, str], None],
        abort_session: Callable[[str], Mapping[str, object]],
        room_topic_for_turn: Callable[[str], str],
        send_room_intercom: Callable[[str, Mapping[str, object]], Mapping[str, object]] | None = None,
        list_room_intercom: Callable[[str], list[Mapping[str, object]]] | None = None,
        room_work: Any | None = None,
        publish_room_work_activity: Callable[..., None] | None = None,
        work_document_for_authority: Callable[[str, str], Mapping[str, object] | None] | None = None,
        dispatch_store: Any | None = None,
        wake_schedules: Any | None = None,
        notify_wake_scheduler: Callable[[], None] | None = None,
        dispatch_facilitator_wake: Callable[
            [Mapping[str, object], Mapping[str, object]], bool
        ]
        | None = None,
        command_acceptance_evidence: Callable[
            [str, str], Mapping[str, object] | None
        ]
        | None = None,
        command_failure_evidence: Callable[
            [str, str], Mapping[str, object] | None
        ]
        | None = None,
        accept_room_work: Callable[
            [str, Mapping[str, object]], Mapping[str, object]
        ]
        | None = None,
        return_room_work: Callable[
            [str, Mapping[str, object]], Mapping[str, object]
        ]
        | None = None,
        add_room_participant: Callable[
            [str, Mapping[str, object]], Mapping[str, object]
        ]
        | None = None,
        remove_room_participant: Callable[
            [str, Mapping[str, object]], Mapping[str, object]
        ]
        | None = None,
        recover_faulted_session: Callable[[str], None] | None = None,
        pending_removal: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.rooms = rooms
        self.room_turns = room_turns
        self.runtime_status = runtime_status
        self.sessions = sessions
        self.room_events = room_events
        self.room_target_idle = room_target_idle
        self.begin_room_turn = begin_room_turn
        self.room_dispatch = room_dispatch
        self.cancel_room_turn = cancel_room_turn
        self.abort_session = abort_session
        self.room_topic_for_turn = room_topic_for_turn
        self.send_room_intercom = send_room_intercom
        self.list_room_intercom = list_room_intercom
        self.room_work = room_work
        self.publish_room_work_activity = publish_room_work_activity
        self.work_document_for_authority = work_document_for_authority
        self.dispatch_store = dispatch_store
        self.wake_schedules = wake_schedules
        self.notify_wake_scheduler = notify_wake_scheduler
        self.dispatch_facilitator_wake = dispatch_facilitator_wake
        self.command_acceptance_evidence = command_acceptance_evidence
        self.command_failure_evidence = command_failure_evidence
        self.accept_room_work = accept_room_work
        self.return_room_work = return_room_work
        self.add_room_participant = add_room_participant
        self.remove_room_participant = remove_room_participant
        self.recover_faulted_session = recover_faulted_session
        self.pending_removal = pending_removal or (lambda _room_id, _participant_id: False)

    def execute(
        self,
        session_id: str,
        args: Mapping[str, object],
        *,
        tool_call_id: str,
        source_loop_id: str = "",
    ) -> dict[str, object]:
        source = self._participant(session_id)
        operation = str(args.get("op") or "list").strip()
        if operation == "list":
            return self._list(source)
        if operation == "add_participant":
            return self._add_participant(source, args)
        if operation == "remove_participant":
            return self._remove_participant(source, args)
        if operation == "delegate":
            return self._delegate(
                source,
                args,
                tool_call_id=tool_call_id,
            )
        if operation == "delegate_batch":
            return self._delegate_batch(
                source,
                args,
                tool_call_id=tool_call_id,
            )
        if operation == "retry":
            return self._retry(
                source,
                args,
                tool_call_id=tool_call_id,
            )
        if operation == "post":
            return self._post(
                source,
                args,
                tool_call_id=tool_call_id,
                source_loop_id=source_loop_id,
            )
        if operation == "collect":
            return self._collect(source, args, wait=False)
        if operation == "wait":
            return self._collect(source, args, wait=True)
        if operation == "accept":
            return self._review(source, args, accept=True)
        if operation == "return":
            return self._review(source, args, accept=False)
        if operation == "peer_list":
            return self._peer_list(source)
        if operation in {"peer_send", "peer_ask", "peer_reply"}:
            return self._peer_message(
                source,
                args,
                operation=operation,
                tool_call_id=tool_call_id,
            )
        raise ValueError(
            "room_partner op must be list, delegate, delegate_batch, retry, post, "
            "collect, wait, accept, return, peer_list, peer_send, peer_ask, "
            "peer_reply, add_participant, or remove_participant"
        )

    def _require_facilitator_root(
        self,
        source: Mapping[str, object],
    ) -> tuple[str, str]:
        if str(source.get("collaborationRole") or "") != "coordinator":
            raise ValueError(
                "only the Room Facilitator can change Room participants"
            )
        return self._active_root(source)

    def _add_participant(
        self,
        source: Mapping[str, object],
        args: Mapping[str, object],
    ) -> dict[str, object]:
        root_id, dispatch_id = self._require_facilitator_root(source)
        if self.add_room_participant is None:
            raise ValueError("Room participant add is unavailable")
        payload: dict[str, object] = {
            "roleId": _required_text(args, "roleId", maximum=320),
            "collaborationRole": _text(args.get("collaborationRole"), maximum=40)
            or "implementer",
        }
        role_version = _text(args.get("roleVersion"), maximum=40)
        if role_version:
            payload["roleVersion"] = role_version
        result = self.add_room_participant(str(source["roomId"]), payload)
        return {
            **dict(result),
            "schemaVersion": "rag-ime.room-partner-result.v1",
            "operation": "add_participant",
            "roomId": str(source["roomId"]),
            "rootId": root_id,
            "dispatchId": dispatch_id,
        }

    def _remove_participant(
        self,
        source: Mapping[str, object],
        args: Mapping[str, object],
    ) -> dict[str, object]:
        root_id, dispatch_id = self._require_facilitator_root(source)
        if self.remove_room_participant is None:
            raise ValueError("Room participant removal is unavailable")
        participant_id = _required_text(args, "participantId", maximum=320)
        if participant_id == str(source.get("id") or ""):
            raise ValueError("the Room Facilitator cannot remove itself")
        result = self.remove_room_participant(
            str(source["roomId"]),
            {
                "participantId": participant_id,
                "actorParticipantId": str(source["id"]),
                "reason": _text(args.get("reason"), maximum=500),
            },
        )
        return {
            **dict(result),
            "schemaVersion": "rag-ime.room-partner-result.v1",
            "operation": "remove_participant",
            "roomId": str(source["roomId"]),
            "rootId": root_id,
            "dispatchId": dispatch_id,
        }

    def _collect(
        self,
        source: Mapping[str, object],
        args: Mapping[str, object],
        *,
        wait: bool,
    ) -> dict[str, object]:
        if self.dispatch_store is None:
            raise ValueError("Room Partner dispatch ledger is unavailable")
        record = self._dispatch_record(args)
        self._require_dispatch_owner(source, record)
        timed_out = False
        if wait:
            timeout_seconds = _integer(
                args.get("timeoutSeconds"),
                default=30,
                minimum=1,
                maximum=300,
            )
            record = self.dispatch_store.wait(
                str(record["childDispatchId"]),
                timeout_seconds=timeout_seconds,
            )
            timed_out = str(record.get("status") or "") in {
                "prepared",
                "dispatched",
            }
        work_item: Mapping[str, object] | None = None
        if self.room_work is not None and record.get("workItemId"):
            work_item = self.room_work.get(
                str(record["workItemId"]),
                room_id=str(record["roomId"]),
            )
        return {
            "schemaVersion": "rag-ime.room-partner-result.v1",
            "operation": "wait" if wait else "collect",
            "roomId": str(record["roomId"]),
            "rootId": str(record["rootId"]),
            "childDispatchId": str(record["childDispatchId"]),
            "workItemId": str(record["workItemId"]),
            "status": str(record["status"]),
            "timedOut": timed_out,
            "dispatch": dict(record),
            **({"workItem": dict(work_item)} if work_item is not None else {}),
        }

    def _review(
        self,
        source: Mapping[str, object],
        args: Mapping[str, object],
        *,
        accept: bool,
    ) -> dict[str, object]:
        if self.dispatch_store is None:
            raise ValueError("Room Partner dispatch ledger is unavailable")
        work_item_id = _required_text(args, "workItemId", maximum=320)
        record = self.dispatch_store.get_by_work(work_item_id)
        self._require_dispatch_owner(source, record)
        # Review runs inside a current Facilitator turn, but the durable
        # WorkItem may have been delegated by an earlier Root in the same
        # long-lived Room Goal.  The dispatch-owner and Work ledger reviewer
        # fences below own authorization; keep event/dispatch attribution on
        # the original record instead of rebinding it to the current Root.
        self._active_root(source)
        payload = {
            "workId": work_item_id,
            "expectedRevision": args.get("expectedRevision"),
            "operabilityVerdict": args.get("operabilityVerdict"),
            "requirementVerdict": args.get("requirementVerdict"),
            "evidenceRefs": args.get("evidenceRefs"),
            "reason": args.get("reason"),
        }
        superseded_by = args.get("supersededByWorkId")
        if superseded_by is not None and str(superseded_by).strip():
            payload["supersededByWorkId"] = superseded_by
        callback = self.accept_room_work if accept else self.return_room_work
        wake = record.get("wake")
        prior_wake = dict(wake) if isinstance(wake, Mapping) else {}
        if callback is not None:
            outcome = callback(str(source["sessionId"]), payload)
            projected = outcome.get("work")
            work = projected if isinstance(projected, Mapping) else outcome
        elif self.room_work is not None:
            operation = self.room_work.accept if accept else self.room_work.return_for_revision
            work = operation(str(source["sessionId"]), payload)
        else:
            raise ValueError("Room WorkItem review is unavailable")
        reviewed_dispatch = self.dispatch_store.record_review(
            str(record["childDispatchId"]),
            accepted=accept,
        )
        if (
            self.wake_schedules is not None
            and str(prior_wake.get("state") or "") in {"pending", "scheduled"}
            and str(prior_wake.get("scheduleId") or "")
        ):
            try:
                self.wake_schedules.cancel_for_root(
                    str(prior_wake["scheduleId"]),
                    reason=(
                        "Room Partner WorkItem was explicitly accepted"
                        if accept
                        else "Room Partner WorkItem was returned for revision"
                    ),
                )
            except (KeyError, ValueError):
                pass
        return {
            "schemaVersion": "rag-ime.room-partner-result.v1",
            "operation": "accept" if accept else "return",
            "roomId": str(record["roomId"]),
            "rootId": str(record["rootId"]),
            "childDispatchId": str(record["childDispatchId"]),
            "workItemId": work_item_id,
            "status": str(reviewed_dispatch["status"]),
            "workItem": dict(work),
        }

    def _dispatch_record(
        self,
        args: Mapping[str, object],
    ) -> Mapping[str, object]:
        child_dispatch_id = _text(args.get("childDispatchId"), maximum=320)
        work_item_id = _text(args.get("workItemId"), maximum=320)
        if bool(child_dispatch_id) == bool(work_item_id):
            raise ValueError(
                "collect/wait requires exactly one childDispatchId or workItemId"
            )
        if child_dispatch_id:
            return self.dispatch_store.get(child_dispatch_id)
        return self.dispatch_store.get_by_work(work_item_id)

    @staticmethod
    def _require_dispatch_owner(
        source: Mapping[str, object],
        record: Mapping[str, object],
    ) -> None:
        if (
            str(source.get("roomId") or "") != str(record.get("roomId") or "")
            or str(source.get("id") or "")
            != str(record.get("sourceParticipantId") or "")
        ):
            raise ValueError(
                "only the accountable Facilitator can collect or review this dispatch"
            )

    def observe_room_event(self, event: Mapping[str, object]) -> None:
        """Settle a delegated receipt from append-only Room events."""

        if self.dispatch_store is None:
            return
        event_type = str(event.get("eventType") or "")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return
        if event_type == "room_post":
            post = payload.get("post")
            if not isinstance(post, Mapping) or str(post.get("kind") or "") != "work_result":
                return
            child_dispatch_id = str(post.get("dispatchId") or "")
            if not child_dispatch_id:
                return
            record = self.dispatch_store.get(child_dispatch_id)
            if str(record.get("targetParticipantId") or "") != str(
                post.get("authorActorRef") or ""
            ):
                return
            result = str(post.get("content") or "")
            self.dispatch_store.record_result(child_dispatch_id, result)
            work_result = post.get("workResult")
            work_result = (
                work_result if isinstance(work_result, Mapping) else {}
            )
            self._settle_dispatch(
                record,
                phase="completed",
                result=result,
                completion_source="room_post",
                post_id=str(post.get("postId") or ""),
                proposed_operability=str(
                    work_result.get("proposedOperabilityVerdict") or ""
                ),
                proposed_requirement=str(
                    work_result.get("proposedRequirementVerdict") or ""
                ),
            )
            return
        data = payload.get("data")
        if not isinstance(data, Mapping):
            data = payload
        child_dispatch_id = str(
            data.get("dispatchId") or data.get("childDispatchId") or ""
        )
        if not child_dispatch_id:
            return
        if event_type == "participant_message":
            message = data.get("message")
            if isinstance(message, Mapping):
                self.dispatch_store.record_result(
                    child_dispatch_id,
                    agent_message_text(message),
                )
            return
        if (
            event_type != "participant_activity"
            or str(data.get("activityKind") or "") != "child"
            or str(data.get("phase") or "")
            not in {"completed", "failed", "aborted"}
        ):
            return
        record = self.dispatch_store.get(child_dispatch_id)
        if str(record.get("status") or "") not in {"prepared", "dispatched"}:
            return
        result = str(record.get("result") or data.get("summary") or "")
        self._settle_dispatch(
            record,
            phase=str(data.get("phase") or "failed"),
            result=result,
            completion_source="session_terminal",
        )

    def _settle_dispatch(
        self,
        record: Mapping[str, object],
        *,
        phase: str,
        result: str,
        completion_source: str,
        post_id: str = "",
        proposed_operability: str = "",
        proposed_requirement: str = "",
    ) -> Mapping[str, object]:
        jev = getattr(self, "jev_application", None)
        if jev is not None:
            handled = jev.settle_dispatch(record, phase=phase, result=result,
                completion_source=completion_source, post_id=post_id,
                proposed_operability=proposed_operability, proposed_requirement=proposed_requirement)
            if handled is not None:
                return handled
        source = self.rooms.participant(str(record["sourceParticipantId"]))
        target = self.rooms.participant(str(record["targetParticipantId"]))
        work_item = (
            self.room_work.get(
                str(record["workItemId"]),
                room_id=str(record["roomId"]),
            )
            if self.room_work is not None
            else None
        )
        settled_work = self._settle_delegated_work(
            work_item,
            phase=phase,
            result=result,
            child_dispatch_id=str(record["childDispatchId"]),
            source=source,
            target=target,
            proposed_operability=proposed_operability,
            proposed_requirement=proposed_requirement,
        )
        if phase == "completed":
            work_state = str((settled_work or {}).get("state") or "review")
            status = "blocked" if work_state == "blocked" else "review"
        else:
            status = "aborted" if phase == "aborted" else "failed"
        settled = self.dispatch_store.settle(
            str(record["childDispatchId"]),
            status=status,
            result=result,
            completion_source=completion_source,
            post_id=post_id,
            error="" if status in {"review", "blocked"} else result,
        )
        self._schedule_completion_wake(settled)
        return settled

    def _schedule_completion_wake(
        self,
        record: Mapping[str, object],
    ) -> None:
        if getattr(self, "jev_application", None) is not None and self.jev_application.lifecycle.effect_for_dispatch(str(record.get("childDispatchId") or "")) is not None:
            return  # The committed WorkItem event advances the Jev controller.
        if self.wake_schedules is None:
            return
        wake = record.get("wake")
        wake = wake if isinstance(wake, Mapping) else {}
        if str(wake.get("state") or "") != "pending":
            return
        generation = int(wake.get("generation") or 0)
        child_dispatch_id = str(record["childDispatchId"])
        schedule_id = f"room-wake:{child_dispatch_id}:{generation}"
        if (
            str(record.get("status") or "") == "accepted"
            and generation >= _MAX_MISSING_TYPED_RESULT_WAKE_GENERATION
        ):
            instruction = (
                "上一主管回合已正常结束，但同一 Room Root 仍缺少结构化终态。"
                "不要输出普通回复，不要重复 collect 或 accept；立即且只调用一次 "
                "room_partner，参数 op=post、kind=result，并在 content 中如实汇总"
                "已验收结果与未验证边界。工具返回后直接结束回合。"
            )
        elif str(record.get("status") or "") == "accepted":
            instruction = (
                f"Room Partner {record['targetParticipantId']} 的 WorkItem "
                f"{record['workItemId']} 已完成双轴验收，但上一主管回合未形成终态。"
                f"先用 room_partner collect 查看 {child_dispatch_id} 并对账同一 Root 的"
                "全部 WorkItem、子 Agent 与后台任务；不要重复 accept。若均已结清，"
                "恰好一次 post(kind=result)，然后正常结束回合。"
            )
        elif str(record.get("status") or "") in {"failed", "aborted"}:
            instruction = (
                f"Room Partner {record['targetParticipantId']} 的工作已进入 "
                f"{record['status']}。先用 room_partner collect 查看 "
                f"{child_dispatch_id} 与 WorkItem {record['workItemId']}；"
                "若仍有可行修复，选择空闲伙伴并对同一 WorkItem 调用 retry，携带"
                "最新 expectedRevision 与具体 reason。不要对 failed/aborted WorkItem "
                "调用只适用于 review 的 accept 或 return。若重试预算已用尽或没有"
                "可行路径，则发布一条如实列出未解决边界的最终 result。"
            )
        elif str(record.get("status") or "") == "blocked":
            instruction = (
                f"Room Partner {record['targetParticipantId']} 的 WorkItem "
                f"{record['workItemId']} 因证据或其他可修复前置条件不足而 blocked。"
                f"先用 room_partner collect 查看 {child_dispatch_id}；补齐可修复前提后，"
                "选择空闲伙伴并对同一 WorkItem 调用 retry，携带最新 expectedRevision "
                "与具体 reason。不可修复时发布诚实的未解决终态。"
            )
        else:
            instruction = (
                f"Room Partner {record['targetParticipantId']} 的工作已进入 "
                f"{record['status']}。先用 room_partner collect 查看 "
                f"{child_dispatch_id} 与 WorkItem {record['workItemId']}；"
                "检查 WorkDocument 和证据后，用非空 reason 显式 accept，或 return。"
                "审查报告 unverified、changes_required、failed 或未解决 HIGH/MEDIUM "
                "时必须 return，不得写成 passed/satisfied；Runtime 会机械拒绝"
                "在这种提交之上 accept。"
            )
        try:
            self.wake_schedules.create_room_wake(
                schedule_id=schedule_id,
                target_session_id=str(record["sourceSessionId"]),
                created_by_session_id=str(record["targetSessionId"]),
                title="伙伴交付待验收",
                instruction=instruction,
                metadata={
                    "kind": "room_partner_completion",
                    "childDispatchId": child_dispatch_id,
                    "generation": generation,
                    "roomId": str(record["roomId"]),
                    "rootId": str(record["rootId"]),
                    "workItemId": str(record["workItemId"]),
                },
            )
        except Exception as exc:
            self.dispatch_store.mark_wake(
                child_dispatch_id,
                generation=generation,
                state="failed",
                error=_public_error(exc),
            )
            raise
        self.dispatch_store.mark_wake(
            child_dispatch_id,
            generation=generation,
            state="scheduled",
            schedule_id=schedule_id,
        )
        if self.notify_wake_scheduler is not None:
            self.notify_wake_scheduler()

    def dispatch_wake(self, claim: Mapping[str, object]) -> None:
        if self.dispatch_store is None:
            raise ValueError("Room completion wake adapter is unavailable")
        metadata = claim.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        child_dispatch_id = _required_text(
            metadata,
            "childDispatchId",
            maximum=320,
        )
        record = self.dispatch_store.get(child_dispatch_id)
        wake = record.get("wake")
        wake = wake if isinstance(wake, Mapping) else {}
        claim_schedule_id = str(claim.get("id") or "")
        try:
            claim_generation = int(metadata.get("generation"))
        except (TypeError, ValueError):
            claim_generation = -1
        if self._root_has_typed_result(record):
            if self.wake_schedules is not None and claim_schedule_id:
                try:
                    self.wake_schedules.cancel_for_root(
                        claim_schedule_id,
                        reason="Room Root already has its terminal result",
                    )
                except (KeyError, ValueError):
                    pass
            self.dispatch_store.mark_wake(
                child_dispatch_id,
                generation=claim_generation,
                state="cancelled",
                schedule_id=claim_schedule_id,
            )
            return
        if (
            str(record.get("status") or "")
            not in {"review", "blocked", "failed", "aborted", "accepted"}
            or str(wake.get("state") or "") != "scheduled"
            or claim_generation != int(wake.get("generation") or 0)
            or claim_schedule_id != str(wake.get("scheduleId") or "")
        ):
            if self.wake_schedules is not None and claim_schedule_id:
                try:
                    self.wake_schedules.cancel_for_root(
                        claim_schedule_id,
                        reason="Stale Room Partner completion wake",
                    )
                except (KeyError, ValueError):
                    pass
            return
        if self.dispatch_facilitator_wake is None:
            raise ValueError("Room completion wake adapter is unavailable")
        delivered = self.dispatch_facilitator_wake(claim, record)
        if delivered:
            self.dispatch_store.mark_wake(
                child_dispatch_id,
                generation=int(wake.get("generation") or 0),
                state="delivered",
                schedule_id=str(claim.get("id") or ""),
            )

    def record_wake_failure(
        self,
        claim: Mapping[str, object],
        error: BaseException,
    ) -> None:
        """Keep the Room dispatch projection aligned with a failed wake run."""

        if self.dispatch_store is None:
            return
        metadata = claim.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        child_dispatch_id = str(metadata.get("childDispatchId") or "").strip()
        if not child_dispatch_id:
            return
        try:
            generation = int(metadata.get("generation"))
        except (TypeError, ValueError):
            return
        self.dispatch_store.mark_wake(
            child_dispatch_id,
            generation=generation,
            state="failed",
            schedule_id=str(claim.get("id") or ""),
            error=_public_error(error),
        )

    def observe_wake_terminal_event(
        self,
        event: Any,
        schedule: Mapping[str, object],
    ) -> None:
        """Allocate one new Room wake after a recoverable Host terminal."""

        if self.dispatch_store is None or self.wake_schedules is None:
            return
        metadata = schedule.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        if str(metadata.get("kind") or "") != "room_partner_completion":
            return
        event_type = str(getattr(event, "event_type", "") or "")
        if event_type not in {"turn_completed", "turn_failed"}:
            return
        child_dispatch_id = str(metadata.get("childDispatchId") or "")
        if not child_dispatch_id:
            return
        record = self.dispatch_store.get(child_dispatch_id)
        payload = getattr(event, "payload", {})
        payload = payload if isinstance(payload, Mapping) else {}
        if event_type == "turn_completed":
            self._requeue_missing_typed_result_wake(
                record,
                schedule,
                session_id=str(getattr(event, "session_id", "") or ""),
                turn_id=str(getattr(event, "turn_id", "") or ""),
            )
            return
        failure = " ".join(
            (
                str(payload.get("failureKind") or ""),
                str(payload.get("error") or ""),
            )
        )
        self._requeue_failed_facilitator_wake(
            record,
            schedule,
            session_id=str(getattr(event, "session_id", "") or ""),
            turn_id=str(getattr(event, "turn_id", "") or ""),
            failure=failure,
        )

    def reconcile(self) -> None:
        """Recover missed terminal events and unscheduled wakes after restart."""

        if self.dispatch_store is None:
            return
        grouped: dict[tuple[str, str], list[Mapping[str, object]]] = {}
        for record in self.dispatch_store.inflight():
            grouped.setdefault(
                (str(record["roomId"]), str(record["rootId"])),
                [],
            ).append(record)
        for (room_id, root_id), records in grouped.items():
            expected = {str(record["childDispatchId"]) for record in records}
            for event in self._recovery_events_for_turn(
                room_id,
                root_id,
                dispatch_ids=tuple(sorted(expected)),
                event_types=(
                    "room_post",
                    "participant_message",
                    "participant_activity",
                ),
            ):
                payload = event.get("payload")
                payload = payload if isinstance(payload, Mapping) else {}
                data = payload.get("data")
                data = data if isinstance(data, Mapping) else payload
                post = payload.get("post")
                dispatch_id = (
                    str(post.get("dispatchId") or "")
                    if isinstance(post, Mapping)
                    else str(data.get("dispatchId") or data.get("childDispatchId") or "")
                )
                if dispatch_id in expected:
                    self.observe_room_event(event)
        acceptance_lookup = getattr(
            self.sessions,
            "prompt_acceptance_evidence",
            None,
        )
        terminal_lookup = getattr(
            self.sessions,
            "runtime_turn_terminal_event",
            None,
        )
        command_acceptance_lookup = self.command_acceptance_evidence
        if callable(terminal_lookup) and (
            callable(command_acceptance_lookup)
            or callable(acceptance_lookup)
        ):
            cursor = ""
            while records := self.dispatch_store.runtime_recovery_candidates(
                after_child_dispatch_id=cursor,
            ):
                cursor = str(records[-1]["childDispatchId"])
                for record in records:
                    self._recover_dispatch_from_session_ledger(
                        record,
                        acceptance_lookup=acceptance_lookup,
                        command_acceptance_lookup=(
                            command_acceptance_lookup
                        ),
                        terminal_lookup=terminal_lookup,
                    )
        for record in self.dispatch_store.pending_wakes():
            self._schedule_completion_wake(record)
        if self.wake_schedules is not None:
            self._requeue_legacy_preacceptance_conflict_wakes()
            self._requeue_completed_wakes_missing_typed_result()
            self._requeue_legacy_stale_facilitator_wakes()
            self._requeue_recoverable_facilitator_wakes(terminal_lookup)
            for record in self.dispatch_store.scheduled_wakes():
                wake = record.get("wake")
                wake = wake if isinstance(wake, Mapping) else {}
                schedule_id = str(wake.get("scheduleId") or "")
                if not schedule_id:
                    continue
                try:
                    schedule = self.wake_schedules.get(schedule_id)
                except KeyError:
                    self.dispatch_store.mark_wake(
                        str(record["childDispatchId"]),
                        generation=int(wake.get("generation") or 0),
                        state="failed",
                        schedule_id=schedule_id,
                        error="Room Partner wake schedule is missing",
                    )
                    continue
                schedule_status = str(schedule.get("status") or "")
                if schedule_status not in {"failed", "cancelled"}:
                    continue
                self.dispatch_store.mark_wake(
                    str(record["childDispatchId"]),
                    generation=int(wake.get("generation") or 0),
                    state=schedule_status,
                    schedule_id=schedule_id,
                    error=str(schedule.get("lastError") or ""),
                )

    def _recovery_events_for_turn(
        self,
        room_id: str,
        root_id: str,
        *,
        dispatch_ids: tuple[str, ...],
        event_types: tuple[str, ...],
    ) -> list[Mapping[str, object]]:
        dispatch_lookup = getattr(
            self.rooms,
            "list_recovery_events_for_dispatches",
            None,
        )
        if callable(dispatch_lookup):
            return dispatch_lookup(
                room_id,
                root_id,
                dispatch_ids=dispatch_ids,
                limit=2_000,
            )
        targeted_lookup = getattr(self.rooms, "list_events_for_turn", None)
        if callable(targeted_lookup):
            return targeted_lookup(
                room_id,
                root_id,
                event_types=event_types,
                after_sequence=0,
                limit=2_000,
            )
        return [
            event
            for event in self.rooms.list_events(
                room_id,
                after_sequence=0,
                limit=2_000,
            )
            if str(event.get("turnId") or "") == root_id
            and str(event.get("eventType") or "") in event_types
        ]

    def _requeue_legacy_preacceptance_conflict_wakes(self) -> None:
        """Repair a bounded legacy wake whose typed conflict was lost."""

        if (
            self.dispatch_store is None
            or self.wake_schedules is None
            or not callable(self.command_failure_evidence)
        ):
            return
        for record in self.dispatch_store.failed_wakes():
            wake = record.get("wake")
            wake = wake if isinstance(wake, Mapping) else {}
            generation = int(wake.get("generation") or 0)
            schedule_id = str(wake.get("scheduleId") or "")
            if (
                generation < 1
                or generation >= _MAX_LEGACY_PREACCEPTANCE_WAKE_GENERATION
                or not schedule_id
            ):
                continue
            try:
                schedule = self.wake_schedules.get(schedule_id)
            except KeyError:
                continue
            metadata = schedule.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            latest_run = schedule.get("latestRun")
            latest_run = (
                latest_run if isinstance(latest_run, Mapping) else {}
            )
            run_id = str(latest_run.get("runId") or "")
            source_session_id = str(record.get("sourceSessionId") or "")
            target_session_id = str(record.get("targetSessionId") or "")
            if (
                str(record.get("status") or "")
                not in {"review", "blocked", "failed", "aborted", "accepted"}
                or str(wake.get("state") or "") != "failed"
                or str(schedule.get("status") or "") != "failed"
                or str(latest_run.get("state") or "") != "failed"
                or str(latest_run.get("turnId") or "")
                or not run_id
                or not source_session_id
                or str(schedule.get("id") or "") != schedule_id
                or str(schedule.get("targetSessionId") or "")
                != source_session_id
                or str(schedule.get("createdBySessionId") or "")
                != target_session_id
                or str(metadata.get("kind") or "")
                != "room_partner_completion"
                or str(metadata.get("childDispatchId") or "")
                != str(record.get("childDispatchId") or "")
                or int(metadata.get("generation") or 0) != generation
                or str(metadata.get("roomId") or "")
                != str(record.get("roomId") or "")
                or str(metadata.get("rootId") or "")
                != str(record.get("rootId") or "")
                or str(metadata.get("workItemId") or "")
                != str(record.get("workItemId") or "")
            ):
                continue
            evidence = self.command_failure_evidence(
                source_session_id,
                run_id,
            )
            evidence = evidence if isinstance(evidence, Mapping) else {}
            cause_code = str(evidence.get("causeCode") or "").strip().upper()
            if cause_code not in {
                "SESSION_BUSY",
                "AGENT_TURN_CONFLICT",
            }:
                continue
            projected_result = latest_run.get("result")
            projected_result = (
                projected_result
                if isinstance(projected_result, Mapping)
                else {}
            )
            projected_cause = str(
                projected_result.get("causeCode") or ""
            ).strip().upper()
            if projected_cause and projected_cause != cause_code:
                continue
            requeued = self.dispatch_store.requeue_failed_wake(
                str(record["childDispatchId"]),
                generation=generation,
                expected_schedule_id=schedule_id,
                expected_error=str(record.get("error") or ""),
                max_generation=_MAX_LEGACY_PREACCEPTANCE_WAKE_GENERATION,
            )
            requeued_wake = requeued.get("wake")
            requeued_wake = (
                requeued_wake if isinstance(requeued_wake, Mapping) else {}
            )
            if (
                int(requeued_wake.get("generation") or 0) == generation + 1
                and str(requeued_wake.get("state") or "") == "pending"
            ):
                self._schedule_completion_wake(requeued)

    def _requeue_completed_wakes_missing_typed_result(self) -> None:
        if self.dispatch_store is None or self.wake_schedules is None:
            return
        for record in self.dispatch_store.terminal_result_candidates():
            wake = record.get("wake")
            wake = wake if isinstance(wake, Mapping) else {}
            schedule_id = str(wake.get("scheduleId") or "")
            if not schedule_id:
                continue
            try:
                schedule = self.wake_schedules.get(schedule_id)
            except KeyError:
                continue
            latest_run = schedule.get("latestRun")
            latest_run = latest_run if isinstance(latest_run, Mapping) else {}
            if (
                str(schedule.get("status") or "") == "completed"
                and str(latest_run.get("state") or "") == "completed"
            ):
                self._requeue_missing_typed_result_wake(
                    record,
                    schedule,
                    session_id=str(latest_run.get("sessionId") or ""),
                    turn_id=str(latest_run.get("turnId") or ""),
                )

    def _requeue_missing_typed_result_wake(
        self,
        record: Mapping[str, object],
        schedule: Mapping[str, object],
        *,
        session_id: str,
        turn_id: str,
    ) -> bool:
        """Give the Facilitator one bounded chance to publish the typed result."""

        wake = record.get("wake")
        wake = wake if isinstance(wake, Mapping) else {}
        metadata = schedule.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        latest_run = schedule.get("latestRun")
        latest_run = latest_run if isinstance(latest_run, Mapping) else {}
        generation = int(wake.get("generation") or 0)
        wake_state = str(wake.get("state") or "")
        eligible_wake_state = wake_state in {"scheduled", "delivered"} or (
            wake_state == "failed"
            and str(record.get("error") or "") == _MISSING_TYPED_RESULT_ERROR
        )
        if (
            self.dispatch_store is None
            or str(record.get("status") or "") != "accepted"
            or not eligible_wake_state
            or str(schedule.get("status") or "") != "completed"
            or str(latest_run.get("state") or "") != "completed"
            or str(latest_run.get("sessionId") or "") != session_id
            or str(latest_run.get("turnId") or "") != turn_id
            or not session_id
            or not turn_id
            or str(record.get("sourceSessionId") or "") != session_id
            or str(wake.get("scheduleId") or "")
            != str(schedule.get("id") or "")
            or str(metadata.get("kind") or "")
            != "room_partner_completion"
            or str(metadata.get("childDispatchId") or "")
            != str(record.get("childDispatchId") or "")
            or int(metadata.get("generation") or 0) != generation
            or str(metadata.get("roomId") or "")
            != str(record.get("roomId") or "")
            or str(metadata.get("rootId") or "")
            != str(record.get("rootId") or "")
            or self._root_has_typed_result(record)
        ):
            return False
        if generation >= _MAX_MISSING_TYPED_RESULT_WAKE_GENERATION:
            if self._project_terminal_result_from_accepted_work(
                record,
                schedule,
                session_id=session_id,
                turn_id=turn_id,
            ):
                settled = self.dispatch_store.settle_terminal_projection(
                    str(record["childDispatchId"]),
                    generation=generation,
                    expected_schedule_id=str(schedule["id"]),
                    expected_error=_MISSING_TYPED_RESULT_ERROR,
                )
                settled_wake = settled.get("wake")
                settled_wake = (
                    settled_wake
                    if isinstance(settled_wake, Mapping)
                    else {}
                )
                return (
                    str(settled_wake.get("state") or "") == "delivered"
                    and not str(settled.get("error") or "")
                )
            self.dispatch_store.mark_wake(
                str(record["childDispatchId"]),
                generation=generation,
                state="failed",
                schedule_id=str(schedule["id"]),
                error=_MISSING_TYPED_RESULT_ERROR,
            )
            return False
        requeued = self.dispatch_store.requeue_delivered_wake(
            str(record["childDispatchId"]),
            generation=generation,
            expected_schedule_id=str(schedule["id"]),
            max_generation=_MAX_MISSING_TYPED_RESULT_WAKE_GENERATION,
        )
        requeued_wake = requeued.get("wake")
        requeued_wake = (
            requeued_wake if isinstance(requeued_wake, Mapping) else {}
        )
        if (
            int(requeued_wake.get("generation") or 0) != generation + 1
            or str(requeued_wake.get("state") or "") != "pending"
        ):
            return False
        self._schedule_completion_wake(requeued)
        return True

    def _project_terminal_result_from_accepted_work(
        self,
        record: Mapping[str, object],
        schedule: Mapping[str, object],
        *,
        session_id: str,
        turn_id: str,
    ) -> bool:
        """Project one final receipt from explicit persisted reviews.

        The fallback never parses assistant prose and never changes WorkItem
        review state.  It runs only after the bounded finalization wake itself
        completed and every WorkItem under the Root already carries the
        Facilitator's two-axis acceptance evidence.
        """

        publish_projection = getattr(
            self.room_events,
            "publish_projection",
            None,
        )
        if self.room_work is None or not callable(publish_projection):
            return False
        if self._root_has_typed_result(record):
            return False
        room_id = str(record.get("roomId") or "")
        root_id = str(record.get("rootId") or "")
        source_participant_id = str(record.get("sourceParticipantId") or "")
        if not room_id or not root_id or not source_participant_id:
            return False
        room = self.rooms.get(room_id)
        if not isinstance(room, Mapping):
            return False
        moderator = self._active_room_moderator(room)
        if moderator is None:
            return False
        moderator_participant_id = str(moderator.get("id") or "")
        moderator_session_id = str(moderator.get("sessionId") or "")
        if (
            not moderator_participant_id
            or not moderator_session_id
            or source_participant_id != moderator_participant_id
            or session_id != moderator_session_id
        ):
            return False
        root_work = self._accepted_root_work(
            room_id=room_id,
            root_turn_id=root_id,
            facilitator_participant_id=moderator_participant_id,
        )
        if not root_work:
            return False
        latest_run = schedule.get("latestRun")
        latest_run = latest_run if isinstance(latest_run, Mapping) else {}
        wake = record.get("wake")
        wake = wake if isinstance(wake, Mapping) else {}
        generation = int(wake.get("generation") or 0)
        schedule_id = str(schedule.get("id") or "")
        finished_at_ms = int(
            latest_run.get("finishedAtMs")
            or schedule.get("updatedAtMs")
            or 0
        )
        if not schedule_id or finished_at_ms <= 0:
            return False
        ordered_work = sorted(
            root_work,
            key=lambda item: str(item.get("id") or ""),
        )
        post = {
            "schemaVersion": "wisdom-weasel.room-post.v2",
            "postId": f"room-post:runtime-terminal:{root_id}",
            "roomId": room_id,
            "rootId": root_id,
            "generation": generation,
            "dispatchId": str(record.get("parentDispatchId") or ""),
            "authorActorRef": moderator_participant_id,
            "kind": "result",
            "visibility": "room",
            "content": _terminal_result_content(ordered_work),
            "idempotencyKey": f"runtime-terminal:{root_id}",
            "publicationSource": {
                "kind": "runtime_projection",
                "ref": schedule_id,
            },
            "createdAtMs": finished_at_ms,
        }
        validate_contract(post, "room-post.v2.json")
        with getattr(self.room_turns, "lock", nullcontext()):
            cancelled = getattr(self.room_turns, "is_cancelled", None)
            if callable(cancelled) and cancelled(moderator_session_id, root_id):
                return False
            if self._accepted_root_work(room_id=room_id, root_turn_id=root_id,
                                        facilitator_participant_id=moderator_participant_id) is None:
                return False
            try:
                publish_projection(
                    projection_key=f"room-terminal-result:{room_id}:{root_id}",
                    projection_guard=lambda: self._require_accepted_root_work(
                        room_id=room_id, root_turn_id=root_id,
                        facilitator_participant_id=moderator_participant_id),
                    room_id=room_id,
                    event_type="room_post",
                    payload={
                        "post": post,
                        "sourceTurnId": turn_id,
                        "terminalProjection": {
                            "kind": "runtime_terminal_receipt",
                            "basis": "explicit_dual_axis_work_reviews",
                            "sourceScheduleId": schedule_id,
                            "sourceRunId": str(latest_run.get("runId") or ""),
                        },
                    },
                    turn_id=root_id,
                    participant_id=moderator_participant_id,
                    source_session_id=moderator_session_id,
                    topic_id=self.room_topic_for_turn(root_id),
                    created_at_ms=finished_at_ms,
                )
            except _RootWorkPending:
                return False
        return True

    def _require_accepted_root_work(
        self, *, room_id: str, root_turn_id: str, facilitator_participant_id: str,
    ) -> None:
        if self._accepted_root_work(room_id=room_id, root_turn_id=root_turn_id,
                                    facilitator_participant_id=facilitator_participant_id) is None:
            raise _RootWorkPending("Room result requires every WorkItem to be explicitly accepted with passed/satisfied evidence")

    def _accepted_root_work(
        self,
        *,
        room_id: str,
        root_turn_id: str,
        facilitator_participant_id: str,
    ) -> list[Mapping[str, object]] | None:
        if self.room_work is None:
            return []
        root_work = self.room_work.list_for_root(
            room_id=room_id,
            root_turn_id=root_turn_id,
        )
        if (
            self.dispatch_store is not None
            and self.dispatch_store.has_unsettled_root_dispatches(
                room_id=room_id,
                root_id=root_turn_id,
            )
        ):
            return None
        for item in root_work:
            review = item.get("review")
            review = review if isinstance(review, Mapping) else {}
            evidence_refs = review.get("evidenceRefs")
            if (
                str(item.get("state") or "") != "done"
                or not str(item.get("resultSummary") or "").strip()
                or str(review.get("operabilityVerdict") or "") != "passed"
                or str(review.get("requirementVerdict") or "") != "satisfied"
                or not isinstance(evidence_refs, list)
                or not any(str(value or "").strip() for value in evidence_refs)
                or str(review.get("reviewerParticipantId") or "")
                != facilitator_participant_id
                or int(review.get("reviewedAtMs") or 0) <= 0
            ):
                return None
        return sorted(root_work, key=lambda item: str(item.get("id") or ""))

    def _retained_root_typed_result(
        self,
        *,
        room_id: str,
        root_turn_id: str,
    ) -> Mapping[str, object] | None:
        """Return the one retained typed result event for an exact replay."""

        list_events = getattr(self.rooms, "list_events", None)
        if not room_id or not root_turn_id or not callable(list_events):
            return None
        try:
            room = self.rooms.get(room_id)
            if not isinstance(room, Mapping):
                return None
            last_sequence = int(room.get("lastEventSequence") or 0)
            events = list_events(
                room_id,
                after_sequence=max(0, last_sequence - 2_000),
                limit=2_000,
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(events, (list, tuple)):
            return None
        candidates: list[Mapping[str, object]] = []
        for event in events:
            if not isinstance(event, Mapping):
                continue
            if (
                str(event.get("roomId") or "") != room_id
                or str(event.get("turnId") or "") != root_turn_id
                or str(event.get("eventType") or "") != "room_post"
            ):
                continue
            payload = event.get("payload")
            payload = payload if isinstance(payload, Mapping) else {}
            post = payload.get("post")
            publication_source = (
                post.get("publicationSource")
                if isinstance(post, Mapping)
                else None
            )
            if (
                isinstance(post, Mapping)
                and str(post.get("kind") or "") == "result"
                and str(post.get("roomId") or "") == room_id
                and str(post.get("rootId") or "") == root_turn_id
                and str(post.get("postId") or "")
                and str(post.get("idempotencyKey") or "")
                and isinstance(publication_source, Mapping)
                and str(publication_source.get("kind") or "") == "room_post"
                and str(publication_source.get("ref") or "")
                == str(post.get("idempotencyKey") or "")
            ):
                candidates.append(event)
        if len(candidates) != 1:
            return None
        event = candidates[0]
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        post = payload.get("post")
        event_id = str(event.get("eventId") or "")
        if (
            not event_id
            or not isinstance(post, Mapping)
            or not str(post.get("postId") or "")
            or not str(post.get("idempotencyKey") or "")
        ):
            return None
        return event

    def _root_has_typed_result(self, record: Mapping[str, object]) -> bool:
        room_id = str(record.get("roomId") or "")
        root_id = str(record.get("rootId") or "")
        if not room_id or not root_id:
            return False
        has_projection = getattr(self.room_events, "has_projection", None)
        if callable(has_projection) and has_projection(
            f"room-terminal-result:{room_id}:{root_id}"
        ):
            return True
        has_typed_result = getattr(self.rooms, "has_typed_result", None)
        if callable(has_typed_result):
            return bool(has_typed_result(room_id, root_id))
        list_events = getattr(self.rooms, "list_events", None)
        if not callable(list_events):
            return False
        room = self.rooms.get(room_id)
        last_sequence = int(room.get("lastEventSequence") or 0)
        for event in list_events(
            room_id,
            after_sequence=max(0, last_sequence - 2_000),
            limit=2_000,
        ):
            if (
                str(event.get("turnId") or "") != root_id
                or str(event.get("eventType") or "") != "room_post"
            ):
                continue
            payload = event.get("payload")
            payload = payload if isinstance(payload, Mapping) else {}
            post = payload.get("post")
            if isinstance(post, Mapping) and str(post.get("kind") or "") == "result":
                return True
        return False


    def _requeue_legacy_stale_facilitator_wakes(self) -> None:
        """Repair one wake cancelled by an older Gateway during an upgrade.

        Older Gateway code rejected an already-accepted dispatch as stale even
        though the Facilitator still owed the Root one typed terminal result.
        The exact cancellation reason and a separate generation ceiling keep
        this compatibility repair bounded; ordinary Host failures retain their
        existing single-retry budget.
        """

        if self.dispatch_store is None or self.wake_schedules is None:
            return
        for record in self.dispatch_store.scheduled_wakes():
            wake = record.get("wake")
            wake = wake if isinstance(wake, Mapping) else {}
            schedule_id = str(wake.get("scheduleId") or "")
            if not schedule_id:
                continue
            try:
                schedule = self.wake_schedules.get(schedule_id)
            except KeyError:
                continue
            metadata = schedule.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            generation = int(wake.get("generation") or 0)
            if (
                str(record.get("status") or "") != "accepted"
                or str(schedule.get("status") or "") != "cancelled"
                or str(schedule.get("lastError") or "")
                != _LEGACY_STALE_WAKE_ERROR
                or str(metadata.get("kind") or "")
                != "room_partner_completion"
                or str(metadata.get("childDispatchId") or "")
                != str(record.get("childDispatchId") or "")
                or int(metadata.get("generation") or 0) != generation
                or str(metadata.get("roomId") or "")
                != str(record.get("roomId") or "")
                or str(metadata.get("rootId") or "")
                != str(record.get("rootId") or "")
                or str(wake.get("scheduleId") or "")
                != str(schedule.get("id") or "")
            ):
                continue
            requeued = self.dispatch_store.requeue_delivered_wake(
                str(record["childDispatchId"]),
                generation=generation,
                expected_schedule_id=schedule_id,
                max_generation=_MAX_LEGACY_STALE_WAKE_GENERATION,
            )
            requeued_wake = requeued.get("wake")
            requeued_wake = (
                requeued_wake if isinstance(requeued_wake, Mapping) else {}
            )
            if (
                int(requeued_wake.get("generation") or 0) == generation + 1
                and str(requeued_wake.get("state") or "") == "pending"
            ):
                self._schedule_completion_wake(requeued)

    def _requeue_recoverable_facilitator_wakes(
        self,
        terminal_lookup: Callable[[str, str], Mapping[str, object] | None]
        | object,
    ) -> None:
        """Resume one accepted Root after a recoverable Host-owned wake failure."""

        if (
            self.dispatch_store is None
            or self.wake_schedules is None
            or not callable(terminal_lookup)
        ):
            return
        for record in self.dispatch_store.delivered_wakes():
            wake = record.get("wake")
            wake = wake if isinstance(wake, Mapping) else {}
            schedule_id = str(wake.get("scheduleId") or "")
            if not schedule_id:
                continue
            try:
                schedule = self.wake_schedules.get(schedule_id)
            except KeyError:
                continue
            latest_run = schedule.get("latestRun")
            latest_run = latest_run if isinstance(latest_run, Mapping) else {}
            source_session_id = str(record["sourceSessionId"])
            turn_id = str(latest_run.get("turnId") or "")
            if (
                str(schedule.get("status") or "") != "failed"
                or str(latest_run.get("state") or "") != "failed"
                or not turn_id
            ):
                continue
            terminal = terminal_lookup(source_session_id, turn_id)
            if not isinstance(terminal, Mapping) or str(
                terminal.get("eventType") or ""
            ) != "turn_failed":
                continue
            self._requeue_failed_facilitator_wake(
                record,
                schedule,
                session_id=source_session_id,
                turn_id=turn_id,
                failure=str(terminal.get("status") or ""),
            )

    def _requeue_failed_facilitator_wake(
        self,
        record: Mapping[str, object],
        schedule: Mapping[str, object],
        *,
        session_id: str,
        turn_id: str,
        failure: str,
    ) -> bool:
        wake = record.get("wake")
        wake = wake if isinstance(wake, Mapping) else {}
        metadata = schedule.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        latest_run = schedule.get("latestRun")
        latest_run = latest_run if isinstance(latest_run, Mapping) else {}
        generation = int(wake.get("generation") or 0)
        if (
            self.dispatch_store is None
            or str(record.get("status") or "")
            not in {"review", "blocked", "failed", "aborted", "accepted"}
            or str(wake.get("state") or "") not in {"scheduled", "delivered"}
            or str(schedule.get("status") or "") != "failed"
            or str(latest_run.get("state") or "") != "failed"
            or str(latest_run.get("sessionId") or "") != session_id
            or str(latest_run.get("turnId") or "") != turn_id
            or not session_id
            or not turn_id
            or str(record.get("sourceSessionId") or "") != session_id
            or str(wake.get("scheduleId") or "")
            != str(schedule.get("id") or "")
            or str(metadata.get("kind") or "")
            != "room_partner_completion"
            or str(metadata.get("childDispatchId") or "")
            != str(record.get("childDispatchId") or "")
            or int(metadata.get("generation") or 0) != generation
            or str(metadata.get("roomId") or "")
            != str(record.get("roomId") or "")
            or str(metadata.get("rootId") or "")
            != str(record.get("rootId") or "")
        ):
            return False
        if (
            generation >= _MAX_AUTONOMOUS_COMPLETION_WAKE_GENERATION
            or not _recoverable_runtime_host_failure(failure)
        ):
            self.dispatch_store.mark_wake(
                str(record["childDispatchId"]),
                generation=generation,
                state="failed",
                schedule_id=str(schedule["id"]),
                error=failure,
            )
            return False
        requeued = self.dispatch_store.requeue_delivered_wake(
            str(record["childDispatchId"]),
            generation=generation,
            expected_schedule_id=str(schedule["id"]),
            max_generation=_MAX_AUTONOMOUS_COMPLETION_WAKE_GENERATION,
        )
        requeued_wake = requeued.get("wake")
        requeued_wake = (
            requeued_wake if isinstance(requeued_wake, Mapping) else {}
        )
        if (
            int(requeued_wake.get("generation") or 0) != generation + 1
            or str(requeued_wake.get("state") or "") != "pending"
        ):
            return False
        self._schedule_completion_wake(requeued)
        return True

    def _recover_dispatch_from_session_ledger(
        self,
        record: Mapping[str, object],
        *,
        acceptance_lookup: Callable[
            [str, str], Mapping[str, object] | None
        ]
        | None,
        command_acceptance_lookup: Callable[
            [str, str], Mapping[str, object] | None
        ]
        | None,
        terminal_lookup: Callable[[str, str], Mapping[str, object] | None],
    ) -> None:
        """Repair the Room projection from Pi's durable prompt/turn ledger."""

        child_dispatch_id = str(record["childDispatchId"])
        if getattr(self, "jev_application", None) is not None and self.jev_application.lifecycle.effect_for_dispatch(child_dispatch_id) is not None:
            return  # Jev reconciles exact effect receipts; missing is unknown.
        target_session_id = str(record["targetSessionId"])
        target_turn_id = str(record.get("targetSessionTurnId") or "")
        if not target_turn_id:
            acceptance = (
                command_acceptance_lookup(
                    target_session_id,
                    child_dispatch_id,
                )
                if callable(command_acceptance_lookup)
                else None
            )
            if acceptance is None and callable(acceptance_lookup):
                acceptance = acceptance_lookup(
                    target_session_id,
                    child_dispatch_id,
                )
            if acceptance is None:
                result = (
                    "Partner dispatch was not durably accepted before Runtime restart."
                )
                self._publish_recovered_terminal(
                    record,
                    phase="failed",
                    status="recovery_missing_prompt_acceptance",
                    result=result,
                )
                self._settle_dispatch(
                    record,
                    phase="failed",
                    result=result,
                    completion_source="restart_recovery",
                )
                return
            target_turn_id = str(acceptance.get("turnId") or "")
            if not target_turn_id:
                result = (
                    "Partner prompt acceptance evidence did not retain a Runtime turn."
                )
                self._publish_recovered_terminal(
                    record,
                    phase="failed",
                    status="recovery_missing_target_turn",
                    result=result,
                )
                self._settle_dispatch(
                    record,
                    phase="failed",
                    result=result,
                    completion_source="restart_recovery",
                )
                return
            record = self.dispatch_store.mark_dispatched(
                child_dispatch_id,
                target_session_turn_id=target_turn_id,
            )

        terminal = terminal_lookup(target_session_id, target_turn_id)
        if terminal is None:
            return
        if str(terminal.get("eventType") or "") == "turn_failed":
            phase = "failed"
        elif str(terminal.get("status") or "") == "aborted":
            phase = "aborted"
        else:
            phase = "completed"
        disposition = {
            "completed": "completed",
            "failed": "failed",
            "aborted": "was aborted",
        }[phase]
        result = (
            f"Partner Session {disposition}; recovered from durable Runtime "
            f"terminal event {terminal.get('eventId') or target_turn_id}."
        )
        self._publish_recovered_terminal(
            record,
            phase=phase,
            status="recovered_after_restart",
            result=result,
            terminal=terminal,
        )
        current = self.dispatch_store.get(child_dispatch_id)
        if str(current.get("status") or "") in {"prepared", "dispatched"}:
            self._settle_dispatch(
                current,
                phase=phase,
                result=result,
                completion_source="restart_recovery",
            )

    def _publish_recovered_terminal(
        self,
        record: Mapping[str, object],
        *,
        phase: str,
        status: str,
        result: str,
        terminal: Mapping[str, object] | None = None,
    ) -> None:
        terminal = terminal or {}
        publish = self.room_events.publish_child_terminal if terminal else self.room_events.publish
        projection_args = {
            "runtime_event_id": str(terminal.get("eventId") or record.get("targetSessionTurnId") or ""),
            "dispatch_id": str(record["childDispatchId"]),
        } if terminal else {}
        publish(
            **projection_args,
            room_id=str(record["roomId"]),
            event_type="participant_activity",
            payload={
                "activityKind": "child",
                "phase": phase,
                "status": status,
                "rootId": str(record["rootId"]),
                "childDispatchId": str(record["childDispatchId"]),
                "dispatchId": str(record["childDispatchId"]),
                "targetParticipantId": str(record["targetParticipantId"]),
                "targetSessionId": str(record["targetSessionId"]),
                "targetSessionTurnId": str(
                    record.get("targetSessionTurnId")
                    or terminal.get("turnId")
                    or ""
                ),
                "summary": result[:2_000],
                **(
                    {"sourceRuntimeEventId": str(terminal.get("eventId") or "")}
                    if terminal
                    else {}
                ),
            },
            turn_id=str(record["rootId"]),
            participant_id=str(record["targetParticipantId"]),
            source_session_id=str(record["targetSessionId"]),
            topic_id=self.room_topic_for_turn(str(record["rootId"])),
        )

    def cancel_root(
        self,
        *,
        room_id: str,
        root_id: str,
        reason: str,
    ) -> list[dict[str, object]]:
        if self.dispatch_store is None:
            return []
        records = self.dispatch_store.cancel_root(
            room_id=room_id,
            root_id=root_id,
            reason=reason,
        )
        if self.wake_schedules is not None:
            for record in records:
                wake = record.get("wake")
                wake = wake if isinstance(wake, Mapping) else {}
                schedule_id = str(wake.get("scheduleId") or "")
                if not schedule_id:
                    continue
                try:
                    self.wake_schedules.cancel_for_root(
                        schedule_id,
                        reason=reason,
                    )
                except (KeyError, ValueError):
                    pass
        return records

    def _peer_list(self, source: Mapping[str, object]) -> dict[str, object]:
        root_id, dispatch_id = self.room_turns.active_turn(
            str(source["sessionId"])
        )
        room = self.rooms.get(str(source["roomId"]))
        peers = []
        for ordinal, participant in enumerate(room.get("participants", [])):
            if (
                not isinstance(participant, Mapping)
                or str(participant.get("status") or "") != "active"
                or str(participant.get("id") or "") == str(source["id"])
            ):
                continue
            peers.append(
                {
                    "participantId": str(participant.get("id") or ""),
                    "displayName": str(participant.get("displayName") or ""),
                    "celestialName": _room_celestial_name(
                        participant,
                        fallback_ordinal=ordinal,
                    ),
                    "collaborationRole": str(
                        participant.get("collaborationRole") or "implementer"
                    ),
                    "sessionId": str(participant.get("sessionId") or ""),
                }
            )
        items = (
            self.list_room_intercom(str(source["sessionId"]))
            if self.list_room_intercom is not None
            else []
        )
        return {
            "schemaVersion": "rag-ime.room-partner-result.v1",
            "operation": "peer_list",
            "roomId": str(source["roomId"]),
            "rootId": root_id,
            "dispatchId": dispatch_id,
            "peers": peers,
            "messages": [dict(item) for item in items],
        }

    def _peer_message(
        self,
        source: Mapping[str, object],
        args: Mapping[str, object],
        *,
        operation: str,
        tool_call_id: str,
    ) -> dict[str, object]:
        if self.send_room_intercom is None:
            raise ValueError("Room peer messaging is unavailable")
        # Peer communication is Room-member authority, not Facilitator or
        # parent-turn authority. In particular, an intercom-delivered Pi turn
        # may answer its sender even though it is not a dispatched Room root.
        root_id, dispatch_id = self.room_turns.active_turn(
            str(source["sessionId"])
        )
        kind = {
            "peer_send": "send",
            "peer_ask": "ask",
            "peer_reply": "reply",
        }[operation]
        payload: dict[str, object] = {
            "kind": kind,
            "content": _required_text(args, "content", maximum=4_000),
            "clientMessageId": _text(args.get("clientMessageId"), maximum=200)
            or tool_call_id,
        }
        if kind != "reply":
            payload["targetParticipantId"] = _required_text(
                args,
                "targetParticipantId",
                maximum=240,
            )
        else:
            payload["replyTo"] = _required_text(args, "replyTo", maximum=240)
        item = self.send_room_intercom(str(source["sessionId"]), payload)
        return {
            "schemaVersion": "rag-ime.room-partner-result.v1",
            "operation": operation,
            "roomId": str(source["roomId"]),
            "rootId": root_id,
            "dispatchId": dispatch_id,
            "message": dict(item),
        }

    def _delegate_batch(
        self,
        source: Mapping[str, object],
        args: Mapping[str, object],
        *,
        tool_call_id: str,
    ) -> dict[str, object]:
        raw_tasks = args.get("tasks")
        if not isinstance(raw_tasks, list) or not 2 <= len(raw_tasks) <= 7:
            raise ValueError("tasks must contain between two and seven Partner tasks")
        tasks: list[dict[str, object]] = []
        target_ids: list[str] = []
        for index, value in enumerate(raw_tasks):
            if not isinstance(value, Mapping):
                raise ValueError(f"tasks[{index}] must be an object")
            task = dict(value)
            target_id = _required_text(
                task,
                "targetParticipantId",
                maximum=240,
            )
            _required_text(task, "task", maximum=8_000)
            _required_text(task, "expectedOutput", maximum=1_200)
            criteria = _string_list(
                task.get("acceptanceCriteria"),
                limit=8,
                maximum=320,
            )
            if not criteria:
                raise ValueError(
                    f"tasks[{index}].acceptanceCriteria must not be empty"
                )
            if target_id in target_ids:
                raise ValueError("delegate_batch requires unique target Partners")
            target_ids.append(target_id)
            tasks.append(task)

        room_id = str(source["roomId"])
        root_id, _parent_dispatch_id = self._active_root(source)
        with getattr(self.room_turns, "lock", nullcontext()):
            self._require_open_root(source, root_id)
        existing_session_ids = {
            session_id
            for _participant, session_id, _turn_id in self.room_turns.turn_targets(
                root_id,
                resolve=lambda value: self.rooms.participant_for_session(
                    value,
                    active_only=False,
                ),
            )
        }
        targets: list[Mapping[str, object]] = []
        target_session_ids: list[str] = []
        existing_dispatch_ids: list[str] = []
        for target_id in target_ids:
            target = self.rooms.participant(target_id)
            if (
                str(target.get("roomId") or "") != room_id
                or str(target.get("status") or "") != "active"
            ):
                raise ValueError("target Partner is not active in this Room")
            if target_id == str(source["id"]):
                raise ValueError("a Partner cannot delegate Room work to itself")
            target_session_id = str(target.get("sessionId") or "")
            if not target_session_id:
                raise ValueError("target Partner has no Session")
            index = len(targets)
            existing_dispatch_id = self._existing_child_dispatch(
                room_id,
                root_id,
                f"{tool_call_id}:{index}",
            )
            if self.pending_removal(room_id, target_id) and not existing_dispatch_id:
                raise ValueError("target Partner is pending Room removal")
            if target_session_id in existing_session_ids and not existing_dispatch_id:
                raise ValueError(
                    "target Partner is already active in this Room turn"
                )
            targets.append(target)
            target_session_ids.append(target_session_id)
            existing_dispatch_ids.append(existing_dispatch_id)

        reserved_session_ids = [
            session_id
            for session_id, existing_dispatch_id in zip(
                target_session_ids,
                existing_dispatch_ids,
                strict=True,
            )
            if not existing_dispatch_id
        ]

        target_by_session_id = {
            session_id: target
            for session_id, target in zip(
                target_session_ids,
                targets,
                strict=True,
            )
        }

        def ensure_active(session_id: str) -> None:
            target = target_by_session_id[session_id]
            latest = self.rooms.participant_for_session(
                session_id,
                active_only=True,
            )
            if (latest is None or str(latest.get("id") or "") != str(target["id"])
                or self.pending_removal(room_id, str(target["id"]))):
                raise ValueError("target Partner changed or is pending removal before batch dispatch")

        priority_reservation = None
        try:
            if reserved_session_ids:
                priority_reservation = self.room_turns.hold_priority_if_idle(
                    reserved_session_ids,
                    ensure_available=ensure_active,
                )
        except RoomSessionBusyError:
            raise ValueError("one or more target Partners are currently busy") from None
        try:
            busy = [
                target
                for target, session_id in zip(targets, target_session_ids, strict=True)
                if session_id in reserved_session_ids and not self.room_target_idle(
                    session_id,
                    allow_user_priority=True,
                )
            ]
            if busy:
                names = "、".join(
                    str(item.get("displayName") or "Partner") for item in busy
                )
                raise ValueError(f"target Partners are currently busy: {names}")

            phase = _required_text(args, "phase", maximum=120)
            wave_uuid = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"rag-ime:{room_id}:{root_id}:{tool_call_id}",
            )
            wave_id = f"room-wave:{wave_uuid}"
            indexed_results: dict[int, dict[str, object]] = {}
            with ThreadPoolExecutor(
                max_workers=len(tasks),
                thread_name_prefix="room-partner-wave",
            ) as executor:
                futures = {
                    executor.submit(
                        self._delegate,
                        source,
                        task,
                        tool_call_id=f"{tool_call_id}:{index}",
                        priority_reserved=not existing_dispatch_ids[index],
                        wave_id=wave_id,
                        phase=phase,
                        parallel_index=index,
                        parallel_size=len(tasks),
                    ): index
                    for index, task in enumerate(tasks)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        indexed_results[index] = future.result()
                    except Exception as exc:
                        indexed_results[index] = {
                            "schemaVersion": "rag-ime.room-partner-result.v1",
                            "operation": "delegate",
                            "roomId": room_id,
                            "rootId": root_id,
                            "participantId": target_ids[index],
                            "status": "failed",
                            "result": "",
                            "error": _public_error(exc),
                            "waveId": wave_id,
                            "phase": phase,
                            "parallelIndex": index,
                            "parallelSize": len(tasks),
                        }
        finally:
            # The wave acquired these reservations and releases them once,
            # including tasks that failed before dispatch_target was reached.
            for session_id in reserved_session_ids:
                self.room_turns.release_priority_session(session_id, reservation=priority_reservation)

        results = [indexed_results[index] for index in range(len(tasks))]
        accepted = sum(
            str(item.get("status") or "") == "accepted" for item in results
        )
        failed = len(results) - accepted
        return {
            "schemaVersion": "rag-ime.room-partner-result.v1",
            "operation": "delegate_batch",
            "roomId": room_id,
            "rootId": root_id,
            "waveId": wave_id,
            "phase": phase,
            "parallelism": len(tasks),
            "status": (
                "accepted"
                if failed == 0
                else "failed"
                if accepted == 0
                else "partial"
            ),
            "accepted": accepted,
            "failed": failed,
            "results": results,
        }

    def _participant(self, session_id: str) -> dict[str, object]:
        participant = self.rooms.participant_for_session(
            session_id,
            active_only=True,
        )
        if participant is None:
            raise ValueError("room_partner is only available in an active Room")
        room = self.rooms.get(str(participant["roomId"]))
        if str(room.get("status") or "") != "active":
            raise ValueError("Room is not active")
        return participant

    def _active_root(
        self,
        source: Mapping[str, object],
    ) -> tuple[str, str]:
        root_id, dispatch_id = self.room_turns.active_turn(
            str(source["sessionId"])
        )
        if not root_id or not dispatch_id:
            raise ValueError("room_partner requires an active Room turn")
        return root_id, dispatch_id

    def _require_open_root(self, source: Mapping[str, object], root_id: str) -> None:
        cancelled = getattr(self.room_turns, "is_cancelled", None)
        if callable(cancelled) and cancelled(str(source["sessionId"]), root_id):
            raise ValueError("Room Root stopped before work admission")
        if self.room_events.has_projection(f"room-terminal-result:{source['roomId']}:{root_id}"):
            raise ValueError("Room Root closed by its formal result")

    @staticmethod
    def _active_room_moderator(
        room: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        """Return the active participant named by the persisted moderator ID."""

        moderator_id = _text(
            room.get("moderatorParticipantId"),
            maximum=320,
        )
        participants = room.get("participants")
        if (
            not moderator_id
            or not isinstance(participants, list)
        ):
            return None
        matches = [
            participant
            for participant in participants
            if (
                isinstance(participant, Mapping)
                and str(participant.get("id") or "") == moderator_id
            )
        ]
        if len(matches) != 1 or str(matches[0].get("status") or "") != "active":
            return None
        return matches[0]


    def _list(self, source: Mapping[str, object]) -> dict[str, object]:
        room = self.rooms.get(str(source["roomId"]))
        root_id, dispatch_id = self._active_root(source)
        active_sessions = {
            str(value)
            for value in self.runtime_status().get(
                "activeSessionIds",
                [],
            )
            if str(value or "").strip()
        }
        partners = []
        for ordinal, value in enumerate(room.get("participants", [])):
            if (
                not isinstance(value, Mapping)
                or str(value.get("status") or "") != "active"
                or str(value.get("id") or "") == str(source["id"])
                or self.pending_removal(str(room["id"]), str(value.get("id") or ""))
            ):
                continue
            partner_session_id = str(value.get("sessionId") or "")
            session = self.sessions.get(partner_session_id)
            partners.append(
                {
                    "participantId": str(value.get("id") or ""),
                    "displayName": str(value.get("displayName") or ""),
                    "celestialName": _room_celestial_name(
                        value,
                        fallback_ordinal=ordinal,
                    ),
                    "collaborationRole": str(
                        value.get("collaborationRole") or "implementer"
                    ),
                    "sessionId": partner_session_id,
                    "modelProfile": str(session.get("modelProfile") or ""),
                    "thinkingLevel": str(session.get("thinkingLevel") or ""),
                    "status": (
                        "busy"
                        if partner_session_id in active_sessions
                        else str(session.get("status") or "idle")
                    ),
                }
            )
        recoverable_work_items: list[dict[str, object]] = []
        if self.room_work is not None:
            for work_item in self.room_work.list(
                room_id=str(room["id"]),
                states=("active",),
                limit=200,
            ):
                if (
                    not isinstance(work_item, Mapping)
                    or str(work_item.get("accountableParticipantId") or "")
                    != str(source["id"])
                ):
                    continue
                blocker = work_item.get("blocker")
                feedback = (
                    str(blocker.get("reviewFeedback") or "").strip()
                    if isinstance(blocker, Mapping)
                    else ""
                )
                review = work_item.get("review")
                if not feedback and isinstance(review, Mapping):
                    feedback = str(review.get("reason") or "").strip()
                if not feedback:
                    continue
                recoverable_work_items.append(
                    {
                        "workItemId": str(work_item.get("id") or ""),
                        "state": "active",
                        "expectedRevision": int(work_item.get("revision") or 0),
                        "currentOwnerParticipantId": str(
                            work_item.get("currentOwnerParticipantId") or ""
                        ),
                        "recommendedOperation": "retry",
                        "reason": feedback,
                    }
                )
        return {
            "schemaVersion": "rag-ime.room-partner-result.v1",
            "operation": "list",
            "roomId": str(room["id"]),
            "rootId": root_id,
            "dispatchId": dispatch_id,
            "partners": partners,
            "recoverableWorkItems": recoverable_work_items,
        }

    def _delegate(
        self,
        source: Mapping[str, object],
        args: Mapping[str, object],
        *,
        tool_call_id: str,
        priority_reserved: bool = False,
        wave_id: str = "",
        phase: str = "",
        parallel_index: int = 0,
        parallel_size: int = 1,
        retry_terminal: bool = False,
        expected_revision: int = 0,
        retry_reason: str = "",
    ) -> dict[str, object]:
        target_id = _required_text(args, "targetParticipantId", maximum=240)
        task = _required_text(args, "task", maximum=8_000)
        expected_output = _required_text(args, "expectedOutput", maximum=1_200)
        criteria = _string_list(
            args.get("acceptanceCriteria"),
            limit=8,
            maximum=320,
        )
        if not criteria:
            raise ValueError("acceptanceCriteria must not be empty")
        requested_work_item_id = _text(
            args.get("workItemId"),
            maximum=240,
        )
        room_id = str(source["roomId"])
        room = self.rooms.get(room_id)
        root_id, parent_dispatch_id = self._active_root(source)
        with getattr(self.room_turns, "lock", nullcontext()):
            self._require_open_root(source, root_id)
        target = self.rooms.participant(target_id)
        if (
            str(target.get("roomId") or "") != room_id
            or str(target.get("status") or "") != "active"
        ):
            raise ValueError("target Partner is not active in this Room")
        if target_id == str(source["id"]):
            raise ValueError("a Partner cannot delegate Room work to itself")
        target_session_id = str(target.get("sessionId") or "")
        existing_record = (
            self.dispatch_store.get_by_tool(
                room_id=room_id,
                root_id=root_id,
                tool_call_id=tool_call_id,
            )
            if self.dispatch_store is not None
            else None
        )
        existing_dispatch_id = (
            str(existing_record.get("childDispatchId") or "")
            if existing_record is not None
            else self._existing_child_dispatch(room_id, root_id, tool_call_id)
        )
        if existing_dispatch_id and (
            existing_record is None
            or str(existing_record.get("status") or "") != "prepared"
        ):
            work_item = self._create_delegated_work(
                room_id=room_id,
                root_id=root_id,
                topic_id=str(room.get("activeTopicId") or ""),
                tool_call_id=tool_call_id,
                source=source,
                target=target,
                task=task,
                expected_output=expected_output,
                acceptance_criteria=criteria,
                requested_work_item_id=requested_work_item_id,
                retry_terminal=retry_terminal,
                expected_revision=expected_revision,
                retry_reason=retry_reason,
            )
            if (
                existing_record is not None
                and str(existing_record.get("workItemId") or "")
                != str(work_item.get("id") or "")
            ):
                raise ValueError(
                    "Room delegate idempotency key was reused for a different WorkItem"
                )
            result = self._delegate_receipt(
                room_id=room_id,
                root_id=root_id,
                child_dispatch_id=existing_dispatch_id,
                target=target,
                work_item=work_item,
                record=existing_record,
                idempotent_replay=True,
            )
            if wave_id:
                result.update(
                    {
                        "waveId": wave_id,
                        "phase": phase,
                        "parallelIndex": parallel_index,
                        "parallelSize": parallel_size,
                    }
                )
            return result

        if self.pending_removal(room_id, target_id):
            raise ValueError("target Partner is pending Room removal")
        if any(
            session_id == target_session_id
            for _participant, session_id, _turn_id in self.room_turns.turn_targets(
                root_id,
                resolve=lambda value: self.rooms.participant_for_session(
                    value,
                    active_only=False,
                ),
            )
        ):
            raise ValueError("target Partner is already active in this Room turn")

        def ensure_active(session_id: str) -> None:
            latest = self.rooms.participant_for_session(
                session_id,
                active_only=True,
            )
            if (latest is None or str(latest.get("id") or "") != target_id
                or self.pending_removal(room_id, target_id)):
                raise ValueError("target Partner changed or is pending removal before dispatch")

        priority_reservation = None
        if not priority_reserved:
            try:
                priority_reservation = self.room_turns.hold_priority_if_idle(
                    [target_session_id],
                    ensure_available=ensure_active,
                )
            except RoomSessionBusyError:
                raise ValueError("target Partner is currently busy") from None
        dispatch_started = False
        turn_bound = False
        try:
            if not priority_reserved:
                if not self.room_target_idle(
                    target_session_id,
                    allow_user_priority=True,
                ):
                    raise ValueError("target Partner is currently busy")

            decision = self.rooms.plan_routes(
                room_id,
                task,
                requested_participant_ids=[target_id],
                conversation_only=False,
            )[0]
            child_dispatch_id = existing_dispatch_id or (
                "room-child:"
                + str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"rag-ime:{room_id}:{root_id}:{tool_call_id}",
                    )
                )
            )
            # Admission and binding commit against the same original Root fence
            # as formal result publication and Stop. Native Prompt stays outside.
            with getattr(self.room_turns, "lock", nullcontext()):
                if self._active_root(source) != (root_id, parent_dispatch_id):
                    raise ValueError("Room work lost its original active Room turn")
                self._require_open_root(source, root_id)
                work_item = self._create_delegated_work(
                    room_id=room_id,
                    root_id=root_id,
                    topic_id=str(room.get("activeTopicId") or ""),
                    tool_call_id=tool_call_id,
                    source=source,
                    target=target,
                    task=task,
                    expected_output=expected_output,
                    acceptance_criteria=criteria,
                    requested_work_item_id=requested_work_item_id,
                    retry_terminal=retry_terminal,
                    expected_revision=expected_revision,
                    retry_reason=retry_reason,
                )
                if self.dispatch_store is not None:
                    existing_record = self.dispatch_store.register(
                        child_dispatch_id=child_dispatch_id,
                        room_id=room_id,
                        root_id=root_id,
                        parent_dispatch_id=parent_dispatch_id,
                        tool_call_id=tool_call_id,
                        source_participant_id=str(source["id"]),
                        source_session_id=str(source["sessionId"]),
                        target_participant_id=target_id,
                        target_session_id=target_session_id,
                        work_item_id=str(work_item["id"]),
                    )
            decision.update(
                {
                    # A Partner Tool dispatch is an explicit coordinator
                    # delegation, not a fresh invitation from the user.  Keep the
                    # distinction in the public route event so Room projections do
                    # not attribute this child Session to the user.
                    "reason": "partner_delegate",
                    "rootId": root_id,
                    "dispatchId": child_dispatch_id,
                    "targetSessionId": target_session_id,
                    "parentDispatchId": parent_dispatch_id,
                    "child": True,
                    "toolCallId": tool_call_id,
                    **({"waveId": wave_id} if wave_id else {}),
                    **({"phaseName": phase} if phase else {}),
                    "parallelIndex": parallel_index,
                    "parallelSize": parallel_size,
                    **(
                        {
                            "workItemId": str(work_item["id"]),
                            "workItemState": str(work_item.get("state") or ""),
                        }
                        if work_item
                        else {}
                    ),
                }
            )
            topic_id = str(room.get("activeTopicId") or "")
            self.room_events.publish(
                room_id=room_id,
                event_type="route_decision",
                payload=decision,
                turn_id=root_id,
                participant_id=target_id,
                source_session_id=target_session_id,
                topic_id=topic_id,
            )
            self.room_events.publish(
                room_id=room_id,
                event_type="participant_activity",
                payload={
                    "activityKind": "child",
                    "phase": "started",
                    "parentParticipantId": str(source["id"]),
                    "targetParticipantId": target_id,
                    "parentDispatchId": parent_dispatch_id,
                    "childDispatchId": child_dispatch_id,
                    "toolCallId": tool_call_id,
                    "task": task[:1_200],
                    "expectedOutput": expected_output,
                    "acceptanceCriteria": criteria,
                    **({"workItem": dict(work_item)} if work_item else {}),
                    **({"waveId": wave_id} if wave_id else {}),
                    **({"phaseName": phase} if phase else {}),
                    "parallelIndex": parallel_index,
                    "parallelSize": parallel_size,
                },
                turn_id=root_id,
                participant_id=str(source["id"]),
                source_session_id=str(source["sessionId"]),
                topic_id=topic_id,
            )
            turn_bound = True
            self.begin_room_turn(
                target_session_id,
                root_id,
                topic_id,
                dispatch_id=child_dispatch_id,
                child=True,
            )
            previous_accepted_turn_id = str(work_item.get("acceptedTurnId") or "")
            work_claimed = False
            if self.room_work is not None and previous_accepted_turn_id != root_id:
                work_item = self.room_work.claim_dispatch(
                    str(work_item["id"]),
                    room_id=room_id,
                    owner_participant_id=target_id,
                    assignment_key=str(work_item["assignmentKey"]),
                    previous_accepted_turn_id=previous_accepted_turn_id,
                    room_turn_id=root_id,
                )
                work_claimed = True
            unread = self.rooms.unread_public_messages(
                room_id,
                target_id,
                topic_id=topic_id,
                exclude_turn_id=root_id,
                limit=12,
            )
            task_message = _partner_task_message(
                source=source,
                task=task,
                expected_output=expected_output,
                acceptance_criteria=criteria,
            )
            dispatch_started = True
            dispatched = self.room_dispatch.dispatch_target(
                room=room,
                target=target,
                decision=decision,
                message=task_message,
                room_turn_id=root_id,
                topic_id=topic_id,
                unread=unread,
                work_item=work_item or None,
                attachment_ids=(),
                release_priority_on_exit=False,
            )
            if dispatched.get("accepted") is not True:
                cancelled = (
                    dispatched.get("cancelled") is True
                    or str(dispatched.get("status") or "") == "cancelled"
                )
                self._fail_delegated_work(
                    work_item,
                    source=source,
                    target=target,
                    root_id=root_id,
                    previous_accepted_turn_id=previous_accepted_turn_id,
                    work_claimed=work_claimed,
                    reason=str(dispatched.get("error") or "Partner rejected task"),
                    cancelled=cancelled,
                )
                if self.dispatch_store is not None:
                    self.dispatch_store.settle(
                        child_dispatch_id,
                        status="cancelled" if cancelled else "failed",
                        result="",
                        completion_source=(
                            "dispatch_cancelled"
                            if cancelled
                            else "dispatch_rejected"
                        ),
                        error=str(dispatched.get("error") or "Partner rejected task"),
                    )
                raise RuntimeError(str(dispatched.get("error") or "Partner rejected task"))
            record = (
                self.dispatch_store.mark_dispatched(
                    child_dispatch_id,
                    target_session_turn_id=str(
                        dispatched.get("sessionTurnId") or ""
                    ),
                )
                if self.dispatch_store is not None
                else None
            )
            result = self._delegate_receipt(
                room_id=room_id,
                root_id=root_id,
                child_dispatch_id=child_dispatch_id,
                target=target,
                work_item=work_item,
                record=record,
                idempotent_replay=False,
            )
            if wave_id:
                result.update(
                    {
                        "waveId": wave_id,
                        "phase": phase,
                        "parallelIndex": parallel_index,
                        "parallelSize": parallel_size,
                    }
                )
            return result
        except Exception:
            if turn_bound and not dispatch_started:
                self.cancel_room_turn(target_session_id, root_id)
            raise
        finally:
            if not priority_reserved:
                self.room_turns.release_priority_session(target_session_id, reservation=priority_reservation)

    def _retry(
        self,
        source: Mapping[str, object],
        args: Mapping[str, object],
        *,
        tool_call_id: str,
    ) -> dict[str, object]:
        with getattr(self.room_turns, "lock", nullcontext()):
            root_id, _ = self._active_root(source)
            self._require_open_root(source, root_id)
        if self.room_work is None:
            raise ValueError("Room WorkItem retry is unavailable")
        work_item_id = _required_text(args, "workItemId", maximum=240)
        reason = _required_text(args, "reason", maximum=2_000)
        expected_revision = _expected_revision(args.get("expectedRevision"))
        existing = self.room_work.get(
            work_item_id,
            room_id=str(source["roomId"]),
        )
        target_id = _text(args.get("targetParticipantId"), maximum=240) or str(
            existing.get("currentOwnerParticipantId") or ""
        )
        if not target_id:
            raise ValueError(
                "retry requires targetParticipantId when the WorkItem has no current owner"
            )
        if str(existing.get("currentOwnerParticipantId") or "") == target_id:
            target = self.rooms.participant(target_id)
            target_session_id = str(target.get("sessionId") or "")
            if (
                target_session_id
                and str(self.sessions.get(target_session_id).get("status") or "")
                == "faulted"
            ):
                if self.recover_faulted_session is None:
                    raise RuntimeError(
                        "faulted Room Partner Session recovery is unavailable"
                    )
                self.recover_faulted_session(target_session_id)
        prior_dispatch = (
            self.dispatch_store.get_by_work(work_item_id)
            if self.dispatch_store is not None
            else {}
        )
        returned_revision = (
            str(existing.get("state") or "") == "active"
            and str(prior_dispatch.get("status") or "") == "returned"
        )
        return self._delegate(
            source,
            {
                "targetParticipantId": target_id,
                "task": str(existing.get("objective") or ""),
                "expectedOutput": str(existing.get("expectedOutput") or ""),
                "acceptanceCriteria": list(
                    existing.get("acceptanceCriteria") or []
                ),
                "workItemId": work_item_id,
            },
            tool_call_id=tool_call_id,
            retry_terminal=not returned_revision,
            expected_revision=expected_revision,
            retry_reason=reason,
        )

    @staticmethod
    def _delegate_receipt(
        *,
        room_id: str,
        root_id: str,
        child_dispatch_id: str,
        target: Mapping[str, object],
        work_item: Mapping[str, object],
        record: Mapping[str, object] | None,
        idempotent_replay: bool,
    ) -> dict[str, object]:
        dispatch_status = str((record or {}).get("status") or "dispatched")
        return {
            "schemaVersion": "rag-ime.room-partner-result.v1",
            "operation": "delegate",
            "roomId": room_id,
            "rootId": root_id,
            "childDispatchId": child_dispatch_id,
            "workItemId": str(work_item.get("id") or ""),
            "participantId": str(target["id"]),
            "displayName": str(target.get("displayName") or ""),
            "status": (
                "accepted"
                if dispatch_status in {"prepared", "dispatched"}
                else dispatch_status
            ),
            "dispatchStatus": dispatch_status,
            "result": str((record or {}).get("result") or "")[:16_000],
            "idempotentReplay": idempotent_replay,
            "workItem": dict(work_item),
            **({"dispatch": dict(record)} if record is not None else {}),
        }

    def _wait_for_child(
        self,
        *,
        room_id: str,
        root_id: str,
        child_dispatch_id: str,
        target: Mapping[str, object],
        source: Mapping[str, object],
        timeout_seconds: int,
        idempotent_replay: bool,
        work_item: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if self.dispatch_store is None:
            raise ValueError("Room Partner dispatch ledger is unavailable")
        record = self.dispatch_store.wait(
            child_dispatch_id,
            timeout_seconds=timeout_seconds,
        )
        current_work = (
            self.room_work.get(
                str(record["workItemId"]),
                room_id=room_id,
            )
            if self.room_work is not None
            else work_item or {}
        )
        return {
            "schemaVersion": "rag-ime.room-partner-result.v1",
            "operation": "wait",
            "roomId": room_id,
            "rootId": root_id,
            "childDispatchId": child_dispatch_id,
            "workItemId": str(record["workItemId"]),
            "participantId": str(target["id"]),
            "displayName": str(target.get("displayName") or ""),
            "status": str(record["status"]),
            "result": str(record.get("result") or "")[:16_000],
            "timedOut": str(record["status"]) in {"prepared", "dispatched"},
            "idempotentReplay": idempotent_replay,
            "dispatch": dict(record),
            "workItem": dict(current_work),
        }

    def _create_delegated_work(
        self,
        *,
        room_id: str,
        root_id: str,
        topic_id: str,
        tool_call_id: str,
        source: Mapping[str, object],
        target: Mapping[str, object],
        task: str,
        expected_output: str,
        acceptance_criteria: list[str],
        requested_work_item_id: str = "",
        retry_terminal: bool = False,
        expected_revision: int = 0,
        retry_reason: str = "",
    ) -> dict[str, object]:
        if self.room_work is None:
            # Compatibility for isolated adapters. The installed AgentService
            # always supplies the durable Room responsibility ledger.
            return {}
        if requested_work_item_id:
            existing = self.room_work.get(
                requested_work_item_id,
                room_id=room_id,
            )
            if retry_terminal:
                existing = self.room_work.retry(
                    requested_work_item_id,
                    actor_participant_id=str(source["id"]),
                    current_owner_participant_id=str(target["id"]),
                    expected_revision=expected_revision,
                    reason=retry_reason,
                )
                self._publish_work_activity(
                    existing,
                    phase="retried",
                    actor=source,
                )
            if (
                str(existing.get("state") or "") not in {"active", "blocked"}
                or str(existing.get("currentOwnerParticipantId") or "")
                != str(target["id"])
                or str(existing.get("accountableParticipantId") or "")
                != str(source["id"])
                or str(existing.get("objective") or "") != task
                or str(existing.get("expectedOutput") or "") != expected_output
                or list(existing.get("acceptanceCriteria") or [])
                != acceptance_criteria
            ):
                raise ValueError(
                    "Room revision delegate does not match the returned WorkItem"
                )
            return dict(existing)
        client_message_id = f"room-partner:{root_id}:{tool_call_id}"
        for existing in self.room_work.list(
            room_id=room_id,
            states=(),
            limit=200,
        ):
            if (
                str(existing.get("createdByParticipantId") or "")
                == str(source["id"])
                and str(existing.get("clientMessageId") or "")
                == client_message_id
            ):
                if (
                    str(existing.get("currentOwnerParticipantId") or "")
                    != str(target["id"])
                    or str(existing.get("accountableParticipantId") or "")
                    != str(source["id"])
                    or str(existing.get("objective") or "") != task
                    or str(existing.get("expectedOutput") or "")
                    != expected_output
                    or list(existing.get("acceptanceCriteria") or [])
                    != acceptance_criteria
                ):
                    raise ValueError(
                        "Room delegate idempotency key was reused for a different WorkItem"
                    )
                return dict(existing)
        work = self.room_work.create(
            room_id=room_id,
            objective=task,
            expected_output=expected_output,
            current_owner_participant_id=str(target["id"]),
            created_by_participant_id=str(source["id"]),
            accountable_participant_id=str(source["id"]),
            client_message_id=client_message_id,
            topic_id=topic_id,
            root_turn_id=root_id,
            acceptance_criteria=acceptance_criteria,
            state="active",
            depth=1,
        )
        self._publish_work_activity(work, phase="assigned", actor=source)
        return dict(work)

    def _settle_delegated_work(
        self,
        work_item: Mapping[str, object] | None,
        *,
        phase: str,
        result: str,
        child_dispatch_id: str,
        source: Mapping[str, object],
        target: Mapping[str, object],
        proposed_operability: str = "",
        proposed_requirement: str = "",
    ) -> Mapping[str, object] | None:
        if self.room_work is None or not work_item or not work_item.get("id"):
            return work_item
        current = self.room_work.get(
            str(work_item["id"]),
            room_id=str(work_item["roomId"]),
        )
        state = str(current.get("state") or "")
        if phase == "completed":
            document: Mapping[str, object] | None = None
            document_sync: dict[str, object] | None = None
            if self.work_document_for_authority is not None:
                try:
                    document = self.work_document_for_authority(
                        "room_work_item",
                        str(current["id"]),
                    )
                except Exception as document_error:
                    document_sync = _document_sync_receipt(
                        work_item=current,
                        child_dispatch_id=child_dispatch_id,
                        state="failed",
                        reason=_public_error(document_error),
                    )
                else:
                    document_revision = _document_revision(document)
                    if document is None:
                        document_sync = _document_sync_receipt(
                            work_item=current,
                            child_dispatch_id=child_dispatch_id,
                            state="pending",
                            reason="尚未找到可用的 active WorkDocument",
                        )
                    elif document_revision < 2:
                        document_sync = _document_sync_receipt(
                            work_item=current,
                            child_dispatch_id=child_dispatch_id,
                            state="pending",
                            reason="WorkDocument 尚未完成交付内容修订",
                            document=document,
                        )
                    else:
                        document_sync = _document_sync_receipt(
                            work_item=current,
                            child_dispatch_id=child_dispatch_id,
                            state="applied",
                            document=document,
                        )
            if state in {"active", "blocked"}:
                evidence_refs = [child_dispatch_id]
                if document is not None:
                    evidence_refs.append(
                        "workdoc:"
                        f"{document.get('documentId')}@"
                        f"{document.get('documentRevision')}"
                    )
                if document_sync is not None and document_sync["state"] != "applied":
                    evidence_refs.extend(
                        [
                            str(document_sync["traceId"]),
                            f"document-sync:{document_sync['state']}:{current['id']}",
                        ]
                    )
                summary = (
                    result[:4_000]
                    or "Partner Session completed the delegated WorkItem."
                )
                # The typed work_result post carries the Partner's structured
                # proposals; the Work ledger infers from FAILED/UNVERIFIED
                # prose markers only when both axes are absent.
                current = self.room_work.submit(
                    str(target["sessionId"]),
                    {
                        "workId": current["id"],
                        "resultSummary": summary,
                        "evidenceRefs": evidence_refs,
                        "proposedOperabilityVerdict": proposed_operability,
                        "proposedRequirementVerdict": proposed_requirement,
                    },
                )
                self._publish_work_activity(
                    current,
                    phase="submitted",
                    actor=target,
                    document_sync=document_sync,
                )
            return current
        if state in {"active", "blocked"}:
            current = self.room_work.escalate(
                str(target["sessionId"]),
                {
                    "workId": current["id"],
                    "reason": result[:2_000] or f"Partner dispatch ended as {phase}",
                    "nextStep": "由 Room 伙伴缩小范围后重新分派，或明确保留为未解决项。",
                },
            )
            self._publish_work_activity(current, phase="failed", actor=target)
        return current

    def _fail_delegated_work(
        self,
        work_item: Mapping[str, object],
        *,
        source: Mapping[str, object],
        target: Mapping[str, object],
        root_id: str,
        previous_accepted_turn_id: str,
        work_claimed: bool,
        reason: str,
        cancelled: bool = False,
    ) -> None:
        if self.room_work is None or not work_item.get("id"):
            return
        current = dict(work_item)
        if work_claimed:
            current = self.room_work.fail_dispatch(
                str(current["id"]),
                room_id=str(current["roomId"]),
                actor_participant_id=str(source["id"]),
                room_turn_id=root_id,
                previous_accepted_turn_id=previous_accepted_turn_id,
                reason=reason,
            )
        if (
            not cancelled
            and str(current.get("state") or "") in {"active", "blocked"}
        ):
            current = self.room_work.escalate(
                str(target["sessionId"]),
                {
                    "workId": current["id"],
                    "reason": reason,
                    "nextStep": "检查目标 Session 后重新分派。",
                },
            )
        self._publish_work_activity(
            current,
            phase="aborted" if cancelled else "failed",
            actor=source,
        )

    def _publish_work_activity(
        self,
        work: Mapping[str, object],
        *,
        phase: str,
        actor: Mapping[str, object],
        document_sync: Mapping[str, object] | None = None,
    ) -> None:
        if self.publish_room_work_activity is None or not work:
            return
        self.publish_room_work_activity(
            work,
            phase=phase,
            actor=actor,
            **(
                {"document_sync": dict(document_sync)}
                if document_sync is not None
                else {}
            ),
        )

    def _existing_child_dispatch(
        self,
        room_id: str,
        root_id: str,
        tool_call_id: str,
    ) -> str:
        for event in self.rooms.list_events(
            room_id,
            after_sequence=0,
            limit=2_000,
        ):
            if (
                str(event.get("turnId") or "") != root_id
                or str(event.get("eventType") or "") != "participant_activity"
            ):
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            if (
                payload.get("activityKind") == "child"
                and payload.get("phase") == "started"
                and str(payload.get("toolCallId") or "") == tool_call_id
            ):
                return str(payload.get("childDispatchId") or "")
        return ""

    def _post(
        self,
        source: Mapping[str, object],
        args: Mapping[str, object],
        *,
        tool_call_id: str,
        source_loop_id: str = "",
    ) -> dict[str, object]:
        content = _required_text(args, "content", maximum=8_000)
        kind = _text(args.get("kind"), maximum=40) or "progress"
        if kind not in {
            "progress",
            "result",
            "work_result",
            "review_result",
            "handoff",
            "wait",
            "blocked",
        }:
            raise ValueError("room_partner post kind is invalid")
        work_result_proposal: dict[str, str] = {}
        if kind == "work_result":
            proposed_operability = _text(
                args.get("proposedOperabilityVerdict"),
                maximum=40,
            )
            proposed_requirement = _text(
                args.get("proposedRequirementVerdict"),
                maximum=40,
            )
            if (
                proposed_operability not in {"passed", "failed", "unverified"}
                or proposed_requirement
                not in {"satisfied", "not_satisfied", "unverified"}
            ):
                raise ValueError(
                    "post kind=work_result requires proposedOperabilityVerdict "
                    "(passed/failed/unverified) and proposedRequirementVerdict "
                    "(satisfied/not_satisfied/unverified) stating the Partner's "
                    "honest submission verdicts"
                )
            work_result_proposal = {
                "proposedOperabilityVerdict": proposed_operability,
                "proposedRequirementVerdict": proposed_requirement,
            }
        root_id, dispatch_id = self._active_root(source)
        room = self.rooms.get(str(source["roomId"]))
        author_participant_id = str(source["id"])
        existing_result_event: Mapping[str, object] | None = None
        existing_result_post: Mapping[str, object] | None = None
        existing_result_event_id = ""
        idempotent_replay = False
        if kind == "result":
            moderator = (
                self._active_room_moderator(room)
                if isinstance(room, Mapping)
                else None
            )
            moderator_participant_id = (
                str(moderator.get("id") or "")
                if moderator is not None
                else ""
            )
            if (
                not moderator_participant_id
                or str(source.get("id") or "") != moderator_participant_id
            ):
                raise ValueError("only the Room Facilitator can publish kind=result")
            author_participant_id = moderator_participant_id
            root_has_typed_result = self._root_has_typed_result(
                {"roomId": str(room["id"]), "rootId": root_id}
            )
            if root_has_typed_result:
                existing_result_event = self._retained_root_typed_result(
                    room_id=str(room["id"]),
                    root_turn_id=root_id,
                )
                existing_payload = (
                    existing_result_event.get("payload")
                    if existing_result_event is not None
                    else None
                )
                existing_payload = (
                    existing_payload
                    if isinstance(existing_payload, Mapping)
                    else {}
                )
                existing_post = existing_payload.get("post")
                existing_result_post = (
                    existing_post
                    if isinstance(existing_post, Mapping)
                    else None
                )
                existing_result_event_id = str(
                    existing_result_event.get("eventId") or ""
                ) if existing_result_event is not None else ""
                existing_idempotency_key = (
                    str(existing_result_post.get("idempotencyKey") or "")
                    if existing_result_post is not None
                    else ""
                )
                if (
                    existing_result_event is None
                    or existing_result_post is None
                    or not existing_result_event_id
                    or not existing_idempotency_key
                    or existing_idempotency_key != tool_call_id
                ):
                    raise ValueError("Room Root already has a typed result")
                idempotent_replay = True
            if not idempotent_replay:
                accepted_work = self._accepted_root_work(
                    room_id=str(room["id"]),
                    root_turn_id=root_id,
                    facilitator_participant_id=moderator_participant_id,
                )
                if accepted_work is None:
                    raise ValueError(
                        "Room result requires every WorkItem to be explicitly "
                        "accepted with passed/satisfied evidence"
                    )
        post_id = (
            str(existing_result_post.get("postId") or "")
            if idempotent_replay and existing_result_post is not None
            else f"room-post:{tool_call_id}"
        )
        created_at_ms = int(time.time() * 1_000)
        post = {
            "schemaVersion": "wisdom-weasel.room-post.v2",
            "postId": post_id,
            "roomId": str(room["id"]),
            "rootId": root_id,
            "generation": 0,
            "dispatchId": dispatch_id,
            "authorActorRef": author_participant_id,
            "kind": kind,
            "visibility": "room",
            "content": content,
            "idempotencyKey": tool_call_id,
            "publicationSource": {
                "kind": "room_post",
                "ref": tool_call_id,
            },
            "createdAtMs": created_at_ms,
            **(
                {
                    "taskId": _text(
                        args.get("workItemId"),
                        maximum=320,
                    )
                }
                if _text(args.get("workItemId"), maximum=320)
                else {}
            ),
            **(
                {"workResult": work_result_proposal}
                if work_result_proposal
                else {}
            ),
        }
        validate_contract(post, "room-post.v2.json")
        latest_runtime_turn_id = getattr(
            self.sessions,
            "latest_runtime_turn_id",
            None,
        )
        source_turn_id = (
            str(latest_runtime_turn_id(str(source["sessionId"])) or "")
            if callable(latest_runtime_turn_id)
            else ""
        )
        publish_values = {
            "room_id": str(room["id"]),
            "event_type": "room_post",
            "payload": {
                "post": post,
                **(
                    {"sourceTurnId": source_turn_id}
                    if source_turn_id
                    else {}
                ),
                **(
                    {"sourceLoopId": source_loop_id[:240]}
                    if source_loop_id
                    else {}
                ),
            },
            "turn_id": root_id,
            "participant_id": author_participant_id,
            "source_session_id": str(source["sessionId"]),
            "topic_id": self.room_topic_for_turn(root_id),
        }
        published = True
        if kind == "result":
            # Stop records its intent and captures the original fan-out under
            # this registry fence. A still-pending native drain must not allow
            # a late Facilitator post to declare that cancelled Root complete.
            with getattr(self.room_turns, "lock", nullcontext()):
                cancelled = getattr(self.room_turns, "is_cancelled", None)
                if callable(cancelled) and cancelled(str(source["sessionId"]), root_id):
                    raise ValueError("Room Root stopped before result publication")
                if self._active_root(source) != (root_id, dispatch_id):
                    raise ValueError("Room result lost its original active Room turn")
                projection_key = f"room-terminal-result:{room['id']}:{root_id}"
                has_projection = getattr(self.room_events, "has_projection", None)
                if callable(has_projection) and has_projection(projection_key):
                    published = False
                elif idempotent_replay:
                    published = False
                elif self._root_has_typed_result(
                    {"roomId": str(room["id"]), "rootId": root_id}
                ):
                    raise ValueError("Room Root already has a typed result")
                else:
                    if self._accepted_root_work(room_id=str(room["id"]), root_turn_id=root_id,
                                                facilitator_participant_id=author_participant_id) is None:
                        raise ValueError("Room result requires every WorkItem to be explicitly accepted with passed/satisfied evidence")
                    self.room_events.publish_projection(
                        projection_key=projection_key,
                        projection_guard=lambda: self._require_accepted_root_work(
                            room_id=str(room["id"]), root_turn_id=root_id,
                            facilitator_participant_id=author_participant_id),
                        **publish_values,
                    )
        else:
            self.room_events.publish(**publish_values)
        return {
            "schemaVersion": "rag-ime.room-partner-result.v1",
            "operation": "post",
            "roomId": str(room["id"]),
            "rootId": root_id,
            "postId": post_id,
            "kind": kind,
            "published": published,
            "settledWorkItems": [],
            **(
                {
                    "eventId": existing_result_event_id,
                    "idempotentReplay": True,
                }
                if idempotent_replay
                else {}
            ),
        }


def _terminal_result_content(
    work_items: list[Mapping[str, object]],
) -> str:
    lines = [
        "终态收据",
        (
            "主管 Agent 已完成显式双轴验收；Runtime 根据持久化验收账本投影"
            "这一终态，未解析普通回复，也未代替主管接受 WorkItem。"
        ),
        "",
    ]
    for item in work_items:
        review = item.get("review")
        review = review if isinstance(review, Mapping) else {}
        evidence = review.get("evidenceRefs")
        evidence = evidence if isinstance(evidence, list) else []
        evidence_text = "；".join(
            _text(value, maximum=240)
            for value in evidence[:6]
            if _text(value, maximum=240)
        )
        lines.extend(
            [
                f"- {_text(item.get('objective'), maximum=320)}",
                (
                    "  运行可操作性 "
                    f"{_text(review.get('operabilityVerdict'), maximum=40)}；"
                    "需求满足 "
                    f"{_text(review.get('requirementVerdict'), maximum=40)}"
                ),
                f"  {_text(item.get('resultSummary'), maximum=700)}",
                f"  证据：{evidence_text}",
            ]
        )
    lines.extend(
        [
            "",
            (
                "实现、测试、构建、浏览器运行与未验证边界仍以各 WorkItem 的"
                "结果摘要和证据引用为准。"
            ),
        ]
    )
    return "\n".join(lines)[:8_000]


def _partner_task_message(
    *,
    source: Mapping[str, object],
    task: str,
    expected_output: str,
    acceptance_criteria: list[str],
) -> str:
    lines = [
        f"来自 Room 伙伴 {source.get('displayName') or 'Partner'} 的 WorkItem：",
        task,
    ]
    if expected_output:
        lines.extend(["", f"预期输出：{expected_output}"])
    if acceptance_criteria:
        lines.extend(
            ["", "验收条件：", *[f"- {value}" for value in acceptance_criteria]]
        )
    lines.extend(
        [
            "",
            "在编辑前判断是否存在至少两个独立、非重叠的支持或验证面。若实际工具目录提供 agents 且有真实并发收益，加载 orchestrate-session，一次批量委派有界子任务，父 Agent 保留自己的工作线；否则当前 Session 直接完成。不要把整个 WorkItem 再转交给子 Agent。",
            "依赖本 WorkItem 尚未生成文件、构建或运行产物的私有审核任务，必须等产物真实存在后再派发；不要让审核者和生产者在同一波并发后再把‘文件尚不存在’误报为缺陷。",
            "",
            "直接完成当前 WorkItem 并返回有界结果；需要其他伙伴信息时直接使用 peer @ 通道，不要让发送者代为转发。Root 伙伴负责最终汇合。",
        ]
    )
    return "\n".join(lines)[:12_000]


def _required_text(
    value: Mapping[str, object],
    key: str,
    *,
    maximum: int,
) -> str:
    text = _text(value.get(key), maximum=maximum)
    if not text:
        raise ValueError(f"{key} must not be empty")
    return text


def _text(value: object, *, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _string_list(value: object, *, limit: int, maximum: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        text = _text(item, maximum=maximum)
        if text and text not in result:
            result.append(text)
    return result


def _integer(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _expected_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2:
        raise ValueError("expectedRevision must be an integer between 0 and 2")
    return value


def _public_error(error: BaseException) -> str:
    return " ".join(str(error).split())[:240] or error.__class__.__name__


def _document_revision(document: Mapping[str, object] | None) -> int:
    if not isinstance(document, Mapping):
        return 0
    try:
        return max(0, int(document.get("documentRevision") or 0))
    except (TypeError, ValueError):
        return 0


def _document_sync_receipt(
    *,
    work_item: Mapping[str, object],
    child_dispatch_id: str,
    state: str,
    reason: str = "",
    document: Mapping[str, object] | None = None,
) -> dict[str, object]:
    normalized_state = state if state in {"applied", "pending", "failed"} else "failed"
    work_id = _text(work_item.get("id"), maximum=240)
    trace_id = f"trace:room-document-sync:{_text(child_dispatch_id, maximum=200)}"
    receipt: dict[str, object] = {
        "state": normalized_state,
        "attemptCount": 1,
        "retryable": False,
        "authorityKind": "room_work_item",
        "authorityId": work_id,
        "traceId": trace_id,
        "publicNotice": (
            "WorkDocument 同步已记录一次；不阻断真实工作结果，也不要仅因文档同步失败自动 retry/return。"
            if normalized_state != "applied"
            else "WorkDocument 已同步。"
        ),
    }
    if isinstance(document, Mapping):
        document_id = _text(document.get("documentId"), maximum=240)
        if document_id:
            receipt["documentId"] = document_id
        receipt["documentRevision"] = _document_revision(document)
    normalized_reason = " ".join(str(reason).split())[:240] if reason else ""
    if normalized_reason:
        receipt["reason"] = normalized_reason
    return receipt


def _recoverable_runtime_host_failure(error: str) -> bool:
    normalized = " ".join(str(error or "").lower().split())
    return any(
        marker in normalized
        for marker in _RECOVERABLE_RUNTIME_HOST_FAILURE_MARKERS
    )
