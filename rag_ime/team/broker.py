"""Per-attempt Unix broker: validate human grants before tools or model I/O."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
import json
import logging
import os
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from threading import BoundedSemaphore, Lock, Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..db import sqlite_connection
from .errors import TeamError
from .grants import execution_authority

_LOGGER = logging.getLogger(__name__)
_MAX_BODY = 2 * 1024 * 1024


@dataclass(frozen=True)
class TeamModelConfig:
    base_url: str
    model: str
    api_key: str = field(default='', repr=False)
    wire_api: str = 'openai-completions'
    user_daily_requests: int = 500
    project_daily_requests: int = 2000
    max_output_tokens: int = 8192
    allow_loopback_http: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        local = self.allow_loopback_http and parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
        if (parsed.scheme != 'https' and not local) or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('Team model base URL must use HTTPS (explicit loopback HTTP is available for local tests)')
        if not self.model or len(self.model) > 128 or any(ord(c) < 32 for c in self.model):
            raise ValueError('A bounded team model identifier is required')
        if self.wire_api not in {'openai-completions', 'openai-responses', 'anthropic-messages'}:
            raise ValueError('Unsupported team model wire API')
        for value in (self.user_daily_requests, self.project_daily_requests, self.max_output_tokens):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000:
                raise ValueError('Team model limits must be positive bounded integers')

    def public(self) -> dict[str, object]:
        return {'model': self.model, 'wireApi': self.wire_api,
                'userDailyRequests': self.user_daily_requests, 'projectDailyRequests': self.project_daily_requests,
                'maxOutputTokens': self.max_output_tokens, 'configured': True}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TeamError(502, 'model_redirect_rejected', 'The configured model service returned a redirect')


class TeamModelBroker:
    def __init__(self, team: Any, config: TeamModelConfig) -> None:
        self.team = team
        self.config = config
        self._lock = Lock()
        self._active: dict[str, BoundedSemaphore] = {}
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def reserve(self, binding: Mapping[str, object]) -> BoundedSemaphore:
        owner, space = str(binding['ownerUserId']), str(binding['spaceId'])
        with self._lock:
            gate = self._active.setdefault(owner, BoundedSemaphore(4))
        if not gate.acquire(blocking=False):
            raise TeamError(429, 'model_concurrency_limit', 'This member already has four active model requests')
        try:
            day = datetime.now(timezone.utc).date().isoformat()
            with sqlite_connection(self.team.identity.db_path) as conn:
                conn.execute('BEGIN IMMEDIATE')
                user_count = conn.execute('SELECT COALESCE(SUM(requests),0) FROM team_model_request_usage WHERE day=? AND owner_user_id=?', (day, owner)).fetchone()[0]
                space_count = conn.execute('SELECT COALESCE(SUM(requests),0) FROM team_model_request_usage WHERE day=? AND space_id=?', (day, space)).fetchone()[0]
                if user_count >= self.config.user_daily_requests or space_count >= self.config.project_daily_requests:
                    raise TeamError(429, 'model_daily_limit', 'The member or space daily model request allowance has been used')
                conn.execute('INSERT INTO team_model_request_usage VALUES(?,?,?,1) ON CONFLICT(day,owner_user_id,space_id) DO UPDATE SET requests=requests+1', (day, owner, space))
            return gate
        except BaseException:
            gate.release()
            raise

    def open(self, path: str, payload: dict[str, object]):
        endpoints = {'openai-completions': '/chat/completions', 'openai-responses': '/responses', 'anthropic-messages': '/messages'}
        endpoint = endpoints[self.config.wire_api]
        if path != '/v1' + endpoint or payload.get('model') != self.config.model:
            raise TeamError(403, 'model_not_allowed', 'The requested model route is outside this execution grant')
        # Model selection, endpoint, credentials and output budget are owned by
        # the operator. Arbitrary tools cannot turn this into an HTTP proxy.
        if self.config.wire_api == 'openai-completions':
            key = 'max_completion_tokens' if 'max_completion_tokens' in payload else 'max_tokens'
        else:
            key = 'max_output_tokens' if self.config.wire_api == 'openai-responses' else 'max_tokens'
        proposed = payload.get(key, self.config.max_output_tokens)
        if isinstance(proposed, bool) or not isinstance(proposed, int) or proposed <= 0:
            raise TeamError(400, 'invalid_model_budget', 'A positive model output budget is required')
        payload[key] = min(proposed, self.config.max_output_tokens)
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if self.config.wire_api == 'anthropic-messages':
            headers.update({'x-api-key': self.config.api_key, 'anthropic-version': '2023-06-01'})
        else:
            headers['Authorization'] = 'Bearer ' + self.config.api_key
        request = Request(self.config.base_url.rstrip('/') + endpoint, data=json.dumps(payload).encode(), headers=headers, method='POST')
        return self.opener.open(request, timeout=90)


class _UnixHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self.slots = BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class AttemptBroker:
    def __init__(self, team: Any, *, socket_path: Path, session_id: str, space_id: str, model: TeamModelBroker) -> None:
        self.team, self.session_id, self.space_id, self.model = team, session_id, space_id, model
        self.socket_path = socket_path
        socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if socket_path.exists() or socket_path.is_symlink():
            raise ValueError('Attempt broker socket already exists')
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def setup(self):
                self.request.settimeout(15)
                super().setup()

            def log_message(self, *_args):
                return

            def do_POST(self):  # noqa: N802
                committed = False
                gate = None
                try:
                    token = self.headers.get('X-RAG-IME-Agent-Token', '')
                    if not token:
                        token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                    if not token:
                        token = self.headers.get('x-api-key', '')
                    def current():
                        return owner.team.grants.resolve_token(token, owner.session_id, owner.space_id)
                    binding = current()
                    length = int(self.headers.get('Content-Length', '-1'))
                    if self.headers.get('Transfer-Encoding') or not 0 <= length <= _MAX_BODY:
                        raise TeamError(413, 'request_too_large', 'Broker request size is invalid')
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise TeamError(400, 'invalid_request', 'A JSON object is required')
                    if self.path.startswith('/api/agent/tool/'):
                        if payload.get('sessionId') != owner.session_id:
                            raise TeamError(403, 'execution_not_authorized', 'The tool Session does not match this attempt')
                        with execution_authority(current):
                            result = owner.tool(self.path, payload, binding)
                            current()
                            self.reply(200, result)
                        return
                    gate = owner.model.reserve(binding)
                    current()
                    with owner.model.open(self.path, payload) as response:
                        self.send_response(response.status)
                        self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        committed = True
                        self.close_connection = True
                        total = 0
                        while chunk := response.read1(16 * 1024):
                            current()
                            total += len(chunk)
                            if total > 32 * 1024 * 1024:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except TeamError as exc:
                    if not committed:
                        self.reply(exc.status, {'ok': False, 'error': exc.message, 'errorCode': exc.code})
                except (ValueError, TypeError, json.JSONDecodeError):
                    if not committed:
                        self.reply(400, {'ok': False, 'error': 'Invalid broker request'})
                except (HTTPError, URLError, TimeoutError):
                    if not committed:
                        self.reply(502, {'ok': False, 'error': 'The configured model service could not complete the request'})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    _LOGGER.exception('Team broker request failed')
                    if not committed:
                        self.reply(500, {'ok': False, 'error': 'Team tool request failed'})
                finally:
                    if gate is not None:
                        gate.release()

            def reply(self, status, value):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True

        self.server = _UnixHTTPServer(str(socket_path), Handler)
        # The socket is mounted into exactly one sandbox. The parent directory
        # remains 0700 and is never mounted, so other local members cannot find it.
        os.chmod(socket_path, 0o666)
        self.thread = Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
        self.thread.start()

    def tool(self, path: str, payload: dict[str, object], binding: Mapping[str, object]) -> dict[str, object]:
        space = self.team.identity.require_space(str(binding['ownerUserId']), self.space_id, action='write')
        service = self.team.service(space)
        handlers = {
            '/api/agent/tool/execute': service.agent_tools.execute,
            '/api/agent/tool/lifecycle-event': service.agent_lifecycle_hooks.record_event,
            '/api/agent/tool/context-refresh': service.agent.refresh_session_context,
            '/api/agent/tool/workflow-state': service.agent.internal_workflow_state,
            '/api/agent/tool/goal-usage': service.agent.record_goal_usage,
            '/api/agent/tool/goal-settle': service.agent.settle_goal_runtime,
            '/api/agent/tool/approval-result': service.agent.approval_result,
        }
        handler = handlers.get(path)
        if handler is None:
            raise TeamError(404, 'route_not_found', 'The broker does not expose this endpoint')
        with service.agent.sessions.as_actor(str(binding['ownerUserId'])):
            return handler(payload)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.socket_path.unlink(missing_ok=True)


__all__ = ['AttemptBroker', 'TeamModelBroker', 'TeamModelConfig']
