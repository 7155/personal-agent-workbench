from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
import sqlite3
from typing import Protocol

from .agent_execution_policy import (
    APPROVAL_ASK,
    APPROVAL_AUTO,
    APPROVAL_DENY,
    APPROVAL_MODEL,
    ROOM_UNRESTRICTED_EXECUTION_MODE,
    approval_strategy,
    full_access_policy_active,
    read_only_policy_active,
    unrestricted_workspace_policy_active,
)
from .agent_external_approval import ExternalApprovalFinalizer
from .agent_approval_model import ApprovalModelArbiter, requires_jev_approval
from .jev_tasks.policy import tool_approval_mode
from .agent_sessions import AgentSessionStore
from .agent_events import AgentEventHub
from .agent_memory_sources import AgentMemorySourceStore
from rag_ime.rooms.store import AgentRoomStore, AgentRoomEventHub
from rag_ime.rooms.turn_registry import RoomTurnRegistry
from .external_actions import (
    PORTABLE_RESTORE_ACTION,
    materialize_portable_restore_plan,
)


_RECOVERABLE_GOVERNED_MEMORY_OPERATIONS = frozenset(
    {
        "remember_apply",
        "correct_apply",
        "forget_apply",
        "governance_rollback",
    }
)


class ApprovalRuntime(Protocol):
    """Only the live approval/review capability, resolved after Runtime replacement."""

    def has_pending_approval(self, session_id: str, approval_id: str) -> bool: ...
    def has_pending_review(self, session_id: str, run_id: str) -> bool: ...
    def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        *,
        approved: bool,
        resolution_state: str = "",
    ) -> None: ...
    def resolve_review(
        self, session_id: str, run_id: str, *, reviewed: bool
    ) -> None: ...


ApprovalExecutor = Callable[[Mapping[str, object]], Mapping[str, object]]


