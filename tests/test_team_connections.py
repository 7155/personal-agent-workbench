from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

from rag_ime.team.errors import TeamError
from rag_ime.team.gateway import TeamApplication, make_team_server
from rag_ime.team.grants import execution_authority
from rag_ime.db import sqlite_connection


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.hook = None
        self.code_calls = []
        self.refresh_calls = []

    def account(self, credentials):
        if self.hook:
            self.hook()
        return {'login': 'fixture-account', 'id': '42'}

    def authorization_url(self, **kwargs):
        return 'https://github.com/login/oauth/authorize?' + urlencode(kwargs)

    def exchange_code(self, **kwargs):
        self.code_calls.append(kwargs)
        return {'accessToken': 'fixture-oauth-token', 'tokenType': 'bearer'}

    def refresh_token(self, credentials):
        self.refresh_calls.append(credentials)
        return {'accessToken': 'fixture-refreshed-token', 'tokenType': 'bearer'}

    def execute(self, credentials, operation, repository, args):
        self.calls.append((credentials, operation, repository, args))
        if self.hook:
            self.hook()
        return {'number': 5, 'title': 'Fixture result'}


class TeamConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='paw-team-connections-')
        self.root = Path(self.tmp.name)
        self.github = FakeGitHub()
        self.app = TeamApplication(
            self.root / 'data', self.root / 'web',
            connections_config={'githubClientId': 'fixture-client', 'githubClientSecret': 'fixture-secret'},
            connection_client=self.github,
        )
        self.admin = self.app.identity.bootstrap_admin('admin', 'administrator-123')
        self.alice = self.app.identity.create_member(self.admin['id'], 'alice', 'alice-password-123')
        self.project = self.app.identity.create_project(self.admin['id'], 'Website')
        self.space_id = self.project['id']
        self.app.identity.add_project_member(self.admin['id'], self.space_id, self.alice['id'])
        self.login = self.app.identity.login('admin', 'administrator-123')
        self.alice_login = self.app.identity.login('alice', 'alice-password-123')
        self.server = make_team_server(self.app, port=0)
        self.app.public_origin = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        self.thread.start()
        self.base = f'/api/team/spaces/{self.space_id}/connections'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.app.close()
        self.tmp.cleanup()

    def http(self, suffix='', *, body=None, login=None, csrf=True, absolute=False):
        current = login or self.login
        headers = {'Cookie': 'paw_team_dev=' + current['token'], 'Origin': self.app.public_origin}
        if csrf:
            headers['X-CSRF-Token'] = current['csrfToken']
        if body is not None:
            headers['Content-Type'] = 'application/json'
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('GET' if body is None else 'POST', suffix if absolute else self.base + suffix,
                         body=None if body is None else json.dumps(body), headers=headers)
            response = conn.getresponse()
            raw = response.read()
            result = json.loads(raw) if raw else {}
            return response.status, result, dict(response.getheaders())
        finally:
            conn.close()

    def input(self, scope='personal'):
        return {'scope': scope, 'label': 'Website source', 'repositories': ['example/website'],
                'operations': ['repo.read', 'issue.create'], 'token': 'fixture-private-token'}

    def create(self, scope='personal'):
        status, result, _ = self.http('/token', body=self.input(scope))
        self.assertEqual(status, 201, result)
        self.assertNotIn('fixture-private-token', json.dumps(result))
        return result['connection']

    def session(self, user=None):
        actor = (user or self.admin)['id']
        space = self.app.identity.require_space(actor, self.space_id)
        service = self.app.service(space)
        with service.agent.sessions.as_actor(actor):
            return service.agent.sessions.create(title='Connection task')

    def grant(self, connection, session):
        status, result, _ = self.http('/grants', body={
            'connectionId': connection['id'], 'sessionId': session['id'], 'repository': 'example/website',
            'operations': ['repo.read', 'issue.create'], 'ttlSeconds': 600,
        })
        self.assertEqual(status, 201, result)
        return result['grant']

    def execute(self, session, grant, op='repo.read', **args):
        with execution_authority(lambda: self.app.grants.binding(session['id'])):
            return self.app.connections.execute(session['id'], op, {
                'grantId': grant['id'], 'repository': 'example/website', **args,
            }, tool_call_id='fixture-tool-call')

    def test_connection_setup_and_list_stay_private_and_do_not_start_runtime(self):
        self.create()
        status, own, _ = self.http()
        self.assertEqual(status, 200)
        self.assertEqual(len(own['items']), 1)
        self.assertEqual(own['sessions'], [])
        self.assertEqual(self.app._services, {})
        status, peer, _ = self.http(login=self.alice_login)
        self.assertEqual(status, 200)
        self.assertEqual(peer['items'], [])
        self.assertNotIn('fixture-private-token', json.dumps(own))
        self.assertEqual(self.http('/token', body=self.input(), csrf=False)[0], 403)

    def test_external_token_failure_does_not_expire_the_paw_login(self):
        with patch.object(self.github, 'account', side_effect=TeamError(401, 'connection_reconnect_required', 'GitHub authorization must be renewed')):
            status, result, _ = self.http('/token', body=self.input())
        self.assertEqual(status, 409, 'Only PAW authentication errors use HTTP401')
        self.assertEqual(result['errorCode'], 'connection_reconnect_required')
        self.assertEqual(self.http('/api/team/me', absolute=True)[0], 200)

    def test_task_authority_required_and_scope_checked_before_external_io(self):
        session = self.session()
        grant = self.grant(self.create(), session)
        with self.assertRaises(TeamError):
            self.app.connections.execute(session['id'], 'list', {}, tool_call_id='call')
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.execute(session, grant)['number'], 5)
        other = self.session()
        with self.assertRaises(TeamError):
            self.execute(other, grant)
        self.assertEqual(len(self.github.calls), 1)

    def test_write_receipt_replay_and_unknown_outcome_never_resend(self):
        session = self.session()
        grant = self.grant(self.create(), session)
        params = {'requestId': 'publish-1', 'title': 'Issue', 'body': 'Fixture'}
        first = self.execute(session, grant, 'issue.create', **params)
        self.assertEqual(self.execute(session, grant, 'issue.create', **params), first)
        self.assertEqual(len(self.github.calls), 1)
        with self.assertRaises(TeamError):
            self.execute(session, grant, 'issue.create', **{**params, 'title': 'Different'})
        def uncertain():
            raise TeamError(502, 'connection_outcome_unknown', 'External result is unknown')
        self.github.hook = uncertain
        for _ in range(2):
            with self.assertRaises(TeamError) as caught:
                self.execute(session, grant, 'issue.create', **{**params, 'requestId': 'publish-2'})
            self.assertEqual(caught.exception.code, 'connection_outcome_unknown')
        self.assertEqual(len(self.github.calls), 2)

    def test_write_receipt_persists_only_effect_metadata_and_never_submitted_text(self):
        session = self.session()
        grant = self.grant(self.create(), session)
        with patch.object(self.github, 'execute', return_value={'number': 7, 'title': 'PRIVATE_TITLE', 'body': 'PRIVATE_ISSUE_BODY', 'htmlUrl': 'https://unexpected.invalid/?token=PRIVATE'}):
            result = self.execute(session, grant, 'issue.create', requestId='minimal-receipt', title='PRIVATE_TITLE', body='PRIVATE_ISSUE_BODY')
        self.assertEqual(result, {'number': 7, 'htmlUrl': 'https://github.com/example/website/issues/7'})
        with sqlite_connection(self.app.identity.db_path) as conn:
            serialized = conn.execute('SELECT result_json FROM team_connection_operations WHERE request_id=?', ('minimal-receipt',)).fetchone()[0]
        self.assertNotIn('PRIVATE', serialized)
        self.assertEqual(json.loads(serialized), result)

    def test_canceled_refresh_before_dispatch_preserves_other_members_project_grants(self):
        session, peer = self.session(), self.session(self.alice)
        store = self.app.connections.store
        connection = store.create_connection(self.admin['id'], self.space_id, scope='project', label='Project account',
            repositories=['example/website'], operations=['repo.read', 'issue.create'], account_login='fixture-account',
            credentials={'accessToken': 'fixture-old', 'tokenType': 'bearer', 'expiresAtMs': self.app.connections.now_ms() + 1000, 'refreshToken': 'fixture-refresh'})
        grant = self.grant(connection, session)
        peer_grant = store.create_grant(self.alice['id'], self.space_id, connection_id=connection['id'], session_id=peer['id'], repository='example/website', operations=['repo.read'])
        begin = store.begin_refresh
        def cancel_after_begin(connection_id):
            credentials = begin(connection_id)
            store.revoke_grant(self.admin['id'], self.space_id, grant['id'])
            return credentials
        with patch.object(store, 'begin_refresh', side_effect=cancel_after_begin):
            with self.assertRaises(TeamError):
                self.execute(session, grant)
        self.assertEqual(self.github.refresh_calls, [])
        self.assertEqual(store.authorize(peer['id'], peer_grant['id'], 'repo.read', 'example/website')['connection']['status'], 'active')
        self.assertEqual(self.execute(peer, peer_grant)['number'], 5)
        self.assertEqual(len(self.github.refresh_calls), 1)

    def test_actual_provider_adapter_keeps_malformed_success_as_unknown_without_resend(self):
        from rag_ime.team.connection_github import GitHubConnectionClient, GitHubResponse
        calls = []
        def transport(request):
            calls.append(request)
            payload = {'login': 'fixture-account', 'id': 42} if request.path == '/user' else {}
            return GitHubResponse(status=200 if request.path == '/user' else 201,
                                  headers=(('Content-Type', 'application/json'),), body=json.dumps(payload).encode())
        self.app.connections.client = GitHubConnectionClient(transport=transport)
        session = self.session()
        grant = self.grant(self.create(), session)
        for _ in range(2):
            with self.assertRaises(TeamError) as caught:
                self.execute(session, grant, 'issue.create', requestId='malformed-success', title='Only once', body='Fixture')
            self.assertEqual(caught.exception.code, 'connection_outcome_unknown')
        self.assertEqual([request.path for request in calls], ['/user', '/repos/example/website/issues'])

    def test_expiring_credentials_refresh_without_widening_or_invalidating_task_grant(self):
        session = self.session()
        connection = self.app.connections.store.create_connection(
            self.admin['id'], self.space_id, scope='personal', label='Expiring connection',
            repositories=['example/website'], operations=['repo.read', 'issue.create'],
            account_login='fixture-account', credentials={'accessToken': 'fixture-old-token', 'tokenType': 'bearer',
                                                         'expiresAtMs': self.app.connections.now_ms() + 1000,
                                                         'refreshToken': 'fixture-refresh-token'},
        )
        grant = self.grant(connection, session)
        self.assertEqual(self.execute(session, grant)['number'], 5)
        self.assertEqual(len(self.github.refresh_calls), 1)
        self.assertEqual(self.github.calls[0][0]['accessToken'], 'fixture-refreshed-token')
        current = self.http()[1]
        self.assertEqual(current['items'][0]['status'], 'active')
        self.assertEqual(current['grants'][0]['id'], grant['id'])
        self.assertEqual(current['grants'][0]['status'], 'active')

    def test_revocation_during_io_hides_result_and_replay_does_not_bypass_revocation(self):
        session = self.session()
        grant = self.grant(self.create(), session)
        self.github.hook = lambda: self.app.connections.store.revoke_grant(self.admin['id'], self.space_id, grant['id'])
        with self.assertRaises(TeamError):
            self.execute(session, grant)
        self.assertEqual(len(self.github.calls), 1)

    def test_real_tool_gateway_preserves_read_only_and_exact_allowlist(self):
        session = self.session()
        grant = self.grant(self.create(), session)
        service = self.app.service(self.project)
        request = {'schemaVersion': 'rag-ime.agent-tool-call.v1', 'sessionId': session['id'],
                   'tool': 'connections', 'toolCallId': 'tool-1',
                   'args': {'op': 'issue.create', 'grantId': grant['id'], 'repository': 'example/website',
                            'requestId': 'gateway-write-1', 'title': 'Fixture', 'body': 'Only this issue'}}
        with execution_authority(lambda: self.app.grants.binding(session['id'])):
            result = service.agent_tools.execute(request)
        self.assertEqual(result['result']['number'], 5)
        self.assertNotIn('approval', result['result'])
        with service.agent.sessions.as_actor(self.admin['id']):
            service.agent.sessions.set_runtime_policy(session['id'], execution_mode='read_only', allowed_tools=['connections'])
        with execution_authority(lambda: self.app.grants.binding(session['id'])):
            with self.assertRaisesRegex(ValueError, 'read-only'):
                service.agent_tools.execute({**request, 'toolCallId': 'tool-2'})
            read = service.agent_tools.execute({**request, 'args': {'op': 'list'}})
        self.assertEqual(read['result']['items'][0]['id'], grant['id'])
        self.assertEqual(len(self.github.calls), 1)

    def test_only_own_current_sessions_appear_without_reading_transcripts(self):
        own = self.session()
        self.session(self.alice)
        with patch.object(self.app, 'service', side_effect=AssertionError('metadata GET must remain lazy')):
            result = self.http()[1]
        self.assertEqual([row['id'] for row in result['sessions']], [own['id']])

    def test_policy_change_while_waiting_for_network_denies_the_queued_write(self):
        session = self.session()
        grant = self.grant(self.create(), session)
        service = self.app.service(self.project)
        entered, release = Event(), Event()
        outcomes = []
        class Gate:
            def __enter__(self):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Test network gate was not released')
            def __exit__(self, *args):
                pass
        request = {'schemaVersion': 'rag-ime.agent-tool-call.v1', 'sessionId': session['id'],
                   'tool': 'connections', 'toolCallId': 'queued-tool',
                   'args': {'op': 'issue.create', 'grantId': grant['id'], 'repository': 'example/website',
                            'requestId': 'queued-write', 'title': 'Fixture', 'body': ''}}
        def write():
            try:
                with execution_authority(lambda: self.app.grants.binding(session['id'])):
                    outcomes.append(service.agent_tools.execute(request))
            except Exception as exc:
                outcomes.append(exc)
        with patch.object(self.app.connections, '_network_slots', Gate()):
            thread = Thread(target=write, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with service.agent.sessions.as_actor(self.admin['id']):
                    service.agent.sessions.set_runtime_policy(session['id'], execution_mode='read_only', allowed_tools=['connections'])
            finally:
                release.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], TeamError)
        self.assertEqual(outcomes[0].code, 'connection_task_policy_changed')
        self.assertEqual(self.github.calls, [])

    def test_successful_write_receipt_survives_manager_restart_but_new_grant_is_required_after_revocation(self):
        from rag_ime.team.connections import TeamConnections
        session = self.session()
        grant = self.grant(self.create(), session)
        args = {'requestId': 'restart-1', 'title': 'One issue', 'body': ''}
        expected = self.execute(session, grant, 'issue.create', **args)
        self.app.connections.close()
        self.app.connections = TeamConnections(self.app, config={}, client=self.github)
        self.assertEqual(self.execute(session, grant, 'issue.create', **args), expected)
        self.assertEqual(len(self.github.calls), 1)
        self.app.connections.store.revoke_grant(self.admin['id'], self.space_id, grant['id'])
        with self.assertRaises(TeamError):
            self.execute(session, grant, 'issue.create', **args)

    def test_crashed_pending_write_does_not_replay_after_restart(self):
        session = self.session()
        grant = self.grant(self.create(), session)
        args = {'grantId': grant['id'], 'repository': 'example/website', 'requestId': 'pending-1', 'title': 'Issue', 'body': ''}
        self.app.connections._reserve(session['id'], 'pending-1', grant['id'], 'issue.create', args)
        with self.assertRaises(TeamError) as caught:
            self.execute(session, grant, 'issue.create', requestId='pending-1', title='Issue', body='')
        self.assertEqual(caught.exception.code, 'connection_outcome_unknown')
        self.assertEqual(self.github.calls, [])

    def test_oauth_query_is_not_logged_and_server_restart_invalidates_pending_flow(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from rag_ime.team.connections import TeamConnections
        body = self.input()
        del body['token']
        _, result, _ = self.http('/oauth/start', body=body)
        state = parse_qs(urlsplit(result['authorizationUrl']).query)['state'][0]
        self.app.connections.close()
        self.app.connections = TeamConnections(self.app, config={}, client=self.github)
        logs = StringIO()
        with redirect_stdout(logs):
            status, _, headers = self.http('/api/team/connections/github/callback?' + urlencode({'code': 'do-not-log-code', 'state': state}), absolute=True)
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/?teamConnection=failed')
        self.assertEqual(self.github.code_calls, [])
        self.assertNotIn(state, logs.getvalue())
        self.assertNotIn('do-not-log-code', logs.getvalue())

    def test_oauth_uses_single_use_state_bound_to_original_login(self):
        body = self.input()
        del body['token']
        status, result, _ = self.http('/oauth/start', body=body)
        self.assertEqual(status, 200, result)
        state = parse_qs(urlsplit(result['authorizationUrl']).query)['state'][0]
        callback = '/api/team/connections/github/callback?' + urlencode({'code': 'fixture-code', 'state': state})
        status, _, headers = self.http(callback, absolute=True)
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/?teamConnection=connected')
        self.assertEqual(len(self.github.code_calls), 1)
        self.assertTrue(self.github.code_calls[0]['verifier'])
        self.assertEqual(self.http(callback, absolute=True)[2]['Location'], '/?teamConnection=failed')
        self.assertEqual(len(self.github.code_calls), 1)
        self.assertNotIn('fixture-oauth-token', json.dumps(self.http()[1]))

    def test_oauth_wrong_human_or_logout_cannot_complete(self):
        body = self.input()
        del body['token']
        _, result, _ = self.http('/oauth/start', body=body)
        state = parse_qs(urlsplit(result['authorizationUrl']).query)['state'][0]
        callback = '/api/team/connections/github/callback?' + urlencode({'code': 'fixture-code', 'state': state})
        self.assertEqual(self.http(callback, login=self.alice_login, absolute=True)[2]['Location'], '/?teamConnection=failed')
        self.assertEqual(self.github.code_calls, [])
        self.github.hook = lambda: self.app.identity.logout(self.login['token'])
        status, _, _ = self.http('/token', body=self.input())
        self.assertIn(status, (401, 403))
        self.assertEqual(self.app.connections.store.list_connections(self.admin['id'], self.space_id), [])

    def test_oauth_expiry_and_replaced_login_do_not_reuse_handshake(self):
        body = self.input()
        del body['token']
        _, result, _ = self.http('/oauth/start', body=body)
        state = parse_qs(urlsplit(result['authorizationUrl']).query)['state'][0]
        replacement_login = self.app.identity.login('admin', 'administrator-123')
        callback = '/api/team/connections/github/callback?' + urlencode({'code': 'fixture-code', 'state': state})
        self.assertEqual(self.http(callback, login=replacement_login, absolute=True)[2]['Location'], '/?teamConnection=failed')
        _, result, _ = self.http('/oauth/start', body=body)
        state = parse_qs(urlsplit(result['authorizationUrl']).query)['state'][0]
        callback = '/api/team/connections/github/callback?' + urlencode({'code': 'fixture-code', 'state': state})
        now = self.app.connections.now_ms()
        with patch.object(self.app.connections, 'now_ms', return_value=now + 601_000):
            self.assertEqual(self.http(callback, absolute=True)[2]['Location'], '/?teamConnection=failed')
        self.assertEqual(self.github.code_calls, [])

    def test_project_creator_cannot_be_replaced_by_platform_admin_without_membership(self):
        other = self.app.identity.create_project(self.alice['id'], 'Alice project')
        self.base = f'/api/team/spaces/{other["id"]}/connections'
        self.assertEqual(self.http('/token', body=self.input('project'))[0], 404)
        self.assertEqual(self.github.calls, [])

    def test_oauth_state_capacity_and_client_urls_are_not_user_controlled(self):
        body = self.input()
        del body['token']
        self.assertEqual(self.http('/oauth/start', body={**body, 'redirectUri': 'https://elsewhere.invalid'})[0], 400)
        for _ in range(8):
            self.assertEqual(self.http('/oauth/start', body=body)[0], 200)
        self.assertEqual(self.http('/oauth/start', body=body)[0], 429)

    def test_failed_oauth_url_creation_leaves_no_pending_flow_and_callback_errors_still_redirect(self):
        body = self.input()
        del body['token']
        login_id = self.app.identity.authenticate(self.login['token'])['sessionId']
        with patch.object(self.github, 'authorization_url', side_effect=RuntimeError('fixture construction failure')):
            with self.assertRaises(RuntimeError):
                self.app.connections.start_oauth(self.admin['id'], self.space_id, login_id, body)
        self.assertEqual(self.app.connections._flows, {})
        with patch.object(self.app.connections, 'finish_oauth', side_effect=RuntimeError('PRIVATE_CALLBACK_DETAIL')):
            status, result, headers = self.http('/api/team/connections/github/callback?code=fixture&state=fixture', absolute=True)
        self.assertEqual((status, result, headers['Location']), (303, {}, '/?teamConnection=failed'))

    def test_unconfigured_connection_route_is_explicit_and_never_creates_vault(self):
        app = TeamApplication(self.root / 'disabled-data', self.root / 'web')
        try:
            user = app.identity.bootstrap_admin('admin', 'administrator-123')
            space = app.identity.list_spaces(user['id'])[0]
            result = app.connections.list(user['id'], space['id'])
            self.assertFalse(result['configured'])
            self.assertEqual(result['items'], [])
            self.assertFalse((app.data_root / 'secrets').exists())
        finally:
            app.close()


class ConnectionConfigurationTests(unittest.TestCase):
    def test_runtime_schema_rejects_write_ids_on_reads_and_extra_list_arguments(self):
        from jsonschema import Draft202012Validator
        from rag_ime.agent_tools import _runtime_tool_parameter_schema
        schema = _runtime_tool_parameter_schema('connections', ['list', 'repo.read', 'issue.create'])
        validator = Draft202012Validator(schema)
        self.assertFalse(validator.is_valid({'op': 'list', 'grantId': 'grant'}))
        self.assertFalse(validator.is_valid({'op': 'repo.read', 'grantId': 'grant', 'repository': 'example/website', 'requestId': 'write-only'}))
        self.assertTrue(validator.is_valid({'op': 'issue.create', 'grantId': 'grant', 'repository': 'example/website', 'requestId': 'create-one', 'title': 'Fixture', 'body': ''}))

    def test_owner_only_config_and_safe_field_validation(self):
        from rag_ime.team.__main__ import load_connections
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            path = root / 'connections.json'
            path.write_text('{}')
            path.chmod(0o600)
            self.assertEqual(load_connections(path), {})
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'owner-only'):
                load_connections(path)
            path.chmod(0o600)
            link = root / 'link.json'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                load_connections(link)
            for invalid in ('[]', '{"proxy":"https://unknown.invalid"}', ' ' * 65537):
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    load_connections(path)


if __name__ == '__main__':
    unittest.main()
