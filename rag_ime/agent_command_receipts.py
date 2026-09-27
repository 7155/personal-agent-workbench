from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .db import apply_database_migrations


DEFAULT_PENDING_RECOVERY_GRACE_MS = 30_000


class AgentCommandReceiptError(ValueError):
    http_status = 409
    error_code = "AGENT_COMMAND_RECEIPT_ERROR"
    receipt_state = "failed"

    def __init__(
        self,
        message: str,
        *,
        client_message_id: str = "",
        cause_code: str = "",
        recovery_state: str = "",
    ) -> None:
        super().__init__(message)
        self.client_message_id = client_message_id
        self.cause_code = cause_code
        self.recovery_state = recovery_state

    def response_payload(self) -> dict[str, object]:
        receipt: dict[str, object] = {
            "state": self.receipt_state,
            "clientMessageId": self.client_message_id,
        }
        if self.cause_code:
            receipt["causeCode"] = self.cause_code
        if self.recovery_state:
            receipt["recoveryState"] = self.recovery_state
        return {
            "code": self.error_code,
            "commandReceipt": receipt,
        }


class AgentCommandReceiptConflict(AgentCommandReceiptError):
    error_code = "AGENT_COMMAND_CONFLICT"
    receipt_state = "conflict"


class AgentCommandReceiptPending(AgentCommandReceiptError):
    error_code = "AGENT_COMMAND_PENDING"
    receipt_state = "pending"


class AgentCommandReceiptFailed(AgentCommandReceiptError):
    error_code = "AGENT_COMMAND_FAILED"
    receipt_state = "failed"


class AgentTurnConflictError(ValueError):
    """A direct Agent prompt cannot start while another owner holds the turn."""

    error_code = "AGENT_TURN_CONFLICT"


@dataclass(frozen=True)
class AgentCommandClaim:
    claim_token: str
    replay_response: dict[str, object] | None = None

    @property
    def is_replay(self) -> bool:
        return self.replay_response is not None


