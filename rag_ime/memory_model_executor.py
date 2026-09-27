from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from .agent_sessions import AgentSessionNotFound, AgentSessionStore
from .agent_tool_ids import MEMORY_CURATION_TOOL_PROFILE
from rag_ime.pi.values import (
    PiRuntimeCommandRejected,
    PiRuntimeSettlementLookupTimeout,
    PiRuntimeTurnConflict,
)


MEMORY_CURATION_PROFILE = "MEMORY_CURATION"
DEFAULT_MEMORY_MODEL_REFERENCE = "openai-codex/gpt-5.6-luna"
REQUIRED_MEMORY_THINKING_LEVEL = "max"
MINIMUM_MEMORY_CONTEXT_TOKENS = 272_000
MAXIMUM_MEMORY_PROMPT_CHARS = 1_000_000
DEFAULT_MEMORY_CURATION_TIMEOUT_SECONDS = 1_200.0
LATE_MEMORY_SETTLEMENT_GRACE_SECONDS = 5.0
MINIMUM_MEMORY_PROVIDER_OUTPUT_TOKENS = 16_384
DEFAULT_MEMORY_PROVIDER_OUTPUT_TOKENS = 24_000
MAXIMUM_MEMORY_PROVIDER_OUTPUT_TOKENS = 262_144


class MemoryModelUnavailable(RuntimeError):
    """The selected governed Provider/runtime cannot execute memory work."""


class MemoryModelTimeout(MemoryModelUnavailable):
    """A resumable Memory Session did not settle inside its bounded lease."""


class _ConfirmedMemoryTimeoutAbort(MemoryModelUnavailable):
    """The exact timed-out model turn settled as our own drained abort."""


class MemorySessionRuntime(Protocol):
    sessions: AgentSessionStore

    def available_models(self) -> list[dict[str, object]]: ...

    def set_model(
        self,
        session_id: str,
        *,
        provider: str,
        model_id: str,
        max_tokens: int | None = None,
    ) -> dict[str, object]: ...

    def set_thinking_level(
        self,
        session_id: str,
        *,
        level: str,
    ) -> dict[str, object]: ...

    def prompt(
        self,
        session_id: str,
        message: str,
        *,
        images: list[Mapping[str, str]] | None = None,
        client_message_id: str = "",
        delivery: str = "prompt",
    ) -> dict[str, object]: ...

    def await_turn_settled(
        self,
        session_id: str,
        turn_id: str,
        *,
        client_message_id: str,
        timeout_seconds: float,
    ) -> dict[str, object]: ...

    def session_snapshot(self, session_id: str) -> dict[str, object]: ...

    def abort(self, session_id: str) -> dict[str, object]: ...

    def close_session(self, session_id: str) -> bool: ...


