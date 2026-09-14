from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Protocol

from rag_ime.agent_command_receipts import AgentCommandReceiptFailed
from rag_ime.rooms.store import AgentRoomStore, AgentRoomEventHub
from rag_ime.rooms.work import AgentRoomWorkStore
from rag_ime.rooms.partner_dispatch_store import AgentRoomPartnerDispatchStore
from rag_ime.rooms.turn_registry import RoomSessionBusyError, RoomTurnRegistry


ROOM_CONTEXT_UNREAD_MESSAGE_LIMIT = 12


class RoomTargetIdle(Protocol):
    def __call__(
        self, session_id: str, *, allow_user_priority: bool = False
    ) -> bool: ...


class RoomEvidenceRecorder(Protocol):
    def __call__(
        self,
        *,
        room_id: str,
        room_event: Mapping[str, object],
        text: str,
        role_id: str,
        session_id: str,
        event_type: str,
        accepted: bool,
    ) -> dict[str, object]: ...


class ParticipantPromptBuilder(Protocol):
    def __call__(
        self,
        room: Mapping[str, object],
        target: Mapping[str, object],
        message: str,
        *,
        recent_messages: Sequence[Mapping[str, object]],
        omitted_message_count: int,
        work_item: Mapping[str, object] | None,
    ) -> str: ...


