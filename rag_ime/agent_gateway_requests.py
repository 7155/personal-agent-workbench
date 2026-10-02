"""Durable Gateway admission identity; approvals still own effect claims."""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping

from .agent_sessions import AgentSessionStore


class GatewayRequestUnresolved(ValueError):
    error_code = "tool_request_outcome_unknown"


class GatewayRequestConflict(ValueError):
    error_code = "tool_request_identity_conflict"


class GatewayRequestStore:
    def __init__(self, sessions: AgentSessionStore) -> None:
        self.sessions = sessions

    def admit(self, request: Mapping[str, object]) -> dict[str, object] | None:
        session_id = str(request["sessionId"])
        tool_call_id = str(request["toolCallId"])
        if not tool_call_id.strip() or len(tool_call_id) > 512:
            raise ValueError("toolCallId must contain between 1 and 512 characters")
        digest = hashlib.sha256(json.dumps(dict(request), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        timestamp = int(time.time() * 1000)
        with self.sessions._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM agent_gateway_requests WHERE session_id = ? AND tool_call_id = ?",
                (session_id, tool_call_id)).fetchone()
            if row is not None:
                if str(row["request_sha256"]) != digest:
                    raise GatewayRequestConflict("toolCallId is already bound to a different request")
                if str(row["state"]) == "completed" and row["response_json"]:
                    return json.loads(str(row["response_json"]))
                raise GatewayRequestUnresolved("Original tool request is pending or its outcome is unknown; inspect its receipt before retrying")
            # Old receipts cannot prove the original Gateway argument mapping.
            # Neither pick a newest historical row nor replay a claimed effect.
            historical = conn.execute("SELECT 1 FROM agent_approvals WHERE session_id = ? AND tool_call_id = ? LIMIT 1",
                (session_id, tool_call_id)).fetchone()
            if historical is not None:
                raise GatewayRequestUnresolved("Historical tool approval exists; inspect the original receipt, automatic replay is unavailable")
            binding = request.get("executionBinding")
            binding = binding if isinstance(binding, Mapping) else {}
            conn.execute("INSERT INTO agent_gateway_requests(session_id, tool_call_id, request_sha256, state, response_json, created_at_ms, updated_at_ms, turn_id, client_message_id) VALUES (?, ?, ?, 'admitted', NULL, ?, ?, ?, ?)",
                (session_id, tool_call_id, digest, timestamp, timestamp,
                    str(binding.get("turnId") or ""), str(binding.get("clientMessageId") or "")))
        return None

    def complete(self, request: Mapping[str, object], response: Mapping[str, object]) -> None:
        encoded = json.dumps(dict(response), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)
        with self.sessions._connect() as conn:
            cursor = conn.execute("UPDATE agent_gateway_requests SET state = 'completed', response_json = ?, updated_at_ms = ? WHERE session_id = ? AND tool_call_id = ? AND state = 'admitted'",
                (encoded, int(time.time() * 1000), str(request["sessionId"]), str(request["toolCallId"])))
            if cursor.rowcount != 1:
                raise GatewayRequestUnresolved("Tool result could not be durably recorded; outcome is unknown")

    def unknown(self, request: Mapping[str, object]) -> None:
        with self.sessions._connect() as conn:
            conn.execute("UPDATE agent_gateway_requests SET state = 'unknown', updated_at_ms = ? WHERE session_id = ? AND tool_call_id = ? AND state = 'admitted'",
                (int(time.time() * 1000), str(request["sessionId"]), str(request["toolCallId"])))
