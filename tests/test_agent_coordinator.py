from __future__ import annotations
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from rag_ime.agent_service import AgentService
from rag_ime.pi.config import PiRuntimeConfig
from tests.sqlite_fixtures import copy_current_database

class AgentCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='paw-coordinator-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        db = root / 'agent.sqlite'
        copy_current_database(db)
        self.service = AgentService(db_path=db, runtime_config=PiRuntimeConfig(enabled=False, executable=None,
            agent_dir=root/'config', session_dir=root/'sessions', logs_dir=root/'logs'))
        self.addCleanup(self.service.close)
        engine = patch.object(self.service.runtime, 'require_session_engine')
        engine.start()
        self.addCleanup(engine.stop)
        self.source = self.service.ensure_coordinator({})['session']

    def command(self, action, **kwargs):
        return self.service.coordinator_command({'sourceSessionId': self.source['id'], 'action': action, **kwargs})

    def test_global_persistence_does_not_upgrade_daily_primary(self):
        daily = self.service.ensure_primary_assistant({})['session']
        self.assertNotEqual(daily['id'], self.source['id'])
        self.assertEqual(daily['executionMode'], 'read_only')
        self.assertEqual(self.source['executionMode'], 'full_trust')
        self.assertEqual(self.source['runtimeEngine'], 'durable')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.service.ensure_coordinator({}), range(8)))
        self.assertEqual({x['session']['id'] for x in results}, {self.source['id']})
        self.assertTrue(all(not x['created'] for x in results))
        self.assertEqual(self.command('read')['objects'], [])
        with self.assertRaises(ValueError):
            self.service.ensure_coordinator({'workspaceRoots': ['/somewhere']})

    def test_creation_is_passive_idempotent_and_owned(self):
        with patch.object(self.service, 'prompt', side_effect=AssertionError('no auto prompt')):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: self.command('create_session', clientRequestId='create-1',
                    input={'task': 'Review the supplied notes'}), range(8)))
        self.assertEqual(sum(x['created'] for x in results), 1)
        self.assertEqual(len({x['target']['id'] for x in results}), 1)
        target = results[0]['target']
        self.assertEqual(target['executionMode'], 'full_trust')
        self.assertEqual(target['messageCount'], 0)
        objects = self.command('read')['objects']
        self.assertEqual(objects[0]['sourceSessionId'], self.source['id'])
        self.assertEqual(objects[0]['id'], target['id'])
        with self.assertRaisesRegex(ValueError, 'different'):
            self.command('create_session', clientRequestId='create-1', input={'task':'Changed task'})
        self.service.sessions.delete(target['id'])
        with self.assertRaisesRegex(ValueError, 'deleted'):
            self.command('create_session', clientRequestId='create-1', input={'task':'Review the supplied notes'})

    def test_room_creation_is_passive_idempotent_and_scoped(self):
        input = {'task':'Inspect notes together', 'participants':[
            {'roleId':'companion-present-v1','collaborationRole':'coordinator'},
            {'roleId':'companion-future-v1','collaborationRole':'reviewer'}]}
        with patch.object(self.service, 'post_room_message', side_effect=AssertionError('no auto dispatch')):
            first = self.command('create_room', clientRequestId='room-1', input=input)
            again = self.command('create_room', clientRequestId='room-1', input=input)
        self.assertTrue(first['created'])
        self.assertFalse(again['created'])
        self.assertEqual(first['target']['id'], again['target']['id'])
        self.assertEqual(len(first['target']['participants']), 2)
        owned = self.command('read')['objects']
        self.assertEqual([(o['kind'], o['id']) for o in owned], [('room', first['target']['id'])])
        other = self.service.create_session({'title':'Unrelated'})['session']
        with patch.object(self.service, 'prompt', side_effect=AssertionError('cross owner never calls owner')):
            with self.assertRaisesRegex(ValueError, 'not controlled'):
                self.command('prompt', targetId=other['id'], input={'message':'Do this'})

    def test_control_passes_exact_identity_to_existing_owner(self):
        target = self.command('create_session', clientRequestId='control-1', input={'task':'Check notes'})['target']
        with patch.object(self.service, 'abort', return_value={'ok':True, 'runtimeReceipt':{'turnId':'turn-1'}}) as abort:
            result = self.command('stop', targetId=target['id'], input={'turnId':'turn-1','clientMessageId':'message-1'})
            abort.assert_called_once_with(target['id'], {'turnTarget':{'turnId':'turn-1','clientMessageId':'message-1'}})
            self.assertEqual(result['runtimeReceipt']['turnId'], 'turn-1')
        with self.assertRaisesRegex(ValueError, 'exact'):
            self.command('stop', targetId=target['id'], input={'turnId':'turn-1'})
        with self.assertRaisesRegex(ValueError, 'active persistent'):
            self.service.coordinator_command({'sourceSessionId':target['id'], 'action':'read'})

    def test_stale_turn_stop_never_cancels_workspace_commands_or_approvals(self):
        target = self.command('create_session', clientRequestId='stale-1', input={'task':'Check exact Stop'})['target']
        from rag_ime.pi.runtime import PiRuntimeTurnConflict
        with patch.object(self.service.runtime, 'abort_with_approval_fence', side_effect=PiRuntimeTurnConflict('Stop target changed')) as fence:
            cancellation = lambda _: self.fail('stale target cannot cancel commands')
            self.service.bind_workspace_command_cancellation(cancellation)
            with patch.object(self.service.sessions, 'cancel_pending_approvals', side_effect=AssertionError('stale target cannot cancel approvals')):
                with self.assertRaises(PiRuntimeTurnConflict):
                    self.command('stop', targetId=target['id'], input={'turnId':'old','clientMessageId':'old-client'})
            self.assertEqual(fence.call_args.kwargs['expected_identity'], {'turnId':'old','clientMessageId':'old-client'})

    def test_tool_schema_exposes_creation_only_to_actual_global_coordinator(self):
        from rag_ime.agent_tools import ControlToolGateway
        gateway = ControlToolGateway(sessions=self.service.sessions, management=object(), core=object(), project='test', collaboration=self.service)
        native = next(item for item in gateway.runtime_manifests(self.source) if item['name']=='agents')
        self.assertIn('coordinator', native['parameters']['properties']['op']['enum'])
        self.assertIn('input', native['parameters']['properties'])
        daily = self.service.ensure_primary_assistant({})['session']
        old = next(item for item in gateway.runtime_manifests(daily) if item['name']=='agents')
        self.assertNotIn('coordinator', old['parameters']['properties']['op']['enum'])
        with patch.object(self.service, 'coordinator_command', return_value={'ok':True}) as command:
            gateway._agents('coordinator', {'_sessionId':self.source['id'],'action':'read','sourceSessionId':'smuggled'})
            command.assert_called_once_with({'sourceSessionId':self.source['id'],'action':'read'})

    def test_routes_keep_new_global_authority_local_only(self):
        from rag_ime.control_api import ControlAccessContext, ControlApiError, ControlRequest, default_route_policy
        policy = default_route_policy()
        for path_id, body in [('agent.coordinator.ensure',{}),('agent.coordinator.command',{'sourceSessionId':self.source['id'],'action':'read'})]:
            request = ControlRequest(request_id='test',path_id=path_id,body=body)
            policy.authorize(request,ControlAccessContext.native())
            with self.assertRaises(ControlApiError):
                policy.authorize(request,ControlAccessContext.remote(device_id='remote',scopes={'*'}))

    def test_deleted_coordinator_rotates_identity_without_adopting_old_targets(self):
        old = self.command('create_session', clientRequestId='old-owner', input={'task':'Old task'})['target']
        self.service.sessions.delete(self.source['id'])
        replacement = self.service.ensure_coordinator({})
        self.assertTrue(replacement['created'])
        self.assertNotEqual(replacement['session']['id'],self.source['id'])
        self.assertEqual(replacement['objects'],[])
        with self.assertRaisesRegex(ValueError,'not controlled'):
            self.service.coordinator_command({'sourceSessionId':replacement['session']['id'],'action':'stop','targetId':old['id'],'input':{'turnId':'turn','clientMessageId':'client'}})

    def test_permission_downgrade_never_creates_full_trust_children(self):
        self.service.sessions.set_runtime_policy(self.source['id'],mode='assistant',tool_profile_version='subagent-readonly-v1',execution_mode='read_only',allowed_tools=None)
        with self.assertRaisesRegex(ValueError,'permissions changed'):
            self.command('create_session',clientRequestId='downgraded',input={'task':'No escalation'})
        self.assertEqual(self.command('read')['objects'],[])

    def test_session_and_room_partners_inherit_exact_creator_model_selection(self):
        self.service.sessions.set_model_profile(self.source['id'],'openai-codex/gpt-6-sol')
        self.service.sessions.set_thinking_level(self.source['id'],'xhigh')
        task = self.command('create_session',clientRequestId='selection-session',input={'task':'Use exact creator selection'})['target']
        room = self.command('create_room',clientRequestId='selection-room',input={'task':'Use exact creator selection','participants':[
            {'roleId':'companion-present-v1','collaborationRole':'coordinator'},
            {'roleId':'companion-future-v1','collaborationRole':'reviewer'}]})['target']
        targets = [task,*[self.service.sessions.get(p['sessionId']) for p in room['participants']]]
        self.assertEqual([(t['modelProfile'],t['thinkingLevel']) for t in targets],[('openai-codex/gpt-6-sol','xhigh')]*3)

    def test_room_ownership_survives_interrupted_post_commit_response(self):
        request = {'task':'Only create once','participants':[
            {'roleId':'companion-present-v1','collaborationRole':'coordinator'},
            {'roleId':'companion-future-v1','collaborationRole':'reviewer'}]}
        with patch.object(self.service.room_management.lifecycle.events,'publish',side_effect=RuntimeError('response interrupted')):
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                self.command('create_room',clientRequestId='interrupted-room',input=request)
        objects = self.command('read')['objects']
        self.assertEqual(len(objects),1)
        replay = self.command('create_room',clientRequestId='interrupted-room',input=request)
        self.assertFalse(replay['created'])
        self.assertEqual(replay['target']['id'],objects[0]['id'])

    def test_gateway_envelope_accepts_coordinator_operation_after_real_passive_creation(self):
        from rag_ime.agent_tools import ControlToolGateway
        gateway = ControlToolGateway(sessions=self.service.sessions, management=object(), core=object(), project='test', collaboration=self.service)
        request = {'schemaVersion':'rag-ime.agent-tool-call.v1','sessionId':self.source['id'],
            'toolCallId':'actual-envelope','tool':'agents','args':{'op':'coordinator','action':'create_session',
            'clientRequestId':'actual-envelope-create','input':{'task':'Passive envelope regression'}}}
        first = gateway.execute(request)
        self.assertEqual(first['operation'],'coordinator')
        self.assertTrue(first['result']['created'])
        self.assertEqual(gateway.execute(request),first)
        self.assertEqual(len(self.command('read')['objects']),1)

    def test_stopped_native_creation_after_lock_wait_cannot_create_session_or_room(self):
        import threading
        from contextlib import contextmanager
        from rag_ime.agent_coordinator import _LOCKS, _LOCKS_GUARD
        from rag_ime.pi.runtime import PiRuntimeTurnConflict
        database = str(self.service.sessions.db_path.resolve())
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(database,threading.RLock())
        alive = threading.Event(); alive.set()
        @contextmanager
        def fence(source, binding):
            if not alive.is_set():
                raise PiRuntimeTurnConflict('source turn stopped')
            yield
        for action in ('create_session','create_room'):
            alive.set()
            input = {'task':'Must not appear after Stop'}
            if action == 'create_room':
                input['participants'] = [{'roleId':'companion-present-v1','collaborationRole':'coordinator'},
                    {'roleId':'companion-future-v1','collaborationRole':'reviewer'}]
            with lock, patch.object(self.service.runtime,'gateway_turn_effect_fence',side_effect=fence):
                pool = ThreadPoolExecutor(max_workers=1)
                started = threading.Event()
                def create(started=started, action=action, payload=input):
                    started.set()
                    return self.service.coordinator_command({'sourceSessionId':self.source['id'], 'action':action,
                        'clientRequestId':action+'-stopped', 'input':payload},execution_binding={'turnId':'source-turn','clientMessageId':'source-client'})
                future = pool.submit(create)
                self.assertTrue(started.wait(2))
                alive.clear()
            with self.assertRaises(PiRuntimeTurnConflict):
                future.result(timeout=5)
            pool.shutdown()
            self.assertEqual(self.command('read')['objects'],[])
        passive = self.command('create_session',clientRequestId='ui-authorized',input={'task':'Passive UI creation still works'})
        self.assertTrue(passive['created'])

    def test_native_agents_forwards_original_execution_binding(self):
        from rag_ime.agent_tools import ControlToolGateway
        gateway = ControlToolGateway(sessions=self.service.sessions, management=object(), core=object(), project='test', collaboration=self.service)
        binding = {'turnId':'source-turn','clientMessageId':'source-client'}
        with patch.object(self.service,'coordinator_command',return_value={'ok':True}) as command:
            gateway._agents('coordinator',{'_sessionId':self.source['id'],'action':'read','_executionBinding':binding})
            command.assert_called_once_with({'sourceSessionId':self.source['id'],'action':'read'},execution_binding=binding)

    def test_room_dispatch_rechecks_source_after_routing_before_root_or_goal_resume(self):
        from contextlib import contextmanager
        from rag_ime.pi.runtime import PiRuntimeTurnConflict
        room = self.command('create_room',clientRequestId='routing-fence',input={'task':'Route only under exact source','participants':[
            {'roleId':'companion-present-v1','collaborationRole':'coordinator'},
            {'roleId':'companion-future-v1','collaborationRole':'reviewer'}]})['target']
        active = [True]
        @contextmanager
        def fence(*args, **kwargs):
            if not active[0]:
                raise PiRuntimeTurnConflict('controller stopped during routing')
            yield
        def route(rooms, room, message, decisions):
            active[0] = False
            return decisions
        with patch.object(self.service.runtime,'is_gateway_turn_active',return_value=True), \
             patch.object(self.service.runtime,'gateway_turn_effect_fence',side_effect=fence), \
             patch('rag_ime.rooms.session_dispatch.route_with_jev',side_effect=route), \
             patch.object(self.service.room_dispatch,'_resume_room_goal_if_paused',side_effect=AssertionError('late source must not resume goal')), \
             patch.object(self.service.room_dispatch,'prompt',side_effect=AssertionError('no late participant dispatch')):
            with self.assertRaises(PiRuntimeTurnConflict):
                self.service.coordinator_command({'sourceSessionId':self.source['id'],'action':'prompt','targetId':room['id'],
                    'input':{'message':'Only admit while source current','clientMessageId':'routing-client'}},
                    execution_binding={'turnId':'controller-turn','clientMessageId':'controller-client'})
        events = self.service.rooms.list_events(room['id'])
        self.assertFalse(any(event.get('type',event.get('eventType'))=='user_message' for event in events))
