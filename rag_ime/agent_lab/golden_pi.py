"""Durable Lab model calls through the resident Pi Session owner.

The adapter never interprets an Agent's prose as completion. Reopening a call
reads its original Pi settlement; it does not submit another paid prompt.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..agent_sessions import AgentSessionStore
from ..agent_tool_ids import READONLY_TOOL_PROFILE
from ..db import sqlite_connection
from ..pi.values import PiRuntimeSettlementLookupTimeout


class GoldenPiCallError(RuntimeError):
    def __init__(self, message: str, *, interrupted: bool = True,
                 completion: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.interrupted = interrupted
        # Set only from a validated Pi settlement or its persisted receipt.
        self.completion = dict(completion) if completion is not None else None


class AgentLabGoldenPiExecutor:
    def __init__(self, db_path: str | Path, *, sessions: AgentSessionStore,
                 runtime: Callable[[], Any], timeout_seconds: float = 900,
                 session_identity: Callable[[str], Mapping[str, Any]] | None = None) -> None:
        self.db_path = Path(db_path)
        self.sessions = sessions
        self.runtime = runtime
        self.timeout_seconds = timeout_seconds
        self.session_identity = session_identity
        self._locks: dict[str, threading.RLock] = {}
        self._lock = threading.Lock()

    def complete(self, *, request_id: str, model: Mapping[str, object], prompt: str,
                 on_session: Callable[[str], None], cancelled: Callable[[], bool],
                 on_progress: Callable[[dict], None] | None = None) -> dict[str, object]:
        with self._lock:
            lock = self._locks.setdefault(request_id, threading.RLock())
        with lock:
            return self._complete(request_id, model, prompt, on_session, cancelled, on_progress)

    def completed_result(self, request_id: str, model: Mapping[str, object]) -> dict[str, object] | None:
        """Read a settled call for explicit reprocessing, without new admission.

        A changed generation template is irrelevant to consuming the original
        output. The frozen model still must match. Ordinary complete() keeps
        requiring the exact original prompt and model for every replay.
        """
        if not isinstance(request_id, str) or not request_id.strip():
            raise GoldenPiCallError("评测缓存请求缺少身份。", interrupted=False)
        frozen_model = json.dumps(dict(model), ensure_ascii=False, sort_keys=True)
        try:
            with sqlite_connection(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, row_factory=sqlite3.Row) as conn:
                row = conn.execute("SELECT * FROM agent_lab_golden_model_calls WHERE request_id=?", (request_id,)).fetchone()
        except (sqlite3.Error, OSError) as exc:
            raise GoldenPiCallError("原评测回执暂不可读；尚未发起新的请求。") from exc
        if row is None:
            return None
        if row["model_json"] != frozen_model:
            raise GoldenPiCallError("此评测回执绑定了不同的模型配置。", interrupted=False)
        return self._result(row) if row["state"] == "completed" else None

    def _complete(self, request_id: str, model: Mapping[str, object], prompt: str,
                  on_session: Callable[[str], None], cancelled: Callable[[], bool],
                  on_progress: Callable[[dict], None] | None = None) -> dict[str, object]:
        if cancelled():
            raise GoldenPiCallError("任务已停止。", interrupted=False)
        if not request_id or not prompt.strip():
            raise GoldenPiCallError("评测请求缺少任务内容。", interrupted=False)
        provider = str(model.get("provider") or "").strip()
        model_id = str(model.get("model") or "").strip()
        thinking = str(model.get("thinkingLevel") or "").strip()
        if not provider or not model_id or not thinking:
            raise GoldenPiCallError("请先选择本次评测使用的模型与推理强度。", interrupted=False)
        frozen_model = json.dumps(dict(model), ensure_ascii=False, sort_keys=True)
        row = self._prepare(request_id, frozen_model, prompt, provider, model_id, thinking)
        session_id = str(row["session_id"])
        on_session(session_id)
        if row["state"] in {"completed", "failed", "cancelled"}:
            return self._terminal_result(row)
        runtime = self.runtime()
        turn_id = str(row["turn_id"])
        started = time.monotonic()
        if row["state"] == "prepared":
            try:
                selection = runtime.set_model(session_id, provider=provider, model_id=model_id)
                selected = selection.get("selected") or {}
                if selected.get("provider") != provider or selected.get("id") != model_id:
                    raise GoldenPiCallError("Runtime 返回的模型与本次选择不一致。")
                effort = runtime.set_thinking_level(session_id, level=thinking)
                if effort.get("thinkingLevel") != thinking:
                    raise GoldenPiCallError("Runtime 返回的推理强度与本次选择不一致。")
            except Exception as exc:
                raise GoldenPiCallError("模型暂时不可用；本次尚未发送，可以恢复重试。") from exc
            if cancelled():
                return self._terminal_result(self._write(request_id, state="cancelled", error="任务已停止。"))
            with sqlite_connection(self.db_path) as conn:
                claimed = conn.execute(
                    "UPDATE agent_lab_golden_model_calls SET state='admitting', updated_at_ms=? "
                    "WHERE request_id=? AND state='prepared'", (_now(), request_id)
                ).rowcount
            if claimed:
                try:
                    accepted = runtime.prompt(session_id, prompt, images=[], client_message_id=request_id)
                    turn_id = str(accepted.get("turnId") or "")
                    if not turn_id:
                        raise GoldenPiCallError("Runtime 未返回任务身份，正在保留原请求。")
                    self._write(request_id, state="accepted", turn_id=turn_id)
                except Exception:
                    return self._record_error(request_id, GoldenPiCallError("发送结果尚未确认；恢复时只核对原请求。"), model=model, started=started)
        if not turn_id:
            turn_id = self._recover_turn(runtime, session_id, request_id)
            if not turn_id:
                raise GoldenPiCallError("原请求是否被接受尚未确认；已保留任务，没有重复发送。")
            self._write(request_id, state="accepted", turn_id=turn_id)
        if cancelled():
            runtime.abort(session_id)
            return self._terminal_result(self._write(request_id, state="cancelled", error="任务已停止。"))
        progress = _PublicCallProgress(runtime, session_id, turn_id, on_progress)
        progress.emit({'stage':'model_wait'})
        try:
            deadline = time.monotonic() + max(1.0, min(3600.0, self.timeout_seconds))
            while True:
                if cancelled():
                    runtime.abort(session_id)
                    self._write(request_id, state="cancelled", error="任务已停止。")
                    raise GoldenPiCallError("任务已停止。", interrupted=False)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Pi Session turn settlement timed out")
                try:
                    progress.drain()
                    # Re-enter Pi's exact settlement.get lane at least every
                    # five seconds when an await notification is missed. This
                    # repeats only a read; Session/turn admission stays above.
                    settlement = runtime.await_turn_settled(
                        session_id, turn_id, client_message_id=request_id,
                        timeout_seconds=min(1.0 if on_progress else 5.0, remaining),
                    )
                    progress.drain()
                    break
                except PiRuntimeSettlementLookupTimeout:
                    # The Host explicitly permits another lookup of this exact
                    # identity. A transient read timeout must not stop an
                    # otherwise healthy paid turn or create a new admission.
                    # Keep the original deadline and cancellation checks.
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                except TimeoutError as exc:
                    # A normal wait expiry says the turn was not settled yet.
                    # Unclassified transport failures remain interruptions.
                    if str(exc) != "Pi Session turn settlement timed out":
                        raise
            text, usage, receipt_id = _settled_output(settlement, session_id, turn_id, request_id)
        except GoldenPiCallError as exc:
            return self._record_error(request_id, exc, model=model, started=started)
        except Exception:
            return self._record_error(request_id, GoldenPiCallError("结算回执暂不可读；恢复后继续核对原任务。"), model=model, started=started)
        if cancelled():
            return self._terminal_result(self._write(request_id, state="cancelled", error="任务已停止；未将迟到结果写入评测集。"))
        receipt = {
            "status": "completed", "transport": "pi_session", "requestId": request_id,
            "sessionId": session_id, "turnId": turn_id, "settlementReceiptId": receipt_id,
            "model": dict(model), "usage": usage,
            "elapsedMs": max(0, int((time.monotonic() - started) * 1000)),
        }
        row = self._write(request_id, state="completed", output_text=text,
                          receipt_json=json.dumps(receipt, ensure_ascii=False), error="")
        return self._terminal_result(row)

    def _record_error(self, request_id: str, error: GoldenPiCallError, *,
                      model: Mapping[str, object], started: float) -> dict[str, object]:
        fields: dict[str, object] = {
            "state": "interrupted" if error.interrupted else "failed", "error": str(error),
        }
        if error.completion is not None:
            receipt = {**error.completion["receipt"], "model": dict(model),
                       "elapsedMs": max(0, int((time.monotonic() - started) * 1000))}
            fields["receipt_json"] = json.dumps(receipt, ensure_ascii=False)
        row = self._write(request_id, **fields)
        if row["state"] in {"completed", "failed", "cancelled"}:
            return self._terminal_result(row)
        error.completion = self._completion(row)
        raise error

    def _prepare(self, request_id: str, model_json: str, prompt: str,
                 provider: str, model_id: str, thinking: str) -> sqlite3.Row:
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row, foreign_keys=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM agent_lab_golden_model_calls WHERE request_id=?", (request_id,)).fetchone()
            if row is not None:
                if row["model_json"] != model_json or row["prompt"] != prompt:
                    raise GoldenPiCallError("此任务身份已绑定其他输入；不能覆盖已有运行。", interrupted=False)
                return row
            identity = dict(self.session_identity(request_id)) if self.session_identity else {}
            allowed_tools = identity.get('allowed_tools', [])
            if allowed_tools not in ([], ['lab_research']):
                raise GoldenPiCallError('此应用请求了未绑定的工具能力。', interrupted=False)
            session = self.sessions.create(
                title=identity.get('title', "Lab · Golden 评测"), model_profile=f"{provider}/{model_id}",
                thinking_level=thinking, tool_profile_version=READONLY_TOOL_PROFILE,
                project_context_enabled=False, pi_skills_enabled=False, codex_skills_enabled=False,
                workspace_roots=[], surface_kind="extension_app", owner_app_id=identity.get('owner_app_id', "extension:agent-lab"),
                surface_key=identity.get('surface_key', f"golden.{uuid.uuid4().hex}"), _connection=conn,
            )
            session_id = str(session["id"])
            self.sessions.set_runtime_policy(
                session_id, mode="assistant", tool_profile_version=READONLY_TOOL_PROFILE,
                allowed_tools=allowed_tools, project_context_enabled=False, pi_skills_enabled=False,
                codex_skills_enabled=False, workspace_roots=[], connection=conn,
            )
            conn.execute(
                "INSERT INTO agent_lab_golden_model_calls(request_id,session_id,model_json,prompt,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?)",
                (request_id, session_id, model_json, prompt, _now(), _now()),
            )
            return conn.execute("SELECT * FROM agent_lab_golden_model_calls WHERE request_id=?", (request_id,)).fetchone()

    def _recover_turn(self, runtime: Any, session_id: str, request_id: str) -> str:
        # The transcript carries exact clientMessageId/turnId bindings even if
        # the original admission response was lost. An empty read is not proof
        # that a paid request never happened.
        try:
            snapshot = runtime.session_snapshot(session_id)
            turns = {
                str(message.get("turnId") or "")
                for message in snapshot.get("messages", [])
                if isinstance(message, Mapping) and message.get("role") == "user"
                and message.get("clientMessageId") == request_id and message.get("turnId")
            }
        except Exception:
            return ""
        return next(iter(turns)) if len(turns) == 1 else ""

    def _write(self, request_id: str, **fields: object) -> sqlite3.Row:
        allowed = {"state", "turn_id", "output_text", "receipt_json", "error"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("invalid model-call update")
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE agent_lab_golden_model_calls SET {','.join(f'{key}=?' for key in fields)},updated_at_ms=? "
                "WHERE request_id=? AND state NOT IN ('completed', 'failed', 'cancelled')",
                (*fields.values(), _now(), request_id),
            )
            # A second owner may settle this exact request while an earlier
            # observer times out. Terminal truth wins over every late write.
            row = conn.execute("SELECT * FROM agent_lab_golden_model_calls WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise GoldenPiCallError("运行回执暂不可读。")
            return row

    def _row(self, request_id: str) -> sqlite3.Row:
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            row = conn.execute("SELECT * FROM agent_lab_golden_model_calls WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise GoldenPiCallError("运行回执暂不可读。")
        return row

    @staticmethod
    def _result(row: sqlite3.Row) -> dict[str, object]:
        receipt = json.loads(row["receipt_json"])
        return {"text": row["output_text"], "sessionId": row["session_id"],
                "turnId": row["turn_id"], "usage": receipt.get("usage", {}), "receipt": receipt}

    @staticmethod
    def _completion(row: sqlite3.Row) -> dict[str, object] | None:
        receipt = json.loads(row["receipt_json"])
        if not receipt:
            return None
        return {"sessionId": row["session_id"], "turnId": row["turn_id"],
                "usage": receipt.get("usage", {}), "receipt": receipt}

    def _terminal_result(self, row: sqlite3.Row) -> dict[str, object]:
        if row["state"] == "completed":
            return self._result(row)
        raise GoldenPiCallError(str(row["error"] or "原任务未完成。"), interrupted=False,
                                completion=self._completion(row))


class _PublicCallProgress:
    """Read Pi's public event projection; settlement remains the only result."""
    def __init__(self, runtime, session_id, turn_id, callback):
        self.hub = getattr(runtime, 'events', None)
        self.session_id, self.turn_id, self.callback = session_id, turn_id, callback
        self.cursor, self.blocks, self.partial = '', {}, False

    def emit(self, value):
        if self.callback:
            try: self.callback(value)
            except Exception: pass  # Optional feedback cannot interrupt Pi.

    def drain(self):
        if not self.callback or self.hub is None: return
        try:
            events, gap = self.hub.replay(self.session_id, after_event_id=self.cursor)
            if gap:
                self.partial = True
                self.emit({'streamPartial':True,'text':''})
            text_changed = False
            for event in events:
                self.cursor = event.event_id
                if event.session_id != self.session_id or event.turn_id != self.turn_id: continue
                payload = event.payload
                if event.event_type == 'status_changed' and payload.get('phase') == 'reasoning':
                    self.emit({'stage':'thinking'})
                elif event.event_type in {'tool_started', 'tool_finished'} and payload.get('toolName') == 'lab_research':
                    args = payload.get('args')
                    operation = args.get('op') if isinstance(args, Mapping) else None
                    tool_call_id = payload.get('toolCallId')
                    if operation in {'discover', 'find', 'open', 'search'} and isinstance(tool_call_id, str) and tool_call_id:
                        # Only execution identity crosses this public projection.
                        # The App research owner journals sources/budget itself;
                        # copying tool results here would leak raw source text.
                        self.emit({'stage': 'researching', 'runtime': {
                            'sessionId': self.session_id, 'turnId': self.turn_id,
                            'toolCallId': tool_call_id[:500], 'toolName': 'lab_research', 'operation': operation,
                            'status': 'running' if event.event_type == 'tool_started' else 'failed' if payload.get('isError') else 'completed'}})
                elif event.event_type == 'text_delta' and not self.partial:
                    if payload.get('replaceBlock'): self.blocks.clear()
                    key = (str(payload.get('blockId','')), int(payload.get('contentIndex',0)))
                    prefix = '' if payload.get('replaceContent') else self.blocks.get(key,'')
                    self.blocks[key] = (prefix + str(payload.get('delta','')))[:100000]
                    text_changed = True
            if text_changed:
                self.emit({'stage':'answering','text':'\n\n'.join(self.blocks.values())[:100000]})
        except Exception:
            # A missing public event feed is not a failed model execution.
            self.partial = True
            self.emit({'streamPartial':True,'text':''})


