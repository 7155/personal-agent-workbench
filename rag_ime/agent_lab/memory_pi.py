"""Memory evaluation adapter over Lab's existing durable Pi Session calls.

The organizer/curator keep semantic validation, shadow writes and rollback.
This adapter only freezes inputs, requests schema-shaped output and returns
the original Pi settlement. It neither owns a model loop nor fabricates cost.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path

from ..agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from .golden_pi import AgentLabGoldenPiExecutor, GoldenPiCallError
from ..contracts.json_schema import validate_contract
from ..personal_memory_luna_evaluation import _evaluation_prompt, _phase, personal_memory_phase_schema


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _memory_usage(value: object) -> dict[str, object]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    fields = ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens")
    if any(not isinstance(raw.get(key), int) or isinstance(raw.get(key), bool) or raw[key] < 0 for key in fields):
        return {"available": False, "runtimeUsage": raw}
    # Pi input is uncached; CLI input totals include cache hits. Keep the
    # existing evaluation aggregate vocabulary without treating missing as 0.
    return {"available": True, "uncachedInputTokens": raw["inputTokens"],
        "cachedInputTokens": raw["cacheReadTokens"], "cacheWriteInputTokens": raw["cacheWriteTokens"],
        "inputTokens": sum(raw[key] for key in fields[:3]), "outputTokens": raw["outputTokens"],
        "runtimeUsage": raw}


class AgentLabMemoryPiExecutor:
    provider = "openai-codex"
    transport = "pi_session"

    def __init__(self, artifact_root: str | Path, *, pi_executor: AgentLabGoldenPiExecutor,
                 model_id: str = DEFAULT_AGENT_MODEL_ID, thinking_level: str = "max",
                 context_profile: str = "full-json-v1", prompt_contract: str = "standard-v1",
                 cancelled: Callable[[], bool] = lambda: False,
                 receipt_observer: Callable[[Mapping[str, object]], None] | None = None,
                 request_namespace: str = "", audit_db_path: str | Path | None = None) -> None:
        if model_id not in {DEFAULT_AGENT_MODEL_ID, "gpt-5.6-luna", "gpt-5.6-sol"} or thinking_level != "max":
            raise ValueError("Memory evaluation supports Luna/Sol with frozen max thinking")
        if context_profile not in {"full-json-v1", "compact-json-v1"} or prompt_contract not in {"standard-v1", "concise-json-v1"}:
            raise ValueError("unsupported Memory evaluation prompt configuration")
        self.model_id, self.thinking_level = model_id, thinking_level
        self.context_profile, self.prompt_contract = context_profile, prompt_contract
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.artifact_root.stat().st_mode & 0o077:
            raise ValueError("Memory evaluation artifact directory must be private")
        self.pi_executor = pi_executor
        self.audit_db_path = Path(audit_db_path).expanduser().resolve(strict=True) if audit_db_path is not None else None
        if self.audit_db_path is not None and self.audit_db_path.samefile(self.pi_executor.db_path):
            raise ValueError("Memory audit must use the isolated shadow, not the resident Runtime database")
        if not isinstance(request_namespace, str) or len(request_namespace) > 256:
            raise ValueError("Memory request namespace must be a bounded identifier")
        self._request_namespace = request_namespace
        self._cancelled = cancelled
        self._receipt_observer = receipt_observer
        self._closed = threading.Event()
        self._run: dict[str, object] | None = None
        self._run_id = ""
        self._receipts: list[dict[str, object]] = []

    @property
    def receipts(self) -> tuple[dict[str, object], ...]:
        return tuple(json.loads(_canonical(item)) for item in self._receipts)

    def _observe(self, receipt: Mapping[str, object]) -> None:
        """Persist private call bindings before notifying an embedding Job owner."""
        snapshot = json.loads(_canonical(dict(receipt)))
        path = self.artifact_root / "pi-request-receipts.jsonl"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(_canonical(snapshot) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if self._receipt_observer is not None:
            self._receipt_observer(snapshot)

    def begin_run(self, run_id: object, *, frozen_input_sha256: object = "") -> dict[str, object]:
        if self._closed.is_set() or self._run is not None:
            raise RuntimeError("Memory executor is closed or already has an active run")
        identity, frozen = str(run_id or "").strip(), str(frozen_input_sha256 or "").strip().lower()
        if not identity or len(identity) > 240 or not re.fullmatch(r"[0-9a-f]{64}", frozen):
            raise ValueError("run_id and frozen_input_sha256 are required")
        binding = {"runIdSha256": _digest(identity), "frozenInputSha256": frozen,
            "model": self.model_id, "thinking": self.thinking_level, "transport": self.transport,
            "contextProfile": self.context_profile, "promptContract": self.prompt_contract}
        if self._request_namespace:
            binding["requestNamespaceSha256"] = _digest(self._request_namespace)
        path = self.artifact_root / f"run-{_digest(identity)}.json"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(_canonical(binding))
        except FileExistsError:
            if json.loads(path.read_text(encoding="utf-8")) != binding:
                raise ValueError("Memory run binding changed; preserve the original model and input")
        self._begin_audit_run(identity, frozen)
        self._run = binding
        self._run_id = identity
        return {**binding, "runId": identity}

    def complete(self, *, phase: object, messages: Sequence[Mapping[str, object]],
                 isolated: bool = False, max_tokens: int | None = None) -> dict[str, object]:
        if self._run is None:
            raise RuntimeError("begin_run must precede Memory completion")
        run = dict(self._run)
        normalized = _phase(phase)
        if bool(isolated) != (normalized in {"independent-verifier", "atom-first-verifier"}):
            raise ValueError("Memory verifier isolation flag is inconsistent")
        prompt, projection = _evaluation_prompt(normalized, messages, requested_output_tokens=max_tokens,
            required_model=str(run["model"]), context_profile=str(run["contextProfile"]), prompt_contract=str(run["promptContract"]))
        schema = personal_memory_phase_schema(normalized, prompt_contract=str(run["promptContract"]))
        prompt += "\nReturn exactly one JSON object matching this output schema:\n" + _canonical(schema)
        model = {"provider": self.provider, "model": run["model"], "thinkingLevel": run["thinking"]}
        # The same frozen packet after shadow rollback reuses its original
        # request even when the curator assigns a new logical run id.
        request_identity = {"phase": normalized, "model": model,
            "input": prompt, "frozenInputSha256": run["frozenInputSha256"]}
        if self._request_namespace:
            request_identity["requestNamespaceSha256"] = _digest(self._request_namespace)
        request_id = "memory-pi:" + _digest(_canonical(request_identity))
        prior = None
        session_id = ""
        def on_session(value: str) -> None:
            nonlocal session_id
            session_id = value
            self._bind_audit_session(value)
            self._observe({**run, "requestId": request_id, "sessionId": value,
                "phase": normalized, "status": "session_bound", "transport": self.transport})
        try:
            prior = self.pi_executor.completed_result(request_id, model)
            result = self.pi_executor.complete(request_id=request_id, model=model, prompt=prompt,
                on_session=on_session, cancelled=lambda: self._closed.is_set() or self._cancelled())
        except GoldenPiCallError as exc:
            completion = exc.completion or {}
            # Abort may have a durable accepted turn without a settlement yet.
            # Read that original request identity; never invent a Pi turn.
            try:
                bound = self.pi_executor._row(request_id)
                identity = {"sessionId": bound["session_id"], "turnId": bound["turn_id"],
                    "requestState": bound["state"], "bindingAvailable": True}
            except (GoldenPiCallError, OSError, sqlite3.Error):
                identity = {"bindingAvailable": False}
            status = str(dict(completion.get("receipt") or {}).get("status") or "")
            if identity.get("requestState") == "cancelled":
                status = "cancelled"
            receipt = {**dict(completion.get("receipt") or {}), **run, **projection, "requestId": request_id,
                "sessionId": session_id or completion.get("sessionId"), "turnId": completion.get("turnId"),
                **identity,
                "transport": self.transport, "phase": normalized,
                "status": status or ("interrupted" if exc.interrupted else "failed"),
                "usage": _memory_usage(completion.get("usage")), "schemaValid": False}
            self._receipts.append(receipt)
            self._observe(receipt)
            self._record_audit_request(receipt, messages=messages, output_text="")
            raise
        receipt = {**dict(result.get("receipt") or {}), **run, **projection,
            "requestId": request_id, "sessionId": result.get("sessionId") or session_id,
            "turnId": result.get("turnId"), "transport": self.transport, "phase": normalized,
            "isolated": bool(isolated), "resumed": prior is not None,
            "inputSha256": _digest(prompt), "inputChars": len(prompt), "schemaSha256": _digest(_canonical(schema)),
            "usage": _memory_usage(result.get("usage")), "schemaValid": False,
            "elapsedSeconds": float(dict(result.get("receipt") or {}).get("elapsedMs") or 0) / 1000}
        self._receipts.append(receipt)
        if receipt.get("status") != "completed":
            self._observe(receipt)
            self._record_audit_request(receipt, messages=messages, output_text="")
            raise GoldenPiCallError("Memory Pi request did not complete", interrupted=False, completion=result)
        try:
            output = json.loads(str(result.get("text") or ""))
            validate_contract(output, schema)
        except (ValueError, TypeError):
            self._observe(receipt)
            self._record_audit_request(receipt, messages=messages, output_text=str(result.get("text") or ""))
            raise ValueError("Memory Pi output failed the phase schema; original settlement retained") from None
        receipt["schemaValid"] = True
        output_text = _canonical(output)
        receipt["outputSha256"] = _digest(output_text)
        self._observe(receipt)
        self._record_audit_request(receipt, messages=messages, output_text=output_text)
        if self._closed.is_set() or self._cancelled():
            # The actual Pi call completed and its cost remains recorded, but
            # a cancelled evaluation may not start a new semantic write from
            # this late output. Already-applied shadow changes still roll back.
            raise GoldenPiCallError("Memory evaluation stopped before consuming the settled output", interrupted=False, completion=result)
        return {"choices": [{"message": {"role": "assistant", "content": output_text}, "finish_reason": "stop"}],
            "requestId": request_id, "turnId": receipt["turnId"], "receipt": dict(receipt)}

    def finish_run(self, *, state: str = "completed") -> dict[str, object]:
        if state not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid Memory run terminal state")
        result = {"state": state, "retired": True, "transport": self.transport,
            "runIdSha256": self._run.get("runIdSha256") if self._run else None, "requestCount": len(self._receipts)}
        if self.audit_db_path is not None and self._run_id:
            with closing(sqlite3.connect(self.audit_db_path, timeout=30)) as conn, conn:
                timestamp = int(time.time() * 1000)
                conn.execute("UPDATE memory_curation_model_runs SET state=?,updated_at_ms=?,completed_at_ms=? WHERE run_id=?",
                    (state, timestamp, timestamp, self._run_id))
        self._run = None
        self._run_id = ""
        return result

    def fail_run(self, error: BaseException) -> dict[str, object]:
        return {**self.finish_run(state="failed"), "errorClass": type(error).__name__}

    def close(self) -> None:
        self._closed.set()
        self.finish_run(state="cancelled")

    def _begin_audit_run(self, run_id: str, frozen: str) -> None:
        if self.audit_db_path is None:
            return
        with closing(sqlite3.connect(self.audit_db_path, timeout=30)) as conn, conn:
            row = conn.execute("SELECT provider,model_id,thinking_level,frozen_input_sha256 FROM memory_curation_model_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is not None and tuple(row) != (self.provider, self.model_id, self.thinking_level, frozen):
                raise ValueError("Memory shadow audit run identity changed")
            timestamp = int(time.time() * 1000)
            # The Session field remains absent until Pi actually binds one.
            conn.execute("INSERT INTO memory_curation_model_runs(run_id,provider,model_id,thinking_level,frozen_input_sha256,state,created_at_ms,updated_at_ms) "
                "VALUES(?,?,?,?,?,'running',?,?) ON CONFLICT(run_id) DO UPDATE SET state='running',updated_at_ms=excluded.updated_at_ms,completed_at_ms=NULL",
                (run_id, self.provider, self.model_id, self.thinking_level, frozen, timestamp, timestamp))

    def _bind_audit_session(self, session_id: str) -> None:
        if self.audit_db_path is None:
            return
        # Mirror the observed resident Session as an inert evaluation snapshot.
        # No synthetic Session is created and no second Runtime owns this row.
        with closing(sqlite3.connect(self.pi_executor.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            source.row_factory = sqlite3.Row
            actual = source.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,)).fetchone()
        if actual is None:
            raise ValueError("Memory Pi Session snapshot is unavailable")
        with closing(sqlite3.connect(self.audit_db_path, timeout=30)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            columns = [row[1] for row in conn.execute("PRAGMA table_info(agent_sessions)") if row[1] in actual.keys()]
            values = [1 if name == "evaluation_snapshot" else actual[name] for name in columns]
            names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
            conn.execute(f"INSERT OR IGNORE INTO agent_sessions({names}) VALUES({','.join('?' for _ in columns)})", values)
            conn.execute("UPDATE memory_curation_model_runs SET session_id=COALESCE(session_id,?) WHERE run_id=?",
                (session_id, self._run_id))

    def _record_audit_request(self, receipt: Mapping, *, messages: Sequence[Mapping], output_text: str) -> None:
        if self.audit_db_path is None:
            return
        messages_json = _canonical([dict(item) for item in messages])
        status = str(receipt.get("status") or "failed")
        state = status if status in {"completed", "cancelled", "failed"} else "resumable"
        timestamp = int(time.time() * 1000)
        with closing(sqlite3.connect(self.audit_db_path, timeout=30)) as conn, conn:
            existing = conn.execute("SELECT run_id,messages_json FROM memory_curation_model_requests WHERE request_id=?", (receipt["requestId"],)).fetchone()
            if existing is not None and tuple(existing) != (self._run_id, messages_json):
                raise ValueError("Memory shadow request conflicts with its actual Pi binding")
            ordinal = conn.execute("SELECT COUNT(*)+1 FROM memory_curation_model_requests WHERE run_id=?", (self._run_id,)).fetchone()[0]
            conn.execute("INSERT INTO memory_curation_model_requests(request_id,run_id,phase,ordinal,input_sha256,messages_json,input_chars,state,turn_id,attempt_count,output_text,receipt_json,created_at_ms,updated_at_ms,completed_at_ms,session_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET "
                "state=excluded.state,turn_id=excluded.turn_id,attempt_count=memory_curation_model_requests.attempt_count+1,output_text=excluded.output_text,receipt_json=excluded.receipt_json,updated_at_ms=excluded.updated_at_ms,completed_at_ms=excluded.completed_at_ms,session_id=excluded.session_id",
                (receipt["requestId"], self._run_id, receipt["phase"], ordinal, _digest(messages_json), messages_json,
                    len(messages_json), state, str(receipt.get("turnId") or ""), output_text, _canonical(dict(receipt)),
                    timestamp, timestamp, timestamp if state in {"completed", "failed", "cancelled"} else None,
                    str(receipt.get("sessionId") or "")))
