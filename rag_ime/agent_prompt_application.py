from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from .screen_context import screen_context

from .agent_command_receipts import (
    AgentCommandReceiptPending,
)
from .agent_prompt_delivery import (
    AgentPromptAcceptanceUnknown,
    AgentPromptPostAcceptanceFailure,
)
from .agent_prompt_support import (
    bounded_text,
    deep_search_prompt,
    optional_client_message_id,
    prompt_delivery,
    prompt_user_message_payload,
)


class AgentPromptApplicationService:
    """Own prompt idempotency, attachments, and memory checkpoints."""

    def __init__(
        self,
        *,
        sessions: Any,
        command_receipts: Any,
        configuration_store: Any,
        media: Any,
        events: Any,
        memory_sources: Any,
        memory_context: Any,
        memory_evidence: Any,
        prompt_delivery_service: Any,
        runtime_provider: Callable[[], Any],
        runtime_status: Callable[[], Mapping[str, object]],
        create_session: Callable[
            [Mapping[str, object]],
            Mapping[str, object],
        ],
        dispatch_checkpoint: Callable[..., Mapping[str, object]],
        context_source_token: object,
        transient_context_char_budget: int,
    ) -> None:
        self.sessions = sessions
        self.command_receipts = command_receipts
        self.configuration_store = configuration_store
        self.media = media
        self.events = events
        self.memory_sources = memory_sources
        self.memory_context = memory_context
        self.memory_evidence = memory_evidence
        self.prompt_delivery_service = prompt_delivery_service
        self._runtime_provider = runtime_provider
        self.runtime_status = runtime_status
        self.create_session = create_session
        self.dispatch_checkpoint = dispatch_checkpoint
        self.context_source_token = context_source_token
        self.transient_context_char_budget = (
            transient_context_char_budget
        )

    @property
    def runtime(self) -> Any:
        return self._runtime_provider()

    def _reserve_prompt_admission(
        self,
        session_id: str,
        request: Mapping[str, object],
    ) -> Any | None:
        if str(request.get("delivery") or "prompt") != "prompt":
            return None
        runtime = self.runtime
        reserve = getattr(runtime, "reserve_prompt_admission", None)
        if not callable(reserve):
            return None
        reserve(
            session_id,
            client_message_id=str(
                request.get("clientMessageId") or ""
            ),
        )
        return runtime

    @staticmethod
    def _release_prompt_admission(
        runtime: Any | None,
        session_id: str,
        request: Mapping[str, object],
    ) -> None:
        if runtime is None:
            return
        release = getattr(runtime, "release_prompt_admission", None)
        if callable(release):
            release(
                session_id,
                client_message_id=str(
                    request.get("clientMessageId") or ""
                ),
            )

    def _require_prompt_admission_active(
        self,
        session_id: str,
        *,
        client_message_id: str,
        delivery: str,
    ) -> None:
        if delivery != "prompt":
            return
        require_active = getattr(
            self.runtime,
            "require_prompt_admission_active",
            None,
        )
        if callable(require_active):
            require_active(
                session_id,
                client_message_id=client_message_id,
            )

    def prompt_rewritten_session(
        self,
        *,
        session_id: str,
        message: str,
        checkpoint_text: str,
        attachment_ids: list[str],
        client_message_id: str,
        context_source: str,
    ) -> dict[str, object]:
        """Admit the replacement after rewind, using the same Stop fence as send."""

        request = {"clientMessageId": client_message_id}
        runtime = self._reserve_prompt_admission(session_id, request)
        try:
            response = dict(self.dispatch_checkpoint(
                session_id=session_id,
                message=message,
                checkpoint_text=checkpoint_text,
                attachment_ids=attachment_ids,
                client_message_id=client_message_id,
                context_source=context_source,
            ))
        except (AgentPromptAcceptanceUnknown, AgentPromptPostAcceptanceFailure):
            # Preserve an uncertain dispatched admission until Host reconciliation.
            raise
        except Exception:
            self._release_prompt_admission(runtime, session_id, request)
            raise
        self._release_prompt_admission(runtime, session_id, request)
        return response

    def prompt(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        request = self._prompt_request(payload)
        if request["screenContext"]:
            receipt = self.media.receipt(str(request["screenContext"]["mediaId"]), session_id=session_id)
            if not str(receipt.get("mimeType") or "").startswith("image/"):
                raise ValueError("screenContext must reference an image owned by this Session")
            if str(request["screenContext"]["mediaId"]) not in request["attachmentIds"]:
                # A follow-up may refer to the same capture only after it was
                # actually delivered in this Session, not merely imported.
                if not self.media.was_attached(session_id=session_id, media_id=str(request["screenContext"]["mediaId"])):
                    raise ValueError("screenContext image must be attached on its first message")
        client_message_id = str(
            request["clientMessageId"]
        )
        if not client_message_id:
            self.sessions.require_goal_execution(session_id)
            reserved_runtime = self._reserve_prompt_admission(
                session_id,
                request,
            )
            try:
                return dict(self.dispatch_checkpoint(
                    session_id=session_id,
                    **_checkpoint_arguments(request),
                ))
            finally:
                self._release_prompt_admission(
                    reserved_runtime,
                    session_id,
                    request,
                )
        receipt_payload = {
            "message": request["message"],
            "attachments": request["attachmentIds"],
            "contextSource": request["contextSource"],
            "delivery": request["delivery"],
            "retryOfClientMessageId": request[
                "retryOfClientMessageId"
            ],
            **({"screenContext": request["screenContext"]} if request["screenContext"] else {}),
        }
        try:
            claim = self.command_receipts.begin(
                command_scope="session_prompt",
                scope_id=session_id,
                client_message_id=client_message_id,
                payload=receipt_payload,
            )
        except AgentCommandReceiptPending:
            recovered = self._recover_accepted_prompt(
                session_id=session_id,
                client_message_id=client_message_id,
                retry_of_client_message_id=str(
                    request["retryOfClientMessageId"]
                ),
                receipt_payload=receipt_payload,
            )
            if recovered is None:
                raise
            return {
                **recovered,
                "idempotentReplay": True,
            }
        if claim.replay_response is not None:
            return {
                **claim.replay_response,
                "idempotentReplay": True,
            }
        try:
            self.sessions.require_goal_execution(session_id)
            def record_acceptance(
                accepted: Mapping[str, object],
            ) -> None:
                self.command_receipts.record_acceptance_evidence(
                    claim,
                    command_scope="session_prompt",
                    scope_id=session_id,
                    client_message_id=client_message_id,
                    accepted=accepted,
                    retry_of_client_message_id=str(
                        request["retryOfClientMessageId"]
                    ),
                )

            reserved_runtime = self._reserve_prompt_admission(
                session_id,
                request,
            )
            try:
                response = dict(self.dispatch_checkpoint(
                    session_id=session_id,
                    on_accepted=record_acceptance,
                    **_checkpoint_arguments(request),
                ))
            except (
                AgentPromptAcceptanceUnknown,
                AgentPromptPostAcceptanceFailure,
            ):
                # A dispatched command without an ACK may already be running.
                # Keep its exact admission fenced until a correlated Host event
                # or durable command evidence resolves it; releasing here lets
                # a different prompt overtake the unknown side effect.
                raise
            except Exception:
                self._release_prompt_admission(
                    reserved_runtime,
                    session_id,
                    request,
                )
                raise
            else:
                self._release_prompt_admission(
                    reserved_runtime,
                    session_id,
                    request,
                )
        except Exception as exc:
            recovered = self._recover_accepted_prompt(
                session_id=session_id,
                client_message_id=client_message_id,
                retry_of_client_message_id=str(
                    request["retryOfClientMessageId"]
                ),
                receipt_payload=receipt_payload,
            )
            if recovered is not None:
                return recovered
            if isinstance(
                exc,
                (
                    AgentPromptAcceptanceUnknown,
                    AgentPromptPostAcceptanceFailure,
                ),
            ):
                raise AgentCommandReceiptPending(
                    str(exc),
                    client_message_id=client_message_id,
                    recovery_state="in_flight",
                ) from exc
            cause_code = _delivery_failure_code(exc)
            if cause_code == "PROMPT_ADMISSION_CANCELLED":
                return self.command_receipts.complete(
                    claim,
                    command_scope="session_prompt",
                    scope_id=session_id,
                    client_message_id=client_message_id,
                    response={
                        "accepted": False,
                        "cancelled": True,
                        "abortRequested": True,
                        "admissionCancelled": True,
                        "clientMessageId": client_message_id,
                        "turnId": "",
                        "piEntryId": "",
                    },
                )
            replay = self.command_receipts.fail(
                claim,
                command_scope="session_prompt",
                scope_id=session_id,
                client_message_id=client_message_id,
                error=exc,
                cause_code=cause_code,
            )
            if replay is not None:
                return {
                    **replay,
                    "idempotentReplay": True,
                }
            raise self.command_receipts.failed_receipt(
                command_scope="session_prompt",
                scope_id=session_id,
                client_message_id=client_message_id,
            ) from exc
        command_receipt: dict[str, object] = {
            "state": "accepted",
            "clientMessageId": client_message_id,
        }
        retry_of_client_message_id = str(
            request["retryOfClientMessageId"]
        )
        if retry_of_client_message_id:
            command_receipt["retryOfClientMessageId"] = (
                retry_of_client_message_id
            )
        response["commandReceipt"] = command_receipt
        try:
            return self.command_receipts.complete(
                claim,
                command_scope="session_prompt",
                scope_id=session_id,
                client_message_id=client_message_id,
                response=response,
            )
        except Exception:
            # Pi has already accepted this command and the acceptance evidence
            # was persisted before local projection/receipt completion. Recover
            # from that evidence instead of surfacing a failure that invites a
            # duplicate retry.
            recovered = self._recover_accepted_prompt(
                session_id=session_id,
                client_message_id=client_message_id,
                retry_of_client_message_id=retry_of_client_message_id,
                receipt_payload=receipt_payload,
            )
            if recovered is not None:
                return recovered
            raise

    def _recover_accepted_prompt(
        self,
        *,
        session_id: str,
        client_message_id: str,
        retry_of_client_message_id: str,
        receipt_payload: Mapping[str, object],
    ) -> dict[str, object] | None:
        evidence = self.command_receipts.pending_acceptance_evidence(
            command_scope="session_prompt",
            scope_id=session_id,
            client_message_id=client_message_id,
            payload=receipt_payload,
        )
        evidence_source = "receipt"
        if evidence is None:
            evidence = self.sessions.prompt_acceptance_evidence(
                session_id,
                client_message_id,
            )
            evidence_source = "event"
        if evidence is None:
            return None
        if str(
            evidence.get("retryOfClientMessageId") or ""
        ) != retry_of_client_message_id:
            # A mismatched lineage cannot prove acceptance of this exact
            # command. Keep the receipt pending instead of guessing or
            # executing the command again.
            return None
        command_receipt: dict[str, object] = {
            "state": "accepted",
            "clientMessageId": client_message_id,
            "recoveredFromDurableEvidence": True,
            "projectionState": "partial",
        }
        command_receipt[
            (
                "recoveredFromDurableReceipt"
                if evidence_source == "receipt"
                else "recoveredFromDurableEvent"
            )
        ] = True
        if retry_of_client_message_id:
            command_receipt["retryOfClientMessageId"] = (
                retry_of_client_message_id
            )
        response: dict[str, object] = {
            "schemaVersion": (
                "rag-ime.agent-prompt-accepted.v1"
            ),
            "ok": True,
            "accepted": True,
            "sessionId": session_id,
            "turnId": str(evidence.get("turnId") or ""),
            "piEntryId": str(
                evidence.get("piEntryId")
                or evidence.get("messageId")
                or ""
            ),
            "recoveredFromDurableEvidence": True,
            "projectionState": "partial",
            "commandReceipt": command_receipt,
        }
        response[
            (
                "recoveredFromDurableReceipt"
                if evidence_source == "receipt"
                else "recoveredFromDurableEvent"
            )
        ] = True
        return self.command_receipts.accept_pending_from_evidence(
            command_scope="session_prompt",
            scope_id=session_id,
            client_message_id=client_message_id,
            payload=receipt_payload,
            response=response,
        )

    def deep_search(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if (
            str(
                payload.get("privacyDisposition") or ""
            ).strip()
            != "allowed"
        ):
            raise ValueError(
                "deep search requires an allowed foreground privacy disposition"
            )
        question = bounded_text(
            payload.get("query"),
            maximum=4_000,
        )
        if not question:
            raise ValueError("query must not be empty")
        runtime = self.runtime_status()
        self._validate_deep_search_runtime(runtime)
        session, created = self.deep_search_session(runtime)
        prompt_message, evidence_count = deep_search_prompt(
            payload,
            question=question,
        )
        session_id = str(session["id"])
        admission_request = {
            "delivery": "prompt",
            "clientMessageId": "",
        }
        reserved_runtime = self._reserve_prompt_admission(
            session_id,
            admission_request,
        )
        try:
            accepted = dict(self.dispatch_checkpoint(
                session_id=session_id,
                message=prompt_message,
                checkpoint_text=question,
                attachment_ids=[],
                context_source="deep_search",
            ))
        finally:
            self._release_prompt_admission(
                reserved_runtime,
                session_id,
                admission_request,
            )
        return {
            "schemaVersion": "rag-ime.agent-deep-search.v1",
            "ok": True,
            "accepted": True,
            "sessionId": session_id,
            "sessionCreated": created,
            "session": self.sessions.get(
                str(session["id"])
            ),
            "questionChars": len(question),
            "evidenceCount": evidence_count,
            "controlDestination": "assistant",
            "turnId": accepted.get("turnId", ""),
        }

    def deep_search_session(
        self,
        runtime: Mapping[str, object],
    ) -> tuple[dict[str, object], bool]:
        active_session_id = str(
            runtime.get("activeSessionId") or ""
        ).strip()
        if active_session_id:
            active = self.sessions.get(active_session_id)
            if (
                active.get("mode") == "assistant"
                and active.get("status") != "archived"
            ):
                return active, False
        daily_title = (
            "记忆检索 "
            f"{datetime.now().astimezone().date().isoformat()}"
        )
        for session in self.sessions.list(
            include_archived=False,
            limit=100,
        ):
            if (
                session.get("mode") == "assistant"
                and session.get("title") == daily_title
            ):
                return session, False
        configuration = self.configuration_store.snapshot()["configuration"]
        defaults = configuration["sessionDefaults"]
        created = self.create_session(
            {
                "title": daily_title,
                "mode": "assistant",
                "roleId": str(defaults["roleId"]),
                "roleVersion": str(defaults["roleVersion"]),
                "toolProfileVersion": str(
                    defaults["toolProfileVersion"]
                ),
                "_modelRoute": "primary",
            }
        )
        return dict(created["session"]), True

    def prompt_with_checkpoint(
        self,
        *,
        session_id: str,
        message: str,
        checkpoint_text: str,
        attachment_ids: list[str],
        client_message_id: str = "",
        retry_of_client_message_id: str = "",
        context_source: str = "user",
        delivery: str = "prompt",
        transient_context: str = "",
        media_owner_room_id: str = "",
        on_accepted: (
            Callable[[Mapping[str, object]], None] | None
        ) = None,
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        self._require_prompt_admission_active(
            session_id,
            client_message_id=client_message_id,
            delivery=delivery,
        )
        bootstrap = self.memory_context.ensure_bootstrap(
            session,
            query_text=checkpoint_text,
        )
        self._require_prompt_admission_active(
            session_id,
            client_message_id=client_message_id,
            delivery=delivery,
        )
        attachments, images, attachment_context = self.media.prompt_attachments(
            "" if media_owner_room_id else session_id,
            attachment_ids,
            room_id=media_owner_room_id,
        )
        if images:
            self._validate_images(session_id, attachment_ids)
        accepted, trace_id, delivered = (
            self.prompt_delivery_service.deliver(
                session_id,
                message,
                images=images,
                client_message_id=client_message_id,
                source_kind=context_source,
                delivery=delivery,
                transient_context="\n\n".join(part for part in (attachment_context, transient_context) if part),
                memory_bootstrap=bootstrap,
                on_accepted=on_accepted,
                before_runtime=lambda: (
                    self._require_prompt_admission_active(
                        session_id,
                        client_message_id=client_message_id,
                        delivery=delivery,
                    )
                ),
            )
        )
        if not media_owner_room_id:
            self.media.bind_to_pi_entry(
                session_id=session_id,
                pi_entry_id=str(
                    accepted.get("piEntryId") or ""
                ),
                turn_id=str(accepted.get("turnId") or ""),
                media_ids=attachment_ids,
            )
        turn_id = str(accepted.get("turnId") or "")
        self._publish_user_message(
            session_id=session_id,
            turn_id=turn_id,
            message=message,
            client_message_id=client_message_id,
            retry_of_client_message_id=(
                retry_of_client_message_id
            ),
            attachments=attachments,
            delivery=delivery,
            accepted=accepted,
        )
        checkpoint = self._checkpoint_user_message(
            session_id=session_id,
            checkpoint_text=checkpoint_text,
            accepted=accepted,
        )
        evidence = (
            _room_evidence_placeholder()
            if context_source == "room"
            else self.memory_evidence.record_user(
                session_id=session_id,
                pi_entry_id=str(
                    accepted.get("piEntryId")
                    or accepted.get("turnId")
                    or ""
                ),
                turn_id=turn_id,
                text=checkpoint_text,
            )
        )
        return {
            "schemaVersion": (
                "rag-ime.agent-prompt-accepted.v1"
            ),
            "ok": True,
            "sessionId": session_id,
            "memoryCheckpoint": checkpoint,
            "memoryEvidence": evidence,
            "memoryBootstrap": bootstrap,
            "attachments": attachments,
            "contextTraceId": trace_id,
            "contextItemsDelivered": delivered,
            **accepted,
        }

    def _prompt_request(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        message = _required_text(payload, "message")
        screen_data, screen_prompt = screen_context(payload.get("screenContext"))
        raw_attachments = payload.get("attachments")
        if raw_attachments is None:
            attachment_ids: list[str] = []
        elif isinstance(raw_attachments, list):
            attachment_ids = [
                str(item) for item in raw_attachments
            ]
        else:
            raise ValueError(
                "attachments must be an array of managed mediaId values"
            )
        trusted = (
            payload.get("_contextSourceToken")
            is self.context_source_token
        )
        client_message_id = optional_client_message_id(
            payload.get("clientMessageId")
        )
        retry_of_client_message_id = optional_client_message_id(
            payload.get("retryOfClientMessageId")
        )
        if (
            retry_of_client_message_id
            and not client_message_id
        ):
            raise ValueError(
                "retryOfClientMessageId requires clientMessageId"
            )
        if (
            retry_of_client_message_id
            and retry_of_client_message_id == client_message_id
        ):
            raise ValueError(
                "retryOfClientMessageId must identify an earlier command"
            )
        return {
            "message": message,
            "screenContext": screen_data,
            "clientMessageId": client_message_id,
            "retryOfClientMessageId": (
                retry_of_client_message_id
            ),
            "delivery": prompt_delivery(
                payload.get("delivery")
            ),
            "attachmentIds": attachment_ids,
            "mediaOwnerRoomId": (
                str(payload.get("_mediaOwnerRoomId") or "").strip()[:160]
                if trusted and str(payload.get("_contextSource") or "") == "room"
                else ""
            ),
            "contextSource": (
                str(payload.get("_contextSource") or "user")
                if trusted
                else "user"
            ),
            "checkpointText": (
                str(payload.get("_checkpointText") or message)
                if trusted
                else message
            ),
            "transientContext": (
                bounded_text(
                    payload.get("_transientContext"),
                    maximum=(
                        self.transient_context_char_budget
                    ),
                )
                if trusted
                else screen_prompt
            ),
        }

    @staticmethod
    def _validate_deep_search_runtime(
        runtime: Mapping[str, object],
    ) -> None:
        capabilities = (
            runtime.get("capabilities")
            if isinstance(
                runtime.get("capabilities"),
                Mapping,
            )
            else {}
        )
        if runtime.get("enabled") is not True:
            raise ValueError("Pi runtime is disabled")
        if capabilities.get("rpc") is not True:
            raise ValueError(
                "managed Pi runtime is not installed"
            )
        if capabilities.get("modelConfigured") is False:
            raise ValueError("Pi model is not configured")
        if runtime.get("status") == "busy":
            raise ValueError(
                "Pi is already processing another task"
            )

    def _validate_images(
        self,
        session_id: str,
        attachment_ids: list[str],
    ) -> None:
        if not attachment_ids:
            return
        selected = self.runtime.model_catalog(
            session_id
        ).get("selected")
        if (
            not isinstance(selected, Mapping)
            or selected.get("supportsImages") is not True
        ):
            raise ValueError(
                "当前模型不支持图片，请切换到支持图片的模型后重试"
            )

    def _publish_user_message(
        self,
        *,
        session_id: str,
        turn_id: str,
        message: str,
        client_message_id: str,
        retry_of_client_message_id: str,
        attachments: list[dict[str, object]],
        delivery: str,
        accepted: Mapping[str, object],
    ) -> None:
        user_message = prompt_user_message_payload(
            session_id=session_id,
            turn_id=turn_id,
            message_id=(
                str(accepted.get("piEntryId") or "")
                or f"{turn_id}:user"
            ),
            text=message,
            client_message_id=client_message_id,
            retry_of_client_message_id=(
                retry_of_client_message_id
            ),
            attachments=attachments,
            delivery=delivery,
        )
        event_payload: dict[str, object] = {
            "message": user_message
        }
        if client_message_id:
            event_payload["clientMessageId"] = (
                client_message_id
            )
        if retry_of_client_message_id:
            event_payload["retryOfClientMessageId"] = (
                retry_of_client_message_id
            )
        self.events.publish(
            session_id,
            "message_completed",
            event_payload,
            turn_id=turn_id,
        )

    def _checkpoint_user_message(
        self,
        *,
        session_id: str,
        checkpoint_text: str,
        accepted: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            return self.memory_sources.checkpoint_user_message(
                session_id=session_id,
                pi_entry_id=str(
                    accepted.get("piEntryId")
                    or accepted.get("turnId")
                    or ""
                ),
                turn_id=str(
                    accepted.get("turnId") or ""
                ),
                text=checkpoint_text,
            )
        except Exception as exc:
            return {
                "schemaVersion": (
                    "rag-ime.agent-memory-checkpoint.v1"
                ),
                "ok": False,
                "stored": False,
                "status": "checkpoint_failed",
                "error": _public_error(exc),
            }


def _checkpoint_arguments(
    request: Mapping[str, object],
) -> dict[str, object]:
    return {
        "message": str(request["message"]),
        "checkpoint_text": str(
            request["checkpointText"]
        ),
        "attachment_ids": list(
            request["attachmentIds"]
        ),
        "media_owner_room_id": str(request["mediaOwnerRoomId"]),
        "client_message_id": str(
            request["clientMessageId"]
        ),
        "retry_of_client_message_id": str(
            request["retryOfClientMessageId"]
        ),
        "context_source": str(request["contextSource"]),
        "delivery": str(request["delivery"]),
        "transient_context": str(
            request["transientContext"]
        ),
    }


def _delivery_failure_code(error: BaseException) -> str:
    return str(
        getattr(error, "host_error_code", "")
        or getattr(error, "error_code", "")
        or ""
    )


def _room_evidence_placeholder() -> dict[str, object]:
    return {
        "schemaVersion": (
            "rag-ime.agent-memory-evidence-write.v1"
        ),
        "ok": True,
        "stored": False,
        "status": "recorded_as_room_event",
    }


def _required_text(
    payload: Mapping[str, object],
    key: str,
) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value


def _public_error(error: BaseException) -> str:
    return (
        " ".join(str(error).split())[:240]
        or error.__class__.__name__
    )
