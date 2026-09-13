from __future__ import annotations

from .agent_runtime_driver import RuntimeSessionBranching
from .agent_sessions import AgentSessionStore
from .agent_runtime_driver import RuntimeDriverFactory
from rag_ime.rooms.store import AgentRoomStore
from .agent_delegation import AgentDelegationCoordinator
from .agent_media import AgentMediaStore
from .agent_events import AgentEventHub
from .agent_command_receipts import AgentCommandReceiptStore

from collections.abc import Callable, Mapping

from .contracts.json_schema import validate_contract


class AgentSessionBranchingService:
    """Own persisted Pi fork and in-place rewrite workflows."""

    def __init__(
        self,
        *,
        sessions: AgentSessionStore,
        runtime_provider: Callable[[], RuntimeSessionBranching],
        runtime_factory: RuntimeDriverFactory,
        rooms: AgentRoomStore,
        delegation: AgentDelegationCoordinator,
        media: AgentMediaStore,
        events: AgentEventHub,
        command_receipts: AgentCommandReceiptStore,
        prompt_with_checkpoint: Callable[..., Mapping[str, object]],
    ) -> None:
        self.sessions = sessions
        self._runtime_provider = runtime_provider
        self.runtime_factory = runtime_factory
        self.rooms = rooms
        self.delegation = delegation
        self.media = media
        self.events = events
        self.command_receipts = command_receipts
        self.prompt_with_checkpoint = prompt_with_checkpoint

    @property
    def runtime(self) -> RuntimeSessionBranching:
        return self._runtime_provider()

    def fork_candidates(self, session_id: str) -> dict[str, object]:
        self.forkable_session(session_id)
        response = {
            "schemaVersion": (
                "rag-ime.agent-session-fork-candidates.v1"
            ),
            "ok": True,
            "sessionId": session_id,
            "items": self.runtime.fork_candidates(session_id),
        }
        validate_contract(
            response,
            "agent-session-fork-candidates.v1.json",
        )
        return response

    def fork_session(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        source = self.forkable_session(session_id)
        entry_id = _required_text(payload, "entryId")
        requested_title = str(payload.get("title") or "").strip()
        title = requested_title or f"{source['title']} · 分支"
        target = self.sessions.create(
            title=title,
            mode=str(source["mode"]),
            role_id=str(source["roleId"]),
            role_version=str(source["roleVersion"]),
            role_book_revision_id=str(
                source.get("roleBookRevisionId") or ""
            ),
            model_profile=str(source["modelProfile"]),
            tool_profile_version=str(
                source["toolProfileVersion"]
            ),
            project_context_enabled=bool(
                source.get("projectContextEnabled", False)
            ),
            pi_skills_enabled=bool(
                source.get("piSkillsEnabled", False)
            ),
            codex_skills_enabled=bool(
                source.get("codexSkillsEnabled", False)
            ),
            workspace_roots=[
                str(value)
                for value in source.get("workspaceRoots") or []
            ],
            shell_policy_version=(
                str(source.get("shellPolicyVersion") or "")
                or None
            ),
            session_kind="conversation",
        )
        target_id = str(target["id"])
        allowed_tools = (
            [
                str(value)
                for value in source.get("allowedTools") or []
            ]
            if source.get("toolAllowlistMode") == "explicit"
            else None
        )
        target = self.sessions.set_runtime_policy(
            target_id,
            mode=str(source["mode"]),
            tool_profile_version=str(
                source["toolProfileVersion"]
            ),
            allowed_tools=allowed_tools,
            project_context_enabled=bool(
                source.get("projectContextEnabled", False)
            ),
            pi_skills_enabled=bool(
                source.get("piSkillsEnabled", False)
            ),
            codex_skills_enabled=bool(
                source.get("codexSkillsEnabled", False)
            ),
            workspace_roots=[
                str(value)
                for value in source.get("workspaceRoots") or []
            ],
        )
        try:
            forked = self.runtime.fork_session(
                session_id,
                target_id,
                entry_id=entry_id,
            )
        except Exception:
            try:
                self.sessions.delete(target_id)
            except KeyError:
                pass
            raise
        response = {
            "schemaVersion": (
                "rag-ime.agent-session-fork-create.v1"
            ),
            "ok": True,
            "sourceSessionId": session_id,
            "entryId": entry_id,
            "selectedText": str(
                forked.get("selectedText") or ""
            ),
            "session": (
                forked.get("session")
                or self.sessions.get(target_id)
            ),
        }
        validate_contract(
            response,
            "agent-session-fork-create.v1.json",
        )
        return response

    def rewrite_session(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        self.rewritable_session(session_id)
        entry_id = _required_text(payload, "entryId")
        message = _required_text(payload, "message")
        client_message_id = _optional_client_message_id(
            payload.get("clientMessageId")
        )
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
        receipt_payload = {
            "entryId": entry_id,
            "message": message,
            "attachments": attachment_ids,
        }
        if not client_message_id:
            return self.rewrite_session_once(
                session_id=session_id,
                entry_id=entry_id,
                message=message,
                attachment_ids=attachment_ids,
                client_message_id="",
            )
        claim = self.command_receipts.begin(
            command_scope="session_rewrite",
            scope_id=session_id,
            client_message_id=client_message_id,
            payload=receipt_payload,
        )
        if claim.replay_response is not None:
            return {
                **claim.replay_response,
                "idempotentReplay": True,
            }
        try:
            response = self.rewrite_session_once(
                session_id=session_id,
                entry_id=entry_id,
                message=message,
                attachment_ids=attachment_ids,
                client_message_id=client_message_id,
            )
        except Exception as exc:
            self.command_receipts.fail(
                claim,
                command_scope="session_rewrite",
                scope_id=session_id,
                client_message_id=client_message_id,
                error=exc,
            )
            raise
        return self.command_receipts.complete(
            claim,
            command_scope="session_rewrite",
            scope_id=session_id,
            client_message_id=client_message_id,
            response=response,
        )

    def rewrite_session_once(
        self,
        *,
        session_id: str,
        entry_id: str,
        message: str,
        attachment_ids: list[str],
        client_message_id: str,
    ) -> dict[str, object]:
        rewind = getattr(self.runtime, "rewind_session", None)
        if not callable(rewind):
            raise ValueError(
                "managed Pi runtime does not support in-place conversation rewrite"
            )
        self.sessions.require_goal_execution(session_id)
        if attachment_ids:
            _, images, _ = self.media.prompt_attachments(session_id, attachment_ids)
            if images:
                selected = self.runtime.model_catalog(session_id).get("selected")
                if not isinstance(selected, Mapping) or selected.get("supportsImages") is not True:
                    raise ValueError("当前模型不支持图片，请切换到支持图片的模型后重试")
        rewound = dict(rewind(session_id, entry_id=entry_id))
        try:
            accepted = self.prompt_with_checkpoint(
                session_id=session_id,
                message=message,
                checkpoint_text=message,
                attachment_ids=attachment_ids,
                client_message_id=client_message_id,
                context_source="conversation_rewrite",
            )
        except Exception:
            # A rewind is only durable once Pi appends the replacement prompt.
            # On admission failure the JSONL selected branch is still the old
            # leaf, so force clients back to that authoritative snapshot.
            self.events.invalidate_projection(
                session_id,
                reason="session_rewrite_failed",
            )
            raise
        # Invalidate only after Pi has appended the replacement user message.
        # Invalidating immediately after rewind lets an eager snapshot read the
        # previous durable leaf and resurrect the future branch in the UI.
        invalidate_recent = getattr(
            self.sessions,
            "invalidate_recent_message_projection",
            None,
        )
        if callable(invalidate_recent):
            invalidate_recent(session_id)
        self.events.invalidate_projection(
            session_id,
            reason="session_rewritten",
        )
        return {
            **accepted,
            "schemaVersion": "rag-ime.agent-session-rewrite.v1",
            "ok": True,
            "accepted": True,
            "sessionId": session_id,
            "entryId": entry_id,
            "rewound": rewound,
        }

    def rewritable_session(
        self,
        session_id: str,
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        self._validate_conversation_session(
            session_id,
            session,
            operation="rewritten",
        )
        return session

    def forkable_session(
        self,
        session_id: str,
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        self._validate_conversation_session(
            session_id,
            session,
            operation="forked",
        )
        return session

    def _validate_conversation_session(
        self,
        session_id: str,
        session: Mapping[str, object],
        *,
        operation: str,
    ) -> None:
        if (
            str(
                session.get("sessionKind") or "conversation"
            )
            != "conversation"
        ):
            raise ValueError(
                f"only conversation Sessions can be {operation}"
            )
        if self.rooms.participant_for_session(
            session_id,
            active_only=False,
        ) is not None:
            raise ValueError(
                f"room participant Sessions cannot be {operation}"
            )
        if self.delegation.owns_session(session_id):
            raise ValueError(
                f"subagent Sessions cannot be {operation}"
            )
        if (
            str(session.get("status") or "")
            not in {"idle", "active"}
        ):
            noun = (
                "rewrite"
                if operation == "rewritten"
                else "forks"
            )
            raise ValueError(
                f"conversation {noun} are only available for idle Sessions"
            )


def _required_text(
    payload: Mapping[str, object],
    key: str,
) -> str:
    value = " ".join(str(payload.get(key) or "").split())
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value


def _optional_client_message_id(value: object) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > 240:
        raise ValueError(
            "clientMessageId must be at most 240 characters"
        )
    return normalized