def _settled_output(value: Mapping[str, object], session_id: str, turn_id: str,
                    request_id: str) -> tuple[str, dict[str, object], str]:
    if (value.get("schemaVersion") != "rag-ime.pi-turn-settlement.v1"
            or value.get("sessionId") != session_id or value.get("turnId") != turn_id
            or value.get("clientMessageId") != request_id):
        raise GoldenPiCallError("结算回执与本次任务身份不一致。")
    receipt = value.get("receipt")
    if (not isinstance(receipt, Mapping) or receipt.get("schemaVersion") != "pi.agent-settled.v2"
            or not value.get("runtimeSessionId") or receipt.get("sessionId") != value.get("runtimeSessionId")):
        raise GoldenPiCallError("Runtime 结算回执无效。")
    disposition = receipt.get("disposition")
    completion = _settlement_completion(receipt, session_id, turn_id, request_id)
    if disposition != "completed":
        raise GoldenPiCallError("Pi 任务没有完成。", interrupted=disposition not in {"failed", "cancelled", "aborted"},
                                completion=completion)
    if receipt.get("aborted") or receipt.get("pendingOperations") != 0:
        raise GoldenPiCallError("Pi 任务仍有未结束的操作。", completion=completion)
    message = receipt.get("finalMessage")
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        raise GoldenPiCallError("Pi 未返回最终回答。", interrupted=False, completion=completion)
    content = message.get("content", message.get("blocks", message.get("text", "")))
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                data = block.get("data")
                chunks.append(str(data.get("text") or "") if isinstance(data, Mapping) else str(block.get("text") or ""))
        text = "\n\n".join(chunks).strip()
    else:
        text = str(content or "").strip()
    if not text:
        raise GoldenPiCallError("Pi 已结束，但没有可评测的回答。", interrupted=False, completion=completion)
    usage = message.get("usage")
    return text, normalize_golden_usage(usage), str(receipt.get("receiptId") or "")


