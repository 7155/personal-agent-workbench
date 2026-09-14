"""Durable human ownership and revocable execution capabilities for Pi Sessions."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
from pathlib import Path
import secrets
import sqlite3
import time

from ..db import sqlite_connection
from .errors import TeamError
from .identity import TeamIdentityStore


_execution_authority: ContextVar[Callable[[], object] | None] = ContextVar('team_execution_authority', default=None)


@contextmanager
def execution_authority(check: Callable[[], object]):
    token = _execution_authority.set(check)
    try:
        check()
        yield
    finally:
        _execution_authority.reset(token)


def current_execution_authority() -> Callable[[], object] | None:
    return _execution_authority.get()


def execution_session_key(session_id: str) -> str:
    """Stable server-owned disk key, independent of attempt credentials."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def _denied() -> TeamError:
    return TeamError(403, 'execution_not_authorized', 'This execution is no longer authorized')


def _binding(row: sqlite3.Row) -> dict[str, object]:
    return {
        'sessionId': str(row['session_id']), 'spaceId': str(row['space_id']),
        'ownerUserId': str(row['owner_user_id']), 'membershipRevision': int(row['membership_revision']),
        'workspacePath': str(row['workspace_path']), 'audience': str(row['audience']),
        'generation': int(row['generation']), 'active': bool(row['active']),
    }


class TeamGrantStore:
    def __init__(
        self, db_path: Path, identity: TeamIdentityStore, *, now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.identity = identity
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        if self.db_path.resolve() != identity.db_path.resolve():
            raise ValueError('Team grants and identity must share the team metadata database')
        identity.initialize()

    def bind_session(
        self, owner_user_id: str, space_id: str, session_id: str, workspace: Path,
        *, audience: str = 'owner',
    ) -> dict[str, object]:
        scope = self.identity.require_space(owner_user_id, space_id, action='write')
        if audience not in {'owner', 'project'} or not session_id or len(session_id) > 256:
            raise ValueError('Invalid Session binding')
        resolved = workspace.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError('Session workspace must be a directory')
        revision = int(scope['membershipRevision'])
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row, foreign_keys=True) as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM team_session_bindings WHERE session_id=?', (session_id,)).fetchone()
            if old is not None:
                if (old['owner_user_id'], old['space_id'], old['workspace_path']) != (owner_user_id, space_id, str(resolved)):
                    raise TeamError(409, 'session_binding_conflict', 'Session ownership and workspace cannot be replaced')
                if not old['active'] or int(old['membership_revision']) != revision:
                    raise _denied()
                return _binding(old)
            conn.execute(
                'INSERT INTO team_session_bindings(session_id,space_id,owner_user_id,membership_revision,workspace_path,audience,created_at_ms) VALUES(?,?,?,?,?,?,?)',
                (session_id, space_id, owner_user_id, revision, str(resolved), audience, self.now_ms()),
            )
            row = conn.execute('SELECT * FROM team_session_bindings WHERE session_id=?', (session_id,)).fetchone()
            return _binding(row)

    def binding(self, session_id: str, *, check_current: bool = True) -> dict[str, object]:
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            row = conn.execute('SELECT * FROM team_session_bindings WHERE session_id=?', (session_id,)).fetchone()
        if row is None:
            raise _denied()
        result = _binding(row)
        if check_current:
            scope = self.identity.require_space(str(result['ownerUserId']), str(result['spaceId']), action='write')
            if not result['active'] or result['membershipRevision'] != scope['membershipRevision']:
                raise _denied()
        return result

    def require_session(self, actor: str, space_id: str, session_id: str, *, action: str = 'read') -> dict[str, object]:
        scope = self.identity.require_space(actor, space_id, action='read' if action == 'read' else 'write')
        result = self.binding(session_id, check_current=False)
        if result['spaceId'] != space_id:
            raise TeamError(404, 'resource_not_found', 'Session not found in this space')
        owns = result['ownerUserId'] == actor
        if not owns and not (action == 'read' and result['audience'] == 'project') and not (
            action == 'stop' and scope['role'] in {'owner', 'maintainer'}
        ):
            raise TeamError(404, 'resource_not_found', 'Session not found in this space')
        if action == 'write':
            self.binding(session_id)
        return result

    def issue_attempt(self, session_id: str, *, ttl_ms: int = 3_600_000) -> dict[str, object]:
        current = self.binding(session_id)
        if not 1_000 <= ttl_ms <= 86_400_000:
            raise ValueError('Execution lease duration is out of bounds')
        token = secrets.token_urlsafe(48)
        attempt_id = 'attempt_' + secrets.token_urlsafe(24)
        now = self.now_ms()
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM team_session_bindings WHERE session_id=?', (session_id,)).fetchone()
            if row is None or not row['active']:
                raise _denied()
            generation = int(row['generation']) + 1
            conn.execute('UPDATE team_session_bindings SET generation=? WHERE session_id=?', (generation, session_id))
            conn.execute(
                'INSERT INTO team_execution_attempts(id,session_id,generation,token_hash,expires_at_ms,created_at_ms) VALUES(?,?,?,?,?,?)',
                (attempt_id, session_id, generation, hashlib.sha256(token.encode()).hexdigest(), now + ttl_ms, now),
            )
        return {**current, 'attemptId': attempt_id, 'generation': generation, 'token': token, 'expiresAtMs': now + ttl_ms}

    def resolve_token(self, token: str, session_id: str, space_id: str) -> dict[str, object]:
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise _denied()
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            row = conn.execute(
                'SELECT * FROM team_execution_attempts WHERE token_hash=?',
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
        if row is None or row['session_id'] != session_id or row['revoked_at_ms'] is not None or row['expires_at_ms'] <= self.now_ms():
            raise _denied()
        current = self.binding(session_id)
        if current['spaceId'] != space_id or current['generation'] != row['generation']:
            raise _denied()
        return {**current, 'attemptId': str(row['id']), 'expiresAtMs': int(row['expires_at_ms'])}

    def retire_attempt(self, attempt_id: str) -> None:
        with sqlite_connection(self.db_path) as conn:
            conn.execute('UPDATE team_execution_attempts SET revoked_at_ms=? WHERE id=? AND revoked_at_ms IS NULL', (self.now_ms(), attempt_id))

    def revoke_user(self, user_id: str, *, space_id: str | None = None) -> list[str]:
        sql = 'owner_user_id=?' + (' AND space_id=?' if space_id else '')
        params = (user_id, space_id) if space_id else (user_id,)
        with sqlite_connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            ids = [str(row[0]) for row in conn.execute(f'SELECT session_id FROM team_session_bindings WHERE {sql} AND active=1', params)]
            conn.execute(f'UPDATE team_session_bindings SET active=0 WHERE {sql}', params)
        return ids

    def list_bindings(self, space_id: str) -> list[dict[str, object]]:
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            return [_binding(row) for row in conn.execute('SELECT * FROM team_session_bindings WHERE space_id=?', (space_id,))]