@dataclass
class GovernedMemoryModelExecutor:
    """Run curation in one hidden Session owned by the resident Gateway.

    SQLite owns the frozen request and terminal response. The Pi transcript is
    only a resumable transport artifact and is retired when the curation run is
    terminal.
    """

    runtime: MemorySessionRuntime
    provider: str
    model_id: str
    thinking_level: str
    timeout_seconds: float = DEFAULT_MEMORY_CURATION_TIMEOUT_SECONDS
    sessions: AgentSessionStore | None = None
    db_path: str | Path | None = None

    def __post_init__(self) -> None:
        self.provider = _model_part(self.provider, field="provider", maximum=80)
        self.model_id = _model_part(self.model_id, field="modelId", maximum=160)
        self.thinking_level = str(self.thinking_level or "").strip().lower()
        if self.thinking_level != REQUIRED_MEMORY_THINKING_LEVEL:
            raise MemoryModelUnavailable(
                "memory curation requires thinking=max"
            )
        self.timeout_seconds = max(1.0, min(3_600.0, float(self.timeout_seconds)))
        self.sessions = self.sessions or getattr(self.runtime, "sessions", None)
        if not isinstance(self.sessions, AgentSessionStore):
            raise MemoryModelUnavailable(
                "Gateway Memory executor requires the resident Agent Session store"
            )
        resolved_db_path = self.db_path or self.sessions.db_path
        self.db_path = Path(resolved_db_path)
        self.sessions.initialize()
        self._lock = threading.RLock()
        self._active_run_id = ""
        self._active_session_id = ""
        self._accepted_requests: dict[str, tuple[str, str]] = {}
        self.selected_model = self._resolve_live_model()

    @property
    def reference(self) -> str:
        return f"{self.provider}/{self.model_id}"

    def begin_run(
        self,
        run_id: object,
        *,
        frozen_input_sha256: object = "",
    ) -> dict[str, object]:
        requested_run_id = _identifier(run_id, field="runId", maximum=240)
        frozen_hash = _sha256_value(frozen_input_sha256, allow_empty=True)
        with self._lock:
            normalized_run_id, row = self._resolve_run_attempt(
                requested_run_id,
                frozen_hash=frozen_hash,
            )
            assert row is not None
            if (
                str(row["state"]) == "running"
                and self._active_run_id != normalized_run_id
            ):
                row = self._recover_interrupted_run(row)
            if str(row["state"]) == "resumable":
                row = self._ensure_resumable_run_session(row)
            self._active_run_id = normalized_run_id
            self._active_session_id = str(row["session_id"] or "")
            if not self._active_session_id:
                raise MemoryModelUnavailable(
                    "memory curation run has no resumable internal Session"
                )
            return _run_payload(row)

    def _resolve_run_attempt(
        self,
        requested_run_id: str,
        *,
        frozen_hash: str,
    ) -> tuple[str, sqlite3.Row]:
        attempt = 1
        while attempt <= 1_000:
            candidate_run_id = _attempt_run_id(requested_run_id, attempt)
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM memory_curation_model_runs WHERE run_id = ?",
                    (candidate_run_id,),
                ).fetchone()
            if row is None:
                session = self._create_internal_session(
                    title=f"Memory curation · {_short_run_label(candidate_run_id)}",
                )
                timestamp = _now_ms()
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO memory_curation_model_runs(
                            run_id, session_id, profile, provider, model_id,
                            thinking_level, frozen_input_sha256, state,
                            created_at_ms, updated_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                        """,
                        (
                            candidate_run_id,
                            str(session["id"]),
                            MEMORY_CURATION_PROFILE,
                            self.provider,
                            self.model_id,
                            self.thinking_level,
                            frozen_hash,
                            timestamp,
                            timestamp,
                        ),
                    )
                    row = conn.execute(
                        "SELECT * FROM memory_curation_model_runs WHERE run_id = ?",
                        (candidate_run_id,),
                    ).fetchone()
                assert row is not None
                return candidate_run_id, row
            if not self._run_row_matches_model_profile(row):
                if str(row["state"]) == "running":
                    raise MemoryModelUnavailable(
                        "existing Memory run is active under a different frozen model profile"
                    )
                # Keep the old model's accepted requests and receipts frozen.
                # A new configured model starts at the next stable attempt id.
                attempt += 1
                continue
            self._validate_run_row(row, frozen_hash=frozen_hash)
            if str(row["state"]) not in {"failed", "cancelled"}:
                return candidate_run_id, row
            attempt += 1
        raise MemoryModelUnavailable(
            "Memory run exceeded the bounded successor-attempt limit"
        )

    def _recover_interrupted_run(self, row: sqlite3.Row) -> sqlite3.Row:
        run_id = str(row["run_id"])
        session_id = str(row["session_id"] or "")
        error = "memory_curation_interrupted"
        if session_id:
            try:
                self.runtime.abort(session_id)
            except Exception as exc:
                error = f"{error}: {_public_error(exc)}"
        replacement = self._create_internal_session(
            title=(
                "Memory curation recovery · "
                f"{_short_run_label(run_id)}"
            ),
        )
        replacement_session_id = str(replacement["id"])
        timestamp = _now_ms()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_requests
                SET state = 'resumable', last_error = ?, updated_at_ms = ?
                WHERE run_id = ? AND state = 'running'
                """,
                (error, timestamp, run_id),
            )
            conn.execute(
                """
                UPDATE memory_curation_model_runs
                SET session_id = ?, state = 'resumable', last_error = ?,
                    updated_at_ms = ?
                WHERE run_id = ? AND state = 'running'
                """,
                (replacement_session_id, error, timestamp, run_id),
            )
            recovered = conn.execute(
                "SELECT * FROM memory_curation_model_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if recovered is None:
            raise MemoryModelUnavailable("interrupted Memory run disappeared")
        return recovered

    def _ensure_resumable_run_session(self, row: sqlite3.Row) -> sqlite3.Row:
        session_id = str(row["session_id"] or "")
        try:
            session = self.sessions.get(session_id) if session_id else None
        except AgentSessionNotFound:
            session = None
        if session is not None and str(session["status"]) != "archived":
            return row
        replacement = self._create_internal_session(
            title=f"Memory curation recovery · {_short_run_label(str(row['run_id']))}",
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_runs
                SET session_id = ?, updated_at_ms = ?
                WHERE run_id = ? AND state = 'resumable'
                """,
                (str(replacement["id"]), _now_ms(), str(row["run_id"])),
            )
            recovered = conn.execute(
                "SELECT * FROM memory_curation_model_runs WHERE run_id = ?",
                (str(row["run_id"]),),
            ).fetchone()
        if recovered is None:
            raise MemoryModelUnavailable("resumable Memory run disappeared")
        return recovered

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        phase: str = "model-call",
        isolated: bool = False,
    ) -> dict[str, object]:
        normalized_messages = _messages(messages)
        frozen_json = json.dumps(
            normalized_messages,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        input_sha256 = hashlib.sha256(frozen_json.encode("utf-8")).hexdigest()
        normalized_phase = _identifier(phase, field="phase", maximum=80)
        with self._lock:
            if not self._active_run_id:
                self.begin_run(f"memory-model-run:{uuid.uuid4()}")
            run_id = self._active_run_id
            session_id = self._active_session_id
            request = self._prepare_request(
                run_id=run_id,
                phase=normalized_phase,
                input_sha256=input_sha256,
                messages_json=frozen_json,
                isolated=bool(isolated),
                max_tokens=max_tokens,
            )
            if str(request["state"]) == "completed":
                return self._response_from_request(request)
            request_id = str(request["request_id"])
            replay_recovery = _json_object(request["receipt_json"]).get(
                "replayRecovery"
            )
            if not isinstance(replay_recovery, Mapping):
                replay_recovery = {}
            session_id = str(request["session_id"] or session_id)
            if not session_id:
                raise MemoryModelUnavailable(
                    "memory curation request has no resumable internal Session"
                )

            prompt = _session_prompt(
                normalized_messages,
                request_id=str(request["request_id"]),
                input_sha256=input_sha256,
                max_tokens=max_tokens,
            )
            if len(prompt) > MAXIMUM_MEMORY_PROMPT_CHARS:
                raise MemoryModelUnavailable(
                    "memory curation packet exceeds the managed Session input limit"
                )

            model_receipt: dict[str, object] = {}
            thinking_receipt: dict[str, object] = {}
            catalog_max_tokens = int(self.selected_model.get("maxTokens") or 0)
            provider_max_tokens = _memory_provider_max_tokens(
                max_tokens,
                catalog_max_tokens=catalog_max_tokens,
            )
            started = time.monotonic()
            accepted_turn_id = ""
            admission_confirmed = False
            prompt_attempted = False
            pre_admission_rejection_code = ""

            def resumable_receipt(
                cancellation: Mapping[str, object],
            ) -> dict[str, object]:
                receipt: dict[str, object] = {"cancellation": dict(cancellation)}
                if replay_recovery:
                    receipt["replayRecovery"] = dict(replay_recovery)
                if admission_confirmed:
                    receipt["admission"] = _memory_admission_payload(
                        session_id=session_id,
                        turn_id=accepted_turn_id,
                        request_id=request_id,
                    )
                elif pre_admission_rejection_code:
                    receipt["admission"] = {
                        "schemaVersion": "rag-ime.memory-admission.v1",
                        "status": "rejected",
                        "reasonCode": pre_admission_rejection_code,
                        "sessionId": session_id,
                        "clientMessageId": request_id,
                    }
                elif prompt_attempted:
                    receipt["admission"] = {
                        "schemaVersion": "rag-ime.memory-admission.v1",
                        "status": "unknown",
                        "sessionId": session_id,
                        "clientMessageId": request_id,
                    }
                return receipt

            try:
                model_receipt = dict(
                    self.runtime.set_model(
                        session_id,
                        provider=self.provider,
                        model_id=self.model_id,
                        max_tokens=provider_max_tokens,
                    )
                )
                thinking_receipt = dict(
                    self.runtime.set_thinking_level(
                        session_id,
                        level=self.thinking_level,
                    )
                )
                _verify_runtime_receipts(
                    model_receipt,
                    thinking_receipt,
                    provider=self.provider,
                    model_id=self.model_id,
                    thinking_level=self.thinking_level,
                    max_tokens=provider_max_tokens,
                )
                self._mark_request_running(
                    request_id=request_id,
                )
                prompt_attempted = True
                try:
                    accepted = self.runtime.prompt(
                        session_id,
                        prompt,
                        images=[],
                        client_message_id=request_id,
                    )
                except PiRuntimeTurnConflict as exc:
                    pre_admission_rejection_code = exc.error_code
                    raise
                except PiRuntimeCommandRejected as exc:
                    pre_admission_rejection_code = exc.host_error_code
                    raise
                accepted_turn_id = str(accepted.get("turnId") or "").strip()
                admission_confirmed = (
                    accepted.get("accepted") is True or bool(accepted_turn_id)
                )
                if admission_confirmed:
                    self._accepted_requests[request_id] = (
                        session_id,
                        accepted_turn_id,
                    )
                if not accepted_turn_id:
                    raise MemoryModelUnavailable(
                        "managed Pi did not return a Memory Session turn id"
                    )
                for bind_attempt in range(2):
                    try:
                        self._bind_turn(
                            request_id=request_id,
                            turn_id=accepted_turn_id,
                        )
                        break
                    except sqlite3.OperationalError as exc:
                        if "unable to open database file" not in str(exc).lower():
                            raise
                        if bind_attempt == 0:
                            # Pi has already accepted the prompt. Retry only
                            # this idempotent binding once; if storage remains
                            # unavailable, continue settling with the local
                            # admission identity and persist it in the next
                            # terminal/resumable write that reaches SQLite.
                            time.sleep(0.05)
                settlement = self.runtime.await_turn_settled(
                    session_id,
                    accepted_turn_id,
                    client_message_id=request_id,
                    timeout_seconds=self.timeout_seconds,
                )
                terminal = _memory_terminal_from_settlement(
                    settlement,
                    session_id=session_id,
                    turn_id=accepted_turn_id,
                    client_message_id=request_id,
                )
                if terminal["state"] == "failed":
                    raise MemoryModelUnavailable(
                        str(terminal.get("error") or "Memory Session turn failed")
                    )
                output_text = str(terminal.get("text") or "").strip()
                if not output_text:
                    raise MemoryModelUnavailable(
                        "Memory Session completed without assistant JSON"
                    )
                elapsed_ms = max(0, int((time.monotonic() - started) * 1_000))
                receipt = {
                    "schemaVersion": "rag-ime.memory-curation-model-receipt.v1",
                    "profile": MEMORY_CURATION_PROFILE,
                    "transport": "gateway_internal_session",
                    "runId": run_id,
                    "requestId": request_id,
                    "sessionId": session_id,
                    "turnId": accepted_turn_id,
                    "provider": self.provider,
                    "modelId": self.model_id,
                    "thinkingLevel": self.thinking_level,
                    "contextWindow": int(self.selected_model.get("contextWindow") or 0),
                    "maxTokens": provider_max_tokens,
                    "catalogMaxTokens": catalog_max_tokens,
                    "inputSha256": input_sha256,
                    "inputChars": len(prompt),
                    "elapsedMs": elapsed_ms,
                    "usage": dict(terminal.get("usage") or {}),
                    "modelSelection": model_receipt,
                    "thinkingSelection": thinking_receipt,
                }
                if replay_recovery:
                    receipt["replayRecovery"] = dict(replay_recovery)
                completed = self._complete_request(
                    request_id=request_id,
                    turn_id=accepted_turn_id,
                    output_text=output_text,
                    receipt=receipt,
                )
                self._accepted_requests.pop(request_id, None)
                return self._response_from_request(completed)
            except PiRuntimeSettlementLookupTimeout as exc:
                # A failed receipt read says nothing about the accepted
                # model turn's outcome. Preserve its identity for recovery
                # without cancelling a potentially healthy Provider run.
                self._mark_request_resumable(
                    request_id=request_id,
                    turn_id=accepted_turn_id,
                    error="memory_settlement_lookup_timeout",
                    receipt=resumable_receipt({
                        "requested": False,
                        "reason": "settlement_lookup_timeout",
                    }),
                )
                raise MemoryModelUnavailable(
                    "Memory Session settlement lookup timed out; "
                    "the accepted turn remains unresolved and was not replayed"
                ) from exc
            except TimeoutError as exc:
                cancellation = self._cancel_request_session(session_id)
                self._mark_request_resumable(
                    request_id=request_id,
                    turn_id=accepted_turn_id,
                    error="memory_curation_timeout",
                    receipt=resumable_receipt(cancellation),
                )
                # Cancellation itself can take long enough for the exact turn
                # to publish a durable successful settlement. Reconcile once
                # after that boundary before surfacing a timeout; otherwise a
                # completed model response is stranded as ``resumable`` until
                # another maintenance run happens to revisit the same input.
                with self._connect() as conn:
                    late_request = conn.execute(
                        "SELECT * FROM memory_curation_model_requests "
                        "WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                if late_request is not None and accepted_turn_id:
                    try:
                        recovered = self._recover_resumable_request(
                            late_request,
                            max_tokens=max_tokens,
                            recovery_timeout_seconds=(
                                LATE_MEMORY_SETTLEMENT_GRACE_SECONDS
                            ),
                        )
                    except MemoryModelUnavailable:
                        pass
                    else:
                        return self._response_from_request(recovered)
                raise MemoryModelTimeout(
                    "Memory Session timed out; the frozen request remains resumable"
                ) from exc
            except MemoryModelUnavailable as exc:
                cancellation = self._cancel_request_session(session_id)
                self._mark_request_resumable(
                    request_id=request_id,
                    turn_id=accepted_turn_id,
                    error=_public_error(exc),
                    receipt=resumable_receipt(cancellation),
                )
                raise
            except Exception as exc:
                cancellation = self._cancel_request_session(session_id)
                self._mark_request_resumable(
                    request_id=request_id,
                    turn_id=accepted_turn_id,
                    error=_public_error(exc),
                    receipt=resumable_receipt(cancellation),
                )
                raise MemoryModelUnavailable(
                    f"selected memory model request failed ({self.reference}): "
                    f"{_public_error(exc)}"
                ) from exc

    def _cancel_request_session(self, session_id: str) -> dict[str, object]:
        try:
            return dict(self.runtime.abort(session_id))
        except Exception as exc:
            return {"error": _public_error(exc)}

    def finish_run(self, *, state: str = "completed") -> dict[str, object]:
        normalized_state = str(state or "").strip().lower()
        if normalized_state not in {"completed", "failed", "cancelled"}:
            raise ValueError("terminal memory model run state is invalid")
        with self._lock:
            run_id = self._active_run_id
            if not run_id:
                return {}
            with self._connect() as conn:
                request_sessions = [
                    str(row["session_id"] or "")
                    for row in conn.execute(
                        """
                        SELECT DISTINCT session_id
                        FROM memory_curation_model_requests
                        WHERE run_id = ? AND session_id != ''
                        ORDER BY session_id
                        """,
                        (run_id,),
                    ).fetchall()
                ]
            session_ids = list(
                dict.fromkeys(
                    session_id
                    for session_id in [self._active_session_id, *request_sessions]
                    if session_id
                )
            )
            timestamp = _now_ms()
            try:
                for session_id in session_ids:
                    self.runtime.close_session(session_id)
                    self.sessions.retire_system_internal(
                        session_id,
                        tool_profile_version=MEMORY_CURATION_TOOL_PROFILE,
                        updated_at_ms=timestamp,
                    )
            except Exception as exc:
                self._set_run_error(
                    run_id,
                    state="resumable",
                    error=f"memory_session_retirement_failed: {_public_error(exc)}",
                )
                raise MemoryModelUnavailable(
                    "Memory run could not retire its internal Session; "
                    "the database run remains resumable"
                ) from exc
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE memory_curation_model_runs
                    SET state = ?, last_error = '', updated_at_ms = ?,
                        completed_at_ms = ?
                    WHERE run_id = ?
                    """,
                    (normalized_state, timestamp, timestamp, run_id),
                )
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM memory_curation_model_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            self._accepted_requests.clear()
            self._active_run_id = ""
            self._active_session_id = ""
            return _run_payload(row) if row is not None else {}

    def fail_run(self, error: BaseException) -> dict[str, object]:
        with self._lock:
            if not self._active_run_id:
                return {}
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT state FROM memory_curation_model_runs WHERE run_id = ?",
                    (self._active_run_id,),
                ).fetchone()
            if row is not None and str(row["state"]) == "resumable":
                return self.run_status(self._active_run_id)
            self._set_run_error(
                self._active_run_id,
                state="failed",
                error=_public_error(error),
            )
            return self.finish_run(state="failed")

    def cancel_run(self) -> dict[str, object]:
        with self._lock:
            run_id = self._active_run_id
            session_id = self._active_session_id
            if not run_id:
                return {}
            if session_id:
                try:
                    self.runtime.abort(session_id)
                except Exception:
                    pass
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE memory_curation_model_requests
                    SET state = 'cancelled', updated_at_ms = ?, completed_at_ms = ?
                    WHERE run_id = ? AND state IN ('prepared', 'running', 'resumable')
                    """,
                    (_now_ms(), _now_ms(), run_id),
                )
            return self.finish_run(state="cancelled")

    def run_status(self, run_id: object) -> dict[str, object]:
        normalized = _identifier(run_id, field="runId", maximum=240)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_curation_model_runs WHERE run_id = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                return {}
            requests = conn.execute(
                """
                SELECT request_id, phase, ordinal, input_sha256, input_chars,
                       session_id, state, turn_id, attempt_count, receipt_json, last_error,
                       created_at_ms, updated_at_ms, completed_at_ms
                FROM memory_curation_model_requests
                WHERE run_id = ?
                ORDER BY ordinal
                """,
                (normalized,),
            ).fetchall()
        payload = _run_payload(row)
        payload["requests"] = [_request_public_payload(item) for item in requests]
        return payload

    def close(self) -> None:
        """Release this adapter without ever stopping the shared Gateway Host."""

        with self._lock:
            self._accepted_requests.clear()
            self._active_run_id = ""
            self._active_session_id = ""

    def _resolve_live_model(self) -> dict[str, object]:
        try:
            models = self.runtime.available_models()
        except Exception as exc:
            raise MemoryModelUnavailable(
                f"selected memory runtime is unavailable for {self.reference}: "
                f"{_public_error(exc)}"
            ) from exc
        selected = next(
            (
                dict(model)
                for model in models
                if isinstance(model, Mapping)
                and str(model.get("provider") or "") == self.provider
                and str(model.get("id") or "") == self.model_id
            ),
            None,
        )
        if selected is None:
            raise MemoryModelUnavailable(
                f"selected memory model is unavailable: {self.reference}"
            )
        levels = selected.get("thinkingLevels")
        if not isinstance(levels, list) or self.thinking_level not in {
            str(level) for level in levels
        }:
            raise MemoryModelUnavailable(
                f"selected memory model does not support thinking level "
                f"{self.thinking_level}: {self.reference}"
            )
        context_window = int(selected.get("contextWindow") or 0)
        if context_window < MINIMUM_MEMORY_CONTEXT_TOKENS:
            raise MemoryModelUnavailable(
                f"selected memory model context window is below "
                f"{MINIMUM_MEMORY_CONTEXT_TOKENS}: {context_window}"
            )
        catalog_max_tokens = int(selected.get("maxTokens") or 0)
        if catalog_max_tokens < MINIMUM_MEMORY_PROVIDER_OUTPUT_TOKENS:
            raise MemoryModelUnavailable(
                "selected memory model output budget is below "
                f"{MINIMUM_MEMORY_PROVIDER_OUTPUT_TOKENS}: {catalog_max_tokens}"
            )
        return selected

    def _recover_resumable_request(
        self,
        row: sqlite3.Row,
        *,
        max_tokens: int | None,
        recovery_timeout_seconds: float = 1.0,
    ) -> sqlite3.Row:
        session_id = str(row["session_id"] or "")
        turn_id = str(row["turn_id"] or "")
        request_id = str(row["request_id"] or "")
        if not request_id:
            raise MemoryModelUnavailable(
                "accepted Memory request lost its request identity; it was not replayed"
            )
        if not session_id:
            raise MemoryModelUnavailable(
                "accepted Memory request lost its Session identity; it was not replayed"
            )
        if not turn_id:
            raise MemoryModelUnavailable(
                "accepted Memory request has no recoverable turn id; it was not replayed"
            )
        try:
            settlement = self.runtime.await_turn_settled(
                session_id,
                turn_id,
                client_message_id=request_id,
                timeout_seconds=min(
                    max(1.0, float(recovery_timeout_seconds)),
                    self.timeout_seconds,
                ),
            )
            terminal = _memory_terminal_from_settlement(
                settlement,
                session_id=session_id,
                turn_id=turn_id,
                client_message_id=request_id,
            )
        except PiRuntimeSettlementLookupTimeout as exc:
            self._mark_request_resumable(
                request_id=request_id,
                turn_id=turn_id,
                error="memory_settlement_lookup_timeout",
                receipt=_json_object(row["receipt_json"]),
            )
            raise MemoryModelUnavailable(
                "previously accepted Memory Session settlement lookup timed out; "
                "the turn remains unresolved and the request was not replayed"
            ) from exc
        except TimeoutError as exc:
            raise MemoryModelTimeout(
                "previously accepted Memory Session turn remains unresolved; "
                "the request was not replayed"
            ) from exc
        except MemoryModelUnavailable as exc:
            raise MemoryModelUnavailable(
                f"{_public_error(exc)}; the request was not replayed"
            ) from exc
        except Exception as exc:
            raise MemoryModelUnavailable(
                "previously accepted Memory Session turn could not be recovered; "
                f"the request was not replayed ({_public_error(exc)})"
            ) from exc
        if terminal["state"] != "completed":
            if (
                str(terminal.get("error") or "").strip().lower() == "aborted"
                and _own_timeout_abort_confirmed(row)
            ):
                raise _ConfirmedMemoryTimeoutAbort(
                    "the timed-out Memory turn settled as our own abort"
                )
            raise MemoryModelUnavailable(
                "previously accepted Memory Session turn failed; "
                "the request was not replayed: "
                + str(terminal.get("error") or "unknown terminal failure")
            )
        output_text = str(terminal.get("text") or "").strip()
        if not output_text:
            raise MemoryModelUnavailable(
                "previously accepted Memory Session completed without assistant JSON; "
                "the request was not replayed"
            )

        catalog_max_tokens = int(self.selected_model.get("maxTokens") or 0)
        provider_max_tokens = _memory_provider_max_tokens(
            max_tokens,
            catalog_max_tokens=catalog_max_tokens,
        )
        receipt = {
            "schemaVersion": "rag-ime.memory-curation-model-receipt.v1",
            "profile": MEMORY_CURATION_PROFILE,
            "transport": "gateway_internal_session",
            "runId": str(row["run_id"]),
            "requestId": request_id,
            "sessionId": session_id,
            "turnId": turn_id,
            "provider": self.provider,
            "modelId": self.model_id,
            "thinkingLevel": self.thinking_level,
            "contextWindow": int(self.selected_model.get("contextWindow") or 0),
            "maxTokens": provider_max_tokens,
            "catalogMaxTokens": catalog_max_tokens,
            "inputSha256": str(row["input_sha256"] or ""),
            "inputChars": int(row["input_chars"] or 0),
            "elapsedMs": max(
                0,
                int(
                    dict(settlement.get("receipt") or {}).get("settledAtMs")
                    or 0
                )
                - int(row["created_at_ms"] or 0),
            ),
            "usage": dict(terminal.get("usage") or {}),
            "modelSelection": {},
            "thinkingSelection": {},
            "recoveredSettlement": True,
        }
        previous_receipt = _json_object(row["receipt_json"])
        for key in ("admission", "cancellation", "replayRecovery"):
            previous_value = previous_receipt.get(key)
            if isinstance(previous_value, Mapping):
                receipt[key] = dict(previous_value)
        completed = self._complete_request(
            request_id=request_id,
            turn_id=turn_id,
            output_text=output_text,
            receipt=receipt,
        )
        self._accepted_requests.pop(request_id, None)
        self._set_run_error(self._active_run_id, state="running", error="")
        return completed

    def _unrecoverable_request_session_reason(
        self,
        row: sqlite3.Row,
    ) -> str:
        """Return a replay-safe reason only when the old Runtime is gone.

        A live or merely idle Session with its durable transcript can still
        publish a late terminal result, so its admitted request remains
        recovery-only. An archived/missing internal Session, or an idle/faulted
        Session whose transcript disappeared, cannot do that: the frozen model
        request has no side effects of its own and may be replayed in a fresh
        hidden Session while the previous admission remains attached to the
        durable receipt.
        """

        session_id = str(row["session_id"] or "")
        if not session_id:
            return "runtime_session_missing"
        try:
            session = self.sessions.get(session_id)
        except AgentSessionNotFound:
            return "runtime_session_missing"
        status = str(session["status"] or "")
        if status == "archived":
            return "runtime_session_archived"
        transcript = str(session["sessionFile"] or "").strip()
        if (
            status in {"idle", "faulted"}
            and transcript
            and not Path(transcript).is_file()
        ):
            return "runtime_transcript_missing"
        return ""

    def _reprepare_unrecoverable_accepted_request(
        self,
        row: sqlite3.Row,
        *,
        isolated: bool,
        reason_code: str,
    ) -> sqlite3.Row:
        previous_session_id = str(row["session_id"] or "")
        previous_turn_id = str(row["turn_id"] or "")
        previous_receipt = _json_object(row["receipt_json"])
        previous_admission = previous_receipt.get("admission")
        replay_recovery: dict[str, object] = {
            "schemaVersion": "rag-ime.memory-request-replay-recovery.v1",
            "reasonCode": reason_code,
            "previousSessionId": previous_session_id,
            "previousTurnId": previous_turn_id,
        }
        if isinstance(previous_admission, Mapping):
            replay_recovery["previousAdmission"] = dict(previous_admission)

        replacement_session_id = "" if isolated else self._active_session_id
        if replacement_session_id:
            try:
                replacement_session = self.sessions.get(replacement_session_id)
            except AgentSessionNotFound:
                replacement_session = None
            if (
                replacement_session is None
                or str(replacement_session["status"] or "") == "archived"
                or replacement_session_id == previous_session_id
            ):
                replacement_session_id = ""
        if not replacement_session_id:
            replacement = self._create_internal_session(
                title=(
                    "Memory verification recovery · "
                    if isolated
                    else "Memory curation recovery · "
                )
                + f"{_short_run_label(str(row['run_id']))} · {str(row['phase'])[:32]}",
            )
            replacement_session_id = str(replacement["id"])

        timestamp = _now_ms()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_requests
                SET session_id = ?, state = 'prepared', turn_id = '',
                    receipt_json = ?, last_error = '', updated_at_ms = ?,
                    completed_at_ms = NULL
                WHERE request_id = ? AND state = 'resumable'
                """,
                (
                    replacement_session_id,
                    json.dumps(
                        {"replayRecovery": replay_recovery},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    str(row["request_id"]),
                ),
            )
            if not isolated:
                conn.execute(
                    """
                    UPDATE memory_curation_model_runs
                    SET session_id = ?, updated_at_ms = ?
                    WHERE run_id = ?
                    """,
                    (replacement_session_id, timestamp, str(row["run_id"])),
                )
                self._active_session_id = replacement_session_id
            recovered = conn.execute(
                "SELECT * FROM memory_curation_model_requests WHERE request_id = ?",
                (str(row["request_id"]),),
            ).fetchone()
        if recovered is None:
            raise MemoryModelUnavailable(
                "accepted Memory request disappeared before replay recovery"
            )
        return recovered

    def _recover_ambiguous_admission_from_snapshot(
        self,
        row: sqlite3.Row,
    ) -> sqlite3.Row:
        session_id = str(row["session_id"] or "")
        request_id = str(row["request_id"] or "")
        if not session_id or not request_id:
            return row
        try:
            snapshot = self.runtime.session_snapshot(session_id)
        except Exception:
            return row
        messages = snapshot.get("messages")
        if not isinstance(messages, list):
            return row
        turn_ids = {
            str(message.get("turnId") or "").strip()
            for message in messages
            if (
                isinstance(message, Mapping)
                and str(message.get("role") or "").strip().lower() == "user"
                and str(message.get("clientMessageId") or "").strip()
                == request_id
                and str(message.get("turnId") or "").strip()
            )
        }
        if len(turn_ids) > 1:
            raise MemoryModelUnavailable(
                "ambiguous Memory admission maps to multiple durable turns; "
                "the request was not replayed"
            )
        if not turn_ids:
            return row
        turn_id = next(iter(turn_ids))
        self._mark_request_resumable(
            request_id=request_id,
            turn_id=turn_id,
            error="memory_admission_recovered_from_runtime_snapshot",
            receipt={
                "admission": _memory_admission_payload(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_id=request_id,
                ),
                "recoverySource": "runtime_session_snapshot",
            },
        )
        with self._connect() as conn:
            recovered = conn.execute(
                "SELECT * FROM memory_curation_model_requests "
                "WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if recovered is None:
            raise MemoryModelUnavailable(
                "accepted Memory request disappeared before snapshot recovery"
            )
        return recovered

    def _prepare_request(
        self,
        *,
        run_id: str,
        phase: str,
        input_sha256: str,
        messages_json: str,
        isolated: bool,
        max_tokens: int | None,
    ) -> sqlite3.Row:
        timestamp = _now_ms()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_curation_model_requests
                WHERE run_id = ? AND phase = ? AND input_sha256 = ?
                """,
                (run_id, phase, input_sha256),
            ).fetchone()
        if row is not None:
            request_id = str(row["request_id"] or "")
            local_admission = self._accepted_requests.get(request_id)
            if (
                local_admission is not None
                and str(row["state"]) in {"prepared", "running", "resumable"}
                and not _request_has_admission_evidence(row)
            ):
                local_session_id, local_turn_id = local_admission
                persisted_session_id = str(row["session_id"] or "")
                if persisted_session_id and persisted_session_id != local_session_id:
                    raise MemoryModelUnavailable(
                        "accepted Memory request Session identity changed; "
                        "the request was not replayed"
                    )
                self._mark_request_resumable(
                    request_id=request_id,
                    turn_id=local_turn_id,
                    error="memory_admission_persistence_recovered",
                    receipt={
                        "admission": _memory_admission_payload(
                            session_id=local_session_id,
                            turn_id=local_turn_id,
                            request_id=request_id,
                        )
                    },
                )
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM memory_curation_model_requests "
                        "WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                if row is None:
                    raise MemoryModelUnavailable(
                        "accepted Memory request disappeared before recovery"
                    )
            if _request_has_ambiguous_admission(row):
                row = self._recover_ambiguous_admission_from_snapshot(row)
            if _request_has_ambiguous_admission(row):
                self._mark_request_resumable(
                    request_id=str(row["request_id"] or ""),
                    error="memory_admission_ambiguous_not_replayed",
                    receipt={
                        "admission": {
                            "schemaVersion": "rag-ime.memory-admission.v1",
                            "status": "unknown",
                            "sessionId": str(row["session_id"] or ""),
                            "clientMessageId": str(row["request_id"] or ""),
                        }
                    },
                )
                raise MemoryModelUnavailable(
                    "ambiguous Memory admission has no durable turn id; "
                    "the request was not replayed"
                )
            if (
                str(row["state"]) in {"running", "resumable"}
                and _request_has_admission_evidence(row)
            ):
                reason_code = self._unrecoverable_request_session_reason(row)
                if reason_code:
                    row = self._reprepare_unrecoverable_accepted_request(
                        row,
                        isolated=isolated,
                        reason_code=reason_code,
                    )
                else:
                    try:
                        return self._recover_resumable_request(
                            row,
                            max_tokens=max_tokens,
                        )
                    except _ConfirmedMemoryTimeoutAbort:
                        row = self._reprepare_unrecoverable_accepted_request(
                            row,
                            isolated=isolated,
                            reason_code="own_timeout_abort",
                        )
            if str(row["state"]) == "resumable":
                previous_session_id = str(row["session_id"] or "")
                self._retire_existing_internal_session(previous_session_id)
                replacement_session_id = self._active_session_id
                if isolated or replacement_session_id == previous_session_id:
                    replacement = self._create_internal_session(
                        title=(
                            "Memory verification recovery · "
                            if isolated
                            else "Memory curation recovery · "
                        )
                        + f"{_short_run_label(run_id)} · {phase[:32]}",
                    )
                    replacement_session_id = str(replacement["id"])
                with self._connect() as conn:
                    conn.execute(
                        """
                        UPDATE memory_curation_model_requests
                        SET session_id = ?, state = 'prepared', turn_id = '',
                            last_error = '', updated_at_ms = ?
                        WHERE request_id = ? AND state = 'resumable'
                        """,
                        (
                            replacement_session_id,
                            _now_ms(),
                            str(row["request_id"]),
                        ),
                    )
                    if not isolated:
                        conn.execute(
                            """
                            UPDATE memory_curation_model_runs
                            SET session_id = ?, updated_at_ms = ?
                            WHERE run_id = ?
                            """,
                            (replacement_session_id, _now_ms(), run_id),
                        )
                        self._active_session_id = replacement_session_id
                    row = conn.execute(
                        "SELECT * FROM memory_curation_model_requests WHERE request_id = ?",
                        (str(row["request_id"]),),
                    ).fetchone()
            return row
        request_session_id = self._active_session_id
        if isolated:
            isolated_session = self._create_internal_session(
                title=(
                    "Memory verification · "
                    f"{_short_run_label(run_id)} · {phase[:32]}"
                ),
            )
            request_session_id = str(isolated_session["id"])
        elif (
            request_session_id
            and str(self.sessions.get(request_session_id)["status"]) == "archived"
        ):
            replacement = self._create_internal_session(
                title=(
                    "Memory curation recovery · "
                    f"{_short_run_label(run_id)} · {phase[:32]}"
                ),
            )
            request_session_id = str(replacement["id"])
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE memory_curation_model_runs
                    SET session_id = ?, updated_at_ms = ?
                    WHERE run_id = ? AND state = 'resumable'
                    """,
                    (request_session_id, _now_ms(), run_id),
                )
            self._active_session_id = request_session_id
        with self._connect() as conn:
            ordinal = int(
                conn.execute(
                    """
                    SELECT COALESCE(MAX(ordinal), 0) + 1
                    FROM memory_curation_model_requests
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            request_id = "memory-request:" + hashlib.sha256(
                f"{run_id}\0{phase}\0{input_sha256}".encode("utf-8")
            ).hexdigest()[:32]
            conn.execute(
                """
                INSERT INTO memory_curation_model_requests(
                    request_id, run_id, phase, ordinal, input_sha256,
                    messages_json, input_chars, session_id, state, created_at_ms,
                    updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (
                    request_id,
                    run_id,
                    phase,
                    ordinal,
                    input_sha256,
                    messages_json,
                    len(messages_json),
                    request_session_id,
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """
                UPDATE memory_curation_model_runs
                SET frozen_input_sha256 = CASE
                        WHEN frozen_input_sha256 = '' THEN ?
                        ELSE frozen_input_sha256
                    END,
                    updated_at_ms = ?
                WHERE run_id = ?
                """,
                (input_sha256, timestamp, run_id),
            )
            return conn.execute(
                "SELECT * FROM memory_curation_model_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()

    def _create_internal_session(self, *, title: str) -> dict[str, object]:
        session = self.sessions.create(
            title=title,
            mode="assistant",
            role_id="memory-curator",
            role_version="1",
            model_profile=self.reference,
            thinking_level=self.thinking_level,
            tool_profile_version=MEMORY_CURATION_TOOL_PROFILE,
            project_context_enabled=False,
            pi_skills_enabled=False,
            codex_skills_enabled=False,
            workspace_roots=[],
            session_kind="subagent_runtime",
        )
        return self.sessions.set_runtime_policy(
            str(session["id"]),
            mode="assistant",
            tool_profile_version=MEMORY_CURATION_TOOL_PROFILE,
            allowed_tools=[],
            project_context_enabled=False,
            pi_skills_enabled=False,
            codex_skills_enabled=False,
            workspace_roots=[],
        )

    def _retire_internal_session(self, session_id: str) -> None:
        self.runtime.close_session(session_id)
        self.sessions.retire_system_internal(
            session_id,
            tool_profile_version=MEMORY_CURATION_TOOL_PROFILE,
            updated_at_ms=_now_ms(),
        )

    def _retire_existing_internal_session(self, session_id: str) -> None:
        if not session_id:
            return
        try:
            session = self.sessions.get(session_id)
        except AgentSessionNotFound:
            return
        if str(session["status"]) == "archived":
            return
        self._retire_internal_session(session_id)

    def _mark_request_running(self, *, request_id: str) -> None:
        timestamp = _now_ms()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_requests
                SET state = 'running', attempt_count = attempt_count + 1,
                    last_error = '', updated_at_ms = ?
                WHERE request_id = ?
                """,
                (timestamp, request_id),
            )
            conn.execute(
                """
                UPDATE memory_curation_model_runs
                SET state = 'running', last_error = '', updated_at_ms = ?
                WHERE run_id = ?
                """,
                (timestamp, self._active_run_id),
            )

    def _bind_turn(self, *, request_id: str, turn_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_requests
                SET turn_id = ?, updated_at_ms = ?
                WHERE request_id = ?
                """,
                (turn_id, _now_ms(), request_id),
            )

    def _complete_request(
        self,
        *,
        request_id: str,
        turn_id: str,
        output_text: str,
        receipt: Mapping[str, object],
    ) -> sqlite3.Row:
        timestamp = _now_ms()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_requests
                SET state = 'completed', turn_id = ?, output_text = ?,
                    receipt_json = ?, last_error = '', updated_at_ms = ?,
                    completed_at_ms = ?
                WHERE request_id = ?
                """,
                (
                    turn_id,
                    output_text,
                    json.dumps(dict(receipt), ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                    timestamp,
                    request_id,
                ),
            )
            return conn.execute(
                "SELECT * FROM memory_curation_model_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()

    def _mark_request_resumable(
        self,
        *,
        request_id: str,
        error: str,
        receipt: Mapping[str, object] | None = None,
        turn_id: str = "",
    ) -> None:
        timestamp = _now_ms()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_requests
                SET state = 'resumable',
                    turn_id = CASE WHEN ? != '' THEN ? ELSE turn_id END,
                    receipt_json = ?, last_error = ?, updated_at_ms = ?
                WHERE request_id = ? AND state != 'completed'
                """,
                (
                    turn_id,
                    turn_id,
                    json.dumps(dict(receipt or {}), ensure_ascii=False, separators=(",", ":")),
                    str(error)[:800],
                    timestamp,
                    request_id,
                ),
            )
        self._set_run_error(self._active_run_id, state="resumable", error=error)

    def _set_run_error(self, run_id: str, *, state: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_runs
                SET state = ?, last_error = ?, updated_at_ms = ?
                WHERE run_id = ?
                """,
                (state, str(error)[:800], _now_ms(), run_id),
            )

    def _response_from_request(self, row: sqlite3.Row) -> dict[str, object]:
        receipt = _json_object(row["receipt_json"])
        text = str(row["output_text"] or "").strip()
        if not text:
            raise MemoryModelUnavailable(
                "completed Memory request has no database-owned output"
            )
        return {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "provider": self.provider,
            "model": self.model_id,
            "thinkingLevel": self.thinking_level,
            "profile": MEMORY_CURATION_PROFILE,
            "requestId": str(row["request_id"]),
            "turnId": str(row["turn_id"] or ""),
            "elapsedMs": max(0, int(receipt.get("elapsedMs") or 0)),
            "usage": dict(receipt.get("usage") or {}),
            "receipt": receipt,
        }

    def _validate_run_row(
        self,
        row: sqlite3.Row,
        *,
        frozen_hash: str,
    ) -> None:
        if not self._run_row_matches_model_profile(row):
            raise MemoryModelUnavailable(
                "existing Memory run uses a different frozen model profile"
            )
        existing_hash = str(row["frozen_input_sha256"] or "")
        if frozen_hash and existing_hash and frozen_hash != existing_hash:
            raise MemoryModelUnavailable(
                "existing Memory run input hash does not match the frozen batch"
            )

    def _run_row_matches_model_profile(self, row: sqlite3.Row) -> bool:
        return (
            str(row["profile"]) == MEMORY_CURATION_PROFILE
            and str(row["provider"]) == self.provider
            and str(row["model_id"]) == self.model_id
            and str(row["thinking_level"]) == self.thinking_level
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def reconcile_stale_memory_runtime_sessions(
    sessions: AgentSessionStore,
    *,
    db_path: str | Path | None = None,
) -> dict[str, object]:
    """Retire Memory-only Sessions left resident by a previous Gateway.

    A new Gateway starts with an empty Pi Host. Therefore every unarchived
    ``memory-curation-v1`` internal Session belongs to the previous process;
    every unfinished Memory request remains resumable (including orphaned
    rows whose Session was already lost), while old Runtime bindings and busy
    UI projections must not survive the process boundary.
    """

    resolved_db_path = Path(db_path or sessions.db_path)
    timestamp = _now_ms()
    with sqlite3.connect(resolved_db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        stale_session_ids = [
            str(row["id"])
            for row in conn.execute(
                """
                SELECT id
                FROM agent_sessions
                WHERE session_kind = 'subagent_runtime'
                  AND tool_profile_version = ?
                  AND archived_at_ms IS NULL
                ORDER BY created_at_ms, id
                """,
                (MEMORY_CURATION_TOOL_PROFILE,),
            ).fetchall()
        ]
        run_cursor = conn.execute(
            """
            UPDATE memory_curation_model_runs
            SET state = 'resumable', last_error = 'memory_gateway_restart',
                updated_at_ms = ?, completed_at_ms = NULL
            WHERE state IN ('prepared', 'running')
               OR run_id IN (
                    SELECT DISTINCT run_id
                    FROM memory_curation_model_requests
                    WHERE state IN ('prepared', 'running')
               )
            """,
            (timestamp,),
        )
        run_count = max(0, int(run_cursor.rowcount))
        request_cursor = conn.execute(
            """
            UPDATE memory_curation_model_requests
            SET state = 'resumable', last_error = 'memory_gateway_restart',
                updated_at_ms = ?, completed_at_ms = NULL
            WHERE state IN ('prepared', 'running')
            """,
            (timestamp,),
        )
        request_count = max(0, int(request_cursor.rowcount))
        conn.commit()

    for session_id in stale_session_ids:
        sessions.retire_system_internal(
            session_id,
            tool_profile_version=MEMORY_CURATION_TOOL_PROFILE,
            updated_at_ms=timestamp,
        )
    return {
        "schemaVersion": "rag-ime.memory-runtime-restart-recovery.v1",
        "recoveredSessionCount": len(stale_session_ids),
        "resumableRequestCount": request_count,
        "resumableRunCount": run_count,
        "recoveredAtMs": timestamp,
    }


def split_memory_model_reference(value: object) -> tuple[str, str]:
    reference = " ".join(str(value or "").strip().split())
    if reference == "gpt/gpt-5.6-luna":
        reference = DEFAULT_MEMORY_MODEL_REFERENCE
    provider, separator, model_id = reference.partition("/")
    if not separator:
        raise ValueError("memory model must be a provider/model reference")
    return (
        _model_part(provider, field="provider", maximum=80),
        _model_part(model_id, field="modelId", maximum=160),
    )


def build_governed_memory_model_executor(
    runtime: MemorySessionRuntime,
    model_reference: object,
    thinking_level: object,
    *,
    timeout_seconds: float = DEFAULT_MEMORY_CURATION_TIMEOUT_SECONDS,
    db_path: str | Path | None = None,
    sessions: AgentSessionStore | None = None,
) -> GovernedMemoryModelExecutor:
    provider, model_id = split_memory_model_reference(model_reference)
    return GovernedMemoryModelExecutor(
        runtime,
        provider,
        model_id,
        str(thinking_level or "").strip().lower(),
        timeout_seconds=timeout_seconds,
        sessions=sessions,
        db_path=db_path,
    )


def build_managed_pi_memory_model_executor(
    db_path: str | Path,
    model_reference: object,
    thinking_level: object,
    *,
    timeout_seconds: float = DEFAULT_MEMORY_CURATION_TIMEOUT_SECONDS,
) -> GovernedMemoryModelExecutor:
    del db_path, model_reference, thinking_level, timeout_seconds
    raise MemoryModelUnavailable(
        "standalone Memory model execution is disabled; trigger the running Agent Gateway"
    )


def memory_curation_model_status(
    conn: sqlite3.Connection,
    *,
    limit: int = 8,
    required_model: str = DEFAULT_MEMORY_MODEL_REFERENCE,
) -> dict[str, object]:
    """Expose persisted run/phase receipts without prompt or response contents."""

    bounded_limit = max(1, min(30, int(limit)))
    runs = conn.execute(
        """
        SELECT *
        FROM memory_curation_model_runs
        ORDER BY updated_at_ms DESC, run_id DESC
        LIMIT ?
        """,
        (bounded_limit,),
    ).fetchall()
    payloads: list[dict[str, object]] = []
    for run in runs:
        requests = conn.execute(
            """
            SELECT request_id, phase, ordinal, input_sha256, input_chars,
                   session_id, state, turn_id, attempt_count, receipt_json,
                   last_error, created_at_ms, updated_at_ms, completed_at_ms
            FROM memory_curation_model_requests
            WHERE run_id = ?
            ORDER BY ordinal
            """,
            (str(run["run_id"]),),
        ).fetchall()
        payload = _run_payload(run)
        payload["requests"] = [_request_public_payload(row) for row in requests]
        payloads.append(payload)
    state_counts = {
        str(row["state"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT state, COUNT(*) AS count
            FROM memory_curation_model_runs
            GROUP BY state
            ORDER BY state
            """
        ).fetchall()
    }
    return {
        "schemaVersion": "rag-ime.memory-curation-model-status.v1",
        "ok": True,
        "profile": MEMORY_CURATION_PROFILE,
        "requiredModel": required_model,
        "requiredThinkingLevel": REQUIRED_MEMORY_THINKING_LEVEL,
        "minimumContextTokens": MINIMUM_MEMORY_CONTEXT_TOKENS,
        "stateCounts": state_counts,
        "runs": payloads,
    }


def _messages(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("memory model messages are required")
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("memory model message must be an object")
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "")
        if role not in {"system", "user", "assistant"} or not content:
            raise ValueError("memory model message role/content is invalid")
        normalized.append({"role": role, "content": content})
    return normalized


def _session_prompt(
    messages: list[dict[str, str]],
    *,
    request_id: str,
    input_sha256: str,
    max_tokens: int | None,
) -> str:
    parts = [
        "MEMORY_CURATION\n",
        f"request_id={request_id}\n",
        f"frozen_input_sha256={input_sha256}\n",
        f"requested_output_tokens={max(0, int(max_tokens or 0))}\n",
        "Each section below is length-delimited application data. ",
        "Return exactly one JSON object.\n",
    ]
    for index, message in enumerate(messages, start=1):
        content = message["content"]
        parts.extend(
            (
                f"\nSECTION {index} role={message['role']} ",
                f"utf8_bytes={len(content.encode('utf-8'))}\n",
                content,
                "\nEND_SECTION\n",
            )
        )
    return "".join(parts)


def _memory_provider_max_tokens(
    requested: int | None,
    *,
    catalog_max_tokens: int,
) -> int:
    requested_tokens = int(requested or DEFAULT_MEMORY_PROVIDER_OUTPUT_TOKENS)
    return min(
        max(MINIMUM_MEMORY_PROVIDER_OUTPUT_TOKENS, requested_tokens),
        catalog_max_tokens,
        MAXIMUM_MEMORY_PROVIDER_OUTPUT_TOKENS,
    )


def _verify_runtime_receipts(
    model_receipt: Mapping[str, object],
    thinking_receipt: Mapping[str, object],
    *,
    provider: str,
    model_id: str,
    thinking_level: str,
    max_tokens: int,
) -> None:
    selected = model_receipt.get("selected")
    if not isinstance(selected, Mapping):
        raise MemoryModelUnavailable("Pi model-selection receipt is missing")
    if (
        str(selected.get("provider") or "") != provider
        or str(selected.get("id") or selected.get("modelId") or "") != model_id
    ):
        raise MemoryModelUnavailable("Pi selected a different Memory model")
    if int(selected.get("maxTokens") or 0) != max_tokens:
        raise MemoryModelUnavailable("Pi did not apply the Memory output budget")
    if str(thinking_receipt.get("thinkingLevel") or "") != thinking_level:
        raise MemoryModelUnavailable("Pi selected a different Memory thinking level")


def _memory_terminal_from_settlement(
    settlement: Mapping[str, object],
    *,
    session_id: str,
    turn_id: str,
    client_message_id: str,
) -> dict[str, object]:
    if (
        str(settlement.get("schemaVersion") or "")
        != "rag-ime.pi-turn-settlement.v1"
        or str(settlement.get("sessionId") or "") != session_id
        or str(settlement.get("turnId") or "") != turn_id
        or str(settlement.get("clientMessageId") or "") != client_message_id
    ):
        raise MemoryModelUnavailable(
            "managed Pi returned a settlement for a different Memory turn"
        )
    runtime_session_id = str(settlement.get("runtimeSessionId") or "").strip()
    receipt = settlement.get("receipt")
    if (
        not runtime_session_id
        or not isinstance(receipt, Mapping)
        or str(receipt.get("schemaVersion") or "") != "pi.agent-settled.v2"
        or str(receipt.get("sessionId") or "") != runtime_session_id
    ):
        raise MemoryModelUnavailable("managed Pi settlement receipt is invalid")
    disposition = str(receipt.get("disposition") or "").strip().lower()
    if disposition != "completed":
        if disposition == "suspended":
            raise MemoryModelUnavailable(
                "managed Pi returned a non-terminal Memory settlement"
            )
        return {
            "state": "failed",
            "text": "",
            "usage": {},
            "error": str(
                receipt.get("stopReason")
                or f"Memory Session turn {disposition or 'failed'}"
            ),
        }
    if receipt.get("aborted") is True or int(receipt.get("pendingOperations") or 0) != 0:
        raise MemoryModelUnavailable(
            "managed Pi completed receipt still owns pending Memory work"
        )
    final_message = receipt.get("finalMessage")
    if (
        not isinstance(final_message, Mapping)
        or str(final_message.get("role") or "").strip().lower() != "assistant"
    ):
        raise MemoryModelUnavailable(
            "Memory Session completed without an authoritative assistant message"
        )
    usage = final_message.get("usage")
    return {
        "state": "completed",
        "text": _assistant_message_text(final_message),
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
        "error": "",
    }


def _assistant_message_text(message: Mapping[str, object]) -> str:
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
        blocks = message.get("content")
    if not isinstance(blocks, list):
        return str(message.get("content") or message.get("text") or "").strip()
    chunks: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        block_type = str(block.get("type") or "").strip().lower()
        if block_type and block_type != "text":
            continue
        data = block.get("data")
        if isinstance(data, Mapping):
            text = str(data.get("text") or "")
        else:
            text = str(block.get("text") or "")
        if text:
            chunks.append(text)
    return "\n\n".join(chunks).strip()


def _run_payload(row: sqlite3.Row) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.memory-curation-model-run.v1",
        "runId": str(row["run_id"]),
        "sessionId": str(row["session_id"] or ""),
        "profile": str(row["profile"]),
        "provider": str(row["provider"]),
        "modelId": str(row["model_id"]),
        "thinkingLevel": str(row["thinking_level"]),
        "frozenInputSha256": str(row["frozen_input_sha256"]),
        "state": str(row["state"]),
        "lastError": str(row["last_error"]),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "completedAtMs": int(row["completed_at_ms"] or 0),
    }


