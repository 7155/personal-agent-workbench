from __future__ import annotations

from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from rag_ime.team.gateway import TeamApplication, make_team_server
from rag_ime.team.files import TeamFiles


class TeamGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        web = root / 'web'
        web.mkdir()
        (web / 'index.html').write_text('<html><head></head><body>PAW</body></html>')
        self.created: list[str] = []

        def factory(space):
            self.created.append(space['id'])
            files = root / 'files' / space['id']
            files.mkdir(parents=True, exist_ok=True)
            (files / 'owner.txt').write_text(space['name'])
            return SimpleNamespace(desktop_files=TeamFiles(files))

        self.app = TeamApplication(root / 'data', web, service_factory=factory)
        self.admin = self.app.identity.bootstrap_admin('admin', 'admin-password-123')
        self.alice = self.app.identity.create_member(self.admin['id'], 'alice', 'alice-password-123')
        self.server = make_team_server(self.app, host='127.0.0.1', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.app.close()
        self.tmp.cleanup()

    def request(self, path, body=None, *, csrf='', origin=None, method=None):
        headers = {'Origin': origin or self.base}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if csrf:
            headers['X-CSRF-Token'] = csrf
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        try:
            # Cold space setup initializes several stores. This transport
            # deadline bounds functional tests, not a five-second latency
            # contract; upload/worker deadline tests use their own clocks.
            with self.client.open(request, timeout=30) as response:
                raw = response.read()
                return response.status, json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def login(self, username='admin', password='admin-password-123'):
        status, result = self.request('/api/team/login', {'username': username, 'password': password})
        self.assertEqual(status, 200, result)
        self.assertNotIn('token', result)
        return result

    def test_loopback_does_not_bypass_team_login_and_services_are_lazy(self) -> None:
        personal = self.app.identity.list_spaces(self.admin['id'])[0]
        status, _ = self.request(f"/team/spaces/{personal['id']}/api/files/list")
        self.assertEqual(status, 401)
        self.assertEqual(self.created, [])
        self.assertEqual(self.request('/api/health')[0], 404)

    def test_two_accounts_have_different_private_files(self) -> None:
        admin = self.login()
        admin_space = admin['spaces'][0]['id']
        self.assertEqual(self.request(f'/team/spaces/{admin_space}/api/files/read?path=/owner.txt')[0], 200)
        alice = self.login('alice', 'alice-password-123')
        self.assertNotEqual(alice['spaces'][0]['id'], admin_space)
        self.assertIn(self.request(f'/team/spaces/{admin_space}/api/files/read?path=/owner.txt')[0], (403, 404))
        self.assertEqual(self.request(f"/team/spaces/{alice['spaces'][0]['id']}/api/files/list")[0], 200)

    def test_csrf_and_cross_origin_requests_cannot_create_members(self) -> None:
        login = self.login()
        body = {'username': 'injected', 'password': 'injected-password'}
        self.assertEqual(self.request('/api/team/members', body)[0], 403)
        self.assertEqual(self.request('/api/team/members', body, csrf=login['csrfToken'], origin='https://attacker.invalid')[0], 403)
        self.assertEqual(len(self.app.identity.list_members(self.admin['id'])), 2)

    def test_logout_revokes_the_cookie_and_does_not_destroy_project_data(self) -> None:
        login = self.login()
        status, project = self.request('/api/team/projects', {'name': 'Website'}, csrf=login['csrfToken'])
        self.assertEqual(status, 201, project)
        self.assertEqual(self.request('/api/team/logout', {}, csrf=login['csrfToken'])[0], 200)
        self.assertEqual(self.request('/api/team/me')[0], 401)
        again = self.login()
        self.assertIn(project['space']['id'], [s['id'] for s in again['spaces']])

    def test_project_viewer_cannot_create_sessions_or_get_host_provider_credentials(self) -> None:
        project = self.app.identity.create_project(self.admin['id'], 'Website')
        self.app.identity.add_project_member(self.admin['id'], project['id'], self.alice['id'], role='viewer')
        login = self.login('alice', 'alice-password-123')
        prefix = f"/team/spaces/{project['id']}"
        self.assertEqual(self.request(prefix + '/api/agent/sessions', {'title': 'unauthorized'}, csrf=login['csrfToken'])[0], 403)
        self.assertEqual(self.request(prefix + '/api/agent/providers')[0], 403)

    def test_app_boots_explicit_team_mode_without_serving_parent_files(self) -> None:
        status, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(b'name="paw-deployment" content="team"', body)
        self.assertEqual(self.request('/%2e%2e/data/team.sqlite')[0], 404)


if __name__ == '__main__':
    unittest.main()