class AgentApprovalApplicationService:
    """Own approval decisions, execution receipts, and external finalization."""

    def __init__(
        self,
        *,
        sessions: AgentSessionStore,
        events: AgentEventHub,
        rooms: AgentRoomStore,
        room_events: AgentRoomEventHub,
        room_turns: RoomTurnRegistry,
        memory_sources: AgentMemorySourceStore,
        approval_model: ApprovalModelArbiter,
        runtime_provider: Callable[[], ApprovalRuntime],
        executor_provider: Callable[[], ApprovalExecutor | None],
        process_id_provider: Callable[[], int],
        record_tool_receipt_evidence: Callable[
            [Mapping[str, object]], dict[str, object]
        ],
        active_room_dispatch_context: Callable[[str], Mapping[str, object] | None],
    ) -> None:
        self.sessions = sessions
        self.events = events
        self.rooms = rooms
        self.room_events = room_events
        self.room_turns = room_turns
        self.memory_sources = memory_sources
        self.approval_model = approval_model
        self._runtime_provider = runtime_provider
        self._executor_provider = executor_provider
        self._process_id_provider = process_id_provider
        self._record_tool_receipt_evidence_safely = record_tool_receipt_evidence
        self._active_room_dispatch_context = active_room_dispatch_context
        self.external = ExternalApprovalFinalizer(
            sessions=sessions,
            events=events,
            memory_sources=memory_sources,
            process_id_provider=process_id_provider,
            record_tool_receipt_evidence=record_tool_receipt_evidence,
        )

    @property
    def runtime(self) -> ApprovalRuntime:
        return self._runtime_provider()

    def _claim_approval_execution(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        """Linearize a Room authorization with turn rotation and Runtime binding."""

        approval_id = str(approval.get("approvalId") or "")
        causal = (
            approval.get("causalMetadata")
            if isinstance(approval.get("causalMetadata"), Mapping)
            else {}
        )
        if not bool(causal.get("roomBound")):
            return self.sessions.claim_approval_execution(approval_id)
        session_id = str(approval.get("sessionId") or "")
        # Room begin/finish/cancel use this same lock.  Keep it held while the
        # approval store atomically compares the runtime generation and claims
        # the effect, making the claim the one execution-start boundary.
        with self.room_turns.lock:
            live_context = self._active_room_dispatch_context(session_id)
            return self.sessions.claim_approval_execution(
                approval_id,
                room_context=(
                    live_context
                    if isinstance(live_context, Mapping)
                    else {}
                ),
            )


    def reconcile_abandoned_execution_claims(self) -> dict[str, object]:
        """Repair interrupted approval projections without replaying Tools."""

        recovered, projection_candidates = (
            self.sessions.fail_abandoned_approval_executions()
        )
        session_projected_ids: list[str] = []
        for approval in projection_candidates:
            session_id = str(approval.get("sessionId") or "")
            approval_id = str(approval.get("approvalId") or "")
            causal = (
                approval.get("causalMetadata")
                if isinstance(approval.get("causalMetadata"), Mapping)
                else {}
            )
            tool_call_id = str(approval.get("toolCallId") or "").strip()
            has_approval_terminal = self.sessions.has_runtime_approval_resolution(
                session_id,
                approval_id,
            )
            has_tool_terminal = self.sessions.has_runtime_tool_terminal(
                session_id,
                tool_call_id,
                turn_id=str(causal.get("turnId") or ""),
                tool_name=str(approval.get("toolId") or ""),
                requested_after_ms=int(approval.get("requestedAtMs") or 0),
            )
            if has_approval_terminal and has_tool_terminal:
                continue
            self._project_recovered_session_execution(
                approval,
                include_approval=not has_approval_terminal,
                include_tool=not has_tool_terminal,
            )
            session_projected_ids.append(approval_id)
        if session_projected_ids:
            flush = getattr(self.events, "flush", None)
            if callable(flush) and not flush():
                raise RuntimeError(
                    "recovered Session projections did not drain"
                )
        projected_ids: list[str] = []
        has_tool_terminal = getattr(
            self.room_events,
            "has_tool_terminal",
            None,
        )
        for approval in projection_candidates:
            causal = (
                approval.get("causalMetadata")
                if isinstance(approval.get("causalMetadata"), Mapping)
                else {}
            )
            if not bool(causal.get("roomBound")):
                continue
            approval_id = str(approval.get("approvalId") or "")
            room_id = str(causal.get("roomId") or "")
            tool_call_id = str(approval.get("toolCallId") or "")
            if (
                callable(has_tool_terminal)
                and has_tool_terminal(
                    room_id,
                    tool_call_id,
                    root_id=str(causal.get("rootId") or ""),
                    dispatch_id=str(causal.get("dispatchId") or ""),
                )
            ):
                continue
            self._project_abandoned_room_execution(approval)
            projected_ids.append(approval_id)
        return {
            "recoveredCount": len(recovered),
            "approvalIds": [
                str(approval.get("approvalId") or "")
                for approval in recovered
            ],
            "sessionReconciledCount": len(session_projected_ids),
            "sessionApprovalIds": session_projected_ids,
            "projectionReconciledCount": len(projected_ids),
            "projectionApprovalIds": projected_ids,
        }

    def _project_recovered_session_execution(
        self,
        approval: Mapping[str, object],
        *,
        include_approval: bool,
        include_tool: bool,
    ) -> None:
        """Publish known durable terminal facts without repeating effects.

        This deliberately does not call ``execute_approved``, checkpoint
        memory, or record Tool evidence again. The approval row and receipt
        are already authoritative; only the missing public lifecycle is
        repaired.
        """

        session_id = str(approval.get("sessionId") or "").strip()
        approval_id = str(approval.get("approvalId") or "").strip()
        tool_call_id = str(approval.get("toolCallId") or "").strip()
        state = str(approval.get("state") or "failed")
        turn_id = _approval_turn_id(approval)
        receipt = (
            approval.get("receipt")
            if isinstance(approval.get("receipt"), Mapping)
            else {}
        )
        summary = str(receipt.get("summary") or "").strip() or (
            "上次工具执行已完成。"
            if state == "applied"
            else "上次工具执行已终止。"
        )
        if not session_id or not approval_id:
            return

        causal = (
            approval.get("causalMetadata")
            if isinstance(approval.get("causalMetadata"), Mapping)
            else {}
        )
        room_bound = bool(causal.get("roomBound"))

        if self.runtime.has_pending_approval(session_id, approval_id):
            # A surviving Runtime may still own a waiting Tool bridge. Resolve
            # it with the persisted truth, but continue to write the durable
            # recovery events below so another restart cannot lose the fact.
            self.runtime.resolve_approval(
                session_id,
                approval_id,
                approved=state in {"applied", "external_pending"},
                resolution_state=state,
            )
        if include_approval:
            recovery_identity = (
                {
                    "automatic": True,
                    "decisionMode": "policy",
                }
                if room_bound
                else {}
            )
            self.events.publish(
                session_id,
                "approval_resolved",
                {
                    "approvalId": approval_id,
                    "state": state,
                    "recoveredExecutionTerminal": True,
                    **recovery_identity,
                    **_approval_event_identity(approval),
                },
                turn_id=turn_id,
            )
        if include_tool and tool_call_id:
            is_error = state != "applied"
            self.events.publish(
                session_id,
                "tool_finished",
                {
                    "approvalId": approval_id,
                    "toolCallId": tool_call_id,
                    "toolName": str(approval.get("toolId") or ""),
                    "isError": is_error,
                    "status": "failed" if is_error else "completed",
                    "state": state,
                    "summary": summary,
                    "result": dict(receipt),
                    "recoveredExecutionTerminal": True,
                },
                turn_id=turn_id,
            )

    def _project_abandoned_room_execution(
        self,
        approval: Mapping[str, object],
    ) -> None:
        """Persist the recovered terminal in Room without volatile turn state."""

        causal = (
            approval.get("causalMetadata")
            if isinstance(approval.get("causalMetadata"), Mapping)
            else {}
        )
        if not bool(causal.get("roomBound")):
            return
        session_id = str(approval.get("sessionId") or "").strip()
        room_id = str(causal.get("roomId") or "").strip()
        root_id = str(causal.get("rootId") or "").strip()
        dispatch_id = str(causal.get("dispatchId") or "").strip()
        approval_id = str(approval.get("approvalId") or "").strip()
        if not session_id or not room_id or not root_id or not approval_id:
            return
        participant = self.rooms.participant_for_session(
            session_id,
            active_only=False,
        )
        if (
            not isinstance(participant, Mapping)
            or str(participant.get("roomId") or "") != room_id
        ):
            return
        receipt = (
            approval.get("receipt")
            if isinstance(approval.get("receipt"), Mapping)
            else {}
        )
        state = str(approval.get("state") or "failed")
        summary = str(receipt.get("summary") or "").strip() or (
            "上次工具执行已完成。"
            if state == "applied"
            else "上次工具执行的结果未知，未自动重放。"
        )
        tool_call_id = str(approval.get("toolCallId") or "").strip()
        is_error = state != "applied"
        resolved_at_ms = (
            int(receipt.get("reconciledAtMs") or 0)
            or int(approval.get("decidedAtMs") or 0)
            or None
        )
        effect_may_have_occurred = bool(
            receipt.get("effectMayHaveOccurred")
            or receipt.get("mutationApplied") is True
            or state == "applied"
        )
        approval_data: dict[str, object] = {
            "rootId": root_id,
            "dispatchId": dispatch_id,
            "generation": int(causal.get("generation") or 0),
            "approvalId": approval_id,
            "toolCallId": tool_call_id,
            "toolName": str(approval.get("toolId") or ""),
            "state": state,
            "resolutionState": state,
            "status": "failed" if is_error else "completed",
            "automatic": True,
            "decisionMode": "policy",
            "isError": is_error,
            "summary": summary,
            "executionOutcome": str(
                receipt.get("executionOutcome") or (
                    "completed" if state == "applied" else "unknown"
                )
            ),
            "effectMayHaveOccurred": effect_may_have_occurred,
            "replayAllowed": bool(receipt.get("replayAllowed")),
        }
        if is_error:
            approval_data["error"] = summary
        self.room_events.publish_projection(
            projection_key=(
                f"approval-execution-recovery:{approval_id}"
            ),
            room_id=room_id,
            event_type="participant_activity",
            payload={
                "sourceEventId": (
                    f"approval-execution-recovery:{approval_id}"
                ),
                "sourceEventType": "approval_resolved",
                "data": approval_data,
            },
            turn_id=root_id,
            participant_id=str(participant.get("id") or ""),
            source_session_id=session_id,
            topic_id="",
            created_at_ms=resolved_at_ms,
        )
        self.room_events.publish_projection(
            projection_key=(
                f"approval-execution-recovery:{approval_id}:tool-finished"
            ),
            room_id=room_id,
            event_type="participant_activity",
            payload={
                "sourceEventId": (
                    f"approval-execution-recovery:{approval_id}:tool-finished"
                ),
                "sourceEventType": "tool_finished",
                "data": {
                    "rootId": root_id,
                    "dispatchId": dispatch_id,
                    "generation": int(causal.get("generation") or 0),
                    "approvalId": approval_id,
                    "toolCallId": tool_call_id,
                    "toolName": str(approval.get("toolId") or ""),
                    "state": state,
                    "status": "failed" if is_error else "completed",
                    "automatic": True,
                    "decisionMode": "policy",
                    "isError": is_error,
                    "summary": summary,
                    "mutationApplied": receipt.get("mutationApplied"),
                    "executionOutcome": str(
                        receipt.get("executionOutcome") or (
                            "completed" if state == "applied" else "unknown"
                        )
                    ),
                    "effectMayHaveOccurred": effect_may_have_occurred,
                    "replayAllowed": bool(receipt.get("replayAllowed")),
                    "recoveredExecutionTerminal": True,
                },
            },
            turn_id=root_id,
            participant_id=str(participant.get("id") or ""),
            source_session_id=session_id,
            topic_id="",
            created_at_ms=resolved_at_ms,
        )

    def list_approvals(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        session_id = str(payload.get("sessionId") or "").strip()
        requested_state = str(payload.get("state") or "").strip()
        requested_limit = _integer(
            payload.get("limit"),
            default=100,
            minimum=1,
            maximum=500,
        )
        recent = self.sessions.list_approvals(
            session_id=session_id,
            limit=500,
        )
        self._reconcile_runtime_terminal_approvals(recent)
        items = (
            recent[:requested_limit]
            if not requested_state
            else self.sessions.list_approvals(
                session_id=session_id,
                state=requested_state,
                limit=requested_limit,
            )
        )
        return {
            "schemaVersion": "rag-ime.agent-approval-list.v1",
            "ok": True,
            "sessionId": session_id,
            "items": items,
        }

    def _reconcile_runtime_terminal_approvals(
        self,
        approvals: list[Mapping[str, object]],
    ) -> None:
        """Deliver durable approval terminal states to a waiting Pi Tool call."""

        for approval in approvals:
            state = str(approval.get("state") or "")
            if state not in {
                "applied",
                "external_pending",
                "rejected",
                "expired",
                "stale",
                "failed",
            }:
                continue
            session_id = str(approval.get("sessionId") or "")
            approval_id = str(approval.get("approvalId") or "")
            if not self.runtime.has_pending_approval(
                session_id,
                approval_id,
            ):
                continue
            if state in {"applied", "external_pending"}:
                self.finish_decision(approval, pending_in_pi=True)
            else:
                self.finish_terminal(approval, pending_in_pi=True)

    def resolve_review(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        run_id = _required_text(payload, "runId")
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"reviewed", "deferred"}:
            raise ValueError("decision must be reviewed or deferred")
        if not self.runtime.has_pending_review(session_id, run_id):
            recovery = self._recover_applied_memory_review(
                session_id,
                run_id,
            )
            if recovery is None:
                raise ValueError("review is no longer active in Pi")
            return {
                "schemaVersion": "rag-ime.agent-review-decision.v1",
                "ok": True,
                "sessionId": session_id,
                "runId": run_id,
                "decision": decision,
                # The exact recovered turn was retired and Pi was confirmed
                # idle. This is terminal runtime recovery, not a normal
                # review.resolve delivery.
                "runtimeNotified": True,
                "recovered": True,
                "recoveryAction": "retire_recovered_turn",
                "recoveryReceipt": recovery,
            }
        self.runtime.resolve_review(
            session_id,
            run_id,
            reviewed=decision == "reviewed",
        )
        return {
            "schemaVersion": "rag-ime.agent-review-decision.v1",
            "ok": True,
            "sessionId": session_id,
            "runId": run_id,
            "decision": decision,
            "runtimeNotified": True,
        }

    def _recover_applied_memory_review(
        self,
        session_id: str,
        run_id: str,
    ) -> dict[str, object] | None:
        """Retire one lost review turn only after durable identity checks.

        ``pending_reviews`` is an in-memory Host map. It can disappear when
        the Python process or Host is recreated even though the cleanup run is
        already applied and the durable runtime event still identifies the
        waiting turn. Recovery must never turn a newer turn into an abort, so
        the current Host control state is compared with the exact event turn
        before the existing Runtime retirement protocol is invoked.
        """

        if self._memory_cleanup_run_status(run_id) != "applied":
            return None
        candidate_turn_ids = self._memory_review_turn_ids(
            session_id,
            run_id,
        )
        if not candidate_turn_ids:
            return None
        ensure = getattr(self.runtime, "ensure", None)
        retire = getattr(
            self.runtime,
            "retire_recovered_turn",
            None,
        )
        if not callable(ensure) or not callable(retire):
            # Older Runtime drivers have no exact-turn retirement contract;
            # refusing here is safer than falling back to a broad abort.
            return None
        ensured = ensure(session_id)
        state = ensured.get("state") if isinstance(ensured, Mapping) else None
        state = state if isinstance(state, Mapping) else {}
        active_turn = state.get("activeTurn")
        active_turn = active_turn if isinstance(active_turn, Mapping) else {}
        active_turn_id = str(active_turn.get("turnId") or "").strip()
        if (
            state.get("isIdle") is not True
            or active_turn_id not in candidate_turn_ids
        ):
            raise ValueError(
                "Pi Runtime active turn does not match the expected recovered turn"
            )
        receipt = retire(session_id, active_turn_id)
        return dict(receipt) if isinstance(receipt, Mapping) else {}

    def _memory_cleanup_run_status(self, run_id: str) -> str:
        """Read only the durable status gate needed for stale review repair."""

        db_path = getattr(self.sessions, "db_path", None)
        if db_path is None:
            return ""
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute(
                    "SELECT status FROM memory_cleanup_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
        except sqlite3.Error:
            # Preserve the existing API error if the status store cannot be
            # read; this path must not guess that a review is recoverable.
            return ""
        return str(row[0] or "") if row is not None else ""

    def _memory_review_turn_ids(
        self,
        session_id: str,
        run_id: str,
    ) -> list[str]:
        """Collect live and durable exact turn identities for one review."""

        candidates: list[str] = []
        try:
            replayed, _gap = self.events.replay(session_id)
        except Exception:
            replayed = []
        for event in replayed:
            if (
                getattr(event, "event_type", "") != "user_input_required"
                or str(getattr(event, "turn_id", "") or "").strip() == ""
            ):
                continue
            event_payload = getattr(event, "payload", {})
            if not isinstance(event_payload, Mapping):
                continue
            if (
                str(event_payload.get("requestKind") or "")
                != "memory_review"
                or str(event_payload.get("runId") or "") != run_id
            ):
                continue
            candidates.append(str(event.turn_id).strip())

        durable_lookup = getattr(
            self.sessions,
            "runtime_review_request_turn_ids",
            None,
        )
        if callable(durable_lookup):
            try:
                durable = durable_lookup(session_id, run_id)
            except Exception:
                durable = []
            if isinstance(durable, (list, tuple)):
                candidates.extend(str(value).strip() for value in durable)

        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                unique.append(candidate)
        return unique

    def decide_approval(
        self,
        approval_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        current = self.sessions.get_approval(approval_id)
        session_id = str(current["sessionId"])
        approved = decision == "approve"
        pending_in_pi = self.runtime.has_pending_approval(
            session_id,
            approval_id,
        )
        current_state = str(current.get("state") or "")
        if (
            current_state == "approved"
            and approved
            and current.get("toolId") == "memory"
            and current.get("operation")
            in _RECOVERABLE_GOVERNED_MEMORY_OPERATIONS
        ):
            self._require_current_payload(current, payload)
            executor = self._executor_provider()
            if executor is None:
                raise ValueError("approval executor is unavailable")
            try:
                current = self._claim_approval_execution(current)
            except ValueError:
                terminal = self.sessions.get_approval(approval_id)
                return self.finish_terminal(
                    terminal,
                    pending_in_pi=pending_in_pi,
                )
            if str(current.get("state") or "") != "approved":
                return self.finish_terminal(
                    current,
                    pending_in_pi=pending_in_pi,
                )
            try:
                receipt = dict(executor(current))
            except Exception as exc:
                receipt = _failed_receipt(
                    current,
                    summary="操作恢复未完成",
                    reason="recovery_failed",
                    error=exc,
                )
            final = self._complete_executor_receipt(
                current,
                receipt,
            )
            return self.finish_decision(
                final,
                pending_in_pi=pending_in_pi,
            )
        if current_state != "pending":
            return self.finish_terminal(
                current,
                pending_in_pi=pending_in_pi,
            )
        if approved and not pending_in_pi:
            raise ValueError("approval is no longer active in Pi")
        payload_sha256 = _required_text(payload, "payloadSha256")
        if payload_sha256 != str(current.get("payloadSha256") or ""):
            try:
                self.sessions.decide_approval(
                    approval_id,
                    approved=approved,
                    payload_sha256=payload_sha256,
                    decided_by="native-control-center",
                )
            except ValueError:
                terminal = self.sessions.get_approval(approval_id)
                if terminal.get("state") not in {"expired", "stale"}:
                    raise
                return self.finish_terminal(
                    terminal,
                    pending_in_pi=pending_in_pi,
                )
        if approved and self._executor_provider() is None:
            raise ValueError("approval executor is unavailable")
        try:
            decided = self.sessions.decide_approval(
                approval_id,
                approved=approved,
                payload_sha256=payload_sha256,
                decided_by="native-control-center",
            )
        except ValueError:
            terminal = self.sessions.get_approval(approval_id)
            if terminal.get("state") not in {"expired", "stale"}:
                raise
            return self.finish_terminal(
                terminal,
                pending_in_pi=pending_in_pi,
            )
        final = (
            self.execute_approved(decided)
            if approved
            else decided
        )
        return self.finish_decision(
            final,
            pending_in_pi=pending_in_pi,
        )

    def finish_decision(
        self,
        approval: Mapping[str, object],
        *,
        pending_in_pi: bool,
    ) -> dict[str, object]:
        final = dict(approval)
        approval_id = str(final.get("approvalId") or "")
        session_id = str(final.get("sessionId") or "")
        runtime_notified = False
        runtime_warning = ""
        memory_checkpoint = self.checkpoint_applied(final)
        memory_evidence: dict[str, object] = {}
        if final.get("state") == "applied":
            memory_evidence = (
                self._record_tool_receipt_evidence_safely(final)
            )
        if pending_in_pi:
            resolution_state = str(
                final.get("state") or "rejected"
            )
            try:
                self.runtime.resolve_approval(
                    session_id,
                    approval_id,
                    approved=resolution_state
                    in {"applied", "external_pending"},
                    resolution_state=resolution_state,
                )
                runtime_notified = True
            except Exception:
                runtime_warning = _append_warning(
                    runtime_warning,
                    "Pi 会话未收到审批结果，请刷新该对话",
                )
        else:
            self.events.publish(
                session_id,
                "approval_resolved",
                {
                    "approvalId": approval_id,
                    "state": str(
                        final.get("state") or "rejected"
                    ),
                    **_approval_event_identity(final),
                },
                turn_id=_approval_turn_id(final),
            )
        return {
            "schemaVersion": "rag-ime.agent-approval-decision.v1",
            "ok": True,
            "approval": final,
            "runtimeNotified": runtime_notified,
            "runtimeWarning": runtime_warning,
            "memoryCheckpoint": memory_checkpoint,
            "memoryEvidence": memory_evidence,
        }

    def auto_approve_pending(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            return self._auto_approve_pending(approval)
        except Exception as exc:
            return self._fail_automatic_approval(approval, exc)

    def _auto_approve_pending(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        approval_id = str(approval.get("approvalId") or "")
        current = self.sessions.get_approval(approval_id)
        session_id = str(current.get("sessionId") or "")
        session = self.sessions.get(session_id)
        causal = (
            current.get("causalMetadata")
            if isinstance(current.get("causalMetadata"), Mapping)
            else {}
        )
        effective_session = {
            **session,
            "roomDispatchAuthorized": False,
        }
        room_bound = bool(causal.get("roomBound"))
        room_binding_live = self._room_binding_matches_live_dispatch(
            session_id,
            causal,
        )
        if room_binding_live and not read_only_policy_active(session):
            effective_session = {
                **session,
                "roomExecutionMode": ROOM_UNRESTRICTED_EXECUTION_MODE,
                "roomDispatchAuthorized": True,
            }
        strategy = (
            APPROVAL_DENY
            if room_bound and not room_binding_live
            else approval_strategy(
                effective_session,
                tool=str(current.get("toolId") or ""),
                operation=str(current.get("operation") or ""),
                preview=(
                    current.get("preview")
                    if isinstance(current.get("preview"), Mapping)
                    else None
                ),
                risk_level=current.get("riskLevel"),
            )
        )
        jev_only = False
        if room_binding_live:
            turn_id = str(causal.get("turnId") or "")
            with self.room_turns.lock:
                key = (session_id, turn_id)
                bound_turn_id = turn_id if (
                    self.room_turns.turn_by_session_turn.get(key) == causal.get("rootId")
                    and self.room_turns.dispatch_by_session_turn.get(key) == causal.get("dispatchId")
                ) else ""
                mode = tool_approval_mode(
                    self.sessions.db_path, session_id=session_id,
                    causal=causal, bound_turn_id=bound_turn_id,
                )
            if mode == "jev_dangerous" and strategy == APPROVAL_AUTO:
                jev_only = requires_jev_approval(current, effective_session)
                if jev_only:
                    strategy = APPROVAL_MODEL
        if current.get("state") != "pending":
            return self._automatic_terminal_result(current)
        if (
            not unrestricted_workspace_policy_active(session)
            and str(approval.get("payloadSha256") or "") != str(
                current.get("payloadSha256") or ""
            )
        ):
            raise ValueError(
                "unattended approval payload no longer matches its preview"
            )

        model_decision: Mapping[str, object] | None = None
        approved = strategy == APPROVAL_AUTO
        if strategy == APPROVAL_MODEL:
            model_decision = (
                self.approval_model.decide(current, session, jev_only=True)
                if jev_only else self.approval_model.decide(current, session)
            )
            approved = model_decision.get("decision") == "approve" and model_decision.get("status") == "decided"
            decided_by = (
                "approval-model:"
                + str(model_decision.get("receiptId") or "")
            )
        elif strategy == APPROVAL_DENY:
            decided_by = (
                "execution-policy:room_dispatch_stale"
                if room_bound and not room_binding_live
                else "execution-policy:read_only"
                if read_only_policy_active(session)
                else "execution-policy:denied"
            )
        elif strategy == APPROVAL_ASK:
            return {
                "summary": "当前策略需要显式确认，操作尚未执行",
                "approvalRequired": True,
                "autoApproved": False,
                "approvalId": approval_id,
                "approval": dict(current),
                "receipt": {},
                "memoryCheckpoint": {},
                "decisionMode": "manual",
                "terminal": False,
                "retryable": False,
            }
        else:
            decided_by = (
                "execution-policy:"
                + str(
                    effective_session.get("roomExecutionMode")
                    or ("full_access" if full_access_policy_active(effective_session) else "")
                    or effective_session.get("executionMode")
                    or "per_action"
                )
            )

        if approved and self._executor_provider() is None:
            raise ValueError("approval executor is unavailable")
        try:
            decided = self.sessions.decide_approval(
                approval_id,
                approved=approved,
                payload_sha256=str(current["payloadSha256"]),
                decided_by=decided_by,
            )
        except ValueError:
            terminal = self.sessions.get_approval(approval_id)
            if terminal.get("state") not in {"expired", "stale"}:
                raise
            decision_result = self.finish_terminal(
                terminal,
                pending_in_pi=self.runtime.has_pending_approval(
                    session_id,
                    approval_id,
                ),
            )
            result = self._automatic_terminal_result(terminal)
            result["runtimeNotified"] = bool(
                decision_result.get("runtimeNotified")
            )
            result["runtimeWarning"] = str(
                decision_result.get("runtimeWarning") or ""
            )
            if model_decision is not None:
                result["decisionMode"] = "model"
                result["modelDecided"] = True
                result["approvalModelDecision"] = dict(model_decision)
            return result
        final = self.execute_approved(decided) if approved else dict(decided)
        memory_checkpoint = (
            self.checkpoint_applied(final)
            if approved
            else {}
        )
        event_payload: dict[str, object] = {
            "approvalId": approval_id,
            "state": str(final.get("state") or "failed"),
            "automatic": True,
            "decisionMode": (
                "model"
                if model_decision is not None
                else "policy"
            ),
        }
        event_payload.update(_approval_event_identity(final))
        if model_decision is not None:
            event_payload["approvalModelDecision"] = dict(model_decision)
        self.events.publish(
            session_id,
            "approval_resolved",
            event_payload,
            turn_id=_approval_turn_id(final),
        )
        receipt = (
            final.get("receipt")
            if isinstance(final.get("receipt"), Mapping)
            else {}
        )
        summary = str(
            receipt.get("summary")
            or (
                model_decision.get("rationaleSummary")
                if model_decision is not None
                else ""
            )
            or (
                "Luna Max 已拒绝这次操作"
                if model_decision is not None and not approved
                else "自动批准的操作未返回摘要"
            )
        )
        result: dict[str, object] = {
            "summary": summary,
            "approvalRequired": False,
            "autoApproved": approved and str(final.get("state") or "") in {
                "applied",
                "external_pending",
            },
            "approvalId": approval_id,
            "approval": final,
            "receipt": dict(receipt),
            "memoryCheckpoint": memory_checkpoint,
            "decisionMode": (
                "model"
                if model_decision is not None
                else "policy"
            ),
        }
        if model_decision is not None:
            result["modelDecided"] = True
            result["approvalModelDecision"] = dict(model_decision)
            if model_decision.get("status") == "failed_closed":
                result["failureCode"] = str(
                    model_decision.get("failureCode") or ""
                )
        result["terminal"] = True
        result["retryable"] = bool(jev_only and model_decision and model_decision.get("status") == "failed_closed")
        if jev_only and not approved:
            result["blocked"] = True
            result["recoveryAction"] = "retry_with_new_preview"
        result["terminalReason"] = summary
        return result

    def _room_binding_matches_live_dispatch(
        self,
        session_id: str,
        causal: Mapping[str, object],
    ) -> bool:
        if not bool(causal.get("roomBound")):
            return False
        inspect = self._active_room_dispatch_context
        try:
            live = inspect(session_id)
        except Exception:
            return False
        if not isinstance(live, Mapping):
            return False
        room_id = str(causal.get("roomId") or "").strip()
        root_id = str(causal.get("rootId") or "").strip()
        dispatch_id = str(causal.get("dispatchId") or "").strip()
        generation = int(causal.get("generation") or 0)
        live_room_id = str(live.get("roomId") or "").strip()
        live_root_id = str(live.get("rootId") or "").strip()
        live_dispatch_id = str(live.get("dispatchId") or "").strip()
        live_generation = int(live.get("generation") or 0)
        if (
            not room_id
            or not root_id
            or not dispatch_id
            or generation <= 0
            or not live_room_id
            or not live_root_id
            or not live_dispatch_id
            or live_generation <= 0
        ):
            return False
        return (
            live_room_id == room_id
            and live_root_id == root_id
            and live_dispatch_id == dispatch_id
            and live_generation == generation
        )

    def _fail_automatic_approval(
        self,
        approval: Mapping[str, object],
        error: Exception,
    ) -> dict[str, object]:
        """Close an unattended approval when its bridge fails.

        The Pi Tool call is synchronous, so an exception between preview
        creation and the automatic decision otherwise leaves a durable
        ``pending`` approval with no human owner.  That orphan is projected as
        an endlessly running Room activity.  Convert the exact approval to a
        terminal state and return an ordinary terminal Tool result instead.
        """

        approval_id = str(approval.get("approvalId") or "")
        current = self.sessions.get_approval(approval_id)
        state = str(current.get("state") or "")
        if state == "pending":
            try:
                current = self.sessions.decide_approval(
                    approval_id,
                    approved=False,
                    payload_sha256=str(current.get("payloadSha256") or ""),
                    decided_by="automatic-approval-bridge",
                )
            except ValueError:
                current = self.sessions.get_approval(approval_id)
        elif state == "approved":
            unknown_receipt = _unknown_effect_receipt(
                current,
                error=error,
            )
            try:
                current = self.sessions.fail_claimed_approval_execution(
                    approval_id,
                    receipt=unknown_receipt,
                )
            except ValueError:
                # The decision failed before the irreversible execution claim,
                # so the executor is known not to have started.
                receipt = _failed_receipt(
                    current,
                    summary="自动审批执行失败，原操作没有执行",
                    reason="automatic_approval_bridge_failed",
                    error=error,
                )
                try:
                    current = self.sessions.complete_approval(
                        approval_id,
                        state="failed",
                        receipt=receipt,
                    )
                except ValueError:
                    current = self.sessions.get_approval(approval_id)

        terminal_state = str(current.get("state") or "failed")
        current_receipt = (
            current.get("receipt")
            if isinstance(current.get("receipt"), Mapping)
            else {}
        )
        effect_unknown = bool(
            current_receipt.get("effectMayHaveOccurred") is True
            and current_receipt.get("executionOutcome") == "unknown"
            and current_receipt.get("replayAllowed") is False
        )
        summary = str(current_receipt.get("summary") or "").strip() or (
            "自动审批执行结果未知，不会自动重放"
            if effect_unknown
            else "自动审批执行失败，原操作没有执行"
        )
        event_payload: dict[str, object] = {
            "approvalId": approval_id,
            "state": terminal_state,
            "automatic": True,
            "decisionMode": "policy",
            "error": _public_error(error),
            **_approval_event_identity(current),
        }
        resolved_session_id = str(
            current.get("sessionId") or approval.get("sessionId") or ""
        )
        if not self.sessions.has_runtime_approval_resolution(
            resolved_session_id,
            approval_id,
        ):
            self.events.publish(
                resolved_session_id,
                "approval_resolved",
                event_payload,
                turn_id=_approval_turn_id(current),
            )
        result = {
            "summary": summary,
            "approvalRequired": False,
            "autoApproved": terminal_state in {
                "applied",
                "external_pending",
            },
            "approvalId": approval_id,
            "approval": dict(current),
            "receipt": dict(current_receipt),
            "memoryCheckpoint": {},
            "decisionMode": "policy",
            "terminal": terminal_state not in {"pending", "approved"},
            "retryable": False,
            "terminalReason": summary,
        }
        if terminal_state not in {"applied", "external_pending"}:
            result["failureCode"] = (
                "automatic_approval_effect_unknown"
                if effect_unknown
                else "automatic_approval_bridge_failed"
            )
        return result

    @staticmethod
    def _automatic_terminal_result(
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        state = str(approval.get("state") or "stale")
        receipt = (
            approval.get("receipt")
            if isinstance(approval.get("receipt"), Mapping)
            else {}
        )
        summary = str(receipt.get("summary") or "").strip()
        if not summary:
            summary = {
                "rejected": "这次操作已被审批终止，未执行。",
                "stale": "这次审批已取消，未执行。",
                "expired": "这次审批已过期，未执行。",
                "failed": "这次操作已失败，未执行。",
                "applied": "这次操作已完成。",
                "external_pending": "这次操作已批准，等待外部监督器完成。",
            }.get(state, "这次审批已结束，未执行。")
        model_owned = str(approval.get("decidedBy") or "").startswith(
            "approval-model:"
        )
        result: dict[str, object] = {
            "summary": summary,
            "approvalRequired": False,
            "autoApproved": state in {"applied", "external_pending"},
            "approvalId": str(approval.get("approvalId") or ""),
            "approval": dict(approval),
            "receipt": dict(receipt),
            "decisionMode": "model" if model_owned else "policy",
            "terminal": state not in {"pending", "approved"},
            "retryable": False,
            "terminalReason": summary,
        }
        if model_owned:
            result["modelDecided"] = True
        return result

    def cancel_pending_for_session(
        self,
        session_id: str,
        *,
        reason: str = "user_abort",
        turn_id: str = "",
        client_message_id: str | None = None,
        approval_ids: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        summary = self.sessions.cancel_pending_approvals(
            session_id,
            reason=reason,
            turn_id=turn_id,
            client_message_id=client_message_id,
            approval_ids=approval_ids,
        )
        cancelled_ids = [
            str(value)
            for value in summary.get("cancelledApprovalIds") or []
            if str(value).strip()
        ]
        for approval_id in cancelled_ids:
            final = self.sessions.get_approval(approval_id)
            self.events.publish(
                session_id,
                "approval_resolved",
                {
                    "approvalId": approval_id,
                    "state": str(final.get("state") or "stale"),
                    "cancelled": True,
                    "reason": reason,
                    **_approval_event_identity(final),
                },
                turn_id=_approval_turn_id(final) or str(turn_id or ""),
            )
        return {
            **dict(summary),
            "cancelledApprovalCount": len(cancelled_ids),
        }

    def execute_approved(
        self,
        decided: Mapping[str, object],
    ) -> dict[str, object]:
        approval_id = str(decided.get("approvalId") or "")
        session_id = str(decided.get("sessionId") or "")
        try:
            decided = self._claim_approval_execution(decided)
        except ValueError:
            # Stop/cancel may win after the policy decision but before the
            # external effect. The durable terminal row wins and the executor
            # must never be entered.
            return self.sessions.get_approval(approval_id)
        if str(decided.get("state") or "") != "approved":
            return dict(decided)
        try:
            executor = self._executor_provider()
            assert executor is not None
            receipt = dict(executor(decided))
        except Exception as exc:
            receipt = _failed_receipt(
                decided,
                summary="操作未执行",
                reason="execution_failed",
                error=exc,
            )
        external_action_pending = (
            receipt.get("externalActionPending") is True
        )
        if external_action_pending:
            origin_process_id = int(self._process_id_provider())
            receipt["originProcessId"] = origin_process_id
            if receipt.get("externalAction") == PORTABLE_RESTORE_ACTION:
                try:
                    receipt = materialize_portable_restore_plan(
                        approval=decided,
                        session=self.sessions.get(session_id),
                        pending_receipt=receipt,
                        origin_process_id=origin_process_id,
                    )
                except Exception as exc:
                    receipt = _failed_receipt(
                        decided,
                        summary=(
                            "外部恢复计划未创建，数据库没有发生变化"
                        ),
                        reason="external_plan_failed",
                        error=exc,
                    )
                    receipt["externalActionPending"] = False
                    external_action_pending = False
        return self._complete_executor_receipt(
            decided,
            receipt,
            external_action_pending=external_action_pending,
        )

    def _complete_executor_receipt(
        self,
        approval: Mapping[str, object],
        receipt: Mapping[str, object],
        *,
        external_action_pending: bool = False,
    ) -> dict[str, object]:
        """Persist executor truth or fail closed to a non-replayable unknown."""

        approval_id = str(approval.get("approvalId") or "")
        try:
            return self.sessions.complete_approval(
                approval_id,
                state=(
                    "external_pending"
                    if external_action_pending
                    else "applied"
                    if receipt.get("mutationApplied") is True
                    else "failed"
                ),
                receipt=receipt,
            )
        except Exception as error:
            if receipt.get("mutationApplied") is not True:
                raise
            return self.sessions.fail_claimed_approval_execution(
                approval_id,
                receipt=_unknown_effect_receipt(
                    approval,
                    error=error,
                ),
            )

    def checkpoint_applied(
        self,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        if approval.get("state") != "applied":
            return {}
        session_id = str(approval.get("sessionId") or "")
        try:
            checkpoint = self.memory_sources.checkpoint_tool_receipt(
                approval
            )
        except Exception as exc:
            checkpoint = {
                "schemaVersion": "rag-ime.agent-memory-checkpoint.v1",
                "ok": False,
                "stored": False,
                "status": "checkpoint_failed",
                "error": _public_error(exc),
            }
        if checkpoint.get("stored") is True:
            self.events.publish(
                session_id,
                "memory_checkpointed",
                {
                    "sourceRole": "tool_receipt",
                    "status": "checkpointed",
                    "summary": (
                        "已应用工具回执已保存为记忆来源，等待异步整理"
                    ),
                },
            )
        return dict(checkpoint)

    def finish_terminal(
        self,
        approval: Mapping[str, object],
        *,
        pending_in_pi: bool,
    ) -> dict[str, object]:
        approval_id = str(approval.get("approvalId") or "")
        session_id = str(approval.get("sessionId") or "")
        state = str(approval.get("state") or "stale")
        runtime_notified = False
        runtime_warning = ""
        if pending_in_pi:
            try:
                self.runtime.resolve_approval(
                    session_id,
                    approval_id,
                    approved=False,
                    resolution_state=state,
                )
                runtime_notified = True
            except Exception:
                runtime_warning = _append_warning(
                    runtime_warning,
                    "Pi 会话未收到审批终态，请刷新该对话",
                )
        else:
            self.events.publish(
                session_id,
                "approval_resolved",
                {
                    "approvalId": approval_id,
                    "state": state,
                    **_approval_event_identity(approval),
                },
                turn_id=_approval_turn_id(approval),
            )
        return {
            "schemaVersion": "rag-ime.agent-approval-decision.v1",
            "ok": True,
            "approval": dict(approval),
            "runtimeNotified": runtime_notified,
            "runtimeWarning": runtime_warning,
            "memoryCheckpoint": {},
        }

    def finalize_external(
        self,
        approval_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        result = self.external.finalize(approval_id, payload)
        approval = (
            result.get("approval")
            if isinstance(result.get("approval"), Mapping)
            else {}
        )
        causal = _exact_room_causal_metadata(approval)
        if causal is None:
            return result

        session_id = str(approval.get("sessionId") or "").strip()
        tool_call_id = str(approval.get("toolCallId") or "").strip()
        has_session_terminal = self.sessions.has_runtime_tool_terminal(
            session_id,
            tool_call_id,
            turn_id=str(causal.get("turnId") or ""),
            tool_name=str(approval.get("toolId") or ""),
            requested_after_ms=int(approval.get("requestedAtMs") or 0),
        )
        if not has_session_terminal:
            self._project_recovered_session_execution(
                approval,
                include_approval=False,
                include_tool=True,
            )

        has_room_terminal = getattr(
            self.room_events,
            "has_tool_terminal",
            None,
        )
        if not (
            callable(has_room_terminal)
            and has_room_terminal(
                str(causal["roomId"]),
                tool_call_id,
                root_id=str(causal["rootId"]),
                dispatch_id=str(causal["dispatchId"]),
            )
        ):
            self._project_abandoned_room_execution(approval)
        return result

    def approval_result(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        session_id = _required_text(payload, "sessionId")
        if payload.get("toolCallId"):
            if payload.get("approvalId"):
                raise ValueError("provide one lookup identity: approvalId or toolCallId")
            approvals = self.sessions.find_tool_call_approvals(
                session_id,
                _required_text(payload, "toolCallId"),
                _required_text(payload, "tool"),
            )
            response: dict[str, object] = {
                "schemaVersion": "rag-ime.agent-approval-result.v1",
                "ok": True,
                "lookupState": "not_found" if not approvals else "ambiguous",
            }
            if len(approvals) == 1:
                approval = approvals[0]
                receipt = approval.get("receipt")
                applied = (
                    approval.get("state") == "applied"
                    and isinstance(receipt, Mapping)
                    and receipt.get("mutationApplied") is True
                )
                response.update({
                    "lookupState": "applied" if applied else "pending" if approval.get("state") in {
                        "pending", "approved", "external_pending",
                    } else "unknown",
                    "approval": approval,
                })
            return response
        approval_id = _required_text(payload, "approvalId")
        approval = self.sessions.get_approval(approval_id)
        if approval.get("sessionId") != session_id:
            raise ValueError(
                "approval does not belong to this session"
            )
        return {
            "schemaVersion": "rag-ime.agent-approval-result.v1",
            "ok": True,
            "approval": approval,
        }

    @staticmethod
    def _require_current_payload(
        approval: Mapping[str, object],
        payload: Mapping[str, object],
    ) -> None:
        if _required_text(payload, "payloadSha256") != str(
            approval.get("payloadSha256") or ""
        ):
            raise ValueError("approval payload is stale")

def _approval_event_identity(
    approval: Mapping[str, object],
) -> dict[str, object]:
    tool_call_id = str(approval.get("toolCallId") or "").strip()
    return {"toolCallId": tool_call_id} if tool_call_id else {}


def _exact_room_causal_metadata(
    approval: Mapping[str, object],
) -> dict[str, object] | None:
    causal = (
        approval.get("causalMetadata")
        if isinstance(approval.get("causalMetadata"), Mapping)
        else {}
    )
    try:
        generation = int(causal.get("generation") or 0)
    except (TypeError, ValueError):
        return None
    room_id = str(causal.get("roomId") or "").strip()
    root_id = str(causal.get("rootId") or "").strip()
    dispatch_id = str(causal.get("dispatchId") or "").strip()
    if (
        causal.get("roomBound") is not True
        or not room_id
        or not root_id
        or not dispatch_id
        or generation <= 0
    ):
        return None
    return {
        **dict(causal),
        "roomId": room_id,
        "rootId": root_id,
        "dispatchId": dispatch_id,
        "generation": generation,
    }


def _approval_turn_id(approval: Mapping[str, object]) -> str:
    causal = approval.get("causalMetadata")
    if not isinstance(causal, Mapping):
        return ""
    return str(causal.get("turnId") or "").strip()


def _failed_receipt(
    approval: Mapping[str, object],
    *,
    summary: str,
    reason: str,
    error: BaseException,
) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.agent-operation-receipt.v1",
        "mutationApplied": False,
        "approvalId": str(approval.get("approvalId") or ""),
        "toolId": str(approval.get("toolId") or ""),
        "operation": str(approval.get("operation") or ""),
        "summary": summary,
        "reason": reason,
        "error": _public_error(error),
    }


def _unknown_effect_receipt(
    approval: Mapping[str, object],
    *,
    error: BaseException,
) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.agent-operation-receipt.v1",
        "approvalId": str(approval.get("approvalId") or ""),
        "toolId": str(approval.get("toolId") or ""),
        "operation": str(approval.get("operation") or ""),
        "summary": (
            "工具可能已经产生外部效果，但终态保存失败；"
            "结果未知，不会自动重放。"
        ),
        "reason": "terminal_persistence_failed_after_effect",
        "mutationApplied": None,
        "executionOutcome": "unknown",
        "effectMayHaveOccurred": True,
        "replayAllowed": False,
        "retryTaskAllowed": False,
        "error": _public_error(error),
    }


def _append_warning(current: str, addition: str) -> str:
    return "；".join(value for value in (current, addition) if value)


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = " ".join(str(payload.get(key) or "").split())
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value


def _public_error(error: BaseException) -> str:
    return " ".join(f"{type(error).__name__}: {error}".split())[:500]


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