def _request_public_payload(row: sqlite3.Row) -> dict[str, object]:
    receipt = _json_object(row["receipt_json"])
    return {
        "requestId": str(row["request_id"]),
        "sessionId": str(row["session_id"] or ""),
        "phase": str(row["phase"]),
        "ordinal": int(row["ordinal"]),
        "inputSha256": str(row["input_sha256"]),
        "inputChars": int(row["input_chars"]),
        "state": str(row["state"]),
        "turnId": str(row["turn_id"]),
        "attemptCount": int(row["attempt_count"]),
        "receipt": receipt,
        "lastError": str(row["last_error"]),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "completedAtMs": int(row["completed_at_ms"] or 0),
    }


def _request_has_admission_evidence(row: sqlite3.Row) -> bool:
    if str(row["turn_id"] or "").strip():
        return True
    admission = _json_object(row["receipt_json"]).get("admission")
    return isinstance(admission, Mapping) and admission.get("accepted") is True


def _own_timeout_abort_confirmed(row: sqlite3.Row) -> bool:
    if (
        str(row["state"] or "") != "resumable"
        or str(row["last_error"] or "") != "memory_curation_timeout"
    ):
        return False
    receipt = _json_object(row["receipt_json"])
    admission = receipt.get("admission")
    cancellation = receipt.get("cancellation")
    if not isinstance(admission, Mapping) or not isinstance(cancellation, Mapping):
        return False
    lifecycle = cancellation.get("lifecycle")
    session_id = str(row["session_id"] or "")
    turn_id = str(row["turn_id"] or "")
    return bool(
        session_id
        and turn_id
        and admission.get("accepted") is True
        and admission.get("sessionId") == session_id
        and admission.get("turnId") == turn_id
        and admission.get("clientMessageId") == str(row["request_id"])
        and cancellation.get("schemaVersion")
        == "rag-ime.pi-session-abort-receipt.v1"
        and cancellation.get("sessionId") == session_id
        and cancellation.get("turnId") == turn_id
        and isinstance(lifecycle, Mapping)
        and lifecycle.get("drained") is True
        and lifecycle.get("idle") is True
    )


