"""Shared project material through the authenticated, space-bound HTTP path."""
from __future__ import annotations

from http.cookiejar import CookieJar
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from rag_ime.team.gateway import TeamApplication, make_team_server
from rag_ime.team.grants import execution_authority
from tests import test_team_gateway as gateway_fixture


class TeamKnowledgeHttpTests(unittest.TestCase):
    request = gateway_fixture.TeamGatewayTests.request
    login = gateway_fixture.TeamGatewayTests.login

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix='paw-team-knowledge-http-')
        self.root = Path(self.tmp.name)
        self.app = TeamApplication(self.root / 'data', self.root / 'web')
        self.admin = self.app.identity.bootstrap_admin('admin', 'admin-password-123')
        self.alice = self.app.identity.create_member(self.admin['id'], 'alice', 'alice-password-123')
        self.project = self.app.identity.create_project(self.admin['id'], 'Shared website')
        self.app.identity.add_project_member(self.admin['id'], self.project['id'], self.alice['id'], role='contributor')
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

    @staticmethod
    def path(space_id: str, suffix: str = '') -> str:
        return f'/team/spaces/{space_id}/api/knowledge-bases{suffix}'

    def create_base(self, login, space_id, name='Project decisions'):
        status, body = self.request(self.path(space_id), {
            'name': name, 'agentEnabled': True, 'parserProvider': 'builtin',
        }, csrf=login['csrfToken'])
        self.assertEqual(status, 201, body)
        return body['base']

    def upload(self, login, space_id, kb_id, content, *, name='contract.md', headers=None):
        query = urlencode({'fileName': name, 'mimeType': 'text/markdown', 'parserProvider': 'builtin'})
        request = Request(self.base + self.path(space_id, f'/{kb_id}/documents/import?{query}'),
                          data=content, headers={
                              'Origin': self.base, 'X-CSRF-Token': login['csrfToken'],
                              'Content-Type': 'text/markdown', **(headers or {}),
                          }, method='POST')
        try:
            response = self.client.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def ready(self, space_id, kb_id, doc_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, body = self.request(self.path(space_id, f'/{kb_id}/documents/{doc_id}'))
            self.assertEqual(status, 200, body)
            if body['document']['status'] not in {'queued', 'parsing', 'indexing'}:
                self.assertEqual(body['document']['status'], 'ready', body)
                return
            threading.Event().wait(0.03)
        self.fail('The uploaded document did not finish indexing')

    def test_member_upload_is_shared_and_project_material_survives_their_departure(self):
        admin = self.login()
        space_id = self.project['id']
        kb = self.create_base(admin, space_id)
        alice = self.login('alice', 'alice-password-123')
        content = b'# Registration\n\nThe shared canary is PROJECT-ONLY-241.\n'
        status, uploaded = self.upload(alice, space_id, kb['id'], content)
        self.assertEqual(status, 201, uploaded)
        doc_id = uploaded['receipt']['documentId']
        self.ready(space_id, kb['id'], doc_id)
        status, hits = self.request(self.path(space_id, f'/{kb["id"]}/search'),
                                    {'query': 'canary', 'mode': 'lexical'}, csrf=alice['csrfToken'])
        self.assertEqual(status, 200, hits)
        self.assertIn('PROJECT-ONLY-241', json.dumps(hits))
        status, source = self.request(self.path(space_id, f'/{kb["id"]}/documents/{doc_id}/source'))
        self.assertEqual(status, 200)
        self.assertEqual(source, content)
        self.app.identity.remove_project_member(self.admin['id'], space_id, self.alice['id'])
        self.app.revoke_user(self.alice['id'], space_id=space_id)
        self.assertIn(self.request(self.path(space_id, f'/{kb["id"]}/documents/{doc_id}/source'))[0], (403, 404))
        self.login()
        self.assertEqual(self.request(self.path(space_id, f'/{kb["id"]}/documents/{doc_id}/source')), (200, content))

    def test_same_search_term_in_personal_and_two_project_spaces_does_not_cross_into_the_agent(self):
        login = self.login()
        personal = login['spaces'][0]
        second = self.app.identity.create_project(self.admin['id'], 'Another project')
        bases = {}
        for space, marker in [(personal, 'PERSONAL-ONLY-735'), (self.project, 'PROJECT-A-682'), (second, 'PROJECT-B-193')]:
            kb = self.create_base(login, space['id'])
            bases[space['id']] = kb
            status, body = self.upload(login, space['id'], kb['id'], f'Sharedword {marker}\n'.encode())
            self.assertEqual(status, 201, body)
            self.ready(space['id'], kb['id'], body['receipt']['documentId'])
        alice = self.login('alice', 'alice-password-123')
        kb = bases[self.project['id']]
        status, results = self.request(self.path(self.project['id'], f'/{kb["id"]}/search'),
                                       {'query': 'Sharedword', 'mode': 'lexical'}, csrf=alice['csrfToken'])
        self.assertEqual(status, 200, results)
        self.assertIn('PROJECT-A-682', json.dumps(results))
        for marker in ('PERSONAL-ONLY-735', 'PROJECT-B-193'):
            self.assertNotIn(marker, json.dumps(results))
        for other in (personal, second):
            status, body = self.request(self.path(other['id']))
            self.assertIn(status, (403, 404), body)
            status, body = self.request(self.path(self.project['id'], f'/{bases[other["id"]]["id"]}'))
            self.assertIn(status, (400, 403, 404), body)
        with patch('rag_ime.pi.host_client.PiRuntimeHostClient.start', side_effect=AssertionError('No Provider execution')):
            status, body = self.request(f'/team/spaces/{self.project["id"]}/api/agent/sessions',
                                        {'title': 'Use shared project decisions'}, csrf=alice['csrfToken'])
            self.assertEqual(status, 201, body)
            session_id = body['session']['id']
            service = self.app.service(self.project)
            with execution_authority(lambda: self.app.grants.binding(session_id)):
                result = service.agent_tools.execute({
                    'schemaVersion': 'rag-ime.agent-tool-call.v1', 'sessionId': session_id,
                    'tool': 'knowledge', 'toolCallId': 'shared-search',
                    'args': {'op': 'search', 'kbId': kb['id'], 'query': 'Sharedword', 'searchMode': 'lexical'},
                })
            self.assertIn('PROJECT-A-682', json.dumps(result))
            self.assertNotIn('PERSONAL-ONLY-735', json.dumps(result))
            self.assertNotIn('PROJECT-B-193', json.dumps(result))

    def test_viewer_can_search_but_no_member_can_use_hidden_management_to_expand_their_role(self):
        login = self.login()
        space_id = self.project['id']
        kb = self.create_base(login, space_id)
        status, uploaded = self.upload(login, space_id, kb['id'], b'Viewer can read this evidence.\n')
        self.assertEqual(status, 201, uploaded)
        doc = uploaded['receipt']['documentId']
        self.ready(space_id, kb['id'], doc)
        alice = self.login('alice', 'alice-password-123')
        for body, suffix, method in [
            ({'name': 'Forbidden'}, '', 'POST'),
            ({'expectedRevision': kb['revision'], 'agentEnabled': False}, f'/{kb["id"]}', 'PATCH'),
            (None, f'/{kb["id"]}/documents/{doc}', 'DELETE'),
        ]:
            status, result = self.request(self.path(space_id, suffix), body, csrf=alice['csrfToken'], method=method)
            self.assertEqual(status, 403, result)
        self.app.identity.remove_project_member(self.admin['id'], space_id, self.alice['id'])
        self.app.identity.add_project_member(self.admin['id'], space_id, self.alice['id'], role='viewer')
        status, result = self.request(self.path(space_id, f'/{kb["id"]}/search'),
                                      {'query': 'evidence', 'mode': 'lexical'}, csrf=alice['csrfToken'])
        self.assertEqual(status, 200, result)
        self.assertTrue(result['items'])
        status, result = self.upload(alice, space_id, kb['id'], b'Forbidden viewer upload')
        self.assertEqual(status, 403, result)
        self.assertEqual(len(self.request(self.path(space_id, f'/{kb["id"]}/documents'))[1]['items']), 1)

    def test_raw_upload_preserves_csrf_origin_and_bounded_framing(self):
        login = self.login()
        space_id = self.project['id']
        kb = self.create_base(login, space_id)
        for headers, expected in [
            ({'X-CSRF-Token': ''}, 403),
            ({'Origin': 'https://unrelated.invalid'}, 403),
            ({'Content-Encoding': 'gzip'}, 415),
            ({'Transfer-Encoding': 'chunked'}, 400),
        ]:
            status, body = self.upload(login, space_id, kb['id'], b'No import', headers=headers)
            self.assertEqual(status, expected, body)
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        query = urlencode({'fileName': 'large.md', 'mimeType': 'text/markdown'})
        connection.putrequest('POST', self.path(space_id, f'/{kb["id"]}/documents/import?{query}'))
        for key, value in [('Origin', self.base), ('X-CSRF-Token', login['csrfToken']),
                           ('Cookie', self.cookie_header()), ('Content-Length', str(8 * 1024 * 1024 + 1))]:
            connection.putheader(key, value)
        connection.endheaders()
        try:
            response = connection.getresponse()
            self.assertEqual(response.status, 413, response.read())
        finally:
            connection.close()
        self.assertEqual(self.request(self.path(space_id, f'/{kb["id"]}/documents'))[1]['items'], [])

    def cookie_header(self):
        request = Request(self.base + '/api/team/me')
        for handler in self.client.handlers:
            if isinstance(handler, HTTPCookieProcessor):
                handler.cookiejar.add_cookie_header(request)
                return request.get_header('Cookie') or ''
        raise AssertionError('The HTTP fixture has no cookie handler')

    def test_revocation_while_receiving_a_file_prevents_its_intake(self):
        login = self.login()
        space_id = self.project['id']
        kb = self.create_base(login, space_id)
        alice = self.login('alice', 'alice-password-123')
        service = self.app.service(self.project)
        entered = threading.Event()
        prepare = service.prepare_team_request

        def observed_prepare(request):
            result = prepare(request)
            if request.request.path_id == 'knowledgeBases.document.import':
                entered.set()
            return result

        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        content = b'Late upload must not reach the project library.\n'
        query = urlencode({'fileName': 'late.md', 'mimeType': 'text/markdown'})
        with patch.object(service, 'prepare_team_request', side_effect=observed_prepare):
            connection.putrequest('POST', self.path(space_id, f'/{kb["id"]}/documents/import?{query}'))
            for key, value in [('Origin', self.base), ('X-CSRF-Token', alice['csrfToken']),
                               ('Cookie', self.cookie_header()), ('Content-Length', str(len(content)))]:
                connection.putheader(key, value)
            connection.endheaders()
            connection.send(content[:1])
            try:
                self.assertTrue(entered.wait(3), 'Request never passed the ordinary route authorization')
                self.app.identity.remove_project_member(self.admin['id'], space_id, self.alice['id'])
                connection.send(content[1:])
                response = connection.getresponse()
                self.assertIn(response.status, (403, 404), response.read())
            finally:
                connection.close()
        self.login()
        self.assertEqual(self.request(self.path(space_id, f'/{kb["id"]}/documents'))[1]['items'], [])

    def test_cross_base_delete_is_rejected_before_a_document_is_mutated(self):
        login = self.login()
        space_id = self.project['id']
        first = self.create_base(login, space_id, 'First library')
        second = self.create_base(login, space_id, 'Second library')
        status, body = self.upload(login, space_id, first['id'], b'Preserve this first-library evidence.\n')
        self.assertEqual(status, 201, body)
        doc_id = body['receipt']['documentId']
        self.ready(space_id, first['id'], doc_id)
        status, body = self.request(self.path(space_id, f'/{second["id"]}/documents/{doc_id}'),
                                    csrf=login['csrfToken'], method='DELETE')
        self.assertIn(status, (400, 403, 404), body)
        self.assertEqual(self.request(self.path(space_id, f'/{first["id"]}/documents/{doc_id}/source'))[0], 200)

    def test_incomplete_upload_has_a_total_deadline_and_keeps_the_library_empty(self):
        login = self.login()
        space_id = self.project['id']
        kb = self.create_base(login, space_id)
        query = urlencode({'fileName': 'slow.md', 'mimeType': 'text/markdown'})
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        started = time.monotonic()
        with patch('rag_ime.team.gateway._KNOWLEDGE_UPLOAD_SECONDS', 0.15):
            connection.putrequest('POST', self.path(space_id, f'/{kb["id"]}/documents/import?{query}'))
            for key, value in [('Origin', self.base), ('X-CSRF-Token', login['csrfToken']),
                               ('Cookie', self.cookie_header()), ('Content-Length', '100')]:
                connection.putheader(key, value)
            connection.endheaders()
            connection.send(b'one fragment')
            try:
                response = connection.getresponse()
                result = json.loads(response.read())
                self.assertEqual(response.status, 408, result)
                self.assertEqual(result['errorCode'], 'knowledge_upload_timeout')
                self.assertEqual(result['error'], '文件上传超时，请重试。')
                self.assertLess(time.monotonic() - started, 2)
            finally:
                connection.close()
        self.assertEqual(self.request(self.path(space_id, f'/{kb["id"]}/documents'))[1]['items'], [])

    def test_duplicate_framing_headers_cannot_hide_compression_or_transfer_encoding(self):
        login = self.login()
        space_id = self.project['id']
        kb = self.create_base(login, space_id)
        query = urlencode({'fileName': 'headers.md', 'mimeType': 'text/markdown'})
        for extra, expected in [
            ([('Content-Length', '1')], 400),
            ([('Transfer-Encoding', ''), ('Transfer-Encoding', 'chunked')], 400),
            ([('Content-Encoding', ''), ('Content-Encoding', 'gzip')], 415),
        ]:
            connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
            connection.putrequest('POST', self.path(space_id, f'/{kb["id"]}/documents/import?{query}'))
            for key, value in [('Origin', self.base), ('X-CSRF-Token', login['csrfToken']),
                               ('Cookie', self.cookie_header()), ('Content-Length', '1'), *extra]:
                connection.putheader(key, value)
            connection.endheaders()
            try:
                response = connection.getresponse()
                self.assertEqual(response.status, expected, response.read())
            finally:
                connection.close()
        self.assertEqual(self.request(self.path(space_id, f'/{kb["id"]}/documents'))[1]['items'], [])


if __name__ == '__main__':
    unittest.main()