def _settlement_completion(receipt: Mapping[str, object], session_id: str, turn_id: str,
                           request_id: str) -> dict[str, object] | None:
    status = {"completed": "completed", "failed": "failed", "cancelled": "cancelled",
              "aborted": "cancelled", "suspended": "interrupted"}.get(receipt.get("disposition"))
    if status is None:
        return None
    message = receipt.get("finalMessage")
    usage = normalize_golden_usage(message.get("usage") if isinstance(message, Mapping) else None)
    # Do not copy finalMessage: it may contain private thinking blocks. Identity,
    # observed usage, and the authoritative disposition suffice for this receipt.
    observed = {"status": status, "transport": "pi_session", "requestId": request_id,
                "sessionId": session_id, "turnId": turn_id,
                "settlementReceiptId": str(receipt.get("receiptId") or ""), "usage": usage}
    return {"sessionId": session_id, "turnId": turn_id, "usage": usage, "receipt": observed}


def normalize_golden_usage(value: object) -> dict[str, object]:
    """Normalize observed tokens while keeping Pi's catalog cost an estimate."""
    if not isinstance(value, Mapping):
        return {}
    def number(item: object) -> bool:
        return isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) and item >= 0
    result: dict[str, object] = {}
    for canonical, aliases in {
        "inputTokens": ("inputTokens", "input_tokens", "input"),
        "outputTokens": ("outputTokens", "output_tokens", "output"),
        "totalTokens": ("totalTokens", "total_tokens"),
        "cacheReadTokens": ("cacheReadTokens", "cacheRead"),
        "cacheWriteTokens": ("cacheWriteTokens", "cacheWrite"),
    }.items():
        for alias in aliases:
            if number(value.get(alias)):
                result[canonical] = value[alias]
                break
    if "totalTokens" not in result and {"inputTokens", "outputTokens"} <= result.keys():
        result["totalTokens"] = sum(result.get(key, 0) for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"))
    if number(value.get("costUsd")):
        result["costUsd"] = value["costUsd"]
        result["costBasis"] = str(value.get("costBasis") or "runtime_reported")
    else:
        cost = value.get("cost")
        if isinstance(cost, Mapping) and number(cost.get("total")) and cost['total'] > 0:
            result["estimatedCostUsd"] = cost["total"]
            result["costBasis"] = "model_catalog_estimate"
        elif isinstance(cost, Mapping) and cost.get('total') == 0:
            # Pi custom providers default catalog prices to zero when none
            # were configured. That value cannot prove a free model call.
            result['costBasis'] = 'unavailable'
            result['costUnavailableReason'] = '模型目录零值不能证明实际免费；本次费用未提供。'
    return result


def _now() -> int:
    return int(time.time() * 1000)
