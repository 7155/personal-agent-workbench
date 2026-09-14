from __future__ import annotations

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from rag_ime.team.broker import AttemptBroker, TeamModelBroker, TeamModelConfig
from rag_ime.team.gateway import TeamApplication


class UnixConnection(HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=5)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class TeamBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='paw-broker-', dir='/tmp')
        root = Path(self.tmp.name)
        self.app = TeamApplication(root / 'data', root / 'web')
        self.admin = self.app.identity.bootstrap_admin('admin', 'administrator-password')
        self.space = self.app.identity.list_spaces(self.admin['id'])[0]
        workspace = root / 'workspace'
        workspace.mkdir()
        self.app.grants.bind_session(self.admin['id'], self.space['id'], 'agent:test', workspace)
        self.attempt = self.app.grants.issue_attempt('agent:test')
        self.calls = []
        calls = self.calls

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, self.headers.get('Authorization'), body))
                raw = json.dumps({'choices': [{'message': {'content': 'offline fixture'}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.thread = Thread(target=self.upstream.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()
        config = TeamModelConfig(
            base_url=f'http://127.0.0.1:{self.upstream.server_port}/v1', model='fixture',
            api_key='fixture-upstream-key', allow_loopback_http=True, user_daily_requests=2,
        )
        self.broker = AttemptBroker(self.app, socket_path=root / 'broker.sock', session_id='agent:test',
                                    space_id=self.space['id'], model=TeamModelBroker(self.app, config))

    def tearDown(self):
        self.broker.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join(timeout=3)
        self.app.close()
        self.tmp.cleanup()

    def request(self, path='/v1/chat/completions', body=None, token=None):
        conn = UnixConnection(self.broker.socket_path)
        try:
            conn.request('POST', path, body=json.dumps(body or {'model': 'fixture', 'messages': []}), headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (token or self.attempt['token']),
            })
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_model_has_fixed_endpoint_secret_and_durable_request_quota(self):
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.calls[0][0], '/v1/chat/completions')
        self.assertEqual(self.calls[0][1], 'Bearer fixture-upstream-key')
        self.assertEqual(self.calls[0][2]['max_tokens'], 8192)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(len(self.calls), 2)

    def test_old_generation_and_other_session_payload_cannot_use_the_broker(self):
        code, result = self.request('/api/agent/tool/execute', {'sessionId': 'agent:other', 'tool': 'memory'})
        self.assertEqual(code, 403, result)
        self.app.grants.issue_attempt('agent:test')
        self.assertEqual(self.request()[0], 403)
        self.assertEqual(self.calls, [])

    def test_revocation_blocks_next_model_admission(self):
        self.app.grants.revoke_user(self.admin['id'])
        self.assertEqual(self.request()[0], 403)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
