"""At-most-once resident Source notice index; execution/admission remain in Pi.

A reserved occurrence is never retried after an uncertain outcome or crash.
The until_ack item and explicit ACK owner are independent of this index.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from typing import Any

from .agent_coordinator_work import AgentCoordinatorWork
from .agent_session_mode_gate import AgentSessionModeConflict
from .pi.values import PiRuntimeCommandRejected, PiRuntimeTurnConflict


class AgentCoordinatorDelivery:
    def __init__(self, *, sessions: Any, receipts: Any, context: Any,
                 eligible: Callable[[str], bool], submit: Callable[..., Mapping[str, object]]) -> None:
        self.sessions = sessions
        self.receipts = receipts
        self.context = context
        self.eligible = eligible
        self.submit = submit

    def reconcile_once(self, *, limit: int = 1) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError('delivery limit must be between 1 and 20')
        self._index_results()
        with self.sessions._connect() as conn:
            existing = conn.execute("SELECT * FROM agent_coordinator_result_deliveries WHERE phase IN ('reserved','uncertain','accepted') AND terminal_refs_json='[]' ORDER BY checked_at_ms, created_at_ms, delivery_id LIMIT 20").fetchall()
        for row in existing:
            self._reconcile_original(row)
        with self.sessions._connect() as conn:
            candidates = conn.execute("SELECT * FROM agent_coordinator_result_deliveries WHERE phase='pending' ORDER BY created_at_ms, delivery_id LIMIT 20").fetchall()
        admitted = 0
        attempted = 0
        for row in candidates:
            if attempted >= limit:
                break
            if self._retire_invalid(row):
                continue
            if not self.eligible(str(row['source_session_id'])):
                # Even pending invalid ownership/ACK can retire without a Host.
                self._retire_invalid(row)
                continue
            if not self._claim(row):
                continue
            attempted += 1
            try:
                response = self.submit(source_id=str(row['source_session_id']),
                    client=str(row['source_client_message_id']), item_id=str(row['context_item_id']),
                    message="原工作结果已返回。请核对所附原对话证据，报告结果和仍不确定的部分，并按原授权继续当前工作。返回不是验收或 ACK；不要重发原任务，也不要把未知状态写成成功。",
                    on_prepared=lambda envelope, occurrence=row: self._freeze(occurrence, envelope),
                    before_native_write=lambda occurrence=row: self._guard_native_write(occurrence))
            except (AgentSessionModeConflict, PiRuntimeTurnConflict) as exc:
                # This service's entry/reservation failed before command creation
                # and envelope preparation. Only that proven non-submission defers.
                with self.sessions._connect() as conn:
                    conn.execute("UPDATE agent_coordinator_result_deliveries SET phase='pending', last_error_code=? WHERE delivery_id=? AND phase IN ('reserved','uncertain') AND envelope_json=''",
                                 (type(exc).__name__, row['delivery_id']))
            except Exception as exc:
                self._uncertain(row, type(exc).__name__)
            else:
                if response.get('cancelled') is True and response.get('accepted') is False:
                    with self.sessions._connect() as conn:
                        conn.execute("UPDATE agent_coordinator_result_deliveries SET phase='cancelled', acceptance_json=?, checked_at_ms=? WHERE delivery_id=? AND phase IN ('reserved','uncertain')",
                                     (_json(response), _now(), row['delivery_id']))
                else:
                    admitted += int(self._reconcile_original(row))
        return admitted

    def _index_results(self) -> None:
        with self.sessions._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute("SELECT a.attempt_id, a.target_session_id, a.context_item_id, a.created_at_ms, w.coordinator_id, w.source_session_id, i.payload_json "
                "FROM agent_coordinator_work_attempts a JOIN agent_coordinator_work w ON w.work_id=a.work_id "
                "JOIN agent_context_items i ON i.item_id=a.context_item_id WHERE a.projected_at_ms IS NOT NULL AND a.retired_reason='' AND NOT EXISTS (SELECT 1 FROM agent_coordinator_result_deliveries d WHERE d.attempt_id=a.attempt_id) ORDER BY a.created_at_ms, a.attempt_id LIMIT 20 ").fetchall()
            for row in rows:
                digest = _digest(_json(json.loads(str(row['payload_json']))))
                occurrence = _digest(_json([row['coordinator_id'], row['source_session_id'], row['attempt_id'], row['context_item_id'], digest]))
                conn.execute("INSERT OR IGNORE INTO agent_coordinator_result_deliveries(delivery_id,coordinator_id,source_session_id,target_session_id,attempt_id,context_item_id,result_sha256,source_client_message_id,phase,created_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f'coordinator-delivery:{occurrence}', row['coordinator_id'], row['source_session_id'], row['target_session_id'], row['attempt_id'], row['context_item_id'], digest,
                     f'coordinator-notice:{occurrence}', 'pending', row['created_at_ms']))

    def _invalid_reason(self, conn: sqlite3.Connection, row: Mapping[str, Any]) -> str:
        if not AgentCoordinatorWork._active_link(conn, row):
            return 'ownership_retired'
        original = conn.execute("SELECT a.context_item_id, a.target_session_id, w.work_id, w.source_session_id, w.coordinator_id FROM agent_coordinator_work_attempts a JOIN agent_coordinator_work w ON w.work_id=a.work_id WHERE a.attempt_id=?", (row['attempt_id'],)).fetchone()
        item = conn.execute('SELECT * FROM agent_context_items WHERE item_id=?', (row['context_item_id'],)).fetchone()
        if original is None or item is None:
            return 'original_evidence_missing'
        payload = json.loads(str(item['payload_json']))
        binding = {'workId': original['work_id'], 'attemptId': row['attempt_id'], 'coordinatorId': row['coordinator_id'],
                   'sourceSessionId': row['source_session_id'], 'targetSessionId': row['target_session_id']}
        if (original['context_item_id'] != row['context_item_id'] or original['target_session_id'] != row['target_session_id']
            or original['source_session_id'] != row['source_session_id'] or original['coordinator_id'] != row['coordinator_id']
            or item['session_id'] != row['source_session_id'] or item['source_kind'] != 'coordinator_result'
            or item['source_id'] != row['attempt_id'] or item['lifecycle'] != 'until_ack'
            or not isinstance(payload, dict) or payload.get('schemaVersion') != 'rag-ime.coordinator-work-result-context.v1'
            or payload.get('authority') != 'evidence_only'
            or any(payload.get(key) != value for key, value in binding.items())):
            return 'foreign_result_binding'
        if _digest(_json(payload)) != row['result_sha256']:
            return 'original_result_changed'
        if item['status'] not in {'pending', 'delivered'} or (item['expires_at_ms'] is not None and item['expires_at_ms'] <= _now()):
            return 'result_withdrawn'
        return ''

    def _retire_invalid(self, row: Mapping[str, Any]) -> bool:
        with self.sessions._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            reason = self._invalid_reason(conn, row)
            if reason:
                conn.execute("UPDATE agent_coordinator_result_deliveries SET phase='retired', retired_reason=?, checked_at_ms=? WHERE delivery_id=? AND phase='pending'", (reason, _now(), row['delivery_id']))
            return bool(reason)

    def _claim(self, row: Mapping[str, Any]) -> bool:
        with self.sessions._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            reason = self._invalid_reason(conn, row)
            if reason:
                conn.execute("UPDATE agent_coordinator_result_deliveries SET phase='retired', retired_reason=? WHERE delivery_id=? AND phase='pending'", (reason, row['delivery_id']))
                return False
            return conn.execute("UPDATE agent_coordinator_result_deliveries SET phase='reserved', checked_at_ms=? WHERE delivery_id=? AND phase='pending'", (_now(), row['delivery_id'])).rowcount == 1

    def _freeze(self, row: Mapping[str, Any], envelope: Mapping[str, object]) -> None:
        encoded = _json(envelope)
        ids = envelope.get('contextItemIds')
        if (envelope.get('sessionId') != row['source_session_id'] or envelope.get('clientMessageId') != row['source_client_message_id']
            or envelope.get('delivery') != 'prompt' or not isinstance(ids, list) or row['context_item_id'] not in ids
            or not isinstance(envelope.get('message'), str)):
            raise ValueError('prepared envelope does not contain the exact original result')
        with self.sessions._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if self._invalid_reason(conn, row):
                raise ValueError('original result ownership changed before admission')
            changed = conn.execute("UPDATE agent_coordinator_result_deliveries SET phase='uncertain', envelope_json=?, envelope_sha256=?, context_item_ids_json=?, checked_at_ms=? "
                "WHERE delivery_id=? AND phase IN ('reserved','uncertain') AND envelope_json=''",
                (encoded, _digest(str(envelope['message'])), _json(ids), _now(), row['delivery_id'])).rowcount
            if changed != 1:
                raise ValueError('original delivery was already prepared or retired')
        # SQLite writer is released before the existing admission/write fence.

    def _guard_native_write(self, row: Mapping[str, Any]) -> None:
        # Called inside the existing native JSONL write / admission / Stop
        # fence, after tool preflight; this transaction ends before native write.
        with self.sessions._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            fresh = conn.execute('SELECT * FROM agent_coordinator_result_deliveries WHERE delivery_id=?', (row['delivery_id'],)).fetchone()
            reason = self._invalid_reason(conn, row)
            if fresh is None or fresh['phase'] not in {'reserved', 'uncertain'} or not fresh['envelope_json']:
                reason = reason or 'original_delivery_not_prepared'
            if reason and fresh is not None:
                conn.execute("UPDATE agent_coordinator_result_deliveries SET phase='retired', retired_reason=?, checked_at_ms=? WHERE delivery_id=? AND phase IN ('reserved','uncertain')",
                             (reason, _now(), row['delivery_id']))
        # Raise after commit so the retirement tombstone survives rejection.
        if reason:
            raise PiRuntimeCommandRejected('Original coordinator result is no longer eligible',
                                           host_error_code='COORDINATOR_DELIVERY_RETIRED')

    def _uncertain(self, row: Mapping[str, Any], code: str) -> None:
        with self.sessions._connect() as conn:
            conn.execute("UPDATE agent_coordinator_result_deliveries SET phase='uncertain', last_error_code=?, checked_at_ms=? WHERE delivery_id=? AND phase IN ('reserved','uncertain')", (code[:80], _now(), row['delivery_id']))
        self._reconcile_original(row)

    def _reconcile_original(self, row: Mapping[str, Any]) -> bool:
        proof = self.receipts.acceptance_evidence_for_exact_command(command_scope='session_prompt', scope_id=str(row['source_session_id']), client_message_id=str(row['source_client_message_id']))
        if proof is None:
            proof = self.sessions.prompt_acceptance_evidence(str(row['source_session_id']), str(row['source_client_message_id']))
        if not proof or proof.get('clientMessageId') != row['source_client_message_id'] or not proof.get('turnId'):
            with self.sessions._connect() as conn:
                conn.execute("UPDATE agent_coordinator_result_deliveries SET phase=CASE WHEN phase='reserved' THEN 'uncertain' ELSE phase END, checked_at_ms=? WHERE delivery_id=? AND phase IN ('reserved','uncertain','accepted')", (_now(), row['delivery_id']))
            return False
        turn = str(proof['turnId'])
        terminal = self.sessions.runtime_turn_terminal_event(str(row['source_session_id']), turn)
        phase = 'cancelled' if terminal and terminal.get('status') == 'aborted' else 'accepted'
        with self.sessions._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            fresh = conn.execute('SELECT * FROM agent_coordinator_result_deliveries WHERE delivery_id=?', (row['delivery_id'],)).fetchone()
            if fresh is None or fresh['phase'] == 'retired' or not fresh['envelope_json']:
                return False
            if fresh['source_turn_id'] and fresh['source_turn_id'] != turn:
                raise ValueError('original delivery acceptance changed identity')
            first = not bool(fresh['source_turn_id'])
            conn.execute("UPDATE agent_coordinator_result_deliveries SET phase=?, source_turn_id=?, acceptance_json=?, terminal_refs_json=?, last_error_code='', checked_at_ms=? WHERE delivery_id=?",
                (phase, turn, fresh['acceptance_json'] if fresh['source_turn_id'] else _json(proof), _json([terminal]) if terminal else fresh['terminal_refs_json'], _now(), row['delivery_id']))
        self.context.mark_delivered(json.loads(fresh['context_item_ids_json']), turn_id=turn,
                                    expected_delivery_id=f"dispatch:client:{row['source_client_message_id']}")
        return first


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> int:
    return int(time.time() * 1000)
