from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Protocol

from rag_ime.agent_command_receipts import AgentCommandReceiptFailed
from rag_ime.pi.values import PiRuntimeCommandRejected
from rag_ime.rooms.jev_routing import route_with_jev
from rag_ime.rooms.routing import plan_room_routes
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


class AcceptedTurnProjectionRuntime(Protocol):
    def prepare_accepted_turn_projection(
        self, session_id: str, turn_id: str, *, client_message_id: str,
    ) -> Callable[[Callable[[str], None]], str]: ...


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
        pending_removal: Callable[[str, str], bool] | None = None,
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
        self.pending_removal = pending_removal or (lambda _room_id, _participant_id: False)

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
        if room["status"] != "active":
            raise ValueError("agent room is archived")
        departing = {str(p["id"]) for p in room["participants"]
                     if self.pending_removal(room_id, str(p["id"]))}
        routing_room = ({**room, "participants": [p for p in room["participants"]
                         if p["id"] not in departing]} if departing else room)
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
        # A pending member remains active for its old work/verification, but
        # routing must never choose it for a fresh legacy Room message.
        decisions = (plan_room_routes(routing_room, message,
            requested_participant_ids=requested_participant_ids,
            profiles=profiles,
            authoritative_participant_id=authoritative_participant_id,
            conversation_only=work_item is None)
            if departing else self.rooms.plan_routes(
                room_id, message,
                requested_participant_ids=requested_participant_ids,
                profiles=profiles,
                authoritative_participant_id=authoritative_participant_id,
                conversation_only=work_item is None))
        # The Jev route helper refetches an unfiltered Room after its provider
        # call; keep deterministic filtered routing while removal is pending.
        if not departing:
            decisions = route_with_jev(self.rooms, room, message, decisions)
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
            if (str(latest_target.get("status") or "") != "active"
                or self.pending_removal(room_id, str(target["id"]))):
                raise ValueError("selected Room participant is removed or pending removal")

        try:
            # ensure_available runs inside the registry lock, so the
            # participant re-check and the priority reservation remain the
            # single critical section they were as inline code.
            priority_reservation = self.room_turns.hold_priority_if_idle(
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
            self.room_turns.release_priority(target_session_ids, reservation=priority_reservation)
            raise
        if busy_targets:
            self.room_turns.release_priority(target_session_ids, reservation=priority_reservation)
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
            self.room_turns.release_priority(target_session_ids, reservation=priority_reservation)
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
            try:
                # No Runtime dispatch has started yet. Clear every pending
                # binding before publishing fallible terminal projections.
                for session_id in target_session_ids:
                    self.room_turns.cancel(session_id, room_turn_id)
                for decision, target in zip(decisions, targets, strict=True):
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
            finally:
                self.room_turns.release_priority(target_session_ids, reservation=priority_reservation)
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
                        release_priority_on_exit=False,
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
            # This batch acquired the reservation, so it releases it once.
            # A worker must not release early and let this cleanup erase a
            # newer request's reservation for the same Session.
            self.room_turns.release_priority(target_session_ids, reservation=priority_reservation)

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

    def dispatch_prepared(
        self,
        request: Mapping[str, object],
        *,
        validate: Callable[[], None],
        reserve: Callable[[], object],
        release: Callable[[], object],
        require_active: Callable[[], None],
        configure: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        """Admit one prepared identity, retaining ambiguity until exact lookup.

        All four callbacks are bound to this dispatch, including release when
        reserve raises after changing Runtime memory. Pi consumes its own
        reservation under its admission lock; Stop can still fence that exact
        reservation while this method assembles context outside the Room lock.
        """
        session_id = str(request["sessionId"])
        root_id = str(request["rootId"])
        dispatch_id = str(request["dispatchId"])
        held = False
        priority_reservation = None
        reserve_attempted = False
        prompt_attempted = False
        rejected = False
        partner_registered = False
        base = {"participantId": str(request["ownerId"]), "sessionId": session_id,
                "dispatchId": dispatch_id, "accepted": False, "sessionTurnId": ""}

        def release_registry(*, cancel: bool) -> None:
            # A delayed cleanup may run after this Session has been reused by
            # another dispatch of the SAME Root. Root identity alone is unsafe.
            with self.room_turns.lock:
                if self.room_turns.active_turn(session_id) == (root_id, dispatch_id):
                    if cancel:
                        self.room_turns.cancel(session_id, root_id)
                    self.room_turns.release_priority_session(session_id, reservation=priority_reservation)

        try:
            purpose = str(request.get("purpose") or "execute")
            if purpose not in {"execute", "plan", "verify", "synthesize"}:
                raise ValueError("unsupported Jev execution purpose")
            room = self.rooms.get(str(request["roomId"]))
            target = self.rooms.participant(str(request["ownerId"]))
            if target.get("sessionId") != session_id or target.get("status") != "active":
                raise ValueError("prepared executor binding is no longer active")
            if self.pending_removal(str(room["id"]), str(target["id"])):
                raise ValueError("prepared executor is pending Room removal")
            # Planning/review/synthesis concerns a subject Task but is not its
            # business attempt and must never submit that Task's work_result.
            work = (self.room_work.get(str(request["taskId"]), room_id=str(room["id"]))
                    if purpose == "execute" else None)
            topic_id = str((work or {}).get("topicId") or room.get("activeTopicId") or "")
            validate()  # Skill catalog/material reads run outside the Room lock.
            with self.room_turns.lock:
                if self.room_turns.is_cancelled(session_id, root_id):
                    raise ValueError("Root stopped before admission")
                priority_reservation = self.room_turns.hold_priority_if_idle((session_id,))
                held = True
                try:
                    if not self._room_target_idle(session_id, allow_user_priority=True):
                        raise RoomSessionBusyError(session_id)
                    self.room_turns.begin(session_id, root_id, topic_id,
                        dispatch_id=dispatch_id, child=True,
                        **({"work_item_id": str(work["id"]),
                            "work_item_revision": int(work["revision"]),
                            "attempt_id": dispatch_id} if work is not None else {}))
                except Exception:
                    # Still in the acquiring critical section, before another
                    # request can take an unbound priority reservation.
                    self.room_turns.release_priority_session(session_id, reservation=priority_reservation)
                    raise
                reserve_attempted = True
                reserve()
            if configure is not None:
                configure()
            decision = {"dispatchId": dispatch_id, "rootId": root_id, "child": True,
                        "parentDispatchId": str(request["graphId"]),
                        "targetParticipantId": target["id"], "targetSessionId": session_id,
                        "routingPolicy": "jev", "purpose": purpose,
                        **({"workItemId": work["id"]} if work is not None else {
                            "subjectTaskId": str(request.get("taskId") or ""),
                            "subjectHash": str(request.get("subjectHash") or "")})}
            if work is not None:
                accountable = self.rooms.participant(str(work["accountableParticipantId"]))
                # Register may commit then fail while notifying. Cleanup below
                # therefore attempts exact terminalization even when it raises.
                partner_registered = True
                self.room_partner_dispatches.register(child_dispatch_id=dispatch_id,
                    room_id=str(room["id"]), root_id=root_id, parent_dispatch_id=str(request["graphId"]),
                    tool_call_id=dispatch_id, source_participant_id=str(accountable["id"]),
                    source_session_id=str(accountable["sessionId"]), target_participant_id=str(target["id"]),
                    target_session_id=session_id, work_item_id=str(work["id"]))
            self.room_events.publish(room_id=str(room["id"]), event_type="route_decision",
                payload=decision, turn_id=root_id, participant_id=str(target["id"]),
                source_session_id=session_id, topic_id=topic_id)
            room_context = self.build_participant_prompt({**room, "_jevExecutionPurpose": purpose}, target, "", recent_messages=[],
                omitted_message_count=0, work_item=work)
            execution_context = (
                "Jev ExecutionPack: execute only this responsibility. Report evidence and unresolved issues when relevant to the actual request. "
                "For execute, call room_partner op=result_submit with proposal={resultSummary,artifactRefs,evidenceRefs} then finish. "
                "resultSummary is the final user-facing answer, not an internal activity report. For greetings or casual conversation, "
                "submit only the natural conversational reply; do not append completion, verification, evidence, or no-unresolved-items boilerplate. "
                "For substantive tasks preserve actual results, evidence and limitations. Keep bookkeeping in structured fields. "
                "Other purposes use their requested structured submission. A work_result is a submission, not acceptance. Materials marked read_exact must be read via existing tools.\n"
                + json.dumps(request["contextManifest"], ensure_ascii=False))
            from rag_ime.jev_tasks.policy import model_role_guidance
            model = request["contextManifest"].get("executionScope", {}).get("modelSelection", {})
            guidance = model_role_guidance(str(model.get("modelId") or ""), purpose)
            if guidance:
                execution_context += "\nModel role guidance:\n" + guidance
            message = str(request["taskBrief"]["objective"])
            # A reused Session has the previous completion in its transcript.
            # Make the new revision explicit in the current input as well as
            # the task-scoped feedback material, without starting a new task.
            feedback = (work.get("blocker") or {}).get("reviewFeedback") if work is not None else None
            if purpose == "execute" and work is not None and work["revision"] > 0 and feedback:
                message = (
                    f"同一任务的第 {work['revision']} 次修订：上一版本未通过验收，需要继续修复。\n"
                    "按原范围核对具体问题，完成实际修复与检查后重新提交；旧完成声明或 WorkDocument "
                    "过程记录不能替代要求的交付文件。\n"
                    f"返修依据（不扩展目标或权限）：{str(feedback)[:2000]}\n\n原任务目标：{message}"
                )
            validate()  # Refresh before the short registry check.
            with self.room_turns.lock:
                if self.pending_removal(str(room["id"]), str(target["id"])):
                    raise ValueError("prepared executor is pending Room removal")
                if self.room_turns.is_cancelled(session_id, root_id):
                    raise ValueError("Root stopped during prompt preparation")
                if self.room_turns.active_turn(session_id) != (root_id, dispatch_id):
                    raise ValueError("prepared dispatch no longer owns its Room reservation")
                require_active()
            # From here on, loss of a response is not non-admission. Runtime
            # rechecks/consumes the same reservation at the actual write fence.
            prompt_attempted = True
            accepted = self.prompt(session_id, {
                "message": message, "clientMessageId": dispatch_id,
                "_contextSourceToken": self._context_source_token, "_contextSource": "room",
                "_checkpointText": message,
                "_transientContext": "\n\n".join(p for p in (room_context, execution_context) if p),
                "attachments": list(request["contextManifest"].get("executionScope", {}).get("attachmentIds", [])), "_mediaOwnerRoomId": str(room["id"]),
            })
            if accepted.get("accepted") is False:
                rejected = True
                raise ValueError(str(accepted.get("error") or "Pi rejected the exact prepared admission"))
            turn_id = str(accepted.get("turnId") or "")
            if not turn_id:
                raise RuntimeError("Pi Runtime returned no exact accepted turn identity")
        except Exception as exc:
            rejected_before_prompt = isinstance(exc, (PiRuntimeCommandRejected, AgentCommandReceiptFailed)) and (
                _error_cause_code(exc) in {"TOOL_MANIFEST_SYNC_FAILED", "PROMPT_ADMISSION_CANCELLED"})
            not_sent = not prompt_attempted or rejected or rejected_before_prompt
            cleanup_failures = []
            if not_sent:
                if reserve_attempted:
                    try:
                        release()
                    except Exception:
                        cleanup_failures.append("release_prompt_admission")
                if held:
                    try:
                        release_registry(cancel=True)
                    except Exception:
                        cleanup_failures.append("release_room_reservation")
                if partner_registered:
                    try:
                        self.room_partner_dispatches.settle(dispatch_id, status="cancelled", result="",
                            completion_source="dispatch_not_admitted", error=_public_error(exc))
                    except KeyError:
                        pass  # register did not commit; there is nothing to settle.
                    except Exception:
                        cleanup_failures.append("settle_partner_dispatch")
            elif held:
                try:
                    release_registry(cancel=False)
                except Exception:
                    cleanup_failures.append("release_room_priority")
            return {**base, "notSent": not_sent,
                    "status": "not_sent" if not_sent else "unknown", "error": _public_error(exc),
                    **({"cleanupSync": {"state": "pending", "failedOperations": cleanup_failures}}
                       if cleanup_failures else {})}

        # No projection failure below can revoke Pi's already accepted turn.
        priority_failed = False
        try:
            release_registry(cancel=False)
        except Exception:
            priority_failed = True
        result = self._accepted_dispatch(room=room, target=target, decision=decision,
            room_turn_id=root_id, topic_id=topic_id, through_sequence=0, session_turn_id=turn_id)
        if priority_failed:
            pending = result.setdefault("projectionSync", {"state": "pending", "failedOperations": []})
            pending["failedOperations"].append("release_room_priority")
        if partner_registered:
            try:
                self.room_partner_dispatches.mark_dispatched(dispatch_id, target_session_turn_id=turn_id)
            except Exception:
                pending = result.setdefault("projectionSync", {"state": "pending", "failedOperations": []})
                pending["failedOperations"].append("mark_dispatched")
        return result

    def repair_prepared_projection(
        self, request: Mapping[str, object], accepted_receipt: Mapping[str, object], *,
        validate: Callable[[], None], runtime: AcceptedTurnProjectionRuntime,
    ) -> dict[str, object]:
        """Repair one proven admission without replaying any execution effect.

        The caller validates the current execute binding or auxiliary subject.
        Pi's guard proves which exact turn may occupy the live registry; a
        settled or retired turn repairs metadata without reopening that turn.
        Failure remains projectionSync=pending on the original accepted receipt.
        """
        if (accepted_receipt.get("state") != "accepted"
            or any(accepted_receipt.get(key) != request.get(key) for key in ("sessionId", "dispatchId", "taskId"))):
            raise ValueError("projection receipt does not match the prepared dispatch")
        session_id, dispatch_id = str(request["sessionId"]), str(request["dispatchId"])
        root_id, turn_id = str(request["rootId"]), str(accepted_receipt.get("turnId") or "")
        if not turn_id or (request.get("turnId") and request["turnId"] != turn_id):
            raise ValueError("projection repair requires the exact accepted Pi turn")
        result = dict(accepted_receipt)
        failed: list[str] = []
        sync: dict[str, object] = {"state": "pending", "failedOperations": failed}
        result["projectionSync"] = sync
        operation = "observe_runtime"
        try:
            # Runtime ensure may consult Room context; never call it while
            # holding the Room lock and reversing lifecycle lock order.
            guard = runtime.prepare_accepted_turn_projection(session_id, turn_id, client_message_id=dispatch_id)
            with self.room_turns.lock:
                operation = "validate_binding"
                validate()
                purpose = str(request.get("purpose") or "execute")
                if purpose not in {"execute", "plan", "verify", "synthesize"}:
                    raise ValueError("unsupported Jev execution purpose")
                room = self.rooms.get(str(request["roomId"]))
                target = self.rooms.participant(str(request["ownerId"]))
                if target.get("sessionId") != session_id or target.get("status") != "active":
                    raise ValueError("prepared executor binding is no longer active")
                work = (self.room_work.get(str(request["taskId"]), room_id=str(room["id"]))
                        if purpose == "execute" else None)
                topic_id = str((work or {}).get("topicId") or room.get("activeTopicId") or "")
                decision = {"dispatchId": dispatch_id, "rootId": root_id, "child": True,
                    "parentDispatchId": str(request["graphId"]), "targetParticipantId": target["id"],
                    "targetSessionId": session_id, "routingPolicy": "jev", "purpose": purpose,
                    **({"workItemId": work["id"]} if work is not None else {
                        "subjectTaskId": str(request["taskId"]), "subjectHash": str(request.get("subjectHash") or "")})}

                def check_registry() -> tuple[str, str]:
                    active = self.room_turns.active_turn(session_id)
                    if active not in {("", ""), (root_id, dispatch_id)}:
                        raise RoomSessionBusyError(session_id)
                    for key, mapped_root in self.room_turns.turn_by_session_turn.items():
                        if key[0] == session_id and (key[1] != turn_id or mapped_root != root_id
                            or self.room_turns.dispatch_by_session_turn.get(key) != dispatch_id):
                            raise RoomSessionBusyError(session_id)
                    if (session_id in self.room_turns.private_intercom_pending_by_session
                        or any(key[0] == session_id for key in self.room_turns.private_intercom_by_session_turn)
                        or (active == ("", "") and session_id in self.room_turns.user_priority_sessions)):
                        raise RoomSessionBusyError(session_id)
                    return active

                def project(mode: str) -> None:
                    active = check_registry()
                    if mode == "active":
                        if self.room_turns.is_cancelled(session_id, root_id):
                            raise ValueError("Room Root stopped before projection repair")
                        if active == ("", ""):
                            self.room_turns.begin(session_id, root_id, topic_id, dispatch_id=dispatch_id, child=True,
                                **({"work_item_id": str(work["id"]), "work_item_revision": int(work["revision"]),
                                    "attempt_id": dispatch_id} if work is not None else {}))
                        self.room_turns.release_priority_session(session_id)
                        self._accept_room_turn(session_id, turn_id, root_id)
                    elif mode in {"settled", "retired"}:
                        if active == (root_id, dispatch_id):
                            self.room_turns.release_priority_session(session_id)
                            self.room_turns.cancel(session_id, root_id)
                    else:
                        raise ValueError("Runtime did not prove the projection state")

                operation = "repair_room_projection"
                # The guard can reconcile a real terminal receipt. Check the
                # Room owner before that emits any old-turn projection events.
                check_registry()
                sync["runtimeState"] = guard(project)
                if work is not None:
                    try:
                        accountable = self.rooms.participant(str(work["accountableParticipantId"]))
                        self.room_partner_dispatches.register(child_dispatch_id=dispatch_id,
                            room_id=str(room["id"]), root_id=root_id, parent_dispatch_id=str(request["graphId"]),
                            tool_call_id=dispatch_id, source_participant_id=str(accountable["id"]),
                            source_session_id=str(accountable["sessionId"]), target_participant_id=str(target["id"]),
                            target_session_id=session_id, work_item_id=str(work["id"]))
                    except Exception:
                        failed.append("register_partner_dispatch")
                    try:
                        self.room_partner_dispatches.mark_dispatched(dispatch_id, target_session_turn_id=turn_id)
                    except Exception:
                        failed.append("mark_dispatched")
                for name, callback in (
                    ("advance_delivery_cursor", lambda: self.rooms.advance_delivery_cursor(str(room["id"]),
                        str(target["id"]), topic_id=topic_id, through_sequence=0)),
                    ("commit_route", lambda: self.rooms.commit_route(str(room["id"]), decision)),
                ):
                    try:
                        callback()
                    except Exception:
                        failed.append(name)
        except Exception as exc:
            failed.append(operation)
            sync["error"] = _public_error(exc)
        sync["state"] = "pending" if failed else "synced"
        return result

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
        execution_context: str = "",
        release_priority_on_exit: bool = True,
    ) -> dict[str, object]:
        session_id = str(target["sessionId"])
        participant_id = str(target["id"])
        dispatch_id = str(decision["dispatchId"])
        try:
            if self.pending_removal(str(room["id"]), participant_id):
                raise ValueError("selected Room participant is pending removal")
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
                    "_transientContext": "\n\n".join(p for p in (room_turn_context, execution_context) if p),
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
            return self._accepted_dispatch(room=room, target=target, decision=decision,
                room_turn_id=room_turn_id, topic_id=topic_id,
                through_sequence=int(unread["throughSequence"]), session_turn_id=session_turn_id)
        except Exception as exc:
            return self._failed_dispatch(
                room=room, target=target, decision=decision,
                room_turn_id=room_turn_id, topic_id=topic_id, error=exc,
            )
        finally:
            if release_priority_on_exit:
                self.room_turns.release_priority_session(session_id)

    def _accepted_dispatch(
        self, *, room: Mapping[str, object], target: Mapping[str, object],
        decision: Mapping[str, object], room_turn_id: str, topic_id: str,
        through_sequence: int, session_turn_id: str,
    ) -> dict[str, object]:
        session_id, participant_id = str(target["sessionId"]), str(target["id"])
        failed_operations: list[str] = []
        try:
            with self.room_turns.lock:
                if self.room_turns.active_turn(session_id) != (room_turn_id, str(decision["dispatchId"])):
                    raise ValueError("accepted turn projection was superseded by a different dispatch")
                self._accept_room_turn(session_id, session_turn_id, room_turn_id)
        except Exception:
            failed_operations.append("accept_turn")
        try:
            self.rooms.advance_delivery_cursor(str(room["id"]), participant_id,
                topic_id=topic_id, through_sequence=through_sequence)
        except Exception:
            failed_operations.append("advance_delivery_cursor")
        try:
            self.rooms.commit_route(str(room["id"]), decision)
        except Exception:
            failed_operations.append("commit_route")
        return {"participantId": participant_id, "sessionId": session_id,
                "dispatchId": str(decision["dispatchId"]), "accepted": True,
                "cancelled": False, "status": "accepted", "sessionTurnId": session_turn_id,
                "error": "", **({"projectionSync": {"state": "pending",
                    "failedOperations": failed_operations}} if failed_operations else {})}

    def _failed_dispatch(
        self, *, room: Mapping[str, object], target: Mapping[str, object],
        decision: Mapping[str, object], room_turn_id: str, topic_id: str,
        error: Exception,
    ) -> dict[str, object]:
        session_id = str(target["sessionId"])
        participant_id = str(target["id"])
        dispatch_id = str(decision["dispatchId"])
        # The dispatch caller owns its priority reservation and cleanup.
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