class RoomSessionDispatchService:
    """Route explicit Room messages through ordinary participant Pi Sessions."""

    def __init__(
        self,
        *,
        rooms: AgentRoomStore,
        room_work: AgentRoomWorkStore,
        room_events: AgentRoomEventHub,
        room_turns: RoomTurnRegistry,
        room_partner_dispatches: AgentRoomPartnerDispatchStore,
        context_source_token: object,
        restore_participant_sessions: Callable[[Mapping[str, object]], None],
        guard_session_route: Callable[[str, str], None],
        recover_faulted_session: Callable[[str], None],
        resume_goal_if_paused: Callable[[str], None],
        target_idle: RoomTargetIdle,
        record_room_evidence: RoomEvidenceRecorder,
        accept_turn: Callable[[str, str, str], None],
        prompt: Callable[[str, Mapping[str, object]], dict[str, object]],
        build_participant_prompt: ParticipantPromptBuilder,
        resolve_attachments: Callable[
            [str, Sequence[str], Sequence[str]], list[dict[str, object]]
        ],
        human_actor_provider: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        self.rooms = rooms
        self.room_work = room_work
        self.room_events = room_events
        self.room_turns = room_turns
        self.room_partner_dispatches = room_partner_dispatches
        self._context_source_token = context_source_token
        self._restore_room_participant_sessions = restore_participant_sessions
        self._guard_room_session_route = guard_session_route
        self._recover_faulted_room_session = recover_faulted_session
        self._resume_room_goal_if_paused = resume_goal_if_paused
        self._room_target_idle = target_idle
        self._record_room_evidence_safely = record_room_evidence
        self._accept_room_turn = accept_turn
        self.prompt = prompt
        self.build_participant_prompt = build_participant_prompt
        self.resolve_attachments = resolve_attachments
        self.human_actor_provider = human_actor_provider

    def post_message(
        self,
        room_id: str,
        *,
        message: str,
        client_message_id: str,
        retry_of_root_id: str,
        requested_participant_ids: Sequence[str],
        work_item_id: str,
        attachment_ids: Sequence[str],
    ) -> dict[str, object]:
        route_id = (
            "room.message.execute"
            if str(work_item_id or "").strip()
            else "room.message.conversation"
        )
        return self._post_session_messages(
            room_id,
            message=message,
            client_message_id=client_message_id,
            retry_of_root_id=retry_of_root_id,
            requested_participant_ids=requested_participant_ids,
            work_item_id=work_item_id,
            attachment_ids=attachment_ids,
            route_id=route_id,
        )

    def _post_session_messages(
        self,
        room_id: str,
        *,
        message: str,
        client_message_id: str,
        retry_of_root_id: str,
        requested_participant_ids: Sequence[str],
        work_item_id: str,
        attachment_ids: Sequence[str],
        route_id: str,
    ) -> dict[str, object]:
        room = self.rooms.get(room_id)
        self._restore_room_participant_sessions(room)
        work_item: dict[str, object] | None = None
        authoritative_participant_id = ""
        if retry_of_root_id and not str(work_item_id or "").strip():
            returned_candidates: list[Mapping[str, object]] = []
            for candidate in self.room_work.list(
                room_id=room_id,
                states=("active",),
                limit=200,
            ):
                if (
                    not isinstance(candidate, Mapping)
                    or str(candidate.get("rootTurnId") or "")
                    != retry_of_root_id
                ):
                    continue
                blocker = candidate.get("blocker")
                review = candidate.get("review")
                has_return_feedback = (
                    isinstance(blocker, Mapping)
                    and bool(str(blocker.get("reviewFeedback") or "").strip())
                ) or (
                    isinstance(review, Mapping)
                    and bool(str(review.get("reason") or "").strip())
                )
                if has_return_feedback:
                    returned_candidates.append(candidate)
            if len(returned_candidates) > 1:
                raise ValueError(
                    "retryOfRootId matches multiple returned WorkItems; "
                    "provide workItemId"
                )
            if returned_candidates:
                work_item_id = str(returned_candidates[0].get("id") or "")
                route_id = "room.message.execute"
        if work_item_id:
            work_item, authoritative_participant_id = (
                self.room_work.authoritative_owner(
                    work_item_id,
                    room_id=room_id,
                )
            )
        profiles: dict[str, dict[str, object]] = {}
        for value in room["participants"]:
            if not isinstance(value, Mapping):
                continue
            if str(value.get("status") or "") != "active":
                continue
            # Room routing consumes only explicit mentions, collaboration
            # responsibility and WorkItem ownership. Persona/Role Book is an
            # optional Package and must not be loaded by the core Room path.
            profiles[str(value["id"])] = {
                "tagline": "",
                "summary": "",
                "traits": [],
                "routingTags": [],
                "roleBookRevisionId": "",
            }
        decisions = self.rooms.plan_routes(
            room_id,
            message,
            requested_participant_ids=requested_participant_ids,
            profiles=profiles,
            authoritative_participant_id=authoritative_participant_id,
            # A Room is one lead Session with optional child/partner Sessions,
            # not an automatic broadcast.  Explicit @mentions can still fan
            # out; an unaddressed request always starts one lead turn.
            conversation_only=work_item is None,
        )
        if work_item is not None:
            for decision in decisions:
                decision["workItemId"] = work_item_id
                decision["workItemState"] = str(work_item["state"])
                decision["workItemRevision"] = int(work_item.get("revision") or 0)

        targets = [
            self.rooms.participant(str(decision["targetParticipantId"]))
            for decision in decisions
        ]
        target_session_ids = [str(target["sessionId"]) for target in targets]
        if retry_of_root_id:
            for session_id in target_session_ids:
                self._recover_faulted_room_session(session_id)
        attachment_receipts = self.resolve_attachments(
            room_id,
            target_session_ids,
            attachment_ids,
        )
        if len(set(target_session_ids)) != len(target_session_ids):
            raise RuntimeError("Room routing produced duplicate participant Sessions")
        for session_id in target_session_ids:
            self._guard_room_session_route(
                route_id,
                session_id,
            )
        # The explicit user message carries the resume intent for a paused
        # target Goal. This runs before the Root and user event become
        # durable; wake, partner and Tool Agent dispatches never reach here.
        for session_id in target_session_ids:
            self._resume_room_goal_if_paused(session_id)

        target_by_session_id = {
            session_id: target
            for target, session_id in zip(targets, target_session_ids, strict=True)
        }

        def _ensure_participant_active(session_id: str) -> None:
            target = target_by_session_id[session_id]
            latest_target = self.rooms.participant(str(target["id"]))
            if str(latest_target.get("status") or "") != "active":
                raise ValueError("selected Room participant is no longer active")

        try:
            # ensure_available runs inside the registry lock, so the
            # participant re-check and the priority reservation remain the
            # single critical section they were as inline code.
            self.room_turns.hold_priority_if_idle(
                target_session_ids,
                ensure_available=_ensure_participant_active,
            )
        except RoomSessionBusyError as busy:
            target = target_by_session_id[busy.session_id]
            raise AgentCommandReceiptFailed(
                f"{target.get('displayName') or 'selected Room participant'} "
                "is currently busy",
                client_message_id=client_message_id,
                cause_code="ROOM_PARTICIPANT_BUSY",
            ) from None
        try:
            busy_targets = [
                target
                for target, session_id in zip(targets, target_session_ids, strict=True)
                if not self._room_target_idle(session_id, allow_user_priority=True)
            ]
        except Exception:
            # Status reads can fail before any Root/event is published. Only
            # this request's reservation is ours to release; otherwise every
            # later manual send would mistake the abandoned claim for work.
            self.room_turns.release_priority(target_session_ids)
            raise
        if busy_targets:
            self.room_turns.release_priority(target_session_ids)
            names = "、".join(str(item.get("displayName") or "Agent") for item in busy_targets)
            raise AgentCommandReceiptFailed(
                f"Room participants are currently busy: {names}",
                client_message_id=client_message_id,
                cause_code="ROOM_PARTICIPANT_BUSY",
            )

        room_turn_id = f"room-turn:{uuid.uuid4()}"
        topic_id = str(room.get("activeTopicId") or "")
        for decision, target in zip(decisions, targets, strict=True):
            decision["rootId"] = room_turn_id
            decision["dispatchId"] = f"room-dispatch:{uuid.uuid4()}"
            decision["targetSessionId"] = str(target["sessionId"])
            if work_item is not None:
                decision["attemptId"] = room_turn_id
        try:
            user_event_payload: dict[str, object] = {
                "text": message,
                "targetParticipantIds": [
                    str(decision["targetParticipantId"]) for decision in decisions
                ],
                "dispatches": [
                    {
                        "dispatchId": str(decision["dispatchId"]),
                        "participantId": str(decision["targetParticipantId"]),
                    }
                    for decision in decisions
                ],
            }
            if self.human_actor_provider is not None:
                actor = self.human_actor_provider()
                if actor.get('userId'):
                    user_event_payload['actorUserId'] = str(actor['userId'])
                    user_event_payload['actorDisplayName'] = str(actor.get('displayName') or actor['userId'])
            if client_message_id:
                user_event_payload["clientMessageId"] = client_message_id
            if retry_of_root_id:
                user_event_payload["retryOfRootId"] = retry_of_root_id
            if work_item_id:
                user_event_payload["workItemId"] = work_item_id
            if attachment_receipts:
                user_event_payload["attachmentReceipts"] = attachment_receipts
            user_room_event = self.room_events.publish(
                room_id=room_id,
                event_type="user_message",
                payload=user_event_payload,
                turn_id=room_turn_id,
                topic_id=topic_id,
            )
            timeline_events = [user_room_event]
            self._record_room_evidence_safely(
                room_id=room_id,
                room_event=user_room_event,
                text=message,
                role_id=str(targets[0].get("roleId") or ""),
                session_id=target_session_ids[0],
                event_type="user_message",
                accepted=False,
            )
            for decision, target in zip(decisions, targets, strict=True):
                route_room_event = self.room_events.publish(
                    room_id=room_id,
                    event_type="route_decision",
                    payload=decision,
                    turn_id=room_turn_id,
                    participant_id=str(target["id"]),
                    source_session_id=str(target["sessionId"]),
                    topic_id=topic_id,
                )
                timeline_events.append(route_room_event)
                self.room_turns.begin(
                    str(target["sessionId"]),
                    room_turn_id,
                    topic_id,
                    dispatch_id=str(decision["dispatchId"]),
                    **(
                        {
                            "work_item_id": str(work_item["id"]),
                            "work_item_revision": int(
                                work_item.get("revision") or 0
                            ),
                            "attempt_id": room_turn_id,
                        }
                        if work_item is not None
                        else {}
                    ),
                )
            unread_by_participant = {
                str(target["id"]): self.rooms.unread_public_messages(
                    room_id,
                    str(target["id"]),
                    topic_id=topic_id,
                    exclude_turn_id=room_turn_id,
                    limit=ROOM_CONTEXT_UNREAD_MESSAGE_LIMIT,
                )
                for target in targets
            }
        except Exception:
            for session_id in target_session_ids:
                self.room_turns.cancel(session_id, room_turn_id)
            self.room_turns.release_priority(target_session_ids)
            raise

        work_claimed = False
        previous_accepted_turn_id = ""
        retry_dispatch_id = ""
        try:
            if work_item is not None:
                previous_accepted_turn_id = str(
                    work_item.get("acceptedTurnId") or ""
                )
                work_item = self.room_work.claim_dispatch(
                    str(work_item["id"]),
                    room_id=room_id,
                    owner_participant_id=str(targets[0]["id"]),
                    assignment_key=str(work_item["assignmentKey"]),
                    previous_accepted_turn_id=previous_accepted_turn_id,
                    room_turn_id=room_turn_id,
                    root_turn_id=room_turn_id,
                )
                work_claimed = True
                # A retry submitted from the Room UI still represents the
                # same governed Partner WorkItem.  Register its ordinary Room
                # dispatch in the existing durable Partner ledger so a typed
                # work_result can settle to review and wake the accountable
                # Facilitator exactly like a tool-originated retry.
                retry_dispatch_id = str(decisions[0]["dispatchId"])
                accountable = self.rooms.participant(
                    str(work_item["accountableParticipantId"])
                )
                self.room_partner_dispatches.register(
                    child_dispatch_id=retry_dispatch_id,
                    room_id=room_id,
                    root_id=room_turn_id,
                    parent_dispatch_id=f"room-user-retry:{room_turn_id}",
                    tool_call_id=client_message_id or room_turn_id,
                    source_participant_id=str(accountable["id"]),
                    source_session_id=str(accountable["sessionId"]),
                    target_participant_id=str(targets[0]["id"]),
                    target_session_id=str(targets[0]["sessionId"]),
                    work_item_id=str(work_item["id"]),
                )
        except Exception as exc:
            for decision, target in zip(decisions, targets, strict=True):
                self.room_turns.cancel(str(target["sessionId"]), room_turn_id)
                self.room_events.publish(
                    room_id=room_id,
                    event_type="turn_failed",
                    payload={
                        "rootId": room_turn_id,
                        "dispatchId": decision["dispatchId"],
                        "error": _public_error(exc),
                    },
                    turn_id=room_turn_id,
                    participant_id=str(target["id"]),
                    source_session_id=str(target["sessionId"]),
                    topic_id=topic_id,
                )
            self.room_turns.release_priority(target_session_ids)
            # The compatibility path historically failed synchronously when the
            # authoritative WorkItem changed between route planning and claim.
            # Do not turn that concurrency fence into a superficially successful
            # `accepted=false` response: callers must retry against the new owner.
            raise

        dispatch_results: list[dict[str, object]] = []
        try:
            with ThreadPoolExecutor(
                max_workers=len(targets),
                thread_name_prefix="room-user-dispatch",
            ) as executor:
                futures = {
                    executor.submit(
                        self.dispatch_target,
                        room=room,
                        target=target,
                        decision=decision,
                        message=message,
                        room_turn_id=room_turn_id,
                        topic_id=topic_id,
                        unread=unread_by_participant[str(target["id"])],
                        work_item=work_item,
                        attachment_ids=attachment_ids,
                    ): index
                    for index, (decision, target) in enumerate(
                        zip(decisions, targets, strict=True)
                    )
                }
                indexed_results: dict[int, dict[str, object]] = {}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        indexed_results[index] = future.result()
                    except Exception as exc:
                        # One worker cannot revoke another participant's accepted
                        # Pi turn. Convert only this target into the same typed
                        # rejection as dispatch_target, then settle the batch.
                        indexed_results[index] = self._failed_dispatch(
                            room=room, target=targets[index], decision=decisions[index],
                            room_turn_id=room_turn_id, topic_id=topic_id, error=exc,
                        )
                dispatch_results = [
                    indexed_results[index] for index in range(len(indexed_results))
                ]
        finally:
            # Admission priority is only a short-lived reservation. Release it
            # even if publishing an individual failure receipt also fails.
            self.room_turns.release_priority(target_session_ids)

        successful = [result for result in dispatch_results if result["accepted"] is True]
        if retry_dispatch_id and successful:
            self.room_partner_dispatches.mark_dispatched(
                retry_dispatch_id,
                target_session_turn_id=str(
                    successful[0].get("sessionTurnId") or ""
                ),
            )
        cancelled_only = bool(dispatch_results) and all(
            result.get("status") == "cancelled" for result in dispatch_results
        )
        if work_claimed and work_item is not None and not successful:
            try:
                work_item = self.room_work.fail_dispatch(
                    str(work_item["id"]),
                    room_id=room_id,
                    actor_participant_id=str(targets[0]["id"]),
                    room_turn_id=room_turn_id,
                    previous_accepted_turn_id=previous_accepted_turn_id,
                    reason=(
                        "Room dispatch was cancelled before Runtime admission"
                        if cancelled_only
                        else "Room Runtime rejected the assigned dispatch"
                    ),
                )
            except Exception:
                pass
        if not successful:
            first_error = next(
                (
                    result.get("_exception")
                    for result in dispatch_results
                    if isinstance(result.get("_exception"), BaseException)
                ),
                None,
            )
            if isinstance(first_error, BaseException):
                raise first_error
        for result in dispatch_results:
            result.pop("_exception", None)

        primary_result = successful[0] if successful else dispatch_results[0]
        primary_index = dispatch_results.index(primary_result)
        response: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-room-message.v1",
            "ok": True,
            "accepted": bool(successful),
            "status": (
                "accepted"
                if successful
                else "cancelled"
                if cancelled_only
                else "rejected"
            ),
            "cancelled": cancelled_only,
            "executionOwner": "session",
            "phase": (
                "alignment"
                if work_item is None
                and str(room.get("roomKind") or "collaboration")
                == "collaboration"
                else "conversation"
                if work_item is None
                else "execution"
            ),
            "roomId": room_id,
            "roomTurnId": room_turn_id,
            "clientMessageId": client_message_id,
            "retryOfRootId": retry_of_root_id,
            "participant": targets[primary_index],
            "participants": targets,
            "routeDecision": decisions[primary_index],
            "routeDecisions": decisions,
            "dispatches": dispatch_results,
            "topicId": topic_id,
            "sessionTurnId": primary_result.get("sessionTurnId", ""),
            "timelineEvents": timeline_events,
        }
        if work_item is not None:
            response["workItem"] = work_item
        return response

    def dispatch_target(
        self,
        *,
        room: Mapping[str, object],
        target: Mapping[str, object],
        decision: Mapping[str, object],
        message: str,
        room_turn_id: str,
        topic_id: str,
        unread: Mapping[str, object],
        work_item: Mapping[str, object] | None,
        attachment_ids: Sequence[str],
    ) -> dict[str, object]:
        session_id = str(target["sessionId"])
        participant_id = str(target["id"])
        dispatch_id = str(decision["dispatchId"])
        try:
            room_turn_context = self.build_participant_prompt(
                room,
                target,
                "",
                recent_messages=[
                    item
                    for item in unread["items"]
                    if isinstance(item, Mapping)
                ],
                omitted_message_count=int(unread["omittedCount"]),
                work_item=work_item,
            )
            accepted = self.prompt(
                session_id,
                {
                    "message": message,
                    "clientMessageId": dispatch_id,
                    "_contextSourceToken": self._context_source_token,
                    "_contextSource": "room",
                    "_checkpointText": message,
                    "_transientContext": room_turn_context,
                    "attachments": list(attachment_ids),
                    "_mediaOwnerRoomId": str(room["id"]),
                },
            )
            if accepted.get("accepted") is False:
                cancelled = (
                    accepted.get("cancelled") is True
                    or accepted.get("admissionCancelled") is True
                )
                receipt_error = " ".join(
                    str(accepted.get("error") or "").split()
                )[:240]
                error = receipt_error or (
                    "Pi Runtime cancelled the Room dispatch before admission"
                    if cancelled
                    else "Pi Runtime rejected the Room dispatch"
                )
                self.room_turns.cancel(session_id, room_turn_id)
                child = decision.get("child") is True
                self.room_events.publish(
                    room_id=str(room["id"]),
                    event_type=(
                        "participant_activity" if child else "turn_failed"
                    ),
                    payload=(
                        {
                            "activityKind": "child",
                            "phase": "aborted" if cancelled else "failed",
                            "status": (
                                "dispatch_cancelled"
                                if cancelled
                                else "dispatch_rejected"
                            ),
                            "rootId": room_turn_id,
                            "childDispatchId": dispatch_id,
                            "dispatchId": dispatch_id,
                            "parentDispatchId": str(
                                decision.get("parentDispatchId") or ""
                            ),
                            "error": error,
                        }
                        if child
                        else {
                            "rootId": room_turn_id,
                            "dispatchId": dispatch_id,
                            "status": (
                                "dispatch_cancelled"
                                if cancelled
                                else "dispatch_rejected"
                            ),
                            "cancelled": cancelled,
                            "error": error,
                        }
                    ),
                    turn_id=room_turn_id,
                    participant_id=participant_id,
                    source_session_id=session_id,
                    topic_id=topic_id,
                )
                return {
                    "participantId": participant_id,
                    "sessionId": session_id,
                    "dispatchId": dispatch_id,
                    "accepted": False,
                    "cancelled": cancelled,
                    "admissionCancelled": (
                        accepted.get("admissionCancelled") is True
                    ),
                    "status": "cancelled" if cancelled else "rejected",
                    "sessionTurnId": "",
                    "error": error,
                }
            session_turn_id = str(accepted.get("turnId") or "")
            if not session_turn_id:
                raise RuntimeError("Pi Runtime accepted a Room dispatch without a turnId")
            self._accept_room_turn(
                session_id,
                session_turn_id,
                room_turn_id,
            )
            # Pi has already admitted this exact turn. A failed projection
            # write cannot revoke that admission or make a client retry run
            # it again. Retain the accepted receipt and name pending metadata.
            failed_operations: list[str] = []
            try:
                self.rooms.advance_delivery_cursor(
                    str(room["id"]),
                    participant_id,
                    topic_id=topic_id,
                    through_sequence=int(unread["throughSequence"]),
                )
            except Exception:
                failed_operations.append("advance_delivery_cursor")
            try:
                self.rooms.commit_route(str(room["id"]), decision)
            except Exception:
                failed_operations.append("commit_route")
            return {
                "participantId": participant_id,
                "sessionId": session_id,
                "dispatchId": dispatch_id,
                "accepted": True,
                "cancelled": False,
                "status": "accepted",
                "sessionTurnId": session_turn_id,
                "error": "",
                **({"projectionSync": {
                    "state": "pending", "failedOperations": failed_operations,
                }} if failed_operations else {}),
            }
        except Exception as exc:
            return self._failed_dispatch(
                room=room, target=target, decision=decision,
                room_turn_id=room_turn_id, topic_id=topic_id, error=exc,
            )
        finally:
            self.room_turns.release_priority_session(session_id)

    def _failed_dispatch(
        self, *, room: Mapping[str, object], target: Mapping[str, object],
        decision: Mapping[str, object], room_turn_id: str, topic_id: str,
        error: Exception,
    ) -> dict[str, object]:
        session_id = str(target["sessionId"])
        participant_id = str(target["id"])
        dispatch_id = str(decision["dispatchId"])
        try:
            self.room_turns.cancel(session_id, room_turn_id)
            child = decision.get("child") is True
            cause_code = _error_cause_code(error)
            self.room_events.publish(
                room_id=str(room["id"]),
                event_type=(
                    "participant_activity" if child else "turn_failed"
                ),
                payload=(
                    {
                        "activityKind": "child",
                        "phase": "failed",
                        "status": "dispatch_failed",
                        "rootId": room_turn_id,
                        "childDispatchId": dispatch_id,
                        "dispatchId": dispatch_id,
                        "parentDispatchId": str(
                            decision.get("parentDispatchId") or ""
                        ),
                        "error": _public_error(error),
                        **({"causeCode": cause_code} if cause_code else {}),
                    }
                    if child
                    else {
                        "rootId": room_turn_id,
                        "dispatchId": dispatch_id,
                        "error": _public_error(error),
                        **({"causeCode": cause_code} if cause_code else {}),
                    }
                ),
                turn_id=room_turn_id,
                participant_id=participant_id,
                source_session_id=session_id,
                topic_id=topic_id,
            )
            return {
                "participantId": participant_id,
                "sessionId": session_id,
                "dispatchId": dispatch_id,
                "accepted": False,
                "cancelled": False,
                "status": "failed",
                "sessionTurnId": "",
                "error": _public_error(error),
                "_exception": error,
            }
        finally:
            self.room_turns.release_priority_session(session_id)


def _public_error(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return text[:240] or error.__class__.__name__


def _error_cause_code(error: BaseException) -> str:
    """Project a durable Room causeCode.

    Session receipts keep the canonical lowercase ``error_code``
    (``goal_paused``). Room timeline events uppercase the same token so they
    match existing wake/partner cause comparisons.
    """

    return " ".join(
        str(
            getattr(error, "cause_code", "")
            or getattr(error, "error_code", "")
            or ""
        ).split()
    ).upper()[:80]
