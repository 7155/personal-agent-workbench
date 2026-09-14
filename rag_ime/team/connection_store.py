"""Durable ownership and Session-scoped grants for external connections.

This module is deliberately narrower than the provider client and OAuth
manager.  It owns the linearizable SQLite decisions about who may create or
revoke a connection, which exact repositories and operations a Session may
use, and whether the current account and membership still authorize that use.
The provider layer receives credentials only after this store has performed
those checks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, cast

from ..db import sqlite_connection
from .errors import TeamError
from .grants import TeamGrantStore
from .identity import TeamIdentityStore
from .secret_vault import TeamSecretVault


try:
    # The provider agent owns this module.  The fallback keeps this store
    # importable while the optional provider extra is absent and mirrors the
    # contract's first provider exactly.
    from .connection_github import GITHUB_OPERATIONS as _PROVIDER_OPERATIONS
except ImportError:  # pragma: no cover - the provider module is optional here.
    _PROVIDER_OPERATIONS = (
        "repo.read",
        "file.read",
        "issues.list",
        "issue.read",
        "issue.create",
        "issue.comment",
    )

try:
    from .connection_github import normalize_repository as _PROVIDER_NORMALIZE_REPOSITORY
except ImportError:  # pragma: no cover - provider may not expose the helper yet.
    _PROVIDER_NORMALIZE_REPOSITORY = None


GITHUB_OPERATIONS = tuple(str(item) for item in _PROVIDER_OPERATIONS)
_OPERATIONS_ORDER = {operation: index for index, operation in enumerate(GITHUB_OPERATIONS)}
_OPERATION_SET = frozenset(GITHUB_OPERATIONS)

_MAX_IDENTIFIER_LENGTH = 256
_MAX_LABEL_LENGTH = 160
_MAX_ACCOUNT_LOGIN_LENGTH = 128
_MAX_REPOSITORIES = 64
_MAX_REPOSITORY_LENGTH = 201
_MAX_OPERATIONS = len(GITHUB_OPERATIONS)
_MAX_GRANT_TTL_SECONDS = 86_400
_MIN_GRANT_TTL_SECONDS = 60
_MAX_CONNECTIONS_PER_OWNER = 256
_MAX_GRANTS_PER_SESSION = 256
_MAX_LIST_CONNECTIONS = 512
_MAX_LIST_GRANTS = 512
_SQLITE_BUSY_TIMEOUT_MS = 5_000
_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)


def _wall_clock_ms() -> int:
    return int(time.time() * 1000)


def _invalid(message: str = "connection input is invalid") -> TeamError:
    return TeamError(400, "connection_invalid_input", message)


def _connection_not_found() -> TeamError:
    # The same response is used for a guessed id and a connection outside the
    # caller's scope, avoiding an ownership oracle.
    return TeamError(404, "connection_not_found", "connection not found")


def _grant_not_found() -> TeamError:
    return TeamError(404, "grant_not_found", "connection grant not found")


def _forbidden(message: str = "connection access is not authorized") -> TeamError:
    return TeamError(403, "forbidden", message)


def _not_authorized() -> TeamError:
    return TeamError(403, "connection_not_authorized", "connection access is no longer authorized")


def _grant_expired() -> TeamError:
    return TeamError(403, "connection_grant_expired", "connection grant has expired")


def _connection_unusable() -> TeamError:
    return TeamError(409, "connection_reconnect_required", "Reconnect the external account before continuing")


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{field} is invalid")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_IDENTIFIER_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
        or "/" in normalized
        or "\\" in normalized
    ):
        raise _invalid(f"{field} is invalid")
    return normalized


def _text(value: object, field: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{field} is invalid")
    normalized = value.strip()
    if (
        (not normalized and not allow_empty)
        or len(normalized) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise _invalid(f"{field} is invalid")
    return normalized


def _scope(value: object) -> str:
    normalized = _text(value, "scope", 16).casefold()
    if normalized not in {"personal", "project"}:
        raise _invalid("scope is invalid")
    return normalized


def _repository(value: object) -> str:
    if _PROVIDER_NORMALIZE_REPOSITORY is not None:
        try:
            normalized_by_provider = _PROVIDER_NORMALIZE_REPOSITORY(value)
        except (TeamError, TypeError, ValueError):
            raise _invalid("repository must be an exact owner/repository name") from None
        if not isinstance(normalized_by_provider, str):
            raise _invalid("repository must be an exact owner/repository name")
        normalized = normalized_by_provider.casefold()
        if (
            not normalized
            or len(normalized) > _MAX_REPOSITORY_LENGTH
            or normalized.count("/") != 1
            or ".." in normalized
        ):
            raise _invalid("repository must be an exact owner/repository name")
        return normalized
    normalized = _text(value, "repository", _MAX_REPOSITORY_LENGTH).casefold()
    if (
        _REPOSITORY_RE.fullmatch(normalized) is None
        or ".." in normalized
        or normalized.endswith(".")
        or normalized.endswith(".lock")
    ):
        raise _invalid("repository must be an exact owner/repository name")
    return normalized


def _repositories(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= _MAX_REPOSITORIES:
        raise _invalid("choose one to sixty-four repositories")
    normalized = {_repository(item) for item in value}
    if len(normalized) != len(value):
        raise _invalid("repositories must be unique")
    return sorted(normalized)


def _operations(value: object, *, allowed: frozenset[str] | None = None) -> list[str]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= _MAX_OPERATIONS:
        raise _invalid("choose supported connection operations")
    chosen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise _invalid("connection operations are invalid")
        operation = item.strip()
        if operation not in _OPERATION_SET or (allowed is not None and operation not in allowed):
            raise _invalid("connection operation is outside the connection scope")
        chosen.add(operation)
    if len(chosen) != len(value):
        raise _invalid("connection operations must be unique")
    return sorted(chosen, key=lambda item: _OPERATIONS_ORDER.get(item, len(_OPERATIONS_ORDER)))


def _json_list(value: object, field: str) -> list[str]:
    if not isinstance(value, str):
        raise TeamError(500, "connection_metadata_invalid", f"stored {field} metadata is invalid")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, UnicodeError):
        raise TeamError(500, "connection_metadata_invalid", f"stored {field} metadata is invalid") from None
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise TeamError(500, "connection_metadata_invalid", f"stored {field} metadata is invalid")
    return [str(item) for item in decoded]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def _public_connection(row: sqlite3.Row, can_manage: bool) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "provider": str(row["provider"]),
        "scope": str(row["scope"]),
        # ``ownerId`` follows the public scope: a personal connection is
        # owned by its human, while a project connection remains owned by the
        # project even after its creating human leaves.
        "ownerId": str(row["space_id"])
        if str(row["scope"]) == "project"
        else str(row["owner_user_id"]),
        "label": str(row["label"]),
        "accountLogin": str(row["account_login"]),
        "repositories": _json_list(row["repositories_json"], "repository"),
        "operations": _json_list(row["operations_json"], "operation"),
        "status": str(row["status"]),
        "revision": int(row["revision"]),
        "canManage": bool(can_manage),
        "createdAtMs": int(row["created_at_ms"]),
    }


def _public_grant(row: sqlite3.Row, status: str | None = None) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "connectionId": str(row["connection_id"]),
        "sessionId": str(row["session_id"]),
        "spaceId": str(row["space_id"]),
        "repository": str(row["repository"]),
        "operations": _json_list(row["operations_json"], "grant operation"),
        "expiresAtMs": int(row["expires_at_ms"]),
        "status": str(status if status is not None else row["status"]),
        "connectionLabel": str(row["connection_label"]),
        "accountLogin": str(row["account_login"]),
    }


class TeamConnectionStore:
    """SQLite-backed connection metadata, encrypted credentials, and grants."""

    def __init__(
        self,
        identity: TeamIdentityStore,
        grants: TeamGrantStore,
        vault_key_path: str | Path,
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if Path(grants.db_path).resolve() != Path(identity.db_path).resolve():
            raise ValueError("connection grants and identity must share the team database")
        self.identity = identity
        self.grants = grants
        self.now_ms = now_ms or _wall_clock_ms
        self.identity.initialize()
        self.key_path = Path(vault_key_path)
        self.vault_key_path = self.key_path
        # A missing key is safe only for a fresh database.  Generating a new
        # key beside an existing encrypted connection would silently strand
        # every credential and make recovery look like a successful startup.
        try:
            self.key_path.lstat()
            create_if_missing = False
        except FileNotFoundError:
            with sqlite_connection(self.identity.db_path) as conn:
                active_connections = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM team_connections WHERE status='active' AND secret_blob IS NOT NULL"
                    ).fetchone()[0]
                )
            if active_connections:
                raise TeamError(
                    503,
                    "connection_vault_key_missing",
                    "connection encryption key is missing; restore the operator key before using stored connections",
                )
            create_if_missing = active_connections == 0
        except OSError:
            create_if_missing = False
        self.vault = TeamSecretVault(self.key_path, create_if_missing=create_if_missing)

    def _now_ms(self) -> int:
        try:
            return int(self.now_ms())
        except (TypeError, ValueError) as exc:
            raise RuntimeError("connection store clock must return milliseconds") from exc

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self.identity.initialize()
        with sqlite_connection(
            self.identity.db_path,
            row_factory=sqlite3.Row,
            foreign_keys=True,
        ) as conn:
            conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn

    def _scope_conn(
        self,
        conn: sqlite3.Connection,
        actor: str,
        space_id: str,
        action: str,
    ) -> dict[str, object]:
        user = self.identity._require_active_user(conn, actor)  # type: ignore[attr-defined]
        space, role, membership_revision = self.identity._space_access(  # type: ignore[attr-defined]
            conn, actor, space_id, action
        )
        current_revision = (
            int(user["authorization_revision"])
            if str(space["kind"]) == "personal"
            else int(membership_revision)
        )
        return {
            "id": str(space["id"]),
            "kind": str(space["kind"]),
            "name": str(space["name"]),
            "role": str(role),
            "membershipRevision": current_revision,
            "authorizationRevision": int(user["authorization_revision"]),
            "userId": actor,
            "displayName": str(user["display_name"] or user["username"]),
        }

    @staticmethod
    def _personal_space_id(conn: sqlite3.Connection, actor: str) -> str:
        row = conn.execute(
            "SELECT id FROM team_spaces WHERE kind='personal' AND owner_user_id=?",
            (actor,),
        ).fetchone()
        if row is None:
            raise TeamError(500, "personal_space_missing", "personal space is unavailable")
        return str(row["id"])

    @staticmethod
    def _login_session_conn(
        conn: sqlite3.Connection,
        actor: str,
        login_session_id: str,
        now: int,
    ) -> None:
        row = conn.execute(
            """
            SELECT s.user_id, s.revoked_at_ms, s.expires_at_ms, u.active
            FROM team_sessions AS s
            JOIN team_users AS u ON u.id = s.user_id
            WHERE s.id = ?
            """,
            (login_session_id,),
        ).fetchone()
        if (
            row is None
            or str(row["user_id"]) != actor
            or row["revoked_at_ms"] is not None
            or int(row["expires_at_ms"]) <= now
            or not bool(row["active"])
        ):
            raise TeamError(401, "connection_login_expired", "sign in again before connecting this account")

    def create_connection(
        self,
        actor: object,
        space_id: object,
        *,
        scope: object,
        label: object,
        repositories: object,
        operations: object,
        account_login: object,
        credentials: object,
        login_session_id: object = "",
        expected_membership_revision: object | None = None,
    ) -> dict[str, object]:
        actor_id = _identifier(actor, "actor_user_id")
        context_space_id = _identifier(space_id, "space_id")
        normalized_scope = _scope(scope)
        normalized_label = _text(label, "label", _MAX_LABEL_LENGTH)
        normalized_repositories = _repositories(repositories)
        normalized_operations = _operations(operations)
        normalized_account_login = _text(account_login, "account_login", _MAX_ACCOUNT_LOGIN_LENGTH)
        if login_session_id is None:
            normalized_login_session_id = ""
        elif isinstance(login_session_id, str):
            normalized_login_session_id = login_session_id.strip()
        else:
            raise _invalid("login session is invalid")
        if len(normalized_login_session_id) > _MAX_IDENTIFIER_LENGTH:
            raise _invalid("login session is invalid")
        if expected_membership_revision is not None and (
            isinstance(expected_membership_revision, bool)
            or not isinstance(expected_membership_revision, int)
            or expected_membership_revision < 0
        ):
            raise _invalid("membership revision is invalid")
        now = self._now_ms()

        with self._db(write=True) as conn:
            context = self._scope_conn(
                conn,
                actor_id,
                context_space_id,
                "manage" if normalized_scope == "project" else "read",
            )
            if normalized_scope == "project":
                if context["kind"] != "project":
                    raise _invalid("project connections require a project space")
                owner_space_id = context_space_id
                can_manage = True
            else:
                # A personal connection may be set up from the owner's
                # personal page or from any project page the owner can see.
                # Its durable ownership is always the immutable personal
                # space, so moving between UI contexts cannot change it.
                owner_space_id = self._personal_space_id(conn, actor_id)
                can_manage = True
            if (
                expected_membership_revision is not None
                and int(context["membershipRevision"]) != expected_membership_revision
            ):
                raise TeamError(
                    409,
                    "connection_membership_changed",
                    "project membership changed while connecting the account",
                )
            if normalized_login_session_id:
                self._login_session_conn(conn, actor_id, normalized_login_session_id, now)
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM team_connections WHERE owner_user_id=? AND scope='personal' AND status <> 'revoked'",
                    (actor_id,),
                ).fetchone()[0]
            )
            if normalized_scope == "personal" and count >= _MAX_CONNECTIONS_PER_OWNER:
                raise TeamError(429, "connection_limit_reached", "connection limit reached")
            if normalized_scope == "project":
                project_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM team_connections WHERE scope='project' AND space_id=? AND status <> 'revoked'",
                        (owner_space_id,),
                    ).fetchone()[0]
                )
                if project_count >= _MAX_CONNECTIONS_PER_OWNER:
                    raise TeamError(429, "connection_limit_reached", "connection limit reached")
            connection_id = _new_id("conn")
            secret_blob = self.vault.seal(
                credentials,
                owner_id=actor_id,
                connection_id=connection_id,
                provider="github",
                version=1,
            )
            try:
                conn.execute(
                    """
                    INSERT INTO team_connections(
                        id, provider, scope, owner_user_id, space_id, label,
                        account_login, repositories_json, operations_json, status,
                        revision, secret_blob, secret_version, refresh_state,
                        refresh_started_at_ms, created_at_ms, updated_at_ms
                    ) VALUES (?, 'github', ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, 1,
                              'idle', NULL, ?, ?)
                    """,
                    (
                        connection_id,
                        normalized_scope,
                        actor_id,
                        owner_space_id,
                        normalized_label,
                        normalized_account_login,
                        json.dumps(normalized_repositories, separators=(",", ":")),
                        json.dumps(normalized_operations, separators=(",", ":")),
                        secret_blob,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise TeamError(500, "connection_creation_failed", "connection could not be created") from None
            row = conn.execute(
                "SELECT * FROM team_connections WHERE id=?", (connection_id,)
            ).fetchone()
            assert row is not None
            return _public_connection(row, can_manage)

    def list_connections(self, actor: object, space_id: object) -> list[dict[str, object]]:
        actor_id = _identifier(actor, "actor_user_id")
        normalized_space_id = _identifier(space_id, "space_id")
        with self._db() as conn:
            context = self._scope_conn(conn, actor_id, normalized_space_id, "read")
            if context["kind"] == "personal":
                rows = conn.execute(
                    "SELECT * FROM team_connections WHERE scope='personal' AND space_id=? ORDER BY created_at_ms DESC, id DESC LIMIT ?",
                    (normalized_space_id, _MAX_LIST_CONNECTIONS),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM team_connections
                    WHERE (scope='project' AND space_id=?)
                       OR (scope='personal' AND owner_user_id=?)
                    ORDER BY created_at_ms DESC, id DESC
                    LIMIT ?
                    """,
                    (normalized_space_id, actor_id, _MAX_LIST_CONNECTIONS),
                ).fetchall()
            return [
                _public_connection(
                    row,
                    str(row["owner_user_id"]) == actor_id
                    if str(row["scope"]) == "personal"
                    else context["role"] in {"owner", "maintainer"},
                )
                for row in rows
            ]

    def _connection_for_revoke(
        self,
        conn: sqlite3.Connection,
        actor_id: str,
        context_space_id: str,
        connection_id: str,
    ) -> tuple[sqlite3.Row, bool]:
        row = conn.execute(
            "SELECT * FROM team_connections WHERE id=?", (connection_id,)
        ).fetchone()
        if row is None:
            raise _connection_not_found()
        if str(row["scope"]) == "project":
            context = self._scope_conn(conn, actor_id, context_space_id, "manage")
            if context["kind"] != "project" or str(row["space_id"]) != context_space_id:
                raise _connection_not_found()
            return cast(sqlite3.Row, row), True
        # Personal connections can be revoked from the owner's personal page
        # or a visible project page, but only by their immutable owner.
        if str(row["owner_user_id"]) != actor_id:
            raise _connection_not_found()
        self._scope_conn(conn, actor_id, context_space_id, "read")
        return cast(sqlite3.Row, row), True

    def revoke_connection(
        self,
        actor: object,
        space_id: object,
        connection_id: object,
    ) -> dict[str, object]:
        actor_id = _identifier(actor, "actor_user_id")
        normalized_space_id = _identifier(space_id, "space_id")
        normalized_connection_id = _identifier(connection_id, "connection_id")
        now = self._now_ms()
        with self._db(write=True) as conn:
            row, can_manage = self._connection_for_revoke(
                conn, actor_id, normalized_space_id, normalized_connection_id
            )
            if str(row["status"]) != "revoked":
                conn.execute(
                    """
                    UPDATE team_connections
                    SET status='revoked', revision=revision+1, secret_blob=NULL,
                        refresh_state='idle', refresh_started_at_ms=NULL, updated_at_ms=?
                    WHERE id=? AND status <> 'revoked'
                    """,
                    (now, normalized_connection_id),
                )
                conn.execute(
                    """
                    UPDATE team_connection_grants
                    SET status='revoked', revoked_at_ms=?
                    WHERE connection_id=? AND status='active'
                    """,
                    (now, normalized_connection_id),
                )
            current = conn.execute(
                "SELECT * FROM team_connections WHERE id=?", (normalized_connection_id,)
            ).fetchone()
            assert current is not None
            return _public_connection(current, can_manage)

    def _session_binding_conn(
        self,
        conn: sqlite3.Connection,
        actor_id: str,
        space_id: str,
        session_id: str,
        *,
        expected_revision: int,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM team_session_bindings WHERE session_id=?", (session_id,)
        ).fetchone()
        if (
            row is None
            or not bool(row["active"])
            or str(row["owner_user_id"]) != actor_id
            or str(row["space_id"]) != space_id
        ):
            raise _forbidden("connection grants can target only your own Session")
        # Both project tombstones and personal account authorization revisions
        # are part of the Session binding.  A stale binding must not be
        # treated as a new grant after disable/re-enable.
        if int(row["membership_revision"]) != expected_revision:
            raise _not_authorized()
        return cast(sqlite3.Row, row)

    def _grant_membership_revision(
        self,
        conn: sqlite3.Connection,
        actor_id: str,
        space_id: str,
    ) -> tuple[dict[str, object], int]:
        context = self._scope_conn(conn, actor_id, space_id, "write")
        return context, int(context["membershipRevision"])

    def create_grant(
        self,
        actor: object,
        space_id: object,
        *,
        connection_id: object,
        session_id: object,
        repository: object,
        operations: object,
        ttl_seconds: object = 28_800,
    ) -> dict[str, object]:
        actor_id = _identifier(actor, "actor_user_id")
        normalized_space_id = _identifier(space_id, "space_id")
        normalized_connection_id = _identifier(connection_id, "connection_id")
        normalized_session_id = _identifier(session_id, "session_id")
        normalized_repository = _repository(repository)
        now = self._now_ms()
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise _invalid("grant duration is invalid")
        if not _MIN_GRANT_TTL_SECONDS <= ttl_seconds <= _MAX_GRANT_TTL_SECONDS:
            raise _invalid("grant duration is outside the allowed range")

        with self._db(write=True) as conn:
            context, membership_revision = self._grant_membership_revision(
                conn, actor_id, normalized_space_id
            )
            connection = conn.execute(
                "SELECT * FROM team_connections WHERE id=?", (normalized_connection_id,)
            ).fetchone()
            if connection is None:
                raise _connection_not_found()
            if str(connection["status"]) != "active":
                raise _connection_unusable()
            if str(connection["scope"]) == "project":
                if context["kind"] != "project" or str(connection["space_id"]) != normalized_space_id:
                    raise _connection_not_found()
            elif str(connection["owner_user_id"]) != actor_id:
                raise _connection_not_found()
            connection_repositories = set(_json_list(connection["repositories_json"], "repository"))
            if normalized_repository not in connection_repositories:
                raise _invalid("repository is outside the connection allowlist")
            chosen_operations = _operations(
                operations,
                allowed=frozenset(_json_list(connection["operations_json"], "operation")),
            )
            self._session_binding_conn(
                conn,
                actor_id,
                normalized_space_id,
                normalized_session_id,
                expected_revision=membership_revision,
            )
            self._expire_grants_conn(conn, now, session_id=normalized_session_id)
            existing_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM team_connection_grants WHERE session_id=? AND status='active'",
                    (normalized_session_id,),
                ).fetchone()[0]
            )
            if existing_count >= _MAX_GRANTS_PER_SESSION:
                raise TeamError(429, "grant_limit_reached", "Session grant limit reached")
            grant_id = _new_id("grant")
            try:
                conn.execute(
                    """
                    INSERT INTO team_connection_grants(
                    id, connection_id, session_id, space_id, actor_user_id,
                    repository, operations_json, membership_revision,
                    user_authorization_revision, connection_revision, expires_at_ms,
                    status, created_at_ms,
                    revoked_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL)
                    """,
                    (
                        grant_id,
                        normalized_connection_id,
                        normalized_session_id,
                        normalized_space_id,
                        actor_id,
                        normalized_repository,
                        json.dumps(chosen_operations, separators=(",", ":")),
                        membership_revision,
                        int(context["authorizationRevision"]),
                        int(connection["revision"]),
                        now + ttl_seconds * 1000,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise TeamError(500, "grant_creation_failed", "connection grant could not be created") from None
            row = conn.execute(
                """
                SELECT g.*, c.label AS connection_label, c.account_login
                FROM team_connection_grants AS g
                JOIN team_connections AS c ON c.id=g.connection_id
                WHERE g.id=?
                """,
                (grant_id,),
            ).fetchone()
            assert row is not None
            return _public_grant(row)

    @staticmethod
    def _expire_grants_conn(conn: sqlite3.Connection, now: int, *, session_id: str | None = None) -> None:
        if session_id is None:
            conn.execute(
                "UPDATE team_connection_grants SET status='expired' WHERE status='active' AND expires_at_ms <= ?",
                (now,),
            )
        else:
            conn.execute(
                "UPDATE team_connection_grants SET status='expired' WHERE status='active' AND expires_at_ms <= ? AND session_id=?",
                (now, session_id),
            )

    def _current_binding_context_conn(
        self,
        conn: sqlite3.Connection,
        binding: sqlite3.Row,
    ) -> tuple[dict[str, object], int]:
        actor_id = str(binding["owner_user_id"])
        space_id = str(binding["space_id"])
        context = self._scope_conn(conn, actor_id, space_id, "write")
        if not bool(binding["active"]):
            raise _not_authorized()
        current_revision = int(context["membershipRevision"])
        if int(binding["membership_revision"]) != current_revision:
            raise _not_authorized()
        return context, current_revision

    def _grant_row_conn(
        self,
        conn: sqlite3.Connection,
        grant_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT g.*, c.provider, c.scope AS connection_scope, c.owner_user_id AS connection_owner_id,
                   c.space_id AS connection_space_id, c.label AS connection_label,
                   c.account_login, c.repositories_json, c.operations_json AS connection_operations_json,
                   c.status AS connection_status, c.revision AS connection_revision_current,
                   c.secret_blob, c.secret_version, c.refresh_state, c.created_at_ms AS connection_created_at_ms
            FROM team_connection_grants AS g
            JOIN team_connections AS c ON c.id=g.connection_id
            WHERE g.id=?
            """,
            (grant_id,),
        ).fetchone()
        if row is None:
            raise _grant_not_found()
        return cast(sqlite3.Row, row)

    def _authorize_row_conn(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        session_id: str,
        operation: str,
        repository: str,
        now: int,
        *,
        decrypt: bool,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, Any] | None]:
        if str(row["session_id"]) != session_id:
            raise _not_authorized()
        if str(row["status"]) != "active":
            raise _not_authorized()
        if int(row["expires_at_ms"]) <= now:
            conn.execute(
                "UPDATE team_connection_grants SET status='expired' WHERE id=? AND status='active'",
                (str(row["id"]),),
            )
            conn.commit()
            raise _grant_expired()
        binding = conn.execute(
            "SELECT * FROM team_session_bindings WHERE session_id=?", (session_id,)
        ).fetchone()
        if binding is None:
            raise _not_authorized()
        context, current_membership_revision = self._current_binding_context_conn(conn, binding)
        if str(row["actor_user_id"]) != str(binding["owner_user_id"]):
            raise _not_authorized()
        if str(row["space_id"]) != str(binding["space_id"]):
            raise _not_authorized()
        if int(row["membership_revision"]) != current_membership_revision:
            raise _not_authorized()
        if int(row["user_authorization_revision"]) != int(context["authorizationRevision"]):
            raise _not_authorized()
        if str(row["connection_status"]) != "active":
            raise _connection_unusable()
        if str(row["connection_scope"]) == "project" and str(row["connection_space_id"]) != str(row["space_id"]):
            raise _not_authorized()
        if str(row["connection_scope"]) == "personal" and str(row["connection_owner_id"]) != str(row["actor_user_id"]):
            raise _not_authorized()
        if int(row["connection_revision"]) != int(row["connection_revision_current"]):
            raise _not_authorized()
        if str(row["repository"]) != repository:
            raise _not_authorized()
        grant_operations = set(_json_list(row["operations_json"], "grant operation"))
        if operation not in grant_operations:
            raise _not_authorized()
        if operation not in set(_json_list(row["connection_operations_json"], "operation")):
            raise _not_authorized()
        if str(row["repository"]) not in set(_json_list(row["repositories_json"], "repository")):
            raise _not_authorized()
        connection = {
            "id": str(row["connection_id"]),
            "provider": str(row["provider"]),
            "scope": str(row["connection_scope"]),
            "ownerId": str(row["connection_space_id"])
            if str(row["connection_scope"]) == "project"
            else str(row["connection_owner_id"]),
            "label": str(row["connection_label"]),
            "accountLogin": str(row["account_login"]),
            "repositories": _json_list(row["repositories_json"], "repository"),
            "operations": _json_list(row["connection_operations_json"], "operation"),
            "status": str(row["connection_status"]),
            "revision": int(row["connection_revision_current"]),
            "canManage": False,
            "createdAtMs": int(row["connection_created_at_ms"]),
        }
        grant = _public_grant(row)
        credentials: dict[str, Any] | None = None
        if decrypt:
            try:
                credentials = self.vault.open(
                    row["secret_blob"],
                    owner_id=str(row["connection_owner_id"]),
                    connection_id=str(row["connection_id"]),
                    provider=str(row["provider"]),
                    version=int(row["secret_version"]),
                )
            except TeamError as error:
                if error.code == "connection_secret_invalid":
                    conn.execute(
                        """
                        UPDATE team_connections
                        SET status='reconnect_required', secret_blob=NULL,
                            refresh_state='idle', refresh_started_at_ms=NULL, updated_at_ms=?
                        WHERE id=? AND status='active'
                        """,
                        (now, str(row["connection_id"])),
                    )
                    conn.execute(
                        "UPDATE team_connection_grants SET status='revoked', revoked_at_ms=? WHERE connection_id=? AND status='active'",
                        (now, str(row["connection_id"])),
                    )
                    conn.commit()
                raise
        return grant, connection, credentials

    def list_grants(
        self,
        actor: object,
        space_id: object,
        *,
        session_id: object | None = None,
    ) -> list[dict[str, object]]:
        actor_id = _identifier(actor, "actor_user_id")
        normalized_space_id = _identifier(space_id, "space_id")
        normalized_session_id: str | None
        if session_id is None:
            normalized_session_id = None
        else:
            normalized_session_id = _identifier(session_id, "session_id")
        now = self._now_ms()
        with self._db(write=True) as conn:
            self._scope_conn(conn, actor_id, normalized_space_id, "read")
            if normalized_session_id is not None:
                binding = conn.execute(
                    "SELECT * FROM team_session_bindings WHERE session_id=?", (normalized_session_id,)
                ).fetchone()
                if (
                    binding is None
                    or not bool(binding["active"])
                    or str(binding["owner_user_id"]) != actor_id
                    or str(binding["space_id"]) != normalized_space_id
                ):
                    raise _grant_not_found()
            self._expire_grants_conn(conn, now, session_id=normalized_session_id)
            sql = (
                """
                SELECT g.*, c.provider, c.scope AS connection_scope, c.owner_user_id AS connection_owner_id,
                       c.space_id AS connection_space_id, c.label AS connection_label,
                       c.account_login, c.repositories_json, c.operations_json AS connection_operations_json,
                       c.status AS connection_status, c.revision AS connection_revision_current,
                       c.secret_blob, c.secret_version, c.refresh_state,
                       c.created_at_ms AS connection_created_at_ms
                FROM team_connection_grants AS g
                JOIN team_connections AS c ON c.id=g.connection_id
                WHERE g.actor_user_id=? AND g.space_id=?
                """
                + (" AND g.session_id=?" if normalized_session_id is not None else "")
                + " ORDER BY g.created_at_ms DESC, g.id DESC LIMIT ?"
            )
            params: tuple[object, ...] = (
                (actor_id, normalized_space_id, normalized_session_id, _MAX_LIST_GRANTS)
                if normalized_session_id is not None
                else (actor_id, normalized_space_id, _MAX_LIST_GRANTS)
            )
            rows = conn.execute(sql, params).fetchall()
            result: list[dict[str, object]] = []
            for row in rows:
                if str(row["status"]) != "active":
                    result.append(_public_grant(row))
                    continue
                grant_operations = _json_list(row["operations_json"], "grant operation")
                if not grant_operations:
                    conn.execute(
                        "UPDATE team_connection_grants SET status='revoked', revoked_at_ms=? WHERE id=? AND status='active'",
                        (now, str(row["id"])),
                    )
                    result.append(_public_grant(row, "revoked"))
                    continue
                try:
                    grant, _connection, _credentials = self._authorize_row_conn(
                        conn,
                        row,
                        str(row["session_id"]),
                        grant_operations[0],
                        str(row["repository"]),
                        now,
                        decrypt=False,
                    )
                except TeamError as error:
                    if error.code == "connection_secret_invalid":
                        raise
                    if error.code == "connection_grant_expired":
                        result.append(_public_grant(row, "expired"))
                        continue
                    conn.execute(
                        "UPDATE team_connection_grants SET status='revoked', revoked_at_ms=? WHERE id=? AND status='active'",
                        (now, str(row["id"])),
                    )
                    result.append(_public_grant(row, "revoked"))
                else:
                    result.append(grant)
            return result

    def revoke_grant(
        self,
        actor: object,
        space_id: object,
        grant_id: object,
    ) -> dict[str, object]:
        actor_id = _identifier(actor, "actor_user_id")
        normalized_space_id = _identifier(space_id, "space_id")
        normalized_grant_id = _identifier(grant_id, "grant_id")
        now = self._now_ms()
        with self._db(write=True) as conn:
            self._scope_conn(conn, actor_id, normalized_space_id, "read")
            row = conn.execute(
                """
                SELECT g.*, c.label AS connection_label, c.account_login
                FROM team_connection_grants AS g
                JOIN team_connections AS c ON c.id=g.connection_id
                WHERE g.id=? AND g.space_id=? AND g.actor_user_id=?
                """,
                (normalized_grant_id, normalized_space_id, actor_id),
            ).fetchone()
            if row is None:
                raise _grant_not_found()
            if str(row["status"]) == "active":
                if int(row["expires_at_ms"]) <= now:
                    conn.execute(
                        "UPDATE team_connection_grants SET status='expired' WHERE id=? AND status='active'",
                        (normalized_grant_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE team_connection_grants SET status='revoked', revoked_at_ms=? WHERE id=? AND status='active'",
                        (now, normalized_grant_id),
                    )
            current = conn.execute(
                """
                SELECT g.*, c.label AS connection_label, c.account_login
                FROM team_connection_grants AS g
                JOIN team_connections AS c ON c.id=g.connection_id
                WHERE g.id=?
                """,
                (normalized_grant_id,),
            ).fetchone()
            assert current is not None
            return _public_grant(current)

    def authorize(
        self,
        session_id: object,
        grant_id: object,
        operation: object,
        repository: object,
    ) -> dict[str, object]:
        normalized_session_id = _identifier(session_id, "session_id")
        normalized_grant_id = _identifier(grant_id, "grant_id")
        if not isinstance(operation, str) or operation not in _OPERATION_SET:
            raise _invalid("connection operation is invalid")
        normalized_operation = operation
        normalized_repository = _repository(repository)
        now = self._now_ms()
        # The short transaction is a linearization point for current session,
        # membership, grant, connection revision, and secret lookup.  A
        # provider client must still recheck authority after external I/O.
        with self._db(write=True) as conn:
            row = self._grant_row_conn(conn, normalized_grant_id)
            grant, connection, credentials = self._authorize_row_conn(
                conn,
                row,
                normalized_session_id,
                normalized_operation,
                normalized_repository,
                now,
                decrypt=True,
            )
            assert credentials is not None
            return {"grant": grant, "connection": connection, "credentials": credentials}

    def task_grants(self, session_id: object) -> list[dict[str, object]]:
        normalized_session_id = _identifier(session_id, "session_id")
        now = self._now_ms()
        with self._db(write=True) as conn:
            self._expire_grants_conn(conn, now, session_id=normalized_session_id)
            rows = conn.execute(
                """
                SELECT g.*, c.provider, c.scope AS connection_scope, c.owner_user_id AS connection_owner_id,
                       c.space_id AS connection_space_id, c.label AS connection_label,
                       c.account_login, c.repositories_json, c.operations_json AS connection_operations_json,
                       c.status AS connection_status, c.revision AS connection_revision_current,
                       c.secret_blob, c.secret_version, c.refresh_state, c.created_at_ms AS connection_created_at_ms
                FROM team_connection_grants AS g
                JOIN team_connections AS c ON c.id=g.connection_id
                WHERE g.session_id=? AND g.status='active'
                ORDER BY g.created_at_ms, g.id
                LIMIT ?
                """,
                (normalized_session_id, _MAX_GRANTS_PER_SESSION),
            ).fetchall()
            results: list[dict[str, object]] = []
            for row in rows:
                try:
                    grant, _connection, _credentials = self._authorize_row_conn(
                        conn,
                        row,
                        normalized_session_id,
                        str(_json_list(row["operations_json"], "grant operation")[0]),
                        str(row["repository"]),
                        now,
                        decrypt=False,
                    )
                except (TeamError, IndexError):
                    conn.execute(
                        "UPDATE team_connection_grants SET status='revoked', revoked_at_ms=? WHERE id=? AND status='active'",
                        (now, str(row["id"])),
                    )
                    continue
                results.append(grant)
            return results

    def read_credentials(self, connection_id: object) -> dict[str, Any]:
        normalized_connection_id = _identifier(connection_id, "connection_id")
        with self._db(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM team_connections WHERE id=?", (normalized_connection_id,)
            ).fetchone()
            if row is None:
                raise _connection_not_found()
            if str(row["status"]) != "active":
                raise _connection_unusable()
            if str(row["refresh_state"]) == "pending":
                raise TeamError(409, "connection_refresh_in_progress", "connection refresh is already in progress")
            try:
                return self.vault.open(
                    row["secret_blob"],
                    owner_id=str(row["owner_user_id"]),
                    connection_id=str(row["id"]),
                    provider=str(row["provider"]),
                    version=int(row["secret_version"]),
                )
            except TeamError as error:
                if error.code == "connection_secret_invalid":
                    conn.execute(
                        """
                        UPDATE team_connections
                        SET status='reconnect_required', secret_blob=NULL,
                            refresh_state='idle', refresh_started_at_ms=NULL, updated_at_ms=?
                        WHERE id=? AND status='active'
                        """,
                        (self._now_ms(), normalized_connection_id),
                    )
                    conn.execute(
                        "UPDATE team_connection_grants SET status='revoked', revoked_at_ms=? WHERE connection_id=? AND status='active'",
                        (self._now_ms(), normalized_connection_id),
                    )
                    conn.commit()
                raise

    def begin_refresh(self, connection_id: object) -> dict[str, Any]:
        normalized_connection_id = _identifier(connection_id, "connection_id")
        now = self._now_ms()
        with self._db(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM team_connections WHERE id=?", (normalized_connection_id,)
            ).fetchone()
            if row is None:
                raise _connection_not_found()
            if str(row["status"]) != "active" or str(row["refresh_state"]) == "pending":
                raise _connection_unusable()
            try:
                credentials = self.vault.open(
                    row["secret_blob"],
                    owner_id=str(row["owner_user_id"]),
                    connection_id=str(row["id"]),
                    provider=str(row["provider"]),
                    version=int(row["secret_version"]),
                )
            except TeamError:
                conn.execute(
                    """
                    UPDATE team_connections
                    SET status='reconnect_required', secret_blob=NULL,
                        refresh_state='idle', refresh_started_at_ms=NULL, updated_at_ms=?
                    WHERE id=? AND status='active'
                    """,
                    (now, normalized_connection_id),
                )
                conn.execute(
                    "UPDATE team_connection_grants SET status='revoked', revoked_at_ms=? WHERE connection_id=? AND status='active'",
                    (now, normalized_connection_id),
                )
                conn.commit()
                raise
            conn.execute(
                """
                UPDATE team_connections
                SET refresh_state='pending', refresh_started_at_ms=?, updated_at_ms=?
                WHERE id=? AND status='active' AND refresh_state='idle'
                """,
                (now, now, normalized_connection_id),
            )
            return credentials

    def finish_refresh(self, connection_id: object, credentials: object) -> None:
        normalized_connection_id = _identifier(connection_id, "connection_id")
        validated = self.vault.validate(credentials)
        now = self._now_ms()
        with self._db(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM team_connections WHERE id=?", (normalized_connection_id,)
            ).fetchone()
            if row is None:
                raise _connection_not_found()
            if str(row["status"]) == "revoked":
                raise TeamError(409, "connection_revoked", "connection has been revoked")
            if str(row["refresh_state"]) != "pending":
                raise _connection_unusable()
            next_secret_version = int(row["secret_version"]) + 1
            sealed = self.vault.seal(
                validated,
                owner_id=str(row["owner_user_id"]),
                connection_id=str(row["id"]),
                provider=str(row["provider"]),
                version=next_secret_version,
            )
            conn.execute(
                """
                UPDATE team_connections
                SET status='active', secret_blob=?, secret_version=?,
                    refresh_state='idle', refresh_started_at_ms=NULL, updated_at_ms=?
                WHERE id=? AND status <> 'revoked' AND refresh_state='pending'
                """,
                (sealed, next_secret_version, now, normalized_connection_id),
            )

    def cancel_refresh(self, connection_id: object) -> None:
        """Cancel a refresh before provider I/O without invalidating the account.

        The manager calls this only when the pre-dispatch authorization check
        fails.  A pending refresh therefore means no replacement credential was
        sent to the provider yet; returning the durable connection to ``idle``
        preserves its sealed credential, revision, and grants for other
        project members.  A revoke or a previously completed/failed refresh
        wins the race and is left untouched.
        """

        normalized_connection_id = _identifier(connection_id, "connection_id")
        now = self._now_ms()
        with self._db(write=True) as conn:
            row = conn.execute(
                "SELECT status, refresh_state FROM team_connections WHERE id=?",
                (normalized_connection_id,),
            ).fetchone()
            if row is None:
                raise _connection_not_found()
            if str(row["status"]) == "active" and str(row["refresh_state"]) == "pending":
                conn.execute(
                    """
                    UPDATE team_connections
                    SET refresh_state='idle', refresh_started_at_ms=NULL, updated_at_ms=?
                    WHERE id=? AND status='active' AND refresh_state='pending'
                    """,
                    (now, normalized_connection_id),
                )

    def fail_refresh(self, connection_id: object) -> None:
        normalized_connection_id = _identifier(connection_id, "connection_id")
        now = self._now_ms()
        with self._db(write=True) as conn:
            row = conn.execute(
                "SELECT status FROM team_connections WHERE id=?", (normalized_connection_id,)
            ).fetchone()
            if row is None:
                raise _connection_not_found()
            if str(row["status"]) != "revoked":
                conn.execute(
                    """
                    UPDATE team_connections
                    SET status='reconnect_required', secret_blob=NULL,
                        refresh_state='idle', refresh_started_at_ms=NULL, updated_at_ms=?
                    WHERE id=? AND status <> 'revoked'
                    """,
                    (now, normalized_connection_id),
                )
                conn.execute(
                    "UPDATE team_connection_grants SET status='revoked', revoked_at_ms=? WHERE connection_id=? AND status='active'",
                    (now, normalized_connection_id),
                )


__all__ = ["GITHUB_OPERATIONS", "TeamConnectionStore"]
