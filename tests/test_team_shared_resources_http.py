"""Admin publication and project selection through the actual Team HTTP server."""
from __future__ import annotations

from http.cookiejar import CookieJar
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, build_opener

from rag_ime.team.gateway import TeamApplication, make_team_server
from tests import test_team_gateway as gateway_fixture


class TeamSharedResourcesHttpTests(unittest.TestCase):
    request = gateway_fixture.TeamGatewayTests.request
    login = gateway_fixture.TeamGatewayTests.login

    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='paw-team-resources-http-')
        self.root = Path(self.tmp.name)
        repository = Path(__file__).resolve().parents[1]
        self.app = TeamApplication(self.root / 'data', self.root / 'web', package_source_root=repository,
                                   package_catalog=repository / 'deploy/team/packages.catalog.example.json')
        self.admin = self.app.identity.bootstrap_admin('admin', 'admin-password-123')
        self.alice = self.app.identity.create_member(self.admin['id'], 'alice', 'alice-password-123')
        self.project = self.app.identity.create_project(self.alice['id'], 'Shared project')
        self.server = make_team_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.app.close()
        self.tmp.cleanup()

    def publish(self, login, version='1.1.0'):
        status, result = self.request('/api/team/resources/publish',
                                      {'packageId': 'session-review', 'version': version}, csrf=login['csrfToken'])
        self.assertEqual(status, 201, result)
        return result['resource']

    def selection_path(self, space_id=None):
        return f'/api/team/spaces/{space_id or self.project["id"]}/resources'

    def create_session(self, login):
        status, result = self.request(f'/team/spaces/{self.project["id"]}/api/agent/sessions',
                                      {'title': 'Shared-resource fixture'}, csrf=login['csrfToken'])
        self.assertEqual(status, 201, result)
        return result['session']

    def snapshot(self, session):
        return self.request(f'/api/team/spaces/{self.project["id"]}/sessions/{quote(session["id"], safe="")}/resources')

    def test_catalog_publication_and_roles_do_not_start_a_space_or_host(self):
        self.assertEqual(self.request('/api/team/resources')[0], 401)
        admin = self.login()
        status, catalog = self.request('/api/team/resources/catalog')
        self.assertEqual(status, 200, catalog)
        self.assertIn('session-review', [item['id'] for item in catalog['items']])
        publication = self.publish(admin)
        self.assertEqual(self.publish(admin)['publicationId'], publication['publicationId'])
        self.assertEqual(publication['status'], 'published')
        public = json.dumps(publication)
        self.assertNotIn(str(self.root), public)
        self.assertNotIn('sourcePath', public)
        self.assertEqual(self.app._services, {})
        # A platform administrator still cannot access an unjoined project.
        self.assertIn(self.request(self.selection_path())[0], (403, 404))
        alice = self.login('alice', 'alice-password-123')
        self.assertEqual(self.request('/api/team/resources')[1]['items'][0]['publicationId'], publication['publicationId'])
        self.assertEqual(self.request('/api/team/resources/catalog')[0], 403)
        self.assertEqual(self.request('/api/team/resources/publish',
                                      {'packageId': 'session-review', 'version': '1.0.0'}, csrf=alice['csrfToken'])[0], 403)
        self.assertEqual(self.app._services, {})

    def test_project_update_keeps_existing_sessions_and_children_fixed(self):
        admin = self.login()
        first = self.publish(admin, '1.0.0')
        second = self.publish(admin)
        alice = self.login('alice', 'alice-password-123')
        csrf = alice['csrfToken']
        status, selected = self.request(self.selection_path(),
                                        {'baseRevision': 0, 'publicationIds': [first['publicationId']]}, csrf=csrf)
        self.assertEqual(status, 200, selected)
        session = self.create_session(alice)
        self.assertEqual(self.snapshot(session)[1]['publicationIds'], [first['publicationId']])
        status, selected = self.request(self.selection_path(),
                                        {'baseRevision': 1, 'publicationIds': [second['publicationId']]}, csrf=csrf)
        self.assertEqual(status, 200, selected)
        self.assertEqual(self.snapshot(session)[1]['publicationIds'], [first['publicationId']])
        new_session = self.create_session(alice)
        self.assertEqual(self.snapshot(new_session)[1]['publicationIds'], [second['publicationId']])
        service = self.app.service(self.app.identity.require_space(self.alice['id'], self.project['id']))
        child = service.agent.sessions.create_child(session['id'], title='Fixture child')
        self.assertEqual(self.snapshot(child)[1]['publicationIds'], [first['publicationId']])
        conflict, _ = self.request(self.selection_path(), {'baseRevision': 1, 'publicationIds': []}, csrf=csrf)
        self.assertEqual(conflict, 409)
        self.assertEqual(self.request(self.selection_path())[1]['revision'], 2)

    def test_csrf_path_injection_withdrawal_and_current_membership(self):
        admin = self.login()
        self.assertEqual(self.request('/api/team/resources/publish', {'packageId': 'session-review', 'version': '1.1.0'})[0], 403)
        self.assertEqual(self.request('/api/team/resources/publish',
                                      {'packageId': 'session-review', 'version': '1.1.0', 'sourcePath': '/etc'}, csrf=admin['csrfToken'])[0], 400)
        resource = self.publish(admin)
        status, _ = self.request(f'/api/team/resources/{resource["publicationId"]}/status',
                                 {'status': 'withdrawn'}, csrf=admin['csrfToken'])
        self.assertEqual(status, 200)
        alice = self.login('alice', 'alice-password-123')
        status, _ = self.request(self.selection_path(),
                                 {'baseRevision': 0, 'publicationIds': [resource['publicationId']]}, csrf=alice['csrfToken'])
        self.assertEqual(status, 409)
        self.app.identity.add_project_member(self.alice['id'], self.project['id'], self.admin['id'], role='viewer')
        admin = self.login()
        self.assertEqual(self.request(self.selection_path())[0], 200)
        self.assertEqual(self.request(self.selection_path(), {'baseRevision': 0, 'publicationIds': []}, csrf=admin['csrfToken'])[0], 403)
        self.app.identity.remove_project_member(self.alice['id'], self.project['id'], self.admin['id'])
        self.assertIn(self.request(self.selection_path())[0], (403, 404))

    def test_two_people_open_selected_native_app_as_separate_owned_tasks(self):
        admin = self.login()
        status, result = self.request('/api/team/resources/publish',
                                      {'packageId': '@paw/zhanggui-wenshu', 'version': '0.2.0'}, csrf=admin['csrfToken'])
        self.assertEqual(status, 201, result)
        publication = result['resource']
        self.assertEqual(publication['metadata']['extensionApp']['id'], 'extension:zhanggui-wenshu')
        alice = self.login('alice', 'alice-password-123')
        path = f'/team/spaces/{self.project["id"]}/api/agent/sessions'
        body = {'title': 'Shared App HTTP fixture', 'surfaceKind': 'extension_app',
                'ownerAppId': 'extension:zhanggui-wenshu', 'surfaceKey': 'ask'}
        self.assertEqual(self.request(path, body, csrf=alice['csrfToken'])[0], 403)
        status, selected = self.request(self.selection_path(),
                                        {'baseRevision': 0, 'publicationIds': [publication['publicationId']]}, csrf=alice['csrfToken'])
        self.assertEqual(status, 200, selected)
        with patch('rag_ime.pi.host_client.PiRuntimeHostClient.start') as host:
            status, first = self.request(path, body, csrf=alice['csrfToken'])
            self.assertEqual(status, 201, first)
            self.app.identity.add_project_member(self.alice['id'], self.project['id'], self.admin['id'], role='contributor')
            admin = self.login()
            status, second = self.request(path, body, csrf=admin['csrfToken'])
            self.assertEqual(status, 201, second)
            host.assert_not_called()
        first, second = first['session'], second['session']
        self.assertNotEqual(first['id'], second['id'])
        self.assertNotEqual(first['workspaceRoots'], second['workspaceRoots'])
        self.assertEqual(first['ownerUserId'], self.alice['id'])
        self.assertEqual(second['ownerUserId'], self.admin['id'])
        self.assertEqual(self.snapshot(first)[1]['publicationIds'], self.snapshot(second)[1]['publicationIds'])

    def test_team_app_can_update_mode_with_its_runtime_projection(self):
        alice = self.login('alice', 'alice-password-123')
        session = self.create_session(alice)
        path = f'/team/spaces/{self.project["id"]}/api/agent/sessions/{quote(session["id"], safe="")}'
        status, result = self.request(
            path,
            {
                'mode': 'coordinator',
                'executionMode': 'workspace_managed',
                'toolProfileVersion': 'control-center-v1',
                'projectContextEnabled': False,
                'workspaceRoots': [],
                'piSkillsEnabled': False,
                'codexSkillsEnabled': False,
            },
            csrf=alice['csrfToken'],
            method='PATCH',
        )
        self.assertEqual(status, 200, result)
        self.assertTrue(result['ok'])
        self.assertEqual(result['session']['mode'], 'coordinator')
        self.assertEqual(result['session']['executionMode'], 'workspace_managed')
        self.assertEqual(result['session']['toolProfileVersion'], 'control-center-v1')
        self.assertEqual(result['session']['workspaceRoots'], session['workspaceRoots'])


if __name__ == '__main__':
    unittest.main()