class AgentCommandReceiptStore:
    """Durable idempotency boundary for commands that start Agent turns."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        pending_recovery_grace_ms: int = (
            DEFAULT_PENDING_RECOVERY_GRACE_MS
        ),
    ):
        self.db_path = Path(db_path)
        self.pending_recovery_grace_ms = max(
            0,
            int(pending_recovery_grace_ms),
        )

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)

    def begin(
        self,
        *,
        command_scope: str,
        scope_id: str,
        client_message_id: str,
        payload: Mapping[str, object],
    ) -> AgentCommandClaim:
        digest = _sha256_json(payload)
        semantic_digest = _semantic_payload_sha256(payload)
        retry_of_client_message_id = str(
            payload.get("retryOfClientMessageId") or ""
        ).strip()
        claim_token = str(uuid.uuid4())
        timestamp = _now_ms()
        with self._connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_command_receipts
                WHERE command_scope = ? AND scope_id = ? AND client_message_id = ?
                """,
                (command_scope, scope_id, client_message_id),
            ).fetchone()
            if row is None:
                if retry_of_client_message_id:
                    _validate_retry_lineage(
                        conn,
                        command_scope=command_scope,
                        scope_id=scope_id,
                        retry_of_client_message_id=(
                            retry_of_client_message_id
                        ),
                        semantic_payload_sha256=(
                            semantic_digest
                        ),
                        client_message_id=client_message_id,
                    )
                conn.execute(
                    """
                    INSERT INTO agent_command_receipts(
                        command_scope, scope_id, client_message_id, payload_sha256,
                        semantic_payload_sha256, retry_of_client_message_id,
                        claim_token, state, response_json, error,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, '', ?, ?)
                    """,
                    (
                        command_scope,
                        scope_id,
                        client_message_id,
                        digest,
                        semantic_digest,
                        retry_of_client_message_id,
                        claim_token,
                        timestamp,
                        timestamp,
                    ),
                )
                return AgentCommandClaim(claim_token=claim_token)

            if str(row["payload_sha256"]) != digest:
                raise _new_command_required_conflict(
                    "clientMessageId belongs to a different command",
                    client_message_id=client_message_id,
                )
            state = str(row["state"])
            if state == "accepted":
                response = json.loads(str(row["response_json"] or "{}"))
                if not isinstance(response, dict):
                    raise RuntimeError("stored Agent command response is invalid")
                return AgentCommandClaim(
                    claim_token=str(row["claim_token"]),
                    replay_response=dict(response),
                )
            if state == "failed":
                raise _failed_receipt(
                    client_message_id=client_message_id,
                    stored_error=str(row["error"] or ""),
                )
            recovery_state = (
                "unresolved"
                if (
                    timestamp
                    - int(row["updated_at_ms"])
                    >= self.pending_recovery_grace_ms
                )
                else "in_flight"
            )
            message = (
                "the original Agent command may have been accepted, "
                "but no durable acceptance evidence is available; "
                "do not execute it again automatically"
                if recovery_state == "unresolved"
                else (
                    "the Agent command is already pending; "
                    "refresh the session snapshot before retrying"
                )
            )
            raise AgentCommandReceiptPending(
                message,
                client_message_id=client_message_id,
                recovery_state=recovery_state,
            )

    def accept_pending_from_evidence(
        self,
        *,
        command_scope: str,
        scope_id: str,
        client_message_id: str,
        payload: Mapping[str, object],
        response: Mapping[str, object],
    ) -> dict[str, object]:
        """Terminalize pending only after a durable acceptance proves execution."""

        digest = _sha256_json(payload)
        accepted = dict(response)
        encoded = _json(accepted)
        with self._connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_command_receipts
                WHERE command_scope = ? AND scope_id = ? AND client_message_id = ?
                """,
                (command_scope, scope_id, client_message_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "Agent command receipt disappeared before evidence reconciliation"
                )
            if str(row["payload_sha256"]) != digest:
                raise _new_command_required_conflict(
                    "clientMessageId belongs to a different command",
                    client_message_id=client_message_id,
                )
            state = str(row["state"])
            if state == "accepted":
                return _accepted_response(row)
            if state == "failed":
                raise _failed_receipt(
                    client_message_id=client_message_id,
                    stored_error=str(row["error"] or ""),
                )
            cursor = conn.execute(
                """
                UPDATE agent_command_receipts
                SET state = 'accepted', response_json = ?, error = '',
                    updated_at_ms = ?
                WHERE command_scope = ? AND scope_id = ?
                  AND client_message_id = ? AND state = 'pending'
                """,
                (
                    encoded,
                    _now_ms(),
                    command_scope,
                    scope_id,
                    client_message_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Agent command receipt changed during evidence reconciliation"
                )
        return accepted

    def reopen_accepted_response_for_compatibility(
        self,
        *,
        command_scope: str,
        scope_id: str,
        client_message_id: str,
        payload: Mapping[str, object],
        expected_response: Mapping[str, object],
    ) -> AgentCommandClaim:
        """Atomically replace one exact obsolete accepted response.

        This is intentionally narrower than a retry API.  A caller must first
        read an accepted response through ``begin`` and then supply that exact
        response here.  Only an unchanged row is reopened; a concurrent repair
        returns its newer response or reports the in-flight claim instead of
        executing the command twice.
        """

        digest = _sha256_json(payload)
        expected = dict(expected_response)
        claim_token = str(uuid.uuid4())
        timestamp = _now_ms()
        with self._connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_command_receipts
                WHERE command_scope = ? AND scope_id = ?
                  AND client_message_id = ?
                """,
                (command_scope, scope_id, client_message_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "Agent command receipt disappeared before compatibility repair"
                )
            if str(row["payload_sha256"]) != digest:
                raise _new_command_required_conflict(
                    "clientMessageId belongs to a different command",
                    client_message_id=client_message_id,
                )
            state = str(row["state"])
            if state == "failed":
                raise _failed_receipt(
                    client_message_id=client_message_id,
                    stored_error=str(row["error"] or ""),
                )
            if state == "pending":
                raise AgentCommandReceiptPending(
                    "the Agent command compatibility repair is already pending; "
                    "refresh before retrying",
                    client_message_id=client_message_id,
                    recovery_state="in_flight",
                )
            current = _accepted_response(row)
            if current != expected:
                return AgentCommandClaim(
                    claim_token=str(row["claim_token"]),
                    replay_response=current,
                )
            cursor = conn.execute(
                """
                UPDATE agent_command_receipts
                SET claim_token = ?, state = 'pending', response_json = NULL,
                    error = '', updated_at_ms = ?
                WHERE command_scope = ? AND scope_id = ?
                  AND client_message_id = ? AND claim_token = ?
                  AND state = 'accepted' AND response_json = ?
                """,
                (
                    claim_token,
                    timestamp,
                    command_scope,
                    scope_id,
                    client_message_id,
                    str(row["claim_token"]),
                    str(row["response_json"] or ""),
                ),
            )
            if cursor.rowcount != 1:
                raise AgentCommandReceiptPending(
                    "the Agent command compatibility repair changed concurrently; "
                    "refresh before retrying",
                    client_message_id=client_message_id,
                    recovery_state="in_flight",
                )
        return AgentCommandClaim(claim_token=claim_token)

    def record_acceptance_evidence(
        self,
        claim: AgentCommandClaim,
        *,
        command_scope: str,
        scope_id: str,
        client_message_id: str,
        accepted: Mapping[str, object],
        retry_of_client_message_id: str = "",
    ) -> dict[str, object]:
        """Persist the first local proof immediately after Pi accepts.

        The receipt deliberately remains pending until the remaining local
        projections complete. If the process dies before this transaction,
        there is no source-proven Host ledger to establish remote acceptance;
        the command must therefore remain unresolved rather than be replayed.
        """

        evidence: dict[str, object] = {
            "schemaVersion": (
                "rag-ime.agent-command-acceptance-evidence.v1"
            ),
            "accepted": True,
            "clientMessageId": client_message_id,
            "turnId": str(accepted.get("turnId") or ""),
            "piEntryId": str(accepted.get("piEntryId") or ""),
        }
        lineage = str(retry_of_client_message_id).strip()
        if lineage:
            evidence["retryOfClientMessageId"] = lineage
        with self._connect(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE agent_command_receipts
                SET response_json = ?, updated_at_ms = ?
                WHERE command_scope = ? AND scope_id = ?
                  AND client_message_id = ? AND claim_token = ?
                  AND state = 'pending'
                """,
                (
                    _json(evidence),
                    _now_ms(),
                    command_scope,
                    scope_id,
                    client_message_id,
                    claim.claim_token,
                ),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    """
                    SELECT * FROM agent_command_receipts
                    WHERE command_scope = ? AND scope_id = ?
                      AND client_message_id = ?
                    """,
                    (command_scope, scope_id, client_message_id),
                ).fetchone()
                if row is not None and str(row["state"]) == "accepted":
                    return _accepted_response(row)
                raise RuntimeError(
                    "Agent command receipt changed before acceptance evidence"
                )
        return evidence

    def pending_acceptance_evidence(
        self,
        *,
        command_scope: str,
        scope_id: str,
        client_message_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Return exact pending acceptance evidence without changing state."""

        digest = _sha256_json(payload)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_command_receipts
                WHERE command_scope = ? AND scope_id = ?
                  AND client_message_id = ?
                """,
                (command_scope, scope_id, client_message_id),
            ).fetchone()
        if row is None:
            return None
        if str(row["payload_sha256"]) != digest:
            raise _new_command_required_conflict(
                "clientMessageId belongs to a different command",
                client_message_id=client_message_id,
            )
        if str(row["state"]) != "pending":
            return None
        return _stored_acceptance_evidence(
            str(row["response_json"] or "")
        )

    def accepted_response_for_exact_command(
        self, *, command_scope: str, scope_id: str, client_message_id: str,
    ) -> dict[str, object] | None:
        """Read an accepted receipt without re-admitting a turn or its payload.

        The caller must already own/authorize this scope. This also supports
        Room receipts whose execution identity is a root rather than a turn.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state,response_json FROM agent_command_receipts "
                "WHERE command_scope=? AND scope_id=? AND client_message_id=?",
                (command_scope, scope_id, client_message_id),
            ).fetchone()
        return _accepted_response(row) if row and row['state'] == 'accepted' else None

    def acceptance_evidence_for_exact_command(
        self,
        *,
        command_scope: str,
        scope_id: str,
        client_message_id: str,
    ) -> dict[str, object] | None:
        """Return content-free acceptance proof for one exact command key.

        Room restart recovery does not have the original prompt body available,
        so it cannot use the payload-digest lookup above.  The full idempotency
        key is nevertheless exact and already unique.  Only durable acceptance
        proof is projected; stored prompt content is never returned.
        """

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT state, response_json
                FROM agent_command_receipts
                WHERE command_scope = ? AND scope_id = ?
                  AND client_message_id = ?
                """,
                (command_scope, scope_id, client_message_id),
            ).fetchone()
        if row is None or str(row["state"]) == "failed":
            return None
        stored = _stored_acceptance_evidence(
            str(row["response_json"] or "")
        )
        if stored is not None:
            return stored
        if str(row["state"]) != "accepted":
            return None
        response = _accepted_response(row)
        turn_id = str(response.get("turnId") or "")
        if not turn_id:
            return None
        return {
            "schemaVersion": (
                "rag-ime.agent-command-acceptance-evidence.v1"
            ),
            "accepted": True,
            "clientMessageId": client_message_id,
            "turnId": turn_id,
            "piEntryId": str(response.get("piEntryId") or ""),
        }

    def failure_evidence_for_exact_command(
        self,
        *,
        command_scope: str,
        scope_id: str,
        client_message_id: str,
    ) -> dict[str, object] | None:
        """Return the typed failure for one exact durably failed command."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT state, error
                FROM agent_command_receipts
                WHERE command_scope = ? AND scope_id = ?
                  AND client_message_id = ?
                """,
                (command_scope, scope_id, client_message_id),
            ).fetchone()
        if row is None or str(row["state"]) != "failed":
            return None
        message, cause_code = _stored_failure(
            str(row["error"] or "")
        )
        evidence: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-command-failure.v1",
            "message": message,
        }
        if cause_code:
            evidence["causeCode"] = cause_code
        return evidence

    def complete(
        self,
        claim: AgentCommandClaim,
        *,
        command_scope: str,
        scope_id: str,
        client_message_id: str,
        response: Mapping[str, object],
    ) -> dict[str, object]:
        payload = dict(response)
        encoded = _json(payload)
        with self._connect(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE agent_command_receipts
                SET state = 'accepted', response_json = ?, error = '', updated_at_ms = ?
                WHERE command_scope = ? AND scope_id = ? AND client_message_id = ?
                  AND claim_token = ? AND state = 'pending'
                """,
                (
                    encoded,
                    _now_ms(),
                    command_scope,
                    scope_id,
                    client_message_id,
                    claim.claim_token,
                ),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    """
                    SELECT * FROM agent_command_receipts
                    WHERE command_scope = ? AND scope_id = ?
                      AND client_message_id = ?
                    """,
                    (
                        command_scope,
                        scope_id,
                        client_message_id,
                    ),
                ).fetchone()
                if (
                    row is not None
                    and str(row["claim_token"])
                    == claim.claim_token
                    and str(row["state"]) == "accepted"
                ):
                    return _accepted_response(row)
                raise RuntimeError(
                    "Agent command receipt changed before completion"
                )
        return payload

    def fail(
        self,
        claim: AgentCommandClaim,
        *,
        command_scope: str,
        scope_id: str,
        client_message_id: str,
        error: BaseException,
        cause_code: str = "",
    ) -> dict[str, object] | None:
        message = " ".join(str(error).split())[:240] or error.__class__.__name__
        normalized_cause_code = " ".join(
            str(cause_code).split()
        )[:80]
        stored_error = message
        if normalized_cause_code:
            stored_error = _json(
                {
                    "schemaVersion": (
                        "rag-ime.agent-command-failure.v1"
                    ),
                    "message": message,
                    "causeCode": normalized_cause_code,
                }
            )
        with self._connect(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_command_receipts
                WHERE command_scope = ? AND scope_id = ?
                  AND client_message_id = ?
                """,
                (command_scope, scope_id, client_message_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "Agent command receipt disappeared before failure"
                )
            if str(row["claim_token"]) != claim.claim_token:
                raise RuntimeError(
                    "Agent command receipt changed before failure"
                )
            if str(row["state"]) == "accepted":
                return _accepted_response(row)
            if str(row["state"]) == "failed":
                return None
            if _stored_acceptance_evidence(
                str(row["response_json"] or "")
            ) is not None:
                raise AgentCommandReceiptPending(
                    "Pi accepted the Agent command, but local projection is incomplete; "
                    "do not execute it again automatically",
                    client_message_id=client_message_id,
                    recovery_state="in_flight",
                )
            cursor = conn.execute(
                """
                UPDATE agent_command_receipts
                SET state = 'failed', response_json = NULL, error = ?, updated_at_ms = ?
                WHERE command_scope = ? AND scope_id = ? AND client_message_id = ?
                  AND claim_token = ? AND state = 'pending'
                """,
                (
                    stored_error,
                    _now_ms(),
                    command_scope,
                    scope_id,
                    client_message_id,
                    claim.claim_token,
                ),
            )
            if cursor.rowcount != 1:
                changed = conn.execute(
                    """
                    SELECT * FROM agent_command_receipts
                    WHERE command_scope = ? AND scope_id = ?
                      AND client_message_id = ?
                    """,
                    (command_scope, scope_id, client_message_id),
                ).fetchone()
                if changed is not None and str(changed["state"]) == "accepted":
                    return _accepted_response(changed)
                raise RuntimeError(
                    "Agent command receipt changed during failure"
                )
        return None

    def failed_receipt(
        self,
        *,
        command_scope: str,
        scope_id: str,
        client_message_id: str,
    ) -> AgentCommandReceiptFailed:
        """Project a typed public failure from the durable terminal receipt."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT state, error FROM agent_command_receipts
                WHERE command_scope = ? AND scope_id = ?
                  AND client_message_id = ?
                """,
                (command_scope, scope_id, client_message_id),
            ).fetchone()
        if row is None or str(row["state"]) != "failed":
            raise RuntimeError(
                "Agent command receipt is not durably failed"
            )
        return _failed_receipt(
            client_message_id=client_message_id,
            stored_error=str(row["error"] or ""),
        )

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def _sha256_json(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_json(dict(payload)).encode("utf-8")).hexdigest()


def _semantic_payload_sha256(payload: Mapping[str, object]) -> str:
    semantic = dict(payload)
    semantic.pop("retryOfClientMessageId", None)
    return _sha256_json(semantic)


def _validate_retry_lineage(
    conn: sqlite3.Connection,
    *,
    command_scope: str,
    scope_id: str,
    retry_of_client_message_id: str,
    semantic_payload_sha256: str,
    client_message_id: str,
) -> None:
    prior = conn.execute(
        """
        SELECT state, semantic_payload_sha256
        FROM agent_command_receipts
        WHERE command_scope = ? AND scope_id = ?
          AND client_message_id = ?
        """,
        (command_scope, scope_id, retry_of_client_message_id),
    ).fetchone()
    if prior is None:
        raise _new_command_required_conflict(
            "retryOfClientMessageId does not identify a command in this scope",
            client_message_id=client_message_id,
        )
    if str(prior["state"]) != "failed":
        raise _new_command_required_conflict(
            "only a durably failed command may have a successor",
            client_message_id=client_message_id,
        )
    prior_digest = str(prior["semantic_payload_sha256"] or "")
    if (
        not prior_digest
        or prior_digest != semantic_payload_sha256
    ):
        raise _new_command_required_conflict(
            "retry payload does not match the failed command",
            client_message_id=client_message_id,
        )
    successor = conn.execute(
        """
        SELECT client_message_id
        FROM agent_command_receipts
        WHERE command_scope = ? AND scope_id = ?
          AND retry_of_client_message_id = ?
        """,
        (command_scope, scope_id, retry_of_client_message_id),
    ).fetchone()
    if successor is not None:
        raise _new_command_required_conflict(
            "the failed command already has a retry successor",
            client_message_id=client_message_id,
        )


def _new_command_required_conflict(
    message: str,
    *,
    client_message_id: str,
) -> AgentCommandReceiptConflict:
    """Tell clients to allocate a new identity without exposing digests.

    Exact replays keep the original identity and payload. Changed content,
    attachments, delivery, or retry lineage is a distinct command and must use
    a fresh clientMessageId; internal SHA-256 values never enter the response.
    """

    return AgentCommandReceiptConflict(
        message,
        client_message_id=client_message_id,
        recovery_state="new_command_required",
    )


def _json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stored_failure(value: str) -> tuple[str, str]:
    fallback = value or "the original Agent command failed"
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return fallback, ""
    if not isinstance(decoded, dict):
        return fallback, ""
    if decoded.get("schemaVersion") != (
        "rag-ime.agent-command-failure.v1"
    ):
        return fallback, ""
    message = " ".join(str(decoded.get("message") or "").split())
    cause_code = " ".join(
        str(decoded.get("causeCode") or "").split()
    )
    return (
        message[:240] or "the original Agent command failed",
        cause_code[:80],
    )


def _failed_receipt(
    *,
    client_message_id: str,
    stored_error: str,
) -> AgentCommandReceiptFailed:
    _, cause_code = _stored_failure(stored_error)
    return AgentCommandReceiptFailed(
        "The Agent command failed before acceptance",
        client_message_id=client_message_id,
        cause_code=cause_code,
    )


def _accepted_response(row: sqlite3.Row) -> dict[str, object]:
    response = json.loads(str(row["response_json"] or "{}"))
    if not isinstance(response, dict):
        raise RuntimeError("stored Agent command response is invalid")
    return dict(response)


def _stored_acceptance_evidence(
    value: str,
) -> dict[str, object] | None:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(decoded, dict)
        or decoded.get("schemaVersion")
        != "rag-ime.agent-command-acceptance-evidence.v1"
        or decoded.get("accepted") is not True
    ):
        return None
    return dict(decoded)


def _now_ms() -> int:
    return int(time.time() * 1000)