def _request_has_explicit_pre_admission_rejection(row: sqlite3.Row) -> bool:
    admission = _json_object(row["receipt_json"]).get("admission")
    return (
        isinstance(admission, Mapping)
        and admission.get("schemaVersion") == "rag-ime.memory-admission.v1"
        and admission.get("status") == "rejected"
        and bool(str(admission.get("reasonCode") or "").strip())
    )


def _request_has_ambiguous_admission(row: sqlite3.Row) -> bool:
    if str(row["turn_id"] or "").strip() or int(row["attempt_count"] or 0) <= 0:
        return False
    if _request_has_explicit_pre_admission_rejection(row):
        return False
    return str(row["state"] or "") in {"running", "resumable"}


def _memory_admission_payload(
    *,
    session_id: str,
    turn_id: str,
    request_id: str,
) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.memory-admission.v1",
        "accepted": True,
        "sessionId": session_id,
        "turnId": turn_id,
        "clientMessageId": request_id,
    }


def _json_object(value: object) -> dict[str, object]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _model_part(value: object, *, field: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError(f"memory {field} is invalid")
    return normalized


def _identifier(value: object, *, field: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > maximum
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", normalized)
    ):
        raise ValueError(f"memory {field} is invalid")
    return normalized


def _sha256_value(value: object, *, allow_empty: bool) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized[7:]
    if not normalized and allow_empty:
        return ""
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError("memory frozen input sha256 is invalid")
    return normalized


def _short_run_label(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]


def _attempt_run_id(requested_run_id: str, attempt: int) -> str:
    if attempt <= 1:
        return requested_run_id
    suffix = f":attempt:{attempt}"
    candidate = f"{requested_run_id}{suffix}"
    if len(candidate) <= 240:
        return candidate
    digest = hashlib.sha256(requested_run_id.encode("utf-8")).hexdigest()
    return f"memory-run:{digest}{suffix}"


def _now_ms() -> int:
    return int(time.time() * 1_000)


def _public_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:800] or exc.__class__.__name__
