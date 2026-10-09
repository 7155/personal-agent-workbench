"""Real SQLite/application/admission; only the native wire and recall are doubled."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.pi.runtime import _HostedSessionState
from rag_ime.pi.values import PiRuntimeCommandAcceptanceUnknown
from tests import test_agent_coordinator_work as p1


class NativeWire:
    running = True

    def __init__(self, service, *, lost_ack=False, stop_before_write=False, close_before_write=False, mutation_before_write=None, mutation_after_write=None):
        self.service = service
        self.calls = []
        self.lost_ack = lost_ack
        self.stop_before_write = stop_before_write
        self.close_before_write = close_before_write
        self.mutation_before_write = mutation_before_write
        self.mutation_after_write = mutation_after_write

    def send(self, method, params=None, *, before_write=None, **kwargs):
        if method == 'session.prompt' and self.stop_before_write:
            self.service.runtime.abort(params['sessionId'])
        if method == 'session.prompt' and self.close_before_write:
            self.service.runtime._open_sessions.discard(params['sessionId'])
        if method == 'session.prompt' and self.mutation_before_write:
            self.mutation_before_write()
        if before_write:
            before_write()
        self.calls.append((method, dict(params or {})))
        if method == 'session.prompt':
            if self.mutation_after_write:
                self.mutation_after_write()
            if self.lost_ack:
                raise PiRuntimeCommandAcceptanceUnknown('native accepted, response lost')
            return {'accepted': True, 'turnId': 'source-notice-turn', 'clientMessageId':params['clientMessageId'],
                    'piEntryId':'native-source-input', 'disposition': 'started'}
        if method == 'session.abort':
            return {'schemaVersion':'rag-ime.pi-session-abort-receipt.v1', 'sessionId':params['sessionId'],
                'turnId':params['expectedTurnId'], 'lifecycle':{'schemaVersion':'pi.agent-abort-receipt.v1',
                    'idle':True, 'drained':True, 'pendingOperations':[]}}
        return {'accepted': True}

    def close(self):
        self.running = False

    def stop(self):
        self.close()


class CoordinatorDeliveryTests(unittest.TestCase):
    setUp = p1.CoordinatorWorkTests.setUp
    command = p1.CoordinatorWorkTests.command
    accept = p1.CoordinatorWorkTests.accept
    send = p1.CoordinatorWorkTests.send
    complete = p1.CoordinatorWorkTests.complete
    tables = p1.CoordinatorWorkTests.tables
    attempts = p1.CoordinatorWorkTests.attempts
    items = p1.CoordinatorWorkTests.items

    def seed(self):
        self.send()
        self.complete()
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        return self.items()[0]

    def resident(self, **options):
        native = NativeWire(self.service, **options)
        runtime = self.service.runtime
        runtime._client = native
        runtime._host_capabilities = {'sessionBoundAbort': True}
        runtime._open_sessions.add(self.source)
        runtime._states[self.source] = _HostedSessionState()
        self.service.sessions.set_status(self.source, 'idle')
        sync = patch.object(runtime, '_sync_prompt_tool_manifest')
        sync.start(); self.addCleanup(sync.stop)
        recall = patch.object(self.service.memory_context_application, 'ensure_bootstrap', return_value={})
        recall.start(); self.addCleanup(recall.stop)
        return native

    def rows(self):
        with self.service.sessions._connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM agent_coordinator_result_deliveries ORDER BY created_at_ms, delivery_id')]

    def tick(self):
        return self.service.coordinator_delivery.reconcile_once()

    def original_item(self, item):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT status, payload_json, acknowledged_at_ms FROM agent_context_items WHERE item_id=?', (item['itemId'],)).fetchone()

    def test_concurrent_claim_and_existing_user_stop_barriers_do_not_dispatch_target(self):
        item = self.seed()
        original = self.original_item(item)[1]
        originals = {item['itemId']: original}
        self.send(client='parallel-result-client', turn='parallel-result-turn')
        self.complete(client='parallel-result-client', turn='parallel-result-turn', sequence=2, text='Second independent result')
        self.service.coordinator_work.reconcile_once()
        other_context = self.service.context_runtime.enqueue(session_id=self.source, source_kind='manual',
            title='Retain ordinary context', lane='fact', lifecycle='persistent', payload={'fact':'ordinary context stays assembled'})
        self.assertEqual(len(self.items()), 2, 'default materialization remains unchanged')
        originals.update({item['itemId']:self.original_item(item)[1] for item in self.items()})
        native = self.resident()
        self.service.runtime.reserve_prompt_admission(self.source, client_message_id='existing-user')
        self.assertEqual(self.tick(), 0)
        self.assertEqual(native.calls, [])
        self.service.runtime.release_prompt_admission(self.source, client_message_id='existing-user')
        barrier = Barrier(2)
        def race(_):
            barrier.wait(timeout=5)
            return self.tick()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(race, range(2)))
        prompts = [params for method, params in native.calls if method == 'session.prompt']
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]['sessionId'], self.source)
        row = next(row for row in self.rows() if row['phase'] == 'accepted')
        other = next(row for row in self.rows() if row['phase'] == 'pending')
        item = {'itemId': row['context_item_id']}
        original = originals[item['itemId']]
        self.assertEqual(row['phase'], 'accepted')
        self.assertEqual(row['context_item_id'], item['itemId'])
        self.assertEqual(row['source_turn_id'], 'source-notice-turn')
        self.assertEqual(row['source_client_message_id'], prompts[0]['clientMessageId'])
        self.assertEqual(row['envelope_sha256'], hashlib.sha256(prompts[0]['message'].encode()).hexdigest())
        self.assertEqual(json.loads(row['envelope_json'])['message'], prompts[0]['message'])
        self.assertEqual(self.original_item(item)[1], original)
        self.assertIsNone(self.original_item(item)[2])
        self.assertEqual(len(self.attempts()), 2)
        envelope = json.loads(row['envelope_json'])
        self.assertIn(other_context['itemId'], envelope['contextItemIds'])
        self.assertNotIn(other['context_item_id'], envelope['contextItemIds'])
        attempt = next(attempt for attempt in self.attempts() if attempt['attemptId'] == row['attempt_id'])
        other_attempt = next(attempt for attempt in self.attempts() if attempt['attemptId'] == other['attempt_id'])
        self.assertIn(attempt['turnId'], prompts[0]['message'])
        self.assertNotIn(other_attempt['turnId'], prompts[0]['message'])
        with self.service.sessions._connect() as conn:
            pending = conn.execute('SELECT status, delivered_turn_id, acknowledged_at_ms FROM agent_context_items WHERE item_id=?', (other['context_item_id'],)).fetchone()
        self.assertEqual(tuple(pending), ('pending', '', None))
        self.assertIn('Retain ordinary context', prompts[0]['message'])
        with self.service.sessions._connect() as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                conn.execute("UPDATE agent_coordinator_result_deliveries SET envelope_json='changed' WHERE delivery_id=?", (row['delivery_id'],))
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                conn.execute('DELETE FROM agent_coordinator_result_deliveries WHERE delivery_id=?', (row['delivery_id'],))
        self.assertFalse(any(params.get('sessionId') == self.target for _, params in native.calls))

    def test_lost_ack_restart_recovers_exact_original_without_reassembly_or_resend(self):
        item = self.seed()
        native = self.resident(lost_ack=True)
        self.tick()
        row = self.rows()[0]
        self.assertEqual(row['phase'], 'uncertain')
        before = (row['envelope_json'], row['envelope_sha256'], row['source_client_message_id'])
        for _ in range(100):
            self.tick()
        self.assertEqual(sum(method == 'session.prompt' for method, _ in native.calls), 1)
        self.service.close()
        self.service = AgentService(db_path=self.db, runtime_config=self.config)
        self.addCleanup(self.service.close)
        # Cold reopen/unknown original proof cannot start a Host or resend.
        with patch.object(self.service.runtime, 'ensure', side_effect=AssertionError('cold auto wake')):
            for _ in range(100):
                self.tick()
        self.assertEqual(self.rows()[0]['phase'], 'uncertain')
        self.service.sessions.record_runtime_event(event_id='original-notice-acceptance', session_id=self.source,
            turn_id='original-source-turn', sequence=100, event_type='message_completed', created_at_ms=500,
            metrics={'promptAcceptance': {'clientMessageId': row['source_client_message_id'], 'turnId': 'original-source-turn', 'messageId': 'native-original-user'}})
        # Later current context/time must not replace the old frozen envelope.
        with self.service.sessions._connect() as conn:
            conn.execute("UPDATE agent_context_items SET summary='later wording' WHERE item_id=?", (item['itemId'],))
        with patch.object(self.service.prompt_delivery_application, 'deliver', side_effect=AssertionError('reassembly')):
            self.tick()
            for _ in range(100):
                self.tick()
        recovered = self.rows()[0]
        self.assertEqual(recovered['phase'], 'accepted')
        self.assertEqual(recovered['source_turn_id'], 'original-source-turn')
        self.assertEqual((recovered['envelope_json'], recovered['envelope_sha256'], recovered['source_client_message_id']), before)
        self.assertIsNone(self.original_item(item)[2])

    def test_ack_terminal_stop_and_new_occurrence_are_separate_from_acceptance(self):
        item = self.seed()
        native = self.resident(stop_before_write=True)
        self.tick()
        row = self.rows()[0]
        self.assertEqual(row['phase'], 'cancelled')
        self.assertFalse(any(method == 'session.prompt' for method, _ in native.calls))
        self.assertIsNone(self.original_item(item)[2])
        for _ in range(100):
            self.tick()
        self.assertEqual(self.rows()[0]['phase'], 'cancelled')
        # A distinct accepted target attempt creates a distinct occurrence.
        self.send(client='next-target-client', turn='next-target-turn')
        self.complete(client='next-target-client', turn='next-target-turn', sequence=2)
        self.service.coordinator_work.reconcile_once()
        native.stop_before_write = False
        self.service.runtime._states[self.source] = _HostedSessionState()
        self.service.sessions.set_status(self.source, 'idle')
        self.tick()
        next_row = self.rows()[-1]
        self.assertNotEqual(next_row['delivery_id'], row['delivery_id'])
        self.assertEqual(next_row['phase'], 'accepted')
        next_item = {'itemId': next_row['context_item_id']}
        self.service.sessions.record_runtime_event(event_id='source-said-done', session_id=self.source,
            turn_id=next_row['source_turn_id'], sequence=100, event_type='turn_completed', created_at_ms=500,
            redacted_summary='completed', metrics={'status': 'completed'})
        for _ in range(100):
            self.tick()
        self.assertIsNone(self.original_item(next_item)[2])
        self.assertEqual(sum(method == 'session.prompt' for method, _ in native.calls), 1)
        self.service.acknowledge_context_item(self.source, next_row['context_item_id'])
        self.assertIsNotNone(self.original_item(next_item)[2])
        self.assertEqual(self.tick(), 0)
        self.assertFalse(any(params.get('sessionId') == self.target for _, params in native.calls))

    def test_resident_eligibility_retirement_and_private_selector_leave_pending_evidence(self):
        # Empty repeated maintenance observations produce no command/Host.
        with patch.object(self.service.runtime, 'ensure', side_effect=AssertionError('auto Host')):
            for _ in range(100):
                self.assertEqual(self.tick(), 0)
        self.assertEqual(self.rows(), [])
        item = self.seed()
        manual = self.service.context_runtime.enqueue(session_id=self.source, source_kind='manual',
            title='Not a coordinator result', lifecycle='until_ack')
        for sid, selected in [(self.source, manual['itemId']), (self.target, item['itemId'])]:
            with self.assertRaisesRegex(ValueError, 'selector'):
                self.service.context_runtime.materialize(sid, _coordinator_result_item_id=selected)
        native = self.resident()
        with patch.object(self.service.runtime, 'ensure', side_effect=AssertionError('auto Host')):
            for status in ['busy', 'active', 'faulted']:
                with self.subTest(status=status):
                    self.service.sessions.set_status(self.source, status)
                    self.assertEqual(self.tick(), 0)
            self.service.sessions.set_status(self.source, 'idle')
            goal = self.service.sessions.mutate_agent_goal(self.source, {'action':'confirm_setup',
                'confirmed':True, 'expectedRevision':0, 'objective':'Bounded original goal'})['workflow']['goal']
            paused = self.service.sessions.mutate_agent_goal(self.source, {'action':'pause',
                'expectedRevision':goal['revision']})['workflow']['goal']
            for _ in range(100):
                self.assertEqual(self.tick(), 0)
            self.assertEqual(self.rows()[0]['phase'], 'pending')
            self.service.sessions.mutate_agent_goal(self.source, {'action':'resume', 'expectedRevision':paused['revision']})
            for flag, value in [('recoverable', True), ('compaction_target', 'original'),
                                ('abort_pending_admission', True), ('abort_requested_turn_id', 'original'),
                                ('turn_id', 'original')]:
                with self.subTest(flag=flag):
                    state = self.service.runtime._states[self.source]
                    original = getattr(state, flag)
                    setattr(state, flag, value)
                    self.assertEqual(self.tick(), 0)
                    setattr(state, flag, original)
            self.service.runtime._open_sessions.discard(self.source)
            for _ in range(100):
                self.assertEqual(self.tick(), 0)
        self.assertEqual(native.calls, [])
        self.assertEqual(self.rows()[0]['phase'], 'pending')
        self.assertIsNone(self.original_item(item)[2])
        old_source = self.source
        self.service.sessions.archive(old_source)
        with patch.object(self.service.runtime, 'require_session_engine'):
            replacement = self.service.ensure_coordinator({})['sourceSessionId']
        self.assertNotEqual(old_source, replacement)
        self.tick()
        self.assertEqual(self.rows()[0]['retired_reason'], 'ownership_retired')
        self.assertFalse(self.service.context_runtime.materialize(replacement)['items'])
        self.assertEqual(native.calls, [])

    def test_foreign_result_binding_retires_without_native_effect(self):
        item = self.seed()
        native = self.resident()
        with self.service.sessions._connect() as conn:
            payload = json.loads(conn.execute('SELECT payload_json FROM agent_context_items WHERE item_id=?', (item['itemId'],)).fetchone()[0])
            payload['sourceSessionId'] = self.target
            conn.execute('UPDATE agent_context_items SET payload_json=? WHERE item_id=?', (json.dumps(payload), item['itemId']))
        self.tick()
        self.assertEqual(self.rows()[0]['retired_reason'], 'foreign_result_binding')
        self.assertEqual(native.calls, [])
        self.assertIsNone(self.original_item(item)[2])

    def test_actual_admitted_source_stop_cold_restart_does_not_resurrect(self):
        item = self.seed()
        native = self.resident()
        self.tick()
        row = self.rows()[0]
        self.assertEqual(row['phase'], 'accepted')
        self.service.runtime.abort(self.source)
        aborts = [params for method, params in native.calls if method == 'session.abort']
        self.assertEqual(len(aborts), 1)
        self.assertEqual(aborts[0]['sessionId'], self.source)
        self.assertEqual(aborts[0]['expectedTurnId'], row['source_turn_id'])
        self.assertEqual(aborts[0]['clientMessageId'], row['source_client_message_id'])
        self.service.sessions.record_runtime_event(event_id='source-original-stopped', session_id=self.source,
            turn_id=row['source_turn_id'], sequence=101, event_type='turn_completed', created_at_ms=501,
            redacted_summary='aborted', metrics={'status':'aborted'})
        self.tick()
        self.assertEqual(self.rows()[0]['phase'], 'cancelled')
        self.service.close()
        self.service = AgentService(db_path=self.db, runtime_config=self.config)
        self.addCleanup(self.service.close)
        with patch.object(self.service.runtime, 'ensure', side_effect=AssertionError('stopped auto Host')):
            for _ in range(100):
                self.assertEqual(self.tick(), 0)
        self.assertEqual(self.rows()[0]['phase'], 'cancelled')
        self.assertIsNone(self.original_item(item)[2])
        self.assertEqual(sum(method == 'session.prompt' for method, _ in native.calls), 1)

    def test_resident_closes_at_native_write_fence_without_cold_ensure(self):
        self.seed()
        native = self.resident(close_before_write=True)
        with patch.object(self.service.runtime, 'ensure', side_effect=AssertionError('cold write')):
            self.tick()
            for _ in range(100):
                self.tick()
        self.assertFalse(any(method == 'session.prompt' for method, _ in native.calls))
        self.assertEqual(self.rows()[0]['phase'], 'uncertain')
        self.assertTrue(self.rows()[0]['envelope_json'])

    def test_existing_maintenance_tick_owns_harvest_and_one_notice_without_new_loop(self):
        self.send()
        self.complete()
        native = self.resident()
        with patch.object(self.service.jev_application, 'tick', return_value=0), \
             patch.object(self.service, '_run_eval_schedules_once', return_value=0):
            self.assertEqual(self.service._run_scheduled_work_once(), 2)
            self.assertEqual(self.service._run_scheduled_work_once(), 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['phase'], 'accepted')
        self.assertEqual(sum(method == 'session.prompt' for method, _ in native.calls), 1)

    def test_source_archived_after_freeze_at_native_write_retires_without_dispatch(self):
        item = self.seed()
        seen = []
        def archive_at_write():
            seen.append(bool(self.rows()[0]['envelope_json']))
            self.service.sessions.archive(self.source)
        native = self.resident(mutation_before_write=archive_at_write)
        self.tick()
        self.assertEqual(seen, [True])
        self.assertFalse(any(method == 'session.prompt' for method, _ in native.calls))
        self.assertEqual(self.service.sessions.get(self.source)['status'], 'archived')
        self.assertEqual(self.rows()[0]['phase'], 'retired')
        self.assertEqual(self.rows()[0]['retired_reason'], 'ownership_retired')
        self.assertIsNone(self.original_item(item)[2])
        for _ in range(100):
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.rows()[0]['phase'], 'retired')

    def test_original_result_ack_after_freeze_at_native_write_retires_without_dispatch(self):
        item = self.seed()
        prepared = []
        def ack_at_write():
            prepared.append(self.rows()[0]['envelope_sha256'])
            self.service.acknowledge_context_item(self.source, item['itemId'])
        native = self.resident(mutation_before_write=ack_at_write)
        self.tick()
        self.assertTrue(prepared[0])
        self.assertFalse(any(method == 'session.prompt' for method, _ in native.calls))
        self.assertEqual(self.rows()[0]['phase'], 'retired')
        self.assertEqual(self.rows()[0]['retired_reason'], 'result_withdrawn')
        self.assertIsNotNone(self.original_item(item)[2])
        frozen = self.rows()[0]['envelope_json']
        for _ in range(100):
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.rows()[0]['envelope_json'], frozen)

    def test_deleted_pending_source_retires_and_same_maintenance_tick_reaches_replacement(self):
        self.seed()
        self.assertEqual(self.tick(), 0)
        old = self.rows()[0]
        old_source = self.source
        self.service.delete_session(old_source)
        with patch.object(self.service.runtime, 'require_session_engine'):
            self.source = self.service.ensure_coordinator({})['sourceSessionId']
        self.assertNotEqual(old_source, self.source)
        self.target = self.command('create_session', clientRequestId='replacement-worker', input={'task':'Read replacement notes'})['target']['id']
        self.send(client='replacement-input', turn='replacement-turn')
        with patch.object(self.service.sessions, 'bind_runtime_session') as deferred_bind:
            transcript = self.complete(client='replacement-input', turn='replacement-turn', text='Replacement original evidence')
        entries = [json.loads(line) for line in transcript.read_text().splitlines()]
        for entry in entries:
            for field in ('id', 'parentId'):
                if entry.get(field) == 'physical':
                    entry[field] = 'replacement-physical'
        transcript.write_text(''.join(json.dumps(entry)+'\n' for entry in entries))
        binding = deferred_bind.call_args.kwargs
        binding['external_session_id'] = 'replacement-physical'
        self.service.sessions.bind_runtime_session(self.target, **binding)
        native = self.resident()
        with patch.object(self.service.jev_application, 'tick', return_value=0), \
             patch.object(self.service, '_run_eval_schedules_once', return_value=0) as later_owner:
            self.assertEqual(self.service._run_scheduled_work_once(), 2)
            self.assertEqual(self.service._run_scheduled_work_once(), 0)
        self.assertEqual(later_owner.call_count, 2)
        retired = next(row for row in self.rows() if row['delivery_id'] == old['delivery_id'])
        accepted = next(row for row in self.rows() if row['phase'] == 'accepted')
        self.assertEqual(retired['phase'], 'retired')
        self.assertEqual(retired['retired_reason'], 'ownership_retired')
        self.assertEqual(retired['source_session_id'], old_source)
        self.assertEqual(accepted['source_session_id'], self.source)
        self.assertNotEqual(retired['source_client_message_id'], accepted['source_client_message_id'])
        self.assertEqual(sum(method == 'session.prompt' for method, _ in native.calls), 1)

    def test_native_acceptance_then_archive_preserves_archive_and_original_receipt(self):
        item = self.seed()
        native = self.resident(mutation_after_write=lambda:self.service.sessions.archive(self.source))
        self.assertEqual(self.tick(), 1)
        self.assertEqual(sum(method == 'session.prompt' for method, _ in native.calls), 1)
        self.assertEqual(self.rows()[0]['phase'], 'accepted')
        self.assertEqual(self.rows()[0]['source_turn_id'], 'source-notice-turn')
        self.assertEqual(self.service.sessions.get(self.source)['status'], 'archived')
        self.assertFalse(any(event.event_type == 'status_changed' and event.payload.get('status') == 'busy'
            for event in self.service.events.replay(self.source)[0]))
        self.assertIsNone(self.original_item(item)[2])

    def settle_notice(self, native, row, *, aborted=False):
        turn, client = row['source_turn_id'], row['source_client_message_id']
        receipt = {'schemaVersion':'pi.agent-settled.v2', 'sessionId':'original-native-session',
            'runId':turn, 'scopeId':f'original-native-session:{turn}',
            'disposition':'aborted' if aborted else 'completed', 'aborted':aborted,
            'pendingOperations':0, 'operations':{'pending':0},
            'finalMessage':{'role':'assistant','content':[{'type':'text','text':'Original notice read'}]}}
        with patch.object(self.service.runtime, '_refresh_terminal_recent_projection'):
            self.service.runtime._handle_host_event({'protocolVersion':'2','event':'agent.event',
                'sessionId':self.source,'turnId':turn,'clientMessageId':client,
                'payload':{'type':'agent_settled','receipt':receipt}}, source_client=native)

    def test_archived_notice_real_durable_terminal_cannot_admit_later_original_result(self):
        item = self.seed()
        self.send(client='later-result-client', turn='later-result-turn')
        self.complete(client='later-result-client', turn='later-result-turn', sequence=2)
        self.service.coordinator_work.reconcile_once()
        native = self.resident(mutation_after_write=lambda:self.service.sessions.archive(self.source))
        self.assertEqual(self.tick(), 1)
        row = next(row for row in self.rows() if row['phase'] == 'accepted')
        self.assertEqual(self.service.sessions.get(self.source)['status'], 'archived')
        self.settle_notice(native, row)
        self.assertEqual(self.service.runtime._states[self.source].turn_id, '')
        self.assertEqual(self.service.sessions.get(self.source)['status'], 'archived')
        for _ in range(100):
            self.assertEqual(self.tick(), 0)
        recovered = next(record for record in self.rows() if record['delivery_id'] == row['delivery_id'])
        self.assertEqual(recovered['phase'], 'accepted')
        self.assertTrue(json.loads(recovered['terminal_refs_json']))
        self.assertTrue(any(record['phase'] == 'retired' for record in self.rows()))
        self.assertEqual(sum(method == 'session.prompt' for method, _ in native.calls), 1)
        self.assertIsNone(self.original_item(item)[2])
        self.assertTrue(any(event.event_type == 'turn_completed' and event.turn_id == row['source_turn_id']
            for event in self.service.events.replay(self.source)[0]))

    def test_archived_notice_exact_stop_terminal_preserves_archive_and_abort(self):
        item = self.seed()
        native = self.resident()
        self.tick()
        row = self.rows()[0]
        self.service.runtime.abort(self.source)
        self.service.sessions.archive(self.source)
        self.settle_notice(native, row, aborted=True)
        self.assertEqual(self.service.runtime._states[self.source].turn_id, '')
        self.assertEqual(self.service.sessions.get(self.source)['status'], 'archived')
        self.tick()
        self.assertEqual(self.rows()[0]['phase'], 'cancelled')
        aborts = [params for method, params in native.calls if method == 'session.abort']
        self.assertEqual(len(aborts), 1)
        self.assertEqual(aborts[0]['expectedTurnId'], row['source_turn_id'])
        self.assertTrue(any(event.event_type == 'turn_completed' and event.payload.get('aborted') is True
            for event in self.service.events.replay(self.source)[0]))
        self.assertIsNone(self.original_item(item)[2])

    def test_unarchived_notice_real_durable_terminal_still_projects_idle_without_ack(self):
        item = self.seed()
        native = self.resident()
        self.tick()
        row = self.rows()[0]
        self.settle_notice(native, row)
        self.assertEqual(self.service.sessions.get(self.source)['status'], 'idle')
        self.assertEqual(self.service.runtime._states[self.source].turn_id, '')
        self.assertTrue(any(event.event_type == 'turn_completed' and event.turn_id == row['source_turn_id']
            for event in self.service.events.replay(self.source)[0]))
        self.assertIsNone(self.original_item(item)[2])

    def test_archived_notice_failure_terminal_preserves_archive_and_failure_event(self):
        item = self.seed()
        native = self.resident(mutation_after_write=lambda:self.service.sessions.archive(self.source))
        self.tick()
        row = self.rows()[0]
        self.service.runtime._states[self.source].final_error = 'Original notice failure'
        self.settle_notice(native, row)
        self.assertEqual(self.service.sessions.get(self.source)['status'], 'archived')
        self.assertEqual(self.service.runtime._states[self.source].turn_id, '')
        self.assertTrue(any(event.event_type == 'turn_failed' and event.turn_id == row['source_turn_id']
            for event in self.service.events.replay(self.source)[0]))
        self.assertIsNone(self.original_item(item)[2])

    def test_archived_notice_valid_durable_observation_after_terminal_stays_archived(self):
        from tests.test_pi_durable_runtime import ENGINE_CAPABILITIES
        self.seed()
        native = self.resident(mutation_after_write=lambda:self.service.sessions.archive(self.source))
        self.tick()
        self.settle_notice(native, self.rows()[0])
        snapshot = {'schemaVersion':'rag-ime.pi-session-control-state.v1', 'sessionId':self.source,
            'runtimeEngine':'durable', 'piSessionId':'original-native-session',
            'durableStoreRef':str(self.root/'sessions'/'session.sqlite'), 'durableConversationId':'1',
            'projectionCurrent':True, 'paused':False, 'recoverable':False, 'isIdle':True,
            'activeTurn':None, 'messages':[], 'engineCapabilities':dict(ENGINE_CAPABILITIES)}
        self.service.runtime._validate_durable_state(self.source, snapshot, control=True)
        self.service.runtime._observe_durable_state(self.source, snapshot)
        self.assertEqual(self.service.sessions.get(self.source)['status'], 'archived')
        for _ in range(100):
            self.assertEqual(self.tick(), 0)
        self.assertEqual(sum(method == 'session.prompt' for method, _ in native.calls), 1)
