from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

from rag_ime.agent_service import AgentService
from rag_ime.agent_prompt_delivery import AgentPromptAcceptanceUnknown
from rag_ime.pi.config import PiRuntimeConfig
from tests.sqlite_fixtures import copy_current_database


class CoordinatorWorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='paw-passive-work-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'test.sqlite'
        copy_current_database(self.db)
        self.config = PiRuntimeConfig(enabled=False, executable=None, agent_dir=self.root / 'config',
            session_dir=self.root / 'sessions', logs_dir=self.root / 'logs')
        self.service = AgentService(db_path=self.db, runtime_config=self.config)
        self.addCleanup(self.service.close)
        with patch.object(self.service.runtime, 'require_session_engine'):
            self.source = self.service.ensure_coordinator({})['sourceSessionId']
        self.target = self.command('create_session', clientRequestId='original-worker', input={'task': 'Read original notes'})['target']['id']

    def command(self, action, **fields):
        return self.service.coordinator_command({'sourceSessionId': self.source, 'action': action, **fields})

    def tables(self):
        with sqlite3.connect(self.db) as conn:
            return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def accept(self, client='original-client', turn='original-turn'):
        receipts = self.service.command_receipts
        claim = receipts.begin(command_scope='session_prompt', scope_id=self.target,
            client_message_id=client, payload={'message': client})
        receipts.record_acceptance_evidence(claim, command_scope='session_prompt', scope_id=self.target,
            client_message_id=client, accepted={'turnId': turn, 'piEntryId': f'{turn}-user'})
        return {'accepted': True, 'turnId': turn, 'clientMessageId': client}

    def send(self, client='original-client', turn='original-turn', **extra):
        with patch.object(self.service, 'prompt', side_effect=lambda target, payload: self.accept(client, turn)) as prompt:
            result = self.command('prompt', targetId=self.target,
                input={'message': client, 'clientMessageId': client, **extra})
        prompt.assert_called_once()
        return result

    def complete(self, client='original-client', turn='original-turn', status='completed', sequence=1, text='Original answer'):
        self.service.sessions.record_runtime_event(event_id=f'event:{turn}', session_id=self.target,
            turn_id=turn, sequence=sequence, event_type='turn_completed', created_at_ms=1234, redacted_summary=status)
        entries = [{'type': 'session', 'id': 'physical'}]
        parent = 'physical'
        for original, cid, body in [(turn, client, text), ('new-turn', 'new-client', 'Do not substitute newer result')]:
            for entry in [{'type':'custom','id':f'{original}-binding','customType':'rag-ime.pi-turn-binding',
                'data':{'schemaVersion':'rag-ime.pi-turn-binding.v1','turnId':original,'clientMessageId':cid}},
                {'type':'message','id':f'{original}-user','message':{'role':'user','content':cid}},
                {'type':'message','id':f'{original}-assistant','message':{'role':'assistant','content':[{'type':'text','text':body}]}}]:
                entry['parentId'] = parent
                entries.append(entry)
                parent = entry['id']
        transcript = self.root / 'sessions' / 'worker.jsonl'
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(''.join(json.dumps(entry)+'\n' for entry in entries))
        self.service.sessions.bind_runtime_session(self.target, driver_id='managed-pi', runtime_kind='pi_rpc',
            external_session_id='physical', transcript_ref=str(transcript), branch_anchor=parent, binding_state='active', message_count=4)
        return transcript

    def attempts(self):
        return self.service.coordinator_work.attempts_for_source(self.source)

    def items(self):
        return [item for item in self.service.context_runtime.materialize(self.source)["items"]
                if item['sourceKind'] == 'coordinator_result']

    def test_original_coordinator_prompt_registers_before_dispatch(self):
        def dispatch(target, payload):
            self.assertIn('agent_coordinator_work_attempts', self.tables(), 'owned prompt must persist its original attempt before dispatch')
            with sqlite3.connect(self.db) as conn:
                row = conn.execute('SELECT target_session_id, client_message_id, turn_id FROM agent_coordinator_work_attempts').fetchone()
            self.assertEqual(row, (self.target, 'original-client', ''))
            return self.accept()
        with patch.object(self.service, 'prompt', side_effect=dispatch):
            self.command('prompt', targetId=self.target, input={'message':'original-client','clientMessageId':'original-client'})
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.attempts()[0]['turnId'], 'original-turn')

    def test_terminal_is_atomically_enqueued_as_original_evidence_not_goal_acceptance(self):
        self.send()
        path = self.complete(status='')
        before = path.read_bytes()
        self.assertEqual(self.service.coordinator_work.reconcile_once(limit=10), 1)
        item = self.items()[0]
        self.assertEqual(item['lifecycle'], 'until_ack')
        self.assertEqual(item['lane'], 'result')
        result = item['payload']['result']
        self.assertEqual(result['state'], 'unknown')
        self.assertTrue(result['evidenceOnly'])
        self.assertEqual(result['execution']['turnId'], 'original-turn')
        self.assertEqual(result['finalMessages'][0]['text'], 'Original answer')
        self.assertEqual(result['artifacts'], [])
        self.assertEqual(path.read_bytes(), before)
        self.assertNotEqual(self.service.sessions.agent_goal(self.target)['status'], 'completed')
        self.assertEqual(self.attempts()[0]['contextItemId'], item['itemId'])
        self.assertEqual(self.service.coordinator_work.reconcile_once(limit=10), 0)
        self.assertEqual(len(self.items()), 1)


    def test_lost_ack_recovers_original_acceptance_on_restart_without_redispatch(self):
        def unknown(target, payload):
            self.accept()
            raise AgentPromptAcceptanceUnknown('lost original ACK')
        with patch.object(self.service, 'prompt', side_effect=unknown) as prompt:
            with self.assertRaises(AgentPromptAcceptanceUnknown):
                self.command('prompt', targetId=self.target, input={'message':'original-client','clientMessageId':'original-client'})
        prompt.assert_called_once()
        self.assertEqual(self.attempts()[0]['turnId'], '')
        self.complete()
        self.service.close()
        self.service = AgentService(db_path=self.db, runtime_config=self.config)
        self.addCleanup(self.service.close)
        self.assertEqual(self.attempts()[0]['turnId'], 'original-turn')
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)

    def test_atomic_failure_after_enqueue_rolls_back_item_and_marker_then_recovers(self):
        self.send()
        self.complete()
        enqueue = self.service.context_runtime.enqueue
        def fail_after_write(**kwargs):
            enqueue(**kwargs)
            raise RuntimeError('injected crash before marker')
        with patch.object(self.service.context_runtime, 'enqueue', side_effect=fail_after_write):
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.attempts()[0]['contextItemId'], '')
        self.assertIsNone(self.attempts()[0]['projectedAtMs'])
        self.assertEqual(self.attempts()[0]['lastErrorCode'], 'RuntimeError')
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        self.assertEqual(len(self.items()), 1)

    def test_two_connection_concurrent_harvest_has_one_item_and_marker(self):
        self.send()
        self.complete()
        barrier = Barrier(6)
        reconcile = self.service.coordinator_work._reconcile
        def synchronize(row):
            barrier.wait(timeout=5)
            return reconcile(row)
        with patch.object(self.service.coordinator_work, '_reconcile', side_effect=synchronize):
            with ThreadPoolExecutor(max_workers=6) as pool:
                counts = list(pool.map(lambda _: self.service.coordinator_work.reconcile_once(), range(6)))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.attempts()[0]['contextItemId'], self.items()[0]['itemId'])

    def test_pending_first_attempt_does_not_starve_later_terminal_with_limit_one(self):
        self.send(client='pending-client', turn='pending-turn')
        self.send(client='original-client', turn='original-turn')
        self.complete()
        count = sum(self.service.coordinator_work.reconcile_once(limit=1) for _ in range(3))
        self.assertEqual(count, 1)
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.items()[0]['payload']['result']['execution']['turnId'], 'original-turn')

    def test_unknown_without_acceptance_never_queries_history_or_redispatches(self):
        with patch.object(self.service, 'prompt', side_effect=AgentPromptAcceptanceUnknown('lost ACK without evidence')):
            with self.assertRaises(AgentPromptAcceptanceUnknown):
                self.command('prompt', targetId=self.target, input={'message':'original-client','clientMessageId':'original-client'})
        with patch.object(self.service.runtime, 'messages', side_effect=AssertionError('no original acceptance')) as history, \
             patch.object(self.service.runtime, '_host', side_effect=AssertionError('no Host')) as host, \
             patch.object(self.service, 'prompt', side_effect=AssertionError('never redispatch')) as prompt:
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        history.assert_not_called(); host.assert_not_called(); prompt.assert_not_called()
        self.assertEqual(self.attempts()[0]['turnId'], '')
        self.assertEqual(self.items(), [])

    def test_original_client_turn_conflict_is_not_overwritten(self):
        self.send()
        with patch.object(self.service.command_receipts, 'acceptance_evidence_for_exact_command',
             return_value={'turnId':'foreign-turn','clientMessageId':'original-client'}):
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.attempts()[0]['turnId'], 'original-turn')
        self.assertEqual(self.attempts()[0]['lastErrorCode'], 'ValueError')
        self.assertEqual(self.items(), [])

    def test_missing_history_stays_pending_then_original_history_repairs(self):
        self.send()
        self.complete()
        with patch.object(self.service.runtime, 'messages', side_effect=OSError('original history unavailable')):
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        self.assertEqual(self.items()[0]['payload']['result']['finalMessages'][0]['text'], 'Original answer')

    def test_archived_target_retires_attempt_without_result_projection(self):
        self.send()
        self.complete()
        self.service.sessions.archive(self.target)
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.attempts()[0]['retiredReason'], 'ownership_retired')
        self.assertEqual(self.items(), [])

    def test_archived_source_identity_is_never_rerouted_to_replacement(self):
        self.send()
        self.complete()
        old_source = self.source
        self.service.sessions.archive(old_source)
        with patch.object(self.service.runtime, 'require_session_engine'):
            new_source = self.service.ensure_coordinator({})['sourceSessionId']
        self.assertNotEqual(old_source, new_source)
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.attempts()[0]['retiredReason'], 'ownership_retired')
        self.assertFalse(self.service.context_runtime.materialize(new_source)['items'])

    def test_archival_during_history_read_is_rechecked_before_atomic_enqueue(self):
        self.send()
        self.complete()
        read = self.service.coordinator_work.read_result
        def archive_during_read(*args):
            result = read(*args)
            self.service.sessions.archive(self.source)
            return result
        with patch.object(self.service.coordinator_work, 'read_result', side_effect=archive_during_read):
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.attempts()[0]['retiredReason'], 'ownership_retired')

    def test_same_id_replay_is_one_attempt_and_changed_input_is_rejected_before_dispatch(self):
        self.send()
        with patch.object(self.service, 'prompt', return_value={'turnId':'original-turn'}) as prompt:
            self.command('prompt', targetId=self.target, input={'message':'original-client','clientMessageId':'original-client'})
            self.assertEqual(len(self.attempts()), 1)
            with self.assertRaisesRegex(ValueError, 'different coordinator attempt'):
                self.command('prompt', targetId=self.target, input={'message':'Changed','clientMessageId':'original-client'})
        self.assertEqual(prompt.call_count, 1)

    def test_explicit_retry_is_a_distinct_attempt_for_original_work_and_nonowned_is_rejected(self):
        self.send(client='first-client', turn='first-turn')
        self.send(client='retry-client', turn='retry-turn', retryOfClientMessageId='first-client')
        rows = self.attempts()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row['workId'] for row in rows}), 1)
        self.assertEqual(len({row['attemptId'] for row in rows}), 2)
        other = self.service.create_session({})['session']['id']
        with self.assertRaisesRegex(ValueError, 'not controlled'):
            self.service.coordinator_work.register_prompt(self.source, other, {'message':'bad','clientMessageId':'bad'})
        self.assertEqual(len(self.attempts()), 2)

    def test_one_hundred_unchanged_maintenance_ticks_have_no_prompt_host_or_history(self):
        self.send()
        self.complete()
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        with patch.object(self.service.runtime, 'messages', side_effect=AssertionError('no history after harvest')) as history, \
             patch.object(self.service.runtime, '_host', side_effect=AssertionError('no Host')) as host, \
             patch.object(self.service, 'prompt', side_effect=AssertionError('no automatic Source input')) as prompt, \
             patch.object(self.service.runtime, 'resume_session', side_effect=AssertionError('no resume')) as resume:
            for _ in range(100):
                self.assertEqual(self.service._run_scheduled_work_once(), 0)
        for probe in (history, host, prompt, resume):
            probe.assert_not_called()
        self.assertEqual(len(self.items()), 1)

    def test_limit_validation_and_followup_do_not_claim_new_original_work(self):
        for value in [0, 101, True, '10']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.service.coordinator_work.reconcile_once(limit=value)
        with patch.object(self.service, 'prompt', return_value={'accepted':True}):
            self.command('prompt', targetId=self.target, input={'message':'follow','clientMessageId':'follow','delivery':'follow_up'})
        self.assertEqual(self.attempts(), [])


    def test_owned_room_prompt_does_not_register_session_work(self):
        room = self.command('create_room', clientRequestId='owned-room', input={'task':'Discuss original notes',
            'participants':[{'roleId':'companion-present-v1','collaborationRole':'coordinator'},
                            {'roleId':'companion-future-v1','collaborationRole':'reviewer'}]})['target']['id']
        with patch.object(self.service, 'post_room_message', return_value={'accepted':True}) as post:
            self.command('prompt', targetId=room, input={'message':'Discuss','clientMessageId':'room-client'})
        post.assert_called_once()
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.attempts()[0]['targetRoomId'], room)
        self.assertNotIn('targetSessionId', self.attempts()[0])
        with self.service.sessions._read_connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_coordinator_work_attempts').fetchone()[0], 0)

    def test_new_target_user_turn_terminal_does_not_replace_original_attempt(self):
        self.send()
        self.complete()
        self.accept('new-client', 'new-turn')
        self.service.sessions.record_runtime_event(event_id='event:new-turn', session_id=self.target, turn_id='new-turn',
            sequence=2, event_type='turn_completed', created_at_ms=4567, redacted_summary='completed')
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        self.assertEqual(self.items()[0]['payload']['result']['finalMessages'][0]['text'], 'Original answer')
        self.assertEqual(self.items()[0]['payload']['result']['terminalRefs'][0]['eventId'], 'event:original-turn')
        self.assertEqual(len(self.attempts()), 1)

    def test_one_hundred_pending_checks_do_not_open_history_host_or_source(self):
        self.send()
        with patch.object(self.service.runtime, 'messages', side_effect=AssertionError('no terminal yet')) as history, \
             patch.object(self.service.runtime, '_host', side_effect=AssertionError('no Host')) as host, \
             patch.object(self.service, 'prompt', side_effect=AssertionError('no Source model')) as prompt:
            for _ in range(100):
                self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        for probe in (history, host, prompt):
            probe.assert_not_called()
        self.assertEqual(self.items(), [])

    def test_stopped_source_fence_prevents_attempt_registration_and_dispatch(self):
        from contextlib import contextmanager
        @contextmanager
        def stopped(*args, **kwargs):
            raise ValueError('original Source stopped')
            yield
        with patch.object(self.service.runtime, 'gateway_turn_effect_fence', stopped), \
             patch.object(self.service.runtime, 'is_gateway_turn_active', return_value=True), \
             patch.object(self.service, 'prompt', side_effect=AssertionError('must not dispatch')) as prompt:
            with self.assertRaisesRegex(ValueError, 'Source stopped'):
                self.service.coordinator_command({'sourceSessionId':self.source,'action':'prompt','targetId':self.target,
                    'input':{'message':'original-client','clientMessageId':'original-client'}},
                    execution_binding={'turnId':'source-turn','clientMessageId':'source-client'})
        prompt.assert_not_called()
        self.assertEqual(self.attempts(), [])


    def test_whitespace_client_identity_matches_original_prompt_normalization(self):
        with patch.object(self.service, 'prompt', side_effect=lambda target, request: self.accept()):
            self.command('prompt', targetId=self.target, input={'message':'original-client','clientMessageId':' original-client '})
        self.assertEqual(self.attempts()[0]['clientMessageId'], 'original-client')
        self.assertEqual(self.attempts()[0]['turnId'], 'original-turn')



    def test_terminal_with_missing_classic_history_never_starts_host(self):
        self.send()
        transcript = self.complete()
        transcript.unlink()
        with patch.object(self.service.runtime, '_host', side_effect=AssertionError('background Host start')) as host, \
             patch.object(self.service.runtime, 'ensure', side_effect=AssertionError('background Session open')) as ensure:
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        host.assert_not_called()
        ensure.assert_not_called()
        self.assertEqual(self.items(), [])
        self.assertIsNone(self.attempts()[0]['projectedAtMs'])

    def test_terminal_with_unreadable_classic_history_never_starts_host(self):
        self.send()
        transcript = self.complete()
        original_open = Path.open
        def unreadable(path, *args, **kwargs):
            if path.resolve(strict=False) == transcript.resolve(strict=False):
                raise PermissionError('native history cannot be read')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', unreadable), \
             patch.object(self.service.runtime, '_host', side_effect=AssertionError('background Host start')) as host, \
             patch.object(self.service.runtime, 'ensure', side_effect=AssertionError('background Session open')) as ensure:
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        host.assert_not_called()
        ensure.assert_not_called()
        self.assertEqual(self.items(), [])



    def test_explicit_user_open_then_existing_native_snapshot_allows_pending_harvest(self):
        self.send()
        transcript = self.complete()
        native = self.service.runtime._durable_history_snapshot(self.target)
        self.assertIsNotNone(native)
        transcript.unlink()
        with patch.object(self.service.runtime, '_host', side_effect=AssertionError('background Host start')) as no_host:
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        no_host.assert_not_called()
        client = Mock(running=True)
        client.send.side_effect = lambda method, params=None, **kwargs: {'snapshot':native} if method == 'session.open' else native
        self.service.runtime.config = replace(self.service.runtime.config, provider='test', model='offline-model', idle_timeout_seconds=0)
        self.service.runtime._client = client
        self.service.runtime._host_capabilities = {"sessionSkillAllowlist": True, "sessionPromptSettings": True}
        # This is an explicit user opening, through the real Runtime owner.
        # Only its native wire is doubled; no model or replacement input exists.
        with patch.object(self.service.runtime, '_host', return_value=client):
            self.service.session_application.ensure_runtime({'sessionId':self.target})
        self.assertIn(self.target, self.service.runtime._open_sessions)
        client.send.reset_mock()
        with patch.object(self.service.runtime, '_host', side_effect=AssertionError('never restart resident Host')) as host, \
             patch.object(self.service.runtime, 'ensure', side_effect=AssertionError('never reopen resident Session')) as ensure:
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        host.assert_not_called(); ensure.assert_not_called()
        self.assertEqual([call.args[0] for call in client.send.call_args_list], ['session.snapshot'])
        self.assertEqual(self.items()[0]['payload']['result']['finalMessages'][0]['text'], 'Original answer')

    def test_explicit_read_result_retains_ordinary_inspection_behavior(self):
        self.send()
        transcript = self.complete()
        transcript.unlink()
        with patch.object(self.service.runtime, '_host', side_effect=AssertionError('explicit inspection may reach Host')) as host:
            with self.assertRaisesRegex(AssertionError, 'explicit inspection'):
                self.command('read_result', targetId=self.target,
                    input={'turnId':'original-turn','clientMessageId':'original-client'})
        host.assert_called_once()

    def test_cold_classic_original_log_harvests_without_any_host_or_ensure(self):
        self.send()
        self.complete()
        with patch.object(self.service.runtime, '_host', side_effect=AssertionError('no cold Host')) as host, \
             patch.object(self.service.runtime, 'ensure', side_effect=AssertionError('no cold Session open')) as ensure:
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        host.assert_not_called(); ensure.assert_not_called()
        self.assertEqual(self.items()[0]['payload']['result']['finalMessages'][0]['text'], 'Original answer')



    def churn_events(self, count=150, retain=100):
        start = self.service.sessions.max_event_sequence(self.target)
        for sequence in range(start+1, start+count+1):
            self.service.sessions.record_runtime_event(event_id=f'later:{sequence}', session_id=self.target,
                turn_id='unrelated-later-turn', sequence=sequence, event_type='status_changed', created_at_ms=2000+sequence,
                retain_per_session=retain)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM agent_runtime_events WHERE session_id=? AND turn_id='original-turn' AND event_type='turn_completed'",
                                          (self.target,)).fetchone()[0], 0, 'test must actually prune the original terminal')

    def test_backlog_pruned_terminal_retains_original_reference_and_harvests_once(self):
        self.send()
        self.complete(status='')
        self.churn_events()
        terminal = self.service.sessions.runtime_turn_terminal_event(self.target, 'original-turn')
        self.assertIsNotNone(terminal, 'old exact terminal must outlive the recent event window')
        self.assertEqual(terminal['eventId'], 'event:original-turn')
        self.assertEqual(terminal['status'], '')
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        self.assertEqual(self.items()[0]['payload']['result']['state'], 'unknown')
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(len(self.items()), 1)

    def test_default_thousand_event_window_does_not_drop_original_terminal(self):
        self.send()
        self.complete()
        self.churn_events(count=1100, retain=1000)
        self.assertIsNotNone(self.service.sessions.runtime_turn_terminal_event(self.target, 'original-turn'))
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)

    def test_lost_ack_backlog_cold_start_uses_original_acceptance_not_new_turn(self):
        def lose_ack(target, request):
            self.accept()
            raise AgentPromptAcceptanceUnknown('original ACK lost')
        with patch.object(self.service, 'prompt', side_effect=lose_ack):
            with self.assertRaises(AgentPromptAcceptanceUnknown):
                self.command('prompt', targetId=self.target, input={'message':'original-client','clientMessageId':'original-client'})
        self.assertEqual(self.attempts()[0]['turnId'], '')
        self.complete()
        self.churn_events()
        self.service.close()
        self.service = AgentService(db_path=self.db, runtime_config=self.config)
        self.addCleanup(self.service.close)
        self.assertEqual(self.attempts()[0]['turnId'], 'original-turn')
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.items()[0]['payload']['result']['execution']['clientMessageId'], 'original-client')
        self.assertEqual(self.items()[0]['payload']['result']['terminalRefs'][0]['eventId'], 'event:original-turn')



    def test_backlog_with_only_original_user_acceptance_survives_both_windows(self):
        def accepted_user_without_ack(target, request):
            self.service.sessions.record_runtime_event(event_id='original-user-acceptance', session_id=self.target,
                turn_id='original-turn', sequence=1, event_type='message_completed', created_at_ms=1111,
                metrics={'promptAcceptance':{'clientMessageId':'original-client','turnId':'original-turn','messageId':'original-turn-user'}})
            raise AgentPromptAcceptanceUnknown('no command ACK proof')
        with patch.object(self.service, 'prompt', side_effect=accepted_user_without_ack):
            with self.assertRaises(AgentPromptAcceptanceUnknown):
                self.command('prompt', targetId=self.target, input={'message':'original-client','clientMessageId':'original-client'})
        self.complete(sequence=2)
        self.churn_events()
        self.assertEqual(self.service.sessions.prompt_acceptance_evidence(self.target, 'original-client')['eventId'], 'original-user-acceptance')
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)
        self.assertEqual(self.items()[0]['payload']['result']['terminalRefs'][0]['eventId'], 'event:original-turn')

    def test_unaccepted_attempt_does_not_claim_unrelated_terminal_after_pruning(self):
        with patch.object(self.service, 'prompt', side_effect=AgentPromptAcceptanceUnknown('no acceptance')):
            with self.assertRaises(AgentPromptAcceptanceUnknown):
                self.command('prompt', targetId=self.target, input={'message':'original-client','clientMessageId':'original-client'})
        self.complete()
        self.churn_events()
        self.assertIsNone(self.service.sessions.runtime_turn_terminal_event(self.target, 'original-turn'))
        self.assertEqual(self.attempts()[0]['turnId'], '')
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.items(), [])

    def test_untracked_session_still_prunes_ordinary_runtime_events(self):
        self.complete()
        self.churn_events()
        self.assertIsNone(self.service.sessions.runtime_turn_terminal_event(self.target, 'original-turn'))
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.items(), [])

    def test_same_turn_without_original_client_acceptance_cannot_claim_reference(self):
        self.send()
        self.complete()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE agent_coordinator_work_attempts SET client_message_id='unaccepted-client', acceptance_json='{}'")
        self.churn_events()
        self.assertIsNone(self.service.sessions.runtime_turn_terminal_event(self.target, 'original-turn'))
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.items(), [])

    def test_pruned_reference_foreign_session_turn_or_untyped_status_is_rejected(self):
        self.send()
        self.complete()
        self.churn_events()
        with sqlite3.connect(self.db) as conn:
            original = conn.execute('SELECT terminal_refs_json FROM agent_coordinator_work_attempts').fetchone()[0]
        for field, value in [('sessionId','foreign-session'),('turnId','foreign-turn'),('sequence',True),('status',None),('eventType',[])]:
            ref = json.loads(original)[0]; ref[field] = value
            with sqlite3.connect(self.db) as conn:
                conn.execute('UPDATE agent_coordinator_work_attempts SET terminal_refs_json=?', (json.dumps([ref]),))
            with self.subTest(field=field):
                self.assertIsNone(self.service.sessions.runtime_turn_terminal_event(self.target, 'original-turn'))
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE agent_coordinator_work_attempts SET terminal_refs_json=?', (original,))
        self.assertIsNotNone(self.service.sessions.runtime_turn_terminal_event(self.target, 'original-turn'))
        other = self.service.create_session({})['session']['id']
        self.assertIsNone(self.service.sessions.runtime_turn_terminal_event(other, 'original-turn'))

    def test_backlog_missing_log_stays_pending_without_host_then_original_log_repairs(self):
        self.send()
        path = self.complete()
        data = path.read_bytes()
        self.churn_events()
        path.unlink()
        with patch.object(self.service.runtime, '_host', side_effect=AssertionError('no backlog Host')) as host, \
             patch.object(self.service.runtime, 'ensure', side_effect=AssertionError('no backlog Session open')) as ensure:
            self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        host.assert_not_called(); ensure.assert_not_called()
        self.assertEqual(self.items(), [])
        path.write_bytes(data)
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)

    def test_prune_capture_failure_rolls_back_reference_and_original_event_deletion(self):
        self.send()
        self.complete()
        retain = self.service.sessions._retain_coordinator_terminal
        def fail_after_capture(*args):
            retain(*args)
            raise RuntimeError('capture crash before pruning')
        for sequence in range(2, 101):
            self.service.sessions.record_runtime_event(event_id=f'pre:{sequence}', session_id=self.target,
                turn_id='later-turn', sequence=sequence, event_type='status_changed', created_at_ms=1000+sequence, retain_per_session=100)
        with patch.object(self.service.sessions, '_retain_coordinator_terminal', side_effect=fail_after_capture):
            with self.assertRaisesRegex(RuntimeError, 'capture crash'):
                self.service.sessions.record_runtime_event(event_id='cross-window', session_id=self.target,
                    turn_id='later-turn', sequence=101, event_type='status_changed', created_at_ms=2000, retain_per_session=100)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT terminal_refs_json FROM agent_coordinator_work_attempts').fetchone()[0], '[]')
            self.assertEqual(conn.execute("SELECT count(*) FROM agent_runtime_events WHERE event_id='event:original-turn'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM agent_runtime_events WHERE event_id='cross-window'").fetchone()[0], 0)
        self.churn_events()
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 1)

    def test_concurrent_pruned_terminal_harvest_has_one_context_and_original_reference(self):
        self.send()
        self.complete()
        self.churn_events()
        with ThreadPoolExecutor(max_workers=4) as pool:
            counts = list(pool.map(lambda _: self.service.coordinator_work.reconcile_once(), range(4)))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.items()[0]['payload']['result']['terminalRefs'][0]['eventId'], 'event:original-turn')

    def test_retired_creation_relation_cannot_transfer_pruned_reference_to_new_source(self):
        self.send()
        self.complete()
        self.churn_events()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE agent_coordinator_objects SET coordinator_id='foreign-creator'")
        self.assertIsNone(self.service.sessions.runtime_turn_terminal_event(self.target, 'original-turn'))
        self.assertEqual(self.service.coordinator_work.reconcile_once(), 0)
        self.assertEqual(self.items(), [])



if __name__ == '__main__':
    unittest.main()
