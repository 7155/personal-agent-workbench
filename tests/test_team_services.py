from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import threading
import unittest
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, build_opener
from urllib.parse import quote
import json

from rag_ime.team.gateway import TeamApplication, make_team_server
from rag_ime.team.errors import TeamError
from tests import test_team_gateway as gateway_fixture


class TeamSpaceServiceTests(unittest.TestCase):
    request = gateway_fixture.TeamGatewayTests.request
    login = gateway_fixture.TeamGatewayTests.login

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix='paw-team-services-')
        root = Path(self.tmp.name)
        self.app = TeamApplication(root / 'data', root / 'web')
        self.admin = self.app.identity.bootstrap_admin('admin', 'admin-password-123')
        self.alice = self.app.identity.create_member(self.admin['id'], 'alice', 'alice-password-123')
        self.server = make_team_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.app.close()
        self.tmp.cleanup()

    def create(self, login, space_id, title='Task'):
        code, result = self.request(f'/team/spaces/{space_id}/api/agent/sessions', {'title': title}, csrf=login['csrfToken'])
        self.assertEqual(code, 201, result)
        return result['session']

    def test_real_private_sessions_and_databases_do_not_cross_accounts(self) -> None:
        admin = self.login()
        private = admin['spaces'][0]['id']
        session = self.create(admin, private, 'Private planning')
        self.assertEqual(session['ownerUserId'], self.admin['id'])
        binding = self.app.grants.binding(session['id'])
        self.assertEqual(session['workspaceRoots'], [binding['workspacePath']])
        self.assertEqual(session['executionMode'], 'workspace_managed')
        self.assertFalse(session['codexSkillsEnabled'])
        alice = self.login('alice', 'alice-password-123')
        alice_space = alice['spaces'][0]['id']
        second = self.create(alice, alice_space, 'Alice private')
        self.assertNotEqual(second['workspaceRoots'], session['workspaceRoots'])
        self.assertNotEqual(self.app.service(alice['spaces'][0]).config.db_path, self.app.service(admin['spaces'][0]).config.db_path)
        code, _ = self.request(f'/team/spaces/{private}/api/agent/sessions')
        self.assertIn(code, (403, 404))
        code, _ = self.request(f'/team/spaces/{alice_space}/api/agent/sessions/{session["id"]}/messages')
        self.assertIn(code, (403, 404))

    def test_project_progress_is_shared_but_members_cannot_drive_other_sessions(self) -> None:
        project = self.app.identity.create_project(self.admin['id'], 'Website')
        self.app.identity.add_project_member(self.admin['id'], project['id'], self.alice['id'], role='contributor')
        admin = self.login()
        session = self.create(admin, project['id'], 'Website layout')
        self.assertEqual(session['audience'], 'project')
        alice = self.login('alice', 'alice-password-123')
        prefix = f'/team/spaces/{project["id"]}'
        code, result = self.request(prefix + f'/api/agent/sessions/{session["id"]}/messages')
        self.assertEqual(code, 200, result)
        code, result = self.request(prefix + '/api/agent/sessions?limit=100&projectionOnly=1')
        self.assertEqual(code, 200, result)
        self.assertFalse(next(item for item in result['items'] if item['id'] == session['id'])['canControl'])
        code, _ = self.request(prefix + f'/api/agent/sessions/{session["id"]}/prompt', {'message': 'Take over'}, csrf=alice['csrfToken'])
        self.assertIn(code, (403, 404))
        with patch('rag_ime.pi.host_client.PiRuntimeHostClient.start', side_effect=AssertionError('Pi Host must not launch when creating a stored Session')):
            own = self.create(alice, project['id'], 'Website API')
        self.assertNotEqual(own['workspaceRoots'], session['workspaceRoots'])
        root = Path(self.app.grants.binding(own['id'])['workspacePath'])
        (root / 'api.txt').write_text('Draft API contract\n')
        status, result = self.request(f'/api/team/projects/{project["id"]}/drafts', {'sessionId': own['id'], 'title': 'API draft'}, csrf=alice['csrfToken'])
        self.assertEqual(status, 201, result)
        self.assertEqual(result['draft']['creatorUserId'], self.alice['id'])
        self.assertEqual(result['draft']['title'], 'API draft')
        draft_id = result['draft']['draftId']
        code, diff = self.request(f'/api/team/projects/{project["id"]}/drafts/{draft_id}/diff')
        self.assertEqual(code, 200, diff)
        self.assertIn('+Draft API contract', diff['diff'])
        self.app.identity.remove_project_member(self.admin['id'], project['id'], self.alice['id'])
        self.app.revoke_user(self.alice['id'], space_id=project['id'])
        self.assertIn(self.request(prefix + '/api/agent/sessions')[0], (403, 404))
        self.login()
        code, result = self.request(f'/api/team/projects/{project["id"]}/drafts')
        self.assertEqual(code, 200, result)
        self.assertEqual(len(result['items']), 1)
        self.app.identity.add_project_member(self.admin['id'], project['id'], self.alice['id'], role='contributor')
        self.login('alice', 'alice-password-123')
        code, snapshot = self.request(prefix + '/api/agent/sessions?limit=100&projectionOnly=1')
        self.assertEqual(code, 200, snapshot)
        retired = next(item for item in snapshot['items'] if item['id'] == own['id'])
        self.assertFalse(retired['canControl'], 'Rejoining cannot revive controls for a retired Session')

    def test_two_humans_can_add_their_agents_to_one_room(self) -> None:
        project = self.app.identity.create_project(self.admin['id'], 'Shared website')
        self.app.identity.add_project_member(self.admin['id'], project['id'], self.alice['id'], role='contributor')
        admin = self.login()
        prefix = f'/team/spaces/{project["id"]}'
        code, result = self.request(prefix + '/api/agent/rooms', {
            'title': 'Build website',
            'participants': [{'roleId': 'companion-present-v1'}, {'roleId': 'companion-firstlight-v1'}],
        }, csrf=admin['csrfToken'])
        self.assertEqual(code, 201, result)
        room = result['room']
        code, rejection = self.request(prefix + f'/api/agent/rooms/{room["id"]}/messages', {
            'message': 'Start the shared project', 'clientMessageId': 'unconfigured-fixture',
        }, csrf=admin['csrfToken'])
        self.assertEqual(code, 409, rejection)
        self.assertEqual(rejection['errorCode'], 'team_worker_unavailable')
        alice = self.login('alice', 'alice-password-123')
        code, result = self.request(prefix + f'/api/agent/rooms/{room["id"]}/participants', {
            'roleId': 'companion-future-v1', 'collaborationRole': 'implementer',
        }, csrf=alice['csrfToken'])
        self.assertIn(code, (200, 201), result)
        code, result = self.request(prefix + f'/api/agent/rooms/{room["id"]}')
        self.assertEqual(code, 200, result)
        code, listing = self.request(prefix + '/api/agent/rooms?limit=100&projectionOnly=1')
        self.assertEqual(code, 200, listing)
        participants = result['room']['participants']
        self.assertEqual({item['ownerUserId'] for item in participants}, {self.admin['id'], self.alice['id']})
        self.assertTrue(all(item['ownerDisplayName'] for item in participants))
        alice_participant = next(item for item in participants if item['ownerUserId'] == self.alice['id'])
        admin_participant = next(item for item in participants if item['ownerUserId'] == self.admin['id'])
        work = {'objective': 'Implement API', 'expectedOutput': 'API draft',
                'acceptanceCriteria': ['API contract is included in the draft'],
                'currentOwnerParticipantId': alice_participant['id'], 'clientMessageId': 'owned-work'}
        code, rejected = self.request(prefix + f'/api/agent/rooms/{room["id"]}/work-items', {
            **work, 'createdByParticipantId': admin_participant['id'],
        }, csrf=alice['csrfToken'])
        self.assertEqual(code, 403, rejected)
        code, accepted = self.request(prefix + f'/api/agent/rooms/{room["id"]}/work-items', work,
                                      csrf=alice['csrfToken'])
        self.assertEqual(code, 201, accepted)
        self.assertEqual(accepted['workItem']['createdByParticipantId'], alice_participant['id'])
        code, missing = self.request(prefix + '/api/agent/rooms/missing-room')
        self.assertEqual(code, 404, missing)
        admin = self.login()
        code, rejected = self.request(prefix + f'/api/agent/rooms/{room["id"]}', {
            'workspaceRoots': ['/etc'],
        }, csrf=admin['csrfToken'], method='PATCH')
        self.assertEqual(code, 403, rejected)
        self.assertEqual(rejected['errorCode'], 'team_scope_required')
        code, updated = self.request(prefix + f'/api/agent/rooms/{room["id"]}/participants', {
            'participantId': alice_participant['id'], 'collaborationRole': 'researcher',
        }, csrf=admin['csrfToken'], method='PATCH')
        self.assertEqual(code, 200, updated)
        bindings = [self.app.grants.binding(item['sessionId']) for item in participants]
        self.assertEqual({item['ownerUserId'] for item in bindings}, {self.admin['id'], self.alice['id']})
        self.assertEqual(len({item['workspacePath'] for item in bindings}), len(bindings))
        service = self.app.service(project)
        for item in bindings:
            self.assertEqual(service.agent.sessions.get(item['sessionId'])['workspaceRoots'], [item['workspacePath']])

    def test_background_child_requires_explicit_parent_instead_of_another_owners_path(self) -> None:
        login = self.login()
        space = login['spaces'][0]
        parent = self.create(login, space['id'])
        store = self.app.service(space).agent.sessions
        with self.assertRaises(TeamError):
            store.create(title='Forged from a path', workspace_roots=parent['workspaceRoots'])
        child = store.create_child(parent['id'], title='Bounded child', session_kind='subagent_runtime')
        binding = self.app.grants.binding(child['id'])
        self.assertEqual(binding['ownerUserId'], self.admin['id'])
        self.assertNotEqual(child['workspaceRoots'], parent['workspaceRoots'])

    def test_http_cold_history_is_readable_and_corruption_is_not_an_empty_session(self) -> None:
        login = self.login()
        space = login['spaces'][0]
        session = self.create(login, space['id'], 'Stored conversation')
        service = self.app.service(space)
        history = service.history_root(session['id'])
        history.mkdir(parents=True, mode=0o700)
        transcript = history / 'stored.jsonl'
        entries = [
            {'type': 'session', 'id': 'persisted-pi'},
            {'type': 'message', 'id': 'user-1', 'parentId': '', 'timestamp': 100,
             'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'Keep this history'}]}},
        ]
        transcript.write_text(''.join(json.dumps(entry) + '\n' for entry in entries))
        service.agent.sessions.bind_runtime_session(
            session['id'], driver_id='managed-pi', runtime_kind='pi_rpc',
            external_session_id='persisted-pi', transcript_ref=str(transcript),
            branch_anchor='user-1', binding_state='active', message_count=1,
        )
        url = f'/team/spaces/{space["id"]}/api/agent/sessions/{session["id"]}/messages'
        with patch('rag_ime.pi.host_client.PiRuntimeHostClient.start') as start:
            for suffix in ('', '?view=recent'):
                code, result = self.request(url + suffix)
                self.assertEqual(code, 200, result)
                self.assertEqual(result['items'][0]['blocks'][0]['data']['text'], 'Keep this history')
            transcript.write_text('corrupt transcript\n')
            code, result = self.request(url)
            self.assertGreaterEqual(code, 400, result)
            self.assertFalse(result['ok'])
            self.assertNotIn('items', result)
            start.assert_not_called()

    def test_project_work_can_be_handed_off_without_copying_a_retired_members_authority(self) -> None:
        bob = self.app.identity.create_member(self.admin['id'], 'bob', 'bob-password-123')
        project = self.app.identity.create_project(self.admin['id'], 'Shared handoff')
        for user in (self.alice, bob):
            self.app.identity.add_project_member(self.admin['id'], project['id'], user['id'], role='contributor')
        admin = self.login()
        prefix = f'/team/spaces/{project["id"]}/api/agent/rooms'
        code, created = self.request(prefix, {
            'title': 'Registration handoff',
            'participants': [{'roleId': 'companion-present-v1'}, {'roleId': 'companion-firstlight-v1'}],
        }, csrf=admin['csrfToken'])
        self.assertEqual(code, 201, created)
        room = created['room']
        accountable = room['participants'][0]
        participants = {}
        for name in ('alice', 'bob'):
            member = self.login(name, name + '-password-123')
            code, added = self.request(prefix + f'/{room["id"]}/participants', {
                'roleId': 'companion-future-v1', 'collaborationRole': 'implementer',
            }, csrf=member['csrfToken'])
            self.assertIn(code, (200, 201), added)
            code, snapshot = self.request(prefix + f'/{room["id"]}')
            self.assertEqual(code, 200, snapshot)
            participants[name] = next(p for p in snapshot['room']['participants'] if p['ownerUserId'] == member['user']['id'])
        source = participants['alice']
        target = participants['bob']
        target_binding = self.app.grants.binding(target['sessionId'])
        source_binding = self.app.grants.binding(source['sessionId'])
        admin = self.login()
        code, created_work = self.request(prefix + f'/{room["id"]}/work-items', {
            'objective': 'Implement registration API', 'expectedOutput': 'Fixed API draft',
            'acceptanceCriteria': ['Duplicate email is explained'],
            'createdByParticipantId': accountable['id'], 'accountableParticipantId': accountable['id'],
            'currentOwnerParticipantId': source['id'], 'clientMessageId': 'handoff-fixture',
        }, csrf=admin['csrfToken'])
        self.assertEqual(code, 201, created_work)
        work_id = created_work['workItem']['id']
        self.app.identity.remove_project_member(self.admin['id'], project['id'], self.alice['id'])
        self.app.revoke_user(self.alice['id'], space_id=project['id'])
        with patch('rag_ime.pi.host_client.PiRuntimeHostClient.start') as start:
            code, reassigned = self.request(prefix + f'/{room["id"]}/work-items/{work_id}/reassign', {
                'actorParticipantId': accountable['id'], 'targetParticipantId': target['id'],
                'reason': 'Continue from shared project evidence',
            }, csrf=admin['csrfToken'])
            self.assertEqual(code, 200, reassigned)
            self.assertEqual(reassigned['workItem']['currentOwnerParticipantId'], target['id'])
            # A retained Room participant does not restore its departed owner.
            code, rejected = self.request(prefix + f'/{room["id"]}/work-items/{work_id}/reassign', {
                'actorParticipantId': accountable['id'], 'targetParticipantId': source['id'],
            }, csrf=admin['csrfToken'])
            self.assertIn(code, (403, 404), rejected)
            start.assert_not_called()
        self.assertEqual(self.app.grants.binding(target['sessionId']), target_binding)
        self.assertNotEqual(target_binding['workspacePath'], source_binding['workspacePath'])
        self.assertFalse(self.app.grants.binding(source['sessionId'], check_current=False)['active'])
        with self.assertRaises(TeamError):
            self.app.grants.binding(source['sessionId'])

    def test_project_runtime_context_keeps_the_sessions_fixed_requirements_version(self) -> None:
        project = self.app.identity.create_project(self.admin['id'], 'Scoped requirements')
        self.app.identity.add_project_member(self.admin['id'], project['id'], self.alice['id'], role='contributor')
        admin = self.login()
        brief_url = f'/api/team/projects/{project["id"]}/brief'
        code, published = self.request(brief_url, {
            'baseRevision': 0, 'objective': 'Original registration requirements',
            'acceptanceCriteria': ['Explain duplicate email'],
        }, csrf=admin['csrfToken'])
        self.assertEqual(code, 200, published)
        session = self.create(admin, project['id'], 'Registration form')
        service = self.app.service(project)
        with patch('rag_ime.pi.host_client.PiRuntimeHostClient.start') as start:
            context = service.agent._runtime_session_context(service.agent.sessions.get(session['id']))
            self.assertIn('Original registration requirements', context['sessionContext'])
            self.assertIn('Explain duplicate email', context['sessionContext'])
            code, published = self.request(brief_url, {
                'baseRevision': 1, 'objective': 'New registration requirements',
                'acceptanceCriteria': ['Company name is required'],
            }, csrf=admin['csrfToken'])
            self.assertEqual(code, 200, published)
            context = service.agent._runtime_session_context(service.agent.sessions.get(session['id']))
            self.assertIn('Original registration requirements', context['sessionContext'])
            self.assertNotIn('New registration requirements', context['sessionContext'])
            child = service.agent.sessions.create_child(session['id'], title='Field validation child')
            child_context = service.agent._runtime_session_context(child)
            self.assertIn('Original registration requirements', child_context['sessionContext'])
            self.assertNotIn('New registration requirements', child_context['sessionContext'])
            url = f'/api/team/projects/{project["id"]}/sessions/{quote(session["id"], safe="")}/requirements'
            alice = self.login('alice', 'alice-password-123')
            code, rejected = self.request(url, {'baseRevision': 1, 'revision': 2}, csrf=alice['csrfToken'])
            self.assertIn(code, (403, 404), rejected)
            admin = self.login()
            code, accepted = self.request(url, {'baseRevision': 1, 'revision': 2}, csrf=admin['csrfToken'])
            self.assertEqual(code, 200, accepted)
            self.assertEqual(accepted['requirementsRevision'], 2)
            self.assertEqual(accepted['previousRequirementsRevision'], 1)
            context = service.agent._runtime_session_context(service.agent.sessions.get(session['id']))
            self.assertIn('New registration requirements', context['sessionContext'])
            self.assertNotIn('Original registration requirements', context['sessionContext'])
            # A parent's new baseline does not silently rewrite its existing child.
            self.assertIn('Original registration requirements', service.agent._runtime_session_context(child)['sessionContext'])
            start.assert_not_called()

    def test_project_brief_http_shares_versions_without_overwriting_or_starting_pi(self) -> None:
        project = self.app.identity.create_project(self.admin['id'], 'Registration')
        self.app.identity.add_project_member(self.admin['id'], project['id'], self.alice['id'], role='contributor')
        admin = self.login()
        url = f'/api/team/projects/{project["id"]}'
        with patch('rag_ime.pi.host_client.PiRuntimeHostClient.start') as start:
            code, empty = self.request(url + '/overview')
            self.assertEqual(code, 200, empty)
            self.assertEqual(empty['brief']['revision'], 0)
            self.assertIsNone(empty['repository'])
            body = {'baseRevision': 0, 'objective': 'Ship registration',
                    'acceptanceCriteria': ['New email succeeds', 'Duplicate email has an explicit error']}
            code, first = self.request(url + '/brief', body, csrf=admin['csrfToken'])
            self.assertEqual(code, 200, first)
            self.assertEqual(first['brief']['revision'], 1)
            self.assertEqual(first['brief']['updatedByUserId'], self.admin['id'])
            code, conflict = self.request(url + '/brief', {**body, 'objective': 'A stale edit'}, csrf=admin['csrfToken'])
            self.assertEqual(code, 409, conflict)
            code, second = self.request(url + '/brief', {**body, 'baseRevision': 1, 'objective': 'Require company name'}, csrf=admin['csrfToken'])
            self.assertEqual(code, 200, second)
            self.assertEqual(second['brief']['revision'], 2)
            alice = self.login('alice', 'alice-password-123')
            code, shared = self.request(url + '/overview')
            self.assertEqual(code, 200, shared)
            self.assertEqual(shared['brief']['objective'], 'Require company name')
            self.assertEqual({item['revision'] for item in shared['briefHistory']}, {1, 2})
            self.assertEqual(self.request(url + '/brief', {**body, 'baseRevision': 2}, csrf=alice['csrfToken'])[0], 403)
            self.assertEqual(self.request(url + '/brief', body)[0], 403)
            private = alice['spaces'][0]['id']
            self.assertEqual(self.request(f'/api/team/projects/{private}/overview')[0], 404)
            self.app.identity.remove_project_member(self.admin['id'], project['id'], self.alice['id'])
            self.assertIn(self.request(url + '/overview')[0], (403, 404))
            start.assert_not_called()

    def test_project_overview_uses_room_task_metadata_without_raw_history(self) -> None:
        project = self.app.identity.create_project(self.admin['id'], 'Shared contract')
        admin = self.login()
        prefix = f'/team/spaces/{project["id"]}'
        code, created = self.request(prefix + '/api/agent/rooms', {
            'title': 'Registration implementation',
            'participants': [{'roleId': 'companion-present-v1'}, {'roleId': 'companion-firstlight-v1'}],
        }, csrf=admin['csrfToken'])
        self.assertEqual(code, 201, created)
        room = created['room']
        participant = room['participants'][0]
        code, created_work = self.request(prefix + f'/api/agent/rooms/{room["id"]}/work-items', {
            'objective': 'Implement form', 'expectedOutput': 'A form draft',
            'acceptanceCriteria': ['Duplicate email is explained'],
            'createdByParticipantId': participant['id'], 'currentOwnerParticipantId': participant['id'],
            'clientMessageId': 'project-overview-fixture',
        }, csrf=admin['csrfToken'])
        self.assertEqual(code, 201, created_work)
        service = self.app.service(project)
        history = service.history_root(participant['sessionId'])
        history.mkdir(parents=True, mode=0o700)
        (history / 'private.jsonl').write_text('PRIVATE_TRANSCRIPT_MUST_NOT_BE_PROJECT_METADATA')
        with patch('rag_ime.pi.host_client.PiRuntimeHostClient.start') as start:
            code, overview = self.request(f'/api/team/projects/{project["id"]}/overview')
            self.assertEqual(code, 200, overview)
            task = next(item for r in overview['rooms'] for item in r['workItems'] if item['id'] == created_work['workItem']['id'])
            self.assertEqual(task['currentOwnerUserId'], self.admin['id'])
            self.assertTrue(task['currentOwnerDisplayName'])
            self.assertNotIn('PRIVATE_TRANSCRIPT', json.dumps(overview))
            self.assertNotIn(str(self.app.data_root), json.dumps(overview))
            self.assertFalse(overview['runtime']['configured'])
            start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
