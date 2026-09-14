"""Durable Team package publications and immutable Session resource snapshots.

The resource store is deliberately metadata-only.  A trusted package staging
component owns the source path and integrity checks; this module records only
the staged package's public identity and an immutable selection snapshot.  It
never loads a Pi host, follows a package path, or executes package content.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re
import secrets
import sqlite3
from typing import Callable, cast

from .errors import TeamError
from .grants import TeamGrantStore
from .identity import TeamIdentityStore
from .shared_packages import StagedTeamPackage


_MAX_ACTOR_ID = 256
_MAX_SPACE_ID = 256
_MAX_SESSION_ID = 256
_MAX_PUBLICATION_ID = 256
_MAX_PACKAGE_ID = 128
_MAX_VERSION = 64
_MAX_METADATA_BYTES = 256 * 1024
_MAX_PUBLICATIONS = 512
_MAX_SELECTION = 16
_MAX_REVISION = 2**31 - 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_ID = re.compile(r"^[A-Za-z0-9@._/-]{1,128}$")
_STATUSES = frozenset({"published", "withdrawn"})
_PRIVATE_METADATA_KEYS = frozenset(
    {
        "path",
        "sourcepath",
        "sourceroot",
        "hostpath",
        "storageroot",
        "storagepath",
        "catalogpath",
    }
)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(16)}"


def _invalid(field: str, message: str = "is invalid") -> TeamError:
    return TeamError(400, "invalid_input", f"{field} {message}")


def _identifier(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise _invalid(field, "must be text")
    if not value or len(value) > maximum or "\x00" in value:
        raise _invalid(field, "has an invalid size")
    if "/" in value or "\\" in value:
        raise _invalid(field, "contains a path separator")
    return value


def _revision(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(field, "must be an integer")
    if not 0 <= value <= _MAX_REVISION:
        raise _invalid(field, "is out of range")
    return value


def _resource_not_found(message: str = "resource not found") -> TeamError:
    return TeamError(404, "resource_not_found", message)


def _session_not_found() -> TeamError:
    return _resource_not_found("Session not found in this space")


def _forbidden(message: str = "resource management access is required") -> TeamError:
    return TeamError(403, "forbidden", message)


def _canonical_json(value: object, *, field: str, maximum: int = _MAX_METADATA_BYTES) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _invalid(field, "must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise TeamError(400, "metadata_too_large", f"{field} exceeds its size limit")
    return encoded


def _public_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TeamError(400, "invalid_package", "staged package metadata must be an object")

    def validate(node: object) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise TeamError(400, "invalid_package", "staged package metadata has an invalid key")
                if key.casefold() in _PRIVATE_METADATA_KEYS:
                    raise TeamError(
                        400,
                        "invalid_package",
                        "staged package metadata contains a private path",
                    )
                validate(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                validate(child)

    validate(value)
    encoded = _canonical_json(value, field="metadata")
    normalized = json.loads(encoded)
    if not isinstance(normalized, dict):
        raise TeamError(400, "invalid_package", "staged package metadata must be an object")
    return cast(dict[str, object], normalized)


def _json_list(raw: object, *, field: str, maximum: int) -> list[object]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > maximum:
        raise TeamError(500, "resource_store_corrupt", f"{field} is invalid")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TeamError(500, "resource_store_corrupt", f"{field} is invalid") from exc
    if not isinstance(value, list) or len(value) > _MAX_PUBLICATIONS:
        raise TeamError(500, "resource_store_corrupt", f"{field} is invalid")
    return list(value)


def _json_object(raw: object, *, field: str) -> dict[str, object]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise TeamError(500, "resource_store_corrupt", f"{field} is invalid")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TeamError(500, "resource_store_corrupt", f"{field} is invalid") from exc
    if not isinstance(value, dict):
        raise TeamError(500, "resource_store_corrupt", f"{field} is invalid")
    return cast(dict[str, object], value)


class TeamSharedResourceStore:
    """Store administrator publications and per-Session frozen selections."""

    def __init__(
        self,
        identity: TeamIdentityStore,
        *,
        grants: TeamGrantStore | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(identity, TeamIdentityStore):
            raise TypeError("identity must be a TeamIdentityStore")
        self.identity = identity
        self._clock = clock or identity._now_ms
        self.grants = grants or TeamGrantStore(
            identity.db_path,
            identity,
            now_ms=self._now_ms,
        )
        # Ensure the dedicated migration (including revision-zero snapshots
        # for pre-existing bindings) is applied before this store is used.
        identity.initialize()

    def _now_ms(self) -> int:
        try:
            return int(self._clock())
        except (TypeError, ValueError) as exc:
            raise RuntimeError("team resource clock must return milliseconds") from exc

    def list_published(self, actor: str) -> list[dict[str, object]]:
        actor_id = _identifier(actor, "actor_user_id", _MAX_ACTOR_ID)
        with self.identity._connection() as conn:
            self.identity._require_active_user(conn, actor_id)
            rows = conn.execute(
                """
                SELECT *
                FROM team_published_resources
                ORDER BY published_at_ms DESC, publication_id DESC
                LIMIT ?
                """,
                (_MAX_PUBLICATIONS,),
            ).fetchall()
            return [self._public_resource(row) for row in rows]

    def publish(self, actor: str, staged: StagedTeamPackage) -> dict[str, object]:
        actor_id = _identifier(actor, "actor_user_id", _MAX_ACTOR_ID)
        package_id, version, digest, metadata_json = self._validate_staged(staged)
        now = self._now_ms()
        with self.identity._connection(write=True) as conn:
            self.identity._require_admin(conn, actor_id)
            existing = conn.execute(
                """
                SELECT * FROM team_published_resources
                WHERE package_id = ? AND version = ?
                """,
                (package_id, version),
            ).fetchone()
            if existing is not None:
                if str(existing["digest"]) != digest:
                    raise TeamError(
                        409,
                        "resource_version_conflict",
                        "that package version was published with different content",
                    )
                if str(existing["metadata_json"]) != metadata_json:
                    raise TeamError(
                        409,
                        "resource_metadata_conflict",
                        "that package version has immutable metadata",
                    )
                return self._public_resource(existing)

            publication_id = _new_id("pub")
            try:
                conn.execute(
                    """
                    INSERT INTO team_published_resources(
                        publication_id, package_id, version, digest, status,
                        metadata_json, published_by_user_id, published_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, 'published', ?, ?, ?, ?)
                    """,
                    (
                        publication_id,
                        package_id,
                        version,
                        digest,
                        metadata_json,
                        actor_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # The package/version uniqueness fence is also the final
                # protection for two concurrent administrators.  Re-read the
                # winner and apply the same idempotency rules when possible.
                if "package_id" not in str(exc) and "idx_team_published_package_version" not in str(exc):
                    raise TeamError(500, "resource_publish_failed", "resource could not be published") from None
                winner = conn.execute(
                    """
                    SELECT * FROM team_published_resources
                    WHERE package_id = ? AND version = ?
                    """,
                    (package_id, version),
                ).fetchone()
                if winner is None:
                    raise TeamError(500, "resource_publish_failed", "resource could not be published") from None
                if str(winner["digest"]) != digest:
                    raise TeamError(
                        409,
                        "resource_version_conflict",
                        "that package version was published with different content",
                    ) from None
                if str(winner["metadata_json"]) != metadata_json:
                    raise TeamError(
                        409,
                        "resource_metadata_conflict",
                        "that package version has immutable metadata",
                    ) from None
                return self._public_resource(winner)
            row = conn.execute(
                "SELECT * FROM team_published_resources WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            assert row is not None
            return self._public_resource(row)

    def set_status(
        self,
        actor: str,
        publication_id: str,
        status: str,
    ) -> dict[str, object]:
        actor_id = _identifier(actor, "actor_user_id", _MAX_ACTOR_ID)
        publication = _identifier(publication_id, "publication_id", _MAX_PUBLICATION_ID)
        if not isinstance(status, str):
            raise _invalid("status", "must be text")
        normalized_status = status.strip().casefold()
        if normalized_status not in _STATUSES:
            raise TeamError(400, "invalid_status", "status must be published or withdrawn")
        now = self._now_ms()
        with self.identity._connection(write=True) as conn:
            self.identity._require_admin(conn, actor_id)
            row = conn.execute(
                "SELECT * FROM team_published_resources WHERE publication_id = ?",
                (publication,),
            ).fetchone()
            if row is None:
                raise _resource_not_found("published resource not found")
            if str(row["status"]) != normalized_status:
                conn.execute(
                    "UPDATE team_published_resources SET status = ?, updated_at_ms = ? WHERE publication_id = ?",
                    (normalized_status, now, publication),
                )
                row = conn.execute(
                    "SELECT * FROM team_published_resources WHERE publication_id = ?",
                    (publication,),
                ).fetchone()
                assert row is not None
            return self._public_resource(row)

    def selection(self, actor: str, space_id: str) -> dict[str, object]:
        actor_id = _identifier(actor, "actor_user_id", _MAX_ACTOR_ID)
        normalized_space = _identifier(space_id, "space_id", _MAX_SPACE_ID)
        with self.identity._connection() as conn:
            self._require_space(conn, actor_id, normalized_space, action="read")
            return self._selection_from_db(conn, normalized_space)

    def select(
        self,
        actor: str,
        space_id: str,
        base_revision: int,
        publication_ids: Sequence[str],
    ) -> dict[str, object]:
        actor_id = _identifier(actor, "actor_user_id", _MAX_ACTOR_ID)
        normalized_space = _identifier(space_id, "space_id", _MAX_SPACE_ID)
        expected_revision = _revision(base_revision, "base_revision")
        ids = self._selection_ids(publication_ids)
        now = self._now_ms()
        with self.identity._connection(write=True) as conn:
            self._require_space(conn, actor_id, normalized_space, action="manage")
            current = conn.execute(
                "SELECT * FROM team_space_resource_selections WHERE space_id = ?",
                (normalized_space,),
            ).fetchone()
            current_revision = int(current["revision"]) if current is not None else 0
            if current_revision != expected_revision:
                raise TeamError(
                    409,
                    "resource_selection_conflict",
                    "resource selection changed; reload before saving",
                )

            rows = self._published_rows_by_ids(conn, ids)
            for publication_id in ids:
                row = rows[publication_id]
                if str(row["status"]) != "published":
                    raise TeamError(
                        409,
                        "resource_not_published",
                        "withdrawn resources cannot be selected for a new Session",
                    )
            package_ids = [str(rows[item]["package_id"]) for item in ids]
            if len(package_ids) != len(set(package_ids)):
                raise TeamError(
                    409,
                    "duplicate_package_selection",
                    "select at most one version of each package",
                )

            revision = current_revision + 1
            ids_json = _canonical_json(ids, field="publication_ids", maximum=16 * 1024)
            if current is None:
                conn.execute(
                    """
                    INSERT INTO team_space_resource_selections(
                        space_id, revision, publication_ids_json, updated_by_user_id, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (normalized_space, revision, ids_json, actor_id, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE team_space_resource_selections
                    SET revision = ?, publication_ids_json = ?, updated_by_user_id = ?, updated_at_ms = ?
                    WHERE space_id = ?
                    """,
                    (revision, ids_json, actor_id, now, normalized_space),
                )
            return self._selection_from_db(conn, normalized_space)

    def capture_session(
        self,
        actor: str,
        space_id: str,
        session_id: str,
        parent_session_id: str | None = None,
    ) -> dict[str, object]:
        actor_id = _identifier(actor, "actor_user_id", _MAX_ACTOR_ID)
        normalized_space = _identifier(space_id, "space_id", _MAX_SPACE_ID)
        normalized_session = _identifier(session_id, "session_id", _MAX_SESSION_ID)
        normalized_parent = (
            None
            if parent_session_id is None or parent_session_id == ""
            else _identifier(parent_session_id, "parent_session_id", _MAX_SESSION_ID)
        )
        if normalized_parent is not None and normalized_parent == normalized_session:
            raise _invalid("parent_session_id", "must identify a different Session")
        # This is the canonical grant check.  The write transaction below
        # repeats the binding/member checks on its own connection so a revoke
        # racing this call cannot be followed by a stale snapshot insert.
        self._require_session_owner(actor_id, normalized_space, normalized_session)
        if normalized_parent is not None:
            self._require_session_owner(actor_id, normalized_space, normalized_parent)

        with self.identity._connection(write=True) as conn:
            self._require_binding_owner_conn(conn, actor_id, normalized_space, normalized_session)
            existing = conn.execute(
                "SELECT * FROM team_session_resource_snapshots WHERE session_id = ?",
                (normalized_session,),
            ).fetchone()

            if normalized_parent is None:
                selection = self._selection_from_db(conn, normalized_space)
                selection_ids = list(cast(list[object], selection["publicationIds"]))
                rows = self._published_rows_by_ids(conn, selection_ids)
                for publication_id in selection_ids:
                    row = rows[str(publication_id)]
                    if str(row["status"]) != "published":
                        # A withdrawn selection may remain visible for old
                        # tasks, but it cannot seed a newly captured root.
                        raise TeamError(
                            409,
                            "resource_selection_stale",
                            "the current resource selection contains a withdrawn package",
                        )
                selection_revision = int(selection["revision"])
                publication_ids = [str(item) for item in selection_ids]
                items = [self._public_resource(rows[item]) for item in publication_ids]
            else:
                # Re-check the parent under the same writer transaction.  The
                # preliminary grant call above only establishes the canonical
                # path; it cannot authorize a parent that was revoked before
                # this snapshot was inserted.
                self._require_binding_owner_conn(conn, actor_id, normalized_space, normalized_parent)
                parent = conn.execute(
                    "SELECT * FROM team_session_resource_snapshots WHERE session_id = ?",
                    (normalized_parent,),
                ).fetchone()
                if parent is None:
                    raise _session_not_found()
                selection_revision = int(parent["selection_revision"])
                publication_ids = [
                    str(item)
                    for item in _json_list(
                        parent["publication_ids_json"],
                        field="parent publication_ids",
                        maximum=16 * 1024,
                    )
                ]
                raw_items = _json_list(
                    parent["items_json"],
                    field="parent items",
                    maximum=_MAX_METADATA_BYTES,
                )
                if any(not isinstance(item, dict) for item in raw_items):
                    raise TeamError(500, "resource_store_corrupt", "parent items are invalid")
                items = [cast(dict[str, object], item) for item in raw_items]

            if existing is not None:
                existing_ids = [
                    str(item)
                    for item in _json_list(
                        existing["publication_ids_json"],
                        field="snapshot publication_ids",
                        maximum=16 * 1024,
                    )
                ]
                existing_items = _json_list(
                    existing["items_json"],
                    field="snapshot items",
                    maximum=_MAX_METADATA_BYTES,
                )
                if (
                    int(existing["selection_revision"]) == selection_revision
                    and existing_ids == publication_ids
                    and existing_items == items
                ):
                    return self._public_snapshot(existing)
                raise TeamError(
                    409,
                    "session_snapshot_immutable",
                    "Session resource snapshot cannot be replaced",
                )

            now = self._now_ms()
            publication_ids_json = _canonical_json(
                publication_ids,
                field="publication_ids",
                maximum=16 * 1024,
            )
            items_json = _canonical_json(items, field="items", maximum=_MAX_METADATA_BYTES)
            try:
                conn.execute(
                    """
                    INSERT INTO team_session_resource_snapshots(
                        session_id, space_id, selection_revision,
                        publication_ids_json, items_json, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_session,
                        normalized_space,
                        selection_revision,
                        publication_ids_json,
                        items_json,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "session_id" not in str(exc):
                    raise TeamError(500, "resource_snapshot_failed", "Session resources could not be captured") from None
                winner = conn.execute(
                    "SELECT * FROM team_session_resource_snapshots WHERE session_id = ?",
                    (normalized_session,),
                ).fetchone()
                if winner is None:
                    raise TeamError(500, "resource_snapshot_failed", "Session resources could not be captured") from None
                return self._public_snapshot(winner)
            row = conn.execute(
                "SELECT * FROM team_session_resource_snapshots WHERE session_id = ?",
                (normalized_session,),
            ).fetchone()
            assert row is not None
            return self._public_snapshot(row)

    def snapshot_for_binding(self, session_id: str) -> dict[str, object]:
        """Return a frozen snapshot for a currently authorized binding.

        This is an internal controller seam.  It intentionally accepts no
        client actor and never fills a missing snapshot from the live space
        selection.  The grant's current owner/member fence is the authority
        for the lookup; callers should invoke it immediately before runtime
        assembly.
        """

        normalized_session = _identifier(session_id, "session_id", _MAX_SESSION_ID)
        binding = self.grants.binding(normalized_session, check_current=True)
        with self.identity._connection() as conn:
            # Re-read the binding fence after the grant lookup as well.  This
            # closes the small revoke race before a controller assembles the
            # fixed package paths for a worker.
            self._require_binding_owner_conn(
                conn,
                str(binding["ownerUserId"]),
                str(binding["spaceId"]),
                normalized_session,
            )
            row = conn.execute(
                "SELECT * FROM team_session_resource_snapshots WHERE session_id = ?",
                (normalized_session,),
            ).fetchone()
            if row is None or str(row["space_id"]) != str(binding["spaceId"]):
                raise _session_not_found()
            return self._public_snapshot(row)

    def session_snapshot(
        self,
        actor: str,
        space_id: str,
        session_id: str,
    ) -> dict[str, object]:
        actor_id = _identifier(actor, "actor_user_id", _MAX_ACTOR_ID)
        normalized_space = _identifier(space_id, "space_id", _MAX_SPACE_ID)
        normalized_session = _identifier(session_id, "session_id", _MAX_SESSION_ID)
        self._require_session_read(actor_id, normalized_space, normalized_session)
        with self.identity._connection() as conn:
            # Re-read the owner binding/member revision after the grant check.
            # A revocation that wins between those two reads therefore still
            # denies this snapshot request.
            binding = self._require_binding_read_conn(
                conn,
                actor_id,
                normalized_space,
                normalized_session,
            )
            row = conn.execute(
                "SELECT * FROM team_session_resource_snapshots WHERE session_id = ?",
                (normalized_session,),
            ).fetchone()
            if row is None or str(row["space_id"]) != normalized_space:
                raise _session_not_found()
            if str(binding["space_id"]) != normalized_space:
                raise _session_not_found()
            return self._public_snapshot(row)

    def _validate_staged(
        self,
        staged: StagedTeamPackage,
    ) -> tuple[str, str, str, str]:
        if not isinstance(staged, StagedTeamPackage):
            raise TeamError(400, "invalid_package", "publish requires a verified staged package")
        package_id = staged.package_id
        if (
            not isinstance(package_id, str)
            or _PACKAGE_ID.fullmatch(package_id) is None
            or package_id.startswith("/")
            or package_id.endswith("/")
            or "//" in package_id
            or any(part in {".", ".."} for part in package_id.split("/"))
        ):
            raise _invalid("package_id", "is not a valid package identity")
        version = _identifier(staged.version, "version", _MAX_VERSION)
        digest = staged.digest
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise TeamError(400, "invalid_package", "staged package digest is invalid")
        metadata = _public_metadata(staged.public_metadata)
        if metadata.get("packageId") != package_id or metadata.get("version") != version:
            raise TeamError(
                400,
                "invalid_package",
                "staged package metadata does not match its identity",
            )
        metadata_json = _canonical_json(metadata, field="metadata")
        return package_id, version, digest, metadata_json

    def _selection_ids(self, publication_ids: Sequence[str]) -> list[str]:
        if isinstance(publication_ids, (str, bytes, bytearray)) or not isinstance(
            publication_ids, Sequence
        ):
            raise _invalid("publication_ids", "must be a list")
        if len(publication_ids) > _MAX_SELECTION:
            raise TeamError(400, "selection_limit", "a Session can select at most 16 packages")
        result: list[str] = []
        for value in publication_ids:
            item = _identifier(value, "publication_id", _MAX_PUBLICATION_ID)
            if item in result:
                raise TeamError(400, "duplicate_selection", "a publication may be selected only once")
            result.append(item)
        return result

    def _published_rows_by_ids(
        self,
        conn: sqlite3.Connection,
        publication_ids: Sequence[str],
    ) -> dict[str, sqlite3.Row]:
        if not publication_ids:
            return {}
        placeholders = ",".join("?" for _ in publication_ids)
        rows = conn.execute(
            f"SELECT * FROM team_published_resources WHERE publication_id IN ({placeholders})",
            tuple(publication_ids),
        ).fetchall()
        by_id = {str(row["publication_id"]): row for row in rows}
        if len(by_id) != len(publication_ids):
            raise _resource_not_found("published resource not found")
        return by_id

    def _selection_from_db(
        self,
        conn: sqlite3.Connection,
        space_id: str,
    ) -> dict[str, object]:
        row = conn.execute(
            "SELECT * FROM team_space_resource_selections WHERE space_id = ?",
            (space_id,),
        ).fetchone()
        if row is None:
            return {
                "spaceId": space_id,
                "revision": 0,
                "publicationIds": [],
                "items": [],
                "updatedByUserId": "",
                "updatedAtMs": 0,
            }
        ids = [
            str(item)
            for item in _json_list(
                row["publication_ids_json"],
                field="selection publication_ids",
                maximum=16 * 1024,
            )
        ]
        if len(ids) > _MAX_SELECTION or len(ids) != len(set(ids)):
            raise TeamError(500, "resource_store_corrupt", "resource selection is invalid")
        resources = self._published_rows_by_ids(conn, ids)
        return {
            "spaceId": space_id,
            "revision": int(row["revision"]),
            "publicationIds": ids,
            "items": [self._public_resource(resources[item]) for item in ids],
            "updatedByUserId": str(row["updated_by_user_id"] or ""),
            "updatedAtMs": int(row["updated_at_ms"]),
        }

    def _public_resource(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            "publicationId": str(row["publication_id"]),
            "packageId": str(row["package_id"]),
            "version": str(row["version"]),
            "digest": str(row["digest"]),
            "status": str(row["status"]),
            "metadata": _json_object(row["metadata_json"], field="resource metadata"),
            "publishedByUserId": str(row["published_by_user_id"]),
            "publishedAtMs": int(row["published_at_ms"]),
            "updatedAtMs": int(row["updated_at_ms"]),
        }

    def _public_snapshot(self, row: sqlite3.Row) -> dict[str, object]:
        raw_ids = _json_list(
            row["publication_ids_json"],
            field="snapshot publication_ids",
            maximum=16 * 1024,
        )
        raw_items = _json_list(
            row["items_json"],
            field="snapshot items",
            maximum=_MAX_METADATA_BYTES,
        )
        if any(not isinstance(item, dict) for item in raw_items):
            raise TeamError(500, "resource_store_corrupt", "Session resource snapshot is invalid")
        return {
            "sessionId": str(row["session_id"]),
            "spaceId": str(row["space_id"]),
            "selectionRevision": int(row["selection_revision"]),
            "publicationIds": [str(item) for item in raw_ids],
            "items": [cast(dict[str, object], item) for item in raw_items],
            "createdAtMs": int(row["created_at_ms"]),
        }

    def _require_space(
        self,
        conn: sqlite3.Connection,
        actor: str,
        space_id: str,
        *,
        action: str,
    ) -> dict[str, object]:
        user = self.identity._require_active_user(conn, actor)
        space, role, membership_revision = self.identity._space_access(conn, actor, space_id, action)
        if str(space["kind"]) == "personal":
            membership_revision = int(user["authorization_revision"])
        if action == "read":
            return {"id": str(space["id"]), "kind": str(space["kind"]), "role": role, "membershipRevision": membership_revision}
        return {"id": str(space["id"]), "kind": str(space["kind"]), "role": role, "membershipRevision": membership_revision}

    def _require_session_owner(self, actor: str, space_id: str, session_id: str) -> None:
        try:
            self.grants.require_session(actor, space_id, session_id, action="write")
        except TeamError as exc:
            if exc.status in {403, 404}:
                raise _session_not_found() from None
            raise

    def _require_session_read(self, actor: str, space_id: str, session_id: str) -> None:
        try:
            self.grants.require_session(actor, space_id, session_id, action="read")
        except TeamError as exc:
            if exc.status in {403, 404}:
                raise _session_not_found() from None
            raise

    def _require_binding_owner_conn(
        self,
        conn: sqlite3.Connection,
        actor: str,
        space_id: str,
        session_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM team_session_bindings WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None or str(row["space_id"]) != space_id or str(row["owner_user_id"]) != actor:
            raise _session_not_found()
        if not bool(row["active"]):
            raise _session_not_found()
        scope = self._require_space(conn, actor, space_id, action="write")
        if int(row["membership_revision"]) != int(scope["membershipRevision"]):
            raise _session_not_found()
        return row

    def _require_binding_read_conn(
        self,
        conn: sqlite3.Connection,
        actor: str,
        space_id: str,
        session_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM team_session_bindings WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None or str(row["space_id"]) != space_id or not bool(row["active"]):
            raise _session_not_found()
        owner = str(row["owner_user_id"])
        owner_scope = self._require_space(conn, owner, space_id, action="write")
        if int(row["membership_revision"]) != int(owner_scope["membershipRevision"]):
            raise _session_not_found()
        self._require_space(conn, actor, space_id, action="read")
        if owner != actor and str(row["audience"]) != "project":
            raise _session_not_found()
        return row


__all__ = ["TeamSharedResourceStore"]
