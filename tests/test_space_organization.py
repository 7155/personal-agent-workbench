import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.rooms.store import AgentRoomStore
from rag_ime.space_organization import CATEGORIES, OrganizationConflict, SpaceOrganization
from rag_ime.control_api.dispatch import DescriptorRouteDispatcher
from rag_ime.control_api.route_table import find_route
from types import SimpleNamespace


class SpaceOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'main.sqlite'
        self.sessions = AgentSessionStore(self.path)
        self.sessions.initialize()
        self.rooms = AgentRoomStore(self.path)
        self.rooms.initialize()
        self.session = self.sessions.create(title='等数据再继续 RAG 实验')
        self.key = 'session:' + self.session['id']
        self.now = 1000
        self.service = SpaceOrganization(self.path, sessions=self.sessions, rooms=self.rooms, clock=lambda: self.now)

    def read(self):
        return self.service.read({'keys': [self.key]})

    def command(self, operation='placement', value='shelf', revision=0, identity='c1'):
        return self.service.command({'spaceKey': self.key, 'commandId': identity,
            'expectedRevision': revision, 'operation': operation, 'value': value})

    def answer(self, choice='waiting', confidence=.95):
        return {'model': 'jev-test', 'answers': {'category': {'type': 'choice', 'choice': choice,
            'confidence': confidence, 'probabilities': {key: float(key == choice) for key in CATEGORIES}}}}

    def suggest(self, response=None):
        with patch('rag_ime.jev.api_key', return_value='test-only'), patch('rag_ime.jev.evaluate', return_value=response or self.answer()) as call:
            result = self.service.suggest({'spaceKey': self.key})
        self.assertEqual(json.loads(call.call_args.args[0]), {'title': self.session['title']})
        return result

    def test_real_store_roundtrip_restart_undo_preserves_canonical_session(self):
        before = self.sessions.get(self.session['id'])
        self.command()
        reopened = SpaceOrganization(self.path, sessions=self.sessions, rooms=self.rooms)
        saved = reopened.read({'keys': [self.key]})
        self.assertEqual(saved['items'][0]['placement'], 'shelf')
        self.assertEqual(saved['receipts'][0]['id'], 'c1')
        reopened.undo({'receiptId': 'c1'})
        self.assertEqual(self.read()['items'][0]['placement'], 'desk')
        self.assertEqual(self.sessions.get(self.session['id']), before)
        self.assertTrue(reopened.undo({'receiptId': 'c1'})['replayed'])

    def test_suggestion_apply_durable_undo(self):
        proposal = self.suggest()['proposal']
        self.command('proposal', proposal['id'])
        self.assertEqual(self.read()['items'][0]['category'], 'waiting')
        self.service.undo({'receiptId': 'c1'})
        self.assertEqual(self.read()['items'][0]['category'], 'unknown')

    def test_source_change_and_expiry_reject_stale_suggestions(self):
        proposal = self.suggest()['proposal']
        self.now += 301
        with self.assertRaises(OrganizationConflict):
            self.command('proposal', proposal['id'])
        self.now = 1000
        self.sessions.rename(self.session['id'], title='新目标')
        with self.assertRaises(OrganizationConflict):
            self.command('proposal', proposal['id'])

    def test_idempotency_and_command_id_reuse(self):
        self.command()
        self.assertTrue(self.command()['replayed'])
        self.assertEqual(self.read()['items'][0]['revision'], 1)
        with self.assertRaises(OrganizationConflict):
            self.command(value='desk')

    def test_undo_does_not_overwrite_later_manual_edit(self):
        self.command()
        self.command('category', 'reference', 1, 'c2')
        with self.assertRaises(OrganizationConflict):
            self.service.undo({'receiptId': 'c1'})
        self.assertEqual(self.read()['items'][0]['category'], 'reference')

    def test_pin_protects_placement_and_group_is_metadata_only(self):
        self.command('pinned', True)
        with self.assertRaises(ValueError):
            self.command(revision=1, identity='c2')
        self.command('group', '实验', 1, 'c3')
        self.assertEqual(self.read()['items'][0]['group'], '实验')

    def test_low_confidence_unknown_and_malformed_provider_are_not_applied(self):
        self.assertIsNone(self.suggest(self.answer(confidence=.4))['proposal'])
        self.assertIsNone(self.suggest(self.answer(choice='unknown'))['proposal'])
        broken = self.answer(); broken['answers']['category']['probabilities']['waiting'] = .1
        with self.assertRaises(RuntimeError):
            self.suggest(broken)
        self.assertEqual(self.read()['items'][0]['revision'], 0)

    def test_internal_and_owned_sessions_are_not_ordinary_space_candidates(self):
        internal = self.sessions.create(title='内部', session_kind='subagent_runtime', tool_profile_version='subagent-readonly-v1')
        with self.assertRaises(ValueError):
            self.service.source('session:' + internal['id'])
        self.assertEqual(self.service.read({'keys': ['session:missing']})['unavailable'], ['session:missing'])

    def test_http_descriptors_reach_real_service_and_validate_command(self):
        dispatcher = DescriptorRouteDispatcher(SimpleNamespace(space_organization=self.service), query_first=lambda query, key: '')
        route = find_route('POST', '/api/agent/organization/read')
        self.assertIsNotNone(route)
        result = dispatcher.dispatch(route, payload={'keys': [self.key]})
        self.assertEqual(result.payload['items'][0]['key'], self.key)
        route = find_route('POST', '/api/agent/organization/command')
        with self.assertRaises(ValueError):
            dispatcher.dispatch(route, payload={'spaceKey': self.key, 'automatic': True})
        result = dispatcher.dispatch(route, payload={'spaceKey': self.key, 'commandId': 'http-command',
            'expectedRevision': 0, 'operation': 'category', 'value': 'reference'})
        self.assertTrue(result.payload['ok'])
        self.assertEqual(self.read()['items'][0]['category'], 'reference')


    def test_room_organization_preserves_timeline_and_excludes_partner_sessions(self):
        partners = [self.sessions.create(title=f'伙伴 {index}') for index in range(2)]
        room = self.rooms.create(title='等待数据的协作', routing_policy='manual_mentions', participants=[
            {'sessionId': partner['id'], 'roleId': partner['roleId'], 'roleVersion': '1', 'displayName': partner['title']}
            for partner in partners
        ])
        key = 'room:' + room['id']
        before = self.rooms.get(room['id'])
        self.service.command({'spaceKey': key, 'commandId': 'room-organize',
            'expectedRevision': 0, 'operation': 'placement', 'value': 'shelf'})
        self.assertEqual(self.service.read({'keys': [key]})['items'][0]['placement'], 'shelf')
        self.service.undo({'receiptId': 'room-organize'})
        self.assertEqual(self.rooms.get(room['id']), before)
        with self.assertRaises(ValueError):
            self.service.source('session:' + partners[0]['id'])

    def test_real_http_preserves_conflict_and_provider_error_status(self):
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from rag_ime.debug_server import DebugRequestHandler

        class Handler(DebugRequestHandler):
            pass
        Handler.service = SimpleNamespace(
            space_organization=self.service,
            config=SimpleNamespace(server_name='debug server'),
            management_security_settings=lambda: {'postRequiresJson': True},
        )
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)

        def post(action, body):
            request = Request(f'http://127.0.0.1:{server.server_port}/api/agent/organization/{action}',
                data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as error:
                with error:
                    return error.code, json.loads(error.read())

        body = {'spaceKey': self.key, 'commandId': 'one', 'expectedRevision': 0,
                'operation': 'category', 'value': 'reference'}
        self.assertEqual(post('command', body)[0], 200)
        self.assertEqual(post('command', {**body, 'commandId': 'stale'})[0], 409)
        with patch('rag_ime.jev.api_key', return_value='test-only'), patch('rag_ime.jev.evaluate', side_effect=RuntimeError('private-provider-details')):
            status, result = post('suggest', {'spaceKey': self.key})
        self.assertEqual(status, 503)
        self.assertNotIn('private-provider-details', json.dumps(result))
        self.assertIn('模型账号', result['error'])


if __name__ == '__main__':
    unittest.main()
