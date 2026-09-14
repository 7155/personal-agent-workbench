"""Task-scoped external accounts, OAuth handshakes and durable effect receipts.

Pi remains the only Agent loop. This owner receives calls through the existing
Tool gateway and attempt broker, and never forwards secrets into a worker.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
from threading import BoundedSemaphore, Event, Lock, RLock
import time
from typing import Any

from ..agent_execution_policy import read_only_policy_active
from ..db import sqlite_connection
from .errors import TeamError
from .grants import current_execution_authority


_CREATION_FIELDS = frozenset({'scope', 'label', 'repositories', 'operations'})
_WRITES = frozenset({'issue.create', 'issue.comment'})
_CALLBACK_PATH = '/api/team/connections/github/callback'


def _input_text(value: object, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise TeamError(400, 'connection_invalid_input', 'Connection input has an invalid size or format')
    return value.strip()


@dataclass(repr=False)
class _OAuthFlow:
    actor: str
    login_session_id: str
    space_id: str
    membership_revision: int
    verifier: str = field(repr=False)
    redirect_uri: str
    creation: dict[str, object]
    expires_at_ms: int


class TeamConnections:
    def __init__(self, team: Any, *, config: Mapping[str, object] | None = None, client: Any = None, now_ms=None):
        self.team = team
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.configured = config is not None
        self.store = None
        self.client = None
        self._oauth_enabled = False
        self._flows: dict[str, _OAuthFlow] = {}
        self._flow_lock = RLock()
        self._refresh_locks = tuple(Lock() for _ in range(32))
        self._network_slots = BoundedSemaphore(8)
        self._closed = Event()
        if config is not None:
            from .connection_github import GitHubConnectionClient
            from .connection_store import TeamConnectionStore
            if set(config) - {'githubClientId', 'githubClientSecret', 'vaultKeyFile'}:
                raise ValueError('Unsupported connections configuration field')
            client_id, client_secret = config.get('githubClientId', ''), config.get('githubClientSecret', '')
            if not isinstance(client_id, str) or not isinstance(client_secret, str) or bool(client_id) != bool(client_secret):
                raise ValueError('Connections OAuth configuration requires both client ID and secret')
            key_path = Path(str(config.get('vaultKeyFile') or team.data_root / 'secrets' / 'connections.key')).expanduser()
            if not key_path.is_absolute():
                raise ValueError('Connection vault key path must be absolute')
            self.store = TeamConnectionStore(team.identity, team.grants, key_path, now_ms=self.now_ms)
            self.client = client or GitHubConnectionClient(client_id=client_id, client_secret=client_secret, now_ms=self.now_ms)
            self._oauth_enabled = bool(client_id and client_secret)

    @property
    def oauth_available(self) -> bool:
        return self.configured and self._oauth_enabled and bool(self.team.public_origin)

    def close(self) -> None:
        self._closed.set()
        with self._flow_lock:
            self._flows.clear()

    def _enabled(self) -> None:
        if not self.configured or self._closed.is_set():
            raise TeamError(503, 'connections_unavailable', 'External connections are not configured or are stopping')

    def _provider(self, method: str, *args, **kwargs):
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except TeamError as exc:
            if exc.status == 401:
                # A GitHub token failure is not a PAW login failure. Team's
                # frontend/worker reserve HTTP401 for their own credential.
                raise TeamError(409, exc.code, exc.message) from None
            raise

    def _login(self, actor: str, login_session_id: str) -> None:
        with sqlite_connection(self.team.identity.db_path, row_factory=sqlite3.Row) as conn:
            row = conn.execute(
                'SELECT s.*,u.active FROM team_sessions s JOIN team_users u ON u.id=s.user_id WHERE s.id=?',
                (login_session_id,),
            ).fetchone()
        if row is None or row['user_id'] != actor or not row['active'] or row['revoked_at_ms'] is not None or row['expires_at_ms'] <= self.now_ms():
            raise TeamError(401, 'connection_login_expired', 'Sign in again before connecting this account')

    def _creation(self, actor: str, space_id: str, body: Mapping[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        self._enabled()
        if set(body) != _CREATION_FIELDS or not isinstance(body.get('scope'), str) or body['scope'] not in {'personal', 'project'}:
            raise TeamError(400, 'connection_invalid_input', 'Provide connection ownership, label, repositories and operations')
        scope = self.team.identity.require_space(actor, space_id, action='manage' if body['scope'] == 'project' else 'read')
        if body['scope'] == 'project' and scope['kind'] != 'project':
            raise TeamError(400, 'connection_invalid_input', 'Project connections require a project space')
        from .connection_github import GITHUB_OPERATIONS, normalize_repository
        repositories, operations = body['repositories'], body['operations']
        if not isinstance(repositories, list) or not 1 <= len(repositories) <= 20:
            raise TeamError(400, 'connection_invalid_input', 'Choose up to 20 exact owner/repository names')
        repositories = [normalize_repository(repo) for repo in repositories]
        if not isinstance(operations, list) or not operations or len(operations) > len(GITHUB_OPERATIONS) or any(
            not isinstance(op, str) or op not in GITHUB_OPERATIONS for op in operations
        ):
            raise TeamError(400, 'connection_invalid_input', 'Choose supported connection operations')
        return scope, {'scope': body['scope'], 'label': _input_text(body['label'], 160),
                       'repositories': sorted({repo.lower() for repo in repositories}), 'operations': sorted(set(operations))}

    def _own_sessions(self, actor: str, space_id: str) -> list[dict[str, object]]:
        scope = self.team.identity.require_space(actor, space_id)
        with sqlite_connection(self.team.identity.db_path, row_factory=sqlite3.Row) as conn:
            bindings = conn.execute(
                'SELECT session_id FROM team_session_bindings WHERE owner_user_id=? AND space_id=? AND active=1 '
                'AND membership_revision=? ORDER BY created_at_ms DESC LIMIT 300',
                (actor, space_id, scope['membershipRevision']),
            ).fetchall()
        path = self.team.data_root / 'spaces' / space_id / 'state.sqlite'
        if not bindings or path.is_symlink() or not path.is_file():
            return []
        ids = [str(row['session_id']) for row in bindings]
        # Metadata GET does not construct a service, migrate a database, read
        # a transcript, or start a Pi host.
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT id,title,status FROM agent_sessions WHERE id IN (' + ','.join('?' for _ in ids)
                + ") AND status<>'archived' ORDER BY updated_at_ms DESC LIMIT 100", ids,
            ).fetchall()
        return [{'id': row['id'], 'title': str(row['title'])[:160], 'status': row['status'], 'ownerUserId': actor} for row in rows]

    def list(self, actor: str, space_id: str) -> dict[str, object]:
        scope = self.team.identity.require_space(actor, space_id)
        result = {
            'configured': self.configured, 'oauthAvailable': self.oauth_available,
            'items': self.store.list_connections(actor, space_id) if self.store is not None else [],
            'grants': self.store.list_grants(actor, space_id) if self.store is not None else [],
            'sessions': self._own_sessions(actor, space_id),
            'canCreateProject': scope['kind'] == 'project' and scope['role'] in {'owner', 'maintainer'},
        }
        self.team.identity.require_space(actor, space_id)
        return result

    def connect_token(self, actor: str, space_id: str, login_session_id: str, body: Mapping[str, object]) -> dict[str, object]:
        creation = {key: value for key, value in body.items() if key != 'token'}
        scope, creation = self._creation(actor, space_id, creation)
        token = _input_text(body.get('token'), 8192)
        self._login(actor, login_session_id)
        credentials = {'accessToken': token, 'tokenType': 'bearer'}
        with self._network_slots:
            self._enabled()
            self._login(actor, login_session_id)
            account = self._provider('account', credentials)
        self._enabled()
        self._login(actor, login_session_id)
        return self.store.create_connection(actor, space_id, **creation, account_login=account['login'],
                                            credentials=credentials, login_session_id=login_session_id,
                                            expected_membership_revision=scope['membershipRevision'])

    def start_oauth(self, actor: str, space_id: str, login_session_id: str, body: Mapping[str, object]) -> str:
        scope, creation = self._creation(actor, space_id, body)
        if not self.oauth_available:
            raise TeamError(503, 'connection_oauth_unavailable', 'Configure the GitHub OAuth application and public server origin')
        self._login(actor, login_session_id)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')
        redirect_uri = self.team.public_origin + _CALLBACK_PATH
        authorization_url = self.client.authorization_url(redirect_uri=redirect_uri, state=state, code_challenge=challenge)
        now = self.now_ms()
        with self._flow_lock:
            self._flows = {key: flow for key, flow in self._flows.items() if flow.expires_at_ms > now}
            if len(self._flows) >= 128 or sum(flow.login_session_id == login_session_id for flow in self._flows.values()) >= 8:
                raise TeamError(429, 'connection_oauth_busy', 'Finish an existing account connection before starting another')
            self._flows[hashlib.sha256(state.encode()).hexdigest()] = _OAuthFlow(
                actor, login_session_id, space_id, int(scope['membershipRevision']), verifier,
                redirect_uri, creation, now + 600_000,
            )
        return authorization_url

    def finish_oauth(self, actor: str, login_session_id: str, query: Mapping[str, list[str]]) -> None:
        self._enabled()
        if set(query) - {'state', 'code', 'error', 'error_description', 'error_uri'} or any(len(value) != 1 for value in query.values()):
            raise TeamError(400, 'connection_oauth_invalid', 'Account connection could not be completed')
        state = _input_text(query.get('state', [''])[0], 128)
        with self._flow_lock:
            flow = self._flows.pop(hashlib.sha256(state.encode()).hexdigest(), None)
        if flow is None or flow.expires_at_ms <= self.now_ms() or flow.actor != actor or flow.login_session_id != login_session_id:
            raise TeamError(403, 'connection_oauth_invalid', 'Restart the account connection from your signed-in PAW window')
        if 'error' in query:
            raise TeamError(400, 'connection_oauth_declined', 'Account connection was not approved')
        code = _input_text(query.get('code', [''])[0], 4096)

        def current():
            self._enabled()
            self._login(actor, login_session_id)
            scope, _ = self._creation(actor, flow.space_id, flow.creation)
            if int(scope['membershipRevision']) != flow.membership_revision:
                raise TeamError(403, 'connection_oauth_invalid', 'Project access changed during account connection')

        current()
        with self._network_slots:
            current()
            credentials = self._provider('exchange_code', code=code, verifier=flow.verifier, redirect_uri=flow.redirect_uri)
            current()
            account = self._provider('account', credentials)
        current()
        self.store.create_connection(actor, flow.space_id, **flow.creation, account_login=account['login'],
                                     credentials=credentials, login_session_id=login_session_id,
                                     expected_membership_revision=flow.membership_revision)

    def create_grant(self, actor: str, space_id: str, body: Mapping[str, object]) -> dict[str, object]:
        self._enabled()
        required = {'connectionId', 'sessionId', 'repository', 'operations'}
        if not required <= set(body) or set(body) - required - {'ttlSeconds'}:
            raise TeamError(400, 'connection_invalid_input', 'Provide the connection, task, repository and operations')
        self.team.grants.require_session(actor, space_id, str(body['sessionId']), action='write')
        session = self.team.service(self.team.identity.require_space(actor, space_id)).agent.sessions.get(str(body['sessionId']))
        if session['status'] == 'archived':
            raise TeamError(409, 'connection_task_closed', 'Choose an active task for this connection')
        operations = body['operations']
        if read_only_policy_active(session) and isinstance(operations, list) and any(op in _WRITES for op in operations):
            raise TeamError(403, 'connection_read_only', 'A read-only task cannot receive external write operations')
        return self.store.create_grant(actor, space_id, connection_id=body['connectionId'], session_id=body['sessionId'],
                                       repository=body['repository'], operations=operations, ttl_seconds=body.get('ttlSeconds', 28800))

    def _execution(self, session_id: str):
        self._enabled()
        authority = current_execution_authority()
        if authority is None:
            raise TeamError(403, 'execution_not_authorized', 'Connections require an active task execution')
        authority()
        binding = self.team.grants.binding(session_id)
        return authority, binding

    def _credentials(self, authorized, current):
        credentials = authorized['credentials']
        if int(credentials.get('expiresAtMs') or self.now_ms() + 120_000) > self.now_ms() + 30_000:
            return credentials
        connection_id = authorized['connection']['id']
        lock = self._refresh_locks[int(hashlib.sha256(connection_id.encode()).hexdigest(), 16) % len(self._refresh_locks)]
        with lock:
            current()
            credentials = self.store.read_credentials(connection_id)
            if int(credentials.get('expiresAtMs') or self.now_ms() + 120_000) > self.now_ms() + 30_000:
                return credentials
            if not credentials.get('refreshToken') or int(credentials.get('refreshExpiresAtMs') or self.now_ms() + 1) <= self.now_ms():
                self.store.fail_refresh(connection_id)
                raise TeamError(409, 'connection_reconnect_required', 'Reconnect the external account before continuing')
            credentials = self.store.begin_refresh(connection_id)
            dispatched = False
            try:
                with self._network_slots:
                    current()
                    dispatched = True
                    refreshed = self._provider('refresh_token', credentials)
                    account = self._provider('account', refreshed)
                if account['login'].casefold() != authorized['connection']['accountLogin'].casefold():
                    raise TeamError(409, 'connection_reconnect_required', 'Reconnect the external account before continuing')
                self.store.finish_refresh(connection_id, refreshed)
            except Exception:
                if not dispatched:
                    self.store.cancel_refresh(connection_id)
                    raise
                self.store.fail_refresh(connection_id)
                raise TeamError(409, 'connection_reconnect_required', 'Account refresh did not complete; reconnect before continuing') from None
            # A single task losing access must not invalidate a successfully
            # rotated project credential still authorized for other members.
            current()
            return refreshed

    def _reserve(self, session_id: str, request_id: str, grant_id: str, operation: str, arguments: Mapping[str, object]):
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        with sqlite_connection(self.team.identity.db_path, row_factory=sqlite3.Row) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM team_connection_operations WHERE session_id=? AND request_id=?', (session_id, request_id)).fetchone()
            if row is not None:
                if row['arguments_sha256'] != digest or row['grant_id'] != grant_id or row['operation'] != operation:
                    raise TeamError(409, 'connection_request_conflict', 'This operation ID is already bound to different inputs')
                if row['status'] == 'succeeded':
                    return json.loads(row['result_json'])
                if row['status'] == 'rejected':
                    raise TeamError(409, row['error_code'] or 'connection_provider_rejected', 'The previous external request was rejected; review before using a new operation ID')
                raise TeamError(409, 'connection_outcome_unknown', 'The previous external result is unknown; inspect the service before submitting a new operation')
            now = self.now_ms()
            conn.execute('INSERT INTO team_connection_operations VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (session_id, request_id, grant_id, operation, digest, 'pending', None, None, now, now))
        return None

    def _finish(self, session_id, request_id, *, result=None, error=None):
        status = 'succeeded' if error is None else 'unknown' if error == 'connection_outcome_unknown' else 'rejected'
        with sqlite_connection(self.team.identity.db_path) as conn:
            conn.execute('UPDATE team_connection_operations SET status=?,result_json=?,error_code=?,updated_at_ms=? WHERE session_id=? AND request_id=? AND status=?',
                         (status, json.dumps(result, ensure_ascii=False) if error is None else None, error, self.now_ms(), session_id, request_id, 'pending'))

    @staticmethod
    def _write_result(operation: str, repository: str, args: Mapping[str, object], result: Mapping[str, object]):
        # The ordinary transcript already owns submitted text. Effect receipts
        # keep only an external identifier and a deterministic resource link,
        # never a copy of the Issue/comment body, title, or provider headers.
        key = 'number' if operation == 'issue.create' else 'id'
        identifier = result.get(key)
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1:
            raise TeamError(502, 'connection_outcome_unknown', 'The external write result did not contain a verifiable identifier')
        number = identifier if operation == 'issue.create' else args['number']
        url = f'https://github.com/{repository.lower()}/issues/{number}'
        if operation == 'issue.comment':
            url += f'#issuecomment-{identifier}'
        return {key: identifier, 'htmlUrl': url}

    def execute(self, session_id: str, operation: str, args: Mapping[str, object], *, tool_call_id: str, current_policy=None) -> dict[str, object]:
        authority, _ = self._execution(session_id)
        def policy():
            if current_policy is not None:
                try:
                    current_policy()
                except ValueError:
                    raise TeamError(403, 'connection_task_policy_changed', 'The current task no longer authorizes this connection operation') from None
        policy()
        from .connection_github import GITHUB_OPERATIONS
        if operation == 'list':
            if args:
                raise TeamError(400, 'connection_invalid_input', 'Listing task connections does not accept filters')
            result = {'items': self.store.task_grants(session_id)}
            self._execution(session_id)
            policy()
            return result
        if operation not in GITHUB_OPERATIONS:
            raise TeamError(400, 'connection_invalid_input', 'Unsupported external operation')
        grant_id, repository = _input_text(args.get('grantId')), _input_text(args.get('repository'))
        def current():
            self._execution(session_id)
            authority()
            policy()
            return self.store.authorize(session_id, grant_id, operation, repository)
        authorized = current()
        credentials = self._credentials(authorized, current)
        provider_args = {key: value for key, value in args.items() if key not in {'grantId', 'repository', 'requestId'}}
        write = operation in _WRITES
        request_id = ''
        if write:
            request_id = _input_text(args.get('requestId'), 128)
            previous = self._reserve(session_id, request_id, grant_id, operation, args)
            if previous is not None:
                current()
                return previous
        elif 'requestId' in args:
            raise TeamError(400, 'connection_invalid_input', 'Read operations do not accept a write operation ID')
        try:
            with self._network_slots:
                current()
                result = self._provider('execute', credentials, operation, repository, provider_args)
            if write:
                result = self._write_result(operation, repository, provider_args, result)
                self._finish(session_id, request_id, result=result)
        except TeamError as exc:
            if exc.code == 'connection_reconnect_required':
                self.store.fail_refresh(authorized['connection']['id'])
            if write:
                self._finish(session_id, request_id, error=exc.code)
            raise
        except Exception:
            if write:
                self._finish(session_id, request_id, error='connection_outcome_unknown')
            raise TeamError(502, 'connection_outcome_unknown', 'The external request result could not be verified') from None
        current()
        return result


__all__ = ['TeamConnections']
