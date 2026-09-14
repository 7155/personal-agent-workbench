from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit

from rag_ime.team.errors import TeamError
from rag_ime.team.gateway import TeamApplication, make_team_server
from rag_ime.team.preview_gateway import make_preview_server
from rag_ime.team.previews import PreviewOrigin
from rag_ime.team.project_context import TeamProjectContext


class FakePreviewRuntime:
    """Explicit test adapter; these tests make no OCI isolation claim."""

    def __init__(self):
        self.live = set()
        self.starts = []
        self.requests = []
        self.start_hook = lambda: None
        self.request_hook = lambda: None
        self.stop_verified = True
        self.closed = False
        self.shutdown_hook = lambda: None
        self.response = SimpleNamespace(status=200, headers=[('Content-Type', 'text/html')], body=b'<h1>fixture preview</h1>')

    def start(self, deployment_id, source):
        if self.closed:
            raise TeamError(503, 'preview_closed', 'Preview runtime is closed')
        self.starts.append((deployment_id, source))
        self.live.add(deployment_id)
        self.start_hook()
        if self.closed:
            raise TeamError(503, 'preview_closed', 'Preview runtime is closed')

    def is_running(self, deployment_id):
        return deployment_id in self.live

    def stop(self, deployment_id):
        if self.stop_verified:
            self.live.discard(deployment_id)
        return SimpleNamespace(verified=self.stop_verified)

    def request(self, deployment_id, method, path, headers, body):
        self.requests.append((deployment_id, method, path, headers, body))
        self.request_hook()
        return self.response

    def shutdown(self):
        self.closed = True
        self.shutdown_hook()
        for deployment_id in list(self.live):
            if not self.stop(deployment_id).verified:
                raise RuntimeError('Synthetic removal could not be verified')


class TeamPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='paw-preview-http-')
        self.root = Path(self.tmp.name)
        web = self.root / 'web'
        web.mkdir()
        (web / 'index.html').write_text('<html><head></head></html>')
        self.runtime = FakePreviewRuntime()
        self.app = TeamApplication(self.root / 'data', web, preview_runtime=self.runtime,
                                   preview_origin='http://{deployment}.localhost:8771')
        self.admin = self.app.identity.bootstrap_admin('admin', 'administrator-123')
        self.alice = self.app.identity.create_member(self.admin['id'], 'alice', 'alice-password-123')
        self.project = self.app.identity.create_project(self.admin['id'], 'Shared registration')
        self.project_id = self.project['id']
        self.app.identity.add_project_member(self.admin['id'], self.project_id, self.alice['id'])
        source = self.root / 'repo'
        source.mkdir()
        for args in (['init', '--initial-branch=main'], ['config', 'user.name', 'fixture'], ['config', 'user.email', 'fixture@example.test']):
            self.git(source, args)
        (source / 'index.html').write_text('<h1>committed fixture</h1>')
        (source / 'paw-preview.json').write_text(json.dumps({
            'schemaVersion': 1, 'command': ['python3', '-m', 'http.server', '3000'],
            'port': 3000, 'healthPath': '/', 'startupTimeoutSeconds': 1,
        }))
        self.git(source, ['add', '.'])
        self.git(source, ['commit', '-m', 'fixture'])
        self.app.workspaces.ensure_project(self.project_id, source)
        self.validation = []

        def verify(path, context):
            self.validation.append(context)
            return {'ok': True}

        self.app.workspaces.verifier = verify
        self.team_server = make_team_server(self.app, port=0)
        self.preview_server = make_preview_server(self.app.previews, port=0)
        self.servers = [self.team_server, self.preview_server]
        self.threads = [Thread(target=s.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True) for s in self.servers]
        for thread in self.threads:
            thread.start()
        self.admin_login = self.app.identity.login('admin', 'administrator-123')
        self.alice_login = self.app.identity.login('alice', 'alice-password-123')

    @staticmethod
    def git(source, args):
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=source,
                       capture_output=True, check=True, env={'PATH': '/usr/bin:/bin', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'})

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)
        self.runtime.stop_verified = True
        self.app.close()
        self.tmp.cleanup()

    def settle(self):
        with self.app.previews._lock:
            jobs = list(self.app.previews._jobs.values())
        for job in jobs:
            job.join(timeout=10)
            self.assertFalse(job.is_alive())

    def start(self, request_id='test-start'):
        self.app.previews.start(self.admin['id'], self.project_id, request_id)
        self.settle()
        return self.app.previews.status(self.admin['id'], self.project_id)

    def http(self, server, path, *, method='GET', headers=None, body=None):
        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            conn.close()

    def team_http(self, path='', *, method='GET', body=None, login=None, csrf=True):
        current = login or self.admin_login
        headers = {'Cookie': 'paw_team_dev=' + current['token'], 'Origin': f'http://127.0.0.1:{self.team_server.server_port}'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if csrf:
            headers['X-CSRF-Token'] = current['csrfToken']
        return self.http(self.team_server, f'/api/team/projects/{self.project_id}/preview' + path,
                         method=method, headers=headers, body=json.dumps(body) if body is not None else None)

    def enter(self, login=None):
        status, headers, _ = self.team_http('/open', login=login)
        self.assertEqual(status, 302)
        location = urlsplit(dict(headers)['Location'])
        status, entry_headers, _ = self.http(self.preview_server, location.path + '?' + location.query,
                                           headers={'Host': location.netloc, 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate'})
        self.assertEqual(status, 303)
        cookie = dict(entry_headers)['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertNotIn('Domain=', cookie)
        self.assertEqual(dict(entry_headers)['Location'], '/')
        return location.netloc, cookie.split(';', 1)[0]

    def test_health_failure_keeps_previous_preview_and_idempotency_does_not_restart(self):
        first = self.start()
        first_id = first['active']['id']
        self.assertEqual(len(self.validation), 1)
        self.assertEqual(self.start()['active']['id'], first_id)
        self.assertEqual(len(self.runtime.starts), 1)

        def fail():
            raise TeamError(503, 'preview_health_failed', 'Preview health check failed')

        self.runtime.start_hook = fail
        second = self.start('next-attempt')
        self.assertEqual(second['active']['id'], first_id)
        self.assertEqual(second['latest']['status'], 'failed')
        self.assertEqual(self.runtime.live, {first_id})

    def test_new_brief_during_startup_prevents_switch_and_revokes_candidate(self):
        first = self.start()
        self.runtime.start_hook = lambda: TeamProjectContext(self.app).publish_brief(self.admin['id'], self.project_id, {
            'baseRevision': 0, 'objective': 'Changed registration', 'acceptanceCriteria': ['New field required'],
        })
        second = self.start('changed-brief')
        self.assertEqual(second['active']['id'], first['active']['id'])
        self.assertEqual(second['latest']['status'], 'failed')
        self.assertEqual(second['currentRequirementsRevision'], 1)

    def test_stop_during_startup_cannot_be_undone_by_late_success(self):
        entered, release = Event(), Event()

        def wait_for_stop():
            entered.set()
            self.assertTrue(release.wait(5))

        self.runtime.start_hook = wait_for_stop
        self.app.previews.start(self.admin['id'], self.project_id, 'stop-during-start')
        self.assertTrue(entered.wait(5))
        try:
            stopped = self.app.previews.stop(self.admin['id'], self.project_id)
            self.assertIsNone(stopped['active'])
            self.assertEqual(stopped['latest']['status'], 'recovery_required')
        finally:
            release.set()
        self.settle()
        self.assertEqual(self.app.previews.status(self.admin['id'], self.project_id)['latest']['status'], 'stopped')
        self.assertFalse(self.runtime.live)

    def test_current_authority_and_separate_origins_guard_the_entire_http_response(self):
        self.start()
        host, cookie = self.enter(self.alice_login)
        headers = {'Host': host, 'Cookie': cookie + '; app_session=demo; paw_team_dev=must-not-forward',
                   'Authorization': 'Bearer must-not-forward', 'X-CSRF-Token': 'must-not-forward', 'Sec-Fetch-Site': 'same-origin'}
        status, response_headers, body = self.http(self.preview_server, '/', headers=headers)
        self.assertEqual((status, body), (200, b'<h1>fixture preview</h1>'))
        forwarded = {name.lower(): value for name, value in self.runtime.requests[-1][3].items()}
        self.assertEqual(forwarded['cookie'], 'app_session=demo')
        self.assertNotIn('authorization', forwarded)
        self.assertNotIn('x-csrf-token', forwarded)
        self.assertNotIn('Access-Control-Allow-Origin', dict(response_headers))
        self.assertIn("frame-ancestors 'none'", dict(response_headers)['Content-Security-Policy'])
        self.runtime.request_hook = lambda: self.app.identity.remove_project_member(self.admin['id'], self.project_id, self.alice['id'])
        self.assertEqual(self.http(self.preview_server, '/', headers=headers)[0], 401)
        self.assertTrue(self.runtime.live, 'Member removal must not delete the project service')

    def test_logout_cross_origin_cross_deployment_and_viewer_writes_are_denied(self):
        self.start()
        host, cookie = self.enter(self.alice_login)
        headers = {'Host': host, 'Cookie': cookie}
        self.assertEqual(self.http(self.preview_server, '/', headers={**headers, 'Origin': 'https://paw.example.test'})[0], 403)
        self.assertEqual(self.http(self.preview_server, '/', headers={**headers, 'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'cors'})[0], 403)
        self.assertEqual(self.http(self.preview_server, '/', headers={**headers, 'Host': 'pv-' + 'f' * 32 + '.localhost:8771'})[0], 401)
        self.app.identity.logout(self.alice_login['token'])
        self.assertEqual(self.http(self.preview_server, '/', headers=headers)[0], 401)
        self.app.identity.remove_project_member(self.admin['id'], self.project_id, self.alice['id'])
        self.app.identity.add_project_member(self.admin['id'], self.project_id, self.alice['id'], role='viewer')
        self.alice_login = self.app.identity.login('alice', 'alice-password-123')
        host, cookie = self.enter(self.alice_login)
        self.assertEqual(self.http(self.preview_server, '/register', method='POST',
                                  headers={'Host': host, 'Cookie': cookie, 'Origin': 'http://' + host}, body=b'{}')[0], 403)
        self.assertEqual(self.team_http('', method='POST', body={'clientRequestId': 'viewer'}, login=self.alice_login)[0], 403)

    def test_app_cannot_widen_cookie_scope_or_redirect_into_team_console(self):
        self.start()
        host, cookie = self.enter()
        self.runtime.response = SimpleNamespace(status=200, headers=[
            ('Set-Cookie', 'app_session=valid; Path=/; HttpOnly'),
            ('Set-Cookie', 'wide=bad; Domain=localhost; Path=/'),
            ('Set-Cookie', 'paw_preview_dev=bad; Path=/'),
            ('Access-Control-Allow-Origin', '*'),
        ], body=b'fixture')
        status, output, _ = self.http(self.preview_server, '/', headers={'Host': host, 'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertEqual([value for name, value in output if name == 'Set-Cookie'], ['app_session=valid; HttpOnly; Path=/'])
        self.runtime.response = SimpleNamespace(status=302, headers=[('Location', 'http://127.0.0.1:8770/api/team/me')], body=b'')
        self.assertEqual(self.http(self.preview_server, '/', headers={'Host': host, 'Cookie': cookie})[0], 502)
        self.runtime.response = SimpleNamespace(status=302, headers=[('Location', '/' + chr(92) + 'evil.example')], body=b'')
        self.assertEqual(self.http(self.preview_server, '/', headers={'Host': host, 'Cookie': cookie})[0], 502)

    def test_close_cancels_and_joins_startup_before_returning(self):
        entered, release = Event(), Event()
        self.runtime.start_hook = lambda: (entered.set(), release.wait(5))
        self.runtime.shutdown_hook = release.set
        self.app.previews.start(self.admin['id'], self.project_id, 'shutdown')
        self.assertTrue(entered.wait(5))
        self.app.close()
        self.assertFalse(self.app.previews._jobs)
        self.assertFalse(self.runtime.live)
        self.assertEqual(self.app.previews.store.public_status(self.admin['id'], self.project_id)['latest']['status'], 'stopped')

    def test_cancel_before_launch_cleans_source_without_requiring_docker(self):
        entered, release = Event(), Event()

        def verifier(_path, _context):
            entered.set()
            self.assertTrue(release.wait(5))
            return {'ok': True}

        self.app.workspaces.verifier = verifier
        self.runtime.stop_verified = False
        self.app.previews.start(self.admin['id'], self.project_id, 'prelaunch-cancel')
        self.assertTrue(entered.wait(5))
        try:
            self.app.previews.stop(self.admin['id'], self.project_id)
        finally:
            release.set()
        self.settle()
        self.assertFalse(self.runtime.starts)
        self.assertFalse(list(self.app.workspaces.staging_root.iterdir()))
        self.assertEqual(self.app.previews.store.public_status(self.admin['id'], self.project_id)['latest']['status'], 'stopped')

    def test_stop_failure_is_recovery_required_and_never_a_false_stopped_receipt(self):
        self.start()
        host, cookie = self.enter()
        self.runtime.stop_verified = False
        state = self.app.previews.stop(self.admin['id'], self.project_id)
        self.assertIsNone(state['active'])
        self.assertEqual(state['latest']['status'], 'recovery_required')
        self.assertEqual(self.http(self.preview_server, '/', headers={'Host': host, 'Cookie': cookie})[0], 401)
        self.assertTrue(self.runtime.live)
        self.runtime.stop_verified = True
        self.app.previews.reap()
        self.assertFalse(self.runtime.live)

    def test_old_tabs_stay_pinned_and_only_one_previous_deployment_is_retained(self):
        first = self.start()
        first_host, first_cookie = self.enter()
        second = self.start('second-version')
        second_host, second_cookie = self.enter()
        self.assertNotEqual(first_host, second_host)
        self.assertEqual(self.http(self.preview_server, '/', headers={'Host': first_host, 'Cookie': first_cookie})[0], 200)
        self.start('third-version')
        self.app.previews.reap()
        self.assertNotIn(first['active']['id'], self.runtime.live)
        self.assertIn(second['active']['id'], self.runtime.live)
        self.assertEqual(self.http(self.preview_server, '/', headers={'Host': first_host, 'Cookie': first_cookie})[0], 401)
        self.assertEqual(self.http(self.preview_server, '/', headers={'Host': second_host, 'Cookie': second_cookie})[0], 200)
        self.assertEqual(len(self.runtime.live), 2)

    def test_restart_cannot_restore_ready_from_database_or_replay_a_start(self):
        self.start()
        # Simulate loss of the controller while its external process survives.
        self.app.previews._closed.set()
        self.app.previews._reaper.join(timeout=2)
        replacement = TeamApplication(self.app.data_root, self.app.static_dir, preview_runtime=self.runtime,
                                      preview_origin='http://{deployment}.localhost:8771')
        try:
            state = replacement.previews.status(self.admin['id'], self.project_id)
            self.assertIsNone(state['active'])
            self.assertIn(state['latest']['status'], {'recovery_required', 'stopped'})
            self.assertEqual(len(self.runtime.starts), 1)
            replacement.previews.reap()
            self.assertFalse(self.runtime.live)
        finally:
            replacement.close()

    def test_main_preview_routes_require_csrf_and_reject_request_configuration(self):
        self.assertEqual(self.team_http('', method='POST', body={'clientRequestId': 'no-csrf'}, csrf=False)[0], 403)
        self.assertEqual(self.team_http('', method='POST', body={'clientRequestId': 'invalid', 'image': 'hostile'})[0], 400)
        status, _, body = self.team_http('', method='POST', body={'clientRequestId': 'http-start'})
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(body)['configured'])
        self.settle()
        self.assertEqual(self.team_http('/stop', method='POST', body={})[0], 200)
        self.assertFalse(self.runtime.live)

    def test_http_entry_reaches_real_runtime_unix_transport_with_explicit_synthetic_launcher(self):
        # Exercise the actual manager/runtime/proxy seam. The only fake is the
        # launcher: it runs a synthetic Unix HTTP server, never project code.
        from tests.test_team_preview_runtime import _SyntheticLauncher
        from rag_ime.team.preview_runtime import PreviewRuntime
        with TemporaryDirectory(prefix='pv-wire-', dir='/tmp') as directory:
            runtime = PreviewRuntime(Path(directory), image='explicit-synthetic-image',
                                     launcher=_SyntheticLauncher(), worker_user='501:20')
            self.app.previews.runtime = runtime
            try:
                state = self.start('real-unix-transport')
                self.assertIsNotNone(state['active'], state)
                host, cookie = self.enter()
                status, _, body = self.http(self.preview_server, '/registration', method='POST',
                                           headers={'Host': host, 'Cookie': cookie, 'Origin': 'http://' + host}, body=b'fixture-email')
                self.assertEqual(status, 200)
                self.assertIn(b'POST', body)
                self.app.previews.stop(self.admin['id'], self.project_id)
                self.assertFalse(runtime.handles())
            finally:
                runtime.shutdown()
                self.app.previews.runtime = self.runtime


class PreviewOriginTests(unittest.TestCase):
    def test_console_default_port_collision_and_credentials_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for origin in ('https://:fixture-password@example.test', 'https://@example.test', 'https://%70v-' + 'a' * 32 + '.example.test'):
                with self.subTest(origin=origin), self.assertRaises(ValueError):
                    TeamApplication(root / 'data', root, public_origin=origin)
            with self.assertRaises(ValueError):
                TeamApplication(root / 'data', root, public_origin='https://pv-' + 'a' * 32 + '.example.test:443',
                                preview_runtime=FakePreviewRuntime(), preview_origin='https://{deployment}.example.test')

    def test_origin_is_per_deployment_and_remote_access_requires_tls(self):
        for invalid in ('http://{deployment}.example.com', 'https://example.com/{deployment}',
                        'https://{deployment}.example.com/path', 'https://user@{deployment}.example.com',
                        'https://{deployment}.example.com?other=1'):
            with self.subTest(origin=invalid), self.assertRaises(ValueError):
                PreviewOrigin(invalid)
        origin = PreviewOrigin('https://{deployment}.previews.example.com')
        self.assertEqual(origin.cookie_name, '__Host-paw_preview')
        deployment = 'pv-' + 'a' * 32
        self.assertEqual(origin.deployment_for_host(deployment + '.previews.example.com'), deployment)
        with self.assertRaises(TeamError):
            origin.deployment_for_host(deployment + '.previews.example.com.attacker.test')
