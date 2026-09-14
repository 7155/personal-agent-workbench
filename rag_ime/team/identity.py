"""SQLite-backed team identity, sessions, and project membership.

This module owns the small identity boundary used by a single team server.  It
deliberately uses its own migration directory and database path; the personal
RAG/Trace database is never opened by this store.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from ..db import apply_database_migrations, sqlite_connection
from .errors import TeamError


_MIGRATIONS_DIR = Path(__file__).with_name("migrations")

_ACCOUNT_ROLES = frozenset({"admin", "member"})
_PROJECT_ROLES = frozenset({"owner", "maintainer", "contributor", "viewer"})
_ACTIONS = frozenset({"read", "write", "manage"})
_WRITE_ROLES = frozenset({"owner", "maintainer", "contributor"})
_MANAGE_ROLES = frozenset({"owner", "maintainer"})

_MAX_USERNAME_LENGTH = 64
_MAX_PASSWORD_LENGTH = 1_024
_MIN_PASSWORD_LENGTH = 8
_MAX_DISPLAY_NAME_LENGTH = 128
_MAX_PROJECT_NAME_LENGTH = 160
_MAX_IDENTIFIER_LENGTH = 256
_MAX_TOKEN_LENGTH = 512

_LOGIN_FAILURE_LIMIT = 5
_LOCKOUT_MS = 30_000
_DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
_MAX_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
_SQLITE_BUSY_TIMEOUT_MS = 5_000

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 5
_SCRYPT_DKLEN = 64
_SCRYPT_SALT_BYTES = 16
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_PASSWORD_HASH_SLOTS = 4
_PASSWORD_HASH_SEMAPHORE = threading.BoundedSemaphore(_PASSWORD_HASH_SLOTS)


def _wall_clock_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    """Generate a non-sequential server-owned identifier."""

    return f"{prefix}_{secrets.token_urlsafe(24)}"


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode_password(password: str, *, salt: bytes | None = None) -> str:
    chosen_salt = secrets.token_bytes(_SCRYPT_SALT_BYTES) if salt is None else salt
    if len(chosen_salt) != _SCRYPT_SALT_BYTES:
        raise ValueError("password salt must be exactly 16 bytes")
    with _PASSWORD_HASH_SEMAPHORE:
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=chosen_salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_SCRYPT_DKLEN,
            maxmem=_SCRYPT_MAXMEM,
        )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(chosen_salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


# A fixed dummy hash keeps unknown-user login attempts on the same password
# verification path without storing or deriving anything from the submitted
# username.  It is never returned or persisted in the team database.
_DUMMY_PASSWORD_HASH = _encode_password("team-dummy-password", salt=b"paw-team-dummy!!")


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, encoded_salt, encoded_digest = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        n = int(raw_n)
        r = int(raw_r)
        p = int(raw_p)
        salt = base64.b64decode(encoded_salt.encode("ascii"), validate=True)
        expected = base64.b64decode(encoded_digest.encode("ascii"), validate=True)
        if not salt or not expected or n <= 1 or r <= 0 or p <= 0:
            return False
        with _PASSWORD_HASH_SEMAPHORE:
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=n,
                r=r,
                p=p,
                dklen=len(expected),
                maxmem=_SCRYPT_MAXMEM,
            )
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected)


def _text(value: object, field: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TeamError(400, "invalid_input", f"{field} must be text")
    if "\x00" in value:
        raise TeamError(400, "invalid_input", f"{field} contains an invalid character")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise TeamError(400, "invalid_input", f"{field} is required")
    if len(normalized) > maximum:
        raise TeamError(400, "invalid_input", f"{field} is too long")
    return normalized


def _username(value: object) -> tuple[str, str]:
    normalized = _text(value, "username", _MAX_USERNAME_LENGTH)
    return normalized, normalized.casefold()


def _password(value: object) -> str:
    if not isinstance(value, str):
        raise TeamError(400, "invalid_input", "password must be text")
    if "\x00" in value or len(value) > _MAX_PASSWORD_LENGTH:
        raise TeamError(400, "invalid_input", "password has an invalid size")
    if len(value) < _MIN_PASSWORD_LENGTH:
        raise TeamError(400, "invalid_input", "password is too short")
    return value


def _identifier(value: object, field: str) -> str:
    return _text(value, field, _MAX_IDENTIFIER_LENGTH)


def _token(value: object, field: str = "token") -> str:
    return _text(value, field, _MAX_TOKEN_LENGTH)


def _role(value: object, *, project: bool = False) -> str:
    allowed = _PROJECT_ROLES if project else _ACCOUNT_ROLES
    normalized = _text(value, "role", 32).casefold()
    if normalized not in allowed:
        raise TeamError(400, "invalid_role", "role is not supported")
    return normalized


def _public_user(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "username": str(row["username"]),
        "displayName": str(row["display_name"]),
        "role": str(row["role"]),
        "active": bool(row["active"]),
    }


def _public_space(
    row: sqlite3.Row,
    role: str,
    membership_revision: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(row["id"]),
        "kind": str(row["kind"]),
        "name": str(row["name"]),
        "role": role,
        "revision": int(row["revision"]),
    }
    if membership_revision is not None:
        payload["membershipRevision"] = int(membership_revision)
    return payload


def _public_session(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "createdAtMs": int(row["created_at_ms"]),
        "expiresAtMs": int(row["expires_at_ms"]),
    }


def _invalid_credentials() -> TeamError:
    # Unknown, inactive, locked, and wrong-password cases intentionally share
    # one response so login cannot be used as a username/account probe.
    return TeamError(401, "invalid_credentials", "username or password is invalid")


def _space_not_found() -> TeamError:
    # A missing ID and an existing space outside the caller's membership have
    # the same result, avoiding an authorization oracle.
    return TeamError(404, "space_not_found", "space not found")


class TeamIdentityStore:
    """Authoritative identity and membership store for one team server."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], int] | None = None,
        session_ttl_seconds: int = _DEFAULT_SESSION_TTL_SECONDS,
    ) -> None:
        if not isinstance(session_ttl_seconds, int) or not 1 <= session_ttl_seconds <= _MAX_SESSION_TTL_SECONDS:
            raise ValueError(
                f"session_ttl_seconds must be between 1 and {_MAX_SESSION_TTL_SECONDS}"
            )
        self.db_path = Path(db_path)
        self._clock = clock or _wall_clock_ms
        self._session_ttl_ms = session_ttl_seconds * 1000

    def initialize(self) -> int:
        """Create or verify only this store's schema and return its version."""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite_connection(self.db_path, foreign_keys=True) as conn:
            conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            conn.execute("BEGIN IMMEDIATE")
            return int(apply_database_migrations(conn, migrations_dir=_MIGRATIONS_DIR).current_version)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self.initialize()
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row, foreign_keys=True) as conn:
            conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn

    def _now_ms(self) -> int:
        value = self._clock()
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("team identity clock must return milliseconds") from exc

    def bootstrap_admin(
        self,
        username: object,
        password: object,
        display_name: object = "",
    ) -> dict[str, object]:
        normalized_username, username_key = _username(username)
        normalized_password = _password(password)
        normalized_display_name = _text(
            display_name,
            "display_name",
            _MAX_DISPLAY_NAME_LENGTH,
            allow_empty=True,
        )
        now = self._now_ms()
        with self._connection(write=True) as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM team_users").fetchone()[0])
            if count:
                raise TeamError(409, "bootstrap_already_complete", "team bootstrap is already complete")
            user_id = _new_id("usr")
            try:
                conn.execute(
                    """
                    INSERT INTO team_users(
                        id, username, username_key, password_hash, display_name, role,
                        active, failed_login_count, locked_until_ms, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, 'admin', 1, 0, 0, ?, ?)
                    """,
                    (
                        user_id,
                        normalized_username,
                        username_key,
                        _encode_password(normalized_password),
                        normalized_display_name,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "username_key" in str(exc):
                    raise TeamError(409, "username_taken", "username is already in use") from None
                raise
            self._insert_personal_space(conn, user_id, normalized_username, now)
            row = conn.execute("SELECT * FROM team_users WHERE id = ?", (user_id,)).fetchone()
            assert row is not None
            return _public_user(row)

    def login(self, username: object, password: object) -> dict[str, object]:
        _normalized_username, username_key = _username(username)
        normalized_password = _password(password)
        # Password work happens outside the SQLite write transaction.  This
        # keeps a bounded scrypt slot from also holding the team metadata lock
        # while another login waits for a hashing slot.
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM team_users WHERE username_key = ?",
                (username_key,),
            ).fetchone()
            if row is None:
                _verify_password(normalized_password, _DUMMY_PASSWORD_HASH)
                raise _invalid_credentials()
            stored_hash = str(row["password_hash"])
            password_hash = str(row["password_hash"])
        valid_password = _verify_password(normalized_password, password_hash)

        with self._connection(write=True) as conn:
            # Re-read account state after hashing.  Disable/lockout changes
            # racing with the hash must win over the earlier read snapshot.
            row = conn.execute(
                "SELECT * FROM team_users WHERE username_key = ?",
                (username_key,),
            ).fetchone()
            if row is None:
                raise _invalid_credentials()
            now = self._now_ms()
            locked = int(row["locked_until_ms"]) > now
            active = bool(row["active"])
            if str(row["password_hash"]) != password_hash:
                valid_password = _verify_password(normalized_password, str(row["password_hash"]))
            if not valid_password or locked or not active:
                if active and not locked and not valid_password:
                    failures = min(int(row["failed_login_count"]) + 1, _LOGIN_FAILURE_LIMIT)
                    locked_until = now + _LOCKOUT_MS if failures >= _LOGIN_FAILURE_LIMIT else 0
                    conn.execute(
                        """
                        UPDATE team_users
                        SET failed_login_count = ?, locked_until_ms = ?, updated_at_ms = ?
                        WHERE id = ?
                        """,
                        (failures, locked_until, now, str(row["id"])),
                    )
                    # sqlite_connection rolls back on an exception.  Commit
                    # the bounded failure counter before returning the shared
                    # invalid-credentials error so lockout cannot be bypassed
                    # by repeatedly submitting bad passwords.
                    conn.commit()
                raise _invalid_credentials()

            conn.execute(
                """
                UPDATE team_users
                SET failed_login_count = 0, locked_until_ms = 0, updated_at_ms = ?
                WHERE id = ?
                """,
                (now, str(row["id"])),
            )
            token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            session_id = _new_id("sess")
            expires_at = now + self._session_ttl_ms
            conn.execute(
                """
                INSERT INTO team_sessions(
                    id, token_hash, csrf_hash, csrf_token, user_id,
                    created_at_ms, expires_at_ms, revoked_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    session_id,
                    _token_hash(token),
                    _token_hash(csrf_token),
                    csrf_token,
                    str(row["id"]),
                    now,
                    expires_at,
                ),
            )
            current = conn.execute("SELECT * FROM team_users WHERE id = ?", (str(row["id"]),)).fetchone()
            assert current is not None
            session_row = conn.execute(
                "SELECT * FROM team_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            assert session_row is not None
            return {
                "token": token,
                "csrfToken": csrf_token,
                "user": _public_user(current),
                "sessionId": str(session_row["id"]),
                "expiresAtMs": int(session_row["expires_at_ms"]),
                "session": _public_session(session_row),
            }

    def authenticate(self, token: object) -> dict[str, object]:
        normalized_token = _token(token)
        now = self._now_ms()
        with self._connection(write=True) as conn:
            row = conn.execute(
                """
                SELECT s.*, u.id AS user_id, u.username, u.display_name,
                       u.role AS user_role, u.active AS user_active
                FROM team_sessions AS s
                JOIN team_users AS u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (_token_hash(normalized_token),),
            ).fetchone()
            if row is None:
                raise TeamError(401, "authentication_required", "authentication is required")
            if (
                row["revoked_at_ms"] is not None
                or int(row["expires_at_ms"]) <= now
                or not bool(row["user_active"])
                or not str(row["csrf_token"])
            ):
                # Mark an otherwise-live session retired when a disabled user
                # or expired token is first observed.  set_member_active also
                # revokes all sessions eagerly on disable.
                if row["revoked_at_ms"] is None:
                    conn.execute(
                        "UPDATE team_sessions SET revoked_at_ms = ? WHERE id = ? AND revoked_at_ms IS NULL",
                        (now, str(row["id"])),
                    )
                    conn.commit()
                raise TeamError(401, "authentication_required", "authentication is required")
            user = {
                "id": str(row["user_id"]),
                "username": str(row["username"]),
                "displayName": str(row["display_name"]),
                "role": str(row["user_role"]),
                "active": bool(row["user_active"]),
            }
            return {
                "user": user,
                "csrfToken": str(row["csrf_token"]),
                "sessionId": str(row["id"]),
                "expiresAtMs": int(row["expires_at_ms"]),
            }

    def validate_csrf(self, token: object, csrf_token: object) -> bool:
        """Return whether a live session owns the supplied CSRF token."""

        try:
            normalized_token = _token(token)
            normalized_csrf = _token(csrf_token, "csrf_token")
        except TeamError:
            return False
        now = self._now_ms()
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT s.csrf_hash
                FROM team_sessions AS s
                JOIN team_users AS u ON u.id = s.user_id
                WHERE s.token_hash = ?
                  AND s.revoked_at_ms IS NULL
                  AND s.expires_at_ms > ?
                  AND u.active = 1
                """,
                (_token_hash(normalized_token), now),
            ).fetchone()
            return row is not None and hmac.compare_digest(
                str(row["csrf_hash"]), _token_hash(normalized_csrf)
            )

    def require_csrf(self, token: object, csrf_token: object) -> None:
        if not self.validate_csrf(token, csrf_token):
            raise TeamError(403, "csrf_invalid", "CSRF token is invalid")

    def logout(self, token: object) -> dict[str, object]:
        normalized_token = _token(token)
        now = self._now_ms()
        with self._connection(write=True) as conn:
            conn.execute(
                """
                UPDATE team_sessions
                SET revoked_at_ms = COALESCE(revoked_at_ms, ?)
                WHERE token_hash = ?
                """,
                (now, _token_hash(normalized_token)),
            )
        return {"loggedOut": True}

    def create_member(
        self,
        actor_user_id: object,
        username: object,
        password: object,
        display_name: object = "",
        role: object = "member",
    ) -> dict[str, object]:
        actor_id = _identifier(actor_user_id, "actor_user_id")
        normalized_username, username_key = _username(username)
        normalized_password = _password(password)
        normalized_display_name = _text(
            display_name,
            "display_name",
            _MAX_DISPLAY_NAME_LENGTH,
            allow_empty=True,
        )
        normalized_role = _role(role)
        now = self._now_ms()
        with self._connection(write=True) as conn:
            self._require_admin(conn, actor_id)
            user_id = _new_id("usr")
            try:
                conn.execute(
                    """
                    INSERT INTO team_users(
                        id, username, username_key, password_hash, display_name, role,
                        active, failed_login_count, locked_until_ms, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, 0, ?, ?)
                    """,
                    (
                        user_id,
                        normalized_username,
                        username_key,
                        _encode_password(normalized_password),
                        normalized_display_name,
                        normalized_role,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "username_key" in str(exc):
                    raise TeamError(409, "username_taken", "username is already in use") from None
                raise
            self._insert_personal_space(conn, user_id, normalized_username, now)
            row = conn.execute("SELECT * FROM team_users WHERE id = ?", (user_id,)).fetchone()
            assert row is not None
            return _public_user(row)

    def list_members(self, actor_user_id: object) -> list[dict[str, object]]:
        actor_id = _identifier(actor_user_id, "actor_user_id")
        with self._connection() as conn:
            self._require_admin(conn, actor_id)
            rows = conn.execute(
                "SELECT * FROM team_users ORDER BY username_key, id"
            ).fetchall()
            return [_public_user(row) for row in rows]

    def member_directory(self, actor_user_id: object) -> list[dict[str, object]]:
        """Return the active member directory for an authenticated user.

        The administrative ``list_members`` method intentionally exposes
        account state and remains admin-only.  The invite/member picker needs
        a smaller read-only projection that every active account can use; it
        never includes password hashes, session tokens, CSRF material, or
        inactive accounts.
        """

        actor_id = _identifier(actor_user_id, "actor_user_id")
        with self._connection() as conn:
            self._require_active_user(conn, actor_id)
            rows = conn.execute(
                """
                SELECT id, username, display_name
                FROM team_users
                WHERE active = 1
                ORDER BY username_key, id
                """
            ).fetchall()
            return [
                {
                    "id": str(row["id"]),
                    "username": str(row["username"]),
                    "displayName": str(row["display_name"]),
                }
                for row in rows
            ]

    def list_directory(self, actor_user_id: object) -> list[dict[str, object]]:
        """Compatibility alias for callers that name the directory a list."""

        return self.member_directory(actor_user_id)

    def set_member_active(
        self,
        actor_user_id: object,
        target_user_id: object,
        active: object,
    ) -> dict[str, object]:
        actor_id = _identifier(actor_user_id, "actor_user_id")
        target_id = _identifier(target_user_id, "target_user_id")
        if not isinstance(active, bool):
            raise TeamError(400, "invalid_input", "active must be boolean")
        now = self._now_ms()
        with self._connection(write=True) as conn:
            self._require_admin(conn, actor_id)
            target = conn.execute("SELECT * FROM team_users WHERE id = ?", (target_id,)).fetchone()
            if target is None:
                raise TeamError(404, "user_not_found", "user not found")
            currently_active = bool(target["active"])
            if currently_active and not active and str(target["role"]) == "admin":
                admin_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM team_users WHERE active = 1 AND role = 'admin'"
                    ).fetchone()[0]
                )
                if admin_count <= 1:
                    raise TeamError(409, "last_active_admin", "the last active admin cannot be disabled")
            if currently_active != active:
                conn.execute(
                    """
                    UPDATE team_users
                    SET active = ?, authorization_revision = authorization_revision + 1,
                        updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (1 if active else 0, now, target_id),
                )
            else:
                conn.execute(
                    "UPDATE team_users SET active = ?, updated_at_ms = ? WHERE id = ?",
                    (1 if active else 0, now, target_id),
                )
            if not active:
                conn.execute(
                    "UPDATE team_sessions SET revoked_at_ms = COALESCE(revoked_at_ms, ?) WHERE user_id = ?",
                    (now, target_id),
                )
            current = conn.execute("SELECT * FROM team_users WHERE id = ?", (target_id,)).fetchone()
            assert current is not None
            return _public_user(current)

    def list_spaces(self, user_id: object) -> list[dict[str, object]]:
        normalized_user_id = _identifier(user_id, "user_id")
        with self._connection() as conn:
            self._require_active_user(conn, normalized_user_id)
            rows = conn.execute(
                """
                SELECT s.*, 'owner' AS member_role,
                       u.authorization_revision AS membership_revision
                FROM team_spaces AS s
                JOIN team_users AS u ON u.id = s.owner_user_id
                WHERE s.kind = 'personal' AND s.owner_user_id = ?
                UNION ALL
                SELECT s.*, pm.role AS member_role,
                       pm.membership_revision AS membership_revision
                FROM team_spaces AS s
                JOIN team_project_members AS pm
                  ON pm.space_id = s.id AND pm.active = 1
                WHERE s.kind = 'project' AND pm.user_id = ?
                ORDER BY kind, name, id
                """,
                (normalized_user_id, normalized_user_id),
            ).fetchall()
            return [
                _public_space(
                    row,
                    str(row["member_role"]),
                    int(row["membership_revision"]),
                )
                for row in rows
            ]

    def create_project(self, actor_user_id: object, name: object) -> dict[str, object]:
        actor_id = _identifier(actor_user_id, "actor_user_id")
        normalized_name = _text(name, "name", _MAX_PROJECT_NAME_LENGTH)
        now = self._now_ms()
        with self._connection(write=True) as conn:
            self._require_active_user(conn, actor_id)
            space_id = _new_id("space")
            try:
                conn.execute(
                    """
                    INSERT INTO team_spaces(
                        id, kind, name, owner_user_id, revision, created_at_ms, updated_at_ms
                    ) VALUES (?, 'project', ?, ?, 1, ?, ?)
                    """,
                    (space_id, normalized_name, actor_id, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO team_project_members(space_id, user_id, role, added_at_ms)
                    VALUES (?, ?, 'owner', ?)
                    """,
                    (space_id, actor_id, now),
                )
            except sqlite3.IntegrityError:
                # IDs are generated server-side; do not expose raw SQLite
                # details through the team transport on an integrity failure.
                raise TeamError(500, "space_creation_failed", "project could not be created") from None
            row = conn.execute("SELECT * FROM team_spaces WHERE id = ?", (space_id,)).fetchone()
            assert row is not None
            return _public_space(row, "owner", 1)

    def require_space(
        self,
        user_id: object,
        space_id: object,
        action: str = "read",
    ) -> dict[str, object]:
        normalized_user_id = _identifier(user_id, "user_id")
        normalized_space_id = _identifier(space_id, "space_id")
        normalized_action = _text(action, "action", 16).casefold()
        if normalized_action not in _ACTIONS:
            raise TeamError(400, "invalid_action", "action must be read, write, or manage")
        with self._connection() as conn:
            user = self._require_active_user(conn, normalized_user_id)
            _space, role, membership_revision = self._space_access(
                conn,
                normalized_user_id,
                normalized_space_id,
                normalized_action,
            )
            if str(_space["kind"]) == "personal":
                membership_revision = int(user["authorization_revision"])
            return _public_space(_space, role, membership_revision)

    def project_members(
        self,
        actor_user_id: object,
        space_id: object,
    ) -> list[dict[str, object]]:
        actor_id = _identifier(actor_user_id, "actor_user_id")
        normalized_space_id = _identifier(space_id, "space_id")
        with self._connection() as conn:
            self._require_active_user(conn, actor_id)
            space, _role, _membership_revision = self._space_access(
                conn,
                actor_id,
                normalized_space_id,
                "read",
            )
            if str(space["kind"]) != "project":
                raise TeamError(400, "not_project", "space is not a project")
            rows = conn.execute(
                """
                SELECT u.*, pm.role AS member_role, pm.membership_revision
                FROM team_project_members AS pm
                JOIN team_users AS u ON u.id = pm.user_id
                WHERE pm.space_id = ? AND pm.active = 1
                ORDER BY CASE pm.role WHEN 'owner' THEN 0 WHEN 'maintainer' THEN 1
                                      WHEN 'contributor' THEN 2 ELSE 3 END,
                         u.username_key, u.id
                """,
                (normalized_space_id,),
            ).fetchall()
            members: list[dict[str, object]] = []
            for row in rows:
                member = _public_user(row)
                member.update(
                    {
                        "spaceId": normalized_space_id,
                        "role": str(row["member_role"]),
                        "membershipRevision": int(row["membership_revision"]),
                        "revision": int(space["revision"]),
                    }
                )
                members.append(member)
            return members

    def add_project_member(
        self,
        actor_user_id: object,
        space_id: object,
        user_id: object,
        role: object = "contributor",
    ) -> dict[str, object]:
        actor_id = _identifier(actor_user_id, "actor_user_id")
        normalized_space_id = _identifier(space_id, "space_id")
        target_id = _identifier(user_id, "user_id")
        normalized_role = _role(role, project=True)
        if normalized_role == "owner":
            raise TeamError(400, "owner_role_reserved", "project ownership is assigned at creation")
        now = self._now_ms()
        with self._connection(write=True) as conn:
            self._require_active_user(conn, actor_id)
            space, _actor_role, _membership_revision = self._space_access(
                conn,
                actor_id,
                normalized_space_id,
                "manage",
            )
            if str(space["kind"]) != "project":
                raise TeamError(400, "not_project", "space is not a project")
            target = conn.execute("SELECT * FROM team_users WHERE id = ?", (target_id,)).fetchone()
            if target is None:
                raise TeamError(404, "user_not_found", "user not found")
            if not bool(target["active"]):
                raise TeamError(409, "account_disabled", "disabled users cannot join a project")
            existing = conn.execute(
                """
                SELECT role, active, membership_revision
                FROM team_project_members
                WHERE space_id = ? AND user_id = ?
                """,
                (normalized_space_id, target_id),
            ).fetchone()
            if existing is not None and bool(existing["active"]):
                raise TeamError(409, "member_exists", "user is already a project member")
            membership_revision = (
                int(existing["membership_revision"]) + 1 if existing is not None else 1
            )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO team_project_members(
                        space_id, user_id, role, active, membership_revision, added_at_ms
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (normalized_space_id, target_id, normalized_role, membership_revision, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE team_project_members
                    SET role = ?, active = 1, membership_revision = ?, added_at_ms = ?
                    WHERE space_id = ? AND user_id = ? AND active = 0
                    """,
                    (
                        normalized_role,
                        membership_revision,
                        now,
                        normalized_space_id,
                        target_id,
                    ),
                )
            conn.execute(
                "UPDATE team_spaces SET revision = revision + 1, updated_at_ms = ? WHERE id = ?",
                (now, normalized_space_id),
            )
            updated_space = conn.execute(
                "SELECT * FROM team_spaces WHERE id = ?",
                (normalized_space_id,),
            ).fetchone()
            assert updated_space is not None
            member = _public_user(target)
            member.update(
                {
                    "spaceId": normalized_space_id,
                    "role": normalized_role,
                    "membershipRevision": membership_revision,
                    "revision": int(updated_space["revision"]),
                }
            )
            return member

    def remove_project_member(
        self,
        actor_user_id: object,
        space_id: object,
        user_id: object,
    ) -> dict[str, object]:
        actor_id = _identifier(actor_user_id, "actor_user_id")
        normalized_space_id = _identifier(space_id, "space_id")
        target_id = _identifier(user_id, "user_id")
        now = self._now_ms()
        with self._connection(write=True) as conn:
            self._require_active_user(conn, actor_id)
            space, _actor_role, _membership_revision = self._space_access(
                conn,
                actor_id,
                normalized_space_id,
                "manage",
            )
            if str(space["kind"]) != "project":
                raise TeamError(400, "not_project", "space is not a project")
            membership = conn.execute(
                """
                SELECT role, active, membership_revision
                FROM team_project_members
                WHERE space_id = ? AND user_id = ?
                """,
                (normalized_space_id, target_id),
            ).fetchone()
            if membership is None or not bool(membership["active"]):
                raise TeamError(404, "member_not_found", "project member not found")
            member_role = str(membership["role"])
            previous_membership_revision = int(membership["membership_revision"])
            if member_role == "owner":
                raise TeamError(409, "last_manager", "the last project manager cannot be removed")
            if member_role == "maintainer":
                manager_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM team_project_members
                        WHERE space_id = ? AND active = 1
                          AND role IN ('owner', 'maintainer')
                        """,
                        (normalized_space_id,),
                    ).fetchone()[0]
                )
                if manager_count <= 1:
                    raise TeamError(409, "last_manager", "the last project manager cannot be removed")
            membership_revision = previous_membership_revision + 1
            conn.execute(
                """
                UPDATE team_project_members
                SET active = 0, membership_revision = ?
                WHERE space_id = ? AND user_id = ? AND active = 1
                """,
                (membership_revision, normalized_space_id, target_id),
            )
            conn.execute(
                "UPDATE team_spaces SET revision = revision + 1, updated_at_ms = ? WHERE id = ?",
                (now, normalized_space_id),
            )
            updated_space = conn.execute(
                "SELECT revision FROM team_spaces WHERE id = ?",
                (normalized_space_id,),
            ).fetchone()
            assert updated_space is not None
            return {
                "removed": True,
                "spaceId": normalized_space_id,
                "userId": target_id,
                "membershipRevision": membership_revision,
                "revision": int(updated_space["revision"]),
            }

    @staticmethod
    def _insert_personal_space(
        conn: sqlite3.Connection,
        user_id: str,
        username: str,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO team_spaces(
                id, kind, name, owner_user_id, revision, created_at_ms, updated_at_ms
            ) VALUES (?, 'personal', ?, ?, 1, ?, ?)
            """,
            (_new_id("space"), f"{username}'s space", user_id, now, now),
        )

    @staticmethod
    def _require_active_user(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM team_users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise TeamError(401, "authentication_required", "authentication is required")
        if not bool(row["active"]):
            raise TeamError(401, "account_disabled", "account is disabled")
        return cast(sqlite3.Row, row)

    @classmethod
    def _require_admin(cls, conn: sqlite3.Connection, user_id: str) -> sqlite3.Row:
        row = cls._require_active_user(conn, user_id)
        if str(row["role"]) != "admin":
            raise TeamError(403, "forbidden", "admin role is required")
        return row

    @staticmethod
    def _space_access(
        conn: sqlite3.Connection,
        user_id: str,
        space_id: str,
        action: str,
    ) -> tuple[sqlite3.Row, str, int]:
        space = conn.execute("SELECT * FROM team_spaces WHERE id = ?", (space_id,)).fetchone()
        if space is None:
            raise _space_not_found()
        if str(space["kind"]) == "personal":
            if str(space["owner_user_id"]) != user_id:
                raise _space_not_found()
            role = "owner"
        else:
            membership = conn.execute(
                """
                SELECT role, active, membership_revision
                FROM team_project_members
                WHERE space_id = ? AND user_id = ?
                """,
                (space_id, user_id),
            ).fetchone()
            if membership is None or not bool(membership["active"]):
                raise _space_not_found()
            role = str(membership["role"])
            membership_revision = int(membership["membership_revision"])
        if action == "write" and role not in _WRITE_ROLES:
            raise TeamError(403, "forbidden", "project write access is required")
        if action == "manage" and role not in _MANAGE_ROLES:
            raise TeamError(403, "forbidden", "project manage access is required")
        if str(space["kind"]) == "personal":
            membership_revision = 1
        return space, role, membership_revision


__all__ = ["TeamIdentityStore"]
