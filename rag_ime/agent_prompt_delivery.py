from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from typing import Any

from .agent_context_runtime import (
    compose_runtime_prompt,
    render_context_items,
    render_provider_context_items,
)
from .agent_execution_policy import execution_policy_prompt
from rag_ime.pi.values import (
    PiRuntimeCommandRejected,
    PiRuntimeError,
    PiRuntimeTurnConflict,
)
from .memory_maintenance_settings import memory_enabled_from_settings
from .text_utils import compact_whitespace


class AgentPromptAcceptanceUnknown(RuntimeError):
    """Pi may have accepted, but the caller did not receive an acceptance."""


class AgentPromptPostAcceptanceFailure(RuntimeError):
    """Pi accepted, but the first local durable projection failed."""


class AgentPromptDeliveryService:
    """Materialize provider-only context and hand one turn to Pi."""

    def __init__(
        self,
        *,
        sessions: Any,
        context_runtime: Any,
        runtime_provider: Callable[[], Any],
        runtime_tool_manifest: Callable[
            [Mapping[str, object]],
            list[Mapping[str, object]],
        ],
        room_public_recovery_context: Callable[[str], str],
        room_admission_gate: Callable[[str, str], Any] | None = None,
        execution_policy_context: (
            Callable[[Mapping[str, object]], str] | None
        ) = None,
        memory_enabled_provider: Callable[[], bool] | None = None,
        session_memory_enabled_provider: Callable[[str], bool] | None = None,
        personal_profile_context: Callable[[str], str] | None = None,
        memory_items_filter: Callable[[str, list[Mapping[str, object]]], list[Mapping[str, object]]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.context_runtime = context_runtime
        self._runtime_provider = runtime_provider
        self.runtime_tool_manifest = runtime_tool_manifest
        self.room_admission_gate = room_admission_gate
        self._session_memory_enabled_provider = session_memory_enabled_provider
        self._personal_profile_context = personal_profile_context
        self._memory_items_filter = memory_items_filter
        self.room_public_recovery_context = (
            room_public_recovery_context
        )
        self.execution_policy_context = (
            execution_policy_context or execution_policy_prompt
        )
        sessions_db_path = getattr(sessions, "db_path", "")
        self._memory_enabled_provider = memory_enabled_provider or (
            lambda: memory_enabled_from_settings(sessions_db_path)
        )

    @property
    def runtime(self) -> Any:
        return self._runtime_provider()

    def deliver(
        self,
        session_id: str,
        message: str,
        *,
        images: list[Mapping[str, str]] | None = None,
        client_message_id: str = "",
        source_kind: str,
        delivery: str = "prompt",
        transient_context: str = "",
        memory_bootstrap: Mapping[str, object] | None = None,
        on_accepted: (
            Callable[[Mapping[str, object]], None] | None
        ) = None,
        before_runtime: Callable[[], None] | None = None,
        room_id: str = "",
    ) -> tuple[dict[str, object], str, int]:
        trace_id = self.context_runtime.begin_trace(
            session_id,
            source_kind=source_kind,
        )
        input_node = self.context_runtime.add_trace_node(
            trace_id,
            stage="input",
            label="当前输入",
            source_kind=source_kind,
            content=message,
            summary="已接收当前回合输入",
            metadata={
                "hasImages": bool(images),
                "imageCount": len(images or []),
                "delivery": delivery,
            },
        )
        session = self.sessions.get(session_id)
        session_node = self.context_runtime.add_trace_node(
            trace_id,
            stage="session",
            label="Session 与角色",
            source_kind="gateway",
            parents=[input_node],
            summary=(
                "已解析当前 Session、角色、模式与运行偏好"
            ),
            metadata={
                "mode": str(session.get("mode") or ""),
                "roleId": str(session.get("roleId") or ""),
                "modelConfigured": bool(
                    session.get("modelProfile")
                ),
                "workspaceCount": len(
                    session.get("workspaceRoots") or []
                ),
            },
        )
        tool_count = len(self.runtime_tool_manifest(session))
        tool_node = self.context_runtime.add_trace_node(
            trace_id,
            stage="tools",
            label="动态工具目录",
            source_kind="gateway",
            parents=[session_node],
            summary="已按当前模式、权限和工作区生成工具目录",
            metadata={"toolCount": tool_count},
        )
        delivery_id = (
            f"dispatch:client:{client_message_id}"
            if client_message_id
            else f"dispatch:trace:{trace_id}"
        )
        materialized = self._materialize(
            session_id,
            delivery=delivery,
            delivery_id=delivery_id,
        )
        memory_items, async_items = _partition_items(
            materialized["items"]
        )
        task_brief_items = [item for item in async_items if item.get("sourceKind") == "primary_task_brief"]
        async_items = [item for item in async_items if item.get("sourceKind") != "primary_task_brief"]
        if self._memory_items_filter is not None:
            memory_items = self._memory_items_filter(session_id, memory_items)
        memory_context = render_provider_context_items(memory_items)
        memory_node = self._trace_memory(
            trace_id,
            session_node=session_node,
            memory_items=memory_items,
            async_items=async_items,
            char_count=len(memory_context),
            delivery=delivery,
            bootstrap=memory_bootstrap,
        )
        inbox_node = self._trace_inbox(
            trace_id,
            session_node=session_node,
            async_items=async_items,
            delivery=delivery,
        )
        runtime_message = compose_runtime_prompt(
            message,
            "\n\n".join(
                part.strip()
                for part in (
                    transient_context,
                    render_context_items(async_items),
                )
                if part and part.strip()
            ),
            session_context_prompt="\n\n".join(
                value
                for value in (
                    self.execution_policy_context(session),
                    self._profile_context(session_id, delivery=delivery),
                    render_provider_context_items(task_brief_items),
                    memory_context,
                    (
                        self.room_public_recovery_context(session_id)
                        if delivery == "prompt"
                        else ""
                    ),
                )
                if value
            ),
        )
        request_node = self.context_runtime.add_trace_node(
            trace_id,
            stage="runtime_request",
            label="Pi Runtime 请求",
            source_kind="gateway",
            parents=[
                input_node,
                tool_node,
                memory_node,
                inbox_node,
            ],
            summary="完成预算化组装并交给 Pi Runtime",
            content=runtime_message,
            metadata={
                "contextItemCount": len(
                    materialized["itemIds"]
                ),
                "toolCount": tool_count,
            },
        )
        # All catalog, context, memory, and attachment work above runs before
        # this brief admission fence. It serializes the exact Pi hand-off with
        # a durable member-removal intent, not with the global Room lock.
        guard = (self.room_admission_gate(session_id, room_id)
                 if source_kind == "room" and room_id and self.room_admission_gate is not None
                 else nullcontext())
        with guard:
            if before_runtime is not None:
                before_runtime()
            accepted, duration_ms = self._runtime_prompt(
                trace_id,
                request_node=request_node,
                session_id=session_id,
                runtime_message=runtime_message,
                images=images,
                client_message_id=client_message_id,
                delivery=delivery,
            )
            if on_accepted is not None:
                try:
                    on_accepted(accepted)
                except Exception as exc:
                    # Pi accepted, but its Host receipt is uncertain. Removal
                    # must later prove the exact existing turn has drained.
                    raise AgentPromptPostAcceptanceFailure(
                        "Pi accepted the command, but durable local acceptance "
                        "evidence could not be written"
                    ) from exc
        turn_id = str(accepted.get("turnId") or "")
        self.context_runtime.mark_delivered(
            list(materialized["itemIds"]),
            turn_id=turn_id,
            expected_delivery_id=delivery_id,
        )
        self.context_runtime.add_trace_node(
            trace_id,
            stage="runtime_result",
            label="Pi Runtime 已接受",
            source_kind="runtime",
            parents=[request_node],
            summary=(
                "运行时已建立回合，后续事件通过 Session 流返回"
            ),
            duration_ms=duration_ms,
            metadata={"accepted": True},
        )
        self.context_runtime.finalize_trace(
            trace_id,
            status="accepted",
            turn_id=turn_id,
            final_content=runtime_message,
        )
        return (
            dict(accepted),
            trace_id,
            len(materialized["itemIds"]),
        )

    def _materialize(
        self,
        session_id: str,
        *,
        delivery: str,
        delivery_id: str,
    ) -> dict[str, object]:
        if delivery != "prompt":
            return {
                "itemIds": [],
                "items": [],
                "prompt": "",
                "charCount": 0,
            }
        materialized = self.context_runtime.materialize_for_delivery(
            session_id,
            delivery_id=delivery_id,
        )
        memory_enabled = self._memory_enabled(session_id)
        if memory_enabled and not any(
            isinstance(item, Mapping)
            and item.get("sourceKind") == "personal_profile"
            for item in materialized.get("items") or []
        ):
            return materialized
        # Do not inject a previously materialized memory pack after the
        # master or conversation switch is off. The durable rows are retained
        # (and can be reused when re-enabled); only this turn's projection is
        # filtered out.
        items = [
            item
            for item in materialized.get("items") or []
            if isinstance(item, Mapping)
            # Profiles are always fresh card projections. Never replay an
            # earlier materialized copy, including after an ON/OFF cycle.
            and item.get("sourceKind") != "personal_profile"
            and (memory_enabled or item.get("sourceKind") != "memory_bootstrap")
        ]
        item_ids = [
            str(item.get("itemId") or "")
            for item in items
            if str(item.get("itemId") or "")
        ]
        prompt = render_context_items(items)
        return {
            "itemIds": item_ids,
            "items": items,
            "prompt": prompt,
            "charCount": len(prompt),
        }

    def _profile_context(self, session_id: str, *, delivery: str) -> str:
        # Steer/follow-up belong to the already active turn. The Host queues
        # their message verbatim instead of decoding the provider envelope,
        # so personal data must never be added to that transcript path.
        if delivery != "prompt" or not self._memory_enabled(session_id):
            return ""
        if self._personal_profile_context is None:
            return ""
        try:
            return self._personal_profile_context(session_id)
        except Exception:
            return ""

    def _memory_enabled(self, session_id: str) -> bool:
        try:
            return bool(self._memory_enabled_provider()) and (
                self._session_memory_enabled_provider is None
                or bool(self._session_memory_enabled_provider(session_id))
            )
        except Exception:
            return False

    def _trace_memory(
        self,
        trace_id: str,
        *,
        session_node: str,
        memory_items: list[Mapping[str, object]],
        async_items: list[Mapping[str, object]],
        char_count: int,
        delivery: str = "prompt",
        bootstrap: Mapping[str, object] | None = None,
    ) -> str:
        bootstrap = bootstrap or {}
        timeline_intent = _timeline_intent(memory_items)
        trace_metadata: dict[str, object] = {
            "itemCount": len(memory_items),
            "priority": "developer",
            "lifecycle": "session",
            "timelineRequested": (
                timeline_intent.get("requested") is True
            ),
            "timelineReason": str(
                timeline_intent.get("reason") or "none"
            ),
            "timelineMatched": "、".join(
                compact_whitespace(str(value))
                for value in timeline_intent.get("matched")
                or []
                if compact_whitespace(str(value))
            ),
            "timelineRange": str(
                timeline_intent.get("range") or ""
            ),
        }
        # Keep the source identifiers that were actually selected by the
        # recall projection.  The context trace is the existing public
        # evidence path used by the UI's Memory/Knowledge deep links; without
        # these IDs a real recall row can only show a dead summary.
        reference_keys = {
            "memory_book": "memoryBookIds",
            "memory_atom": "memoryAtomIds",
            "memory_timeline": "memoryTimelineIds",
        }
        recalled_items: dict[tuple[str, str], Mapping[str, object]] = {}
        triggers: list[str] = []
        for context_item in memory_items:
            payload = context_item.get("payload")
            if not isinstance(payload, Mapping):
                continue
            trigger = compact_whitespace(str(payload.get("trigger") or ""))
            if trigger:
                triggers.append(trigger)
            recalled = payload.get("items")
            for item in recalled if isinstance(recalled, (list, tuple)) else []:
                if not isinstance(item, Mapping):
                    continue
                identity = (
                    compact_whitespace(str(item.get("sourceType") or "")).casefold(),
                    compact_whitespace(str(item.get("sourceId") or "")),
                )
                if identity[1]:
                    recalled_items[identity] = item
        if memory_items:
            trace_metadata["hitCount"] = len(recalled_items)
        elif isinstance(bootstrap.get("sourceCount"), int):
            trace_metadata["hitCount"] = max(0, int(bootstrap["sourceCount"]))
        trigger = next(iter(triggers), str(bootstrap.get("recallTrigger") or ""))
        if trigger:
            trace_metadata["recallTrigger"] = trigger
        if bootstrap.get("status") == "recall_failed":
            recall_status = "failed"
        elif bootstrap.get("status") == "disabled":
            recall_status = "disabled"
        elif bootstrap.get("reused") is True or (
            delivery != "prompt" and bootstrap.get("status") == "ready"
        ):
            recall_status = "reused"
        elif memory_items:
            recall_status = "included" if recalled_items else "empty"
        else:
            recall_status = "unavailable"
        trace_metadata["recallStatus"] = recall_status
        source_titles = _whole_metadata_values(
            compact_whitespace(str(item.get("title") or ""))
            for item in recalled_items.values()
        )
        if source_titles:
            trace_metadata["sourceTitles"] = source_titles
        for source_type, metadata_key in reference_keys.items():
            source_ids = _whole_metadata_values(
                source_id for (kind, source_id) in recalled_items if kind == source_type
            )
            if source_ids:
                trace_metadata[metadata_key] = source_ids
        return self.context_runtime.add_trace_node(
            trace_id,
            stage="memory_recall",
            label="新 Session 个人记忆召回",
            source_kind="memory_bootstrap",
            parents=[session_node],
            disposition=(
                "failed" if recall_status == "failed"
                else "included" if memory_items else "omitted"
            ),
            summary=(
                "本轮记忆召回失败，当前消息仍可继续"
                if recall_status == "failed"
                else "记忆召回已关闭"
                if recall_status == "disabled"
                else "沿用当前会话已有记忆，没有重复查询"
                if recall_status == "reused"
                else "已加入首问与最近完整输入召回的角色可见 "
                "Timeline/Topic Book/Atom 记忆包"
                if memory_items
                else "本 Session 尚无可投递的首问记忆包"
            ),
            char_count=(
                char_count
                if memory_items and not async_items
                else 0
            ),
            reason=(
                ""
                if memory_items
                else "memory pack unavailable or active turn delivery"
            ),
            metadata=trace_metadata,
        )

    def _trace_inbox(
        self,
        trace_id: str,
        *,
        session_node: str,
        async_items: list[Mapping[str, object]],
        delivery: str,
    ) -> str:
        return self.context_runtime.add_trace_node(
            trace_id,
            stage="context_inbox",
            label="异步上下文收件箱",
            source_kind="gateway",
            parents=[session_node],
            disposition=(
                "included" if async_items else "omitted"
            ),
            summary=(
                f"本回合加入 {len(async_items)} 条分流上下文"
                if async_items
                else (
                    "排队消息沿用活动回合上下文，不重复注入"
                    if delivery != "prompt"
                    else "本回合没有待投递的异步上下文"
                )
            ),
            char_count=0,
            reason="" if async_items else "inbox empty",
            metadata={"itemCount": len(async_items)},
        )

    def _runtime_prompt(
        self,
        trace_id: str,
        *,
        request_node: str,
        session_id: str,
        runtime_message: str,
        images: list[Mapping[str, str]] | None,
        client_message_id: str,
        delivery: str,
    ) -> tuple[Mapping[str, object], int]:
        started = time.perf_counter()
        try:
            accepted = self.runtime.prompt(
                session_id,
                runtime_message,
                images=images,
                client_message_id=client_message_id,
                delivery=delivery,
            )
        except Exception as exc:
            duration_ms = _duration_ms(started)
            pre_accept_rejection = _pre_accept_runtime_rejection(exc)
            known_rejection = isinstance(
                exc,
                (
                    PiRuntimeCommandRejected,
                    PiRuntimeTurnConflict,
                ),
            ) or pre_accept_rejection is not None
            reported_error = pre_accept_rejection or exc
            try:
                self.context_runtime.add_trace_node(
                    trace_id,
                    stage="runtime_result",
                    label=(
                        "Pi Runtime 拒绝"
                        if known_rejection
                        else "Pi Runtime 接纳状态未知"
                    ),
                    source_kind="runtime",
                    disposition="failed",
                    parents=[request_node],
                    summary=(
                        "运行时未接受当前回合"
                        if known_rejection
                        else (
                            "调用未返回可证明的接纳结果；"
                            "禁止自动重新执行"
                        )
                    ),
                    duration_ms=duration_ms,
                    reason=_public_error(reported_error),
                )
                self.context_runtime.finalize_trace(
                    trace_id,
                    status="failed",
                    final_content=runtime_message,
                )
            except Exception:
                pass
            if known_rejection:
                if pre_accept_rejection is not None:
                    raise pre_accept_rejection from exc
                raise
            raise AgentPromptAcceptanceUnknown(
                "Pi acceptance is unknown because the runtime call did not "
                "return a source-proven result"
            ) from exc
        return accepted, _duration_ms(started)


def _pre_accept_runtime_rejection(
    error: BaseException,
) -> PiRuntimeCommandRejected | None:
    if (
        isinstance(error, PiRuntimeError)
        and str(error) == "Pi runtime is disabled"
    ):
        return PiRuntimeCommandRejected(
            str(error),
            host_error_code="PI_RUNTIME_DISABLED",
        )
    return None


def _partition_items(
    items: object,
) -> tuple[
    list[Mapping[str, object]],
    list[Mapping[str, object]],
]:
    values = [
        item
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, Mapping)
    ]
    memory = [
        item
        for item in values
        if item.get("sourceKind") == "memory_bootstrap"
    ]
    asynchronous = [
        item
        for item in values
        if item.get("sourceKind") != "memory_bootstrap"
    ]
    return memory, asynchronous


def _whole_metadata_values(values: Any, *, maximum: int = 240) -> str:
    """Fit complete values inside the public trace's scalar text budget."""
    selected: list[str] = []
    for value in values:
        if not value or value in selected or "," in value:
            continue
        if len(",".join([*selected, value])) <= maximum:
            selected.append(value)
    return ",".join(selected)


def _timeline_intent(
    memory_items: list[Mapping[str, object]],
) -> dict[str, object]:
    if not memory_items:
        return {}
    payload = memory_items[0].get("payload")
    if not isinstance(payload, Mapping):
        return {}
    retrieval = payload.get("retrieval")
    if not isinstance(retrieval, Mapping):
        return {}
    intent = retrieval.get("timelineIntent")
    return dict(intent) if isinstance(intent, Mapping) else {}


def _duration_ms(started: float) -> int:
    return max(
        0,
        int((time.perf_counter() - started) * 1000),
    )


def _public_error(error: BaseException) -> str:
    return (
        " ".join(str(error).split())[:240]
        or error.__class__.__name__
    )
