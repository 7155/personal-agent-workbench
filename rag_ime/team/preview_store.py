"""Project-scoped preview state and short-lived browser access leases.

Preview metadata belongs to the shared project.  It records which committed
project version a controller is bringing up, while the controller remains the
authority for actual process health and stop receipts.  Browser tickets and
leases are only hashed in the team database and are rechecked against the
current PAW login session and project membership on every use.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import re
import secrets
import sqlite3
import time
from typing import Any, cast

from ..db import sqlite_connection
from .errors import TeamError
from .identity import TeamIdentityStore


_MAX_IDENTIFIER_LENGTH = 256
_MAX_BRANCH_LENGTH = 128
_MAX_COMMIT_LENGTH = 64
_MAX_CLIENT_REQUEST_LENGTH = 256
_MAX_ERROR_LENGTH = 2_000
_MAX_REQUIREMENTS_REVISION = 2**31 - 1
_PREVIEW_TICKET_TTL_MS = 30_000
_PREVIEW_LEASE_TTL_MS = 15 * 60_000
_SQLITE_BUSY_TIMEOUT_MS = 5_000
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
# ``retained`` previews can still own a live controller process after a newer
# deployment becomes active.  Boot recovery must inspect those rows as well;
# the database status is metadata, not proof that a process is healthy.
_LIVE_STATUSES = frozenset({"starting", "ready", "retained", "recovery_required"})
_PUBLIC_STATUSES = frozenset(
    {"starting", "ready", "retained", "failed", "stopped", "recovery_required"}
)


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


def _identifier(value: object, field: str) -> str:
    normalized = _text(value, field, _MAX_IDENTIFIER_LENGTH)
    if "/" in normalized or "\\" in normalized:
        raise TeamError(400, "invalid_input", f"{field} contains a path separator")
    return normalized


def _branch(value: object) -> str:
    normalized = _text(value, "branch", _MAX_BRANCH_LENGTH)
    if (
        not _BRANCH_RE.fullmatch(normalized)
        or ".." in normalized
        or normalized.endswith("/")
        or "//" in normalized
        or normalized.endswith(".")
        or normalized.endswith(".lock")
    ):
        raise TeamError(400, "invalid_input", "branch is not a safe Git branch")
    return normalized


def _commit(value: object) -> str:
    normalized = _text(value, "commit", _MAX_COMMIT_LENGTH).casefold()
    if _COMMIT_RE.fullmatch(normalized) is None:
        raise TeamError(400, "invalid_input", "commit must be a Git object id")
    return normalized


def _revision(value: object, field: str = "requirements_revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TeamError(400, "invalid_input", f"{field} must be an integer")
    if not 0 <= value <= _MAX_REQUIREMENTS_REVISION:
        raise TeamError(400, "invalid_input", f"{field} is out of range")
    return value


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _not_found(message: str = "preview deployment not found") -> TeamError:
    return TeamError(404, "resource_not_found", message)


def _access_denied() -> TeamError:
    return TeamError(403, "forbidden", "preview access is not authorized")


def _ticket_invalid() -> TeamError:
    return TeamError(401, "preview_ticket_invalid", "preview ticket is invalid or expired")


def _lease_invalid() -> TeamError:
    return TeamError(401, "preview_lease_invalid", "preview lease is invalid or expired")


class TeamPreviewStore:
    """Persist project preview state without owning a process or HTTP server."""

    def __init__(
        self,
        identity: TeamIdentityStore,
        *,
        clock: Any | None = None,
    ) -> None:
        self.identity = identity
        self.clock = clock or (lambda: int(time.time() * 1000))

    def _now_ms(self) -> int:
        try:
            return int(self.clock())
        except (TypeError, ValueError) as exc:
            raise RuntimeError("preview clock must return milliseconds") from exc

    @staticmethod
    def _prune_expired_conn(conn: sqlite3.Connection, now: int) -> None:
        """Remove expired capability rows while a writer already owns the DB."""

        conn.execute(
            "DELETE FROM team_preview_tickets WHERE expires_at_ms <= ?",
            (now,),
        )
        conn.execute(
            "DELETE FROM team_preview_leases WHERE expires_at_ms <= ?",
            (now,),
        )

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
                self._prune_expired_conn(conn, self._now_ms())
            yield conn

    def _scope_conn(
        self,
        conn: sqlite3.Connection,
        actor_user_id: str,
        space_id: str,
        action: str,
    ) -> dict[str, object]:
        # These identity helpers operate on the caller's already-open
        # transaction.  Calling the public method here would open a second
        # SQLite connection and could observe a different membership revision.
        user = self.identity._require_active_user(conn, actor_user_id)  # type: ignore[attr-defined]
        space, role, membership_revision = self.identity._space_access(  # type: ignore[attr-defined]
            conn,
            actor_user_id,
            space_id,
            action,
        )
        if str(space["kind"]) != "project":
            raise _not_found()
        return {
            "id": str(space["id"]),
            "kind": "project",
            "name": str(space["name"]),
            "role": str(role),
            "membershipRevision": int(membership_revision),
            "userId": actor_user_id,
            "displayName": str(user["display_name"] or user["username"]),
        }

    def _session_conn(
        self,
        conn: sqlite3.Connection,
        login_session_id: str,
        *,
        now: int,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT s.*, u.active AS user_active
            FROM team_sessions AS s
            JOIN team_users AS u ON u.id = s.user_id
            WHERE s.id = ?
            """,
            (login_session_id,),
        ).fetchone()
        if (
            row is None
            or row["revoked_at_ms"] is not None
            or int(row["expires_at_ms"]) <= now
            or not bool(row["user_active"])
        ):
            raise _ticket_invalid()
        return cast(sqlite3.Row, row)

    @staticmethod
    def _latest_requirements_revision_conn(conn: sqlite3.Connection, space_id: str) -> int:
        row = conn.execute(
            "SELECT revision FROM team_project_briefs WHERE space_id = ? ORDER BY revision DESC LIMIT 1",
            (space_id,),
        ).fetchone()
        return int(row["revision"]) if row is not None else 0

    @staticmethod
    def _require_requirements_revision_conn(
        conn: sqlite3.Connection,
        space_id: str,
        revision: int,
    ) -> None:
        if revision == 0:
            return
        row = conn.execute(
            "SELECT 1 FROM team_project_briefs WHERE space_id = ? AND revision = ?",
            (space_id, revision),
        ).fetchone()
        if row is None:
            raise TeamError(
                409,
                "requirements_revision_not_found",
                "requirements revision is not available for this project",
            )

    @staticmethod
    def _project_conn(
        conn: sqlite3.Connection,
        space_id: str,
        branch: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM team_projects WHERE space_id = ? AND target_branch = ?",
            (space_id, branch),
        ).fetchone()
        if row is None:
            raise TeamError(
                409,
                "preview_repository_unavailable",
                "project repository is not provisioned for this branch",
            )
        return cast(sqlite3.Row, row)

    @staticmethod
    def _public_deployment(row: sqlite3.Row) -> dict[str, object]:
        result: dict[str, object] = {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "branch": str(row["branch"]),
            "commit": str(row["commit_sha"]),
            "requirementsRevision": int(row["requirements_revision"]),
            "requestedByDisplayName": str(row["requested_by_display_name"]),
            "createdAtMs": int(row["created_at_ms"]),
        }
        if row["ready_at_ms"] is not None:
            result["readyAtMs"] = int(row["ready_at_ms"])
        if row["error"]:
            result["error"] = str(row["error"])
        return result

    @staticmethod
    def _snapshot_deployment(row: sqlite3.Row) -> dict[str, object]:
        result = TeamPreviewStore._public_deployment(row)
        result.update(
            {
                "spaceId": str(row["space_id"]),
                "clientRequestId": str(row["client_request_id"]),
                "requestedByUserId": str(row["requested_by_user_id"]),
                "requestedByMembershipRevision": int(row["requested_by_membership_revision"]),
                "updatedAtMs": int(row["updated_at_ms"]),
            }
        )
        return result

    @staticmethod
    def _new_deployment_id() -> str:
        return "pv-" + secrets.token_hex(16)

    def reserve(
        self,
        actor_user_id: object,
        space_id: object,
        *,
        branch: object,
        commit: object,
        requirements_revision: object,
        client_request_id: object,
    ) -> dict[str, object]:
        """Reserve one project preview candidate with an idempotency key."""

        actor = _identifier(actor_user_id, "actor_user_id")
        normalized_space = _identifier(space_id, "space_id")
        normalized_branch = _branch(branch)
        normalized_commit = _commit(commit)
        normalized_requirements = _revision(requirements_revision)
        normalized_request = _text(
            client_request_id,
            "client_request_id",
            _MAX_CLIENT_REQUEST_LENGTH,
        )
        with self._db(write=True) as conn:
            scope = self._scope_conn(conn, actor, normalized_space, "manage")
            self._project_conn(conn, normalized_space, normalized_branch)
            self._require_requirements_revision_conn(
                conn,
                normalized_space,
                normalized_requirements,
            )
            existing = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE space_id = ? AND client_request_id = ?",
                (normalized_space, normalized_request),
            ).fetchone()
            if existing is not None:
                same_request = (
                    str(existing["requested_by_user_id"]) == actor
                    and str(existing["branch"]) == normalized_branch
                    and str(existing["commit_sha"]) == normalized_commit
                    and int(existing["requirements_revision"]) == normalized_requirements
                )
                if not same_request:
                    raise TeamError(
                        409,
                        "preview_request_conflict",
                        "client request id is already bound to another preview",
                    )
                return self._public_deployment(existing)
            blocking = conn.execute(
                """
                SELECT 1 FROM team_preview_deployments
                WHERE space_id = ? AND status IN ('starting', 'recovery_required')
                LIMIT 1
                """,
                (normalized_space,),
            ).fetchone()
            if blocking is not None:
                raise TeamError(
                    409,
                    "preview_starting_exists",
                    "this project already has a preview starting or recovering",
                )
            now = self._now_ms()
            deployment_id = self._new_deployment_id()
            conn.execute(
                """
                INSERT INTO team_preview_deployments(
                    id, space_id, branch, commit_sha, requirements_revision,
                    client_request_id, requested_by_user_id,
                    requested_by_membership_revision, requested_by_display_name,
                    status, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'starting', ?, ?)
                """,
                (
                    deployment_id,
                    normalized_space,
                    normalized_branch,
                    normalized_commit,
                    normalized_requirements,
                    normalized_request,
                    actor,
                    int(cast(Any, scope["membershipRevision"])),
                    str(scope["displayName"]),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (deployment_id,),
            ).fetchone()
            assert row is not None
            return self._public_deployment(cast(sqlite3.Row, row))

    def public_status(self, actor_user_id: object, space_id: object) -> dict[str, object]:
        """Return bounded project preview metadata without paths or tokens."""

        actor = _identifier(actor_user_id, "actor_user_id")
        normalized_space = _identifier(space_id, "space_id")
        with self._db() as conn:
            self._scope_conn(conn, actor, normalized_space, "read")
            current_requirements = self._latest_requirements_revision_conn(conn, normalized_space)
            active = conn.execute(
                """
                SELECT * FROM team_preview_deployments
                WHERE space_id = ? AND status = 'ready'
                -- A test clock and a busy server can create two requests in
                -- one millisecond.  The implicit SQLite rowid preserves the
                -- insertion order in that tie; random deployment IDs do not.
                ORDER BY created_at_ms DESC, rowid DESC LIMIT 1
                """,
                (normalized_space,),
            ).fetchone()
            latest = conn.execute(
                """
                SELECT * FROM team_preview_deployments
                WHERE space_id = ?
                ORDER BY created_at_ms DESC, rowid DESC LIMIT 1
                """,
                (normalized_space,),
            ).fetchone()
            branch = (
                str(active["branch"])
                if active is not None
                else str(latest["branch"])
                if latest is not None
                else "main"
            )
            project = conn.execute(
                """
                SELECT head_commit FROM team_projects
                WHERE space_id = ? AND target_branch = ?
                """,
                (normalized_space, branch),
            ).fetchone()
            return {
                "active": self._public_deployment(cast(sqlite3.Row, active)) if active is not None else None,
                "latest": self._public_deployment(cast(sqlite3.Row, latest)) if latest is not None else None,
                "currentRequirementsRevision": current_requirements,
                "repositoryHead": str(project["head_commit"]) if project is not None else None,
            }

    def activate(self, actor_user_id: object, deployment_id: object) -> dict[str, object]:
        """Atomically promote a current, still-authorized candidate."""

        actor = _identifier(actor_user_id, "actor_user_id")
        normalized_deployment = _identifier(deployment_id, "deployment_id")
        with self._db(write=True) as conn:
            actor_scope = self._scope_conn(conn, actor, self._space_for_deployment_conn(conn, normalized_deployment), "manage")
            row = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (normalized_deployment,),
            ).fetchone()
            if row is None:
                raise _not_found()
            row = cast(sqlite3.Row, row)
            space_id = str(row["space_id"])
            if str(actor_scope["id"]) != space_id:
                raise _not_found()
            if str(row["status"]) != "starting":
                raise TeamError(409, "preview_not_starting", "preview is no longer awaiting activation")
            latest = conn.execute(
                "SELECT id FROM team_preview_deployments WHERE space_id = ? ORDER BY created_at_ms DESC, rowid DESC LIMIT 1",
                (space_id,),
            ).fetchone()
            if latest is None or str(latest["id"]) != normalized_deployment:
                raise TeamError(409, "preview_not_latest", "preview is no longer the latest project request")
            try:
                requester_scope = self._scope_conn(
                    conn,
                    str(row["requested_by_user_id"]),
                    space_id,
                    "manage",
                )
            except TeamError as exc:
                raise TeamError(
                    409,
                    "preview_request_revoked",
                    "preview requester membership changed during activation",
                ) from exc
            if int(requester_scope["membershipRevision"]) != int(row["requested_by_membership_revision"]):
                raise TeamError(
                    409,
                    "preview_request_revoked",
                    "preview requester membership changed during activation",
                )
            current_requirements = self._latest_requirements_revision_conn(conn, space_id)
            if int(row["requirements_revision"]) != current_requirements:
                raise TeamError(
                    409,
                    "preview_stale",
                    "preview requirements do not match the current project brief",
                )
            project = self._project_conn(conn, space_id, str(row["branch"]))
            if str(project["head_commit"]).casefold() != str(row["commit_sha"]).casefold():
                raise TeamError(
                    409,
                    "preview_stale",
                    "preview commit does not match the current project branch",
                )
            now = self._now_ms()
            conn.execute(
                "UPDATE team_preview_deployments SET status = 'retained', updated_at_ms = ? WHERE space_id = ? AND status = 'ready'",
                (now, space_id),
            )
            conn.execute(
                """
                UPDATE team_preview_deployments
                SET status = 'ready', ready_at_ms = ?, updated_at_ms = ?, error = NULL
                WHERE id = ? AND status = 'starting'
                """,
                (now, now, normalized_deployment),
            )
            updated = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (normalized_deployment,),
            ).fetchone()
            assert updated is not None
            return self._public_deployment(cast(sqlite3.Row, updated))

    @staticmethod
    def _space_for_deployment_conn(conn: sqlite3.Connection, deployment_id: str) -> str:
        row = conn.execute(
            "SELECT space_id FROM team_preview_deployments WHERE id = ?",
            (deployment_id,),
        ).fetchone()
        if row is None:
            raise _not_found()
        return str(row["space_id"])

    def mark_failed(self, deployment_id: object, error: object) -> dict[str, object]:
        """Record a controller failure; this method does not claim a stop."""

        normalized_deployment = _identifier(deployment_id, "deployment_id")
        normalized_error = _text(error, "error", _MAX_ERROR_LENGTH)
        with self._db(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (normalized_deployment,),
            ).fetchone()
            if row is None:
                raise _not_found()
            row = cast(sqlite3.Row, row)
            status = str(row["status"])
            if status in {"failed", "stopped", "retained"}:
                return self._public_deployment(row)
            if status == "ready":
                raise TeamError(409, "preview_state_conflict", "an active preview cannot be marked failed")
            if status != "starting" and status != "recovery_required":
                raise TeamError(409, "preview_state_conflict", "preview cannot be marked failed")
            now = self._now_ms()
            conn.execute(
                "UPDATE team_preview_deployments SET status = 'failed', error = ?, updated_at_ms = ? WHERE id = ?",
                (normalized_error, now, normalized_deployment),
            )
            updated = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (normalized_deployment,),
            ).fetchone()
            assert updated is not None
            return self._public_deployment(cast(sqlite3.Row, updated))

    def deactivate(self, deployment_id: object) -> dict[str, object]:
        """Remove a ready preview from the active slot without claiming a stop."""

        normalized_deployment = _identifier(deployment_id, "deployment_id")
        with self._db(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (normalized_deployment,),
            ).fetchone()
            if row is None:
                raise _not_found()
            row = cast(sqlite3.Row, row)
            if str(row["status"]) in {"retained", "failed", "stopped", "recovery_required"}:
                return self._public_deployment(row)
            if str(row["status"]) != "ready":
                raise TeamError(409, "preview_state_conflict", "preview is not active")
            now = self._now_ms()
            conn.execute(
                "UPDATE team_preview_deployments SET status = 'retained', updated_at_ms = ? WHERE id = ? AND status = 'ready'",
                (now, normalized_deployment),
            )
            updated = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (normalized_deployment,),
            ).fetchone()
            assert updated is not None
            return self._public_deployment(cast(sqlite3.Row, updated))

    def mark_stopped(self, deployment_id: object, error: object | None = None) -> dict[str, object]:
        """Record a verified stop receipt supplied by the trusted controller."""

        normalized_deployment = _identifier(deployment_id, "deployment_id")
        normalized_error = (
            _text(error, "error", _MAX_ERROR_LENGTH, allow_empty=True)
            if error is not None
            else ""
        )
        with self._db(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (normalized_deployment,),
            ).fetchone()
            if row is None:
                raise _not_found()
            row = cast(sqlite3.Row, row)
            if str(row["status"]) == "stopped":
                return self._public_deployment(row)
            now = self._now_ms()
            conn.execute(
                "UPDATE team_preview_deployments SET status = 'stopped', error = ?, updated_at_ms = ? WHERE id = ?",
                (normalized_error or None, now, normalized_deployment),
            )
            updated = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (normalized_deployment,),
            ).fetchone()
            assert updated is not None
            return self._public_deployment(cast(sqlite3.Row, updated))

    def mark_recovery_required(self, deployment_id: object, error: object) -> dict[str, object]:
        """Quarantine a deployment before controller recovery or a stop.

        This is the fail-closed controller transition.  It immediately removes
        the deployment from the ready/retained lease-authority states while
        making no claim that the underlying process has stopped.  A later
        ``mark_stopped`` call records the actual controller stop receipt.
        """

        normalized_deployment = _identifier(deployment_id, "deployment_id")
        normalized_error = _text(error, "error", _MAX_ERROR_LENGTH)
        with self._db(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (normalized_deployment,),
            ).fetchone()
            if row is None:
                raise _not_found()
            row = cast(sqlite3.Row, row)
            status = str(row["status"])
            if status in {"failed", "stopped", "recovery_required"}:
                return self._public_deployment(row)
            if status not in {"starting", "ready", "retained"}:
                raise TeamError(409, "preview_state_conflict", "preview cannot enter recovery")
            now = self._now_ms()
            conn.execute(
                """
                UPDATE team_preview_deployments
                SET status = 'recovery_required', error = ?, updated_at_ms = ?
                WHERE id = ? AND status IN ('starting', 'ready', 'retained')
                """,
                (normalized_error, now, normalized_deployment),
            )
            updated = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (normalized_deployment,),
            ).fetchone()
            assert updated is not None
            return self._public_deployment(cast(sqlite3.Row, updated))

    def snapshot(self, deployment_id: object) -> dict[str, object]:
        """Return controller-only metadata, including requester provenance."""

        normalized_deployment = _identifier(deployment_id, "deployment_id")
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM team_preview_deployments WHERE id = ?",
                (normalized_deployment,),
            ).fetchone()
            if row is None:
                raise _not_found()
            return self._snapshot_deployment(cast(sqlite3.Row, row))

    def list_live(self) -> list[dict[str, object]]:
        """Return internal rows needing controller recovery or health checks."""

        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM team_preview_deployments
                WHERE status IN ('starting', 'ready', 'retained', 'recovery_required')
                ORDER BY created_at_ms ASC, rowid ASC
                """
            ).fetchall()
            return [self._snapshot_deployment(cast(sqlite3.Row, row)) for row in rows]

    def issue_ticket(
        self,
        actor_user_id: object,
        space_id: object,
        login_session_id: object,
    ) -> dict[str, object]:
        """Issue one short-lived browser ticket for the current active preview."""

        actor = _identifier(actor_user_id, "actor_user_id")
        normalized_space = _identifier(space_id, "space_id")
        normalized_session = _identifier(login_session_id, "login_session_id")
        with self._db(write=True) as conn:
            scope = self._scope_conn(conn, actor, normalized_space, "read")
            now = self._now_ms()
            session = self._session_conn(conn, normalized_session, now=now)
            if str(session["user_id"]) != actor:
                raise _access_denied()
            deployment = conn.execute(
                """
                SELECT * FROM team_preview_deployments
                WHERE space_id = ? AND status = 'ready'
                ORDER BY created_at_ms DESC, rowid DESC LIMIT 1
                """,
                (normalized_space,),
            ).fetchone()
            if deployment is None:
                raise _not_found("active preview not found")
            token = secrets.token_urlsafe(32)
            ticket_id = "pt-" + secrets.token_hex(16)
            expires_at = now + _PREVIEW_TICKET_TTL_MS
            conn.execute(
                """
                INSERT INTO team_preview_tickets(
                    id, deployment_id, login_session_id, actor_user_id,
                    membership_revision, token_hash, expires_at_ms, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    str(deployment["id"]),
                    normalized_session,
                    actor,
                    int(cast(Any, scope["membershipRevision"])),
                    _hash_token(token),
                    expires_at,
                    now,
                ),
            )
            return {
                "deployment": self._public_deployment(cast(sqlite3.Row, deployment)),
                "ticket": token,
                "expiresAtMs": expires_at,
            }

    def consume_ticket(self, deployment_id: object, ticket: object) -> dict[str, object]:
        """Atomically exchange a one-use ticket for a current lease token."""

        normalized_deployment = _identifier(deployment_id, "deployment_id")
        normalized_ticket = _text(ticket, "ticket", 512)
        with self._db(write=True) as conn:
            now = self._now_ms()
            row = conn.execute(
                """
                SELECT t.*, d.space_id, d.status AS deployment_status
                FROM team_preview_tickets AS t
                JOIN team_preview_deployments AS d ON d.id = t.deployment_id
                WHERE t.token_hash = ? AND t.deployment_id = ?
                """,
                (_hash_token(normalized_ticket), normalized_deployment),
            ).fetchone()
            if (
                row is None
                or row["consumed_at_ms"] is not None
                or int(row["expires_at_ms"]) <= now
                or str(row["deployment_status"]) not in {"ready", "retained"}
            ):
                raise _ticket_invalid()
            row = cast(sqlite3.Row, row)
            try:
                session = self._session_conn(conn, str(row["login_session_id"]), now=now)
                scope = self._scope_conn(
                    conn,
                    str(row["actor_user_id"]),
                    str(row["space_id"]),
                    "read",
                )
            except TeamError as exc:
                raise _ticket_invalid() from exc
            if (
                str(session["user_id"]) != str(row["actor_user_id"])
                or int(scope["membershipRevision"]) != int(row["membership_revision"])
            ):
                raise _ticket_invalid()
            consumed = conn.execute(
                "UPDATE team_preview_tickets SET consumed_at_ms = ? WHERE id = ? AND consumed_at_ms IS NULL",
                (now, str(row["id"])),
            )
            if consumed.rowcount != 1:
                raise _ticket_invalid()
            lease_token = secrets.token_urlsafe(48)
            lease_id = "pl-" + secrets.token_hex(16)
            expires_at = now + _PREVIEW_LEASE_TTL_MS
            conn.execute(
                """
                INSERT INTO team_preview_leases(
                    id, deployment_id, login_session_id, actor_user_id,
                    membership_revision, token_hash, expires_at_ms, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    normalized_deployment,
                    str(row["login_session_id"]),
                    str(row["actor_user_id"]),
                    int(scope["membershipRevision"]),
                    _hash_token(lease_token),
                    expires_at,
                    now,
                ),
            )
            return {
                "leaseToken": lease_token,
                "expiresAtMs": expires_at,
                "actorUserId": str(row["actor_user_id"]),
                "spaceId": str(row["space_id"]),
                "deploymentId": normalized_deployment,
                "role": str(scope["role"]),
            }

    def authorize_lease(
        self,
        deployment_id: object,
        lease_token: object,
        *,
        action: str = "read",
    ) -> dict[str, object]:
        """Revalidate a browser lease against current login and membership."""

        normalized_deployment = _identifier(deployment_id, "deployment_id")
        normalized_lease = _text(lease_token, "lease_token", 512)
        if not isinstance(action, str) or action not in {"read", "write"}:
            raise TeamError(400, "invalid_action", "action must be read or write")
        with self._db() as conn:
            now = self._now_ms()
            row = conn.execute(
                """
                SELECT l.*, d.space_id, d.status AS deployment_status
                FROM team_preview_leases AS l
                JOIN team_preview_deployments AS d ON d.id = l.deployment_id
                WHERE l.token_hash = ? AND l.deployment_id = ?
                """,
                (_hash_token(normalized_lease), normalized_deployment),
            ).fetchone()
            if (
                row is None
                or int(row["expires_at_ms"]) <= now
                or str(row["deployment_status"]) not in {"ready", "retained"}
            ):
                raise _lease_invalid()
            row = cast(sqlite3.Row, row)
            try:
                session = self._session_conn(conn, str(row["login_session_id"]), now=now)
            except TeamError as exc:
                raise _lease_invalid() from exc
            if str(session["user_id"]) != str(row["actor_user_id"]):
                raise _lease_invalid()
            try:
                scope = self._scope_conn(
                    conn,
                    str(row["actor_user_id"]),
                    str(row["space_id"]),
                    "read",
                )
            except TeamError as exc:
                raise _lease_invalid() from exc
            if int(scope["membershipRevision"]) != int(row["membership_revision"]):
                raise _lease_invalid()
            if action == "write" and str(scope["role"]) == "viewer":
                raise TeamError(403, "forbidden", "preview write access is not available to viewers")
            return {
                "actorUserId": str(row["actor_user_id"]),
                "spaceId": str(row["space_id"]),
                "deploymentId": normalized_deployment,
                "role": str(scope["role"]),
            }


__all__ = ["TeamPreviewStore"]
